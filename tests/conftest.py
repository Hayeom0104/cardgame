from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 설정 캐시가 잡히기 전에 테스트용 경로를 주입해야 한다.
_TMP = Path(tempfile.mkdtemp(prefix="cardgame-test-"))
os.environ.setdefault("CARDGAME_DB_PATH", str(_TMP / "test.db"))
os.environ.setdefault("CARDGAME_UPLOAD_DIR", str(_TMP / "uploads"))
os.environ.setdefault("CARDGAME_RENDER_CACHE_DIR", str(_TMP / "cache"))
# 테스트는 실제 중앙봇 또는 로컬 .env의 비밀값에 의존하지 않는다.
os.environ["CARDGAME_CENTRAL_API_ENABLED"] = "false"
# 운영 .env의 실제 Discord 채널 제한은 테스트의 가상 채널에 적용하지 않는다.
os.environ["CARDGAME_ALLOWED_CHANNEL_IDS"] = "[]"

from cardgamebot.db.database import SessionLocal, engine  # noqa: E402
from cardgamebot.db.models import Base  # noqa: E402
from cardgamebot.db.seed import seed_all  # noqa: E402


@pytest.fixture()
def session():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = SessionLocal()
    seed_all(s)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def user(session):
    from cardgamebot.core.commands import get_or_create_user

    u = get_or_create_user(session, "test-user-1", "테스터")
    session.commit()
    return u
