"""Minimal MCP client over stdio (newline-delimited JSON-RPC 2.0), standard library only.

Enough to validate a real MCP server end to end: initialize -> tools/list -> tools/call.
It exists so the integration suite needs no extra dependency and tests the wire protocol
itself rather than an SDK's view of it.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

PROTOCOL = "2025-06-18"


class MCPError(RuntimeError):
    """JSON-RPC error object returned by the server."""


class MCPStdioClient:
    def __init__(self, argv: list[str], env: dict[str, str] | None = None, stderr_path: str | None = None):
        self.argv = argv
        self.env = {**os.environ, **(env or {})}
        self._stderr_path = stderr_path
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue = queue.Queue()
        self._id = 0
        self.server_info: dict = {}
        self.capabilities: dict = {}

    # -- process ----------------------------------------------------------
    def start(self) -> MCPStdioClient:
        err = open(self._stderr_path, "ab") if self._stderr_path else subprocess.DEVNULL
        self._proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, env=self.env, text=True, bufsize=1
        )
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._q.put(json.loads(line))
            except json.JSONDecodeError:
                self._q.put({"_non_json": line})
        self._q.put({"_eof": True})

    def close(self) -> None:
        p = self._proc
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.wait(timeout=5)
        except Exception:
            p.kill()
        self._proc = None

    def __enter__(self) -> MCPStdioClient:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- protocol ---------------------------------------------------------
    def _send(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def request(self, method: str, params: dict | None = None, timeout: float = 120.0):
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"MCP {method}: no reply in {timeout:g}s")
            try:
                m = self._q.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(f"MCP {method}: no reply in {timeout:g}s") from None
            if m.get("_eof"):
                raise MCPError("MCP server closed its stdout (process exited)")
            if "_non_json" in m:  # a server must not write anything but JSON-RPC to stdout
                raise MCPError(f"non-JSON line on MCP stdout: {m['_non_json'][:200]!r}")
            if m.get("method") and "id" in m:  # server -> client request: answer politely
                self._send({"jsonrpc": "2.0", "id": m["id"], "result": {}})
                continue
            if m.get("id") != rid:
                continue  # notification or stale reply
            if "error" in m:
                raise MCPError(f"{method}: {m['error']}")
            return m["result"]

    def initialize(self) -> dict:
        res = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "duosim-validation", "version": "1"},
            },
            timeout=60,
        )
        self.server_info = res.get("serverInfo", {})
        self.capabilities = res.get("capabilities", {})
        self.notify("notifications/initialized")
        return res

    def list_tools(self) -> list[dict]:
        return self.request("tools/list")["tools"]

    def call_tool(self, name: str, arguments: dict | None = None, timeout: float = 180.0) -> dict:
        """Returns {'text': str, 'images': [b64...], 'is_error': bool, 'raw': result}."""
        res = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        content = res.get("content", [])
        return {
            "text": "\n".join(c.get("text", "") for c in content if c.get("type") == "text"),
            "images": [c["data"] for c in content if c.get("type") == "image"],
            "is_error": bool(res.get("isError")),
            "raw": res,
        }
