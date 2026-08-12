"""런 사이 계정 진행 — §3.4.3 튜토리얼 완료 · §4.4 성급 상승 · §8.4 장비 강화
· §9.2 연구 · §7.2 허브 상점.

여기의 모든 조작은 런 **밖**에서 일어난다. §16.2.3에 따라 런 진행 중에도 허용되며,
그 효과는 **다음 런**부터 적용된다 — 진행 중인 런은 자신의 빌드 스냅샷을 읽기
때문이다.

§17.1 [v6.3, B-07]: **성급 상승은 §17 범위에서 잘못 제외되어 있었다.** §15.4가
성급 상승에 코인 비용을 부여하므로 이것은 정확히 §17이 보호하려는 Central+로컬
혼합 연산이다. deduct 트랜잭션의 `local_payload`가 **세 가지 로컬 효과 전부**
(조각 차감, 와일드카드 차감, 성급 증가)를 담고 하나의 fulfillment receipt(§17.6)
아래 한 로컬 트랜잭션으로 적용된다. 카드 업그레이드도 동일한 형태이므로 P-1이
§17을 다시 손댈 필요가 없도록 여기에 미리 등록해 둔다.
"""

from __future__ import annotations

import logging
import uuid

from app.central import transactions as tx
from app.content.balance import Balance
from app.db.connection import Database, utcnow
from app.engine import achievements as ach

logger = logging.getLogger(__name__)


class ProgressionError(RuntimeError):
    """플레이어에게 보여줄 사유를 담는다 (§19.4 문자열은 호출자가 매핑)."""


def local_handlers() -> dict:
    """§17.4 복구가 사용할 operation → 로컬 적용 함수 매핑.

    이것이 없으면 코인 차감 후 크래시한 트랜잭션이 `apply_local=None`으로
    재개되어 `completed`로 표시되고, **코인은 빠졌는데 로컬 효과는 영원히
    유실된다.** 시작 시 스캔은 반드시 이 맵을 넘겨야 한다.
    """
    return {
        "star_up": _apply_star_up,
        "research": _apply_research,
        "hub_equipment": _apply_hub_equipment,
        "hub_stone": _apply_hub_stone,
    }


def _attempt_tx_id(db: Database, base: str) -> str:
    """같은 업무 이벤트의 재시도는 같은 키를 쓰되, **끝난 실패 시도**는 새 키를
    받는다.

    §17.3 규칙 2는 요청이 타임아웃했다고 새 키를 만드는 것을 금지하지만, 그것은
    *같은 시도*에 대한 이야기다. `rejected_no_charge`나 `compensated`(코인 부족
    같은 평범한 결과)는 종료 상태이므로, 결정적 키를 그대로 재사용하면
    `run_transaction`이 단락되어 그 조작이 계정에서 영구히 막혀 버린다.
    """
    row = db.one(
        "SELECT tx_id, status FROM purchase_transactions WHERE tx_id = ? OR "
        "tx_id LIKE ? ORDER BY created_at DESC LIMIT 1", (base, f"{base}#%"))
    if row is None:
        return base
    if row["status"] not in tx.TERMINAL_STATUSES:
        return row["tx_id"]      # 진행 중인 시도 — 같은 키로 재개한다

    finished = db.one(
        "SELECT COUNT(*) AS n FROM purchase_transactions WHERE tx_id = ? OR "
        "tx_id LIKE ?", (base, f"{base}#%"))
    return f"{base}#{int(finished['n']) + 1}"


