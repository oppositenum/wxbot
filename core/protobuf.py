"""无 schema 的 protobuf wire-format 解码器（容错）。

用于解析 chat_room.ext_buffer 群成员 blob。返回嵌套结构：
    { field_no: [values...] }
value 可能是 int（varint/fixed）、bytes（length-delimited，可能是字符串或子消息）。
"""


def _read_varint(buf, i):
    shift = 0
    result = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
    raise ValueError("varint 截断")


def decode(buf):
    """解码为 { field_no: [ {'type':..,'value':..}, ... ] }。"""
    fields = {}
    i = 0
    n = len(buf)
    while i < n:
        try:
            tag, i = _read_varint(buf, i)
        except ValueError:
            break
        field_no = tag >> 3
        wire = tag & 0x07
        if wire == 0:  # varint
            val, i = _read_varint(buf, i)
            entry = {"type": "varint", "value": val}
        elif wire == 2:  # length-delimited
            length, i = _read_varint(buf, i)
            if i + length > n:
                break
            raw = buf[i:i + length]
            i += length
            entry = {"type": "bytes", "value": raw}
        elif wire == 5:  # 32-bit
            if i + 4 > n:
                break
            entry = {"type": "i32", "value": int.from_bytes(buf[i:i + 4], "little")}
            i += 4
        elif wire == 1:  # 64-bit
            if i + 8 > n:
                break
            entry = {"type": "i64", "value": int.from_bytes(buf[i:i + 8], "little")}
            i += 8
        else:
            break
        fields.setdefault(field_no, []).append(entry)
    return fields


def as_str(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def as_submsg(raw):
    """尝试把 bytes 当子消息解码；失败返回 None。"""
    try:
        sub = decode(raw)
        return sub if sub else None
    except Exception:  # noqa: BLE001
        return None
