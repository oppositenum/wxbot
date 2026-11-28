"""文字转语音(免费的 Microsoft Edge 在线语音,edge-tts 包)。

音色做成配置:内置几种常用中文音色预设,可在后台改语速/音调或新增自定义音色,
存在 work/tts.json。合成结果是 mp3,交给 direct_send.send_voice 转成声波视频发出
(Linux 版微信发不了语音条)。

edge-tts 走微软在线服务,需要能上网(容器里走代理)。
"""
import asyncio
import json
import os
import re
import threading
import time

import config

MAX_TEXT = 500
RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
PITCH_RE = re.compile(r"^[+-]\d{1,3}Hz$")
KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
NAME_RE = re.compile(r"^[a-z]{2,3}-[A-Za-z]{2,4}(-[a-z]+)?-[A-Za-z]+Neural$")

# 内置预设:key -> 显示名、Edge 音色、语速、音调、音量
# (zh-TW-YunJheNeural 在音色列表里有,但 2026-11 实测合成返回空音频,没放进来)
PRESETS = {
    "warm_low": {"label": "温柔低语(男)", "name": "zh-CN-YunxiNeural", "rate": "-15%", "pitch": "-8Hz"},
    "calm_deep": {"label": "沉稳克制(男)", "name": "zh-CN-YunyangNeural", "rate": "-12%", "pitch": "-6Hz"},
    "magnetic": {"label": "磁性低音(男)", "name": "zh-CN-YunjianNeural", "rate": "-10%", "pitch": "-5Hz"},
    "sunny": {"label": "阳光少年(男)", "name": "zh-CN-YunxiNeural", "rate": "+0%", "pitch": "+0Hz"},
    "boyish": {"label": "少年感(男)", "name": "zh-CN-YunxiaNeural", "rate": "-5%", "pitch": "+0Hz"},
    "cantonese": {"label": "粤语(男)", "name": "zh-HK-WanLungNeural", "rate": "-5%", "pitch": "+0Hz"},
    "female_warm": {"label": "温暖女声", "name": "zh-CN-XiaoxiaoNeural", "rate": "-5%", "pitch": "+0Hz"},
    "female_lively": {"label": "活泼女声", "name": "zh-CN-XiaoyiNeural", "rate": "+0%", "pitch": "+0Hz"},
    "female_tw": {"label": "台湾腔女声", "name": "zh-TW-HsiaoChenNeural", "rate": "-5%", "pitch": "+0Hz"},
}
DEFAULT_KEY = "warm_low"
# 语音发送形式:声波视频(点开就播) / mp3 文件(文件卡片,点开预览播放) /
# 真语音条(微信 4.1.9+ 界面录音,见 core/voice_bar.py)
SEND_AS = ("video", "file", "voice_bar")

_lock = threading.Lock()
_voices_cache = {"t": 0, "list": []}


class TTSError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _path():
    return os.path.join(config.WORK_DIR, "tts.json")


def _read():
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}
    if _migrate(d):
        _write(d)
    return d


