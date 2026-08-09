"""Combat rules from §2 — the ones the design doc calls out as previously broken."""

from __future__ import annotations

import pytest

from app.engine import enemy_ai as ai
from app.engine import statuses as st
from app.engine import stats
from app.engine import timed_effects as te
from app.engine import units as un


# =====================================================================
# §15.1 damage formula and stat pipeline
# =====================================================================
def test_damage_floors_once_at_the_end(balance):
    """`floor` is applied ONCE, to the final value, before the max(1, …) clamp."""
    result = stats.compute_damage(
        balance, attacker_atk=12, card_multiplier=1.8, target_def=5,
    )
    # floor(12 × 1.8 − 5) = floor(16.6) = 16 — §15.1's tempo validation figure.
    assert result.final_damage == 16


def test_tutorial_skill_damage_is_14_not_15(balance):
    """§15.9: `floor(10 × 1.8 − 4) = 14`. v6.1 wrote 15; arithmetic error."""
    result = stats.compute_damage(
        balance, attacker_atk=10, card_multiplier=1.8, target_def=4)
    assert result.final_damage == 14
    basic = stats.compute_damage(
        balance, attacker_atk=10, card_multiplier=1.0, target_def=4)
    assert basic.final_damage == 6


def test_minimum_damage_floor_is_one(balance):
    result = stats.compute_damage(
        balance, attacker_atk=3, card_multiplier=1.0, target_def=99)
    assert result.final_damage == 1


def test_element_affinity_is_a_single_cycle(balance):
    # 화 → 수 → 풍 → 지 → 광 → 암 → 화; strong against the next, weak against
    # the previous.
    assert stats.element_affinity(balance, "화", "수") == 1.5
    assert stats.element_affinity(balance, "수", "화") == 0.75
    assert stats.element_affinity(balance, "암", "화") == 1.5
    assert stats.element_affinity(balance, "화", "암") == 0.75
    assert stats.element_affinity(balance, "화", "지") == 1.0
    # 무속성 neither deals nor receives affinity modifiers.
    assert stats.element_affinity(balance, "무속성", "화") == 1.0
    assert stats.element_affinity(balance, "화", "무속성") == 1.0


def test_speed_is_never_multiplied(balance):
    """§15.1 — 속도 changes only via 속도 감소 and flat equipment bonuses."""
    base = balance.get("base_stats_by_role")["공격형"]["spd"]
    at_1_star = stats.effective_stat(balance, "spd", base, star_rank=1,
                                     research_step=0, equipment_flat=0)
    at_3_star = stats.effective_stat(balance, "spd", base, star_rank=3,
                                     research_step=10, equipment_flat=0)
    assert at_1_star == at_3_star == base
    # Only the flat equipment term moves it.
    assert stats.effective_stat(balance, "spd", base, star_rank=3,
                                research_step=10, equipment_flat=7) == base + 7


def test_research_does_not_apply_to_speed_but_does_to_attack(balance):
    """+3% per step, 10 steps → +30%, on HP / 공격 / 방어 only."""
    assert stats.effective_stat(balance, "atk", 100, star_rank=1,
                                research_step=10, equipment_flat=0) == 130


def test_block_reduces_hp_loss_without_reducing_the_hit(balance):
    result = stats.compute_damage(
        balance, attacker_atk=20, card_multiplier=1.0, target_def=0, target_block=8)
    assert result.final_damage == 20
    assert result.hp_loss == 12
    assert result.block_consumed == 8


def test_shield_pierce_ignores_block_but_not_defense(balance):
    """보호막 관통: incoming damage ignores block only; 방어력 still subtracted."""
    result = stats.compute_damage(
        balance, attacker_atk=20, card_multiplier=1.0, target_def=5,
        target_block=8, ignores_block=True)
    assert result.final_damage == 15      # defense still applied
    assert result.hp_loss == 15           # block ignored


# =====================================================================
# §2.5.1 status models and clocks
# =====================================================================
def _unit(db, battle_id=1, side="ally", spd=100):
    cursor = db.execute(
        "INSERT INTO battle_units (battle_id, side, registration_order, visible_slot, "
        "unit_def_id, hp_current, hp_max, atk, def, spd, element, role) "
        "VALUES (?, ?, ?, ?, 'x', 100, 100, 10, 5, ?, '무속성', '공격형')",
        (battle_id, side,
         un.next_registration_order(db, battle_id, side),
         un.next_visible_slot(db, battle_id, side), spd),
    )
    return int(cursor.lastrowid)


def test_stack_decay_has_no_duration_and_loses_one_stack_per_trigger(db, version):
    """화상/출혈: the stack count IS the lifetime (C-04)."""
    registry = st.StatusRegistry(db, version)
    unit_id = _unit(db)
    st.apply_status(db, registry, unit_id, st.BURN, stacks=3)

    row = db.one("SELECT * FROM battle_unit_statuses WHERE battle_unit_id = ?",
                 (unit_id,))
    assert row["stacks"] == 3
    assert row["duration_remaining"] is None

    triggered = st.tick_turn_start_triggers(db, registry, unit_id)
    assert triggered == [(st.BURN, 9)]        # 3 flat damage per stack
    assert st.stacks_of(db, unit_id, st.BURN) == 2


