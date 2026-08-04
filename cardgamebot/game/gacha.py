"""가챠 시스템 (설계 문서 §5).

확정 사항:
* §5.1 한 번의 뽑기에서 **캐릭터 또는 배틀 카드**가 나온다. 캐릭터도 "카드"의
  한 카테고리로 같은 시스템에서 뽑힌다.
* §5.2 중복 캐릭터 → 와일드카드, 중복 카드 → 카드 조각.
* §5.3 뽑은 카드는 해당 캐릭터에 대해 **영구적으로 '획득 가능' 표시**가 되고,
  이후 모든 런의 보상 노드(§3.2) 선택지에 등장할 자격을 얻는다.
* §5.4 상시 배너 + 한정 픽업 배너. 한정 배너 최고 등급 적중 시 50/50,
  실패하면 다음 최고 등급은 픽업 확정.
* §5.5 재화는 카르타.
* §6 패시브 카드도 먼저 가챠로 해금된다.

TBD (§5.7, §13): 상시 뽑기 천장 방식, 배너별 재화 분리 여부, 픽업 대상이
캐릭터 전용인지 카드도 포함인지, 정확한 카르타 비용.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import balance
from ..db.models import (
    Banner,
    BannerType,
    Card,
    Character,
    GachaState,
    PassiveCard,
    User,
    UserCard,
    UserCharacter,
    UserPassive,
)


class GachaError(Exception):
    pass


@dataclass
class PullResult:
    category: str          # "character" | "card" | "passive"
    code: str
    name: str
    tier: int              # 뽑기 티어 1~3
    rarity: int            # 캐릭터는 성급, 카드는 1~6 등급
    is_new: bool
    is_pickup: bool = False
    fragments_gained: int = 0
    wildcards_gained: int = 0

    @property
    def label(self) -> str:
        mark = "🆕" if self.is_new else "♻️"
        pickup = " ⭐PICKUP" if self.is_pickup else ""
        stars = "★" * self.tier
        return f"{mark} [{stars}] {self.name}{pickup}"


@dataclass
class _PoolEntry:
    category: str
    code: str
    name: str
    tier: int
    rarity: int


# ---------------------------------------------------------------------------
# 풀 구성
# ---------------------------------------------------------------------------


def _build_pool(session: Session, banner: Banner) -> dict[int, list[_PoolEntry]]:
    """티어별 후보 목록을 만든다. 배너에 명시 목록이 없으면 활성 전체를 쓴다."""
    pool: dict[int, list[_PoolEntry]] = {1: [], 2: [], 3: []}

    char_q = select(Character).where(Character.is_active.is_(True))
    if banner.pool_characters:
        char_q = char_q.where(Character.code.in_(banner.pool_characters))
    for ch in session.scalars(char_q).all():
        tier = max(1, min(3, ch.base_rarity))
        pool[tier].append(_PoolEntry("character", ch.code, ch.name, tier, ch.base_rarity))

    card_q = select(Card).where(Card.is_active.is_(True), Card.in_gacha_pool.is_(True))
    if banner.pool_cards:
        card_q = card_q.where(Card.code.in_(banner.pool_cards))
    for card in session.scalars(card_q).all():
        tier = balance.CARD_RARITY_TO_GACHA_TIER.get(card.rarity, 1)
        pool[tier].append(_PoolEntry("card", card.code, card.name, tier, card.rarity))

    # §6: 패시브 카드도 가챠로 해금된다.
    for passive in session.scalars(
        select(PassiveCard).where(PassiveCard.is_active.is_(True))
    ).all():
        tier = balance.CARD_RARITY_TO_GACHA_TIER.get(passive.rarity, 1)
        pool[tier].append(_PoolEntry("passive", passive.code, passive.name, tier, passive.rarity))

    return pool


def _pickup_entries(pool: dict[int, list[_PoolEntry]], banner: Banner) -> list[_PoolEntry]:
    codes = set(banner.pickup_characters or []) | set(banner.pickup_cards or [])
    if not codes:
        return []
    return [e for e in pool[3] if e.code in codes]


# ---------------------------------------------------------------------------
# 등급 판정
# ---------------------------------------------------------------------------


def _roll_tier(rng: random.Random, pity: int) -> int:
    """TBD(§5.7): 소프트/하드 천장은 임시 구현이다.

    하드 천장에 도달하면 최고 등급 확정, 소프트 천장부터는 확률이 선형 증가.
    """
    if pity + 1 >= balance.HARD_PITY:
        return 3

    top_rate = balance.RARITY_RATES[3]
    if pity + 1 >= balance.SOFT_PITY_START:
        steps = (pity + 1) - balance.SOFT_PITY_START + 1
        top_rate = min(1.0, top_rate + steps * 0.06)

    roll = rng.random()
    if roll < top_rate:
        return 3
    if roll < top_rate + balance.RARITY_RATES[2]:
        return 2
    return 1


# ---------------------------------------------------------------------------
# 지급 처리
# ---------------------------------------------------------------------------


def _grant(session: Session, user: User, entry: _PoolEntry, is_pickup: bool) -> PullResult:
    if entry.category == "character":
        owned = session.scalar(
            select(UserCharacter).where(
                UserCharacter.user_id == user.id,
                UserCharacter.character_code == entry.code,
            )
        )
        if owned is None:
            session.add(
                UserCharacter(user_id=user.id, character_code=entry.code, star=entry.rarity)
            )
            # 같은 연차 안에서 또 나오면 '중복'으로 잡히도록 즉시 반영한다.
            session.flush()
            return PullResult("character", entry.code, entry.name, entry.tier, entry.rarity, True, is_pickup)
        # §5.2 중복 캐릭터 → 와일드카드
        gained = balance.DUP_CHARACTER_WILDCARDS.get(entry.tier, 1)
        user.wildcards += gained
        return PullResult(
            "character", entry.code, entry.name, entry.tier, entry.rarity, False, is_pickup,
            wildcards_gained=gained,
        )

    if entry.category == "passive":
        row = session.scalar(
            select(UserPassive).where(
                UserPassive.user_id == user.id, UserPassive.passive_code == entry.code
            )
        )
        if row is None:
            # §6: 가챠는 '해금'까지만. 실제 사용 가능한 사본은 런 보상으로 얻는다.
            session.add(UserPassive(user_id=user.id, passive_code=entry.code, unlocked=True, owned=0))
            session.flush()
            return PullResult("passive", entry.code, entry.name, entry.tier, entry.rarity, True, is_pickup)
        # §5.2 는 "중복 카드 → 카드 조각" 만 규정한다. 패시브도 카드 계열이므로
        # 같은 처리를 적용한다.
        gained = balance.DUP_CARD_FRAGMENTS.get(entry.rarity, 1)
        user.fragments += gained
        return PullResult(
            "passive", entry.code, entry.name, entry.tier, entry.rarity, False, is_pickup,
            fragments_gained=gained,
        )

    # 배틀 카드
    row = session.scalar(
        select(UserCard).where(UserCard.user_id == user.id, UserCard.card_code == entry.code)
    )
    if row is None:
        # §5.3 영구 해금 — 이후 모든 런의 보상 노드 선택지에 등장 가능해진다.
        session.add(UserCard(user_id=user.id, card_code=entry.code, pull_count=1))
        session.flush()
        return PullResult("card", entry.code, entry.name, entry.tier, entry.rarity, True, is_pickup)

    row.pull_count += 1
    gained = balance.DUP_CARD_FRAGMENTS.get(entry.rarity, 1)
    user.fragments += gained
    return PullResult(
        "card", entry.code, entry.name, entry.tier, entry.rarity, False, is_pickup,
        fragments_gained=gained,
    )


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def get_banner(session: Session, code: str | None = None) -> Banner:
    if code:
        banner = session.scalar(select(Banner).where(Banner.code == code, Banner.is_active.is_(True)))
        if banner is None:
            raise GachaError(f"배너 `{code}` 를 찾을 수 없습니다.")
        return banner
    banner = session.scalar(
        select(Banner)
        .where(Banner.is_active.is_(True))
        .order_by(Banner.type.desc(), Banner.id.desc())
    )
    if banner is None:
        raise GachaError("활성화된 배너가 없습니다.")
    return banner


def _state(session: Session, user: User, banner: Banner) -> GachaState:
    state = session.scalar(
        select(GachaState).where(
            GachaState.user_id == user.id, GachaState.banner_code == banner.code
        )
    )
    if state is None:
        state = GachaState(user_id=user.id, banner_code=banner.code)
        session.add(state)
        session.flush()
    return state


def pull(
    session: Session,
    user: User,
    banner_code: str | None = None,
    count: int = 1,
    rng: random.Random | None = None,
) -> tuple[list[PullResult], int]:
    """뽑기를 실행하고 (결과 목록, 소모한 카르타)를 반환한다."""
    rng = rng or random.Random()
    banner = get_banner(session, banner_code)

    if count == balance.MULTI_PULL_COUNT:
        cost = balance.MULTI_PULL_COST_CARTA
    else:
        cost = balance.PULL_COST_CARTA * count

    if user.carta < cost:
        raise GachaError(f"카르타가 부족합니다. (필요 {cost:,}, 보유 {user.carta:,})")

    pool = _build_pool(session, banner)
    if not any(pool.values()):
        raise GachaError("이 배너의 가챠 풀이 비어 있습니다. 콘텐츠를 먼저 등록해주세요.")

    pickups = _pickup_entries(pool, banner)
    state = _state(session, user, banner)
    user.carta -= cost

    results: list[PullResult] = []
    for _ in range(count):
        tier = _roll_tier(rng, state.pity)
        state.total_pulls += 1

        if tier == 3:
            state.pity = 0
        else:
            state.pity += 1

        candidates = pool[tier] or pool[1] or pool[2] or pool[3]
        is_pickup = False

        # §5.4 한정 배너 최고 등급 50/50
        if tier == 3 and banner.type is BannerType.LIMITED and pickups:
            if state.guaranteed_pickup or rng.random() < balance.PICKUP_CHANCE:
                entry = rng.choice(pickups)
                is_pickup = True
                state.guaranteed_pickup = False
            else:
                non_pickup = [e for e in candidates if e not in pickups] or candidates
                entry = rng.choice(non_pickup)
                state.guaranteed_pickup = True  # 다음 최고 등급은 픽업 확정
        else:
            entry = rng.choice(candidates)

        results.append(_grant(session, user, entry, is_pickup))

    session.flush()
    return results, cost


def star_up(session: Session, user: User, character_code: str) -> dict:
    """§4.4 성급 상승 — 카드 조각을 소모한다."""
    owned = session.scalar(
        select(UserCharacter).where(
            UserCharacter.user_id == user.id, UserCharacter.character_code == character_code
        )
    )
    if owned is None:
        raise GachaError("보유하지 않은 캐릭터입니다.")

    character = session.scalar(select(Character).where(Character.code == character_code))
    if character is None:
        raise GachaError("캐릭터 데이터를 찾을 수 없습니다.")

    # §4.4: 일반 캐릭터는 3성, 지정된 특별 캐릭터만 6성까지.
    cap = min(character.max_star, balance.SPECIAL_MAX_STAR)
    if owned.star >= cap:
        raise GachaError(f"{character.name} 은(는) 이미 최대 {cap}성입니다.")

    target = owned.star + 1
    cost = balance.STAR_UP_FRAGMENT_COST.get(target)
    if cost is None:
        raise GachaError(f"{target}성 상승 비용이 정의되어 있지 않습니다.")
    if user.fragments < cost:
        raise GachaError(f"카드 조각이 부족합니다. (필요 {cost}, 보유 {user.fragments})")

    user.fragments -= cost
    owned.star = target
    session.flush()
    return {"name": character.name, "star": target, "cost": cost}
