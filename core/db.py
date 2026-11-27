"""打开解密后的数据库并提供通用查询。"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


def connect(key_name):
    """返回某核心库的只读连接（key_name 见 config.CORE_DBS）。"""
    path = config.decrypted_path(key_name)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} 不存在，请先运行 core.keys（sudo）与 core.decrypt。"
        )
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # 微信库里可能混进非法 UTF-8 的 TEXT(例如 Name2Id 里的坏 user_name),
    # 默认 str 解码会让整次读取抛错、bot 整轮失败。替换坏字节,只影响那一行。
    con.text_factory = _lenient_text
    return con


def _lenient_text(b):
    return b.decode("utf-8", "replace")


def query(key_name, sql, params=()):
    con = connect(key_name)
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def table_exists(con, name):
    r = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


def columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]
