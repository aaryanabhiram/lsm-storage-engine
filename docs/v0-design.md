# V0 design: append-only log with sequential-scan reads

V0 is the deliberately simple persistent baseline. It has one data file, every write appends a record, and every read scans the whole file.

## API

```python
from lsm_store import KVStore

with KVStore("data.lsm", sync=True) as db:   # path of the single data file
    db.put(b"k", b"v")        # append a PUT record
    db.get(b"k")              # -> b"v", or None if absent / deleted
    db.delete(b"k")           # append a DELETE record (tombstone)
```

- Keys and values are `bytes` (anything else raises `TypeError`). Empty keys and empty values are allowed. `get` returns `b""` for a present-but-empty value and `None` for absent.
- Use after `close()` raises `ValueError`. `close()` is idempotent.
- `delete` returns nothing and always appends a tombstone, even if the key is absent (checking would need a full scan).

## On-disk record format

All integers are unsigned and **big-endian**. Header size is 13 bytes (`struct` format `">IBII"`).

| offset | size | field | meaning |
|---|---|---|---|
| 0 | 4 | `crc32` | CRC-32 (`zlib.crc32`) of every byte after this field |
| 4 | 1 | `op` | `1` = PUT, `2` = DELETE |
| 5 | 4 | `key_len` | key length in bytes |
| 9 | 4 | `value_len` | value length in bytes; always `0` for DELETE |
| 13 | `key_len` | key | |
| 13 + `key_len` | `value_len` | value | |

Record length = `13 + key_len + value_len`. The file is a concatenation of such records with no separators or file header.

The checksum exists to detect corrupted bytes (bad write, bit rot). It doesn't repair data and isn't a security mechanism.

## Invariants

1. The file only grows by appending whole records; existing records are never modified.
2. After `KVStore(path)` returns, the file is exactly a sequence of complete, checksum-valid records.
3. The state of a key is determined by its **last** record in file order: PUT gives its value, DELETE gives absent.

## Durability policy

Three distinct levels:

1. `write` copies bytes into Python's buffer (lost on process crash).
2. `flush` hands them to the OS page cache (survives a process crash, not power loss).
3. `os.fsync` asks the OS to push them to the storage device (survives power loss, as far as the OS and device honor it).

`put`/`delete` always write and flush. With `sync=True` (default) they also `fsync` before returning. With `sync=False` they don't, so the most recent writes can be lost on power loss or OS crash. `close()` always flushes and fsyncs. V0 doesn't fsync the containing directory, and it makes no claim beyond the above.

## Open / recovery

`KVStore(path)` scans the whole file once:

- **Truncated tail** (the file ends mid-record, e.g. a crash during append): the partial record was never acknowledged, so the file is truncated to the end of the last valid record.
- **Checksum mismatch or malformed fields in a complete record**: raises `CorruptionError`, and nothing is modified.

## GET

Open a read handle, scan every record from the start, keep the latest state for the requested key, and return it. There's no index and no early exit. Cost is O(file size) per `get`. This is the baseline to improve on.

## Known limitations (V0)

- Single process, single writer, no file locking. Two processes using the same file can corrupt it.
- A corrupted length field in the middle of the file looks like a truncated tail, so recovery would truncate everything after it.
- A write that fails midway within a running process (e.g. disk full) can leave a partial record that is only cleaned up on the next open.
- Old versions and tombstones are never reclaimed; the file only grows.
- `get` and the open-time scan are O(file size).
- Not thread-safe.
