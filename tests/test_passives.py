"""§6 패시브 카드 — 해금부터 전투 발동까지."""

from __future__ import annotations

import json

import pytest

from app.content import validation
from app.content.operators import ValidationError
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.db.connection import utcnow
from app.engine import battle as bt
from app.engine import encounter as enc
from app.engine import gacha
from app.engine import lifecycle as lc
from app.engine import passives as pv
from app.engine import statuses as st
from app.engine import timed_effects as te
from app.engine import units as un
from app.engine.rng import JournaledRng


def unlock(db, user_id: int, passive_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO unlocked_passives (user_id, passive_card_id, "
        "unlocked_at) VALUES (?, ?, ?)", (user_id, passive_id, utcnow()),
    )


def start_run(db, balance, version, user_id, passives: list[str]) -> int:
    return lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           passive_card_ids=passives, is_tutorial=True),
        version,
    )


def battle_with(db, balance, version, run_id: int) -> bt.BattleEngine:
    battle_id = enc.create_battle(
        db, balance, run_id=run_id, node_index=0, encounter_id="enc_tut_2",
        content_version_id=version,
    )
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    engine = bt.build_engine(db, balance, battle_id=battle_id, run_id=run_id,
                             content_version_id=version,
                             rng=JournaledRng(db, run_id, run["rng_seed"]))
    engine.start()
    return engine


# =====================================================================
# 콘텐츠
# =====================================================================
def test_the_seed_set_covers_every_rarity_tier(db, version):
    """§5.6 — 어느 등급에도 없는 패시브는 영영 뽑히지 않는다."""
    tiers = {row["rarity_tier"] for row in pv.available(db, version)}
    assert tiers == {1, 2, 3, 4, 5, 6}


def test_the_seed_set_uses_both_activation_patterns(db, version):
    """§6 — "상시" 와 "전투 중 조건부" 가 둘 다 실제로 쓰인다."""
    rows = pv.available(db, version)
    triggers = {row["trigger_event"] for row in rows}
    assert triggers == {pv.TRIGGER_BATTLE_START, pv.TRIGGER_ROUND_START}
    assert any(json.loads(row["trigger_params_json"]).get("hp_below")
               for row in rows)
    assert any(int(row["once_per_battle"]) == 1 for row in rows)


def test_seed_content_passes_validation(db, version):
    validation.validate_version(db, version)


# =====================================================================
# §10.5 — 시전자가 없다는 사실을 저장 시점에 강제한다
# =====================================================================
def _write_passive(db, version, **overrides) -> None:
    row = {
        "passive_card_id": "pas_시험", "name": "시험", "description": "",
        "rarity_tier": 3, "trigger_event": pv.TRIGGER_BATTLE_START,
        "trigger_params_json": "{}", "target_scope": pv.SCOPE_PARTY,
        "once_per_battle": 0,
        "effects_json": json.dumps(
            [{"operator": "modify_stat",
              "params": {"stat": "atk", "delta": 1, "duration_rounds": 2}}]),
    }
    row.update(overrides)
    db.execute(
        "INSERT OR REPLACE INTO passive_cards (content_version_id, "
        "passive_card_id, name, description, rarity_tier, trigger_event, "
        "trigger_params_json, effects_json, target_scope, once_per_battle, "
        "art_asset, in_gacha_pool, is_retired) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 0)",
        (version, row["passive_card_id"], row["name"], row["description"],
         row["rarity_tier"], row["trigger_event"], row["trigger_params_json"],
         row["effects_json"], row["target_scope"], row["once_per_battle"]),
    )


def test_an_operator_that_needs_a_caster_is_rejected(db, version):
    """`deal_damage` 는 시전자의 공격력으로 계산된다. 패시브에는 시전자가
    없으므로 저장할 때 막는다 — 전투 중에 이상한 값이 나오는 것이 아니라."""
    _write_passive(db, version, effects_json=json.dumps(
        [{"operator": "deal_damage", "params": {"multiplier": 1.0}}]))
    with pytest.raises(ValidationError, match="패시브에 쓸 수 없습니다"):
        validation.validate_version(db, version)


def test_an_operator_that_needs_a_hand_is_rejected(db, version):
    _write_passive(db, version, effects_json=json.dumps(
        [{"operator": "draw_cards", "params": {"count": 1}}]))
    with pytest.raises(ValidationError, match="패시브에 쓸 수 없습니다"):
        validation.validate_version(db, version)


def test_an_unknown_trigger_is_rejected(db, version):
    _write_passive(db, version, trigger_event="언젠가")
    with pytest.raises(ValidationError, match="trigger_event"):
        validation.validate_version(db, version)


def test_an_empty_effect_list_is_rejected(db, version):
    _write_passive(db, version, effects_json="[]")
    with pytest.raises(ValidationError, match="효과가 비어 있습니다"):
        validation.validate_version(db, version)


