"""Deckout 콘텐츠 확장팩 v1.

신규 스킬 카드 60장 + 패시브 카드 30장 + 이벤트 4종.
기존 seed와 같은 데이터 스키마/연산자만 사용하며, seed_constants 단계에서
idempotent 하게 INSERT OR REPLACE 된다.
"""

from __future__ import annotations

import json

from app.db.connection import Database
from app.engine import passives as pv
from app.engine import statuses as st
from app.engine import timed_effects as te


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _damage(multiplier: float, **extra) -> list[dict]:
    return [{"operator": "deal_damage", "params": {"multiplier": multiplier, **extra}}]


def _status(status_id: str, stacks: int = 1, **extra) -> dict:
    return {"operator": "apply_status", "params": {"status_id": status_id, "stacks": stacks, **extra}}


def _block(mode: str, value: float) -> dict:
    return {"operator": "grant_block", "params": {"mode": mode, "value": value}}


def _heal(mode: str, value: float) -> dict:
    return {"operator": "heal", "params": {"mode": mode, "value": value}}


def _stat(stat: str, delta: int, rounds: int, *, percent: bool = False) -> dict:
    return {"operator": "modify_stat", "params": {
        "stat": stat, "delta": delta, "duration_rounds": rounds, "is_percent": percent,
    }}


def _resource(delta: int) -> dict:
    return {"operator": "modify_resource", "params": {"delta": delta}}


def _draw(count: int) -> dict:
    return {"operator": "draw_cards", "params": {"count": count}}


