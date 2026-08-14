"""전투 화면 렌더링 (설계 문서 §11 "전투 화면 — 필수").

레이아웃

    ┌ 헤더 ─────────────── 라운드 · 자원 오브 ┐
    │ 적 진영 (다중 행 그리드, §2.6 상한 없음) │
    ├ 턴 표시 구분선 ─────────────────────────┤
    │ 아군 진영 (최대 3슬롯, §2.6/§4.1)        │
    │ 이번 턴 드로우 카드 (§2.2)               │
    └ 전투 로그 ──────────────────────────────┘

§2.6 반영: 적 진영은 3슬롯 고정 단일 행이 아니라 다중 행 그리드다. 적 수에
상한이 없으므로 캔버스 높이가 적 수에 따라 늘어난다.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from ..game.combat import CombatEngine
from ..game.entities import Unit
from . import base
from .base import (
    ACCENT,
    BG_PANEL,
    BG_PANEL_HI,
    BLOCK_HI,
    BLOCK_LO,
    ENEMY_HI,
    ENEMY_LO,
    FG,
    FG_DIM,
    FG_MUTED,
    GOLD,
    GOLD_DIM,
    LINE,
)

WIDTH = 1000
PAD = 22

ENEMY_W, ENEMY_H = 216, 104
ENEMY_GAP = 14
ENEMY_PER_ROW = 4          # 넘치면 다음 행으로 (§2.6)

PLAYER_W, PLAYER_H = 296, 158
PLAYER_GAP = 16

CARD_W, CARD_H = 172, 232
CARD_GAP = 16

HEADER_H = 62
DIVIDER_H = 40
LOG_LINES = 4


def _hp_ratio(unit: Unit) -> float:
    return unit.hp / unit.max_hp if unit.max_hp else 0.0


# ---------------------------------------------------------------------------
# 적 카드
# ---------------------------------------------------------------------------


def _draw_enemy(img: Image.Image, unit: Unit, x: int, y: int, index: int) -> None:
    box = (x, y, x + ENEMY_W, y + ENEMY_H)
    dead = not unit.alive
    ratio = _hp_ratio(unit)

    if dead:
        base.panel(img, box, top=(34, 30, 36), bottom=(24, 22, 28),
                   border=(58, 52, 58), radius=12)
    else:
        base.panel(img, box, top=BG_PANEL_HI, bottom=BG_PANEL,
                   border=base.shade(ENEMY_LO, 1.15), radius=12)

    draw = ImageDraw.Draw(img)

    # 초상화
    art_box = (x + 10, y + 10, x + 10 + 60, y + 10 + 60)
    base.art_frame(img, art_box, unit.sprite_path, unit.code, unit.name[:2],
                   radius=9, grayscale=dead, tint=ENEMY_LO, label_ratio=0.42)
    draw.rounded_rectangle(art_box, radius=9,
                           outline=(70, 62, 70) if dead else base.shade(ENEMY_LO, 1.3), width=2)

    # 대상 번호 배지 — `!덱아웃 사용 <카드> <대상>` 에서 쓰는 번호
    bx, by = x + 6, y + 6
    draw.ellipse((bx, by, bx + 22, by + 22),
                 fill=(58, 52, 58) if dead else ENEMY_LO,
                 outline=(80, 74, 80) if dead else ENEMY_HI, width=2)
    base.draw_center(draw, (bx + 11, by + 11), str(index), base.font(12, bold=True),
                     FG_MUTED if dead else FG)

    tx = x + 82
    name_font = base.font(15, bold=True)
    tag_font = base.font(11)

    draw.text((tx, y + 12), base.truncate(draw, unit.name, name_font, ENEMY_W - 96),
              font=name_font, fill=FG_MUTED if dead else FG)

    if dead:
        draw.text((tx, y + 36), "전투 불능", font=base.font(12), fill=FG_MUTED)
        return

    # HP 게이지
    base.gauge(img, (tx, y + 36, x + ENEMY_W - 12, y + 50), ratio, *base.hp_colors(ratio))
    draw.text((tx, y + 54), f"{unit.hp} / {unit.max_hp}", font=tag_font, fill=FG_DIM)

    # 상태 배지 (블록 / 버프·디버프)
    px = tx
    py = y + 72
    if unit.block:
        px += base.pill(img, (px, py), f"블록 {unit.block}", tag_font,
                        fg=BLOCK_HI, bg=(28, 44, 66), border=BLOCK_LO) + 5
    for m in unit.modifiers[:2]:
        if px > x + ENEMY_W - 60:
            break
        up = m.amount > 0
        px += base.pill(img, (px, py), f"{m.stat[:3]} {m.amount:+d}", tag_font,
                        fg=(150, 220, 130) if up else (224, 150, 200),
                        bg=(30, 44, 32) if up else (48, 30, 42)) + 5


# ---------------------------------------------------------------------------
# 아군 카드
# ---------------------------------------------------------------------------


def _draw_player(img: Image.Image, unit: Unit, x: int, y: int, active: bool) -> None:
    box = (x, y, x + PLAYER_W, y + PLAYER_H)
    dead = not unit.alive
    ratio = _hp_ratio(unit)

    if active:
        base.outer_glow(img, box, GOLD, radius=15, blur=12, opacity=110, spread=4)

    if dead:
        base.panel(img, box, top=(32, 30, 38), bottom=(22, 21, 28),
                   border=(56, 54, 64), radius=15)
    else:
        base.panel(img, box,
                   top=(58, 54, 80) if active else BG_PANEL_HI,
                   bottom=(36, 34, 54) if active else BG_PANEL,
                   border=GOLD if active else LINE,
                   border_width=2, radius=15)

    draw = ImageDraw.Draw(img)

    art_box = (x + 12, y + 12, x + 12 + 68, y + 12 + 68)
    base.art_frame(img, art_box, unit.sprite_path, unit.code, unit.name[:2],
                   radius=10, grayscale=dead, tint=(86, 92, 150), label_ratio=0.40)
    draw.rounded_rectangle(art_box, radius=10,
                           outline=GOLD_DIM if active else (74, 78, 100), width=2)

    tx = x + 92
    draw.text((tx, y + 14), base.truncate(draw, unit.name, base.font(17, bold=True), PLAYER_W - 106),
              font=base.font(17, bold=True), fill=FG_MUTED if dead else FG)
    base.stars(draw, (tx, y + 38), unit.star, max(unit.star, 3), base.font(12))

    if dead:
        draw.text((tx, y + 60), "전투 불능", font=base.font(12), fill=FG_MUTED)
        return

    tag_font = base.font(11)

    # 스탯 요약 — 공격/방어/속도
    stat_font = base.font(11)
    sx = x + 92
    for label, value, color in (
        ("공", unit.stat("attack"), (240, 160, 150)),
        ("방", unit.stat("defense"), (150, 190, 240)),
        ("속", unit.stat("speed"), (200, 200, 220)),
    ):
        draw.text((sx, y + 58), label, font=stat_font, fill=FG_MUTED)
        draw.text((sx + 16, y + 58), str(value), font=base.font(11, bold=True), fill=color)
        sx += 48

    # HP
    base.gauge(img, (x + 12, y + 90, x + PLAYER_W - 12, y + 106), ratio, *base.hp_colors(ratio))
    draw.text((x + 14, y + 110), f"HP {unit.hp} / {unit.max_hp}", font=tag_font, fill=FG_DIM)

    if unit.block:
        base.pill(img, (x + PLAYER_W - 82, y + 108), f"블록 {unit.block}", tag_font,
                  fg=BLOCK_HI, bg=(28, 44, 66), border=BLOCK_LO)

    # §2.2 남은 드로우 더미 — 고갈되면 더 못 뽑으므로 항상 보여준다.
    px = x + 12
    py = y + 130
    low = len(unit.draw_pile) <= 3
    px += base.pill(img, (px, py), f"덱 {len(unit.draw_pile)}", tag_font,
                    fg=(246, 180, 120) if low else FG_DIM,
                    bg=(52, 38, 28) if low else (34, 37, 52),
                    border=(180, 120, 70) if low else None) + 5
    px += base.pill(img, (px, py), f"버림 {len(unit.discard_pile)}", tag_font,
                    fg=FG_MUTED, bg=(30, 33, 46)) + 5
    for m in unit.modifiers[:2]:
        if px > x + PLAYER_W - 66:
            break
        up = m.amount > 0
        px += base.pill(img, (px, py), f"{m.stat[:3]} {m.amount:+d}", tag_font,
                        fg=(150, 220, 130) if up else (224, 150, 200),
                        bg=(30, 44, 32) if up else (48, 30, 42)) + 5


# ---------------------------------------------------------------------------
# 손패 카드
# ---------------------------------------------------------------------------

KIND_LABEL = {
    "attack": "공격", "defense": "방어", "buff": "버프",
    "debuff": "디버프", "heal": "회복",
}
TARGET_LABEL = {
    "enemy_single": "적 1", "enemy_all": "적 전체", "ally_single": "아군 1",
    "ally_all": "아군 전체", "self": "자신",
}


def _draw_card(img: Image.Image, card, x: int, y: int, index: int, affordable: bool) -> None:
    box = (x, y, x + CARD_W, y + CARD_H)
    hi, lo = base.kind_colors(card.kind)
    rar_hi, rar_lo = base.rarity_colors(card.rarity)

    if affordable:
        base.outer_glow(img, box, lo, radius=14, blur=10, opacity=80, spread=2)
        base.panel(img, box, top=base.mix(BG_PANEL_HI, lo, 0.28),
                   bottom=base.mix(BG_PANEL, lo, 0.12),
                   border=rar_hi, border_width=2, radius=14)
    else:
        base.panel(img, box, top=(34, 35, 42), bottom=(24, 25, 32),
                   border=(60, 62, 72), border_width=2, radius=14, shadow=False)

    draw = ImageDraw.Draw(img)

    # 아트
    art_box = (x + 9, y + 38, x + CARD_W - 9, y + 150)
    base.art_frame(img, art_box, card.art_path, card.code, "",
                   radius=9, grayscale=not affordable, fade_bottom=True,
                   tint=lo)
    draw.rounded_rectangle(art_box, radius=9, outline=(*rar_lo, 200), width=1)

    # 상단 바 — 선택 번호 / 이름 / 비용
    draw.ellipse((x + 8, y + 8, x + 30, y + 30),
                 fill=lo if affordable else (54, 56, 66),
                 outline=hi if affordable else (74, 76, 86), width=2)
    base.draw_center(draw, (x + 19, y + 19), str(index), base.font(13, bold=True), FG)

    name_font = base.font(14, bold=True)
    draw.text((x + 36, y + 12),
              base.truncate(draw, card.name, name_font, CARD_W - 78),
              font=name_font, fill=FG if affordable else FG_MUTED)

    # 비용 (§2.3) — 자원 오브와 같은 모양으로 통일
    cx = x + CARD_W - 26
    draw.ellipse((cx - 12, y + 7, cx + 12, y + 31),
                 fill=(46, 62, 104) if affordable else (48, 48, 56),
                 outline=ACCENT if affordable else (76, 78, 88), width=2)
    base.draw_center(draw, (cx, y + 19), str(card.cost), base.font(14, bold=True),
                     FG if affordable else FG_MUTED)

    # 종류 / 대상 배지 (아트 위에 얹음)
    small = base.font(11)
    base.pill(img, (x + 14, y + 124), KIND_LABEL.get(card.kind, card.kind), small,
              fg=FG, bg=(*lo, 235))
    tgt = TARGET_LABEL.get(card.target, card.target)
    tw = base.text_width(draw, tgt, small) + 16
    base.pill(img, (x + CARD_W - 14 - tw, y + 124), tgt, small, fg=FG_DIM, bg=(24, 26, 36, 235))

    # 설명
    for i, line in enumerate(base.wrap(draw, card.description or "", small, CARD_W - 24, 3)):
        draw.text((x + 12, y + 158 + i * 16), line, font=small,
                  fill=FG_DIM if affordable else FG_MUTED)

    # 등급 보석
    for i in range(min(card.rarity, 6)):
        gx = x + 12 + i * 11
        gy = y + CARD_H - 17
        draw.ellipse((gx, gy, gx + 7, gy + 7), fill=rar_hi if affordable else (70, 72, 82))

    if not affordable:
        base.pill(img, (x + CARD_W - 66, y + CARD_H - 22), "자원 부족", small,
                  fg=(240, 160, 160), bg=(58, 32, 36))


# ---------------------------------------------------------------------------
# 전체 화면
# ---------------------------------------------------------------------------


def _draw_header(img: Image.Image, engine: CombatEngine, title: str) -> None:
    draw = ImageDraw.Draw(img)
    draw.text((PAD, PAD - 2), base.sanitize(title) or "전투",
              font=base.font(22, bold=True), fill=FG)

    # 라운드 배지
    base.pill(img, (PAD, PAD + 28), f"라운드 {engine.round_number}", base.font(12),
              fg=GOLD, bg=(46, 38, 24), border=GOLD_DIM)

    # §2.3 파티 공유 자원 — 오브로 표시
    label_font = base.font(13)
    orb_r = 11
    total_w = engine.resource_max * (orb_r * 2 + 7)
    ox = WIDTH - PAD - total_w
    base.draw_right(draw, (ox - 10, PAD + 4), "자원", label_font, FG_DIM)

    for i in range(engine.resource_max):
        cx = ox + i * (orb_r * 2 + 7) + orb_r
        cy = PAD + 12
        box = (cx - orb_r, cy - orb_r, cx + orb_r, cy + orb_r)
        if i < engine.resource:
            base.outer_glow(img, box, ACCENT, radius=orb_r, blur=7, opacity=120, spread=1)
            orb = base.linear_gradient((orb_r * 2, orb_r * 2), (168, 204, 255), (58, 108, 210))
            orb.putalpha(base.rounded_mask((orb_r * 2, orb_r * 2), orb_r))
            img.alpha_composite(orb, (cx - orb_r, cy - orb_r))
            draw.ellipse(box, outline=(196, 220, 255), width=1)
        else:
            draw.ellipse(box, fill=(30, 33, 48), outline=(64, 72, 98), width=2)

    base.draw_right(draw, (WIDTH - PAD, PAD + 30),
                    f"{engine.resource} / {engine.resource_max}", base.font(12), FG_MUTED)


def _draw_divider(img: Image.Image, y: int, engine: CombatEngine) -> None:
    draw = ImageDraw.Draw(img)
    cy = y + DIVIDER_H // 2
    draw.line((PAD, cy, WIDTH - PAD, cy), fill=(58, 62, 86), width=1)

    active = engine.active_unit
    if active is None:
        return

    label = f"{active.name} 의 턴"
    f = base.font(13, bold=True)
    w = base.text_width(draw, label, f) + 30
    box = (WIDTH // 2 - w // 2, cy - 14, WIDTH // 2 + w // 2, cy + 14)
    base.outer_glow(img, box, GOLD, radius=14, blur=9, opacity=70, spread=1)
    base.panel(img, box, top=(70, 58, 32), bottom=(48, 38, 20),
               border=GOLD, radius=14, shadow=False)
    base.draw_center(draw, (WIDTH // 2, cy), label, f, GOLD)


def _draw_log(img: Image.Image, engine: CombatEngine, y: int) -> None:
    draw = ImageDraw.Draw(img)
    box = (PAD, y, WIDTH - PAD, y + LOG_LINES * 18 + 16)
    base.panel(img, box, top=(26, 28, 40), bottom=(20, 22, 32),
               border=(46, 50, 70), radius=10, shadow=False, highlight=False)

    f = base.font(12)
    lines = [ln for ln in engine.log if ln.strip()][-LOG_LINES:]
    for i, line in enumerate(lines):
        # 최신 줄일수록 밝게 — 시선이 아래로 흐르게 한다.
        t = (i + 1) / max(1, len(lines))
        color = base.mix(FG_MUTED, FG_DIM, t)
        draw.text((PAD + 12, y + 8 + i * 18),
                  base.truncate(draw, line, f, WIDTH - PAD * 2 - 24), font=f, fill=color)


def render_battle(engine: CombatEngine, title: str = "") -> bytes:
    enemies = engine.enemies
    players = engine.players
    cards = engine.drawn_cards()

    enemy_rows = max(1, math.ceil(len(enemies) / ENEMY_PER_ROW)) if enemies else 0
    enemy_block = enemy_rows * (ENEMY_H + ENEMY_GAP)
    card_block = (CARD_H + 34) if cards else 0
    log_block = LOG_LINES * 18 + 16 + 6

    height = (
        PAD + HEADER_H + enemy_block + DIVIDER_H
        + PLAYER_H + 18 + card_block + log_block + PAD
    )

    img = base.scene_background((WIDTH, height), tint=(58, 40, 78))

    _draw_header(img, engine, title)
    y = PAD + HEADER_H

    # ---- 적 진영 (§2.6 다중 행 그리드) ----
    for i, unit in enumerate(enemies):
        row, col = divmod(i, ENEMY_PER_ROW)
        in_row = min(ENEMY_PER_ROW, len(enemies) - row * ENEMY_PER_ROW)
        total_w = in_row * ENEMY_W + (in_row - 1) * ENEMY_GAP
        start_x = (WIDTH - total_w) // 2
        _draw_enemy(img, unit, start_x + col * (ENEMY_W + ENEMY_GAP),
                    y + row * (ENEMY_H + ENEMY_GAP), i + 1)
    y += enemy_block

    _draw_divider(img, y, engine)
    y += DIVIDER_H

    # ---- 아군 진영 (최대 3슬롯) ----
    total_w = len(players) * PLAYER_W + (len(players) - 1) * PLAYER_GAP
    start_x = (WIDTH - total_w) // 2
    active = engine.active_unit
    for i, unit in enumerate(players):
        _draw_player(img, unit, start_x + i * (PLAYER_W + PLAYER_GAP), y,
                     active=active is not None and active.uid == unit.uid)
    y += PLAYER_H + 18

    # ---- 이번 턴 드로우 (§2.2) ----
    if cards:
        draw = ImageDraw.Draw(img)
        draw.text((PAD, y), "이번 턴 드로우", font=base.font(13, bold=True), fill=FG_DIM)
        draw.text((PAD + 96, y + 1),
                  "1장만 사용할 수 있고 나머지는 버려집니다  ·  !덱아웃 사용 <번호> [대상]",
                  font=base.font(12), fill=FG_MUTED)
        y += 24
        total_w = len(cards) * CARD_W + (len(cards) - 1) * CARD_GAP
        start_x = (WIDTH - total_w) // 2
        for i, card in enumerate(cards):
            _draw_card(img, card, start_x + i * (CARD_W + CARD_GAP), y, i + 1,
                       affordable=card.cost <= engine.resource)
        y += CARD_H + 10

    _draw_log(img, engine, y + 4)

    return base.to_png_bytes(img)
