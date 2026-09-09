"""Design Addendum A-2 — Card Batch 1 pool shape (§10.5 gated check).

`batch1_shape_enforced` is off by default (`config/12_카드_배치1.toml`) — the
roster is 7 in-gacha-pool characters today, not the addendum's 12, and no
Batch 1 cards have been authored yet. These tests turn the flag on directly
against a synthetic card set, independent of when the real roster/authoring
catches up.
"""

from __future__ import annotations

import json

import pytest

from app.content import validation
from app.content.operators import ValidationError
from app.content.seed import CARD_BASIC_ATTACK, CARD_BASIC_DEFENSE, CARD_STARTER_SKILL

CARD_COLUMNS = ("content_version_id, card_id, name, element, cost, category, "
               "target_side, rarity_tier, effects_json, art_asset, is_retired")


def _enable(db, version):
    db.execute("UPDATE balancing_constants SET value_json = 'true' "
              "WHERE content_version_id = ? AND key = 'batch1_shape_enforced'",
              (version,))


def _clear_batch_cards(db, version):
    """Drop every seeded card except the three excluded from the batch.

    `reward_tables` references card_id, and `events` can reference
    `reward_tables` via `offer_reward` (both checked at §10.4/§10.5
    validation time) — neither is part of what this file tests, so both go
    too rather than leaving dangling references behind.
    """
    db.execute("DELETE FROM events WHERE content_version_id = ?", (version,))
    db.execute("DELETE FROM reward_tables WHERE content_version_id = ?", (version,))
    db.execute(
        "DELETE FROM cards WHERE content_version_id = ? AND card_id NOT IN (?, ?, ?)",
        (version, CARD_BASIC_ATTACK, CARD_BASIC_DEFENSE, CARD_STARTER_SKILL))


def _insert_card(db, version, card_id, *, element, cost, rarity_tier):
    effects = json.dumps([{"operator": "deal_damage", "params": {"multiplier": 1.0}}])
    db.execute(
        f"INSERT INTO cards ({CARD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
        (version, card_id, card_id, element, cost, "공격", "enemy", rarity_tier, effects))


def _char_elements(db, version) -> list[str]:
    rows = db.query(
        "SELECT element FROM characters WHERE content_version_id = ? "
        "AND in_gacha_pool = 1 AND is_retired = 0", (version,))
    return [row["element"] for row in rows]


def _seed_valid_batch(db, version):
    """Exactly the addendum's shape, against however many in-gacha-pool
    characters this content version actually has."""
    _clear_batch_cards(db, version)
    elements = _char_elements(db, version)
    char_count = len(elements)

    for i in range(char_count):
        element = elements[i]
        _insert_card(db, version, f"b1_char_{i}_a", element=element, cost=2, rarity_tier=1)
        _insert_card(db, version, f"b1_char_{i}_b", element=element, cost=2, rarity_tier=2)
        _insert_card(db, version, f"b1_char_{i}_c", element=element, cost=3, rarity_tier=4)

    for i in range(4):
        _insert_card(db, version, f"b1_uni_c1_{i}", element="무속성", cost=1, rarity_tier=1)
        _insert_card(db, version, f"b1_uni_c2_{i}", element="무속성", cost=2, rarity_tier=1)
        _insert_card(db, version, f"b1_uni_c3_{i}", element="무속성", cost=3, rarity_tier=2)
    # 8 기본(위 12장 중 8장이 tier 1로 이미 기본) + 4 중간 필요 — 재배치한다.
    db.execute("UPDATE cards SET rarity_tier = 4 WHERE content_version_id = ? "
              "AND card_id LIKE 'b1_uni_c3_%'", (version,))


def test_the_published_batch_is_enforced_and_valid(db, version, balance):
    assert bool(balance.get("batch1_shape_enforced")) is True
    validation.validate_version(db, version)


def test_a_correctly_shaped_batch_passes(db, version, balance):
    _enable(db, version)
    _seed_valid_batch(db, version)
    validation.validate_version(db, version)  # 터지지 않아야 한다


def test_too_few_character_exclusive_cards_is_rejected(db, version, balance):
    _enable(db, version)
    _seed_valid_batch(db, version)
    db.execute("DELETE FROM cards WHERE content_version_id = ? AND card_id = 'b1_char_0_a'",
              (version,))
    with pytest.raises(ValidationError, match="캐릭터 전용 카드"):
        validation.validate_version(db, version)


