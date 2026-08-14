"""출시 콘텐츠가 실제로 끝까지 플레이 가능한가.

여기서 확인하는 것은 "콘텐츠가 많은가"가 아니라 **"빠진 자리가 없는가"** 다.
빠진 자리는 대체로 플레이어가 몇 분을 들여 거기까지 간 다음에야 드러난다.
"""

from __future__ import annotations

import pytest

from app.content import validation
from app.content.operators import ValidationError
from app.engine import achievements as ach
from app.engine import encounter as enc
from app.engine import lifecycle as lc
from app.engine import map_gen
from app.engine import nodes
from app.engine.rng import JournaledRng


# =====================================================================
# 월드가 끝까지 이어지는가
# =====================================================================
def test_every_world_has_a_normal_and_a_boss_encounter(db, version):
    """이것이 없으면 그 칸에 도달한 런이 그 자리에서 죽는다."""
    worlds = [row["world_id"] for row in db.query(
        "SELECT world_id FROM worlds WHERE content_version_id = ?", (version,))]
    assert worlds
    for world_id in worlds:
        for kind in ("normal", "boss"):
            count = db.one(
                "SELECT COUNT(*) AS n FROM encounters WHERE content_version_id = ? "
                "AND world_id = ? AND kind = ?", (version, world_id, kind))["n"]
            assert count, f"{world_id} 에 {kind} 조우가 없습니다"


def test_validation_rejects_a_world_with_no_boss(db, version):
    db.execute("DELETE FROM encounters WHERE content_version_id = ? AND kind = 'boss' "
               "AND world_id = 'world_2'", (version,))
    with pytest.raises(ValidationError, match="world_2"):
        validation.validate_version(db, version)


def test_every_encounter_names_enemies_that_exist(db, version):
    import json

    known = {row["enemy_id"] for row in db.query(
        "SELECT enemy_id FROM enemies WHERE content_version_id = ?", (version,))}
    for row in db.query("SELECT * FROM encounters WHERE content_version_id = ?",
                        (version,)):
        for unit in json.loads(row["units_json"]):
            assert unit["enemy_id"] in known, \
                f"{row['encounter_id']} 가 없는 적 {unit['enemy_id']} 를 부릅니다"


def graduate(db, user_id: int, *worlds: str) -> list[str]:
    """§4.1 — 본편은 파티 2명부터다. 튜토리얼을 마친 계정을 그 상태로 만든다."""
    from app.db.connection import utcnow

    db.execute("UPDATE accounts SET tutorial_completed_at = ?, party_slots = 2 "
               "WHERE user_id = ?", (utcnow(), user_id))
    db.execute("INSERT OR IGNORE INTO owned_characters (user_id, character_id, "
               "star_rank, acquired_at) VALUES (?, 'char_ignis', 2, ?)",
               (user_id, utcnow()))
    for world_id in worlds:
        db.execute(
            "INSERT OR IGNORE INTO world_unlocks (user_id, world_id, unlocked_at) "
            "VALUES (?, ?, ?)", (user_id, world_id, utcnow()))
    return ["starter_001", "char_ignis"]


def test_a_run_in_the_second_world_can_actually_start_a_battle(
        db, balance, version, user_id):
    """1세계를 깬 계정이 2세계에 들어가면 전투가 실제로 붙는가."""
    party = graduate(db, user_id, "world_2")

    run_id = lc.create_run(
        db, balance,
        lc.RunBuildRequest(user_id=user_id, world_id="world_2",
                           party_character_ids=party),
        version,
    )
    run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    rng = JournaledRng(db, run_id, run["rng_seed"])

    combat = db.one(
        "SELECT * FROM run_nodes WHERE run_id = ? AND node_type = ? "
        "ORDER BY node_index LIMIT 1", (run_id, map_gen.COMBAT))
    assert combat is not None
    encounter_id = nodes._pick_encounter(db, rng, run, combat["node_index"],
                                         "normal")
    battle_id = enc.create_battle(
        db, balance, run_id=run_id, node_index=combat["node_index"],
        encounter_id=encounter_id, content_version_id=version)
    units = db.query("SELECT * FROM battle_units WHERE battle_id = ? AND side = 'enemy'",
                     (battle_id,))
    assert units, "2세계 전투에 적이 하나도 없습니다"


def test_every_world_can_pick_a_boss_encounter(db, balance, version, user_id):
    worlds = ("world_1", "world_2", "world_3", "world_4")
    party = graduate(db, user_id, *worlds)

    for world_id in worlds:
        run_id = lc.create_run(
            db, balance,
            lc.RunBuildRequest(user_id=user_id, world_id=world_id,
                               party_character_ids=party),
            version,
        )
        run = db.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        rng = JournaledRng(db, run_id, run["rng_seed"])
        assert nodes._pick_encounter(db, rng, run, 0, "boss")
        # 다음 월드를 시작하려면 이 런을 끝내야 한다 (§16.3).
        db.execute("UPDATE runs SET state = 'run_completed' WHERE run_id = ?",
                   (run_id,))


