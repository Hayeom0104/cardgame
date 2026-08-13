"""§3.5 map generation, §8.6.3 settlement, §15.10 retention."""

from __future__ import annotations

import pytest

from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID, WORLD_1_ID
from app.db.connection import utcnow
from app.engine import lifecycle as lc
from app.engine import map_gen
from app.engine import settlement as sl
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


# =====================================================================
# §3.5 map generation
# =====================================================================
def test_map_has_the_fixed_quota_and_depth_structure(db, run_id):
    """§3.5.1/§3.5.2 — 15 nodes plus one terminal 보스, in 1/2/3/3/3/2/1."""
    nodes = db.query("SELECT * FROM run_nodes WHERE run_id = ? ORDER BY node_index",
                     (run_id,))
    playable = [node for node in nodes if node["node_type"] != map_gen.BOSS]
    assert len(playable) == 15
    assert sum(1 for node in nodes if node["node_type"] == map_gen.BOSS) == 1

    by_depth: dict[int, int] = {}
    for node in playable:
        by_depth[node["depth"]] = by_depth.get(node["depth"], 0) + 1
    assert [by_depth[d] for d in sorted(by_depth)] == [1, 2, 3, 3, 3, 2, 1]

    counts: dict[str, int] = {}
    for node in playable:
        counts[node["node_type"]] = counts.get(node["node_type"], 0) + 1
    assert counts == {"전투": 7, "이벤트": 3, "보상": 2, "휴식": 2, "상점": 1}


def test_depth_one_is_combat_and_depth_seven_is_rest(db, run_id):
    """A shop first would leave nothing to buy with; a rest before the boss is
    guaranteed."""
    first = db.one("SELECT node_type FROM run_nodes WHERE run_id = ? AND depth = 1",
                   (run_id,))
    last = db.one("SELECT node_type FROM run_nodes WHERE run_id = ? AND depth = 7",
                  (run_id,))
    assert first["node_type"] == map_gen.COMBAT
    assert last["node_type"] == map_gen.REST


def test_shop_never_sits_before_depth_three(db, run_id):
    shops = db.query("SELECT depth FROM run_nodes WHERE run_id = ? AND node_type = ?",
                     (run_id, map_gen.SHOP))
    assert all(row["depth"] >= 3 for row in shops)


def test_two_rest_nodes_never_occupy_adjacent_depths(db, run_id):
    depths = {row["depth"] for row in db.query(
        "SELECT depth FROM run_nodes WHERE run_id = ? AND node_type = ?",
        (run_id, map_gen.REST))}
    assert not any(depth + 1 in depths for depth in depths)


def test_branch_width_is_two_or_three_at_depths_one_to_four(db, run_id):
    for depth in range(1, 5):
        for node in db.query(
            "SELECT node_index FROM run_nodes WHERE run_id = ? AND depth = ?",
            (run_id, depth),
        ):
            degree = db.one(
                "SELECT COUNT(*) AS n FROM run_edges WHERE run_id = ? "
                "AND from_node_index = ?", (run_id, node["node_index"]))["n"]
            assert 2 <= degree <= 3, f"depth {depth} node {node['node_index']}"


def test_every_node_is_reachable_and_reaches_the_boss(db, run_id):
    nodes = {row["node_index"]: row for row in db.query(
        "SELECT * FROM run_nodes WHERE run_id = ?", (run_id,))}
    edges = [(row["from_node_index"], row["to_node_index"]) for row in db.query(
        "SELECT * FROM run_edges WHERE run_id = ?", (run_id,))]

    start = min(index for index, node in nodes.items() if node["depth"] == 1)
    reachable = {start}
    changed = True
    while changed:
        changed = False
        for source, target in edges:
            if source in reachable and target not in reachable:
                reachable.add(target)
                changed = True
    assert reachable == set(nodes)

    boss = max(nodes)
    reaches_boss = {boss}
    changed = True
    while changed:
        changed = False
        for source, target in edges:
            if target in reaches_boss and source not in reaches_boss:
                reaches_boss.add(source)
                changed = True
    assert reaches_boss == set(nodes)


