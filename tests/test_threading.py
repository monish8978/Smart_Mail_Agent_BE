import unittest
from unittest.mock import MagicMock, patch
import json
from datetime import datetime

from app.pipeline.context import PipelineContext
from app.pipeline.evaluator import evaluate_draft_and_decide
from app.pipeline.agent import check_customer_resolution, build_system_prompt
from app.pipeline.enricher import fetch_rag_context
from worker.tasks import resolve_thread_id


class TestThreadingAndTroubleshooting(unittest.TestCase):

    def test_01_resolution_detection(self):
        """Customer resolution phrases must be detected accurately"""
        self.assertTrue(check_customer_resolution("That worked, thank you so much!"))
        self.assertTrue(check_customer_resolution("The issue is resolved now, thanks."))
        self.assertTrue(check_customer_resolution("It is working properly now."))
        self.assertTrue(check_customer_resolution("All good now, appreciate the help."))
        self.assertTrue(check_customer_resolution("Thank you, it works!"))

        self.assertFalse(check_customer_resolution("I tried that but it still doesn't work."))
        self.assertFalse(check_customer_resolution("What is the status of my ticket?"))
        self.assertFalse(check_customer_resolution("The screen is still black."))

    def test_02_evaluator_bypasses_escalation_on_resolved(self):
        """When is_resolved is True, evaluator must auto-send warm closure and never escalate to a ticket"""
        score, decision = evaluate_draft_and_decide(
            client_id="CLI-TEST",
            reply="Dear Customer,\n\nWe are glad to hear the issue is resolved! Please feel free to reach out if you need anything else.\n\nThanks & Regards,\nSupport Team",
            query="That worked, thank you!",
            context_succeeded=False,  # Even if RAG context was empty!
            is_resolved=True
        )
        self.assertEqual(decision, "auto_send")
        self.assertGreaterEqual(score, 90)

    def test_03_rag_parameter_inversion_fix(self):
        """fetch_rag_context must pass client_id as first argument and query as second to query_knowledge"""
        with patch("app.pipeline.enricher.get_rag_id", return_value=None), \
             patch("app.pipeline.enricher.query_knowledge") as mock_qk:
            mock_qk.return_value = "Knowledge context for troubleshooting"
            ctx_text, succeeded, rag_id = fetch_rag_context("CLIENT_A", "printer not printing")
            mock_qk.assert_called_once_with("CLIENT_A", "printer not printing", top_k=3)
            self.assertTrue(succeeded)
            self.assertEqual(ctx_text, "Knowledge context for troubleshooting")

    def test_04_thread_id_resolution_via_in_reply_to(self):
        """resolve_thread_id should link to existing thread_id via In-Reply-To header"""
        cursor = MagicMock()
        cursor.fetchone.return_value = ("th_existing_123", 1)

        thread_id, prior_step = resolve_thread_id(
            client_id="CLI-TEST",
            from_email="user@example.com",
            subject="Re: Printer issue",
            in_reply_to="<msg-root-001@example.com>",
            references="",
            cursor=cursor
        )

        self.assertEqual(thread_id, "th_existing_123")
        self.assertEqual(prior_step, 1)

    def test_05_thread_id_resolution_fallback_new(self):
        """resolve_thread_id generates a new th_ id when no match is found"""
        cursor = MagicMock()
        cursor.fetchone.return_value = None

        thread_id, prior_step = resolve_thread_id(
            client_id="CLI-TEST",
            from_email="newuser@example.com",
            subject="Brand new issue",
            in_reply_to="",
            references="",
            cursor=cursor
        )

        self.assertTrue(thread_id.startswith("th_"))
        self.assertEqual(prior_step, 0)

    def test_06_sql_history_fallback_on_cold_redis(self):
        """get_history must fallback to SQL email_logs when Redis cache is empty"""
        from app.chat_history import get_history

        mock_redis = MagicMock()
        mock_redis.lrange.return_value = []  # Cold cache / expired TTL

        fake_sql_rows = [
            (
                "My router power light is blinking orange.",
                "Please disconnect the power adapter for 30 seconds and plug it back in. Let us know if the light turns green.",
                "Router problem",
                datetime(2026, 9, 14, 10, 0, 0),
                "sent",
                "Router troubleshooting",
                1
            )
        ]

        mock_db = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = fake_sql_rows
        mock_db.cursor.return_value.__enter__.return_value = mock_cur

        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_db

        with patch("app.chat_history.redis_client", mock_redis), \
             patch("app.chat_history.get_db_ctx", return_value=mock_ctx):
            history = get_history(
                client_id="CLI-TEST",
                from_email="customer@example.com",
                last_n=10,
                thread_id="th_123"
            )

            # History must contain customer turn and support turn reconstructed from SQL
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["role"], "customer")
            self.assertIn("blinking orange", history[0]["body"])
            self.assertEqual(history[1]["role"], "support")
            self.assertIn("disconnect the power adapter", history[1]["body"])


if __name__ == "__main__":
    unittest.main()
