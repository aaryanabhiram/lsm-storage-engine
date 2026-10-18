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

## Code walkthrough

V4 adds `bloom.py` (42 lines) and wires it into `sstable.py`.

**1. `bloom.py` has four small functions.**

| function | job |
|---|---|
| `num_hashes(bits_per_key)` | `round(b * ln 2)`, clamped to 1..32 |
| `filter_bytes(count, bits_per_key)` | filter size in bytes, minimum 8 |
| `_positions(key, m, k)` | the `k` bit positions for a key |
| `add` / `may_contain` | set bits / test bits |

**2. `_positions` is double hashing with a deterministic hash:**

```python
h1, h2 = _HALVES.unpack(hashlib.blake2b(key, digest_size=16).digest())
h2 |= 1
for i in range(k):
    yield (h1 + i * h2) % m
```

One 16-byte digest becomes two 64-bit halves; `h2 |= 1` forces it odd so the stride doesn't collapse to a short cycle. Python's built-in `hash()` is deliberately not used, because it's randomised per process and the bits are stored on disk.

**3. `may_contain` returns `False` at the first clear bit.** That's why a typical negative costs about two probes.

**4. `write_sstable(..., bloom_bits_per_key=b)` fills the filter as records are written.** It needs the key count up front to size the bitset, which is why it materialises non-list inputs (and why V6 later passes `expected_count`). It adds **every** key, tombstones included:

```python
bloom.add(bits, key, k)  # PUT and DELETE alike: a tombstone is a version of the key
```

**5. The V4 footer** adds `filter_len`, `num_hashes` and `filter_crc`. `_open_v4` validates `1 <= k <= 32`, at least one filter bit per key, then loads and CRC-checks the filter block into `self._bloom`.

**6. `SSTable.may_contain(key)` and `lookup`:**

```python
def may_contain(self, key):
    return self._bloom is None or bloom.may_contain(self._bloom, key, self.num_hashes)
...
if not self.may_contain(key):
    return NOT_FOUND            # no file access at all
```

A V2/V3 table has `_bloom is None`, so it always answers "maybe".

**7. `_scan` (used by `items()`) rebuilds the filter from the keys it reads** and compares it to the stored bytes. That catches a filter that has valid CRCs but doesn't match the table.

## Interview questions

**What problem does a Bloom filter solve here?**
V3 still reads a block from every table a missing key falls within. A Bloom filter lets most tables answer "definitely not here" from memory, with no file access. It saves the most for lookups of keys that aren't in a table.

**Can a Bloom filter give false negatives? Why not?**
No. Adding a key sets its `k` bits, and bits are never cleared, so an added key always sees all its bits set. False positives are possible and just cost one wasted block read.

**Why must tombstones go into the filter?**
If a deleted key were missing from its table's filter, a GET would skip that newer table and fall through to an older table that still holds the value, resurrecting the deleted key.

**How did you choose 10 bits per key and 7 hashes?**
The optimal number of hashes for `b` bits per key is `b * ln 2`, so 10 bits gives about 7. The false-positive rate is then `(1 - e^(-k/b))^k`, about 0.82%. Ten bits is about 1.25 bytes per key, around 1% of a ~111-byte record. It's described as a starting point, not tuned (see the source TODO).

**Why not use Python's `hash()`?**
It's randomised per process (`PYTHONHASHSEED`). The filter is persisted, so the same key must map to the same bits in every run. A test checks filter bytes are identical across seeds and match a pinned digest.

**Why double hashing instead of `k` independent hashes?**
Kirsch and Mitzenmacher showed that deriving `k` positions from two hashes keeps the same asymptotic false-positive rate while costing one hash call. Here, one BLAKE2b digest is split in two.

**Why `h2 |= 1`?**
An odd stride avoids degenerate position cycles (an even stride can only reach a subset of positions when `m` has factors of 2).

**Why is the filter built per SSTable rather than one global filter?**
Tables are immutable, so each filter is built once and never updated or invalidated. A global filter would need deletes (which Bloom filters can't do) and rebuilding on every compaction.

**What breaks if you change the hash construction later?**
Every stored filter would silently give wrong answers, including false negatives. That requires a new format magic, which is why the construction is pinned by a test.

**What stops an old table without a filter from being read as "empty filter"?**
`_bloom is None` marks "no filter", and `may_contain` returns `True` for it. An empty filter would answer "no" for every key and lose data.

**What does a Bloom filter *not* help with?**
Keys that are present (a hit still reads one block), range scans, and it can't shrink the number of tables. Filter memory also grows with total keys.

**How would you test that a "no" really skips the file?**
Count calls through the single `_open_table` function. The tests assert zero opens, which is structural evidence rather than a timing guess.
