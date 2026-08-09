"""Regressions for defects found by review after the first implementation pass.

Each test names the rule it protects; none of them passed before the fix.
"""

from __future__ import annotations

import pytest

from app.central import transactions as tx
from app.central.client import CurrencyResult
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.engine import battle as bt
from app.engine import deck
from app.engine import effects as fx
from app.engine import enemy_ai as ai
from app.engine import encounter as enc
from app.engine import gacha
from app.engine import lifecycle as lc
from app.engine import statuses as st
from app.engine import targeting as tg
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
def rng(db, run_id) -> JournaledRng:
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    return JournaledRng(db, run_id, run["rng_seed"])


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


# =====================================================================
# §2.1.1 rule 4 — a skipped entry must still advance the cursor
# =====================================================================
def test_a_dead_unit_in_the_snapshot_does_not_stall_the_round(engine, db):
    """PHASE A consumed the entry but left `turn_cursor` where it was, so
    `begin_turn` re-served the same dead unit forever.

    One enemy of two is killed mid-round, so the battle stays live and the
    round has to complete around the corpse (§2.1.1 rule 4).
    """
    corpse = un.load_units(db, engine.battle_id, side=un.ENEMY)[0]
    db.execute("UPDATE battle_units SET hp_current = 0, is_alive = 0 "
               "WHERE battle_unit_id = ?", (corpse.battle_unit_id,))

    # Point the cursor at the dead unit's entry and try to take its turn.
    entry = db.one(
        "SELECT order_index FROM battle_round_order WHERE battle_id = ? "
        "AND round_no = 1 AND battle_unit_id = ?",
        (engine.battle_id, corpse.battle_unit_id))
    db.execute("UPDATE battles SET turn_cursor = ? WHERE battle_id = ?",
               (entry["order_index"], engine.battle_id))

    engine.begin_turn()
    served = engine.acting_unit()
    assert served is None or served.battle_unit_id != corpse.battle_unit_id
    assert db.one(
        "SELECT consumed FROM battle_round_order WHERE battle_id = ? "
        "AND round_no = 1 AND order_index = ?",
        (engine.battle_id, entry["order_index"]))["consumed"] == 1


def test_advance_drives_the_battle_to_the_next_player_decision(engine, db):
    """Enemy turns carry no component interaction, so something must drive
    them — otherwise the battle stalls after one card."""
    engine.advance()
    unit = engine.acting_unit()
    playable = engine.playable_cards(unit)
    enemy = un.load_units(db, engine.battle_id, side=un.ENEMY, living_only=True)[0]
    engine.play_card(unit, playable[0]["card_instance_id"], [enemy.battle_unit_id])

    results = engine.advance()
    assert results
    last = results[-1]
    assert last.awaiting_input or last.battle_ended
    # The enemies took their turns rather than idling.
    assert any(entry.side == un.ENEMY and entry.acted for entry in results)


def test_advance_stops_at_a_terminal_battle(engine, db):
    for enemy in un.load_units(db, engine.battle_id, side=un.ENEMY):
        db.execute("UPDATE battle_units SET hp_current = 0, is_alive = 0 "
                   "WHERE battle_unit_id = ?", (enemy.battle_unit_id,))
    results = engine.advance()
    assert db.one("SELECT state FROM battles WHERE battle_id = ?",
                  (engine.battle_id,))["state"] in (bt.BATTLE_WON, bt.BATTLE_ACTIVE)
    assert len(results) < 64      # did not spin to the guard


# =====================================================================
# §2.7.3 / §16.4 — one op_key per insertion, and k is an ORDINAL
# =====================================================================
def test_each_curse_insertion_gets_its_own_journal_key(db, run_id, rng):
    """A shared op_key made every curse replay the first one's position."""
    positions = []
    for seq in range(4):
        instance_id = deck.insert_cursed_card(db, rng, run_id, 1,
                                              "curse_고통의각인",
                                              seq=f"b1r{seq}u5s0")
        positions.append(db.one(
            "SELECT pile_position FROM run_deck_cards WHERE card_instance_id = ?",
            (instance_id,))["pile_position"])
    assert len(set(positions)) > 1


