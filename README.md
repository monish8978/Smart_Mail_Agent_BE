# Smart Mail Agent — Backend (Mail AI Automation Platform)

Comprehensive technical documentation and operational guide for the **Smart Mail Agent Backend**, an enterprise-grade autonomous email processing, classification, ticketing, and customer response orchestration platform.

---

## 1. System Architecture Overview

The backend is built around a decoupled, event-driven microservices architecture containerized with Docker Compose:

```
[Inbound Emails (IMAP)] ───────────┐
                                   ▼
 [Direct API Ingestion] ──► [FastAPI Application] ──► [Redis Broker] ──► [Celery Worker Pipeline]
                                  │                                              │
                                  ├─► [MySQL 8.0] (Metadata, Audit, Configs)    ├─► [Groq/OpenAI/Claude LLMs]
                                  ├─► [Redis Cache & Ratelimiting]               ├─► [Qdrant Vector Engine]
                                  └─► [Secrets Crypto / Fernet]                  ├─► [Microservice: Embed Service]
                                                                                 ├─► [Dynamic CRM Connectors]
                                                                                 └─► [Outbound SMTP Mailer]
```

### Core Components
1. **FastAPI Application (`app/main.py`)**:
   - Serves REST APIs and WebSockets on port `8024`.
   - Handles multi-tenant client authentication, admin management, telemetry statistics, RAG document uploads, CRM connector configuration lifecycle, and review queues.
2. **Celery Worker (`worker/celery_worker.py` & `worker/tasks.py`)**:
   - Asynchronous execution engine consuming jobs from Redis broker (`redis://mail_ai_redis:6379/0`).
   - Executes multi-step AI email processing pipeline: spam/marketing filtering, keyword sanitization, sentiment scoring, intent parsing, CRM docket status lookup, semantic RAG query, automated response evaluation, draft creation, and ticket generation.
3. **IMAP Listener (`worker/imap_reader.py`)**:
   - Daemon service managing multithreaded IMAP listeners per registered tenant mailbox.
   - Polls unread emails, cleans MIME headers and bodies, and schedules `process_email_task` onto Celery.
4. **Qdrant Vector Database (`mail_ai_qdrant:6333`)**:
   - Production vector database storing chunked document embeddings with tenant-level isolation via `client_id` payload filters.
5. **Embedding Microservice (`embed_service.py` / `mail_ai_embed_service:8500`)**:
   - Standalone FastAPI container hosting lightweight HuggingFace sentence transformer models (e.g. `all-MiniLM-L6-v2`) offloaded from workers.
6. **Relational Database (`MySQL`)**:
   - Stores tenant accounts, credentials, email logs, tickets, connector definitions, audit trails, and review queues.

---

## 2. Codebase Structure

