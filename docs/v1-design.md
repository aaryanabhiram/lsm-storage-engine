# V1 design: write-ahead log + memtable

V1 is a **transitional** version. It changes the read path from "scan the whole file" (V0) to "dictionary lookup in memory", while the on-disk file stays an append-only log.

```
V0:  GET -> sequentially scan the entire append-only file
V1:  open -> replay WAL into memtable;   GET -> dictionary lookup in the memtable
```

V1 reshapes V0's store: `KVStore` in `store.py` now serves reads from an in-memory memtable and is backed by the write-ahead log in `wal.py`.

## API

```python
from lsm_store import KVStore

with KVStore("data.wal", sync=True) as db:
    db.put(b"k", b"v")
    db.get(b"k")        # -> b"v"; None if absent or deleted
    db.delete(b"k")     # appends a tombstone
```

Same contract as V0: keys and values are `bytes` (else `TypeError`), empty keys and values are allowed, use after `close()` raises `ValueError`, and `close()` is idempotent.

## Components

**WAL (`wal.py`)** is the persistent source of truth: an append-only file of PUT/DELETE records in mutation order. It reuses the V0 record format unchanged (`crc32 | op | key_len | value_len | key | value`, see [v0-design.md](v0-design.md)), so a given mutation sequence produces byte-identical output in V0 and V1. `replay(path)` rebuilds state, and `WriteAheadLog` appends.

**Memtable** is a plain `dict[bytes, bytes | None]`, one entry per distinct key:

| entry | meaning |
|---|---|
| `bytes` value (including `b""`) | live value |
| `None` | tombstone (deleted) |
| key missing | no information about the key |

Code never uses truthiness to tell `b""` from `None`, because `b""` is a real value. No ordered structure is used, since V1 never iterates in key order.

## Write ordering

```
validate input -> append record to WAL -> flush -> fsync (if sync=True) -> update memtable -> return
```

Invariant: **if a mutation is visible in the memtable, its WAL append completed.** If the write, flush or fsync raises, the exception propagates before the memtable is touched. Tests inject write, flush and fsync failures and check the memtable is unchanged.

One consequence worth knowing: if `fsync` fails after the bytes reached the OS, the record may still be in the file and will appear after a restart even though the call raised. The failed call's outcome is therefore "unknown", not "did not happen".

## GET path

