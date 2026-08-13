"""§5.8 카드 업그레이드와 §2.5.1a 상태 scope — v6.4에서 확정된 P-1."""

from __future__ import annotations

import json

import pytest

from app.central import transactions as tx
from app.content import validation
from app.content.operators import ValidationError
from app.content.seed import (CARD_STARTER_SKILL, STARTER_CHARACTER_ID,
                              TUTORIAL_WORLD_ID)
from app.db.connection import EXPECTED_SCHEMA_VERSION, Database, utcnow
from app.engine import battle as bt
from app.engine import card_upgrades as cu
from app.engine import encounter as enc
from app.engine import lifecycle as lc
from app.engine import progression as pg
from app.engine import statuses as st
from app.engine import units as un
from app.engine.rng import JournaledRng
from tests.test_progression import FakeCentral


@pytest.fixture
def central() -> FakeCentral:
    return FakeCentral()


# =====================================================================
# §18.9 스키마 마이그레이션
# =====================================================================
def test_the_scope_column_is_added_to_an_existing_database(tmp_path):
    """§18.9 — 마이그레이션은 전진 전용이며 한 트랜잭션씩 적용되고 기록된다."""
    path = tmp_path / "legacy.db"
    legacy = Database(path)
    legacy.migrate()
    # v1 상태로 되돌린다: scope 컬럼이 없던 시절의 파일을 흉내낸다.
    legacy.execute("UPDATE schema_version SET version = 1 WHERE id = 1")
    legacy.close()

    reopened = Database(path)
    assert reopened.migrate() == EXPECTED_SCHEMA_VERSION
    columns = {row[1] for row in reopened.query("PRAGMA table_info(statuses)")}
    assert "scope" in columns
    assert reopened.one("SELECT version FROM schema_version WHERE id = 1")["version"] \
        == EXPECTED_SCHEMA_VERSION


def test_migrating_twice_is_harmless(tmp_path):
    path = tmp_path / "twice.db"
    first = Database(path)
    first.migrate()
    first.execute("UPDATE schema_version SET version = 1 WHERE id = 1")
    first.close()

    for _ in range(2):
        again = Database(path)
        again.migrate()
        again.close()
    final = Database(path)
    assert final.one("SELECT version FROM schema_version WHERE id = 1")["version"] \
        == EXPECTED_SCHEMA_VERSION


def test_a_newer_file_than_the_code_fails_closed(tmp_path):
    from app.db.connection import SchemaVersionError

    path = tmp_path / "future.db"
    database = Database(path)
    database.migrate()
    database.execute("UPDATE schema_version SET version = 999 WHERE id = 1")
    database.close()

    with pytest.raises(SchemaVersionError, match="refusing to start"):
        Database(path).migrate()


# =====================================================================
# §2.5.1a 상태 scope
# =====================================================================
def test_the_base_ten_statuses_are_all_universal(db, version):
    """§2.5.1a — 기본 10개는 전부 `universal`."""
    base = (st.BURN, st.BLEED, st.STUN, st.SILENCE, st.SHIELD_PIERCE, st.TAUNT,
            st.DEFENSE_DOWN, st.ATTACK_UP, st.HEAL_DOWN, st.SPEED_DOWN)
    for status_id in base:
        row = db.one(
            "SELECT scope FROM statuses WHERE content_version_id = ? AND status_id = ?",
            (version, status_id))
        assert row["scope"] == st.UNIVERSAL, status_id


def test_a_player_only_status_is_excluded_from_enemy_action_pools(db, version):
    """카드 업그레이드가 새 상태를 저작할 수 있으므로, 거버넌스 규칙이 없으면
    모든 새 상태가 조용히 적 AI가 걸 수 있는 것이 되어 버린다."""
    db.execute(
        "UPDATE enemy_actions SET effects_json = ? WHERE content_version_id = ? "
        "AND action_id = 'act_화상부여'",
        (json.dumps([{"operator": "apply_status",
                      "params": {"status_id": "집중", "stacks": 1}}]), version))
    with pytest.raises(ValidationError, match="player_only"):
        validation.validate_version(db, version)


