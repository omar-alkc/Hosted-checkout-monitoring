# PII and Data Flow Security Audit Report

**System:** Hosted Checkout Monitoring System  
**Date:** 2026-05-06  
**Auditor Role:** Information Security Manager  
**Classification:** Internal -- Confidential

---

## 1. Audit Objective

This audit verifies two specific controls:

1. **No upstream write operations** to any third-party database, vendor system, or external service.
2. **No sharing of PII** with any AI pipeline, machine-learning service, or automated external data consumer.

---

## 2. Methodology

- Full static analysis of all Python source files, configuration files, container/Compose definitions (Podman on RHEL 9 production; Docker on Windows dev), dependency manifests, and templates in the repository.
- Keyword search for outbound HTTP libraries (`requests`, `httpx`, `urllib.request`, `aiohttp`, `http.client`), AI/ML service patterns (`openai`, `azure`, `anthropic`, `gemini`, `llm`, `embedding`, `inference`), and write operations (`INSERT`, `UPDATE`, `DELETE`, `PUT`, `POST` to external hosts).
- Trace of every database connection to determine read/write direction.
- Review of all environment variables and configuration parameters for external service endpoints.

---

## 3. Audit Scope -- Systems and Data Stores

| Component | Type | Owner | Direction |
|-----------|------|-------|-----------|
| PostgreSQL (`aml_web`) | Primary application database | Internal (self-hosted or GCP) | Read + Write |
| MariaDB/MySQL (`MINITRANS_*`) | External enrichment database | Third party / shared infrastructure | **Read-only** |
| Browser UI (FastAPI + Jinja2) | Authenticated web interface | Internal | Output to authenticated users only |
| Excel export | Downloadable workbook | Internal | Manual download by authenticated users |

---

## 4. Finding 1 -- No Upstream Write Operations to Third-Party Databases

### 4.1 Verdict: CONFIRMED -- No write operations to any third-party database

### 4.2 Evidence

**MariaDB/MySQL (the only external database connection):**

The application connects to an external MariaDB/MySQL database using five environment variables (`MINITRANS_HOST`, `MINITRANS_PORT`, `MINITRANS_USER`, `MINITRANS_PASSWORD`, `MINITRANS_DATABASE`) configured in `io_utils.py`.

Three functions access this database. Every one executes only `SELECT` statements:

| Function | File | SQL Operation | Tables Accessed |
|----------|------|---------------|-----------------|
| `fetch_wallet_profiles()` | `io_utils.py` line 293 | `SELECT msisdn, extra13 AS Fullname, city FROM actors_clean1_clone WHERE msisdn IN (...)` | `actors_clean1_clone` |
| `fetch_last_30_days_transactions()` | `io_utils.py` line 346 | `SELECT ... FROM minitrans_clone WHERE creditedMSISDN IN (...) OR debitedMSISDN IN (...)` | `minitrans_clone` |
| `fetch_post_card_debit_transactions()` | `io_utils.py` line 394+ | `SELECT ... FROM minitrans_clone WHERE ...` | `minitrans_clone` |