`get` validates the key and does `memtable.get(key)`. It never opens or reads the WAL (a test makes any WAL read or file open raise during `get`). The expected cost is an O(1) dictionary lookup. Measured latency against V0 is in [benchmarks.md](benchmarks.md#v1-benchmark).

## Recovery

`KVStore(path)`:

1. Start with an empty memtable.
2. Read WAL records in file order, setting `memtable[key] = value` (PUT) or `None` (DELETE). PUT and DELETE are absolute state, so the last record for a key wins, and replaying the same prefix always yields the same memtable.
3. Track the end offset of the last valid record.
4. If the file ends mid-record, truncate it to that offset (and fsync).
5. Only then open the WAL for append.

## Torn tail and corruption

- **Torn tail** (header or body cut short at end of file): the record was never acknowledged, so it's removed. A second open finds a clean file.
- **Corruption** (a *complete* record with a bad checksum, unknown op, or DELETE carrying a value): `CorruptionError` is raised and the file isn't modified. Nothing is silently repaired.

## Durability

Identical to V0. Every mutation writes and flushes to the OS. With `sync=True` (default) it also `fsync`s before returning, and with `sync=False` it doesn't, so recent writes can be lost on power loss or OS crash (but survive a process crash). `close()` always flushes and fsyncs. The containing directory isn't fsynced.

## Delete / tombstone semantics

`delete` always appends a tombstone, even when the key is absent, and stores `None` in the memtable. This matches how a tombstone must later suppress older values in persistent files. A later `put` replaces the tombstone. Tombstones are kept in the memtable and survive replay.

## Empty values

`b""` is a legitimate value, encoded as a PUT with `value_len = 0`, distinct from DELETE both on disk (`op`) and in memory (`b""` vs `None`).

## Known limitations

- Transitional design: the whole dataset lives in memory.
- The WAL grows without bound; stale versions and tombstones are never reclaimed.
- The memtable grows with the number of distinct keys (including tombstoned keys) and the whole dataset must fit in memory.
- Startup replay is O(WAL size).
- Single process, single writer; no file locking; not thread-safe.
- Inherited from V0: a corrupted length field mid-file looks like a truncated tail, so recovery would truncate everything after it. A write that fails partway inside a running process can leave a partial record in the WAL, which is only cleaned up on the next open (later appends in the same session land after it).

## Code walkthrough

V1 is small: `store.py` is about 50 lines because `wal.py` does the file work.

**1. `wal.replay(path)` rebuilds state and repairs the file.** It loops over `read_records`, and for each record sets `kv_map[key] = value` (PUT) or `None` (DELETE). It also tracks `good_end`, and if the file is longer than that, truncates and fsyncs.

```python
for r in read_records(f):
    kv_map[r.key] = r.value if r.op == OP_PUT else None
    good_end = r.end_offset
if good_end < os.fstat(f.fileno()).st_size:
    f.truncate(good_end); f.flush(); os.fsync(f.fileno())
```

This is V0's recovery scan plus a dictionary. Because each record is *absolute* state (not "add 1" or "append to"), replaying the same prefix always gives the same result.

**2. `WriteAheadLog.append`** encodes a record, writes it, flushes, and fsyncs if `sync=True`. It's V0's `_append` moved into its own class.

**3. `KVStore.__init__`** is two lines, and the order is the point:

```python
self._memtable = replay(path)            # recover (and repair) first ...
self._wal = WriteAheadLog(path, sync)    # ... then open for append
```

**4. `put` is the write-ordering invariant in code:**

```python
self._wal.append(OP_PUT, key, value)   # may raise: memtable untouched
self._memtable[key] = value
```

If `append` raises, the second line never runs, so nothing is visible that isn't in the log.

**5. `get` is one line**, `self._memtable.get(key)`. `None` covers both "never seen" and "tombstone" (callers can't tell them apart, and don't need to). It never touches the file.

## Interview questions

**What problem does the memtable solve, and what does it cost?**
It turns `get` from O(file size) into an O(1) dict lookup. The cost: the whole dataset (every key *and* value) must fit in RAM, and startup must replay the whole WAL. That's why V1 is called transitional.

**Why is the log called a *write-ahead* log?**
The record is made durable in the log *before* the in-memory state changes. If we crash after the log write, replay reproduces the change. If we crash before it, the change was never acknowledged.

**Why append to the WAL first and update the memtable second? What if you swapped them?**
Swapped, a reader could see a value that was never made durable, and a failed append would leave the memtable ahead of the log. The invariant "visible in the memtable implies its WAL append completed" only holds in the WAL-first order. Tests inject write, flush and fsync failures to check it.

**If `fsync` raises, did the write happen?**
Unknown. The bytes may already be in the OS cache and could appear after a restart even though the call raised. The honest contract is "outcome unknown", not "didn't happen".

**Why `dict[bytes, bytes | None]` and not an ordered structure like a skip list?**
V1 never iterates in key order, so ordering would cost without benefit. (V2 needs sorted output, but it simply sorts the dict once at flush time: `sorted(self._memtable.items())`.)

**Why store `None` as the tombstone rather than removing the key from the dict?**
Removing it would lose the information "this key was deleted", which later stages need to shadow older values on disk. It also keeps replay consistent: the last record for a key wins, even if that's a delete.

**Why must you never use truthiness (`if value:`) on values here?**
`b""` is a legitimate value and is falsy. `None` and `b""` mean different things (deleted vs empty), so code uses `is None` / dictionary membership.

**Why recover (and truncate) *before* opening the WAL for append?**
Otherwise new records would be written after a torn partial record and become unreadable garbage for the next replay.

**Why is the WAL format byte-identical to V0's?**
It lets the same reader/writer be reused, and a test can assert that the same operations produce identical bytes in V0 and V1. It also means the format stays stable while the architecture changes.

**What are V1's scaling limits?**
Memory is proportional to the number of distinct keys plus value bytes. Startup is O(WAL size). The WAL never shrinks, because stale versions and tombstones are never reclaimed.
