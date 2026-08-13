"""§5.8 — 카드 업그레이드 시스템 [v6.4에서 P-1 확정].

구조적 제약 (v6.2에서 변경 없음):

    · 카드 조각은 (user_id, card_id)별로 저장된다
    · 카드 A의 조각은 카드 A에만 쓸 수 있다
    · 업그레이드는 **계정 단위 영구 변경**(`unlocked_cards.upgrade_tier`)이며
      런 스코프가 아니다 — 업그레이드된 카드는 이후 런에 업그레이드된 채로
      들어간다
    · 업그레이드 효과는 §10.4 연산자로 표현 가능해야 한다
    · 코인 소비이므로 §17의 적용을 받는다

티어는 5단계다: `upgrade_tier ∈ {0..5}`, 0은 뽑은 그대로의 상태이므로 전이는
0→1 … 4→5의 다섯 개다 (§5.8.1).

런 안에서의 해석은 §16.2.3 스냅샷을 통한다: `run_build_snapshot.card_upgrade_json`이
런 생성 시점의 티어를 얼려 두므로, 런 도중 업그레이드해도 진행 중인 전투의
수치가 바뀌지 않는다.
"""

from __future__ import annotations

import copy
import json

from app.db.connection import Database

#: §5.8.1 — 0은 뽑은 그대로의 상태. 상한은 §15 상수에서 읽는다
#: (`max_tier()`); 이 값은 상수가 없을 때의 폴백일 뿐이다.
MIN_TIER = 0
MAX_TIER = 5

#: §5.8.2 — 와일드카드는 2→3 전이부터 든다.
WILDCARD_FROM_TIER = 3

#: §5.8.3 — 능력 추가(카드에 새 `apply_status` 연산자를 붙이는 것)가 허용되는
#: 전이와, 그 전이에서 참조 가능한 상태 scope.
#:   0→1        능력 추가 없음, 숫자 변경만
#:   1→2, 2→3   `player_only` scope 상태만, 약한 강도
#:   3→4, 4→5   모든 scope, 강한 강도
ABILITY_ADDITION_NONE = ()
ABILITY_ADDITION_WEAK = ("player_only",)
ABILITY_ADDITION_FULL = ("player_only", "enemy_only", "universal")

ALLOWED_SCOPES_BY_TIER: dict[int, tuple[str, ...]] = {
    1: ABILITY_ADDITION_NONE,
    2: ABILITY_ADDITION_WEAK,
    3: ABILITY_ADDITION_WEAK,
    4: ABILITY_ADDITION_FULL,
    5: ABILITY_ADDITION_FULL,
}

#: §5.8.3 — 적용되는 상태의 지속시간 상한 (상태 자신의 base_duration에도 묶인다).
MAX_APPLIED_DURATION = 4

#: §5.8.3 — 숫자 변경으로 허용되는 연산자 (데미지/방어/회복 배율, 또는 비용 감소).
NUMERIC_OPERATORS = frozenset({"deal_damage", "deal_flat_damage", "grant_block",
                               "heal", "modify_cost"})


def max_tier(balance) -> int:
    """§15 — 어떤 값도 하드코딩하지 않는다. 대시보드에서 상한을 바꿀 수 있다."""
    return int(balance.get("card_upgrade_max_tier", MAX_TIER))


def upgrade_row(db: Database, content_version_id: int, card_id: str,
                target_tier: int):
    return db.one(
        "SELECT * FROM card_upgrades WHERE content_version_id = ? AND card_id = ? "
        "AND target_tier = ?", (content_version_id, card_id, target_tier))


def upgrade_path(db: Database, content_version_id: int,
                 card_id: str) -> list[dict]:
    """이 카드에 저작된 전이들을 티어 순으로."""
    return [dict(row) for row in db.query(
        "SELECT * FROM card_upgrades WHERE content_version_id = ? AND card_id = ? "
        "ORDER BY target_tier", (content_version_id, card_id))]


