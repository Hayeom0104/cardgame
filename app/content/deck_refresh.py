"""Additive content update: existing authored bosses/cards are preserved.

All patterns use the shared action/condition/status engine. These seed values
are editable content rows, not enemy-ID branches in the battle engine.
"""
import json

from app.content.balance import DEFAULT_CONSTANTS
from app.engine import statuses as st


def apply(db, version):
    for key in ("deck_skill_copy_limit", "character_starter_skills", "boss_preview_hints"):
        db.execute("INSERT OR IGNORE INTO balancing_constants(content_version_id,key,value_json) "
                   "VALUES(?,?,?)", (version, key, json.dumps(DEFAULT_CONSTANTS[key], ensure_ascii=False)))
    if not db.one("SELECT 1 FROM worlds WHERE content_version_id=? AND world_id='world_1'", (version,)):
        return

    def damage(value):
        return {"operator": "deal_damage", "params": {"multiplier": value}}

    def block(value):
        return {"operator": "grant_block", "params": {"mode": "multiplier", "value": value}}

    def modifier(stat, delta):
        return {"operator": "modify_stat", "params": {
            "stat": stat, "delta": delta, "is_percent": False, "duration_rounds": 1}}

    def status(sid):
        return {"operator": "apply_status", "params": {
            "status_id": sid, "stacks": 1, "duration_override": 2}}

    actions = [
        ("act_w1_hex", "쇠약 주술 · 다음 저주 폭발", "버프디버프", "enemy", [status(st.SPEED_DOWN)]),
        ("act_w1_hex_all", "광역 쇠약 · 다음 저주 폭발", "버프디버프", "all", [status(st.DEFENSE_DOWN)]),
        ("act_w1_hex_blast", "저주 폭발 · 방어로 대비", "공격", "enemy", [damage(1.5)]),
        ("act_w1_hex_rest", "주술 재정비 · 공격 기회", "방어", "self", [block(0.3)]),
        ("act_w1_armor", "철갑 방어 · 다음 방패 강타", "방어", "self", [block(1.5), modifier("def", 6)]),
        ("act_w1_armor_rage", "철갑 반격 태세 · 다음 강타", "방어", "self", [block(2), modifier("def", 6)]),
        ("act_w1_shield_hit", "방패 강타 · 다음 갑옷 정비", "공격", "enemy", [damage(1.4)]),
        ("act_w1_shield_rage", "격노의 강타 · 다음 갑옷 정비", "공격", "enemy", [damage(1.8)]),
        ("act_w1_armor_open", "갑옷 정비 · 방어 0, 공격 기회", "방어", "self", [modifier("def", -8)]),
    ]
    for aid, name, category, side, effects in actions:
        db.execute("INSERT OR IGNORE INTO enemy_actions(content_version_id,action_id,name,"
                   "category,target_side,is_basic_attack,effects_json) VALUES(?,?,?,?,?,0,?)",
                   (version, aid, name, category, side, json.dumps(effects, ensure_ascii=False)))

    def cycle(aid, offset, *, phase=None):
        rule = {"priority": 2 if phase else 5, "condition": {
            "op": "owner_turn_index_mod", "divisor": 3, "value": offset},
            "action_id": aid, "weight": 1, "cooldown_turns": 0}
        if phase:
            rule["min_phase"] = phase
        return rule

    basic = {"priority": 20, "condition": None, "action_id": "act_기본공격",
             "weight": 1, "cooldown_turns": 0}
    bosses = [
        ("shaman", "고블린 대주술사", "암", "서포터형", 200, 12, 3, 88,
         [cycle("act_w1_hex_all", 1, phase=2), cycle("act_w1_hex", 1),
          cycle("act_w1_hex_blast", 2), cycle("act_w1_hex_rest", 0), basic]),
        ("armored", "고블린 철갑대장", "지", "방어형", 240, 14, 8, 84,
         [cycle("act_w1_armor_rage", 1, phase=2), cycle("act_w1_shield_rage", 2, phase=2),
          cycle("act_w1_armor", 1), cycle("act_w1_shield_hit", 2),
          cycle("act_w1_armor_open", 0), basic]),
    ]
    for suffix, name, element, role, hp, atk, defense, spd, rules in bosses:
        eid = f"enemy_w1_{suffix}"
        # Do not overwrite operators' edits when publishing a copied draft.
        created = db.execute("INSERT OR IGNORE INTO enemies(content_version_id,enemy_id,name,tier,"
                             "element,role,hp,atk,def,spd,strategy_override,action_rules_json,is_retired) "
                             "VALUES(?,?,?,'보스',?,?,?,?,?,?,'strat_lowest_hp_pct',?,0)",
                             (version, eid, name, element, role, hp, atk, defense, spd,
                              json.dumps(rules, ensure_ascii=False))).rowcount
        if created:
            db.execute("INSERT INTO boss_phases(content_version_id,boss_phase_id,enemy_id,"
                       "phase_index,hp_threshold_pct,effect_ids_json) VALUES(?,?,?,2,0.5,'[]')",
                       (version, f"phase_w1_{suffix}_2", eid))
        db.execute("INSERT OR IGNORE INTO encounters(content_version_id,encounter_id,world_id,kind,"
                   "units_json) VALUES(?,?,'world_1','boss',?)",
                   (version, f"enc_w1_{suffix}", json.dumps([{"enemy_id": eid, "slot": 0}])))

    # Designate the existing run-only grant event explicitly as a trial.
    # It never inserts an unlocked_cards row or awards duplicate materials.
    row = db.one("SELECT branches_json FROM events WHERE content_version_id=? "
                 "AND event_id='event_수상한행상'", (version,))
    if row:
        branches = json.loads(row["branches_json"])
        if branches and branches[0].get("label") == "카드를 산다":
            branches[0]["label"] = "화염 강타 체험 · 이번 모험에서만 사용"
            db.execute("UPDATE events SET branches_json=? WHERE content_version_id=? "
                       "AND event_id='event_수상한행상'",
                       (json.dumps(branches, ensure_ascii=False), version))


def validate_bundles(db, version):
    from app.content.operators import ValidationError
    row = db.one("SELECT value_json FROM balancing_constants WHERE content_version_id=? "
                 "AND key='character_starter_skills'", (version,))
    if not row:  # Pre-update run snapshots remain valid.
        return
    bundles = json.loads(row["value_json"])
    for cid, card_ids in bundles.items():
        character = db.one("SELECT element FROM characters WHERE content_version_id=? AND character_id=?",
                           (version, cid))
        if not character or not card_ids or len(set(card_ids)) != len(card_ids):
            raise ValidationError(f"Invalid starter bundle: {cid}")
        for card_id in card_ids:
            card = db.one("SELECT element,is_retired FROM cards WHERE content_version_id=? AND card_id=?",
                          (version, card_id))
            if not card or card["is_retired"] or card["element"] not in ("무속성", character["element"]):
                raise ValidationError(f"Invalid starter skill: {cid} / {card_id}")
