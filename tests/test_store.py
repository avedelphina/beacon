import pytest

from backend import store
from tests.conftest import make_agent, make_host


def test_list_hosts_empty_when_no_file(fleet):
    assert store.list_hosts() == []


def test_upsert_and_get_host(fleet):
    store.upsert_host(make_host(id="edge-01"))
    got = store.get_host("edge-01")
    assert got.id == "edge-01"
    assert got.address == "10.0.0.1"


def test_upsert_host_overwrites_by_id(fleet):
    store.upsert_host(make_host(id="edge-01", user="deploy"))
    store.upsert_host(make_host(id="edge-01", user="root"))
    hosts = store.list_hosts()
    assert len(hosts) == 1
    assert hosts[0].ssh.user == "root"


def test_get_missing_host_raises_not_found(fleet):
    with pytest.raises(store.NotFound):
        store.get_host("nope")


def test_upsert_host_rejects_bad_id(fleet):
    with pytest.raises(store.InvalidId):
        store.upsert_host(make_host(id="Not_Valid!"))


def test_delete_host(fleet):
    store.upsert_host(make_host(id="edge-01"))
    store.delete_host("edge-01")
    assert store.list_hosts() == []


def test_delete_missing_host_raises(fleet):
    with pytest.raises(store.NotFound):
        store.delete_host("nope")


def test_agent_requires_existing_host(fleet):
    with pytest.raises(store.NotFound):
        store.upsert_agent(make_agent(id="a1", host="no-such-host"))


def test_upsert_and_get_agent(fleet):
    store.upsert_host(make_host(id="edge-01"))
    store.upsert_agent(make_agent(id="a1", host="edge-01"))
    got = store.get_agent("a1")
    assert got.id == "a1"
    assert got.host == "edge-01"


def test_list_agents_sorted_by_id(fleet):
    store.upsert_host(make_host(id="edge-01"))
    store.upsert_agent(make_agent(id="b-agent", host="edge-01"))
    store.upsert_agent(make_agent(id="a-agent", host="edge-01"))
    ids = [a.id for a in store.list_agents()]
    assert ids == ["a-agent", "b-agent"]


def test_delete_missing_agent_raises(fleet):
    with pytest.raises(store.NotFound):
        store.delete_agent("nope")


def test_archive_agent_moves_record_out_of_live_fleet(fleet):
    store.upsert_host(make_host(id="edge-01"))
    store.upsert_agent(make_agent(id="a1", host="edge-01"))

    store.archive_agent("a1")

    with pytest.raises(store.NotFound):
        store.get_agent("a1")
    assert (fleet / "decommissioned" / "a1.yaml").exists()


def test_archive_missing_agent_raises(fleet):
    with pytest.raises(store.NotFound):
        store.archive_agent("nope")
