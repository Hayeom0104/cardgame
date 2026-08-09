"""§19.1 commands and §19.3 screen flow.

All in-run screens live in the run's private thread. Only the hub screen and
the run-end summary appear in the parent channel.

Every command and component interaction validates the current state first
(§16.2). An action illegal in the current state is rejected with an ephemeral
notice and a forced re-render, never silently ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.api import custom_id as cid
from app.api import errors
from app.api import events as ev
from app.api.gates import GateError, check_gates
from app.central import delivery
from app.content.balance import Balance
from app.content.seed import TUTORIAL_WORLD_ID, create_account
from app.db.connection import Database
from app.engine import achievements as ach
from app.engine import battle as bt
from app.engine import lifecycle as lc
from app.engine import map_gen
from app.engine import units as un
from app.engine.rng import JournaledRng

logger = logging.getLogger(__name__)

# §19.1 subcommands. 🟡 sub-names — the scheme is owner-confirmed, the
# individual words are flagged for review. `!카드` was deliberately avoided:
# the Central Bot already exposes a profile 카드 feature.
CMD_HUB = ""
CMD_START = "시작"
CMD_ABANDON = "포기"
CMD_DECK = "덱"
CMD_GACHA = "뽑기"
CMD_CHARACTERS = "캐릭터"
CMD_EQUIPMENT = "장비"
CMD_RESEARCH = "연구"
CMD_SHOP = "상점"
CMD_ACHIEVEMENTS = "업적"


@dataclass
class HandlerContext:
    db: Database
    balance: Balance | None
    central: Any
    content_version_id: int | None


def _ephemeral(message: str) -> dict:
    return {"action": "reply_ephemeral", "content": message}


def _reply(content: str, components: list | None = None) -> dict:
    return {"action": "reply", "content": content, "components": components or []}


# =====================================================================
# Message commands
# =====================================================================
def handle_message(ctx: HandlerContext, event: ev.MessageEvent) -> dict:
    if ctx.content_version_id is None:
        return _ephemeral("콘텐츠가 아직 준비되지 않았습니다.")

    subcommand = (event.args[0] if event.args else CMD_HUB)

    # §4.6.5 — an unknown user issuing !덱아웃 is created and routed into the
    # tutorial world. Idempotent against a retried first command.
    create_account(ctx.db, event.user_id, ctx.content_version_id)

    if subcommand == CMD_HUB:
        return hub_screen(ctx, event.user_id)
    if subcommand == CMD_START:
        return start_run(ctx, event.user_id)
    if subcommand == CMD_ABANDON:
        return abandon_run(ctx, event.user_id)
    if subcommand == CMD_ACHIEVEMENTS:
        return achievements_screen(ctx, event.user_id)
    if subcommand in (CMD_DECK, CMD_GACHA, CMD_CHARACTERS, CMD_EQUIPMENT,
                      CMD_RESEARCH, CMD_SHOP):
        return _reply(f"[{subcommand}] 화면은 아직 콘텐츠 작업 중입니다. (§13.2)")
    return _ephemeral(errors.ILLEGAL_STATE)


def hub_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃` → 허브 화면, a public reply in the Deckout channel.

    Re-issuing it while a run is active re-renders the current screen (§16.3);
    if the thread was deleted it is recreated at surface_generation + 1 (§16.8).
    """
    run = lc.active_run_for(ctx.db, user_id)
    if run is not None:
        if run["thread_id"] is None:
            generation = lc.recreate_surface(ctx.db, run["run_id"])
            return {"action": "reply_ephemeral",
                    "content": f"런 스레드를 다시 만듭니다. (generation {generation})"}
        # §16.3 second start attempt → redirect to the existing run's thread.
        return {"action": "redirect", "thread_id": run["thread_id"],
                "content": errors.RUN_ALREADY_ACTIVE}

    account = ctx.db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    lines = [
        "**덱아웃**",
        f"카르타 {account['carta']} · 와일드카드 {account['wildcards']}",
        f"파티 슬롯 {account['party_slots']} · 패시브 슬롯 {account['passive_slots']}",
    ]
    if account["tutorial_completed_at"] is None:
        lines.append("튜토리얼이 아직 남아 있습니다. `!덱아웃 시작`")
    return _reply("\n".join(lines))


