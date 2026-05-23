# For backwards compatibility, we make aioredis backend available at the top level of the package if it's installed
try:
    from traffik.backends.redis.aioredis import RedisBackend  # noqa: F401
except ImportError:
    pass
