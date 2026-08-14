"""§6 — 패시브 카드.

패시브는 손에 들어오지 않는다. 런을 시작하기 전에 파티 공유 슬롯
(`accounts.passive_slots`, 기본 2에서 연구로 최대 4까지 — §9)에 장착하고,
전투가 시작되면 정해진 시점에 스스로 발동한다.

발동 시점은 §6이 말하는 두 갈래를 그대로 옮긴 것이다:

    battle_start  전투가 시작할 때 한 번. `duration_rounds = -1` 과 함께 쓰면
                  전투 내내 유지되는 "상시" 효과가 된다.
    round_start   라운드마다. `trigger_params_json` 의 조건과 `once_per_battle`
                  을 붙이면 §6 이 말하는 "전투 중 조건부 발동" 이 된다 —
                  예: 파티원이 40% 밑으로 떨어진 그 라운드에 한 번만.

**시전자가 없다.** 카드는 누가 냈는지가 분명하지만 패시브는 그렇지 않아서,
시전자의 공격력에 기대는 연산자(`deal_damage`)나 손패를 건드리는 연산자
(`draw_cards`)는 패시브 효과 목록에 쓸 수 없다. 그 제한은 여기서 조용히
무시하는 대신 §10.5 검증(`validation._validate_passives`)이 발행 시점에
막는다 — 운영자가 대시보드에서 잘못 넣었을 때 전투 중이 아니라 저장할 때
알아야 하기 때문이다.
"""

from __future__ import annotations

import json

import app.content.operators as ops
import app.engine.effects as fx
import app.engine.units as un
from app.db.connection import Database

TRIGGER_BATTLE_START = "battle_start"
TRIGGER_ROUND_START = "round_start"

TRIGGERS = (TRIGGER_BATTLE_START, TRIGGER_ROUND_START)

SCOPE_PARTY = "party"
SCOPE_ENEMIES = "enemies"
SCOPES = (SCOPE_PARTY, SCOPE_ENEMIES)

#: 패시브 효과 목록에 쓸 수 있는 연산자. 시전자를 필요로 하지 않고, 손패나
#: 런 덱처럼 "지금 턴을 잡은 사람" 이 있어야 뜻이 통하는 상태를 건드리지 않는
#: 것만 남겼다. §10.4.6 으로 새 연산자가 등록되면 여기에 넣을지 따로 판단한다.
ALLOWED_OPERATORS = frozenset({
    "modify_stat",
    "set_invulnerable",
    "grant_block",
    "heal",
    "apply_status",
    "remove_status",
    "modify_resource",
    "modify_hp",
    "deal_flat_damage",
})


def definition(db: Database, content_version_id: int,
               passive_card_id: str) -> dict | None:
    row = db.one(
        "SELECT * FROM passive_cards WHERE content_version_id = ? "
        "AND passive_card_id = ?", (content_version_id, passive_card_id),
    )
    return dict(row) if row else None


def available(db: Database, content_version_id: int) -> list[dict]:
    """발행된 패시브 전부 (은퇴한 것 제외)."""
    return [dict(row) for row in db.query(
        "SELECT * FROM passive_cards WHERE content_version_id = ? "
        "AND is_retired = 0 ORDER BY rarity_tier DESC, passive_card_id",
        (content_version_id,),
    )]


def owned(db: Database, user_id: int, content_version_id: int) -> list[dict]:
    """§6 획득 — 가챠로 해금한 것만 고를 수 있다."""
    return [dict(row) for row in db.query(
        "SELECT p.* FROM passive_cards AS p "
        "JOIN unlocked_passives AS u ON u.passive_card_id = p.passive_card_id "
        "WHERE p.content_version_id = ? AND u.user_id = ? AND p.is_retired = 0 "
        "ORDER BY p.rarity_tier DESC, p.passive_card_id",
        (content_version_id, user_id),
    )]


# =====================================================================
# 전투 스냅샷
# =====================================================================
def snapshot_battle(db: Database, battle_id: int, run_id: int) -> int:
    """런의 장착 패시브를 전투에 고정한다 (§16.2.3 과 같은 이유).

    전투가 도는 동안 `run_passives` 가 바뀌어도 이 전투는 시작할 때의 구성으로
    끝난다. 재시도로 새 전투 행이 생기면 그때 다시 찍힌다.
    """
    rows = db.query(
        "SELECT passive_slot, passive_card_id FROM run_passives WHERE run_id = ? "
        "ORDER BY passive_slot", (run_id,),
    )
    count = 0
    for row in rows:
        db.execute(
            "INSERT OR REPLACE INTO battle_passives (battle_id, passive_slot, "
            "passive_card_id, fired_count) VALUES (?, ?, ?, 0)",
            (battle_id, row["passive_slot"], row["passive_card_id"]),
        )
        count += 1
    return count


def equipped(db: Database, battle_id: int, content_version_id: int) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT bp.passive_slot, bp.fired_count, p.* FROM battle_passives AS bp "
        "JOIN passive_cards AS p ON p.passive_card_id = bp.passive_card_id "
        "WHERE bp.battle_id = ? AND p.content_version_id = ? "
        "ORDER BY bp.passive_slot",
        (battle_id, content_version_id),
    )]


# =====================================================================
# 발동
# =====================================================================
def fire(engine, trigger: str, *, context: dict | None = None) -> list[str]:
    """`trigger` 시점의 패시브를 슬롯 순서대로 발동하고 로그를 돌려준다.

    슬롯 순서로 도는 것은 결정성 때문이다 — 같은 구성이면 항상 같은 순서로
    적용된다. 실패한 패시브 하나가 전투를 멈추게 두지 않고, 어떤 패시브가
    왜 안 걸렸는지 로그로 남긴다.
    """
    log: list[str] = []
    for entry in equipped(engine.db, engine.battle_id, engine.content_version_id):
        if entry["trigger_event"] != trigger:
            continue
        limit = int(entry["once_per_battle"] or 0)
        if limit and int(entry["fired_count"]) >= limit:
            continue
        if not _condition_met(engine, entry, context or {}):
            continue

        targets = _targets(engine, entry["target_scope"])
        if not targets:
            continue

        effects = json.loads(entry["effects_json"])
        ctx = engine._context(targets[0], targets, ops.CTX_PASSIVE)
        fx.execute_effects(effects, ctx)

        engine.db.execute(
            "UPDATE battle_passives SET fired_count = fired_count + 1 "
            "WHERE battle_id = ? AND passive_slot = ?",
            (engine.battle_id, entry["passive_slot"]),
        )
        log.append(f"🜂 패시브 「{entry['name']}」 발동")
    return log


def _targets(engine, scope: str) -> list:
    side = un.ALLY if scope == SCOPE_PARTY else un.ENEMY
    return un.load_units(engine.db, engine.battle_id, side=side, living_only=True)


def _condition_met(engine, entry: dict, context: dict) -> bool:
    """`trigger_params_json` 이 거는 추가 조건.

    지금 있는 조건은 하나뿐이다: `hp_below` 는 파티에서 한 명이라도 최대 HP의
    그 비율 밑으로 떨어졌을 때만 통과시킨다. 조건이 없으면 항상 통과한다.
    """
    params = json.loads(entry["trigger_params_json"] or "{}")
    threshold = params.get("hp_below")
    if threshold is None:
        return True
    allies = un.load_units(engine.db, engine.battle_id, side=un.ALLY,
                           living_only=True)
    return any(unit.hp_percent < float(threshold) for unit in allies)
