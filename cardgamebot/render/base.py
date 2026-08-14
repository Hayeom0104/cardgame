"""Pillow 렌더링 공용 유틸 (설계 문서 §11).

디스코드에 올라가는 이미지가 게임의 유일한 화면이라, 이 모듈은 단순한
그리기 헬퍼가 아니라 **UI 키트**로 쓴다. 그라데이션 / 글로우 / 그림자 /
게이지 같은 표현 도구를 여기 모아두고, 각 화면 렌더러는 배치에만 집중한다.

폰트는 한글이 필수라 시스템 CJK 폰트를 탐색한다. 운영 배포 시에는
`cardgamebot/assets/fonts/` 에 원하는 폰트를 넣어두면 그걸 최우선으로 쓴다.
"""

from __future__ import annotations

import colorsys
import io
import logging
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..config import get_settings

log = logging.getLogger(__name__)

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
FONT_DIR = ASSET_DIR / "fonts"

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]

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
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
]


# ---------------------------------------------------------------------------
# 팔레트 — 다크 판타지
# ---------------------------------------------------------------------------

BG_DEEP: RGB = (13, 14, 22)          # 화면 가장 바깥
BG_DARK: RGB = (20, 22, 33)
BG_PANEL: RGB = (32, 35, 50)
BG_PANEL_HI: RGB = (44, 48, 68)      # 패널 상단 (그라데이션용)
BG_PANEL_ALT: RGB = (52, 44, 68)

FG: RGB = (240, 242, 248)
FG_DIM: RGB = (176, 182, 200)
FG_MUTED: RGB = (120, 127, 148)

LINE: RGB = (62, 68, 92)
GOLD: RGB = (240, 196, 104)
GOLD_DIM: RGB = (150, 120, 60)
ACCENT: RGB = (122, 168, 255)
ACCENT_DIM: RGB = (60, 84, 140)

HP_HI: RGB = (126, 224, 150)
HP_LO: RGB = (64, 176, 112)
HP_HURT_HI: RGB = (240, 180, 90)
HP_HURT_LO: RGB = (208, 128, 48)
HP_CRIT_HI: RGB = (246, 108, 116)
HP_CRIT_LO: RGB = (196, 56, 72)

BLOCK_HI: RGB = (140, 206, 255)
BLOCK_LO: RGB = (72, 138, 210)

ENEMY_HI: RGB = (232, 112, 116)
ENEMY_LO: RGB = (150, 52, 62)

KIND_COLORS: dict[str, tuple[RGB, RGB]] = {
    "attack": ((238, 118, 110), (150, 52, 56)),
    "defense": ((116, 172, 240), (48, 88, 156)),
    "buff": ((150, 220, 130), (62, 132, 74)),
    "debuff": ((190, 138, 224), (100, 60, 148)),
    "heal": ((124, 224, 194), (44, 138, 122)),
}

NODE_COLORS: dict[str, tuple[RGB, RGB]] = {
    "combat": ((238, 118, 110), (140, 48, 54)),
    "reward": ((244, 200, 110), (156, 116, 40)),
    "rest": ((124, 224, 194), (40, 132, 116)),
    "shop": ((122, 168, 255), (48, 86, 160)),
    "event": ((190, 138, 224), (98, 58, 146)),
    "boss": ((250, 130, 96), (156, 52, 36)),
}

# 등급(1~6) 색. 카드 프레임/보석에 쓴다.
RARITY_COLORS: dict[int, tuple[RGB, RGB]] = {
    1: ((150, 158, 178), (86, 92, 110)),
    2: ((124, 206, 156), (54, 122, 92)),
    3: ((122, 168, 255), (48, 86, 160)),
    4: ((190, 138, 232), (104, 58, 152)),
    5: ((248, 178, 96), (162, 100, 32)),
    6: ((250, 122, 132), (170, 46, 66)),
}


def rarity_colors(rarity: int) -> tuple[RGB, RGB]:
    return RARITY_COLORS.get(max(1, min(6, int(rarity or 1))), RARITY_COLORS[1])


def kind_colors(kind: str) -> tuple[RGB, RGB]:
    return KIND_COLORS.get(kind, (ACCENT, ACCENT_DIM))


def mix(a: RGB, b: RGB, t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def shade(color: RGB, factor: float) -> RGB:
    """factor<1 이면 어둡게, >1 이면 밝게."""
    return tuple(max(0, min(255, int(c * factor))) for c in color)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 폰트 / 텍스트
# ---------------------------------------------------------------------------


@lru_cache(maxsize=96)
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
# 번들 폰트 실측 결과: → ▶ ★ ☆ ● ◆ 는 정상 렌더링되고,
# ✓ ⚔ ❓ ⬆ ☠ ♻ ⏭ 및 보조 평면 이모지는 두부로 깨진다.
_STRIP_RANGES = (
    (0x1F000, 0x1FAFF),   # 이모지 (보조 평면)
    (0xFE00, 0xFE0F),     # variation selector
    (0x2300, 0x23FF),     # 기술 기호 (⏭ 등)
    (0x2600, 0x27BF),     # 기타 기호 / 딩벳 (✓ ⚔ ❓ ☠ ♻ 등)
    (0x2B00, 0x2BFF),     # ⬆ 등
)
_KEEP = {"★", "☆"}


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


def text_size(draw: ImageDraw.ImageDraw, text: str, f) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=f)
    return (box[2] - box[0], box[3] - box[1])


