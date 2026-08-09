"""§2.8.1–2.8.3 — targeting strategies, the threat model, and the 도발 override.

All strategies are **side-neutral** (§2.8.2): the side comes from the card's or
action's `target_side`, and a strategy only selects *within* the already
resolved side. A heal automatically looks at friendlies and an attack at
hostiles, for player and enemy units alike, with no per-strategy special casing.

도발 is a **universal override applied after strategy selection** (§2.5.1,
C-01) — not a strategy of its own. v6.2 had both, and they contradicted.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.connection import Database
from app.engine import statuses as st
from app.engine import units as un
from app.engine.units import Unit

# §10.4.4 selector operators.
LOWEST_HP_ABSOLUTE = "lowest_hp_absolute"
LOWEST_HP_PERCENT = "lowest_hp_percent"
HIGHEST_HP_ABSOLUTE = "highest_hp_absolute"
RANDOM_UNIFORM = "random_uniform"
HIGHEST_SCORE = "highest_score"
HAS_STATUS = "has_status"
LACKS_STATUS_CATEGORY = "lacks_status_category"
FIXED_SLOT = "fixed_slot"

# scoring expressions
THREAT_SCORE = "threat_score"
CURRENT_ATTACK = "current_attack"
MISSING_HP = "missing_hp"


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    name: str
    selector_operator: str
    valid_target_filter: str | None
    scoring_expression: str | None
    tie_breaker: str
    fallback_strategy_id: str | None
    params: dict


class StrategyRegistry:
    def __init__(self, db: Database, content_version_id: int):
        self.db = db
        self.content_version_id = content_version_id
        self._cache: dict[str, Strategy] = {}

    def get(self, strategy_id: str) -> Strategy:
        if strategy_id not in self._cache:
            import json

            row = self.db.one(
                "SELECT * FROM targeting_strategies "
                "WHERE content_version_id = ? AND strategy_id = ?",
                (self.content_version_id, strategy_id),
            )
            if row is None:
                raise KeyError(
                    f"targeting strategy {strategy_id!r} is not defined at content "
                    f"version {self.content_version_id}"
                )
            self._cache[strategy_id] = Strategy(
                strategy_id=row["strategy_id"],
                name=row["name"],
                selector_operator=row["selector_operator"],
                valid_target_filter=row["valid_target_filter"],
                scoring_expression=row["scoring_expression"],
                tie_breaker=row["tie_breaker"],
                fallback_strategy_id=row["fallback_strategy_id"],
                params=json.loads(row["params_json"]) if row["params_json"] else {},
            )
        return self._cache[strategy_id]

    def role_default(self, enemy_role_id: str) -> tuple[str, str | None]:
        row = self.db.one(
            "SELECT default_strategy_id, fallback_strategy_id FROM enemy_roles "
            "WHERE content_version_id = ? AND enemy_role_id = ?",
            (self.content_version_id, enemy_role_id),
        )
        if row is None:
            raise KeyError(f"enemy role {enemy_role_id!r} is not defined")
        return row["default_strategy_id"], row["fallback_strategy_id"]


# =====================================================================
# §2.8.2 target-side resolution
# =====================================================================
def resolve_target_side(actor_side: str, target_side: str) -> str | None:
    """Map a card's `target_side` (relative to its user) onto a battle side.

    Returns None for `all`, which is not a single side — AoE handling is the
    caller's job and skips selection entirely (§2.5.2).
    """
    if target_side == "enemy":
        return un.opposite(actor_side)
    if target_side in ("ally", "self"):
        return actor_side
    return None


def valid_targets(db: Database, battle_id: int, actor: Unit,
                  target_side: str) -> list[Unit]:
    """Living units on the resolved side, in registration order."""
    if target_side == "self":
        return [actor] if actor.is_alive else []
    side = resolve_target_side(actor.side, target_side)
    if side is None:      # `all` — every living unit in the battle
        return un.load_units(db, battle_id, living_only=True)
    return un.load_units(db, battle_id, side=side, living_only=True)


# =====================================================================
# §2.8.2 threat model
# =====================================================================
def threat_score(db: Database, content_version_id: int, observer: Unit,
                 target: Unit) -> float:
    """`role_weight[observer_role][target_job_role] × target_current_공격력`.

    Multiplying by the **live** stat makes threat responsive to buffs and gear.
    """
    row = db.one(
        "SELECT weight FROM threat_weights WHERE content_version_id = ? "
        "AND enemy_role_id = ? AND ally_role_id = ?",
        (content_version_id, observer.role, target.role),
    )
    if row is None:
        # §10.5 rejects a partially populated grid, so this is a content bug
        # rather than a runtime condition to paper over.
        raise KeyError(
            f"threat weight missing for ({observer.role!r}, {target.role!r}) at "
            f"content version {content_version_id}"
        )
    return float(row["weight"]) * un.effective_atk(db, target)


def _score(db: Database, content_version_id: int, expression: str,
           observer: Unit, target: Unit) -> float:
    if expression == THREAT_SCORE:
        return threat_score(db, content_version_id, observer, target)
    if expression == CURRENT_ATTACK:
        return un.effective_atk(db, target)
    if expression == MISSING_HP:
        return float(target.hp_max - target.hp_current)
    raise ValueError(f"unsupported scoring expression {expression!r}")


# =====================================================================
# Selection
# =====================================================================
def select_target(
    db: Database,
    registry: StrategyRegistry,
    status_registry: st.StatusRegistry,
    content_version_id: int,
    *,
    observer: Unit,
    candidates: list[Unit],
    strategy_id: str,
    rng=None,
    rng_key: str | None = None,
    apply_taunt: bool = True,
) -> Unit | None:
    """Run one strategy, then apply the §2.5.1 도발 override.

        resolved_target = strategy.select(valid_targets)
        if any unit on that side carries 도발:
            resolved_target = that unit          # override, unconditional

    Ties among multiple taunting units break by registration_order, as do ties
    inside every selector.

    `apply_taunt` must be False for anything that is not a **hostile**
    single-target action: 도발 redirects incoming aggression, so letting it
    capture a supporter's heal or buff would invert the mechanic. AoE actions
    never reach here at all.
    """
    if not candidates:
        return None

    strategy = registry.get(strategy_id)
    chosen = _apply_selector(
        db, registry, status_registry, content_version_id,
        observer=observer, candidates=candidates, strategy=strategy,
        rng=rng, rng_key=rng_key,
    )

    if chosen is None and strategy.fallback_strategy_id:
        return select_target(
            db, registry, status_registry, content_version_id,
            observer=observer, candidates=candidates,
            strategy_id=strategy.fallback_strategy_id,
            rng=rng, rng_key=rng_key, apply_taunt=apply_taunt,
        )

    if chosen is None:
        return None
    if not apply_taunt:
        return chosen
    return apply_taunt_override(db, status_registry, chosen, candidates)


def apply_taunt_override(db: Database, status_registry: st.StatusRegistry,
                         chosen: Unit, candidates: list[Unit]) -> Unit:
    """§2.5.1 — unconditional, applied to every hostile single-target action.

    AoE actions never reach here; they have no single resolved target.
    """
    taunting = [
        unit for unit in candidates
        if unit.is_alive and st.has_status(db, unit.battle_unit_id, st.TAUNT)
    ]
    if not taunting:
        return chosen
    taunting.sort(key=lambda unit: unit.registration_order)
    return taunting[0]


def _apply_selector(db, registry, status_registry, content_version_id, *,
                    observer: Unit, candidates: list[Unit], strategy: Strategy,
                    rng, rng_key) -> Unit | None:
    op = strategy.selector_operator
    pool = [unit for unit in candidates if unit.is_alive]
    if not pool:
        return None

    # Tie-break is ALWAYS registration_order (§2.8.1), so every min/max below
    # sorts on a (metric, registration_order) pair.
    if op == LOWEST_HP_ABSOLUTE:
        return min(pool, key=lambda u: (u.hp_current, u.registration_order))
    if op == HIGHEST_HP_ABSOLUTE:
        return min(pool, key=lambda u: (-u.hp_current, u.registration_order))
    if op == LOWEST_HP_PERCENT:
        return min(pool, key=lambda u: (u.hp_percent, u.registration_order))
    if op == RANDOM_UNIFORM:
        if rng is None or rng_key is None:
            # Deterministic fallback keeps the engine usable outside a run
            # (content previews, validation) without consuming journal keys.
            return pool[0]
        ordered = sorted(pool, key=lambda u: u.registration_order)
        index = rng.randint(rng_key, 0, len(ordered) - 1)
        return ordered[index]
    if op == HIGHEST_SCORE:
        if not strategy.scoring_expression:
            raise ValueError(
                f"strategy {strategy.strategy_id!r} uses highest_score without a "
                "scoring_expression"
            )
        return min(pool, key=lambda u: (
            -_score(db, content_version_id, strategy.scoring_expression, observer, u),
            u.registration_order,
        ))
    if op == HAS_STATUS:
        # #5 디버프 보유 우선 — any target carrying a debuff, else fall back.
        wanted = strategy.params.get("status_id")
        category = strategy.params.get("category", "debuff")
        matches = [
            unit for unit in pool
            if _carries(db, status_registry, unit, wanted, category)
        ]
        if not matches:
            return None
        return min(matches, key=lambda u: u.registration_order)
    if op == LACKS_STATUS_CATEGORY:
        # #6 버프 없는 대상 우선.
        category = strategy.params.get("category", "buff")
        matches = [
            unit for unit in pool
            if not _carries(db, status_registry, unit, None, category)
        ]
        if not matches:
            return None
        return min(matches, key=lambda u: u.registration_order)
    if op == FIXED_SLOT:
        slot = strategy.params.get("visible_slot", 0)
        matches = [unit for unit in pool if unit.visible_slot == slot]
        if not matches:
            return None
        return matches[0]

    raise ValueError(f"unsupported selector operator {op!r}")


def _carries(db: Database, status_registry: st.StatusRegistry, unit: Unit,
             status_id: str | None, category: str | None) -> bool:
    if status_id:
        return st.has_status(db, unit.battle_unit_id, status_id)
    for entry in st.active_statuses(db, unit.battle_unit_id):
        if status_registry.get(entry["status_id"]).kind == category:
            return True
    return False
