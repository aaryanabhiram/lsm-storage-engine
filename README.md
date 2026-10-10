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

## Architecture roadmap

Versions so far. Each step gets benchmarked against the one before it.

| version | change | status |
|---|---|---|
| V0 | append-only persistent baseline | implemented |
| V1 | write-ahead log + memtable | implemented, benchmarked |
| V2 | SSTables | implemented, benchmarked |

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

- `src/lsm_store/`: the storage engine (`record.py`: record format; `wal.py`: write-ahead log; `sstable.py`: SSTable format; `fsutil.py`: directory fsync helper; `store.py`: `KVStore`)
- `tests/`: correctness tests
- `benchmarks/`: benchmark scripts and result JSON
- `docs/`: design and benchmark documentation

