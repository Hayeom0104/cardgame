"""전투 화면 렌더링 (설계 문서 §11 "전투 화면 — 필수").

§2.6 반영: 적 진영은 3슬롯 고정 단일 행이 아니라 **다중 행 그리드**다.
적 수에 상한이 없으므로 캔버스 높이가 적 수에 따라 늘어난다.
플레이어 진영은 파티 크기에 묶여 최대 3슬롯(§2.6/§4.1).
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from ..game.combat import CombatEngine
from ..game.entities import Unit
from . import base
from .base import (
    ACCENT,
    BG_DARK,
    BG_PANEL,
    BG_PANEL_ALT,
    BLOCK_BLUE,
    ENEMY_RED,
    FG,
    FG_MUTED,
    GOLD,
    HP_GREEN,
    HP_RED,
    KIND_COLORS,
)

WIDTH = 980
PAD = 20

ENEMY_W, ENEMY_H = 210, 132
ENEMY_PER_ROW = 4          # 넘치면 다음 행으로 (§2.6)
PLAYER_W, PLAYER_H = 290, 150
CARD_W, CARD_H = 168, 216


def _hp_ratio(unit: Unit) -> float:
    return unit.hp / unit.max_hp if unit.max_hp else 0.0


def _draw_enemy(img: Image.Image, draw: ImageDraw.ImageDraw, unit: Unit, x: int, y: int, label: str) -> None:
    box = (x, y, x + ENEMY_W, y + ENEMY_H)
    dead = not unit.alive
    base.rounded_panel(
        draw, box,
        fill=(30, 26, 30) if dead else BG_PANEL,
        outline=(70, 60, 62) if dead else ENEMY_RED,
        radius=10, width=2,
    )

    art_size = (52, 52)
    art = base.load_art(unit.sprite_path, art_size) or base.placeholder_art(
        art_size, unit.code, unit.name[:2]
    )
    if dead:
        art = art.convert("LA").convert("RGBA")
    img.paste(art, (x + 10, y + 12), art)

    name_font = base.font(15, bold=True)
    tag_font = base.font(12)
    text_x = x + 72

    name = base.truncate(draw, unit.name, name_font, ENEMY_W - 90)
    draw.text((text_x, y + 12), name, font=name_font, fill=FG_MUTED if dead else FG)
    draw.text((text_x, y + 32), label, font=tag_font, fill=FG_MUTED)

    if dead:
        draw.text((text_x, y + 56), "전투 불능", font=tag_font, fill=FG_MUTED)
        return

    base.bar(draw, (x + 12, y + 76, x + ENEMY_W - 12, y + 90), _hp_ratio(unit),
             HP_GREEN if _hp_ratio(unit) > 0.3 else HP_RED)
    draw.text((x + 12, y + 94), f"HP {unit.hp}/{unit.max_hp}", font=tag_font, fill=FG_MUTED)

    if unit.block:
        draw.text((x + ENEMY_W - 72, y + 94), f"블록 {unit.block}", font=tag_font, fill=BLOCK_BLUE)
    if unit.modifiers:
        mods = " ".join(f"{m.stat[:3]}{m.amount:+d}" for m in unit.modifiers[:3])
        draw.text((x + 12, y + 110), base.truncate(draw, mods, tag_font, ENEMY_W - 24),
                  font=tag_font, fill=ACCENT)


def _draw_player(img: Image.Image, draw: ImageDraw.ImageDraw, unit: Unit, x: int, y: int, active: bool) -> None:
    box = (x, y, x + PLAYER_W, y + PLAYER_H)
    dead = not unit.alive
    base.rounded_panel(
        draw, box,
        fill=(28, 30, 40) if dead else (BG_PANEL_ALT if active else BG_PANEL),
        outline=GOLD if active else (70, 74, 92),
        radius=12, width=3 if active else 2,
    )

    art_size = (60, 60)
    art = base.load_art(unit.sprite_path, art_size) or base.placeholder_art(
        art_size, unit.code, unit.name[:2]
    )
    if dead:
        art = art.convert("LA").convert("RGBA")
    img.paste(art, (x + 12, y + 14), art)

    name_font = base.font(16, bold=True)
    tag_font = base.font(12)
    tx = x + 84

    draw.text((tx, y + 14), base.truncate(draw, unit.name, name_font, PLAYER_W - 100),
              font=name_font, fill=FG_MUTED if dead else FG)
    draw.text((tx, y + 36), "★" * unit.star, font=tag_font, fill=GOLD)

    if dead:
        draw.text((tx, y + 58), "전투 불능", font=tag_font, fill=FG_MUTED)
        return

    base.bar(draw, (x + 12, y + 86, x + PLAYER_W - 12, y + 102), _hp_ratio(unit),
             HP_GREEN if _hp_ratio(unit) > 0.3 else HP_RED)
    draw.text((x + 12, y + 106), f"HP {unit.hp}/{unit.max_hp}", font=tag_font, fill=FG_MUTED)

    if unit.block:
        draw.text((x + PLAYER_W - 82, y + 106), f"블록 {unit.block}", font=tag_font, fill=BLOCK_BLUE)

    # §2.2 남은 드로우 더미 — 고갈되면 더 못 뽑으므로 항상 보여준다.
    draw.text((x + 12, y + 124), f"덱 {len(unit.draw_pile)} / 버림 {len(unit.discard_pile)}",
              font=tag_font, fill=FG_MUTED)
    if unit.modifiers:
        mods = " ".join(f"{m.stat[:3]}{m.amount:+d}" for m in unit.modifiers[:3])
        draw.text((x + PLAYER_W - 110, y + 124),
                  base.truncate(draw, mods, tag_font, 100), font=tag_font, fill=ACCENT)


def _draw_card(img: Image.Image, draw: ImageDraw.ImageDraw, card, x: int, y: int, index: int, affordable: bool) -> None:
    box = (x, y, x + CARD_W, y + CARD_H)
    accent = KIND_COLORS.get(card.kind, ACCENT)
    base.rounded_panel(
        draw, box,
        fill=BG_PANEL if affordable else (30, 30, 36),
        outline=accent if affordable else (66, 66, 74),
        radius=12, width=2,
    )

    art_box = (x + 8, y + 34, x + CARD_W - 8, y + 128)
    art_size = (art_box[2] - art_box[0], art_box[3] - art_box[1])
    art = base.load_art(card.art_path, art_size) or base.placeholder_art(
        art_size, card.code, card.kind
    )
    if not affordable:
        art = art.convert("LA").convert("RGBA")
    img.paste(art, (art_box[0], art_box[1]), art)

    idx_font = base.font(14, bold=True)
    name_font = base.font(14, bold=True)
    small = base.font(11)

    draw.ellipse((x + 8, y + 8, x + 30, y + 30), fill=accent)
    base.draw_center(draw, (x + 19, y + 19), str(index), idx_font, (20, 20, 26))

    # 비용 (§2.3)
    draw.ellipse((x + CARD_W - 32, y + 8, x + CARD_W - 10, y + 30), fill=(60, 66, 92))
    base.draw_center(draw, (x + CARD_W - 21, y + 19), str(card.cost), idx_font, FG)

    draw.text((x + 36, y + 12), base.truncate(draw, card.name, name_font, CARD_W - 76),
              font=name_font, fill=FG if affordable else FG_MUTED)

    for i, line in enumerate(base.wrap(draw, card.description or "", small, CARD_W - 20, 3)):
        draw.text((x + 10, y + 136 + i * 15), line, font=small, fill=FG_MUTED)

    draw.text((x + 10, y + CARD_H - 20), f"{card.kind} · {card.target}", font=small, fill=accent)


def render_battle(engine: CombatEngine, title: str = "") -> bytes:
    enemies = engine.enemies
    players = engine.players
    cards = engine.drawn_cards()

    enemy_rows = max(1, math.ceil(len(enemies) / ENEMY_PER_ROW))
    header_h = 64
    enemy_block_h = enemy_rows * (ENEMY_H + 12)
    divider_h = 44
    player_block_h = PLAYER_H + 16
    card_block_h = (CARD_H + 30) if cards else 0
    height = header_h + enemy_block_h + divider_h + player_block_h + card_block_h + PAD * 2

    img = Image.new("RGBA", (WIDTH, height), BG_DARK)
    draw = ImageDraw.Draw(img)

    # ---- 헤더 ----
    title_font = base.font(20, bold=True)
    info_font = base.font(14)
    draw.text((PAD, PAD), base.sanitize(title) or "전투", font=title_font, fill=FG)

    # §2.3 파티 공유 자원 + 라운드
    info = f"라운드 {engine.round_number}"
    draw.text((WIDTH - PAD - 260, PAD + 2), info, font=info_font, fill=FG_MUTED)
    pip_x = WIDTH - PAD - 150
    draw.text((pip_x - 46, PAD + 2), "자원", font=info_font, fill=FG_MUTED)
    for i in range(engine.resource_max):
        cx = pip_x + i * 26
        filled = i < engine.resource
        draw.ellipse((cx, PAD, cx + 20, PAD + 20),
                     fill=ACCENT if filled else (52, 56, 72),
                     outline=ACCENT, width=2)

    y = PAD + header_h

    # ---- 적 진영 (§2.6 그리드) ----
    for i, unit in enumerate(enemies):
        row, col = divmod(i, ENEMY_PER_ROW)
        per_row = min(ENEMY_PER_ROW, len(enemies) - row * ENEMY_PER_ROW)
        total_w = per_row * ENEMY_W + (per_row - 1) * 12
        start_x = (WIDTH - total_w) // 2
        x = start_x + col * (ENEMY_W + 12)
        _draw_enemy(img, draw, unit, x, y + row * (ENEMY_H + 12), f"적 {i + 1}번")

    y += enemy_block_h

    # ---- 구분선 ----
    draw.line((PAD, y + divider_h // 2, WIDTH - PAD, y + divider_h // 2), fill=(60, 64, 82), width=2)
    active = engine.active_unit
    if active is not None:
        label = f"{active.name} 의 턴"
        w, _ = base.text_size(draw, label, info_font)
        draw.rectangle((WIDTH // 2 - w // 2 - 12, y + divider_h // 2 - 12,
                        WIDTH // 2 + w // 2 + 12, y + divider_h // 2 + 12), fill=BG_DARK)
        base.draw_center(draw, (WIDTH // 2, y + divider_h // 2), label, info_font, GOLD)
    y += divider_h

    # ---- 플레이어 진영 (최대 3슬롯) ----
    total_w = len(players) * PLAYER_W + (len(players) - 1) * 14
    start_x = (WIDTH - total_w) // 2
    for i, unit in enumerate(players):
        _draw_player(img, draw, unit, start_x + i * (PLAYER_W + 14), y,
                     active=active is not None and active.uid == unit.uid)
    y += player_block_h

    # ---- 이번 턴 드로우 (§2.2) ----
    if cards:
        hint = base.font(13)
        draw.text((PAD, y), "이번 턴 드로우 — 1장만 사용할 수 있고 나머지는 버려집니다 (!카드 사용 <번호>)",
                  font=hint, fill=FG_MUTED)
        y += 24
        total_w = len(cards) * CARD_W + (len(cards) - 1) * 14
        start_x = (WIDTH - total_w) // 2
        for i, card in enumerate(cards):
            _draw_card(img, draw, card, start_x + i * (CARD_W + 14), y, i + 1,
                       affordable=card.cost <= engine.resource)

    return base.to_png_bytes(img)
