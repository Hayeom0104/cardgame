"""대시보드가 하는 일 — HTTP와 분리된 도메인 조작.

여기가 지키는 규칙 하나가 나머지 전부를 좌우한다.

    **발행된 버전은 고칠 수 없다.**

§10.6이 콘텐츠 버전을 불변 스냅샷으로 정의하기 때문이다. 진행 중인 런은
`runs.content_version_id` 로 그 버전을 붙들고 있으므로, 발행된 값을 바꾸면
런 도중에 카드가 달라진다. 그래서 모든 편집은 **초안**으로 간다. 초안이
없으면 현재 버전을 복사해 만든다.

발행은 §10.5 검증을 통과해야만 이루어진다 (`versioning.publish`가 강제한다).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.content import versioning as vs
from app.content.validation import ValidationError
from app.db.connection import Database

logger = logging.getLogger(__name__)


class AdminError(RuntimeError):
    """운영자에게 그대로 보여줄 사유."""


# =====================================================================
# 버전
# =====================================================================
def versions(db: Database) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT version_id, published_at, fingerprint, is_current, is_draft "
        "FROM content_versions ORDER BY version_id DESC")]


def draft_version_id(db: Database) -> int | None:
    """편집 중인 초안. 없으면 None."""
    row = db.one("SELECT version_id FROM content_versions WHERE is_draft = 1 "
                 "ORDER BY version_id DESC LIMIT 1")
    return int(row["version_id"]) if row else None


def open_draft(db: Database) -> int:
    """편집할 초안을 얻는다. 없으면 현재 버전을 복사해 만든다.

    복사본으로 시작하는 이유는 §10.6이 버전을 *완전한* 스냅샷으로 정의하기
    때문이다. 빈 초안을 발행하면 콘텐츠가 통째로 사라진다.
    """
    existing = draft_version_id(db)
    if existing is not None:
        return existing

    source = vs.current_version_id(db)
    draft = vs.create_version(db, is_draft=True)
    if source is not None:
        vs.copy_version(db, source, draft)
        logger.info("버전 %s를 복사해 초안 %s를 만들었습니다", source, draft)
    return draft


def editable_version_id(db: Database) -> int:
    """지금 편집이 향해야 할 버전. 항상 초안이다."""
    return open_draft(db)


def discard_draft(db: Database) -> None:
    """초안을 버린다. 발행된 버전은 절대 지우지 않는다."""
    draft = draft_version_id(db)
    if draft is None:
        return
    with db.tx() as conn:
        for table in vs.CONTENT_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE content_version_id = ?",
                         (draft,))
        conn.execute("DELETE FROM content_versions WHERE version_id = ? "
                     "AND is_draft = 1", (draft,))
    logger.info("초안 %s를 버렸습니다", draft)


def validate(db: Database, version_id: int) -> str | None:
    """§10.5 검증. 통과하면 None, 아니면 사유."""
    from app.content.validation import validate_version

    try:
        validate_version(db, version_id)
    except ValidationError as error:
        return str(error)
    return None


def publish_draft(db: Database) -> str:
    """초안을 발행한다. 검증을 통과하지 못하면 아무것도 바뀌지 않는다."""
    draft = draft_version_id(db)
    if draft is None:
        raise AdminError("발행할 초안이 없습니다.")
    try:
        stamp = vs.publish(db, draft)
    except ValidationError as error:
        raise AdminError(f"검증에 실패해 발행하지 않았습니다: {error}") from error
    logger.info("초안 %s를 발행했습니다 (%s)", draft, stamp[:12])
    return stamp


# =====================================================================
# §15 밸런싱 상수
# =====================================================================
def balance_rows(db: Database, version_id: int) -> list[dict]:
    """설정값과 그 **설명**을 함께. 설명은 `config/*.toml` 주석에서 온다."""
    docs = {row["key"]: row for row in db.query(
        "SELECT key, source_file, description FROM balancing_metadata")}
    rows = []
    for row in db.query(
        "SELECT key, value_json FROM balancing_constants "
        "WHERE content_version_id = ? ORDER BY key", (version_id,),
    ):
        meta = docs.get(row["key"])
        rows.append({
            "key": row["key"],
            "value": row["value_json"],
            "pretty": _pretty(row["value_json"]),
            "source_file": meta["source_file"] if meta else "",
            "description": meta["description"] if meta else "",
        })
    return rows


def _pretty(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return raw


def save_constant(db: Database, version_id: int, key: str, raw: str) -> None:
    """상수 하나를 초안에 저장한다.

    JSON으로 읽히지 않으면 거절한다. 깨진 값을 넣어 두면 그 값을 처음 읽는
    순간 — 대개 전투 도중에 — 터진다.
    """
    _require_draft(db, version_id)
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise AdminError(f"JSON으로 읽을 수 없습니다: {error}") from error

    existing = db.one(
        "SELECT 1 FROM balancing_constants WHERE content_version_id = ? AND key = ?",
        (version_id, key))
    if existing is None:
        raise AdminError(f"{key!r} 은(는) 이 버전에 없는 설정입니다.")

    db.execute(
        "UPDATE balancing_constants SET value_json = ? "
        "WHERE content_version_id = ? AND key = ?",
        (json.dumps(value, ensure_ascii=False), version_id, key))


# =====================================================================
# 콘텐츠 행
# =====================================================================
def content_tables(db: Database, version_id: int) -> list[dict]:
    listing = []
    for table, keys in sorted(vs.CONTENT_TABLES.items()):
        count = db.one(f"SELECT COUNT(*) AS n FROM {table} "
                       f"WHERE content_version_id = ?", (version_id,))
        # `keys` 라는 이름은 쓰지 않는다 — 템플릿에서 dict의 `.keys` 메서드로
        # 잡혀 조용히 엉뚱한 것이 렌더링된다.
        listing.append({"table": table, "key_columns": keys,
                        "count": int(count["n"])})
    return listing


def content_rows(db: Database, table: str, version_id: int) -> list[dict]:
    keys = _require_table(table)
    order = ", ".join(keys)
    return [dict(row) for row in db.query(
        f"SELECT * FROM {table} WHERE content_version_id = ? ORDER BY {order}",
        (version_id,))]


def content_row(db: Database, table: str, version_id: int,
                key_values: list[str]) -> dict | None:
    keys = _require_table(table)
    where = " AND ".join(f"{column} = ?" for column in keys)
    row = db.one(f"SELECT * FROM {table} WHERE content_version_id = ? AND {where}",
                 (version_id, *key_values))
    return dict(row) if row else None


def save_content_row(db: Database, table: str, version_id: int,
                     key_values: list[str], payload: dict) -> None:
    """행 하나를 초안에 저장한다. 논리 키는 바꿀 수 없다.

    §10.6에서 `logical_id` 는 **영구**하다 — 플레이어가 가진 데이터가 그
    id를 참조한다. 이름을 바꾸면 그 참조가 통째로 끊긴다.
    """
    _require_draft(db, version_id)
    keys = _require_table(table)
    columns = {row[1] for row in db.query(f"PRAGMA table_info({table})")}

    updates = {}
    for column, value in payload.items():
        if column in ("content_version_id", *keys):
            continue                          # 논리 키와 버전은 손대지 않는다
        if column not in columns:
            raise AdminError(f"{table}에 {column!r} 열이 없습니다.")
        updates[column] = value
    if not updates:
        raise AdminError("바꿀 값이 없습니다.")

    assignments = ", ".join(f"{column} = ?" for column in updates)
    where = " AND ".join(f"{column} = ?" for column in keys)
    cursor = db.execute(
        f"UPDATE {table} SET {assignments} WHERE content_version_id = ? AND {where}",
        (*updates.values(), version_id, *key_values))
    if cursor.rowcount == 0:
        raise AdminError("해당 행을 찾지 못했습니다.")


def sync_status(db: Database, version_id: int) -> dict | None:
    """§10.7 — this version's git mirror status, for the versions page."""
    row = db.one("SELECT * FROM content_sync_log WHERE version_id = ?", (version_id,))
    return dict(row) if row else None


