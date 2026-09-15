# mail_ai_automation — Dynamic Connector System & Related Features
## Project Overview / Technical Documentation

**Status:** Core system implemented and tested. Some pieces deferred deliberately (documented below).
**Environment:** Dev/staging. FastAPI (`api`) + Celery (`worker`) + IMAP listener (`listener`) + MySQL + Redis + Qdrant + standalone embed service, all via `docker-compose`.

---

## 1. Background & Motivation

The original system hardcoded exactly two CRM API integrations per client:
- `create_payload_table` — one "create ticket" webhook config per client.
- `payload_get_table` — one "fetch ticket status" webhook config per client.

Both used a bespoke per-request LLM call (`design_payload` in `app/llm.py`) to map dynamic email data into a CRM's expected JSON payload, then base64-encoded that JSON into a URL query string (`?data=<base64>` / `?postData=<base64>`).

Limitations of the old system:
- Exactly one create endpoint and one status endpoint per client — no support for additional trigger types.
- No admin approval workflow — any client could point their `url` at anything, including internal Docker-network hosts (SSRF risk — `redis`, `mysql`, `qdrant`, `embed_service`, the API itself were all reachable).
- No rate limiting on the config-write endpoints.
- Per-request LLM mapping cost on every single ticket creation, instead of a stable, pre-approved template.

**Goal:** Replace this with a generic `connector_configs` table supporting **N** APIs per client, **N** trigger types, driven by an admin-approved template + generic executor — with zero per-trigger-type Python code required to add a new integration.

---

## 2. Core Design Principles

1. **Generic execution, not per-type code.** One executor renders a template, fires an HTTP request, and maps the response — regardless of what `trigger_type` it's serving.
2. **Admin approval always required before `live`**, regardless of who authored the config (client or admin).
3. **Fail loud, fail early.** Bad templates, bad URLs, and bad response mappings are rejected at **approval time** wherever possible — not discovered in production when a real email fails silently.
4. **Defense in depth on security-relevant checks** (URL allowlist) — checked both at approval time and again at execution time, since a config could theoretically go stale between the two.
5. **No automatic-content trust.** LLM-generated templates (see §8) go through the *exact same* validation as human-authored ones. No exemptions.
6. **Verify everything by actually running it.** Nothing in this system was accepted as "looks correct" — every function, every endpoint, every edge case was tested against a real database and, where relevant, a real HTTP server, before being considered done. This caught several real bugs (see §10) that code review alone would not have caught.

---

## 3. Database Schema

### 3.1 `connector_configs`

The central table. One row per connector configuration (one client, one trigger_type, one version/lifecycle state).

| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT AUTO_INCREMENT PK | |
| `client_id` | VARCHAR(50) | |
| `trigger_type` | VARCHAR(100) | Free string — routing key only, e.g. `order_status`, `ticket_create`. No per-type Python in the executor. |
| `http_method` | VARCHAR(10) | `GET` or `POST` |
| `url` | VARCHAR(500) | Must match an entry in `url_allowlist` (exact scheme+netloc+path match) |
| `headers_template` | JSON | `{{key}}`-placeholder template, same rules as `request_template` |
| `request_template` | JSON | `{{key}}`-placeholder template rendered from `context_data` at execution time |
| `response_mapping` | JSON | `{"fields": [{"field": ..., "path": <JMESPath>, "extract_regex": ...}], "pagination": {...}}` |
| `auth_type` | ENUM | `bearer` / `basic` / `api_key_header` / `api_key_query` |
| `auth_secret_encrypted` | TEXT | Fernet-encrypted (see §6) |
| `auth_field_name` | VARCHAR(100) | Header/query param name, only used by `api_key_*` auth types |
| `payload_encoding` | ENUM | `plain` (default) or `base64_query` |
| `base64_query_param_name` | VARCHAR(50) | Required if `payload_encoding='base64_query'` — enforced at insert time |
| `status` | ENUM | `draft` / `pending_approval` / `live` / `disabled` |
| `version` | INT | |
| `created_by`, `approved_by`, `approved_at` | | |
| `live_marker` | VARCHAR(100), GENERATED | `trigger_type` if `status='live'`, else `NULL` |
| `pending_marker` | VARCHAR(100), GENERATED | `trigger_type` if `status='pending_approval'`, else `NULL` |

