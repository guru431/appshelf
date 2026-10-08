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
  Several people can use it, and a person can have several Apple IDs: each Apple ID gets its own shelf; new
  Apple IDs join by the owner's invite link.
- Purchase history: you publish only what you pick. Apps removed from the App Store are added by an
  `apps.apple.com/…/id…` link or ID.
- A catalog of ~480 apps removed from the App Store (banks and their clones, VK, Mail.ru and more):
  appshelf checks which of them your Apple ID holds a license for.
- Catalog with an "Install" button; the current and the previous version are kept.
- Nightly check for new versions; the owner gets an email when their Apple ID session expires or disk space runs low.
- PWA: the shelf on the Home Screen.

## How it works

    iPhone (Safari) ─► Apache (HTTPS), vhost apps.example.com
                         ├─ /        → unix:/run/appshelf/web.sock (appshelf-web: Apple ID sign-in at /login)
                         ├─ /join/, /l/ → no sign-in (invite, fallback link)
                         ├─ /healthz → no password (monitoring)
                         ├─ /pwa/    → no password (PWA manifest, icon)
                         └─ /d/      → <archive>/ no password (manifests, IPAs, icons; path holds a secret token)
    appshelf-web (job worker) ─┐
    appshelf-nightly (04:30)   ┴─► bin/ipatool (HOME=/etc/appshelf/accounts/<id>, flock locks/<id>.lock) ─► Apple

