import unittest
from unittest.mock import MagicMock, patch
from app.pipeline.context import PipelineContext
from app.pipeline.tools import SUPPORT_TOOLS, execute_tool_call

class TestAgentTools(unittest.TestCase):

    def test_01_tool_schemas(self):
        """SUPPORT_TOOLS must declare all 6 functional tools with valid JSON schemas"""
        self.assertEqual(len(SUPPORT_TOOLS), 6)
        tool_names = [t["function"]["name"] for t in SUPPORT_TOOLS]
        self.assertIn("lookup_order_status", tool_names)
        self.assertIn("lookup_payment_status", tool_names)
        self.assertIn("lookup_ticket_status", tool_names)
        self.assertIn("lookup_ticket_or_order_status", tool_names)
        self.assertIn("search_knowledge_base", tool_names)
        self.assertIn("escalate_and_create_ticket", tool_names)

        for t in SUPPORT_TOOLS:
            fn = t["function"]
            self.assertIn("description", fn)
            self.assertIn("parameters", fn)
            params = fn["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("required", params)

    def test_02_unrecognized_tool_handling(self):
        """Unrecognized tool name must return structured error with available tools list"""
        ctx = PipelineContext.from_task_data("test-task-unknown", {
            "client_id": "CLI-08BDA27B",
            "from_email": "customer@example.com",
            "subject": "Unknown tool inquiry",
            "body": "test"
        })
        cursor = MagicMock()
        res = execute_tool_call("non_existent_tool_xyz", {}, ctx, cursor)
        self.assertEqual(res.get("status"), "unrecognized_tool")
        self.assertIn("available_tools", res)

    def test_03_knowledge_base_tool_execution(self):
        """search_knowledge_base tool must return structured results without crashing"""
        ctx = PipelineContext.from_task_data("test-task-rag", {
            "client_id": "CLI-08BDA27B",
            "from_email": "customer@example.com",
            "subject": "Help with login",
            "body": "How do I reset my password?"
        })
        cursor = MagicMock()
        res = execute_tool_call("search_knowledge_base", {"query": "reset password"}, ctx, cursor)
        self.assertIn("status", res)
        self.assertIn(res["status"], ("success", "empty"))
        self.assertIn("content", res)

    def test_04_ticket_status_lookup_handling(self):
        """lookup_ticket_or_order_status tool must return structured result for missing ID"""
        ctx = PipelineContext.from_task_data("test-task-crm", {
            "client_id": "CLI-08BDA27B",
            "from_email": "customer@example.com",
            "subject": "Ticket status",
            "body": "Checking ticket status"
        })
        cursor = MagicMock()
        res = execute_tool_call("lookup_ticket_or_order_status", {"ticket_id": "T-NONEXISTENT-999"}, ctx, cursor)
        self.assertIn("status", res)
        self.assertIn(res["status"], ("not_found", "lookup_failed", "found"))
        self.assertIn("message", res)

    def test_05_tool_exception_resilience(self):
        """Unexpected internal exceptions in tools must be caught and return structured error with hint"""
        ctx = PipelineContext.from_task_data("test-task-err", {
            "client_id": "CLI-08BDA27B",
            "from_email": "customer@example.com",
            "subject": "Error test",
            "body": "Test error"
        })
        cursor = MagicMock()

        with patch("app.pipeline.tools.fetch_crm_ticket_status", side_effect=RuntimeError("Simulated CRM Socket Timeout")):
            res = execute_tool_call("lookup_ticket_or_order_status", {"ticket_id": "123"}, ctx, cursor)
            # Depending on whether fetch_crm_ticket_status handles it or bubbles, execute_tool_call catches it cleanly
            self.assertIn("status", res)
            self.assertIn(res["status"], ("error", "lookup_failed"))
            self.assertIn("hint", res)


if __name__ == "__main__":
    unittest.main()
