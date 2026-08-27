# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) once there's
an API worth being stable about — pre-1.0, breaking changes can land in a
minor bump. See [README.md](README.md#limitations) for current limitations
and [ROADMAP.md](ROADMAP.md) for what's planned.

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
