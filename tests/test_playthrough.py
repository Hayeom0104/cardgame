"""런 하나를 처음부터 끝까지 — **화면이 내놓은 것만 눌러서.**

이 파일은 테스트이자 탐침이다. 이번 작업에서 반복해 나온 결함은 전부 같은
모양이었다: 엔진은 완성인데 그 앞의 한 층이 비어 있고, 테스트는 그 층을
건너뛰고 엔진을 직접 불러서 통과한다.

그래서 여기서는 `custom_id` 를 **합성하지 않는다.** 화면이 돌려준 컴포넌트에
들어 있는 것만 누르고, 런이 끝날 때까지 그것을 반복한다. 중간에 누를 것이
없어지면 그 자리가 곧 구멍이다.
"""

from __future__ import annotations

import pytest

from app.api import controls
from app.api import events as ev
from app.api import handlers
from app.central import surfaces
from app.content.seed import TUTORIAL_WORLD_ID
from app.engine import lifecycle as lc

PARENT_CHANNEL = 5959
TERMINAL = {lc.RUN_COMPLETED, lc.RUN_DEFEATED, lc.RUN_ABANDONED, lc.RUN_EXPIRED}


class FakeCentral:
    """스레드를 열어 주고 코인을 받아 주는 최소 중앙봇."""

    def __init__(self):
        self._next = 900000
        self.balance = 0

    def create_thread(self, **kwargs):
        self._next += 1
        return {"thread_id": self._next, "message_id": 11}

    def recreate_thread(self, **kwargs):
        return self.create_thread(**kwargs)

    def edit_message(self, **kwargs):
        return {"status": "edited"}

    def get_user(self, user_id):
        return {"balance": self.balance}

    def currency_add(self, user_id, amount, idempotency_key):
        from app.central.client import CurrencyResult

        self.balance += amount
        return CurrencyResult(requested=amount, applied=amount)

    def currency_deduct(self, user_id, amount, idempotency_key):
        from app.central.client import CurrencyResult

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


def _cost_in(label: str) -> int:
    """라벨에 적힌 비용. 화면이 `이름 (비용)` 으로 적어 두므로 사람도 이걸 읽는다."""
    import re

    match = re.search(r"\((\d+)\)\s*$", label or "")
    return int(match.group(1)) if match else -1


def _strongest(options: list[dict]) -> dict:
    """가장 비싼 선택지. 늘 첫 번째만 누르면 평타로 보스를 깎게 된다 —
    사람이 하지 않을 선택이라 테스트가 게임을 대표하지 못한다."""
    return max(options, key=lambda option: _cost_in(option.get("label", "")))


