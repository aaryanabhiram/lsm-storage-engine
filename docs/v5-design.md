# V5 design: block cache

V4 decides which tables to search (Bloom filters) and which block of a table to read (sparse index). It still goes to the file for every block it needs, even a block it read a moment ago. V5 keeps recently read blocks in memory. Everything else is V4's: the WAL, memtable, synchronous flush, recovery, SSTable formats, Bloom filters and sparse index ([v4-design.md](v4-design.md)).

V5 adds a block cache to `KVStore` in `store.py` (V4 plus the cache). `blockcache.py` holds the cache, and `sstable.py` reads blocks through it. There's **no on-disk change**: the cache is purely in memory, and V2, V3 and V4 tables are read exactly as before.

```
GET:  memtable -> SSTables newest to oldest; for each table:
        Bloom filter --NO--> skip
                     --MAYBE--> binary-search the index -> block b
                                   block cache (table, b) --HIT--> bytes from memory
                                                          --MISS--> read the block from the file, cache it
                                   decode and validate the block's records
```

## API

```python
from lsm_store import KVStore

with KVStore("db_dir", block_cache_bytes=8 * 1024 * 1024) as db:
    ...
    db.cache_stats()   # {"capacity_bytes", "bytes", "entries", "hits", "misses", "evictions", "insertions", "hit_rate"}
```

`block_cache_bytes` (default 8 MiB, about 4,000 blocks of 16 records of the benchmark workload) is a hard bound on cached block bytes. `0` disables the cache. All other arguments are V4's.

## The cache

| question | answer |
|---|---|
| key | `(table path, block index)`. The block index is the position in the table's in-memory sparse index. A byte offset alone would collide across tables. The path contains the table's `sst-NNNNNNNN` id, which the store allocates from a counter that never repeats, and a table is never rewritten, so a key identifies one immutable block. |
| value | The raw bytes of the block (`bytes`, the block's `[start, end)` range of the record region). |
| capacity | Bytes: sum of the cached blocks' lengths. Python object and dictionary overhead are not counted, so actual memory is somewhat larger. A block longer than the whole capacity is never admitted. |
| eviction | LRU: a hit moves the entry to the most-recent end; inserting evicts from the least-recent end until the new block fits. Deterministic, with no randomness or clock. |
| invalidation | None. Tables are immutable, so a cached block cannot become stale. The one exception is damage: see below. |
| restart | The cache lives in the store object and starts empty on every open. |
| threads | None assumed, no locks: single process, single thread, like the store. |

**Why raw bytes.** A hit re-runs the same record decoding and validation (checksums, key order, the index checks) over the cached bytes as a miss runs over the file bytes. A hit therefore returns exactly what a disk read returns, including a `CorruptionError` for a damaged block. The cache saves the file open and read, not the decode. Caching decoded records would also save the decode, but it would skip validation and start to look like a second key-value store.

**Why LRU.** The cache exists to serve repeated access to a working set, and LRU keeps that set resident. A long scan can flush it, but the engine has no range reads yet. The simpler alternative (FIFO) ignores hits, and scan-resistant and adaptive policies (2Q, ARC) add state this project has no workload to justify.

**What is cached.** Blocks of V3 and V4 tables, once a lookup has located them. V2 tables have no blocks (a lookup scans the file from the start), so they bypass the cache rather than cache a whole table. The memtable, the WAL and unfinished `.tmp` tables never enter it.

## Lookup

`SSTable.lookup` is unchanged up to `locate`. `_scan_block(block, start, end)` then:

1. asks the cache for `(path, block)`. On a miss it reads `[start, end)` from the file through `_open_table` (the single physical read) and inserts the bytes if the read returned the full length;
2. decodes and validates records from the bytes, stopping at the matching record or the first larger key, as V4 did.

Result semantics (value, empty value, tombstone, `NOT_FOUND`), newest-table precedence, Bloom behavior and index behavior are unchanged. A Bloom "no" or an index rule-out never consults the cache.

## Corruption

- A table's footer, index and filter are validated at open and aren't cached.
- A short read (a truncated file) isn't cached, so it fails the same way on every lookup.
- A block that raises `CorruptionError` during decoding is removed from the cache, so a damaged block is never served from memory as if valid, and the next access reads the file again.
- Because a lookup stops at the matching record, the tail of a block is validated only when a lookup reaches it. This is V4's behavior and is unchanged: a cached block is decoded exactly as far as a disk read would be.
- A table file changed behind the store's back after a block was cached isn't detected for that block (the same holds for every in-memory structure: index and filter). CRCs detect accidental damage at read time, not concurrent modification.

## Observability

`cache_stats()` reports capacity, current bytes, entry count, hits, misses, evictions, insertions and hit rate. A cache miss on an indexed table is exactly one physical block read, and a hit is none. Reads of the record region still go through `sstable._open_table`, so the structural counters used for V4 (tables consulted, Bloom checks, tables opened, records decoded, bytes decoded) apply unchanged, and tests count physical reads, not time.

## Limitations

- Hits still pay record decoding and checksum verification; only the file open and read are avoided. How much that matters is measured in [benchmarks.md](benchmarks.md#v5-benchmark).
- Only block bytes are counted against the capacity.
- The cache helps only when blocks are revisited. A workload that touches each block once gains nothing and pays for the insertions.
- Hits and misses for the memtable aren't cache events, since a key found in the memtable never reaches it.
- The cache isn't persisted. `sync`, flush stalls and single-writer limits are as in V2.

## Code walkthrough

V5 is `blockcache.py` plus a few lines in `SSTable._scan_block`.

**1. `BlockCache` is an `OrderedDict` used as an LRU.** Keys are `(table_path, block_index)` and values are raw block `bytes`.

```python
def get(self, table, block):
    data = self._entries.get((table, block))
    if data is None:
        self.misses += 1
        return None
    self._entries.move_to_end(key)      # most recently used goes last
    self.hits += 1
    return data
```

**2. `put` evicts from the front until the new block fits:**

```python
while self.bytes + len(data) > self.capacity_bytes:
    _, victim = self._entries.popitem(last=False)   # least recently used
    self.bytes -= len(victim)
    self.evictions += 1
```

A block larger than the whole capacity is rejected (`return False`). Capacity `0` disables the cache.

**3. `SSTable._scan_block` consults the cache before the disk:**

```python
data = self._cache.get(self.path, block)
if data is None:
    data = self._read_block(start, end)           # the only physical read
    if len(data) == end - start:                  # never cache a short read
        self._cache.put(self.path, block, data)
```

Then it decodes and validates the bytes exactly as before, hit or miss.

**4. On `CorruptionError` the block is evicted** (`self._cache.discard(self.path, block)`), so a damaged block is never served from memory as if it were valid.

**5. `KVStore` creates one cache and shares it with every `SSTable`** (`cache=self._block_cache`), and exposes `cache_stats()`.

## Interview questions

**What does the cache store: decoded records or raw bytes? Why?**
Raw block bytes. A hit still decodes and CRC-checks the block, so it returns exactly what a disk read would, including a `CorruptionError` for damage. Caching decoded records would be faster but would skip validation and start to look like a second key-value store.

**What is the downside of caching raw bytes?**
A hit still pays decoding and checksum cost; only the file open and read are saved. The source has a TODO noting this may cost more than the read it saves. The benchmark section measures it.

**Why LRU?**
The cache exists to serve repeated access to a working set, and LRU keeps that set resident, deterministically. FIFO ignores hits. Scan-resistant policies (2Q, ARC) add state that this project has no workload to justify.

**What is LRU's weakness?**
A long sequential scan can flush the working set. The engine has no range reads yet, so it doesn't matter here, but it would if scans were added.

**Why is the cache key `(table path, block index)` and not a byte offset?**
An offset alone collides across tables. The path contains a never-reused table id, and tables are never rewritten, so a key always names one immutable block.

**Why does the cache never need invalidation?**
Tables are immutable, so a cached block can't become stale. The exceptions are damage (handled by eviction on `CorruptionError`) and compaction (which discards the replaced tables' entries just to free capacity).

**Why not cache a short read?**
A truncated file returns fewer bytes. Caching that would mask the problem on later lookups. Not caching it means it fails the same way every time.

**Why is capacity counted in bytes, not entries?**
Block sizes vary with record sizes. Bytes give a real bound (payload only; Python object overhead isn't counted, which the docs state).

**Why is V2 (unindexed) excluded from the cache?**
It has no blocks. Caching would mean caching a whole table.

**Why is the cache empty after every restart?**
It lives in the store object and isn't persisted. Persisting it would add on-disk state and consistency questions for little benefit.

**Is it thread-safe?**
No. The whole store is single-threaded. The source has a TODO: background threads would need a lock around `get`/`put`.

**When does a block cache give no benefit?**
When each block is touched once. Then it only pays for insertions. It also doesn't help requests that never reach a table (memtable hits, Bloom "no").

**How would you evaluate if the cache is worth keeping?**
Compare hit rate and latency with the cache on and off for a skewed workload and a uniform one (the V5 benchmark section reports the measured results). If the hit rate is low or hit latency isn't better than a miss, the added code isn't earning its keep.
