"""Exact deck counts, grants, stable bosses, trials, and boss action execution."""
import json
from collections import Counter

import pytest

from app.api import screens
from app.content import seed
from app.db.connection import utcnow
from app.engine import loadouts, lifecycle as lc, boss_selection, progression as pg
from app.engine import nodes, encounter, battle, enemy_ai, units, effects, timed_effects
from app.engine.rng import JournaledRng


@pytest.fixture
def graduate(db, balance, version, user_id):
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    db.execute("INSERT INTO owned_characters(user_id,character_id,star_rank,acquired_at) "
               "VALUES(?,'char_aquel',3,?)", (user_id, utcnow()))
    loadouts.backfill(db, user_id, version)
    return user_id


def water_deck():
    return ["card_수_물결"] * 3 + ["card_수_보호막"] * 2 + ["card_수_치유"] * 2


def test_event_pool_never_sends_tutorial_party_into_campaign_combat(db, version):
    tutorial = {"content_version_id": version, "world_id": "world_tutorial"}
    campaign = {"content_version_id": version, "world_id": "world_1"}
    combat_event = [{"effects": [{"operator": "start_combat", "params": {
        "encounter_id": "enc_w1_normal_3"}}]}]
    assert not nodes._event_matches_world(db, tutorial, combat_event)
    assert nodes._event_matches_world(db, campaign, combat_event)
    assert nodes._event_matches_world(db, tutorial, [{"effects": []}])


def request(user):
    return lc.RunBuildRequest(user_id=user, world_id="world_1",
                              party_character_ids=["starter_001", "char_aquel"])


def prep(db, balance, version, user):
    screens.handle_prep(db, balance, user, "dko:prep:world", ["world_1"], version)
    return screens.handle_prep(db, balance, user, "dko:prep:party",
                               ["starter_001", "char_aquel"], version)


def test_saved_exact_counts_reach_snapshot_and_survive_party_reordering(db, balance, version, graduate):
    cards = water_deck()
    loadouts.save(db, balance, graduate, version, "char_aquel", cards)
    build = request(graduate)
    build.party_character_ids.reverse()
    rid = lc.create_run(db, balance, build, version)
    actual = Counter(r["card_id"] for r in db.query(
        "SELECT card_id FROM run_deck_cards WHERE run_id=? AND party_slot=1", (rid,)))
    assert actual == Counter(cards + [seed.CARD_BASIC_ATTACK] * 6 + [seed.CARD_BASIC_DEFENSE] * 5)
    loadouts.save(db, balance, graduate, version, "char_aquel", ["card_수_물결"] * 7)
    assert actual == Counter(r["card_id"] for r in db.query(
        "SELECT card_id FROM run_deck_cards WHERE run_id=? AND party_slot=1", (rid,)))


@pytest.mark.parametrize("cards", [
    ["card_수_물결"] * 6, ["card_수_물결"] * 8,
    [seed.CARD_BASIC_ATTACK] * 7, ["card_화_불씨"] * 7,
    ["card_수_해일"] * 7,
])
def test_invalid_counts_basic_cards_wrong_element_and_locked_cards_rejected(db, balance, version, graduate, cards):
    with pytest.raises(ValueError):
        loadouts.save(db, balance, graduate, version, "char_aquel", cards)
    build = request(graduate)
    build.deck_by_slot = {2: cards}
    with pytest.raises(lc.LifecycleError):
        lc.create_run(db, balance, build, version)
    assert db.one("SELECT COUNT(*) n FROM runs")["n"] == 0


def test_grants_preserve_upgrades_and_never_create_materials(db, balance, version, graduate):
    db.execute("UPDATE unlocked_cards SET upgrade_tier=3 WHERE user_id=? AND card_id='card_수_물결'", (graduate,))
    before = dict(db.one("SELECT carta,wildcards FROM accounts WHERE user_id=?", (graduate,)))
    for _ in range(3):
        seed.create_account(db, graduate, version)
        loadouts.backfill(db, graduate, version)
    assert db.one("SELECT upgrade_tier FROM unlocked_cards WHERE user_id=? AND card_id='card_수_물결'",
                  (graduate,))["upgrade_tier"] == 3
    assert not db.query("SELECT * FROM card_fragments WHERE user_id=?", (graduate,))
    assert dict(db.one("SELECT carta,wildcards FROM accounts WHERE user_id=?", (graduate,))) == before


