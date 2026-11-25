"""Clearance registry — maps each mutating Beacon capability to how much
autonomy it's allowed: can it just run, does it need a second look, does it
need a human.

Only CL2's step is actually enforced today: every capability at CL2 or above
requires an explicit confirm=true before it runs. CL3-CL5 are *named* here so
the risk is visible and queryable (API, GUI, MCP tool descriptions), but
nothing yet checks that a review or approval actually happened before
confirm=true was sent — there's no reviewer or approval-chain mechanism
wired up. Naming the clearance is meant to be a step toward enforcing it, not a
claim that it's enforced already — see ROADMAP.md.

Clearance assignments below are Beacon's own judgment calls about each
capability's risk and reversibility, not a settled standard — the rationale
string on each one says why it landed where it did.
"""

from enum import IntEnum


class Clearance(IntEnum):
    CL0 = 0  # Observe — read-only, always allowed
    CL1 = 1  # Propose — plan only, not executed
    CL2 = 2  # Execute reversible — autonomous, notify after
    CL3 = 3  # Execute with review
    CL4 = 4  # Execute with human approval
    CL5 = 5  # Human-only


CLEARANCE_LABELS: dict[Clearance, str] = {
    Clearance.CL0: "Observe",
    Clearance.CL1: "Propose",
    Clearance.CL2: "Execute reversible",
    Clearance.CL3: "Execute w/ review",
    Clearance.CL4: "Execute w/ human approval",
    Clearance.CL5: "Human-only",
}

# capability -> (clearance, rationale)
CAPABILITY_CLEARANCES: dict[str, tuple[Clearance, str]] = {
    "status": (Clearance.CL0, "Read-only observability."),
    "logs": (Clearance.CL0, "Read-only observability."),
    "list_plugins": (Clearance.CL0, "Read-only observability."),
    "reconcile_check": (Clearance.CL0, "Dry-run diagnosis — makes no changes."),
    "config_diff": (Clearance.CL0, "Read-only comparison; secret values never leave the host."),
    "restart": (
        Clearance.CL2,
        "Reversible, low-risk, everyday operate action — the service comes back up on its "
        "own, nothing is lost.",
    ),
    "restart_agent": (
        Clearance.CL3,
        "Hard restart of the whole agent (container, or the systemd unit without Hermes's "
        "graceful drain) — drops live sessions, so a step above the gateway restart.",
    ),
    "apply_fix": (
        Clearance.CL2,
        "Reconcile's findings are all reversible-by-design fixes (restart, orphaned-unit "
        "cleanup) — same bracket as restart.",
    ),
    "push_config": (
        Clearance.CL3,
        "Config change plus a conditional restart — worth a second look before it runs, "
        "since Beacon doesn't yet distinguish critical vs. non-critical config keys.",
    ),
    "update_plugin": (
        Clearance.CL3,
        "Pulls new, unreviewed code onto the host — routine, but not something that should "
        "run silently.",
    ),
    "deploy": (
        Clearance.CL3,
        "Installs a new gateway (simple/add-profile/new-user). Meaningful footprint on the "
        "host, but a fresh install with nothing yet depending on it.",
    ),
    "apply_template": (
        Clearance.CL2,
        "Adds a template name to agent records — fleet YAML only, touches no host. "
        "Reversible by removing the name; the config it implies still passes through "
        "push_config's own CL3 gate before it reaches a host.",
    ),
    "set_template": (
        Clearance.CL2,
        "Saves or deletes a template — a record in the fleet store, touching no host. "
        "Reversible from the history; what it implies still passes through push_config's "
        "own CL3 gate before it reaches a host.",
    ),
    "enroll_agent": (
        Clearance.CL4,
        "Mints a single-use code that makes Beacon hand the agent's pull token (inside its "
        "install script) to whoever presents it within 15 minutes. That is the agent's "
        "identity for the mesh, so the LLM-facing MCP token must not be able to mint one. "
        "The board's enroll_request/enroll_claim reach the same code only through an approval "
        "a person gave in a browser session.",
    ),
    "set_policy": (
        Clearance.CL4,
        "Changes who may call whom across the whole fleet, and restarts every agent whose "
        "set of allowed callers changes. The MCP token must not be able to widen its own "
        "reach, so this needs a human browser session.",
    ),
    "create_client": (
        Clearance.CL4,
        "Makes an identity that reads and answers workflow requests for an operator, and mints its token. "
        "That is access, so the LLM-facing MCP token must not be able to create one: a person in a browser "
        "session, and only for themselves.",
    ),
    "mint_client_token": (
        Clearance.CL4,
        "A new token for a client agent: whoever holds it acts as that client. Shown once. A person in a "
        "browser session, and only the operator the client acts for.",
    ),
    "update_client": (
        Clearance.CL4,
        "Raises what a client may answer (`max_clearance`, never above CL3) or turns it back on. A person in a "
        "browser session, and only the operator the client acts for.",
    ),
    "disable_client": (
        Clearance.CL2,
        "Turns a client off, or revokes one of its tokens: it only takes access away, reversible by the "
        "operator it acts for. Any operator may, so a lost token can be stopped by whoever notices.",
    ),
    "set_agent_access": (
        Clearance.CL4,
        "Tags, `reach.callers`, and `reach.board` decide who may call whom and who may use "
        "the board, so changing them is a policy change by another route. Human browser "
        "session only, like the policy editor.",
    ),
    "auto_heal": (
        Clearance.CL2,
        "The host sync restarts a gateway that stopped answering its own probe: the everyday fix, "
        "reversible, gracefully (never cutting a running session), at most once an hour and three "
        "times a day, then it stops and tells a person. Runs only where the fleet policy says `heal`, "
        "and saving the policy is human-only (CL4): that choice is the standing approval.",
    ),
    "update_agent": (
        Clearance.CL4,
        "Escalated above deploy/update_plugin's CL3: this touches the shared code checkout, "
        "affecting every profile on that install, not just one agent.",
    ),
    "decommission": (
        Clearance.CL4,
        "Baseline case (no purge, no remove_user): stops and uninstalls a service. "
        "Escalates to CL5 below when purge or remove_user is requested.",
    ),
}

DECOMMISSION_CL5: tuple[Clearance, str] = (
    Clearance.CL5,
    "purge and/or remove_user requested — irreversible data or account deletion.",
)


def clearance_for(capability: str, **params: object) -> tuple[Clearance, str]:
    """Look up (clearance, rationale) for a capability. A few capabilities' clearance
    depends on request parameters — currently only decommission, where
    purge/remove_user escalate CL4 -> CL5.
    """
    if capability == "decommission" and (params.get("purge") or params.get("remove_user")):
        return DECOMMISSION_CL5
    if capability not in CAPABILITY_CLEARANCES:
        raise KeyError(f"no clearance registered for capability {capability!r}")
    return CAPABILITY_CLEARANCES[capability]


def requires_confirm(clearance: Clearance) -> bool:
    """Every capability that actually executes something (CL2+) requires an
    explicit confirm=true. This was previously enforced only by mcp/server.py
    as a courtesy to its own callers; every mutating endpoint in app.py now
    enforces it directly, so a plain HTTP call gets the same plan-before-apply
    step an MCP client always had. CL0/CL1 never reach this — there's nothing
    to execute.
    """
    return clearance >= Clearance.CL2
