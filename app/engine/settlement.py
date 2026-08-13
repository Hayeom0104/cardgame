"""§8.6.3 run-inventory settlement and the §16.2.1 `run_settlement` step machine.

Every terminal path passes through settlement, so the logic exists in exactly
one place. Settlement **intent is persisted before any mutation** (B-08): a
crash after `state = run_settlement` must not lose either *why* the run ended
or *what had already been done*.

포기 is treated identically to 패배. That is what closes the farm-and-quit
exploit: quitting immediately after a drop lands in the shallow band, which
retains nothing. Diving deeper is the only way to keep anything.
"""

from __future__ import annotations

import json
import logging

from app.content.balance import Balance
from app.db.connection import Database, utcnow
from app.engine import achievements as ach
from app.engine.rng import JournaledRng

logger = logging.getLogger(__name__)

# settlement_step values, advanced one at a time, each committed before the next.
STEP_INVENTORY = "inventory"
STEP_REWARDS = "rewards"
STEP_ACHIEVEMENTS = "achievements"
STEP_RENDER = "render"
STEP_SURFACE = "surface"
STEP_DONE = "done"

STEP_ORDER = (STEP_INVENTORY, STEP_REWARDS, STEP_ACHIEVEMENTS, STEP_RENDER,
              STEP_SURFACE, STEP_DONE)

RUN_COMPLETED = "run_completed"
RUN_DEFEATED = "run_defeated"
RUN_ABANDONED = "run_abandoned"
RUN_EXPIRED = "run_expired"
ADMIN_TERMINATED = "admin_terminated"

TERMINAL_STATES = (RUN_COMPLETED, RUN_DEFEATED, RUN_ABANDONED, RUN_EXPIRED,
                   ADMIN_TERMINATED)


# =====================================================================
# §8.6.1 drop tier — world sets the band, depth picks within it
# =====================================================================
def drop_tier_for(db: Database, content_version_id: int, world_id: str,
                  depth: int) -> int:
    """
        WORLD  decides the tier BAND
        DEPTH within the run decides the position INSIDE that band
               (depths 1-3 → low end, 4-5 → mid, 6-7 → high end)

    World 1 is uniformly T0 *because its band has one value*, and from world 2
    onward diving deeper genuinely raises quality — both owner statements hold.

    깊이가 등급의 어디쯤에 대응하는지는 `config/06_경제.toml` 의
    `equipment_drop_tier_by_depth` 가 정한다.
    """
    balance = Balance(db, content_version_id)
    row = db.one(
        "SELECT drop_tier_min, drop_tier_max FROM worlds "
        "WHERE content_version_id = ? AND world_id = ?",
        (content_version_id, world_id),
    )
    if row is None:
        return 0
    low, high = int(row["drop_tier_min"]), int(row["drop_tier_max"])
    if high <= low:
        return low

    span = high - low
    # 깊이가 깊을수록 그 월드의 최고 등급에 가까워진다.
    position = 1.0
    for entry in balance.get("equipment_drop_tier_by_depth"):
        if depth <= int(entry["max_depth"]):
            position = float(entry["position"])
            break
    return low + int(round(span * position))


# =====================================================================
# §15.10 retention bands
# =====================================================================
def retention_band(balance: Balance, deepest_depth: int, *,
                   boss_cleared: bool = False) -> dict:
    if boss_cleared:
        return dict(balance.get("retention_boss_cleared"))
    for band in balance.get("retention_bands"):
        if deepest_depth <= int(band["max_depth"]):
            return dict(band)
    return dict(balance.get("retention_bands")[-1])


# =====================================================================
# §8.6.3 settle_run_inventory
# =====================================================================
def settle_run_inventory(db: Database, balance: Balance, rng: JournaledRng, *,
                         run_id: int, target_state: str,
                         op_key: str) -> dict:
    """Transfer, tier down, or destroy the run inventory.

        if run ended in run_completed OR admin_terminated:
            transfer 100%, tiers unchanged
            # admin_terminated is operator fault, never charged to the player
        else (run_defeated | run_abandoned | run_expired):
            keep_count = floor(item_count × band.retain_rate)
            keep_set   = keep_count items chosen by JOURNALED rng
            each kept item: with band.tier_down_chance, tier -= 1
                            equipment at T0 that tiers down is DESTROYED
                            a stone at T1 that tiers down is DESTROYED
            everything outside keep_set is destroyed
    """
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        raise KeyError(f"run {run_id} does not exist")

    entries = [dict(row) for row in db.query(
        "SELECT * FROM run_inventory WHERE run_id = ? ORDER BY entry_id", (run_id,))]

    report = {"kept": [], "lost": [], "tiered_down": [], "destroyed_by_tier_down": []}
    if not entries:
        return report

    if target_state in (RUN_COMPLETED, ADMIN_TERMINATED):
        for entry in entries:
            _transfer(db, run["user_id"], entry)
            report["kept"].append(entry)
        db.execute("DELETE FROM run_inventory WHERE run_id = ?", (run_id,))
        return report

    band = retention_band(balance, int(run["deepest_depth_reached"]))
    keep_count = int(len(entries) * float(band["retain_rate"]))
    entry_ids = [entry["entry_id"] for entry in entries]
    keep_ids = set(rng.sample(op_key, entry_ids, keep_count))

    for entry in entries:
        if entry["entry_id"] not in keep_ids:
            report["lost"].append(entry)
            continue

        tier = int(entry["tier"] or 0)
        if rng.chance(f"{op_key}:down:{entry['entry_id']}",
                      float(band["tier_down_chance"])):
            # FLOOR RULE (C-05): equipment at T0 and stones at T1 are DESTROYED
            # rather than tiering into a tier that does not exist.
            floor_tier = 1 if entry["kind"] == "stone" else 0
            if tier <= floor_tier:
                report["destroyed_by_tier_down"].append(entry)
                continue
            entry = {**entry, "tier": tier - 1}
            if entry["kind"] == "stone":
                entry["stone_tier"] = tier - 1
            report["tiered_down"].append(entry)

        _transfer(db, run["user_id"], entry)
        report["kept"].append(entry)

    db.execute("DELETE FROM run_inventory WHERE run_id = ?", (run_id,))
    return report


