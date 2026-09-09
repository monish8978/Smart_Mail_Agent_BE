# race_test.py
import sys
from app.connector_config import insert_connector_config_checked, CapExceededError, CreationRaceError

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

try:
    row_id = insert_connector_config_checked(
        client_id="TEST-CLIENT-1",
        trigger_type="order_status",
        new_status="pending_approval",
        payload=payload,
        cap=10
    )
    print(f"[{sys.argv[1]}] SUCCESS row_id={row_id}")
except CapExceededError as e:
    print(f"[{sys.argv[1]}] CAP EXCEEDED: {e}")
except CreationRaceError as e:
    print(f"[{sys.argv[1]}] RACE CAUGHT (expected for one of the two): {e}")