def skill_cards(card_cost_min: int = 1) -> list[tuple]:
    """신규 스킬 카드 60장.

    원소별 8장(48) + 무속성 12장. 각 원소는 저등급 기반 카드, 중간 시너지,
    고등급 빌드 피니셔를 모두 갖도록 구성한다.
    """
    cmin = max(1, int(card_cost_min))
    return [
        # 화 — 화상 누적 / 공격 상승 / 고점 폭발
        ("card_x1_화_불꽃찌르기", "불꽃 찌르기", "화", 1, "공격", "enemy", 1,
         _damage(1.15) + [_status(st.BURN)]),
        ("card_x1_화_잔불회수", "잔불 회수", "화", 1, "공격", "enemy", 2,
         _damage(0.9) + [_resource(1)]),
        ("card_x1_화_열기방패", "열기 방패", "화", 2, "방어", "self", 2,
         [_block("multiplier", 1.7), _status(st.ATTACK_UP)]),
        ("card_x1_화_재점화", "재점화", "화", 2, "공격", "enemy", 3,
         _damage(1.25) + [_status(st.BURN, 2)]),
        ("card_x1_화_소각표식", "소각 표식", "화", 2, "버프디버프", "enemy", 3,
         [_status(st.DEFENSE_DOWN), _status(st.BURN)]),
        ("card_x1_화_폭열검", "폭열검", "화", 3, "공격", "enemy", 4,
         _damage(2.1, crit_chance=0.10, crit_multiplier=2.2)),
        ("card_x1_화_불사조호흡", "불사조의 호흡", "화", 3, "공격", "enemy", 5,
         _damage(1.8) + [_status(st.BURN, 3), _status(st.ATTACK_UP, target="self")]),
        ("card_x1_화_종말화", "종말화", "화", 4, "공격", "enemy", 6,
         _damage(3.2, crit_chance=0.15, crit_multiplier=2.5) + [_status(st.BURN, 4)]),

        # 수 — 회복 / 보호막 / 감속 / 정화
        ("card_x1_수_물방울", "물방울 베기", "수", 1, "공격", "enemy", 1, _damage(1.25)),
        ("card_x1_수_얕은치유", "얕은 치유", "수", 1, "회복", "ally", 2,
         [_heal("percent_max_hp", 0.12)]),
        ("card_x1_수_잔물결막", "잔물결 장막", "수", 2, "방어", "ally", 2,
         [_block("multiplier", 1.65)]),
        ("card_x1_수_저류", "저류", "수", 2, "버프디버프", "enemy", 3,
         [_status(st.SPEED_DOWN, 2)]),
        ("card_x1_수_맑은흐름", "맑은 흐름", "수", 2, "회복", "ally", 3,
         [{"operator": "remove_status", "params": {"category": "debuff", "count": 1}},
          _heal("percent_max_hp", 0.10)]),
        ("card_x1_수_쇄도", "쇄도", "수", 3, "공격", "enemy", 4,
         _damage(1.75) + [_status(st.HEAL_DOWN)]),
        ("card_x1_수_푸른성역", "푸른 성역", "수", 3, "방어", "ally", 5,
         [_block("multiplier", 2.4), _heal("percent_max_hp", 0.10)]),
        ("card_x1_수_대해의은총", "대해의 은총", "수", 4, "회복", "ally", 6,
         [_heal("percent_max_hp", 0.30),
          {"operator": "remove_status", "params": {"category": "debuff", "count": 2}}]),

        # 풍 — 속도 / 치명타 / 드로우 / 자원 순환
        ("card_x1_풍_바람칼", "바람칼", "풍", 1, "공격", "enemy", 1,
         _damage(1.15, crit_chance=0.18, crit_multiplier=2.0)),
        ("card_x1_풍_가벼운발", "가벼운 발", "풍", 1, "버프디버프", "self", 2,
         [_stat("spd", 8, 2)]),
        ("card_x1_풍_순환기류", "순환 기류", "풍", 2, "버프디버프", "self", 2,
         [_draw(1)]),
        ("card_x1_풍_역풍", "역풍", "풍", 2, "버프디버프", "enemy", 3,
         [_status(st.SPEED_DOWN, 2)]),
        ("card_x1_풍_연속베기", "연속 베기", "풍", 2, "공격", "enemy", 3,
         _damage(1.55, crit_chance=0.22, crit_multiplier=2.15)),
        ("card_x1_풍_바람길", "바람길", "풍", 3, "버프디버프", "self", 4,
         [_draw(1), _resource(1)]),
        ("card_x1_풍_청람", "청람", "풍", 3, "공격", "enemy", 5,
         _damage(2.3, crit_chance=0.30, crit_multiplier=2.5)),
        ("card_x1_풍_천풍일섬", "천풍일섬", "풍", 4, "공격", "enemy", 6,
         _damage(2.9, crit_chance=0.38, crit_multiplier=2.6)),

        # 지 — 방어 / 도발 / 방어 붕괴 / 안정적 피해
        ("card_x1_지_석편", "석편 타격", "지", 1, "공격", "enemy", 1, _damage(1.3)),
        ("card_x1_지_흙벽", "흙벽", "지", 1, "방어", "self", 2,
         [_block("multiplier", 1.55)]),
        ("card_x1_지_수호석", "수호석", "지", 2, "방어", "ally", 2,
         [_block("flat", 14)]),
        ("card_x1_지_압박", "지반 압박", "지", 2, "버프디버프", "enemy", 3,
         [_status(st.DEFENSE_DOWN, 2)]),
        ("card_x1_지_버티기", "버티기", "지", 2, "방어", "self", 3,
         [_block("multiplier", 1.9), _status(st.TAUNT)]),
        ("card_x1_지_대지망치", "대지의 망치", "지", 3, "공격", "enemy", 4,
         _damage(2.05) + [_status(st.DEFENSE_DOWN)]),
        ("card_x1_지_산맥의수호", "산맥의 수호", "지", 3, "방어", "ally", 5,
         [_block("multiplier", 2.65), _stat("def", 4, 2)]),
        ("card_x1_지_천지붕괴", "천지 붕괴", "지", 4, "공격", "enemy", 6,
         _damage(2.75, ignores_defense=True) + [_status(st.SPEED_DOWN, 2)]),

        # 광 — 회복 / 정화 / 아군 강화 / 안정성
        ("card_x1_광_빛살", "빛살", "광", 1, "공격", "enemy", 1, _damage(1.2)),
        ("card_x1_광_작은축복", "작은 축복", "광", 1, "버프디버프", "ally", 2,
         [_status(st.ATTACK_UP)]),
        ("card_x1_광_치유광", "치유광", "광", 2, "회복", "ally", 2,
         [_heal("percent_max_hp", 0.14)]),
        ("card_x1_광_정화인", "정화의 인", "광", 2, "회복", "ally", 3,
         [{"operator": "remove_status", "params": {"category": "debuff", "count": 1}}]),
        ("card_x1_광_격려", "찬란한 격려", "광", 2, "버프디버프", "ally", 3,
         [_status(st.ATTACK_UP, 2)]),
        ("card_x1_광_심판광", "심판광", "광", 3, "공격", "enemy", 4,
         _damage(1.9) + [_status(st.DEFENSE_DOWN)]),
        ("card_x1_광_성역", "성역", "광", 3, "방어", "ally", 5,
         [_block("multiplier", 2.1), _heal("percent_max_hp", 0.15)]),
        ("card_x1_광_백야", "백야", "광", 4, "버프디버프", "ally", 6,
         [_status(st.ATTACK_UP, 3),
          {"operator": "remove_status", "params": {"category": "debuff", "count": 2}},
          _heal("percent_max_hp", 0.12)]),

        # 암 — 출혈 / 방어 무시 / 침묵 / 약화
        ("card_x1_암_검은칼끝", "검은 칼끝", "암", 1, "공격", "enemy", 1,
         _damage(1.1) + [_status(st.BLEED)]),
        ("card_x1_암_쇠약", "쇠약", "암", 1, "버프디버프", "enemy", 2,
         [_status(st.DEFENSE_DOWN)]),
        ("card_x1_암_피의흔적", "피의 흔적", "암", 2, "공격", "enemy", 2,
         _damage(1.25) + [_status(st.BLEED, 2)]),
        ("card_x1_암_그림자봉인", "그림자 봉인", "암", 2, "버프디버프", "enemy", 3,
         [_status(st.SILENCE)]),
        ("card_x1_암_틈새베기", "틈새 베기", "암", 2, "공격", "enemy", 3,
         _damage(1.65, ignores_defense=True)),
        ("card_x1_암_흡영", "흡영", "암", 3, "공격", "enemy", 4,
         _damage(1.8) + [_status(st.ATTACK_UP, target="self")]),
        ("card_x1_암_심연인", "심연의 인", "암", 3, "버프디버프", "enemy", 5,
         [_status(st.DEFENSE_DOWN, 2), _status(st.HEAL_DOWN, 2)]),
        ("card_x1_암_무월", "무월", "암", 4, "공격", "enemy", 6,
         _damage(2.8, ignores_defense=True, crit_chance=0.18, crit_multiplier=2.4)
         + [_status(st.BLEED, 3)]),

        # 무속성 — 덱 안정화 / 범용 대응. 효율은 원소 특화 카드보다 약간 낮춘다.
        ("card_x1_무_빠른타격", "빠른 타격", "무속성", cmin, "공격", "enemy", 1,
         _damage(1.05)),
        ("card_x1_무_응급방어", "응급 방어", "무속성", cmin, "방어", "self", 1,
         [_block("flat", 8)]),
        ("card_x1_무_전열교대", "전열 교대", "무속성", 1, "버프디버프", "ally", 2,
         [_stat("def", 2, 2)]),
        ("card_x1_무_약점포착", "약점 포착", "무속성", 1, "버프디버프", "enemy", 2,
         [_status(st.DEFENSE_DOWN)]),
        ("card_x1_무_전투호흡", "전투 호흡", "무속성", 2, "버프디버프", "self", 3,
         [_resource(1)]),
        ("card_x1_무_전술드로우", "전술 드로우", "무속성", 2, "버프디버프", "self", 3,
         [_draw(1)]),
        ("card_x1_무_엄호사격", "엄호 사격", "무속성", 2, "공격", "enemy", 4,
         _damage(1.4) + [_status(st.SPEED_DOWN)]),
        ("card_x1_무_회복진", "회복진", "무속성", 2, "회복", "ally", 4,
         [_heal("percent_max_hp", 0.16)]),
        ("card_x1_무_방어지시", "방어 지시", "무속성", 3, "방어", "ally", 5,
         [_block("multiplier", 2.0), _stat("def", 2, 2)]),
        ("card_x1_무_공격지시", "공격 지시", "무속성", 3, "버프디버프", "ally", 5,
         [_status(st.ATTACK_UP, 2)]),
        ("card_x1_무_완전재정비", "완전 재정비", "무속성", 3, "버프디버프", "self", 6,
         [_draw(2), _resource(1)]),
        ("card_x1_무_결사항전", "결사항전", "무속성", 4, "방어", "self", 6,
         [_block("multiplier", 2.7), _status(st.ATTACK_UP, 2)]),
    ]


