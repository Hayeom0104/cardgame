"""§20 — the local achievement system.

Achievements in Deckout are LOCAL to this bot. They NEVER call the Central Bot
achievement API: the `achievement:grant` scope was deliberately excluded
(§1.3.9), and a registered bot name cannot be re-registered.

Idempotency is backed by a receipt table (C-06). `achievement_progress` stores
only totals, and `processed_events` is run-scoped — it does not cover non-run
mutations such as star-up or equipment tier-up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.db.connection import Database, utcnow

# counter_key values the engine hooks emit (§20.2).
BOSS_DEFEATED = "boss_defeated"
RUN_CLEARED = "run_cleared"
ENEMY_KILLED = "enemy_killed"
CURSE_REMOVED = "curse_removed"
EQUIPMENT_TIERED = "equipment_tiered"
CHARACTER_STARRED = "character_starred"


@dataclass
class AchievementUpdate:
    achievement_id: str
    current_value: int
    target_value: int
    newly_completed: bool = False
    carta_granted: int = 0


@dataclass
class AchievementResult:
    updates: list[AchievementUpdate] = field(default_factory=list)
    carta_granted: int = 0
    already_applied: bool = False


def advance_counter(db: Database, user_id: int, counter_key: str, delta: int, *,
                    mutation_id: str, content_version_id: int) -> AchievementResult:
    """Advance every achievement tracking `counter_key`.

    The 「보스 처치」 ladder shares one counter deliberately, so a single boss
    kill advances all three rungs.
    """
    rows = db.query(
        "SELECT * FROM achievements WHERE content_version_id = ? AND counter_key = ? "
        "ORDER BY target_value, achievement_id",
        (content_version_id, counter_key),
    )
    result = AchievementResult()
    for row in rows:
        single = advance_by_id(
            db, user_id, row["achievement_id"], delta,
            mutation_id=f"{mutation_id}:{row['achievement_id']}",
            content_version_id=content_version_id,
        )
        result.updates.extend(single.updates)
        result.carta_granted += single.carta_granted
    return result


def advance_by_id(db: Database, user_id: int, achievement_id: str, delta: int, *,
                  mutation_id: str, content_version_id: int) -> AchievementResult:
    """One idempotent increment, guarded by its receipt.

    Every increment inserts its receipt in the SAME local transaction as the
    counter update. A retry's duplicate INSERT fails on the primary key, the
    transaction rolls back, and the handler treats the increment as already
    applied.
    """
    result = AchievementResult()
    definition = db.one(
        "SELECT * FROM achievements WHERE content_version_id = ? AND achievement_id = ?",
        (content_version_id, achievement_id),
    )
    if definition is None:
        return result

    existing_receipt = db.one(
        "SELECT 1 FROM achievement_progress_receipts WHERE mutation_id = ?",
        (mutation_id,),
    )
    if existing_receipt is not None:
        result.already_applied = True
        return result

    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO achievement_progress_receipts (mutation_id, user_id, "
                "achievement_id, delta, applied_at) VALUES (?, ?, ?, ?, ?)",
                (mutation_id, user_id, achievement_id, delta, utcnow()),
            )
            conn.execute(
                "INSERT INTO achievement_progress (user_id, achievement_id, "
                "current_value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, achievement_id) DO UPDATE SET "
                "current_value = current_value + excluded.current_value",
                (user_id, achievement_id, delta),
            )
            progress = conn.execute(
                "SELECT * FROM achievement_progress WHERE user_id = ? "
                "AND achievement_id = ?", (user_id, achievement_id),
            ).fetchone()

            update = AchievementUpdate(
                achievement_id=achievement_id,
                current_value=int(progress["current_value"]),
                target_value=int(definition["target_value"]),
            )

            # Completion is evaluated on every increment; the reward is granted
            # in the same local transaction (§20.2), one time only (§20.3).
            if (progress["current_value"] >= definition["target_value"]
                    and progress["completed_at"] is None):
                carta = int(definition["carta_reward"])
                conn.execute(
                    "UPDATE achievement_progress SET completed_at = ?, "
                    "reward_claimed_at = ? WHERE user_id = ? AND achievement_id = ?",
                    (utcnow(), utcnow() if carta else None, user_id, achievement_id),
                )
                if carta:
                    conn.execute(
                        "UPDATE accounts SET carta = carta + ?, updated_at = ? "
                        "WHERE user_id = ?", (carta, utcnow(), user_id),
                    )
                update.newly_completed = True
                update.carta_granted = carta
                result.carta_granted += carta

            result.updates.append(update)
    except Exception as error:  # pragma: no cover - integrity path
        if "UNIQUE" in str(error) or "PRIMARY KEY" in str(error):
            result.already_applied = True
            return result
        raise
    return result


def is_completed(db: Database, user_id: int, achievement_id: str) -> bool:
    row = db.one(
        "SELECT completed_at FROM achievement_progress WHERE user_id = ? "
        "AND achievement_id = ?", (user_id, achievement_id),
    )
    return bool(row and row["completed_at"])


def progress_list(db: Database, user_id: int, content_version_id: int,
                  include_hidden: bool = False) -> list[dict]:
    """§20.5 — `!덱아웃 업적`. Hidden entries stay hidden until completed."""
    rows = db.query(
        "SELECT a.*, p.current_value, p.completed_at FROM achievements a "
        "LEFT JOIN achievement_progress p ON p.achievement_id = a.achievement_id "
        "AND p.user_id = ? WHERE a.content_version_id = ? "
        "ORDER BY a.counter_key, a.target_value",
        (user_id, content_version_id),
    )
    listing = []
    for row in rows:
        completed = bool(row["completed_at"])
        if row["is_hidden"] and not completed and not include_hidden:
            continue
        listing.append({
            "achievement_id": row["achievement_id"],
            "name": row["name"],
            "description": row["description"],
            "current_value": int(row["current_value"] or 0),
            "target_value": int(row["target_value"]),
            "completed": completed,
            "carta_reward": int(row["carta_reward"]),
        })
    return listing
