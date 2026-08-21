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
from app.api import controls, errors, hub, visuals
from app.api import events as ev
from app.api.gates import GateError, check_gates
from app.api import screens
from app.central import delivery, surfaces
from app.config import settings
from app.content.balance import Balance
from app.content.seed import TUTORIAL_WORLD_ID, create_account
from app.db.connection import Database
from app.engine import achievements as ach
from app.engine import attendance as att
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
CMD_PASSIVES = "패시브"


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


def _registration_prompt() -> dict:
    return _reply(
        "**덱아웃**에 오신 것을 환영합니다. 시작하려면 먼저 가입해 주세요.",
        [{"type": "button", "custom_id": f"{REGISTER_PREFIX}go", "label": "가입하기"}])


def handle_register(ctx: HandlerContext, user_id: int) -> dict:
    """가입 버튼 → 계정을 만들고 허브로. 이미 가입돼 있으면 그대로 허브만."""
    create_account(ctx.db, user_id, ctx.content_version_id)
    screen = hub_screen(ctx, user_id)
    return {**screen, "action": "edit",
           "content": f"가입되었습니다. 먼저 튜토리얼을 진행해 주세요 "
                      f"(`!덱아웃 시작`).\n\n{screen.get('content', '')}"}


#: 중앙봇의 사용자 응답에서 코인 잔액을 찾을 때 볼 키들. 연동 가이드의 응답
#: 스키마가 이 저장소에 없어서(§1.1 표에 경로만 있다) 흔한 이름을 순서대로
#: 본다. 가이드를 확인하면 고칠 곳은 이 상수 하나다.
COIN_BALANCE_KEYS = ("balance", "coin", "coins", "currency", "value")


def coin_balance(ctx: HandlerContext, user_id: int) -> int | None:
    """플레이어의 코인 잔액. 알 수 없으면 None.

    **표시 전용이다 (§17.3).** 권위 있는 검사는 언제나 §17 트랜잭션 안에서
    중앙봇이 한다 — 여기서 읽은 값과 결제 시점 사이에 다른 미니게임이 차감할
    수 있기 때문이다. 그래서 이 값으로 버튼을 막지 않는다.

    코인 가격은 성급 상승·카드 강화·연구·상점 다섯 화면에 전부 찍히는데
    "내가 얼마 있는지"는 어디에도 없었다. 살 수 있는지 없는지를 눌러 봐야
    아는 화면이었다는 뜻이다.

    중앙봇이 죽어 있어도 화면은 떠야 하므로 실패는 None 으로 돌려준다.
    """
    if ctx.central is None:
        return None
    try:
        payload = ctx.central.get_user(user_id) or {}
    except Exception:                                        # noqa: BLE001
        logger.info("코인 잔액을 읽지 못했습니다 (user %s)", user_id)
        return None
    for key in COIN_BALANCE_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    logger.warning(
        "사용자 응답에서 코인 잔액을 찾지 못했습니다. 받은 키: %s — "
        "handlers.COIN_BALANCE_KEYS 를 연동 가이드에 맞춰 고쳐야 합니다",
        sorted(payload.keys()))
    return None


def _coin_line(ctx: HandlerContext, user_id: int) -> str:
    balance = coin_balance(ctx, user_id)
    return f"코인 {balance}" if balance is not None else "코인 —"


# =====================================================================
# Message commands
# =====================================================================
#: 계정이 없는 사용자에게 내미는 가입 버튼. 런 밖 화면들과 같은 이유로
#: (§19.2의 custom_id는 run_id를 요구하는데 가입 전에는 run_id 자체가
#: 없다) 전용 접두사를 쓴다.
REGISTER_PREFIX = "dko:reg:"

#: 튜토리얼을 마치기 전에도 눌러야 하는 것들 — 허브 자체와, 튜토리얼을
#: 시작·재시도·포기하는 길. 나머지(뽑기·상점·캐릭터·장비·연구·업적·덱·
#: 패시브)는 튜토리얼을 마친 뒤에만 연다.
_ALLOWED_BEFORE_TUTORIAL = frozenset({"", "시작", "포기"})


