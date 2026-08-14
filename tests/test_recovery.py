"""§16.8 기동 복구 — 계획을 세우는 것과 실행하는 것은 다르다.

`recover_runs` 는 비종료 런을 훑어 무엇이 필요한지 분류해 돌려준다. 그런데
그 계획 중 실제로 실행되는 것은 만료 정산 하나뿐이었고, 나머지는 `state` 에
담겨 아무도 읽지 않았다. §16.3 만료가 같은 이유로 발동하지 않던 것과 똑같은
모양이다.

나머지 항목이 모두 문제인 것은 아니다. 전투나 상점 화면을 다시 그리는 일은
런이 그대로 살아 있어 플레이어가 이어서 누를 수 있다. 여기서 확인하는 것은
**플레이어가 스스로 풀 수 없는 둘** 이다.
"""

from __future__ import annotations

import pytest

from app.api import server
from app.central import surfaces
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.engine import lifecycle as lc
from app.engine import settlement as sl

PARENT_CHANNEL = 3131


class FakeCentral:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self._next = 600000
        self.create_calls = 0
        self.balance = 0

    def create_thread(self, **kwargs):
        self.create_calls += 1
        if self.fail:
            raise RuntimeError("중앙봇이 응답하지 않습니다")
        self._next += 1
        return {"thread_id": self._next, "message_id": 3}

    def edit_message(self, **kwargs):
        return {"status": "edited"}

    def currency_add(self, user_id, amount, idempotency_key):
        from app.central.client import CurrencyResult

        self.balance += amount
        return CurrencyResult(requested=amount, applied=amount)


@pytest.fixture
def central() -> FakeCentral:
    return FakeCentral()


@pytest.fixture(autouse=True)
def channel(monkeypatch):
    """부모 채널이 없으면 스레드를 만들 수 없다 — 기동 시 이것이 없으면
    서비스가 아예 뜨지 않는다 (§1.3.5)."""
    from app.config import settings

    monkeypatch.setattr(settings, "parent_channel_id", PARENT_CHANNEL)


def make_run(db, balance, version, user_id) -> int:
    return lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version,
    )


# =====================================================================
# 정산 도중에 죽은 런
# =====================================================================
def test_a_run_stuck_in_settlement_blocks_the_account(db, balance, version,
                                                      user_id):
    """이것이 문제인 이유 — `run_settlement` 은 활성 상태이고, 그 화면에는
    누를 것이 없다."""
    run_id = make_run(db, balance, version, user_id)
    sl.enter_settlement(db, run_id, target_state=sl.RUN_ABANDONED,
                        end_reason="테스트")

    assert lc.active_run_for(db, user_id) is not None
    assert lc.RUN_SETTLEMENT in lc.ACTIVE_STATES


def test_startup_finishes_a_settlement_that_was_interrupted(db, balance, version,
                                                            user_id, central):
    run_id = make_run(db, balance, version, user_id)
    sl.enter_settlement(db, run_id, target_state=sl.RUN_ABANDONED,
                        end_reason="정산 중 크래시")

    plan = lc.recover_runs(db, balance)
    assert any(entry["action"] == "resume_settlement" for entry in plan), \
        "복구 계획이 이 런을 정산 재개로 분류하지 않았습니다"

    resumed = server._resume_settlements(db, balance, plan, central)

    assert resumed == 1
    assert lc.active_run_for(db, user_id) is None, "계정이 여전히 묶여 있습니다"
    run = db.one("SELECT state, ended_at FROM runs WHERE run_id = ?", (run_id,))
    assert run["state"] == sl.RUN_ABANDONED
    assert run["ended_at"] is not None