def test_a_player_only_status_is_excluded_from_cursed_cards(db, version):
    db.execute(
        "UPDATE cursed_cards SET penalty_json = ? WHERE content_version_id = ? "
        "AND cursed_card_id = 'curse_부식된갑주'",
        (json.dumps([{"operator": "apply_status",
                      "params": {"status_id": "집중", "stacks": 1}}]), version))
    with pytest.raises(ValidationError, match="player_only"):
        validation.validate_version(db, version)


def test_a_universal_status_stays_legal_everywhere(db, version):
    """§2.5.1a — `universal`은 양쪽 풀 모두에서 허용된다."""
    db.execute(
        "UPDATE enemy_actions SET effects_json = ? WHERE content_version_id = ? "
        "AND action_id = 'act_화상부여'",
        (json.dumps([{"operator": "apply_status",
                      "params": {"status_id": st.BURN, "stacks": 2}}]), version))
    validation.validate_version(db, version)


def test_an_unknown_scope_is_rejected(db, version):
    db.execute("UPDATE statuses SET scope = 'everyone' WHERE content_version_id = ? "
               "AND status_id = ?", (version, st.BURN))
    with pytest.raises(ValidationError, match="scope"):
        validation.validate_version(db, version)


# =====================================================================
# §5.8.1 / §5.8.2 티어와 비용
# =====================================================================
def test_there_are_five_tiers_above_the_pulled_state(balance):
    """§5.8.1 — `upgrade_tier ∈ {0..5}`, 0은 뽑은 그대로."""
    assert cu.MIN_TIER == 0
    assert cu.MAX_TIER == int(balance.get("card_upgrade_max_tier")) == 5
    assert sorted(cu.ALLOWED_SCOPES_BY_TIER) == [1, 2, 3, 4, 5]


def test_wildcards_are_spent_only_from_the_2_to_3_transition(balance):
    """§5.8.2 — 0→1과 1→2는 카드 조각 + 코인만."""
    costs = balance.get("card_upgrade_costs")
    assert costs["1"]["wildcards"] == 0
    assert costs["2"]["wildcards"] == 0
    for tier in ("3", "4", "5"):
        assert costs[tier]["wildcards"] > 0


def test_the_cost_curve_is_steep_and_back_loaded(balance):
    """§5.8.2 — 각 전이는 이전보다 확실히 비싸고, 증가폭은 2→3 이후가 더 가파르다."""
    costs = balance.get("card_upgrade_costs")
    coins = [int(costs[str(tier)]["coin"]) for tier in range(1, 6)]
    assert coins == sorted(coins)
    assert all(later > earlier for earlier, later in zip(coins, coins[1:]))

    # back-loaded: 후반 전이의 증가폭이 전반보다 크다.
    early_growth = coins[1] - coins[0]
    late_growth = coins[4] - coins[3]
    assert late_growth > early_growth


def test_a_wildcard_cost_below_the_threshold_is_rejected(db, version):
    db.execute(
        "UPDATE card_upgrades SET wildcard_cost = 1 WHERE content_version_id = ? "
        "AND card_id = ? AND target_tier = 1", (version, CARD_STARTER_SKILL))
    with pytest.raises(ValidationError, match="2→3"):
        validation.validate_version(db, version)


def test_a_missing_wildcard_cost_at_or_above_the_threshold_is_rejected(db, version):
    db.execute(
        "UPDATE card_upgrades SET wildcard_cost = 0 WHERE content_version_id = ? "
        "AND card_id = ? AND target_tier = 3", (version, CARD_STARTER_SKILL))
    with pytest.raises(ValidationError, match="2→3"):
        validation.validate_version(db, version)


def test_a_flat_cost_curve_is_rejected(db, version):
    db.execute(
        "UPDATE card_upgrades SET coin_cost = 2000 WHERE content_version_id = ? "
        "AND card_id = ?", (version, CARD_STARTER_SKILL))
    with pytest.raises(ValidationError, match="steep"):
        validation.validate_version(db, version)


