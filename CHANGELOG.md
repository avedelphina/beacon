# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) once there's
an API worth being stable about — pre-1.0, breaking changes can land in a
minor bump.

## [0.2.0] — 2026-08-26

### Added

- tbot Machine ID sidecar (`tbot/Dockerfile`, `tbot` service in
  `docker-compose.yml`) — no longer a bare host process the container just
  reads from. Verified live: handed an already-established `tbot/storage`
  identity from a running host process to the container mid-session and it
  renewed clean (generation 4 → 5), no clone-detection lockout.
- MCP server (`mcp/server.py`, `mcp` service, port 8643) exposing the fleet
  over the Model Context Protocol — a thin client of Beacon's own HTTP API.
  Read tools (status/logs/list/reconcile-check/config-diff) unrestricted;
  mutating tools (deploy/apply_fix/push_config/decommission) require an
  explicit `confirm=true`, describing the action instead of taking it
  otherwise. Verified live against a real MCP client: tool listing,
  annotations, all 13 real fleet agents round-tripped correctly, confirm
  gating held on both `deploy` and `decommission`.

## [0.1.0] — 2026-08-26

Initial build: a working fleet console for Hermes agents, end to end.

### Added

- YAML-backed registry (`fleet/hosts.yaml`, `fleet/agents/*.yaml`), no
  database — a small FastAPI backend and a vanilla-JS frontend over it.
- SSH transport (`backend/ssh.py`) via the system `ssh` binary, either a
  static key or an `ssh_config` file — the latter lets a host connect
  through a Teleport Machine ID (tbot) identity instead of a long-lived key.
- Hermes driver (`backend/drivers/hermes.py`):
  - **Deploy** — three modes (`simple`, `add-profile`, `new-user`), idempotent,
    streamed live to the GUI.
  - **Track** — live systemd `--user` status, including a `crashlooping`
    state distinct from plain `inactive` for a unit stuck in `activating`/
    `auto-restart`.
  - **Troubleshoot** — log tail, falling back to `journalctl` when a unit
    has never started successfully and has no log file yet.
  - **Reconcile** — dry-run diagnosis of drift patterns found on real hosts
    (orphaned unit, stuck-failed unit, installed-but-not-started, linger
    disabled), with a named fix to apply per finding.
  - **Config** — diffs `desired.config`/`desired.env_keys` against the live
    `config.yaml` and `.env` key names (never values), and can push
    declared keys via `hermes config set`.
  - **Decommission** — stop, uninstall, optionally purge profile data or
    delete the OS account, archive the record to `fleet/decommissioned/`.
- Docker packaging (`Dockerfile`, `docker-compose.yml`) — tbot still runs as
  a host process for now, bind-mounted into the container.

### Known gaps

- tbot isn't containerized yet — it's a host process bind-mounted in.
- `ssh.config_file` paths are absolute and machine-specific; nothing
  resolves them relative to a portable location yet.
- No auth on the web UI or API — fine bound to localhost, not fine exposed
  beyond it.
- One driver (Hermes). The driver seam exists; nothing has exercised it with
  a second agent type yet.
