# Changelog

## [Unreleased]

## [0.26.0] — 2026-10-29

### Added

- **Members: owners see their agents.** Write a person's email as an agent's
  `owner` and that person can log in (their provider must report the email as
  verified); there is no list in the environment. A member sees only the
  agents whose `owner` is their email, in a trimmed form (no endpoint, config, templates or host details),
  and can see whether each answers over A2A, restart its gateway (with the
  usual confirm step), work the board for it (cards assigned to it or made by
  them, comments, state), and answer what their agents ask a person or an
  approval about them. Logs are open to the owner only where an operator
  turned on `member_logs`, because logs of a shared household agent hold other
  people's chats. The UI shows members the Agents, Board and Workflows tabs.
- The server is what refuses: a member's session reaches only the routes in
  `backend/members.py`, and any route not on that list, including one added
  later, is closed to them. Another person's agent answers 404.

### Changed

- **The login is checked on every request**, not only at login: taking an
  email off `BEACON_ALLOWED_EMAILS`, or changing the owner of a member's last
  agent, ends that session at once. An operator who also owns agents stays an
  operator.
- **`owner` and `member_logs` are access fields**: changing them needs a human
  browser session, not the MCP token, as for tags and `reach.board`, because an
  owner now gives a person access. `/auth/me` reports the role. Set each
  agent's owner to the person's email; an old name like `tom` matches nobody.

## [0.25.2] — 2026-10-29

### Added

- **A pending gateway restart is visible.** Hermes reads its A2A listen
  address only when the gateway starts, so an agent enrolled with a new
  address keeps listening on its old one (the default is 127.0.0.1) until the
  restart that the sync asks for goes through, and that restart waits for idle
  chats and is retried on every run. The sync now reports whether a restart is
  still owed (`X-Beacon-Restart`), and when the agent does not answer, its
  availability detail and the enroll checklist say the restart is pending and
  that `hermes gateway restart` on the host does it at once. Hosts need the new
  sync to report it; an older one says nothing and nothing changes.

## [0.25.1] — 2026-10-28

### Fixed

- **A comment on a card assigned to an agent now reaches that agent.** Before,
  only the assignment sent a notice, so a comment sat in the card's history
  and the agent never saw it. The notice goes through the mailbox like the
  assignment's, is recorded as a `comment_notice` event on the card, and is not
  sent when the assignee wrote the comment. A note given with a card update
  counts as a comment. State and field changes still send nothing.
- **The agent table fits its window.** The action buttons were a flex
  container set on a table cell, which pulled the cell out of the table: Delete
  ran off the right edge in Firefox, the buttons shifted from row to row, the
  row borders broke, and a row with a chip was taller than the rest. The cell is
  a table cell again, the page is wider (1280px instead of 1000px), the name and
  its chips stay on one line, and below 1100px the Type column (always the same
  today) is hidden so the actions stay on screen. Rows highlight on hover.

## [0.25.0] — 2026-10-28

### Added

- **Onboarding for agents that already run.** The agent form now has an A2A
  mesh section (endpoint, network, bind, board), and an **Enroll** button
  creates a one-time command for the agent's host:
  `curl .../enroll/<code> | bash -s -- --schedule`. The code works once, for
  15 minutes, and only its hash is stored. It fetches the agent's install
  script, so the pull token never passes through the browser, and it works for
  hosts Beacon cannot SSH into. Creating a code is a human-session action
  (`enroll_agent`, T4).
- **Checklist.** `GET /api/agents/{id}/onboarding` and the Enroll dialog show
  the command run, first sync, schedule (a second pull), A2A answer, and chat
  status, live.
- **`--schedule` for the install scripts.** With the flag, the script adds
  the 5-minute sync to the crontab of whoever runs it, replacing an earlier
  line for the same sync and keeping the rest. Without it, nothing changes.
- Beacon counts pulls per agent (`agent_seen.pulls`), which the checklist
  uses to see the schedule work.
