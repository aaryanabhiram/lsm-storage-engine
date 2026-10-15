# V4 design: Bloom filters

V3 answers "where in this SSTable should I look?". V4 answers "do I need to look in this SSTable at all?". Every SSTable gets a Bloom filter over its keys, and a GET asks the filter first and skips the table when the filter says the key is definitely absent. The sparse index stays: it still selects the block when the filter says "maybe". Nothing else changes: the WAL, memtable, synchronous flush, recovery and immutability rules are V2's ([v2-design.md](v2-design.md)), and the index is V3's ([v3-design.md](v3-design.md)).

V4 changes only the table format that the store's flushes write: `KVStore` in `store.py` gains a `bloom_bits_per_key` option. `bloom.py` holds the filter primitives; `sstable.py` reads all three formats. The V2 and V3 versions of `store.py` still write their own formats.

```
GET:  memtable -> SSTables newest to oldest; for each table:
        Bloom filter --NO--> skip (no file access)
                     --MAYBE--> binary-search the index -> one block -> read and scan that block
```

## API

```python
from lsm_store import KVStore

with KVStore("db_dir", sync=True, memtable_limit_bytes=4 * 1024 * 1024,
               index_interval=16, bloom_bits_per_key=10) as db:
    ...
```

`bloom_bits_per_key` (1..64, default 10) applies to tables written from then on. Each table records its own filter size and hash count in its footer, so tables with different settings coexist. A directory may hold V2, V3 and V4 tables together.

## The filter

A Bloom filter is a bitset plus `k` hash positions per key. Adding a key sets its `k` bits. A query returns **no** if any of the key's bits is clear and **maybe** otherwise.

- **No false negatives.** Every key that was added has all its bits set, and bits are never cleared, so an added key always gets "maybe". A table is skipped only when the key was never added to it.
- **False positives are allowed.** An absent key gets "maybe" when all its `k` bits happen to be set by other keys. This costs one wasted index search and block read, but the answer is still correct, because the block scan finds nothing and returns `NOT_FOUND`.
- **Tombstones are added.** The filter holds every key in the table, PUT and DELETE alike. A tombstone is the table's newest statement about a key, and if it were left out, a GET could skip the newer table and find an older value in a lower table, resurrecting a deleted key.

### Parameters and cost model

| quantity | value | reason |
|---|---|---|
| bits per key `b` | 10 (default) | ~1.25 bytes per key, about 1% of a ~111-byte record, for a false-positive rate near 1% |
| hash functions `k` | `round(b * ln 2)` = 7 | the `k` minimising the false-positive rate for a given `b` |
| expected false-positive rate | `(1 - e^(-k/b))^k` = **0.82%** | formula for `m/n = 10`, `k = 7` (8 bits: 2.2%; 12 bits: 0.31%) |
| filter size | `max(8, ceil(count * b / 8))` bytes | rounded to whole bytes, minimum 64 bits so tiny tables are not degenerate |

At the default memtable limit (4 MiB, 111-byte payload entries in the benchmark workload) a flushed table has 37,787 records and a **47,234-byte** filter. A 10M-record database has about 12.5 MB of filters in total, held in memory like the index.

**CPU per query:** one BLAKE2b hash of the key, then at most `k` = 7 position computations and bit tests. The query stops at the first clear bit, so a typical negative needs about two probes. This work is independent of table size and is small next to opening a file and decoding a block.

