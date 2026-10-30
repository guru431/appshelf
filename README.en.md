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
- Catalog with an "Install" button; the current and the previous version are kept, and one of them can be pinned to
  survive later updates. iOS does not install over an installed app — offload it first (see "Updating an installed
  app").
- Nightly check for new versions (versions downloaded this week are marked in the catalog: apps removed from the
  App Store get updates only from here); the owner gets an email when their Apple ID session expires or disk space
  runs low.
- PWA: the shelf on the Home Screen.
- Android: a section with each person's Android shelf — APKs from RuStore, Google Play, AppGallery, F-Droid, APKPure,
  from a direct link on a bank's site or an uploaded file; install and update from the phone's browser, nightly check
  for new versions, a signing rule (see "Android").
- A public Android shelf: the owner fills it, a guest opens it with a 6-digit PIN and can only install; brute-force
  protection.

## How it works

    iPhone (Safari) ─► Apache (HTTPS), vhost apps.example.com
                         ├─ /        → unix:/run/appshelf/web.sock (appshelf-web: Apple ID sign-in at /login)
                         ├─ /join/, /l/ → no sign-in (invite, fallback link)
                         ├─ /healthz → no password (monitoring)
                         ├─ /pwa/    → no password (PWA manifest, icon)
                         └─ /d/      → <archive>/ no password (manifests, IPAs, icons; path holds a secret token)
    appshelf-web (job worker) ─┐
    appshelf-nightly (04:30)   ┴─► bin/ipatool (HOME=/etc/appshelf/accounts/<id>, flock locks/<id>.lock) ─► Apple
                               └─► android/net.py (https and global addresses only) ─► RuStore, Google Play,
                                   AppGallery, F-Droid, APKPure, websites (APK)

IPAs are fetched by `bin/ipatool`, a fork of [ipatool-cpp](https://github.com/Sorvigolova/ipatool) with our
patches (`ipatool/`): the password is never stored and is passed only via stdin, a `list-purchases`
command, and exit codes for the wrapper. The web part is FastAPI and Jinja2, data lives in SQLite. Design
document (Russian): [docs/superpowers/specs/2026-10-05-appshelf-design.md](docs/superpowers/specs/2026-10-05-appshelf-design.md).
Sign-in and multiple Apple IDs: [docs/superpowers/specs/2026-10-08-multi-apple-id-design.md](docs/superpowers/specs/2026-10-08-multi-apple-id-design.md).

| What | Where |
|---|---|
| Code, `.venv`, `bin/ipatool` | `/var/_sh/appshelf`, owned by root: appshelf only reads and runs it (hard-coded in `deploy/*.service` and the `deploy/` scripts) |
| Data | `/var/lib/appshelf`: `appshelf.db`, `tmp/`, `locks/`, `status/nightly.stamp`, `cookie-key` |
| IPA archive | `<archive>/<shelf token>/`: each Apple ID has its own random token (`accounts.shelf` in the database; the Apple ID carried over from the first version uses `<archive>/<PUB_TOKEN>/`); `<archive>` is `APPSHELF_PUB` or `/var/lib/appshelf/pub`. May be a network share — then `APPSHELF_PUB` is a subdirectory inside it, not the mount point itself (an empty mount point is a directory too, and IPAs would land on the local disk). The database and `tmp/` stay local (SQLite on CIFS gets corrupted) |
| Settings | `/etc/appshelf/appshelf.env` (sample: `deploy/appshelf.env.example`), App Store tokens in `/etc/appshelf/accounts/<id>/.ipatool/` |
| Services | `appshelf-web.service`, `appshelf-nightly.service` + `.timer` |

## Requirements

- A Linux server (tested on Debian 13), Python ≥ 3.11 with `venv`.
- Apache 2.4 with `mod_ssl`, `mod_proxy`, `mod_proxy_http`, `mod_headers` and a domain with a valid HTTPS
  certificate: iOS installs over `itms-services` only from trusted HTTPS.
- Docker, only to build `bin/ipatool`: on the server or on a separate host (`APPSHELF_BUILD_SSH` in `.env`) with
  passwordless sudo; the server needs the `libunicorn2t64` package.
- Optional: a local `sendmail` (e.g. exim4) for emails.

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

5. `bin/ipatool`: on your machine run `bash deploy/build-ipatool.sh` → `OK: /var/_sh/appshelf/bin/ipatool`.

6. Apache: a vhost based on `deploy/apache-appshelf.conf` (domain, certificate, archive path), then
   `sudo a2enmod ssl proxy proxy_http headers && sudo apache2ctl configtest && sudo systemctl reload apache2`.
   Check: `https://<domain>/healthz` → 200, `/` → sign-in form.

7. First sign-in: open the site and sign in with the Apple ID from `APPSHELF_OWNER` (password and 2FA code) —
   you become the owner. Invite others on the "Участники" (People) page.

**Permissions.** The code, `.venv` and `bin/` belong to root (`deploy/install.sh` enforces it on every deploy):
otherwise any code execution as appshelf (an image from an IPA, ipatool) would become root at the next deploy or
build. The code group (`APPSHELF_CODE_GROUP` in `.env`) gets read access only, and appshelf is not in it. The
services see `/usr` and `/etc` read-only, except `/etc/appshelf/accounts` (`ProtectSystem=full`).
`/var/lib/appshelf` and `status/` are `0711`: Apache and monitoring pass through without
listing. `/etc/appshelf` is `root:appshelf 0711`, so Apache cannot read `appshelf.env`. **Do not add
www-data to the `appshelf` group**: Apache reaches the socket through the `www-data` group on `/run/appshelf`
(`ExecStartPost` in the unit); otherwise an RCE in any PHP site on the server could read `PUB_TOKEN`
(`deploy.sh` checks this).

## Deploy and build

    bash deploy/deploy.sh                                          # code + restart; target is in .env (.env.example)
    bash deploy/build-ipatool.sh                                   # bin/ipatool (after changing ipatool/ and deploying)

What ships is the committed `HEAD` (`git archive`): with uncommitted changes in the code `deploy.sh` refuses. The new
code replaces the old one as a whole and only after `pip` succeeds; to roll back, deploy the previous commit (across a
database schema change — only if that migration declared the schema readable by the older code, see `SCHEMA_VERSION`
in `store.py`). If
`bin/ipatool` was not built from the deployed patches, `deploy.sh` reminds you to rebuild; when to rebuild without
changes — [ipatool/README.md](ipatool/README.md). `build-ipatool.sh` builds the `ipatool/` deployed on the server
in a throwaway `debian:trixie` container on the `APPSHELF_BUILD_SSH` host (or on the server itself if unset) and
installs the binary on the server only if the server has all its libraries.

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
Apple ID menu in the header switches between everyone's shelves. This is how a shared Apple ID is used: it stays a
separate person, and signing in with it alone opens only its shelf. "Remove from this device" (the "Accounts" page)
removes one person from the browser, "Sign out" (the same menu) removes all; "Sign out on all devices" revokes one
person everywhere.

When a store token expires there is a banner on the shelf and "Sign in again"; the owner gets an email about
their Apple ID. If Apple breaks sign-in, use a fallback link (one-time, 24 h; it opens a page with a "Sign in"
button, so a messenger's link preview does not use it up): "Участники" → "Sign-in link", or on the server

    sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf login-link --email <Apple ID>'

To Apple all sign-ins come from the server: the client is Apple Configurator on a Mac, and the map in the sign-in
prompt shows the server's IP location. Each new Apple ID gets its own MAC (`IPATOOL_DEVICE_MAC`, patch 05) — a
separate "Mac" to Apple.

A console `auth login` is only good for testing the fork: the site does not learn about it, so jobs and the
nightly check stay idle until you sign in through the panel. If you need it anyway — only as `appshelf` and under
that Apple ID's lock (otherwise the service cannot read the account file or collides with a running download):

    sudo -u appshelf env HOME=/etc/appshelf/accounts/<id> IPATOOL_DEVICE_MAC=<accounts.device_mac, if not empty> flock /var/lib/appshelf/locks/<id>.lock /var/_sh/appshelf/bin/ipatool auth login -e <Apple ID>

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
signed-in people; the choice is in the section's header menu, next to "Публичная" (Public).

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
even when you hold the license and the app downloads by id. "Refresh history" therefore also checks the
catalog `appshelf/data/removed_apps.json` against your Apple ID. The button skips apps that had no license; the
nightly check re-checks them once a week, on the Apple ID's own weekday, and fills in versions then. The "Removed"
page (`/blocked`) shows the whole catalog and lets you add an app by link or ID. To extend the catalog, append
`{"id", "name", "aliases"}` to the JSON. Whether an ID is gone from the RU App Store:
`itunes.apple.com/lookup?id=…&country=ru` (empty — removed).

The catalog check takes minutes (~480 requests) and yields to publishing. While it runs, "Removed" and "History"
show "checked X of Y: found …, no license …, errors …" — the progress lives in the job (`jobs.progress`) and survives
a page reload and yielding. When it ends, "Removed" shows the result of the last check, and IDs that Apple answered
with a technical error are marked "check failed", separately from "not in account"; the next check asks them again.

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

"⋯" → "Check for update" asks Apple for the latest version of one app right away, without waiting for the night and
without checking the history and the catalog; repeated taps do not queue a second job, and without an Apple ID
sign-in it waits for one. If Apple refuses, the current version stays and installs. The card shows the result of the
last check (by the button or at night): "checked DD.MM HH:MM UTC — no newer version / new version downloaded /
failed: …"; this is about the shelf, not the version on the phone.

## Android

The same server also keeps Android shelves: the **Android** section of the panel, with an Apple/Android switch in
the header. From an Android phone the panel opens right in it, from an iPhone in Apple, from a computer in the
section opened last. An Android shelf belongs to a person, not to an Apple ID: an APK is not tied to any account.
Design (Russian): [docs/superpowers/specs/2026-10-28-android-design.md](docs/superpowers/specs/2026-10-28-android-design.md)
(stages 1–5 are done: RuStore, direct link, uploaded file, public shelf by PIN, Google sign-in and linking, Google
Play — free apps that come as one APK, AppGallery, F-Droid, APKPure).

- **Sources.** "Add" → RuStore search (a universal APK; the signature is checked against the one RuStore reports;
  paid apps are unavailable), Google Play — with a connected Google account (below), "By link" — an app page on
  Google Play, AppGallery (or its ID like `C102312137`), F-Droid, APKPure, RuStore (the source and package are taken
  from the address) or a direct `https` link to an APK on a bank's or developer's site, "Own file" — an upload of up
  to 1 GB. The server never follows a link into an internal network: the name is resolved before every request and
  after every redirect, and any non-global address (private, loopback, link-local, multicast) is refused before
  connecting.
- **Source limits.** One whole APK only, nothing paid except apps bought in Google Play. The terms of RuStore,
  AppGallery, APKPure and Google Play forbid automated downloads: requests go one at a time with a pause, the nightly
  check runs once a day; the server owner accepts the risk of losing access.
- **Installing.** "Download APK" in Chrome on the phone → "Open" on Chrome's bar or in the download notification →
  "Install"; the first time Android asks to allow installs from the browser. A site cannot install right away: the
  browser only downloads the file, and the person opens the installer. Updating is the same "Download APK" over the installed app, its data stays. A browser installs only one
  whole APK: bundles (split APK, XAPK, APKS) are refused when added. "Copy link" in the card's "⋯" gives a permanent address
  `<site address>/apk/<shelf token>/<package>.apk`: always the current version, opens without signing in (like the
  files themselves); changing the shelf token (`rotate-token`, a new PIN of the public shelf) disables it too.
- **Signature.** The first version sets the app's signature on the shelf. A new version signed with another
  certificate does not become current: the card shows "signature changed" and "Accept the new signature" — such a
  version will not install over the installed one, the app would have to be removed with its data. If the app came
  from Google Play with another signature, remove it first.
- **Versions** — as with IPAs: current, previous, pinned; "⋯" — check for update, pin, change source, upload a new
  version as a file, remove from the shelf. Layout:
  `<archive>/<Android shelf token>/<package>-<versionCode>/{<package>-<version>.apk, icon.png}`.
- **Nightly check** — after the Apple IDs, and without any Apple ID sign-in too: RuStore, Google Play, AppGallery and
  APKPure by `versionCode`, F-Droid by the suggested version, a link by `ETag`, `Last-Modified`, `Content-Length`;
  uploaded files are not checked. The night's result ends with
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

Hints for the phone are in the section's catalog: Samsung Auto Blocker, Huawei Pure Mode, Play Protect; from 2027
Google requires developer verification for installs outside stores too — apps of unverified developers (probably
including sanctioned banks) install only through the one-time "advanced flow" (developer mode, a day's wait).

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
- **One APK only.** Only what Play delivers as a single file goes on a shelf. Almost everything in Play is an App
  Bundle (split APKs): a browser cannot install that, the card says so and, if the package comes as one APK in
  RuStore (free), AppGallery or F-Droid, names them and suggests switching the source. A new version that arrives as a
  bundle leaves the previous one on the shelf, marked.
- **Purchased apps** go only to the personal shelf of the person whose account bought them (checked on the server):
  not to the public shelf, not to anyone else's. "My purchases" will come after a live check of purchases through a
  proxy in the account's country.
- **Proxies** — for the account's country: `APPSHELF_PROXIES` in `appshelf.env` (space-separated `name=url`,
  `http://host:port` or `socks5h://[user:password@]host:port`), chosen by the owner when connecting. A proxy is used
  only for Google Play requests; if it is down, the card shows an error — there is no direct fallback. From a Russian
  address Play shows prices in rubles and does not deliver paid apps.
- **Expired.** A changed Google password or revoked access means "подключение Google Play истекло": a banner in the
  Android section and on the accounts page, the account's jobs wait, its apps are skipped at night; mail goes only
  about the owner's Google accounts, one per episode. "Подключить заново" takes a new `oauth_token`.
- **Live test** — `pytest -m integration tests/test_android_play_live.py` with `APPSHELF_PLAY_TEST_GID=<id of a
  connected Google account>` in `.env`: `details` of a free package as `appshelf` on the server.

### AppGallery, F-Droid, APKPure

Added "By link" with the app's page (search is RuStore only); "⋯ → Change source" switches a card to any of them by
its package. The signature of every downloaded version is compared with RuStore.

- **AppGallery** — Huawei's client API without sign-in (`client.updateCheck` by package name: version, file size and
  SHA-256, APK link); an ID from a page (`appgallery.huawei.com/app/C…`) is turned into the package. The API host
  defaults to the Russian `store-drru.hispace.dbankcloud.ru`, another one goes to `APPSHELF_APPGALLERY_HOST` in
  `appshelf.env`. From a foreign address AppGallery answers for another zone with no banks — "AppGallery не нашёл
  приложение (зона …)". Huawei may re-sign an app with its own key: a signature other than RuStore's gives the card
  the mark "подписан не так, как в RuStore: не встанет поверх версии оттуда" (the app is still added).
- **F-Droid** — the official `f-droid.org` repository: the package API and APKs over HTTPS. The F-Droid index
  signature is **not verified** (the index is tens of MB of JSON): the first version on the shelf sets the app's
  signature, as with a link. One version may have several builds per ABI, and the suggested one is not always ARM
  (VLC's is x86_64): the shelf takes a universal or `arm64-v8a` build, otherwise `armeabi-v7a`, otherwise "в F-Droid
  нет сборки для ARM". There are no banks in F-Droid.
- **APKPure** — the APKPure client API without sign-in, single APKs only: an XAPK is refused as a bundle with a hint
  where the package comes as one APK. APKPure has distributed tampered APKs (2021 — a trojan in its client, 2025 —
  Telegram X with a backdoor), so: if the package is in RuStore the signature **must** match, otherwise "подпись не
  совпала с RuStore"; if not, it is added only with the "I understand" checkbox and the mark "подпись не сверена",
  then the general signature rule applies.

### Public shelf

One Android shelf per server for people without a panel sign-in. The owner fills it (Android → **Публичная** →
"Добавить в публичную": the same sources), people see it on the same tab without a PIN and without
actions, a guest opens `<site address>/p` and enters the PIN — and sees only the list with "Download APK" and the
hints, no menu and no panel.

- **PIN** — 6 digits, set and changed by the owner in the "Гостевой доступ" block of the public tab. Until a PIN is
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
  right away — "Открыть ввод PIN" on the public tab; if the PIN may have leaked, change it (that reopens entry too).
- File links, as with IPAs, work without the PIN: the PIN guards the page, not the files. Purchased apps never go to
  the public shelf.

## New shelf token

Each Apple ID has its own random archive directory token, which is also part of install links and manifests. If a
shelf's links leaked, give it a new token (the directory is renamed, manifests are fixed, old links stop working,
installed apps stay):

    sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf rotate-token --email <Apple ID>'

Android shelves likewise: `rotate-token --email <Apple ID> --android` — the shelf of that Apple ID's person,
`rotate-token --public` — the public one (changing the PIN rotates it too). The directory is renamed, APK links on the
page become new, old ones stop working.

`PUB_TOKEN` names only the directory of the shelf carried over from the first version; after `rotate-token` on that
shelf no shelf needs it. Directories that version 2 derived as an HMAC of `PUB_TOKEN` are moved to random tokens by
`appshelf-web` on start.

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

## Backups

Nightly database snapshot — `appshelf-backup.timer` (05:30, after the nightly check): `appshelf backup --daily` puts
`appshelf-<date>.db` into `APPSHELF_BACKUP_DIR` and keeps the last 14. The directory should not be on the server's
disk (for example, on the share next to the archive) and is created once in advance as `appshelf`; without an
`APPSHELF_BACKUP_DIR` line in `appshelf.env`, `install.sh` does not enable the timer. The snapshot is made locally
and lands in the directory as a finished file. File permissions on a share are set by the mount: the snapshot is
open to the same users as the archive (Apple ID addresses and names are in the IPAs themselves too).

A restore set is a database snapshot, a copy of the archive, and a check that the shelves can be restored from that
pair in full.

1. Database snapshot — with the command, not a file copy while running (WAL): consistent, a single file, mode 0600.
   The directory must be new or empty:

       sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf backup --out /backup/appshelf/2026-10-21'

   The snapshot holds the people, Apple IDs, shelf tokens and the list of versions with sizes and SHA-256.
2. The IPA archive — as a directory copy, right after the snapshot: `rsync -a <archive>/ /backup/appshelf/2026-10-21/pub/`.
3. Check the copy — the same integrity check, run against the snapshot and the archive copy (the snapshot is opened
   read-only; no Apple calls, no mail):

       sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf check --db /backup/appshelf/2026-10-21/appshelf.db --archive /backup/appshelf/2026-10-21/pub --deep'

   Exit code 0 means the set is good. Missing files or directories mean the nightly run replaced a version or
   `rotate-token` ran between the snapshot and the copy: take the set again. A snapshot from another schema version
   is refused — check it with the appshelf version that took it.

- `/etc/appshelf/appshelf.env` (`PUB_TOKEN`) and `/var/lib/appshelf/cookie-key` — separately, to a protected place:
  without them the archive directories and device sign-ins cannot be restored as they were.
- ipatool accounts (`/etc/appshelf/accounts`) are encrypted with a key derived from `machine-id` and cannot be read
  on another machine: after a move each Apple ID signs in again; the archive moves together with the database and
  `appshelf.env`.

## Development

    python -m venv .venv && .venv/bin/pip install -e ".[test]"   # Windows: .venv\Scripts\pip
    .venv/bin/python -m pytest -q                                 # fast suite, no network
    .venv/bin/python -m pytest -q -m integration                  # live bin/ipatool checks over SSH (.env)

GitHub Actions runs the fast suite on Linux too (`.github/workflows/tests.yml`): flock, process groups and file
modes behave as on the server, which Windows cannot check. Editing and updating the ipatool-cpp patches:
[ipatool/README.md](ipatool/README.md) (Russian).

## License

[MIT](LICENSE). ipatool-cpp, which the patches in `ipatool/patches/` apply to, is MIT as well.
