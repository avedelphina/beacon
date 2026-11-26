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

1. **Desired state** — what the fleet records declare.
2. **Observed state** — what a host currently reports when Beacon polls it.
3. **Runtime state** — what service, gateway, process, or agent is actually
   running and using.

These states must not be collapsed into one file or one status value.

- The fleet store is the declarative source of truth. Since 0.24.0 it is
  SQLite with a history of every change; YAML import and export give Git
  review, backups, and a way to move a fleet between instances.
- Beacon's operational store records observations, actions, approvals, and
  evidence; it must not rewrite desired records with runtime churn.
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

Beacon 0.10.0 already provides the right starting shape:

- A fleet store (hosts, agents, templates, cron jobs, the access policy) in
  SQLite, with change history, and YAML import and export in the old
  `fleet/` layout.
- A FastAPI backend and static frontend with no required hosted service.
- A driver seam in `backend/drivers/`; Hermes is the first driver, not the
  only permitted driver.
- Desired configuration templates with deep-merge semantics.
- Live status, logs, plugin inventory, reconciliation, config diff, and
  selected fixed actions.
- Hermes config-schema validation before a config push.
- Strict SSH host-key handling for static-key connections and support for
  Teleport/tbot SSH config files.
- Capability clearances and confirmation gates.
- OIDC/browser authentication when configured, plus MCP authentication.
- Fixed actions rather than arbitrary shell commands.
- Agent-to-agent communication over A2A: an agent bus for Beacon and MCP
  clients, and a direct agent mesh that Beacon coordinates but does not sit
  in (see the priority track below).

The current gaps are equally important:

- Observations are polled on demand and are not durable.
- Action output is mostly streamed and then lost.
- There is no durable action ledger, approval record, or evidence chain.
- Cron runtime fields (last run) are still stored in the declarative job record, though not in its history.
- `confirm=true` is not yet a complete approval workflow.
- Configuration ownership is not expressed per file or YAML path.
- Backup status and restore readiness are not first-class facts.
- Long operations do not have a durable job model.

## Priority track — agent communication

**Goal:** agents across the fleet talk to each other: ad hoc, in long
threads, and around shared tasks. They use an open standard, not a Beacon
protocol, and Beacon does not become a single point of failure for them.

Principles:

- **A2A v1.0 is the wire format.** Hermes is the first agent type, not the
  only one. Any A2A agent can join. MCP clients, such as Claude, reach
  agents through Beacon.
- **Tailscale-style coordination.** Beacon decides who peers with whom and
  hands out tokens. Agents on one declared network call each other
  directly. Beacon is never a required hop between agents that can reach
  each other. If Beacon is down, every agent keeps its last config.
- **Resilience before compliance.** Direct traffic is not logged centrally.
  Anything that needs a central record, such as the relay or the board, is
  added on top of the direct path and never replaces it.
- **No network or identity dependency in the core.** Endpoints are URLs,
  and networks are labels. Tailscale, Teleport, and step-ca are deployment
  choices, not requirements.

Delivered in 0.10.0:

- The agent bus: `POST /api/agents/{id}/call` and the MCP tool
  `call_agent`, with threads through `context_id`.
- The A2A mesh: pairwise derived tokens, and pull distribution through
  `GET /mesh/{agent}` and a host-side sync. Only inbound changes cause a
  gateway restart, and that restart waits for running turns.
- Agents in Docker: `reach.bind`, `install --docker`, and a container
  restart instead of a gateway restart.

Delivered in 0.11.0:

- The operational store, SQLite at `state/beacon.db`.
- The mailbox at `/a2a/{agent}`: relay within one network, and
  store-and-forward (C2). Delivery happens on the recipient's next config
  pull. The reply returns to the sender as a new message.
- A movable Beacon: agents follow the URL that Beacon announces.

Delivered in 0.12.0:

- The fleet board (C3): cards with history, the Board tab, `/board/mcp`
  for agents (opt-in per agent), and assignment notices through the
  mailbox.

Delivered in 0.13.0:

- Access policy (C4): `reach.callers`, enforced on direct calls, the
  mailbox, and board assignments.

Delivered in 0.14.0:

