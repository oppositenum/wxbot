"""微信语音通话 AI 代理(实验):拨给一个人,接通后用 AI 实时对话。

在微信容器里以 wechat 用户运行:
    cd /app && python3 tools/voice_call.py --selftest                  # 不打电话:TTS→ASR→AI 自测
    cd /app && python3 tools/voice_call.py --dial --chat wxid_xxx       # 拨号(只拨你允许的人)

音频走 PulseAudio:
  我们说的 → 播进 vmic(null sink)→ virtual_mic(微信的麦克风)
  对方说的 → 微信播到 vspk(null sink,设为默认输出)→ 录 vspk.monitor → 阿里云实时识别
两路分开,AI 听不到自己的声音(除非对方手机外放回声)。

每通电话的录音、识别、回复、各环节耗时都写到 work/voice_calls/<时间>/,用来分析。
"""
import argparse
import json
import os
import queue
import re
import subprocess as sp
import sys
import threading
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

RATE = 16000
FRAME = 3200                       # 100ms 16k s16le mono
CALL_ICON = (1348, 264)            # 聊天窗口右上角电话图标(微信窗口 916x666 在 502,206)
VOICE_ITEM = (1263, 304)           # 弹出菜单「语音通话」
MAX_CALL = 300                     # 一通最多 5 分钟
RING_TIMEOUT = 60
SILENCE_HANGUP = 45                # 接通后对方 45 秒不说话就道别挂断
ENV = dict(os.environ, DISPLAY=":0", XDG_RUNTIME_DIR=f"/tmp/runtime-{os.getuid()}")


