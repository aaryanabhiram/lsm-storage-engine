# V2 design: SSTables

V2 adds immutable sorted files (SSTables) to V1's WAL + memtable. When the memtable gets large it's written out as an SSTable and the WAL records it covered are dropped, so memory and recovery work are bounded by the *unflushed* data rather than by the whole database.

V2 turns `store.py` into a directory-based store (`KVStore`), with `sstable.py` (the table format) and `fsutil.py` (a directory-fsync helper). It reuses the V0/V1 record format and `WriteAheadLog`/`replay`. `sstable.py` also reads and writes the V3 indexed format ([v3-design.md](v3-design.md)), but the V2 store itself always writes the unindexed format described here.

```
PUT/DELETE:  [flush first if the memtable is full] -> WAL append -> memtable update
GET:         memtable -> SSTables newest to oldest; the first entry found (value or tombstone) decides
OPEN:        drop unfinished tables -> check SSTable footers -> replay surviving WAL segments into the memtable
```

## API

```python
from lsm_store import KVStore

with KVStore("db_dir", sync=True, memtable_limit_bytes=4 * 1024 * 1024) as db:
    db.put(b"k", b"v"); db.get(b"k"); db.delete(b"k")
    db.flush()      # force the memtable into an SSTable now
```

Value semantics are the same as V1 (bytes only; `b""` is a value; `delete` appends a tombstone even for an absent key; use after `close()` raises `ValueError`; `close()` is idempotent). **The path is a directory** (V0/V1 use a single file). `memtable_limit_bytes` is the flush threshold on the memtable's payload (sum of key and value bytes; tombstones count their key). It excludes Python object overhead, so real memory use is a multiple of it. The 4 MiB default is a starting value, not a tuned one.

## Directory layout

```
db_dir/
  wal-00000003.log     WAL segments: V1 records, mutation order within and across segments (ids ascending)
  wal-00000005.log
  sst-00000002.sst     immutable SSTables (ids ascending = oldest to newest)
  sst-00000004.sst
```

Segment and table ids come from one counter (`max(existing ids) + 1`), so every file has a unique id and a higher id is always newer. No file is ever modified after it's complete, except the active WAL segment, which is append-only. `*.tmp` files are unfinished SSTable builds.

The table set is simply "the files named `sst-*.sst`", and a table appears under that name only when it's complete (see below).

## SSTable format

```
record_1 | ... | record_n | footer
```

- **Records:** the V0/V1 record, `crc32 | op | key_len | value_len | key | value` (13-byte header, big-endian; see [v0-design.md](v0-design.md)). PUT carries the value (possibly empty), and DELETE (`op = 2`) is a tombstone. Keys are in strictly ascending order with one record per key, so a tombstone and an empty value stay distinct and a key never appears twice.
- **Footer (28 bytes, big-endian):** `magic "LSMSST01" (8) | record_count (8) | data_len (8) | crc32 (4)`; the CRC covers the first 24 footer bytes, and `data_len` is the byte length of the record region.

Why a footer: The V0 record reader treats a short read as a torn tail and stops quietly, which is correct for a log but would let a truncated table pass for a shorter valid one. The footer says exactly how long and how large the record region must be, so any truncation, extension or miscount is detected. Per-record CRCs (reused, not reinvented) detect damaged bytes.

**Verification:** at open, each table's footer is checked (cheap, one small read) and must agree with the file size. Record CRCs and key order are verified on every read of those records, and a scan that reaches the end also checks the record count and total length. There's no whole-file verification at open, which would make startup proportional to the total size of all tables. Anything that fails raises `CorruptionError`. A bad table is never skipped, because the WAL records it replaced may already be gone.

**Lookup:** a sequential scan from the start of the file that stops at the first key greater than the target (possible because keys are sorted), or at the end. Cost per table is O(table size) in the worst case.

## Flush protocol

`flush()` is called automatically before a write when the memtable payload is at or over the limit, so a failed flush never leaves a half-applied write (the memtable can exceed the limit by up to one write):

1. **Build and install the SSTable.** Write `sst-N.sst.tmp`, flush and **fsync**, `os.replace` it to `sst-N.sst`, and fsync the directory (POSIX). The fsync happens regardless of the store's `sync` flag. *This rename is the commit point.* Before it, nothing has changed and the WAL still holds every record.
2. **Switch WAL segment.** Create `wal-M.log` (M newer than N), fsync the directory, make it the active WAL, give the store an empty memtable, and close the old segment (flush + fsync).
3. **Drop covered segments.** Delete every WAL segment with an id below M, **oldest first**.

