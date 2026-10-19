# lsm-store

A persistent key-value storage engine in Python, built from scratch. It starts from a deliberately simple append-only baseline and evolves toward a log-structured merge-tree (LSM) design. Each architectural change is implemented, tested for correctness, benchmarked under a fixed methodology, and compared quantitatively with the previous version.

## Engineering focus

The project asks one question and answers it with measurements: **how much does each storage-engine technique actually buy, and what does it cost?**

Every technique trades off some of these: write-path cost, read amplification, write amplification, space amplification, memory usage, latency, throughput, durability and implementation complexity. A technique that improves one doesn't improve all of them, so each step is evaluated on the metrics it's expected to move and on the ones it may make worse.

The project covers on-disk record formats, durability and fsync semantics, crash recovery, and performance characterization.

## Current status

**V0 is implemented**: a persistent bytes-to-bytes store backed by a single append-only data file.

- `put` and `delete` append checksummed records (CRC-32 per record); `delete` appends a tombstone.
- `get` scans the whole file and returns the newest state of the key. There's no index yet.
- Durability is configurable: every write is flushed to the OS, and with `sync=True` (default) it's also `fsync`ed.
- Opening a store validates the file and truncates a torn trailing record left by a crash mid-append. A checksum failure in a complete record raises `CorruptionError`.
- Single process, single writer, no file locking.

The API, record format, invariants, durability policy and known limitations are specified in [docs/v0-design.md](docs/v0-design.md).

**V1 is implemented** as `KVStore`, a transitional step: a write-ahead log (same record format as V0) plus an in-memory memtable. Mutations are appended to the WAL before the memtable is updated, `get` is a dictionary lookup that never reads the file, and opening replays the WAL and truncates a torn tail. See [docs/v1-design.md](docs/v1-design.md). V1's measured behavior against V0 (GET scaling, open/replay time, write latency, memory, WAL growth) is in [docs/benchmarks.md](docs/benchmarks.md#v1-benchmark).

**V2 is implemented** as `KVStore`, which adds immutable sorted SSTables. When the memtable reaches a size limit it's written to a checksummed SSTable (reusing the V0/V1 record format plus a footer), the WAL moves to a fresh segment, and the covered segments are deleted. `get` checks the memtable, then SSTables newest to oldest, and a table is found by a sequential scan. The store is a directory, not a single file. See [docs/v2-design.md](docs/v2-design.md) for the format, flush protocol, invariants and crash behavior. V2's measured behavior (flush cost and the periodic write stalls it causes, GET cost across tables, open time, memory, WAL and SSTable growth) is in [docs/benchmarks.md](docs/benchmarks.md#v2-benchmark).

**V3 is implemented** as `KVStore`, which adds a sparse index inside each SSTable: one entry (first key and file offset) per 16 records by default. A lookup binary-searches the in-memory index, then reads and scans only the selected block of the table instead of the whole file. Everything else (WAL, memtable, synchronous flush, recovery) is V2's, and V2 tables are still readable. See [docs/v3-design.md](docs/v3-design.md). V3's measured behavior against V2 (GET latency, records decoded per GET, open time, memory, index size, write cost) is in [docs/benchmarks.md](docs/benchmarks.md#v3-benchmark).

**V4 is implemented** as `KVStore`, which adds a Bloom filter to each SSTable (10 bits per key and 7 hashes by default, about 0.8% expected false positives). A lookup asks the table's filter first and skips the table without any file access when the key is definitely absent. Otherwise it uses V3's sparse index and bounded block scan. Tombstone keys are in the filter too. V2 and V3 tables are still readable (they have no filter and are always searched). See [docs/v4-design.md](docs/v4-design.md). V4's measured behavior against V3 (GET hit and miss latency, tables opened per GET, Bloom skip and false-positive rates, flush cost, open time, memory) is in [docs/benchmarks.md](docs/benchmarks.md#v4-benchmark).

**V5 is implemented** as `KVStore`, which adds a byte-bounded LRU block cache (8 MiB by default, `block_cache_bytes=0` disables it). After the Bloom filter and sparse index pick a block, the lookup takes the block's raw bytes from the cache or reads them from the file and caches them, and records are decoded and validated the same way either way. Entries are keyed by table and block index, and tables are immutable, so nothing is invalidated. The cache is in memory only and starts empty on every open. There's no on-disk format change, and V2, V3 and V4 tables are still readable. See [docs/v5-design.md](docs/v5-design.md). V5's measured behavior against V4 (the standard workload, where the cache helps only where blocks are revisited, and a separate controlled locality workload across cache capacities, with hit rates, physical block reads and latency) is in [docs/benchmarks.md](docs/benchmarks.md#v5-benchmark).

## Architecture roadmap

Versions so far. Each step gets benchmarked against the one before it.

| version | change | status |
|---|---|---|
| V0 | append-only persistent baseline | implemented |
| V1 | write-ahead log + memtable | implemented, benchmarked |
| V2 | SSTables | implemented, benchmarked |
| V3 | sparse index | implemented, benchmarked |
| V4 | Bloom filters | implemented, benchmarked |
| V5 | block cache | implemented, benchmarked |

## Performance methodology

No performance claim is made except what is measured. Benchmarks live in `benchmarks/`, use deterministic seeded workloads, a fresh database per run, multiple independent runs, and per-operation latency timing. The same workload definition is meant to be reused for every version so results are comparable. Benchmarks run with a warm OS page cache, and cold-cache performance isn't claimed. Physical I/O isn't measured for V0. Its scan work, space amplification and record-format overhead are derived from the record format (`benchmarks/structural.py`).

Methodology, workloads, results and limitations are documented in [docs/benchmarks.md](docs/benchmarks.md). V0 has a small-dataset baseline benchmark and a scaling characterization that measures how GET latency, PUT latency and open/recovery time change as the database grows, plus derived structural metrics (records and bytes scanned per GET, space amplification, record-format overhead, recovery work). These are the quantitative baseline for later versions.

## Usage

```python
from lsm_store import KVStore

with KVStore("data.lsm") as db:       # sync=True: fsync after every write
    db.put(b"user:42", b"Aaryan")
    db.get(b"user:42")                # b"Aaryan"
    db.delete(b"user:42")
    db.get(b"user:42")                # None
```

Keys and values are `bytes`. Pass `sync=False` to skip the per-write `fsync` (writes then survive a process crash but not necessarily power loss).

## Development

Requires Python 3.10+.

```bash
python -m venv .venv
# activate the venv, then:
pip install -e ".[dev]"
pytest                                # run the tests
```

## Repository layout

- `src/lsm_store/`: the storage engine (`record.py`: record format; `wal.py`: write-ahead log; `sstable.py`: SSTable format; `bloom.py`: Bloom filter primitives; `blockcache.py`: LRU block cache; `fsutil.py`: directory fsync helper; `store.py`: `KVStore`)
- `tests/`: correctness tests
- `benchmarks/`: benchmark scripts and result JSON
- `docs/`: design and benchmark documentation

