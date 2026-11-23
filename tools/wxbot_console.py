#!/usr/bin/env python3
"""Host-side registry for already-running WeChat containers.

Run:  python3 tools/wxbot_console.py
Registry:  ~/.wxbot/console/registry.json
"""
import fcntl
import html
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

PORT = int(os.environ.get("WXBOT_CONSOLE_PORT", "5090"))
BIND = "127.0.0.1"
MAX_ACCOUNTS = 5
PROBE_TIMEOUT = 2
POOL_SIZE = 5
REGISTRY = Path(os.environ.get(
    "WXBOT_CONSOLE_REGISTRY", str(Path.home() / ".wxbot" / "console" / "registry.json")))
# Lock a stable sibling. os.replace swaps the registry inode, so LOCK_EX on
# registry.json itself does not cover the next writer.
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


def lock_path():
    return Path(str(REGISTRY) + ".lock")


def locked(modify):
    """Hold LOCK_EX on registry.json.lock across read, temp write, and replace."""
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
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


def _bind_ends(text):
    """Split a docker bind 'source:dest' without treating ':' inside the source as dest.

    Dest paths we care about are absolute and contain no extra ':'.
    """
    if ":/" not in text:
        return text, ""
    src, dest = text.rsplit(":", 1)
    if not dest.startswith("/"):
        src, dest = text.split(":", 1)
    return src, dest


def volume_name_from_source(src):
    """Volume name is the leaf, or the segment before a docker `_data` mountpoint."""
    parts = [part for part in src.split("/") if part]
    if not parts:
        return ""
    if parts[-1] == "_data" and len(parts) >= 2:
        return parts[-2]
    return parts[-1]


def volume_from_binds(binds):
    """Prefer the mount at /home/wechat. Never treat a directory named wxbot as the volume."""
    if not isinstance(binds, list):
        return ""
    parsed = []
    for bind in binds:
        src, dest = _bind_ends(str(bind))
        name = volume_name_from_source(src)
        if name:
            parsed.append((dest, name))
    for dest, name in parsed:
        if dest == "/home/wechat":
            return name
    homes = [name for _dest, name in parsed if name.endswith("-home")]
    if homes:
        return max(homes, key=len)
    named = [name for _dest, name in parsed if name.startswith("wxbot-")]
    if named:
        return max(named, key=len)
    return ""


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
        volume = volume_from_binds(binds)
    return volume, hostname


def inspect_self_container(name):
    # --format= keeps the template off the bare-argument scan used by the docker stub.
    proc = docker_run(["inspect", "--format={{json .Config.Env}}", name])
    if proc.returncode != 0:
        return ""
    return self_container_from_env(proc.stdout.strip())


def self_container_from_env(raw_env):
    try:
        env = json.loads(raw_env) if raw_env and raw_env not in ("<no value>", "null") else []
    except ValueError:
        env = []
    if isinstance(env, dict):
        env = [f"{key}={value}" for key, value in env.items()]
    if not isinstance(env, list):
        return ""
    prefix = "WXBOT_SELF_CONTAINER="
    for item in env:
        text = str(item)
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def page_hostname(host_header):
    """Hostname of the page request. Empty and 0.0.0.0 are rejected. IPv6 is [addr]."""
    raw = (host_header or "").strip()
    if not raw:
        return ""
    hostname = urlsplit("//" + raw).hostname or ""
    if not hostname or hostname == "0.0.0.0":
        return ""
    if ":" in hostname:
        return "[" + hostname + "]"
    return hostname


def entry_urls(account, host_header):
    """Direct http links to the ports adopt stored. Missing port omits that link."""
    host = page_hostname(host_header)
    admin = vnc = ""
    if not host:
        return admin, vnc
    web = account.get("web_port")
    vnc_port = account.get("vnc_port")
    if web:
        admin = f"http://{host}:{int(web)}/"
    if vnc_port:
        vnc = (
            f"http://{host}:{int(vnc_port)}/vnc.html"
            "?autoconnect=true&resize=scale&view_only=false&path=websockify"
        )
    return admin, vnc


def self_name_matches(name):
    """True only when inspect Config.Env self name equals this container's name."""
    try:
        self_name = inspect_self_container(name)
    except Exception:
        return False
    return bool(self_name) and self_name == name


def public_account(account, host_header=""):
    info = {
        "id": account.get("id", ""),
        "label": account.get("label", ""),
        "container": account.get("container", ""),
        "web_port": account.get("web_port"),
        "vnc_port": account.get("vnc_port"),
        "expected_wxid": account.get("expected_wxid", ""),
        "volume": account.get("volume", ""),
        "hostname": account.get("hostname", ""),
    }
    if host_header:
        admin, vnc = entry_urls(account, host_header)
        if admin:
            info["admin_url"] = admin
        if vnc:
            info["vnc_url"] = vnc
        name = account.get("container") or ""
        info["can_stop"] = bool(name) and self_name_matches(name)
    return info


def probe_one(account, host_header=""):
    name = account.get("container") or ""
    info = public_account(account, host_header)
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


def list_with_probes(host_header=""):
    registry = read_registry()
    accounts = registry.get("accounts") or []
    error = ""
    probed = []
    if accounts:
        futures = [_POOL.submit(probe_one, account, host_header) for account in accounts]
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


