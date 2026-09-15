import unittest
import json
import os
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

from app.pipeline.context import PipelineContext
from app.pipeline.agent import run_support_agent, check_customer_resolution
from app.pipeline.evaluator import evaluate_draft_and_decide
from app.pipeline.tools import execute_tool_call
from app.chat_history import get_history_from_sql
from app.secrets_crypto import encrypt_secret, decrypt_secret
from app.vector_store import _validate_tenant_id
from app.connector_config.dispatch import (
    run_order_status_lookup,
    run_payment_status_lookup,
    run_ticket_status_lookup,
    run_ticket_create,
)


class TestEndToEndScenarios(unittest.TestCase):

    @patch("app.pipeline.tools.fetch_rag_context")
    def test_01_multi_turn_troubleshooting_to_escalation(self, mock_rag):
        """
        E2E Scenario 1: Multi-turn troubleshooting progression.
        Turn 1: Diagnostic Step 1 delivered via knowledge search -> troubleshooting_step incremented.
        Turn 2: Customer replies step failed -> Step 2 diagnostic delivered -> step incremented.
        Turn 3: Customer replies step 2 failed -> 3-turn ceiling reached -> escalated ticket created
                with full diagnostic history included in ticket context.
        """
        client_id = "CLI-E2E-TROUBLESHOOT"
        from_email = "user@testdomain.com"
        subject = "VPN Connection Failure"
        thread_id = "th_e2e_vpn_test_01"

        mock_rag.return_value = ("Troubleshooting guide: 1. Restart adapter. 2. Flush DNS.", True, "rag_vpn_1")

        # --- TURN 1 ---
        ctx_turn1 = PipelineContext.from_task_data("task-t1", {
            "client_id": client_id,
            "from_email": from_email,
            "subject": subject,
            "body": "My VPN client keeps failing to connect with error 800.",
            "thread_id": thread_id,
            "troubleshooting_step": 0,
            "is_resolved": False,
            "history": []
        })

        # Agent calls search_knowledge_base then returns Step 1
        tool_call_1 = MagicMock()
        tool_call_1.id = "call_turn1_rag"
        tool_call_1.function.name = "search_knowledge_base"
        tool_call_1.function.arguments = json.dumps({"query": "error 800 VPN adapter"})

        first_resp_t1 = MagicMock(choices=[MagicMock(message=MagicMock(content="", tool_calls=[tool_call_1]))])
        second_resp_t1 = MagicMock(choices=[MagicMock(message=MagicMock(
            content="Dear User,\n\nPlease restart your network adapter and verify if error 800 clears.\n\nThanks & Regards,\nSupport Team",
            tool_calls=None
        ))])

        with patch("app.pipeline.agent.client") as mock_llm_client:
            mock_llm_client.chat.completions.create.side_effect = [first_resp_t1, second_resp_t1]
            res_ctx_1 = run_support_agent(ctx_turn1)
            self.assertEqual(res_ctx_1.troubleshooting_step, 1)
            self.assertFalse(res_ctx_1.is_resolved)
            self.assertIn("restart your network adapter", res_ctx_1.draft_reply)

        # Record Turn 1 into history list
        history_after_t1 = [
            {"role": "customer", "body": "My VPN client keeps failing to connect with error 800."},
            {"role": "assistant", "body": res_ctx_1.draft_reply}
        ]

        # --- TURN 2 ---
        ctx_turn2 = PipelineContext.from_task_data("task-t2", {
            "client_id": client_id,
            "from_email": from_email,
            "subject": f"Re: {subject}",
            "body": "I restarted the adapter as suggested, but error 800 is still appearing.",
            "thread_id": thread_id,
            "troubleshooting_step": 1,
            "is_resolved": False,
            "history": history_after_t1
        })

        tool_call_2 = MagicMock()
        tool_call_2.id = "call_turn2_rag"
        tool_call_2.function.name = "search_knowledge_base"
        tool_call_2.function.arguments = json.dumps({"query": "error 800 DNS flush"})

        first_resp_t2 = MagicMock(choices=[MagicMock(message=MagicMock(content="", tool_calls=[tool_call_2]))])
        second_resp_t2 = MagicMock(choices=[MagicMock(message=MagicMock(
            content="Dear User,\n\nPlease flush your DNS by running 'ipconfig /flushdns' in terminal.\n\nThanks & Regards,\nSupport Team",
            tool_calls=None
        ))])

        with patch("app.pipeline.agent.client") as mock_llm_client:
            mock_llm_client.chat.completions.create.side_effect = [first_resp_t2, second_resp_t2]
            res_ctx_2 = run_support_agent(ctx_turn2)
            self.assertEqual(res_ctx_2.troubleshooting_step, 2)
            self.assertFalse(res_ctx_2.is_resolved)
            self.assertIn("flush your DNS", res_ctx_2.draft_reply)

        history_after_t2 = history_after_t1 + [
            {"role": "customer", "body": "I restarted the adapter as suggested, but error 800 is still appearing."},
            {"role": "assistant", "body": res_ctx_2.draft_reply}
        ]

        # --- TURN 3: CEILING REACHED -> ESCALATION ---
        ctx_turn3 = PipelineContext.from_task_data("task-t3", {
            "client_id": client_id,
            "from_email": from_email,
            "subject": f"Re: {subject}",
            "body": "DNS flush did not work either. Still failing.",
            "thread_id": thread_id,
            "troubleshooting_step": 2,
            "is_resolved": False,
            "history": history_after_t2
        })

        # Test execute_tool_call for escalate_and_create_ticket to verify transcript inclusion
        with patch("app.pipeline.tools.create_ticket_and_reply") as mock_ticket_create:
            mock_ticket_create.return_value = ("Support ticket created", "TKT-ESCALATE-999", "ticket_created_and_sent")
            res_tool = execute_tool_call("escalate_and_create_ticket", {
                "issue_summary": "VPN connection failure persists after 2 diagnostic steps",
                "priority": "High"
            }, ctx_turn3)

            self.assertEqual(res_tool.get("status"), "ticket_created")
            self.assertEqual(res_tool.get("ticket_id"), "TKT-ESCALATE-999")

            # Verify that create_ticket_and_reply received context containing troubleshooting notes
            call_kwargs = mock_ticket_create.call_args[1]
            ticket_context = call_kwargs.get("context", "")
            self.assertIn("Troubleshooting History:", ticket_context)
            self.assertIn("restart your network adapter", ticket_context)
            self.assertIn("flush your DNS", ticket_context)

    def test_02_conversational_resolution_early_exit(self):
        """
        E2E Scenario 2: Conversational resolution shortcut.
        Turn 1: Diagnostic delivered.
        Turn 2: Customer confirms 'it worked! All good now.'
        Asserts: Resolution regex detects success, is_resolved=True, evaluator bypasses
                 ticket creation with score=95, decision='auto_send', and zero CRM tickets are opened.
        """
        # 1. Test resolution regex detector
        self.assertTrue(check_customer_resolution("That worked, thank you so much!"))
        self.assertTrue(check_customer_resolution("Problem is fixed now, all good"))
        self.assertTrue(check_customer_resolution("It is working properly now"))
        self.assertFalse(check_customer_resolution("This did not work at all"))

        # 2. Context with prior troubleshooting
        ctx = PipelineContext.from_task_data("task-res-test", {
            "client_id": "CLI-RESOLVE-TEST",
            "from_email": "customer@example.com",
            "subject": "Re: Connection issue",
            "body": "Thank you very much, that worked! The issue is fixed.",
            "thread_id": "th_resolved_001",
            "troubleshooting_step": 1,
            "is_resolved": False,
            "history": [
                {"role": "customer", "body": "Cannot connect"},
                {"role": "assistant", "body": "Please toggle airplane mode"}
            ]
        })

        with patch("app.pipeline.agent.client") as mock_llm_client:
            mock_llm_client.chat.completions.create.return_value = MagicMock(
                choices=[MagicMock(
                    message=MagicMock(
                        content="Dear Customer,\n\nWe are delighted to hear that resolved your issue! Please let us know if you need anything else.\n\nThanks & Regards,\nSupport Team",
                        tool_calls=None
                    )
                )]
            )
            res_ctx = run_support_agent(ctx)
            self.assertTrue(res_ctx.is_resolved)
            self.assertIn("delighted to hear", res_ctx.draft_reply)

        # 3. Verify evaluate_draft_and_decide respects is_resolved and bypasses ticket creation
        score, decision = evaluate_draft_and_decide(
            client_id=res_ctx.client_id,
            reply=res_ctx.draft_reply,
            query=res_ctx.body,
            context_succeeded=True,
            is_resolved=res_ctx.is_resolved
        )
        self.assertEqual(decision, "auto_send")
        self.assertEqual(score, 95)

    @patch("app.chat_history.get_db_ctx")
    def test_03_asynchronous_cold_cache_sql_reconstitution(self, mock_db_ctx):
        """
        E2E Scenario 3: Cold cache dialogue reconstitution.
        When Redis TTL expires or cache is cold, get_history_from_sql loads the full
        historical transcript from SQL email_logs using thread_id.
        """
        now = datetime.now()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # Returned rows from MySQL 'ORDER BY id DESC' (newest first).
        # Columns: (body, reply, subject, created_at, status, summary, troubleshooting_step)
        mock_cursor.fetchall.return_value = [
            ("Restarted client, still failing", "Let us check firewall rules", "Re: VPN issue", now, "sent", "summary 2", 2),
            ("My VPN is down", "Have you tried restarting the client?", "VPN issue", now, "sent", "summary 1", 1),
        ]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_db_ctx.return_value.__enter__.return_value = mock_conn

        history = get_history_from_sql(
            client_id="CLI-COLD-CACHE",
            from_email="user@example.com",
            thread_id="th_vpn_cold_start",
            limit=10
        )
        self.assertEqual(len(history), 4)  # 2 customer messages + 2 assistant replies
        self.assertEqual(history[0]["role"], "customer")
        self.assertEqual(history[0]["body"], "My VPN is down")
        self.assertEqual(history[1]["role"], "support")
        self.assertEqual(history[1]["body"], "Have you tried restarting the client?")
        self.assertEqual(history[2]["role"], "customer")
        self.assertEqual(history[2]["body"], "Restarted client, still failing")
        self.assertEqual(history[3]["role"], "support")
        self.assertEqual(history[3]["body"], "Let us check firewall rules")

    def test_04_hostile_cross_tenant_isolation_stress_test(self):
        """
        E2E Scenario 4: Hostile cross-tenant data and credential breach attempt.
        1. Encrypt secret under CLIENT_ALPHA. Decrypting under CLIENT_BETA must raise error.
        2. Vector store reject query/mutation on empty or wildcard 'ALL'.
        3. Fallback database files strictly partitioned by client ID.
        """
        tenant_alpha = "CLI-ALPHA-999"
        tenant_beta = "CLI-BETA-888"
        raw_secret = "secret_stripe_api_key_sk_live_alpha12345"

        # 1. HKDF Cryptographic Tenant Isolation
        encrypted_token = encrypt_secret(raw_secret, client_id=tenant_alpha)
        # Attempting decrypt with matching tenant succeeds
        decrypted_alpha = decrypt_secret(encrypted_token, client_id=tenant_alpha)
        self.assertEqual(decrypted_alpha, raw_secret)

        # Attempting decrypt with hostile tenant raises DecryptionError
        with self.assertRaises(Exception):
            decrypt_secret(encrypted_token, client_id=tenant_beta)

        # 2. Vector Store Guardrails Reject Wildcard & Cross-Tenant Queries
        with self.assertRaises(ValueError) as ctx_err:
            _validate_tenant_id("ALL", "search")
        self.assertIn("'ALL'", str(ctx_err.exception))

        with self.assertRaises(ValueError) as ctx_empty:
            _validate_tenant_id("", "search")
        self.assertIn("requires a valid client_id", str(ctx_empty.exception))

        # 3. Physical Partitioning File Paths
        from app.rag import get_fallback_path
        path_alpha = get_fallback_path(tenant_alpha)
        path_beta = get_fallback_path(tenant_beta)
        self.assertNotEqual(path_alpha, path_beta)
        self.assertTrue(path_alpha.endswith(f"fallback_{tenant_alpha.lower()}.json"))
        self.assertTrue(path_beta.endswith(f"fallback_{tenant_beta.lower()}.json"))

    @patch("app.pipeline.tools.fetch_crm_order_status")
    @patch("app.pipeline.tools.fetch_payment_status")
    @patch("app.pipeline.tools.fetch_crm_ticket_status")
    def test_05_discrete_multi_system_status_routing(
        self,
        mock_fetch_ticket,
        mock_fetch_payment,
        mock_fetch_order
    ):
        """
        E2E Scenario 5: Multi-System Discrete Status Routing.
        Ensures order queries route strictly to order connector, payment queries route
        strictly to payment connector, and ticket queries route strictly to ticket connector.
        """
        mock_fetch_order.return_value = {
            "success": True,
            "data": {"order_id": "ORD-7711", "status": "Shipped", "carrier": "FedEx"}
        }
        mock_fetch_payment.return_value = {
            "success": True,
            "data": {"payment_status": "Paid", "transaction_id": "tx_stripe_999", "amount": "$120.00"}
        }
        mock_fetch_ticket.return_value = {
            "success": True,
            "data": {"ticket_id": "T-4401", "ticket_status": "Open", "assignee": "Sarah"}
        }

        ctx = PipelineContext.from_task_data("task-multi", {
            "client_id": "CLI-ROUTING-TEST",
            "from_email": "shopper@domain.com",
            "subject": "Inquiries",
            "body": "Inquiry test"
        })

        # 1. Order Status Tool Dispatch
        res_order = execute_tool_call("lookup_order_status", {"order_id": "ORD-7711"}, ctx)
        self.assertEqual(res_order.get("status"), "found")
        self.assertEqual(res_order.get("order_id"), "ORD-7711")
        mock_fetch_order.assert_called_once()
        mock_fetch_payment.assert_not_called()
        mock_fetch_ticket.assert_not_called()

        # 2. Payment Status Tool Dispatch
        res_payment = execute_tool_call("lookup_payment_status", {"payment_id_or_order_id": "tx_stripe_999"}, ctx)
        self.assertEqual(res_payment.get("status"), "found")
        self.assertEqual(res_payment.get("payment_reference"), "tx_stripe_999")
        mock_fetch_payment.assert_called_once()
        mock_fetch_ticket.assert_not_called()

        # 3. Ticket Status Tool Dispatch
        res_ticket = execute_tool_call("lookup_ticket_status", {"ticket_id": "T-4401"}, ctx)
        self.assertEqual(res_ticket.get("status"), "found")
        self.assertEqual(res_ticket.get("ticket_id"), "T-4401")
        mock_fetch_ticket.assert_called_once()


if __name__ == "__main__":
    unittest.main()
