import os

from .record import OP_PUT, encode, read_records

# memtable is a plain dict, None = tombstone


def replay(path):
    kv_map = {}
    if not os.path.exists(path):
        return kv_map
    with open(path, "r+b") as f:
        good_end = 0
        for r in read_records(f):  # CorruptionError if the data is bad
            if r.op == OP_PUT:
                kv_map[r.key] = r.value
            else:
                kv_map[r.key] = None
            good_end = r.end_offset
        if good_end < os.fstat(f.fileno()).st_size:
            f.truncate(good_end)
            f.flush()
            os.fsync(f.fileno())
    return kv_map


class WriteAheadLog:
    def __init__(self, path, sync=True):
        self._sync = sync
        self._file = open(path, "ab")

    @property
    def closed(self):
        return self._file.closed

    def append(self, op, key, value=b""):
        self._file.write(encode(op, key, value))
        self._file.flush()  # py buffer -> os
        if self._sync:
            os.fsync(self._file.fileno())  # os -> disk

    def close(self):
        if self._file.closed:
            return
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
