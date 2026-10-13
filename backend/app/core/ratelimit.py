from __future__ import annotations

import math
import time

from limits import parse
from slowapi import Limiter

from app.api.errors import APIError


class RequestLimits:
    def __init__(self):
        self.limiter = Limiter(key_func=lambda request: request.state.identity.bucket_hash,
                               storage_uri="memory://", strategy="fixed-window")
        self.buckets = {"read": parse("600/minute"), "mutation": parse("20/minute"), "admin": parse("10/minute")}

    def check(self, request) -> None:
        path = request.url.path
        if not path.startswith("/api/") or request.method == "OPTIONS":
            return
        if path.startswith("/api/admin/") or path == "/api/admin":
            scope = "admin"
        elif request.method == "GET":
            scope = "read"
        elif request.method == "POST" and (path == "/api/debates" or path.startswith("/api/debates/")):
            scope = "mutation"
        else:
            return
        limit = self.buckets[scope]
        key = request.state.identity.bucket_hash
        if not self.limiter.limiter.hit(limit, key, scope):
            stats = self.limiter.limiter.get_window_stats(limit, key, scope)
            raise APIError(429, "request_rate_limit", "Too many requests. Try again shortly.",
                           retry_after=max(1, math.ceil(stats.reset_time - time.time())))
