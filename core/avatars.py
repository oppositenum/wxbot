"""头像读取（head_image.db，image_buffer 是明文 JPEG）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db  # noqa: E402


def get_avatar(username):
    """返回 (bytes, mimetype) 或 (None, None)。"""
    try:
        con = db.connect("head_image")
    except FileNotFoundError:
        return None, None
    try:
        if not db.table_exists(con, "head_image"):
            return None, None
        row = con.execute(
            "SELECT image_buffer FROM head_image WHERE username=?", (username,)
        ).fetchone()
    finally:
        con.close()
    if not row or not row["image_buffer"]:
        return None, None
    buf = row["image_buffer"]
    if isinstance(buf, str):
        buf = buf.encode("latin1", "ignore")
    return bytes(buf), "image/jpeg"
