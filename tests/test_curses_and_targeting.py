"""§2.7 cursed cards and §2.5.1/§2.8 targeting including the 도발 override."""

from __future__ import annotations

import pytest

from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.engine import battle as bt
from app.engine import deck
from app.engine import encounter as enc
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
# §2.7.3 atomic insertion
# =====================================================================
def test_a_cursed_card_lands_at_a_journaled_position(db, run_id, rng):
    before = deck.pile_size(db, run_id, 1, deck.DRAW)
    instance_id = deck.insert_cursed_card(db, rng, run_id, 1, "curse_고통의각인", seq=0)

    assert deck.pile_size(db, run_id, 1, deck.DRAW) == before + 1
    row = db.one("SELECT * FROM run_deck_cards WHERE card_instance_id = ?",
                 (instance_id,))
    assert row["is_cursed"] == 1
    assert 0 <= row["pile_position"] <= before


def test_pile_positions_stay_unique_after_the_shift(db, run_id, rng):
    """The UNIQUE(run_id, party_slot, pile, pile_position) index makes a
    partially applied shift fail loudly instead of producing two cards at one
    index with SQL-order-dependent draw order."""
    for seq in range(4):
        deck.insert_cursed_card(db, rng, run_id, 1, "curse_고통의각인", seq=seq)

    positions = [row["pile_position"] for row in db.query(
        "SELECT pile_position FROM run_deck_cards WHERE run_id = ? AND party_slot = 1 "
        "AND pile = 'draw'", (run_id,))]
    assert len(positions) == len(set(positions))
    assert sorted(positions) == list(range(len(positions)))


def test_the_insertion_position_is_replayed_on_retry(db, run_id, rng):
    first = deck.insert_cursed_card(db, rng, run_id, 1, "curse_고통의각인", seq=7)
    position = db.one("SELECT pile_position FROM run_deck_cards WHERE "
                      "card_instance_id = ?", (first,))["pile_position"]
    # Same op_key → same k.
    second = deck.insert_cursed_card(db, rng, run_id, 1, "curse_무거운사슬", seq=7)
    assert db.one("SELECT pile_position FROM run_deck_cards WHERE "
                  "card_instance_id = ?", (second,))["pile_position"] == position


# =====================================================================
# §2.7.1 forced resolution
# =====================================================================
class _TopOfPile(JournaledRng):
    """Forces §2.7.3's `k` to 0 so the curse lands where the next draw sees it."""

    def randint(self, op_key, low, high):
        return 0


def _curse_on_top(db, run_id, card_id="curse_고통의각인", seq=0):
    return deck.insert_cursed_card(db, _TopOfPile(db, run_id, 1), run_id, 1,
                                   card_id, seq=seq)


def _ally_turn(engine):
    """Step turns until the ally's own turn resolves, and return that result.

    `advance()` runs on to the *next* player decision, which is one turn too
    far when the turn under test is the one that forfeits or auto-defends.
    """
    for _ in range(32):
        result = engine.begin_turn()
        if result.side == un.ALLY or result.battle_ended:
            return result
    raise AssertionError("the ally never got a turn")


def test_a_drawn_curse_forfeits_the_turn_but_still_reaches_phase_e(engine, db,
                                                                   run_id):
    """P4 — durations, boss phases and telegraphs must still update."""
    _curse_on_top(db, run_id)

    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]

    result = _ally_turn(engine)
    assert result.reason == "저주받은 카드"
    assert not result.acted
    # 8 flat self-damage, ignoring block and 방어력 (§15.5). Asserted on the
    # turn's own damage events: a faster enemy has usually already hit by now.
    assert [event["hp_loss"] for event in result.damage_events] == [8]
    # The turn's entry was consumed, so the round can complete.
    assert db.one(
        "SELECT consumed FROM battle_round_order WHERE battle_id = ? "
        "AND battle_unit_id = ? AND round_no = 1",
        (engine.battle_id, ally.battle_unit_id))["consumed"] == 1


def test_a_curse_is_not_consumed_on_use(engine, db, run_id):
    """It goes to discard and returns on the next reshuffle."""
    _curse_on_top(db, run_id)
    _ally_turn(engine)

    survivors = deck.cursed_cards_in_deck(db, run_id)
    assert len(survivors) == 1
    assert survivors[0]["pile"] == "discard"


def test_cursed_penalties_can_kill(engine, db, run_id):
    """No HP-1 floor; a floor would remove the threat."""
    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    db.execute("UPDATE battle_units SET hp_current = 5 WHERE battle_unit_id = ?",
               (ally.battle_unit_id,))
    _curse_on_top(db, run_id)

    result = _ally_turn(engine)
    assert ally.battle_unit_id in result.deaths
    assert not un.load_unit(db, ally.battle_unit_id).is_alive


# =====================================================================
# §2.2 draw pile exhaustion
# =====================================================================
def test_an_empty_draw_pile_auto_defends_then_reshuffles(engine, db, run_id):
    db.execute("UPDATE run_deck_cards SET pile = 'discard' WHERE run_id = ? "
               "AND party_slot = 1", (run_id,))
    # Re-pack discard positions so the unique index holds.
    for position, row in enumerate(db.query(
        "SELECT card_instance_id FROM run_deck_cards WHERE run_id = ? "
        "AND party_slot = 1 ORDER BY card_instance_id", (run_id,))
    ):
        db.execute("UPDATE run_deck_cards SET pile_position = ? WHERE "
                   "card_instance_id = ?", (position, row["card_instance_id"]))

    result = _ally_turn(engine)
    assert result.reason == "draw pile exhausted"
    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    # block = 방어력 × 1.0
    assert ally.block == int(un.effective_def(db, ally))
    assert deck.pile_size(db, run_id, 1, deck.DRAW) == 18


