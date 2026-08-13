"""Central Bot HTTP minigame contract.

This module deliberately mirrors ``중앙봇_API_연동_가이드_업데이트 (3).md``.
Do not add a second, private response envelope here: Central consumes the
returned action directly.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict


class EventRequest(BaseModel):
    """The permissive common view of Central's five inbound event shapes."""

    model_config = ConfigDict(extra="allow")

    type: str = "message"
    content: str = ""
    command: str = ""
    args: list[str] = []
    raw_content: str = ""
    user_id: str = ""
    channel_id: str = ""
    parent_channel_id: str = ""
    thread_id: str = ""
    guild_id: str = ""
    message_id: str = ""
    username: str = ""
    custom_id: str = ""
    component_type: str = "button"
    values: list[str] = []
    fields: dict[str, str] = {}
    request_id: str = ""
    success: bool | None = None
    partial: bool | None = None

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "EventRequest":
        def pick(*keys: str, default: str = "") -> str:
            for key in keys:
                value = payload.get(key)
                if value not in (None, ""):
                    return str(value)
            return default

        raw_content = pick("raw_content", "content", "message", "text")
        raw_args = payload.get("args", [])
        return cls(
            type=pick("type", "event_type", default="message"),
            content=raw_content,
            command=pick("command"),
            args=[str(v) for v in raw_args] if isinstance(raw_args, list) else [],
            raw_content=raw_content,
            user_id=pick("user_id", "userId", "author_id"),
            channel_id=pick("channel_id", "channelId"),
            parent_channel_id=pick("parent_channel_id", "parentChannelId"),
            thread_id=pick("thread_id", "threadId"),
            guild_id=pick("guild_id", "guildId"),
            message_id=pick("message_id", "messageId"),
            username=pick("username", "name", "display_name"),
            custom_id=pick("custom_id", "customId"),
            component_type=pick("component_type", default="button"),
            values=[str(v) for v in payload.get("values", []) if isinstance(v, str)],
            fields={str(k): str(v) for k, v in payload.get("fields", {}).items()}
            if isinstance(payload.get("fields"), dict) else {},
            request_id=pick("request_id"),
            success=payload.get("success") if isinstance(payload.get("success"), bool) else None,
            partial=payload.get("partial") if isinstance(payload.get("partial"), bool) else None,
        )


@dataclass
class Attachment:
    filename: str
    data: bytes

    def to_dict(self) -> dict[str, str]:
        return {
            "filename": self.filename,
            "content_type": "image/png",
            "data_b64": base64.b64encode(self.data).decode("ascii"),
        }


@dataclass
class BotResponse:
    content: str = ""
    action: str = "reply"
    ephemeral: bool = False
    attachments: list[Attachment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        if self.action == "ignore":
            return {"action": "ignore"}
        action = "reply_ephemeral" if self.ephemeral else self.action
        return {
            "action": action,
            "content": self.content,
            "embeds": [],
            "components": [],
            "attachments": [a.to_dict() for a in self.attachments],
            "attachment_mode": "replace",
        }

    @classmethod
    def ignored(cls) -> "BotResponse":
        return cls(action="ignore")

    @classmethod
    def text(cls, message: str) -> "BotResponse":
        return cls(content=message)

    @classmethod
    def error(cls, message: str, *, ephemeral: bool = False) -> "BotResponse":
        return cls(content=f"❌ {message}", ephemeral=ephemeral)

    def with_image(self, filename: str, data: bytes) -> "BotResponse":
        if len(self.attachments) >= 2:
            raise ValueError("Central Bot allows at most two PNG attachments per action.")
        self.attachments.append(Attachment(filename, data))
        return self
