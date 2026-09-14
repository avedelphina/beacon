import pytest
from pydantic import ValidationError

from backend.schemas import Agent, Host, SSHConfig


def test_ssh_config_requires_key_or_config_file():
    with pytest.raises(ValidationError):
        SSHConfig(user="deploy")


def test_ssh_config_rejects_both_key_and_config_file():
    # Found while adding tests: the old validator only checked "at least
    # one", so both set silently worked — ssh.py picks config_file and
    # `key` becomes dead weight with no warning. Should fail loudly instead.
    with pytest.raises(ValidationError):
        SSHConfig(user="deploy", key="~/.ssh/id", config_file="/tbot/ssh_config")


def test_ssh_config_accepts_key_only():
    cfg = SSHConfig(user="deploy", key="~/.ssh/id")
    assert cfg.key == "~/.ssh/id"
    assert cfg.config_file is None


def test_ssh_config_accepts_config_file_only():
    cfg = SSHConfig(user="deploy", config_file="/tbot/ssh_config")
    assert cfg.config_file == "/tbot/ssh_config"
    assert cfg.key is None


def test_host_requires_valid_ssh_config():
    with pytest.raises(ValidationError):
        Host(id="h1", address="10.0.0.1", ssh={"user": "deploy"})


def test_agent_desired_defaults_to_empty_dict():
    agent = Agent(id="a1", type="hermes", host="h1")
    assert agent.desired == {}
    assert agent.profile is None


# ssh.py builds the literal argv token `f"{user}@{address}"` for the `ssh`
# binary — a leading `-` is parsed as an ssh option (e.g.
# `-oProxyCommand=...`), not a literal username/host, and is enough to get
# local code execution out of a Host record. See backend/ssh.py::_base_cmd.


def test_ssh_config_rejects_option_injection_in_user():
    with pytest.raises(ValidationError):
        SSHConfig(user="-oProxyCommand=touch /tmp/pwned", key="~/.ssh/id")


def test_ssh_config_rejects_whitespace_in_user():
    with pytest.raises(ValidationError):
        SSHConfig(user="deploy -oProxyCommand=x", key="~/.ssh/id")


def test_host_rejects_option_injection_in_address():
    with pytest.raises(ValidationError):
        Host(id="h1", address="-oProxyCommand=touch /tmp/pwned",
             ssh=SSHConfig(user="deploy", key="~/.ssh/id"))


def test_host_accepts_normal_address_and_user():
    host = Host(id="h1", address="10.0.0.1", ssh=SSHConfig(user="deploy", key="~/.ssh/id"))
    assert host.address == "10.0.0.1"
    assert host.ssh.user == "deploy"
