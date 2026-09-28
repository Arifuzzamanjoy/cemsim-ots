"""Mock of the FreeCAD-MCP addon XML-RPC server, for testing the CemSim bridge
without a live FreeCAD.  Same methods the bridge uses: ping, get_rpc_status,
execute_code.  Test-control methods: mock_mode(mode), mock_stats().

modes:  ok        - behaves like a healthy FreeCAD
        stalled   - GUI thread not dispatching (like a stuck mouse button / modal dialog):
                    execute_code returns the addon's "gave up ... waiting to start" error
        restarted - FreeCAD restarted: helper module and document are gone (then -> ok)
"""

import sys
import threading
import time
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

STATE = {"mode": "ok", "installed": False, "built": False, "builds": 0, "updates": 0, "calls": 0}
LOCK = threading.Lock()


def ping():
    return True


def get_rpc_status():
    return {
        "success": True,
        "rpc_server": "running",
        "gui_dispatch": {"state": "healthy", "task_id": 0, "operation": ""},
    }


def execute_code(code):
    with LOCK:
        STATE["calls"] += 1
        mode = STATE["mode"]
    if mode == "stalled":
        time.sleep(2.0)  # the real addon waits queue_timeout (90 s)
        return {
            "success": False,
            "error": "GUI dispatch gave up after 90.0s waiting for 'execute_code' to start (queue_timeout=90s)",
        }
    time.sleep(0.02)
    with LOCK:
        if "sys.modules['cemsim_fc'] = m" in code:
            compile(code, "<mock>", "exec")  # the payload must at least be valid Python
            STATE["installed"] = True
            return {"success": True, "message": "Python code executed successfully.\nOutput: cemsim_fc installed\n"}
        if ".build()" in code:
            if not STATE["installed"]:
                return {"success": False, "error": "KeyError: 'cemsim_fc'"}
            time.sleep(0.5)
            STATE["built"] = True
            STATE["builds"] += 1
            return {"success": True, "message": "Python code executed successfully.\nOutput: objects 92\n"}
        if "m.update(" in code:
            if not (STATE["installed"] and STATE["built"]):
                return {"success": False, "error": "RuntimeError: CEMSIM_NEEDS_BUILD"}
            STATE["updates"] += 1
            return {"success": True, "message": "Python code executed successfully.\n"}
    return {"success": True, "message": "Python code executed successfully.\n"}


def mock_mode(mode):
    with LOCK:
        if mode == "restarted":
            STATE.update(installed=False, built=False)
            mode = "ok"
        STATE["mode"] = mode
    return True


def mock_stats():
    with LOCK:
        return dict(STATE)


class Handler(SimpleXMLRPCRequestHandler):
    def log_message(self, *a):
        pass


if __name__ == "__main__":
    from socketserver import ThreadingMixIn

    class TServer(ThreadingMixIn, SimpleXMLRPCServer):
        daemon_threads = True

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9901
    srv = TServer(("127.0.0.1", port), requestHandler=Handler, allow_none=True, logRequests=False)
    for f in (ping, get_rpc_status, execute_code, mock_mode, mock_stats):
        srv.register_function(f)
    print(f"mock FreeCAD RPC on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()