`close()` doesn't flush. The WAL covers the memtable, and reopening replays it.

## Invariants

- **I1 (WAL covers the memtable).** The memtable equals the last-value-wins merge of the WAL segments present in the directory, replayed in id order.
- **I2 (SSTable covers the segments it replaced).** An SSTable is built from the whole memtable, so by I1 it contains the final state of every segment that existed when it was built.
- **I3 (surviving segments are a suffix).** Segments are deleted oldest first, and only after the SSTable covering them is durable. So the segments left at any moment are the newest ones, and replaying a suffix gives, for the keys it mentions, exactly the state the SSTables hold or newer. A deleted segment can therefore never leave an older value alive in the WAL that contradicts a newer table. (Deleting newest first would break this: an older segment could survive and resurrect an overwritten value. A test covers the order.)
- **I4 (read order).** For any key the newest state is the memtable entry if present (a value, `b""`, or a tombstone), otherwise the entry in the highest-id SSTable that has one. A tombstone is an entry, so it stops the search. Tombstones are never dropped, so a deleted key can't reappear from an older table.
- **I5 (no acknowledged write is lost).** A mutation returns only after its WAL record is flushed (and fsynced when `sync=True`). It leaves the WAL's protection only after its data is in an fsynced, renamed SSTable.

## Crash behavior

| crash point | files left behind | what recovery does | result |
|---|---|---|---|
| while building the table | `sst-N.sst.tmp`, old WAL intact | delete `.tmp`, replay WAL | nothing lost; flush happens again later |
| after the rename, before the new WAL segment | `sst-N.sst` and the old segment | replay the old segment (redundant with the table), start a new segment | nothing lost; the table and memtable agree (I2, I4) |
| after the new segment, before deletions | `sst-N.sst`, old segment(s), new segment | replay all segments in order | nothing lost; the redundant segments go at the next flush |
| during deletions | `sst-N.sst`, a suffix of the old segments, new segment | replay the suffix (I3) | nothing lost, no stale value resurrected |
| mid-append to the active segment | torn trailing record in the newest segment | truncate to the last valid record (as V1) | the unacknowledged record is dropped |

Only the newest WAL segment may end in a torn record. Older segments were closed with an fsync before a newer one existed, so a truncated record in one of them means data was lost, and recovery raises `CorruptionError` instead of repairing it. A complete record with a bad checksum raises in any segment.

## Durability notes

