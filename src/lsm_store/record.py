import struct
import zlib
from collections import namedtuple

OP_PUT = 1
OP_DELETE = 2

# > big-endian, I = u32, B = u8
HEADER = struct.Struct(">IBII")
HEADER_SIZE = HEADER.size  # 13
MAX_LEN = 2**32 - 1


class CorruptionError(Exception):
    pass


# end_offset = offset right after the record
Record = namedtuple("Record", "op key value end_offset")


def encode(op, key, value=b""):
    if op == OP_DELETE and value:
        raise ValueError("DELETE records carry no value")
    if len(key) > MAX_LEN:
        raise ValueError("key or value too large for a uint32 length field")
    if len(value) > MAX_LEN:
        raise ValueError("key or value too large for a uint32 length field")
    # crc covers op, lens, key, value
    body = struct.pack(">BII", op, len(key), len(value)) + key + value
    return struct.pack(">I", zlib.crc32(body)) + body


def read_records(f):
    f.seek(0)
    offset = 0
    while True:
        hdr = f.read(HEADER_SIZE)
        if len(hdr) < HEADER_SIZE:
            return  # eof or torn header
        crc, op, key_len, value_len = HEADER.unpack(hdr)
        # TODO key_len/value_len are trusted until the crc check, a bad length field = we try to read up to 4GB. cap it
        pl = f.read(key_len + value_len)
        if len(pl) < key_len + value_len:
            return  # torn body
        if zlib.crc32(hdr[4:] + pl) != crc:
            raise CorruptionError(f"checksum mismatch in record at offset {offset}")
        if op not in (OP_PUT, OP_DELETE):
            raise CorruptionError(f"malformed record at offset {offset}")
        if op == OP_DELETE:
            if value_len != 0:
                raise CorruptionError(f"malformed record at offset {offset}")
        offset += HEADER_SIZE + key_len + value_len
        yield Record(op, pl[:key_len], pl[key_len:], offset)
