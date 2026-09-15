import logging
from app.request_handler import get_order_status

logger = logging.getLogger(__name__)


def get_order_by_id(client_id: str, order_id: str):
    """
    Fetch order details by order ID.
    
    Args:
        client_id: The client whose CRM payload config to use
        order_id: The order/docket number to fetch
        
    Returns:
        Order data dict if found, None if not found or error occurs
    """
    try:
        logger.info(f"🔍 [Client {client_id}] Fetching order details for order_id: {order_id}")
        
        # 1. First attempt dynamic connector lookup
        from app.connector_config import run_order_status_lookup
        dyn_res = run_order_status_lookup(client_id=client_id, order_id=order_id)
        if dyn_res.get("success"):
            order_data = dyn_res.get("data")
            logger.info(f"✅ Order found via dynamic connector: {order_id}")
            return order_data

        # 2. Fall back to legacy get_order_status (payload_get_ticket)
        response = get_order_status(client_id, order_id)
        
        # Check if API call was successful
        if response.get("success"):
            order_data = response.get("data")
            logger.info(f"✅ Order found via legacy connector: {order_id}")
            return order_data
        else:
            error = response.get("error", "Unknown error")
            logger.warning(f"⚠️ Order not found or API error: {error}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Failed to fetch order {order_id}: {e}")
        return None
