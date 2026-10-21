"""V6 benchmark: what does full synchronous compaction change, and what does it cost? Methodology: docs/benchmarks.md.

Usage:
    python benchmarks/bench.py --scales 10000 --out PATH             # churn (+ control) runs for the listed scales
    python benchmarks/bench.py --small --out PATH                    # 10,000 keys with the 16 KiB memtable limit
    python benchmarks/bench.py --crash --out PATH                    # small crash/restart sanity checks
    python benchmarks/bench.py --analyze-only --out FINAL.json

Every run builds a fresh database, then measures it BEFORE and AFTER one `compact()` call. Two workloads, never mixed:

* CHURN (primary). The standard V0..V5 load (ascending keys, `Random(42)` values), then a deterministic churn phase that
  leaves stale versions, tombstones, resurrected keys and empty values spread over many tables (CHURN below). Compaction
  has something to remove.
* CONTROL (no stale data). The standard load only, copied before the churn phase starts, so every key exists once.
  Compaction can only merge tables here. It is run for all runs up to 1M records and for one run at 5M and 10M.

Within a workload the sequence is: open (timed recovery) -> structural state -> timed GETs (hit, miss, in-range miss,
deleted key; empty cache) -> untimed read-work sample -> [full scan and verification] -> `compact()` -> structural state
-> the same timed GETs (the cache is empty again: compaction discarded the retired tables' blocks) -> untimed sample ->
[full scan] -> close -> reopen (timed recovery) -> correctness checks. Before and after samples are never pooled together.
The GET keys, sample sizes, timing boundary, percentile code, seed, key/value sizes, Bloom and index configuration, cache
size and warm-OS-cache condition are those of V0..V5 (workload, full_summary).

Correctness: a reference model (one CRC-32 per key index, -1 = absent) is updated alongside every write. All timed GETs
are checked against it, and in the runs with a full scan every record of every table plus the memtable is checked
against it before and after compaction. No production code is changed; everything is observed from outside (monkey-
patched counters, file sizes). All numbers are WARM OS-CACHE numbers. V0..V5 results are read, never written.
"""

import tracemalloc
from contextlib import contextmanager
import argparse
import gc
import heapq
import json
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zlib
from array import array
from pathlib import Path

import lsm_store.sstable as sstable_module
from lsm_store import KVStore
from lsm_store.blockcache import DEFAULT_BLOCK_CACHE_BYTES
from lsm_store.record import HEADER_SIZE
from lsm_store.sstable import FOOTER_V4_SIZE, SSTable
from lsm_store.store import DEFAULT_MEMTABLE_LIMIT_BYTES
from lsm_store.wal import replay
from lsm_store import bloom
from lsm_store.blockcache import BlockCache
from lsm_store.sstable import NOT_FOUND
from lsm_store.store import DEFAULT_BLOOM_BITS_PER_KEY

# ---- helpers (the same ones the earlier stages used) ------------------------

ROOT = Path(__file__).resolve().parent.parent
VALUE_SIZE = 100

# ---- scale ladder carried over from the V0 scaling benchmark ----------------------------------
# records -> (GETs per kind in the V0 run, independent runs). V1 GETs are cheap, so V1 uses
# GETS_PER_KIND instead; the V0 count only keeps the first part of the RNG stream identical to V0's.
V0_SCALES = {
    10_000: {"gets_per_kind": 200, "runs": 5},
    100_000: {"gets_per_kind": 200, "runs": 5},
    1_000_000: {"gets_per_kind": 100, "runs": 5},
    5_000_000: {"gets_per_kind": 50, "runs": 3},
    10_000_000: {"gets_per_kind": 50, "runs": 3},
}
N_OVERWRITES = 1000  # timed PUTs of existing keys after the load, at every scale
MIN_FREE_RAM = 3 * 2**30  # refuse to start a run with less available RAM than this
MIN_FREE_DISK = 20 * 2**30


def key_of(i):
    return b"key%08d" % i


RECORD_BYTES = 13 + len(key_of(0)) + VALUE_SIZE  # header + key + value = 124


def timed_call(fn, *args):
    t0 = time.perf_counter_ns()
    result = fn(*args)
    return time.perf_counter_ns() - t0, result


def summarize(latencies_ns):
    s = sorted(latencies_ns)
    q = lambda p: s[min(len(s) - 1, int(p * len(s)))]  # noqa: E731  nearest-rank
    total_s = sum(s) / 1e9
    return {
        "ops": len(s),
        "p50_us": q(0.50) / 1e3,
        "p95_us": q(0.95) / 1e3,
        "p99_us": q(0.99) / 1e3,
        "throughput_ops_per_s": len(s) / total_s,
    }


def full_summary(latencies_ns):
    s = summarize(sorted(latencies_ns))
    s["mean_us"] = sum(latencies_ns) / len(latencies_ns) / 1e3
    s["min_us"] = min(latencies_ns) / 1e3
    s["max_us"] = max(latencies_ns) / 1e3
    return s


