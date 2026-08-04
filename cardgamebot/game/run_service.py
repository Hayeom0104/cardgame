"""런(탐험) 오케스트레이션 (설계 문서 §3, §4, §12).

한 런의 전 생애주기를 담당한다:

* 런 시작 — 파티 편성, 맵 생성, 런 스코프 덱/패시브 구성
* 노드 이동 — 플레이어가 분기를 직접 선택 (§3)
* 노드 해결 — 전투/보상/휴식/상점/이벤트/보스 (§3.1)
* 전투 연결 — CombatEngine 스냅샷 저장/복원
* 패배 처리 — §3 "패배 → 런 전체 리셋" (부분 재시도 없음)

§12 의 영속/런 스코프 구분을 그대로 지킨다. HP 는 런 안에서 노드를 넘어
유지되고, 런이 끝나면 사라진다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import balance
from ..db.models import (
    Card,
    Character,
    Enemy,
    EnemyTier,
    NodeType,
    Run,
    RunStatus,
    User,
    UserCard,
    UserCharacter,
    utcnow,
)
from . import equipment as equipment_service
from . import research
from .combat import BattlePhase, CombatEngine
from .entities import CardView, Side, Unit
from .mapgen import GameMap, MapNode, generate_map


class RunError(Exception):
    pass


@dataclass
class NodeOutcome:
    """노드 진입 결과. 커맨드 계층이 이걸 보고 응답을 만든다."""

    kind: str                 # "battle" | "reward" | "rest" | "shop" | "event" | "cleared"
    message: str
    lines: list[str]
    choices: list[dict]


# ---------------------------------------------------------------------------
# 런 조회
# ---------------------------------------------------------------------------


def active_run(session: Session, user: User) -> Run | None:
    return session.scalar(
        select(Run).where(Run.user_id == user.id, Run.status == RunStatus.ACTIVE)
    )


def require_run(session: Session, user: User) -> Run:
    run = active_run(session, user)
    if run is None:
        raise RunError("진행 중인 런이 없습니다. `!카드 시작` 으로 새 런을 시작하세요.")
    return run


def get_map(run: Run) -> GameMap:
    return GameMap.from_dict(run.map_data)


# ---------------------------------------------------------------------------
# 파티 구성
# ---------------------------------------------------------------------------


def _character_stats(
    session: Session, user: User, character: Character, star: int, bonuses: research.AccountBonuses
) -> dict[str, int]:
    """기본 스탯 → 성급 → 연구 → 장비 순으로 누적한다."""
    star_mult = 1.0 + (star - 1) * balance.STAR_STAT_MULTIPLIER
    equip = equipment_service.stat_bonus(session, user, character.code)

    def compute(stat: str, base: int) -> int:
        value = base * star_mult * bonuses.stat_multiplier(stat)
        return int(value) + int(equip.get(stat, 0))

    return {
        "hp": max(1, compute("hp", character.base_hp)),
        "attack": compute("attack", character.base_attack),
        "defense": compute("defense", character.base_defense),
        "speed": max(1, compute("speed", character.base_speed)),
    }


def _starter_deck(session: Session, character_code: str) -> list[str]:
    """§4.3 가챠 0회여도 캐릭터는 항상 플레이 가능해야 한다.

    기본 카드(평타 + 기본 방어)를 캐릭터 전용 + 공용에서 모아 복사본을 만든다.
    """
    cards = session.scalars(
        select(Card).where(
            Card.is_starter.is_(True),
            Card.is_active.is_(True),
            (Card.character_code == character_code) | (Card.character_code.is_(None)),
        )
    ).all()
    deck: list[str] = []
    for card in cards:
        deck.extend([card.code] * balance.STARTER_DECK_COPIES)
    return deck


def build_party(
    session: Session, user: User, character_codes: list[str]
) -> tuple[list[dict], dict[str, list[str]]]:
    """파티 스냅샷과 런 스코프 덱을 만든다."""
    bonuses = research.account_bonuses(session, user)
    slots = bonuses.party_slots

    if not character_codes:
        raise RunError("파티에 넣을 캐릭터를 지정해주세요.")
    if len(character_codes) > slots:
        raise RunError(
            f"현재 파티 슬롯은 {slots}칸입니다. (연구 시스템에서 확장할 수 있습니다)"
        )

    owned = {
        uc.character_code: uc
        for uc in session.scalars(
            select(UserCharacter).where(UserCharacter.user_id == user.id)
        ).all()
    }

    party: list[dict] = []
    decks: dict[str, list[str]] = {}
    for slot, code in enumerate(character_codes):
        if code not in owned:
            raise RunError(f"`{code}` 는 보유하지 않은 캐릭터입니다.")
        character = session.scalar(select(Character).where(Character.code == code))
        if character is None or not character.is_active:
            raise RunError(f"`{code}` 캐릭터 데이터를 찾을 수 없습니다.")

        star = owned[code].star
        stats = _character_stats(session, user, character, star, bonuses)
        party.append(
            {
                "uid": f"p{slot}",
                "code": code,
                "name": character.name,
                "star": star,
                "hp": stats["hp"],
                "max_hp": stats["hp"],
                "attack": stats["attack"],
                "defense": stats["defense"],
                "speed": stats["speed"],
                "sprite_path": character.portrait_path,
            }
        )
        decks[code] = _starter_deck(session, code)

    return party, decks


# ---------------------------------------------------------------------------
# 런 시작 / 종료
# ---------------------------------------------------------------------------


def start_run(
    session: Session, user: User, character_codes: list[str], seed: int | None = None
) -> Run:
    if active_run(session, user) is not None:
        raise RunError("이미 진행 중인 런이 있습니다. `!카드 포기` 로 정리할 수 있습니다.")

    party, decks = build_party(session, user, character_codes)
    seed = seed if seed is not None else random.randrange(1, 2**31)
    game_map = generate_map(seed)

    run = Run(
        user_id=user.id,
        status=RunStatus.ACTIVE,
        seed=seed,
        map_data=game_map.to_dict(),
        current_node_id=None,
        cleared_node_ids=[],
        party=party,
        decks=decks,
        equipped_passives=[],
        shop_state={},
        inventory=[],
        battle=None,
    )
    session.add(run)
    session.flush()
    return run


def abandon_run(session: Session, run: Run) -> None:
    run.status = RunStatus.ABANDONED
    run.ended_at = utcnow()
    session.flush()


def fail_run(session: Session, run: Run) -> None:
    """§3 패배 → 런 전체 리셋. 부분 진행 재시도는 없다."""
    run.status = RunStatus.FAILED
    run.ended_at = utcnow()
    run.battle = None
    session.flush()


def clear_run(session: Session, user: User, run: Run) -> None:
    run.status = RunStatus.CLEARED
    run.ended_at = utcnow()
    run.battle = None
    # §5.5 런 플레이 보상으로 카르타를 지급한다.
    user.carta += balance.RUN_CLEAR_CARTA
    session.flush()


# ---------------------------------------------------------------------------
# 이동 가능한 노드
# ---------------------------------------------------------------------------


def available_nodes(run: Run) -> list[MapNode]:
    game_map = get_map(run)
    if run.current_node_id is None:
        return game_map.entry_nodes()
    if run.battle:
        return []  # 전투 중에는 이동 불가
    if run.current_node_id not in run.cleared_node_ids:
        return []  # 현재 노드를 아직 해결하지 않았다
    return game_map.next_nodes(run.current_node_id)


# ---------------------------------------------------------------------------
# 노드 진입
# ---------------------------------------------------------------------------


def enter_node(session: Session, user: User, run: Run, node_id: str) -> NodeOutcome:
    game_map = get_map(run)
    options = {n.id for n in available_nodes(run)}
    if node_id not in options:
        raise RunError("지금 이동할 수 없는 노드입니다.")

    node = game_map.nodes[node_id]
    run.current_node_id = node_id

    handler = {
        NodeType.COMBAT.value: _enter_combat,
        NodeType.BOSS.value: _enter_combat,
        NodeType.REWARD.value: _enter_reward,
        NodeType.REST.value: _enter_rest,
        NodeType.SHOP.value: _enter_shop,
        NodeType.EVENT.value: _enter_event,
    }.get(node.type)

    if handler is None:
        raise RunError(f"알 수 없는 노드 타입 `{node.type}`")

    outcome = handler(session, user, run, node, game_map)
    run.map_data = game_map.to_dict()
    session.flush()
    return outcome


# --- 전투 노드 (§2, §3.1) ---


def _pick_enemies(session: Session, node: MapNode, rng: random.Random) -> list[Enemy]:
    """§2.6 적 수는 스테이지/노드마다 다르고 상한이 없다.

    TBD: 층별 정확한 편성 규칙은 밸런싱 사항이다. 여기서는 층이 깊어질수록
    수가 늘어나고, 보스 노드는 보스 1 + 호위 여럿이 되도록만 잡았다.
    """
    is_boss = node.type == NodeType.BOSS.value
    if is_boss:
        bosses = session.scalars(
            select(Enemy).where(Enemy.is_active.is_(True), Enemy.tier == EnemyTier.BOSS)
        ).all()
        minions = session.scalars(
            select(Enemy).where(Enemy.is_active.is_(True), Enemy.tier != EnemyTier.BOSS)
        ).all()
        if not bosses:
            bosses = minions
        if not bosses:
            raise RunError("등록된 적 데이터가 없습니다. 대시보드에서 적을 먼저 등록해주세요.")
        squad = [rng.choice(bosses)]
        escort = min(4, 1 + node.floor // 4)
        squad.extend(rng.choice(minions) for _ in range(escort) if minions)
        return squad

    pool = session.scalars(
        select(Enemy).where(Enemy.is_active.is_(True), Enemy.tier != EnemyTier.BOSS)
    ).all()
    if not pool:
        raise RunError("등록된 적 데이터가 없습니다. 대시보드에서 적을 먼저 등록해주세요.")

    count = min(6, 1 + node.floor // 3 + rng.randint(0, 1))
    return [rng.choice(pool) for _ in range(count)]


def _load_catalog(session: Session, run: Run) -> dict[str, CardView]:
    """이번 전투에 등장할 수 있는 모든 카드를 스냅샷으로 굳힌다.

    전투 중 대시보드에서 카드가 수정돼도 진행 중인 전투가 흔들리지 않는다.
    """
    codes: set[str] = set()
    for deck in run.decks.values():
        codes.update(deck)
    if not codes:
        return {}
    cards = session.scalars(select(Card).where(Card.code.in_(list(codes)))).all()
    return {c.code: CardView.from_model(c) for c in cards}


def _enter_combat(
    session: Session, user: User, run: Run, node: MapNode, game_map: GameMap
) -> NodeOutcome:
    rng = random.Random(f"{run.seed}:{node.id}")
    enemies = _pick_enemies(session, node, rng)

    units: list[Unit] = []
    # §2.4 HP 는 노드 간 유지된다 — 파티 스냅샷의 현재 HP 를 그대로 쓴다.
    for member in run.party:
        deck = list(run.decks.get(member["code"], []))
        units.append(
            Unit(
                uid=member["uid"],
                code=member["code"],
                name=member["name"],
                side=Side.PLAYER,
                hp=member["hp"],
                max_hp=member["max_hp"],
                attack=member["attack"],
                defense=member["defense"],
                speed=member["speed"],
                draw_pile=deck,
                star=member.get("star", 1),
                sprite_path=member.get("sprite_path"),
            )
        )

    enemy_moves: dict[str, list] = {}
    for i, enemy in enumerate(enemies):
        units.append(
            Unit(
                uid=f"e{i}",
                code=enemy.code,
                name=enemy.name,
                side=Side.ENEMY,
                hp=enemy.hp,
                max_hp=enemy.hp,
                attack=enemy.attack,
                defense=enemy.defense,
                speed=enemy.speed,
                sprite_path=enemy.sprite_path,
            )
        )
        enemy_moves[enemy.code] = enemy.moves or []

    engine = CombatEngine.start(
        units=units,
        catalog=_load_catalog(session, run),
        enemy_moves=enemy_moves,
        seed=rng.randrange(1, 2**31),
    )
    run.battle = engine.to_dict()

    title = "👑 보스 전투!" if node.type == NodeType.BOSS.value else "⚔️ 전투 시작"
    return NodeOutcome(kind="battle", message=title, lines=engine.log[-12:], choices=[])


# --- 보상 노드 (§3.2) ---


def _reward_candidates(session: Session, user: User, run: Run, rng: random.Random) -> list[Card]:
    """§3.2 + §5.3: 가챠로 영구 해금한 카드만 보상 선택지에 등장한다.

    파티 캐릭터 전용 카드 + 공용 카드가 대상이다.
    """
    unlocked = [
        uc.card_code
        for uc in session.scalars(select(UserCard).where(UserCard.user_id == user.id)).all()
    ]
    if not unlocked:
        return []

    party_codes = [m["code"] for m in run.party]
    cards = session.scalars(
        select(Card).where(
            Card.code.in_(unlocked),
            Card.is_active.is_(True),
            (Card.character_code.is_(None)) | (Card.character_code.in_(party_codes)),
        )
    ).all()
    if not cards:
        return []

    rng.shuffle(cards)
    return cards[: balance.REWARD_CARD_CHOICES]


def _enter_reward(
    session: Session, user: User, run: Run, node: MapNode, game_map: GameMap
) -> NodeOutcome:
    rng = random.Random(f"{run.seed}:{node.id}:reward")
    candidates = _reward_candidates(session, user, run, rng)

    if not candidates:
        node.data["resolved"] = True
        _mark_cleared(run, node)
        return NodeOutcome(
            kind="reward",
            message="🎁 보상 노드",
            lines=[
                "아직 가챠로 해금한 카드가 없어 제시할 선택지가 없습니다.",
                "`!카드 뽑기` 로 카드를 해금하면 이후 런의 보상 노드에서 선택할 수 있습니다. (§5.3)",
            ],
            choices=[],
        )

    node.data["offer"] = [c.code for c in candidates]
    return NodeOutcome(
        kind="reward",
        message="🎁 보상 노드 — 이번 런의 덱에 추가할 카드를 고르세요",
        lines=["선택한 카드는 **이번 런에서만** 유지됩니다. (§3.2)"],
        choices=[
            {
                "index": i + 1,
                "code": c.code,
                "name": c.name,
                "cost": c.cost,
                "kind": c.kind.value,
                "description": c.description,
            }
            for i, c in enumerate(candidates)
        ],
    )


def take_reward(session: Session, user: User, run: Run, choice: int) -> str:
    game_map = get_map(run)
    node = game_map.nodes.get(run.current_node_id or "")
    if node is None or node.type != NodeType.REWARD.value:
        raise RunError("지금은 보상을 고를 수 없습니다.")

    offer: list[str] = node.data.get("offer", [])
    if not (1 <= choice <= len(offer)):
        raise RunError(f"1~{len(offer)} 사이의 번호를 골라주세요.")

    card_code = offer[choice - 1]
    card = session.scalar(select(Card).where(Card.code == card_code))
    if card is None:
        raise RunError("카드 데이터를 찾을 수 없습니다.")

    # §3.2: 이번 런의 액티브 덱에만 추가된다.
    owner = card.character_code
    targets = [owner] if owner else [m["code"] for m in run.party]
    for code in targets:
        run.decks.setdefault(code, []).append(card.code)

    node.data["offer"] = []
    node.data["resolved"] = True
    run.map_data = game_map.to_dict()
    _mark_cleared(run, node)
    session.flush()

    scope = card.character_code or "파티 전원"
    return f"🃏 [{card.name}] 을(를) 이번 런의 덱에 추가했습니다. (대상: {scope})"


# --- 휴식 노드 (§3.1) ---


def _enter_rest(
    session: Session, user: User, run: Run, node: MapNode, game_map: GameMap
) -> NodeOutcome:
    lines: list[str] = []
    for member in run.party:
        before = member["hp"]
        healed = int(member["max_hp"] * balance.REST_HEAL_PERCENT)
        member["hp"] = min(member["max_hp"], member["hp"] + healed)
        lines.append(f"💚 {member['name']}: {before} → {member['hp']} / {member['max_hp']}")

    node.data["resolved"] = True
    _mark_cleared(run, node)
    return NodeOutcome(kind="rest", message="🔥 휴식 — 파티가 회복했습니다", lines=lines, choices=[])


# --- 상점 노드 (§7.1) ---


def _enter_shop(
    session: Session, user: User, run: Run, node: MapNode, game_map: GameMap
) -> NodeOutcome:
    from . import shop as shop_service

    stock = shop_service.roll_node_stock(session, user, run, node)
    run.shop_state[node.id] = stock
    return NodeOutcome(
        kind="shop",
        message="🏪 상점 — `!카드 구매 <번호>`",
        lines=["여기서 산 물건은 **이번 런에서만** 유효합니다. (§7.1)"],
        choices=stock["items"],
    )


# --- 이벤트 노드 (§3.1) ---


def _enter_event(
    session: Session, user: User, run: Run, node: MapNode, game_map: GameMap
) -> NodeOutcome:
    """TBD (§13): 이벤트 노드의 콘텐츠는 미확정이다.

    임의로 이벤트를 만들어 넣지 않는다. 노드는 통과 처리하고, 콘텐츠가
    비어 있다는 사실을 명시한다.
    """
    node.data["resolved"] = True
    _mark_cleared(run, node)
    return NodeOutcome(
        kind="event",
        message="❓ 이벤트 노드",
        lines=[
            "이벤트 콘텐츠는 아직 설계 미확정(TBD) 항목이라 비어 있습니다.",
            "아무 일도 일어나지 않고 통과합니다.",
        ],
        choices=[],
    )


# ---------------------------------------------------------------------------
# 전투 진행
# ---------------------------------------------------------------------------


def load_battle(run: Run) -> CombatEngine:
    if not run.battle:
        raise RunError("진행 중인 전투가 없습니다.")
    return CombatEngine.from_dict(run.battle)


def _sync_party_hp(run: Run, engine: CombatEngine) -> None:
    """§2.4 전투 결과 HP 를 런 파티 스냅샷에 반영한다 (노드 간 유지)."""
    by_uid = {u.uid: u for u in engine.players}
    for member in run.party:
        unit = by_uid.get(member["uid"])
        if unit is not None:
            member["hp"] = unit.hp


def _mark_cleared(run: Run, node: MapNode) -> None:
    if node.id not in run.cleared_node_ids:
        run.cleared_node_ids = [*run.cleared_node_ids, node.id]


def resolve_battle_state(session: Session, user: User, run: Run, engine: CombatEngine) -> NodeOutcome:
    """전투 엔진 상태를 런에 반영하고 결과를 만든다."""
    _sync_party_hp(run, engine)
    game_map = get_map(run)
    node = game_map.nodes[run.current_node_id or ""]

    if engine.phase is BattlePhase.WON:
        run.battle = None
        _mark_cleared(run, node)
        lines = list(engine.log[-12:])

        is_boss = node.type == NodeType.BOSS.value
        lines.append(
            f"💰 코인 +{balance.BOSS_NODE_COIN if is_boss else balance.COMBAT_NODE_COIN}"
            " (중앙봇 반영)"
        )

        if is_boss:
            clear_run(session, user, run)
            lines.append(f"🏁 런 클리어! 카르타 +{balance.RUN_CLEAR_CARTA}")
            return NodeOutcome(kind="cleared", message="👑 보스 격파!", lines=lines, choices=[])

        run.map_data = game_map.to_dict()
        session.flush()
        return NodeOutcome(kind="battle", message="🏆 전투 승리", lines=lines, choices=[])

    if engine.phase is BattlePhase.LOST:
        # §3 패배 → 런 전체 리셋
        lines = list(engine.log[-12:])
        lines.append("이 런은 여기서 종료됩니다. 진행 상황은 모두 초기화됩니다. (§3)")
        fail_run(session, run)
        return NodeOutcome(kind="failed", message="☠️ 패배 — 런 종료", lines=lines, choices=[])

    run.battle = engine.to_dict()
    run.map_data = game_map.to_dict()
    session.flush()
    return NodeOutcome(kind="battle", message="", lines=engine.log[-12:], choices=[])


def battle_reward_coin(run: Run) -> int:
    """방금 클리어한 노드의 코인 보상액 (중앙봇에 반영할 값)."""
    game_map = get_map(run)
    node = game_map.nodes.get(run.current_node_id or "")
    if node is None:
        return 0
    return balance.BOSS_NODE_COIN if node.type == NodeType.BOSS.value else balance.COMBAT_NODE_COIN
