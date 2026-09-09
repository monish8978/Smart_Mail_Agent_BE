# Mail AI Automation / AI Mail Agent — Client Guide

This guide explains how the system works from your side as a client, what you can control, and what to expect when a customer email comes in. It leaves out the technical/developer details (API routes, database fields, etc.) and focuses on what you can actually see and do.

---

## 1. Getting Started

Your account is set up by an admin, who gives you:
- A login (email + password)
- Your inbox connection (the email address the system watches for customer messages)
- Default settings: how confident the AI needs to be before it auto-replies, and what tone it should write in

Once you're set up, you log in and get access to your own dashboard. You can only see and manage your own data — you cannot see other clients' emails, tickets, or settings, and they cannot see yours.

**What you can edit yourself:**
- Your department and company name
- Your inbox email/password, confidence threshold, and reply tone

---

## 2. How an Incoming Email Gets Handled

Every customer email that lands in your inbox goes through the same pipeline automatically — you don't have to do anything for it to start:

1. **It's picked up** — either the system is watching your inbox directly, or an email gets submitted to it directly.
2. **It's checked against your rules first** — is this sender paused? Does the message contain a keyword you've blocked? If either is true, the AI does not touch it (see Sections 6 and 7).
3. **The AI reads it** — figures out whether it's a general question or a ticket/order status check, and gauges the customer's tone (angry, neutral, happy) and urgency.
4. **If the customer mentions more than one ticket/order number**, the system doesn't guess — it asks them to clarify which one they mean before doing anything else.

From there, one of three things happens:

### Path A — "What's the status of my ticket/order?"
If the email clearly references a ticket or order number, the system looks it up in your connected CRM/ticketing system.
- **Found it** → AI writes a status update and sends it automatically.
- **Can't find it** → the customer gets a polite message asking them to double check the ID. The conversation is now "waiting on the customer" until they reply.

### Path B — General question
If there's no ticket number, the system searches your knowledge base (FAQs, policies, docs you've uploaded) for an answer.
- If it finds a good answer **and** is confident enough (based on your threshold setting), it replies automatically.
- If it's not confident, or finds nothing useful, it does **not** guess or send a weak answer — it quietly moves to Path C instead. The customer never sees a low-quality draft.

### Path C — Escalation / new ticket
This is the fallback for anything the first two paths couldn't resolve:
- If the customer was previously asked to confirm a ticket ID and still can't be matched, the system creates a brand-new ticket from their description and lets them know.
- If there was never a ticket ID at all, it creates one directly.
- If the AI finds multiple *old* tickets in the conversation history and isn't sure which one is relevant, it asks the customer to clarify rather than guessing.