def start_run(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 시작` → 준비 화면 (§16.2.2), ephemeral in the channel.

    NO run row and NO thread exist yet: steps 1-4 are pure UI, so abandoning
    them costs nothing and creates no `one_active_run` conflict.
    """
    if lc.active_run_for(ctx.db, user_id) is not None:
        return _ephemeral(errors.RUN_ALREADY_ACTIVE)

    account = ctx.db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    worlds = ctx.db.query(
        "SELECT wu.world_id, w.name FROM world_unlocks wu JOIN worlds w "
        "ON w.world_id = wu.world_id AND w.content_version_id = ? "
        "WHERE wu.user_id = ? ORDER BY w.sequence_index",
        (ctx.content_version_id, user_id),
    )
    if not worlds:
        return _ephemeral(errors.TUTORIAL_NOT_CLEARED)

    is_tutorial = account["tutorial_completed_at"] is None
    if is_tutorial:
        # The tutorial is the only content a new account can play: party size 1
        # exists only there (§3.4).
        return _prepare_and_materialize(ctx, user_id, TUTORIAL_WORLD_ID,
                                        is_tutorial=True)

    owned = ctx.db.query(
        "SELECT character_id FROM owned_characters WHERE user_id = ? "
        "ORDER BY acquired_at", (user_id,))
    if len(owned) < 2:
        return _ephemeral(errors.PARTY_TOO_SMALL)

    return {
        "action": "reply_ephemeral",
        "content": "준비 화면 — [1] 월드 선택 → [2] 파티 선택 → [3] 패시브 선택 → [4] 확정",
        "components": [
            {"type": "string_select", "custom_id": "dko:prep:world",
             "options": [{"label": row["name"], "value": row["world_id"]}
                         for row in worlds]},
        ],
    }


def _prepare_and_materialize(ctx: HandlerContext, user_id: int, world_id: str, *,
                             is_tutorial: bool,
                             party: list[str] | None = None) -> dict:
    """Step [5] MATERIALIZE, then [6] SURFACE."""
    from app.content.seed import STARTER_CHARACTER_ID

    request = lc.RunBuildRequest(
        user_id=user_id,
        world_id=world_id,
        party_character_ids=party or [STARTER_CHARACTER_ID],
        is_tutorial=is_tutorial,
    )
    try:
        run_id = lc.create_run(ctx.db, ctx.balance, request, ctx.content_version_id)
    except lc.LifecycleError as error:
        return _ephemeral(str(error))

    run = ctx.db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    request_id = delivery.mint_request_id("thread")
    delivery.record_intent(
        ctx.db, request_id=request_id, run_id=run_id, purpose="canonical",
        surface_generation=run["surface_generation"],
        presentation_revision=run["presentation_revision"],
    )
    # The private thread is created through the service-initiated API (§1.3.5),
    # not the response-path `create_thread` action, which would make a public
    # thread anyone with View Channel could watch.
    return {
        "action": "reply_ephemeral",
        "content": "런을 시작합니다.",
        "metadata": {"request_id": request_id},
        "thread_request": {
            "logical_session_id": run["logical_session_id"],
            "surface_generation": run["surface_generation"],
            "owner_user_id": user_id,
            "thread_name": f"덱아웃 - {user_id}",
        },
        "run_id": run_id,
    }


def abandon_run(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 포기` → run_settlement.

    포기 is treated identically to 패배 (§15.10): quitting right after a drop
    lands in the 0% retention band, so the farm-and-quit loop yields nothing.
    """
    from app.engine import settlement as sl

    run = lc.active_run_for(ctx.db, user_id)
    if run is None:
        return _ephemeral(errors.ILLEGAL_STATE)

    sl.enter_settlement(ctx.db, run["run_id"], target_state=sl.RUN_ABANDONED,
                        end_reason="포기 명령")
    rng = JournaledRng(ctx.db, run["run_id"], run["rng_seed"])
    report = sl.advance_settlement(
        ctx.db, ctx.balance, rng, run_id=run["run_id"],
        content_version_id=run["content_version_id"],
    )
    kept = len(report.get("inventory", {}).get("kept", []))
    lost = len(report.get("inventory", {}).get("lost", []))
    return _reply(f"런을 포기했습니다. 보관 {kept}개 · 소실 {lost}개")


def achievements_screen(ctx: HandlerContext, user_id: int) -> dict:
    """§20.5 — progress bars, hiding `is_hidden` entries until completed."""
    listing = ach.progress_list(ctx.db, user_id, ctx.content_version_id)
    if not listing:
        return _reply("업적이 없습니다.")
    lines = ["**업적**"]
    for entry in listing:
        mark = "✅" if entry["completed"] else "　"
        lines.append(
            f"{mark} {entry['name']} — {entry['current_value']}/{entry['target_value']}"
        )
    return _reply("\n".join(lines))


# =====================================================================
# Component interactions
# =====================================================================
def handle_interaction(ctx: HandlerContext, event: ev.InteractionEvent) -> dict:
    if event.custom_id.startswith("dko:prep:"):
        return _ephemeral("준비 화면 진행 중입니다.")

    try:
        parsed = cid.parse(event.custom_id)
    except cid.CustomIdError as error:
        logger.info("rejected custom_id: %s", error)
        return _ephemeral(errors.ILLEGAL_STATE)

    handler = _INTERACTION_HANDLERS.get(parsed.action)
    if handler is None:
        return _ephemeral(errors.ILLEGAL_STATE)

    try:
        return handler(ctx, event, parsed)
    except GateError as error:
        logger.info("gate rejected %s: %s", parsed.action, error.reason)
        return _ephemeral(error.message)
    except lc.StaleRevisionError:
        return _ephemeral(errors.STALE_REVISION)


def handle_modal_submit(ctx: HandlerContext, event: ev.ModalSubmitEvent) -> dict:
    return _ephemeral(errors.ILLEGAL_STATE)


def _on_node_choose(ctx: HandlerContext, event: ev.InteractionEvent,
                    parsed: cid.CustomId) -> dict:
    """맵 화면 → node buttons (branch width 2-3)."""
    gate = check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                       allowed_states={lc.MAP_NAVIGATION})
    node_index = int(parsed.payload)

    options = map_gen.available_next_nodes(ctx.db, parsed.run_id,
                                           gate.run["current_node_index"])
    if node_index not in {node["node_index"] for node in options}:
        raise GateError(errors.ILLEGAL_STATE, reason="node is not adjacent")

    node = ctx.db.one(
        "SELECT * FROM run_nodes WHERE run_id = ? AND node_index = ?",
        (parsed.run_id, node_index))

    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        ctx.db.execute(
            "UPDATE runs SET current_node_index = ?, state = ?, "
            "deepest_depth_reached = MAX(deepest_depth_reached, ?) WHERE run_id = ?",
            (node_index, lc.NODE_RESOLUTION, node["depth"], parsed.run_id),
        )
    return _reply(f"{node['node_type']} 노드에 진입합니다.")


def _on_card_select(ctx: HandlerContext, event: ev.InteractionEvent,
                    parsed: cid.CustomId) -> dict:
    """전투 화면 → card String Select, then the target select round-trip."""
    gate = check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                       allowed_states={lc.BATTLE, lc.BOSS_BATTLE})
    engine = _engine_for(ctx, gate.run)
    unit = engine.acting_unit()
    if unit is None or unit.side != un.ALLY:
        raise GateError(errors.ILLEGAL_STATE, reason="not a player turn")

    card_instance_id = int(event.values[0] if event.values else parsed.payload)
    try:
        selection = engine.select_card(unit, card_instance_id)
    except ValueError as error:
        raise GateError(errors.ILLEGAL_STATE, reason=str(error)) from error

    if not selection["needs_target"]:
        return _resolve_card(ctx, gate.run, engine, unit, card_instance_id,
                            [t.battle_unit_id for t in selection["targets"]],
                            parsed)
    return {
        "action": "edit",
        "content": "대상을 선택하세요.",
        "components": [{
            "type": "string_select",
            "custom_id": cid.build(cid.ACTION_TARGET_SELECT, parsed.run_id,
                                   parsed.generation, parsed.revision,
                                   str(card_instance_id)),
            "options": [
                {"label": f"슬롯 {target.visible_slot} "
                          f"({target.hp_current}/{target.hp_max})",
                 "value": str(target.battle_unit_id)}
                for target in selection["targets"]
            ],
        }],
    }


def _on_target_select(ctx: HandlerContext, event: ev.InteractionEvent,
                      parsed: cid.CustomId) -> dict:
    gate = check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                       allowed_states={lc.BATTLE, lc.BOSS_BATTLE})
    engine = _engine_for(ctx, gate.run)
    unit = engine.acting_unit()
    if unit is None or unit.side != un.ALLY:
        raise GateError(errors.ILLEGAL_STATE, reason="not a player turn")

    card_instance_id = int(parsed.payload)
    target_ids = [int(value) for value in event.values]
    return _resolve_card(ctx, gate.run, engine, unit, card_instance_id, target_ids,
                         parsed)


def _resolve_card(ctx, run, engine, unit, card_instance_id, target_ids,
                  parsed) -> dict:
    """One committed authoritative state transition = one revision increment.

    The player's turn, every enemy turn that follows it, and any round boundary
    they trigger are ONE transition, committed under ONE CAS in the same local
    transaction (§16.7, B-14) — the player submits again only when the battle is
    back at PHASE D-P step P7.
    """
    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        try:
            result = engine.play_card(unit, card_instance_id, target_ids)
        except ValueError as error:
            raise GateError(errors.ILLEGAL_STATE, reason=str(error)) from error
        # Enemy turns, stunned turns and auto-defends carry no component
        # interaction, so the engine has to be driven through them here.
        followups = [] if result.battle_ended else engine.advance()

    damage = len(result.damage_events) + sum(len(f.damage_events) for f in followups)
    lines = [f"{damage}건의 피해가 발생했습니다."]
    final = followups[-1] if followups else result
    if final.battle_ended or result.battle_ended:
        state = final.battle_state if final.battle_ended else result.battle_state
        lines.append("전투 종료: " + state)
    return {"action": "edit", "content": "\n".join(lines)}


def _on_skip(ctx: HandlerContext, event: ev.InteractionEvent,
             parsed: cid.CustomId) -> dict:
    """§3.2 — a 안 받기 option is mandatory on every 보상 node.

    Without it deck size only grows and deck-thinning becomes impossible.
    """
    check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                allowed_states={lc.REWARD_SELECTION})
    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        ctx.db.execute(
            "UPDATE pending_choices SET status = 'resolved', selected_option = 'skip' "
            "WHERE run_id = ? AND status = 'open'", (parsed.run_id,))
        ctx.db.execute("UPDATE runs SET state = ? WHERE run_id = ?",
                       (lc.MAP_NAVIGATION, parsed.run_id))
    return {"action": "edit", "content": errors.LABEL_SKIP}


def _on_shop_exit(ctx: HandlerContext, event: ev.InteractionEvent,
                  parsed: cid.CustomId) -> dict:
    check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                allowed_states={lc.SHOP})
    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        ctx.db.execute("UPDATE runs SET state = ? WHERE run_id = ?",
                       (lc.MAP_NAVIGATION, parsed.run_id))
    return {"action": "edit", "content": errors.LABEL_EXIT}


_INTERACTION_HANDLERS = {
    cid.ACTION_NODE_CHOOSE: _on_node_choose,
    cid.ACTION_CARD_SELECT: _on_card_select,
    cid.ACTION_TARGET_SELECT: _on_target_select,
    cid.ACTION_SKIP: _on_skip,
    cid.ACTION_SHOP_EXIT: _on_shop_exit,
}


def _engine_for(ctx: HandlerContext, run) -> bt.BattleEngine:
    battle = ctx.db.one(
        "SELECT battle_id FROM battles WHERE run_id = ? AND state = 'active' "
        "ORDER BY battle_id DESC LIMIT 1", (run["run_id"],))
    if battle is None:
        raise GateError(errors.ILLEGAL_STATE, reason="no active battle")
    return bt.build_engine(
        ctx.db, ctx.balance, battle_id=battle["battle_id"], run_id=run["run_id"],
        content_version_id=run["content_version_id"],
        rng=JournaledRng(ctx.db, run["run_id"], run["rng_seed"]),
    )