```
Smart_Mail_Agent_BE/
├── app/
│   ├── main.py                  # FastAPI application entrypoint, routers & lifespan management
│   ├── auth.py                  # User authentication, JWT tokens, password hashing
│   ├── auth_deps.py             # FastAPI dependency injection for role checks (admin/client)
│   ├── db.py                    # MySQL connection pooling (DBUtils) and context managers
│   ├── llm.py                   # Multi-provider LLM factory (Groq, OpenAI, Anthropic, Gemini, Azure)
│   ├── scoring.py               # Reply evaluation & strict scoring heuristics (0–100 scale)
│   ├── rag.py                   # High-level RAG interface (Qdrant search + JSON fallback)
│   ├── vector_store.py          # Qdrant client connector & schema migrations
│   ├── embed_client.py          # HTTP client for the standalone embed_service
│   ├── connector_config.py      # CRM connector lifecycle, allowlist validation, state machines
│   ├── connector_executor.py    # Generic template renderer, JMESPath response extractor
│   ├── context_data.py          # Fixed vocabulary definitions for connector templating
│   ├── email_credential.py      # IMAP/SMTP account credential management
│   ├── email_disclaimers.py     # Tenant disclaimer append rules & DB storage
│   ├── draft_service.py         # Human-in-the-loop draft review, editing, and batch sending
│   ├── keyword_filter.py        # Blocked keywords detection & sanitize checks
│   ├── paused_email_history.py  # Paused sender review queue management
│   ├── chat_history.py          # Threaded customer interaction history (MySQL + Redis)
│   ├── mailer.py                # SMTP outbound transmission engine
│   ├── text_cleaning.py         # Strips email reply headers, signatures, and HTML formatting
│   ├── url_allowlist.py         # SSRF protection and external webhook domain allowlist
│   ├── secrets_crypto.py        # Fernet AES-128-CBC encryption for connector secrets & passwords
│   ├── rate_limiter.py          # Redis token bucket rate limiters
│   ├── langgraph_agent.py       # Standalone LangGraph ReAct orchestration agent
│   └── mcp_server.py            # FastMCP server exposing platform tools to AI agents
├── worker/
│   ├── celery_worker.py         # Celery worker initialization and warmup signals
│   ├── tasks.py                 # Core asynchronous email execution pipeline
│   ├── imap_reader.py           # Multi-tenant IMAP reader daemon
│   └── credential_service.py    # IMAP credential provider for workers
├── chroma_db/                   # Shared fallback JSON vector storage path (container mount)
├── embed_service.py             # Standalone sentence embedding microservice
├── Dockerfile                   # Main container definition (FastAPI, Worker, Listener)
├── Dockerfile.embed             # Embed service container definition
├── docker-compose.yml           # Multi-service stack orchestration definition
├── requirements.txt             # Main Python dependencies
├── requirements.embed.txt       # Embed microservice dependencies
└── .env                         # Environment variables and secrets
```

---

## 3. Email Ingestion & Processing Pipeline

Incoming messages processed by `worker/tasks.py::process_email_task` pass through the following deterministic pipeline:

```
[Inbound Email]
       │
       ▼
[Idempotency Check] ──────► (celery_task_log prevents duplicate runs)
       │
       ▼
[HTML / Text Sanitization] ──► (Strips quoted replies & signatures)
       │
       ▼
[Master Bot Automation Check] ──► (Halt if admin or tenant switch is OFF)
       │
       ▼
[Paused Sender Check] ───────► (If paused: Route to paused_email_history review queue)
       │
       ▼
[Blocked Keywords Check] ────► (If blocked: Route to reply_blocked_by_keyword review queue)
       │
       ▼
[Marketing / Promo Check] ───► (Auto-ignore promotional or spam blasts)
       │
       ▼
[LLM Intent & Sentiment] ────► (Classify: Sentiment, Priority, Intent, Ticket IDs)
       │
       ├────────────────────────────────────────┬────────────────────────────────────────┐
       ▼                                        ▼                                        ▼
 [PATH A: Ticket Status]                [PATH B: Knowledge Base]                 [PATH C: Escalation]
 • Check referenced docket              • Semantic Qdrant lookup                 • If RAG low confidence or
 • Execute CRM connector                • Generate grounded AI reply              verification fails
 • Auto-reply or queue draft            • Score quality (scoring.py)             • Create ticket via connector
                                        • If score >= threshold: auto-reply      • Dispatch ticket confirmation
                                        • Else: fallback to Path C
```

### Review Queue & Draft Mode
- If a tenant has `feature_auto_send: false`, generated replies are saved to `draft_emails` with status `pending`.
- If an AI-generated reply scores below the tenant's `email_accounts.score_threshold` (default: 80), the system suppresses auto-send and triggers Path C ticket creation or queues for human review.

---

## 4. Dynamic CRM Connector System

The platform replaces legacy hardcoded webhooks with a generic, template-driven connector engine:

### 4.1 Schema (`connector_configs`)
- **Routing Key (`trigger_type`):** Generic trigger identifier (e.g., `order_status`, `ticket_create`).
- **Templates:** `request_template` and `headers_template` accept mustache-style placeholders (`{{from_email}}`, `{{subject}}`, `{{ticket_id}}`).
- **Response Mapping:** JMESPath queries map arbitrary external JSON responses to standardized internal fields (`docket_no`, `ticket_status`, `ticket_id`).
- **Security:** Strict SSRF URL allowlisting (`url_allowlist`) verified at both approval time and execution time.
- **Secrets Encryption:** API keys, Bearer tokens, and Basic Auth credentials are encrypted at rest using Fernet keys (`CONNECTOR_SECRET_ENCRYPTION_KEY`).

### 4.2 Lifecycle States
1. `draft`: Unrestricted tenant scratchpad.
2. `pending_approval`: Submitted for admin authorization.
3. `live`: Admin-approved and serving live production traffic.
4. `disabled`: Revoked or replaced configuration (preserved for audit logs).

---

## 5. Environment Variables & Configuration

Create a `.env` file in the project root:

```ini
# Database (MySQL)
DB_HOST=127.0.0.1
DB_USER=mail_admin
DB_PASS=YourSecurePassword
DB_NAME=ai_mail_bot_v2
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=InitialAdminPassword

# Default Primary LLM (Groq / OpenAI compatible)
GROQ_API_KEY=gsk_...
GROQ_MODEL=qwen/qwen3.6-27b

# Redis Cache & Celery Brokers
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_HISTORY_URL=redis://127.0.0.1:6379/1

# Encryption Key (Fernet AES-128 URL-safe base64-encoded 32-byte key)
CONNECTOR_SECRET_ENCRYPTION_KEY=Qkm0GHw1HpURirfXMaBt7qgAK9EvaDjT9qm1eDPVScE=

# Qdrant Vector DB & Embed Service
QDRANT_HOST=mail_ai_qdrant
QDRANT_PORT=6333
EMBED_SERVICE_URL=http://mail_ai_embed_service:8500
```

---

## 6. Deployment & Operations

### 6.1 Docker Compose Deployment
```bash
# Build and run all services in detached mode
docker-compose up --build -d

# View status of running containers
docker-compose ps

# Tail logs of worker and listener
docker-compose logs -f worker listener
```

### 6.2 Manual Service Commands (Bare Metal / Dev Mode)
```bash
# 1. Start FastAPI Web Server
uvicorn app.main:app --host 0.0.0.0 --port 8024 --reload

# 2. Start Celery Worker
celery -A worker.celery_worker.celery worker --loglevel=info --concurrency=1

# 3. Start IMAP Daemon
python worker/imap_reader.py

# 4. Start Standalone Embed Service
uvicorn embed_service:app --host 0.0.0.0 --port 8500
```

---

## 7. Key REST API Reference

| Method | Endpoint | Access | Description |
|---|---|---|---|
| `POST` | `/login` | Public | Authenticate user & issue JWT |
| `POST` | `/process-email` | Client/Admin | Manually submit email for pipeline processing |
| `GET` | `/dashboard/stats/{client_id}` | Client/Admin | Aggregated statistics, volumes & breakdown |
| `GET` | `/emails/{client_id}` | Client/Admin | Fetch processed email audit logs |
| `GET` | `/drafts` | Client/Admin | Query pending review queue drafts |
| `PUT` | `/drafts/{draft_id}` | Client/Admin | Edit draft reply body |
| `POST` | `/drafts/{draft_id}/send` | Client/Admin | Approve and dispatch draft via SMTP |
| `POST` | `/rag/upload` | Client/Admin | Upload document to Qdrant vector store |
| `POST` | `/admin/connector-configs` | Client/Admin | Create CRM webhook connector |
| `POST` | `/admin/connector-configs/{id}/approve`| Admin Only | Authorize connector for live traffic |
| `POST` | `/admin/master-bot-toggle` | Admin Only | Global kill-switch for all automation |
| `POST` | `/admin/llm-configs` | Admin Only | Provision tenant/global LLM providers |
