import asyncio
import functools

# Load the shipped pytest plugin from the working tree (installed users get it
# automatically via the pytest11 entry point in pyproject.toml).
pytest_plugins = ("agent_saga.pytest_plugin",)


def aio(fn):
    """Run an async test without requiring pytest-asyncio. Keeps the test suite
    dependency-free so `git clone && pytest` works for a drive-by contributor."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper
