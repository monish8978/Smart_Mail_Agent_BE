import logging
import os
import sys
from typing import Any, Dict

# Ensure project root is in path for relative imports if run as a script
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP

# Core platform service imports
from app.email_credential import get_email_account, create_email_record_db
from app.request_handler import get_order_status
from app.rag import query_knowledge, add_knowledge
from app.mailer import send_email

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mcp_server")

# Initialize MCP Server
mcp = FastMCP("MailAIAutomationServer")

@mcp.tool()
def get_email_account_tool(client_id: str) -> Dict[str, Any]:
    """
    Retrieve registered IMAP email and credentials setup details for a given client.
    
    Args:
        client_id: The unique identifier of the client.
    """
    logger.info(f"MCP Tool call: get_email_account for client_id={client_id}")
    try:
        account = get_email_account(client_id)
        if not account:
            return {"status": "error", "message": f"No email account registered for client: {client_id}"}
        return {"status": "success", "account": account}
    except Exception as e:
        logger.error(f"Error in get_email_account MCP tool: {e}")
        return {"status": "error", "message": str(e)}


@mcp.tool()
def get_order_status_tool(client_id: str, order_id: str) -> Dict[str, Any]:
    """
    Query the external ticket or order delivery system status for a client.
    
    Args:
        client_id: The client identifier.
        order_id: The docket or ticket ID (e.g. ORD12345 or support ID).
    """
    logger.info(f"MCP Tool call: get_order_status for client_id={client_id}, order_id={order_id}")
    try:
        res = get_order_status(client_id, order_id)
        return res
    except Exception as e:
        logger.error(f"Error in get_order_status MCP tool: {e}")
        return {"status": "error", "message": str(e)}


@mcp.tool()
def create_support_ticket_tool(
    client_id: str, 
    mail_id: str, 
    subject: str, 
    body: str, 
    status: str = "Ticket_Generated"
) -> Dict[str, Any]:
    """
    Insert a ticket record into the system database for manual handling or escalations.
    
    Args:
        client_id: The client identifier.
        mail_id: Email address of the customer reporting the issue.
        subject: Email subject line.
        body: Message body content.
        status: Ticket status. Must be 'Ticket_Generated' or 'Done_Replied'.
    """
    logger.info(f"MCP Tool call: create_support_ticket for client={client_id}, sender={mail_id}")
    try:
        # Construct standard structure
        payload = {
            "client_id": client_id,
            "mail_id": mail_id,
            "subject": subject,
            "body": body,
            "status": status
        }
        res = create_email_record_db(payload)
        return res
    except Exception as e:
        logger.error(f"Error in create_support_ticket MCP tool: {e}")
        return {"status": "error", "message": str(e)}


@mcp.tool()
def query_rag_knowledge_tool(client_id: str, query: str) -> Dict[str, Any]:
    """
    Perform a semantic context lookup in the user-isolated ChromaDB/JSON knowledge database.
    Useful to fetch specific answers regarding order policies, company info, or FAQs.
    
    Args:
        client_id: The client identifier.
        query: Semantic query text or email body content to search for context.
    """
    logger.info(f"MCP Tool call: query_rag_knowledge for client_id={client_id}, query={query[:50]}")
    try:
        context = query_knowledge(client_id, query)
        return {"status": "success", "context": context if context else "No matching context found."}
    except Exception as e:
        logger.error(f"Error in query_rag_knowledge MCP tool: {e}")
        return {"status": "error", "message": str(e)}


@mcp.tool()
def add_rag_knowledge_tool(client_id: str, title: str, content: str) -> Dict[str, Any]:
    """
    Upload and index a new knowledge document or FAQ item into the vector store database.
    
    Args:
        client_id: The client identifier.
        title: Title of the document or filename.
        content: Clean text content to segment and embed.
    """
    logger.info(f"MCP Tool call: add_rag_knowledge for client={client_id}, title={title}")
    try:
        doc_id = add_knowledge(client_id, title, content)
        return {"status": "success", "document_id": doc_id, "message": f"Successfully indexed '{title}'"}
    except Exception as e:
        logger.error(f"Error in add_rag_knowledge MCP tool: {e}")
        return {"status": "error", "message": str(e)}


@mcp.tool()
def send_email_tool(client_id: str, to_email: str, subject: str, body: str) -> Dict[str, Any]:
    """
    Deliver a professional SMTP email response directly to a customer.
    
    Args:
        client_id: The client credentials to use for sending.
        to_email: Target customer email address.
        subject: Email subject.
        body: Text message body.
    """
    logger.info(f"MCP Tool call: send_email to={to_email} via client={client_id}")
    try:
        success = send_email(client_id, to_email, subject, body)
        if success:
            return {"status": "success", "message": f"Email successfully delivered to {to_email}"}
        return {"status": "error", "message": "SMTP transmission failed"}
    except Exception as e:
        logger.error(f"Error in send_email MCP tool: {e}")
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    # When executed directly, run standard stdio transport for local agent connections
    logger.info("🚀 Starting Model Context Protocol (MCP) server...")
    mcp.run(transport="stdio")
