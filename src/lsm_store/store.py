from .record import OP_DELETE, OP_PUT
from .wal import WriteAheadLog, replay


class KVStore:
    # TODO _memtable is unbounded, every live key AND value sits in RAM so the whole dataset has to fit
    # TODO single process only, no file locking

    def __init__(self, path, sync=True):
        # full wal replay on open is slow but it works for now
        self._memtable = replay(path)  # recover (and repair) first ...
        self._wal = WriteAheadLog(path, sync)  # ... then open for append

    def put(self, key, value):
        self._check_bytes(key, value)
        self._check_open()
        # write path blocks main thread here, deal with background workers later
        self._wal.append(OP_PUT, key, value)  # may raise: memtable untouched
        self._memtable[key] = value

    def delete(self, key):
        self._check_bytes(key)
        self._check_open()
        self._wal.append(OP_DELETE, key)
        # TODO tombstones stay in the memtable + wal forever, nothing reclaims deleted keys
        self._memtable[key] = None

    def get(self, key):
        self._check_bytes(key)
        self._check_open()


        # None = missing or tombstone, same thing. no truthiness test, b"" is a valid value
        return self._memtable.get(key)

    def close(self):
        self._wal.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _check_open(self):
        if self._wal.closed:
            raise ValueError("store is closed")

    def _check_bytes(self, *items):
        for item in items:
            if not isinstance(item, bytes):
                raise TypeError(f"keys and values must be bytes, got {type(item).__name__}")
