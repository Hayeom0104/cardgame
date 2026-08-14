"""§17 — the Central ↔ local transaction contract.

Any operation changing **both** a Central coin balance and local data needs a
durable transaction. The state machine is direction-aware (M-11): v6.1's single
`created → coin_deducted → local_applied → completed` path cannot describe a
positive grant or a refund, and a run-clear reward may have no local
entitlement to apply at all.

The rule that catches the most bugs: **never mint a new key because a request
timed out.** The same business event always reuses the same key, and on
`coin_unknown` the same key is re-issued so Central replays its prior result.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Callable

from app.central.client import CentralClient, CurrencyResult
from app.db.connection import Database, utcnow

logger = logging.getLogger(__name__)

# directions
DEDUCT = "deduct"
GRANT = "grant"
REFUND = "refund"

# central_status
PENDING = "pending"
UNKNOWN = "unknown"
APPLIED = "applied"
REJECTED = "rejected"
PARTIAL = "partial"

# local_status
NOT_REQUIRED = "not_required"
LOCAL_PENDING = "pending"
LOCAL_APPLIED = "applied"

# status (the row's overall position in §17.2)
CREATED = "created"
COIN_DEDUCTED = "coin_deducted"
COIN_GRANTED = "coin_granted"
COIN_REFUNDED = "coin_refunded"
COIN_UNKNOWN = "coin_unknown"
COMPENSATION_PENDING = "compensation_pending"
COMPENSATED = "compensated"
COMPLETED = "completed"
REJECTED_NO_CHARGE = "rejected_no_charge"
OPERATOR_REQUIRED = "operator_required"

TERMINAL_STATUSES = frozenset({
    COMPLETED, REJECTED_NO_CHARGE, COMPENSATED, OPERATOR_REQUIRED,
})


class TransactionError(RuntimeError):
    pass


def coin_key(operation: str, tx_id: str) -> str:
    """§17.3 key namespace `deckout:{operation}:{tx_id}`."""
    return f"deckout:{operation}:{tx_id}"


def refund_key(tx_id: str) -> str:
    """Compensation uses a SEPARATE stable key."""
    return f"deckout:refund:{tx_id}"


@dataclass
class TransactionResult:
    tx_id: str
    status: str
    central_status: str
    local_status: str
    applied_delta: int | None = None
    message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in (COMPLETED,)


def create_transaction(db: Database, *, tx_id: str, user_id: int, operation: str,
                       direction: str, expected_coin_delta: int,
                       local_payload: dict | None = None,
                       local_required: bool = True) -> str:
    """§17.3 rule 1 — CREATE the local transaction row BEFORE calling Central."""
    db.execute(
        "INSERT OR IGNORE INTO purchase_transactions (tx_id, user_id, operation, "
        "direction, central_status, local_status, expected_coin_delta, "
        "coin_idempotency_key, refund_idempotency_key, local_payload, status, "
        "attempts, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (tx_id, user_id, operation, direction, PENDING,
         LOCAL_PENDING if local_required else NOT_REQUIRED,
         expected_coin_delta, coin_key(operation, tx_id), refund_key(tx_id),
         json.dumps(local_payload or {}, ensure_ascii=False), CREATED,
         utcnow(), utcnow()),
    )
    return tx_id


def _update(db: Database, tx_id: str, **fields) -> None:
    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    db.execute(
        f"UPDATE purchase_transactions SET {assignments} WHERE tx_id = ?",
        (*fields.values(), tx_id),
    )


def run_transaction(db: Database, central: CentralClient, *, tx_id: str,
                    apply_local: Callable[[Database, dict], None] | None = None,
                    kind: str = "generic") -> TransactionResult:
    """Drive one transaction row to a terminal status.

    Safe to call repeatedly: it resumes from the recorded status, which is
    exactly what §17.4 recovery does on startup and on `/shutdown`.
    """
    row = db.one("SELECT * FROM purchase_transactions WHERE tx_id = ?", (tx_id,))
    if row is None:
        raise TransactionError(f"transaction {tx_id!r} does not exist")
    if row["status"] in TERMINAL_STATUSES:
        return _result(row)

    direction = row["direction"]
    if direction == DEDUCT:
        return _run_deduct(db, central, row, apply_local, kind)
    if direction == GRANT:
        return _run_grant(db, central, row, apply_local, kind)
    if direction == REFUND:
        return _run_refund(db, central, row)
    raise TransactionError(f"unknown direction {direction!r}")


# =====================================================================
# DEDUCT
# =====================================================================
def _run_deduct(db, central, row, apply_local, kind) -> TransactionResult:
    tx_id = row["tx_id"]
    amount = abs(int(row["expected_coin_delta"]))

    if row["status"] in (CREATED, COIN_UNKNOWN):
        # §17.3 rule 6: on coin_unknown NEVER refund speculatively — re-issue
        # the SAME key and let Central replay its prior result.
        try:
            result = central.currency_deduct(row["user_id"], amount,
                                             row["coin_idempotency_key"])
        except Exception as error:
            logger.warning("deduct %s outcome unclear: %s", tx_id, error)
            _update(db, tx_id, status=COIN_UNKNOWN, central_status=UNKNOWN,
                    attempts=row["attempts"] + 1)
            return _reload(db, tx_id)

        # §17.3 rule 3 — VALIDATE THE APPLIED DELTA.
        if result.applied != -amount:
            if result.applied == 0:
                _update(db, tx_id, status=REJECTED_NO_CHARGE, central_status=REJECTED,
                        coin_applied_delta=0, local_status=NOT_REQUIRED)
                return _reload(db, tx_id)
            # A partial deduction was taken: do NOT grant locally, compensate.
            _update(db, tx_id, status=COMPENSATION_PENDING, central_status=PARTIAL,
                    coin_applied_delta=result.applied)
            return _compensate(db, central, tx_id)

        _update(db, tx_id, status=COIN_DEDUCTED, central_status=APPLIED,
                coin_applied_delta=result.applied)
        row = db.one("SELECT * FROM purchase_transactions WHERE tx_id = ?", (tx_id,))

    if row["status"] == COMPENSATION_PENDING:
        return _compensate(db, central, tx_id)

    if row["status"] == COIN_DEDUCTED:
        _apply_local_guarded(db, row, apply_local, kind)
        _update(db, tx_id, status=COMPLETED, local_status=LOCAL_APPLIED)

    return _reload(db, tx_id)


def _compensate(db, central, tx_id: str) -> TransactionResult:
    """Refund the partial deduction with a SEPARATE stable compensation key."""
    row = db.one("SELECT * FROM purchase_transactions WHERE tx_id = ?", (tx_id,))
    taken = abs(int(row["coin_applied_delta"] or 0))
    if taken == 0:
        _update(db, tx_id, status=COMPENSATED, local_status=NOT_REQUIRED)
        return _reload(db, tx_id)
    try:
        central.currency_add(row["user_id"], taken, row["refund_idempotency_key"])
    except Exception as error:
        logger.warning("compensation for %s unclear: %s", tx_id, error)
        _update(db, tx_id, status=COIN_UNKNOWN, central_status=UNKNOWN)
        return _reload(db, tx_id)
    _update(db, tx_id, status=COMPENSATED, local_status=NOT_REQUIRED)
    return _reload(db, tx_id)


# =====================================================================
# GRANT
# =====================================================================
def _run_grant(db, central, row, apply_local, kind) -> TransactionResult:
    tx_id = row["tx_id"]
    amount = abs(int(row["expected_coin_delta"]))

    if row["status"] in (CREATED, COIN_UNKNOWN):
        try:
            result: CurrencyResult = central.currency_add(
                row["user_id"], amount, row["coin_idempotency_key"])
        except Exception as error:
            logger.warning("grant %s outcome unclear: %s", tx_id, error)
            _update(db, tx_id, status=COIN_UNKNOWN, central_status=UNKNOWN,
                    attempts=row["attempts"] + 1)
            return _reload(db, tx_id)

        if result.applied != amount:
            # §17.3 rule 8 — a partial positive grant is an anomaly. Do not
            # auto-resolve. The same holds for an applied delta of 0 or one
            # larger than requested: §1.3.8's applied-delta validation is
            # "not exactly equal → DO NOT grant locally", and a grant is the
            # one direction where guessing would hand out an unpaid reward.
            _update(db, tx_id, status=OPERATOR_REQUIRED, central_status=PARTIAL,
                    coin_applied_delta=result.applied)
            return _reload(db, tx_id)

        _update(db, tx_id, status=COIN_GRANTED, central_status=APPLIED,
                coin_applied_delta=result.applied)
        row = db.one("SELECT * FROM purchase_transactions WHERE tx_id = ?", (tx_id,))

    if row["status"] == COIN_GRANTED:
        # A grant with local_status = not_required is already complete and must
        # never be refunded (§17.2 recovery note).
        if row["local_status"] != NOT_REQUIRED:
            _apply_local_guarded(db, row, apply_local, kind)
            _update(db, tx_id, local_status=LOCAL_APPLIED)
        _update(db, tx_id, status=COMPLETED)

    return _reload(db, tx_id)


# =====================================================================
# REFUND
# =====================================================================
def _run_refund(db, central, row) -> TransactionResult:
    tx_id = row["tx_id"]
    amount = abs(int(row["expected_coin_delta"]))
    if row["status"] in (CREATED, COIN_UNKNOWN):
        try:
            central.currency_add(row["user_id"], amount, row["coin_idempotency_key"])
        except Exception as error:
            logger.warning("refund %s outcome unclear: %s", tx_id, error)
            _update(db, tx_id, status=COIN_UNKNOWN, central_status=UNKNOWN)
            return _reload(db, tx_id)
        _update(db, tx_id, status=COIN_REFUNDED, central_status=APPLIED)
    _update(db, tx_id, status=COMPENSATED, local_status=NOT_REQUIRED)
    return _reload(db, tx_id)


# =====================================================================
# §17.6 fulfillment receipts
# =====================================================================
def _apply_local_guarded(db: Database, row, apply_local, kind: str) -> None:
    """Apply the local side in ONE local transaction, guarded by a receipt.

    A retry's duplicate INSERT fails on the primary key, the transaction rolls
    back, and the caller advances the status instead of granting again — which
    is what stops an equipment purchase from granting a second instance after a
    crash between the grant and the status write.
    """
    if apply_local is None:
        return
    payload = json.loads(row["local_payload"] or "{}")
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    existing = db.one("SELECT tx_id FROM fulfillment_receipts WHERE tx_id = ?",
                      (row["tx_id"],))
    if existing is not None:
        return

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO fulfillment_receipts (tx_id, user_id, kind, payload_hash, "
            "applied_at) VALUES (?, ?, ?, ?, ?)",
            (row["tx_id"], row["user_id"], kind, payload_hash, utcnow()),
        )
        apply_local(db, payload)


def _reload(db: Database, tx_id: str) -> TransactionResult:
    return _result(db.one("SELECT * FROM purchase_transactions WHERE tx_id = ?", (tx_id,)))


def _result(row) -> TransactionResult:
    return TransactionResult(
        tx_id=row["tx_id"],
        status=row["status"],
        central_status=row["central_status"],
        local_status=row["local_status"],
        applied_delta=row["coin_applied_delta"],
    )


# =====================================================================
# §17.3 rule 8 — operator reconciliation for a parked GRANT
# =====================================================================
def reconcile_operator_required(db: Database, central: CentralClient, *,
                                tx_id: str | None = None,
                                handlers: dict[str, Callable] | None = None
                                ) -> list[TransactionResult]:
    """Re-examine GRANT transactions parked at `OPERATOR_REQUIRED`.

    `OPERATOR_REQUIRED` is deliberately in `TERMINAL_STATUSES` — `resume_pending`
    never touches it, because an applied-delta mismatch on a grant must not
    auto-resolve on every startup. This is the operator's tool, called by hand
    (e.g. `python -m app.cli.reconcile_transactions`), not part of the startup
    scan.

    It re-issues the grant's ORIGINAL idempotency key — never a fresh one, for
    the same reason §17.3 rule 6 re-issues the same key on `coin_unknown`.
    Central's idempotency guarantee means this cannot double-grant: a key
    already settled just replays its stored result. That is exactly how a
    grant Central genuinely applied in full, but that Deckout misread as
    partial (e.g. because it parsed the wrong response fields), gets
    reconciled here without ever calling `currency_add` for a fresh amount.

    A transaction whose re-checked applied delta still does not match the
    requested amount stays at `OPERATOR_REQUIRED` — a real partial grant is
    not something this function may resolve on its own.
    """
    query = ("SELECT * FROM purchase_transactions WHERE status = ? "
            "AND direction = ?")
    params: list = [OPERATOR_REQUIRED, GRANT]
    if tx_id is not None:
        query += " AND tx_id = ?"
        params.append(tx_id)
    rows = db.query(query, tuple(params))
    return [_reconcile_one_grant(db, central, row, (handlers or {}).get(row["operation"]),
                                 row["operation"])
            for row in rows]


def _reconcile_one_grant(db, central, row, apply_local, kind) -> TransactionResult:
    tx_id = row["tx_id"]
    amount = abs(int(row["expected_coin_delta"]))
    try:
        result: CurrencyResult = central.currency_add(
            row["user_id"], amount, row["coin_idempotency_key"])
    except Exception as error:
        logger.warning("operator reconciliation for %s unclear: %s", tx_id, error)
        return _reload(db, tx_id)          # stays at OPERATOR_REQUIRED

    if result.applied != amount:
        # Re-checked with the SAME key and it still does not match — a real
        # anomaly, not a parsing artifact. Leave it exactly where it was.
        _update(db, tx_id, coin_applied_delta=result.applied)
        return _reload(db, tx_id)

    _update(db, tx_id, status=COIN_GRANTED, central_status=APPLIED,
           coin_applied_delta=result.applied)
    row = db.one("SELECT * FROM purchase_transactions WHERE tx_id = ?", (tx_id,))
    if row["local_status"] != NOT_REQUIRED:
        _apply_local_guarded(db, row, apply_local, kind)
        _update(db, tx_id, local_status=LOCAL_APPLIED)
    _update(db, tx_id, status=COMPLETED)
    return _reload(db, tx_id)


# =====================================================================
# §17.4 recovery
# =====================================================================
def resume_pending(db: Database, central: CentralClient,
                   handlers: dict[str, Callable] | None = None) -> list[TransactionResult]:
    """Scan non-terminal transactions and resume from the recorded status.

    Never mint fresh payment keys for the same attempt; never repeat a payment
    side already recorded as successful. A row in `local_applied` is retried
    FORWARD, not refunded.
    """
    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    rows = db.query(
        f"SELECT tx_id, operation FROM purchase_transactions "
        f"WHERE status NOT IN ({placeholders}) ORDER BY created_at",
        tuple(TERMINAL_STATUSES),
    )
    results = []
    for row in rows:
        handler = (handlers or {}).get(row["operation"])
        results.append(run_transaction(db, central, tx_id=row["tx_id"],
                                       apply_local=handler,
                                       kind=row["operation"]))
    return results