def _guess_sample(created):
    """旧复刻音色没记样本路径:按创建时间找 work/voice_clone 里最接近(不晚于)的 *-sample.wav。"""
    d = os.path.join(config.WORK_DIR, "voice_clone")
    best = None
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith("-sample.wav"))
    except OSError:
        return ""
    for n in names:
        try:
            t = time.mktime(time.strptime(n[:15], "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if t <= (created or 0) + 5:
            best = n
    return os.path.join(d, best) if best else ""


def _migrate(d):
    """旧格式 {provider: cosyvoice, voice_id, model} → {provider: aliyun, model, ids: {分组|模型: voice_id}, sample}。
    原来的 key 迁到 voice_keys 时分组是 default。"""
    changed = False
    for key, v in (d.get("voices") or {}).items():
        if v.get("provider") == "cosyvoice":
            model = v.get("model") or "cosyvoice-v3.5-plus"
            v.update(provider="aliyun", model=model, ids={f"default|{model}": v.pop("voice_id", "")} if v.get("voice_id")
                     else {}, sample=v.get("sample") or _guess_sample(v.get("created")))
            v["ids"] = {k: x for k, x in v["ids"].items() if x}
            changed = True
    return changed


def _write(d):
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path())


def get_config(include_hidden=False):
    """{default, send_as, hidden, voices:[{key,label,name,rate,pitch,volume,builtin,changed}]}。用户改动覆盖预设。
    hidden 里的内置音色不出现在列表里(只隐藏,复刻音色合成失败时仍在后台拿 DEFAULT_KEY 兜底)。"""
    d = _read()
    hidden = [k for k in (d.get("hidden") or []) if k in PRESETS]
    custom = d.get("voices") or {}
    voices = []
    for key, base in PRESETS.items():
        v = dict(base, volume="+0%")
        v.update({k: x for k, x in (custom.get(key) or {}).items() if k in ("label", "name", "rate", "pitch", "volume")})
        voices.append(dict(v, key=key, builtin=True, changed=key in custom))
    for key, v in custom.items():
        if key not in PRESETS:
            voices.append(dict({"volume": "+0%"}, **v, key=key, builtin=False, changed=True))
    shown = [v for v in voices if v["key"] not in hidden]
    keys = [v["key"] for v in shown] or [DEFAULT_KEY]
    default = d.get("default") if d.get("default") in keys else (DEFAULT_KEY if DEFAULT_KEY in keys else keys[0])
    send_as = d.get("send_as") if d.get("send_as") in SEND_AS else "video"
    return {"default": default, "send_as": send_as, "hidden": hidden,
            "voices": voices if include_hidden else shown}


def set_hidden(keys):
    """隐藏这些内置音色(不在 PRESETS 里的 key 忽略);传空列表 = 全部显示。"""
    keys = [k for k in dict.fromkeys(keys or []) if k in PRESETS]
    with _lock:
        d = _read()
        if keys:
            d["hidden"] = keys
        else:
            d.pop("hidden", None)
        _write(d)
    return get_config()


def keep_only(keys):
    """只留下这些音色:其余内置音色全部隐藏(自定义音色不受影响),默认音色设成第一个。"""
    keys = list(keys or [])
    for k in keys:
        get_voice(k)
    set_hidden([k for k in PRESETS if k not in keys])
    if keys:
        set_default(keys[0])
    return get_config()


def set_send_as(mode):
    if mode not in SEND_AS:
        raise TTSError("bad_param", "发送形式只能是 video(声波视频)、file(mp3 文件)或 voice_bar(语音条)")
    with _lock:
        d = _read()
        d["send_as"] = mode
        _write(d)
    return get_config()


def send_as():
    return get_config()["send_as"]


def get_voice(key=None):
    cfg = get_config(include_hidden=True)      # 隐藏的音色也要能用(兜底、旧配置)
    key = key or cfg["default"]
    for v in cfg["voices"]:
        if v["key"] == key:
            return v
    raise TTSError("bad_voice", f"没有这个音色:{key}")


def _clean(v):
    out = {}
    label = str(v.get("label") or "").strip()[:30]
    if label:
        out["label"] = label
    name = str(v.get("name") or "").strip()
    if name:
        if not NAME_RE.match(name):
            raise TTSError("bad_voice", f"Edge 音色名不对:{name}")
        out["name"] = name
    for field, rx, unit in (("rate", RATE_RE, "%"), ("pitch", PITCH_RE, "Hz"), ("volume", RATE_RE, "%")):
        x = str(v.get(field) or "").strip()
        if x:
            if not rx.match(x):
                raise TTSError("bad_param", f"{field} 格式应为 +10{unit} / -10{unit}")
            out[field] = x
    return out


def save_voice(key, fields):
    """新增或修改音色。内置音色只保存与预设不同的字段,自定义音色要有 label 和 name。"""
    key = str(key or "").strip()
    if not KEY_RE.match(key):
        raise TTSError("bad_key", "音色 key 只能用小写字母、数字、下划线")
    clean = _clean(fields or {})
    with _lock:
        d = _read()
        voices = d.setdefault("voices", {})
        if key in PRESETS:
            clean = {k: x for k, x in clean.items() if PRESETS[key].get(k, "+0%") != x}
            if clean:
                voices[key] = clean
            else:
                voices.pop(key, None)
        elif (voices.get(key) or {}).get("provider") in _clone_providers():
            # 复刻音色能改显示名和合成模型;语速音调由样本决定
            label = str((fields or {}).get("label") or "").strip()[:30]
            if label:
                voices[key]["label"] = label
            model = str((fields or {}).get("model") or "").strip()
            if model:
                from core import voice_providers
                p = voice_providers.get(voices[key]["provider"])
                if model not in {m for m, _ in p.models}:
                    raise TTSError("bad_param", f"{p.label} 没有这个模型:{model}")
                voices[key]["model"] = model
                voices[key]["name"] = f"{p.id}:{model}"
        else:
            merged = dict(voices.get(key) or {}, **clean)
            if not merged.get("name") or not merged.get("label"):
                raise TTSError("bad_voice", "自定义音色要填显示名和 Edge 音色名")
            voices[key] = merged
        _write(d)
    return get_config()


def _clone_providers():
    from core import voice_providers
    return voice_providers.PROVIDERS


def is_clone(v):
    return (v or {}).get("provider") in _clone_providers()


def add_clone_voice(key, label, provider, model, sample, sample_seconds=None, opts=None):
    """登记一个复刻音色(core/voice_clone.py 创建;真正的复刻在第一次 enroll 时做)。"""
    if not KEY_RE.match(key or ""):
        raise TTSError("bad_key", "音色 key 只能用小写字母、数字、下划线")
    from core import voice_providers
    p = voice_providers.get(provider)
    model = model or p.default_model
    if model not in {m for m, _ in p.models}:
        raise TTSError("bad_param", f"{p.label} 没有这个模型:{model}")
    with _lock:
        d = _read()
        d.setdefault("voices", {})[key] = {
            "label": (label or key)[:30], "provider": p.id, "model": model, "name": f"{p.id}:{model}",
            "rate": "+0%", "pitch": "+0Hz", "volume": "+0%", "ids": {}, "sample": sample,
            "opts": {k: str(x)[:64] for k, x in (opts or {}).items() if x},
            "created": int(time.time()), "sample_seconds": sample_seconds}
        _write(d)
    return get_config()


def _slot(p, group, model):
    """音色 ID 按什么存:同一分组(同一厂商账号)共用;绑定模型的厂商还要按模型分。"""
    return f"{group}|{model}" if p.model_bound else group


def _set_id(key, slot, vid):
    with _lock:
        d = _read()
        v = (d.get("voices") or {}).get(key)
        if v is None:
            return
        ids = v.setdefault("ids", {})
        if vid:
            ids[slot] = vid
        else:
            ids.pop(slot, None)
        _write(d)


def _clone_audio(key, text, log=None):
    """用复刻音色合成,返回 mp3 字节。按 key 轮询;这个 key 所在分组还没有这个音色时用样本复刻一次。"""
    from core import voice_keys, voice_providers
    from core.voice_providers import ProviderError
    v = get_voice(key)
    p = voice_providers.get(v["provider"])
    model = v.get("model") or p.default_model
    sample = v.get("sample") or ""

    def enroll(k, slot):
        if not sample or not os.path.isfile(sample):
            raise ProviderError("skip", "没有录音样本,不能在这个 key 下复刻")
        vid = p.enroll(k["key"], sample, key, model, v.get("opts"))
        _set_id(key, slot, vid)
        if log:
            log(f"[语音] {p.label} 在 key 分组 {k['group']} 下复刻了「{v['label']}」")
        return vid

    def attempt(k):
        slot = _slot(p, k["group"], model)
        ids = (get_voice(key).get("ids") or {})
        vid = ids.get(slot)
        if not vid:
            if ids and not voice_keys.auto_enroll(p.id, p.auto_enroll_default):
                raise ProviderError("skip", "这个 key 分组没有复刻过这个音色")
            vid = enroll(k, slot)
        try:
            return p.synth(k["key"], text, vid, model)
        except ProviderError as e:
            if e.kind != "voice_missing":
                raise
            _set_id(key, slot, "")          # 音色被厂商删了(如 MiniMax 7 天未用):重新复刻再试一次
            return p.synth(k["key"], text, enroll(k, slot), model)

    return voice_keys.run(p.id, attempt, log=log)


def enroll_now(key, log=None):
    """刚登记的复刻音色:用第一个可用 key 复刻(新建时就让错误马上暴露)。返回 voice_id。"""
    from core import voice_keys, voice_providers
    v = get_voice(key)
    p = voice_providers.get(v["provider"])
    model = v.get("model") or p.default_model

    def attempt(k):
        vid = p.enroll(k["key"], v["sample"], key, model, v.get("opts"))
        _set_id(key, _slot(p, k["group"], model), vid)
        return vid
    return voice_keys.run(p.id, attempt, log=log)


def delete_voice(key):
    """自定义音色删掉(复刻音色同时删掉厂商那边各个 key 分组下的音色);内置音色恢复预设。"""
    v = (_read().get("voices") or {}).get(key) or {}
    if is_clone(v) and v.get("ids"):
        from core import voice_keys, voice_providers
        p = voice_providers.get(v["provider"])
        keys = {}
        for k in voice_keys.candidates(p.id):
            keys.setdefault(k["group"], k["key"])
        for slot, vid in v["ids"].items():
            k = keys.get(slot.split("|")[0])
            if k:
                try:
                    p.delete(k, vid)
                except Exception:
                    pass
    with _lock:
        d = _read()
        (d.get("voices") or {}).pop(key, None)
        if d.get("default") == key and key not in PRESETS:
            d.pop("default", None)
        _write(d)
    return get_config()


def set_default(key):
    get_voice(key)
    with _lock:
        d = _read()
        d["default"] = key
        _write(d)
    return get_config()


def _edge():
    try:
        import edge_tts
        return edge_tts
    except ImportError:
        raise TTSError("no_edge_tts", "没有安装 edge-tts(python3 -m pip install edge-tts)")


def list_edge_voices(prefix="zh-"):
    """微软在线可用音色(缓存 1 天),给后台下拉框用。"""
    if _voices_cache["list"] and time.time() - _voices_cache["t"] < 86400:
        return _voices_cache["list"]
    edge = _edge()
    try:
        raw = asyncio.run(edge.list_voices())
    except Exception as e:  # noqa: BLE001 网络错误
        raise TTSError("network", f"取音色列表失败:{e}")
    out = [{"name": v["ShortName"], "gender": v.get("Gender", ""), "locale": v.get("Locale", ""),
            "tags": ", ".join((v.get("VoiceTag") or {}).get("VoicePersonalities") or [])}
           for v in raw if v.get("ShortName", "").startswith(prefix)]
    _voices_cache.update(t=time.time(), list=out)
    return out


def synth(text, voice=None, rate=None, pitch=None, volume=None, out_dir=None):
    """合成 mp3,返回路径。rate/pitch/volume 为空时用音色配置里的值。"""
    text = " ".join(str(text or "").split())
    if not text:
        raise TTSError("empty", "没有要读的文字")
    if len(text) > MAX_TEXT:
        raise TTSError("too_long", f"文字超过 {MAX_TEXT} 字")
    v = get_voice(voice)
    out_dir = out_dir or os.path.join(config.WORK_DIR, "direct_media")
    os.makedirs(out_dir, exist_ok=True)
    if is_clone(v):
        from core import voice_keys
        from core.voice_providers import ProviderError
        path = os.path.join(out_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}.clone.mp3")
        try:
            audio = _clone_audio(v["key"], text, log=lambda m: print(m, flush=True))
            with open(path, "wb") as f:
                f.write(audio)
            return path
        except (ProviderError, voice_keys.NoKeyError) as e:
            # 所有 key 都用完/失效或网络问题时退回默认的免费音色,别让语音功能整个停掉
            print(f"[tts] 复刻音色「{v.get('label')}」合成失败,改用默认音色:{e.message}", flush=True)
            v = get_voice(DEFAULT_KEY)      # 内置微软音色,不会再是复刻音色
            rate = pitch = volume = None
    params = _clean({"rate": rate or v.get("rate"), "pitch": pitch or v.get("pitch"),
                     "volume": volume or v.get("volume")})
    edge = _edge()
    path = os.path.join(out_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}.tts.mp3")
    comm = edge.Communicate(text, v["name"], rate=params.get("rate", "+0%"),
                            pitch=params.get("pitch", "+0Hz"), volume=params.get("volume", "+0%"))
    try:
        asyncio.run(comm.save(path))
    except Exception as e:  # noqa: BLE001 网络/服务错误
        raise TTSError("network", f"语音合成失败:{e}")
    if not os.path.isfile(path) or not os.path.getsize(path):
        raise TTSError("network", "语音合成失败:没有返回音频")
    return path
