"""§16.3 방치 만료 — 규칙이 실제로 발동하는가.

`is_expired()` 는 처음부터 있었지만 `recover_runs()` 안에서만 불렸고, 그 결과는
계획으로 반환되어 아무도 실행하지 않았다. 그동안 방치된 런 하나가 §16.3의
계정당 하나 규칙으로 그 계정의 새 런을 **영구히** 막았다.

여기서 확인하는 것은 "만료를 판정할 수 있는가"가 아니라 **"만료가 일어나는가"**다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api import errors, handlers, screens
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID, WORLD_1_ID
from app.db.connection import utcnow
from app.engine import lifecycle as lc
from app.engine import progression as pg


@pytest.fixture
def ctx(db, balance, version):
    return handlers.HandlerContext(db=db, balance=balance, central=None,
                                   content_version_id=version)


@pytest.fixture
def run_id(db, balance, version, user_id) -> int:
    return lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)


def _idle(db, run_id: int, minutes: int) -> None:
    """마지막 조작을 과거로 밀어 방치 상태를 만든다."""
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    db.execute("UPDATE runs SET last_activity_at = ? WHERE run_id = ?",
               (when.isoformat(timespec="seconds"), run_id))


# =====================================================================
# 판정
# =====================================================================
def test_a_fresh_run_is_not_expired(db, balance, run_id):
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert not lc.is_expired(db, balance, run)


def test_the_threshold_comes_from_the_config(db, balance, version, run_id):
    """`inactivity_expiry_minutes` 를 바꾸면 판정이 따라 바뀐다."""
    minutes = int(balance.get("inactivity_expiry_minutes"))
    _idle(db, run_id, minutes + 1)
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert lc.is_expired(db, balance, run)

    db.execute("UPDATE balancing_constants SET value_json = '600' "
               "WHERE content_version_id = ? AND key = 'inactivity_expiry_minutes'",
               (version,))
    from app.content.balance import Balance
    assert not lc.is_expired(db, Balance(db, version), run)


def test_activity_pushes_the_deadline_back(db, balance, run_id):
    """조작이 있으면 만료 시계가 다시 돌아야 한다 — §16.7 CAS가 갱신한다."""
    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)
    revision = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                      (run_id,))["presentation_revision"]
    lc.claim_mutation(db, run_id, revision)

    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert not lc.is_expired(db, balance, run)


# =====================================================================
# 실제로 정산되는가
# =====================================================================
def test_an_idle_run_is_settled_not_merely_flagged(db, balance, run_id, user_id):
    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)

    report = lc.expire_if_stale(db, balance, user_id)

    assert report is not None
    state = db.one("SELECT state, end_reason FROM runs WHERE run_id = ?",
                   (run_id,))["state"]
    assert state == lc.RUN_EXPIRED
    assert lc.active_run_for(db, user_id) is None


def test_a_live_run_is_left_alone(db, balance, run_id, user_id):
    assert lc.expire_if_stale(db, balance, user_id) is None
    assert lc.active_run_for(db, user_id) is not None


def test_expiring_twice_is_harmless(db, balance, run_id, user_id):
    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)
    lc.expire_if_stale(db, balance, user_id)
    assert lc.expire_if_stale(db, balance, user_id) is None


def test_expiry_applies_the_retention_bands(db, balance, run_id, user_id):
    """§15.10 — 방치가 포기보다 이득이 되어서는 안 된다.

    얕은 깊이에서 방치하면 보존율 0% 구간에 걸린다. 방치로 장비를 지킬 수
    있다면 판을 열어두고 잠수하는 것이 최적 전략이 된다.
    """
    db.execute("INSERT INTO run_inventory (run_id, kind, equipment_def_id, "
               "tier, acquired_at_depth, acquired_at) "
               "VALUES (?, 'equipment', 'eq_수련검', 2, 1, ?)", (run_id, utcnow()))
    db.execute("UPDATE runs SET deepest_depth_reached = 1 WHERE run_id = ?",
               (run_id,))
    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)

    report = lc.expire_if_stale(db, balance, user_id)
    inventory = report["inventory"]
    assert inventory["kept"] == []
    assert len(inventory["lost"]) == 1
    assert db.one("SELECT COUNT(*) AS n FROM owned_equipment WHERE user_id = ?",
                  (user_id,))["n"] == 0


# =====================================================================
# 계정이 막히지 않는가 — 이게 이 규칙의 존재 이유다
# =====================================================================
def test_an_idle_run_no_longer_blocks_a_new_one(ctx, db, balance, run_id,
                                                user_id, version):
    """§16.3 — 방치된 런이 계정을 영구히 막고 있었다."""
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_aquel', 3, ?)", (user_id, utcnow()))
    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)

    screen = handlers.start_run(ctx, user_id)
    assert screen["content"] != errors.RUN_ALREADY_ACTIVE
    assert "월드 선택" in screen["content"]


def test_a_live_run_still_blocks_a_new_one(ctx, db, run_id, user_id):
    """만료 정리가 지나쳐 진행 중인 런까지 걷어내면 안 된다."""
    assert handlers.start_run(ctx, user_id)["content"] == errors.RUN_ALREADY_ACTIVE


def test_the_hub_reports_that_it_cleaned_up(ctx, db, balance, run_id, user_id):
    """조용히 지우면 플레이어는 자기 런이 어디 갔는지 알 수 없다."""
    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)
    screen = handlers.hub_screen(ctx, user_id)
    assert "정리했습니다" in screen["content"]


def test_the_preparing_screen_also_clears_an_idle_run(db, balance, version,
                                                      run_id, user_id):
    """준비 화면도 §16.3으로 거절하므로 같은 정리가 필요하다."""
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_aquel', 3, ?)", (user_id, utcnow()))
    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)

    result = screens.handle_prep(db, balance, user_id,
                                 f"{screens.PREP_PREFIX}world", [WORLD_1_ID],
                                 version)
    assert result["content"] != errors.RUN_ALREADY_ACTIVE


# =====================================================================
# 기동 시 정리
# =====================================================================
def test_the_startup_scan_settles_expired_runs(db, balance, run_id, user_id):
    """계획만 세우고 끝내면 §16.3은 없는 규칙이 된다."""
    from app.api.server import _settle_expired

    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)
    plan = lc.recover_runs(db, balance)
    assert {"run_id": run_id, "action": "settle_expired"} in plan

    assert _settle_expired(db, balance, plan) == 1
    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.RUN_EXPIRED


def test_a_stale_in_thread_component_click_settles_the_run_as_expired(
        ctx, db, balance, run_id, user_id):
    """R3 M-02 / §9 Mandatory Test D — `check_gates()` never checked
    `is_expired()`, so a component click inside an old thread revived an
    already-abandoned run via `claim_mutation()`'s `last_activity_at` bump
    instead of settling it. This clicks a real map button after the run has
    gone idle past the configured window."""
    from app.api import controls
    from app.api import events as ev
    from app.api import handlers as h

    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)

    buttons = controls.game_map(db, run_id)
    assert buttons, "지도에 누를 것이 없습니다"
    stale_button = buttons[0]

    reply = h.handle_interaction(ctx, ev.InteractionEvent(
        event_id="stale-click", user_id=user_id, guild_id=1, channel_id=2,
        custom_id=stale_button["custom_id"], values=[]))

    assert reply["action"] == "reply_ephemeral"
    assert reply["content"] == errors.RUN_EXPIRED

    run = db.one("SELECT state FROM runs WHERE run_id = ?", (run_id,))
    assert run["state"] == lc.RUN_EXPIRED
    assert lc.active_run_for(db, user_id) is None


def test_one_failing_run_does_not_stop_the_others(db, balance, run_id, user_id,
                                                   monkeypatch):
    """만료 정산에 걸려 서비스가 못 뜨면 막힌 계정을 풀 방법도 사라진다."""
    from app.api import server

    _idle(db, run_id, int(balance.get("inactivity_expiry_minutes")) + 1)
    plan = [{"run_id": run_id, "action": "settle_expired"},
            {"run_id": 999999, "action": "settle_expired"}]

    calls = {"n": 0}
    real = lc.expire_run

    def flaky(db_, balance_, run):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("정산 도중 실패")
        return real(db_, balance_, run)

    monkeypatch.setattr(server.lifecycle, "expire_run", flaky)
    # 없는 런은 건너뛰고, 실패한 런은 삼키고 계속 간다.
    assert server._settle_expired(db, balance, plan) == 0
