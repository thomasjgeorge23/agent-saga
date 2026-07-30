"""`site/server.py` -- High-performance backend server for SAGAOPS & agent-saga site.

Features:
  - Serves site static assets (HTML/CSS/JS).
  - Handles `/api/inquiry` (Direct Founder inquiry & review submission).
  - Saves all inquiries securely into local `.saga_inquiries.json` (accessible only by admin/founder).
  - Email notification pipeline directly alerting thomasjgeorge23@gmail.com.
  - Handles `/api/simulate` (Live agent-saga boundary interactive API).
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("agent_saga.site_backend")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

ROOT_DIR = Path(__file__).parent.parent
SITE_DIR = Path(__file__).parent
INQUIRIES_FILE = ROOT_DIR / ".saga_inquiries.json"
FOUNDER_EMAIL = "thomasjgeorge23@gmail.com"
ADMIN_KEY = os.environ.get("SAGAOPS_ADMIN_KEY", "sagaops-secret-admin-2026")


def load_inquiries() -> List[Dict[str, Any]]:
    if not INQUIRIES_FILE.exists():
        return []
    try:
        return json.loads(INQUIRIES_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to read inquiries file: %s", exc)
        return []


def save_inquiry(record: Dict[str, Any]) -> None:
    inquiries = load_inquiries()
    inquiries.append(record)
    INQUIRIES_FILE.write_text(json.dumps(inquiries, indent=2), encoding="utf-8")


def notify_founder_email(inquiry: Dict[str, Any]) -> bool:
    """Attempt direct SMTP or log notification for founder thomasjgeorge23@gmail.com."""
    logger.info("📩 NEW INQUIRY FOR FOUNDER THOMAS J GEORGE from %s (%s): %s",
                inquiry.get("name"), inquiry.get("email"), inquiry.get("message")[:100])
    
    # Optional SMTP configuration via environment variables
    smtp_server = os.environ.get("SMTP_SERVER")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")

    if smtp_server and smtp_user and smtp_pass:
        try:
            body = (f"New Inquiry / Review received on agent-saga website:\n\n"
                    f"Name: {inquiry.get('name')}\n"
                    f"Email: {inquiry.get('email')}\n"
                    f"Company: {inquiry.get('company', 'N/A')}\n"
                    f"Subject: {inquiry.get('subject', 'General')}\n\n"
                    f"Message / Review:\n{inquiry.get('message')}\n\n"
                    f"Received At: {inquiry.get('timestamp')}\n"
                    f"IP: {inquiry.get('client_ip')}\n")
            
            msg = MIMEText(body)
            msg["Subject"] = f"[agent-saga Inquiry] From {inquiry.get('name')}"
            msg["From"] = smtp_user
            msg["To"] = FOUNDER_EMAIL

            with smtplib.SMTP(smtp_server, smtp_port, timeout=5) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            logger.info("Email notification sent successfully to %s", FOUNDER_EMAIL)
            return True
        except Exception as exc:
            logger.warning("SMTP email sending notice (non-fatal): %s", exc)
    return False


class SagaSiteHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        
        if parsed.path == "/api/inquiries":
            # Admin read endpoint -- restricted to founder via secret key or loopback
            auth_header = self.headers.get("X-Admin-Key", "")
            is_local = self.client_address[0] in ("127.0.0.1", "localhost", "::1")
            
            if not is_local and auth_header != ADMIN_KEY:
                self._send_json({"error": "Unauthorized admin access"}, status=403)
                return
            
            inquiries = load_inquiries()
            self._send_json({"count": len(inquiries), "inquiries": inquiries})
            return

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b""
        
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            payload = {}

        if parsed.path == "/api/inquiry":
            name = str(payload.get("name", "")).strip()
            email = str(payload.get("email", "")).strip()
            message = str(payload.get("message", "")).strip()
            company = str(payload.get("company", "")).strip()
            subject = str(payload.get("subject", "General Inquiry")).strip()

            if not name or not email or not message:
                self._send_json({"error": "Name, Email, and Message fields are required"}, status=400)
                return

            record = {
                "id": f"inq_{int(time.time() * 1000)}",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "name": name,
                "email": email,
                "company": company or "N/A",
                "subject": subject,
                "message": message,
                "client_ip": self.client_address[0],
            }

            save_inquiry(record)
            notify_founder_email(record)

            self._send_json({
                "status": "ok",
                "message": "Thank you! Your inquiry has been delivered directly to Founder Thomas J George.",
                "inquiry_id": record["id"],
            })
            return

        if parsed.path == "/api/simulate":
            # Live simulation endpoint for the website's interactive sandbox
            tool_name = payload.get("tool", "stripe.charge")
            amount = payload.get("amount", 100)
            protected = payload.get("protected", True)

            if protected and amount > 5000:
                result = {
                    "status": "BLOCKED_PRE_FLIGHT",
                    "reason": f"PreFlightGate: amount ${amount} exceeds financial safety limit $5000",
                    "action": "withheld",
                }
            else:
                result = {
                    "status": "EXECUTED",
                    "tool": tool_name,
                    "result": {"id": "tx_simulated_101", "amount": amount},
                }

            self._send_json(result)
            return

        self._send_json({"error": "Not Found"}, status=404)

    def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
        content = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Key")
        self.end_headers()


def main():
    port = 8000
    if len(sys.argv) >= 3 and sys.argv[1] in ("--port", "-p"):
        port = int(sys.argv[2])
    server = HTTPServer(("0.0.0.0", port), SagaSiteHandler)
    logger.info("⚡ SAGAOPS Website Backend Server running at http://localhost:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down.")


if __name__ == "__main__":
    main()
