# Beacon for iOS and iPadOS — technical plan

Status: decisions taken 2026-11-26 (§8); P1 built on `feat/app-login`. Implements step 2 of the App track in
[ROADMAP.md](../ROADMAP.md), iOS and iPadOS first; macOS later from the same code.

Audience: whoever builds it — a person or an agent with this repository. Each
phase below is one PR (or a few), with its own tests and acceptance checks, so
the work can stop or be handed over between phases.

## 1. What the app is for

The PWA (0.29.0) already shows the whole page on a phone and sends Web Push.
The native app exists for what Safari cannot do:

1. **Answer from the notification.** Approve or reject a workflow request from
   the lock screen or banner, without opening a page and logging in.
2. **Face ID before a CL4/CL5 answer**, and proof of it that Beacon itself can
   check — not only a local prompt.
3. **A login that lasts** on a personal device (the browser session is 8 h), and
   that an operator can revoke per device.

So v1 is deliberately narrow: **requests inbox + notifications + a read-mostly
fleet view.** Everything else stays in the web page (policy, enrollment,
decommission, templates, hosts, cron, clients). The backend changes are generic,
so moving more into the app later is UI work only.

## 2. Decisions this plan makes

| Topic | Decision | Why |
|---|---|---|
| UI | SwiftUI, one multiplatform target (iOS destinations only for now) | iPhone and iPad from one code base; macOS later by adding a destination |
| Minimum OS | iOS/iPadOS 18 | `@Observable`, mature `NavigationSplitView`; one major back from current |
| Dependencies | **None.** URLSession, AuthenticationServices, CryptoKit, LocalAuthentication, UserNotifications, Security | No SPM graph to audit or update |
| Where the code lives | `apps/<platform>/` in this repository: `apps/apple/` now (iOS, iPadOS, later macOS), other platforms beside it later (Android, Windows, Linux desktop) | The API contract and every app change in the same PR; one CI |
| Shared contract | `apps/contract/`: JSON fixtures and test vectors, generated or checked by pytest, read by every app's tests | One source of truth for all clients, not per platform |
| Login | Zitadel **native** OIDC client (PKCE via `ASWebAuthenticationSession`); the app gives the ID token to Beacon once; Beacon issues its **own device token** | See §3.1. Changes the ROADMAP wording "Beacon accepting Zitadel access tokens" |
| Human proof | A Secure Enclave P-256 key per device, usable only after Face ID (`.biometryCurrentSet`); Beacon verifies its signature on every CL4/CL5 answer or action | The bearer token alone never counts as a human for CL4/CL5 |
| Push | APNs, token auth (`.p8`), HTTP/2 from Beacon | Notification actions need APNs; Web Push stays for browsers |
| Distribution | TestFlight, internal testers | No App Store review, no demo server needed |

## 3. Backend work

### 3.1 App login and device tokens