def next_cost(db: Database, content_version_id: int, card_id: str,
              current_tier: int) -> dict | None:
    """다음 티어의 비용. 최대 티어이거나 전이가 저작되지 않았으면 None."""
    if current_tier >= MAX_TIER:
        return None
    row = upgrade_row(db, content_version_id, card_id, current_tier + 1)
    if row is None:
        return None
    return {
        "target_tier": int(row["target_tier"]),
        "fragments": int(row["fragment_cost"]),
        "wildcards": int(row["wildcard_cost"]),
        "coin": int(row["coin_cost"]),
    }


def effective_card(db: Database, content_version_id: int, card_row,
                   upgrade_tier: int) -> dict:
    """카드 정의에 0..`upgrade_tier`의 전이 오버레이를 순서대로 적용한다.

    오버레이는 **누적**된다: 각 전이의 `effects_json`이 기본 효과 목록 위에
    덧씌워지며, 같은 연산자는 파라미터를 대체하고 새 연산자(능력 추가)는
    뒤에 붙는다. 원본 행은 건드리지 않는다 — §10.6의 콘텐츠 불변성 때문에
    카드 정의 자체는 버전 안에서 절대 변하지 않는다.
    """
    card = dict(card_row)
    effects = json.loads(card["effects_json"])
    cost = int(card["cost"])

    if upgrade_tier <= MIN_TIER:
        return {**card, "effects": effects, "cost": cost, "upgrade_tier": 0}

    for tier in range(1, min(upgrade_tier, MAX_TIER) + 1):
        row = upgrade_row(db, content_version_id, card["card_id"], tier)
        if row is None:
            # 저작되지 않은 전이는 조용히 건너뛴다: 소유 데이터가 콘텐츠보다
            # 앞서 있을 수 있고(§10.6), 그래도 카드는 렌더링되어야 한다.
            continue
        effects, cost = _apply_overlay(effects, cost, json.loads(row["effects_json"]))

    return {**card, "effects": effects, "cost": cost,
            "upgrade_tier": min(upgrade_tier, MAX_TIER)}


def _apply_overlay(effects: list[dict], cost: int,
                   overlay: list[dict]) -> tuple[list[dict], int]:
    result = copy.deepcopy(effects)
    for entry in overlay:
        operator = entry["operator"]
        params = entry.get("params") or {}

        if operator == "modify_cost":
            # 비용 감소는 카드의 cost 필드에 적용된다 — 연산자 목록에 남기면
            # 전투 중에 실행되어 버린다.
            cost = max(0, cost + int(params.get("delta", 0)))
            continue

        existing = _matching_entry(result, operator, params)
        if existing is not None:
            # 숫자 변경이든 같은 상태의 재저작이든, 이미 있는 항목은 대체한다.
            # 덧붙이면 T5 카드가 같은 상태를 두 번 걸어 스택이 의도의 두 배가
            # 된다 — 오버레이는 *누적*이지 *중복*이 아니다.
            existing["params"] = {**(existing.get("params") or {}), **params}
        else:
            # 능력 추가: 새 연산자를 뒤에 붙인다.
            result.append({"operator": operator, "params": params})
    return result, cost


def _matching_entry(effects: list[dict], operator: str, params: dict):
    """오버레이가 대체할 기존 항목.

    숫자 연산자는 종류가 같으면 같은 항목이다. `apply_status`는 *상태별*로
    구분된다: 한 카드가 서로 다른 두 상태를 거는 것은 정당하지만, 같은 상태를
    두 번 거는 것은 티어가 올라가며 값이 갱신된 것이다.
    """
    for entry in effects:
        if entry["operator"] != operator:
            continue
        if operator == "apply_status":
            if (entry.get("params") or {}).get("status_id") == params.get("status_id"):
                return entry
            continue
        if operator in NUMERIC_OPERATORS:
            return entry
    return None


def added_status_operators(overlay: list[dict]) -> list[dict]:
    """오버레이가 추가하는 `apply_status` 연산자들 (§5.8.3 게이트 대상)."""
    return [entry for entry in overlay if entry.get("operator") == "apply_status"]
