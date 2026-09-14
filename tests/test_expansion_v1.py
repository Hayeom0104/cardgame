from app.content.expansion_v1 import event_rows, passive_rows, skill_cards


def test_expansion_counts_and_unique_ids():
    skills = skill_cards(1)
    passives = passive_rows()
    events = event_rows()

    assert len(skills) == 60
    assert len(passives) == 30
    assert len(events) == 4

    skill_ids = [row[0] for row in skills]
    passive_ids = [row[0] for row in passives]
    event_ids = [row[0] for row in events]
    assert len(skill_ids) == len(set(skill_ids))
    assert len(passive_ids) == len(set(passive_ids))
    assert len(event_ids) == len(set(event_ids))


def test_skill_distribution_and_cost_floor():
    skills = skill_cards(1)
    by_element = {}
    by_tier = {}
    for _, _, element, cost, _, _, tier, effects in skills:
        by_element[element] = by_element.get(element, 0) + 1
        by_tier[tier] = by_tier.get(tier, 0) + 1
        assert 1 <= cost <= 3
        assert 1 <= tier <= 6
        assert effects

    assert by_element == {
        "화": 8, "수": 8, "풍": 8, "지": 8, "광": 8, "암": 8, "무속성": 12,
    }
    assert by_tier == {1: 8, 2: 14, 3: 14, 4: 8, 5: 8, 6: 8}


def test_passives_have_five_per_tier_and_valid_trigger_shape():
    rows = passive_rows()
    counts = {tier: 0 for tier in range(1, 7)}
    for _, _, _, tier, trigger, params, scope, once, effects in rows:
        counts[tier] += 1
        assert trigger in {"battle_start", "round_start"}
        assert scope in {"party", "enemies"}
        assert once in {0, 1}
        assert set(params).issubset({"hp_below"})
        assert effects
    assert counts == {1: 5, 2: 5, 3: 5, 4: 5, 5: 5, 6: 5}


def test_event_shapes():
    for event_id, name, kind, combat_link, branches in event_rows():
        assert event_id.startswith("event_x1_")
        assert name
        assert kind in {"choice", "instant"}
        assert combat_link in {"none", "fixed", "conditional"}
        assert len(branches) >= 1
        for branch in branches:
            assert branch["label"]
            assert "effects" in branch


def test_published_expansion_matches_authored_costs(db, version):
    """발행 시 암묵적인 보정 없이 원본과 저장 비용이 일치한다."""
    for card_id, _, _, cost, *_ in skill_cards():
        row = db.one(
            "SELECT cost FROM cards WHERE content_version_id = ? AND card_id = ?",
            (version, card_id),
        )
        assert row is not None
        assert row["cost"] == cost


def test_invalid_expansion_cost_is_rejected_at_publish(db, monkeypatch):
    import pytest
    from app.content import expansion_v1
    from app.content.operators import ValidationError
    from app.content.seed import seed_all

    authored = expansion_v1.skill_cards

    def invalid_cards(card_cost_min=1):
        cards = authored(card_cost_min)
        row = list(cards[0])
        row[3] = 4
        cards[0] = tuple(row)
        return cards

    monkeypatch.setattr(expansion_v1, "skill_cards", invalid_cards)
    with pytest.raises(ValidationError, match="허용 범위"):
        seed_all(db)