Why not accept Zitadel access tokens as `Bearer` (the ROADMAP's first idea):
Zitadel's access tokens are opaque by default and carry no email; validating
them means introspection or userinfo calls per request, a second token format,
and refresh handling in the app. Revocation would be Zitadel-wide, not per
device. A Beacon-issued token, minted once from a verified ID token, keeps
Beacon's existing rule — *the role is read from the email on every request* —
and adds a device list that the person and the operators can see and revoke.

**Flow**

```
app                              Zitadel                         Beacon
 |-- GET /auth/app/config ------------------------------------------>|  {issuer, client_id, api}
 | create SE key K (Face ID), nonce = b64url(sha256(K.pub_x963))
 |-- ASWebAuthenticationSession: /oauth/v2/authorize
 |     client_id=<native>, PKCE S256, scope=openid email profile,
 |     nonce=<nonce>, redirect=<bundle-id>:/oauth/callback ---->|
 |<--------------------------------------- code ----------------|
 |-- POST /oauth/v2/token (code + verifier, public client) ---->|
 |<--------------------------------------- id_token ------------|
 |-- POST /auth/app/login {id_token, public_key, name, signature} ------>|
 |                                    verify id_token (JWKS, iss, aud=native client,
 |                                    exp, iat < 5 min, nonce == sha256(public_key)),
 |                                    verify signature by K over "login\n"+sha256(id_token),
 |                                    admit(claims) as for the browser
 |<------------------------------- {token, device_id, expires_at} ------|
```

- The nonce binds the ID token to the device key, and the signature proves the
  app holds that key. A leaked ID token cannot register another device.
- `admit(claims)` is the same function the browser callback uses, so members
  need `email_verified` as they do today.

**Storage** — new opstore table `app_devices` (migration in `backend/opstore.py`):

| column | |
|---|---|
| `id` | text, random |
| `email` | casefolded |
| `name` | "Alex's iPhone", from the app |
| `token_hash` | sha256 of the token; the token itself is never stored |
| `public_key` | 65-byte X9.63 P-256 point, base64 |
| `last_signed_at` | float; replay guard, see §3.2 |
| `created_at`, `last_seen_at` | expiry is `last_seen_at` + 30 days; logout and revoke delete the row |
| `apns_token`, `apns_env` | added in P2: null until the app registers; `sandbox` or `production` |

- Token: `bk_app_` + 32 random bytes, url-safe. Expiry 30 days after last use
  (sliding; `last_seen_at` is written at most once a minute). Decided in §8.
- At most 10 devices per person; an 11th login replaces the least recently used.

**New module** `backend/app_devices.py` (login, resolve, revoke, list, verify a
signature). Keep it beside `auth.py` rather than inside it: `auth.py` is about
the browser.

**Routes**

| Route | Who | |
|---|---|---|
| `GET /auth/app/config` | anyone | `{issuer, client_id, api}`; 404 when `ZITADEL_APP_CLIENT_ID` is unset |
| `POST /auth/app/login` | anyone | the flow above |
| `GET /api/app/devices` | person | their own devices; an operator gets everyone's |
| `DELETE /api/app/devices/{id}` | person | their own (logout = delete self); an operator may revoke any |
| `PUT /api/app/devices/self/apns` | app | P2. `{token, env}`; the app calls it at each launch |

Add the three `/api/app/devices…` routes to `members._ROUTES` (own devices only).

**Config** — one new env var, `ZITADEL_APP_CLIENT_ID` (the native app in the
same Zitadel project). Document it in `.env.example` and the README.

**Middleware** — in `AuthMiddleware`, after the MCP and agent-token checks and
before the session check: a `Bearer bk_app_…` resolves through
`app_devices.resolve()`; on success set `request.state.app_device` and
`request.state.user = {sub, email, name}`, then apply exactly the same
operator/member logic as for a session (role from `role_for_email` each time,
`members.check` for members). An unknown, expired or revoked token is a 401
(not a redirect). CSRF does not apply: a bearer header is not ambient, and the
CSRF middleware already only acts on session users.

**One user accessor.** Eleven places read `request.session.get("user")`
directly (`app.py`: `_record_actor`, `_workflow_human`, `_workflow_view_actor`,
`_workflow_response_actor`, `_board_author`, `_push_person`, …). Replace them
all with `auth.current_user(request)` that returns the session user or
`request.state.user`. This is the only refactor in the plan, and without it the
app's answers would be recorded as `beacon`.

- `_record_actor`: `audit.VIA` becomes `"app"` for device tokens, so
  `host_actions` and `fleet_history` show the path.
- `_workflow_human`: true for a session user **or** an app device.
- `_push_person`: accepts an app device (it already refuses MCP).
- Error texts that say "only a person in the web page" become "only a person
  (web page or app)".

### 3.2 Proof of Face ID for CL4/CL5

For a CL4/CL5 request (answering a workflow request of clearance ≥ 4, or a
CL4/CL5 action through `_require_human_session`), an app device must send:

```
X-Beacon-Signed-At: <unix seconds, float>
X-Beacon-Signature: <base64 DER ECDSA P-256/SHA-256 by the device key>
```

over the canonical string

```
"beacon-app-v1\n" + METHOD + "\n" + PATH + "\n" + signed_at + "\n" + hex(sha256(body))
```

Beacon checks: the key is the device's; `signed_at` is within 120 s of now and
greater than the device's `last_signed_at` (then stores it — no nonce table
needed); the signature verifies (`cryptography`, already installed).

