"""A boss preview lasts until that world's next run is materialized."""
import json
import secrets


def preview(db, user_id, world_id, version):
    with db.tx():
        row = db.one("SELECT encounter_id FROM boss_previews WHERE user_id=? "
                     "AND world_id=? AND content_version_id=?", (user_id, world_id, version))
        if row:
            return row["encounter_id"]
        candidates = [r["encounter_id"] for r in db.query(
            "SELECT encounter_id FROM encounters WHERE content_version_id=? "
            "AND world_id=? AND kind='boss' ORDER BY encounter_id", (version, world_id))]
        if not candidates:
            raise ValueError("이 월드에 보스가 없습니다.")
        chosen = secrets.choice(candidates)
        db.execute("INSERT INTO boss_previews(user_id,world_id,content_version_id,encounter_id) "
                   "VALUES(?,?,?,?)", (user_id, world_id, version, chosen))
        return chosen


def description(db, user_id, world_id, version):
    encounter_id = preview(db, user_id, world_id, version)
    row = db.one("SELECT units_json FROM encounters WHERE content_version_id=? "
                 "AND encounter_id=?", (version, encounter_id))
    names = []
    for entry in json.loads(row["units_json"]):
        enemy = db.one("SELECT name FROM enemies WHERE content_version_id=? AND enemy_id=?",
                       (version, entry["enemy_id"]))
        names.append(enemy["name"])
    from app.content.balance import Balance
    hints = Balance(db, version).get("boss_preview_hints", {})
    return "이번 도전 보스: " + " · ".join(names) + "\n" + hints.get(encounter_id, "적의 다음 행동을 보고 대비하세요.")
