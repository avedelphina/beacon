# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/) once there's
an API worth being stable about — pre-1.0, breaking changes can land in a
minor bump.

## [0.4.0] — 2026-08-27

### Added

- MCP server auth: `BEACON_MCP_TOKEN`, a static bearer token rather than
  OIDC (Zitadel service-user `client_credentials` for this instance never
  got resolved — see the v0.3.0 entry). Same token doubles as MCP's own
  credential when it calls Beacon's API, since a service has no browser to
  complete a PKCE redirect with — Beacon's `AuthMiddleware` now accepts
  `Authorization: Bearer <BEACON_MCP_TOKEN>` as an alternative to a session.
- Plugins: `list_plugins` (name, version, enabled/disabled, source — via
  `hermes plugins list --json`) and `update_plugin` (git pull one plugin
  by name), both in the API, GUI Inspect panel, and as MCP tools.
- `restart` — plain `gateway restart`, the everyday operate action, distinct
  from Reconcile's problem-triggered fixes.
- `update_agent` — `hermes update --yes` on the shared code checkout,
  streamed like Deploy.

### Fixed / found during testing

- **Real production impact**: updating `deltachat-platform` on a live agent
  pulled a new upstream revision that tripped Hermes's own security scanner
  (traversal/exfiltration heuristics fired on the plugin's own test
  fixtures — strings like `"../../../etc/passwd"` used as test data for
  path-traversal *prevention* tests). Hermes auto-disabled the plugin as a
  result, silently taking the agent's only messaging channel offline.
  Restored it (re-enable + restart) within about 90 seconds of noticing.
  `update_plugin`'s result now carries a `disabled_by_scan` flag so this
  shows up as a first-class warning instead of being buried in a wall of
  scan-report text — the GUI alerts on it explicitly rather than requiring
  the operator to read the output to notice their platform went dark.
- `list_plugins`' `name` field (the plugin's manifest name) isn't always
  what `plugins update` expects — it wants the installed directory name.
  Real example: listed as `deltachat`, updated as `deltachat-platform`.
  Documented on `update_plugin`; no clean way to resolve one from the other
  short of guessing, so this is on the caller.
- `restart`'s first live test timed out at the generic 60s default — a
  graceful restart drains in-flight turns and waits for the new process to
  report runtime-ready, which took longer in practice. Bumped to 120s.
  (The CLI's own output names a much higher ceiling — "waiting up to 1815s
  for in-flight turns + drain" — which 120s still doesn't cover; treating
  that as an acceptable fast-path default rather than building full
  progress streaming for a rare edge case.)
- `docker-compose.yml` defined `BEACON_MCP_TOKEN` for the `mcp` service but
  not `beacon` — the one that actually needs to check it. First integration
  test failed with a 401 from Beacon's own API until this was caught.

## [0.3.0] — 2026-08-27

### Added

- Login against Zitadel (or any OIDC provider) via authorization code +
  PKCE (`backend/auth.py`). Every route except `/auth/*` requires a
  session once `ZITADEL_ISSUER`/`ZITADEL_CLIENT_ID` are set — unset,
  Beacon still runs open. Session is a signed cookie
  (`itsdangerous`/`SessionMiddleware`), no server-side session store.
  GUI shows the logged-in user + a logout link.
- `.env.example` for the three vars this needs; `docker-compose.yml`'s
  `beacon` service reads them from `.env` (gitignored).
- Verified end to end: unauthenticated `/` redirects to `/auth/login`,
  unauthenticated `/api/*` gets a clean 401, and a real login against a
  live Zitadel instance completes the full round trip (PKCE challenge,
  code exchange, ID token signature verification via JWKS, session
  cookie) both locally and containerized.

### Known gaps

- MCP server (port 8643) has no auth yet. Zitadel service-user
  `client_credentials` for machine-to-machine auth hit a `client not
  found` error across every combination tried (both auth-header styles,
  three regenerated secrets, numeric and named client ID, org-scoped and
  unscoped) — looks structural on the Zitadel/proxy side, not a
  credential issue, and needs the instance's own audit log to diagnose.
  Don't expose 8643 beyond a trusted network until this is sorted.

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
