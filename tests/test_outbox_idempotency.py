import time
import uuid
import unittest
from app.db import get_db_ctx
from app.action_outbox import (
    compute_idempotency_key,
    execute_idempotent_action,
    sweep_stale_actions,
    get_action_logs
)
from worker.imap_reader import is_message_duplicate

class TestOutboxIdempotency(unittest.TestCase):

    def test_01_idempotency_key_deterministic(self):
        """Identical parameters must produce the exact same SHA256 idempotency key"""
        k1 = compute_idempotency_key("CLI-TEST", "<msg-12345@domain.com>", "crm_ticket_create")
        k2 = compute_idempotency_key("CLI-TEST", "<msg-12345@domain.com>", "crm_ticket_create")
        k3 = compute_idempotency_key("CLI-TEST", "<msg-diff@domain.com>", "crm_ticket_create")

        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertEqual(len(k1), 64)

    def test_02_idempotent_action_single_execution(self):
        """Action function must be called only once even when invoked multiple times with same key"""
        client_id = f"CLI-TEST-{uuid.uuid4().hex[:6]}"
        message_id = f"test-msg-{uuid.uuid4().hex}"
        call_counter = [0]

        def test_side_effect():
            call_counter[0] += 1
            return {"ticket_id": f"TICKET-{call_counter[0]}", "status": "created"}

        # First run: should execute
        r1 = execute_idempotent_action(
            client_id=client_id,
            message_id=message_id,
            action_type="test_action",
            action_fn=test_side_effect,
            extract_ref_fn=lambda res: res.get("ticket_id")
        )
        self.assertTrue(r1["success"])
        self.assertFalse(r1["already_completed"])
        self.assertEqual(call_counter[0], 1)
        self.assertEqual(r1["external_ref"], "TICKET-1")

        # Second run: must NOT execute side effect function again
        r2 = execute_idempotent_action(
            client_id=client_id,
            message_id=message_id,
            action_type="test_action",
            action_fn=test_side_effect,
            extract_ref_fn=lambda res: res.get("ticket_id")
        )
        self.assertTrue(r2["success"])
        self.assertTrue(r2["already_completed"])
        self.assertEqual(call_counter[0], 1, "Side effect was executed more than once!")
        self.assertEqual(r2["external_ref"], "TICKET-1")

    def test_03_outbox_sweeper_stale_detection(self):
        """Pending actions older than max_age_seconds must transition to failed"""
        client_id = "CLI-SWEEP-TEST"
        test_key = f"mock_stale_{uuid.uuid4().hex[:12]}"

        with get_db_ctx() as db:
            with db.cursor() as cur:
                # Insert pending row backdated by 300 seconds
                cur.execute("""
                    INSERT INTO action_logs (client_id, idempotency_key, action_type, status, created_at)
                    VALUES (%s, %s, 'test_mock_sweep', 'pending', DATE_SUB(NOW(), INTERVAL 300 SECOND))
                """, (client_id, test_key))
                db.commit()

        # Execute sweep
        sweep_res = sweep_stale_actions(max_age_seconds=120)
        self.assertGreaterEqual(sweep_res["swept_count"], 1)
        self.assertIn(test_key, sweep_res["swept_keys"])

        # Verify DB state
        with get_db_ctx() as db:
            with db.cursor() as cur:
                cur.execute("SELECT status, error_message FROM action_logs WHERE idempotency_key = %s", (test_key,))
                row = cur.fetchone()
                self.assertEqual(row[0], "failed")
                self.assertIn("timed out in pending state", row[1])

    def test_04_imap_deduplication_cache(self):
        """Duplicate Message-IDs within 24h window must be flagged as duplicates"""
        unique_msg_id = f"<test-imap-{uuid.uuid4().hex}@domain.com>"
        client_id = "CLI-IMAP-TEST"

        # First encounter: must not be duplicate
        first_check = is_message_duplicate(client_id, unique_msg_id)
        self.assertFalse(first_check)

        # Immediate second encounter: must be duplicate
        second_check = is_message_duplicate(client_id, unique_msg_id)
        self.assertTrue(second_check)


if __name__ == "__main__":
    unittest.main()
