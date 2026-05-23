# For backwards compatibility, we make aiomcache backend available at the top level of the package if it's installed
try:
    from traffik.backends.memcached.aiomcache import MemcachedBackend  # noqa: F401
except ImportError:
    pass