def ram_bytes():
    """(total, available) physical RAM on Windows, or None elsewhere."""
    if sys.platform != "win32":
        return None
    import ctypes

    class MemStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                    ("ptotal", ctypes.c_ulonglong), ("pavail", ctypes.c_ulonglong),
                    ("vtotal", ctypes.c_ulonglong), ("vavail", ctypes.c_ulonglong),
                    ("ext", ctypes.c_ulonglong)]

    st = MemStatus()
    st.length = ctypes.sizeof(MemStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return st.total, st.avail


def check_headroom():
    ram = ram_bytes()
    if ram and ram[1] < MIN_FREE_RAM:
        raise SystemExit(f"stopping safely: only {ram[1] / 2**30:.1f} GiB RAM available")
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < MIN_FREE_DISK:
        raise SystemExit(f"stopping safely: only {free / 2**30:.1f} GiB disk free")


# ---- protocol, fixed before the final run ---------------------------------------------------
SEED = 42
# independent runs per scale (fresh database each): the V0 ladder, 5/5/5/3/3 (V0_SCALES[records]["runs"])
N_DELETES = 1000  # timed DELETEs of keys drawn uniformly from the keyspace (may repeat), per run
MEMORY_PROBES = 3  # untimed probes per scale


def delete_keys(records, seed=SEED):
    rng = random.Random(f"delete:{seed}:{records}")
    return [key_of(rng.randrange(records)) for _ in range(N_DELETES)]


# ---- process memory (Windows) ---------------------------------------------------------------
# TODO: this uses the Windows psapi through ctypes, so the memory probes only run on Windows.
def process_memory():
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    c = Counters()
    c.cb = ctypes.sizeof(Counters)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return {"working_set": c.WorkingSetSize, "peak_working_set": c.PeakWorkingSetSize,
            "private": c.PrivateUsage}


# ---- protocol, fixed before the final run ----------------------------------------------------
# GET samples per run, per kind. A V2 GET that reaches the SSTables is a Python-level scan, so counts follow cost:
# misses use V0's per-kind counts from 100,000 records up (a miss scans every table to its end, like V0's full-file
# scan); hits are cheaper and get larger counts. At 10,000 records nothing is flushed under the default limit, so a
# GET is a memtable lookup (like V1) and gets V1's count.
GET_HITS = {10_000: 100_000, 100_000: 5_000, 1_000_000: 2_000, 5_000_000: 1_000, 10_000_000: 1_000}
GET_MISSES = {10_000: 100_000, 100_000: 200, 1_000_000: 100, 5_000_000: 50, 10_000_000: 50}
STRUCT_HITS = 200  # untimed instrumented GETs per run that count tables consulted / records / bytes examined
SMALL_LIMIT_BYTES = 16 * 1024  # baseline variant in which a 10,000-key database is spread over many SSTables
ENTRY_PAYLOAD = len(key_of(0)) + VALUE_SIZE  # 111


def workload(records, n_hits, n_misses, v0_gets):
    """The logical workload of one run. Same RNG roles and order as the V0 and V1 benchmarks: V0-count hit
    draws first, then the overwrite keys and values (the value RNG then continues into the load values); extra hit
    keys beyond V0's count come from a separate RNG."""
    value_rng = random.Random(SEED)
    op_rng = random.Random(SEED)
    first = [op_rng.randrange(records) for _ in range(v0_gets)]
    overwrites = [(key_of(op_rng.randrange(records)), value_rng.randbytes(VALUE_SIZE)) for _ in range(N_OVERWRITES)]
    extra = random.Random(f"extra-hits:{SEED}:{records}")
    hit_idx = (first + [extra.randrange(records) for _ in range(max(0, n_hits - v0_gets))])[:n_hits]
    miss_keys = [key_of(records + i) for i in range(n_misses)]  # above every stored key; never written
    return value_rng, hit_idx, overwrites, miss_keys, delete_keys(records)


def metric_range(vals):
    return {"median": statistics.median(vals), "min": min(vals), "max": max(vals)}


def pct_change(new, old):
    return (new / old - 1) * 100


def write_row(s):
    """Write-side summary of one scale entry (works on V2 and V3 result entries: same layout)."""
    sp = s["put_load_split_aggregate"]
    runs = s["runs"]
    flushes = [f for r in runs for f in r["flushes"]["put_load"]]
    trig = s["pooled_all_runs"]["put_load_flush_trigger"]
    row = {
        "records": s["records"],
        "flushes_per_run": [len(r["flushes"]["put_load"]) for r in runs],
        "all_p50_p95_p99_us": [sp["all"][m]["median"] for m in ("p50_us", "p95_us", "p99_us")],
        "non_flush_p50_p95_p99_us": [sp["non_flush"][m]["median"] for m in ("p50_us", "p95_us", "p99_us")],
        "throughput_ops_per_s": sp["all"]["throughput_ops_per_s"]["median"],
        "max_write_ms_median_over_runs": statistics.median(r["put_load_max_ns"] for r in runs) / 1e6,
    }
    if trig:
        row["flush_trigger_p50_p95_p99_max_us"] = [trig[m] for m in ("p50_us", "p95_us", "p99_us", "max_us")]
        row["flush_trigger_ops_pooled"] = trig["ops"]
        row["flush_ns_p50_p95_max_ms"] = [
            x / 1e6 for x in (statistics.median(f["flush_ns"] for f in flushes),
                              sorted(f["flush_ns"] for f in flushes)[int(0.95 * len(flushes))],
                              max(f["flush_ns"] for f in flushes))]
        row["write_sstable_ms_median"] = statistics.median(f["write_sstable_ns"] for f in flushes) / 1e6
        row["flush_outside_write_sstable_ms_median"] = statistics.median(
            f["flush_ns"] - f["write_sstable_ns"] for f in flushes) / 1e6
        row["write_sstable_share_of_flush_median"] = statistics.median(f["write_sstable_ns"] / f["flush_ns"]
                                                                       for f in flushes)
        row["amortized_flush_us_per_put"] = statistics.median(
            sum(f["flush_ns"] for f in r["flushes"]["put_load"]) for r in runs) / s["records"] / 1e3
        row["table_bytes"] = runs[0]["flushes"]["put_load"][0]["table_bytes"]
    return row


BITS_PER_KEY = DEFAULT_BLOOM_BITS_PER_KEY  # 10
NUM_HASHES = bloom.num_hashes(BITS_PER_KEY)  # 7
WORK_FIELDS = ("tables_consulted", "bloom_checks", "bloom_negative", "bloom_maybe", "false_positives",
               "fp_without_file_access", "index_lookups", "index_rule_outs", "tables_opened", "blocks_scanned",
               "records_decoded", "bytes_decoded")


# ---- read-work instrumentation (untimed) --------------------------------------------------------
def measure_read_work(db, keys):
    """For each key return [category] + WORK_FIELDS of one GET.

    tables_consulted   SSTable.lookup calls
    bloom_checks       SSTable.may_contain calls; bloom_negative / bloom_maybe split them by the answer
    false_positives    lookups whose Bloom answer was "maybe" but which returned NOT_FOUND (the key is not there)
    fp_without_file_access  of those, the ones the sparse index then ruled out (key below the table's first key),
                       so no file was opened
    index_lookups      SSTable.locate calls; index_rule_outs those returning None (no candidate block)
    tables_opened      sstable._open_table calls (= blocks read: a lookup opens its table once, for one block)
    blocks_scanned     SSTable._scan_block calls
    records/bytes_decoded  counted in the record reader (application-level work, not physical I/O)
    Untimed."""
    work = dict.fromkeys(WORK_FIELDS, 0)
    state = {"last_maybe": None}
    real_lookup, real_may, real_locate = SSTable.lookup, SSTable.may_contain, SSTable.locate
    real_scan_block, real_read, real_open = SSTable._scan_block, sstable_module.read_records, sstable_module._open_table

    def lookup(self, key):
        work["tables_consulted"] += 1
        state["last_maybe"] = None
        opened = work["tables_opened"]
        found = real_lookup(self, key)
        if state["last_maybe"] and found is NOT_FOUND:
            work["false_positives"] += 1
            if work["tables_opened"] == opened:
                work["fp_without_file_access"] += 1
        return found

    def may_contain(self, key):
        answer = real_may(self, key)
        work["bloom_checks"] += 1
        work["bloom_maybe" if answer else "bloom_negative"] += 1
        state["last_maybe"] = answer
        return answer

    def locate(self, key):
        found = real_locate(self, key)
        work["index_lookups"] += 1
        if found is None:
            work["index_rule_outs"] += 1
        return found

    def scan_block(self, *args):
        work["blocks_scanned"] += 1
        return real_scan_block(self, *args)

    def open_table(path):
        work["tables_opened"] += 1
        return real_open(path)

    def counting(f):
        for rec in real_read(f):
            work["records_decoded"] += 1
            work["bytes_decoded"] += HEADER_SIZE + len(rec.key) + len(rec.value or b"")
            yield rec

    out = []
    SSTable.lookup, SSTable.may_contain, SSTable.locate, SSTable._scan_block = lookup, may_contain, locate, scan_block
    sstable_module.read_records, sstable_module._open_table = counting, open_table
    try:
        for key in keys:
            work.update(dict.fromkeys(WORK_FIELDS, 0))
            value = db.get(key)
            if value is None:
                cat = "miss"
            elif work["tables_consulted"] == 0:
                cat = "memtable_hit"
            elif work["tables_consulted"] == 1:
                cat = "newest_sstable_hit"
            else:
                cat = "older_sstable_hit"
            out.append([cat] + [work[f] for f in WORK_FIELDS])
    finally:
        SSTable.lookup, SSTable.may_contain, SSTable.locate, SSTable._scan_block = (
            real_lookup, real_may, real_locate, real_scan_block)
        sstable_module.read_records, sstable_module._open_table = real_read, real_open
    return out


def table_memory(paths):
    """Bytes of Python heap retained by opening the tables (tracemalloc, current traced size after minus before): the
    sparse indexes, the Bloom filter blocks and the SSTable objects. `bloom_bytes_objects` is the exact size of the
    filter `bytes` objects (sys.getsizeof), so the rest is the index and object overhead. Run after all timed phases;
    tracemalloc is stopped again before returning."""
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    tables = [SSTable(p) for p in paths]
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    entries = sum(len(t._keys) for t in tables)
    bloom_bytes = sum(t.filter_len for t in tables)
    bloom_objects = sum(sys.getsizeof(t._bloom) for t in tables)
    return {"tables": len(tables), "index_entries": entries, "retained_bytes": current - base,
            "peak_bytes_above_base": peak - base, "bloom_block_bytes": bloom_bytes,
            "bloom_bytes_objects": bloom_objects, "retained_minus_bloom_objects": current - base - bloom_objects,
            "retained_bytes_per_index_entry_excluding_bloom": (current - base - bloom_objects) / entries
            if entries else None}


OUT_NONE, OUT_HIT, OUT_MISS = 0, 1, 2  # per-GET cache outcome: no cache access / block served from cache / block read


class _NoCache:
    hits = misses = 0


def cache_of(db):
    """The store's block cache, or a counter stub for a store without one. (Not `or`: an empty BlockCache is falsy.)"""
    cache = getattr(db, "_block_cache", None)
    return _NoCache if cache is None else cache


# ---- result checksums and per-GET cache outcome (recorded outside every timed interval) -------------------------
def crc_of(value):
    return -1 if value is None else zlib.crc32(value)


class Capture:
    """Per-run record of every timed GET's cache outcome and result checksum, in call order."""

    def __init__(self):
        self.outcome = array("b")
        self.crc = array("q")


def make_timed_call(capture):
    def timed_call(fn, *args):
        cache = cache_of(fn.__self__)
        h0, m0 = cache.hits, cache.misses  # before the clock starts
        t0 = time.perf_counter_ns()
        result = fn(*args)
        ns = time.perf_counter_ns() - t0
        capture.outcome.append(OUT_MISS if cache.misses > m0 else OUT_HIT if cache.hits > h0 else OUT_NONE)
        capture.crc.append(crc_of(result))
        return ns, result

    return timed_call


@contextmanager
def swapped_cache(db, capacity):
    """Give every open table of `db` a fresh, empty BlockCache of `capacity` bytes for the duration (the store's own
    cache and its counters are untouched). External: assigns an instance attribute only."""
    cache = BlockCache(capacity)
    saved = [(t, t._cache) for t in db._sstables]
    for t, _ in saved:
        t._cache = cache
    try:
        yield cache
    finally:
        for t, old in saved:
            t._cache = old


def cache_counters(cache):
    s = cache.stats()
    return {k: s[k] for k in ("capacity_bytes", "bytes", "entries", "hits", "misses", "insertions", "evictions",
                              "hit_rate")}

SCALES = list(V0_SCALES)
RESULTS = ROOT / "benchmarks/results"
INDEX_INTERVAL = 16

# ---- the churn phase (fixed here, before any measurement) -----------------------------------------------------------
CHURN = {
    "overwrites": "records // 5 puts of uniformly drawn keys (with replacement); every 20th has an empty value",
    "deletes": "records // 20 distinct keys, drawn without replacement",
    "resurrections": "a tenth of each round's deletes are written again with a new value",
    "order": "10 rounds, each: a tenth of the overwrites, a tenth of the deletes, then that round's resurrections",
    "rng": "random.Random(f'churn:{SEED}:{records}') chooses keys; random.Random(f'churn-values:{SEED}:{records}') makes values",
}
CONTROL_RUNS = {10_000: 5, 100_000: 5, 1_000_000: 5, 5_000_000: 1, 10_000_000: 1}  # runs that also measure the control
SMALL_HITS, SMALL_MISSES = 5_000, 1_000  # GETs per phase in the 16 KiB-limit configuration (about 70 tables: 100,000 miss GETs would take minutes)
STRUCT_MISSES = 50  # instrumented samples per miss-like phase (as STRUCT_MISSES_V4)
VERIFY_UNIFORM = 2000  # random key indices checked after the final reopen, on top of the category samples
CATEGORY_SAMPLE = 2000  # keys kept per churn category for the after-reopen checks
PHASES = ("get_hit", "get_miss", "get_miss_inrange", "get_deleted")
TABLE_RE = re.compile(r"^(sst|cmp)-(\d{8})\.sst$")


def dir_state6(path):
    """Files in a V6 database directory. getsize, not DirEntry.stat (stale for files open for append on Windows)."""
    out = {"wal_files": 0, "wal_bytes": 0, "sst_files": 0, "sst_bytes": 0, "cmp_files": 0, "cmp_bytes": 0, "tmp_files": 0}
    for name in os.listdir(path):
        full = os.path.join(path, name)
        m = TABLE_RE.match(name)
        if name.startswith("wal-"):
            out["wal_files"] += 1
            out["wal_bytes"] += os.path.getsize(full)
        elif m:
            out[m.group(1) + "_files"] += 1
            out[m.group(1) + "_bytes"] += os.path.getsize(full)
        elif name.endswith(".tmp"):
            out["tmp_files"] += 1
    return out


def warm_read(path):
    """Read every file of the database once, untimed, before its first timed open (the OS page cache is then warm for
    every database alike, including the control database, which is a fresh copy)."""
    for name in os.listdir(path):
        with open(os.path.join(path, name), "rb") as f:
            while f.read(1 << 20):
                pass


def structure(db):
    """The table set of an open store, from the in-memory tables and the files."""
    ts = db._sstables
    sizes = [os.path.getsize(t.path) for t in ts]
    out = {"tables": len(ts), "table_bytes": sum(sizes), "records": sum(t.count for t in ts),
           "data_bytes": sum(t.data_len for t in ts), "index_bytes": sum(t.index_len for t in ts),
           "filter_bytes": sum(t.filter_len for t in ts), "footer_bytes": len(ts) * FOOTER_V4_SIZE,
           "index_entries": sum(len(t._keys) for t in ts),
           "cmp_tables": sum(os.path.basename(t.path).startswith("cmp-") for t in ts),
           "memtable_entries": len(db._memtable), "memtable_payload_bytes": db._mem_bytes,
           "largest_table_records": max((t.count for t in ts), default=0)}
    out["index_filter_footer_bytes"] = out["index_bytes"] + out["filter_bytes"] + out["footer_bytes"]
    assert all(s == t.data_len + t.index_len + t.filter_len + FOOTER_V4_SIZE for s, t in zip(sizes, ts)), \
        "a table's size is not data + index + filter + footer"
    return out


# ---- the model, the churn phase, the GET key sets ---------------------------------------------------------------------
def crc(value):
    return -1 if value is None else zlib.crc32(value)


def index_of(key):
    return int(key[3:])


def load_database(path, records, limit, value_rng, model):
    db = KVStore(path, sync=False, memtable_limit_bytes=limit)
    assert db._index_interval == INDEX_INTERVAL and db._bloom_bits_per_key == BITS_PER_KEY
    t0 = time.perf_counter()
    for i in range(records):
        v = value_rng.randbytes(VALUE_SIZE)
        model[i] = zlib.crc32(v)
        db.put(key_of(i), v)
    wall = time.perf_counter() - t0
    db.close()
    return {"wall_s": wall, "user_payload_bytes": records * ENTRY_PAYLOAD,
            "wal_record_bytes": records * RECORD_BYTES, "puts": records}


CHURN_ROUNDS = 10


def apply_churn(path, records, limit, model):
    """The churn phase, in CHURN_ROUNDS rounds of (overwrites, deletes, resurrections), each a tenth of the totals, so
    tombstones and stale versions end up in flushed tables and not only in the memtable. A round's resurrections are
    keys that round deleted. An overwrite draw may hit a key deleted earlier (the model records whatever the last write
    was); the final category samples keep only keys whose final state matches their category."""
    rng = random.Random(f"churn:{SEED}:{records}")
    vrng = random.Random(f"churn-values:{SEED}:{records}")
    n_over, n_del = records // 5, records // 20
    n_res = n_del // 10
    dels = rng.sample(range(records), n_del)
    db = KVStore(path, sync=False, memtable_limit_bytes=limit)
    st = {"payload": 0, "wal": 0, "empties": 0, "over_ops": 0, "res_ops": 0, "del_ops": 0}
    over_sample, empty_sample, res_keys = [], [], []

    def put(i, v):
        k = key_of(i)
        db.put(k, v)
        model[i] = zlib.crc32(v)
        st["payload"] += len(k) + len(v)
        st["wal"] += HEADER_SIZE + len(k) + len(v)

    t0 = time.perf_counter()
    for r in range(CHURN_ROUNDS):
        o_lo, o_hi = n_over * r // CHURN_ROUNDS, n_over * (r + 1) // CHURN_ROUNDS
        d_lo, d_hi = n_del * r // CHURN_ROUNDS, n_del * (r + 1) // CHURN_ROUNDS
        for j in range(o_lo, o_hi):
            i = rng.randrange(records)
            empty = j % 20 == 19
            put(i, b"" if empty else vrng.randbytes(VALUE_SIZE))
            st["over_ops"] += 1
            if empty:
                st["empties"] += 1
                empty_sample.append(i)
            else:
                over_sample.append(i)
        for i in dels[d_lo:d_hi]:
            k = key_of(i)
            db.delete(k)
            model[i] = -1
            st["payload"] += len(k)
            st["wal"] += HEADER_SIZE + len(k)
            st["del_ops"] += 1
        mine = dels[d_lo:d_lo + (d_hi - d_lo) // 10]
        for i in mine:
            put(i, vrng.randbytes(VALUE_SIZE))
            st["res_ops"] += 1
        res_keys.extend(mine)
    wall = time.perf_counter() - t0
    db.close()
    dead = [i for i in dels if model[i] == -1]
    return {"wall_s": wall, "overwrites": st["over_ops"], "empty_value_puts": st["empties"], "deletes": st["del_ops"],
            "resurrections": st["res_ops"], "ops": st["over_ops"] + st["del_ops"] + st["res_ops"], "rounds": CHURN_ROUNDS,
            "user_payload_bytes": st["payload"], "wal_record_bytes": st["wal"], "keys_dead_at_end": len(dead),
            "samples": {"overwritten": [i for i in over_sample if model[i] not in (-1, 0)][:CATEGORY_SAMPLE],
                        "empty": [i for i in empty_sample if model[i] == 0][:CATEGORY_SAMPLE],
                        "resurrected": [i for i in res_keys if model[i] > 0][:CATEGORY_SAMPLE],
                        "deleted": dead[:CATEGORY_SAMPLE]},
            "dead_pool": dead}


def key_sets(kind, records, n_hits, n_misses, v0_gets, model, plan):
    """phase -> (keys, wanted checksums). Hit keys are the standard hit draws, with a key that the churn deleted replaced by
    a draw from a separate generator (so every 'hit' is a live key); misses are the standard keys above the stored range;
    in-range misses are `key_of(i) + b'x'`, which sort between two stored keys and were never written; deleted keys are
    keys whose newest version is a tombstone (churn only; cycled if the pool is smaller than the sample)."""
    _, hit_idx, _, miss_keys, _ = workload(records, n_hits, n_misses, v0_gets)
    redraw = random.Random(f"live-hits:{SEED}:{records}")
    live = []
    for i in hit_idx:
        while model[i] == -1:
            i = redraw.randrange(records)
        live.append(i)
    rng = random.Random(f"inrange-misses:{SEED}:{records}")
    inrange = [key_of(rng.randrange(records)) + b"x" for _ in range(n_misses)]
    sets = {"get_hit": ([key_of(i) for i in live], [model[i] for i in live]),
            "get_miss": (miss_keys, [-1] * len(miss_keys)),
            "get_miss_inrange": (inrange, [-1] * len(inrange))}
    if kind == "churn":
        pool = plan["dead_pool"]
        keys = [key_of(pool[j % len(pool)]) for j in range(n_misses)]
        assert all(model[index_of(k)] == -1 for k in keys)
        sets["get_deleted"] = (keys, [-1] * len(keys))
    return sets


# ---- timed GETs and read-work samples ----------------------------------------------------------------------------------
def timed_phase(db, keys, want):
    capture = Capture()
    call = make_timed_call(capture)
    lat = array("q")
    for k in keys:
        ns, _ = call(db.get, k)
        lat.append(ns)
    assert capture.crc.tolist() == want, "a GET returned a value that differs from the reference model"
    outcome = capture.outcome
    return lat, {"none": outcome.count(OUT_NONE), "cache_hit": outcome.count(OUT_HIT),
                 "cache_miss": outcome.count(OUT_MISS)}


def read_work(db, keys):
    """measure_read_work on a cold cache of the store's capacity; per-field mean/min/max over the sample."""
    with swapped_cache(db, db.block_cache.capacity_bytes):
        rows = measure_read_work(db, keys)
    out = {"n": len(rows), "fields": {}}
    for j, f in enumerate(WORK_FIELDS, start=1):
        col = [r[j] for r in rows]
        out["fields"][f] = {"mean": statistics.fmean(col), "min": min(col), "max": max(col)}
    cats = {}
    for r in rows:
        cats[r[0]] = cats.get(r[0], 0) + 1
    out["categories"] = cats
    return out


def get_stage(db, sets, raw, stage):
    """All timed GET phases of one stage (empty cache first), then the untimed samples. Raw latencies go to `raw`."""
    base = cache_counters(db.block_cache)
    assert base["entries"] == 0 and base["bytes"] == 0, "cache is not empty"  # counters are cumulative; entries are not
    out = {"phases": {}, "read_work": {}}
    for ph, (keys, want) in sets.items():
        lat, outcome = timed_phase(db, keys, want)
        raw[(stage, ph)] = lat
        out["phases"][ph] = {"summary": full_summary(lat), "gets": len(keys), "cache_outcome": outcome}
    now = cache_counters(db.block_cache)
    out["cache"] = {**now, **{k: now[k] - base[k] for k in ("hits", "misses", "insertions", "evictions")}}
    out["cache"]["hit_rate"] = out["cache"]["hits"] / (out["cache"]["hits"] + out["cache"]["misses"])         if out["cache"]["hits"] + out["cache"]["misses"] else 0.0
    for ph, (keys, _) in sets.items():
        n = STRUCT_HITS if ph == "get_hit" else min(len(keys), STRUCT_MISSES)
        out["read_work"][ph] = read_work(db, keys[:n])
    return out


# ---- full scan: classification of every physical record, and verification against the model -----------------------------
def _ranked(table, rank):
    for key, value in table.items():
        yield key, rank, value


def scan_and_verify(db, model, records):
    """One merge over every table (newest first per key) plus the memtable. Classifies each physical record as `live`
    (the newest version of its key, a value), `tombstone` (the newest version, a delete) or `obsolete` (older than another
    version), with byte sizes (record = 13-byte header + key + value), and checks the logical contents against the model:
    every key in the tables and the memtable, and the number of live keys (so no live key can be missing)."""
    streams = [_ranked(t, r) for r, t in enumerate(reversed(db._sstables))]
    mem = db._memtable
    s = dict.fromkeys(("physical", "live", "tombstone", "obsolete", "live_record_bytes", "tombstone_record_bytes",
                       "obsolete_record_bytes", "live_payload_bytes", "logical_live"), 0)
    last = None
    for key, _rank, value in heapq.merge(*streams):
        size = HEADER_SIZE + len(key) + (0 if value is None else len(value))
        s["physical"] += 1
        if key == last:
            s["obsolete"] += 1
            s["obsolete_record_bytes"] += size
            continue
        last = key
        if value is None:
            s["tombstone"] += 1
            s["tombstone_record_bytes"] += size
        else:
            s["live"] += 1
            s["live_record_bytes"] += size
            s["live_payload_bytes"] += len(key) + len(value)
        if key in mem:
            continue  # shadowed by the memtable; checked below
        assert crc(value) == model[index_of(key)], f"table contents differ from the model for {key!r}"
        s["logical_live"] += value is not None
    for key, value in mem.items():
        assert crc(value) == model[index_of(key)], f"memtable contents differ from the model for {key!r}"
        s["logical_live"] += value is not None
    want_live = records - model.count(-1)
    assert s["logical_live"] == want_live, f"{s['logical_live']} live keys, the model has {want_live}"
    s["model_live_keys"] = want_live
    s["memtable_entries"] = len(mem)
    return s


# ---- recovery ------------------------------------------------------------------------------------------------------------
def open_components(path):
    """The steps of recovery re-run with the modules' own functions (read-only): directory listing, opening every table,
    replaying the WAL segments. Run on the same directory right after the measured open."""
    t0 = time.perf_counter_ns()
    names = sorted(os.listdir(path))
    tabs = [n for n in names if TABLE_RE.match(n)]
    wal = [n for n in names if n.startswith("wal-")]
    t1 = time.perf_counter_ns()
    tables = [SSTable(os.path.join(path, n)) for n in tabs]
    t2 = time.perf_counter_ns()
    replayed = sum(len(replay(os.path.join(path, n))) for n in wal)
    t3 = time.perf_counter_ns()
    return {"list_dir_ns": t1 - t0, "sstable_open_ns": t2 - t1, "wal_replay_ns": t3 - t2, "tables": len(tables),
            "wal_segments": len(wal), "memtable_entries_replayed": replayed,
            "index_entries_loaded": sum(len(t._keys) for t in tables),
            "index_bytes_read": sum(t.index_len for t in tables), "filter_bytes_read": sum(t.filter_len for t in tables),
            "footer_bytes_read": len(tables) * FOOTER_V4_SIZE}


def table_memory_both(db):
    return table_memory([t.path for t in db._sstables])


# ---- correctness after the final reopen ----------------------------------------------------------------------------------
def verify_reopened(db, path, model, records, plan, sets):
    rng = random.Random(f"verify:{SEED}:{records}")
    checked = {}

    def check(name, idxs):
        for i in idxs:
            assert crc(db.get(key_of(i))) == model[i], f"after reopen: wrong value for key {i} ({name})"
        checked[name] = len(idxs)

    cats = plan["samples"] if plan else {}
    for name in ("overwritten", "empty", "resurrected", "deleted"):
        if plan:
            check(name, cats[name])
    check("uniform", [rng.randrange(records) for _ in range(VERIFY_UNIFORM)])
    for name, (keys, want) in sets.items():
        sample = list(zip(keys, want))[:VERIFY_UNIFORM]
        for k, w in sample:
            assert crc(db.get(k)) == w, f"after reopen: wrong result for {k!r} ({name})"
        checked[name] = len(sample)
    # the files on disk: only compaction outputs, no unfinished builds, and exactly what the store loaded
    state = dir_state6(path)
    names = sorted(n for n in os.listdir(path) if TABLE_RE.match(n))
    loaded = sorted(os.path.basename(t.path) for t in db._sstables)
    audit = {"table_files": names, "loaded_tables": loaded, "files_equal_loaded": names == loaded,
             "only_cmp_tables": all(n.startswith("cmp-") for n in names), "tmp_files": state["tmp_files"],
             "sst_files_left": state["sst_files"]}
    assert audit["files_equal_loaded"] and audit["tmp_files"] == 0
    return {"checked": checked, "audit": audit}


# ---- one workload: before, compact, after, recover -----------------------------------------------------------------------
def measure_workload(kind, path, model, records, limit, n_hits, n_misses,
                     v0_gets, plan, full_verify):
    sets = key_sets(kind, records, n_hits, n_misses, v0_gets, model, plan)
    raw = {}
    out = {"kind": kind, "full_verify": full_verify}
    warm_read(path)
    state_before = dir_state6(path)
    out["dir_before_open"] = state_before

    # -- before: recovery, structure, timed GETs
    t0 = time.perf_counter_ns()
    db = KVStore(path, sync=False, memtable_limit_bytes=limit)
    open_pre_ns = time.perf_counter_ns() - t0
    pre = structure(db)
    assert pre["tables"] == state_before["sst_files"] and pre["cmp_tables"] == 0
    comp_pre = open_components(path)
    pre["table_heap"] = table_memory_both(db)
    pre["stage"] = get_stage(db, sets, raw, "pre")
    pre["scan"] = scan_and_verify(db, model, records) if full_verify else None

    # -- compaction (timed by the store and, around the call, here)
    t0 = time.perf_counter()
    stats = db.compact()
    wall = time.perf_counter() - t0
    out["compaction"] = {**stats, "wall_s": wall}
    assert db.compaction_stats()["bytes_written"] == stats["bytes_written"]

    # -- after: structure, the same GETs
    post = structure(db)
    post["table_heap"] = table_memory_both(db)
    post["stage"] = get_stage(db, sets, raw, "post")
    post["scan"] = scan_and_verify(db, model, records) if full_verify else None
    assert post["memtable_entries"] == pre["memtable_entries"], "compaction changed the memtable"
    db.close()
    del db
    post["dir_closed"] = dir_state6(path)
    out["reconciliation"] = reconcile(pre, post, stats)

    # -- recovery after compaction, and correctness after restart
    t0 = time.perf_counter_ns()
    db = KVStore(path, sync=False, memtable_limit_bytes=limit)
    open_post_ns = time.perf_counter_ns() - t0
    reopened = structure(db)
    comp_post = open_components(path)
    cache = db.cache_stats()
    out["cache_after_reopen"] = {k: cache[k] for k in ("entries", "bytes", "hits", "misses")}
    assert cache["entries"] == cache["hits"] == cache["misses"] == 0, "the cache did not start empty after reopen"
    out["verification"] = verify_reopened(db, path, model, records, plan, sets)
    for key in ("tables", "table_bytes", "records", "index_entries", "memtable_entries"):
        assert reopened[key] == post[key], f"reopen changed {key}"
    db.close()
    out.update({"pre": pre, "post": post, "reopened": reopened,
                "recovery": {"pre": {"open_ns": open_pre_ns, "components": comp_pre},
                             "post": {"open_ns": open_post_ns, "components": comp_post}}})
    return out, raw


def reconcile(pre, post, st):
    """Every identity that must hold between the structure before, the structure after and the compaction statistics.
    A failed identity raises: nothing is silently dropped."""
    r = {}
    noop = st["compactions"] == 0
    r["noop"] = noop
    if noop:
        for key in ("tables", "table_bytes", "records"):
            assert pre[key] == post[key], f"a no-op compaction changed {key}"
        assert pre["tables"] == 0 or (pre["tables"] == 1 and pre["cmp_tables"] == 1)
        r["identities"] = {"noop_state_unchanged": True}
        return r
    ids = {
        "input_tables == tables before": st["input_tables"] == pre["tables"],
        "output_tables == tables after == 1": st["output_tables"] == post["tables"] == 1 and post["cmp_tables"] == 1,
        "records_read == 2 x records before": st["records_read"] == 2 * pre["records"],
        "records_emitted == records after": st["records_emitted"] == post["records"],
        "records_read / 2 == emitted + obsolete + tombstones":
            st["records_read"] // 2 == st["records_emitted"] + st["obsolete_records_removed"] + st["tombstones_removed"],
        "bytes_read == 2 x record bytes before": st["bytes_read"] == 2 * pre["data_bytes"],
        "input_table_bytes == table bytes before": st["input_table_bytes"] == pre["table_bytes"],
        "bytes_written == output_table_bytes == table bytes after":
            st["bytes_written"] == st["output_table_bytes"] == post["table_bytes"],
        "record bytes after < record bytes before or equal": post["data_bytes"] <= pre["data_bytes"],
    }
    if pre["scan"] is not None:
        sc, ps = pre["scan"], post["scan"]
        ids.update({
            "scan: physical == records before": sc["physical"] == pre["records"],
            "scan: live == records_emitted": sc["live"] == st["records_emitted"],
            "scan: tombstones == tombstones_removed": sc["tombstone"] == st["tombstones_removed"],
            "scan: obsolete == obsolete_records_removed": sc["obsolete"] == st["obsolete_records_removed"],
            "scan: live + tombstone + obsolete bytes == record bytes before":
                sc["live_record_bytes"] + sc["tombstone_record_bytes"] + sc["obsolete_record_bytes"] == pre["data_bytes"],
            "scan: live record bytes == record bytes after": sc["live_record_bytes"] == post["data_bytes"],
            "scan after: no obsolete, no tombstone, live == records after":
                ps["obsolete"] == 0 and ps["tombstone"] == 0 and ps["live"] == post["records"],
            "logical live keys unchanged by compaction": sc["logical_live"] == ps["logical_live"],
        })
    assert all(ids.values()), f"reconciliation failed: {[k for k, v in ids.items() if not v]}"
    r["identities"] = ids
    return r


# ---- one run: load, (control copy), churn, measure -------------------------------------------------------------------------
def v6_run(records, limit, n_hits, n_misses, v0_gets, run_idx, control,
           full_verify, probe):
    check_headroom()
    value_rng, *_ = workload(records, n_hits, n_misses, v0_gets)
    model = array("q", bytes(8 * records))
    raw_all = {}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "db")
        load = load_database(path, records, limit, value_rng, model)
        load["state"] = dir_state6(path)
        model_control = None
        if control:
            shutil.copytree(path, os.path.join(d, "control"))
            model_control = array("q", model)
        churn = apply_churn(path, records, limit, model)
        churn["state"] = dir_state6(path)
        probe_dir = None
        if probe:
            probe_dir = os.path.join(d, "probe")
            shutil.copytree(path, probe_dir)
        results = {}
        results["churn"], raw_all["churn"] = measure_workload("churn", path, model, records, limit, n_hits, n_misses,
                                                              v0_gets, churn, full_verify)
        if control:
            results["control"], raw_all["control"] = measure_workload("control", os.path.join(d, "control"),
                                                                       model_control, records, limit, n_hits, n_misses,
                                                                       v0_gets, None, full_verify)
        memory = run_memory_probe(probe_dir, limit) if probe else None
    assert not os.path.exists(d)
    churn_plan = {k: v for k, v in churn.items() if k not in ("samples", "dead_pool")}
    return {"load": load, "churn_phase": churn_plan, "workloads": results, "memory_probe": memory, "_raw": raw_all}


# ---- memory probe + compaction I/O counters (fresh processes; the compaction in the probe is instrumented) ----------------
class IOCounters:
    """Counts reads through `sstable._open_table` (every read of an input table's records goes through it) and fsyncs.
    Bytes are those the Python file object returned to its caller: a logical count above the OS and its read-ahead."""

    def __init__(self):
        self.opens = self.read_calls = self.bytes_read = self.fsyncs = 0

    def __enter__(self):
        c = self
        self._open, self._fsync = sstable_module._open_table, os.fsync
        real_open, real_fsync = self._open, self._fsync

        class Counted:
            def __init__(self, f):
                self._f = f

            def __enter__(self):
                self._f.__enter__()
                return self

            def __exit__(self, *a):
                return self._f.__exit__(*a)

            def seek(self, *a):
                return self._f.seek(*a)

            def read(self, n=-1):
                data = self._f.read(n)
                c.read_calls += 1
                c.bytes_read += len(data)
                return data

        def open_table(path):
            c.opens += 1
            return Counted(real_open(path))

        def fsync(fd):
            c.fsyncs += 1
            return real_fsync(fd)

        sstable_module._open_table, os.fsync = open_table, fsync
        return self

    def __exit__(self, *exc):
        sstable_module._open_table, os.fsync = self._open, self._fsync

    def as_dict(self):
        return {"table_opens": self.opens, "read_calls": self.read_calls, "bytes_read": self.bytes_read,
                "fsyncs": self.fsyncs}


def probe_main(args):
    gc.collect()
    before = process_memory()
    db = KVStore(args.probe_path, sync=False, memtable_limit_bytes=args.probe_limit)
    gc.collect()
    after_open = process_memory()
    out = {"mode": args.probe, "before": before, "after_open": after_open, "structure": structure(db)}
    if args.probe == "compact":
        with IOCounters() as io:
            stats = db.compact()
        gc.collect()
        out.update({"after_compact": process_memory(), "io": io.as_dict(), "stats": stats,
                    "structure_after": structure(db)})
    db.close()
    return out


def run_memory_probe(probe_dir, limit):
    out = {}
    for mode in ("compact", "open"):  # `compact` leaves the compacted directory that `open` then recovers
        cmd = [sys.executable, str(Path(__file__).resolve()), "--probe", mode, "--probe-path", probe_dir,
               "--probe-limit", str(limit)]
        out[mode] = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1])
    c, o = out["compact"], out["open"]
    stats = c["stats"]
    if stats["compactions"]:
        assert c["io"]["bytes_read"] == stats["bytes_read"], "counted input bytes differ from the store's bytes_read"
        assert c["io"]["table_opens"] == 2 * stats["input_tables"], "each input table must be opened once per pass"
    return {
        "pre_open": {"private_delta": c["after_open"]["private"] - c["before"]["private"],
                     "working_set_delta": c["after_open"]["working_set"] - c["before"]["working_set"],
                     "peak_working_set": c["after_open"]["peak_working_set"],
                     "tables": c["structure"]["tables"], "index_entries": c["structure"]["index_entries"],
                     "filter_bytes": c["structure"]["filter_bytes"], "memtable_entries": c["structure"]["memtable_entries"]},
        "compaction": {"working_set_before": c["after_open"]["working_set"], "private_before": c["after_open"]["private"],
                       "peak_working_set_after": c["after_compact"]["peak_working_set"],
                       "peak_working_set_over_open": c["after_compact"]["peak_working_set"] - c["after_open"]["working_set"],
                       "private_after": c["after_compact"]["private"],
                       "private_change": c["after_compact"]["private"] - c["after_open"]["private"],
                       "working_set_after": c["after_compact"]["working_set"], "io": c["io"], "stats": stats},
        "post_open": {"private_delta": o["after_open"]["private"] - o["before"]["private"],
                      "working_set_delta": o["after_open"]["working_set"] - o["before"]["working_set"],
                      "peak_working_set": o["after_open"]["peak_working_set"],
                      "tables": o["structure"]["tables"], "index_entries": o["structure"]["index_entries"],
                      "filter_bytes": o["structure"]["filter_bytes"], "memtable_entries": o["structure"]["memtable_entries"]},
    }


