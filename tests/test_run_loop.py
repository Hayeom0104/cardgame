"""§3 node resolution and the §16.2 run loop, end to end."""

from __future__ import annotations

import json

import pytest

from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID, WORLD_1_ID
from app.engine import battle as bt
from app.engine import deck
from app.engine import lifecycle as lc
from app.engine import map_gen
from app.engine import nodes
from app.engine import settlement as sl
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


def _node_of_type(db, run_id, node_type):
    row = db.one(
        "SELECT * FROM run_nodes WHERE run_id = ? AND node_type = ? "
        "ORDER BY node_index LIMIT 1", (run_id, node_type))
    assert row is not None, f"no {node_type} node in this map"
    return row


def _enter(db, run_id, node):
    """Put the run at a node the way `_on_node_choose` does."""
    db.execute(
        "UPDATE runs SET state = ?, current_node_index = ?, "
        "deepest_depth_reached = MAX(deepest_depth_reached, ?) WHERE run_id = ?",
        (lc.NODE_RESOLUTION, node["node_index"], node["depth"], run_id))


# =====================================================================
# 전투 (§16.2 node_resolution ──전투──▶ battle)
# =====================================================================
def test_a_combat_node_materializes_and_starts_a_battle(db, balance, rng, run_id):
    node = _node_of_type(db, run_id, map_gen.COMBAT)
    _enter(db, run_id, node)

    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert result["screen"] == "battle"
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.BATTLE

    battle = db.one("SELECT * FROM battles WHERE battle_id = ?",
                    (result["battle_id"],))
    assert battle["state"] == bt.BATTLE_ACTIVE
    assert battle["round_no"] == 1
    # Round 1's snapshot, resource pool and telegraphs are all in place.
    assert db.query("SELECT * FROM battle_round_order WHERE battle_id = ?",
                    (result["battle_id"],))
    assert db.query("SELECT * FROM enemy_plans WHERE battle_id = ?",
                    (result["battle_id"],))


def test_the_encounter_roll_is_journaled_so_recovery_reproduces_it(db, balance,
                                                                   rng, run_id):
    """§16.8 — `node_resolution` recovery re-runs resolution, and the rng
    journal reproduces the original roll."""
    node = _node_of_type(db, run_id, map_gen.COMBAT)
    _enter(db, run_id, node)
    first = nodes.resolve_node(db, balance, rng, run_id=run_id,
                               node_index=node["node_index"])

    _enter(db, run_id, node)      # simulate a restart mid-resolution
    second = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert first["encounter_id"] == second["encounter_id"]
    # The retry inserted a new attempt rather than reusing the row.
    assert second["battle_id"] != first["battle_id"]


# =====================================================================
# 휴식 (§15.7)
# =====================================================================
def test_a_rest_node_heals_and_returns_to_the_map(db, balance, rng, run_id):
    db.execute("UPDATE run_characters SET hp_current = 10 WHERE run_id = ?",
               (run_id,))
    node = _node_of_type(db, run_id, map_gen.REST)
    _enter(db, run_id, node)

    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert result["screen"] == "map"
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.MAP_NAVIGATION

    member = db.one("SELECT * FROM run_characters WHERE run_id = ?", (run_id,))
    # Tutorial heal is 50% of max HP — deliberately not a full heal.
    assert member["hp_current"] == 10 + int(member["hp_max"] * 0.5)


def test_a_rest_does_not_revive_a_fallen_member(db, balance, rng, run_id):
    db.execute("UPDATE run_characters SET hp_current = 0 WHERE run_id = ?",
               (run_id,))
    node = _node_of_type(db, run_id, map_gen.REST)
    _enter(db, run_id, node)
    nodes.resolve_node(db, balance, rng, run_id=run_id,
                       node_index=node["node_index"])
    assert db.one("SELECT hp_current FROM run_characters WHERE run_id = ?",
                  (run_id,))["hp_current"] == 0


# =====================================================================
# 보상 (§3.2, §15.7)
# =====================================================================
def test_a_reward_node_offers_three_cards_with_a_skip(db, balance, rng, run_id):
    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)

    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert result["screen"] == "reward"
    assert result["skip_available"] is True
    assert len(result["options"]) == 3
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.REWARD_SELECTION