def entity_history(db: Database, table: str, key_values: list[str]) -> list[dict]:
    """§10.7 되돌리기 step 1 — this entity's git history, newest first.

    Empty (not an error) when sync is off — an entity simply has no history
    to offer yet, same as one that has never been published a second time.
    """
    from app.config import settings

    if not settings.content_sync_enabled:
        return []
    from app.content import github_sync as gs

    logical_id = "~".join(key_values)
    return [
        {"commit_sha": entry.commit_sha, "committed_at": entry.committed_at,
         "subject": entry.subject}
        for entry in gs.history_for(settings.content_repo_path, table, logical_id)
    ]


def revert_diff(db: Database, table: str, key_values: list[str],
                commit_sha: str) -> dict:
    """§10.7 되돌리기 step 2 — current live values vs. the picked version,
    field by field, shown BEFORE any write happens."""
    from app.config import settings

    if not settings.content_sync_enabled:
        raise AdminError("GitHub 콘텐츠 동기화가 꺼져 있어 되돌리기를 쓸 수 없습니다.")
    from app.content import github_sync as gs

    logical_id = "~".join(key_values)
    picked = gs.value_at(settings.content_repo_path, table, logical_id, commit_sha)
    if picked is None:
        raise AdminError("그 시점의 값을 git에서 찾지 못했습니다.")

    keys = _require_table(table)
    current_version = vs.current_version_id(db)
    current = content_row(db, table, current_version, key_values) if current_version else None

    excluded = {"content_version_id", *keys}
    fields = sorted((set(picked) | set(current or {})) - excluded)
    return {
        "fields": [
            {"column": column, "current": (current or {}).get(column),
             "picked": picked.get(column)}
            for column in fields
        ],
    }


