"""Tier registry — maps each mutating Beacon capability to how much
autonomy it's allowed: can it just run, does it need a second look, does it
need a human.

Only T2's step is actually enforced today: every capability at T2 or above
requires an explicit confirm=true before it runs. T3-T5 are *named* here so
the risk is visible and queryable (API, GUI, MCP tool descriptions), but
nothing yet checks that a review or approval actually happened before
confirm=true was sent — there's no reviewer or approval-chain mechanism
wired up. Naming the tier is meant to be a step toward enforcing it, not a
claim that it's enforced already — see ROADMAP.md.

Tier assignments below are Beacon's own judgment calls about each
capability's risk and reversibility, not a settled standard — the rationale
string on each one says why it landed where it did.
"""

from enum import IntEnum


class Tier(IntEnum):
    T0 = 0  # Observe — read-only, always allowed
    T1 = 1  # Propose — plan only, not executed
    T2 = 2  # Execute reversible — autonomous, notify after
    T3 = 3  # Execute with review
    T4 = 4  # Execute with human approval
    T5 = 5  # Human-only


TIER_LABELS: dict[Tier, str] = {
    Tier.T0: "Observe",
    Tier.T1: "Propose",
    Tier.T2: "Execute reversible",
    Tier.T3: "Execute w/ review",
    Tier.T4: "Execute w/ human approval",
    Tier.T5: "Human-only",
}

# capability -> (tier, rationale)
CAPABILITY_TIERS: dict[str, tuple[Tier, str]] = {
    "status": (Tier.T0, "Read-only observability."),
    "logs": (Tier.T0, "Read-only observability."),
    "list_plugins": (Tier.T0, "Read-only observability."),
    "reconcile_check": (Tier.T0, "Dry-run diagnosis — makes no changes."),
    "config_diff": (Tier.T0, "Read-only comparison; secret values never leave the host."),
    "restart": (
        Tier.T2,
        "Reversible, low-risk, everyday operate action — the service comes back up on its "
        "own, nothing is lost.",
    ),
    "apply_fix": (
        Tier.T2,
        "Reconcile's findings are all reversible-by-design fixes (restart, orphaned-unit "
        "cleanup) — same bracket as restart.",
    ),
    "push_config": (
        Tier.T3,
        "Config change plus a conditional restart — worth a second look before it runs, "
        "since Beacon doesn't yet distinguish critical vs. non-critical config keys.",
    ),
    "update_plugin": (
        Tier.T3,
        "Pulls new, unreviewed code onto the host — routine, but not something that should "
        "run silently.",
    ),
    "deploy": (
        Tier.T3,
        "Installs a new gateway (simple/add-profile/new-user). Meaningful footprint on the "
        "host, but a fresh install with nothing yet depending on it.",
    ),
    "apply_template": (
        Tier.T2,
        "Adds a template name to agent records — fleet YAML only, touches no host. "
        "Reversible by removing the name; the config it implies still passes through "
        "push_config's own T3 gate before it reaches a host.",
    ),
    "update_agent": (
        Tier.T4,
        "Escalated above deploy/update_plugin's T3: this touches the shared code checkout, "
        "affecting every profile on that install, not just one agent.",
    ),
    "decommission": (
        Tier.T4,
        "Baseline case (no purge, no remove_user): stops and uninstalls a service. "
        "Escalates to T5 below when purge or remove_user is requested.",
    ),
}

DECOMMISSION_T5: tuple[Tier, str] = (
    Tier.T5,
    "purge and/or remove_user requested — irreversible data or account deletion.",
)


def tier_for(capability: str, **params: object) -> tuple[Tier, str]:
    """Look up (tier, rationale) for a capability. A few capabilities' tier
    depends on request parameters — currently only decommission, where
    purge/remove_user escalate T4 -> T5.
    """
    if capability == "decommission" and (params.get("purge") or params.get("remove_user")):
        return DECOMMISSION_T5
    if capability not in CAPABILITY_TIERS:
        raise KeyError(f"no tier registered for capability {capability!r}")
    return CAPABILITY_TIERS[capability]


def requires_confirm(tier: Tier) -> bool:
    """Every capability that actually executes something (T2+) requires an
    explicit confirm=true. This was previously enforced only by mcp/server.py
    as a courtesy to its own callers; every mutating endpoint in app.py now
    enforces it directly, so a plain HTTP call gets the same plan-before-apply
    step an MCP client always had. T0/T1 never reach this — there's nothing
    to execute.
    """
    return tier >= Tier.T2
