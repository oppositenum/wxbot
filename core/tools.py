"""Agent 工具实现 + Claude 工具规格。

工具：
- web_search   联网搜索(DuckDuckGo HTML，无需 key，走 cfg.proxy)
- search_history 在某会话的历史消息里检索
- get_member_profile 查某人的长期画像(core.memory)
- search_kb    在知识库里检索(core.knowledge, FTS5/BM25)

run(name, inp, ctx) 统一分发；ctx={"chat":当前会话wxid, "cfg":llm配置}。
"""
import html
import re
import sys
import os
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402,F401

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

SPECS = [
    {"name": "web_search",
     "description": "联网搜索实时/外部信息(新闻、事实、你不知道的东西)。返回若干条标题+摘要。",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "搜索关键词"}}, "required": ["query"]}},
    {"name": "search_history",
     "description": "在当前(或指定)微信会话的历史聊天记录里检索相关消息。",
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


def search_history(query, chat, cfg=None, k=8):
    from core import messages, knowledge
    if not chat:
        return "[search_history] 未指定会话"
    try:
        msgs = messages.get_messages(chat, limit=400)
    except Exception as e:  # noqa: BLE001
        return f"[search_history 失败] {e}"
    # 简单：用知识库的 n-gram 匹配思路做本地打分(不落库)，退回子串匹配
    toks = knowledge._ngramize(query).split()
    scored = []
    for m in msgs:
        c = (m.get("content") or "")
        if not c or c.startswith("["):
            continue
        cg = knowledge._ngramize(c)
        hit = sum(cg.count(t) for t in toks) if toks else 0
        if hit == 0 and query in c:
            hit = 1
        if hit:
            who = "我" if m.get("is_self") else (m.get("sender_name") or "对方")
            scored.append((hit, f"{who}: {c}"))
    scored.sort(key=lambda x: -x[0])
    lines = [s for _, s in scored[:k]]
    return "\n".join(lines) if lines else "[search_history 无匹配]"


def get_member_profile(name_or_wxid, cfg=None):
    from core import memory, contacts
    wid = name_or_wxid
    if not str(name_or_wxid).startswith("wxid_") and "@" not in str(name_or_wxid):
        for c in contacts.list_contacts():          # 名字→wxid
            if c.get("name") == name_or_wxid:
                wid = c["username"]
                break
    txt = memory.profile_context(wid)
    return txt or f"[暂无关于 {name_or_wxid} 的画像]"


def search_kb(query, cfg=None, k=5):
    from core import knowledge
    hits = knowledge.search(query, k)
    if not hits:
        return "[知识库无相关内容]"
    return "\n\n".join(f"[{h['source']}] {h['content']}" for h in hits)


def run(name, inp, ctx=None):
    ctx = ctx or {}
    cfg = ctx.get("cfg")
    if name == "web_search":
        return web_search(inp.get("query", ""), cfg)
    if name == "search_history":
        return search_history(inp.get("query", ""), inp.get("chat") or ctx.get("chat"), cfg)
    if name == "get_member_profile":
        return get_member_profile(inp.get("name_or_wxid", ""), cfg)
    if name == "search_kb":
        return search_kb(inp.get("query", ""), cfg)
    return f"[未知工具 {name}]"


def specs_for(names=None):
    """按白名单过滤工具规格；names 为空返回全部。"""
    if not names:
        return SPECS
    return [s for s in SPECS if s["name"] in names]
