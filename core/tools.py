"""Agent 工具实现 + Claude 工具规格。

工具：
- web_search   联网搜索(DuckDuckGo HTML，无需 key，走 cfg.proxy)
- search_history 在某会话的历史消息里检索
- get_member_profile 查某人的长期画像(core.memory)
- search_kb    在知识库里检索(core.knowledge, FTS5/BM25)

run(name, inp, ctx) 统一分发；读取需要服务端签发的 ctx["read_access"]，缺少则拒绝。
"""
import html
import re
import sys
import os
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402,F401
from core import read_access

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

SPECS = [
    {"name": "web_search",
     "description": "联网搜索实时/外部信息(新闻、事实、你不知道的东西)。返回若干条标题+摘要。",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "搜索关键词"}}, "required": ["query"]}},
    {"name": "search_history",
     "description": "只在服务端授权的当前微信会话中检索普通文本历史。",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "要查找的内容/关键词"},
         "chat": {"type": "string", "description": "会话wxid，缺省为当前会话"}},
         "required": ["query"]}},
    {"name": "get_member_profile",
     "description": "查某个联系人/群成员的长期画像(已知的身份、偏好、关系等)。",
     "input_schema": {"type": "object", "properties": {
         "name_or_wxid": {"type": "string", "description": "对方的名字或wxid"}},
         "required": ["name_or_wxid"]}},
    {"name": "search_kb",
     "description": "在本地知识库(导入的文档/FAQ)里检索答案。",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "问题/关键词"}}, "required": ["query"]}},
    {"name": "draw_image",
     "description": "文生图并【直接发给当前会话对方】。对方让你画/生成一张图时用它。"
                    "prompt 用具体的画面英文或中文描述(主体/风格/场景)。返回是否成功发出。",
     "input_schema": {"type": "object", "properties": {
         "prompt": {"type": "string", "description": "要画的画面内容描述,越具体越好"}},
         "required": ["prompt"]}},
]


def _proxy_opener(proxy):
    if proxy:
        h = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(h)
    return urllib.request.build_opener()


def web_search(query, cfg=None, k=5):
    cfg = cfg or {}
    proxy = cfg.get("proxy") or None
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _proxy_opener(proxy).open(req, timeout=20) as resp:
            page = resp.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return f"[web_search 失败] {e}"
    # 解析结果块：标题(result__a) + 摘要(result__snippet)
    titles = re.findall(r'result__a"[^>]*>(.*?)</a>', page, re.S)
    snips = re.findall(r'result__snippet"[^>]*>(.*?)</a>', page, re.S)

    def clean(s):
        return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
    items = []
    for i in range(min(k, len(titles))):
        t = clean(titles[i])
        s = clean(snips[i]) if i < len(snips) else ""
        if t:
            items.append(f"{i+1}. {t}\n   {s}")
    return "\n".join(items) if items else "[web_search 无结果]"


def search_history(query, chat, cfg=None, k=8, *, access=None):
    from core import knowledge
    if not read_access.valid(access, chat):
        return "[search_history] 只允许在有效服务端账号及当前会话授权下读取"
    try:
        msgs = read_access.history(access, limit=400)
    except Exception:
        return "[search_history] 历史读取失败"
    # 简单：用知识库的 n-gram 匹配思路做本地打分(不落库)，退回子串匹配
    toks = knowledge._ngramize(query).split()
    scored = []
    for m in msgs:
        c = (m.get("content") or "")
        if not c or c.startswith("["):
            continue
        # 本机器人的定时提醒不进普通历史检索(只命中自己发的;用户提到前缀的消息不受影响)
        # Scheduled records were filtered using the pinned account's files in read_access.history.
        cg = knowledge._ngramize(c)
        hit = sum(cg.count(t) for t in toks) if toks else 0
        if hit == 0 and query in c:
            hit = 1
        if hit:
            who = "我" if m.get("is_self") else (m.get("sender_name") or "对方")
            scored.append((hit, f"{who}: {c}", m.get("create_time")))
    scored.sort(key=lambda x: -x[0])
    import time as _t
    now = _t.time()

    def _tl(ct):
        if not ct:
            return "时间未知"
        d = now - ct
        if d < 3600:
            return f"{int(d//60)}分钟前"
        if d < 86400:
            return f"{int(d//3600)}小时前"
        if d < 86400 * 30:
            return f"{int(d//86400)}天前"
        return _t.strftime("%Y-%m-%d", _t.localtime(ct))
    lines = [f"[{_tl(ct)}] {s}" for _, s, ct in scored[:k]]
    if not read_access.valid(access, chat):
        return "[search_history] 授权账号已变化，拒绝返回数据"
    return "\n".join(lines) if lines else "[search_history 无匹配]"