- Verification runs in one async dependency (it needs `await request.body()`)
  that sets `request.state.app_verified`. Only requests with the header pay for
  it.
- `_require_human_session`: for clearance ≥ 4, an app device without
  `app_verified` → 403 `"confirm with Face ID in the app"`.
- `_respond_workflow`: read the request's clearance first; same rule. CL ≤ 3
  answers from the app need only the bearer token (this is what lets a
  lock-screen action work, see §4.4).
- The Secure Enclave key uses `.biometryCurrentSet`: if someone adds a face or
  fingerprint, the key is gone and the app must log in again. That is intended.

**Shared test vector** — `apps/contract/signature-v1.json`
(a key, a request, the canonical string, a signature). `pytest` verifies it with
the Python code, and XCTest builds the same canonical string. Either side
changing the format breaks a test.

### 3.3 APNs sender

New module `backend/apns.py`, next to `push.py`, same rules:

- **Only the subject**, never an agent's words (same lock-screen reasoning as
  `push.py`).
- Token auth: ES256 JWT (`pyjwt`, already installed) with key id + team id,
  cached and renewed every 40 min (Apple refuses renewals more often than 20 min
  and tokens older than 60 min).
- `httpx.Client(http2=True)` to `api.push.apple.com` or
  `api.sandbox.push.apple.com` by the device's `apns_env`. **New dependency
  `h2`** (pin in `requirements.txt`, regenerate `requirements.lock` with
  `pip-compile --generate-hashes`).
- `410 Unregistered` or `400 BadDeviceToken` → clear that device's `apns_token`.
- Sends in a background thread, as `push.send` does.

Config (all four or APNs is off; the app still works, without notifications):

```
BEACON_APNS_KEY_FILE   path to the .p8, mounted read-only like the ssh key
BEACON_APNS_KEY_ID
BEACON_APNS_TEAM_ID
BEACON_APNS_TOPIC      the app's bundle id
```

The `.p8` is key material: gitignored path, mounted, never in the image.

**Payload**

```json
{
  "aps": {
    "alert": {"title": "alpha needs your answer"},
    "sound": "default",
    "thread-id": "card-42",
    "category": "WF_APPROVE",
    "interruption-level": "time-sensitive"
  },
  "beacon": {"workflow": 17, "clearance": 3}
}
```

| category | when | actions on the notification |
|---|---|---|
| `WF_APPROVE` | `approval` or `review`, clearance ≤ 3 | Approve, Reject (both `.authenticationRequired`, run in background), Open |
| `WF_HUMAN` | any request with clearance ≥ 4 | Review… (`.foreground` → app opens on the request, Face ID) |
| `WF_OPEN` | `decision`, `question`, `handoff` | Open |
| none | health, quiet, stuck cards | tap opens the app |

`interruption-level: time-sensitive` and `apns-priority: 10` for `WF_HUMAN`
and for any notice sent with `urgent=True`; everything else `active` with
priority 5. Time-sensitive needs the Time Sensitive capability on the App ID.
It can break through Focus and the notification summary where the person
allows that, but it does not sound on a muted phone.

