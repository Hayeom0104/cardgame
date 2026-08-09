"""Battle setup — §2.6 composition, §15.1 party-size-2 downscaling, §2.6.1 summons."""

from __future__ import annotations

import json

from app.content.balance import Balance
from app.db.connection import Database, utcnow
from app.engine import statuses as st
from app.engine import stats
from app.engine import units as un


def _enemy_def(db: Database, content_version_id: int, enemy_id: str):
    row = db.one(
        "SELECT * FROM enemies WHERE content_version_id = ? AND enemy_id = ?",
        (content_version_id, enemy_id),
    )
    if row is None:
        raise KeyError(f"enemy {enemy_id!r} is not defined at version {content_version_id}")
    return row


def downscale_for_party_size(units: list[dict], party_size: int,
                             tier_rank: dict[str, int]) -> tuple[list[dict], dict]:
    """§15.1 — deterministic party-size-2 downscaling.

        Encounters are authored for party size 3.
        For party size 2:
          if the authored encounter has >= 2 units:
              remove EXACTLY ONE — the lowest-tier unit;
              tie-break by HIGHEST authored slot index
          if it has exactly 1 unit: remove nothing

    Summoned units are never affected — they arrive after adjustment.

    The rule is written for party size 2 and applies to **exactly** that.
    Party size 1 exists only in the tutorial world (§4.1), whose encounters are
    authored directly against the solo starter (§15.9's 1-2 enemies per node);
    thinning them further would contradict that tuning.
    """
    if party_size != 2 or len(units) < 2:
        return list(units), {"removed": None, "reason": "no adjustment"}

    def sort_key(entry: dict) -> tuple[int, int]:
        return (tier_rank.get(entry.get("tier", "일반"), 0), -int(entry.get("slot", 0)))

    victim = min(units, key=sort_key)
    remaining = [entry for entry in units if entry is not victim]
    return remaining, {
        "removed": victim.get("enemy_id"),
        "removed_slot": victim.get("slot"),
        "reason": f"party_size={party_size} downscaling",
    }


def create_battle(db: Database, balance: Balance, *, run_id: int, node_index: int,
                  encounter_id: str, content_version_id: int,
                  is_boss: bool = False) -> int:
    """Materialize a battle from an authored encounter.

    A tutorial retry inserts a NEW battle row at attempt_no + 1 (§3.4.2); the
    previous row is retained for telemetry and never destructively reset.
    """
    encounter = db.one(
        "SELECT * FROM encounters WHERE content_version_id = ? AND encounter_id = ?",
        (content_version_id, encounter_id),
    )
    if encounter is None:
        raise KeyError(f"encounter {encounter_id!r} is not defined")

    party = db.query(
        "SELECT rc.*, rbs.character_id AS snap_character_id, rbs.star_rank, "
        "rbs.research_stat_step, rbs.equipment_json FROM run_characters rc "
        "LEFT JOIN run_build_snapshot rbs ON rbs.run_id = rc.run_id "
        "AND rbs.party_slot = rc.party_slot WHERE rc.run_id = ? ORDER BY rc.party_slot",
        (run_id,),
    )
    party_size = len(party)

    authored = json.loads(encounter["units_json"])
    tier_rank = {"일반": 0, "엘리트": 1, "보스": 2}
    for entry in authored:
        if "tier" not in entry:
            entry["tier"] = _enemy_def(db, content_version_id, entry["enemy_id"])["tier"]
    adjusted, adjustment = downscale_for_party_size(authored, party_size, tier_rank)

    cap = int(balance.get("max_enemies_per_encounter"))
    if len(adjusted) > cap:
        raise ValueError(
            f"encounter {encounter_id!r} exceeds the {cap}-enemy cap after adjustment"
        )

    pool = balance.get("resource_pool_by_party_size")
    resource = int(pool.get(str(party_size), pool[str(max(pool, key=int))]))

    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    attempt = db.one(
        "SELECT MAX(attempt_no) AS m FROM battles WHERE run_id = ? AND node_index = ?",
        (run_id, node_index),
    )
    attempt_no = (attempt["m"] or 0) + 1

    cursor = db.execute(
        "INSERT INTO battles (run_id, node_index, attempt_no, state, round_no, "
        "turn_cursor, turn_phase, party_resource_current, source_encounter_id, "
        "applied_adjustment_json, rng_seed, rng_counter, created_at) "
        "VALUES (?, ?, ?, 'active', 1, 0, 'await_card', ?, ?, ?, ?, 0, ?)",
        (run_id, node_index, attempt_no, resource, encounter_id,
         json.dumps(adjustment, ensure_ascii=False), run["rng_seed"], utcnow()),
    )
    battle_id = int(cursor.lastrowid)

    for index, member in enumerate(party):
        _insert_ally(db, balance, battle_id, member, index, content_version_id)
    for entry in sorted(adjusted, key=lambda e: int(e.get("slot", 0))):
        _insert_enemy(db, battle_id, entry["enemy_id"], content_version_id,
                      is_boss=is_boss or entry.get("tier") == "보스")

    return battle_id