def test_a_player_only_status_cannot_be_put_on_enemies(db, version):
    """§2.5.1a — 집중은 `player_only`. 적을 대상으로 하는 패시브에 넣을 수 없다."""
    _write_passive(db, version, target_scope=pv.SCOPE_ENEMIES,
                   effects_json=json.dumps(
                       [{"operator": "apply_status",
                         "params": {"status_id": "집중", "stacks": 1}}]))
    with pytest.raises(ValidationError):
        validation.validate_version(db, version)


# =====================================================================
# §6 획득 — 뽑아야 고를 수 있다
# =====================================================================
def test_a_passive_that_was_never_pulled_cannot_be_selected(db, version, user_id):
    assert lc.selectable_passives(db, user_id, version) == []


def test_a_pulled_passive_becomes_selectable(db, version, user_id):
    unlock(db, user_id, "pas_예리함")
    ids = [row["passive_card_id"]
           for row in lc.selectable_passives(db, user_id, version)]
    assert ids == ["pas_예리함"]


def test_starting_a_run_with_an_unowned_passive_is_refused(db, balance, version,
                                                           user_id):
    """위조된 `custom_id` 로도 남의 패시브를 들고 들어갈 수 없다."""
    with pytest.raises(lc.LifecycleError, match="not available"):
        start_run(db, balance, version, user_id, ["pas_적진교란"])


def test_more_passives_than_slots_is_refused(db, balance, version, user_id):
    for passive_id in ("pas_예리함", "pas_굳은가죽", "pas_경보"):
        unlock(db, user_id, passive_id)
    # 기본 슬롯은 2 (§6), 연구로만 늘어난다 (§9).
    with pytest.raises(lc.LifecycleError, match="too many passives"):
        start_run(db, balance, version, user_id,
                  ["pas_예리함", "pas_굳은가죽", "pas_경보"])


def test_the_same_passive_cannot_fill_two_slots(db, balance, version, user_id):
    unlock(db, user_id, "pas_예리함")
    with pytest.raises(lc.LifecycleError, match="two slots"):
        start_run(db, balance, version, user_id, ["pas_예리함", "pas_예리함"])


def test_the_gacha_can_unlock_a_passive(db, balance, version, user_id):
    """§6 — 패시브는 가챠로만 해금된다. 카르타를 넉넉히 주고 여러 번 뽑아
    적어도 하나는 패시브가 나오는지 본다."""
    db.execute("UPDATE accounts SET carta = 100000 WHERE user_id = ?", (user_id,))
    for index in range(12):
        gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                   pull_kind="ten", content_version_id=version,
                   gacha_id=f"test-passive-{index}")
        if db.one("SELECT 1 FROM unlocked_passives WHERE user_id = ?", (user_id,)):
            break
    else:
        pytest.fail("120번을 뽑고도 패시브가 한 장도 나오지 않았다")


def test_turning_the_passive_share_off_stops_them_dropping(db, balance, version,
                                                           user_id):
    db.execute(
        "UPDATE balancing_constants SET value_json = '0' WHERE content_version_id = ? "
        "AND key = 'gacha_passive_share_of_cards'", (version,),
    )
    fresh = type(balance)(db, version)
    db.execute("UPDATE accounts SET carta = 100000 WHERE user_id = ?", (user_id,))
    for index in range(12):
        gacha.pull(db, fresh, user_id=user_id, banner_id="banner_standard",
                   pull_kind="ten", content_version_id=version,
                   gacha_id=f"test-noshare-{index}")
    assert db.one("SELECT 1 FROM unlocked_passives WHERE user_id = ?",
                  (user_id,)) is None


def test_a_duplicate_passive_pays_out_wildcards(db, balance, version, user_id):
    """패시브에는 업그레이드가 없어서 조각을 줄 곳이 없다."""
    unlock(db, user_id, "pas_적진교란")
    before = db.one("SELECT wildcards FROM accounts WHERE user_id = ?",
                    (user_id,))["wildcards"]
    with db.tx() as conn:
        result = gacha._grant(conn, balance, user_id, "passive", "pas_적진교란",
                              "top", version, won_5050=None)
    after = db.one("SELECT wildcards FROM accounts WHERE user_id = ?",
                   (user_id,))["wildcards"]
    assert result.is_duplicate
    assert after - before == result.wildcards > 0


# =====================================================================
# 전투 발동
# =====================================================================
def test_an_always_on_passive_raises_the_stat_for_the_whole_battle(
        db, balance, version, user_id):
    unlock(db, user_id, "pas_굳은가죽")
    run_id = start_run(db, balance, version, user_id, ["pas_굳은가죽"])
    engine = battle_with(db, balance, version, run_id)

    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    assert un.effective_def(db, ally) == ally.base_def + 2

    # 라운드 경계를 몇 번 넘겨도 꺼지지 않는다 — 이것이 `BATTLE_LONG` 의 요점이다.
    for round_no in range(1, 6):
        te.expire_round(db, engine.battle_id, round_no)
    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    assert un.effective_def(db, ally) == ally.base_def + 2


