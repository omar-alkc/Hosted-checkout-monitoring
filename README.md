# Hosted Checkout Monitoring

Monitor **card-not-present (hosted checkout) cash-in** activity: import transaction Excel files, run **daily and weekly AML-style scenarios** (D1–D3, W1–W3), and triage **detections** in a web UI (status workflow, notes, exports).

The web app and optional desktop tool (`run.py`) share the same rule engine (`scenarios.py`, `io_utils.py`, `wallet_enrichment.py`).

---

## Quick start (Docker — recommended)

**Requirements:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows) or Podman Compose (RHEL 9).

1. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   For local dev, keep `ALLOW_INSECURE_DEV=true` in `.env` (or set a 32+ character `SESSION_SECRET`).

2. **Start the stack** (Postgres + web app)
   ```bash
   docker compose up --build -d    # Windows / Docker Desktop
   podman compose up --build -d    # RHEL 9
   ```

3. **Open the app** — use **http**, not https:
   - UI: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
   - Health: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) → `{"ok":true}`

4. **Create the first admin user**
   ```bash
   docker exec -it card_cashin_web python -m app.scripts.create_admin admin
   ```
   (Use `podman exec` on RHEL.) You will be prompted for a password.

5. **Log in** at `/login`, then create supervisor/investigator users from **Users** (`/admin/users`).

On startup the web container runs **Alembic migrations** automatically, then **Uvicorn** on port 8000.

| Command | Purpose |
|---------|---------|
| `docker compose ps` | Check services are **Up** (not Restarting) |
| `docker compose logs -f web` | View app logs |
| `docker compose down` | Stop containers (keeps DB data) |
| `docker compose down -v` | Stop and **delete** DB volume |

**Ports (loopback only):**

| Service | Host URL |
|---------|----------|
| Web UI | `127.0.0.1:8000` |
| Postgres | `127.0.0.1:15433` → database `aml_web` |

Do **not** run `start_app.cmd` at the same time — it also binds port 8000.

---

## Choose how to run

| Mode | When to use | How |
|------|-------------|-----|
| **Full Docker** | Simplest; matches production layout | `docker compose up --build -d` |
| **Hybrid (Windows)** | Postgres in Docker, app on host (Python 3.13) | `run_setup.cmd` then `start_app.cmd` |
| **Hybrid (Linux/RHEL)** | Same as hybrid on Linux | `./run_setup_rhel.sh` then `./start_app.sh` |
| **Production (RHEL 9)** | Rootless Podman + Nginx TLS | [deploy/rhel9/README.md](deploy/rhel9/README.md) |

**Hybrid setup (summary):**

1. Copy `.env.example` → `.env` with `DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:15433/aml_web`
2. Start **only** the database: `docker compose up -d db`
3. Install deps: `pip install -r requirements.txt`
4. Migrate: `python -m alembic upgrade head`
5. Create admin: `python -m app.scripts.create_admin admin`
6. Run app: `start_app.cmd` (Windows) or `./start_app.sh`

---

## Web workflow (after login)

| Role | Main tasks |
|------|------------|
| **admin** | User management only |
| **supervisor** | Upload Excel imports, run scenarios, configure thresholds, export detections |
| **investigator** | Review detections, change status (within policy), add notes |

Typical supervisor flow:

1. **Imports** — upload `.xlsx` transaction file  
2. Open the batch → **Run scenarios** (daily / weekly / both)  
3. **Detections** — triage queue, filters, bulk status (supervisors)  
4. **Scenario Manager** — thresholds and rolling weekly runs  

Investigators work from **Detections** → open a case → review transactions/metrics → notes → change status.

---

## Monitoring scenarios

| ID | Period | Pattern (summary) |
|----|--------|-------------------|
| D1 | Daily | Many cards → one wallet (volume) |
| D2 | Daily | One card → many wallets |
| D3 | Daily | Multiple failed / rejected transactions |
| W1 | Weekly | Many cards → one wallet (higher thresholds) |
| W2 | Weekly | One card → many wallets |
| W3 | Weekly | Multiple failed transactions |

