"""관리자 대시보드 라우트 (설계 문서 §10).

브라우저 폼 기반 CMS 로, 이 봇이 소유한 SQLite DB 에 직접 쓴다(§10.3).
디스코드를 거치지 않는다.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.database import get_session
from ...db.models import AdminRole, AdminUser, AuditLog
from . import auth
from .schema import SPECS, EntitySpec

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _ctx(request: Request, admin: AdminUser | None, **extra) -> dict:
    return {
        "request": request,
        "admin": admin,
        "specs": SPECS,
        "settings": get_settings(),
        **extra,
    }


# ---------------------------------------------------------------------------
# 인증 (§10.2 디스코드 OAuth)
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    if not auth.oauth_configured():
        return templates.TemplateResponse(
            request,
            "login.html",
            _ctx(request, None, error=(
                "디스코드 OAuth 가 설정되지 않았습니다. "
                "CARDGAME_DISCORD_CLIENT_ID / CARDGAME_DISCORD_CLIENT_SECRET / "
                "CARDGAME_DISCORD_REDIRECT_URI 환경 변수를 설정해주세요."
            )),
        )
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    return RedirectResponse(auth.authorize_url(state), status_code=302)


@router.get("/auth/callback")
async def oauth_callback(
    request: Request, code: str = "", state: str = "", session: Session = Depends(get_session)
):
    expected = request.session.pop("oauth_state", None)
    if not code or not state or state != expected:
        raise HTTPException(status_code=400, detail="OAuth state 검증에 실패했습니다.")

    profile = await auth.exchange_code(code)
    admin = auth.upsert_admin(session, profile)
    request.session[auth.SESSION_KEY] = admin.discord_id
    return RedirectResponse("/admin/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=302)


# ---------------------------------------------------------------------------
# 대시보드
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: AdminUser | None = Depends(auth.current_admin),
    session: Session = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)

    counts = {
        key: session.scalar(select(func.count()).select_from(spec.model)) or 0
        for key, spec in SPECS.items()
    }
    recent = session.scalars(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(15)
    ).all()
    return templates.TemplateResponse(
        request, "dashboard.html", _ctx(request, admin, counts=counts, recent=recent)
    )


# ---------------------------------------------------------------------------
# 콘텐츠 CRUD (§10.1 카드 / 적 / 캐릭터)
# ---------------------------------------------------------------------------


def _spec_or_404(entity: str) -> EntitySpec:
    spec = SPECS.get(entity)
    if spec is None:
        raise HTTPException(status_code=404, detail="알 수 없는 엔티티입니다.")
    return spec


async def _save_image(upload: UploadFile | None) -> str | None:
    """§10.2 대시보드에서 일러스트를 직접 업로드한다."""
    if upload is None or not upload.filename:
        return None
    if upload.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 이미지 형식: {upload.content_type}")

    data = await upload.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="이미지가 너무 큽니다. (최대 4MB)")

    suffix = Path(upload.filename).suffix.lower() or ".png"
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        raise HTTPException(status_code=400, detail="지원하지 않는 확장자입니다.")

    name = f"{uuid.uuid4().hex}{suffix}"
    target = get_settings().upload_dir / name
    target.write_bytes(data)
    # DB 에는 업로드 디렉터리 기준 상대 경로만 저장한다.
    return name


def _coerce(spec: EntitySpec, form: dict) -> dict:
    values: dict = {}
    for f in spec.fields:
        if f.type == "image":
            continue  # 업로드는 별도 처리
        raw = form.get(f.name)

        if f.type == "bool":
            values[f.name] = raw is not None
            continue
        if raw is None:
            continue
        raw = raw.strip() if isinstance(raw, str) else raw

        if f.type == "int":
            try:
                values[f.name] = int(raw or 0)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{f.label}: 숫자를 입력해주세요.") from exc
        elif f.type == "json":
            try:
                parsed = json.loads(raw or "[]")
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=400, detail=f"{f.label}: JSON 형식이 올바르지 않습니다. ({exc.msg})"
                ) from exc
            values[f.name] = parsed
        elif f.name == "character_code":
            values[f.name] = raw or None
        else:
            values[f.name] = raw

    missing = [fld.label for fld in spec.fields if fld.required and not values.get(fld.name)]
    if missing:
        raise HTTPException(status_code=400, detail=f"필수 항목 누락: {', '.join(missing)}")
    return values


def _audit(session: Session, admin: AdminUser, action: str, spec: EntitySpec, code: str) -> None:
    session.add(
        AuditLog(
            actor_discord_id=admin.discord_id,
            action=action,
            entity=spec.key,
            entity_code=code,
            detail={"by": admin.username},
        )
    )


@router.get("/{entity}", response_class=HTMLResponse)
async def list_entities(
    entity: str,
    request: Request,
    admin: AdminUser | None = Depends(auth.current_admin),
    session: Session = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    spec = _spec_or_404(entity)
    rows = session.scalars(select(spec.model).order_by(spec.model.id.desc())).all()
    return templates.TemplateResponse(
        request, "list.html", _ctx(request, admin, spec=spec, rows=rows)
    )


@router.get("/{entity}/new", response_class=HTMLResponse)
async def new_entity(
    entity: str,
    request: Request,
    admin: AdminUser | None = Depends(auth.current_admin),
):
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    spec = _spec_or_404(entity)
    return templates.TemplateResponse(
        request, "form.html", _ctx(request, admin, spec=spec, row=None)
    )


@router.post("/{entity}/new")
async def create_entity(
    entity: str,
    request: Request,
    admin: AdminUser = Depends(auth.require_admin),
    session: Session = Depends(get_session),
    image: UploadFile | None = File(None),
):
    spec = _spec_or_404(entity)
    form = dict(await request.form())
    values = _coerce(spec, form)

    exists = session.scalar(select(spec.model).where(spec.model.code == values["code"]))
    if exists is not None:
        raise HTTPException(status_code=400, detail=f"코드 `{values['code']}` 는 이미 존재합니다.")

    saved = await _save_image(image)
    if saved and spec.image_field:
        values[spec.image_field] = saved

    row = spec.model(**values)
    session.add(row)
    _audit(session, admin, "create", spec, values["code"])
    session.commit()
    return RedirectResponse(f"/admin/{entity}", status_code=303)


@router.get("/{entity}/{code}/edit", response_class=HTMLResponse)
async def edit_entity(
    entity: str,
    code: str,
    request: Request,
    admin: AdminUser | None = Depends(auth.current_admin),
    session: Session = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/admin/login", status_code=302)
    spec = _spec_or_404(entity)
    row = session.scalar(select(spec.model).where(spec.model.code == code))
    if row is None:
        raise HTTPException(status_code=404, detail="대상을 찾을 수 없습니다.")
    return templates.TemplateResponse(request, "form.html", _ctx(request, admin, spec=spec, row=row))


@router.post("/{entity}/{code}/edit")
async def update_entity(
    entity: str,
    code: str,
    request: Request,
    admin: AdminUser = Depends(auth.require_admin),
    session: Session = Depends(get_session),
    image: UploadFile | None = File(None),
):
    spec = _spec_or_404(entity)
    row = session.scalar(select(spec.model).where(spec.model.code == code))
    if row is None:
        raise HTTPException(status_code=404, detail="대상을 찾을 수 없습니다.")

    form = dict(await request.form())
    values = _coerce(spec, form)
    values.pop("code", None)  # 코드는 식별자라 변경하지 않는다.

    saved = await _save_image(image)
    if saved and spec.image_field:
        values[spec.image_field] = saved

    for key, value in values.items():
        setattr(row, key, value)

    _audit(session, admin, "update", spec, code)
    session.commit()
    return RedirectResponse(f"/admin/{entity}", status_code=303)


@router.post("/{entity}/{code}/delete")
async def delete_entity(
    entity: str,
    code: str,
    admin: AdminUser = Depends(auth.require_admin),
    session: Session = Depends(get_session),
):
    spec = _spec_or_404(entity)
    row = session.scalar(select(spec.model).where(spec.model.code == code))
    if row is None:
        raise HTTPException(status_code=404, detail="대상을 찾을 수 없습니다.")

    # 진행 중인 런이 참조할 수 있으므로 물리 삭제 대신 비활성화한다.
    row.is_active = False
    _audit(session, admin, "deactivate", spec, code)
    session.commit()
    return RedirectResponse(f"/admin/{entity}", status_code=303)


# ---------------------------------------------------------------------------
# 사용자 관리 (§10.2 — Owner 전용)
# ---------------------------------------------------------------------------


@router.get("/users/manage", response_class=HTMLResponse)
async def list_admins(
    request: Request,
    admin: AdminUser = Depends(auth.require_owner),
    session: Session = Depends(get_session),
):
    admins = session.scalars(select(AdminUser).order_by(AdminUser.id)).all()
    return templates.TemplateResponse(request, "users.html", _ctx(request, admin, admins=admins))


@router.post("/users/manage/{admin_id}")
async def update_admin(
    admin_id: int,
    role: str = Form(...),
    is_active: str | None = Form(None),
    owner: AdminUser = Depends(auth.require_owner),
    session: Session = Depends(get_session),
):
    target = session.get(AdminUser, admin_id)
    if target is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")

    if target.id == owner.id and role != AdminRole.OWNER.value:
        raise HTTPException(status_code=400, detail="자신의 Owner 권한은 해제할 수 없습니다.")

    try:
        target.role = AdminRole(role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="알 수 없는 권한입니다.") from exc

    target.is_active = is_active is not None
    session.commit()
    return RedirectResponse("/admin/users/manage", status_code=303)
