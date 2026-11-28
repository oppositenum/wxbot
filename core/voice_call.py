"""微信语音通话 AI(实验):拨给允许名单里的人,接通后 AI 实时对话;也能自动接听名单里的人打来的电话。

音频走 PulseAudio:
  我们说的 → 播进 vmic(null sink)→ virtual_mic(微信的麦克风)
  对方说的 → 微信播到 vspk(null sink,设为默认输出)→ 录 vspk.monitor → 阿里云实时识别
两路分开,AI 听不到自己的声音。

入口:place_call(chat, purpose)(控制台按钮、定时任务)、start_watcher()(自动接听)、tools/voice_call.py(命令行)。
一次只打一通;每通的录音、识别、回复、耗时写在 work/voice_calls/<时间>/。设置在账号目录 voice_call.json。
"""
import json
import os
import queue
import re
import subprocess as sp
import threading
import time
import uuid
import wave

import config

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

    def __init__(self, key, log, out_dir, on_partial=None, vocabulary_id=None):
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

        extra = {"vocabulary_id": vocabulary_id} if vocabulary_id else {}
        self.rec = Recognition(model="paraformer-realtime-v2", format="pcm", sample_rate=RATE, callback=CB(),
                               max_sentence_silence=700, language_hints=["zh"], **extra)
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
    def __init__(self, persona_text, name, log, purpose="", direction="out"):
        from core import llm
        self.llm, self.log = llm, log
        cfg = dict(llm.load_cfg())
        cfg.update(max_tokens=160, gpt_reasoning_effort="low", grok_reasoning_effort="low")
        self.cfg = cfg
        self.purpose, self.direction = (purpose or "").strip(), direction
        extra = ""
        if self.purpose:
            extra = (f"\n这通电话是你打给对方的,目的是:{self.purpose}。开口就自然地说清楚这件事;"
                     "对方答应了 / 知道了,就简短叮嘱一句、道别并加 [[HANGUP]],不要拖着闲聊。\n")
        elif direction == "in":
            extra = "\n这通电话是对方打给你的,你刚接起来。\n"
        self.system = persona_card(persona_text, llm, cfg, log) + "\n" + RULES + extra
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
        if self.direction == "in":
            hint = "(你刚接起对方打来的电话。用一句很短的话接电话,比如「喂,怎么啦?」。)"
        elif self.purpose:
            hint = f"(电话刚接通,对方还没说话。你打过来是为了:{self.purpose}。先打个招呼,然后直接说这件事,两句以内。)"
        else:
            hint = "(电话刚接通,对方还没说话。你主动打过来的,先开口说一两句,比如打招呼、说说为什么打给对方。)"
        text = self.llm.chat(self.system, [{"role": "user", "content": hint}], self.cfg)
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


def ocr(path, box=None, scale=1):
    p = path
    if box:
        x, y, w, h = box
        p = path + ".crop.png"
        vf = f"crop={w}:{h}:{x}:{y}" + (f",scale=iw*{scale}:ih*{scale}" if scale > 1 else "")
        sh("ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vf", vf, p)
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


# ---------------- 设置 ----------------

DEFAULTS = {
    "allow": [],                 # 允许拨打 / 自动接听的联系人 [{"username","name"}]
    "auto_answer": False,        # 名单里的人打来自动接听
    "self_request": False,       # 允许名单外的人在聊天里让机器人给「他自己」打电话
    "max_minutes": 5,
    "fallback_text": True,       # 定时电话没接通 → 改发文字提醒
    "precall_text": "等下给你打个电话～",   # 会话不在最近列表时,先发这句把会话顶上来
    "vocabulary_id": "", "vocabulary_words": [],
}
TEST_ONLY = {"wxid_vn2w1t3doieu22"}   # 实验阶段:名单为空时只允许拨这个测试号


def _settings_path():
    return os.path.join(config.account_dir(), "voice_call.json")


def load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return {**DEFAULTS, **(d if isinstance(d, dict) else {})}


