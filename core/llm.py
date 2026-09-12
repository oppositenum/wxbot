"""统一 LLM 客户端：支持 Claude(Anthropic) 与 GPT(OpenAI)，走可选代理。
仅用标准库 urllib，避免额外依赖。配置读 llm_config.json。
"""
import json
import logging
import threading
import uuid
from collections import deque
import os
import sys
import urllib.request
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

# 可用 WXBOT_LLM_CONFIG 指定(容器部署时放到持久化卷里)
CONFIG_FILE = os.environ.get("WXBOT_LLM_CONFIG") or \
    os.path.join(config.PROJECT_DIR, "llm_config.json")

# 有些中转在 Cloudflare 后面，按 UA 拦截(error 1010)，用浏览器 UA 绕过
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def load_cfg():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _default_model(provider):
    return "claude-sonnet-5" if provider == "claude" else "gpt-4o"


def creds(cfg, provider):
    """取某 provider 的凭据 (base_url, api_key, model)。

    支持【两套独立中转】：cfg 里 cfg['claude']/cfg['gpt'] 各含 base_url/api_key/model。
    没有独立配置时退回扁平旧字段(单中转)：base_url/api_key/(model 或 gpt_model)。
    """
    sub = cfg.get(provider)
    if isinstance(sub, dict) and (sub.get("api_key") or sub.get("base_url")):
        base = (sub.get("base_url") or cfg.get("base_url") or "").rstrip("/")
        key = sub.get("api_key") or os.environ.get("WXBOT_LLM_KEY", "")
        model = sub.get("model") or _default_model(provider)
        return base, key, model
    base = (cfg.get("base_url") or "").rstrip("/")
    key = cfg.get("api_key") or os.environ.get("WXBOT_LLM_KEY", "")
    if provider == "claude":
        model = cfg.get("model", "claude-sonnet-5")
    else:
        model = cfg.get("gpt_model") or cfg.get("model") or "gpt-4o"
    return base, key, model


# Adapter protocol support, not a claim about any gateway model's capabilities.
TOOL_PROVIDERS = frozenset({"claude", "gpt"})
_ROUTE_LOG = deque(maxlen=100)
_ROUTE_LOCK = threading.Lock()


class CapabilityError(RuntimeError):
    pass


def route_info(cfg, purpose="chat"):
    independent = purpose == "tools" and bool(cfg.get("tools_provider") or cfg.get("tools_model"))
    provider = (cfg.get("tools_provider") if purpose == "tools" else None) or cfg.get("provider", "claude")
    _, _, model = creds(cfg, provider)
    if purpose == "tools":
        model = cfg.get("tools_model") or model
    return {"provider": provider, "model": model, "independent": bool(independent),
            "adapter_supports_tools": provider in TOOL_PROVIDERS}


def route_diagnostics():
    with _ROUTE_LOCK:
        return list(_ROUTE_LOG)


def _request_route(provider, model, phase, request_id):
    # Allowlist only: never headers, endpoint, exception body or private prompt.
    rec = {"request_id": request_id, "phase": phase, "provider": provider, "model": model}
    with _ROUTE_LOCK:
        _ROUTE_LOG.append(rec)
    logging.getLogger(__name__).info("llm_route %s", json.dumps(rec, ensure_ascii=False))


def _opener(proxy):
    if proxy:
        h = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(h)
    return urllib.request.build_opener()


def _post(url, headers, body, proxy, timeout=60, retries=3):
    import time
    import urllib.error
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with _opener(proxy).open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if (e.code >= 500 or e.code == 429) and attempt < retries:
                # 网关抖动(502/503/504)或并发限流(429)重试；429 退避更久
                time.sleep((3.0 if e.code == 429 else 1.5) * (attempt + 1))
                continue
            raise RuntimeError(f"模型接口 HTTP {e.code}；未切换 provider/model") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:                         # 超时/连接抖动重试
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError("模型接口连接失败；未切换 provider/model") from None


def _endpoint(base, tail):
    """拼接端点，避免重复 /v1。tail 如 'messages' 或 'chat/completions'。
    base 可填到域名(补 /v1/<tail>)、带 /v1(补 /<tail>)、或完整端点(原样)。"""
    base = base.rstrip("/")
    if base.endswith(tail):
        return base
    if base.endswith("/v1"):
        return base + "/" + tail
    return base + "/v1/" + tail


