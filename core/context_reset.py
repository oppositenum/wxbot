"""Per-chat context reset: later replies use persona + messages after this point."""
import json
import os
import time

import config
from core import account_session as sessions


def _path():
    return os.path.join(config.account_dir(), "context_reset.json")


def _read():
    try:
        data = json.loads(open(_path(), encoding="utf-8").read())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get(chat):
    row = _read().get(chat) or {}
    return {
        "after_id": int(row.get("after_id") or 0),
        "at": float(row.get("at") or 0),
    }


def clear(chat, after_id=0):
    """Forget history before after_id (inclusive). Next replies start from newer messages."""
    if not chat:
        raise ValueError("missing_chat")
    data = _read()
    data[chat] = {"after_id": int(after_id or 0), "at": time.time()}
    _write(data)
    return data[chat]


def filter_history(chat, messages):
    after = get(chat)["after_id"]
    if not after:
        return list(messages or [])
    return [m for m in (messages or []) if (m.get("local_id") or 0) > after]
