"""§2.11 per-turn processing order and §2.12 round boundary.

The turn machine branches PHASE D on the acting unit's side (B-01) — v6.2's
version had a player-deck-only PHASE C/D and could not run an enemy turn at
all. Everything before and after the branch is shared.

    PHASE A  VALIDATE       both sides
    PHASE B  TURN START     both sides — block reset, DoT, death check
    PHASE C  ACTION GATING  both sides — 기절, durations NOT yet decremented
    PHASE D-P  player action     draw / curse / present / resolve
    PHASE D-E  enemy action      load plan / re-validate / execute
    PHASE E  TURN END       both sides — countdown decrement, cleanup, cooldown
    PHASE F  POST-TURN      both sides — boss thresholds, consume entry, end check
    → §2.12 ROUND BOUNDARY
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.content.balance import Balance
from app.db.connection import Database
from app.engine import card_upgrades as cu
from app.engine import deck
from app.engine import effects as fx
from app.engine import enemy_ai as ai
from app.engine import statuses as st
from app.engine import stats
from app.engine import targeting as tg
from app.engine import timed_effects as te
from app.engine import units as un
from app.engine.rng import JournaledRng, key_draw
from app.engine.units import Unit

logger = logging.getLogger(__name__)

# battles.turn_phase (§18.4) — persisted at each sub-step so a restart resumes
# mid-selection rather than re-rendering a generic turn (M-05).
AWAIT_CARD = "await_card"
AWAIT_TARGET = "await_target"
RESOLVING = "resolving"
AWAIT_NESTED_CHOICE = "await_nested_choice"

BATTLE_ACTIVE = "active"
BATTLE_WON = "won"
BATTLE_LOST = "lost"


@dataclass
class TurnResult:
    unit_id: int
    side: str
    acted: bool = False
    reason: str | None = None
    damage_events: list[dict] = field(default_factory=list)
    deaths: list[int] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    awaiting_input: bool = False
    battle_ended: bool = False
    battle_state: str = BATTLE_ACTIVE


@dataclass
class BattleEngine:
    db: Database
    balance: Balance
    battle_id: int
    run_id: int
    content_version_id: int
    rng: JournaledRng
    status_registry: st.StatusRegistry
    strategy_registry: tg.StrategyRegistry
    action_registry: ai.ActionRegistry
    #: party_slot → {card_id: upgrade_tier}, read once per battle from the
    #: §16.2.3 build snapshot.
    _upgrade_cache: dict = field(default_factory=dict)

    # -- accessors -----------------------------------------------------
    def battle_row(self):
        row = self.db.one("SELECT * FROM battles WHERE battle_id = ?", (self.battle_id,))
        if row is None:
            raise KeyError(f"battle {self.battle_id} does not exist")
        return row

    @property
    def round_no(self) -> int:
        return int(self.battle_row()["round_no"])

    # =================================================================
    # §2.1.1 round order snapshot
    # =================================================================
    def build_round_order(self, round_no: int) -> list[int]:
        """Build and PERSIST an ordered snapshot of every living unit.

        Ordering is descending speed; allies before enemies on equal speed;
        same-side ties by registration_order — not by display slot, so summoned
        units order deterministically.
        """
        living = un.load_units(self.db, self.battle_id, living_only=True)
        ordered = sorted(
            living,
            key=lambda unit: (
                -un.effective_spd(self.db, self.status_registry, unit),
                0 if unit.side == un.ALLY else 1,
                unit.registration_order,
            ),
        )
        self.db.execute(
            "DELETE FROM battle_round_order WHERE battle_id = ? AND round_no = ?",
            (self.battle_id, round_no),
        )
        for index, unit in enumerate(ordered):
            self.db.execute(
                "INSERT INTO battle_round_order (battle_id, round_no, order_index, "
                "battle_unit_id, consumed) VALUES (?, ?, ?, ?, 0)",
                (self.battle_id, round_no, index, unit.battle_unit_id),
            )
        return [unit.battle_unit_id for unit in ordered]

    def current_entry(self):
        battle = self.battle_row()
        return self.db.one(
            "SELECT * FROM battle_round_order WHERE battle_id = ? AND round_no = ? "
            "AND order_index = ?",
            (self.battle_id, battle["round_no"], battle["turn_cursor"]),
        )

    def acting_unit(self) -> Unit | None:
        entry = self.current_entry()
        if entry is None:
            return None
        return un.load_unit(self.db, entry["battle_unit_id"])

    def _consume_entry(self) -> None:
        battle = self.battle_row()
        self.db.execute(
            "UPDATE battle_round_order SET consumed = 1 WHERE battle_id = ? "
            "AND round_no = ? AND order_index = ?",
            (self.battle_id, battle["round_no"], battle["turn_cursor"]),
        )

    # =================================================================
    # Battle start
    # =================================================================
    def start(self) -> None:
        """Round 1 setup: snapshot, resource, and the first telegraphs."""
        self.build_round_order(1)
        self._refill_resource()
        self._build_plans(1)
        self.db.execute(
            "UPDATE battles SET turn_cursor = 0, turn_phase = ? WHERE battle_id = ?",
            (AWAIT_CARD, self.battle_id),
        )

    def _refill_resource(self) -> None:
        """§2.3 — the pool refills at ROUND START, sized by party size (§15.1)."""
        size = self.db.one(
            "SELECT COUNT(*) AS n FROM battle_units WHERE battle_id = ? "
            "AND side = 'ally' AND is_alive = 1",
            (self.battle_id,),
        )
        pool = self.balance.get("resource_pool_by_party_size")
        value = pool.get(str(int(size["n"])), pool[str(max(pool, key=int))])
        self.db.execute(
            "UPDATE battles SET party_resource_current = ? WHERE battle_id = ?",
            (int(value), self.battle_id),
        )

    def _build_plans(self, round_no: int) -> None:
        ai.build_plans(
            self.db, self.action_registry, self.strategy_registry,
            self.status_registry, battle_id=self.battle_id, round_no=round_no,
            content_version_id=self.content_version_id, rng=self.rng,
        )

    # =================================================================
    # §2.11 PHASE A–C — shared prologue
    # =================================================================
    def begin_turn(self) -> TurnResult:
        """Run PHASE A through C and stop where the side branch begins.

        For a player unit this ends holding a drawn hand awaiting selection; for
        an enemy it falls straight through to `execute_enemy_turn`.
        """
        unit = self.acting_unit()
        if unit is None:
            return TurnResult(unit_id=-1, side="", reason="no pending turn")
        result = TurnResult(unit_id=unit.battle_unit_id, side=unit.side)

        # ━━ PHASE A — VALIDATE ━━
        battle = self.battle_row()
        if battle["state"] != BATTLE_ACTIVE or not unit.is_alive:
            # A unit that died mid-round has its entry skipped — still marked
            # consumed (§2.1.1 rule 4) — and the cursor must advance, or the
            # round never completes.
            result.reason = "unit or battle is not live"
            self._consume_entry()
            if battle["state"] == BATTLE_ACTIVE:
                self.round_boundary()
            result.battle_state = self.battle_row()["state"]
            result.battle_ended = result.battle_state != BATTLE_ACTIVE
            return result

        # ━━ PHASE B — TURN START ━━
        # 2. Reset the acting unit's block to 0 (Slay-the-Spire standard).
        un.set_block(self.db, unit.battle_unit_id, 0)
        unit.block = 0

        # 3. TURN_START_TRIGGER clock: 화상 / 출혈, ignoring block and 방어력,
        #    then decay their stacks.
        for status_id, damage in st.tick_turn_start_triggers(
            self.db, self.status_registry, unit.battle_unit_id
        ):
            if te.is_invulnerable(self.db, unit.battle_unit_id, battle["round_no"]):
                continue
            hp = un.apply_hp_loss(self.db, unit.battle_unit_id, damage)
            unit.hp_current = hp
            result.damage_events.append({
                "target_id": unit.battle_unit_id, "source": status_id,
                "final_damage": damage, "hp_loss": damage, "hp_after": hp,
            })

        # 4. ★ DEATH CHECK — a dead unit must never draw.
        if un.death_check(self.db, unit.battle_unit_id):
            result.deaths.append(unit.battle_unit_id)
            result.reason = "died to a turn-start trigger"
            return self._finish_turn(unit, result, executed_action=None)

        # ━━ PHASE C — ACTION GATING (durations NOT yet decremented) ━━
        # 5. 기절 active → no action; an ENEMY additionally DISCARDS its plan
        #    here, and NO cooldown is consumed (§2.8.5).
        if st.has_status(self.db, unit.battle_unit_id, st.STUN):
            if unit.side == un.ENEMY:
                ai.discard_plan(self.db, self.battle_id, unit.battle_unit_id)
            result.reason = "기절"
            return self._finish_turn(unit, result, executed_action=None)

        if unit.side == un.ENEMY:
            return self.execute_enemy_turn(unit, result)
        return self._begin_player_turn(unit, result)

    # =================================================================
    # §2.11 PHASE D-P — player unit action
    # =================================================================
    def _begin_player_turn(self, unit: Unit, result: TurnResult) -> TurnResult:
        battle = self.battle_row()
        party_slot = unit.party_slot
        if party_slot is None:
            result.reason = "ally unit has no party slot"
            return self._finish_turn(unit, result, executed_action=None)

        # Draw-pile exhaustion at the START of a turn resolves as auto-defend
        # + turn skip, then reshuffles (§2.2).
        if deck.pile_size(self.db, self.run_id, party_slot, deck.DRAW) == 0:
            self._auto_defend(unit)
            result.reason = "draw pile exhausted"
            deck.reshuffle(self.db, self.rng, self.run_id, party_slot,
                           self.battle_id, battle["round_no"])
            return self._finish_turn(unit, result, executed_action=None)

        # P1. Draw 3 (or fewer — partial draw plays normally).
        count = int(self.balance.get("draws_per_turn"))
        drawn = deck.draw_cards(self.db, self.run_id, party_slot, count)
        self.db.execute("DELETE FROM battle_draw WHERE battle_id = ? AND battle_unit_id = ?",
                        (self.battle_id, unit.battle_unit_id))
        for card in drawn:
            self.db.execute(
                "INSERT INTO battle_draw (battle_id, battle_unit_id, card_instance_id) "
                "VALUES (?, ?, ?)",
                (self.battle_id, unit.battle_unit_id, card["card_instance_id"]),
            )

        # P2. Detect cursed cards → apply penalties in ascending card-ID order.
        cursed = sorted((card for card in drawn if card["is_cursed"]),
                        key=lambda card: (card["card_id"], card["card_instance_id"]))
        if cursed:
            for card in cursed:
                self._resolve_cursed(unit, card, result)
            # P3. ★ DEATH CHECK — cursed penalties can kill.
            if un.death_check(self.db, unit.battle_unit_id):
                result.deaths.append(unit.battle_unit_id)
            # P4. Turn forfeited — but PHASE E and F still run, so durations,
            # boss phases and telegraphs update.
            result.reason = "저주받은 카드"
            return self._finish_turn(unit, result, executed_action=None)

        # P5. Present playable cards.
        playable = self.playable_cards(unit)
        if not playable:
            # P6. No legal play → auto-defend. The screen states the reason;
            # there is no manual skip button.
            self._auto_defend(unit)
            result.reason = self._no_play_reason(unit, drawn)
            return self._finish_turn(unit, result, executed_action=None)

        # P7. Await card selection, then target selection. turn_phase is
        # persisted at each sub-step (§18.4).
        self.db.execute(
            "UPDATE battles SET turn_phase = ?, acting_unit_id = ?, "
            "selected_card_instance_id = NULL, pending_target_side = NULL "
            "WHERE battle_id = ?",
            (AWAIT_CARD, unit.battle_unit_id, self.battle_id),
        )
        result.awaiting_input = True
        return result

    def playable_cards(self, unit: Unit) -> list[dict]:
        """Cards in hand this unit may legally play right now.

        Filters: 침묵 blocks 버프/디버프/회복; §2.10 filters by element;
        the party resource pool filters by cost.
        """
        battle = self.battle_row()
        resource = int(battle["party_resource_current"])
        silenced = st.has_status(self.db, unit.battle_unit_id, st.SILENCE)

        playable = []
        for row in self.db.query(
            "SELECT rdc.* FROM battle_draw bd JOIN run_deck_cards rdc "
            "ON rdc.card_instance_id = bd.card_instance_id "
            "WHERE bd.battle_id = ? AND bd.battle_unit_id = ? "
            "ORDER BY rdc.card_instance_id",
            (self.battle_id, unit.battle_unit_id),
        ):
            if row["is_cursed"]:
                continue
            card = self.card_def(row["card_id"], unit.party_slot)
            if card is None or card["cost"] > resource:
                continue
            if silenced and card["category"] in ("버프디버프", "회복"):
                continue
            if (card["element"] != stats.NEUTRAL_ELEMENT
                    and card["element"] != unit.element):
                continue
            playable.append({**dict(row), "card": dict(card)})
        return playable

    def _no_play_reason(self, unit: Unit, drawn: list[dict]) -> str:
        if st.has_status(self.db, unit.battle_unit_id, st.SILENCE):
            return "침묵 — 사용할 수 있는 카드가 없습니다"
        battle = self.battle_row()
        if battle["party_resource_current"] <= 0:
            return "자원 부족 — 사용할 수 있는 카드가 없습니다"
        return "사용할 수 있는 카드가 없습니다"

    def card_def(self, card_id: str, party_slot: int | None = None):
        """§5.8 — the card as this run sees it, upgrade overlays applied.

        The tier comes from `run_build_snapshot.card_upgrade_json` (§16.2.3), not
        from the live account row: upgrading mid-run must not change the numbers
        of a battle already in progress.
        """
        row = self.db.one(
            "SELECT * FROM cards WHERE content_version_id = ? AND card_id = ?",
            (self.content_version_id, card_id),
        )
        if row is None:
            return None
        return cu.effective_card(self.db, self.content_version_id, row,
                                 self._upgrade_tier(card_id, party_slot))

    def _upgrade_tier(self, card_id: str, party_slot: int | None) -> int:
        if party_slot is None:
            return 0
        cached = self._upgrade_cache.get(party_slot)
        if cached is None:
            row = self.db.one(
                "SELECT card_upgrade_json FROM run_build_snapshot WHERE run_id = ? "
                "AND party_slot = ?", (self.run_id, party_slot))
            cached = json.loads(row["card_upgrade_json"]) if row else {}
            self._upgrade_cache[party_slot] = cached
        return int(cached.get(card_id, 0))

    def _auto_defend(self, unit: Unit) -> None:
        block = stats.auto_defend_block(self.balance, un.effective_def(self.db, unit))
        un.set_block(self.db, unit.battle_unit_id, block)
        unit.block = block

    def _resolve_cursed(self, unit: Unit, card: dict, result: TurnResult) -> None:
        """Forced resolution on draw — the player does not select it.

        Not consumed on use: it goes to discard and returns on the next
        reshuffle (§2.7.1).
        """
        definition = self.db.one(
            "SELECT * FROM cursed_cards WHERE content_version_id = ? AND cursed_card_id = ?",
            (self.content_version_id, card["card_id"]),
        )
        if definition is None:
            result.log.append(f"cursed card {card['card_id']!r} is not defined")
            return
        ctx = self._context(unit, [unit], fx.ops.CTX_CURSED_CARD)
        outcome = fx.execute_effects(json.loads(definition["penalty_json"]), ctx)
        result.damage_events.extend(outcome.damage_events)
        result.log.extend(outcome.log)
        result.log.append(f"저주: {definition['name']}")

    # -- card play (P7 → P8) -------------------------------------------
    def select_card(self, unit: Unit, card_instance_id: int) -> dict:
        """First half of the two-step flow (§2.5.2). Returns the target set."""
        playable = {entry["card_instance_id"]: entry for entry in self.playable_cards(unit)}
        entry = playable.get(card_instance_id)
        if entry is None:
            raise ValueError("card is not playable right now")

        card = entry["card"]
        target_side = card["target_side"]
        self.db.execute(
            "UPDATE battles SET turn_phase = ?, selected_card_instance_id = ?, "
            "pending_target_side = ? WHERE battle_id = ?",
            (AWAIT_TARGET, card_instance_id, target_side, self.battle_id),
        )

        # AoE cards skip step 2.
        if target_side == "all":
            return {"card": card, "targets": [], "needs_target": False}
        candidates = tg.valid_targets(self.db, self.battle_id, unit, target_side)
        return {"card": card, "targets": candidates,
                "needs_target": len(candidates) > 1}

    def play_card(self, unit: Unit, card_instance_id: int,
                  target_ids: list[int]) -> TurnResult:
        """P8 — validate + consume party resource → resolve the operator list."""
        result = TurnResult(unit_id=unit.battle_unit_id, side=unit.side)
        playable = {entry["card_instance_id"]: entry for entry in self.playable_cards(unit)}
        entry = playable.get(card_instance_id)
        if entry is None:
            raise ValueError("card is not playable right now")
        card = entry["card"]

        targets = self._resolve_play_targets(unit, card, target_ids)
        if not targets and card["target_side"] != "self":
            raise ValueError("no legal target for this card")

        self.db.execute(
            "UPDATE battles SET party_resource_current = party_resource_current - ?, "
            "turn_phase = ? WHERE battle_id = ?",
            (int(card["cost"]), RESOLVING, self.battle_id),
        )

        ctx = self._context(unit, targets, fx.ops.CTX_BATTLE_CARD)
        # `effects` is the upgrade-resolved list (§5.8); `effects_json` on the
        # row is the un-upgraded original and must not be executed directly.
        outcome = fx.execute_effects(card["effects"], ctx)
        result.damage_events.extend(outcome.damage_events)
        result.log.extend(outcome.log)
        result.acted = True

        # P9. ★ DEATH CHECK for every affected unit.
        for target in targets:
            if un.death_check(self.db, target.battle_unit_id):
                result.deaths.append(target.battle_unit_id)

        return self._finish_turn(unit, result, executed_action=None)

    def _resolve_play_targets(self, unit: Unit, card: dict,
                              target_ids: list[int]) -> list[Unit]:
        side = card["target_side"]
        if side == "self":
            return [unit]
        candidates = tg.valid_targets(self.db, self.battle_id, unit, side)
        if side == "all":
            return candidates
        by_id = {candidate.battle_unit_id: candidate for candidate in candidates}
        chosen = [by_id[target_id] for target_id in target_ids if target_id in by_id]
        if not chosen and len(candidates) == 1:
            chosen = candidates
        # 도발 overrides player targeting too — it is a universal rule applied
        # to every hostile single-target action (§2.5.1).
        if chosen and side == "enemy":
            chosen = [tg.apply_taunt_override(
                self.db, self.status_registry, chosen[0], candidates)]
        return chosen

    # =================================================================
    # §2.11 PHASE D-E — enemy unit action
    # =================================================================
    def execute_enemy_turn(self, unit: Unit, result: TurnResult) -> TurnResult:
        battle = self.battle_row()
        round_no = int(battle["round_no"])

        # E1. Load the committed plan; a planless enemy (summoned this round, or
        # its plan was discarded) builds one NOW rather than idling a full round.
        plan = ai.load_plan(self.db, self.battle_id, unit.battle_unit_id)
        if plan is None:
            ai.build_plan_for(
                self.db, self.action_registry, self.strategy_registry,
                self.status_registry, enemy=unit, battle_id=self.battle_id,
                round_no=round_no, content_version_id=self.content_version_id,
                rng=self.rng,
            )
            plan = ai.load_plan(self.db, self.battle_id, unit.battle_unit_id)
        if plan is None:
            result.reason = "no action available"
            return self._finish_turn(unit, result, executed_action=None)

        # E2. Re-validate the plan against §2.8.6.
        action = self.action_registry.get(plan.action_id)
        if (st.has_status(self.db, unit.battle_unit_id, st.SILENCE)
                and action.is_blocked_by_silence):
            action = ai.basic_attack(self.action_registry, unit.unit_def_id)
            result.log.append("침묵 — 기본 공격으로 대체")

        targets = self._revalidate_targets(unit, action, plan)
        if not targets:
            # FIZZLE. Turn consumed, NO cooldown.
            result.reason = "대상 없음 — 불발"
            ai.discard_plan(self.db, self.battle_id, unit.battle_unit_id)
            return self._finish_turn(unit, result, executed_action=None)

        # E3. Resolve against the action definition at planned_at_content_version.
        ctx = self._context(unit, targets, fx.ops.CTX_ENEMY_ACTION)
        ctx.content_version_id = plan.content_version_id
        outcome = fx.execute_effects(action.effects, ctx)
        result.damage_events.extend(outcome.damage_events)
        result.log.extend(outcome.log)
        result.acted = True

        # E4. ★ DEATH CHECK for every affected unit.
        for target in targets:
            if un.death_check(self.db, target.battle_unit_id):
                result.deaths.append(target.battle_unit_id)

        # E5. Mark the plan EXECUTED — the cooldown starts at PHASE E step 7.
        return self._finish_turn(unit, result, executed_action=action.action_id)

    def _revalidate_targets(self, unit: Unit, action: ai.EnemyAction,
                            plan: ai.Plan) -> list[Unit]:
        candidates = tg.valid_targets(self.db, self.battle_id, unit, action.target_side)
        if not candidates:
            return []
        if action.target_side == "all":
            return candidates
        if action.target_side == "self":
            return [unit]

        by_id = {candidate.battle_unit_id: candidate for candidate in candidates}
        alive = [by_id[target_id] for target_id in plan.target_ids if target_id in by_id]

        # 도발 redirects hostile aggression only — a supporter's heal or buff on
        # its own side must not be captured by an enemy taunt.
        hostile = action.target_side == "enemy"

        if not alive:
            # Planned target died → EXECUTES, re-targeting via planned_strategy_id.
            chosen = tg.select_target(
                self.db, self.strategy_registry, self.status_registry,
                self.content_version_id, observer=unit, candidates=candidates,
                strategy_id=plan.strategy_id or ai.resolve_strategy_id(
                    self.strategy_registry, self.action_registry, unit),
                rng=self.rng,
                rng_key=f"battle:{self.battle_id}:retarget:{unit.battle_unit_id}",
                apply_taunt=hostile,
            )
            return [chosen] if chosen else []

        if not hostile:
            return [alive[0]]
        # 도발 applied since planning → EXECUTES, target becomes the taunting unit.
        return [tg.apply_taunt_override(self.db, self.status_registry, alive[0],
                                        candidates)]

    # =================================================================
    # §2.11 PHASE E–F — shared epilogue
    # =================================================================
    def _finish_turn(self, unit: Unit, result: TurnResult,
                     executed_action: str | None) -> TurnResult:
        battle = self.battle_row()
        round_no = int(battle["round_no"])

        # ━━ PHASE E — TURN END ━━
        # 6. Decrement every countdown / stack_duration status that was ACTIVE
        #    during this turn. stack_decay statuses already decayed at step 3.
        st.tick_owner_turn_countdown(self.db, self.status_registry, unit.battle_unit_id)

        # 7. PLAYER: discard this turn's draw. ENEMY: delete the plan; start the
        #    cooldown IF AND ONLY IF the action actually executed.
        if unit.side == un.ALLY:
            if unit.party_slot is not None:
                deck.discard_hand(self.db, self.run_id, unit.party_slot)
            self.db.execute(
                "DELETE FROM battle_draw WHERE battle_id = ? AND battle_unit_id = ?",
                (self.battle_id, unit.battle_unit_id),
            )
        else:
            ai.discard_plan(self.db, self.battle_id, unit.battle_unit_id)
            if executed_action is not None:
                ai.start_cooldown(
                    self.db, self.battle_id, unit.battle_unit_id, executed_action,
                    round_no,
                    ai.cooldown_turns_for(self.action_registry, unit.unit_def_id,
                                          executed_action),
                )

        # ━━ PHASE F — POST-TURN ━━
        # 8. For each SURVIVING boss: evaluate phase thresholds.
        for entry in self._evaluate_boss_phases(round_no):
            result.log.append(entry)

        # 9. Mark this snapshot entry consumed.
        self._consume_entry()

        # 10. Check battle-end conditions.
        state = self._battle_end_state()
        if state != BATTLE_ACTIVE:
            self._finalize(state)
            result.battle_ended = True
            result.battle_state = state
            return result

        # 11. → §2.12 ROUND BOUNDARY
        self.round_boundary()
        result.battle_state = self.battle_row()["state"]
        result.battle_ended = result.battle_state != BATTLE_ACTIVE
        return result

    # =================================================================
    # §2.8.4 boss phase transitions
    # =================================================================
    def _evaluate_boss_phases(self, round_no: int) -> list[str]:
        """Threshold crossing policy.

        1. Full damage has already landed and HP is clamped at 0
        2. A boss that is now dead simply dies — NO transition effect fires
        3. Identify every newly crossed threshold, descending
        4. Enter each newly crossed phase exactly once, effects in order
        5. 무적 applies only to damage AFTER the transition
        6. Discard and rebuild the plan for THAT UNIT ONLY
        """
        log: list[str] = []
        for unit in un.load_units(self.db, self.battle_id, side=un.ENEMY):
            if unit.boss_phase is None or not unit.is_alive:
                continue
            phases = self.db.query(
                "SELECT * FROM boss_phases WHERE content_version_id = ? AND enemy_id = ? "
                "ORDER BY phase_index",
                (self.content_version_id, unit.unit_def_id),
            )
            if not phases:
                continue
            hp_pct = unit.hp_percent
            target_phase = unit.boss_phase
            for phase in phases:
                if hp_pct <= float(phase["hp_threshold_pct"]):
                    target_phase = max(target_phase, int(phase["phase_index"]))

            if target_phase <= unit.boss_phase:
                continue

            for phase in phases:
                index = int(phase["phase_index"])
                if not (unit.boss_phase < index <= target_phase):
                    continue
                for effect_id in json.loads(phase["effect_ids_json"]):
                    self._fire_transition_effect(unit, effect_id, round_no)
                    log.append(f"boss phase {index}: {effect_id}")

            self.db.execute(
                "UPDATE battle_units SET boss_phase = ? WHERE battle_unit_id = ?",
                (target_phase, unit.battle_unit_id),
            )
            ai.discard_plan(self.db, self.battle_id, unit.battle_unit_id)
        return log

    def _fire_transition_effect(self, unit: Unit, effect_id: str, round_no: int) -> None:
        row = self.db.one(
            "SELECT * FROM transition_effects WHERE content_version_id = ? "
            "AND transition_effect_id = ?",
            (self.content_version_id, effect_id),
        )
        if row is None:
            return
        ctx = self._context(unit, [unit], fx.ops.CTX_TRANSITION_EFFECT)
        # Timed effects are created with applied_at_round = the current round
        # (§2.5.3), which is what makes a 1-round 무적 cover the next round.
        ctx.round_no = round_no
        fx.execute_effects(json.loads(row["effects_json"]), ctx)

    # =================================================================
    # §2.12 round boundary
    # =================================================================
    def round_boundary(self) -> str:
        """Runs immediately after §2.11 step 11.

        The ordering of ROUND_END steps 1-3 before 4-8 is load-bearing:
        round-scoped effects must expire and be able to end the battle BEFORE a
        new round is materialized.

        This does NOT increment presentation_revision (B-14) — the §16.7 CAS is
        the sole owner of that counter.
        """
        battle = self.battle_row()

        # Checked FIRST: a battle won by the final unit of a round must not
        # trigger a meaningless refill and next-round snapshot.
        state = self._battle_end_state()
        if state != BATTLE_ACTIVE:
            self._finalize(state)
            return state

        remaining = self.db.one(
            "SELECT MIN(order_index) AS next FROM battle_round_order "
            "WHERE battle_id = ? AND round_no = ? AND consumed = 0",
            (self.battle_id, battle["round_no"]),
        )
        if remaining and remaining["next"] is not None:
            self.db.execute(
                "UPDATE battles SET turn_cursor = ?, turn_phase = ? WHERE battle_id = ?",
                (int(remaining["next"]), AWAIT_CARD, self.battle_id),
            )
            return BATTLE_ACTIVE

        round_no = int(battle["round_no"])

        # 1. Expire every timed effect whose window has closed. An effect
        #    created THIS round has expires_after_round = R + D and SURVIVES.
        te.expire_round(self.db, self.battle_id, round_no)

        # 2. ★ DEATH CHECK
        for unit in un.load_units(self.db, self.battle_id, living_only=True):
            un.death_check(self.db, unit.battle_unit_id)

        # 3. Re-check battle-end; if ended, finalize and STOP.
        state = self._battle_end_state()
        if state != BATTLE_ACTIVE:
            self._finalize(state)
            return state

        # 4. round_no += 1
        next_round = round_no + 1
        # 5. Build and PERSIST the next snapshot. Speed changes accrued during
        #    the previous round take effect here; units summoned during the
        #    previous round are included now.
        self.db.execute("UPDATE battles SET round_no = ? WHERE battle_id = ?",
                        (next_round, self.battle_id))
        self.build_round_order(next_round)
        # 6. Refill and PERSIST party_resource_current.
        self._refill_resource()
        # 7. Create plans for living enemies that have none.
        self._build_plans(next_round)
        # 8. turn_cursor <- first entry of the new snapshot
        self.db.execute(
            "UPDATE battles SET turn_cursor = 0, turn_phase = ? WHERE battle_id = ?",
            (AWAIT_CARD, self.battle_id),
        )
        return BATTLE_ACTIVE

    # =================================================================
    # End conditions
    # =================================================================
    def _battle_end_state(self) -> str:
        allies = self.db.one(
            "SELECT COUNT(*) AS n FROM battle_units WHERE battle_id = ? "
            "AND side = 'ally' AND is_alive = 1", (self.battle_id,))
        enemies = self.db.one(
            "SELECT COUNT(*) AS n FROM battle_units WHERE battle_id = ? "
            "AND side = 'enemy' AND is_alive = 1", (self.battle_id,))
        if int(allies["n"]) == 0:
            return BATTLE_LOST
        if int(enemies["n"]) == 0:
            return BATTLE_WON
        return BATTLE_ACTIVE

    def _finalize(self, state: str) -> None:
        self.db.execute(
            "UPDATE battles SET state = ?, turn_phase = NULL WHERE battle_id = ?",
            (state, self.battle_id),
        )
        from app.engine.encounter import sync_party_hp_to_run

        sync_party_hp_to_run(self.db, self.battle_id, self.run_id)

    # =================================================================
    # Helpers
    # =================================================================
    def _context(self, actor: Unit, targets: list[Unit], host: str) -> fx.EffectContext:
        return fx.EffectContext(
            db=self.db,
            run_id=self.run_id,
            battle_id=self.battle_id,
            round_no=int(self.battle_row()["round_no"]),
            balance=self.balance,
            status_registry=self.status_registry,
            strategy_registry=self.strategy_registry,
            content_version_id=self.content_version_id,
            rng=self.rng,
            actor=actor,
            targets=targets,
            host_context=host,
        )

    def advance(self, max_turns: int | None = None) -> list[TurnResult]:
        """Run turns until the battle needs player input, or it ends.

        Enemy turns, stunned turns, cursed turns and auto-defends all resolve
        without a component interaction, so something has to drive them: the
        player only ever submits at PHASE D-P step P7. `max_turns` is a
        runaway guard, not a game rule — a well-formed battle always reaches
        either `awaiting_input` or a terminal state well inside it. The
        limit itself is `battle_max_turns_per_advance` in `config/01_전투.toml`.
        """
        if max_turns is None:
            max_turns = int(self.balance.get("battle_max_turns_per_advance"))
        results: list[TurnResult] = []
        for _ in range(max_turns):
            if self.battle_row()["state"] != BATTLE_ACTIVE:
                return results
            result = self.begin_turn()
            results.append(result)
            if result.awaiting_input or result.battle_ended:
                return results
            if result.unit_id == -1:
                return results
        logger.warning("battle %s hit the %d-turn advance guard", self.battle_id,
                       max_turns)
        return results

    def telegraphs(self) -> list[dict]:
        """§11 — every enemy's next action and target, before the player acts."""
        return [
            {
                "enemy_unit_id": unit.battle_unit_id,
                "visible_slot": unit.visible_slot,
                **ai.telegraph_for(self.db, self.action_registry, self.battle_id,
                                   unit.battle_unit_id),
            }
            for unit in un.load_units(self.db, self.battle_id, side=un.ENEMY,
                                      living_only=True)
        ]


def build_engine(db: Database, balance: Balance, *, battle_id: int, run_id: int,
                 content_version_id: int, rng: JournaledRng) -> BattleEngine:
    return BattleEngine(
        db=db, balance=balance, battle_id=battle_id, run_id=run_id,
        content_version_id=content_version_id, rng=rng,
        status_registry=st.StatusRegistry(db, content_version_id),
        strategy_registry=tg.StrategyRegistry(db, content_version_id),
        action_registry=ai.ActionRegistry(db, content_version_id),
    )
