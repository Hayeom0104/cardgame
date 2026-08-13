"""§16 — run lifecycle, the `preparing` build flow, and mutation concurrency.

Two guarantees this module owns:

* **One committed authoritative state transition = exactly one revision
  increment, owned solely by the §16.7 CAS** (B-14). The round boundary does
  not bump the counter; a unit turn and any round boundary it triggers are one
  transition committed under one CAS.
* **The account build is snapshotted, not locked** (§16.2.3, B-10). Hub
  commands stay available during a run — locking them for 20-30 minutes would
  be hostile — so every in-run stat computation reads the snapshot and account
  mutations take effect on the *next* run.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field

from app.content.balance import Balance
from app.db.connection import Database, utcnow
from app.engine import map_gen
from app.engine.rng import JournaledRng

logger = logging.getLogger(__name__)

# §16.1 active states
PREPARING = "preparing"
MAP_NAVIGATION = "map_navigation"
NODE_RESOLUTION = "node_resolution"
BATTLE = "battle"
BOSS_BATTLE = "boss_battle"
POST_BATTLE = "post_battle"
REWARD_SELECTION = "reward_selection"
EVENT_CHOICE = "event_choice"
SHOP = "shop"
RUN_SETTLEMENT = "run_settlement"

# terminal
RUN_COMPLETED = "run_completed"
RUN_DEFEATED = "run_defeated"
RUN_ABANDONED = "run_abandoned"
RUN_EXPIRED = "run_expired"
ADMIN_TERMINATED = "admin_terminated"

# exceptional — excluded from play, surfaced to operators, NEVER silently deleted
RECOVERY_REQUIRED = "recovery_required"

TERMINAL_STATES = frozenset({RUN_COMPLETED, RUN_DEFEATED, RUN_ABANDONED,
                             RUN_EXPIRED, ADMIN_TERMINATED})

ACTIVE_STATES = frozenset({PREPARING, MAP_NAVIGATION, NODE_RESOLUTION, BATTLE,
                           BOSS_BATTLE, POST_BATTLE, REWARD_SELECTION,
                           EVENT_CHOICE, SHOP, RUN_SETTLEMENT})

#: §16.2 legal transitions. Every command and component interaction validates
#: the current state first; an illegal action is rejected with an ephemeral
#: notice and a forced re-render, never silently ignored.
TRANSITIONS: dict[str, frozenset[str]] = {
    PREPARING: frozenset({MAP_NAVIGATION, RECOVERY_REQUIRED, RUN_SETTLEMENT}),
    MAP_NAVIGATION: frozenset({NODE_RESOLUTION, RUN_SETTLEMENT}),
    NODE_RESOLUTION: frozenset({BATTLE, BOSS_BATTLE, REWARD_SELECTION, EVENT_CHOICE,
                                SHOP, MAP_NAVIGATION, RUN_SETTLEMENT}),
    # boss_battle victory ALWAYS ends the run — one world = one run (§3.6).
    BATTLE: frozenset({POST_BATTLE, NODE_RESOLUTION, RUN_SETTLEMENT}),
    BOSS_BATTLE: frozenset({RUN_SETTLEMENT, NODE_RESOLUTION}),
    POST_BATTLE: frozenset({REWARD_SELECTION, MAP_NAVIGATION, RUN_SETTLEMENT}),
    REWARD_SELECTION: frozenset({MAP_NAVIGATION, RUN_SETTLEMENT}),
    # an event branch may be a TERMINAL_STATE_TRANSITION operator → battle
    EVENT_CHOICE: frozenset({MAP_NAVIGATION, BATTLE, RUN_SETTLEMENT}),
    SHOP: frozenset({SHOP, MAP_NAVIGATION, RUN_SETTLEMENT}),   # SELF-LOOP
    RUN_SETTLEMENT: frozenset(TERMINAL_STATES),
}


class LifecycleError(RuntimeError):
    pass


class StaleRevisionError(LifecycleError):
    """The §16.7 CAS was lost. Reject and re-render; mutate nothing."""


# =====================================================================
# §16.7 compare-and-swap
# =====================================================================
def claim_mutation(db: Database, run_id: int, expected_revision: int) -> int:
    """Win the revision increment, or perform no mutation at all.

    Gates 1-4 of §1.3.10 are individually correct but not atomic: two
    simultaneous component clicks can both read revision R, both pass
    validation, and both mutate. Only the CAS winner proceeds.

    The caller must hold — or immediately open — the same local transaction as
    its mutation, so the increment and the state change commit together.
    """
    cursor = db.execute(
        "UPDATE runs SET presentation_revision = presentation_revision + 1, "
        "last_activity_at = ? WHERE run_id = ? AND presentation_revision = ?",
        (utcnow(), run_id, expected_revision),
    )
    if cursor.rowcount == 0:
        raise StaleRevisionError(
            f"run {run_id}: expected revision {expected_revision} but the row has moved"
        )
    return expected_revision + 1


def event_already_handled(db: Database, event_id: str) -> bool:
    """§16.7 — has this event_id already been processed to completion?

    Separate from `record_event` so a handler that raises does not leave the
    event marked handled; Central's redelivery must still reach us.
    """
    return db.one("SELECT 1 FROM processed_events WHERE event_id = ?",
                  (event_id,)) is not None


def record_event(db: Database, event_id: str, run_id: int | None) -> bool:
    """§16.7 duplicate event protection. Returns False if already handled.

    Central may redeliver an event; a duplicate `event_id` insert fails and the
    handler exits without re-applying.
    """
    try:
        db.execute(
            "INSERT INTO processed_events (event_id, run_id, handled_at) "
            "VALUES (?, ?, ?)", (event_id, run_id, utcnow()),
        )
        return True
    except Exception as error:
        if "UNIQUE" in str(error) or "PRIMARY KEY" in str(error):
            return False
        raise


def transition(db: Database, run_id: int, new_state: str, *,
               expected_revision: int | None = None) -> None:
    """Move a run between states, validating the §16.2 edge."""
    run = db.one("SELECT state FROM runs WHERE run_id = ?", (run_id,))
    if run is None:
        raise LifecycleError(f"run {run_id} does not exist")
    current = run["state"]
    allowed = TRANSITIONS.get(current, frozenset())
    if new_state not in allowed and new_state != RECOVERY_REQUIRED:
        raise LifecycleError(f"illegal transition {current!r} → {new_state!r}")
    with db.tx() as conn:
        if expected_revision is not None:
            claim_mutation(db, run_id, expected_revision)
        conn.execute("UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                     (new_state, utcnow(), run_id))


# =====================================================================
# §16.2.2 the `preparing` build flow
# =====================================================================
@dataclass
class RunBuildRequest:
    """Steps 1-4 are pure UI and hold NO run row, so abandoning them costs
    nothing and creates no `one_active_run` conflict."""

    user_id: int
    world_id: str
    party_character_ids: list[str]
    passive_card_ids: list[str] = field(default_factory=list)
    is_tutorial: bool = False


def selectable_passives(db: Database, user_id: int,
                        content_version_id: int) -> list[dict]:
    """§6 — the passives this account may bring into a run.

    This is the SINGLE source of truth: the preparing screen offers exactly
    this list and `validate_build` accepts exactly this list, so a forged
    `custom_id` submission can never smuggle in an id the screen never showed.

    §6 passive content is not authored in the launch content set (see README),
    and the `cards` table carries no passive marker — `cards.category` is one
    of 공격 | 방어 | 버프디버프 | 회복. There is therefore nothing to select yet,
    and this returns empty rather than filtering on a value that cannot exist.
    When passives are authored, this function is the only place that changes.
    """
    return []


def validate_build(db: Database, balance: Balance, request: RunBuildRequest,
                   content_version_id: int) -> None:
    """Gate the confirm step (§19.3 [4])."""
    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (request.user_id,))
    if account is None:
        raise LifecycleError("account does not exist")

    unlocked = db.one(
        "SELECT 1 FROM world_unlocks WHERE user_id = ? AND world_id = ?",
        (request.user_id, request.world_id),
    )
    if unlocked is None:
        raise LifecycleError("world is not unlocked for this account")

    party = request.party_character_ids
    if len(party) != len(set(party)):
        raise LifecycleError("duplicates of the same character are not allowed")
    if len(party) > int(account["party_slots"]):
        raise LifecycleError("party exceeds the account's unlocked slots")

    # Party size 1 exists ONLY in the tutorial world; the main campaign requires ≥2.
    if not request.is_tutorial and len(party) < 2:
        raise LifecycleError("본편은 파티원 2명부터 입장할 수 있습니다.")
    if request.is_tutorial and len(party) != 1:
        raise LifecycleError("tutorial runs use exactly the starter character")

    for character_id in party:
        owned = db.one(
            "SELECT 1 FROM owned_characters WHERE user_id = ? AND character_id = ?",
            (request.user_id, character_id),
        )
        if owned is None:
            raise LifecycleError(f"character {character_id!r} is not owned")

    passives = request.passive_card_ids
    if len(passives) != len(set(passives)):
        raise LifecycleError("the same passive cannot occupy two slots")
    if len(passives) > int(account["passive_slots"]):
        raise LifecycleError("too many passives for the account's slots")

    # Ownership, not just count: `run_passives` must never hold an id the
    # account cannot actually bring.
    if passives:
        legal = {row["card_id"] for row in
                 selectable_passives(db, request.user_id, content_version_id)}
        for passive_id in passives:
            if passive_id not in legal:
                raise LifecycleError(f"passive {passive_id!r} is not available")


def create_run(db: Database, balance: Balance, request: RunBuildRequest,
               content_version_id: int) -> int:
    """Step [5] MATERIALIZE — ONE local transaction.

    The run only exists from here; steps 1-4 created nothing.
    """
    validate_build(db, balance, request, content_version_id)

    existing = db.one(
        "SELECT run_id FROM runs WHERE user_id = ? AND state NOT IN "
        "('run_completed','run_defeated','run_abandoned','run_expired',"
        "'admin_terminated')",
        (request.user_id,),
    )
    if existing is not None:
        raise LifecycleError("이미 진행 중인 런이 있습니다.")

    seed = secrets.randbits(63)
    with db.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO runs (user_id, world_id, state, is_tutorial, "
            "content_version_id, map_seed, rng_seed, rng_counter, "
            "current_node_index, deepest_depth_reached, run_currency, "
            "logical_session_id, surface_generation, presentation_revision, "
            "created_at, updated_at, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, 1, 0, ?, 1, 0, ?, ?, ?)",
            (request.user_id, request.world_id, PREPARING, int(request.is_tutorial),
             content_version_id, seed, seed, "pending", utcnow(), utcnow(), utcnow()),
        )
        run_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE runs SET logical_session_id = ? WHERE run_id = ?",
            (f"dko-run-{run_id}", run_id),
        )

        account = conn.execute("SELECT * FROM accounts WHERE user_id = ?",
                               (request.user_id,)).fetchone()
        for slot, character_id in enumerate(request.party_character_ids, start=1):
            _snapshot_and_materialize(conn, balance, run_id, slot, character_id,
                                      account, content_version_id)

        for slot, passive_id in enumerate(request.passive_card_ids, start=1):
            conn.execute(
                "INSERT INTO run_passives (run_id, passive_slot, passive_card_id) "
                "VALUES (?, ?, ?)", (run_id, slot, passive_id),
            )

    # Map generation uses the journaled RNG, so a recovery replays it (§16.4).
    rng = JournaledRng(db, run_id, seed)
    generated = map_gen.generate_map(rng, balance)
    map_gen.persist_map(db, run_id, generated)
    if generated.used_fallback:
        logger.warning("run %s used the FALLBACK_TEMPLATE map", run_id)
    return run_id


def _snapshot_and_materialize(conn, balance: Balance, run_id: int, slot: int,
                              character_id: str, account, content_version_id: int) -> None:
    """§16.2.3 — freeze the account build, then build the run rows from it."""
    from app.engine import stats

    owned = conn.execute(
        "SELECT star_rank FROM owned_characters WHERE user_id = ? AND character_id = ?",
        (account["user_id"], character_id),
    ).fetchone()
    character = conn.execute(
        "SELECT * FROM characters WHERE content_version_id = ? AND character_id = ?",
        (content_version_id, character_id),
    ).fetchone()
    if character is None:
        raise LifecycleError(f"character {character_id!r} is not defined")

    equipment = {}
    for row in conn.execute(
        "SELECT equipment_def_id, tier, equipped_slot FROM owned_equipment "
        "WHERE user_id = ? AND equipped_character_id = ?",
        (account["user_id"], character_id),
    ).fetchall():
        equipment[row["equipped_slot"]] = {"def_id": row["equipment_def_id"],
                                           "tier": row["tier"]}

    card_upgrades = {
        row["card_id"]: row["upgrade_tier"]
        for row in conn.execute(
            "SELECT card_id, upgrade_tier FROM unlocked_cards WHERE user_id = ?",
            (account["user_id"],),
        ).fetchall()
    }

    conn.execute(
        "INSERT INTO run_build_snapshot (run_id, party_slot, character_id, star_rank, "
        "research_stat_step, equipment_json, card_upgrade_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, slot, character_id, owned["star_rank"] if owned else 1,
         account["stat_research_step"],
         json.dumps(equipment, ensure_ascii=False),
         json.dumps(card_upgrades, ensure_ascii=False)),
    )

    from app.engine.encounter import _equipment_flat

    class _Wrapper:
        """Adapt the open cursor to the Database read interface."""

        def one(self, sql, params=()):
            return conn.execute(sql, params).fetchone()

    snapshot = stats.BuildSnapshot(
        character_id=character_id,
        star_rank=owned["star_rank"] if owned else 1,
        research_stat_step=account["stat_research_step"],
        job_role=character["job_role"],
        equipment_flat=_equipment_flat(_Wrapper(), content_version_id,
                                       json.dumps(equipment)),
    )
    block = stats.character_stats(balance, snapshot)

    conn.execute(
        "INSERT INTO run_characters (run_id, party_slot, character_id, hp_current, "
        "hp_max) VALUES (?, ?, ?, ?, ?)",
        (run_id, slot, character_id, block["hp"], block["hp"]),
    )

    # 보상 nodes add cards at the snapshotted upgrade tier; the starting deck
    # is composed here at the §15.1 base size.
    for position, card_id in enumerate(
        _compose_deck(conn, balance, account["user_id"], character, content_version_id)
    ):
        conn.execute(
            "INSERT INTO run_deck_cards (run_id, party_slot, card_id, is_cursed, "
            "pile, pile_position) VALUES (?, ?, ?, 0, 'draw', ?)",
            (run_id, slot, card_id, position),
        )


def _compose_deck(conn, balance: Balance, user_id: int, character,
                  content_version_id: int) -> list[str]:
    """Build one character's run deck at exactly `base_deck_size` cards.

    §4.3: every character starts with 평타 and a basic defense card, both
    무속성, guaranteeing playability. The remainder is filled from the account's
    unlocked cards this character can legally play (§2.10) — for a fresh
    account holding only the starter skill that reproduces §4.6.2's 6/5/7 split
    exactly, and with it the 79.8% skill rate in a 3-card draw.
    """
    from app.content.seed import CARD_BASIC_ATTACK, CARD_BASIC_DEFENSE

    size = int(balance.get("base_deck_size"))
    composition = balance.get("starter_deck_composition")
    deck = ([CARD_BASIC_ATTACK] * int(composition["평타"])
            + [CARD_BASIC_DEFENSE] * int(composition["기본_방어"]))

    playable = [
        row["card_id"]
        for row in conn.execute(
            "SELECT uc.card_id FROM unlocked_cards uc JOIN cards c "
            "ON c.card_id = uc.card_id AND c.content_version_id = ? "
            "WHERE uc.user_id = ? AND c.is_retired = 0 AND c.element = ? "
            "AND uc.card_id NOT IN (?, ?) ORDER BY c.rarity_tier DESC, uc.card_id",
            (content_version_id, user_id, character["element"],
             CARD_BASIC_ATTACK, CARD_BASIC_DEFENSE),
        ).fetchall()
    ]
    if not playable:
        # No elemental card unlocked yet: pad with 평타 so the deck still
        # reaches base size and can never soft-lock a draw.
        playable = [CARD_BASIC_ATTACK]

    index = 0
    while len(deck) < size:
        deck.append(playable[index % len(playable)])
        index += 1
    return deck[:size]


# =====================================================================
# §16.3 scope, concurrency, expiry
# =====================================================================
def active_run_for(db: Database, user_id: int):
    return db.one(
        "SELECT * FROM runs WHERE user_id = ? AND state NOT IN "
        "('run_completed','run_defeated','run_abandoned','run_expired',"
        "'admin_terminated')",
        (user_id,),
    )


def run_by_thread(db: Database, thread_id: int):
    """`route_threads: true` delivers in-thread events; resolve the run by
    thread_id (§18.5)."""
    return db.one("SELECT * FROM runs WHERE thread_id = ?", (thread_id,))


def is_expired(db: Database, balance: Balance, run) -> bool:
    """30 minutes without a player interaction (§16.3)."""
    from datetime import datetime, timedelta, timezone

    minutes = int(balance.get("inactivity_expiry_minutes"))
    last = datetime.fromisoformat(run["last_activity_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last > timedelta(minutes=minutes)


def touch(db: Database, run_id: int) -> None:
    db.execute("UPDATE runs SET last_activity_at = ? WHERE run_id = ?",
               (utcnow(), run_id))


# =====================================================================
# §16.8 startup recovery
# =====================================================================
def recover_runs(db: Database, balance: Balance) -> list[dict]:
    """Scan non-terminal runs and classify what each needs.

    Returns a plan rather than acting, so the caller can order surface work
    through the §16.6 single-writer queue.
    """
    plan: list[dict] = []
    for run in db.query(
        "SELECT * FROM runs WHERE state NOT IN "
        "('run_completed','run_defeated','run_abandoned','run_expired',"
        "'admin_terminated','recovery_required') ORDER BY run_id",
    ):
        if is_expired(db, balance, run):
            plan.append({"run_id": run["run_id"], "action": "settle_expired"})
            continue

        state = run["state"]
        if state == PREPARING:
            # Verify the surface binding; absent → retry create at the SAME
            # surface_generation (idempotent). Exhausted → recovery_required.
            action = "resume_map" if run["thread_id"] else "retry_surface"
        elif state in (BATTLE, BOSS_BATTLE):
            # battle rows are authoritative; enemy_plans are REUSED, never
            # re-rolled.
            action = "rerender_battle"
        elif state in (REWARD_SELECTION, EVENT_CHOICE, SHOP):
            action = "rerender_pending_choice"
        elif state == NODE_RESOLUTION:
            # the rng journal reproduces the original roll
            action = "rerun_node_resolution"
        elif state == RUN_SETTLEMENT:
            action = "resume_settlement"
        else:
            action = "rerender_current"
        plan.append({"run_id": run["run_id"], "action": action, "state": state})
    return plan


def handle_thread_deleted(db: Database, logical_session_id: str) -> dict:
    """§16.8 — the run is NOT terminated and is NOT a defeat.

    It stays in its current state until the inactivity window elapses. If the
    player re-issues `!덱아웃` inside that window, the thread is recreated at
    surface_generation + 1 and the current screen re-rendered.
    """
    run = db.one("SELECT * FROM runs WHERE logical_session_id = ?",
                 (logical_session_id,))
    if run is None:
        return {"handled": False, "reason": "unknown logical_session_id"}
    db.execute(
        "UPDATE runs SET thread_id = NULL, canonical_message_id = NULL, "
        "updated_at = ? WHERE run_id = ?", (utcnow(), run["run_id"]),
    )
    return {"handled": True, "run_id": run["run_id"], "state": run["state"],
            "awaiting_recreate": True}


def recreate_surface(db: Database, run_id: int) -> int:
    """Bump `surface_generation`. It increments ONLY here, never reused or
    decremented (§1.3.5)."""
    with db.tx() as conn:
        conn.execute(
            "UPDATE runs SET surface_generation = surface_generation + 1, "
            "updated_at = ? WHERE run_id = ?", (utcnow(), run_id),
        )
        row = conn.execute("SELECT surface_generation FROM runs WHERE run_id = ?",
                           (run_id,)).fetchone()
    return int(row["surface_generation"])
