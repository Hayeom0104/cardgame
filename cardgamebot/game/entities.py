"""전투 런타임 엔티티.

전투 상태는 `Run.battle` JSON 컬럼에 통째로 직렬화된다. 모든 dataclass 는
`to_dict()` / `from_dict()` 왕복이 가능해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    PLAYER = "player"
    ENEMY = "enemy"


@dataclass
class Modifier:
    """§2.5 버프/디버프 — 지속 턴이 있는 스탯 보정.

    `duration` 은 소유 유닛의 턴 시작마다 1씩 감소하고 0이 되면 제거된다.
    `duration = -1` 은 전투 종료까지 유지(패시브/장비 유래).
    """

    stat: str          # "attack" | "defense" | "speed"
    amount: int
    duration: int = 1
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "stat": self.stat,
            "amount": self.amount,
            "duration": self.duration,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Modifier":
        return cls(
            stat=data["stat"],
            amount=int(data["amount"]),
            duration=int(data.get("duration", 1)),
            source=data.get("source", ""),
        )


@dataclass
class Unit:
    """전투에 참여하는 유닛 하나 (아군 캐릭터 또는 적)."""

    uid: str                 # 전투 내 고유 ID ("p0", "e2" ...)
    code: str                # Character.code 또는 Enemy.code
    name: str
    side: Side

    hp: int
    max_hp: int
    attack: int
    defense: int
    speed: int

    # §2.5: 방어는 영구 스탯 상승이 아니라 턴 시작 시 사라지는 블록이다.
    block: int = 0
    modifiers: list[Modifier] = field(default_factory=list)

    # 플레이어 유닛 전용 — §2.2 캐릭터별 드로우 더미
    draw_pile: list[str] = field(default_factory=list)
    discard_pile: list[str] = field(default_factory=list)

    # 적 전용 — 다음에 사용할 행동 인덱스(의도 표시용)
    intent: dict | None = None

    star: int = 1
    sprite_path: str | None = None

    # ---------------- 파생 스탯 ----------------

    def stat(self, name: str) -> int:
        base = getattr(self, name)
        bonus = sum(m.amount for m in self.modifiers if m.stat == name)
        return max(0, base + bonus)

    @property
    def alive(self) -> bool:
        return self.hp > 0

    # ---------------- 상태 변화 ----------------

    def take_damage(self, amount: int) -> tuple[int, int]:
        """블록 → HP 순으로 데미지를 적용한다.

        Returns: (블록으로 막은 양, 실제 HP 감소량)
        """
        if amount <= 0:
            return (0, 0)
        blocked = min(self.block, amount)
        self.block -= blocked
        remaining = amount - blocked
        dealt = min(self.hp, remaining)
        self.hp -= dealt
        return (blocked, dealt)

    def heal(self, amount: int) -> int:
        if amount <= 0:
            return 0
        before = self.hp
        self.hp = min(self.max_hp, self.hp + amount)
        return self.hp - before

    def gain_block(self, amount: int) -> int:
        if amount <= 0:
            return 0
        self.block += amount
        return amount

    def tick_modifiers(self) -> None:
        """턴 시작 시 지속시간 감소. duration<0 은 영구."""
        remaining: list[Modifier] = []
        for m in self.modifiers:
            if m.duration < 0:
                remaining.append(m)
                continue
            m.duration -= 1
            if m.duration > 0:
                remaining.append(m)
        self.modifiers = remaining

    # ---------------- 직렬화 ----------------

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "code": self.code,
            "name": self.name,
            "side": self.side.value,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "attack": self.attack,
            "defense": self.defense,
            "speed": self.speed,
            "block": self.block,
            "modifiers": [m.to_dict() for m in self.modifiers],
            "draw_pile": list(self.draw_pile),
            "discard_pile": list(self.discard_pile),
            "intent": self.intent,
            "star": self.star,
            "sprite_path": self.sprite_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Unit":
        return cls(
            uid=data["uid"],
            code=data["code"],
            name=data["name"],
            side=Side(data["side"]),
            hp=int(data["hp"]),
            max_hp=int(data["max_hp"]),
            attack=int(data["attack"]),
            defense=int(data["defense"]),
            speed=int(data["speed"]),
            block=int(data.get("block", 0)),
            modifiers=[Modifier.from_dict(m) for m in data.get("modifiers", [])],
            draw_pile=list(data.get("draw_pile", [])),
            discard_pile=list(data.get("discard_pile", [])),
            intent=data.get("intent"),
            star=int(data.get("star", 1)),
            sprite_path=data.get("sprite_path"),
        )


@dataclass
class CardView:
    """전투 중 플레이어에게 보여주는 카드 스냅샷.

    Card 마스터 데이터의 읽기 전용 사본이며, 렌더러와 커맨드 응답이 공유한다.
    """

    code: str
    name: str
    description: str
    kind: str
    target: str
    cost: int
    rarity: int
    effects: list
    art_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "target": self.target,
            "cost": self.cost,
            "rarity": self.rarity,
            "effects": self.effects,
            "art_path": self.art_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CardView":
        return cls(
            code=data["code"],
            name=data["name"],
            description=data.get("description", ""),
            kind=data["kind"],
            target=data["target"],
            cost=int(data.get("cost", 0)),
            rarity=int(data.get("rarity", 1)),
            effects=data.get("effects", []),
            art_path=data.get("art_path"),
        )

    @classmethod
    def from_model(cls, card) -> "CardView":
        return cls(
            code=card.code,
            name=card.name,
            description=card.description or "",
            kind=card.kind.value if hasattr(card.kind, "value") else str(card.kind),
            target=card.target.value if hasattr(card.target, "value") else str(card.target),
            cost=card.cost,
            rarity=card.rarity,
            effects=list(card.effects or []),
            art_path=card.art_path,
        )
