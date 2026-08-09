"""§2.11 turn machine and §2.12 round boundary, driven through a real battle."""

from __future__ import annotations

import pytest

from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.engine import battle as bt
from app.engine import deck
from app.engine import enemy_ai as ai
from app.engine import encounter as enc
from app.engine import lifecycle as lc
from app.engine import statuses as st
from app.engine import timed_effects as te
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
    battle_id = enc.create_battle(
        db, balance, run_id=run_id, node_index=0, encounter_id="enc_tut_2",
        content_version_id=version,
    )
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    built = bt.build_engine(db, balance, battle_id=battle_id, run_id=run_id,
                            content_version_id=version,
                            rng=JournaledRng(db, run_id, run["rng_seed"]))
    built.start()
    return built


# =====================================================================
# Setup
# =====================================================================
def test_starter_deck_is_exactly_eighteen_cards(db, run_id, balance):
    """§4.6.2 — 6/5/7, giving a 79.8% skill rate in a 3-card draw."""
    cards = db.query(
        "SELECT card_id, COUNT(*) AS n FROM run_deck_cards WHERE run_id = ? "
        "GROUP BY card_id", (run_id,))
    counts = {row["card_id"]: row["n"] for row in cards}
    assert sum(counts.values()) == int(balance.get("base_deck_size")) == 18
    assert counts["card_평타"] == 6
    assert counts["card_기본방어"] == 5
    assert counts["card_starter_화염참"] == 7


def test_round_order_is_speed_descending_allies_first_on_ties(engine, db):
    """§2.1: descending speed; allies before enemies on equal speed; same-side
    ties by registration_order."""
    order = db.query(
        "SELECT battle_unit_id FROM battle_round_order WHERE battle_id = ? "
        "AND round_no = 1 ORDER BY order_index", (engine.battle_id,))
    speeds = [
        un.effective_spd(db, engine.status_registry, un.load_unit(db, row["battle_unit_id"]))
        for row in order
    ]
    assert speeds == sorted(speeds, reverse=True)


def test_every_enemy_has_a_telegraph_before_the_player_acts(engine):
    """§2.8.6 — ALL enemies display their next action and target."""
    telegraphs = engine.telegraphs()
    assert telegraphs
    assert all(entry["state"] == "planned" for entry in telegraphs)


def test_party_resource_matches_party_size(engine, db, balance):
    """§15.1 — pool 3 for the solo tutorial party."""
    row = db.one("SELECT party_resource_current FROM battles WHERE battle_id = ?",
                 (engine.battle_id,))
    assert row["party_resource_current"] == 3


# =====================================================================
# §2.11 PHASE D-E — the B-01 fix
# =====================================================================
def test_enemy_turn_executes_its_committed_plan(engine, db):
    """v6.2's turn machine had no enemy execution path at all."""
    enemy = un.load_units(db, engine.battle_id, side=un.ENEMY)[0]
    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    hp_before = ally.hp_current

    # Point the cursor at the enemy and run its turn.
    index = db.one(
        "SELECT order_index FROM battle_round_order WHERE battle_id = ? "
        "AND round_no = 1 AND battle_unit_id = ?",
        (engine.battle_id, enemy.battle_unit_id))
    db.execute("UPDATE battles SET turn_cursor = ? WHERE battle_id = ?",
               (index["order_index"], engine.battle_id))

    result = engine.begin_turn()
    assert result.side == un.ENEMY
    assert result.acted
    assert un.load_unit(db, ally.battle_unit_id).hp_current < hp_before


def test_stunned_enemy_discards_its_plan_and_consumes_no_cooldown(engine, db):
    """§2.11 step 5 — otherwise a player could permanently suppress a boss's
    signature move by stunning it on the turn it was announced."""
    enemy = un.load_units(db, engine.battle_id, side=un.ENEMY)[0]
    st.apply_status(db, engine.status_registry, enemy.battle_unit_id, st.STUN)

    index = db.one(
        "SELECT order_index FROM battle_round_order WHERE battle_id = ? "
        "AND round_no = 1 AND battle_unit_id = ?",
        (engine.battle_id, enemy.battle_unit_id))
    db.execute("UPDATE battles SET turn_cursor = ? WHERE battle_id = ?",
               (index["order_index"], engine.battle_id))

    result = engine.begin_turn()
    assert result.reason == "기절"
    assert not result.acted
    cooldowns = db.query(
        "SELECT * FROM enemy_cooldowns WHERE battle_id = ? AND enemy_unit_id = ?",
        (engine.battle_id, enemy.battle_unit_id))
    assert cooldowns == []


