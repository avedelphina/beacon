"""Bringing an agent that already runs into the mesh.

A person in the web UI asks for an enrollment code for an agent. The code is
single-use and good for 15 minutes; the command it goes in
(`curl .../enroll/<code> | bash`) is run on the agent's host and fetches that
agent's install script, which holds its pull token. So the token never passes
through a browser, a clipboard or a terminal history, and it works for hosts
Beacon has no SSH to. Only a hash of the code is stored.

`status()` is the checklist the UI shows while the host catches up. It is read
from what Beacon already records (pulls of /mesh/{agent}, the A2A probe, the
chat report), so there is nothing for the host to say twice.
"""

import hashlib
import secrets
import time

from . import availability, mesh, opstore, store, workflow

CODE_TTL_SECONDS = 15 * 60
CAPABILITY = "enroll_agent"


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create(agent_id: str, docker: str | None, docker_user: str, actor: str) -> dict:
    """A new code for `agent_id`. Raises ValueError when no script could be made
    for it now (no reach block, no Beacon URL or secret), so a code that cannot
    work is never handed out."""
    mesh.install_script(agent_id, docker or None, docker_user)
    code = secrets.token_urlsafe(24)
    now = time.time()
    with opstore.connect() as db:
        db.execute("INSERT INTO enrollments (code_hash, agent, docker, docker_user, created_by, created_at, expires_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (_hash(code), agent_id, docker or None, docker_user, actor, now,
                                                    now + CODE_TTL_SECONDS))
    return {"code": code, "expires_at": now + CODE_TTL_SECONDS}


def redeem(code: str) -> tuple[str, str] | None:
    """(agent id, install script) for a valid code, once; None for anything
    else, with no hint which (unknown, used, expired). The script is made
    before the code is used up, so a failure to make it costs nothing."""
    now = time.time()
    with opstore.connect() as db:
        row = db.execute("SELECT * FROM enrollments WHERE code_hash = ?", (_hash(code),)).fetchone()
    if row is None or row["fetched_at"] is not None or row["expires_at"] < now:
        return None
    try:
        script = mesh.install_script(row["agent"], row["docker"], row["docker_user"] or "hermes")
    except (store.NotFound, ValueError, mesh.MissingSecret):
        return None
    with opstore.connect() as db:
        pulls = db.execute("SELECT pulls FROM agent_seen WHERE agent = ?", (row["agent"],)).fetchone()
        # Compare-and-set: two fetches at once cannot both win.
        used = db.execute("UPDATE enrollments SET fetched_at = ?, pulls_at_fetch = ? "
                          "WHERE code_hash = ? AND fetched_at IS NULL", (now, pulls["pulls"] if pulls else 0,
                                                                         row["code_hash"])).rowcount
    return (row["agent"], script) if used else None


def _plan(requester: str, target: dict) -> dict:
    """The text a person approves and its binding, made from the target alone.
    claim() makes it again, so a request made by hand through workflow_create
    with other words (or another target) than the ones shown is refused."""
    agent_id, docker = target["agent"], target["docker"]
    where = f" in Docker container {docker!r}" if docker else ""
    title = f"Enroll {agent_id} in the Beacon mesh"
    summary = (f"{requester} asks for a one-time command (15 minutes, works once) that installs the Beacon mesh sync for "
               f"{agent_id}{where}. Approving lets {requester} receive it; the command carries {agent_id}'s pull token "
               f"in the script it fetches. Reject if you did not expect this.")
    return {"title": title, "summary": summary,
            **workflow.plan_binding({"capability": CAPABILITY, "tier": 4, "title": title, "summary": summary,
                                     "options": [], "target": target})}


def _check_requester(requester: str, agent_id: str) -> None:
    if requester != mesh.BEACON_PEER and requester != agent_id:
        raise workflow.UnauthorizedWorkflowAccess("an agent can ask to enroll only itself")


def request_enrollment(requester: str, card_id: int, agent_id: str, docker: str | None, docker_user: str,
                       schedule: bool) -> dict:
    """The second way in: an agent (for itself) or an MCP client (for any agent)
    asks, a person approves in the web UI (a T4 approval workflow), and the
    requester then claims the command with claim(). The person is the gate the
    HTTP route's session check is for; the requester never needs a session."""
    _check_requester(requester, agent_id)
    store.get_agent(agent_id)
    mesh.install_script(agent_id, docker or None, docker_user)  # ValueError: could not work, so do not ask
    target = {"agent": agent_id, "docker": docker or None, "docker_user": docker_user, "schedule": schedule}
    plan = _plan(requester, target)
    return workflow.create(card_id=card_id, kind="approval", requester=requester, eligible_responder="human",
                           capability=CAPABILITY, tier=4, title=plan["title"], summary=plan["summary"], options=None,
                           target=target, plan_revision=plan["plan_revision"], plan_hash=plan["plan_hash"])


