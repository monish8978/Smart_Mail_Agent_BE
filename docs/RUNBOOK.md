# Operations Runbook & Incident Response Guide

This runbook provides actionable, step-by-step triage and disaster recovery procedures for platform engineers and on-call operators running the Mail AI Automation platform in staging and production.

---

## 1. Quick Incident Response (First 60 Seconds)

When an alert fires or customer reports indicate email processing has halted:

```bash
# 1. Check all container states
docker compose ps

# 2. Inspect real-time error logs across services
docker compose logs --tail=100 -f api worker listener embed_service

# 3. Query the deep health probe endpoint
curl -s http://localhost:8024/health | jq .
```

### Healthy Baseline State
```json
{
  "status": "healthy",
  "components": {
    "mysql": "connected",
    "redis": "connected",
    "qdrant": "connected",
    "embed_service": "connected"
  },
  "timestamp": "2026-10-01T12:00:00Z"
}
```

If `/health` returns `503 Service Unavailable`, jump immediately to the degraded component in Section 3 below.

---

## 2. Service Topology & Port Matrix

| Service | Container Name | Port | Critical Dependency | Key Process |
|---|---|---|---|---|
| API Gateway | `mail_ai_api` | `8024` | MySQL, Redis, Qdrant, Embed | Uvicorn / FastAPI |
| Pipeline Consumer | `mail_ai_worker` | — | MySQL, Redis, LLM APIs | Celery Worker |
| Mailbox Watcher | `mail_ai_listener` | — | Redis, Client IMAP servers | Python IMAP Loop |
| Embedding Engine | `mail_ai_embed_service` | `8500` | Redis | FastAPI / PyTorch CPU |
| Vector Store | `mail_ai_qdrant` | `6333` | Docker Volume | Qdrant Engine |
| Broker & Cache | `mail_ai_redis` | `6379` | Docker Volume | Redis 7 Server |
| Relational DB | `mail_ai_mysql` | `3306` | Docker Volume | MySQL 8.0 Server |

---

## 3. Component Triage & Recovery

### A. Celery Worker Stalls or High Queue Backlog (`mail_ai_worker`)

**Symptoms:** Inbound emails are detected by listener but customer replies stop going out; Redis queue size increases continuously.

**Triage:**
1. Check task consumption and active workers:
   ```bash
   docker exec mail_ai_worker celery -A worker.celery inspect active
   ```
2. Inspect queue depth in Redis:
   ```bash
   docker exec mail_ai_redis redis-cli llen celery
   ```
3. Check for unhandled exceptions or poison pill crashes:
   ```bash
   docker compose logs --tail=200 worker | grep -E "CRITICAL|ERROR|Poison Pill"
   ```

**Recovery:**
- **Soft Worker Restart** (preserves active in-flight tasks):
  ```bash
  docker compose restart worker
  ```
- **Purge Stale Tasks** (emergency only — when poison pills flood the queue):
  ```bash
  docker exec mail_ai_worker celery -A worker.celery purge -f
  ```
- **Check DB Deadlocks**:
  If workers are blocked waiting for database rows, inspect active MySQL transactions:
  ```bash
  docker exec mail_ai_mysql mysql -u root -p"$MYSQL_ROOT_PASSWORD" -e "SHOW ENGINE INNODB STATUS\G"
  ```

---

### B. Embedding Service Latency or OOM (`mail_ai_embed_service`)

**Symptoms:** Log warnings `⚠️ Embedding request timed out after 25s` or HTTP 503 errors during knowledge base ingestion or search.

**Root Causes:**
1. PyTorch CPU thread contention (defaulting to all cores instead of constrained pool).
2. Host memory exhaustion from concurrent embedding batches.

**Triage:**
1. Check container CPU & memory usage:
   ```bash
   docker stats mail_ai_embed_service --no-stream
   ```
2. Verify Redis vector query cache hit rates:
   ```bash
   docker exec mail_ai_redis redis-cli keys "embed:*" | wc -l
   ```

**Recovery:**
- Verify that `torch.set_num_threads(2)` is configured in `embed_service.py` to prevent CPU thread starvation.
- Restart the microservice:
  ```bash
  docker compose restart embed_service
  ```