def test_a_consumed_enemy_shows_행동_완료_not_a_speculative_plan(engine, db):
    """§2.8.6, C-03 — generating a next-round plan early would leak information
    and consume journaled RNG out of order."""
    enemy = un.load_units(db, engine.battle_id, side=un.ENEMY)[0]
    ai.discard_plan(db, engine.battle_id, enemy.battle_unit_id)
    telegraph = ai.telegraph_for(db, engine.action_registry, engine.battle_id,
                                 enemy.battle_unit_id)
    assert telegraph == {"state": "acted", "label": ai.TELEGRAPH_ACTED}


# =====================================================================
# §2.11 PHASE D-P
# =====================================================================
def test_player_turn_draws_three_and_awaits_selection(engine, db):
    # A fast enemy often acts before a slow ally (§2.8.6), so advance to the
    # first player decision rather than assuming turn 1 is the ally's.
    result = engine.advance()[-1]
    assert result.side == un.ALLY
    assert result.awaiting_input
    drawn = db.query("SELECT * FROM battle_draw WHERE battle_id = ?",
                     (engine.battle_id,))
    assert len(drawn) == 3
    row = db.one("SELECT turn_phase FROM battles WHERE battle_id = ?",
                 (engine.battle_id,))
    assert row["turn_phase"] == bt.AWAIT_CARD


def test_playing_a_card_consumes_resource_and_deals_damage(engine, db):
    engine.advance()
    unit = engine.acting_unit()
    playable = engine.playable_cards(unit)
    attack = next(entry for entry in playable
                  if entry["card"]["category"] == "공격")
    enemy = un.load_units(db, engine.battle_id, side=un.ENEMY, living_only=True)[0]
    hp_before = enemy.hp_current
    cost = attack["card"]["cost"]

    result = engine.play_card(unit, attack["card_instance_id"],
                              [enemy.battle_unit_id])
    assert result.acted
    assert un.load_unit(db, enemy.battle_unit_id).hp_current < hp_before
    # The pool refills at round start, so the deduction is visible until then.
    battle = db.one("SELECT party_resource_current, round_no FROM battles "
                    "WHERE battle_id = ?", (engine.battle_id,))
    if battle["round_no"] == 1:
        assert battle["party_resource_current"] == 3 - cost


def test_unselected_cards_are_discarded_at_end_of_turn(engine, db, run_id):
    """§2.2 — no persistent hand."""
    engine.advance()
    unit = engine.acting_unit()
    playable = engine.playable_cards(unit)
    engine.play_card(unit, playable[0]["card_instance_id"],
                     [un.load_units(db, engine.battle_id, side=un.ENEMY,
                                    living_only=True)[0].battle_unit_id])
    in_hand = db.query(
        "SELECT * FROM run_deck_cards WHERE run_id = ? AND pile = 'in_hand'",
        (run_id,))
    assert in_hand == []
    assert deck.pile_size(db, run_id, 1, deck.DISCARD) == 3


def test_element_and_silence_filter_the_playable_set(engine, db):
    """§2.10 element lock and §2.5.1 침묵: 공격 stays usable."""
    engine.advance()
    unit = engine.acting_unit()
    st.apply_status(db, engine.status_registry, unit.battle_unit_id, st.SILENCE)
    for entry in engine.playable_cards(unit):
        assert entry["card"]["category"] not in ("버프디버프", "회복")
        assert entry["card"]["element"] in ("무속성", unit.element)


# =====================================================================
# §2.12 round boundary
# =====================================================================
def test_round_advances_only_after_every_entry_is_consumed(engine, db):
    """A round ends when the snapshot is exhausted, not per turn."""
    total = db.one(
        "SELECT COUNT(*) AS n FROM battle_round_order WHERE battle_id = ? "
        "AND round_no = 1", (engine.battle_id,))["n"]

    for _ in range(total):
        battle = db.one("SELECT round_no, state FROM battles WHERE battle_id = ?",
                        (engine.battle_id,))
        if battle["state"] != bt.BATTLE_ACTIVE:
            return
        result = engine.begin_turn()
        if result.awaiting_input:
            unit = engine.acting_unit()
            playable = engine.playable_cards(unit)
            enemies = un.load_units(db, engine.battle_id, side=un.ENEMY,
                                    living_only=True)
            engine.play_card(unit, playable[0]["card_instance_id"],
                             [enemies[0].battle_unit_id] if enemies else [])

    battle = db.one("SELECT round_no FROM battles WHERE battle_id = ?",
                    (engine.battle_id,))
    assert battle["round_no"] == 2


