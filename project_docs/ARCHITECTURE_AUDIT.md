# Architecture & Security Audit

**Initial Audit Date:** 2026-09-14  
**Scope:** Tenant isolation (RAG, credentials, responses), conversational troubleshooting, CRM/order/payment status checks, email threading.

---

## 1. Core Requirements vs. Reality

| Target Requirement | Current Implementation State | Architectural Risk / Gap | Severity |
|---|---|---|---|
| **Multi-Turn Troubleshooting** | Single-turn execution. Auto-escalates to ticket if RAG score $\le$ threshold. | Bot cannot diagnose issues back-and-forth; cuts tickets immediately on turn 1. | **CRITICAL** |
| **Conversational Memory** | Redis cache only with `HISTORY_TTL = 3600` (1 hr). MySQL `chat_history` only populates *after* ticket creation. | Asynchronous email replies (>1 hr) lose all context; bot suffers complete amnesia. | **CRITICAL** |
| **RFC-822 Email Threading** | `imap_reader.py` ignores `In-Reply-To` and `References`. `email_logs` lacks `thread_id`. | Inquiries from same sender cross-contaminate; cannot track true email conversation trees. | **HIGH** |
| **RAG Tenant Isolation** | Single shared Qdrant collection (`mail_ai_knowledge`) filtered by metadata; single shared `fallback_db.json`. | Soft logical isolation only; programmatic omission of filter or `search_all_clients` leaks data across tenants. | **HIGH** |
| **Credential Tenant Isolation** | Single static Fernet key (`CONNECTOR_SECRET_ENCRYPTION_KEY`) in `.env` for all client secrets. | Compromise of master key compromises every client's Zoho, Shopify, and Freshdesk accounts. | **HIGH** |
| **Payment Status Inquiries** | Only `ticket_status` and `order_status` triggers exist; no payment trigger or tool. | Bot cannot look up payment, invoice, or refund transactions. | **MEDIUM** |
| **Connector Routing** | Regex heuristics on query text (`"order"` vs `"ticket"`). | Ambiguous queries (e.g. "ticket for my order") trigger wrong API connector. | **MEDIUM** |
| **Legacy Dual Connector Stack** | `request_handler.py` and `order_routes.py` retain hardcoded C-Zentrix GET/base64 logic alongside dynamic `connector_executor.py`. | Split brain: `worker/tasks.py` uses dynamic connectors, but `order_routes.py`, `mcp_server.py`, and frontend `OrderTracking.tsx` use the legacy hardcoded path. | **HIGH** |
| **Resolution Detection** | No resolution detector. | Customer saying "That worked, thanks!" gets low RAG confidence and triggers a new ticket. | **HIGH** |
| **RAG Argument Bug** | `app/pipeline/enricher.py` line 76 inverts `(client_id, query)` signature. | Knowledge lookup fails when `rag_id` is None. | **HIGH** |
| **API Ingestion Security** | `POST /process-email` has no auth or signature validation. | Any actor can trigger worker jobs under arbitrary `client_id`. | **HIGH** |

---

## 2. Technical Vulnerability Details

