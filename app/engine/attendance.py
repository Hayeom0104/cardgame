"""§13.2 출석 보상 — 하루에 한 번, 연속으로 올수록 많이.

## 하루의 경계

`daily_claims.date_kst` 라는 이름 그대로 한국 시각 기준이다. 다만 자정을
경계로 삼으면 밤 늦게 노는 사람이 "어제 것"과 "오늘 것"을 몇 분 사이에
연달아 받게 되므로, 경계 시각을 `daily_reset_hour_kst` 로 뺄 수 있게 했다.
4로 두면 새벽 4시가 날짜가 바뀌는 시점이고, 03:59의 접속은 아직 어제다.

## 빠뜨린 날

`daily_streak_grace_days` 만큼은 봐준다. 0이면 하루라도 거르면 연속이
1부터 다시 시작하고, 1이면 하루 거른 것까지는 이어진다. 이 값을 바꾸면
과거 기록의 해석 자체가 달라진다 — 연속 일수를 어딘가에 저장해 두지 않고
매번 `daily_claims` 에서 다시 세기 때문이다. 저장해 두었다면 규칙을 바꾼
순간 옛 값과 새 규칙이 뒤섞였을 것이다.

## 코인

코인은 중앙봇이 가진 재화라서 §17의 `grant` 트랜잭션으로 나간다. 키를
`daily:{user_id}:{날짜}` 로 만드는 것이 이 기능의 중복 방지 장치다 — 같은
날 두 번 눌러도 같은 키이므로 두 번 지급될 수 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.central import transactions as tx
from app.content.balance import Balance
from app.db.connection import Database, utcnow

CLAIM_TYPE = "attendance"

#: 한국 표준시. 서머타임이 없어서 고정 오프셋으로 충분하다.
KST = timezone(timedelta(hours=9))


class AttendanceError(RuntimeError):
    """평범한 거절 — 오늘 것을 이미 받았다든지."""


@dataclass
class ClaimResult:
    date_kst: str
    streak: int
    day_in_cycle: int
    coin: int = 0
    carta: int = 0
    wildcards: int = 0
    coin_result: object = None
    already_claimed: bool = False
    log: list[str] = field(default_factory=list)


# =====================================================================
# 날짜
# =====================================================================
def today_kst(balance: Balance, now: datetime | None = None) -> date:
    """지금이 속한 "출석일".

    경계 시각 전이면 아직 어제로 친다.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(KST)
    reset_hour = int(balance.get("daily_reset_hour_kst"))
    return (moment - timedelta(hours=reset_hour)).date()


def _claimed_dates(db: Database, user_id: int) -> set[date]:
    rows = db.query(
        "SELECT date_kst FROM daily_claims WHERE user_id = ? AND claim_type = ?",
        (user_id, CLAIM_TYPE),
    )
    return {date.fromisoformat(row["date_kst"]) for row in rows}


def streak_through(dates: set[date], anchor: date, grace: int) -> int:
    """`anchor` 를 마지막 출석일로 볼 때의 연속 일수.

    하루 전이 비어 있어도 `grace` 만큼 더 거슬러 올라가 찾는다. 찾으면 연속이
    이어진 것으로 보고, 못 찾으면 거기서 끊긴다.
    """
    if anchor not in dates:
        return 0
    streak = 0
    current = anchor
    while True:
        streak += 1
        previous = None
        for back in range(1, grace + 2):
            candidate = current - timedelta(days=back)
            if candidate in dates:
                previous = candidate
                break
        if previous is None:
            return streak
        current = previous


# =====================================================================
# 조회
# =====================================================================
def status(db: Database, balance: Balance, *, user_id: int,
           now: datetime | None = None) -> dict:
    """지금 받을 수 있는지, 받으면 며칠째인지, 무엇을 받는지."""
    today = today_kst(balance, now)
    dates = _claimed_dates(db, user_id)
    grace = int(balance.get("daily_streak_grace_days"))

    claimed = today in dates
    streak = streak_through(dates | {today}, today, grace)
    rewards = balance.get("daily_rewards")
    cycle = len(rewards)
    day_in_cycle = ((streak - 1) % cycle) + 1 if cycle else 1
    reward = _reward_for(rewards, day_in_cycle)

    return {
        "date_kst": today.isoformat(),
        "claimable": not claimed,
        "streak": streak,
        "day_in_cycle": day_in_cycle,
        "cycle_length": cycle,
        "reward": reward,
    }


def _reward_for(rewards: dict, day_in_cycle: int) -> dict:
    entry = rewards.get(str(day_in_cycle)) or {}
    return {
        "coin": int(entry.get("coin", 0)),
        "carta": int(entry.get("carta", 0)),
        "wildcards": int(entry.get("wildcards", 0)),
    }


# =====================================================================
# 수령
# =====================================================================
def claim(db: Database, balance: Balance, central, *, user_id: int,
          now: datetime | None = None) -> ClaimResult:
    """오늘 것을 받는다. 같은 날 두 번째 호출은 아무것도 주지 않는다."""
    state = status(db, balance, user_id=user_id, now=now)
    result = ClaimResult(date_kst=state["date_kst"], streak=state["streak"],
                         day_in_cycle=state["day_in_cycle"],
                         **state["reward"])
    if not state["claimable"]:
        result.already_claimed = True
        return result

    account = db.one("SELECT user_id FROM accounts WHERE user_id = ?", (user_id,))
    if account is None:
        raise AttendanceError("계정이 없습니다.")

    payload = {
        "kind": CLAIM_TYPE,
        "user_id": user_id,
        "date_kst": state["date_kst"],
        "carta": result.carta,
        "wildcards": result.wildcards,
    }

    if result.coin <= 0:
        # 코인이 없는 날은 중앙봇에 갈 일이 없다. 그래도 기록과 지급은 한
        # 트랜잭션에서 이뤄져야 두 번 받는 일이 없다.
        with db.tx():
            _apply_claim(db, payload)
        return result

    # 키에 날짜가 들어 있는 것이 이 기능의 중복 방지 장치다 (§17.3 규칙 2).
    tx_id = f"daily:{user_id}:{state['date_kst']}"
    tx.create_transaction(
        db, tx_id=tx_id, user_id=user_id, operation=CLAIM_TYPE,
        direction=tx.GRANT, expected_coin_delta=result.coin,
        local_payload=payload,
    )
    result.coin_result = tx.run_transaction(
        db, central, tx_id=tx_id, apply_local=_apply_claim, kind=CLAIM_TYPE)
    return result


def _apply_claim(db: Database, payload: dict) -> None:
    """로컬 효과 — 출석 기록과 카르타·와일드카드.

    기록을 여기에 두는 이유: 지급과 같은 로컬 트랜잭션 안에 있어야 "기록은
    남았는데 못 받았다" 나 "받았는데 기록이 없다" 가 생기지 않는다.
    """
    db.execute(
        "INSERT OR IGNORE INTO daily_claims (user_id, date_kst, claim_type) "
        "VALUES (?, ?, ?)",
        (payload["user_id"], payload["date_kst"], CLAIM_TYPE),
    )
    if payload.get("carta") or payload.get("wildcards"):
        db.execute(
            "UPDATE accounts SET carta = carta + ?, wildcards = wildcards + ?, "
            "updated_at = ? WHERE user_id = ?",
            (int(payload.get("carta", 0)), int(payload.get("wildcards", 0)),
             utcnow(), payload["user_id"]),
        )
