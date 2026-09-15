import sys
import os
import json
sys.path.insert(0, '/app')

from app.pipeline.context import PipelineContext
from app.pipeline.filters import apply_deterministic_filters
from app.db import get_db_ctx

def test_syntax_and_imports():
    print("=" * 60)
    print("🧪 1. TESTING WORKER TASKS SYNTAX & IMPORTS")
    print("=" * 60)
    import worker.tasks as tasks
    assert hasattr(tasks, "process_email_task")
    assert hasattr(tasks, "get_client_features")
    assert hasattr(tasks, "publish_email_update")
    assert hasattr(tasks, "_finalize_task_and_log")
    print("✅ worker.tasks successfully imported with all required symbols")


def test_deterministic_filters_unit():
    print("\n" + "=" * 60)
    print("🧪 2. TESTING DETERMINISTIC FILTERS (LEVEL 0)")
    print("=" * 60)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            # Test A: Bounce detection
            ctx_bounce = PipelineContext.from_task_data("test-bounce-1", {
                "client_id": "CLI-08BDA27B",
                "from_email": "mailer-daemon@google.com",
                "subject": "Delivery Status Notification (Failure)",
                "body": "The user you are trying to reach does not exist."
            })
            halted = apply_deterministic_filters(ctx_bounce, cursor)
            print(f"Bounce halted: {halted}, status: {ctx_bounce.status}")
            assert halted is True
            assert ctx_bounce.status == "system_bounce_dropped"

            # Test B: Marketing Sender
            ctx_mkt = PipelineContext.from_task_data("test-mkt-1", {
                "client_id": "CLI-08BDA27B",
                "from_email": "newsletter@promotions.com",
                "subject": "Special Weekend Discounts 50% Off",
                "body": "Check out our exclusive weekend sale!"
            })
            # Insert temporary marketing sender rule to test
            cursor.execute("SELECT id FROM marketing_senders WHERE client_id = %s AND sender_email = %s",
                           ("CLI-08BDA27B", "newsletter@promotions.com"))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO marketing_senders (client_id, sender_email) VALUES (%s, %s)",
                               ("CLI-08BDA27B", "newsletter@promotions.com"))
                db.commit()

            halted_mkt = apply_deterministic_filters(ctx_mkt, cursor)
            print(f"Marketing halted: {halted_mkt}, status: {ctx_mkt.status}")
            assert halted_mkt is True
            assert ctx_mkt.status == "no_action_needed"

            print("✅ Deterministic Level 0 filters verified successfully!")


def test_multi_ticket_detection():
    print("\n" + "=" * 60)
    print("🧪 3. TESTING MULTI-TICKET REGEX DETECTION")
    print("=" * 60)
    from app.llm import extract_ticket_and_order_ids
    test_body = "Hi, can you update me on #120 and also ticket T-260505-00117?"
    ids = extract_ticket_and_order_ids(test_body)
    print(f"Extracted IDs: {ids}")
    assert len(ids) == 2
    print("✅ Multi-ticket regex correctly identifies multiple distinct IDs!")


if __name__ == "__main__":
    test_syntax_and_imports()
    test_deterministic_filters_unit()
    test_multi_ticket_detection()
    print("\n🎉 ALL CONSOLIDATED WORKER TESTS PASSED!")
