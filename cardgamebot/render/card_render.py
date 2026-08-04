"""카드/가챠 결과 렌더링 (설계 문서 §11 "카드 아트 — 카드마다 고유").

일러스트 자체는 §10.2 대시보드 업로드로 공급되는 **아트 프로덕션 산출물**이다.
아직 업로드되지 않은 카드는 자리표시 이미지로 대체해 렌더링이 깨지지 않게 한다.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from . import base
from .base import ACCENT, BG_DARK, BG_PANEL, FG, FG_MUTED, GOLD, KIND_COLORS

CARD_W, CARD_H = 176, 234
GAP = 16
PAD = 22
PER_ROW = 5


def _card_face(draw: ImageDraw.ImageDraw, img: Image.Image, x: int, y: int, item: dict) -> None:
    """item: {name, code, kind, cost, rarity, description, art_path, badge}"""
    accent = KIND_COLORS.get(item.get("kind", ""), ACCENT)
    base.rounded_panel(draw, (x, y, x + CARD_W, y + CARD_H), fill=BG_PANEL,
                       outline=accent, radius=12, width=2)

    art_box = (x + 8, y + 32, x + CARD_W - 8, y + 140)
    size = (art_box[2] - art_box[0], art_box[3] - art_box[1])
    art = base.load_art(item.get("art_path"), size) or base.placeholder_art(
        size, item.get("code", ""), item.get("kind", "?")
    )
    img.paste(art, (art_box[0], art_box[1]), art)

    name_font = base.font(14, bold=True)
    small = base.font(11)

    draw.text((x + 10, y + 10), base.truncate(draw, item["name"], name_font, CARD_W - 56),
              font=name_font, fill=FG)

    cost = item.get("cost")
    if cost is not None:
        draw.ellipse((x + CARD_W - 34, y + 6, x + CARD_W - 10, y + 30), fill=(60, 66, 92))
        base.draw_center(draw, (x + CARD_W - 22, y + 18), str(cost), base.font(13, bold=True), FG)

    rarity = item.get("rarity")
    if rarity:
        draw.text((x + 10, y + 146), "★" * min(int(rarity), 6), font=small, fill=GOLD)

    for i, line in enumerate(base.wrap(draw, item.get("description", ""), small, CARD_W - 20, 3)):
        draw.text((x + 10, y + 164 + i * 15), line, font=small, fill=FG_MUTED)

    badge = item.get("badge")
    if badge:
        bw = base.text_size(draw, badge, small)[0] + 12
        draw.rounded_rectangle((x + CARD_W - bw - 8, y + CARD_H - 24, x + CARD_W - 8, y + CARD_H - 6),
                               radius=8, fill=accent)
        base.draw_center(draw, (x + CARD_W - bw // 2 - 8, y + CARD_H - 15), badge, small, (20, 20, 26))


def render_card_sheet(items: list[dict], title: str = "카드") -> bytes:
    """카드 목록을 한 장의 이미지로 렌더링한다 (가챠 결과, 덱 보기 등)."""
    if not items:
        items = []
    rows = max(1, math.ceil(len(items) / PER_ROW)) if items else 1
    width = PAD * 2 + PER_ROW * CARD_W + (PER_ROW - 1) * GAP
    height = PAD * 2 + 44 + rows * (CARD_H + GAP)

    img = Image.new("RGBA", (width, height), BG_DARK)
    draw = ImageDraw.Draw(img)
    draw.text((PAD, PAD), title, font=base.font(20, bold=True), fill=FG)

    if not items:
        draw.text((PAD, PAD + 44), "표시할 카드가 없습니다.", font=base.font(14), fill=FG_MUTED)
        return base.to_png_bytes(img)

    for i, item in enumerate(items):
        row, col = divmod(i, PER_ROW)
        x = PAD + col * (CARD_W + GAP)
        y = PAD + 44 + row * (CARD_H + GAP)
        _card_face(draw, img, x, y, item)

    return base.to_png_bytes(img)
