"""런 사이 진행 — §3.4.3 튜토리얼 완료 · §4.4 성급 상승 · §8.4 강화 · §9.2 연구
· §7.2 허브 상점."""

from __future__ import annotations

import pytest

from app.central import transactions as tx
from app.central.client import CurrencyResult
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID, WORLD_1_ID
from app.db.connection import utcnow
from app.engine import achievements as ach
from app.engine import lifecycle as lc
from app.engine import progression as pg
from app.engine import settlement as sl
from app.engine.rng import JournaledRng


class FakeCentral:
    """§1.3.8대로 차감을 잔액에서 clamp하는 중앙봇 스텁."""

    def __init__(self, balance: int = 1_000_000):
        self.balance = balance
        self.applied_keys: dict[str, int] = {}

    def currency_add(self, user_id, amount, idempotency_key):
        if idempotency_key in self.applied_keys:
            return CurrencyResult(requested=amount,
                                  applied=self.applied_keys[idempotency_key])
        self.balance += amount
        self.applied_keys[idempotency_key] = amount
        return CurrencyResult(requested=amount, applied=amount)

    def currency_deduct(self, user_id, amount, idempotency_key):
        if idempotency_key in self.applied_keys:
            return CurrencyResult(requested=-abs(amount),
                                  applied=self.applied_keys[idempotency_key])
        applied = -min(abs(amount), self.balance)
        self.balance += applied
        self.applied_keys[idempotency_key] = applied
        return CurrencyResult(requested=-abs(amount), applied=applied)


@pytest.fixture
def central() -> FakeCentral:
    return FakeCentral()


# =====================================================================
# §3.4.3 튜토리얼 완료
# =====================================================================
def test_completing_the_tutorial_grants_slot_two_and_carta(db, balance, version,
                                                           user_id):
    before = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    assert before["party_slots"] == 1
    assert before["tutorial_completed_at"] is None

    result = pg.complete_tutorial(db, balance, user_id=user_id,
                                  content_version_id=version)

    after = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    assert after["party_slots"] == 2
    assert after["carta"] == before["carta"] + 300
    assert after["tutorial_completed_at"] is not None
    assert result["unlocked_world"] == WORLD_1_ID


def test_the_tutorial_world_becomes_unavailable_on_completion(db, balance, version,
                                                              user_id):
    """완료 시 계정은 본편으로 전환되고 튜토리얼 월드는 이용 불가가 된다."""
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    unlocked = {row["world_id"] for row in db.query(
        "SELECT world_id FROM world_unlocks WHERE user_id = ?", (user_id,))}
    assert TUTORIAL_WORLD_ID not in unlocked
    assert WORLD_1_ID in unlocked


def test_completion_does_not_grant_a_second_character(db, balance, version,
                                                      user_id):
    """[v6.3] "두 번째 캐릭터" 줄은 표에서 삭제되었다 — 슬롯을 채우는 것은
    §5.10 뽑기 보장의 일이다."""
    before = db.one("SELECT COUNT(*) AS n FROM owned_characters WHERE user_id = ?",
                    (user_id,))["n"]
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    assert db.one("SELECT COUNT(*) AS n FROM owned_characters WHERE user_id = ?",
                  (user_id,))["n"] == before


def test_completion_is_idempotent(db, balance, version, user_id):
    """반복 불가 — 완료 시 영구 잠금."""
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    carta = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                   (user_id,))["carta"]

    again = pg.complete_tutorial(db, balance, user_id=user_id,
                                 content_version_id=version)
    assert again["already_completed"] is True
    assert db.one("SELECT carta FROM accounts WHERE user_id = ?",
                  (user_id,))["carta"] == carta


def test_settling_a_cleared_tutorial_run_completes_the_tutorial(db, balance,
                                                                version, user_id):
    """정산의 rewards 단계가 §3.4.3 보상을 지급한다 — 이것이 없으면 튜토리얼을
    깨도 본편에 진입할 수 없다."""
    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    rng = JournaledRng(db, run_id, 1)

    sl.enter_settlement(db, run_id, target_state=sl.RUN_COMPLETED,
                        end_reason="보스 처치")
    report = sl.advance_settlement(db, balance, rng, run_id=run_id,
                                   content_version_id=version)

    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    assert account["tutorial_completed_at"] is not None
    assert account["party_slots"] == 2
    # 튜토리얼은 §15.4 경제 모델 밖이므로 런 클리어 코인은 지급하지 않는다.
    assert report["rewards"]["coin"] == 0
    assert report["rewards"]["carta"] == 300


