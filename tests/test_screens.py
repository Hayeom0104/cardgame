"""§16.2.2 준비 화면과 §5 뽑기 화면 — 런 밖 컴포넌트."""

from __future__ import annotations

import pytest

from app.api import errors, screens
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID, WORLD_1_ID
from app.db.connection import utcnow
from app.engine import lifecycle as lc
from app.engine import progression as pg


@pytest.fixture
def graduate(db, balance, version, user_id):
    """튜토리얼을 마치고 두 번째 캐릭터까지 얻은 계정."""
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_aquel', 3, ?)", (user_id, utcnow()))
    return user_id


# =====================================================================
# 막혀 있던 경로
# =====================================================================
def test_a_fresh_graduate_can_reach_the_main_campaign(db, balance, version,
                                                      user_id):
    """튜토리얼 클리어 → 뽑기 → 본편. 뽑기 화면이 없던 동안은 두 번째 캐릭터를
    얻을 길이 없어 이 경로 전체가 막혀 있었다."""
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    assert db.one("SELECT COUNT(*) AS n FROM owned_characters WHERE user_id = ?",
                  (user_id,))["n"] == 1

    # 카르타 1,900 — 시작 1,600 + 튜토리얼 300.
    screen = screens.gacha_screen(db, balance, user_id, version)
    ten = next(entry for entry in screen["components"]
               if entry["custom_id"].endswith(":ten"))
    screens.handle_gacha(db, balance, user_id, ten["custom_id"], version)

    # §5.10 보장이 발동해 캐릭터가 최소 한 명 더 들어온다.
    assert db.one("SELECT COUNT(*) AS n FROM owned_characters WHERE user_id = ?",
                  (user_id,))["n"] >= 2

    characters = screens.owned_characters(db, user_id, version)
    screens.handle_prep(db, balance, user_id, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    screens.handle_prep(db, balance, user_id, f"{screens.PREP_PREFIX}party",
                        [entry["character_id"] for entry in characters[:2]], version)
    result = screens.handle_prep(db, balance, user_id,
                                 f"{screens.PREP_PREFIX}confirm", [], version)

    assert result.get("run_id") is not None
    run = db.one("SELECT world_id, state FROM runs WHERE run_id = ?",
                 (result["run_id"],))
    assert run["world_id"] == WORLD_1_ID
    assert run["state"] == lc.PREPARING


# =====================================================================
# §16.2.2 준비 화면
# =====================================================================
def test_the_draft_holds_no_run_row(db, balance, version, graduate):
    """[1]-[4]는 순수 UI다 — 도중에 그만둬도 아무 비용도 남지 않고
    `one_active_run`과 충돌하지 않는다."""
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}party",
                        [STARTER_CHARACTER_ID, "char_aquel"], version)

    assert db.one("SELECT COUNT(*) AS n FROM runs")["n"] == 0
    assert lc.active_run_for(db, graduate) is None
    # 선택은 초안에만 쌓여 있다.
    draft = screens.load_draft(db, graduate)
    assert draft["world_id"] == WORLD_1_ID
    assert len(draft["party"]) == 2


def test_cancelling_costs_nothing(db, balance, version, graduate):
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}cancel", [], version)
    assert "취소" in result["content"]
    assert screens.load_draft(db, graduate)["world_id"] is None
    assert db.one("SELECT COUNT(*) AS n FROM runs")["n"] == 0


def test_the_confirm_screen_shows_what_will_be_snapshotted(db, balance, version,
                                                           graduate):
    """[4] CONFIRM은 §16.2.3에서 얼려질 바로 그 값을 보여준다."""
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    screen = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}party",
                                 [STARTER_CHARACTER_ID, "char_aquel"], version)

    # 아쿠엘은 3★ 서포터형: HP 70 × (1 + 0.12×2) = 86, 공 6 × (1 + 0.10×2) = 7
    assert "HP 86" in screen["content"]
    assert "공 7" in screen["content"]
    # 스타터는 1★ 딜서포트형이라 기본값 그대로.
    assert "HP 75" in screen["content"]
    assert [button["label"] for button in screen["components"]] == ["확정", "취소"]


def test_the_confirm_values_match_the_run_snapshot(db, balance, version, graduate):
    """확인 화면이 보여준 것과 실제로 얼려진 것이 같아야 한다."""
    from app.engine import stats

    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}party",
                        [STARTER_CHARACTER_ID, "char_aquel"], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}confirm", [], version)

    members = db.query(
        "SELECT rc.character_id, rc.hp_max, rbs.star_rank FROM run_characters rc "
        "JOIN run_build_snapshot rbs ON rbs.run_id = rc.run_id "
        "AND rbs.party_slot = rc.party_slot WHERE rc.run_id = ? ORDER BY rc.party_slot",
        (result["run_id"],))
    by_id = {row["character_id"]: row for row in members}
    assert by_id["char_aquel"]["hp_max"] == 86
    assert by_id["char_aquel"]["star_rank"] == 3
    assert by_id[STARTER_CHARACTER_ID]["hp_max"] == 75


