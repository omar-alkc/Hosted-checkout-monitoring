# Security Findings Report -- Development Environment

**System:** Hosted Checkout Monitoring System  
**Date:** 2026-05-06  
**Auditor Role:** Information Security Manager  
**Classification:** Internal -- Confidential  
**Note:** These findings were identified during the data flow audit. They cover application-level security controls and configuration hygiene. This is not a penetration test or a full OWASP assessment — these are observations from static code review. **Production deployment target:** RHEL 9 with rootless Podman Compose (see `deploy/rhel9/README.md`); Windows dev uses Docker Desktop.

---

## Findings Summary

| ID | Severity | Title | Status |
|----|----------|-------|--------|
| SEC-01 | High | Weak default session secret | Open |
| SEC-02 | Medium | Default database credentials in code | Open |
| SEC-03 | Medium | Session cookie not hardened for production | Open |
| SEC-04 | Medium | MariaDB connection without TLS | Open |
| SEC-05 | Medium | Web application served over plain HTTP | Open |
| SEC-06 | Low | Binary installer committed to repository | Open |
| SEC-07 | Low | Third-party CDN script without integrity check | Open |
| SEC-08 | Low | `.gitignore` gaps for data files | Open |
| SEC-09 | Informational | CLI password exposure in documentation | Open |

---

## Detailed Findings

### SEC-01 -- Weak Default Session Secret

**Severity:** High  
**File:** `app/config.py`, line 71  
**Category:** Authentication and Session Management

**Description:**

If the `SESSION_SECRET` environment variable is not set, the application falls back to a hardcoded string: `"dev-insecure-change-me-set-SESSION_SECRET"`. This value is visible in the source code. Anyone with access to the repository can forge valid session cookies, impersonate any user, and escalate privileges.

**Code:**

```python
sess = os.getenv("SESSION_SECRET", "").strip() or "dev-insecure-change-me-set-SESSION_SECRET"
```

**Risk:** Session hijacking, privilege escalation, full account takeover.

**Recommendation:**
- Validate at application startup that `SESSION_SECRET` is set and meets minimum entropy requirements (at least 32 random bytes, base64-encoded).
- Refuse to start the application if the secret is missing or matches the default.
- Generate production secrets using `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

---

### SEC-02 -- Default Database Credentials in Code

**Severity:** Medium  
**Files:** `app/config.py` line 64, `docker-compose.yml` line 8  
**Category:** Credential Management

**Description:**

The database URL defaults to `postgresql://postgres:postgres@127.0.0.1:5432/aml_web` when the `DATABASE_URL` environment variable is unset. Similarly, `docker-compose.yml` defaults `POSTGRES_PASSWORD` to `postgres`. While acceptable for local development, these defaults risk being carried unchanged into shared or production environments.

**Code (`app/config.py`):**

```python
db_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/aml_web").strip()
```

**Code (`docker-compose.yml`):**

```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
```

**Risk:** Unauthorized database access if defaults are used beyond local development.

**Recommendation:**
- Add a startup check that warns or fails if the default credentials are detected in non-development environments.
- Document mandatory credential rotation for any shared or staging deployment.

---

### SEC-03 -- Session Cookie Not Hardened for Production

**Severity:** Medium  
**File:** `app/main.py`, lines 20-25  
**Category:** Session Management

**Description:**

The Starlette `SessionMiddleware` is configured with `same_site="lax"` but does not set `https_only=True`. This means the session cookie can be transmitted over unencrypted HTTP connections, making it vulnerable to interception on the network.

**Code:**

```python
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
)
```

**Risk:** Session cookie interception over HTTP; session fixation.

**Recommendation:**
- Set `https_only=True` when deploying behind a TLS terminator.
- Consider making this configurable via an environment variable (e.g., `SECURE_COOKIES=true`).

---

### SEC-04 -- MariaDB Connection Without TLS

**Severity:** Medium  
**File:** `io_utils.py`, lines 312-321  
**Category:** Data in Transit

**Description:**

The PyMySQL connection to the external MariaDB database does not configure SSL/TLS. If the database is accessed over a network (not localhost), credentials and query results (including MSISDNs and wallet holder names) are transmitted in cleartext.

**Code:**

```python
conn = pymysql.connect(
    host=host, port=port, user=user, password=password,
    database=database, charset="utf8mb4",
    cursorclass=pymysql.cursors.Cursor, autocommit=True,
)
```

**Risk:** Credential and PII interception in transit on untrusted networks.

**Recommendation:**
- Add SSL parameters: `ssl={"ca": "/path/to/ca-cert.pem"}` or `ssl_disabled=False` with appropriate certificates.
- Add environment variables for SSL configuration (e.g., `MINITRANS_SSL_CA`).

---

### SEC-05 -- Web Application Served Over Plain HTTP

**Severity:** Medium  
**Files:** `docker-compose.yml` (port `8000:8000`), `docker-entrypoint.sh`  
**Category:** Data in Transit

**Description:**