def handle_message(ctx: HandlerContext, event: ev.MessageEvent) -> dict:
    if ctx.content_version_id is None:
        return _ephemeral("콘텐츠가 아직 준비되지 않았습니다.")

    subcommand = (event.args[0] if event.args else CMD_HUB)

    # 계정이 없으면 어떤 명령을 쳤든 가입부터 시킨다 — §4.6.5는 첫 명령에서
    # 계정을 조용히 만들었지만, 그러면 "가입"이라는 순간이 플레이어에게
    # 전혀 보이지 않는다. 오너 지시로 명시적인 가입 단계를 둔다.
    account = ctx.db.one("SELECT * FROM accounts WHERE user_id = ?", (event.user_id,))
    if account is None:
        return _registration_prompt()

    # 튜토리얼을 마치기 전에는 그 밖의 진행(뽑기·상점 등)을 열지 않는다 —
    # "강제 튜토리얼". 마친 계정이나 이미 런이 진행 중인 계정은 그대로
    # 지나간다(런 중 상태 검사는 각 화면·게이트가 따로 한다).
    if (account["tutorial_completed_at"] is None
            and subcommand not in _ALLOWED_BEFORE_TUTORIAL
            and lc.active_run_for(ctx.db, event.user_id) is None):
        return _ephemeral(errors.TUTORIAL_NOT_CLEARED)

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
    if subcommand == CMD_PASSIVES:
        return passives_screen(ctx, event.user_id)
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
             f"(캐릭터 {len(characters)} · 카드 {len(actions)}) · "
             f"{_coin_line(ctx, user_id)}"]
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
    render_rows: list[dict] = []
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
        cursed_n = cursed["n"] if cursed else 0
        line = (f"{row['name']} — 뽑을 더미 {piles.get('draw', 0)} · "
                f"버린 더미 {piles.get('discard', 0)} · "
                f"손패 {piles.get('in_hand', 0)}")
        if cursed_n:
            line += f" · 저주 {cursed_n}"
        lines.append(line)
        render_rows.append({"name": row["name"], "draw": piles.get("draw", 0),
                           "discard": piles.get("discard", 0),
                           "hand": piles.get("in_hand", 0), "cursed": cursed_n})
    lines.append("이 런의 덱은 시작할 때 고정됩니다. "
                 "계정에서 카드를 강화해도 다음 런부터 반영됩니다. (§16.2.3)")
    return {**_reply("\n".join(lines)), "attachments": visuals.run_deck(render_rows)}


def _expire_and_close(ctx: HandlerContext, user_id: int) -> dict | None:
    """방치된 런을 정산하고, 그 런의 스레드 화면도 마지막 상태로 바꾼다.

    정산만 하고 화면을 그대로 두면 플레이어의 스레드에는 지도와 살아 있는
    버튼이 남는다. 그 버튼은 게이트가 막아 주지만, 왜 안 되는지는 아무 데도
    쓰여 있지 않다.
    """
    expired = lc.expire_if_stale(ctx.db, ctx.balance, user_id)
    if expired is None:
        return None
    kept = len(expired.get("inventory", {}).get("kept", []))
    surfaces.close_run_surface(
        ctx.db, ctx.central, expired["run_id"],
        summary=f"오래 조작이 없어 이 런을 정리했습니다. 보관 {kept}개. (§16.3)")
    return expired