def save_settings(patch):
    d = load_settings()
    for k in ("auto_answer", "self_request", "fallback_text"):
        if k in patch:
            d[k] = bool(patch[k])
    if "max_minutes" in patch:
        d["max_minutes"] = max(1, min(30, int(patch["max_minutes"] or 5)))
    if "precall_text" in patch:
        d["precall_text"] = str(patch["precall_text"] or "")[:60]
    if "allow" in patch:
        seen, out = set(), []
        for x in patch["allow"] or []:
            u = (x.get("username") if isinstance(x, dict) else str(x)) or ""
            if u and u not in seen and not u.endswith("@chatroom"):
                seen.add(u)
                out.append({"username": u, "name": (x.get("name") if isinstance(x, dict) else "") or u})
        d["allow"] = out[:100]
    for k in ("vocabulary_id", "vocabulary_words"):
        if k in patch:
            d[k] = patch[k]
    p = _settings_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return d


def allowed(username, settings=None):
    s = settings or load_settings()
    names = {x["username"] for x in s.get("allow") or []}
    return username in names if names else username in TEST_ONLY


# ---------------- 名字匹配(OCR 核对会话标题 / 来电人) ----------------

CJK = re.compile(r"[一-鿿]")


def _core(name):
    return "".join(CJK.findall(name or "")), re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _emoji_count(name):
    """名字里的表情/符号个数:OCR 会把每个认成一两个怪字。"""
    return sum(1 for ch in name or "" if ord(ch) > 0x2000 and not CJK.match(ch) and not ch.isalnum())


def name_matches(display, ocr_text, fuzzy=False, others=()):
    """OCR 出来的标题是不是这个人。
    - 显示名里的汉字都要出现(fuzzy 时 ≥3 个汉字的名字容 1 个认错,只用来在列表里挑候选);英文数字部分要包含;
    - OCR 多出来的汉字 ≤ 表情个数(至少 1):表情会被认成怪字,「🐮🐮🌱besos」常认成「高谢咱besos」;
    - others:别的联系人名字。OCR 里出现了某个相似联系人独有的汉字(比如「蜜蜜」)→ 一定不是这个人。"""
    han, asc = _core(display)
    ohan, oasc = _core(ocr_text)
    if not han and not asc:
        return False
    missing = [ch for ch in han if ch not in ohan]
    if (len(missing) > (1 if fuzzy and len(han) >= 3 else 0)) or (asc and asc not in oasc):
        return False
    extra = [ch for ch in ohan if ch not in han]
    if len(extra) > max(1, _emoji_count(display)):
        return False
    for other in others:
        o_han, o_asc = _core(other)
        own = [ch for ch in o_han if ch not in han]          # 那个人名字里、这个人没有的汉字
        if own and all(ch in ohan for ch in own) and (not asc or o_asc == asc or not o_asc):
            return False
    return True


def similar_names(display):
    """和这个名字容易混的联系人(英文数字部分一样、或者共用汉字),给 name_matches 做排除。"""
    han, asc = _core(display)
    out = []
    try:
        from core import contacts
        for c in contacts.list_contacts():
            n = c.get("name") or ""
            if not n or n == display:
                continue
            h2, a2 = _core(n)
            if (asc and a2 == asc) or (han and set(han) & set(h2)):
                out.append(n)
    except Exception:  # noqa: BLE001
        pass
    return out[:30]


# 下面的位置都相对主窗口左上角(主窗口默认 502,206,916x666;实际位置每次现查)
HEADER_OFF = (318, 39, 460, 40)          # 聊天标题
LIST_OFF = (120, 84, 190, 580)           # 会话列表的名字那一列(不含头像,头像会被认成怪字)
CALL_OFF = (846, 58)                     # 聊天右上角电话图标
VOICE_OFF = (761, 98)                    # 弹出菜单「语音通话」
WIN = {"x": 502, "y": 206}


def main_window():
    """找到微信主窗口(名字「微信」、宽 800+);缩到托盘了就调出来。返回 (x, y) 或 None。"""
    for _ in range(2):
        for w in sh("xdotool", "search", "--class", "wechat").split():
            if sh("xdotool", "getwindowname", w).strip() != "微信":
                continue
            g = sh("xdotool", "getwindowgeometry", w)
            m = re.search(r"Position: (\d+),(\d+).*?Geometry: (\d+)x(\d+)", g, re.S)
            if not m or int(m.group(3)) < 800:
                continue
            WIN.update(x=int(m.group(1)), y=int(m.group(2)))
            if _drawn():
                return WIN["x"], WIN["y"]
        click(1731, 13)                      # 托盘图标:缩到托盘 / 最小化的主窗口只有它叫得出来(wmctrl、windowmap 都不行)
        time.sleep(1.5)
    return None


