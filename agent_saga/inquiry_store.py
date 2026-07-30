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


def record_inquiry(name: str, email: str, message: str, company: str = "N/A",
                   subject: str = "General", client_ip: str = "127.0.0.1") -> Dict[str, Any]:
    inquiries = load_all_inquiries()
    record = {
        "id": f"inq_{int(time.time() * 1000)}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "name": name.strip(),
        "email": email.strip(),
        "company": company.strip() or "N/A",
        "subject": subject.strip() or "General Inquiry",
        "message": message.strip(),
        "client_ip": client_ip,
    }
    inquiries.append(record)
    content = json.dumps(inquiries, indent=2)
    INQUIRIES_FILE.write_text(content, encoding="utf-8")
    BACKUP_FILE.write_text(content, encoding="utf-8")
    return record


def format_inquiries_summary() -> str:
    inquiries = load_all_inquiries()
    lines = [f"Physical User Inquiries & Reviews ({len(inquiries)} total):",
             f"File Location: {INQUIRIES_FILE}", ""]
    if not inquiries:
        lines.append("  No inquiries received yet. Users can submit via website or CLI.")
        return "\n".join(lines)

    for idx, inq in enumerate(inquiries, 1):
        lines.append(f"[{idx}] ID: {inq.get('id')} | Received: {inq.get('timestamp')}")
        lines.append(f"    From    : {inq.get('name')} <{inq.get('email')}> ({inq.get('company')})")
        lines.append(f"    Subject : {inq.get('subject')}")
        lines.append(f"    Message : {inq.get('message')}")
        lines.append("-" * 65)
    return "\n".join(lines)


__all__ = [
    "INQUIRIES_FILE",
    "format_inquiries_summary",
    "get_inquiries_file",
    "load_all_inquiries",
    "record_inquiry",
]
