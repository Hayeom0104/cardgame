"""§10.4 operator categories / host contexts and §10.5 content validation."""

from __future__ import annotations

import json

import pytest

from app.content import operators as ops
from app.content import validation
from app.content.operators import ValidationError
from app.content.versioning import (copy_version, create_version, current_version_id,
                                    fingerprint, publish)


# =====================================================================
# §10.4.1 — category is computed from (operator, params)
# =====================================================================
def test_category_depends_on_parameters_not_the_operator_name():
    assert ops.categorize("discard_cards", {"count": 1, "selector": "random"}) \
        == ops.PURE_SYNCHRONOUS
    assert ops.categorize("discard_cards", {"count": 1, "selector": "choose"}) \
        == ops.PENDING_CHOICE


def test_grant_currency_is_central_only_for_coin():
    """§10.4.3 — CENTRAL when currency = 코인."""
    assert ops.categorize("grant_currency", {"currency": "coin", "amount": 1}) \
        == ops.CENTRAL_TRANSACTION
    assert ops.categorize("grant_currency", {"currency": "carta", "amount": 1}) \
        == ops.PURE_SYNCHRONOUS


def test_start_combat_is_a_terminal_state_transition():
    assert ops.categorize("start_combat", {"encounter_id": "x"}) \
        == ops.TERMINAL_STATE_TRANSITION


# =====================================================================
# §10.4.1a — host-context permissions, the B-15 fix
# =====================================================================
def test_a_battle_card_cannot_grant_permanent_progression():
    """The B-15 hole: a dashboard editor could author a valid battle card that
    granted 카르타 or fragments every time it was played."""
    effects = [{"operator": "grant_currency",
                "params": {"currency": "carta", "amount": 500}}]
    with pytest.raises(ValidationError, match="PROGRESSION"):
        ops.validate_effect_list(effects, ops.CTX_BATTLE_CARD)


def test_progression_operators_are_legal_in_event_context():
    effects = [{"operator": "grant_character_fragments",
                "params": {"character_id": "char_ignis", "amount": 20}}]
    ops.validate_effect_list(effects, ops.CTX_EVENT)


def test_run_currency_grants_are_still_progression_tagged():
    """탐험 자금 is destroyed at run end, but run economy belongs to nodes and
    events, not to repeatable battle cards."""
    effects = [{"operator": "grant_currency",
                "params": {"currency": "run_currency", "amount": 40}}]
    with pytest.raises(ValidationError):
        ops.validate_effect_list(effects, ops.CTX_BATTLE_CARD)
    ops.validate_effect_list(effects, ops.CTX_SHOP)


def test_a_passive_may_not_suspend_a_battle_turn():
    """§6 — a passive cannot suspend a battle turn for a choice."""
    effects = [{"operator": "discard_cards",
                "params": {"count": 1, "selector": "choose"}}]
    with pytest.raises(ValidationError, match="PENDING_CHOICE"):
        ops.validate_effect_list(effects, ops.CTX_PASSIVE)


def test_terminal_transition_must_be_last():
    effects = [
        {"operator": "start_combat", "params": {"encounter_id": "x"}},
        {"operator": "grant_currency", "params": {"currency": "carta", "amount": 1}},
    ]
    with pytest.raises(ValidationError, match="must be the last operator"):
        ops.validate_effect_list(effects, ops.CTX_EVENT)


def test_unknown_operators_and_bad_parameters_are_rejected():
    with pytest.raises(ValidationError, match="unknown operator"):
        ops.validate_effect_list([{"operator": "mind_control", "params": {}}],
                                 ops.CTX_EVENT)
    with pytest.raises(ValidationError, match="missing parameter"):
        ops.validate_effect_list([{"operator": "deal_damage", "params": {}}],
                                 ops.CTX_BATTLE_CARD)
    with pytest.raises(ValidationError, match="not in"):
        ops.validate_effect_list(
            [{"operator": "grant_block", "params": {"mode": "sideways", "value": 1}}],
            ops.CTX_BATTLE_CARD)


# =====================================================================
# §10.5 content validation
# =====================================================================
def test_seeded_content_passes_validation(db, version):
    validation.validate_version(db, version)


def test_a_status_declaring_no_duration_under_countdown_is_rejected(db, version):
    db.execute(
        "UPDATE statuses SET base_duration = NULL WHERE content_version_id = ? "
        "AND status_id = '기절'", (version,))
    with pytest.raises(ValidationError, match="requires base_duration"):
        validation.validate_version(db, version)


def test_stack_decay_may_not_declare_a_duration(db, version):
    """C-04 — v6.2 required base_duration on every status while 화상/출혈 have
    no duration at all."""
    db.execute(
        "UPDATE statuses SET base_duration = 3 WHERE content_version_id = ? "
        "AND status_id = '화상'", (version,))
    with pytest.raises(ValidationError, match="must not declare base_duration"):
        validation.validate_version(db, version)


def test_a_partially_populated_threat_grid_is_rejected(db, version):
    db.execute(
        "DELETE FROM threat_weights WHERE content_version_id = ? "
        "AND enemy_role_id = '공격형' AND ally_role_id = '서포터형'", (version,))
    with pytest.raises(ValidationError, match="partially populated"):
        validation.validate_version(db, version)


def test_an_enemy_without_a_basic_attack_is_rejected(db, version):
    rules = json.dumps([
        {"priority": 10, "condition": None, "action_id": "act_강타", "weight": 1.0,
         "cooldown_turns": 1},
    ])
    db.execute(
        "UPDATE enemies SET action_rules_json = ? WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_w1_고블린'", (rules, version))
    with pytest.raises(ValidationError, match="기본 공격"):
        validation.validate_version(db, version)