def passives_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 패시브` — 보유 패시브와 그것이 실제로 하는 일 (§6).

    패시브를 뽑을 수는 있는데 그것을 볼 곳이 준비 화면의 드롭다운뿐이었다.
    무엇을 가지고 있는지도, 그것이 무슨 효과인지도 확인할 방법이 없으면
    슬롯에 무엇을 넣을지 고를 수가 없다.
    """
    from app.engine import passives as pv

    owned = pv.owned(ctx.db, user_id, ctx.content_version_id)
    account = ctx.db.one("SELECT passive_slots FROM accounts WHERE user_id = ?",
                         (user_id,))
    slots = int(account["passive_slots"]) if account else 0

    if not owned:
        return _reply(
            f"**패시브** — 슬롯 {slots}칸\n"
            "아직 해금한 패시브가 없습니다. 패시브는 `!덱아웃 뽑기`로 얻습니다.")

    # 진행 중인 런이 있으면 지금 무엇을 끼고 있는지도 보여준다 — 장착은 런
    # 단위이고 (§6) 런 도중에는 바꿀 수 없으므로, 그 사실이 드러나야 한다.
    run = lc.active_run_for(ctx.db, user_id)
    equipped = set()
    if run is not None:
        equipped = {row["passive_card_id"] for row in ctx.db.query(
            "SELECT passive_card_id FROM run_passives WHERE run_id = ?",
            (run["run_id"],))}

    when = {pv.TRIGGER_BATTLE_START: "전투 시작", pv.TRIGGER_ROUND_START: "라운드마다"}
    lines = [f"**패시브** — 보유 {len(owned)}장 · 슬롯 {slots}칸"]
    for row in owned:
        mark = "▶" if row["passive_card_id"] in equipped else "　"
        lines.append(
            f"{mark} {row['name']} ({'★' * int(row['rarity_tier'])}) · "
            f"{when.get(row['trigger_event'], row['trigger_event'])}")
        if row["description"]:
            lines.append(f"　　{row['description']}")

    if run is not None:
        lines.append("장착은 런을 시작할 때 정해집니다. "
                     "진행 중인 런에서는 바꿀 수 없습니다. (§6)")
    else:
        lines.append(f"런을 시작할 때 최대 {slots}장까지 고를 수 있습니다. "
                     "슬롯은 `!덱아웃 연구`로 늘립니다.")

    return {**_reply("\n".join(lines)),
            "attachments": visuals.passive_collection(
                ctx.db, user_id=user_id,
                content_version_id=ctx.content_version_id)}


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

    lines = [f"**캐릭터** — {_coin_line(ctx, user_id)}"]
    #: 재화가 실제로 충분한 것만 버튼에 올린다. 눌러도 거절되는 선택지를
    #: 늘어놓으면 목록만 길어진다.
    ready: list[tuple] = []
    render_rows: list[dict] = []
    for row in rows:
        cap = pg.star_cap(ctx.balance, bool(row["special_cap"]))
        stars = "★" * int(row["star_rank"])
        line = (f"{row['name']} {stars} ({row['star_rank']}/{cap}) · "
                f"{row['element']} · {row['job_role']}")
        render_row = {"character_id": row["character_id"], "name": row["name"],
                     "element": row["element"], "job_role": row["job_role"],
                     "star_rank": row["star_rank"]}
        try:
            plan = pg.check_star_up(ctx.db, ctx.balance, user_id=user_id,
                                    character_id=row["character_id"],
                                    content_version_id=ctx.content_version_id)
            cost = plan["cost"]
            line += (f"\n　다음 성급: 조각 {plan['have_fragments']}/{cost['fragments']} "
                     f"· 와일드카드 {plan['have_wildcards']}/{cost['wildcards']} "
                     f"· 코인 {cost['coin']}")
            render_row["status"] = f"다음 성급: 코인 {cost['coin']}"
            render_row["status_ready"] = bool(plan["affordable_locally"])
        except pg.ProgressionError as reason:
            line += f"\n　{reason}"
            render_row["status"] = str(reason)
        else:
            if plan["affordable_locally"]:
                ready.append((row["character_id"], row["name"], plan))
        lines.append(line)
        render_rows.append(render_row)

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
    return {**_reply("\n".join(lines), components),
            "attachments": visuals.characters(render_rows)}


def equipment_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 장비` — 보유 장비, 티어, 다음 강화 비용 (§8.4)."""
    rows = ctx.db.query(
        "SELECT oe.equipment_instance_id, oe.equipment_def_id, oe.tier, "
        "oe.equipped_character_id, ed.name, ed.slot, ed.set_name FROM owned_equipment oe "
        "JOIN equipment_defs ed ON ed.equipment_def_id = oe.equipment_def_id "
        "AND ed.content_version_id = ? WHERE oe.user_id = ? "
        "ORDER BY oe.equipment_instance_id", (ctx.content_version_id, user_id))
    stones = ctx.db.query(
        "SELECT tier, amount FROM enhancement_stones WHERE user_id = ? "
        "AND amount > 0 ORDER BY tier", (user_id,))
    stone_rows = [dict(row) for row in stones]

    lines = [f"**장비** — {_coin_line(ctx, user_id)}"]
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
    render_rows: list[dict] = []
    for row in rows:
        equipped = f" [{row['equipped_character_id']}]" if row["equipped_character_id"] else ""
        line = f"{row['name']} T{row['tier']} · {row['slot']}{equipped}"
        render_row = {"equipment_def_id": row["equipment_def_id"], "name": row["name"],
                     "tier": row["tier"], "slot": row["slot"],
                     "equipped_character_id": row["equipped_character_id"]}
        if int(row["tier"]) < max_tier:
            cost = pg.enhancement_cost(ctx.balance, int(row["tier"]) + 1)
            need = f"T{int(row['tier']) + 1}×{cost['current_tier_stones']}"
            if cost["previous_tier_stones"]:
                need += f" + T{row['tier']}×{cost['previous_tier_stones']}"
            line += f"\n　다음 강화: {need}"
            render_row["next_enhance"] = f"다음 강화: {need}"
            upgradable.append((row["equipment_instance_id"], row["name"],
                               int(row["tier"]) + 1))
        lines.append(line)
        render_rows.append(render_row)

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
    return {**_reply("\n".join(lines), components),
            "attachments": visuals.equipment(render_rows, stones=stone_rows)}


def research_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 연구` — §20.5대로 필요 업적과 진행도를 인라인으로 보여준다."""
    listing = pg.research_status(ctx.db, user_id=user_id,
                                 content_version_id=ctx.content_version_id)
    account = ctx.db.one("SELECT wildcards FROM accounts WHERE user_id = ?",
                         (user_id,))
    lines = [f"**연구** — {_coin_line(ctx, user_id)} · "
             f"와일드카드 {account['wildcards']}"]
    available: list[dict] = []
    render_rows: list[dict] = []
    for entry in listing:
        completed = entry["steps_taken"] >= entry["max_steps"]
        step = (f" [{entry['steps_taken']}/{entry['max_steps']}]"
                if entry["max_steps"] > 1 else "")
        render_rows.append({**entry, "completed": completed})
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
    return {**_reply("\n".join(lines), components),
            "attachments": visuals.research(render_rows)}


