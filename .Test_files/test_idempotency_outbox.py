import sys
sys.path.insert(0, '/app')

from app.action_outbox import execute_idempotent_action, compute_idempotency_key
from app.migrations.init_schema import ensure_action_logs_table
from app.db import get_db_ctx

def test_outbox():
    print("🛠️ Testing Action Outbox...")
    ensure_action_logs_table()

    client_id = "test_client_outbox"
    message_id = "msg-12345-abcde"
    action_type = "crm_ticket_create"

    call_count = 0

    def mock_crm_create():
        nonlocal call_count
        call_count += 1
        return {"ticket_id": "T-260526-99999", "status": "Success"}

    # Clean up test row if exists
    key = compute_idempotency_key(client_id, message_id, action_type)
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM action_logs WHERE idempotency_key = %s", (key,))
        db.commit()

    # Call 1: should execute action
    res1 = execute_idempotent_action(client_id, message_id, action_type, mock_crm_create)
    assert res1["success"] is True
    assert res1["already_completed"] is False
    assert res1["external_ref"] == "T-260526-99999"
    assert call_count == 1
    print("✅ First call succeeded and called external action once.")

    # Call 2: with identical client_id, message_id, action_type -> MUST NOT call mock_crm_create
    res2 = execute_idempotent_action(client_id, message_id, action_type, mock_crm_create)
    assert res2["success"] is True
    assert res2["already_completed"] is True
    assert res2["external_ref"] == "T-260526-99999"
    assert call_count == 1, f"Expected call_count 1, got {call_count}! Idempotency violated!"
    print("✅ Second call successfully deduplicated! External action was NOT called again.")

    # Clean up test row
    with get_db_ctx() as db:
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM action_logs WHERE idempotency_key = %s", (key,))
        db.commit()

    print("🎉 ALL IDEMPOTENCY OUTBOX TESTS PASSED!")

if __name__ == "__main__":
    test_outbox()
