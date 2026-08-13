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
from app.api import errors, hub, visuals
from app.api import events as ev
from app.api.gates import GateError, check_gates
from app.api import screens
from app.central import delivery
from app.content.balance import Balance
from app.content.seed import TUTORIAL_WORLD_ID, create_account
from app.db.connection import Database
from app.engine import achievements as ach
from app.engine import battle as bt
from app.engine import deck
from app.engine import lifecycle as lc
from app.engine import map_gen
from app.engine import nodes
from app.engine import progression as pg
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
    if subcommand == CMD_CHARACTERS:
        return characters_screen(ctx, event.user_id)
    if subcommand == CMD_EQUIPMENT:
        return equipment_screen(ctx, event.user_id)
    if subcommand == CMD_RESEARCH:
        return research_screen(ctx, event.user_id)
    if subcommand == CMD_SHOP:
        return hub_shop_screen(ctx, event.user_id)
    if subcommand == CMD_GACHA:
        return screens.gacha_screen(ctx.db, ctx.balance, event.user_id,
                                    ctx.content_version_id)
    if subcommand == CMD_DECK:
        return deck_screen(ctx, event.user_id)
    return _ephemeral(errors.ILLEGAL_STATE)


def deck_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 덱` — 런 중이면 그 런의 덱을, 밖이면 소장 카드와 강화를.

    캐릭터도 카드다(§5). 소장 목록은 캐릭터 카드와 행동 카드를 한 목록으로
    보여주고, 행동 카드는 여기서 바로 강화할 수 있다 — §5.8 카드 업그레이드가
    엔진에만 있고 닿을 길이 없었다.
    """
    from app.content import catalog

    run = lc.active_run_for(ctx.db, user_id)
    if run is not None:
        return _run_deck_screen(ctx, run)

    cards = catalog.owned(ctx.db, user_id, ctx.content_version_id)
    if not cards:
        return _reply("아직 가진 카드가 없습니다. `!덱아웃 뽑기`로 시작해 보세요.")

    characters = [card for card in cards if card.is_character]
    actions = [card for card in cards if not card.is_character]

    lines = [f"**덱** — 소장 {len(cards)}장 "
             f"(캐릭터 {len(characters)} · 카드 {len(actions)})"]
    for card in characters:
        lines.append(f"　{card.name} {'★' * card.star_rank} · {card.element}")

    upgradable: list[tuple] = []
    for card in sorted(actions, key=lambda c: (-c.rarity, c.card_id)):
        tier = f" +{card.upgrade_tier}" if card.upgrade_tier else ""
        line = f"　{card.name}{tier} · {card.element} · 비용 {card.cost}"
        try:
            plan = pg.check_card_upgrade(ctx.db, user_id=user_id,
                                         card_id=card.card_id,
                                         content_version_id=ctx.content_version_id)
        except pg.ProgressionError as reason:
            line += f"  ({reason})"
        else:
            cost = plan["cost"]
            line += (f"\n　　다음 강화: 조각 {plan['have_fragments']}/"
                     f"{cost['fragments']} · 와일드카드 "
                     f"{plan['have_wildcards']}/{cost['wildcards']} · "
                     f"코인 {cost['coin']}")
            if plan["affordable_locally"]:
                upgradable.append((card, plan))
        lines.append(line)

    components = []
    if upgradable:
        components.append({
            "type": "string_select", "custom_id": f"{hub.HUB_PREFIX}cardup",
            "placeholder": "강화할 카드",
            "options": [
                {"label": f"{card.name} → +{plan['next_tier']}",
                 "description": f"코인 {plan['cost']['coin']}",
                 "value": card.card_id}
                for card, plan in upgradable[:25]],
        })
    return {**_reply("\n".join(lines), components),
            "attachments": visuals.collection(
                ctx.db, user_id=user_id,
                content_version_id=ctx.content_version_id)}


def _run_deck_screen(ctx: HandlerContext, run) -> dict:
    """런 중의 `덱` — 자리마다 뽑을 더미와 버린 더미가 어떻게 남았는지.

    §16.2.3 때문에 여기 보이는 덱은 런이 시작될 때 얼려진 것이며, 지금
    계정에서 카드를 강화해도 이 런에는 반영되지 않는다.
    """
    lines = ["**덱** — 진행 중인 런"]
    for row in ctx.db.query(
        "SELECT rc.party_slot, rc.character_id, c.name FROM run_characters rc "
        "JOIN characters c ON c.character_id = rc.character_id "
        "AND c.content_version_id = ? WHERE rc.run_id = ? ORDER BY rc.party_slot",
        (run["content_version_id"], run["run_id"]),
    ):
        piles = {entry["pile"]: entry["n"] for entry in ctx.db.query(
            "SELECT pile, COUNT(*) AS n FROM run_deck_cards WHERE run_id = ? "
            "AND party_slot = ? GROUP BY pile", (run["run_id"], row["party_slot"]))}
        cursed = ctx.db.one(
            "SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ? "
            "AND party_slot = ? AND is_cursed = 1",
            (run["run_id"], row["party_slot"]))
        line = (f"{row['name']} — 뽑을 더미 {piles.get('draw', 0)} · "
                f"버린 더미 {piles.get('discard', 0)} · "
                f"손패 {piles.get('in_hand', 0)}")
        if cursed and cursed["n"]:
            line += f" · 저주 {cursed['n']}"
        lines.append(line)
    lines.append("이 런의 덱은 시작할 때 고정됩니다. "
                 "계정에서 카드를 강화해도 다음 런부터 반영됩니다. (§16.2.3)")
    return _reply("\n".join(lines))


def characters_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 캐릭터` — 보유 캐릭터와 성급 상승 비용 (§4.4)."""
    rows = ctx.db.query(
        "SELECT oc.character_id, oc.star_rank, c.name, c.element, c.job_role, "
        "c.special_cap FROM owned_characters oc JOIN characters c "
        "ON c.character_id = oc.character_id AND c.content_version_id = ? "
        "WHERE oc.user_id = ? ORDER BY oc.acquired_at",
        (ctx.content_version_id, user_id))
    if not rows:
        return _reply("보유한 캐릭터가 없습니다.")

    lines = ["**캐릭터**"]
    #: 재화가 실제로 충분한 것만 버튼에 올린다. 눌러도 거절되는 선택지를
    #: 늘어놓으면 목록만 길어진다.
    ready: list[tuple] = []
    for row in rows:
        cap = pg.star_cap(ctx.balance, bool(row["special_cap"]))
        stars = "★" * int(row["star_rank"])
        line = (f"{row['name']} {stars} ({row['star_rank']}/{cap}) · "
                f"{row['element']} · {row['job_role']}")
        try:
            plan = pg.check_star_up(ctx.db, ctx.balance, user_id=user_id,
                                    character_id=row["character_id"],
                                    content_version_id=ctx.content_version_id)
            cost = plan["cost"]
            line += (f"\n　다음 성급: 조각 {plan['have_fragments']}/{cost['fragments']} "
                     f"· 와일드카드 {plan['have_wildcards']}/{cost['wildcards']} "
                     f"· 코인 {cost['coin']}")
        except pg.ProgressionError as reason:
            line += f"\n　{reason}"
        else:
            if plan["affordable_locally"]:
                ready.append((row["character_id"], row["name"], plan))
        lines.append(line)

    components = []
    if ready:
        components.append({
            "type": "string_select", "custom_id": f"{hub.HUB_PREFIX}starup",
            "placeholder": "성급을 올릴 캐릭터",
            "options": [
                {"label": f"{name} → {plan['next_rank']}★",
                 "description": f"코인 {plan['cost']['coin']}", "value": cid_}
                for cid_, name, plan in ready[:25]],
        })
    return _reply("\n".join(lines), components)


def equipment_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 장비` — 보유 장비, 티어, 다음 강화 비용 (§8.4)."""
    rows = ctx.db.query(
        "SELECT oe.equipment_instance_id, oe.tier, oe.equipped_character_id, "
        "ed.name, ed.slot, ed.set_name FROM owned_equipment oe "
        "JOIN equipment_defs ed ON ed.equipment_def_id = oe.equipment_def_id "
        "AND ed.content_version_id = ? WHERE oe.user_id = ? "
        "ORDER BY oe.equipment_instance_id", (ctx.content_version_id, user_id))
    stones = ctx.db.query(
        "SELECT tier, amount FROM enhancement_stones WHERE user_id = ? "
        "AND amount > 0 ORDER BY tier", (user_id,))

    lines = ["**장비**"]
    if stones:
        lines.append("강화석 " + " · ".join(
            f"T{row['tier']}×{row['amount']}" for row in stones))
    if not rows:
        lines.append("보유한 장비가 없습니다.")
        return _reply("\n".join(lines))

    max_tier = int(ctx.balance.get("equipment_max_tier"))
    #: 강화석이 있는지는 엔진이 판단한다. 여기서는 아직 최고 티어가 아닌
    #: 장비만 올린다.
    upgradable: list[tuple] = []
    for row in rows:
        equipped = f" [{row['equipped_character_id']}]" if row["equipped_character_id"] else ""
        line = f"{row['name']} T{row['tier']} · {row['slot']}{equipped}"
        if int(row["tier"]) < max_tier:
            cost = pg.enhancement_cost(ctx.balance, int(row["tier"]) + 1)
            need = f"T{int(row['tier']) + 1}×{cost['current_tier_stones']}"
            if cost["previous_tier_stones"]:
                need += f" + T{row['tier']}×{cost['previous_tier_stones']}"
            line += f"\n　다음 강화: {need}"
            upgradable.append((row["equipment_instance_id"], row["name"],
                               int(row["tier"]) + 1))
        lines.append(line)

    components = []
    if upgradable:
        components.append({
            "type": "string_select", "custom_id": f"{hub.HUB_PREFIX}enhance",
            "placeholder": "강화할 장비",
            "options": [{"label": f"{name} → T{target}", "value": str(instance)}
                        for instance, name, target in upgradable[:25]],
        })
    components.append({
        "type": "string_select", "custom_id": f"{hub.HUB_PREFIX}equip",
        "placeholder": "장착할 장비",
        "options": [{"label": f"{row['name']} T{row['tier']} ({row['slot']})",
                     "value": str(row["equipment_instance_id"])}
                    for row in rows[:25]],
    })
    return _reply("\n".join(lines), components)