def chat(system, messages, cfg=None):
    """messages: [{"role":"user"/"assistant","content":str}]，返回回复文本。
    provider 决定用 claude 还是 gpt 那套独立中转(见 creds)。"""
    cfg = cfg or load_cfg()
    provider = cfg.get("provider", "claude")
    if provider not in ("claude", "gpt"):
        raise CapabilityError("所选 provider 没有聊天适配器")
    proxy = cfg.get("proxy") or None          # 空=直连(不隐式走系统代理)
    max_tokens = cfg.get("max_tokens", 600)
    temperature = cfg.get("temperature", 0.9)
    base, key, model = creds(cfg, provider)
    if not key:
        raise RuntimeError(f"未配置 {provider} 的 api_key（在 AI 设置里填该套中转）")

    _request_route(provider, model, "chat", uuid.uuid4().hex)
    if provider == "claude":
        body = {"model": model, "max_tokens": max_tokens,
                "system": system, "messages": messages}
        if temperature is not None and cfg.get("send_temperature"):
            body["temperature"] = temperature
        headers = {"content-type": "application/json", "x-api-key": key,
                   "authorization": f"Bearer {key}",   # 中转常用 Bearer
                   "anthropic-version": "2023-06-01", "user-agent": UA}
        url = _endpoint(base or "https://api.anthropic.com", "messages")
        r = _post(url, headers, body, proxy, retries=0) if cfg.get("single_attempt") else _post(url, headers, body, proxy)
        parts = r.get("content", [])
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()

    # OpenAI / GPT 兼容
    url = _endpoint(base or "https://api.openai.com/v1", "chat/completions")
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    body = {"model": model, "max_tokens": max_tokens, "messages": msgs}
    if temperature is not None and cfg.get("send_temperature"):
        body["temperature"] = temperature
    headers = {"content-type": "application/json", "authorization": f"Bearer {key}",
               "user-agent": UA}
    r = _post(url, headers, body, proxy, retries=0) if cfg.get("single_attempt") else _post(url, headers, body, proxy)
    return r["choices"][0]["message"]["content"].strip()