### A. Conversational Amnesia in Email Context
In [chat_history.py](file:///home/hyper_is_op/mail_ai_automation/app/chat_history.py):
```python
HISTORY_TTL = 3600  # 1 hour sliding window
```
- When a troubleshooting question is sent to a customer, the customer replies hours or days later.
- Redis evicts the key after 1 hour.
- [tasks.py](file:///home/hyper_is_op/mail_ai_automation/worker/tasks.py) loads history solely via `get_history(client_id, ctx.from_email, last_n=10)`.
- If the ticket has not yet been created, `get_ticket_history()` returns `None`.
- `email_logs` is not queried. The LLM receives `messages = []`, meaning it cannot evaluate whether previous diagnostic steps succeeded.

### B. Single Shared Collection Vector Store
In [vector_store.py](file:///home/hyper_is_op/mail_ai_automation/app/vector_store.py):
```python
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "mail_ai_knowledge")
```
- All client chunks reside within `mail_ai_knowledge`.
- Helper `search_all_clients()` exists for admin use, posing a high data-leak blast radius if an unauthenticated or compromised context passes `client_id="ALL"`.
- Fallback storage in `app/rag.py` writes to a single shared file `chroma_db/fallback_db.json`.

### C. Master Encryption Key
In [secrets_crypto.py](file:///home/hyper_is_op/mail_ai_automation/app/secrets_crypto.py):
- All client secrets (API keys, OAuth refresh tokens, basic auth passwords) in `connector_configs.auth_secret_encrypted` use a single symmetric key.
- Lacks tenant envelope encryption or HKDF key derivation using client ID salts.

### D. Parameter Inversion Bug in Enricher
In [app/pipeline/enricher.py](file:///home/hyper_is_op/mail_ai_automation/app/pipeline/enricher.py):
```python
# app/pipeline/enricher.py:76
rag_res = query_knowledge(query, client_id, top_k=3)

# app/rag.py:239
def query_knowledge(client_id: str, query: str, top_k: int = 3) -> str:
```
- `query` is passed as `client_id`, and `client_id` is passed as `query`.
- Qdrant queries for a client whose ID matches the customer's raw email text, which always yields zero results.

---

## 3. Post-Implementation Resolution & Closure Matrix

All 12 identified vulnerabilities have been completely remediated, verified, and closed:

| Audit Item | Root Cause / Vulnerability | Remediation Applied | Resolution Status |
|---|---|---|---|
| **1. Multi-Turn Troubleshooting** | Bot auto-escalated to CRM ticket on Turn 1 when score $\le$ threshold. | Implemented 3-turn state machine in [`app/pipeline/evaluator.py`](file:///home/hyper_is_op/mail_ai_automation/app/pipeline/evaluator.py); guides back-and-forth troubleshooting with diagnostic transcript embedding on ceiling escalation. | **RESOLVED & VERIFIED** |
| **2. Conversational Memory** | Redis 1-hour sliding TTL caused complete amnesia on async customer replies. | Reconstituted dialogue from MySQL `email_logs` via [`get_history_from_sql()`](file:///home/hyper_is_op/mail_ai_automation/app/chat_history.py#L153) on Redis cache miss. | **RESOLVED & VERIFIED** |
| **3. RFC-822 Threading** | Ignored `In-Reply-To` and `References` headers. | Extracted RFC-822 headers in [`worker/imap_reader.py`](file:///home/hyper_is_op/mail_ai_automation/worker/imap_reader.py), indexed `thread_id`, `message_id`, and `in_reply_to` in MySQL `email_logs`. | **RESOLVED & VERIFIED** |
| **4. RAG Tenant Isolation** | Programmatic risk of cross-client data leak; single fallback file. | Added strict non-empty/non-`"ALL"` validation in [`app/vector_store.py`](file:///home/hyper_is_op/mail_ai_automation/app/vector_store.py); partitioned per-client JSON fallbacks `chroma_db/fallback_{client_id}.json` in [`app/rag.py`](file:///home/hyper_is_op/mail_ai_automation/app/rag.py). | **RESOLVED & VERIFIED** |
| **5. Credential Tenant Isolation** | Single global master Fernet key across all tenants. | Derived tenant-specific encryption keys using HKDF (`master_key + client_id`) in [`app/secrets_crypto.py`](file:///home/hyper_is_op/mail_ai_automation/app/secrets_crypto.py) with legacy key migration fallback. | **RESOLVED & VERIFIED** |
| **6. Payment Status Inquiries** | No payment trigger type, tool, or schema validation. | Added `payment_status` trigger to [`app/connector_config/validation.py`](file:///home/hyper_is_op/mail_ai_automation/app/connector_config/validation.py) and discrete tool `lookup_payment_status` in [`app/pipeline/tools.py`](file:///home/hyper_is_op/mail_ai_automation/app/pipeline/tools.py). | **RESOLVED & VERIFIED** |
| **7. Connector Routing** | Regex heuristics caused collisions on ambiguous queries. | Introduced discrete OpenAI function schemas for order, payment, and ticket lookups and explicit dispatchers in [`app/connector_config/dispatch.py`](file:///home/hyper_is_op/mail_ai_automation/app/connector_config/dispatch.py). | **RESOLVED & VERIFIED** |
| **8. Legacy Dual Connector Stack** | Split-brain between dynamic connectors and legacy hardcoded C-Zentrix endpoints. | Bridged [`app/order_routes.py`](file:///home/hyper_is_op/mail_ai_automation/app/order_routes.py) and [`app/mcp_server.py`](file:///home/hyper_is_op/mail_ai_automation/app/mcp_server.py) to check dynamic connectors first; updated operator UI [`frontend/src/pages/OrderTracking.tsx`](file:///home/hyper_is_op/mail_ai_automation/frontend/src/pages/OrderTracking.tsx). | **RESOLVED & VERIFIED** |
| **9. Resolution Detection** | Customer saying "that worked" was evaluated as low RAG confidence and opened ticket. | Added regex resolution detector in [`app/pipeline/evaluator.py`](file:///home/hyper_is_op/mail_ai_automation/app/pipeline/evaluator.py) triggering early-exit confirmation reply (score 95, 0 CRM tickets opened). | **RESOLVED & VERIFIED** |
| **10. RAG Argument Bug** | `app/pipeline/enricher.py` line 76 inverted `(client_id, query)`. | Fixed argument signature to `query_knowledge(client_id, query, top_k=3)`. | **RESOLVED & VERIFIED** |
| **11. API Ingestion Security** | `POST /process-email` was open without authentication. | Protected `POST /process-email` with `Depends(verify_ingestion_auth)` supporting `X-API-Key`, `X-Webhook-Secret`, and Bearer tokens in [`app/auth_deps.py`](file:///home/hyper_is_op/mail_ai_automation/app/auth_deps.py). | **RESOLVED & VERIFIED** |
| **12. Filter Order Precedence** | Sender rate limit evaluated before master bot switch, consuming limit on disabled tenants. | Reordered master bot switch before rate limit calculation in [`app/pipeline/filters.py`](file:///home/hyper_is_op/mail_ai_automation/app/pipeline/filters.py); exported `get_redis_client()` in [`app/rate_limiter.py`](file:///home/hyper_is_op/mail_ai_automation/app/rate_limiter.py). | **RESOLVED & VERIFIED** |