def research_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 연구` — §20.5대로 필요 업적과 진행도를 인라인으로 보여준다."""
    listing = pg.research_status(ctx.db, user_id=user_id,
                                 content_version_id=ctx.content_version_id)
    account = ctx.db.one("SELECT wildcards FROM accounts WHERE user_id = ?",
                         (user_id,))
    lines = [f"**연구** (와일드카드 {account['wildcards']})"]
    available: list[dict] = []
    for entry in listing:
        completed = entry["steps_taken"] >= entry["max_steps"]
        step = (f" [{entry['steps_taken']}/{entry['max_steps']}]"
                if entry["max_steps"] > 1 else "")
        if completed:
            # 다 끝난 노드에 다음 단계 가격이나 선행 조건을 붙이지 않는다.
            lines.append(f"✅ {entry['name']}{step}")
            continue

        mark = "　" if entry["available"] else "🔒"
        line = (f"{mark} {entry['name']}{step} — 코인 {entry['coin_cost']} "
                f"+ 와일드카드 {entry['wildcard_cost']}")
        progress = entry["achievement_progress"]
        if progress and not entry["available"]:
            # 잠긴 노드는 단순히 거부하는 대신 스스로를 설명한다 (§20.5).
            line += (f"\n　선행: 「{progress['name']}」 "
                     f"{progress['current']}/{progress['target']}")
        if entry["available"]:
            available.append(entry)
        lines.append(line)

    components = []
    if available:
        components.append({
            "type": "string_select", "custom_id": f"{hub.HUB_PREFIX}research",
            "placeholder": "해금할 연구",
            "options": [
                {"label": entry["name"][:80],
                 "description": f"코인 {entry['coin_cost']} · "
                                f"와일드카드 {entry['wildcard_cost']}",
                 "value": entry["node_id"]}
                for entry in available[:25]],
        })
    return _reply("\n".join(lines), components)


def hub_shop_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 상점` — 허브 상점은 전투 루프 밖이므로 코인 결제가 허용된다
    (§7.2)."""
    listing = pg.hub_shop_listing(ctx.db, ctx.balance,
                                  content_version_id=ctx.content_version_id)
    lines = ["**허브 상점**", "장비"]
    for entry in listing["equipment"]:
        lines.append(f"　{entry['name']} ({entry['slot']}) — 코인 {entry['price_coin']}")
    lines.append("장비 강화석")
    lines.append("　" + " · ".join(
        f"T{entry['tier']} {entry['price_coin']}" for entry in listing["stones"]))

    components = [
        {"type": "string_select", "custom_id": f"{hub.HUB_PREFIX}buyequip",
         "placeholder": "구매할 장비",
         "options": [{"label": entry["name"][:80],
                      "description": f"{entry['slot']} · 코인 {entry['price_coin']}",
                      "value": entry["equipment_def_id"]}
                     for entry in listing["equipment"][:25]]},
        {"type": "string_select", "custom_id": f"{hub.HUB_PREFIX}buystone",
         "placeholder": "구매할 강화석",
         "options": [{"label": f"T{entry['tier']} 강화석",
                      "description": f"코인 {entry['price_coin']}",
                      "value": str(entry["tier"])}
                     for entry in listing["stones"][:25]]},
    ]
    return _reply("\n".join(lines), components)


def hub_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃` → 허브 화면, a public reply in the Deckout channel.

    Re-issuing it while a run is active re-renders the current screen (§16.3);
    if the thread was deleted it is recreated at surface_generation + 1 (§16.8).
    """
    expired = lc.expire_if_stale(ctx.db, ctx.balance, user_id)

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
    lines = []
    if expired is not None:
        kept = len(expired.get("inventory", {}).get("kept", []))
        lines.append(f"오래 조작이 없어 이전 런을 정리했습니다. 보관 {kept}개.")
    lines += [
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
    # 방치된 런이 §16.3 규칙으로 계정을 막고 있을 수 있다. 새 런을 거절하기
    # **전에** 확인한다 — 그러지 않으면 만료 규칙이 있으나 마나가 된다.
    lc.expire_if_stale(ctx.db, ctx.balance, user_id)
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
        # 두 번째 캐릭터는 §5.10 첫 뽑기 보장으로 들어온다 — 어디로 가야 하는지
        # 알려주지 않으면 플레이어는 여기서 막힌다.
        return _ephemeral(f"{errors.PARTY_TOO_SMALL}\n`!덱아웃 뽑기`로 동료를 모아보세요.")

    # 이전 준비를 끝내지 않고 다시 들어온 경우가 있으므로 초안을 비우고 시작한다.
    screens.clear_draft(ctx.db, user_id)
    return screens.world_select_screen(ctx.db, user_id, ctx.content_version_id)


def _prepare_and_materialize(ctx: HandlerContext, user_id: int, world_id: str, *,
                             is_tutorial: bool,
                             party: list[str] | None = None) -> dict:
    """튜토리얼 경로 — 월드도 파티도 정해져 있으므로 [1]-[4]를 건너뛴다.

    [5] MATERIALIZE와 [6] SURFACE는 준비 화면과 같은 코드를 쓴다: 스레드 생성을
    빠뜨린 런은 화면 없이 계정만 점유한다 (§16.3).
    """
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

    return {**screens.surface_request(ctx.db, run_id, user_id),
            "action": "reply_ephemeral"}


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
    # 준비·뽑기 화면은 런 밖에서 동작하므로 §19.2의 custom_id 형식(run_id를
    # 요구한다)을 쓸 수 없다. 각자의 접두사로 먼저 갈라낸다.
    if event.custom_id.startswith(screens.PREP_PREFIX):
        return screens.handle_prep(ctx.db, ctx.balance, event.user_id,
                                   event.custom_id, event.values,
                                   ctx.content_version_id)
    if event.custom_id.startswith(hub.HUB_PREFIX):
        return hub.handle_hub(ctx, event.user_id, event.custom_id, event.values,
                              event_id=event.event_id)
    if event.custom_id.startswith(screens.GACHA_PREFIX):
        return screens.handle_gacha(ctx.db, ctx.balance, event.user_id,
                                    event.custom_id, ctx.content_version_id,
                                    event_id=event.event_id)

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
    """맵 화면 → node buttons (branch width 2-3), then §3 node resolution."""
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
        try:
            result = nodes.resolve_node(ctx.db, ctx.balance, _rng(ctx, gate.run),
                                        run_id=parsed.run_id, node_index=node_index)
        except nodes.NodeError as error:
            # Surfaced as an ephemeral notice rather than a 500: an unhandled
            # exception would leave the event unrecorded and Central would
            # redeliver it indefinitely (§16.7).
            raise GateError(errors.ILLEGAL_STATE, reason=str(error)) from error

        # A 전투 node starts with whichever unit is fastest, which is often an
        # enemy (§2.8.6), so the battle has to be driven to the first player
        # decision before the screen is rendered.
        conclusion = None
        if result["screen"] == "battle":
            conclusion = _drive_battle(ctx, gate.run, result["battle_id"])

    content = _screen_summary(node, result)
    if conclusion is not None:
        content = f"{content}\n{_conclusion_summary(conclusion)}"
    return {"action": "edit", "content": content,
            "attachments": _screen_art(ctx, gate.run, result, conclusion)}


def _screen_art(ctx: HandlerContext, run, result: dict,
                conclusion: dict | None = None) -> list[dict]:
    """이 화면에 붙일 그림 (§11).

    그림은 곁가지라 실패해도 글자만으로 화면이 나간다 — `visuals` 가 예외를
    삼킨다. 여기서 터지면 조작이 통째로 되돌아가고 중앙봇이 이벤트를 무한히
    재전송하게 된다 (§16.7).
    """
    if conclusion is not None and conclusion.get("screen") == "settlement":
        return visuals.settlement(conclusion["report"])
    # 런 행은 전투가 진행되며 갱신되므로 다시 읽는다.
    fresh = ctx.db.one("SELECT * FROM runs WHERE run_id = ?", (run["run_id"],))
    screen = result.get("screen")
    if screen == "battle" and result.get("battle_id"):
        return visuals.battle(ctx.db, ctx.balance,
                              battle_id=result["battle_id"], run=fresh)
    if screen == "shop":
        return visuals.shop(ctx.db, fresh, result.get("items", []))
    if screen == "map":
        return visuals.game_map(ctx.db, fresh)
    return []


def _screen_summary(node, result: dict) -> str:
    if result["screen"] == "battle":
        return f"{node['node_type']} — 전투 시작"
    if result["screen"] == "reward":
        return f"보상: {len(result.get('options', []))}장 중 선택 · {errors.LABEL_SKIP}"
    if result["screen"] == "shop":
        return f"상점: {len(result.get('items', []))}개 상품 · {errors.LABEL_EXIT}"
    if result["screen"] == "event":
        return f"이벤트: {result['options']['name']}"
    return f"{node['node_type']} 노드를 해결했습니다."


def _drive_battle(ctx: HandlerContext, run, battle_id: int) -> dict | None:
    """Advance a fresh battle to its first player decision.

    A battle can end before the player ever acts — a fast enemy can wipe a
    weakened party during the opening turns — so the conclusion has to be
    routed here too. Leaving `runs.state = 'battle'` with no active battle row
    would soft-lock every later interaction.
    """
    engine = bt.build_engine(
        ctx.db, ctx.balance, battle_id=battle_id, run_id=run["run_id"],
        content_version_id=run["content_version_id"], rng=_rng(ctx, run))
    results = engine.advance()
    if results and results[-1].battle_ended:
        return nodes.conclude_battle(
            ctx.db, ctx.balance, _rng(ctx, run), run_id=run["run_id"],
            battle_id=battle_id)
    return None


def _on_reward_pick(ctx: HandlerContext, event: ev.InteractionEvent,
                    parsed: cid.CustomId) -> dict:
    """보상 화면 → card Select + recipient Select.

    The payload carries `<card_id>|<party_slot>`; the recipient step is
    required because decks are per-character (§3.2).
    """
    gate = check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                       allowed_states={lc.REWARD_SELECTION})
    choice = ctx.db.one(
        "SELECT choice_id FROM pending_choices WHERE run_id = ? AND status = 'open'",
        (parsed.run_id,))
    if choice is None:
        raise GateError(errors.ILLEGAL_STATE, reason="no open reward choice")

    raw = event.values[0] if event.values else parsed.payload
    card_id, _, slot = raw.partition("|")
    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        try:
            nodes.choose_reward(ctx.db, parsed.run_id,
                                choice_id=choice["choice_id"], card_id=card_id,
                                party_slot=int(slot) if slot else None)
        except nodes.NodeError as error:
            raise GateError(errors.ILLEGAL_STATE, reason=str(error)) from error
    return {"action": "edit", "content": f"{card_id} 카드를 받았습니다."}


