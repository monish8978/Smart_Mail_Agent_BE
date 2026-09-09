
import logging
from urllib.parse import urlsplit
from app.db import get_db

logger = logging.getLogger(__name__)


def ensure_url_allowlist_table():
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS url_allowlist (
                    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
                    scheme     VARCHAR(20)  NOT NULL,
                    netloc     VARCHAR(255) NOT NULL,
                    path       VARCHAR(255) NOT NULL DEFAULT '',
                    added_by   VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE INDEX uq_scheme_netloc_path (scheme, netloc, path(191))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
        logger.info("✅ url_allowlist table ensured")
    finally:
        conn.close()


def is_url_allowed(url: str) -> bool:
    """
    Exact-match check against url_allowlist, on (scheme, netloc, path)
    only — query string is deliberately excluded, since callers append
    dynamic query params at request time (see request_handler.py's
    existing pattern: url + '?data=...'). Never substring/startswith —
    that's the classic SSRF-allowlist bypass
    ("https://crm.example.com".startswith(...) matches
    "https://crm.example.com.attacker.com").
    """
    parts = urlsplit(url)
    scheme, netloc, path = parts.scheme, parts.netloc, parts.path

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM url_allowlist WHERE scheme=%s AND netloc=%s AND path=%s LIMIT 1",
                (scheme, netloc, path)
            )
            return cursor.fetchone() is not None
    finally:
        conn.close()


def add_url_to_allowlist(url: str, added_by: str) -> int:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"URL must include scheme and host: {url}")

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO url_allowlist (scheme, netloc, path, added_by) VALUES (%s, %s, %s, %s)",
                (parts.scheme, parts.netloc, parts.path, added_by)
            )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()