def test_the_main_campaign_becomes_startable_after_the_tutorial(db, balance,
                                                                version, user_id):
    """튜토리얼 완료 → 파티 슬롯 2 → 캐릭터 2명이면 본편 런 생성이 통과한다."""
    pg.complete_tutorial(db, balance, user_id=user_id, content_version_id=version)
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_ignis', 2, ?)", (user_id, utcnow()))

    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=WORLD_1_ID,
                           party_character_ids=[STARTER_CHARACTER_ID, "char_ignis"],
                           is_tutorial=False),
        version)
    assert db.one("SELECT COUNT(*) AS n FROM run_characters WHERE run_id = ?",
                  (run_id,))["n"] == 2


# =====================================================================
# §4.4 / §15.4 성급 상승
# =====================================================================
def _give_star_up_materials(db, user_id, character_id, fragments, wildcards):
    db.execute(
        "INSERT INTO character_fragments (user_id, character_id, amount) "
        "VALUES (?, ?, ?) ON CONFLICT(user_id, character_id) DO UPDATE SET "
        "amount = ?", (user_id, character_id, fragments, fragments))
    db.execute("UPDATE accounts SET wildcards = ? WHERE user_id = ?",
               (wildcards, user_id))


def test_star_up_costs_match_the_published_table(db, balance, version, user_id):
    """§15.4 🟡 R-5 — 1★→2★는 조각 10 + 와일드카드 1 + 코인 3,000."""
    assert pg.star_up_cost(balance, 1) == {"fragments": 10, "wildcards": 1,
                                           "coin": 3000}
    assert pg.star_up_cost(balance, 2) == {"fragments": 25, "wildcards": 3,
                                           "coin": 10000}
    assert pg.star_up_cost(balance, 5) == {"fragments": 150, "wildcards": 30,
                                           "coin": 150000}


def test_taking_a_1_star_character_to_its_cap_costs_35_fragments(db, balance):
    """§15.4 검산: 일반 1★ 캐릭터를 3★ 상한까지 = 조각 35 + 와일드카드 4."""
    total_fragments = (pg.star_up_cost(balance, 1)["fragments"]
                       + pg.star_up_cost(balance, 2)["fragments"])
    total_wildcards = (pg.star_up_cost(balance, 1)["wildcards"]
                       + pg.star_up_cost(balance, 2)["wildcards"])
    assert total_fragments == 35
    assert total_wildcards == 4
    # 1★ 중복 3회는 조각 45(충분)지만 와일드카드는 3개뿐 — 하나 모자라므로
    # 실제로는 중복 4회가 필요하다 [v6.3 정정].
    duplicate = balance.get("duplicate_character_yield")["1"]
    assert duplicate["fragments"] * 3 >= total_fragments
    assert duplicate["wildcards"] * 3 < total_wildcards
    assert duplicate["wildcards"] * 4 >= total_wildcards


