"""실제로 런을 진행하면서, 전투 화면마다 그림이 실제로 그려지는지 확인한다.

`visuals.battle()` 은 그림이 실패해도 화면이 죽지 않도록 예외를 전부
삼킨다(§11) — 그런데 그 덕분에, 렌더 쪽에 버그가 생겨도 "첨부가 0장"이라는
결과만 남고 테스트는 그걸 눈치채지 못한다. `test_playthrough.py` 의
`Session` 은 컴포넌트가 있는지만 보지 그림이 실제로 나왔는지는 보지 않는다.

여기서는 `visuals.battle()` 이 하는 일을 그대로, 다만 예외를 삼키지 않고
반복해서 — 진행 중인 런의 전투마다 불러서, 첨부 두 장이 실제로 유효한 PNG로
나오는지 확인한다.
"""

from __future__ import annotations

import pytest

from app.api import handlers
from app.api import visuals
from app.engine import battle as bt
from app.engine import lifecycle as lc
from app.engine import passives as pv
from app.engine import units as un
from app.render import panels
from app.render import theme as theme_module
from tests.test_playthrough import FakeCentral, Session, graduate


@pytest.fixture
def central() -> FakeCentral:
    return FakeCentral()


@pytest.fixture
def ctx(db, balance, version, central):
    return handlers.HandlerContext(db=db, balance=balance, central=central,
                                   content_version_id=version)


def render_battle_unwrapped(db, balance, *, battle_id: int, run) -> list:
    """`visuals.battle()` 과 같은 일을 하되, 예외를 삼키지 않는다.

    렌더가 실제로 실패하면 여기서 바로 터져야 그 자리에서 원인을 알 수
    있다 — `safely()` 뒤에서는 그냥 빈 목록만 남는다."""
    allies = [visuals._unit_view(db, unit, run) for unit in
              un.load_units(db, battle_id, side=un.ALLY)]
    enemies = [visuals._unit_view(db, unit, run) for unit in
               un.load_units(db, battle_id, side=un.ENEMY)]
    row = db.one("SELECT * FROM battles WHERE battle_id = ?", (battle_id,))

    engine = bt.build_engine(
        db, balance, battle_id=battle_id, run_id=run["run_id"],
        content_version_id=run["content_version_id"], rng=None)
    telegraphs = {entry["enemy_unit_id"]: entry for entry in engine.telegraphs()}

    return panels.render_battle_screen(
        allies, enemies, telegraphs,
        resource=int(row["party_resource_current"] or 0),
        round_no=int(row["round_no"] or 1),
        hand=visuals._hand(db, run),
        passives=pv.equipped(db, battle_id, run["content_version_id"]),
        log=bt.recent_log(db, battle_id,
                          theme_module.load().int_("battle_log_lines")))


def _assert_battle_screen_is_valid(db, balance, run_id: int) -> None:
    battle_row = db.one(
        "SELECT battle_id FROM battles WHERE run_id = ? "
        "ORDER BY battle_id DESC LIMIT 1", (run_id,))
    if battle_row is None:
        return
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))

    attachments = render_battle_unwrapped(
        db, balance, battle_id=battle_row["battle_id"], run=run)

    assert len(attachments) == 2, (
        f"전투 화면은 항상 PNG 두 장이어야 합니다 (§1.3.7) — "
        f"실제로는 {len(attachments)}장")
    names = [attachment.filename for attachment in attachments]
    assert names == ["deckout_situation.png", "deckout_turn.png"], names
    for attachment in attachments:
        attachment.validate()
        assert attachment.decoded_size > 0


class RenderCheckingSession(Session):
    """`Session` 과 같지만, 누를 때마다 지금 전투 화면이 실제로 그려지는지도
    같이 확인한다."""

    def __init__(self, ctx, user_id: int, balance):
        super().__init__(ctx, user_id)
        self.balance = balance
        self.battle_rounds_seen = 0

    def _check(self) -> None:
        state = self.state()
        if state not in (lc.BATTLE, lc.BOSS_BATTLE):
            return
        run = lc.active_run_for(self.ctx.db, self.user_id)
        if run is None:
            return
        _assert_battle_screen_is_valid(self.ctx.db, self.balance, run["run_id"])
        self.battle_rounds_seen += 1

    def command(self, *args: str) -> dict:
        result = super().command(*args)
        self._check()
        return result

    def press_first(self) -> dict:
        result = super().press_first()
        self._check()
        return result


