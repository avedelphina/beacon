from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import auth, store
from .drivers import get_driver
from .schemas import Agent, Host


class FixRequest(BaseModel):
    fix: str


class DecommissionRequest(BaseModel):
    purge: bool = False
    remove_user: bool = False

app = FastAPI(title="Beacon", version="0.1.0")

app.include_router(auth.router)
app.add_middleware(auth.AuthMiddleware)
# Outermost — added last, so it runs first and request.session exists by
# the time AuthMiddleware reads it.
app.add_middleware(SessionMiddleware, secret_key=auth.SESSION_SECRET, https_only=False)


@app.exception_handler(store.NotFound)
def not_found(_request, exc: store.NotFound):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(store.InvalidId)
def invalid_id(_request, exc: store.InvalidId):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(ValueError)
def bad_value(_request, exc: ValueError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/hosts")
def list_hosts() -> list[Host]:
    return store.list_hosts()


@app.get("/api/hosts/{id_}")
def get_host(id_: str) -> Host:
    return store.get_host(id_)


@app.put("/api/hosts/{id_}")
def put_host(id_: str, host: Host) -> Host:
    if host.id != id_:
        raise HTTPException(400, "body id must match URL id")
    return store.upsert_host(host)


@app.delete("/api/hosts/{id_}")
def delete_host(id_: str) -> dict:
    store.delete_host(id_)
    return {"ok": True}


@app.get("/api/agents")
def list_agents() -> list[Agent]:
    return store.list_agents()


@app.get("/api/agents/{id_}")
def get_agent(id_: str) -> Agent:
    return store.get_agent(id_)


@app.put("/api/agents/{id_}")
def put_agent(id_: str, agent: Agent) -> Agent:
    if agent.id != id_:
        raise HTTPException(400, "body id must match URL id")
    return store.upsert_agent(agent)


@app.delete("/api/agents/{id_}")
def delete_agent(id_: str) -> dict:
    store.delete_agent(id_)
    return {"ok": True}


@app.get("/api/agents/{id_}/status")
def get_agent_status(id_: str) -> dict:
    agent = store.get_agent(id_)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    return driver.status(agent, host)


@app.get("/api/agents/{id_}/logs")
def get_agent_logs(id_: str, lines: int = 200) -> dict:
    agent = store.get_agent(id_)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    return {"text": driver.logs(agent, host, lines=lines)}


@app.post("/api/agents/{id_}/deploy")
def deploy_agent(id_: str) -> StreamingResponse:
    agent = store.get_agent(id_)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    gen = driver.deploy(agent, host)

    # Pull the first chunk here, outside the StreamingResponse body, so a
    # validation error (e.g. bad install_mode) raises synchronously and comes
    # back as a clean 400 instead of surfacing mid-stream after a 200 already
    # went out.
    try:
        first = next(gen)
    except StopIteration:
        first = None

    def body():
        if first is not None:
            yield first + "\n"
        for line in gen:
            yield line + "\n"

    return StreamingResponse(body(), media_type="text/plain")


@app.get("/api/agents/{id_}/reconcile")
def reconcile_agent(id_: str) -> list[dict]:
    agent = store.get_agent(id_)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    return driver.reconcile(agent, host)


@app.post("/api/agents/{id_}/reconcile")
def apply_fix(id_: str, body: FixRequest) -> dict:
    agent = store.get_agent(id_)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    return driver.apply_fix(agent, host, body.fix)


@app.get("/api/agents/{id_}/config-diff")
def get_config_diff(id_: str) -> dict:
    agent = store.get_agent(id_)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    return driver.config_diff(agent, host)


@app.post("/api/agents/{id_}/config-diff")
def push_config(id_: str) -> StreamingResponse:
    agent = store.get_agent(id_)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    gen = driver.push_config(agent, host)

    try:
        first = next(gen)
    except StopIteration:
        first = None

    def body():
        if first is not None:
            yield first + "\n"
        for line in gen:
            yield line + "\n"

    return StreamingResponse(body(), media_type="text/plain")


@app.post("/api/agents/{id_}/decommission")
def decommission_agent(id_: str, body: DecommissionRequest) -> StreamingResponse:
    agent = store.get_agent(id_)
    host = store.get_host(agent.host)
    driver = get_driver(agent.type)
    gen = driver.decommission(agent, host, purge=body.purge, remove_user=body.remove_user)

    # Same synchronous-first-chunk trick as deploy: a validation error (e.g.
    # purge on the default profile) raises here as a clean 400, before any
    # streaming response has gone out.
    try:
        first = next(gen)
    except StopIteration:
        first = None

    def body_stream():
        exit_code = None

        def emit(line: str) -> str | None:
            nonlocal exit_code
            if line.startswith("__BEACON_EXIT__"):
                exit_code = line.removeprefix("__BEACON_EXIT__")
                return None
            return line + "\n"

        if first is not None and (out := emit(first)):
            yield out
        for line in gen:
            if out := emit(line):
                yield out

        # The decommission script never uses `set -e` and swallows its own
        # step failures with `|| echo`, so its exit code is 0 whenever ssh
        # actually reached the host and ran it to completion — anything else
        # (255 = ssh-level failure, none = timeout/no ssh binary) means we
        # have no real confidence teardown happened, so leave the record be.
        if exit_code == "0":
            store.archive_agent(id_)
            yield "[beacon] record archived to fleet/decommissioned/\n"
        else:
            yield "[beacon] teardown didn't complete cleanly — record left in place, nothing archived\n"

    return StreamingResponse(body_stream(), media_type="text/plain")


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
