"""§1.2/§1.3 Central Bot contract, §16.4 RNG journal, §16.7 CAS, §17 transactions."""

from __future__ import annotations

import pytest

from app.api import custom_id as cid
from app.api import events as ev
from app.central import delivery
from app.central import transactions as tx
from app.central.client import (CapabilityError, CentralError, CurrencyResult,
                                REQUIRED_CAPABILITIES, validate_multi_action,
                                validate_png_attachment, verify_capabilities)
from app.content.seed import STARTER_CHARACTER_ID, TUTORIAL_WORLD_ID
from app.db.connection import utcnow
from app.engine import achievements as ach
from app.engine import lifecycle as lc
from app.engine.rng import JournaledRng


@pytest.fixture
def run_id(db, balance, version, user_id) -> int:
    return lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                           party_character_ids=[STARTER_CHARACTER_ID],
                           is_tutorial=True),
        version,
    )


# =====================================================================
# §1.3.4 capability discovery — fail closed
# =====================================================================
def test_a_fully_compliant_central_bot_passes():
    """v6.2 invented four field names that do not exist in the contract, so a
    literal implementation refused to start against a compliant Central Bot."""
    assert verify_capabilities(dict(REQUIRED_CAPABILITIES))


def test_the_invented_v6_2_field_names_are_not_required():
    for invented in ("string_select", "private_thread_api", "durable_message_edit",
                     "attachments_per_action"):
        assert invented not in REQUIRED_CAPABILITIES


def test_a_missing_or_false_flag_is_a_hard_startup_failure():
    payload = dict(REQUIRED_CAPABILITIES)
    del payload["supports_thread_recreation"]
    with pytest.raises(CapabilityError, match="supports_thread_recreation"):
        verify_capabilities(payload)

    payload = dict(REQUIRED_CAPABILITIES)
    payload["supports_out_of_band_message_edit"] = False
    with pytest.raises(CapabilityError):
        verify_capabilities(payload)


def test_contract_versions_are_checked_exactly():
    payload = dict(REQUIRED_CAPABILITIES)
    payload["component_contract_version"] = "2.1"
    with pytest.raises(CapabilityError):
        verify_capabilities(payload)


# =====================================================================
# §1.3.7 attachment and multi_action rules
# =====================================================================
def test_png_limits_are_enforced_when_composing():
    validate_png_attachment(data_b64="aGk=", filename="battle.png", width=720,
                            height=360, decoded_size=1000)
    with pytest.raises(CentralError, match="data: URI"):
        validate_png_attachment(data_b64="data:image/png;base64,aGk=",
                                filename="a.png", width=1, height=1, decoded_size=1)
    with pytest.raises(CentralError, match="path component"):
        validate_png_attachment(data_b64="aGk=", filename="../a.png", width=1,
                                height=1, decoded_size=1)
    with pytest.raises(CentralError, match="4096"):
        validate_png_attachment(data_b64="aGk=", filename="a.png", width=5000,
                                height=1, decoded_size=1)


def test_multi_action_requires_edit_first_and_distinct_child_request_ids():
    """The parent's request_id is batch-level and must never map panel IDs."""
    with pytest.raises(CentralError, match="first multi_action child"):
        validate_multi_action([{"action": "post_channel_message",
                                "metadata": {"request_id": "a"}}])
    with pytest.raises(CentralError, match="distinct"):
        validate_multi_action([
            {"action": "edit", "metadata": {"request_id": "a"}},
            {"action": "post_channel_message", "metadata": {"request_id": "a"}},
        ])
    validate_multi_action([
        {"action": "edit", "metadata": {"request_id": "a"}},
        {"action": "post_channel_message", "metadata": {"request_id": "b"}},
    ])


# =====================================================================
# §1.2 per-type event parsing
# =====================================================================
def test_delivery_result_carries_no_user_id():
    """A strict common model would reject valid callbacks."""
    parsed = ev.parse_event({
        "type": "message_delivery_result", "request_id": "r1", "action": "edit",
        "success": True, "partial": False, "message_id": 5, "thread_id": 9,
    })
    assert isinstance(parsed, ev.DeliveryResultEvent)
    assert parsed.message_id == 5
    assert not hasattr(parsed, "user_id")