# =====================================================================
# 튜토리얼 한 판 — 전투마다 그림 확인
# =====================================================================
def test_every_battle_screen_in_a_tutorial_run_renders(ctx, db, balance, user_id):
    session = RenderCheckingSession(ctx, user_id, balance)
    session.command()
    session.command("시작")
    final = session.play()

    assert final in (lc.RUN_COMPLETED, lc.RUN_DEFEATED, lc.RUN_ABANDONED,
                     lc.RUN_EXPIRED)
    assert session.battle_rounds_seen >= 1, \
        "전투 화면을 한 번도 확인하지 못한 채 런이 끝났습니다"


def test_every_battle_screen_in_a_main_campaign_run_renders(ctx, db, balance, user_id):
    session = RenderCheckingSession(ctx, user_id, balance)
    session.command()
    graduate(db, user_id, ctx.content_version_id)
    session.command("시작")

    for _ in range(20):
        if lc.active_run_for(db, user_id) is not None:
            break
        session.press_first()

    final = session.play()
    assert final in (lc.RUN_COMPLETED, lc.RUN_DEFEATED, lc.RUN_ABANDONED,
                     lc.RUN_EXPIRED)
    assert session.battle_rounds_seen >= 1


# =====================================================================
# 보스전 — 적이 많을 때도 그림이 나오는지
# =====================================================================
def test_the_boss_battle_screen_renders_with_equipped_passives(ctx, db, balance, user_id):
    """장착 패시브가 있는 상태로 보스전 화면이 실제로 그려지는지 — 손패
    화면에 패시브 줄을 새로 얹은 뒤로는 아직 실제 런에서 확인한 적이
    없었다."""
    from app.db.connection import utcnow
    from app.engine import map_gen

    session = Session(ctx, user_id)
    session.command()
    session.command("시작")
    run_id = session.run_id()

    # §6 — 패시브 하나를 장착한 상태로 만든다.
    passive_row = db.one(
        "SELECT passive_card_id FROM passive_cards WHERE content_version_id = ? "
        "LIMIT 1", (ctx.content_version_id,))
    if passive_row is not None:
        db.execute(
            "INSERT OR IGNORE INTO unlocked_passives (user_id, passive_card_id, "
            "unlocked_at) VALUES (?, ?, ?)",
            (user_id, passive_row["passive_card_id"], utcnow()))
        db.execute(
            "INSERT OR REPLACE INTO run_passives (run_id, passive_slot, "
            "passive_card_id) VALUES (?, 1, ?)",
            (run_id, passive_row["passive_card_id"]))

    boss = db.one("SELECT node_index FROM run_nodes WHERE run_id = ? "
                  "AND node_type = ? LIMIT 1", (run_id, map_gen.BOSS))
    assert boss is not None

    from app.engine import nodes
    from app.engine.rng import JournaledRng

    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    rng = JournaledRng(db, run_id, run["rng_seed"])
    db.execute("UPDATE runs SET state = ?, current_node_index = ? WHERE run_id = ?",
               (lc.NODE_RESOLUTION, boss["node_index"], run_id))
    opened = nodes.resolve_node(db, ctx.balance, rng, run_id=run_id,
                                node_index=boss["node_index"])
    assert opened["screen"] == "battle"

    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    enemy_count = db.one(
        "SELECT COUNT(*) AS n FROM battle_units WHERE battle_id = ? AND side = 'enemy'",
        (opened["battle_id"],))["n"]
    assert enemy_count >= 1

    _assert_battle_screen_is_valid(db, ctx.balance, run_id)


