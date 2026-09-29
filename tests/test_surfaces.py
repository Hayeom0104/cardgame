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
    assert f"<@{user_id}>" in call["content"]


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


def test_thread_open_renders_map_without_parent_controls(ctx, db, central, user_id):
    from app.api.events import InteractionEvent
    from app.api import custom_id

    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    result = surfaces.fulfil_thread_request(
        db, central, response, parent_channel_id=PARENT_CHANNEL)
    assert result["action"] == "redirect"
    assert "components" not in result
    assert "metadata" not in result
    button = central.create_calls[0]["components"][0]
    assert custom_id.parse(button["custom_id"]).action == custom_id.ACTION_SCREEN_REFRESH
    screen = handlers.handle_interaction(ctx, InteractionEvent(
        user_id=user_id, guild_id=1, channel_id=result["thread_id"],
        thread_id=result["thread_id"], custom_id=button["custom_id"]))
    assert screen["action"] == "edit"
    assert screen["attachments"], "지도 PNG가 실제 응답에 있어야 합니다"
    assert screen["components"]
    assert db.one("SELECT state FROM runs WHERE run_id = ?", (run_id,))["state"] == "map_navigation"


def test_old_parent_button_redirects_without_advancing_run(ctx, db, central, user_id, monkeypatch):
    from app.api.events import InteractionEvent
    from app.api import controls
    monkeypatch.setattr(handlers.settings, "parent_channel_id", PARENT_CHANNEL)
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, central, response, parent_channel_id=PARENT_CHANNEL)
    before = dict(db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
    button = controls.game_map(db, run_id)[0]
    result = handlers.handle_interaction(ctx, InteractionEvent(
        user_id=user_id, guild_id=1, channel_id=PARENT_CHANNEL,
        custom_id=button["custom_id"]))
    assert result["action"] == "redirect"
    assert dict(db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))) == before


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

    result = surfaces.fulfil_thread_request(db, failing, response,
                                           parent_channel_id=PARENT_CHANNEL)
    assert "components" not in result
    assert "metadata" not in result

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
    배선이 빠져 있던 곳이 바로 여기였다.

    A-1.1로 가입이 튜토리얼 런을 곧장 만들므로, 스레드는 가입 클릭 자체가
    연다. 뒤이은 `!덱아웃 시작`은 이미 있는 런으로 리다이렉트할 뿐, 새
    스레드를 만들지 않는다.
    """
    from app.api import server
    from app.config import settings

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "surface.db"))
    monkeypatch.setattr(settings, "skip_capability_check", True)
    monkeypatch.setattr(settings, "allow_unauthenticated_local", True)
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
                           if c["custom_id"].startswith("dko:hub:join:"))
        client.post("/event", json={
            "type": "interaction", "user_id": 4242, "guild_id": 1, "channel_id": 2,
            "custom_id": register_id, "values": []})

        assert len(fake.create_calls) == 1, \
            "가입 클릭으로 튜토리얼 런이 열렸는데 스레드가 만들어지지 않았습니다"
        run = server.state["db"].one(
            "SELECT thread_id FROM runs WHERE user_id = 4242")
        assert run["thread_id"] is not None

        reply = client.post("/event", json={
            "type": "message", "user_id": 4242, "guild_id": 1, "channel_id": 2,
            "command": "덱아웃", "args": ["시작"], "raw_content": "!덱아웃 시작"})

        assert reply.status_code == 200
        assert len(fake.create_calls) == 1, \
            "이미 진행 중인 런인데 스레드를 새로 만들었습니다"
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


@pytest.mark.parametrize('status', [200, 409])
def test_rejected_edit_preserves_revision_and_logs_only_safe_code(
        db, balance, version, user_id, caplog, status):
    import httpx
    class ConflictingCentral(EditingCentral):
        def edit_message(self, **kwargs):
            result = {'detail': {'code': 'rejected_binding', 'private': 'DO-NOT-LOG-THIS'}}
            if status == 409:
                response = httpx.Response(409, json=result,
                    request=httpx.Request('POST', 'http://central/v1/minigames/messages/edit'))
                response.raise_for_status()
            return result
    central = ConflictingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central, content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)
    before = dict(db.one('SELECT * FROM runs WHERE run_id = ?', (run_id,)))
    assert not surfaces.close_run_surface(db, central, run_id, summary='정산 완료')
    assert dict(db.one('SELECT * FROM runs WHERE run_id = ?', (run_id,))) == before
    assert 'rejected_binding' in caplog.text
    assert 'DO-NOT-LOG-THIS' not in caplog.text
    assert db.one('SELECT status FROM delivery_queue WHERE run_id = ?', (run_id,))['status'] == 'failed'


def test_failed_frame_retries_the_identical_payload_and_id(db, balance, version, user_id):
    central = EditingCentral(edit_fails=True)
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central, content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)
    assert not surfaces.close_run_surface(db, central, run_id, summary='첫 화면')
    central.edit_fails = False
    assert surfaces.close_run_surface(db, central, run_id, summary='나중 화면')
    assert central.edits[0] == central.edits[1]
    assert central.edits[1]['content'] == '첫 화면'


def test_an_edit_response_cannot_rewind_a_concurrent_game_revision(db, balance, version, user_id):
    class RacingCentral(EditingCentral):
        def edit_message(self, **kwargs):
            db.execute('UPDATE runs SET presentation_revision = presentation_revision + 5')
            return {'status': 'edited'}
    central = RacingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central, content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)
    before = db.one('SELECT presentation_revision FROM runs WHERE run_id = ?', (run_id,))[0]
    assert not surfaces.close_run_surface(db, central, run_id, summary='종료')
    assert db.one('SELECT presentation_revision FROM runs WHERE run_id = ?', (run_id,))[0] == before + 5


# =====================================================================
# 운영 409 회귀 — Central의 편집 revision은 로컬 CAS 카운터와 별개다
# =====================================================================
class RevisionCheckingCentral(EditingCentral):
    """실제 Central처럼 바인딩마다 편집 revision을 0에서 시작하고, 정확히
    current → current+1 인 편집만 받는다. 응답 경로의 클릭 편집은 이 값을
    모른다 — 그래서 로컬 CAS 값을 보내면 409가 난다(운영 로그 재현)."""

    def __init__(self):
        super().__init__()
        self.revision = 0

    def edit_message(self, **kwargs):
        import httpx

        self.edits.append(kwargs)
        if kwargs["expected_presentation_revision"] != self.revision:
            request = httpx.Request("POST", "http://central/v1/minigames/messages/edit")
            response = httpx.Response(409, json={"status": "stale_revision"},
                                      request=request)
            raise httpx.HTTPStatusError("409", request=request, response=response)
        self.revision = kwargs["new_presentation_revision"]
        return {"status": "edited"}


def test_a_run_that_was_played_can_still_close_its_thread_screen(db, balance, version,
                                                                 user_id):
    """버튼을 몇 번 누른 뒤(로컬 CAS가 오른 뒤) 끝난 런의 화면을 닫는
    편집이 Central에 받아들여져야 한다. 예전엔 로컬 값을 그대로 보내서
    첫 클릭 이후 항상 409였고, 끝난 스레드에 살아 있는 버튼이 남았다."""
    from app.engine import lifecycle as lc

    central = RevisionCheckingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)
    for revision in range(4):                      # 플레이어가 네 번 누른 셈
        lc.claim_mutation(db, run_id, revision)

    assert surfaces.close_run_surface(db, central, run_id, summary="끝났습니다")
    assert central.edits[-1]["expected_presentation_revision"] == 0
    run = db.one("SELECT presentation_revision, central_revision FROM runs "
                 "WHERE run_id = ?", (run_id,))
    assert run["central_revision"] == 1
    assert run["presentation_revision"] == 5


def test_consecutive_frames_follow_central_s_revision(db, balance, version, user_id):
    from app.engine import lifecycle as lc

    central = RevisionCheckingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)
    lc.claim_mutation(db, run_id, 0)
    assert surfaces.push_frame(db, central, run_id, content="하나")
    lc.claim_mutation(db, run_id, 2)
    assert surfaces.push_frame(db, central, run_id, content="둘")
    assert [e["expected_presentation_revision"] for e in central.edits] == [0, 1]


def test_a_new_thread_binding_restarts_central_s_revision(db, balance, version, user_id):
    central = RevisionCheckingCentral()
    ctx = handlers.HandlerContext(db=db, balance=balance, central=central,
                                  content_version_id=version)
    run_id = surfaced_run(ctx, db, central, user_id)
    db.execute("UPDATE runs SET central_revision = 7 WHERE run_id = ?", (run_id,))
    surfaces._bind(db, "dko-thread-new", {"thread_id": 424242, "message_id": 5},
                   run_id=run_id, surface_generation=1)
    # 알 수 없는 request_id는 적용되지 않으므로 값도 그대로여야 한다.
    assert db.one("SELECT central_revision FROM runs WHERE run_id = ?",
                  (run_id,))["central_revision"] == 7


def test_migration_11_backfills_central_revision_from_sent_frames(tmp_path):
    from app.db.connection import Database

    db = Database(tmp_path / "old.db")
    db.migrate()
    db.execute(
        "INSERT INTO runs (run_id, user_id, world_id, state, is_tutorial, "
        "content_version_id, map_seed, rng_seed, rng_counter, deepest_depth_reached, "
        "run_currency, logical_session_id, surface_generation, presentation_revision, "
        "created_at, updated_at, last_activity_at) VALUES "
        "(5, 1, 'w', 'run_abandoned', 0, 1, 1, 1, 0, 1, 0, 'dko-run-5', 2, 9, 't', 't', 't')")
    for rid, status in (("dko-frame-5-g1-r3", "sent"), ("dko-frame-5-g2-r4", "sent"),
                        ("dko-frame-5-g2-r6", "sent"), ("dko-frame-5-g2-r9", "failed")):
        db.execute("INSERT INTO delivery_queue (run_id, target_revision, "
                   "delivery_request_id, payload_json, status, created_at) "
                   "VALUES (5, 0, ?, '{}', ?, 't')", (rid, status))
    db.execute("ALTER TABLE runs DROP COLUMN central_revision")
    db.execute("UPDATE schema_version SET version = 10")

    assert db.migrate() == 11
    # 지금 세대(2)에서 실제로 성공한 편집 두 번만 센다.
    assert db.one("SELECT central_revision FROM runs WHERE run_id = 5")[0] == 2


# =====================================================================
# 스레드 제목 — 디스코드 숫자 ID를 드러내지 않는다 (오너 지시)
# =====================================================================
def test_the_thread_is_named_after_the_display_name_not_the_discord_id(
        ctx, db, central, user_id):
    db.execute("UPDATE accounts SET display_name = ? WHERE user_id = ?",
               ("月冴", user_id))
    surfaced_run(ctx, db, central, user_id)
    name = central.create_calls[-1]["thread_name"]
    assert name == "덱아웃 - 月冴"
    assert str(user_id) not in name


def test_an_unknown_display_name_never_falls_back_to_the_discord_id(
        ctx, db, central, user_id):
    surfaced_run(ctx, db, central, user_id)
    name = central.create_calls[-1]["thread_name"]
    assert str(user_id) not in name
    assert name == "덱아웃 런"


# =====================================================================
# 다시 연 스레드의 첫 메시지 — 가이드 §8 필수 필드 + 누를 버튼
# =====================================================================
def test_a_reopened_thread_carries_a_button_that_opens_the_current_screen(
        ctx, db, central, user_id):
    """연동 가이드 §8 — recreate 요청에도 content·embeds·components가 필요하다.
    예전엔 셋 다 빠져 있었고, 다시 생긴 스레드에는 누를 것이 없었다."""
    from app.api import events as ev
    from app.engine import lifecycle as lc

    run_id = surfaced_run(ctx, db, central, user_id)
    run = db.one("SELECT logical_session_id FROM runs WHERE run_id = ?", (run_id,))
    lc.handle_thread_deleted(db, run["logical_session_id"])
    thread_id = surfaces.reopen_thread(db, central, run_id,
                                       parent_channel_id=PARENT_CHANNEL)

    call = central.recreate_calls[-1]
    assert f"<@{user_id}>" in call["content"]
    button = call["components"][0]
    reply = handlers.handle_interaction(ctx, ev.InteractionEvent(
        event_id=None, user_id=user_id, guild_id=1, channel_id=thread_id,
        thread_id=thread_id, custom_id=button["custom_id"], values=[]))
    assert reply["action"] != "reply_ephemeral", reply
    assert reply["components"], "현재 화면의 조작이 와야 합니다"


def test_the_recreate_request_body_has_the_guide_s_required_fields():
    """가이드 §8: create/recreate는 최소한 이 필드들을 싣는다."""
    from app.central.client import CentralClient

    sent = {}
    client = CentralClient("http://central", "key")
    client._post = lambda path, body, **kw: sent.update(path=path, body=body) or {}
    client.recreate_thread(logical_session_id="dko-run-1", surface_generation=2,
                           parent_channel_id=1, owner_user_id=2, thread_name="덱아웃 런",
                           content="다시", components=[{"type": "button", "label": "a",
                                                       "custom_id": "x"}])
    body = sent["body"]
    for key in ("logical_session_id", "surface_generation", "parent_channel_id",
                "owner_user_id", "thread_name", "content", "embeds", "components"):
        assert key in body, key
    assert body["components"][0]["type"] == 1          # action row


# =====================================================================
# 운영 ReadTimeout — Central의 스레드 생성이 1.5초를 넘길 때
# =====================================================================
class SlowCentral(FakeCentral):
    """첫 생성 호출은 응답이 늦다(그 사이 Central은 스레드를 만든다). 같은
    세대로 다시 부르면 같은 스레드를 돌려준다 — 실제 Central의 멱등성."""

    def __init__(self, slow_calls: int = 1):
        super().__init__()
        self.slow_calls = slow_calls
        self.made: dict[tuple, dict] = {}
        self.timeouts: list = []

    def create_thread(self, **kwargs):
        import httpx

        self.create_calls.append(kwargs)
        self.timeouts.append(kwargs.get("timeout"))
        key = (kwargs["logical_session_id"], kwargs["surface_generation"])
        thread = self.made.setdefault(key, self._thread())
        if len(self.create_calls) <= self.slow_calls:
            raise httpx.ReadTimeout("timed out")
        return thread


def _slow_ctx(db, balance, version, central):
    return handlers.HandlerContext(db=db, balance=balance, central=central,
                                   content_version_id=version)


def test_a_slow_thread_create_is_finished_in_the_background(db, balance, version,
                                                            user_id):
    from app.engine import lifecycle as lc

    central = SlowCentral()
    ctx = _slow_ctx(db, balance, version, central)
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    reply = surfaces.fulfil_thread_request(db, central, response,
                                           parent_channel_id=PARENT_CHANNEL)
    assert reply["content"] == surfaces.SURFACE_PENDING_MESSAGE
    surfaces.wait_for_background(5)

    run = db.one("SELECT thread_id, state, surface_generation FROM runs WHERE run_id = ?",
                 (run_id,))
    assert run["thread_id"] == central.made[("dko-run-%d" % run_id, 1)]["thread_id"]
    assert run["state"] == lc.MAP_NAVIGATION
    assert run["surface_generation"] == 1, "새 스레드를 만들면 안 됩니다"
    assert len({(c["logical_session_id"], c["surface_generation"])
                for c in central.create_calls}) == 1
    assert central.timeouts[-1] == pytest.approx(15.0)


def test_the_hub_does_not_open_a_second_thread_while_one_is_pending(
        db, balance, version, user_id, monkeypatch):
    import threading

    from app.config import settings

    monkeypatch.setattr(settings, "parent_channel_id", PARENT_CHANNEL)
    gate = threading.Event()

    class BlockedCentral(SlowCentral):
        def create_thread(self, **kwargs):
            if kwargs.get("timeout"):
                gate.wait(5)                     # 백그라운드 호출이 아직 안 끝났다
            return super().create_thread(**kwargs)

    central = BlockedCentral()
    ctx = _slow_ctx(db, balance, version, central)
    response = start_tutorial(ctx, user_id)
    surfaces.fulfil_thread_request(db, central, response,
                                   parent_channel_id=PARENT_CHANNEL)
    calls = len(central.create_calls)
    reply = handlers.hub_screen(ctx, user_id)
    assert reply["content"] == surfaces.SURFACE_PENDING_MESSAGE
    assert len(central.create_calls) == calls
    gate.set()
    surfaces.wait_for_background(5)


def test_a_never_bound_run_is_retried_at_the_same_generation(
        db, balance, version, user_id, monkeypatch):
    """예전엔 허브가 이 런을 '스레드가 지워진 런'으로 보고 세대를 올려 새
    스레드를 만들었다 — Central이 늦게 만든 첫 스레드는 버려진 채 남았다."""
    from app.config import settings

    monkeypatch.setattr(settings, "parent_channel_id", PARENT_CHANNEL)
    central = FakeCentral(fail=True)
    ctx = _slow_ctx(db, balance, version, central)
    response = start_tutorial(ctx, user_id)
    run_id = response["run_id"]
    surfaces.fulfil_thread_request(db, central, response,
                                   parent_channel_id=PARENT_CHANNEL)
    central.fail = False
    reply = handlers.hub_screen(ctx, user_id)
    assert reply["action"] == "redirect"
    run = db.one("SELECT surface_generation, thread_id FROM runs WHERE run_id = ?",
                 (run_id,))
    assert run["surface_generation"] == 1
    assert central.recreate_calls == []
    assert run["thread_id"] == reply["thread_id"]