def test_generation_is_replayed_from_the_journal(db, balance, run_id, rng):
    """§16.4 — a recovery replays the identical map rather than rolling a
    fresh one."""
    first = map_gen.generate_map(rng, balance)
    second = map_gen.generate_map(rng, balance)
    assert [node["node_type"] for node in first.nodes] == \
           [node["node_type"] for node in second.nodes]
    assert first.edges == second.edges


def test_the_fallback_template_is_valid(balance):
    """A generation failure must never block a player."""
    generated = map_gen._fallback_template(
        balance.get("map_depth_structure"),
        dict(balance.get("map_node_quota")),
        int(balance.get("map_node_shuffle_attempts")),
        map_gen.MapRules.from_balance(balance),
    )
    assert generated.used_fallback
    playable = [n for n in generated.nodes if n["node_type"] != map_gen.BOSS]
    assert len(playable) == 15
    assert all(node["node_type"] is not None for node in generated.nodes)


# =====================================================================
# §8.6.1 drop tier — world sets the band, depth picks within it
# =====================================================================
def test_world_one_is_uniformly_t0_because_its_band_has_one_value(db, version):
    for depth in range(1, 8):
        assert sl.drop_tier_for(db, version, WORLD_1_ID, depth) == 0


def test_from_world_two_onward_depth_raises_quality(db, version):
    assert sl.drop_tier_for(db, version, "world_2", 1) == 0
    assert sl.drop_tier_for(db, version, "world_2", 7) == 1
    assert sl.drop_tier_for(db, version, "world_4", 2) == 2
    assert sl.drop_tier_for(db, version, "world_4", 6) == 3


# =====================================================================
# §15.10 retention bands
# =====================================================================
@pytest.mark.parametrize("depth,retain,tier_down", [
    (1, 0.00, 0.00), (2, 0.00, 0.00),
    (3, 0.40, 0.60), (4, 0.40, 0.60),
    (5, 0.65, 0.35), (6, 0.65, 0.35),
    (7, 0.80, 0.20),
])
def test_retention_bands_match_the_table(balance, depth, retain, tier_down):
    band = sl.retention_band(balance, depth)
    assert band["retain_rate"] == retain
    assert band["tier_down_chance"] == tier_down


def test_a_boss_clear_keeps_everything(balance):
    band = sl.retention_band(balance, 3, boss_cleared=True)
    assert band["retain_rate"] == 1.0 and band["tier_down_chance"] == 0.0


# =====================================================================
# §8.6.3 settlement
# =====================================================================
def _stock_inventory(db, run_id, count=10, kind="equipment", tier=2):
    for _ in range(count):
        db.execute(
            "INSERT INTO run_inventory (run_id, kind, equipment_def_id, tier, "
            "stone_tier, amount, acquired_at_depth, acquired_at) "
            "VALUES (?, ?, 'eq_수련검', ?, ?, 1, 5, ?)",
            (run_id, kind, tier, tier if kind == "stone" else None, utcnow()),
        )


def test_a_completed_run_transfers_the_whole_inventory(db, balance, run_id, rng,
                                                       user_id):
    _stock_inventory(db, run_id, count=6)
    report = sl.settle_run_inventory(db, balance, rng, run_id=run_id,
                                     target_state=sl.RUN_COMPLETED,
                                     op_key="settle:test")
    assert len(report["kept"]) == 6
    assert len(report["lost"]) == 0
    owned = db.one("SELECT COUNT(*) AS n FROM owned_equipment WHERE user_id = ?",
                   (user_id,))
    assert owned["n"] == 6
    # Tiers unchanged.
    assert all(row["tier"] == 2 for row in db.query(
        "SELECT tier FROM owned_equipment WHERE user_id = ?", (user_id,)))


