"""초기 콘텐츠 시드.

여기 들어있는 카드/캐릭터/적/장비/연구 노드는 **게임이 처음부터 끝까지 돌아가는
것을 확인하기 위한 예시 콘텐츠**다. 설계 문서의 확정 규칙(§4.3 기본 카드,
§3.1 노드 타입, §9 연구 범위 등)은 지키지만, 수치와 라인업 자체는 확정된
기획이 아니다. 실제 콘텐츠는 §10 관리자 대시보드에서 등록한다.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    Banner,
    BannerType,
    Card,
    CardKind,
    Character,
    Enemy,
    EnemyTier,
    Equipment,
    PassiveCard,
    ResearchNode,
    TargetType,
)

# ---------------------------------------------------------------------------
# 캐릭터
# ---------------------------------------------------------------------------

CHARACTERS = [
    dict(code="aria", name="아리아", description="균형 잡힌 근접 딜러.",
         base_rarity=3, max_star=6, base_hp=62, base_attack=8, base_defense=2, base_speed=12),
    dict(code="noel", name="노엘", description="파티를 지키는 방패.",
         base_rarity=2, max_star=3, base_hp=78, base_attack=5, base_defense=4, base_speed=8),
    dict(code="mika", name="미카", description="회복과 보조에 특화된 서포터.",
         base_rarity=2, max_star=3, base_hp=54, base_attack=4, base_defense=1, base_speed=10),
    dict(code="ren", name="렌", description="빠른 연타형 공격수.",
         base_rarity=1, max_star=3, base_hp=48, base_attack=6, base_defense=1, base_speed=15),
]

# ---------------------------------------------------------------------------
# 카드
#   §4.3 — 모든 캐릭터는 가챠 0회여도 평타 + 기본 방어를 갖고 시작한다.
#   is_starter=True 이면서 character_code=None 이면 전 캐릭터 공용 기본 카드.
# ---------------------------------------------------------------------------

CARDS = [
    # --- 기본 카드 (§4.3) ---
    dict(code="basic_strike", name="평타", description="적 하나에게 피해를 줍니다.",
         character_code=None, kind=CardKind.ATTACK, target=TargetType.ENEMY_SINGLE,
         cost=1, rarity=1, is_starter=True, in_gacha_pool=False,
         effects=[{"op": "damage", "amount": 2}]),
    dict(code="basic_guard", name="기본 방어", description="자신에게 블록을 얻습니다.",
         character_code=None, kind=CardKind.DEFENSE, target=TargetType.SELF,
         cost=1, rarity=1, is_starter=True, in_gacha_pool=False,
         effects=[{"op": "block", "amount": 6}]),

    # --- 공용 카드 (§5.1 universal) ---
    dict(code="uni_sweep", name="휩쓸기", description="적 전체에게 피해를 줍니다.",
         character_code=None, kind=CardKind.ATTACK, target=TargetType.ENEMY_ALL,
         cost=2, rarity=3, effects=[{"op": "damage", "amount": 1}]),
    dict(code="uni_rally", name="집결", description="아군 전체의 공격력을 2턴간 올립니다.",
         character_code=None, kind=CardKind.BUFF, target=TargetType.ALLY_ALL,
         cost=2, rarity=4, effects=[{"op": "buff", "stat": "attack", "amount": 3, "duration": 2}]),
    dict(code="uni_bandage", name="붕대", description="아군 하나의 HP를 회복합니다.",
         character_code=None, kind=CardKind.HEAL, target=TargetType.ALLY_SINGLE,
         cost=1, rarity=2, effects=[{"op": "heal", "amount": 10}]),
    dict(code="uni_focus", name="집중", description="자원을 1 회복하고 카드를 1장 더 뽑습니다.",
         character_code=None, kind=CardKind.BUFF, target=TargetType.SELF,
         cost=0, rarity=5, effects=[{"op": "resource", "amount": 1}, {"op": "draw", "amount": 1}]),
    dict(code="uni_weaken", name="약화", description="적 하나의 공격력을 3턴간 낮춥니다.",
         character_code=None, kind=CardKind.DEBUFF, target=TargetType.ENEMY_SINGLE,
         cost=1, rarity=3, effects=[{"op": "debuff", "stat": "attack", "amount": 4, "duration": 3}]),

    # --- 캐릭터 전용 카드 (§5.1 character-exclusive) ---
    dict(code="aria_pierce", name="아리아: 관통", description="블록과 방어를 무시하는 고정 피해.",
         character_code="aria", kind=CardKind.ATTACK, target=TargetType.ENEMY_SINGLE,
         cost=2, rarity=5, effects=[{"op": "pierce", "amount": 14}]),
    dict(code="aria_flurry", name="아리아: 연격", description="적 하나를 3회 공격합니다.",
         character_code="aria", kind=CardKind.ATTACK, target=TargetType.ENEMY_SINGLE,
         cost=2, rarity=4, effects=[{"op": "damage", "amount": 0, "hits": 3}]),
    dict(code="noel_bulwark", name="노엘: 방벽", description="아군 전체에게 블록을 부여합니다.",
         character_code="noel", kind=CardKind.DEFENSE, target=TargetType.ALLY_ALL,
         cost=2, rarity=4, effects=[{"op": "block", "amount": 7}]),
    dict(code="noel_taunt", name="노엘: 도발", description="자신의 방어력을 크게 올립니다.",
         character_code="noel", kind=CardKind.BUFF, target=TargetType.SELF,
         cost=1, rarity=3, effects=[{"op": "buff", "stat": "defense", "amount": 4, "duration": 3},
                                    {"op": "block", "amount": 5}]),
    dict(code="mika_prayer", name="미카: 기도", description="아군 전체를 회복합니다.",
         character_code="mika", kind=CardKind.HEAL, target=TargetType.ALLY_ALL,
         cost=2, rarity=5, effects=[{"op": "heal", "amount": 6, "percent": 0.1}]),
    dict(code="mika_blessing", name="미카: 축복", description="아군 하나의 방어력을 올립니다.",
         character_code="mika", kind=CardKind.BUFF, target=TargetType.ALLY_SINGLE,
         cost=1, rarity=2, effects=[{"op": "buff", "stat": "defense", "amount": 3, "duration": 3}]),
    dict(code="ren_dash", name="렌: 질주", description="적 하나를 공격하고 자원을 1 회복합니다.",
         character_code="ren", kind=CardKind.ATTACK, target=TargetType.ENEMY_SINGLE,
         cost=1, rarity=4, effects=[{"op": "damage", "amount": 3}, {"op": "resource", "amount": 1}]),
]

# ---------------------------------------------------------------------------
# 패시브 카드 (§6)
#   TBD: 효과 목록은 미확정. 아래는 확정된 구조(상시/조건부, 6등급)만 보여주는
#   최소 예시다.
# ---------------------------------------------------------------------------

PASSIVES = [
    dict(code="pas_vigor", name="활력", description="전투 시작 시 파티 전원의 최대 HP가 소폭 증가합니다.",
         rarity=3, trigger="always", effects=[{"op": "buff", "stat": "defense", "amount": 1, "duration": -1}]),
    dict(code="pas_edge", name="예리함", description="전투 내내 공격력이 증가합니다.",
         rarity=5, trigger="always", effects=[{"op": "buff", "stat": "attack", "amount": 2, "duration": -1}]),
]

# ---------------------------------------------------------------------------
# 적 (§2.6 — 노드/스테이지에 따라 등장 수가 달라진다)
# ---------------------------------------------------------------------------

ENEMIES = [
    dict(code="slime", name="슬라임", tier=EnemyTier.NORMAL, hp=26, attack=4, defense=0, speed=7,
         moves=[
             {"name": "몸통 박치기", "weight": 3, "target": "enemy_single",
              "effects": [{"op": "damage", "amount": 2}]},
             {"name": "굳히기", "weight": 1, "target": "self",
              "effects": [{"op": "block", "amount": 5}]},
         ]),
    dict(code="goblin", name="고블린", tier=EnemyTier.NORMAL, hp=34, attack=6, defense=1, speed=11,
         moves=[
             {"name": "찌르기", "weight": 3, "target": "enemy_single",
              "effects": [{"op": "damage", "amount": 3}]},
             {"name": "위협", "weight": 1, "target": "enemy_single",
              "effects": [{"op": "debuff", "stat": "attack", "amount": 2, "duration": 2}]},
         ]),
    dict(code="wolf", name="늑대", tier=EnemyTier.NORMAL, hp=28, attack=7, defense=0, speed=16,
         moves=[
             {"name": "물어뜯기", "weight": 4, "target": "enemy_single",
              "effects": [{"op": "damage", "amount": 1, "hits": 2}]},
         ]),
    dict(code="stone_golem", name="바위 골렘", tier=EnemyTier.ELITE, hp=70, attack=9, defense=4, speed=5,
         moves=[
             {"name": "내려찍기", "weight": 3, "target": "enemy_single",
              "effects": [{"op": "damage", "amount": 6}]},
             {"name": "지진", "weight": 1, "target": "enemy_all",
              "effects": [{"op": "damage", "amount": 1}]},
         ]),
    dict(code="dread_knight", name="공포의 기사", tier=EnemyTier.BOSS, hp=180, attack=12, defense=5, speed=13,
         moves=[
             {"name": "대검 강타", "weight": 3, "target": "enemy_single",
              "effects": [{"op": "damage", "amount": 8}]},
             {"name": "광역 참격", "weight": 2, "target": "enemy_all",
              "effects": [{"op": "damage", "amount": 3}]},
             {"name": "전투 태세", "weight": 1, "target": "self",
              "effects": [{"op": "block", "amount": 14},
                          {"op": "buff", "stat": "attack", "amount": 3, "duration": 3}]},
         ]),
]

# ---------------------------------------------------------------------------
# 장비 (§7.2, §8)
# ---------------------------------------------------------------------------

EQUIPMENT = [
    dict(code="eq_ring", name="수련의 반지", description="공격력이 오릅니다.",
         slot="accessory", base_stats={"attack": 2}, max_tier=5, price_coin=800, price_carta=0),
    dict(code="eq_plate", name="강철 흉갑", description="최대 HP가 오릅니다.",
         slot="armor", base_stats={"hp": 12}, max_tier=5, price_coin=1200, price_carta=0),
    dict(code="eq_boots", name="바람의 신발", description="속도가 오릅니다.",
         slot="boots", base_stats={"speed": 3}, max_tier=5, price_coin=0, price_carta=200),
]

# ---------------------------------------------------------------------------
# 연구 노드 (§9)
#   §9 확정 범위: 파티 슬롯 확장, 패시브 슬롯 확장, 스탯 강화, 신규 노드 해금.
#   TBD: 전체 트리와 비용.
# ---------------------------------------------------------------------------

RESEARCH = [
    dict(code="party_2", name="파티 슬롯 II", description="파티를 2명까지 편성할 수 있습니다.",
         requires=[], max_level=1, effect={"op": "party_slot", "value": 2},
         costs=[{"coin": 2000, "fragment": 20, "wildcard": 0}]),
    dict(code="party_3", name="파티 슬롯 III", description="파티를 3명까지 편성할 수 있습니다.",
         requires=["party_2"], max_level=1, effect={"op": "party_slot", "value": 3},
         costs=[{"coin": 6000, "fragment": 60, "wildcard": 3}]),
    dict(code="passive_3", name="패시브 슬롯 III", description="패시브 카드를 3장까지 장착합니다.",
         requires=[], max_level=1, effect={"op": "passive_slot", "value": 3},
         costs=[{"coin": 3000, "fragment": 30, "wildcard": 1}]),
    dict(code="passive_4", name="패시브 슬롯 IV", description="패시브 카드를 4장까지 장착합니다.",
         requires=["passive_3"], max_level=1, effect={"op": "passive_slot", "value": 4},
         costs=[{"coin": 9000, "fragment": 90, "wildcard": 5}]),
    dict(code="stat_hp", name="체력 단련", description="파티 전원의 최대 HP가 레벨당 5% 증가합니다.",
         requires=[], max_level=5, effect={"op": "stat", "stat": "hp", "percent": 0.05},
         costs=[{"coin": 1000 * i, "fragment": 10 * i, "wildcard": 0} for i in range(1, 6)]),
    dict(code="stat_attack", name="무기 연마", description="파티 전원의 공격력이 레벨당 4% 증가합니다.",
         requires=[], max_level=5, effect={"op": "stat", "stat": "attack", "percent": 0.04},
         costs=[{"coin": 1200 * i, "fragment": 12 * i, "wildcard": 0} for i in range(1, 6)]),
]

BANNERS = [
    dict(code="standard", name="상시 모집", type=BannerType.STANDARD,
         pickup_characters=[], pickup_cards=[], pool_characters=[], pool_cards=[]),
    dict(code="pickup_aria", name="한정 픽업: 아리아", type=BannerType.LIMITED,
         pickup_characters=["aria"], pickup_cards=[], pool_characters=[], pool_cards=[]),
]


def _upsert(session: Session, model, rows: list[dict]) -> int:
    """code 기준으로 없으면 추가한다. 기존 행은 건드리지 않는다.

    운영자가 대시보드에서 수정한 값을 시드가 덮어쓰지 않도록 하기 위함이다.
    """
    created = 0
    for row in rows:
        exists = session.scalar(select(model).where(model.code == row["code"]))
        if exists is None:
            session.add(model(**row))
            created += 1
    return created


def seed_all(session: Session) -> dict[str, int]:
    counts = {
        "characters": _upsert(session, Character, CHARACTERS),
        "cards": _upsert(session, Card, CARDS),
        "passives": _upsert(session, PassiveCard, PASSIVES),
        "enemies": _upsert(session, Enemy, ENEMIES),
        "equipment": _upsert(session, Equipment, EQUIPMENT),
        "research": _upsert(session, ResearchNode, RESEARCH),
        "banners": _upsert(session, Banner, BANNERS),
    }
    session.commit()
    return counts
