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
