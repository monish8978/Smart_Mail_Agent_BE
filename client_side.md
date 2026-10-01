# Mail AI Automation Platform — Client & Operator Guide

This guide explains how the AI Mail Agent operates from the client and operations perspective: what the system does automatically, what controls and switches you have, and how customer email conversations are handled end-to-end.

---

## 1. Getting Started

Your client account is provisioned by a system administrator with:
- **Account Credentials**: Secure login for the web dashboard.
- **Mailbox Connection**: The IMAP/SMTP credentials for the mailbox monitored by the system.
- **Tenant Policies**: Your company name, department, reply tone, and AI confidence threshold.

Each client runs in complete tenant isolation. You can only view and manage your own emails, tickets, knowledge base documents, and connector configurations.

---

## 2. How Incoming Emails Are Handled

Every customer email received in your monitored inbox automatically passes through a four-stage pipeline:

```
Incoming Customer Email
         │
         ▼
 1. Safety Filters       ──▶ Bot switch OFF / Daemon Bounce / Blocked Keywords?
         │                   └──▶ Dropped or routed to review queue
         ▼
 2. Context Enrichment   ──▶ Classifies intent, sentiment, urgency & threading
         │
         ▼
 3. Autonomous Agent     ──▶ Evaluates customer context & executes necessary tools:
         │                   • Semantic Knowledge Base retrieval (RAG)
         │                   • CRM/ERP order & ticket status lookup
         │                   • Multi-turn customer clarification request
         │                   • Ticket escalation and creation
         ▼
 4. Quality Evaluation   ──▶ Confidence score (0–100) vs. your threshold:
                             • Meets threshold & auto-send enabled ──▶ Sent automatically
                             • Low confidence or review required  ──▶ Placed in Review Queue
```

### Flow Highlights:
- **Direct Answers**: If the customer asks a policy or product question answered in your Knowledge Base, the agent composes a grounded, professional response.
- **Order & Ticket Inquiries**: If an order ID or ticket reference is provided, the agent queries your connected CRM/ERP API using your approved connector and replies with real-time status.
- **Ambiguity & Clarification**: If a customer provides conflicting references or incomplete details, the agent politely requests clarification rather than hallucinating an answer.
- **Escalation**: When an issue cannot be resolved automatically or diagnostics fail, the agent creates a ticket in your CRM and confirms the ticket reference with the customer.
- **Safety First**: Delivery failures, mailer-daemon bounces, and automated out-of-office notifications are dropped immediately to avoid infinite email reply loops.

---

## 3. Operational Feature Switches

You have granular control over what the AI is permitted to perform on your behalf:

- **Master Bot Switch**: Global kill-switch. When turned OFF, incoming emails bypass the AI completely and route to your manual queue.
- **Auto-Send**: When ON, replies scoring above your confidence threshold are dispatched directly to the customer. When OFF, all generated drafts wait for human approval.
- **Auto-Ticket Creation**: Allows the agent to open new CRM tickets when complex issues cannot be answered automatically.
- **Knowledge Base Search**: Enables or disables semantic search across your uploaded documents.
- **Order Tracking**: Enables or disables automated lookups against external order APIs.

---

## 4. Live Inbox & Review Queue

The web dashboard provides real-time visibility into all incoming and outgoing mail:

- **Real-Time Streaming**: Inbound emails and automated agent actions appear instantly via WebSockets without manual page refreshes.
- **Multi-Turn Thread Inspection**: View complete conversation histories showing both customer messages and agent responses with timestamps and sentiment badges.
- **Human-in-the-Loop Actions**:
  - **Approve Draft**: Inspect the AI-drafted reply and send it with one click.
  - **Edit & Send**: Tweak the wording before dispatching.
  - **Manual Reply**: Compose your own custom response, bypassing the AI.
  - **Mark Ignored / Resolved**: Clear queue backlogs when an email requires no outbound reply.

---

## 5. Pausing Customer Conversations

If an agent needs to handle a sensitive customer thread manually, you can pause that specific sender:
- **Pause Sender**: Stops all automated AI replies to that email address until unpaused.
- **Audit Queue**: Emails received while paused are preserved in your **Paused Emails** queue (`pending_review`, `ignored`, or `replied`).
- **Unpause**: Resumes automated AI processing for future messages from that sender.

---

## 6. Keyword Blocking Rules

Protect your brand by intercepting messages containing specific words or phrases (e.g. legal threats, executive complaints, VIP keywords):
- **Policy Rules**: Configure keywords that automatically pull matching emails out of the AI pipeline.
- **Dedicated Queue**: Intercepted messages route to the **Blocked Keyword Queue** where operators can review context and send manual replies.

---

## 7. Connecting Your CRM & Helpdesk (Connectors)

The platform supports dynamic connections to any REST API (Zoho Desk, Zendesk, Freshdesk, Salesforce, custom ERPs):
- **Actions Supported**: Order status lookup, ticket status lookup, payment status lookup, and ticket creation.
- **Secure Credentials**: All API keys, Bearer tokens, and Basic Auth credentials are encrypted at rest with Fernet cryptography.
- **AI-Assisted Template Generator**: Describe your CRM's endpoint format in plain English or paste a sample JSON response; the system drafts the request template and JMESPath response mapping automatically.
- **Admin Approval Gate**: Every new connection or updated revision starts in `pending_approval` and must be approved by an administrator before going live, preventing unintended external calls.
- **Zero-Downtime Updates**: Modifying a live connector creates a new revision while the existing live connector continues serving production emails uninterrupted.

---

## 8. Knowledge Base Management

Train the agent on your business policies, FAQs, and product documentation:
- **Document Ingestion**: Upload PDF, text, or spreadsheet files. Content is extracted, chunked, and embedded into the vector database automatically.
- **Semantic Retrieval Sandbox**: Test queries directly in the dashboard to inspect what chunks the vector store retrieves and what similarity scores they achieve before going live.
- **Resilient Fallback**: If the vector database is undergoing maintenance, queries transparently fall back to encrypted local document stores.

---

## 9. AI Telemetry & Budget Controls

- **Token & Cost Analytics**: Monitor daily, weekly, and monthly LLM token usage, request counts, and dollar spend across models (Groq, OpenAI, Anthropic, Gemini).
- **Monthly Budget Quotas**: Administrators can set monthly spending limits per tenant. The dashboard displays real-time burn-rate gauges and dispatches alerts when approaching thresholds.

---

## 10. Operational Summary Table

| Capability | Client Permissions | Admin Permissions |
|---|---|---|
| Mailbox Configuration | View & Update IMAP/SMTP credentials | Provision & delete client accounts |
| Feature Switches | Toggle Auto-Send, Bot Switch, Ticket Creation | Set global tenant defaults |
| Inbox & Threads | View logs, approve drafts, send manual replies | View system-wide logs & health |
| CRM Connectors | Create, draft with AI, submit for approval | Approve or reject connector revisions |
| Knowledge Base | Upload, delete, and test semantic documents | Manage cluster embedding settings |
| LLM Telemetry | View token usage and cost burn-rate | Allocate monthly spending limits & models |
