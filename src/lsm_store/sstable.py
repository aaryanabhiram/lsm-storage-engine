import bisect
import os
import struct
import zlib
from contextlib import closing

from .fsutil import fsync_dir
from .record import OP_DELETE, OP_PUT, CorruptionError, encode, read_records

MAGIC = b"LSMSST01"
_FOOTER_BODY = struct.Struct(">8sQQ")
FOOTER_SIZE = _FOOTER_BODY.size + 4  # 28

MAGIC_V3 = b"LSMSST02"
_FOOTER_V3_BODY = struct.Struct(">8sQQQII")
FOOTER_V3_SIZE = _FOOTER_V3_BODY.size + 4  # 44
_INDEX_ENTRY_HEADER = struct.Struct(">IQ")  # key_len, record offset

# lookup() result meaning "this table has no entry for the key" (distinct from a tombstone, None).
NOT_FOUND = object()


def _bad_interval(index_interval):
    if not isinstance(index_interval, int):
        return True
    if isinstance(index_interval, bool):
        return True
    if index_interval < 1:
        return True
    return False


def write_sstable(path, items, index_interval=None):
    if index_interval is not None and _bad_interval(index_interval):
        raise ValueError("index_interval must be a positive integer")
    tmp = path + ".tmp"
    index_entries = []
    cnt = 0
    serialized_len = 0
    prev_key = None
    try:
        with open(tmp, "wb") as f:
            for key, value in items:
                if prev_key is not None and key <= prev_key:
                    raise ValueError("SSTable keys must be strictly ascending")
                prev_key = key
                if value is None:
                    data = encode(OP_DELETE, key)
                else:
                    data = encode(OP_PUT, key, value)
                if index_interval is not None and cnt % index_interval == 0:
                    index_entries.append(_INDEX_ENTRY_HEADER.pack(len(key), serialized_len) + key)
                f.write(data)
                cnt += 1
                serialized_len += len(data)
            if cnt == 0:
                raise ValueError("refusing to write an empty SSTable")
            if index_interval is None:
                body = _FOOTER_BODY.pack(MAGIC, cnt, serialized_len)
            else:
                index_block = b"".join(index_entries)
                f.write(index_block)
                body = _FOOTER_V3_BODY.pack(MAGIC_V3, cnt, serialized_len, len(index_block), index_interval, zlib.crc32(index_block))
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
    def __init__(self, f, limit, start=0):
        self._f = f
        self._start = start
        self._remaining = limit - start
        self._left = self._remaining

    def seek(self, offset):  # read_records only ever seeks to 0
        assert offset == 0
        self._f.seek(self._start)
        self._left = self._remaining

    def read(self, n):
        data = self._f.read(min(n, self._left))
        self._left -= len(data)
        return data


