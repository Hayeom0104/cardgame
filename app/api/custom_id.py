"""§19.2 — `custom_id` format and §1.3.10 component validation.

    dko:<action>:<run_id36>:<gen>:<rev>:<payload>

Namespace `dko:` — `mod:` is reserved for Central moderation (guide §8.8).
Well under Discord's 100-character limit. Select option `value` uses the same
shape.

Central performs **no** truncation, sorting, dedupe, normalization, coercion or
legality checking on `values[]` (guide §7.2), and validates none of the five
gates. All of it is this service's job.
"""

from __future__ import annotations

from dataclasses import dataclass

NAMESPACE = "dko"

# §19.2 action codes — 2-4 chars.
ACTION_CARD_SELECT = "cs"
ACTION_TARGET_SELECT = "ts"
ACTION_NODE_CHOOSE = "nd"
ACTION_REWARD_PICK = "rw"
ACTION_REWARD_RECIPIENT = "rc"
ACTION_SKIP = "sk"
ACTION_SHOP_BUY = "sh"
ACTION_SHOP_EXIT = "sx"
ACTION_EVENT_BRANCH = "ev"
ACTION_CLEANSE_PICK = "cz"

KNOWN_ACTIONS = frozenset({
    ACTION_CARD_SELECT, ACTION_TARGET_SELECT, ACTION_NODE_CHOOSE,
    ACTION_REWARD_PICK, ACTION_REWARD_RECIPIENT, ACTION_SKIP,
    ACTION_SHOP_BUY, ACTION_SHOP_EXIT, ACTION_EVENT_BRANCH, ACTION_CLEANSE_PICK,
})

_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


class CustomIdError(ValueError):
    pass


def to_base36(value: int) -> str:
    if value < 0:
        raise CustomIdError("run_id must be non-negative")
    if value == 0:
        return "0"
    out = ""
    while value:
        value, remainder = divmod(value, 36)
        out = _DIGITS[remainder] + out
    return out


def from_base36(text: str) -> int:
    return int(text, 36)


@dataclass(frozen=True)
class CustomId:
    action: str
    run_id: int
    generation: int
    revision: int
    payload: str = ""

    def encode(self) -> str:
        return ":".join((NAMESPACE, self.action, to_base36(self.run_id),
                         str(self.generation), str(self.revision), self.payload))


def build(action: str, run_id: int, generation: int, revision: int,
          payload: str = "") -> str:
    if action not in KNOWN_ACTIONS:
        raise CustomIdError(f"unknown action code {action!r}")
    if ":" in payload:
        raise CustomIdError("payload may not contain ':'")
    return CustomId(action, run_id, generation, revision, payload).encode()


def parse(custom_id: str) -> CustomId:
    parts = custom_id.split(":")
    if len(parts) < 5:
        raise CustomIdError(f"malformed custom_id {custom_id!r}")
    namespace, action, run36, generation, revision = parts[:5]
    payload = ":".join(parts[5:]) if len(parts) > 5 else ""
    if namespace != NAMESPACE:
        raise CustomIdError(f"custom_id namespace {namespace!r} is not {NAMESPACE!r}")
    if action not in KNOWN_ACTIONS:
        raise CustomIdError(f"unknown action code {action!r}")
    try:
        return CustomId(action, from_base36(run36), int(generation), int(revision),
                        payload)
    except ValueError as error:
        raise CustomIdError(f"malformed custom_id {custom_id!r}") from error
