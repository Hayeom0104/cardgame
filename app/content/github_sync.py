"""§10.7 — GitHub content sync.

Mirrors every published content version into `content-data/` in the git
repo the service runs from — a human-readable backup plus a per-entity
change history (`git log` on one file is that entity's full history).
SQLite (§10.3) stays the sole source of truth the engine reads from; this
module only OBSERVES a publish and writes alongside it. A failure here must
never fail the publish itself (§10.7 Reliability) — every entry point here
is designed to return a result object rather than raise, and the caller
wraps the call in `try/except` anyway as a defensive backstop.

Disabled entirely unless `settings.content_repo_path` is set (§config.py) —
by default this module is never invoked, so it stays inert in this shared
dev repo and in tests unless a test explicitly points it at a throwaway
git repo.

## Interpretation note — §10.6 vs §10.7 tension, owner-confirmed this session

§10.6 copies EVERY row into the new version on every publish, changed or
not, so a row's own `content_version_id` column always advances even when
its business data did not change. §10.7 says to embed `content_version_id`
(and the whole-version `fingerprint`) in each entity's file while also
claiming a publish "touches N entities" and produces "N changed files" —
read literally, embedding either ever-advancing value would make every
file's JSON text differ on every single publish, which contradicts the
N-changed-files framing entirely (the whole point of `git log` per file,
per §10.7's own Storage note, is that it isolates that one entity's real
history).

Resolved (owner-confirmed): a file is rewritten only when that entity's
BUSINESS DATA — every column except `content_version_id` — actually
changed since the last time the file was written. The `content_version_id`
recorded in a file therefore means "the version this entity's data last
changed at", not "the version currently published". The whole-version
`fingerprint` has the identical problem (it changes on every publish
regardless of any one entity), so it is not embedded per-file at all —
it goes in the commit message instead, where §10.7 itself already allows
`content_version_id` as commit metadata "not normative here".
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.db.connection import Database, utcnow

logger = logging.getLogger(__name__)

CONTENT_DATA_DIR = "content-data"

#: §10.7 Reliability — concurrent publishes are serialized before the git
#: write, reusing the single-writer-queue pattern at §16.6. This process is
#: the one writer for any given repo_path, so a plain lock is that queue.
_GIT_LOCK = threading.Lock()


class GitSyncError(RuntimeError):
    """A git operation failed. Callers must catch this — see module docstring."""


@dataclass
class SyncResult:
    version_id: int
    changed_count: int = 0
    commit_sha: str | None = None
    commit_error: str | None = None


@dataclass
class PushResult:
    pushed: bool = False
    error: str | None = None


def _run_git(repo_path: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise GitSyncError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _safe_filename(logical_id: str) -> str:
    """Logical ids are content-authored (§10.6) and may hold any script, but
    never a path separator — this only guards against one accidentally
    escaping `content-data/<table>/`."""
    return logical_id.replace("/", "_").replace("\\", "_")


def _entity_files(db: Database, version_id: int, repo_root: Path):
    """Yield `(file_path, payload_text)` for every row in every content table
    at `version_id` — one file per logical entity (§10.7 Storage)."""
    from app.content import versioning as vs

    for table in sorted(vs.CONTENT_TABLES):
        keys = vs.CONTENT_TABLES[table]
        order = ", ".join(keys)
        table_dir = repo_root / CONTENT_DATA_DIR / table
        for raw_row in db.query(
            f"SELECT * FROM {table} WHERE content_version_id = ? ORDER BY {order}",
            (version_id,),
        ):
            row = dict(raw_row)
            logical_id = "~".join(str(row[key]) for key in keys)
            file_path = table_dir / f"{_safe_filename(logical_id)}.json"

            business = {key: value for key, value in row.items()
                       if key != "content_version_id"}
            effective_version = version_id
            existing_text = None
            if file_path.exists():
                existing_text = file_path.read_text(encoding="utf-8")
                try:
                    existing = json.loads(existing_text)
                except json.JSONDecodeError:
                    existing = None
                if isinstance(existing, dict):
                    existing_business = {key: value for key, value in existing.items()
                                         if key != "content_version_id"}
                    if existing_business == business:
                        effective_version = existing.get("content_version_id", version_id)

            payload = {"content_version_id": effective_version, **business}
            text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            yield file_path, text, existing_text


def sync_publish(db: Database, version_id: int, *, repo_path: str) -> SyncResult:
    """Write and commit the git mirror for one publish. Never raises.

    Only entities whose business data actually changed produce a rewritten
    file (see the module docstring) — those are exactly what the commit and
    `SyncResult.changed_count` cover.
    """
    from app.content import versioning as vs

    result = SyncResult(version_id=version_id)
    repo_root = Path(repo_path)
    changed_paths: list[str] = []

    try:
        with _GIT_LOCK:
            for file_path, text, existing_text in _entity_files(db, version_id, repo_root):
                if existing_text == text:
                    continue
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(text, encoding="utf-8")
                changed_paths.append(str(file_path.relative_to(repo_root)))

            result.changed_count = len(changed_paths)
            if not changed_paths:
                return result

            fingerprint = vs.fingerprint(db, version_id)
            message = (f"content publish v{version_id}\n\n"
                      f"entities changed: {len(changed_paths)}\n"
                      f"fingerprint: {fingerprint}")
            _run_git(repo_path, "add", "--", *changed_paths)
            _run_git(repo_path, "commit", "-m", message)
            result.commit_sha = _run_git(repo_path, "rev-parse", "HEAD")
    except GitSyncError as error:
        logger.warning("§10.7 content sync commit failed for version %s: %s",
                       version_id, error)
        result.commit_error = str(error)
    except OSError as error:
        logger.warning("§10.7 content sync write failed for version %s: %s",
                       version_id, error)
        result.commit_error = str(error)

    return result


def attempt_push(repo_path: str) -> PushResult:
    """Best-effort `git push`. A failure is never fatal — see Reliability."""
    try:
        with _GIT_LOCK:
            _run_git(repo_path, "push")
        return PushResult(pushed=True)
    except GitSyncError as error:
        logger.warning("§10.7 content sync push failed: %s", error)
        return PushResult(pushed=False, error=str(error))


def record_result(db: Database, result: SyncResult, push: PushResult | None) -> None:
    """Persist `result`/`push` to `content_sync_log` for dashboard visibility."""
    now = utcnow()
    db.execute(
        "INSERT INTO content_sync_log (version_id, changed_count, commit_sha, "
        "commit_error, pushed_at, push_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(version_id) DO UPDATE SET changed_count = excluded.changed_count, "
        "commit_sha = excluded.commit_sha, commit_error = excluded.commit_error, "
        "pushed_at = excluded.pushed_at, push_error = excluded.push_error, "
        "updated_at = excluded.updated_at",
        (result.version_id, result.changed_count, result.commit_sha, result.commit_error,
         now if push and push.pushed else None, push.error if push else None, now),
    )


def sync_and_record(db: Database, version_id: int, *, repo_path: str,
                    push: bool = False) -> SyncResult:
    """The full §10.7 flow for one publish: commit, optionally push, log.

    Called from `versioning.publish()` and never allowed to raise past that
    call — every branch here already catches its own git/OS errors.
    """
    result = sync_publish(db, version_id, repo_path=repo_path)
    push_result = None
    if result.commit_sha is not None and push:
        push_result = attempt_push(repo_path)
    try:
        record_result(db, result, push_result)
    except Exception:                                              # noqa: BLE001
        logger.exception("§10.7 content sync could not log its own result")
    return result


# =====================================================================
# 되돌리기 (revert) — git history is the entity history (§10.7)
# =====================================================================
@dataclass
class HistoryEntry:
    commit_sha: str
    committed_at: str
    subject: str


def history_for(repo_path: str, table: str, logical_id: str) -> list[HistoryEntry]:
    """Every commit that touched this one entity's file, newest first."""
    path = f"{CONTENT_DATA_DIR}/{table}/{_safe_filename(logical_id)}.json"
    try:
        log = _run_git(repo_path, "log", "--follow",
                       "--format=%H%x1f%cI%x1f%s%x1e", "--", path)
    except GitSyncError:
        return []
    entries = []
    for chunk in filter(None, log.split("\x1e")):
        sha, committed_at, subject = chunk.strip("\n").split("\x1f")
        entries.append(HistoryEntry(sha, committed_at, subject))
    return entries


def value_at(repo_path: str, table: str, logical_id: str, commit_sha: str) -> dict | None:
    """This entity's published values as of `commit_sha` — the DRAFT source
    for step 3 of §10.7's revert flow."""
    path = f"{CONTENT_DATA_DIR}/{table}/{_safe_filename(logical_id)}.json"
    try:
        text = _run_git(repo_path, "show", f"{commit_sha}:{path}")
    except GitSyncError:
        return None
    payload = json.loads(text)
    payload.pop("content_version_id", None)
    return payload
