# lsm-store

A persistent key-value store in Python, built from scratch as a sequence of seven versions. V0 is an append-only file that scans everything on every read, and V6 is a log-structured merge-tree with a write-ahead log, a memtable, SSTables with sparse indexes and Bloom filters, a block cache and compaction. Each version changes one thing, is tested for correctness, and is benchmarked against the previous one with the same workloads.

V6 is the current version and completes the planned evolution from the V0 baseline to a compact LSM-style storage engine.

## What it covers

- On-disk formats: checksummed records, SSTables with a footer, a sparse index and a Bloom filter.
- Durability: write-ahead log ordering, `fsync` policy, torn-tail recovery.
- Crash safety: atomic table installs by rename, and a compaction commit rule that needs no manifest.
- Measurement: what each technique buys and what it costs, using seeded workloads, repeated runs, pooled percentiles, and derived structural metrics (records decoded and tables consulted per GET, space and write amplification).

## Final architecture (V6)

```
put / delete:  WAL append (fsync if sync=True) -> memtable; flush to an SSTable when the memtable is full
get:           memtable -> SSTables newest to oldest:
                 Bloom filter says no   -> skip the table
                 otherwise              -> sparse index -> one block (via the LRU block cache) -> scan it
compact():     merge all SSTables into one, keeping the newest version of each key and dropping tombstones
open:          drop unfinished tables -> apply the compaction rule -> check table footers -> replay the WAL
```

A store is a directory of `wal-*.log` segments and `sst-*.sst` / `cmp-*.sst` tables. Compaction is explicit and synchronous (`db.compact()`), with no background thread, leveling or manifest. It's single process, single writer.

## V0 to V6

| version | change | design doc |
|---|---|---|
| V0 | single append-only file; `get` scans all of it | [v0-design.md](docs/v0-design.md) |
| V1 | write-ahead log + in-memory memtable; `get` is a dict lookup | [v1-design.md](docs/v1-design.md) |
| V2 | memtable flushed to immutable sorted SSTables; a lookup scans a table | [v2-design.md](docs/v2-design.md) |
| V3 | sparse index per SSTable (one entry per 16 records); a lookup reads one block | [v3-design.md](docs/v3-design.md) |
| V4 | Bloom filter per SSTable (10 bits/key, 7 hashes, about 0.8% false positives) | [v4-design.md](docs/v4-design.md) |
| V5 | byte-bounded LRU block cache (8 MiB by default) | [v5-design.md](docs/v5-design.md) |
| V6 | explicit full compaction into one table | [v6-design.md](docs/v6-design.md) |

`KVStore` is edited in place from version to version; each version's design is described in its own doc. V3 to V6 can read tables written by the earlier table formats, but V2 to V5 can't read a directory that contains V6's `cmp-*` tables.

## Results at 10 million records

Median latency and open time at the largest scale (10,000,000 records, 11-byte keys, 100-byte values, `sync=False`), from [docs/benchmarks.md](docs/benchmarks.md#v0--v6-summary). V6 is shown after one compaction of the churn workload (overwrites and deletes, so there's stale data to remove).

| version | GET hit p50 | GET miss p50 | open time |
|---|---|---|---|
| V0 | 7,841,657 µs | 7,901,652 µs | 7.81 s |
| V1 | 0.8 µs | 0.6 µs | 11.02 s |
| V2 | 29,664 µs | 13,167,709 µs | 0.078 s |
| V3 | 107.7 µs | 14,500 µs | 0.353 s |
| V4 | 282.4 µs | 556.0 µs | 0.359 s |
| V5 | 265.5 µs | 507.5 µs | 0.344 s |
| V6 (churn, after compact) | 63.0 µs | 2.2 µs | 0.310 s |

What the table shows and what it doesn't:

- V1's latency is a dictionary lookup, but it holds the whole database in memory (2,273 MB of private bytes at 10M keys) and replays the whole WAL on open. V2 onward bound both.
- The sparse index (V3) cut hit latency from tens of milliseconds to about 100 µs but left misses scanning one block per table. The Bloom filter (V4) removed most of that miss cost.
- V4's and V5's hit latency is higher than V3's at this scale. Without compaction a 10M-record database has 264 flushed tables, and a miss evaluates a filter in every one of them. V6 removes that: before compaction the churn workload's miss p50 is 650.2 µs, and after it's 2.2 µs.
- The block cache (V5) helps only when blocks are revisited. The standard workload draws keys uniformly, so from 1M records up its hit rate is 1.4% to 1.9% and V5 is close to V4. In a separate locality workload (90% of GETs on 1,000 hot blocks), p50 falls from 73.1 µs on V4 to 36.1 µs with a 4 MiB cache.
- Compaction isn't free: it reads every input record twice and rewrites the survivors. Its measured cost per scale, and what it frees, are in the V6 section of the benchmark doc.

## Benchmark methodology

Benchmarks live in `benchmarks/`. Workloads are seeded and deterministic, each run uses a fresh database, and only the store call is timed (`perf_counter_ns` per operation). Scales run from 10,000 to 10,000,000 records with 3 to 5 runs each, and percentiles are pooled across runs. The workload definitions are shared across versions so results are comparable.

Limits that matter when reading the numbers:

- Every figure is a warm OS page cache number on one machine (Windows 11, Python 3.10.6). There's no cold-cache measurement, and "block read" means a read served from the page cache.
- Physical I/O isn't measured. Records decoded, bytes scanned, tables consulted and the amplification figures are application-level or derived from the file formats, and the docs label which is which.
- Timings are sensitive to machine load. Differences within about twice the between-run spread are treated as unresolved.
- Large-scale p99 values rest on few samples (150 pooled at the 5M and 10M miss phases).

Workloads, results, derived metrics and the full list of limitations are in [docs/benchmarks.md](docs/benchmarks.md).

## Usage

```python
from lsm_store import KVStore

with KVStore("db_dir") as db:       # a directory; sync=True fsyncs every write
    db.put(b"user:42", b"Aaryan")
    db.get(b"user:42")                # b"Aaryan"
    db.delete(b"user:42")
    db.get(b"user:42")                # None
    db.compact()                      # merge all SSTables into one
```

Keys and values are `bytes`. Pass `sync=False` to skip the per-write `fsync`, and writes then survive a process crash but not necessarily power loss. The V0 and V1 stores take a file path instead of a directory.

## Development

Requires Python 3.10+. No runtime dependencies.

```bash
python -m venv .venv
# activate the venv, then:
pip install -e ".[dev]"
pytest
```

## Repository layout

- `src/lsm_store/`: the engine. `record.py` (record format), `wal.py`, `sstable.py` (table format), `bloom.py`, `blockcache.py`, `fsutil.py` (directory fsync), and `store.py`, the store itself, which is edited in place from version to version.
- `tests/`: correctness tests, including crash and recovery cases.
- `benchmarks/`: benchmark scripts and result JSON in `results/`.
- `docs/`: per-version design docs and `benchmarks.md`.

