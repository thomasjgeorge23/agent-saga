"""stdio transport: sit between an MCP client and an MCP server subprocess.

Speaks JSON-RPC on the wire rather than through an MCP SDK, deliberately. The
proxy has to work against whatever server the customer already runs, and an SDK
dependency would tie the version of that server to the version of this library
for no benefit -- the only messages that need interpreting are `tools/list` and
`tools/call`. Everything else is forwarded byte-for-byte, so protocol features
this library has never heard of keep working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Optional

logger = logging.getLogger("agent_saga.mcp")

# JSON-RPC error codes. -32000..-32099 is the reserved implementation-defined
# range; a refusal is not a protocol error, it is this server's answer.
REFUSED = -32001


class UpstreamServer:
    """An MCP server running as a subprocess, addressed by JSON-RPC over stdio."""

    def __init__(self, command: list, env: Optional[dict] = None):
        self.command = command
        self.env = env
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._pending: dict = {}
        self._next_id = 10_000
        self._reader: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,          # let the server's logs reach the operator
            env=self.env)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fut = self._pending.pop(message.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(message)
        except asyncio.CancelledError:
            raise
        finally:
            # The server died. Fail every in-flight request with the real cause
            # rather than letting callers wait out a timeout.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("upstream MCP server exited"))
            self._pending.clear()

    async def request(self, method: str, params: Optional[dict] = None) -> Any:
        assert self.proc and self.proc.stdin
        self._next_id += 1
        message_id = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = fut
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method,
                   "params": params or {}}
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()
        response = await fut
        if "error" in response:
            raise RuntimeError(f"upstream {method} failed: {response['error']}")
        return response.get("result")

    async def call_tool(self, tool: str, arguments: dict) -> Any:
        result = await self.request("tools/call", {"name": tool, "arguments": arguments})
        # An MCP tool signals a *tool-level* failure with isError, not a
        # JSON-RPC error. Treating that as success would record a committed step
        # for a call that did nothing, and hand the rollback a compensation for
        # an effect that never happened.
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError(f"tool {tool!r} reported an error: "
                               f"{_text_of(result)[:400]}")
        return result

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()


def _text_of(result: Any) -> str:
    if isinstance(result, dict):
        chunks = result.get("content") or []
        return " ".join(c.get("text", "") for c in chunks if isinstance(c, dict))
    return str(result)


async def serve_stdio(proxy: Any, upstream: UpstreamServer, *,
                      stdin: Any = None, stdout: Any = None) -> int:
    """Relay this process's stdin/stdout to `upstream`, intercepting tool calls.

    Anything that is not `tools/list` or `tools/call` is forwarded verbatim, so
    initialization, resources, prompts and any method added after this was
    written continue to work.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), stdin or sys.stdin)
    out = stdout or sys.stdout
    failed = False

    def reply(message_id: Any, result: Any = None, error: Optional[dict] = None) -> None:
        body: dict = {"jsonrpc": "2.0", "id": message_id}
        if error is not None:
            body["error"] = error
        else:
            body["result"] = result
        out.write(json.dumps(body) + "\n")
        out.flush()

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            method = message.get("method")
            message_id = message.get("id")

            if method == "tools/list":
                upstream_result = await upstream.request("tools/list", message.get("params"))
                tools = (upstream_result or {}).get("tools", [])
                reply(message_id, {**(upstream_result or {}),
                                   "tools": proxy.decorate_tools(tools)})
                continue

            if method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name", "")
                try:
                    result = await proxy.call(name, params.get("arguments") or {})
                    reply(message_id, result)
                except Exception as exc:
                    # A refusal must be legible to the model, or it retries the
                    # blocked call forever. It goes back as an error carrying
                    # the reason, not as a silent empty result.
                    failed = True
                    logger.warning("refused or failed %r: %r", name, exc)
                    reply(message_id, error={"code": REFUSED, "message": str(exc)})
                continue

            if message_id is None:
                # A notification: forward and expect nothing back.
                assert upstream.proc and upstream.proc.stdin
                upstream.proc.stdin.write(line)
                await upstream.proc.stdin.drain()
                continue

            try:
                reply(message_id, await upstream.request(method, message.get("params")))
            except Exception as exc:
                reply(message_id, error={"code": REFUSED, "message": str(exc)})
    finally:
        # The client is gone. `failed` decides commit vs rollback, and the
        # boundary policy decides what an ambiguous disconnect means.
        outcome = await proxy.close(failed=failed)
        logger.info("session ended: %s", outcome)
    return 0


__all__ = ["UpstreamServer", "serve_stdio", "REFUSED"]
