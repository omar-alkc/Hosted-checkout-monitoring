# RHEL 9 deployment (rootless Podman)

Production deployment for **Red Hat Enterprise Linux 9** using **rootless Podman** and the existing [`docker-compose.yml`](../../docker-compose.yml). Podman is natively integrated with RHEL and does not require Docker.

## Architecture

- **db**: `postgres:16-alpine` on host loopback `127.0.0.1:15433`
- **web**: image built from [`Dockerfile`](../../Dockerfile) on host loopback `127.0.0.1:8000`
- **Optional**: Nginx on the host terminates TLS and proxies to `127.0.0.1:8000`

Both services bind to loopback only — no firewalld rules are needed for ports 8000/15433. Open **443** (and optionally **80**) only when Nginx serves public HTTPS.

## Prerequisites

```bash
sudo dnf install -y podman podman-compose git curl
```

Rootless Podman on RHEL 9 uses preconfigured `subuid`/`subgid` mappings. Ports **8000** and **15433** are unprivileged; no `setcap` or sysctl changes are required.

Enable user systemd units to survive logout and reboot:

```bash
loginctl enable-linger "$USER"
```

## Quick start

```bash
git clone <repo-url> ~/aml-web
cd ~/aml-web
cp .env.example .env
# Edit .env: SESSION_SECRET (32+ chars), POSTGRES_PASSWORD, ENV=production
chmod +x deploy/rhel9/setup.sh
./deploy/rhel9/setup.sh
```

Or manually:

```bash
podman compose pull db
podman compose up --build -d
podman compose ps
curl http://127.0.0.1:8000/health
```

Create the first admin user:

```bash
podman exec -it card_cashin_web python -m app.scripts.create_admin myuser
```

## Production `.env` checklist

| Variable | Production |
|----------|------------|
| `SESSION_SECRET` | 32+ random characters (required) |
| `ENV` | `production` |
| `ALLOW_INSECURE_DEV` | **Unset** or `false` |
| `POSTGRES_PASSWORD` | Strong password (not `postgres`) |
| `SECURE_COOKIES` | `true` when behind HTTPS |
| `SESSION_SAME_SITE` | `strict` recommended |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` when Nginx is on the same host |

The **web** container receives `.env` via Compose `env_file`. Its internal `DATABASE_URL` is overridden to reach Postgres on the Compose network (`db:5432`).

## Day-to-day commands

| Task | Command |
|------|---------|
| Start stack | `podman compose up -d` |
| Rebuild after code change | `podman compose up --build -d` |
| Stop (keep data) | `podman compose down` |
| Status | `podman compose ps` |
| Logs | `podman compose logs -f web` |
| Shell in web container | `podman exec -it card_cashin_web sh` |

Docker Desktop equivalents on Windows use `docker` instead of `podman`.

## Systemd auto-start (user unit)

1. Edit [`aml-web.service`](aml-web.service) — set `WorkingDirectory` to your install path (default example: `/home/aml/aml-web`).
2. Install the unit:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/rhel9/aml-web.service ~/.config/systemd/user/
# Edit WorkingDirectory if needed
systemctl --user daemon-reload
systemctl --user enable --now aml-web.service
systemctl --user status aml-web.service
```

Ensure linger is enabled (`loginctl enable-linger "$USER"`) so the user service starts at boot without an interactive login.

## HTTPS with Nginx

See [`nginx-aml-web.conf.example`](nginx-aml-web.conf.example). After placing TLS certificates:

```bash
sudo cp deploy/rhel9/nginx-aml-web.conf.example /etc/nginx/conf.d/aml-web.conf
# Edit server_name and certificate paths
sudo nginx -t && sudo systemctl enable --now nginx
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
```

## Backup and restore

Database dump:

```bash
podman exec aml_web_postgres pg_dump -U postgres aml_web > aml_web_backup.sql
```

Volume location (rootless):

```bash
podman volume inspect aml_web_pgdata
```

Restore into a fresh volume: stop the stack, recreate the volume if needed, start `db` only, then `psql` or `pg_restore` as appropriate.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `podman compose` not found | Missing package | `sudo dnf install -y podman-compose` |
| Port already in use | Another service on 8000/15433 | `ss -tlnp \| grep -E '8000\|15433'` |
| Stack stops after logout | Linger disabled | `loginctl enable-linger "$USER"` |
| Web exits on start | Missing `SESSION_SECRET` in production | Set secret or `ALLOW_INSECURE_DEV=true` (dev only) |
| Password auth failed | Stale volume with old password | `podman compose down -v` then redeploy (destroys data) |
| Pull/build slow or blocked | Registry/network | Retry; configure corporate proxy if required |

Validate compose syntax without starting containers:

```bash
podman compose config
```

## Rootful Podman (appendix)

If your team runs Podman as root (`sudo podman`), use the same compose file but install the systemd unit under `/etc/systemd/system/` instead of the user unit, and run compose as root from the install directory. Rootless is the recommended RHEL default.

## Verification checklist

After deployment on RHEL 9:

1. `podman compose config` — no errors
2. `podman compose up --build -d` — both services healthy
3. `curl http://127.0.0.1:8000/health` → `{"ok":true}`
4. `ss -tlnp | grep -E '8000|15433'` — bound to `127.0.0.1` only
5. Reboot with systemd user unit enabled — stack auto-starts
