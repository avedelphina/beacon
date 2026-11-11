# Changelog

## [Unreleased]

## [0.33.0] — 2026-11-11

### Added

- **Agents' answers to board notices are kept on the card.** An assignment,
  comment or reminder reached the agent, but what it answered was thrown away,
  so nobody could tell an agent that had not synced yet from one that was
  working or one that never got the message. The answer is now an `answer`
  event in the card's history. It does not count as a change to the card, so
  reminders still apply.
- Notices carry a push address, like messages: when the agent's turn outlasts
  the sync's wait, the notice is `working` (it was marked `delivered`), and the
  answer arrives when the turn ends. A notice whose answer never comes is
  `delivered` after the reply window, with the reason, and nobody is told it
  expired.
- The Board tab shows each open card's latest notice at a glance (waiting for
  the agent's sync, the agent is on it, answered, failed), and the card's
  history names the recipient on each notice. `GET /api/board` gives it as
  `delivery`.
- Agent skill 1.9.0: answer a board message in one or two sentences, because
  that answer is what people see.

## [0.32.1] — 2026-11-11

### Added
- Board: a Refresh button reloads the cards without reloading the page.

### Changed
- The top bar (brand and tabs) now stays at the top of the page while you scroll.

## [0.32.0] — 2026-11-11

### Changed

- **`beacon` and `tbot` now run unprivileged by default** (uid 10001, as `mcp`
  already did). A one-shot `init` service runs first and owns `./state`,
  `./tbot/data` and `./tbot/storage` for that user, so upgrading is
  `docker compose up -d --build` with no manual `chown`. `BEACON_RUN_AS=0:0`
  restores root. Closes #25.
- **Upgrade notes.** A static SSH key in `./ssh` must be readable by uid 10001
  (`sudo chown 10001 ssh/fleet_ed25519`); `init` leaves `./ssh` alone. Files
  under `./state` and `./tbot` are now owned by 10001, so editing them by hand
  needs `sudo`. See README, "Running unprivileged".

## [0.31.1] — 2026-11-10

### Added

