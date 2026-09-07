"""多轮工具调用 Agent：给 LLM 挂上工具(联网/查历史/查画像/查知识库)，自主决定调用。

用于机器人"agent 模式"回复：不仅按人设风格说话，还能查资料/查历史/查知识库后再答。
配置见 llm_config.json 的 agent 段(或 bot_rules 的 agent)：
  {"enabled":true, "tools":["web_search","search_kb",...]}
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402,F401
from core import llm, tools  # noqa: E402


def run(system, user_msg, chat=None, tool_names=None, cfg=None, max_rounds=6):
    """跑一轮 agent 对话。system=人设/指令，user_msg=要回应的内容(可含上下文)。

    返回最终回复文本。工具执行失败会作为工具结果回给模型，由模型决定如何继续。
    """
    cfg = cfg or llm.load_cfg()
    specs = tools.specs_for(tool_names)
    ctx = {"chat": chat, "cfg": cfg}

    def dispatch(name, inp):
        return tools.run(name, inp, ctx)

    return llm.chat_tools(system, [{"role": "user", "content": user_msg}],
                          specs, dispatch, cfg=cfg, max_rounds=max_rounds)


def agent_config(rules=None):
    """合并出 agent 配置：bot_rules.agent 优先，其次 llm_config.agent。"""
    cfg = llm.load_cfg().get("agent", {}) or {}
    if rules and isinstance(rules.get("agent"), dict):
        cfg = {**cfg, **rules["agent"]}
    return {"enabled": bool(cfg.get("enabled")),
            "tools": cfg.get("tools") or [s["name"] for s in tools.SPECS]}