def test_thread_deleted_carries_neither_user_id_nor_channel_id():
    parsed = ev.parse_event({
        "type": "thread_deleted", "thread_id": 11, "parent_channel_id": 3,
        "logical_session_id": "dko-run-1", "surface_generation": 2,
        "source": "external",
    })
    assert isinstance(parsed, ev.ThreadDeletedEvent)
    assert parsed.logical_session_id == "dko-run-1"


def test_interaction_values_are_passed_through_unmodified():
    """Central performs no truncation, sorting, dedupe, normalization or
    coercion on values[] (guide §7.2)."""
    parsed = ev.parse_event({
        "type": "interaction", "user_id": 1, "custom_id": "dko:cs:1:1:0:x",
        "values": ["9", "3", "3"],
    })
    assert parsed.values == ["9", "3", "3"]


def test_unsupported_event_types_are_rejected():
    with pytest.raises(ev.EventParseError):
        ev.parse_event({"type": "update"})


# =====================================================================
# §19.2 custom_id
# =====================================================================
def test_custom_id_roundtrips_and_stays_short():
    encoded = cid.build(cid.ACTION_CARD_SELECT, run_id=1234567, generation=2,
                        revision=48, payload="7")
    assert encoded.startswith("dko:")
    assert len(encoded) < 100        # well under Discord's limit
    parsed = cid.parse(encoded)
    assert (parsed.action, parsed.run_id, parsed.generation, parsed.revision,
            parsed.payload) == (cid.ACTION_CARD_SELECT, 1234567, 2, 48, "7")


def test_the_mod_namespace_is_rejected():
    """`mod:` is reserved for Central moderation (guide §8.8)."""
    with pytest.raises(cid.CustomIdError, match="namespace"):
        cid.parse("mod:cs:1:1:0:x")


def test_unknown_action_codes_are_rejected():
    with pytest.raises(cid.CustomIdError):
        cid.parse("dko:zz:1:1:0:x")


# =====================================================================
# §16.4 RNG operation journal
# =====================================================================
def test_a_replay_returns_the_original_result(db, run_id):
    """v6.1's claim that a persisted counter reproduces the roll is false: a
    crash after the counter advanced makes a replay read the NEXT value."""
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    rng = JournaledRng(db, run_id, run["rng_seed"])

    first = rng.randint("node:3:encounter", 0, 1000)
    # Simulate the crash-and-retry: a fresh instance, same key.
    replay = JournaledRng(db, run_id, run["rng_seed"])
    assert replay.randint("node:3:encounter", 0, 1000) == first


def test_different_keys_draw_independently(db, run_id):
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    rng = JournaledRng(db, run_id, run["rng_seed"])
    values = {rng.randint(f"node:{i}:encounter", 0, 10**9) for i in range(20)}
    assert len(values) > 15      # not aliasing onto one another


def test_the_counter_advances_once_per_journaled_draw(db, run_id):
    run = db.one("SELECT rng_seed FROM runs WHERE run_id = ?", (run_id,))
    rng = JournaledRng(db, run_id, run["rng_seed"])
    before = db.one("SELECT rng_counter FROM runs WHERE run_id = ?",
                    (run_id,))["rng_counter"]
    rng.randint("a", 0, 5)
    rng.randint("a", 0, 5)      # replay — no advance
    rng.randint("b", 0, 5)
    after = db.one("SELECT rng_counter FROM runs WHERE run_id = ?",
                   (run_id,))["rng_counter"]
    assert after - before == 2


# =====================================================================
# §16.7 compare-and-swap and duplicate events
# =====================================================================
def test_only_one_of_two_concurrent_clicks_wins_the_cas(db, run_id):
    """Gates 1-4 are individually correct but not atomic: two simultaneous
    clicks can both read revision R and both pass validation."""
    revision = db.one("SELECT presentation_revision FROM runs WHERE run_id = ?",
                      (run_id,))["presentation_revision"]

    assert lc.claim_mutation(db, run_id, revision) == revision + 1
    with pytest.raises(lc.StaleRevisionError):
        lc.claim_mutation(db, run_id, revision)