def passive_rows() -> list[tuple]:
    """신규 패시브 30장 — 등급별 정확히 5장."""
    B = te.BATTLE_LONG
    return [
        # 1등급: 단순하고 읽기 쉬운 기초 안정화
        ("pas_x1_전투준비", "전투 준비", "전투 내내 파티 공격력 +1.", 1, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("atk", 1, B)]),
        ("pas_x1_가죽보강", "가죽 보강", "전투 내내 파티 방어력 +1.", 1, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("def", 1, B)]),
        ("pas_x1_가벼운장화", "가벼운 장화", "전투 내내 파티 속도 +3.", 1, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("spd", 3, B)]),
        ("pas_x1_응급비축", "응급 비축", "파티원이 HP 35% 미만이면 8% 회복. 전투당 1회.", 1, pv.TRIGGER_ROUND_START, {"hp_below": 0.35}, pv.SCOPE_PARTY, 1, [_heal("percent_max_hp", 0.08)]),
        ("pas_x1_작은축전기", "작은 축전기", "전투 시작 시 자원 +1.", 1, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 1, [_resource(1)]),

        # 2등급: 기초 스탯 + 단일 유틸리티
        ("pas_x1_선봉방패", "선봉 방패", "전투 시작 시 파티가 방어도를 얻습니다.", 2, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 1, [_block("multiplier", 0.65)]),
        ("pas_x1_고요한호흡", "고요한 호흡", "HP 50% 미만이면 디버프 1개 제거. 전투당 1회.", 2, pv.TRIGGER_ROUND_START, {"hp_below": 0.5}, pv.SCOPE_PARTY, 1, [{"operator": "remove_status", "params": {"category": "debuff", "count": 1}}]),
        ("pas_x1_집중훈련", "집중 훈련", "전투 내내 파티 공격력 +2.", 2, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("atk", 2, B)]),
        ("pas_x1_방어훈련", "방어 훈련", "전투 내내 파티 방어력 +2.", 2, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("def", 2, B)]),
        ("pas_x1_보급선", "보급선", "전투 시작 시 파티 HP를 5% 회복.", 2, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 1, [_heal("percent_max_hp", 0.05)]),

        # 3등급: 전투 흐름을 바꾸기 시작하는 중간축
        ("pas_x1_연속전술", "연속 전술", "라운드 시작마다 파티가 소량의 방어도를 얻습니다.", 3, pv.TRIGGER_ROUND_START, {}, pv.SCOPE_PARTY, 0, [_block("multiplier", 0.25)]),
        ("pas_x1_가속신호", "가속 신호", "전투 내내 파티 속도 +6.", 3, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("spd", 6, B)]),
        ("pas_x1_약점분석", "약점 분석", "전투 시작 시 적 전체에게 방어력 감소 1중첩.", 3, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_ENEMIES, 1, [_status(st.DEFENSE_DOWN)]),
        ("pas_x1_회복순환", "회복 순환", "HP 50% 미만이면 파티 HP 12% 회복. 전투당 1회.", 3, pv.TRIGGER_ROUND_START, {"hp_below": 0.5}, pv.SCOPE_PARTY, 1, [_heal("percent_max_hp", 0.12)]),
        ("pas_x1_예비동력", "예비 동력", "HP 50% 미만이면 자원 +1. 전투당 1회.", 3, pv.TRIGGER_ROUND_START, {"hp_below": 0.5}, pv.SCOPE_PARTY, 1, [_resource(1)]),

        # 4등급: 빌드 방향성이 드러나는 고효율 패시브
        ("pas_x1_돌격신호", "돌격 신호", "전투 내내 파티 공격력 +3.", 4, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("atk", 3, B)]),
        ("pas_x1_전술장벽", "전술 장벽", "전투 내내 파티 방어력 +3.", 4, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("def", 3, B)]),
        ("pas_x1_선제교란", "선제 교란", "전투 시작 시 적 전체 속도 감소 1중첩.", 4, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_ENEMIES, 1, [_status(st.SPEED_DOWN)]),
        ("pas_x1_재생프로토콜", "재생 프로토콜", "HP 60% 미만이면 15% 회복하고 디버프 1개 제거. 전투당 1회.", 4, pv.TRIGGER_ROUND_START, {"hp_below": 0.6}, pv.SCOPE_PARTY, 1, [_heal("percent_max_hp", 0.15), {"operator": "remove_status", "params": {"category": "debuff", "count": 1}}]),
        ("pas_x1_비상충전", "비상 충전", "HP 40% 미만이면 자원 +2. 전투당 1회.", 4, pv.TRIGGER_ROUND_START, {"hp_below": 0.4}, pv.SCOPE_PARTY, 1, [_resource(2)]),

        # 5등급: 두 효과를 묶되 무한 자원/드로우는 만들지 않는다.
        ("pas_x1_맹공준비", "맹공 준비", "전투 내내 공격력 +4, 속도 +4.", 5, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("atk", 4, B), _stat("spd", 4, B)]),
        ("pas_x1_철의진형", "철의 진형", "전투 내내 방어력 +4, 시작 방어도 추가.", 5, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 1, [_stat("def", 4, B), _block("multiplier", 0.55)]),
        ("pas_x1_해체신호", "해체 신호", "전투 시작 시 적 전체 방어력 감소 2중첩.", 5, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_ENEMIES, 1, [_status(st.DEFENSE_DOWN, 2)]),
        ("pas_x1_생존본능", "생존 본능", "HP 35% 미만이면 20% 회복하고 방어도를 얻습니다. 전투당 1회.", 5, pv.TRIGGER_ROUND_START, {"hp_below": 0.35}, pv.SCOPE_PARTY, 1, [_heal("percent_max_hp", 0.20), _block("multiplier", 0.8)]),
        ("pas_x1_공명코어", "공명 코어", "전투 시작 시 자원 +2.", 5, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 1, [_resource(2)]),

        # 6등급: 강한 런 방향 제시. 슬롯 하나를 차지하는 만큼 눈에 띄되 자동승리는 금지.
        ("pas_x1_전투지휘", "전투 지휘", "전투 내내 파티 공격력 +5, 속도 +6.", 6, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 0, [_stat("atk", 5, B), _stat("spd", 6, B)]),
        ("pas_x1_절대방벽", "절대 방벽", "전투 내내 방어력 +5, 전투 시작 시 큰 방어도.", 6, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 1, [_stat("def", 5, B), _block("multiplier", 1.0)]),
        ("pas_x1_전면교란", "전면 교란", "전투 시작 시 적 전체 방어력/속도를 크게 낮춥니다.", 6, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_ENEMIES, 1, [_status(st.DEFENSE_DOWN, 2), _status(st.SPEED_DOWN, 2)]),
        ("pas_x1_불굴의맥동", "불굴의 맥동", "HP 30% 미만이면 25% 회복, 공격력 증가 2중첩, 자원 +1. 전투당 1회.", 6, pv.TRIGGER_ROUND_START, {"hp_below": 0.30}, pv.SCOPE_PARTY, 1, [_heal("percent_max_hp", 0.25), _status(st.ATTACK_UP, 2), _resource(1)]),
        ("pas_x1_초동우위", "초동 우위", "전투 시작 시 큰 방어도와 자원 +2.", 6, pv.TRIGGER_BATTLE_START, {}, pv.SCOPE_PARTY, 1, [_block("multiplier", 1.2), _resource(2)]),
    ]


