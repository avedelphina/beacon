# Roadmap

Beacon is a local-first fleet control plane for agent runtimes.

The short version is **“Intune for agents”**: declarative desired state,
fleet inventory, drift detection, controlled remediation, rollout rings, and
evidence. The important qualification is that Beacon is not an enterprise
MDM clone. It must remain useful as a standalone tool for one person running a
small fleet, while also integrating cleanly with an ecosystem such as DEEP,
Gitea, Borg, Teleport, and Hermes.

Beacon is public software. The core must not assume CQRE, OCEAN, Hermes, a
particular identity provider, Borg, or a particular Git forge. Those are
adapters and integrations. The standalone path must work with local YAML,
SSH, ordinary Git, local backups, and a browser. The ecosystem path adds
Gitea, Teleport, Borg, OIDC, Mantis/Paperclip, and fleet-wide policy without
changing the core operating model.

## Operating model

Beacon observes three related states:

1. **Desired state** — what the checked-in fleet configuration declares.
2. **Observed state** — what a host currently reports when Beacon polls it.
3. **Runtime state** — what service, gateway, process, or agent is actually
   running and using.

These states must not be collapsed into one file or one status value.

- Git/YAML is the declarative source of truth.
- Beacon's operational store records observations, actions, approvals, and
  evidence; it must not rewrite desired YAML with runtime churn.
- The host remains authoritative for local runtime facts.
- Borg or another backup system remains authoritative for recoverable data.
- Beacon verifies and links evidence from those systems; it does not replace
  them.

The governing loop is:

```text
observe -> compare -> plan -> approve when needed -> snapshot -> apply
-> verify -> record evidence
```

Automation should be deterministic and typed. Agents and operators retain
judgment within their declared authority. Beacon must never become an
arbitrary remote shell with a dashboard attached.

## Current foundation

Beacon 0.9.0 already provides the right starting shape:

- Git-diffable YAML inventory in `fleet/hosts.yaml`,
  `fleet/agents/*.yaml`, `fleet/templates/*.yaml`, and
  `fleet/cron_jobs/*.yaml`.
- A FastAPI backend and static frontend with no required hosted service.
- A driver seam in `backend/drivers/`; Hermes is the first driver, not the
  only permitted driver.
- Desired configuration templates with deep-merge semantics.
- Live status, logs, plugin inventory, reconciliation, config diff, and
  selected fixed actions.
- Hermes config-schema validation before a config push.
- Strict SSH host-key handling for static-key connections and support for
  Teleport/tbot SSH config files.
- Capability tiers and confirmation gates.
- OIDC/browser authentication when configured, plus MCP authentication.
- Fixed actions rather than arbitrary shell commands.

The current gaps are equally important:

- Observations are polled on demand and are not durable.
- Action output is mostly streamed and then lost.
- There is no durable action ledger, approval record, or evidence chain.
- Cron runtime fields are mixed into Git-backed declarative YAML.
- `confirm=true` is not yet a complete approval workflow.
- Configuration ownership is not expressed per file or YAML path.
- Backup status and restore readiness are not first-class facts.
- Long operations do not have a durable job model.

## Horizon 0 — Protect and stabilize the foundation

**Goal:** establish a trustworthy base before adding governance automation.

- Preserve and review any dirty working-tree changes before merging or
  discarding them. Never let a Beacon update overwrite local fleet edits.
- Add concurrency protection for `fleet/*.yaml` and define how Git changes,
  UI changes, and scheduled actions interact.
- Keep the existing Hermes schema guardrail and expand it only from verified
  upstream schemas.
- Make SSH configuration paths portable between standalone and container
  deployments.
- Use pinned, reproducible tbot images and document the standalone SSH-key
  path separately from the Teleport path.
- Keep the API loopback-only or authenticated by default in governance
  deployments. Open unauthenticated mode is for local development only.
- Add a documented backup of Beacon's own repository, `fleet/`, and secrets
  metadata before the first remote reconciliation.

**Acceptance:** a fresh standalone install can inventory a test host, produce a
config diff, and recover its local fleet definition from Git without touching
a production agent.

## Horizon 1 — Make observation first-class

**Goal:** Beacon tells the operator what is wrong without requiring a human to
click every agent.

Add a read-only collector and durable operational store. SQLite is the
initial choice; it complements rather than replaces YAML+Git.

Minimum records:

- **Observation** — agent, collector version, observed time, freshness/expiry,
  normalized result, and payload digest.
- **Evidence** — typed source, timestamps, redacted payload or external
  reference, and retention metadata.
- **BackupEvidence** — backup system, snapshot/reference ID, last successful
  backup, repository reachability, and last restore-test timestamp.
