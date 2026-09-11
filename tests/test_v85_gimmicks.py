"""Deckout v8.5 — 월드 1 기믹 오버레이 회귀 테스트."""

from __future__ import annotations

import json

from app.content.v85_gimmicks import WAR_DRUM_READY, apply_if_legacy_seed


def test_v85_world1_boss_stats_and_phase_are_published(db, version):
    boss = db.one(
        "SELECT hp, atk, def, spd, action_rules_json FROM enemies "
        "WHERE content_version_id = ? AND enemy_id = 'enemy_w1_boss'",
        (version,),
    )
    assert boss is not None
    assert (boss["hp"], boss["atk"], boss["def"], boss["spd"]) == (450, 20, 8, 96)

    phase = db.one(
        "SELECT hp_threshold_pct, effect_ids_json FROM boss_phases "
        "WHERE content_version_id = ? AND enemy_id = 'enemy_w1_boss' "
        "AND phase_index = 2",
        (version,),
    )
    assert phase is not None
    assert float(phase["hp_threshold_pct"]) == 0.55
    assert json.loads(phase["effect_ids_json"]) == ["trans_w1_전쟁개시"]


def test_v85_world1_boss_rules_teach_loot_then_war_drum(db, version):
    boss = db.one(
        "SELECT action_rules_json FROM enemies WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_w1_boss'",
        (version,),
    )
    rules = json.loads(boss["action_rules_json"])
    by_action = {rule["action_id"]: rule for rule in rules}

    assert by_action["act_w1_약탈명령"]["condition"] == {
        "op": "round_number_gte", "value": 2
    }
    assert by_action["act_w1_약탈명령"]["max_phase"] == 1
    assert by_action["act_w1_전쟁북"]["condition"] == {"op": "phase_is", "value": 2}
    assert by_action["act_w1_대돌진"]["condition"] == {
        "op": "self_has_status", "value": WAR_DRUM_READY
    }


def test_v85_loot_goblin_and_war_drum_content_exist(db, version):
    looter = db.one(
        "SELECT name, hp, atk, def, spd FROM enemies "
        "WHERE content_version_id = ? AND enemy_id = 'enemy_w1_전리품꾼'",
        (version,),
    )
    assert looter is not None
    assert looter["name"] == "고블린 전리품꾼"
    assert (looter["hp"], looter["atk"], looter["def"], looter["spd"]) == (38, 6, 2, 84)

    status = db.one(
        "SELECT base_duration, cleansable FROM statuses "
        "WHERE content_version_id = ? AND status_id = ?",
        (version, WAR_DRUM_READY),
    )
    assert status is not None
    assert status["base_duration"] == 2
    assert status["cleansable"] == 0

    actions = {
        row["action_id"] for row in db.query(
            "SELECT action_id FROM enemy_actions WHERE content_version_id = ? "
            "AND action_id IN ('act_w1_약탈명령', 'act_w1_전쟁북', 'act_w1_대돌진')",
            (version,),
        )
    }
    assert actions == {"act_w1_약탈명령", "act_w1_전쟁북", "act_w1_대돌진"}


def test_v85_overlay_does_not_reapply_over_an_already_upgraded_version(db, version):
    before = db.one(
        "SELECT action_rules_json FROM enemies WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_w1_boss'",
        (version,),
    )["action_rules_json"]

    assert apply_if_legacy_seed(db, version) is False

    after = db.one(
        "SELECT action_rules_json FROM enemies WHERE content_version_id = ? "
        "AND enemy_id = 'enemy_w1_boss'",
        (version,),
    )["action_rules_json"]
    assert after == before


def test_v85_world1_normals_have_distinct_target_priority_hooks(db, version):
    expected = {
        "enemy_w1_고블린": "act_필사반격",
        "enemy_w1_방패병": "act_w1_방패엄호",
        "enemy_w1_주술사": "act_w1_점화술",
        "enemy_w1_늑대조련사": "act_w1_사냥명령",
    }
    for enemy_id, action_id in expected.items():
        row = db.one(
            "SELECT action_rules_json FROM enemies WHERE content_version_id = ? "
            "AND enemy_id = ?",
            (version, enemy_id),
        )
        actions = {rule["action_id"] for rule in json.loads(row["action_rules_json"])}
        assert action_id in actions
