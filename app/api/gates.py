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
from app.content.balance import Balance
from app.db.connection import Database


class GateError(RuntimeError):
    """Carries the §19.4 player-facing string alongside the internal reason."""

    def __init__(self, message: str, *, reason: str = ""):
        super().__init__(reason or message)
        self.message = message
        self.reason = reason or message


class RunExpiredError(GateError):
    """R3 M-02 — the run was settled as expired during this very gate check.

    A plain `GateError` only rejects the click. This one also carries the
    run_id so the caller can push the settled screen to the run's thread
    (§16.3) instead of leaving a dead component behind with no explanation.
    """

    def __init__(self, run_id: int):
        super().__init__(errors.RUN_EXPIRED, reason="run expired on component use")
        self.run_id = run_id


class StaleComponentError(GateError):
    """R3 B-02 — the player clicked a component from a revision that's no
    longer current (same surface generation, but the run has moved on since).

    The message they clicked is still the canonical one Central will edit,
    so unlike a stale generation (the surface itself was recreated), it's
    safe to answer with the run's live current screen rather than only
    "화면이 갱신되었습니다. 다시 시도해 주세요." and nothing to act on.
    """

    def __init__(self, run_id: int):
        super().__init__(errors.STALE_REVISION, reason="component revision is stale")
        self.run_id = run_id


@dataclass
class GateResult:
    run: dict
    custom_id: CustomId


def check_gates(db: Database, balance: Balance, *, user_id: int, custom_id: CustomId,
                allowed_states: frozenset[str] | set[str] | None = None,
                legality=None) -> GateResult:
    """Run gates 1-4. Raises GateError with the §19.4 string on the first fail."""
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (custom_id.run_id,))
    if run is None:
        raise GateError(errors.ILLEGAL_STATE, reason="run does not exist")

    # 1. OWNERSHIP
    if int(run["user_id"]) != int(user_id):
        raise GateError(errors.NOT_OWNER, reason="submitting user does not own the run")

    # 1.5 EXPIRY (R3 M-02) — §16.3's 30-minute rule was only enforced when
    # opening the hub or starting a run, never on a component click inside an
    # already-open thread. A player returning to a stale thread could revive
    # an abandoned run just by clicking, instead of it settling as expired.
    # Checked before LIVENESS: an expired run's stored `state` can still be
    # e.g. 'battle', which would otherwise pass that gate.
    from app.engine import lifecycle as lc

    if lc.is_expired(db, balance, run):
        lc.expire_run(db, balance, run)
        raise RunExpiredError(run["run_id"])

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
        raise StaleComponentError(run["run_id"])

    # 4. LEGALITY — the caller supplies the predicate, because what counts as
    # legal is screen-specific (a target that just died, a card no longer in
    # hand, a shop row already purchased).
    if legality is not None and not legality(dict(run)):
        raise GateError(errors.ILLEGAL_STATE,
                        reason="the chosen target or card is no longer valid")

    return GateResult(run=dict(run), custom_id=custom_id)
