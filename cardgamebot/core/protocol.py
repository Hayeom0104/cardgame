"""중앙봇 ↔ 이 봇 사이의 `POST /event` 계약 (설계 문서 §1, §8 참조 항목).

이 봇은 디스코드 게이트웨이 연결도 토큰도 갖지 않는다. 중앙봇이 메시지를
받아서 이 서버로 전달하고, 이 서버가 돌려준 응답을 중앙봇이 디스코드에
게시한다.

⚠️ 정확한 필드명은 `중앙봇_API_연동_가이드_업데이트.md` 에 정의돼 있고 이
저장소에는 그 문서가 없다. 그래서 입력 파싱은 **여러 별칭을 허용**하도록
느슨하게 두었고, 출력은 흔한 형태(content + embeds + files)를 따른다.
가이드와 대조해 확정할 때 고칠 파일은 여기 하나다.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict


class EventRequest(BaseModel):
    """중앙봇이 보내는 이벤트.

    별칭을 허용하는 이유는 위 주석 참조. 알 수 없는 필드는 그대로 통과시킨다.
    """

    model_config = ConfigDict(extra="allow")

    type: str = "message"
    content: str = ""
    user_id: str = ""
    channel_id: str = ""
    guild_id: str = ""
    message_id: str = ""
    username: str = ""

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "EventRequest":
        def pick(*keys: str, default: str = "") -> str:
            for key in keys:
                value = payload.get(key)
                if value not in (None, ""):
                    return str(value)
            # 중첩된 형태(예: {"author": {"id": ...}}) 도 한 단계 훑는다.
            for container in ("author", "user", "member", "data"):
                nested = payload.get(container)
                if isinstance(nested, dict):
                    for key in keys:
                        value = nested.get(key)
                        if value not in (None, ""):
                            return str(value)
            return default

        return cls(
            type=pick("type", "event_type", default="message"),
            content=pick("content", "message", "text"),
            user_id=pick("user_id", "userId", "author_id", "id"),
            channel_id=pick("channel_id", "channelId"),
            guild_id=pick("guild_id", "guildId"),
            message_id=pick("message_id", "messageId"),
            username=pick("username", "name", "display_name"),
        )


@dataclass
class Attachment:
    filename: str
    data: bytes

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "content_type": "image/png",
            # 중앙봇이 JSON 으로 받으므로 base64 로 싣는다.
            "data": base64.b64encode(self.data).decode("ascii"),
        }


@dataclass
class BotResponse:
    """중앙봇이 디스코드에 게시할 내용."""

    content: str = ""
    handled: bool = True
    ephemeral: bool = False
    attachments: list[Attachment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "handled": self.handled,
            "response": {
                "content": self.content,
                "ephemeral": self.ephemeral,
                "files": [a.to_dict() for a in self.attachments],
            },
        }

    @classmethod
    def ignored(cls) -> "BotResponse":
        """이 봇이 처리할 메시지가 아님. 중앙봇은 아무것도 게시하지 않는다."""
        return cls(handled=False)

    @classmethod
    def text(cls, message: str) -> "BotResponse":
        return cls(content=message)

    @classmethod
    def error(cls, message: str) -> "BotResponse":
        return cls(content=f"❌ {message}")

    def with_image(self, filename: str, data: bytes) -> "BotResponse":
        self.attachments.append(Attachment(filename, data))
        return self
