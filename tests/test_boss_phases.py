"""§2.8.4 boss phase transitions — the case §2.5.3 uses to explain B-02.

> In the tutorial this was fully visible: starter 속도 95 ties the boss at 95,
> allies win ties, so the player crosses 50% → 무적 is created → the boss acts
> → the round ends → 무적 is gone. **The boss taught nothing.**
"""

from __future__ import annotations

import pytest

from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.engine import battle as bt
from app.engine import encounter as enc
from app.engine import lifecycle as lc
from app.engine import timed_effects as te
from app.engine import units as un
from app.engine.rng import JournaledRng


@pytest.fixture
def engine(db, balance, version, user_id) -> bt.BattleEngine:
    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version,
    )
    battle_id = enc.create_battle(db, balance, run_id=run_id, node_index=7,
                                  encounter_id="enc_tut_boss",
                                  content_version_id=version, is_boss=True)
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    built = bt.build_engine(db, balance, battle_id=battle_id, run_id=run_id,
                            content_version_id=version,
                            rng=JournaledRng(db, run_id, run["rng_seed"]))
    built.start()
    return built


def _boss(db, engine):
    return un.load_units(db, engine.battle_id, side=un.ENEMY)[0]


def test_the_tutorial_boss_matches_its_published_stat_block(db, engine, balance):
    """§15.9 — HP 100, 방어 4, 속도 95, recomputed in v6.2."""
    boss = _boss(db, engine)
    assert boss.hp_max == 100
    assert boss.base_def == 4
    assert boss.base_spd == 95
    assert boss.boss_phase == 1


def test_the_starter_ties_the_boss_on_speed_and_allies_win_ties(db, engine):
    """§2.1 tie-break: allies before enemies on equal speed."""
    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    boss = _boss(db, engine)
    assert ally.base_spd == boss.base_spd == 95

    order = db.query(
        "SELECT battle_unit_id FROM battle_round_order WHERE battle_id = ? "
        "AND round_no = 1 ORDER BY order_index", (engine.battle_id,))
    assert order[0]["battle_unit_id"] == ally.battle_unit_id


def test_crossing_fifty_percent_fires_invulnerable_and_it_covers_the_next_round(
        db, engine):
    """The whole point of B-02: a 무적 created in round N survives round N's
    boundary and covers the whole of round N+1."""
    boss = _boss(db, engine)
    # Drop the boss to 45% — below the phase-2 threshold, still alive.
    db.execute("UPDATE battle_units SET hp_current = 45 WHERE battle_unit_id = ?",
               (boss.battle_unit_id,))

    engine._evaluate_boss_phases(round_no=1)

    assert un.load_unit(db, boss.battle_unit_id).boss_phase == 2
    assert te.is_invulnerable(db, boss.battle_unit_id, 1)

    # Round 1 ends → still invulnerable through round 2.
    te.expire_round(db, engine.battle_id, 1)
    assert te.is_invulnerable(db, boss.battle_unit_id, 2)

    # Round 2 ends → gone.
    te.expire_round(db, engine.battle_id, 2)
    assert not te.is_invulnerable(db, boss.battle_unit_id, 3)


def test_invulnerable_actually_nullifies_damage(db, engine):
    boss = _boss(db, engine)
    db.execute("UPDATE battle_units SET hp_current = 45 WHERE battle_unit_id = ?",
               (boss.battle_unit_id,))
    engine._evaluate_boss_phases(round_no=1)

    engine.begin_turn()
    unit = engine.acting_unit()
    attack = next(entry for entry in engine.playable_cards(unit)
                  if entry["card"]["category"] == "공격")
    engine.play_card(unit, attack["card_instance_id"], [boss.battle_unit_id])

    assert un.load_unit(db, boss.battle_unit_id).hp_current == 45


def test_a_boss_killed_outright_fires_no_transition(db, engine):
    """Threshold policy step 2: if the boss is now dead → it dies. NO
    transition effect fires."""
    boss = _boss(db, engine)
    db.execute("UPDATE battle_units SET hp_current = 0, is_alive = 0 "
               "WHERE battle_unit_id = ?", (boss.battle_unit_id,))

    engine._evaluate_boss_phases(round_no=1)

    assert un.load_unit(db, boss.battle_unit_id).boss_phase == 1
    assert te.active_for_unit(db, boss.battle_unit_id) == []


def test_a_phase_transition_discards_only_that_unit_s_plan(db, engine, balance,
                                                           version):
    """Threshold policy step 6 — other enemies keep their committed plans."""
    boss = _boss(db, engine)
    other = enc.summon_enemy(db, engine.battle_id, "enemy_tut_박쥐", version, balance)
    engine._build_plans(1)
    assert db.one("SELECT 1 FROM enemy_plans WHERE battle_id = ? AND enemy_unit_id = ?",
                  (engine.battle_id, other)) is not None

    db.execute("UPDATE battle_units SET hp_current = 45 WHERE battle_unit_id = ?",
               (boss.battle_unit_id,))
    engine._evaluate_boss_phases(round_no=1)

    assert db.one("SELECT 1 FROM enemy_plans WHERE battle_id = ? AND enemy_unit_id = ?",
                  (engine.battle_id, boss.battle_unit_id)) is None
    assert db.one("SELECT 1 FROM enemy_plans WHERE battle_id = ? AND enemy_unit_id = ?",
                  (engine.battle_id, other)) is not None


def test_a_phase_is_entered_exactly_once(db, engine):
    boss = _boss(db, engine)
    db.execute("UPDATE battle_units SET hp_current = 45 WHERE battle_unit_id = ?",
               (boss.battle_unit_id,))

    engine._evaluate_boss_phases(round_no=1)
    engine._evaluate_boss_phases(round_no=1)
    engine._evaluate_boss_phases(round_no=1)

    effects = te.active_for_unit(db, boss.battle_unit_id)
    assert len(effects) == 1


def test_the_boss_uses_its_phase_gated_action_only_in_phase_two(db, engine):
    """§2.8.5 — `min_phase` / `max_phase` replace or extend the active rule
    list on a phase change."""
    boss = _boss(db, engine)
    from app.engine import enemy_ai as ai

    action = ai.select_action(db, engine.action_registry, actor=boss,
                              battle_id=engine.battle_id, round_no=1,
                              rng=engine.rng, rng_key="k1")
    assert action.action_id == "act_기본공격"      # phase 1 — gated rule filtered out

    db.execute("UPDATE battle_units SET hp_current = 45, boss_phase = 2 "
               "WHERE battle_unit_id = ?", (boss.battle_unit_id,))
    boss = _boss(db, engine)
    action = ai.select_action(db, engine.action_registry, actor=boss,
                              battle_id=engine.battle_id, round_no=1,
                              rng=engine.rng, rng_key="k2")
    assert action.action_id == "act_boss_기절"
