# Hosted Checkout monitoring system

## What this is

This project is a **card cash-in monitoring** stack for **Visa / Mastercard**-style card activity: it ingests **card-not-present (hosted checkout) transactions** (card identified from **BIN + last four digits** and linked to a **mobile wallet**), then applies **AML-style monitoring scenarios** to surface unusual **load (credit) cash-in** patterns.

**Aim:** help investigators spot wallets and cards that merit review—either from **sheer volume** (many card transactions over a **day or week**) or **relationship complexity**—for example **many distinct cards funneling into one wallet**, **one physical card interacting with multiple wallets**, or elevated **failed / non-approved** payment attempts—before tying results into triage and case workflow in the web UI.

Monitoring is organised into **daily** and **weekly** scenarios (exported as scenario-specific Excel outputs from the desktop tool; the same rule engine backs the hosted web workflow). Thresholds and detection state can be adjusted in the application depending on operating model and risk appetite.

---

## Repository layout

The **AML web UI** (FastAPI) and an optional **desktop script** share one codebase. The web app lives under `app/`; scenario rules and Excel helpers at the repo root (`run.py`, `scenarios.py`, `io_utils.py`, `wallet_enrichment.py`) are imported by the web layer so behaviour stays consistent.

---

## System architecture

At a high level the stack is **browser clients → FastAPI → PostgreSQL**, with **optional MySQL/MariaDB** used only for enrichment lookups during scenario runs (wallet profiles and transaction history).

**Web application**

| Piece | Role |
|-------|------|
| **`app.main`** | FastAPI app: session middleware (signed cookie sessions), static files under `/static`, routers mounted without an API prefix for HTML routes. |
| **`app.routers`** | **`auth_routes`** (login, logout, password change), **`admin_routes`** (users), **`policy_routes`** (supervisor investigator policy), **`web`** (detections, imports, scenarios, transactions explorer, notes/HTMX). |
| **`app.database` / SQLAlchemy** | Primary persistence: users, import batches, stored transaction rows (JSONB), detections, scenario/threshold config, notes, workflow policy, etc. |
| **`app.services`** | Domain logic: imports, scenario execution (`scenario_run`), exports, enrichment retry, detections CRUD. |
| **Root modules** | **`io_utils`** (Excel ingest, optional MariaDB reads), **`scenarios`**, **`wallet_enrichment`** — used by the web app’s import and scenario pipeline. |

**Deployment (Docker Compose)**

- **`db`**: Postgres (application database).
- **`web`**: Image built from **`Dockerfile`** — installs deps, copies `app/` and root helpers, runs **`alembic upgrade head`** then **`uvicorn app.main:app`** (`docker-entrypoint.sh`).

```mermaid
flowchart TB
  subgraph clients [Clients]
    Browser["Browser"]
  end
  subgraph webtier [Web tier]
    API["FastAPI app.main"]
    Mods["scenarios.py · io_utils.py · wallet_enrichment.py"]
  end
  PG[("PostgreSQL")]
  MQ[("MariaDB / MySQL optional")]
  Browser --> API
  API --> Mods
  API --> PG
  Mods --> PG
  Mods -.->|"MINITRANS_* if configured"| MQ
```

---

## Inputs and outputs (I/O)

**Inputs**

| Source | What |
|--------|------|
| **Excel `.xlsx`** | Transaction feeds: validated by **`io_utils.read_transactions_xlsx`** (normalized headers; required columns such as `RequestTimestamp`, `Mobile`, `Bin`, `AccountNumberLast4`, `Credit`, `ReasonCode`, `TransactionId`). **Web uploads** also require **`UniqueId`** per row (deduplication vs existing data). Upload size is capped by **`MAX_UPLOAD_BYTES`** (see `.env.example`). |
| **Environment / `.env`** | **`DATABASE_URL`** (Postgres), **`SESSION_SECRET`**, optional **`SESSION_*`** timeouts, **`MINITRANS_HOST` / `MINITRANS_PORT` / `MINITRANS_USER` / `MINITRANS_PASSWORD` / `MINITRANS_DATABASE`** for enrichment, **`GOV_MAPPING_PATH`** optional Excel for city code → display name mapping. |
| **HTTP** | Forms, filters, session cookie: HTML UI and supervisor export query parameters. |

**Outputs**