def test_transitions_must_be_contiguous_from_t1(db, version):
    db.execute(
        "DELETE FROM card_upgrades WHERE content_version_id = ? AND card_id = ? "
        "AND target_tier = 2", (version, CARD_STARTER_SKILL))
    with pytest.raises(ValidationError, match="contiguous"):
        validation.validate_version(db, version)


def test_a_tier_above_five_is_rejected(db, version):
    db.execute(
        "INSERT INTO card_upgrades (content_version_id, card_id, target_tier, "
        "fragment_cost, wildcard_cost, coin_cost, effects_json) "
        "VALUES (?, ?, 6, 900, 20, 300000, '[]')", (version, CARD_STARTER_SKILL))
    with pytest.raises(ValidationError, match="target_tier"):
        validation.validate_version(db, version)


# =====================================================================
# §5.8.3 효과 규칙 — 전이별 능력 추가 게이트
# =====================================================================
def _set_overlay(db, version, tier, overlay):
    db.execute(
        "UPDATE card_upgrades SET effects_json = ? WHERE content_version_id = ? "
        "AND card_id = ? AND target_tier = ?",
        (json.dumps(overlay, ensure_ascii=False), version, CARD_STARTER_SKILL, tier))


def test_the_first_transition_takes_numeric_changes_only(db, version):
    """§5.8.3 — 0→1은 숫자 변경만, 능력 추가 없음."""
    _set_overlay(db, version, 1, [
        {"operator": "deal_damage", "params": {"multiplier": 1.9}},
        {"operator": "apply_status", "params": {"status_id": "집중"}},
    ])
    with pytest.raises(ValidationError, match="no ability addition"):
        validation.validate_version(db, version)


def test_the_middle_transitions_admit_player_only_statuses(db, version):
    """§5.8.3 — 1→2, 2→3은 `player_only` scope만."""
    _set_overlay(db, version, 2, [
        {"operator": "apply_status", "params": {"status_id": "집중"}},
    ])
    validation.validate_version(db, version)

    # universal 상태는 이 전이에서 허용되지 않는다.
    _set_overlay(db, version, 2, [
        {"operator": "apply_status", "params": {"status_id": st.BURN}},
    ])
    with pytest.raises(ValidationError, match="admits only"):
        validation.validate_version(db, version)


def test_the_late_transitions_admit_any_scope(db, version):
    """§5.8.3 — 3→4, 4→5는 모든 scope."""
    for tier in (4, 5):
        for status_id in ("집중", st.BURN):
            _set_overlay(db, version, tier, [
                {"operator": "apply_status", "params": {"status_id": status_id}},
            ])
            validation.validate_version(db, version)


def test_an_applied_duration_over_four_turns_is_rejected(db, version):
    """§5.8.3 — 지속시간은 4턴까지."""
    _set_overlay(db, version, 4, [
        {"operator": "apply_status",
         "params": {"status_id": st.DEFENSE_DOWN, "duration_override": 5}},
    ])
    with pytest.raises(ValidationError, match="4-turn cap"):
        validation.validate_version(db, version)


def test_an_applied_duration_over_the_status_own_base_is_rejected(db, version):
    """§5.8.3 — 상태 자신의 base_duration에도 묶인다. 기절은 1턴."""
    _set_overlay(db, version, 4, [
        {"operator": "apply_status",
         "params": {"status_id": st.STUN, "duration_override": 3}},
    ])
    with pytest.raises(ValidationError, match="base_duration"):
        validation.validate_version(db, version)


def test_an_undefined_status_in_an_overlay_is_rejected(db, version):
    _set_overlay(db, version, 4, [
        {"operator": "apply_status", "params": {"status_id": "없는상태"}},
    ])
    with pytest.raises(ValidationError, match="not defined"):
        validation.validate_version(db, version)