def event_rows() -> list[tuple]:
    """신규 이벤트 4종. 공짜 보상만 누적되지 않도록 선택 비용을 섞었다."""
    return [
        ("event_x1_균열난천문대", "균열난 천문대", "choice", "none", [
            {"label": "위험한 관측을 한다", "effects": [
                {"operator": "modify_hp", "params": {"mode": "percent_max_hp", "delta": -0.12}},
                {"operator": "grant_currency", "params": {"currency": "run_currency", "amount": 95}},
            ]},
            {"label": "주변만 수색한다", "effects": [
                {"operator": "grant_currency", "params": {"currency": "run_currency", "amount": 30}},
            ]},
        ]),
        ("event_x1_침묵의온천", "침묵의 온천", "choice", "none", [
            {"label": "상처를 씻는다", "effects": [
                {"operator": "heal", "params": {"mode": "percent_max_hp", "value": 0.18}},
            ]},
            {"label": "바닥을 뒤진다", "effects": [
                {"operator": "grant_enhancement_stone", "params": {"tier": 1, "amount": 2}},
            ]},
        ]),
        ("event_x1_폐허의기록실", "폐허의 기록실", "choice", "none", [
            {"label": "전투 기록을 복원한다", "effects": [
                {"operator": "grant_card_fragments", "params": {"card_id": "card_x1_무_완전재정비", "amount": 70}},
            ]},
            {"label": "봉인된 상자를 연다", "effects": [
                {"operator": "grant_currency", "params": {"currency": "carta", "amount": 90}},
            ]},
        ]),
        ("event_x1_시험의종", "시험의 종", "choice", "none", [
            {"label": "종을 울린다", "effects": [
                {"operator": "start_combat", "params": {"encounter_id": "enc_w1_normal_3"}},
            ]},
            {"label": "대가를 치르고 지나간다", "effects": [
                {"operator": "insert_cursed_card", "params": {"cursed_card_id": "curse_무거운사슬"}},
                {"operator": "grant_currency", "params": {"currency": "run_currency", "amount": 110}},
            ]},
        ]),
    ]


