import unittest
import json
from unittest.mock import MagicMock, patch

from app.connector_config.validation import _validate_response_mapping_fields
from app.connector_config.exceptions import ResponseMappingValidationError
from app.connector_config.dispatch import (
    run_payment_status_lookup,
    run_ticket_status_lookup,
    run_order_status_lookup,
)
from app.pipeline.context import PipelineContext
from app.pipeline.tools import execute_tool_call
from app.order_routes import get_order_by_id


class TestConnectorsPaymentAndRouting(unittest.TestCase):

    def test_01_validation_payment_status_required_fields(self):
        """Validates that payment_status response_mapping requires 'payment_status' field."""
        # Empty mapping should fail
        with self.assertRaises(ResponseMappingValidationError):
            _validate_response_mapping_fields("payment_status", "")

        # Missing payment_status field should fail
        invalid_mapping = json.dumps({"transaction_id": "data.id", "amount": "data.total"})
        with self.assertRaises(ResponseMappingValidationError):
            _validate_response_mapping_fields("payment_status", invalid_mapping)

        # Valid mapping should succeed
        valid_dict = {"payment_status": "data.status", "transaction_id": "data.id"}
        _validate_response_mapping_fields("payment_status", json.dumps(valid_dict))

        # Array format {"fields": [...]} should succeed
        valid_array = {"fields": [{"field": "payment_status", "path": "status"}]}
        _validate_response_mapping_fields("payment_status", json.dumps(valid_array))

    @patch("app.connector_config.dispatch.get_live_config")
    def test_02_payment_status_no_live_config(self, mock_get_cfg):
        """run_payment_status_lookup returns error when no live connector is configured."""
        mock_get_cfg.return_value = None
        res = run_payment_status_lookup(
            client_id="CLIENT_NO_CONFIG",
            payment_id="pi_12345",
            order_id="ORD-999",
        )
        self.assertFalse(res.get("success"))
        self.assertIn("No live payment_status connector", res.get("error", ""))

    @patch("app.connector_executor.execute_connector")
    @patch("app.connector_config.dispatch.get_live_config")
    def test_03_payment_status_success(self, mock_get_cfg, mock_exec):
        """run_payment_status_lookup returns mapped data and passes payment/order IDs in context."""
        mock_get_cfg.return_value = {
            "client_id": "CLIENT_TEST_PAY",
            "trigger_type": "payment_status",
            "url": "https://api.stripe.com/v1/payment_intents/{{payment_id}}",
            "http_method": "GET",
        }
        mock_exec.return_value = {
            "success": True,
            "data": {
                "payment_status": "succeeded",
                "amount": "2500.00",
                "transaction_id": "pi_12345",
            }
        }

        res = run_payment_status_lookup(
            client_id="CLIENT_TEST_PAY",
            payment_id="pi_12345",
            order_id="ORD-999",
            from_email="payer@example.com"
        )
        self.assertTrue(res.get("success"))
        data = res.get("data", {})
        self.assertEqual(data.get("payment_status"), "succeeded")
        self.assertEqual(data.get("transaction_id"), "pi_12345")

        # Verify context passed to executor contains payment_id and order_id
        call_args = mock_exec.call_args
        context_base = call_args[0][1]
        self.assertEqual(context_base.get("payment_id"), "pi_12345")
        self.assertEqual(context_base.get("order_id"), "ORD-999")

    @patch("app.connector_executor.execute_connector")
    @patch("app.connector_config.dispatch.get_live_config")
    def test_04_ticket_status_fallback_to_order_status_config(self, mock_get_cfg, mock_exec):
        """run_ticket_status_lookup falls back to order_status config if ticket_status is not set."""
        def side_effect(client_id, trigger_type):
            if trigger_type == "ticket_status":
                return None
            if trigger_type == "order_status":
                return {
                    "client_id": client_id,
                    "trigger_type": "order_status",
                    "url": "https://api.crm.com/tickets/{{ticket_id}}",
                    "http_method": "GET",
                }
            return None

        mock_get_cfg.side_effect = side_effect
        mock_exec.return_value = {
            "success": True,
            "data": {
                "ticket_status": "In Progress",
                "docket_no": "T-1002"
            }
        }

        res = run_ticket_status_lookup("CLIENT_FALLBACK", "T-1002")
        self.assertTrue(res.get("success"))
        self.assertEqual(res.get("data", {}).get("ticket_status"), "In Progress")

    @patch("app.pipeline.tools.fetch_crm_order_status")
    def test_05_execute_tool_call_lookup_order_status(self, mock_fetch_order):
        """execute_tool_call executes lookup_order_status and populates context_data."""
        mock_fetch_order.return_value = {
            "success": True,
            "data": {
                "docket_no": "1001",
                "ticket_status": "Shipped",
                "tracking_url": "https://carrier.com/track/1001"
            }
        }

        ctx = PipelineContext.from_task_data("task-order", {
            "client_id": "CLIENT_DISPATCH",
            "from_email": "buyer@example.com",
            "subject": "Where is my order #1001",
            "body": "Checking order #1001"
        })
        res = execute_tool_call("lookup_order_status", {"order_id": "#1001"}, ctx)
        self.assertEqual(res.get("status"), "found")
        self.assertEqual(res.get("order_id"), "1001")
        self.assertEqual(ctx.context_data.get("ticket_status"), "Shipped")

    @patch("app.pipeline.tools.fetch_payment_status")
    def test_06_execute_tool_call_lookup_payment_status(self, mock_fetch_payment):
        """execute_tool_call executes lookup_payment_status and populates context_data."""
        mock_fetch_payment.return_value = {
            "success": True,
            "data": {
                "payment_status": "Captured",
                "amount": "$49.99",
                "transaction_id": "ch_98124"
            }
        }

        ctx = PipelineContext.from_task_data("task-payment", {
            "client_id": "CLIENT_DISPATCH",
            "from_email": "payer@example.com",
            "subject": "Charged twice",
            "body": "Payment reference ch_98124"
        })
        res = execute_tool_call("lookup_payment_status", {"payment_id_or_order_id": "ch_98124"}, ctx)
        self.assertEqual(res.get("status"), "found")
        self.assertEqual(res.get("payment_reference"), "ch_98124")
        self.assertEqual(ctx.context_data.get("payment_status"), "Captured")

    @patch("app.order_routes.get_order_status")
    @patch("app.connector_config.run_order_status_lookup")
    def test_07_split_brain_bridge_priority(self, mock_dyn_order, mock_legacy_order):
        """get_order_by_id prefers dynamic connector, falling back to legacy request_handler."""
        # 1. Dynamic connector success
        mock_dyn_order.return_value = {
            "success": True,
            "data": {"order_id": "ORD-501", "status": "Delivered"}
        }
        order = get_order_by_id("CLI-BRIDGE", "ORD-501")
        self.assertIsNotNone(order)
        self.assertEqual(order.get("status"), "Delivered")
        mock_legacy_order.assert_not_called()

        # 2. Dynamic connector fails / not configured -> fallback to legacy
        mock_dyn_order.return_value = {"success": False, "error": "No config"}
        mock_legacy_order.return_value = {
            "success": True,
            "data": {"order_id": "ORD-501", "status": "Legacy Shipped"}
        }
        legacy_order = get_order_by_id("CLI-BRIDGE", "ORD-501")
        self.assertIsNotNone(legacy_order)
        self.assertEqual(legacy_order.get("status"), "Legacy Shipped")
        mock_legacy_order.assert_called_once_with("CLI-BRIDGE", "ORD-501")


if __name__ == "__main__":
    unittest.main()
