#!/usr/bin/env python3
"""Tiny host-side helper that runs whitelisted `docker` operations on behalf of
the wxbot management UI, which itself runs *inside* a container with no docker
socket. Bound to all interfaces so containers can reach it via
host.docker.internal, but every request must carry the shared token and may only
touch wxbot-* containers with a fixed set of operations.

Run:  python3 tools/host_docker_agent.py            # port 5199, token from file
Token file (auto-created):  ~/.wxbot/host_agent.token
"""
import json
import os
import re
import secrets
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("WXBOT_HOST_AGENT_PORT", "5199"))
TOKEN_FILE = Path(os.environ.get(
    "WXBOT_HOST_AGENT_TOKEN_FILE", str(Path.home() / ".wxbot" / "host_agent.token")))
NAME_RE = re.compile(r"^wxbot(-[a-z0-9][a-z0-9-]*)?$")
OPS = {"start", "stop", "restart", "inspect"}


def load_token():
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)
    return tok


TOKEN = load_token()


def run_docker(op, container):
    if op == "inspect":
        try:
            state = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.State.Status}}", container],
                stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
        except Exception:
            state = "stopped"
        return {"ok": True, "container": container, "state": state,
                "running": state == "running"}
    try:
        subprocess.run(["docker", op, container], check=True,
                       capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        return {"ok": False, "container": container, "reason": detail[-400:]}
    return {"ok": True, "container": container}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "service": "wxbot-host-docker-agent"})
        self._send(404, {"ok": False, "reason": "not_found"})

    def do_POST(self):
        if self.path != "/docker":
            return self._send(404, {"ok": False, "reason": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self._send(400, {"ok": False, "reason": "bad_request"})
        if not secrets.compare_digest(str(data.get("token", "")), TOKEN):
            return self._send(403, {"ok": False, "reason": "forbidden"})
        op = data.get("op")
        container = str(data.get("container", ""))
        if op not in OPS:
            return self._send(400, {"ok": False, "reason": "unsupported_op"})
        if not NAME_RE.match(container):
            return self._send(400, {"ok": False, "reason": "container_not_allowed"})
        self._send(200, run_docker(op, container))

    def log_message(self, *args):  # keep stdout quiet
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[host-docker-agent] listening on 0.0.0.0:{PORT}; token file {TOKEN_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
