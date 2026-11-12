# appshelf

[Русский](README.md)

A self-hosted shelf for your iPhone apps. IPAs from your Apple ID purchase history are downloaded to
your Linux server and installed on iPhone and iPad straight from Safari via the system `itms-services`
link: no computer, cable or iMazing. The main use case is apps removed from the App Store (Russian banks,
VK and others): Apple no longer lists them, but anyone who has them in their purchases can still download them.

> appshelf only works with your own purchases and does not strip DRM: the IPAs are tied to your Apple ID
> and run on devices signed in with the same Apple ID under "Media & Purchases". Not affiliated with Apple.

The web UI is in Russian.

## Features

- You sign in to the panel with your Apple ID and its password (plus a 2FA code); the password is never stored.
  Several people can use it, and one browser can hold several Apple IDs at once: each Apple ID gets its own shelf;
  new Apple IDs join by the owner's invite link or from the panel of a person who is already signed in.
- Purchase history: you publish only what you pick. Apps removed from the App Store are added by an
  `apps.apple.com/…/id…` link or ID.
- A catalog of ~480 apps removed from the App Store (banks and their clones, VK, Mail.ru and more):
  appshelf checks which of them your Apple ID holds a license for.
- iPhone catalog with an "Install" button (from a computer — a QR code next to it); the current and the previous
  version are kept, and one of them can be pinned to survive later updates. iOS does not install over an installed
  app — offload it first (see "Updating an installed app").
- Nightly check for new versions (versions downloaded this week are marked in the catalog: apps removed from the
  App Store get updates only from here) and "Проверить обновления всех" (check all for updates) above the catalog;
  mail — see "Mail", including a weekly digest of new versions for every person.
- PWA: the shelf on the Home Screen.
- Search saved apps by name or bundle/package ID; filter recent updates, errors and items awaiting a decision (above
  the catalog, non-zero counters "new this week · errors · queued" link to those filters). Parameters stay in the
  URL, with an additional Android source filter. Shared accounts/people pages retain the selected platform; theme
  selection is available on sign-in and guest pages too.
- "Справка" (Help, `/help`, linked from Settings): installing, updating, adding apps; "?" on a card opens "How to
  install".
- Android: a section with each person's Android shelf — APKs from RuStore, Google Play, AppGallery, F-Droid, APKPure,
  GitHub, Codeberg and GitLab releases, from a direct link on a bank's site or an uploaded file; install and update
  from the phone's browser, nightly check for new versions, "What's new" per version, a signing rule (see "Android").
- A public Android shelf: the owner fills it, a guest opens it with a 6-digit PIN and can only install; brute-force
  protection.
- The Android app's “⋯” menu downloads an APK directly from its current source and opens the app's page.
  Selecting a 4PDA attachment also saves its topic; the browser may require forum sign-in.
  The source build may differ from the version selected on the shelf. Google Play provides only a store page.

