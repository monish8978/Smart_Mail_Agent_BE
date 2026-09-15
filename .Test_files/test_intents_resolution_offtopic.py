import os
import sys

# Add project root to sys.path
sys.path.insert(0, '/home/hyper_is_op/mail_ai_automation')

from app.llm import detect_intent_llm, generate_issue_resolved_reply, generate_off_topic_reply

def test_intent_detection():
    test_cases = [
        ("Thank you, my issue with the login is resolved now. Everything is working fine.", "issue_resolved"),
        ("Problem is resolved, thank you for your help!", "issue_resolved"),
        ("Never mind, I managed to fix it myself. Please close the ticket.", "issue_resolved"),
        ("asdfghjk qwertyuiop 12345 ???", "off_topic_nonsense"),
        ("tell me a story about aliens in mars", "off_topic_nonsense"),
        ("Can you please check the status of ticket #T-260505-00117?", "ticket_status"),
        ("I want to raise a complaint, my package never arrived and order #12345 is broken", "general_query"),
    ]

    print("--- Running Intent Detection Tests ---")
    for query, expected_intent in test_cases:
        try:
            res = detect_intent_llm(query)
            actual_intent = res.get("intent")
            sentiment = res.get("sentiment")
            priority = res.get("priority")
            print(f"Query: {query[:50]}...")
            print(f"  -> Result: intent='{actual_intent}', sentiment='{sentiment}', priority='{priority}'")
            if expected_intent == "general_query":
                assert actual_intent in ("general_query", "ticket_create"), f"Expected general_query or ticket_create, got {actual_intent}"
            else:
                assert actual_intent == expected_intent, f"Expected {expected_intent}, got {actual_intent}"
            print("  ✅ PASSED")
        except Exception as e:
            print(f"  ❌ FAILED: {e}")
            raise e

def test_reply_generation():
    print("\n--- Running Reply Generation Tests ---")
    res_reply = generate_issue_resolved_reply("john.doe@example.com", "Issue fixed", "The problem is resolved now, thanks!", ticket_id="T-12345")
    print(f"Issue Resolved Reply:\n{res_reply}\n")
    assert "john" in res_reply.lower() or "customer" in res_reply.lower(), "Should address customer"
    assert "resolve" in res_reply.lower() or "glad" in res_reply.lower() or "sort" in res_reply.lower(), "Should acknowledge resolution"

    off_reply = generate_off_topic_reply("alice@example.com", "hello", "asdfghjk qwertyuiop 12345")
    print(f"Off Topic Reply:\n{off_reply}\n")
    assert "alice" in off_reply.lower() or "customer" in off_reply.lower(), "Should address customer"
    assert "support" in off_reply.lower() or "assist" in off_reply.lower(), "Should mention support/assistance"
    print("✅ Reply generation tests passed successfully")

if __name__ == "__main__":
    test_intent_detection()
    test_reply_generation()
