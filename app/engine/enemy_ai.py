"""§2.8.5–2.8.6 — enemy action selection, cooldowns, and telegraph commitment.

Two rules here are easy to get subtly wrong and are spelled out in the doc
precisely because v6.1/v6.2 got them wrong:

1. **Cooldown starts on EXECUTION, not on planning**, and
   `available_from_round = current_round + cooldown_turns + 1` — so
   `cooldown_turns = 1` genuinely skips a turn (C-02). A plan cancelled by
   기절, invalidated by a phase transition, or fizzled for lack of a target
   consumes **no** cooldown; otherwise a player could permanently suppress a
   boss's signature move by stunning it on the turn it was announced.

2. **"Rebuild telegraphs" means exactly**: for each living enemy WITHOUT a
   plan → create one. It never re-rolls a committed plan, so RNG consumption
   is bounded to one draw per enemy per turn cycle (M-03).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.db.connection import Database
from app.engine import statuses as st
from app.engine import targeting as tg
from app.engine import units as un
from app.engine.units import Unit

TELEGRAPH_ACTED = "행동 완료"   # §2.8.6, C-03


@dataclass(frozen=True)
class EnemyAction:
    action_id: str
    name: str
    category: str          # 공격 | 방어 | 버프디버프 | 회복
    target_side: str
    is_basic_attack: bool
    effects: list[dict]

    @property
    def is_blocked_by_silence(self) -> bool:
        """침묵 blocks 버프/디버프/회복; 공격 stays usable (§2.5.1).

        방어 is deliberately NOT blocked — the status table names exactly three
        categories, and `BattleEngine.playable_cards` filters the player side
        by the same two, so blocking it here would silently give 침묵 a
        different meaning against enemies.
        """
        return self.category in ("버프디버프", "회복")


@dataclass(frozen=True)
class ActionRule:
    priority: int
    condition: dict | None
    action_id: str
    weight: float
    cooldown_turns: int
    min_phase: int | None
    max_phase: int | None


@dataclass
class Plan:
    enemy_unit_id: int
    action_id: str
    target_ids: list[int]
    strategy_id: str | None
    content_version_id: int


class ActionRegistry:
    def __init__(self, db: Database, content_version_id: int):
        self.db = db
        self.content_version_id = content_version_id
        self._cache: dict[str, EnemyAction] = {}

    def get(self, action_id: str) -> EnemyAction:
        if action_id not in self._cache:
            row = self.db.one(
                "SELECT * FROM enemy_actions WHERE content_version_id = ? AND action_id = ?",
                (self.content_version_id, action_id),
            )
            if row is None:
                raise KeyError(
                    f"enemy action {action_id!r} is not defined at content version "
                    f"{self.content_version_id}"
                )
            self._cache[action_id] = EnemyAction(
                action_id=row["action_id"],
                name=row["name"],
                category=row["category"],
                target_side=row["target_side"],
                is_basic_attack=bool(row["is_basic_attack"]),
                effects=json.loads(row["effects_json"]),
            )
        return self._cache[action_id]

    def rules_for(self, enemy_def_id: str) -> list[ActionRule]:
        row = self.db.one(
            "SELECT action_rules_json FROM enemies "
            "WHERE content_version_id = ? AND enemy_id = ?",
            (self.content_version_id, enemy_def_id),
        )
        if row is None:
            raise KeyError(f"enemy {enemy_def_id!r} is not defined")
        rules = []
        for entry in json.loads(row["action_rules_json"]):
            rules.append(ActionRule(
                priority=int(entry.get("priority", 100)),
                condition=entry.get("condition"),
                action_id=entry["action_id"],
                weight=float(entry.get("weight", 1.0)),
                cooldown_turns=int(entry.get("cooldown_turns", 0)),
                min_phase=entry.get("min_phase"),
                max_phase=entry.get("max_phase"),
            ))
        return rules

    def strategy_for(self, enemy_def_id: str) -> str | None:
        row = self.db.one(
            "SELECT strategy_override FROM enemies "
            "WHERE content_version_id = ? AND enemy_id = ?",
            (self.content_version_id, enemy_def_id),
        )
        return row["strategy_override"] if row else None


# =====================================================================
# §2.8.5 cooldowns
# =====================================================================
def is_on_cooldown(db: Database, battle_id: int, enemy_unit_id: int,
                   action_id: str, current_round: int) -> bool:
    row = db.one(
        "SELECT available_from_round FROM enemy_cooldowns "
        "WHERE battle_id = ? AND enemy_unit_id = ? AND action_id = ?",
        (battle_id, enemy_unit_id, action_id),
    )
    if row is None:
        return False
    return current_round < row["available_from_round"]


def start_cooldown(db: Database, battle_id: int, enemy_unit_id: int,
                   action_id: str, current_round: int, cooldown_turns: int) -> None:
    """Called ONLY after an action actually executed (§2.11 step 7).

    `cooldown_turns = N` means the action is unavailable for N of that enemy's
    subsequent turns, hence the `+ 1`:

        round 5 executes → available_from_round = 5 + 1 + 1 = 7
        round 6          → 6 >= 7 false, not eligible
        round 7          → 7 >= 7 true,  eligible again
    """
    if cooldown_turns <= 0:
        return
    db.execute(
        "INSERT INTO enemy_cooldowns (battle_id, enemy_unit_id, action_id, "
        "available_from_round) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(battle_id, enemy_unit_id, action_id) DO UPDATE SET "
        "available_from_round = excluded.available_from_round",
        (battle_id, enemy_unit_id, action_id, current_round + cooldown_turns + 1),
    )


def cooldown_turns_for(registry: ActionRegistry, enemy_def_id: str,
                       action_id: str) -> int:
    for rule in registry.rules_for(enemy_def_id):
        if rule.action_id == action_id:
            return rule.cooldown_turns
    return 0


# =====================================================================
# §10.4.5 condition operators
# =====================================================================
def evaluate_condition(db: Database, condition: dict | None, *, actor: Unit,
                       battle_id: int, round_no: int) -> bool:
    if condition is None:
        return False          # unconditional rules go to the weighted pool
    op = condition.get("op", "always")
    value = condition.get("value")

    if op == "always":
        return True
    if op == "self_hp_below":
        return actor.hp_percent < float(value)
    if op == "self_hp_above":
        return actor.hp_percent > float(value)
    if op == "any_ally_hp_below":
        allies = un.load_units(db, battle_id, side=actor.side, living_only=True)
        return any(unit.hp_percent < float(value) for unit in allies)
    if op == "own_side_count_below":
        allies = un.load_units(db, battle_id, side=actor.side, living_only=True)
        return len(allies) < int(value)
    if op == "opposing_side_count_below":
        foes = un.load_units(db, battle_id, side=un.opposite(actor.side), living_only=True)
        return len(foes) < int(value)
    if op == "self_has_status":
        return st.has_status(db, actor.battle_unit_id, str(value))
    if op == "target_has_status":
        foes = un.load_units(db, battle_id, side=un.opposite(actor.side), living_only=True)
        return any(st.has_status(db, unit.battle_unit_id, str(value)) for unit in foes)
    if op == "round_number_gte":
        return round_no >= int(value)
    if op == "phase_is":
        return actor.boss_phase == int(value)
    if op == "owner_turn_index_mod":
        divisor = int(condition.get("divisor", 2))
        return round_no % divisor == int(value)

    raise ValueError(f"unsupported condition operator {op!r}")


# =====================================================================
# §2.8.5 action selection
# =====================================================================
def select_action(db: Database, registry: ActionRegistry, *, actor: Unit,
                  battle_id: int, round_no: int, rng, rng_key: str) -> EnemyAction:
    """Pick the action for one enemy at plan-build time.

    1. Filter: phase in range, cooldown NOT active, action legal
    2. First rule (ascending priority) whose condition is true wins, deterministically
    3. Otherwise a weighted pick among condition-less rules (journaled RNG)
    4. Otherwise 기본 공격 — every enemy must define one (§10.5 enforces this)
    """
    rules = registry.rules_for(actor.unit_def_id)
    phase = actor.boss_phase or 0
    silenced = st.has_status(db, actor.battle_unit_id, st.SILENCE)

    eligible: list[ActionRule] = []
    for rule in rules:
        if rule.min_phase is not None and phase < rule.min_phase:
            continue
        if rule.max_phase is not None and phase > rule.max_phase:
            continue
        if is_on_cooldown(db, battle_id, actor.battle_unit_id, rule.action_id, round_no):
            continue
        action = registry.get(rule.action_id)
        if silenced and action.is_blocked_by_silence:
            continue
        if not _has_any_target(db, battle_id, actor, action):
            continue
        eligible.append(rule)

    conditional = sorted(
        (rule for rule in eligible if rule.condition is not None),
        key=lambda rule: (rule.priority, rule.action_id),
    )
    for rule in conditional:
        if evaluate_condition(db, rule.condition, actor=actor,
                              battle_id=battle_id, round_no=round_no):
            return registry.get(rule.action_id)

    pool = [rule for rule in eligible if rule.condition is None]
    if pool:
        pool.sort(key=lambda rule: (rule.priority, rule.action_id))
        picked = rng.weighted_choice(
            rng_key, [rule.action_id for rule in pool], [rule.weight for rule in pool]
        )
        return registry.get(picked)

    return basic_attack(registry, actor.unit_def_id)


def basic_attack(registry: ActionRegistry, enemy_def_id: str) -> EnemyAction:
    for rule in registry.rules_for(enemy_def_id):
        action = registry.get(rule.action_id)
        if action.is_basic_attack:
            return action
    raise KeyError(
        f"enemy {enemy_def_id!r} defines no 기본 공격 — §10.5 must reject this content"
    )


def _has_any_target(db: Database, battle_id: int, actor: Unit,
                    action: EnemyAction) -> bool:
    return bool(tg.valid_targets(db, battle_id, actor, action.target_side))


# =====================================================================
# §2.8.6 telegraph commitment
# =====================================================================
def build_plans(db: Database, action_registry: ActionRegistry,
                strategy_registry: tg.StrategyRegistry,
                status_registry: st.StatusRegistry, *, battle_id: int,
                round_no: int, content_version_id: int, rng,
                presentation_revision: int | None = None) -> list[Plan]:
    """For each living enemy WITHOUT a plan → create one. Never re-rolls.

    An enemy that has already consumed its snapshot entry this round has no
    plan and shows `행동 완료` (§2.8.6, C-03) rather than a speculative
    next-round plan, which would leak information and consume journaled RNG out
    of order.
    """
    created: list[Plan] = []
    for enemy in un.load_units(db, battle_id, side=un.ENEMY, living_only=True):
        existing = db.one(
            "SELECT 1 FROM enemy_plans WHERE battle_id = ? AND enemy_unit_id = ?",
            (battle_id, enemy.battle_unit_id),
        )
        if existing is not None:
            continue
        plan = build_plan_for(
            db, action_registry, strategy_registry, status_registry,
            enemy=enemy, battle_id=battle_id, round_no=round_no,
            content_version_id=content_version_id, rng=rng,
            presentation_revision=presentation_revision,
        )
        if plan is not None:
            created.append(plan)
    return created


def build_plan_for(db: Database, action_registry: ActionRegistry,
                   strategy_registry: tg.StrategyRegistry,
                   status_registry: st.StatusRegistry, *, enemy: Unit,
                   battle_id: int, round_no: int, content_version_id: int, rng,
                   presentation_revision: int | None = None) -> Plan | None:
    from app.engine.rng import key_plan

    op_key = key_plan(battle_id, round_no, enemy.battle_unit_id)
    action = select_action(db, action_registry, actor=enemy, battle_id=battle_id,
                           round_no=round_no, rng=rng, rng_key=op_key)

    strategy_id = resolve_strategy_id(strategy_registry, action_registry, enemy)
    target_ids = _resolve_targets(
        db, strategy_registry, status_registry, action_registry,
        content_version_id, enemy=enemy, action=action, battle_id=battle_id,
        rng=rng, rng_key=f"{op_key}:target",
    )

    db.execute(
        "INSERT INTO enemy_plans (battle_id, enemy_unit_id, planned_action_id, "
        "planned_target_ids, planned_strategy_id, planned_at_content_version, "
        "planned_at_revision) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(battle_id, enemy_unit_id) DO UPDATE SET "
        "planned_action_id = excluded.planned_action_id, "
        "planned_target_ids = excluded.planned_target_ids, "
        "planned_strategy_id = excluded.planned_strategy_id, "
        "planned_at_content_version = excluded.planned_at_content_version, "
        "planned_at_revision = excluded.planned_at_revision",
        (battle_id, enemy.battle_unit_id, action.action_id, json.dumps(target_ids),
         strategy_id, content_version_id, presentation_revision),
    )
    return Plan(enemy.battle_unit_id, action.action_id, target_ids, strategy_id,
                content_version_id)


def _resolve_targets(db, strategy_registry, status_registry, action_registry,
                     content_version_id, *, enemy: Unit, action: EnemyAction,
                     battle_id: int, rng, rng_key: str) -> list[int]:
    candidates = tg.valid_targets(db, battle_id, enemy, action.target_side)
    if not candidates:
        return []
    if action.target_side == "all":
        # AoE is unaffected by 도발 (§2.5.1).
        return [unit.battle_unit_id for unit in candidates]
    if action.target_side == "self":
        return [enemy.battle_unit_id]

    strategy_id = resolve_strategy_id(strategy_registry, action_registry, enemy)
    chosen = tg.select_target(
        db, strategy_registry, status_registry, content_version_id,
        observer=enemy, candidates=candidates, strategy_id=strategy_id,
        rng=rng, rng_key=rng_key,
        apply_taunt=action.target_side == "enemy",
    )
    return [chosen.battle_unit_id] if chosen else []


def resolve_strategy_id(strategy_registry: tg.StrategyRegistry,
                        action_registry: ActionRegistry, enemy: Unit) -> str:
    """§2.8.1 layer 3 then layer 2: a per-enemy override wins over the role
    default. Plan building and re-targeting must agree, or an enemy would aim
    differently before and after its target dies."""
    return (action_registry.strategy_for(enemy.unit_def_id)
            or strategy_registry.role_default(enemy.role)[0])


def load_plan(db: Database, battle_id: int, enemy_unit_id: int) -> Plan | None:
    row = db.one(
        "SELECT * FROM enemy_plans WHERE battle_id = ? AND enemy_unit_id = ?",
        (battle_id, enemy_unit_id),
    )
    if row is None:
        return None
    return Plan(
        enemy_unit_id=row["enemy_unit_id"],
        action_id=row["planned_action_id"],
        target_ids=json.loads(row["planned_target_ids"]),
        strategy_id=row["planned_strategy_id"],
        content_version_id=row["planned_at_content_version"],
    )


def discard_plan(db: Database, battle_id: int, enemy_unit_id: int) -> None:
    """Removed on execute, on stun at its turn, on phase invalidation, on death."""
    db.execute(
        "DELETE FROM enemy_plans WHERE battle_id = ? AND enemy_unit_id = ?",
        (battle_id, enemy_unit_id),
    )


def telegraph_for(db: Database, action_registry: ActionRegistry, battle_id: int,
                  enemy_unit_id: int) -> dict[str, Any]:
    """What §11 renders in an enemy's telegraph panel."""
    plan = load_plan(db, battle_id, enemy_unit_id)
    if plan is None:
        return {"state": "acted", "label": TELEGRAPH_ACTED}
    action = action_registry.get(plan.action_id)
    return {
        "state": "planned",
        "action_id": action.action_id,
        "label": action.name,
        "category": action.category,
        "target_ids": plan.target_ids,
    }
