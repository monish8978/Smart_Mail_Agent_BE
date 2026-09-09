import requests
import time
import logging
from threading import Lock

import os

ACCOUNT_API_URL = os.getenv("ACCOUNT_API_URL", "http://mail_ai_api:8024/email-account/")

logger = logging.getLogger(__name__)


class EmailCredentialService:
    def __init__(self, api_url=None, cache_ttl=300):
        self.api_url = api_url
        self.cache_ttl = cache_ttl
        self._cache = {}
        self._last_fetched = {}
        self._lock = Lock()

    def _fetch_from_db(self, client_id):
        """Fetch credentials directly from the database without HTTP overhead."""
        try:
            from app.email_credential import get_email_account
            data = get_email_account(client_id)
            if not data:
                return None, None, 80

            email_user = data.get("email")
            email_pass = data.get("password")
            score_threshold = data.get("score_threshold", 80)
            return email_user, email_pass, score_threshold
        except Exception as e:
            logger.error(f"❌ Failed to fetch credentials from DB for {client_id}: {e}", exc_info=True)
            return None, None, 80

    def get_credentials(self, client_id, force_refresh=False):
        """
        Get credentials with in-memory caching.
        """
        with self._lock:
            current_time = time.time()

            # Cache hit
            last_fetched = self._last_fetched.get(client_id, 0)
            if (
                not force_refresh
                and client_id in self._cache
                and (current_time - last_fetched < self.cache_ttl)
            ):
                return self._cache[client_id]

            creds = self._fetch_from_db(client_id)

            if creds != (None, None, 80):
                self._cache[client_id] = creds
                self._last_fetched[client_id] = current_time
                logger.info(f"✅ Credentials fetched and cached from DB for {client_id}")
            else:
                logger.warning(f"⚠️ No active email credentials found in DB for {client_id}")

            return creds


# Singleton instance
credential_service = EmailCredentialService()


def get_email_credentials(client_id):
    res = credential_service.get_credentials(client_id)
    return res[0], res[1]


def get_email_score_threshold(client_id):
    res = credential_service.get_credentials(client_id)
    return res[2] if len(res) > 2 else 80


def get_mailbox_oauth_token(client_id: str, provider: str, refresh_token: str, oauth_client_id: str, oauth_client_secret: str) -> str | None:
    """
    Exchanges refresh_token for a fresh OAuth 2.0 access token for Google Workspace / Microsoft 365 IMAP/SMTP XOAUTH2.
    """
    if not refresh_token or not oauth_client_id or not oauth_client_secret:
        logger.error(f"❌ Missing OAuth 2.0 refresh parameters for client {client_id}")
        return None

    prov = (provider or "google").lower()
    if "google" in prov or "gmail" in prov:
        token_url = "https://oauth2.googleapis.com/token"
    elif "microsoft" in prov or "azure" in prov or "office" in prov or "outlook" in prov:
        token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    else:
        logger.error(f"❌ Unsupported OAuth provider '{provider}' for client {client_id}")
        return None

    data = {
        "client_id": oauth_client_id,
        "client_secret": oauth_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }

    try:
        res = requests.post(token_url, data=data, timeout=10)
        res.raise_for_status()
        token_data = res.json()
        access_token = token_data.get("access_token")
        logger.info(f"✅ Refreshed XOAUTH2 access token for {client_id} via {provider}")
        return access_token
    except Exception as e:
        logger.error(f"❌ Failed to refresh XOAUTH2 token for {client_id}: {e}")
        return None