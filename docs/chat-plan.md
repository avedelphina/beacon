# Talking with your agent through Beacon — technical plan

Status: decisions taken 2026-11-27 (§9), nothing built.

Audience: whoever builds it — a person or an agent with this repository. Each
phase is one PR (or a few) with its own tests and acceptance checks.

## 1. What it is

An agent's **owner** talks with that agent in the Beacon app: plain
conversations, nothing else. Both directions queue, the agent can write first,
history is kept 30 days, and conversations started elsewhere can be continued.

Not in it: model pickers, prompt libraries, file browsers, voice, attachments,
group chats, agent-to-agent chat, and the web page (later, same API).

**Chat is never an approval.** Nothing said in a conversation answers a workflow
request or satisfies a clearance.

## 2. How it works

```
app ──HTTPS──> Beacon <──HTTPS, the agent connects out── Hermes plugin "beacon"
                  ^
                  └── a2a_call "owner" (the agent writes first)
```

| Direction | Path | Hermes sees |
|---|---|---|
| Owner → agent | `beacon` platform plugin | its operator, on a chat platform |
| Replies, streaming | plugin | a normal chat |
| Scheduled job results, `hermes send` | plugin (home channel) | normal delivery |
| Agent writes first | `a2a_call` to the mailbox peer `owner` | a tool it already has |

The owner never writes over A2A, because Hermes marks every incoming A2A message
as untrusted input from a peer. A reply to a message the agent sent over A2A
goes through the plugin, with the original quoted.

Hermes deliberately gives a model no "send message" tool and asks plugins not to
add one. This plan adds none.

### Shown by a spike

A throwaway Hermes (checkout of 2026-11-23, v0.23.4), a plugin of about 100
lines and a stand-in server: a message queued while the agent was down arrived
at start; a real reply; a streamed reply (one send, 36 edits); listing another
conversation, reading it, and continuing it (the agent recalled what was said
there); a scheduled job delivered to the plugin; `hermes send`; `a2a_call` to a
peer named `owner`.

Learned: continuing a conversation **repoints** the chat and stays so, it also
**relabels** the original (its source becomes the plugin), and `a2a_call`
replaces e-mail addresses and secrets with placeholders.

Shown later (C0, same checkout): **copying** a conversation into a new thread.
A new child session with the transcript copied into it, without ending the
parent; the original kept its source, routing and messages.

C0, on Hermes **0.21.1** in a separate user on a real agent host: queue, real
reply, streaming, a command approval (`/deny` kept the file, `/approve` ran the
command), `/stop`, listing, copying with recall and an untouched original,
`hermes send`.

Not shown: copying a real messaging-platform conversation (the mechanics do not
depend on the source), a Docker agent.

### Hermes versions

Hosts run 0.21.x and 0.22.x; 0.23 changed how sessions are stored. The platform
plugin interface is the same on all. The session calls are not:

| | Hermes ≤ 0.22 | Hermes 0.23 |
|---|---|---|
| Store | `store._db_for_key(key)` (SQLite `SessionDB`) | `runner.persistence.aio` |
| List | `list_sessions_rich` | `sessions.list_sessions` |
| Copy | `create_session(..., model_config={"_branched_from": …})`, `replace_messages` | `NewSession(lineage_kind=BRANCH)`, `replace_transcript` |

`sessions.py` holds both and picks by what it finds.

## 3. Rules

- **Who.** The agent's `owner`, and nobody else: not operators, not the MCP
  bearer, not agent tokens, not clients. A member needs no extra grant for their
  own agent.
- **History belongs to the person.** A conversation is bound to the owner's
  email. When an agent's owner changes, the new one does not see it. (Operators
  can change owners and run the server, so they are not locked out in principle;
  the change is in `fleet_history`.)
- **Opt-in per agent:** `reach.chat`, changed by a person at CL4, like other
  changes to `reach`.
