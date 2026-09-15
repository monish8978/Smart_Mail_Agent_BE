def _encrypt_key(key: str | None, client_id: str | None = None) -> str | None:
    if not key:
        return key
    if key.startswith("gAAAAA"):
        return key
    from app.secrets_crypto import encrypt_secret
    return encrypt_secret(key, client_id=client_id)


def _decrypt_key(key: str | None, client_id: str | None = None) -> str:
    if not key:
        return ""
    if key.startswith("gAAAAA"):
        from app.secrets_crypto import decrypt_secret
        try:
            return decrypt_secret(key, client_id=client_id)
        except Exception:
            return key
    return key