def _transfer(db: Database, user_id: int, entry: dict) -> None:
    """Move one settled entry onto the account.

    Enhancement stones follow the SAME rule as equipment (🟡 R-1), counted as
    units, with tier-down applied per kept stone.
    """
    if entry["kind"] == "equipment":
        db.execute(
            "INSERT INTO owned_equipment (user_id, equipment_def_id, tier) "
            "VALUES (?, ?, ?)",
            (user_id, entry["equipment_def_id"], int(entry["tier"] or 0)),
        )
    else:
        db.execute(
            "INSERT INTO enhancement_stones (user_id, tier, amount) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, tier) DO UPDATE SET amount = amount + excluded.amount",
            (user_id, int(entry["stone_tier"] or entry["tier"] or 1),
             int(entry["amount"] or 1)),
        )


# =====================================================================
# §15.4 run-clear coin (C-07)
# =====================================================================
def run_clear_coin(balance: Balance, *, deepest_depth: int,
                   first_clear: bool) -> int:
    """
        run_clear_coin = BASE_CLEAR_COIN
                       + PER_DEPTH_COIN × deepest_depth_reached
                       + FIRST_CLEAR_BONUS   (first clear of that world only)

    A defeated, abandoned or expired run pays 0 — coin is granted only in
    `settlement_step = 'rewards'`, which runs only when the target state is
    `run_completed`.
    """
    total = int(balance.get("base_clear_coin"))
    total += int(balance.get("per_depth_coin")) * max(0, deepest_depth)
    if first_clear:
        total += int(balance.get("first_clear_bonus"))
    return total


# =====================================================================
# §16.2.1 entering settlement
# =====================================================================
def enter_settlement(db: Database, run_id: int, *, target_state: str,
                     end_reason: str) -> None:
    """Write the settlement intent in ONE transaction, then begin step 1.

    Recovery re-enters at the recorded `settlement_step`; every step is
    idempotent through its receipt (§17.6) or its journal key (§16.4).
    """
    if target_state not in TERMINAL_STATES:
        raise ValueError(f"{target_state!r} is not a terminal run state")
    with db.tx() as conn:
        conn.execute(
            "UPDATE runs SET state = 'run_settlement', settlement_target_state = ?, "
            "settlement_step = ?, end_reason = ?, settlement_rng_op_key = ?, "
            "settlement_tx_id = ?, updated_at = ? WHERE run_id = ?",
            (target_state, STEP_INVENTORY, end_reason,
             f"settle:{run_id}:keepset", f"settle:{run_id}", utcnow(), run_id),
        )


def advance_settlement(db: Database, balance: Balance, rng: JournaledRng, *,
                       run_id: int, content_version_id: int,
                       grant_coin=None) -> dict:
    """Run the settlement steps from wherever the row currently sits.

    `grant_coin(user_id, amount, tx_id)` is injected so step 2 can suspend on a
    §17 transaction without this module importing the Central client.
    """
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        raise KeyError(f"run {run_id} does not exist")
    if run["state"] != "run_settlement":
        raise ValueError(f"run {run_id} is not in run_settlement")

    target_state = run["settlement_target_state"]
    report: dict = {"target_state": target_state, "steps": []}
    step = run["settlement_step"] or STEP_INVENTORY

    while step != STEP_DONE:
        if step == STEP_INVENTORY:
            report["inventory"] = settle_run_inventory(
                db, balance, rng, run_id=run_id, target_state=target_state,
                op_key=run["settlement_rng_op_key"] or f"settle:{run_id}:keepset",
            )

        elif step == STEP_REWARDS:
            if target_state == RUN_COMPLETED:
                report["rewards"] = _grant_rewards(
                    db, balance, run, content_version_id, grant_coin)
            else:
                report["rewards"] = {"coin": 0, "carta": 0,
                                     "note": "non-completed runs pay nothing"}

        elif step == STEP_ACHIEVEMENTS:
            report["achievements"] = _advance_achievements(
                db, run, target_state, content_version_id)

        elif step == STEP_RENDER:
            # §11 requires the player to see what was kept and what was lost.
            report["render"] = {"pending": True}

        elif step == STEP_SURFACE:
            # §16.8 terminal surface policy is applied by the lifecycle layer.
            report["surface"] = {"pending": True}

        report["steps"].append(step)
        step = STEP_ORDER[STEP_ORDER.index(step) + 1]
        db.execute("UPDATE runs SET settlement_step = ?, updated_at = ? WHERE run_id = ?",
                   (step, utcnow(), run_id))

    # 6. 'done' — write settlement_target_state as the run's final state.
    db.execute(
        "UPDATE runs SET state = ?, ended_at = ?, updated_at = ? WHERE run_id = ?",
        (target_state, utcnow(), utcnow(), run_id),
    )
    report["final_state"] = target_state
    return report


