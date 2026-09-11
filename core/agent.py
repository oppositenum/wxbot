"""多轮工具调用 Agent：给 LLM 挂上工具(联网/查历史/查画像/查知识库)，自主决定调用。

用于机器人"agent 模式"回复：不仅按人设风格说话，还能查资料/查历史/查知识库后再答。
配置见 llm_config.json 的 agent 段(或 bot_rules 的 agent)：
  {"enabled":true, "tools":["web_search","search_kb",...]}
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402,F401
from core import llm, tools, read_access, account_session as sessions  # noqa: E402


@sessions.task
def run(system, user_msg, chat=None, tool_names=None, cfg=None, max_rounds=6,
        display_name=None, history=None):
    """跑一轮 agent 对话。system=人设/指令，user_msg=要回应的内容(可含上下文)。

    返回最终回复文本。工具执行失败会作为工具结果回给模型，由模型决定如何继续。
    display_name 供 draw_image 等要主动发消息的工具定位/打开会话用。
    """
    cfg = cfg or llm.load_cfg()
    specs = tools.specs_for(tool_names)
    ctx = {"chat": chat, "cfg": cfg, "display_name": display_name,
           "read_access": read_access.issue(chat)}

    def dispatch(name, inp):
        sessions.check()
        if name == "draw_image" and chat:
            # Give the user immediate feedback before the potentially slow image request.
            from core import sender
            sender.send_text(display_name or chat, "收到，正在为你生成图片，请稍等", chat_username=chat)
        result = tools.run(name, inp, ctx)
        sessions.check()
        return result

    dialogue = [dict(message) for message in (history or [])]
    if any(message.get('role') not in ('user', 'assistant') or
           not isinstance(message.get('content'), str) for message in dialogue):
        raise ValueError('invalid_dialogue_history')
    dialogue.append({"role": "user", "content": user_msg})
    return llm.chat_tools(system, dialogue,
                          specs, dispatch, cfg=cfg, max_rounds=max_rounds)


def agent_config(rules=None):
    """合并出 agent 配置：bot_rules.agent 优先，其次 llm_config.agent。"""
    cfg = llm.load_cfg().get("agent", {}) or {}
    if rules and isinstance(rules.get("agent"), dict):
        cfg = {**cfg, **rules["agent"]}
    return {"enabled": bool(cfg.get("enabled")),
            "tools": cfg.get("tools") or [s["name"] for s in tools.SPECS]}
