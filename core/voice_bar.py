"""真语音条(微信 Linux 4.1.9+)。

微信 4.1.13 的输入栏有「发语音」按钮,录的是默认麦克风。容器里的 PulseAudio 建了虚拟麦克风
(往 vmic 播的声音就是 virtual_mic 的输入,见 docker/ubuntu-manual/start-ubuntu-wechat),
所以:音频 → wav → 界面脚本点「发语音」→ paplay 播进 vmic → 点发送。对方收到的是正常语音条。

走 core.sender 的统一发送链路(界面锁、发送账本、回复复核),和文字/图片发送互斥。
"""
import os
import subprocess
import time
import wave

import config

MAX_SECONDS = 59          # 微信语音条上限 60 秒,留一点余量
LEAD_IN = 0.3             # 开头静音:点「发语音」后录音要一会儿才真正开始
TAIL = 0.4                # 结尾静音:点发送前别把最后一个字截掉
FFMPEG = os.environ.get("WXBOT_FFMPEG", "ffmpeg")


class VoiceBarError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _out_dir():
    d = os.path.join(config.WORK_DIR, "voice_bar")
    os.makedirs(d, exist_ok=True)
    return d


def to_wav(src):
    """任意音频 → 48k 单声道 16bit wav,前后补静音,超长截断。返回 (路径, 秒数)。"""
    if not os.path.isfile(src):
        raise VoiceBarError("no_file", "音频不存在")
    dst = os.path.join(_out_dir(), f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.wav")
    af = f"adelay={int(LEAD_IN * 1000)},apad=pad_dur={TAIL}"
    cmd = [FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", src, "-t", str(MAX_SECONDS - LEAD_IN - TAIL),
           "-af", af, "-ac", "1", "-ar", "48000", "-sample_fmt", "s16", dst]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except FileNotFoundError:
        raise VoiceBarError("no_ffmpeg", "容器里没有 ffmpeg")
    except subprocess.TimeoutExpired:
        raise VoiceBarError("convert_failed", "音频转换超时")
    if r.returncode != 0 or not os.path.isfile(dst):
        err = (r.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        raise VoiceBarError("convert_failed", "音频转换失败:" + (err[-1] if err else f"rc={r.returncode}"))
    with wave.open(dst, "rb") as w:
        secs = w.getnframes() / float(w.getframerate())
    if secs < 1.0:
        raise VoiceBarError("too_short", "语音太短(不到 1 秒)")
    return dst, secs


def _display(chat, name):
    if name:
        return name
    try:
        from core import contacts
        return (contacts.all_names().get(chat) or {}).get("name") or ""
    except Exception:
        return ""


def send_audio(chat, audio_path, name="", **context):
    """发一条语音条。返回发送账本视图(ok/status/reason)。"""
    if not chat or any(c.isspace() for c in chat):
        raise VoiceBarError("bad_chat", "请选择发送对象")
    if chat == "filehelper":
        # 4.1.13 搜「文件传输助手」落在「功能」分区,界面脚本打不开;语音条测试请用真实会话
        raise VoiceBarError("bad_chat", "文件传输助手暂不支持语音条")
    display = _display(chat, name)
    if not display:
        raise VoiceBarError("bad_chat", "找不到这个会话的显示名")
    wav, secs = to_wav(audio_path)
    from core import sender
    res = sender.send_voice_bar(display, wav, chat, **context)
    res = dict(res or {})
    res.setdefault("seconds", round(secs, 1))
    return res


def send_tts(chat, text, voice=None, name="", **context):
    """文字 → 配置的音色合成 → 语音条。"""
    from core import tts
    try:
        mp3 = tts.synth(text, voice)
    except tts.TTSError as e:
        raise VoiceBarError(e.code, e.message)
    return send_audio(chat, mp3, name, **context)
