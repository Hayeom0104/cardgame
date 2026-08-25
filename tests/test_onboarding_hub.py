"""Design Addendum A-1 — join gate, hub dashboard, `!덱아웃 도움말`."""

from __future__ import annotations

import pytest

from app.api import custom_id as cid
from app.api import errors
from app.api import events as ev
from app.api import handlers
from app.api import hub
from app.content.seed import STARTER_CHARACTER_ID, create_account
from app.engine import lifecycle as lc


class FakeCentral:
    def __init__(self, *, profile: dict | None = None):
        self._next = 900000
        self.profile = profile or {}
        self.create_calls: list[dict] = []

    def create_thread(self, **kwargs):
        self._next += 1
        self.create_calls.append(kwargs)
        return {"thread_id": self._next, "message_id": 11}

    def get_user(self, user_id):
        self.get_user_calls = getattr(self, "get_user_calls", 0) + 1
        return self.profile


@pytest.fixture
def central() -> FakeCentral:
    return FakeCentral(profile={"balance": 500, "username": "테스트유저"})


@pytest.fixture
def ctx(db, balance, version, central):
    return handlers.HandlerContext(db=db, balance=balance, central=central,
                                   content_version_id=version)


def _message(user_id: int, *args: str) -> ev.MessageEvent:
    return ev.MessageEvent(event_id=f"e-{user_id}-{args}", user_id=user_id, guild_id=1,
                           channel_id=2, command="덱아웃", args=list(args),
                           raw_content="!덱아웃 " + " ".join(args))


def _interaction(user_id: int, custom_id: str, values=None) -> ev.InteractionEvent:
    return ev.InteractionEvent(event_id=f"i-{user_id}", user_id=user_id, guild_id=1,
                               channel_id=2, custom_id=custom_id, values=values or [])


def _join_button(screen: dict) -> str:
    components = screen.get("components") or []
    return next(c["custom_id"] for c in components
               if c["custom_id"].startswith(f"{hub.HUB_PREFIX}join:"))


# =====================================================================
# A-1.1 join flow
# =====================================================================
def test_the_join_button_encodes_the_target_user(ctx, db):
    fresh_id = 810001
    screen = handlers.handle_message(ctx, _message(fresh_id))
    button_id = _join_button(screen)
    assert button_id == f"{hub.HUB_PREFIX}join:{cid.to_base36(fresh_id)}"


def test_someone_else_cannot_click_your_join_button(ctx, db):
    fresh_id, clicker_id = 810002, 810003
    screen = handlers.handle_message(ctx, _message(fresh_id))
    button_id = _join_button(screen)

    reply = handlers.handle_interaction(ctx, _interaction(clicker_id, button_id))

    assert reply["action"] == "reply_ephemeral"
    assert reply["content"] == errors.NOT_OWNER
    assert db.one("SELECT 1 FROM accounts WHERE user_id = ?", (fresh_id,)) is None
    assert db.one("SELECT 1 FROM accounts WHERE user_id = ?", (clicker_id,)) is None


def test_joining_creates_the_account_and_enters_the_tutorial(ctx, db):
    fresh_id = 810004
    screen = handlers.handle_message(ctx, _message(fresh_id))
    button_id = _join_button(screen)

    reply = handlers.handle_interaction(ctx, _interaction(fresh_id, button_id))

    assert reply["action"] == "edit"
    assert "가입되었습니다" in reply["content"]
    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (fresh_id,))
    assert account is not None
    run = lc.active_run_for(db, fresh_id)
    assert run is not None
    assert run["is_tutorial"] == 1


def test_a_double_click_race_does_not_error_or_restart(ctx, db):
    """§A-1.1 idempotency guard — 계정이 이미 있었다면 조용히 허브만."""
    fresh_id = 810005
    create_account(db, fresh_id, ctx.content_version_id)   # 경합을 흉내낸다
    button_id = f"{hub.HUB_PREFIX}join:{cid.to_base36(fresh_id)}"

    reply = handlers.handle_interaction(ctx, _interaction(fresh_id, button_id))

    assert reply["action"] == "edit"
    assert "가입되었습니다" not in reply["content"]
    count = db.one("SELECT COUNT(*) AS n FROM accounts WHERE user_id = ?",
                   (fresh_id,))["n"]
    assert count == 1
    # 경합 케이스는 런을 새로 만들지 않는다 — 허브만 보여준다.
    assert lc.active_run_for(db, fresh_id) is None


def test_the_join_screen_renders_without_a_promo_asset(ctx, db):
    """정적 홍보 그림이 없어도(A-1.4, 콘텐츠 저작 전) 화면은 정상 응답한다."""
    screen = handlers.handle_message(ctx, _message(810006))
    assert screen["action"] == "reply"
    assert screen["attachments"] == []   # 에셋 파일이 없으므로 빈 목록