def _on_shop_buy(ctx: HandlerContext, event: ev.InteractionEvent,
                 parsed: cid.CustomId) -> dict:
    """상점 화면 → item buttons. A purchase is a SELF-LOOP (§16.2)."""
    gate = check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                       allowed_states={lc.SHOP})
    item_index = int(parsed.payload)
    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        try:
            result = nodes.buy_shop_item(
                ctx.db, _rng(ctx, gate.run), parsed.run_id,
                node_index=gate.run["current_node_index"], item_index=item_index)
        except nodes.NodeError as error:
            message = (errors.INSUFFICIENT_CURRENCY if "재화" in str(error)
                       else errors.ILLEGAL_STATE)
            raise GateError(message, reason=str(error)) from error
    run = ctx.db.one("SELECT * FROM runs WHERE run_id = ?", (parsed.run_id,))
    items = [dict(row) for row in ctx.db.query(
        "SELECT * FROM run_shop_items WHERE run_id = ? AND node_index = ? "
        "ORDER BY item_index", (parsed.run_id, run["current_node_index"]))]
    return {"action": "edit",
            "content": f"구매 완료 · 탐험 자금 {result['run_currency']}",
            "attachments": visuals.shop(ctx.db, run, items)}


def _on_event_branch(ctx: HandlerContext, event: ev.InteractionEvent,
                     parsed: cid.CustomId) -> dict:
    """이벤트 화면 → branch buttons. A branch may transition into battle."""
    gate = check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                       allowed_states={lc.EVENT_CHOICE})
    choice = ctx.db.one(
        "SELECT choice_id FROM pending_choices WHERE run_id = ? AND status = 'open'",
        (parsed.run_id,))
    if choice is None:
        raise GateError(errors.ILLEGAL_STATE, reason="no open event choice")

    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        try:
            result = nodes.choose_event_branch(
                ctx.db, ctx.balance, _rng(ctx, gate.run), parsed.run_id,
                choice_id=choice["choice_id"], branch_index=int(parsed.payload))
        except nodes.NodeError as error:
            raise GateError(errors.ILLEGAL_STATE, reason=str(error)) from error
        conclusion = None
        if result["screen"] == "battle":
            conclusion = _drive_battle(ctx, gate.run, result["battle_id"])

    if result.get("suspended"):
        # §10.4.2 — the branch stopped on a PENDING_CHOICE operator. Render the
        # nested prompt instead of claiming the event resolved.
        return _render_nested_choice(ctx, gate.run, parsed, result)
    if result["screen"] == "battle":
        content = "전투가 시작되었습니다."
        if conclusion is not None:
            content = f"{content}\n{_conclusion_summary(conclusion)}"
        return {"action": "edit", "content": content,
                "attachments": _screen_art(ctx, gate.run, result, conclusion)}
    return {"action": "edit", "content": "이벤트를 해결했습니다."}


