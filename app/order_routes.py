import logging

logger = logging.getLogger(__name__)


def get_order_by_id(client_id: str, order_id: str):
    """
    Fetch order details by order ID using dynamic connector lookup.
    
    Args:
        client_id: The client whose CRM payload config to use
        order_id: The order/docket number to fetch
        
    Returns:
        Order data dict if found, None if not found or error occurs
    """
    try:
        logger.info(f"🔍 [Client {client_id}] Fetching order details for order_id: {order_id}")
        
        from app.connector_config import run_order_status_lookup
        dyn_res = run_order_status_lookup(client_id=client_id, order_id=order_id)
        if dyn_res.get("success"):
            order_data = dyn_res.get("data")
            logger.info(f"✅ Order found via dynamic connector: {order_id}")
            return order_data
        else:
            error = dyn_res.get("error", "Order not found")
            logger.warning(f"⚠️ Order not found or connector error: {error}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Failed to fetch order {order_id}: {e}")
        return None