- **Enrollment asked for by an agent or an MCP client.** Two board tools,
  `enroll_request` and `enroll_claim`: the requester makes a T4 approval on a
  card, a person approves it in the web UI, and the requester then claims the
  same one-time command once. An agent can ask only for itself; an MCP client
  (the bearer) for any agent. Claiming checks that the request is Beacon's
  own text for its target, so one made by hand with `workflow_create` is
  refused. The tools need a token, so for an agent that is not yet enrolled
  they serve a client holding the MCP bearer; an agent that already syncs can
  use them to re-install. The agent skill is 1.3.0.

## [0.24.0] — 2026-10-28

### Changed

- **The fleet lives in the database, not in YAML files.** Hosts, agents,
  templates, cron jobs and the access policy are rows in `state/beacon.db`
  (`fleet_records`), so a write is one transaction and nothing depends on a
  `fleet/` directory that a deploy can replace with an old copy. Every change
  is kept with who made it (`fleet_history`, the latest 100 per record; cron
  runs excluded). Closes the "single writer, no locking" limitation.
- **YAML is for import and export.** `python -m backend.fleet export [dir]`
  writes the old `fleet/` layout; `import [dir]` merges one back, checking
  every record first (`--skip-invalid` to import the valid ones). A store
  with no fleet imports `fleet/` once, on its first start, and never reads the
  directory again. Back up `state/` (or run `export` on a schedule).
- The policy is stored as the text you wrote, comments included; its history
  moves from `fleet/policy-history/` to the fleet history.

### Added

- `PUT /api/templates/{name}` and `DELETE /api/templates/{name}` (capability
  `set_template`, T2): templates can now be created and changed through the
  API. A template an agent still uses cannot be deleted.

## [0.23.1] — 2026-10-28

### Fixed

- **Agents did not know they had workflow tools.** The `agent-mesh` skill
  (1.2.0) listed only the board tools. It now explains decisions, reviews, and
  approvals: when to ask, what each field means, which tiers need a person,
  and how to answer a request. Hosts receive it on their next sync, with no
  restart.
- **`workflow_create` no longer asks an agent to invent a SHA-256.**
  `plan_revision` and `plan_hash` are optional on the agent tool. Left out,
  Beacon binds the request to its own fields (capability, tier, title,
  summary, options, target), so a response must name that exact plan. Giving
  only one of the two is refused.

## [0.23.0] — 2026-10-26

### Changed

- **Availability reflects A2A, not SSH.** The status pill in the Agents tab
  now says whether the agent answers over A2A, from a new
  `GET /api/availability` (one call for the whole fleet, probed in
  parallel). `online`: its A2A server answered `/health`. `sync-only`:
  Beacon cannot reach it, but its host synced within 15 minutes, so the
  host is up. `offline`: neither. `no-a2a`: no reach block. A host without
  SSH settings, or one Beacon has no route to, no longer shows
  "unreachable" because of SSH. The systemd service state over SSH stays in
  the Inspect view, labelled as such.

### Added

- Beacon records every mesh pull per agent (`agent_seen` in the operational
  store), the heartbeat behind `sync-only`.

## [0.22.0] — 2026-10-26

Finishes the agent-communication track (C4 chat exposure, C5 TLS).

### Added

- **An open chat withholds the board.** The host sync reports, with every
  pull, which settings let anyone at all message the agent: `*_ALLOW_ALL_USERS`
  flags in the profile `.env` or a Docker agent's container environment, and
  `allow_all_users` in `config.yaml` (names only, header
  `X-Beacon-Open-Chat`). While any is on, Beacon leaves the board out of the
  agent's mesh config and refuses it at `/board/mcp`, even with
  `reach.board: true`, logs a warning, and marks the agent "board withheld"
  in the Agents tab. `GET /api/agents` and `GET /api/agents/{id}` return the
  report as `chat_open`. An older sync reports nothing; then `reach.board`
  decides alone, as before. Update the sync on each host to get this.
- **Hosts without SSH.** `ssh` is now optional on a host. Beacon never logs in
  to such a host; its agents take part in the mesh, mailbox, and board, while
  driver actions refuse with 409 and status shows `unknown`. The host form
  accepts an empty SSH user for this.
- **TLS without an overlay network (C5).** `BEACON_CA_FILE` names your own
  CA's certificate (PEM), trusted on top of the public roots for Beacon's
  calls to agents (bus, relay, mailbox). With a TLS terminator in front of
  each agent and an `https://` endpoint, agents on a plain LAN talk over TLS.
  README, "TLS without an overlay network", has the recipe.

