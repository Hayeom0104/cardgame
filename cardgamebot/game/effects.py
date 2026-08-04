"""데이터 주도 효과 해석기.

카드/적 행동/패시브는 모두 아래 형태의 효과 리스트를 갖는다::

    [{"op": "damage", "amount": 8}, {"op": "block", "amount": 4}]

지원하는 op 는 설계 문서 §2.5 에서 **확정된 카테고리**(공격/방어/버프·디버프
/회복)에 한정한다.

주의 — 설계 문서 §2.7 및 §13 에서 "상태 효과 분류(화상/기절 등)"는 명시적으로
미확정(TBD)이다. 따라서 임의의 상태 효과를 만들어 넣지 않는다. 확정되면
`register_effect()` 로 op 를 추가하면 되고, 엔진의 다른 부분은 손대지 않아도
된다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .entities import Modifier, Unit

# 효과 핸들러: (source, targets, spec, ctx) -> 로그 문자열 목록
EffectHandler = Callable[[Unit, Sequence[Unit], dict, "EffectContext"], list[str]]

_REGISTRY: dict[str, EffectHandler] = {}


class EffectContext:
    """효과가 전투 전역 상태에 접근해야 할 때 쓰는 핸들.

    카드 드로우나 자원 회복처럼 유닛 밖의 상태를 건드리는 효과가 있어서
    엔진 참조를 넘긴다.
    """

    def __init__(self, engine: Any = None) -> None:
        self.engine = engine


def register_effect(op: str) -> Callable[[EffectHandler], EffectHandler]:
    def deco(fn: EffectHandler) -> EffectHandler:
        _REGISTRY[op] = fn
        return fn

    return deco


def resolve_effects(
    source: Unit,
    targets: Sequence[Unit],
    effects: Sequence[dict],
    ctx: EffectContext | None = None,
) -> list[str]:
    """효과 리스트를 순서대로 적용하고 로그를 반환한다."""
    ctx = ctx or EffectContext()
    log: list[str] = []
    for spec in effects or []:
        op = spec.get("op")
        handler = _REGISTRY.get(op)
        if handler is None:
            # 미지원 op 는 조용히 무시하지 않고 로그로 드러낸다.
            # (대시보드에서 잘못 입력한 효과를 운영자가 바로 알아채야 한다.)
            log.append(f"⚠️ 알 수 없는 효과 `{op}` — 무시됨")
            continue
        log.extend(handler(source, targets, spec, ctx))
    return log


# ---------------------------------------------------------------------------
# 공격 (§2.5)
# ---------------------------------------------------------------------------


@register_effect("damage")
def _damage(source: Unit, targets: Sequence[Unit], spec: dict, ctx: EffectContext) -> list[str]:
    """기본 데미지.

    최종 데미지 = (기본치 + 시전자 공격력 보정) - 대상 방어력, 최소 1.
    `scale_with_attack=False` 로 두면 고정 데미지가 된다.
    """
    base = int(spec.get("amount", 0))
    hits = int(spec.get("hits", 1))
    scale = spec.get("scale_with_attack", True)

    log: list[str] = []
    for target in targets:
        if not target.alive:
            continue
        total_hp_loss = 0
        for _ in range(hits):
            raw = base + (source.stat("attack") if scale else 0)
            mitigated = max(1, raw - target.stat("defense"))
            blocked, dealt = target.take_damage(mitigated)
            total_hp_loss += dealt
            if blocked and dealt:
                log.append(f"{source.name} → {target.name}: {dealt} 피해 (블록 {blocked} 흡수)")
            elif blocked:
                log.append(f"{source.name} → {target.name}: 블록이 {blocked} 전부 막아냄")
            else:
                log.append(f"{source.name} → {target.name}: {dealt} 피해")
        if total_hp_loss and not target.alive:
            log.append(f"💀 {target.name} 전투 불능")
    return log


@register_effect("pierce")
def _pierce(source: Unit, targets: Sequence[Unit], spec: dict, ctx: EffectContext) -> list[str]:
    """블록과 방어력을 모두 무시하는 고정 피해."""
    amount = int(spec.get("amount", 0))
    log: list[str] = []
    for target in targets:
        if not target.alive:
            continue
        dealt = min(target.hp, amount)
        target.hp -= dealt
        log.append(f"{source.name} → {target.name}: {dealt} 관통 피해")
        if not target.alive:
            log.append(f"💀 {target.name} 전투 불능")
    return log


# ---------------------------------------------------------------------------
# 방어 (§2.5) — 턴 시작 시 리셋되는 블록
# ---------------------------------------------------------------------------


@register_effect("block")
def _block(source: Unit, targets: Sequence[Unit], spec: dict, ctx: EffectContext) -> list[str]:
    amount = int(spec.get("amount", 0))
    log: list[str] = []
    for target in targets:
        if not target.alive:
            continue
        gained = target.gain_block(amount)
        log.append(f"🛡️ {target.name} 블록 +{gained} (현재 {target.block})")
    return log


# ---------------------------------------------------------------------------
# 회복 (§2.5)
# ---------------------------------------------------------------------------


@register_effect("heal")
def _heal(source: Unit, targets: Sequence[Unit], spec: dict, ctx: EffectContext) -> list[str]:
    amount = int(spec.get("amount", 0))
    percent = float(spec.get("percent", 0.0))
    log: list[str] = []
    for target in targets:
        if not target.alive:
            continue
        total = amount + int(target.max_hp * percent)
        healed = target.heal(total)
        log.append(f"💚 {target.name} HP +{healed} ({target.hp}/{target.max_hp})")
    return log


# ---------------------------------------------------------------------------
# 버프 / 디버프 (§2.5)
# ---------------------------------------------------------------------------


def _apply_modifier(targets: Sequence[Unit], spec: dict, sign: int, icon: str) -> list[str]:
    stat = spec.get("stat", "attack")
    amount = abs(int(spec.get("amount", 0))) * sign
    duration = int(spec.get("duration", 2))
    log: list[str] = []
    for target in targets:
        if not target.alive:
            continue
        target.modifiers.append(
            Modifier(stat=stat, amount=amount, duration=duration, source=spec.get("source", ""))
        )
        log.append(f"{icon} {target.name} {stat} {amount:+d} ({duration}턴)")
    return log


@register_effect("buff")
def _buff(source: Unit, targets: Sequence[Unit], spec: dict, ctx: EffectContext) -> list[str]:
    return _apply_modifier(targets, spec, sign=1, icon="⬆️")


@register_effect("debuff")
def _debuff(source: Unit, targets: Sequence[Unit], spec: dict, ctx: EffectContext) -> list[str]:
    return _apply_modifier(targets, spec, sign=-1, icon="⬇️")


# ---------------------------------------------------------------------------
# 전투 전역 상태를 건드리는 효과
# ---------------------------------------------------------------------------


@register_effect("resource")
def _resource(source: Unit, targets: Sequence[Unit], spec: dict, ctx: EffectContext) -> list[str]:
    """§2.3 파티 공유 자원 풀을 회복한다."""
    if ctx.engine is None:
        return []
    amount = int(spec.get("amount", 0))
    gained = ctx.engine.gain_resource(amount)
    return [f"⚡ 자원 +{gained} (현재 {ctx.engine.resource}/{ctx.engine.resource_max})"]


@register_effect("draw")
def _draw(source: Unit, targets: Sequence[Unit], spec: dict, ctx: EffectContext) -> list[str]:
    """§2.2 추가 드로우. 드로우 더미가 비면 아무 일도 일어나지 않는다."""
    if ctx.engine is None:
        return []
    amount = int(spec.get("amount", 0))
    drawn = ctx.engine.draw_extra(source, amount)
    if not drawn:
        return ["🃏 드로우 더미가 비어 카드를 뽑지 못함"]
    return [f"🃏 카드 {len(drawn)}장 추가 드로우"]


def supported_ops() -> list[str]:
    """관리자 대시보드에서 선택지로 노출할 op 목록."""
    return sorted(_REGISTRY)
