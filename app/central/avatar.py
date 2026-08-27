"""사용자 프로필 사진 — 허브 대시보드 초상 (오너 요청).

연동 가이드에 정확한 응답 필드 이름이 이 저장소엔 없어서(§1.1은 경로만
적혀 있다) `handlers.COIN_BALANCE_KEYS`/`DISPLAY_NAME_KEYS`와 같은 방식으로
흔한 후보 이름을 순서대로 찾는다.

원격 그림을 받아 오는 것은 렌더링 준비 과정에 새로 들어온 I/O다. 실패는
전부 조용히 삼키고 `None`을 돌려준다(§11 — 그림이 없어도 화면은 나가야
한다). 예산도 짧게 잡는다 — 허브 화면 하나에서 이미 Central 프로필 호출이
한 번 나가는데(R3 M-01), 프로필 사진까지 느리면 인터랙션 예산(§1.2,
2.0초)을 넘긴다. 여기서 쓰는 시간은 그 예산과 별개로, 실패해도 화면
전체를 물고 늘어지지 않을 만큼만 짧게 잡는다.
"""

from __future__ import annotations

import io
import logging

import httpx
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

#: 흔히 쓰이는 필드 이름 후보. 실제로 어느 것이 맞는지 확인되면 이 목록
#: 맨 앞으로 옮기고 나머지는 지운다.
AVATAR_URL_KEYS = ("avatar_url", "avatar", "avatar_url_512", "icon_url",
                   "profile_image_url", "display_avatar_url")

#: 프로필 사진 하나 받아 오는 데 쓸 최대 시간(초). 실패하면 그냥
#: 기존 캐릭터 그림으로 물러난다 — 재시도하지 않는다.
FETCH_TIMEOUT_SECONDS = 0.8

#: 이보다 큰 응답은 프로필 사진이 아니라고 보고 버린다.
MAX_BYTES = 2 * 1024 * 1024


def avatar_url(profile: dict) -> str | None:
    """프로필 dict에서 그림 주소를 찾는다. 없으면 None."""
    for key in AVATAR_URL_KEYS:
        value = profile.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def fetch_avatar(url: str) -> Image.Image | None:
    """그림을 받아 연다. 무엇이든 실패하면 None — 절대 예외를 올리지 않는다."""
    try:
        response = httpx.get(url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.info("프로필 사진을 받지 못했습니다: %s", url)
        return None

    content = response.content
    if not content or len(content) > MAX_BYTES:
        return None

    try:
        image = Image.open(io.BytesIO(content))
        image.load()
        return image.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError):
        logger.info("프로필 사진을 열지 못했습니다: %s", url)
        return None