### When automation is intentionally skipped
- You can turn off auto-ticket-creation or auto-send for your account — replies then wait for your manual approval instead of going out on their own.
- If anything fails along the way (ticket couldn't be created, email couldn't be sent, AI reply couldn't be scored), the email is never silently dropped — it lands in a "pending manual review" queue for a human to handle.

---

## 3. Feature Switches

These are on/off controls for how much the AI is allowed to do on your behalf (set by your admin, visible to you):

- **Ticket creation** — can the system open tickets automatically?
- **Auto-send** — can it send high-confidence replies on its own, or should everything wait for your approval first?
- **Knowledge base search** — should it try to answer from your uploaded docs at all?
- **Order/ticket tracking** — should it try to detect and look up ticket/order numbers?
- **Manual reply tools** — is manual reply available to you?

Turning any of these off doesn't break the pipeline — it just makes that step fall through to a human review queue instead.

---

## 4. Reviewing What Happened

You get a full activity log and dashboard, without having to dig through raw email:

- **Email log** — every message the system touched: who sent it, what it said, what the AI replied (if anything), how confident it was, current status (New / Replied / Ticket Created / Failed / Pending Review), sentiment, urgency, and a plain step-by-step trace of what the system actually did with it.
- **Ticket list** — every support ticket opened on your behalf, with status, urgency, and timestamps.
- **Dashboard stats** — totals for emails processed, pending items, AI replies sent, failed sends, tickets created, orders tracked, and average AI confidence — filterable by today, yesterday, this month, last month, or a custom date range.

### If a reply is waiting on you
- **Approve as-is** — send the AI's drafted reply exactly as written.
- **Write your own** — skip the AI entirely and send your own reply. This is also how you close out items sitting in your review queues (see below) — but a queue item is only marked "handled" once the email actually sends successfully. If sending fails, it stays open so nothing gets lost.

---

## 5. Pausing a Customer Conversation

Sometimes you want to personally handle a conversation without the AI jumping in — pausing lets you do that per customer email address.

- Pause a sender — no more auto-replies to that address until you unpause.
- Unpause — hands control back to the AI.
- See who's currently paused.

**Nothing is lost while paused.** Every email from a paused sender still shows up in your main log *and* in a dedicated "paused" review queue, so you can catch up on exactly what came in while you were handling things manually. You can filter that queue by whether it's still pending, already ignored, or already replied to, and you can group it by sender. Unpausing someone doesn't erase their history in this queue — it just lets new emails through again.

---

## 6. Blocking Keywords

You can set up a list of words or phrases that automatically pull a matching email out of the normal AI pipeline — useful for legal threats, complaints that need a human touch, VIP names, etc.

- Add or remove keywords anytime.
- View your current list.
- Matched emails go into their own review queue, separate from the paused-sender one, with the same pending/ignored/replied states.
- You can clear a backlog of these in one action instead of going one by one.
- Reply directly from this queue the same way you would for a paused email.

---

## 7. Connecting Your CRM / Ticketing System

Rather than the system being locked to one specific CRM, you connect your own system by telling it:
- **What kind of action this connection handles** — checking a ticket's status, or creating a new one.
- **Where to send the request** and how to log in (a token, username/password, or an API key) — your credentials are encrypted and never shown back to you in plain text.
- **What the outgoing message should look like** — built from a set of fields the system fills in automatically per email (customer email, subject, message, ticket ID, sentiment, urgency, etc.).
- **How to read the response** — which parts of your CRM's reply contain the ticket ID and status.

**Nothing goes live automatically.** Every new connection starts in a "waiting for approval" state and is reviewed by an admin before it's used on a real customer email. This exists specifically so nothing you configure can start hitting an external system — or an unintended one — without a second set of eyes.

**Don't want to write the request format by hand?** Describe your CRM in plain language (and paste in a sample response if you have one), and the system will draft the request and response format for you. This is a **preview only** — nothing is saved until you review it and submit it for approval.

**Updating a connection later** doesn't cause downtime — submitting an update creates a new version waiting for approval, while your current live connection keeps working exactly as before until the new one is approved.

You can see all of your connections and their status (draft, waiting for approval, live, or disabled) at any time. Anything using more delicate response-parsing rules is flagged for extra reviewer attention.

**What you can't do:** approve or reject your own connection — that's admin-only, by design, so nothing you build can quietly go live without review.

---

## 8. Knowledge Base

Upload anything you want the AI to be able to answer from — policies, FAQs, product docs.

- Add content directly, or upload a file (PDF, Word, or plain text) and it's read and indexed automatically.
- See everything you've uploaded.
- Remove anything that's outdated.
- Test what the AI would answer for a given question before trusting it in production — so you're not finding gaps the hard way, in front of a real customer.

---

## 9. Conversation History

You can pull up the recent back-and-forth with any specific customer — both what they said and what the system (or you) replied. You can also clear the short-term cached version of that history if needed; this doesn't erase the permanent ticket record, just the fast-access copy.

---

## 10. Real-Time Updates

Your dashboard stays live — when a new email comes in and gets processed, it shows up without you needing to refresh the page.

---

## 11. Cost & Usage Visibility

- See how much AI usage you're generating: number of requests, tokens used, cost, average response time — broken down by which part of the pipeline is using it (intent detection vs. reply drafting, etc.) and a recent activity log.
- If your admin has set you a monthly budget, you can see your spend against it and whether you're on track, close to the limit, or over it. This is informational only — going over budget never stops the system from working.

---

## 12. What You Can and Can't Do — Quick Reference

| Category | You Can | You Can't |
|---|---|---|
| Account | Update your profile, inbox settings, confidence threshold | Recreate/delete accounts |
| Emails | View your logs, approve/send replies, reply manually | — |
| Pausing | Pause/unpause any sender, review the paused queue | — |
| Blocked keywords | Manage your keyword list and how it's handled, review the queue | — |
| CRM connections | Create, edit, get an AI-drafted starting point, view your own | Approve or reject your own connection |
| Knowledge base | Upload, test, and delete your own documents | — |
| Tickets | View your own tickets | — |
| Usage & cost | View your own usage and budget status | — |

---

## 13. A Few Things Worth Knowing

- **A weak or missing answer is never sent just to have *something* go out.** If the AI isn't confident, or the CRM/knowledge base has nothing useful, the email is escalated rather than answered badly.
- **Your data is isolated.** Even though the system serves many clients at once, your emails, tickets, knowledge base, and credentials are kept separate from everyone else's.
- **Nothing external happens without review.** New CRM connections, in particular, always go through an approval step — an AI-drafted or self-configured connection can't quietly start talking to an unintended system.
- **Failures don't disappear.** Every point where something could go wrong (a send failing, a ticket failing to create, a score failing to compute) routes to a manual review queue instead of silently dropping the email.

---

## 14. Future Plans

These are improvements planned or under active consideration — not live yet, included here so you know what's coming and can plan around it:

- **Microsoft Outlook / Microsoft 365 support** — as an alternative to Gmail-only inbox connections, so clients on Microsoft can connect their inbox directly instead of switching providers. Still being decided: whether new mail is delivered instantly (push-based) or checked on a schedule.
- **Yahoo Mail support** — another inbox option alongside Gmail, for clients whose support inbox runs on Yahoo rather than switching providers just to use this system.
- **Better visibility into system health** — infrastructure-side improvements (like log storage limits) planned so the system stays reliable as usage grows, without requiring any changes on your end.
- **Rotating security keys** — the underlying encryption used to protect your stored credentials will support periodic key rotation, an added layer of protection with no action needed from you.
- **Choice of AI provider** — right now the system runs on one AI provider (Groq) behind the scenes. The plan is to let this be switched — Anthropic, Gemini, OpenAI, etc. — so you're not locked into a single provider's pricing, speed, or availability. Note this is a backend flexibility improvement, not something that changes how you use the system day to day.