def test_a_shallow_defeat_retains_nothing(db, balance, run_id, rng, user_id):
    """포기 is treated identically to 패배 — that is what closes the
    farm-and-quit exploit."""
    db.execute("UPDATE runs SET deepest_depth_reached = 2 WHERE run_id = ?", (run_id,))
    _stock_inventory(db, run_id, count=8)
    report = sl.settle_run_inventory(db, balance, rng, run_id=run_id,
                                     target_state=sl.RUN_ABANDONED,
                                     op_key="settle:test")
    assert report["kept"] == []
    assert len(report["lost"]) == 8
    assert db.one("SELECT COUNT(*) AS n FROM owned_equipment WHERE user_id = ?",
                  (user_id,))["n"] == 0


def test_a_deep_defeat_retains_the_band_fraction(db, balance, run_id, rng):
    db.execute("UPDATE runs SET deepest_depth_reached = 7 WHERE run_id = ?", (run_id,))
    _stock_inventory(db, run_id, count=10)
    report = sl.settle_run_inventory(db, balance, rng, run_id=run_id,
                                     target_state=sl.RUN_DEFEATED,
                                     op_key="settle:test")
    # keep_count = floor(10 × 0.80) = 8, minus any destroyed by tier-down.
    assert len(report["kept"]) + len(report["destroyed_by_tier_down"]) == 8
    assert len(report["lost"]) == 2


def test_t0_equipment_that_tiers_down_is_destroyed(db, balance, run_id, rng):
    """C-05 floor rule."""
    db.execute("UPDATE runs SET deepest_depth_reached = 7 WHERE run_id = ?", (run_id,))
    _stock_inventory(db, run_id, count=10, tier=0)

    class AlwaysTierDown(JournaledRng):
        def chance(self, op_key, probability):
            return True

    forced = AlwaysTierDown(db, run_id, 1)
    report = sl.settle_run_inventory(db, balance, forced, run_id=run_id,
                                     target_state=sl.RUN_DEFEATED,
                                     op_key="settle:test")
    assert report["kept"] == []
    assert len(report["destroyed_by_tier_down"]) == 8


def test_a_t1_stone_that_tiers_down_is_destroyed(db, balance, run_id, rng):
    """There is no T0 stone (§8.4)."""
    db.execute("UPDATE runs SET deepest_depth_reached = 7 WHERE run_id = ?", (run_id,))
    _stock_inventory(db, run_id, count=10, kind="stone", tier=1)

    class AlwaysTierDown(JournaledRng):
        def chance(self, op_key, probability):
            return True

    report = sl.settle_run_inventory(db, balance, AlwaysTierDown(db, run_id, 1),
                                     run_id=run_id, target_state=sl.RUN_DEFEATED,
                                     op_key="settle:test")
    assert len(report["destroyed_by_tier_down"]) == 8


def test_admin_termination_settles_as_a_completion(db, balance, run_id, rng):
    """🟡 R-9 — operator fault is never charged to the player."""
    db.execute("UPDATE runs SET deepest_depth_reached = 1 WHERE run_id = ?", (run_id,))
    _stock_inventory(db, run_id, count=5)
    report = sl.settle_run_inventory(db, balance, rng, run_id=run_id,
                                     target_state=sl.ADMIN_TERMINATED,
                                     op_key="settle:test")
    assert len(report["kept"]) == 5


def test_the_keep_set_is_journaled_so_a_replay_keeps_the_same_items(db, balance,
                                                                    run_id, rng):
    db.execute("UPDATE runs SET deepest_depth_reached = 5 WHERE run_id = ?", (run_id,))
    _stock_inventory(db, run_id, count=10)
    entry_ids = [row["entry_id"] for row in db.query(
        "SELECT entry_id FROM run_inventory WHERE run_id = ? ORDER BY entry_id",
        (run_id,))]

    first = rng.sample("settle:keepset", entry_ids, 6)
    second = rng.sample("settle:keepset", entry_ids, 6)
    assert first == second