- **AgentDesiredRevision** — Git commit, content digest, resolved template
  digest, and schema/driver versions used for comparison.

Collect, where the driver supports it:

- agent and host identity;
- Hermes/runtime version and commit;
- gateway service state, PID, uptime, and health;
- active config digest and declared-versus-live drift;
- plugin and provider inventory;
- memory-provider identity and scope configuration without collecting secret
  values;
- backup freshness and restore-test age;
- connectivity and evidence freshness.

Status must distinguish `compliant`, `drifted`, `unhealthy`, `unreachable`,
`stale`, and `unknown`. An unreachable host must never appear compliant merely
because its last observation was healthy.

Keep scheduled collection external and simple: system cron, launchd,
Kubernetes CronJob, or a future Beacon worker may invoke a collector. Beacon
should not require a permanently running scheduler for standalone use.

**Acceptance:** a scheduled read-only collection produces durable evidence,
never mutates a host, and visibly distinguishes stale data from current
compliance.

### Osquery-inspired observation model

Beacon borrows the useful parts of osquery's operating model, not its SQL
surface or its central-control assumptions. The goal is trustworthy,
low-overhead evidence about an agent deployment.

#### Enrollment and stable node identity

Inventory IDs and SSH targets are not sufficient as the only long-lived
identity: hosts move, addresses change, and profiles are recreated. Add an
optional per-installation node identity with:

- installation ID, host/profile identity, and enrollment time;
- credential generation, revocation state, and last-seen time;
- declared driver and collection capabilities;
- a separately tracked stable identity even when host addressing changes.

Standalone deployments may use a locally generated identity plus SSH trust.
Ecosystem deployments may bind it to Teleport, mTLS, or an enrollment adapter.
Enrollment is optional; Beacon must still work with ordinary inventory and SSH.

#### Versioned probe packs with discovery

Evolve templates into reusable, versioned **probe packs**: collections of
read-only checks with a cadence, resource budget, retention class, and explicit
applicability. A pack should report its ID/version, collector version, checks
executed, checks skipped and why, results, and evidence timestamps.

```yaml
id: hermes-gateway-health
version: 1.2.0
applies_when:
  agent_type: hermes
  platform: [linux, darwin]
checks:
  - gateway_service
  - runtime_version
  - config_digest
  - memory_scope
```

Applicability must be discoverable from the node where possible, rather than
requiring a perfect central inventory. A database probe should not run on a
host without the database; a container probe should not pretend a bare-metal
host is non-compliant because Docker is absent.

#### Baselines, differential observations, and periodic snapshots

Use a full baseline on first observation, record later additions/removals as
structured differentials, and take periodic full snapshots. Every observation
needs a stable subject, collector/pack version, sequence number or counter,
timestamp, and digest. A lost local collector state, policy change, or explicit
reset creates a new baseline.

This makes drift useful and legible: provider changed, service disappeared,
PID changed, plugin version changed, or a protected config key appeared or
vanished. Differentials reduce storage and alert noise; snapshots preserve
recovery and audit value.

#### Event sources with polling fallback

Support event-derived evidence where a platform exposes it, while retaining
polling as the portable baseline. Candidate adapters include systemd/launchd
service events, protected-file watchers, container lifecycle events, package
manager events, Hermes gateway lifecycle events, backup completion events, and
Git webhooks.

Events and polls both create observations/evidence. The API must expose source,
freshness, capability, and confidence; it must not imply that every platform
has equivalent real-time coverage.

#### Scheduled probes, not a cron-to-shell bridge

Schedule probe packs with platform/applicability constraints, cadence,
timeout, maximum output, concurrency, retention, and alert policy. Health
observation may run unattended; mutation remains a typed plan/action governed
by the later horizons. Avoid turning `fleet/cron_jobs` into a generic command
scheduler.

#### Safe distributed diagnostics

Offer a small, allowlisted family of read-only diagnostic probes with typed
parameters, output limits, deadlines, and audit records. For a Hermes driver,
examples are effective gateway status, loaded provider identity, configuration
digest, enabled plugin inventory, and recent restart receipt.

Do not introduce arbitrary remote SQL, shell, or distributed write commands.
Mutating work remains subject to plan, approval, backup, execution, and
verification.

#### Buffered delivery and separate evidence streams

Collectors operating on intermittent laptops or VPN-connected hosts need a
bounded local queue with retry/backoff, sequence numbers, deduplication,
maximum disk use, and an explicit dropped-event counter. Never allow unbounded
local evidence growth.

Keep these streams distinct:

- status/health events;
- observation and probe results;
- drift events;
- action lifecycle records;
- audit and security events.

