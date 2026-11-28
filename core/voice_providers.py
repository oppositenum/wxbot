"""复刻音色的语音厂商:阿里云百炼 CosyVoice / MiniMax / 火山引擎豆包 / ElevenLabs。

每家实现三件事:enroll(用样本复刻出音色 ID)、synth(用音色合成 mp3 字节)、delete(删掉音色)。
错误统一成 ProviderError(kind, message),kind 决定 core/voice_keys.run 要不要换 key:
  auth / exhausted / rate  → 换下一个 key(并记下这个 key 的状态)
  voice_missing            → 这个 key 下音色不存在(被厂商删了),core/tts 会用样本重新复刻再试一次
  bad_input / other        → 直接报错

接口细节都来自各家官方文档(2026-11 查阅),写在每个类的注释里。只用标准库 urllib。
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

TIMEOUT = 90


class ProviderError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _http(method, url, *, headers=None, data=None, timeout=TIMEOUT):
    """返回 (状态码, 响应字节, 响应头)。网络错误抛 ProviderError('other')。"""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError("other", f"连不上:{getattr(e, 'reason', e)}")


def _json(body):
    try:
        return json.loads(body.decode("utf-8", "ignore") or "{}")
    except ValueError:
        return {}


def _multipart(fields, files):
    """fields={名: 值}, files=[(字段名, 文件名, 字节, mime)] → (body, content_type)。"""
    b = "----wxbot" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out += [f"--{b}\r\n".encode(), f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode(),
                str(v).encode(), b"\r\n"]
    for name, fn, data, mime in files:
        out += [f"--{b}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(), data, b"\r\n"]
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), "multipart/form-data; boundary=" + b


def _voice_name(prefix="wxbot"):
    """各家对自定义音色 ID 的共同要求:字母开头、8 位以上、只含字母数字下划线、不以 _ 结尾。"""
    return f"{prefix}{time.strftime('%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"


class Provider:
    id = ""
    label = ""
    note = ""
    models = []            # [(模型 id, 显示名)]
    default_model = ""
    model_bound = False    # 复刻出来的音色是否绑定模型(换模型要重新复刻)
    auto_enroll_default = True   # 换到没有这个音色的 key 时,是否自动用样本复刻
    clone_opts = []        # 复刻时额外可填的参数 [(名, 说明)]

    def meta(self):
        return {"id": self.id, "label": self.label, "note": self.note, "models": self.models,
                "default_model": self.default_model, "model_bound": self.model_bound,
                "auto_enroll_default": self.auto_enroll_default, "clone_opts": self.clone_opts}

    def enroll(self, key, sample, name, model, opts=None):
        raise NotImplementedError

    def synth(self, key, text, voice_id, model):
        raise NotImplementedError

    def delete(self, key, voice_id):
        pass


class Aliyun(Provider):
    """阿里云百炼 CosyVoice 声音复刻。
    复刻:POST https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization
         {"model":"voice-enrollment","input":{"action":"create_voice","target_model":模型,"prefix":..,"url":data URI}}
    音色绑定 target_model;创建免费,每账号上限 1000 个。合成用 dashscope SDK(按模型计费)。
    错误:401 InvalidApiKey;403 Arrearage(欠费)/AccessDenied;429 Throttling.RateQuota 限流、
         Throttling.AllocationQuota / insufficient_quota / AllocationQuota.FreeTierOnly 额度。"""
    id = "aliyun"
    label = "阿里云百炼(CosyVoice)"
    note = "创建音色免费,合成按模型计费;音色绑定模型,换模型会自动用录音重新复刻"
    models = [("cosyvoice-v3.5-plus", "cosyvoice-v3.5-plus"), ("cosyvoice-v3.5-flash", "cosyvoice-v3.5-flash"),
              ("cosyvoice-v3-plus", "cosyvoice-v3-plus"), ("cosyvoice-v3-flash", "cosyvoice-v3-flash"),
              ("cosyvoice-v2", "cosyvoice-v2")]
    default_model = "cosyvoice-v3.5-plus"
    model_bound = True
    API = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"

    @staticmethod
    def classify(status, code, message):
        text = f"{code} {message}"
        if status == 401 or "InvalidApiKey" in text or "invalid_api_key" in text:
            return "auth"
        if "Arrearage" in text or "insufficient_quota" in text or "FreeTierOnly" in text or "good standing" in text:
            return "exhausted"
        if "Throttling.AllocationQuota" in text:
            return "bad_input"       # 音色数达上限:换 key 也一样(同账号),要删音色
        if status == 429 or "Throttling" in text:
            return "rate"
        if status == 403 or "AccessDenied" in text:
            return "auth"
        if "NotFound" in text or "not exist" in text.lower():
            return "voice_missing"
        if status == 400:
            return "bad_input"
        return "other"

    def _call(self, key, inp):
        body = json.dumps({"model": "voice-enrollment", "input": inp}).encode()
        status, raw, _h = _http("POST", self.API, data=body, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        d = _json(raw)
        if status != 200:
            code, msg = d.get("code") or "", d.get("message") or raw[:200].decode("utf-8", "ignore")
            raise ProviderError(self.classify(status, code, msg), f"百炼 {status} {code}:{msg}")
        return d

    def enroll(self, key, sample, name, model, opts=None):
        if os.path.getsize(sample) > 10 * 1024 * 1024:
            raise ProviderError("bad_input", "样本超过 10MB")
        data = base64.b64encode(open(sample, "rb").read()).decode()
        prefix = re.sub(r"[^a-z0-9]", "", (name or "wxbot").lower())[:9] or "wxbot"
        d = self._call(key, {"action": "create_voice", "target_model": model or self.default_model,
                             "prefix": prefix, "url": "data:audio/wav;base64," + data})
        vid = (d.get("output") or {}).get("voice_id")
        if not vid:
            raise ProviderError("other", f"百炼没返回 voice_id:{str(d)[:200]}")
        return vid

    def synth(self, key, text, voice_id, model):
        try:
            import dashscope
        except ImportError:
            raise ProviderError("other", "没有安装 dashscope(python3 -m pip install dashscope)")
        import warnings
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                client = dashscope.Client(api_key=key)
                audio = client.audio.tts.synthesize(text, model=model or self.default_model, voice=voice_id,
                                                    format="mp3", sample_rate=24000)
        except Exception as e:  # noqa: BLE001 SDK 把 HTTP 错误包成各种异常,按文字归类
            msg = str(e)
            status = int(m.group(1)) if (m := re.search(r"\b(40[0-9]|429|5\d\d)\b", msg)) else 0
            raise ProviderError(self.classify(status, "", msg), f"百炼合成失败:{msg[:200]}")
        if not audio:
            raise ProviderError("other", "百炼没有返回音频")
        return audio

    def delete(self, key, voice_id):
        self._call(key, {"action": "delete_voice", "voice_id": voice_id})


class MiniMax(Provider):
    """MiniMax 快速复刻(国内站 api.minimax.cn,Bearer 鉴权)。
    1) POST /v1/files/upload multipart purpose=voice_clone,file(mp3/m4a/wav,10 秒~5 分钟,≤20MB)→ file.file_id
    2) POST /v1/voice_clone {"file_id": int, "voice_id": 自定义(字母开头,8~256 位)} → 复刻
    3) POST /v1/t2a_v2 {"model","text","voice_setting":{"voice_id"},"audio_setting":{...},"output_format":"hex"}
       → data.audio(hex)
    音色不绑定模型。复刻的音色 7 天内没正式调用会被删除(这里会在合成时发现并用样本重新复刻)。
    要先在 MiniMax 完成实名认证(否则 2038)。base_resp.status_code:1002/1039/1041/1045 限流,
    1004 鉴权失败,1008 余额不足,2038 无复刻权限。"""
    id = "minimax"
    label = "MiniMax"
    note = "需先实名认证;复刻音色 7 天不用会被删除(会自动重新复刻);首次合成可能有音色解锁费,以 MiniMax 计费页为准"
    models = [("speech-2.8-hd", "speech-2.8-hd"), ("speech-2.8-turbo", "speech-2.8-turbo"),
              ("speech-2.6-hd", "speech-2.6-hd"), ("speech-2.6-turbo", "speech-2.6-turbo"),
              ("speech-02-hd", "speech-02-hd"), ("speech-02-turbo", "speech-02-turbo")]
    default_model = "speech-2.8-hd"
    BASE = os.environ.get("WXBOT_MINIMAX_BASE", "https://api.minimax.cn")

    @staticmethod
    def classify(code, message):
        if code in (1002, 1039, 1041, 1045):
            return "rate"
        if code == 1004 or code == 2038:
            return "auth"
        if code == 1008:
            return "exhausted"
        low = (message or "").lower()
        if "voice" in low and ("not exist" in low or "not found" in low or "invalid voice" in low):
            return "voice_missing"
        if code in (2013, 1042, 1043, 1044, 1026, 1027, 1040):
            return "bad_input"
        return "other"

    def _check(self, status, raw, what):
        d = _json(raw)
        br = d.get("base_resp") or {}
        code = br.get("status_code")
        if status == 401:
            raise ProviderError("auth", f"MiniMax {what}:HTTP 401")
        if status != 200 or code not in (0, None):
            code = code if code is not None else status
            raise ProviderError(self.classify(code, br.get("status_msg") or ""),
                                f"MiniMax {what} {code}:{br.get('status_msg') or raw[:200].decode('utf-8', 'ignore')}")
        return d

    def enroll(self, key, sample, name, model, opts=None):
        auth = {"Authorization": "Bearer " + key}
        body, ctype = _multipart({"purpose": "voice_clone"},
                                 [("file", os.path.basename(sample), open(sample, "rb").read(), "audio/wav")])
        status, raw, _h = _http("POST", self.BASE + "/v1/files/upload", data=body,
                                headers=dict(auth, **{"Content-Type": ctype}))
        fid = (self._check(status, raw, "上传").get("file") or {}).get("file_id")
        if not fid:
            raise ProviderError("other", "MiniMax 上传没返回 file_id")
        vid = _voice_name("wxbot")
        status, raw, _h = _http("POST", self.BASE + "/v1/voice_clone",
                                data=json.dumps({"file_id": int(fid), "voice_id": vid}).encode(),
                                headers=dict(auth, **{"Content-Type": "application/json"}))
        self._check(status, raw, "复刻")
        return vid

    def synth(self, key, text, voice_id, model):
        body = {"model": model or self.default_model, "text": text, "stream": False,
                "voice_setting": {"voice_id": voice_id, "speed": 1, "vol": 1, "pitch": 0},
                "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
                "output_format": "hex"}
        status, raw, _h = _http("POST", self.BASE + "/v1/t2a_v2", data=json.dumps(body).encode(),
                                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        d = self._check(status, raw, "合成")
        audio = (d.get("data") or {}).get("audio") or ""
        if not audio:
            raise ProviderError("other", "MiniMax 没有返回音频")
        return bytes.fromhex(audio)


class Volcengine(Provider):
    """火山引擎豆包语音(新版控制台 API Key,头 X-Api-Key)。
    复刻:POST https://openspeech.bytedance.com/api/v3/tts/voice_clone
         {"speaker_id": 已购音色 S_xxx 或固定值 "custom_speaker_id", "custom_speaker_id": 自定义名(后付费),
          "audio": {"data": base64, "format": "wav"}, "language": 0}
         训练可能要一会儿:POST /api/v3/tts/get_voice 查 status,2/4 才能合成。
    合成:POST /api/v3/tts/unidirectional,头 X-Api-Resource-Id = seed-icl-2.0(复刻 2.0)/ seed-icl-1.0,
         {"user":{"uid"},"req_params":{"text","speaker","audio_params":{"format":"mp3","sample_rate":24000}}}
         返回逐行 JSON,data 是 base64 音频分块,code=20000000 结束。
    计费注意:后付费自定义音色首次合成即「转正」并收音色槽位费,所以换 key 自动复刻默认关闭。
    错误:文档只列了 45000000(音色鉴权失败/并发超限)、55000000;余额/额度类按 HTTP 状态和信息里的关键词判断。"""
    id = "volcengine"
    label = "火山引擎(豆包声音复刻)"
    note = "后付费音色首次合成会收音色槽位费;也可以填控制台买的 S_ 开头音色号。换 key 自动复刻默认关闭"
    models = [("seed-icl-2.0", "声音复刻 2.0"), ("seed-icl-1.0", "声音复刻 1.0")]
    default_model = "seed-icl-2.0"
    auto_enroll_default = False
    clone_opts = [("speaker_id", "已购音色号(S_ 开头,可留空=后付费自定义音色)")]
    BASE = "https://openspeech.bytedance.com"

    @staticmethod
    def classify(status, code, message):
        low = (message or "").lower()
        if "concurrency" in low or status == 429 or "rate" in low and "limit" in low:
            return "rate"
        if any(w in low for w in ("balance", "insufficient", "arrear", "overdue", "quota exceeded")) or \
                any(w in (message or "") for w in ("余额", "欠费", "额度")):
            return "exhausted"
        if "speaker permission denied" in low or "speaker" in low and "not found" in low:
            return "voice_missing"
        if status in (401, 403) or "unauthorized" in low or "invalid api key" in low or "api key" in low:
            return "auth"
        if status == 400:
            return "bad_input"
        return "other"

    def _headers(self, key, extra=None):
        h = {"X-Api-Key": key, "X-Api-Request-Id": str(uuid.uuid4()), "Content-Type": "application/json"}
        h.update(extra or {})
        return h

    def _post(self, key, path, body, what, extra=None):
        status, raw, _h = _http("POST", self.BASE + path, data=json.dumps(body).encode(),
                                headers=self._headers(key, extra))
        d = _json(raw)
        if status != 200:
            msg = d.get("message") or raw[:200].decode("utf-8", "ignore")
            raise ProviderError(self.classify(status, d.get("code"), msg), f"火山引擎 {what} {status}:{msg}")
        return d

    def _ident(self, voice_id):
        if voice_id.startswith("S_"):
            return {"speaker_id": voice_id}
        return {"speaker_id": "custom_speaker_id", "custom_speaker_id": voice_id}

    def enroll(self, key, sample, name, model, opts=None):
        if os.path.getsize(sample) > 10 * 1024 * 1024:
            raise ProviderError("bad_input", "样本超过 10MB")
        slot = ((opts or {}).get("speaker_id") or "").strip()
        vid = slot or _voice_name("wxbot")
        body = dict(self._ident(vid), audio={"data": base64.b64encode(open(sample, "rb").read()).decode(),
                                              "format": "wav"}, language=0)
        d = self._post(key, "/api/v3/tts/voice_clone", body, "复刻")
        status = d.get("status")
        deadline = time.time() + 120
        while status not in (2, 4):
            if status == 3:
                raise ProviderError("bad_input", "火山引擎复刻失败(样本质量或时长不合格)")
            if time.time() > deadline:
                raise ProviderError("other", "火山引擎复刻超时,稍后在后台重试")
            time.sleep(3)
            status = self._post(key, "/api/v3/tts/get_voice", self._ident(vid), "查询").get("status")
        return vid

    def synth(self, key, text, voice_id, model):
        body = {"user": {"uid": "wxbot"},
                "req_params": {"text": text, "speaker": voice_id,
                               "audio_params": {"format": "mp3", "sample_rate": 24000}}}
        status, raw, _h = _http("POST", self.BASE + "/api/v3/tts/unidirectional", data=json.dumps(body).encode(),
                                headers=self._headers(key, {"X-Api-Resource-Id": model or self.default_model}))
        chunks, err = [], None
        for line in raw.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line.startswith("{"):
                continue
            d = _json(line.encode())
            code = d.get("code")
            if d.get("data"):
                chunks.append(base64.b64decode(d["data"]))
            if code not in (0, 20000000, None):
                err = (code, d.get("message") or "")
        if status != 200 or err:
            code, msg = err or (status, raw[:200].decode("utf-8", "ignore"))
            raise ProviderError(self.classify(status, code, msg), f"火山引擎合成 {code}:{msg}")
        if not chunks:
            raise ProviderError("other", "火山引擎没有返回音频")
        return b"".join(chunks)


class ElevenLabs(Provider):
    """ElevenLabs 即时复刻(头 xi-api-key,国内要走代理)。
    复刻:POST https://api.elevenlabs.io/v1/voices/add multipart name + files → voice_id
    合成:POST /v1/text-to-speech/{voice_id}?output_format=mp3_44100_128 {"text","model_id"} → mp3
    删除:DELETE /v1/voices/{voice_id}
    错误:401 key 无效,402 额度不足/要付费,429 限流或并发超限,404 音色不存在。"""
    id = "elevenlabs"
    label = "ElevenLabs"
    note = "按订阅额度计费,复刻音色数受套餐限制;容器要能走代理访问 api.elevenlabs.io"
    models = [("eleven_v4", "eleven_v4"), ("eleven_v4_turbo", "eleven_v4_turbo"),
              ("eleven_multilingual_v2", "eleven_multilingual_v2"), ("eleven_flash_v2_5", "eleven_flash_v2_5"),
              ("eleven_v3", "eleven_v3")]
    default_model = "eleven_multilingual_v2"
    BASE = "https://api.elevenlabs.io"

    @staticmethod
    def classify(status, message):
        if status == 401:
            return "auth"
        if status == 402 or "quota_exceeded" in (message or "") or "insufficient_credits" in (message or ""):
            return "exhausted"
        if status == 429:
            return "rate"
        if status == 404:
            return "voice_missing"
        if status in (400, 422):
            return "bad_input"
        return "other"

    def _err(self, status, raw, what):
        msg = raw[:300].decode("utf-8", "ignore")
        return ProviderError(self.classify(status, msg), f"ElevenLabs {what} {status}:{msg[:200]}")

    def enroll(self, key, sample, name, model, opts=None):
        body, ctype = _multipart({"name": (name or "wxbot")[:50]},
                                 [("files", os.path.basename(sample), open(sample, "rb").read(), "audio/wav")])
        status, raw, _h = _http("POST", self.BASE + "/v1/voices/add", data=body,
                                headers={"xi-api-key": key, "Content-Type": ctype})
        if status != 200:
            raise self._err(status, raw, "复刻")
        vid = _json(raw).get("voice_id")
        if not vid:
            raise ProviderError("other", "ElevenLabs 没返回 voice_id")
        return vid

    def synth(self, key, text, voice_id, model):
        status, raw, _h = _http(
            "POST", f"{self.BASE}/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
            data=json.dumps({"text": text, "model_id": model or self.default_model}).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
        if status != 200:
            raise self._err(status, raw, "合成")
        return raw

    def delete(self, key, voice_id):
        status, raw, _h = _http("DELETE", f"{self.BASE}/v1/voices/{voice_id}", headers={"xi-api-key": key})
        if status not in (200, 404):
            raise self._err(status, raw, "删除")


PROVIDERS = {p.id: p for p in (Aliyun(), MiniMax(), Volcengine(), ElevenLabs())}


def get(pid):
    p = PROVIDERS.get(pid)
    if not p:
        raise ProviderError("bad_input", f"不支持的厂商:{pid}")
    return p


def catalog():
    return [p.meta() for p in PROVIDERS.values()]