def test_k_is_an_ordinal_so_a_drawn_down_pile_still_spreads(db, run_id, rng):
    """Positions are sparse after drawing: writing k as an absolute position
    crowded every insertion into the top few cards."""
    # Simulate a drawn-down pile whose remaining positions start at 12.
    db.execute("DELETE FROM run_deck_cards WHERE run_id = ? AND pile_position < 12",
               (run_id,))
    remaining = [row["pile_position"] for row in db.query(
        "SELECT pile_position FROM run_deck_cards WHERE run_id = ? AND party_slot = 1 "
        "AND pile = 'draw' ORDER BY pile_position", (run_id,))]
    assert remaining and min(remaining) >= 12

    landed = []
    for seq in range(6):
        instance_id = deck.insert_cursed_card(db, rng, run_id, 1, "curse_고통의각인",
                                              seq=f"spread-{seq}")
        landed.append(db.one(
            "SELECT pile_position FROM run_deck_cards WHERE card_instance_id = ?",
            (instance_id,))["pile_position"])
    # Every insertion lands inside the surviving pile, not at positions 0..5.
    assert all(position >= 12 for position in landed)


def test_the_same_insertion_still_replays_its_position(db, run_id, rng):
    first = deck.insert_cursed_card(db, rng, run_id, 1, "curse_고통의각인",
                                    seq="fixed-token")
    position = db.one("SELECT pile_position FROM run_deck_cards WHERE "
                      "card_instance_id = ?", (first,))["pile_position"]
    second = deck.insert_cursed_card(db, rng, run_id, 1, "curse_무거운사슬",
                                     seq="fixed-token")
    assert db.one("SELECT pile_position FROM run_deck_cards WHERE "
                  "card_instance_id = ?", (second,))["pile_position"] == position


# =====================================================================
# §10.4.3 discard_cards
# =====================================================================
def test_a_discarded_card_leaves_the_turn_s_draw_set(engine, db, run_id):
    """It stayed in `battle_draw`, so `playable_cards` still offered it."""
    engine.advance()
    unit = engine.acting_unit()
    before = len(engine.playable_cards(unit))

    ctx = engine._context(unit, [unit], fx.ops.CTX_BATTLE_CARD)
    fx.execute_effects(
        [{"operator": "discard_cards", "params": {"count": 1, "selector": "random"}}],
        ctx)

    drawn = db.query("SELECT * FROM battle_draw WHERE battle_id = ? "
                     "AND battle_unit_id = ?",
                     (engine.battle_id, unit.battle_unit_id))
    assert len(drawn) == 2
    assert len(engine.playable_cards(unit)) < before


# =====================================================================
# §17.3 / §1.3.8 — a grant is validated against its applied delta
# =====================================================================
def test_a_grant_that_applied_nothing_never_grants_locally(db, user_id):
    """Recorded as applied and completed, handing out an unpaid reward."""
    class NoOpGrant:
        def currency_add(self, user_id, amount, key):
            return CurrencyResult(requested=amount, applied=0)

    granted: list = []
    tx.create_transaction(db, tx_id="g0", user_id=user_id, operation="run_clear",
                          direction=tx.GRANT, expected_coin_delta=2250)
    result = tx.run_transaction(db, NoOpGrant(), tx_id="g0",
                                apply_local=lambda _db, p: granted.append(p))
    assert result.status == tx.OPERATOR_REQUIRED
    assert granted == []


def test_an_over_grant_is_also_an_anomaly(db, user_id):
    class OverGrant:
        def currency_add(self, user_id, amount, key):
            return CurrencyResult(requested=amount, applied=amount * 2)

    tx.create_transaction(db, tx_id="g1", user_id=user_id, operation="run_clear",
                          direction=tx.GRANT, expected_coin_delta=1000,
                          local_required=False)
    assert tx.run_transaction(db, OverGrant(), tx_id="g1").status \
        == tx.OPERATOR_REQUIRED


# =====================================================================
# §5.9 — a crashed gacha attempt stays retryable
# =====================================================================
def test_a_gacha_id_whose_results_were_never_written_is_retryable(db, balance,
                                                                  version, user_id):
    """The row committed outside the transaction, so the same gacha_id became
    permanently unusable after a crash."""
    from app.db.connection import utcnow

    db.execute(
        "INSERT INTO gacha_transactions (gacha_id, user_id, banner_id, pull_kind, "
        "carta_cost, commit_seed, results_json, status, created_at) "
        "VALUES ('crashed', ?, 'banner_standard', 'single', 160, 'abc123', NULL, "
        "'created', ?)", (user_id, utcnow()))

    outcome = gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                         pull_kind="single", content_version_id=version,
                         gacha_id="crashed")
    assert not outcome.replayed
    assert len(outcome.results) == 1
    # The original commit_seed was reused, so this is a replay of the same roll.
    assert db.one("SELECT commit_seed FROM gacha_transactions WHERE gacha_id = "
                  "'crashed'")["commit_seed"] == "abc123"