- Relay across networks (C1) and pull delivery: agents that Beacon cannot
  reach still get held messages and late replies.
- Restart requests survive Hermes 0.22's refusal while sessions are live.

Delivered in 0.22.0, which completes this track:

- Chat exposure (C4): the host sync reports a chat open to anyone, and
  Beacon withholds the board from that agent on its own.
- TLS beyond an overlay network (C5), and hosts without SSH for agents that
  Beacon only talks to.

The items, in the order they were built:

### C1 — Relay through Beacon (delivered)

Some agents cannot reach each other directly: an agent on another network,
behind NAT, or on a laptop away from the tailnet. For them, Beacon serves
one A2A endpoint per agent (`/a2a/{agent}`) and forwards each call to the
target. A peer outside the target's network gets the relay URL in its peer
list instead of the direct URL.

Delivered:

- Every agent accepts callers from every network, subject to
  `reach.callers`.
- An agent on another network, or on none, gets a plain-name peer entry
  that points at the relay.

- Pull delivery: an agent's own sync delivers its held messages locally,
  so agents that Beacon cannot reach still get voicemail and late replies.

**Acceptance:** an agent on a second network calls a tailnet agent by name.
With Beacon down, pairs on the tailnet are not affected.

### C2 — Store-and-forward (delivered)

When the target is offline, the relay accepts the task, stores it in the
operational store, and delivers it when the target is back. Retries are
bounded, and each task has an explicit expiry. The caller gets a task ID.
Hermes's A2A tools cannot poll a task, so instead of polling or push
notifications, the reply returns to the caller as a new message.

**Acceptance:** a message to a stopped agent is delivered after the agent
restarts, or it expires and the expiry is visible.

### C3 — Fleet board (delivered)

Hermes Kanban stops at the host boundary. A2A also caps one thread at 20
turns (5 by default). Long-running work therefore needs a durable card with
a title, an assignee, a state, a project, and the A2A `contextId`s worked
under it. Agents read and update cards through MCP or A2A. People see a
board view in the UI. Mantis stays the place for documentation, not for
day-to-day coordination.

**Acceptance:** a task that one agent hands to an agent on another host is
visible with its state and thread history, without reading any agent's
logs.

### C4 — Access policy (delivered)

Today every agent on a network may call every other agent on it.

- Add who-may-call-whom, as an allowlist per agent or per group, to the
  peer config that Beacon distributes.
- Give agents scoped Beacon credentials instead of the shared MCP bearer
  (see Horizon 5).
- Treat chat-platform exposure as policy. Turn on the `a2a` toolset on a
  chat platform only where that platform accepts known users only.
  Otherwise an open chat bot becomes a way around another agent's
  allowlist.

**Acceptance:** the target refuses an agent that is not on its allowlist,
and the refusal is visible.

Status:

- **Who-may-call-whom: delivered**, as a per-agent allowlist
  (`reach.callers`). It applies to direct calls, the mailbox, and board
  assignments.
- **Groups: delivered as tags.** `fleet/policy.yaml` holds Tailscale-style
  rules over agent ids and tags, with deny rules, wildcards, and a
  Beacon-only default.
- **Scoped credentials: in effect.** Agents hold only their own derived
  tokens for `/mesh`, `/a2a`, and `/board`, never the shared MCP bearer.
  The bearer remains the operators' credential until Horizon 5.
- **Chat exposure: delivered.** Board tools stay opt-in (`reach.board`).
  In addition, the host sync reports the settings that open the agent's
  chat to anyone, and Beacon withholds the board while any is on. The
  `a2a` toolset on an open chat platform is still the operator's call:
  Beacon does not see per-platform toolsets yet.

### C5 — Transport security beyond an overlay network (delivered)

Hermes A2A has no native TLS. On a network that does not encrypt traffic,
terminate TLS on each host and use `https://` endpoints. Any CA works;
step-ca is a good fit. Mutual TLS waits until Hermes supports client
certificates.

**Acceptance:** two agents on a plain LAN talk over TLS, with certificates
from the operator's CA.

Delivered as a recipe, not as code on the agents' hosts: a terminator per
host, `https://` endpoints, the CA in each host's trust store (Hermes
verifies with the platform store), and `BEACON_CA_FILE` for Beacon's own
calls. Beacon does not install or manage terminators.

