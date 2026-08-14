"""카드/가챠 결과 렌더링 (설계 문서 §11 "카드 아트 — 카드마다 고유").

일러스트 자체는 §10.2 대시보드 업로드로 공급되는 **아트 프로덕션 산출물**이다.
아직 업로드되지 않은 카드는 자리표시 이미지로 대체해 화면이 깨지지 않게 한다.

전투 화면(`battle.py`)의 카드와 같은 프레임 언어를 쓴다 — 등급 테두리, 종류
배지, 등급 보석.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from . import base
from .base import BG_PANEL, BG_PANEL_HI, FG, FG_DIM, FG_MUTED

CARD_W, CARD_H = 180, 244
GAP = 18
PAD = 26
HEADER_H = 50
PER_ROW = 5

KIND_LABEL = {
    "attack": "공격", "defense": "방어", "buff": "버프",
    "debuff": "디버프", "heal": "회복",
    "character": "캐릭터", "passive": "패시브", "card": "카드",
}

BADGE_COLORS = {
    "PICKUP": ((255, 236, 180), (188, 132, 24)),
    "NEW": ((235, 255, 236), (54, 140, 86)),
}


def _card_face(img: Image.Image, x: int, y: int, item: dict) -> None:
    """item: {name, code, kind, cost, rarity, description, art_path, badge}"""
    kind = item.get("kind", "")
    rarity = int(item.get("rarity") or 1)
    hi, lo = base.kind_colors(kind)
    rar_hi, rar_lo = base.rarity_colors(rarity)

    box = (x, y, x + CARD_W, y + CARD_H)
    # 고등급일수록 더 강하게 발광시켜 결과창에서 한눈에 들어오게 한다.
    if rarity >= 5:
        base.outer_glow(img, box, rar_hi, radius=15, blur=14,
                        opacity=90 + (rarity - 5) * 45, spread=3)

    base.panel(img, box,
               top=base.mix(BG_PANEL_HI, lo, 0.26),
               bottom=base.mix(BG_PANEL, lo, 0.10),
               border=rar_hi, border_width=2, radius=15)

    draw = ImageDraw.Draw(img)

    # 아트
    art_box = (x + 10, y + 36, x + CARD_W - 10, y + 158)
    base.art_frame(img, art_box, item.get("art_path"), item.get("code", ""), "",
                   radius=10, fade_bottom=True, tint=lo)
    draw.rounded_rectangle(art_box, radius=10, outline=(*rar_lo, 210), width=1)

    # 이름
    name_font = base.font(15, bold=True)
    cost = item.get("cost")
    name_w = CARD_W - 24 - (26 if cost is not None else 0)
    draw.text((x + 12, y + 11), base.truncate(draw, item.get("name", ""), name_font, name_w),
              font=name_font, fill=FG)

    # 비용
    if cost is not None:
        cx = x + CARD_W - 24
        draw.ellipse((cx - 12, y + 8, cx + 12, y + 32), fill=(46, 62, 104),
                     outline=base.ACCENT, width=2)
        base.draw_center(draw, (cx, y + 20), str(cost), base.font(13, bold=True), FG)

    # 종류 배지 (아트 위)
    small = base.font(11)
    base.pill(img, (x + 15, y + 132), KIND_LABEL.get(kind, kind), small, fg=FG, bg=(*lo, 235))

    # 설명
    for i, line in enumerate(base.wrap(draw, item.get("description", ""), small, CARD_W - 26, 3)):
        draw.text((x + 13, y + 166 + i * 16), line, font=small, fill=FG_DIM)

    # 등급 보석
    for i in range(min(rarity, 6)):
        gx = x + 13 + i * 12
        gy = y + CARD_H - 19
        draw.ellipse((gx, gy, gx + 8, gy + 8), fill=rar_hi)

    badge = item.get("badge")
    if badge:
        fg, bg = BADGE_COLORS.get(badge, (FG, (60, 64, 88)))
        bw = base.text_width(draw, badge, small) + 16
        base.pill(img, (x + CARD_W - bw - 12, y + CARD_H - 24), badge, small, fg=fg, bg=bg)


def render_card_sheet(items: list[dict], title: str = "카드", subtitle: str = "") -> bytes:
    """카드 목록을 한 장의 이미지로 렌더링한다 (가챠 결과, 덱 보기 등)."""
    items = items or []
    per_row = min(PER_ROW, max(1, len(items))) if items else 1
    rows = max(1, math.ceil(len(items) / PER_ROW)) if items else 1

    width = PAD * 2 + per_row * CARD_W + (per_row - 1) * GAP
    height = PAD * 2 + HEADER_H + rows * (CARD_H + GAP) - GAP

    img = base.scene_background((width, height), tint=(58, 44, 86))
    draw = ImageDraw.Draw(img)

    draw.text((PAD, PAD - 4), base.sanitize(title), font=base.font(21, bold=True), fill=FG)
    if subtitle:
        draw.text((PAD, PAD + 22), base.sanitize(subtitle), font=base.font(12), fill=FG_MUTED)

    if not items:
        draw.text((PAD, PAD + HEADER_H), "표시할 카드가 없습니다.",
                  font=base.font(14), fill=FG_MUTED)
        return base.to_png_bytes(img)

    for i, item in enumerate(items):
        row, col = divmod(i, PER_ROW)
        in_row = min(PER_ROW, len(items) - row * PER_ROW)
        total_w = in_row * CARD_W + (in_row - 1) * GAP
        start_x = (width - total_w) // 2
        _card_face(img, start_x + col * (CARD_W + GAP),
                   PAD + HEADER_H + row * (CARD_H + GAP), item)

    return base.to_png_bytes(img)