def test_the_offer_is_persisted_so_a_restart_re_renders_the_same_cards(
        db, balance, rng, run_id):
    """M-06 — not a fresh roll."""
    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)
    first = nodes.resolve_node(db, balance, rng, run_id=run_id,
                               node_index=node["node_index"])

    _enter(db, run_id, node)
    second = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert second["replayed"] is True
    assert ([entry["card_id"] for entry in second["options"]]
            == [entry["card_id"] for entry in first["options"]])


def test_only_characters_that_can_legally_play_a_card_are_offered(db, balance,
                                                                  rng, run_id):
    """§3.2 / §2.10 — 무속성 cards may go to anyone."""
    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])

    # The solo starter is 화, so only 화 and 무속성 cards are offerable at all,
    # and each lists the starter's slot as its recipient.
    for option in result["options"]:
        assert option["element"] in ("무속성", "화")
        assert option["recipients"] == [1]


def test_taking_a_card_adds_it_to_that_run_s_deck_only(db, balance, rng, run_id,
                                                       user_id):
    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    option = next(entry for entry in result["options"] if entry["recipients"])

    before = db.one("SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ?",
                    (run_id,))["n"]
    unlocked_before = db.one(
        "SELECT COUNT(*) AS n FROM unlocked_cards WHERE user_id = ?",
        (user_id,))["n"]

    nodes.choose_reward(db, run_id, choice_id=result["choice_id"],
                        card_id=option["card_id"], party_slot=1)

    assert db.one("SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ?",
                  (run_id,))["n"] == before + 1
    # The account's permanent unlock list is untouched.
    assert db.one("SELECT COUNT(*) AS n FROM unlocked_cards WHERE user_id = ?",
                  (user_id,))["n"] == unlocked_before
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.MAP_NAVIGATION


def test_skipping_keeps_the_deck_the_same_size(db, balance, rng, run_id):
    """Without 안 받기, deck size only grows and deck-thinning is impossible."""
    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    before = db.one("SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ?",
                    (run_id,))["n"]

    nodes.choose_reward(db, run_id, choice_id=result["choice_id"], card_id=None,
                        party_slot=None)
    assert db.one("SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ?",
                  (run_id,))["n"] == before


def test_an_illegal_recipient_is_rejected(db, balance, rng, run_id):
    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    option = result["options"][0]
    with pytest.raises(nodes.NodeError, match="recipient"):
        nodes.choose_reward(db, run_id, choice_id=result["choice_id"],
                            card_id=option["card_id"], party_slot=99)


# =====================================================================
# 상점 (§7.1)
# =====================================================================
def test_a_shop_node_generates_four_to_six_one_time_rows(db, balance, rng, run_id):
    node = _node_of_type(db, run_id, map_gen.SHOP)
    _enter(db, run_id, node)

    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert result["screen"] == "shop"
    assert 4 <= len(result["items"]) <= 6
    assert all(item["purchased"] == 0 for item in result["items"])
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.SHOP


def test_the_shop_sells_only_cards_and_immediate_effects(db, balance, rng, run_id):
    """B-16 — no storage or use-flow exists for consumables or buffs."""
    node = _node_of_type(db, run_id, map_gen.SHOP)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    for item in result["items"]:
        assert json.loads(item["item_ref"])["kind"] in ("card", "effect")


def test_a_purchase_spends_run_currency_and_loops(db, balance, rng, run_id):
    """The shop is a SELF-LOOP: the run stays in `shop` (§16.2)."""
    db.execute("UPDATE runs SET run_currency = 500 WHERE run_id = ?", (run_id,))
    node = _node_of_type(db, run_id, map_gen.SHOP)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    item = result["items"][0]

    purchase = nodes.buy_shop_item(db, rng, run_id,
                                   node_index=node["node_index"],
                                   item_index=item["item_index"], party_slot=1)
    assert purchase["run_currency"] == 500 - item["price"]
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.SHOP


