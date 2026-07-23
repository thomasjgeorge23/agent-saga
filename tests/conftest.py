import asyncio
import functools

# The plugin is registered via entry points in pyproject.toml


def aio(fn):
    """Run an async test without requiring pytest-asyncio. Keeps the test suite
    dependency-free so `git clone && pytest` works for a drive-by contributor."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper
