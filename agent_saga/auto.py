"""`agent_saga/auto.py` -- Zero-Code Global Auto-Activation Module (`import agent_saga.auto`).

Importing this module anywhere in a Python process automatically activates the
SAGAOPS Omnipresent Reality Shield (`saga.omni.shield()`), instruments global
agent framework hooks (`saga.patch_all()`), and enables crash-safe WAL logging.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

import logging
from .omni import shield
from .compat import patch_all

logger = logging.getLogger("agent_saga.auto")


def _activate_global_auto_saga() -> None:
    """Perform zero-code global auto-activation."""
    try:
        engine = shield()
        patch_all()
        logger.info("🌌 SAGAOPS Global Auto-Activation Enabled")
    except Exception as exc:
        logger.warning("Failed to complete global auto-activation: %s", exc)


# Execute auto-activation upon import
_activate_global_auto_saga()

__all__ = ["patch_all", "shield"]