# =====================================================================
# 오버레이 해석
# =====================================================================
def _card_row(db, version, card_id=CARD_STARTER_SKILL):
    return db.one("SELECT * FROM cards WHERE content_version_id = ? AND card_id = ?",
                  (version, card_id))


def test_tier_zero_is_the_card_exactly_as_authored(db, version):
    card = cu.effective_card(db, version, _card_row(db, version), 0)
    assert card["upgrade_tier"] == 0
    assert card["cost"] == 2
    assert card["effects"] == [{"operator": "deal_damage",
                                "params": {"multiplier": 1.8}}]


def test_a_numeric_change_replaces_the_same_operator(db, version):
    """§5.8.3 — 숫자 변경은 같은 연산자의 파라미터를 대체한다."""
    card = cu.effective_card(db, version, _card_row(db, version), 1)
    damage = [e for e in card["effects"] if e["operator"] == "deal_damage"]
    assert len(damage) == 1
    assert damage[0]["params"]["multiplier"] == 1.9


def test_an_ability_addition_appends_a_new_operator(db, version):
    card = cu.effective_card(db, version, _card_row(db, version), 2)
    operators = [entry["operator"] for entry in card["effects"]]
    assert operators == ["deal_damage", "apply_status"]
    assert card["effects"][1]["params"]["status_id"] == "집중"


def test_overlays_accumulate_across_tiers(db, version):
    """오버레이는 누적된다 — T5는 T1..T5를 순서대로 밟은 결과다."""
    card = cu.effective_card(db, version, _card_row(db, version), 5)
    damage = [e for e in card["effects"] if e["operator"] == "deal_damage"]
    assert damage[0]["params"]["multiplier"] == 2.8
    # 2→3의 집중과 3→4/4→5의 화상이 모두 남아 있다.
    statuses = {e["params"]["status_id"] for e in card["effects"]
                if e["operator"] == "apply_status"}
    assert statuses == {"집중", st.BURN}


def test_a_cost_reduction_lands_on_the_cost_field_not_the_effect_list(db, version):
    """`modify_cost`를 연산자 목록에 남기면 전투 중에 실행되어 버린다."""
    card = cu.effective_card(db, version, _card_row(db, version), 5)
    assert card["cost"] == 1        # 원래 2, T5에서 −1
    assert all(entry["operator"] != "modify_cost" for entry in card["effects"])


def test_the_original_card_row_is_never_mutated(db, version):
    """§10.6 — 카드 정의는 버전 안에서 절대 변하지 않는다."""
    cu.effective_card(db, version, _card_row(db, version), 5)
    row = _card_row(db, version)
    assert json.loads(row["effects_json"]) == [
        {"operator": "deal_damage", "params": {"multiplier": 1.8}}]
    assert row["cost"] == 2


def test_an_unauthored_transition_is_skipped_not_fatal(db, version):
    """소유 데이터가 콘텐츠보다 앞설 수 있고(§10.6), 그래도 카드는 렌더링된다."""
    db.execute("DELETE FROM card_upgrades WHERE content_version_id = ? "
               "AND card_id = ? AND target_tier >= 3", (version, CARD_STARTER_SKILL))
    card = cu.effective_card(db, version, _card_row(db, version), 5)
    assert card["upgrade_tier"] == 5
    damage = [e for e in card["effects"] if e["operator"] == "deal_damage"]
    assert damage[0]["params"]["multiplier"] == 2.0     # T2까지만 적용


# =====================================================================
# 런 안에서의 반영 — §16.2.3 스냅샷 경유
# =====================================================================
@pytest.fixture
def upgraded_run(db, balance, version, user_id):
    """스타터 스킬을 T2까지 올린 계정으로 런을 만든다."""
    db.execute("UPDATE unlocked_cards SET upgrade_tier = 2 WHERE user_id = ? "
               "AND card_id = ?", (user_id, CARD_STARTER_SKILL))
    return lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)


def test_the_run_snapshot_freezes_the_upgrade_tier(db, upgraded_run, user_id):
    snapshot = db.one("SELECT card_upgrade_json FROM run_build_snapshot "
                      "WHERE run_id = ?", (upgraded_run,))
    assert json.loads(snapshot["card_upgrade_json"])[CARD_STARTER_SKILL] == 2


