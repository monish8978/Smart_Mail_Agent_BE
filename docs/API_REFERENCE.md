# Mail AI Automation — REST API & Integration Reference

This reference documents the primary REST endpoints for external systems, webhook integrations, and client management.

---

## 1. Authentication & Headers

| Header | Format | Purpose |
|---|---|---|
| `Authorization` | `Bearer <JWT_TOKEN>` | Standard authentication for dashboard sessions and client/admin API routes. |
| `X-API-Key` | `<INGESTION_API_KEY>` | Service-to-service key for direct email ingestion webhooks (`POST /process-email`). |
| `Content-Type` | `application/json` | Required for all POST / PATCH requests with JSON bodies. |

---

## 2. Inbound Email Ingestion API

External CRM or email gateway systems can inject incoming emails directly into the autonomous pipeline:

### `POST /process-email`
Dispatches an email into the Celery task queue for autonomous agent processing.

- **Authentication**: `X-API-Key: <INGESTION_API_KEY>` or `Bearer <JWT_TOKEN>`
- **Request Body**:
```json
{
  "client_id": "CLI-ACME-01",
  "from_email": "customer@example.com",
  "to_email": "support@acme.com",
  "subject": "Where is my order ORD-99214?",
  "body": "Hi, I ordered 3 days ago and have not received a tracking update. Please check ORD-99214.",
  "message_id": "<20261001-99214@mail.example.com>",
  "in_reply_to": null,
  "references": null
}
```

- **Response (200 OK)**:
```json
{
  "success": true,
  "task_id": "8f9a2b1c-3d4e-5f6a-7b8c-9d0e1f2a3b4c",
  "status": "queued",
  "message": "Email queued for autonomous processing"
}
```

- **Error Codes**:
  - `400 Bad Request`: Missing mandatory fields (`client_id`, `from_email`, `body`).
  - `401 Unauthorized`: Invalid or missing API key.
  - `403 Forbidden`: Monitored account for `client_id` is inactive or disabled.

---

## 3. Dynamic Connector Admin API

Endpoints for configuring, testing, and governing third-party CRM and ERP connections.

### `POST /admin/connector-configs`
Create a new connector configuration in `draft` or `pending_approval` state.

- **Access**: Client (own `client_id`) or Admin.
- **Request Body**:
```json
{
  "client_id": "CLI-ACME-01",
  "trigger_type": "order_status",
  "http_method": "POST",
  "url": "https://api.acme-crm.com/v1/orders/lookup",
  "auth_type": "bearer",
  "auth_secret": "secret_token_abc123",
  "request_template": {
    "order_id": "{{ticket_id}}",
    "customer_email": "{{from_email}}"
  },
  "response_mapping": {
    "fields": [
      {
        "field": "ticket_status",
        "path": "order.delivery_status"
      },
      {
        "field": "docket_no",
        "path": "order.tracking_number"
      }
    ]
  },
  "status": "pending_approval"
}
```

- **Response (201 Created)**:
```json
{
  "id": 14,
  "client_id": "CLI-ACME-01",
  "trigger_type": "order_status",
  "version": 1,
  "status": "pending_approval",
  "created_at": "2026-10-01T12:00:00Z"
}
```

---

### `POST /admin/connector-configs/{id}/approve`
Promotes a `pending_approval` configuration to `live`.

- **Access**: **System Administrator Only**.
- **Behavior**:
  - Re-verifies URL against `url_allowlist` (SSRF prevention).
  - Validates `{{placeholder}}` keys against `CONTEXT_DATA_KEYS`.
  - Validates required fields for the trigger type (`REQUIRED_RESPONSE_FIELDS`).
  - Atomically swaps existing `live` config to `disabled` and sets new config to `live`.

- **Response (200 OK)**:
```json
{
  "success": true,
  "config_id": 14,
  "status": "live",
  "message": "Connector approved and live in production"
}
```

- **Error Codes**:
  - `400 Bad Request`: `TemplateValidationError` or `ResponseMappingValidationError`.
  - `403 Forbidden`: Caller lacks admin privileges or URL not in allowlist (`AllowlistViolationError`).
  - `409 Conflict`: `SwapRaceError` (the configuration is no longer in pending state).
  - `429 Too Many Requests`: Client has exceeded their maximum live connector cap.

---

### `POST /admin/connector-configs/generate-preview`
Uses an LLM to generate draft `request_template` and `response_mapping` JSON from a plain-English CRM description.

- **Access**: Client or Admin.
- **Request Body**:
```json
{
  "trigger_type": "order_status",
  "crm_schema_description": "Our CRM expects a POST request with orderId and customerEmail. It returns a JSON object with data.order_state and data.tracking_code.",
  "sample_response": "{\"data\": {\"order_state\": \"Shipped\", \"tracking_code\": \"TRK-102\"}}"
}
```

- **Response (200 OK)**:
```json
{
  "success": true,
  "request_template": {
    "orderId": "{{ticket_id}}",
    "customerEmail": "{{from_email}}"
  },
  "response_mapping": {
    "fields": [
      { "field": "ticket_status", "path": "data.order_state" },
      { "field": "docket_no", "path": "data.tracking_code" }
    ]
  }
}
```
*Note: This endpoint is preview-only and performs zero database writes.*

---

## 4. Operator Review & Manual Actions

### `POST /manual-reply`
Dispatches a human-written or approved AI draft directly to the customer.

- **Request Body**:
```json
{
  "client_id": "CLI-ACME-01",
  "log_id": 1042,
  "reply_text": "Hello John,\n\nYour order ORD-99214 has shipped via FedEx tracking #99281928.",
  "paused_history_record_id": null,
  "blocked_record_id": null
}
```

- **Behavior**:
  - Validates client permissions and target email log.
  - Sends email via tenant SMTP credentials.
  - On send success, updates `email_logs.status = 'sent'` and closes associated review queues. On send failure, leaves queue items open so messages are never lost.

---

## 5. System Health & Telemetry

### `GET /health`
Deep diagnostic dependency probe checking connectivity across all infrastructure tiers.

- **Response (200 OK when all healthy, 503 if any degraded)**:
```json
{
  "status": "healthy",
  "components": {
    "mysql": "connected",
    "redis": "connected",
    "qdrant": "connected",
    "embed_service": "connected"
  },
  "version": "2.1.0",
  "timestamp": "2026-10-01T12:00:00Z"
}
```