def revert_content_row(db: Database, table: str, key_values: list[str],
                       commit_sha: str) -> str:
    """§10.7 되돌리기 step 3 — load the picked version as the draft, then
    immediately publish through the normal §10.5/§10.6 flow.

    This creates a NEW content_version_id; §10.6's versions are
    forward-only, so a revert is structurally indistinguishable from any
    other publish, git mirror included.
    """
    from app.config import settings

    if not settings.content_sync_enabled:
        raise AdminError("GitHub 콘텐츠 동기화가 꺼져 있어 되돌리기를 쓸 수 없습니다.")
    from app.content import github_sync as gs

    logical_id = "~".join(key_values)
    payload = gs.value_at(settings.content_repo_path, table, logical_id, commit_sha)
    if payload is None:
        raise AdminError("그 시점의 값을 git에서 찾지 못했습니다.")

    version_id = editable_version_id(db)
    save_content_row(db, table, version_id, key_values, payload)
    return publish_draft(db)


def _require_table(table: str) -> tuple[str, ...]:
    keys = vs.CONTENT_TABLES.get(table)
    if keys is None:
        raise AdminError(f"{table!r} 은(는) 콘텐츠 테이블이 아닙니다.")
    return keys


def _require_draft(db: Database, version_id: int) -> None:
    row = db.one("SELECT is_draft, is_current FROM content_versions "
                 "WHERE version_id = ?", (version_id,))
    if row is None:
        raise AdminError(f"버전 {version_id} 이(가) 없습니다.")
    if not row["is_draft"]:
        raise AdminError(
            "발행된 버전은 고칠 수 없습니다 (§10.6). 진행 중인 런이 그 버전을 "
            "붙들고 있어서, 값을 바꾸면 런 도중에 내용이 달라집니다. "
            "초안을 만들어 편집하세요.")


# =====================================================================
# 운영 현황
# =====================================================================
def overview(db: Database, balance) -> dict:
    from app.engine import lifecycle as lc

    runs_by_state = {row["state"]: int(row["n"]) for row in db.query(
        "SELECT state, COUNT(*) AS n FROM runs GROUP BY state ORDER BY state")}
    active = [row for row in db.query(
        "SELECT * FROM runs WHERE state NOT IN "
        "('run_completed','run_defeated','run_abandoned','run_expired',"
        "'admin_terminated')")]
    stale = sum(1 for run in active
                if balance is not None and lc.is_expired(db, balance, run))

    pending_tx = db.one(
        "SELECT COUNT(*) AS n FROM purchase_transactions WHERE status NOT IN "
        "('completed','compensated','rejected_no_charge','failed_permanent')")
    return {
        "accounts": int(db.one("SELECT COUNT(*) AS n FROM accounts")["n"]),
        "runs_by_state": runs_by_state,
        "active_runs": len(active),
        "stale_runs": stale,
        "recovery_required": int(db.one(
            "SELECT COUNT(*) AS n FROM runs WHERE state = 'recovery_required'")["n"]),
        "pending_transactions": int(pending_tx["n"]) if pending_tx else 0,
        "current_version": vs.current_version_id(db),
        "draft_version": draft_version_id(db),
    }