# ---- pooling and the scale entry --------------------------------------------------------------------------------------------
def v6_entry(records, limit, n_hits, n_misses, n_runs, n_control):
    runs = []
    pooled = {}
    v0_gets = V0_SCALES[records]["gets_per_kind"]
    for i in range(n_runs):
        control = i < n_control
        full_verify = i == 0 or records <= 1_000_000
        probe = i < MEMORY_PROBES
        print(f"scale {records:>10,} V6 run {i + 1}/{n_runs}  control={control} full_verify={full_verify} probe={probe} ...",
              flush=True)
        t0 = time.perf_counter()
        r = v6_run(records, limit, n_hits, n_misses, v0_gets, i, control, full_verify, probe)
        for kind, raw in r.pop("_raw").items():
            for (stage, ph), lat in raw.items():
                pooled.setdefault((kind, stage, ph), array("q")).extend(lat)
        runs.append(r)
        c = r["workloads"]["churn"]
        s = c["compaction"]
        print(f"  done in {time.perf_counter() - t0:.0f}s  tables {c['pre']['tables']}->{c['post']['tables']}  "
              f"compact={s['wall_s']:.2f}s  records {s['records_read'] // 2 if s['compactions'] else 0}->{s['records_emitted']}  "
              f"hit p50 {c['pre']['stage']['phases']['get_hit']['summary']['p50_us']:.1f}->"
              f"{c['post']['stage']['phases']['get_hit']['summary']['p50_us']:.1f}us  "
              f"open {c['recovery']['pre']['open_ns'] / 1e9:.3f}->{c['recovery']['post']['open_ns'] / 1e9:.3f}s", flush=True)
        gc.collect()
    pooled_summary = {}
    for (kind, stage, ph), lat in pooled.items():
        pooled_summary.setdefault(kind, {}).setdefault(stage, {})[ph] = {"ops": len(lat), **full_summary(lat)}
    return {"records": records, "memtable_limit_bytes": limit, "gets_hit_per_run": n_hits, "gets_miss_per_run": n_misses,
            "runs_planned": n_runs, "runs_completed": len(runs), "control_runs": n_control,
            "index_interval": INDEX_INTERVAL, "bloom_bits_per_key": BITS_PER_KEY,
            "block_cache_bytes": DEFAULT_BLOCK_CACHE_BYTES, "runs": runs, "pooled": pooled_summary}