## [0.21.1] — 2026-10-26

### Added

- **Version in the header.** The page shows the running Beacon version next to
  the logo, from the new `GET /api/version`.

## [0.21.0] — 2026-10-26

### Added

- **Restart agent.** A second button beside "Restart gateway", plus
  `POST /api/agents/{id}/restart-agent` and an MCP `restart_agent` tool (T3).
  An agent with `desired.container` restarts that Docker container
  (`docker restart -t 120`, with a `sudo -n` fallback). Any other agent gets
  `systemctl --user restart` on its gateway unit, which works on a wedged
  gateway that `hermes gateway restart` cannot drain.

## [0.20.0] — 2026-10-26

### Added

- **Long work through the mailbox.** Work that took longer than a short
  exchange never came back: a Hermes caller gives up after 120 s, and a Hermes
  agent dropped any answer it found after its 300 s `A2A_REPLY_TIMEOUT`. The
  mailbox now waits at most 90 s. A slower answer is handed off: the caller
  gets `TASK_STATE_WORKING` and a durable task id `mbx-N` at once, the agent
  POSTs the finished task to Beacon (A2A push notification, to a per-message
  URL with a derived key), and the answer reaches the caller as a new message.
  This applies to live relays, to held messages that Beacon delivers, and to
  held messages that the host sync delivers locally. The sync waits 60 s
  instead of 330 s, so one long task no longer holds it. The host sync sets each
  agent's `A2A_REPLY_TIMEOUT` to 4 hours, which restarts each gateway once.
  A handed-off task with no answer after 4 h 15 min expires with a notice to
  the caller. `POST /api/agents/{id}/call` with `queue_if_offline` (MCP
  `call_agent`) goes through the same path.
- **Mailbox task status.** `GET /api/mailbox/tasks/{task_id}`, the MCP tool
  `mailbox_task`, and A2A `GetTask` on `/a2a/{agent}` (sender and target only)
  show a mailbox task as an A2A task with its delivery details. `uncertain` and
  `expired` count as `FAILED`. The mailbox does not stream, so
  `SubscribeToTask` returns an unsupported-operation error. Board assignment
  notices record their own mailbox task on the card (`assign_notice` events),
  separate from the assignment, and their task ids can be looked up. A notice
  that gets no answer in time counts as delivered and is not sent again.
- **Web governance workflows.** Board cards can now carry durable decision,
  review, and approval requests in SQLite, with explicit lifecycle states,
  response history, expiry, and exact plan revision/hash binding.
- **Workflow inbox.** The web UI has a Workflows tab for listing pending
  requests and responding to eligible decisions, reviews, and approvals.
- **T-level enforcement.** T3 `push_config` requires an approved review;
  T4/T5 workflow responses require a human browser session. Agent workflow
  access is constrained by board opt-in and the existing mesh access policy.
- **Human workflow sentinel hardening.** The `human` responder slot is reserved
  for signed browser sessions and cannot be claimed or satisfied as an agent id.
- **Execution outcome tracking.** Approved config pushes become `executed`,
  `failed`, or `execution_uncertain`, so failed or interrupted streams consume
  their approval rather than leaving it reusable.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) once there's
