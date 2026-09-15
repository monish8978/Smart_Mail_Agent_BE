# Implementation Roadmap & Milestones

This document tracks the phased implementation plan required to align the **Mail AI Automation** codebase with production requirements: tenant isolation, RFC-822 email threading, stateful troubleshooting, and multi-system connectors (orders, tickets, payments).

---

## Milestone 1: Thread Persistence & Conversational Troubleshooting Engine
**Objective:** Replace fragile 1-hour Redis-only history with persistent SQL conversation threading and a multi-turn diagnostic state machine.

- [x] **1.1 RFC-822 Email Threading Ingestion**
  - Update `worker/imap_reader.py` to extract `Message-ID`, `In-Reply-To`, and `References`.
  - Pass header metadata into Celery task payload.
- [x] **1.2 Schema Extension for Conversation Threads**
  - Add `thread_id`, `message_id`, and `in_reply_to` columns to `email_logs`.
  - Implement conversation thread linkage (group by `thread_id` or match `In-Reply-To` with previous `message_id`).
- [x] **1.3 Persistent History Fallback**
  - Update `app/chat_history.py`: when Redis cache misses, reconstruct thread history from `email_logs`.
- [x] **1.4 Troubleshooting State Machine & Tools**
  - Introduce `troubleshooting_state` into `PipelineContext` (`step_count`, `max_steps=3`, `status`).
  - Add resolution detection (e.g. "That solved it" $\rightarrow$ mark resolved, do not cut ticket).
  - Escalate to ticket creation only after troubleshooting fails or reaches the maximum step threshold.

---

## Milestone 2: Multi-Tenancy & Credential Hardening
**Objective:** Prevent cross-tenant data leaks and isolate external API credentials.

- [x] **2.1 Fix RAG Parameter Inversion**
  - Correct `app/pipeline/enricher.py` line 76 argument order to `query_knowledge(client_id, query)`.
- [x] **2.2 Tenant-Partitioned Vector Storage**
  - Update `app/vector_store.py` to enforce strict runtime validation that rejects empty or `ALL` in query/mutation paths.
  - Partition fallback store to isolated per-client files `chroma_db/fallback_{client_id}.json`.
- [x] **2.3 Tenant-Derived Credential Encryption**
  - Update `app/secrets_crypto.py` to derive tenant-specific Fernet keys via HKDF (`master_key + client_id`).
  - Provide a migration path for existing encrypted secrets via master key fallback.
- [x] **2.4 Ingestion Endpoint Authentication**
  - Add API key / webhook secret / Bearer session validation to `POST /process-email` in `app/api/emails.py`.

---

## Milestone 3: Connectors & Multi-System Status Capabilities
**Objective:** Support payments, orders, and tickets across Zoho Desk, Shopify, and Freshdesk without keyword heuristic collisions.

- [x] **3.1 Schema & Trigger Types Extension**
  - Add `payment_status` trigger type to `connector_configs` validation and UI (`REQUIRED_RESPONSE_FIELDS` requires `payment_status`).
  - Update frontend components (`PayloadConfig.tsx`, `ConnectorEditorModal.tsx`, `AiTemplateModal.tsx`, `DraftFilterBar.tsx`).
- [x] **3.2 Discrete Agent Tools**
  - Refactor `app/pipeline/tools.py` into distinct tools:
    - `lookup_order_status(order_id)`
    - `lookup_payment_status(payment_id_or_order_id)`
    - `lookup_ticket_status(ticket_id)`
    - `lookup_ticket_or_order_status(ticket_id)` (legacy backward compatibility)
  - Replace ambiguous regex routing in `app/connector_config/dispatch.py` with explicit functions: `run_order_status_lookup`, `run_payment_status_lookup`, and `run_ticket_status_lookup`.
- [x] **3.3 Response Mapping Enhancements & Split-Brain Bridge**
  - Standardize payment response mapping schema (`payment_status`, `amount`, `transaction_id`, `status`).
  - Bridge `app/order_routes.py` and `app/mcp_server.py` to query dynamic connectors first before falling back to legacy C-Zentrix GET tables.

---

## Milestone 4: End-to-End Verification & Regression Testing
**Objective:** Validate all scenarios with automated unit and integration suites.

- [x] **4.1 Troubleshooting Multi-Turn Test**
  - Test Turn 1 (diagnostic step 1 sent), Turn 2 (step failed $\rightarrow$ step 2 sent), Turn 3 (3-turn ceiling $\rightarrow$ ticket created with diagnostic transcript).
  - Test Resolution Path (step succeeded $\rightarrow$ issue resolved, no ticket opened, zero CRM tickets generated).
- [x] **4.2 Asynchronous Reply Simulation**
  - Verify that replies arriving after Redis TTL expiration retain full conversational context via SQL `get_history_from_sql()`.
- [x] **4.3 Cross-Tenant Isolation & Discrete Routing Test**
  - Verify Tenant A cannot query Tenant B's RAG data, decrypt Tenant B's credentials, or access Tenant B's connectors.
  - Verify discrete routing of order, payment, and ticket lookups without cross-system interference.

---

## Post-Milestone Production Rollout
**Objective:** Expose unified status APIs for dashboard operators, verify container health across restarts, and execute live end-to-end sandbox verification.

- [x] **Step 1: REST API Parity for Operator Lookups**
  - Added `POST /payment-status` and `POST /ticket-status` endpoints to `app/api/connectors.py`.
  - Added frontend API wrappers in `frontend/src/lib/api/emails.ts` and unified multi-system tabbed view in `frontend/src/pages/OrderTracking.tsx`.
- [x] **Step 2: Container Process Restart & Health Check**
  - Restarted `mail_ai_worker`, `mail_ai_listener`, `mail_ai_api`.
  - Verified clean Redis pool initialization, Celery beat, Qdrant indices, and zero crash loops.
- [x] **Step 3: Live Sandbox Ingestion Verification**
  - Authenticated ingestion via `POST /process-email` with `X-API-Key`.
  - Verified Celery task execution, LLM tool dispatch (`lookup_payment_status`), fallback draft generation, AI issue summary, and MySQL state persistence.

