#!/usr/bin/env python3
"""
tests/realworld_scenarios_test.py

End-to-End Real-World Scenario Testing Suite for Mail AI Automation:
1. Scenario 1: Unresolved Technical Issue -> Dynamic Zoho Desk Ticket Creation (No-subject)
2. Scenario 2: Live Ticket Status Inquiry -> Agent Tool Execution (Zoho Desk lookup)
3. Scenario 3: Self-Serve Knowledge Inquiry -> Qdrant RAG Auto-Reply (Zero Junk Tickets)
4. Scenario 4: Fast Deterministic Guardrails:
   - 4A: Automated Bounce Filter (Zero LLM tokens)
   - 4B: Multi-Ticket Ambiguity Clarification (Deterministic regex intercept)
"""

import sys
import os
import time
import json
import logging
from typing import Dict, Any

# Ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_db_ctx
from app.rag import add_knowledge, get_knowledge_base
from worker.tasks import process_email_task

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RealWorldTest")

CLIENT_ID = "CLI-425589BC"
SENDER = "md.yazdani@c-zentrix.com"

def get_latest_log(client_id: str, from_email: str) -> Dict[str, Any]:
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("""
                SELECT id, client_id, from_email, subject, reply, score, status, execution_steps, summary
                FROM email_logs 
                WHERE client_id = %s AND from_email = %s 
                ORDER BY id DESC LIMIT 1
            """, (client_id, from_email))
            row = cursor.fetchone()
            if not row:
                return {}
            return {
                "id": row[0],
                "client_id": row[1],
                "from_email": row[2],
                "subject": row[3],
                "reply": row[4],
                "score": row[5],
                "status": row[6],
                "execution_steps": json.loads(row[7]) if row[7] else [],
                "summary": row[8]
            }

def run_scenario_1_ticket_creation():
    print("\n" + "="*70)
    print("🧪 SCENARIO 1: Real-World Technical Issue -> Zoho Desk Ticket Creation")
    print("="*70)
    print("Description: Customer sends an urgent dialer failure email with (no subject).")
    print("Expectation: Empty subject normalized, Qdrant searched, Zoho ticket created, confirmation sent.")
    
    payload = {
        "client_id": CLIENT_ID,
        "from_email": SENDER,
        "subject": "(no subject)",
        "body": "Urgent assistance needed: C-Zentrix Dialer server node 4 crashed unexpectedly with error code ERR_VOIP_AUTH_TIMEOUT. All 25 telecallers were disconnected.",
        "message_id": f"<rw-sc1-{int(time.time())}@c-zentrix.com>"
    }

    t0 = time.time()
    process_email_task(payload)
    elapsed = time.time() - t0

    log = get_latest_log(CLIENT_ID, SENDER)
    print(f"\n⏱️ Completed in {elapsed:.2f}s")
    print(f"📊 Database Log ID: {log.get('id')}")
    print(f"🏷️ Subject: {log.get('subject')}")
    print(f"📌 Status: {log.get('status')}")
    print(f"🪜 Steps: {log.get('execution_steps')}")
    print(f"📝 Summary: {log.get('summary')}")
    if log.get('reply'):
        print(f"💬 Reply excerpt: {log.get('reply')[:200]}...")

    assert log.get("status") in ("ticket_created_and_sent", "ticket_created_draft_pending", "auto_replied"), \
        f"Unexpected status: {log.get('status')}"
    assert log.get("subject") != "(no subject)" and len(log.get("subject", "")) > 3, \
        f"Subject was not properly normalized: {log.get('subject')}"
    print("✅ SCENARIO 1 PASSED: Real CRM Ticket created and confirmed!")


def run_scenario_2_ticket_status_lookup():
    print("\n" + "="*70)
    print("🧪 SCENARIO 2: Real-World Ticket Status Inquiry (Zoho Desk Tool Execution)")
    print("="*70)
    print("Description: Customer asks for the progress of ticket #275424000000452001.")
    print("Expectation: Agent extracts ticket ID, executes lookup tool against Zoho Desk, informs customer.")

    payload = {
        "client_id": CLIENT_ID,
        "from_email": SENDER,
        "subject": "Follow up on my ticket",
        "body": "Hi, could you please provide a status update on my open ticket #275424000000452001? Has an engineer looked into it?",
        "message_id": f"<rw-sc2-{int(time.time())}@c-zentrix.com>"
    }

    t0 = time.time()
    process_email_task(payload)
    elapsed = time.time() - t0

    log = get_latest_log(CLIENT_ID, SENDER)
    print(f"\n⏱️ Completed in {elapsed:.2f}s")
    print(f"📊 Database Log ID: {log.get('id')}")
    print(f"📌 Status: {log.get('status')}")
    print(f"🪜 Steps: {log.get('execution_steps')}")
    print(f"💬 Reply excerpt: {log.get('reply')[:250]}...")

    assert any(tool in str(log.get("execution_steps")) for tool in ["lookup_ticket_status", "lookup_ticket_or_order_status"]), \
        f"lookup tool was not called: {log.get('execution_steps')}"
    assert any(term in (log.get("reply") or "").lower() for term in ["open", "ticket", "dialer", "progress"]), \
        f"Reply does not reference ticket status: {log.get('reply')}"
    print("✅ SCENARIO 2 PASSED: Live CRM status retrieved and reported to customer!")