def test_a_listing_cannot_be_bought_twice(db, balance, rng, run_id):
    """C-08 — each row is one-time stock."""
    db.execute("UPDATE runs SET run_currency = 5000 WHERE run_id = ?", (run_id,))
    node = _node_of_type(db, run_id, map_gen.SHOP)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    item = result["items"][0]

    nodes.buy_shop_item(db, rng, run_id, node_index=node["node_index"],
                        item_index=item["item_index"], party_slot=1)
    with pytest.raises(nodes.NodeError, match="이미 구매"):
        nodes.buy_shop_item(db, rng, run_id, node_index=node["node_index"],
                            item_index=item["item_index"], party_slot=1)


def test_an_unaffordable_purchase_is_rejected(db, balance, rng, run_id):
    db.execute("UPDATE runs SET run_currency = 0 WHERE run_id = ?", (run_id,))
    node = _node_of_type(db, run_id, map_gen.SHOP)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    with pytest.raises(nodes.NodeError, match="재화"):
        nodes.buy_shop_item(db, rng, run_id, node_index=node["node_index"],
                            item_index=result["items"][0]["item_index"])


def test_leaving_the_shop_returns_to_the_map(db, run_id):
    db.execute("UPDATE runs SET state = ? WHERE run_id = ?", (lc.SHOP, run_id))
    nodes.leave_shop(db, run_id)
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.MAP_NAVIGATION


def test_shop_stock_is_replayed_after_a_restart(db, balance, rng, run_id):
    node = _node_of_type(db, run_id, map_gen.SHOP)
    _enter(db, run_id, node)
    first = nodes.resolve_node(db, balance, rng, run_id=run_id,
                               node_index=node["node_index"])
    _enter(db, run_id, node)
    second = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert second["replayed"] is True
    assert ([item["item_ref"] for item in second["items"]]
            == [item["item_ref"] for item in first["items"]])


# =====================================================================
# 이벤트 (§3.3)
# =====================================================================
def test_an_event_node_persists_its_outcome_before_presenting(db, balance, rng,
                                                              run_id):
    node = _node_of_type(db, run_id, map_gen.EVENT)
    _enter(db, run_id, node)

    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert result["screen"] == "event"
    stored = db.one("SELECT * FROM pending_choices WHERE choice_id = ?",
                    (result["choice_id"],))
    assert stored["status"] == "open"
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.EVENT_CHOICE


def test_a_normal_event_branch_returns_to_the_map(db, balance, rng, run_id):
    node = _node_of_type(db, run_id, map_gen.EVENT)
    _enter(db, run_id, node)
    # Pin the event so the branch semantics are known.
    db.execute("DELETE FROM pending_choices WHERE run_id = ?", (run_id,))
    _force_event(db, run_id, node, "event_떠도는상인")

    choice = db.one("SELECT choice_id FROM pending_choices WHERE run_id = ?",
                    (run_id,))
    result = nodes.choose_event_branch(db, balance, rng, run_id,
                                       choice_id=choice["choice_id"],
                                       branch_index=0)
    assert result["screen"] == "map"
    assert db.one("SELECT run_currency FROM runs WHERE run_id = ?",
                  (run_id,))["run_currency"] == 55


def test_an_ambush_branch_transitions_into_battle(db, balance, rng, run_id):
    """§3.3.1 events 4 and 8 use TERMINAL_STATE_TRANSITION and therefore
    transition the lifecycle rather than returning to the map."""
    node = _node_of_type(db, run_id, map_gen.EVENT)
    _enter(db, run_id, node)
    db.execute("DELETE FROM pending_choices WHERE run_id = ?", (run_id,))
    _force_event(db, run_id, node, "event_매복")

    choice = db.one("SELECT choice_id FROM pending_choices WHERE run_id = ?",
                    (run_id,))
    result = nodes.choose_event_branch(db, balance, rng, run_id,
                                       choice_id=choice["choice_id"],
                                       branch_index=0)
    assert result["screen"] == "battle"
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.BATTLE


def _force_event(db, run_id, node, event_id):
    import uuid

    from app.db.connection import utcnow

    event = db.one(
        "SELECT * FROM events WHERE event_id = ? AND content_version_id = "
        "(SELECT content_version_id FROM runs WHERE run_id = ?)",
        (event_id, run_id))
    branches = json.loads(event["branches_json"])
    db.execute(
        "INSERT INTO pending_choices (choice_id, run_id, node_index, choice_type, "
        "options_json, remaining_operators_json, operator_cursor, status, "
        "rng_op_key, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, 'open', NULL, ?)",
        (uuid.uuid4().hex, run_id, node["node_index"], nodes.CHOICE_EVENT,
         json.dumps({"event_id": event_id, "name": event["name"]},
                    ensure_ascii=False),
         json.dumps(branches, ensure_ascii=False), utcnow()))
    db.execute("UPDATE runs SET state = ? WHERE run_id = ?",
               (lc.EVENT_CHOICE, run_id))