# =====================================================================
# §3.4.3 튜토리얼 완료 보상
# =====================================================================
def complete_tutorial(db: Database, balance: Balance, *, user_id: int,
                      content_version_id: int) -> dict:
    """파티 슬롯 2 + 카르타 300 + 다음 월드 해금. 튜토리얼은 영구 잠금.

    [v6.3] "두 번째 캐릭터" 항목은 이 표에서 **삭제되었다.** v6.2는 튜토리얼
    완료가 두 번째 캐릭터를 *지급*한다고 했지만 §5.10은 실제로는 뽑기 배치
    보장을 구현하고 있었다 — 서로 다른 두 가지다. 진짜 보장은 전적으로 §5.10에
    있고 뽑기에서 발동한다. 여기서는 **파티 슬롯 2**만 지급하고, 그 슬롯을
    **채우는 것**은 §5.10의 일이다.

    멱등하다: 이미 완료된 계정에 다시 호출해도 아무것도 지급하지 않는다.
    """
    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    if account is None:
        raise ProgressionError("계정이 존재하지 않습니다.")
    if account["tutorial_completed_at"] is not None:
        return {"already_completed": True}

    slot = int(balance.get("tutorial_reward_party_slot"))
    carta = int(balance.get("tutorial_reward_carta"))

    with db.tx() as conn:
        conn.execute(
            "UPDATE accounts SET tutorial_completed_at = ?, party_slots = MAX(party_slots, ?), "
            "carta = carta + ?, updated_at = ? WHERE user_id = ?",
            (utcnow(), slot, carta, utcnow(), user_id),
        )
        # 완료 시 튜토리얼 월드는 이용 불가가 되고, 계정은 본편으로 전환된다.
        conn.execute(
            "DELETE FROM world_unlocks WHERE user_id = ? AND world_id IN "
            "(SELECT world_id FROM worlds WHERE content_version_id = ? "
            "AND is_tutorial = 1)",
            (user_id, content_version_id),
        )
        first_world = conn.execute(
            "SELECT world_id FROM worlds WHERE content_version_id = ? "
            "AND is_tutorial = 0 ORDER BY sequence_index LIMIT 1",
            (content_version_id,),
        ).fetchone()
        if first_world is not None:
            conn.execute(
                "INSERT OR IGNORE INTO world_unlocks (user_id, world_id, unlocked_at) "
                "VALUES (?, ?, ?)", (user_id, first_world["world_id"], utcnow()),
            )

    return {"already_completed": False, "party_slots": slot, "carta": carta,
            "unlocked_world": first_world["world_id"] if first_world else None}


# =====================================================================
# §4.4 성급 상승 — §17.1 B-07
# =====================================================================
def star_up_cost(balance: Balance, current_rank: int) -> dict:
    costs = balance.get("star_up_costs")
    entry = costs.get(str(current_rank))
    if entry is None:
        raise ProgressionError("더 이상 성급을 올릴 수 없습니다.")
    return dict(entry)


def star_cap(balance: Balance, special_cap: bool) -> int:
    """일반 상한 3★, `special_cap` 플래그가 붙은 캐릭터는 6★ (§4.4)."""
    return int(balance.get("star_cap_special" if special_cap else "star_cap_normal"))


def check_star_up(db: Database, balance: Balance, *, user_id: int,
                  character_id: str, content_version_id: int) -> dict:
    """비용과 가능 여부를 계산한다. 코인 잔액은 확인하지 않는다 — 사전 잔액
    조회는 UX 전용이며 절대 권위 있는 검사가 아니다(§17.3)."""
    owned = db.one(
        "SELECT star_rank FROM owned_characters WHERE user_id = ? AND character_id = ?",
        (user_id, character_id))
    if owned is None:
        raise ProgressionError("보유하지 않은 캐릭터입니다.")

    definition = db.one(
        "SELECT special_cap FROM characters WHERE content_version_id = ? "
        "AND character_id = ?", (content_version_id, character_id))
    if definition is None:
        raise ProgressionError("정의되지 않은 캐릭터입니다.")

    current = int(owned["star_rank"])
    cap = star_cap(balance, bool(definition["special_cap"]))
    if current >= cap:
        raise ProgressionError(f"이미 최대 성급({cap}★)입니다.")

    cost = star_up_cost(balance, current)
    fragments = db.one(
        "SELECT amount FROM character_fragments WHERE user_id = ? AND character_id = ?",
        (user_id, character_id))
    account = db.one("SELECT wildcards FROM accounts WHERE user_id = ?", (user_id,))

    have_fragments = int(fragments["amount"]) if fragments else 0
    have_wildcards = int(account["wildcards"])
    return {
        "current_rank": current,
        "next_rank": current + 1,
        "cost": cost,
        "have_fragments": have_fragments,
        "have_wildcards": have_wildcards,
        "affordable_locally": (have_fragments >= cost["fragments"]
                               and have_wildcards >= cost["wildcards"]),
    }