def text_width(draw: ImageDraw.ImageDraw, text: str, f) -> int:
    return text_size(draw, sanitize(text), f)[0]


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    f,
    fill=FG,
    shadow: bool = False,
) -> None:
    """좌측 정렬 텍스트. shadow=True 면 어두운 배경 위 가독성을 위해 그림자를 깐다."""
    text = sanitize(text)
    if shadow:
        draw.text((xy[0] + 1, xy[1] + 1), text, font=f, fill=(0, 0, 0, 150))
    draw.text(xy, text, font=f, fill=fill)


def draw_center(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, f, fill=FG,
                shadow: bool = False) -> None:
    text = sanitize(text)
    w, h = text_size(draw, text, f)
    pos = (xy[0] - w // 2, xy[1] - h // 2)
    if shadow:
        draw.text((pos[0] + 1, pos[1] + 1), text, font=f, fill=(0, 0, 0, 150))
    draw.text(pos, text, font=f, fill=fill)


def draw_right(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, f, fill=FG) -> None:
    text = sanitize(text)
    w, _ = text_size(draw, text, f)
    draw.text((xy[0] - w, xy[1]), text, font=f, fill=fill)


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


# ---------------------------------------------------------------------------
# 그라데이션 / 배경
# ---------------------------------------------------------------------------


def linear_gradient(size: tuple[int, int], top: RGB, bottom: RGB) -> Image.Image:
    """세로 선형 그라데이션. 1px 폭으로 만들고 늘려서 비용을 줄인다."""
    w, h = size
    strip = Image.new("RGB", (1, max(1, h)))
    px = strip.load()
    for y in range(max(1, h)):
        px[0, y] = mix(top, bottom, y / max(1, h - 1))
    return strip.resize(size, Image.BILINEAR).convert("RGBA")


def radial_glow(size: tuple[int, int], color: RGB, strength: float = 0.55) -> Image.Image:
    """중앙이 밝은 방사형 광원. 배경에 깊이를 준다."""
    w, h = size
    small = Image.new("L", (64, 64), 0)
    d = ImageDraw.Draw(small)
    d.ellipse((8, 8, 56, 56), fill=int(255 * strength))
    small = small.filter(ImageFilter.GaussianBlur(12))
    mask = small.resize(size, Image.BILINEAR)
    layer = Image.new("RGBA", size, (*color, 0))
    layer.putalpha(mask)
    return layer


def vignette(size: tuple[int, int], strength: float = 0.5) -> Image.Image:
    """가장자리를 어둡게 눌러 시선을 가운데로 모은다."""
    w, h = size
    small = Image.new("L", (64, 64), 255)
    d = ImageDraw.Draw(small)
    d.ellipse((-14, -14, 78, 78), fill=0)
    small = small.filter(ImageFilter.GaussianBlur(16))
    mask = small.resize(size, Image.BILINEAR).point(lambda v: int(v * strength))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.putalpha(mask)
    return layer


def scene_background(size: tuple[int, int], tint: RGB = (46, 40, 78)) -> Image.Image:
    """화면 공통 배경 — 그라데이션 + 상단 광원 + 비네트."""
    w, h = size
    img = linear_gradient(size, mix(BG_DARK, tint, 0.35), BG_DEEP)
    glow = radial_glow((w, int(h * 0.7)), tint, 0.42)
    img.alpha_composite(glow, (0, -int(h * 0.18)))
    img.alpha_composite(vignette(size, 0.55))
    return img


# ---------------------------------------------------------------------------
# 패널 / 그림자 / 글로우
# ---------------------------------------------------------------------------


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def drop_shadow(
    target: Image.Image,
    box: tuple[int, int, int, int],
    radius: int = 14,
    blur: int = 10,
    offset: tuple[int, int] = (0, 5),
    opacity: int = 130,
) -> None:
    """패널 아래에 부드러운 그림자를 깔아 레이어를 분리한다."""
    x0, y0, x1, y1 = box
    pad = blur * 3
    size = (x1 - x0 + pad * 2, y1 - y0 + pad * 2)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (pad, pad, size[0] - pad - 1, size[1] - pad - 1), radius=radius, fill=(0, 0, 0, opacity)
    )
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    target.alpha_composite(layer, (x0 - pad + offset[0], y0 - pad + offset[1]))