def test_duplicate_party_picks_are_collapsed(db, balance, version, graduate):
    """§16.2.2 — 같은 캐릭터를 중복으로 넣을 수 없다."""
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}party",
                        [STARTER_CHARACTER_ID, STARTER_CHARACTER_ID], version)
    assert screens.load_draft(db, graduate)["party"] == [STARTER_CHARACTER_ID]


def test_a_character_the_account_does_not_own_is_rejected(db, balance, version,
                                                          graduate):
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}party",
                                 ["char_umbra"], version)
    assert result["content"] == errors.ILLEGAL_STATE
    assert screens.load_draft(db, graduate)["party"] == []


def test_a_forged_passive_id_never_reaches_the_run(db, balance, version,
                                                   graduate):
    """§6 — 화면이 보여준 적 없는 패시브 id는 초안에도 런에도 들어가지 않는다.

    `custom_id`는 클라이언트가 그대로 되돌려 보내는 값이라 위조할 수 있다.
    개수만 세는 검사로는 막을 수 없다.
    """
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}party",
                        [STARTER_CHARACTER_ID], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}passive",
                                 ["card_평타"], version)
    assert result["content"] == errors.ILLEGAL_STATE
    assert screens.load_draft(db, graduate)["passives"] == []


def test_the_engine_rejects_a_passive_the_account_cannot_bring(db, balance,
                                                               version, graduate):
    """화면을 우회해 엔진을 직접 호출해도 같은 목록으로 걸러진다."""
    request = lc.RunBuildRequest(
        user_id=graduate, world_id=WORLD_1_ID,
        party_character_ids=[STARTER_CHARACTER_ID],
        passive_card_ids=["card_평타"],
    )
    with pytest.raises(lc.LifecycleError):
        lc.validate_build(db, balance, request, version)


def test_the_passive_step_is_skipped_when_nothing_is_selectable(db, balance,
                                                                version, graduate):
    """고를 패시브가 없으면 [3]을 건너뛰고 곧장 [4]로 간다."""
    assert lc.selectable_passives(db, graduate, version) == []
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}party",
                                 [STARTER_CHARACTER_ID], version)
    assert "[4] 확정" in result["content"]


def test_skipping_ahead_returns_to_the_world_step(db, balance, version, graduate):
    """월드를 고르지 않고 파티 단계를 제출하면 [1]로 되돌린다 — 순서를 건너뛴
    제출이 조용히 통과해서는 안 된다."""
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}party",
                                 [STARTER_CHARACTER_ID], version)
    assert "[1] 월드 선택" in result["content"]


def test_a_world_that_is_not_unlocked_is_rejected(db, balance, version, graduate):
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}world", ["world_4"],
                                 version)
    assert result["content"] == errors.ILLEGAL_STATE


def test_the_main_campaign_still_needs_two_characters(db, balance, version,
                                                      user_id):
    """§4.1 — 한 명만 남은 계정에는 어디로 가야 하는지 알려준다."""
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    screens.handle_prep(db, balance, user_id, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    screen = screens.party_select_screen(db, user_id, version,
                                         screens.load_draft(db, user_id))
    assert errors.PARTY_TOO_SMALL in screen["content"]
    assert "뽑기" in screen["content"]


def test_a_run_started_elsewhere_ends_the_draft(db, balance, version, graduate):
    """§16.3 계정당 하나 규칙은 준비 화면에서도 지켜져야 한다."""
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    lc.create_run(db, balance,
                  lc.RunBuildRequest(user_id=graduate, world_id=WORLD_1_ID,
                                     party_character_ids=[STARTER_CHARACTER_ID,
                                                          "char_aquel"],
                                     is_tutorial=False),
                  version)

    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}party",
                                 [STARTER_CHARACTER_ID, "char_aquel"], version)
    assert result["content"] == errors.RUN_ALREADY_ACTIVE
    assert screens.load_draft(db, graduate)["world_id"] is None


def test_the_draft_is_cleared_once_the_run_exists(db, balance, version, graduate):
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}party",
                        [STARTER_CHARACTER_ID, "char_aquel"], version)
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}confirm",
                        [], version)
    assert screens.load_draft(db, graduate)["world_id"] is None