def test_a_redelivered_event_is_a_no_op(db, run_id):
    assert lc.record_event(db, "evt-1", run_id) is True
    assert lc.record_event(db, "evt-1", run_id) is False


def test_one_active_run_per_user_is_enforced_globally(db, balance, version, user_id):
    lc.create_run(db, balance,
                  lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                                     party_character_ids=[STARTER_CHARACTER_ID],
                                     is_tutorial=True),
                  version)
    with pytest.raises(lc.LifecycleError, match="이미 진행 중인 런"):
        lc.create_run(db, balance,
                      lc.RunBuildRequest(user_id=user_id, world_id=TUTORIAL_WORLD_ID,
                                         party_character_ids=[STARTER_CHARACTER_ID],
                                         is_tutorial=True),
                      version)


def test_the_main_campaign_requires_at_least_two_characters(db, balance, version,
                                                            user_id):
    db.execute("INSERT OR IGNORE INTO world_unlocks (user_id, world_id, unlocked_at) "
               "VALUES (?, 'world_1', ?)", (user_id, utcnow()))
    with pytest.raises(lc.LifecycleError, match="파티원 2명"):
        lc.create_run(db, balance,
                      lc.RunBuildRequest(user_id=user_id, world_id="world_1",
                                         party_character_ids=[STARTER_CHARACTER_ID],
                                         is_tutorial=False),
                      version)


def test_illegal_state_transitions_are_rejected(db, run_id):
    with pytest.raises(lc.LifecycleError, match="illegal transition"):
        lc.transition(db, run_id, lc.RUN_COMPLETED)


def test_boss_victory_always_ends_the_run(db):
    """§3.6 — v6.2's "if a further world exists" branch is deleted."""
    assert lc.RUN_SETTLEMENT in lc.TRANSITIONS[lc.BOSS_BATTLE]
    assert lc.POST_BATTLE not in lc.TRANSITIONS[lc.BOSS_BATTLE]


def test_the_shop_is_a_self_loop(db):
    """§16.2 — a shop visit is a loop, not a single choice."""
    assert lc.SHOP in lc.TRANSITIONS[lc.SHOP]


# =====================================================================
# §16.2.3 the account build snapshot (B-10)
# =====================================================================
def test_account_mutations_during_a_run_do_not_change_the_run(db, run_id, user_id):
    """Hub commands stay available during a run, so the live rows can move —
    the run reads the snapshot instead."""
    snapshot = db.one("SELECT * FROM run_build_snapshot WHERE run_id = ?", (run_id,))
    assert snapshot["star_rank"] == 1

    db.execute("UPDATE owned_characters SET star_rank = 3 WHERE user_id = ?",
               (user_id,))
    db.execute("UPDATE accounts SET stat_research_step = 10 WHERE user_id = ?",
               (user_id,))

    unchanged = db.one("SELECT * FROM run_build_snapshot WHERE run_id = ?", (run_id,))
    assert unchanged["star_rank"] == 1
    assert unchanged["research_stat_step"] == 0


def test_the_run_pins_a_content_version_that_never_changes(db, run_id, version):
    run = db.one("SELECT content_version_id FROM runs WHERE run_id = ?", (run_id,))
    assert run["content_version_id"] == version


# =====================================================================
# §16.8 thread_deleted
# =====================================================================
def test_thread_deletion_does_not_terminate_or_defeat_the_run(db, run_id):
    db.execute("UPDATE runs SET thread_id = 99, state = ? WHERE run_id = ?",
               (lc.MAP_NAVIGATION, run_id))
    result = lc.handle_thread_deleted(db, f"dko-run-{run_id}")
    assert result["handled"] and result["state"] == lc.MAP_NAVIGATION

    run = db.one("SELECT state, thread_id FROM runs WHERE run_id = ?", (run_id,))
    assert run["state"] == lc.MAP_NAVIGATION
    assert run["thread_id"] is None


