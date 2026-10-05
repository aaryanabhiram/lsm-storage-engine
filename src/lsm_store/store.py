import os

from .record import OP_DELETE, OP_PUT, encode, read_records


class KVStore:
    # TODO single process only, nothing stops two stores opening the same file

    def __init__(self, path, sync=True):
        self._path = path
        self._sync = sync
        self._recover()
        self._file = open(path, "ab")

    def _recover(self):
        # cut off a torn trailing record if there is one
        if not os.path.exists(self._path):
            return
        with open(self._path, "r+b") as f:
            good_end = 0
            # full scan on open just to find where the last good record ends, slow on big files but it works for now
            for r in read_records(f):  # raises CorruptionError on bad data
                good_end = r.end_offset
            if good_end < os.fstat(f.fileno()).st_size:
                f.truncate(good_end)
                f.flush()
                os.fsync(f.fileno())

    def put(self, key, value):
        self._check_bytes(key, value)
        self._append(encode(OP_PUT, key, value))

    def delete(self, key):
        # TODO file only ever grows, old values + tombstones never get cleaned up so overwrites waste disk
        self._check_bytes(key)
        self._append(encode(OP_DELETE, key))

    def get(self, key):
        self._check_bytes(key)
        self._check_open()
        curr = None

        # TODO every get scans the whole file, O(file size). main problem with v0
        with open(self._path, "rb") as f:
            for r in read_records(f):  # later records override earlier ones
                if r.key == key:
                    if r.op == OP_PUT:
                        curr = r.value
                    else:
                        curr = None
        return curr

    def close(self):
        if self._file.closed:
            return
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _append(self, data):
        self._check_open()
        self._file.write(data)
        self._file.flush()  # py buffer -> os
        if self._sync:
            # one fsync per write, safe but slow (no batching)
            os.fsync(self._file.fileno())  # os -> disk

    def _check_open(self):
        if self._file.closed:
            raise ValueError("store is closed")

    def _check_bytes(self, *items):
        for item in items:
            if not isinstance(item, bytes):
                raise TypeError(f"keys and values must be bytes, got {type(item).__name__}")
