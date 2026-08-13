"""캐릭터도 카드다 — 통합 목록과 런 시작 전 덱 구성.

플레이어가 보는 것은 하나의 카드 목록이다. 그중 캐릭터 카드가 파티 자리를
차지하고, 그 캐릭터가 낼 수 있는 행동 카드가 그 캐릭터의 덱에 들어간다(§3.2).
"""

from __future__ import annotations

import pytest

from app.api import screens
from app.content import catalog
from app.content.seed import (CARD_BASIC_ATTACK, CARD_BASIC_DEFENSE,
                              STARTER_CHARACTER_ID, WORLD_1_ID)
from app.db.connection import utcnow
from app.engine import lifecycle as lc


@pytest.fixture
def graduate(db, balance, version, user_id):
    from app.engine import progression as pg

    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_aquel', 3, ?)", (user_id, utcnow()))
    return user_id


def _unlock(db, user_id, card_id):
    db.execute("INSERT OR IGNORE INTO unlocked_cards (user_id, card_id, "
               "upgrade_tier, unlocked_at) VALUES (?, ?, 0, ?)",
               (user_id, card_id, utcnow()))


# =====================================================================
# 통합 목록
# =====================================================================
def test_characters_and_action_cards_are_one_list(db, version, graduate):
    """뽑기도 소장 목록도 덱 구성도 이 하나의 목록 위에서 이루어진다."""
    cards = catalog.owned(db, graduate, version)
    kinds = {card.kind for card in cards}
    assert kinds == {catalog.KIND_CHARACTER, catalog.KIND_ACTION}
    assert all(card.owned for card in cards)


def test_a_character_card_carries_its_star_rank(db, version, graduate):
    aquel = next(card for card in catalog.owned(db, graduate, version)
                 if card.card_id == "char_aquel")
    assert aquel.is_character
    assert aquel.star_rank == 3
    assert aquel.cost is None            # 캐릭터 카드는 자원을 쓰지 않는다


def test_an_action_card_carries_its_cost_and_upgrade_tier(db, version, graduate):
    _unlock(db, graduate, "card_화_강타")
    db.execute("UPDATE unlocked_cards SET upgrade_tier = 2 WHERE user_id = ? "
               "AND card_id = ?", (graduate, "card_화_강타"))
    card = catalog.find(db, version, "card_화_강타", user_id=graduate)
    assert not card.is_character
    assert card.cost is not None and card.upgrade_tier == 2


def test_the_catalog_shows_unowned_cards_too(db, version, graduate):
    """도감에는 아직 못 가진 카드도 보여야 한다."""
    every = catalog.all_cards(db, version, user_id=graduate)
    assert any(not card.owned for card in every)
    assert len(every) > len(catalog.owned(db, graduate, version))


# =====================================================================
# §2.10 어떤 카드를 어떤 캐릭터가 낼 수 있는가
# =====================================================================
def test_a_neutral_card_goes_to_anyone(db, version, graduate):
    for character in catalog.characters(db, graduate, version):
        playable = catalog.playable_for(db, graduate, version, character.card_id)
        assert CARD_BASIC_ATTACK in {card.card_id for card in playable}


def test_an_elemental_card_only_goes_to_a_matching_character(db, version, graduate):
    """§2.10 — 속성이 다른 캐릭터의 덱에는 넣을 수 없다."""
    _unlock(db, graduate, "card_화_강타")
    fire = catalog.find(db, version, "card_화_강타", user_id=graduate)
    assert fire.element == "화"

    aquel = catalog.find(db, version, "char_aquel", user_id=graduate)
    assert aquel.element != "화"
    assert not fire.playable_by(aquel)
    assert "card_화_강타" not in {
        card.card_id for card in
        catalog.playable_for(db, graduate, version, "char_aquel")}


def test_a_character_card_is_not_playable_into_a_deck(db, version, graduate):
    """캐릭터 카드는 자리를 차지하지, 덱에 들어가지 않는다."""
    aquel = catalog.find(db, version, "char_aquel", user_id=graduate)
    starter = catalog.find(db, version, STARTER_CHARACTER_ID, user_id=graduate)
    assert not aquel.playable_by(starter)
    assert all(not card.is_character for card in
               catalog.playable_for(db, graduate, version, STARTER_CHARACTER_ID))


# =====================================================================
# 덱 구성
# =====================================================================
def _reach_deck_step(db, balance, user_id, version, party):
    screens.handle_prep(db, balance, user_id, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    return screens.handle_prep(db, balance, user_id,
                               f"{screens.PREP_PREFIX}party", party, version)


def test_the_deck_step_comes_before_the_run_starts(db, balance, version, graduate):
    """런을 시작하기 전에 덱을 먼저 설정한다."""
    screen = _reach_deck_step(db, balance, graduate, version,
                              [STARTER_CHARACTER_ID, "char_aquel"])
    assert "[3] 덱 구성 (1/2)" in screen["content"]
    assert db.one("SELECT COUNT(*) AS n FROM runs")["n"] == 0


def test_each_party_slot_gets_its_own_deck(db, balance, version, graduate):
    """§3.2 — 덱은 캐릭터별이다. 자리마다 한 번씩 묻는다."""
    _reach_deck_step(db, balance, graduate, version,
                     [STARTER_CHARACTER_ID, "char_aquel"])
    second = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}deck_auto:1", [], version)
    assert "[3] 덱 구성 (2/2)" in second["content"]
    last = screens.handle_prep(db, balance, graduate,
                               f"{screens.PREP_PREFIX}deck_auto:2", [], version)
    assert "[4] 확정" in last["content"]


