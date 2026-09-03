"""Config templates: the deep-merge, load/validation, store's resolved read,
and the three HTTP routes (list, get, apply — including the confirm gate).
"""

import pytest
import yaml
from fastapi.testclient import TestClient

from backend import store, templates
from backend.app import app
from tests.conftest import make_agent, make_host


def write_template(fleet, name: str, body: dict) -> None:
    (fleet / "templates" / f"{name}.yaml").write_text(yaml.safe_dump(body))


# --- deep merge -----------------------------------------------------------


def test_deep_merge_recurses_dicts():
    base = {"model": {"primary": "a", "fallbacks": ["x"]}, "tools": {"model": "t"}}
    over = {"model": {"primary": "b"}}
    assert templates._deep_merge(base, over) == {
        "model": {"primary": "b", "fallbacks": ["x"]},
        "tools": {"model": "t"},
    }


def test_deep_merge_replaces_lists_wholesale():
    assert templates._deep_merge({"f": ["a", "b"]}, {"f": ["c"]}) == {"f": ["c"]}


def test_deep_merge_replaces_on_type_mismatch():
    assert templates._deep_merge({"k": {"a": 1}}, {"k": "scalar"}) == {"k": "scalar"}


# --- resolve ------------------------------------------------------------------


def test_resolve_no_templates_is_own_desired(fleet):
    agent = make_agent(desired={"config": {"model": {"primary": "a"}}})
    assert templates.resolve(agent) == {"config": {"model": {"primary": "a"}}}


def test_resolve_merges_template_then_agent_wins(fleet):
    write_template(fleet, "anthropic", {
        "config": {"model": {"primary": "anthropic/sonnet", "fallbacks": ["openai/gpt-4o"]},
                   "tools": {"model": "anthropic/haiku"}},
        "env_keys": ["ANTHROPIC_API_KEY"],
    })
    agent = make_agent(templates=["anthropic"],
                       desired={"config": {"model": {"primary": "openai/gpt-4o"}}})
    assert templates.resolve(agent) == {
        "config": {
            "model": {"primary": "openai/gpt-4o", "fallbacks": ["openai/gpt-4o"]},
            "tools": {"model": "anthropic/haiku"},
        },
        "env_keys": ["ANTHROPIC_API_KEY"],
    }


def test_resolve_multiple_templates_last_wins(fleet):
    write_template(fleet, "base", {"config": {"model": {"primary": "a", "tokens": 100}}})
    write_template(fleet, "override", {"config": {"model": {"primary": "b"}}})
    agent = make_agent(templates=["base", "override"])
    assert templates.resolve(agent) == {"config": {"model": {"primary": "b", "tokens": 100}}}


# --- load_template ----------------------------------------------------------


def test_load_missing_template_raises_not_found(fleet):
    with pytest.raises(store.NotFound):
        templates.load_template("nope")


def test_load_template_rejects_unknown_keys(fleet):
    write_template(fleet, "bad", {"config": {}, "install_mode": "simple"})
    with pytest.raises(ValueError):
        templates.load_template("bad")


def test_load_template_rejects_bad_name(fleet):
    with pytest.raises(ValueError):
        templates.load_template("Not_Valid")


# --- store resolved read --------------------------------------------------


def test_get_agent_resolved_merges(fleet):
    store.upsert_host(make_host(id="h1"))
    write_template(fleet, "stack", {"config": {"model": {"primary": "a"}}})
    store.upsert_agent(make_agent(id="a1", host="h1", templates=["stack"]))

    assert store.get_agent("a1").desired == {}
    assert store.get_agent("a1", resolved=True).desired == {"config": {"model": {"primary": "a"}}}


# --- HTTP routes --------------------------------------------------------------


@pytest.fixture
def client(fleet, fake_ssh):
    return TestClient(app)


def test_list_and_get_templates_with_reverse_lookup(client, fleet):
    store.upsert_host(make_host(id="h1"))
    write_template(fleet, "stack", {"config": {"model": {"primary": "a"}}})
    store.upsert_agent(make_agent(id="a1", host="h1", templates=["stack"]))
    store.upsert_agent(make_agent(id="a2", host="h1"))

    r = client.get("/api/templates")
    assert r.status_code == 200
    assert r.json() == [{"name": "stack", "used_by": ["a1"]}]

    r = client.get("/api/templates/stack")
    assert r.json()["content"] == {"config": {"model": {"primary": "a"}}}
    assert r.json()["used_by"] == ["a1"]

    assert client.get("/api/templates/nope").status_code == 404


def test_apply_template_without_confirm_writes_nothing(client, fleet):
    store.upsert_host(make_host(id="h1"))
    write_template(fleet, "stack", {"config": {"model": {"primary": "a"}}})
    store.upsert_agent(make_agent(id="a1", host="h1"))

    r = client.post("/api/templates/stack/apply", json={"agent_ids": ["a1"]})
    assert r.status_code == 200
    assert r.json()["would_run"] is True
    assert store.get_agent("a1").templates == []


def test_apply_template_with_confirm_adds_name_once(client, fleet):
    store.upsert_host(make_host(id="h1"))
    write_template(fleet, "stack", {"config": {"model": {"primary": "a"}}})
    store.upsert_agent(make_agent(id="a1", host="h1"))
    store.upsert_agent(make_agent(id="a2", host="h1"))

    r = client.post("/api/templates/stack/apply", json={"agent_ids": ["a1", "a2"], "confirm": True})
    assert r.status_code == 200
    assert store.get_agent("a1").templates == ["stack"]
    assert store.get_agent("a2").templates == ["stack"]

    # idempotent — applying again doesn't duplicate
    client.post("/api/templates/stack/apply", json={"agent_ids": ["a1"], "confirm": True})
    assert store.get_agent("a1").templates == ["stack"]


def test_apply_template_bad_agent_id_404s_before_writing(client, fleet):
    store.upsert_host(make_host(id="h1"))
    write_template(fleet, "stack", {"config": {}})
    store.upsert_agent(make_agent(id="a1", host="h1"))

    r = client.post("/api/templates/stack/apply", json={"agent_ids": ["a1", "ghost"], "confirm": True})
    assert r.status_code == 404
    assert store.get_agent("a1").templates == []  # first agent not written either


def test_agent_get_exposes_effective_desired(client, fleet):
    store.upsert_host(make_host(id="h1"))
    write_template(fleet, "stack", {"config": {"model": {"primary": "a"}}})
    store.upsert_agent(make_agent(id="a1", host="h1", templates=["stack"],
                                 desired={"config": {"model": {"tokens": 50}}}))

    body = client.get("/api/agents/a1").json()
    assert body["desired"] == {"config": {"model": {"tokens": 50}}}
    assert body["effective_desired"] == {"config": {"model": {"primary": "a", "tokens": 50}}}


def test_config_diff_uses_resolved_desired(client, fleet, fake_ssh):
    from backend.ssh import SSHResult

    store.upsert_host(make_host(id="h1"))
    write_template(fleet, "stack", {"config": {"model": {"primary": "wanted"}}})
    store.upsert_agent(make_agent(id="a1", host="h1", templates=["stack"]))

    fake_ssh.result = SSHResult(
        ok=True, returncode=0,
        stdout="model:\n  primary: actual\n__BEACON_ENV_KEYS__\n", stderr="",
    )
    findings = client.get("/api/agents/a1/config-diff").json()["config"]
    assert findings == [{"path": "model.primary", "status": "drift", "desired": "wanted", "live": "actual"}]
