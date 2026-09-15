# Current System State Snapshot

**Last Updated:** 2026-09-15 11:18 IST  
**Git Commit/Branch:** `main` (Production Hardened: Thread Isolation, Stateful Diagnostic Loop & RAG Batching Verified)

---

## 1. System Architecture Overview

```
[Inbound Email (IMAP / REST)] 
       │ (MIME Headers: Message-ID, In-Reply-To, References, X-API-Key / Bearer)
       ▼
[Celery Worker: process_email_task]
       │
       ├─► [Thread Linkage & History Fallback] (RFC-822 Linkage + Reply-Only Subject Match + SQL Reconstitution)
       │
       ├─► [Deterministic Filters] (Bounces, Marketing, Paused, Keywords, Sender Rate Limit: 30/hr)
       │
       ├─► [Stateful Diagnostic Agent] (3-Turn Problem Diagnosis [Turns 1-3] + Resolution Detector)
       │         │
       │         ├─► [lookup_order_status] (Shopify / WooCommerce / ERP order lookup)
       │         ├─► [lookup_payment_status] (Stripe / Razorpay / Zoho Books payment lookup)
       │         ├─► [lookup_ticket_status] (Zoho Desk / Freshdesk ticket lookup)
       │         ├─► [search_knowledge_base] (Qdrant RAG + Per-tenant JSON fallback)
       │         └─► [escalate_and_create_ticket] (Triggered on Turn 4+ or unresolvable failure)
       │
       ├─► [Outbox / SMTP / Drafts] (Idempotent delivery)
       │
       └─► [Logging & Notification] (email_logs with thread_id/troubleshooting_step + Redis Pub/Sub)
```

---

## 2. Component Health & Status

| Subsystem | Underlying Technology | Current Status | Capabilities & Hardening |
|---|---|---|---|
| **Queue / Worker** | Celery + Redis (DB 0) | Operational | Fully thread-aware (`resolve_thread_id`), tracks `troubleshooting_step`, runs background outbox sweeper. |
| **Database** | MySQL 8.0 / MariaDB (PyMySQL) | Operational | Schema updated: `message_id`, `in_reply_to`, `thread_id`, `is_resolved`, `troubleshooting_step` indexed; `ticket_record` auto-migrated with `sentiment` and `priority`. |
| **Vector DB** | Qdrant (`mail_ai_knowledge`) | Production Hardened | Single collection, `client_id` payload isolation, full-text indexes (`content`, `title`), cosine floor threshold (`min_score >= 0.68`). |
| **Credential Crypto** | `app/secrets_crypto.py` (HKDF + Fernet) | Hardened | Tenant-derived Fernet keys via HKDF (`client_id` salt) with legacy master key fallback. |
| **Ingestion Auth** | FastAPI (`app/auth_deps.py`) | Secured | `POST /process-email` protected by API key / webhook secret (`INGESTION_API_KEY`) or Bearer session. |
| **Embeddings** | `embed_service.py` (FastAPI / e5-small) | Production Hardened | 384-dimensional vector embeddings on port 8500. Client-side batching (`BATCH_SIZE=32`) in `app/embed_client.py` preventing 422 batch size overflows. |
| **Connectors & API** | `connector_executor.py` + `dispatch.py` | Operational | Supports `ticket_create`, `ticket_status`, `order_status`, `payment_status`. Dedicated operator REST endpoints (`POST /order-status`, `POST /payment-status`, `POST /ticket-status`). |
| **Chat History** | Redis (DB 1) + SQL Reconstitution | Operational | Redis hot cache backed by full cold-start SQL transcript reconstruction from `email_logs`. |
| **IMAP Listener** | `worker/imap_reader.py` | Operational | Captures RFC-822 `Message-ID`, `In-Reply-To`, and `References`. |
| **Frontend UI** | React + Vite + Tailwind | Operational | Unified multi-system **Status Lookup** workspace with tabbed switching (Orders, Payments, Support Tickets). |

---

## 3. Active Configuration & Environment

- **Database:** MySQL on `mail_ai_mysql:3306` (or localhost)
- **Redis Broker:** `redis://mail_ai_redis:6379/0`
- **Redis Cache/History:** `redis://mail_ai_redis:6379/1`
- **Qdrant:** `http://mail_ai_qdrant:6333`
- **RAG Score Floor:** `RAG_MIN_SCORE=0.68`
- **Embedding Service:** `http://mail_ai_embed_service:8500` (`EMBED_TIMEOUT_SECONDS=10`)
- **Sender Rate Limit:** `SENDER_HOURLY_RATE_LIMIT=30` (default: 30/hour per client/sender)
- **Ingestion Key:** `INGESTION_API_KEY` configured in `.env`
- **Master Encryption Key:** `CONNECTOR_SECRET_ENCRYPTION_KEY` configured in `.env`
- **Default LLM Provider:** Groq (`qwen/qwen3.6-27b` / `llama-3.3-70b-versatile`)

- **Test Suite Status:** 82/82 automated tests passing (0 failures, 0 errors) across 12 test modules; 4/4 live cluster real-world scenarios verified end-to-end.