# =====================================================================
# Battle conclusion (§16.2)
# =====================================================================
def _win_battle(db, run_id, battle_id):
    db.execute("UPDATE battle_units SET hp_current = 0, is_alive = 0 "
               "WHERE battle_id = ? AND side = 'enemy'", (battle_id,))
    db.execute("UPDATE battles SET state = ? WHERE battle_id = ?",
               (bt.BATTLE_WON, battle_id))


def _lose_battle(db, run_id, battle_id):
    db.execute("UPDATE battle_units SET hp_current = 0, is_alive = 0 "
               "WHERE battle_id = ? AND side = 'ally'", (battle_id,))
    db.execute("UPDATE battles SET state = ? WHERE battle_id = ?",
               (bt.BATTLE_LOST, battle_id))


def test_a_normal_victory_pays_run_currency_and_returns_to_the_map(db, balance,
                                                                   rng, run_id):
    node = _node_of_type(db, run_id, map_gen.COMBAT)
    _enter(db, run_id, node)
    started = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                 node_index=node["node_index"])
    _win_battle(db, run_id, started["battle_id"])

    result = nodes.conclude_battle(db, balance, rng, run_id=run_id,
                                   battle_id=started["battle_id"])
    assert result["screen"] == "map"
    # §15.4 — 전투 clear pays 25-45 탐험 자금.
    assert 25 <= result["rewards"]["run_currency"] <= 45
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.MAP_NAVIGATION


def test_drops_go_to_the_run_inventory_not_the_account(db, balance, rng, run_id,
                                                       user_id):
    """§8.6.2 — they cannot be equipped or spent during the run."""
    node = _node_of_type(db, run_id, map_gen.COMBAT)
    _enter(db, run_id, node)
    started = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                 node_index=node["node_index"])
    _win_battle(db, run_id, started["battle_id"])

    class AlwaysDrop(JournaledRng):
        def chance(self, op_key, probability):
            return True

    nodes.conclude_battle(db, balance, AlwaysDrop(db, run_id, 1), run_id=run_id,
                          battle_id=started["battle_id"])

    assert db.query("SELECT * FROM run_inventory WHERE run_id = ?", (run_id,))
    assert db.one("SELECT COUNT(*) AS n FROM owned_equipment WHERE user_id = ?",
                  (user_id,))["n"] == 0


def test_a_tutorial_defeat_retries_the_node_instead_of_ending_the_run(db, balance,
                                                                      rng, run_id):
    """§3.4.1 — defeat does not end the tutorial run; the party is restored."""
    node = _node_of_type(db, run_id, map_gen.COMBAT)
    _enter(db, run_id, node)
    started = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                 node_index=node["node_index"])
    _lose_battle(db, run_id, started["battle_id"])

    result = nodes.conclude_battle(db, balance, rng, run_id=run_id,
                                   battle_id=started["battle_id"])
    assert result["tutorial_retry"] is True
    assert result["next_attempt"] == 2
    # Resolution runs straight through: no component is rendered for
    # `node_resolution`, so parking there would leave nothing to click.
    assert result["screen"] == "battle"
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.BATTLE

    member = db.one("SELECT * FROM run_characters WHERE run_id = ?", (run_id,))
    assert member["hp_current"] == member["hp_max"]
    # §3.4.2 — a retry inserts a NEW battle row at attempt_no + 1; the previous
    # row is retained for telemetry and never destructively reset.
    assert db.one("SELECT state FROM battles WHERE battle_id = ?",
                  (started["battle_id"],))["state"] == bt.BATTLE_LOST
    retry = db.one("SELECT * FROM battles WHERE battle_id = ?",
                   (result["battle_id"],))
    assert retry["attempt_no"] == 2
    assert retry["node_index"] == started_node_index(db, started["battle_id"])
    assert retry["state"] == bt.BATTLE_ACTIVE