**notify.py integration** — builds on the `urgent` flag that the
working-hours branch (`feat/working-hours`, in progress on 2026-11-26) adds to
`_tell` and `push.send`; rebase P2 on it. The signature becomes
`_tell(recipients, subject, body, urgent=False, about=None)`:
`about` is `{"workflow": id, "clearance": n, "kind": k, "card": c}` from
`workflow_created`, `workflow_escalated`, `client_needs_person` and
`workflow_waiting_on_owner`; other notices pass nothing. `_tell` calls
`apns.send(people, subject, urgent=urgent, about=about)` beside `push.send`,
counted under the same hourly limit key per person (`"push " + email`, or
`"urgent push " + email`), so one person is not notified twice as often for
having both. Duplicate notices for someone who also has the
PWA subscribed on the same phone: they turn one off; no dedup logic (a
per-person rule would also silence their Mac's Safari).

### 3.4 API contract

- `GET /api/version` → `{"version": "0.4x.y", "api": 1}`. `api` goes up only
  when a change breaks the app (as the tier → clearance rename would have). The
  app refuses to run against an `api` it does not know, with "Update the app" or
  "Update Beacon".
- Add `api` to the CHANGELOG convention in AGENTS.md: a breaking change for
  clients bumps it.
- The app decodes leniently: unknown fields are ignored, new optional fields
  are optional in Swift. Fixture JSON for the app's decoding tests is produced
  by a pytest (`tests/test_app_contract.py` writes the shapes of
  `/api/workflows`, `/api/workflows/{id}`, `/api/agents`, `/api/availability`
  into `apps/contract/` and fails if they changed without the
  fixtures being committed).

### 3.5 Web page

A "Devices" section under the user menu: name, created, last used, revoke.
Operators see all people's devices. Small: one table, the same `fetch` helpers.

### 3.6 Backend tests (pytest, existing fixtures)

- Login: good token; wrong `aud`; old `iat`; nonce not matching the key; bad
  signature; email not admitted; member without `email_verified`. Stub JWKS by
  patching `auth._jwks` (or a small `verify_id_token` seam).
- Device token: resolves to operator/member; role re-read (remove owner → 401);
  revoked and expired → 401; member blocked from an operator route; no CSRF
  check on app requests.
- Actor: an app answer is recorded with the person's email, `via = app`.
- CL4/CL5: no signature → 403; good → OK; replay with same `signed_at` → 403;
  stale → 403; wrong body hash → 403. CL3 answer with bearer only → OK.
- MCP and agent tokens still refused everywhere they are refused today
  (regression).
- APNs: payload/category per request kind and clearance; JWT reuse; 410 clears
  the token; disabled when env is incomplete. Use `httpx.MockTransport`; no
  network, no `h2` needed in the test.
- Shared signature vector.

## 4. The app

### 4.1 Layout

```
apps/
  contract/                   shared by every app: API shape fixtures, signature-v1.json
  apple/
    Beacon.xcodeproj            Xcode 16 folder references, so the pbxproj rarely changes
    Config/
      Base.xcconfig             committed; PRODUCT_BUNDLE_IDENTIFIER = org.example.beacon
      Local.xcconfig            gitignored: DEVELOPMENT_TEAM, real bundle id
    Beacon/
      BeaconApp.swift           @main, AppDelegate for APNs registration
      Beacon.entitlements       aps-environment, keychain group
      API/
        Client.swift            URLSession, bearer, signing, error mapping (401 → login, 409/403 text shown)
        Models.swift            Codable: WorkflowRequest, Agent, Availability, Card, …
      Auth/
        Login.swift             config fetch, PKCE, ASWebAuthenticationSession, token exchange
        DeviceKey.swift         SecureEnclave.P256.Signing key, Face ID access control
        Keychain.swift          token + key blob, kSecAttrAccessibleWhenUnlockedThisDeviceOnly
      Push/
        Notifications.swift     categories, action handler, deep link to a request
      Views/
        RootView.swift          NavigationSplitView: Requests, Agents, Board, Settings
        Requests/…  Agents/…  Board/…  Settings/…
    BeaconTests/                reads ../../contract/ as test resources
      PKCETests.swift           RFC 7636 Appendix B vector
      SignatureTests.swift      canonical string = vector
      DecodingTests.swift       fixtures decode
```

- The public repo holds only `org.example.beacon`; the real bundle id and team
  live in `Local.xcconfig`. No server URL is built in: the person types it on
  first launch.
- One `@Observable` `Session` object (server URL, device token, current user,
  role) in the environment; views call `Client` directly. No view-model layer,
  no repository layer.

### 4.2 Screens (v1)

| Screen | API | Notes |
|---|---|---|
| Server + login | `/auth/app/config`, `/auth/app/login`, `/api/version` | URL field, "Log in" |
| **Requests** (default) | `GET /api/workflows?state=pending` | waiting on me first, then others I can see; urgent ones (`urgent_reason` set) at the top of each group, marked URGENT; pull to refresh; badge count |
| Request detail | `GET /api/workflows/{id}` | title, clearance, kind, URGENT with `urgent_reason`, expiry (by default two working days), summary, card context and links (0.39.2), consulted, options; actions by kind: Approve / Reject / Request changes / choose option / answer text; "Send to a person"; plan_revision/plan_hash sent from what was shown; Face ID sheet for CL ≥ 4 |
| Agents | `GET /api/agents`, `/api/availability` | availability pill, tags, owner; search |
| Agent detail | `/status`, `/health-events`, `/logs` | Restart: show the `would_run` text, then confirm (`confirm=true`) |
| Board | `GET /api/board`, card detail, comments | v1: read, comment, change state |
| Settings | `/api/app/devices` | this device, other devices, log out, notifications state, Beacon version |

iPad: the same views in a three-column split (sidebar, list, detail); iPhone
collapses to a stack. Keyboard: ⌘R refresh, ⌘↩ approve on the open request.

A 409 `would_run` and a 403 with text are shown as they come: the server's text
is the message, as in the web page.

### 4.3 TLS

Beacon on a private CA (step-ca) needs that root installed on the device as a
profile, with full trust turned on (Settings → General → About → Certificate
Trust Settings) — the same as the PWA needs today. The app adds no pinning or
custom trust in v1; add a "trust this CA" option only if someone needs the app
without trusting the CA device-wide.

### 4.4 Notification actions

- Register categories at launch; request authorization (alert, sound, badge).
- `didRegisterForRemoteNotificationsWithDeviceToken` → `PUT /api/app/devices/self/apns`
  with `env = sandbox` for debug builds, `production` for TestFlight.
- Approve/Reject on `WF_APPROVE`: the device must be unlocked
  (`.authenticationRequired`); the handler runs in the background, GETs the
  request (for the current plan binding), POSTs the answer, then posts a local
  notification with the result ("Approved" or the server's refusal text). If
  the plan changed since the notice, the server marks it stale — that is the
  intended safety, and the result notice says so.
- `WF_HUMAN` always opens the app on the request; the answer is signed after
  Face ID. A background action cannot show Face ID, which is why CL4/CL5 have no
  lock-screen button.

### 4.5 App tests

Unit tests only (above). UI checked by hand on a device per phase; screenshots
of iPhone and iPad in the PR.

## 5. CI

A separate `.github/workflows/apple.yml`, so macOS runner minutes are spent
only when the app or its contract changes:

```yaml
on:
  push: {paths: ['apps/apple/**', 'apps/contract/**', 'tests/test_app_contract.py']}
  pull_request: {paths: ['apps/apple/**', 'apps/contract/**', 'tests/test_app_contract.py']}
jobs:
  test:
    runs-on: macos-15
    steps:
      - uses: actions/checkout@v4
      - run: >
          xcodebuild test -project apps/apple/Beacon.xcodeproj -scheme Beacon
          -destination 'platform=iOS Simulator,name=iPhone 16'
          CODE_SIGNING_ALLOWED=NO
```

No signing, no upload in CI; TestFlight builds are archived from Xcode by hand
until that hurts.

## 6. Phases

Sizes: S ≈ half a day, M ≈ 1–2 days, L ≈ 3–5 days of focused work.

**P0 — Prerequisites (the owner, not code).**
1. Apple Developer Program membership (individual is enough for internal
   TestFlight).
2. App ID with Push Notifications and Time Sensitive Notifications; APNs key
   (`.p8`, downloaded once — keep it with the other Beacon secrets).
3. App Store Connect app record, for TestFlight.
4. Zitadel: a **Native** application in Beacon's project, auth method PKCE,
   redirect `<bundle-id>:/oauth/callback`, ID token includes email
   ("User Info inside ID Token"). Its client id → `ZITADEL_APP_CLIENT_ID`.
5. Device: step-ca root trusted; Beacon's host may reach `api.push.apple.com:443`.
6. Choose internal or external TestFlight per member (§8.3).

**P1 — Backend: app login (M–L).** §3.1, §3.2, §3.5, the user accessor
refactor, tests. Acceptance: with a stub JWKS, an app token reads
`/api/workflows`, answers a CL3 request recorded under the person's email with
`via = app`, is refused a CL4 answer without a signature and accepted with one;
revoking the device ends access on the next request. Release as a minor version.

**P2 — Backend: APNs + contract (M).** §3.3, §3.4, `h2` + lock files.
Acceptance: unit tests; with real credentials, a test notice reaches a device
(manual, recorded in the PR).

**P3 — App: skeleton, login, requests (L).** Project, xcconfig, Keychain, device
key, login flow, Client with signing, Requests list + detail + answering,
version check, CI job. Acceptance: on a device against a real Beacon, log in,
answer a CL3 request, answer a CL4 request with Face ID; the audit row shows the
person and `app`.

**P4 — App: notifications (M).** Registration, categories, background
Approve/Reject, deep links, badge. Acceptance: lock-screen Approve on a CL3
request works with the phone locked; a CL4 notice opens the app on that request;
a changed plan gives the "stale" result notice.

**P5 — App: fleet and board (M).** Agents, agent detail, restart with confirm,
logs, health events; Board read/comment/state. Acceptance: a member sees only
their agents and cards; restart shows the `would_run` text first.

**P6 — iPad and TestFlight (S–M).** Split view polish, keyboard shortcuts,
icons, first TestFlight build, a device checklist (both orientations, Split
View, dark mode, Dynamic Type, VoiceOver labels on the action buttons).

P1 and P3's login/skeleton can run in parallel once the endpoint shapes in §3.1
are fixed; P3 needs P1 merged for anything past login.

**Later, not in this plan:** macOS destination; CL4/CL5 direct actions in the
app (policy save, decommission — the backend already accepts a signed request);
widgets; a "trust this CA" option; Xcode Cloud or fastlane; Critical Alerts, so
that an urgent request sounds on a muted phone (needs an entitlement Apple grants
on application, which it may refuse; apply only if time-sensitive proves too
quiet).

## 7. Docs to update with the code

- ROADMAP.md App track: replace "Beacon accepting Zitadel access tokens as
  Bearer" with the device-token design; mark steps as they land.
- README: app section (config vars, device tokens, APNs), API reference for the
  new routes and `api` in `/api/version`.
- AGENTS.md: `apps/` (per platform) and `apps/contract/` in code organization, `app_devices.py`/`apns.py`, the
  "bump `api` on a client-breaking change" convention, the
  "an app device is a human only with a valid signature at CL4/CL5" rule under
  Security considerations.
- CHANGELOG per release.
- `members.py` docstring: devices are on the member's list.

## 8. Decisions taken (2026-11-26)

1. **Device token lifetime: 30 days since last use**, no absolute limit. Role
   and revocation are checked on every request.
2. **CL ≤ 3 answers from the lock screen** with device unlock only. CL4/CL5
   always open the app and need Face ID.
3. **Members get the app too.** They answer requests about their own agents,
   CL4 included, with the same Face ID rule. Consequence for P0 and P6: each
   member becomes a TestFlight tester. Internal testing needs them as App Store
   Connect users on the team (up to 100); external testing needs Apple's beta
   review per build but only an email invite. Pick per person; family members
   who should not see App Store Connect go external.
4. **Apps stay in this repository**, under `apps/<platform>/`, with the shared
   contract in `apps/contract/`. Apps for all platforms, desktops included, will
   follow: `apps/apple/` serves iOS and iPadOS now and macOS later (one SwiftUI
   multiplatform target); Android, Windows and Linux desktop get their own
   directories beside it when they start. Everything on the server side is
   platform-neutral already: device tokens, the signature format (any platform
   with a hardware-backed P-256 key and a biometric gate — Android Keystore/
   StrongBox, Windows Hello/TPM — can produce it), the `api` number and the
   contract fixtures. Only the push sender is per platform: APNs now; FCM for
   Android later (the `app_devices` row then needs a `push_kind` column instead
   of `apns_*` — rename when the second kind arrives, not before). Desktop apps
   without a platform push service can rely on Web Push or no push.
