import os
import struct
import zlib
from contextlib import closing

from .fsutil import fsync_dir
from .record import OP_DELETE, OP_PUT, CorruptionError, encode, read_records

MAGIC = b"LSMSST01"
_FOOTER_BODY = struct.Struct(">8sQQ")
FOOTER_SIZE = _FOOTER_BODY.size + 4  # 28

# lookup() result meaning "this table has no entry for the key" (distinct from a tombstone, None).
NOT_FOUND = object()


def write_sstable(path, items):
    tmp = path + ".tmp"
    cnt = 0
    data_len = 0
    last_key = None
    try:
        with open(tmp, "wb") as f:
            for key, value in items:
                if last_key is not None and key <= last_key:
                    raise ValueError("SSTable keys must be strictly ascending")
                last_key = key
                if value is None:
                    blob = encode(OP_DELETE, key)
                else:
                    blob = encode(OP_PUT, key, value)
                f.write(blob)
                cnt += 1
                data_len += len(blob)
            if cnt == 0:
                raise ValueError("refusing to write an empty SSTable")
            body = _FOOTER_BODY.pack(MAGIC, cnt, data_len)
            f.write(body + struct.pack(">I", zlib.crc32(body)))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)  # no half written tables lying around
        raise
    fsync_dir(os.path.dirname(path) or ".")


class _Bounded:
    def __init__(self, f, limit):
        self._f = f
        self._limit = limit
        self._pos = 0

    def seek(self, offset):  # read_records only ever seeks to 0
        assert offset == 0
        self._f.seek(0)
        self._pos = 0

    def read(self, n):
        data = self._f.read(min(n, self._limit - self._pos))
        self._pos += len(data)
        return data


class SSTable:
    def __init__(self, path):
        self.path = path
        sz = os.path.getsize(path)
        if sz < FOOTER_SIZE:
            raise CorruptionError(f"{path}: too short to contain a footer")
        with open(path, "rb") as f:
            f.seek(sz - FOOTER_SIZE)
            ftr = f.read(FOOTER_SIZE)
        magic, cnt, data_len = _FOOTER_BODY.unpack(ftr[:-4])
        (crc,) = struct.unpack(">I", ftr[-4:])
        if magic != MAGIC or zlib.crc32(ftr[:-4]) != crc:
            raise CorruptionError(f"{path}: invalid footer")
        if cnt == 0 or data_len != sz - FOOTER_SIZE:
            raise CorruptionError(f"{path}: footer disagrees with file size")
        self.count = cnt
        self.data_len = data_len

    def _scan(self):
        with open(self.path, "rb") as raw:
            n = 0
            end = 0
            prev = None
            for rec in read_records(_Bounded(raw, self.data_len)):
                if prev is not None and rec.key <= prev:
                    raise CorruptionError(f"{self.path}: keys out of order at offset {end}")
                prev = rec.key
                n += 1
                end = rec.end_offset
                yield rec
            if n != self.count or end != self.data_len:
                raise CorruptionError(f"{self.path}: record region does not match footer")

    def lookup(self, key):
        # TODO: every lookup re-reads and CRC-checks the table from the start.
        # Cost is O(table size) per get per table. Also reopens the file on every call.
        with closing(self._scan()) as records:
            for rec in records:
                if rec.key == key:
                    if rec.op == OP_PUT:
                        return rec.value
                    return None
                if rec.key > key:
                    return NOT_FOUND
        return NOT_FOUND

    def items(self):
        for rec in self._scan():
            if rec.op == OP_PUT:
                yield rec.key, rec.value
            else:
                yield rec.key, None
