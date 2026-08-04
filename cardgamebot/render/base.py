"""Pillow 렌더링 공용 유틸 (설계 문서 §11).

폰트는 한글이 필수라, 시스템에 설치된 CJK 폰트를 탐색한다. 운영 배포 시에는
`cardgamebot/assets/fonts/` 에 원하는 폰트를 넣어두면 그걸 최우선으로 쓴다.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import get_settings

log = logging.getLogger(__name__)

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
FONT_DIR = ASSET_DIR / "fonts"

# 프로젝트 동봉 폰트를 먼저 찾고, 없으면 시스템 CJK 폰트로 폴백한다.
_FONT_CANDIDATES = [
    FONT_DIR / "NotoSansKR-Regular.ttf",
    FONT_DIR / "NanumGothic.ttf",
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
]

_BOLD_CANDIDATES = [
    FONT_DIR / "NotoSansKR-Bold.ttf",
    FONT_DIR / "NanumGothicBold.ttf",
    Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
]


# --- 팔레트 ---------------------------------------------------------------

BG_DARK = (22, 24, 33)
BG_PANEL = (33, 36, 48)
BG_PANEL_ALT = (43, 47, 62)
FG = (232, 234, 240)
FG_MUTED = (150, 155, 170)
ACCENT = (108, 160, 255)
HP_GREEN = (86, 196, 122)
HP_RED = (222, 84, 92)
BLOCK_BLUE = (96, 168, 232)
ENEMY_RED = (208, 92, 96)
GOLD = (232, 190, 96)

KIND_COLORS = {
    "attack": (214, 96, 92),
    "defense": (92, 148, 214),
    "buff": (140, 200, 120),
    "debuff": (170, 120, 200),
    "heal": (110, 200, 170),
}

NODE_COLORS = {
    "combat": (214, 96, 92),
    "reward": (232, 190, 96),
    "rest": (110, 200, 170),
    "shop": (108, 160, 255),
    "event": (170, 120, 200),
    "boss": (240, 110, 90),
}


@lru_cache(maxsize=64)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (_BOLD_CANDIDATES if bold else []) + _FONT_CANDIDATES
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    log.warning("한글 지원 폰트를 찾지 못해 기본 비트맵 폰트로 대체합니다.")
    return ImageFont.load_default()


# 번들 폰트에 글리프가 없어 두부(□)로 깨지는 문자 구간.
# ★☆ 처럼 실제로 렌더링되는 기호는 예외로 남긴다.
_STRIP_RANGES = (
    (0x1F000, 0x1FAFF),   # 이모지 (보조 평면)
    (0xFE00, 0xFE0F),     # variation selector
    (0x2190, 0x21FF),     # 화살표
    (0x2600, 0x27BF),     # 기타 기호 / 딩벳
    (0x2B00, 0x2BFF),
)
_KEEP = {"\u2605", "\u2606"}   # ★ ☆


def sanitize(text: str) -> str:
    """이미지에 그릴 문자열에서 렌더링 불가 문자를 제거한다.

    디스코드 본문 텍스트에서는 이모지가 정상 표시되므로, 제거는 이미지
    렌더링 경로에서만 한다. 렌더러는 이모지 대신 한글 라벨을 쓴다.
    """
    out = []
    for ch in text:
        if ch in _KEEP:
            out.append(ch)
            continue
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in _STRIP_RANGES):
            continue
        out.append(ch)
    return "".join(out).strip()


def text_size(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=f)
    return (box[2] - box[0], box[3] - box[1])


def draw_center(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, f, fill=FG
) -> None:
    text = sanitize(text)
    w, h = text_size(draw, text, f)
    draw.text((xy[0] - w // 2, xy[1] - h // 2), text, font=f, fill=fill)


def rounded_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill=BG_PANEL,
    outline=None,
    radius: int = 10,
    width: int = 2,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def bar(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    ratio: float,
    fill,
    back=(58, 62, 78),
    radius: int = 5,
) -> None:
    """HP/블록 게이지."""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=radius, fill=back)
    ratio = max(0.0, min(1.0, ratio))
    if ratio > 0:
        filled = x0 + int((x1 - x0) * ratio)
        # 반경보다 짧아지면 rounded_rectangle 이 예외를 내므로 하한을 준다.
        filled = max(filled, x0 + radius * 2)
        draw.rounded_rectangle((x0, y0, filled, y1), radius=radius, fill=fill)


def truncate(draw: ImageDraw.ImageDraw, text: str, f, max_width: int) -> str:
    text = sanitize(text)
    if text_size(draw, text, f)[0] <= max_width:
        return text
    ellipsis = "…"
    out = text
    while out and text_size(draw, out + ellipsis, f)[0] > max_width:
        out = out[:-1]
    return out + ellipsis


def wrap(draw: ImageDraw.ImageDraw, text: str, f, max_width: int, max_lines: int = 3) -> list[str]:
    """한글은 단어 경계가 드물어 글자 단위로 접는다."""
    text = sanitize(text)
    lines: list[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        trial = current + ch
        if text_size(draw, trial, f)[0] > max_width and current:
            lines.append(current)
            current = ch
        else:
            current = trial
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines


def load_art(path: str | None, size: tuple[int, int]) -> Image.Image | None:
    """업로드된 일러스트를 불러온다 (§10.2 대시보드 업로드 결과)."""
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = get_settings().upload_dir / path
    if not candidate.exists():
        return None
    try:
        img = Image.open(candidate).convert("RGBA")
        return img.resize(size, Image.LANCZOS)
    except OSError:
        log.warning("이미지를 열 수 없습니다: %s", candidate)
        return None


def placeholder_art(size: tuple[int, int], seed: str, label: str) -> Image.Image:
    """일러스트가 아직 없을 때 쓰는 자리표시 이미지.

    §11 은 카드마다 고유 아트를 요구하지만, 그건 아트 프로덕션 의존성이다.
    에셋이 없어도 렌더링이 깨지지 않도록 코드 기준 색상 블록으로 대체한다.
    """
    h = (hash(seed) % 360) / 360.0
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(h, 0.35, 0.45)
    img = Image.new("RGBA", size, (int(r * 255), int(g * 255), int(b * 255), 255))
    draw = ImageDraw.Draw(img)
    f = font(max(11, size[1] // 6))
    draw_center(draw, (size[0] // 2, size[1] // 2), truncate(draw, label, f, size[0] - 8), f, FG)
    return img


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
