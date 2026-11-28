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
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d):
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path())


def get_config():
    """{default, voices:[{key,label,name,rate,pitch,volume,builtin,changed}]}。用户改动覆盖预设。"""
    d = _read()
    custom = d.get("voices") or {}
    voices = []
    for key, base in PRESETS.items():
        v = dict(base, volume="+0%")
        v.update({k: x for k, x in (custom.get(key) or {}).items() if k in ("label", "name", "rate", "pitch", "volume")})
        voices.append(dict(v, key=key, builtin=True, changed=key in custom))
    for key, v in custom.items():
        if key not in PRESETS:
            voices.append(dict({"volume": "+0%"}, **v, key=key, builtin=False, changed=True))
    keys = {v["key"] for v in voices}
    default = d.get("default") if d.get("default") in keys else DEFAULT_KEY
    return {"default": default, "voices": voices}


def get_voice(key=None):
    cfg = get_config()
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
        else:
            merged = dict(voices.get(key) or {}, **clean)
            if not merged.get("name") or not merged.get("label"):
                raise TTSError("bad_voice", "自定义音色要填显示名和 Edge 音色名")
            voices[key] = merged
        _write(d)
    return get_config()


def delete_voice(key):
    """自定义音色删掉;内置音色恢复预设。"""
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
    params = _clean({"rate": rate or v.get("rate"), "pitch": pitch or v.get("pitch"),
                     "volume": volume or v.get("volume")})
    edge = _edge()
    out_dir = out_dir or os.path.join(config.WORK_DIR, "direct_media")
    os.makedirs(out_dir, exist_ok=True)
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
