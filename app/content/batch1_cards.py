"""Deckout 1차 카드 배치 48장 (Design Addendum A-2)."""

from __future__ import annotations

from app.engine import statuses as st


def cards(card_cost_min: int) -> list[tuple]:
    damage = lambda multiplier, **extra: [
        {"operator": "deal_damage", "params": {"multiplier": multiplier, **extra}}
    ]
    status = lambda status_id, stacks=1, **extra: {
        "operator": "apply_status",
        "params": {"status_id": status_id, "stacks": stacks, **extra},
    }
    block = lambda mode, value: {
        "operator": "grant_block", "params": {"mode": mode, "value": value}
    }
    heal = lambda mode, value: {
        "operator": "heal", "params": {"mode": mode, "value": value}
    }

    return [
        # 화: 화상 누적과 순간 화력
        ("card_화_불씨", "타오르는 불씨", "화", 2, "공격", "enemy", 1,
         damage(1.25) + [status(st.BURN)]),
        ("card_화_강타", "화염 강타", "화", 2, "공격", "enemy", 2,
         damage(1.55) + [status(st.BURN)]),
        ("card_화_불꽃장막", "불꽃 장막", "화", 2, "방어", "self", 2,
         [block("multiplier", 1.8), status(st.ATTACK_UP)]),
        ("card_화_열기고조", "열기 고조", "화", 2, "버프디버프", "self", 3,
         [status(st.ATTACK_UP, 2)]),
        ("card_화_폭발", "연쇄 폭발", "화", 3, "공격", "enemy", 4,
         damage(1.45) + [status(st.BURN, 2)]),
        ("card_화_홍련", "홍련의 심판", "화", 3, "공격", "enemy", 5,
         damage(2.75, crit_chance=0.15, crit_multiplier=3.2) + [status(st.BURN, 3)]),

        # 수: 회복, 보호막, 흐름 제어
        ("card_수_물결", "물결 베기", "수", 2, "공격", "enemy", 1, damage(1.45)),
        ("card_수_치유", "물의 치유", "수", 2, "회복", "ally", 2,
         [heal("percent_max_hp", 0.22)]),
        ("card_수_보호막", "물의 장막", "수", 2, "방어", "ally", 2,
         [block("multiplier", 2.0)]),
        ("card_수_냉류", "차가운 물살", "수", 2, "버프디버프", "enemy", 3,
         [status(st.SPEED_DOWN, 2)]),
        ("card_수_해일", "해일", "수", 3, "공격", "enemy", 4,
         damage(1.55) + [status(st.HEAL_DOWN)]),
        ("card_수_생명의샘", "생명의 샘", "수", 3, "회복", "ally", 5,
         [heal("percent_max_hp", 0.18),
          {"operator": "remove_status", "params": {"category": "debuff", "count": 1}}]),
        # 리라 합류: 수 속성의 딜서포트가 선택할 수 있는 세 장을 함께 넣어
        # 캐릭터 수와 전용 카드 풀이 항상 같은 규칙을 지키게 한다.
        ("card_수_유리파편", "유리 파편", "수", 2, "공격", "enemy", 1,
         damage(1.35) + [status(st.HEAL_DOWN)]),
        ("card_수_파도수호", "파도 수호", "수", 2, "방어", "ally", 2,
         [block("multiplier", 1.75), status(st.ATTACK_UP)]),
        ("card_수_월류", "월류의 합창", "수", 3, "회복", "ally", 4,
         [heal("percent_max_hp", 0.16), status(st.ATTACK_UP, target="all_allies")]),

        # 풍: 속도, 드로우, 치명타
        ("card_풍_질풍", "질풍베기", "풍", 2, "공격", "enemy", 1,
         damage(1.4, crit_chance=0.2, crit_multiplier=2.1)),
        ("card_풍_가속", "순풍", "풍", 2, "버프디버프", "ally", 2,
         [{"operator": "modify_stat", "params": {
             "stat": "spd", "delta": 12, "duration_rounds": 2}}]),
        ("card_풍_회오리", "회오리 칼날", "풍", 2, "공격", "enemy", 2, damage(1.05)),
        ("card_풍_바람읽기", "바람 읽기", "풍", 2, "버프디버프", "self", 3,
         [{"operator": "draw_cards", "params": {"count": 1}},
          {"operator": "modify_resource", "params": {"delta": 1}}]),
        ("card_풍_폭풍무", "폭풍의 춤", "풍", 3, "공격", "enemy", 4,
         damage(2.25, crit_chance=0.3, crit_multiplier=2.8)),
        ("card_풍_천공", "천공의 소용돌이", "풍", 3, "공격", "enemy", 5,
         damage(1.65) + [status(st.SPEED_DOWN, 2)]),

        # 지: 방어막, 도발, 방어 붕괴
        ("card_지_암석타", "암석타", "지", 2, "공격", "enemy", 1, damage(1.5)),
        ("card_지_도발", "대지의 방벽", "지", 2, "방어", "self", 2,
         [block("multiplier", 2.35), status(st.TAUNT)]),
        ("card_지_석갑", "석갑", "지", 2, "방어", "ally", 2,
         [block("flat", 18)]),
        ("card_지_균열", "균열", "지", 2, "버프디버프", "enemy", 3,
         [status(st.DEFENSE_DOWN, 2)]),
        ("card_지_흔들기", "대지 가르기", "지", 3, "공격", "enemy", 4,
         damage(1.5) + [status(st.SPEED_DOWN)]),
        ("card_지_성벽", "불괴의 성벽", "지", 3, "방어", "self", 5,
         [block("multiplier", 1.8), status(st.ATTACK_UP, target="all_allies")]),

        # 광: 정화, 회복, 아군 강화
        ("card_광_섬광", "섬광", "광", 2, "공격", "enemy", 1, damage(1.4)),
        ("card_광_축복", "빛의 축복", "광", 2, "버프디버프", "ally", 2,
         [status(st.ATTACK_UP)]),
        ("card_광_온기", "온기의 빛", "광", 2, "회복", "ally", 2,
         [heal("percent_max_hp", 0.2)]),
        ("card_광_정화", "정화의 빛", "광", 2, "회복", "ally", 3,
         [{"operator": "remove_status", "params": {"category": "debuff", "count": 2}},
          heal("percent_max_hp", 0.1)]),
        ("card_광_성광", "성광의 파동", "광", 3, "공격", "enemy", 4,
         damage(1.5) + [status(st.DEFENSE_DOWN)]),
        ("card_광_새벽", "새벽의 기도", "광", 3, "회복", "ally", 5,
         [heal("percent_max_hp", 0.2), status(st.ATTACK_UP, target="all_allies")]),

        # 암: 출혈, 침묵, 방어 무시
        ("card_암_출혈", "그림자 칼날", "암", 2, "공격", "enemy", 1,
         damage(1.3) + [status(st.BLEED)]),
        ("card_암_약화", "그림자 약화", "암", 2, "버프디버프", "enemy", 2,
         [status(st.DEFENSE_DOWN, 2)]),
        ("card_암_흡수", "어둠 흡수", "암", 2, "공격", "enemy", 2,
         damage(1.35) + [status(st.ATTACK_UP, target="self")]),
        ("card_암_침묵", "검은 속삭임", "암", 2, "버프디버프", "enemy", 3,
         [status(st.SILENCE)]),
        ("card_암_심연", "심연의 손아귀", "암", 3, "공격", "enemy", 4,
         damage(2.3, ignores_defense=True) + [status(st.SPEED_DOWN)]),
        ("card_암_월식", "월식", "암", 3, "공격", "enemy", 5,
         damage(1.55) + [status(st.BLEED, 2), status(st.HEAL_DOWN)]),

        # 무속성: 어느 파티에서도 쓰는 전술 카드 12장
        ("card_무_응급처치", "응급 처치", "무속성", card_cost_min, "회복", "ally", 1,
         [heal("flat", 10)]),
        ("card_무_견제", "견제", "무속성", card_cost_min, "공격", "enemy", 1,
         damage(0.9) + [status(st.SPEED_DOWN)]),
        ("card_무_호흡", "호흡 정돈", "무속성", card_cost_min, "버프디버프", "self", 2,
         [{"operator": "modify_resource", "params": {"delta": 1}}]),
        ("card_무_엄호", "엄호", "무속성", card_cost_min, "방어", "ally", 2,
         [block("flat", 10)]),
        ("card_무_정밀타격", "정밀 타격", "무속성", 2, "공격", "enemy", 2,
         damage(1.45, crit_chance=0.2, crit_multiplier=2.2)),
        ("card_무_철벽", "철벽", "무속성", 2, "방어", "self", 2,
         [block("multiplier", 2.2)]),
        ("card_무_전술지시", "전술 지시", "무속성", 2, "버프디버프", "ally", 3,
         [status(st.ATTACK_UP), {"operator": "draw_cards", "params": {"count": 1}}]),
        ("card_무_해독", "상태 정리", "무속성", 2, "회복", "ally", 3,
         [{"operator": "remove_status", "params": {"category": "debuff", "count": 1}},
          heal("flat", 6)]),
        ("card_무_총공세", "총공세", "무속성", 3, "공격", "enemy", 4, damage(1.4)),
        ("card_무_재정비", "재정비", "무속성", 3, "버프디버프", "self", 4,
         [{"operator": "draw_cards", "params": {"count": 2}},
          {"operator": "modify_resource", "params": {"delta": 1}}]),
        ("card_무_수호진", "수호진", "무속성", 3, "방어", "self", 5,
         [block("flat", 14)]),
        ("card_무_결전태세", "결전 태세", "무속성", 3, "버프디버프", "self", 5,
         [status(st.ATTACK_UP, 2, target="all_allies")]),
    ]