def test_star_up_applies_all_three_local_effects_under_one_receipt(
        db, balance, version, user_id, central):
    """§17.1 B-07 — 조각 차감, 와일드카드 차감, 성급 증가가 한 트랜잭션에서."""
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    _give_star_up_materials(db, user_id, "char_terradon", 40, 5)

    result = pg.star_up(db, balance, central, user_id=user_id,
                        character_id="char_terradon", content_version_id=version)

    assert result.status == tx.COMPLETED
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["star_rank"] == 2
    assert db.one("SELECT amount FROM character_fragments WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["amount"] == 30
    assert db.one("SELECT wildcards FROM accounts WHERE user_id = ?",
                  (user_id,))["wildcards"] == 4
    assert central.balance == 1_000_000 - 3000
    # §17.6 — 트랜잭션당 receipt 하나.
    assert db.one("SELECT COUNT(*) AS n FROM fulfillment_receipts")["n"] == 1


def test_a_retried_star_up_never_applies_twice(db, balance, version, user_id,
                                               central):
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    _give_star_up_materials(db, user_id, "char_terradon", 40, 5)

    pg.star_up(db, balance, central, user_id=user_id,
               character_id="char_terradon", content_version_id=version,
               tx_id="fixed")
    tx.run_transaction(db, central, tx_id="fixed",
                       apply_local=pg._apply_star_up, kind="star_up")

    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["star_rank"] == 2
    assert db.one("SELECT amount FROM character_fragments WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["amount"] == 30


def test_a_clamped_coin_deduction_leaves_the_star_rank_alone(db, balance, version,
                                                             user_id):
    """§1.3.8 — 요청과 적용액이 정확히 같지 않으면 로컬에 지급하지 않는다."""
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    _give_star_up_materials(db, user_id, "char_terradon", 40, 5)
    poor = FakeCentral(balance=500)

    result = pg.star_up(db, balance, poor, user_id=user_id,
                        character_id="char_terradon", content_version_id=version)

    assert result.status == tx.COMPENSATED
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["star_rank"] == 1
    assert db.one("SELECT amount FROM character_fragments WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["amount"] == 40
    assert poor.balance == 500      # 부분 차감분은 환불되었다


def test_star_up_is_capped_at_three_for_normal_characters(db, balance, version,
                                                          user_id, central):
    """§4.4 — 일반 상한 3★."""
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 3, ?)", (user_id, utcnow()))
    _give_star_up_materials(db, user_id, "char_terradon", 999, 999)

    with pytest.raises(pg.ProgressionError, match="최대 성급"):
        pg.star_up(db, balance, central, user_id=user_id,
                   character_id="char_terradon", content_version_id=version)


def test_special_cap_characters_reach_six(db, balance, version, user_id):
    assert pg.star_cap(balance, special_cap=False) == 3
    assert pg.star_cap(balance, special_cap=True) == 6


def test_insufficient_fragments_is_rejected_before_any_central_call(
        db, balance, version, user_id, central):
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    _give_star_up_materials(db, user_id, "char_terradon", 2, 5)

    with pytest.raises(pg.ProgressionError, match="재화가 부족"):
        pg.star_up(db, balance, central, user_id=user_id,
                   character_id="char_terradon", content_version_id=version)
    assert central.balance == 1_000_000


# =====================================================================
# §8.4 / §15.8 장비 강화
# =====================================================================
def _give_equipment(db, user_id, tier=0):
    cursor = db.execute(
        "INSERT INTO owned_equipment (user_id, equipment_def_id, tier) "
        "VALUES (?, 'eq_수련검', ?)", (user_id, tier))
    return int(cursor.lastrowid)


def _give_stones(db, user_id, tier, amount):
    db.execute(
        "INSERT INTO enhancement_stones (user_id, tier, amount) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, tier) DO UPDATE SET amount = ?",
        (user_id, tier, amount, amount))


def test_enhancement_costs_match_the_published_table(balance):
    """§15.8 — B(N)은 +2, +3, +4, +5로 자라고 A(N)은 +1, +1, +2, +2, +2."""
    assert pg.enhancement_cost(balance, 1) == {"current_tier_stones": 2,
                                               "previous_tier_stones": 0}
    assert pg.enhancement_cost(balance, 4) == {"current_tier_stones": 6,
                                               "previous_tier_stones": 7}
    assert pg.enhancement_cost(balance, 6) == {"current_tier_stones": 10,
                                               "previous_tier_stones": 16}


def test_from_t4_upward_the_previous_tier_stone_dominates(balance):
    """오너 지시사항: T4부터 이전 티어 강화석이 지배적 비용이 된다."""
    for tier in (4, 5, 6):
        cost = pg.enhancement_cost(balance, tier)
        assert cost["previous_tier_stones"] > cost["current_tier_stones"]


def test_t1_is_exempt_from_the_previous_tier_requirement(db, balance, version,
                                                         user_id):
    """B(1)은 면제 — T0 강화석은 존재하지 않는다."""
    instance = _give_equipment(db, user_id, tier=0)
    _give_stones(db, user_id, 1, 2)

    result = pg.enhance_equipment(db, balance, user_id=user_id,
                                  equipment_instance_id=instance,
                                  content_version_id=version)
    assert result["tier"] == 1
    assert result["spent"] == {1: 2}
    assert db.one("SELECT amount FROM enhancement_stones WHERE user_id = ? "
                  "AND tier = 1", (user_id,))["amount"] == 0


def test_enhancing_consumes_both_tiers_of_stone(db, balance, version, user_id):
    instance = _give_equipment(db, user_id, tier=1)
    _give_stones(db, user_id, 2, 3)
    _give_stones(db, user_id, 1, 2)

    result = pg.enhance_equipment(db, balance, user_id=user_id,
                                  equipment_instance_id=instance,
                                  content_version_id=version)
    assert result["tier"] == 2
    assert db.one("SELECT amount FROM enhancement_stones WHERE user_id = ? "
                  "AND tier = 2", (user_id,))["amount"] == 0
    assert db.one("SELECT amount FROM enhancement_stones WHERE user_id = ? "
                  "AND tier = 1", (user_id,))["amount"] == 0


def test_enhancement_never_fails_and_never_destroys(db, balance, version, user_id):
    """§8.4 — 100% 성공. 파괴, 하락, 보호 아이템 없음."""
    instance = _give_equipment(db, user_id, tier=0)
    for tier in range(1, 7):
        _give_stones(db, user_id, tier, 99)

    for expected in range(1, 7):
        result = pg.enhance_equipment(db, balance, user_id=user_id,
                                      equipment_instance_id=instance,
                                      content_version_id=version)
        assert result["tier"] == expected
    assert db.one("SELECT tier FROM owned_equipment WHERE equipment_instance_id = ?",
                  (instance,))["tier"] == 6


def test_a_full_t0_to_t6_costs_73_stones(db, balance, version, user_id):
    """§15.8 — 한 부위 누적 73개 (제2차 검증 보고서 §5.5에서 독립 검증)."""
    totals: dict[int, int] = {}
    for tier in range(1, 7):
        cost = pg.enhancement_cost(balance, tier)
        totals[tier] = totals.get(tier, 0) + cost["current_tier_stones"]
        if cost["previous_tier_stones"]:
            totals[tier - 1] = (totals.get(tier - 1, 0)
                                + cost["previous_tier_stones"])
    assert totals == {1: 4, 2: 7, 3: 11, 4: 17, 5: 24, 6: 10}
    assert sum(totals.values()) == 73


def test_insufficient_stones_is_rejected(db, balance, version, user_id):
    instance = _give_equipment(db, user_id, tier=0)
    _give_stones(db, user_id, 1, 1)
    with pytest.raises(pg.ProgressionError, match="강화석이 부족"):
        pg.enhance_equipment(db, balance, user_id=user_id,
                             equipment_instance_id=instance,
                             content_version_id=version)


def test_enhancing_needs_no_central_call(db, balance, version, user_id):
    """§8.4 — 강화 실행은 순수 로컬이다."""
    instance = _give_equipment(db, user_id, tier=0)
    _give_stones(db, user_id, 1, 2)
    pg.enhance_equipment(db, balance, user_id=user_id,
                         equipment_instance_id=instance,
                         content_version_id=version)
    # 코인 트랜잭션이 하나도 만들어지지 않았다.
    assert db.one("SELECT COUNT(*) AS n FROM purchase_transactions")["n"] == 0


# =====================================================================
# §9.2 연구
# =====================================================================
def test_research_is_gated_by_its_prerequisite_achievement(db, balance, version,
                                                           user_id, central):
    """§9.1 — 연구는 이제 비용 **더하기** 선행 조건이다."""
    db.execute("UPDATE accounts SET wildcards = 99 WHERE user_id = ?", (user_id,))
    with pytest.raises(pg.ProgressionError, match="선행 업적"):
        pg.unlock_research(db, balance, central, user_id=user_id,
                           node_id="res_파티슬롯3", content_version_id=version)


def test_unlocking_party_slot_three_raises_the_slot_count(db, balance, version,
                                                          user_id, central):
    db.execute("UPDATE accounts SET wildcards = 20, party_slots = 2 "
               "WHERE user_id = ?", (user_id,))
    for index in range(3):
        ach.advance_counter(db, user_id, ach.BOSS_DEFEATED, 1,
                            mutation_id=f"boss:{index}",
                            content_version_id=version)

    result = pg.unlock_research(db, balance, central, user_id=user_id,
                                node_id="res_파티슬롯3",
                                content_version_id=version)
    assert result.status == tx.COMPLETED
    account = db.one("SELECT * FROM accounts WHERE user_id = ?", (user_id,))
    assert account["party_slots"] == 3
    assert account["wildcards"] == 10       # 코인 20,000 + 와일드카드 10
    assert central.balance == 1_000_000 - 20_000


def test_stat_research_is_ungated_and_repeats_ten_times(db, balance, version,
                                                        user_id, central):
    """🟡 R-7 — 스탯 강화는 코인 싱크가 항상 열려 있도록 의도적으로 게이트가 없다."""
    listing = {entry["node_id"]: entry
               for entry in pg.research_status(db, user_id=user_id,
                                               content_version_id=version)}
    assert listing["res_스탯강화"]["required_achievement"] is None
    assert listing["res_스탯강화"]["available"] is True

    for step in range(10):
        pg.unlock_research(db, balance, central, user_id=user_id,
                           node_id="res_스탯강화", content_version_id=version)
    assert db.one("SELECT stat_research_step FROM accounts WHERE user_id = ?",
                  (user_id,))["stat_research_step"] == 10

    with pytest.raises(pg.ProgressionError, match="이미 해금"):
        pg.unlock_research(db, balance, central, user_id=user_id,
                           node_id="res_스탯강화", content_version_id=version)


def test_stat_research_cost_scales_with_the_step(db, balance, version, user_id,
                                                 central):
    """§9.2 — 코인 2,000 × step."""
    pg.unlock_research(db, balance, central, user_id=user_id,
                       node_id="res_스탯강화", content_version_id=version)
    assert central.balance == 1_000_000 - 2_000
    pg.unlock_research(db, balance, central, user_id=user_id,
                       node_id="res_스탯강화", content_version_id=version)
    assert central.balance == 1_000_000 - 2_000 - 4_000


def test_research_status_shows_the_prerequisite_progress_inline(db, balance,
                                                                version, user_id):
    """§20.5 — 잠긴 노드가 단순히 거부하는 대신 스스로를 설명한다."""
    ach.advance_counter(db, user_id, ach.BOSS_DEFEATED, 1, mutation_id="b1",
                        content_version_id=version)
    listing = {entry["node_id"]: entry
               for entry in pg.research_status(db, user_id=user_id,
                                               content_version_id=version)}
    slot_three = listing["res_파티슬롯3"]
    assert slot_three["available"] is False
    assert slot_three["achievement_progress"] == {
        "name": "보스 3회 처치", "current": 1, "target": 3}
    # 「첫 보스 처치」는 달성되었으므로 패시브 슬롯 3은 열려 있다.
    assert listing["res_패시브슬롯3"]["available"] is True


def test_total_research_wildcard_demand_is_26(balance, db, version):
    """§15.4 — 연구 전체 와일드카드 수요 26개."""
    total = db.one(
        "SELECT SUM(wildcard_cost) AS n FROM research_nodes "
        "WHERE content_version_id = ?", (version,))["n"]
    assert total == 26


# =====================================================================
# §7.2 허브 상점
# =====================================================================
def test_stone_prices_follow_the_squared_curve(db, balance, version):
    """§15.4 — 코인 400 × tier² (T1 400 … T6 14,400)."""
    listing = pg.hub_shop_listing(db, balance, content_version_id=version)
    prices = {entry["tier"]: entry["price_coin"] for entry in listing["stones"]}
    assert prices[1] == 400
    assert prices[6] == 14_400


def test_buying_equipment_grants_one_instance_with_a_receipt(db, balance, version,
                                                             user_id, central):
    result = pg.buy_hub_equipment(db, balance, central, user_id=user_id,
                                  equipment_def_id="eq_수련검",
                                  content_version_id=version, tx_id="buy-1")
    assert result.status == tx.COMPLETED
    owned = db.query("SELECT * FROM owned_equipment WHERE user_id = ?", (user_id,))
    assert len(owned) == 1
    assert owned[0]["tier"] == 0
    assert owned[0]["source_tx_id"] == "buy-1"      # §17.6 추적 가능성


def test_a_retried_equipment_purchase_never_grants_a_second_instance(
        db, balance, version, user_id, central):
    """§17.6 — receipt가 없다면 재시도가 두 번째 인스턴스를 지급한다."""
    pg.buy_hub_equipment(db, balance, central, user_id=user_id,
                         equipment_def_id="eq_수련검",
                         content_version_id=version, tx_id="buy-1")
    tx.run_transaction(db, central, tx_id="buy-1",
                       apply_local=pg._apply_hub_equipment, kind="equipment")
    assert db.one("SELECT COUNT(*) AS n FROM owned_equipment WHERE user_id = ?",
                  (user_id,))["n"] == 1


def test_buying_stones_increments_the_balance(db, balance, version, user_id,
                                              central):
    pg.buy_hub_stone(db, balance, central, user_id=user_id, tier=2, amount=3,
                     tx_id="stone-1")
    assert db.one("SELECT amount FROM enhancement_stones WHERE user_id = ? "
                  "AND tier = 2", (user_id,))["amount"] == 3
    assert central.balance == 1_000_000 - (400 * 4 * 3)


def test_an_out_of_range_stone_tier_is_rejected(db, balance, version, user_id,
                                                central):
    with pytest.raises(pg.ProgressionError, match="티어"):
        pg.buy_hub_stone(db, balance, central, user_id=user_id, tier=0)
    with pytest.raises(pg.ProgressionError, match="티어"):
        pg.buy_hub_stone(db, balance, central, user_id=user_id, tier=7)


# =====================================================================
# §8.1 / §8.2 장착과 세트 보너스
# =====================================================================
def test_equipping_replaces_whatever_held_that_slot(db, balance, version, user_id):
    """3종, 캐릭터당 슬롯 1개."""
    first = _give_equipment(db, user_id)
    second = _give_equipment(db, user_id)

    pg.equip(db, user_id=user_id, equipment_instance_id=first,
             character_id=STARTER_CHARACTER_ID, content_version_id=version)
    pg.equip(db, user_id=user_id, equipment_instance_id=second,
             character_id=STARTER_CHARACTER_ID, content_version_id=version)

    equipped = db.query(
        "SELECT * FROM owned_equipment WHERE user_id = ? "
        "AND equipped_character_id IS NOT NULL", (user_id,))
    assert len(equipped) == 1
    assert equipped[0]["equipment_instance_id"] == second


def test_the_set_bonus_is_full_set_only(db, balance, version, user_id):
    """§8.2 — 3슬롯 전부가 같은 이름이어야 하며 2피스 단계는 없다."""
    pieces = []
    for def_id in ("eq_수련검", "eq_수련갑", "eq_수련부적"):
        cursor = db.execute(
            "INSERT INTO owned_equipment (user_id, equipment_def_id, tier) "
            "VALUES (?, ?, 0)", (user_id, def_id))
        pieces.append(int(cursor.lastrowid))

    # 2피스에서는 보너스가 없다.
    for instance in pieces[:2]:
        pg.equip(db, user_id=user_id, equipment_instance_id=instance,
                 character_id=STARTER_CHARACTER_ID, content_version_id=version)
    assert pg.set_bonus_for(db, user_id=user_id,
                            character_id=STARTER_CHARACTER_ID,
                            content_version_id=version) is None

    # 3피스 풀세트에서 발동한다.
    pg.equip(db, user_id=user_id, equipment_instance_id=pieces[2],
             character_id=STARTER_CHARACTER_ID, content_version_id=version)
    assert pg.set_bonus_for(db, user_id=user_id,
                            character_id=STARTER_CHARACTER_ID,
                            content_version_id=version) == {"atk_flat": 3,
                                                            "def_flat": 3}


# =====================================================================
# §16.2.3 — 계정 변경은 진행 중인 런에 영향을 주지 않는다
# =====================================================================
def test_star_up_during_a_run_takes_effect_only_on_the_next_run(db, balance,
                                                                version, user_id,
                                                                central):
    """허브를 잠그는 것은 적대적이므로 스냅샷으로 해결한다 (B-10)."""
    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version)
    _give_star_up_materials(db, user_id, STARTER_CHARACTER_ID, 40, 5)

    pg.star_up(db, balance, central, user_id=user_id,
               character_id=STARTER_CHARACTER_ID, content_version_id=version)

    # 계정은 올라갔지만 진행 중인 런의 스냅샷은 그대로다.
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = ?",
                  (user_id, STARTER_CHARACTER_ID))["star_rank"] == 2
    assert db.one("SELECT star_rank FROM run_build_snapshot WHERE run_id = ?",
                  (run_id,))["star_rank"] == 1


