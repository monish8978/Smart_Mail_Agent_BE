import sys
sys.path.insert(0, '/app')

import time
import httpx
from app.db import get_db_ctx

API_URL = "http://mail_ai_api:8024/process-email"
CLIENT_ID = "CLI-425589BC"

def get_latest_email_log(client_id, subject):
    with get_db_ctx() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, client_id, from_email, subject, reply, score, status, execution_steps, summary, created_at "
                "FROM email_logs WHERE client_id=%s AND subject=%s ORDER BY id DESC LIMIT 1",
                (client_id, subject)
            )
            row = cur.fetchone()
            if row and isinstance(row, tuple):
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
            return row

def get_action_logs(client_id):
    with get_db_ctx() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT action_type, status, idempotency_key, external_ref, created_at "
                "FROM action_logs WHERE client_id=%s ORDER BY created_at DESC LIMIT 5",
                (client_id,)
            )
            rows = cur.fetchall()
            if rows and isinstance(rows[0], tuple):
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            return rows

def wait_for_task(subject, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        log = get_latest_email_log(CLIENT_ID, subject)
        if log and log.get("reply"):
            return log
        time.sleep(2)
    return get_latest_email_log(CLIENT_ID, subject)

def run_tests():
    print("=" * 70)
    print("🚀 RUNNING END-TO-END LIVE PIPELINE TESTS (CLIENT: CLI-425589BC)")
    print("=" * 70)

    client = httpx.Client(timeout=10.0)

    # -------------------------------------------------------------
    # Test 1: RAG Knowledge Base Retrieval & Synthesis
    # -------------------------------------------------------------
    print("\n--- [TEST 1] RAG Knowledge Base Inquiry ---")
    sub1 = f"CZ ACD Callers Waiting in Queue (Run {int(time.time())})"
    payload1 = {
        "client_id": CLIENT_ID,
        "from_email": "ops-lead@partner.com",
        "subject": sub1,
        "body": "Hello, our inbound callers are waiting in queue even though agents show Ready in CZ ACD. What should we check?"
    }
    r1 = client.post(API_URL, json=payload1)
    print(f"API Response: {r1.status_code} {r1.text}")
    assert r1.status_code == 200, "API did not accept email"

    print("Waiting for worker processing...")
    log1 = wait_for_task(sub1)
    if log1 and log1.get("reply"):
        print(f"✅ Email Log ID: {log1['id']}")
        print(f"   Status: {log1['status']} | Score: {log1['score']}")
        print(f"   Execution Steps: {log1.get('execution_steps')}")
        print("   Generated Reply Snippet:")
        print("   " + "-" * 50)
        for line in (log1.get('reply') or "").split("\n")[:8]:
            print(f"   {line}")
        print("   " + "-" * 50)
    else:
        print(f"⚠️ Task log retrieved: {log1}")

    # -------------------------------------------------------------
    # Test 2: Order Status Lookup Tool Call
    # -------------------------------------------------------------
    print("\n--- [TEST 2] Order / Ticket Status Tool Call ---")
    sub2 = f"Checking status of order #1001 (Run {int(time.time())})"
    payload2 = {
        "client_id": CLIENT_ID,
        "from_email": "shopper@customer.com",
        "subject": sub2,
        "body": "Hi, I placed order #1001 last week. Can you check the shipping status for me?"
    }
    r2 = client.post(API_URL, json=payload2)
    print(f"API Response: {r2.status_code} {r2.text}")
    assert r2.status_code == 200

    print("Waiting for worker processing...")
    log2 = wait_for_task(sub2)
    if log2 and log2.get("reply"):
        print(f"✅ Email Log ID: {log2['id']}")
        print(f"   Status: {log2['status']} | Score: {log2['score']}")
        print(f"   Execution Steps: {log2.get('execution_steps')}")
        print("   Generated Reply Snippet:")
        print("   " + "-" * 50)
        for line in (log2.get('reply') or "").split("\n")[:8]:
            print(f"   {line}")
        print("   " + "-" * 50)
    else:
        print(f"⚠️ Task log retrieved: {log2}")

    # -------------------------------------------------------------
    # Test 3: Action Outbox Verification
    # -------------------------------------------------------------
    print("\n--- [TEST 3] Outbox Idempotency Logs ---")
    outbox_entries = get_action_logs(CLIENT_ID)
    print(f"Found {len(outbox_entries)} recent outbox records:")
    for entry in outbox_entries:
        print(f"   Action: {entry['action_type']} | Status: {entry['status']} | Ref: {entry['external_ref']}")

    print("\n" + "=" * 70)
    print("🎯 END-TO-END TEST RUN FINISHED")
    print("=" * 70)

if __name__ == "__main__":
    run_tests()