---

### C. Qdrant Vector Store Failure & Fallback Recovery

**Symptoms:** API or worker logs show:
```
⚠️ Qdrant search failed: ... — falling back to JSON
```

**Operating Behavior:**
The system is built with **graceful degradation**. If Qdrant goes down, queries fall back transparently to `knowledge_fallback/` local JSON files, maintaining business continuity.

**Triage:**
1. Check Qdrant container health and HTTP port:
   ```bash
   curl -s http://localhost:6333/collections/mail_ai_knowledge | jq .
   ```
2. Check disk space on Qdrant volume mount:
   ```bash
   df -h /var/lib/docker/volumes/
   ```

**Recovery:**
1. Restart Qdrant:
   ```bash
   docker compose restart qdrant
   ```
2. **Re-sync Vector Store from Fallback JSON**:
   If the Qdrant collection is corrupted or deleted, repopulate it from the primary MySQL or local JSON stores using the maintenance task:
   ```bash
   docker exec mail_ai_api python -c "
   from app.rag import sync_all_fallback_to_qdrant
   sync_all_fallback_to_qdrant()
   "
   ```

---

### D. IMAP Mailbox Watcher Disconnects (`mail_ai_listener`)

**Symptoms:** Emails sent to client inboxes are not processed; no new tasks in Redis.

**Triage:**
1. Check listener logs for auth or connection rejections:
   ```bash
   docker compose logs --tail=100 listener
   ```
2. Verify client IMAP credentials and master bot switch status in MySQL:
   ```bash
   docker exec mail_ai_mysql mysql -u root -p"$MYSQL_ROOT_PASSWORD" -D mail_ai -e "
   SELECT client_id, email, is_active, bot_switch FROM email_accounts;
   "
   ```

**Recovery:**
- If an account shows `bot_switch = 0`, the client or administrator disabled the bot. Enable via dashboard or SQL:
  ```sql
  UPDATE email_accounts SET bot_switch = 1 WHERE client_id = '<CLIENT_ID>';
  ```
- Restart the listener daemon:
  ```bash
  docker compose restart listener
  ```

---

### E. LLM Provider Outages & Circuit Breakers

**Symptoms:** Logs show `🔌 Circuit breaker TRIPPED for provider 'groq' after 3 failures`.

**Operating Behavior:**
`app/llm_circuit_breaker.py` protects the pipeline from upstream LLM outages. Once tripped:
- In-flight tasks automatically fall back to secondary configured providers (OpenAI, Anthropic, Gemini, Ollama).
- The circuit breaker holds open for an exponential cooldown period (default 60s) before sending a single canary probe request to test provider recovery.

**Action:**
- Check provider status dashboards (e.g. status.groq.com, status.openai.com).
- To manually reset circuit breakers across all providers without restarting containers:
  ```bash
  docker exec mail_ai_api python -c "
  from app.llm_circuit_breaker import reset_all_circuit_breakers
  reset_all_circuit_breakers()
  "
  ```

---

## 4. Disaster Recovery & Backups

### Database Backup
Run daily snapshots of the relational state:
```bash
docker exec mail_ai_mysql mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" \
  --single-transaction --quick mail_ai > backup_mail_ai_$(date +%F).sql
```

### Database Restore
```bash
docker exec -i mail_ai_mysql mysql -u root -p"$MYSQL_ROOT_PASSWORD" mail_ai < backup_mail_ai_2026-10-01.sql
```

### Encryption Key Safeguard
The `CONNECTOR_SECRET_ENCRYPTION_KEY` variable in `.env` encrypts all stored client credentials.
> [!CAUTION]
> If this key is lost, all connector credentials in `connector_configs` become unrecoverable and must be re-entered by clients. Ensure `.env` is backed up in a secure secret manager (e.g. HashiCorp Vault or AWS Secrets Manager).

---

## 5. Routine Maintenance Commands

```bash
# Run complete test verification suite
docker exec mail_ai_api python -m unittest discover tests

# Tail all errors across services simultaneously
docker compose logs --tail=50 -f | grep -E "ERROR|CRITICAL|Traceback"

# Clean up stale Docker resources
docker system prune -f
```