A gateway restart must not be represented as configuration drift, and a failed
SSH connection must not look like a healthy unchanged agent.

#### Cost and safety budgets

Every probe pack/check declares a timeout, output cap, cadence floor,
concurrency limit, platform restriction, sensitivity classification, and
retention class. Collection has a fleet cost; a slow check scheduled across a
fleet is not harmless monitoring.

**Acceptance:** Beacon can run a versioned, applicable read-only probe pack;
record a baseline plus meaningful differentials; keep observations bounded;
and explain whether a result came from an event, a poll, or stale evidence.

## Horizon 2 — Separate desired state from operational state

**Goal:** Git remains reviewable while runtime history becomes queryable.

Keep these in Git/YAML:

- host and agent inventory;
- version/channel pins;
- declared configuration and templates;
- ownership and policy declarations;
- backup requirements;
- enabled integrations.

Move or mirror these into the operational store:

- cron last-run state and output;
- observation history;
- action requests and runs;
- approvals and decisions;
- evidence links;
- deployment receipts;
- job progress and retries.

Do not automatically rewrite declarative YAML with runtime timestamps or
command output. A Git diff should answer “what do we want?”; an operational
query should answer “what happened?”

**Acceptance:** repeated observation and scheduled actions leave the desired
Git worktree clean, while the UI/API can show current and historical runtime
state.

## Horizon 3 — Govern changes with typed plans

**Goal:** replace direct mutation calls with inspectable, replay-safe plans.

The change workflow becomes:

1. Resolve the desired state and templates.
2. Create a plan containing exact targets, parameters, risk tier, Git revision,
   content digests, driver version, and expiry.
3. Show the blast radius and required backup evidence.
4. Obtain approval when the policy requires it.
5. Snapshot or verify backup readiness.
6. Dispatch a fixed typed action.
7. Record before/after observations and evidence.
8. Verify runtime state and publish a deployment receipt.

Initial action vocabulary should remain deliberately small:

- `collect_observation`
- `snapshot_profile`
- `restart_gateway`
- `push_declared_config`
- `deploy_gateway`
- `update_plugin`
- `update_agent`
- `reconcile_fix` with enumerated fix IDs
- `verify_gateway`
- `restore_profile`
- `decommission_agent`

No arbitrary shell commands, shell fragments, or user-authored scripts in
fleet YAML, API requests, or scheduled policies.

An approved plan becomes invalid if its desired revision or target set
changes. This prevents “approve one thing, execute another” races.

Introduce a durable background job model for deploys and updates so closing a
browser does not destroy progress visibility or evidence.

**Acceptance:** every mutation has an immutable request, actor/executor,
exact desired revision, before/after evidence, outcome, and recovery guidance.

## Horizon 4 — Govern configuration ownership

**Goal:** stop agents and tools silently rewriting protected configuration.

Ownership is expressed by file and YAML path, not only by profile:

- **operator-controlled:** provider, memory identity, gateway policy, network
  policy, backup policy, version pin;
- **owner-controlled:** model preferences, selected tools, agent-facing
  behavior where the deployment permits it;
- **agent-proposable:** changes that may be submitted as a proposal but not
  applied silently;
- **runtime-owned:** session state, leases, PID files, generated health data;
- **secret-managed:** credentials and tokens, never returned as values;
- **unmanaged/cache:** ignored unless explicitly adopted.

For every protected change Beacon should be able to show who or what changed
it, when, why, whether it was approved, and whether the running service loaded
it. Drift should initially alert and quarantine, not auto-revert: an automatic
revert during an incident can destroy the very evidence needed to recover.

**Acceptance:** an agent can propose a permitted change, but cannot silently
change protected identity, provider, gateway, backup, or security settings.

## Horizon 5 — Roles, identity, and policy

**Goal:** make the risk model enforceable without recreating enterprise
bureaucracy.

Use a small set of roles rather than a universal permission matrix:

- `viewer` — observe and export evidence;
- `operator` — execute bounded reversible actions;
- `approver` — approve changes above the operator ceiling;
- `admin` — manage policy, credentials, and fleet membership.

Separate human identity from agent identity. Replace the shared MCP bearer
with scoped agent credentials or capability tokens that can propose and read
but cannot self-approve. Human approval must remain out-of-band for high-risk
operations.

Policy should cover:

- action tier and role ceiling;
- proposer/approver separation for higher-risk changes;
- maintenance windows;
- maximum target count and blast radius;
- required backup freshness;
- retry, timeout, and circuit-breaker limits;
- host or fleet tags.

