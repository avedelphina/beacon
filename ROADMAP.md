# Roadmap

Beacon runs one person's fleet, on their own machine, over their own
identity provider. This isn't a growth-stage SaaS roadmap — "first-class"
here means three specific things, in order:

1. **Trustworthy unattended.** Reconcile and status only run when someone
   asks. A broken agent can sit undiscovered indefinitely.
2. **Safe by default.** Mistakes should be hard to make and easy to undo.
   Confirm-gating exists on the risky verbs, but there's no audit trail of
   who did what, and no role that can look without being able to break.
3. **Actually generalizes.** The driver seam is the whole design
   premise — Hermes first, not only. It has exactly one implementation and
   no proof it holds up against a second, different kind of agent.

Horizons are ordered by dependency, not just priority — later ones assume
earlier ones landed.

## Horizon 0 — Harden what exists

**Goal:** stop finding bugs by testing on real agents in production.

**Done:** a test suite (`tests/`, ~130 tests) covering the driver layer's
script generation and validation, the tier registry, and confirm-gating as
callers actually hit it — a fake `ssh` module that records the command it
was asked to run instead of touching a real host, so deploy/reconcile/
restart/decommission all get exercised without network access. Also caught
a real bug along the way: `SSHConfig`'s validator only checked "at least
one of key/config_file", not "exactly one" — both set silently worked, with
`config_file` winning and `key` becoming dead weight. CI
(`.github/workflows/ci.yml`) runs the suite plus a build of all three
Docker images on every push.

Still open:

- **Concurrency safety on `fleet/*.yaml`.** Currently a single-writer
  assumption with no locking. Fine solo; not fine the day a second person
  edits the fleet folder.
- **Portable `ssh.config_file` paths.** Currently an absolute path mirrored
  between host and container — works on exactly one machine.
- **A real tbot sidecar image**, pinned rather than built from
  `install.sh` inside a from-scratch Dockerfile at every build.

## Horizon 1 — Operate unattended

**Goal:** Beacon tells you something's wrong before you go looking for it.

- **Scheduled reconciliation.** Today it only runs when a human clicks
  Check in the GUI.
- **Alerting hook** (webhook / ntfy / Slack) for new critical findings.
- **Durable audit log of Beacon's own actions** — who ran what, on which
  agent, when, with what result. Distinct from Teleport's transport-level
  session log, which doesn't know what Beacon actually did.
- **Roles: viewer / operator / admin.** Every logged-in user currently has
  the same blast radius — full deploy/decommission/config-push power, no
  read-only option. Note: this is a *human*-identity axis, distinct from
  the tier registry (`backend/tiers.py`, added 0.5.0) — tier is
  per-operation risk (restart vs. decommission), role is per-caller
  privilege. They compose (an "operator" might be allowed T2-T3 but not
  T4-T5) rather than one replacing the other. Also distinct from — and not
  precluded by — the "no enterprise-grade RBAC" non-goal below: per-
  operation tier gating isn't a permissions matrix, it's a risk
  classification each capability already carries regardless of who's
  calling.
- **Agent-caller identity distinct from the human OIDC session and the
  flat `BEACON_MCP_TOKEN`.** Needed for the tier model's T3 step (a second
  reviewer) to mean anything — today every MCP-authenticated caller is
  indistinguishable from every other, so Beacon has no way to tell a
  proposing agent from a reviewing one. The target shape is scoped
  per-agent capability credentials, not one shared token.
- **Bulk actions** across the fleet table — reconcile everything, restart
  everything with drift — instead of one agent at a time. First slice
  landed: config templates (`fleet/templates/`, `POST
  /api/templates/{name}/apply`) push a shared model/config stack to a batch
  of agents. Still one-at-a-time: reconcile, restart, deploy.
- **Per-agent history / timeline** of deploys, restarts, and config
  changes, beyond scrolling git log by hand.

## Horizon 2 — Scale the architecture

**Goal:** prove the design holds past one driver and one browser tab.

- **Background job model for long operations.** Deploy and `update_agent`
  block on a streamed HTTP response — close the tab, lose the progress
  view, no retry.
- **A second driver**, genuinely different from Hermes, to prove the
  abstraction generalizes rather than assert it.
- **Reconsider fleet-state storage** — SQLite for live state/audit once
  YAML-file concurrency actually becomes a bottleneck, keeping YAML+git as
  the declarative source of truth rather than replacing it.
- **Multi-fleet / workspace separation**, if a genuinely separate context
  shows up and one flat `fleet/` directory stops being the right shape.

## Horizon 3 — Polish & distribute

**Goal:** make it nice to live with long-term, not just correct.

- **Mobile-conscious quick-status view** — a "is anything on fire" check
  shouldn't need a laptop.
- **Real secrets lifecycle.** `env_keys` only checks presence by name today
  — no rotation, no vault integration.
- **Packaging beyond one machine's docker-compose** — a real deploy story
  for wherever this ends up living long-term.

## Start here

Tests and CI landed — see Horizon 0. The remaining Horizon 0 items
(concurrency safety, portable `ssh.config_file` paths, a real tbot image)
are all small and independent; after those, Horizon 1's roles/audit-log
work is what actually needs the safety net now in place under it.

## Deliberately not doing

- **Multi-tenant SaaS.** Beacon runs one person's fleet under their own
  identity — not building account isolation for strangers.
- **Enterprise-grade RBAC.** Three roles plus tag scoping covers a
  family-sized fleet — not building a permissions matrix.
- **Scale beyond a personal fleet.** YAML+git and a handful of Docker
  services is the right size for tens to low hundreds of agents — not
  pre-optimizing for an order of magnitude more.