**Constraints:**
- `UNIQUE (client_id, live_marker)` — at most one `live` row per (client, trigger_type). MySQL treats multiple `NULL`s as non-conflicting, so this only constrains actual live rows.
- `UNIQUE (client_id, pending_marker)` — same mechanism, at most one `pending_approval` row per (client, trigger_type).
- `draft` status is **uncapped** and unconstrained — free scratch space.
- `INDEX (client_id, status)` — required for the cap-check locking (`FOR UPDATE`) to take a real gap lock under `REPEATABLE READ`.

**Known DDL gotcha (hit during build):** a `UNIQUE INDEX` across `scheme + netloc + path` VARCHAR columns under `utf8mb4` can exceed MySQL's 3072-byte max key length. This actually happened on `url_allowlist` (see §10) and crashed the whole API container silently on every restart until diagnosed. Always compute `chars × 4` against 3072 before adding a multi-column unique index on VARCHAR columns.

### 3.2 `url_allowlist`

```sql
CREATE TABLE url_allowlist (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    scheme VARCHAR(20) NOT NULL,
    netloc VARCHAR(255) NOT NULL,
    path VARCHAR(255) NOT NULL DEFAULT '',
    added_by VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX uq_scheme_netloc_path (scheme, netloc, path(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

SSRF prevention. `path(191)` is a prefix index — necessary to stay under the 3072-byte key-length limit (this table was where the byte-length bug actually surfaced).

### 3.3 `paused_email_history`

```sql
CREATE TABLE paused_email_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    client_id VARCHAR(50) NOT NULL,
    from_email VARCHAR(255) NOT NULL,
    subject TEXT,
    body TEXT,
    status ENUM('pending_review', 'ignored', 'replied') DEFAULT 'pending_review',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_client_status (client_id, status),
    INDEX idx_client_email (client_id, from_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

A review-queue table, deliberately modeled on the existing `reply_blocked_by_keyword` pattern. **Coexists** with `email_logs` (does not replace it) — every paused email still gets a `status='paused'` row in `email_logs` (the universal audit log) **and** a `pending_review` row here (the actionable review queue).

### 3.4 `email_accounts.connector_cap` (new column)

```sql
ALTER TABLE email_accounts ADD COLUMN connector_cap INT DEFAULT 5
```

Per-client cap on live/pending connector configs. Read via `get_connector_cap(client_id)`, defaults to `5` if unset.

### 3.5 Existing tables referenced (unchanged)

`email_accounts`, `email_logs`, `paused_emails`, `reply_blocked_by_keyword`, `chat_history`, `ticket_record`, `users`, `global_default_llm`, `globally_available_llm_configs`, `client_llm_config`, `client_email_disclaimers`, `llm_logs`, `email_customers`, `keyword_block_policy`, `blocked_keywords`, `celery_task_log` — see inline code comments; not modified by this work except where noted.

---

## 4. Concurrency & Race Safety

All count-changing writes to `connector_configs` go through **one** function: `insert_connector_config_checked()`. It:
- Sets `REPEATABLE READ` isolation and opens a transaction.
- For `status='live'` or `'pending_approval'`, takes a `SELECT COUNT(*) ... FOR UPDATE` gap lock and checks against the per-client cap before inserting.
- `draft` inserts skip locking entirely (uncapped by design).
- Catches both `IntegrityError` (unique-constraint collision — converted to `CreationRaceError`) and MySQL error 1213/1205 (deadlock / lock-timeout — also converted to `CreationRaceError`).

**Promotion to `live`** is split into two distinct functions, not one:
- `swap_to_live(client_id, trigger_type, new_config_id, old_config_id, ...)` — replacement of an existing live row. Disables the old row, then activates the new one, in one transaction. Count-neutral (no cap check needed).
- `activate_first_live(client_id, trigger_type, new_config_id, ..., cap)` — first-ever activation for a (client, trigger_type) with no existing live row. **Does** change the live count, so it takes the same `FOR UPDATE` cap-check lock as the insert path.
- `approve_connector_config(...)` — the single entry point admin endpoints call; looks up whether a live row already exists and routes to the correct one of the two functions above.

Both `swap_to_live` and `activate_first_live` also:
- Fetch and validate `url`, `request_template`, `headers_template`, `response_mapping` **before** opening the write transaction (cheap checks first, fail before taking any lock).
- Check the URL against `url_allowlist` (`AllowlistViolationError` if not present).
- Validate every `{{key}}` placeholder against `CONTEXT_DATA_KEYS` (`TemplateValidationError`).
- Validate `response_mapping` against the required-field contract for that `trigger_type` (`ResponseMappingValidationError`).

**Confirmed via actual concurrency testing** (two processes racing, `time.sleep()` injected to widen the race window): correctly produces exactly one `SUCCESS` and one `CapExceededError`/`SwapRaceError` under contention. A real MySQL deadlock (error 1213) was triggered and observed during this testing — the original exception handling only caught `IntegrityError`, letting the deadlock leak as a raw `pymysql.err.OperationalError`; this was found and fixed by adding an explicit deadlock/lock-timeout check (`_is_deadlock_or_lock_timeout`) to every write path.

**Rejection:** `reject_connector_config(config_id, client_id, admin, reason)` — moves a `pending_approval` row to `disabled` (never deletes — audit trail preserved). Reuses `SwapRaceError` for the "row no longer pending" case rather than adding a fifth exception type.

---

## 5. Context Data (`app/context_data.py`)

Defines the **fixed, closed vocabulary** of placeholders any `request_template`/`headers_template` may reference:

```
client_id, from_email, subject, body, cleaned_body, ticket_id,
intent, sentiment, priority, customer_name, history_summary, issue_description
```

Split for cost reasons:
- **`CHEAP_KEYS`** (10 fields) — pure Python, no LLM call, computed unconditionally by `build_context_data_base(...)` once per email, before any trigger_type-specific branching.
- **`EXPENSIVE_KEYS`** (`history_summary`, `issue_description`) — each requires a real LLM call. Computed **lazily**, only if the selected template actually references them, via `resolve_expensive_keys(needed_keys, body, history, old_summary)`. This split exists specifically to avoid burning LLM calls on every email for context fields a given trigger_type's template never uses.

A startup-time `assert CHEAP_KEYS | EXPENSIVE_KEYS == CONTEXT_DATA_KEYS` guards against the two sets silently drifting out of sync with the schema.

---

## 6. Secrets (`app/secrets_crypto.py`)

The original spec assumed reuse of an existing `token_encryption_keys` mechanism. **That mechanism does not exist anywhere in this codebase** — confirmed by exhaustive grep (`import re; ...`, checked both `app/` and `worker/`, only hits were unrelated OIDC library noise in a vendored venv). This is a real gap in the original planning document.

Built from scratch: `encrypt_secret(plaintext) -> str` / `decrypt_secret(token) -> str`, using `cryptography.fernet.Fernet`, keyed by the `CONNECTOR_SECRET_ENCRYPTION_KEY` environment variable (generated once, stored in `.env`, loaded by every container via the existing `env_file:` docker-compose convention). Round-trip tested directly.

---

## 7. The Executor (`app/connector_executor.py`)

`execute_connector(config, context_base, body, history, old_summary) -> {"success": bool, "data"/"error": ...}`

Pipeline, in order:
1. Parse `request_template` + `headers_template` for `{{key}}` placeholders (regex: `\{\{(\w+)\}\}`).
2. Determine which `EXPENSIVE_KEYS` are actually referenced; call `resolve_expensive_keys` only for those.
3. Merge into full context; render both templates — JSON-escaped substitution (`json.dumps(str(value))[1:-1]`), never raw string interpolation (prevents a stray `"` in, e.g., a customer email address from breaking the JSON structure or enabling injection).
4. Re-validate every placeholder is a known key with a non-`None` value — fails loudly (`TemplateRenderError`) rather than silently substituting a literal `"None"`.
5. **Re-check the URL allowlist** — a second, independent check from the one done at approval time (defense in depth: a config could theoretically be approved, then have its URL revoked from the allowlist before execution).
6. Decrypt `auth_secret_encrypted`, apply per `auth_type`:
   - `bearer` → `Authorization: Bearer <secret>` header
   - `basic` → HTTP basic auth (`secret` is a JSON blob `{"username": ..., "password": ...}`)
   - `api_key_header` → custom header named by `auth_field_name`
   - `api_key_query` → custom query param named by `auth_field_name`
7. Fire the HTTP request, per `payload_encoding`:
   - `plain` (default) — JSON body for POST, plain query params for GET. **Deliberately does not replicate** the old system's base64-in-query-string convention — that was judged a legacy quirk of one specific vendor's CRM, not a general pattern worth preserving, especially given this project has zero live clients to migrate.
   - `base64_query` — base64-encodes the entire rendered body as JSON, puts it in a single query parameter (name configurable via `base64_query_param_name`, required at insert time when this mode is selected). Added specifically to support integrations that *do* need this shape.
8. Apply `response_mapping`: JMESPath extraction per field (`jmespath.search(path, response_json)`), then optional regex fallback (`extract_regex`) if the field needs an ID pulled out of a larger string (mirrors the old system's `re.search(r"T-\d{6}-\d+", ...)` pattern for reference numbers embedded in a message).
9. **Pagination is explicitly unimplemented.** The schema (`pagination.enabled`, `mode`, `cursor`/`page_number` config) exists and validates fine, but the executor only ever fetches page 1. If `pagination.enabled=true` is set, a warning is logged and the request proceeds as if pagination were off. No current trigger_type in this system returns a list, so there has been no real case to build and test pagination logic against — building it speculatively was explicitly rejected as unvalidated scope with no way to prove correctness (see §11 "Deferred").

`format_mapped_data_for_prompt(mapped_data)` — a generic formatter that turns the executor's flat output dict into human-readable lines for LLM prompt context (`docket_no` → "Ticket ID:", `ticket_status` → "Status:", everything else auto-title-cased), skipping `None`/empty values. Replaces four near-identical hardcoded f-string blocks that previously existed in `worker/tasks.py`.

---

## 8. Required Response Fields (Fixed Minimal Contract)

Rather than requiring every connector to reproduce the old CRM's full 13-field response shape, or going fully generic (which turned out to conflict with `chat_history`'s fixed SQL schema — see discussion below), a **minimal required-field floor** is enforced per `trigger_type` at approval time:

```python
REQUIRED_RESPONSE_FIELDS = {
    "order_status": {"docket_no", "ticket_status"},
    "ticket_create": {"ticket_id"},
}
```

Rationale for exactly these fields: they're the ones actually load-bearing in control flow or written to fixed DB columns (`chat_history.status`, `ticket_record.ticket_id`). Everything else (priority, remarks, disposition, assigned dept/user, person details) is optional and passed through generically if a given CRM's `response_mapping` happens to include it — `format_mapped_data_for_prompt` will render whatever extra fields are present, without requiring them.

**Why not fully generic:** initially considered, but `chat_history`/`ticket_record` have real fixed SQL columns (`priority VARCHAR(50)`, `status VARCHAR(50)`) that need *something* written into them consistently — full genericity doesn't eliminate the need for a contract, it just moves it from "response_mapping must produce these names" to "whoever configures response_mapping must also specify which of their arbitrary field names maps to the fixed DB columns" — arguably more complex, not less. The minimal fixed-contract approach was chosen deliberately after working through this tradeoff.

---

## 9. Wiring Into the Email Pipeline (`worker/tasks.py`)

Four legacy call sites, all migrated:

| Old call | New call | Location |
|---|---|---|
| `get_order_status(client_id, ticket_id)` | `run_order_status_lookup(...)` | PATH A — initial ticket-status check |
| `get_order_status(client_id, stored_ticket_id)` | `run_order_status_lookup(...)` | PATH C — `pending_verification` retry |
| `get_order_status(client_id, history_ticket_id)` | `run_order_status_lookup(...)` | PATH C — history-scan branch |
| `call_create_ticket(...)` | `run_ticket_create(...)` | `_create_ticket_and_reply()` helper |

Both wrapper functions (in `app/connector_config.py`) preserve the **return contract** of their legacy counterparts (`{"success": bool, "data"/"ticket_id": ..., "error": ...}`) so the surrounding PATH A/B/C branching logic in `worker/tasks.py` required **zero structural changes** — only the call sites themselves and the immediately-following context-building code (swapped from hardcoded nested-dict field access to `format_mapped_data_for_prompt`).

`run_ticket_create` passes through **all** fields the executor's `response_mapping` produced (not just `ticket_id`) — with a defensive collision guard against a misconfigured mapping producing a field literally named `success` or `message`, which would otherwise silently corrupt the wrapper's own return contract.

**"Zero live connector config" degraded path:** both wrapper functions return the same `{"success": False, "error": ...}` shape `get_order_status`/`call_create_ticket` already returned on failure — meaning the existing fallback logic in PATH A/C (verification flow, ticket-creation-on-failure) handles a missing connector config identically to how it already handled a missing legacy payload config. No new fallback code was needed.

---

## 10. Admin API Endpoints (`app/main.py`)

All under `/admin/connector-configs*`, following existing codebase conventions (`require_client_access`, `require_admin`, `RedisRateLimiter`).

| Endpoint | Access | Purpose |
|---|---|---|
| `POST /admin/connector-configs` | client-or-admin (own client_id) | Create — defaults to `pending_approval`, accepts `status: draft\|pending_approval` |
| `GET /admin/connector-configs/{client_id}` | client-or-admin | List — flags `requires_regex_review: true` if any field uses `extract_regex` |
| `POST /admin/connector-configs/{id}/approve` | **admin only** | Looks up `trigger_type` server-side (not client-supplied), routes through `approve_connector_config` |
| `POST /admin/connector-configs/{id}/reject` | **admin only** | Marks `disabled` |
| `POST /admin/connector-configs/regenerate` | client-or-admin | Mechanical resubmit — creates a new `pending_approval` row for an existing (client, trigger_type); the current `live` row is untouched and keeps serving until the new one is explicitly approved (zero-downtime editing) |
| `POST /admin/connector-configs/generate-preview` | client-or-admin | LLM-assisted template drafting (§11) — **no DB write** |
| `POST /admin/url-allowlist`, `GET /admin/url-allowlist` | admin only | Manage the SSRF allowlist |

All error paths (`CapExceededError`, `CreationRaceError`, `SwapRaceError`, `AllowlistViolationError`, `TemplateValidationError`, `ResponseMappingValidationError`) are mapped to proper HTTP status codes (429/409/400) — verified to actually surface correctly through the HTTP layer, not leak as raw 500s.

**Verified end-to-end via real HTTP calls with real auth tokens** — create, list, both approve outcomes (success and allowlist-rejection), admin-gate rejection (client token correctly gets 403 on approve), reject, and regenerate (confirmed the old `live` row remains untouched while a new `pending_approval` row coexists).

---

## 11. LLM-Assisted Template Generation

`generate_connector_template(trigger_type, crm_schema_description, sample_response="")` — takes a **human-written** free-text description of a target CRM's expected request shape and (optionally) a sample response, and asks an LLM to draft a `request_template` + `response_mapping` pair.

- Constrained explicitly to `CONTEXT_DATA_KEYS` — the prompt shows the full allowed vocabulary, told these are the *only* valid placeholders.
- Shown the `REQUIRED_RESPONSE_FIELDS` contract for the given `trigger_type`.
- Output passes through the **exact same** `_validate_template_placeholders` / `_validate_response_mapping_fields` functions a human-submitted config must pass — no exemption for LLM output.
- **Preview-only design** (deliberate product decision): generation never writes to the database. The endpoint (`POST /admin/connector-configs/generate-preview`) returns the draft JSON for human review/editing; the human then submits it through the already-existing, already-tested `create` endpoint. This was chosen over auto-save specifically so a human gets a real editing opportunity before anything reaches `pending_approval`.

**A real bug was found and fixed during testing, not assumed away:** given a description mentioning a CRM field with no corresponding `CONTEXT_DATA_KEYS` match (e.g. a customer phone number — not in the schema), the LLM reliably emitted that field with a literal `null` or empty-string value, rather than omitting it — despite an explicit prompt instruction saying "never emit null." **Two independent rounds of stronger prompt wording (including a worked before/after example) failed to reliably stop this.** Resolved instead with deterministic post-processing: `_strip_unmapped_fields()` removes any `request_template` key whose value doesn't contain an actual `{{placeholder}}`, applied after LLM generation, before validation. This is a direct illustration of a broader lesson from this build: prompt-only instructions are not a reliable enforcement mechanism for hard constraints — enforce those in code, use the prompt only to shape the common case.

Tested across 5 distinct scenarios (flat response, nested JMESPath paths, vague/underspecified description, unmappable request field ×2 confirming the fix, unmappable response field) plus one full generate→preview→submit round trip through real HTTP endpoints.

---

## 12. Paused-Email Review Workflow

Mirrors the existing `reply_blocked_by_keyword` review-queue pattern (three states: `pending_review` / `ignored` / `replied`), applied to paused senders.

- **Dual-write, not a replacement:** when an email arrives from a paused sender, it's written to `email_logs` (`status='paused'`, universal audit trail, unchanged) **and** to `paused_email_history` (`status='pending_review'`, the new actionable queue) — both in the same transaction, in `worker/tasks.py`.
- `GET /paused-email-history/{client_id}` — list, with independent `status` filter and `group_by_email` boolean (composable — can combine both).
- `PATCH /paused-email-history/{client_id}/{record_id}` — manually flip status to `ignored`/`replied`. No bulk-ignore endpoint (deliberately out of scope per product decision — considered and declined).
- **`/manual-reply` extended** to accept an optional `paused_history_record_id`, alongside the pre-existing `blocked_record_id`, using the *identical* validate → send → conditionally-update pattern already proven for blocked-keyword mail: the review-queue row is only marked `replied` if the SMTP send actually succeeds; on failure it deliberately stays `pending_review` rather than falsely marking the item handled. Both IDs may be supplied in the same call (accepted, not mutually exclusive — a deliberate product decision, not an oversight).

The originally-proposed simpler design (a read-only endpoint joining `email_logs` + `paused_emails`, no dedicated table, no status tracking) was **superseded** by this design once a `pending_review`/`ignored`/`replied` workflow was specifically requested — the simple grouped endpoint was removed in favor of this table-backed approach.

---

## 13. Real Bugs Found During This Build (Not Hypothetical)

This list exists because every one of these was caught only by actually running code against a real database/HTTP server — none would have been caught by code review alone.

1. **MySQL key-length overflow** on `url_allowlist`'s original `UNIQUE INDEX (scheme, netloc, path)` under `utf8mb4` — silently crash-looped the entire `api` container on every restart for a period, while the visible symptom ("table doesn't exist") pointed at an unrelated hypothesis (lifespan not running) until the actual container logs were checked.
2. **`insert_connector_config_checked` missing `import json`** — the `_validate_response_mapping_fields` function referenced `json` without it being imported in that file, surfaced only when actually exercised, not at import time.
3. **`_apply_response_mapping` assumed `response_mapping` was already a dict** — pymysql actually returns JSON columns as plain `str`; the function crashed with `AttributeError: 'str' object has no attribute 'get'` on the very first real end-to-end executor test. Fixed by deserializing defensively.
4. **Two separate "missing `from app.connector_config import run_order_status_lookup`" incidents** in `worker/tasks.py` call sites — passed a bare `import worker.tasks` sanity check (which only proves the *module* imports cleanly, not that every function body's late imports are present) and only surfaced when the specific code path was actually executed.
5. **LLM-generated templates silently including unmappable fields as `null`/empty-string**, despite explicit prompt instructions not to — required a code-level fix (§11), prompt wording alone was insufficient across multiple attempts.
6. **Stale test data causing misleading results repeatedly** — e.g. an "unexpected" successful allowlist check that turned out to be a leftover allowlist entry from an earlier test run, not a real bug; several `SwapRaceError`s that were actually just re-running a test against already-consumed rows. Recurring lesson: always verify actual DB state before concluding a test result means what it appears to mean.
7. **`.run()` vs `.delay()` on a Celery task** — calling `process_email_task.run(...)` directly (bypassing the broker) fails immediately with `Column 'task_id' cannot be null`, since `self.request.id` is unset outside real task context. Full end-to-end testing requires actually dispatching through Celery (`.delay()`) and tailing worker logs, not shortcutting via direct function calls.
8. **Forgetting to restart the `worker` container after editing `worker/tasks.py` or `app/connector_config.py`** — recurring throughout the session. Unlike `api` (runs `uvicorn --reload`, auto-reloads on file change), `worker`'s Celery process has no such flag; a code edit on disk has zero effect on the running process until it's explicitly restarted. Caused several rounds of "the fix isn't working" that were actually "the fix was never loaded."

---

## 14. Deliberately Deferred (Not Forgotten, Not Oversights)

- **Old-code teardown.** `app/request_handler.py`'s `get_order_status`/`call_create_ticket`, the now-dead `from app.request_handler import call_create_ticket, get_order_status` import line in `worker/tasks.py`, `create_payload_table`/`payload_get_table`, and the old unrestricted endpoints (`/insert-create_payload_ticket`, `/insert-payload_get_ticket`) are all still present and functional, deliberately left as a safety net. Explicit decision: keep them until confidence in the new system is very high, revisit teardown as a separate deliberate step — not bundled into feature work.
- **Pagination in `response_mapping`** — schema exists, executor logic does not. No current trigger_type returns a list, so there is no real target to build or validate pagination logic against. Explicit decision: build it when a real list-returning trigger_type actually exists, treat that as the test case, not before.
- **`reply_blocked_by_keyword` teardown** — considered (in favor of a unified `email_logs`-based approach) and explicitly declined once the operational cost was made concrete (three live endpoints depend on it, plus the `/manual-reply` integration).
- **Bulk-ignore for `paused_email_history`** — considered, explicitly declined as unnecessary scope.

---

## 15. Testing Philosophy Applied Throughout

A recurring, explicit pattern across this entire build, worth stating directly since it shaped every decision:

- Every function was tested standalone, via direct Python calls against the real dev database, before being wired into anything else.
- Every concurrency-sensitive function was tested under *actual* concurrent load (two racing processes, artificial delays to widen race windows) — not just read for correctness, because lock behavior is exactly the kind of thing that looks correct on paper and fails under real contention.
- Every HTTP-facing piece (executor, admin endpoints) was tested against a real running HTTP server (`.Test_files/Dummy_APIs.py`, extended with new `/v2/*` endpoints for this work) and real `curl`/`requests` calls with real auth tokens — never mocked.
- Full-task proofs were run through the *real* Celery dispatch path (`.delay()` + tailing worker logs), not shortcut via direct function calls, specifically because the shortcut path was shown to skip real task-context setup.
- Claims of "done" were repeatedly walked back and re-verified when evidence was ambiguous (e.g. re-checking whether an approval's success was genuine or just stale allowlist state) rather than accepted on the strength of a plausible-looking result.

This is the reason the build took as long as it did, and also the reason the real bug count (§13) is fully known and fixed rather than latent.