from fastapi import APIRouter

from app.api.settings.crypto import _encrypt_key, _decrypt_key
from app.api.settings.automation import router as automation_router
from app.api.settings.llm_routes import router as llm_router
from app.api.settings.clients_admin import router as clients_admin_router
from app.api.settings.moderation import router as moderation_router

router = APIRouter(tags=["Settings & Configuration"])

router.include_router(automation_router)
router.include_router(llm_router)
router.include_router(clients_admin_router)
router.include_router(moderation_router)

__all__ = [
    "router",
    "_encrypt_key",
    "_decrypt_key",
    "automation_router",
    "llm_router",
    "clients_admin_router",
    "moderation_router",
]