- **The app unlocks chat with Face ID.** A device token alone must not command
  an agent that has a terminal. A signed request (as for CL4) opens a chat
  grant for that device, 15 minutes from last use; sending and reading need it.
  A browser session needs none.
- **Slash commands** typed in the app: only `/stop`, `/new`, `/approve`,
  `/deny`. Beacon and the plugin both refuse the rest.
- **An agent reaches only its own owner.** The `owner` peer resolves to the
  caller's owner; there is no way to name another person.
- **Notifications** say "alpha sent you a message", never its words.

## 4. Beacon

### 4.1 Data (`backend/opstore.py`)

- `chat_threads`: `id, agent, owner, title, origin` (`app`, `agent`,
  `continued`, `home`), `source_session, created_at, updated_at, read_at`.
- `chat_messages`: `id, thread_id, direction, body, state, remote_id, reply_to,
  created_at, updated_at, final`.

`state` for outgoing: `queued → delivered`, or `expired`, `cancelled`, `failed`.
A streamed reply is one row whose text is replaced; `final` closes it. Bodies are
limited to 32 KB.

- A queued message expires after the agent's `mailbox_ttl` (default 72 h), and
  the owner can cancel it. The agent is told when it was written, so a late
  message is not taken for a fresh one.
- Messages older than **30 days** are deleted, and empty threads with them. There
  is no timer, so this runs on chat requests and agents' pulls.
- The database and its backups now hold conversations. Say so in the README.

### 4.2 Agent side

Authenticated by the agent's pull token, as `/mesh/{agent}`.

| Route | |
|---|---|
| `GET /mesh/{agent}/chat?after=` | long poll, up to 25 s: queued messages and requests (list, history, continue) |
| `POST /mesh/{agent}/chat` | a new message, an edit, a delivery receipt, or an answer to a request |

The long poll must be `async`: a blocking one would hold a worker thread per
agent.

### 4.3 Person side

`GET /api/chat/threads`, `POST /api/chat/threads`,
`GET /api/chat/threads/{id}?since=` (long poll), `POST …/messages`,
`DELETE …/messages/{id}` (cancel a queued one), `DELETE …/threads/{id}`,
`POST /api/chat/unlock` (signed), `GET /api/chat/agents/{agent}/sessions`.

Each goes into `members._ROUTES` with an ownership check, and the same check
applies to operators.

### 4.4 Writing first

`mesh.config` adds an outbound peer `owner` (`<beacon>/a2a/owner`) for agents
with an owner and `reach.chat`. The mailbox takes a message for `owner`, stores
it in a thread with origin `agent`, answers at once "delivered to your owner; a
reply, if any, arrives as a new message", and notifies. `owner` becomes a
reserved agent id. Limit per agent: 20 an hour.

This is a new path to a recipient, so it gets its own rule beside
`mesh.may_call`: caller has an owner and chat on.

### 4.5 Notifications

`notify` with the subject only; `about = {thread, agent}` so that a tap opens the
thread. No lock-screen actions and no lock-screen reply. No notification while
the thread is being read.

## 5. The plugin

`beacon` platform plugin, shipped from this repository
(`backend/drivers/hermes_chat_plugin/`), using only `httpx` and Hermes's
platform interface (`register_platform`, `connect`, `send`, `edit_message`,
`build_source`, `handle_message`).

- One app conversation is one Hermes chat id, so each has its own session. The
  home channel (scheduled results) is one fixed thread.
- Allowed user: the owner only.
- The host sync installs it, enables it and writes its settings, only for agents
  with `reach.chat`, records all of it in `managed.json`, and removes only that
  when chat is turned off. Installing restarts the gateway once.
- It reports its own version and Hermes's; the web page shows an agent whose
  plugin has not connected.

### The part that relies on Hermes internals

Listing, reading and continuing other conversations use the session store, which
is not a plugin interface. Keep all of it in one module (`sessions.py`) with a
self-check at start. If it fails, the plugin reports `continue: unavailable`, the
app says so, and chat keeps working.

