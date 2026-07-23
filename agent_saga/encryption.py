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

import hashlib
import json
import logging
import os
import time
from typing import Any, Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger("agent_saga.encryption")

_PREFIX = "E1:"


def _key_fingerprint(key: str | bytes) -> str:
    """A short, non-reversible fingerprint of a key, safe to log/persist. Never
    log or store the key material itself."""
    raw = key.encode() if isinstance(key, str) else key
    return hashlib.sha256(raw).hexdigest()[:12]


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
    + HMAC). Supports single key or key rotation via MultiFernet (comma-separated or list of keys)."""

    def __init__(self, key: str | bytes | list[str | bytes]):
        try:
            from cryptography.fernet import Fernet, MultiFernet
        except ImportError as exc:  # pragma: no cover - exercised via the extra
            raise ImportError(
                "WAL encryption needs the 'cryptography' package. Install it with "
                "`pip install agent-saga[encryption]`."
            ) from exc

        if isinstance(key, str) and "," in key:
            keys: list[str | bytes] = [k.strip() for k in key.split(",") if k.strip()]
        elif isinstance(key, (list, tuple)):
            keys = list(key)
        else:
            keys = [key]

        fernets = [Fernet(k) for k in keys]
        self._fernet = MultiFernet(fernets) if len(fernets) > 1 else fernets[0]

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, token: bytes) -> bytes:
        return self._fernet.decrypt(token)


class KeyRingEncryptor(FernetEncryptor):
    """Explicit Key Ring for key rotation.

    Uses `primary_key` to encrypt new WAL records, and falls back to
    `fallback_keys` (old keys) when decrypting historical records.

    Rotation can be automatic. With ``rotation_interval_days`` set, the next
    ``encrypt`` after the interval elapses mints a new primary from
    ``key_factory`` (default: a fresh Fernet key), demotes the old primary to the
    front of ``fallback_keys`` so historical records stay readable, and records a
    rotation event (key *fingerprints* only -- never the key material). Pass an
    ``on_rotate`` callback to persist that event (e.g. append it to a WAL); doing
    it via a callback rather than writing the WAL from inside ``encrypt`` avoids
    re-entering the very write path that called us.
    """

    def __init__(
        self,
        primary_key: str | bytes,
        fallback_keys: Optional[list[str | bytes]] = None,
        *,
        rotation_interval_days: Optional[float] = None,
        key_factory: Optional[Callable[[], str | bytes]] = None,
        on_rotate: Optional[Callable[[dict], None]] = None,
        max_fallback_keys: Optional[int] = None,
        _clock: Callable[[], float] = time.time,
    ):
        keys: list[str | bytes] = [primary_key]
        if fallback_keys:
            keys.extend(fallback_keys)
        super().__init__(key=keys)
        self.primary_key = primary_key
        self.fallback_keys = list(fallback_keys or [])
        self.rotation_interval_days = rotation_interval_days
        self.key_factory = key_factory or generate_key
        self.on_rotate = on_rotate
        self.max_fallback_keys = max_fallback_keys
        self._clock = _clock
        self._last_rotation = _clock()
        self.rotation_events: list[dict] = []

    def _rebuild(self) -> None:
        from cryptography.fernet import Fernet, MultiFernet
        keys = [self.primary_key, *self.fallback_keys]
        fernets = [Fernet(k) for k in keys]
        self._fernet = MultiFernet(fernets) if len(fernets) > 1 else fernets[0]

    def rotation_due(self) -> bool:
        if not self.rotation_interval_days:
            return False
        return (self._clock() - self._last_rotation) >= self.rotation_interval_days * 86400.0

    def rotate(self, new_key: Optional[str | bytes] = None) -> str | bytes:
        """Promote a new primary key, demote the current one to fallback. Returns
        the new primary. Call directly to rotate on demand, or let ``encrypt``
        trigger it once ``rotation_interval_days`` elapses."""
        new_key = new_key or self.key_factory()
        old_primary = self.primary_key
        self.fallback_keys = [old_primary, *self.fallback_keys]
        if self.max_fallback_keys is not None:
            self.fallback_keys = self.fallback_keys[: self.max_fallback_keys]
        self.primary_key = new_key
        self._last_rotation = self._clock()
        self._rebuild()

        event = {
            "event": "KEY_ROTATED",
            "ts": self._last_rotation,
            "new_primary_fingerprint": _key_fingerprint(new_key),
            "demoted_fingerprint": _key_fingerprint(old_primary),
            "fallback_count": len(self.fallback_keys),
        }
        self.rotation_events.append(event)
        logger.info("KeyRingEncryptor rotated primary key -> %s (was %s)",
                    event["new_primary_fingerprint"], event["demoted_fingerprint"])
        if self.on_rotate is not None:
            try:
                self.on_rotate(event)
            except Exception:
                logger.exception("KeyRingEncryptor on_rotate callback failed")
        return new_key

    def maybe_rotate(self) -> bool:
        if self.rotation_due():
            self.rotate()
            return True
        return False

    def encrypt(self, plaintext: bytes) -> bytes:
        self.maybe_rotate()   # lazy, time-based auto-rotation
        return super().encrypt(plaintext)


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
    from . import serialization
    js = serialization.dumps(record)
    if encryptor is None:
        return js
    return _PREFIX + encryptor.encrypt(js.encode("utf-8")).decode("ascii")


def decode_line(line: str, encryptor: Optional[WALEncryptor]) -> dict:
    """Parse one WAL line to a record. Raises EncryptedRecordError for an
    encrypted line with no key, ValueError/JSONDecodeError for corruption."""
    from . import serialization
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
        return serialization.loads(plaintext.decode("utf-8"))
    return serialization.loads(line)


def is_encrypted_line(line: str) -> bool:
    return line.lstrip().startswith(_PREFIX)


__all__ = [
    "WALEncryptor",
    "FernetEncryptor",
    "KeyRingEncryptor",
    "EncryptedRecordError",
    "generate_key",
    "set_wal_encryptor",
    "get_wal_encryptor",
    "encode_line",
    "decode_line",
    "is_encrypted_line",
]
