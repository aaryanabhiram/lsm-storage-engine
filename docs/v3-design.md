# V3 design: sparse index

V3 adds a sparse index to SSTables so a lookup reads one small region of a table instead of scanning it from the start. Nothing else changes: the WAL, memtable, synchronous flush, recovery and immutability rules are V2's ([v2-design.md](v2-design.md)).

V3 changes only the table format that the store's flushes write: `KVStore` in `store.py` gains an `index_interval` option. `sstable.py` reads both formats. V2 tables have no index and are scanned sequentially.

```
GET:  memtable -> SSTables newest to oldest; for each indexed table:
        binary-search the in-memory index -> one block -> read and scan only that block's bytes
```

## API

```python
from lsm_store import KVStore

with KVStore("db_dir", sync=True, memtable_limit_bytes=4 * 1024 * 1024, index_interval=16) as db:
    ...
```

`index_interval` is the number of records per index block (default 16, a starting value, not a tuned one). It applies to tables written from then on, and each table records its own interval in its footer, so tables with different intervals can coexist. A directory may hold V2 (unindexed) and V3 tables together, e.g. after opening a V2 database with `KVStore`, and a V2 table is still scanned in full.

## Table format (V3)

```
record_1 | ... | record_n | index block | footer
```

- **Records:** unchanged (V0/V1 record, strictly ascending keys).
- **Index block:** one entry for every `interval`-th record (records 0, N, 2N, ...), each `key_len (4) | offset (8) | key`, big-endian. `key` is that record's key, and `offset` is where the record starts in the file. Entry *b* therefore describes **block b**: records `b*N .. b*N+N-1`, the byte range from `offset[b]` to `offset[b+1]` (the last block ends at `data_len`).
- **Footer (44 bytes):** `magic "LSMSST02" (8) | record_count (8) | data_len (8) | index_len (8) | interval (4) | index_crc (4) | crc32 (4)`. The last CRC covers the first 40 footer bytes, and `index_crc` covers the index block. The index block starts at `data_len`.

V2 tables end in a 28-byte footer with magic `LSMSST01`. The reader tries that footer first and falls back to the V3 footer, so both are recognised and anything else is `CorruptionError`.

The index lives inside the table, not in a separate file, so a table and its index are installed by one rename and can never disagree through a crash. A separate index file would need its own atomic-pair protocol.

## Sampling policy

Every `interval` records (record count, not bytes). The maximum scan work per table is therefore at most `interval` records, and in bytes it's bounded by `interval` times the largest record. Entries are `ceil(count / interval)`, so a table smaller than the interval has one entry (its first key), and the index is about `1/interval` the size of the key set. The index is kept in memory (a sorted list of first keys plus offsets) and loaded at open. It isn't a per-key directory.

## Lookup

`SSTable.locate(key)` does `bisect_right` on the index keys:

- `key` < first key of the table: not present; **no file access**.
- otherwise block *b* = the last entry whose key is <= `key`. If the key exists it's in block *b*, because block *b+1* starts with a larger key. `key` equal to an indexed key selects the block it starts, and a key after the last record selects the last block (and misses there).

`lookup` then opens the file and reads only bytes `[offset[b], end)` through the existing record reader, stopping at the first key >= the target. Result semantics are V2's: value, tombstone (`None`), or `NOT_FOUND`, and the first non-`NOT_FOUND` result across tables decides.

## Corruption and crash safety

Table construction is V2's: write `.tmp` (records, index, footer), fsync, rename. A file with a final name is complete and self-consistent, and a `.tmp` is deleted at open.

Verified at **open** (cost proportional to index size, not table size): footer CRC and magic; `data_len + index_len + footer == file size`; index CRC; every entry parses; entry count equals `ceil(count / interval)`; first offset is 0; keys and offsets strictly ascending; all offsets inside the record region.