| Destination | What |
|-------------|------|
| **Browser** | HTML pages (Jinja2), JSON for a few endpoints, **`/static`** assets, file download responses. |
| **PostgreSQL** | Normalized application state after import and scenario runs (detections, metrics, statuses, notes, etc.). |
| **Excel downloads** | Supervisors can export filtered detections (**`/detections/export`**, built in **`app/services/detections_export.py`**). |
| **Desktop folder** | When using **`run.py`**, one workbook per scenario (e.g. `Scenario_D1_daily.xlsx` … `Scenario_W3_weekly.xlsx`) in the chosen output directory. |
| **Optional MariaDB/MySQL** | Read-only queries for enrichment (e.g. wallet names, `minitrans_clone` patterns) when configured; if unavailable, scenarios can still run with reduced enrichment (see logs). |

---

## Database DDL (PostgreSQL)

**Canonical source:** schema changes live in **`alembic/versions/`**. On deploy, run **`alembic upgrade head`** (the Docker entrypoint does this before Uvicorn). The SQL below is a **reference snapshot** of the application tables after revision **`007_d1_d2_high_risk_thresholds`**—use it for documentation or manual review; avoid applying it over an existing database that is already migrated.

**Seeds (not fully shown below):**

- **`001_initial`** inserts the first **`scenario_config`** row (default thresholds).
- **`004_users_policy`** inserts **`investigator_status_policy`** row **`id = 1`** with a default **`allowed_map`**.

ORM mapping: **`app/models.py`**.

```sql
-- === import_batches =========================================================
CREATE TABLE import_batches (
    id SERIAL PRIMARY KEY,
    original_filename VARCHAR(512) NOT NULL,
    status VARCHAR(32) NOT NULL,
    row_count INTEGER NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- === scenario_config (singleton row seeded by migration 001) ===============
CREATE TABLE scenario_config (
    id SERIAL PRIMARY KEY,
    d_amount_min NUMERIC(24, 6) NOT NULL,
    d_total_amount_min NUMERIC(24, 6) NOT NULL,
    d1_min_txn INTEGER NOT NULL,
    d1_min_unique_cards INTEGER NOT NULL,
    d2_min_wallets INTEGER NOT NULL,
    d3_min_rejected INTEGER NOT NULL,
    w1_min_txn INTEGER NOT NULL,
    w1_min_unique_cards INTEGER NOT NULL,
    w1_min_total_amount NUMERIC(24, 6) NOT NULL,
    w2_min_wallets INTEGER NOT NULL,
    w2_min_total_amount NUMERIC(24, 6) NOT NULL,
    w3_min_rejected INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    monitored_banks JSONB NOT NULL DEFAULT '{}'::jsonb,
    w2_min_txn INTEGER NOT NULL,
    scenario_enabled JSONB NOT NULL,
    d1_risk_min_total_amount NUMERIC(24, 6) NOT NULL,
    d1_risk_min_expenditure_pct NUMERIC(24, 6) NOT NULL,
    d2_risk_min_total_amount NUMERIC(24, 6) NOT NULL,
    d2_risk_min_wallet_expenditure_pct NUMERIC(24, 6) NOT NULL,
    d2_risk_min_wallets_pct NUMERIC(24, 6) NOT NULL
);

-- === transaction_rows =======================================================
CREATE TABLE transaction_rows (
    id SERIAL PRIMARY KEY,
    import_batch_id INTEGER NOT NULL REFERENCES import_batches (id) ON DELETE CASCADE,
    row_index INTEGER NOT NULL,
    payload JSONB NOT NULL,
    transaction_external_id VARCHAR(256)
);

CREATE INDEX ix_transaction_rows_import_batch_id ON transaction_rows (import_batch_id);

CREATE UNIQUE INDEX uq_transaction_rows_transaction_external_id
    ON transaction_rows (transaction_external_id)
    WHERE transaction_external_id IS NOT NULL;

-- === detections =============================================================
CREATE TABLE detections (
    id SERIAL PRIMARY KEY,
    import_batch_id INTEGER NOT NULL REFERENCES import_batches (id) ON DELETE CASCADE,
    scenario_id VARCHAR(8) NOT NULL,
    period VARCHAR(16) NOT NULL,
    status VARCHAR(64) NOT NULL,
    assigned_senior VARCHAR(256),
    metrics JSONB NOT NULL,
    raw_row_indices JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_detections_import_batch_id ON detections (import_batch_id);
CREATE INDEX ix_detections_scenario_id ON detections (scenario_id);
CREATE INDEX ix_detections_status ON detections (status);

-- === notes ==================================================================
CREATE TABLE notes (
    id SERIAL PRIMARY KEY,
    detection_id INTEGER NOT NULL REFERENCES detections (id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    author_name VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_notes_detection_id ON notes (detection_id);

-- === status_history =========================================================
CREATE TABLE status_history (
    id SERIAL PRIMARY KEY,
    detection_id INTEGER NOT NULL REFERENCES detections (id) ON DELETE CASCADE,
    from_status VARCHAR(64),
    to_status VARCHAR(64) NOT NULL,
    actor_name VARCHAR(256) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_status_history_detection_id ON status_history (detection_id);

-- === users ==================================================================
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(128) NOT NULL,
    password_hash VARCHAR(256) NOT NULL,
    display_name VARCHAR(256) NOT NULL DEFAULT '',
    role VARCHAR(32) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_username UNIQUE (username)
);

CREATE INDEX ix_users_username ON users (username);
CREATE INDEX ix_users_role ON users (role);

-- === investigator_status_policy (singleton id = 1, seeded in migration 004) =
CREATE TABLE investigator_status_policy (
    id INTEGER NOT NULL,
    allowed_map JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id),
    CONSTRAINT ck_investigator_status_policy_singleton CHECK (id = 1)
);
```

