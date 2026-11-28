"""语音厂商的 API Key:每家可配多个,轮询使用,用完/失效自动换下一个。

存在数据卷里(默认 /home/wechat/.wxbot/voice_keys.json,权限 600),接口只返回打码后的 key。
  {"providers": {厂商: {"keys": [{id, key, label, group, enabled, added}], "auto_enroll": bool, "next": 0}},
   "state": {key_id: {until, reason, error, uses, fails, last_ok}}}

group(账号分组):同一个厂商账号下的多个 key 共用复刻音色;不同分组要各自复刻一次(见 core/tts.py)。
不填时每个 key 自成一组。

出错后的处理(厂商把错误归成下面几类,见 core/voice_providers.py):
  rate       限流/并发超限 → 冷却 RATE_COOLDOWN 秒
  exhausted  余额不足/额度用完/欠费 → 冷却 EXHAUSTED_COOLDOWN 秒(后台可手动恢复)
  auth       key 无效/没权限 → 停用,直到在后台恢复
其余错误(参数不对、网络超时等)不换 key,直接报出来。
"""
import json
import os
import threading
import time
import uuid

import config

RATE_COOLDOWN = 60
EXHAUSTED_COOLDOWN = 6 * 3600
FOREVER = 10 ** 12
ROTATE_ON = ("rate", "exhausted", "auth")

_lock = threading.RLock()


class NoKeyError(Exception):
    """这家厂商没有可用的 key(没配、全在冷却或全失效)。"""
    def __init__(self, provider, detail=""):
        super().__init__(detail or f"{provider} 没有可用的 API Key")
        self.provider = provider
        self.message = str(self)


def _path():
    p = os.environ.get("WXBOT_VOICE_KEYS")
    if p:
        return p
    d = "/home/wechat/.wxbot"
    return os.path.join(d if os.path.isdir(d) else config.WORK_DIR, "voice_keys.json")


def _read():
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("providers", {})
            d.setdefault("state", {})
            return d
    except (OSError, ValueError):
        pass
    return {"providers": {}, "state": {}}


def _write(d):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _migrate(d):
    """第一次用时把原来的百炼 key(环境变量 / 旧 key 文件)导进来,分组 default。"""
    prov = d["providers"].setdefault("aliyun", {})
    if prov.get("keys") or prov.get("migrated"):
        return False
    prov["migrated"] = True
    try:
        from core import voice_clone
        k = voice_clone.legacy_key()
    except Exception:
        k = ""
    if k:
        prov["keys"] = [{"id": "k" + uuid.uuid4().hex[:8], "key": k, "label": "原有 key", "group": "default",
                         "enabled": True, "added": int(time.time())}]
    return True


def _load():
    d = _read()
    if _migrate(d):
        _write(d)
    return d


def mask(k):
    k = k or ""
    return (k[:4] + "…" + k[-4:]) if len(k) > 12 else "…" + k[-2:]


def group_of(k):
    return (k.get("group") or "").strip() or k["id"]


def _status(st, now):
    until = st.get("until") or 0
    if until > now:
        return st.get("reason") or "cooldown"
    return "ok"


def view(provider=None):
    """后台看的:每家的 key(打码)和状态。"""
    with _lock:
        d = _load()
    now = time.time()
    out = {}
    for name, p in d["providers"].items():
        if provider and name != provider:
            continue
        keys = []
        for k in p.get("keys") or []:
            st = d["state"].get(k["id"]) or {}
            status = "disabled" if not k.get("enabled", True) else _status(st, now)
            keys.append({"id": k["id"], "masked": mask(k["key"]), "label": k.get("label") or "",
                         "group": k.get("group") or "", "enabled": k.get("enabled", True), "status": status,
                         "until": st.get("until") if status not in ("ok", "disabled") else None,
                         "error": st.get("error") or "", "uses": st.get("uses", 0), "fails": st.get("fails", 0),
                         "last_ok": st.get("last_ok")})
        out[name] = {"keys": keys, "auto_enroll": p.get("auto_enroll")}
    return out


