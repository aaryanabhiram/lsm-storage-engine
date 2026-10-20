# lsm-store

A persistent key-value store in Python, built from scratch as a sequence of seven versions. V0 is an append-only file that scans everything on every read, and V6 is a log-structured merge-tree with a write-ahead log, a memtable, SSTables with sparse indexes and Bloom filters, a block cache and compaction. Each version changes one thing, is tested for correctness, and V1 to V5 are each benchmarked against the previous version with the same workloads.

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