def test_surface_generation_only_ever_increments(db, run_id):
    assert lc.recreate_surface(db, run_id) == 2
    assert lc.recreate_surface(db, run_id) == 3


# =====================================================================
# §1.3.3 delivery intents (B-13)
# =====================================================================
def test_the_intent_supplies_the_metadata_the_callback_omits(db, run_id):
    """The callback carries neither surface_generation nor
    presentation_revision, so Deckout persists its own intent BEFORE sending."""
    request_id = delivery.mint_request_id("canonical")
    delivery.record_intent(db, request_id=request_id, run_id=run_id,
                           purpose="canonical", surface_generation=1,
                           presentation_revision=0)

    result = delivery.handle_delivery_result(db, {
        "type": "message_delivery_result", "request_id": request_id,
        "action": "reply", "success": True, "partial": False,
        "message_id": 555, "thread_id": 777,
    })
    assert result["applied"]

    binding = db.one("SELECT * FROM discord_bindings WHERE request_id = ?",
                     (request_id,))
    assert binding["surface_generation"] == 1
    assert binding["presentation_revision"] == 0
    run = db.one("SELECT thread_id, canonical_message_id FROM runs WHERE run_id = ?",
                 (run_id,))
    assert run["thread_id"] == 777 and run["canonical_message_id"] == 555


def test_a_repeated_callback_is_a_no_op(db, run_id):
    request_id = delivery.mint_request_id("canonical")
    delivery.record_intent(db, request_id=request_id, run_id=run_id,
                           purpose="canonical", surface_generation=1,
                           presentation_revision=0)
    payload = {"request_id": request_id, "action": "reply", "success": True,
               "partial": False, "message_id": 1, "thread_id": 2}
    delivery.handle_delivery_result(db, payload)
    second = delivery.handle_delivery_result(db, payload)
    assert second["idempotent"]


def test_a_stale_generation_callback_is_recorded_and_ignored(db, run_id):
    request_id = delivery.mint_request_id("canonical")
    delivery.record_intent(db, request_id=request_id, run_id=run_id,
                           purpose="canonical", surface_generation=1,
                           presentation_revision=0)
    lc.recreate_surface(db, run_id)      # generation is now 2

    result = delivery.handle_delivery_result(db, {
        "request_id": request_id, "action": "reply", "success": True,
        "partial": False, "message_id": 1, "thread_id": 2,
    })
    assert result["stale"] and not result["applied"]
    # Recorded, never applied to a live binding.
    assert db.one("SELECT * FROM discord_bindings WHERE request_id = ?",
                  (request_id,)) is not None
    assert db.one("SELECT thread_id FROM runs WHERE run_id = ?",
                  (run_id,))["thread_id"] is None


def test_an_unknown_request_id_is_logged_and_dropped(db):
    result = delivery.handle_delivery_result(db, {"request_id": "nope",
                                                  "success": True})
    assert not result["handled"]


# =====================================================================
# §16.6 ordering
# =====================================================================
def test_a_stale_render_job_is_discarded_not_sent(db, run_id):
    delivery.enqueue_frame(db, run_id=run_id, target_revision=0,
                           delivery_request_id=delivery.frame_request_id(run_id, 1, 0),
                           payload={})
    delivery.enqueue_frame(db, run_id=run_id, target_revision=1,
                           delivery_request_id=delivery.frame_request_id(run_id, 1, 1),
                           payload={})
    lc.claim_mutation(db, run_id, 0)      # revision is now 1

    frame = delivery.next_frame(db, run_id)
    assert frame["target_revision"] == 1
    discarded = db.one("SELECT status FROM delivery_queue WHERE target_revision = 0")
    assert discarded["status"] == "discarded"


def test_a_frame_request_id_is_stable_per_revision(db, run_id):
    """So a retry returns already_applied instead of double-editing."""
    request_id = delivery.frame_request_id(run_id, 1, 4)
    assert delivery.enqueue_frame(db, run_id=run_id, target_revision=4,
                                  delivery_request_id=request_id, payload={})
    assert delivery.enqueue_frame(db, run_id=run_id, target_revision=4,
                                  delivery_request_id=request_id, payload={}) is None