def _render_nested_choice(ctx: HandlerContext, run, parsed: cid.CustomId,
                          result: dict) -> dict:
    """Prompt for a suspended operator's decision (§16.5).

    Only `remove_cursed_card` needs a real picker in the seed content — with
    more than one cursed card present the player picks, with exactly one it
    auto-resolves, and with none the operator is a no-op (§2.7.4).
    """
    revision = ctx.db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                          (parsed.run_id,))["presentation_revision"]
    cursed = deck.cursed_cards_in_deck(ctx.db, parsed.run_id)
    if not cursed:
        nodes.resume_pending_choice(ctx.db, parsed.run_id, selection=None)
        return {"action": "edit", "content": "제거할 저주받은 카드가 없습니다."}
    if len(cursed) == 1:
        nodes.resume_pending_choice(ctx.db, parsed.run_id,
                                    selection=cursed[0]["card_instance_id"])
        return {"action": "edit", "content": "저주받은 카드를 제거했습니다."}

    return {
        "action": "edit",
        "content": "제거할 저주받은 카드를 선택하세요.",
        "components": [{
            "type": "string_select",
            "custom_id": cid.build(cid.ACTION_CLEANSE_PICK, parsed.run_id,
                                   parsed.generation, revision),
            "options": [
                {"label": card["card_id"], "value": str(card["card_instance_id"])}
                for card in cursed[:25]
            ],
        }],
    }


