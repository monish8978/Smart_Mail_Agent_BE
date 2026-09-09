# worker/seed_pending.py
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