The application is served via `uvicorn` on plain HTTP port 8000. The Docker Compose web service binds to `0.0.0.0:8000` (all network interfaces), meaning the unencrypted UI -- which displays PII including MSISDNs, wallet holder names, card fragments, and financial transaction data -- is accessible from any host on the local network.

The PostgreSQL service, by contrast, is correctly bound to `127.0.0.1:15433` (loopback only).

**Risk:** PII exposure in transit; session cookie theft via network sniffing.

**Recommendation:**
- Deploy behind a TLS-terminating reverse proxy (nginx, Caddy, or a cloud load balancer).
- Change the web service port binding to `127.0.0.1:8000:8000` if accessed only through a local reverse proxy.
- Alternatively, configure uvicorn with `--ssl-keyfile` and `--ssl-certfile` for direct TLS.

---

### SEC-06 -- Binary Installer in Repository

**Severity:** Low  
**File:** `postgresql-installer.exe` (root directory, untracked)  
**Category:** Supply Chain / Repository Hygiene

**Description:**

A Windows PostgreSQL installer binary (~300+ MB typically) exists in the repository root. Binary executables in source control cannot be meaningfully reviewed, their origin cannot be verified from git history, and they inflate repository size permanently (even after deletion, they persist in git history).

**Risk:** Supply chain integrity; repository bloat; potential for tampered binaries.

**Recommendation:**
- Remove the file from the repository.
- Add `*.exe` to `.gitignore`.
- Document the PostgreSQL download URL in `README.md` instead.

---

### SEC-07 -- Third-Party CDN Script Without Integrity Check

**Severity:** Low  
**File:** `app/templates/base.html`  
**Category:** Supply Chain / Client-Side Security

**Description:**

HTMX is loaded from `https://unpkg.com/htmx.org@1.9.12` without a Subresource Integrity (SRI) `integrity` attribute. If the unpkg CDN is compromised or serves a modified script, injected JavaScript would execute in the context of every authenticated user session.

**Risk:** Cross-site scripting (XSS) via CDN compromise; session theft.

**Recommendation:**
- Add an SRI hash: `<script src="https://unpkg.com/htmx.org@1.9.12" integrity="sha384-..." crossorigin="anonymous"></script>`.
- Alternatively, self-host the HTMX library under `app/static/` to eliminate CDN dependency entirely.

---

### SEC-08 -- `.gitignore` Gaps for Data Files

**Severity:** Low  
**File:** `.gitignore`  
**Category:** Data Leakage Prevention

**Description:**

The current `.gitignore` does not include patterns for file types that may contain sensitive data:

- `*.exe` -- binary installers (see SEC-06)
- `*.xlsx` -- transaction data files uploaded to the app, or the `Gov_mapping.xlsx` referenced in `.env.example`
- `*.docx` -- BRD documents generated by `generate_brd.py`

These files could be accidentally committed, exposing transaction data or PII in version control history.

**Recommendation:**

Add the following to `.gitignore`:

```
*.exe
*.xlsx
*.docx
```

---

### SEC-09 -- CLI Password Exposure in Documentation

**Severity:** Informational  
**File:** `.env.example`, line 20  
**Category:** Credential Handling

**Description:**

The documented admin creation command passes the password as a command-line argument:

```
python -m app.scripts.create_admin myuser "myP@ssw0rd" "Display name"
```

On multi-user systems, command-line arguments are visible in process listings (e.g., `ps aux`, Task Manager). The password also persists in shell history files (`.bash_history`, PowerShell `PSReadLine` history).

**Risk:** Credential exposure on shared systems.

**Recommendation:**
- Modify `create_admin.py` to accept passwords via `getpass.getpass()` (interactive stdin prompt) when no CLI argument is provided.
- Or accept the password via an environment variable.

---

## Well-Implemented Controls

The following security controls were found to be properly implemented:

| Control | Implementation | File(s) |
|---------|---------------|---------|
| Secrets excluded from git | `.env` in `.gitignore`; only `.env.example` tracked | `.gitignore` |
| Role-based access control | Three roles (admin, supervisor, investigator) with route-level enforcement via FastAPI dependencies | `app/deps/auth.py` |
| Session idle timeout | Configurable, default 15 min, minimum 60s enforced | `app/deps/auth.py`, `app/config.py` |
| Session max lifetime | Configurable, default 24h, minimum 300s enforced | `app/deps/auth.py`, `app/config.py` |
| Password hashing | pbkdf2_sha256 via passlib with automatic salting | `app/services/auth_service.py` |
| Parameterized SQL queries | All queries use bound parameters (SQLAlchemy `bindparams`, pymysql `%s` placeholders) | All service files, `io_utils.py` |
| External DB is read-only | Only SELECT queries against MariaDB clone tables | `io_utils.py` |
| PostgreSQL bound to loopback | `127.0.0.1:15433:5432` in Docker Compose | `docker-compose.yml` |
| PII excluded from logs | Logs contain counts and IDs only, no raw PII values | `io_utils.py`, service files |
| Upload size limits | Configurable max upload size with floor enforcement | `app/config.py` |

---

*End of Security Findings Report*
