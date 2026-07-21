#!/usr/bin/env python3
"""agent-saga :: LLM Hallucination Recovery Demo

Demonstrates agent-saga protecting business state against LLM tool-call hallucinations.
If the model hallucinates an invalid API parameter during a multi-step workflow,
the engine traps the error and rolls back all prior steps in LIFO order, ensuring
no partial/corrupted state remains.

No external databases or credentials required. Pointed at in-memory mock fakes.
Run directly using:
    python examples/hallucination_recovery.py
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import tempfile
from pathlib import Path

# Force UTF-8 output on Windows to prevent console encoding issues
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Insert repository root to path so agent_saga is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging

# Mute internal engine warning logs to keep output clean and readable
logging.getLogger("agent_saga").setLevel(logging.CRITICAL)

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    Compensation,
    SagaAborted,
    compensator,
    saga_scope,
)

# --------------------------------------------------------------------------
# Mock Distributed System State
# --------------------------------------------------------------------------
SYSTEM_STATE = {
    "supabase_clients": {},   # Data Domain mock
    "notion_workspaces": {},  # SaaS Domain mock
    "aws_instances": {}       # DevOps Domain mock
}


def get_timestamp() -> str:
    """Helper to return current timestamp for logs."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------
# Registered Compensators (LIFO Actions)
# --------------------------------------------------------------------------
@compensator("examples.supabase.delete_row")
def delete_supabase_row(client_id: str) -> None:
    """Compensating action for Step 1 (Supabase Client Creation)."""
    SYSTEM_STATE["supabase_clients"].pop(client_id, None)
    print(f"[{get_timestamp()}] [ROLLBACK] Undoing Step 1: DELETE ROW for client_id={client_id!r} in Supabase.")


@compensator("examples.notion.archive_workspace")
def archive_notion_workspace(workspace_id: str) -> None:
    """Compensating action for Step 2 (Notion Workspace Provisioning)."""
    SYSTEM_STATE["notion_workspaces"].pop(workspace_id, None)
    print(f"[{get_timestamp()}] [ROLLBACK] Undoing Step 2: ARCHIVE WORKSPACE for workspace_id={workspace_id!r} in Notion.")


@compensator("examples.aws.terminate_server")
def terminate_aws_server(instance_id: str) -> None:
    """Compensating action for Step 3 (AWS Server Configuration)."""
    SYSTEM_STATE["aws_instances"].pop(instance_id, None)
    print(f"[{get_timestamp()}] [ROLLBACK] Undoing Step 3: TERMINATE SERVER for instance_id={instance_id!r} on AWS.")


# --------------------------------------------------------------------------
# Step Declarations (Forward Workflows)
# --------------------------------------------------------------------------
async def create_client_record(ctx, client_id: str, name: str) -> dict:
    """Step 1 (Data Domain): Create new client record in Supabase."""
    async def _forward():
        await asyncio.sleep(0.05)  # Simulate network latency
        SYSTEM_STATE["supabase_clients"][client_id] = {"name": name, "status": "active"}
        return {"client_id": client_id}

    print(f"\n[{get_timestamp()}] [STEP 1] Data Domain: Creating Supabase client record for {name!r}...")
    res = await ctx.execute(
        tool="supabase.create_client_record",
        semantics=ActionSemantics.COMPENSABLE,
        forward=_forward,
        compensate=lambda r: Compensation(
            fn=delete_supabase_row,
            handler="examples.supabase.delete_row",
            kwargs={"client_id": r["client_id"] if r else "unknown"},
            description=f"DELETE ROW for client_id={r['client_id'] if r else 'unknown'}"
        )
    )
    print(f" -> Step 1 Complete. Registered compensation: DELETE ROW.")
    return res


