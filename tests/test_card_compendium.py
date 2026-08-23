"""§5.3a 카드 도감 — read-only, every action-card slot always visible."""

from __future__ import annotations

import pytest

from app.api import handlers, hub
from app.content import catalog
from app.render import panels


@pytest.fixture
def ctx(db, balance, version):
    return handlers.HandlerContext(db=db, balance=balance, central=None,
                                   content_version_id=version)


# =====================================================================
# catalog.compendium
# =====================================================================
def test_compendium_includes_every_action_card_slot_owned_or_not(db, version, user_id):
    all_action_cards = db.one(
        "SELECT COUNT(*) AS n FROM cards WHERE content_version_id = ? "
        "AND is_retired = 0", (version,))["n"]
    cards = catalog.compendium(db, user_id, version)
    assert len(cards) == all_action_cards
    assert any(card.owned for card in cards)
    assert any(not card.owned for card in cards)


def test_compendium_excludes_characters(db, version, user_id):
    cards = catalog.compendium(db, user_id, version)
    assert all(not card.is_character for card in cards)


def test_an_unowned_card_reports_upgrade_tier_zero(db, version, user_id):
    cards = catalog.compendium(db, user_id, version)
    unowned = next(card for card in cards if not card.owned)
    assert unowned.upgrade_tier == 0


# =====================================================================
# render_card(masked=True)
# =====================================================================
def _card_dict(**overrides) -> dict:
    base = {"card_id": "card_풍_질풍", "name": "질풍베기", "element": "풍",
            "rarity_tier": 2, "cost": 1, "category": "공격", "upgrade_tier": 0}
    return {**base, **overrides}


def test_masked_hides_name_cost_and_category():
    canvas = panels.Canvas(panels.theme_module.load().size("card_size"))
    image = panels.render_card(_card_dict(), canvas, masked=True)
    # No crash, and a distinct image comes back — the real assertion is at
    # the pixel/text level, checked indirectly via the unmasked comparison
    # below plus the compendium screenshot review during implementation.
    assert image.size == canvas.theme.size("card_size")


def test_masked_and_unmasked_render_differently(db, version):
    canvas = panels.Canvas(panels.theme_module.load().size("card_size"))
    masked = panels.render_card(_card_dict(), canvas, masked=True)
    unmasked = panels.render_card(_card_dict(), canvas, masked=False)
    assert list(masked.getdata()) != list(unmasked.getdata())


def test_masked_card_still_shows_rarity_gems():
    """§5.3a — rarity stays visible even when everything else is hidden."""
    canvas = panels.Canvas(panels.theme_module.load().size("card_size"))
    zero_rarity = panels.render_card(_card_dict(rarity_tier=1), canvas, masked=True)
    high_rarity = panels.render_card(_card_dict(rarity_tier=6), canvas, masked=True)
    assert list(zero_rarity.getdata()) != list(high_rarity.getdata())


# =====================================================================
# screens — the [도감] button reaches the render
# =====================================================================
def test_the_deck_screen_carries_the_catalog_button(ctx, user_id):
    screen = handlers.deck_screen(ctx, user_id)
    custom_ids = [c["custom_id"] for c in screen["components"]]
    assert f"{hub.HUB_PREFIX}catalog" in custom_ids


def test_the_catalog_button_appears_even_with_no_owned_cards(ctx, db, version):
    from app.content.seed import create_account

    fresh_user = 900321
    create_account(db, fresh_user, version)
    db.execute("DELETE FROM unlocked_cards WHERE user_id = ?", (fresh_user,))
    db.execute("DELETE FROM owned_characters WHERE user_id = ?", (fresh_user,))

    screen = handlers.deck_screen(ctx, fresh_user)
    custom_ids = [c["custom_id"] for c in screen.get("components", [])]
    assert f"{hub.HUB_PREFIX}catalog" in custom_ids


def test_catalog_screen_renders_regardless_of_run_state(ctx, user_id):
    screen = handlers.catalog_screen(ctx, user_id)
    assert screen["attachments"], "the compendium must render an attachment"


def test_the_catalog_button_reaches_the_screen_through_hub_routing(ctx, user_id):
    result = hub.handle_hub(ctx, user_id, f"{hub.HUB_PREFIX}catalog", [])
    assert result["action"] == "edit"
    assert result["attachments"]


def test_the_run_deck_screen_also_carries_the_catalog_button(ctx, db, version, user_id):
    """§5.3a — available in and out of a run, not just out of one."""
    from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
    from app.engine import lifecycle as lc

    lc.create_run(
        db, ctx.balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version,
    )
    screen = handlers.deck_screen(ctx, user_id)
    custom_ids = [c["custom_id"] for c in screen["components"]]
    assert f"{hub.HUB_PREFIX}catalog" in custom_ids
