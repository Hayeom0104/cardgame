"""§1.3.5 — 런이 실제로 비공개 스레드를 얻는가.

`surface_request` 는 `thread_request` 를 응답에 담아 돌려주고 있었지만 그것을
호출로 바꾸는 코드가 없었다. 그래서 `!덱아웃 시작` 은 런 행만 만들고 스레드는
만들지 않았고, `runs.thread_id` 는 NULL 로 남았다 — §16.3의 "계정당 하나"
규칙 때문에 그 계정은 새 런도 시작할 수 없는 상태로 잠겼다.

여기 있는 테스트는 "스레드 API가 동작하는가"가 아니라 **"부르기는 하는가"** 를
본다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import handlers, screens
from app.central import surfaces
from app.content.seed import TUTORIAL_WORLD_ID

PARENT_CHANNEL = 12345


class FakeCentral:
    """스레드 API만 흉내 내는 중앙봇."""

    def __init__(self, *, fail: bool = False, response: dict | None = None):
        self.fail = fail
        self.response = response
        self.create_calls: list[dict] = []
        self.recreate_calls: list[dict] = []
        self._next_thread = 900000

    def _thread(self) -> dict:
        self._next_thread += 1
        return {"thread_id": self._next_thread, "message_id": 5555,
                "parent_channel_id": PARENT_CHANNEL}

    def create_thread(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("중앙봇이 응답하지 않습니다")
        return self.response if self.response is not None else self._thread()

    def recreate_thread(self, **kwargs):
        self.recreate_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("중앙봇이 응답하지 않습니다")
        return self.response if self.response is not None else self._thread()


@pytest.fixture
def central() -> FakeCentral:
    return FakeCentral()


@pytest.fixture
def ctx(db, balance, version, central):
    return handlers.HandlerContext(db=db, balance=balance, central=central,
                                   content_version_id=version)


def start_tutorial(ctx, user_id: int) -> dict:
    return handlers._prepare_and_materialize(
        ctx, user_id, TUTORIAL_WORLD_ID, is_tutorial=True)


# =====================================================================
# 요청이 실제 호출이 되는가
# =====================================================================
def test_starting_a_run_asks_for_a_thread(ctx, user_id):
    response = start_tutorial(ctx, user_id)
    assert "thread_request" in response, \
        "런을 시작했는데 스레드를 요청하지 않았습니다"


def test_the_request_becomes_a_real_call(ctx, db, central, user_id):
    response = start_tutorial(ctx, user_id)
    surfaces.fulfil_thread_request(db, central, response,
                                   parent_channel_id=PARENT_CHANNEL)

    assert len(central.create_calls) == 1, "create_thread 가 불리지 않았습니다"
    call = central.create_calls[0]
    assert call["parent_channel_id"] == PARENT_CHANNEL
    assert call["owner_user_id"] == user_id
    assert call["surface_generation"] >= 1


def test_the_run_is_bound_to_the_thread_that_was_created(ctx, db, central,
                                                         user_id):
    """이것이 없으면 런은 화면 없이 계정만 점유한다 (§16.3)."""
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, central, response,
                                   parent_channel_id=PARENT_CHANNEL)

    run = db.one("SELECT thread_id, canonical_message_id FROM runs "
                 "WHERE run_id = ?", (run_id,))
    assert run["thread_id"] == central._next_thread
    assert run["canonical_message_id"] == 5555


def test_internal_keys_do_not_leak_into_the_reply(ctx, db, central, user_id):
    """중앙봇에게 보내는 것은 화면이지 우리 배선이 아니다."""
    response = start_tutorial(ctx, user_id)
    cleaned = surfaces.fulfil_thread_request(
        db, central, response, parent_channel_id=PARENT_CHANNEL)
    assert "thread_request" not in cleaned
    assert "run_id" not in cleaned
    assert cleaned["content"]


def test_the_binding_is_recorded_against_the_intent(ctx, db, central, user_id):
    """§1.3.3 — 보내기 전에 적어 둔 intent 와 결과가 이어져야 한다."""
    response = start_tutorial(ctx, user_id)
    request_id = response["metadata"]["request_id"]
    assert db.one("SELECT 1 FROM delivery_intents WHERE request_id = ?",
                  (request_id,)) is not None

    surfaces.fulfil_thread_request(db, central, response,
                                   parent_channel_id=PARENT_CHANNEL)
    binding = db.one("SELECT * FROM discord_bindings WHERE request_id = ?",
                     (request_id,))
    assert binding is not None and binding["applied"] == 1


def test_a_response_without_a_thread_request_is_untouched(db, central):
    response = {"action": "reply", "content": "안녕"}
    assert surfaces.fulfil_thread_request(
        db, central, dict(response), parent_channel_id=PARENT_CHANNEL) == response
    assert central.create_calls == []


# =====================================================================
# 실패해도 런은 살아 있어야 한다
# =====================================================================
def test_a_failed_thread_call_does_not_lose_the_run(ctx, db, user_id):
    failing = FakeCentral(fail=True)
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]

    surfaces.fulfil_thread_request(db, failing, response,
                                   parent_channel_id=PARENT_CHANNEL)

    run = db.one("SELECT state, thread_id FROM runs WHERE run_id = ?", (run_id,))
    assert run is not None, "스레드 실패가 런을 지웠습니다"
    assert run["thread_id"] is None


def test_no_channel_configured_is_reported_not_guessed(ctx, db, central, user_id):
    response = start_tutorial(ctx, user_id)
    surfaces.fulfil_thread_request(db, central, response, parent_channel_id=0)
    assert central.create_calls == []


def test_an_unrecognised_response_shape_is_not_silently_dropped(ctx, db, user_id,
                                                               caplog):
    """연동 가이드의 응답 스키마가 저장소에 없다. 못 알아보면 로그로 드러낸다."""
    odd = FakeCentral(response={"unexpected": "shape"})
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]

    with caplog.at_level("ERROR"):
        surfaces.fulfil_thread_request(db, odd, response,
                                       parent_channel_id=PARENT_CHANNEL)
    assert "thread_id" in caplog.text
    assert db.one("SELECT thread_id FROM runs WHERE run_id = ?",
                  (run_id,))["thread_id"] is None


# =====================================================================
# §16.8 스레드가 지워졌을 때
# =====================================================================
def test_reopening_bumps_the_generation_and_actually_recreates(ctx, db, central,
                                                               user_id):
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, central, response,
                                   parent_channel_id=PARENT_CHANNEL)
    first = db.one("SELECT thread_id, surface_generation FROM runs "
                   "WHERE run_id = ?", (run_id,))

    # 중앙봇이 스레드 삭제를 알려 온다 (§16.8).
    from app.engine import lifecycle as lc

    run = db.one("SELECT logical_session_id FROM runs WHERE run_id = ?", (run_id,))
    lc.handle_thread_deleted(db, run["logical_session_id"])
    assert db.one("SELECT thread_id FROM runs WHERE run_id = ?",
                  (run_id,))["thread_id"] is None

    reopened = surfaces.reopen_thread(db, central, run_id,
                                      parent_channel_id=PARENT_CHANNEL)
    assert len(central.recreate_calls) == 1, "recreate_thread 가 불리지 않았습니다"
    after = db.one("SELECT thread_id, surface_generation FROM runs "
                   "WHERE run_id = ?", (run_id,))
    assert after["surface_generation"] == first["surface_generation"] + 1
    assert after["thread_id"] == reopened != first["thread_id"]


def test_the_hub_reopens_the_thread_and_redirects(ctx, db, central, user_id,
                                                  monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "parent_channel_id", PARENT_CHANNEL)

    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, central, response,
                                   parent_channel_id=PARENT_CHANNEL)

    run = db.one("SELECT logical_session_id FROM runs WHERE run_id = ?", (run_id,))
    from app.engine import lifecycle as lc

    lc.handle_thread_deleted(db, run["logical_session_id"])

    screen = handlers.hub_screen(ctx, user_id)
    assert screen["action"] == "redirect"
    assert screen["thread_id"] == db.one(
        "SELECT thread_id FROM runs WHERE run_id = ?", (run_id,))["thread_id"]


def test_the_hub_says_so_when_the_thread_cannot_be_reopened(ctx, db, user_id,
                                                            monkeypatch):
    from app.api import errors
    from app.config import settings
    from app.engine import lifecycle as lc

    monkeypatch.setattr(settings, "parent_channel_id", PARENT_CHANNEL)

    working = FakeCentral()
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, working, response,
                                   parent_channel_id=PARENT_CHANNEL)
    run = db.one("SELECT logical_session_id FROM runs WHERE run_id = ?", (run_id,))
    lc.handle_thread_deleted(db, run["logical_session_id"])

    ctx.central = FakeCentral(fail=True)
    screen = handlers.hub_screen(ctx, user_id)
    assert errors.SURFACE_UNAVAILABLE in screen["content"]
    # 런은 그대로다 — 다음 시도에 이어서 할 수 있다.
    assert db.one("SELECT state FROM runs WHERE run_id = ?", (run_id,)) is not None


# =====================================================================
# 서비스 전체를 통과할 때
# =====================================================================
def test_the_event_endpoint_opens_the_thread(tmp_path, monkeypatch):
    """핸들러를 직접 부르는 것이 아니라 `/event` 를 통과시켜 본다 —
    배선이 빠져 있던 곳이 바로 여기였다."""
    from app.api import server
    from app.config import settings

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "surface.db"))
    monkeypatch.setattr(settings, "skip_capability_check", True)
    monkeypatch.setattr(settings, "central_api_key", "")
    monkeypatch.setattr(settings, "parent_channel_id", PARENT_CHANNEL)

    fake = FakeCentral()
    with TestClient(server.app) as client:
        from app.content.balance import Balance
        from app.content.seed import seed_all

        version = seed_all(server.state["db"])
        server.state["content_version_id"] = version
        server.state["balance"] = Balance(server.state["db"], version)
        server.state["central"] = fake

        first = client.post("/event", json={
            "type": "message", "user_id": 4242, "guild_id": 1, "channel_id": 2,
            "command": "덱아웃", "args": [], "raw_content": "!덱아웃"})
        # 계정이 없는 첫 명령은 가입 프롬프트만 돌려준다 — 눌러야 계정이
        # 생긴다.
        # /event 는 컴포넌트를 액션 로우로 감싼다 — 한 단계 더 들어간다.
        buttons = [c for row in first.json()["components"]
                  for c in row.get("components", [row])]
        register_id = next(c["custom_id"] for c in buttons
                           if c["custom_id"].startswith("dko:reg:"))
        client.post("/event", json={
            "type": "interaction", "user_id": 4242, "guild_id": 1, "channel_id": 2,
            "custom_id": register_id, "values": []})
        reply = client.post("/event", json={
            "type": "message", "user_id": 4242, "guild_id": 1, "channel_id": 2,
            "command": "덱아웃", "args": ["시작"], "raw_content": "!덱아웃 시작"})

        assert reply.status_code == 200
        assert len(fake.create_calls) == 1, \
            "/event 를 통과했는데 스레드가 만들어지지 않았습니다"
        run = server.state["db"].one(
            "SELECT thread_id FROM runs WHERE user_id = 4242")
        assert run["thread_id"] is not None
        assert "thread_request" not in reply.json()


# =====================================================================
# §1.3.6 / §16.6 — 끝난 런의 화면을 마지막 상태로
# =====================================================================
class EditingCentral(FakeCentral):
    """스레드에 더해 메시지 편집까지 흉내 낸다."""

    def __init__(self, *, edit_fails: bool = False):
        super().__init__()
        self.edit_fails = edit_fails
        self.edits: list[dict] = []

    def edit_message(self, **kwargs):
        if kwargs["new_presentation_revision"] != \
                kwargs["expected_presentation_revision"] + 1:
            raise AssertionError("§1.3.6 — new는 expected + 1 이어야 합니다")
        self.edits.append(kwargs)
        if self.edit_fails:
            raise RuntimeError("중앙봇이 응답하지 않습니다")
        return {"status": "edited"}


def surfaced_run(ctx, db, central, user_id) -> int:
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, central, response,
                                   parent_channel_id=PARENT_CHANNEL)
    return run_id


def test_a_finished_run_gets_its_screen_rewritten(db, balance, version, user_id):
    central = EditingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)

    assert surfaces.close_run_surface(db, central, run_id, summary="끝났습니다")
    assert len(central.edits) == 1
    assert central.edits[0]["content"] == "끝났습니다"


def test_the_finished_screen_has_no_buttons_left(db, balance, version, user_id):
    """게이트가 막아 주더라도, 눌리는데 안 되는 버튼을 남겨 둘 이유는 없다."""
    central = EditingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)

    surfaces.close_run_surface(db, central, run_id, summary="끝났습니다")
    assert central.edits[0]["components"] == []


def test_the_revision_moves_only_after_the_edit_succeeds(db, balance, version,
                                                         user_id):
    """실패했는데 번호만 올라가면 그 뒤의 조작이 전부 STALE_REVISION 이 된다."""
    central = EditingCentral(edit_fails=True)
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)
    before = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                    (run_id,))["presentation_revision"]

    assert surfaces.close_run_surface(db, central, run_id, summary="끝") is False
    after = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                   (run_id,))["presentation_revision"]
    assert after == before

    queued = db.one("SELECT status FROM delivery_queue WHERE run_id = ?", (run_id,))
    assert queued["status"] == "failed"


def test_a_successful_edit_advances_the_revision_by_one(db, balance, version,
                                                        user_id):
    central = EditingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)
    before = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                    (run_id,))["presentation_revision"]

    surfaces.close_run_surface(db, central, run_id, summary="끝")
    after = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                   (run_id,))["presentation_revision"]
    assert after == before + 1
    assert db.one("SELECT status FROM delivery_queue WHERE run_id = ?",
                  (run_id,))["status"] == "sent"


def test_a_run_with_no_thread_is_skipped_quietly(db, balance, version, user_id):
    central = EditingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    response = start_tutorial(ctx, user_id)          # 스레드를 열지 않는다
    run_id = response["run_id"]

    assert surfaces.close_run_surface(db, central, run_id, summary="끝") is False
    assert central.edits == []


def test_abandoning_from_the_channel_closes_the_thread_screen(db, balance,
                                                              version, user_id):
    """포기는 채널에서 하고 화면은 스레드에 있다."""
    central = EditingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    surfaced_run(ctx, db, central, user_id)

    reply = handlers.abandon_run(ctx, user_id)
    assert "포기" in reply["content"]
    assert len(central.edits) == 1, "스레드에 지도가 그대로 남았습니다"
    assert central.edits[0]["components"] == []


def test_an_expired_run_closes_its_thread_screen(db, balance, version, user_id):
    central = EditingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)

    # 방치 시간을 넘긴 것으로 만든다 (§16.3).
    db.execute("UPDATE runs SET last_activity_at = '2020-01-01T00:00:00+00:00' "
               "WHERE run_id = ?", (run_id,))
    handlers.hub_screen(ctx, user_id)

    assert len(central.edits) == 1
    assert "정리했습니다" in central.edits[0]["content"]


def test_a_live_run_screen_is_not_touched(db, balance, version, user_id):
    """진행 중인 런의 화면은 인터랙션 응답이 고친다. 두 기록자가 생기면
    §16.6이 막으려던 경합이 된다."""
    central = EditingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    surfaced_run(ctx, db, central, user_id)

    handlers.hub_screen(ctx, user_id)          # 만료되지 않은 런
    assert central.edits == []