def hub_shop_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 상점` — 허브 상점은 전투 루프 밖이므로 코인 결제가 허용된다
    (§7.2)."""
    listing = pg.hub_shop_listing(ctx.db, ctx.balance,
                                  content_version_id=ctx.content_version_id)
    lines = [f"**허브 상점** — {_coin_line(ctx, user_id)}", "장비"]
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
    return {**_reply("\n".join(lines), components),
            "attachments": visuals.hub_shop(
                listing["equipment"], listing["stones"],
                currency=coin_balance(ctx, user_id))}


def hub_screen(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃` → 허브 화면, a public reply in the Deckout channel.

    Re-issuing it while a run is active re-renders the current screen (§16.3);
    if the thread was deleted it is recreated at surface_generation + 1 (§16.8).
    """
    expired = _expire_and_close(ctx, user_id)

    run = lc.active_run_for(ctx.db, user_id)
    if run is not None:
        if run["thread_id"] is None:
            # §16.8 — 세대만 올리고 끝내면 스레드는 영영 돌아오지 않는다.
            thread_id = surfaces.reopen_thread(
                ctx.db, ctx.central, run["run_id"],
                parent_channel_id=settings.parent_channel_id)
            if thread_id is None:
                return _ephemeral(errors.SURFACE_UNAVAILABLE)
            return {"action": "redirect", "thread_id": thread_id,
                    "content": "런 스레드를 다시 만들었습니다."}
        # §16.3 second start attempt → redirect to the existing run's thread.
        return {"action": "redirect", "thread_id": run["thread_id"],
                "content": errors.RUN_ALREADY_ACTIVE}

    account = ctx.db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    note = None
    lines = []
    if expired is not None:
        kept = len(expired.get("inventory", {}).get("kept", []))
        note = f"오래 조작이 없어 이전 런을 정리했습니다. 보관 {kept}개."
        lines.append(note)
    lines += [
        "**덱아웃**",
        f"{_coin_line(ctx, user_id)} · 카르타 {account['carta']} · "
        f"와일드카드 {account['wildcards']}",
        f"파티 슬롯 {account['party_slots']} · 패시브 슬롯 {account['passive_slots']}",
    ]
    if account["tutorial_completed_at"] is None:
        lines.append("튜토리얼이 아직 남아 있습니다. `!덱아웃 시작`")

    daily = att.status(ctx.db, ctx.balance, user_id=user_id)
    components = []
    if daily["claimable"]:
        reward = daily["reward"]
        lines.append(
            f"출석 {daily['streak']}일째 — 받을 것: 코인 {reward['coin']} · "
            f"카르타 {reward['carta']}")
        components.append({
            "type": "button", "custom_id": f"{hub.HUB_PREFIX}daily",
            "label": "출석 보상 받기",
        })
    else:
        lines.append(f"출석 {daily['streak']}일째 — 오늘 것은 받았습니다.")

    screen = _reply("\n".join(lines))
    if components:
        screen["components"] = components
    screen["attachments"] = visuals.hub(
        dict(account), coin=coin_balance(ctx, user_id), daily=daily, note=note)
    return screen


def start_run(ctx: HandlerContext, user_id: int) -> dict:
    """`!덱아웃 시작` → 준비 화면 (§16.2.2), ephemeral in the channel.

    NO run row and NO thread exist yet: steps 1-4 are pure UI, so abandoning
    them costs nothing and creates no `one_active_run` conflict.
    """
    # 방치된 런이 §16.3 규칙으로 계정을 막고 있을 수 있다. 새 런을 거절하기
    # **전에** 확인한다 — 그러지 않으면 만료 규칙이 있으나 마나가 된다.
    _expire_and_close(ctx, user_id)
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
    summary = f"런을 포기했습니다. 보관 {kept}개 · 소실 {lost}개"
    # 포기는 채널에서 하고 화면은 스레드에 있다. 여기서 고쳐 쓰지 않으면
    # 스레드에는 지도와 살아 있는 버튼이 그대로 남는다 (§1.3.6).
    surfaces.close_run_surface(ctx.db, ctx.central, run["run_id"],
                               summary=summary)
    return _reply(summary)


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
    return {**_reply("\n".join(lines)), "attachments": visuals.achievements(listing)}


# =====================================================================
# Component interactions
# =====================================================================
def handle_interaction(ctx: HandlerContext, event: ev.InteractionEvent) -> dict:
    # 준비·뽑기 화면은 런 밖에서 동작하므로 §19.2의 custom_id 형식(run_id를
    # 요구한다)을 쓸 수 없다. 각자의 접두사로 먼저 갈라낸다.
    if event.custom_id.startswith(REGISTER_PREFIX):
        return handle_register(ctx, event.user_id)
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
            "components": _screen_controls(ctx, gate.run, result, conclusion),
            "attachments": _screen_art(ctx, gate.run, result, conclusion)}


def _screen_controls(ctx: HandlerContext, run, result: dict,
                     conclusion: dict | None = None) -> list[dict]:
    """이 화면에서 누를 수 있는 것 (§19.2).

    그림(`_screen_art`)과 짝을 이룬다. 다만 그림은 없어도 화면을 볼 수 있고,
    컨트롤은 없으면 아무것도 할 수 없다.

    전투가 끝나 정산으로 넘어간 경우에는 누를 것이 없다 — 런이 끝났다.
    """
    effective = _effective_screen(result, conclusion)
    if effective.get("screen") == "settlement":
        return []
    if effective.get("screen") == "battle" and effective.get("battle_id"):
        engine = bt.build_engine(
            ctx.db, ctx.balance, battle_id=int(effective["battle_id"]),
            run_id=run["run_id"], content_version_id=run["content_version_id"],
            rng=_rng(ctx, run))
        return controls.battle(ctx.db, run["run_id"], engine)
    return controls.for_screen(ctx.db, run["run_id"], effective)


def _effective_screen(result: dict, conclusion: dict | None) -> dict:
    """지금 플레이어 앞에 있는 화면.

    전투가 끝나면 `conclude_battle` 이 다음 화면을 열어 준다 — 지도일 수도,
    정산일 수도, §3.4.1 튜토리얼 재도전이 다시 연 전투나 이벤트일 수도 있다.
    그때도 방금 끝난 전투(`result`)를 기준으로 화면을 그리면, 이미 끝난 판을
    그리고 그 위에는 누를 것이 하나도 나오지 않는다 — 런이 그대로 굳는다.

    결론이 화면을 열었으면 **그쪽이 지금 화면이다.**
    """
    if conclusion is not None and conclusion.get("screen"):
        return conclusion
    return result


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
    effective = _effective_screen(result, conclusion)
    screen = effective.get("screen")
    if screen == "battle" and effective.get("battle_id"):
        return visuals.battle(ctx.db, ctx.balance,
                              battle_id=int(effective["battle_id"]), run=fresh)
    if screen == "shop":
        return visuals.shop(ctx.db, fresh, effective.get("items", []))
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
    conclusion = None
    # §3.4.1 — 튜토리얼 패배는 같은 칸을 **다시 연다.** 그렇게 새로 열린 전투도
    # 첫 플레이어 결정까지 몰아 주지 않으면 적이 턴을 잡은 채로 멈추고, 화면에는
    # 낼 수 있는 카드가 없어 누를 것이 하나도 남지 않는다 — 런이 그대로 굳는다.
    # 연달아 지는 경우가 있으므로 몇 번까지만 이어 간다.
    for _ in range(5):
        engine = bt.build_engine(
            ctx.db, ctx.balance, battle_id=battle_id, run_id=run["run_id"],
            content_version_id=run["content_version_id"], rng=_rng(ctx, run))
        results = engine.advance()
        if not (results and results[-1].battle_ended):
            return conclusion
        conclusion = nodes.conclude_battle(
            ctx.db, ctx.balance, _rng(ctx, run), run_id=run["run_id"],
            battle_id=battle_id)
        if conclusion.get("screen") != "battle" or not conclusion.get("battle_id"):
            return conclusion
        battle_id = conclusion["battle_id"]
    return conclusion


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
    # 수령이 끝나면 런은 지도로 돌아간다 (§3.2). 지도 버튼을 다시 붙이지
    # 않으면 그 자리에서 더 갈 곳이 없어진다.
    return {"action": "edit", "content": f"{card_id} 카드를 받았습니다.",
            "components": controls.game_map(ctx.db, parsed.run_id),
            "attachments": visuals.game_map(ctx.db, gate.run)}


def _on_shop_buy(ctx: HandlerContext, event: ev.InteractionEvent,
                 parsed: cid.CustomId) -> dict:
    """상점 화면 → item buttons. A purchase is a SELF-LOOP (§16.2)."""
    gate = check_gates(ctx.db, user_id=event.user_id, custom_id=parsed,
                       allowed_states={lc.SHOP})
    # 진열이 4~6줄이라 버튼(5개 한도)보다 선택 컴포넌트가 맞고, 그러면 값은
    # `values[0]` 로 온다. 카드 선택·보상 수령은 이미 둘 다 받는데 여기만
    # payload 만 읽어서, 선택으로 산 물건이 ValueError 로 터졌다 — 그러면
    # 이벤트가 기록되지 않아 중앙봇이 같은 조작을 무한히 재전송한다 (§16.7).
    raw = event.values[0] if event.values else parsed.payload
    try:
        item_index = int(raw)
    except (TypeError, ValueError) as error:
        raise GateError(errors.ILLEGAL_STATE,
                        reason=f"item index {raw!r}") from error
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
            "content": f"구매 완료 · 실버 {result['run_currency']}",
            "components": controls.shop(ctx.db, parsed.run_id, items),
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
                "components": _screen_controls(ctx, gate.run, result, conclusion),
                "attachments": _screen_art(ctx, gate.run, result, conclusion)}
    return {"action": "edit", "content": "이벤트를 해결했습니다.",
            "components": controls.game_map(ctx.db, parsed.run_id),
            "attachments": visuals.game_map(ctx.db, gate.run)}


def _render_nested_choice(ctx: HandlerContext, run, parsed: cid.CustomId,
                          result: dict) -> dict:
    """Prompt for a suspended operator's decision (§16.5).

    **어떤 연산자가 멈춘 것인지 보고 갈라야 한다.** 예전에는 무조건 저주받은
    카드 화면을 띄웠고, 그래서 `offer_reward` 로 멈춘 이벤트는 "제거할
    저주받은 카드가 없습니다" 라는 엉뚱한 답을 받고 보상이 사라졌다.
    """
    choice = ctx.db.one(
        "SELECT * FROM pending_choices WHERE run_id = ? AND status = 'open'",
        (parsed.run_id,))
    if choice is not None and choice["choice_type"] == "offer_reward":
        offer = nodes.materialize_offer_reward(
            ctx.db, ctx.balance, _rng(ctx, run), parsed.run_id, choice)
        if offer["screen"] != "reward":
            nodes.resume_pending_choice(ctx.db, parsed.run_id, selection=None)
            return {"action": "edit", "content": "받을 수 있는 카드가 없습니다."}
        return {"action": "edit", "content": "보상 카드를 고르세요.",
                "components": controls.reward(
                    ctx.db, parsed.run_id, offer["options"],
                    skip_available=offer.get("skip_available", True))}

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
    # 전투가 끝나지 않으면 아래 `if ended:` 가 실행되지 않는다. 초기화가
    # 없으면 평범한 카드 한 장에 UnboundLocalError 가 나고, 그러면 이벤트가
    # 기록되지 않아 중앙봇이 같은 조작을 무한히 재전송한다 (§16.7).
    conclusion = None
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
            conclusion = _drive_concluded_battle(ctx, run, conclusion, lines)
    run = ctx.db.one("SELECT * FROM runs WHERE run_id = ?", (parsed.run_id,))
    # 결론이 새 전투를 열었으면(튜토리얼 재도전) 화면은 **그 전투** 를 가리켜야
    # 한다. 방금 끝난 전투를 그리면 플레이어는 이미 끝난 판을 보게 된다.
    effective = _effective_screen(
        {"screen": "battle", "battle_id": engine.battle_id}, conclusion)
    battle_id = (int(effective["battle_id"])
                 if effective.get("screen") == "battle" and effective.get("battle_id")
                 else engine.battle_id)
    art = (visuals.settlement(conclusion["report"])
           if conclusion is not None and conclusion.get("screen") == "settlement"
           else visuals.battle(ctx.db, ctx.balance, battle_id=battle_id, run=run))
    return {"action": "edit", "content": "\n".join(lines),
            "components": _screen_controls(
                ctx, run, {"screen": "battle", "battle_id": battle_id},
                conclusion),
            "attachments": art}


def _drive_concluded_battle(ctx: HandlerContext, run, conclusion: dict,
                            lines: list[str]) -> dict:
    """결론이 새 전투를 열었으면 그것도 첫 플레이어 결정까지 몰아 준다.

    §3.4.1의 튜토리얼 재도전이 이 경우다. 실제로 몰아 주는 일은 `_drive_battle`
    이 하고(그쪽이 세 호출부에서 모두 쓰인다), 여기서는 그 결과를 플레이어가
    읽을 줄로 옮긴다.
    """
    if conclusion.get("screen") != "battle" or not conclusion.get("battle_id"):
        return conclusion
    deeper = _drive_battle(ctx, run, conclusion["battle_id"])
    if deeper is None:
        return conclusion
    lines.append(_conclusion_summary(deeper))
    return deeper


def _conclusion_summary(conclusion: dict) -> str:
    # §3.4.1 — 튜토리얼 패배는 런을 끝내지 않고 같은 칸을 다시 연다. 그때
    # `conclude_battle` 이 돌려주는 것은 다시 연 화면(대개 `battle`)이므로,
    # 화면 이름으로 판별하면 **패배한 플레이어에게 "승리!" 라고 말하게 된다.**
    # 실제로 그랬다.
    if conclusion.get("tutorial_retry"):
        return "패배했지만 튜토리얼은 계속됩니다. 다시 도전하세요."
    screen = conclusion["screen"]
    if screen == "settlement":
        report = conclusion["report"]
        kept = len(report.get("inventory", {}).get("kept", []))
        if conclusion.get("cleared"):
            rewards = report.get("rewards", {})
            return (f"월드 클리어! 코인 {rewards.get('coin', 0)} · "
                    f"카르타 {rewards.get('carta', 0)} · 보관 {kept}개")
        return f"런 종료 — 보관 {kept}개"
    rewards = conclusion.get("rewards", {})
    drops = len(rewards.get("drops", []))
    return f"승리! 실버 +{rewards.get('run_currency', 0)} · 획득 {drops}개"


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
            "components": controls.game_map(ctx.db, parsed.run_id),
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