an API worth being stable about — pre-1.0, breaking changes can land in a
minor bump. See [README.md](README.md#limitations) for current limitations
and [ROADMAP.md](ROADMAP.md) for what's planned.

## [Unreleased]

## [0.19.0] — 2026-10-24

### Added

- **Tag management in the app.** The Agents tab has a Tags column, filter chips
  (one per tag, plus "untagged"), tick boxes, and a **Tag…** action that adds
  or removes a tag on all selected agents at once. The Policy tab lists every
  tag with its agents.
- **The app works on a phone.** Below 720px the tab bar wraps onto its own
  row that scrolls sideways, tables become one card per row (each cell
  labelled), dialogs fill the screen, form fields stack, touch targets are
  larger, and form text is 16px so iOS does not zoom. Before, Policy, Templates
  and Cron jobs were unreachable on a phone and the action buttons were off
  the edge of the screen.

### Changed

- **Changing an agent's tags, `reach.callers`, or `reach.board` needs a human
  browser session** (tier T4, `set_agent_access`). These decide who may call
  whom, so the MCP token must not be able to change them. Other edits are
  unchanged.

## [0.18.0] — 2026-10-24

### Changed

- **The host sync no longer replaces an agent's whole A2A configuration.** It
  used to overwrite `a2a_agents`, `A2A_PEER_TOKENS`, `A2A_TRUSTED_PEERS`, and
  every `A2A_TOKEN_*` variable on each run, which would have erased any peer,
  caller, or token an agent's owner added. It now touches only the names it
  created, tracked in `managed.json`, and leaves everything else alone. The
  caller lists are merged; peers are written and removed one entry at a time
  (`config set a2a_agents.<peer>`, `config unset`). A fresh install adopts
  nothing. **An install made before this version must run
  `sync.py <state> --adopt` before the new script takes over**, or a peer that
  Beacon later revokes stays; see the README.

### Fixed

- **Tests could write to the real operational store.** A background thread
  from a board-notice test outlived its fixtures and wrote into the real
  `state/beacon.db`, which flipped a delivered message back to queued. The
  suite now uses a disposable state directory for the whole session.

## [0.17.0] — 2026-10-24

### Added

- **Policy editor.** A **Policy** tab edits `fleet/policy.yaml`. **Check**
  shows validation errors, likely mistakes, the new who-may-call-whom table
  with the added and removed paths marked, and which agents would restart.
  **Save** asks for confirmation and writes the text as typed, so comments
  survive. It is a human-only action (new tier T4, `set_policy`); the MCP
  token cannot save. A stale editor is refused (409) and invalid text too
  (422). Each replaced version is kept in `fleet/policy-history/` (newest
  50, named by time and author). `POST /api/policy/preview` and
  `PUT /api/policy` are the API; MCP gains read-only `get_policy` and
  `preview_policy`.
- The agent form has a **Tags** field.

### Fixed

- **Saving an agent in the UI dropped its `reach` block** (endpoint, network,
  board, callers, capabilities), because the form rebuilt the record from
  only the fields it shows. It now keeps the loaded record underneath the
  edits. Anyone who edited an agent in the UI since 0.10 should check that
  agent's `reach`.

## [0.16.0] — 2026-10-24

### Changed

- **Access is now decided by `fleet/policy.yaml`, and the default is Beacon
  only.** Before, any agent on the same network could call any other. Now no
  agent may call another unless a rule allows it. **To keep today's behavior,
  create `fleet/policy.yaml` with `rules: [{from: ["*"], to: ["*"]}]` before you
  deploy this version**, or every agent loses its peers. See "Access policy"
  in the README.

### Added

- **Tag policy.** Agents carry `tags`; `fleet/policy.yaml` holds
  Tailscale-style rules (`from`/`to` selectors: agent id, `tag:name` with `*`
  wildcards, or `*`) and `deny` rules that always win. Rules are one-way;
  replies are always allowed, and late replies arrive from Beacon so a
  one-way rule does not block them. `reach.callers` remains as an extra
  per-agent limit. `python -m backend.mesh policy` prints the full
  who-may-call-whom table and lists likely mistakes; `GET /api/policy` returns
  it; Beacon logs problems at startup. The registry shows each agent's tags.

### Fixed

- **`beacon` can no longer be an agent id.** It is the mesh's name for Beacon
  itself (operators and MCP clients). An agent with that id would have
  derived Beacon's own tokens and passed every access policy. The schema now
  refuses it, so the API answers 422.

## [0.15.0] — 2026-10-23

### Added

- **Skill sync.** Beacon hands each agent every `skills/<name>/SKILL.md`
  with its mesh config, and the host sync writes the ones that differ
  into the profile's `skills/` folder. Only plain names, up to 64 KB, with
  a matching checksum; nothing is deleted. The image now includes
  `skills/`; `BEACON_SKILLS_DIR` overrides it.
- **Agent skill** (`skills/agent-mesh/SKILL.md`): how an agent should use
  the peer tools, the mailbox, and the board, how to treat peer messages,
  and how to read the errors.

## [0.14.0] — 2026-10-23

### Fixed

- **Restarts on Hermes 0.22 are no longer lost.** Hermes 0.22 refuses
  `gateway restart` while sessions are live, where 0.21 waited for them.
  The host sync now keeps a restart request until one succeeds. It
  records each detached attempt's exit code and retries on every run. It
  never stacks attempts on a drain that is still running.

### Added

- **Relay across networks (C1).** An agent on another network, or on
  none, appears in each peer list under its plain name, pointing at
  Beacon's relay (`/a2a/{agent}`). Agents call peers by name whether the
  path is direct or relayed. Every agent now accepts callers from every
  network, because they arrive through Beacon with their own token. The
  access policy still decides who.
- **Pull delivery.** Messages the mailbox holds for an agent travel with
  its mesh-config pull. The agent's own sync delivers them on its host and
  reports each result to `POST /mesh/{agent}/inbox/{id}`. A delivery
  passes the reply back to the original sender; a delivery that never
  arrived is handed back; a refusal or timeout is marked failed. Beacon
  never needs to reach the agent, so it works behind NAT. A sync
  announces support with `X-Beacon-Sync-Version: 2`; older syncs keep the
  Beacon-calls-the-agent path. The mesh config now includes the agent's
  own `endpoint`.

## [0.13.0] — 2026-10-23

### Added

- **Access policy:** `reach.callers` lists the agents that may call an
  agent; unset means anyone on its network. Beacon is always allowed. One
  rule (`mesh.may_call`) applies to direct calls (only allowed callers'
  tokens reach the target), to the mailbox (HTTP 403 with the reason,
  logged, and re-checked for messages queued before a change), and to
  board assignments (an agent can only hand work to agents that accept
  it). Peer lists show only peers that accept the agent. The registry
  shows each agent's policy.

## [0.12.0] — 2026-10-23

### Added

- **Fleet board.** Shared cards with a state (`todo`, `doing`, `blocked`,
  `review`, `done`), an assignee, a project, and a history in which every
  change carries its author and an optional A2A `context_id`.
  - People use the new **Board** tab or `/api/board`.
  - Agents and MCP clients use `/board/mcp`, an MCP endpoint on Beacon with
    board tools only. Each agent authenticates with its own pull token.
  - Assigning a card to an agent sends it a notice through the mailbox.
  - Agents get the board tools only with `reach.board: true`. The host sync
    then adds `mcp_servers.beacon-board`, with the token in `.env` behind an
    `${env:}` reference, and restarts the gateway once.
- Beacon now depends on the `mcp` SDK (2.0.0, the version the MCP service
  already pins). The lock files are regenerated.

## [0.11.0] — 2026-10-23

### Added

- **Mailbox: relay and voicemail.** `POST /a2a/{agent}` speaks A2A on the
  agent's behalf. If the agent answers, Beacon relays the call. If it
  cannot be reached, Beacon keeps the message and delivers it when the
  agent's host next pulls its mesh config; that pull is the liveness
  signal, so there is no scheduler. The reply goes back to the original
  sender as a new message. Callers keep their identity end to end, and no
  agent can pose as Beacon. Unanswered messages expire after 72 hours with
  a notice to the sender. Peer lists gain a `<peer>-mailbox` entry per
  peer.
- `queue_if_offline` on `POST /api/agents/{id}/call` and on MCP
  `call_agent`; `GET /api/mailbox` and the MCP tool `mailbox` to read
  replies.
- **Operational store:** `backend/opstore.py`, SQLite at `state/beacon.db`
  (`BEACON_STATE_DIR`), and a `./state` volume in `docker-compose.yml`.
- **Movable Beacon:** the mesh config announces Beacon's URL, and the
  host-side sync follows it.

## [0.10.0] — 2026-10-23

### Added

- **Agent bus over A2A.** Beacon (and any MCP client, such as Claude) can
  call a registered agent by name. `POST /api/agents/{id}/call` with
  `{"message", "context_id?"}` sends one A2A v1.0 `SendMessage` to the
  agent's `reach.endpoint`. It returns `task_id`, `context_id`, `state`, and
  `reply`. Pass `context_id` back to continue a thread.
  `GET /api/agents/registry` lists who is callable and which capabilities
  they declare, without exposing endpoints or token names. MCP tools:
  `agent_registry` and `call_agent`. An agent with no `reach` block stays
  inventory-only. Auth on the route is the existing session or MCP bearer.
  See README, "Agent bus".
- **A2A mesh.** Agents with the same `reach.network` label call each other
  directly; Beacon only hands out peer lists, so it is never on the message
  path. Tokens are pairwise and derived from one secret,
  `BEACON_A2A_SECRET`. Each agent's host pulls its own config from
  `GET /mesh/{agent}`, which accepts only that agent's derived pull token,
  and applies it with a stdlib `sync.py`. If Beacon is down, agents keep
  their last config. Only inbound changes restart the gateway (gracefully).
  `python -m backend.mesh install <agent-id>` prints the one-time host
  bootstrap; scheduling the sync is left to the operator. `reach.bind`
  declares where an agent listens when that differs from its endpoint
  (Docker, NAT). `sync.json` takes an optional `restart` command, so a
  Docker agent's container is restarted from the host instead of its
  gateway: an s6-supervised image never finishes `hermes gateway restart`.
  `python -m backend.mesh install <agent> --docker <container>` generates
  that host-side setup: run on the container's host as root.
  New `.env` keys:
  `BEACON_MESH_BIND` and `BEACON_PUBLIC_URL`. See README, "A2A mesh".

### Fixed

- `docker-compose.yml` now loads `.env` into the beacon container
  (`env_file`, optional). Before, per-agent token variables named in fleet
  YAML never reached the container, so every bus call would have failed.

## [0.9.0] — 2026-09-25

### Added

- **Fleet cron jobs.** Schedule Beacon-native actions (`restart`, `status`,
  `push_config`) against one or more agents from YAML records in
  `fleet/cron_jobs/*.yaml`. New API routes: `GET/PUT/DELETE
  /api/cron-jobs/{id}` and `GET /api/cron-jobs`. A lightweight scheduler in
  `backend/scheduler.py` is driven by an external minute-level trigger (system
  cron/launchd/k8s CronJob) calling `python -m backend.scheduler tick` — no
  background scheduler process inside Beacon. Validates 5-field cron
  expressions and refuses arbitrary shell commands in v1; persists last-run
  status/output per job.
- **Manual run / dry-run.** `POST /api/cron-jobs/{id}/run` executes a job
  immediately regardless of schedule; `POST /api/cron-jobs/{id}/dry-run`
  reports whether it is due without touching a host. Exposed in the GUI tab
  as **Run now** and **Dry run** buttons per job.

## [0.8.0] — 2026-09-25

### Added

- **Config schema guardrail.** `hermes config set` only hard-refuses the
  narrow wrong-prefix case — a plain typo like `model.primary` is *written
  anyway* with a post-write notice and exit code 0, so a fail-closed push
  would report success while the key did nothing. Beacon now validates every
  `desired.config`/template path itself before pushing, against a checked-in
  snapshot of Hermes's `DEFAULT_CONFIG` plus its open-section key sets
  (`backend/drivers/hermes_config_schema.yaml`, regenerated from a
  hermes-agent checkout with `scripts/dump_hermes_config_schema.py`, with a
  small hand-maintained overlay for sections DEFAULT_CONFIG seeds
  degenerately, e.g. `model`). Unknown subkeys of known sections are
  refused with a 400 naming every bad path and a did-you-mean suggestion;
  unknown top-level keys (legal in Hermes — custom keys bridge to the
  environment) push with a streamed warning. `config_diff` annotates every
  finding with `schema: ok|warn|error`, and the GUI renders schema errors as
  critical rather than plain drift.
- **Orchestration template example** (`fleet/templates/orchestration.yaml.example`):
  the `delegation` section — `orchestrator_enabled`, concurrency/depth caps,
  `subagent_auto_approve`, optional per-subagent model pinning and a separate
  `delegation.fallback_providers` chain.

### Fixed

- The shipped template example taught a fictional config schema
  (`model.primary`, `model.fallbacks`, `tools.model`, `summarization.model`)
  that no Hermes build reads — every push of it would have been silently
  inert. `fleet/templates/anthropic-stack.yaml.example` and the README now
  use the real schema: `model.default`/`provider`/`aliases`, the top-level
  `fallback_providers` object list (pushed as one structured JSON value),
  and `auxiliary.<task>.{provider,model}` for helper models.

## [0.7.0] — 2026-09-03

### Added

- **Config templates.** A `fleet/templates/<name>.yaml` fragment (`config`
  and `env_keys` only) merged under many agents' `desired` at once — the
  main/fallback/auxiliary-task model stack declared in one place instead of
  copied into every agent file. An agent opts in with a `templates:` list;
  `backend/templates.py` deep-merges the named fragments in order, then the
  agent's own `desired` on top (agent wins), on every read. The merge is
  never written back — the agent record keeps its own overrides only, and
  `GET /api/agents/{id}` now returns `effective_desired` alongside the raw
  `desired`. New routes: `GET /api/templates` (with reverse lookup of which
  agents use each), `GET /api/templates/{name}`, and
  `POST /api/templates/{name}/apply` (`apply_template`, tier T2) to add a
  template to a batch of agents in one call. `config-diff` GET/POST now
  compare and push the merged view. Exposed over MCP too (`list_templates`,
  `get_template`, `apply_template`).
- **GUI: Templates tab.** Lists templates with their content and which
  agents use each; "Apply to…" opens a checkbox list of agents (ones
  already using it shown checked/disabled) and applies in one call. The
  agent form gained a `templates` field.

  The agent form's save path sends `templates` explicitly, so editing an
  agent through the GUI doesn't clear a template list set elsewhere.

## [0.6.1] — 2026-08-27

### Fixed

- CI's `test` job couldn't import `backend` on a clean runner — worked
  locally only because `python -m pytest` adds cwd to `sys.path` on its
  own, which the bare `pytest` console script CI runs does not. Fixed with
  pytest's own `pythonpath = .` ini option. `0.6.0`'s tagged commit never
  had a passing CI run; this one does.

## [0.6.0] — 2026-08-27

### Added

- Test suite (`tests/`, ~130 tests): the driver layer's script generation
  and validation, the tier registry, `ssh.py`'s command construction,
  auth's PKCE pair, and confirm-gating as a plain HTTP client actually
  hits it. `tests/conftest.py` provides a throwaway `fleet/` per test and a
  fake `ssh` module that records the command it was asked to run instead
  of touching a real host or network.
- CI (`.github/workflows/ci.yml`): the test suite plus a build of all
  three Docker images, on every push.

### Fixed

- `SSHConfig`'s validator only required "at least one" of `key`/
  `config_file`, not "exactly one" — both set silently worked, with
  `config_file` winning and `key` becoming dead weight with no warning.
  Now rejects both-set the same as neither-set.

## [0.5.0] — 2026-08-27

### Added

- Tier registry (`backend/tiers.py`): every mutating capability mapped to a
  T0-T5 autonomy tier — how much oversight it needs — with the rationale
  for each assignment recorded alongside it. `GET /api/tiers` exposes the
  registry; an MCP `list_tiers` read-only tool mirrors it.
- `confirm=true` is now required by the API itself (not just the MCP
  server) for every T2+ capability — `restart`, `apply_fix`, `push_config`,
  `deploy`, `update_plugin`, `update_agent`, `decommission`. Without it, the
  endpoint returns a 200 describing what it would do instead of running it.
  Previously this was enforced only in `mcp/server.py`; a direct HTTP call
  bypassed it entirely.
- `decommission` escalates from T4 to T5 when `purge` or `remove_user` is
  requested — those are irreversible; a bare stop+uninstall is not.

### Changed

- `mcp/server.py`'s mutating tools now pass `confirm=true` through to
  Beacon's API once their own `confirm` check has passed, since the API
  enforces the same gate independently now.
- `frontend/app.js`'s risky-action buttons already ask via the browser's
  native `confirm()` before calling the API — they now also send
  `confirm: true` (or `?confirm=true`) so those calls still execute instead
  of getting the new "would run" response back silently.

## [0.4.0] — 2026-08-27

### Added

- MCP server auth: `BEACON_MCP_TOKEN`, a static bearer token. The same
  token doubles as MCP's own credential when it calls Beacon's API in
  turn — Beacon's `AuthMiddleware` accepts
  `Authorization: Bearer <BEACON_MCP_TOKEN>` as an alternative to a
  session cookie.
- Plugins: `list_plugins` (name, version, enabled/disabled, source) and
  `update_plugin` (git pull one plugin by name), both in the API, GUI
  Inspect panel, and as MCP tools.
- `restart` — plain `gateway restart`, distinct from Reconcile's
  problem-triggered fixes.
- `update_agent` — `hermes update --yes` on the shared code checkout,
  streamed like Deploy.

### Fixed

- `update_plugin` now returns a `disabled_by_scan` flag: an update can
  introduce a revision that trips Hermes's own security scanner, which
  auto-disables the plugin — silently taking down whatever platform it
  served. This surfaces as a first-class warning instead of being buried
  in scan-report text.
- `list_plugins`'s `name` field (the plugin's manifest name) isn't always
  what `plugins update` expects, which wants the installed directory name
  instead — documented on `update_plugin`.
