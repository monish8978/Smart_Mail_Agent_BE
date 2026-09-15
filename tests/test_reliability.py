import unittest
import time
import os
import tempfile
from unittest.mock import patch

from app.llm_circuit_breaker import CircuitBreaker
from app.utils import normalize_subject


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1)

    def test_circuit_initially_closed(self):
        self.assertFalse(self.cb.is_open("openai"))

    def test_circuit_trips_after_threshold(self):
        self.cb.record_failure("groq")
        self.cb.record_failure("groq")
        self.assertFalse(self.cb.is_open("groq"))
        
        # 3rd failure hits threshold of 3
        self.cb.record_failure("groq")
        self.assertTrue(self.cb.is_open("groq"))

    def test_record_success_resets_counter(self):
        self.cb.record_failure("gemini")
        self.cb.record_failure("gemini")
        self.cb.record_success("gemini")
        self.assertFalse(self.cb.is_open("gemini"))
        
        # Another failure starts from 1, not 3
        self.cb.record_failure("gemini")
        self.assertFalse(self.cb.is_open("gemini"))

    def test_circuit_recovers_after_timeout(self):
        self.cb.record_failure("anthropic")
        self.cb.record_failure("anthropic")
        self.cb.record_failure("anthropic")
        self.assertTrue(self.cb.is_open("anthropic"))

        # Wait for recovery timeout (1 sec)
        time.sleep(1.1)
        self.assertFalse(self.cb.is_open("anthropic"))


class TestFallbackDbConcurrency(unittest.TestCase):
    def test_save_and_load_fallback_db(self):
        import sys
        from unittest.mock import MagicMock
        sys.modules.setdefault("qdrant_client", MagicMock())
        sys.modules.setdefault("qdrant_client.models", MagicMock())
        sys.modules.setdefault("qdrant_client.http", MagicMock())
        sys.modules.setdefault("qdrant_client.http.exceptions", MagicMock())
        import app.rag as rag
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db_path = os.path.join(tmpdir, "test_fallback.json")
            with patch.object(rag, "FALLBACK_DB_PATH", test_db_path):
                # Empty initially
                self.assertEqual(rag.load_fallback_db(), {})

                # Save data
                payload = {"doc_1": {"text": "hello world", "client_id": "test_client"}}
                rag.save_fallback_db(payload)

                # Load data
                loaded = rag.load_fallback_db()
                self.assertEqual(loaded, payload)


class TestNormalizeSubject(unittest.TestCase):
    def test_fallback_to_body_first_line(self):
        self.assertEqual(normalize_subject("", "Need password reset urgently\nMore details..."), "Need password reset urgently")
        self.assertEqual(normalize_subject("(no subject)", "Billing discrepancy noticed"), "Billing discrepancy noticed")

    def test_fallback_to_default(self):
        self.assertEqual(normalize_subject(""), "Support Request")
        self.assertEqual(normalize_subject("   "), "Support Request")
        self.assertEqual(normalize_subject("none"), "Support Request")

    def test_valid_subject_preserved(self):
        self.assertEqual(normalize_subject("Re: Urgent issue"), "Re: Urgent issue")


class TestTelemetryLLMClient(unittest.TestCase):
    def test_client_structure(self):
        from app.llm_config import client, TelemetryLLMClient
        self.assertIsInstance(client, TelemetryLLMClient)
        self.assertTrue(hasattr(client, "chat"))
        self.assertTrue(hasattr(client.chat, "completions"))
        self.assertTrue(hasattr(client.chat.completions, "create"))

    @patch("app.llm_config.get_llm_config_for_client")
    @patch("app.llm_config.get_dynamic_client")
    @patch("app.llm_config.log_llm_metrics_db")
    def test_client_create_with_explicit_caller(self, mock_log_db, mock_get_client, mock_get_cfg):
        import sys
        from unittest.mock import MagicMock
        from app.llm_config import client
        mock_get_cfg.return_value = {
            "id": 1,
            "provider": "groq",
            "model_name": "llama-3.3-70b-versatile",
            "api_key": "test_key",
            "base_url": None
        }
        mock_target = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "Test response"
        mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=20)
        mock_target.chat.completions.create.return_value = mock_resp
        mock_get_client.return_value = mock_target

        res = client.chat.completions.create(
            messages=[{"role": "user", "content": "hello"}],
            caller="test_unit_caller"
        )
        self.assertEqual(res, mock_resp)
        mock_get_cfg.assert_called_with("SYSTEM", "test_unit_caller")
        mock_log_db.assert_called()
        self.assertEqual(mock_log_db.call_args[0][6], "test_unit_caller")


class TestImapBoundedReader(unittest.TestCase):
    def setUp(self):
        from worker.imap_reader import _account_cooldowns
        _account_cooldowns.clear()

    def test_account_cooldown_lifecycle(self):
        from worker.imap_reader import is_in_cooldown, set_account_cooldown, clear_account_cooldown
        self.assertFalse(is_in_cooldown("CLI-TEST-1"))

        set_account_cooldown("CLI-TEST-1", duration=10)
        self.assertTrue(is_in_cooldown("CLI-TEST-1"))

        clear_account_cooldown("CLI-TEST-1")
        self.assertFalse(is_in_cooldown("CLI-TEST-1"))

    @patch("worker.imap_reader.is_bot_enabled_for_client")
    def test_check_mailbox_skips_when_bot_disabled(self, mock_bot_enabled):
        from worker.imap_reader import check_client_mailbox
        mock_bot_enabled.return_value = (False, "Disabled by Administrator")
        
        result = check_client_mailbox("CLI-DISABLED", "test@example.com", "secret")
        self.assertEqual(result, 0)

    @patch("worker.imap_reader.is_bot_enabled_for_client")
    def test_check_mailbox_skips_when_in_cooldown(self, mock_bot_enabled):
        from worker.imap_reader import check_client_mailbox, set_account_cooldown
        mock_bot_enabled.return_value = (True, "Active")
        set_account_cooldown("CLI-COOLDOWN", duration=60)

        result = check_client_mailbox("CLI-COOLDOWN", "test@example.com", "secret")
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