class SSTable:
    # TODO whole sparse index of every table sits in memory, RAM grows with the number of tables. fine for now

    def __init__(self, path):
        self.path = path
        size = os.path.getsize(path)
        if size < FOOTER_SIZE:
            raise CorruptionError(f"{path}: too short to contain a footer")
        with open(path, "rb") as f:
            f.seek(size - FOOTER_SIZE)
            tail = f.read(FOOTER_SIZE)
            if tail[:8] == MAGIC:
                self._open_v2(size, tail)
            else:
                self._open_v3(f, size)

    def _open_v2(self, size, footer):
        magic, count, data_len = _FOOTER_BODY.unpack(footer[:-4])
        (crc,) = struct.unpack(">I", footer[-4:])
        if zlib.crc32(footer[:-4]) != crc:
            raise CorruptionError(f"{self.path}: invalid footer")
        if count == 0 or data_len != size - FOOTER_SIZE:
            raise CorruptionError(f"{self.path}: footer disagrees with file size")
        self.count = count
        self.data_len = data_len
        self.interval = None
        self._keys = []
        self._offsets = []

    def _open_v3(self, f, size):
        path = self.path
        if size < FOOTER_V3_SIZE:
            raise CorruptionError(f"{path}: invalid footer")
        f.seek(size - FOOTER_V3_SIZE)
        footer = f.read(FOOTER_V3_SIZE)
        magic, count, data_len, index_len, interval, index_crc = _FOOTER_V3_BODY.unpack(footer[:-4])
        (crc,) = struct.unpack(">I", footer[-4:])
        if magic != MAGIC_V3 or zlib.crc32(footer[:-4]) != crc:
            raise CorruptionError(f"{path}: invalid footer")
        if count == 0 or interval == 0 or data_len + index_len != size - FOOTER_V3_SIZE:
            raise CorruptionError(f"{path}: footer disagrees with file size")
        f.seek(data_len)
        block = f.read(index_len)
        if len(block) != index_len or zlib.crc32(block) != index_crc:
            raise CorruptionError(f"{path}: index block is damaged")
        keys = []
        offsets = []
        pos = 0
        while pos < len(block):
            if pos + _INDEX_ENTRY_HEADER.size > len(block):
                raise CorruptionError(f"{path}: malformed index entry")
            key_len, offset = _INDEX_ENTRY_HEADER.unpack_from(block, pos)
            pos += _INDEX_ENTRY_HEADER.size
            if pos + key_len > len(block):
                raise CorruptionError(f"{path}: malformed index entry")
            keys.append(block[pos:pos + key_len])
            offsets.append(offset)
            pos += key_len
        # sanity: 1 entry per interval, first at offset 0, keys + offsets strictly ascending, offsets inside the data region
        if len(keys) != -(-count // interval) or offsets[0] != 0 or offsets[-1] >= data_len:
            raise CorruptionError(f"{path}: index inconsistent with footer")
        for i in range(1, len(keys)):
            if keys[i - 1] >= keys[i] or offsets[i - 1] >= offsets[i]:
                raise CorruptionError(f"{path}: index inconsistent with footer")
        self.count = count
        self.data_len = data_len
        self.interval = interval
        self._keys = keys
        self._offsets = offsets

    def _scan(self):
        with open(self.path, "rb") as raw:
            n = 0
            end = 0
            prev = None
            for rec in read_records(_Bounded(raw, self.data_len)):
                if prev is not None and rec.key <= prev:
                    raise CorruptionError(f"{self.path}: keys out of order at offset {end}")
                if self.interval is not None and n % self.interval == 0:
                    b = n // self.interval
                    if self._offsets[b] != end or self._keys[b] != rec.key:
                        raise CorruptionError(f"{self.path}: index entry {b} does not match the record at offset {end}")
                prev = rec.key
                n += 1
                end = rec.end_offset
                yield rec
            if n != self.count or end != self.data_len:
                raise CorruptionError(f"{self.path}: record region does not match footer")

    def locate(self, key):
        b = bisect.bisect_right(self._keys, key) - 1
        if b < 0:
            return None
        if b + 1 < len(self._offsets):
            end = self._offsets[b + 1]
        else:
            end = self.data_len
        return b, self._offsets[b], end

    def lookup(self, key):
        # TODO: a key that isn't in the table still costs a block read if it's inside the key range. also reopens the file every call
        if self.interval is None:
            records = self._scan()
        else:
            located = self.locate(key)
            if located is None:
                return NOT_FOUND
            records = self._scan_block(*located)
        with closing(records):
            for rec in records:
                if rec.key == key:
                    if rec.op == OP_PUT:
                        return rec.value
                    return None
                if rec.key > key:
                    return NOT_FOUND
        return NOT_FOUND

    def _scan_block(self, block, start, end):
        expected = min(self.interval, self.count - block * self.interval)
        if block + 1 < len(self._keys):
            next_key = self._keys[block + 1]
        else:
            next_key = None
        with open(self.path, "rb") as raw:
            n = 0
            pos = start
            prev = None
            for rec in read_records(_Bounded(raw, end, start)):
                if n == 0 and rec.key != self._keys[block]:
                    raise CorruptionError(f"{self.path}: index entry {block} does not match the record at offset {start}")
                if prev is not None and rec.key <= prev:
                    raise CorruptionError(f"{self.path}: keys out of order at offset {pos}")
                if next_key is not None and rec.key >= next_key:
                    raise CorruptionError(f"{self.path}: block {block} overlaps the next block")
                prev = rec.key
                n += 1
                pos = start + rec.end_offset
                yield rec
            if n != expected or pos != end:
                raise CorruptionError(f"{self.path}: block {block} does not match the index")

    def items(self):
        for rec in self._scan():
            if rec.op == OP_PUT:
                yield rec.key, rec.value
            else:
                yield rec.key, None
