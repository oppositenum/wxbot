#!/usr/bin/env python3
"""Host-side registry for already-running WeChat containers.

Listens on 127.0.0.1:5090 (WXBOT_CONSOLE_PORT). It only records containers
the operator adopts. It does not start, stop, or restart anything, and it
does not write any container's work/multi_accounts.json.

Run:  python3 tools/wxbot_console.py
Registry:  ~/.wxbot/console/registry.json
"""
import fcntl
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("WXBOT_CONSOLE_PORT", "5090"))
BIND = "127.0.0.1"
MAX_ACCOUNTS = 5
PROBE_TIMEOUT = 2
POOL_SIZE = 5
REGISTRY = Path(os.environ.get(
    "WXBOT_CONSOLE_REGISTRY", str(Path.home() / ".wxbot" / "console" / "registry.json")))
# Exclusive lock lives on the registry path itself (not a sibling .lock file).
NAME_RE = re.compile(r"^wxbot[a-zA-Z0-9_.-]*$")
WEB_CONTAINER_PORT = "5100"
VNC_CONTAINER_PORT = "6080"
SECRET_KEYS = ("token", "password", "secret", "passwd", "key")

_POOL = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="wxbot-console")


def slug_for(container):
    """Stable id derived only from the container name. Not a fixed 'primary'."""
    slug = re.sub(r"[^a-z0-9]+", "-", container.lower()).strip("-")
    return slug or "wxbot"


def _empty():
    return {"version": 1, "accounts": []}


def _read_unlocked():
    try:
        value = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(value, dict):
        return _empty()
    accounts = [a for a in value.get("accounts") or [] if isinstance(a, dict)]
    return {"version": 1, "accounts": accounts}


def _write_unlocked(value):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, REGISTRY)


def locked(modify):
    """fcntl exclusive lock around one read-modify-write of the registry file."""
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(REGISTRY, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = _read_unlocked()
        updated, result = modify(current)
        if updated is not None:
            _write_unlocked(updated)
        return result
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_registry():
    def _load(current):
        return None, current
    return locked(_load)


def docker_run(args, timeout=PROBE_TIMEOUT):
    return subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def container_exists(name):
    # Exit 0 means docker found the object. Do not require a particular format
    # string so callers can stub inspect without echoing the name back.
    proc = docker_run(["inspect", "--format", "{{.Name}}", name])
    return proc.returncode == 0


def parse_port_lines(text):
    web = vnc = None
    for line in text.splitlines():
        # "5100/tcp -> 127.0.0.1:5100" or "5100/tcp -> [::]:5100"
        match = re.match(r"^(\d+)/tcp\s+->\s+\S+:(\d+)\s*$", line.strip())
        if not match:
            continue
        container_port, host_port = match.group(1), int(match.group(2))
        if container_port == WEB_CONTAINER_PORT and web is None:
            web = host_port
        elif container_port == VNC_CONTAINER_PORT and vnc is None:
            vnc = host_port
    return web, vnc


def published_ports(name):
    """Host ports for container 5100 (web) and 6080 (noVNC) from `docker port`."""
    proc = docker_run(["port", name])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "docker port failed").strip()[-400:])
    return parse_port_lines(proc.stdout)


def inspect_meta(name):
    fmt = "{{json .HostConfig.Binds}}\t{{.Config.Hostname}}"
    proc = docker_run(["inspect", "--format", fmt, name])
    volume = ""
    hostname = ""
    if proc.returncode == 0 and proc.stdout.strip():
        raw_binds, _, raw_host = proc.stdout.strip().partition("\t")
        hostname = raw_host.strip()
        try:
            binds = json.loads(raw_binds) if raw_binds and raw_binds != "<no value>" else []
        except ValueError:
            binds = []
        if isinstance(binds, list):
            for bind in binds:
                text = str(bind)
                src = text.split(":", 1)[0]
                parts = [part for part in src.split("/") if part and part != "_data"]
                base = ""
                for part in reversed(parts):
                    if part.endswith("-home") or part.startswith("wxbot"):
                        base = part
                        break
                if not base and parts:
                    base = parts[-1]
                if base:
                    volume = base
                    break
    return volume, hostname