# ---- crash / restart sanity (small; the implementation tests hold the full matrix) -------------------------------------------
def crash_sanity(records, limit):
    """Four crash windows on copies of one churned database, each followed by a reopen and a full check against the model,
    plus a clean compaction -> reopen -> second compaction (which must be a no-op)."""
    n_hits = min(GET_HITS[records], 2000)
    value_rng, *_ = workload(records, n_hits, 10, 10)
    model = array("q", bytes(8 * records))
    out = {"records": records, "memtable_limit_bytes": limit, "cases": {}}
    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, "base")
        load_database(base, records, limit, value_rng, model)
        apply_churn(base, records, limit, model)
        out["tables_before"] = dir_state6(base)["sst_files"]
        orig_retire, orig_write = KVStore._retire, sstable_module.write_sstable

        def reopen_and_check(path, name, expect_cmp):
            db = KVStore(path, sync=False, memtable_limit_bytes=limit)
            scan = scan_and_verify(db, model, records)
            files = sorted(n for n in os.listdir(path) if TABLE_RE.match(n))
            tabs = db.table_count()
            res = {"tables_after_reopen": tabs, "files": len(files), "cmp_tables": sum(n.startswith("cmp-") for n in files),
                   "sst_tables_left": sum(n.startswith("sst-") for n in files), "tmp_files": dir_state6(path)["tmp_files"],
                   "live_keys": scan["logical_live"], "obsolete_after_reopen": scan["obsolete"],
                   "cache_entries_at_open": db.cache_stats()["entries"]}
            db.close()
            assert res["live_keys"] == records - model.count(-1) and res["tmp_files"] == 0
            assert res["cmp_tables"] == (1 if expect_cmp else 0)
            res["ok"] = True
            out["cases"][name] = res

        # 1. clean compaction, reopen, compact again
        p = os.path.join(d, "clean")
        shutil.copytree(base, p)
        db = KVStore(p, sync=False, memtable_limit_bytes=limit)
        db.compact()
        db.close()
        reopen_and_check(p, "completed_then_reopen", True)
        db = KVStore(p, sync=False, memtable_limit_bytes=limit)
        again = db.compact()
        db.close()
        out["cases"]["completed_then_reopen"]["second_compaction_is_noop"] = again["compactions"] == 0
        assert again["compactions"] == 0

        # 2. crash after the output is installed, before any old table is deleted
        p = os.path.join(d, "after_commit")
        shutil.copytree(base, p)
        db = KVStore(p, sync=False, memtable_limit_bytes=limit)
        KVStore._retire = lambda self, old: (_ for _ in ()).throw(RuntimeError("simulated crash"))
        try:
            db.compact()
        except RuntimeError:
            pass
        finally:
            KVStore._retire = orig_retire
        out["cases_state_before_recovery"] = {"after_commit": dir_state6(p)}
        db._wal.close()
        reopen_and_check(p, "crash_after_commit_before_cleanup", True)

        # 3. crash while deleting the old tables (only the first old table was removed)
        p = os.path.join(d, "mid_cleanup")
        shutil.copytree(base, p)
        db = KVStore(p, sync=False, memtable_limit_bytes=limit)

        def partial_retire(self, old):
            os.remove(old[0].path)
            raise RuntimeError("simulated crash")

        KVStore._retire = partial_retire
        try:
            db.compact()
        except RuntimeError:
            pass
        finally:
            KVStore._retire = orig_retire
        out["cases_state_before_recovery"]["mid_cleanup"] = dir_state6(p)
        db._wal.close()
        reopen_and_check(p, "crash_during_cleanup", True)

        # 4. crash while writing the output: a half-built .tmp file, old tables untouched
        p = os.path.join(d, "mid_write")
        shutil.copytree(base, p)
        db = KVStore(p, sync=False, memtable_limit_bytes=limit)

        def failing_write(path, items, **kw):
            with open(path + ".tmp", "wb") as f:
                f.write(b"half a table")
            raise RuntimeError("simulated crash")

        sstable_module.write_sstable = failing_write
        try:
            db.compact()
        except RuntimeError:
            pass
        finally:
            sstable_module.write_sstable = orig_write
        out["cases_state_before_recovery"]["mid_write"] = dir_state6(p)
        db._wal.close()
        reopen_and_check(p, "crash_during_output_write", False)
    return out