10 bits per key is a starting point, not a tuned value; what it buys is measured in [benchmarks.md](benchmarks.md#v4-benchmark).

### Deterministic hashing

The bits are persisted, so a key must map to the same positions in every process. Python's `hash()` is randomized per process and isn't used. `bloom.py` takes one `hashlib.blake2b(key, digest_size=16)` digest, splits it into two 64-bit integers `h1` and `h2` (`h2` forced odd), and uses position `i = (h1 + i * h2) mod m` for `i` in `0..k-1`. Deriving `k` positions from two hashes (double hashing, Kirsch and Mitzenmacher) gives the same asymptotic false-positive rate as `k` independent hashes and costs one hash call. A test checks the filter bytes are identical across processes with different `PYTHONHASHSEED` values and match a pinned digest, because changing the construction would silently invalidate every stored filter and would need a new format magic.

## Table format (V4)

```
record_1 | ... | record_n | index block | filter block | footer
```

- **Records** and **index block:** exactly V3's.
- **Filter block:** `filter_len` bytes; bit `p` is bit `p & 7` of byte `p >> 3`.
- **Footer (60 bytes, big-endian):** `magic "LSMSST03" (8) | record_count (8) | data_len (8) | index_len (8) | interval (4) | index_crc (4) | filter_len (8) | num_hashes (4) | filter_crc (4) | crc32 (4)`. The last CRC covers the first 56 footer bytes, and `index_crc` and `filter_crc` cover their blocks. The index starts at `data_len`, and the filter at `data_len + index_len`.

The filter lives inside the table, so one temp-file build, fsync and atomic rename installs records, index and filter together. A table is built in `path + ".tmp"`, fsynced, then renamed, as in V2/V3, and a `.tmp` is deleted at open. The filter is built while the records are written, so it needs the record count first: a flush passes a list, and any other iterable is materialised.

### Format detection and compatibility

`SSTable` tries the V4, V2 and V3 footers and accepts a format only when its magic and its footer CRC both check out. (A magic check alone would let a V3 table whose last key ends in `LSMSST03...` be mistaken for V4.) If none validates, the file is `CorruptionError`.

| table | filter | lookup |
|---|---|---|
| V2 (`LSMSST01`) | none | full scan, as before |
| V3 (`LSMSST02`) | none | sparse index, as before |
| V4 (`LSMSST03`) | yes | Bloom, then sparse index on "maybe" |

An old table is never read as "an empty filter" (which would say "no" to every key). `SSTable.may_contain` returns `True` for a table without a filter, and `_bloom is None` marks it.

## Lookup

`SSTable.lookup(key)`:

1. `may_contain(key)`: if the table has a filter and it says no, return `NOT_FOUND` immediately. The record region isn't opened and the index isn't searched.
2. Otherwise V3's path: `locate` (binary search of the in-memory index), then read and scan only the selected block.

Result semantics are unchanged: a value (possibly empty), `None` for a tombstone, or `NOT_FOUND`, and the first non-`NOT_FOUND` across tables, newest first, decides. A false positive yields the same result as a table with no filter.

### Structural observability

Reads of the record region go through one function, `sstable._open_table`, and the Bloom check, index search and block scan are the separate methods `may_contain`, `locate` and `_scan_block`. Wrapping them gives, per GET, the Bloom checks, tables skipped, tables searched, blocks scanned, records decoded and bytes read. A Bloom false positive is a "maybe" whose block scan returns `NOT_FOUND`. `SSTable.filter_len` and `SSTable.index_len` give the byte sizes of the two blocks. The tests use these seams to show structurally, not by timing, that a Bloom "no" performs no file access.

## Corruption and crash safety

Construction and atomicity are V2/V3's. A file with a final name is complete.

Verified at **open** (cost proportional to index plus filter size): footer magic and CRC; `data_len + index_len + filter_len + footer == file size`; the V3 index checks; `1 <= num_hashes <= 32`; at least one filter bit per key; the filter block's length and CRC. Any failure raises `CorruptionError`, and a store refuses to open with a damaged table rather than skipping it, because the WAL that covered it may already be gone.

Verified by a **full scan** (`items()`): everything V3 checks, and in addition the filter is rebuilt from the keys read and must equal the stored bytes. This catches a filter that is internally consistent (valid CRCs) but doesn't match the table, for example zeroed bits or a different hash count, which would otherwise produce false negatives.

## Limitations

- A forged or buggy filter with valid CRCs passes `open` and is detected only by a full scan, and a lookup trusts a filter that passed the open-time checks. CRCs detect accidental damage, not a deliberately rewritten table.
- Filters are held in memory for every table, about `b` bits per key in total, and they're loaded at open, so startup also grows with total filter size.
- The filter answers per-key membership only. It doesn't help range reads (there are none yet) and does nothing for keys that are present: a hit still reads one block.
- Table count and total filter memory keep growing, and every GET still evaluates one filter per table.
- Bits per key is fixed per table at write time; changing the setting doesn't rewrite existing tables.
- `sync`, flush stalls, single-writer limits and Windows directory-fsync caveats are as in V2.