def _grant_rewards(db: Database, balance: Balance, run, content_version_id: int,
                   grant_coin) -> dict:
    user_id = run["user_id"]
    world_id = run["world_id"]
    depth = int(run["deepest_depth_reached"])

    # §3.4.3 — 튜토리얼 클리어는 자체 보상표를 쓴다: 파티 슬롯 2와 카르타 300을
    # 받고, 튜토리얼은 영구 잠금되며 본편이 열린다. 런 클리어 코인은 지급하지
    # 않는다 — 튜토리얼은 §15.4의 경제 모델 밖이다.
    if run["is_tutorial"]:
        from app.engine.progression import complete_tutorial

        result = complete_tutorial(db, balance, user_id=user_id,
                                   content_version_id=content_version_id)
        return {"coin": 0, "carta": result.get("carta", 0), "tutorial": result,
                "next_world_unlocked": result.get("unlocked_world")}

    unlock = db.one(
        "SELECT first_cleared_at FROM world_unlocks WHERE user_id = ? AND world_id = ?",
        (user_id, world_id),
    )
    first_clear = not (unlock and unlock["first_cleared_at"])

    coin = run_clear_coin(balance, deepest_depth=depth, first_clear=first_clear)
    carta_band = balance.get("carta_income")
    span = int(carta_band["run_clear_max"]) - int(carta_band["run_clear_min"])
    full_depth = float(balance.get("run_clear_carta_full_depth"))
    carta = int(carta_band["run_clear_min"]) + int(span * min(1.0, depth / full_depth))

    with db.tx() as conn:
        # 카르타 is local and immediate.
        conn.execute(
            "UPDATE accounts SET carta = carta + ?, updated_at = ? WHERE user_id = ?",
            (carta, utcnow(), user_id),
        )
        conn.execute(
            "UPDATE world_unlocks SET first_cleared_at = COALESCE(first_cleared_at, ?) "
            "WHERE user_id = ? AND world_id = ?",
            (utcnow(), user_id, world_id),
        )
        # Unlock the NEXT world at the account level (§3.6).
        next_world = conn.execute(
            "SELECT world_id FROM worlds WHERE content_version_id = ? "
            "AND sequence_index = (SELECT sequence_index + 1 FROM worlds "
            "WHERE content_version_id = ? AND world_id = ?)",
            (content_version_id, content_version_id, world_id),
        ).fetchone()
        if next_world is not None:
            conn.execute(
                "INSERT OR IGNORE INTO world_unlocks (user_id, world_id, unlocked_at) "
                "VALUES (?, ?, ?)", (user_id, next_world["world_id"], utcnow()),
            )

    # 코인 goes through a §17 `grant` transaction keyed settlement_tx_id.
    coin_result = None
    if grant_coin is not None and coin:
        coin_result = grant_coin(user_id, coin, run["settlement_tx_id"])

    return {
        "coin": coin,
        "coin_result": coin_result,
        "carta": carta,
        "first_clear": first_clear,
        "next_world_unlocked": next_world["world_id"] if next_world else None,
    }


def _advance_achievements(db: Database, run, target_state: str,
                          content_version_id: int) -> dict:
    """§20.2 engine hooks fire from the settlement step, keyed `settle:<run_id>`."""
    if target_state != RUN_COMPLETED:
        return {"updates": []}
    result = ach.advance_counter(
        db, run["user_id"], ach.BOSS_DEFEATED, 1,
        mutation_id=f"settle:{run['run_id']}:boss",
        content_version_id=content_version_id,
    )
    cleared = ach.advance_counter(
        db, run["user_id"], ach.RUN_CLEARED, 1,
        mutation_id=f"settle:{run['run_id']}:cleared",
        content_version_id=content_version_id,
    )
    return {
        "updates": [update.__dict__ for update in result.updates + cleared.updates],
        "carta_granted": result.carta_granted + cleared.carta_granted,
    }


def settlement_report_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, default=str)
