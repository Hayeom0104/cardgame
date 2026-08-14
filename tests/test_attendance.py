"""§13.2 출석 보상 — 하루의 경계, 연속 규칙, 두 번 받지 않기."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.api import handlers, hub
from app.central.client import CurrencyResult
from app.engine import attendance as att
from app.engine.attendance import KST


class FakeCentral:
    def __init__(self, balance: int = 0):
        self.balance = balance
        self.applied_keys: dict[str, int] = {}
        self.add_calls = 0

    def currency_add(self, user_id, amount, idempotency_key):
        self.add_calls += 1
        if idempotency_key in self.applied_keys:
            return CurrencyResult(requested=amount,
                                  applied=self.applied_keys[idempotency_key])
        self.balance += amount
        self.applied_keys[idempotency_key] = amount
        return CurrencyResult(requested=amount, applied=amount)

    def currency_deduct(self, user_id, amount, idempotency_key):
        applied = -min(abs(amount), self.balance)
        self.balance += applied
        return CurrencyResult(requested=-abs(amount), applied=applied)


@pytest.fixture
def central() -> FakeCentral:
    return FakeCentral()


@pytest.fixture
def ctx(db, balance, version, central):
    return handlers.HandlerContext(db=db, balance=balance, central=central,
                                   content_version_id=version)


def at(day: str, hour: int, minute: int = 0) -> datetime:
    """한국 시각으로 그 순간."""
    year, month, dayno = (int(part) for part in day.split("-"))
    return datetime(year, month, dayno, hour, minute, tzinfo=KST)


def mark(db, user_id: int, *days: str) -> None:
    for day in days:
        db.execute(
            "INSERT OR IGNORE INTO daily_claims (user_id, date_kst, claim_type) "
            "VALUES (?, ?, ?)", (user_id, day, att.CLAIM_TYPE))


# =====================================================================
# 하루의 경계
# =====================================================================
def test_the_day_turns_over_at_the_configured_hour(balance):
    """경계가 4시면 03:59는 아직 어제다."""
    assert att.today_kst(balance, at("2026-03-10", 3, 59)) == date(2026, 3, 9)
    assert att.today_kst(balance, at("2026-03-10", 4, 0)) == date(2026, 3, 10)


def test_midnight_boundary_is_configurable(db, balance, version):
    db.execute("UPDATE balancing_constants SET value_json = '0' "
               "WHERE content_version_id = ? AND key = 'daily_reset_hour_kst'",
               (version,))
    fresh = type(balance)(db, version)
    assert att.today_kst(fresh, at("2026-03-10", 3, 59)) == date(2026, 3, 10)


def test_utc_input_is_read_in_korean_time(balance):
    """UTC 15:30 은 한국의 다음 날 00:30 — 경계가 4시이므로 아직 그 전날이다."""
    moment = datetime(2026, 3, 9, 15, 30, tzinfo=timezone.utc)
    assert att.today_kst(balance, moment) == date(2026, 3, 9)


# =====================================================================
# 연속 일수
# =====================================================================
def test_consecutive_days_build_a_streak():
    days = {date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 3)}
    assert att.streak_through(days, date(2026, 3, 3), grace=0) == 3


def test_a_missed_day_breaks_the_streak_without_grace():
    days = {date(2026, 3, 1), date(2026, 3, 3)}
    assert att.streak_through(days, date(2026, 3, 3), grace=0) == 1


def test_one_missed_day_is_forgiven_with_one_day_of_grace():
    days = {date(2026, 3, 1), date(2026, 3, 3)}
    assert att.streak_through(days, date(2026, 3, 3), grace=1) == 2


def test_two_missed_days_break_even_with_one_day_of_grace():
    days = {date(2026, 3, 1), date(2026, 3, 4)}
    assert att.streak_through(days, date(2026, 3, 4), grace=1) == 1


def test_a_day_that_was_never_claimed_has_no_streak():
    assert att.streak_through({date(2026, 3, 1)}, date(2026, 3, 5), grace=1) == 0


# =====================================================================
# 조회
# =====================================================================
def test_a_new_account_can_claim_today(db, balance, user_id):
    state = att.status(db, balance, user_id=user_id)
    assert state["claimable"]
    assert state["streak"] == 1
    assert state["day_in_cycle"] == 1
    assert state["reward"]["coin"] > 0


def test_the_reward_cycles_back_to_day_one(db, balance, user_id):
    """주기가 7이면 8일째는 다시 1일째의 보상이다."""
    today = att.today_kst(balance)
    mark(db, user_id, *[(today - timedelta(days=n)).isoformat()
                        for n in range(1, 8)])
    state = att.status(db, balance, user_id=user_id)
    assert state["streak"] == 8
    assert state["day_in_cycle"] == 1


def test_the_seventh_day_pays_more_than_the_first(db, balance, user_id):
    today = att.today_kst(balance)
    mark(db, user_id, *[(today - timedelta(days=n)).isoformat()
                        for n in range(1, 7)])
    seventh = att.status(db, balance, user_id=user_id)
    assert seventh["day_in_cycle"] == 7
    first = att._reward_for(balance.get("daily_rewards"), 1)
    assert seventh["reward"]["carta"] > first["carta"]


# =====================================================================
# 수령
# =====================================================================
def test_claiming_pays_out_and_records_the_day(db, balance, central, user_id):
    before = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                    (user_id,))["carta"]
    result = att.claim(db, balance, central, user_id=user_id)

    assert not result.already_claimed
    after = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                   (user_id,))["carta"]
    assert after - before == result.carta
    assert central.balance == result.coin
    assert db.one(
        "SELECT 1 FROM daily_claims WHERE user_id = ? AND date_kst = ?",
        (user_id, result.date_kst)) is not None


def test_a_second_claim_on_the_same_day_pays_nothing(db, balance, central,
                                                     user_id):
    att.claim(db, balance, central, user_id=user_id)
    carta = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                   (user_id,))["carta"]
    coin = central.balance

    again = att.claim(db, balance, central, user_id=user_id)
    assert again.already_claimed
    assert db.one("SELECT carta FROM accounts WHERE user_id = ?",
                  (user_id,))["carta"] == carta
    assert central.balance == coin


def test_the_transaction_key_is_the_day_so_a_retry_cannot_pay_twice(
        db, balance, central, user_id):
    """§17.3 규칙 2 — 같은 날의 재시도는 새 키를 만들지 않는다."""
    result = att.claim(db, balance, central, user_id=user_id)
    db.execute("DELETE FROM daily_claims WHERE user_id = ?", (user_id,))

    att.claim(db, balance, central, user_id=user_id)
    assert central.balance == result.coin, "코인이 두 번 지급되었습니다"
    keys = db.query("SELECT tx_id FROM purchase_transactions WHERE user_id = ?",
                    (user_id,))
    assert len(keys) == 1


def test_a_day_with_no_coin_never_calls_central(db, balance, version, central,
                                                user_id):
    db.execute(
        "UPDATE balancing_constants SET value_json = ? WHERE content_version_id = ? "
        "AND key = 'daily_rewards'",
        ('{"1": {"coin": 0, "carta": 25, "wildcards": 0}}', version),
    )
    fresh = type(balance)(db, version)
    result = att.claim(db, fresh, central, user_id=user_id)
    assert result.coin == 0
    assert central.add_calls == 0
    assert result.carta == 25


def test_claiming_for_an_account_that_does_not_exist_is_refused(db, balance,
                                                               central):
    with pytest.raises(att.AttendanceError):
        att.claim(db, balance, central, user_id=999999)


# =====================================================================
# 화면에서 닿는가
# =====================================================================
def test_the_hub_offers_the_claim_button(ctx, user_id):
    screen = handlers.hub_screen(ctx, user_id)
    ids = [component["custom_id"] for component in screen.get("components", [])]
    assert f"{hub.HUB_PREFIX}daily" in ids
    assert "출석" in screen["content"]


def test_the_button_actually_pays_out(ctx, db, user_id):
    before = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                    (user_id,))["carta"]
    reply = hub.handle_hub(ctx, user_id, f"{hub.HUB_PREFIX}daily", [],
                           event_id="evt-1")
    after = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                   (user_id,))["carta"]
    assert after > before
    assert "1일째" in reply["content"]


def test_the_button_disappears_once_claimed(ctx, user_id):
    hub.handle_hub(ctx, user_id, f"{hub.HUB_PREFIX}daily", [], event_id="evt-1")
    screen = handlers.hub_screen(ctx, user_id)
    ids = [component["custom_id"] for component in screen.get("components", [])]
    assert f"{hub.HUB_PREFIX}daily" not in ids
    assert "이미 받았습니다" in screen["content"] or "받았습니다" in screen["content"]


def test_pressing_the_button_twice_says_so(ctx, user_id):
    hub.handle_hub(ctx, user_id, f"{hub.HUB_PREFIX}daily", [], event_id="evt-1")
    reply = hub.handle_hub(ctx, user_id, f"{hub.HUB_PREFIX}daily", [],
                           event_id="evt-2")
    assert "이미 받았습니다" in reply["content"]


# =====================================================================
# 복구
# =====================================================================
def test_recovery_knows_how_to_finish_an_attendance_claim():
    """§17.4 — 이 표에 없으면 코인만 나가고 로컬 효과가 영영 유실된다."""
    from app.engine import progression as pg

    assert att.CLAIM_TYPE in pg.local_handlers()
