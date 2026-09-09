# worker/race_approve_test.py
import sys
from dotenv import load_dotenv
load_dotenv()
from app.connector_config import approve_connector_config, CapExceededError, SwapRaceError

LABEL = sys.argv[1]
ROW_ID = int(sys.argv[2])
TRIGGER_TYPE = sys.argv[3]

try:
    approve_connector_config(
        client_id="TEST-CLIENT-2",
        trigger_type=TRIGGER_TYPE,
        new_config_id=ROW_ID,
        approving_admin="admin_" + LABEL,
        cap=1
    )
    print(f"[{LABEL}] SUCCESS")
except CapExceededError as e:
    print(f"[{LABEL}] CAP EXCEEDED: {e}")
except SwapRaceError as e:
    print(f"[{LABEL}] SWAP RACE CAUGHT: {e}")