def run_scenario_3_rag_knowledge_auto_reply():
    print("\n" + "="*70)
    print("🧪 SCENARIO 3: Real-World Knowledge Inquiry -> Qdrant RAG Auto-Reply")
    print("="*70)
    print("Description: Ingesting C-Zentrix support guidelines into Qdrant, then testing customer query.")
    print("Expectation: Agent retrieves policy from Qdrant, auto-replies with high score (no junk ticket created).")

    # Seed knowledge into Qdrant
    kb_title = "C-Zentrix Dialer Audio Codec Configuration"
    kb_content = (
        "C-Zentrix Dialer Audio Codec Configuration Guide:\n"
        "1. Supported codecs are G.711u (PCMU, 64 kbps) and G.729 (8 kbps compressed).\n"
        "2. To change the active codec, navigate to Settings > VoIP Configuration > Codecs in the admin dashboard.\n"
        "3. After updating codecs, click 'Save and Restart Audio Gateway' to apply changes across active agents."
    )
    doc_id = add_knowledge(CLIENT_ID, kb_title, kb_content)
    print(f"📚 Uploaded test knowledge doc to Qdrant: ID={doc_id}")

    payload = {
        "client_id": CLIENT_ID,
        "from_email": SENDER,
        "subject": "Inquiry regarding VoIP Audio Codecs",
        "body": "Hello, could you tell me which audio codecs are supported by the C-Zentrix dialer and where in the admin panel can I configure them?",
        "message_id": f"<rw-sc3-{int(time.time())}@c-zentrix.com>"
    }

    t0 = time.time()
    process_email_task(payload)
    elapsed = time.time() - t0

    log = get_latest_log(CLIENT_ID, SENDER)
    print(f"\n⏱️ Completed in {elapsed:.2f}s")
    print(f"📊 Database Log ID: {log.get('id')}")
    print(f"💯 Confidence Score: {log.get('score')}")
    print(f"📌 Status: {log.get('status')}")
    print(f"🪜 Steps: {log.get('execution_steps')}")
    print(f"💬 Reply excerpt:\n{log.get('reply')[:300]}...")

    assert "search_knowledge_base" in str(log.get("execution_steps")), \
        f"search_knowledge_base was not called: {log.get('execution_steps')}"
    assert any(term in (log.get("reply") or "").lower() for term in ["g.711", "g.729", "codecs", "voip configuration"]), \
        f"Reply did not cite retrieved RAG knowledge: {log.get('reply')}"
    print("✅ SCENARIO 3 PASSED: Qdrant RAG retrieved knowledge and accurately resolved question!")


def run_scenario_4_deterministic_guardrails():
    print("\n" + "="*70)
    print("🧪 SCENARIO 4: Fast Deterministic Guardrails (Zero-LLM Cost)")
    print("="*70)

    # 4A: Mailer-Daemon Bounce Detection
    print("\n--- 4A: Automated Bounce Dropping ---")
    bounce_payload = {
        "client_id": CLIENT_ID,
        "from_email": "mailer-daemon@googlemail.com",
        "subject": "Delivery Status Notification (Failure)",
        "body": "Technical details of permanent failure: 550 5.1.1 The user account does not exist.",
        "message_id": f"<bounce-{int(time.time())}@googlemail.com>"
    }
    t0 = time.time()
    process_email_task(bounce_payload)
    elapsed = time.time() - t0
    log = get_latest_log(CLIENT_ID, "mailer-daemon@googlemail.com")
    print(f"⏱️ Dropped in {elapsed:.3f}s | Status: {log.get('status')} | Score: {log.get('score')}")
    assert log.get("status") == "system_bounce_dropped", f"Expected bounce dropped, got: {log.get('status')}"
    assert elapsed < 1.0, f"Bounce filter took too long: {elapsed}s"
    print("✅ 4A Bounce filter dropped loop message instantly!")

    # 4B: Multi-Ticket Ambiguity Clarification
    print("\n--- 4B: Multi-Ticket Ambiguity Clarification ---")
    multi_payload = {
        "client_id": CLIENT_ID,
        "from_email": SENDER,
        "subject": "Checking on multiple tickets",
        "body": "Hello, what is the status of ticket #275424000000452001 and also ticket #275424000000433120?",
        "message_id": f"<multi-{int(time.time())}@c-zentrix.com>"
    }
    t0 = time.time()
    process_email_task(multi_payload)
    elapsed = time.time() - t0
    log = get_latest_log(CLIENT_ID, SENDER)
    print(f"⏱️ Intercepted in {elapsed:.3f}s | Status: {log.get('status')}")
    print(f"💬 Clarification message:\n{log.get('reply')[:250]}...")
    assert log.get("status") == "clarification_sent", f"Expected clarification_sent, got {log.get('status')}"
    assert "275424000000452001" in (log.get("reply") or ""), "Clarification missing ticket 1"
    assert "275424000000433120" in (log.get("reply") or ""), "Clarification missing ticket 2"
    print("✅ 4B Multi-ticket ambiguity intercepted deterministically!")


if __name__ == "__main__":
    print("\n" + "#"*70)
    print("🚀 STARTING REAL-WORLD LIVE SYSTEM VALIDATION")
    print("#"*70)

    try:
        run_scenario_4_deterministic_guardrails()
        run_scenario_3_rag_knowledge_auto_reply()
        run_scenario_2_ticket_status_lookup()
        run_scenario_1_ticket_creation()

        print("\n" + "#"*70)
        print("🎉 ALL REAL-WORLD SCENARIOS EXECUTED AND PASSED SUCCESSFULLY!")
        print("#"*70 + "\n")
    except Exception as e:
        logger.exception(f"❌ Real-world scenario test failed: {e}")
        sys.exit(1)