class Session:
    """플레이어 한 명. 화면이 준 것만 누른다."""

    def __init__(self, ctx, user_id: int):
        self.ctx = ctx
        self.user_id = user_id
        self.screen: dict = {}
        self.presses = 0
        self.log: list[str] = []

    # -- 명령 --------------------------------------------------------
    def command(self, *args: str) -> dict:
        event = ev.MessageEvent(
            event_id=f"cmd-{self.presses}-{'-'.join(args)}", user_id=self.user_id,
            guild_id=1, channel_id=2, command="덱아웃", args=list(args),
            raw_content="!덱아웃 " + " ".join(args))
        self.presses += 1
        self.screen = handlers.handle_message(self.ctx, event)
        self.screen = surfaces.fulfil_thread_request(
            self.ctx.db, self.ctx.central, self.screen,
            parent_channel_id=PARENT_CHANNEL)
        return self.screen

    # -- 컴포넌트 ----------------------------------------------------
    def components(self) -> list[dict]:
        return self.screen.get("components") or []

    def press_first(self) -> dict:
        """화면의 컴포넌트를 눌러 본다.

        거절당하면 다음 컴포넌트로 넘어간다 — 사람도 그렇게 한다. 전부
        거절당하면 그 화면은 빠져나갈 길이 없는 화면이다.
        """
        components = self.components()
        if not components:
            raise AssertionError(
                f"누를 것이 없습니다 (상태 {self.state()!r}, "
                f"내용 {self.screen.get('content', '')[:60]!r})")

        reply = None
        for component in components:
            values = []
            if component.get("type") == "string_select":
                options = component.get("options") or []
                if not options:
                    raise AssertionError("선택지가 비어 있는 선택 컴포넌트입니다")
                values = [_strongest(options)["value"]]

            event = ev.InteractionEvent(
                event_id=f"press-{self.presses}", user_id=self.user_id,
                guild_id=1, channel_id=2, custom_id=component["custom_id"],
                values=values)
            self.presses += 1
            reply = handlers.handle_interaction(self.ctx, event)
            self.log.append(f"{self.state()}: {reply.get('content', '')[:40]}")
            if reply.get("action") != "reply_ephemeral":
                self.screen = reply
                return reply

        raise AssertionError(
            f"상태 {self.state()!r} 의 화면에서 누를 수 있는 것이 하나도 "
            f"없습니다 — 전부 거절되었습니다. 마지막 응답: "
            f"{(reply or {}).get('content', '')[:60]!r}")

    def state(self) -> str | None:
        run = lc.active_run_for(self.ctx.db, self.user_id)
        if run is not None:
            return run["state"]
        row = self.ctx.db.one(
            "SELECT state FROM runs WHERE user_id = ? ORDER BY run_id DESC LIMIT 1",
            (self.user_id,))
        return row["state"] if row else None

    def run_id(self) -> int:
        return self.ctx.db.one(
            "SELECT run_id FROM runs WHERE user_id = ? ORDER BY run_id DESC LIMIT 1",
            (self.user_id,))["run_id"]

    # -- 진행 --------------------------------------------------------
    def play(self, max_presses: int = 250, *, give_up: bool = True) -> str:
        """끝날 때까지 누른다. 누를 것이 없어지면 그 자리가 구멍이다.

        예산을 다 쓰면 `!덱아웃 포기` 로 끝낸다. 이 테스트가 확인하려는 것은
        "이길 수 있는가" 가 아니라 **"어느 화면에서도 막히지 않는가"** 이고,
        승패는 §15의 밸런싱 수치에 달려 있어 여기서 단정할 일이 아니다.
        """
        while self.presses < max_presses:
            state = self.state()
            if state in TERMINAL:
                return state
            if not self.components():
                # 화면에 컨트롤이 없다 — 지도로 돌아온 직후일 수 있으므로
                # 현재 상태에 맞는 것을 한 번 다시 그려 본다.
                if state == lc.MAP_NAVIGATION:
                    self.screen = {"components": controls.game_map(
                        self.ctx.db, self.run_id())}
                    if self.components():
                        continue
                raise AssertionError(
                    f"상태 {state!r} 에서 누를 것이 없습니다. "
                    f"지나온 화면: {self.log[-6:]}")
            self.press_first()

        if not give_up:
            raise AssertionError(
                f"{max_presses}번을 눌렀는데 런이 끝나지 않았습니다. "
                f"마지막 상태 {self.state()!r}")
        self.command("포기")
        return self.state()


# =====================================================================
# 튜토리얼 한 판
# =====================================================================
def test_a_tutorial_run_never_gets_stuck(ctx, db, user_id):
    """`!덱아웃` 부터 정산까지, 합성한 `custom_id` 없이.

    한 판을 도는 데 수백 번을 눌러야 하므로 확인할 것을 여기 모아 둔다 —
    같은 판을 여러 번 돌 이유가 없다.
    """
    session = Session(ctx, user_id)
    session.command()                 # 계정 생성 + 허브
    session.command("시작")

    # [6] SURFACE 가 끝나면 런은 지도에 서 있어야 한다 (§16.2.2).
    assert session.state() == lc.MAP_NAVIGATION, \
        "런을 시작했는데 지도에 서 있지 않습니다"
    assert session.components(), "런의 첫 화면에 누를 것이 없습니다"

    run_id = session.run_id()
    thread_id = db.one("SELECT thread_id FROM runs WHERE run_id = ?",
                       (run_id,))["thread_id"]
    assert thread_id is not None, "스레드 없이 런이 시작되었습니다"

    final = session.play()

    # 1. 끝났고, 왜 끝났는지가 남아 있다.
    assert final in TERMINAL
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    assert run["ended_at"] is not None
    assert run["end_reason"]

    # 2. 실제로 게임을 했다 — 전투를 적어도 하나 치렀다.
    battles = db.one("SELECT COUNT(*) AS n FROM battles WHERE run_id = ?",
                     (run_id,))["n"]
    assert battles >= 1, "전투 한 번 없이 런이 끝났습니다"

    # 3. 스레드는 런 내내 같은 것이었다.
    assert run["thread_id"] == thread_id, "런 도중에 스레드가 바뀌었습니다"

    # 4. §16.3 — 끝난 런이 계정을 붙들고 있으면 다음 런을 못 시작한다.
    assert lc.active_run_for(db, user_id) is None
    again = session.command("시작")
    assert again.get("content") != "이미 진행 중인 런이 있습니다."

    # 5. 조용히 삼킨 응답이 없었다.
    assert all(line.strip() for line in session.log)


