import unittest
from unittest.mock import MagicMock, patch

from app.mcp_server import (
    get_email_account_tool,
    get_order_status_tool,
    get_payment_status_tool,
    get_ticket_status_tool,
    create_support_ticket_tool,
    query_rag_knowledge_tool,
    add_rag_knowledge_tool,
    send_email_tool
)


class TestMCPServer(unittest.TestCase):

    @patch("app.mcp_server.get_email_account")
    def test_01_get_email_account_tool(self, mock_get_acc):
        """MCP get_email_account_tool must return account details or structured error"""
        mock_get_acc.return_value = {"email": "support@example.com", "score_threshold": 80}
        res = get_email_account_tool("CLI-TEST")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["account"]["email"], "support@example.com")

        mock_get_acc.return_value = None
        res_err = get_email_account_tool("CLI-MISSING")
        self.assertEqual(res_err["status"], "error")
        self.assertIn("No email account", res_err["message"])

    @patch("app.connector_config.run_order_status_lookup")
    def test_02_get_order_status_tool_dynamic(self, mock_lookup):
        """MCP get_order_status_tool must query dynamic connector and return when successful"""
        mock_lookup.return_value = {
            "success": True,
            "order_id": "ORD-1234",
            "order_status": "Shipped",
            "tracking_number": "TRK987"
        }
        res = get_order_status_tool("CLI-TEST", "ORD-1234")
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("order_status"), "Shipped")

    @patch("app.mcp_server.get_order_status")
    @patch("app.connector_config.run_order_status_lookup")
    def test_03_get_order_status_tool_legacy_fallback(self, mock_lookup, mock_legacy):
        """MCP get_order_status_tool must fallback to legacy handler if dynamic connector has no config"""
        mock_lookup.return_value = {"success": False, "error": "No connector"}
        mock_legacy.return_value = {"status": "success", "data": {"status": "In Transit"}}

        res = get_order_status_tool("CLI-LEGACY", "ORD-OLD")
        self.assertEqual(res.get("status"), "success")
        mock_legacy.assert_called_once_with("CLI-LEGACY", "ORD-OLD")

    @patch("app.connector_config.run_payment_status_lookup")
    def test_04_get_payment_status_tool(self, mock_payment):
        """MCP get_payment_status_tool must route to run_payment_status_lookup"""
        mock_payment.return_value = {
            "success": True,
            "payment_status": "succeeded",
            "amount": 49.99
        }
        res = get_payment_status_tool("CLI-TEST", "tx_999")
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("payment_status"), "succeeded")
        mock_payment.assert_called_once_with(
            client_id="CLI-TEST",
            payment_id="tx_999",
            order_id="tx_999"
        )

    @patch("app.connector_config.run_ticket_status_lookup")
    def test_05_get_ticket_status_tool(self, mock_ticket):
        """MCP get_ticket_status_tool must route to run_ticket_status_lookup"""
        mock_ticket.return_value = {
            "success": True,
            "ticket_status": "In Progress",
            "ticket_id": "T-1001"
        }
        res = get_ticket_status_tool("CLI-TEST", "T-1001")
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("ticket_status"), "In Progress")

    @patch("app.mcp_server.query_knowledge")
    @patch("app.mcp_server.add_knowledge")
    def test_06_rag_tools(self, mock_add, mock_query):
        """MCP query and add knowledge tools must invoke RAG service"""
        mock_query.return_value = "Policy content"
        res_query = query_rag_knowledge_tool("CLI-TEST", "refund")
        self.assertEqual(res_query["status"], "success")
        self.assertEqual(res_query["context"], "Policy content")

        mock_add.return_value = "doc-uuid-123"
        res_add = add_rag_knowledge_tool("CLI-TEST", "FAQ", "Content")
        self.assertEqual(res_add["status"], "success")
        self.assertEqual(res_add["document_id"], "doc-uuid-123")

    @patch("app.mcp_server.send_email")
    @patch("app.mcp_server.create_email_record_db")
    def test_07_support_ticket_and_email_tools(self, mock_record, mock_send):
        """MCP create_support_ticket and send_email tools must execute correctly"""
        mock_record.return_value = {"status": "success", "ticket_id": 42}
        res_ticket = create_support_ticket_tool(
            client_id="CLI-TEST",
            mail_id="customer@example.com",
            subject="Help",
            body="Broken item"
        )
        self.assertEqual(res_ticket["status"], "success")

        mock_send.return_value = True
        res_mail = send_email_tool("CLI-TEST", "customer@example.com", "Resolved", "Here is your solution")
        self.assertEqual(res_mail["status"], "success")


if __name__ == "__main__":
    unittest.main()