# =====================================================================
# §17 transactions
# =====================================================================
class FakeCentral:
    """A Central Bot stub that clamps deductions the way §1.3.8 describes."""

    def __init__(self, balance: int = 10_000):
        self.balance = balance
        self.calls: list[tuple[str, int, str]] = []
        self.applied_keys: dict[str, int] = {}

    def currency_add(self, user_id, amount, idempotency_key):
        self.calls.append(("add", amount, idempotency_key))
        if idempotency_key in self.applied_keys:
            # Central replays the prior result for a repeated key.
            return CurrencyResult(requested=amount,
                                  applied=self.applied_keys[idempotency_key])
        self.balance += amount
        self.applied_keys[idempotency_key] = amount
        return CurrencyResult(requested=amount, applied=amount)

    def currency_deduct(self, user_id, amount, idempotency_key):
        self.calls.append(("deduct", amount, idempotency_key))
        if idempotency_key in self.applied_keys:
            return CurrencyResult(requested=-abs(amount),
                                  applied=self.applied_keys[idempotency_key])
        # An over-large deduction clamps to 0 and returns HTTP 200.
        applied = -min(abs(amount), self.balance)
        self.balance += applied
        self.applied_keys[idempotency_key] = applied
        return CurrencyResult(requested=-abs(amount), applied=applied)


def test_a_matching_deduction_applies_the_local_grant_once(db, user_id):
    central = FakeCentral(balance=10_000)
    granted: list[dict] = []

    tx.create_transaction(db, tx_id="t1", user_id=user_id, operation="hub_purchase",
                          direction=tx.DEDUCT, expected_coin_delta=-3000,
                          local_payload={"equipment_def_id": "eq_수련검"})
    result = tx.run_transaction(db, central, tx_id="t1",
                                apply_local=lambda _db, payload: granted.append(payload),
                                kind="equipment")
    assert result.status == tx.COMPLETED
    assert len(granted) == 1

    # A retry must not grant a second instance — §17.6.
    tx.run_transaction(db, central, tx_id="t1",
                       apply_local=lambda _db, payload: granted.append(payload),
                       kind="equipment")
    assert len(granted) == 1


def test_a_clamped_deduction_never_grants_and_is_compensated(db, user_id):
    """§1.3.8 — an over-large deduction returns HTTP 200 with a smaller applied
    delta. Not exactly equal → DO NOT grant locally."""
    central = FakeCentral(balance=500)
    granted: list[dict] = []

    tx.create_transaction(db, tx_id="t2", user_id=user_id, operation="hub_purchase",
                          direction=tx.DEDUCT, expected_coin_delta=-3000,
                          local_payload={})
    result = tx.run_transaction(db, central, tx_id="t2",
                                apply_local=lambda _db, payload: granted.append(payload))
    assert result.status == tx.COMPENSATED
    assert granted == []
    # The refund used a SEPARATE stable compensation key.
    refund_calls = [call for call in central.calls if call[0] == "add"]
    assert refund_calls and refund_calls[0][2] == "deckout:refund:t2"
    assert central.balance == 500


def test_a_zero_applied_deduction_is_rejected_with_no_charge(db, user_id):
    central = FakeCentral(balance=0)
    tx.create_transaction(db, tx_id="t3", user_id=user_id, operation="research",
                          direction=tx.DEDUCT, expected_coin_delta=-20000)
    result = tx.run_transaction(db, central, tx_id="t3")
    assert result.status == tx.REJECTED_NO_CHARGE


def test_an_unclear_outcome_never_refunds_speculatively(db, user_id):
    """§17.3 rule 6 — re-issue the SAME key; Central replays its prior result."""
    class Timeout(FakeCentral):
        def __init__(self):
            super().__init__()
            self.fail = True

        def currency_deduct(self, user_id, amount, idempotency_key):
            if self.fail:
                self.fail = False
                raise TimeoutError("network")
            return super().currency_deduct(user_id, amount, idempotency_key)

    central = Timeout()
    tx.create_transaction(db, tx_id="t4", user_id=user_id, operation="hub_purchase",
                          direction=tx.DEDUCT, expected_coin_delta=-1000)
    first = tx.run_transaction(db, central, tx_id="t4")
    assert first.status == tx.COIN_UNKNOWN
    assert not [call for call in central.calls if call[0] == "add"]   # no refund

    second = tx.run_transaction(db, central, tx_id="t4",
                                apply_local=lambda _db, _p: None)
    assert second.status == tx.COMPLETED
    # The same key throughout — never a fresh one because a request timed out.
    keys = {call[2] for call in central.calls if call[0] == "deduct"}
    assert keys == {"deckout:hub_purchase:t4"}