def add(provider, key, label="", group=""):
    key = (key or "").strip()
    if not key or any(c.isspace() for c in key) or len(key) > 512:
        raise ValueError("API Key 格式不对")
    with _lock:
        d = _load()
        p = d["providers"].setdefault(provider, {})
        keys = p.setdefault("keys", [])
        if any(k["key"] == key for k in keys):
            raise ValueError("这个 key 已经加过了")
        kid = "k" + uuid.uuid4().hex[:8]
        keys.append({"id": kid, "key": key, "label": (label or "").strip()[:30],
                     "group": (group or "").strip()[:30], "enabled": True, "added": int(time.time())})
        _write(d)
    return kid


def update(provider, key_id, **fields):
    """改 label/group/enabled;reset=True 清掉冷却/失效状态。"""
    with _lock:
        d = _load()
        k = _find(d, provider, key_id)
        for f in ("label", "group"):
            if f in fields and fields[f] is not None:
                k[f] = str(fields[f]).strip()[:30]
        if fields.get("enabled") is not None:
            k["enabled"] = bool(fields["enabled"])
        if fields.get("reset") or fields.get("enabled"):
            st = d["state"].setdefault(key_id, {})
            st.update(until=0, reason="", error="")
        _write(d)


def remove(provider, key_id):
    with _lock:
        d = _load()
        p = d["providers"].get(provider) or {}
        p["keys"] = [k for k in p.get("keys") or [] if k["id"] != key_id]
        d["state"].pop(key_id, None)
        _write(d)


def set_auto_enroll(provider, on):
    with _lock:
        d = _load()
        d["providers"].setdefault(provider, {})["auto_enroll"] = bool(on)
        _write(d)


def auto_enroll(provider, default=True):
    p = _load()["providers"].get(provider) or {}
    return default if p.get("auto_enroll") is None else bool(p["auto_enroll"])


def _find(d, provider, key_id):
    for k in (d["providers"].get(provider) or {}).get("keys") or []:
        if k["id"] == key_id:
            return k
    raise ValueError("没有这个 key")


def candidates(provider):
    """可用的 key,从轮询指针开始排好序。返回 [{id, key, group}]。"""
    with _lock:
        d = _load()
        p = d["providers"].get(provider) or {}
        keys = [k for k in p.get("keys") or [] if k.get("enabled", True)]
        now = time.time()
        ok = [k for k in keys if _status(d["state"].get(k["id"]) or {}, now) == "ok"]
        if not ok:
            return []
        start = (p.get("next") or 0) % len(ok)
        ordered = ok[start:] + ok[:start]
        p["next"] = (start + 1) % len(ok)
        _write(d)
    return [{"id": k["id"], "key": k["key"], "group": group_of(k)} for k in ordered]


def report(provider, key_id, outcome, error=""):
    """outcome: ok / rate / exhausted / auth / other。"""
    with _lock:
        d = _load()
        st = d["state"].setdefault(key_id, {})
        now = time.time()
        if outcome == "ok":
            st.update(uses=st.get("uses", 0) + 1, last_ok=int(now), until=0, reason="", error="")
        else:
            st["fails"] = st.get("fails", 0) + 1
            st["error"] = (error or "")[:200]
            if outcome == "rate":
                st.update(until=now + RATE_COOLDOWN, reason="rate")
            elif outcome == "exhausted":
                st.update(until=now + EXHAUSTED_COOLDOWN, reason="exhausted")
            elif outcome == "auth":
                st.update(until=FOREVER, reason="auth")
        _write(d)


def run(provider, fn, log=None):
    """用这家厂商的 key 轮流调 fn(key_entry):key 被限流/用完/失效就记下状态换下一个。
    fn 抛 voice_providers.ProviderError;kind 不在 ROTATE_ON 时直接往上抛。"""
    from core.voice_providers import ProviderError
    tried = []
    for k in candidates(provider):
        try:
            out = fn(k)
        except ProviderError as e:
            if e.kind == "skip":           # 这个 key 用不了这个音色(没复刻过且不允许自动复刻)
                tried.append(f"{k['id']}:skip")
                continue
            if e.kind in ROTATE_ON:
                report(provider, k["id"], e.kind, e.message)
                tried.append(f"{k['id']}:{e.kind}")
                if log:
                    log(f"[语音] {provider} key {k['id']} {e.kind}:{e.message[:80]},换下一个")
                continue
            report(provider, k["id"], "other", e.message)
            raise
        report(provider, k["id"], "ok")
        return out
    raise NoKeyError(provider, f"{provider} 没有可用的 API Key" + (f"(试过:{', '.join(tried)})" if tried else
                                                                   "(后台还没配,或都在冷却/失效)"))
