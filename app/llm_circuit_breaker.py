import time
import logging
from threading import Lock

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Per-provider circuit breaker for LLM API calls."""

    def __init__(self, failure_threshold=5, recovery_timeout=60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = {}     # provider -> count
        self._last_failure = {} # provider -> timestamp
        self._tripped = {}      # provider -> bool
        self._lock = Lock()

    def record_success(self, provider: str):
        with self._lock:
            self._failures[provider] = 0
            self._tripped[provider] = False

    def record_failure(self, provider: str):
        with self._lock:
            self._failures[provider] = self._failures.get(provider, 0) + 1
            self._last_failure[provider] = time.time()
            if self._failures[provider] >= self.failure_threshold:
                self._tripped[provider] = True
                logger.warning(f"🔌 Circuit breaker TRIPPED for provider '{provider}' after {self._failures[provider]} failures")

    def is_open(self, provider: str) -> bool:
        with self._lock:
            if not self._tripped.get(provider):
                return False
            # Check if recovery timeout has passed
            elapsed = time.time() - self._last_failure.get(provider, 0)
            if elapsed >= self.recovery_timeout:
                self._tripped[provider] = False
                self._failures[provider] = 0
                logger.info(f"🔌 Circuit breaker RESET for provider '{provider}' after {self.recovery_timeout}s cooldown")
                return False
            return True


llm_circuit_breaker = CircuitBreaker()