# =====================================================================
# §15.4 run-clear coin (C-07)
# =====================================================================
def test_run_clear_coin_matches_the_published_formula(balance):
    # 1,200 + 150 × 7 = 2,250 normally
    assert sl.run_clear_coin(balance, deepest_depth=7, first_clear=False) == 2250
    # + 5,000 on a world's first clear = 7,250
    assert sl.run_clear_coin(balance, deepest_depth=7, first_clear=True) == 7250


# =====================================================================
# §16.2.1 the settlement step machine (B-08)
# =====================================================================
def test_settlement_intent_is_written_before_any_mutation(db, run_id):
    sl.enter_settlement(db, run_id, target_state=sl.RUN_DEFEATED,
                        end_reason="파티 전멸")
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert run["state"] == "run_settlement"
    assert run["settlement_target_state"] == sl.RUN_DEFEATED
    assert run["settlement_step"] == sl.STEP_INVENTORY
    assert run["end_reason"] == "파티 전멸"
    assert run["settlement_tx_id"] == f"settle:{run_id}"


def test_settlement_advances_through_every_step_to_the_final_state(db, balance,
                                                                   version, run_id,
                                                                   rng):
    _stock_inventory(db, run_id, count=4)
    sl.enter_settlement(db, run_id, target_state=sl.RUN_COMPLETED,
                        end_reason="보스 처치")
    report = sl.advance_settlement(db, balance, rng, run_id=run_id,
                                   content_version_id=version)
    assert report["steps"] == list(sl.STEP_ORDER[:-1])
    assert report["final_state"] == sl.RUN_COMPLETED
    run = db.one("SELECT state, settlement_step FROM runs WHERE run_id = ?", (run_id,))
    assert run["state"] == sl.RUN_COMPLETED
    assert run["settlement_step"] == sl.STEP_DONE


def test_a_defeated_run_pays_no_coin_and_no_carta(db, balance, version, run_id, rng):
    carta_before = db.one("SELECT carta FROM accounts WHERE user_id = "
                          "(SELECT user_id FROM runs WHERE run_id = ?)",
                          (run_id,))["carta"]
    sl.enter_settlement(db, run_id, target_state=sl.RUN_DEFEATED, end_reason="패배")
    report = sl.advance_settlement(db, balance, rng, run_id=run_id,
                                   content_version_id=version)
    assert report["rewards"]["coin"] == 0
    assert db.one("SELECT carta FROM accounts WHERE user_id = "
                  "(SELECT user_id FROM runs WHERE run_id = ?)",
                  (run_id,))["carta"] == carta_before


def test_clearing_a_world_unlocks_the_next_one(db, balance, version, run_id, rng,
                                               user_id):
    """§3.6 — one world = one run; the next world is played as a NEW run."""
    sl.enter_settlement(db, run_id, target_state=sl.RUN_COMPLETED,
                        end_reason="보스 처치")
    sl.advance_settlement(db, balance, rng, run_id=run_id,
                          content_version_id=version)
    unlocked = {row["world_id"] for row in db.query(
        "SELECT world_id FROM world_unlocks WHERE user_id = ?", (user_id,))}
    assert WORLD_1_ID in unlocked


def test_clearing_advances_the_boss_defeated_achievement_ladder(db, balance, version,
                                                                run_id, rng, user_id):
    """§20.4 — the ladder shares one counter, and the tutorial boss counts."""
    sl.enter_settlement(db, run_id, target_state=sl.RUN_COMPLETED,
                        end_reason="보스 처치")
    sl.advance_settlement(db, balance, rng, run_id=run_id,
                          content_version_id=version)
    progress = db.one(
        "SELECT current_value, completed_at FROM achievement_progress "
        "WHERE user_id = ? AND achievement_id = 'ach_첫보스처치'", (user_id,))
    assert progress["current_value"] == 1
    assert progress["completed_at"] is not None
