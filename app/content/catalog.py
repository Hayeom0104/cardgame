"""카드 한 장의 통일된 모습 — 캐릭터도 카드다.

플레이어가 보는 것은 전부 **카드**다. 그중 어떤 카드는 파티 자리에 놓이는
**캐릭터 카드**이고, 나머지는 그 캐릭터의 덱에 들어가는 **행동 카드**다.
뽑기도, 소장 목록도, 덱 구성도 전부 이 하나의 목록 위에서 이루어진다.

저장은 여전히 `characters` / `cards` 두 테이블로 나뉘어 있다. 성급(★)과
업그레이드 단계처럼 두 종류가 서로 다르게 자라기 때문이고, §10.6의 콘텐츠
버전 고정과 §16.2.3의 런 스냅샷이 이 구분 위에 서 있기 때문이다. 그 차이를
아는 것은 이 모듈 하나뿐이고, 위로는 언제나 하나의 카드 목록만 보인다.

    KIND_CHARACTER  파티 자리를 차지한다. 성장 단위는 성급(★).
    KIND_ACTION     덱에 들어간다. 성장 단위는 업그레이드 단계.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.db.connection import Database

KIND_CHARACTER = "character"
KIND_ACTION = "action"

#: §2.10 — 무속성 카드는 누구에게나 들어가고, 속성 카드는 같은 속성에게만.
NEUTRAL_ELEMENT = "무속성"


@dataclass
class Card:
    """어느 종류든 카드 한 장이 보이는 모습."""

    card_id: str
    kind: str
    name: str
    element: str
    rarity: int
    #: 행동 카드만 가진다. 캐릭터 카드는 None.
    cost: int | None = None
    category: str | None = None
    #: 캐릭터 카드만 가진다.
    job_role: str | None = None
    #: 계정이 가지고 있는가. 안 가진 카드도 도감에는 보여야 한다.
    owned: bool = False
    #: 캐릭터 카드의 성급, 행동 카드의 업그레이드 단계.
    star_rank: int = 0
    upgrade_tier: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_character(self) -> bool:
        return self.kind == KIND_CHARACTER

    def playable_by(self, character: "Card") -> bool:
        """이 행동 카드를 저 캐릭터 카드가 낼 수 있는가 (§2.10)."""
        if self.is_character or not character.is_character:
            return False
        return self.element in (NEUTRAL_ELEMENT, character.element)

    def as_art(self) -> dict:
        """`panels.render_card` 가 읽는 모양."""
        return {
            "card_id": self.card_id, "name": self.name, "element": self.element,
            "rarity_tier": self.rarity, "cost": self.cost,
            "category": self.category or ("캐릭터" if self.is_character else ""),
            "upgrade_tier": self.upgrade_tier,
        }


# =====================================================================
# 조회
# =====================================================================
def _character(row, owned_row=None) -> Card:
    return Card(
        card_id=row["character_id"], kind=KIND_CHARACTER, name=row["name"],
        element=row["element"], rarity=int(row["base_rarity"]),
        job_role=row["job_role"],
        owned=owned_row is not None,
        star_rank=int(owned_row["star_rank"]) if owned_row is not None
        else int(row["base_rarity"]),
    )


def _action(row, owned_row=None) -> Card:
    return Card(
        card_id=row["card_id"], kind=KIND_ACTION, name=row["name"],
        element=row["element"], rarity=int(row["rarity_tier"]),
        cost=int(row["cost"]), category=row["category"],
        owned=owned_row is not None,
        upgrade_tier=int(owned_row["upgrade_tier"]) if owned_row is not None else 0,
    )


def all_cards(db: Database, content_version_id: int, *, user_id: int | None = None,
              owned_only: bool = False) -> list[Card]:
    """캐릭터 카드와 행동 카드를 한 목록으로.

    `user_id` 를 주면 소유 여부·성급·업그레이드 단계가 채워진다.
    """
    owned_characters = {}
    unlocked = {}
    if user_id is not None:
        owned_characters = {row["character_id"]: row for row in db.query(
            "SELECT character_id, star_rank FROM owned_characters WHERE user_id = ?",
            (user_id,))}
        unlocked = {row["card_id"]: row for row in db.query(
            "SELECT card_id, upgrade_tier FROM unlocked_cards WHERE user_id = ?",
            (user_id,))}

    cards = [
        _character(row, owned_characters.get(row["character_id"]))
        for row in db.query(
            "SELECT character_id, name, element, job_role, base_rarity "
            "FROM characters WHERE content_version_id = ? AND is_retired = 0 "
            "ORDER BY character_id", (content_version_id,))
    ]
    cards += [
        _action(row, unlocked.get(row["card_id"]))
        for row in db.query(
            "SELECT card_id, name, element, cost, category, rarity_tier "
            "FROM cards WHERE content_version_id = ? AND is_retired = 0 "
            "ORDER BY card_id", (content_version_id,))
    ]
    if owned_only:
        cards = [card for card in cards if card.owned]
    return cards


def owned(db: Database, user_id: int, content_version_id: int) -> list[Card]:
    """계정이 가진 카드만. 덱 구성 화면이 보는 목록이다."""
    return all_cards(db, content_version_id, user_id=user_id, owned_only=True)


def characters(db: Database, user_id: int, content_version_id: int) -> list[Card]:
    return [card for card in owned(db, user_id, content_version_id)
            if card.is_character]


def actions(db: Database, user_id: int, content_version_id: int) -> list[Card]:
    return [card for card in owned(db, user_id, content_version_id)
            if not card.is_character]


def find(db: Database, content_version_id: int, card_id: str, *,
         user_id: int | None = None) -> Card | None:
    """id 하나로 찾는다. 캐릭터인지 행동 카드인지는 묻지 않는다."""
    for card in all_cards(db, content_version_id, user_id=user_id):
        if card.card_id == card_id:
            return card
    return None


def playable_for(db: Database, user_id: int, content_version_id: int,
                 character_id: str) -> list[Card]:
    """이 캐릭터의 덱에 넣을 수 있는, 계정이 가진 행동 카드들 (§2.10)."""
    every = owned(db, user_id, content_version_id)
    character = next((card for card in every
                      if card.is_character and card.card_id == character_id), None)
    if character is None:
        return []
    return [card for card in every if card.playable_by(character)]
