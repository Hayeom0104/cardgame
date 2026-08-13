"""애플리케이션 설정.

환경 변수 또는 .env 파일로 주입한다. 봇 이름은 설계 문서 §0에서 TBD이므로
`CardGameBot` 을 플레이스홀더로 사용한다.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CARDGAME_", extra="ignore")

    # --- 봇 정체성 (§0: 최종 이름 TBD) ---
    bot_name: str = "CardGameBot"

    # --- 저장소 ---
    # 설계 문서 §1.1: 중앙봇의 central.db 와 분리된 자체 SQLite DB.
    db_path: Path = BASE_DIR / "data" / "cardgame.db"
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    render_cache_dir: Path = BASE_DIR / "data" / "render_cache"

    # --- 중앙봇 연동 (§1, §1.1) ---
    central_api_base: str = "http://localhost:8000"
    central_api_key: str = ""
    # Deprecated compatibility alias; use CARDGAME_CENTRAL_API_KEY.
    central_api_token: str = ""
    central_api_timeout: float = 5.0
    # 중앙봇이 아직 없는 개발 환경에서 코인/XP 호출을 로컬 스텁으로 처리한다.
    central_api_enabled: bool = False

    # --- 이 서비스의 HTTP 수신 주소 ---
    # 중앙봇이 service_url 로 호출하는 FastAPI 서버의 bind 설정이다.
    service_host: str = "0.0.0.0"
    service_port: int = 8090

    # --- 명령 처리 ---
    command_prefix: str = "!"
    # 비어 있으면 모든 채널 허용. 운영 시 지정 채널만 넣는다.
    allowed_channel_ids: list[str] = []

    # --- 관리자 대시보드 (§10) ---
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = "http://localhost:8080/admin/auth/callback"
    session_secret: str = "dev-insecure-change-me"
    # 최초 로그인 시 Owner 권한을 부여할 디스코드 유저 ID 목록.
    bootstrap_owner_ids: list[str] = []

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    s.render_cache_dir.mkdir(parents=True, exist_ok=True)
    return s