- `restart`'s timeout raised from 60s to 120s — a graceful restart drains
  in-flight turns and waits for the new process to report runtime-ready,
  which can take longer than a quick fix-style command.
- `docker-compose.yml` now passes `BEACON_MCP_TOKEN` to the `beacon`
  service as well as `mcp` — it's the one that actually checks it.

## [0.3.0] — 2026-08-27

### Added

- Login against Zitadel (or any OIDC provider) via authorization code +
  PKCE (`backend/auth.py`). Every route except `/auth/*` requires a
  session once `ZITADEL_ISSUER`/`ZITADEL_CLIENT_ID` are set — unset,
  Beacon runs open. Session is a signed cookie
  (`itsdangerous`/`SessionMiddleware`), no server-side session store.
  GUI shows the logged-in user with a logout link.
- `.env.example` for the vars this needs; `docker-compose.yml`'s `beacon`
  service reads them from `.env` (gitignored).

## [0.2.0] — 2026-08-26

### Added

- tbot Machine ID sidecar (`tbot/Dockerfile`, `tbot` service in
  `docker-compose.yml`) — runs as its own container rather than a bare
  host process the `beacon` container reads from.
- MCP server (`mcp/server.py`, `mcp` service, port 8643) exposing the
  fleet over the Model Context Protocol — a thin client of Beacon's own
  HTTP API. Read tools (status/logs/list/reconcile-check/config-diff)
  unrestricted; mutating tools (deploy/apply_fix/push_config/
  decommission) require an explicit `confirm=true`, describing the
  action instead of taking it otherwise.

