"""§10.5 — 문서만 약속하고 실제로는 없던 검사 둘.

`config/01_전투.toml` 은 `card_cost_min`/`card_cost_max` 를 "이 범위를 벗어난
카드는 저장이 거부됩니다" 라고 설명하지만, 그 검사는 존재하지 않았다. 그 틈으로
비용 0짜리 카드(`card_무_재정비`)가 실제로 발행되어 있었다 — 이 세션에서 콘텐츠를
늘리며 직접 만든 카드다.

`research_nodes.effect_json` 도 전혀 검사되지 않았다. 오타 난 `kind` 를 쓴
노드는 `_apply_research` 의 어느 `elif` 에도 걸리지 않아 **와일드카드와 코인만
나가고 아무 효과도 없이 끝난다** — `offer_reward` 가 비어 있던 것과 같은 모양의
버그다. `party_slot` 값에도 상한이 없어 실제로 지원하는 파티 인원보다 큰 값을
그대로 지급할 수 있었다.
"""

from __future__ import annotations

import json

import pytest

from app.content import validation
from app.content.operators import ValidationError
from app.engine import progression as pg


# =====================================================================
# 카드 비용 범위
# =====================================================================
def test_the_shipped_content_stays_inside_the_configured_range(db, version,
                                                               balance):
    """이 검사가 없어서 비용 0짜리 카드가 실제로 발행되어 있었다."""
    cost_min = int(balance.get("card_cost_min"))
    cost_max = int(balance.get("card_cost_max"))
    for row in db.query("SELECT card_id, cost FROM cards WHERE content_version_id = ?",
                        (version,)):
        assert cost_min <= row["cost"] <= cost_max, \
            f"{row['card_id']} 의 비용 {row['cost']} 이(가) 범위 밖입니다"


def test_a_card_below_the_minimum_is_rejected(db, version):
    db.execute("UPDATE cards SET cost = 0 WHERE content_version_id = ? "
               "AND card_id = 'card_평타'", (version,))
    with pytest.raises(ValidationError, match="허용 범위"):
        validation.validate_version(db, version)


def test_a_card_above_the_maximum_is_rejected(db, version, balance):
    over = int(balance.get("card_cost_max")) + 1
    db.execute("UPDATE cards SET cost = ? WHERE content_version_id = ? "
               "AND card_id = 'card_평타'", (over, version))
    with pytest.raises(ValidationError, match="허용 범위"):
        validation.validate_version(db, version)


# =====================================================================
# 연구 효과 종류
# =====================================================================
def test_an_unknown_effect_kind_is_rejected(db, version):
    """예전에는 이것이 그냥 통과했고, 실행 시점에 와일드카드만 소모하고
    아무 일도 하지 않았다."""
    db.execute(
        "UPDATE research_nodes SET effect_json = ? WHERE content_version_id = ? "
        "AND node_id = 'res_스탯강화'",
        (json.dumps({"kind": "party_sllot"}), version))  # 오타
    with pytest.raises(ValidationError, match="kind"):
        validation.validate_version(db, version)


def test_malformed_effect_json_is_rejected(db, version):
    db.execute(
        "UPDATE research_nodes SET effect_json = 'not json' "
        "WHERE content_version_id = ? AND node_id = 'res_스탯강화'", (version,))
    with pytest.raises(ValidationError, match="JSON"):
        validation.validate_version(db, version)


def test_a_party_slot_over_the_cap_is_rejected(db, version, balance):
    over = int(balance.get("max_party_slots")) + 1
    db.execute(
        "UPDATE research_nodes SET effect_json = ? WHERE content_version_id = ? "
        "AND node_id = 'res_파티슬롯3'",
        (json.dumps({"kind": "party_slot", "value": over}), version))
    with pytest.raises(ValidationError, match="max_party_slots"):
        validation.validate_version(db, version)


def test_a_party_slot_at_the_cap_is_accepted(db, version, balance):
    cap = int(balance.get("max_party_slots"))
    db.execute(
        "UPDATE research_nodes SET effect_json = ? WHERE content_version_id = ? "
        "AND node_id = 'res_파티슬롯3'",
        (json.dumps({"kind": "party_slot", "value": cap}), version))
    validation.validate_version(db, version)  # 터지지 않아야 한다


def test_the_shipped_research_nodes_all_use_known_kinds(db, version):
    for row in db.query(
        "SELECT node_id, effect_json FROM research_nodes WHERE content_version_id = ?",
        (version,)):
        kind = json.loads(row["effect_json"]).get("kind")
        assert kind in validation.KNOWN_RESEARCH_EFFECT_KINDS, \
            f"{row['node_id']} 의 kind {kind!r} 가 알려지지 않았습니다"


# =====================================================================
# 실행 시점 방어 — 검증을 뚫고 들어와도 조용히 넘어가지 않는다
# =====================================================================
def test_applying_an_unknown_kind_fails_loudly_instead_of_doing_nothing(db, user_id):
    """§10.5를 뚫고 들어온 콘텐츠라도, 재화만 받고 끝나는 대신 실패해야 한다 —
    `offer_reward` 가 비어 있던 것과 같은 실수를 되풀이하지 않기 위해서."""
    with pytest.raises(pg.ProgressionError, match="research effect kind"):
        pg._apply_research(db, {
            "user_id": user_id, "wildcards": 0, "step_id": "test",
            "effect_json": json.dumps({"kind": "존재하지_않음"}),
        })