def claim(request_id: int, actor: str) -> dict:
    """The command for an approved enrollment request, once, to its requester."""
    request = workflow.claim_approved(request_id, actor=actor, capability=CAPABILITY)
    target = request["target"]
    try:
        if set(target) != {"agent", "docker", "docker_user", "schedule"}:
            raise workflow.WorkflowError("this request was not made by enroll_request")
        _check_requester(actor, target["agent"])
        if _plan(actor, target)["plan_hash"] != request["plan_hash"]:
            raise workflow.WorkflowError("this request was not made by enroll_request")
        made = create(target["agent"], target["docker"], target["docker_user"], f"{actor}, workflow {request_id}")
    except Exception:
        workflow.mark_execution_outcome(request_id, actor, "failed")
        raise
    workflow.mark_executed(request_id, actor)
    return {"agent": target["agent"], "expires_at": made["expires_at"],
            "command": command(made["code"], bool(target["docker"]), target["schedule"])}


def command(code: str, docker: bool, schedule: bool) -> str:
    """What to run on the host. A Docker agent's script needs docker access,
    so it runs as root; the other kind runs as the user who owns the agent."""
    flags = " -- --schedule" if schedule else ""
    return f"curl -fsS {mesh.public_url()}/enroll/{code} | {'sudo ' if docker else ''}bash -s{flags}"


def status(agent_id: str) -> dict:
    """The checklist for one agent: each step done or not, with what to do or
    what was seen. Probes the agent's A2A endpoint, so it can take a few seconds."""
    agent = store.get_agent(agent_id)
    now = time.time()
    with opstore.connect() as db:
        enrollment = db.execute("SELECT * FROM enrollments WHERE agent = ? ORDER BY created_at DESC LIMIT 1",
                                (agent_id,)).fetchone()
        seen = db.execute("SELECT pulls FROM agent_seen WHERE agent = ?", (agent_id,)).fetchone()
    total = seen["pulls"] if seen else 0
    fetched = enrollment is not None and enrollment["fetched_at"] is not None
    # Pulls since the script was fetched; before any enrollment, all of them.
    pulls = total - (enrollment["pulls_at_fetch"] or 0) if fetched else total
    waiting = enrollment is not None and not fetched and enrollment["expires_at"] >= now
    expired = enrollment is not None and not fetched and not waiting
    reach = agent.reach is not None
    avail = availability.check_all([agent])[agent_id]
    chat = mesh.open_chats().get(agent_id)

    def step(key: str, label: str, done: bool, detail: str = "") -> dict:
        return {"key": key, "label": label, "done": done, "detail": detail}

    steps = [
        step("record", "Agent record with an A2A endpoint", reach,
             agent.reach.endpoint if reach else "Edit the agent and fill in its A2A endpoint and network."),
        step("command", "Enrollment command run on the host", fetched or total > 0,
             "" if fetched or total > 0 else
             "Waiting for the command to be run." if waiting else
             "The code expired; create a new one." if expired else "Create an enrollment command."),
        step("sync", "First sync pulled the mesh config", pulls >= 1, "" if pulls >= 1 else "Waiting for the first sync."),
        step("schedule", "Sync repeats on a schedule", pulls >= 2,
             f"{pulls} pulls seen" if pulls >= 2 else "Waiting for a second pull, about 5 minutes after the first. "
             "If none comes, the cron line was not added."),
        step("a2a", "Agent answers over A2A", avail["state"] == "online", avail["detail"] or avail["state"]),
        step("chat", "Chat status reported", chat is not None,
             ("not reported yet: the host needs the current sync script" if chat is None else
              f"chat open to anyone ({', '.join(chat)}): the board stays withheld" if chat else "chat is closed")),
    ]
    return {"agent": agent_id, "steps": steps, "done": all(s["done"] for s in steps),
            "enrollment": "waiting" if waiting else "used" if fetched else "expired" if expired else "none",
            "expires_at": enrollment["expires_at"] if waiting else None}
