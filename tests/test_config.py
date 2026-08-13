"""`config/*.toml` — 설정 파일이 실제로 게임을 움직이는지 확인한다.

파일이 잘 읽히는지만 보는 게 아니라, 값을 바꿨을 때 동작이 따라 바뀌는지를
본다. 그래야 "설정 가능하다"는 말이 실제로 참이 된다.
"""

from __future__ import annotations

import pytest

from app.content import config_loader as cl
from app.content.balance import DEFAULT_CONSTANTS, Balance, describe
from app.engine import card_upgrades as cu
from app.engine import map_gen


# =====================================================================
# 파일 자체
# =====================================================================
def test_every_setting_carries_an_explanation():
    """코드를 몰라도 무엇을 바꾸는 값인지 알 수 있어야 한다."""
    assert cl.undocumented() == []


def test_the_settings_are_split_across_gameplay_areas():
    """한 파일에 전부 몰아넣지 않는다 — 만지려는 부분만 열어볼 수 있어야 한다."""
    names = [path.name for path in cl.config_files()]
    assert len(names) >= 5
    origins = set(cl.source_files().values())
    assert len(origins) == len(names), "값이 하나도 없는 설정 파일이 있습니다"


def test_a_setting_defined_in_two_files_is_refused(tmp_path, monkeypatch):
    """어느 쪽이 이겼는지 알 수 없는 상태로 조용히 넘어가면 안 된다."""
    (tmp_path / "a.toml").write_text("# 설명\ndraws_per_turn = 3\n", encoding="utf-8")
    (tmp_path / "b.toml").write_text("# 설명\ndraws_per_turn = 9\n", encoding="utf-8")
    monkeypatch.setenv("DECKOUT_CONFIG_DIR", str(tmp_path))
    with pytest.raises(cl.ConfigError, match="한 곳에만"):
        cl.load_constants()


def test_an_empty_config_directory_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DECKOUT_CONFIG_DIR", str(tmp_path))
    with pytest.raises(cl.ConfigError):
        cl.load_constants()


def test_the_none_sentinel_becomes_a_real_empty_value():
    """TOML에는 빈 값이 없으므로 "없음"으로 적고, 읽을 때 None이 된다."""
    burn = DEFAULT_CONSTANTS["status_magnitudes"]["화상"]
    assert burn["duration"] is None
    assert burn["stack_cap"] is None
    assert burn["magnitude"] == 3


def test_a_separator_comment_is_not_mistaken_for_an_explanation():
    docs = cl.parse_docs(
        "# ─────────────\n"
        "#  칸막이\n"
        "# ─────────────\n"
        "\n"
        "# 진짜 설명입니다.\n"
        "draws_per_turn = 3\n"
    )
    assert docs["draws_per_turn"] == "진짜 설명입니다."


def test_an_explanation_reaches_the_database(db, version):
    """대시보드가 설정 옆에 그대로 띄울 수 있어야 한다."""
    text = describe(db, "draws_per_turn")
    assert text and "뽑는 카드 수" in text
    row = db.one("SELECT source_file FROM balancing_metadata WHERE key = ?",
                 ("gacha_base_rates",))
    assert row["source_file"] == "04_뽑기.toml"


# =====================================================================
# 값이 실제로 동작을 바꾸는가
# =====================================================================
def _override(db, version, key: str, value_json: str) -> Balance:
    db.execute("UPDATE balancing_constants SET value_json = ? "
               "WHERE content_version_id = ? AND key = ?", (value_json, version, key))
    return Balance(db, version)


def test_the_shop_cannot_appear_before_its_configured_depth(db, version):
    """`map_shop_min_depth` 를 올리면 상점이 더 깊은 곳에서만 나온다."""
    balance = _override(db, version, "map_shop_min_depth", "5")
    rules = map_gen.MapRules.from_balance(balance)
    nodes = [{"node_index": 0, "depth": 4, "node_type": map_gen.SHOP}]
    assert not map_gen._placement_ok(nodes, 7, rules)
    nodes[0]["depth"] = 5
    assert map_gen._placement_ok(nodes, 7, rules)


def test_rest_nodes_keep_the_configured_distance(db, version):
    balance = _override(db, version, "map_rest_min_depth_gap", "3")
    rules = map_gen.MapRules.from_balance(balance)
    nodes = [{"node_index": 0, "depth": 2, "node_type": map_gen.REST},
             {"node_index": 1, "depth": 4, "node_type": map_gen.REST}]
    assert not map_gen._placement_ok(nodes, 7, rules)
    nodes[1]["depth"] = 5
    assert map_gen._placement_ok(nodes, 7, rules)


def test_a_generated_map_still_obeys_the_default_rules(balance):
    """기본값으로 만든 지도는 설정한 규칙을 그대로 지킨다."""
    rules = map_gen.MapRules.from_balance(balance)

    class _Rng:
        def draw(self, key, fn):
            import random
            return fn(random.Random(7))

    generated = map_gen.generate_map(_Rng(), balance)
    for node in generated.nodes:
        if node["node_type"] == map_gen.SHOP:
            assert node["depth"] >= rules.shop_min_depth
    rest_depths = sorted(node["depth"] for node in generated.nodes
                         if node["node_type"] == map_gen.REST)
    for earlier, later in zip(rest_depths, rest_depths[1:]):
        assert later - earlier >= rules.rest_min_depth_gap


