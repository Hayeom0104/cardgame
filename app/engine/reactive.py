"""§2.13 — reactive abilities (반응형 능력 / 반격).

Authored content (dashboard, §10.1) attached to any unit definition —
character or enemy alike. Not a status (§2.5.1 untouched) and not a card.
Counter/반격 is the first use case; the mechanism is generic to any future
`trigger` beyond the one supported today.

Hooked into §2.11 P8/E3 from `app.engine.effects._land` — immediately after
each `deal_damage` / `deal_flat_damage` operator lands against one target.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.content import operators as ops
from app.db.connection import Database
from app.engine import enemy_ai as ai
from app.engine import units as un
from app.engine.rng import key_counter, next_journaled_seq
from app.engine.units import Unit

TRIGGER_ON_DAMAGE_TAKEN = "on_damage_taken"
TRIGGERS = frozenset({TRIGGER_ON_DAMAGE_TAKEN})

OWNER_CHARACTER = "character"
OWNER_ENEMY = "enemy"
OWNER_CONTENT_TYPES = frozenset({OWNER_CHARACTER, OWNER_ENEMY})


@dataclass(frozen=True)
class ReactiveAbility:
    reactive_ability_id: str
    name: str
    owner_content_type: str
    owner_id: str
    trigger_event: str
    condition_operators: list[dict]
    effects: list[dict]


def owner_content_type_for(side: str) -> str:
    return OWNER_CHARACTER if side == un.ALLY else OWNER_ENEMY


def load_for_owner(db: Database, content_version_id: int, owner_content_type: str,
                   owner_id: str) -> list[ReactiveAbility]:
    """Every non-retired ability this unit definition owns, in authored order."""
    rows = db.query(
        "SELECT * FROM reactive_abilities WHERE content_version_id = ? AND "
        "owner_content_type = ? AND owner_id = ? AND is_retired = 0 "
        "ORDER BY sort_order, reactive_ability_id",
        (content_version_id, owner_content_type, owner_id),
    )
    return [
        ReactiveAbility(
            reactive_ability_id=row["reactive_ability_id"],
            name=row["name"],
            owner_content_type=row["owner_content_type"],
            owner_id=row["owner_id"],
            trigger_event=row["trigger_event"],
            condition_operators=json.loads(row["condition_operators_json"]),
            effects=json.loads(row["effects_json"]),
        )
        for row in rows
    ]


def fire_on_damage_taken(ctx, outcome, *, owner: Unit, source: Unit) -> None:
    """§2.13 steps 2-4, called from `effects._land` after damage lands.

    `ctx`/`outcome` are the `EffectContext`/`EffectOutcome` of the operator
    that just dealt damage to `owner`; damage_events/heal_events/log/deaths
    raised by a firing ability are merged straight into that same `outcome`,
    so the caller's own §11 log builder picks them up with no special case.
    """
    # Step 2 — 사망 시 무효. `owner.hp_current` already reflects the final
    # HP `_land` just applied (same value §2.11's own death check uses).
    if owner.hp_current <= 0:
        return
    if ctx.battle_id is None:
        return

    depth_cap = int(ctx.balance.get("reactive_ability_max_depth"))
    if ctx.reactive_depth >= depth_cap:
        return

    abilities = load_for_owner(ctx.db, ctx.content_version_id,
                               owner_content_type_for(owner.side), owner.unit_def_id)
    for ability in abilities:
        if ability.trigger_event != TRIGGER_ON_DAMAGE_TAKEN:
            continue

        if not _conditions_pass(ctx, ability, owner=owner):
            continue

        from app.engine import effects as fx

        sub_ctx = fx.EffectContext(
            db=ctx.db, run_id=ctx.run_id, battle_id=ctx.battle_id,
            round_no=ctx.round_no, balance=ctx.balance,
            status_registry=ctx.status_registry,
            strategy_registry=ctx.strategy_registry,
            content_version_id=ctx.content_version_id, rng=ctx.rng,
            actor=owner, targets=[source],
            host_context=ops.CTX_REACTIVE_ABILITY,
            reactive_depth=ctx.reactive_depth + 1,
        )
        sub_outcome = fx.execute_effects(ability.effects, sub_ctx)
        outcome.damage_events.extend(sub_outcome.damage_events)
        outcome.heal_events.extend(sub_outcome.heal_events)
        outcome.log.extend(sub_outcome.log)
        outcome.log.append(f"반격: {ability.name}")

        # Step 3c — a counter can kill its target, subject to the normal
        # step-9/E4-style death check.
        if un.death_check(ctx.db, source.battle_unit_id):
            if source.battle_unit_id not in outcome.deaths:
                outcome.deaths.append(source.battle_unit_id)


def _conditions_pass(ctx, ability: ReactiveAbility, *, owner: Unit) -> bool:
    if not ability.condition_operators:
        return True
    for condition in ability.condition_operators:
        rng_key = None
        if condition.get("op") == "random_chance":
            # Lazily allocated only when a roll is actually needed — turns
            # are processed strictly sequentially within one battle (§16.7's
            # CAS is what makes that true), so counting existing journal rows
            # in this (battle, round) namespace gives a seq that stays unique
            # across every top-level turn in the round and every condition
            # entry, not just within this one operator's call tree.
            prefix = f"battle:{ctx.battle_id}:r{ctx.round_no}:counter:"
            rng_key = key_counter(ctx.battle_id, ctx.round_no,
                                  next_journaled_seq(ctx.db, prefix))
        if not ai.evaluate_condition(
            ctx.db, condition, actor=owner, battle_id=ctx.battle_id,
            round_no=ctx.round_no, rng=ctx.rng, rng_key=rng_key,
        ):
            return False
    return True