def players(db: Database, needle: str = "", limit: int = 50) -> list[dict]:
    sql = ("SELECT a.user_id, a.carta, a.wildcards, a.party_slots, "
           "a.tutorial_completed_at, "
           "(SELECT COUNT(*) FROM runs r WHERE r.user_id = a.user_id) AS runs "
           "FROM accounts a")
    params: tuple = ()
    if needle:
        sql += " WHERE CAST(a.user_id AS TEXT) LIKE ?"
        params = (f"%{needle}%",)
    sql += " ORDER BY a.user_id LIMIT ?"
    return [dict(row) for row in db.query(sql, (*params, limit))]


def player_detail(db: Database, user_id: int) -> dict | None:
    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    if account is None:
        return None
    return {
        "account": dict(account),
        "runs": [dict(row) for row in db.query(
            "SELECT run_id, world_id, state, is_tutorial, deepest_depth_reached, "
            "created_at, last_activity_at FROM runs WHERE user_id = ? "
            "ORDER BY run_id DESC LIMIT 20", (user_id,))],
        "characters": [dict(row) for row in db.query(
            "SELECT character_id, star_rank FROM owned_characters "
            "WHERE user_id = ? ORDER BY character_id", (user_id,))],
        "cards": [dict(row) for row in db.query(
            "SELECT card_id, upgrade_tier FROM unlocked_cards WHERE user_id = ? "
            "ORDER BY card_id", (user_id,))],
        "transactions": [dict(row) for row in db.query(
            "SELECT tx_id, operation, direction, status, expected_coin_delta, "
            "created_at FROM purchase_transactions WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 20", (user_id,))],
    }


def terminate_run(db: Database, run_id: int, reason: str) -> None:
    """§16.1 `admin_terminated` — 정산 없이 런을 닫는다.

    보상도 보존도 적용하지 않는다. 정상적인 끝맺음이 아니라 **운영 개입**이며,
    그 사실이 `end_reason` 에 남아야 한다. 평범한 종료가 필요하면 플레이어가
    `포기`를 쓰거나 §16.3 만료가 처리한다.
    """
    from app.engine import lifecycle as lc

    run = db.one("SELECT state FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        raise AdminError(f"런 {run_id} 이(가) 없습니다.")
    if run["state"] in lc.TERMINAL_STATES:
        raise AdminError("이미 끝난 런입니다.")
    db.execute(
        "UPDATE runs SET state = 'admin_terminated', end_reason = ?, "
        "updated_at = ? WHERE run_id = ?",
        (f"관리자 종료: {reason}"[:200], _now(), run_id))
    logger.warning("run %s를 관리자가 종료했습니다: %s", run_id, reason)


def _now() -> str:
    from app.db.connection import utcnow

    return utcnow()


# =====================================================================
# 에셋
# =====================================================================
def asset_report(db: Database, version_id: int) -> list[dict]:
    from app.cli.assets import wanted
    from app.render.assets import AssetLibrary
    from app.render.theme import load as load_theme

    library = AssetLibrary(load_theme())
    return library.inventory(wanted(db, version_id))


def save_asset(kind: str, entity_id: str, data: bytes) -> str:
    """올린 그림을 정해진 자리에 쓴다.

    파일명은 콘텐츠 id에서 만든다 — 업로드가 준 이름을 쓰면 `../` 같은 것이
    끼어들 여지가 생긴다.
    """
    from PIL import Image, UnidentifiedImageError
    from app.render.assets import AssetLibrary
    from app.render.theme import load as load_theme

    import io

    library = AssetLibrary(load_theme())
    library.kind(kind)                                # 모르는 종류면 여기서 막힌다
    if len(data) > library.max_bytes:
        raise AdminError(f"파일이 너무 큽니다 ({len(data):,}바이트).")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise AdminError(f"이미지로 읽을 수 없습니다: {error}") from error

    if not _safe_id(entity_id):
        raise AdminError("id에 경로 문자를 쓸 수 없습니다.")
    path = library.expected_path(kind, entity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    logger.info("에셋을 저장했습니다: %s", path)
    return str(path)


def _safe_id(entity_id: str) -> bool:
    return bool(entity_id) and not ({"/", "\\"} & set(entity_id)) and ".." not in entity_id
