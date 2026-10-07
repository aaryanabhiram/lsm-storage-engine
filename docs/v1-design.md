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