def chat_tools(system, messages, tools, dispatch, cfg=None, max_rounds=6):
    """One explicit route for all tool rounds and finalization; no credential-based fallback.

    tools_provider/tools_model override the main route only when explicitly set.
    GPT uses Chat Completions function calls; Claude uses Messages tool blocks.
    Gateway rejection propagates as a limitation/error, never as another model's reply.
    """
    cfg = load_cfg() if cfg is None else cfg
    route = route_info(cfg, "tools")
    provider, model = route["provider"], route["model"]
    if provider not in TOOL_PROVIDERS:
        raise CapabilityError("所选 provider 的适配器不支持工具调用；请显式配置支持的工具路由")
    base, key, _ = creds(cfg, provider)
    if not key:
        raise CapabilityError("所选工具 provider 未配置凭据；未使用其他 provider")
    proxy = cfg.get("proxy") or None
    rid = uuid.uuid4().hex
    headers = {"content-type": "application/json", "authorization": f"Bearer {key}", "user-agent": UA}
    if provider == "claude":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        url = _endpoint(base or "https://api.anthropic.com", "messages")
        convo = list(messages)
    else:
        url = _endpoint(base or "https://api.openai.com/v1", "chat/completions")
        convo = ([{"role": "system", "content": system}] if system else []) + list(messages)
    allowed = {t["name"] for t in tools}

    def invoke(name, args):
        if name not in allowed or not isinstance(args, dict):
            return "[工具请求无效或未授权]"
        try:
            return str(dispatch(name, args))[:6000]
        except Exception:
            return "[工具执行失败]"       # do not relay exceptions containing private inputs

    turn = 0
    while turn <= max_rounds:
        final = turn == max_rounds
        body = {"model": model, "max_tokens": cfg.get("max_tokens", 800), "messages": convo}
        if cfg.get("send_temperature") and cfg.get("temperature") is not None:
            body["temperature"] = cfg["temperature"]
        if provider == "claude":
            body["system"] = system
            if not final:
                body["tools"] = tools
        elif not final:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t["input_schema"]}} for t in tools]
        _request_route(provider, model, "tools_final" if final else "tools_round", rid)
        r = _post(url, headers, body, proxy)
        if provider == "claude":
            content = r.get("content", [])
            calls = [c for c in content if c.get("type") == "tool_use"]
            text = "".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
            if calls and not final:
                convo.append({"role": "assistant", "content": content})
                convo.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": c["id"],
                     "content": invoke(c.get("name"), c.get("input"))} for c in calls]})
                turn += 1
                continue
        else:
            msg = r["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            text = (msg.get("content") or "").strip()
            if calls and not final:
                convo.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                for c in calls:
                    fn = c.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments", ""))
                    except (ValueError, TypeError):
                        args = None
                    out = invoke(fn.get("name"), args) if c.get("type") == "function" else "[不支持的工具类型]"
                    convo.append({"role": "tool", "tool_call_id": c["id"], "content": out})
                turn += 1
                continue
        if calls:
            raise CapabilityError("工具轮数已用尽，模型仍请求工具；未切换模型或继续执行工具")
        if text:
            _request_route(provider, model, "tools_reply", rid)
            return text
        if final:
            break
        turn = max_rounds  # empty response: one explicit finalization, not repeated tool rounds
    raise CapabilityError("所选工具模型未返回有效最终回复；未切换模型")


def embed(texts, cfg=None):
    """可选：文本向量化。多数中转不支持(404)→返回 None，RAG 退回 FTS5/BM25。"""
    cfg = cfg or load_cfg()
    if not cfg.get("enable_embed"):
        return None
    base = (cfg.get("embed_base_url") or cfg.get("base_url") or "").rstrip("/")
    key = cfg.get("embed_api_key") or cfg.get("api_key") or ""
    model = cfg.get("embed_model", "text-embedding-3-small")
    proxy = cfg.get("proxy") or None
    if not base or not key:
        return None
    try:
        r = _post(_endpoint(base, "embeddings"),
                  {"content-type": "application/json",
                   "authorization": f"Bearer {key}", "user-agent": UA},
                  {"model": model, "input": texts}, proxy)
        return [d["embedding"] for d in r.get("data", [])] or None
    except Exception:  # noqa: BLE001
        return None


def describe_image(img_bytes, media_type="image/jpeg",
                   prompt="用一句中文简短、客观地描述这张图片的主要内容（是什么/在做什么），不要评论、不要猜测。",
                   cfg=None, strict=False):
    """让多模态模型看图并返回一句中文描述。支持 Claude / OpenAI 视觉。
    视觉用哪套中转：cfg['vision_provider'] 指定，缺省跟随主 provider。"""
    import base64
    cfg = cfg or load_cfg()
    provider = cfg.get("vision_provider") or cfg.get("provider", "claude")
    proxy = cfg.get("proxy") or None
    base, key, model0 = creds(cfg, provider)
    if not key:
        if strict:
            raise CapabilityError("媒体接口未配置")
        return None
    b64 = base64.b64encode(img_bytes).decode()
    try:
        if provider == "claude":
            model = cfg.get("vision_model") or model0
            body = {"model": model, "max_tokens": 300, "messages": [{"role": "user",
                    "content": [{"type": "text", "text": prompt},
                                {"type": "image", "source": {"type": "base64",
                                 "media_type": media_type, "data": b64}}]}]}
            headers = {"content-type": "application/json", "x-api-key": key,
                       "authorization": f"Bearer {key}",
                       "anthropic-version": "2023-06-01", "user-agent": UA}
            url = _endpoint(base or "https://api.anthropic.com", "messages")
            r = _post(url, headers, body, proxy)
            return "".join(p.get("text", "") for p in r.get("content", [])
                           if p.get("type") == "text").strip() or None
        # OpenAI 视觉
        model = cfg.get("vision_model") or model0
        url = _endpoint(base or "https://api.openai.com/v1", "chat/completions")
        body = {"model": model, "max_tokens": 300, "messages": [{"role": "user",
                "content": [{"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {
                             "url": f"data:{media_type};base64,{b64}"}}]}]}
        headers = {"content-type": "application/json",
                   "authorization": f"Bearer {key}", "user-agent": UA}
        r = _post(url, headers, body, proxy)
        return r["choices"][0]["message"]["content"].strip() or None
    except Exception:
        if strict:
            raise RuntimeError("媒体接口请求或响应失败") from None
        return None