def seed_expansion(db: Database, version: int, *, card_cost_min: int = 1) -> dict[str, int]:
    """확장팩을 한 콘텐츠 버전에 저장한다. 여러 번 호출해도 동일 결과다."""
    cards = skill_cards(card_cost_min)
    passives = passive_rows()
    events = event_rows()

    for card_id, name, element, cost, category, target, tier, effects in cards:
        db.execute(
            "INSERT OR REPLACE INTO cards (content_version_id, card_id, name, element, "
            "cost, category, target_side, rarity_tier, effects_json, art_asset, is_retired) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
            (version, card_id, name, element, cost, category, target, tier, _json(effects)),
        )

    for (passive_id, name, description, tier, trigger, trigger_params, scope,
         once, effects) in passives:
        db.execute(
            "INSERT OR REPLACE INTO passive_cards (content_version_id, passive_card_id, "
            "name, description, rarity_tier, trigger_event, trigger_params_json, "
            "effects_json, target_scope, once_per_battle, art_asset, in_gacha_pool, "
            "is_retired) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 0)",
            (version, passive_id, name, description, tier, trigger,
             _json(trigger_params), _json(effects), scope, once),
        )

    for event_id, name, kind, combat_link, branches in events:
        db.execute(
            "INSERT OR REPLACE INTO events (content_version_id, event_id, name, "
            "interaction_kind, branches_json, combat_link) VALUES (?, ?, ?, ?, ?, ?)",
            (version, event_id, name, kind, _json(branches), combat_link),
        )

    return {"skills": len(cards), "passives": len(passives), "events": len(events)}
