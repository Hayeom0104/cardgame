"""§2.5.1 — status taxonomy: two clocks, three persistence models.

The critical property, which the whole §2.11 turn order is built around: a
countdown status is **evaluated before it is decremented**, so a 1-turn effect
always acts exactly once.

Round-duration modifiers are NOT statuses — see `timed_effects` (§2.5.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.connection import Database

# -- clocks (§2.5.1) -----------------------------------------------------
TURN_START_TRIGGER = "turn_start_trigger"      # fires at the owner's turn, pre-gating
OWNER_TURN_COUNTDOWN = "owner_turn_countdown"  # checked while active, decremented at turn end

# -- scope (§2.5.1a, v6.4) ----------------------------------------------
# Card upgrades (§5.8) can author brand-new statuses. Without a governance
# rule, every new status silently becomes something enemy AI can also be
# authored to inflict, which was never separately reviewed.
PLAYER_ONLY = "player_only"
ENEMY_ONLY = "enemy_only"
UNIVERSAL = "universal"
SCOPES = frozenset({PLAYER_ONLY, ENEMY_ONLY, UNIVERSAL})

# -- persistence models --------------------------------------------------
COUNTDOWN = "countdown"            # no intensity; duration in turns; refresh to max
STACK_DURATION = "stack_duration"  # stacks + one shared duration; add AND refresh
STACK_DECAY = "stack_decay"        # stacks ARE the lifetime; add stacks only

# -- the base set of 10 (§2.5.1) ----------------------------------------
BURN = "화상"
BLEED = "출혈"
STUN = "기절"
SILENCE = "침묵"
SHIELD_PIERCE = "보호막_관통"
TAUNT = "도발"
DEFENSE_DOWN = "방어력_감소"
ATTACK_UP = "공격력_증가"
HEAL_DOWN = "회복량_감소"
SPEED_DOWN = "속도_감소"

#: Statuses whose flat damage bypasses block *and* 방어력 (§2.5.1 design note).
DOT_STATUSES = (BURN, BLEED)


@dataclass(frozen=True)
class StatusDef:
    status_id: str
    name: str
    kind: str            # buff | debuff
    model: str
    clock: str
    stack_cap: int | None       # NULL for countdown
    base_duration: int | None   # NULL for stack_decay
    magnitude: float
    cleansable: bool = True
    persists_through_boss_phase: bool = False
    scope: str = UNIVERSAL


class StatusRegistry:
    """Status definitions resolved at one pinned content version (§10.6)."""

    def __init__(self, db: Database, content_version_id: int):
        self.db = db
        self.content_version_id = content_version_id
        self._cache: dict[str, StatusDef] = {}

    def get(self, status_id: str) -> StatusDef:
        if status_id not in self._cache:
            row = self.db.one(
                "SELECT * FROM statuses WHERE content_version_id = ? AND status_id = ?",
                (self.content_version_id, status_id),
            )
            if row is None:
                raise KeyError(
                    f"status {status_id!r} is not defined at content version "
                    f"{self.content_version_id}"
                )
            self._cache[status_id] = StatusDef(
                status_id=row["status_id"],
                name=row["name"],
                kind=row["kind"],
                model=row["model"],
                clock=row["clock"],
                stack_cap=row["stack_cap"],
                base_duration=row["base_duration"],
                magnitude=row["magnitude"],
                cleansable=bool(row["cleansable"]),
                persists_through_boss_phase=bool(row["persists_through_boss_phase"]),
                scope=row["scope"],
            )
        return self._cache[status_id]


# =====================================================================
# Application
# =====================================================================
def apply_status(db: Database, registry: StatusRegistry, unit_id: int,
                 status_id: str, stacks: int = 1,
                 duration_override: int | None = None) -> None:
    """Apply or re-apply a status (§2.5.1 re-application column).

    countdown       — refresh duration to max, intensity is meaningless
    stack_duration  — add stacks AND refresh duration to max (Q23 = A)
    stack_decay     — add stacks only; there is no duration to refresh
    """
    definition = registry.get(status_id)
    existing = db.one(
        "SELECT stacks, duration_remaining FROM battle_unit_statuses "
        "WHERE battle_unit_id = ? AND status_id = ?",
        (unit_id, status_id),
    )

    if definition.model == STACK_DECAY:
        new_stacks = (existing["stacks"] if existing else 0) + max(1, stacks)
        if definition.stack_cap is not None:
            new_stacks = min(new_stacks, definition.stack_cap)
        _upsert(db, unit_id, status_id, new_stacks, None)
        return

    duration = duration_override if duration_override is not None else definition.base_duration
    if duration is None:
        raise ValueError(
            f"status {status_id!r} uses model {definition.model!r} and needs a duration"
        )

    if definition.model == COUNTDOWN:
        # No intensity. Re-application refreshes the duration to max.
        _upsert(db, unit_id, status_id, 1, duration)
        return

    # STACK_DURATION: one duration governs the whole stack.
    new_stacks = (existing["stacks"] if existing else 0) + max(1, stacks)
    if definition.stack_cap is not None:
        new_stacks = min(new_stacks, definition.stack_cap)
    _upsert(db, unit_id, status_id, new_stacks, duration)


def _upsert(db: Database, unit_id: int, status_id: str, stacks: int,
            duration: int | None) -> None:
    db.execute(
        "INSERT INTO battle_unit_statuses "
        "(battle_unit_id, status_id, stacks, duration_remaining) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(battle_unit_id, status_id) DO UPDATE SET "
        "stacks = excluded.stacks, duration_remaining = excluded.duration_remaining",
        (unit_id, status_id, stacks, duration),
    )


def remove_status(db: Database, unit_id: int, status_id: str) -> None:
    db.execute(
        "DELETE FROM battle_unit_statuses WHERE battle_unit_id = ? AND status_id = ?",
        (unit_id, status_id),
    )


def remove_by_category(db: Database, registry: StatusRegistry, unit_id: int,
                       category: str, count: int) -> int:
    """Cleanse up to `count` cleansable statuses of one kind. Returns removals."""
    rows = db.query(
        "SELECT status_id FROM battle_unit_statuses WHERE battle_unit_id = ? "
        "ORDER BY status_id",
        (unit_id,),
    )
    removed = 0
    for row in rows:
        if removed >= count:
            break
        definition = registry.get(row["status_id"])
        if definition.kind == category and definition.cleansable:
            remove_status(db, unit_id, row["status_id"])
            removed += 1
    return removed


# =====================================================================
# Queries
# =====================================================================
def stacks_of(db: Database, unit_id: int, status_id: str) -> int:
    row = db.one(
        "SELECT stacks FROM battle_unit_statuses "
        "WHERE battle_unit_id = ? AND status_id = ?",
        (unit_id, status_id),
    )
    return int(row["stacks"]) if row else 0


def has_status(db: Database, unit_id: int, status_id: str) -> bool:
    return stacks_of(db, unit_id, status_id) > 0


def active_statuses(db: Database, unit_id: int) -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT status_id, stacks, duration_remaining FROM battle_unit_statuses "
        "WHERE battle_unit_id = ? ORDER BY status_id",
        (unit_id,),
    )]


def attack_up_bonus(db: Database, registry: StatusRegistry, unit_id: int) -> float:
    """+% damage dealt, summed over 공격력 증가 stacks."""
    stacks = stacks_of(db, unit_id, ATTACK_UP)
    return registry.get(ATTACK_UP).magnitude * stacks if stacks else 0.0


def defense_down_bonus(db: Database, registry: StatusRegistry, unit_id: int) -> float:
    """+% damage taken, summed over 방어력 감소 stacks on the *target*."""
    stacks = stacks_of(db, unit_id, DEFENSE_DOWN)
    return registry.get(DEFENSE_DOWN).magnitude * stacks if stacks else 0.0


def heal_down_modifier(db: Database, registry: StatusRegistry, unit_id: int) -> float:
    """Multiplier applied to incoming healing; clamped so healing never inverts."""
    stacks = stacks_of(db, unit_id, HEAL_DOWN)
    if not stacks:
        return 1.0
    return max(0.0, 1.0 + registry.get(HEAL_DOWN).magnitude * stacks)


def speed_modifier(db: Database, registry: StatusRegistry, unit_id: int) -> float:
    """Flat speed delta from 속도 감소. Reorders the NEXT round (§2.1.1 rule 6)."""
    stacks = stacks_of(db, unit_id, SPEED_DOWN)
    return registry.get(SPEED_DOWN).magnitude * stacks if stacks else 0.0


# =====================================================================
# Clocks
# =====================================================================
def tick_turn_start_triggers(db: Database, registry: StatusRegistry,
                             unit_id: int) -> list[tuple[str, int]]:
    """§2.11 step 3 — TURN_START_TRIGGER clock.

    Returns [(status_id, flat_damage)] for the caller to apply. Damage ignores
    block and 방어력; the stack decays by 1 *after* the damage.
    """
    results: list[tuple[str, int]] = []
    for row in db.query(
        "SELECT status_id, stacks FROM battle_unit_statuses "
        "WHERE battle_unit_id = ? ORDER BY status_id",
        (unit_id,),
    ):
        definition = registry.get(row["status_id"])
        if definition.clock != TURN_START_TRIGGER:
            continue
        stacks = int(row["stacks"])
        if stacks <= 0:
            continue
        results.append((definition.status_id, int(definition.magnitude * stacks)))

        remaining = stacks - 1     # −1 stack after damage
        if remaining <= 0:
            remove_status(db, unit_id, definition.status_id)
        else:
            db.execute(
                "UPDATE battle_unit_statuses SET stacks = ? "
                "WHERE battle_unit_id = ? AND status_id = ?",
                (remaining, unit_id, definition.status_id),
            )
    return results


def tick_owner_turn_countdown(db: Database, registry: StatusRegistry,
                              unit_id: int) -> list[str]:
    """§2.11 step 6 — decrement countdown / stack_duration statuses at turn END.

    stack_decay statuses are NOT touched here; they decayed at step 3. Returns
    the ids that expired.
    """
    expired: list[str] = []
    for row in db.query(
        "SELECT status_id, duration_remaining FROM battle_unit_statuses "
        "WHERE battle_unit_id = ? ORDER BY status_id",
        (unit_id,),
    ):
        definition = registry.get(row["status_id"])
        if definition.model == STACK_DECAY:
            continue
        if definition.clock != OWNER_TURN_COUNTDOWN:
            continue
        remaining = (row["duration_remaining"] or 0) - 1
        if remaining <= 0:
            remove_status(db, unit_id, definition.status_id)
            expired.append(definition.status_id)
        else:
            db.execute(
                "UPDATE battle_unit_statuses SET duration_remaining = ? "
                "WHERE battle_unit_id = ? AND status_id = ?",
                (remaining, unit_id, definition.status_id),
            )
    return expired
