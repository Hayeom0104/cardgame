"""R3 M-03 — `terminal_thread_retention_hours` was configured but nothing
ever deleted a terminal run's private thread. `CentralClient.delete_thread()`
was never called anywhere in the service."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.db.connection import utcnow
from app.engine import lifecycle as lc


class FakeCentral:
    def __init__(self, outcome: str = "deleted"):
        self.outcome = outcome
        self.calls: list[dict] = []

    def delete_thread(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": self.outcome}


@pytest.fixture
def terminal_run(db, balance, version, user_id) -> int:
    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    db.execute(
        "UPDATE runs SET state = ?, thread_id = 555, ended_at = ?, "
        "end_reason = 'defeat' WHERE run_id = ?",
        (lc.RUN_DEFEATED, utcnow(), run_id))
    return run_id


def _age(db, run_id: int, hours: float) -> None:
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    db.execute("UPDATE runs SET ended_at = ? WHERE run_id = ?",
              (when.isoformat(timespec="seconds"), run_id))


def test_a_freshly_ended_run_is_not_due_yet(db, balance, terminal_run):
    assert lc.threads_due_for_cleanup(db, balance) == []


def test_a_run_past_the_retention_window_is_due(db, balance, terminal_run):
    hours = int(balance.get("terminal_thread_retention_hours"))
    _age(db, terminal_run, hours + 1)
    due = lc.threads_due_for_cleanup(db, balance)
    assert [row["run_id"] for row in due] == [terminal_run]


def test_an_active_run_is_never_due(db, balance, version, user_id):
    lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    assert lc.threads_due_for_cleanup(db, balance) == []


def test_cleanup_deletes_and_marks_it_so_it_is_not_retried(db, balance, terminal_run):
    hours = int(balance.get("terminal_thread_retention_hours"))
    _age(db, terminal_run, hours + 1)
    central = FakeCentral(outcome="deleted")

    report = lc.cleanup_terminal_threads(db, balance, central)

    assert report == {"checked": 1, "by_outcome": {"deleted": 1}}
    assert central.calls == [{
        "logical_session_id": db.one(
            "SELECT logical_session_id FROM runs WHERE run_id = ?",
            (terminal_run,))["logical_session_id"],
        "expected_thread_id": 555,
        "expected_surface_generation": 1,
        "reason": "run_ended",
    }]
    run = db.one("SELECT thread_deleted_at FROM runs WHERE run_id = ?", (terminal_run,))
    assert run["thread_deleted_at"] is not None

    # Second sweep: already cleaned up, not retried.
    report2 = lc.cleanup_terminal_threads(db, balance, central)
    assert report2 == {"checked": 0, "by_outcome": {}}
    assert len(central.calls) == 1


def test_already_missing_counts_as_cleaned_up(db, balance, terminal_run):
    """The guide's own outcome vocabulary — the thread can be long gone
    (someone manually deleted it) and that's still a successful cleanup."""
    hours = int(balance.get("terminal_thread_retention_hours"))
    _age(db, terminal_run, hours + 1)
    central = FakeCentral(outcome="already_missing")

    lc.cleanup_terminal_threads(db, balance, central)

    run = db.one("SELECT thread_deleted_at FROM runs WHERE run_id = ?", (terminal_run,))
    assert run["thread_deleted_at"] is not None


def test_a_stale_generation_is_left_for_the_next_sweep(db, balance, terminal_run):
    hours = int(balance.get("terminal_thread_retention_hours"))
    _age(db, terminal_run, hours + 1)
    central = FakeCentral(outcome="stale_generation")

    lc.cleanup_terminal_threads(db, balance, central)

    run = db.one("SELECT thread_deleted_at FROM runs WHERE run_id = ?", (terminal_run,))
    assert run["thread_deleted_at"] is None


def test_one_failing_thread_does_not_stop_the_rest(db, balance, version, user_id):
    from app.content.seed import create_account

    other_id = user_id + 1
    create_account(db, other_id, version)
    run_a = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    run_b = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=other_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    hours = int(balance.get("terminal_thread_retention_hours"))
    for run_id, thread_id in ((run_a, 1001), (run_b, 1002)):
        db.execute("UPDATE runs SET state = ?, thread_id = ?, ended_at = ? "
                  "WHERE run_id = ?", (lc.RUN_ABANDONED, thread_id, utcnow(), run_id))
        _age(db, run_id, hours + 1)

    class FlakyCentral:
        def __init__(self):
            self.calls = 0

        def delete_thread(self, **kwargs):
            self.calls += 1
            if kwargs["expected_thread_id"] == 1001:
                raise RuntimeError("central is down")
            return {"status": "deleted"}

    central = FlakyCentral()
    report = lc.cleanup_terminal_threads(db, balance, central)

    assert central.calls == 2
    assert report["by_outcome"]["deleted"] == 1
    assert report["by_outcome"]["retryable_failure"] == 1
    assert db.one("SELECT thread_deleted_at FROM runs WHERE run_id = ?",
                 (run_b,))["thread_deleted_at"] is not None
    assert db.one("SELECT thread_deleted_at FROM runs WHERE run_id = ?",
                 (run_a,))["thread_deleted_at"] is None