# =====================================================================
# 전투 말고 다른 화면들도 — 렌더 실패를 삼키지 않고 처음부터 끝까지
# =====================================================================
def test_no_screen_in_a_tutorial_run_silently_fails_to_render(ctx, db, user_id,
                                                               monkeypatch):
    """`visuals.safely()` 는 렌더 실패를 삼켜 화면을 글자만으로 대신한다
    (§11) — 안전하지만, 그 안전망 뒤에서 그림이 매번 조용히 비어 나가도
    아무 테스트도 알아채지 못한다. 여기서는 안전망을 잠깐 꺼서, 지도·상점·
    준비·보상·정산 화면 전부가 실제 플레이 도중 예외 없이 그려지는지
    확인한다."""
    monkeypatch.setattr(visuals, "safely", lambda build: build())

    session = Session(ctx, user_id)
    session.command()
    session.command("시작")
    final = session.play()
    assert final in (lc.RUN_COMPLETED, lc.RUN_DEFEATED, lc.RUN_ABANDONED,
                     lc.RUN_EXPIRED)


def test_no_screen_in_a_main_campaign_run_silently_fails_to_render(ctx, db, user_id,
                                                                    monkeypatch):
    monkeypatch.setattr(visuals, "safely", lambda build: build())

    session = Session(ctx, user_id)
    session.command()
    graduate(db, user_id, ctx.content_version_id)
    session.command("시작")

    for _ in range(20):
        if lc.active_run_for(db, user_id) is not None:
            break
        session.press_first()

    final = session.play()
    assert final in (lc.RUN_COMPLETED, lc.RUN_DEFEATED, lc.RUN_ABANDONED,
                     lc.RUN_EXPIRED)


# =====================================================================
# 대상 선택 화면 — 그림이 사라지지 않고, 무엇을 고르는지 알려 준다
# =====================================================================
def _probe_target_prompt(session) -> tuple[bool, dict | None]:
    """전투 중이면 손패 카드를 차례로 골라 본다. 대상이 필요한 카드는 대상
    선택 화면을 돌려주고(상태 변화 없음), 필요 없는 카드는 그 자리에서
    쓰인다 — 그러면 그게 이번 클릭이므로 세션 화면을 그 응답으로 옮긴다.

    (눌렀는지, 대상 선택 화면)을 돌려준다."""
    from app.api import events as ev

    select = next((c for c in session.components()
                   if c.get("placeholder") == "낼 카드를 고르세요"), None)
    if select is None:
        return False, None
    for option in select.get("options") or []:
        reply = handlers.handle_interaction(session.ctx, ev.InteractionEvent(
            event_id=None, user_id=session.user_id, guild_id=1,
            channel_id=session.thread_id or 2, thread_id=session.thread_id,
            custom_id=select["custom_id"], values=[option["value"]]))
        if reply.get("content") == "대상을 선택하세요.":
            return True, reply
        if reply.get("action") != "reply_ephemeral":
            session.screen = reply
            return True, None
    return False, None


def test_the_target_select_screen_keeps_the_turn_image_and_says_what_to_pick(
        ctx, db):
    """응답 경로 첨부는 replace만 지원한다(연동 가이드 §8). 대상 선택
    편집에 그림을 빼면 고르는 동안 전투 그림이 메시지에서 사라진다."""
    from app.content.seed import create_account

    prompt = None
    for attempt in range(5):                      # 지도는 매번 무작위다
        user = 515000 + attempt
        create_account(db, user, ctx.content_version_id)
        session = Session(ctx, user)
        session.command()
        session.command("시작")
        for _ in range(60):
            if session.state() in (lc.RUN_COMPLETED, lc.RUN_DEFEATED,
                                   lc.RUN_ABANDONED, lc.RUN_EXPIRED):
                break
            pressed, prompt = _probe_target_prompt(session)
            if prompt is not None:
                break
            if not pressed:
                session.press_first()
        if prompt is not None:
            break
    assert prompt is not None, "다섯 판 동안 대상 선택이 한 번도 나오지 않았습니다"

    select = prompt["components"][0]
    assert select.get("placeholder") == "대상을 고르세요"
    assert [a["filename"] for a in prompt["attachments"]] == ["deckout_turn.png"]
