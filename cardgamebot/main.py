"""FastAPI 진입점 (설계 문서 §1).

이 서버 하나가 두 가지 역할을 한다:

1. 중앙봇이 호출하는 게임 API (`POST /event`) — 디스코드 연결은 중앙봇이 담당.
2. 콘텐츠 관리 대시보드 (`/admin`) — §10.3 에서 "같은 FastAPI 앱의 라우트
   그룹으로 두어도 된다"고 한 옵션을 택했다. 분리 배포가 필요해지면 admin
   라우터만 떼어내면 된다.

실행: `uvicorn cardgamebot.main:app --reload --port 8080`
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .api.admin.routes import router as admin_router
from .api.event import router as event_router
from .config import get_settings
from .db.database import init_db, session_scope
from .db.seed import seed_all

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    with session_scope() as session:
        counts = seed_all(session)
    if any(counts.values()):
        log.info("초기 콘텐츠 시드 완료: %s", counts)
    log.info("%s 기동 완료 (DB: %s)", settings.bot_name, settings.db_path)
    yield


settings = get_settings()

app = FastAPI(
    title=f"{settings.bot_name} API",
    description=(
        "ARI 미니게임 카드 배틀 봇. 디스코드 전달은 중앙봇이 프록시한다 (설계 문서 §1)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# 대시보드 로그인 세션 (§10.2 디스코드 OAuth)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="cardgame_admin",
    same_site="lax",
    https_only=False,
)

app.include_router(event_router)
app.include_router(admin_router)

# 대시보드에서 업로드한 일러스트 (§10.2 → §11 렌더링 파이프라인)
app.mount("/uploads", StaticFiles(directory=str(settings.upload_dir)), name="uploads")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """대시보드는 HTML, API 는 JSON 으로 오류를 낸다."""
    if request.url.path.startswith("/admin"):
        return HTMLResponse(
            f"""<body style="background:#16181f;color:#e8eaf0;font-family:sans-serif;padding:60px;text-align:center">
            <h1 style="font-size:48px;margin:0">{exc.status_code}</h1>
            <p style="color:#969baa">{exc.detail}</p>
            <p><a href="/admin/" style="color:#6ca0ff">대시보드로 돌아가기</a></p></body>""",
            status_code=exc.status_code,
        )
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.get("/")
async def root() -> dict:
    return {
        "service": settings.bot_name,
        "docs": "/docs",
        "admin": "/admin/",
        "event_endpoint": "POST /event",
    }
