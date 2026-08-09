"""§11 — Pillow rendering.

Two-panel layout: ally and enemy panels as **two PNGs in one action**
(§1.3.7). Each ≤ 4 MiB, ≤ 4096×4096.

Asset fallback: missing/invalid/deleted art renders a **rarity-tier silhouette
plus the entity name as text**. The game never fails to render; the dashboard
flags such entities.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.central.client import (MAX_PNG_BYTES, MAX_PNG_DIMENSION,
                                validate_png_attachment)

logger = logging.getLogger(__name__)

PANEL_WIDTH = 720
PANEL_HEIGHT = 360
MAP_WIDTH = 900
MAP_HEIGHT = 560

BACKGROUND = (24, 26, 34)
PANEL_BG = (34, 37, 48)
TEXT = (232, 234, 240)
MUTED = (150, 156, 172)
HP_FULL = (96, 200, 120)
HP_LOW = (216, 92, 92)
BLOCK = (110, 170, 230)
ACCENT = (226, 186, 96)

#: Rarity-tier silhouette fills, used when art is missing or invalid.
TIER_SILHOUETTE = {
    1: (86, 92, 108), 2: (92, 116, 132), 3: (104, 132, 96),
    4: (128, 116, 84), 5: (134, 96, 112), 6: (150, 120, 60),
}


def _font(size: int) -> ImageFont.ImageFont:
    """Prefer a real font; fall back to Pillow's bitmap default.

    Korean glyphs need a CJK-capable font. Where none is installed the default
    font still renders (as boxes for CJK), which keeps the §11 promise that the
    game never fails to render.
    """
    for candidate in (
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


@dataclass
class Attachment:
    filename: str
    data_b64: str
    width: int
    height: int
    decoded_size: int

    def validate(self) -> None:
        validate_png_attachment(
            data_b64=self.data_b64, filename=self.filename,
            width=self.width, height=self.height, decoded_size=self.decoded_size,
        )


def to_attachment(image: Image.Image, filename: str) -> Attachment:
    """Encode a PNG within the §1.3.7 limits.

    `data_b64` is standard base64 with no `data:` URI prefix.
    """
    if image.width > MAX_PNG_DIMENSION or image.height > MAX_PNG_DIMENSION:
        image = image.copy()
        image.thumbnail((MAX_PNG_DIMENSION, MAX_PNG_DIMENSION))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    raw = buffer.getvalue()
    if len(raw) > MAX_PNG_BYTES:
        raise ValueError(f"rendered PNG is {len(raw)} bytes, over the 4 MiB limit")

    attachment = Attachment(
        filename=filename,
        data_b64=base64.b64encode(raw).decode("ascii"),
        width=image.width, height=image.height, decoded_size=len(raw),
    )
    attachment.validate()
    return attachment


def _silhouette(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                name: str, tier: int) -> None:
    """The §11 fallback: a rarity-tier silhouette plus the entity name."""
    draw.rounded_rectangle(box, radius=8, fill=TIER_SILHOUETTE.get(tier, MUTED))
    draw.text((box[0] + 8, box[1] + 8), name[:12], font=_font(14), fill=TEXT)


def _hp_bar(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int,
            current: int, maximum: int, block: int = 0) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=4, fill=(56, 58, 70))
    ratio = max(0.0, min(1.0, current / maximum if maximum else 0.0))
    colour = HP_FULL if ratio > 0.35 else HP_LOW
    if ratio > 0:
        draw.rounded_rectangle((x, y, x + int(width * ratio), y + height),
                               radius=4, fill=colour)
    draw.text((x + width + 6, y - 2), f"{current}/{maximum}", font=_font(12),
              fill=TEXT)
    if block:
        # Block is shown separately from HP: the player must be able to read
        # what a hit will actually cost.
        draw.text((x + width + 66, y - 2), f"🛡{block}", font=_font(12), fill=BLOCK)


def render_ally_panel(units: list[dict], *, resource: int, round_no: int) -> Image.Image:
    """Ally panel: placement, HP bars, block, status icons (§11)."""
    image = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((16, 12), f"라운드 {round_no}", font=_font(18), fill=ACCENT)
    draw.text((PANEL_WIDTH - 150, 12), f"자원 {resource}", font=_font(18), fill=ACCENT)

    for index, unit in enumerate(units[:3]):
        top = 56 + index * 96
        draw.rounded_rectangle((16, top, PANEL_WIDTH - 16, top + 80), radius=10,
                               fill=PANEL_BG)
        _silhouette(draw, (28, top + 10, 88, top + 70), unit.get("name", "?"),
                    unit.get("tier", 1))
        draw.text((104, top + 12), unit.get("name", "?"), font=_font(16), fill=TEXT)
        _hp_bar(draw, 104, top + 40, 320, 14, unit.get("hp_current", 0),
                unit.get("hp_max", 1), unit.get("block", 0))
        statuses = " ".join(
            f"{entry['status_id']}×{entry['stacks']}"
            for entry in unit.get("statuses", [])
        )
        if statuses:
            draw.text((104, top + 60), statuses[:60], font=_font(11), fill=MUTED)
        if not unit.get("is_alive", True):
            draw.text((PANEL_WIDTH - 90, top + 30), "전투불능", font=_font(14),
                      fill=HP_LOW)
    return image


def render_enemy_panel(units: list[dict], telegraphs: dict[int, dict]) -> Image.Image:
    """Enemy panel including 적 행동 예고 for **all** enemies (§11).

    An enemy that already consumed its snapshot entry shows `행동 완료` — not a
    blank space and not a speculatively generated next-round plan (§2.8.6).
    """
    image = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((16, 12), "적", font=_font(18), fill=ACCENT)

    columns = 4
    for index, unit in enumerate(units[:8]):
        column, row = index % columns, index // columns
        left = 16 + column * 176
        top = 48 + row * 150
        draw.rounded_rectangle((left, top, left + 160, top + 134), radius=10,
                               fill=PANEL_BG)
        _silhouette(draw, (left + 10, top + 8, left + 70, top + 68),
                    unit.get("name", "?"), unit.get("tier", 1))
        draw.text((left + 78, top + 10), unit.get("name", "?")[:8], font=_font(13),
                  fill=TEXT)
        _hp_bar(draw, left + 10, top + 76, 90, 10, unit.get("hp_current", 0),
                unit.get("hp_max", 1), unit.get("block", 0))

        telegraph = telegraphs.get(unit.get("battle_unit_id"), {})
        label = telegraph.get("label", "")
        draw.text((left + 10, top + 100), f"▶ {label}"[:22], font=_font(12),
                  fill=ACCENT if telegraph.get("state") == "planned" else MUTED)
        if not unit.get("is_alive", True):
            draw.text((left + 100, top + 40), "격파", font=_font(14), fill=HP_LOW)
    return image


def render_battle_screen(ally_units: list[dict], enemy_units: list[dict],
                         telegraphs: dict[int, dict], *, resource: int,
                         round_no: int) -> list[Attachment]:
    """The two-panel pattern: two PNGs in one action (§1.3.7)."""
    return [
        to_attachment(render_ally_panel(ally_units, resource=resource,
                                        round_no=round_no), "deckout_ally.png"),
        to_attachment(render_enemy_panel(enemy_units, telegraphs),
                      "deckout_enemy.png"),
    ]


def render_map(nodes: list[dict], edges: list[tuple[int, int]], *,
               current_node_index: int | None,
               available: set[int] | None = None) -> Attachment:
    """맵 화면 — the whole map as one image (§11)."""
    image = Image.new("RGB", (MAP_WIDTH, MAP_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    available = available or set()

    by_depth: dict[int, list[dict]] = {}
    for node in nodes:
        by_depth.setdefault(node["depth"], []).append(node)
    max_depth = max(by_depth) if by_depth else 1

    positions: dict[int, tuple[int, int]] = {}
    for depth, row in sorted(by_depth.items()):
        y = 48 + int((depth - 1) * (MAP_HEIGHT - 110) / max(1, max_depth - 1))
        for index, node in enumerate(sorted(row, key=lambda n: n["node_index"])):
            x = int(MAP_WIDTH * (index + 1) / (len(row) + 1))
            positions[node["node_index"]] = (x, y)

    for source, target in edges:
        if source in positions and target in positions:
            draw.line((*positions[source], *positions[target]), fill=(64, 68, 84),
                      width=2)

    for node in nodes:
        x, y = positions[node["node_index"]]
        is_current = node["node_index"] == current_node_index
        is_open = node["node_index"] in available
        fill = ACCENT if is_current else (PANEL_BG if not is_open else (60, 82, 108))
        draw.ellipse((x - 22, y - 22, x + 22, y + 22), fill=fill,
                     outline=TEXT if is_open else MUTED, width=2)
        draw.text((x - 16, y - 8), node["node_type"][:2], font=_font(14), fill=TEXT)
    return to_attachment(image, "deckout_map.png")


def render_settlement(report: dict) -> Attachment:
    """런 인벤토리 / 정산 화면 — the player must see what was kept and what was
    lost (§8.6.3, §11)."""
    image = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((16, 12), "정산", font=_font(20), fill=ACCENT)

    inventory = report.get("inventory", {})
    rows = [
        ("보관", inventory.get("kept", []), HP_FULL),
        ("등급 하락", inventory.get("tiered_down", []), ACCENT),
        ("소실", inventory.get("lost", []), HP_LOW),
        ("파괴", inventory.get("destroyed_by_tier_down", []), MUTED),
    ]
    y = 56
    for label, entries, colour in rows:
        draw.text((16, y), f"{label} {len(entries)}", font=_font(16), fill=colour)
        for entry in entries[:4]:
            y += 20
            name = entry.get("equipment_def_id") or f"강화석 T{entry.get('stone_tier')}"
            draw.text((36, y), f"· {name} (T{entry.get('tier', 0)})",
                      font=_font(12), fill=MUTED)
        y += 28

    rewards = report.get("rewards") or {}
    draw.text((PANEL_WIDTH - 240, 56),
              f"코인 {rewards.get('coin', 0)} · 카르타 {rewards.get('carta', 0)}",
              font=_font(14), fill=TEXT)
    return to_attachment(image, "deckout_settlement.png")