def sh(*cmd, check=False, timeout=20):
    r = sp.run(cmd, env=ENV, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{cmd[0]} 失败:{r.stderr.strip()[:200]}")
    return r.stdout


class Log:
    def __init__(self, d):
        self.d = d
        os.makedirs(d, exist_ok=True)
        self.t0 = time.time()
        self.f = open(os.path.join(d, "events.jsonl"), "a", encoding="utf-8")
        self.lock = threading.Lock()

    def __call__(self, kind, **kw):
        rec = {"t": round(time.time() - self.t0, 3), "kind": kind, **kw}
        with self.lock:
            self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.f.flush()
        print(f"[{rec['t']:7.2f}] {kind} {json.dumps(kw, ensure_ascii=False)[:300]}", flush=True)


# ---------------- 音频 ----------------

def audio_setup(log):
    sinks = sh("pactl", "list", "short", "sinks")
    if not re.search(r"\bvspk\b", sinks):
        sh("pactl", "load-module", "module-null-sink", "sink_name=vspk",
           "sink_properties=device.description=CallSpeaker", "rate=48000", "channels=1")
    sh("pactl", "set-default-sink", "vspk")
    sh("pactl", "set-default-source", "virtual_mic")
    log("audio_setup", sinks=sh("pactl", "list", "short", "sinks").strip().splitlines())


def _streams(kind):
    """pactl list sink-inputs / source-outputs → [(index, 程序名, 当前设备)]。"""
    out, cur = [], None
    for line in sh("pactl", "list", kind).splitlines():
        m = re.match(r"\S.*#(\d+)$", line)
        if m:
            cur = {"idx": m.group(1), "bin": "", "name": "", "dev": ""}
            out.append(cur)
            continue
        if cur is None:
            continue
        line = line.strip()
        if line.startswith("application.process.binary"):
            cur["bin"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("application.name"):
            cur["name"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith(("Sink:", "Source:")):
            cur["dev"] = line.split(":", 1)[1].strip()
    return out


OURS = ("parecord", "pacat", "paplay", "ffmpeg", "python3")


def route_wechat_streams(log):
    """只挪微信自己的流:播放 → vspk,录音 → virtual_mic。我们自己的录音/播放程序不能动
    (第一次实测就是把自己的录音器挪到了麦克风上,结果一直在录自己)。"""
    moved, skipped = [], []
    for st in _streams("sink-inputs"):
        if st["bin"] in OURS or "wechat" not in (st["bin"] + st["name"]).lower():
            skipped.append(("out", st["idx"], st["bin"] or st["name"]))
            continue
        sh("pactl", "move-sink-input", st["idx"], "vspk")
        moved.append(("out", st["idx"], st["bin"] or st["name"]))
    for st in _streams("source-outputs"):
        if st["bin"] in OURS or "wechat" not in (st["bin"] + st["name"]).lower():
            skipped.append(("in", st["idx"], st["bin"] or st["name"]))
            continue
        sh("pactl", "move-source-output", st["idx"], "virtual_mic")
        moved.append(("in", st["idx"], st["bin"] or st["name"]))
    log("route_streams", moved=moved, skipped=skipped)


_our_rec = set()


class Speaker:
    """把合成好的音频播进 vmic;可以被打断。play=整段文件,stream=边合成边播。"""

    def __init__(self, log):
        self.log = log
        self.proc = None
        self.playing = threading.Event()
        self.current = ""
        self.until = 0.0

    def play(self, path, text):
        self.current = text
        dec = sp.Popen(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ar", "48000", "-ac", "1", "-"],
                       stdout=sp.PIPE, env=ENV)
        self.proc = sp.Popen(["pacat", "--device=vmic", "--format=s16le", "--rate=48000", "--channels=1",
                              "--latency-msec=60"], stdin=dec.stdout, env=ENV)
        dec.stdout.close()
        self.playing.set()
        t = time.time()
        self.proc.wait()
        dec.wait()
        self.playing.clear()
        self.until = time.time()
        return time.time() - t

    def play_pcm(self, pcm, rate, text=""):
        self.current = text
        self.proc = sp.Popen(["pacat", "--device=vmic", "--format=s16le", f"--rate={rate}", "--channels=1",
                              "--latency-msec=60"], stdin=sp.PIPE, env=ENV)
        self.playing.set()
        try:
            self.proc.stdin.write(pcm)
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        self.proc.wait()
        self.playing.clear()
        self.until = time.time()

    def open_stream(self, tts_ctx, marks):
        """提前建好合成会话(连 WebSocket + 开任务要 ~1.1 秒),和 AI 思考并行。返回 handle。"""
        from dashscope.audio.tts_v2 import SpeechSynthesizer, ResultCallback, AudioFormat
        import dashscope
        dashscope.api_key = tts_ctx["key"]
        h = {"marks": marks, "ready": threading.Event(), "killed": False}
        h["proc"] = sp.Popen(["pacat", "--device=vmic", "--format=s16le", "--rate=24000", "--channels=1",
                              "--latency-msec=60"], stdin=sp.PIPE, env=ENV)
        spk = self

        class CB(ResultCallback):
            def on_data(self, data):
                if "first_audio" not in marks:
                    marks["first_audio"] = time.time()
                    spk.playing.set()
                if not h["killed"]:
                    try:
                        h["proc"].stdin.write(data)
                    except (BrokenPipeError, ValueError):
                        h["killed"] = True

            def on_error(self, m):
                marks["tts_error"] = str(m)[:200]

        syn = SpeechSynthesizer(model=tts_ctx["model"], voice=tts_ctx["vid"],
                                format=AudioFormat.PCM_24000HZ_MONO_16BIT, callback=CB())
        h["syn"] = syn

        def start():
            try:
                syn._SpeechSynthesizer__start_stream()          # 私有方法:SDK 只在第一次 streaming_call 时才连
                syn._is_first = False
                marks["tts_open"] = time.time()
            except Exception as e:  # noqa: BLE001
                marks["tts_error"] = "open: " + str(e)[:180]
            h["ready"].set()
        threading.Thread(target=start, daemon=True).start()
        return h

    def stream(self, pieces, h):
        """pieces: AI 流式输出的文字片段;一边收一边送进已经打开的合成会话,音频一到就播。"""
        marks, syn = h["marks"], h["syn"]
        self.proc, self.handle = h["proc"], h
        self.current, said = "", []
        opened = False
        try:
            for piece in pieces:
                if h["killed"]:
                    break
                if not piece:
                    continue
                said.append(piece)
                self.current += piece
                marks.setdefault("first_text", time.time())
                if not opened:
                    h["ready"].wait(10)
                    opened = True
                if "tts_error" in marks:
                    break
                syn.streaming_call(piece)
            if not h["killed"] and said and "tts_error" not in marks:
                syn.streaming_complete()
        except Exception as e:  # noqa: BLE001
            marks["tts_error"] = str(e)[:200]
        if not said:                                          # AI 什么都没说:把开好的会话关掉
            try:
                h["ready"].wait(5); syn.streaming_cancel()
            except Exception:  # noqa: BLE001
                pass
        try:
            h["proc"].stdin.close()
        except Exception:  # noqa: BLE001
            pass
        h["proc"].wait()
        self.playing.clear()
        self.until = time.time()
        self.killed = h["killed"]
        return "".join(said)

    def stop(self):
        self.killed = True
        if getattr(self, "handle", None):
            self.handle["killed"] = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            return True
        return False


# ---------------- 识别 ----------------

class Ears:
    """录 vspk.monitor,送阿里云 paraformer 实时识别;一句说完放进 queue。"""

    def __init__(self, key, log, out_dir, on_partial=None):
        import dashscope
        from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult
        dashscope.api_key = key
        self.log, self.q, self.on_partial = log, queue.Queue(), on_partial
        ears = self

        class CB(RecognitionCallback):
            def on_event(self, result):
                s = result.get_sentence()
                if not isinstance(s, dict) or not s.get("text"):
                    return
                if RecognitionResult.is_sentence_end(s):
                    ears.q.put((s["text"], time.time()))
                    ears.log("asr_final", text=s["text"], begin=s.get("begin_time"), end=s.get("end_time"))
                elif ears.on_partial:
                    ears.on_partial(s["text"])

            def on_error(self, result):
                ears.log("asr_error", msg=str(getattr(result, "message", result))[:200])

            def on_close(self):
                ears.log("asr_close")

        self.rec = Recognition(model="paraformer-realtime-v2", format="pcm", sample_rate=RATE, callback=CB(),
                               max_sentence_silence=700, language_hints=["zh"])
        self.wav = wave.open(os.path.join(out_dir, "remote.wav"), "wb")
        self.wav.setnchannels(1); self.wav.setsampwidth(2); self.wav.setframerate(RATE)
        self.stop_ev = threading.Event()
        self.level = 0.0

    def start(self, source="vspk.monitor", feed=None):
        self.rec.start()
        if feed is not None:                       # 自测:直接喂 PCM
            threading.Thread(target=self._feed_bytes, args=(feed,), daemon=True).start()
            return
        self.proc = sp.Popen(["parecord", f"--device={source}", "--raw", f"--rate={RATE}", "--channels=1",
                              "--format=s16le", "--latency-msec=50"], stdout=sp.PIPE, env=ENV)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        import audioop
        while not self.stop_ev.is_set():
            b = self.proc.stdout.read(FRAME)
            if not b:
                break
            self.level = audioop.rms(b, 2)
            self.wav.writeframes(b)
            try:
                self.rec.send_audio_frame(b)
            except Exception as e:  # noqa: BLE001
                self.log("asr_send_error", err=str(e)[:200])
                break

    def _feed_bytes(self, pcm):
        for i in range(0, len(pcm), FRAME):
            self.rec.send_audio_frame(pcm[i:i + FRAME])
            self.wav.writeframes(pcm[i:i + FRAME])
            time.sleep(0.1)
        silence = b"\0" * FRAME
        for _ in range(15):
            self.rec.send_audio_frame(silence)
            time.sleep(0.1)

    def stop(self):
        self.stop_ev.set()
        try:
            if getattr(self, "proc", None):
                self.proc.kill()
            self.rec.stop()
        except Exception:  # noqa: BLE001
            pass
        self.wav.close()


# ---------------- 大脑 ----------------

RULES = """
现在你正在用微信【语音电话】和对方实时通话,你说的话会被直接读出来。要求:
- 像真人打电话:口语、自然,每次只说一两句,不超过 35 个字。
- 不要表情符号、括号、动作描写、markdown、列表。
- 保持你的角色口吻,不要主动谈论语音识别、模型、接口这类技术细节;
  但对方认真问你是不是 AI / 真人时,要如实承认是 AI,不能否认。
- 对方的话来自语音识别,可能有错别字、断句不对或者只有半句;意思不明确就自然地问一句
  (「嗯?你说什么」),不要顺着奇怪的字面意思往下编,也不要改变对对方的称呼。
- 你的名字常被语音识别听错成发音相近的词(比如「夏以昼」被听成「下宇宙」「下一周」「夏一舟」),
  听到这类词先想想是不是在叫你。
- 如果电话那头换成了小朋友(对方说孩子要跟你说话,或者听起来是孩子),就用普通、亲切的大人口吻跟孩子聊,
  不说任何情侣间、暧昧或亲密的话,不用「主人」之类的称呼。
- 对方说要挂了 / 再见 / 拜拜 / 去睡了,你简短道别,并在末尾单独加上 [[HANGUP]]。
"""

CARD_PROMPT = """下面是一个角色的人设。把它浓缩成「打电话用的角色卡」,给另一个模型在语音通话里扮演用:
- 300 字以内,第二人称(「你是……」);
- 只保留:身份、和对方的关系、对对方的称呼、说话的语气和口头习惯、性格要点、绝对不能做的事;
- 去掉场景细节、长例子、格式说明。只输出角色卡本身。

人设:
"""

HIST_TURNS = 8                     # 只带最近 8 条对话


def persona_card(persona_text, llm, cfg, log):
    """长人设(几千字)每轮都带会让首字慢 2 秒以上:浓缩成角色卡,按内容缓存。"""
    import hashlib
    text = (persona_text or "").strip()
    if len(text) <= 600:
        return text
    h = hashlib.sha256(text.encode()).hexdigest()[:16]
    cache = os.path.join(config.WORK_DIR, "voice_calls", "cards", h + ".txt")
    if os.path.isfile(cache):
        card = open(cache, encoding="utf-8").read()
        log("persona_card", cached=True, chars=len(card))
        return card
    t = time.time()
    card = llm.chat("", [{"role": "user", "content": CARD_PROMPT + text}], dict(cfg, max_tokens=700)).strip()
    if len(card) < 50:
        return text
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    open(cache, "w", encoding="utf-8").write(card)
    log("persona_card", cached=False, chars=len(card), secs=round(time.time() - t, 2), card=card)
    return card


class Brain:
    def __init__(self, persona_text, name, log):
        from core import llm
        self.llm, self.log = llm, log
        cfg = dict(llm.load_cfg())
        cfg.update(max_tokens=160, gpt_reasoning_effort="low", grok_reasoning_effort="low")
        self.cfg = cfg
        self.system = persona_card(persona_text, llm, cfg, log) + "\n" + RULES
        self.name = name
        self.hist = []

    def _msgs(self):
        h = self.hist[-HIST_TURNS:]
        while h and h[0]["role"] != "user":               # 开头不能是 assistant(部分中转会报错)
            h = h[1:]
        return h or self.hist[-1:]

    def stream(self, heard, out, cancel):
        """流式回复:yield 干净的文字片段。cancel 被置位就停(对方又说话了,换成新内容重想)。
        历史只在调用方确认后写(commit),被取消的一次不留痕。"""
        cfg, llm = self.cfg, self.llm
        prov = cfg.get("provider")
        msgs = self._msgs() + [{"role": "user", "content": heard}]
        if msgs[0]["role"] != "user":
            msgs = msgs[1:]
        t = time.time()
        if prov == "claude":
            raw_iter = iter([llm.chat(self.system, msgs, cfg)])
        else:
            raw_iter = _openai_stream(llm, cfg, prov, self.system, msgs)
        f = _Clean()
        for raw in raw_iter:
            if cancel.is_set():
                out["cancelled"] = True
                return
            out.setdefault("first_token", time.time())
            piece = f.feed(raw)
            if piece:
                yield piece
        tail = f.flush()
        if tail:
            yield tail
        out["text"], out["hangup"] = f.text.strip(), f.hangup
        out["llm_secs"] = round(time.time() - t, 2)

    def commit(self, heard, said):
        self.hist.append({"role": "user", "content": heard})
        if said:
            self.hist.append({"role": "assistant", "content": said})

    def opener(self):
        t = time.time()
        text = self.llm.chat(self.system, [{"role": "user", "content":
                             "(电话刚接通,对方还没说话。你主动打过来的,先开口说一两句,比如打招呼、说说为什么打给对方。)"}], self.cfg)
        text = re.sub(r"[（(\[【].*?[）)\]】]", "", text or "").strip()
        self.hist.append({"role": "user", "content": "(电话接通了)"})
        self.hist.append({"role": "assistant", "content": text})
        self.log("llm_opener", reply=text, secs=round(time.time() - t, 2))
        return text


def _openai_stream(llm, cfg, prov, system, messages):
    import urllib.request
    base, key, model = llm.creds(cfg, prov)
    url = llm._endpoint(base or llm._default_base(prov), "chat/completions")
    body = {"model": model, "max_tokens": cfg.get("max_tokens") or 200, "stream": True,
            "messages": [{"role": "system", "content": system}] + list(messages)}
    body.update(llm._compat_extras(prov, cfg))
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "content-type": "application/json", "authorization": "Bearer " + key, "user-agent": llm.UA})
    with llm._opener(cfg.get("proxy") or None).open(req, timeout=60) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                d = json.loads(data)
            except ValueError:
                continue
            for ch in d.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    yield piece


class _Clean:
    """流式过滤:去掉（动作）/[旁白]/表情,截住 [[HANGUP]]。"""
    OPEN, CLOSE = "（([【", "）)]】"

    def __init__(self):
        self.depth, self.buf, self.text, self.hangup = 0, "", "", False

    def feed(self, raw):
        out = []
        self.buf += raw
        if "[[HANGUP]]" in self.buf:
            self.hangup = True
            self.buf = self.buf.replace("[[HANGUP]]", "")
        if "[[" in self.buf or self.buf.endswith("["):
            return ""                                          # 可能是 [[HANGUP]] 的一半,等下一片
        for ch in self.buf:
            if ch in self.OPEN:
                self.depth += 1
            elif ch in self.CLOSE:
                self.depth = max(0, self.depth - 1)
            elif self.depth == 0 and not (0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF):
                out.append(ch)
        self.buf = ""
        piece = "".join(out)
        self.text += piece
        return piece

    def flush(self):
        self.buf = self.buf.replace("[[", "").replace("]]", "")
        return self.feed("") if self.buf else ""


def tts_context(voice):
    """流式合成要用的 key / 模型 / 这个 key 分组下的音色 id。非阿里云复刻音色返回 None(走整段合成)。"""
    from core import tts, voice_keys, voice_providers
    v = tts.get_voice(voice)
    if not tts.is_clone(v) or v.get("provider") != "aliyun":
        return None
    p = voice_providers.get("aliyun")
    model = v.get("model") or p.default_model
    for k in voice_keys.candidates("aliyun"):
        vid = (v.get("ids") or {}).get(tts._slot(p, k["group"], model))
        if vid:
            return {"key": k["key"], "model": model, "vid": vid}
    return None


def synth_pcm(text, voice, out_dir, rate=24000):
    from core import tts
    mp3 = tts.synth(text, voice=voice, out_dir=out_dir)
    return sp.run(["ffmpeg", "-loglevel", "error", "-i", mp3, "-f", "s16le", "-ar", str(rate), "-ac", "1", "-"],
                  capture_output=True).stdout


FILLERS = ["嗯。", "嗯嗯。", "哦～"]


def play_filler(pcm):
    """「嗯」单独起一个播放进程,和 AI 思考同时进行(第 3 通实测:串行时带「嗯」的轮次反而慢 0.6 秒)。"""
    p = sp.Popen(["pacat", "--device=vmic", "--format=s16le", "--rate=24000", "--channels=1"],
                 stdin=sp.PIPE, env=ENV)
    try:
        p.stdin.write(pcm); p.stdin.close()
    except BrokenPipeError:
        pass
    p.wait()


def say(text, voice, speaker, log, out_dir):
    from core import tts
    t = time.time()
    path = tts.synth(text, voice=voice, out_dir=out_dir)
    syn = time.time() - t
    log("tts", text=text, secs=round(syn, 2))
    dur = speaker.play(path, text)
    log("spoke", secs=round(dur, 2))
    return syn


# ---------------- 界面 ----------------

def shot(path):
    sh("scrot", "-o", path)


def ocr(path, box=None):
    p = path
    if box:
        x, y, w, h = box
        p = path + ".crop.png"
        sh("ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vf", f"crop={w}:{h}:{x}:{y}", p)
    return sh("tesseract", p, "stdout", "-l", "chi_sim", "--psm", "11", timeout=20)


def wechat_windows():
    out = sh("xdotool", "search", "--onlyvisible", "--class", "wechat").split()
    wins = {}
    for w in out:
        g = sh("xdotool", "getwindowgeometry", w)
        m = re.search(r"Position: (\d+),(\d+).*?Geometry: (\d+)x(\d+)", g, re.S)
        name = sh("xdotool", "getwindowname", w).strip()
        if m:
            wins[w] = {"name": name, "box": tuple(int(x) for x in m.groups())}
    return wins


def click(x, y):
    sh("xdotool", "mousemove", str(x), str(y), "click", "1")


class CallWindow:
    """拨号后新出现的窗口就是通话窗口;接通判定 = 窗口里 OCR 到计时 mm:ss。"""

    def __init__(self, before, log, out_dir):
        self.before, self.log, self.dir = before, log, out_dir
        self.win = None
        self.n = 0

    def find(self):
        for w, info in wechat_windows().items():
            if w not in self.before:
                self.win = (w, info)
                return info
        return None

    def state(self):
        if self.win and self.win[0] not in wechat_windows():
            return "closed", ""
        info = self.find() if not self.win else self.win[1]
        self.n += 1
        p = os.path.join(self.dir, f"ui_{self.n:03d}.png")
        shot(p)
        if not info:                                   # 没找到独立的通话窗口:整屏 OCR,只认通话相关字样
            text = ocr(p).replace(" ", "")
            if re.search(r"挂断|拒绝|无应答|未接听|已取消|通话结束|对方忙", text) and "等待" not in text:
                return "ended", text
            return ("ringing" if re.search(r"等待|呼叫|邀请", text) else "none"), text
        text = ocr(p, info["box"]).replace(" ", "")
        if re.search(r"\b\d{1,2}[:：]\d{2}\b", text):
            return "connected", text
        if re.search(r"挂断|拒绝|无应答|未接听|已取消|通话结束|对方忙", text) and "等待" not in text:
            return "ended", text
        return "ringing", text

    def hangup(self):
        if not self.win:
            self.log("hangup_skipped", why="没找到通话窗口,不盲点")
            return
        x, y, w, h = self.win[1]["box"]
        p = os.path.join(self.dir, "hangup_before.png")
        shot(p)
        # 挂断按钮在通话窗口底部中间偏右(红色);先试 OCR「挂断」位置,找不到就点底部中间
        data = sh("tesseract", p, "stdout", "-l", "chi_sim", "--psm", "11", "tsv", timeout=20)
        for row in data.splitlines()[1:]:
            c = row.split("\t")
            if len(c) == 12 and "挂断" in c[11]:
                bx, by = int(c[6]) + int(c[8]) // 2, int(c[7]) - 30
                if x <= bx <= x + w and y <= by <= y + h:
                    click(bx, by)
                    self.log("hangup_click", x=bx, y=by, how="ocr")
                    return
        click(x + w // 2, y + h - 70)
        self.log("hangup_click", x=x + w // 2, y=y + h - 70, how="guess")


# ---------------- 主流程 ----------------

def aliyun_key():
    from core import voice_keys
    c = voice_keys.candidates("aliyun")
    if not c:
        raise SystemExit("没有可用的阿里云 key(语音识别要用)")
    return c[0]["key"]


def setup_brain(chat, log):
    from core import personalization, voice_reply
    p = personalization.resolve_persona(chat)["persona"]
    voice = voice_reply.voice_for(chat)
    log("persona", name=p.get("name"), voice=voice, source="contact" if voice else "default")
    return Brain(p.get("persona"), p.get("name"), log), voice


CJK = re.compile(r"[\u4e00-\u9fff]")
SHORT_OK = set("嗯好哦喂对行是啊噢嗨哈欸诶")


def meaningful(text):
    """识别碎片过滤:没有中文的短英文(Yeah. / Sure.)、单个无意义的字,都不算一句话。"""
    han = CJK.findall(text or "")
    if not han:
        return len(re.findall(r"[A-Za-z]+", text or "")) >= 3
    if len(han) == 1:
        return han[0] in SHORT_OK
    return True


def run_call(chat, out_dir, dial=True):
    import random
    log = Log(out_dir)
    brain, voice = setup_brain(chat, log)
    audio_setup(log)
    speaker = Speaker(log)
    last_heard = [0.0]
    pending = []                       # 还没回应的话 [(text, t_end)]
    plock = threading.Lock()
    new_speech = threading.Event()     # 有新的一句进来

    def on_partial(text):
        last_heard[0] = time.time()
        # 打断:我们正在说,对方说了 ≥3 个汉字,且不是我们自己的回声
        if speaker.playing.is_set() and len(CJK.findall(text)) >= 3:
            cur = set(speaker.current)
            overlap = sum(1 for ch in text if ch in cur) / max(1, len(text))
            if overlap < 0.5 and speaker.stop():
                log("barge_in", partial=text)

    ears = Ears(aliyun_key(), log, out_dir, on_partial=on_partial)
    ctx = tts_context(voice)
    log("tts_mode", streaming=bool(ctx))

    # 拨号前就把开场白和「嗯」合成好,接通立刻开口
    t = time.time()
    opener_text = brain.opener()
    opener_pcm = synth_pcm(opener_text, voice, out_dir)
    fillers = [synth_pcm(f, voice, out_dir) for f in FILLERS] if ctx else []
    log("prepared", secs=round(time.time() - t, 2), opener=opener_text)

    before = set(wechat_windows())
    if dial:
        p = os.path.join(out_dir, "before_dial.png")
        shot(p)
        head = ocr(p, (820, 245, 460, 40)).replace(" ", "")
        log("chat_header", ocr=head)
        if "besos" not in head or "蜜" in head:
            log("abort", why="当前打开的会话不是 🐮🐮🌱besos,不拨")
            return
    ears.start()
    if dial:
        click(*CALL_ICON); time.sleep(0.8)
        click(*VOICE_ITEM)
        log("dialed", chat=chat)
    t_dial = time.time()
    cw = CallWindow(before, log, out_dir)
    state = "none"
    while time.time() - t_dial < RING_TIMEOUT:
        time.sleep(1.0)
        state, text = cw.state()
        if state != "ringing" or cw.n % 5 == 1:
            log("ui", state=state, ocr=text[:60], window=cw.win[1] if cw.win else None)
        if state in ("connected", "ended", "closed"):
            break
        if last_heard[0] > t_dial + 1 or not ears.q.empty():
            state = "connected"
            log("connected_by_voice")
            break
    if state != "connected":
        log("not_connected", state=state)
        if state == "ringing":
            cw.hangup()
        ears.stop()
        return
    route_wechat_streams(log)
    t_conn = time.time()
    log("connected")

    # 收集线程:识别出的整句放进 pending(过滤碎片和回声)
    stop_all = threading.Event()

    def collector():
        while not stop_all.is_set():
            try:
                heard, t_end = ears.q.get(timeout=0.3)
            except queue.Empty:
                continue
            mine = speaker.current or ""
            if mine and (speaker.playing.is_set() or t_end < speaker.until + 1.5):
                ov = sum(1 for ch in heard if ch in mine) / max(1, len(heard))
                if ov >= 0.6:
                    log("echo_ignored", heard=heard, overlap=round(ov, 2))
                    continue
            if not meaningful(heard):
                log("fragment_ignored", heard=heard)
                continue
            with plock:
                pending.append((heard, t_end))
            new_speech.set()
    threading.Thread(target=collector, daemon=True).start()

    time.sleep(0.3)
    with plock:
        pending.clear()                                  # 接起来说的「喂」:开场白就是回应
    log("speak_opener", text=opener_text, waited=round(time.time() - t_conn, 2))
    speaker.play_pcm(opener_pcm, 24000, opener_text)

    hang = False
    while not hang and time.time() - t_conn < MAX_CALL:
        if time.time() - getattr(ears, "_lvl_t", 0) > 5:
            ears._lvl_t = time.time()
            log("level", remote_rms=int(ears.level))
        if not new_speech.wait(timeout=1.0):
            if int(time.time()) % 3 == 0:
                st, _ = cw.state()
                if st in ("closed", "ended"):
                    log("remote_hangup", state=st)
                    break
            if time.time() - max(last_heard[0], speaker.until, t_conn) > SILENCE_HANGUP:
                say("你那边好像没声音了,那我先挂啦,晚点再聊。", voice, speaker, log, out_dir)
                hang = True
            continue
        # 一句说完马上开始想;对方接着说会触发下面的「合并重想」,不用先干等
        while time.time() - last_heard[0] < 0.2:
            time.sleep(0.05)
        with plock:
            batch, pending[:] = list(pending), []
            new_speech.clear()
        if not batch:
            continue
        heard = "".join(x[0] for x in batch)
        t_end = batch[-1][1]
        if not ctx:
            continue
        # 回复:对方在我们开口前又说了新的话 → 取消这次,合并后重想(最多重来 2 次)
        for attempt in range(3):
            out, marks, cancel = {}, {}, threading.Event()
            h = speaker.open_stream(ctx, marks)
            if fillers and attempt == 0 and random.random() < 0.5:
                threading.Thread(target=play_filler, args=(random.choice(fillers),), daemon=True).start()
                marks["filler"] = time.time()
            res = {}

            def work():
                res["said"] = speaker.stream(brain.stream(heard, out, cancel), h)
            th = threading.Thread(target=work, daemon=True)
            th.start()
            restarted = False
            while th.is_alive():
                th.join(0.1)
                if new_speech.is_set() and "first_audio" not in marks and attempt < 2:
                    cancel.set(); speaker.stop()
                    th.join(3)
                    with plock:
                        more, pending[:] = list(pending), []
                        new_speech.clear()
                    if more:
                        heard += "".join(x[0] for x in more)
                        t_end = more[-1][1]
                    log("rethink", heard=heard, attempt=attempt + 1)
                    restarted = True
                    break
            if restarted:
                continue
            said = res.get("said", "")
            brain.commit(heard, said + ("……" if speaker.killed else ""))
            hang = bool(out.get("hangup")) and not speaker.killed
            rel = lambda k: round(marks[k] - t_end, 2) if k in marks else None  # noqa: E731
            log("turn", heard=heard, reply=said, hangup=hang, killed=speaker.killed, attempt=attempt,
                filler=rel("filler"), first_token=round(out["first_token"] - t_end, 2) if "first_token" in out else None,
                first_audio=rel("first_audio"), done=round(time.time() - t_end, 2), tts_error=marks.get("tts_error"))
            break
    stop_all.set()
    if hang or time.time() - t_conn >= MAX_CALL:
        time.sleep(0.8)
        cw.hangup()
    ears.stop()
    log("call_end", secs=round(time.time() - t_conn, 1))


def selftest_turns(out_dir):
    """不打电话:角色卡 + 三轮流式回复(声音播进 vmic,没人听),看首字/首音时间。"""
    log = Log(out_dir)
    brain, voice = setup_brain(SELFTEST_CHAT, log)
    audio_setup(log)
    ctx, spk = tts_context(voice), Speaker(log)
    log("opener", text=brain.opener())
    for heard in ["你为什么给我打电话？", "是不是有点延迟啊，你是AI吗", "好啦我要去睡觉了，拜拜"]:
        t_end, out, marks = time.time(), {}, {}
        h = spk.open_stream(ctx, marks)
        said = spk.stream(brain.stream(heard, out, threading.Event()), h)
        brain.commit(heard, said)
        log("turn", heard=heard, reply=said, hangup=out.get("hangup"),
            first_token=round(out["first_token"] - t_end, 2), first_audio=round(marks["first_audio"] - t_end, 2))


SELFTEST_CHAT = "wxid_vn2w1t3doieu22"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dial", action="store_true")
    ap.add_argument("--chat", default="")
    a = ap.parse_args()
    out_dir = os.path.join(config.WORK_DIR, "voice_calls", time.strftime("%Y%m%d-%H%M%S"))
    if a.selftest:
        return selftest_turns(out_dir)
    if a.dial:
        if a.chat != "wxid_vn2w1t3doieu22":
            raise SystemExit("实验阶段只允许拨给 🐮🐮🌱besos")
        return run_call(a.chat, out_dir)
    ap.print_help()


if __name__ == "__main__":
    main()
