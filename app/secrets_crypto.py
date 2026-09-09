# app/secrets_crypto.py
import os
import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_ENCRYPTION_KEY_ENV = "CONNECTOR_SECRET_ENCRYPTION_KEY"


def _get_fernet() -> Fernet:
    key = os.getenv(_ENCRYPTION_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{_ENCRYPTION_KEY_ENV} is not set — cannot encrypt/decrypt connector "
            f"auth secrets. Generate one with: python -c "
            f"'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    """Returns a Fernet token (str) suitable for storing in auth_secret_encrypted."""
    if plaintext is None:
        raise ValueError("Cannot encrypt None")
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Raises InvalidToken if the key is wrong or the value was tampered with."""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error("❌ Failed to decrypt auth_secret_encrypted — invalid token or wrong key")
        raise