def star_up(db: Database, balance: Balance, central, *, user_id: int,
            character_id: str, content_version_id: int,
            tx_id: str | None = None) -> tx.TransactionResult:
    """코인을 차감하는 `deduct` 트랜잭션으로 실행한다 (§17.1).

    로컬 페이로드가 세 효과 전부를 담기 때문에, 순진한 구현이 저지를 수 있는
    "코인만 빼고 조각을 소모하지 못함" 또는 "돈을 안 받고 성급만 올림"이
    구조적으로 불가능하다.
    """
    plan = check_star_up(db, balance, user_id=user_id, character_id=character_id,
                         content_version_id=content_version_id)
    if not plan["affordable_locally"]:
        raise ProgressionError("재화가 부족합니다.")

    tx_id = tx_id or _attempt_tx_id(
        db, f"starup:{user_id}:{character_id}:{plan['current_rank']}")
    tx.create_transaction(
        db, tx_id=tx_id, user_id=user_id, operation="star_up",
        direction=tx.DEDUCT, expected_coin_delta=-int(plan["cost"]["coin"]),
        local_payload={
            "kind": "star_up",
            "user_id": user_id,
            "character_id": character_id,
            "from_rank": plan["current_rank"],
            "fragments": plan["cost"]["fragments"],
            "wildcards": plan["cost"]["wildcards"],
        },
    )
    return tx.run_transaction(db, central, tx_id=tx_id,
                              apply_local=_apply_star_up, kind="star_up")


def _apply_star_up(db: Database, payload: dict) -> None:
    """세 로컬 효과를 하나의 로컬 트랜잭션에서 적용한다 (§17.1).

    호출자가 이미 fulfillment receipt를 같은 트랜잭션에 삽입했으므로, 재시도는
    기본 키에서 실패하고 롤백되어 두 번 적용되지 않는다.
    """
    character_id = payload["character_id"]
    uid = int(payload["user_id"])

    fragments = db.one(
        "SELECT amount FROM character_fragments WHERE user_id = ? AND character_id = ?",
        (uid, character_id))
    if fragments is None or int(fragments["amount"]) < int(payload["fragments"]):
        raise ProgressionError("캐릭터 조각이 부족합니다.")
    account = db.one("SELECT wildcards FROM accounts WHERE user_id = ?", (uid,))
    if int(account["wildcards"]) < int(payload["wildcards"]):
        raise ProgressionError("와일드카드가 부족합니다.")

    db.execute(
        "UPDATE character_fragments SET amount = amount - ? WHERE user_id = ? "
        "AND character_id = ?", (int(payload["fragments"]), uid, character_id))
    db.execute(
        "UPDATE accounts SET wildcards = wildcards - ?, updated_at = ? "
        "WHERE user_id = ?", (int(payload["wildcards"]), utcnow(), uid))
    db.execute(
        "UPDATE owned_characters SET star_rank = star_rank + 1 WHERE user_id = ? "
        "AND character_id = ?", (uid, character_id))


# =====================================================================
# §8.4 장비 강화 — 순수 로컬, Central 호출 없음
# =====================================================================
def enhancement_cost(balance: Balance, target_tier: int) -> dict:
    """티어 N으로 올리는 비용: `{N}티어 강화석 × A(N)` + `{N-1}티어 강화석 × B(N)`.

    B(1)은 면제 — T0 강화석은 존재하지 않는다.
    """
    costs = balance.get("enhancement_costs")
    entry = costs.get(str(target_tier))
    if entry is None:
        raise ProgressionError("해당 티어의 강화 비용이 정의되어 있지 않습니다.")
    return {"current_tier_stones": int(entry["current"]),
            "previous_tier_stones": int(entry["previous"])}


