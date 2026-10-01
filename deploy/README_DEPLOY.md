# Deploying AS4PUR

Target: Ubuntu Server 22.04, on-prem (CLAUDE.md Section 2).

## 1. Dedicated non-root user (CLAUDE.md Section 5)

```
sudo useradd --system --home /opt/as4pur --shell /usr/sbin/nologin as4pur
sudo mkdir -p /opt/as4pur
sudo chown as4pur:as4pur /opt/as4pur
```

## 2. Application code + virtualenv

The code is cloned straight into `/opt/as4pur` (the unit's `WorkingDirectory`),
and `git clone` needs that directory to be empty - which is why `data/` is
created after the clone, not in step 1.

```
sudo -u as4pur git clone <repo-url> /opt/as4pur
cd /opt/as4pur
sudo -u as4pur mkdir -p data    # database + temporary uploads (git-ignored, must persist)
sudo -u as4pur python3 -m venv .venv    # needs Python 3.11 or 3.12, see the note below
sudo -u as4pur .venv/bin/pip install -r requirements.txt
```

**Python version:** `requirements.txt` pins exact versions, and one of them (`websockets` 17.1) needs
Python 3.11 or newer. Ubuntu 22.04's default `python3` is 3.10, so the install above fails there with a
"no matching distribution" error until you create the venv with a newer interpreter
(`python3.11 -m venv .venv`, or 3.12); Ubuntu 24.04's default `python3` is 3.12. See the README,
"Requirements".

## 3. Configuration

```
sudo -u as4pur cp .env.example /opt/as4pur/.env
sudo chmod 600 /opt/as4pur/.env
```

Edit `/opt/as4pur/.env` - at minimum confirm `AS4PUR_SECURE_COOKIES=true` and set
`AS4PUR_INVITE_ALLOWED_DOMAINS` to the email domain(s) you will invite (comma-separated;
while it is empty every invite is refused). Leave `AS4PUR_ENABLE_API_DOCS=0` on a server.

`.env` doesn't currently hold a long-lived secret key (session/CSRF tokens are `secrets.token_urlsafe`-generated and stored server-side, not signed into the cookie), but it's still config specific to this deployment, worth keeping narrower than the whole-tree `chown` above - `chmod 600` restricts it to the `as4pur` user only, no group/other read access, rather than relying solely on the directory-level ownership already set in step 1.

## 4. Database migrations (Alembic)

Introduced 2026-09-08 (see CLAUDE.md Section 9's "Add users to an existing
rule" stale-user entry's neighbor and `alembic/versions/`) - schema changes
from this point forward are tracked migrations, not
`Base.metadata.create_all()` side effects.

**Updated 2026-09-09** (a real drift incident - `create_all()` silently
created a new model's table without Alembic ever recording it, then a
later `alembic upgrade head` crashed on "table already exists" - see
CLAUDE.md's Migrations section): `init_db()` (run automatically on every
app startup) now only calls `create_all()` when the database is
genuinely empty - no tables at all, including no `alembic_version` - a
true first-ever setup. The moment ANY schema exists, `init_db()` never
touches DDL again; it only compares the database's recorded Alembic
revision against the latest migration on disk and logs a warning on any
mismatch (check `journalctl -u as4pur` for "Database schema may be out of
date" after a deploy - it's the app itself telling you a migration is
needed, not a game to auto-heal). From here on, run migrations explicitly
as part of every deploy/update:

```
sudo -u as4pur /opt/as4pur/.venv/bin/python -m alembic upgrade head
```

**One-time step for an already-running deployment** (a database that
existed before Alembic was introduced, so it has real tables but no
migration history yet): mark it as already at the baseline WITHOUT
re-running the baseline's `CREATE TABLE` statements against tables that
already exist (`stamp`, not `upgrade` - `stamp` only writes Alembic's own
bookkeeping row, it never touches application data or schema):

```
sudo -u as4pur /opt/as4pur/.venv/bin/python -m alembic stamp head
```

Do this exactly once, before the first `alembic upgrade head` that
carries a real schema change. Running `upgrade head` on a database that
was never stamped (and already has these tables) will fail with a "table
already exists" error - that failure is expected and safe to hit; it
means `stamp head` was skipped, not that data was damaged.

## 5. First user

```
sudo -u as4pur /opt/as4pur/.venv/bin/python scripts/create_user.py <username> admin
```

The first account should be an `admin`: an admin can then invite everyone else
from `/admin` (there is no self-registration and no default account).

## 6. TLS certificate (self-signed or an internal CA is fine - CLAUDE.md Section 5)

```
sudo mkdir -p /etc/ssl/as4pur
sudo openssl req -x509 -nodes -newkey rsa:2048 \
  -keyout /etc/ssl/as4pur/privkey.pem \
  -out /etc/ssl/as4pur/fullchain.pem \
  -days 825 -subj "/CN=as4pur.internal"
```

## 7. systemd + Nginx

```
sudo cp deploy/as4pur.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now as4pur

sudo cp deploy/nginx_as4pur.conf /etc/nginx/sites-available/as4pur
sudo ln -s /etc/nginx/sites-available/as4pur /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 8. Host hardening (2026-09-13)

Netskope ZTNA is the primary access boundary for this app (CLAUDE.md
Section 2), but - same principle as requiring real login on top of that
boundary (Section 5) - the host itself shouldn't rely on it being the
*only* layer.

### Firewall baseline (ufw)

```
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow from <ADMIN_SSH_RANGE> to any port 22 proto tcp
sudo ufw enable
```

**`<ADMIN_SSH_RANGE>` is a placeholder, not a real value** - fill in the
actual admin/jump-host CIDR or IP before running this (e.g.
`203.0.113.10/32` for one address, `203.0.113.0/24` for a range). Nothing
elsewhere in this repo documents what that range should be, and it isn't
this document's call to invent one. Get it wrong (too narrow) and you can
lock out legitimate admin access to a host with no other way in; get it
wrong (too broad, e.g. `0.0.0.0/0`) and this rule does nothing.

### Log rotation

Nginx's access/error logs and the app's own output need rotation - `logrotate`
ships on Ubuntu 22.04 by default, and Nginx's own package already installs a
working `/etc/logrotate.d/nginx`; check it's present rather than assuming:

```
cat /etc/logrotate.d/nginx   # confirm this exists (installed by the nginx package)
```

The app's own stdout/stderr goes to the systemd journal (`Type=simple`,
no explicit log file in `as4pur.service`), which `journald` already
rotates/caps on its own (see `journalctl --disk-usage`, and
`/etc/systemd/journald.conf`'s `SystemMaxUse=` if the default cap needs
adjusting) - no separate `logrotate` entry is needed for it unless a
future change starts writing the app's own log file to disk directly.

### OS patch policy

```
sudo apt install unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

Recommended enabled, not a reason to control updates manually - this is a
small, low-traffic internal tool with `Restart=on-failure` already in
place, not a service where an unplanned restart from a kernel/security
update is meaningfully more disruptive than staying unpatched. If that
calculus ever changes (e.g. a maintenance-window requirement gets
introduced), revisit this rather than leaving the host silently
unpatched by default.

## Important: never scale to more than one worker process

The background-job design (CLAUDE.md Section 3, Section 11) passes the
operator's Netskope API token straight into an in-process background
thread - never into a queue, a database row, or any other cross-process
channel. This only works if the process that accepts the request is the
same process that runs the job and answers its status polls. Do not add
`--workers N>1` to the `ExecStart` line in `as4pur.service`, and do not
run this behind a multi-process Gunicorn setup.

`as4pur.service` pins `Environment=WEB_CONCURRENCY=1`, so an inherited value cannot start extra
workers; keep `WEB_CONCURRENCY` out of `.env` too. If it is set to anything but 1 the app's workers
refuse to start, but uvicorn's supervisor respawns them, so the unit still shows `active (running)`
while serving nothing: look in `journalctl -u as4pur` for repeated `AS4PUR refuses to start` and
`Child process [...] died` lines. The check does not see `--workers N` on the command line,
`UVICORN_WORKERS` or `gunicorn -w N`: those are on you. If AS4PUR ever needs to
scale beyond what a single process can handle, that's a real design
change to Section 3's token-handling model, not a config flag - revisit
`app/jobs/manager.py`'s docstring first.

## Retention (CLAUDE.md Section 2)

No cleanup is needed on day one. When it's time to add one, `Job`,
`JobItem`, and `AuditLog` all carry indexed timestamp columns already, so
an archival job (e.g. "export + delete audit rows older than 90 days")
can be added as a straightforward scheduled task without a schema
redesign.
