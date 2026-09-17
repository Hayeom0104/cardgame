"""Render reproducible before/after PNGs without contacting Discord.

Run from the checkout being inspected; output goes to --output.
"""
import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path.cwd()))
from app.api import handlers, screens
from app.content.balance import Balance
from app.content.seed import seed_all, create_account
from app.db.connection import Database, utcnow
from app.engine import progression


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / 'preview.db')
        db.migrate()
        version = seed_all(db)
        balance = Balance(db, version)
        uid = 424242
        create_account(db, uid, version)
        progression.complete_tutorial(db, balance, user_id=uid, content_version_id=version)
        db.execute("INSERT INTO owned_characters(user_id,character_id,star_rank,acquired_at) VALUES(?,'char_aquel',3,?)", (uid, utcnow()))
        cards = ['card_수_물결'] * 3 + ['card_수_보호막'] * 2 + ['card_수_치유'] * 2
        for card in set(cards):
            db.execute('INSERT OR IGNORE INTO unlocked_cards(user_id,card_id,upgrade_tier,unlocked_at) VALUES(?,?,0,?)', (uid, card, utcnow()))
        for definition, tier, slot in [('eq_수련검', 1, '무기'), ('eq_수련갑', 0, '방어구'), ('eq_수련부적', 2, '악세서리')]:
            db.execute("INSERT INTO owned_equipment(user_id,equipment_def_id,tier,equipped_character_id,equipped_slot) VALUES(?,?,?,'starter_001',?)", (uid, definition, tier, slot))
        draft = {'world_id': 'world_1', 'party': ['starter_001', 'char_aquel'], 'passives': [], 'deck': {2: cards}}
        ctx = handlers.HandlerContext(db=db, balance=balance, central=None, content_version_id=version)
        views = {'deck': screens.deck_select_screen(db, uid, version, draft, slot=2),
                 'equipment': handlers.equipment_screen(ctx, uid)}
        for name, view in views.items():
            assert view.get('attachments'), name
            for i, attachment in enumerate(view.pop('attachments')):
                (output / f'{name}_{i}.png').write_bytes(base64.b64decode(attachment['data_b64']))
        (output / 'screens.json').write_text(json.dumps(views, ensure_ascii=False, indent=2))
        db.close()


if __name__ == '__main__':
    main()
