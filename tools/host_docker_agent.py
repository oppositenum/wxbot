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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("WXBOT_HOST_AGENT_PORT", "5199"))
TOKEN_FILE = Path(os.environ.get(
    "WXBOT_HOST_AGENT_TOKEN_FILE", str(Path.home() / ".wxbot" / "host_agent.token")))
NAME_RE = re.compile(r"^wxbot(-[a-z0-9][a-z0-9-]*)?$")
OPS = {"start", "stop", "restart", "inspect", "wechat"}
LOOPBACK = {"127.0.0.1", "::1"}
PEER_CACHE_TTL = 5.0
_peer_cache = {"at": None, "owners": {}}
_peer_lock = threading.Lock()


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
CONTAINER_TOKEN = os.environ.get("WXBOT_HOST_AGENT_CONTAINER_TOKEN", "").strip()


def container_names():
    raw = os.environ.get("WXBOT_HOST_AGENT_CONTAINER_NAMES", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def loopback_peer(peer):
    host = str(peer or "").strip().lower()
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in LOOPBACK


def _normalize_peer(peer):
    host = str(peer or "").strip().lower()
    if host.startswith("::ffff:"):
        return host[7:]
    return host


def _parse_container_owners(raw):
    """Map container IPAddress values to Names from `docker network inspect`.

    The gateway is a network field, not a container endpoint, so it is ignored.
    A non-list payload is a failed inspect and must not allow anyone. An address
    that appears on two different containers is ambiguous and is dropped.
    """
    try:
        networks = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(networks, list):
        return None
    found = {}
    for net in networks:
        containers = (net or {}).get("Containers") or {}
        if not isinstance(containers, dict):
            continue
        for info in containers.values():
            if not isinstance(info, dict):
                continue
            name = str(info.get("Name") or "").strip()
            if not name:
                continue
            for key in ("IPv4Address", "IPv6Address"):
                ip = str(info.get(key) or "").split("/", 1)[0].strip().lower()
                if ip:
                    found.setdefault(ip, set()).add(name)
    return {ip: next(iter(names)) for ip, names in found.items() if len(names) == 1}


def container_owners(now=None):
    """IP to container name from `docker network inspect`. Cached a few seconds."""
    moment = time.monotonic() if now is None else now
    with _peer_lock:
        if _peer_cache["at"] is not None and moment - _peer_cache["at"] < PEER_CACHE_TTL:
            return dict(_peer_cache["owners"])
    try:
        ids = subprocess.check_output(
            ["docker", "network", "ls", "-q"],
            stderr=subprocess.DEVNULL, text=True, timeout=5).split()
        raw = subprocess.check_output(
            ["docker", "network", "inspect", *ids],
            stderr=subprocess.DEVNULL, text=True, timeout=5) if ids else "[]"
    except Exception:
        raw = None
    owners = _parse_container_owners(raw) if raw is not None else None
    if owners is None:
        # Inspect failed: do not allow random peers. Keep a previous cache if any.
        with _peer_lock:
            return dict(_peer_cache["owners"])
    with _peer_lock:
        _peer_cache["at"] = moment
        _peer_cache["owners"] = dict(owners)
    return dict(owners)


def container_peer_ips(now=None):
    return set(container_owners(now))


def peer_allowed(peer):
    if loopback_peer(peer):
        return True
    return _normalize_peer(peer) in container_peer_ips()


def authorize(peer, presented, container):
    """Local token: loopback for any container, or a container peer for itself.

    The optional container token may only touch names bound in the environment.
    """
    presented = str(presented or "")
    if TOKEN and secrets.compare_digest(presented, TOKEN):
        if loopback_peer(peer):
            return True
        own = container_owners().get(_normalize_peer(peer))
        return bool(own) and container == own
    if not CONTAINER_TOKEN or not secrets.compare_digest(presented, CONTAINER_TOKEN):
        return False
    names = container_names()
    return bool(names) and container in names


def run_docker(op, container):
    if op == "wechat":
        try:
            proc = subprocess.run(
                ["docker", "exec", container, "pgrep", "-f", "/opt/wechat/wechat"],
                capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "container": container, "reason": str(exc)[-400:]}
        if proc.returncode == 0:
            return {"ok": True, "container": container, "running": True}
        if proc.returncode == 1:
            return {"ok": True, "container": container, "running": False}
        detail = (proc.stderr or proc.stdout or "pgrep_failed").strip()
        return {"ok": False, "container": container, "reason": detail[-400:]}
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
        peer = self.client_address[0] if self.client_address else ""
        if not peer_allowed(peer):
            print(f"[host-docker-agent] rejected peer {peer}", flush=True)
            return self._send(403, {"ok": False, "reason": "forbidden"})
        op = data.get("op")
        container = str(data.get("container", ""))
        if not authorize(peer, data.get("token", ""), container):
            return self._send(403, {"ok": False, "reason": "forbidden"})
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