def test_battle_reads_the_card_at_its_snapshotted_tier(db, balance, version,
                                                       upgraded_run):
    battle_id = enc.create_battle(db, balance, run_id=upgraded_run, node_index=0,
                                  encounter_id="enc_tut_1",
                                  content_version_id=version)
    engine = bt.build_engine(db, balance, battle_id=battle_id, run_id=upgraded_run,
                             content_version_id=version,
                             rng=JournaledRng(db, upgraded_run, 1))
    card = engine.card_def(CARD_STARTER_SKILL, party_slot=1)
    assert card["upgrade_tier"] == 2
    damage = [e for e in card["effects"] if e["operator"] == "deal_damage"]
    assert damage[0]["params"]["multiplier"] == 2.0


def test_upgrading_mid_run_does_not_change_the_running_battle(db, balance, version,
                                                              upgraded_run, user_id):
    """§16.2.3 — 진행 중인 런은 스냅샷을 읽으므로 수치가 흔들리지 않는다."""
    battle_id = enc.create_battle(db, balance, run_id=upgraded_run, node_index=0,
                                  encounter_id="enc_tut_1",
                                  content_version_id=version)
    engine = bt.build_engine(db, balance, battle_id=battle_id, run_id=upgraded_run,
                             content_version_id=version,
                             rng=JournaledRng(db, upgraded_run, 1))

    db.execute("UPDATE unlocked_cards SET upgrade_tier = 5 WHERE user_id = ? "
               "AND card_id = ?", (user_id, CARD_STARTER_SKILL))

    fresh = bt.build_engine(db, balance, battle_id=battle_id, run_id=upgraded_run,
                            content_version_id=version,
                            rng=JournaledRng(db, upgraded_run, 1))
    assert fresh.card_def(CARD_STARTER_SKILL, party_slot=1)["upgrade_tier"] == 2


def test_an_upgraded_card_actually_hits_harder(db, balance, version, upgraded_run):
    """오버레이가 실제 데미지에 반영된다."""
    # 스킬이 확실히 뽑히도록 덱을 스킬로만 채운다 — 드로우 운에 기대면
    # 79.8% 확률로만 실행되는 테스트가 된다.
    db.execute("UPDATE run_deck_cards SET card_id = ? WHERE run_id = ?",
               (CARD_STARTER_SKILL, upgraded_run))

    battle_id = enc.create_battle(db, balance, run_id=upgraded_run, node_index=0,
                                  encounter_id="enc_tut_1",
                                  content_version_id=version)
    engine = bt.build_engine(db, balance, battle_id=battle_id, run_id=upgraded_run,
                             content_version_id=version,
                             rng=JournaledRng(db, upgraded_run, 1))
    engine.start()
    engine.advance()

    unit = engine.acting_unit()
    skill = next(entry for entry in engine.playable_cards(unit)
                 if entry["card"]["card_id"] == CARD_STARTER_SKILL)

    enemy = un.load_units(db, battle_id, side=un.ENEMY, living_only=True)[0]
    hp_before = enemy.hp_current
    engine.play_card(unit, skill["card_instance_id"], [enemy.battle_unit_id])

    # T0의 1.8배가 아니라 T2의 2.0배로 계산된다: floor(10 × 2.0 − 3) = 17.
    dealt = hp_before - un.load_unit(db, enemy.battle_unit_id).hp_current
    assert dealt == 17
    # 그리고 T2가 추가한 집중이 실제로 걸린다.
    assert st.has_status(db, unit.battle_unit_id, "집중")


# =====================================================================
# §17.1 카드 업그레이드 트랜잭션
# =====================================================================
def _give_card_materials(db, user_id, card_id, fragments, wildcards):
    db.execute(
        "INSERT INTO card_fragments (user_id, card_id, amount) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, card_id) DO UPDATE SET amount = ?",
        (user_id, card_id, fragments, fragments))
    db.execute("UPDATE accounts SET wildcards = ? WHERE user_id = ?",
               (wildcards, user_id))


