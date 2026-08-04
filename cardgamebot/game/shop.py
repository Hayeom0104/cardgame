"""상점 시스템 (설계 문서 §7).

두 상점의 역할이 다르다:

* §7.1 런 내 노드 상점 — 런 스코프 소모품/임시 버프 + 이번 런에서 쓰는 카드.
  런이 끝나면 진열도 구매 내역도 사라진다.
* §7.2 허브 상점 — 런 밖 상시 운영. 영구 아이템, 주로 장비(§8).

TBD (§13): 양쪽 상점의 가격 정책 전부. balance.py 의 값은 임시다.
소모품의 구체적 목록도 설계에 명시된 바 없어, 아래 CONSUMABLES 는 상점이
동작하도록 두는 **플레이스홀더 콘텐츠**다 (기획 확정 시 교체 대상).
"""

from __future__ import annotations

import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import balance
from ..core.central_client import CentralAPIError, get_central_client
from ..db.models import Card, Equipment, Run, User, UserCard, UserEquipment


class ShopError(Exception):
    pass


# 플레이스홀더 소모품 (§7.1 "런 스코프 소모품/임시 버프")
CONSUMABLES: list[dict] = [
    {
        "code": "potion_small",
        "name": "회복 물약",
        "description": "파티 전원의 HP를 최대치의 25% 회복합니다.",
        "price": balance.SHOP_CONSUMABLE_PRICE_COIN,
        "effect": {"op": "heal_party", "percent": 0.25},
    },
    {
        "code": "whetstone",
        "name": "숫돌",
        "description": "이번 런 동안 파티 전원의 공격력이 2 오릅니다.",
        "price": balance.SHOP_CONSUMABLE_PRICE_COIN,
        "effect": {"op": "party_stat", "stat": "attack", "amount": 2},
    },
    {
        "code": "iron_plate",
        "name": "철판",
        "description": "이번 런 동안 파티 전원의 최대 HP가 8 오릅니다.",
        "price": balance.SHOP_CONSUMABLE_PRICE_COIN,
        "effect": {"op": "party_stat", "stat": "max_hp", "amount": 8},
    },
]


# ---------------------------------------------------------------------------
# §7.1 런 내 노드 상점
# ---------------------------------------------------------------------------


def roll_node_stock(session: Session, user: User, run: Run, node) -> dict:
    """노드 상점 진열을 뽑는다. 같은 노드는 항상 같은 진열을 갖는다."""
    existing = run.shop_state.get(node.id)
    if existing:
        return existing

    rng = random.Random(f"{run.seed}:{node.id}:shop")
    items: list[dict] = []

    # 카드: §3.2 와 마찬가지로 영구 해금(§5.3)된 것 중에서만 판다.
    unlocked = [
        uc.card_code
        for uc in session.scalars(select(UserCard).where(UserCard.user_id == user.id)).all()
    ]
    party_codes = [m["code"] for m in run.party]
    if unlocked:
        cards = session.scalars(
            select(Card).where(
                Card.code.in_(unlocked),
                Card.is_active.is_(True),
                (Card.character_code.is_(None)) | (Card.character_code.in_(party_codes)),
            )
        ).all()
        rng.shuffle(cards)
        for card in cards[:2]:
            items.append(
                {
                    "kind": "card",
                    "code": card.code,
                    "name": card.name,
                    "description": card.description,
                    "price": balance.SHOP_CARD_PRICE_COIN,
                }
            )

    for item in rng.sample(CONSUMABLES, k=min(len(CONSUMABLES), balance.SHOP_NODE_SLOTS - len(items))):
        items.append({"kind": "consumable", **item})

    for i, item in enumerate(items):
        item["index"] = i + 1

    return {"items": items, "sold": []}