# =====================================================================
# A-1.2 hub dashboard
# =====================================================================
@pytest.fixture
def graduated_user(ctx, db) -> int:
    """튜토리얼을 마친 계정 — 허브가 리다이렉트 없이 대시보드를 그린다."""
    user_id = 810100
    create_account(db, user_id, ctx.content_version_id)
    db.execute("UPDATE accounts SET tutorial_completed_at = ? WHERE user_id = ?",
              ("2026-01-01T00:00:00+00:00", user_id))
    return user_id


def test_the_dashboard_renders_an_image(ctx, graduated_user):
    screen = handlers.hub_screen(ctx, graduated_user)
    assert screen["attachments"], "허브 대시보드 그림이 없습니다"


def test_the_dashboard_shows_the_nickname_from_central(ctx, graduated_user):
    name = handlers.display_name(ctx, graduated_user)
    assert name == "테스트유저"


def test_the_hub_fetches_the_central_profile_only_once(ctx, graduated_user, central):
    """R3 M-01 — the coin line and the A-1.2 dashboard used to each call
    `GET /v1/users/{id}` separately, pushing a slow Central over the 2.5s
    message budget for one hub render."""
    handlers.hub_screen(ctx, graduated_user)
    assert central.get_user_calls == 1


def test_display_name_falls_back_when_central_has_no_known_key(db, balance, version):
    ctx = handlers.HandlerContext(db=db, balance=balance,
                                  central=FakeCentral(profile={"weird_key": 1}),
                                  content_version_id=version)
    assert handlers.display_name(ctx, 999) == "플레이어 999"


def test_quick_action_buttons_cover_every_documented_action(ctx, graduated_user):
    screen = handlers.hub_screen(ctx, graduated_user)
    codes = {c["custom_id"].split(":")[2] for c in screen["components"]
            if c["custom_id"].startswith(hub.HUB_PREFIX)
            and c["custom_id"].split(":")[2] in dict(handlers._QUICK_ACTION_BUTTONS)}
    assert codes == {"st", "dk", "gc", "ch", "rs", "sp", "ac"}


def test_someone_else_cannot_click_your_quick_action_button(ctx, graduated_user):
    other_id = graduated_user + 1
    screen = handlers.hub_screen(ctx, graduated_user)
    deck_button = next(c["custom_id"] for c in screen["components"]
                       if c["custom_id"].startswith(f"{hub.HUB_PREFIX}dk:"))

    reply = handlers.handle_interaction(ctx, _interaction(other_id, deck_button))
    assert reply["action"] == "reply_ephemeral"
    assert reply["content"] == errors.NOT_OWNER


def test_the_deck_quick_action_matches_the_text_command(ctx, graduated_user):
    screen = handlers.hub_screen(ctx, graduated_user)
    deck_button = next(c["custom_id"] for c in screen["components"]
                       if c["custom_id"].startswith(f"{hub.HUB_PREFIX}dk:"))

    via_button = handlers.handle_interaction(ctx, _interaction(graduated_user, deck_button))
    via_command = handlers.deck_screen(ctx, graduated_user)

    assert via_button["action"] == "edit"
    assert via_button["content"] == via_command["content"]


def test_max_passive_slots_reflects_the_research_tree(ctx):
    # §9.2 seed content unlocks passive slots up to 4.
    assert handlers._max_passive_slots(ctx) == 4


# =====================================================================
# A-1.3 !덱아웃 도움말
# =====================================================================
def test_help_lists_every_documented_subcommand():
    screen = handlers.help_screen()
    assert screen["action"] == "reply"
    for word in ("시작", "덱", "뽑기", "캐릭터", "장비", "연구", "상점", "업적", "포기"):
        assert word in screen["content"]


def test_help_is_reachable_before_the_tutorial_is_completed(ctx, db):
    fresh_id = 810200
    create_account(db, fresh_id, ctx.content_version_id)
    screen = handlers.handle_message(ctx, _message(fresh_id, "도움말"))
    assert screen["action"] == "reply"
    assert screen["content"] != errors.TUTORIAL_NOT_CLEARED


def test_other_commands_stay_blocked_even_with_a_tutorial_run_active(ctx, db):
    """A-1.1 이후 가입이 곧장 튜토리얼 런을 만들므로, 런이 있다는 사실만으로
    강제 튜토리얼 게이트를 풀어 주면 안 된다."""
    fresh_id = 810201
    create_account(db, fresh_id, ctx.content_version_id)
    handlers.start_run(ctx, fresh_id)   # 튜토리얼 런을 연다
    assert lc.active_run_for(db, fresh_id) is not None

    screen = handlers.handle_message(ctx, _message(fresh_id, "뽑기"))
    assert screen["action"] == "reply_ephemeral"
    assert screen["content"] == errors.TUTORIAL_NOT_CLEARED
