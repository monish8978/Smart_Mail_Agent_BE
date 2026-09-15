import logging
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel, EmailStr

from app.auth_deps import get_current_user, require_admin
from app.rate_limiter import RedisRateLimiter
from app.auth import (
    login_user,
    register_admin_by_admin,
    admin_reset_client_password,
    send_reset_otp,
    reset_password_with_otp,
    destroy_session,
    set_user_status,
)
from app.db import get_db_ctx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Authentication & Users"])


class CreateAdminRequest(BaseModel):
    email: EmailStr
    password: str


class AdminResetPasswordRequest(BaseModel):
    client_id: str
    new_password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordWithOtpRequest(BaseModel):
    email: EmailStr
    otp: str
    new_password: str


class ApproveRequest(BaseModel):
    email: EmailStr


class SetUserStatusRequest(BaseModel):
    client_id: str
    status: str  # 'active' or 'inactive'


@router.post("/login", dependencies=[Depends(RedisRateLimiter(limit=5, window=60))])
def login(data: LoginRequest):
    res = login_user(data.email, data.password)
    if not res["success"]:
        raise HTTPException(status_code=401, detail=res["error"])
    return res


@router.post("/logout")
def logout(authorization: str = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        destroy_session(authorization[7:])
    return {"status": "logged_out"}


@router.post("/admin/create-admin")
def create_admin(data: CreateAdminRequest, user: dict = Depends(require_admin())):
    res = register_admin_by_admin(data.email, data.password, user.get("client_id", "ADMIN"))
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/admin/reset-client-password")
def reset_client_password_endpoint(data: AdminResetPasswordRequest, user: dict = Depends(require_admin())):
    res = admin_reset_client_password(data.client_id, data.new_password)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/forgot-password/send-otp", dependencies=[Depends(RedisRateLimiter(limit=5, window=60))])
def send_otp_endpoint(data: ForgotPasswordRequest):
    res = send_reset_otp(data.email)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/forgot-password/reset", dependencies=[Depends(RedisRateLimiter(limit=5, window=60))])
def reset_password_endpoint(data: ResetPasswordWithOtpRequest):
    res = reset_password_with_otp(data.email, data.otp, data.new_password)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/approve-registration")
def approve_registration_endpoint(data: ApproveRequest, user: dict = Depends(require_admin())):
    email = data.email
    try:
        with get_db_ctx() as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT client_id, role, status FROM users WHERE email = %s", (email,))
                db_user = cursor.fetchone()
                if not db_user:
                    raise HTTPException(status_code=404, detail="User not found")

                client_id, role, status = db_user[0], db_user[1], db_user[2]
                if status == 'active':
                    return {"status": "success", "message": "User is already active"}

                cursor.execute("UPDATE users SET status = 'active' WHERE email = %s", (email,))
                db.commit()

                try:
                    cursor.execute("SELECT client_id FROM email_accounts LIMIT 1")
                    row = cursor.fetchone()
                    sender_client_id = row[0] if row else "CLI-7AE811F3"

                    from app.mailer import send_email
                    subject = "Your Mail AI Account is Approved!"
                    body = (
                        f"Hello,\n\n"
                        f"Your registration request has been approved by the admin.\n"
                        f"You can now log in to your account at:\n"
                        f"http://172.16.3.215:1947/login\n\n"
                        f"Best regards,\n"
                        f"Mail AI Team"
                    )
                    send_email(sender_client_id, email, subject, body)
                except Exception as mail_err:
                    logger.error(f"Failed to send confirmation email: {mail_err}")

                return {"status": "success", "message": f"User {email} has been approved successfully."}
    except Exception as e:
        logger.error(f"Approval error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/set-user-status")
def set_user_status_endpoint(data: SetUserStatusRequest, user: dict = Depends(require_admin())):
    res = set_user_status(data.client_id, data.status)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res