# ---- metadata / orchestration -------------------------------------------------------------------------------------------------
def metadata(args):
    ram = ram_bytes()
    overrides = {k: getattr(args, k) for k in ("memtable_limit_bytes", "hits", "misses", "runs", "control_runs")
                 if getattr(args, k) is not None}
    return {
        "benchmark": "v6",
        "command": " ".join([Path(sys.executable).name] + sys.argv),
        "overrides": overrides or None,
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "ram_total_bytes": ram[0] if ram else None,
        "cache_condition": "WARM OS page cache. No cold-cache measurement is made or claimed.",
        "params": {
            "seed": SEED, "key_size_bytes": len(key_of(0)), "value_size_bytes": VALUE_SIZE, "record_bytes": RECORD_BYTES,
            "index_interval": INDEX_INTERVAL, "bloom_bits_per_key": BITS_PER_KEY, "bloom_num_hashes": NUM_HASHES,
            "block_cache_bytes": DEFAULT_BLOCK_CACHE_BYTES, "store_sync": False,
            "memtable_limit_bytes": args.memtable_limit_bytes or DEFAULT_MEMTABLE_LIMIT_BYTES,
            "default_memtable_limit_bytes": DEFAULT_MEMTABLE_LIMIT_BYTES, "small_limit_bytes": SMALL_LIMIT_BYTES,
            "runs_per_scale": {str(r): c["runs"] for r, c in V0_SCALES.items()},
            "control_runs_per_scale": {str(r): c for r, c in CONTROL_RUNS.items()},
            "gets_hit_per_run": {str(r): c for r, c in GET_HITS.items()},
            "gets_miss_per_run": {str(r): c for r, c in GET_MISSES.items()},
            "instrumented_sample_gets_per_run": {"hit": STRUCT_HITS, "miss_like": STRUCT_MISSES},
            "memory_probes_per_scale": MEMORY_PROBES, "churn": CHURN, "verify_uniform_keys": VERIFY_UNIFORM,
            "category_sample_keys": CATEGORY_SAMPLE, "small_limit_gets": {"hit": SMALL_HITS, "miss": SMALL_MISSES},
        },
        "timing_notes": "Only store.get calls are timed (one perf_counter_ns pair per GET, as V0..V5); percentiles are "
        "nearest-rank (same code). compact() is timed twice: by the store (duration_s, inside the call) and around the "
        "call (wall_s). The cache outcome of each GET and its result checksum are recorded outside the timed interval. "
        "The read-work samples, the table-heap measurement, the full scans, the correctness checks and the memory/I-O "
        "probes are untimed and run between or after the timed phases. open_recovery is the first KVStore(path) after "
        "close() in the same process (before) and the reopen after the compacted store was closed (after), both with "
        "an empty cache.",
    }