def test_resuming_a_settlement_twice_changes_nothing(db, balance, version,
                                                     user_id, central):
    """정산 단계는 receipt 와 저널 키로 멱등하다 (§16.2.1). 기동이 두 번
    일어나도 보상이 두 번 나가면 안 된다."""
    run_id = make_run(db, balance, version, user_id)
    sl.enter_settlement(db, run_id, target_state=sl.RUN_ABANDONED,
                        end_reason="정산 중 크래시")

    plan = lc.recover_runs(db, balance)
    server._resume_settlements(db, balance, plan, central)
    carta = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                   (user_id,))["carta"]

    # 두 번째 기동. 이미 끝난 런은 계획에 오르지 않는다.
    again = lc.recover_runs(db, balance)
    assert not any(entry["action"] == "resume_settlement" for entry in again)
    assert server._resume_settlements(db, balance, again, central) == 0
    assert db.one("SELECT carta FROM accounts WHERE user_id = ?",
                  (user_id,))["carta"] == carta


def test_a_failure_does_not_stop_the_service_coming_up(db, balance, version,
                                                       user_id, central):
    """복구에 걸려 서비스가 아예 뜨지 못하면, 막힌 계정을 풀 방법마저 사라진다."""
    run_id = make_run(db, balance, version, user_id)
    sl.enter_settlement(db, run_id, target_state=sl.RUN_ABANDONED,
                        end_reason="테스트")
    # 계획에는 있지만 실제로는 사라진 런.
    plan = [{"run_id": 9999, "action": "resume_settlement"},
            {"run_id": run_id, "action": "resume_settlement"}]

    assert server._resume_settlements(db, balance, plan, central) == 1


# =====================================================================
# 화면을 얻지 못한 채 남은 런
# =====================================================================
def test_startup_opens_the_thread_a_preparing_run_never_got(db, balance, version,
                                                            user_id, central):
    run_id = make_run(db, balance, version, user_id)
    assert db.one("SELECT thread_id FROM runs WHERE run_id = ?",
                  (run_id,))["thread_id"] is None

    plan = lc.recover_runs(db, balance)
    assert any(entry["action"] == "retry_surface" for entry in plan)

    retried = server._retry_surfaces(db, plan, central)

    assert retried == 1
    run = db.one("SELECT thread_id, state FROM runs WHERE run_id = ?", (run_id,))
    assert run["thread_id"] is not None
    # 화면이 생겼으므로 런은 지도로 넘어간다 (§16.2.2 [6]).
    assert run["state"] == lc.MAP_NAVIGATION


def test_the_retry_keeps_the_same_surface_generation(db, balance, version,
                                                     user_id, central):
    """§16.8 — 세대를 올리는 것은 스레드가 실제로 사라졌을 때다. 여기는 애초에
    만들어지지 않은 경우이고, 같은 세대의 생성은 멱등하다 (§1.3.5)."""
    run_id = make_run(db, balance, version, user_id)
    before = db.one("SELECT surface_generation FROM runs WHERE run_id = ?",
                    (run_id,))["surface_generation"]

    surfaces.retry_surface(db, central, run_id, parent_channel_id=PARENT_CHANNEL)

    after = db.one("SELECT surface_generation FROM runs WHERE run_id = ?",
                   (run_id,))["surface_generation"]
    assert after == before


def test_a_run_that_already_has_a_thread_is_left_alone(db, balance, version,
                                                       user_id, central):
    run_id = make_run(db, balance, version, user_id)
    db.execute("UPDATE runs SET thread_id = 55, state = ? WHERE run_id = ?",
               (lc.MAP_NAVIGATION, run_id))

    assert surfaces.retry_surface(db, central, run_id,
                                  parent_channel_id=PARENT_CHANNEL) is None
    assert central.create_calls == 0


def test_a_failed_retry_leaves_the_run_ready_to_try_again(db, balance, version,
                                                          user_id):
    run_id = make_run(db, balance, version, user_id)
    broken = FakeCentral(fail=True)

    plan = lc.recover_runs(db, balance)
    assert server._retry_surfaces(db, plan, broken) == 0

    run = db.one("SELECT state, thread_id FROM runs WHERE run_id = ?", (run_id,))
    # 준비 상태로 남아야 다음 기동이 다시 시도한다.
    assert run["state"] == lc.PREPARING
    assert run["thread_id"] is None
