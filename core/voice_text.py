"""Reuse an already stored WeChat transcript; never request STT or fetch audio.

Observed Linux 4.1.1.8 message.packed_info_data field 5: submessage field 1=2,
field 2=UTF-8 transcript. Unknown/status-incomplete layouts fail closed. This
is a local observed format, not a promised stable public WeChat protocol.
"""
from core import protobuf


def _complete_wire(raw):
    """The shared decoder is tolerant; reject truncated buffers before using text."""
    i = 0
    try:
        while i < len(raw):
            tag, i = protobuf._read_varint(raw, i)
            if tag >> 3 == 0:
                return False
            wire = tag & 7
            if wire == 0:
                _, i = protobuf._read_varint(raw, i)
            elif wire == 2:
                size, i = protobuf._read_varint(raw, i)
                i += size
            elif wire in (1, 5):
                i += 8 if wire == 1 else 4
            else:
                return False
        return i == len(raw)
    except ValueError:
        return False


def from_packed(raw):
    if not isinstance(raw, bytes) or len(raw) > 65536 or not _complete_wire(raw):
        return ''
    try:
        fields = protobuf.decode(raw)
        entries = fields.get(5, [])
        if len(entries) != 1 or entries[0]['type'] != 'bytes':
            return ''
        if not _complete_wire(entries[0]['value']):
            return ''
        sub = protobuf.decode(entries[0]['value'])
        status, values = sub.get(1, []), sub.get(2, [])
        if len(status) != 1 or status[0]['type'] != 'varint' or status[0]['value'] != 2 or len(values) != 1 or values[0]['type'] != 'bytes':
            return ''
        text = values[0]['value'].decode('utf-8').strip()
        if not text or len(text) > 16000 or any(ord(c) < 32 and c not in '\n\t' for c in text):
            return ''
        return text
    except (ValueError, TypeError, KeyError, UnicodeError):
        return ''
