# Mail AI Automation Platform

An enterprise-grade autonomous email processing, support triage, and customer response platform. Built on FastAPI, Celery, Redis, MySQL, Qdrant, and React, the system combines multi-provider LLM intelligence, semantic document retrieval (RAG), dynamic third-party API connectors, and automated human-in-the-loop review workflows.

---

## 📚 Documentation Hub

| Document | Target Persona | Focus / Description |
|---|---|---|
| 📖 [**Architecture & Execution Flow**](file:///home/hyper_is_op/mail_ai_automation/docs/ARCHITECTURE_FLOW.md) | Backend & Integration Engineers | Mermaid lifecycle state charts, 4-stage pipeline specifications, and Celery task mechanics. |
| 🧑‍💼 [**Operator & Client Guide**](file:///home/hyper_is_op/mail_ai_automation/docs/OPERATOR_GUIDE.md) | Support Leads & Tenant Operators | Non-technical manual: live review queues, pause controls, keyword filters, and monthly budgets. |
| 🗄️ [**Database Schema & ERD**](file:///home/hyper_is_op/mail_ai_automation/docs/DATABASE_SCHEMA.md) | DBAs & Backend Engineers | Complete entity-relationship model and data dictionary for all 15+ relational tables. |
| ⚙️ [**Configuration & Tuning**](file:///home/hyper_is_op/mail_ai_automation/docs/CONFIGURATION.md) | DevOps & System Administrators | Environment variables, Redis database partitioning, thread tunables, and circuit breakers. |
| 🛠️ [**Operations Runbook**](file:///home/hyper_is_op/mail_ai_automation/docs/RUNBOOK.md) | DevOps & On-Call Engineers | Incident response, deep `/health` probes, component triage (Celery, Qdrant, PyTorch), and recovery. |
| 🔌 [**REST API Reference**](file:///home/hyper_is_op/mail_ai_automation/docs/API_REFERENCE.md) | External Integrators | Direct email injection schemas, connector governance endpoints, and webhook error contracts. |
| 📋 [**Release Changelog**](file:///home/hyper_is_op/mail_ai_automation/CHANGELOG.md) | All Stakeholders | SemVer release history tracking enhancements, bugfixes, and dependency changes. |

---

## 1. System Architecture

The platform runs as a distributed multi-container application orchestrated via Docker Compose:

```
                           Incoming Emails (IMAP / Ingestion API)
                                           │
                                           ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   WORKER SERVICES                                      │
│                                                                                        │
│  ┌─────────────────────────┐                     ┌──────────────────────────────────┐  │
│  │   mail_ai_listener      │                     │        mail_ai_worker            │  │
│  │   (IMAP Poller / Idle)  │────── Redis ───────▶│        (Celery Worker)           │  │
│  └─────────────────────────┘      Queues         └─────────────────┬────────────────┘  │
└────────────────────────────────────────────────────────────────────┼───────────────────┘
                                                                     │
                                    ┌────────────────────────────────┼────────────────────────────────┐
                                    ▼                                ▼                                ▼
┌──────────────────────────────────────┐  ┌──────────────────────────────────────┐  ┌─────────────────────────────────┐
│           CORE DATA TIERS            │  │          VECTOR & EMBEDDINGS         │  │       EXTERNAL LLM ROUTING      │
│                                      │  │                                      │  │                                 │
│ • MySQL 8.0: Multi-tenant schemas,   │  │ • Qdrant: Multi-tenant collections,  │  │ • Multi-Provider Router:        │
│   logs, credentials, templates       │  │   payload metadata filtering         │  │   Groq, OpenAI, Anthropic,       │
│ • Redis 7: Task queues, outbox locks,│  │ • knowledge_fallback: Resilient      │  │   Google Gemini, Ollama          │
│   chat history, embeddings cache     │  │   local JSON document fallback       │  │ • Circuit Breakers: Auto-trip & │
│                                      │  │ • PyTorch Embed Service (port 8500)  │  │   exponential cooldown           │
└──────────────────────────────────────┘  └──────────────────────────────────────┘  └─────────────────────────────────┘
                                                                     ▲
                                                                     │
┌────────────────────────────────────────────────────────────────────┴───────────────────┐
│                                 REST & WEBSOCKET APIS                                  │
│                                                                                        │
│  ┌─────────────────────────┐                     ┌──────────────────────────────────┐  │
│  │      mail_ai_api        │◀──── WebSockets ────│     Smart_Mail_Agent_FE          │  │
│  │   (FastAPI Application) │      REST APIs      │  (React 18 + Vite + Tailwind UI) │  │
│  └─────────────────────────┘                     └──────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Core Services

| Service Name | Technology | Description |
|---|---|---|
| `mail_ai_api` | FastAPI / Uvicorn | High-performance REST API and real-time WebSocket hub for dashboards, configuration, and telemetry. |
| `mail_ai_worker` | Celery / Python | Distributed task consumer executing the multi-stage autonomous email processing pipeline. |
| `mail_ai_listener` | Python / IMAP | Mailbox watcher monitoring client IMAP inboxes for inbound messages and queueing tasks into Redis. |
| `mail_ai_embed_service` | PyTorch / FastAPI | Dedicated CPU-optimized embedding microservice (`BAAI/bge-small-en-v1.5`) with Redis query caching. |
| `mail_ai_qdrant` | Qdrant Vector DB | High-speed vector database for tenant knowledge base indexing and semantic retrieval. |
| `mail_ai_redis` | Redis 7 | Message broker for Celery, distributed locks, outbox idempotency, and cache tier. |
| `mail_ai_mysql` | MySQL 8.0 | Primary relational persistence for multi-tenant accounts, logs, connector templates, and alerts. |

---

## 2. Autonomous Email Pipeline

Every incoming email is processed through a strict, multi-stage pipeline designed for safety, precision, and auditability:

```
Incoming Email
     │
     ▼
[ Stage 1: Filters ] ─────────▶ Master Bot Switch OFF / Daemon Bounce / Keyword Blocked?
     │ (Passed)                 └──▶ Drop or Route to Review Queue
     ▼
[ Stage 2: Enricher ] ────────▶ Extract Intent, Sentiment, Urgency & Tenant Policies
     │
     ▼
[ Stage 3: Agent Loop ] ◀─────▶ Tool Execution:
     │                          • search_knowledge_base (Semantic RAG)
     │                          • lookup_order_status (Dynamic Connector)
     │                          • lookup_payment_status (Dynamic Connector)
     │                          • lookup_ticket_status (Dynamic Connector)
     │                          • escalate_and_create_ticket (CRM Connector)
     │                          • request_customer_clarification (Multi-turn)
     ▼
[ Stage 4: Evaluator ] ───────▶ Confidence Scoring & Policy Adherence Verification
     │
     ├── Score >= Threshold & Auto-Send Enabled ──▶ Automated Outbound SMTP Send
     └── Score < Threshold or Requires Review  ──▶ Route to Human Pending Review Queue
```

1. **Stage 1: Filters (`app/pipeline/filters.py`)**
   - **Bot Switches**: Validates tenant-level master switch and automated features.
   - **Bounce & Daemon Detection**: Filters out delivery status notifications, auto-responders, and mailer-daemons to eliminate infinite reply loops.
   - **Keyword Policies**: Checks sender and body against client keyword blocking rules, dual-writing to `paused_email_history` or `reply_blocked_by_keyword`.
2. **Stage 2: Enricher (`app/pipeline/enricher.py`)**
   - Classifies customer intent (inquiry, complaint, tracking, cancellation), sentiment (positive, neutral, negative), and priority.
   - Hydrates tenant SLA and threading metadata.
3. **Stage 3: Agent Loop & Tools (`app/pipeline/agent.py`, `app/pipeline/tools.py`)**
   - Multi-turn autonomous agent maintaining state across conversation turns.
   - Executes authorized tools against knowledge bases or external CRM/ERP systems.
   - Handles ambiguity: if multiple tickets or orders are detected, requests clarification instead of hallucinating.
4. **Stage 4: Evaluator (`app/pipeline/evaluator.py`)**
   - Scores draft answers (0–100) based on factual grounding, completeness, and tone.
   - Gating logic: Only replies exceeding the client's configured confidence threshold are auto-sent. Low-confidence drafts route safely to human operators.

---

## 3. Dynamic Connector System

The platform replaces legacy hardcoded API endpoints with a generic, admin-governed connector engine (`app/connector_executor.py`). It enables integration with arbitrary third-party CRM, Helpdesk, or ERP APIs (e.g. Zoho Desk, Zendesk, Salesforce) without writing custom code per provider.

### Core Architecture
- **Generic Execution**: One unified executor renders templates, manages authentication, dispatches HTTP requests, and extracts responses via JMESPath expressions.
- **SSRF Prevention (`url_allowlist`)**: All connector target URLs must match an admin-approved URL allowlist (`scheme`, `netloc`, `path`). Checked at configuration approval and re-verified at runtime execution.
- **Fernet Secret Encryption (`app/secrets_crypto.py`)**: Auth secrets (Bearer tokens, Basic Auth, API keys) are Fernet-encrypted at rest and decrypted only in-memory during dispatch.
- **Safe Template Substitution**: Templates use `{{key}}` placeholders populated from `CONTEXT_DATA_KEYS`. Values are JSON-escaped before substitution to prevent structure injection.
- **Zero-Downtime Reconfiguration**: Updating a live connector creates a new `pending_approval` revision while the existing `live` revision continues serving traffic uninterrupted.

### Database Schema Overview
- `connector_configs`: Stores HTTP method, URL, headers template, request template, response mapping (JMESPath), auth configuration, and lifecycle state (`draft`, `pending_approval`, `live`, `disabled`).
- `url_allowlist`: Admin-curated table of permitted domain and path prefixes.
- `paused_email_history`: Actionable review queue for emails received while customer conversations are paused.

---

## 4. Resilience & Fallbacks

- **Vector Search Fallback**: If Qdrant becomes unreachable, semantic queries fall back transparently to `knowledge_fallback/` local JSON stores.
- **LLM Circuit Breakers (`app/llm_circuit_breaker.py`)**: Tracks failure rates across each LLM provider. Trips after repeated timeouts or 5xx responses, failing over to alternative providers with automated exponential cooldown recovery.
- **Outbox Idempotency (`app/action_outbox.py`)**: Prevents duplicate side-effects (e.g. creating multiple tickets for the same email) by enforcing Redis/MySQL transaction locks on action keys.
- **Poison Pill Circuit Breakers (`worker/tasks.py`)**: Malformed emails that crash the worker pipeline are dead-lettered after retry limits to protect the worker pool from starvation.

---

## 5. Multi-Provider LLM & Telemetry

- **Supported Providers**: Groq, OpenAI, Anthropic, Google Gemini, Ollama.
- **Granular Token & Cost Accounting**: Every LLM invocation records prompt tokens, completion tokens, latency (ms), and cost in `llm_logs`.
- **Tenant Quotas & Budgets**: Real-time tracking against client-allocated monthly budgets with automated alert dispatching.

---

## 6. Frontend SaaS Application (`frontend/`)

Modern enterprise interface built with React 18, Vite, TypeScript, and Microsoft Fluent-inspired design aesthetics:
- **Operations Dashboard**: Real-time traffic KPIs, latency monitors, confidence distributions, and LLM telemetry.
- **Live Inbox**: WebSocket-powered continuous inbox streaming with multi-turn conversation thread views, manual reply overrides, and audit trails.
- **Knowledge Base Workspace**: Document upload (PDF, TXT, Excel), automated chunk inspection, and an interactive semantic retrieval test sandbox.
- **Payload & Connector Studio**: Interactive modal for drafting, testing, and submitting dynamic CRM connectors with live LLM template generation.
- **Client & LLM Administration**: Multi-tenant client provisioning, model assignment, and monthly budget limits.

---

## 7. Development & Deployment

### Prerequisites
- Docker Engine 24+ & Docker Compose v2+
- Node.js 18+ & pnpm (for frontend development)
- Python 3.10+ (for local CLI tool development)

### Quick Start (Docker Compose)

1. **Clone the repository and prepare environment variables**:
   ```bash
   cp .env.example .env
   # Edit .env with your database credentials, LLM API keys, and secret keys
   ```

2. **Launch all services**:
   ```bash
   docker compose up -d --build
   ```

3. **Verify running containers**:
   ```bash
   docker compose ps
   ```

4. **Access Applications**:
   - **Frontend UI**: `http://localhost:1947`
   - **Backend REST API**: `http://localhost:8024`
   - **API Documentation (Swagger)**: `http://localhost:8024/docs`
   - **Embedding Service**: `http://localhost:8500/health`
   - **Qdrant Vector DB**: `http://localhost:6333/dashboard`

---

## 8. Automated Testing

The platform maintains an automated test suite verifying units, concurrency, threading, connectors, and end-to-end conversation flows.

To run the complete test suite inside the running API container:
```bash
docker exec mail_ai_api python -m unittest discover tests
```

### Test Coverage Highlights
- `tests/test_threading.py`: Thread resolution, parent/child association, Redis thread key normalization.
- `tests/test_connectors_payment.py`: Dynamic connector execution, JMESPath extraction, order/payment lookups.
- `tests/test_back_and_forth_scenarios.py`: Multi-turn conversational support flows, clarification loops, and state retention.
- `tests/test_rag_pipeline.py`: Qdrant vector retrieval, similarity score thresholds, and local JSON fallbacks.
- `tests/test_realworld_scenarios.py`: Full end-to-end integration covering LLM calls, email parsing, and outbound dispatch.