- **`beacon` and `tbot` can run unprivileged.** Set `BEACON_RUN_AS=10001:10001`
  in `.env` after owning `./state`, `./tbot/data` and `./tbot/storage` by that
  user (README, "Running unprivileged"). The images now have that account. It
  is off by default, so nothing changes on upgrade: the default would break
  stacks whose directories are owned by root. (#25)

## [0.31.0] — 2026-11-10

### Added

- **An audit log of actions on hosts.** Every action that passes the confirm
  gate (T2 and up) is recorded in the store: when, who (a person's email, or
  `beacon` for the MCP token or an agent), how they were authenticated
  (`session`, `mcp`, `agent`, `open`), the capability, tier and target, what
  the gate said it would do, and the outcome. Deploy, update, config push and
  decommission record how they ended (`succeeded`, `failed`, `did not finish`,
  `interrupted`); the others stay `requested`. Read it with
  `GET /api/audit?limit=&target=&actor=`, operators and the MCP token only. The
  cron scheduler's own runs are not recorded, and nothing alerts on the log
  yet. (#27)

## [0.30.5] — 2026-11-10

### Fixed

- **A host that agents use cannot be deleted.** `DELETE /api/hosts/{id}` answers
  `409` and names the agents, instead of leaving them with "host not found" on
  their next status, deploy or logs call. Delete or move the agents first. (#18)
- **Deleting an agent says what it does not do.** The confirmation now says it
  removes only Beacon's record, that a deployed gateway keeps running with
  nothing tracking it, and points to Decommission. The delete buttons show an
  error instead of failing silently. (#18)

## [0.30.4] — 2026-11-10

### Fixed

- **A streamed operation says how it ended.** Deploy, update and config push
  end their output with `✓ succeeded (exit 0)`, `✗ failed (exit N)` or
  `✗ did not finish` (no exit status: the connection dropped or the time limit
  passed). The response is 200 either way, since it starts before the command
  ends, so before you had to read the log to tell. (#30)

### Documentation

- **Static SSH keys under Docker.** The container sees only `./ssh`, mounted at
  `/etc/beacon`; the README and `hosts.yaml.example` now say to put the key and
  `known_hosts` there and use those paths. (#23)

## [0.30.3] — 2026-11-10

### Changed

- **Action needed for Teleport users:** the tbot proxy is no longer hardcoded.
  Set `TELEPORT_PROXY_SERVER` (for example `teleport.example.com:443`) in
  `.env` for the compose stack, and in the environment for `tbot/run.sh`. The
  compose default is a placeholder, so a stack that uses Teleport hosts does
  not connect until you set it. (#26)
- **`tbot/run.sh` takes the join token from the environment or a file**, not
  as an argument: `TBOT_TOKEN=<token> ./tbot/run.sh` or
  `TBOT_TOKEN_FILE=/path ./tbot/run.sh`. On a command line `ps` shows the
  token to every user on the host. Giving it as an argument now fails with a
  message. (#26)
- Containers: `beacon` and `mcp` have healthchecks, and `mcp` waits for
  `beacon` to be healthy. All three have memory caps (`BEACON_MEM_LIMIT` 1g,
  `MCP_MEM_LIMIT` 256m, `TBOT_MEM_LIMIT` 256m; raise them in `.env` if a
  service is killed). `mcp` runs as an unprivileged user. (#25)
- CI actions are pinned by commit SHA, with the tag in a comment. (#25)

### Fixed

- **Login works behind a TLS-terminating reverse proxy.** `BEACON_TRUSTED_PROXIES`
  names the proxy whose `X-Forwarded-*` headers uvicorn believes (default
  `127.0.0.1`), so the OIDC redirect URI is built as `https://`. See
  [Authentication](README.md#authentication). (#24)

### Known gaps

- `beacon` and `tbot` still run as root. `beacon` writes to the `./state` bind
  mount and reads tbot's private key material, so an unprivileged user needs
  matching ownership of those host directories; that is its own change. (#25)

## [0.30.2] — 2026-11-10

### Fixed

- **`sudo` to another user never waits for a password.** `sudo -u <user> -i`
  in the Hermes driver (running a command as an agent's OS user, deploy under a
  new user, removing the user) now has `-n`, like the other `sudo` calls. A
  host whose sudo needs a password now fails at once instead of hanging until
  the timeout. (#14)
- **Decommission output stays open.** The window no longer closes when the
  stream ends, so the operator reads whether the teardown worked. (#29)
- **Irreversible decommission choices need typing.** Deleting an agent's
  profile data, or its OS account, asks for the agent id or user name; Enter on
  an empty box keeps the data. Before, OK meant delete. (#31)
- **An expired session goes to the login page.** Any `401` from the API
  sends the browser to `/auth/login`, instead of failing quietly call by
  call. (#32)

## [0.30.1] — 2026-11-10

### Fixed

- **A request with "changes requested" can be answered with a changed plan.**
  Before, `changes_requested` ended the request: only `pending` accepts a
  response, so the requester had to open a second request. The requester now
  sends the changed plan with `POST /api/workflows/{id}/revise`, the
  `workflow_revise` board tool, or the `revise_workflow` MCP tool. The request
  goes back to `pending` under the new plan revision and hash, with a new
  expiry, and the responder gets a notice. Only the requester may revise, only
  from `changes_requested`, and the plan binding must change; an approved
  request is never rewritten. The history keeps the earlier answer and a
  `revised` event. The agent skill is 1.8.0. (#81)
- **Plans in the Workflows tab are readable.** The summary keeps its line
  breaks (it ran together as one line), the requester shows, the plan is open
  while the request waits, and the plan hashes sit inside it. A request with
  "changes requested" shows the responder's note. (#82)

## [0.30.0] — 2026-11-09

### Added

- **A card says what moves it on.** Cards have three new optional fields:
  `next_action` (the next step), `done_when` (what must be true to close the
  card), and `links` (`[{url, label}]`, http and https only, at most 20, for a
  PR or an issue elsewhere). They are in `POST`/`PATCH /api/board/cards`, in
  the `card_create` and `card_update` board tools, and in the card view. The
  assignment notice and the reminders carry the next action, so an agent that
  is reminded about a card also reads what to do. A change to `next_action` or
  `done_when` is recorded in the history with its new text. A link is only a
  pointer: Beacon does not fetch it or copy its state, and it does not approve
  anything. Existing cards get the columns on the first start, empty.
- **Requests on the card.** The card view lists the card's decision, review,
  and approval requests with their state, responder, expiry, and answer. On
  the board, a card with a request that waits (`pending` or
  `changes_requested`) shows a badge. Requests are still answered in the
  Workflows tab.
- The agent skill (1.7.0) says to set `next_action` and `done_when`, and that
  a link proves nothing.

### Changed

- **A card's history is easier to read, on a phone most of all.** Comments
  show as text blocks with their author and time, keep their line breaks, and
  wrap long URLs and context ids instead of running off the screen. Moves,
  edits, notices, and reminders stay as short, quiet lines, and a notice
  shows its delivery state. On a phone the history no longer scrolls in a box
  of its own inside the card; on a wide screen the card is wider and the
  history opens at the newest entry. Dates are shorter (`9 Nov 2026, 16:09`).

## [0.29.0] — 2026-11-07

### Added

- **Beacon as an app, with notifications.** The page installs as a PWA (a web
  app manifest, icons, and a service worker) on iPhone and iPad from the Home
  Screen, on a Mac from Safari's Add to Dock, and from Chrome or Edge. A
  **Notify me** button in the header turns on Web Push for that browser, and
  Beacon sends a test notification at once. From then on, every notice that
  goes to people by email (workflow requests, self-healing restarts and
  give-ups, quiet and returning agents, stuck board cards) also goes as a push
  to the same people, with or without a mail server, at most 10 per hour to
  one person. A push holds only the subject line, never an agent's own text.
  It is encrypted to the browser (RFC 8291) and signed with a VAPID key that
  Beacon derives from `BEACON_A2A_SECRET`, so there is nothing new to
  configure. Beacon sends only to the browsers' push services (Apple, Google,
  Mozilla, Microsoft), whatever endpoint it is given. A subscription that the
  push service reports as gone is removed. New routes: `GET`/`POST`/`DELETE
  /api/push` (members too, for their own browser).

### Changed

- The manifest, the service worker and the icons load without a session, so
  that a browser can install the page and update the worker after the session
  ends.

## [0.28.0] — 2026-11-07

### Added

- **Board reminders.** A card that nothing changes on for the agent's
  `remind_after` (new field in the policy's `health` list; default `24h`,
  `never` turns it off) is brought up again with whoever has to move it: the
  assignee, or for a card in `review` or with nobody assigned, the agent that
  made it. Before, an agent heard about a card once, when it was assigned, and
  as it acts only in a turn, cards sat in `todo`, `doing`, `blocked` and
  `review` for days. The reminder rides the agent's own pull (so it is online),
  one notice lists all its waiting cards (one turn), and each card gets two,
  `remind_after` apart. Then operators and the owners involved get one email.
  Cards only a person can move (in `review` and made by their own assignee,
  assigned to a person, or waiting on an agent without board tools) go to
  people at once. Any change to a card starts over; the card's history records
  each `reminder` and `escalation`. The agent skill (1.6.0) says what to do
  with a reminder.
- The Policy tab's health table shows each agent's `remind_after`.

## [0.27.0] — 2026-11-06

### Added

- **Self-healing.** The fleet policy gets a `health` list (same selectors as
  the access rules, later entries win field by field). With `heal: true` the
  agent's host sync probes the agent on every run: `/health`, then `GetTask`
  for a missing task with Beacon's token, which proves the gateway accepts
  today's tokens. Two failures in a row (within 15 minutes, so a laptop's sleep
  doesn't count) restart the gateway gracefully: at most once an hour and three
  times a day, then `gave-up` and a person takes over. A network problem is
  reported, never restarted. Without `heal` the sync only reports.
- **Quiet agents.** `quiet_after` (`30m`, `3d`, `never`) is how long an agent
  may go without syncing. Within it the Agents tab shows the new `away` state,
  past it operators and the owner are emailed, and again when it is back.
  Beacon still has no timer: every pull checks the other agents.
- **`mailbox_ttl`**: how long the mailbox holds a message for an agent (a
  laptop may be off for days). The agent skill (1.5.1) says so.
- Health reports and their history: the sync sends `X-Beacon-Health` with each
  pull, every change is kept (`GET /api/agents/{id}/health-events`), and
  operators and owners are emailed when a gateway is restarted, when healing
  gives up, and when the agent answers again. The Policy tab shows each agent's
  effective health settings. `auto_heal` is registered as a T2 capability.

### Changed

- **New host sync.** Healing and health reports need it; an older sync keeps
  working and reports nothing.
- Defaults when no `health` entry matches: no healing, `quiet_after: 30m`,
  `mailbox_ttl: 72h`. An agent silent for 15 to 30 minutes now shows `away`
  instead of `offline`.

## [0.26.4] — 2026-10-31

### Added

- **Workflow requests notify.** Before, an agent that asked for an approval had
  to poll for the answer, and the person asked had to open the Workflows tab.
  Now an agent responder is told when it is asked, and the requester agent is
  told when its request is answered (a mailbox notice, with the state and any
  note). Failures are logged and never stop the request.
- **Email to people**, when a mail server is configured (`BEACON_SMTP_HOST`,
  `_PORT`, `_USER`, `_PASSWORD`, `_FROM`, `_SECURITY`; off by default). A
  `human` request emails every operator and the owners of the agents involved;
  a request addressed to an email goes to that person. The text is plain, marks
  the agent's words as the requester's, is cut at 800 characters, carries one
  link to Beacon's own page and no plan hash, and is limited to 10 per address
  per hour. The agent skill (1.5.0) says that an answer comes as a notice, so
  agents do not poll.

## [0.26.3] — 2026-10-31

### Fixed

- **A workflow request addressed to a name nobody has was accepted, and then
  nobody could see or answer it.** An agent asked for approvals with
  `eligible_responder` set to a first name. Beacon took any string, the request
  sat pending, and no operator saw it in the Workflows tab, because that shows
  requests for `human` or for your own login. Beacon now refuses a responder
  that is not `human`, an agent id, or the email of an operator or an agent's
  owner, and the error says what to use.

### Added

- **Ask a particular person.** A request can be addressed to the email of an
  operator or of an agent's owner. That person sees it and answers it (the
  case of the email does not matter). `human` still means any operator, plus the
  owners involved.
- **`agent_owner`** (board MCP): an agent looks up its own owner, or the owner
  of an agent it may call, and gets the value to use as `eligible_responder`.
  The agent skill (1.4.0) explains when to ask the owner and when `human`.

## [0.26.2] — 2026-10-29

### Fixed

- **The page no longer runs an old script next to a new page.** The static
  files carried an ETag but no `Cache-Control`, so after an upgrade a browser
  could keep an old `app.js` with the new `index.html`: the new host form
  showed its SSH config file field while the old script ignored it and the
  old error handler printed "[object Object]". Beacon now sends
  `Cache-Control: no-cache` (revalidate on each load, usually a 304) on the
  page, its scripts and API answers.

## [0.26.1] — 2026-10-29

### Fixed

- **The host form can set an SSH config file.** It had only a key path, so a
  host reached through Teleport tbot (which uses `/tbot-data/ssh_config`, not a
  key) could not be created or switched to SSH in the UI, and saving an existing
  one with the form dropped its config file and failed. The form now has the
  field, fills it in, and sends exactly one of key and config file.
- **Validation errors are readable.** The page showed "[object Object]" for any
  error the server sent as a list (every validation failure). It now says which
  field and why, for example `ssh: ssh needs exactly one of key or config_file,
  not both or neither`.

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
