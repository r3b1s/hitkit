import re
import stat

from hitkit import config
from hitkit.addressing import DEFAULT_SUBNET


def test_load_returns_none_when_absent(user):
    assert config.load(user) is None


def test_load_or_create_writes_defaults_privately(user):
    cfg = config.load_or_create(user)
    assert cfg.subnet == DEFAULT_SUBNET
    assert re.fullmatch(r"[0-9a-f]{32}", cfg.install_id)
    assert stat.S_IMODE(user.config_file.stat().st_mode) == 0o600
    assert config.load(user) == cfg


def test_load_or_create_is_stable(user):
    first = config.load_or_create(user)
    second = config.load_or_create(user)
    assert second.install_id == first.install_id
    assert second == first


def test_subnet_change_keeps_install_id(user):
    first = config.load_or_create(user)
    changed = config.load_or_create(user, subnet="10.88.0.0/24")
    assert changed.subnet == "10.88.0.0/24"
    assert changed.install_id == first.install_id
    assert config.load(user).subnet == "10.88.0.0/24"
