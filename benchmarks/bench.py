"""V3 benchmark: V2 plus a sparse index in every SSTable (default interval 16). Methodology: docs/benchmarks.md.

Usage:
    python benchmarks/bench.py --scales 10000 --out PATH       # scaling runs for the listed scales
    python benchmarks/bench.py --baseline --out PATH           # 10,000-key head-to-head workloads
    python benchmarks/bench.py --analyze-only --out FINAL.json

The protocol is V2's (py): same seed, keys, values, workloads, scale ladder, run counts, GET sample counts,
4 MiB memtable limit, timing boundary (only the store call is timed, one perf_counter_ns pair per operation) and
percentile code. Only the store under test changes (KVStore with its default index interval of 16). The V0, V1 and
V2 results (benchmarks/results/*.json) are read, never written. All numbers are WARM OS-CACHE numbers.

No production code is changed. Flush boundaries are observed by wrapping the instance's `flush` method and the
module-level `lsm_store.sstable.write_sstable` (the V3 store calls it through that module). Read work is counted
untimed by wrapping `SSTable.lookup`, `SSTable.locate`, `SSTable._scan_block` and the record reader. After every timed
phase of a run (so that nothing timed is affected) an untimed pass decomposes the cost of one table lookup and
measures the index's retained memory.
"""

import argparse
import gc
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from array import array
from pathlib import Path

import lsm_store.sstable as sstable_module
from lsm_store import KVStore
from lsm_store.record import HEADER_SIZE
from lsm_store.sstable import FOOTER_SIZE, FOOTER_V3_SIZE, SSTable
from lsm_store.store import DEFAULT_MEMTABLE_LIMIT_BYTES
from lsm_store.store import DEFAULT_INDEX_INTERVAL
from lsm_store.wal import replay

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


def build_workload(seed, keyspace, n_get, n_mixed, n_sync):
    """Pre-generate every operation so no RNG work happens inside timed regions."""
    rng = random.Random(seed)
    value = lambda: rng.randbytes(VALUE_SIZE)  # noqa: E731
    load = [(key_of(i), value()) for i in range(keyspace)]
    gets = []
    for i in range(n_get):
        # even i: existing key (hit); odd i: key outside the keyspace (miss)
        gets.append(key_of(rng.randrange(keyspace)) if i % 2 == 0 else key_of(keyspace + i))
    mixed = []
    for _ in range(n_mixed):
        r = rng.random()
        k = key_of(rng.randrange(keyspace))
        if r < 0.70:
            mixed.append(("get", k, None))
        elif r < 0.95:
            mixed.append(("put", k, value()))
        else:
            mixed.append(("delete", k, None))
    sync_puts = [(key_of(i), value()) for i in range(n_sync)]
    return {"load": load, "gets": gets, "mixed": mixed, "sync_puts": sync_puts}


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


def aggregate(runs):
    agg = {}
    for name in runs[0]["summary"]:
        agg[name] = {}
        for metric in runs[0]["summary"][name]:
            vals = [r["summary"][name][metric] for r in runs]
            agg[name][metric] = {"median": statistics.median(vals), "min": min(vals), "max": max(vals)}
    return agg


def ols(xs, ys):
    """Least squares y = a + b x; returns (a, b, r2)."""
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return a, b, (1 - ss_res / ss_tot) if ss_tot else None  # R^2 undefined for a constant series


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
DELETE_RECORD_BYTES = HEADER_SIZE + len(key_of(0))  # 24: a DELETE record carries no value


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
STRUCT_MISSES = 3
VERIFY_SAMPLE = 200  # untimed spot checks of hit keys after the final reopen
BASELINE_RUNS = 5
SMALL_LIMIT_BYTES = 16 * 1024  # baseline variant in which a 10,000-key database is spread over many SSTables
PUT_RECORD_BYTES = RECORD_BYTES  # 124
ENTRY_PAYLOAD = len(key_of(0)) + VALUE_SIZE  # 111


# ---- flush instrumentation (no production code changed) ----------------------------------------
def dir_state(path):
    wal_files = wal_bytes = sst_files = sst_bytes = 0
    for name in os.listdir(path):
        # os.path.getsize, not DirEntry.stat: on Windows the latter can report a stale size for a file still open for append.
        if name.startswith("wal-"):
            wal_files += 1
            wal_bytes += os.path.getsize(os.path.join(path, name))
        elif name.startswith("sst-") and name.endswith(".sst"):
            sst_files += 1
            sst_bytes += os.path.getsize(os.path.join(path, name))
    return {"wal_files": wal_files, "wal_bytes": wal_bytes, "sst_files": sst_files, "sst_bytes": sst_bytes}


class WriteStats:
    """Latencies of one write phase, split into operations that flushed and operations that did not."""

    def __init__(self):
        self.all = array("q")
        self.normal = array("q")
        self.trigger = array("q")
        self.flushes = []


def timed_write(db, probe, stats, op, key, value, index):
    # Whether this write will flush is a pure function of the memtable's byte count and the limit; reading it here,
    # outside the timed region, lets us snapshot the directory before the flush and cross-check the probe afterwards.
    will_flush = db._mem_bytes >= db._limit
    before = dir_state(db._dir) if will_flush else None
    probe.last = None
    ns = timed_call(db.put, key, value)[0] if op == "put" else timed_call(db.delete, key)[0]
    flushed = probe.last is not None
    assert flushed == will_flush, "flush detection disagrees with the memtable-limit rule"
    stats.all.append(ns)
    if not flushed:
        stats.normal.append(ns)
        return
    stats.trigger.append(ns)
    table = db._sstables[-1]
    after = dir_state(db._dir)
    stats.flushes.append({
        "op_index": index, "op": op, "op_ns": ns, **probe.last,
        "table_records": table.count, "table_bytes": os.path.getsize(table.path),
        "wal_files_before": before["wal_files"], "wal_bytes_before": before["wal_bytes"],
        "wal_files_after": after["wal_files"], "wal_bytes_after": after["wal_bytes"],
    })


def write_split(stats):
    """Summaries of all writes, of writes that did not flush, and of the writes that triggered a flush."""
    out = {"ops": len(stats.all), "n_flush_triggering_ops": len(stats.trigger),
           "all": full_summary(stats.all) if stats.all else None,
           "non_flush": full_summary(stats.normal) if stats.normal else None,
           "flush_trigger": full_summary(stats.trigger) if stats.trigger else None}
    if stats.trigger and stats.normal:
        fastest_trigger = min(stats.trigger)
        out["non_flush_ops_at_least_as_slow_as_fastest_flush_trigger"] = sum(1 for x in stats.normal if x >= fastest_trigger)
        out["slowest_non_flush_ns"] = max(stats.normal)
        out["fastest_flush_trigger_ns"] = fastest_trigger
        out["trigger_p50_over_non_flush_p50"] = out["flush_trigger"]["p50_us"] / out["non_flush"]["p50_us"]
        out["max_over_non_flush_p50"] = max(stats.all) / 1e3 / out["non_flush"]["p50_us"]
        flush_ns = [f["flush_ns"] for f in stats.flushes]
        out["total_flush_ns"] = sum(flush_ns)
        out["flush_ns_p50"] = statistics.median(flush_ns)
        out["flush_share_of_trigger_op_p50"] = statistics.median(f["flush_ns"] / f["op_ns"] for f in stats.flushes)
    return out


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