def started_node_index(db, battle_id: int) -> int:
    return db.one("SELECT node_index FROM battles WHERE battle_id = ?",
                  (battle_id,))["node_index"]


def test_a_main_campaign_defeat_settles_the_run(db, balance, version, user_id):
    from app.db.connection import utcnow

    db.execute("UPDATE accounts SET party_slots = 2, tutorial_completed_at = ? "
               "WHERE user_id = ?", (utcnow(), user_id))
    db.execute("INSERT OR IGNORE INTO owned_characters (user_id, character_id, "
               "star_rank, acquired_at) VALUES (?, 'char_ignis', 2, ?)",
               (user_id, utcnow()))
    db.execute("INSERT OR IGNORE INTO world_unlocks (user_id, world_id, "
               "unlocked_at) VALUES (?, ?, ?)", (user_id, WORLD_1_ID, utcnow()))

    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=WORLD_1_ID,
                           party_character_ids=[STARTER_CHARACTER_ID, "char_ignis"],
                           is_tutorial=False),
        version)
    rng = JournaledRng(db, run_id, 1)
    node = _node_of_type(db, run_id, map_gen.COMBAT)
    _enter(db, run_id, node)
    started = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                 node_index=node["node_index"])
    _lose_battle(db, run_id, started["battle_id"])

    result = nodes.conclude_battle(db, balance, rng, run_id=run_id,
                                   battle_id=started["battle_id"])
    assert result["screen"] == "settlement"
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == sl.RUN_DEFEATED


def test_a_boss_victory_always_ends_the_run_and_unlocks_the_next_world(
        db, balance, rng, run_id, user_id):
    """§3.6 — one world = one run."""
    node = _node_of_type(db, run_id, map_gen.BOSS)
    _enter(db, run_id, node)
    started = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                 node_index=node["node_index"])
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.BOSS_BATTLE
    _win_battle(db, run_id, started["battle_id"])

    result = nodes.conclude_battle(db, balance, rng, run_id=run_id,
                                   battle_id=started["battle_id"])
    assert result["cleared"] is True
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == sl.RUN_COMPLETED
    unlocked = {row["world_id"] for row in db.query(
        "SELECT world_id FROM world_unlocks WHERE user_id = ?", (user_id,))}
    assert WORLD_1_ID in unlocked


# =====================================================================
# Full traversal
# =====================================================================
def _play_to_the_end(db, balance, run_id) -> list[str]:
    """Walk a run from depth 1 to its terminal state, winning every battle."""
    rng = JournaledRng(db, run_id,
                       db.one("SELECT rng_seed FROM runs WHERE run_id = ?",
                              (run_id,))["rng_seed"])
    visited: list[str] = []
    current = None

    for _ in range(20):
        run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if run["state"] in sl.TERMINAL_STATES:
            break

        options = map_gen.available_next_nodes(db, run_id, current)
        if not options:
            break
        node = options[0]
        _enter(db, run_id, node)
        result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                    node_index=node["node_index"])
        visited.append(node["node_type"])
        current = node["node_index"]

        if result["screen"] == "battle":
            _win_battle(db, run_id, result["battle_id"])
            nodes.conclude_battle(db, balance, rng, run_id=run_id,
                                  battle_id=result["battle_id"])
        elif result["screen"] == "reward":
            nodes.choose_reward(db, run_id, choice_id=result["choice_id"],
                                card_id=None, party_slot=None)
        elif result["screen"] == "shop":
            nodes.leave_shop(db, run_id)
        elif result["screen"] == "event":
            choice = db.one(
                "SELECT choice_id FROM pending_choices WHERE run_id = ? "
                "AND status = 'open'", (run_id,))
            if choice is not None:
                outcome = nodes.choose_event_branch(
                    db, balance, rng, run_id, choice_id=choice["choice_id"],
                    branch_index=0)
                if outcome["screen"] == "battle":
                    _win_battle(db, run_id, outcome["battle_id"])
                    nodes.conclude_battle(db, balance, rng, run_id=run_id,
                                          battle_id=outcome["battle_id"])
                elif outcome.get("suspended"):
                    # A branch carrying a PENDING_CHOICE operator waits on a
                    # nested submission; abandon it and carry on to the map.
                    db.execute(
                        "UPDATE pending_choices SET status = 'cancelled' "
                        "WHERE run_id = ? AND status = 'open'", (run_id,))
                    db.execute("UPDATE runs SET state = ? WHERE run_id = ?",
                               (lc.MAP_NAVIGATION, run_id))
    return visited


