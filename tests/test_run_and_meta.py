"""런 / 맵 / 가챠 / 연구 테스트 — 설계 문서 §3~§9 의 확정 규칙 검증."""

from __future__ import annotations

import random

import pytest

from cardgamebot import balance
from cardgamebot.db.models import (
    Card,
    NodeType,
    RunStatus,
    UserCard,
    UserCharacter,
    UserResearch,
)
from cardgamebot.game import gacha, research, run_service
from cardgamebot.game.mapgen import generate_map


def give_character(session, user, code: str, star: int = 1) -> None:
    session.add(UserCharacter(user_id=user.id, character_code=code, star=star))
    session.flush()


# ---------------------------------------------------------------------------
# §3 맵
# ---------------------------------------------------------------------------


def test_map_is_random_per_run_and_ends_in_boss():
    m1 = generate_map(seed=1)
    m2 = generate_map(seed=2)
    types1 = [n.type for n in m1.nodes.values()]
    types2 = [n.type for n in m2.nodes.values()]
    assert types1 != types2, "런마다 다른 맵이 나와야 한다"

    # 마지막 층은 보스 단독 노드
    assert len(m1.floors[-1]) == 1
    assert m1.boss_node().type == NodeType.BOSS.value
    assert m1.edges[m1.boss_node().id] == []


def test_map_has_no_dead_ends():
    for seed in range(20):
        game_map = generate_map(seed=seed)
        for depth, row in enumerate(game_map.floors[:-1]):
            reachable = {t for nid in row for t in game_map.edges[nid]}
            for nid in row:
                assert game_map.edges[nid], f"{nid} 에 진출로가 없다"
            # 다음 층의 모든 노드에 진입로가 있어야 한다
            assert set(game_map.floors[depth + 1]) <= reachable


def test_same_seed_reproduces_same_map():
    assert generate_map(seed=99).to_dict() == generate_map(seed=99).to_dict()


# ---------------------------------------------------------------------------
# §4 파티
# ---------------------------------------------------------------------------


def test_party_starts_at_one_slot(session, user):
    give_character(session, user, "aria")
    give_character(session, user, "noel")

    with pytest.raises(run_service.RunError, match="파티 슬롯"):
        run_service.start_run(session, user, ["aria", "noel"])

    run = run_service.start_run(session, user, ["aria"])
    assert len(run.party) == 1


def test_research_expands_party_slots(session, user):
    give_character(session, user, "aria")
    give_character(session, user, "noel")
    session.add(UserResearch(user_id=user.id, node_code="party_2", level=1))
    session.flush()

    bonuses = research.account_bonuses(session, user)
    assert bonuses.party_slots == 2

    run = run_service.start_run(session, user, ["aria", "noel"])
    assert len(run.party) == 2


def test_character_is_playable_with_zero_gacha_pulls(session, user):
    """§4.3 기본 카드 덕분에 가챠 0회여도 덱이 비지 않는다."""
    give_character(session, user, "ren")
    run = run_service.start_run(session, user, ["ren"])
    assert len(run.decks["ren"]) > 0


# ---------------------------------------------------------------------------
# §2.4 / §3 HP 지속과 패배 처리
# ---------------------------------------------------------------------------


def test_hp_persists_between_nodes_and_rest_heals(session, user):
    give_character(session, user, "noel")
    run = run_service.start_run(session, user, ["noel"])

    run.party[0]["hp"] = 10
    max_hp = run.party[0]["max_hp"]

    # 휴식 노드를 직접 호출해 회복 규칙만 확인한다.
    game_map = run_service.get_map(run)
    rest_node = next(iter(game_map.nodes.values()))
    rest_node.type = NodeType.REST.value
    outcome = run_service._enter_rest(session, user, run, rest_node, game_map)

    expected = 10 + int(max_hp * balance.REST_HEAL_PERCENT)
    assert run.party[0]["hp"] == min(max_hp, expected)
    assert outcome.kind == "rest"


def test_defeat_resets_the_whole_run(session, user):
    give_character(session, user, "ren")
    run = run_service.start_run(session, user, ["ren"])
    run_service.fail_run(session, run)

    assert run.status is RunStatus.FAILED
    assert run.battle is None
    assert run_service.active_run(session, user) is None


def test_only_one_active_run_at_a_time(session, user):
    give_character(session, user, "ren")
    run_service.start_run(session, user, ["ren"])
    with pytest.raises(run_service.RunError, match="이미 진행 중인 런"):
        run_service.start_run(session, user, ["ren"])


# ---------------------------------------------------------------------------
# §3.2 / §5.3 보상 노드는 영구 해금된 카드만 제시한다
# ---------------------------------------------------------------------------


def test_reward_node_offers_only_gacha_unlocked_cards(session, user):
    give_character(session, user, "aria")
    run = run_service.start_run(session, user, ["aria"])
    rng = random.Random(0)

    assert run_service._reward_candidates(session, user, run, rng) == []

    session.add(UserCard(user_id=user.id, card_code="aria_pierce"))
    session.add(UserCard(user_id=user.id, card_code="uni_sweep"))
    # 파티에 없는 캐릭터 전용 카드는 후보에서 빠져야 한다.
    session.add(UserCard(user_id=user.id, card_code="noel_bulwark"))
    session.flush()

    codes = {c.code for c in run_service._reward_candidates(session, user, run, rng)}
    assert codes == {"aria_pierce", "uni_sweep"}


