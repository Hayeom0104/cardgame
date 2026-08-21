"""§2.13 reactive abilities (반응형 능력 / 반격) — unit-agnostic counters.

Hooked into `effects._land`, immediately after each `deal_damage` /
`deal_flat_damage` operator lands against one target (§2.11 P8/E3).
"""

from __future__ import annotations

import json

import pytest

from app.content import operators as ops
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.content.validation import ValidationError, validate_version
from app.engine import battle as bt
from app.engine import effects as fx
from app.engine import encounter as enc
from app.engine import lifecycle as lc
from app.engine import reactive as ra
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


def _set_unit_def(db, unit: un.Unit, unit_def_id: str, *, hp_current: int,
                  hp_max: int) -> un.Unit:
    db.execute(
        "UPDATE battle_units SET unit_def_id = ?, hp_current = ?, hp_max = ? "
        "WHERE battle_unit_id = ?",
        (unit_def_id, hp_current, hp_max, unit.battle_unit_id),
    )
    return un.load_unit(db, unit.battle_unit_id)


# =====================================================================
# seed content — both owner types wired
# =====================================================================
def test_seed_reactive_abilities_cover_both_owner_types(db, version):
    rows = {row["reactive_ability_id"]: row for row in db.query(
        "SELECT * FROM reactive_abilities WHERE content_version_id = ?", (version,))}
    assert len(rows) == 2
    owner_types = {row["owner_content_type"] for row in rows.values()}
    assert owner_types == {ra.OWNER_CHARACTER, ra.OWNER_ENEMY}


# =====================================================================
# engine hook — §2.13 steps 1-4
# =====================================================================
def test_a_character_owned_counter_hits_the_attacker(db, engine):
    """돌벽의 반격 — char_terradon, self_hp_below 0.5."""
    ally = _set_unit_def(db, _ally(db, engine), "char_terradon",
                         hp_current=20, hp_max=65)   # 30% — below the gate
    enemy = _enemy(db, engine)

    ctx = engine._context(enemy, [ally], fx.ops.CTX_ENEMY_ACTION)
    outcome = fx.execute_effects(
        [{"operator": "deal_flat_damage", "params": {"amount": 3}}], ctx)

    counter_events = [event for event in outcome.damage_events
                      if event["target_id"] == enemy.battle_unit_id]
    assert counter_events, "the ally's counter never landed on the attacker"
    assert any("반격: 돌벽의 반격" in line for line in outcome.log)


def test_the_condition_gates_the_counter(db, engine):
    """Above the 50% gate, 돌벽의 반격 must not fire."""
    ally = _set_unit_def(db, _ally(db, engine), "char_terradon",
                         hp_current=60, hp_max=65)   # ~92% — above the gate
    enemy = _enemy(db, engine)
    enemy_hp_before = enemy.hp_current

    ctx = engine._context(enemy, [ally], fx.ops.CTX_ENEMY_ACTION)
    fx.execute_effects([{"operator": "deal_flat_damage", "params": {"amount": 3}}], ctx)

    assert un.load_unit(db, enemy.battle_unit_id).hp_current == enemy_hp_before


def test_an_enemy_owned_counter_hits_the_player(db, engine):
    """가시 반격 — enemy_w2_가시덩굴, unconditional."""
    enemy = _set_unit_def(db, _enemy(db, engine), "enemy_w2_가시덩굴",
                          hp_current=66, hp_max=66)
    ally = _ally(db, engine)

    ctx = engine._context(ally, [enemy], fx.ops.CTX_BATTLE_CARD)
    outcome = fx.execute_effects(
        [{"operator": "deal_damage", "params": {"multiplier": 1.0}}], ctx)

    counter_events = [event for event in outcome.damage_events
                      if event["target_id"] == ally.battle_unit_id]
    assert counter_events, "가시덩굴's counter never landed on the player"


def test_a_lethal_hit_does_not_trigger_a_counter(db, engine):
    """§2.13 step 2 — 사망 시 무효."""
    ally = _set_unit_def(db, _ally(db, engine), "char_terradon",
                         hp_current=3, hp_max=65)   # dies to the hit below
    enemy = _enemy(db, engine)
    enemy_hp_before = enemy.hp_current

    ctx = engine._context(enemy, [ally], fx.ops.CTX_ENEMY_ACTION)
    fx.execute_effects([{"operator": "deal_flat_damage", "params": {"amount": 10}}], ctx)

    assert un.load_unit(db, ally.battle_unit_id).hp_current == 0
    assert un.load_unit(db, enemy.battle_unit_id).hp_current == enemy_hp_before


