import os
import re

from .fsutil import fsync_dir
from .record import OP_DELETE, OP_PUT, CorruptionError, read_records
from .sstable import NOT_FOUND, SSTable, write_sstable
from .wal import WriteAheadLog, replay

DEFAULT_MEMTABLE_LIMIT_BYTES = 4 * 1024 * 1024

_WAL_RE = re.compile(r"^wal-(\d{8})\.log$")
_SST_RE = re.compile(r"^sst-(\d{8})\.sst$")


def _entry_size(key, value):
    if value is None:
        return len(key)
    return len(key) + len(value)


class KVStore:
    # TODO _sstables only ever grows and get() checks every table newest -> oldest, so reads get slower the more tables pile up
    # TODO memtable limit is key+value bytes only, dict overhead isn't counted so real mem use is a few x the limit

    def __init__(self, path, sync=True, memtable_limit_bytes=DEFAULT_MEMTABLE_LIMIT_BYTES):
        if not isinstance(memtable_limit_bytes, int) or memtable_limit_bytes < 1:
            raise ValueError("memtable_limit_bytes must be a positive integer")
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
            self._sstables.append(SSTable(self._sst_path(i)))
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
        write_sstable(sst_path, sorted(self._memtable.items()))
        self._sstables.append(SSTable(sst_path))
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
        for oid in self._ids(_WAL_RE):
            if oid < wal_id:
                os.remove(self._wal_path(oid))

    def _replay_segments(self, wal_ids):
        kv_map = {}
        for i in range(len(wal_ids)):
            path = self._wal_path(wal_ids[i])
            if i == len(wal_ids) - 1:
                kv_map.update(replay(path))  # newest segment: repair a torn tail like V1
            else:
                kv_map.update(self._replay_closed_segment(path))
        return kv_map

    def _replay_closed_segment(self, path):
        # closed segs were fsynced before the next one existed, so they have to be complete. torn tail == real corruption
        kv_map = {}
        good_end = 0
        with open(path, "rb") as f:
            for r in read_records(f):
                if r.op == OP_PUT:
                    kv_map[r.key] = r.value
                else:
                    kv_map[r.key] = None
                good_end = r.end_offset
        if good_end != os.path.getsize(path):
            raise CorruptionError(f"{path}: truncated record in a non-final WAL segment")
        return kv_map

    def _ids(self, pattern):
        found = []
        for name in os.listdir(self._dir):
            mm = pattern.match(name)
            if mm:
                found.append(int(mm.group(1)))
        found.sort()
        return found

    def _take_id(self):
        curr_id = self._next_id
        self._next_id += 1
        return curr_id

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