def account_control(account_id, operation, confirm_wxid):
    if operation not in ("stop", "restart"):
        return 400, {"ok": False, "reason": "unsupported_operation"}
    account_id = (account_id or "").strip()
    confirm_wxid = (confirm_wxid or "").strip()

    def _find(current):
        account = next(
            (a for a in current.get("accounts") or [] if a.get("id") == account_id),
            None,
        )
        return None, account

    account = locked(_find)
    if not account:
        return 404, {"ok": False, "reason": "unknown_account"}
    expected = (account.get("expected_wxid") or "").strip()
    if not confirm_wxid or confirm_wxid != expected:
        return 400, {"ok": False, "reason": "confirm_wxid_mismatch"}
    name = account.get("container") or ""
    if not name or not self_name_matches(name):
        return 403, {"ok": False, "reason": "self_name_mismatch"}
    try:
        proc = docker_run([operation, name], timeout=30)
    except subprocess.TimeoutExpired:
        return 502, {"ok": False, "reason": "docker_timeout"}
    except Exception as exc:
        return 502, {"ok": False, "reason": str(exc)[-400:]}
    if proc.returncode != 0:
        return 502, {"ok": False, "reason": (proc.stderr or proc.stdout or "docker failed").strip()[-400:]}
    return 200, {"ok": True, "account": public_account(account)}


def _link(url, text):
    if not url:
        return ""
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'


def render_page(host_header):
    try:
        registry = read_registry()
        error = ""
    except Exception as exc:
        registry = _empty()
        error = str(exc)[-400:]
    cards = []
    for account in registry.get("accounts") or []:
        admin, vnc = entry_urls(account, host_header)
        name = account.get("container") or ""
        can_control = bool(name) and self_name_matches(name)
        label = html.escape(account.get("label") or account.get("id") or "")
        container = html.escape(name)
        wxid = html.escape(account.get("expected_wxid") or "")
        account_id = html.escape(account.get("id") or "", quote=True)
        links = " ".join(part for part in (_link(admin, "后台"), _link(vnc, "noVNC")) if part)
        controls = ""
        if can_control:
            controls = (
                f'<form method="post" action="/api/accounts/{quote(account.get("id") or "")}/stop">'
                f'<input name="confirm_wxid" placeholder="确认 wxid" required> '
                f'<button type="submit">停止</button></form>'
                f'<form method="post" action="/api/accounts/{quote(account.get("id") or "")}/restart">'
                f'<input name="confirm_wxid" placeholder="确认 wxid" required> '
                f'<button type="submit">重启</button></form>'
            )
        cards.append(
            f'<section class="card"><h2>{label}</h2>'
            f'<p>容器 {container}</p>'
            f'<p>期望 wxid {wxid}</p>'
            f'<p data-account="{account_id}">{links}</p>'
            f'{controls}</section>'
        )
    body = "\n".join(cards) or "<p>还没有登记容器。</p>"
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>微信控制台</title>
<style>
  body {{ font: 15px/1.5 -apple-system, sans-serif; margin: 24px; color: #1a1a1a; }}
  .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 12px 16px; margin: 12px 0; }}
  label {{ display: block; margin: 8px 0; }}
  input {{ padding: 4px 8px; min-width: 240px; }}
  button {{ padding: 6px 14px; }}
  .err {{ color: #a30; }}
</style>
</head>
<body>
<h1>微信控制台</h1>
<p>只登记已经在跑的容器。卡片打开该容器自己的后台和 noVNC。停止只在容器自报名字与真实名字一致时出现，并且要填写该号的期望 wxid。</p>
{err}
<div id="cards">{body}</div>
<h2>登记一个正在运行的容器</h2>
<form id="adopt">
  <label>容器名 <input name="container" required placeholder="wxbot-ubuntu-manual"></label>
  <label>显示名 <input name="label" required></label>
  <label>期望 wxid <input name="expected_wxid" placeholder="可留空，不会自动填写"></label>
  <button type="submit">登记</button>
</form>
<pre id="candidates"></pre>
<script>
document.getElementById('adopt').addEventListener('submit', async (ev) => {{
  ev.preventDefault();
  const form = ev.target;
  const body = {{
    container: form.container.value,
    label: form.label.value,
    expected_wxid: form.expected_wxid.value
  }};
  const res = await fetch('/api/adopt', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)
  }});
  const data = await res.json();
  if (!data.ok) {{
    const err = document.createElement('p');
    err.className = 'err';
    err.textContent = data.reason || '登记失败';
    document.body.prepend(err);
    return;
  }}
  location.reload();
}});
fetch('/api/candidates').then(r => r.json()).then(listed => {{
  const lines = (listed.candidates || []).map(c => c.name + '  ' + (c.ports_raw || ''));
  document.getElementById('candidates').textContent =
    lines.length ? '正在运行的候选：\\n' + lines.join('\\n') : '没有名字以 wxbot 开头的运行中容器';
}});
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

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype == "application/x-www-form-urlencoded":
            from urllib.parse import parse_qs
            parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            return {key: values[-1] if values else "" for key, values in parsed.items()}
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("bad_request")
        return data

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, render_page(self.headers.get("Host", "")), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, {"ok": True, "service": "wxbot-console"})
        if path == "/api/accounts":
            host = self.headers.get("Host", "")
            try:
                return self._send(200, list_with_probes(host))
            except Exception as exc:
                registry = _read_unlocked()
                return self._send(200, {
                    "version": 1,
                    "accounts": [public_account(a, host) for a in registry.get("accounts") or []],
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
        control = re.fullmatch(r"/api/accounts/([^/]+)/(stop|restart)", path)
        if path != "/api/adopt" and not control:
            return self._send(404, {"ok": False, "reason": "not_found"})
        try:
            data = self._read_body()
        except (ValueError, OSError):
            return self._send(400, {"ok": False, "reason": "bad_request"})
        if control:
            account_id, operation = control.group(1), control.group(2)
            from urllib.parse import unquote
            code, payload = account_control(
                unquote(account_id),
                operation,
                str(data.get("confirm_wxid", "")),
            )
            return self._send(code, payload)
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
