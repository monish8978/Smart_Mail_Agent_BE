"""
Automated Test Suite for Mail AI Automation Platform.
Covers API routers, deterministic pipeline filters, outbox idempotency, and agent tool calling.
"""
import sys
import os
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