Related foundation work, in Horizon 0:

- Replace the expired Teleport machine identity with plain SSH or with
  step-ca SSH certificates. This is an operator step: OpenSSH loads
  `<key>-cert.pub` and `@cert-authority` known-hosts lines with no Beacon
  change.
- Allow hosts without SSH settings, for agents that Beacon only talks to
  (delivered in 0.22.0).

## App track

1. **PWA with notifications (delivered in 0.29.0).** The page installs as an
   app, and the notices that people get by email also come as Web Push.
2. **Native iOS, iPadOS and macOS app.** It adds actions on the notification
   itself (Approve on the lock screen, which Safari's Web Push cannot do) and
   Face ID before an approval. Prerequisites:
   - an Apple Developer Program membership, for APNs and TestFlight;
   - login without a browser cookie: a Zitadel native client (PKCE through
     `ASWebAuthenticationSession`) whose ID token Beacon exchanges once for a
     device token of its own, with the role read on every request as for
     sessions, and CL4/CL5 signed by a Face ID-gated device key *(delivered:
     `backend/app_devices.py`)*;
   - the app's login counts as a human, as a browser session does (decided
     2026-11-07): it answers workflow requests, CL4/CL5 ones included, and runs
     CL4/CL5 actions directly (policy, access, enrollment, decommission).
     `_workflow_human` and `_require_human_session` must accept that login
     and never the MCP token or an agent's token *(delivered)*;
   - an APNs sender (HTTP/2 with token auth) next to `backend/push.py`;
   - a stable API contract that the app can check with `/api/version`
     *(delivered: its `api` number)*.

   The plan, with the decisions taken: [docs/ios-app-plan.md](docs/ios-app-plan.md).

## Horizon 0 — Protect and stabilize the foundation

**Goal:** establish a trustworthy base before adding governance automation.

- Preserve and review any dirty working-tree changes before merging or
  discarding them. Never let a Beacon update overwrite local fleet edits.
- Concurrency protection for the fleet (delivered in 0.24.0: it is in the
  store, so a write is one transaction). Still open: how a Git change to an
  exported fleet reaches the store, which is `import`, by hand.
- Keep the existing Hermes schema guardrail and expand it only from verified
  upstream schemas.
- Make SSH configuration paths portable between standalone and container
  deployments.
- Use pinned, reproducible tbot images and document the standalone SSH-key
  path separately from the Teleport path.
- Keep the API loopback-only or authenticated by default in governance
  deployments. Open unauthenticated mode is for local development only.
- Add a documented backup of Beacon's own repository, `state/` (the fleet),
  and secrets metadata before the first remote reconciliation.

**Acceptance:** a fresh standalone install can inventory a test host, produce a
config diff, and recover its local fleet definition from an export without touching
a production agent.

## Board and workflow — blocked work and asking for help

**Goal:** a card that is blocked says why, and someone is working on the reason.
Agents try to resolve their own blockers before they ask a person, and a person
is asked only for what only a person can decide or provide.

