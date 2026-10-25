"""Minimal protobuf wire-format encoder/decoder.

Antigravity stores each conversation step as a schema-less protobuf blob in
SQLite. We never got a .proto file, so we work directly on the wire format:
field numbers and wire types are enough to read the text out and to synthesise
new steps that the CLI accepts.
"""
from __future__ import annotations


# ---------------------------------------------------------------- encoding

def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def v(field: int, n: int) -> bytes:
    """varint field"""
    return tag(field, 0) + varint(n)


def s(field: int, data: str | bytes) -> bytes:
    """length-delimited field (string / bytes / nested message)"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return tag(field, 2) + varint(len(data)) + data


def ts(field: int, seconds: int, nanos: int = 0) -> bytes:
    """google.protobuf.Timestamp-shaped nested message"""
    return s(field, v(1, seconds) + v(2, nanos))


# ---------------------------------------------------------------- decoding

def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    r = shift = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << shift
        if not (x & 0x80):
            return r, i
        shift += 7


def parse(b: bytes) -> dict[int, list]:
    """Parse into {field_number: [values]}. Values are int or bytes."""
    out: dict[int, list] = {}
    i = 0
    while i < len(b):
        try:
            t, i = _read_varint(b, i)
        except IndexError:
            break
        f, w = t >> 3, t & 7
        if w == 0:
            val, i = _read_varint(b, i)
        elif w == 2:
            ln, i = _read_varint(b, i)
            val = b[i:i + ln]
            i += ln
        elif w == 5:
            val = b[i:i + 4]
            i += 4
        elif w == 1:
            val = b[i:i + 8]
            i += 8
        else:
            break
        out.setdefault(f, []).append(val)
    return out


def first_str(d: dict[int, list], field: int) -> str | None:
    vals = d.get(field)
    if not vals:
        return None
    raw = vals[0]
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
