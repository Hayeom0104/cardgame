"""§10.1–10.3 관리자 대시보드.

여기서 지키는 것 두 가지가 나머지 전부보다 중요하다.

* **비밀번호가 없으면 대시보드가 열리지 않는다.** 기본 비밀번호를 두면 설정을
  잊은 배포가 열린 관리자 화면으로 뜬다.
* **발행된 버전은 고칠 수 없다.** 진행 중인 런이 그 버전을 붙들고 있으므로
  (§10.6), 값을 바꾸면 런 도중에 내용이 달라진다.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.admin import auth, service
from app.content import versioning as vs

PASSWORD = "충분히-긴-관리자-비밀번호-1234"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import server
    from app.config import settings

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "admin.db"))
    monkeypatch.setattr(settings, "skip_capability_check", True)
    monkeypatch.setattr(settings, "central_api_key", "")
    monkeypatch.setattr(settings, "admin_password", PASSWORD)
    monkeypatch.setattr(settings, "admin_secret", "테스트-서명-키")

    with TestClient(server.app) as test_client:
        from app.content.balance import Balance
        from app.content.seed import seed_all

        version = seed_all(server.state["db"])
        server.state["content_version_id"] = version
        server.state["balance"] = Balance(server.state["db"], version)
        yield test_client


@pytest.fixture
def signed_in(client):
    response = client.post("/admin/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return client


def _db():
    from app.api import server

    return server.state["db"]


# =====================================================================
# 인증
# =====================================================================
def test_without_a_password_the_dashboard_does_not_open(client, monkeypatch):
    """기본 비밀번호는 두지 않는다 — 설정을 잊으면 꺼진 채로 뜬다."""
    from app.config import settings

    monkeypatch.setattr(settings, "admin_password", "")
    for path in ("/admin/", "/admin/login", "/admin/balance", "/admin/players"):
        assert client.get(path).status_code == 503, path
    # 로그인 시도조차 통과하지 못한다.
    assert client.post("/admin/login", json={"password": ""}).status_code == 503


def test_a_wrong_password_is_refused(client):
    assert client.post("/admin/login",
                       json={"password": "틀린비밀번호"}).status_code == 401


def test_an_unauthenticated_request_is_sent_to_login(client):
    response = client.get("/admin/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_a_forged_cookie_does_not_pass(client):
    client.cookies.set(auth.COOKIE_NAME, "9999999999.ZmFrZS1zaWduYXR1cmU")
    response = client.get("/admin/", follow_redirects=False)
    assert response.status_code == 303


def test_a_cookie_signed_with_another_key_does_not_pass(client, monkeypatch):
    """서명 키를 모르면 만료 시각만 바꿔 붙여도 통하지 않는다."""
    from app.config import settings

    monkeypatch.setattr(settings, "admin_secret", "남의-키")
    forged = auth.issue()
    monkeypatch.setattr(settings, "admin_secret", "테스트-서명-키")
    assert not auth.verify(forged)


def test_an_expired_cookie_does_not_pass(client):
    import time

    assert not auth.verify(auth.issue(now=time.time() - 100_000))


def test_a_valid_cookie_passes(client):
    assert auth.verify(auth.issue())


def test_https_login_marks_the_admin_cookie_secure(client):
    response = client.post(
        "/admin/login", json={"password": PASSWORD},
        headers={"x-forwarded-proto": "https"})
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie


def test_local_http_login_keeps_the_admin_cookie_usable(client):
    response = client.post("/admin/login", json={"password": PASSWORD})
    assert "secure" not in response.headers["set-cookie"].lower()


def test_logging_out_clears_the_session(signed_in):
    signed_in.get("/admin/logout", follow_redirects=False)
    assert signed_in.get("/admin/", follow_redirects=False).status_code == 303


def test_every_page_renders_for_a_signed_in_operator(signed_in):
    for path in ("/admin/", "/admin/balance", "/admin/content", "/admin/assets",
                 "/admin/versions", "/admin/players"):
        response = signed_in.get(path)
        assert response.status_code == 200, path
        assert "덱아웃 관리" in response.text


# =====================================================================
# §10.6 — 발행된 버전은 고칠 수 없다
# =====================================================================
def test_editing_targets_a_draft_not_the_published_version(signed_in):
    """편집 화면을 열면 초안이 만들어지고, 현재 버전은 그대로 남는다."""
    db = _db()
    published = vs.current_version_id(db)
    assert service.draft_version_id(db) is None

    signed_in.get("/admin/balance")

    draft = service.draft_version_id(db)
    assert draft is not None and draft != published
    assert vs.current_version_id(db) == published


def test_the_draft_starts_as_a_full_copy(signed_in):
    """빈 초안을 발행하면 콘텐츠가 통째로 사라진다 (§10.6은 완전한 스냅샷)."""
    db = _db()
    published = vs.current_version_id(db)
    signed_in.get("/admin/balance")
    draft = service.draft_version_id(db)

    for table in ("cards", "characters", "enemies", "balancing_constants"):
        before = db.one(f"SELECT COUNT(*) AS n FROM {table} "
                        f"WHERE content_version_id = ?", (published,))["n"]
        after = db.one(f"SELECT COUNT(*) AS n FROM {table} "
                       f"WHERE content_version_id = ?", (draft,))["n"]
        assert after == before, table


def test_the_service_refuses_to_write_a_published_version(signed_in):
    """화면을 우회해 서비스 층을 직접 불러도 막힌다."""
    db = _db()
    published = vs.current_version_id(db)
    with pytest.raises(service.AdminError, match="발행된 버전은 고칠 수 없습니다"):
        service.save_constant(db, published, "draws_per_turn", "9")


def test_a_saved_constant_does_not_touch_the_live_version(signed_in):
    db = _db()
    published = vs.current_version_id(db)

    signed_in.get("/admin/balance")
    response = signed_in.post("/admin/balance/save",
                              json={"key": "draws_per_turn", "value": "9"})
    assert response.json()["ok"]

    live = db.one("SELECT value_json FROM balancing_constants "
                  "WHERE content_version_id = ? AND key = 'draws_per_turn'",
                  (published,))
    assert json.loads(live["value_json"]) == 3

    draft = service.draft_version_id(db)
    edited = db.one("SELECT value_json FROM balancing_constants "
                    "WHERE content_version_id = ? AND key = 'draws_per_turn'",
                    (draft,))
    assert json.loads(edited["value_json"]) == 9


# =====================================================================
# 밸런싱 화면
# =====================================================================
def test_the_balance_screen_shows_the_explanations(signed_in):
    """설명이 없으면 코드를 아는 사람만 이 화면을 쓸 수 있다."""
    body = signed_in.get("/admin/balance").text
    assert "draws_per_turn" in body
    assert "뽑는 카드 수" in body
    assert "04_뽑기.toml" in body


def test_broken_json_is_refused(signed_in):
    signed_in.get("/admin/balance")
    response = signed_in.post("/admin/balance/save",
                              json={"key": "draws_per_turn", "value": "{{{"})
    assert response.status_code == 400
    assert "JSON" in response.json()["message"]


def test_an_unknown_constant_is_refused(signed_in):
    signed_in.get("/admin/balance")
    response = signed_in.post("/admin/balance/save",
                              json={"key": "없는설정", "value": "1"})
    assert response.status_code == 400


# =====================================================================
# 콘텐츠 편집
# =====================================================================
def test_a_content_row_can_be_edited_in_the_draft(signed_in):
    db = _db()
    signed_in.get("/admin/content")
    draft = service.draft_version_id(db)

    response = signed_in.post("/admin/content/cards/card_평타/save",
                              json={"values": {"name": "평타(수정)"}})
    assert response.json()["ok"]
    assert db.one("SELECT name FROM cards WHERE content_version_id = ? "
                  "AND card_id = 'card_평타'", (draft,))["name"] == "평타(수정)"


def test_the_logical_key_cannot_be_renamed(signed_in):
    """§10.6 — 플레이어가 가진 데이터가 그 id를 참조한다."""
    db = _db()
    signed_in.get("/admin/content")
    draft = service.draft_version_id(db)

    signed_in.post("/admin/content/cards/card_평타/save",
                   json={"values": {"card_id": "card_다른이름", "name": "평타"}})
    assert db.one("SELECT COUNT(*) AS n FROM cards WHERE content_version_id = ? "
                  "AND card_id = 'card_평타'", (draft,))["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM cards WHERE content_version_id = ? "
                  "AND card_id = 'card_다른이름'", (draft,))["n"] == 0


def test_broken_effects_json_is_refused_before_it_is_stored(signed_in):
    """깨진 효과 목록은 그 값을 처음 읽는 순간 — 대개 전투 중에 — 터진다."""
    signed_in.get("/admin/content")
    response = signed_in.post("/admin/content/cards/card_평타/save",
                              json={"values": {"effects_json": "[깨진 JSON"}})
    assert response.status_code == 400
    assert "effects_json" in response.json()["message"]


def test_an_unknown_table_is_refused(signed_in):
    assert signed_in.get("/admin/content/sqlite_master").status_code == 404
    assert signed_in.get("/admin/content/accounts").status_code == 404


# =====================================================================
# 발행
# =====================================================================
def test_publishing_a_valid_draft_makes_it_current(signed_in):
    db = _db()
    before = vs.current_version_id(db)
    signed_in.get("/admin/balance")
    draft = service.draft_version_id(db)

    response = signed_in.post("/admin/versions/publish")
    assert response.json()["ok"], response.json()
    assert vs.current_version_id(db) == draft != before


def test_publishing_refreshes_what_the_service_reads(signed_in):
    from app.api import server

    db = _db()
    signed_in.get("/admin/balance")
    signed_in.post("/admin/balance/save",
                   json={"key": "draws_per_turn", "value": "5"})
    signed_in.post("/admin/versions/publish")

    assert server.state["content_version_id"] == vs.current_version_id(db)
    assert int(server.state["balance"].get("draws_per_turn")) == 5


def test_an_invalid_draft_is_not_published(signed_in):
    """§10.5를 통과하지 못하면 아무것도 바뀌지 않는다."""
    db = _db()
    before = vs.current_version_id(db)
    signed_in.get("/admin/balance")
    draft = service.draft_version_id(db)

    # 뽑기 확률의 합을 1에서 벗어나게 만든다.
    db.execute("UPDATE balancing_constants SET value_json = ? "
               "WHERE content_version_id = ? AND key = 'gacha_base_rates'",
               (json.dumps({"top": 0.5, "mid": 0.5, "base": 0.5}), draft))

    response = signed_in.post("/admin/versions/publish")
    assert response.status_code == 400
    assert vs.current_version_id(db) == before


def test_validation_reports_the_reason_without_publishing(signed_in):
    db = _db()
    signed_in.get("/admin/balance")
    draft = service.draft_version_id(db)
    db.execute("UPDATE balancing_constants SET value_json = ? "
               "WHERE content_version_id = ? AND key = 'gacha_base_rates'",
               (json.dumps({"top": 0.5, "mid": 0.5, "base": 0.5}), draft))

    body = signed_in.post("/admin/versions/validate").json()
    assert body["ok"] is False and body["message"]


def test_discarding_a_draft_leaves_the_published_version_alone(signed_in):
    db = _db()
    published = vs.current_version_id(db)
    signed_in.get("/admin/balance")
    assert service.draft_version_id(db) is not None

    signed_in.post("/admin/versions/discard")
    assert service.draft_version_id(db) is None
    assert vs.current_version_id(db) == published
    assert db.one("SELECT COUNT(*) AS n FROM cards WHERE content_version_id = ?",
                  (published,))["n"] > 0


# =====================================================================
# 그림 업로드
# =====================================================================
def _png(color=(200, 40, 40)) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_an_uploaded_png_lands_where_the_game_looks_for_it(signed_in, tmp_path,
                                                           monkeypatch):
    from app.render.assets import AssetLibrary
    from app.render.theme import load as load_theme

    monkeypatch.setattr("app.render.assets.ROOT", tmp_path)
    monkeypatch.setattr("app.render.theme.ROOT", tmp_path)

    response = signed_in.post("/admin/assets/card/card_평타", content=_png())
    assert response.json()["ok"], response.json()

    library = AssetLibrary(load_theme())
    assert library.load("card", "card_평타") is not None


def test_a_file_that_is_not_an_image_is_refused(signed_in, tmp_path, monkeypatch):
    monkeypatch.setattr("app.render.assets.ROOT", tmp_path)
    monkeypatch.setattr("app.render.theme.ROOT", tmp_path)

    response = signed_in.post("/admin/assets/card/card_평타",
                              content="이건 그림이 아니다".encode("utf-8"))
    assert response.status_code == 400
    assert "이미지" in response.json()["message"]


def test_a_path_traversing_id_is_refused(signed_in, tmp_path, monkeypatch):
    """id로 파일 경로를 만들므로, 경로 문자가 섞이면 안 된다."""
    monkeypatch.setattr("app.render.assets.ROOT", tmp_path)
    monkeypatch.setattr("app.render.theme.ROOT", tmp_path)

    with pytest.raises(service.AdminError, match="경로 문자"):
        service.save_asset("card", "../../etc/passwd", _png())


def test_an_unknown_asset_kind_is_refused(signed_in):
    response = signed_in.post("/admin/assets/없는종류/x", content=_png())
    assert response.status_code == 400


# =====================================================================
# 플레이어와 운영 개입
# =====================================================================
@pytest.fixture
def player(signed_in):
    from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID, create_account
    from app.engine import lifecycle as lc

    from app.api import server

    db, version = _db(), server.state["content_version_id"]
    user_id = 5150
    create_account(db, user_id, version)
    run_id = lc.create_run(
        db, server.state["balance"],
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    return user_id, run_id


def test_a_player_page_shows_their_runs(signed_in, player):
    user_id, run_id = player
    body = signed_in.get(f"/admin/players/{user_id}").text
    assert str(run_id) in body
    assert "preparing" in body


def test_terminating_a_run_records_why(signed_in, player):
    """§16.1 `admin_terminated` — 정상적인 끝맺음이 아니라 운영 개입이다."""
    user_id, run_id = player
    response = signed_in.post(f"/admin/players/runs/{run_id}/terminate",
                              json={"reason": "지원 요청으로 정리"})
    assert response.json()["ok"]

    run = _db().one("SELECT state, end_reason FROM runs WHERE run_id = ?", (run_id,))
    assert run["state"] == "admin_terminated"
    assert "지원 요청으로 정리" in run["end_reason"]


def test_terminating_without_a_reason_is_refused(signed_in, player):
    _, run_id = player
    response = signed_in.post(f"/admin/players/runs/{run_id}/terminate",
                              json={"reason": "  "})
    assert response.status_code == 400
    assert _db().one("SELECT state FROM runs WHERE run_id = ?",
                     (run_id,))["state"] != "admin_terminated"


def test_a_finished_run_cannot_be_terminated_again(signed_in, player):
    _, run_id = player
    signed_in.post(f"/admin/players/runs/{run_id}/terminate", json={"reason": "정리"})
    second = signed_in.post(f"/admin/players/runs/{run_id}/terminate",
                            json={"reason": "또"})
    assert second.status_code == 400


def test_the_reap_action_settles_idle_runs(signed_in, player):
    from datetime import datetime, timedelta, timezone

    _, run_id = player
    stale = datetime.now(timezone.utc) - timedelta(days=1)
    _db().execute("UPDATE runs SET last_activity_at = ? WHERE run_id = ?",
                  (stale.isoformat(timespec="seconds"), run_id))

    assert signed_in.post("/admin/actions/reap").json()["ok"]
    assert _db().one("SELECT state FROM runs WHERE run_id = ?",
                     (run_id,))["state"] == "run_expired"


def test_the_dashboard_never_touches_the_central_bot_contract(signed_in):
    """`/event` 는 대시보드와 무관하게 그대로 동작해야 한다."""
    response = signed_in.post("/event", json={
        "type": "message", "user_id": 4242, "command": "덱아웃", "args": [],
        "raw_content": "!덱아웃"})
    assert response.status_code == 200
    assert response.json()["action"] in ("reply", "reply_ephemeral", "redirect")