def test_a_grant_with_no_local_entitlement_completes(db, user_id):
    """A run-clear reward may have no local entitlement to apply at all, and
    must never be refunded."""
    central = FakeCentral()
    tx.create_transaction(db, tx_id="t5", user_id=user_id, operation="run_clear",
                          direction=tx.GRANT, expected_coin_delta=2250,
                          local_required=False)
    result = tx.run_transaction(db, central, tx_id="t5")
    assert result.status == tx.COMPLETED
    assert result.local_status == tx.NOT_REQUIRED
    assert not [call for call in central.calls if call[0] == "deduct"]


def test_a_partial_positive_grant_requires_an_operator(db, user_id):
    """§17.3 rule 8 — an anomaly. Do not auto-resolve."""
    class PartialGrant(FakeCentral):
        def currency_add(self, user_id, amount, idempotency_key):
            return CurrencyResult(requested=amount, applied=amount // 2)

    tx.create_transaction(db, tx_id="t6", user_id=user_id, operation="run_clear",
                          direction=tx.GRANT, expected_coin_delta=2000,
                          local_required=False)
    result = tx.run_transaction(db, PartialGrant(), tx_id="t6")
    assert result.status == tx.OPERATOR_REQUIRED


def test_recovery_resumes_non_terminal_transactions(db, user_id):
    """§17.4 — resume from the recorded status, never mint fresh keys."""
    central = FakeCentral()
    tx.create_transaction(db, tx_id="t7", user_id=user_id, operation="run_clear",
                          direction=tx.GRANT, expected_coin_delta=1200,
                          local_required=False)
    results = tx.resume_pending(db, central)
    assert [r.status for r in results] == [tx.COMPLETED]
    assert tx.resume_pending(db, central) == []


# =====================================================================
# §20.2 achievement idempotency (C-06)
# =====================================================================
def test_a_repeated_increment_is_absorbed_by_its_receipt(db, version, user_id):
    """processed_events is run-scoped and does not cover non-run mutations such
    as star-up or equipment tier-up."""
    for _ in range(3):
        ach.advance_by_id(db, user_id, "ach_첫보스처치", 1,
                          mutation_id="starup:tx-1", content_version_id=version)
    progress = db.one("SELECT current_value FROM achievement_progress "
                      "WHERE user_id = ? AND achievement_id = 'ach_첫보스처치'",
                      (user_id,))
    assert progress["current_value"] == 1


def test_completion_grants_its_carta_exactly_once(db, version, user_id):
    before = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                    (user_id,))["carta"]
    ach.advance_counter(db, user_id, ach.BOSS_DEFEATED, 1,
                        mutation_id="settle:1", content_version_id=version)
    ach.advance_counter(db, user_id, ach.BOSS_DEFEATED, 1,
                        mutation_id="settle:2", content_version_id=version)
    after = db.one("SELECT carta FROM accounts WHERE user_id = ?",
                   (user_id,))["carta"]
    # 「첫 보스 처치」 pays 100 once; the 3-kill and 10-kill rungs are not yet met.
    assert after - before == 100


def test_the_ladder_shares_one_counter(db, version, user_id):
    for index in range(3):
        ach.advance_counter(db, user_id, ach.BOSS_DEFEATED, 1,
                            mutation_id=f"settle:{index}",
                            content_version_id=version)
    assert ach.is_completed(db, user_id, "ach_첫보스처치")
    assert ach.is_completed(db, user_id, "ach_보스3회처치")
    assert not ach.is_completed(db, user_id, "ach_보스10회처치")
