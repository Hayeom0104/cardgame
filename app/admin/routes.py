"""§10.1–10.3 관리자 대시보드 — HTTP 층.

`/admin` 아래에 붙는다. 게임 서비스와 같은 프로세스에서 돌지만 `/event` 와는
완전히 분리돼 있다 — 중앙봇은 이 경로를 알지 못하고, 여기서 무엇을 하든
§1.3의 계약에 영향을 주지 않는다.

두 가지를 지킨다.

* **비밀번호가 없으면 아무것도 열지 않는다.** 인증 화면조차 띄우지 않고
  "꺼져 있다"고 답한다.
* **변경은 전부 JSON POST.** 폼 인코딩이나 멀티파트를 쓰지 않아 추가
  의존성이 없다. 그림 업로드도 원본 바이트를 그대로 받는다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin import auth, service
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _key_parts(row: dict, keys: tuple) -> str:
    """행의 논리 키를 URL 한 조각으로. 값에 `/` 가 있으면 못 쓰므로 인코딩한다."""
    from urllib.parse import quote

    return "~".join(quote(str(row[column]), safe="") for column in keys)


templates.env.filters["keyparts"] = _key_parts


# =====================================================================
# 공통
# =====================================================================
def _db(request: Request):
    from app.api.server import state

    db = state.get("db")
    if db is None:                                # 서비스 밖에서 임포트된 경우
        raise RuntimeError("database is not initialised")
    return db


def _balance(request: Request):
    from app.api.server import state

    return state.get("balance")


def _render(request: Request, template: str, **context) -> HTMLResponse:
    db = _db(request)
    return templates.TemplateResponse(request, template, {
        "logged_in": True,
        "current_version": service.vs.current_version_id(db),
        "draft_version": service.draft_version_id(db),
        **context,
    })


def _guard(request: Request):
    """로그인 상태를 확인한다. 통과하지 못하면 대신 내보낼 응답을 돌려준다."""
    if not settings.admin_enabled:
        return templates.TemplateResponse(request, "disabled.html",
                                          {"logged_in": False}, status_code=503)
    if not auth.verify(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/admin/login", status_code=303)
    return None


def _fail(error: Exception, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "message": str(error)}, status_code=status)


def _ok(message: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, **extra})


# =====================================================================
# 로그인
# =====================================================================
@router.get("/login")
async def login_page(request: Request):
    if not settings.admin_enabled:
        return templates.TemplateResponse(request, "disabled.html",
                                          {"logged_in": False}, status_code=503)
    return templates.TemplateResponse(request, "login.html", {"logged_in": False})


@router.post("/login")
async def login(request: Request):
    if not settings.admin_enabled:
        # 꺼져 있을 때는 인증을 시도할 수조차 없다. 401을 주면 "비밀번호가
        # 있는데 틀렸다"는 뜻이 되어, 없다는 사실을 감추려다 오히려 흘린다.
        return JSONResponse({"ok": False, "message": "대시보드가 꺼져 있습니다."},
                            status_code=503)
    body = await request.json()
    if not auth.password_matches(body.get("password", "")):
        # 왜 틀렸는지는 알려주지 않는다. 비밀번호가 설정돼 있는지조차 흘리지 않는다.
        logger.warning("관리자 로그인 실패")
        return JSONResponse({"ok": False}, status_code=401)

    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE_NAME, auth.issue(), httponly=True, samesite="lax",
        max_age=settings.admin_session_hours * 3600,
        # HTTPS 뒤에 두는 것이 정상이지만, 로컬에서도 쓸 수 있어야 하므로
        # secure 는 배포에서 리버스 프록시가 담당한다.
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


# =====================================================================
# 현황
# =====================================================================
@router.get("/")
async def index(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    overview = service.overview(db, _balance(request))

    missing = 0
    version = overview["current_version"] or overview["draft_version"]
    if version is not None:
        report = service.asset_report(db, version)
        missing = sum(1 for entry in report
                      if not entry["present"] or entry["problem"])
    return _render(request, "index.html", overview=overview,
                   missing_assets=missing)


@router.post("/actions/reap")
async def reap(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    from app.engine import lifecycle as lc

    db, balance = _db(request), _balance(request)
    if balance is None:
        return _fail(RuntimeError("콘텐츠 버전이 없어 만료를 판정할 수 없습니다."))

    settled = 0
    for run in db.query(
        "SELECT * FROM runs WHERE state NOT IN "
        "('run_completed','run_defeated','run_abandoned','run_expired',"
        "'admin_terminated')",
    ):
        if not lc.is_expired(db, balance, run):
            continue
        try:
            lc.expire_run(db, balance, run)
            settled += 1
        except Exception:                                    # noqa: BLE001
            # 하나가 걸려도 나머지는 계속 정리한다.
            logger.exception("run %s 만료 정산 실패", run["run_id"])
    return _ok(f"만료된 런 {settled}개를 정산했습니다.")


# =====================================================================
# 밸런싱
# =====================================================================
@router.get("/balance")
async def balance_page(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    version_id = service.editable_version_id(db)
    rows = service.balance_rows(db, version_id)

    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["source_file"], []).append(row)
    return _render(request, "balance.html", version_id=version_id, rows=rows,
                   grouped=dict(sorted(grouped.items())))


@router.post("/balance/save")
async def balance_save(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    body = await request.json()
    try:
        service.save_constant(db, service.editable_version_id(db),
                              body["key"], body["value"])
    except (service.AdminError, KeyError) as error:
        return _fail(error)
    return _ok(f"{body['key']} 을(를) 저장했습니다.")


# =====================================================================
# 콘텐츠
# =====================================================================
@router.get("/content")
async def content_index(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    version_id = service.editable_version_id(db)
    return _render(request, "content.html", version_id=version_id,
                   tables=service.content_tables(db, version_id))


@router.get("/content/{table}")
async def content_table(request: Request, table: str):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    version_id = service.editable_version_id(db)
    try:
        rows = service.content_rows(db, table, version_id)
    except service.AdminError as error:
        return HTMLResponse(str(error), status_code=404)

    columns = [column for column in (rows[0].keys() if rows else [])
               if column != "content_version_id"]
    return _render(request, "content_table.html", table=table, rows=rows,
                   columns=columns, version_id=version_id,
                   keys=service.vs.CONTENT_TABLES[table])


@router.get("/content/{table}/{key_path}")
async def content_row(request: Request, table: str, key_path: str):
    if (blocked := _guard(request)) is not None:
        return blocked
    from urllib.parse import unquote

    db = _db(request)
    version_id = service.editable_version_id(db)
    parts = [unquote(part) for part in key_path.split("~")]
    try:
        row = service.content_row(db, table, version_id, parts)
    except service.AdminError as error:
        return HTMLResponse(str(error), status_code=404)
    if row is None:
        return HTMLResponse("해당 행이 없습니다.", status_code=404)

    pretty = {column: _pretty(value) for column, value in row.items()}
    return _render(request, "content_row.html", table=table, row=row,
                   pretty=pretty, version_id=version_id, key_path=key_path,
                   keys=service.vs.CONTENT_TABLES[table])


def _pretty(value: Any) -> str:
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), ensure_ascii=False, indent=2)
        except ValueError:
            return value
    return "" if value is None else str(value)


@router.post("/content/{table}/{key_path}/save")
async def content_save(request: Request, table: str, key_path: str):
    if (blocked := _guard(request)) is not None:
        return blocked
    from urllib.parse import unquote

    db = _db(request)
    body = await request.json()
    parts = [unquote(part) for part in key_path.split("~")]
    values = {}
    for column, raw in (body.get("values") or {}).items():
        if column.endswith("_json"):
            # 저장하기 전에 JSON으로 읽히는지 본다. 깨진 값을 넣어 두면 그 값을
            # 처음 읽는 순간 — 대개 전투 도중에 — 터진다.
            try:
                values[column] = json.dumps(json.loads(raw), ensure_ascii=False)
            except ValueError as error:
                return _fail(ValueError(f"{column}: JSON으로 읽을 수 없습니다 — {error}"))
        else:
            values[column] = raw
    try:
        service.save_content_row(db, table, service.editable_version_id(db),
                                 parts, values)
    except service.AdminError as error:
        return _fail(error)
    return _ok("저장했습니다.")


# =====================================================================
# 버전
# =====================================================================
@router.get("/versions")
async def versions_page(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    return _render(request, "versions.html", versions=service.versions(db))


@router.post("/versions/draft")
async def versions_draft(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    version_id = service.open_draft(_db(request))
    return _ok(f"초안 v{version_id} 을(를) 열었습니다.", version_id=version_id)


@router.post("/versions/validate")
async def versions_validate(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    draft = service.draft_version_id(db)
    if draft is None:
        return _fail(service.AdminError("검증할 초안이 없습니다."))
    reason = service.validate(db, draft)
    if reason:
        return JSONResponse({"ok": False, "message": reason}, status_code=200)
    return _ok("§10.5 검증을 통과했습니다.")


@router.post("/versions/publish")
async def versions_publish(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    try:
        stamp = service.publish_draft(db)
    except service.AdminError as error:
        return _fail(error)

    # 발행 뒤에는 서비스가 새 버전을 읽어야 한다. 진행 중인 런은 자기 버전을
    # 그대로 쓰므로 영향받지 않는다 (§10.6).
    from app.api.server import state
    from app.content.balance import Balance
    from app.content.versioning import current_version_id

    version_id = current_version_id(db)
    state["content_version_id"] = version_id
    state["balance"] = Balance(db, version_id) if version_id else None
    return _ok(f"v{version_id} 을(를) 발행했습니다. ({stamp[:12]})")


@router.post("/versions/discard")
async def versions_discard(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    service.discard_draft(_db(request))
    return _ok("초안을 버렸습니다.")


# =====================================================================
# 그림
# =====================================================================
@router.get("/assets")
async def assets_page(request: Request):
    if (blocked := _guard(request)) is not None:
        return blocked
    db = _db(request)
    version_id = (service.vs.current_version_id(db)
                  or service.draft_version_id(db))
    report = service.asset_report(db, version_id) if version_id else []
    return _render(request, "assets.html", report=report,
                   present=sum(1 for e in report if e["present"] and not e["problem"]),
                   missing=sum(1 for e in report if not e["present"]),
                   broken=sum(1 for e in report if e["problem"]))


@router.post("/assets/{kind}/{entity_id}")
async def asset_upload(request: Request, kind: str, entity_id: str):
    if (blocked := _guard(request)) is not None:
        return blocked
    data = await request.body()
    if not data:
        return _fail(service.AdminError("빈 파일입니다."))
    try:
        path = service.save_asset(kind, entity_id, data)
    except (service.AdminError, KeyError) as error:
        return _fail(error)
    return _ok(f"{Path(path).name} 을(를) 올렸습니다.")


# =====================================================================
# 플레이어
# =====================================================================
@router.get("/players")
async def players_page(request: Request, q: str = ""):
    if (blocked := _guard(request)) is not None:
        return blocked
    return _render(request, "players.html", needle=q,
                   players=service.players(_db(request), q))


@router.get("/players/{user_id}")
async def player_page(request: Request, user_id: int):
    if (blocked := _guard(request)) is not None:
        return blocked
    from app.engine import lifecycle as lc

    detail = service.player_detail(_db(request), user_id)
    if detail is None:
        return HTMLResponse("계정이 없습니다.", status_code=404)
    return _render(request, "player.html", detail=detail,
                   terminal=lc.TERMINAL_STATES)


@router.post("/players/runs/{run_id}/terminate")
async def terminate_run(request: Request, run_id: int):
    if (blocked := _guard(request)) is not None:
        return blocked
    body = await request.json()
    reason = (body.get("reason") or "").strip()
    if not reason:
        return _fail(service.AdminError("사유를 남겨야 합니다."))
    try:
        service.terminate_run(_db(request), run_id, reason)
    except service.AdminError as error:
        return _fail(error)
    return _ok(f"런 {run_id} 을(를) 종료했습니다.")
