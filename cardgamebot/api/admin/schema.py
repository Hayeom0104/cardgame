"""대시보드가 편집할 엔티티 정의 (설계 문서 §10.1).

관리 대상은 **카드 / 적 / 캐릭터** 세 가지다. 장비 관리는 §10.1 에서 명시적으로
범위 밖이라 여기 넣지 않았다(추후 필요해지면 EntitySpec 하나만 추가하면 된다).

폼은 이 스펙에서 자동 생성된다. 새 필드를 추가할 때 템플릿을 고칠 필요가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...db.models import Card, CardKind, Character, Enemy, EnemyTier, TargetType
from ...game.effects import supported_ops


@dataclass
class Field:
    name: str
    label: str
    type: str = "text"       # text | textarea | int | bool | select | json | image
    options: list[tuple[str, str]] = field(default_factory=list)
    required: bool = False
    help: str = ""
    default: Any = ""


@dataclass
class EntitySpec:
    key: str
    label: str
    model: type
    fields: list[Field]
    image_field: str | None = None
    list_columns: list[str] = field(default_factory=list)


_EFFECT_HELP = (
    "효과 JSON 배열. 예: [{\"op\": \"damage\", \"amount\": 8}]  "
    f"사용 가능한 op: {', '.join(supported_ops())}. "
    "※ 상태 효과(화상/기절 등) 분류는 설계 미확정(TBD)이라 아직 op 가 없습니다."
)


CHARACTER_SPEC = EntitySpec(
    key="characters",
    label="캐릭터",
    model=Character,
    image_field="portrait_path",
    list_columns=["code", "name", "base_rarity", "max_star", "base_hp", "base_attack"],
    fields=[
        Field("code", "코드", "text", required=True, help="영문/숫자 식별자. 등록 후 변경 불가."),
        Field("name", "이름", "text", required=True),
        Field("description", "설명", "textarea"),
        Field("base_rarity", "기본 성급", "int", default=1, help="가챠에서 나오는 초기 성급 (1~3)"),
        Field("max_star", "최대 성급", "int", default=3,
              help="일반 캐릭터는 3. 특별 캐릭터만 6까지 (설계 §4.4)"),
        Field("base_hp", "기본 HP", "int", default=50),
        Field("base_attack", "기본 공격력", "int", default=10),
        Field("base_defense", "기본 방어력", "int", default=0),
        Field("base_speed", "기본 속도", "int", default=10),
        Field("portrait_path", "초상화", "image", help="PNG/JPG/WEBP 업로드"),
        Field("is_active", "활성화", "bool", default=True),
    ],
)


CARD_SPEC = EntitySpec(
    key="cards",
    label="카드",
    model=Card,
    image_field="art_path",
    list_columns=["code", "name", "character_code", "kind", "cost", "rarity"],
    fields=[
        Field("code", "코드", "text", required=True),
        Field("name", "이름", "text", required=True),
        Field("description", "설명", "textarea"),
        Field("character_code", "전용 캐릭터", "text",
              help="비워두면 전 캐릭터 공용 카드 (설계 §5.1)"),
        Field("kind", "종류", "select",
              options=[(k.value, k.value) for k in CardKind], default=CardKind.ATTACK.value),
        Field("target", "대상", "select",
              options=[(t.value, t.value) for t in TargetType],
              default=TargetType.ENEMY_SINGLE.value,
              help="타게팅은 카드마다 개별 정의된다 (설계 §2.5)"),
        Field("cost", "비용", "int", default=1, help="파티 공유 자원 소모량 (설계 §2.3)"),
        Field("rarity", "등급", "int", default=1, help="1~6 (설계 §5.6)"),
        Field("effects", "효과", "json", default="[]", help=_EFFECT_HELP),
        Field("is_starter", "기본 카드", "bool",
              help="체크 시 가챠 없이도 시작 덱에 포함된다 (설계 §4.3)"),
        Field("in_gacha_pool", "가챠 풀 포함", "bool", default=True),
        Field("art_path", "카드 아트", "image"),
        Field("is_active", "활성화", "bool", default=True),
    ],
)


ENEMY_SPEC = EntitySpec(
    key="enemies",
    label="적",
    model=Enemy,
    image_field="sprite_path",
    list_columns=["code", "name", "tier", "hp", "attack", "speed"],
    fields=[
        Field("code", "코드", "text", required=True),
        Field("name", "이름", "text", required=True),
        Field("description", "설명", "textarea"),
        Field("tier", "등급", "select",
              options=[(t.value, t.value) for t in EnemyTier], default=EnemyTier.NORMAL.value),
        Field("hp", "HP", "int", default=30),
        Field("attack", "공격력", "int", default=6),
        Field("defense", "방어력", "int", default=0),
        Field("speed", "속도", "int", default=8),
        Field("moves", "행동 패턴", "json", default="[]",
              help="[{\"name\": \"공격\", \"weight\": 3, \"target\": \"enemy_single\", "
                   "\"effects\": [{\"op\": \"damage\", \"amount\": 5}]}] "
                   "※ 적 AI 패턴은 설계 미확정(TBD)이라 가중치 랜덤으로만 동작합니다."),
        Field("sprite_path", "스프라이트", "image"),
        Field("is_active", "활성화", "bool", default=True),
    ],
)


SPECS: dict[str, EntitySpec] = {
    s.key: s for s in (CHARACTER_SPEC, CARD_SPEC, ENEMY_SPEC)
}
