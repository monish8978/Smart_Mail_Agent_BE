import sys
sys.path.insert(0, '/app')

from app.llm import detect_intent_llm, generate_issue_resolved_reply, generate_off_topic_reply, scan_history_for_ticket
from worker.tasks import _is_pure_status_ping

# Real-world test scenarios
real_world_cases = [
    # Category 1: Issue Resolved (various realistic phrasings)
    {
        "category": "Issue Resolved",
        "query": "Subject: Login issue\n\nThanks team, our IT department reset the router and everything is working fine now. Please close this out.",
        "expected_intent": "issue_resolved"
    },
    {
        "category": "Issue Resolved",
        "query": "Subject: Re: [Ticket #120] Calls not routing\n\nNever mind, it was an issue on our telecom provider side. Problem is resolved now, thanks!",
        "expected_intent": "issue_resolved"
    },
    {
        "category": "Issue Resolved",
        "query": "Subject: Payment failed\n\nAll good now! The bank transaction just went through. No further action needed.",
        "expected_intent": "issue_resolved"
    },

    # Category 2: Off-topic / Nonsense / Casual Openers
    {
        "category": "Off-Topic / Nonsense",
        "query": "Subject: \n\nhello ladies",
        "expected_intent": "off_topic_nonsense"
    },
    {
        "category": "Off-Topic / Nonsense",
        "query": "Subject: \n\nwho is the president of the united states?",
        "expected_intent": "off_topic_nonsense"
    },
    {
        "category": "Off-Topic / Nonsense",
        "query": "Subject: Hi\n\nhey baby what are you doing tonight",
        "expected_intent": "off_topic_nonsense"
    },
    {
        "category": "Off-Topic / Nonsense",
        "query": "Subject: test\n\nasdfghjk qwerty 12345 ???",
        "expected_intent": "off_topic_nonsense"
    },
    {
        "category": "Off-Topic / Nonsense",
        "query": "Subject: Good morning\n\nGood morning team",
        "expected_intent": "off_topic_nonsense"
    },

    # Category 3: Genuine Support Inquiries (Must NOT be misclassified as off-topic or resolved)
    {
        "category": "Genuine General Query",
        "query": "Subject: Progressive mode issue\n\nOur agents are in progressive mode but staying Ready without receiving any calls. There are no browser console errors.",
        "expected_intent": "general_query"
    },
    {
        "category": "Genuine Status Inquiry",
        "query": "Subject: Ticket update\n\nCould you please check the status of ticket #T-260505-00117? We need an urgent update.",
        "expected_intent": "ticket_status"
    }
]

print("="*70)
print("🎯 1. TESTING INTENT CLASSIFICATION ON REAL-WORLD SAMPLES")
print("="*70)
passed = 0
failed = 0

for case in real_world_cases:
    query = case["query"]
    expected = case["expected_intent"]
    cat = case["category"]
    res = detect_intent_llm(query)
    actual = res.get("intent")
    ticket_ids = res.get("ticket_ids", [])
    sentiment = res.get("sentiment")
    priority = res.get("priority")
    
    status_icon = "✅" if actual == expected else "❌"
    if actual == expected:
        passed += 1
    else:
        failed += 1
        
    first_line = query.replace('\n', ' ')[:55]
    print(f"{status_icon} [{cat}] Query: '{first_line}...'")
    print(f"   -> Detected: intent='{actual}', tickets={ticket_ids}, sentiment='{sentiment}', priority='{priority}'")
    if actual != expected:
        print(f"   ⚠️ MISMATCH: Expected '{expected}', got '{actual}'")

print(f"\nIntent Test Summary: {passed}/{len(real_world_cases)} PASSED\n")

print("="*70)
print("🔍 2. TESTING COMPOUND STATUS PING VS PURE STATUS PING")
print("="*70)
status_test_queries = [
    ("what is the status of ticket #120?", True),
    ("any update on ticket #120?", True),
    ("please let me know the progress", True),
    ("can you specify what was the problem. which created the ticket", False),
    ("for what problem you created the ticket can you tell me", False),
    ("why did this issue occur in the first place?", False),
    ("can you explain what caused the dialer downtime?", False)
]

for q, expected in status_test_queries:
    res = _is_pure_status_ping(q)
    icon = "✅" if res == expected else "❌"
    mode = "Static Template" if res else "LLM Analytical Reply"
    print(f"{icon} Query: '{q}'")
    print(f"   -> Result: {res} ({mode}) | Expected: {expected}")
    assert res == expected, f"Failed on '{q}'"

print("\nStatus Ping Test Summary: ALL PASSED\n")

print("="*70)
print("📜 3. TESTING CONVERSATION HISTORY LINKING FOR FOLLOW-UP INQUIRIES")
print("="*70)
history = [
    {"role": "customer", "body": "Progressive dialer agents receiving no calls"},
    {"role": "support", "body": "We have created ticket #275424000000446001 to investigate."},
    {"role": "customer", "body": "okay thanks my issue is resolved"},
    {"role": "support", "body": "Glad to hear that!"}
]
followup_query = "can you specify what was the problem?"
scan_res = scan_history_for_ticket(followup_query, history)
print(f"Follow-up: '{followup_query}'")
print(f"Scan result: {scan_res}")
assert scan_res.get("found") is True and scan_res.get("ticket_id") == "275424000000446001", "History scan failed to link follow-up!"
print("✅ Follow-up inquiry successfully linked to existing ticket #275424000000446001 without opening a new ticket!")

print("\n" + "="*70)
print("✉️ 4. TESTING BOT RESPONSE GENERATION QUALITY")
print("="*70)
resolved_mail = generate_issue_resolved_reply("md.yazdani@c-zentrix.com", "Resolved", "Everything is working fine now, thank you!", ticket_id="275424000000446001")
print("Resolved Reply Sample:")
print("-" * 40)
print(resolved_mail)
print("-" * 40)

offtopic_mail = generate_off_topic_reply("md.yazdani@c-zentrix.com", "Hi", "who is the prime minister of india?")
print("\nOff-Topic Reply Sample:")
print("-" * 40)
print(offtopic_mail)
print("-" * 40)

print("\n🚀 ALL REAL WORLD TEST SUITES EXECUTED SUCCESSFULLY!")
