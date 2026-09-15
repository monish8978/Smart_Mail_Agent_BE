import os
import hmac
from typing import Optional
from fastapi import Header, HTTPException
from app.auth import get_session

INGESTION_API_KEY_ENV = "INGESTION_API_KEY"

def get_current_user(authorization: str = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization[7:]
    user = get_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user

def require_admin(user: dict = None):
    from fastapi import Depends
    def _check(user: dict = Depends(get_current_user)):
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
        return user
    return _check

def require_client_access(client_id: str, user: dict = None):
    """
    Use as: Depends(lambda user=Depends(get_current_user): verify_client_access(client_id, user))
    Simpler: call verify_client_access(client_id, user) inside the route body.
    """
    if user.get("role") == "admin":
        return user
    if user.get("client_id") != client_id:
        raise HTTPException(status_code=403, detail="Not authorized for this client_id")
    return user


def _get_ingestion_api_key() -> str:
    key = os.getenv(INGESTION_API_KEY_ENV)
    if key:
        return key.strip()
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{INGESTION_API_KEY_ENV}="):
                        val = line.split("=", 1)[1].strip()
                        os.environ[INGESTION_API_KEY_ENV] = val
                        return val
        except Exception:
            pass
    return "mail_ai_ingest_secret_token_dev"


def verify_ingestion_auth(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_webhook_secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    """
    Secures the email ingestion endpoint (/process-email).
    Accepts:
      1. API Key via X-API-Key or X-Webhook-Secret matching INGESTION_API_KEY.
      2. Valid Bearer JWT token from an authenticated user.
      3. Valid Bearer token matching INGESTION_API_KEY directly.
    Rejects with 401 Unauthorized if neither is valid.
    """
    configured_key = _get_ingestion_api_key()

    # Check header API key / secret
    provided_key = x_api_key or x_webhook_secret
    if configured_key and provided_key:
        if hmac.compare_digest(provided_key.strip(), configured_key.strip()):
            return {"type": "api_key"}

    # Check Authorization header
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        # Direct API key match in Bearer
        if configured_key and hmac.compare_digest(token, configured_key.strip()):
            return {"type": "api_key"}
        
        # User session JWT check
        user = get_session(token)
        if user:
            return {"type": "session", "user": user}

    raise HTTPException(
        status_code=401,
        detail="Unauthorized: Valid X-API-Key, X-Webhook-Secret, or Bearer Authorization token required"
    )