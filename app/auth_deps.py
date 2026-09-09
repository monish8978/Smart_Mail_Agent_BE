from fastapi import Header, HTTPException
from app.auth import get_session

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