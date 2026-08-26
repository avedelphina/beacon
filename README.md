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
docker compose up -d --build
```

`fleet/` is bind-mounted, so your inventory persists across rebuilds and is
editable from the host. See [Teleport / tbot](#teleport--tbot) for what the
second bind mount in `docker-compose.yml` is for.

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

**Running tbot:**

```bash
./tbot/run.sh <join-token>   # first run
./tbot/run.sh                # every run after
```

This writes `tbot/data/ssh_config` (plus keys, certs, `known_hosts`) and
renews continuously. Point a host's `ssh.config_file` at that path and
`ssh.address` at the Teleport-qualified node name
(`<node>.<your-cluster-domain>`).

`tbot/data/` and `tbot/storage/` are gitignored — they're private key
material, never committed.

**Docker note**: `docker-compose.yml` bind-mounts `./tbot/data` into the
container at the *same absolute path* it has on the host. The generated
`ssh_config` hardcodes `IdentityFile`/`CertificateFile`/`UserKnownHostsFile`
as absolute paths under wherever `--destination` pointed when tbot ran, so
the mount target has to match rather than get remapped — this also means
`fleet/hosts.yaml`'s `ssh.config_file` value works unchanged in and out of
the container. tbot itself isn't containerized yet; it runs as a host
process and the container just reads its output. A real tbot sidecar
container is the natural next step.

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
| `POST` | `/api/agents/{id}/decommission` | Tear down (`{"purge": bool, "remove_user": bool}`, streamed) |

## Project layout

```
backend/
  app.py          FastAPI routes
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
  run.sh          starts the Teleport Machine ID bot
Dockerfile
docker-compose.yml
```
