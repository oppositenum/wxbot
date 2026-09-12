"""Media evidence for replies. No UI interaction, key refresh or feature activation."""
import io
import json
from dataclasses import dataclass

from core import imgdec, media, llm


HONESTY = (
    "\n【媒体真实性】只有标注已解析的媒体才有可引用的内容；视频封面不是完整视频。"
    "标注未读取的媒体内容未知，禁止声称看过/听懂，禁止描述、推测具体场景或声音。"
    "同批有文字就正常回应文字，必要时简短说明媒体没读到；不要向用户暴露内部状态、密钥或接口细节。"
)
LABELS = {3: "图片", 34: "语音", 43: "视频封面"}
VISION_PROMPT = (
    '判断图片是否可读，只输出JSON。可读时返回'
    '{"status":"success","description":"简短、客观的可见内容描述"}；'
    '看不到或无法识别时返回{"status":"unreadable","description":""}。'
    '只陈述实际可见内容，不推测画外情节，不把无法识别写成成功。'
)


@dataclass(frozen=True)
class Result:
    status: str
    kind: str
    text: str = ""

    def context(self):
        if self.status == "success":
            return f"[{self.kind}已解析：{self.text}]"
        return f"[{self.kind}未读取：内容未知，不得描述或猜测]"


def unavailable_context(msg):
    """For proactive paths: reuse evidence only; never initiate another paid request."""
    native = native_voice(msg)
    return (native or Result("not_read", LABELS[msg["type"]])).context()


def _failure_reason(reason):
    reason = str(reason or "")
    if reason.startswith("no-img-key"):
        return "missing_key"
    if reason.startswith(("decode", "not-silk", "empty-voice")):
        return "decode_failed"
    if reason.startswith("no-silk-decoder"):
        return "decoder_unavailable"
    return "file_unavailable"


def native_voice(msg):
    text = msg.get("voice_transcript")
    if (msg.get("type") == 34 and msg.get("voice_transcript_source") == "wechat_packed_v1"
            and isinstance(text, str) and text.strip()):
        return Result("success", "语音", text.strip())
    return None


def read(chat, msg, cfg):
    native = native_voice(msg)
    if native:
        return native
    kind = LABELS[msg["type"]]
    t = msg["type"]
    if t == 34 and not cfg.get("enable_stt"):
        return Result("disabled", kind)
    try:
        getter = {3: imgdec.get_msg_image, 34: media.get_msg_voice,
                  43: media.get_msg_video_thumb}[t]
        data, mime = getter(chat, msg.get("local_id"))
    except (OSError, TypeError, ValueError):
        return Result("file_unavailable", kind)
    except Exception:
        return Result("decode_failed", kind)
    if not data:
        return Result(_failure_reason(mime), kind)
    if t != 34:
        try:
            from PIL import Image
        except ImportError:
            return Result("decoder_unavailable", kind)
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.load()  # reject truncated/corrupt bytes, not merely a valid header
                mime = Image.MIME.get(im.format)
            if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                return Result("decode_failed", kind)
        except Exception:
            return Result("decode_failed", kind)
    try:
        if t == 34:
            filename = "voice.wav" if mime == "audio/wav" else "voice.mp3"
            text = llm.transcribe(data, filename=filename, content_type=mime,
                                  cfg=cfg, strict=True)
        else:
            text = llm.describe_image(data, media_type=mime, prompt=VISION_PROMPT, cfg=cfg, strict=True)
    except llm.CapabilityError:
        return Result("not_configured", kind)
    except Exception:
        return Result("api_failed", kind)
    if not isinstance(text, str) or not text.strip() or text.strip().lower() in ("none", "null"):
        return Result("invalid_result", kind)
    if t != 34:
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            return Result("invalid_result", kind)
        if not isinstance(payload, dict):
            return Result("invalid_result", kind)
        if payload.get("status") == "unreadable":
            return Result("unreadable", kind)
        text = payload.get("description")
        if payload.get("status") != "success" or not isinstance(text, str) or not text.strip():
            return Result("invalid_result", kind)
    return Result("success", kind, text.strip())


def failed_reply(results):
    kinds = list(dict.fromkeys(r.kind for r in results))
    if kinds == ["语音"]:
        return "这条语音我没能读取，麻烦用文字说一下。"
    return "这次的" + "、".join(kinds) + "我没能读取，麻烦重发一下，或者用文字描述一下。"
