# Running and releasing the iOS/iPadOS app

## Once

**Apple developer portal** (developer.apple.com, Certificates, IDs & Profiles)

1. Identifiers: add an App ID with your bundle id. Turn on **Push Notifications**
   and **Time Sensitive Notifications**. Xcode's automatic signing can create the
   ID, but it does not add these.
2. Keys: add a key with **Apple Push Notifications service**. The `.p8` file can
   be downloaded once. Note the key id and your team id.

**Beacon**

3. Run Beacon 0.41.0 or later.
4. In `.env`: `ZITADEL_APP_CLIENT_ID` and the four `BEACON_APNS_*` settings (see
   `.env.example`). Beacon must reach `api.push.apple.com:443`, and
   `api.sandbox.push.apple.com:443` for builds run from Xcode.
5. In Zitadel, in Beacon's project, add an application: type **Native**, PKCE,
   redirect URI `<bundle id, lowercased>:/oauth/callback`, user info inside the
   ID token. Its client id is `ZITADEL_APP_CLIENT_ID`.

**This machine and the devices**

6. `cp Config/Local.xcconfig.example Config/Local.xcconfig`, then set
   `DEVELOPMENT_TEAM` and `BEACON_BUNDLE_ID`. The file is gitignored.
7. If Beacon's certificate comes from a private CA, install the root on each
   device and turn on full trust (Settings, General, About, Certificate Trust
   Settings).

Run on your own phone: open `Beacon.xcodeproj`, choose the phone, Run. Such a
build uses Apple's **sandbox** push service; TestFlight builds use production.
The same `.p8` key serves both.

## TestFlight

Needed once: in App Store Connect, **New App** (iOS, your bundle id, any SKU).
The name must be unique in the whole App Store.

Each release: raise `CURRENT_PROJECT_VERSION` (and `MARKETING_VERSION` when it
is a new version) in `Config/Base.xcconfig`. Then in Xcode: Product, Archive,
Distribute App, TestFlight. Or from this folder:

```bash
xcodebuild archive -project Beacon.xcodeproj -scheme Beacon \
  -destination 'generic/platform=iOS' -archivePath build/Beacon.xcarchive \
  -allowProvisioningUpdates
xcodebuild -exportArchive -archivePath build/Beacon.xcarchive \
  -exportOptionsPlist ExportOptions.plist -exportPath build/export \
  -allowProvisioningUpdates
```

**Export compliance.** The app declares `ITSAppUsesNonExemptEncryption = NO`: it
uses only the system's cryptography (HTTPS, and signing and hashing to prove who
is logged in). This is your declaration to Apple; confirm that it holds.

**Testers.** Builds expire after 90 days.

- *Internal* testers are users of your App Store Connect team (up to 100). No
  review.
- *External* testers need only an email invitation, but Apple reviews the build,
  and the reviewer must be able to log in. A private Beacon gives them no way to,
  so expect a refusal unless you offer a demo Beacon and account. **Plan on
  internal testing** for members, or settle this before promising them the app.

## Check on a real device

None of this can be checked in the simulator or in CI.

- [ ] Log in: Face ID is asked, and the device appears under App devices on the
      web page.
- [ ] A CL3 review notification: **Approve** from the locked phone asks to
      unlock, then reports "Approved: …"; the audit shows you, `via` `app`.
- [ ] A CL4 request notification offers only **Review with Face ID…**; answering
      it asks for Face ID and succeeds.
- [ ] The same CL4 answer after cancelling Face ID changes nothing.
- [ ] A request its agent replaced after the notification: **Approve** on the old
      notification reports it is already withdrawn.
- [ ] A CL4 request changed by its agent while open on the phone: the answer is
      refused and the screen shows the new plan.
- [ ] A replacing request shows what the old one said; a reported one shows the
      report.
- [ ] A tap on a notification opens that request, from a closed app too.
- [ ] A member account sees only its agents, cards and requests.
- [ ] Revoke the device on the web page: the app returns to login.
- [ ] Add a face or finger in Settings: a CL4 answer is not sent and says to log in again.
- [ ] Turn Beacon's notifications off in the system Settings: the app's Settings says Off; turn them on
      again and a notice arrives without logging in again.
- [ ] Log out: no further notices arrive on that phone.
- [ ] Restart an agent: Beacon's "Would restart…" text shows first.
- [ ] A secret an agent offers: the notification opens its secrets; Reveal asks for Face ID once, shows
      the value, and the host's file is gone.
- [ ] Give an agent a secret, multi-line too: Face ID, then the file is in `beacon-secrets/inbox/`.
- [ ] With a value shown, the app switcher shows a blank card; after returning from another app it is gone.
- [ ] Pause a project from the phone: its agents get the notice; resume it. A "project … is paused"
      notification opens that project.
- [ ] Chats: opening the tab asks for Face ID once; a message to a stopped agent shows "waiting for the
      agent", arrives when it starts, and the answer notifies; the tap opens that conversation.
- [ ] A streamed answer grows on screen; Stop ends a long task; a dangerous command shows Approve and
      Deny and each does what it says.
- [ ] After 15 minutes unused, chat asks for Face ID again. The app switcher shows nothing of it.
- [ ] iPhone: five tabs, no "More"; the gear on Requests opens Settings and closes again.
- [ ] iPad: both orientations, Split View, a hardware keyboard (⌘R, ⌘↩).
- [ ] Dark mode, the largest Dynamic Type size, and VoiceOver on the answer
      buttons and the availability labels.
