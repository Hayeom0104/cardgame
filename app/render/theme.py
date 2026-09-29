"""화면의 크기·색·글꼴 — `config/10_화면.toml` 에서 읽는다.

겉모습만 정하므로 콘텐츠 버전에 고정하지 않고 파일에서 곧장 읽는다. 색을
바꾸면 새 버전을 발행하지 않아도 다음 화면부터 반영된다. 게임 수치는 절대
이렇게 다루지 않는다 — 그쪽은 `Balance` 를 거쳐 버전에 고정된다.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import ImageDraw, ImageFont

from app.content import config_loader

logger = logging.getLogger(__name__)

#: 저장소 루트. 설정에 적힌 상대 경로는 여기를 기준으로 푼다.
ROOT = Path(__file__).resolve().parents[2]

Color = tuple[int, int, int]


class Theme:
    """겉모습 설정 한 벌. 이름으로 값을 꺼내 쓴다."""

    def __init__(self, values: dict[str, Any]):
        self._values = values

    # -- 원시 값 -------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        if default is not None:
            return default
        raise KeyError(
            f"화면 설정 {key!r} 이(가) config/10_화면.toml 에 없습니다")

    def int_(self, key: str) -> int:
        return int(self.get(key))

    def size(self, key: str) -> tuple[int, int]:
        width, height = self.get(key)
        return int(width), int(height)

    def color(self, key: str) -> Color:
        red, green, blue = self.get(key)
        return int(red), int(green), int(blue)

    # -- 표에서 고르는 색 ----------------------------------------------
    def _from_table(self, table: str, key: Any, fallback: str) -> Color:
        entry = self.get(table).get(str(key))
        if entry is None:
            return self.color(fallback)
        red, green, blue = entry
        return int(red), int(green), int(blue)

    def rarity_color(self, tier: Any) -> Color:
        """희귀도별 색. 그림이 없을 때 대신 칠하는 색이기도 하다."""
        return self._from_table("color_by_rarity", tier, "color_panel")

    def element_color(self, element: Any) -> Color:
        return self._from_table("color_by_element", element, "color_muted")

    def node_color(self, node_type: Any) -> Color:
        return self._from_table("color_by_node_type", node_type, "color_muted")

    # -- 글꼴 ----------------------------------------------------------
    def font(self, size: int | None = None, *, role: str = "body"):
        """설정에 적힌 순서대로 글꼴을 찾는다.

        한글이 네모로 보인다면 한글을 지원하는 .ttf 를 `assets/fonts/` 에
        넣고 `font_candidates` 맨 위에 적으면 된다. 하나도 없어도 Pillow
        기본 글꼴로 그려지므로 화면이 실패하는 일은 없다 (§11).
        """
        if size is None:
            size = self.int_(f"font_size_{role}")
        return _load_font(tuple(self.get("font_candidates")), size)


class PaletteTheme(Theme):
    """기존 크기/표 설정은 공유하면서 색만 별도 팔레트로 덮는 뷰.

    지도와 뽑기 화면은 기존 어두운 팔레트를 유지하고, 나머지 화면만 밝은
    오로라 팔레트를 쓰기 위해 Theme 전체를 복제하지 않고 이 얇은 래퍼를
    사용한다. ``aurora_color_text``가 있으면 ``color_text`` 대신 고른다.
    """

    def __init__(self, base: Theme, palette: str):
        self._base = base
        self.palette = palette
        super().__init__(base._values)

    def get(self, key: str, default: Any = None) -> Any:
        override = f"{self.palette}_{key}"
        if override in self._values:
            return self._values[override]
        return self._base.get(key, default)


#: 글꼴에 한글이 실제로 들어 있는지 확인할 때 그려 보는 글자.
_HANGUL_PROBE = "한글"

#: 어떤 글꼴에도 없는 글자(사용자 영역). 이걸 그린 결과와 위 글자를 그린
#: 결과가 같으면, 그 글꼴은 한글을 네모(.notdef)로 그리고 있다는 뜻이다.
_MISSING_PROBE = ""


def _supports_hangul(font) -> bool:
    """파일이 있다는 것과 한글이 그려진다는 것은 다르다.

    DejaVu처럼 설치는 되어 있지만 한글 글리프가 없는 글꼴을 고르면 이름이
    전부 네모로 나온다. 그래서 존재 여부가 아니라 실제로 그려지는지를 본다.
    """
    try:
        drawn = font.getmask(_HANGUL_PROBE)
        missing = font.getmask(_MISSING_PROBE)
    except Exception:                                    # 글꼴이 이상한 경우
        return False
    if drawn.size[0] == 0:
        return False
    return bytes(drawn) != bytes(missing)


class FallbackFont(ImageFont.FreeTypeFont):
    """기본 글꼴에 없는 글자만 대체 글꼴로 그리는 글꼴 (오너 지시).

    기본 글꼴인 Neo둥근모(도트풍)에는 `→`(전투 로그의 모든 줄), `★☆`(성급),
    한자·일본어(닉네임 `月冴` 등)가 없어 네모로 깨졌다. Pillow에는 글꼴 대체
    기능이 없어서, 줄을 "같은 글꼴로 그릴 수 있는 글자 묶음"으로 나눠 기준선을
    맞춰 이어 그린다 — 나머지 글자는 도트 글꼴 그대로다. 그리기는 아래
    `ImageDraw` 감싸기가, 크기 측정은 이 클래스의 `getbbox`/`getlength`가 같은
    방식으로 한다.
    """

    def __init__(self, path: str, size: int, fallback) -> None:
        super().__init__(path, size)
        self._fallback = fallback
        # 글자 확인·묶음 그리기는 가로채지 않은 일반 글꼴로 한다 — `getmask`가
        # 내부에서 `getmask2`를 부르므로 자기 자신으로 하면 끝없이 되돌아온다.
        self._probe = ImageFont.truetype(path, size)
        self._covered: dict[str, bool] = {}
        self._missing_mask: bytes | None = None

    def _covers(self, char: str) -> bool:
        known = self._covered.get(char)
        if known is not None:
            return known
        if char.isspace():
            result = True
        else:
            if self._missing_mask is None:
                self._missing_mask = bytes(self._probe.getmask(_MISSING_PROBE[0]))
            try:
                result = bytes(self._probe.getmask(char)) != self._missing_mask
            except Exception:                                  # noqa: BLE001
                result = False
        self._covered[char] = result
        return result

    def _needs_fallback(self, text) -> bool:
        return isinstance(text, str) and not all(self._covers(ch) for ch in text)

    def runs(self, text: str) -> list[tuple[object, str]]:
        """(그릴 글꼴, 글자 묶음) 목록. 기본 글꼴에 있는 글자는 도트 글꼴로."""
        out: list[tuple[object, str]] = []
        for char in text:
            font = self._probe if self._covers(char) else self._fallback
            if out and out[-1][0] is font:
                out[-1] = (font, out[-1][1] + char)
            else:
                out.append((font, char))
        return out

    def baseline(self) -> int:
        """기본 글꼴의 윗선(ascender)에서 기준선까지 — 앵커 `la`의 기준."""
        return self._probe.getmetrics()[0]

    def _mixable(self, text, anchor) -> bool:
        return (anchor in (None, "la") and isinstance(text, str) and "\n" not in text
                and self._needs_fallback(text))

    def getmask2(self, text, *args, **kwargs):
        # `ImageDraw` 감싸기를 거치지 않는 그리기(다른 앵커 등)의 안전망 —
        # 이 경우만 줄 전체를 대체 글꼴로 그린다. 네모보다는 낫다.
        if self._needs_fallback(text):
            return self._fallback.getmask2(text, *args, **kwargs)
        return super().getmask2(text, *args, **kwargs)

    def getbbox(self, text, mode="", direction=None, features=None, language=None,
                stroke_width=0, anchor=None):
        if not self._mixable(text, anchor):
            if self._needs_fallback(text):
                return self._fallback.getbbox(text, mode, direction, features,
                                              language, stroke_width, anchor)
            return super().getbbox(text, mode, direction, features, language,
                                   stroke_width, anchor)
        x, top, bottom, left, right = 0.0, None, None, None, None
        base = self.baseline()
        for font, run in self.runs(text):
            l, t, r, b = font.getbbox(run, mode, direction, features, language,
                                      stroke_width, "ls")
            left = x + l if left is None else min(left, x + l)
            right = x + r if right is None else max(right, x + r)
            top = base + t if top is None else min(top, base + t)
            bottom = base + b if bottom is None else max(bottom, base + b)
            x += font.getlength(run, mode, direction, features, language)
        return (left, top, right, bottom)

    def getlength(self, text, mode="", direction=None, features=None, language=None):
        if isinstance(text, str) and self._needs_fallback(text):
            return sum(font.getlength(run, mode, direction, features, language)
                       for font, run in self.runs(text))
        return super().getlength(text, mode, direction, features, language)


_draw_text = ImageDraw.ImageDraw.text


def _mixed_text(self, xy, text, fill=None, font=None, anchor=None, *args, **kwargs):
    """`ImageDraw.text` — 글꼴이 `FallbackFont`이고 기본 글꼴에 없는 글자가
    있을 때만 글자 묶음별로 나눠 그린다. 그 밖에는 원래 함수 그대로다."""
    if not (isinstance(font, FallbackFont) and font._mixable(text, anchor)):
        return _draw_text(self, xy, text, fill, font, anchor, *args, **kwargs)
    x, y = xy
    base = y + font.baseline()
    for run_font, run in font.runs(text):
        _draw_text(self, (x, base), run, fill, run_font, "ls", *args, **kwargs)
        x += run_font.getlength(run)
    return None


ImageDraw.ImageDraw.text = _mixed_text


@lru_cache(maxsize=32)
def _load_font(candidates: tuple[str, ...], size: int):
    """설정에 적힌 순서대로 찾되, 한글이 그려지는 글꼴을 우선한다.

    한글이 되는 글꼴이 둘 이상이면 첫 번째를 기본으로, 두 번째를 기본
    글꼴에 없는 글자(→ ★ 한자 등)용 대체 글꼴로 쓴다(`FallbackFont`).

    한글이 되는 글꼴이 하나도 없으면 그래도 읽히는 것 중 첫 번째를 쓴다 —
    이름이 네모로 나올지언정 화면은 나가야 한다 (§11).
    """
    fallback = None
    primary: tuple[str, object] | None = None
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            continue
        try:
            font = ImageFont.truetype(str(path), size)
        except OSError:
            logger.warning("글꼴을 읽지 못했습니다: %s", path)
            continue
        if _supports_hangul(font):
            if primary is None:
                primary = (str(path), font)
                continue
            if path.resolve() == Path(primary[0]).resolve():
                continue
            try:
                return FallbackFont(primary[0], size, font)
            except OSError:
                return primary[1]
        fallback = fallback or font
    if primary is not None:
        return primary[1]

    if fallback is not None:
        logger.warning(
            "한글을 그릴 수 있는 글꼴이 없어 이름이 네모로 나옵니다. "
            "한글 .ttf 를 assets/fonts/main.ttf 에 넣거나 "
            "config/10_화면.toml 의 font_candidates 맨 위에 경로를 적으세요.")
        return fallback
    return ImageFont.load_default()


def load() -> Theme:
    """현재 설정 파일의 겉모습 값을 읽는다.

    캐시하지 않는다 — 파일을 고치면 다음 화면부터 바로 반영되는 편이 좋고,
    화면 하나를 그리는 비용에 비하면 파일 몇 개를 읽는 값은 미미하다.
    """
    return Theme(config_loader.load_presentation())