def get_member_profile(name_or_wxid, cfg=None, scope=None, *, access=None):
    """Only current valid facts of a server-authorized member. Never the aggregate summary."""
    from core import memory
    import time
    if not read_access.valid(access):
        return "[get_member_profile] 缺少有效服务端账号及会话授权"
    actual_scope = memory.scope_of_chat(access.chat)
    if scope is not None and scope != actual_scope:
        return "[get_member_profile] 不允许更改授权作用域"
    matches = {w for w, n in access.names if n == name_or_wxid or w == name_or_wxid}
    if len(matches) != 1:
        return "[get_member_profile] 只能查询当前会话授权的明确成员"
    wid = matches.pop()
    try:
        p = read_access.profile(access, wid)
    except Exception:
        return "[get_member_profile] 画像读取失败"
    now = time.time()
    active = []
    for f in p.get("facts", []):
        if f.get("status") != "active" or f.get("scope") != actual_scope:
            continue
        if f.get("expires_ts") and f["expires_ts"] <= now:
            continue
        text = f.get("text") or ""
        if not text or memory._JUNK_RE.search(text):
            continue
        tl = memory._rel_time(f.get("event_ts"), f.get("recorded_ts"), now)
        active.append(f"· {text} [{tl}]")
    if not read_access.valid(access):
        return "[get_member_profile] 授权账号已变化，拒绝返回数据"
    return "\n".join(active) if active else "[当前作用域没有有效画像事实]"


def search_kb(query, cfg=None, k=5, *, access=None):
    if not read_access.valid(access):
        return "[search_kb] 缺少有效服务端账号及会话授权"
    try:
        hits = read_access.kb(access, query, k)
    except Exception:
        return "[search_kb] 读取失败"
    if not read_access.valid(access):
        return "[search_kb] 授权账号已变化，拒绝返回数据"
    return "\n\n".join(f"[{h['source']}] {h['content']}" for h in hits) if hits else "[知识库无相关内容]"


from core import account_session as sessions, send_ledger

@sessions.task
def draw_image(prompt, ctx=None):
    """文生图 → 存宿主机临时文件 → 发给当前会话对方。
    生成失败(如中转未开通图像权限)返回明确错误串,让模型如实告知对方画不了。"""
    ctx = ctx or {}
    chat = ctx.get("chat")
    cfg = ctx.get("cfg")
    if not read_access.valid(ctx.get("read_access"), chat):
        return "[draw_image] 缺少有效会话授权"
    if not chat:
        return "[draw_image] 无当前会话,图发不出去"
    if not prompt:
        return "[draw_image] 没给画面描述"
    if send_ledger.blocked_by_uncertainty('image', chat):
        return '[上一图片发送结果待核对，已阻止重复生成和发送]'
    from core import sender
    blocked = sender.preflight(chat, kind='image')
    if blocked:
        return '[draw_image] ' + blocked['message']
    from core import llm
    try:
        img = llm.gen_image(prompt, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        return f"[画图失败] {e}"        # 未开通权限/报错 → 模型据此如实说画不了
    sessions.check()
    import tempfile
    fd, path = tempfile.mkstemp(prefix="wxdraw-", suffix=".png")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(img)
        from core import sender
        if not read_access.valid(ctx.get("read_access"), chat):
            return "[draw_image] 授权账号已变化，取消发送"
        disp = ctx.get("display_name") or chat
        r = sender.send_image(disp, path, chat_username=chat)
    except Exception as e:  # noqa: BLE001
        return f"[图片已生成但发送出错] {e}"
    if r.get("ok"):
        return f"[已生成并把图片发给对方成功] 画的是:{prompt}"
    if r.get('status') == 'uncertain':
        return "[图片发送结果待核对，不得再次调用画图或发送来重试，也不能声称图片已送达]"
    return f"[图片已生成但未发送] {r.get('error')}"


def run(name, inp, ctx=None):
    ctx = ctx or {}
    cfg = ctx.get("cfg")
    auth_chat = ctx.get("chat")            # 服务端定的授权会话,模型参数不可覆盖
    access = ctx.get("read_access")
    if name in ("search_history", "get_member_profile", "search_kb", "draw_image") and (
            not auth_chat or not read_access.valid(access, auth_chat)):
        return "[工具授权拒绝] 只允许在有效服务端账号及当前会话授权下读取或操作"
    if name == "web_search":
        return web_search(inp.get("query", ""), cfg)
    if name == "search_history":
        # 强制只查当前会话：模型传别的 chat 一律拒绝(不能靠提示词约束)
        want = (inp.get("chat") or "").strip()
        if want and auth_chat and want != auth_chat:
            return "[search_history] 只允许检索当前会话的历史"
        return search_history(inp.get("query", ""), auth_chat, cfg, access=access)
    if name == "get_member_profile":
        from core import memory
        scope = memory.scope_of_chat(auth_chat) if auth_chat else None
        return get_member_profile(inp.get("name_or_wxid", ""), cfg, scope=scope, access=access)
    if name == "search_kb":
        return search_kb(inp.get("query", ""), cfg, access=access)
    if name == "draw_image":
        return draw_image(inp.get("prompt", ""), ctx)
    return f"[未知工具 {name}]"


def specs_for(names=None):
    """按白名单过滤工具规格；names 为空返回全部。"""
    if not names:
        return SPECS
    return [s for s in SPECS if s["name"] in names]