def _on_cleanse_pick(ctx: HandlerContext, event: ev.InteractionEvent,
                     parsed: cid.CustomId) -> dict:
    """§2.7.4 — the player picks which 저주받은 카드 is removed."""
    check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                allowed_states={lc.EVENT_CHOICE, lc.SHOP, lc.REWARD_SELECTION})
    raw = event.values[0] if event.values else parsed.payload
    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        try:
            nodes.resume_pending_choice(ctx.db, parsed.run_id,
                                        selection=int(raw))
        except nodes.NodeError as error:
            raise GateError(errors.ILLEGAL_STATE, reason=str(error)) from error
    return {"action": "edit", "content": "저주받은 카드를 제거했습니다."}


def _rng(ctx: HandlerContext, run) -> JournaledRng:
    return JournaledRng(ctx.db, run["run_id"], run["rng_seed"])


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

        damage = len(result.damage_events) + sum(len(f.damage_events)
                                                 for f in followups)
        lines = [f"{damage}건의 피해가 발생했습니다."]

        final = followups[-1] if followups else result
        ended = final.battle_ended or result.battle_ended
        if ended:
            # §16.2 — route the finished battle: post_battle rewards, a
            # tutorial retry, or settlement.
            conclusion = nodes.conclude_battle(
                ctx.db, ctx.balance, _rng(ctx, run), run_id=parsed.run_id,
                battle_id=engine.battle_id)
            lines.append(_conclusion_summary(conclusion))
    run = ctx.db.one("SELECT * FROM runs WHERE run_id = ?", (parsed.run_id,))
    art = (visuals.settlement(conclusion["report"])
           if conclusion is not None and conclusion.get("screen") == "settlement"
           else visuals.battle(ctx.db, ctx.balance, battle_id=engine.battle_id,
                               run=run))
    return {"action": "edit", "content": "\n".join(lines), "attachments": art}


