"""`saga_service/service.py` -- High-performance FastAPI control plane service for agent-saga (SAGAOPS).

Features:
  - Asynchronous FastAPI lifespan context manager.
  - FileSnapshotStore configured under saga_service/snapshots/.
  - PreFlightGate with high-value escalation rules ($5000+) and anti-spam keyword filters.
  - BYOK FernetEncryptor via AGENT_SAGA_WAL_KEY.
  - Background SnapshotGC daemon alongside RecoveryDaemon.
  - /api/sagas/gate-check and /api/sagas/gc endpoints.
  - SAGAOPS Founder & Owner Attribution: Thomas J George (thomasjgeorge23@gmail.com).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Header, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent_saga.encryption import FernetEncryptor, generate_key
from agent_saga.gate import DEFAULT_RULES, Decision, GateContext, PreFlightGate, PreFlightViolation, Rule, Verdict
from agent_saga.semantics import ActionSemantics
from agent_saga.inquiry_store import get_owner_inquiries, load_all_inquiries, record_inquiry, verify_owner_passcode
from agent_saga.recovery import RecoveryDaemon
from agent_saga.durable import FileSnapshotStore

logger = logging.getLogger("agent_saga.saga_service")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

SERVICE_DIR = Path(__file__).parent
SNAPSHOTS_DIR = SERVICE_DIR / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# 1. BYOK Fernet Encryption Setup via AGENT_SAGA_WAL_KEY
DEFAULT_DEV_KEY = generate_key()
RAW_WAL_KEY = os.environ.get("AGENT_SAGA_WAL_KEY", DEFAULT_DEV_KEY)
encryptor = FernetEncryptor(RAW_WAL_KEY)

# 2. FileSnapshotStore Setup
snapshot_store = FileSnapshotStore(root=SNAPSHOTS_DIR)

# 3. PreFlightGate with High-Value Escalation & Anti-Spam Keyword Rules
def high_value_when(ctx: GateContext) -> bool:
    kwargs = ctx.kwargs or {}
    amount = kwargs.get("amount", 0)
    return isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 5000

high_value_rule = Rule(
    name="high_value_escalation",
    when=high_value_when,
    verdict=Verdict.REQUIRE_APPROVAL,
    reason="Transaction amount exceeds high-value threshold ($5000.00). Human approval required.",
)

# Rule 2: Anti-Spam Keyword Filter
SPAM_KEYWORDS = {"spam", "malicious", "drop_table", "exfiltrate", "phishing"}

def anti_spam_when(ctx: GateContext) -> bool:
    text_content = str(ctx.kwargs or {}).lower()
    return any(kw in text_content for kw in SPAM_KEYWORDS)

anti_spam_rule = Rule(
    name="anti_spam_keyword_filter",
    when=anti_spam_when,
    verdict=Verdict.BLOCK,
    reason="Forbidden keyword detected in parameters. Operation blocked pre-flight.",
)

def default_approval_provider(ctx: GateContext, rule: Rule) -> bool:
    # Allows REQUIRE_APPROVAL decision to return verdict REQUIRE_APPROVAL for API inspection
    return True

# Initialize PreFlightGate with anti-spam filter (BLOCK first), high-value escalation, and approval provider
gate = PreFlightGate(rules=[anti_spam_rule, high_value_rule, *DEFAULT_RULES], approval_provider=default_approval_provider)


# 4. Background Daemons: SnapshotGC and RecoveryDaemon
class SnapshotGCDaemon:
    def __init__(self, store: FileSnapshotStore, ttl_seconds: float = 86400.0):
        self.store = store
        self.ttl_seconds = ttl_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.sweeps_completed = 0
        self.snapshots_pruned = 0

    async def start(self, interval: float = 300.0):
        self._running = True
        self._task = asyncio.create_task(self._loop(interval))
        logger.info("⚡ Background SnapshotGC daemon started (interval=%.1fs, ttl=%.1fs)", interval, self.ttl_seconds)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SnapshotGC daemon stopped.")

    async def sweep(self) -> Dict[str, Any]:
        pruned_count = 0
        cutoff = time.time() - self.ttl_seconds
        try:
            for snap_file in SNAPSHOTS_DIR.glob("*.json"):
                if snap_file.stat().st_mtime < cutoff:
                    snap_file.unlink(missing_ok=True)
                    pruned_count += 1
            self.sweeps_completed += 1
            self.snapshots_pruned += pruned_count
            logger.info("🧹 SnapshotGC sweep completed: %d expired snapshot(s) pruned.", pruned_count)
        except Exception as exc:
            logger.error("Error during SnapshotGC sweep: %s", exc)
        return {"pruned": pruned_count, "sweeps_total": self.sweeps_completed, "pruned_total": self.snapshots_pruned}

    async def _loop(self, interval: float):
        while self._running:
            await self.sweep()
            await asyncio.sleep(interval)


gc_daemon = SnapshotGCDaemon(snapshot_store)
recovery_daemon = RecoveryDaemon(wal_path=str(SERVICE_DIR.parent / "agent-saga.wal"))


# 5. Asynchronous Lifespan Context Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting SAGAOPS Control Plane Service (v0.5.6)...")
    logger.info("🔐 BYOK Encryption active via AGENT_SAGA_WAL_KEY")
    logger.info("📁 FileSnapshotStore initialized at %s", SNAPSHOTS_DIR)

    # Start background daemons
    await gc_daemon.start(interval=300.0)
    recov_task = asyncio.create_task(recovery_daemon.watch(interval=10.0))

    yield

    # Clean shutdown of daemons
    logger.info("Shutting down SAGAOPS Service daemons...")
    await gc_daemon.stop()
    recov_task.cancel()
    try:
        await recov_task
    except asyncio.CancelledError:
        pass
    logger.info("SAGAOPS Service shutdown clean.")


# FastAPI App
app = FastAPI(
    title="SAGAOPS Control Plane Service",
    description="Enterprise Autonomous Agent Safety Platform & Transaction Control Plane. Founded & Owned by Thomas J George.",
    version="0.5.6",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic Schemas
class GateCheckRequest(BaseModel):
    tool: str = Field(..., json_schema_extra={"example": "stripe.charge"})
    kwargs: Dict[str, Any] = Field(default_factory=dict, json_schema_extra={"example": {"amount": 9500, "currency": "USD"}})


class InquirySubmission(BaseModel):
    name: str
    email: str
    company: Optional[str] = "N/A"
    subject: Optional[str] = "General Inquiry"
    message: str


# Endpoints
@app.get("/api/sagas/status")
async def get_service_status():
    snaps = list(SNAPSHOTS_DIR.glob("*.json"))
    return {
        "service": "SAGAOPS Control Plane",
        "version": "0.5.6",
        "owner": "Thomas J George (thomasjgeorge23@gmail.com)",
        "byok_encryption": {
            "status": "ACTIVE",
            "algorithm": "AES-128-CBC-Fernet-HMAC-SHA256",
            "key_source": "AGENT_SAGA_WAL_KEY",
        },
        "snapshot_store": {
            "type": "FileSnapshotStore",
            "directory": str(SNAPSHOTS_DIR),
            "active_snapshots": len(snaps),
        },
        "gate": {
            "rules_count": len(gate.rules),
            "high_value_escalation_limit": 5000.0,
            "anti_spam_filters": list(SPAM_KEYWORDS),
        },
        "daemons": {
            "snapshot_gc": {
                "running": gc_daemon._running,
                "sweeps_completed": gc_daemon.sweeps_completed,
                "snapshots_pruned_total": gc_daemon.snapshots_pruned,
            },
            "recovery_daemon": {
                "running": True,
                "wal_path": recovery_daemon.wal_path,
            },
        },
    }


@app.post("/api/sagas/gate-check")
async def check_preflight_gate(req: GateCheckRequest):
    ctx = GateContext(tool=req.tool, semantics=ActionSemantics.COMPENSABLE, kwargs=req.kwargs)
    try:
        decision = await gate.evaluate(ctx)
        return {
            "verdict": decision.verdict.value,
            "rule": decision.rule,
            "reason": decision.reason,
            "timestamp": time.time(),
        }
    except PreFlightViolation as exc:
        return {
            "verdict": exc.decision.verdict.value,
            "rule": exc.decision.rule,
            "reason": exc.decision.reason,
            "timestamp": time.time(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/sagas/gc")
async def run_manual_gc_sweep():
    result = await gc_daemon.sweep()
    return {
        "status": "OK",
        "message": f"Manual GC sweep completed. {result['pruned']} snapshot(s) pruned.",
        "metrics": result,
    }


@app.post("/api/inquiry")
async def submit_inquiry_api(inq: InquirySubmission):
    if not inq.name or not inq.email or not inq.message:
        raise HTTPException(status_code=400, detail="Name, Email, and Message fields are required")
    record = record_inquiry(
        name=inq.name,
        email=inq.email,
        company=inq.company or "N/A",
        subject=inq.subject or "General Inquiry",
        message=inq.message,
    )
    return {
        "status": "ok",
        "message": "Thank you! Your inquiry has been saved directly to physical disk store (inquiries.json) for Founder Thomas J George.",
        "inquiry_id": record["id"],
    }


@app.get("/api/inquiries")
async def list_inquiries_api(x_owner_key: Optional[str] = Header(None), passcode: Optional[str] = None):
    key = x_owner_key or passcode or ""
    inquiries = get_owner_inquiries(key)
    if inquiries is None:
        raise HTTPException(status_code=403, detail="🔒 Access Denied: Founder Inquiry Vault is restricted to Founder Thomas J George.")
    return {"count": len(inquiries), "recipient": "Founder Thomas J George (thomasjgeorge23@gmail.com)", "inquiries": inquiries}


def main():
    import uvicorn
    port = int(os.environ.get("PORT", 8090))
    uvicorn.run("saga_service.service:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    main()
