"""§1.3 — the Central Bot compliance contract.

This service holds no Discord gateway connection and no bot token. All Discord
delivery is proxied through the Central Bot.

Two rules dominate this module:

* **Capability discovery fails closed** (§1.3.4). The ten flags below are copied
  verbatim from the Central Bot guide §11.1 — v6.2 invented four field names
  that do not exist in the contract, so a literal implementation would have
  refused to start against a fully compliant Central Bot.
* **Applied-delta validation** (§1.3.8). An over-large deduction returns HTTP
  200 with the clamped delta, so the requested and applied amounts must be
  compared on every currency call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: §1.3.4 — every one of these is checked exactly. Deckout's core loop depends
#: on all ten, so a missing or false value is a HARD STARTUP FAILURE.
REQUIRED_CAPABILITIES: dict[str, Any] = {
    "component_contract_version": "2.0",
    "message_delivery_contract_version": "1.0",
    "supports_string_select_forwarding": True,      # §2.5.2 target select
    "supports_string_select_rendering": True,
    "supports_private_thread_creation": True,       # §16 run surface
    "supports_thread_deletion": True,               # §16.8 terminal policy
    "supports_thread_recreation": True,             # §16.8 thread_deleted recovery
    "supports_external_thread_deletion_events": True,   # §1.3.5
    "supports_out_of_band_message_edit": True,      # §1.3.6
    "supports_message_edit_revision_guard": True,   # §16.6 ordering
}

#: §1.2 outbound actions used by this bot. `update` is NOT an alias of `edit`;
#: using it is a controlled failure.
OUTBOUND_ACTIONS = frozenset({
    "reply", "reply_ephemeral", "edit", "multi_action",
    "upsert_user_safezone_message", "post_channel_message", "modal",
    "redirect", "ignore",
})

#: §1.3.7 PNG attachment limits — a protocol rule, never discovered at runtime.
MAX_ATTACHMENTS_PER_ACTION = 2
MAX_PNG_BYTES = 4 * 1024 * 1024
MAX_PNG_DIMENSION = 4096
MAX_FILENAME_LENGTH = 128

#: Component Contract 2.0 — outer rows are numeric type 1, buttons are 2,
#: string selects are 3. Handlers build human-readable flat dicts
#: (`{"type": "button"}`); `to_action_rows` is the one place that turns those
#: into what Central actually accepts.
ACTION_ROW_TYPE = 1
BUTTON_TYPE = 2
STRING_SELECT_TYPE = 3
_COMPONENT_TYPE_NUMBERS = {"button": BUTTON_TYPE, "string_select": STRING_SELECT_TYPE}
#: 🟡 Discord's own button schema requires a style (1-5); Central's guide isn't
#: in this repo to confirm it forwards that requirement verbatim, but sending
#: a default is strictly safer than omitting a field a real button needs.
DEFAULT_BUTTON_STYLE = 1


class CapabilityError(RuntimeError):
    """Raised when the Central Bot does not satisfy §1.3.4. Fails startup."""


class CentralError(RuntimeError):
    pass


@dataclass
class CurrencyResult:
    """§1.3.8 — the applied delta is authoritative, the request is not."""

    requested: int
    applied: int
    balance_after: int | None = None

    @property
    def matched(self) -> bool:
        return self.requested == self.applied


#: §1.3.8 `/v1/currency/add` response keys, in preference order. The deployed
#: Central `ChangeResponse` returns `amount` (the applied delta) and
#: `value_after` (the balance after) — confirmed against a real production
#: +500 daily-attendance grant that this client used to misread as `applied=0`
#: because it only looked for `applied_delta`/`applied`/`balance`, which
#: silently routed a successfully-applied grant into `OPERATOR_REQUIRED`.
#: The old names are kept as a fallback only; never reorder them ahead of the
#: confirmed keys.
APPLIED_DELTA_KEYS = ("amount", "applied_delta", "applied")
BALANCE_AFTER_KEYS = ("value_after", "balance", "balance_after")


def _applied_delta(payload: dict) -> int:
    for key in APPLIED_DELTA_KEYS:
        if key in payload and payload[key] is not None:
            return int(payload[key])
    return 0


def _balance_after(payload: dict) -> int | None:
    for key in BALANCE_AFTER_KEYS:
        if key in payload and payload[key] is not None:
            return int(payload[key])
    return None


class CentralClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 5.0,
                 client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    def _post(self, path: str, payload: dict) -> dict:
        response = self._client.post(f"{self.base_url}{path}", json=payload,
                                     headers=self._headers())
        response.raise_for_status()
        return response.json()

    def _get(self, path: str) -> dict:
        response = self._client.get(f"{self.base_url}{path}", headers=self._headers())
        response.raise_for_status()
        return response.json()

    # =================================================================
    # §1.3.4 capability discovery
    # =================================================================
    def check_capabilities(self) -> dict:
        """Verify all ten flags. Refuses commands rather than running under an
        assumed contract."""
        payload = self._get("/v1/minigames/capabilities")
        return verify_capabilities(payload)

    # =================================================================
    # §1.3.8 economy
    # =================================================================
    def get_user(self, user_id: int) -> dict:
        """Balance reads are UX only — never the authoritative check, because
        another minigame may deduct in the interval (§17.3)."""
        return self._get(f"/v1/users/{user_id}")

    def currency_add(self, user_id: int, amount: int,
                     idempotency_key: str) -> CurrencyResult:
        payload = self._post("/v1/currency/add", {
            "user_id": user_id,
            "amount": amount,
            "idempotency_key": idempotency_key,
        })
        return CurrencyResult(
            requested=amount,
            applied=_applied_delta(payload),
            balance_after=_balance_after(payload),
        )

    def currency_deduct(self, user_id: int, amount: int,
                        idempotency_key: str) -> CurrencyResult:
        """A deduction larger than the balance clamps to 0 and returns HTTP 200
        with the real applied delta — hence §17.3 rule 3."""
        payload = self._post("/v1/currency/add", {
            "user_id": user_id,
            "amount": -abs(amount),
            "idempotency_key": idempotency_key,
        })
        return CurrencyResult(requested=-abs(amount), applied=_applied_delta(payload),
                              balance_after=_balance_after(payload))

    # =================================================================
    # §1.3.5 private thread lifecycle
    # =================================================================
    def create_thread(self, *, logical_session_id: str, surface_generation: int,
                      parent_channel_id: int, owner_user_id: int,
                      thread_name: str, content: str = "",
                      embeds: list | None = None,
                      components: list | None = None) -> dict:
        """Service-initiated private thread API (guide §11.3).

        NOT the response-path `create_thread` action, which creates a
        GUILD_PUBLIC_THREAD — anyone with View Channel on the parent could
        watch, and it has no member-add field.
        """
        return self._post("/v1/minigames/threads/create", {
            "logical_session_id": logical_session_id,
            "surface_generation": surface_generation,
            "parent_channel_id": parent_channel_id,
            "owner_user_id": owner_user_id,
            "thread_name": thread_name,
            "content": content,
            "embeds": embeds or [],
            "components": to_action_rows(components),
        })

    def delete_thread(self, *, logical_session_id: str, expected_thread_id: int,
                      expected_surface_generation: int, reason: str) -> dict:
        response = self._client.request(
            "DELETE", f"{self.base_url}/v1/minigames/threads",
            json={
                "logical_session_id": logical_session_id,
                "expected_thread_id": expected_thread_id,
                "expected_surface_generation": expected_surface_generation,
                "reason": reason,
            },
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    def recreate_thread(self, *, logical_session_id: str, surface_generation: int,
                        parent_channel_id: int, owner_user_id: int,
                        thread_name: str) -> dict:
        """`surface_generation` increments ONLY on recreate, never reused or
        decremented; a same-generation create is idempotent."""
        return self._post("/v1/minigames/threads/recreate", {
            "logical_session_id": logical_session_id,
            "surface_generation": surface_generation,
            "parent_channel_id": parent_channel_id,
            "owner_user_id": owner_user_id,
            "thread_name": thread_name,
        })

    # =================================================================
    # §1.3.6 durable out-of-band edit
    # =================================================================
    def edit_message(self, *, delivery_request_id: str, logical_session_id: str,
                     expected_thread_id: int, expected_message_id: int,
                     expected_surface_generation: int,
                     expected_presentation_revision: int,
                     new_presentation_revision: int,
                     content: str | None = None, embeds: list | None = None,
                     components: list | None = None,
                     attachment_policy: str = "preserve") -> dict:
        """`new_presentation_revision` must be EXACTLY `expected + 1`.

        Success → `edited`. The same `delivery_request_id` retried returns
        `already_applied`; do NOT issue a second edit.
        """
        if new_presentation_revision != expected_presentation_revision + 1:
            raise CentralError(
                "new_presentation_revision must be exactly expected + 1; "
                f"got {expected_presentation_revision} → {new_presentation_revision}"
            )
        return self._post("/v1/minigames/messages/edit", {
            "delivery_request_id": delivery_request_id,
            "logical_session_id": logical_session_id,
            "expected_thread_id": expected_thread_id,
            "expected_message_id": expected_message_id,
            "expected_surface_generation": expected_surface_generation,
            "expected_presentation_revision": expected_presentation_revision,
            "new_presentation_revision": new_presentation_revision,
            "content": content,
            "embeds": embeds or [],
            "components": to_action_rows(components),
            "attachment_policy": attachment_policy,
        })


def verify_capabilities(payload: dict) -> dict:
    """Guide §11.1: *flag가 없거나 false면 해당 기능을 시작하지 않습니다.*

    Deckout depends on all ten, so a missing or false value is a hard startup
    failure rather than a degraded mode.
    """
    missing: list[str] = []
    mismatched: list[str] = []
    for field_name, expected in REQUIRED_CAPABILITIES.items():
        if field_name not in payload:
            missing.append(field_name)
        elif payload[field_name] != expected:
            mismatched.append(f"{field_name}={payload[field_name]!r} (want {expected!r})")

    if missing or mismatched:
        raise CapabilityError(
            "Central Bot capability check failed — refusing to start. "
            f"missing={missing} mismatched={mismatched}"
        )
    return payload


def validate_png_attachment(*, data_b64: str, filename: str, width: int,
                            height: int, decoded_size: int) -> None:
    """§1.3.7 — enforced by Deckout when composing responses.

    There is no attachment-count capability field; these are protocol rules
    (guide §8.3), never discovered at runtime.
    """
    if data_b64.startswith("data:"):
        raise CentralError("data_b64 must be standard base64 without a data: URI prefix")
    if decoded_size > MAX_PNG_BYTES:
        raise CentralError(f"decoded PNG exceeds {MAX_PNG_BYTES} bytes")
    if not (1 <= width <= MAX_PNG_DIMENSION and 1 <= height <= MAX_PNG_DIMENSION):
        raise CentralError(f"PNG dimensions must be 1-{MAX_PNG_DIMENSION} on each axis")
    if "/" in filename or "\\" in filename:
        raise CentralError("filename must be a basename with no path component")
    if not filename.endswith(".png") or len(filename) > MAX_FILENAME_LENGTH:
        raise CentralError(f"filename must be a .png basename of at most "
                           f"{MAX_FILENAME_LENGTH} characters")


def to_action_rows(components: list[dict] | None) -> list[dict]:
    """Component Contract 2.0 — wrap a flat component list into action rows.

    Deckout's handlers build a flat list of human-readable dicts
    (`{"type": "button", ...}`, `{"type": "string_select", ...}`). Central
    requires an explicit outer row (`{"type": 1, "components": [...]}`) and
    numeric component types inside it — a flat, string-typed list is a
    contract violation Central rejects outright, which is what turned every
    interactive reply (including the very first prep screen) into the
    generic "처리 중 문제가 발생했습니다" error.

    A select menu takes its row alone; up to five buttons share a row.
    Idempotent — a list that is already wrapped in rows (type 1 outer dicts)
    passes through unchanged, so this is safe to apply defensively at more
    than one boundary.
    """
    if not components:
        return []
    if all(isinstance(item, dict) and item.get("type") == ACTION_ROW_TYPE
           for item in components):
        return components

    rows: list[dict] = []
    current_buttons: list[dict] = []

    def flush_buttons() -> None:
        if current_buttons:
            rows.append({"type": ACTION_ROW_TYPE, "components": list(current_buttons)})
            current_buttons.clear()

    for item in components:
        kind = item.get("type")
        numeric = _COMPONENT_TYPE_NUMBERS.get(kind)
        if numeric is None:
            raise CentralError(f"unknown component type {kind!r}")
        body = {key: value for key, value in item.items() if key != "type"}
        body["type"] = numeric
        if numeric == BUTTON_TYPE:
            body.setdefault("style", DEFAULT_BUTTON_STYLE)
            current_buttons.append(body)
            if len(current_buttons) == 5:
                flush_buttons()
        else:
            flush_buttons()
            rows.append({"type": ACTION_ROW_TYPE, "components": [body]})
    flush_buttons()
    return rows


def validate_multi_action(children: list[dict]) -> None:
    """§1.3.7 no-thinking `multi_action` shape.

    The FIRST child must be `edit`, updating the message holding the clicked
    component; later children may only be `post_channel_message` or
    `upsert_user_safezone_message`. Each child needs its own distinct
    `metadata.request_id` — the parent's is batch-level and must never map
    panel IDs.
    """
    if not children:
        raise CentralError("multi_action requires at least one child action")
    if children[0].get("action") != "edit":
        raise CentralError("the first multi_action child must be `edit`")
    allowed_tail = {"post_channel_message", "upsert_user_safezone_message"}
    for child in children[1:]:
        if child.get("action") not in allowed_tail:
            raise CentralError(
                f"multi_action child {child.get('action')!r} is not allowed after the "
                "first `edit`"
            )
    request_ids = [child.get("metadata", {}).get("request_id") for child in children]
    if any(request_id is None for request_id in request_ids):
        raise CentralError("every multi_action child needs its own metadata.request_id")
    if len(set(request_ids)) != len(request_ids):
        raise CentralError("multi_action child request_ids must be distinct")
