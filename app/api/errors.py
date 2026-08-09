"""§19.4 — error strings. 🟡

These are the fixed player-facing strings. The full per-screen button and
select label set remains content work (§13.2); §13.3 explicitly does NOT claim
the UI *copy* as closed, only the structure.
"""

NOT_OWNER = "본인의 게임이 아닙니다."
STALE_REVISION = "화면이 갱신되었습니다. 다시 시도해 주세요."
ILLEGAL_STATE = "지금은 할 수 없는 동작입니다."
RUN_ALREADY_ACTIVE = "이미 진행 중인 런이 있습니다."
INSUFFICIENT_CURRENCY = "재화가 부족합니다."
PAYMENT_FAILED = "결제에 실패했습니다. 차감된 금액은 환불됩니다."
TUTORIAL_NOT_CLEARED = "튜토리얼을 먼저 완료해 주세요."
ACHIEVEMENT_LOCKED = "선행 업적을 먼저 달성해 주세요."
PARTY_TOO_SMALL = "본편은 파티원 2명부터 입장할 수 있습니다."

# §19.3 flow labels named as fixed in the doc.
LABEL_SKIP = "안 받기"
LABEL_EXIT = "나가기"
