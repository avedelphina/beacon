"""Tag policy (fleet/policy.yaml): who may call whom, Tailscale-style.
Beacon-only without a file; deny beats allow; selectors are ids, tags, tag
wildcards and "*"; replies bypass the policy and arrive from Beacon."""

import pytest
import yaml

from backend import board, mailbox, mesh, opstore, policy, store
from backend.schemas import Agent, Host


def _write(fleet, text):
    (fleet / "policy.yaml").write_text(text)


def _agent(id_, *tags, network="tailnet"):
    return Agent.model_validate({"id": id_, "type": "hermes", "host": "h1", "tags": list(tags),
                                 "reach": {"endpoint": f"http://{id_}.test:9900", "network": network}})


FAMILIES = """
tags: [family-a, family-b, family-c]
rules:
  - {from: [tag:family-a], to: [tag:family-a]}
  - {from: [tag:family-b], to: [tag:family-b]}
  - {from: [tag:family-c], to: [tag:family-c]}
  - {from: [coordinator], to: ["tag:family-*"]}
deny:
  - {from: [tag:family-c], to: [tag:family-b]}
"""


@pytest.fixture
def families(fleet, monkeypatch):
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.setenv(mesh.PUBLIC_URL_ENV, "http://beacon.test:8642")
    store.upsert_host(Host.model_validate({"id": "h1", "address": "10.0.0.1", "ssh": {"user": "u", "key": "/k"}}))
    for agent in (_agent("coordinator"), _agent("a-1", "family-a"), _agent("a-2", "family-a"),
                  _agent("b-1", "family-b"), _agent("c-1", "family-c"), _agent("loner")):
        store.upsert_agent(agent)
    _write(fleet, FAMILIES)
    return fleet


def _can(caller, target):
    fleet = store.list_agents()
    return mesh.may_call(caller, next(a for a in fleet if a.id == target), fleet)


def test_a_family_talks_within_itself_and_alice_reaches_all(families):
    assert _can("a-1", "a-2") and _can("a-2", "a-1")
    assert not _can("a-1", "b-1") and not _can("b-1", "a-1")
    for family_member in ("a-1", "b-1", "c-1"):
        assert _can("coordinator", family_member)
    assert not _can("loner", "a-1") and not _can("a-1", "loner")


def test_rules_are_one_way_so_families_cannot_call_alice_unless_allowed(families):
    assert not _can("a-1", "coordinator")
    _write(families, FAMILIES.replace("deny:", "  - {from: ['tag:family-*'], to: [coordinator]}\ndeny:"))
    assert _can("a-1", "coordinator")


def test_deny_beats_allow(families):
    _write(families, FAMILIES.replace("deny:\n  - {from: [tag:family-c], to: [tag:family-b]}",
                                       "deny:\n  - {from: [coordinator], to: [tag:family-b]}"))
    assert not _can("coordinator", "b-1") and _can("coordinator", "c-1")


def test_adding_a_bot_is_one_tag(families):
    store.upsert_agent(_agent("a-3"))
    assert not _can("a-1", "a-3")
    store.upsert_agent(_agent("a-3", "family-a"))
    assert _can("a-1", "a-3") and _can("a-3", "a-1") and _can("coordinator", "a-3")


def test_no_policy_file_means_beacon_only(families):
    (families / "policy.yaml").unlink()
    assert not _can("a-1", "a-2") and not _can("coordinator", "a-1")
    assert mesh.may_call(mesh.BEACON_PEER, store.get_agent("a-1"))
    assert mesh.config("a-1")["outbound"] == []
    assert set(mesh.config("a-1")["inbound"]) == {"beacon"}


def test_star_rule_allows_everyone(families):
    _write(families, 'rules:\n  - {from: ["*"], to: ["*"]}\n')
    assert _can("loner", "a-1") and _can("a-1", "b-1")