def test_the_chosen_cards_reach_the_run_deck(db, balance, version, graduate):
    """고른 카드가 실제로 덱에 들어가야 한다 — 이게 전부여야 한다."""
    _unlock(db, graduate, "card_starter_화염참")
    _reach_deck_step(db, balance, graduate, version, [STARTER_CHARACTER_ID,
                                                      "char_aquel"])
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}deck:1",
                        ["card_starter_화염참"], version)
    screens.handle_prep(db, balance, graduate,
                        f"{screens.PREP_PREFIX}deck_auto:2", [], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}confirm", [], version)

    deck = [row["card_id"] for row in db.query(
        "SELECT card_id FROM run_deck_cards WHERE run_id = ? AND party_slot = 1",
        (result["run_id"],))]
    assert deck.count("card_starter_화염참") > 0
    assert len(deck) == int(balance.get("base_deck_size"))


def test_the_basic_cards_survive_any_deck_choice(db, balance, version, graduate):
    """§4.3 — 낼 카드가 없어 자동 방어만 하는 덱은 만들 수 없어야 한다."""
    _unlock(db, graduate, "card_starter_화염참")
    _reach_deck_step(db, balance, graduate, version, [STARTER_CHARACTER_ID,
                                                      "char_aquel"])
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}deck:1",
                        ["card_starter_화염참"], version)
    screens.handle_prep(db, balance, graduate,
                        f"{screens.PREP_PREFIX}deck_auto:2", [], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}confirm", [], version)

    deck = [row["card_id"] for row in db.query(
        "SELECT card_id FROM run_deck_cards WHERE run_id = ? AND party_slot = 1",
        (result["run_id"],))]
    composition = balance.get("starter_deck_composition")
    assert deck.count(CARD_BASIC_ATTACK) >= int(composition["평타"])
    assert deck.count(CARD_BASIC_DEFENSE) == int(composition["기본_방어"])


def test_a_card_the_character_cannot_play_is_refused(db, balance, version,
                                                     graduate):
    """§2.10 — 화면이 보여준 적 없는 조합은 위조해도 통하지 않는다."""
    from app.api import errors

    _unlock(db, graduate, "card_화_강타")
    _reach_deck_step(db, balance, graduate, version, [STARTER_CHARACTER_ID,
                                                      "char_aquel"])
    screens.handle_prep(db, balance, graduate,
                        f"{screens.PREP_PREFIX}deck_auto:1", [], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}deck:2",
                                 ["card_화_강타"], version)
    assert result["content"] == errors.ILLEGAL_STATE
    assert screens.load_draft(db, graduate)["deck"].get(2) is None


def test_the_engine_refuses_a_forged_deck(db, balance, version, graduate):
    """화면을 우회해 엔진을 직접 불러도 같은 규칙으로 막힌다."""
    _unlock(db, graduate, "card_화_강타")
    request = lc.RunBuildRequest(
        user_id=graduate, world_id=WORLD_1_ID,
        party_character_ids=[STARTER_CHARACTER_ID, "char_aquel"],
        deck_by_slot={2: ["card_화_강타"]})
    with pytest.raises(lc.LifecycleError, match="낼 수 없"):
        lc.validate_build(db, balance, request, version)


def test_changing_the_party_clears_the_deck(db, balance, version, graduate):
    """자리 번호가 가리키는 캐릭터가 바뀌면 이미 고른 덱은 뜻을 잃는다."""
    _unlock(db, graduate, "card_starter_화염참")
    _reach_deck_step(db, balance, graduate, version, [STARTER_CHARACTER_ID,
                                                      "char_aquel"])
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}deck:1",
                        ["card_starter_화염참"], version)
    assert screens.load_draft(db, graduate)["deck"][1]

    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}party",
                        ["char_aquel", STARTER_CHARACTER_ID], version)
    assert screens.load_draft(db, graduate)["deck"] == {}


def test_the_preview_matches_the_deck_that_gets_built(db, balance, version,
                                                      graduate):
    """화면이 보여준 덱과 실제로 만들어지는 덱이 달라서는 안 된다."""
    _unlock(db, graduate, "card_starter_화염참")
    chosen = ["card_starter_화염참"]
    preview = lc.preview_deck(db, balance, graduate, STARTER_CHARACTER_ID,
                              version, chosen=chosen)

    _reach_deck_step(db, balance, graduate, version, [STARTER_CHARACTER_ID,
                                                      "char_aquel"])
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}deck:1",
                        chosen, version)
    screens.handle_prep(db, balance, graduate,
                        f"{screens.PREP_PREFIX}deck_auto:2", [], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}confirm", [], version)

    built = [row["card_id"] for row in db.query(
        "SELECT card_id FROM run_deck_cards WHERE run_id = ? AND party_slot = 1 "
        "ORDER BY pile_position", (result["run_id"],))]
    assert sorted(preview) == sorted(built)


def test_the_deck_screen_shows_the_deck(db, balance, version, graduate):
    screen = _reach_deck_step(db, balance, graduate, version,
                              [STARTER_CHARACTER_ID, "char_aquel"])
    assert screen["attachments"][0]["filename"] == "deckout_deck.png"


def test_the_tutorial_still_starts_without_a_deck_choice(db, balance, version,
                                                         user_id):
    """튜토리얼은 고를 것이 사실상 없다. 덱을 고르지 않아도 시작돼야 한다."""
    from app.content.seed import TUTORIAL_WORLD_ID

    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    assert db.one("SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ?",
                  (run_id,))["n"] == int(balance.get("base_deck_size"))