# =====================================================================
# §2.5.1 the 도발 universal override (C-01)
# =====================================================================
def _make_unit(db, battle_id, side, role, spd=100, atk=10, hp=100):
    cursor = db.execute(
        "INSERT INTO battle_units (battle_id, side, registration_order, visible_slot, "
        "unit_def_id, hp_current, hp_max, atk, def, spd, element, role) "
        "VALUES (?, ?, ?, ?, 'x', ?, ?, ?, 5, ?, '무속성', ?)",
        (battle_id, side, un.next_registration_order(db, battle_id, side),
         un.next_visible_slot(db, battle_id, side), hp, hp, atk, spd, role),
    )
    return un.load_unit(db, int(cursor.lastrowid))


def test_taunt_overrides_the_strategy_unconditionally(db, version):
    """v6.2 said only strategies "that respect taunt" prioritize the taunting
    ally, while §2.8.6 unconditionally re-targeted onto it. Both cannot be true."""
    strategies = tg.StrategyRegistry(db, version)
    statuses = st.StatusRegistry(db, version)
    observer = _make_unit(db, 900, un.ENEMY, "공격형")
    weak = _make_unit(db, 900, un.ALLY, "서포터형", hp=10)
    tank = _make_unit(db, 900, un.ALLY, "방어형", hp=100)

    candidates = un.load_units(db, 900, side=un.ALLY, living_only=True)
    # Without taunt, 최저 HP% picks the weak one.
    chosen = tg.select_target(db, strategies, statuses, version, observer=observer,
                              candidates=candidates,
                              strategy_id="strat_lowest_hp_pct")
    assert chosen.battle_unit_id == weak.battle_unit_id

    st.apply_status(db, statuses, tank.battle_unit_id, st.TAUNT)
    chosen = tg.select_target(db, strategies, statuses, version, observer=observer,
                              candidates=candidates,
                              strategy_id="strat_lowest_hp_pct")
    assert chosen.battle_unit_id == tank.battle_unit_id


def test_multiple_taunts_break_by_registration_order(db, version):
    strategies = tg.StrategyRegistry(db, version)
    statuses = st.StatusRegistry(db, version)
    observer = _make_unit(db, 901, un.ENEMY, "공격형")
    first = _make_unit(db, 901, un.ALLY, "방어형")
    second = _make_unit(db, 901, un.ALLY, "방어형")
    st.apply_status(db, statuses, first.battle_unit_id, st.TAUNT)
    st.apply_status(db, statuses, second.battle_unit_id, st.TAUNT)

    candidates = un.load_units(db, 901, side=un.ALLY, living_only=True)
    chosen = tg.select_target(db, strategies, statuses, version, observer=observer,
                              candidates=candidates, strategy_id="strat_random")
    assert chosen.battle_unit_id == first.battle_unit_id


def test_a_dedicated_taunt_strategy_no_longer_exists(db, version):
    """Seed strategy #7 (도발 우선) was removed — it is now redundant."""
    rows = db.query("SELECT strategy_id, name FROM targeting_strategies "
                    "WHERE content_version_id = ?", (version,))
    assert len(rows) == 7
    assert not any("도발" in row["name"] for row in rows)


def test_the_tanker_defaults_to_highest_threat(db, version):
    """With 도발 a universal override, the old default/fallback pair collapsed
    into just 최고 위협도."""
    strategies = tg.StrategyRegistry(db, version)
    default, fallback = strategies.role_default("방어형")
    assert default == "strat_highest_threat"
    assert fallback is None


# =====================================================================
# §2.8.2 threat model, verified against §15.3's published scores
# =====================================================================
@pytest.mark.parametrize("observer_role,expected_role", [
    ("공격형", "서포터형"),      # 서포터 15.0 is the top score
    ("방어형", "공격형"),        # 공격형 24.0
    ("서포터형", "디버퍼형"),    # 디버퍼 17.6
])
def test_threat_scores_pick_the_documented_target(db, balance, version,
                                                  observer_role, expected_role):
    strategies = tg.StrategyRegistry(db, version)
    statuses = st.StatusRegistry(db, version)
    battle_id = 902 + hash(observer_role) % 50
    observer = _make_unit(db, battle_id, un.ENEMY, observer_role)

    base = balance.get("base_stats_by_role")
    allies = {}
    for role in ("공격형", "방어형", "서포터형", "디버퍼형", "딜서포트형"):
        allies[role] = _make_unit(db, battle_id, un.ALLY, role,
                                  atk=base[role]["atk"])

    candidates = un.load_units(db, battle_id, side=un.ALLY, living_only=True)
    chosen = tg.select_target(db, strategies, statuses, version, observer=observer,
                              candidates=candidates,
                              strategy_id="strat_highest_threat")
    assert chosen.battle_unit_id == allies[expected_role].battle_unit_id


def test_a_side_neutral_strategy_selects_within_the_resolved_side(db, version):
    """§2.8.2 — a heal automatically looks at friendlies, an attack at hostiles,
    with no per-strategy special casing."""
    observer = _make_unit(db, 950, un.ENEMY, "서포터형")
    _make_unit(db, 950, un.ENEMY, "공격형", hp=20)
    _make_unit(db, 950, un.ALLY, "공격형", hp=20)

    friendly = tg.valid_targets(db, 950, observer, "ally")
    hostile = tg.valid_targets(db, 950, observer, "enemy")
    assert all(unit.side == un.ENEMY for unit in friendly)
    assert all(unit.side == un.ALLY for unit in hostile)
