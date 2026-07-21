"""Credential references.

Compensation kwargs are fsynced to the WAL as plaintext JSON and read by a
separate daemon process. Anything you put in them is written to disk, shipped
to whatever log aggregator tails that file, and included in any support bundle.

So credentials never travel in kwargs. A compensation carries a *reference* --
"stripe_prod" -- and the daemon resolves it against its own secret store at the
moment of use. The WAL records which credential was used, never its value.

This is not defense in depth; it is the difference between a WAL you can hand
to an auditor and one you must treat as a secret-bearing artifact forever.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional

_RESOLVER: Optional[Callable[[str], str]] = None


def set_credential_resolver(fn: Optional[Callable[[str], str]]) -> None:
    """Point the library at Vault, AWS Secrets Manager, or your own store.

        set_credential_resolver(lambda ref: vault.read(f"agents/{ref}"))
    """
    global _RESOLVER
    _RESOLVER = fn


def resolve_credential(ref: str) -> str:
    """Resolve a reference to a live secret. Called in the agent process and,
    after a crash, in the daemon -- both must reach the same store."""
    if _RESOLVER is not None:
        value = _RESOLVER(ref)
        if not value:
            raise CredentialError(f"credential resolver returned nothing for {ref!r}")
        return value

    env = f"AGENT_SAGA_CRED_{re.sub(r'[^A-Z0-9]', '_', ref.upper())}"
    value = os.environ.get(env)
    if not value:
        raise CredentialError(
            f"no credential for {ref!r}. Set {env}, or call "
            f"set_credential_resolver() to use your secret store. The daemon "
            f"must be able to resolve this too, or recovery will escalate."
        )
    return value


class CredentialError(RuntimeError):
    pass


class SecretLeak(ValueError):
    """Raised at authoring time, not after the secret is already on disk."""


# Heuristics for things that must never be written to a WAL. Deliberately
# aggressive: a false positive costs a developer one rename; a false negative
# costs a credential rotation and an incident report.
_PATTERNS = (
    (re.compile(r"^(postgres|postgresql|mysql|mongodb)(\+\w+)?://[^/\s]*:[^@/\s]+@"), "database URI with an embedded password"),
    (re.compile(r"^(sk|rk|pk)_(live|test)_[A-Za-z0-9]{10,}"), "Stripe secret key"),
    (re.compile(r"^ghp_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"^xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"^ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."), "JWT"),
    (re.compile(r"^00D[A-Za-z0-9]{12,}![A-Za-z0-9._-]{20,}"), "Salesforce session token"),
    (re.compile(r"^AKIA[0-9A-Z]{16}$"), "AWS access key id"),
)

_SUSPICIOUS_KEYS = re.compile(
    r"(password|passwd|secret|token|api_?key|auth|credential|conn(ection)?_?(uri|string)|dsn)",
    re.IGNORECASE,
)


def _find_secret_value(value: Any, path: str) -> Optional[tuple[str, str]]:
    """Walk nested dicts and lists; return (path, label) for the first string
    matching a credential pattern, else None.

    Only *values* are matched here, at any depth. The patterns are specific
    (`sk_live_...`, JWTs, DB URIs), so recursing them through a row snapshot or
    Stripe metadata has a low false-positive rate. Key *names* are deliberately
    NOT recursed into these payloads -- a column literally named `token` or
    `auth_provider` is user data, not a leak, and flagging it would break real
    connectors.
    """
    if isinstance(value, str):
        for pattern, label in _PATTERNS:
            if pattern.match(value):
                return path, label
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            hit = _find_secret_value(v, f"{path}.{k}" if path else str(k))
            if hit:
                return hit
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            hit = _find_secret_value(v, f"{path}[{i}]")
            if hit:
                return hit
    return None


def assert_no_secrets(kwargs: dict, *, where: str) -> None:
    """Fail loudly while the developer is still writing the connector.

    Catches a credential-shaped value anywhere in the kwargs -- including nested
    dicts and lists such as a Postgres row snapshot or Stripe metadata, not just
    top-level strings -- plus top-level kwarg *names* that look like credentials
    (those names are connector-authored, so strictness there is safe).
    """
    hit = _find_secret_value(kwargs, "")
    if hit:
        path, label = hit
        loc = f"value at {path!r}" if path else "value"
        raise SecretLeak(
            f"{where}: {loc} looks like a {label}. Compensation kwargs are written "
            f"to the WAL in plaintext, including nested structures. Pass a "
            f"credential reference and resolve it in the handler via "
            f"resolve_credential()."
        )
    for key in kwargs:
        if isinstance(key, str) and _SUSPICIOUS_KEYS.search(key) and not key.endswith(
            ("_ref", "_reference", "_name")
        ):
            raise SecretLeak(
                f"{where}: compensation kwarg {key!r} is named like a credential. "
                f"If it is one, pass a reference instead (e.g. {key}_ref='stripe_prod'). "
                f"If it genuinely is not, rename it."
            )


__all__ = ["set_credential_resolver", "resolve_credential", "assert_no_secrets",
           "CredentialError", "SecretLeak"]
