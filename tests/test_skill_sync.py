"""Skill sync: Beacon hands its skills out with each agent's mesh config and
the host sync writes the ones that changed — only valid names, only intact
content, never a deletion."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from backend import mesh, store
from backend.schemas import Agent, Host

SYNC_PATH = Path(__file__).parents[1] / "backend" / "drivers" / "hermes_mesh_sync.py"


@pytest.fixture
def sync():
    spec = importlib.util.spec_from_file_location("hermes_mesh_sync", SYNC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _skill(name, content):
    return {"name": name, "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}


def test_beacon_serves_each_skill_folder(tmp_path, monkeypatch):
    (tmp_path / "alpha-skill").mkdir()
    (tmp_path / "alpha-skill" / "SKILL.md").write_text("---\nname: alpha-skill\n---\nhello\n")
    (tmp_path / "Bad Name").mkdir()
    (tmp_path / "Bad Name" / "SKILL.md").write_text("x")
    (tmp_path / "huge").mkdir()
    (tmp_path / "huge" / "SKILL.md").write_text("x" * (mesh.MAX_SKILL_BYTES + 1))
    (tmp_path / "no-skill-file").mkdir()
    monkeypatch.setenv(mesh.SKILLS_DIR_ENV, str(tmp_path))

    served = mesh.skills()

    assert [s["name"] for s in served] == ["alpha-skill"]
    assert served[0]["sha256"] == hashlib.sha256(served[0]["content"].encode()).hexdigest()


def test_the_repos_own_skill_is_served_by_default(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "s")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    store.upsert_agent(Agent.model_validate({"id": "a1", "type": "hermes", "host": "h1",
                                             "reach": {"endpoint": "http://a1.test:9900"}}))
    assert "agent-mesh" in [s["name"] for s in mesh.config("a1")["skills"]]


def test_install_writes_updates_and_leaves_matching_files_alone(sync, tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    assert sync.install_skills([_skill("demo", "v1")], skills_dir) == ["demo"]
    assert (skills_dir / "demo" / "SKILL.md").read_text() == "v1"

    mtime = (skills_dir / "demo" / "SKILL.md").stat().st_mtime_ns
    assert sync.install_skills([_skill("demo", "v1")], skills_dir) == []  # unchanged: not rewritten
    assert (skills_dir / "demo" / "SKILL.md").stat().st_mtime_ns == mtime

    assert sync.install_skills([_skill("demo", "v2")], skills_dir) == ["demo"]  # a Beacon edit reaches the agent
    assert (skills_dir / "demo" / "SKILL.md").read_text() == "v2"


def test_install_refuses_unsafe_or_damaged_skills(sync, tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    damaged = _skill("good-name", "text")
    damaged["sha256"] = "0" * 64
    bad = [_skill("../escape", "x"), _skill("UPPER", "x"), _skill("a/b", "x"), _skill("", "x"),
           _skill("big", "x" * (sync.MAX_SKILL_BYTES + 1)), damaged, {"name": "no-content"}]

    assert sync.install_skills(bad, skills_dir) == []
    assert list(skills_dir.iterdir()) == []
    assert not (tmp_path / "escape").exists()


def test_install_never_deletes(sync, tmp_path):
    skills_dir = tmp_path / "skills"
    (skills_dir / "local-only").mkdir(parents=True)
    (skills_dir / "local-only" / "SKILL.md").write_text("mine")

    sync.install_skills([_skill("demo", "x")], skills_dir)
    sync.install_skills([], skills_dir)

    assert (skills_dir / "local-only" / "SKILL.md").read_text() == "mine"
    assert (skills_dir / "demo" / "SKILL.md").exists()


def test_a_full_sync_installs_skills_beside_the_env_file(sync, tmp_path, monkeypatch):
    monkeypatch.setattr(sync.subprocess, "run", lambda cmd, **kw: None)
    beacon = tmp_path / "beacon" / "mesh"
    beacon.mkdir(parents=True)
    home = tmp_path / "hermes-home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "token").write_text("tok")
    (home / ".env").write_text("")
    (state / "sync.json").write_text(json.dumps({"beacon": f"file://{tmp_path / 'beacon'}", "agent": "alpha",
                                                 "hermes": ["hermes"], "env_file": str(home / ".env")}))
    (beacon / "alpha").write_text(json.dumps({
        "agent": "alpha", "beacon": f"file://{tmp_path / 'beacon'}", "listen": {"port": 9900, "host": None},
        "inbound": {}, "outbound": [], "board": None, "skills": [_skill("agent-mesh", "# skill")]}))
    monkeypatch.setattr(sync, "restart", lambda cfg, state: None)

    sync.sync(state)

    assert (home / "skills" / "agent-mesh" / "SKILL.md").read_text() == "# skill"
