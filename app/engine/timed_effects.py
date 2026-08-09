"""§2.5.3 — round-scoped modifier instances (B-02 / B-03 / B-04).

Round-duration effects are **not statuses**. They are modifier instances with a
fully recoverable definition, which is what makes `modify_stat(duration_rounds)`
persistable at all and what fixes the 무적 expiry bug:

    On creation during round N with duration_rounds = D:
        applied_at_round    = N
        expires_after_round = N + D
    At ROUND_END of round R:
        expire every row where expires_after_round <= R

So a 무적 created in round N survives round N's boundary and covers the whole
of round N+1 — v6.2 expired it before it ever blocked a hit.

`battle_units.spd` and friends are never mutated directly by a temporary
effect: effective stats are `base + Σ(active timed modifiers)`.
"""

from __future__ import annotations

import math

from app.db.connection import Database

INVULNERABLE = "invulnerable"
STAT_MODIFIER = "stat_modifier"


def create_invulnerable(db: Database, battle_id: int, unit_id: int, *,
                        current_round: int, duration_rounds: int,
                        source_ref: str | None = None) -> int:
    cursor = db.execute(
        "INSERT INTO battle_timed_effects "
        "(battle_id, battle_unit_id, effect_kind, applied_at_round, "
        " expires_after_round, source_ref) VALUES (?, ?, ?, ?, ?, ?)",
        (battle_id, unit_id, INVULNERABLE, current_round,
         current_round + duration_rounds, source_ref),
    )
    return int(cursor.lastrowid)


def create_stat_modifier(db: Database, battle_id: int, unit_id: int, *,
                         stat: str, delta: float, is_percent: bool,
                         current_round: int, duration_rounds: int,
                         source_ref: str | None = None) -> int:
    """Each modifier stores its own stat/delta/is_percent, so expiry subtracts
    exactly what was added and overlapping modifiers stay independent rows."""
    cursor = db.execute(
        "INSERT INTO battle_timed_effects "
        "(battle_id, battle_unit_id, effect_kind, stat, delta, is_percent, "
        " applied_at_round, expires_after_round, source_ref) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (battle_id, unit_id, STAT_MODIFIER, stat, float(delta), int(is_percent),
         current_round, current_round + duration_rounds, source_ref),
    )
    return int(cursor.lastrowid)


def is_invulnerable(db: Database, unit_id: int, current_round: int) -> bool:
    row = db.one(
        "SELECT 1 FROM battle_timed_effects WHERE battle_unit_id = ? "
        "AND effect_kind = ? AND expires_after_round > ?",
        (unit_id, INVULNERABLE, current_round - 1),
    )
    return row is not None


def stat_delta(db: Database, unit_id: int, stat: str, base_value: float) -> float:
    """Σ of active modifiers for one stat.

    Percent modifiers scale the *base* value, so two +30% rows add to +60% of
    base rather than compounding — which keeps expiry exactly reversible.
    """
    total = 0.0
    for row in db.query(
        "SELECT delta, is_percent FROM battle_timed_effects "
        "WHERE battle_unit_id = ? AND effect_kind = ? AND stat = ?",
        (unit_id, STAT_MODIFIER, stat),
    ):
        if row["is_percent"]:
            total += base_value * (float(row["delta"]) / 100.0)
        else:
            total += float(row["delta"])
    return total


def effective_stat(db: Database, unit_id: int, stat: str, base_value: float) -> int:
    return int(math.floor(base_value + stat_delta(db, unit_id, stat, base_value)))


def expire_round(db: Database, battle_id: int, round_no: int) -> int:
    """§2.12 ROUND_END step 1 — expire everything whose window has closed.

    An effect created THIS round has expires_after_round = R + D and therefore
    SURVIVES this boundary.
    """
    cursor = db.execute(
        "DELETE FROM battle_timed_effects WHERE battle_id = ? AND expires_after_round <= ?",
        (battle_id, round_no),
    )
    return cursor.rowcount


def clear_unit(db: Database, unit_id: int) -> None:
    db.execute("DELETE FROM battle_timed_effects WHERE battle_unit_id = ?", (unit_id,))


def active_for_unit(db: Database, unit_id: int) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT * FROM battle_timed_effects WHERE battle_unit_id = ? "
        "ORDER BY effect_instance_id",
        (unit_id,),
    )]