def _drawn():
    """主窗口真的画在屏幕上吗:左侧栏是浅灰色,桌面是深蓝色。"""
    try:
        from PIL import Image
        p = "/tmp/vc_drawn.png"
        shot(p)
        px = Image.open(p).convert("RGB").getpixel((WIN["x"] + 30, WIN["y"] + 300))
        return sum(px) > 500
    except Exception:  # noqa: BLE001
        return True


def _abs(off):
    return (WIN["x"] + off[0], WIN["y"] + off[1]) + tuple(off[2:])


def header_text(out_dir):
    p = os.path.join(out_dir, f"hdr_{int(time.time() * 1000)}.png")
    shot(p)
    x, y, w, h = _abs(HEADER_OFF)
    crop = p + ".crop.png"
    sh("ffmpeg", "-loglevel", "error", "-y", "-i", p, "-vf", f"crop={w}:{h}:{x}:{y},scale=iw*2:ih*2", crop)
    return sh("tesseract", crop, "stdout", "-l", "chi_sim", "--psm", "7", timeout=20).replace(" ", "").replace("\n", "")


def _list_rows(out_dir):
    """会话列表里每一项的名字和位置:[(名字, x, y)]。"""
    p = os.path.join(out_dir, f"list_{int(time.time() * 1000)}.png")
    shot(p)
    x0, y0, w, h = _abs(LIST_OFF)
    crop = p + ".crop.png"
    # 放大 2 倍再认:小字号的「蜜」原尺寸会被认成「宣」
    sh("ffmpeg", "-loglevel", "error", "-y", "-i", p, "-vf", f"crop={w}:{h}:{x0}:{y0},scale=iw*2:ih*2", crop)
    rows = {}
    for line in sh("tesseract", crop, "stdout", "-l", "chi_sim", "--psm", "11", "tsv", timeout=20).splitlines()[1:]:
        c = line.split("\t")
        if len(c) != 12 or not c[11].strip():
            continue
        c[6], c[7] = str(int(c[6]) // 2), str(int(c[7]) // 2)
        top = int(c[7])
        slot, off = top // 68, top % 68              # 每项 68 像素高:名字在上面 ~30 像素,下面是消息预览
        if off >= 30:
            continue
        rows.setdefault(slot, []).append((int(c[6]), c[11].strip()))
    out = []
    for slot, parts in sorted(rows.items()):
        name = "".join(t for _x, t in sorted(parts))
        name = re.sub(r"(昨天|前天|星期.|\d{1,2}[:：]\d{2}|\d{1,4}/\d{1,2}(/\d{1,2})?)", "", name)   # 去掉右边的时间
        out.append((name, x0 + 60, y0 + slot * 68 + 22))
    return out


def dial(before, log, out_dir):
    """点电话图标 → 确认弹出了「语音通话」菜单 → 点它 → 等「语音聊天」窗口出来。任何一步没对上就重来一次。
    (实测:主窗口偶尔在拨号前被别的东西缩到托盘,不核对就会对着桌面空点,还以为在响铃。)"""
    for attempt in range(2):
        if not main_window():
            log("dial_failed", why="找不到主窗口")
            return False
        click(*_abs(CALL_OFF))
        time.sleep(0.8)
        p = os.path.join(out_dir, f"dial_menu_{attempt}.png")
        shot(p)
        mx, my = _abs(VOICE_OFF)
        menu = ocr(p, (mx - 70, my - 22, 200, 80), scale=2).replace(" ", "")
        if "语音" not in menu:
            log("dial_retry", why="没看到「语音通话」菜单", ocr=menu[:40], attempt=attempt)
            sh("xdotool", "key", "Escape")
            continue
        click(mx, my)
        for _ in range(25):
            time.sleep(0.2)
            new = {w: i for w, i in wechat_windows().items() if w not in before}
            if any(i["name"] == "语音聊天" or i["box"][2] >= 300 for i in new.values()):
                return True
        shot(os.path.join(out_dir, f"dial_nowindow_{attempt}.png"))
        log("dial_retry", why="点了语音通话但没出现通话窗口", attempt=attempt)
    log("dial_failed", why="两次都没拨出去")
    return False


def ensure_chat_open(username, display, log, out_dir, allow_precall=True):
    """把主窗口切到这个人的聊天,并用标题 OCR 核对。顺序:已经打开 → 最近会话列表里点 → 先发一句把会话顶上来再点。"""
    if not main_window():
        log("chat_open_failed", why="找不到微信主窗口")
        return False
    others = similar_names(display)
    head = header_text(out_dir)
    if name_matches(display, head, others=others):
        log("chat_open", how="already", header=head)
        return True
    for name, x, y in _list_rows(out_dir):
        if name_matches(display, name, fuzzy=True, others=others):
            click(x, y); time.sleep(0.8)
            head = header_text(out_dir)
            if name_matches(display, head, others=others):
                log("chat_open", how="list", header=head)
                return True
    s = load_settings()
    if allow_precall and s.get("precall_text"):
        from core import sender
        r = sender.send_text(display, s["precall_text"], chat_username=username)
        log("precall_text", status=r.get("status"), reason=r.get("reason"))
        time.sleep(2.5)
        for name, x, y in _list_rows(out_dir)[:3]:
            if name_matches(display, name, fuzzy=True, others=others):
                click(x, y); time.sleep(0.8)
                head = header_text(out_dir)
                if name_matches(display, head, others=others):
                    log("chat_open", how="precall", header=head)
                    return True
    log("chat_open_failed", header=head, others=others[:5])
    return False


# ---------------- 热词 ----------------

def vocabulary(key, words, log):
    """识别热词(人设名字、对方名字):paraformer 不认识「夏以昼」,会听成「下宇宙」。词表变了才重建。"""
    words = sorted({w for w in words if w and 2 <= len(w) <= 10 and CJK.search(w)})[:50]
    if not words:
        return None
    s = load_settings()
    if s.get("vocabulary_id") and s.get("vocabulary_words") == words:
        return s["vocabulary_id"]
    try:
        import dashscope
        from dashscope.audio.asr import VocabularyService
        dashscope.api_key = key
        svc = VocabularyService()
        if s.get("vocabulary_id"):
            try:
                svc.delete_vocabulary(s["vocabulary_id"])
            except Exception:  # noqa: BLE001
                pass
        vid = svc.create_vocabulary(prefix="wxbotcall", target_model="paraformer-realtime-v2",
                                    vocabulary=[{"text": w, "weight": 4, "lang": "zh"} for w in words])
        save_settings({"vocabulary_id": vid, "vocabulary_words": words})
        log("vocabulary", words=words, created=True)
        return vid
    except Exception as e:  # noqa: BLE001
        log("vocabulary_error", err=str(e)[:200])
        return None


# ---------------- 通话管理 ----------------

def aliyun_key():
    from core import voice_keys
    c = voice_keys.candidates("aliyun")
    if not c:
        raise CallError("没有可用的阿里云 key(语音识别要用),到「音色与复刻」里配置")
    return c[0]["key"]


class CallError(Exception):
    pass


def setup_brain(chat, log, purpose="", direction="out"):
    from core import personalization, voice_reply
    p = personalization.resolve_persona(chat)["persona"]
    voice = voice_reply.voice_for(chat)
    log("persona", name=p.get("name"), voice=voice, purpose=purpose, direction=direction)
    return Brain(p.get("persona"), p.get("name"), log, purpose=purpose, direction=direction), voice, p.get("name") or ""


_lock = threading.Lock()
STATE = {"busy": False}
_stop = threading.Event()
HISTORY_KEEP = 50


def _calls_dir():
    return os.path.join(config.WORK_DIR, "voice_calls")


def _history_file():
    return os.path.join(config.account_dir(), "voice_call_history.json")


def history():
    try:
        with open(_history_file(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_history(rec):
    h = [x for x in history() if x.get("id") != rec["id"]]
    h.insert(0, rec)
    tmp = _history_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h[:HISTORY_KEEP], f, ensure_ascii=False, indent=1)
    os.replace(tmp, _history_file())


def call_events(call_id):
    rec = next((x for x in history() if x.get("id") == call_id), None)
    if not rec:
        return None
    try:
        with open(os.path.join(rec["dir"], "events.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def status():
    return dict(STATE)


def hangup():
    if not STATE.get("busy"):
        return False
    _stop.set()
    return True


def place_call(username, display="", purpose="", source="manual", on_done=None):
    """后台拨一通电话,立刻返回通话 id。不在允许名单 / 正在通话 → CallError。"""
    if not allowed(username):
        raise CallError("这个联系人不在「可拨打名单」里(控制台 → 语音通话 → 设置)")
    if not _lock.acquire(blocking=False):
        raise CallError("正在通话中,一次只能打一通")
    try:
        display = display or _display_name(username)
        call_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        _stop.clear()
        STATE.clear()
        STATE.update(busy=True, id=call_id, direction="out", chat=username, name=display, purpose=purpose,
                     source=source, phase="preparing", started=time.time(), transcript=[])
    except Exception:
        _lock.release()
        raise

    def run():
        res = {}
        try:
            res = run_call(username, display, os.path.join(_calls_dir(), call_id), purpose=purpose, direction="out")
        except Exception as e:  # noqa: BLE001
            res = {"result": "error", "error": f"{type(e).__name__}: {e}"[:200]}
        finally:
            _finish(res, on_done)
    threading.Thread(target=run, daemon=True, name="voice-call").start()
    return call_id


def _finish(res, on_done):
    rec = {k: STATE.get(k) for k in ("id", "direction", "chat", "name", "purpose", "source", "started")}
    rec.update(ended=time.time(), result=res.get("result", "error"), error=res.get("error", ""),
               connected=bool(res.get("connected")), secs=res.get("secs", 0), turns=res.get("turns", 0),
               transcript=STATE.get("transcript", [])[-40:],
               dir=os.path.join(_calls_dir(), rec["id"] or ""))
    try:
        _save_history(rec)
    except Exception:  # noqa: BLE001
        pass
    STATE.update(busy=False, phase="idle", last=rec)
    _lock.release()
    if on_done:
        try:
            on_done(rec)
        except Exception:  # noqa: BLE001
            pass


def _display_name(username):
    try:
        from core.bot import send_name_for
        return send_name_for(username) or username
    except Exception:  # noqa: BLE001
        return username


def _say_line(role, text):
    STATE.setdefault("transcript", []).append({"t": round(time.time() - STATE.get("started", time.time()), 1),
                                               "who": role, "text": text})


# ---------------- 一通电话 ----------------

SHORT_OK = set("嗯好哦喂对行是啊噢嗨哈欸诶")


def meaningful(text):
    """识别碎片过滤:没有中文的短英文(Yeah. / Sure.)、单个无意义的字,都不算一句话。"""
    han = CJK.findall(text or "")
    if not han:
        return len(re.findall(r"[A-Za-z]+", text or "")) >= 3
    if len(han) == 1:
        return han[0] in SHORT_OK
    return True


def run_call(chat, display, out_dir, purpose="", direction="out", incoming_window=None):
    """打出(direction=out:开会话、拨号、等接通)或接听(in:incoming_window 已点了接听)之后的整通对话。
    返回 {result: connected/no_answer/declined/aborted/error, connected, secs, turns}。"""
    import random
    from core.sender import SEND_LOCK
    log = Log(out_dir)
    settings = load_settings()
    max_call = int(settings.get("max_minutes") or 5) * 60
    brain, voice, persona_name = setup_brain(chat, log, purpose=purpose, direction=direction)
    audio_setup(log)
    speaker = Speaker(log)
    key = aliyun_key()
    vocab = vocabulary(key, [persona_name, re.sub(r"[^一-鿿]", "", display or "")]
                       + [re.sub(r"[^一-鿿]", "", x.get("name", "")) for x in settings.get("allow") or []], log)
    last_heard = [0.0]
    pending, plock, new_speech = [], threading.Lock(), threading.Event()

    def on_partial(text):
        last_heard[0] = time.time()
        if speaker.playing.is_set() and len(CJK.findall(text)) >= 3:
            cur = set(speaker.current)
            overlap = sum(1 for ch in text if ch in cur) / max(1, len(text))
            if overlap < 0.5 and speaker.stop():
                log("barge_in", partial=text)

    ears = Ears(key, log, out_dir, on_partial=on_partial, vocabulary_id=vocab)
    ctx = tts_context(voice)
    if not ctx:
        raise CallError("实时通话需要阿里云复刻音色(给这个联系人选一个阿里云音色)")
    STATE["phase"] = "preparing"
    t = time.time()
    opener_text = brain.opener()
    opener_pcm = synth_pcm(opener_text, voice, out_dir)
    fillers = [synth_pcm(f, voice, out_dir) for f in FILLERS]
    log("prepared", secs=round(time.time() - t, 2), opener=opener_text)

    if direction == "out":
        STATE["phase"] = "dialing"
        if not SEND_LOCK.acquire(timeout=30):
            return {"result": "error", "error": "界面被占用(正在发消息),稍后再试"}
        try:
            if not ensure_chat_open(chat, display, log, out_dir):
                return {"result": "aborted", "error": "打不开和对方的聊天(没核对上标题),没有拨号"}
            before = set(wechat_windows())
            ears.start()
            if not dial(before, log, out_dir):
                ears.stop()
                return {"result": "aborted", "error": "拨号没成功(没出现通话窗口),截图在通话记录目录里"}
            log("dialed", chat=chat)
        finally:
            SEND_LOCK.release()
        STATE["phase"] = "ringing"
        t_dial = time.time()
        cw = CallWindow(before, log, out_dir)
        state = "none"
        while time.time() - t_dial < RING_TIMEOUT and not _stop.is_set():
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
            log("not_connected", state=state, stopped=_stop.is_set())
            if state in ("ringing", "none"):
                cw.hangup()
            ears.stop()
            return {"result": "declined" if state in ("ended", "closed") else "no_answer"}
    else:
        cw = incoming_window
        ears.start()

    route_wechat_streams(log)
    t_conn = time.time()
    STATE.update(phase="connected", connected_at=t_conn)
    log("connected")
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
        pending.clear()
        new_speech.clear()
    log("speak_opener", text=opener_text, waited=round(time.time() - t_conn, 2))
    _say_line("ai", opener_text)
    speaker.play_pcm(opener_pcm, 24000, opener_text)

    hang, turns, why = False, 0, ""
    while not hang and time.time() - t_conn < max_call:
        if _stop.is_set():
            why = "manual"
            break
        if time.time() - getattr(ears, "_lvl_t", 0) > 5:
            ears._lvl_t = time.time()
            log("level", remote_rms=int(ears.level))
        if not new_speech.wait(timeout=1.0):
            if int(time.time()) % 3 == 0:
                st, _ = cw.state()
                if st in ("closed", "ended"):
                    log("remote_hangup", state=st)
                    why = "remote"
                    break
            if time.time() - max(last_heard[0], speaker.until, t_conn) > SILENCE_HANGUP:
                say("你那边好像没声音了,那我先挂啦,晚点再聊。", voice, speaker, log, out_dir)
                hang, why = True, "silence"
            continue
        while time.time() - last_heard[0] < 0.2:
            time.sleep(0.05)
        with plock:
            batch, pending[:] = list(pending), []
            new_speech.clear()
        if not batch:
            continue
        heard = "".join(x[0] for x in batch)
        t_end = batch[-1][1]
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
                if (new_speech.is_set() and "first_audio" not in marks and attempt < 2) or _stop.is_set():
                    cancel.set(); speaker.stop()
                    th.join(3)
                    if _stop.is_set():
                        break
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
            _say_line("them", heard)
            if said:
                _say_line("ai", said + ("……" if speaker.killed else ""))
            turns += 1
            hang = bool(out.get("hangup")) and not speaker.killed
            if hang:
                why = "goodbye"
            rel = lambda k: round(marks[k] - t_end, 2) if k in marks else None  # noqa: E731
            log("turn", heard=heard, reply=said, hangup=hang, killed=speaker.killed, attempt=attempt,
                filler=rel("filler"), first_token=round(out["first_token"] - t_end, 2) if "first_token" in out else None,
                first_audio=rel("first_audio"), done=round(time.time() - t_end, 2), tts_error=marks.get("tts_error"))
            break
    stop_all.set()
    if why != "remote":
        if why == "manual":
            speaker.stop()
        time.sleep(0.8)
        cw.hangup()
    ears.stop()
    secs = round(time.time() - t_conn, 1)
    log("call_end", secs=secs, why=why or "max_time", turns=turns)
    return {"result": "connected", "connected": True, "secs": secs, "turns": turns, "why": why or "max_time"}


# ---------------- 自动接听 ----------------

INCOMING_RE = re.compile(r"邀请你|语音通话邀请|来电|接听")
_watcher = {"started": False}


def _incoming_window(known):
    """新出现的通话窗口,且 OCR 里有「邀请你…通话 / 接听」→ (窗口, OCR 文字)。"""
    for w, info in wechat_windows().items():
        if w in known or info["box"][2] < 200:
            continue
        p = f"/tmp/vc_incoming_{w}.png"
        shot(p)
        text = ocr(p, info["box"]).replace(" ", "")
        if INCOMING_RE.search(text):
            return w, info, text, p
    return None


def _click_text(png, box, words):
    """在截图的 box 区域里找含 words 之一的文字,点它上方的按钮(按钮图标在文字上面)。"""
    x0, y0, w, h = box
    for line in sh("tesseract", png, "stdout", "-l", "chi_sim", "--psm", "11", "tsv", timeout=20).splitlines()[1:]:
        c = line.split("\t")
        if len(c) == 12 and any(k in c[11] for k in words):
            bx, by = int(c[6]) + int(c[8]) // 2, int(c[7]) - 30
            if x0 <= bx <= x0 + w and y0 <= by <= y0 + h:
                click(bx, by)
                return bx, by
    return None


def watch_once(known):
    """看一眼有没有来电;名单里的人打来就接听并开始对话(在新线程里)。返回是否处理了来电。"""
    s = load_settings()
    if not s.get("auto_answer") or STATE.get("busy"):
        return False
    hit = _incoming_window(known)
    if not hit:
        return False
    w, info, text, png = hit
    caller = next((x for x in s.get("allow") or [] if name_matches(x.get("name", ""), text)), None)
    known.add(w)
    if not caller:
        print(f"[语音通话] 来电不在名单里,不接:{text[:40]}", flush=True)
        return False
    if not _lock.acquire(blocking=False):
        return False
    call_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out_dir = os.path.join(_calls_dir(), call_id)
    _stop.clear()
    STATE.clear()
    STATE.update(busy=True, id=call_id, direction="in", chat=caller["username"], name=caller.get("name"),
                 purpose="", source="incoming", phase="answering", started=time.time(), transcript=[])

    def run():
        res = {}
        try:
            log = Log(out_dir)
            log("incoming", ocr=text[:80], window=info)
            audio_setup(log)                           # 接听前把默认设备设好,微信新开的流直接走对
            pos = _click_text(png, info["box"], ("接听",))
            if not pos:
                x, y, ww, hh = info["box"]
                pos = (x + ww * 3 // 4, y + hh - 90)      # 接听一般在右下(绿色)
                click(*pos)
            log("answer_click", pos=pos)
            time.sleep(1.5)
            cw = CallWindow(set(), log, out_dir)
            cw.win = (w, info)
            res = run_call(caller["username"], caller.get("name"), out_dir, direction="in", incoming_window=cw)
        except Exception as e:  # noqa: BLE001
            res = {"result": "error", "error": f"{type(e).__name__}: {e}"[:200]}
        finally:
            _finish(res, None)
    threading.Thread(target=run, daemon=True, name="voice-call-in").start()
    return True


def start_watcher(interval=2.0):
    """后台盯来电(只在开了自动接听时才截图 OCR)。"""
    if _watcher["started"]:
        return
    _watcher["started"] = True

    def loop():
        known = set()
        while True:
            try:
                if load_settings().get("auto_answer") and not STATE.get("busy"):
                    cur = set(wechat_windows())
                    known &= cur                       # 关掉的窗口从 known 里去掉
                    if not known:
                        known |= {w for w, i in wechat_windows().items() if i["name"] != "语音聊天"}
                    watch_once(known)
            except Exception as e:  # noqa: BLE001
                print(f"[语音通话] 来电检测出错:{e}", flush=True)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="voice-call-watch").start()


# ---------------- 自测 ----------------

def selftest_turns(out_dir, chat="wxid_vn2w1t3doieu22", purpose=""):
    """不打电话:角色卡 + 三轮流式回复(声音播进 vmic,没人听),看首字/首音时间。"""
    log = Log(out_dir)
    brain, voice, _ = setup_brain(chat, log, purpose=purpose)
    audio_setup(log)
    ctx, spk = tts_context(voice), Speaker(log)
    log("opener", text=brain.opener())
    for heard in ["喂？", "好的我知道了", "好啦拜拜"]:
        t_end, out, marks = time.time(), {}, {}
        h = spk.open_stream(ctx, marks)
        said = spk.stream(brain.stream(heard, out, threading.Event()), h)
        brain.commit(heard, said)
        log("turn", heard=heard, reply=said, hangup=out.get("hangup"),
            first_token=round(out["first_token"] - t_end, 2), first_audio=round(marks["first_audio"] - t_end, 2))