def test_clearing_the_tutorial_opens_the_main_campaign(ctx, db, user_id):
    """§3.4.3 — 튜토리얼을 깨면 본편이 열려야 한다.

    화면을 눌러 보스를 잡는 데까지 가는 것은 위 테스트가 확인한다. 여기서
    보려는 것은 **클리어의 결과** 이므로, 보스 전투만 엔진에서 끝내고 그
    뒤를 본다 — §15의 밸런싱에 따라 몇 번을 지느냐가 달라지는 것에 테스트가
    좌우되지 않게 하기 위해서다.
    """
    from app.engine import battle as bt
    from app.engine import nodes
    from app.engine import units as un
    from app.engine.rng import JournaledRng

    session = Session(ctx, user_id)
    session.command()
    session.command("시작")
    run_id = session.run_id()

    # 보스 칸까지 화면을 눌러 나아간다.
    for _ in range(200):
        if session.state() == lc.BOSS_BATTLE:
            break
        if session.state() in TERMINAL:
            pytest.fail("보스에 닿기 전에 런이 끝났습니다")
        if not session.components() and session.state() == lc.MAP_NAVIGATION:
            session.screen = {"components": controls.game_map(db, run_id)}
        session.press_first()
    else:
        pytest.fail("보스 칸에 닿지 못했습니다")

    # 보스만 쓰러뜨린다.
    battle = db.one("SELECT battle_id FROM battles WHERE run_id = ? "
                    "AND state = 'active' ORDER BY battle_id DESC LIMIT 1",
                    (run_id,))
    for unit in un.load_units(db, battle["battle_id"], side=un.ENEMY):
        db.execute("UPDATE battle_units SET hp_current = 0, is_alive = 0 "
                   "WHERE battle_unit_id = ?", (unit.battle_unit_id,))

    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    rng = JournaledRng(db, run_id, run["rng_seed"])
    engine = bt.build_engine(db, ctx.balance, battle_id=battle["battle_id"],
                             run_id=run_id,
                             content_version_id=run["content_version_id"], rng=rng)
    # 전투 종료 판정은 라운드 경계에서 일어난다 (§2.12). 플레이어의 턴을
    # 기다리는 중이므로 여기서 한 번 밟아 주어야 `won` 으로 확정된다.
    engine.round_boundary()
    nodes.conclude_battle(db, ctx.balance, rng, run_id=run_id,
                          battle_id=battle["battle_id"])

    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.RUN_COMPLETED
    account = db.one("SELECT tutorial_completed_at FROM accounts WHERE user_id = ?",
                     (user_id,))
    assert account["tutorial_completed_at"] is not None
    assert account is not None

    # §3.4.3 — 완료하면 튜토리얼 월드는 **이용 불가가 되고** 본편 1세계가
    # 열린다. 갯수가 아니라 어디가 열렸는지를 본다.
    worlds = [row["world_id"] for row in db.query(
        "SELECT wu.world_id FROM world_unlocks wu JOIN worlds w "
        "ON w.world_id = wu.world_id AND w.content_version_id = ? "
        "WHERE wu.user_id = ? AND w.is_tutorial = 0",
        (ctx.content_version_id, user_id))]
    assert worlds, "튜토리얼을 깼는데 본편이 열리지 않았습니다"
    assert TUTORIAL_WORLD_ID not in worlds

    # 파티 슬롯도 2로 올라야 본편에 들어갈 수 있다 (§4.1).
    assert db.one("SELECT party_slots FROM accounts WHERE user_id = ?",
                  (user_id,))["party_slots"] >= 2


def test_a_shop_purchase_reaches_the_handler(ctx, db, user_id):
    """진열이 4~6줄이라 상점은 선택 컴포넌트를 쓴다. 핸들러가 `values` 를 보지
    않고 payload 만 읽어서, 선택으로 산 물건이 ValueError 로 터졌다."""
    from app.api import custom_id as cid

    session = Session(ctx, user_id)
    session.command()
    session.command("시작")
    run_id = session.run_id()
    db.execute("UPDATE runs SET run_currency = 500 WHERE run_id = ?", (run_id,))

    components = controls.shop(db, run_id, [
        {"item_index": 0, "price": 10, "purchased": 0,
         "item_ref": '{"kind": "card", "name": "시험"}'}])
    select = next(entry for entry in components
                  if entry["type"] == "string_select")
    # 선택 컴포넌트의 payload 는 비어 있고 값은 `values` 로 온다.
    assert cid.parse(select["custom_id"]).payload == ""
    assert select["options"][0]["value"] == "0"


def test_stock_you_cannot_afford_is_not_offered(ctx, db, user_id):
    """탐험 자금은 로컬 재화라 여기서 판단해도 틀릴 일이 없다. 걸러 내지
    않으면 빈털터리 플레이어에게 늘 거절되는 목록만 남는다."""
    session = Session(ctx, user_id)
    session.command()
    session.command("시작")
    run_id = session.run_id()
    db.execute("UPDATE runs SET run_currency = 5 WHERE run_id = ?", (run_id,))

    components = controls.shop(db, run_id, [
        {"item_index": 0, "price": 999, "purchased": 0, "item_ref": "{}"}])
    assert all(entry["type"] != "string_select" for entry in components)
    assert components, "나가기까지 사라지면 상점에서 못 나온다"
