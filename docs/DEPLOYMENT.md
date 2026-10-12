# Continuous deployment (GitHub Actions → your server)

```
push to main ──► CI runs ──────────────┐
                                       ├─► CD waits until CI is green on that exact commit
push to main ──► CD starts ────────────┘
                 │
                 ▼  ssh (key restricted to one script, host key pinned)
        scripts/deploy.sh on the server
          1. only a full commit hash that is on origin/main is accepted
          2. refuses a dirty working tree; one deploy at a time (flock)
          3. uv sync --frozen, then a fast test gate  ── fails here: nothing was restarted
          4. restart cemsim-freecad, then cemsim; health-check
          5. unhealthy? ── automatic rollback to the previous commit and exit 1
                 │
                 ▼
        public smoke test (/, /hmi, /api/snapshots)
```

Rollback by hand: **Actions → CD → Run workflow**, `ref` = an older commit on `main`
(or on the server: `scripts/deploy.sh rollback`, which toggles between the last two).

CI vs CD: CI (`ci.yml`, `codeql.yml`, `freecad-integration.yml`) decides *whether a commit is good*;
CD never re-runs those checks, it only refuses to ship a commit CI did not pass.

## What to add on GitHub (one time)

1. **Environment**: Settings → Environments → New environment → `production`.
   * *Required reviewers*: yourself/your team (a manual approval before every production deploy; remove
     it later if you want fully automatic deploys).
   * *Deployment branches*: only `main`.
2. **Secrets** (store them on the `production` *environment*, not repository-wide):

   | Name | Value |
   |---|---|
   | `DEPLOY_HOST` | server IP or DNS name, e.g. `169.58.162.158` |
   | `DEPLOY_USER` | the SSH user, e.g. `ubuntu` |
   | `DEPLOY_SSH_KEY` | the **private** deploy key (generated below) |
   | `DEPLOY_KNOWN_HOSTS` | the server's host key line(s), generated below |

3. **Variables** (environment `production`): `DEPLOY_URL` = `https://your.domain` (used for the smoke test and
   shown on the deployment), optional `DEPLOY_PORT` (default 22).
4. **Branch protection** on `main` (Settings → Branches): require pull requests and the CI checks. CD deploys
   whatever lands on `main`, so what protects production is what can reach `main`.
5. Nothing else: the workflow (`.github/workflows/cd.yml`) uses no third-party actions.

## What to do on the server (one time)

```bash
# 1. A deploy key that can run exactly one script (generate it anywhere, keep only the public half here)
ssh-keygen -t ed25519 -N '' -C cemsim-cd -f ./cemsim_cd          # paste ./cemsim_cd into DEPLOY_SSH_KEY, then delete it
echo "restrict,command=\"/home/ubuntu/SIM/cemsim-ots/scripts/deploy.sh\" $(cat ./cemsim_cd.pub)" \
  >> /home/ubuntu/.ssh/authorized_keys       # `restrict` = no shell, no forwarding, no pty

# 2. DEPLOY_KNOWN_HOSTS: take it from the server itself, not from the network
ssh-keyscan -t ed25519 169.58.162.158        # compare the fingerprint with: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub

# 3. Run the services as an unprivileged user (they currently run as root from a root-owned checkout)
sudo chown -R ubuntu:ubuntu /home/ubuntu/SIM/cemsim-ots
sudo install -d -m 0750 -o root -g ubuntu /etc/cemsim
python3 -c "import secrets; print(secrets.token_urlsafe(32))" | sudo tee /etc/cemsim/freecad_token >/dev/null
sudo chmod 0640 /etc/cemsim/freecad_token && sudo chgrp ubuntu /etc/cemsim/freecad_token
sudo tee /etc/cemsim/cemsim.env >/dev/null <<'ENV'
GEMINI_API_KEY=...
CEMSIM_PUBLIC_DEMO=1
ENV
sudo chmod 0640 /etc/cemsim/cemsim.env && sudo chgrp ubuntu /etc/cemsim/cemsim.env

# 4. systemd units (the app unit keeps port 8000, so Caddy / link_domain.sh need no change)
sudo cp deploy/systemd/cemsim-freecad.service deploy/systemd/cemsim.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cemsim-freecad cemsim

# 5. let the deploy user restart exactly these units (nothing else)
sudo tee /etc/sudoers.d/cemsim-deploy >/dev/null <<'SUDO'
ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart cemsim.service, /usr/bin/systemctl restart cemsim-freecad.service, \
                           /usr/bin/systemctl cat cemsim.service, /usr/bin/systemctl cat cemsim-freecad.service
SUDO
sudo visudo -cf /etc/sudoers.d/cemsim-deploy

# 6. a private repository needs read access for `git fetch` on the server (a read-only deploy key or token)
```

Check it by hand before trusting automation: `scripts/deploy.sh status`, then
`scripts/deploy.sh deploy $(git rev-parse origin/main)`.

## If you log in as `root` (this server's current setup)

The commands above assume an `ubuntu` deploy user. With root, the differences are:

* `DEPLOY_USER` = `root`; the key line goes in **`/root/.ssh/authorized_keys`** (same `restrict,command=...` prefix,
  same script path `/home/ubuntu/SIM/cemsim-ots/scripts/deploy.sh`).
* No sudoers rule (step 5) and no `chown` (step 3) are needed: `deploy.sh` runs `systemctl` directly as root.
* In `deploy/systemd/*.service` delete the `User=ubuntu` / `Group=ubuntu` lines to keep running as root like
  today (the token file may then be `0600 root`). Running the services unprivileged is the safer choice, but it
  is not required for CD to work.
* The restricted key still cannot open a shell, even though it belongs to root.

## What the safety net does and does not give you

* **Gives you**: no deploy of a commit CI rejected or that is off `main`; no shell for the deploy key; no
  overwritten local edits; a test gate before any restart; automatic rollback on a failed health check; one
  deploy at a time; a history in `.cemsim/deploy/history.log`.
* **Does not**: zero downtime (a restart takes a few seconds; the FreeCAD worker ~10 s to be ready), database
  migrations (there are none: the SQLite store creates its tables itself), or blue/green. The checkout is
  updated in place, so for the seconds between checkout and restart the old process serves new static files.
  If that matters, deploy into a new release directory and switch a symlink (a larger change).
* **Secrets** (`GEMINI_API_KEY`, the FreeCAD token) live only in `/etc/cemsim`, never in the repository, the
  workflow or the deploy script.

## Container alternative

`docker-compose.yml` + `deploy/*.Dockerfile` run the same system as two containers (the worker on an internal,
internet-less network). To deploy that way instead: build and push the two images to GHCR from a workflow, and
have the server run `docker compose pull && docker compose up -d` over the same restricted SSH key. The trade-off
is a 4.7 GB worker image and a registry, in exchange for reproducible environments.