def enhance_equipment(db: Database, balance: Balance, *, user_id: int,
                      equipment_instance_id: int,
                      content_version_id: int) -> dict:
    """강화 실행은 **순수 로컬**이다 — 로컬 강화석을 소모하고 로컬 티어를 쓰므로
    Central 호출이 필요 없다 (§8.4).

    **실패 없음.** 100% 성공, 파괴/하락/보호 아이템 없음.
    """
    item = db.one(
        "SELECT * FROM owned_equipment WHERE equipment_instance_id = ? AND user_id = ?",
        (equipment_instance_id, user_id))
    if item is None:
        raise ProgressionError("보유하지 않은 장비입니다.")

    target = int(item["tier"]) + 1
    max_tier = int(balance.get("equipment_max_tier"))
    if target > max_tier:
        raise ProgressionError(f"이미 최대 티어(T{max_tier})입니다.")

    cost = enhancement_cost(balance, target)
    need = {target: cost["current_tier_stones"]}
    if cost["previous_tier_stones"]:
        need[target - 1] = cost["previous_tier_stones"]

    for tier, amount in need.items():
        row = db.one(
            "SELECT amount FROM enhancement_stones WHERE user_id = ? AND tier = ?",
            (user_id, tier))
        if row is None or int(row["amount"]) < amount:
            raise ProgressionError(
                f"{tier}티어 장비 강화석이 부족합니다. "
                f"({int(row['amount']) if row else 0}/{amount})")

    with db.tx() as conn:
        for tier, amount in need.items():
            conn.execute(
                "UPDATE enhancement_stones SET amount = amount - ? WHERE user_id = ? "
                "AND tier = ?", (amount, user_id, tier))
        conn.execute(
            "UPDATE owned_equipment SET tier = ? WHERE equipment_instance_id = ?",
            (target, equipment_instance_id))

    # §20.2 엔진 훅 — 업적 카운터는 런 밖 조작이므로 receipt로 멱등성을 보장한다.
    ach.advance_counter(
        db, user_id, ach.EQUIPMENT_TIERED, 1,
        mutation_id=f"enhance:{equipment_instance_id}:T{target}",
        content_version_id=content_version_id)

    return {"equipment_instance_id": equipment_instance_id, "tier": target,
            "spent": need}


# =====================================================================
# §9.2 연구 시스템
# =====================================================================
def _steps_taken(db: Database, user_id: int, node_id: str) -> int:
    """이 노드에 대해 해금된 단계 수.

    `node_id LIKE '<id>%'`를 쓰지 않는다: SQLite에서 `_`는 LIKE 와일드카드이고
    (`res_스탯강화` 같은 id가 전부 그렇다), 한 노드 id가 다른 것의 접두사이면
    단계 수를 잘못 세어 이미 지불한 해금을 영구히 거부할 수 있다.
    """
    rows = db.query(
        "SELECT node_id FROM research_unlocks WHERE user_id = ?", (user_id,))
    prefix = f"{node_id}:"
    return sum(1 for row in rows
               if row["node_id"] == node_id or row["node_id"].startswith(prefix))


def research_status(db: Database, *, user_id: int,
                    content_version_id: int) -> list[dict]:
    """§20.5 — 연구 화면은 필요 업적과 그 진행도를 인라인으로 보여주므로,
    잠긴 노드가 단순히 거부하는 대신 스스로를 설명한다."""
    listing = []
    for node in db.query(
        "SELECT * FROM research_nodes WHERE content_version_id = ? ORDER BY node_id",
        (content_version_id,),
    ):
        steps_taken = _steps_taken(db, user_id, node["node_id"])
        required = node["required_achievement"]
        progress = None
        if required:
            row = db.one(
                "SELECT p.current_value, a.target_value, a.name FROM achievements a "
                "LEFT JOIN achievement_progress p ON p.achievement_id = a.achievement_id "
                "AND p.user_id = ? WHERE a.content_version_id = ? "
                "AND a.achievement_id = ?",
                (user_id, content_version_id, required))
            if row is not None:
                progress = {"name": row["name"],
                            "current": int(row["current_value"] or 0),
                            "target": int(row["target_value"])}
        listing.append({
            "node_id": node["node_id"],
            "name": node["name"],
            "coin_cost": _step_coin_cost(node, steps_taken),
            "wildcard_cost": int(node["wildcard_cost"]),
            "required_achievement": required,
            "achievement_progress": progress,
            "steps_taken": steps_taken,
            "max_steps": int(node["max_steps"]),
            "available": (steps_taken < int(node["max_steps"])
                          and (not required
                               or ach.is_completed(db, user_id, required))),
        })
    return listing


