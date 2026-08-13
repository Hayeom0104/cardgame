"""허브 화면의 실행 버튼 — 진행 시스템에 실제로 닿는가.

성급 상승·카드 업그레이드·연구·상점·강화는 엔진에 전부 구현돼 있었지만
허브 화면이 읽기 전용이라 플레이어가 실행할 방법이 없었다. 여기서 확인하는
것은 "엔진이 동작하는가"가 아니라 **"화면에서 닿는가"** 다.
"""

from __future__ import annotations

import pytest

from app.api import errors, handlers, hub
from app.central import transactions as tx
from app.central.client import CurrencyResult
from app.content.seed import STARTER_CHARACTER_ID
from app.db.connection import utcnow
from app.engine import lifecycle as lc
from app.engine import progression as pg


class FakeCentral:
    """§1.3.8대로 차감을 잔액에서 clamp하는 중앙봇 스텁."""

    def __init__(self, balance: int = 1_000_000):
        self.balance = balance
        self.applied_keys: dict[str, int] = {}
        self.deduct_calls = 0

    def currency_add(self, user_id, amount, idempotency_key):
        if idempotency_key in self.applied_keys:
            return CurrencyResult(requested=amount,
                                  applied=self.applied_keys[idempotency_key])
        self.balance += amount
        self.applied_keys[idempotency_key] = amount
        return CurrencyResult(requested=amount, applied=amount)

    def currency_deduct(self, user_id, amount, idempotency_key):
        self.deduct_calls += 1
        if idempotency_key in self.applied_keys:
            return CurrencyResult(requested=-abs(amount),
                                  applied=self.applied_keys[idempotency_key])
        applied = -min(abs(amount), self.balance)
        self.balance += applied
        self.applied_keys[idempotency_key] = applied
        return CurrencyResult(requested=-abs(amount), applied=applied)


@pytest.fixture
def central() -> FakeCentral:
    return FakeCentral()


@pytest.fixture
def ctx(db, balance, version, central):
    return handlers.HandlerContext(db=db, balance=balance, central=central,
                                   content_version_id=version)


def _select(custom_id: str, value: str) -> tuple[str, list[str]]:
    return f"{hub.HUB_PREFIX}{custom_id}", [value]


def _options(screen: dict, custom_id_suffix: str) -> list[dict]:
    for component in screen.get("components", []):
        if component["custom_id"].endswith(custom_id_suffix):
            return component["options"]
    return []


# =====================================================================
# §4.4 성급 상승
# =====================================================================
def test_the_character_screen_offers_a_star_up_when_it_is_affordable(
        ctx, db, user_id):
    """재화가 모이면 화면에 실행 선택지가 나타나야 한다."""
    assert _options(handlers.characters_screen(ctx, user_id), "starup") == []

    db.execute("INSERT INTO character_fragments (user_id, character_id, amount) "
               "VALUES (?, ?, 999)", (user_id, STARTER_CHARACTER_ID))
    db.execute("UPDATE accounts SET wildcards = 99 WHERE user_id = ?", (user_id,))

    options = _options(handlers.characters_screen(ctx, user_id), "starup")
    assert [entry["value"] for entry in options] == [STARTER_CHARACTER_ID]


def test_a_star_up_from_the_screen_actually_raises_the_rank(ctx, db, user_id):
    db.execute("INSERT INTO character_fragments (user_id, character_id, amount) "
               "VALUES (?, ?, 999)", (user_id, STARTER_CHARACTER_ID))
    db.execute("UPDATE accounts SET wildcards = 99 WHERE user_id = ?", (user_id,))
    before = db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                    "AND character_id = ?", (user_id, STARTER_CHARACTER_ID))

    custom_id, values = _select("starup", STARTER_CHARACTER_ID)
    result = hub.handle_hub(ctx, user_id, custom_id, values, event_id="evt-1")

    after = db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                   "AND character_id = ?", (user_id, STARTER_CHARACTER_ID))
    assert after["star_rank"] == before["star_rank"] + 1
    assert "성급을 올렸습니다" in result["content"]
    # 끝난 뒤에는 갱신된 화면이 그대로 붙어 나간다.
    assert "**캐릭터**" in result["content"]


