"""复刻音色(阿里云百炼 CosyVoice 声音复刻)。

只用于本人或已获授权的声音。流程:
  prepare_sample(任意音视频) → 抽出说话片段、压掉长停顿、响度归一,得到 10~20 秒 24k 单声道 wav
  create_voice(样本, 显示名) → 百炼 voice-enrollment 创建音色(免费),写进 core/tts 的音色配置
  synth(文本, 音色) → 用绑定的模型合成 mp3(按百炼模型计费,控制台可设「用完即停」)

key:环境变量 DASHSCOPE_API_KEY;读不到时读 /home/wechat/.wxbot/dashscope_api_key(在容器数据卷里,权限 600)。
复刻音色绑定创建时的模型,不能跨模型用。
"""
import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

import config

MODEL = os.environ.get("WXBOT_CLONE_MODEL", "cosyvoice-v3.5-plus")
API = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
KEY_FILE = os.environ.get("WXBOT_DASHSCOPE_KEY_FILE", "/home/wechat/.wxbot/dashscope_api_key")
FFMPEG = os.environ.get("WXBOT_FFMPEG", "ffmpeg")
TARGET_SECONDS = 18       # 百炼推荐 10~20 秒
GAP = 0.3                 # 片段之间统一留的停顿(要求停顿 ≤ 2 秒)


class CloneError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def api_key():
    k = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if not k:
        try:
            with open(KEY_FILE, encoding="utf-8") as f:
                k = f.read().strip()
        except OSError:
            k = ""
    if not k:
        raise CloneError("no_key", "没有配置 DASHSCOPE_API_KEY")
    return k


def _ffmpeg(args, timeout=180):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", *args], capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        raise CloneError("no_ffmpeg", "没有 ffmpeg")
    return r


def _speech_segments(wav, duration):
    out = _ffmpeg(["-i", wav, "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"]).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    segs, cur = [], 0.0
    for s, e in zip(starts, ends):
        if s - cur > 0.25:
            segs.append((max(0.0, cur - 0.12), min(duration, s + 0.12)))
        cur = e
    if duration - cur > 0.25:
        segs.append((max(0.0, cur - 0.12), duration))
    return segs


def prepare_sample(src, out_dir=None):
    """任意音频/视频 → 适合复刻的样本 wav。返回 (路径, 秒数)。"""
    if not os.path.isfile(src):
        raise CloneError("no_file", "录音文件不存在")
    out_dir = out_dir or os.path.join(config.WORK_DIR, "voice_clone")
    os.makedirs(out_dir, exist_ok=True)
    tag = time.strftime("%Y%m%d-%H%M%S")
    full = os.path.join(out_dir, f"{tag}-full.wav")
    r = _ffmpeg(["-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", full])
    if r.returncode != 0 or not os.path.isfile(full):
        raise CloneError("bad_audio", "读不出音轨:" + (r.stderr.strip().splitlines() or ["?"])[-1])
    import wave
    with wave.open(full, "rb") as w:
        duration = w.getnframes() / float(w.getframerate())
    segs = _speech_segments(full, duration)
    parts, total = [], 0.0
    for a, b in segs:
        parts.append((a, b))
        total += b - a + GAP
        if total >= TARGET_SECONDS:
            break
    if sum(b - a for a, b in parts) < 6:
        raise CloneError("too_short", "录音里有效说话不到 6 秒,复刻至少要 5 秒连续清晰的说话")
    filt = []
    for i, (a, b) in enumerate(parts):
        filt.append(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.03,"
                    f"afade=t=out:st={max(0, b - a - 0.03):.3f}:d=0.03[s{i}];")
        filt.append(f"anullsrc=r=24000:cl=mono,atrim=0:{GAP}[g{i}];")
    graph = ("".join(filt) + "".join(f"[s{i}][g{i}]" for i in range(len(parts)))
             + f"concat=n={2 * len(parts)}:v=0:a=1,loudnorm=I=-18:TP=-2,aresample=24000[out]")
    sample = os.path.join(out_dir, f"{tag}-sample.wav")
    r = _ffmpeg(["-y", "-loglevel", "error", "-i", full, "-filter_complex", graph, "-map", "[out]",
                 "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", sample])
    if r.returncode != 0:
        raise CloneError("convert_failed", "整理样本失败:" + (r.stderr.strip().splitlines() or ["?"])[-1])
    with wave.open(sample, "rb") as w:
        secs = w.getnframes() / float(w.getframerate())
    return sample, secs


def _call(inp, timeout=90):
    req = urllib.request.Request(API, data=json.dumps({"model": "voice-enrollment", "input": inp}).encode(),
                                 headers={"Authorization": "Bearer " + api_key(),
                                          "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        raise CloneError("api_error", f"百炼返回 {e.code}:{body}")
    except urllib.error.URLError as e:
        raise CloneError("network", f"连不上百炼:{e.reason}")


def enroll(sample_wav, prefix="wxbot", model=MODEL):
    """样本 → 百炼复刻音色,返回 voice_id。样本以 Base64 Data URI 提交,不需要公网地址。"""
    if os.path.getsize(sample_wav) > 10 * 1024 * 1024:
        raise CloneError("too_large", "样本超过 10MB")
    data = base64.b64encode(open(sample_wav, "rb").read()).decode()
    prefix = re.sub(r"[^a-z0-9]", "", prefix.lower())[:9] or "wxbot"
    r = _call({"action": "create_voice", "target_model": model, "prefix": prefix,
               "url": "data:audio/wav;base64," + data})
    vid = (r.get("output") or {}).get("voice_id")
    if not vid:
        raise CloneError("api_error", f"没拿到 voice_id:{r}")
    return vid


def delete_voice(voice_id):
    _call({"action": "delete_voice", "voice_id": voice_id})


def list_voices(prefix=None):
    inp = {"action": "list_voice", "page_index": 0, "page_size": 100}
    if prefix:
        inp["prefix"] = prefix
    return (_call(inp).get("output") or {}).get("voice_list") or []


def synth(text, voice_id, model=MODEL, path=None):
    """用复刻音色合成 mp3,写到 path 并返回 path。"""
    try:
        import dashscope
    except ImportError:
        raise CloneError("no_sdk", "没有安装 dashscope(python3 -m pip install dashscope)")
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # 未配置 workspace 时走公共网关的提示
            client = dashscope.Client(api_key=api_key())
            audio = client.audio.tts.synthesize(text, model=model, voice=voice_id, format="mp3", sample_rate=24000)
    except CloneError:
        raise
    except Exception as e:  # noqa: BLE001 SDK 的各种网络/额度错误
        raise CloneError("synth_failed", f"复刻音色合成失败:{e}")
    if not audio:
        raise CloneError("synth_failed", "复刻音色没有返回音频")
    with open(path, "wb") as f:
        f.write(audio)
    return path


def create_voice(src, label, key=None, model=MODEL):
    """一步到位:整理样本 → 创建复刻音色 → 写进音色配置。返回音色配置里的 key。"""
    from core import tts
    sample, secs = prepare_sample(src)
    key = key or "clone_" + time.strftime("%m%d%H%M")
    vid = enroll(sample, prefix=key.replace("_", ""), model=model)
    tts.add_clone_voice(key, label, vid, model, sample_seconds=round(secs, 1))
    return key, vid, secs
