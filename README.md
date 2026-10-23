# Beacon

Central console for a fleet of [Hermes](https://github.com/NousResearch/hermes-agent)
agents spread across hosts, profiles, and OS users. Deploy, track, configure,
troubleshoot, reconcile, and decommission them from one place instead of an
SSH session per host.

Built agent-type-agnostic on purpose: Hermes is the first driver, not the
only one it's meant to ever support.

## How it's built

- **Source of truth**: plain YAML files under `fleet/` — `hosts.yaml` (SSH
  targets) and `agents/*.yaml` (one file per agent). Git-diffable, hand-editable,
  no database.
- **Backend**: FastAPI ([backend/](backend/)), reads/writes that YAML and
  runs everything else over SSH.
- **Drivers**: agent-type-specific logic lives behind a small module
  interface ([backend/drivers/hermes.py](backend/drivers/hermes.py)) — `status`,
  `logs`, `deploy`, `reconcile`, `config_diff`/`push_config`, `decommission`.
  A second agent type means writing a second driver module, not touching
  the core.
- **Frontend**: a single static page ([frontend/](frontend/)) — vanilla
  HTML/CSS/JS, no build step, no framework.
- **Transport**: the system `ssh` binary, either with a static key or an
  `ssh_config` file (see [Teleport / tbot](#teleport--tbot) below) — never a
  Python SSH library, so anything that works from your terminal works here.

## Quickstart

### Local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp fleet/hosts.yaml.example fleet/hosts.yaml   # first run only
.venv/bin/uvicorn backend.app:app --port 8642
```

Open `http://localhost:8642`.

### Docker

```bash
cp fleet/hosts.yaml.example fleet/hosts.yaml   # first run only

# only needed once, ever — establishes tbot's stored identity in
# ./tbot/storage. Get a token from `tctl bots add beacon --roles=agent`.
# Skip this if a host in hosts.yaml only uses ssh.key, not config_file.
./tbot/run.sh <join-token>
# Ctrl-C once it logs "Fetched new bot identity" — the compose stack takes it from here.

docker compose up -d --build
```

Two services: `tbot` (renews the Machine ID identity into `./tbot/data`)
and `beacon` (reads it). `fleet/` is bind-mounted too, so your inventory
persists across rebuilds and is editable from the host. See
[Teleport / tbot](#teleport--tbot) for the details.

Copy `.env.example` to `.env` and fill it in to require login — see
[Authentication](#authentication). No `.env`, no login: Beacon runs open,
same as local dev.

## Data model

### `fleet/hosts.yaml`

```yaml
hosts:
  - id: edge-01
    address: 10.20.4.11
    ssh:
      user: deploy
      key: ~/.ssh/fleet_ed25519   # OR config_file (mutually exclusive with key)
      port: 22
    tags: [prod, edge]
```

`ssh.key` and `ssh.config_file` are mutually exclusive — exactly one is
required. `config_file` points at an `ssh_config` (e.g. one a Teleport tbot
identity generates); when set, Beacon connects with `ssh -F <file>` and lets
that file's own `Host` block own auth and host-key verification instead of
`-i`/`-p`. Static-key hosts require a reviewed known-hosts file configured by
`BEACON_KNOWN_HOSTS`; Beacon uses strict checking and never trusts a new key
automatically.

### `fleet/agents/<id>.yaml`

```yaml
id: edge-01-primary
type: hermes          # selects the driver
host: edge-01         # must match a hosts.yaml id
profile: primary      # omit or "default" for Hermes's default profile
owner: tom
notes: "handles EU relay traffic"

templates: [anthropic-stack]   # config templates merged under `desired` — see Templates

desired:
  install_mode: simple       # simple | add-profile | new-user
  os_user: null              # required for install_mode: new-user
  service: null               # override the systemd unit name (rarely needed)
  log_path: null               # override the log file path (rarely needed)
  config: {}                   # config.yaml keys Beacon manages — see Config below
  env_keys: []                 # expected .env key NAMES — values never leave the host

reach:                       # omit entirely if this agent is inventory-only
  transport: a2a             # A2A v1.0 JSON-RPC, the only transport so far
  endpoint: http://10.20.4.11:9900   # where JSON-RPC is POSTed, as every caller sees it
  network: tailnet           # agents sharing a network call each other directly
  bind: 0.0.0.0:9900         # optional: where the agent listens, if not the endpoint (Docker, NAT)
  board: false               # true: the agent gets the fleet board's MCP tools (see Fleet board)
  callers: [edge-02-primary] # optional: only these agents may call it (see Access policy)
  capabilities: [relay]      # declared, not enforced — see Agent bus
```

Only what you actually declare is managed. An agent record with an empty
`desired` is valid — Beacon just won't have an opinion about it beyond
`status`/`logs`.

Nothing here holds live state (running/stopped, PID, current config).
`status()` and `config_diff()` always poll the host fresh — YAML never goes
stale in that direction.

## Agent bus

Beacon calls an agent by name over [A2A](https://a2a-protocol.org) v1.0,
the Linux Foundation agent-to-agent standard. Beacon does not invent a
protocol: any A2A agent can be a target, and Hermes (0.20+) speaks it
natively. The main caller today is an MCP client such as Claude, which
cannot speak A2A itself.

```text
caller  --session or MCP bearer-->  POST /api/agents/{id}/call
                                         |
                                         | A2A SendMessage (JSON-RPC, bearer token)
                                         v
                                    target's A2A endpoint
```

`reach` on the agent record says how. `transport` is `a2a` and nothing else
yet. `endpoint` is the URL that JSON-RPC is POSTed to, as every caller sees
it. Hermes serves its own profile at `/` and other local profiles under a
path, so a path is allowed. `network` is a label; see [A2A mesh](#a2a-mesh).
`capabilities` are a declaration for the registry. Beacon does not route or
authorize on them.

Beacon calls as the peer `beacon`. The token is derived from
`BEACON_A2A_SECRET` (see below), so nothing is stored per agent. If the
secret is missing, the call fails rather than going out unauthenticated.

`GET /api/agents/registry` returns `id`, `callable`, and `capabilities` for
every agent. It does not return endpoints or token-env names. A caller names
an agent; it does not learn how to reach it.

`POST /api/agents/{id}/call` takes `{"message": "...", "context_id": "..."}`.
`message` is required and non-empty. `context_id` is optional. Without it,
the agent starts a new thread. To continue a thread, pass back the
`context_id` from an earlier reply. The response is:

```json
{"agent": "<id>", "task_id": "...", "context_id": "...",
 "state": "TASK_STATE_COMPLETED", "reply": "<agent text>"}
```

The call blocks until the agent answers. Hermes gives up after
`A2A_REPLY_TIMEOUT` (300 s by default) and answers `TASK_STATE_FAILED`.
Beacon waits 330 s, so the agent's verdict arrives first. A failed or
rejected task is a normal 200 response with that `state`. Hermes also caps
one thread at `A2A_MAX_PINGPONG_TURNS` (5 by default, 20 at most). After the
cap, it rejects the turn. Start a new thread at that point.

An agent with no `reach` block is inventory-only: deploy, status, and the
rest still work, and a call to it returns 502. An unknown id returns 404.
These also return 502, and the detail never includes the token:

- the target refuses the token or the peer (the A2A error message and code
  are passed through);
- the target does not answer;
- the target returns a body Beacon cannot read.

Auth is the existing gate. With OIDC configured, the route wants a session
cookie or `Authorization: Bearer <BEACON_MCP_TOKEN>`. Without it, Beacon is
open, same as every other route. The call does not take `confirm=true`: it
changes no host, and the only thing it spends is one turn on the target.
The same two operations are MCP tools, `agent_registry` and `call_agent`.

If the agent is offline, the call fails with 502. Add
`"queue_if_offline": true` to leave the message in the
[mailbox](#mailbox-relay-and-voicemail) instead. The response then has
`state: TASK_STATE_SUBMITTED` and a `queued` message ID. The reply turns
up later in `GET /api/mailbox` (MCP tool: `mailbox`) with recipient
`beacon`.

Not yet built: relaying between different networks, streaming and push
notifications, and any check that a caller may reach a particular agent. See ROADMAP.md, "Priority track — agent
communication".

### A2A mesh

Agents call each other directly, Tailscale-style. Beacon is only the
coordination server: it decides who peers with whom and hands out tokens.
Each agent's host pulls its own config from Beacon every few minutes and
applies it. Beacon is never on the message path. If Beacon is down, every
agent keeps its last config and keeps talking.

- **Peers:** two agents peer directly when their `reach.network` labels
  match. The label is free text, for example `tailnet` or `office-lan`.
  Beacon does not check that the network exists or that it is reachable.
- **Other networks:** an agent on another network, or on none, appears in
  the peer list under its plain name with Beacon's relay URL. Agents call
  `edge-02-primary` the same way in both cases, and Beacon carries the call when
  there is no direct path. Every agent accepts callers from every network,
  because a relayed caller arrives with its own token. The access policy
  (`reach.callers`) still decides who may call.
- **Tokens:** pairwise and derived, `HMAC(BEACON_A2A_SECRET, "caller>target")`.
  Beacon keeps one secret, not N² tokens. A host that leaks its tokens
  exposes only calls into itself. An agent pulls with its own derived token,
  `token(<agent>, "beacon")`. To rotate everything, change the secret, then
  run `install` for every agent again (the old pull tokens stop working).
- **Transport security:** use a network that encrypts (WireGuard, for
  example), or put a TLS terminator in front of each agent and use an
  `https://` endpoint.

**The pull endpoint.** `GET /mesh/{agent-id}` returns that agent's config:

```json
{"agent": "edge-01-primary",
 "listen": {"port": 9900, "host": "100.64.0.11"},
 "inbound": {"beacon": "<token>", "edge-02-primary": "<token>"},
 "outbound": [{"name": "edge-02-primary", "url": "http://100.64.0.12:9900",
               "token": "<token>", "capabilities": []}]}
```

The only credential it accepts is that agent's own pull token. A browser
session or the MCP bearer does not work, because the response contains
tokens. The format is not specific to Hermes; another A2A agent type needs
only its own sync. Agents' hosts must reach Beacon for this, so set these in
`.env`:

```bash
BEACON_MESH_BIND=100.64.0.1:8642          # extra port binding for agents
BEACON_PUBLIC_URL=http://100.64.0.1:8642  # the URL their sync calls
```

The UI and MCP keep their own bindings (`BEACON_BIND_HOST`). The extra
binding serves the whole app, so configure OIDC before you set it.

**Joining an agent (one time per agent).** Run this where Beacon's
environment is. The output contains the agent's pull token, so pipe it to
the host and do not save it:

```bash
python -m backend.mesh install edge-01-primary | ssh root@edge-01 'bash -s'
```

The script does these things on the host:

1. It puts `beacon-mesh/` next to the profile's `config.yaml`: the pull
   token (mode 0600), `sync.py`, and `sync.json`.
2. It turns on the `a2a` toolset for the `a2a` platform. Without it, an
   agent cannot pass a peer's task on to another peer.
3. It runs one sync.
4. It prints a cron line. **Add the schedule yourself:** the script does
   not install a cron job, a systemd timer, or a launchd agent on its own.

**Each sync** (`beacon-mesh/sync.py`, stdlib only):

- It writes `A2A_PORT`, `A2A_HOST`, `A2A_AGENT_NAME`, `A2A_PEER_TOKENS`,
  `A2A_TRUSTED_PEERS`, and one `A2A_TOKEN_<PEER>` per peer into the profile
  `.env`. Tokens go into `.env` directly, never into argv.
- It replaces `a2a_agents` in `config.yaml` with `hermes config set --force`.
  Tokens appear there only as `${env:A2A_TOKEN_<PEER>}` references.
- Outbound changes take effect on the agent's next turn, with no restart.
  Inbound changes (who may call in) need a restart, because Hermes reads
  them at startup. For those, the sync asks for a graceful
  `hermes gateway restart`. The request is kept until it succeeds and is
  retried on every run. Hermes 0.21 waits for running turns before
  restarting. Hermes 0.22 instead refuses while sessions are live, and
  offers only `--yes`, which would interrupt them. The retry gets past
  that: the restart happens at the first idle moment, and no session is
  cut off.
- If Beacon cannot be reached, nothing changes. A lock file stops two
  runs from overlapping.

To let agents reach peers from a chat platform, turn the toolset on there
yourself, for example
`hermes -p <profile> tools enable a2a --platform deltachat-platform`.
Agents then use Hermes's `a2a_call`, `a2a_orchestrate`, and `a2a_history`
tools against their peers by name.

**Agents in Docker.** Callers dial the published port; the container
listens on its own address. Declare both:

```yaml
reach:
  endpoint: http://100.64.0.20:9901   # published, tailnet IP only
  bind: 0.0.0.0:9900                     # inside the container
  network: tailnet
```

If the container shares a host-networked Tailscale sidecar
(`network_mode: "service:tailscale"`), it can bind the tailnet IP itself.
Then leave out `bind` and do not publish a port.

1. Otherwise, publish the port on the endpoint address only, for example
   `ports: ["100.64.0.20:9901:9900"]`, and recreate the container.
2. Run the install **on the container's host, as root**:

   ```bash
   python -m backend.mesh install nas-agent --docker hermes-nas | ssh root@nas 'bash -s'
   ```

   The script finds the host path of the data volume with
   `docker inspect`. It puts `beacon-mesh/` there and writes `sync.json`
   with Hermes behind `docker exec -u hermes <container>` and restart as
   `docker restart -t 120 <container>`. Then it runs one sync. If the
   container user is not `hermes`, add `--docker-user`.
3. Schedule the printed line as root with the host's scheduler: cron, or
   Task Scheduler on Synology DSM.

The sync restarts the container, not the gateway. In the s6-supervised
official image, `hermes gateway restart` stops the chat platforms, but the
process does not exit, and the agent stays half down. `docker restart -t
120` gives running turns two minutes to finish. If `sudo` asks for a
password, copy the script to the host and run it there with `sudo bash`.

### Access policy

`reach.callers` lists the agents that may call an agent. If it is not
set, any agent on the same network may call it. Beacon itself, which
means operators and MCP clients, is always allowed, so nobody locks
themselves out. A single rule, `mesh.may_call`, applies on every path, so
no path routes around it:

- **Direct calls:** the target's `A2A_PEER_TOKENS` holds only allowed
  callers, so Hermes refuses anyone else. The host sync distributes the
  list.
- **The mailbox:** Beacon refuses with HTTP 403, `"<target> does not
  accept calls from <caller>"`, and logs the refusal. A message queued
  before a policy change is checked again at delivery and marked `failed`
  with the reason.
- **The board:** an agent cannot assign a card to an agent that would
  not accept a call from it. Handing over work is a way of calling. People
  and Beacon are not limited.
- **Peer lists:** an agent's list includes only peers that accept it, so
  it is never offered a call that can only fail.

`GET /api/agents/registry` shows each agent's `callers`.

A change to `callers` changes who the target accepts. Its gateway therefore
restarts once, gracefully, on the next sync.

### Mailbox: relay and voicemail

`POST /a2a/{agent}` is an A2A endpoint that Beacon serves for each agent.
Every agent's peer list has two entries per peer: `<peer>`, which is
direct, and `<peer>-mailbox`, which goes through Beacon. An agent uses the
mailbox entry when the direct call fails.

- **The target answers:** Beacon forwards the call and returns the reply
  (relay).
- **The target cannot be reached:** Beacon keeps the message and answers
  `TASK_STATE_SUBMITTED` (voicemail). The message travels with the target's
  next mesh-config pull, which is within 5 minutes of the agent coming
  back. The target's own sync then delivers it to the agent on its own
  host and reports the result (pull delivery). Beacon therefore never needs
  to reach the agent, and this works behind NAT too. A sync older than
  version 2 does not announce pull delivery. For such a host, Beacon calls
  the agent itself when the pull arrives. Either way the pull is the
  liveness signal, so Beacon needs no scheduler.
- **The reply goes back to the original caller as a new message** from the
  target. Hermes's A2A tools cannot poll a task for a late result, so a
  late answer must arrive like any other message. Replies are never
  answered again, so nothing loops.
- **Identity is kept end to end.** The caller presents its own pairwise
  token for the target. Beacon checks it, then delivers with that same
  token, so the target sees the real sender. Beacon never accepts its own
  token from an agent, so no agent can pose as Beacon.
- **Only messages that never arrived are queued:** no connection, or HTTP
  502/503/504 from a proxy in front of the agent. A refusal, or a read
  timeout after the agent may have started on the message, is returned as
  an error instead. Retrying either could deliver the message twice.
- **Messages expire after 72 hours.** The sender then gets a notice.
- **Across networks:** the same endpoint relays between networks. It is
  the plain-name peer entry for agents on another network. The caller must
  reach Beacon at `BEACON_PUBLIC_URL`, and Beacon must reach the target.
- **Agents that Beacon cannot reach** still get held messages and late
  replies, through their own sync (pull delivery). They need only to reach
  Beacon.

The messages live in the operational store, `state/beacon.db` (SQLite;
`BEACON_STATE_DIR` changes the directory). In Docker it is the `./state`
volume. It is runtime state: never commit it, and back it up like `fleet/`.

**Moving Beacon.** Each pulled config includes `"beacon"`: the URL that
the agent's next sync should call. To move Beacon, run the new instance
with the same `fleet/`, `state/`, and `BEACON_A2A_SECRET`. Then set the
old instance's `BEACON_PUBLIC_URL` to the new URL. Every agent's sync
switches over on its next pull, and nobody has to touch the agents' hosts.
Keep the old instance until every agent has moved.

### Fleet board

Shared cards for work that spans agents, hosts, or days. Hermes Kanban
stops at the host boundary, and A2A caps one thread at a few turns. A card
is the durable record: title, description, project, assignee, state
(`todo`, `doing`, `blocked`, `review`, `done`), and a history. The history
records every creation, move, reassignment, and comment, each with its
author. It also records the A2A `context_id` the work happened under.
Cards live in the operational store, not in fleet YAML.

**Who uses it, and how:**

- **People:** the **Board** tab in the UI, or `/api/board`
  (`GET /api/board`, `GET|PATCH /api/board/cards/{id}`,
  `POST /api/board/cards`, `POST /api/board/cards/{id}/comments`). A
  logged-in person is recorded by email.
- **Agents and MCP clients:** the MCP endpoint `/board/mcp` on Beacon
  itself, with the tools `board_list`, `board_card`, `card_create`,
  `card_update`, and `card_comment`. It is served at the address agents
  already reach for their mesh config. An agent authenticates with its own
  pull token and is recorded under its own name. The MCP bearer is
  recorded as `beacon`. The endpoint has board tools only; fleet operations
  stay in the separate MCP service.

**Assigning a card to an agent sends that agent a notice** through the
mailbox, so it hears about the work even while offline. The notice
contains the card itself, so the agent can act even without the board
tools.

**Board tools are opt-in per agent**, with `reach.board: true`. Hermes
offers an MCP server on every chat platform. For an agent whose chat is
open to strangers, strangers could then write to the board, and an
assignment would reach other agents. That is a way around an allowlisted
agent's restrictions. For each opted-in agent, the host sync adds this to
`config.yaml`:

```yaml
mcp_servers:
  beacon-board:
    url: <BEACON_PUBLIC_URL>/board/mcp
    headers: {Authorization: "Bearer ${env:BEACON_BOARD_TOKEN}"}
```

The token goes into the profile `.env`. The gateway restarts once, because
Hermes connects MCP servers at startup. If `board` is turned off later, the
server entry is disabled and the token is removed. `/board/mcp` enforces
the opt-in as well: an agent without `reach.board` is refused even when it
presents its valid pull token.

To connect Claude Code:

```bash
claude mcp add --scope user --transport http beacon-board http://127.0.0.1:8642/board/mcp \
  --header "Authorization: Bearer $BEACON_MCP_TOKEN"
```

### `fleet/templates/<name>.yaml`

A config fragment applied to many agents at once — "every EU relay runs this
model stack" declared in one place instead of copied into every agent file.

### `fleet/cron_jobs/<id>.yaml`

A scheduled, recurring Beacon action against one or more agents. The scheduler
is external: a system cron/launchd/k8s CronJob calls
`python -m backend.scheduler tick` every minute; Beacon itself has no
background scheduler process.

```yaml
id: nightly-restart
enabled: true
schedule: "0 2 * * *"      # standard 5-field cron, UTC unless you override `timezone`
timezone: UTC
command:
  action: restart           # restart | push_config | status
target_agent_ids:
  - edge-01-primary
  - edge-02-primary
owner: ops
notes: "Restart gateways during the maintenance window"
```

`push_config` pushes `desired.config` to each target agent and restarts its
gateway if active. `status` just collects and stores the result. Arbitrary
shell commands are intentionally not supported in v1 — only the fixed,
auditable Beacon actions above. See [Cron jobs](#cron-jobs) for operating it.

## The key paths below are the **real Hermes schema** (verified against
hermes-agent v2026.9.7+):

```yaml
# fleet/templates/anthropic-stack.yaml
config:
  model:
    default: claude-sonnet-4-5     # bare model id, routed via model.provider
    provider: anthropic
    aliases:                       # free-form role -> provider/model routes
      fast: openrouter/google/gemini-2.5-flash
  fallback_providers:              # ordered chain, pushed as one JSON list
    - provider: openrouter
      model: anthropic/claude-sonnet-4
      base_url: https://openrouter.ai/api/v1
      key_env: OPENROUTER_API_KEY
      api_mode: chat_completions
  auxiliary:
    compression:                   # context compaction / summarization
      provider: openrouter
      model: google/gemini-2.5-flash
env_keys: [ANTHROPIC_API_KEY, OPENROUTER_API_KEY]
```

A second shipped example, `fleet/templates/orchestration.yaml.example`,
covers the `delegation` section — how an agent orchestrates subagents
(`orchestrator_enabled`, `max_concurrent_children`, `max_spawn_depth`,
`subagent_auto_approve`, plus optional per-subagent model pinning and a
separate `delegation.fallback_providers` chain).

A template carries only `config` and `env_keys` (the same shape as those two
keys inside an agent's `desired`). Host-shaping fields — `install_mode`,
`os_user`, `service`, `log_path` — stay per-agent on purpose: a shared
`install_mode` is a good way to break several installs at once.

An agent opts in by listing template names in `templates:`. On every read,
`backend/templates.py` deep-merges those fragments **in listed order**, then
the agent's own `desired` on top — the agent always wins. dicts deep-merge;
scalars and lists are replaced wholesale (`fallback_providers: [...]` in an
agent fully replaces the template's, no element merge). The merge never
touches the file: the agent record keeps its own overrides only, and
`GET /api/agents/{id}` returns both the raw `desired` and the merged
`effective_desired`.

`POST /api/templates/{name}/apply` with `{"agent_ids": [...], "confirm": true}`
adds a template name to several agent records in one call.

#### Config schema guardrail

`hermes config set` only hard-refuses one narrow typo shape (a known key
under the wrong prefix); a plain wrong subkey like `model.primary` is
**written anyway** with a post-write notice and exit code 0 — so a push
would report success while the key does nothing. Beacon therefore validates
every `desired.config`/template path itself before a push, against a
checked-in snapshot of Hermes's `DEFAULT_CONFIG`
(`backend/drivers/hermes_config_schema.yaml`):

- **unknown subkey of a known section** (`model.primary`,
  `delegation.max_childs`) — `push_config` refuses with a 400 naming every
  bad path and a did-you-mean suggestion; `config_diff` flags the finding as
  `schema: "error"` (rendered critical in the UI)
- **unknown top-level key** — legal in Hermes (custom keys bridge to the
  environment for skills), so it pushes, but with a streamed warning and a
  `schema: "warn"` flag in the diff
- free-form sections (`model.aliases.<role>`, `mcp_servers.<name>.*`,
  `platforms.<name>.*`, …) accept anything, matching the CLI

Regenerate the snapshot when the fleet's Hermes version moves:

```bash
python3 scripts/dump_hermes_config_schema.py [path/to/hermes-agent]
```

## Capabilities

| Capability | What it does | Where |
|---|---|---|
| **Deploy** | Installs and starts the gateway. Three modes: `simple` (fresh single-user install), `add-profile` (a named profile on an existing install), `new-user` (create the OS account first, then install under it). Idempotent — safe to re-run. | `POST /api/agents/{id}/deploy` (streamed) |
| **Track** | Live status: running/stopped/failed/crash-looping/not-installed, PID, uptime. Polled by the fleet table every 30s. | `GET /api/agents/{id}/status` |
| **Troubleshoot** | Recent log tail — the app-level log file, falling back to `journalctl` when a unit has never started successfully. | `GET /api/agents/{id}/logs` |
| **Reconcile** | Diagnoses drift patterns found in the wild — an orphaned unit (profile dir deleted, unit left behind), a unit stuck `failed`, installed-but-not-started, linger disabled. Dry run by default; each finding names a `fix` to apply individually. | `GET` (dry run) / `POST` (apply one fix) `/api/agents/{id}/reconcile` |
| **Config** | Compares `desired.config`/`desired.env_keys` against the live `config.yaml` and `.env` **key names only** (secret values never leave the host). Push validates every declared path against the known Hermes config schema first — an unknown subkey of a known section is refused (the CLI would write it silently), unknown top-level keys push with a warning — then runs `hermes config set` per declared key and restarts the gateway if it's active. Both sides see the template-merged `desired`. | `GET`/`POST /api/agents/{id}/config-diff` |
| **Templates** | A `fleet/templates/*.yaml` config fragment (`config` + `env_keys` only) merged under many agents' `desired` at once — model stacks, fallbacks, and auxiliary-task models declared once. Apply adds a template name to a batch of agent records. | `GET /api/templates`, `GET /api/templates/{name}`, `POST /api/templates/{name}/apply` |
| **Plugins** | Lists installed plugins with version, enabled/disabled state, and source. Updating one (git pull) can trip Hermes's own security scan and auto-disable it — that's surfaced as a `disabled_by_scan` flag rather than left buried in scan-report text, since an auto-disabled plugin can mean a messaging platform silently goes offline. | `GET /api/agents/{id}/plugins`, `POST /api/agents/{id}/plugins/{name}/update` |
| **Restart** | Plain `gateway restart` — the everyday operate action, works regardless of current state. Distinct from Reconcile's problem-triggered fixes. | `POST /api/agents/{id}/restart` |
| **Update** | `hermes update` on the shared code checkout — affects every profile on that install, not just one agent's record of it. Streamed. | `POST /api/agents/{id}/update` |
| **Decommission** | Stops and uninstalls the gateway, optionally purges the profile's data (refused for the default profile — that directory is shared with every other profile on the install) and optionally deletes the OS account. Moves the YAML record to `fleet/decommissioned/` rather than deleting it. | `POST /api/agents/{id}/decommission` (streamed) |

## Autonomy tiers

Every capability that changes something (not just reads it) is registered in
[`backend/tiers.py`](backend/tiers.py) against a T0–T5 autonomy tier — how
much oversight it needs, from "always allowed" to "human only." `GET
/api/tiers` lists the full registry with the rationale for each assignment.

**What's enforced today:** every capability at T2 or above requires an
explicit `confirm=true` before it runs — call without it and the endpoint
describes what it would do instead of doing it. This was previously only a
courtesy the MCP server extended to its own callers; it's now a property of
the API itself, so a direct HTTP call gets the same plan-before-apply step
an MCP client always had. `decommission` additionally escalates from T4 to
T5 when `purge` or `remove_user` is set — those are irreversible, the
baseline decommission (stop + uninstall) is not.

**What's named but not yet enforced:** T3 ("review") and T4/T5 ("human
approval") are recorded per capability, but Beacon has no reviewer or
approval-chain mechanism yet — there's no way today to check that a review
or approval actually happened before `confirm=true` was sent. The tier is
visible and queryable; the gate behind it is still just the T2 confirm step
for everything, until that mechanism exists. See ROADMAP.md.

## Teleport / tbot

Hosts can be reached two ways: a static SSH key (`ssh.key`), or a
[Teleport](https://goteleport.com) Machine ID identity (`ssh.config_file`) —
no long-lived key material on disk at all, short-lived certs instead, and
every command Beacon runs shows up in Teleport's audit log under its own bot
identity.

**One-time cluster setup** (run by a Teleport admin):

```bash
tctl bots add beacon --roles=agent
```

This prints a join token. First run of tbot consumes it; every renewal after
that uses tbot's own stored identity, so the token is never needed again
unless `tbot/storage/` is wiped.

**Running tbot — two ways:**

- **Docker (recommended, matches `docker-compose.yml`)**: the `tbot`
  service builds `tbot/Dockerfile` and runs continuously alongside `beacon`,
  writing to `./tbot/data` (bind-mounted, shared with the `beacon`
  container at `/tbot-data`) and persisting its own renewable identity in
  `./tbot/storage`. First-ever run still needs a token — see
  [Quickstart → Docker](#docker) — every run after reuses stored state, no
  token needed, and both containers coming up fresh just resumes renewal
  where it left off.
- **Bare host process** (`./tbot/run.sh`) — useful for local (non-Docker)
  dev. Writes to the same `tbot/data`/`tbot/storage` layout.

```bash
./tbot/run.sh <join-token>   # first run
./tbot/run.sh                # every run after
```

Either way this produces `tbot/data/ssh_config` (plus keys, certs,
`known_hosts`) and renews continuously. Point a host's `ssh.config_file` at
that path (`/tbot-data/ssh_config` from inside the `beacon` container,
`tbot/data/ssh_config` for a bare local run) and `ssh.address` at the
Teleport-qualified node name (`<node>.<your-cluster-domain>`).

`tbot/data/` and `tbot/storage/` are gitignored — they're private key
material, never committed.

Only run one tbot at a time against a given `./tbot/storage` — Teleport
tracks a strictly-increasing generation counter per identity specifically
to detect two processes renewing from the same stored credential (that
pattern is what a leaked/cloned credential looks like), and will lock the
bot out if it sees it. Switching from the bare-process run to the Docker
sidecar means stopping the host process first, not running both.

## Authentication

Login against [Zitadel](https://zitadel.com) (or any OIDC provider) via
authorization code + PKCE — `backend/auth.py`. Optional: set nothing and
Beacon runs open, same as before this existed. Set `ZITADEL_ISSUER` and
`ZITADEL_CLIENT_ID` and every route except `/auth/*` requires a logged-in
session — a browser hitting `/` gets redirected to `/auth/login`, an
unauthenticated API call gets a clean 401. Set `BEACON_ALLOWED_EMAILS` to a
comma-separated exact email allowlist; it is required when OIDC is enabled and
accounts outside it are rejected at callback time. Authenticated browser mutations require a
same-origin `Origin`/`Referer` header; MCP bearer calls are exempt because they do not use
browser cookies. Sessions are Secure by default, expire after 8 hours, and `POST /auth/logout`
clears them. Set `BEACON_SESSION_HTTPS_ONLY=false` only for local HTTP development.

**Zitadel setup:**

1. Create a **Web** application, auth method **PKCE** (public client — no
   secret to manage or leak).
2. Redirect URI: `http://localhost:8642/auth/callback` for local testing;
   add your real domain's equivalent alongside it when you have one —
   Zitadel apps take multiple redirect URIs.
3. Copy the generated client ID into `.env` (see `.env.example`):

   ```bash
   ZITADEL_ISSUER=https://id.example.com
   ZITADEL_CLIENT_ID=<the generated numeric ID>
   BEACON_SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
   BEACON_ALLOWED_EMAILS=tom@example.com
   ```

The client ID isn't sensitive (PKCE public clients don't hold a secret) —
`BEACON_SESSION_SECRET` is what actually needs protecting, it signs the
session cookie. `.env` is gitignored; `docker-compose.yml` reads it
automatically.

Session is a signed cookie (Starlette's `SessionMiddleware`, `itsdangerous`)
holding `sub`/`email`/`name` from the verified ID token — no server-side
session store, no database. The GUI shows who's logged in top-right with a
logout link once `/auth/me` returns a user.

The MCP server has its own auth — see [MCP server](#mcp-server) — a static
`BEACON_MCP_TOKEN` rather than OIDC, since MCP clients are typically
configured with a fixed API key rather than walked through a browser login.

## MCP server

`mcp/server.py` exposes the fleet over the [Model Context Protocol](https://modelcontextprotocol.io)
— a thin client of Beacon's own HTTP API, same contract the web GUI uses.
Runs as its own service (`docker compose up` starts it on `:8643`, endpoint
`/mcp`, streamable-http transport).

Two tiers, matching the API's own read/write split:

- **Read-only** (`list_hosts`, `list_agents`, `get_agent`, `get_status`,
  `get_logs`, `reconcile_check`, `config_diff`, `list_plugins`,
  `list_templates`, `get_template`) — unrestricted. An agent asking "what's
  broken" is exactly what Beacon is for.
- **Mutating** (`deploy`, `apply_fix`, `push_config`, `apply_template`,
  `decommission`, `restart`, `update_plugin`, `update_agent`) — each takes a
  `confirm: bool = False` parameter. Called without it, the tool describes
  what it *would* do and does nothing; only `confirm=true` actually runs
  it. This is on top of whatever confirmation UI the MCP client itself
  provides (tools are also annotated `destructiveHint=true` for clients
  that read that) — a misread instruction here is a much worse failure
  mode than a misclick, so it gets two independent gates, not one.

Point any MCP client at `http://localhost:8643/mcp` (or `http://mcp:8643/mcp`
from inside the compose network). `BEACON_URL` env var controls which
Beacon instance it talks to (defaults to `http://beacon:8642`, the compose
service name).

**Auth**: set `BEACON_MCP_TOKEN` (see `.env.example`) and every request to
the MCP server needs `Authorization: Bearer <token>` — a static shared
secret, not OIDC, since MCP clients are typically configured with a fixed
API key rather than walked through a browser login. Unset, MCP runs open,
same convention as everything else here. The same token doubles as MCP's
own credential when it calls *Beacon's* API on your behalf: once
`ZITADEL_ISSUER`/`ZITADEL_CLIENT_ID` are set, Beacon requires a session for
everything, and a service has no browser to complete a PKCE redirect with —
`Authorization: Bearer <BEACON_MCP_TOKEN>` is accepted by Beacon's own
auth middleware as an alternative to a session cookie, scoped to exactly
this one credential.

## API reference

| Method | Path | Purpose |
|---|---|---|
| `GET`/`PUT`/`DELETE` | `/api/hosts/{id}` | Host CRUD |
| `GET`/`PUT`/`DELETE` | `/api/agents/{id}` | Agent CRUD (`GET` also returns `effective_desired`, the template-merged view) |
| `GET` | `/api/agents/registry` | Who can be called, and the capabilities each declares. No endpoints, no key names |
| `POST` | `/api/agents/{id}/call` | Send one A2A message to a registered agent (`{"message": "...", "context_id": "..."}`) — see [Agent bus](#agent-bus) |
| `GET` | `/api/templates` | List templates, each with the agent ids using it |
| `GET` | `/api/templates/{name}` | One template's content + its `used_by` agents |
| `POST` | `/api/templates/{name}/apply` | Add a template to agents (`{"agent_ids": [...], "confirm": true}`) — T2 |
| `GET` | `/api/agents/{id}/status` | Live status |
| `GET` | `/api/agents/{id}/logs?lines=N` | Log tail |
| `GET` | `/api/tiers` | Tier registry — every gated capability, its tier, and why |
| `POST` | `/api/agents/{id}/deploy?confirm=true` | Install/bring up (streamed) — T3 |
| `GET`/`POST` | `/api/agents/{id}/reconcile` | Diagnose / apply one fix (`{"fix": "...", "confirm": true}`) — apply is T2 |
| `GET`/`POST` | `/api/agents/{id}/config-diff?confirm=true` | Compare / push desired config — push is T3 |
| `GET` | `/api/agents/{id}/plugins` | List installed plugins (name, version, status, source) |
| `POST` | `/api/agents/{id}/plugins/{name}/update?confirm=true` | Update one plugin (git pull) — T3 |
| `POST` | `/api/agents/{id}/restart?confirm=true` | Restart the gateway — T2 |
| `POST` | `/api/agents/{id}/update?confirm=true` | `hermes update` on the shared install (streamed) — T4 |
| `POST` | `/api/agents/{id}/decommission` | Tear down (`{"purge": bool, "remove_user": bool, "confirm": true}`, streamed) — T4, T5 if purge/remove_user |
| `GET`/`PUT`/`DELETE` | `/api/cron-jobs/{id}` | Cron job CRUD |
| `GET` | `/api/cron-jobs` | List cron jobs |

Every row above marked with a tier requires `confirm=true` (query param, or
a `confirm` field alongside an existing JSON body) — omit it and you get a
200 describing the action instead of it running. See [Autonomy
tiers](#autonomy-tiers).

Cron jobs are an exception: they do not use the `confirm=true` gate because
they are pre-authorized fleet records, persisted ahead of time and reviewed
like any other YAML. They are limited to a fixed set of v1 actions
(`restart`, `status`, `push_config`) to keep the blast radius bounded.

## Limitations

- **Single writer.** `fleet/*.yaml` has no locking — fine for one operator,
  not safe for two people editing the fleet at once.
- **One driver.** Hermes is the only agent type implemented. The driver
  interface is designed to support others; nothing has exercised that yet.
- **No audit trail.** Every logged-in user has full deploy/config/
  decommission power, and no record persists of who did what.
- **Pull-based only.** Status and Reconcile run when requested — nothing
  polls or alerts on its own.
- **`ssh.config_file` paths are absolute and machine-specific** — see
  [Teleport / tbot](#teleport--tbot).

See [ROADMAP.md](ROADMAP.md) for what's planned to close these.

## Cron jobs

Schedule Beacon-native actions against agents using `fleet/cron_jobs/*.yaml`.
There is no background scheduler inside Beacon; the web API only manages the
records. Execution is triggered externally once per minute:

```bash
.venv/bin/python -m backend.scheduler tick
```

List jobs and whether each is due right now:

```bash
.venv/bin/python -m backend.scheduler list
```

Dry-run a tick to see what would execute without touching any host:

```bash
.venv/bin/python -m backend.scheduler tick --dry-run
```

Use the API (or edit YAML directly) to create a job:

```bash
curl -X PUT http://localhost:8642/api/cron-jobs/nightly-restart \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "nightly-restart",
    "enabled": true,
    "schedule": "0 2 * * *",
    "command": {"action": "restart"},
    "target_agent_ids": ["edge-01-primary"]
  }'
```

A job's last-run status and output are persisted back to its YAML file, so
`GET /api/cron-jobs/{id}` and the scheduler list show history.

### Deployment hook examples

macOS launchd (run once per minute, UTC):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>net.cqre.beacon.cron</string>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>ProgramArguments</key>
    <array>
      <string>/path/to/beacon/.venv/bin/python</string>
      <string>-m</string>
      <string>backend.scheduler</string>
      <string>tick</string>
      <string>--utc</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/beacon</string>
  </dict>
</plist>
```

Linux systemd timer (`beacon-cron.service` + `beacon-cron.timer`):

```ini
# /etc/systemd/system/beacon-cron.service
[Unit]
Description=Beacon fleet cron tick

[Service]
Type=oneshot
WorkingDirectory=/path/to/beacon
ExecStart=/path/to/beacon/.venv/bin/python -m backend.scheduler tick --utc
```

```ini
# /etc/systemd/system/beacon-cron.timer
[Unit]
Description=Run Beacon fleet cron every minute

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s

[Install]
WantedBy=timers.target
```

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

No real SSH, no real filesystem outside a tmp dir: `tests/conftest.py`
redirects `fleet/` into a throwaway directory per test and swaps
`backend/drivers/hermes.py`'s `ssh` module for a fake that records the
command it was asked to run and hands back a canned result. That's enough
to catch the class of bug this project has actually shipped — a timeout too
short, a wrong command string, a validation gap — without needing a real
host. Runs in CI (`.github/workflows/ci.yml`) on every push, alongside a
build of all three Docker images.

## Project layout

```
backend/
  app.py          FastAPI routes
  auth.py         Zitadel OIDC login (PKCE) + the auth-gating middleware
  tiers.py        capability -> autonomy tier registry
  store.py        fleet/ YAML read/write
  ssh.py          the one place that shells out to `ssh`
  schemas.py      Host/Agent pydantic models
  templates.py    config templates: load, validate, deep-merge under `desired`
  drivers/
    hermes.py     the only driver so far
fleet/
  hosts.yaml.example
  agents/         one YAML file per agent (gitignored — real inventory)
  templates/      config fragments merged under many agents (gitignored)
  cron_jobs/      scheduled Beacon actions (gitignored)
  decommissioned/ archived agent records (gitignored)
frontend/
  index.html, app.js, styles.css   no build step
tbot/
  Dockerfile      the tbot sidecar image
  run.sh          starts tbot as a bare host process (local dev)
mcp/
  server.py       MCP server — a client of Beacon's own HTTP API
  Dockerfile
tests/            pytest — see Testing below
.github/workflows/ci.yml
.env.example      Zitadel issuer/client ID/session secret template
requirements-dev.txt
pytest.ini
Dockerfile        the beacon image
docker-compose.yml
```