The standalone deployment may use one local operator and explicit confirmation
instead of OIDC. The ecosystem deployment may use OIDC, Teleport identity,
scoped MCP credentials, and external review systems. The core action and
receipt model remains the same.

**Acceptance:** `confirm=true` alone cannot execute a policy-denied action;
changed plans cannot be approved or executed; all denials are auditable.

## Horizon 6 — Backups and bounded automation

**Goal:** make unattended operation safe enough to use while keeping backup
responsibility in the right system.

Beacon is not Borg. Borg, restic, filesystem snapshots, or another integration
remains responsible for backup transport, encryption, retention, pruning, and
restore. Beacon should:

- require or warn on missing pre-change backup evidence;
- record backup snapshot IDs against action requests;
- expose typed snapshot and restore operations through an adapter;
- track restore-test age;
- alert when backup evidence is stale or absent;
- refuse destructive actions when policy says recovery evidence is required.

Start automation with read-only observation and alerting. Later permit only an
explicit allowlist of reversible actions, such as a bounded gateway restart
inside a maintenance window. Automatic config pushes, runtime upgrades,
profile deletion, secret changes, and restores remain approval-required until
there is strong operational evidence.

Scheduled policies must invoke typed actions against bounded selectors. They
must emit one action record per target and support retry limits, backoff, and a
circuit breaker. They must never become a generic cron-to-shell bridge.

**Acceptance:** an automated run cannot exceed its declared target set or
action allowlist, and every target has an action result plus evidence.

## Horizon 7 — Public standalone and ecosystem distribution

**Goal:** make the same governance model useful outside our environment.

Standalone Beacon should support:

- local YAML+Git as the desired-state store;
- ordinary SSH keys and known-hosts;
- local cron/launchd/Kubernetes invocation;
- local auth or one-user operator mode bound to loopback;
- Borg/restic or no backup adapter;
- no dependency on Hermes-specific concepts outside the Hermes driver.

Ecosystem integrations should be optional adapters:

- **Git:** GitHub, GitLab, Gitea, or local repositories;
- **identity:** local operator, OIDC, Teleport/tbot;
- **backup:** Borg, restic, snapshots, or another provider;
- **agent:** Hermes first, then genuinely different drivers;
- **work tracking:** MantisBT, Paperclip, GitHub Issues, or none;
- **notifications:** webhook, email, ntfy, Slack, or none.

Integration adapters must not leak private fleet assumptions into the public
core. CQRE/OCEAN deployment policy belongs in a private instance repository or
adapter configuration, not in the public Beacon repository.

Prove the driver abstraction with a second genuinely different agent type.
The purpose is architectural evidence, not feature count.

**Acceptance:** a user can run Beacon with only Git, SSH, and one driver; an
ecosystem deployment can add identity, backup, and review adapters without a
fork of the core.

## Horizon 8 — Safe fleet rollout

Use rollout rings rather than updating every agent at once:

1. Beacon itself and a disposable test agent;
2. Anikke or another local canary;
3. one low-risk remote agent;
4. family/team canaries;
5. the remaining fleet.

Each ring requires:

- known-good backup evidence;
- clean or understood configuration drift;
- cold start and restart verification;
- gateway and messaging smoke checks;
- memory-scope verification where applicable;
- rollback instructions;
- a bounded observation period.

Stop on the first failed health or verification criterion. A version pin is
not a rollout plan, and a green SSH command is not evidence that the runtime
loaded the intended code.

## Non-goals

Beacon deliberately does not become:

- a multi-tenant SaaS control plane;
- a replacement for Git, Borg, Teleport, Hermes validation, or host
  configuration management;
- an arbitrary remote shell or script runner;
- a secret-value vault or secret exfiltration service;
- a universal enterprise RBAC/approval matrix;
- an automatic revert engine for every drift event;
- a claim of compliance when evidence is stale or the host is unreachable;
- a central authority that exceeds the owner's declared blast-radius boundary.

The design principle is simple:

> Beacon is a typed desired-state reconciler with evidence, not a remote shell
> with a dashboard.

## Existing work and next implementation slice

The repository already has a substantial Horizon 0/early Horizon 1 base: tests,
CI, fixed driver actions, config-schema validation, templates, cron actions,
and confirmation tiers.

Before implementing governance features, keep the current working tree safe
and handle its uncommitted changes separately from this roadmap. The next
focused implementation slice should be:

1. add a small SQLite operational store;
2. record read-only observations and action runs;
3. add backup evidence as an adapter contract;
4. keep current mutation endpoints working while introducing plan/execute
   receipts;
5. add drift and evidence views without automatic remediation.

That sequence gives Beacon a trustworthy memory of what happened before it is
given more authority to change the fleet.
