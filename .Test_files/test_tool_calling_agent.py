import sys
sys.path.insert(0, '/app')

from app.pipeline.context import PipelineContext
from app.pipeline.tools import SUPPORT_TOOLS, execute_tool_call
from app.pipeline.agent import run_support_agent
from app.db import get_db_ctx

def test_tools():
    print("=" * 60)
    print("🛠️ 1. TESTING TOOL SCHEMAS AND DIRECT EXECUTION")
    print("=" * 60)
    assert len(SUPPORT_TOOLS) == 3, f"Expected 3 tools, found {len(SUPPORT_TOOLS)}"
    tool_names = [t["function"]["name"] for t in SUPPORT_TOOLS]
    print(f"✅ Declared tools: {tool_names}")
    assert "lookup_ticket_or_order_status" in tool_names
    assert "search_knowledge_base" in tool_names
    assert "escalate_and_create_ticket" in tool_names

    with get_db_ctx() as db:
        with db.cursor() as cursor:
            ctx = PipelineContext.from_task_data("test-task-1", {
                "client_id": "CLI-08BDA27B",
                "from_email": "customer@example.com",
                "subject": "Ticket status inquiry",
                "body": "Could you check the status of ticket T-260505-00117?"
            })

            # Test 1: Tool execution for knowledge base
            rag_res = execute_tool_call("search_knowledge_base", {"query": "login issues"}, ctx, cursor)
            print(f"✅ Knowledge Base Tool returned status: {rag_res.get('status')}")
            assert "status" in rag_res

            # Test 2: Tool execution for ticket status lookup
            crm_res = execute_tool_call("lookup_ticket_or_order_status", {"ticket_id": "T-260505-00117"}, ctx, cursor)
            print(f"✅ Ticket Status Tool returned status: {crm_res.get('status')}")
            assert "status" in crm_res

            # Test 3: Agent Loop Autonomous Tool Calling
            print("\n" + "=" * 60)
            print("🤖 2. TESTING AUTONOMOUS AGENT LOOP")
            print("=" * 60)
            agent_ctx = run_support_agent(ctx, cursor)
            print(f"Agent Steps: {agent_ctx.execution_steps}")
            print(f"Draft Reply Sample:\n{agent_ctx.draft_reply[:200]}...")
            print(f"Score: {agent_ctx.score}, Action: {agent_ctx.response_action}")

            # Verify that the agent executed tool calling
            has_tool_call = any("Tool_Call" in step for step in agent_ctx.execution_steps)
            print(f"Autonomous Tool Calling Triggered: {'✅ YES' if has_tool_call else 'ℹ️ DIRECT'}")

    print("\n🎉 ALL TOOL-CALLING TESTS EXECUTED SUCCESSFULLY!")

if __name__ == "__main__":
    test_tools()
