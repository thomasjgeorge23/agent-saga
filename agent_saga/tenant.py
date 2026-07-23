"""Multi-Tenant Saga Isolation & Context Scoping.

Provides TenantContext contextvars to namespace WALs, LimitStores, ApprovalStores,
and SnapshotStores cleanly across multi-tenant SaaS agent environments.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional

_current_tenant: contextvars.ContextVar[Optional[TenantContext]] = contextvars.ContextVar(
    "agent_saga_tenant", default=None
)


@dataclass
class TenantContext:
    tenant_id: str
    organization_id: Optional[str] = None
    environment: str = "production"

    def scope_key(self, key: str) -> str:
        return f"tenant:{self.tenant_id}:{key}"

    @property
    def prefix(self) -> str:
        """The namespace every scoped store key is prefixed with for this tenant."""
        return f"tenant:{self.tenant_id}:"

    def apply(self, *, stores: Optional[list] = None) -> "_TenantScope":
        """Namespace every active store for this tenant in one call.

        Returns a context manager that, for the duration of the block:

          * binds this tenant so ``get_current_tenant()`` returns it, and
          * prefixes the ``key_prefix`` of each active shared-backend store
            (LimitStore, ApprovalStore, SnapshotStore, SwitchStore, semantic
            locks) with this tenant's namespace, so two tenants sharing one Redis
            never read or write each other's keys.

        On exit every prefix and the tenant binding are restored, so nested or
        sequential tenants do not bleed into one another::

            with TenantContext("acme").apply():
                await run_saga()      # all stores scoped to tenant:acme:

        Pass ``stores=[...]`` to scope an explicit set instead of auto-discovery.
        """
        return _TenantScope(self, stores)


def get_current_tenant() -> Optional[TenantContext]:
    return _current_tenant.get()


def set_current_tenant(tenant: Optional[TenantContext]) -> contextvars.Token:
    return _current_tenant.set(tenant)


def _discover_stores() -> list:
    """The active stores that might carry a key_prefix worth scoping. Best-effort:
    a backend that is not configured (or whose getter raises) is simply skipped."""
    found = []
    getters = []
    try:
        from .limits import get_limit_store
        getters.append(get_limit_store)
    except Exception:
        pass
    try:
        from .approvals import get_approval_store
        getters.append(get_approval_store)
    except Exception:
        pass
    try:
        from .durable import get_snapshot_store
        getters.append(get_snapshot_store)
    except Exception:
        pass
    try:
        from .locks import get_semantic_locks
        getters.append(get_semantic_locks)
    except Exception:
        pass
    try:
        from .killswitch import get_kill_switch
        getters.append(get_kill_switch)
    except Exception:
        pass
    for getter in getters:
        try:
            store = getter()
        except Exception:
            store = None
        if store is not None:
            found.append(store)
    return found


class _TenantScope:
    """Context manager returned by ``TenantContext.apply()``."""

    def __init__(self, tenant: TenantContext, stores: Optional[list]):
        self.tenant = tenant
        self._explicit = stores
        self._restore: list[tuple] = []   # (store, original_prefix)
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> TenantContext:
        self._token = set_current_tenant(self.tenant)
        prefix = self.tenant.prefix
        stores = self._explicit if self._explicit is not None else _discover_stores()
        for store in stores:
            old = getattr(store, "key_prefix", None)
            if isinstance(old, str) and not old.startswith(prefix):
                store.key_prefix = prefix + old
                self._restore.append((store, old))
        return self.tenant

    def __exit__(self, *exc) -> None:
        for store, old in self._restore:
            store.key_prefix = old
        self._restore.clear()
        if self._token is not None:
            _current_tenant.reset(self._token)
            self._token = None


__all__ = ["TenantContext", "get_current_tenant", "set_current_tenant"]
