"""수치 밸런싱 값 모음.

설계 문서 §13 "Open TBD List" 에 따라, 아래 값들은 **확정된 설계가 아니라
동작을 위한 임시 플레이스홀더**다. 밸런싱 패스에서 전부 재조정되어야 한다.

코드 전역에서 매직 넘버를 쓰지 않고 반드시 이 모듈을 참조한다. 그래야
밸런싱 확정 시 한 파일만 고치면 된다.

각 값의 `TBD:` 주석은 설계 문서에서 해당 항목이 미확정임을 표시한 곳이다.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 전투 (§2)
# ---------------------------------------------------------------------------

# TBD (§2.2): 턴당 뽑는 카드 수. 설계 의도는 "여러 장 뽑아 1장 선택".
DRAW_PER_TURN: Final[int] = 3

# TBD (§2.3): 파티 공유 자원 풀 크기. 라운드마다 완전 회복된다.
RESOURCE_POOL_MAX: Final[int] = 3

# TBD (§2.2): 드로우 더미 고갈 시 페널티. 현재는 "카드를 못 뽑음"만 구현하고
# 추가 페널티는 없음. 확정 전까지 수치 페널티를 임의로 넣지 않는다.
EMPTY_PILE_PENALTY_DAMAGE: Final[int] = 0

# TBD (§2.1): 턴 순서 결정 방식. speed 스탯 기준 정렬로 임시 구현하며,
# 고정 순서로 바꾸려면 TurnOrder 정책만 교체하면 된다.
TURN_ORDER_MODE: Final[str] = "speed"  # "speed" | "fixed"

# 기본 카드 (§4.3) 는 항상 덱에 들어가는 최소 보장 카드다.
#
# ⚠️ 설계상 주의: §2.2 는 "턴마다 여러 장 드로우 + 재섞기 없음"이므로
#    한 캐릭터가 한 전투에서 행동할 수 있는 턴 수 = ceil(덱 크기 / 드로우 수)
#    로 **덱 크기가 전투 길이의 상한**이 된다. 이 값이 작으면 전투 도중
#    드로우 더미가 말라 아무것도 못 하고 진다. 드로우 더미 고갈 페널티가
#    확정(§13)되기 전까지는 이 상호작용을 염두에 두고 조정해야 한다.
STARTER_DECK_COPIES: Final[int] = 8  # TBD: 평타/기본방어 각각 몇 장으로 시작할지

# ---------------------------------------------------------------------------
# 맵 / 런 (§3)
# ---------------------------------------------------------------------------

MAP_FLOORS: Final[int] = 12          # TBD: 보스 전까지의 층 수
MAP_MIN_BRANCH: Final[int] = 2       # 한 층의 최소 노드 수
MAP_MAX_BRANCH: Final[int] = 3       # 한 층의 최대 노드 수

# TBD: 노드 타입 가중치. 보스는 마지막 층 고정이라 여기 포함하지 않는다.
NODE_WEIGHTS: Final[dict[str, int]] = {
    "combat": 45,
    "reward": 18,
    "rest": 12,
    "shop": 10,
    "event": 15,
}

# 첫 층은 항상 전투로 고정해 런의 시작을 일관되게 만든다.
FIRST_FLOOR_ALL_COMBAT: Final[bool] = True

REST_HEAL_PERCENT: Final[float] = 0.3   # TBD: 휴식 노드 회복량 (최대 HP 비율)
REWARD_CARD_CHOICES: Final[int] = 3     # TBD: 보상 노드에서 제시할 카드 수

# ---------------------------------------------------------------------------
# 파티 / 캐릭터 (§4)
# ---------------------------------------------------------------------------

BASE_PARTY_SLOTS: Final[int] = 1   # §4.1: 시작 파티는 1명
MAX_PARTY_SLOTS: Final[int] = 3    # §4.1/§2.6: 플레이어 측 최대 3슬롯

NORMAL_MAX_STAR: Final[int] = 3    # §4.4: 일반 캐릭터 상한
SPECIAL_MAX_STAR: Final[int] = 6   # §4.4: 특정 캐릭터만 6성까지

# TBD (§4.4): 성급 상승에 필요한 카드 조각. index = 목표 성급.
STAR_UP_FRAGMENT_COST: Final[dict[int, int]] = {
    2: 30, 3: 80, 4: 200, 5: 400, 6: 800,
}

# 성급당 스탯 배율. TBD.
STAR_STAT_MULTIPLIER: Final[float] = 0.15  # 성급 1당 +15%

# ---------------------------------------------------------------------------
# 가챠 (§5)
# ---------------------------------------------------------------------------

# TBD (§5): 카르타 기준 뽑기 비용
PULL_COST_CARTA: Final[int] = 160
MULTI_PULL_COUNT: Final[int] = 10
MULTI_PULL_COST_CARTA: Final[int] = 1600

# TBD (§5.7): 확률 및 천장.
RARITY_RATES: Final[dict[int, float]] = {3: 0.006, 2: 0.051, 1: 0.943}
SOFT_PITY_START: Final[int] = 74
HARD_PITY: Final[int] = 90
PICKUP_CHANCE: Final[float] = 0.5   # §5.4: 50/50 (확정 사항)

# §5.2: 중복 변환 보상량 (변환 대상 자체는 확정, 수량은 TBD)
DUP_CHARACTER_WILDCARDS: Final[dict[int, int]] = {1: 1, 2: 5, 3: 25}
DUP_CARD_FRAGMENTS: Final[dict[int, int]] = {1: 1, 2: 2, 3: 5, 4: 10, 5: 20, 6: 40}

# TBD: 캐릭터는 1~3성(§4.4), 카드/패시브는 6단계(§5.6)로 서로 다른 척도를 쓴다.
# 같은 가챠 풀에서 뽑히므로(§5.1) 카드 등급을 뽑기 티어 1~3 으로 접는 매핑이
# 필요한데, 설계상 확정된 대응표가 없어 임시로 2단계씩 묶는다.
CARD_RARITY_TO_GACHA_TIER: Final[dict[int, int]] = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3, 6: 3}

# ---------------------------------------------------------------------------
# 패시브 카드 (§6)
# ---------------------------------------------------------------------------

BASE_PASSIVE_SLOTS: Final[int] = 2  # §6: 확정
MAX_PASSIVE_SLOTS: Final[int] = 4   # §6: 확정

PASSIVE_RARITY_TIERS: Final[int] = 6  # §5.6: 확정

# ---------------------------------------------------------------------------
# 재화 / 보상 (§5.5)
# ---------------------------------------------------------------------------

DAILY_CARTA: Final[int] = 60        # TBD: 일일/출석 보상
DAILY_COIN: Final[int] = 500        # TBD
RUN_CLEAR_CARTA: Final[int] = 100   # TBD: 런 클리어 보상
RUN_CLEAR_COIN: Final[int] = 1000   # TBD
COMBAT_NODE_COIN: Final[int] = 40   # TBD: 전투 노드 1회 클리어 보상
BOSS_NODE_COIN: Final[int] = 300    # TBD

# ---------------------------------------------------------------------------
# 상점 (§7)
# ---------------------------------------------------------------------------

SHOP_NODE_SLOTS: Final[int] = 4     # TBD: 노드 상점 진열 수
SHOP_CARD_PRICE_COIN: Final[int] = 120     # TBD
SHOP_CONSUMABLE_PRICE_COIN: Final[int] = 80  # TBD

# ---------------------------------------------------------------------------
# 장비 (§8)
# ---------------------------------------------------------------------------

EQUIPMENT_MAX_TIER: Final[int] = 5  # TBD
# TBD (§8): 전용 강화 재료의 이름/획득처 미확정. 코드상 식별자는
# `equip_material` 로 두고, 이름이 확정되면 표시 문자열만 교체한다.
EQUIP_MATERIAL_DISPLAY_NAME: Final[str] = "장비 강화 재료(가칭)"
EQUIP_TIER_UP_COST: Final[dict[int, int]] = {2: 5, 3: 12, 4: 25, 5: 50}