def outer_glow(
    target: Image.Image,
    box: tuple[int, int, int, int],
    color: RGB,
    radius: int = 14,
    blur: int = 9,
    opacity: int = 165,
    spread: int = 3,
) -> None:
    """강조 요소 주변 발광. 현재 턴/선택 가능 노드에 쓴다."""
    x0, y0, x1, y1 = box
    pad = blur * 3
    size = (x1 - x0 + pad * 2, y1 - y0 + pad * 2)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (pad - spread, pad - spread, size[0] - pad - 1 + spread, size[1] - pad - 1 + spread),
        radius=radius + spread, fill=(*color, opacity),
    )
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    target.alpha_composite(layer, (x0 - pad, y0 - pad))


def panel(
    target: Image.Image,
    box: tuple[int, int, int, int],
    top: RGB = BG_PANEL_HI,
    bottom: RGB = BG_PANEL,
    radius: int = 14,
    border: RGB | None = LINE,
    border_width: int = 2,
    shadow: bool = True,
    highlight: bool = True,
) -> None:
    """그라데이션 + 상단 하이라이트 + 그림자를 갖춘 기본 패널."""
    x0, y0, x1, y1 = box
    w, h = max(1, x1 - x0), max(1, y1 - y0)

    if shadow:
        drop_shadow(target, box, radius=radius)

    fill = linear_gradient((w, h), top, bottom)
    fill.putalpha(rounded_mask((w, h), radius))
    target.alpha_composite(fill, (x0, y0))

    draw = ImageDraw.Draw(target)
    if highlight:
        # 상단 1px 밝은 선 — 유리/금속 느낌의 입체감
        draw.rounded_rectangle(
            (x0 + 1, y0 + 1, x1 - 1, y1 - 1), radius=radius - 1,
            outline=(*shade(top, 1.35), 90), width=1,
        )
    if border:
        draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, outline=border, width=border_width)