def test_upgrading_applies_all_three_local_effects_under_one_receipt(
        db, version, user_id, central):
    """§17.1 — 성급 상승과 동일한 형태."""
    _give_card_materials(db, user_id, CARD_STARTER_SKILL, 200, 10)

    result = pg.upgrade_card(db, central, user_id=user_id,
                             card_id=CARD_STARTER_SKILL,
                             content_version_id=version)

    assert result.status == tx.COMPLETED
    assert db.one("SELECT upgrade_tier FROM unlocked_cards WHERE user_id = ? "
                  "AND card_id = ?", (user_id, CARD_STARTER_SKILL))["upgrade_tier"] == 1
    # 0→1은 카드 조각 20 + 코인 2,000, 와일드카드는 들지 않는다.
    assert db.one("SELECT amount FROM card_fragments WHERE user_id = ? "
                  "AND card_id = ?", (user_id, CARD_STARTER_SKILL))["amount"] == 180
    assert db.one("SELECT wildcards FROM accounts WHERE user_id = ?",
                  (user_id,))["wildcards"] == 10
    assert central.balance == 1_000_000 - 2000
    assert db.one("SELECT COUNT(*) AS n FROM fulfillment_receipts")["n"] == 1


def test_fragments_of_one_card_are_spendable_only_on_that_card(db, version,
                                                               user_id, central):
    """§5.8 구조적 제약 — 카드 A의 조각은 카드 A에만."""
    _give_card_materials(db, user_id, "card_수_치유", 500, 10)
    db.execute("INSERT OR IGNORE INTO unlocked_cards (user_id, card_id, "
               "upgrade_tier, unlocked_at) VALUES (?, ?, 0, ?)",
               (user_id, "card_수_치유", utcnow()))

    with pytest.raises(pg.ProgressionError, match="재화가 부족"):
        pg.upgrade_card(db, central, user_id=user_id, card_id=CARD_STARTER_SKILL,
                        content_version_id=version)


def test_a_clamped_deduction_leaves_the_tier_alone(db, version, user_id):
    """§1.3.8 — 요청과 적용액이 다르면 로컬에 지급하지 않는다."""
    _give_card_materials(db, user_id, CARD_STARTER_SKILL, 200, 10)
    poor = FakeCentral(balance=500)

    result = pg.upgrade_card(db, poor, user_id=user_id,
                             card_id=CARD_STARTER_SKILL,
                             content_version_id=version)
    assert result.status == tx.COMPENSATED
    assert db.one("SELECT upgrade_tier FROM unlocked_cards WHERE user_id = ? "
                  "AND card_id = ?", (user_id, CARD_STARTER_SKILL))["upgrade_tier"] == 0
    assert db.one("SELECT amount FROM card_fragments WHERE user_id = ? "
                  "AND card_id = ?", (user_id, CARD_STARTER_SKILL))["amount"] == 200


def test_upgrading_stops_at_the_maximum_tier(db, version, user_id, central):
    _give_card_materials(db, user_id, CARD_STARTER_SKILL, 5000, 100)
    for expected in range(1, 6):
        result = pg.upgrade_card(db, central, user_id=user_id,
                                 card_id=CARD_STARTER_SKILL,
                                 content_version_id=version)
        assert result.status == tx.COMPLETED
        assert db.one("SELECT upgrade_tier FROM unlocked_cards WHERE user_id = ? "
                      "AND card_id = ?",
                      (user_id, CARD_STARTER_SKILL))["upgrade_tier"] == expected

    with pytest.raises(pg.ProgressionError, match="최대 강화"):
        pg.upgrade_card(db, central, user_id=user_id, card_id=CARD_STARTER_SKILL,
                        content_version_id=version)


