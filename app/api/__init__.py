from app.api.auth import router as auth_router
from app.api.emails import router as emails_router
from app.api.drafts import router as drafts_router
from app.api.knowledge import router as knowledge_router
from app.api.connectors import router as connectors_router
from app.api.analytics import router as analytics_router
from app.api.settings import router as settings_router

__all__ = [
    "auth_router",
    "emails_router",
    "drafts_router",
    "knowledge_router",
    "connectors_router",
    "analytics_router",
    "settings_router",
]
