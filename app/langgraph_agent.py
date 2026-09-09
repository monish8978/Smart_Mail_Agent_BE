import os
import sys
import asyncio
import logging
from typing import Dict, Any

# Ensure project root is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("langgraph_agent")

async def run_mcp_agent(client_id: str, customer_email: str, email_body: str) -> Dict[str, Any]:
    """
    Spins up a LangGraph agent, connects to the MCP tool server, and orchestrates
    the automated customer support, ticketing, and email reply dispatching.
    
    Args:
        client_id: The unique identifier of the client.
        customer_email: The email of the customer.
        email_body: The query email body received.
    """
    logger.info("🤖 Preparing LangGraph MCP ReAct Agent...")

    # Get absolute path to the MCP server script
    mcp_server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
    
    # Configure the MCP server client to spin up the local server process
    mcp_config = {
        "mail_ai_server": {
            "command": sys.executable,  # Use same python executable
            "args": [mcp_server_path],
            "transport": "stdio"
        }
    }

    # Resolve the LangChain Chat Model dynamically using database settings
    from app.llm import resolve_langchain_model
    try:
        model = resolve_langchain_model(client_id, "run_mcp_agent", temperature=0.2)
    except Exception as e:
        logger.error(f"❌ Failed to resolve LLM config for agent: {e}")
        return {"status": "error", "message": f"LLM configuration error: {e}"}

    logger.info("🔌 Connecting to MCP Server and loading dynamic tools...")
    
    # Connect asynchronously to the MCP server
    async with MultiServerMCPClient(mcp_config) as mcp_client:
        # Dynamically discover all tools exposed by the MCP server
        mcp_tools = await mcp_client.get_tools()
        logger.info(f"✅ Discovered {len(mcp_tools)} tools from MCP server:")
        for tool in mcp_tools:
            logger.info(f" - {tool.name}: {tool.description[:70]}...")

        # System instructions to direct the LangGraph ReAct agent's decision making
        system_instruction = f"""
        You are a highly efficient Mail AI Automation agent. 
        Your goal is to handle incoming support emails for client '{client_id}'.
        The customer's email is '{customer_email}'.
        
        Follow these instructions:
        1. Search the customer knowledge base (RAG) using 'query_rag_knowledge_tool' with the user query to find relevant policies, context, or responses.
        2. If the user mentions a docket, ticket, or order status (like ORD12345 or T-xxxx), query the status using 'get_order_status_tool'.
        3. If you can solve the query completely and with high confidence using RAG or status details, generate a highly professional email reply. 
           - Call 'send_email_tool' to deliver the final response to '{customer_email}'.
           - Let the user know the email has been sent successfully.
        4. If you DO NOT have enough information, or the issue is an escalation or complaint, generate a support ticket:
           - Call 'create_support_ticket_tool' to record it in the MySQL database.
           - Then generate a ticket acknowledgment response and deliver it using 'send_email_tool'.
        
        Always keep replies concise, professional, and do not hallucinate details.
        """

        # Create a native LangGraph ReAct agent preloaded with the MCP tools
        agent = create_react_agent(model, tools=mcp_tools, state_modifier=system_instruction)

        logger.info(f"🚀 Invoking LangGraph agent for customer: {customer_email}")
        
        user_input = {
            "messages": [
                ("user", f"Process this incoming email:\nSender: {customer_email}\nBody: {email_body}")
            ]
        }

        # Run the agent execution loop
        result = await agent.ainvoke(user_input)
        
        # Log final messages exchange
        final_messages = result.get("messages", [])
        if final_messages:
            logger.info(f"🤖 Agent Execution Complete. Final Output: {final_messages[-1].content}")
            return {
                "status": "success",
                "final_reply": final_messages[-1].content,
                "execution_steps": len(final_messages)
            }
        
        return {"status": "error", "message": "No response returned from agent execution loop."}

if __name__ == "__main__":
    # Test stub to demonstrate standalone execution
    test_client = "CLI-DEMO123"
    test_email = "monish.customer@gmail.com"
    test_body = "Hi team, what is the status of my order ORD88273? I ordered it 3 days ago. Thanks."
    
    print("\n--- Starting LangGraph MCP Agent Standalone Demo ---\n")
    try:
        response = asyncio.run(run_mcp_agent(test_client, test_email, test_body))
        print("\nDemo Output:")
        print(response)
    except Exception as exc:
        print(f"Demo failed: {exc}")
