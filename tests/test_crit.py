"""§10.4.3 crit_chance / crit_multiplier — journaled at resolution.

On success the damage computation uses crit_multiplier IN PLACE OF (not
stacked with) multiplier/amount's normal scaling.
"""

from __future__ import annotations

import pytest

from app.content.operators import ValidationError, validate_effect_list, CTX_BATTLE_CARD
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.engine import battle as bt
from app.engine import effects as fx
from app.engine import encounter as enc
from app.engine import lifecycle as lc
from app.engine import units as un
from app.engine.rng import JournaledRng


@pytest.fixture
def run_id(db, balance, version, user_id) -> int:
    return lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version,
    )


@pytest.fixture
def engine(db, balance, version, run_id) -> bt.BattleEngine:
    battle_id = enc.create_battle(db, balance, run_id=run_id, node_index=0,
                                  encounter_id="enc_tut_2",
                                  content_version_id=version)
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    built = bt.build_engine(db, balance, battle_id=battle_id, run_id=run_id,
                            content_version_id=version,
                            rng=JournaledRng(db, run_id, run["rng_seed"]))
    built.start()
    return built


def _ally(db, engine) -> un.Unit:
    return un.load_units(db, engine.battle_id, side=un.ALLY)[0]


def _enemy(db, engine) -> un.Unit:
    return un.load_units(db, engine.battle_id, side=un.ENEMY)[0]


# =====================================================================
# seed content
# =====================================================================
def test_seed_content_uses_crit_on_both_sides(db, version):
    card = db.one("SELECT effects_json FROM cards WHERE content_version_id = ? "
                  "AND card_id = 'card_풍_질풍'", (version,))
    assert '"crit_chance"' in card["effects_json"]
    action = db.one("SELECT effects_json FROM enemy_actions WHERE "
                    "content_version_id = ? AND action_id = 'act_강타'", (version,))
    assert '"crit_chance"' in action["effects_json"]


# =====================================================================
# engine — deal_damage
# =====================================================================
def test_a_forced_crit_replaces_the_multiplier_not_stacked(db, engine):
    ally = _ally(db, engine)
    enemy = _enemy(db, engine)

    baseline_ctx = engine._context(ally, [enemy], CTX_BATTLE_CARD)
    baseline = fx.execute_effects(
        [{"operator": "deal_damage", "params": {"multiplier": 1.0}}], baseline_ctx)
    baseline_damage = baseline.damage_events[0]["final_damage"]

    # Reset the enemy's HP so the second hit starts from the same baseline.
    db.execute("UPDATE battle_units SET hp_current = hp_max WHERE battle_unit_id = ?",
              (enemy.battle_unit_id,))

    crit_ctx = engine._context(ally, [enemy], CTX_BATTLE_CARD)
    crit_outcome = fx.execute_effects(
        [{"operator": "deal_damage",
          "params": {"multiplier": 1.0, "crit_chance": 1.0, "crit_multiplier": 3.0}}],
        crit_ctx)
    crit_damage = crit_outcome.damage_events[0]["final_damage"]

    db.execute("UPDATE battle_units SET hp_current = hp_max WHERE battle_unit_id = ?",
              (enemy.battle_unit_id,))
    tripled_ctx = engine._context(ally, [enemy], CTX_BATTLE_CARD)
    tripled = fx.execute_effects(
        [{"operator": "deal_damage", "params": {"multiplier": 3.0}}], tripled_ctx)
    tripled_damage = tripled.damage_events[0]["final_damage"]

    assert crit_damage == tripled_damage
    assert crit_damage > baseline_damage
    assert any("치명타" in line for line in crit_outcome.log)


def test_crit_chance_zero_never_rolls_or_crits(db, engine):
    ally = _ally(db, engine)
    enemy = _enemy(db, engine)

    ctx = engine._context(ally, [enemy], CTX_BATTLE_CARD)
    outcome = fx.execute_effects(
        [{"operator": "deal_damage",
          "params": {"multiplier": 1.0, "crit_multiplier": 5.0}}], ctx)

    assert not any("치명타" in line for line in outcome.log)
    rows = db.query("SELECT op_key FROM rng_operations WHERE op_key LIKE ?",
                    (f"battle:{engine.battle_id}:%crit%",))
    assert rows == []


def test_the_crit_roll_is_journaled(db, engine):
    ally = _ally(db, engine)
    enemy = _enemy(db, engine)

    ctx = engine._context(ally, [enemy], CTX_BATTLE_CARD)
    fx.execute_effects(
        [{"operator": "deal_damage",
          "params": {"multiplier": 1.0, "crit_chance": 1.0, "crit_multiplier": 2.0}}],
        ctx)

    rows = db.query("SELECT op_key FROM rng_operations WHERE op_key LIKE ?",
                    (f"battle:{engine.battle_id}:%crit%",))
    assert len(rows) == 1


# =====================================================================
# engine — deal_flat_damage
# =====================================================================
def test_a_forced_crit_scales_the_flat_amount(db, engine):
    ally = _ally(db, engine)
    enemy = _enemy(db, engine)
    db.execute("UPDATE battle_units SET block = 0 WHERE battle_unit_id = ?",
              (enemy.battle_unit_id,))

    ctx = engine._context(ally, [enemy], CTX_BATTLE_CARD)
    outcome = fx.execute_effects(
        [{"operator": "deal_flat_damage",
          "params": {"amount": 10, "crit_chance": 1.0, "crit_multiplier": 2.0}}], ctx)

    assert outcome.damage_events[0]["final_damage"] == 20


# =====================================================================
# §10.5 validation
# =====================================================================
def test_validation_rejects_a_crit_chance_out_of_range():
    with pytest.raises(ValidationError, match="crit_chance"):
        validate_effect_list(
            [{"operator": "deal_damage",
              "params": {"multiplier": 1.0, "crit_chance": 1.5, "crit_multiplier": 2.0}}],
            CTX_BATTLE_CARD)


def test_validation_rejects_a_non_positive_crit_multiplier():
    with pytest.raises(ValidationError, match="crit_multiplier"):
        validate_effect_list(
            [{"operator": "deal_flat_damage",
              "params": {"amount": 5, "crit_chance": 0.5, "crit_multiplier": 0}}],
            CTX_BATTLE_CARD)


def test_validation_accepts_deal_damage_without_any_crit_params():
    # Overwhelmingly the common case — crit params are fully optional.
    validate_effect_list(
        [{"operator": "deal_damage", "params": {"multiplier": 1.0}}], CTX_BATTLE_CARD)