---

## Run with Docker (recommended for the web UI)

Requires [Docker Compose](https://docs.docker.com/compose/) v2.

1. Copy **`.env.example`** to **`.env`** and edit secrets / optional **`MINITRANS_*`** enrichment vars (MariaDB/MySQL for wallet and minitrans lookups). Compose loads **`.env`** so values apply to containers (the **web** service overrides **`DATABASE_URL`** so it uses the Postgres service **`db`** on the Compose network—you do not need to change that URL for Docker).
2. Build and start **Postgres** + **web**:

   ```bash
   docker compose up --build -d
   ```

3. Open **http://127.0.0.1:8000/** (not https). **`/health`** returns `{"ok":true}` without requiring the DB.

The database is exposed on the host at **`127.0.0.1:15433`** (for GUI clients, backups, or running **`alembic`** / **`run_setup.cmd`** against the same DB from your machine).

Stop and remove containers (data volume kept unless you add `-v`):

```bash
docker compose down
```

**Images:** **`Dockerfile`** builds the API from **`requirements.txt`**, runs **`alembic upgrade head`** on start, then **uvicorn** on port 8000.

---

## Local development (no Docker for Python)

Use **Python 3.13** (see `pyproject.toml` and `.python-version`).

**One venv** at the repository root:

```bash
py -3.13 -m venv .venv
```

Activate it, or rely on **`start_app.cmd`** / **`run_setup.cmd`**, which prefer **`.venv\Scripts\python.exe`** when present.

```bash
pip install -r requirements.txt
```

Copy **`.env.example`** to **`.env`**. For local runs, point Postgres at the mapped port:

`DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:15433/aml_web`

- **`run_setup.cmd`** installs Python deps, starts **only** the **`db`** Compose service (not the **`web`** image), then runs **`alembic upgrade head`**—suitable when you develop with **`start_app.cmd`** on the host.
- If nothing is listening on **15433**, DB-backed routes return **500**.

### Web UI (local)

```bash
start_app.cmd
```

or:

```bash
python -m uvicorn app.main:app --reload --reload-exclude ".venv" --host 127.0.0.1 --port 8000
```

### Desktop script (optional)

```bash
python run.py
```

---

## Expected input columns (desktop / imports)

The script validates these columns exist (case/spacing tolerant):

- `RequestTimestamp` (date/time; used for day/week grouping)
- `Mobile` (WalletId)
- `Bin` + `AccountNumberLast4` (CardId = Bin + AccountNumberLast4)
- `Credit` (Amount)
- `ReasonCode` (Rejected = ReasonCode != 0)

If your headers differ (extra spaces, different casing), the loader normalizes them and surfaces detected columns if anything is missing.

---

## Desktop outputs

One file per scenario into the chosen output folder:

- `Scenario_D1_daily.xlsx`
- `Scenario_D2_daily.xlsx`
- `Scenario_D3_daily.xlsx`
- `Scenario_W1_weekly.xlsx`
- `Scenario_W2_weekly.xlsx`
- `Scenario_W3_weekly.xlsx`
