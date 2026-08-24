"""§10.7 — GitHub content sync.

A failed git operation must never fail the publish (Reliability), and only
entities whose business data actually changed produce a rewritten file and
a commit (owner-confirmed interpretation — see `github_sync`'s docstring).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from app.content import github_sync as gs
from app.content import versioning as vs
from app.content.seed import seed_all
from app.db.connection import Database


def _init_repo(path) -> str:
    repo = str(path)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.com"],
                   check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True)
    return repo


def _read(repo: str, table: str, logical_id: str) -> dict:
    path = f"{repo}/{gs.CONTENT_DATA_DIR}/{table}/{logical_id}.json"
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# =====================================================================
# sync_publish
# =====================================================================
def test_a_fresh_publish_writes_one_file_per_entity(db, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    version = seed_all(db, publish_version=False)
    vs.publish(db, version)   # writes rows but does not sync — that's separate here

    result = gs.sync_publish(db, version, repo_path=repo)

    assert result.commit_error is None
    assert result.commit_sha is not None
    assert result.changed_count > 0

    card = _read(repo, "cards", "card_평타")
    assert card["content_version_id"] == version
    assert card["name"] == "평타"


def test_an_unchanged_entity_keeps_its_recorded_version(db, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    v1 = seed_all(db, publish_version=False)
    vs.publish(db, v1)
    gs.sync_publish(db, v1, repo_path=repo)

    # A second version that copies v1 verbatim, then edits exactly one card.
    v2 = vs.create_version(db)
    vs.copy_version(db, v1, v2)
    db.execute("UPDATE cards SET name = ? WHERE content_version_id = ? AND card_id = ?",
              ("평타(수정)", v2, "card_평타"))
    vs.publish(db, v2)

    result = gs.sync_publish(db, v2, repo_path=repo)

    changed_card = _read(repo, "cards", "card_평타")
    assert changed_card["content_version_id"] == v2
    assert changed_card["name"] == "평타(수정)"

    # An untouched card's file keeps v1 as its recorded version — its
    # business data never changed, even though the DB row now belongs to v2.
    untouched = _read(repo, "cards", "card_화_강타")
    assert untouched["content_version_id"] == v1

    # Only the one genuinely-changed entity produced a commit-worthy file.
    assert result.changed_count == 1


def test_a_second_sync_with_no_edits_commits_nothing(db, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    version = seed_all(db, publish_version=False)
    vs.publish(db, version)
    first = gs.sync_publish(db, version, repo_path=repo)
    assert first.changed_count > 0

    again = gs.sync_publish(db, version, repo_path=repo)
    assert again.changed_count == 0
    assert again.commit_sha is None


def test_a_git_failure_is_reported_not_raised(db, tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    version = seed_all(db, publish_version=False)
    vs.publish(db, version)

    result = gs.sync_publish(db, version, repo_path=str(not_a_repo))

    assert result.commit_error is not None
    assert result.commit_sha is None


# =====================================================================
# push
# =====================================================================
def test_a_push_without_a_remote_fails_gracefully(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (tmp_path / "repo" / "placeholder.txt").write_text("x")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "seed"], check=True)

    result = gs.attempt_push(repo)

    assert result.pushed is False
    assert result.error is not None


# =====================================================================
# publish() integration
# =====================================================================
def test_publish_syncs_when_enabled(db, tmp_path, monkeypatch):
    from app.config import settings

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(settings, "content_repo_path", repo)
    monkeypatch.setattr(settings, "content_repo_push", False)

    version = seed_all(db, publish_version=False)
    vs.publish(db, version)

    log = db.one("SELECT * FROM content_sync_log WHERE version_id = ?", (version,))
    assert log is not None
    assert log["commit_sha"] is not None
    assert log["changed_count"] > 0


def test_publish_never_touches_git_when_disabled(db, tmp_path):
    """Default settings — the whole point is that this stays inert."""
    from app.config import settings

    assert settings.content_sync_enabled is False   # the actual test config

    version = seed_all(db, publish_version=False)
    vs.publish(db, version)   # must not raise, must not need a repo at all

    log = db.one("SELECT * FROM content_sync_log WHERE version_id = ?", (version,))
    assert log is None


def test_a_broken_repo_path_never_fails_the_publish(db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "content_repo_path", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(settings, "content_repo_push", False)

    version = seed_all(db, publish_version=False)
    vs.publish(db, version)   # the assertion is simply that this returns

    row = db.one("SELECT is_current FROM content_versions WHERE version_id = ?",
                 (version,))
    assert row["is_current"] == 1


# =====================================================================
# revert data — history_for / value_at
# =====================================================================
def test_history_and_value_at_follow_one_entity_across_publishes(db, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    v1 = seed_all(db, publish_version=False)
    vs.publish(db, v1)
    gs.sync_publish(db, v1, repo_path=repo)

    v2 = vs.create_version(db)
    vs.copy_version(db, v1, v2)
    db.execute("UPDATE cards SET name = ? WHERE content_version_id = ? AND card_id = ?",
              ("평타(개명)", v2, "card_평타"))
    vs.publish(db, v2)
    gs.sync_publish(db, v2, repo_path=repo)

    history = gs.history_for(repo, "cards", "card_평타")
    assert len(history) == 2   # the original write, then the rename

    latest = gs.value_at(repo, "cards", "card_평타", history[0].commit_sha)
    assert latest["name"] == "평타(개명)"
    original = gs.value_at(repo, "cards", "card_평타", history[-1].commit_sha)
    assert original["name"] == "평타"

    # An entity never touched again only has its one write.
    untouched_history = gs.history_for(repo, "cards", "card_화_강타")
    assert len(untouched_history) == 1