def test_new_gacha_character_gets_bundle_and_duplicate_conversion_stays(db, balance, version, user_id):
    from app.engine.gacha import _grant
    with db.tx() as conn:
        first = _grant(conn, balance, user_id, "character", "char_terradon", "base", version, None)
        cards = {r["card_id"] for r in db.query("SELECT card_id FROM unlocked_cards WHERE user_id=?", (user_id,))}
        assert set(balance.get("character_starter_skills")["char_terradon"]) <= cards
        duplicate = _grant(conn, balance, user_id, "character", "char_terradon", "base", version, None)
    assert not first.is_duplicate and duplicate.is_duplicate
    assert duplicate.fragments > 0 and duplicate.wildcards > 0


def test_editor_click_path_saves_3_2_2_and_refuses_incomplete_confirm(db, balance, version, graduate):
    screen = prep(db, balance, version, graduate)
    screen = screens.handle_prep(db, balance, graduate, "dko:prep:deck_auto:1", [], version)
    screen = screens.handle_prep(db, balance, graduate, "dko:prep:deck_reset:2", [], version)
    assert next(c for c in screen["components"] if "deck_save:" in c["custom_id"])["disabled"]
    incomplete = screens.handle_prep(db, balance, graduate, "dko:prep:confirm", [], version)
    assert "정확히 7장" in incomplete["content"]
    for cid, count in Counter(water_deck()).items():
        picker = next(c for c in screen["components"] if c["type"] == "string_select")
        assert cid in {o["value"] for o in picker["options"]}
        count_screen = screens.handle_prep(db, balance, graduate, picker["custom_id"], [cid], version)
        count_control = count_screen["components"][0]
        assert str(count) in {o["value"] for o in count_control["options"]}
        screen = screens.handle_prep(db, balance, graduate, count_control["custom_id"], [str(count)], version)
        # The same selection is idempotent on redelivery.
        screen = screens.handle_prep(db, balance, graduate, count_control["custom_id"], [str(count)], version)
    save = next(c for c in screen["components"] if "deck_save:" in c["custom_id"])
    assert not save["disabled"]
    screens.handle_prep(db, balance, graduate, save["custom_id"], [], version)
    assert Counter(loadouts.saved(db, balance, graduate, version, "char_aquel")) == Counter(water_deck())
    result = screens.handle_prep(db, balance, graduate, "dko:prep:confirm", [], version)
    assert result["run_id"]


def test_every_unlocked_skill_is_reachable_by_paging(db, balance, version, graduate):
    for row in db.query("SELECT card_id FROM cards WHERE content_version_id=? AND is_retired=0", (version,)):
        db.execute("INSERT OR IGNORE INTO unlocked_cards(user_id,card_id,upgrade_tier,unlocked_at) "
                   "VALUES(?,?,0,?)", (graduate, row["card_id"], utcnow()))
    screen = prep(db, balance, version, graduate)
    seen = set()
    for _ in range(10):
        for component in screen["components"]:
            if component["type"] == "string_select":
                assert len(component["options"]) <= 25
                seen.update(o["value"] for o in component["options"])
        nxt = next((c for c in screen["components"] if c.get("label") == "다음 목록"), None)
        if not nxt:
            break
        screen = screens.handle_prep(db, balance, graduate, nxt["custom_id"], [], version)
    assert seen == {c.card_id for c in loadouts.legal_skills(db, graduate, version, "starter_001")}


def test_boss_preview_survives_cancel_then_is_consumed_on_run_start(db, balance, version, graduate, monkeypatch):
    picks = iter(["enc_w1_shaman", "enc_w1_shaman", "enc_w1_armored"])
    monkeypatch.setattr(boss_selection.secrets, "choice", lambda candidates: next(picks))
    prep(db, balance, version, graduate)
    first = boss_selection.preview(db, graduate, "world_1", version)
    screens.handle_prep(db, balance, graduate, "dko:prep:cancel", [], version)
    prep(db, balance, version, graduate)
    assert boss_selection.preview(db, graduate, "world_1", version) == first
    rid = lc.create_run(db, balance, request(graduate), version)
    run = db.one("SELECT * FROM runs WHERE run_id=?", (rid,))
    assert run["boss_encounter_id"] == first
    assert nodes._pick_encounter(db, balance, JournaledRng(db, rid, run["rng_seed"]), run, 7, "boss") == first
    assert boss_selection.preview(db, graduate, "world_1", version) == first  # repeats allowed