No `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, or `TRUNCATE` statements are issued against the MariaDB connection in any code path. The connection is opened with `autocommit=True` and closed in a `finally` block, consistent with read-only usage.

The accessed tables use a `_clone` suffix (`actors_clean1_clone`, `minitrans_clone`), indicating the application reads from replicated/cloned copies rather than production source tables.

**No other external database connections exist.** The only other database is the application's own PostgreSQL instance (`aml_web`), which is self-managed and internal to the deployment.

### 4.3 Outbound Network Calls

A full search of all Python files confirmed:

- **No `requests` library usage** -- not imported or called anywhere.
- **No `httpx` library usage** -- not imported or called anywhere.
- **No `urllib.request` usage** -- `urllib.parse` is used for URL encoding only (query string construction for redirects in `app/routers/web.py`).
- **No `aiohttp` usage** -- not imported or called anywhere.
- **No `http.client` usage** -- not imported or called anywhere.
- **No webhook endpoints** -- no outbound POST/PUT calls to external URLs.
- **No message queue producers** -- no Kafka, RabbitMQ, SQS, or similar integrations.
- **No scheduled export jobs** -- no cron, APScheduler, Celery, or similar task schedulers.

The `requirements.txt` dependency manifest does not include any HTTP client library (no `requests`, `httpx`, `aiohttp`).

### 4.4 Data Output Channels

All data leaves the application through exactly two controlled channels:

1. **Browser UI** -- HTML pages rendered server-side via Jinja2, served to authenticated users over the web interface. Data is displayed but never forwarded to external systems.
2. **Excel download** -- `GET /detections/export` generates an `.xlsx` workbook from detection data. The file is downloaded by the authenticated user's browser. No automated delivery (no email, no FTP, no cloud storage upload).

Both channels require active human involvement. There is no automated, scheduled, or event-driven data export mechanism.

---

## 5. Finding 2 -- No PII Sharing with AI Pipelines

### 5.1 Verdict: CONFIRMED -- No PII is shared with any AI pipeline or service

### 5.2 Evidence

**No AI/ML dependencies:**

The `requirements.txt` file contains no AI/ML-related packages. Specifically absent:

- No `openai`, `anthropic`, `google-generativeai`, `azure-ai-*`, `langchain`, `llama-index`
- No `transformers`, `torch`, `tensorflow`, `scikit-learn`, `keras`
- No `boto3` (AWS AI services), `google-cloud-aiplatform`, `azure-cognitiveservices-*`
- No embedding, vectorization, or inference libraries

**No AI API calls in source code:**

A search of all `.py` files found zero references to:

- Any LLM API endpoint (OpenAI, Anthropic, Azure OpenAI, Google Gemini, etc.)
- Any AI inference endpoint or model serving URL
- Any embedding or vectorization service
- Any AI-related environment variables or configuration keys

**No AI-related environment variables:**

The `.env.example` file and `app/config.py` define only:

- `DATABASE_URL` -- PostgreSQL connection
- `POSTGRES_PASSWORD` -- Database credential
- `APP_TITLE` -- UI display string
- `SESSION_SECRET` -- Session signing key
- `MAX_UPLOAD_BYTES` -- Upload size limit
- `APP_ACTOR_NAME` -- Display name
- `MINITRANS_*` -- MariaDB enrichment credentials
- `GOV_MAPPING_PATH` -- Local Excel file path for city mapping
- `SESSION_IDLE_TIMEOUT_SECONDS` / `SESSION_MAX_AGE_SECONDS` -- Session configuration
- `DEMO_MODE` -- Feature flag (not functionally referenced)

None of these connect to any AI service.

**PII inventory -- what the application handles:**

| PII Element | Storage Location | Shared with AI? |
|-------------|-----------------|-----------------|
| MSISDN (mobile wallet number) | PostgreSQL JSONB | No |
| Wallet holder full name | PostgreSQL JSONB (enriched from MariaDB) | No |
| Card BIN + last 4 digits | PostgreSQL JSONB | No |
| Account holder name | PostgreSQL JSONB | No |
| Transaction amounts and dates | PostgreSQL JSONB | No |
| City of wallet holder | PostgreSQL JSONB (enriched from MariaDB) | No |
| Application user credentials | PostgreSQL `users` table (hashed) | No |

All PII remains within the application's PostgreSQL database and is only surfaced through the authenticated browser UI or manually downloaded Excel exports.

### 5.3 PII Logging Practices

Application logs do not contain PII:

- `io_utils.py` logs only chunk progress counts (e.g., "chunk 3/5 (1000 MSISDNs)"), not actual MSISDN values.
- `scenario_run.py` logs detection/batch IDs and exception types, not metric contents.
- `create_admin.py` prints the username after creation but no passwords.
- No transaction data, wallet holder names, or card data appears in any log statement.

---

## 6. Summary of Controls Verified

| Control | Status | Evidence |
|---------|--------|----------|
| No write operations to external MariaDB | **PASS** | Only SELECT queries; no INSERT/UPDATE/DELETE in codebase |
| No outbound HTTP/API calls | **PASS** | No HTTP client libraries in dependencies or code |
| No webhook or event-driven exports | **PASS** | No webhook, queue, or scheduler code |
| No AI/ML library dependencies | **PASS** | requirements.txt contains zero AI packages |
| No AI API endpoint calls | **PASS** | No AI service URLs or SDK usage in any Python file |
| No AI-related configuration | **PASS** | No AI API keys or endpoints in env vars |
| PII confined to internal database | **PASS** | All PII stored in self-managed PostgreSQL only |
| PII excluded from application logs | **PASS** | Logs contain operational metadata only |
| Data export is manual only | **PASS** | Excel download requires authenticated human action |
| No ZainCash database connection | **PASS** | String "zaincash" absent from entire codebase |

---

## 7. Conclusion

This application is a **self-contained, internally facing monitoring tool** with no upstream write capabilities and no AI integration. Its data flow is unidirectional from external sources (Excel uploads, MariaDB reads) into its own PostgreSQL database, with human-controlled output via browser UI and manual Excel downloads. PII never leaves the application boundary through any automated mechanism, and no PII is exposed to any AI pipeline, LLM, or machine-learning service.

---

*End of PII and Data Flow Audit Report*
