import os
import re

from . import bloom, sstable
from .blockcache import DEFAULT_BLOCK_CACHE_BYTES, BlockCache
from .fsutil import fsync_dir
from .record import OP_DELETE, OP_PUT, CorruptionError, read_records
from .sstable import NOT_FOUND, SSTable
from .wal import WriteAheadLog, replay

DEFAULT_MEMTABLE_LIMIT_BYTES = 4 * 1024 * 1024
DEFAULT_INDEX_INTERVAL = 16  # records per index block
DEFAULT_BLOOM_BITS_PER_KEY = bloom.DEFAULT_BITS_PER_KEY  # 10 bits/key, 7 hashes, ~0.8% false positives

_WAL_RE = re.compile(r"^wal-(\d{8})\.log$")
_SST_RE = re.compile(r"^sst-(\d{8})\.sst$")


def _entry_size(key, value):
    if value is None:
        return len(key)
    return len(key) + len(value)


class KVStore:
    # TODO: _sstables only grows and get() walks every table newest to oldest, reads degrade as tables pile up
    # TODO memtable limit is key+value bytes only, dict overhead isn't counted so real mem use is a few x the limit
    # TODO: index_interval is fixed at 16, never tuned. bigger interval = smaller index, more records scanned per lookup
    # TODO: filters only help for keys missing from a table, a present key still needs a block read. no range scan at all
    # TODO: block cache is per store instance (cold after reopen, not shared), sized in payload bytes only

    def __init__(self, path, sync=True, memtable_limit_bytes=DEFAULT_MEMTABLE_LIMIT_BYTES,
                 index_interval=DEFAULT_INDEX_INTERVAL, bloom_bits_per_key=DEFAULT_BLOOM_BITS_PER_KEY,
                 block_cache_bytes=DEFAULT_BLOCK_CACHE_BYTES):
        if not isinstance(memtable_limit_bytes, int) or memtable_limit_bytes < 1:
            raise ValueError("memtable_limit_bytes must be a positive integer")
        if not isinstance(index_interval, int) or isinstance(index_interval, bool) or index_interval < 1:
            raise ValueError("index_interval must be a positive integer")
        if (not isinstance(bloom_bits_per_key, int) or isinstance(bloom_bits_per_key, bool)
                or not 1 <= bloom_bits_per_key <= bloom.MAX_BITS_PER_KEY):
            raise ValueError(f"bloom_bits_per_key must be an integer in 1..{bloom.MAX_BITS_PER_KEY}")
        self._block_cache = BlockCache(block_cache_bytes)  # before opening the tables: they share it
        self._index_interval = index_interval
        self._bloom_bits_per_key = bloom_bits_per_key
        self._dir = path
        self._sync = sync
        self._limit = memtable_limit_bytes
        os.makedirs(path, exist_ok=True)

        # leftover .tmp from a flush that died, safe to delete
        for name in os.listdir(path):
            if name.endswith(".tmp") and _SST_RE.match(name[:-4]):
                os.remove(os.path.join(path, name))

        sst_ids = self._ids(_SST_RE)
        wal_ids = self._ids(_WAL_RE)
        self._sstables = []  # oldest -> newest
        for i in sst_ids:
            self._sstables.append(SSTable(self._sst_path(i), cache=self._block_cache))
        self._memtable = self._replay_segments(wal_ids)
        self._mem_bytes = 0
        for k, v in self._memtable.items():
            self._mem_bytes += _entry_size(k, v)
        self._next_id = max(sst_ids + wal_ids, default=0) + 1

        # only keep appending to the newest wal if it's newer than all the ssts, else new segment
        # (fresh dir, or flush died after the sst was already durable)
        if wal_ids and wal_ids[-1] > max(sst_ids, default=0):
            self._wal_id = wal_ids[-1]
            self._wal = WriteAheadLog(self._wal_path(self._wal_id), sync)
        else:
            self._wal_id = self._take_id()
            self._wal = WriteAheadLog(self._wal_path(self._wal_id), sync)
            fsync_dir(self._dir)

    # ---- public api ----

    def put(self, key, value):
        self._check_bytes(key, value)
        self._check_open()


        # flush runs inline when the memtable is full, so this put() eats the whole sstable write + fsync. bg thread someday
        self._flush_if_full()  # flush first so a failed flush can't leave a half applied put
        self._wal.append(OP_PUT, key, value)  # may raise: memtable untouched
        self._apply(key, value)

    def delete(self, key):
        self._check_bytes(key)
        self._check_open()
        self._flush_if_full()
        self._wal.append(OP_DELETE, key)
        self._apply(key, None)

    def get(self, key):
        self._check_bytes(key)
        self._check_open()
        if key in self._memtable:
            return self._memtable[key]  # None == tombstone
        for table in reversed(self._sstables):
            found = table.lookup(key)
            if found is not NOT_FOUND:
                return found
        return None

    def flush(self):
        self._check_open()
        if not self._memtable:
            return
        # 1. write the sst. nothing changed until the rename in write_sstable returns, wal still has everything
        sst_path = self._sst_path(self._take_id())
        sstable.write_sstable(sst_path, sorted(self._memtable.items()), index_interval=self._index_interval,
                              bloom_bits_per_key=self._bloom_bits_per_key)
        self._sstables.append(SSTable(sst_path, cache=self._block_cache))
        # 2. sst covers the memtable now, new wal segment
        new_id = self._take_id()
        new_wal = WriteAheadLog(self._wal_path(new_id), self._sync)
        fsync_dir(self._dir)
        old_wal = self._wal
        self._wal = new_wal
        self._wal_id = new_id
        self._memtable = {}
        self._mem_bytes = 0
        # 3. old wal segs are redundant now, delete oldest first
        old_wal.close()
        self._delete_segments_before(new_id)

    def close(self):
        self._wal.close()

    @property
    def block_cache(self):
        return self._block_cache

    def cache_stats(self):
        return self._block_cache.stats()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- internals ----

    def _apply(self, key, value):
        if key in self._memtable:
            self._mem_bytes -= _entry_size(key, self._memtable[key])
        self._memtable[key] = value
        self._mem_bytes += _entry_size(key, value)

    def _flush_if_full(self):
        if self._mem_bytes >= self._limit:
            self.flush()

    def _delete_segments_before(self, wal_id):
        # oldest first on purpose: if we die halfway the leftovers are the newest segments, replaying those can't bring back stale values
        for old_id in self._ids(_WAL_RE):
            if old_id < wal_id:
                os.remove(self._wal_path(old_id))

    def _replay_segments(self, wal_ids):
        memtable = {}
        for i in range(len(wal_ids)):
            path = self._wal_path(wal_ids[i])
            if i == len(wal_ids) - 1:
                memtable.update(replay(path))  # newest segment: repair a torn tail like V1
            else:
                memtable.update(self._replay_closed_segment(path))
        return memtable

    def _replay_closed_segment(self, path):
        # closed segs were fsynced before the next one existed, so they have to be complete. torn tail == real corruption
        memtable = {}
        valid_end = 0
        with open(path, "rb") as f:
            for rec in read_records(f):
                if rec.op == OP_PUT:
                    memtable[rec.key] = rec.value
                else:
                    memtable[rec.key] = None
                valid_end = rec.end_offset
        if valid_end != os.path.getsize(path):
            raise CorruptionError(f"{path}: truncated record in a non-final WAL segment")
        return memtable

    def _ids(self, pattern):
        ids = []
        for name in os.listdir(self._dir):
            m = pattern.match(name)
            if m:
                ids.append(int(m.group(1)))
        ids.sort()
        return ids

    def _take_id(self):
        taken = self._next_id
        self._next_id += 1
        return taken

    def _wal_path(self, wal_id):
        return os.path.join(self._dir, "wal-%08d.log" % wal_id)

    def _sst_path(self, sst_id):
        return os.path.join(self._dir, "sst-%08d.sst" % sst_id)

    def _check_open(self):
        if self._wal.closed:
            raise ValueError("store is closed")

    def _check_bytes(self, *items):
        for item in items:
            if not isinstance(item, bytes):
                raise TypeError(f"keys and values must be bytes, got {type(item).__name__}")
