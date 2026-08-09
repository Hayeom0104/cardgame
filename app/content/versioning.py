"""§10.6 — content versioning.

Published ids were immutable in v6.1, but the **values** under those ids stayed
editable, so a restart could load an enemy plan created under one action
definition and execute it under another. RNG reproducibility does not fix
mutable content.

    Every content table uses a TWO-PART key:
        PRIMARY KEY (content_version_id, logical_id)

    · logical_id is STABLE and permanent — it is what player-owned data
      references
    · content_version_id selects WHICH revision of that logical entity applies
    · Publishing copies every row into the new version, changed or not, so a
      version is a complete immutable snapshot

    Resolution rule:
        permanent account data  → resolve logical_id at the CURRENT version
        anything inside a run   → resolve logical_id at runs.content_version_id
"""

from __future__ import annotations

import hashlib
import json

from app.db.connection import Database, utcnow

#: Every content table, with the columns that make up its logical key.
CONTENT_TABLES: dict[str, tuple[str, ...]] = {
    "cards": ("card_id",),
    "characters": ("character_id",),
    "enemies": ("enemy_id",),
    "enemy_actions": ("action_id",),
    "encounters": ("encounter_id",),
    "worlds": ("world_id",),
    "equipment_defs": ("equipment_def_id",),
    "equipment_sets": ("set_name",),
    "statuses": ("status_id",),
    "targeting_strategies": ("strategy_id",),
    "enemy_roles": ("enemy_role_id",),
    "threat_weights": ("enemy_role_id", "ally_role_id"),
    "boss_phases": ("boss_phase_id",),
    "transition_effects": ("transition_effect_id",),
    "cursed_cards": ("cursed_card_id",),
    "events": ("event_id",),
    "reward_tables": ("reward_table_id",),
    "banners": ("banner_id",),
    "achievements": ("achievement_id",),
    "research_nodes": ("node_id",),
    "balancing_constants": ("key",),
}


def current_version_id(db: Database) -> int | None:
    row = db.one("SELECT version_id FROM content_versions WHERE is_current = 1")
    return int(row["version_id"]) if row else None


def create_version(db: Database, *, is_draft: bool = False) -> int:
    cursor = db.execute(
        "INSERT INTO content_versions (published_at, fingerprint, is_current, is_draft) "
        "VALUES (NULL, NULL, 0, ?)", (int(is_draft),),
    )
    return int(cursor.lastrowid)


def copy_version(db: Database, source_version_id: int, target_version_id: int) -> None:
    """Publishing copies every row into the new version, changed or not."""
    for table in CONTENT_TABLES:
        columns = [row[1] for row in db.query(f"PRAGMA table_info({table})")]
        others = [column for column in columns if column != "content_version_id"]
        column_list = ", ".join(["content_version_id", *others])
        select_list = ", ".join(["?", *others])
        db.execute(
            f"INSERT OR REPLACE INTO {table} ({column_list}) "
            f"SELECT {select_list} FROM {table} WHERE content_version_id = ?",
            (target_version_id, source_version_id),
        )


def fingerprint(db: Database, version_id: int) -> str:
    """A stable hash over every content row in one version."""
    digest = hashlib.sha256()
    for table in sorted(CONTENT_TABLES):
        columns = [row[1] for row in db.query(f"PRAGMA table_info({table})")]
        key_columns = CONTENT_TABLES[table]
        order = ", ".join(key_columns)
        for row in db.query(
            f"SELECT * FROM {table} WHERE content_version_id = ? ORDER BY {order}",
            (version_id,),
        ):
            payload = {column: row[column] for column in columns
                       if column != "content_version_id"}
            digest.update(table.encode("utf-8"))
            digest.update(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def publish(db: Database, version_id: int) -> str:
    """Mark a version current and immutable.

    Active runs continue on their pinned version until they end. A publish
    never disturbs a run in progress.
    """
    from app.content.validation import validate_version

    validate_version(db, version_id)
    stamp = fingerprint(db, version_id)
    with db.tx() as conn:
        conn.execute("UPDATE content_versions SET is_current = 0")
        conn.execute(
            "UPDATE content_versions SET is_current = 1, is_draft = 0, "
            "published_at = ?, fingerprint = ? WHERE version_id = ?",
            (utcnow(), stamp, version_id),
        )
    return stamp


def resolve_for_run(db: Database, run_id: int) -> int:
    """Anything inside a run resolves through the run's pinned version."""
    row = db.one("SELECT content_version_id FROM runs WHERE run_id = ?", (run_id,))
    if row is None:
        raise KeyError(f"run {run_id} does not exist")
    return int(row["content_version_id"])