def test_mutual_counters_stop_at_the_depth_cap(db, engine, version, balance):
    """Content authoring a mutual counter-loop must not hang the turn."""
    db.execute(
        "INSERT INTO reactive_abilities (content_version_id, reactive_ability_id, "
        "name, owner_content_type, owner_id, trigger_event, sort_order, "
        "condition_operators_json, effects_json, is_retired) VALUES "
        "(?, 'ra_test_상시반격', '상시 반격', ?, 'char_terradon', 'on_damage_taken', "
        "0, '[]', ?, 0)",
        (version, ra.OWNER_CHARACTER,
         json.dumps([{"operator": "deal_flat_damage", "params": {"amount": 1}}])),
    )

    ally = _set_unit_def(db, _ally(db, engine), "char_terradon",
                         hp_current=65, hp_max=65)
    enemy = _set_unit_def(db, _enemy(db, engine), "enemy_w2_가시덩굴",
                          hp_current=66, hp_max=66)

    depth_cap = int(balance.get("reactive_ability_max_depth"))
    ctx = engine._context(enemy, [ally], fx.ops.CTX_ENEMY_ACTION)
    # Must return rather than recurse forever — each level's counter is only
    # 1 flat damage against 65/66 HP pools, so nothing here dies on its own;
    # only the depth cap can stop the chain.
    outcome = fx.execute_effects(
        [{"operator": "deal_flat_damage", "params": {"amount": 1}}], ctx)

    # depth 0 (the initial hit) plus one counter per depth up to the cap,
    # inclusive — see `reactive.fire_on_damage_taken`'s depth check.
    assert len(outcome.damage_events) == depth_cap + 1


def test_a_random_chance_condition_is_journaled_and_replays(db, engine, version):
    """§10.4.5 random_chance, §16.4 — a bare RNG call would not replay."""
    db.execute(
        "UPDATE reactive_abilities SET condition_operators_json = ? "
        "WHERE content_version_id = ? AND reactive_ability_id = 'ra_가시덩굴_가시반격'",
        (json.dumps([{"op": "random_chance", "value": 1.0}]), version),
    )
    enemy = _set_unit_def(db, _enemy(db, engine), "enemy_w2_가시덩굴",
                          hp_current=66, hp_max=66)
    ally = _ally(db, engine)

    ctx = engine._context(ally, [enemy], fx.ops.CTX_BATTLE_CARD)
    outcome = fx.execute_effects(
        [{"operator": "deal_damage", "params": {"multiplier": 1.0}}], ctx)
    assert any(event["target_id"] == ally.battle_unit_id
              for event in outcome.damage_events)

    rows = db.query(
        "SELECT op_key, result_json FROM rng_operations WHERE op_key LIKE ?",
        (f"battle:{engine.battle_id}:%counter%",))
    assert len(rows) == 1
    # Replaying the same op_key must return the same journaled result rather
    # than drawing fresh (§16.4) — the counter fired once, so a second
    # `chance()` call under that exact key must agree.
    assert engine.rng.chance(rows[0]["op_key"], 1.0) == json.loads(rows[0]["result_json"])


# =====================================================================
# §10.5 validation
# =====================================================================
def test_validation_rejects_an_unresolved_owner(db, version):
    db.execute(
        "INSERT INTO reactive_abilities (content_version_id, reactive_ability_id, "
        "name, owner_content_type, owner_id, trigger_event, sort_order, "
        "condition_operators_json, effects_json, is_retired) VALUES "
        "(?, 'ra_bad_owner', 'bad', 'character', 'char_does_not_exist', "
        "'on_damage_taken', 0, '[]', ?, 0)",
        (version, json.dumps([{"operator": "deal_flat_damage", "params": {"amount": 1}}])),
    )
    with pytest.raises(ValidationError, match="does not resolve"):
        validate_version(db, version)


def test_validation_rejects_a_progression_operator_in_effects(db, version):
    """§10.4.1a — a reactive ability resolves inline in a battle turn, so its
    effects must be PURE_SYNCHRONOUS ∩ BATTLE_SAFE, same as a card or passive."""
    db.execute(
        "INSERT INTO reactive_abilities (content_version_id, reactive_ability_id, "
        "name, owner_content_type, owner_id, trigger_event, sort_order, "
        "condition_operators_json, effects_json, is_retired) VALUES "
        "(?, 'ra_bad_effect', 'bad', 'character', 'char_terradon', "
        "'on_damage_taken', 0, '[]', ?, 0)",
        (version, json.dumps([{"operator": "grant_currency",
                              "params": {"currency": "carta", "amount": 100}}])),
    )
    with pytest.raises(ValidationError):
        validate_version(db, version)


def test_validation_rejects_an_unknown_condition_operator(db, version):
    db.execute(
        "INSERT INTO reactive_abilities (content_version_id, reactive_ability_id, "
        "name, owner_content_type, owner_id, trigger_event, sort_order, "
        "condition_operators_json, effects_json, is_retired) VALUES "
        "(?, 'ra_bad_condition', 'bad', 'character', 'char_terradon', "
        "'on_damage_taken', 0, ?, ?, 0)",
        (version, json.dumps([{"op": "not_a_real_operator"}]),
         json.dumps([{"operator": "deal_flat_damage", "params": {"amount": 1}}])),
    )
    with pytest.raises(ValidationError, match="closed set"):
        validate_version(db, version)
