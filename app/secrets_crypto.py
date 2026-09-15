# app/secrets_crypto.py
import os
import logging
import base64
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger(__name__)

_ENCRYPTION_KEY_ENV = "CONNECTOR_SECRET_ENCRYPTION_KEY"
_TENANT_SALT = b"mail_ai_tenant_credential_v1"


def _get_master_fernet() -> Fernet:
    key = os.getenv(_ENCRYPTION_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"{_ENCRYPTION_KEY_ENV} is not set — cannot encrypt/decrypt connector "
            f"auth secrets. Generate one with: python -c "
            f"'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def _get_tenant_fernet(client_id: str) -> Fernet:
    """
    Derives a tenant-specific Fernet key from the master encryption key using HKDF.
    Ensures cryptographic isolation: a token encrypted for client_A cannot be decrypted by client_B.
    """
    key = os.getenv(_ENCRYPTION_KEY_ENV)
    if not key:
        raise RuntimeError(f"{_ENCRYPTION_KEY_ENV} is not set")
    
    cid_clean = client_id.strip() if client_id else ""
    if not cid_clean:
        return _get_master_fernet()

    master_bytes = key.encode() if isinstance(key, str) else key
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_TENANT_SALT,
        info=cid_clean.encode("utf-8"),
    )
    derived_raw = hkdf.derive(master_bytes)
    derived_key = base64.urlsafe_b64encode(derived_raw)
    return Fernet(derived_key)


def encrypt_secret(plaintext: str, client_id: str | None = None) -> str:
    """
    Returns a Fernet token (str) suitable for storing in encrypted columns.
    If client_id is provided, encrypts using a tenant-isolated key derived via HKDF.
    Otherwise, encrypts using the global master key.
    """
    if plaintext is None:
        raise ValueError("Cannot encrypt None")
    fernet = _get_tenant_fernet(client_id) if client_id else _get_master_fernet()
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str, client_id: str | None = None) -> str:
    """
    Decrypts token.
    If client_id is provided:
      1. Attempts decryption with the tenant-specific HKDF key.
      2. On InvalidToken, falls back to the legacy master key (providing zero-downtime migration).
    If client_id is None, decrypts with the master key.
    Raises InvalidToken if all decryption attempts fail.
    """
    if token is None:
        return ""
    token_str = token.decode() if isinstance(token, bytes) else str(token)

    if client_id:
        try:
            return _get_tenant_fernet(client_id).decrypt(token_str.encode()).decode()
        except InvalidToken:
            # Fallback to master key for backward compatibility with pre-migration secrets
            logger.debug(f"Falling back to master key for client_id={client_id}")

    try:
        return _get_master_fernet().decrypt(token_str.encode()).decode()
    except InvalidToken:
        logger.error(f"❌ Failed to decrypt secret — invalid token or wrong key (client_id={client_id})")
        raise