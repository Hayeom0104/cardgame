"""§5 gacha — the pity curve, the redistribution rule, and the §5.10 guarantee."""

from __future__ import annotations

import pytest

from app.engine import gacha


# =====================================================================
# §15.4 pity curve — the M-13 corrections
# =====================================================================
@pytest.mark.parametrize("pull_count,expected_top", [
    (69, 0.015),    # n = 70, still flat
    (70, 0.065),    # n = 71, first +5.0%p step
    (74, 0.265),    # n = 75
    (79, 0.515),    # n = 80
    (84, 0.765),    # n = 85
    (86, 0.865),    # n = 87
    (87, 0.915),    # n = 88
    (88, 0.965),    # n = 89 — peaks below 100%
    (89, 1.0),      # n = 90 — hard pity, and it is REACHABLE
])
def test_pity_curve_matches_the_published_table(balance, pull_count, expected_top):
    """v6.1's +5.5%p reached 100.5% at pull 88, making pull-90 hard pity
    unreachable. +5.0%p peaks at 96.5% on pull 89."""
    probabilities = gacha.band_probabilities(balance, pull_count)
    assert probabilities[gacha.BAND_TOP] == pytest.approx(expected_top)


@pytest.mark.parametrize("pull_count", range(0, 95))
def test_bands_always_sum_to_exactly_one(balance, pull_count):
    """v6.1 never said which lower band lost probability, so the bands did not
    re-sum to 100%."""
    probabilities = gacha.band_probabilities(balance, pull_count)
    assert sum(probabilities.values()) == pytest.approx(1.0)
    assert all(value >= 0 for value in probabilities.values())


def test_mid_band_holds_at_12_percent_until_base_is_exhausted(balance):
    """기본등급 absorbs first; once 기본 reaches 0, 중간 absorbs the remainder."""
    at_85 = gacha.band_probabilities(balance, 84)
    assert at_85[gacha.BAND_MID] == pytest.approx(0.12)
    assert at_85[gacha.BAND_BASE] == pytest.approx(0.115)

    at_88 = gacha.band_probabilities(balance, 87)
    assert at_88[gacha.BAND_BASE] == pytest.approx(0.0)
    assert at_88[gacha.BAND_MID] == pytest.approx(0.085)


# =====================================================================
# §5.10 first-pull guarantee — the B-05 / B-06 fixes
# =====================================================================
def test_ten_single_pulls_still_guarantee_a_character(db, balance, version, user_id):
    """v6.2 keyed the guarantee to "the account's first 10-pull", but nothing
    forces a new player to use one — ten singles bypassed it entirely."""
    for index in range(10):
        gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                   pull_kind="single", content_version_id=version,
                   gacha_id=f"single-{index}")

    owned = db.query(
        "SELECT oc.character_id FROM owned_characters oc JOIN characters c "
        "ON c.character_id = oc.character_id AND c.content_version_id = ? "
        "WHERE oc.user_id = ? AND c.in_gacha_pool = 1", (version, user_id))
    assert len(owned) >= 1      # >= 2 characters total, counting the starter

    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    assert account["first_pull_guarantee_used"] == 1


def test_a_ten_pull_also_consumes_the_guarantee(db, balance, version, user_id):
    outcome = gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                         pull_kind="ten", content_version_id=version)
    assert len(outcome.results) == 10
    assert any(result.kind == "character" for result in outcome.results)
    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    assert account["first_pull_guarantee_used"] == 1


def test_the_forced_result_still_rolls_its_rarity_band_normally(db, balance,
                                                               version, user_id):
    """§5.10.1 — the guarantee overrides only the character/card axis."""
    outcome = gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                         pull_kind="ten", content_version_id=version)
    forced = [result for result in outcome.results if result.forced_by_guarantee]
    for result in forced:
        assert result.kind == "character"
        assert result.band in (gacha.BAND_TOP, gacha.BAND_MID, gacha.BAND_BASE)


def test_limited_banners_are_locked_until_the_guarantee_is_consumed(db, balance,
                                                                    version, user_id):
    """§5.10.2 — resolved by removing the §5.4.1 collision, not ranking it."""
    with pytest.raises(gacha.GachaError, match="limited banners are locked"):
        gacha.pull(db, balance, user_id=user_id, banner_id="banner_limited_aquel",
                   pull_kind="single", content_version_id=version)

    gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
               pull_kind="ten", content_version_id=version)
    # The starting 1,600 카르타 is exactly one 10-pull (§4.6.3), so top up
    # before testing that the limited banner has opened.
    db.execute("UPDATE accounts SET carta = 1600 WHERE user_id = ?", (user_id,))
    # Now it is available.
    outcome = gacha.pull(db, balance, user_id=user_id,
                         banner_id="banner_limited_aquel", pull_kind="single",
                         content_version_id=version)
    assert len(outcome.results) == 1


# =====================================================================
# §5.9 / §17.5 atomicity
# =====================================================================
def test_a_retry_replays_results_and_never_rerolls(db, balance, version, user_id):
    first = gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                       pull_kind="ten", content_version_id=version,
                       gacha_id="fixed-id")
    carta_after = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                         (user_id,))["carta"]

    second = gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                        pull_kind="ten", content_version_id=version,
                        gacha_id="fixed-id")
    assert second.replayed
    assert [r.entity_id for r in second.results] == [r.entity_id for r in first.results]
    # No second deduction.
    assert db.one("SELECT carta FROM accounts WHERE user_id = ?",
                  (user_id,))["carta"] == carta_after


def test_commit_seed_is_server_controlled_and_not_the_run_seed(db, balance,
                                                               version, user_id):
    """§5.9 — a player-visible run seed must never drive a monetized system."""
    outcome = gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                         pull_kind="single", content_version_id=version)
    row = db.one("SELECT commit_seed FROM gacha_transactions WHERE gacha_id = ?",
                 (outcome.gacha_id,))
    assert len(row["commit_seed"]) == 64      # 32 random bytes, hex


def test_duplicate_character_yields_both_fragments_and_wildcards(db, balance,
                                                                 version, user_id):
    """🟡 R-4 — fragments alone would leave 와일드카드 with no source."""
    from app.db.connection import utcnow

    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    with db.tx() as conn:
        result = gacha._grant(conn, balance, user_id, "character", "char_terradon",
                              gacha.BAND_BASE, version, won_5050=None)

    assert result.is_duplicate
    assert result.fragments == 15 and result.wildcards == 1
    fragments = db.one("SELECT amount FROM character_fragments WHERE user_id = ? "
                       "AND character_id = 'char_terradon'", (user_id,))
    assert fragments["amount"] == 15
    assert db.one("SELECT wildcards FROM accounts WHERE user_id = ?",
                  (user_id,))["wildcards"] == 1


def test_insufficient_carta_is_rejected(db, balance, version, user_id):
    db.execute("UPDATE accounts SET carta = 10 WHERE user_id = ?", (user_id,))
    with pytest.raises(gacha.GachaError, match="insufficient"):
        gacha.pull(db, balance, user_id=user_id, banner_id="banner_standard",
                   pull_kind="single", content_version_id=version)


def test_pity_is_keyed_by_scope_not_by_banner(db, version, user_id):
    """§5.4.2 — exactly two counters exist per account."""
    rows = db.query("SELECT pity_scope_id FROM gacha_pity WHERE user_id = ?",
                    (user_id,))
    assert {row["pity_scope_id"] for row in rows} == {
        gacha.STANDARD_SCOPE, gacha.LIMITED_SCOPE}