def public_account(account):
    return {
        "id": account.get("id", ""),
        "label": account.get("label", ""),
        "container": account.get("container", ""),
        "web_port": account.get("web_port"),
        "vnc_port": account.get("vnc_port"),
        "expected_wxid": account.get("expected_wxid", ""),
        "volume": account.get("volume", ""),
        "hostname": account.get("hostname", ""),
    }


def probe_one(account):
    name = account.get("container") or ""
    info = public_account(account)
    info["probe"] = {"ok": False, "running": False, "error": ""}
    if not name:
        info["probe"]["error"] = "no_container"
        return info
    try:
        ps = docker_run(["ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"])
        if ps.returncode != 0:
            info["probe"]["error"] = (ps.stderr or "docker ps failed").strip()[-400:]
            return info
        names = [line.strip() for line in ps.stdout.splitlines() if line.strip()]
        info["probe"]["running"] = name in names
        port_proc = docker_run(["port", name])
        if port_proc.returncode != 0:
            info["probe"]["error"] = (port_proc.stderr or "docker port failed").strip()[-400:]
            return info
        web, vnc = parse_port_lines(port_proc.stdout)
        info["probe"]["web_port"] = web
        info["probe"]["vnc_port"] = vnc
        info["probe"]["ok"] = True
    except subprocess.TimeoutExpired:
        info["probe"]["error"] = "timeout"
    except Exception as exc:
        info["probe"]["error"] = str(exc)[-400:]
    return info


def list_with_probes():
    registry = read_registry()
    accounts = registry.get("accounts") or []
    error = ""
    probed = []
    if accounts:
        futures = [_POOL.submit(probe_one, account) for account in accounts]
        for future in futures:
            try:
                probed.append(future.result(timeout=PROBE_TIMEOUT))
            except FuturesTimeout:
                probed.append({"probe": {"ok": False, "running": False, "error": "timeout"}})
            except Exception as exc:
                error = str(exc)[-400:]
                probed.append({"probe": {"ok": False, "running": False, "error": error}})
    return {"version": 1, "accounts": probed, "error": error}


def list_candidates():
    proc = docker_run(["ps", "--filter", "name=wxbot", "--format", "{{.Names}}"])
    if proc.returncode != 0:
        return {"candidates": [], "error": (proc.stderr or proc.stdout or "docker ps failed").strip()[-400:]}
    names = []
    for line in proc.stdout.splitlines():
        name = line.strip()
        if name and NAME_RE.match(name):
            names.append(name)
    candidates = []
    error = ""
    for name in names:
        item = {"name": name, "ports": {}}
        try:
            port_proc = docker_run(["port", name])
            if port_proc.returncode != 0:
                item["error"] = (port_proc.stderr or "docker port failed").strip()[-400:]
            else:
                item["ports_raw"] = port_proc.stdout.strip()
                web, vnc = parse_port_lines(port_proc.stdout)
                item["ports"] = {"web": web, "vnc": vnc}
        except Exception as exc:
            item["error"] = str(exc)[-400:]
            error = item["error"]
        candidates.append(item)
    return {"candidates": candidates, "error": error}


