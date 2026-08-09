"""§1.3.10 — the five validation gates every component submission runs.

    1. OWNERSHIP  — the submitting user owns this run
    2. LIVENESS   — the run/battle state permits this action (§16)
    3. REVISION   — the component's revision == current presentation_revision
    4. LEGALITY   — the chosen target/card is still valid right now
    5. CAS CLAIM  — the mutation succeeds only if it atomically wins the
                    revision increment (§16.8). Gates 1-4 are not atomic by
                    themselves; two concurrent clicks can both pass them.

Gates 1-4 live here. Gate 5 is `lifecycle.claim_mutation`, called by the
handler inside the same local transaction as its mutation.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.api import errors
from app.api.custom_id import CustomId
from app.db.connection import Database


class GateError(RuntimeError):
    """Carries the §19.4 player-facing string alongside the internal reason."""

    def __init__(self, message: str, *, reason: str = ""):
        super().__init__(reason or message)
        self.message = message
        self.reason = reason or message


@dataclass
class GateResult:
    run: dict
    custom_id: CustomId


def check_gates(db: Database, *, user_id: int, custom_id: CustomId,
                allowed_states: frozenset[str] | set[str] | None = None,
                legality=None) -> GateResult:
    """Run gates 1-4. Raises GateError with the §19.4 string on the first fail."""
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (custom_id.run_id,))
    if run is None:
        raise GateError(errors.ILLEGAL_STATE, reason="run does not exist")

    # 1. OWNERSHIP
    if int(run["user_id"]) != int(user_id):
        raise GateError(errors.NOT_OWNER, reason="submitting user does not own the run")

    # 2. LIVENESS
    if allowed_states is not None and run["state"] not in allowed_states:
        raise GateError(errors.ILLEGAL_STATE,
                        reason=f"state {run['state']!r} does not permit this action")

    # 3. REVISION — also covers surface generation, since a recreated surface
    # invalidates every component rendered against the old one.
    if int(custom_id.generation) != int(run["surface_generation"]):
        raise GateError(errors.STALE_REVISION,
                        reason=f"generation {custom_id.generation} != "
                               f"{run['surface_generation']}")
    if int(custom_id.revision) != int(run["presentation_revision"]):
        raise GateError(errors.STALE_REVISION,
                        reason=f"revision {custom_id.revision} != "
                               f"{run['presentation_revision']}")

    # 4. LEGALITY — the caller supplies the predicate, because what counts as
    # legal is screen-specific (a target that just died, a card no longer in
    # hand, a shop row already purchased).
    if legality is not None and not legality(dict(run)):
        raise GateError(errors.ILLEGAL_STATE,
                        reason="the chosen target or card is no longer valid")

    return GateResult(run=dict(run), custom_id=custom_id)