def _step_coin_cost(node, steps_taken: int) -> int:
    """스탯 강화는 10단계이며 단계당 코인 2,000 × step (§9.2)."""
    if int(node["max_steps"]) > 1:
        return int(node["coin_cost"]) * (steps_taken + 1)
    return int(node["coin_cost"])


def unlock_research(db: Database, balance: Balance, central, *, user_id: int,
                    node_id: str, content_version_id: int,
                    tx_id: str | None = None) -> tx.TransactionResult:
    """즉시 해금 — 타이머 없음. 비용은 코인(Central) + 와일드카드(로컬)이며,
    **선행 조건은 로컬 업적**이다: 연구는 이제 비용 **더하기** 선행 조건이다.
    """
    node = db.one(
        "SELECT * FROM research_nodes WHERE content_version_id = ? AND node_id = ?",
        (content_version_id, node_id))
    if node is None:
        raise ProgressionError("정의되지 않은 연구 노드입니다.")

    steps_taken = _steps_taken(db, user_id, node_id)
    if steps_taken >= int(node["max_steps"]):
        raise ProgressionError("이미 해금한 연구입니다.")

    required = node["required_achievement"]
    if required and not ach.is_completed(db, user_id, required):
        raise ProgressionError("선행 업적을 먼저 달성해 주세요.")

    wildcard_cost = int(node["wildcard_cost"])
    account = db.one("SELECT wildcards FROM accounts WHERE user_id = ?", (user_id,))
    if int(account["wildcards"]) < wildcard_cost:
        raise ProgressionError("재화가 부족합니다.")

    step_id = node_id if int(node["max_steps"]) == 1 else f"{node_id}:{steps_taken + 1}"
    tx_id = tx_id or _attempt_tx_id(db, f"research:{user_id}:{step_id}")
    tx.create_transaction(
        db, tx_id=tx_id, user_id=user_id, operation="research",
        direction=tx.DEDUCT,
        expected_coin_delta=-_step_coin_cost(node, steps_taken),
        local_payload={"kind": "research", "user_id": user_id,
                       "node_id": node_id, "step_id": step_id,
                       "wildcards": wildcard_cost,
                       "effect_json": node["effect_json"]},
    )
    return tx.run_transaction(db, central, tx_id=tx_id,
                              apply_local=_apply_research, kind="research")


def _apply_research(db: Database, payload: dict) -> None:
    import json

    user_id = int(payload["user_id"])
    effect = json.loads(payload["effect_json"])
    wildcards = int(payload["wildcards"])

    # 로컬 트랜잭션 안에서 다시 검증한다. 사전 검사와 이 적용 사이에 다른
    # 조작(성급 상승)이 와일드카드를 소모했을 수 있고, 잔액이 음수가 되면
    # 이후의 모든 연구와 성급 상승이 막힌다.
    account = db.one("SELECT wildcards FROM accounts WHERE user_id = ?", (user_id,))
    if account is None or int(account["wildcards"]) < wildcards:
        raise ProgressionError("와일드카드가 부족합니다.")

    db.execute(
        "UPDATE accounts SET wildcards = wildcards - ?, updated_at = ? "
        "WHERE user_id = ?", (wildcards, utcnow(), user_id))
    db.execute(
        "INSERT OR IGNORE INTO research_unlocks (user_id, node_id, unlocked_at) "
        "VALUES (?, ?, ?)", (user_id, payload["step_id"], utcnow()))

    kind = effect.get("kind")
    if kind == "party_slot":
        db.execute(
            "UPDATE accounts SET party_slots = MAX(party_slots, ?) WHERE user_id = ?",
            (int(effect["value"]), user_id))
    elif kind == "passive_slot":
        db.execute(
            "UPDATE accounts SET passive_slots = MAX(passive_slots, ?) "
            "WHERE user_id = ?", (int(effect["value"]), user_id))
    elif kind == "stat_step":
        db.execute(
            "UPDATE accounts SET stat_research_step = stat_research_step + 1 "
            "WHERE user_id = ?", (user_id,))


