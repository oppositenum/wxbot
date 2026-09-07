"""统一 LLM 客户端：支持 Claude(Anthropic) 与 GPT(OpenAI)，走可选代理。
仅用标准库 urllib，避免额外依赖。配置读 llm_config.json。
"""
import json
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


def _has_provider(cfg, provider):
    sub = cfg.get(provider)
    if isinstance(sub, dict) and sub.get("api_key"):
        return True
    return bool(cfg.get("api_key")) and cfg.get("provider", "claude") == provider


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
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:400]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"HTTP {e.code} @ {url} — {detail or e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:                         # 超时/连接抖动重试
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"连接失败 @ {url} — {e}")


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
    proxy = cfg.get("proxy") or None          # 空=直连(不隐式走系统代理)
    max_tokens = cfg.get("max_tokens", 600)
    temperature = cfg.get("temperature", 0.9)
    base, key, model = creds(cfg, provider)
    if not key:
        raise RuntimeError(f"未配置 {provider} 的 api_key（在 AI 设置里填该套中转）")

    if provider == "claude":
        body = {"model": model, "max_tokens": max_tokens,
                "system": system, "messages": messages}
        if temperature is not None and cfg.get("send_temperature"):
            body["temperature"] = temperature
        headers = {"content-type": "application/json", "x-api-key": key,
                   "authorization": f"Bearer {key}",   # 中转常用 Bearer
                   "anthropic-version": "2023-06-01", "user-agent": UA}
        url = _endpoint(base or "https://api.anthropic.com", "messages")
        r = _post(url, headers, body, proxy)
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
    r = _post(url, headers, body, proxy)
    return r["choices"][0]["message"]["content"].strip()


def chat_tools(system, messages, tools, dispatch, cfg=None, max_rounds=6):
    """多轮工具调用(Claude tools)。dispatch(name, input_dict)->str 执行工具并回结果。

    tools: [{"name","description","input_schema"}]。返回最终回复文本。
    非 Claude provider 时退回无工具的 chat()（中转对 Claude tools 已验证可用）。
    """
    cfg = cfg or load_cfg()
    # 工具调用走 claude 那套(有独立 claude 中转就用它，即使主 provider 是 gpt)
    if cfg.get("provider", "claude") != "claude" and not _has_provider(cfg, "claude"):
        return chat(system, messages, cfg)
    proxy = cfg.get("proxy") or None
    base, key, model = creds(cfg, "claude")
    if not key:
        return chat(system, messages, cfg)
    headers = {"content-type": "application/json", "x-api-key": key,
               "authorization": f"Bearer {key}",
               "anthropic-version": "2023-06-01", "user-agent": UA}
    url = _endpoint(base or "https://api.anthropic.com", "messages")
    convo = list(messages)
    for _ in range(max_rounds):
        body = {"model": model, "max_tokens": cfg.get("max_tokens", 800),
                "system": system, "messages": convo, "tools": tools}
        r = _post(url, headers, body, proxy)
        content = r.get("content", [])
        if r.get("stop_reason") == "tool_use":
            convo.append({"role": "assistant", "content": content})
            results = []
            for blk in content:
                if blk.get("type") == "tool_use":
                    try:
                        out = dispatch(blk.get("name"), blk.get("input") or {})
                    except Exception as e:  # noqa: BLE001
                        out = f"[工具出错] {e}"
                    results.append({"type": "tool_result", "tool_use_id": blk.get("id"),
                                    "content": str(out)[:6000]})
            convo.append({"role": "user", "content": results})
            continue
        return "".join(p.get("text", "") for p in content
                       if p.get("type") == "text").strip()
    # 轮数用尽：再要一次无工具的收尾
    body = {"model": model, "max_tokens": cfg.get("max_tokens", 800),
            "system": system + "\n(请基于已有信息直接给出最终回复。)", "messages": convo}
    r = _post(url, headers, body, proxy)
    return "".join(p.get("text", "") for p in r.get("content", [])
                   if p.get("type") == "text").strip()


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
                   cfg=None):
    """让多模态模型看图并返回一句中文描述。支持 Claude / OpenAI 视觉。
    视觉用哪套中转：cfg['vision_provider'] 指定，缺省跟随主 provider。"""
    import base64
    cfg = cfg or load_cfg()
    provider = cfg.get("vision_provider") or cfg.get("provider", "claude")
    proxy = cfg.get("proxy") or None
    base, key, model0 = creds(cfg, provider)
    if not key:
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
    except Exception:  # noqa: BLE001
        return None


def transcribe(audio_bytes, filename="voice.mp3", content_type="audio/mpeg", cfg=None):
    """语音转文字(whisper 兼容端点)。需在 llm_config.json 里 enable_stt=true 并配置
    stt_base_url/stt_api_key/stt_model。未配置或失败返回 None。"""
    import uuid
    cfg = cfg or load_cfg()
    if not cfg.get("enable_stt"):
        return None
    base = (cfg.get("stt_base_url") or cfg.get("base_url") or "").rstrip("/")
    key = cfg.get("stt_api_key") or cfg.get("api_key") or ""
    model = cfg.get("stt_model", "whisper-1")
    proxy = cfg.get("proxy") or None
    if not base or not key:
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
    except Exception:  # noqa: BLE001
        return None


def available():
    cfg = load_cfg()
    return bool(cfg.get("api_key") or os.environ.get("WXBOT_LLM_KEY")
                or _has_provider(cfg, "claude") or _has_provider(cfg, "gpt"))


if __name__ == "__main__":
    print("provider:", load_cfg().get("provider"), "configured:", available())
    if available():
        print(chat("你是一个只会说'喵'的猫。", [{"role": "user", "content": "你好"}]))