def _conclusion_summary(conclusion: dict) -> str:
    screen = conclusion["screen"]
    if screen == "settlement":
        report = conclusion["report"]
        kept = len(report.get("inventory", {}).get("kept", []))
        if conclusion.get("cleared"):
            rewards = report.get("rewards", {})
            return (f"월드 클리어! 코인 {rewards.get('coin', 0)} · "
                    f"카르타 {rewards.get('carta', 0)} · 보관 {kept}개")
        return f"런 종료 — 보관 {kept}개"
    if screen == "node_resolution" and conclusion.get("tutorial_retry"):
        # §3.4.1 — defeat does not end the tutorial run.
        return "패배했지만 튜토리얼은 계속됩니다. 다시 도전하세요."
    rewards = conclusion.get("rewards", {})
    drops = len(rewards.get("drops", []))
    return f"승리! 탐험 자금 +{rewards.get('run_currency', 0)} · 획득 {drops}개"


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
    gate = check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                       allowed_states={lc.SHOP})
    with ctx.db.tx():
        lc.claim_mutation(ctx.db, parsed.run_id, parsed.revision)
        # 상태 전이는 `nodes` 가 소유한다. 여기서 UPDATE를 직접 쓰면 두 곳이
        # 같은 규칙을 따로 들고 있게 되어 언젠가 갈라진다.
        nodes.leave_shop(ctx.db, parsed.run_id)
    return {"action": "edit", "content": errors.LABEL_EXIT,
            "attachments": visuals.game_map(ctx.db, gate.run)}


_INTERACTION_HANDLERS = {
    cid.ACTION_NODE_CHOOSE: _on_node_choose,
    cid.ACTION_CARD_SELECT: _on_card_select,
    cid.ACTION_TARGET_SELECT: _on_target_select,
    cid.ACTION_REWARD_PICK: _on_reward_pick,
    cid.ACTION_SKIP: _on_skip,
    cid.ACTION_SHOP_BUY: _on_shop_buy,
    cid.ACTION_SHOP_EXIT: _on_shop_exit,
    cid.ACTION_EVENT_BRANCH: _on_event_branch,
    cid.ACTION_CLEANSE_PICK: _on_cleanse_pick,
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
