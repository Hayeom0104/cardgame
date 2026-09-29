import pytest
from dotenv import dotenv_values

from app.cli.setup_admin import configure


def test_enable_admin_preserves_game_credentials_and_existing_signing_secret(tmp_path):
    path = tmp_path / '.env'
    path.write_text('DECKOUT_API_KEY=original\nDECKOUT_DB=runtime/deckout.db\nDECKOUT_ADMIN_SECRET=existing\n')
    configure(path, "test-'한글-password")
    values = dotenv_values(path)
    assert values['DECKOUT_API_KEY'] == 'original'
    assert values['DECKOUT_DB'] == 'runtime/deckout.db'
    assert values['DECKOUT_ADMIN_SECRET'] == 'existing'
    assert values['DECKOUT_ADMIN_PASSWORD'] == "test-'한글-password"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('password', ['', '  ', 'a\nb', '${HOME}'])
def test_invalid_password_leaves_existing_settings_unchanged(tmp_path, password):
    path = tmp_path / '.env'
    original = 'DECKOUT_API_KEY=original\n'
    path.write_text(original)
    with pytest.raises(ValueError):
        configure(path, password)
    assert path.read_text() == original


def test_setup_generates_signing_secret_and_requires_existing_config(tmp_path):
    path = tmp_path / '.env'
    with pytest.raises(ValueError):
        configure(path, 'password')
    assert not path.exists()
    path.write_text('DECKOUT_API_KEY=original\n')
    configure(path, 'password')
    assert len(dotenv_values(path)['DECKOUT_ADMIN_SECRET']) >= 48
