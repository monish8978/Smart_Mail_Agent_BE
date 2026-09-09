import requests
import json
import logging
import os
import base64
import re

from app.email_credential import get_create_payload_table, get_payload_get_ticket_table
from app.llm import design_payload

logger = logging.getLogger(__name__)


def call_create_ticket(client_id: str, mail_id: str, subject: str, body: str, status: str, personal_details: dict = None) -> dict:
    try:
        db_create_payload_table = get_create_payload_table(client_id)

        if not db_create_payload_table:
            logger.warning(f"⚠️ No payload found for client_id={client_id}")
            return {
                "success": False,
                "ticket_id": None,
                "error": f"No payload config found for client_id={client_id}"
            }

        api_url = db_create_payload_table["url"]
        paylod1 = db_create_payload_table["paylod"]
        paylod1 = design_payload(paylod1, mail_id, subject, body, status, personal_details=personal_details)
        logger.info(f"🧩 Designed payload for client_id={client_id}:\n{json.dumps(paylod1, indent=2)}")

        json_str = json.dumps(paylod1)
        encoded_data = base64.b64encode(json_str.encode()).decode()

        url = f"{api_url}?data={encoded_data}"

        logger.info(f"📤 Calling Create Ticket API: {url[:100]}...")

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        result = response.json()
        logger.info(f"📥 API response: {result}")

        ticket_id = None
        ref_text = result.get("Refrence_No") or result.get("Message", "")
        match = re.search(r"T-\d{6}-\d+", ref_text)
        if match:
            ticket_id = match.group()

        if result.get("Status") == "Success":
            return {
                "success": True,
                "ticket_id": ticket_id,
                "message": result.get("Message")
            }
        else:
            return {
                "success": False,
                "ticket_id": None,
                "error": result.get("Message")
            }

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ API failed: {e}")
        return {
            "success": False,
            "ticket_id": None,
            "error": str(e)
        }


# ==============================
# 📦 Order Status API
# ==============================
def get_order_status(client_id: str, order_id: str) -> dict:
    try:
        db_payload_get_ticket_table = get_payload_get_ticket_table(client_id)

        if not db_payload_get_ticket_table:
            logger.warning(f"⚠️ No payload found for client_id={client_id}")
            return {"success": False, "error": f"No payload config found for client_id={client_id}"}

        url = db_payload_get_ticket_table["url"]

        # FIX: deep-copy before mutating — the cached payload dict is shared
        # across tasks in the same worker process. Writing order_id directly
        # into the original would let concurrent tasks overwrite each other's
        # docket_no, causing wrong-ticket lookups.
        paylod2 = json.loads(json.dumps(db_payload_get_ticket_table["paylod"]))
        paylod2["filter"]["docket_no"] = order_id

        json_str = json.dumps(paylod2)
        encoded_data = base64.b64encode(json_str.encode()).decode()

        full_url = f"{url}?postData={encoded_data}"

        logger.info(f"📤 Calling Order Status API for {order_id}")

        response = requests.get(full_url, timeout=10)
        response.raise_for_status()

        result = response.json()

        if "Success" in result:
            return {
                "success": True,
                "data": result.get("Success")
            }
        else:
            return {
                "success": False,
                "error": result.get("Failure")
            }

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Order API failed: {e}")
        return {
            "success": False,
            "error": str(e)
        }