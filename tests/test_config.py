import stat
import sys

from reviewmeh import cli, config


def test_init_writes_fresh_private_secrets(tmp_path, monkeypatch):
    path = tmp_path / 'reviewmeh.toml'
    monkeypatch.setattr(sys, 'argv', ['reviewmeh', 'init', str(path)])
    cli.main()
    text = path.read_text()
    assert '__' not in text.split('[branding]')[0]  # every placeholder was replaced
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_example_config_is_valid_and_matches_the_defaults():
    import tomllib
    from pathlib import Path
    d = tomllib.loads((Path(config.__file__).parent / 'reviewmeh.example.toml').read_text())
    assert d['access']['groups'] == config.GROUP_RIGHTS
    assert d['schema']['cut_by'] == config.SCHEMA.cut_by
    assert set(d['access']['admin_group']) == config.ADMIN_GROUPS
    assert d['access']['share_max_days'] == config.SHARE_MAX_DAYS


def test_browser_settings_hold_no_secret():
    assert not {k for k in config.public() if 'key' in k or 'secret' in k}


def test_missing_settings_are_named(monkeypatch):
    monkeypatch.setattr(config, 'SG_URL', '')
    assert any('shotgrid.url' in p for p in config.problems())