Thresholds are configured per scenario in **Scenario Manager**. Optional **MariaDB/MySQL** enrichment (`MINITRANS_*` in `.env`) adds wallet names and risk data when available; scenarios still run without it.

---

## Excel input

**Required columns** (header names are normalized — case/spacing tolerant):

`RequestTimestamp`, `Mobile`, `Bin`, `AccountNumberLast4`, `Credit`, `ReasonCode`, `TransactionId`

**Web uploads** also require `UniqueId` per row (deduplication).

Upload size limit: `MAX_UPLOAD_BYTES` in `.env.example` (default ~25 MiB).

---

## Desktop tool (optional)

Offline Tkinter runner — no database, writes one workbook per scenario:

```bash
python run.py
```

Outputs: `Scenario_D1_daily.xlsx` … `Scenario_W3_weekly.xlsx` in the chosen folder. Threshold overrides: `scenarios.json`.

---

## Configuration

All environment variables are documented in [`.env.example`](.env.example). Common ones:

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL connection (Compose overrides this inside the web container) |
| `SESSION_SECRET` | Cookie signing — required in production (32+ chars) |
| `ALLOW_INSECURE_DEV` | Dev only — allows default secrets |
| `ENV=production` | Enables strict startup checks |
| `MINITRANS_*` | Optional MariaDB enrichment |
| `APP_TITLE` | Browser tab / header title |

**Schema:** managed by Alembic in `alembic/versions/` — never hand-edit production DB. Current head is applied with `alembic upgrade head` (automatic in Docker entrypoint).

---

## Development & tests

- **Python:** 3.13 (`pyproject.toml`, `.python-version`)
- **Install:** `pip install -r requirements-dev.txt`
- **Tests:** `python -m pytest tests/ -q`
- **Architecture / routes / models:** see [AGENTS.md](AGENTS.md) for contributors and AI agents

---

## Production security (checklist)

Before exposing beyond a trusted network:

1. Set `ENV=production` and a strong `SESSION_SECRET` — **remove** `ALLOW_INSECURE_DEV`
2. Terminate **HTTPS** at Nginx/Caddy ([example config](deploy/rhel9/nginx-aml-web.conf.example))
3. Set `SECURE_COOKIES=true`; rotate `POSTGRES_PASSWORD`
4. Keep web and DB on loopback; expose only the reverse proxy

Full RHEL runbook: [deploy/rhel9/README.md](deploy/rhel9/README.md)

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Browser **Error -102** / connection refused on `:8000` | Web container not running | `docker compose ps` — if **Restarting**, run `docker compose logs web` |
| `exec /docker-entrypoint.sh: no such file or directory` | Windows CRLF in shell script | Pull latest repo; rebuild: `docker compose up --build -d` |
| `relation "users" does not exist` | Migrations not applied | `docker exec card_cashin_web python -m alembic upgrade head` (or re-run hybrid `alembic upgrade head`) |
| Login works but no detections | No imports/scenarios yet | Log in as supervisor → **Imports** → upload Excel → run scenarios |
| Port 8000 conflict | Docker + `start_app.cmd` both running | Use one mode only |

---

## Sample test data

Synthetic Excel for imports and scenario testing (no real PII):

```bash
python scripts/generate_synthetic_import.py
```

Upload `sample_data/synthetic_transactions_demo.xlsx` as supervisor. Details: [sample_data/README.md](sample_data/README.md).

---

## Further reading

| Document | Contents |
|----------|----------|
| [AGENTS.md](AGENTS.md) | Code structure, routes, data model, conventions |
| [deploy/rhel9/README.md](deploy/rhel9/README.md) | Production Podman deployment |
| [.env.example](.env.example) | All environment variables |
| [AUDIT_SECURITY_FINDINGS.md](AUDIT_SECURITY_FINDINGS.md) | Security review notes |
| [AUDIT_PII_DATA_FLOW.md](AUDIT_PII_DATA_FLOW.md) | PII handling |