# =====================================================================
# 뽑기 풀에 구멍이 없는가
# =====================================================================
def test_every_rarity_tier_has_a_card_to_pull(db, version):
    tiers = {row["rarity_tier"] for row in db.query(
        "SELECT DISTINCT rarity_tier FROM cards WHERE content_version_id = ? "
        "AND is_retired = 0", (version,))}
    assert {1, 2, 3, 4, 5, 6} <= tiers


def test_every_gacha_star_band_has_a_character_to_pull(db, balance, version):
    """§15.4가 등급마다 성급을 정해 두므로, 그 성급의 캐릭터가 없으면 그
    등급에서 캐릭터가 나올 때마다 뽑기가 실패한다."""
    for band, star in balance.get("gacha_band_star_rank").items():
        count = db.one(
            "SELECT COUNT(*) AS n FROM characters WHERE content_version_id = ? "
            "AND in_gacha_pool = 1 AND is_retired = 0 AND base_rarity = ?",
            (version, int(star)))["n"]
        assert count, f"{band} 등급(★{star})에 뽑을 캐릭터가 없습니다"


def test_every_element_appears_on_at_least_one_card(db, version):
    from app.engine.stats import ALL_ELEMENTS

    elements = {row["element"] for row in db.query(
        "SELECT DISTINCT element FROM cards WHERE content_version_id = ?", (version,))}
    assert set(ALL_ELEMENTS) <= elements


# =====================================================================
# 장비 세트
# =====================================================================
def test_every_equipment_set_covers_all_three_slots(db, version):
    """§8.2의 세트 보너스는 세 부위를 다 채워야 붙는다. 한 부위라도 없는
    세트는 영영 보너스가 붙지 않는 죽은 장비다."""
    slots: dict[str, set[str]] = {}
    for row in db.query(
            "SELECT set_name, slot FROM equipment_defs WHERE content_version_id = ? "
            "AND set_name IS NOT NULL", (version,)):
        slots.setdefault(row["set_name"], set()).add(row["slot"])

    declared = {row["set_name"] for row in db.query(
        "SELECT set_name FROM equipment_sets WHERE content_version_id = ?", (version,))}
    assert declared
    for set_name in declared:
        assert slots.get(set_name) == {"무기", "방어구", "악세서리"}, \
            f"세트 {set_name} 의 부위가 빠져 있습니다: {slots.get(set_name)}"


# =====================================================================
# 업적 카운터
# =====================================================================
def test_every_counter_the_engine_emits_has_an_achievement(db, version):
    """카운터는 오르는데 그것을 보는 업적이 없으면 진행도가 어디에도 안 보인다."""
    emitted = {ach.BOSS_DEFEATED, ach.RUN_CLEARED, ach.ENEMY_KILLED,
               ach.CURSE_REMOVED, ach.EQUIPMENT_TIERED, ach.CHARACTER_STARRED}
    tracked = {row["counter_key"] for row in db.query(
        "SELECT DISTINCT counter_key FROM achievements WHERE content_version_id = ?",
        (version,))}
    assert emitted <= tracked, f"업적이 없는 카운터: {sorted(emitted - tracked)}"


def test_star_up_advances_its_counter(db, balance, version, user_id):
    """`character_starred` 는 선언만 되어 있고 오르는 곳이 없었다."""
    from app.db.connection import utcnow
    from app.engine import progression as pg

    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    db.execute("INSERT INTO character_fragments (user_id, character_id, amount) "
               "VALUES (?, 'char_terradon', 0)", (user_id,))
    pg._apply_star_up(db, {
        "kind": "star_up", "user_id": user_id, "character_id": "char_terradon",
        "from_rank": 1, "fragments": 0, "wildcards": 0,
        "content_version_id": version,
    })
    row = db.one(
        "SELECT current_value FROM achievement_progress WHERE user_id = ? "
        "AND achievement_id = 'ach_성급상승3'", (user_id,))
    assert row is not None and row["current_value"] == 1


def test_an_old_payload_without_the_version_still_stars_up(db, version, user_id):
    """이 배포 전에 쓰인 트랜잭션 행에는 버전 키가 없다 — 업적 하나 때문에
    이미 코인을 낸 성급 상승을 실패시키지 않는다 (§17.4)."""
    from app.db.connection import utcnow
    from app.engine import progression as pg

    db.execute("INSERT INTO owned_characters (user_id, character_id, star_rank, "
               "acquired_at) VALUES (?, 'char_terradon', 1, ?)", (user_id, utcnow()))
    db.execute("INSERT INTO character_fragments (user_id, character_id, amount) "
               "VALUES (?, 'char_terradon', 0)", (user_id,))
    pg._apply_star_up(db, {
        "kind": "star_up", "user_id": user_id, "character_id": "char_terradon",
        "from_rank": 1, "fragments": 0, "wildcards": 0,
    })
    assert db.one("SELECT star_rank FROM owned_characters WHERE user_id = ? "
                  "AND character_id = 'char_terradon'", (user_id,))["star_rank"] == 2
