import os
import unittest
from cryptography.fernet import InvalidToken

from app.secrets_crypto import encrypt_secret, decrypt_secret
from app import vector_store
from app import rag


class TestMultiTenancyIsolation(unittest.TestCase):

    def test_01_hkdf_tenant_credential_isolation(self):
        """Tokens encrypted for Tenant A MUST NOT be decryptable by Tenant B."""
        secret_text = "zoho_desk_super_secret_token_123"
        token_a = encrypt_secret(secret_text, client_id="CLI-TENANT-A")

        # Tenant A can decrypt successfully
        self.assertEqual(decrypt_secret(token_a, client_id="CLI-TENANT-A"), secret_text)

        # Tenant B MUST fail to decrypt with InvalidToken
        with self.assertRaises(InvalidToken):
            decrypt_secret(token_a, client_id="CLI-TENANT-B")

        # Global master key MUST NOT decrypt a tenant-derived token
        with self.assertRaises(InvalidToken):
            decrypt_secret(token_a, client_id=None)

    def test_02_legacy_credential_migration_fallback(self):
        """Tokens encrypted with legacy global master key must transparently decrypt for any client."""
        legacy_secret = "legacy_pre_migration_password"
        token_legacy = encrypt_secret(legacy_secret, client_id=None)

        # Legacy token decrypts with client_id via fallback
        self.assertEqual(decrypt_secret(token_legacy, client_id="CLI-TENANT-A"), legacy_secret)
        self.assertEqual(decrypt_secret(token_legacy, client_id="CLI-TENANT-B"), legacy_secret)
        self.assertEqual(decrypt_secret(token_legacy, client_id=None), legacy_secret)

    def test_03_vector_store_tenant_guardrails(self):
        """Vector store operations MUST refuse empty or 'ALL' client_id."""
        dummy_vec = [0.01] * 384

        # Search guardrail
        with self.assertRaises(ValueError):
            vector_store.search("ALL", dummy_vec)
        with self.assertRaises(ValueError):
            vector_store.search("", dummy_vec)
        with self.assertRaises(ValueError):
            vector_store.search("   ", dummy_vec)

        # Upsert guardrail
        with self.assertRaises(ValueError):
            vector_store.upsert_chunks("ALL", "doc1", "title", ["chunk"], [dummy_vec])
        with self.assertRaises(ValueError):
            vector_store.upsert_chunks("", "doc1", "title", ["chunk"], [dummy_vec])

        # Document listing guardrail
        with self.assertRaises(ValueError):
            vector_store.get_client_documents("ALL")
        with self.assertRaises(ValueError):
            vector_store.get_client_documents("")

        # Document deletion guardrail
        with self.assertRaises(ValueError):
            vector_store.delete_document("ALL", "doc1")
        with self.assertRaises(ValueError):
            vector_store.delete_client_data("ALL")

    def test_04_fallback_store_file_isolation(self):
        """Fallback knowledge store MUST physically segregate JSON files per tenant."""
        client_a = "CLI-ISOLATION-TEST-A"
        client_b = "CLI-ISOLATION-TEST-B"

        docs_a = [{"id": "doc_a1", "title": "A Policy", "content": "Exclusive warranty policy for Client A"}]
        docs_b = [{"id": "doc_b1", "title": "B Policy", "content": "Exclusive return policy for Client B"}]

        path_a = rag.get_fallback_path(client_a)
        path_b = rag.get_fallback_path(client_b)

        # Ensure distinct paths
        self.assertNotEqual(path_a, path_b)
        self.assertTrue(path_a.endswith("fallback_cli-isolation-test-a.json"))
        self.assertTrue(path_b.endswith("fallback_cli-isolation-test-b.json"))

        try:
            rag.save_fallback_db(client_a, docs_a)
            rag.save_fallback_db(client_b, docs_b)

            # Check loading A only gets A
            loaded_a = rag.load_fallback_db(client_a)
            self.assertEqual(len(loaded_a), 1)
            self.assertEqual(loaded_a[0]["id"], "doc_a1")
            self.assertNotIn("Client B", loaded_a[0]["content"])

            # Check loading B only gets B
            loaded_b = rag.load_fallback_db(client_b)
            self.assertEqual(len(loaded_b), 1)
            self.assertEqual(loaded_b[0]["id"], "doc_b1")
            self.assertNotIn("Client A", loaded_b[0]["content"])

            # Deleting from A does not touch B
            rag.delete_knowledge(client_a, "doc_a1")
            self.assertEqual(len(rag.load_fallback_db(client_a)), 0)
            self.assertEqual(len(rag.load_fallback_db(client_b)), 1)

        finally:
            # Cleanup test files
            for p in [path_a, path_b]:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    def test_05_query_knowledge_rejects_wildcard(self):
        """query_knowledge MUST reject client_id='ALL' or empty client_id."""
        with self.assertRaises(ValueError):
            rag.query_knowledge("ALL", "return policy")
        with self.assertRaises(ValueError):
            rag.query_knowledge("", "return policy")
        with self.assertRaises(ValueError):
            rag.query_knowledge(None, "return policy")


if __name__ == "__main__":
    unittest.main()