- With `sync=True` the per-mutation durability is V1's. With `sync=False`, mutations survive a process crash but not necessarily power loss. A flush additionally makes everything in the memtable durable, since it fsyncs the table.
- Directory entries (new files, renames) are fsynced on POSIX via `fsync_dir`. **On Windows `fsync_dir` is a no-op** (a directory can't be opened for fsync), so the durability of creations and renames there relies on the filesystem journal, and nothing stronger is claimed. V0 and V1 never fsynced directories.
- `os.replace` is atomic on the platforms used, so a file with a final name is always complete.

## Known limitations

- **GET cost grows with the number of tables.** Every table that may hold the key is scanned from its start, and a miss scans every table to its end or past the key's position.
- **Tables only accumulate.** Stale versions and tombstones are never reclaimed, in tables or anywhere else, and the number of files grows without bound.
- **Flush stalls the writer.** It runs synchronously inside the triggering write, and there's no background thread.
- **Memtable size is approximate.** The threshold counts payload bytes, not Python memory.
- **Startup** replays the WAL segments that remain (unflushed data plus any not yet deleted) and reads one footer per table.
- **A table deleted or replaced externally isn't detected** if its name disappears entirely (nothing records which files should exist), though damage inside a present table is.
- A corrupted length field in the WAL still looks like a torn tail (inherited from V0).
- Single process, single writer; no locking; not thread-safe.

## Code walkthrough

Two files carry V2: `sstable.py` (one immutable sorted file) and `store.py` (the coordinator).

**1. `sstable.write_sstable(path, items)` writes a table safely.** `items` is already sorted. The sequence is the whole crash-safety story:

```python
tmp = path + ".tmp"
with open(tmp, "wb") as f:
    ...write records, then footer...
    f.flush(); os.fsync(f.fileno())
os.replace(tmp, path)            # atomic: the final name is either absent or complete
fsync_dir(os.path.dirname(path) or ".")
```

It rejects out-of-order keys (`key <= prev_key`) and refuses to write an empty table. On any exception it removes the `.tmp`.

**2. The footer** is `magic | record_count | data_len | crc32`. `SSTable.__init__` checks magic, CRC, and that `data_len` equals `file size - footer size`.

**3. `SSTable.lookup(key)`** scans from the start with `read_records` (through `_Bounded`, which stops the reader at `data_len` so it can't run into the footer) and stops early once it sees a key greater than the target. It returns a value, `None` (tombstone), or the sentinel `NOT_FOUND`. The sentinel exists because `None` already means "deleted".

**4. `KVStore.get`** is the read order from invariant I4:

```python
if key in self._memtable:
    return self._memtable[key]          # None == tombstone
for table in reversed(self._sstables):  # newest first
    found = table.lookup(key)
    if found is not NOT_FOUND:
        return found
return None
```

**5. `KVStore.flush`** is the three-step protocol from the doc. Read it with the doc beside you:

1. `write_sstable(sst_path, sorted(self._memtable.items()))` (the commit point is the rename inside).
2. Create the new WAL segment, `fsync_dir`, swap `self._wal`, empty the memtable.
3. `old_wal.close()` then `_delete_segments_before(new_id)`, which removes segments **oldest first**.

**6. `put`** calls `_flush_if_full()` *before* appending to the WAL, so a failed flush can't leave a half-applied write.

**7. Recovery** lives in `__init__`, `_replay_segments` and `_replay_closed_segment`. Only the newest segment may have a torn tail (`replay` repairs it). Older segments must be complete, otherwise `CorruptionError`.

## Interview questions

**Why write the memtable out as a *sorted* immutable file?**
Sorted keys let a reader stop early and later enable binary search (V3), merging (V6) and range scans. Immutability means no locking for readers, no in-place update hazards, trivially cacheable (V5), and crash safety reduces to "is the file complete or not".

**Why write to a `.tmp` file and rename?**
`os.replace` is atomic, so a file with the final name is always complete. A crash mid-build leaves only a `.tmp`, which recovery deletes. The rename is the commit point.

**Why fsync the table even when `sync=False`?**
Because after the flush the WAL records covering it get deleted. If the table weren't durable, deleting the WAL would lose data. The flush has to be durable regardless of the per-write setting.

**Why delete WAL segments oldest first?**
So whatever survives a crash is always a *suffix* (the newest segments). Replaying a suffix can't resurrect an older value that a newer table has overwritten. Deleting newest first could leave an old segment holding `k=1` while a later (already deleted) segment held `k=2`: replay would bring back `k=1`. A test covers this.

**Why does the SSTable have a footer when records already have CRCs?**
The record reader treats a short read as a "torn tail" and stops quietly. That's right for a log but would let a *truncated* table pass as a shorter valid one. The footer states the exact record count and length, so truncation, extension and miscounts are detected.

**Why not verify every table fully at open?**
That would make startup proportional to total data size. Open checks only the footer (one small read), and record CRCs are checked whenever those records are read.

**Why must tombstones be kept in SSTables?**
A tombstone in a newer table has to hide a value in an older one. Dropping it would make the old value reappear. (V6 shows when it *is* safe to drop.)

**Why is `NOT_FOUND` a sentinel object instead of `None`?**
`None` already means "tombstone": the key was deleted and the search must stop. `NOT_FOUND` means "this table knows nothing, keep looking in older tables".

**Why does a bad SSTable raise instead of being skipped?**
The WAL records it replaced may already be deleted. Skipping it would silently lose data. Failing loudly is the only safe option.

**What happens if the process crashes between creating the table and deleting old WAL segments?**
Nothing is lost: on reopen the old segments replay into the memtable (redundantly with the table), and the next flush cleans them up. The crash table in the doc lists every window.

**Why does `fsync_dir` do nothing on Windows?**
A directory can't be opened for fsync there. So rename durability relies on the filesystem journal, and the doc claims nothing stronger. On POSIX, a new file's *name* is not durable until its directory is fsynced.

**What does GET cost now, and why is that a problem?**
Memtable lookup, then a scan of every table that could hold the key, and a miss scans all of them. It grows with both table count and table size. V3 fixes the size part, V4 the count part, V6 the count itself.