def test_a_round_duration_modifier_still_expires(db, balance, version, user_id):
    """-1 을 도입해도 보통의 지속시간은 종전대로 만료된다."""
    run_id = start_run(db, balance, version, user_id, [])
    engine = battle_with(db, balance, version, run_id)
    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    te.create_stat_modifier(db, engine.battle_id, ally.battle_unit_id,
                            stat="def", delta=5, is_percent=False,
                            current_round=1, duration_rounds=1)
    assert un.effective_def(db, ally) == ally.base_def + 5
    te.expire_round(db, engine.battle_id, 1)
    assert un.effective_def(db, ally) == ally.base_def + 5      # 만든 라운드는 살아남는다
    te.expire_round(db, engine.battle_id, 2)
    assert un.effective_def(db, ally) == ally.base_def


def test_an_enemy_scoped_passive_debuffs_the_enemies(db, balance, version, user_id):
    unlock(db, user_id, "pas_적진교란")
    run_id = start_run(db, balance, version, user_id, ["pas_적진교란"])
    engine = battle_with(db, balance, version, run_id)

    enemies = un.load_units(db, engine.battle_id, side=un.ENEMY)
    assert enemies
    for enemy in enemies:
        stacks = st.stacks_of(db, enemy.battle_unit_id, st.DEFENSE_DOWN)
        assert stacks == 2
    allies = un.load_units(db, engine.battle_id, side=un.ALLY)
    assert st.stacks_of(db, allies[0].battle_unit_id, st.DEFENSE_DOWN) == 0


def test_a_conditional_passive_waits_for_its_condition(db, balance, version,
                                                       user_id):
    """`hp_below` 가 걸리기 전에는 발동하지 않는다."""
    unlock(db, user_id, "pas_역전의호흡")
    run_id = start_run(db, balance, version, user_id, ["pas_역전의호흡"])
    engine = battle_with(db, balance, version, run_id)

    fired = db.one("SELECT fired_count FROM battle_passives WHERE battle_id = ?",
                   (engine.battle_id,))
    assert fired["fired_count"] == 0

    assert pv.fire(engine, pv.TRIGGER_ROUND_START) == []


def test_a_conditional_passive_fires_once_and_only_once(db, balance, version,
                                                        user_id):
    unlock(db, user_id, "pas_역전의호흡")
    run_id = start_run(db, balance, version, user_id, ["pas_역전의호흡"])
    engine = battle_with(db, balance, version, run_id)

    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    db.execute("UPDATE battle_units SET hp_current = ? WHERE battle_unit_id = ?",
               (max(1, ally.hp_max // 5), ally.battle_unit_id))

    assert pv.fire(engine, pv.TRIGGER_ROUND_START)      # 조건이 걸렸다
    assert pv.fire(engine, pv.TRIGGER_ROUND_START) == []  # 전투당 1회
    fired = db.one("SELECT fired_count FROM battle_passives WHERE battle_id = ?",
                   (engine.battle_id,))
    assert fired["fired_count"] == 1


def test_the_battle_keeps_the_passives_it_started_with(db, balance, version,
                                                       user_id):
    """§16.2.3 과 같은 이유 — 전투 중에 런의 장착이 바뀌어도 이 전투는 그대로."""
    unlock(db, user_id, "pas_전열정비")
    run_id = start_run(db, balance, version, user_id, ["pas_전열정비"])
    engine = battle_with(db, balance, version, run_id)

    db.execute("DELETE FROM run_passives WHERE run_id = ?", (run_id,))
    equipped = pv.equipped(db, engine.battle_id, version)
    assert [row["passive_card_id"] for row in equipped] == ["pas_전열정비"]


def test_no_passives_means_no_change_to_the_battle(db, balance, version, user_id):
    run_id = start_run(db, balance, version, user_id, [])
    engine = battle_with(db, balance, version, run_id)
    assert pv.equipped(db, engine.battle_id, version) == []
    assert pv.fire(engine, pv.TRIGGER_BATTLE_START) == []


def test_a_round_start_passive_fires_at_each_boundary(db, balance, version,
                                                      user_id):
    unlock(db, user_id, "pas_전열정비")
    run_id = start_run(db, balance, version, user_id, ["pas_전열정비"])
    engine = battle_with(db, balance, version, run_id)

    ally = un.load_units(db, engine.battle_id, side=un.ALLY)[0]
    un.set_block(db, ally.battle_unit_id, 0)
    assert pv.fire(engine, pv.TRIGGER_ROUND_START)
    first = un.load_unit(db, ally.battle_unit_id).block
    assert first > 0
    assert pv.fire(engine, pv.TRIGGER_ROUND_START)
    assert un.load_unit(db, ally.battle_unit_id).block > first