## How it works

    iPhone (Safari) ─► Apache (HTTPS), vhost apps.example.com
                         ├─ /        → unix:/run/appshelf/web.sock (appshelf-web: Apple ID sign-in at /login)
                         ├─ /join/, /l/ → no sign-in (invite, fallback link)
                         ├─ /p, /apk/ → no sign-in (public shelf guest by PIN, permanent APK link)
                         ├─ /privacy, /google/*, /theme → no sign-in (policy, Google sign-in, theme)
                         ├─ /healthz → no password (monitoring)
                         ├─ /pwa/    → no password (PWA manifest, icon)
                         └─ /d/      → <archive>/ no password (manifests, IPAs, APKs, icons; path holds a secret token)
    appshelf-web (job worker) ─┐
    appshelf-nightly (04:30)   ┴─► bin/ipatool (HOME=/etc/appshelf/accounts/<id>, flock locks/<id>.lock) ─► Apple
                               └─► android/net.py (https and global addresses only) ─► RuStore, Google Play,
                                   AppGallery, F-Droid, APKPure, GitHub, Codeberg, GitLab, websites (APK)
    appshelf-backup (05:30) ─► database snapshot into APPSHELF_BACKUP_DIR

IPAs are fetched by `bin/ipatool`, a fork of [ipatool-cpp](https://github.com/Sorvigolova/ipatool) with our
patches (`ipatool/`): the password is never stored and is passed only via stdin, a `list-purchases`
command, and exit codes for the wrapper. It hands the Apple sign-in to `bin/ipatool-auth`, a client built from a
pinned [ipatool](https://github.com/majd/ipatool) (Go) with our adapter (`ipatool/auth/`, see "Sign-in and
people"). The web part is FastAPI and Jinja2, data lives in SQLite. Design
document (Russian): [docs/superpowers/specs/2026-10-05-appshelf-design.md](docs/superpowers/specs/2026-10-05-appshelf-design.md).
Sign-in and multiple Apple IDs: [docs/superpowers/specs/2026-10-08-multi-apple-id-design.md](docs/superpowers/specs/2026-10-08-multi-apple-id-design.md).

### What and where

| What | Where |
|---|---|
| Code, `.venv`, `bin/ipatool`, `bin/ipatool-auth`, `bin/ipatool-auth-cache` | `/var/_sh/appshelf`, owned by root: appshelf only reads and runs it (hard-coded in `deploy/*.service` and the `deploy/` scripts) |
| Data | `/var/lib/appshelf`: `appshelf.db`, `tmp/`, `locks/`, `status/nightly.stamp` and `status/backup.stamp` (monitoring), `cookie-key`, `uploads/` (chunks of uploaded APKs, see "Android"), `cache/fdroid/` (verified F-Droid catalogs) |
| IPA archive | `<archive>/<shelf token>/`: each Apple ID has its own random token (`accounts.shelf` in the database; the Apple ID carried over from the first version uses `<archive>/<PUB_TOKEN>/`); `<archive>` is `APPSHELF_PUB` or `/var/lib/appshelf/pub`. May be a network share — then `APPSHELF_PUB` is a subdirectory inside it, not the mount point itself (an empty mount point is a directory too, and IPAs would land on the local disk). The database and `tmp/` stay local (SQLite on CIFS gets corrupted) |
| Settings | `/etc/appshelf/appshelf.env` (sample: `deploy/appshelf.env.example`), App Store tokens in `/etc/appshelf/accounts/<id>/.ipatool/`, Google Play in `/etc/appshelf/google/<id>/`, 4PDA sessions in `/etc/appshelf/fourpda/` |
| Services | `appshelf-web.service`, `appshelf-nightly.service` + `.timer`, `appshelf-backup.service` + `.timer` |

## Requirements

- A Linux server (tested on Debian 13), Python ≥ 3.11 with `venv`.
- Apache 2.4 with `mod_ssl`, `mod_proxy`, `mod_proxy_http`, `mod_headers` and a domain with a valid HTTPS
  certificate: iOS installs over `itms-services` only from trusted HTTPS.
- Docker, only to build `bin/ipatool`: on the server or on a separate host (`APPSHELF_BUILD_SSH` in `.env`) with
  passwordless sudo; the server needs the `libunicorn2t64` package.
- Optional: a local `sendmail` (e.g. exim4) for emails; for the weekly digest to reach people it must also deliver
  to external addresses (see "Mail").
- Android needs AOSP `apksigner` and a Java JDK (`apksigner default-jdk-headless` on Debian, installed by
  `deploy/install.sh`). They verify APK signatures and the signed F-Droid catalog before publication.
  Set `APPSHELF_APKSIGNER` to a verifier executable or its `.jar` when configuring it manually.
- The 4PDA sign-in window needs Chromium and Xvfb (`sudo bash deploy/setup-fourpda.sh` on Debian).
  Deployment installs Playwright; for a manual setup, install the extra with `pip install '.[fourpda]'`.

## Personal 4PDA account

Open "Аккаунты" → "4PDA" → "Подключить или изменить" → "Войти в 4PDA". Complete the sign-in and CAPTCHA
yourself in the displayed browser, then select "Подключить аккаунт". On a phone, tap the forum's input field,
then type using the keyboard below the window.

Cloudflare may repeat its challenge indefinitely: browsers controlled by Playwright are
[unsupported for solving challenges](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/).
If that happens, sign into 4PDA in a normal browser tab and paste the `4pda.to` cookies as JSON on the connection
page, or upload a JSON file. The page has Firefox Android instructions for
[Cookie-Editor](https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/): export the current tab as JSON to
the clipboard. Import from the same browser where you signed in, so AppShelf receives the matching User-Agent.
The server checks the session before saving it; if 4PDA still demands a challenge, the connection remains unavailable.

On "Android" → "Добавить", search by app name or paste a topic/message link into the combined search.
4PDA topics appear alongside other sources; select "Выбрать APK", choose one attachment and add it to your shelf.
This source uses the current person's account and is unavailable on the public shelf.
A direct link to one APK such as `https://4pda.to/forum/dl/post/<id>/<name>.apk` can be pasted into "По ссылке"
(By link): it is downloaded with the connected 4PDA account. The same field accepts it when changing the source of an
existing app. Topic links go into the combined search; a temporary link with `dlsess` does not work.
Each file passes the same APK and cryptographic signature checks as other sources. APKS/XAPK bundles are unsupported.
The selected attachment stays fixed and is not checked at night: for a new version use "Выбрать новую версию в теме"
(choose a new version in the topic) on the card — the topic marks the attachment on the shelf and newer ones. Other
builds or modifications are never substituted automatically.

Passwords are not saved. The session is kept in `/etc/appshelf/fourpda/<person id>.json` (directory 0700,
files 0600), separately from the database and archive. Other people and file CDNs never receive it.
The temporary download URL is obtained again from the attachment page and is not stored in a card or job.
An unfinished sign-in window closes after ten minutes; a person can open it at most five times an hour. A session
JSON may be up to 32 KB. "Отключить 4PDA" removes the local session;
revoking the forum session or signing out on all devices is done separately on 4PDA.
Set `APPSHELF_FOURPDA_BROWSER` in `appshelf.env` to select a browser executable manually.

## Installation

Run the server commands as a user with sudo.

1. User and directories. The code directory belongs to root: appshelf only reads and runs it.

       sudo useradd --system --home /var/_sh/appshelf --shell /usr/sbin/nologin appshelf
       sudo install -d -o root -g root -m 0755 /var/_sh/appshelf /var/_sh/appshelf/bin
       sudo install -d -o appshelf -g appshelf -m 0711 /var/lib/appshelf /var/lib/appshelf/pub /var/lib/appshelf/status
       sudo install -d -o appshelf -g appshelf -m 0700 /var/lib/appshelf/tmp
       sudo install -d -o root -g appshelf -m 0711 /etc/appshelf
       sudo install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts /var/lib/appshelf/locks

2. Python: `sudo python3 -m venv /var/_sh/appshelf/.venv` (as root, like everything in the code directory).

3. Settings: copy `deploy/appshelf.env.example` to `/etc/appshelf/appshelf.env` (`root:appshelf 0640`) and
   fill in `PUB_TOKEN` (`python3 -c "import secrets; print(secrets.token_hex(16))"`), `APPSHELF_PUBLIC_BASE`,
   `MAIL_TO`, `APPSHELF_OWNER` (your Apple ID).

4. Code and services: on your machine create `.env` from `.env.example` and run `bash deploy/deploy.sh` (code,
   dependencies, units; it warns that `bin/ipatool` is not built yet), then on the server
   `sudo systemctl enable appshelf-web && sudo systemctl enable --now appshelf-nightly.timer`.

5. `bin/ipatool` and `bin/ipatool-auth`: on your machine run `bash deploy/build-ipatool.sh` → `OK: /var/_sh/appshelf/bin/ipatool`.

6. Apache: a vhost based on `deploy/apache-appshelf.conf` (domain, certificate, archive path; the
   `Referrer-Policy: same-origin`, `nosniff` and HSTS headers unless set globally), then
   `sudo a2enmod ssl proxy proxy_http headers && sudo apache2ctl configtest && sudo systemctl reload apache2`.
   Check: `https://<domain>/healthz` → 200, `/` → sign-in form.

7. First sign-in: open the site and sign in with the Apple ID from `APPSHELF_OWNER` (password and 2FA code) —
   you become the owner. Invite others on the "Участники" (People) page.

**Permissions.** The code, `.venv` and `bin/` belong to root (`deploy/install.sh` enforces it on every deploy):
otherwise any code execution as appshelf (an image from an IPA, ipatool) would become root at the next deploy or
build. The code group (`APPSHELF_CODE_GROUP` in `.env`) gets read access only, and appshelf is not in it. The
services see `/usr` and `/etc` read-only (`ProtectSystem=full`), except `/etc/appshelf/accounts` and
`/etc/appshelf/google`, plus `/etc/appshelf/fourpda` for `appshelf-web`; `appshelf-backup` writes nothing in `/etc`.
`/var/lib/appshelf` and `status/` are `0711`: Apache and monitoring pass through without
listing. `/etc/appshelf` is `root:appshelf 0711`, so Apache cannot read `appshelf.env`. **Do not add
www-data to the `appshelf` group**: Apache reaches the socket through the `www-data` group on `/run/appshelf`
(`ExecStartPost` in the unit); otherwise an RCE in any PHP site on the server could read `PUB_TOKEN`
(`deploy.sh` checks this). An archive on a share is open to whoever the mount opens it to; `-Indexes` and the
token in the link close it to the internet.

## Deploy and build

    bash deploy/deploy.sh                                          # code + restart; target is in .env (.env.example)
    bash deploy/build-ipatool.sh                                   # bin/ipatool (after changing ipatool/ and deploying)

What ships is the committed `HEAD` (`git archive`): with uncommitted changes in the code `deploy.sh` refuses. While
the nightly check runs (`appshelf-nightly.service`, up to 4 h) the deploy refuses too: that process keeps the old
code in memory and imports modules lazily; wait for it or stop it (`sudo systemctl stop appshelf-nightly.service`,
the next night finishes what was interrupted). During the install `appshelf-nightly.timer` is stopped (a missed run
catches up afterwards), so the check cannot start halfway through the code swap. The new code replaces the old one as a whole and only after `pip`
succeeds. If `/healthz` does not answer 200 within 10 s, `install.sh` restores the previous code (`.previous`) by
itself when the database is readable by it, otherwise it prints the manual steps. To roll back, deploy the previous
commit (across a database schema change — only if that migration declared the schema readable by the older code, see
`SCHEMA_VERSION` in `store.py`). If
`bin/ipatool` or `bin/ipatool-auth` was not built from the deployed `ipatool/`, `deploy.sh` reminds you to rebuild;
when to rebuild without changes — [ipatool/README.md](ipatool/README.md). `build-ipatool.sh` builds both from the
`ipatool/` deployed on the server in a throwaway `debian:trixie` container on the `APPSHELF_BUILD_SSH` host (or on
the server itself if unset), checks them on the server as `appshelf`, prepares `bin/ipatool-auth-cache` and installs
them only if the server has all their libraries.

## Sign-in and people

You sign in to the panel with an Apple ID and its password, then a 2FA code. The password is not stored: ipatool
gets it via stdin, appshelf-web keeps it in memory for at most 10 minutes. The sign-in is remembered for a year;
"Sign out on all devices" (the "Аккаунты" (Accounts) page) revokes all of the person's cookies. You can also sign in
with a Google account — see "Sign in with Google" below.

A new Apple ID gets onto the server only through an invite link: "Участники" (People) → "Create link". The link
works until it is disabled or deleted — one for everyone or one per person; it is asked for once, on the first
sign-in. A disabled link stays in the list, a deleted one disappears from it.
A signed-in person adds another Apple ID themselves, without an invite: "Accounts" → "Add Apple ID". The new Apple
ID is not attached to anyone — it becomes a separate person: its shelf is connected in this browser right away, on
other devices you sign in with it separately, and the owner gets an email about the new person. Each Apple ID has
its own shelf: history, catalog, IPAs (an IPA carries the buyer's data and installs on an iPhone signed in to the
App Store with the same Apple ID).

One browser can be signed in as several people at once: every further sign-in — the sign-in form, "Add Apple ID"
with another person's Apple ID, an invite, a fallback link — adds that person to those already signed in, and the
Shelf menu in the header switches between everyone's shelves (each shows its sign-in state). This is how a shared Apple ID is used: it stays a
separate person, and signing in with it alone opens only its shelf. "Remove from this device" (the "Accounts" page)
removes one person from the browser, "Sign out" in Settings removes all; "Sign out on all devices" revokes one
person everywhere.

The header's Shelf menu selects the current shelf, with "Аккаунты и полки…" (Accounts and shelves) and "Добавить
Apple ID" (Add Apple ID) at the bottom. Settings contains accounts, connections, people, theme (System / Light /
Dark), the "Android-устройство" (Android device) profile, mail, help and sign-out. "Покупки" (Purchases, `/history`,
the Apple ID purchase history) and "Удалённые" (Removed) remain in the Apple bottom bar.

A repeated "Sign in" on the 2FA code step (double tap, slow Apple answer) joins the first sign-in instead of starting
a new one. Sign-in page hints fit the device: iPhone, Android or computer. Signing in as the owner (Apple ID, Google,
fallback link) in a browser without the owner's entry mails the owner the method, time, address and browser.

State cookies (sign-in and its flow, shelf choice, Android section person, Google flow, invite, new link, public
shelf guest) carry the `__Host-` prefix on https, so a sibling subdomain cannot plant them. Cookies under the old
names are read until 2028-01-01, so a deploy signs nobody out. Pages set `<meta name="referrer"
content="same-origin">` themselves: their own POSTs carry `Origin` even under a global `Referrer-Policy:
no-referrer`, without which old Safari (before 16.4, no `Sec-Fetch-Site`) would get 403.

When a store token expires there is a banner on the shelf and "Sign in again"; the owner gets an email about
their Apple ID. If Apple breaks sign-in, use a fallback link (one-time, 24 h; it opens a page with a "Sign in"
button, so a messenger's link preview does not use it up): "Участники" → "Sign-in link", or on the server

    sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf login-link --email <Apple ID>'

To Apple all sign-ins come from the server: the client is Apple Configurator on a Mac, and the map in the sign-in
prompt shows the server's IP location. Each new Apple ID gets its own MAC (`IPATOOL_DEVICE_MAC`, patch 05) — a
separate "Mac" to Apple.

The sign-in itself is done by `bin/ipatool-auth` (`ipatool/auth/`, details in
[ipatool/auth/README.md](ipatool/auth/README.md)): Apple refuses the C++ client's old sign-in (2026-11-08), and
`bin/ipatool auth login --password-stdin` passes the password on stdin to this client. It signs in with the protocol
of current ipatool and imports the store token into the existing encrypted `account` without the password;
purchase history and downloads stay in `bin/ipatool`. Next to the account lives `adi-state`, the device
provisioning state (key derived from `machine-id`, `0600`); an unreadable one (another machine, corruption) does
not block sign-in — it runs as a first sign-in and rewrites the file. Apple and Unicorn libraries for sign-in are in
`bin/ipatool-auth-cache` (root, appshelf only reads it), prepared by `build-ipatool.sh`.

A console sign-in is only good for testing the fork: the site does not learn about it, so jobs and the nightly
check stay idle until you sign in through the panel. If you need it anyway — use the service's path (without
`--password-stdin` the old sign-in runs), only as `appshelf` and under that Apple ID's lock (otherwise the service
cannot read the account file or collides with a running download). Take the password from `read -s`: it stays out
of argv and shell history. On exit code 4 (`auth_code_required`) repeat with `--auth-code <2FA code>`:

    read -rsp 'Apple ID password: ' PW; echo
    printf '%s\n' "$PW" | sudo -u appshelf env HOME=/etc/appshelf/accounts/<id> IPATOOL_DEVICE_MAC=<accounts.device_mac, if not empty> flock /var/lib/appshelf/locks/<id>.lock /var/_sh/appshelf/bin/ipatool auth login -e <Apple ID> --password-stdin
    unset PW

The ipatool account is the file `$HOME/.ipatool/account`, and each launch requirement has its own reason:
- `HOME` only selects whose account to use: every Apple ID has its own `accounts/<id>`. The path is not part of
  the encryption key — a ready `.ipatool` can be moved to another `HOME` on the same machine.
- The key is PBKDF2 over `machine-id`, a passphrase (empty for appshelf) and a salt stored in the file itself. For
  root, `product_uuid` (readable only by root) is added to `machine-id`: an account saved as root cannot be
  decrypted by the service, and one saved by the service cannot be decrypted by root. On another machine
  (different `machine-id`) none of them decrypt.
- The account files belong to `appshelf` with `0700`/`0600`: no other user except root can read them.

### Sign in with Google

"Войти через Google" (Sign in with Google) on the sign-in page uses OpenID Connect (Authorization Code + PKCE). The
Google password is typed on Google's page: the server gets only the account's identity (`sub`, address, name) — no
password and no access to Google. The button appears when `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set in
`appshelf.env`; without them `/google/*` answers 404.

The owner creates the OAuth client once (free; `openid email profile` needs no Google app verification):

1. [Google Cloud Console](https://console.cloud.google.com/) → a new project.
2. "APIs & Services" → "OAuth consent screen": user type External, publishing status In production; scopes — only
   `openid`, `email`, `profile` (none sensitive). "Publish app" stays disabled without a home page (`<site address>`),
   a privacy policy link (`<site address>/privacy` — an appshelf page that opens without signing in) and the site's
   domain in Authorized domains. The Google account needs 2-step verification to use the Cloud Console.
3. "Credentials" → "Create credentials" → "OAuth client ID": type Web application, Authorized redirect URI — exactly
   `<site address>/google/callback` (`APPSHELF_PUBLIC_BASE` + `/google/callback`).
4. Client ID and Client secret go to `/etc/appshelf/appshelf.env` (`GOOGLE_CLIENT_ID=`, `GOOGLE_CLIENT_SECRET=`; the
   secret lives only there), then `sudo systemctl restart appshelf-web`.

As with Apple IDs, a new Google account gets onto the server only through an invite link or from "Accounts" →
"Добавить Google" (Add Google) of a signed-in person — and always as a separate person; the owner gets an email.
Signing in adds the person to those already signed in to the browser. A person with only a Google account has an
empty Apple section — "Add Apple ID" on the "Accounts" page. The Android section shows the shelf of one of the
signed-in people; the choice is in the section's Shelf menu: "Личные" (Personal) — the signed-in people, "Общие"
(Shared) — "Публичная" (Public).

**"Связать" (Link).** When two people are signed in to a browser and at least one of them has no Apple ID, the
"Accounts" page offers "Link": the second person's Google accounts and Android shelf move to the first, the second
person is deleted, and signing in with any of their accounts opens all of the first person's shelves. The server
decides who stays: the person with an Apple ID; if neither has one — the owner, otherwise whoever signed in to this
browser first. An app present on both Android shelves stays with the one who stays. A link cannot be undone. Two
people who each have an Apple ID cannot be linked, and an Apple ID never moves to another person: a shared Apple ID
would open someone else's personal shelves. Do not link an account several people use.

To delete a Google account — "Accounts" → "Удалить" (Delete); if it was the person's last account, the person is
deleted together with their Android shelf. A deleted Google account can be added again — as a separate person.

An iPhone Home Screen web app keeps its cookies apart from Safari, and the return from Google may land in Safari
instead of the web app; then sign in there with an Apple ID or open the page in Safari.

## Removed apps

Apple's purchase history (DAAP, as used by Apple Configurator) does not include apps removed from the store,
even when you hold the license and the app downloads by id. Banks also released dozens of clones under other names.
"Refresh history" therefore also checks the catalog `appshelf/data/removed_apps.json` (sources in the `source`
field) against your Apple ID: your own ones are added to "Покупки" (Purchases) marked "удалено из App Store"
(removed from the App Store; the mark stays after downloading). The button skips apps that had no license; the
nightly check re-checks them once a week, on the Apple ID's own weekday, and fills in versions then. The "Removed"
page (`/blocked`, filter `?status=owned|unknown|missing|failed|all`, by default what the account owns) shows the
whole catalog and lets you add an app by an `apps.apple.com/…/id492224193` link or ID; the name comes from the IPA.
An app added by link without a license gets "not in purchases"; once bought, "Refresh history" or the catalog check
queues it for publishing by itself, and "Retry" is there too. To extend the catalog, append
`{"id", "name", "aliases"}` to the JSON (aliases are for Latin-script search: vk, sber…). Whether an ID is gone from
the RU App Store: `itunes.apple.com/lookup?id=…&country=ru` (empty — removed).

The catalog check takes minutes (~480 requests) and yields between apps to interactive Apple and Android jobs.
Platforms alternate and background work also advances; queue status and waiting reasons are separate from errors.
While it runs, "Removed" and "Purchases"
show "checked X of Y: found …, no license …, errors …" — the progress lives in the job (`jobs.progress`) and survives
a page reload and yielding. When it ends, "Removed" shows the result of the last check, and IDs that Apple answered
with a technical error are marked "check failed", separately from "not in account"; the next check asks them again.
An expired sign-in does not mean no license: an unconfirmed ID gets another attempt after a fresh sign-in,
at most once per sign-in. After two such failures, only that ID is deferred for a week; other apps remain eligible.

The catalog shows a version's build date: the time of `Info.plist` in the IPA. Apple's `releaseDate` (both in
`get-version-metadata` and in `iTunesMetadata.plist`) is the app's first release date, not the version's.

## Updating an installed app

iOS does not install over an app that is already on the device via `itms-services`: "Install" is answered with
"… is Already Installed" and the advice to delete the app. Neither the version nor the Apple ID matters: iOS decides
before the download, by the app identifier alone. There is no need to delete the app — offload it: Settings →
General → iPhone Storage → the app → Offload App, then "Install" in the catalog. Offloading removes the app but
keeps its data, so you get the shelf's version with your data in place. The Home Screen icon will not bring back an
offloaded app that was removed from the App Store — only "Install" from the shelf will.

A pinned version ("⋯" → "Pin", one per app) is not deleted by updates: once it leaves the current and previous
slots it stays in the catalog as "kept" with a regular install link and takes space in the archive until unpinned.
Only the file is kept: rolling back does not guarantee that the app accepts data left by a newer version —
installing it over that data (after offloading the newer one) is at your own risk.

"⋯" → "Версии и закрепления" (Versions and pins) → "Удалить" (Delete) removes the selected old IPA after
confirmation, pinned ones included. The current version cannot be deleted on its own; the deleted version's file and
install link stop working.

"⋯" → "Check for update" asks Apple for the latest version of one app right away, without waiting for the night and
without checking the history and the catalog; repeated taps do not queue a second job, and without an Apple ID
sign-in it waits for one. If Apple refuses, the current version stays and installs. The card shows the result of the
last check (by the button or at night): "checked DD.MM HH:MM UTC — no newer version / new version downloaded /
failed: …"; this is about the shelf, not the version on the phone.
"Проверить обновления всех" (check all for updates) above the catalog queues this check for every app on the shelf.
"Снять с публикации" (unpublish) while any download runs answers "Полка занята скачиванием" (shelf busy) — retry in
a minute.

## Android

The same server also keeps Android shelves: the **Android** section of the panel, with an Apple/Android switch in
the header. From an Android phone the panel opens right in it, from an iPhone in Apple, from a computer in the
section opened last. An Android shelf belongs to a person, not to an Apple ID: an APK is not tied to any account.
Design (Russian): [docs/superpowers/specs/2026-10-28-android-design.md](docs/superpowers/specs/2026-10-28-android-design.md)
(stages 1–5 are done: RuStore, direct link, uploaded file, public shelf by PIN, Google sign-in and linking, Google
Play — free apps that come as one APK, AppGallery, F-Droid, APKPure).

- **Sources.** "Add" has the segments "Поиск · Ссылка · Google Play · Файл" (Search · Link · Google Play · File).
  The combined search by name, package or link covers RuStore, F-Droid, the F-Droid archive, IzzyOnDroid, GitHub,
  Codeberg, GitLab, Google Play, AppGallery, APKPure and your connected 4PDA account. Choose a source and, for F-Droid, a version
  or, for 4PDA, a specific attachment. AppGallery and APKPure are
  checked for up to five matching package names from the catalogs and public Play search; these are not complete
  store catalogs. A failed source is reported separately without hiding other results. Google Play uses a
  connected Google account (below), "Link" accepts an app page on
  Google Play, AppGallery (or its ID like `C102312137`), F-Droid, IzzyOnDroid, APKPure, RuStore (the source and
  package are taken from the address), a repository page on GitHub, codeberg.org or gitlab.com, a 4PDA attachment
  link (see "Personal 4PDA account") or a direct `https` link to an APK on a bank's or developer's site, "File" — an
  upload of up to 1 GB. The server never follows a link into an internal network: the name is resolved before every
  request and after every redirect, and any non-global address (private, loopback, link-local, multicast) is refused
  before connecting. A source request is bounded by 5 minutes as a whole (redirects, headers, body), an APK download
  by 30 minutes.
- **Own file.** The APK goes in 1 MiB chunks over two streams, with progress, size and speed shown. Pause and resume
  are supported; after a drop only the undelivered chunk is repeated. Received chunks are kept for 7 days after the
  last one; an upload without a single chunk expires after an hour, and one silent for an hour keeps only what it
  received in reserve. Space reserve: up to 1 GB and at most two unfinished uploads per person, 2 GB per server;
  with no room to continue the browser waits. The "Незавершённые загрузки" (Unfinished uploads) block on "Add"
  cancels abandoned ones (the owner sees and cancels everyone's). To continue after the page is closed the browser
  keeps a copy of the file until completion or cancellation; if it has no room, choose the same APK again — the
  server's chunks are kept. The service worker sends in the background while the browser allows it. If Android puts
  Firefox to sleep, the upload continues when you return to the site. After assembly the APK passes the usual
  authenticity check; an oversized or damaged file, a server refusal or an expired sign-in is shown as a message. A
  job without a name is labelled with the file name.
- **Codeberg and GitLab.** Like GitHub: the latest stable release of a public repository, one ARM APK, files only
  from the same server and project (for GitLab also uploads linked in the release description). Only codeberg.org
  and gitlab.com: self-hosted Forgejo and GitLab servers cannot be told apart from any website by address.
- **IzzyOnDroid.** An additional [F-Droid repository](https://apt.izzysoft.de/fdroid/), labelled "F-Droid ·
  IzzyOnDroid" in search: the current and saved versions are available, adding by the link
  `https://apt.izzysoft.de/fdroid/index/apk/<package>` and switching a card's source. The current version is updated
  at night, a selected exact version stays fixed. The catalog has its own cache and is verified with the
  repository's own pinned key; the APK is checked against the SHA-256 of the signed catalog and passes the common
  cryptographic check before publication.
- **Search progress.** The page opens immediately and streams source status and partial results through SSE.
  "Stop" closes the search while retaining discovered options; a completed or interrupted search can be retried.
  Network errors include the affected domain; APKPure metadata uses `api.pureapk.com`.
- **Source limits.** One whole APK only, nothing paid except apps bought in Google Play. The terms of RuStore,
  AppGallery, APKPure and Google Play forbid automated downloads: requests go one at a time with a pause, the nightly
  check runs once a day; the server owner accepts the risk of losing access.
- **Installing.** "Download APK" in Chrome on the phone → "Open" on Chrome's bar or in the download notification →
  "Install"; the first time Android asks to allow installs from the browser. A site cannot install right away: the
  browser only downloads the file, and the person opens the installer. Updating is the same "Download APK" over the installed app, its data stays. A browser installs only one
  whole APK: bundles (split APK, XAPK, APKS) are refused when added. "Copy link" in the card's "⋯" gives a permanent address
  `<site address>/apk/<shelf token>/<package>.apk`: without parameters it follows the latest version; selecting an older
  build adds `?version=<id>`. It opens without signing in (like the files themselves); changing the shelf token
  (`rotate-token`, a new PIN of the public shelf) disables it too.
- **Signature.** Before publication, `apksigner` verifies the APK signature and its content integrity; a failed
  check preserves the working version. The first version sets the expected certificate. A different signer
  remains pending: "Сравнить версии и решить" compares versions, sources, full fingerprints and verification
  results. Accept it with confirmation, or reject the build. Without confirmed compatibility, installation
  may require removing the app and its data. A verified v3 key rotation chain is labelled "Ключ обновлён разработчиком"
  with the minimum Android version; the decision stays manual. A matching key alone does not guarantee a safe app.
  Until decided the version lives in `<package>-<versionCode>-p<32 hex>` and cannot be downloaded through shelf links;
  "Принять" (Accept) moves it to the regular directory. The night does not download a pending or rejected version
  again (same source and versionCode). The owner gets a mail about a version with a new key on their own and the
  public shelf (one per decision); the night's result adds "ждут решения по подписи: K" (awaiting a signature
  decision).
- **What's new.** Each version has a collapsed "Что нового" (What's new) text, for guests too: from a GitHub,
  Codeberg or GitLab release, F-Droid and IzzyOnDroid `whatsNew` (Russian, else English) and RuStore. Plain text
  only, up to 2000 characters, the last three versions are kept.
- **Versions.** Each card lists all saved APKs and selects the latest accepted `versionCode` by default.
  Add older builds by link, search or file upload; they keep the latest version and its update source in place.
  After a new current version is saved, the current, previous and one pinned APK remain; older unpinned builds are
  removed from the database and the archive (only after the database commit succeeds). This applies to personal and
  public shelves, including previously accumulated history at the next update. A manually added older build can be
  pinned until the next update; a pin protects the file from automatic removal and does not stop new versions. A
  failed update or a pending signature decision does not trigger cleanup. In "Версии и закрепления" (Versions and
  pins) an old APK can be deleted manually after confirmation, pinned ones included; the current version and a build
  awaiting a signature decision cannot be deleted this way. Its person can do this on a personal shelf, the owner on
  the public one. Identical APKs are not added twice. A different signature requires confirmation;
  accepting an older build adds it to the list while keeping the latest as the default. Selecting a version updates
  the download, details, shared link and QR code; reloading selects the latest again. Android may require removing
  the newer app and its data before installing an older build. "⋯" manages pins and sources; unpinning removes
  protection, so the next successful update may delete that APK.
  Layout:
  `<archive>/<Android shelf token>/<package>-<versionCode>/{<package>-<version>.apk, icon.png}`.
- **Device compatibility.** The optional "Android-устройство" (Android device) profile (Settings; Android pages show
  one line about it) stores Android and supported ABIs only in
  this browser. Choosing a saved APK updates its known compatibility, including minimum Android, old target SDK
  install restrictions and the verified key rotation threshold. Missing data is marked unknown; Android makes the
  final installation decision. F-Droid suggests a suitable build, while keeping an explicit manual selection.
- **Downloaded in this browser.** "Download APK" marks the version on the card (this browser's localStorage); the
  "Есть обновление для меня" (Update available for me) filter shows apps where this browser downloaded a version older
  than the current one. These marks never reach the server.
- **Icons.** PNG/WebP and regular vector/adaptive icons are extracted from APK resources, including shortened file
  names. Vector layers are rendered to PNG; unsupported or damaged drawings leave a placeholder.
- **Nightly check** — after the Apple IDs, and without any Apple ID sign-in too: RuStore, Google Play, AppGallery and
  APKPure by `versionCode`, F-Droid by the suggested version, GitHub, Codeberg and GitLab by the latest stable release,
  the F-Droid archive by the selected version (an exact version stays unchanged), a link by `ETag`, `Last-Modified`,
  `Content-Length`; uploaded files and 4PDA are not checked. The night's result ends with
  "Android: обновлено N, без изменений M, ошибок K".
- **Russian CA certificates.** The RuStore CDN and the Sber and VTB sites use certificates of the Russian Ministry of
  Digital Development CA, which system roots do not trust. The APK downloader (and only it) takes extra certificates
  from the `APPSHELF_SOURCES_CA` file; certificate checks are never disabled: Russian Trusted Root CA (`gu-st.ru`)
  and, for Gazprombank, whose server sends no chain, the LiteSSL RSA CA 2025 intermediate. Without the file RuStore
  downloads fail.
- **Apache.** In the archive's `<Directory>` — `AddType application/vnd.android.package-archive .apk` and
  `Content-Disposition: attachment` for `.apk` (with `application/zip` Chrome saves `.apk.zip`); for the upload
  URLs of own and public shelves — `LimitRequestBody 1100000000`. Uploads go to `TMPDIR=/var/lib/appshelf/tmp`
  (set in the `appshelf-web` unit), not to `/tmp`.

Help and the "How to install" dialog ("?" on a card) cover Samsung Auto Blocker, Huawei Pure Mode and Play Protect. Google's developer verification
expands gradually from 2027 on certified Android 7+ devices; timing depends on region and distribution channel.
Advanced flow, ADB and specific exceptions remain available; see the [official Google FAQ](https://developer.android.com/developer-verification/guides/faq).

### Google Play

A small client of the Google Play protocol of our own (`appshelf/android/play.py`), no external programs. Downloads go
through a Google account connected to Play on the accounts page; there is no shared service account.

- **Connecting** — "Подключить к Google Play" next to a Google account: on a computer sign in with that account at
  `accounts.google.com/EmbeddedSetup`, copy the `oauth_token` cookie (F12 → Application → Cookies) and paste it into the
  form within 10 minutes. The server exchanges it for a master token; the account address in Google's answer must
  match, otherwise it is refused. **The master token gives access to the whole Google account, not only to Play**, the
  server owner can technically use it, and Google may block the account for a third-party client — the connect page
  warns about all of this. The server never gets the Google password.
- **Where tokens live.** The master token and the "device" profile are files in `/etc/appshelf/google/<id>/`
  (`appshelf`, directory 0700, files 0600; `deploy/install.sh` creates `/etc/appshelf/google`, the units write there
  via `ReadWritePaths`), the Play token stays in process memory. Never in the database, logs or argv. "Отключить Google
  Play" and deleting the Google account delete the directory.
- **Checked before adding.** When adding from search, by link or by package name, the server first checks that the
  app is available to the account and the target shelf, then that it is delivered as one APK. A file set (split APK,
  OBB, assets), an unpurchased paid app and an app unavailable to the account are refused in the form: no card and no
  job are created. The check repeats on download. If delivery changed after adding, the worker suggests other
  sources; a new version that arrives as a set leaves the previous one on the shelf, marked.
- **Purchased apps** go only to the personal shelf of the person whose account bought them (checked on the server):
  not to the public shelf, not to anyone else's; public shelf search shows them as "купленное — только на личную
  полку" (purchased — personal shelf only) without a button. "My purchases" will come after a live check of
  purchases through a proxy in the account's country.
- **Proxies** — for the account's country: `APPSHELF_PROXIES` in `appshelf.env` (space-separated `name=url`,
  `http://host:port` or `socks5h://[user:password@]host:port`), chosen by the owner when connecting. A proxy is used
  only for Google Play requests; if it is down, the card shows an error — there is no direct fallback. In the live
  check from a Russian address, Play showed ruble prices and refused an unpurchased paid app; delivery of a
  previously purchased app has not been checked yet.
- **Expired.** A changed Google password or revoked access means "подключение Google Play истекло": a banner in the
  Android section and on the accounts page, the account's jobs wait, its apps are skipped at night; mail goes only
  about the owner's Google accounts, one per episode. "Подключить заново" takes a new `oauth_token`.
- **Live test** — `pytest -m integration tests/test_android_play_live.py` with `APPSHELF_PLAY_TEST_GID=<id of a
  connected Google account>` in `.env`: `details` of a free package as `appshelf` on the server.

### AppGallery, F-Droid, APKPure

Added through the combined search or "Link" with the app's page; "⋯ → Change source" switches a card to any of them by
its package. The signature of every downloaded version is compared with RuStore.

- **AppGallery** — Huawei's client API without sign-in (`client.updateCheck` by package name: version, file size and
  SHA-256, APK link); an ID from a page (`appgallery.huawei.com/app/C…`) is turned into the package. The API host
  defaults to the Russian `store-drru.hispace.dbankcloud.ru`, another one goes to `APPSHELF_APPGALLERY_HOST` in
  `appshelf.env`. From a foreign address AppGallery answers for another zone with no banks — "AppGallery не нашёл
  приложение (зона …)". Huawei may re-sign an app with its own key: a signature other than RuStore's gives the card
  the mark "подписан не так, как в RuStore: не встанет поверх версии оттуда" (the app is still added).
- **F-Droid** — the official `f-droid.org` repository: the `entry.jar` signature is checked against the pinned
  repository certificate, followed by the signed SHA-256 of `index-v2.json` and the APK hash from that catalog.
  Unknown keys, corruption, timestamp rollback and indexes older than fourteen days are rejected. Trusted keys
  change only through a reviewed AppShelf release. One version may have several builds per ABI, and the suggested one is not always ARM
  (VLC's is x86_64): by the verified catalog's `nativecode` the shelf picks a universal or `arm64-v8a` build,
  otherwise `armeabi-v7a`, otherwise "в F-Droid нет сборки для ARM", and downloads only that one. There are no banks
  in F-Droid.
- **F-Droid archive** is a separate source for removed apps and older versions of active apps. A 404 from the
  main package API automatically checks the archive. Normalized catalogs persist locally in
  `/var/lib/appshelf/cache/fdroid/` for six hours and survive web/nightly restarts; after a failed refresh the catalog
  is not downloaded again for 15 minutes. During a source outage, search can show clearly labelled stale
  results; downloading requires fresh validation. Indexed APKs are checked against SHA-256 and the certificate.
  Selecting an exact version or an archive APK URL keeps
  that version without updating it.
- **GitHub** checks the latest stable release for a universal or ARM APK. Automatic checks run only when a store
  returns a bundle: the app description or one developer website page supplies the
  repository, and only that repository's releases are checked. There is no global GitHub search. An explicit
  repository URL can also be supplied. Release information is cached for 15 minutes; API rate limits fall back
  to the same repository's public release page. Repository links follow releases; direct APK links retain the
  selected file. The developer's build may have another package ID and is offered separately. An `http://`
  developer website is skipped.
- **APKPure** — the APKPure client API without sign-in, single APKs only: an XAPK is refused as a bundle with a hint
  where the package comes as one APK. APKPure has distributed tampered APKs (2021 — a trojan in its client, 2025 —
  Telegram X with a backdoor), so: if the package is in RuStore the signature **must** match, otherwise "подпись не
  совпала с RuStore"; if not, it is added only with the "I understand" checkbox and the mark "подпись не сверена",
  then the general signature rule applies.

### Public shelf

One Android shelf per server for people without a panel sign-in. The owner fills it (Android → Shelf menu → **Публичная** →
"Добавить в публичную": the same sources), people select it in that menu without a PIN and without
actions, a guest opens `<site address>/p` and enters the PIN — and sees only the list with "Download APK" and the
hints, search and filters; guests cannot manage the shelf.

- **From my shelf** — "Сравнить и добавить в публичную" lets the owner select an app already downloaded on their personal
  Android shelf; it is in the app's "⋯" menu, including on eligible personal cards. It copies
  accepted saved APKs without downloading them again. A comparison shows versions, content hashes, pins and possible
  signature decisions before confirmation; repeat copying adds missing builds while preserving the public update
  source. Pending and paid builds are excluded. The public shelf has the same retention policy; personal pins are
  not inherited. Deleting the personal copy leaves the public copy intact. New personal downloads are never
  automatically published. Guests and people pick an older version in the card's "Версия" (Version) list; download
  and QR follow the choice. 4PDA and uploaded files get no nightly updates on the public shelf either; a 4PDA copy
  never contacts 4PDA (it has no "Повторить" (Retry)), new versions arrive by copying again.
- **QR code** — the QR icon button to the right of "Скачать APK" reveals it on each public app, including on the
  guest page after entering the PIN. It links directly to the current APK; updates keep the link, while changing
  the PIN or shelf token invalidates old codes.
- **PIN** — 6 digits, set and changed by the owner in the collapsed "Гостевой доступ" block below public apps. Until a PIN is
  set, `/p` shows guests nothing. The database keeps only a salt and a hash (PBKDF2-SHA256, 200,000 iterations), the
  PIN itself is stored nowhere. After a right PIN the guest's browser remembers the shelf for 90 days (a cookie
  signed with `cookie-key`).
- **Changing the PIN** invalidates all guest cookies and gives the public shelf a new directory token: APK links
  shared before stop working. While a download runs, the PIN is not changed — "try again in a minute".
- **Brute force.** 20 wrong PINs from one address in an hour close that address for an hour (IPv6 — by its /64).
  100 wrong PINs from all addresses in a day close PIN entry to new guests for a day; guests with a valid cookie
  still see the shelf. Anything but 6 digits is not an attempt. Counters live in the database, a deploy does not
  reset them. The guest's address is the last element of `X-Forwarded-For`, which Apache appends: there must be no
  other proxy in front of it, or all guests share one counter.
- **The "ввод PIN публичной полки закрыт" mail** — one per episode: someone is guessing the PIN. To reopen entry
  right away — "Открыть ввод PIN" on the public shelf; if the PIN may have leaked, change it (that reopens entry too).
- File links, as with IPAs, work without the PIN: the PIN guards the page, not the files. Purchased apps never go to
  the public shelf.

## Home Screen (PWA)

Safari → Share → Add to Home Screen opens the shelf without the address bar. The manifest and icon (`/pwa/`, drawn
by `web/pwa.py`) are served without a password — by the application and by Apache (`<Location /pwa/>`). A Home
Screen web app does not show a Basic Auth window (401 → black screen), so sign-in is the `/login` form with a
one-year cookie. The upload service worker transfers APK chunks and can resume when the browser returns. It does not cache pages or guarantee continuous background
execution: Android or the browser may stop it. The selected file is kept in IndexedDB until transfer or cancellation.

## Mail

Sent through the local `sendmail`; recurring ones once per episode. To the owner, at `MAIL_TO`: their Apple ID
sign-in expired, low disk space, their account's Google Play connection expired, a new person, a sign-in as the owner
in a browser without the owner's entry, public shelf PIN entry closed, an Android version with another key (own and
public shelves), the nightly integrity check found problems.

The weekly digest "что обновилось на полках" (what was updated on the shelves) goes to every person at most once in
7 days and is not sent when empty: new versions on the shelves of their Apple IDs and their Android shelf, for the
owner also the public shelf. The owner gets it at `MAIL_TO`, a person at their Google account's address, else their
Apple ID; hence `sendmail` must deliver outside too. A failed mail is retried the next night. To turn it off —
Settings → "Письма" (Mail), with a button per person signed in to the browser.

## New shelf token

Each Apple ID has its own random archive directory token, which is also part of install links and manifests. If a
shelf's links leaked, give it a new token (the directory is renamed, manifests are fixed, old links stop working,
installed apps stay):

    sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf rotate-token --email <Apple ID>'

Android shelves likewise: `rotate-token --email <Apple ID> --android` — the shelf of that Apple ID's person,
`rotate-token --user-id <person number> --android` — a personal shelf, including a Google-only person's shelf;
the number is the "№ …" on the person's card on the "Участники" (People) page. `--user-id` requires a positive
number and `--android`, and cannot be combined with `--email` or `--public`. `rotate-token --public` selects the
public shelf (changing its PIN rotates it too). The directory is renamed, APK links on the page become new, old ones
stop working.

If the command reports "перенос не завершён" (move not finished: share failure, Ctrl+C), run it again: it finishes
the move with the same new token. An unfinished move is also finished by a download, unpublishing, pinning and the
start of `appshelf-web`; if both directories exist on the share, missing versions move into the new one.

`PUB_TOKEN` names only the directory of the shelf carried over from the first version; after `rotate-token` on that
shelf no shelf needs it, but keep it in `appshelf.env`: the service does not start without it (32 hex). Directories
that version 2 derived as an HMAC of `PUB_TOKEN` are moved to random tokens by `appshelf-web` on start.

## Integrity check

    sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf check'

Read-only: the database is opened without writes, nothing in the archive is changed or removed. For every version in
the database it checks the directory, `app.ipa` of the recorded size, `icon.png`, that `manifest.plist` parses and
its links point at this shelf; `--deep` also compares the IPA's SHA-256 (saved on download) and the itemId, external
version id and bundle id inside the IPA — it reads the whole archive. Separately: directories without database rows,
`.partial` (normal during a download), an unfinished shelf move, archive directories that belong to no Apple ID.
Android shelves likewise: the version directory, an APK of the recorded size, `icon.png`, and with `--deep` the APK's
SHA-256. Apple IDs and Android shelves are reported by number, shelf tokens as `<токен>` or their first four
characters. `--json` gives a
machine-readable report. Exit code: 0 — no problems, 1 — problems found, 2 — the check did not run (archive not
mounted, no database).

On the server the report ends with lines about the `bin/ipatool` and `bin/ipatool-auth` builds: whether they were
built from the deployed sources, the build date, "older than 90 days" (rebuild after an openssl DSA or once a quarter,
[ipatool/README.md](ipatool/README.md)); in `--json` — the `"ipatool"` key. They never change the exit code.

The nightly check runs the same check too (without `--deep`, removing nothing): the owner's night result has an
"Целостность: …" (Integrity) part, and problems send the mail "appshelf: проверка целостности нашла проблемы",
once per episode.

## Monitoring

- `/healthz` — 200 without sign-in.
- `status/nightly.stamp` — written at the end of every nightly check, with errors too (those are in the night's
  result and in mail): a trigger "not updated for more than 26 h" means the check did not run at all.
- `status/backup.stamp` — after a successful nightly database snapshot (when `APPSHELF_BACKUP_DIR` is set): a
  trigger on age over 26 h means no snapshot was taken; there is no other signal of an `appshelf-backup` failure.

`status/` is `0711`: monitoring sees a file's modification time by name without listing the directory.

## Backups

Nightly database snapshot — `appshelf-backup.timer` (05:30, after the nightly check): `appshelf backup --daily` puts
`appshelf-<date>.db` into `APPSHELF_BACKUP_DIR` and keeps the last 14. The directory should not be on the server's
disk (for example, on the share next to the archive) and is created once in advance as `appshelf`; without an
`APPSHELF_BACKUP_DIR` line in `appshelf.env`, `install.sh` does not enable the timer. The snapshot is made locally,
passes `PRAGMA quick_check` (a damaged one never rotates older ones away) and lands in the directory as a finished
file; a successful nightly one writes `status/backup.stamp` (see "Monitoring"). A snapshot directory inside the
archive is refused: Apache serves the archive under `/d/`, and a snapshot holds every shelf token. A snapshot is
taken from a database of any schema version, including one newer than the code after a rollback. File permissions
on a share are set by the mount: the snapshot is open to the same users as the archive (Apple ID addresses and names
are in the IPAs themselves too).

A file restore set is a database snapshot and an IPA/APK archive copy. Checking that pair confirms file consistency;
sign-ins and automatic updates need a separate check.

1. Database snapshot — with the command, not a file copy while running (WAL): consistent, a single file, mode 0600.
   The directory must be new or empty:

       sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf backup --out /backup/appshelf/2026-10-21'

   The snapshot holds the people, Apple IDs, shelf tokens and the list of versions with sizes and SHA-256.
2. The IPA and APK archive — a directory copy, right after the snapshot: `rsync -a <archive>/ /backup/appshelf/2026-10-21/pub/`.
3. Check the copy — the same integrity check, run against the snapshot and the archive copy (the snapshot is opened
   read-only; no Apple calls, no mail):

       sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf check --db /backup/appshelf/2026-10-21/appshelf.db --archive /backup/appshelf/2026-10-21/pub --deep'

   Exit code 0 means the database and files are consistent. Missing files or directories mean the nightly run replaced a version or
   `rotate-token` ran between the snapshot and the copy: take the set again. A snapshot from another schema version
   is refused — check it with the appshelf version that took it.

4. Save `/etc/appshelf/appshelf.env`, the `APPSHELF_SOURCES_CA` file (if set; without it RuStore downloads fail)
   and `/var/lib/appshelf/cookie-key` separately in protected storage, outside the shared application archive and
   its network share. Inspect the isolated set with a read-only report; pass the directory containing the protected
   copies of `appshelf.env` and the certificate file (side by side, under the same name) and the separate
   `cookie-key` file (exactly 32 bytes — the service does not start with any other):

       appshelf restore-report --db /backup/appshelf/2026-10-21/appshelf.db --archive /backup/appshelf/2026-10-21/pub --secrets-dir /protected/appshelf --cookie-key /protected/appshelf/cookie-key --deep

   The JSON includes code/schema versions, archive consistency, protected-file presence without reading their
   contents, and manual recovery steps. Exit code 0 / `offline_ok: true` means those checks passed;
   code 1 means missing or inconsistent parts, and 2 means the report could not run. `external_connections_verified: false`
   always remains: the command contacts no Apple, Google or 4PDA services, sends no mail and changes nothing.

The connection policy after recovery is **reconnect** (`connections_policy: reconnect`). Provider credentials
are excluded from the shared archive: sign into each Apple ID again (ipatool accounts are tied to `machine-id`),
reconnect personal Google Play accounts, and reimport a personal 4PDA session when needed.
Verify sign-ins, downloads and nightly updates separately afterwards. The database, archive and protected
settings are all needed; saved application files do not prove that external connections work.

## Development

    python -m venv .venv && .venv/bin/pip install -e ".[test]"   # Windows: .venv\Scripts\pip
    .venv/bin/python -m pytest -q                                 # fast suite, no network
    .venv/bin/python -m pytest -q -m integration                  # live checks on the server over SSH (.env)

Local integration suites without external network (browser, JDK and apksigner, APK upload resume) run per file —
commands in [CLAUDE.md](CLAUDE.md) (Russian), section "Команды".

GitHub Actions runs the fast suite on Linux too (`.github/workflows/tests.yml`): flock, process groups and file
modes behave as on the server, which Windows cannot check. Editing and updating the ipatool-cpp patches:
[ipatool/README.md](ipatool/README.md) (Russian).

## License

[MIT](LICENSE). ipatool-cpp, which the patches in `ipatool/patches/` apply to, is MIT as well.