def test_peer_lists_show_only_allowed_peers(families):
    names = {p["name"] for p in mesh.config("a-1")["outbound"]}
    assert {"a-2", "a-2-mailbox"} <= names
    assert not names & {"b-1", "c-1", "coordinator", "loner"}
    assert set(mesh.config("a-1")["inbound"]) == {"beacon", "a-2", "coordinator"}
    assert {p["name"] for p in mesh.config("coordinator")["outbound"]} >= {"a-1", "b-1", "c-1"}


def test_callers_still_narrows_what_the_policy_allows(families):
    store.upsert_agent(Agent.model_validate({"id": "a-2", "type": "hermes", "host": "h1", "tags": ["family-a"],
        "reach": {"endpoint": "http://a-2.test:9900", "network": "tailnet", "callers": ["coordinator"]}}))
    assert _can("coordinator", "a-2") and not _can("a-1", "a-2")


def test_mailbox_refuses_a_call_the_policy_does_not_allow(client, families):
    body = {"jsonrpc": "2.0", "id": "r", "method": "SendMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "hi"}], "messageId": "m"}}}
    refused = client.post("/a2a/b-1", json=body, headers={"Authorization": f"Bearer {mesh.token('a-1', 'b-1')}"})
    assert refused.status_code == 403
    assert refused.json()["error"]["message"] == "b-1 does not accept calls from a-1"


def test_board_assignment_follows_the_policy(families):
    with pytest.raises(ValueError, match="does not accept work"):
        board.create_card("a-1", "x", assignee="b-1")
    assert board.create_card("coordinator", "x", assignee="b-1")["assignee"] == "b-1"


def test_a_late_reply_arrives_from_beacon_even_when_the_policy_is_one_way(families):
    """coordinator may call a-1, a-1 may not call coordinator: its late reply must still get through."""
    message_id = mailbox.enqueue("coordinator", "a-1", "message", "status?")
    claim = mailbox.claim_for_pull("a-1")
    assert claim[0]["token"] == mesh.token("coordinator", "a-1")  # a message arrives as its sender

    assert mailbox.report("a-1", message_id, True, "all fine") == "coordinator"
    reply = mailbox.claim_for_pull("coordinator")
    assert [r["token"] for r in reply] == [mesh.token(mesh.BEACON_PEER, "coordinator")]  # not a-1's token
    assert "Reply from a-1" in reply[0]["text"]


def test_problems_catch_typos_and_empty_selectors(families):
    _write(families, "tags: [family-a]\nrules:\n  - {from: [tag:famly-a], to: [nobody]}\n")
    store.upsert_agent(_agent("b-1", "family-b"))
    found = "\n".join(policy.problems(policy.load(), store.list_agents()))
    assert "tag 'famly-a' is not declared" in found
    assert "no agent with id 'nobody'" in found
    assert "agent 'b-1' has undeclared tag 'family-b'" in found


def test_invalid_policy_files_are_rejected(families):
    for bad in ("rules:\n  - {from: [], to: [x]}\n", "rules:\n  - {from: ['Bad Name'], to: ['*']}\n",
                "rules:\n  - {from: ['*'], to: ['*'], extra: 1}\n", "tags: ['Not Valid']\n"):
        _write(families, bad)
        with pytest.raises(ValueError):
            policy.load()


def test_matrix_and_api_show_the_whole_picture(client, families):
    grid = policy.matrix(policy.load(), store.list_agents())
    assert sorted(grid["coordinator"]) == ["a-1", "a-2", "b-1", "c-1"]
    assert grid["a-1"] == ["a-2"] and grid["loner"] == []

    body = client.get("/api/policy").json()
    assert body["present"] is True and body["matrix"]["c-1"] == []
    assert body["problems"] == [] and body["policy"]["rules"][0]["from"] == ["tag:family-a"]


def test_cli_prints_the_matrix_and_exits_1_on_problems(families, capsys):
    assert mesh.main(["policy"]) == 0
    out = capsys.readouterr().out
    assert "coordinator" in out and "yes" in out
    (families / "policy.yaml").unlink()
    assert mesh.main(["policy"]) == 1
    assert "no fleet/policy.yaml" in capsys.readouterr().out


def test_agent_tags_must_be_valid_names():
    with pytest.raises(ValueError):
        _agent("x", "Not Valid")