# =====================================================================
# 진행 시스템 리뷰에서 나온 회귀
# =====================================================================
def test_recovery_applies_the_local_effect_of_an_interrupted_transaction(
        db, balance, version, user_id, central):
    """§17.4 — 핸들러 맵 없이 재개하면 코인만 빠지고 로컬 효과가 영구 유실된다."""
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    _give_star_up_materials(db, user_id, "char_terradon", 40, 5)

    # 코인 차감 직후 크래시한 트랜잭션을 재현한다.
    tx.create_transaction(
        db, tx_id="crashed", user_id=user_id, operation="star_up",
        direction=tx.DEDUCT, expected_coin_delta=-3000,
        local_payload={"kind": "star_up", "user_id": user_id,
                       "character_id": "char_terradon", "from_rank": 1,
                       "fragments": 10, "wildcards": 1})
    db.execute(
        "UPDATE purchase_transactions SET status = ?, central_status = ?, "
        "coin_applied_delta = -3000 WHERE tx_id = 'crashed'",
        (tx.COIN_DEDUCTED, tx.APPLIED))

    results = tx.resume_pending(db, central, pg.local_handlers())

    assert [r.status for r in results] == [tx.COMPLETED]
    # 성급이 실제로 올랐다 — 코인만 빠지고 끝나지 않았다.
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["star_rank"] == 2
    assert db.one("SELECT amount FROM character_fragments WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["amount"] == 30


def test_a_failed_star_up_does_not_permanently_block_that_character(
        db, balance, version, user_id):
    """코인 부족은 평범한 결과다. 결정적 tx_id를 그대로 재사용하면 종료 상태
    행 때문에 그 캐릭터의 성급 상승이 영구히 막힌다."""
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    _give_star_up_materials(db, user_id, "char_terradon", 40, 5)

    broke = FakeCentral(balance=0)
    first = pg.star_up(db, balance, broke, user_id=user_id,
                       character_id="char_terradon", content_version_id=version)
    assert first.status == tx.REJECTED_NO_CHARGE
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["star_rank"] == 1

    # 코인이 생긴 뒤 재시도하면 이번에는 성공해야 한다.
    funded = FakeCentral(balance=100_000)
    second = pg.star_up(db, balance, funded, user_id=user_id,
                        character_id="char_terradon", content_version_id=version)
    assert second.status == tx.COMPLETED
    assert second.tx_id != first.tx_id
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["star_rank"] == 2
    assert funded.balance == 100_000 - 3000


def test_a_failed_research_unlock_does_not_permanently_block_that_node(
        db, balance, version, user_id):
    db.execute("UPDATE accounts SET wildcards = 20 WHERE user_id = ?", (user_id,))
    broke = FakeCentral(balance=0)
    assert pg.unlock_research(db, balance, broke, user_id=user_id,
                              node_id="res_스탯강화",
                              content_version_id=version).status \
        == tx.REJECTED_NO_CHARGE

    funded = FakeCentral(balance=100_000)
    assert pg.unlock_research(db, balance, funded, user_id=user_id,
                              node_id="res_스탯강화",
                              content_version_id=version).status == tx.COMPLETED
    assert db.one("SELECT stat_research_step FROM accounts WHERE user_id = ?",
                  (user_id,))["stat_research_step"] == 1


def test_an_in_flight_attempt_keeps_its_key(db, balance, version, user_id):
    """§17.3 규칙 2 — 타임아웃했다고 새 키를 만들지 않는다."""
    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    _give_star_up_materials(db, user_id, "char_terradon", 40, 5)

    class Timeout(FakeCentral):
        def currency_deduct(self, user_id, amount, idempotency_key):
            raise TimeoutError("network")

    first = pg.star_up(db, balance, Timeout(), user_id=user_id,
                       character_id="char_terradon", content_version_id=version)
    assert first.status == tx.COIN_UNKNOWN

    second = pg.star_up(db, balance, FakeCentral(), user_id=user_id,
                        character_id="char_terradon", content_version_id=version)
    # 같은 시도이므로 같은 키로 재개되었다.
    assert second.tx_id == first.tx_id


def test_research_never_drives_wildcards_negative(db, balance, version, user_id,
                                                  central):
    """사전 검사와 적용 사이에 다른 조작이 와일드카드를 소모할 수 있다."""
    db.execute("UPDATE accounts SET wildcards = 10 WHERE user_id = ?", (user_id,))
    for index in range(3):
        ach.advance_counter(db, user_id, ach.BOSS_DEFEATED, 1,
                            mutation_id=f"boss:{index}",
                            content_version_id=version)

    tx.create_transaction(
        db, tx_id="race", user_id=user_id, operation="research",
        direction=tx.DEDUCT, expected_coin_delta=-20000,
        local_payload={"kind": "research", "user_id": user_id,
                       "node_id": "res_파티슬롯3", "step_id": "res_파티슬롯3",
                       "wildcards": 10,
                       "effect_json": '{"kind": "party_slot", "value": 3}'})
    # 그 사이 성급 상승이 와일드카드를 다 써 버렸다.
    db.execute("UPDATE accounts SET wildcards = 0 WHERE user_id = ?", (user_id,))

    with pytest.raises(pg.ProgressionError, match="와일드카드가 부족"):
        tx.run_transaction(db, central, tx_id="race",
                           apply_local=pg._apply_research, kind="research")
    assert db.one("SELECT wildcards FROM accounts WHERE user_id = ?",
                  (user_id,))["wildcards"] == 0


def test_a_set_less_piece_does_not_complete_a_set(db, balance, version, user_id):
    """§8.2 — 두 세트 이름을 섞으면 보너스가 없고, 무세트 조각도 마찬가지다."""
    db.execute(
        "INSERT INTO equipment_defs (content_version_id, equipment_def_id, name, "
        "slot, set_name, hp_flat, atk_flat, def_flat, spd_flat, price_coin) "
        "VALUES (?, 'eq_무명부적', '무명 부적', '악세서리', NULL, 0, 0, 0, 0, 100)",
        (version,))

    pieces = []
    for def_id in ("eq_수련검", "eq_수련갑", "eq_무명부적"):
        cursor = db.execute(
            "INSERT INTO owned_equipment (user_id, equipment_def_id, tier) "
            "VALUES (?, ?, 0)", (user_id, def_id))
        pieces.append(int(cursor.lastrowid))
    for instance in pieces:
        pg.equip(db, user_id=user_id, equipment_instance_id=instance,
                 character_id=STARTER_CHARACTER_ID, content_version_id=version)

    # 3슬롯이 다 찼지만 하나가 무세트이므로 풀세트가 아니다.
    assert pg.set_bonus_for(db, user_id=user_id,
                            character_id=STARTER_CHARACTER_ID,
                            content_version_id=version) is None


def test_research_step_counting_is_exact_not_a_like_prefix(db, balance, version,
                                                           user_id, central):
    """`node_id LIKE '<id>%'`는 SQLite에서 `_`가 와일드카드라 오작동하고, 한
    노드 id가 다른 것의 접두사이면 단계를 잘못 센다."""
    db.execute(
        "INSERT INTO research_nodes (content_version_id, node_id, name, coin_cost, "
        "wildcard_cost, required_achievement, effect_json, max_steps) "
        "VALUES (?, 'res_스탯강화_확장', '스탯 강화 확장', 1000, 0, NULL, ?, 1)",
        (version, '{"kind": "stat_step"}'))
    db.execute(
        "INSERT INTO research_unlocks (user_id, node_id, unlocked_at) "
        "VALUES (?, 'res_스탯강화_확장', ?)", (user_id, utcnow()))

    # 접두사가 겹치는 다른 노드의 해금이 스탯 강화 단계로 잘못 세어지면 안 된다.
    listing = {entry["node_id"]: entry
               for entry in pg.research_status(db, user_id=user_id,
                                               content_version_id=version)}
    assert listing["res_스탯강화"]["steps_taken"] == 0
    assert listing["res_스탯강화"]["coin_cost"] == 2000
