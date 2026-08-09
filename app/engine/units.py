"""Battle unit loading and effective-stat resolution.

Effective stats are always `base + Σ(timed modifiers) + Σ(status modifiers)`.
Nothing writes a temporary change back into `battle_units` — §2.5.3 requires
expiry to subtract exactly what was added.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.connection import Database
from app.engine import statuses as st
from app.engine import timed_effects as te

ALLY = "ally"
ENEMY = "enemy"


def opposite(side: str) -> str:
    return ENEMY if side == ALLY else ALLY


@dataclass
class Unit:
    battle_unit_id: int
    battle_id: int
    side: str
    registration_order: int
    visible_slot: int
    unit_def_id: str
    party_slot: int | None
    hp_current: int
    hp_max: int
    base_atk: int
    base_def: int
    base_spd: int
    element: str
    role: str
    block: int
    is_alive: bool
    boss_phase: int | None

    @property
    def hp_percent(self) -> float:
        return self.hp_current / self.hp_max if self.hp_max else 0.0


def load_unit(db: Database, unit_id: int) -> Unit:
    row = db.one("SELECT * FROM battle_units WHERE battle_unit_id = ?", (unit_id,))
    if row is None:
        raise KeyError(f"battle unit {unit_id} does not exist")
    return _from_row(row)


def load_units(db: Database, battle_id: int, *, side: str | None = None,
               living_only: bool = False) -> list[Unit]:
    sql = "SELECT * FROM battle_units WHERE battle_id = ?"
    params: list = [battle_id]
    if side is not None:
        sql += " AND side = ?"
        params.append(side)
    if living_only:
        sql += " AND is_alive = 1"
    sql += " ORDER BY registration_order"
    return [_from_row(row) for row in db.query(sql, tuple(params))]


def _from_row(row) -> Unit:
    return Unit(
        battle_unit_id=row["battle_unit_id"],
        battle_id=row["battle_id"],
        side=row["side"],
        registration_order=row["registration_order"],
        visible_slot=row["visible_slot"],
        unit_def_id=row["unit_def_id"],
        party_slot=row["party_slot"],
        hp_current=row["hp_current"],
        hp_max=row["hp_max"],
        base_atk=row["atk"],
        base_def=row["def"],
        base_spd=row["spd"],
        element=row["element"],
        role=row["role"],
        block=row["block"],
        is_alive=bool(row["is_alive"]),
        boss_phase=row["boss_phase"],
    )


# -- effective stats -----------------------------------------------------
def effective_atk(db: Database, unit: Unit) -> float:
    return unit.base_atk + te.stat_delta(db, unit.battle_unit_id, "atk", unit.base_atk)


def effective_def(db: Database, unit: Unit) -> float:
    return unit.base_def + te.stat_delta(db, unit.battle_unit_id, "def", unit.base_def)


def effective_spd(db: Database, registry: st.StatusRegistry, unit: Unit) -> float:
    """속도 = base + timed modifiers + 속도 감소 stacks.

    Speed changes apply from the NEXT round (§2.1.1 rule 6) — this function is
    only ever consulted while building a round-order snapshot.
    """
    value = unit.base_spd + te.stat_delta(db, unit.battle_unit_id, "spd", unit.base_spd)
    return value + st.speed_modifier(db, registry, unit.battle_unit_id)


# -- mutation ------------------------------------------------------------
def set_block(db: Database, unit_id: int, value: int) -> None:
    db.execute("UPDATE battle_units SET block = ? WHERE battle_unit_id = ?",
               (max(0, int(value)), unit_id))


def add_block(db: Database, unit_id: int, amount: int) -> None:
    db.execute("UPDATE battle_units SET block = block + ? WHERE battle_unit_id = ?",
               (max(0, int(amount)), unit_id))


def consume_block(db: Database, unit_id: int, amount: int) -> None:
    db.execute(
        "UPDATE battle_units SET block = MAX(0, block - ?) WHERE battle_unit_id = ?",
        (max(0, int(amount)), unit_id),
    )


def apply_hp_loss(db: Database, unit_id: int, hp_loss: int) -> int:
    """Apply damage, clamping HP at 0. Returns the new HP.

    §2.8.4 threshold policy step 1: full damage lands and HP clamps at 0 before
    any phase evaluation.
    """
    db.execute(
        "UPDATE battle_units SET hp_current = MAX(0, hp_current - ?) "
        "WHERE battle_unit_id = ?",
        (max(0, int(hp_loss)), unit_id),
    )
    row = db.one("SELECT hp_current FROM battle_units WHERE battle_unit_id = ?", (unit_id,))
    return int(row["hp_current"])


def heal(db: Database, unit_id: int, amount: int) -> int:
    db.execute(
        "UPDATE battle_units SET hp_current = MIN(hp_max, hp_current + ?) "
        "WHERE battle_unit_id = ?",
        (max(0, int(amount)), unit_id),
    )
    row = db.one("SELECT hp_current FROM battle_units WHERE battle_unit_id = ?", (unit_id,))
    return int(row["hp_current"])


def death_check(db: Database, unit_id: int) -> bool:
    """§2.11 ★ DEATH CHECK. Returns True if the unit is now dead.

    A dead unit RETAINS its row and its visible_slot (§2.6.1 rule 2), so the
    battle screen never reshuffles mid-fight.
    """
    row = db.one(
        "SELECT hp_current, is_alive FROM battle_units WHERE battle_unit_id = ?",
        (unit_id,),
    )
    if row is None:
        return False
    if row["hp_current"] <= 0 and row["is_alive"]:
        db.execute("UPDATE battle_units SET is_alive = 0 WHERE battle_unit_id = ?",
                   (unit_id,))
        # A dead enemy's plan is removed (§2.8.6).
        db.execute("DELETE FROM enemy_plans WHERE enemy_unit_id = ?", (unit_id,))
        return True
    return not row["is_alive"]


def next_registration_order(db: Database, battle_id: int, side: str) -> int:
    """Monotonic per battle-side, assigned at insert, NEVER reused (§2.6.1)."""
    row = db.one(
        "SELECT MAX(registration_order) AS m FROM battle_units "
        "WHERE battle_id = ? AND side = ?",
        (battle_id, side),
    )
    return (row["m"] or 0) + 1


def next_visible_slot(db: Database, battle_id: int, side: str) -> int:
    """Lowest unused visible_slot on that side (§2.6.1 rule 3).

    Slots are never reclaimed from the dead, so this only ever grows.
    """
    used = {
        row["visible_slot"]
        for row in db.query(
            "SELECT visible_slot FROM battle_units WHERE battle_id = ? AND side = ?",
            (battle_id, side),
        )
    }
    slot = 0
    while slot in used:
        slot += 1
    return slot