# =====================================================================
# §5.8 카드 업그레이드 — v6.4의 P-1
# =====================================================================
def test_the_deck_screen_offers_a_card_upgrade(ctx, db, user_id):
    """엔진에만 있고 닿을 길이 없던 기능이다."""
    db.execute("INSERT INTO card_fragments (user_id, card_id, amount) "
               "VALUES (?, 'card_starter_화염참', 999)", (user_id,))
    db.execute("INSERT OR IGNORE INTO unlocked_cards (user_id, card_id, "
               "upgrade_tier, unlocked_at) VALUES (?, 'card_starter_화염참', 0, ?)",
               (user_id, utcnow()))
    db.execute("UPDATE accounts SET wildcards = 99 WHERE user_id = ?", (user_id,))

    options = _options(handlers.deck_screen(ctx, user_id), "cardup")
    assert "card_starter_화염참" in {entry["value"] for entry in options}


def test_a_card_upgrade_from_the_screen_raises_the_tier(ctx, db, user_id):
    db.execute("INSERT INTO card_fragments (user_id, card_id, amount) "
               "VALUES (?, 'card_starter_화염참', 999)", (user_id,))
    db.execute("INSERT OR IGNORE INTO unlocked_cards (user_id, card_id, "
               "upgrade_tier, unlocked_at) VALUES (?, 'card_starter_화염참', 0, ?)",
               (user_id, utcnow()))
    db.execute("UPDATE accounts SET wildcards = 99 WHERE user_id = ?", (user_id,))

    custom_id, values = _select("cardup", "card_starter_화염참")
    hub.handle_hub(ctx, user_id, custom_id, values, event_id="evt-2")

    row = db.one("SELECT upgrade_tier FROM unlocked_cards WHERE user_id = ? "
                 "AND card_id = ?", (user_id, "card_starter_화염참"))
    assert row["upgrade_tier"] == 1


# =====================================================================
# §16.7 재전송 — 두 번 청구되지 않는가
# =====================================================================
def test_a_redelivered_event_does_not_charge_twice(ctx, db, central, user_id):
    """서버는 핸들러가 돌아온 *뒤에* `processed_events`를 기록한다.

    차감 직후 죽으면 중앙봇이 같은 이벤트를 다시 보낸다. 그때 새 트랜잭션
    키를 만들면 두 번 청구된다 (§17.3 규칙 2).
    """
    listing = pg.hub_shop_listing(db, ctx.balance,
                                  content_version_id=ctx.content_version_id)
    target = listing["equipment"][0]["equipment_def_id"]

    custom_id, values = _select("buyequip", target)
    hub.handle_hub(ctx, user_id, custom_id, values, event_id="evt-중복")
    spent_once = central.balance
    owned_once = db.one("SELECT COUNT(*) AS n FROM owned_equipment "
                        "WHERE user_id = ?", (user_id,))["n"]

    # 같은 이벤트가 다시 도착한다.
    hub.handle_hub(ctx, user_id, custom_id, values, event_id="evt-중복")

    assert central.balance == spent_once, "두 번 청구되었습니다"
    assert db.one("SELECT COUNT(*) AS n FROM owned_equipment WHERE user_id = ?",
                  (user_id,))["n"] == owned_once


def test_two_different_events_are_two_purchases(ctx, db, user_id):
    """멱등성이 지나쳐 두 번째 구매를 삼켜서도 안 된다."""
    listing = pg.hub_shop_listing(db, ctx.balance,
                                  content_version_id=ctx.content_version_id)
    target = listing["equipment"][0]["equipment_def_id"]
    custom_id, values = _select("buyequip", target)

    hub.handle_hub(ctx, user_id, custom_id, values, event_id="evt-a")
    hub.handle_hub(ctx, user_id, custom_id, values, event_id="evt-b")

    assert db.one("SELECT COUNT(*) AS n FROM owned_equipment WHERE user_id = ?",
                  (user_id,))["n"] == 2


# =====================================================================
# §7.2 허브 상점 · §8.4 강화 · §20.5 연구
# =====================================================================
def test_buying_a_stone_from_the_screen(ctx, db, user_id):
    custom_id, values = _select("buystone", "1")
    hub.handle_hub(ctx, user_id, custom_id, values, event_id="evt-돌")
    row = db.one("SELECT amount FROM enhancement_stones WHERE user_id = ? "
                 "AND tier = 1", (user_id,))
    assert row and row["amount"] >= 1