def test_a_card_with_no_authored_path_cannot_be_upgraded(db, version, user_id,
                                                          central):
    _give_card_materials(db, user_id, "card_수_치유", 500, 10)
    db.execute("INSERT OR IGNORE INTO unlocked_cards (user_id, card_id, "
               "upgrade_tier, unlocked_at) VALUES (?, ?, 0, ?)",
               (user_id, "card_수_치유", utcnow()))
    with pytest.raises(pg.ProgressionError, match="저작되어 있지 않습니다"):
        pg.upgrade_card(db, central, user_id=user_id, card_id="card_수_치유",
                        content_version_id=version)


def test_an_unlocked_card_is_required(db, version, user_id, central):
    with pytest.raises(pg.ProgressionError, match="해금하지 않은"):
        pg.upgrade_card(db, central, user_id=user_id, card_id="card_광_각성",
                        content_version_id=version)


def test_a_retried_upgrade_never_applies_twice(db, version, user_id, central):
    _give_card_materials(db, user_id, CARD_STARTER_SKILL, 200, 10)
    pg.upgrade_card(db, central, user_id=user_id, card_id=CARD_STARTER_SKILL,
                    content_version_id=version, tx_id="fixed")
    tx.run_transaction(db, central, tx_id="fixed",
                       apply_local=pg._apply_card_upgrade, kind="card_upgrade")

    assert db.one("SELECT upgrade_tier FROM unlocked_cards WHERE user_id = ? "
                  "AND card_id = ?", (user_id, CARD_STARTER_SKILL))["upgrade_tier"] == 1
    assert db.one("SELECT amount FROM card_fragments WHERE user_id = ? "
                  "AND card_id = ?", (user_id, CARD_STARTER_SKILL))["amount"] == 180


def test_recovery_applies_an_interrupted_card_upgrade(db, version, user_id, central):
    """§17.4 — 핸들러 맵에 card_upgrade가 등록되어 있어야 한다."""
    _give_card_materials(db, user_id, CARD_STARTER_SKILL, 200, 10)
    tx.create_transaction(
        db, tx_id="crashed-cardup", user_id=user_id, operation="card_upgrade",
        direction=tx.DEDUCT, expected_coin_delta=-2000,
        local_payload={"kind": "card_upgrade", "user_id": user_id,
                       "card_id": CARD_STARTER_SKILL, "from_tier": 0,
                       "fragments": 20, "wildcards": 0})
    db.execute(
        "UPDATE purchase_transactions SET status = ?, central_status = ?, "
        "coin_applied_delta = -2000 WHERE tx_id = 'crashed-cardup'",
        (tx.COIN_DEDUCTED, tx.APPLIED))

    results = tx.resume_pending(db, central, pg.local_handlers())
    assert [r.status for r in results] == [tx.COMPLETED]
    assert db.one("SELECT upgrade_tier FROM unlocked_cards WHERE user_id = ? "
                  "AND card_id = ?", (user_id, CARD_STARTER_SKILL))["upgrade_tier"] == 1


# =====================================================================
# 카드 업그레이드 리뷰에서 나온 회귀
# =====================================================================
def test_the_same_status_is_replaced_across_tiers_not_duplicated(db, version):
    """오버레이는 *누적*이지 *중복*이 아니다. 덧붙이면 T5 카드가 같은 상태를
    두 번 걸어 스택이 의도의 두 배가 된다."""
    card = cu.effective_card(db, version, _card_row(db, version), 5)
    applied = [entry for entry in card["effects"]
               if entry["operator"] == "apply_status"]
    by_status = [entry["params"]["status_id"] for entry in applied]
    assert len(by_status) == len(set(by_status)), by_status

    # 각 상태는 마지막으로 저작된 값을 갖는다.
    focus = next(e for e in applied if e["params"]["status_id"] == "집중")
    burn = next(e for e in applied if e["params"]["status_id"] == st.BURN)
    assert focus["params"]["stacks"] == 2      # T3에서 갱신된 값
    assert burn["params"]["stacks"] == 3       # T5에서 갱신된 값


