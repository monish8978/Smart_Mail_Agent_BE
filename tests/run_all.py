#!/usr/bin/env python3
"""
Mail AI Automation Test Suite Runner.
Executes all automated tests across API routers, pipeline filters, outbox idempotency, and agent tools.
"""
import os
import sys
import time
import unittest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

def main():
    print("=" * 70)
    print("🧪 STARTING MAIL AI AUTOMATION TEST SUITE")
    print("=" * 70)

    test_dir = os.path.dirname(os.path.abspath(__file__))
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=test_dir, pattern="test_*.py")

    start_time = time.time()
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    elapsed = time.time() - start_time

    print("=" * 70)
    print(f"⏱️  Finished {result.testsRun} tests in {elapsed:.2f}s")
    print(f"📊 Results: Failures={len(result.failures)}, Errors={len(result.errors)}, Skipped={len(result.skipped)}")
    print("=" * 70)

    if not result.wasSuccessful():
        sys.exit(1)
    else:
        print("🎉 ALL TESTS PASSED SUCCESSFULLY!")
        sys.exit(0)

if __name__ == "__main__":
    main()