# =====================================================================
# §7.2 허브 상점 — 장비와 강화석을 코인으로
# =====================================================================
def hub_shop_listing(db: Database, balance: Balance, *,
                     content_version_id: int) -> dict:
    """전투 루프 밖이므로 Central 왕복이 허용된다 (§7.2)."""
    equipment = [
        {"equipment_def_id": row["equipment_def_id"], "name": row["name"],
         "slot": row["slot"], "price_coin": int(row["price_coin"])}
        for row in db.query(
            "SELECT * FROM equipment_defs WHERE content_version_id = ? "
            "ORDER BY price_coin, equipment_def_id", (content_version_id,))
    ]
    coefficient = int(balance.get("hub_stone_price_coefficient"))
    max_tier = int(balance.get("equipment_max_tier"))
    stones = [
        # 코인 400 × tier² — T1 400 … T6 14,400 (§15.4)
        {"tier": tier, "price_coin": coefficient * tier * tier}
        for tier in range(1, max_tier + 1)
    ]
    return {"equipment": equipment, "stones": stones}


def buy_hub_equipment(db: Database, balance: Balance, central, *, user_id: int,
                      equipment_def_id: str, content_version_id: int,
                      tx_id: str | None = None) -> tx.TransactionResult:
    definition = db.one(
        "SELECT * FROM equipment_defs WHERE content_version_id = ? "
        "AND equipment_def_id = ?", (content_version_id, equipment_def_id))
    if definition is None:
        raise ProgressionError("정의되지 않은 장비입니다.")

    tx_id = tx_id or f"hubequip:{uuid.uuid4().hex}"
    tx.create_transaction(
        db, tx_id=tx_id, user_id=user_id, operation="hub_equipment",
        direction=tx.DEDUCT, expected_coin_delta=-int(definition["price_coin"]),
        local_payload={"kind": "equipment", "user_id": user_id,
                       "equipment_def_id": equipment_def_id, "tx_id": tx_id},
    )
    return tx.run_transaction(db, central, tx_id=tx_id,
                              apply_local=_apply_hub_equipment, kind="equipment")


def _apply_hub_equipment(db: Database, payload: dict) -> None:
    """§17.6 — receipt가 없다면 구매가 커밋된 뒤 크래시했을 때 재시도가 두 번째
    인스턴스를 지급하게 된다. `source_tx_id`로 추적 가능성도 남긴다."""
    db.execute(
        "INSERT INTO owned_equipment (user_id, equipment_def_id, tier, source_tx_id) "
        "VALUES (?, ?, 0, ?)",
        (int(payload["user_id"]), payload["equipment_def_id"], payload["tx_id"]))


def buy_hub_stone(db: Database, balance: Balance, central, *, user_id: int,
                  tier: int, amount: int = 1,
                  tx_id: str | None = None) -> tx.TransactionResult:
    max_tier = int(balance.get("equipment_max_tier"))
    if not 1 <= tier <= max_tier:
        raise ProgressionError(f"강화석 티어는 1~{max_tier} 사이여야 합니다.")
    if amount < 1:
        raise ProgressionError("수량은 1개 이상이어야 합니다.")

    coefficient = int(balance.get("hub_stone_price_coefficient"))
    price = coefficient * tier * tier * amount

    tx_id = tx_id or f"hubstone:{uuid.uuid4().hex}"
    tx.create_transaction(
        db, tx_id=tx_id, user_id=user_id, operation="hub_stone",
        direction=tx.DEDUCT, expected_coin_delta=-price,
        local_payload={"kind": "stone", "user_id": user_id, "tier": tier,
                       "amount": amount},
    )
    return tx.run_transaction(db, central, tx_id=tx_id,
                              apply_local=_apply_hub_stone, kind="stone")


