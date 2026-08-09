"""§1.2 — the `/event` contract.

> v6.1 stated that all five types carry `guild_id`, `channel_id`, and
> `user_id`. **That is false.** A strict common model would reject valid
> callbacks.

Implementation rule: **parse per type, not against a shared required-field
model.** The user for a callback is recovered by joining `request_id` or
`logical_session_id` to local state, never read from the payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MESSAGE = "message"
INTERACTION = "interaction"
MODAL_SUBMIT = "modal_submit"
MESSAGE_DELIVERY_RESULT = "message_delivery_result"
THREAD_DELETED = "thread_deleted"

INBOUND_TYPES = frozenset({MESSAGE, INTERACTION, MODAL_SUBMIT,
                           MESSAGE_DELIVERY_RESULT, THREAD_DELETED})

#: Types that carry `user_id`. The other two do NOT.
USER_BEARING_TYPES = frozenset({MESSAGE, INTERACTION, MODAL_SUBMIT})


class EventParseError(ValueError):
    pass


@dataclass
class MessageEvent:
    user_id: int
    guild_id: int | None
    channel_id: int | None
    command: str
    args: list[str]
    raw_content: str
    message_id: int | None = None
    parent_channel_id: int | None = None
    thread_id: int | None = None
    event_id: str | None = None


@dataclass
class InteractionEvent:
    user_id: int
    guild_id: int | None
    channel_id: int | None
    custom_id: str
    values: list[str] = field(default_factory=list)
    interaction_token: str | None = None
    component_type: int | None = None
    message_id: int | None = None
    parent_channel_id: int | None = None
    thread_id: int | None = None
    event_id: str | None = None


@dataclass
class ModalSubmitEvent:
    user_id: int
    guild_id: int | None
    channel_id: int | None
    custom_id: str
    fields: dict[str, Any] = field(default_factory=dict)
    parent_channel_id: int | None = None
    thread_id: int | None = None
    event_id: str | None = None


@dataclass
class DeliveryResultEvent:
    """No `user_id`. Recovered by joining `request_id` to `delivery_intents`."""

    request_id: str
    action: str | None
    success: bool
    partial: bool
    guild_id: int | None = None
    channel_id: int | None = None
    message_id: int | None = None
    thread_id: int | None = None
    event_id: str | None = None


@dataclass
class ThreadDeletedEvent:
    """No `user_id`, no `channel_id`. Recovered by `logical_session_id`."""

    thread_id: int
    parent_channel_id: int | None
    logical_session_id: str
    surface_generation: int | None
    source: str | None = None
    event_id: str | None = None


def parse_event(payload: dict):
    """Dispatch on `type` and build the matching shape. No shared model."""
    event_type = payload.get("type")
    if event_type not in INBOUND_TYPES:
        raise EventParseError(f"unsupported inbound event type {event_type!r}")

    event_id = payload.get("event_id") or payload.get("id")

    if event_type == MESSAGE:
        raw = payload.get("raw_content", "")
        return MessageEvent(
            user_id=int(payload["user_id"]),
            guild_id=payload.get("guild_id"),
            channel_id=payload.get("channel_id"),
            command=payload.get("command", ""),
            args=list(payload.get("args") or []),
            raw_content=raw,
            message_id=payload.get("message_id"),
            parent_channel_id=payload.get("parent_channel_id"),
            thread_id=payload.get("thread_id"),
            event_id=event_id,
        )

    if event_type == INTERACTION:
        return InteractionEvent(
            user_id=int(payload["user_id"]),
            guild_id=payload.get("guild_id"),
            channel_id=payload.get("channel_id"),
            custom_id=payload["custom_id"],
            # Central performs NO truncation, sorting, dedupe, normalization,
            # coercion or legality checking on values[] (guide §7.2).
            values=list(payload.get("values") or []),
            interaction_token=payload.get("interaction_token"),
            component_type=payload.get("component_type"),
            message_id=payload.get("message_id"),
            parent_channel_id=payload.get("parent_channel_id"),
            thread_id=payload.get("thread_id"),
            event_id=event_id,
        )

    if event_type == MODAL_SUBMIT:
        return ModalSubmitEvent(
            user_id=int(payload["user_id"]),
            guild_id=payload.get("guild_id"),
            channel_id=payload.get("channel_id"),
            custom_id=payload["custom_id"],
            fields=dict(payload.get("fields") or {}),
            parent_channel_id=payload.get("parent_channel_id"),
            thread_id=payload.get("thread_id"),
            event_id=event_id,
        )

    if event_type == MESSAGE_DELIVERY_RESULT:
        if "user_id" in payload:
            # Not fatal, but worth catching in development: a shared model
            # would have invented this field.
            raise EventParseError(
                "message_delivery_result must not carry user_id (§1.2)"
            )
        return DeliveryResultEvent(
            request_id=payload["request_id"],
            action=payload.get("action"),
            success=bool(payload.get("success")),
            partial=bool(payload.get("partial")),
            guild_id=payload.get("guild_id"),
            channel_id=payload.get("channel_id"),
            message_id=payload.get("message_id"),
            thread_id=payload.get("thread_id"),
            event_id=event_id,
        )

    return ThreadDeletedEvent(
        thread_id=int(payload["thread_id"]),
        parent_channel_id=payload.get("parent_channel_id"),
        logical_session_id=payload["logical_session_id"],
        surface_generation=payload.get("surface_generation"),
        source=payload.get("source"),
        event_id=event_id,
    )
