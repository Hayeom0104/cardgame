"""관리자 대시보드 인증 (설계 문서 §10.2).

* 로그인은 **디스코드 OAuth** — 별도 아이디/비밀번호를 만들지 않는다.
* 권한은 `Owner` / `Editor` 2단계.
    - Owner: 콘텐츠 읽기/쓰기 + 사용자 관리
    - Editor: 콘텐츠 읽기/쓰기, 사용자 관리 불가
  §10.2 에 "세부 granularity 는 조정 가능"이라고 되어 있어, 권한 판정은
  전부 이 파일의 의존성 함수에 모아두었다.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.database import get_session
from ...db.models import AdminRole, AdminUser, utcnow

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = f"{DISCORD_API}/oauth2/token"
SCOPES = "identify"

SESSION_KEY = "admin_discord_id"


def oauth_configured() -> bool:
    s = get_settings()
    return bool(s.discord_client_id and s.discord_client_secret)


def authorize_url(state: str) -> str:
    s = get_settings()
    from urllib.parse import urlencode

    params = {
        "client_id": s.discord_client_id,
        "redirect_uri": s.discord_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    s = get_settings()
    data = {
        "client_id": s.discord_client_id,
        "client_secret": s.discord_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": s.discord_redirect_uri,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        user_resp = await client.get(
            f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {access_token}"}
        )
        user_resp.raise_for_status()
        return user_resp.json()


def upsert_admin(session: Session, profile: dict) -> AdminUser:
    """디스코드 프로필로 관리자 계정을 만들거나 갱신한다.

    Owner 승격 규칙:
      1. 설정의 `bootstrap_owner_ids` 에 포함된 계정
      2. 관리자 테이블이 완전히 비어 있을 때의 최초 로그인 (초기 부트스트랩)
    그 외에는 Editor 로 생성되며, 승격은 Owner 가 대시보드에서 한다.
    """
    settings = get_settings()
    discord_id = str(profile["id"])

    admin = session.scalar(select(AdminUser).where(AdminUser.discord_id == discord_id))
    if admin is None:
        total = session.scalar(select(func.count()).select_from(AdminUser)) or 0
        role = (
            AdminRole.OWNER
            if discord_id in settings.bootstrap_owner_ids or total == 0
            else AdminRole.EDITOR
        )
        admin = AdminUser(discord_id=discord_id, role=role)
        session.add(admin)

    admin.username = profile.get("username", "")
    admin.avatar = profile.get("avatar")
    admin.last_login_at = utcnow()
    if discord_id in settings.bootstrap_owner_ids:
        admin.role = AdminRole.OWNER

    session.commit()
    return admin


# ---------------------------------------------------------------------------
# 의존성
# ---------------------------------------------------------------------------


def current_admin(
    request: Request, session: Session = Depends(get_session)
) -> AdminUser | None:
    discord_id = request.session.get(SESSION_KEY)
    if not discord_id:
        return None
    admin = session.scalar(select(AdminUser).where(AdminUser.discord_id == str(discord_id)))
    if admin is None or not admin.is_active:
        return None
    return admin


def require_admin(admin: AdminUser | None = Depends(current_admin)) -> AdminUser:
    if admin is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다.")
    return admin


def require_owner(admin: AdminUser = Depends(require_admin)) -> AdminUser:
    if admin.role is not AdminRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Owner 권한이 필요합니다."
        )
    return admin