def test_round_boundary_does_not_touch_presentation_revision(engine, db, run_id):
    """B-14 — the §16.7 CAS is the SOLE owner of that counter."""
    before = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                    (run_id,))["presentation_revision"]
    engine.round_boundary()
    after = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                   (run_id,))["presentation_revision"]
    assert before == after


def test_timed_effects_expire_before_the_new_round_is_materialized(engine, db):
    """§2.12 — the ordering of steps 1-3 before 4-8 is load-bearing."""
    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    te.create_invulnerable(db, engine.battle_id, ally.battle_unit_id,
                           current_round=1, duration_rounds=1)

    # Consume every entry so the boundary actually runs ROUND_END.
    db.execute("UPDATE battle_round_order SET consumed = 1 WHERE battle_id = ?",
               (engine.battle_id,))
    engine.round_boundary()

    assert db.one("SELECT round_no FROM battles WHERE battle_id = ?",
                  (engine.battle_id,))["round_no"] == 2
    # Created in round 1 with duration 1 → still covers round 2.
    assert te.is_invulnerable(db, ally.battle_unit_id, 2)


def test_battle_end_is_checked_before_any_refill(engine, db):
    """A battle won by the final unit of a round must not trigger a meaningless
    refill and next-round snapshot."""
    for enemy in un.load_units(db, engine.battle_id, side=un.ENEMY):
        db.execute("UPDATE battle_units SET hp_current = 0, is_alive = 0 "
                   "WHERE battle_unit_id = ?", (enemy.battle_unit_id,))
    state = engine.round_boundary()
    assert state == bt.BATTLE_WON
    assert db.one("SELECT round_no FROM battles WHERE battle_id = ?",
                  (engine.battle_id,))["round_no"] == 1


# =====================================================================
# §2.6.1 summons
# =====================================================================
def test_summon_respects_the_hard_cap_of_eight(db, balance, version, run_id):
    battle_id = enc.create_battle(db, balance, run_id=run_id, node_index=1,
                                  encounter_id="enc_tut_1",
                                  content_version_id=version)
    for _ in range(10):
        enc.summon_enemy(db, battle_id, "enemy_tut_박쥐", version, balance)

    count = db.one("SELECT COUNT(*) AS n FROM battle_units WHERE battle_id = ? "
                   "AND side = 'enemy'", (battle_id,))
    assert count["n"] == 8


def test_dead_units_keep_their_visible_slot(db, balance, version, run_id):
    """§2.6.1 rule 2 — slots are never reclaimed, so the screen does not
    reshuffle mid-fight."""
    battle_id = enc.create_battle(db, balance, run_id=run_id, node_index=2,
                                  encounter_id="enc_tut_2",
                                  content_version_id=version)
    first = un.load_units(db, battle_id, side=un.ENEMY)[0]
    db.execute("UPDATE battle_units SET hp_current = 0 WHERE battle_unit_id = ?",
               (first.battle_unit_id,))
    un.death_check(db, first.battle_unit_id)

    enc.summon_enemy(db, battle_id, "enemy_tut_박쥐", version, balance)
    slots = [unit.visible_slot for unit in un.load_units(db, battle_id, side=un.ENEMY)]
    assert len(slots) == len(set(slots))
    assert un.load_unit(db, first.battle_unit_id).visible_slot == 0


# =====================================================================
# §15.1 party-size downscaling
# =====================================================================
def test_party_size_two_removes_exactly_one_lowest_tier_unit():
    units = [
        {"enemy_id": "a", "slot": 0, "tier": "엘리트"},
        {"enemy_id": "b", "slot": 1, "tier": "일반"},
        {"enemy_id": "c", "slot": 2, "tier": "일반"},
    ]
    tier_rank = {"일반": 0, "엘리트": 1, "보스": 2}
    remaining, adjustment = enc.downscale_for_party_size(units, 2, tier_rank)
    assert len(remaining) == 2
    # lowest tier, tie-broken by HIGHEST authored slot index
    assert adjustment["removed_slot"] == 2


def test_party_size_two_leaves_a_single_unit_encounter_alone():
    units = [{"enemy_id": "a", "slot": 0, "tier": "보스"}]
    remaining, adjustment = enc.downscale_for_party_size(units, 2, {"보스": 2})
    assert len(remaining) == 1
    assert adjustment["removed"] is None