def test_reward_selection_is_run_scoped(session, user):
    give_character(session, user, "aria")
    session.add(UserCard(user_id=user.id, card_code="aria_pierce"))
    session.flush()

    run = run_service.start_run(session, user, ["aria"])
    game_map = run_service.get_map(run)
    node = game_map.nodes["n0_0"]
    node.type = NodeType.REWARD.value
    run.current_node_id = node.id
    run.map_data = game_map.to_dict()
    run_service._enter_reward(session, user, run, node, game_map)
    run.map_data = game_map.to_dict()

    before = len(run.decks["aria"])
    run_service.take_reward(session, user, run, 1)
    assert len(run.decks["aria"]) == before + 1

    # 새 런을 시작하면 덱은 기본 카드만 남는다 (영구 해금과는 별개).
    run_service.abandon_run(session, run)
    fresh = run_service.start_run(session, user, ["aria"])
    assert "aria_pierce" not in fresh.decks["aria"]


# ---------------------------------------------------------------------------
# §5 가챠
# ---------------------------------------------------------------------------


def test_pull_costs_carta_and_grants_items(session, user):
    user.carta = balance.PULL_COST_CARTA * 5
    session.flush()

    results, cost = gacha.pull(session, user, "standard", 1, rng=random.Random(1))
    assert cost == balance.PULL_COST_CARTA
    assert user.carta == balance.PULL_COST_CARTA * 4
    assert len(results) == 1


def test_insufficient_carta_is_rejected(session, user):
    user.carta = 0
    session.flush()
    with pytest.raises(gacha.GachaError, match="카르타가 부족"):
        gacha.pull(session, user, "standard", 1)


def test_duplicate_character_converts_to_wildcard(session, user):
    give_character(session, user, "aria", star=3)
    user.carta = 10_000
    session.flush()

    entry = gacha._PoolEntry("character", "aria", "아리아", 3, 3)
    result = gacha._grant(session, user, entry, is_pickup=False)

    assert result.is_new is False
    assert result.wildcards_gained == balance.DUP_CHARACTER_WILDCARDS[3]
    assert user.wildcards == balance.DUP_CHARACTER_WILDCARDS[3]


def test_duplicate_card_converts_to_fragments(session, user):
    session.add(UserCard(user_id=user.id, card_code="uni_sweep"))
    session.flush()

    card = session.query(Card).filter_by(code="uni_sweep").one()
    entry = gacha._PoolEntry("card", "uni_sweep", card.name, 2, card.rarity)
    result = gacha._grant(session, user, entry, is_pickup=False)

    assert result.is_new is False
    assert user.fragments == balance.DUP_CARD_FRAGMENTS[card.rarity]


def test_new_card_pull_permanently_unlocks_it(session, user):
    entry = gacha._PoolEntry("card", "uni_sweep", "휩쓸기", 2, 3)
    result = gacha._grant(session, user, entry, is_pickup=False)

    assert result.is_new is True
    session.flush()
    row = session.query(UserCard).filter_by(user_id=user.id, card_code="uni_sweep").one()
    assert row.pull_count == 1


def test_limited_banner_5050_guarantees_pickup_after_a_loss(session, user):
    """§5.4: 최고 등급에서 픽업을 놓치면 다음 최고 등급은 픽업 확정."""
    user.carta = 10_000_000
    session.flush()

    state = gacha._state(session, user, gacha.get_banner(session, "pickup_aria"))
    state.guaranteed_pickup = True
    session.flush()

    # 천장 직전으로 맞춰 다음 뽑기가 최고 등급이 되게 한다.
    state.pity = balance.HARD_PITY - 1
    session.flush()

    results, _ = gacha.pull(session, user, "pickup_aria", 1, rng=random.Random(0))
    assert results[0].is_pickup is True
    assert state.guaranteed_pickup is False


def test_hard_pity_forces_top_rarity():
    rng = random.Random(0)
    assert gacha._roll_tier(rng, balance.HARD_PITY - 1) == 3


def test_star_up_consumes_fragments(session, user):
    give_character(session, user, "aria", star=1)
    user.fragments = 1000
    session.flush()

    result = gacha.star_up(session, user, "aria")
    assert result["star"] == 2
    assert user.fragments == 1000 - balance.STAR_UP_FRAGMENT_COST[2]


def test_normal_character_cannot_exceed_three_stars(session, user):
    give_character(session, user, "noel", star=3)   # noel 의 max_star = 3
    user.fragments = 100_000
    session.flush()

    with pytest.raises(gacha.GachaError, match="최대 3성"):
        gacha.star_up(session, user, "noel")


def test_special_character_can_go_beyond_three_stars(session, user):
    give_character(session, user, "aria", star=3)   # aria 의 max_star = 6
    user.fragments = 100_000
    session.flush()

    assert gacha.star_up(session, user, "aria")["star"] == 4


# ---------------------------------------------------------------------------
# §9 연구
# ---------------------------------------------------------------------------


def test_research_requires_prerequisites(session, user):
    nodes = {n["code"]: n for n in research.list_nodes(session, user)}
    assert nodes["party_3"]["locked_by"] == ["party_2"]
    assert nodes["party_2"]["locked_by"] == []


def test_research_bonuses_are_capped_at_design_limits(session, user):
    session.add(UserResearch(user_id=user.id, node_code="party_3", level=1))
    session.add(UserResearch(user_id=user.id, node_code="passive_4", level=1))
    session.flush()

    bonuses = research.account_bonuses(session, user)
    assert bonuses.party_slots == balance.MAX_PARTY_SLOTS
    assert bonuses.passive_slots == balance.MAX_PASSIVE_SLOTS


def test_research_stat_nodes_scale_with_level(session, user):
    session.add(UserResearch(user_id=user.id, node_code="stat_hp", level=3))
    session.flush()
    bonuses = research.account_bonuses(session, user)
    assert bonuses.stat_multiplier("hp") == pytest.approx(1.15)
