"""사용자 프로필 사진 — 허브 대시보드 초상 (오너 요청).

연동 가이드(2026-09-11) §6.3/§7 확인 결과, 아바타 URL은 Central 프로필 API
(`GET /v1/users/{id}`)가 아니라 `/event` 페이로드 자체의 `avatar_url`에서
온다 — `app.content.seed.remember_identity()`가 이벤트가 올 때마다 계정에
적어 둔다. 이 모듈은 그 URL이 주어졌을 때 그림을 받아 오는 부분만 한다.

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

#: 프로필 사진 하나 받아 오는 데 쓸 최대 시간(초). 실패하면 그냥
#: 기존 캐릭터 그림으로 물러난다 — 재시도하지 않는다.
FETCH_TIMEOUT_SECONDS = 0.8

#: 이보다 큰 응답은 프로필 사진이 아니라고 보고 버린다.
MAX_BYTES = 2 * 1024 * 1024


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
