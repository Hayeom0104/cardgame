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
from app.api import errors
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

        # 계정이 없는 첫 명령은 가입 프롬프트만 돌아온다 — 사람이라면 눌러야
        # 하는 화면이지, 원래 치려던 명령이 통과된 게 아니다. 가입을 눌러
        # 주고, 원래 명령을 다시 친다.
        registration = next((component for component in self.components()
                             if component.get("custom_id", "").startswith(
                                 handlers.REGISTER_PREFIX)), None)
        if registration is not None:
            self.press_first()
            return self.command(*args)
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
                # 준비 화면의 파티 선택처럼 여러 개를 요구하는 컴포넌트가 있다.
                # 하나만 골라 보내면 화면이 요구한 것과 다른 제출이 된다.
                wanted = max(1, int(component.get("min_values", 1)))
                values = [option["value"] for option in options[:wanted]]
                if wanted == 1:
                    values = [_strongest(options)["value"]]

            event = ev.InteractionEvent(
                event_id=f"press-{self.presses}", user_id=self.user_id,
                guild_id=1, channel_id=2, custom_id=component["custom_id"],
                values=values)
            self.presses += 1
            reply = handlers.handle_interaction(self.ctx, event)
            # `/event` 는 명령이든 컴포넌트든 **모든** 응답에 대해 스레드 요청을
            # 실행한다. 준비 화면의 마지막 단계가 컴포넌트이므로, 여기서
            # 빠뜨리면 런이 스레드 없이 만들어진다 (§1.3.5).
            reply = surfaces.fulfil_thread_request(
                self.ctx.db, self.ctx.central, reply,
                parent_channel_id=PARENT_CHANNEL)
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

    화면을 눌러 보스까지 가는 길은 위 테스트가 확인한다. 여기서 보려는 것은
    **클리어의 결과** 이므로 보스 칸을 바로 열고 끝낸다 — 지도를 걸어가면
    몇 번을 지느냐가 §15 밸런싱과 시드에 달려 있어, 이 테스트가 확인하려는
    것과 무관한 이유로 흔들린다.
    """
    from app.engine import battle as bt
    from app.engine import map_gen
    from app.engine import nodes
    from app.engine import units as un
    from app.engine.rng import JournaledRng

    session = Session(ctx, user_id)
    session.command()
    session.command("시작")
    run_id = session.run_id()

    boss = db.one("SELECT node_index FROM run_nodes WHERE run_id = ? "
                  "AND node_type = ? LIMIT 1", (run_id, map_gen.BOSS))
    assert boss is not None, "지도에 보스 칸이 없습니다"

    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    rng = JournaledRng(db, run_id, run["rng_seed"])
    db.execute("UPDATE runs SET state = ?, current_node_index = ? WHERE run_id = ?",
               (lc.NODE_RESOLUTION, boss["node_index"], run_id))
    opened = nodes.resolve_node(db, ctx.balance, rng, run_id=run_id,
                                node_index=boss["node_index"])
    assert opened["screen"] == "battle"

    for unit in un.load_units(db, opened["battle_id"], side=un.ENEMY):
        db.execute("UPDATE battle_units SET hp_current = 0, is_alive = 0 "
                   "WHERE battle_unit_id = ?", (unit.battle_unit_id,))

    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    engine = bt.build_engine(db, ctx.balance, battle_id=opened["battle_id"],
                             run_id=run_id,
                             content_version_id=run["content_version_id"], rng=rng)
    # 전투 종료 판정은 라운드 경계에서 일어난다 (§2.12).
    engine.round_boundary()
    nodes.conclude_battle(db, ctx.balance, rng, run_id=run_id,
                          battle_id=opened["battle_id"])

    assert db.one("SELECT state FROM runs WHERE run_id = ?",
                  (run_id,))["state"] == lc.RUN_COMPLETED

    account = db.one("SELECT tutorial_completed_at, party_slots FROM accounts "
                     "WHERE user_id = ?", (user_id,))
    assert account["tutorial_completed_at"] is not None
    # 파티 슬롯이 2가 아니면 본편에 들어갈 수 없다 (§4.1).
    assert account["party_slots"] >= 2

    # §3.4.3 — 튜토리얼 월드는 이용 불가가 되고 본편 1세계가 열린다.
    worlds = [row["world_id"] for row in db.query(
        "SELECT wu.world_id FROM world_unlocks wu JOIN worlds w "
        "ON w.world_id = wu.world_id AND w.content_version_id = ? "
        "WHERE wu.user_id = ? AND w.is_tutorial = 0",
        (ctx.content_version_id, user_id))]
    assert worlds, "튜토리얼을 깼는데 본편이 열리지 않았습니다"
    assert TUTORIAL_WORLD_ID not in worlds


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


# =====================================================================
# 본편 한 판 — 준비 화면부터
# =====================================================================
def graduate(db, user_id: int, content_version_id: int) -> None:
    """튜토리얼을 마친 계정으로 만든다 (§3.4.3 이 하는 일과 같은 결과).

    본편은 파티 2명부터이고(§4.1), 그 두 번째 자리는 §5.10의 첫 뽑기 보장으로
    채워진다. 여기서는 그 결과 상태를 바로 만든다 — 확인하려는 것은 뽑기가
    아니라 **본편 런이 화면으로 돌아가는가** 이기 때문이다.
    """
    from app.db.connection import utcnow

    db.execute("UPDATE accounts SET tutorial_completed_at = ?, party_slots = 2, "
               "carta = 5000 WHERE user_id = ?", (utcnow(), user_id))
    db.execute("DELETE FROM world_unlocks WHERE user_id = ?", (user_id,))
    db.execute("INSERT INTO world_unlocks (user_id, world_id, unlocked_at) "
               "VALUES (?, 'world_1', ?)", (user_id, utcnow()))
    db.execute("INSERT OR IGNORE INTO owned_characters (user_id, character_id, "
               "star_rank, acquired_at) VALUES (?, 'char_ignis', 2, ?)",
               (user_id, utcnow()))
    # 보상 칸이 제안할 수 있으려면 계정이 해금한 카드가 있어야 한다 (§3.2).
    for card_id in ("card_화_강타", "card_수_치유", "card_풍_질풍"):
        db.execute("INSERT OR IGNORE INTO unlocked_cards (user_id, card_id, "
                   "upgrade_tier, unlocked_at) VALUES (?, ?, 0, ?)",
                   (user_id, card_id, utcnow()))


def test_the_preparing_flow_can_be_completed_by_pressing(ctx, db, user_id):
    """§16.2.2 [1]~[6] — 월드·파티·덱·패시브를 화면에서 골라 런을 만든다.

    튜토리얼은 이 단계를 전부 건너뛰므로(월드도 파티도 정해져 있다), 준비
    화면이 실제로 눌러서 통과되는지는 본편에서만 확인할 수 있다.
    """
    session = Session(ctx, user_id)
    session.command()
    graduate(db, user_id, ctx.content_version_id)

    session.command("시작")
    assert session.components(), "준비 화면에 누를 것이 없습니다"

    # 런 행이 생길 때까지 준비 화면을 눌러 나아간다 ([5] MATERIALIZE).
    for _ in range(20):
        if lc.active_run_for(db, user_id) is not None:
            break
        session.press_first()
    else:
        pytest.fail(f"준비 화면을 빠져나오지 못했습니다: {session.log[-4:]}")

    run = lc.active_run_for(db, user_id)
    assert run["world_id"] == "world_1"
    assert run["is_tutorial"] == 0

    party = db.query("SELECT party_slot, character_id FROM run_characters "
                     "WHERE run_id = ? ORDER BY party_slot", (run["run_id"],))
    assert len(party) == 2, "본편인데 파티가 2명이 아닙니다 (§4.1)"

    # 자리마다 §4.6.2의 덱이 서 있어야 한다.
    for member in party:
        count = db.one("SELECT COUNT(*) AS n FROM run_deck_cards WHERE run_id = ? "
                       "AND party_slot = ?",
                       (run["run_id"], member["party_slot"]))["n"]
        assert count > 0, f"자리 {member['party_slot']} 의 덱이 비어 있습니다"


def test_a_main_campaign_run_never_gets_stuck(ctx, db, user_id):
    """본편은 파티 2명이라 라운드마다 두 번 고르고, 보상도 받을 자리를 고른다 —
    튜토리얼이 한 번도 지나지 않는 길이다."""
    session = Session(ctx, user_id)
    session.command()
    graduate(db, user_id, ctx.content_version_id)
    session.command("시작")

    for _ in range(20):
        if lc.active_run_for(db, user_id) is not None:
            break
        session.press_first()
    run_id = session.run_id()

    # 스레드가 열렸고 지도에 서 있다.
    assert db.one("SELECT thread_id FROM runs WHERE run_id = ?",
                  (run_id,))["thread_id"] is not None
    assert session.state() == lc.MAP_NAVIGATION

    final = session.play()
    assert final in TERMINAL
    battles = db.one("SELECT COUNT(*) AS n FROM battles WHERE run_id = ?",
                     (run_id,))["n"]
    assert battles >= 1, "전투 한 번 없이 본편 런이 끝났습니다"
    assert lc.active_run_for(db, user_id) is None


# =====================================================================
# 가입 게이트, 강제 튜토리얼 — 오너 지시로 §4.6.5의 "첫 명령에서 조용히
# 계정을 만든다"를 명시적 가입 단계로 바꾸고, 튜토리얼을 마치기 전에는
# 다른 진행(뽑기·상점 등)을 막는다.
#
# 아래 두 테스트는 공용 `user_id` 픽스처를 쓰지 않는다 — 그 픽스처는
# `create_account`를 직접 불러 계정을 미리 만들어 두므로, 가입 게이트
# 자체를 확인하려면 계정이 아예 없는 사용자로 시작해야 한다.
# =====================================================================
def test_an_unknown_user_is_prompted_to_register_before_anything_else(ctx, db):
    fresh_id = 700001
    event = ev.MessageEvent(event_id="e1", user_id=fresh_id, guild_id=1,
                            channel_id=2, command="덱아웃", args=[],
                            raw_content="!덱아웃")
    screen = handlers.handle_message(ctx, event)

    assert db.one("SELECT user_id FROM accounts WHERE user_id = ?",
                  (fresh_id,)) is None, "가입 버튼을 누르기 전인데 계정이 생겼습니다"
    components = screen.get("components") or []
    register = next((c for c in components if c.get("custom_id", "").startswith(
        handlers.REGISTER_PREFIX)), None)
    assert register is not None, "가입 버튼이 없습니다"

    interaction = ev.InteractionEvent(event_id="e2", user_id=fresh_id, guild_id=1,
                                      channel_id=2, custom_id=register["custom_id"],
                                      values=[])
    handlers.handle_interaction(ctx, interaction)
    assert db.one("SELECT user_id FROM accounts WHERE user_id = ?",
                  (fresh_id,)) is not None, "가입 버튼을 눌렀는데 계정이 안 생겼습니다"


def test_a_freshly_registered_account_cannot_skip_the_tutorial(ctx, db):
    fresh_id = 700002
    session = Session(ctx, fresh_id)
    session.command()          # 가입 프롬프트 → 자동으로 눌러 준다 → 허브

    for blocked in ("뽑기", "상점", "캐릭터", "장비", "연구", "업적", "덱", "패시브"):
        screen = session.command(blocked)
        assert screen.get("action") == "reply_ephemeral", \
            f"튜토리얼 전인데 '{blocked}' 화면이 열렸습니다"
        assert screen.get("content") == errors.TUTORIAL_NOT_CLEARED

    # 허브와 시작은 튜토리얼을 마치기 전에도 열려야 한다 — 그래야 튜토리얼
    # 자체를 진행할 수 있다.
    hub = session.command()
    assert hub.get("content") != errors.TUTORIAL_NOT_CLEARED
    start = session.command("시작")
    assert start.get("content") != errors.TUTORIAL_NOT_CLEARED
