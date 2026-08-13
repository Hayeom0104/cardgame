"""§10.4.2 — operator execution and suspension.

A contiguous run of PURE_SYNCHRONOUS operators commits as one local
transaction, with the death check after the run, so a multi-operator card
resolves atomically to the player.

On reaching a PENDING_CHOICE or CENTRAL_TRANSACTION operator the executor
persists the remaining list plus a cursor and stops; the caller renders the
prompt (or begins the transaction) and resumes at the cursor on submission.
A run recovered from restart resumes from the same cursor (§16.7).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from app.content import operators as ops
from app.db.connection import Database, utcnow
from app.engine import deck
from app.engine import statuses as st
from app.engine import stats
from app.engine import targeting as tg
from app.engine import timed_effects as te
from app.engine import units as un
from app.engine.units import Unit


@dataclass
class EffectContext:
    """Everything an operator may touch while resolving one effect list."""

    db: Database
    run_id: int
    battle_id: int | None
    round_no: int
    balance: object
    status_registry: st.StatusRegistry
    strategy_registry: tg.StrategyRegistry | None
    content_version_id: int
    rng: object
    actor: Unit | None = None
    targets: list[Unit] = field(default_factory=list)
    host_context: str = ops.CTX_BATTLE_CARD
    card_multiplier_source: str = "card"
    #: counters for op_key derivation within one effect list
    sequence: int = 0


@dataclass
class EffectOutcome:
    suspended: bool = False
    suspend_operator: str | None = None
    suspend_cursor: int = 0
    remaining: list[dict] = field(default_factory=list)
    choice_id: str | None = None
    terminal_transition: dict | None = None
    damage_events: list[dict] = field(default_factory=list)
    deaths: list[int] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def execute_effects(effects: list[dict], ctx: EffectContext) -> EffectOutcome:
    """Run an ordered operator list, stopping at the first suspending operator."""
    outcome = EffectOutcome()

    for cursor, entry in enumerate(effects):
        operator = entry["operator"]
        params = dict(entry.get("params") or {})
        category = ops.categorize(operator, params)

        if category in (ops.PENDING_CHOICE, ops.CENTRAL_TRANSACTION):
            outcome.suspended = True
            outcome.suspend_operator = operator
            outcome.suspend_cursor = cursor
            outcome.remaining = effects[cursor:]
            outcome.choice_id = _persist_suspension(ctx, operator, params, effects, cursor)
            return outcome

        if category == ops.TERMINAL_STATE_TRANSITION:
            # §10.4.1a guarantees this is the last operator in the list.
            outcome.terminal_transition = {"operator": operator, "params": params}
            return outcome

        _apply_pure(operator, params, ctx, outcome)
        ctx.sequence += 1

    return outcome


def _persist_suspension(ctx: EffectContext, operator: str, params: dict,
                        effects: list[dict], cursor: int) -> str:
    choice_id = uuid.uuid4().hex
    ctx.db.execute(
        "INSERT INTO pending_choices (choice_id, run_id, node_index, choice_type, "
        "options_json, remaining_operators_json, operator_cursor, status, "
        "rng_op_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
        (choice_id, ctx.run_id, None, operator,
         json.dumps(params, ensure_ascii=False),
         json.dumps(effects[cursor:], ensure_ascii=False),
         cursor, None, utcnow()),
    )
    return choice_id


# =====================================================================
# PURE_SYNCHRONOUS operators
# =====================================================================
def _apply_pure(operator: str, params: dict, ctx: EffectContext,
                outcome: EffectOutcome) -> None:
    handler = _HANDLERS.get(operator)
    if handler is None:
        raise ops.ValidationError(
            f"operator {operator!r} has no runtime implementation — §10.4.6 step 1"
        )
    handler(params, ctx, outcome)


def _op_deal_damage(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    if ctx.actor is None:
        return
    attacker_atk = un.effective_atk(ctx.db, ctx.actor)
    attack_up = st.attack_up_bonus(ctx.db, ctx.status_registry, ctx.actor.battle_unit_id)

    for target in ctx.targets:
        if not target.is_alive:
            continue
        if te.is_invulnerable(ctx.db, target.battle_unit_id, ctx.round_no):
            outcome.log.append(f"unit {target.battle_unit_id} is 무적 — damage nullified")
            continue

        pierced = st.has_status(ctx.db, target.battle_unit_id, st.SHIELD_PIERCE)
        result = stats.compute_damage(
            ctx.balance,
            attacker_atk=attacker_atk,
            card_multiplier=float(params["multiplier"]),
            target_def=un.effective_def(ctx.db, target),
            attacker_element=ctx.actor.element,
            target_element=target.element,
            attack_up_bonus=attack_up,
            defense_down_bonus=st.defense_down_bonus(
                ctx.db, ctx.status_registry, target.battle_unit_id),
            target_block=target.block,
            ignores_block=bool(params.get("ignores_block")) or pierced,
            ignores_defense=bool(params.get("ignores_defense")),
        )
        _land(ctx, outcome, target, result)


def _op_deal_flat_damage(params: dict, ctx: EffectContext,
                         outcome: EffectOutcome) -> None:
    for target in ctx.targets:
        if not target.is_alive:
            continue
        if te.is_invulnerable(ctx.db, target.battle_unit_id, ctx.round_no):
            continue
        pierced = st.has_status(ctx.db, target.battle_unit_id, st.SHIELD_PIERCE)
        result = stats.compute_flat_damage(
            int(params["amount"]), target.block,
            ignores_block=bool(params.get("ignores_block")) or pierced,
        )
        _land(ctx, outcome, target, result)


def _land(ctx: EffectContext, outcome: EffectOutcome, target: Unit,
          result: stats.DamageResult) -> None:
    if result.block_consumed:
        un.consume_block(ctx.db, target.battle_unit_id, result.block_consumed)
    hp = un.apply_hp_loss(ctx.db, target.battle_unit_id, result.hp_loss)
    outcome.damage_events.append({
        "target_id": target.battle_unit_id,
        "final_damage": result.final_damage,
        "hp_loss": result.hp_loss,
        "hp_after": hp,
    })
    target.hp_current = hp
    target.block = max(0, target.block - result.block_consumed)


def _op_grant_block(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    for target in ctx.targets or ([ctx.actor] if ctx.actor else []):
        if target is None or not target.is_alive:
            continue
        amount = stats.block_from_defense(
            ctx.balance, un.effective_def(ctx.db, target),
            params["mode"], params["value"],
        )
        un.add_block(ctx.db, target.battle_unit_id, amount)
        target.block += amount


def _op_heal(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    for target in ctx.targets:
        if not target.is_alive:
            continue
        base = (float(params["value"]) if params["mode"] == "flat"
                else target.hp_max * float(params["value"]))
        modifier = st.heal_down_modifier(ctx.db, ctx.status_registry,
                                         target.battle_unit_id)
        healed = int(base * modifier)
        target.hp_current = un.heal(ctx.db, target.battle_unit_id, healed)


def _op_apply_status(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    for target in _status_targets(params, ctx):
        if not target.is_alive:
            continue
        st.apply_status(
            ctx.db, ctx.status_registry, target.battle_unit_id,
            params["status_id"], int(params.get("stacks", 1)),
            params.get("duration_override"),
        )


def _status_targets(params: dict, ctx: EffectContext) -> list[Unit]:
    """§5.8.3 — `apply_status`는 카드의 target_side와 독립적인 대상을 가질 수
    있다. 지정이 없으면 종래대로 카드가 이미 해결한 대상을 따른다."""
    target = params.get("target")
    if target is None:
        return ctx.targets
    if ctx.actor is None or ctx.battle_id is None:
        return ctx.targets

    if target == "self":
        return [ctx.actor]
    if target == "all_allies":
        return un.load_units(ctx.db, ctx.battle_id, side=ctx.actor.side,
                             living_only=True)
    if target == "all_enemies":
        return un.load_units(ctx.db, ctx.battle_id,
                             side=un.opposite(ctx.actor.side), living_only=True)
    if target == "designated_ally":
        slot = params.get("party_slot")
        allies = un.load_units(ctx.db, ctx.battle_id, side=ctx.actor.side,
                               living_only=True)
        if slot is None:
            return [ctx.actor]
        chosen = [unit for unit in allies if unit.party_slot == int(slot)]
        return chosen or [ctx.actor]
    # single_enemy — 카드가 이미 해결한 단일 대상.
    return ctx.targets[:1] if ctx.targets else []


def _op_remove_status(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    for target in ctx.targets:
        if params.get("status_id"):
            st.remove_status(ctx.db, target.battle_unit_id, params["status_id"])
        elif params.get("category"):
            st.remove_by_category(ctx.db, ctx.status_registry, target.battle_unit_id,
                                  params["category"], int(params.get("count", 1)))


def _op_modify_resource(params: dict, ctx: EffectContext,
                        outcome: EffectOutcome) -> None:
    if ctx.battle_id is None:
        return
    ctx.db.execute(
        "UPDATE battles SET party_resource_current = "
        "MAX(0, party_resource_current + ?) WHERE battle_id = ?",
        (int(params["delta"]), ctx.battle_id),
    )


def _op_draw_cards(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    if ctx.actor is None or ctx.actor.party_slot is None or ctx.battle_id is None:
        return
    drawn = deck.draw_cards(ctx.db, ctx.run_id, ctx.actor.party_slot,
                            int(params["count"]))
    for card in drawn:
        ctx.db.execute(
            "INSERT OR IGNORE INTO battle_draw (battle_id, battle_unit_id, "
            "card_instance_id) VALUES (?, ?, ?)",
            (ctx.battle_id, ctx.actor.battle_unit_id, card["card_instance_id"]),
        )


def _op_discard_cards(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    # Only selector=random reaches here; selector=choose suspends the list.
    if ctx.actor is None or ctx.actor.party_slot is None:
        return
    hand = ctx.db.query(
        "SELECT card_instance_id FROM run_deck_cards WHERE run_id = ? "
        "AND party_slot = ? AND pile = 'in_hand' ORDER BY card_instance_id",
        (ctx.run_id, ctx.actor.party_slot),
    )
    ids = [row["card_instance_id"] for row in hand]
    picked = ctx.rng.sample(
        f"discard:{ctx.battle_id}:{ctx.round_no}:{ctx.sequence}", ids,
        int(params["count"]),
    )
    for card_id in picked:
        ctx.db.execute(
            "UPDATE run_deck_cards SET pile = 'discard', pile_position = ? "
            "WHERE card_instance_id = ?",
            (-card_id, card_id),
        )
        # A discarded card must also leave this turn's draw set, or
        # `playable_cards` would still offer it.
        ctx.db.execute(
            "DELETE FROM battle_draw WHERE battle_id = ? AND card_instance_id = ?",
            (ctx.battle_id, card_id),
        )


def _op_insert_cursed_card(params: dict, ctx: EffectContext,
                           outcome: EffectOutcome) -> None:
    slots = [target.party_slot for target in ctx.targets if target.party_slot is not None]
    if not slots and ctx.actor and ctx.actor.party_slot is not None:
        slots = [ctx.actor.party_slot]
    actor_id = ctx.actor.battle_unit_id if ctx.actor else 0
    for slot in slots:
        # The sequence must be unique per insertion within the run, or the
        # §16.4 journal replays the first curse's position for every later one.
        deck.insert_cursed_card(
            ctx.db, ctx.rng, ctx.run_id, slot, params["cursed_card_id"],
            f"b{ctx.battle_id}r{ctx.round_no}u{actor_id}s{ctx.sequence}",
        )


def _op_modify_stat(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    if ctx.battle_id is None:
        return
    for target in ctx.targets:
        te.create_stat_modifier(
            ctx.db, ctx.battle_id, target.battle_unit_id,
            stat=params["stat"], delta=float(params["delta"]),
            is_percent=bool(params.get("is_percent")),
            current_round=ctx.round_no,
            duration_rounds=int(params["duration_rounds"]),
            source_ref=ctx.host_context,
        )


def _op_set_invulnerable(params: dict, ctx: EffectContext,
                         outcome: EffectOutcome) -> None:
    if ctx.battle_id is None:
        return
    for target in ctx.targets or ([ctx.actor] if ctx.actor else []):
        if target is None:
            continue
        te.create_invulnerable(
            ctx.db, ctx.battle_id, target.battle_unit_id,
            current_round=ctx.round_no,
            duration_rounds=int(params["duration_rounds"]),
            source_ref=ctx.host_context,
        )


def _op_add_card_to_run_deck(params: dict, ctx: EffectContext,
                             outcome: EffectOutcome) -> None:
    slot = ctx.actor.party_slot if ctx.actor else None
    if slot is None:
        return
    deck.add_card(ctx.db, ctx.run_id, slot, params["card_id"])


def _op_remove_card_from_run_deck(params: dict, ctx: EffectContext,
                                  outcome: EffectOutcome) -> None:
    slot = ctx.actor.party_slot if ctx.actor else None
    if slot is None:
        return
    rows = ctx.db.query(
        "SELECT card_instance_id FROM run_deck_cards WHERE run_id = ? "
        "AND party_slot = ? ORDER BY card_instance_id",
        (ctx.run_id, slot),
    )
    ids = [row["card_instance_id"] for row in rows]
    if not ids:
        return
    picked = ctx.rng.sample(f"deckremove:{ctx.run_id}:{ctx.sequence}", ids, 1)
    for card_id in picked:
        deck.remove_card(ctx.db, card_id)


def _op_modify_hp(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    for target in ctx.targets:
        delta = (float(params["delta"]) if params["mode"] == "flat"
                 else target.hp_max * float(params["delta"]))
        if delta >= 0:
            target.hp_current = un.heal(ctx.db, target.battle_unit_id, int(delta))
        else:
            # Cursed penalties can kill — no HP-1 floor (§2.7.1).
            target.hp_current = un.apply_hp_loss(ctx.db, target.battle_unit_id,
                                                 int(-delta))
            outcome.damage_events.append({
                "target_id": target.battle_unit_id,
                "final_damage": int(-delta),
                "hp_loss": int(-delta),
                "hp_after": target.hp_current,
            })


def _op_summon_enemy(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    """§2.6.1 — a summon that would exceed the cap FAILS as a logged no-op.

    It does NOT abort the remaining operators in its effect list (rule 5), and
    the summoned unit joins the NEXT round's snapshot (rule 6).
    """
    if ctx.battle_id is None:
        return
    from app.engine.encounter import summon_enemy

    cap = int(ctx.balance.get("max_enemies_per_encounter"))
    for _ in range(int(params["count"])):
        existing = ctx.db.one(
            "SELECT COUNT(*) AS n FROM battle_units WHERE battle_id = ? AND side = ?",
            (ctx.battle_id, un.ENEMY),
        )
        if int(existing["n"]) >= cap:
            outcome.log.append(
                f"summon of {params['enemy_id']!r} failed: enemy cap {cap} reached"
            )
            return
        summon_enemy(ctx.db, ctx.battle_id, params["enemy_id"],
                     ctx.content_version_id, ctx.balance)


def _op_grant_currency(params: dict, ctx: EffectContext, outcome: EffectOutcome) -> None:
    # Only non-coin currencies land here; coin is CENTRAL and suspends.
    currency = params["currency"]
    amount = int(params["amount"])
    if currency == "run_currency":
        ctx.db.execute(
            "UPDATE runs SET run_currency = run_currency + ? WHERE run_id = ?",
            (amount, ctx.run_id),
        )
        return
    row = ctx.db.one("SELECT user_id FROM runs WHERE run_id = ?", (ctx.run_id,))
    if row is None:
        return
    column = {"carta": "carta", "wildcard": "wildcards"}[currency]
    ctx.db.execute(
        f"UPDATE accounts SET {column} = {column} + ?, updated_at = ? WHERE user_id = ?",
        (amount, utcnow(), row["user_id"]),
    )


def _op_grant_character_fragments(params: dict, ctx: EffectContext,
                                  outcome: EffectOutcome) -> None:
    row = ctx.db.one("SELECT user_id FROM runs WHERE run_id = ?", (ctx.run_id,))
    if row is None:
        return
    ctx.db.execute(
        "INSERT INTO character_fragments (user_id, character_id, amount) "
        "VALUES (?, ?, ?) ON CONFLICT(user_id, character_id) DO UPDATE SET "
        "amount = amount + excluded.amount",
        (row["user_id"], params["character_id"], int(params["amount"])),
    )


def _op_grant_card_fragments(params: dict, ctx: EffectContext,
                             outcome: EffectOutcome) -> None:
    row = ctx.db.one("SELECT user_id FROM runs WHERE run_id = ?", (ctx.run_id,))
    if row is None:
        return
    ctx.db.execute(
        "INSERT INTO card_fragments (user_id, card_id, amount) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, card_id) DO UPDATE SET amount = amount + excluded.amount",
        (row["user_id"], params["card_id"], int(params["amount"])),
    )


def _op_grant_equipment(params: dict, ctx: EffectContext,
                        outcome: EffectOutcome) -> None:
    """§8.6.2 — drops go to the RUN INVENTORY, never straight to the account."""
    from app.engine.settlement import drop_tier_for

    run = ctx.db.one(
        "SELECT world_id, deepest_depth_reached, content_version_id FROM runs "
        "WHERE run_id = ?", (ctx.run_id,))
    if run is None:
        return
    tier = drop_tier_for(ctx.db, run["content_version_id"], run["world_id"],
                         run["deepest_depth_reached"])
    ctx.db.execute(
        "INSERT INTO run_inventory (run_id, kind, equipment_def_id, tier, amount, "
        "acquired_at_depth, acquired_at) VALUES (?, 'equipment', ?, ?, 1, ?, ?)",
        (ctx.run_id, params.get("equipment_id"), tier,
         run["deepest_depth_reached"], utcnow()),
    )


def _op_grant_enhancement_stone(params: dict, ctx: EffectContext,
                                outcome: EffectOutcome) -> None:
    run = ctx.db.one(
        "SELECT deepest_depth_reached FROM runs WHERE run_id = ?", (ctx.run_id,))
    if run is None:
        return
    ctx.db.execute(
        "INSERT INTO run_inventory (run_id, kind, stone_tier, tier, amount, "
        "acquired_at_depth, acquired_at) VALUES (?, 'stone', ?, ?, ?, ?, ?)",
        (ctx.run_id, int(params["tier"]), int(params["tier"]),
         int(params["amount"]), run["deepest_depth_reached"], utcnow()),
    )


def _op_grant_achievement_progress(params: dict, ctx: EffectContext,
                                   outcome: EffectOutcome) -> None:
    from app.engine.achievements import advance_by_id

    row = ctx.db.one("SELECT user_id FROM runs WHERE run_id = ?", (ctx.run_id,))
    if row is None:
        return
    advance_by_id(
        ctx.db, row["user_id"], params["achievement_id"], int(params["delta"]),
        mutation_id=f"run:{ctx.run_id}:ach:{ctx.sequence}:{params['achievement_id']}",
        content_version_id=ctx.content_version_id,
    )


_HANDLERS = {
    "deal_damage": _op_deal_damage,
    "deal_flat_damage": _op_deal_flat_damage,
    "grant_block": _op_grant_block,
    "heal": _op_heal,
    "apply_status": _op_apply_status,
    "remove_status": _op_remove_status,
    "modify_resource": _op_modify_resource,
    "draw_cards": _op_draw_cards,
    "discard_cards": _op_discard_cards,
    "insert_cursed_card": _op_insert_cursed_card,
    "modify_stat": _op_modify_stat,
    "set_invulnerable": _op_set_invulnerable,
    "add_card_to_run_deck": _op_add_card_to_run_deck,
    "remove_card_from_run_deck": _op_remove_card_from_run_deck,
    "modify_hp": _op_modify_hp,
    "summon_enemy": _op_summon_enemy,
    "grant_currency": _op_grant_currency,
    "grant_character_fragments": _op_grant_character_fragments,
    "grant_card_fragments": _op_grant_card_fragments,
    "grant_equipment": _op_grant_equipment,
    "grant_enhancement_stone": _op_grant_enhancement_stone,
    "grant_achievement_progress": _op_grant_achievement_progress,
}
