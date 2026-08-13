"""관리자 대시보드 로그인 — 서명 쿠키.

의존성을 늘리지 않으려고 표준 라이브러리만 쓴다. 대시보드 하나 때문에
세션 라이브러리를 끌어오면 봇 서비스의 배포 조건이 그만큼 까다로워진다.

원칙 두 가지.

* **비밀번호가 설정돼 있지 않으면 대시보드는 켜지지 않는다.** 기본 비밀번호를
  두면 설정을 잊은 배포가 "열린 관리자 화면"으로 뜬다. 안 뜨는 편이 낫다.
* **비밀번호 비교는 상수 시간으로 한다.** 짧은 비밀번호일수록 빨리 틀리는
  비교는 그 자체로 정보를 흘린다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time

from app.config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "deckout_admin"

#: 프로세스마다 새로 만드는 서명 키. `DECKOUT_ADMIN_SECRET` 이 있으면 그것을
#: 쓰고, 없으면 이 값을 쓴다 — 재시작하면 로그인이 풀린다.
_EPHEMERAL_SECRET = secrets.token_hex(32)


def _secret() -> bytes:
    return (settings.admin_secret or _EPHEMERAL_SECRET).encode("utf-8")


def _sign(payload: str) -> str:
    digest = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue(now: float | None = None) -> str:
    """로그인 성공 시 내려줄 쿠키 값."""
    expires = int((now or time.time()) + settings.admin_session_hours * 3600)
    payload = f"{expires}"
    return f"{payload}.{_sign(payload)}"


def verify(cookie: str | None, now: float | None = None) -> bool:
    """쿠키가 우리가 발급한 것이고 아직 살아 있는가."""
    if not settings.admin_enabled or not cookie:
        return False
    payload, _, signature = cookie.rpartition(".")
    if not payload or not signature:
        return False
    # 바이트로 비교한다. `compare_digest` 는 비ASCII 문자열을 받으면 TypeError를
    # 내므로, 쿠키에 아무 값이나 들어와도 되는 이 자리에서는 str 비교가 위험하다.
    if not hmac.compare_digest(signature.encode("utf-8", "surrogatepass"),
                               _sign(payload).encode("ascii")):
        return False
    try:
        expires = int(payload)
    except ValueError:
        return False
    return (now or time.time()) < expires


def password_matches(attempt: str) -> bool:
    """상수 시간 비교. 비밀번호가 없으면 어떤 입력도 통과하지 못한다.

    바이트로 비교한다 — `compare_digest` 에 비ASCII 문자열을 넘기면 TypeError가
    나므로, 한글 비밀번호를 쓴 배포에서 로그인이 500으로 죽는다.
    """
    if not settings.admin_enabled:
        return False
    return hmac.compare_digest((attempt or "").encode("utf-8"),
                               settings.admin_password.encode("utf-8"))