def test_enhancing_from_the_screen_raises_the_tier(ctx, db, balance, user_id):
    """강화는 순수 로컬이다 — Central 왕복이 없어야 한다 (§8.4)."""
    listing = pg.hub_shop_listing(db, balance,
                                  content_version_id=ctx.content_version_id)
    target = listing["equipment"][0]["equipment_def_id"]
    hub.handle_hub(ctx, user_id, *_select("buyequip", target), event_id="evt-장비")

    item = db.one("SELECT equipment_instance_id FROM owned_equipment "
                  "WHERE user_id = ?", (user_id,))
    cost = pg.enhancement_cost(balance, 1)
    db.execute("INSERT INTO enhancement_stones (user_id, tier, amount) "
               "VALUES (?, 1, ?) ON CONFLICT(user_id, tier) DO UPDATE SET "
               "amount = amount + excluded.amount",
               (user_id, cost["current_tier_stones"] + 5))

    before = ctx.central.deduct_calls
    result = hub.handle_hub(
        ctx, user_id, *_select("enhance", str(item["equipment_instance_id"])),
        event_id="evt-강화")

    assert ctx.central.deduct_calls == before, "로컬 강화가 Central을 호출했습니다"
    assert db.one("SELECT tier FROM owned_equipment WHERE equipment_instance_id = ?",
                  (item["equipment_instance_id"],))["tier"] == 1
    assert "T1" in result["content"]


def test_the_research_screen_only_offers_unlocked_nodes(ctx, db, user_id):
    """§20.5 — 선행 업적을 채우지 못한 노드는 선택지에 올리지 않는다."""
    screen = handlers.research_screen(ctx, user_id)
    offered = {entry["value"] for entry in _options(screen, "research")}
    listing = pg.research_status(db, user_id=user_id,
                                 content_version_id=ctx.content_version_id)
    expected = {entry["node_id"] for entry in listing if entry["available"]}
    assert offered == expected


# =====================================================================
# 거절과 위조
# =====================================================================
def test_a_rejection_explains_itself_and_keeps_the_screen(ctx, db, user_id):
    """재화가 모자라 거절될 때 목록이 사라지면 무엇이 부족했는지 알 수 없다."""
    result = hub.handle_hub(ctx, user_id, *_select("starup", STARTER_CHARACTER_ID),
                            event_id="evt-부족")
    assert result["action"] == "reply_ephemeral"
    assert "부족" in result["content"]
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = ?",
                  (user_id, STARTER_CHARACTER_ID))["star_rank"] == 1


def test_an_unknown_action_is_refused(ctx, user_id):
    result = hub.handle_hub(ctx, user_id, f"{hub.HUB_PREFIX}없는동작", ["x"],
                            event_id="evt-이상")
    assert result["content"] == errors.ILLEGAL_STATE


def test_a_card_the_account_has_not_unlocked_cannot_be_upgraded(ctx, db, user_id):
    """`custom_id`는 위조할 수 있으니 엔진이 다시 본다."""
    result = hub.handle_hub(ctx, user_id, *_select("cardup", "card_광_각성"),
                            event_id="evt-위조")
    assert result["action"] == "reply_ephemeral"
    assert "해금" in result["content"]


# =====================================================================
# §16.2.3 런 중의 덱
# =====================================================================
def test_the_deck_screen_during_a_run_shows_the_frozen_deck(ctx, db, balance,
                                                            version, user_id):
    from app.content.seed import TUTORIAL_WORLD_ID

    lc.create_run(db, balance,
                  lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                                     party_character_ids=[STARTER_CHARACTER_ID],
                                     is_tutorial=True),
                  version)
    screen = handlers.deck_screen(ctx, user_id)
    assert "진행 중인 런" in screen["content"]
    assert "뽑을 더미" in screen["content"]
    # 런 중에는 강화 선택지를 내밀지 않는다 — 이 런에는 반영되지 않으므로
    # 여기서 누르게 하면 오해를 부른다.
    assert _options(screen, "cardup") == []


def test_the_collection_screen_shows_characters_as_cards(ctx, db, user_id):
    """캐릭터도 카드다 — 소장 목록에 함께 나온다 (§5)."""
    screen = handlers.deck_screen(ctx, user_id)
    assert "캐릭터" in screen["content"]
    assert screen["attachments"][0]["filename"] == "deckout_collection.png"
