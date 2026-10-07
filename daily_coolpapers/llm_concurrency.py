"""Shared, in-process provider admission limits; no credentials are persisted."""
from contextlib import contextmanager
import hashlib
import json
import logging
from threading import Condition, Lock
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
DEFAULT_CONCURRENCY = 10


class ProviderLimiter:
    def __init__(self, configured_limit=DEFAULT_CONCURRENCY):
        self.configured_limit = max(1, int(configured_limit))
        self.limit = self.configured_limit
        self.active = 0
        self.waiting = 0
        self.generation = 0
        self.condition = Condition()

    def configure(self, limit):
        with self.condition:
            limit = max(1, int(limit))
            if limit != self.configured_limit:
                self.configured_limit = self.limit = limit
                self.generation += 1
                self.condition.notify_all()

    @contextmanager
    def slot(self):
        with self.condition:
            self.waiting += 1
            try:
                self.condition.wait_for(lambda: self.active < self.limit)
                self.active += 1
                ticket = self.generation
            finally:
                self.waiting -= 1
        try:
            yield ticket
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def throttled(self, ticket):
        """Old in-flight errors share one reduction, then retry under the new cap."""
        with self.condition:
            if ticket != self.generation:
                return True
            if self.limit == 1:
                return False
            previous = self.limit
            self.limit = 4 if previous > 4 else previous - 1
            self.generation += 1
            self.condition.notify_all()
            logger.warning('Provider concurrency limited; reducing concurrency %s -> %s', previous, self.limit)
            return True

    def reset_if_idle(self):
        with self.condition:
            if not self.active and not self.waiting:
                self.limit = self.configured_limit
                self.generation += 1


_registry = {}
_registry_lock = Lock()


def provider_limiter(provider, url, headers, configured_limit=DEFAULT_CONCURRENCY, *, client=None):
    endpoint = urlsplit(url)
    auth = {key.lower(): str(value) for key, value in headers.items()
            if key.lower() in {'authorization', 'x-api-key', 'api-key'} }
    fingerprint = hashlib.sha256(json.dumps(auth, sort_keys=True).encode()).hexdigest()
    key = (provider, endpoint.scheme.lower(), endpoint.netloc.lower(), fingerprint)
    with _registry_lock:
        bindings = getattr(client, '_dcp_provider_limiters', None)
        if isinstance(bindings, dict) and key in bindings:
            return bindings[key]
        limiter = _registry.get(key)
        if limiter is None:
            limiter = _registry[key] = ProviderLimiter(configured_limit)
        limiter.configure(configured_limit)
        if client is not None:
            if not isinstance(bindings, dict):
                bindings = {}
            bindings[key] = limiter
            try:
                client._dcp_provider_limiters = bindings
            except (AttributeError, TypeError):
                pass  # Unusual injected clients still use the shared registry.
        return limiter


def reset_idle_provider_limits():
    """A new queued job starts at its configured cap; stages within it share reductions."""
    with _registry_lock:
        for limiter in _registry.values():
            limiter.reset_if_idle()


def is_concurrency_limit(response):
    if int(getattr(response, 'status_code', 0)) not in {429, 503}:
        return False
    try:
        body = json.dumps(response.json(), ensure_ascii=False).lower()
    except Exception:
        body = str(getattr(response, 'text', '') or '').lower()
    headers = getattr(response, 'headers', {})
    resource = str(headers.get('x-ratelimit-resource', '')).lower()
    return any(marker in body or marker in resource for marker in
               ('concurrent', 'concurrency', 'parallel requests', 'parallel_requests', '并发'))