def _apply_hub_stone(db: Database, payload: dict) -> None:
    db.execute(
        "INSERT INTO enhancement_stones (user_id, tier, amount) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, tier) DO UPDATE SET amount = amount + excluded.amount",
        (int(payload["user_id"]), int(payload["tier"]), int(payload["amount"])))


# =====================================================================
# 장착 (§8.1 3종 · 캐릭터당 슬롯 1개)
# =====================================================================
def equip(db: Database, *, user_id: int, equipment_instance_id: int,
          character_id: str, content_version_id: int) -> dict:
    """장착은 계정 영구 상태다 (§12). 런 중 변경은 허용되지만 §16.2.3의 빌드
    스냅샷 때문에 **다음 런**부터 반영된다."""
    item = db.one(
        "SELECT * FROM owned_equipment WHERE equipment_instance_id = ? AND user_id = ?",
        (equipment_instance_id, user_id))
    if item is None:
        raise ProgressionError("보유하지 않은 장비입니다.")
    definition = db.one(
        "SELECT slot FROM equipment_defs WHERE content_version_id = ? "
        "AND equipment_def_id = ?", (content_version_id, item["equipment_def_id"]))
    if definition is None:
        raise ProgressionError("정의되지 않은 장비입니다.")
    owned = db.one(
        "SELECT 1 FROM owned_characters WHERE user_id = ? AND character_id = ?",
        (user_id, character_id))
    if owned is None:
        raise ProgressionError("보유하지 않은 캐릭터입니다.")

    slot = definition["slot"]
    with db.tx() as conn:
        # 캐릭터당 슬롯 1개 — UNIQUE 인덱스가 강제하므로 기존 장비를 먼저 해제한다.
        conn.execute(
            "UPDATE owned_equipment SET equipped_character_id = NULL, "
            "equipped_slot = NULL WHERE user_id = ? AND equipped_character_id = ? "
            "AND equipped_slot = ?", (user_id, character_id, slot))
        conn.execute(
            "UPDATE owned_equipment SET equipped_character_id = ?, equipped_slot = ? "
            "WHERE equipment_instance_id = ?",
            (character_id, slot, equipment_instance_id))
    return {"equipment_instance_id": equipment_instance_id,
            "character_id": character_id, "slot": slot}


def set_bonus_for(db: Database, *, user_id: int, character_id: str,
                  content_version_id: int) -> dict | None:
    """§8.2 — **풀세트 전용.** 3개 슬롯 전부가 같은 이름의 세트여야 하며,
    2피스 단계는 없다. 두 세트 이름을 섞으면 보너스가 **없다**."""
    import json

    rows = db.query(
        "SELECT ed.set_name FROM owned_equipment oe JOIN equipment_defs ed "
        "ON ed.equipment_def_id = oe.equipment_def_id AND ed.content_version_id = ? "
        "WHERE oe.user_id = ? AND oe.equipped_character_id = ?",
        (content_version_id, user_id, character_id))
    # NULL을 걸러내지 않고 그대로 모은다: 세트에 속하지 않는 조각이 하나라도
    # 끼어 있으면 이름 집합의 크기가 1을 넘어 보너스가 사라진다. 걸러내면
    # 2피스 + 무세트 1피스가 풀세트로 취급되어 §8.2를 어기게 된다.
    names = {row["set_name"] for row in rows}
    if len(rows) < 3 or len(names) != 1 or None in names:
        return None

    bonus = db.one(
        "SELECT bonus_json FROM equipment_sets WHERE content_version_id = ? "
        "AND set_name = ?", (content_version_id, names.pop()))
    return json.loads(bonus["bonus_json"]) if bonus else None