Verified at **lookup**, within the block read: record CRCs, key order, the first record's key equals the index key, no key reaches the next block's first key, and (when the scan runs to the block's end) the block holds exactly the expected number of records and bytes. A full scan (`items()`) additionally checks every index entry against the record at its offset. Any failure raises `CorruptionError`, and nothing is silently skipped or answered wrongly from a bad index. As in V2, a lookup that returns early (target found or passed) doesn't verify the rest of the block, and records outside the candidate block aren't read at all.

## Limitations

- A miss still costs one block read per table (plus the index search) across all tables, and the table count still grows.
- Opening reads each table's footer and index block; startup grows with total index size.
- The index holds a key per `interval` records in memory, which isn't negligible with large keys or a small interval.
- Corruption inside a block that a lookup never reads is detected only by a scan that reaches it.
- `sync`, flush stalls, single-writer limits and Windows directory-fsync caveats are as in V2.

## Code walkthrough

All of V3 is in `sstable.py` (plus one new constructor option in `store.py`).

**1. `write_sstable(..., index_interval=N)` builds the index while writing records.** Before each record whose position is a multiple of the interval, it remembers `(key_len, byte offset, key)`:

```python
if index_interval is not None and record_count % index_interval == 0:
    index_entries.append(_INDEX_ENTRY_HEADER.pack(len(key), serialized_length) + key)
```

`serialized_length` is the running byte offset of the next record, so it needs no second pass. The index block and a bigger footer (with `interval` and `index_crc`) are written after the records, and everything still goes through `.tmp` -> fsync -> rename.

**2. `SSTable.__init__` detects the format.** It tries each footer size and accepts one only if magic *and* footer CRC both match (see the interview question below for why). Then `_open_v2` or `_open_v3` runs.

**3. `_load_index` parses the index block into two parallel lists, `self._keys` and `self._offsets`,** and sanity-checks them: entry count is `ceil(count / interval)`, first offset is 0, keys and offsets are strictly ascending, offsets lie inside the record region.

**4. `locate(key)` is a binary search:**

```python
b = bisect.bisect_right(self._keys, key) - 1
if b < 0:
    return None                       # smaller than the table's first key: no file access
end = self._offsets[b + 1] if b + 1 < len(self._offsets) else self.data_len
return b, self._offsets[b], end
```

`bisect_right - 1` is "the last index key <= target", which is the only block that can hold the key.

**5. `_scan_block(block, start, end)` reads only `[start, end)`** (`raw.seek(start); raw.read(end - start)`) and decodes it. While decoding it checks that the first key equals the index key, keys ascend, no key reaches the next block's first key, and (if the scan finishes) the record count and byte length match the index.

**6. `lookup` chooses the path:** unindexed (V2) table -> `_scan()`; indexed -> `locate` then `_scan_block`.

## Interview questions

**Why a *sparse* index instead of one entry per key?**
A per-key index is as big as the key set and defeats the point of having a compact in-memory structure. Indexing every Nth record makes the index about 1/N of the keys while bounding the scan to at most N records.

**How do you pick `index_interval`?**
It's a space/time trade: a larger interval means a smaller index but more records scanned per lookup; a smaller one means the opposite. 16 is a starting value, not a tuned one (the source says so). A real choice would measure lookup latency against index memory on the target workload.

**Why does sparse indexing only work on *sorted* data?**
The binary search relies on order: if block `b` starts with a key <= target and block `b+1` starts with a larger key, the target can only be in block `b`.

**Why is the index stored inside the table file and not in a separate file?**
One rename installs records and index together, so they can never disagree after a crash. Two files would need their own atomic-pair protocol.

**Why record the interval in each table's footer?**
So tables written with different settings can coexist, and settings can change over time without rewriting old tables.

**How does the store stay compatible with V2 tables?**
The reader recognises both footers. A V2 table has `interval = None` and is scanned in full. No migration is required.

**Why check footer CRC as well as magic when detecting the format?**
Table data precedes the footer, and a user key could end in bytes that look like another format's magic. A magic match alone could misclassify a table. Magic plus a valid CRC makes that practically impossible.

**What does a lookup verify, and what doesn't it?**
It validates record CRCs, key order and consistency with the index within the block it reads. It doesn't read other blocks, and it can return early without checking the rest of its own block. Full-table checks happen in `items()`.

**What does a *miss* cost in V3?**
For each table the key falls within, one index search plus one block read, even though the key isn't there. Table count still multiplies this. That is exactly the problem V4 addresses.

**What happens if the index is corrupted?**
The index CRC is checked at open, so damage is caught before any lookup uses it. Consistency checks catch a self-consistent but wrong index when a block is read (first key mismatch, overlap with the next block).

**Why is the index kept in memory rather than read from disk each time?**
Avoiding a disk read per lookup is the point. The cost is memory proportional to keys/interval and startup time proportional to total index size.
