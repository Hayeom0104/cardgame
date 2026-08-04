"""SQLAlchemy 모델.

테이블은 설계 문서 §12 "Persistent vs Run-Scoped Data" 를 그대로 반영해
두 그룹으로 나뉜다.

* 영속(계정) 데이터 — 캐릭터/카드 보유, 성급, 재화, 장비, 연구 해금
* 런 스코프 데이터 — 액티브 덱, 패시브 장착, 맵, 전투 상태, 노드 상점

코인/XP/업적은 이 DB에 저장하지 않는다(§1.1 — 중앙봇 소유).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# 열거형
# ---------------------------------------------------------------------------


class CardKind(str, enum.Enum):
    """§2.5 카드 효과 대분류."""

    ATTACK = "attack"
    DEFENSE = "defense"
    BUFF = "buff"
    DEBUFF = "debuff"
    HEAL = "heal"


class TargetType(str, enum.Enum):
    """§2.5: 타게팅은 카드마다 개별 정의된다(전역 규칙 아님)."""

    ENEMY_SINGLE = "enemy_single"
    ENEMY_ALL = "enemy_all"
    ALLY_SINGLE = "ally_single"
    ALLY_ALL = "ally_all"
    SELF = "self"


class EnemyTier(str, enum.Enum):
    NORMAL = "normal"
    ELITE = "elite"
    BOSS = "boss"


class NodeType(str, enum.Enum):
    """§3.1 확정된 노드 목록."""

    COMBAT = "combat"
    REWARD = "reward"
    REST = "rest"
    SHOP = "shop"
    EVENT = "event"
    BOSS = "boss"


class RunStatus(str, enum.Enum):
    ACTIVE = "active"
    CLEARED = "cleared"
    FAILED = "failed"
    ABANDONED = "abandoned"


class BannerType(str, enum.Enum):
    """§5.4 상시 배너 + 한정 픽업 배너."""

    STANDARD = "standard"
    LIMITED = "limited"


class AdminRole(str, enum.Enum):
    """§10.2 권한 분리 (세부 granularity 는 TBD)."""

    OWNER = "owner"
    EDITOR = "editor"


# ---------------------------------------------------------------------------
# 콘텐츠 테이블 — 관리자 대시보드(§10)가 CRUD 하는 대상
# ---------------------------------------------------------------------------


class Character(Base):
    """플레이어블 캐릭터 마스터 데이터."""

    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")

    # §4.4 블루아카이브식 성급. base_rarity 는 가챠에서 나오는 초기 성급.
    base_rarity: Mapped[int] = mapped_column(Integer, default=1)
    # §4.4: 일부 특정 캐릭터만 6성까지 상승 가능.
    max_star: Mapped[int] = mapped_column(Integer, default=3)

    base_hp: Mapped[int] = mapped_column(Integer, default=50)
    base_attack: Mapped[int] = mapped_column(Integer, default=10)
    base_defense: Mapped[int] = mapped_column(Integer, default=0)
    base_speed: Mapped[int] = mapped_column(Integer, default=10)

    portrait_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    cards: Mapped[list["Card"]] = relationship(back_populates="character")


class Card(Base):
    """전투 카드 마스터 데이터.

    `character_code` 가 None 이면 §5.1 의 "universal card"(캐릭터 공용)다.
    """

    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")

    character_code: Mapped[str | None] = mapped_column(
        ForeignKey("characters.code", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[CardKind] = mapped_column(Enum(CardKind), default=CardKind.ATTACK)
    target: Mapped[TargetType] = mapped_column(Enum(TargetType), default=TargetType.ENEMY_SINGLE)

    cost: Mapped[int] = mapped_column(Integer, default=1)          # §2.3
    rarity: Mapped[int] = mapped_column(Integer, default=1)        # 1..6 (§5.6)

    # 효과는 데이터 주도(data-driven). game/effects.py 의 스펙 참조.
    # 예: [{"op": "damage", "amount": 8}, {"op": "block", "amount": 4}]
    effects: Mapped[list] = mapped_column(JSON, default=list)

    # §4.3: 모든 캐릭터가 가챠 없이도 갖고 시작하는 기본 카드.
    is_starter: Mapped[bool] = mapped_column(Boolean, default=False)
    # 가챠 풀에 포함되는지. 기본 카드는 가챠에 나오지 않는다.
    in_gacha_pool: Mapped[bool] = mapped_column(Boolean, default=True)

    art_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    character: Mapped[Character | None] = relationship(back_populates="cards")


class PassiveCard(Base):
    """§6 패시브 카드 마스터 데이터."""

    __tablename__ = "passive_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")

    rarity: Mapped[int] = mapped_column(Integer, default=1)  # 1..6 (§5.6/§6)

    # §6: "상시 패시브" 와 "전투 중 발동 조건" 두 패턴이 공존한다.
    #  - trigger="always"      → 전투 내내 적용
    #  - trigger="on_<event>"  → 해당 이벤트에서 발동
    trigger: Mapped[str] = mapped_column(String(32), default="always")
    effects: Mapped[list] = mapped_column(JSON, default=list)

    art_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Enemy(Base):
    """적 마스터 데이터."""

    __tablename__ = "enemies"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")

    tier: Mapped[EnemyTier] = mapped_column(Enum(EnemyTier), default=EnemyTier.NORMAL)
    hp: Mapped[int] = mapped_column(Integer, default=30)
    attack: Mapped[int] = mapped_column(Integer, default=6)
    defense: Mapped[int] = mapped_column(Integer, default=0)
    speed: Mapped[int] = mapped_column(Integer, default=8)

    # TBD (§2.7): 적 AI 행동 패턴 미확정. 지금은 행동 목록을 가중치로 순환하는
    # 최소 구현이며, 확정 시 game/enemy_ai.py 만 교체하면 된다.
    # 예: [{"weight": 3, "effects": [{"op": "damage", "amount": 6}]}]
    moves: Mapped[list] = mapped_column(JSON, default=list)

    sprite_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Equipment(Base):
    """§8 장비 마스터 데이터."""

    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")

    slot: Mapped[str] = mapped_column(String(32), default="generic")  # TBD (§8)
    # 티어당 스탯. {"hp": 10, "attack": 2} 가 티어 1 기준이고 티어마다 누적.
    base_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    max_tier: Mapped[int] = mapped_column(Integer, default=5)

    price_coin: Mapped[int] = mapped_column(Integer, default=0)   # TBD (§7.2)
    price_carta: Mapped[int] = mapped_column(Integer, default=0)  # TBD (§7.2)

    art_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Banner(Base):
    """§5.4 가챠 배너."""

    __tablename__ = "banners"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[BannerType] = mapped_column(Enum(BannerType), default=BannerType.STANDARD)

    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 픽업 대상 코드 목록. TBD (§5.4): 픽업이 캐릭터 전용인지 카드도 되는지 미확정
    # 이므로, 스키마는 둘 다 담을 수 있게 두고 운영에서 결정한다.
    pickup_characters: Mapped[list] = mapped_column(JSON, default=list)
    pickup_cards: Mapped[list] = mapped_column(JSON, default=list)

    # 비어 있으면 활성화된 전체 캐릭터/카드를 풀로 사용한다.
    pool_characters: Mapped[list] = mapped_column(JSON, default=list)
    pool_cards: Mapped[list] = mapped_column(JSON, default=list)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ResearchNode(Base):
    """§9 연구 시스템 노드.

    §9: 즉시 해금(타이머 없음), 기존 재화(코인/카드 조각/와일드카드)만 사용.
    전체 노드 목록과 비용은 TBD 이므로 이 테이블은 데이터로 채운다.
    """

    __tablename__ = "research_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")

    # 선행 노드 코드 목록 (테크 트리)
    requires: Mapped[list] = mapped_column(JSON, default=list)
    max_level: Mapped[int] = mapped_column(Integer, default=1)

    # 레벨별 비용. [{"coin": 500, "fragment": 20, "wildcard": 0}, ...]
    costs: Mapped[list] = mapped_column(JSON, default=list)

    # 해금 효과. 예: {"op": "party_slot", "value": 2}
    effect: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------------------
# 플레이어 영속 데이터 (§12 Permanent)
# ---------------------------------------------------------------------------


class User(Base):
    """플레이어 계정.

    코인/XP/레벨은 여기 없다 — 중앙봇 소유(§1.1). 여기에는 이 봇만 아는
    게임 전용 재화만 둔다.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")

    carta: Mapped[int] = mapped_column(Integer, default=0)        # §5.5
    fragments: Mapped[int] = mapped_column(Integer, default=0)    # 카드 조각 §5.2
    wildcards: Mapped[int] = mapped_column(Integer, default=0)    # 와일드카드 §5.2
    # TBD (§8): 장비 강화 전용 재료. 이름 미확정이라 식별자만 잡아둔다.
    equip_material: Mapped[int] = mapped_column(Integer, default=0)

    last_daily_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    characters: Mapped[list["UserCharacter"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    unlocked_cards: Mapped[list["UserCard"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserCharacter(Base):
    """캐릭터 보유 + 성급 (§4.4). 영속."""

    __tablename__ = "user_characters"
    __table_args__ = (UniqueConstraint("user_id", "character_code", name="uq_user_character"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    character_code: Mapped[str] = mapped_column(String(64), index=True)
    star: Mapped[int] = mapped_column(Integer, default=1)
    obtained_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="characters")


class UserCard(Base):
    """§5.3 카드 영구 해금 기록.

    한 번이라도 가챠에서 뽑은 카드는 이후 모든 런의 보상 노드(§3.2) 선택지에
    등장할 자격을 얻는다. 현재 런 덱에 들어있는지와는 무관하다.
    """

    __tablename__ = "user_cards"
    __table_args__ = (UniqueConstraint("user_id", "card_code", name="uq_user_card"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    card_code: Mapped[str] = mapped_column(String(64), index=True)
    pull_count: Mapped[int] = mapped_column(Integer, default=1)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="unlocked_cards")


class UserPassive(Base):
    """§6 패시브 카드 보유.

    가챠로 '해금'된 뒤 인게임 플레이(런 보상)로 실제 '획득'한다.
    """

    __tablename__ = "user_passives"
    __table_args__ = (UniqueConstraint("user_id", "passive_code", name="uq_user_passive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    passive_code: Mapped[str] = mapped_column(String(64), index=True)
    unlocked: Mapped[bool] = mapped_column(Boolean, default=True)   # 가챠 해금 여부
    owned: Mapped[int] = mapped_column(Integer, default=0)          # 런 보상으로 실제 획득한 수


class UserEquipment(Base):
    """§8 장비 보유 + 티어. 영속."""

    __tablename__ = "user_equipment"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    equipment_code: Mapped[str] = mapped_column(String(64), index=True)
    tier: Mapped[int] = mapped_column(Integer, default=1)
    # 어떤 캐릭터에 장착 중인지. None 이면 보관함.
    equipped_on: Mapped[str | None] = mapped_column(String(64), nullable=True)


class UserResearch(Base):
    """§9 연구 해금 상태. 영속."""

    __tablename__ = "user_research"
    __table_args__ = (UniqueConstraint("user_id", "node_code", name="uq_user_research"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    node_code: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GachaState(Base):
    """§5.4 배너별 천장/50:50 상태."""

    __tablename__ = "gacha_state"
    __table_args__ = (UniqueConstraint("user_id", "banner_code", name="uq_gacha_state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    banner_code: Mapped[str] = mapped_column(String(64), index=True)
    pity: Mapped[int] = mapped_column(Integer, default=0)
    # §5.4: 직전 최고등급이 픽업이 아니었으면 다음 최고등급은 픽업 확정.
    guaranteed_pickup: Mapped[bool] = mapped_column(Boolean, default=False)
    total_pulls: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# 런 스코프 데이터 (§12 Run-scoped)
# ---------------------------------------------------------------------------


class Run(Base):
    """진행 중인 런 하나.

    맵/전투/상점 상태는 JSON 으로 보관한다. 런 종료 시 통째로 폐기되므로
    정규화 이득이 없고, 전투 스냅샷을 원자적으로 저장하기에 유리하다.
    """

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.ACTIVE, index=True)
    seed: Mapped[int] = mapped_column(Integer, default=0)

    # §3: 맵은 런마다 새로 생성된다.
    map_data: Mapped[dict] = mapped_column(JSON, default=dict)
    current_node_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cleared_node_ids: Mapped[list] = mapped_column(JSON, default=list)

    # §2.4: HP 는 노드 간 유지되고 런 종료 시 리셋된다.
    party: Mapped[list] = mapped_column(JSON, default=list)

    # §3.2 런 스코프 액티브 덱: {character_code: [card_code, ...]}
    decks: Mapped[dict] = mapped_column(JSON, default=dict)
    # §6 런 스코프 패시브 장착
    equipped_passives: Mapped[list] = mapped_column(JSON, default=list)
    # §7.1 노드 상점 진열/구매 내역 (런 종료 시 소멸)
    shop_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # 소모품 등 런 내 임시 상태
    inventory: Mapped[list] = mapped_column(JSON, default=list)

    battle: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# 관리자 대시보드 (§10)
# ---------------------------------------------------------------------------


class AdminUser(Base):
    """§10.2 디스코드 OAuth 로그인 사용자와 권한."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    avatar: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole), default=AdminRole.EDITOR)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    """§10.2 다중 사용자 환경이므로 콘텐츠 변경 이력을 남긴다."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_discord_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(32))       # create / update / delete
    entity: Mapped[str] = mapped_column(String(32))       # card / enemy / character ...
    entity_code: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


__all__ = [
    "Base",
    "AdminRole",
    "AdminUser",
    "AuditLog",
    "Banner",
    "BannerType",
    "Card",
    "CardKind",
    "Character",
    "Enemy",
    "EnemyTier",
    "Equipment",
    "GachaState",
    "NodeType",
    "PassiveCard",
    "ResearchNode",
    "Run",
    "RunStatus",
    "TargetType",
    "User",
    "UserCard",
    "UserCharacter",
    "UserEquipment",
    "UserPassive",
    "UserResearch",
    "utcnow",
]
