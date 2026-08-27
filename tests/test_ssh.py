from backend.ssh import _base_cmd
from tests.conftest import make_host


def test_base_cmd_uses_key_and_pins_strict_host_key_checking():
    cmd = _base_cmd(make_host(key="~/.ssh/id_ed25519", port=2222))
    assert "-i" in cmd and "~/.ssh/id_ed25519" in cmd
    assert "-p" in cmd and "2222" in cmd
    assert "StrictHostKeyChecking=accept-new" in " ".join(cmd)
    assert "-F" not in cmd


def test_base_cmd_uses_config_file_and_does_not_force_strict_host_key_checking():
    cmd = _base_cmd(make_host(key=None, config_file="/tbot-data/ssh_config"))
    assert "-F" in cmd and "/tbot-data/ssh_config" in cmd
    assert "-i" not in cmd
    assert "-p" not in cmd
    # The config file's own Host block owns host-key trust — forcing our
    # own StrictHostKeyChecking here would override a trust model we don't
    # control (e.g. a Teleport ProxyCommand's own verification).
    assert "StrictHostKeyChecking" not in " ".join(cmd)


def test_base_cmd_always_sets_batch_mode_and_connect_timeout():
    for host in (make_host(key="~/.ssh/id"), make_host(key=None, config_file="/x")):
        joined = " ".join(_base_cmd(host))
        assert "BatchMode=yes" in joined
        assert "ConnectTimeout=8" in joined