def test_a_run_can_be_played_from_depth_one_to_the_boss(db, balance, run_id):
    """Every node type resolves and hands the run back to the map, so the loop
    closes all the way to `run_completed`."""
    visited = _play_to_the_end(db, balance, run_id)

    assert map_gen.BOSS in visited
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == sl.RUN_COMPLETED
    # Depth was tracked all the way down, which is what §15.10 bands read.
    assert db.one("SELECT deepest_depth_reached FROM runs WHERE run_id = ?",
                  (run_id,))["deepest_depth_reached"] >= 7


def test_the_loop_closes_across_many_generated_maps(db, balance, version):
    """Map layouts differ per run (`rng_seed` is server-random), so one
    traversal only exercises one arrangement of node types. This walks twelve
    fresh maps, which reliably covers every node type and both event shapes.
    """
    from app.content.seed import create_account

    seen: set[str] = set()
    for index in range(12):
        user_id = 900_000 + index
        create_account(db, user_id, version)
        run_id = lc.create_run(
            db, balance,
            lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                               party_character_ids=[STARTER_CHARACTER_ID],
                               is_tutorial=True),
            version)
        seen.update(_play_to_the_end(db, balance, run_id))
        assert db.one("SELECT state FROM runs WHERE run_id = ?",
                      (run_id,))["state"] == sl.RUN_COMPLETED

    # Across twelve maps every node type should have been resolved at least once.
    assert {map_gen.COMBAT, map_gen.REST, map_gen.REWARD, map_gen.EVENT,
            map_gen.SHOP, map_gen.BOSS} <= seen


# =====================================================================
# Regressions from review of the node-resolution layer
# =====================================================================
def test_a_reward_node_never_offers_a_card_nobody_can_play(db, balance, rng,
                                                           run_id):
    """`choose_reward` always rejects an empty recipient list, so in a solo
    tutorial all three options could otherwise be unpickable."""
    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    assert result["options"]
    for option in result["options"]:
        assert option["recipients"], option


def test_a_reward_node_pays_run_currency_and_rolls_its_drop_rates(db, balance,
                                                                  run_id):
    """§15.4 — 보상 node 탐험 자금 is 20-40, with the higher reward drop rates."""
    class AlwaysDrop(JournaledRng):
        def chance(self, op_key, probability):
            return True

    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, AlwaysDrop(db, run_id, 1),
                                run_id=run_id, node_index=node["node_index"])

    assert 20 <= result["run_currency"] <= 40
    assert db.one("SELECT run_currency FROM runs WHERE run_id = ?",
                  (run_id,))["run_currency"] == result["run_currency"]
    # Drops land in the run inventory, subject to §15.10 at settlement.
    assert db.query("SELECT * FROM run_inventory WHERE run_id = ?", (run_id,))


def test_reward_node_income_is_not_paid_twice_on_a_restart(db, balance, rng,
                                                           run_id):
    node = _node_of_type(db, run_id, map_gen.REWARD)
    _enter(db, run_id, node)
    nodes.resolve_node(db, balance, rng, run_id=run_id,
                       node_index=node["node_index"])
    after_first = db.one("SELECT run_currency FROM runs WHERE run_id = ?",
                         (run_id,))["run_currency"]

    _enter(db, run_id, node)
    nodes.resolve_node(db, balance, rng, run_id=run_id,
                       node_index=node["node_index"])
    assert db.one("SELECT run_currency FROM runs WHERE run_id = ?",
                  (run_id,))["run_currency"] == after_first


def test_the_shop_never_stocks_a_card_nobody_can_play(db, balance, rng, run_id):
    """An unplayable purchase would take 탐험 자금 for a card that can never be
    drawn into a legal hand (§2.10)."""
    node = _node_of_type(db, run_id, map_gen.SHOP)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    for item in result["items"]:
        payload = json.loads(item["item_ref"])
        if payload["kind"] == "card":
            assert payload["recipients"]