def test_two_different_statuses_both_survive(db, version):
    """서로 다른 두 상태를 거는 것은 정당하다 — 대체 규칙이 그것까지
    삼켜서는 안 된다."""
    card = cu.effective_card(db, version, _card_row(db, version), 5)
    statuses = {entry["params"]["status_id"] for entry in card["effects"]
                if entry["operator"] == "apply_status"}
    assert statuses == {"집중", st.BURN}


def test_modify_cost_is_rejected_on_a_card_s_own_effect_list(db, version):
    """실행기에 핸들러가 없으므로 전투 도중 터진다."""
    db.execute(
        "UPDATE cards SET effects_json = ? WHERE content_version_id = ? "
        "AND card_id = ?",
        (json.dumps([{"operator": "deal_damage", "params": {"multiplier": 1.8}},
                     {"operator": "modify_cost", "params": {"delta": -1}}]),
         version, CARD_STARTER_SKILL))
    with pytest.raises(ValidationError, match="modify_cost"):
        validation.validate_version(db, version)


def test_an_overlay_may_not_smuggle_in_an_arbitrary_operator(db, version):
    """§5.8.3 — 전이는 숫자 변경과 능력 추가만 담는다. 그 밖의 연산자를 넣으면
    전이별 게이트를 통째로 우회한다."""
    _set_overlay(db, version, 1, [
        {"operator": "deal_damage", "params": {"multiplier": 1.9}},
        {"operator": "draw_cards", "params": {"count": 2}},
    ])
    with pytest.raises(ValidationError, match="numeric change or an ability"):
        validation.validate_version(db, version)


def test_an_enemy_only_status_is_excluded_from_the_early_transitions(db, version):
    """§2.5.1a — `enemy_only`는 1→2, 2→3에서 배제된다. 후반 전이는 §5.8.3이
    명시적으로 세 scope를 모두 열거하므로 예외다."""
    db.execute(
        "INSERT OR REPLACE INTO statuses (content_version_id, status_id, name, "
        "kind, model, clock, stack_cap, base_duration, magnitude, cleansable, "
        "persists_through_boss_phase, icon_asset, scope) "
        "VALUES (?, '적전용', '적 전용', 'debuff', ?, ?, 3, 2, 0.1, 1, 0, NULL, ?)",
        (version, st.STACK_DURATION, st.OWNER_TURN_COUNTDOWN, st.ENEMY_ONLY))

    _set_overlay(db, version, 2, [
        {"operator": "apply_status", "params": {"status_id": "적전용"}},
    ])
    with pytest.raises(ValidationError, match="admits only"):
        validation.validate_version(db, version)


def test_the_max_tier_is_read_from_the_balancing_constants(db, balance):
    """§15 — 어떤 값도 하드코딩하지 않는다."""
    assert cu.max_tier(balance) == 5
    db.execute("UPDATE balancing_constants SET value_json = '4' "
               "WHERE key = 'card_upgrade_max_tier'")
    from app.content.balance import Balance

    fresh = Balance(db, balance.content_version_id)
    assert cu.max_tier(fresh) == 4


def test_a_migration_and_its_version_bump_commit_together(tmp_path, monkeypatch):
    """§18.9 — 다단계 마이그레이션이 절반만 적용된 채 버전이 올라가면 안 된다."""
    from app.db import connection as cn

    path = tmp_path / "halfway.db"
    database = Database(path)
    database.migrate()
    database.execute("UPDATE schema_version SET version = 1 WHERE id = 1")
    database.close()

    # 두 번째 문장이 실패하는 마이그레이션을 흉내낸다.
    monkeypatch.setitem(cn.MIGRATIONS, 2, (
        "ALTER TABLE statuses ADD COLUMN scope TEXT NOT NULL DEFAULT 'universal'",
        "ALTER TABLE nonexistent_table ADD COLUMN broken TEXT",
    ))

    broken = Database(path)
    with pytest.raises(Exception):
        broken.migrate()
    # 버전은 오르지 않았으므로 다음 기동이 다시 시도한다.
    assert broken.one("SELECT version FROM schema_version WHERE id = 1")["version"] == 1