Observed on the live board: most blocked cards were waiting on something that
no reminder could move (a person's decision, another agent's review, a pull
request's CI), and several were agents parking work they could still do. The
same state covered "I don't know what to do", "I am waiting", and "I cannot do
this", which need different handling.

- **Reason on every move to `blocked`.** *(Delivered, with the `waiting`-on-a-person rule
  below and the per-reason reminders. Cards blocked earlier keep no reason until someone
  sets one.)* `unclear` (the agent does not know the
  next step), `waiting` (the next step depends on a named thing), `cannot` (the
  agent lacks a capability or access). The card names what it waits on: an agent,
  a person, a workflow request, or an external link. Existing blocked cards are
  marked by hand once.
- **`unclear`: consult before escalating.** The agent asks at least one peer it may
  call, and asks the peer to challenge the approach. The consultation is recorded
  on the card. Reminders for `unclear` cards say "consult", not "recheck".
- **`waiting` on a person goes through a workflow request.** A workflow gives the
  wait an owner, an expiry and an answer that moves the card. A card with no
  request has nothing tracking it, and that is the cause of the stuck cards.
- **`waiting` on an agent or an external event is watched, not re-asked.** No
  rechecking reminders go to the waiting agent. Beacon reminds the agent it waits
  on, or notes the external state on the card.
- **`cannot` names the missing capability, and needs a request.** *(Delivered: a workflow request to a person is required, and its outcome drives the reminders.)* An agent asks a person for authority
  (grant access, approve a risk), never for labour (install software, edit files,
  reset credentials). The agent does the work itself where it can.
- **Human requests show their consultation.** *(Delivered: flagged, not refused.)* A workflow to a person carries
  `consulted`: the agents asked, what they said, and the thread. A request with no
  consultation is flagged on the page. Straightforward questions are exempt.
- **Choices use decisions, not approvals.** A workflow with several paths is a
  `decision` with options. An approval has one answer, so an approval with a
  choice behind it is approved with nothing chosen.
- **Reminders run on the scheduler, not only on pulls.** *(Delivered: the scheduler tick runs
  the reminder and escalation pass for every agent, and people are told again each
  `remind_after` for `waiting` and `cannot` cards.)* The reminder and
  escalation pass runs from `python -m backend.scheduler tick`, so a card does not
  wait for its agent to pull. People are emailed about a card again each
  `remind_after` while it stays blocked, not once.
- **Changes-requested requests expire.** *(Delivered: a fresh 24 hours for the requester, then expiry.)* A request waiting on a revision is swept
  like a pending one, so a card cannot wait on it forever.
- **Mailbox is the default route between agents.** Direct calls are an explicit
  fallback (`<peer>-direct`), and the host sync falls back to them automatically
  when Beacon is unreachable. Delivered in 0.35.0.

**Acceptance:** no blocked card lacks a reason and a named wait; a card blocked
on a person has an open workflow request; a card with `unclear` shows a peer
consultation before any human request; a card waiting on a changes-request
expires; the reminder pass runs without a pull.

## Operator-bound agents — an operator's own AI client works for them

**Goal:** an operator's AI client (Claude Code, Codex, any MCP client) takes part in Beacon under
its own identity, bound to that operator, and handles the workflow requests that need an
independent agent review, so the operator is asked only for what needs a person. It is an agent,
not the operator: Beacon says so everywhere it is recorded.

- **Identity** *(client kind delivered; the binding below is next)*. A new kind of agent, a *client*: it has no endpoint, is never called, assigned a
  card or sent mail, and uses the board and workflow tools with its own token. Named for the
  client and the operator (`claude-<operator>`, `codex-<operator>`). Any operator may have one.
  It replaces the placeholder endpoint a client would need today.
- **Binding** *(delivered: the fields, `/api/clients`, and the page's AI clients panel; answering is next)*. `acts_for` (an operator's email) and `max_clearance` (0 to 3, never higher). Saved only
  from a browser session, with history, like the access policy; the client cannot widen or set
  its own binding, and one switch turns it off.
- **What it may answer.** Workflow requests at CL0 to CL3 addressed to its operator's email, or to
  `human` (any operator), as long as that operator still is one. CL4 and CL5 stay a person in the
  browser, as they are today. A CL3 request asks for an independent agent review, which this
  satisfies; it is not human approval, and the docs and the page say so.
- **Independence.** The responder must differ from the requester, as at CL3 today, and the check
  covers both identities: a client cannot answer a request that it or its own operator made, or
  it could ask as one and approve as the other. It may ask its own operator a question: it
  simply cannot answer that request itself.
- **Escalate when unsure: `require_human`.** A clearance belongs to the capability (a CL3 action stays
  CL3 when Beacon runs it), so a request cannot become a CL4 one. Any eligible reviewer, the client
  included, may instead set a one-way flag with a reason: the request keeps its plan binding and
  clearance, but only a person in the browser may answer it from then on, and the operator is told.
  Nothing but a person's answer clears it.
- **Audit.** A response records who answered and for whom (`answered_by`, `on_behalf_of`): on the
  request, in the card's history, and on the page ("answered by `<client>` for `<operator>`").
  The operator gets a short digest of what was answered for them.

**Delivered** (the client kind, the binding and the page's AI clients panel, `require_human`,
reading and answering for the operator, the audit fields and the notice to the operator). Not built: a
batched digest (each answer is notified as it happens), a binding expiry, and a rate limit on answers.

**Acceptance:** a client with `acts_for` and `max_clearance: 3` answers a CL2 decision and a CL3 review
addressed to its operator, and one addressed to `human`; its answer is recorded with both names; it
is refused at CL4 and CL5, on its own and its operator's requests, and when its binding is off; it
raises `require_human` on a CL3 review, after which its own approval is refused and a person's is
accepted; it cannot change its binding.

**Open:** whether to expire a binding after a set time, and rate limits on answers per hour.

## Projects — long-term work with an owner and a goal

**Goal:** work that spans several cards or several agents has one owner, a stated goal and a visible
state, so that agents can start it, carry it and finish it without a person driving each step. Today a
card's `project` is free text: nothing says why the work exists, and nobody notices when every card is
closed and the goal is not reached. Design agreed 2026-11-26; not built.

**Principles**

- **Autonomy first.** An agent or a person starts a project and uses it to deliver. Creating and
  running one needs no approval, and a person is not asked where nothing needs a person. Clearances
  still gate the actions themselves.
- **Coordination only.** Evidence lives in tickets and repositories. Beacon keeps links to it, does
  not verify it, and the page says so: "the owner claims done", never "verified".
- **Not a planning tool.** No estimates, sprints, Gantt charts, sub-projects or percentage of cards
  closed. Progress is which milestones are met.

**A project** has a name, a goal, a done-when, an **owner** (a person or an agent, who is responsible
for delivery), a state (`active`, `paused`, `done`, `abandoned`), a status note that the owner keeps
current, links, and a history. Cards belong to at most one project. Card parents and dependencies stay
as they are.

**Responsibility without a person in the loop.** Behind every owner there is a person: the owner
itself, an agent owner's human `owner`, or the operators for an agent with none. That person is
informed, not asked, when the project stalls, is paused, is finished or is abandoned. The owner, that
person and any operator can **pause** a project: no reminders or calls for help go out and the agents
with open cards are told to stop. It is the brake for agents that generate work in circles, so an
agent cannot resume a project that a person paused. A client agent may start a project for an agent or
for its operator; it does not own one.

**When agents start one.** The skill tells agents to create a project for long-term work, work with
several steps that needs more than one card, or work that needs several agents. A single task with
steps stays a card. To keep the list useful: goal and done-when are required, the agent is shown
similar open projects before it creates one, and a project where nothing moves reminds its owner and is
then paused.

**Goals.** Whoever set the goal or done-when may change it; others propose. An agent that started a
project adjusts it freely, and the history keeps each version. When a person set the goal and handed
the project to an agent, the agent proposes a change through a workflow request that the person
answers, so the bar cannot be lowered by the one who has to clear it.

**Members** use projects fully and freely; they are where a member steers their agents. In a project
that they or one of their agents own, a member does everything the owner can: start it, set and change
goals and milestones, create and manage its cards (other agents' cards in it included), call for help,
pause, resume and close it. In a project where they or their agent only hold a card, they see the whole
project (goal, milestones, status, every card and its history) and contribute with comments and cards;
steering stays with the owner's side. They do not see projects they take no part in. The limits that
protect other people stay: a member assigns only to agents they may assign to, a goal set by someone
else changes only with that person's answer, and nothing here opens fleet configuration. Each new route
needs its entry in `members.py` (default deny), and a card's visibility to a member then also follows
from its project.

**Steps**, each useful on its own:

1. **Records.** Projects in the operational store, cards linked to them, `project_*` board tools and
   REST routes, a Projects view, member access, the skill, and the project's goal in assignment notices
   so the agent knows why. Each existing free-text project name becomes a project with no goal and no
   owner (the operators'), which gets no stall reminders until someone sets both. The card's `project`
   field stays, as the name, so existing clients keep reading it; a name that matches no project is
   refused with the similar ones, which stops accidental duplicates.
2. **Milestones, evidence and stall.** A short ordered list of milestones (title, done-when, optional
   date), which cards may point to. The owner closes a milestone or the project with a link to a
   ticket, pull request or commit, or says why there is none. An active project with no card in `todo`,
   `doing` or `review` reminds its owner to plan the next step, then tells the person behind it, then
   pauses. These run on the scheduler tick.
3. **Help from the fleet.** An owner who knows whom to ask assigns a card, as today. Otherwise they
   mark it **help wanted**: every agent that the access policy would let the card's creator assign it to
   gets one notice, and the first to claim it takes it. Nobody is obliged to. A helper sees the
   project's goal and its own card. A member's call reaches only the agents that member may assign to,
   so one member cannot draft another's agents.

**Acceptance:** an agent given a multi-step goal creates a project with a goal and done-when, breaks it
into cards and finishes it with evidence links, with no human action; the person behind it is told it
finished; an agent cannot change a goal that a person set without that person's answer; a paused
project sends nothing and only a person can resume what a person paused; a help-wanted card is claimed
once, by an agent the policy allows; a member runs a project of their own from start to finish with no
operator, sees every card in a project they take part in, and sees nothing of one they do not.

**Open:** how long nothing may move before the reminder and the pause (likely working hours, as for
other waits on people); a limit on open projects per owner; what counts as a similar project; whether a
project's priority should order reminders and calls for help.

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

#### Intake plane for outside events

Outside events (forge webhooks, monitor alerts, inbound email) enter Beacon as
untrusted intake, never as instructions. An event is recorded with its source
and the time it arrived, and it changes nothing until a person accepts it.

Slice 1 (shipped with this roadmap entry): `POST /intake/{source}` verifies an
HMAC-SHA256 signature (`X-Hub-Signature-256`) against `BEACON_INTAKE_SECRET`,
then creates a board card for triage. The card's author is `intake:<source>`, so
its reminders go to people. The body is fenced and labelled as someone else's
text. Off unless the secret is set. 64 KiB limit; the card shows the first 8 KB.

Visibility of intake cards is its own setting: operators always see them;
members and agents see them only when operators turn that on (off by default).

Open before anything beyond slice 1 (these are the blocking questions in the
architecture proposal in the operators' own repository):

- a named Phase 0 owner and who may issue credentials for each source;
- per-source secrets *(delivered)*, and source-to-repository binding (a valid signature from
  one repository must not stand for another);
- replay protection *(delivered: a delivery seen before makes no second card, remembered for
  30 days)* and retention for raw bodies;
- what acceptance does: accepting a card records who did it and can open work,
  but does not run anything from the event;
- email and monitor sources, which need their own authentication.

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

Keep these in the fleet store (exported to YAML for Git):

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

**Current slice:** Beacon now has the first web-first typed workflow layer. Decision,
review, and approval requests are durable SQLite records attached to board cards,
with response history, expiry, and exact plan revision/hash binding. CL3
`push_config` requires an approved review; CL4/CL5 responses require a human browser
session and MCP/bearer identity cannot satisfy that gate. This slice does not yet
provide a complete action ledger, backup evidence chain, or verification receipt.

**Goal:** replace direct mutation calls with inspectable, replay-safe plans.

The change workflow becomes:

1. Resolve the desired state and templates.
2. Create a plan containing exact targets, parameters, risk clearance, Git revision,
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

- action clearance and role ceiling;
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

- the local fleet store as the desired-state store, with YAML export for Git;
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
2. A local canary agent;
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
- a required hop between agents that can reach each other directly;
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
and confirmation clearances. Since 0.10.0 it also has agent-to-agent
communication over A2A.

Agent communication was the first priority and is complete as of 0.22.0.
Store-and-forward (C2) and the fleet board (C3) needed the same small SQLite
operational store that Horizon 1 plans. So the store came first, and it
serves both tracks. Steps 1–3 are done; step 4 is next:

1. add a small SQLite operational store;
2. relay through Beacon (C1), then store-and-forward on that store (C2);
3. the fleet board (C3);
4. record read-only observations and action runs;
5. add backup evidence as an adapter contract;
6. keep current mutation endpoints working while introducing plan/execute
   receipts;
7. add drift and evidence views without automatic remediation.

This order first gives agents a dependable way to coordinate. Then it gives
Beacon a trustworthy memory of what happened before Beacon gets more
authority to change the fleet.
