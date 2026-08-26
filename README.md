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
`-i`/`-p`.

### `fleet/agents/<id>.yaml`

```yaml
id: edge-01-primary
type: hermes          # selects the driver
host: edge-01         # must match a hosts.yaml id
profile: primary      # omit or "default" for Hermes's default profile
owner: tom
notes: "handles EU relay traffic"

desired:
  install_mode: simple       # simple | add-profile | new-user
  os_user: null              # required for install_mode: new-user
  service: null               # override the systemd unit name (rarely needed)
  log_path: null               # override the log file path (rarely needed)
  config: {}                   # config.yaml keys Beacon manages — see Config below
  env_keys: []                 # expected .env key NAMES — values never leave the host
```

Only what you actually declare is managed. An agent record with an empty
`desired` is valid — Beacon just won't have an opinion about it beyond
`status`/`logs`.

Nothing here holds live state (running/stopped, PID, current config).
`status()` and `config_diff()` always poll the host fresh — YAML never goes
stale in that direction.

## Capabilities

| Capability | What it does | Where |
|---|---|---|
| **Deploy** | Installs and starts the gateway. Three modes: `simple` (fresh single-user install), `add-profile` (a named profile on an existing install), `new-user` (create the OS account first, then install under it). Idempotent — safe to re-run. | `POST /api/agents/{id}/deploy` (streamed) |
| **Track** | Live status: running/stopped/failed/crash-looping/not-installed, PID, uptime. Polled by the fleet table every 30s. | `GET /api/agents/{id}/status` |
| **Troubleshoot** | Recent log tail — the app-level log file, falling back to `journalctl` when a unit has never started successfully. | `GET /api/agents/{id}/logs` |
| **Reconcile** | Diagnoses drift patterns found in the wild — an orphaned unit (profile dir deleted, unit left behind), a unit stuck `failed`, installed-but-not-started, linger disabled. Dry run by default; each finding names a `fix` to apply individually. | `GET` (dry run) / `POST` (apply one fix) `/api/agents/{id}/reconcile` |
| **Config** | Compares `desired.config`/`desired.env_keys` against the live `config.yaml` and `.env` **key names only** (secret values never leave the host). Push runs `hermes config set` per declared key, then restarts the gateway if it's active. | `GET`/`POST /api/agents/{id}/config-diff` |
| **Plugins** | Lists installed plugins with version, enabled/disabled state, and source. Updating one (git pull) can trip Hermes's own security scan and auto-disable it — that's surfaced as a `disabled_by_scan` flag, not left buried in scan-report text, because it silently took a live messaging platform offline once already in testing. | `GET /api/agents/{id}/plugins`, `POST /api/agents/{id}/plugins/{name}/update` |
| **Restart** | Plain `gateway restart` — the everyday operate action, works regardless of current state. Distinct from Reconcile's problem-triggered fixes. | `POST /api/agents/{id}/restart` |
| **Update** | `hermes update` on the shared code checkout — affects every profile on that install, not just one agent's record of it. Streamed. | `POST /api/agents/{id}/update` |
| **Decommission** | Stops and uninstalls the gateway, optionally purges the profile's data (refused for the default profile — that directory is shared with every other profile on the install) and optionally deletes the OS account. Moves the YAML record to `fleet/decommissioned/` rather than deleting it. | `POST /api/agents/{id}/decommission` (streamed) |

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
  where it left off (confirmed: handing an established `./tbot/storage`
  from a host-process run over to the container mid-session renewed clean,
  generation N → N+1, no clone-detection lockout).
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
unauthenticated API call gets a clean 401.

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
`BEACON_MCP_TOKEN` rather than OIDC, since Zitadel service-user
`client_credentials` for this instance never got resolved (see the v0.3.0
changelog entry) and a static API key is the more natural fit for MCP
clients anyway.

## MCP server

`mcp/server.py` exposes the fleet over the [Model Context Protocol](https://modelcontextprotocol.io)
— a thin client of Beacon's own HTTP API, same contract the web GUI uses.
Runs as its own service (`docker compose up` starts it on `:8643`, endpoint
`/mcp`, streamable-http transport).

Two tiers, matching the API's own read/write split:

- **Read-only** (`list_hosts`, `list_agents`, `get_agent`, `get_status`,
  `get_logs`, `reconcile_check`, `config_diff`, `list_plugins`) —
  unrestricted. An agent asking "what's broken" is exactly what Beacon is
  for.
- **Mutating** (`deploy`, `apply_fix`, `push_config`, `decommission`,
  `restart`, `update_plugin`, `update_agent`) — each takes a
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
| `GET`/`PUT`/`DELETE` | `/api/agents/{id}` | Agent CRUD |
| `GET` | `/api/agents/{id}/status` | Live status |
| `GET` | `/api/agents/{id}/logs?lines=N` | Log tail |
| `POST` | `/api/agents/{id}/deploy` | Install/bring up (streamed) |
| `GET`/`POST` | `/api/agents/{id}/reconcile` | Diagnose / apply one fix (`{"fix": "..."}`) |
| `GET`/`POST` | `/api/agents/{id}/config-diff` | Compare / push desired config |
| `GET` | `/api/agents/{id}/plugins` | List installed plugins (name, version, status, source) |
| `POST` | `/api/agents/{id}/plugins/{name}/update` | Update one plugin (git pull) |
| `POST` | `/api/agents/{id}/restart` | Restart the gateway |
| `POST` | `/api/agents/{id}/update` | `hermes update` on the shared install (streamed) |
| `POST` | `/api/agents/{id}/decommission` | Tear down (`{"purge": bool, "remove_user": bool}`, streamed) |

## Project layout

```
backend/
  app.py          FastAPI routes
  auth.py         Zitadel OIDC login (PKCE) + the auth-gating middleware
  store.py        fleet/ YAML read/write
  ssh.py          the one place that shells out to `ssh`
  schemas.py      Host/Agent pydantic models
  drivers/
    hermes.py     the only driver so far
fleet/
  hosts.yaml.example
  agents/         one YAML file per agent (gitignored — real inventory)
  decommissioned/ archived agent records (gitignored)
frontend/
  index.html, app.js, styles.css   no build step
tbot/
  Dockerfile      the tbot sidecar image
  run.sh          starts tbot as a bare host process (local dev)
mcp/
  server.py       MCP server — a client of Beacon's own HTTP API
  Dockerfile
.env.example      Zitadel issuer/client ID/session secret template
Dockerfile        the beacon image
docker-compose.yml
```