IPAs are fetched by `bin/ipatool`, a fork of [ipatool-cpp](https://github.com/Sorvigolova/ipatool) with our
patches (`ipatool/`): the password is never stored and is passed only via stdin, a `list-purchases`
command, and exit codes for the wrapper. The web part is FastAPI and Jinja2, data lives in SQLite. Design
document (Russian): [docs/superpowers/specs/2026-10-05-appshelf-design.md](docs/superpowers/specs/2026-10-05-appshelf-design.md).
Sign-in and multiple Apple IDs: [docs/superpowers/specs/2026-10-08-multi-apple-id-design.md](docs/superpowers/specs/2026-10-08-multi-apple-id-design.md).

| What | Where |
|---|---|
| Code, `.venv`, `bin/ipatool` | `/var/_sh/appshelf` (hard-coded in `deploy/*.service` and the `deploy/` scripts) |
| Data | `/var/lib/appshelf`: `appshelf.db`, `tmp/`, `locks/`, `status/nightly.stamp`, `cookie-key` |
| IPA archive | `<archive>/<Apple ID token>/` (HMAC of `PUB_TOKEN`; the Apple ID carried over from the first version uses `<archive>/<PUB_TOKEN>/`); `<archive>` is `APPSHELF_PUB` or `/var/lib/appshelf/pub`. May be a network share; the database and `tmp/` stay local (SQLite on CIFS gets corrupted) |
| Settings | `/etc/appshelf/appshelf.env` (sample: `deploy/appshelf.env.example`), App Store tokens in `/etc/appshelf/accounts/<id>/.ipatool/` |
| Services | `appshelf-web.service`, `appshelf-nightly.service` + `.timer` |

## Requirements

- A Linux server (tested on Debian 13), Python ≥ 3.11 with `venv`.
- Apache 2.4 with `mod_ssl`, `mod_proxy`, `mod_proxy_http`, `mod_headers` and a domain with a valid HTTPS
  certificate: iOS installs over `itms-services` only from trusted HTTPS.
- Docker, only to build `bin/ipatool`; the host needs the `libunicorn2t64` package.
- Optional: a local `sendmail` (e.g. exim4) for emails.

## Installation

Run the server commands as a user with sudo.

1. User and directories:

       sudo useradd --system --home /var/_sh/appshelf --shell /usr/sbin/nologin appshelf
       sudo install -d -o appshelf -g appshelf -m 0755 /var/_sh/appshelf /var/_sh/appshelf/bin
       sudo install -d -o appshelf -g appshelf -m 0711 /var/lib/appshelf /var/lib/appshelf/pub /var/lib/appshelf/status
       sudo install -d -o appshelf -g appshelf -m 0700 /var/lib/appshelf/tmp
       sudo install -d -o root -g appshelf -m 0711 /etc/appshelf
       sudo install -d -o appshelf -g appshelf -m 0700 /etc/appshelf/accounts /var/lib/appshelf/locks

2. Code and dependencies. On your machine:

       tar -czf - pyproject.toml appshelf deploy ipatool | ssh user@server 'sudo tar -xzf - -C /var/_sh/appshelf && sudo chown -R appshelf:appshelf /var/_sh/appshelf'

   On the server:

       sudo -u appshelf python3 -m venv /var/_sh/appshelf/.venv
       sudo -u appshelf /var/_sh/appshelf/.venv/bin/pip install /var/_sh/appshelf

3. Settings: copy `deploy/appshelf.env.example` to `/etc/appshelf/appshelf.env` (`root:appshelf 0640`) and
   fill in `PUB_TOKEN` (`python3 -c "import secrets; print(secrets.token_hex(16))"`), `APPSHELF_PUBLIC_BASE`,
   `MAIL_TO`, `APPSHELF_OWNER` (your Apple ID).

4. `bin/ipatool`: `sudo bash /var/_sh/appshelf/deploy/build-ipatool.sh` → `OK: /var/_sh/appshelf/bin/ipatool`.

5. Services: on your machine create `.env` from `.env.example` and run `bash deploy/deploy.sh`, then on the
   server `sudo systemctl enable appshelf-web && sudo systemctl enable --now appshelf-nightly.timer`.

6. First sign-in: open the site and sign in with the Apple ID from `APPSHELF_OWNER` (password and 2FA code) —
   you become the owner. Invite others on the "Участники" (People) page.

7. Apache: a vhost based on `deploy/apache-appshelf.conf` (domain, certificate, archive path), then
   `sudo a2enmod ssl proxy proxy_http headers && sudo apache2ctl configtest && sudo systemctl reload apache2`.
   Check: `https://<domain>/healthz` → 200, `/` → sign-in form.

**Permissions.** `/var/lib/appshelf` and `status/` are `0711`: Apache and monitoring pass through without
listing. `/etc/appshelf` is `root:appshelf 0711`, so Apache cannot read `appshelf.env`. **Do not add
www-data to the `appshelf` group**: Apache reaches the socket through the `www-data` group on `/run/appshelf`
(`ExecStartPost` in the unit); otherwise an RCE in any PHP site on the server could read `PUB_TOKEN`
(`deploy.sh` checks this).

## Deploy and build

    bash deploy/deploy.sh                                          # code + restart; target is in .env (.env.example)
    ssh … 'sudo bash /var/_sh/appshelf/deploy/build-ipatool.sh'   # bin/ipatool (after changing ipatool/)

## Sign-in and people

You sign in to the panel with an Apple ID and its password, then a 2FA code. The password is not stored: ipatool
gets it via stdin, appshelf-web keeps it in memory for at most 10 minutes. The sign-in is remembered for a year;
"Sign out on all devices" (the "Apple ID" page) revokes all of the person's cookies.

A new Apple ID gets onto the server only through an invite link: "Участники" (People) → "Create link". The link
works until it is disabled — one for everyone or one per person; it is asked for once, on the first sign-in.
A person adds a second Apple ID of their own: "Apple ID" → "Add Apple ID". Each Apple ID has its own shelf:
history, catalog, IPAs (an IPA carries the buyer's data and installs on an iPhone signed in to the App Store with
the same Apple ID).

When a store token expires there is a banner on the shelf and "Sign in again"; the owner gets an email about
their Apple ID. If Apple breaks sign-in, use a fallback link (one-time, 24 h): "Участники" → "Sign-in link", or on
the server

    sudo -u appshelf bash -c 'set -a; . /etc/appshelf/appshelf.env; set +a; /var/_sh/appshelf/.venv/bin/appshelf login-link --email <Apple ID>'

To Apple all sign-ins come from the server: the client is Apple Configurator on a Mac, and the map in the sign-in
prompt shows the server's IP location. Each new Apple ID gets its own MAC (`IPATOOL_DEVICE_MAC`, patch 05) — a
separate "Mac" to Apple. The ipatool account file is encrypted with a key derived from `machine-id`: a file
created by another user or with another `HOME` cannot be read by the service.

## Removed apps

Apple's purchase history (DAAP, as used by Apple Configurator) does not include apps removed from the store,
even when you hold the license and the app downloads by id. "Refresh history" therefore also checks the
catalog `appshelf/data/removed_apps.json` against your Apple ID; the "Removed" page (`/blocked`) shows the
whole catalog and lets you add an app by link or ID. To extend the catalog, append `{"id", "name", "aliases"}`
to the JSON. Whether an ID is gone from the RU App Store: `itunes.apple.com/lookup?id=…&country=ru` (empty — removed).

## Rotating PUB_TOKEN

The directory tokens of all Apple IDs are derived from `PUB_TOKEN`, and install links and manifests contain them:
unpublish all apps on all shelves, put the new token into `appshelf.env`, restart `appshelf-web`, publish again;
remove the old token directories from the archive by hand.

## Development

    python -m venv .venv && .venv/bin/pip install -e ".[test]"   # Windows: .venv\Scripts\pip
    .venv/bin/python -m pytest -q                                 # fast suite, no network
    .venv/bin/python -m pytest -q -m integration                  # live bin/ipatool checks over SSH (.env)

Editing and updating the ipatool-cpp patches: [ipatool/README.md](ipatool/README.md) (Russian).

## License

[MIT](LICENSE). ipatool-cpp, which the patches in `ipatool/patches/` apply to, is MIT as well.