def test_trial_event_reaches_run_deck_without_unlocking_or_leaking_to_next_run(db, balance, version, graduate):
    rid = lc.create_run(db, balance, request(graduate), version)
    db.execute("UPDATE runs SET state='event_choice' WHERE run_id=?", (rid,))
    branch = json.loads(db.one("SELECT branches_json FROM events WHERE content_version_id=? "
                               "AND event_id='event_수상한행상'", (version,))["branches_json"])
    db.execute("INSERT INTO pending_choices(choice_id,run_id,node_index,choice_type,options_json,"
               "remaining_operators_json,operator_cursor,status,created_at) "
               "VALUES('trial',?,1,'event_branch','{}',?,0,'open',?)",
               (rid, json.dumps(branch), utcnow()))
    run = db.one("SELECT * FROM runs WHERE run_id=?", (rid,))
    result = nodes.choose_event_branch(db, balance, JournaledRng(db, rid, run["rng_seed"]),
                                       rid, choice_id="trial", branch_index=0)
    choice = db.one("SELECT * FROM pending_choices WHERE choice_id=?", (result["choice_id"],))
    offer = nodes.materialize_trial_card(db, rid, choice)
    assert offer["options"][0]["recipients"] == [1]
    nodes.choose_reward(db, rid, choice_id=choice["choice_id"], card_id="card_화_강타", party_slot=1)
    assert db.one("SELECT 1 FROM run_deck_cards WHERE run_id=? AND card_id='card_화_강타'", (rid,))
    assert not db.one("SELECT 1 FROM unlocked_cards WHERE user_id=? AND card_id='card_화_강타'", (graduate,))
    db.execute("UPDATE runs SET state='run_completed' WHERE run_id=?", (rid,))
    next_id = lc.create_run(db, balance, request(graduate), version)
    assert not db.one("SELECT 1 FROM run_deck_cards WHERE run_id=? AND card_id='card_화_강타'", (next_id,))


