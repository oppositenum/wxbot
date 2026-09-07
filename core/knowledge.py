"""知识库(RAG)：文档切块入 sqlite FTS5，BM25 检索。离线可用、无需 embedding。

中文分词难题的解法：自己把文本打成 2/3-gram(空格连接)存进 FTS5 的索引列，
用 unicode61 分词器让每个 n-gram 成为一个 token；查询同样 n-gram 化，BM25 按
n-gram 重叠度排序——中文召回好、无需外部分词器/embedding。真实内容存 UNINDEXED 列。
每账号一个 kb.db：accounts/<wxid>/kb.db。
"""
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

_CJK = r"一-鿿㐀-䶿"


def _kb_path():
    return os.path.join(config.account_dir(), "kb.db")


def _ngramize(text):
    """文本→空格连接的检索 token：ascii 词 + 中文 2/3-gram。"""
    text = (text or "").lower()
    out = re.findall(r"[a-z0-9]+", text)
    for run in re.findall(f"[{_CJK}]+", text):
        if len(run) == 1:
            out.append(run)
        for n in (2, 3):
            out += [run[i:i + n] for i in range(len(run) - n + 1)]
    return " ".join(out)


def _connect():
    os.makedirs(config.account_dir(), exist_ok=True)
    con = sqlite3.connect(_kb_path())
    con.row_factory = sqlite3.Row
    _ensure(con)
    return con


def _ensure(con):
    if con.execute("SELECT name FROM sqlite_master WHERE name='docs'").fetchone():
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name='docs'").fetchone()[0]
        if "ngrams" in (sql or ""):
            return
        con.execute("DROP TABLE docs")             # 旧 schema：重建(kb 无长期数据)
    try:
        con.execute("CREATE VIRTUAL TABLE docs USING fts5("
                    "ngrams, title UNINDEXED, content UNINDEXED, source UNINDEXED, "
                    "tokenize='unicode61')")
    except sqlite3.OperationalError:              # 无 FTS5：退回普通表
        con.execute("CREATE TABLE docs (ngrams TEXT, title TEXT, content TEXT, source TEXT)")
    con.commit()


def _has_fts(con):
    r = con.execute("SELECT sql FROM sqlite_master WHERE name='docs'").fetchone()
    return bool(r and "fts5" in (r["sql"] or "").lower())


def _chunk(text, size=500, overlap=80):
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 <= size:
            cur = (cur + "\n" + p) if cur else p
        else:
            if cur:
                chunks.append(cur)
            if len(p) <= size:
                cur = p
            else:
                for i in range(0, len(p), size - overlap):
                    chunks.append(p[i:i + size])
                cur = ""
    if cur:
        chunks.append(cur)
    return chunks


def add(title, content, source="manual"):
    con = _connect()
    try:
        con.execute("INSERT INTO docs(ngrams, title, content, source) VALUES(?,?,?,?)",
                    (_ngramize((title or "") + " " + (content or "")),
                     title or "", content or "", source or ""))
        con.commit()
    finally:
        con.close()


def import_text(text, source="import", title_prefix=""):
    """整段文本切块入库，返回块数。"""
    chunks = _chunk(text)
    con = _connect()
    try:
        for i, c in enumerate(chunks):
            t = f"{title_prefix}#{i+1}" if title_prefix else (c[:20] + "…")
            con.execute("INSERT INTO docs(ngrams, title, content, source) VALUES(?,?,?,?)",
                        (_ngramize(t + " " + c), t, c, source))
        con.commit()
    finally:
        con.close()
    return len(chunks)


def search(query, k=5):
    """检索最相关的 k 个知识块。返回 [{title,content,source,score}]。"""
    con = _connect()
    try:
        if _has_fts(con):
            toks = _ngramize(query).split()
            if not toks:
                return []
            match = " OR ".join(f'"{t}"' for t in dict.fromkeys(toks))
            rows = con.execute(
                "SELECT title, content, source, bm25(docs) AS score "
                "FROM docs WHERE docs MATCH ? ORDER BY score LIMIT ?",
                (match, k)).fetchall()
        else:
            rows = con.execute(
                "SELECT title, content, source, 0 AS score FROM docs "
                "WHERE content LIKE ? LIMIT ?", (f"%{query}%", k)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def list_docs(limit=200):
    con = _connect()
    try:
        rows = con.execute(
            "SELECT rowid AS id, title, substr(content,1,80) AS preview, source "
            "FROM docs LIMIT ?", (limit,)).fetchall()
        n = con.execute("SELECT count(*) FROM docs").fetchone()[0]
        return {"total": n, "docs": [dict(r) for r in rows]}
    finally:
        con.close()


def delete(source=None, doc_id=None):
    con = _connect()
    try:
        if doc_id is not None:
            con.execute("DELETE FROM docs WHERE rowid=?", (doc_id,))
        elif source is not None:
            con.execute("DELETE FROM docs WHERE source=?", (source,))
        con.commit()
    finally:
        con.close()


def clear():
    con = _connect()
    try:
        con.execute("DELETE FROM docs")
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    clear()
    import_text("公司报销流程：先在OA提交发票，主管审批后财务打款，周期约5个工作日。\n\n"
                "年假政策：入职满一年10天，每多一年加1天，上限15天。", source="faq")
    for q in ("报销怎么走", "年假有几天"):
        print(f"检索「{q}」：")
        for h in search(q, 2):
            print("  -", h["content"][:36], "| score", round(h["score"], 2))
    clear()
    print("(已清理测试数据)")
