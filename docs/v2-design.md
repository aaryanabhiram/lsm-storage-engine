# V2 design: SSTables

V2 adds immutable sorted files (SSTables) to V1's WAL + memtable. When the memtable gets large it's written out as an SSTable and the WAL records it covered are dropped, so memory and recovery work are bounded by the *unflushed* data rather than by the whole database.

V2 turns `store.py` into a directory-based store (`KVStore`), with `sstable.py` (the table format) and `fsutil.py` (a directory-fsync helper). It reuses the V0/V1 record format and `WriteAheadLog`/`replay`.

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