def test_stack_duration_reapplication_adds_stacks_and_refreshes(db, version):
    """Q23 = A: re-application adds stacks AND refreshes duration to maximum."""
    registry = st.StatusRegistry(db, version)
    unit_id = _unit(db)
    st.apply_status(db, registry, unit_id, st.ATTACK_UP, stacks=2)
    st.tick_owner_turn_countdown(db, registry, unit_id)     # 3 → 2
    st.apply_status(db, registry, unit_id, st.ATTACK_UP, stacks=1)

    row = db.one("SELECT * FROM battle_unit_statuses WHERE battle_unit_id = ?",
                 (unit_id,))
    assert row["stacks"] == 3
    assert row["duration_remaining"] == 3     # refreshed to max, not 2


def test_stack_cap_is_enforced(db, version):
    registry = st.StatusRegistry(db, version)
    unit_id = _unit(db)
    st.apply_status(db, registry, unit_id, st.DEFENSE_DOWN, stacks=99)
    assert st.stacks_of(db, unit_id, st.DEFENSE_DOWN) == 5     # max 5 (+40%)


def test_one_turn_countdown_acts_exactly_once(db, version):
    """The critical property: evaluated BEFORE it is decremented."""
    registry = st.StatusRegistry(db, version)
    unit_id = _unit(db)
    st.apply_status(db, registry, unit_id, st.STUN)     # base_duration 1

    assert st.has_status(db, unit_id, st.STUN)          # gates this turn (PHASE C)
    st.tick_owner_turn_countdown(db, registry, unit_id)  # decremented at turn END
    assert not st.has_status(db, unit_id, st.STUN)      # gone for the next turn


def test_stack_decay_is_untouched_by_the_countdown_clock(db, version):
    """§2.11 step 6 — they already decayed at step 3."""
    registry = st.StatusRegistry(db, version)
    unit_id = _unit(db)
    st.apply_status(db, registry, unit_id, st.BURN, stacks=2)
    st.tick_owner_turn_countdown(db, registry, unit_id)
    assert st.stacks_of(db, unit_id, st.BURN) == 2


# =====================================================================
# §2.5.3 timed effects — the B-02 fix
# =====================================================================
def test_one_round_invulnerable_survives_its_own_round_boundary(db):
    """The v6.2 bug: 무적 created in round N expired at N's boundary, so the
    boss taught nothing. `expires_after_round = N + D` fixes it."""
    unit_id = _unit(db)
    te.create_invulnerable(db, 1, unit_id, current_round=5, duration_rounds=1)

    # Created in round 5 → survives round 5's boundary.
    te.expire_round(db, 1, 5)
    assert te.is_invulnerable(db, unit_id, 6)

    # And covers the whole of round 6, expiring at round 6's boundary.
    te.expire_round(db, 1, 6)
    assert not te.is_invulnerable(db, unit_id, 7)


def test_stat_modifier_expiry_subtracts_exactly_what_was_added(db):
    unit_id = _unit(db)
    te.create_stat_modifier(db, 1, unit_id, stat="atk", delta=30, is_percent=True,
                            current_round=1, duration_rounds=3)
    te.create_stat_modifier(db, 1, unit_id, stat="spd", delta=15, is_percent=False,
                            current_round=1, duration_rounds=3)

    # 각성: 공격 +30% and 속도 +15 flat, both as timed effects, not statuses.
    assert te.stat_delta(db, unit_id, "atk", 100.0) == pytest.approx(30.0)
    assert te.stat_delta(db, unit_id, "spd", 100.0) == pytest.approx(15.0)

    te.expire_round(db, 1, 4)
    assert te.stat_delta(db, unit_id, "atk", 100.0) == 0.0
    assert te.stat_delta(db, unit_id, "spd", 100.0) == 0.0


def test_overlapping_percent_modifiers_scale_base_not_each_other(db):
    unit_id = _unit(db)
    for _ in range(2):
        te.create_stat_modifier(db, 1, unit_id, stat="atk", delta=30,
                                is_percent=True, current_round=1, duration_rounds=3)
    assert te.stat_delta(db, unit_id, "atk", 100.0) == pytest.approx(60.0)


# =====================================================================
# §2.8.5 cooldowns — the C-02 fix
# =====================================================================
def test_cooldown_one_skips_exactly_one_turn(db):
    """v6.2 wrote `current_round + cooldown_turns`, making cooldown 1
    indistinguishable from no cooldown for a once-per-round actor."""
    ai.start_cooldown(db, battle_id=1, enemy_unit_id=7, action_id="a",
                      current_round=5, cooldown_turns=1)
    row = db.one("SELECT available_from_round FROM enemy_cooldowns "
                 "WHERE battle_id = 1 AND enemy_unit_id = 7", ())
    assert row["available_from_round"] == 7      # 5 + 1 + 1

    assert ai.is_on_cooldown(db, 1, 7, "a", 6)   # 6 >= 7 false → not eligible
    assert not ai.is_on_cooldown(db, 1, 7, "a", 7)


def test_cooldown_zero_means_no_cooldown(db):
    ai.start_cooldown(db, battle_id=1, enemy_unit_id=7, action_id="a",
                      current_round=5, cooldown_turns=0)
    assert not ai.is_on_cooldown(db, 1, 7, "a", 5)