def _insert_ally(db: Database, balance: Balance, battle_id: int, member,
                 index: int, content_version_id: int) -> int:
    character = db.one(
        "SELECT * FROM characters WHERE content_version_id = ? AND character_id = ?",
        (content_version_id, member["character_id"]),
    )
    if character is None:
        raise KeyError(f"character {member['character_id']!r} is not defined")

    equipment_flat = _equipment_flat(db, content_version_id, member["equipment_json"])
    snapshot = stats.BuildSnapshot(
        character_id=member["character_id"],
        star_rank=member["star_rank"] or 1,
        research_stat_step=member["research_stat_step"] or 0,
        job_role=character["job_role"],
        equipment_flat=equipment_flat,
    )
    block = stats.character_stats(balance, snapshot)

    cursor = db.execute(
        "INSERT INTO battle_units (battle_id, side, registration_order, visible_slot, "
        "unit_def_id, party_slot, hp_current, hp_max, atk, def, spd, element, role) "
        "VALUES (?, 'ally', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (battle_id, index + 1, index, member["character_id"], member["party_slot"],
         member["hp_current"], member["hp_max"] or block["hp"],
         block["atk"], block["def"], block["spd"],
         character["element"], character["job_role"]),
    )
    return int(cursor.lastrowid)


def _equipment_flat(db: Database, content_version_id: int,
                    equipment_json: str | None) -> dict[str, int]:
    """§8.5 — equipment bonuses are flat additive, applied after the multipliers."""
    totals = {"hp": 0, "atk": 0, "def": 0, "spd": 0}
    if not equipment_json:
        return totals
    for entry in json.loads(equipment_json).values():
        definition = db.one(
            "SELECT * FROM equipment_defs WHERE content_version_id = ? "
            "AND equipment_def_id = ?",
            (content_version_id, entry.get("def_id")),
        )
        if definition is None:
            continue
        # Tier scales the piece linearly; T0 contributes its base values.
        scale = 1.0 + 0.25 * int(entry.get("tier", 0))
        totals["hp"] += int(definition["hp_flat"] * scale)
        totals["atk"] += int(definition["atk_flat"] * scale)
        totals["def"] += int(definition["def_flat"] * scale)
        totals["spd"] += int(definition["spd_flat"] * scale)
    return totals


def _insert_enemy(db: Database, battle_id: int, enemy_id: str,
                  content_version_id: int, *, is_boss: bool = False) -> int:
    definition = _enemy_def(db, content_version_id, enemy_id)
    registration = un.next_registration_order(db, battle_id, un.ENEMY)
    slot = un.next_visible_slot(db, battle_id, un.ENEMY)
    cursor = db.execute(
        "INSERT INTO battle_units (battle_id, side, registration_order, visible_slot, "
        "unit_def_id, hp_current, hp_max, atk, def, spd, element, role, boss_phase) "
        "VALUES (?, 'enemy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (battle_id, registration, slot, enemy_id, definition["hp"], definition["hp"],
         definition["atk"], definition["def"], definition["spd"],
         definition["element"], definition["role"], 1 if is_boss else None),
    )
    return int(cursor.lastrowid)


def summon_enemy(db: Database, battle_id: int, enemy_id: str,
                 content_version_id: int, balance: Balance) -> int | None:
    """§2.6.1 — insert a summoned enemy, respecting the hard cap of 8.

    The unit joins the NEXT round's snapshot (rule 6), which `build_round_order`
    handles by simply reading living units at the boundary.
    """
    cap = int(balance.get("max_enemies_per_encounter"))
    count = db.one(
        "SELECT COUNT(*) AS n FROM battle_units WHERE battle_id = ? AND side = 'enemy'",
        (battle_id,),
    )
    if int(count["n"]) >= cap:
        return None
    return _insert_enemy(db, battle_id, enemy_id, content_version_id)


def party_size(db: Database, run_id: int) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM run_characters WHERE run_id = ?", (run_id,))
    return int(row["n"])


def sync_party_hp_to_run(db: Database, battle_id: int, run_id: int) -> None:
    """§2.4 — HP persists across map nodes within a run. No automatic heal."""
    for unit in un.load_units(db, battle_id, side=un.ALLY):
        if unit.party_slot is None:
            continue
        db.execute(
            "UPDATE run_characters SET hp_current = ? WHERE run_id = ? AND party_slot = ?",
            (unit.hp_current, run_id, unit.party_slot),
        )
