"""`!카드 ...` 커맨드 라우터 (설계 문서 §1 "!커맨드 prefix, ARI 관례").

게임 로직은 전부 game/ 아래 서비스에 있고, 이 모듈은 디스코드 문법 ↔ 서비스
호출을 잇는 얇은 계층이다.

봇 이름이 TBD(§0) 이므로 커맨드 루트는 중립적인 `!카드` 를 쓴다. 이름이
확정되면 ROOT 상수만 바꾸면 된다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import balance
from ..db.models import Card, Character, RunStatus, User, UserCard, UserCharacter, utcnow
from ..game import equipment as equipment_service
from ..game import gacha, research, run_service
from ..game import shop as shop_service
from ..game.combat import BattlePhase, CombatError
from ..game.equipment import EquipmentError
from ..game.gacha import GachaError
from ..game.research import ResearchError
from ..game.run_service import RunError
from ..game.shop import ShopError
from ..render import battle as battle_render
from ..render import card_render, map_render
from .central_client import CentralAPIError, get_central_client
from .protocol import BotResponse, EventRequest

log = logging.getLogger(__name__)

ROOT = "덱아웃"
ROOT_ALIASES = {ROOT, "카드"}  # 기존 설치의 텍스트 명령은 마이그레이션 동안 유지한다.

Handler = Callable[["CommandContext"], Awaitable[BotResponse]]
_HANDLERS: dict[str, Handler] = {}


class CommandContext:
    def __init__(self, session: Session, event: EventRequest, user: User, args: list[str]) -> None:
        self.session = session
        self.event = event
        self.user = user
        self.args = args

    def arg(self, index: int, default: str = "") -> str:
        return self.args[index] if index < len(self.args) else default

    def int_arg(self, index: int, default: int | None = None) -> int:
        raw = self.arg(index)
        if not raw:
            if default is None:
                raise ValueError("번호를 입력해주세요.")
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"`{raw}` 는 숫자가 아닙니다.") from exc


def command(*names: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        for name in names:
            _HANDLERS[name] = fn
        return fn

    return deco


# ---------------------------------------------------------------------------
# 유저 확보
# ---------------------------------------------------------------------------


def get_or_create_user(session: Session, discord_id: str, username: str = "") -> User:
    user = session.scalar(select(User).where(User.discord_id == discord_id))
    if user is None:
        user = User(discord_id=discord_id, display_name=username)
        session.add(user)
        session.flush()
    elif username and user.display_name != username:
        user.display_name = username
    return user


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


async def dispatch(session: Session, event: EventRequest, prefix: str = "!") -> BotResponse:
    content = (event.raw_content or event.content or "").strip()
    # Central message payload normally contains raw_content.  Accept the
    # command/args form too, so a command router need not reconstruct it.
    if not content and event.command:
        content = " ".join([prefix + event.command, *event.args])
    if not content.startswith(prefix):
        return BotResponse.ignored()

    parts = content[len(prefix):].split()
    if not parts or parts[0] not in ROOT_ALIASES:
        return BotResponse.ignored()

    if not event.user_id:
        return BotResponse.error("유저 정보를 확인할 수 없습니다.")

    sub = parts[1] if len(parts) > 1 else "도움말"
    args = parts[2:]

    handler = _HANDLERS.get(sub)
    if handler is None:
        return BotResponse.error(
            f"알 수 없는 명령입니다: `{sub}`\n`{prefix}{ROOT} 도움말` 을 확인해주세요."
        )

    user = get_or_create_user(session, event.user_id, event.username)
    ctx = CommandContext(session, event, user, args)

    try:
        return await handler(ctx)
    except (RunError, GachaError, ResearchError, ShopError, EquipmentError, CombatError,
            CentralAPIError, ValueError) as exc:
        return BotResponse.error(str(exc))
    except Exception:  # 예상치 못한 오류로 봇이 침묵하지 않게 한다.
        log.exception("커맨드 처리 중 오류: %s", content)
        return BotResponse.error("처리 중 오류가 발생했습니다. 관리자에게 문의해주세요.")


# ---------------------------------------------------------------------------
# 기본 커맨드
# ---------------------------------------------------------------------------


@command("도움말", "help", "?")
async def _help(ctx: CommandContext) -> BotResponse:
    lines = [
        "**📖 명령어 목록**",
        "",
        "__계정__",
        "`!카드 정보` — 보유 재화와 진행 상황",
        "`!카드 출석` — 일일 보상 수령 (카르타/코인)",
        "`!카드 캐릭터` — 보유 캐릭터 목록",
        "`!카드 성급 <캐릭터코드>` — 카드 조각으로 성급 상승",
        "",
        "__가챠__",
        "`!카드 배너` — 배너 목록",
        "`!카드 뽑기 [배너코드] [횟수]` — 1회 또는 10연",
        "",
        "__런(탐험)__",
        "`!카드 시작 <캐릭터코드...>` — 새 런 시작",
        "`!카드 맵` — 현재 맵",
        "`!카드 이동 <번호>` — 다음 노드 선택",
        "`!카드 상태` — 전투 화면 다시 보기",
        "`!카드 사용 <카드번호> [대상번호]` — 카드 사용",
        "`!카드 넘기기` — 이번 턴 넘기기",
        "`!카드 선택 <번호>` — 보상 카드 선택",
        "`!카드 구매 <번호>` — 노드 상점 구매",
        "`!카드 포기` — 진행 중인 런 포기",
        "",
        "__허브__",
        "`!카드 상점` / `!카드 상점구매 <번호>` — 허브 상점(장비)",
        "`!카드 장비` — 보유 장비 / `!카드 장착 <장비코드> <캐릭터코드>` / `!카드 강화 <장비코드>`",
        "`!카드 연구` / `!카드 연구해금 <코드>` — 연구 시스템",
    ]
    return BotResponse.text("\n".join(lines))


@command("정보", "프로필")
async def _profile(ctx: CommandContext) -> BotResponse:
    user = ctx.user
    try:
        central = await get_central_client().get_user(user.discord_id)
        coin_line = f"🪙 코인 **{central.balance:,}** (Lv.{central.level})"
    except CentralAPIError:
        coin_line = "🪙 코인 — 중앙봇 조회 실패"

    owned = ctx.session.scalars(
        select(UserCharacter).where(UserCharacter.user_id == user.id)
    ).all()
    unlocked = ctx.session.scalars(select(UserCard).where(UserCard.user_id == user.id)).all()
    bonuses = research.account_bonuses(ctx.session, user)
    run = run_service.active_run(ctx.session, user)

    lines = [
        f"**{user.display_name or user.discord_id} 님의 정보**",
        coin_line,
        f"🎴 카르타 **{user.carta:,}**",
        f"🧩 카드 조각 **{user.fragments:,}**  ·  🃏 와일드카드 **{user.wildcards:,}**",
        f"🛠️ {balance.EQUIP_MATERIAL_DISPLAY_NAME} **{user.equip_material:,}**",
        "",
        f"보유 캐릭터 **{len(owned)}** · 해금 카드 **{len(unlocked)}**",
        f"파티 슬롯 **{bonuses.party_slots}/{balance.MAX_PARTY_SLOTS}** · "
        f"패시브 슬롯 **{bonuses.passive_slots}/{balance.MAX_PASSIVE_SLOTS}**",
    ]
    if run:
        game_map = run_service.get_map(run)
        floor = game_map.nodes[run.current_node_id].floor + 1 if run.current_node_id else 0
        lines.append(f"🗺️ 진행 중인 런: {floor}F / {len(game_map.floors)}F")
    else:
        lines.append("🗺️ 진행 중인 런 없음 — `!카드 시작` 으로 시작하세요.")

    return BotResponse.text("\n".join(lines))


@command("출석", "일일")
async def _daily(ctx: CommandContext) -> BotResponse:
    """§5.5 카르타 획득처: 일일/출석 보상."""
    user = ctx.user
    now = utcnow()
    if user.last_daily_at is not None:
        last = user.last_daily_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=now.tzinfo)
        if now - last < timedelta(hours=20):  # TBD: 리셋 주기
            remain = timedelta(hours=20) - (now - last)
            hours = int(remain.total_seconds() // 3600)
            minutes = int((remain.total_seconds() % 3600) // 60)
            return BotResponse.error(f"다음 출석까지 {hours}시간 {minutes}분 남았습니다.")

    user.last_daily_at = now
    user.carta += balance.DAILY_CARTA

    coin_line = ""
    try:
        await get_central_client().add_currency(
            user.discord_id, balance.DAILY_COIN,
            idempotency_key=f"deckout:daily:{user.discord_id}:{now.date().isoformat()}", reason="daily"
        )
        coin_line = f" · 🪙 코인 +{balance.DAILY_COIN:,}"
    except CentralAPIError:
        coin_line = " · 🪙 코인 지급 실패 (중앙봇 연결 확인 필요)"

    return BotResponse.text(f"✅ 출석 완료! 🎴 카르타 +{balance.DAILY_CARTA:,}{coin_line}")


# ---------------------------------------------------------------------------
# 캐릭터 / 가챠
# ---------------------------------------------------------------------------


@command("캐릭터", "보유")
async def _characters(ctx: CommandContext) -> BotResponse:
    rows = ctx.session.scalars(
        select(UserCharacter).where(UserCharacter.user_id == ctx.user.id)
    ).all()
    if not rows:
        return BotResponse.text("보유한 캐릭터가 없습니다. `!카드 뽑기` 로 캐릭터를 획득하세요.")

    codes = [r.character_code for r in rows]
    masters = {
        c.code: c
        for c in ctx.session.scalars(select(Character).where(Character.code.in_(codes))).all()
    }
    lines = ["**보유 캐릭터**"]
    for row in sorted(rows, key=lambda r: -r.star):
        master = masters.get(row.character_code)
        if master is None:
            continue
        cap = min(master.max_star, balance.SPECIAL_MAX_STAR)
        lines.append(
            f"`{master.code}` **{master.name}** {'★' * row.star}{'☆' * (cap - row.star)} "
            f"(HP {master.base_hp} / ATK {master.base_attack})"
        )
    lines.append("")
    lines.append("`!카드 시작 <캐릭터코드...>` 로 런을 시작할 수 있습니다.")
    return BotResponse.text("\n".join(lines))


@command("성급", "성급상승")
async def _star_up(ctx: CommandContext) -> BotResponse:
    code = ctx.arg(0)
    if not code:
        return BotResponse.error("`!카드 성급 <캐릭터코드>` 형식으로 입력해주세요.")
    result = gacha.star_up(ctx.session, ctx.user, code)
    return BotResponse.text(
        f"⭐ **{result['name']}** {result['star']}성 달성! (카드 조각 -{result['cost']})"
    )


@command("배너")
async def _banners(ctx: CommandContext) -> BotResponse:
    from ..db.models import Banner

    banners = ctx.session.scalars(select(Banner).where(Banner.is_active.is_(True))).all()
    if not banners:
        return BotResponse.text("활성화된 배너가 없습니다.")

    lines = ["**🎴 가챠 배너**"]
    for b in banners:
        kind = "한정 픽업" if b.type.value == "limited" else "상시"
        lines.append(f"`{b.code}` **{b.name}** — {kind}")
        if b.pickup_characters or b.pickup_cards:
            picks = ", ".join([*b.pickup_characters, *b.pickup_cards])
            lines.append(f"　 ⭐ 픽업: {picks}")
    lines.append("")
    lines.append(
        f"1회 {balance.PULL_COST_CARTA} 카르타 · "
        f"{balance.MULTI_PULL_COUNT}연 {balance.MULTI_PULL_COST_CARTA} 카르타"
    )
    return BotResponse.text("\n".join(lines))


@command("뽑기", "가챠")
async def _pull(ctx: CommandContext) -> BotResponse:
    banner_code: str | None = None
    count = 1
    for raw in ctx.args:
        if raw.isdigit():
            count = int(raw)
        else:
            banner_code = raw

    if count not in (1, balance.MULTI_PULL_COUNT):
        return BotResponse.error(f"1회 또는 {balance.MULTI_PULL_COUNT}연만 가능합니다.")

    results, cost = gacha.pull(ctx.session, ctx.user, banner_code, count)

    lines = [f"**🎴 가챠 결과** (카르타 -{cost:,} → 잔여 {ctx.user.carta:,})", ""]
    lines.extend(r.label for r in results)

    frags = sum(r.fragments_gained for r in results)
    wilds = sum(r.wildcards_gained for r in results)
    if frags or wilds:
        parts = []
        if frags:
            parts.append(f"🧩 카드 조각 +{frags}")
        if wilds:
            parts.append(f"🃏 와일드카드 +{wilds}")
        lines.append("")
        lines.append("중복 변환: " + " · ".join(parts))

    new_cards = [r for r in results if r.is_new and r.category in ("card", "passive")]
    if new_cards:
        lines.append("")
        lines.append("🔓 새로 해금된 카드는 이후 런의 보상 노드에서 선택할 수 있습니다.")

    # 결과 이미지
    items = []
    card_codes = [r.code for r in results if r.category == "card"]
    arts = {
        c.code: c
        for c in ctx.session.scalars(select(Card).where(Card.code.in_(card_codes))).all()
    } if card_codes else {}
    for r in results:
        master = arts.get(r.code)
        items.append(
            {
                "name": r.name,
                "code": r.code,
                "kind": master.kind.value if master else r.category,
                "cost": master.cost if master else None,
                "rarity": r.tier,
                "description": ("신규 획득" if r.is_new else "중복 → 재화 변환"),
                "art_path": master.art_path if master else None,
                "badge": "PICKUP" if r.is_pickup else ("NEW" if r.is_new else None),
            }
        )

    response = BotResponse.text("\n".join(lines))
    return response.with_image("gacha.png", card_render.render_card_sheet(items, "가챠 결과"))


# ---------------------------------------------------------------------------
# 런 진행
# ---------------------------------------------------------------------------


def _map_image(run) -> bytes:
    game_map = run_service.get_map(run)
    available = [n.id for n in run_service.available_nodes(run)]
    return map_render.render_map(
        game_map, run.current_node_id, run.cleared_node_ids, available, party=run.party
    )


def _node_choice_lines(run) -> list[str]:
    nodes = run_service.available_nodes(run)
    if not nodes:
        return []
    label = {
        "combat": "⚔️ 전투", "reward": "🎁 보상", "rest": "🔥 휴식",
        "shop": "🏪 상점", "event": "❓ 이벤트", "boss": "👑 보스",
    }
    lines = ["", "**다음 목적지** (`!카드 이동 <번호>`)"]
    for i, node in enumerate(nodes):
        lines.append(f"`{i + 1}` {label.get(node.type, node.type)} — {node.floor + 1}F")
    return lines


@command("시작")
async def _start(ctx: CommandContext) -> BotResponse:
    codes = ctx.args
    if not codes:
        return BotResponse.error(
            "`!카드 시작 <캐릭터코드...>` 형식으로 파티를 지정해주세요.\n"
            "보유 캐릭터는 `!카드 캐릭터` 로 확인할 수 있습니다."
        )

    run = run_service.start_run(ctx.session, ctx.user, codes)
    party = " · ".join(f"{m['name']}({'★' * m['star']})" for m in run.party)

    lines = [
        "**🗺️ 새로운 런을 시작합니다!**",
        f"파티: {party}",
        "",
        "패배하면 런 전체가 초기화됩니다. HP는 노드를 넘어 유지되니 휴식 노드를 잘 활용하세요.",
    ]
    lines.extend(_node_choice_lines(run))
    return BotResponse.text("\n".join(lines)).with_image("map.png", _map_image(run))


@command("맵", "지도")
async def _map(ctx: CommandContext) -> BotResponse:
    run = run_service.require_run(ctx.session, ctx.user)
    game_map = run_service.get_map(run)
    floor = game_map.nodes[run.current_node_id].floor + 1 if run.current_node_id else 0

    lines = [f"**🗺️ 탐험 맵** — {floor}F / {len(game_map.floors)}F"]
    hp = " · ".join(f"{m['name']} {m['hp']}/{m['max_hp']}" for m in run.party)
    lines.append(f"파티 HP: {hp}")
    if run.battle:
        lines.append("")
        lines.append("⚔️ 전투가 진행 중입니다. `!카드 상태` 로 확인하세요.")
    lines.extend(_node_choice_lines(run))
    return BotResponse.text("\n".join(lines)).with_image("map.png", _map_image(run))


@command("이동")
async def _move(ctx: CommandContext) -> BotResponse:
    run = run_service.require_run(ctx.session, ctx.user)
    nodes = run_service.available_nodes(run)
    if not nodes:
        if run.battle:
            return BotResponse.error("전투를 먼저 끝내야 합니다. `!카드 상태`")
        return BotResponse.error("현재 노드를 먼저 해결해야 합니다.")

    choice = ctx.int_arg(0)
    if not (1 <= choice <= len(nodes)):
        return BotResponse.error(f"1~{len(nodes)} 사이의 번호를 골라주세요.")

    outcome = run_service.enter_node(ctx.session, ctx.user, run, nodes[choice - 1].id)
    return await _render_outcome(ctx, run, outcome)


async def _render_outcome(ctx: CommandContext, run, outcome: run_service.NodeOutcome) -> BotResponse:
    lines = [outcome.message] if outcome.message else []
    lines.extend(outcome.lines)

    if outcome.kind == "battle" and run.battle:
        engine = run_service.load_battle(run)
        lines.append("")
        if engine.phase is BattlePhase.AWAITING_INPUT and engine.pending:
            actor = engine.active_unit
            if engine.pending.drawn:
                lines.append(f"▶ **{actor.name}** 의 턴 — `!카드 사용 <번호> [대상번호]`")
            else:
                lines.append(f"▶ **{actor.name}** — 뽑을 카드가 없습니다. `!카드 넘기기`")
        response = BotResponse.text("\n".join(lines))
        return response.with_image("battle.png", battle_render.render_battle(engine, outcome.message))

    if outcome.kind in ("reward", "shop") and outcome.choices:
        lines.append("")
        verb = "선택" if outcome.kind == "reward" else "구매"
        for item in outcome.choices:
            price = f" — {item['price']:,}코인" if "price" in item else ""
            desc = item.get("description") or ""
            lines.append(f"`{item['index'] if 'index' in item else item.get('index', 0)}` "
                         f"**{item['name']}**{price}")
            if desc:
                lines.append(f"　 {desc}")
        lines.append("")
        lines.append(f"`!카드 {verb} <번호>` 로 고르세요.")
        return BotResponse.text("\n".join(lines))

    # 해결이 끝난 노드 — 다음 이동지를 함께 보여준다.
    lines.extend(_node_choice_lines(run))
    response = BotResponse.text("\n".join(lines))
    if run.status == RunStatus.ACTIVE:
        return response.with_image("map.png", _map_image(run))
    return response


@command("상태", "전투")
async def _status(ctx: CommandContext) -> BotResponse:
    run = run_service.require_run(ctx.session, ctx.user)
    if not run.battle:
        return BotResponse.error("진행 중인 전투가 없습니다. `!카드 맵`")

    engine = run_service.load_battle(run)
    lines = [f"**⚔️ 전투 중** — 라운드 {engine.round_number} · 자원 {engine.resource}/{engine.resource_max}"]
    lines.extend(engine.log[-8:])
    if engine.phase is BattlePhase.AWAITING_INPUT and engine.active_unit:
        lines.append("")
        lines.append(f"▶ **{engine.active_unit.name}** 의 턴 — `!카드 사용 <번호> [대상번호]`")

    response = BotResponse.text("\n".join(lines))
    return response.with_image("battle.png", battle_render.render_battle(engine, "전투"))


@command("사용", "카드사용")
async def _play(ctx: CommandContext) -> BotResponse:
    run = run_service.require_run(ctx.session, ctx.user)
    engine = run_service.load_battle(run)

    choice = ctx.int_arg(0)
    target_uid = None
    if len(ctx.args) > 1:
        target_index = ctx.int_arg(1)
        enemies = engine.enemies
        if 1 <= target_index <= len(enemies):
            target_uid = enemies[target_index - 1].uid
        else:
            return BotResponse.error(f"대상 번호는 1~{len(enemies)} 입니다.")

    engine.play(choice, target_uid)
    outcome = run_service.resolve_battle_state(ctx.session, ctx.user, run, engine)
    await _grant_battle_rewards(ctx, run, outcome)
    return await _render_outcome(ctx, run, outcome)


@command("넘기기", "패스")
async def _skip(ctx: CommandContext) -> BotResponse:
    run = run_service.require_run(ctx.session, ctx.user)
    engine = run_service.load_battle(run)
    engine.skip()
    outcome = run_service.resolve_battle_state(ctx.session, ctx.user, run, engine)
    await _grant_battle_rewards(ctx, run, outcome)
    return await _render_outcome(ctx, run, outcome)


async def _grant_battle_rewards(ctx: CommandContext, run, outcome: run_service.NodeOutcome) -> None:
    """전투 승리 시 코인을 중앙봇에 반영한다 (§1.1)."""
    if outcome.kind not in ("battle", "cleared"):
        return
    if run.battle:  # 아직 전투 중
        return
    if outcome.kind == "battle" and "승리" not in outcome.message:
        return

    amount = run_service.battle_reward_coin(run)
    if amount <= 0:
        return
    try:
        await get_central_client().add_currency(
            ctx.user.discord_id, amount,
            idempotency_key=f"deckout:battle:{run.id}:{run.current_node_id}", reason="battle_clear"
        )
    except CentralAPIError:
        log.warning("전투 보상 코인 지급 실패: user=%s", ctx.user.discord_id)


@command("선택")
async def _take_reward(ctx: CommandContext) -> BotResponse:
    run = run_service.require_run(ctx.session, ctx.user)
    message = run_service.take_reward(ctx.session, ctx.user, run, ctx.int_arg(0))
    lines = [message]
    lines.extend(_node_choice_lines(run))
    return BotResponse.text("\n".join(lines)).with_image("map.png", _map_image(run))


@command("구매")
async def _buy_node(ctx: CommandContext) -> BotResponse:
    run = run_service.require_run(ctx.session, ctx.user)
    message = await shop_service.buy_node_item(ctx.session, ctx.user, run, ctx.int_arg(0))

    stock = run.shop_state.get(run.current_node_id or "", {})
    lines = [message, ""]
    for item in stock.get("items", []):
        sold = item["index"] in stock.get("sold", [])
        mark = "~~" if sold else ""
        lines.append(f"`{item['index']}` {mark}**{item['name']}** — {item['price']:,}코인{mark}")
    lines.append("")
    lines.append("`!카드 이동 <번호>` 로 다음 노드로 이동할 수 있습니다.")

    # 상점 노드는 물건을 사지 않아도 지나갈 수 있어야 한다.
    game_map = run_service.get_map(run)
    node = game_map.nodes.get(run.current_node_id or "")
    if node is not None:
        run_service._mark_cleared(run, node)
    lines.extend(_node_choice_lines(run))
    return BotResponse.text("\n".join(lines))


@command("포기")
async def _abandon(ctx: CommandContext) -> BotResponse:
    run = run_service.require_run(ctx.session, ctx.user)
    run_service.abandon_run(ctx.session, run)
    return BotResponse.text("🏳️ 런을 포기했습니다. `!카드 시작` 으로 새로 시작할 수 있습니다.")


# ---------------------------------------------------------------------------
# 허브 (§7.2, §8, §9)
# ---------------------------------------------------------------------------


@command("상점")
async def _hub_shop(ctx: CommandContext) -> BotResponse:
    listings = shop_service.hub_listings(ctx.session)
    if not listings:
        return BotResponse.text("허브 상점에 등록된 상품이 없습니다.")

    lines = ["**🏬 허브 상점** — 영구 아이템"]
    for item in listings:
        price = []
        if item["price_coin"]:
            price.append(f"🪙 {item['price_coin']:,}")
        if item["price_carta"]:
            price.append(f"🎴 {item['price_carta']:,}")
        lines.append(f"`{item['index']}` **{item['name']}** — {' / '.join(price) or '무료'}")
        if item["description"]:
            lines.append(f"　 {item['description']}")
    lines.append("")
    lines.append("`!카드 상점구매 <번호>`")
    return BotResponse.text("\n".join(lines))


@command("상점구매")
async def _hub_buy(ctx: CommandContext) -> BotResponse:
    message = await shop_service.buy_hub_item(ctx.session, ctx.user, ctx.int_arg(0))
    return BotResponse.text(message)


@command("장비")
async def _equipment(ctx: CommandContext) -> BotResponse:
    from ..db.models import Equipment, UserEquipment

    rows = ctx.session.scalars(
        select(UserEquipment).where(UserEquipment.user_id == ctx.user.id)
    ).all()
    if not rows:
        return BotResponse.text("보유한 장비가 없습니다. `!카드 상점` 에서 구매할 수 있습니다.")

    codes = [r.equipment_code for r in rows]
    masters = {
        e.code: e
        for e in ctx.session.scalars(select(Equipment).where(Equipment.code.in_(codes))).all()
    }
    lines = ["**🛠️ 보유 장비**"]
    for row in rows:
        master = masters.get(row.equipment_code)
        if master is None:
            continue
        where = f"→ {row.equipped_on}" if row.equipped_on else "(미장착)"
        stats = " ".join(f"{k}+{v * row.tier}" for k, v in (master.base_stats or {}).items())
        lines.append(f"`{master.code}` **{master.name}** T{row.tier} {where} — {stats}")
    lines.append("")
    lines.append("`!카드 장착 <장비코드> <캐릭터코드>` · `!카드 강화 <장비코드>`")
    return BotResponse.text("\n".join(lines))


@command("장착")
async def _equip(ctx: CommandContext) -> BotResponse:
    equip_code, char_code = ctx.arg(0), ctx.arg(1)
    if not equip_code:
        return BotResponse.error("`!카드 장착 <장비코드> <캐릭터코드>` 형식으로 입력해주세요.")
    equipment_service.equip(ctx.session, ctx.user, equip_code, char_code or None)
    if char_code:
        return BotResponse.text(f"🛠️ `{equip_code}` 를 `{char_code}` 에게 장착했습니다.")
    return BotResponse.text(f"🛠️ `{equip_code}` 장착을 해제했습니다.")


@command("강화")
async def _tier_up(ctx: CommandContext) -> BotResponse:
    code = ctx.arg(0)
    if not code:
        return BotResponse.error("`!카드 강화 <장비코드>` 형식으로 입력해주세요.")
    result = equipment_service.tier_up(ctx.session, ctx.user, code)
    return BotResponse.text(
        f"⬆️ **{result['name']}** 티어 {result['tier']} 달성! "
        f"({balance.EQUIP_MATERIAL_DISPLAY_NAME} -{result['cost']})"
    )


@command("연구")
async def _research_list(ctx: CommandContext) -> BotResponse:
    nodes = research.list_nodes(ctx.session, ctx.user)
    if not nodes:
        return BotResponse.text("등록된 연구 노드가 없습니다.")

    lines = ["**🔬 연구 시스템** — 즉시 해금 (대기 시간 없음)"]
    for node in nodes:
        status = f"Lv.{node['level']}/{node['max_level']}"
        if node["maxed"]:
            lines.append(f"✅ `{node['code']}` **{node['name']}** — {status} 완료")
            continue
        if node["locked_by"]:
            lines.append(f"🔒 `{node['code']}` **{node['name']}** — 선행: {', '.join(node['locked_by'])}")
            continue
        cost = node["next_cost"]
        parts = []
        if cost["coin"]:
            parts.append(f"🪙 {cost['coin']:,}")
        if cost["fragment"]:
            parts.append(f"🧩 {cost['fragment']:,}")
        if cost["wildcard"]:
            parts.append(f"🃏 {cost['wildcard']:,}")
        lines.append(f"▫️ `{node['code']}` **{node['name']}** — {status} · 비용 {' / '.join(parts)}")
        if node["description"]:
            lines.append(f"　 {node['description']}")
    lines.append("")
    lines.append("`!카드 연구해금 <코드>`")
    return BotResponse.text("\n".join(lines))


@command("연구해금")
async def _research_unlock(ctx: CommandContext) -> BotResponse:
    code = ctx.arg(0)
    if not code:
        return BotResponse.error("`!카드 연구해금 <코드>` 형식으로 입력해주세요.")
    result = await research.unlock(ctx.session, ctx.user, code)
    return BotResponse.text(f"🔬 **{result['name']}** Lv.{result['level']} 해금 완료!")