## [0.1.0] — 2026-08-26

Initial build: a working fleet console for Hermes agents, end to end.

### Added

- YAML-backed registry (`fleet/hosts.yaml`, `fleet/agents/*.yaml`), no
  database — a small FastAPI backend and a vanilla-JS frontend over it.
- SSH transport (`backend/ssh.py`) via the system `ssh` binary, either a
  static key or an `ssh_config` file — the latter lets a host connect
  through a Teleport Machine ID (tbot) identity instead of a long-lived
  key.
- Hermes driver (`backend/drivers/hermes.py`):
  - **Deploy** — three modes (`simple`, `add-profile`, `new-user`),
    idempotent, streamed live to the GUI.
  - **Track** — live systemd `--user` status, including a `crashlooping`
    state distinct from plain `inactive`.
  - **Troubleshoot** — log tail, falling back to `journalctl` when a
    unit has never started successfully and has no log file yet.
  - **Reconcile** — dry-run diagnosis of drift patterns (orphaned unit,
    stuck-failed unit, installed-but-not-started, linger disabled), with
    a named fix to apply per finding.
  - **Config** — diffs `desired.config`/`desired.env_keys` against the
    live `config.yaml` and `.env` key names (never values), and can push
    declared keys via `hermes config set`.
  - **Decommission** — stop, uninstall, optionally purge profile data or
    delete the OS account, archive the record to
    `fleet/decommissioned/`.
- Docker packaging (`Dockerfile`, `docker-compose.yml`).
