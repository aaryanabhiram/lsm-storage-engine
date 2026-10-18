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

## Code walkthrough

Read the code in this order: `record.py` (the format), then `store.py` (the three operations).

**1. `record.encode` builds one record.** The CRC is computed over everything *after* itself, then prepended:

```python
body = struct.pack(">BII", op, len(key), len(value)) + key + value
return struct.pack(">I", zlib.crc32(body)) + body
```

Because the lengths are inside the checksummed body, a flipped bit in `key_len` is caught by the CRC like any other bit.

**2. `record.read_records` is a generator that walks the file.** Three outcomes matter:

| situation | what the reader does |
|---|---|
| header or body is shorter than declared | `return` quietly: a torn tail, not an error |
| CRC mismatch, unknown `op`, DELETE with a value | `raise CorruptionError` |
| valid record | `yield Record(op, key, value, end_offset)` |

`end_offset` is what recovery uses to know where the last good record ends.

**3. `KVStore._recover` runs once at open.** It scans the whole file, remembers the last `end_offset`, and if the file is longer than that, truncates it and fsyncs. Then (and only then) `__init__` opens the file in append mode. Order matters: appending before repairing would put new records after garbage.

**4. `put` / `delete` go through `_append`.** `write` (Python buffer) -> `flush` (OS cache) -> optional `os.fsync` (disk). Those are the three durability levels from the table above, one line each.

**5. `get` is the baseline to beat.** It opens a fresh read handle and loops over every record, overwriting `curr` each time the key matches. "Last record wins" is implemented by simply not stopping early.

```python
for r in read_records(f):          # later records override earlier ones
    if r.key == key:
        curr = r.value if r.op == OP_PUT else None
```

Things to notice: a tombstone is just `curr = None`, and there is no truthiness test anywhere, so `b""` stays a real value.

## Interview questions

**Why an append-only log instead of updating records in place?**
Appends are sequential I/O, which is the cheapest access pattern on both HDDs and SSDs. An in-place update can be torn halfway (old and new bytes mixed), whereas an append can only fail at the tail, where recovery can detect and cut it. It also makes "state = last record for the key" a one-line rule.

**Why does `delete` append a tombstone instead of removing the record?**
You can't remove bytes from the middle of an append-only file without rewriting it. A tombstone is also what later stages need: it must be able to *shadow* older values that live in other files.

**Why does `delete` append a tombstone even if the key doesn't exist?**
Checking would need a full scan (V0 has no index), and the tombstone is harmless. It keeps `delete` O(1).

**Why a CRC on every record? What does it not protect against?**
It detects accidental damage (bad write, bit rot). It does not repair anything and is not a security feature: anyone who can rewrite the file can recompute the CRC.

**Why is the CRC placed first and computed over the lengths too?**
So the reader can validate a record from its header + body without trusting any field first. If lengths were outside the checksum, a corrupt length would silently desynchronise every following record.

**Why treat a short tail as "torn" but a bad CRC as "corruption"?**
A short tail is exactly what a crash during append produces, and that write was never acknowledged, so dropping it loses nothing. A *complete* record with a wrong checksum means acknowledged data was damaged. Silently dropping it would hide data loss, so the store refuses to open.

**What's the weakness of that rule?**
A corrupted length field mid-file can make a record look as if it runs past the end of the file, so it looks like a torn tail and recovery would truncate everything after it. (Listed under limitations. There is also a `TODO` in `read_records` about capping lengths before reading.)

**Explain `write`, `flush`, `fsync`. What does each survive?**
`write` copies into Python's buffer: lost on process crash. `flush` hands bytes to the OS: survives a process crash, not power loss. `fsync` asks the device to persist them: survives power loss, as far as the OS and hardware honour it.

**Why default to `sync=True` if it's slow?**
Safe by default: an acknowledged write is durable. `sync=False` is an explicit opt-in to lose the most recent writes on power loss in exchange for throughput.

**Why big-endian and `struct`?**
A fixed, explicit byte order means files are portable across machines and the format is unambiguous. `struct` with `">IBII"` documents the layout in one line.

**What is the time complexity of each operation?**
`put`/`delete`: O(record size) plus one fsync. `get` and open: O(file size). The next versions exist to fix those two.

**How would you make this safe for two processes?**
It isn't: there's no file lock, and two writers can interleave and corrupt the log. A real fix is an OS-level lock (e.g. a `LOCK` file) taken at open.
