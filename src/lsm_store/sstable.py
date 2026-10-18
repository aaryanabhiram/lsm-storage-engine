import bisect
import io
import os
import struct
import zlib
from contextlib import closing

from . import bloom
from .blockcache import BlockCache
from .fsutil import fsync_dir
from .record import OP_DELETE, OP_PUT, CorruptionError, encode, read_records

MAGIC = b"LSMSST01"
_FOOTER_BODY = struct.Struct(">8sQQ")
FOOTER_SIZE = _FOOTER_BODY.size + 4  # 28

MAGIC_V3 = b"LSMSST02"
_FOOTER_V3_BODY = struct.Struct(">8sQQQII")
FOOTER_V3_SIZE = _FOOTER_V3_BODY.size + 4  # 44
_INDEX_ENTRY_HEADER = struct.Struct(">IQ")  # key_len, record offset

MAGIC_V4 = b"LSMSST03"
_FOOTER_V4_BODY = struct.Struct(">8sQQQIIQII")
FOOTER_V4_SIZE = _FOOTER_V4_BODY.size + 4  # 60

# lookup() result meaning "this table has no entry for the key" (distinct from a tombstone, None).
NOT_FOUND = object()


def _open_table(path):
    return open(path, "rb")


def _bad_interval(index_interval):
    if not isinstance(index_interval, int):
        return True
    if isinstance(index_interval, bool):
        return True
    if index_interval < 1:
        return True
    return False


def _bad_bloom_bits(bits_per_key):
    if not isinstance(bits_per_key, int):
        return True
    if isinstance(bits_per_key, bool):
        return True
    if bits_per_key < 1:
        return True
    if bits_per_key > bloom.MAX_BITS_PER_KEY:
        return True
    return False


