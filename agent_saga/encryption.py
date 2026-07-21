"""WAL-at-rest encryption (Bring-Your-Own-Key).

The WAL holds real business data -- row snapshots, API arguments, compensation
payloads. On a shared or regulated host that plaintext is a liability. This
encrypts each record at rest with a symmetric key the developer supplies.

Design constraints, straight from the product line:

  * BYOK, no KMS in the open-source layer. The key comes from the developer --
    an env var (`AGENT_SAGA_WAL_KEY`) or an explicit encryptor on the context.
    Vault/KMS resolution is a paid Enterprise concern and deliberately absent here.

  * Zero-dependency baseline preserved. `cryptography` is imported lazily and
    only when a key is actually configured. Install it with the extra:
    `pip install agent-saga[encryption]`. No key set -> nothing imported, WAL
    stays plaintext, exactly as before.

  * Fail loud, never silent. A reader (or the recovery daemon) that meets an
    encrypted record without a key must error, not skip it. A daemon that
    silently treats an unreadable WAL as "no work to do" would leave every
    crashed saga unrecovered -- the worst possible failure for this component.

Format: each line is either raw JSON (plaintext) or `E1:<fernet-token>`. The
per-line prefix lets a reader auto-detect, tolerate a WAL that was plaintext
before a key was introduced, and give a precise error on an encrypted line it
cannot read.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional, Protocol, runtime_checkable

_PREFIX = "E1:"


@runtime_checkable
class WALEncryptor(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, token: bytes) -> bytes: ...


class EncryptedRecordError(RuntimeError):
    """An encrypted WAL record was met without a key to read it. Distinct from a
    corrupt/truncated line, because the response is different: this is a
    configuration error to surface, not garbage to skip."""


class FernetEncryptor:
    """Authenticated symmetric encryption via `cryptography`'s Fernet (AES-128-CBC
    + HMAC). Lazy import so the dependency is only needed when encryption is on."""

    def __init__(self, key: str | bytes):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - exercised via the extra
            raise ImportError(
                "WAL encryption needs the 'cryptography' package. Install it with "
                "`pip install agent-saga[encryption]`."
            ) from exc
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        return self._fernet.decrypt(token)


def generate_key() -> str:
    """A fresh Fernet key, urlsafe-base64. Store it in your secret manager and
    hand it back via AGENT_SAGA_WAL_KEY or set_wal_encryptor -- losing it means
    losing the ability to read or recover that WAL."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


_ENCRYPTOR: Optional[WALEncryptor] = None


def set_wal_encryptor(encryptor: Optional[WALEncryptor]) -> None:
    """Set the process-wide WAL encryptor. Pass None to clear and fall back to
    the AGENT_SAGA_WAL_KEY environment variable."""
    global _ENCRYPTOR
    _ENCRYPTOR = encryptor


def get_wal_encryptor() -> Optional[WALEncryptor]:
    """Resolve the encryptor: an explicit one wins, else a key in the environment,
    else None (plaintext). The daemon and the agent must resolve the *same* key,
    exactly as they must resolve the same credentials and snapshot store."""
    if _ENCRYPTOR is not None:
        return _ENCRYPTOR
    key = os.environ.get("AGENT_SAGA_WAL_KEY")
    if key:
        return FernetEncryptor(key)
    return None


# ---------------------------------------------------------------------------
# Line codec -- the single place both the writer and every reader agree on
# ---------------------------------------------------------------------------

def encode_line(record: dict, encryptor: Optional[WALEncryptor]) -> str:
    js = json.dumps(record, default=str)
    if encryptor is None:
        return js
    return _PREFIX + encryptor.encrypt(js.encode("utf-8")).decode("ascii")


def decode_line(line: str, encryptor: Optional[WALEncryptor]) -> dict:
    """Parse one WAL line to a record. Raises EncryptedRecordError for an
    encrypted line with no key, ValueError/JSONDecodeError for corruption."""
    line = line.strip()
    if line.startswith(_PREFIX):
        if encryptor is None:
            raise EncryptedRecordError(
                "WAL line is encrypted but no key is configured. Set "
                "AGENT_SAGA_WAL_KEY (or call set_wal_encryptor) with the same key "
                "the writer used."
            )
        try:
            plaintext = encryptor.decrypt(line[len(_PREFIX):].encode("ascii"))
        except EncryptedRecordError:
            raise
        except Exception as exc:
            # A bad token is a truncated (or tampered) ciphertext line. Normalize
            # to ValueError so truncation-tolerant readers skip and *count* it,
            # rather than dying on an encryptor-specific exception type. The
            # corrupt-line counter surfaces it either way.
            raise ValueError(f"undecryptable WAL line: {exc}") from exc
        return json.loads(plaintext)
    return json.loads(line)


def is_encrypted_line(line: str) -> bool:
    return line.lstrip().startswith(_PREFIX)


__all__ = [
    "WALEncryptor",
    "FernetEncryptor",
    "EncryptedRecordError",
    "generate_key",
    "set_wal_encryptor",
    "get_wal_encryptor",
    "encode_line",
    "decode_line",
    "is_encrypted_line",
]