def transcribe(audio_bytes, filename="voice.mp3", content_type="audio/mpeg", cfg=None, strict=False):
    """语音转文字(whisper 兼容端点)。需在 llm_config.json 里 enable_stt=true 并配置
    stt_base_url/stt_api_key/stt_model。未配置或失败返回 None。"""
    import uuid
    cfg = cfg or load_cfg()
    if not cfg.get("enable_stt"):
        if strict:
            raise CapabilityError("语音转写未启用")
        return None
    base = (cfg.get("stt_base_url") or cfg.get("base_url") or "").rstrip("/")
    key = cfg.get("stt_api_key") or cfg.get("api_key") or ""
    model = cfg.get("stt_model", "whisper-1")
    proxy = cfg.get("proxy") or None
    if not base or not key:
        if strict:
            raise CapabilityError("语音接口未配置")
        return None
    bnd = "----wxbot" + uuid.uuid4().hex
    body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n"
            f"{model}\r\n").encode()
    body += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
             ).encode() + audio_bytes + b"\r\n"
    body += (f"--{bnd}--\r\n").encode()
    url = _endpoint(base, "audio/transcriptions")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "authorization": f"Bearer {key}", "user-agent": UA,
        "content-type": f"multipart/form-data; boundary={bnd}"})
    try:
        with _opener(proxy).open(req, timeout=90) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        return (r.get("text") or "").strip() or None
    except Exception:
        if strict:
            raise RuntimeError("媒体接口请求或响应失败") from None
        return None


def image_creds(cfg):
    """取【画图专用】凭据 (base_url, api_key, model)。

    优先用独立的画图中转 cfg['image'] = {base_url, api_key, model}(推荐：
    画图往往要单独的能画图的 key/中转，别和聊天文本模型混)。
    没配独立块时，退回 gpt 那套凭据 + cfg['image_model']（默认 gpt-image-1）。
    """
    sub = cfg.get("image")
    if isinstance(sub, dict) and (sub.get("api_key") or sub.get("base_url")):
        base = (sub.get("base_url") or "").rstrip("/")
        key = sub.get("api_key") or ""
        model = sub.get("model") or "gpt-image-1"
        return base, key, model
    base, key, _ = creds(cfg, "gpt")
    model = cfg.get("image_model") or "gpt-image-1"
    return base, key, model


def gen_image(prompt, size="1024x1024", cfg=None):
    """文生图：调 OpenAI 兼容的 images/generations，返回图片 bytes(PNG/JPEG)。

    凭据经 image_creds()：优先独立画图中转 cfg['image']，否则退回 gpt 那套 + image_model。
    注意：中转可能【未给该 key 开通图像权限】(403 permission_error)——此时抛 RuntimeError，
    由调用方(draw_image 工具)据此如实告诉对方"画不了/未开通"，绝不静默失败或假装画了。
    """
    import base64
    cfg = cfg or load_cfg()
    proxy = cfg.get("proxy") or None
    base, key, model = image_creds(cfg)
    if not key:
        raise RuntimeError("未配置画图中转的 api_key（llm_config.json 的 image 块或 gpt 块）")
    url = _endpoint(base or "https://api.openai.com/v1", "images/generations")
    headers = {"content-type": "application/json",
               "authorization": f"Bearer {key}", "user-agent": UA}
    quality = "standard"
    if isinstance(cfg.get("image"), dict):
        quality = cfg["image"].get("quality") or quality
    quality = cfg.get("image_quality") or quality
    r = _post(url, headers, {"model": model, "prompt": prompt, "n": 1, "size": size,
                             "quality": quality},
              proxy, timeout=120)
    d = (r.get("data") or [{}])[0]
    if d.get("b64_json"):
        return base64.b64decode(d["b64_json"])
    if d.get("url"):
        req = urllib.request.Request(d["url"], headers={"user-agent": UA})
        with _opener(proxy).open(req, timeout=120) as resp:
            return resp.read()
    raise RuntimeError("图像生成返回为空")


def available():
    cfg = load_cfg()
    provider = cfg.get("provider", "claude")
    return provider in ("claude", "gpt") and bool(creds(cfg, provider)[1])


if __name__ == "__main__":
    print("provider:", load_cfg().get("provider"), "configured:", available())
    if available():
        print(chat("你是一个只会说'喵'的猫。", [{"role": "user", "content": "你好"}]))
