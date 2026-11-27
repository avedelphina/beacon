# Beacon for Android — technical plan

Status: decisions taken 2026-11-27 (§8), nothing built.

Audience: whoever builds it — a person or an agent with this repository. Each
phase is one PR (or a few) with its own tests and acceptance checks.

Read [ios-app-plan.md](ios-app-plan.md) first. Login, device tokens, the
signature format, the clearance rules, the lock-screen rules and the shared
contract are the same here and are not repeated. This plan covers only what
Android changes.

## 1. Scope

The web page already installs as a PWA in Chrome and sends Web Push to Android.
The native app exists for the same three things as on iOS:

1. Approve or reject a request (CL3 and below) from the notification.
2. A fingerprint or face check before a CL4/CL5 answer, with proof Beacon can
   verify.
3. A per-device login that lasts 30 days since last use.

v1 has what the iOS app has in 0.41.0: requests, notifications, agents (status,
logs, health, restart), board, secrets, projects, device settings. Everything
else stays in the web page.

Build it after the iOS app has passed its device checklist
(`apps/apple/TESTFLIGHT.md`): a design fault found there is then fixed once.

## 2. Decisions this plan makes

| Topic | Decision | Why |
|---|---|---|
| Where | `apps/android/` | Decided in ios-app-plan.md §8.4 |
| Language, UI | Kotlin, Jetpack Compose, Material 3; one module, one activity | The platform's default; no layers to maintain |
| Minimum OS | Android 12 (API 31) | A notification action can require unlock only from API 31; per-use biometric keys are simple from API 30 |
| Dependencies | Compose, activity-compose, navigation-compose, androidx.browser, OkHttp, kotlinx.serialization, firebase-messaging. Nothing else | Android cannot do this with the platform alone as iOS did. OkHttp because `HttpURLConnection` has no `PATCH`, which the app uses |
| Supply chain | Version catalog plus Gradle `verification-metadata.xml` (checksums) | The same rule as the hashed pip lock files |
| Biometrics | The platform `BiometricPrompt` with a `CryptoObject`, class 3 only | No extra library at API 31 |
| Push | FCM HTTP v1, data-only messages; the app builds the notification | Data messages always reach the app's code, so actions and categories stay under its control |
| Zitadel | The **same** native client as iOS | Beacon accepts one `ZITADEL_APP_CLIENT_ID` as the ID token's audience |

## 3. Backend work

No change to login, tokens, signatures, clearances or `api` (stays 1: everything
below is an addition).

### 3.1 Push kinds

`app_devices` (migration in `backend/opstore.py`): rename `apns_token` to
`push_token`, add `push_kind` (`apns` or `fcm`), keep `apns_env` (null for FCM).
Set `push_kind = 'apns'` on rows that have a token.

Going back to the previous release adds an empty `apns_token` column again;
iPhones register at their next launch. Say so in the upgrade notes.

`set_apns` / `drop_apns` / `apns_targets` become `set_push` / `drop_push` /
`push_targets(people, kind)`.

New route `PUT /api/app/devices/self/fcm {token}`, beside the APNs one, which
stays as it is for installed iOS apps. App devices only (409 otherwise), listed
in `members._ROUTES`. Limit the token to 4096 characters of `[A-Za-z0-9_:-]`.

### 3.2 `backend/fcm.py`

Beside `apns.py`, with the same rules: only the subject, never an agent's words;
sends in a background thread; off unless configured.

- Config: one variable, `BEACON_FCM_KEY_FILE`, the Firebase service account
  JSON (it holds the project id, client email and private key). Put it in
  `./ssh` as `fcm-*.json`; add `ssh/fcm-*.json` to `.gitignore`. Mounted, never
  in the image.
- Auth: RS256 JWT (`pyjwt`, already installed) with scope
  `https://www.googleapis.com/auth/firebase.messaging`, exchanged at the file's
  `token_uri` for an access token; cache it, renew after 50 minutes or on 401.
- Send: `POST https://fcm.googleapis.com/v1/projects/<project>/messages:send`
  with `httpx`. **No new dependency.**
- A response with error code `UNREGISTERED` clears the device's token. Do not
  clear it on `INVALID_ARGUMENT`: a bad payload gives that too.

Message:

```json
{"message": {
  "token": "…",
  "android": {"priority": "HIGH", "ttl": "86400s"},
  "data": {"title": "alpha needs your answer", "category": "WF_APPROVE",
           "workflow": "17", "clearance": "3", "kind": "approval", "card": "4"}
}}
```

FCM data values are strings. `urgent: "true"` when the notice is urgent;
`agent` and `project` as for APNs. Priority `HIGH` when urgent or when there is
a category, else `NORMAL`. `category` is chosen by the same function APNs uses
(move it out of `apns.py` so both import it).