def gauge(
    target: Image.Image,
    box: tuple[int, int, int, int],
    ratio: float,
    hi: RGB,
    lo: RGB,
    back: RGB = (30, 33, 46),
    ghost: float | None = None,
    ghost_color: RGB = (232, 96, 104),
) -> None:
    """그라데이션 게이지.

    `ghost` 를 주면 이전 값 잔상을 옅게 남겨 변화량이 보이게 한다.
    """
    x0, y0, x1, y1 = box
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    radius = h // 2

    draw = ImageDraw.Draw(target)
    draw.rounded_rectangle(box, radius=radius, fill=back)

    if ghost is not None and ghost > ratio:
        gw = int(w * max(0.0, min(1.0, ghost)))
        if gw > radius:
            draw.rounded_rectangle((x0, y0, x0 + gw, y1), radius=radius, fill=(*ghost_color, 110))

    ratio = max(0.0, min(1.0, ratio))
    fw = int(w * ratio)
    if fw > 2:
        fw = max(fw, radius * 2)
        bar = linear_gradient((fw, h), hi, lo)
        bar.putalpha(rounded_mask((fw, h), radius))
        target.alpha_composite(bar, (x0, y0))
        # 상단 광택
        gloss = Image.new("RGBA", (fw, max(1, h // 2)), (255, 255, 255, 34))
        gloss.putalpha(
            Image.eval(rounded_mask((fw, max(1, h // 2)), radius), lambda v: int(v * 0.30))
        )
        target.alpha_composite(gloss, (x0, y0 + 1))

    draw.rounded_rectangle(box, radius=radius, outline=(*shade(lo, 0.7), 200), width=1)


def hp_colors(ratio: float) -> tuple[RGB, RGB]:
    if ratio > 0.5:
        return (HP_HI, HP_LO)
    if ratio > 0.25:
        return (HP_HURT_HI, HP_HURT_LO)
    return (HP_CRIT_HI, HP_CRIT_LO)


def pill(
    target: Image.Image,
    xy: tuple[int, int],
    text: str,
    f,
    fg: RGB = FG,
    bg: RGB = BG_PANEL,
    border: RGB | None = None,
    pad_x: int = 8,
    pad_y: int = 3,
) -> int:
    """작은 라벨 배지. 반환값은 배지 폭."""
    draw = ImageDraw.Draw(target)
    text = sanitize(text)
    tw, th = text_size(draw, text, f)
    w, h = tw + pad_x * 2, th + pad_y * 2 + 2
    box = (xy[0], xy[1], xy[0] + w, xy[1] + h)
    draw.rounded_rectangle(box, radius=h // 2, fill=bg, outline=border, width=1 if border else 0)
    draw.text((xy[0] + pad_x, xy[1] + pad_y), text, font=f, fill=fg)
    return w


def stars(draw: ImageDraw.ImageDraw, xy: tuple[int, int], count: int, cap: int, f,
          color: RGB = GOLD, dim: RGB = (86, 74, 52)) -> None:
    """★ 채움 / ☆ 빈칸으로 성급을 표시한다."""
    x, y = xy
    for i in range(cap):
        ch = "★" if i < count else "☆"
        draw.text((x, y), ch, font=f, fill=color if i < count else dim)
        x += text_size(draw, ch, f)[0] + 1


# ---------------------------------------------------------------------------
# 아트 로딩
# ---------------------------------------------------------------------------


def load_art(path: str | None, size: tuple[int, int]) -> Image.Image | None:
    """업로드된 일러스트를 불러온다 (§10.2 대시보드 업로드 결과).

    비율을 유지한 채 꽉 채우도록 잘라낸다(cover).
    """
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = get_settings().upload_dir / path
    if not candidate.exists():
        return None
    try:
        img = Image.open(candidate).convert("RGBA")
    except OSError:
        log.warning("이미지를 열 수 없습니다: %s", candidate)
        return None

    tw, th = size
    scale = max(tw / img.width, th / img.height)
    resized = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    left = (resized.width - tw) // 2
    top = (resized.height - th) // 2
    return resized.crop((left, top, left + tw, top + th))


def placeholder_art(
    size: tuple[int, int],
    seed: str,
    label: str,
    tint: RGB | None = None,
    label_ratio: float = 0.34,
) -> Image.Image:
    """일러스트가 아직 없을 때 쓰는 자리표시 이미지.

    §11 은 카드마다 고유 아트를 요구하지만 그건 아트 프로덕션 의존성이다.
    에셋이 없어도 화면이 초라해지지 않도록 그라데이션과 사선 패턴을 깔고
    머리글자를 옅게 얹는다.

    `tint` 를 주면 그 색 계열로 칠한다. 카드는 효과 종류 색, 유닛은 진영 색을
    넘겨서 화면 전체 색이 따로 놀지 않게 한다. 없으면 코드 해시로 색을 만든다.
    """
    w, h = size
    if tint is None:
        hue = (abs(hash(seed)) % 360) / 360.0
        r1, g1, b1 = colorsys.hsv_to_rgb(hue, 0.42, 0.52)
        r2, g2, b2 = colorsys.hsv_to_rgb((hue + 0.08) % 1.0, 0.55, 0.24)
        top = (int(r1 * 255), int(g1 * 255), int(b1 * 255))
        bottom = (int(r2 * 255), int(g2 * 255), int(b2 * 255))
    else:
        # 같은 계열 안에서 코드마다 살짝 다른 명도를 줘 개체 구분은 유지한다.
        jitter = (abs(hash(seed)) % 24) / 100.0
        top = mix(shade(tint, 0.85 + jitter), (255, 255, 255), 0.10)
        bottom = shade(tint, 0.34)

    img = linear_gradient(size, top, bottom)
    draw = ImageDraw.Draw(img)

    # 사선 스트라이프로 빈 공간이 밋밋해 보이지 않게 한다.
    for i in range(-h, w, 16):
        draw.line((i, h, i + h, 0), fill=(*shade(top, 1.20), 22), width=6)

    if label:
        f = font(max(12, min(int(size[1] * label_ratio), 30)), bold=True)
        draw_center(draw, (w // 2, h // 2), truncate(draw, label, f, w - 10), f,
                    (255, 255, 255, 118))
    return img


def art_frame(
    target: Image.Image,
    box: tuple[int, int, int, int],
    path: str | None,
    seed: str,
    label: str,
    radius: int = 10,
    grayscale: bool = False,
    fade_bottom: bool = False,
    tint: RGB | None = None,
    label_ratio: float = 0.34,
) -> None:
    """일러스트를 둥근 프레임에 넣어 합성한다."""
    x0, y0, x1, y1 = box
    size = (max(1, x1 - x0), max(1, y1 - y0))
    art = load_art(path, size) or placeholder_art(size, seed, label, tint, label_ratio)
    if grayscale:
        art = art.convert("LA").convert("RGBA")

    mask = rounded_mask(size, radius)
    art.putalpha(mask)

    if fade_bottom:
        # 아래쪽을 어둡게 깔아 그 위에 얹는 텍스트가 읽히게 한다.
        fade = Image.new("RGBA", size, (0, 0, 0, 0))
        fd = ImageDraw.Draw(fade)
        for i in range(size[1] // 2):
            alpha = int(190 * (i / max(1, size[1] // 2)) ** 1.6)
            fd.line((0, size[1] - i - 1, size[0], size[1] - i - 1), fill=(0, 0, 0, alpha))
        fade.putalpha(Image.composite(fade.getchannel("A"), Image.new("L", size, 0), mask))
        art.alpha_composite(fade)

    target.alpha_composite(art, (x0, y0))


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