def test_a_character_exclusive_card_with_the_wrong_cost_is_rejected(db, version, balance):
    _enable(db, version)
    _seed_valid_batch(db, version)
    db.execute("UPDATE cards SET cost = 1 WHERE content_version_id = ? "
              "AND card_id = 'b1_char_0_a'", (version,))
    with pytest.raises(ValidationError, match="비용 2 또는 3"):
        validation.validate_version(db, version)


def test_a_character_exclusive_card_with_no_matching_character_is_rejected(
        db, version, balance):
    """카드가 붙은 원소를 쓰는 가챠풀 캐릭터가 하나도 없으면 (예: 그
    캐릭터를 은퇴시킨 뒤에도 카드는 남아 있는 경우) 걸려야 한다.

    화속성 캐릭터 둘을 모두 은퇴시키면 화 카드가 연결될 캐릭터가 사라진다.
    """
    _enable(db, version)
    _seed_valid_batch(db, version)
    db.execute("UPDATE characters SET is_retired = 1 WHERE content_version_id = ? "
              "AND character_id = 'char_ignis'", (version,))
    db.execute("UPDATE characters SET is_retired = 1 WHERE content_version_id = ? "
              "AND character_id = 'char_pyra'", (version,))
    db.execute("UPDATE cards SET element = '화' WHERE content_version_id = ? "
              "AND card_id = 'b1_char_0_a'", (version,))
    with pytest.raises(ValidationError, match="가챠풀 캐릭터가 없습니다"):
        validation.validate_version(db, version)


def test_wrong_universal_total_is_rejected(db, version, balance):
    _enable(db, version)
    _seed_valid_batch(db, version)
    db.execute("DELETE FROM cards WHERE content_version_id = ? AND card_id = 'b1_uni_c1_0'",
              (version,))
    with pytest.raises(ValidationError, match="무속성 카드"):
        validation.validate_version(db, version)


def test_wrong_universal_cost_split_is_rejected(db, version, balance):
    _enable(db, version)
    _seed_valid_batch(db, version)
    db.execute("UPDATE cards SET cost = 2 WHERE content_version_id = ? "
              "AND card_id = 'b1_uni_c1_0'", (version,))
    with pytest.raises(ValidationError, match="비용 1"):
        validation.validate_version(db, version)


def test_wrong_universal_rarity_split_is_rejected(db, version, balance):
    _enable(db, version)
    _seed_valid_batch(db, version)
    # cost는 그대로 두고 등급만 바꾼다 — 비용 분포 검사에 걸리지 않고
    # 등급 분포 검사만 걸리게 하려고.
    db.execute("UPDATE cards SET rarity_tier = 4 WHERE content_version_id = ? "
              "AND card_id = 'b1_uni_c2_0'", (version,))
    with pytest.raises(ValidationError, match="기본등급"):
        validation.validate_version(db, version)


def test_top_tier_cards_are_excluded_from_the_shape_entirely(db, version, balance):
    """최고등급(6티어)은 배치 1에서 빠진다 (A-2.4) — 아무리 많이 있어도 검사
    대상이 아니다."""
    _enable(db, version)
    _seed_valid_batch(db, version)
    _insert_card(db, version, "b1_extra_top", element="화", cost=3, rarity_tier=6)
    validation.validate_version(db, version)  # 터지지 않아야 한다


def test_a_universal_card_with_an_out_of_batch_cost_is_rejected(db, version, balance):
    """§15의 전역 card_cost_max(현재 3)가 굳이 안 막아 줘도, 배치 1 자체의
    규칙(무속성은 비용 1~3만, A-2.3)이 따로 걸려야 한다 — 그래서 여기서는
    전역 상한을 먼저 올려 그 검사를 피해 간다."""
    _enable(db, version)
    db.execute("UPDATE balancing_constants SET value_json = '5' "
              "WHERE content_version_id = ? AND key = 'card_cost_max'", (version,))
    _seed_valid_batch(db, version)
    db.execute("UPDATE cards SET cost = 4 WHERE content_version_id = ? "
              "AND card_id = 'b1_uni_c1_0'", (version,))
    with pytest.raises(ValidationError, match="비용 1~3"):
        validation.validate_version(db, version)