def checkpoint(result, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(RESULTS / "v6_benchmark.json"))
    ap.add_argument("--scales", default=None, help="comma-separated record counts from the V0 scale ladder")
    ap.add_argument("--small", action="store_true", help="10,000 keys with the 16 KiB memtable limit")
    ap.add_argument("--crash", action="store_true", help="small crash/restart sanity checks")
    ap.add_argument("--analyze-only", action="store_true")
    ap.add_argument("--probe", choices=["open", "compact"])
    ap.add_argument("--probe-path")
    ap.add_argument("--probe-limit", type=int)
    # Sanity/test overrides; recorded in the metadata and The final protocol uses none.
    ap.add_argument("--memtable-limit-bytes", type=int, default=None)
    ap.add_argument("--hits", type=int, default=None)
    ap.add_argument("--misses", type=int, default=None)
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--control-runs", type=int, default=None)
    args = ap.parse_args()
    out_path = Path(args.out)
    limit = args.memtable_limit_bytes or DEFAULT_MEMTABLE_LIMIT_BYTES

    if args.probe:
        print(json.dumps(probe_main(args)))
        return

    if args.analyze_only:
        from analyze import analyze

        result = json.loads(out_path.read_text())
        result["analysis"] = analyze(result)
        checkpoint(result, out_path)
        print(json.dumps({"scales": list(result["scales"]), "small_limit": bool(result.get("small_limit"))}))
        return
    result = {"metadata": metadata(args), "status": "in_progress", "scales": {}, "small_limit": None, "crash_sanity": []}

    if args.crash:
        for records, lim in ((10_000, SMALL_LIMIT_BYTES), (100_000, DEFAULT_MEMTABLE_LIMIT_BYTES)):
            print(f"crash sanity {records:,} records, limit {lim}", flush=True)
            result["crash_sanity"].append(crash_sanity(records, args.memtable_limit_bytes or lim))
            checkpoint(result, out_path)
        result["status"] = "complete"
        checkpoint(result, out_path)
        print("wrote", out_path)
        return

    if args.small:
        n_runs = args.runs or V0_SCALES[10_000]["runs"]
        result["small_limit"] = v6_entry(10_000, SMALL_LIMIT_BYTES, args.hits or SMALL_HITS,
                                         args.misses or SMALL_MISSES, n_runs, n_runs)
        result["status"] = "complete"
        checkpoint(result, out_path)
        print("wrote", out_path)
        return

    for records in [int(s) for s in (args.scales or ",".join(map(str, SCALES))).split(",")]:
        assert records in V0_SCALES, "scale must be on the V0 ladder"
        n_runs = args.runs or V0_SCALES[records]["runs"]
        n_control = min(n_runs, args.control_runs if args.control_runs is not None else CONTROL_RUNS[records])
        result["scales"][str(records)] = v6_entry(records, limit, args.hits or GET_HITS[records],
                                                  args.misses or GET_MISSES[records], n_runs, n_control)
        checkpoint(result, out_path)
    result["status"] = "complete"
    checkpoint(result, out_path)
    print("wrote", out_path)


if __name__ == "__main__":
    main()