def test_a_bought_card_goes_to_a_legal_recipient(db, balance, rng, run_id):
    db.execute("UPDATE runs SET run_currency = 5000 WHERE run_id = ?", (run_id,))
    node = _node_of_type(db, run_id, map_gen.SHOP)
    _enter(db, run_id, node)
    result = nodes.resolve_node(db, balance, rng, run_id=run_id,
                                node_index=node["node_index"])
    card_item = next(
        (item for item in result["items"]
         if json.loads(item["item_ref"])["kind"] == "card"), None)
    if card_item is None:
        pytest.skip("this shop rolled no card listings")

    payload = json.loads(card_item["item_ref"])
    # Even an illegal requested slot is corrected to a legal one.
    nodes.buy_shop_item(db, rng, run_id, node_index=node["node_index"],
                        item_index=card_item["item_index"], party_slot=99)
    added = db.one(
        "SELECT party_slot FROM run_deck_cards WHERE run_id = ? AND card_id = ? "
        "ORDER BY card_instance_id DESC LIMIT 1", (run_id, payload["card_id"]))
    assert added["party_slot"] in payload["recipients"]


def test_a_suspended_event_branch_leaves_the_run_awaiting_a_nested_choice(
        db, balance, rng, run_id):
    """§10.4.2 — 봉인된 제단's cleanse is a PENDING_CHOICE operator, and §16.5
    permits exactly one open pending_choices row per run."""
    node = _node_of_type(db, run_id, map_gen.EVENT)
    _enter(db, run_id, node)
    db.execute("DELETE FROM pending_choices WHERE run_id = ?", (run_id,))
    _force_event(db, run_id, node, "event_봉인된제단")

    choice = db.one("SELECT choice_id FROM pending_choices WHERE run_id = ?",
                    (run_id,))
    result = nodes.choose_event_branch(db, balance, rng, run_id,
                                       choice_id=choice["choice_id"],
                                       branch_index=1)
    assert result["suspended"] is True
    assert result["operator"] == "remove_cursed_card"
    # The branch's own row closed before the nested one opened.
    open_rows = db.query(
        "SELECT * FROM pending_choices WHERE run_id = ? AND status = 'open'",
        (run_id,))
    assert len(open_rows) == 1


def test_resuming_a_cleanse_removes_the_chosen_cursed_card(db, balance, rng,
                                                           run_id):
    """§2.7.4 — with more than one cursed card present, the player picks."""
    first = deck.insert_cursed_card(db, rng, run_id, 1, "curse_고통의각인",
                                    seq="a")
    deck.insert_cursed_card(db, rng, run_id, 1, "curse_무거운사슬", seq="b")

    node = _node_of_type(db, run_id, map_gen.EVENT)
    _enter(db, run_id, node)
    db.execute("DELETE FROM pending_choices WHERE run_id = ?", (run_id,))
    _force_event(db, run_id, node, "event_봉인된제단")
    choice = db.one("SELECT choice_id FROM pending_choices WHERE run_id = ?",
                    (run_id,))
    nodes.choose_event_branch(db, balance, rng, run_id,
                              choice_id=choice["choice_id"], branch_index=1)

    nodes.resume_pending_choice(db, run_id, selection=first)

    remaining = deck.cursed_cards_in_deck(db, run_id)
    assert len(remaining) == 1
    assert remaining[0]["card_instance_id"] != first
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.MAP_NAVIGATION
    assert not db.query(
        "SELECT * FROM pending_choices WHERE run_id = ? AND status = 'open'",
        (run_id,))


def test_a_cleanse_cannot_remove_a_card_from_another_run(db, balance, rng, run_id):
    node = _node_of_type(db, run_id, map_gen.EVENT)
    _enter(db, run_id, node)
    db.execute("DELETE FROM pending_choices WHERE run_id = ?", (run_id,))
    _force_event(db, run_id, node, "event_봉인된제단")
    choice = db.one("SELECT choice_id FROM pending_choices WHERE run_id = ?",
                    (run_id,))
    nodes.choose_event_branch(db, balance, rng, run_id,
                              choice_id=choice["choice_id"], branch_index=1)

    with pytest.raises(nodes.NodeError, match="not in this run"):
        nodes.resume_pending_choice(db, run_id, selection=999_999)