async def provision_notion_workspace(ctx, workspace_id: str, title: str) -> dict:
    """Step 2 (SaaS Domain): Provision team workspace in Notion."""
    async def _forward():
        await asyncio.sleep(0.05)  # Simulate network latency
        SYSTEM_STATE["notion_workspaces"][workspace_id] = {"title": title, "status": "active"}
        return {"workspace_id": workspace_id}

    print(f"\n[{get_timestamp()}] [STEP 2] SaaS Domain: Provisioning Notion workspace {title!r}...")
    res = await ctx.execute(
        tool="notion.provision_workspace",
        semantics=ActionSemantics.COMPENSABLE,
        forward=_forward,
        compensate=lambda r: Compensation(
            fn=archive_notion_workspace,
            handler="examples.notion.archive_workspace",
            kwargs={"workspace_id": r["workspace_id"] if r else "unknown"},
            description=f"ARCHIVE WORKSPACE for workspace_id={r['workspace_id'] if r else 'unknown'}"
        )
    )
    print(f" -> Step 2 Complete. Registered compensation: ARCHIVE WORKSPACE.")
    return res


async def configure_aws_server(ctx, instance_id: str, config: dict) -> dict:
    """Step 3 (DevOps Domain): Configure cloud server on AWS."""
    async def _forward():
        await asyncio.sleep(0.05)  # Simulate network latency
        # Look for parameters that do not belong to the API schema
        allowed_params = {"instance_type", "region", "ami"}
        invalid_params = [k for k in config if k not in allowed_params]
        
        if invalid_params:
            # Simulate a real API schema validation error caused by LLM hallucination
            raise ValueError(
                f"AWS API Error: Unknown parameter(s) {', '.join(repr(k) for k in invalid_params)} "
                f"in run_instances call."
            )
            
        SYSTEM_STATE["aws_instances"][instance_id] = config
        return {"instance_id": instance_id}

    print(f"\n[{get_timestamp()}] [STEP 3] DevOps Domain: Configuring AWS EC2 server {instance_id!r}...")
    print(f" -> Config Payload sent to AWS: {config}")
    
    return await ctx.execute(
        tool="aws.configure_instance",
        semantics=ActionSemantics.REVERSIBLE,
        forward=_forward,
        compensate=lambda r: None,
        policy_args=config
    )


# --------------------------------------------------------------------------
# Main Execution Runner
# --------------------------------------------------------------------------
async def run_demo() -> None:
    print("=" * 85)
    print("      AGENT-SAGA :: ENTERPRISE LLM HALLUCINATION RECOVERY DEMO")
    print("=" * 85)
    print(f"Initial State of Systems:\n  {SYSTEM_STATE}")
    print("-" * 85)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Spin up a temporary local Write-Ahead Log
        wal_path = Path(tmp_dir) / "hallucination_demo.wal"
        wal = AsyncWAL(wal_path)
        await wal.start()

        try:
            # Wrap the entire B2B onboarding workflow in a transactional saga boundary
            async with saga_scope(wal=wal) as ctx:
                
                # 1. Create database record
                await create_client_record(ctx, "client_45", "Delta Heavy Industries")
                
                # 2. Provision SaaS workspace
                await provision_notion_workspace(ctx, "ws_102", "Delta Heavy Workspace")
                
                # 3. Configure cloud server (AI Agent introduces hallucinated parameters)
                hallucinated_config = {
                    "instance_type": "c6i.xlarge",
                    "region": "us-west-2",
                    "ultra_speed_boost": True  # <-- The Hallucination!
                }
                await configure_aws_server(ctx, "i-099ac", hallucinated_config)

        except SagaAborted as aborted:
            print("\n" + "=" * 85)
            print(f"[{get_timestamp()}] [ENGINE] CRASH INTERCEPTED!")
            print(f" -> Reason: {type(aborted.cause).__name__}: {aborted.cause}")
            print(f" -> Action: Initiating automatic LIFO rollback sequence...")
            print("=" * 85)
            
            # The compensations are executed by the engine before raising SagaAborted.
            # Thus, the print logs from delete_supabase_row and archive_notion_workspace
            # will have already printed inside the traceback window above.
            
            print("-" * 85)
            print(f"[{get_timestamp()}] [SAGA ENGINE] Recovery Complete. Hallucination damage neutralized. Systems remain consistent.")
            print("-" * 85)

        finally:
            await wal.close()

    print(f"Final State of Systems:\n  {SYSTEM_STATE}")
    print("=" * 85)


if __name__ == "__main__":
    asyncio.run(run_demo())