def write_sstable(path, items, index_interval=None, bloom_bits_per_key=None):
    if index_interval is not None and _bad_interval(index_interval):
        raise ValueError("index_interval must be a positive integer")
    tmp = path + ".tmp"
    index_entries = []
    record_count = 0
    serialised_length = 0
    prev_key = None
    bits = None
    k = 0
    if bloom_bits_per_key is not None:
        if _bad_bloom_bits(bloom_bits_per_key):
            raise ValueError(f"bloom_bits_per_key must be an integer in 1..{bloom.MAX_BITS_PER_KEY}")
        if index_interval is None:
            raise ValueError("a Bloom filter requires a sparse index (index_interval)")
        if not isinstance(items, (list, tuple)):
            items = list(items)
        if items:
            bits = bytearray(bloom.filter_bytes(len(items), bloom_bits_per_key))
            k = bloom.num_hashes(bloom_bits_per_key)
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
                if index_interval is not None and record_count % index_interval == 0:
                    index_entries.append(_INDEX_ENTRY_HEADER.pack(len(key), serialised_length) + key)
                if bits is not None:
                    bloom.add(bits, key, k)  # PUT and DELETE alike: a tombstone is a version of the key
                f.write(data)
                record_count += 1
                serialised_length += len(data)
            if record_count == 0:
                raise ValueError("refusing to write an empty SSTable")
            if index_interval is None:
                body = _FOOTER_BODY.pack(MAGIC, record_count, serialised_length)
            else:
                index_block = b"".join(index_entries)
                f.write(index_block)
                if bits is None:
                    body = _FOOTER_V3_BODY.pack(MAGIC_V3, record_count, serialised_length, len(index_block), index_interval, zlib.crc32(index_block))
                else:
                    filter_block = bytes(bits)
                    f.write(filter_block)
                    body = _FOOTER_V4_BODY.pack(MAGIC_V4, record_count, serialised_length, len(index_block), index_interval,
                                                zlib.crc32(index_block), len(filter_block), k,
                                                zlib.crc32(filter_block))
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
    # TODO: index + bloom filter of every table live in memory (~10 bits/key for the filter), grows with total keys on disk
    # TODO bits-per-key hardcoded to 10, never tuned against the false positive rate

    def __init__(self, path, cache=None):
        self.path = path
        self._cache = cache  # shared block cache; the table identity in its keys is ``path`` (tables are never rewritten)
        size = os.path.getsize(path)
        if size < FOOTER_SIZE:
            raise CorruptionError(f"{path}: too short to contain a footer")
        self._bloom = None
        self.num_hashes = 0
        self.filter_len = 0
        self.index_len = 0
        with open(path, "rb") as f:
            # only trust a format if magic AND footer crc both match (a user key could end in bytes that look like a magic)
            for footer_size, magic, opener in ((FOOTER_V4_SIZE, MAGIC_V4, self._open_v4),
                                               (FOOTER_SIZE, MAGIC, self._open_v2),
                                               (FOOTER_V3_SIZE, MAGIC_V3, self._open_v3)):
                if size < footer_size:
                    continue
                f.seek(size - footer_size)
                footer = f.read(footer_size)
                if footer[:8] == magic:
                    stored_crc = struct.unpack(">I", footer[-4:])[0]
                    if zlib.crc32(footer[:-4]) == stored_crc:
                        opener(f, size, footer)
                        return
        raise CorruptionError(f"{path}: invalid footer")

    def _open_v2(self, f, size, footer):
        magic, count, data_len = _FOOTER_BODY.unpack(footer[:-4])
        if count == 0 or data_len != size - FOOTER_SIZE:
            raise CorruptionError(f"{self.path}: footer disagrees with file size")
        self.count = count
        self.data_len = data_len
        self.interval = None
        self._keys = []
        self._offsets = []

    def _open_v3(self, f, size, footer):
        magic, count, data_len, index_len, interval, index_crc = _FOOTER_V3_BODY.unpack(footer[:-4])
        if count == 0 or interval == 0 or data_len + index_len != size - FOOTER_V3_SIZE:
            raise CorruptionError(f"{self.path}: footer disagrees with file size")
        self._load_index(f, count, data_len, index_len, interval, index_crc)

    def _open_v4(self, f, size, footer):
        (magic, count, data_len, index_len, interval, index_crc,
         filter_len, k, filter_crc) = _FOOTER_V4_BODY.unpack(footer[:-4])
        path = self.path
        if count == 0 or interval == 0 or data_len + index_len + filter_len != size - FOOTER_V4_SIZE:
            raise CorruptionError(f"{path}: footer disagrees with file size")
        # k has to be 1..MAX_HASHES and >= 1 bit per key, else the footer is damaged and a present key could get a bloom "no"
        if not 1 <= k <= bloom.MAX_HASHES or filter_len * 8 < count:
            raise CorruptionError(f"{path}: invalid Bloom filter parameters")
        self._load_index(f, count, data_len, index_len, interval, index_crc)
        f.seek(data_len + index_len)
        block = f.read(filter_len)
        if len(block) != filter_len or zlib.crc32(block) != filter_crc:
            raise CorruptionError(f"{path}: Bloom filter block is damaged")
        self._bloom = block
        self.num_hashes = k
        self.filter_len = filter_len

    def _load_index(self, f, count, data_len, index_len, interval, index_crc):
        path = self.path
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
        self.index_len = index_len
        self._keys = keys
        self._offsets = offsets

    def _scan(self):
        rebuilt = None
        if self._bloom is not None:
            rebuilt = bytearray(self.filter_len)
        with _open_table(self.path) as raw:
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
                if rebuilt is not None:
                    bloom.add(rebuilt, rec.key, self.num_hashes)
                prev = rec.key
                n += 1
                end = rec.end_offset
                yield rec
            if n != self.count or end != self.data_len:
                raise CorruptionError(f"{self.path}: record region does not match footer")
        if rebuilt is not None and bytes(rebuilt) != self._bloom:
            raise CorruptionError(f"{self.path}: Bloom filter does not match the table's keys")

    def locate(self, key):
        b = bisect.bisect_right(self._keys, key) - 1
        if b < 0:
            return None
        if b + 1 < len(self._offsets):
            end = self._offsets[b + 1]
        else:
            end = self.data_len
        return b, self._offsets[b], end

    def may_contain(self, key):
        return self._bloom is None or bloom.may_contain(self._bloom, key, self.num_hashes)

    def lookup(self, key):
        # TODO: bloom false positive (~1% of misses at 10 bits/key) still pays a full block read. file gets reopened every call too
        if not self.may_contain(key):
            return NOT_FOUND
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
        data = None
        if self._cache is not None:
            data = self._cache.get(self.path, block)
        if data is None:
            data = self._read_block(start, end)
            # only cache full reads, a short read (truncated file) has to keep failing every time
            if self._cache is not None and len(data) == end - start:
                self._cache.put(self.path, block, data)
        try:
            n = 0
            pos = start
            prev = None
            # cached bytes go through the same decode + validation as a disk read
            for rec in read_records(_Bounded(io.BytesIO(data), end - start)):
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
        except CorruptionError:
            # bad block must not stay cached
            if self._cache is not None:
                self._cache.discard(self.path, block)
            raise

    def _read_block(self, start, end):
        # the only place a block really hits the disk (cache miss)
        with _open_table(self.path) as raw:
            raw.seek(start)
            return raw.read(end - start)

    def items(self):
        for rec in self._scan():
            if rec.op == OP_PUT:
                yield rec.key, rec.value
            else:
                yield rec.key, None
