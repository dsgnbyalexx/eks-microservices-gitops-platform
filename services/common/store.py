import os
from functools import cache

import redis


@cache
def redis_from_env() -> redis.Redis:
    # Short timeouts: a hung Redis should fail requests quickly, not freeze them.
    return redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=2,
    )
