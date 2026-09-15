# app/db.py
import os
import time
import logging
import threading
from contextlib import contextmanager
import pymysql
from dbutils.pooled_db import PooledDB

logger = logging.getLogger(__name__)

_pool = None
_pool_lock = threading.Lock()
_pool_pid = None


def _get_pool():
    global _pool, _pool_pid
    current_pid = os.getpid()

    # If the process was forked (e.g. Celery / gunicorn workers), reset pool for child process
    if _pool_pid is not None and _pool_pid != current_pid:
        _pool = None

    if _pool is None:
        with _pool_lock:
            if _pool is None:
                max_retries = 10
                retry_delay = 2
                for attempt in range(1, max_retries + 1):
                    try:
                        db_port_str = os.getenv("DB_PORT", "3306")
                        try:
                            db_port = int(db_port_str)
                        except (ValueError, TypeError):
                            db_port = 3306

                        _pool = PooledDB(
                            creator=pymysql,
                            maxconnections=20,   # tune based on your MySQL max_connections
                            mincached=2,
                            maxcached=10,
                            blocking=True,       # queue requests instead of crashing
                            ping=7,              # auto-reconnect on stale / dropped connections
                            host=os.getenv("DB_HOST"),
                            port=db_port,
                            user=os.getenv("DB_USER"),
                            password=os.getenv("DB_PASS"),
                            database=os.getenv("DB_NAME"),
                            charset="utf8mb4",
                            connect_timeout=10,
                            autocommit=False,
                        )
                        # Test connection immediately
                        test_conn = _pool.connection()
                        test_conn.close()
                        _pool_pid = current_pid
                        logger.info("✅ Database connection pool initialized successfully")
                        break
                    except Exception as e:
                        _pool = None
                        if attempt == max_retries:
                            logger.critical(f"❌ Failed to connect to database after {max_retries} attempts: {e}")
                            raise
                        logger.warning(f"⚠️ Database not ready (attempt {attempt}/{max_retries}): {e}. Retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
    return _pool


def get_db():
    return _get_pool().connection()


@contextmanager
def get_db_ctx():
    conn = get_db()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_err:
            logger.warning(f"⚠️ Failed to rollback dirty transaction: {rb_err}")
        raise
    finally:
        try:
            conn.close()  # returns to pool, doesn't actually close socket
        except Exception:
            pass