def pooled(arrays):
    return array("q", (x for a in arrays for x in a))


def metric_range(vals):
    return {"median": statistics.median(vals), "min": min(vals), "max": max(vals)}


def split_aggregate(runs, phase):
    out = {}
    for part in ("all", "non_flush", "flush_trigger"):
        rows = [r["write_split"][phase][part] for r in runs if r["write_split"][phase][part]]
        out[part] = ({m: metric_range([row[m] for row in rows])
                      for m in ("p50_us", "p95_us", "p99_us", "mean_us", "max_us", "throughput_ops_per_s")}
                     if rows else None)
        out[part + "_runs_with_data"] = len(rows)
    return out


def scale_entry(records, limit, n_hits, n_misses, runs, memory):
    pooled_all = {n: full_summary(pooled(r["_raw"][n] for r in runs)) for n in ("get_hit", "get_miss")}
    for phase in ("put_overwrite", "delete"):
        normal = pooled(r["_raw_write"][phase][0] for r in runs)
        trigger = pooled(r["_raw_write"][phase][1] for r in runs)
        allv = pooled(r["_raw"][phase] for r in runs)
        pooled_all[phase] = {"all": full_summary(allv), "non_flush": full_summary(normal) if normal else None,
                             "flush_trigger": full_summary(trigger) if trigger else None}
    load_triggers = pooled(r["_raw_write"]["put_load"][1] for r in runs)
    pooled_all["put_load_flush_trigger"] = full_summary(load_triggers) if load_triggers else None
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in runs]
    opens = [r["open_recovery_ns"] / 1e9 for r in runs]
    return {
        "records": records,
        "memtable_limit_bytes": limit,
        "gets_hit_per_run": n_hits,
        "gets_miss_per_run": n_misses,
        "runs_planned": V0_SCALES[records]["runs"] if records in V0_SCALES else len(runs),
        "runs_completed": len(runs),
        "open_recovery_seconds": opens,
        "open_recovery_seconds_median": statistics.median(opens),
        "open_recovery_final_seconds": [r["open_recovery_final_ns"] / 1e9 for r in runs],
        "load_wall_seconds": [r["load_wall_seconds"] for r in runs],
        "aggregate_over_runs": aggregate([{"summary": r["summary"]} for r in runs]),
        "put_load_split_aggregate": split_aggregate(runs, "put_load"),
        "pooled_all_runs": pooled_all,
        "runs": clean,
        "memory_probes": memory,
    }


def fit(n, size, values):
    _, slope, r2_log = ols([math.log(x) for x in size], [math.log(y) for y in values])
    a, b, r2 = ols(n, values)
    return {"values": values, "loglog_slope": slope, "loglog_r2": r2_log, "linear_intercept": a,
            "linear_per_record": b, "linear_r2": r2, "ratio_largest_over_smallest": values[-1] / values[0],
            "records_ratio": n[-1] / n[0]}

# ---- protocol: identical to V2's (py), which in turn reuses V0's ladder -----------------------------------
SCALES = list(V0_SCALES)
RESULTS = ROOT / "benchmarks/results"
INDEX_ENTRY_BYTES = 4 + 8 + len(key_of(0))  # key_len(4) + offset(8) + key(11) = 23
DECOMP_REPS_MISS = 20  # repetitions of the per-table cost decomposition over all tables (miss key)
DECOMP_REPS_HIT = 10  # repetitions over the sampled (table, hit key) pairs
DECOMP_HIT_PAIRS = 200