def test_the_upgrade_gate_follows_the_configured_scopes(db, version):
    """`card_upgrade_scopes_by_tier` 가 전이별 능력 추가를 결정한다."""
    balance = Balance(db, version)
    assert cu.rules(balance).allowed_scopes(1) == ()
    assert "enemy_only" not in cu.rules(balance).allowed_scopes(2)

    balance = _override(db, version, "card_upgrade_scopes_by_tier",
                        '{"1": ["universal"], "2": [], "3": [], "4": [], "5": []}')
    assert cu.rules(balance).allowed_scopes(1) == ("universal",)


def test_the_reward_rarity_band_cuts_leave_no_gap(balance):
    """low/mid 경계가 이어져 있어야 모든 희귀도가 어느 한 묶음에 속한다."""
    cuts = balance.get("reward_rarity_band_max_tier")
    assert 1 <= int(cuts["low"]) < int(cuts["mid"])
    # 묶음 이름은 확률표(reward_rarity_weights)와 정확히 맞아야 한다.
    assert set(balance.get("reward_rarity_weights")) == {"low", "mid", "high"}


def test_the_gacha_bands_read_their_contents_from_the_config(balance):
    """어느 희귀도도 빠짐없이 어느 한 등급에는 들어 있어야 한다.

    빠진 희귀도의 카드는 영영 뽑히지 않는다.
    """
    tiers = balance.get("gacha_band_rarity_tiers")
    covered = {tier for band in tiers.values() for tier in band}
    assert covered == {1, 2, 3, 4, 5, 6}

    stars = balance.get("gacha_band_star_rank")
    assert stars["top"] > stars["mid"] > stars["base"]


def test_the_runaway_turn_guard_is_configurable(balance):
    assert int(balance.get("battle_max_turns_per_advance")) == 64


# =====================================================================
# 설정끼리 어긋나면 안 되는 것들
# =====================================================================
def test_the_node_quota_matches_the_depth_structure(balance):
    """칸 종류별 개수의 합이 지도의 칸 수와 다르면 지도를 만들 수 없다."""
    quota = balance.get("map_node_quota")
    assert sum(quota.values()) == int(balance.get("map_node_count"))
    assert sum(balance.get("map_depth_structure")) == int(balance.get("map_node_count"))


def test_the_starter_deck_composition_fills_the_deck_exactly(balance):
    composition = balance.get("starter_deck_composition")
    assert sum(composition.values()) == int(balance.get("base_deck_size"))


def test_the_gacha_rates_add_up_to_one(balance):
    rates = balance.get("gacha_base_rates")
    assert abs(sum(rates.values()) - 1.0) < 1e-9
    weights = balance.get("reward_rarity_weights")
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_the_wildcard_threshold_matches_the_cost_table(balance):
    """와일드카드가 드는 시점과 비용표가 어긋나면 콘텐츠 검사가 막힌다."""
    threshold = int(balance.get("card_upgrade_wildcard_from_tier"))
    for tier, cost in balance.get("card_upgrade_costs").items():
        if int(tier) < threshold:
            assert cost["wildcards"] == 0, f"T{tier}에 와일드카드가 있습니다"
        else:
            assert cost["wildcards"] > 0, f"T{tier}에 와일드카드가 없습니다"


def test_the_tutorial_grants_enough_to_reach_the_main_campaign(balance):
    """튜토리얼 보상이 부족하면 처음 하는 사람이 본편에 들어갈 수 없다."""
    assert (int(balance.get("tutorial_reward_carta"))
            >= int(balance.get("gacha_cost_single")))
    assert int(balance.get("tutorial_reward_party_slot")) >= 2


def test_no_card_can_cost_more_than_a_turn_provides(balance):
    """낼 수 없는 카드가 만들어질 수 있는 설정이면 안 된다."""
    pool = balance.get("resource_pool_by_party_size")
    assert int(balance.get("card_cost_max")) <= max(int(v) for v in pool.values())
    assert int(balance.get("card_cost_min")) >= 1


def test_the_equipment_drop_table_covers_every_depth(balance):
    """마지막 줄이 어떤 깊이든 받아내야 한다 — 아니면 등급이 정해지지 않는다."""
    table = balance.get("equipment_drop_tier_by_depth")
    deepest = len(balance.get("map_depth_structure")) + 1
    assert int(table[-1]["max_depth"]) >= deepest
    assert [int(row["max_depth"]) for row in table] == sorted(
        int(row["max_depth"]) for row in table)


def test_the_retention_bands_are_ordered_by_depth(balance):
    bands = balance.get("retention_bands")
    depths = [int(band["max_depth"]) for band in bands]
    assert depths == sorted(depths)