### 3.3 notify.py and texts

- `_tell`: `reachable` and the send cover both kinds, under the same hourly
  limit per person, so two phones do not double the room.
- Refusals say "Face ID or Touch ID". Make them platform-neutral ("confirm it
  in the app with your fingerprint or face") in `app.py` and `clearances.py`,
  and update the tests that match the text.
- `app_devices.py` docstring: "Secure Enclave" becomes "the device's secure
  hardware".

### 3.4 Contract and tests

- `tests/test_app_contract.py` also writes `apps/contract/fcm.json`.
- pytest with `httpx.MockTransport`: payload per kind and clearance, JWT claims
  and token reuse, `UNREGISTERED` clears, disabled without the file, the
  migration keeps an existing APNs token, a member may use the new route, a
  browser session may not.

## 4. The app

### 4.1 Layout

```
apps/android/
  settings.gradle.kts, build.gradle.kts, gradle/{libs.versions.toml,verification-metadata.xml,wrapper/}
  local.properties            gitignored (see below)
  app/src/main/
    AndroidManifest.xml
    res/xml/network_security_config.xml
    kotlin/…/beacon/
      MainActivity.kt   Session.kt
      api/        Client.kt, Models.kt
      auth/       Login.kt, DeviceKey.kt, TokenStore.kt, Signing.kt
      push/       MessagingService.kt, Notices.kt, AnswerReceiver.kt
      ui/         Requests.kt, Agents.kt, Board.kt, Secrets.kt, Projects.kt, Settings.kt, AgentText.kt
  app/src/test/kotlin/…       reads ../../contract/
```

The public repo holds only `org.example.beacon`. `local.properties` supplies the
real application id, the four Firebase client values (app id, API key, project
id, sender id) and the release keystore. Initialise Firebase from those values
in code, without the Google Services plugin or `google-services.json`, so that
CI and a fresh clone build with push simply off.

### 4.2 Login and keys

- OIDC with PKCE in a Custom Tab. Redirect
  `<applicationId>:/oauth/callback`, caught by an intent filter. Use the iOS
  bundle id as the application id and the Zitadel client needs no change;
  otherwise add a second redirect URI to the same client. Keep verifier and
  state in saved instance state: Android may kill the app behind the browser.
- Another app can register the same URI scheme. PKCE and the nonce bound to the
  device key make a captured code or token useless to it.
- Device key: Android Keystore, EC `secp256r1`, `SHA-256`, user authentication
  required for every use with `AUTH_BIOMETRIC_STRONG`,
  `setInvalidatedByBiometricEnrollment(true)`, StrongBox where present with a
  fallback to the TEE. `KeyPermanentlyInvalidatedException` → log in again.
- Public key: Beacon wants the 65-byte X9.63 point, which is the last 65 bytes
  of the certificate's encoded (SubjectPublicKeyInfo) key.
- Signature: `SHA256withECDSA` already gives the DER Beacon expects.
- Take the signing time **after** the biometric check succeeds, so a slow
  answer does not leave the 120 s window. Format it as plain decimal seconds
  (`Double.toString` gives `1.795E9`).
- Device token: encrypted with an AES-GCM Keystore key and stored in the app's
  private files. `allowBackup="false"` and data extraction rules that exclude
  everything.
- A phone without an enrolled class 3 biometric cannot log in; say why.

### 4.3 Notifications

- Channels: Urgent and Requests (high importance), Other (default). Breaking
  through Do Not Disturb is the person's choice in system settings.
- Ask for `POST_NOTIFICATIONS` (Android 13+) after login. Settings shows when it
  is off. Register the FCM token at each launch and on `onNewToken`.
- `WF_APPROVE`: Approve and Reject with `setAuthenticationRequired(true)`, plus
  Open. `WF_HUMAN` and `WF_OPEN`: open the app only. `setLocalOnly(true)`, so
  that a paired watch does not offer these actions.
- `AnswerReceiver` (not exported) does what `answerFromNotice` does on iOS: GET
  the request, refuse CL4+ or any kind but approval and review, refuse if the
  device has no secure lock, POST with the shown plan's revision and hash, post
  the result as a notification. `goAsync` with a short timeout; on failure say
  "Not answered".
- Actions that open the app use an activity `PendingIntent` directly (Android 12
  forbids starting one from a receiver), immutable, with a request code per
  workflow.
- An activity intent only navigates. Nothing is ever answered from one: other
  apps can send intents.

### 4.4 Security rules carried over from iOS

- Agent-written text is never tappable. Only `http`/`https` card sources and
  references appear, in a separate list that shows where each goes.
- No disk cache, no cookies. Nothing Beacon answers is kept on the device.
- Secrets: `FLAG_SECURE` on the screen; drop revealed and typed values when the
  app stops; copies carry `EXTRA_IS_SENSITIVE`. Android cannot keep the
  clipboard off other devices or reliably expire it, and the screen says so.
- Answer and reveal screens call `setHideOverlayWindows(true)` against
  tapjacking.
- Network security config: no cleartext; trust system **and user** certificate
  authorities, because Beacon on a private CA needs its root installed on the
  phone and apps ignore user roots by default.
- Only the activity is exported.

### 4.5 Tests and CI

JVM unit tests against `apps/contract/`: the PKCE vector, the signature vector
(same messages, signature verifies), every fixture decodes (unknown fields
ignored), every category in `fcm.json` is known and only `WF_APPROVE` answers.

Keystore, biometrics and push need a real device; they are on the checklist, not
in CI.

`.github/workflows/android.yml`: Ubuntu runner, only for `apps/android/` and
`apps/contract/`; `./gradlew testDebugUnitTest lintDebug assembleRelease`
(unsigned); actions pinned by commit; Gradle wrapper validated.

## 5. Distribution

| Way | Needs | Fits |
|---|---|---|
| `adb install` from a computer | Developer options on the phone | Development |
| Signed APK, sideloaded | Android developer verification (below) | A few own devices |
| Play **internal testing** | Play Console account (one-time fee), up to 100 testers | Members; automatic updates |

- Google's developer verification applies on certified devices in Brazil,
  Indonesia, Singapore and Thailand since September 2026, and is planned
  worldwide in 2027. Unregistered apps then install only through ADB or an
  "advanced flow"; a free limited account covers up to 20 devices. See
  [Google's announcement](https://android-developers.googleblog.com/2026/03/android-developer-verification-rolling-out-to-all-developers.html);
  check it again before choosing.
- The 12-testers-for-14-days rule for new personal accounts applies to
  [production access](https://support.google.com/googleplay/android-developer/answer/14151465),
  which this app does not need.
- Back up the release keystore with the other Beacon secrets. Without it,
  updates cannot be installed over the old app.

## 6. Phases

Sizes: S ≈ half a day, M ≈ 1–2 days, L ≈ 3–5 days of focused work.

**A0 — Prerequisites (the owner).** Firebase project with Cloud Messaging; an
Android app in it with the application id; the service account JSON on Beacon's
host; Beacon may reach `fcm.googleapis.com:443` and `oauth2.googleapis.com:443`;
Android Studio; a phone on Android 12+ with Google services and a fingerprint
or class 3 face unlock.

**A1 — Backend: FCM (M).** §3. Acceptance: tests pass; an existing iPhone still
gets notices after the migration; with real credentials a test notice reaches a
device (recorded in the PR).

**A2 — Skeleton and login (M).** Project, config, CI, Client, keys, login,
version check, log out. Acceptance: on a phone, log in; the device appears on
the web page; revoking it returns the app to login.

**A3 — Requests and answering (M).** List, detail, all answer kinds, signing.
Acceptance: a CL3 answer is audited under the person with `via = app`; a CL4
answer needs the biometric check and a cancelled one changes nothing; a member
sees only their own.

**A4 — Notifications (M).** §4.3. Acceptance: Approve from a locked phone asks
for unlock and reports the result; CL4 opens the app; an answer to a changed
plan is refused and says so; a tap opens the request from a closed app.

**A5 — Agents, board, secrets, projects (L).** Parity with iOS. Acceptance: the
agent, board, secret and project items of the iOS checklist.

**A6 — Release (S–M).** Icon, signing, the chosen distribution,
`apps/android/RELEASING.md` with the device checklist (adapted from iOS, plus:
Do Not Disturb, notification permission off and on again, a paired watch,
battery saver, a private CA).

A2 can start beside A1; A4 needs both.

## 7. Docs to update with the code

README (FCM setting, the new route, outbound hosts), `.env.example`, AGENTS.md
(`apps/android/`, `fcm.py`), ROADMAP.md, CHANGELOG with upgrade notes,
`.gitignore`, and the status line here.

Not in this plan: tablet two-pane layouts, widgets, Wear OS, phones without
Google services (UnifiedPush; they can use the app without notifications), iOS
changes.

## 8. Decisions taken (2026-11-27)

1. **Minimum Android 12.** Lower would mean lock-screen answers without a
   guaranteed unlock, or none.
2. **Distribution:** `adb` while developing; Play internal testing once members
   need it.
3. **Copying a revealed secret:** keep the button, with the limit in §4.4
   stated on the screen.
4. **Phones without Google services:** no push, and no UnifiedPush until someone
   needs it.