def test_a_zero_cooldown_summon_is_rejected_outright(db, version):
    """C-09 — unbounded by construction."""
    db.execute(
        "INSERT INTO enemy_actions (content_version_id, action_id, name, category, "
        "target_side, is_basic_attack, effects_json) VALUES (?, 'act_소환', '소환', "
        "'버프디버프', 'self', 0, ?)",
        (version, json.dumps([{"operator": "summon_enemy",
                               "params": {"enemy_id": "enemy_tut_박쥐", "count": 1}}])))
    rules = json.dumps([
        {"priority": 10, "condition": None, "action_id": "act_기본공격",
         "weight": 1.0, "cooldown_turns": 0},
        {"priority": 5, "condition": None, "action_id": "act_소환", "weight": 1.0,
         "cooldown_turns": 0},
    ])
    db.execute("UPDATE enemies SET action_rules_json = ? WHERE content_version_id = ? "
               "AND enemy_id = 'enemy_w1_고블린'", (rules, version))
    with pytest.raises(ValidationError, match="unbounded by construction"):
        validation.validate_version(db, version)


def test_the_summon_bound_is_conservative_and_static(db, version):
    """`initial + Σ count × ceil(ROUND_BUDGET / (cooldown+1)) <= 8`.

    Deliberately pessimistic: it may reject an encounter that would in practice
    stay under 8, which is the correct direction.
    """
    db.execute(
        "INSERT INTO enemy_actions (content_version_id, action_id, name, category, "
        "target_side, is_basic_attack, effects_json) VALUES (?, 'act_소환', '소환', "
        "'버프디버프', 'self', 0, ?)",
        (version, json.dumps([{"operator": "summon_enemy",
                               "params": {"enemy_id": "enemy_tut_박쥐", "count": 2}}])))
    rules = json.dumps([
        {"priority": 10, "condition": None, "action_id": "act_기본공격",
         "weight": 1.0, "cooldown_turns": 0},
        {"priority": 5, "condition": None, "action_id": "act_소환", "weight": 1.0,
         "cooldown_turns": 3},
    ])
    db.execute("UPDATE enemies SET action_rules_json = ? WHERE content_version_id = ? "
               "AND enemy_id = 'enemy_w1_고블린'", (rules, version))
    # 3 authored units + 2 × ceil(12 / 4) = 3 + 6 = 9 > 8
    with pytest.raises(ValidationError, match="summon bound"):
        validation.validate_version(db, version)


def test_boss_phase_thresholds_must_descend(db, version):
    db.execute(
        "INSERT INTO boss_phases (content_version_id, boss_phase_id, enemy_id, "
        "phase_index, hp_threshold_pct, effect_ids_json) "
        "VALUES (?, 'phase_bad', 'enemy_tut_boss', 3, 0.9, ?)",
        (version, json.dumps(["trans_무적"])))
    with pytest.raises(ValidationError, match="thresholds must descend"):
        validation.validate_version(db, version)


def test_research_referencing_a_missing_achievement_is_rejected(db, version):
    db.execute(
        "UPDATE research_nodes SET required_achievement = 'ach_없음' "
        "WHERE content_version_id = ? AND node_id = 'res_파티슬롯3'", (version,))
    with pytest.raises(ValidationError, match="does not resolve"):
        validation.validate_version(db, version)


# =====================================================================
# §10.6 content versioning
# =====================================================================
def test_publishing_copies_a_complete_immutable_snapshot(db, version):
    next_version = create_version(db)
    copy_version(db, version, next_version)

    for table in ("cards", "statuses", "enemies", "balancing_constants"):
        old = db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE content_version_id = ?",
                     (version,))["n"]
        new = db.one(f"SELECT COUNT(*) AS n FROM {table} WHERE content_version_id = ?",
                     (next_version,))["n"]
        assert old == new and old > 0

    assert fingerprint(db, version) == fingerprint(db, next_version)


def test_editing_the_new_version_leaves_the_old_one_untouched(db, version):
    """A run pinned to an older version keeps reading exactly the rows it
    started with."""
    next_version = create_version(db)
    copy_version(db, version, next_version)
    db.execute("UPDATE cards SET cost = 99 WHERE content_version_id = ? "
               "AND card_id = 'card_평타'", (next_version,))

    old_cost = db.one("SELECT cost FROM cards WHERE content_version_id = ? "
                      "AND card_id = 'card_평타'", (version,))["cost"]
    assert old_cost == 1
    assert fingerprint(db, version) != fingerprint(db, next_version)


def test_publishing_switches_the_current_version(db, version):
    next_version = create_version(db)
    copy_version(db, version, next_version)
    publish(db, next_version)
    assert current_version_id(db) == next_version
    # Exactly one version is current.
    assert db.one("SELECT COUNT(*) AS n FROM content_versions WHERE is_current = 1"
                  )["n"] == 1


def test_publishing_invalid_content_fails_closed(db, version):
    next_version = create_version(db)
    copy_version(db, version, next_version)
    db.execute("DELETE FROM threat_weights WHERE content_version_id = ? "
               "AND ally_role_id = '공격형'", (next_version,))
    with pytest.raises(ValidationError):
        publish(db, next_version)
    # The old version is still current — content is never partially loaded.
    assert current_version_id(db) == version