@pytest.mark.parametrize("suffix", ["shaman", "armored"])
def test_boss_patterns_phase_boundary_and_real_effects(db, balance, version, graduate, suffix):
    rid = lc.create_run(db, balance, request(graduate), version)
    bid = encounter.create_battle(db, balance, run_id=rid, node_index=7,
                                  encounter_id=f"enc_w1_{suffix}", content_version_id=version, is_boss=True)
    run = db.one("SELECT * FROM runs WHERE run_id=?", (rid,))
    rng = JournaledRng(db, rid, run["rng_seed"])
    engine = battle.build_engine(db, balance, battle_id=bid, run_id=rid, content_version_id=version, rng=rng)
    engine.start()
    boss = units.load_units(db, bid, side=units.ENEMY)[0]
    expected = (["act_w1_hex", "act_w1_hex_blast", "act_w1_hex_rest"] if suffix == "shaman"
                else ["act_w1_armor", "act_w1_shield_hit", "act_w1_armor_open"])
    for round_no, aid in enumerate(expected, 1):
        chosen = enemy_ai.select_action(db, engine.action_registry, actor=boss, battle_id=bid,
                                        round_no=round_no, rng=rng, rng_key=f"test:{round_no}")
        assert chosen.action_id == aid
    assert engine.telegraphs()[0]["action_id"] == expected[0]
    db.execute("UPDATE battle_units SET hp_current=? WHERE battle_unit_id=?",
               (boss.hp_max // 2 + 1, boss.battle_unit_id))
    engine._evaluate_boss_phases(round_no=1)
    assert units.load_unit(db, boss.battle_unit_id).boss_phase == 1

    db.execute("UPDATE battle_units SET hp_current=? WHERE battle_unit_id=?",
               (boss.hp_max // 2, boss.battle_unit_id))
    engine._evaluate_boss_phases(round_no=1)
    engine._evaluate_boss_phases(round_no=1)
    boss = units.load_unit(db, boss.battle_unit_id)
    assert boss.boss_phase == 2
    chosen = enemy_ai.select_action(db, engine.action_registry, actor=boss, battle_id=bid,
                                    round_no=1, rng=rng, rng_key="phase2")
    assert chosen.action_id == ("act_w1_hex_all" if suffix == "shaman" else "act_w1_armor_rage")
    # Execute the actual authored effects, including vulnerability expiry.
    action = engine.action_registry.get("act_w1_armor_open" if suffix == "armored" else "act_w1_hex")
    targets = [boss] if suffix == "armored" else units.load_units(db, bid, side=units.ALLY)[:1]
    from app.engine import statuses, targeting
    ctx = effects.EffectContext(db=db, run_id=rid, battle_id=bid, round_no=3, balance=balance,
                               status_registry=statuses.StatusRegistry(db, version),
                               strategy_registry=targeting.StrategyRegistry(db, version),
                               content_version_id=version, rng=rng, actor=boss, targets=targets,
                               host_context="enemy_action")
    effects.execute_effects(action.effects, ctx)
    if suffix == "armored":
        assert units.effective_def(db, boss) == 0
        timed_effects.expire_round(db, bid, 4)
        assert units.effective_def(db, boss) == 8
    else:
        assert statuses.has_status(db, targets[0].battle_unit_id, statuses.SPEED_DOWN)
    db.execute("UPDATE battle_units SET is_alive=0,hp_current=0,boss_phase=1 WHERE battle_unit_id=?",
               (boss.battle_unit_id,))
    engine._evaluate_boss_phases(round_no=5)
    assert units.load_unit(db, boss.battle_unit_id).boss_phase == 1


def test_additive_publish_preserves_custom_content_and_previous_snapshot(db, version):
    from app.content.versioning import create_version, copy_version, publish, fingerprint
    original = fingerprint(db, version)
    target = create_version(db, is_draft=True)
    copy_version(db, version, target)
    db.execute("UPDATE enemies SET hp=777 WHERE content_version_id=? AND enemy_id='enemy_w1_armored'", (target,))
    publish(db, target)
    assert db.one("SELECT hp FROM enemies WHERE content_version_id=? AND enemy_id='enemy_w1_armored'",
                  (target,))["hp"] == 777
    assert fingerprint(db, version) == original


def test_v8_migration_keeps_ownership_and_existing_run(db, version, balance, graduate):
    rid = lc.create_run(db, balance, request(graduate), version)
    db.execute("ALTER TABLE runs DROP COLUMN boss_encounter_id")
    db.execute("DROP TABLE character_decks")
    db.execute("DROP TABLE boss_previews")
    db.execute("UPDATE schema_version SET version=8")
    assert db.migrate() == 10
    assert db.migrate() == 10
    assert db.one("SELECT boss_encounter_id FROM runs WHERE run_id=?", (rid,))["boss_encounter_id"] is None
    assert db.one("SELECT COUNT(*) n FROM run_deck_cards WHERE run_id=?", (rid,))["n"] == 36
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id=? AND character_id='char_aquel'",
                  (graduate,))["star_rank"] == 3


def test_normal_rewards_only_offer_unlocked_cards(db, version, balance, graduate):
    rid = lc.create_run(db, balance, request(graduate), version)
    run = db.one("SELECT * FROM runs WHERE run_id=?", (rid,))
    node = db.one("SELECT * FROM run_nodes WHERE run_id=? AND node_type='보상' LIMIT 1", (rid,))
    assert node
    offer = nodes._offer_reward(db, balance, JournaledRng(db, rid, run["rng_seed"]), run, node)
    unlocked = {r["card_id"] for r in db.query("SELECT card_id FROM unlocked_cards WHERE user_id=?", (graduate,))}
    assert {o["card_id"] for o in offer["options"]} <= unlocked


def test_equipment_preview_uses_real_stats_and_wearer_name(db, version, balance, graduate):
    from app.api import handlers
    ctx = handlers.HandlerContext(db=db, balance=balance, central=None, content_version_id=version)
    db.execute("INSERT INTO owned_equipment(user_id,equipment_def_id,tier,equipped_character_id,equipped_slot) "
               "VALUES(?,'eq_수련검',1,'starter_001','무기')", (graduate,))
    screen = handlers.equipment_screen(ctx, graduate)
    assert "장착: 루야" in screen["content"] and "starter_001" not in screen["content"]
    assert "공격 5 > 6 (+1)" in screen["content"]
    assert screen["attachments"]


def test_retired_card_invalidates_saved_deck_without_changing_its_record(db, version, balance, graduate):
    loadouts.save(db, balance, graduate, version, "char_aquel", water_deck())
    db.execute("UPDATE cards SET is_retired=1 WHERE content_version_id=? AND card_id='card_수_물결'", (version,))
    assert loadouts.saved(db, balance, graduate, version, "char_aquel") is None
    assert db.one("SELECT cards_json FROM character_decks WHERE user_id=? AND character_id='char_aquel'",
                  (graduate,))
