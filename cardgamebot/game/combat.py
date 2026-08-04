"""전투 엔진 (설계 문서 §2).

구현된 확정 규칙:

* §2.1 개별 턴 — 파티 단위가 아니라 캐릭터 하나씩 행동한다.
* §2.1 교차 턴 — 플레이어와 적이 한 유닛씩 번갈아 행동한다.
* §2.2 매 턴 여러 장 드로우 → 1장 선택, 나머지는 턴 종료 시 버림.
* §2.2 드로우 더미 고갈 시 재섞기 없음. 더 못 뽑는다.
* §2.3 자원은 파티 공유. 라운드마다 완전 회복.
* §2.4 캐릭터별 개별 HP. (노드 간 유지는 RunService 담당)
* §2.5 방어는 턴 시작 시 사라지는 블록.
* §2.6 적 수는 스테이지마다 다르고 상한이 없다.

엔진은 DB 를 모른다. 카드 카탈로그를 주입받고, 전투 스냅샷을 JSON 으로
왕복 직렬화한다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from .. import balance
from . import enemy_ai
from .effects import EffectContext, resolve_effects
from .entities import CardView, Side, Unit


class BattlePhase(str, Enum):
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"   # 플레이어가 카드를 고를 차례
    WON = "won"
    LOST = "lost"


@dataclass
class PendingChoice:
    """§2.2 이번 턴에 뽑아 제시한 카드들."""

    unit_uid: str
    drawn: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"unit_uid": self.unit_uid, "drawn": list(self.drawn)}

    @classmethod
    def from_dict(cls, data: dict) -> "PendingChoice":
        return cls(unit_uid=data["unit_uid"], drawn=list(data.get("drawn", [])))


class CombatError(Exception):
    """플레이어 입력이 규칙에 어긋날 때."""


class CombatEngine:
    def __init__(
        self,
        units: list[Unit],
        catalog: dict[str, CardView],
        enemy_moves: dict[str, list],
        resource_max: int = balance.RESOURCE_POOL_MAX,
        draw_per_turn: int = balance.DRAW_PER_TURN,
        seed: int | None = None,
    ) -> None:
        self.units = units
        self.catalog = catalog
        self.enemy_moves = enemy_moves
        self.resource_max = resource_max
        self.resource = resource_max          # §2.3 1라운드는 가득 찬 상태로 시작
        self.draw_per_turn = draw_per_turn

        self.round_number = 0
        self.turn_queue: list[str] = []
        self.phase = BattlePhase.RUNNING
        self.pending: PendingChoice | None = None
        self.log: list[str] = []
        self.rng = random.Random(seed)

    # ------------------------------------------------------------------
    # 조회 헬퍼
    # ------------------------------------------------------------------

    @property
    def players(self) -> list[Unit]:
        return [u for u in self.units if u.side is Side.PLAYER]

    @property
    def enemies(self) -> list[Unit]:
        return [u for u in self.units if u.side is Side.ENEMY]

    def unit(self, uid: str) -> Unit:
        for u in self.units:
            if u.uid == uid:
                return u
        raise CombatError(f"유닛 `{uid}` 을(를) 찾을 수 없습니다.")

    @property
    def active_unit(self) -> Unit | None:
        if self.pending is None:
            return None
        return self.unit(self.pending.unit_uid)

    def drawn_cards(self) -> list[CardView]:
        if self.pending is None:
            return []
        return [self.catalog[c] for c in self.pending.drawn if c in self.catalog]

    # ------------------------------------------------------------------
    # 전투 시작
    # ------------------------------------------------------------------

    @classmethod
    def start(
        cls,
        units: list[Unit],
        catalog: dict[str, CardView],
        enemy_moves: dict[str, list],
        resource_max: int = balance.RESOURCE_POOL_MAX,
        draw_per_turn: int = balance.DRAW_PER_TURN,
        seed: int | None = None,
    ) -> "CombatEngine":
        engine = cls(units, catalog, enemy_moves, resource_max, draw_per_turn, seed)
        for u in engine.players:
            engine.rng.shuffle(u.draw_pile)
        engine.log.append("⚔️ 전투 시작!")
        engine.advance()
        return engine

    # ------------------------------------------------------------------
    # 턴 진행
    # ------------------------------------------------------------------

    def _begin_round(self) -> None:
        """§2.3 라운드 시작 — 자원 완전 회복 + 턴 순서 재계산."""
        self.round_number += 1
        self.resource = self.resource_max

        alive = [u for u in self.units if u.alive]
        if balance.TURN_ORDER_MODE == "speed":
            # TBD (§2.1): 속도 기준 정렬은 임시 규칙. 동속은 플레이어 우선.
            alive.sort(key=lambda u: (-u.stat("speed"), u.side is not Side.PLAYER, u.uid))
        self.turn_queue = [u.uid for u in alive]

        self.log.append(f"— 라운드 {self.round_number} 시작 (자원 {self.resource}/{self.resource_max}) —")

    def _check_end(self) -> bool:
        if not any(u.alive for u in self.enemies):
            self.phase = BattlePhase.WON
            self.log.append("🏆 승리!")
            return True
        if not any(u.alive for u in self.players):
            self.phase = BattlePhase.LOST
            self.log.append("☠️ 패배...")
            return True
        return False

    def advance(self) -> list[str]:
        """플레이어 입력이 필요하거나 전투가 끝날 때까지 진행한다."""
        produced: list[str] = []
        guard = 0

        while self.phase is BattlePhase.RUNNING:
            guard += 1
            if guard > 1000:  # 데이터 이상으로 인한 무한 루프 방지
                self.log.append("⚠️ 전투가 비정상적으로 길어져 중단되었습니다.")
                break

            if self._check_end():
                break

            if not self.turn_queue:
                self._begin_round()
                continue

            uid = self.turn_queue.pop(0)
            unit = self.unit(uid)
            if not unit.alive:
                continue

            self._start_turn(unit)

            if unit.side is Side.PLAYER:
                self._offer_cards(unit)
                self.phase = BattlePhase.AWAITING_INPUT
                break

            self._enemy_turn(unit)

        produced = self.log
        return produced

    def _start_turn(self, unit: Unit) -> None:
        """§2.5 블록은 자기 턴 시작 시 사라진다. 버프/디버프 지속시간도 감소."""
        if unit.block:
            unit.block = 0
        unit.tick_modifiers()

    def _offer_cards(self, unit: Unit) -> None:
        """§2.2 여러 장 뽑아 제시한다. 더미가 비면 뽑을 수 있는 만큼만."""
        drawn: list[str] = []
        for _ in range(self.draw_per_turn):
            if not unit.draw_pile:
                break
            drawn.append(unit.draw_pile.pop(0))
        self.pending = PendingChoice(unit_uid=unit.uid, drawn=drawn)

        if not drawn:
            # §2.2 드로우 더미 고갈. TBD: 추가 페널티 미확정이라 넣지 않는다.
            self.log.append(f"🃏 {unit.name}: 드로우 더미가 비어 카드를 뽑지 못했습니다.")

    def _enemy_turn(self, enemy: Unit) -> None:
        moves = self.enemy_moves.get(enemy.code, [])
        move = enemy_ai.choose_move(enemy, moves, self.rng)
        targets = enemy_ai.choose_targets(move, enemy, self.players, self.enemies, self.rng)
        if not targets:
            return
        self.log.append(f"👹 {enemy.name}: {move.get('name', '행동')}")
        self.log.extend(resolve_effects(enemy, targets, move.get("effects", []), EffectContext(self)))

    # ------------------------------------------------------------------
    # 플레이어 입력
    # ------------------------------------------------------------------

    def play(self, choice: int, target_uid: str | None = None) -> list[str]:
        """제시된 카드 중 `choice` (1-based) 를 사용한다."""
        if self.phase is not BattlePhase.AWAITING_INPUT or self.pending is None:
            raise CombatError("지금은 카드를 낼 차례가 아닙니다.")

        drawn = self.pending.drawn
        if not (1 <= choice <= len(drawn)):
            raise CombatError(f"1~{len(drawn)} 사이의 번호를 골라주세요.")

        unit = self.unit(self.pending.unit_uid)
        card = self.catalog.get(drawn[choice - 1])
        if card is None:
            raise CombatError("카드 데이터를 찾을 수 없습니다.")

        if card.cost > self.resource:
            raise CombatError(
                f"자원이 부족합니다. (필요 {card.cost}, 보유 {self.resource})"
            )

        targets = self._resolve_targets(card, unit, target_uid)
        if not targets:
            raise CombatError("유효한 대상이 없습니다.")

        self.resource -= card.cost
        self.log.append(
            f"🎴 {unit.name}: [{card.name}] 사용 (자원 -{card.cost} → {self.resource}/{self.resource_max})"
        )
        self.log.extend(resolve_effects(unit, targets, card.effects, EffectContext(self)))

        self._end_turn(unit)
        return self.advance()

    def skip(self) -> list[str]:
        """카드를 내지 않고 턴을 넘긴다."""
        if self.phase is not BattlePhase.AWAITING_INPUT or self.pending is None:
            raise CombatError("지금은 카드를 낼 차례가 아닙니다.")
        unit = self.unit(self.pending.unit_uid)
        self.log.append(f"⏭️ {unit.name}: 턴을 넘깁니다.")
        self._end_turn(unit)
        return self.advance()

    def _end_turn(self, unit: Unit) -> None:
        """§2.2 턴 종료 시 이번에 뽑은 카드는 전부 버린다.

        사용한 1장도, 고르지 않은 나머지도 모두 버림 더미로 간다 — 핸드로
        남지 않는다.
        """
        assert self.pending is not None
        for code in self.pending.drawn:
            unit.discard_pile.append(code)
        self.pending = None
        self.phase = BattlePhase.RUNNING

    def _resolve_targets(self, card: CardView, actor: Unit, target_uid: str | None) -> list[Unit]:
        """§2.5 타게팅은 카드마다 개별 정의된다."""
        alive_enemies = [u for u in self.enemies if u.alive]
        alive_allies = [u for u in self.players if u.alive]

        if card.target == "enemy_all":
            return alive_enemies
        if card.target == "ally_all":
            return alive_allies
        if card.target == "self":
            return [actor]

        if card.target == "enemy_single":
            if target_uid:
                target = self.unit(target_uid)
                if target.side is not Side.ENEMY or not target.alive:
                    raise CombatError("살아있는 적을 대상으로 지정해주세요.")
                return [target]
            return alive_enemies[:1]

        if card.target == "ally_single":
            if target_uid:
                target = self.unit(target_uid)
                if target.side is not Side.PLAYER or not target.alive:
                    raise CombatError("살아있는 아군을 대상으로 지정해주세요.")
                return [target]
            return [actor]

        raise CombatError(f"알 수 없는 타겟 타입 `{card.target}`")

    # ------------------------------------------------------------------
    # 효과에서 호출하는 훅
    # ------------------------------------------------------------------

    def gain_resource(self, amount: int) -> int:
        before = self.resource
        self.resource = min(self.resource_max, self.resource + amount)
        return self.resource - before

    def draw_extra(self, unit: Unit, amount: int) -> list[str]:
        """추가 드로우. §2.2 재섞기가 없으므로 더미가 비면 빈 리스트."""
        drawn: list[str] = []
        for _ in range(amount):
            if not unit.draw_pile:
                break
            drawn.append(unit.draw_pile.pop(0))
        if drawn and self.pending is not None and self.pending.unit_uid == unit.uid:
            self.pending.drawn.extend(drawn)
        return drawn

    # ------------------------------------------------------------------
    # 직렬화 — Run.battle JSON 컬럼에 그대로 들어간다
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "units": [u.to_dict() for u in self.units],
            "catalog": {k: v.to_dict() for k, v in self.catalog.items()},
            "enemy_moves": self.enemy_moves,
            "resource": self.resource,
            "resource_max": self.resource_max,
            "draw_per_turn": self.draw_per_turn,
            "round_number": self.round_number,
            "turn_queue": list(self.turn_queue),
            "phase": self.phase.value,
            "pending": self.pending.to_dict() if self.pending else None,
            "log": self.log[-60:],  # 로그가 무한히 커지지 않게 자른다
            "rng_state": self.rng.getstate(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CombatEngine":
        engine = cls(
            units=[Unit.from_dict(u) for u in data["units"]],
            catalog={k: CardView.from_dict(v) for k, v in data["catalog"].items()},
            enemy_moves=data.get("enemy_moves", {}),
            resource_max=int(data.get("resource_max", balance.RESOURCE_POOL_MAX)),
            draw_per_turn=int(data.get("draw_per_turn", balance.DRAW_PER_TURN)),
        )
        engine.resource = int(data.get("resource", engine.resource_max))
        engine.round_number = int(data.get("round_number", 0))
        engine.turn_queue = list(data.get("turn_queue", []))
        engine.phase = BattlePhase(data.get("phase", "running"))
        pending = data.get("pending")
        engine.pending = PendingChoice.from_dict(pending) if pending else None
        engine.log = list(data.get("log", []))
        state = data.get("rng_state")
        if state:
            # JSON 왕복으로 list 가 된 부분을 random 이 요구하는 tuple 로 되돌린다.
            version, internal, gauss = state
            engine.rng.setstate((version, tuple(internal), gauss))
        return engine
