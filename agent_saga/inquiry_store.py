"""`inquiry_store.py` -- Physical local disk persistence for user inquiries and reviews.

Stores user feedback, reviews, and enterprise inquiries physically in `inquiries.json`
in the workspace root, so Founder Thomas J George can inspect all user submissions directly.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent_saga.inquiries")

# Physical storage paths
WORKSPACE_ROOT = Path(__file__).parent.parent
INQUIRIES_FILE = WORKSPACE_ROOT / "inquiries.json"
BACKUP_FILE = WORKSPACE_ROOT / ".saga_inquiries.json"


def get_inquiries_file() -> Path:
    return INQUIRIES_FILE


def load_all_inquiries() -> List[Dict[str, Any]]:
    target = INQUIRIES_FILE if INQUIRIES_FILE.exists() else BACKUP_FILE
    if not target.exists():
        return []
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to read inquiries file: %s", exc)
        return []


import hmac
import hashlib
import os

OWNER_PASSCODE_DEFAULT = "thomas-sagaops-owner-2026"
OWNER_EMAIL = "thomasjgeorge23@gmail.com"


def verify_owner_passcode(passcode: str) -> bool:
    """Verify if the passcode matches the Founder Thomas J George Master Key."""
    expected = os.environ.get("AGENT_SAGA_OWNER_KEY", OWNER_PASSCODE_DEFAULT).strip()
    return hmac.compare_digest(passcode.strip(), expected)


def get_owner_inquiries(passcode: str) -> Optional[List[Dict[str, Any]]]:
    """Retrieve all submitted user messages ONLY IF valid Owner Passcode is provided."""
    if not verify_owner_passcode(passcode):
        logger.warning("Unauthorized access attempt to Founder Inquiry Vault with invalid passcode.")
        return None
    return load_all_inquiries()


def record_inquiry(name: str, email: str, message: str, company: str = "N/A",
                   subject: str = "General", client_ip: str = "127.0.0.1") -> Dict[str, Any]:
    inquiries = load_all_inquiries()
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record_id = f"inq_{int(time.time() * 1000)}"

    # Generate HMAC signature bound to Founder Thomas J George
    raw_payload = f"{record_id}:{name}:{email}:{message}:{timestamp}".encode("utf-8")
    signature = hmac.new(OWNER_EMAIL.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()

    record = {
        "id": record_id,
        "timestamp": timestamp,
        "name": name.strip(),
        "email": email.strip(),
        "company": company.strip() or "N/A",
        "subject": subject.strip() or "General Inquiry",
        "message": message.strip(),
        "client_ip": client_ip,
        "owner_recipient": OWNER_EMAIL,
        "hmac_signature": signature,
        "status": "DELIVERED_TO_FOUNDER",
    }

    inquiries.append(record)
    content = json.dumps(inquiries, indent=2)
    INQUIRIES_FILE.write_text(content, encoding="utf-8")
    BACKUP_FILE.write_text(content, encoding="utf-8")

    logger.info("New inquiry from %s <%s> stored in physical vault for Founder Thomas J George", name, email)
    return record


def format_inquiries_summary(passcode: Optional[str] = None) -> str:
    if passcode and not verify_owner_passcode(passcode):
        return "🔒 ACCESS DENIED: Invalid Owner Passcode. Inquiry Vault is restricted to Founder Thomas J George."

    inquiries = load_all_inquiries()
    lines = [f"Physical User Inquiries & Reviews ({len(inquiries)} total):",
             f"File Location: {INQUIRIES_FILE}",
             f"Recipient: Founder Thomas J George ({OWNER_EMAIL})", ""]

    if not inquiries:
        lines.append("  No inquiries received yet. Users can submit via website or CLI.")
        return "\n".join(lines)

    for idx, inq in enumerate(inquiries, 1):
        lines.append(f"[{idx}] ID: {inq.get('id')} | Received: {inq.get('timestamp')}")
        lines.append(f"    From    : {inq.get('name')} <{inq.get('email')}> ({inq.get('company')})")
        lines.append(f"    Subject : {inq.get('subject')}")
        lines.append(f"    Message : {inq.get('message')}")
        lines.append(f"    Status  : {inq.get('status')} (Sig: {inq.get('hmac_signature', '')[:16]}...)")
        lines.append("-" * 65)
    return "\n".join(lines)


__all__ = [
    "INQUIRIES_FILE",
    "OWNER_EMAIL",
    "format_inquiries_summary",
    "get_inquiries_file",
    "get_owner_inquiries",
    "load_all_inquiries",
    "record_inquiry",
    "verify_owner_passcode",
]