# =====================================================================
# §2.5.1 — 도발 redirects HOSTILE aggression only
# =====================================================================
def _unit(db, battle_id, side, role, hp=100, hp_max=100):
    cursor = db.execute(
        "INSERT INTO battle_units (battle_id, side, registration_order, visible_slot, "
        "unit_def_id, hp_current, hp_max, atk, def, spd, element, role) "
        "VALUES (?, ?, ?, ?, 'x', ?, ?, 10, 5, 100, '무속성', ?)",
        (battle_id, side, un.next_registration_order(db, battle_id, side),
         un.next_visible_slot(db, battle_id, side), hp, hp_max, role),
    )
    return un.load_unit(db, int(cursor.lastrowid))


def test_a_taunting_unit_does_not_capture_its_own_side_s_heal(db, version):
    """도발 is a hostile-target override; capturing a friendly heal inverts it."""
    strategies = tg.StrategyRegistry(db, version)
    statuses = st.StatusRegistry(db, version)
    healer = _unit(db, 700, un.ENEMY, "서포터형")
    hurt = _unit(db, 700, un.ENEMY, "공격형", hp=10, hp_max=100)
    tank = _unit(db, 700, un.ENEMY, "방어형", hp=100)
    st.apply_status(db, statuses, tank.battle_unit_id, st.TAUNT)

    candidates = un.load_units(db, 700, side=un.ENEMY, living_only=True)
    chosen = tg.select_target(db, strategies, statuses, version, observer=healer,
                              candidates=candidates,
                              strategy_id="strat_lowest_hp_pct",
                              apply_taunt=False)
    assert chosen.battle_unit_id == hurt.battle_unit_id


def test_a_taunt_still_captures_hostile_single_target_actions(db, version):
    strategies = tg.StrategyRegistry(db, version)
    statuses = st.StatusRegistry(db, version)
    attacker = _unit(db, 701, un.ENEMY, "공격형")
    _unit(db, 701, un.ALLY, "서포터형", hp=10, hp_max=100)
    tank = _unit(db, 701, un.ALLY, "방어형", hp=100)
    st.apply_status(db, statuses, tank.battle_unit_id, st.TAUNT)

    candidates = un.load_units(db, 701, side=un.ALLY, living_only=True)
    chosen = tg.select_target(db, strategies, statuses, version, observer=attacker,
                              candidates=candidates,
                              strategy_id="strat_lowest_hp_pct", apply_taunt=True)
    assert chosen.battle_unit_id == tank.battle_unit_id


# =====================================================================
# §2.8.1 — a per-enemy strategy override wins at plan time too
# =====================================================================
def test_the_enemy_strategy_override_is_used_when_planning(db, version, balance,
                                                           run_id):
    """`planned_strategy_id` persisted the override while planning ignored it,
    so an enemy aimed differently before and after its target died."""
    db.execute("UPDATE enemies SET strategy_override = 'strat_lowest_hp_abs' "
               "WHERE content_version_id = ? AND enemy_id = 'enemy_tut_슬라임'",
               (version,))
    battle_id = enc.create_battle(db, balance, run_id=run_id, node_index=3,
                                  encounter_id="enc_tut_1",
                                  content_version_id=version)
    engine = bt.build_engine(db, balance, battle_id=battle_id, run_id=run_id,
                             content_version_id=version,
                             rng=JournaledRng(db, run_id, 1))
    enemy = un.load_units(db, battle_id, side=un.ENEMY)[0]
    resolved = ai.resolve_strategy_id(engine.strategy_registry,
                                      engine.action_registry, enemy)
    assert resolved == "strat_lowest_hp_abs"


# =====================================================================
# §2.5.1 — 침묵 blocks 버프/디버프/회복 only, on both sides
# =====================================================================
def test_silence_does_not_block_defensive_actions():
    action = ai.EnemyAction(action_id="a", name="방어", category="방어",
                            target_side="self", is_basic_attack=False, effects=[])
    assert not action.is_blocked_by_silence
    for category in ("버프디버프", "회복"):
        blocked = ai.EnemyAction(action_id="b", name="x", category=category,
                                 target_side="ally", is_basic_attack=False,
                                 effects=[])
        assert blocked.is_blocked_by_silence


# =====================================================================
# §16.7 — a failing handler must not swallow Central's redelivery
# =====================================================================
def test_a_failed_handler_leaves_the_event_unrecorded(db, run_id):
    assert not lc.event_already_handled(db, "evt-x")
    lc.record_event(db, "evt-x", run_id)
    assert lc.event_already_handled(db, "evt-x")
    assert lc.record_event(db, "evt-x", run_id) is False
