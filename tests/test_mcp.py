import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def mcp_server(monkeypatch):
    mcp_dir = Path(__file__).parents[1] / "mcp"
    auth_spec = importlib.util.spec_from_file_location("auth", mcp_dir / "auth.py")
    auth_module = importlib.util.module_from_spec(auth_spec)
    monkeypatch.setitem(sys.modules, "auth", auth_module)
    auth_spec.loader.exec_module(auth_module)

    spec = importlib.util.spec_from_file_location("beacon_mcp_server", mcp_dir / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    original_path = sys.path[:]
    repo_root = str(mcp_dir.parent)
    sys.path[:] = [entry for entry in sys.path if entry not in ("", repo_root)]
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = original_path
        sys.modules.pop(spec.name, None)


def test_mcp_path_identifiers_validate_before_http(mcp_server, monkeypatch):
    async def no_request(*args, **kwargs):
        raise AssertionError("invalid MCP input must not make an HTTP request")

    monkeypatch.setattr(mcp_server, "_get", no_request)
    monkeypatch.setattr(mcp_server, "_post", no_request)

    for value in ("../templates/admin", "a/b", "a%2fb", "white space", "UPPER"):
        with pytest.raises(ValueError, match="agent id"):
            asyncio.run(mcp_server.get_agent(value))

    with pytest.raises(ValueError, match="template name"):
        asyncio.run(mcp_server.get_template("../admin"))

    with pytest.raises(ValueError, match="plugin name"):
        asyncio.run(mcp_server.update_plugin("agent-1", "../../plugin", confirm=True))


def test_mcp_path_identifiers_allow_valid_ids_and_use_encoded_segments(mcp_server, monkeypatch):
    calls = []

    async def record_get(path, **params):
        calls.append(("get", path, params))
        return {"desired": {}}

    async def record_post(path, **kwargs):
        calls.append(("post", path, kwargs))
        return {}

    monkeypatch.setattr(mcp_server, "_get", record_get)
    monkeypatch.setattr(mcp_server, "_post", record_post)

    assert asyncio.run(mcp_server.get_agent("agent-1")) == {"desired": {}}
    assert asyncio.run(mcp_server.get_template("base-stack")) == {"desired": {}}
    assert asyncio.run(mcp_server.update_plugin("agent-1", "plugin.v2", confirm=True)) == {}

    assert calls == [
        ("get", "/api/agents/agent-1", {}),
        ("get", "/api/templates/base-stack", {}),
        ("post", "/api/agents/agent-1/plugins/plugin.v2/update?confirm=true", {}),
    ]


def test_mcp_bearer_helper_is_the_constant_time_auth_helper(mcp_server):
    assert mcp_server.is_valid_bearer("Bearer token", "token")
    assert not mcp_server.is_valid_bearer("Bearer other", "token")