## 6. Continuing other conversations

- **Which are listed.** Not all: an agent may hold conversations with other
  people (group chats, other users' direct messages). The plugin lists
  command-line, desktop and Beacon conversations, plus direct messages whose
  platform user id is on the owner's list in the agent's plugin settings. With no
  list, nothing from messaging platforms is shown.
- **How.** Continue a **copy**: branch the conversation into a new thread and
  leave the original alone, so that Telegram and Beacon never write into one
  session. Never switch a thread to the original: that relabels it. Hermes's own
  API fork ends the parent, so the plugin makes the copy itself (create a child
  session, copy the transcript, end nothing). Still to check on a live messaging
  conversation, and what its platform-specific system prompt does in the copy.
- Their history is shown only while the agent is online. Beacon stores none of
  it; what is said from then on is a Beacon thread like any other.

## 7. The app

A **Chats** tab: agents you own that have chat on, then threads, newest first,
unread marked. A thread: messages, a text field, a stop button while the agent
works, queued messages marked with a cancel, and when the agent was last
connected. Face ID on opening the tab.

Agent text is rendered as elsewhere in the app: never a tappable link. Nothing is
cached on the device; the screen is blank in the app switcher.

Android gets the same later (`docs/android-app-plan.md`).

## 8. Phases

Sizes: S ≈ half a day, M ≈ 1–2 days, L ≈ 3–5 days of focused work.

**C1 and C2 are built.** Seen end to end on Hermes 0.21.1 with the real sync and
plugin: queue while the gateway was down, streaming, approval and denial, stop,
`hermes send`, a scheduled job, removal. Found: an approval prompt also offers
`/approve session` and `always`, which Beacon refuses, so the app shows its own
Approve and Deny; Hermes sends its restart notices to the home conversation; an
agent's memory is shared across its conversations, as with any platform.

**C0 — Checks before building (done 2026-11-27, §2).** The Docker check moves to
C2, where the sync installs the plugin.

**C1 — Beacon (L).** §4.1–4.3, §4.5, retention, grant, clearances, members.
Acceptance (pytest): only the owner reads or writes, operators included in the
refusal; an app token without a grant is refused; queue, expiry, cancel, 30-day
deletion; owner change hides old threads; a notification holds no text.

**C2 — Plugin and sync (L).** §5. Acceptance: on a test agent, turn chat on,
sync, send from the API, get a streamed reply; turn it off and the host is as
before. Tests stub Hermes; the tested Hermes version is written down.

**C3 — App (L).** §7. Acceptance: on a phone, a message to a stopped agent is
queued, delivered when it starts, and the reply notifies.

**C4 — Agent writes first (S–M).** §4.4, a short section in the agent skill.
Acceptance: the agent's `a2a_call` to `owner` appears in the app and notifies; a
reply reaches the agent through the plugin with the original quoted.

**C5 — Continue conversations (M–L).** §6. Acceptance: only the allowed
conversations are listed; a continued one recalls its content; the original is
unchanged; with the self-check failing, chat still works.

C1 comes first; C2 needs it; C3 and C4 need C2; C5 is last and can be dropped
without affecting the rest.

## 9. Decisions taken (2026-11-27)

1. **Face ID grant for chat:** 15 minutes from last use.
2. **`/approve` from chat** is allowed, because an agent waiting for a command
   approval is otherwise stuck. It lets the phone approve a shell command after
   Face ID.
3. **Continue a copy**, not the original (§6).
4. **Who is "you" on messaging platforms:** a list of platform user ids per
   agent, entered once. Without it those conversations stay hidden.
5. **A queued message's life:** the same as the agent's mailbox (72 h by
   default).

## 10. Docs to update with the code

README (chat, routes, what the database now holds), `skills/agent-mesh`
(the `owner` peer, chat is not approval), AGENTS.md, ROADMAP.md, CHANGELOG, and
the status line here.