# =====================================================================
# §5 뽑기 화면
# =====================================================================
def test_limited_banners_are_hidden_until_the_guarantee_is_consumed(db, balance,
                                                                    version,
                                                                    user_id):
    """§5.10.2 — 보장이 소비되기 전에는 상시 배너만 뽑을 수 있다."""
    screen = screens.gacha_screen(db, balance, user_id, version)
    ids = {entry["custom_id"] for entry in screen["components"]}
    assert all("banner_standard" in custom_id for custom_id in ids)
    assert "첫" in screen["content"]      # 남은 보장 회수를 안내한다

    screens.handle_gacha(db, balance, user_id,
                         f"{screens.GACHA_PREFIX}banner_standard:ten", version)
    db.execute("UPDATE accounts SET carta = 1600 WHERE user_id = ?", (user_id,))

    reopened = screens.gacha_screen(db, balance, user_id, version)
    reopened_ids = {entry["custom_id"] for entry in reopened["components"]}
    assert any("banner_limited" in custom_id for custom_id in reopened_ids)


def test_a_ten_pull_reports_every_result(db, balance, version, user_id):
    result = screens.handle_gacha(db, balance, user_id,
                                  f"{screens.GACHA_PREFIX}banner_standard:ten",
                                  version)
    lines = result["content"].splitlines()
    assert lines[0].startswith("**뽑기 결과** (10회)")
    assert len(lines) == 11


def test_a_duplicate_shows_both_materials(db, balance, version, user_id):
    """§5.2 🟡 R-4 — 중복 캐릭터는 조각과 와일드카드를 둘 다 준다."""
    from app.engine import gacha

    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    with db.tx() as conn:
        result = gacha._grant(conn, balance, user_id, "character", "char_terradon",
                              gacha.BAND_BASE, version, won_5050=None)
    outcome = gacha.GachaOutcome(gacha_id="x", results=[result])

    text = screens.format_results(db, outcome, version)
    assert "중복" in text and "조각 +15" in text and "와일드카드 +1" in text


def test_insufficient_carta_is_reported_not_charged(db, balance, version, user_id):
    db.execute("UPDATE accounts SET carta = 10 WHERE user_id = ?", (user_id,))
    result = screens.handle_gacha(db, balance, user_id,
                                  f"{screens.GACHA_PREFIX}banner_standard:ten",
                                  version)
    assert result["content"] == errors.INSUFFICIENT_CURRENCY
    assert db.one("SELECT carta FROM accounts WHERE user_id = ?",
                  (user_id,))["carta"] == 10


def test_a_locked_banner_cannot_be_pulled_by_forging_the_custom_id(db, balance,
                                                                   version,
                                                                   user_id):
    """화면에 숨겨져 있어도 엔진이 다시 막는다 (§5.10.2)."""
    result = screens.handle_gacha(
        db, balance, user_id,
        f"{screens.GACHA_PREFIX}banner_limited_aquel:single", version)
    assert result["content"] == errors.ILLEGAL_STATE
    assert db.one("SELECT COUNT(*) AS n FROM gacha_transactions "
                  "WHERE status = 'completed'")["n"] == 0


def test_a_malformed_pull_kind_is_rejected(db, balance, version, user_id):
    result = screens.handle_gacha(db, balance, user_id,
                                  f"{screens.GACHA_PREFIX}banner_standard:hundred",
                                  version)
    assert result["content"] == errors.ILLEGAL_STATE


def test_the_forced_result_is_labelled(db, balance, version, user_id):
    """§5.10.1 — 보장으로 강제된 결과임을 플레이어가 알 수 있어야 한다."""
    from app.engine import gacha

    outcome = gacha.GachaOutcome(gacha_id="x", results=[
        gacha.PullResult(index=9, band=gacha.BAND_BASE, kind="character",
                         entity_id="char_terradon", forced_by_guarantee=True)])
    assert "첫 뽑기 보장" in screens.format_results(db, outcome, version)


def test_the_confirm_screen_and_its_picture_show_the_same_build(db, balance,
                                                                version, graduate):
    """글자와 그림이 서로 다른 값을 보여주면 안 된다 (§16.2.3)."""
    screens.handle_prep(db, balance, graduate, f"{screens.PREP_PREFIX}world",
                        [WORLD_1_ID], version)
    result = screens.handle_prep(db, balance, graduate,
                                 f"{screens.PREP_PREFIX}party",
                                 [STARTER_CHARACTER_ID], version)
    assert result["attachments"], "확정 화면에 그림이 붙지 않았습니다"
    assert result["attachments"][0]["filename"] == "deckout_prep.png"


def test_the_gacha_screen_shows_the_banner(db, balance, version, user_id):
    screen = screens.gacha_screen(db, balance, user_id, version)
    assert screen["attachments"][0]["filename"] == "deckout_banner.png"


def test_the_pull_result_is_also_a_picture(db, balance, version, user_id):
    db.execute("UPDATE accounts SET carta = 100000 WHERE user_id = ?", (user_id,))
    banner = db.one("SELECT banner_id FROM banners WHERE content_version_id = ? "
                    "AND banner_type = 'standard'", (version,))
    result = screens.handle_gacha(
        db, balance, user_id, f"{screens.GACHA_PREFIX}{banner['banner_id']}:single",
        version, event_id="evt-그림-1")
    assert result["attachments"][0]["filename"] == "deckout_gacha.png"
