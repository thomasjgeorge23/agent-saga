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


def get_current_tenant() -> Optional[TenantContext]:
    return _current_tenant.get()


def set_current_tenant(tenant: Optional[TenantContext]) -> contextvars.Token:
    return _current_tenant.set(tenant)


__all__ = ["TenantContext", "get_current_tenant", "set_current_tenant"]
