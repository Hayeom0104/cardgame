"""§10.7 되돌리기 — the admin dashboard's git-mirror history and revert flow."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

PASSWORD = "충분히-긴-관리자-비밀번호-1234"


def _init_repo(path) -> str:
    repo = str(path)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.com"],
                   check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
    return repo


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import server
    from app.config import settings

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "admin.db"))
    monkeypatch.setattr(settings, "skip_capability_check", True)
    monkeypatch.setattr(settings, "central_api_key", "")
    monkeypatch.setattr(settings, "admin_password", PASSWORD)
    monkeypatch.setattr(settings, "admin_secret", "테스트-서명-키")
    monkeypatch.setattr(settings, "content_repo_path", _init_repo(tmp_path / "repo"))
    monkeypatch.setattr(settings, "content_repo_push", False)

    with TestClient(server.app) as test_client:
        from app.content.balance import Balance
        from app.content.seed import seed_all

        version = seed_all(server.state["db"])   # publishes v1 — first sync
        server.state["content_version_id"] = version
        server.state["balance"] = Balance(server.state["db"], version)
        yield test_client


@pytest.fixture
def signed_in(client):
    response = client.post("/admin/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return client


def _publish_a_rename(signed_in) -> None:
    signed_in.post("/admin/versions/draft")
    response = signed_in.post("/admin/content/cards/card_평타/save",
                              json={"values": {"name": "평타(수정)"}})
    assert response.json()["ok"], response.json()
    response = signed_in.post("/admin/versions/publish")
    assert response.json()["ok"], response.json()


def test_a_publish_appears_on_the_versions_page(signed_in):
    response = signed_in.get("/admin/versions")
    assert response.status_code == 200
    assert "GitHub 동기화" in response.text
    assert "커밋만" in response.text or "push 완료" in response.text


def test_the_content_row_page_offers_history_when_sync_is_on(signed_in):
    response = signed_in.get("/admin/content/cards/card_평타")
    assert response.status_code == 200
    assert "이력 (§10.7)" in response.text
    assert "이력 보기" in response.text


def test_the_content_row_page_hides_history_when_sync_is_off(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "content_repo_path", "")
    client.post("/admin/login", json={"password": PASSWORD})
    response = client.get("/admin/content/cards/card_평타")
    assert response.status_code == 200
    assert "이력 (§10.7)" not in response.text


def test_history_grows_after_a_second_publish(signed_in):
    before = signed_in.get("/admin/content/cards/card_평타/history").json()
    assert len(before["history"]) == 1

    _publish_a_rename(signed_in)

    after = signed_in.get("/admin/content/cards/card_평타/history").json()
    assert len(after["history"]) == 2


def test_the_diff_shows_the_field_that_changed(signed_in):
    _publish_a_rename(signed_in)
    history = signed_in.get("/admin/content/cards/card_평타/history").json()["history"]
    original_commit = history[-1]["commit_sha"]

    diff = signed_in.post("/admin/content/cards/card_평타/revert-diff",
                          json={"commit_sha": original_commit}).json()
    assert diff["ok"]
    name_field = next(f for f in diff["fields"] if f["column"] == "name")
    assert name_field["current"] == "평타(수정)"
    assert name_field["picked"] == "평타"


def test_reverting_restores_the_value_and_creates_a_new_version(signed_in):
    from app.content import versioning as vs

    _publish_a_rename(signed_in)
    history = signed_in.get("/admin/content/cards/card_평타/history").json()["history"]
    original_commit = history[-1]["commit_sha"]

    response = signed_in.post("/admin/content/cards/card_평타/revert",
                              json={"commit_sha": original_commit})
    body = response.json()
    assert body["ok"], body

    from app.api import server

    db = server.state["db"]
    current = vs.current_version_id(db)
    row = db.one("SELECT name FROM cards WHERE content_version_id = ? "
                 "AND card_id = 'card_평타'", (current,))
    assert row["name"] == "평타"
    assert current == 3   # v1 (seed) → v2 (rename) → v3 (revert) — never rewinds


def test_revert_of_an_unresolved_commit_is_rejected(signed_in):
    response = signed_in.post("/admin/content/cards/card_평타/revert",
                              json={"commit_sha": "0" * 40})
    assert response.json()["ok"] is False


def test_history_is_empty_when_sync_is_disabled(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "content_repo_path", "")
    signed_in = client
    signed_in.post("/admin/login", json={"password": PASSWORD})
    response = signed_in.get("/admin/content/cards/card_평타/history")
    assert response.json() == {"ok": True, "history": []}
