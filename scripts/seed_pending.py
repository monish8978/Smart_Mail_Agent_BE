import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv
load_dotenv()
from app.connector_config import insert_connector_config_checked

payload = {
    "http_method": "GET",
    "url": "https://example-crm.com/api/status",
    "headers_template": None,
    "request_template": None,
    "response_mapping": None,
    "auth_type": "bearer",
    "auth_secret_encrypted": "dummy",
    "auth_field_name": None,
    "created_by": "test_admin"
}

row_id = insert_connector_config_checked(
    client_id="TEST-CLIENT-2",
    trigger_type="order_status",
    new_status="pending_approval",
    payload=payload,
    cap=10
)
print(f"Seeded pending row id={row_id}")