async def buy_node_item(session: Session, user: User, run: Run, choice: int) -> str:
    node_id = run.current_node_id
    stock = run.shop_state.get(node_id or "")
    if not stock:
        raise ShopError("지금은 상점을 이용할 수 없습니다.")

    items = stock["items"]
    if not (1 <= choice <= len(items)):
        raise ShopError(f"1~{len(items)} 사이의 번호를 골라주세요.")
    if choice in stock["sold"]:
        raise ShopError("이미 구매한 물건입니다.")

    item = items[choice - 1]

    # 코인은 중앙봇 소유 (§1.1)
    try:
        await get_central_client().spend_currency(
            user.discord_id, item["price"], reason=f"shop:{item['code']}"
        )
    except CentralAPIError as exc:
        raise ShopError(str(exc)) from exc

    stock["sold"] = [*stock["sold"], choice]

    if item["kind"] == "card":
        # §7.1: 보상 노드와 같은 런 스코프 덱 추가.
        card = session.scalar(select(Card).where(Card.code == item["code"]))
        if card is None:
            raise ShopError("카드 데이터를 찾을 수 없습니다.")
        targets = [card.character_code] if card.character_code else [m["code"] for m in run.party]
        for code in targets:
            run.decks.setdefault(code, []).append(card.code)
        message = f"🃏 [{card.name}] 구매 — 이번 런의 덱에 추가되었습니다."
    else:
        message = _apply_consumable(run, item)

    # JSON 컬럼 재대입으로 SQLAlchemy 에 변경을 알린다.
    run.shop_state = {**run.shop_state, node_id: stock}
    run.decks = dict(run.decks)
    session.flush()
    return message


def _apply_consumable(run: Run, item: dict) -> str:
    effect = item["effect"]
    op = effect.get("op")

    if op == "heal_party":
        percent = float(effect.get("percent", 0.0))
        for member in run.party:
            member["hp"] = min(member["max_hp"], member["hp"] + int(member["max_hp"] * percent))
        run.party = list(run.party)
        return f"💚 [{item['name']}] 사용 — 파티 전원이 회복했습니다."

    if op == "party_stat":
        stat = effect.get("stat", "attack")
        amount = int(effect.get("amount", 0))
        for member in run.party:
            member[stat] = member.get(stat, 0) + amount
            if stat == "max_hp":
                member["hp"] += amount
        run.party = list(run.party)
        return f"⬆️ [{item['name']}] 사용 — 이번 런 동안 파티 {stat} +{amount}"

    return f"[{item['name']}] 을(를) 구매했습니다."


# ---------------------------------------------------------------------------
# §7.2 허브 상점 (런 밖, 상시)
# ---------------------------------------------------------------------------


def hub_listings(session: Session) -> list[dict]:
    """§7.2 영구 아이템 — 주로 장비."""
    equipment = session.scalars(
        select(Equipment).where(Equipment.is_active.is_(True)).order_by(Equipment.id)
    ).all()
    return [
        {
            "index": i + 1,
            "code": e.code,
            "name": e.name,
            "description": e.description,
            "price_coin": e.price_coin,
            "price_carta": e.price_carta,
        }
        for i, e in enumerate(equipment)
    ]


async def buy_hub_item(session: Session, user: User, choice: int) -> str:
    listings = hub_listings(session)
    if not listings:
        raise ShopError("허브 상점에 등록된 상품이 없습니다.")
    if not (1 <= choice <= len(listings)):
        raise ShopError(f"1~{len(listings)} 사이의 번호를 골라주세요.")

    item = listings[choice - 1]

    # 재화를 깎기 전에 보유 여부부터 확인한다.
    existing = session.scalar(
        select(UserEquipment).where(
            UserEquipment.user_id == user.id, UserEquipment.equipment_code == item["code"]
        )
    )
    if existing is not None:
        raise ShopError("이미 보유한 장비입니다.")

    if item["price_carta"] > 0:
        if user.carta < item["price_carta"]:
            raise ShopError(
                f"카르타가 부족합니다. (필요 {item['price_carta']:,}, 보유 {user.carta:,})"
            )
        user.carta -= item["price_carta"]

    if item["price_coin"] > 0:
        try:
            await get_central_client().spend_currency(
                user.discord_id, item["price_coin"], reason=f"hubshop:{item['code']}"
            )
        except CentralAPIError as exc:
            # 카르타를 이미 깎았다면 되돌린다.
            if item["price_carta"] > 0:
                user.carta += item["price_carta"]
            raise ShopError(str(exc)) from exc

    session.add(UserEquipment(user_id=user.id, equipment_code=item["code"], tier=1))
    session.flush()
    return f"🛠️ [{item['name']}] 을(를) 구매했습니다. (티어 1)"
