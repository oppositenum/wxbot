"""复刻音色:只用于本人或已获授权的声音。

  prepare_sample(任意音视频) → 抽出说话片段、压掉长停顿、响度归一,得到 10~20 秒 24k 单声道 wav
  create_voice(录音, 显示名, 厂商, 模型) → 样本存进 work/voice_clone/samples,登记音色并马上复刻一次
厂商(阿里云百炼/MiniMax/火山引擎/ElevenLabs)见 core/voice_providers.py,API Key 和轮询见 core/voice_keys.py,
合成和"换 key 时自动补复刻"见 core/tts.py。

旧版只支持百炼,key 来自环境变量 DASHSCOPE_API_KEY 或 /home/wechat/.wxbot/dashscope_api_key;
现在第一次用时会自动导入到 voice_keys(legacy_key)。
"""
import os
import re
import subprocess
import time

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


def legacy_key():
    """旧版的百炼 key(环境变量或 key 文件),没有返回空串。"""
    try:
        return api_key()
    except CloneError:
        return ""


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







def _samples_dir():
    d = os.path.join(config.WORK_DIR, "voice_clone", "samples")
    os.makedirs(d, exist_ok=True)
    return d


def create_voice(src, label, key=None, provider="aliyun", model=None, opts=None, from_voice=None):
    """一步到位:整理样本(或沿用已有音色的样本)→ 登记音色 → 用第一个可用 key 复刻。
    返回 (音色 key, voice_id, 样本秒数)。复刻失败时不留下这个音色。"""
    import shutil
    from core import tts, voice_keys
    from core.voice_providers import ProviderError
    key = key or "clone_" + time.strftime("%m%d%H%M%S")
    if from_voice:
        old = tts.get_voice(from_voice)
        if not tts.is_clone(old) or not old.get("sample") or not os.path.isfile(old["sample"]):
            raise CloneError("no_file", "这个音色没有保存录音样本,请重新上传")
        sample, secs = old["sample"], old.get("sample_seconds") or 0
    else:
        sample, secs = prepare_sample(src)
    keep = os.path.join(_samples_dir(), key + ".wav")
    if os.path.abspath(sample) != os.path.abspath(keep):
        shutil.copyfile(sample, keep)
    try:
        tts.add_clone_voice(key, label, provider, model, keep, sample_seconds=round(secs, 1), opts=opts)
    except tts.TTSError as e:
        raise CloneError(e.code, e.message)
    try:
        vid = tts.enroll_now(key)
    except (ProviderError, voice_keys.NoKeyError) as e:
        tts.delete_voice(key)
        raise CloneError(getattr(e, "kind", "no_key"), e.message)
    return key, vid, secs
