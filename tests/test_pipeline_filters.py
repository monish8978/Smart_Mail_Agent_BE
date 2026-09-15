import unittest
from unittest.mock import MagicMock
from app.pipeline.context import PipelineContext
from app.pipeline.filters import apply_deterministic_filters
from app.llm import extract_ticket_and_order_ids
from app.keyword_filter import is_blocked

class TestPipelineFilters(unittest.TestCase):

    def setUp(self):
        try:
            from app.rate_limiter import get_redis_client
            r = get_redis_client()
            if r:
                r.delete("ratelimit:sender:CLI-TEST:customer@example.com")
        except Exception:
            pass

    def test_01_bounce_sender_filtered(self):
        """Emails from mailer-daemon, postmaster, or noreply must be dropped without reply"""
        ctx = PipelineContext.from_task_data("test-task-bounce", {
            "client_id": "CLI-TEST",
            "from_email": "mailer-daemon@googlemail.com",
            "subject": "Delivery Failure",
            "body": "Your message was not delivered."
        })
        cursor = MagicMock()
        halted = apply_deterministic_filters(ctx, cursor)
        self.assertTrue(halted)
        self.assertEqual(ctx.status, "system_bounce_dropped")
        self.assertIn("System_Bounce_Dropped", ctx.execution_steps)

    def test_02_bounce_subject_filtered(self):
        """Emails with delivery status notification subjects must be dropped"""
        ctx = PipelineContext.from_task_data("test-task-sub-bounce", {
            "client_id": "CLI-TEST",
            "from_email": "notifications@somehost.com",
            "subject": "Delivery Status Notification (Failure)",
            "body": "Could not deliver message."
        })
        cursor = MagicMock()
        halted = apply_deterministic_filters(ctx, cursor)
        self.assertTrue(halted)
        self.assertEqual(ctx.status, "system_bounce_dropped")

    def test_03_master_bot_disabled_halt(self):
        """When client or admin bot switch is disabled, processing must halt immediately"""
        ctx = PipelineContext.from_task_data("test-task-halt", {
            "client_id": "CLI-TEST",
            "from_email": "customer@example.com",
            "subject": "Need help with login",
            "body": "Please assist me with my login credentials."
        })
        ctx.features = {"admin_bot_enabled": False, "client_bot_enabled": True}
        cursor = MagicMock()
        halted = apply_deterministic_filters(ctx, cursor)
        self.assertTrue(halted)
        self.assertEqual(ctx.status, "automation_halted")
        self.assertTrue(any("Master_Switch_Halt" in s for s in ctx.execution_steps))

    def test_04_keyword_blocking(self):
        """Blocked keywords should match case-insensitively and block email"""
        blocked_list = ["lawsuit", "attorney", "unacceptable fraud"]
        
        self.assertIsNotNone(is_blocked("I will hire an attorney for this", blocked_list))
        self.assertEqual(is_blocked("I will hire an attorney for this", blocked_list), "attorney")
        self.assertIsNone(is_blocked("Can you please help with my order?", blocked_list))

    def test_05_ticket_id_extraction_single(self):
        """Standard ticket format T-YYMMDD-XXXXX and prefixes should be extracted"""
        text = "Hello, checking the status of ticket T-260505-00117."
        tids = extract_ticket_and_order_ids(text)
        self.assertIn("T-260505-00117", tids)

        text_ord = "Where is my order ORD-10294?"
        tids_ord = extract_ticket_and_order_ids(text_ord)
        self.assertIn("ORD-10294", tids_ord)

    def test_06_ticket_id_extraction_multiple(self):
        """Multiple ticket IDs in single email should be extracted and deduplicated"""
        text = "Regarding ticket T-260505-00117 and also my older order ORD-10294."
        tids = extract_ticket_and_order_ids(text)
        unique_tids = list(dict.fromkeys(tids))
        self.assertEqual(len(unique_tids), 2)
        self.assertIn("T-260505-00117", unique_tids)
        self.assertIn("ORD-10294", unique_tids)


if __name__ == "__main__":
    unittest.main()