def index_entries(count, interval=DEFAULT_INDEX_INTERVAL):
    return -(-count // interval)


def index_bytes(count, interval=DEFAULT_INDEX_INTERVAL):
    """Bytes of the index block of a table of `count` records with 11-byte keys."""
    return index_entries(count, interval) * INDEX_ENTRY_BYTES


def table_index_bytes(table):
    """Index block length read from the file itself: whatever lies between the records and the footer."""
    return os.path.getsize(table.path) - table.data_len - FOOTER_V3_SIZE


# ---- flush instrumentation (no production code changed) ----------------------------------------
class FlushProbe:
    """Observes the flushes of one KVStore instance by wrapping `db.flush` (the instance attribute that
    `_flush_if_full` calls) and `lsm_store.sstable.write_sstable` (called by `KVStore._write_table` through the
    module). Records the index interval each flush's table was written with. `last` is set while an operation
    flushes and cleared by the caller before each operation."""

    def __init__(self, db):
        self.db = db
        self.last = None
        self._write_ns = 0
        self._interval = None
        self._real_write = sstable_module.write_sstable

        def write_wrapper(path, items, **kwargs):
            t0 = time.perf_counter_ns()
            self._interval = kwargs.get("index_interval")
            try:
                return self._real_write(path, items, **kwargs)
            finally:
                self._write_ns = time.perf_counter_ns() - t0

        real_flush = db.flush

        def flush_wrapper():
            self._write_ns = 0
            self._interval = None
            entries, mem_bytes = len(db._memtable), db._mem_bytes
            t0 = time.perf_counter_ns()
            try:
                real_flush()
            finally:
                self.last = {"flush_ns": time.perf_counter_ns() - t0, "write_sstable_ns": self._write_ns,
                             "memtable_entries": entries, "memtable_bytes": mem_bytes,
                             "index_interval": self._interval}

        sstable_module.write_sstable = write_wrapper
        db.flush = flush_wrapper

    def remove(self):
        sstable_module.write_sstable = self._real_write
        self.db.__dict__.pop("flush", None)


# ---- read-work instrumentation (untimed) --------------------------------------------------------
def measure_read_work(db, keys):
    """For each key return [category, tables_consulted, tables_skipped_by_index, blocks_read, records_decoded,
    bytes_decoded] of one GET. `tables_consulted` counts SSTable.lookup calls; `tables_skipped_by_index` those whose
    binary search ruled the key out without any file access; `blocks_read` the table regions actually opened and
    scanned. Records/bytes are counted in the record reader. Application-level work, not physical I/O. Untimed."""
    work = {"tables": 0, "skipped": 0, "blocks": 0, "records": 0, "bytes": 0}
    real_lookup, real_locate = SSTable.lookup, SSTable.locate
    real_scan_block, real_read = SSTable._scan_block, sstable_module.read_records

    def lookup(self, key):
        work["tables"] += 1
        return real_lookup(self, key)

    def locate(self, key):
        found = real_locate(self, key)
        if found is None:
            work["skipped"] += 1
        return found

    def scan_block(self, *args):
        work["blocks"] += 1
        return real_scan_block(self, *args)

    def counting(f):
        for rec in real_read(f):
            work["records"] += 1
            work["bytes"] += HEADER_SIZE + len(rec.key) + len(rec.value)
            yield rec

    out = []
    SSTable.lookup, SSTable.locate, SSTable._scan_block = lookup, locate, scan_block
    sstable_module.read_records = counting
    try:
        for key in keys:
            work.update(tables=0, skipped=0, blocks=0, records=0, bytes=0)
            value = db.get(key)
            if value is None:
                cat = "miss"
            elif work["tables"] == 0:
                cat = "memtable_hit"
            elif work["tables"] == 1:
                cat = "newest_sstable_hit"
            else:
                cat = "older_sstable_hit"
            out.append([cat, work["tables"], work["skipped"], work["blocks"], work["records"], work["bytes"]])
    finally:
        SSTable.lookup, SSTable.locate, SSTable._scan_block = real_lookup, real_locate, real_scan_block
        sstable_module.read_records = real_read
    return out


# ---- untimed cost decomposition and index memory (after all timed phases of a run) -----------------------------
def _per_item_ns(total_ns, items):
    return total_ns / items


def decompose(db, hit_idx, miss_key):
    """Median over repetitions of the cost per table lookup, split into the pieces a V3 lookup is made of:
    `locate` (binary search of the in-memory index), `open_close` (opening and closing the table file),
    `block_read` (open + seek + read the located block's bytes, no decoding) and `lookup` (the full SSTable.lookup).
    The miss key is above every stored key, so it selects the last block of every table; the hit pairs are
    (table, key) for sampled hit keys that live in a flushed table. Batched timing (one clock pair per loop), so the
    clock overhead is amortized. Not part of any timed phase. Returns None if there are no tables."""
    tables = list(db._sstables)
    if not tables:
        return None
    entries_per_table = tables[0].count
    flushed = len(tables)

    def measure(pairs, reps):
        spans = [(t.path, *t.locate(k)[1:]) for t, k in pairs]
        samples = {"locate": [], "open_close": [], "block_read": [], "lookup": []}
        n = len(pairs)
        for _ in range(reps):
            t0 = time.perf_counter_ns()
            for t, k in pairs:
                t.locate(k)
            t1 = time.perf_counter_ns()
            for path, _s, _e in spans:
                open(path, "rb").close()
            t2 = time.perf_counter_ns()
            for path, start, end in spans:
                with open(path, "rb") as f:
                    f.seek(start)
                    f.read(end - start)
            t3 = time.perf_counter_ns()
            for t, k in pairs:
                t.lookup(k)
            t4 = time.perf_counter_ns()
            for name, ns in zip(samples, (t1 - t0, t2 - t1, t3 - t2, t4 - t3)):
                samples[name].append(_per_item_ns(ns, n))
        return {"pairs": n, "reps": reps, **{f"{name}_ns_median": statistics.median(v) for name, v in samples.items()},
                **{f"{name}_ns_min_max": [min(v), max(v)] for name, v in samples.items()}}

    out = {"miss": measure([(t, miss_key) for t in tables], DECOMP_REPS_MISS)}
    hit_pairs = [(tables[i // entries_per_table], key_of(i)) for i in hit_idx[:DECOMP_HIT_PAIRS]
                 if i // entries_per_table < flushed and i < flushed * entries_per_table]
    out["hit"] = measure(hit_pairs, DECOMP_REPS_HIT) if hit_pairs else None
    return out


def index_memory(paths):
    """Bytes of Python heap retained by opening the tables (tracemalloc, current traced size after minus before):
    the sparse indexes plus the SSTable objects. Also the transient peak during the opens. Run after all timed
    phases; tracemalloc is stopped again before returning."""
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    tables = [SSTable(p) for p in paths]
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    entries = sum(len(t._keys) for t in tables)
    return {"tables": len(tables), "index_entries": entries, "retained_bytes": current - base,
            "peak_bytes_above_base": peak - base,
            "retained_bytes_per_entry": (current - base) / entries if entries else None}


# ---- memory probes ---------------------------------------------------------------------------
def memory_probe(mode, records, path, limit):
    """Run inside a fresh process. 'load': build a V3 database of `records` keys; 'open': recover an existing one."""
    gc.collect()
    before = process_memory()
    db = KVStore(path, sync=False, memtable_limit_bytes=limit)
    if mode == "load":
        rng = random.Random(SEED)
        for i in range(records):
            db.put(key_of(i), rng.randbytes(VALUE_SIZE))
    gc.collect()
    after = process_memory()
    out = {"mode": mode, "records": records, "before": before, "after": after, "tables": len(db._sstables),
           "index_entries": sum(len(t._keys) for t in db._sstables),
           "memtable_entries": len(db._memtable), "memtable_payload_bytes": db._mem_bytes,
           "persistent": dir_state(path)}
    db.close()
    return out


def run_memory_probe(records, limit):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "db")
        out = {}
        for mode in ("load", "open"):
            cmd = [sys.executable, str(Path(__file__).resolve()), "--probe", mode, "--probe-records", str(records),
                   "--probe-path", path, "--probe-limit", str(limit)]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            out[mode] = json.loads(proc.stdout.strip().splitlines()[-1])
    return {mode: {"private_delta": p["after"]["private"] - p["before"]["private"],
                   "working_set_delta": p["after"]["working_set"] - p["before"]["working_set"],
                   "peak_working_set_after": p["after"]["peak_working_set"],
                   "private_after": p["after"]["private"], "private_before": p["before"]["private"],
                   "tables": p["tables"], "index_entries": p["index_entries"],
                   "memtable_entries": p["memtable_entries"],
                   "memtable_payload_bytes": p["memtable_payload_bytes"], "persistent": p["persistent"]}
            for mode, p in out.items()}


# ---- V3 scaling run --------------------------------------------------------------------------
def open_components(path):
    """Re-run the steps of KVStore.__init__ (inherited from V2) with the module's own functions to split the open
    time. Read-only: the WAL replay of an intact newest segment truncates nothing. `index_read_io` re-reads exactly
    the bytes SSTable() reads (28-byte probe, 44-byte footer, index block) without parsing or validating them."""
    t0 = time.perf_counter_ns()
    names = os.listdir(path)
    sst = sorted(n for n in names if n.startswith("sst-") and n.endswith(".sst"))
    wal = sorted(n for n in names if n.startswith("wal-"))
    t1 = time.perf_counter_ns()
    tables = [SSTable(os.path.join(path, n)) for n in sst]
    t2 = time.perf_counter_ns()
    replayed = 0
    for n in wal:
        replayed += len(replay(os.path.join(path, n)))
    t3 = time.perf_counter_ns()
    spans = [(t.path, os.path.getsize(t.path), t.data_len, table_index_bytes(t)) for t in tables]
    t4 = time.perf_counter_ns()
    for p, size, data_len, ilen in spans:
        with open(p, "rb") as f:
            f.seek(size - FOOTER_SIZE)
            f.read(FOOTER_SIZE)
            f.seek(size - FOOTER_V3_SIZE)
            f.read(FOOTER_V3_SIZE)
            f.seek(data_len)
            f.read(ilen)
    t5 = time.perf_counter_ns()
    return {"list_dir_ns": t1 - t0, "sstable_open_ns": t2 - t1, "wal_replay_ns": t3 - t2,
            "index_read_io_ns": t5 - t4, "tables": len(tables), "wal_segments": len(wal),
            "memtable_entries_replayed": replayed, "index_bytes_read": sum(s[3] for s in spans),
            "index_entries_loaded": sum(len(t._keys) for t in tables),
            "footer_bytes_read": len(tables) * (FOOTER_SIZE + FOOTER_V3_SIZE)}


def v3_run(records, limit, n_hits, n_misses, v0_gets):
    value_rng, hit_idx, overwrites, miss_keys, del_keys = workload(records, n_hits, n_misses, v0_gets)
    hit_keys = [key_of(i) for i in hit_idx]

    check_headroom()
    hit_wanted = set(hit_idx)
    expected_hit = {}
    raw = {}
    entries_per_table = -(-limit // ENTRY_PAYLOAD)  # ceil: flush fires on the write after the memtable reaches limit
    table_index_b = index_bytes(entries_per_table)
    table_bytes = entries_per_table * PUT_RECORD_BYTES + table_index_b + FOOTER_V3_SIZE
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "db")
        db = KVStore(path, sync=False, memtable_limit_bytes=limit)
        assert db._index_interval == 16
        probe = FlushProbe(db)
        try:
            load = WriteStats()
            wall0 = time.perf_counter()
            for i in range(records):
                k, v = key_of(i), value_rng.randbytes(VALUE_SIZE)
                if i in hit_wanted:
                    expected_hit[i] = v
                timed_write(db, probe, load, "put", k, v, i)
            load_wall_s = time.perf_counter() - wall0
        finally:
            probe.remove()
        load_split = write_split(load)
        n_flushes_load = len(load.flushes)
        # Accounting checks after the load (untimed).
        flushed_records = sum(f["table_records"] for f in load.flushes)
        assert len(db._sstables) == n_flushes_load == (records - 1) // entries_per_table
        assert all(f["table_records"] == entries_per_table and f["index_interval"] == 16 for f in load.flushes)
        assert all(f["table_bytes"] == table_bytes for f in load.flushes)
        assert all(t.interval == 16 and len(t._keys) == index_entries(entries_per_table) for t in db._sstables)
        assert flushed_records + len(db._memtable) == records
        state_after_load = dir_state(path)
        assert state_after_load["wal_files"] == 1 and state_after_load["sst_files"] == n_flushes_load
        assert state_after_load["wal_bytes"] == len(db._memtable) * PUT_RECORD_BYTES
        assert state_after_load["sst_bytes"] == n_flushes_load * table_bytes
        load_max_ns = max(load.all)
        load_flushes = load.flushes
        load_trigger_ns = list(load.trigger)
        del load
        db.close()
        del db

        # open_recovery: the first KVStore(path) after close() in this process.
        t0 = time.perf_counter_ns()
        db = KVStore(path, sync=False, memtable_limit_bytes=limit)
        open_ns = time.perf_counter_ns() - t0
        assert len(db._sstables) == n_flushes_load and len(db._memtable) == records - flushed_records
        components = open_components(path)  # a separate repetition of the same steps, for the breakdown

        # GETs (timed). Every result is checked after the timed call.
        hit_lat = array("q")
        for idx, k in zip(hit_idx, hit_keys):
            ns, got = timed_call(db.get, k)
            assert got == expected_hit[idx], f"GET hit returned wrong value for index {idx}"
            hit_lat.append(ns)
        miss_lat = array("q")
        for k in miss_keys:
            ns, got = timed_call(db.get, k)
            assert got is None, f"GET miss returned a value for {k!r}"
            miss_lat.append(ns)
        raw["get_hit"], raw["get_miss"] = hit_lat, miss_lat

        # Structural read work of a sample of GETs (untimed, instrumented).
        read_work = {"hit": measure_read_work(db, hit_keys[:STRUCT_HITS]),
                     "miss": measure_read_work(db, miss_keys[:STRUCT_MISSES])}

        # Overwrites, then deletes (timed). The probe is attached for the whole phase pair.
        probe = FlushProbe(db)
        try:
            over = WriteStats()
            for n, (k, v) in enumerate(overwrites):
                timed_write(db, probe, over, "put", k, v, n)
            state_after_overwrites = dir_state(path)
            last = {}
            for k, v in overwrites:
                last[k] = v
            for k, v in last.items():
                assert db.get(k) == v, "overwrite did not return the newest value"
            dele = WriteStats()
            for n, k in enumerate(del_keys):
                timed_write(db, probe, dele, "delete", k, None, n)
        finally:
            probe.remove()
        deleted = set(del_keys)
        for k in deleted:
            assert db.get(k) is None, "deleted key still readable"
        state_after_deletes = dir_state(path)
        raw["put_overwrite"], raw["delete"] = over.all, dele.all
        table_list = [[t.count, os.path.getsize(t.path)] for t in db._sstables]
        index_tables = [[len(t._keys), table_index_bytes(t)] for t in db._sstables]
        final_memtable_entries = len(db._memtable)
        final_mem_bytes = db._mem_bytes
        db.close()
        del db
        bytes_final = dir_state(path)

        # Final reopen after overwrites and deletes: timed, then spot-checked against what was acknowledged.
        t0 = time.perf_counter_ns()
        db = KVStore(path, sync=False, memtable_limit_bytes=limit)
        reopen_final_ns = time.perf_counter_ns() - t0
        assert len(db._sstables) == len(table_list) and len(db._memtable) == final_memtable_entries
        for k in deleted:
            assert db.get(k) is None, "tombstone lost across reopen"
        for k, v in last.items():
            if k not in deleted:
                assert db.get(k) == v, "overwrite lost across reopen"
        for idx in hit_idx[:VERIFY_SAMPLE]:
            k = key_of(idx)
            want = None if k in deleted else last.get(k, expected_hit[idx])
            assert db.get(k) == want, "state differs after reopen"

        # Untimed supplementary passes, after every timed phase of this run.
        decomposition = decompose(db, hit_idx, miss_keys[0])
        sst_paths = [t.path for t in db._sstables]
        db.close()
        memory_of_index = index_memory(sst_paths)
    assert not os.path.exists(d), "temp database not cleaned up"

    summary = {"get_hit": full_summary(raw["get_hit"]), "get_miss": full_summary(raw["get_miss"]),
               "put_overwrite": full_summary(raw["put_overwrite"]), "delete": full_summary(raw["delete"]),
               "put_load": load_split["all"]}
    return {
        "summary": summary,
        "write_split": {"put_load": load_split, "put_overwrite": write_split(over), "delete": write_split(dele)},
        "_raw": raw,
        "_raw_write": {"put_load": (None, array("q", load_trigger_ns)),
                       "put_overwrite": (over.normal, over.trigger), "delete": (dele.normal, dele.trigger)},
        "put_load_max_ns": load_max_ns,
        "flushes": {"put_load": load_flushes, "put_overwrite": over.flushes, "delete": dele.flushes},
        "memtable_limit_bytes": limit,
        "index_interval": 16,
        "entries_per_table": entries_per_table,
        "open_recovery_ns": open_ns,
        "open_components": components,
        "open_recovery_final_ns": reopen_final_ns,
        "load_wall_seconds": load_wall_s,
        "records_in_wal_at_open": state_after_load["wal_bytes"] // PUT_RECORD_BYTES,
        "state": {"after_load": state_after_load, "after_overwrites": state_after_overwrites,
                  "after_deletes": state_after_deletes, "final_closed": bytes_final},
        "tables_final": table_list,  # [records, bytes] per SSTable, oldest first
        "index_tables": index_tables,  # [index entries, index block bytes] per SSTable, oldest first
        "final_memtable_entries": final_memtable_entries,
        "final_memtable_payload_bytes": final_mem_bytes,
        "read_work": read_work,
        "decomposition": decomposition,
        "index_memory": memory_of_index,
    }


# ---- 10,000-key head-to-head with the V0 baseline workload -----------------------------------
def baseline_run(wl, limit):
    """Mirror of baseline_run for KVStore: V0's baseline workload with a reference-dict check of every
    GET, flush boundaries observed on every write."""
    out, raw, ref = {}, {}, {}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "db")
        db = KVStore(path, sync=False, memtable_limit_bytes=limit)
        probe = FlushProbe(db)
        try:
            load = WriteStats()
            for n, (k, v) in enumerate(wl["load"]):
                timed_write(db, probe, load, "put", k, v, n)
            ref.update(wl["load"])
            out["state_after_load"] = dir_state(path)
            out["tables_after_load"] = [[t.count, len(t._keys)] for t in db._sstables]

            def get_all(keys):
                lats = []
                for k in keys:
                    ns, got = timed_call(db.get, k)
                    assert got == ref.get(k), "GET disagrees with the reference dict"
                    lats.append(ns)
                return lats

            raw["get_hit"] = get_all(wl["gets"][0::2])
            raw["get_miss"] = get_all(wl["gets"][1::2])
            mixed = {"get": [], "put": [], "delete": []}
            mixed_all = []
            mixed_writes = {"put": WriteStats(), "delete": WriteStats()}
            for n, (op, k, v) in enumerate(wl["mixed"]):
                if op == "get":
                    ns, got = timed_call(db.get, k)
                    assert got == ref.get(k), "mixed GET disagrees with the reference dict"
                else:
                    stats = mixed_writes[op]
                    timed_write(db, probe, stats, op, k, v, n)
                    ns = stats.all[-1]
                    if op == "put":
                        ref[k] = v
                    else:
                        ref.pop(k, None)
                mixed[op].append(ns)
                mixed_all.append(ns)
        finally:
            probe.remove()
        raw["mixed_all"] = mixed_all
        for op, lats in mixed.items():
            raw[f"mixed_{op}"] = lats
        out["state_after_mixed"] = dir_state(path)
        db.close()
        del db
        db = KVStore(path, sync=False, memtable_limit_bytes=limit)  # recovery must reproduce the acknowledged state
        touched = [k for _, k, _ in wl["mixed"]]
        for k in touched + [k for k, _ in wl["load"][::37]]:
            assert db.get(k) == ref.get(k), "state differs after reopen"
        db.close()

        db2 = KVStore(os.path.join(d, "synced"), sync=True, memtable_limit_bytes=limit)
        probe2 = FlushProbe(db2)
        try:
            synced = WriteStats()
            for n, (k, v) in enumerate(wl["sync_puts"]):
                timed_write(db2, probe2, synced, "put", k, v, n)
        finally:
            probe2.remove()
        db2.close()
    raw["put_load"] = [x for x in load.all]
    raw["put_sync"] = [x for x in synced.all]
    out["summary"] = {name: summarize(lats) for name, lats in raw.items() if lats}
    out["write_split"] = {"put_load": write_split(load), "mixed_put": write_split(mixed_writes["put"]),
                          "mixed_delete": write_split(mixed_writes["delete"]), "put_sync": write_split(synced)}
    out["flush_counts"] = {"put_load": len(load.flushes), "mixed_put": len(mixed_writes["put"].flushes),
                           "mixed_delete": len(mixed_writes["delete"].flushes), "put_sync": len(synced.flushes)}
    out["flushes_put_load"] = load.flushes
    return out


def baseline_entry(wl_args, runs, limit):
    wl = build_workload(SEED, *wl_args)
    res = []
    for i in range(runs):
        print(f"  baseline workload {wl_args} limit={limit} run {i + 1}/{runs} ...", flush=True)
        res.append(baseline_run(wl, limit))
    return {"workload": {"keyspace": wl_args[0], "n_get_ops": wl_args[1], "n_mixed_ops": wl_args[2],
                         "n_sync_puts": wl_args[3]},
            "memtable_limit_bytes": limit,
            "index_interval": 16,
            "runs_completed": runs,
            "aggregate_over_runs": aggregate(res),
            "write_split_aggregate": {p: split_aggregate([{"write_split": r["write_split"]} for r in res], p)
                                      for p in res[0]["write_split"]},
            "flush_counts_per_run": [r["flush_counts"] for r in res],
            "state_after_load": [r["state_after_load"] for r in res],
            "state_after_mixed": [r["state_after_mixed"] for r in res],
            "tables_after_load": [r["tables_after_load"] for r in res],
            "runs": [{"summary": r["summary"], "write_split": r["write_split"],
                      "flushes_put_load": r["flushes_put_load"]} for r in res]}


# ---- analysis --------------------------------------------------------------------------------
def v2_reference():
    return json.loads((RESULTS / "v2_benchmark.json").read_text())


def pct_change(new, old):
    return (new / old - 1) * 100


def percentile_nearest_rank(sorted_vals, q):
    return sorted_vals[max(0, math.ceil(q * len(sorted_vals)) - 1)]


def dist(vals):
    s = sorted(vals)
    return {"n": len(s), "mean": statistics.fmean(s), "median": statistics.median(s),
            "p95": percentile_nearest_rank(s, 0.95), "max": s[-1], "min": s[0]}


def read_work_summary(sc):
    """V3 work per GET by category, over the instrumented sample GETs of all runs at a scale. Row layout:
    [category, tables_consulted, tables_skipped_by_index, blocks_read, records_decoded, bytes_decoded]."""
    groups = {}
    for run in sc["runs"]:
        for kind in ("hit", "miss"):
            for row in run["read_work"][kind]:
                groups.setdefault(row[0], []).append(row)
                if kind == "hit":
                    groups.setdefault("all_hits", []).append(row)
    names = ("tables_consulted", "tables_skipped_by_index", "blocks_read", "records_decoded", "bytes_decoded")
    return {cat: {"gets": len(rows), **{name: dist([r[i + 1] for r in rows]) for i, name in enumerate(names)}}
            for cat, rows in groups.items()}


def v2_read_work_all_hits(sc2):
    """The same quantities for the V2 sample (rows [category, tables, records, bytes]) so V2 and V3 can be compared on
    the identical hit keys (the first STRUCT_HITS hit keys of every run are the same keys)."""
    rows = [r for run in sc2["runs"] for r in run["read_work"]["hit"]]
    misses = [r for run in sc2["runs"] for r in run["read_work"]["miss"]]
    return {"hit": {"gets": len(rows), "records": dist([r[2] for r in rows]), "bytes": dist([r[3] for r in rows]),
                    "tables": dist([r[1] for r in rows])},
            "miss": {"gets": len(misses), "records": dist([r[2] for r in misses]),
                     "bytes": dist([r[3] for r in misses]), "tables": dist([r[1] for r in misses])}}


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


def analyze(result):
    v2 = v2_reference()
    sc = [result["scales"][k] for k in sorted(result["scales"], key=int)
          if result["scales"][k]["runs_completed"] == result["scales"][k]["runs_planned"]]
    out = {"scales_used": [s["records"] for s in sc]}
    if len(sc) < 2:
        return out
    n = [s["records"] for s in sc]
    s2 = [v2["scales"][str(r)] for r in n]
    tables = [s["runs"][0]["state"]["after_load"]["sst_files"] for s in sc]
    assert tables == [e["runs"][0]["state"]["after_load"]["sst_files"] for e in s2], "table layout differs from V2"
    out["sstables_after_load"] = tables

    # ---- GET latency, V3 vs V2 ----
    for kind in ("get_hit", "get_miss"):
        for p in ("p50_us", "p95_us", "p99_us", "mean_us"):
            a = [s["pooled_all_runs"][kind][p] for s in sc]
            b = [e["pooled_all_runs"][kind][p] for e in s2]
            out[f"{kind}_{p}"] = {"v3_us": a, "v2_us": b, "v3_over_v2": [x / y for x, y in zip(a, b)],
                                  "v2_over_v3": [y / x for x, y in zip(a, b)],
                                  "change_percent": [pct_change(x, y) for x, y in zip(a, b)]}
        # Medians of per-run medians, to show how much the run medians differ (not a noise-floor claim).
        out[f"{kind}_p50_run_medians_us"] = {
            "v3": [[r["summary"][kind]["p50_us"] for r in s["runs"]] for s in sc],
            "v2": [[r["summary"][kind]["p50_us"] for r in e["runs"]] for e in s2]}
        spread = lambda m: (m["max"] - m["min"]) / m["median"]  # noqa: E731
        out[f"{kind}_p50_between_run_spread"] = {
            "v3": [spread(s["aggregate_over_runs"][kind]["p50_us"]) for s in sc],
            "v2": [spread(e["aggregate_over_runs"][kind]["p50_us"]) for e in s2]}
        withtab = [i for i, t in enumerate(tables) if t > 0]
        if len(withtab) >= 2:
            v3p50 = out[f"{kind}_p50_us"]["v3_us"]
            v2p50 = out[f"{kind}_p50_us"]["v2_us"]
            nn = [n[i] for i in withtab]
            tt = [tables[i] for i in withtab]
            out[f"{kind}_p50_v3_fit_vs_records"] = fit(nn, nn, [v3p50[i] for i in withtab])
            out[f"{kind}_p50_v2_fit_vs_records"] = fit(nn, nn, [v2p50[i] for i in withtab])
            out[f"{kind}_p50_v3_fit_vs_sstables"] = fit(tt, tt, [v3p50[i] for i in withtab])
            out[f"{kind}_p50_per_sstable_us"] = {"v3": [v3p50[i] / tables[i] for i in withtab],
                                                 "v2": [v2p50[i] / tables[i] for i in withtab],
                                                 "scales": nn}
    out["get_samples_pooled"] = {"hit": [s["pooled_all_runs"]["get_hit"]["ops"] for s in sc],
                                 "miss": [s["pooled_all_runs"]["get_miss"]["ops"] for s in sc]}

    # ---- structural read work (instrumented sample), V3 vs V2 ----
    out["read_work_v3"] = [read_work_summary(s) for s in sc]
    out["read_work_v2"] = [v2_read_work_all_hits(e) for e in s2]
    red = []
    for w3, w2 in zip(out["read_work_v3"], out["read_work_v2"]):
        row = {}
        for kind, key3 in (("hit", "all_hits"), ("miss", "miss")):
            if key3 not in w3 or w2[kind]["records"]["mean"] == 0:
                row[kind] = None
                continue
            r3, b3 = w3[key3]["records_decoded"]["mean"], w3[key3]["bytes_decoded"]["mean"]
            row[kind] = {"records_v2_over_v3": w2[kind]["records"]["mean"] / r3 if r3 else None,
                         "bytes_v2_over_v3": w2[kind]["bytes"]["mean"] / b3 if b3 else None,
                         "records_v2_mean": w2[kind]["records"]["mean"], "records_v3_mean": r3,
                         "bytes_v2_mean": w2[kind]["bytes"]["mean"], "bytes_v3_mean": b3,
                         "tables_v2_mean": w2[kind]["tables"]["mean"],
                         "tables_v3_mean": w3[key3]["tables_consulted"]["mean"],
                         "blocks_read_v3_mean": w3[key3]["blocks_read"]["mean"]}
        red.append(row)
    out["read_work_reduction"] = red

    # ---- per-table cost decomposition (untimed pass) ----
    dec = []
    for s in sc:
        runs_dec = [r["decomposition"] for r in s["runs"] if r["decomposition"]]
        if not runs_dec:
            dec.append(None)
            continue
        row = {}
        for kind in ("miss", "hit"):
            parts = [d[kind] for d in runs_dec if d.get(kind)]
            if parts:
                row[kind] = {"pairs_per_rep": parts[0]["pairs"], "reps": parts[0]["reps"], "runs": len(parts),
                             **{f"{c}_us_median": statistics.median(p[f"{c}_ns_median"] for p in parts) / 1e3
                                for c in ("locate", "open_close", "block_read", "lookup")}}
        dec.append(row)
    out["decomposition"] = dec

    # ---- writes ----
    out["put_load"] = {"v3": [write_row(s) for s in sc], "v2": [write_row(e) for e in s2]}
    for phase in ("put_overwrite", "delete"):
        rows = []
        for s, e in zip(sc, s2):
            p, q = s["pooled_all_runs"][phase], e["pooled_all_runs"][phase]
            rows.append({"records": s["records"],
                         "flushing_ops_total_v3": sum(r["write_split"][phase]["n_flush_triggering_ops"] for r in s["runs"]),
                         "flushing_ops_total_v2": sum(r["write_split"][phase]["n_flush_triggering_ops"] for r in e["runs"]),
                         "v3_p50_p95_p99_us": [p["all"][m] for m in ("p50_us", "p95_us", "p99_us")],
                         "v2_p50_p95_p99_us": [q["all"][m] for m in ("p50_us", "p95_us", "p99_us")],
                         "v3_over_v2_p50": p["all"]["p50_us"] / q["all"]["p50_us"]})
        out[phase] = rows

    # ---- recovery ----
    comp = lambda s, key: statistics.median(r["open_components"][key] for r in s["runs"]) / 1e9  # noqa: E731
    out["open_recovery"] = {
        "v3_seconds": [s["open_recovery_seconds_median"] for s in sc],
        "v2_seconds": [e["open_recovery_seconds_median"] for e in s2],
        "v3_over_v2": [s["open_recovery_seconds_median"] / e["open_recovery_seconds_median"] for s, e in zip(sc, s2)],
        "v3_per_run_seconds": [s["open_recovery_seconds"] for s in sc],
        "v2_per_run_seconds": [e["open_recovery_seconds"] for e in s2],
        "v3_between_run_spread": [(max(s["open_recovery_seconds"]) - min(s["open_recovery_seconds"]))
                                  / s["open_recovery_seconds_median"] for s in sc],
        "v3_final_reopen_seconds_median": [statistics.median(s["open_recovery_final_seconds"]) for s in sc],
        "v2_final_reopen_seconds_median": [statistics.median(e["open_recovery_final_seconds"]) for e in s2],
        "v3_components_median_seconds": {k: [comp(s, k) for s in sc] for k in
                                         ("list_dir_ns", "sstable_open_ns", "wal_replay_ns", "index_read_io_ns")},
        "v2_components_median_seconds": {k: [comp(e, k) for e in s2] for k in
                                         ("list_dir_ns", "sstable_footer_validation_ns", "wal_replay_ns")},
        "wal_records_replayed": [s["runs"][0]["records_in_wal_at_open"] for s in sc],
        "sstables_opened": tables,
        "index_bytes_read": [s["runs"][0]["open_components"]["index_bytes_read"] for s in sc],
        "index_entries_loaded": [s["runs"][0]["open_components"]["index_entries_loaded"] for s in sc],
        "footer_bytes_read": [s["runs"][0]["open_components"]["footer_bytes_read"] for s in sc],
    }

    # ---- memory ----
    mem = []
    for s, e in zip(sc, s2):
        d3_load = [p["load"]["private_delta"] for p in s["memory_probes"]]
        d3_open = [p["open"]["private_delta"] for p in s["memory_probes"]]
        d2_load = [p["load"]["private_delta"] for p in e["memory_probes"]]
        d2_open = [p["open"]["private_delta"] for p in e["memory_probes"]]
        retained = [r["index_memory"]["retained_bytes"] for r in s["runs"]]
        mem.append({"records": s["records"],
                    "v3_private_delta_after_load_mb": metric_range([x / 1e6 for x in d3_load]),
                    "v3_private_delta_after_open_mb": metric_range([x / 1e6 for x in d3_open]),
                    "v2_private_delta_after_load_mb": metric_range([x / 1e6 for x in d2_load]),
                    "v2_private_delta_after_open_mb": metric_range([x / 1e6 for x in d2_open]),
                    "v3_over_v2_load_delta": statistics.median(d3_load) / statistics.median(d2_load),
                    "v3_over_v2_open_delta": statistics.median(d3_open) / statistics.median(d2_open),
                    "v3_open_minus_v2_open_mb": (statistics.median(d3_open) - statistics.median(d2_open)) / 1e6,
                    "v3_peak_working_set_after_load_mb": max(p["load"]["peak_working_set_after"]
                                                             for p in s["memory_probes"]) / 1e6,
                    "v2_peak_working_set_after_load_mb": max(p["load"]["peak_working_set_after"]
                                                             for p in e["memory_probes"]) / 1e6,
                    "v3_memtable_entries_after_load": s["memory_probes"][0]["load"]["memtable_entries"],
                    "v3_tables_after_load": s["memory_probes"][0]["load"]["tables"],
                    "index_retained_bytes_tracemalloc": metric_range(retained)})
    out["memory"] = mem

    # ---- index overhead ----
    idx = []
    for s in sc:
        r0 = s["runs"][0]
        ibytes = sum(b for _, b in r0["index_tables"])
        ientries = sum(c for c, _ in r0["index_tables"])
        sst_bytes = sum(b for _, b in r0["tables_final"])
        retained = statistics.median(r["index_memory"]["retained_bytes"] for r in s["runs"])
        idx.append({"records": s["records"], "sstables": len(r0["tables_final"]), "index_entries": ientries,
                    "index_bytes": ibytes, "sstable_bytes": sst_bytes,
                    "index_bytes_share_of_sstable_bytes": ibytes / sst_bytes if sst_bytes else None,
                    "records_in_tables": sum(c for c, _ in r0["tables_final"]),
                    "index_bytes_per_record": ibytes / sum(c for c, _ in r0["tables_final"]) if ibytes else None,
                    "retained_heap_bytes_median": retained,
                    "retained_heap_over_index_bytes": retained / ibytes if ibytes else None,
                    "retained_heap_bytes_per_entry": retained / ientries if ientries else None})
    out["index"] = idx
    return out


def baseline_analysis(result):
    v2 = v2_reference()
    out = {}
    for tag in ("default_limit", "small_limit"):
        entry = result.get("baseline", {}).get(tag)
        if not entry:
            continue
        ref = v2["baseline"][tag]["aggregate_over_runs"]
        rows = {}
        for name, m in entry["aggregate_over_runs"].items():
            if name in ref:
                rows[name] = {p: {"v3_median": m[p]["median"], "v2_median": ref[name][p]["median"],
                                  "v3_over_v2": m[p]["median"] / ref[name][p]["median"]}
                              for p in ("p50_us", "p95_us", "p99_us")}
        out[tag] = rows
    return out


# ---- metadata / orchestration ------------------------------------------------------------------
def metadata(args):
    ram = ram_bytes()
    overrides = {k: getattr(args, k) for k in ("memtable_limit_bytes", "hits", "misses", "runs")
                 if getattr(args, k) is not None}
    return {
        "benchmark": "v3",
        "command": " ".join([Path(sys.executable).name] + sys.argv),
        "overrides": overrides or None,
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "ram_total_bytes": ram[0] if ram else None,
        "cache_condition": "WARM OS page cache. No cold-cache measurement is made or claimed.",
        "params": {
            "seed": SEED,
            "key_size_bytes": len(key_of(0)),
            "value_size_bytes": VALUE_SIZE,
            "record_bytes": RECORD_BYTES,
            "delete_record_bytes": DELETE_RECORD_BYTES,
            "sstable_footer_bytes_v3": FOOTER_V3_SIZE,
            "index_entry_bytes": INDEX_ENTRY_BYTES,
            "index_interval": DEFAULT_INDEX_INTERVAL,
            "store_sync": False,
            "memtable_limit_bytes": args.memtable_limit_bytes or DEFAULT_MEMTABLE_LIMIT_BYTES,
            "default_memtable_limit_bytes": DEFAULT_MEMTABLE_LIMIT_BYTES,
            "runs_per_scale": {str(r): c["runs"] for r, c in V0_SCALES.items()},
            "gets_hit_per_run": {str(r): c for r, c in GET_HITS.items()},
            "gets_miss_per_run": {str(r): c for r, c in GET_MISSES.items()},
            "instrumented_gets_per_run": {"hit": STRUCT_HITS, "miss": STRUCT_MISSES},
            "decomposition": {"miss_reps": DECOMP_REPS_MISS, "hit_reps": DECOMP_REPS_HIT,
                              "hit_pairs": DECOMP_HIT_PAIRS},
            "n_overwrites": N_OVERWRITES,
            "n_deletes": N_DELETES,
            "memory_probes_per_scale": MEMORY_PROBES,
            "baseline_runs": BASELINE_RUNS,
            "baseline_small_limit_bytes": SMALL_LIMIT_BYTES,
        },
        "timing_notes": "Only store calls are timed. Percentiles are nearest-rank (same code as V0/V1/V2). Flush "
        "boundaries are observed by wrapping the store instance's flush and lsm_store.sstable.write_sstable; no "
        "production code is changed. A flush-triggering operation is a put/delete that finds memtable bytes >= the "
        "limit and flushes before applying itself; its latency includes the whole flush. Raw per-operation latencies "
        "are not stored except for flush-triggering operations (one record per flush); percentiles of all, non-flush "
        "and flush-triggering writes are computed in-process. open_recovery is the first KVStore(path) after "
        "close() in the same process; open_components repeats the open steps with the module's own functions. "
        "Read-work counting, the per-table cost decomposition and the index-memory (tracemalloc) pass are untimed and "
        "run after every timed phase of the run they belong to (read-work counting runs between the timed GETs and "
        "the timed overwrites, as in V2).",
    }


def checkpoint(result, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(RESULTS / "v3_benchmark.json"))
    ap.add_argument("--scales", default=None, help="comma-separated record counts from the V0 scale ladder")
    ap.add_argument("--baseline", action="store_true", help="10,000-key head-to-head workloads")
    ap.add_argument("--analyze-only", action="store_true")
    ap.add_argument("--probe", choices=["load", "open"])
    ap.add_argument("--probe-records", type=int)
    ap.add_argument("--probe-path")
    ap.add_argument("--probe-limit", type=int)
    # Sanity/test overrides; recorded in the metadata and The final protocol uses none.
    ap.add_argument("--memtable-limit-bytes", type=int, default=None)
    ap.add_argument("--hits", type=int, default=None)
    ap.add_argument("--misses", type=int, default=None)
    ap.add_argument("--runs", type=int, default=None)
    args = ap.parse_args()
    out_path = Path(args.out)
    limit = args.memtable_limit_bytes or DEFAULT_MEMTABLE_LIMIT_BYTES

    if args.probe:
        print(json.dumps(memory_probe(args.probe, args.probe_records, args.probe_path, args.probe_limit)))
        return

    if args.analyze_only:
        result = json.loads(out_path.read_text())
        result["analysis"] = analyze(result)
        result["baseline_analysis"] = baseline_analysis(result)
        checkpoint(result, out_path)
        print(json.dumps({"analysis_keys": list(result["analysis"]), "scales": list(result["scales"])}))
        return

    result = {"metadata": metadata(args), "status": "in_progress", "scales": {}, "baseline": {}}

    if args.baseline:
        n_runs = args.runs or BASELINE_RUNS
        result["baseline"]["default_limit"] = baseline_entry((10_000, 200, 300, 300), n_runs, limit)
        checkpoint(result, out_path)
        result["baseline"]["small_limit"] = baseline_entry((10_000, 200, 300, 300), n_runs, SMALL_LIMIT_BYTES)
        result["status"] = "complete"
        checkpoint(result, out_path)
        print("wrote", out_path)
        return

    for records in [int(s) for s in (args.scales or ",".join(map(str, SCALES))).split(",")]:
        assert records in V0_SCALES, "scale must be on the V0 ladder"
        n_hits = args.hits or GET_HITS[records]
        n_misses = args.misses or GET_MISSES[records]
        n_runs = args.runs or V0_SCALES[records]["runs"]
        runs = []
        for i in range(n_runs):
            print(f"scale {records:>10,} V3 run {i + 1}/{n_runs} ...", flush=True)
            t0 = time.perf_counter()
            runs.append(v3_run(records, limit, n_hits, n_misses, V0_SCALES[records]["gets_per_kind"]))
            r = runs[-1]
            s = r["summary"]
            print(f"  done in {time.perf_counter() - t0:.0f}s  flushes={len(r['flushes']['put_load'])}  "
                  f"open={r['open_recovery_ns'] / 1e9:.3f}s  hit p50={s['get_hit']['p50_us']:.1f}us  "
                  f"miss p50={s['get_miss']['p50_us']:.1f}us  put max={r['put_load_max_ns'] / 1e6:.1f}ms", flush=True)
        memory = []
        for i in range(MEMORY_PROBES):
            print(f"scale {records:>10,} memory probe {i + 1}/{MEMORY_PROBES} ...", flush=True)
            memory.append(run_memory_probe(records, limit))
        entry = scale_entry(records, limit, n_hits, n_misses, runs, memory)
        entry["runs_planned"] = n_runs
        entry["index_interval"] = 16
        result["scales"][str(records)] = entry
        del runs
        checkpoint(result, out_path)
    result["status"] = "complete"
    result["analysis"] = analyze(result)
    checkpoint(result, out_path)
    print("wrote", out_path)


if __name__ == "__main__":
    main()
