"""Service configuration.

§1.3.0 registration settings, for the Central Bot's config file:

    minigames:
      deckout:
        channel: "<parent-channel-id>"      # guide §6 key name — NOT `channel_id`
        service_url: "http://localhost:<port>"
        route_threads: true                 # MANDATORY — default is false

`route_threads: true` is required and defaults to false. Every run lives in a
private thread (§16), so without it no in-run component interaction is ever
delivered to this service and the game is completely non-functional.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: §1.3.9 API key scopes. `achievement:grant` is deliberately excluded —
#: achievements are local (§20.1) — and `xp:add` too, because Central runs its
#: own activity XP module and double-crediting is forbidden (guide §1).
REQUIRED_SCOPES = ("read", "currency:grant", "currency:deduct")

REGISTER_COMMAND = (
    "python -m app.cli.register_bot --name deckout "
    "--scopes read,currency:grant,currency:deduct"
)

#: §1.2 timing budgets.
BUDGET_MESSAGE_SECONDS = 2.5
BUDGET_INTERACTION_SECONDS = 2.0


@dataclass
class Settings:
    database_path: str = os.environ.get("DECKOUT_DB", "deckout.db")
    central_base_url: str = os.environ.get("DECKOUT_CENTRAL_URL", "http://localhost:8000")
    central_api_key: str = os.environ.get("DECKOUT_API_KEY", "")
    parent_channel_id: int = int(os.environ.get("DECKOUT_CHANNEL_ID", "0"))
    bot_name: str = "deckout"
    command_prefix: str = "!덱아웃"
    channel_name: str = "Deckout"
    component_namespace: str = "dko:"
    #: Skips the §1.3.4 capability probe. Development and tests only — the
    #: service otherwise refuses to start against an unverified Central Bot.
    skip_capability_check: bool = os.environ.get("DECKOUT_SKIP_CAPABILITY_CHECK") == "1"

    # -- §10.1 관리자 대시보드 -----------------------------------------
    #: 비어 있으면 대시보드는 **꺼진다**. 기본 비밀번호는 두지 않는다 —
    #: 설정을 잊은 배포가 열린 대시보드로 뜨는 것보다 안 뜨는 편이 낫다.
    admin_password: str = os.environ.get("DECKOUT_ADMIN_PASSWORD", "")
    #: 세션 쿠키 서명 키. 비워 두면 프로세스마다 새로 만든다 — 재시작하면
    #: 로그인이 풀릴 뿐이고, 약한 고정 키를 쓰는 것보다 안전하다.
    admin_secret: str = os.environ.get("DECKOUT_ADMIN_SECRET", "")
    admin_session_hours: int = int(os.environ.get("DECKOUT_ADMIN_SESSION_HOURS", "12"))

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_password)

    def registration_yaml(self) -> str:
        return (
            "minigames:\n"
            f"  {self.bot_name}:\n"
            f'    channel: "{self.parent_channel_id}"\n'
            f'    service_url: "{self.central_base_url}"\n'
            "    route_threads: true\n"
        )


settings = Settings()
