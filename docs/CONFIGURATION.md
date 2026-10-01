# System Configuration & Runtime Tuning Reference

This reference details all environment variables, runtime tunables, circuit breakers, and database partitioning parameters used across the Mail AI Automation platform.

---

## 1. Environment Variable Reference (`.env`)

### Database (MySQL 8.0)
| Variable | Default / Example | Purpose |
|---|---|---|
| `DB_HOST` | `localhost` / `mysql` | Hostname of the primary relational database. |
| `DB_USER` | `mail_ai` | Relational user account with DDL and DML permissions. |
| `DB_PASS` | `****` | Password for database user. |
| `DB_NAME` | `ai_mail_bot` | Primary relational database name. |
| `ADMIN_EMAIL` | `admin@example.com` | Root administrator account seeded on first startup. |
| `ADMIN_PASSWORD` | `****` | Initial password for the root administrator account. |

### Redis Broker & Database Partitioning
The platform partitions Redis logical databases to prevent key namespace collisions and memory contention:

| Variable | Default URL | Partition Role |
|---|---|---|
| `REDIS_URL` | `redis://mail_ai_redis:6379/0` | **DB 0**: Celery task message broker and task result backend. |
| `REDIS_HISTORY_URL` | `redis://mail_ai_redis:6379/1` | **DB 1**: Multi-turn conversation chat history cache (`chat_history:CLI-*:th_*`). |
| `REDIS_SESSION_URL` | `redis://mail_ai_redis:6379/2` | **DB 2**: Web user session locks, rate limiters, and vector query caches. |

### LLM Provider Credentials
| Variable | Default / Example | Purpose |
|---|---|---|
| `GROQ_API_KEY` | `gsk_...` | Groq API key for high-speed inference. |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Default system model used for agent reasoning and scoring. |
| `OPENAI_API_KEY` | `sk-...` | (Optional) OpenAI API key for GPT-4o / GPT-4o-mini fallbacks. |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | (Optional) Anthropic API key for Claude 3.5 Sonnet fallbacks. |
| `GEMINI_API_KEY` | `AIza...` | (Optional) Google Gemini API key. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | (Optional) Self-hosted Ollama local inference endpoint. |

### Vector Knowledge Base & Embedding Microservice
| Variable | Default | Purpose |
|---|---|---|
| `QDRANT_HOST` | `mail_ai_qdrant` | Hostname of the Qdrant vector database container. |
| `QDRANT_PORT` | `6333` | REST port for Qdrant vector retrieval. |
| `QDRANT_COLLECTION` | `mail_ai_knowledge` | Multi-tenant collection name partitioned by `client_id` payload filters. |
| `EMBED_SERVICE_URL` | `http://mail_ai_embed_service:8500` | Standalone PyTorch microservice generating 384-dim embeddings. |

### Security & Cryptography
| Variable | Requirement | Purpose |
|---|---|---|
| `CONNECTOR_SECRET_ENCRYPTION_KEY` | 32-byte Base64 Fernet Key | Symmetric key encrypting all client CRM passwords and tokens at rest. |
| `INGESTION_API_KEY` | High-entropy string | Secret token required in `X-API-Key` for external `POST /process-email` webhooks. |
| `CORS_ORIGINS` | Comma-separated URLs | Permitted web origins (`http://localhost:1947`, `http://172.16.3.215:1947`, etc.). |

---

## 2. Runtime Concurrency & Threading Tunables

### Embedding Microservice (`mail_ai_embed_service`)
To prevent PyTorch from attempting to consume all available host CPU cores (which starves the Celery workers), CPU threading is strictly capped:
```yaml
environment:
  - OMP_NUM_THREADS=2
  - MKL_NUM_THREADS=2
  - TORCH_NUM_THREADS=2
mem_limit: 1.5g
```
- **Redis Query Cache**: Vector queries are SHA-256 hashed and cached in Redis with a 24-hour TTL, achieving sub-millisecond retrieval (0.7ms) on recurring queries.

### Celery Worker Pool (`mail_ai_worker`)
```bash
celery -A worker.celery_worker.celery worker -B --schedule=/tmp/celerybeat-schedule --loglevel=info --concurrency=2
```
- `--concurrency=2`: Limits parallel email tasks per worker container to 2 concurrent threads to prevent MySQL connection exhaustion.
- `-B`: Runs the Celery beat periodic scheduler in-process for periodic mailbox polling.

### MySQL Connection Pool (`app/db.py`)
Utilizes `dbutils.pooled_db.PooledDB` with the following parameters:
- `maxconnections=20`: Absolute cap on concurrent open database connections.
- `mincached=2`: Idle connections kept open in the pool.
- `blocking=True`: Requests wait when the pool is saturated rather than throwing immediate connection errors.

---

## 3. Pipeline Thresholds & Circuit Breaker Constants

| Parameter | Location | Default Value | Behavioral Impact |
|---|---|---|---|
| `CONFIDENCE_FLOOR` | `app/scoring.py` | `60` | Hard floor. Scores below 60 are unconditionally diverted from auto-send. |
| `CIRCUIT_BREAKER_FAILURES` | `app/llm_circuit_breaker.py` | `3` | Consecutive API errors before tripping provider breaker. |
| `CIRCUIT_BREAKER_COOLDOWN` | `app/llm_circuit_breaker.py` | `60s` | Hold time before probing recovering provider. |
| `IDEMPOTENCY_TTL` | `app/action_outbox.py` | `60s` | Time window deduplicating identical external actions. |
| `MAX_TASK_RETRIES` | `worker/tasks.py` | `2` | Poison pill retry limit before dead-lettering email. |

---

## 4. Frontend Runtime Configuration

The web client (`frontend/`) supports zero-rebuild runtime configuration injection:

1. **`public/config.js`**: Loaded before the React bundle mounts:
   ```javascript
   window.__APP_CONFIG__ = {
     API_URL: "http://172.16.3.215:8024",
     WS_URL: "ws://172.16.3.215:8024",
     BASENAME: "/Smart_Mail_Agent"
   };
   ```
2. **Fallback Chain**:
   - `BASE_URL`: `(window.__APP_CONFIG__?.API_URL || import.meta.env.VITE_API_URL || '/api')`
   - `WS_URL`: `(window.__APP_CONFIG__?.WS_URL || import.meta.env.VITE_WS_URL || derived from window.location)`
   - `Router`: Uses `basename={window.__APP_CONFIG__?.BASENAME || undefined}` allowing single-bundle deployment on root or subpaths.