def adopt(container, label, expected_wxid):
    container = (container or "").strip()
    label = (label or "").strip()
    expected_wxid = (expected_wxid or "").strip()
    if not NAME_RE.match(container):
        return 400, {"ok": False, "reason": "container_not_allowed"}
    if not label:
        return 400, {"ok": False, "reason": "label_required"}
    try:
        exists = container_exists(container)
    except subprocess.TimeoutExpired:
        return 502, {"ok": False, "reason": "docker_timeout"}
    except Exception as exc:
        return 502, {"ok": False, "reason": str(exc)[-400:]}
    if not exists:
        return 404, {"ok": False, "reason": "unknown_container"}
    try:
        web, vnc = published_ports(container)
        volume, hostname = inspect_meta(container)
    except subprocess.TimeoutExpired:
        return 502, {"ok": False, "reason": "docker_timeout"}
    except Exception as exc:
        return 502, {"ok": False, "reason": str(exc)[-400:]}
    if not web or not vnc:
        return 400, {"ok": False, "reason": "ports_not_published", "web_port": web, "vnc_port": vnc}

    account_id = slug_for(container)

    def _modify(current):
        accounts = current.setdefault("accounts", [])
        if any(a.get("id") == account_id or a.get("container") == container for a in accounts):
            return None, (409, {"ok": False, "reason": "already_registered"})
        if len(accounts) >= MAX_ACCOUNTS:
            return None, (409, {"ok": False, "reason": "limit_reached"})
        account = {
            "id": account_id,
            "label": label,
            "container": container,
            "web_port": web,
            "vnc_port": vnc,
            "expected_wxid": expected_wxid,
            "volume": volume,
            "hostname": hostname,
        }
        accounts.append(account)
        return current, (200, {"ok": True, "account": public_account(account)})

    return locked(_modify)


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>微信控制台</title>
<style>
  body { font: 15px/1.5 -apple-system, sans-serif; margin: 24px; color: #1a1a1a; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0 28px; }
  th, td { border-bottom: 1px solid #ddd; text-align: left; padding: 6px 8px; }
  label { display: block; margin: 8px 0; }
  input { padding: 4px 8px; min-width: 240px; }
  button { padding: 6px 14px; }
  .err { color: #a30; }
</style>
</head>
<body>
<h1>微信控制台</h1>
<p>只登记已经在跑的容器。本页没有启停操作。</p>
<h2>已登记</h2>
<div id="err" class="err"></div>
<table>
  <thead><tr><th>名称</th><th>容器</th><th>后台端口</th><th>VNC 端口</th><th>期望 wxid</th></tr></thead>
  <tbody id="rows"></tbody>
</table>
<h2>登记一个正在运行的容器</h2>
<form id="adopt">
  <label>容器名 <input name="container" required placeholder="wxbot-ubuntu-manual"></label>
  <label>显示名 <input name="label" required></label>
  <label>期望 wxid <input name="expected_wxid" placeholder="可留空，不会自动填写"></label>
  <button type="submit">登记</button>
</form>
<pre id="candidates"></pre>
<script>
async function load() {
  const err = document.getElementById('err');
  err.textContent = '';
  const res = await fetch('/api/accounts');
  const data = await res.json();
  if (data.error) err.textContent = data.error;
  const rows = document.getElementById('rows');
  rows.innerHTML = '';
  for (const a of data.accounts || []) {
    const tr = document.createElement('tr');
    for (const key of ['label', 'container', 'web_port', 'vnc_port', 'expected_wxid']) {
      const td = document.createElement('td');
      td.textContent = a[key] == null ? '' : String(a[key]);
      tr.appendChild(td);
    }
    rows.appendChild(tr);
  }
  const cand = await fetch('/api/candidates');
  const listed = await cand.json();
  const lines = (listed.candidates || []).map(c => c.name + '  ' + (c.ports_raw || ''));
  document.getElementById('candidates').textContent =
    lines.length ? '正在运行的候选：\\n' + lines.join('\\n') : '没有名字以 wxbot 开头的运行中容器';
}
document.getElementById('adopt').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const body = {
    container: form.container.value,
    label: form.label.value,
    expected_wxid: form.expected_wxid.value
  };
  const res = await fetch('/api/adopt', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (!data.ok) {
    document.getElementById('err').textContent = data.reason || '登记失败';
    return;
  }
  form.reset();
  load();
});
load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, {"ok": True, "service": "wxbot-console"})
        if path == "/api/accounts":
            try:
                return self._send(200, list_with_probes())
            except Exception as exc:
                registry = _read_unlocked()
                return self._send(200, {
                    "version": 1,
                    "accounts": [public_account(a) for a in registry.get("accounts") or []],
                    "error": str(exc)[-400:],
                })
        if path == "/api/candidates":
            try:
                return self._send(200, list_candidates())
            except Exception as exc:
                return self._send(200, {"candidates": [], "error": str(exc)[-400:]})
        self._send(404, {"ok": False, "reason": "not_found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/adopt":
            return self._send(404, {"ok": False, "reason": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return self._send(400, {"ok": False, "reason": "bad_request"})
        if not isinstance(data, dict):
            return self._send(400, {"ok": False, "reason": "bad_request"})
        code, payload = adopt(
            str(data.get("container", "")),
            str(data.get("label", "")),
            str(data.get("expected_wxid", "")),
        )
        self._send(code, payload)

    def log_message(self, fmt, *args):
        text = fmt % args if args else fmt
        low = str(text).lower()
        if any(key in low for key in SECRET_KEYS):
            return
        return


def main():
    # Do not seed the registry. An empty file is created on the first adopt.
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[wxbot-console] listening on {BIND}:{PORT}; registry {REGISTRY}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
