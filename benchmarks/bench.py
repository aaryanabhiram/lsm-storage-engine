"""V4 benchmark: V3 plus a Bloom filter in every SSTable (10 bits per key, 7 hashes). Methodology: docs/benchmarks.md.

Usage:
    python benchmarks/bench.py --scales 10000 --out PATH       # scaling runs for the listed scales
    python benchmarks/bench.py --baseline --out PATH           # 10,000-key head-to-head workloads
    python benchmarks/bench.py --analyze-only --out FINAL.json

The protocol is V3's (py, which is V2's): same seed, keys, values, workloads, scale ladder, run counts, GET
sample counts, 4 MiB memtable limit, timing boundary (only the store call is timed, one perf_counter_ns pair per
operation) and percentile code. Only the store under test changes (KVStore with its defaults: sparse index interval
16, 10 Bloom bits per key, 7 hashes). The V0..V3 results (benchmarks/results/*.json) are read, never written.
All numbers are WARM OS-CACHE numbers.

No production code is changed. Flush boundaries are observed by wrapping the instance's `flush` method and the
module-level `lsm_store.sstable.write_sstable`. Read work is counted untimed by wrapping `SSTable.lookup`,
`SSTable.may_contain`, `SSTable.locate`, `SSTable._scan_block`, `sstable._open_table` and the record reader. After the
timed phases of a run (so that nothing timed is affected) untimed passes decompose the cost of one table lookup
(Bloom check, index search, file open, block read), validate the Bloom invariant on every table, probe the false-positive
rate with keys known to be absent, and measure the filters' retained memory.
"""


import argparse
import gc
import hashlib
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
import zlib
from array import array
from pathlib import Path

import lsm_store.sstable as sstable_module
from lsm_store import KVStore, bloom
from lsm_store.record import HEADER_SIZE
from lsm_store.sstable import FOOTER_V4_SIZE, NOT_FOUND, SSTable
from lsm_store.store import DEFAULT_MEMTABLE_LIMIT_BYTES
from lsm_store.store import DEFAULT_INDEX_INTERVAL
from lsm_store.store import DEFAULT_BLOOM_BITS_PER_KEY
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


INDEX_ENTRY_BYTES = 4 + 8 + len(key_of(0))  # key_len(4) + offset(8) + key(11) = 23
DECOMP_REPS_MISS = 20  # repetitions of the per-table cost decomposition over all tables (miss key)
DECOMP_REPS_HIT = 10  # repetitions over the sampled (table, hit key) pairs
DECOMP_HIT_PAIRS = 200


def index_entries(count, interval=DEFAULT_INDEX_INTERVAL):
    return -(-count // interval)


def index_bytes(count, interval=DEFAULT_INDEX_INTERVAL):
    """Bytes of the index block of a table of `count` records with 11-byte keys."""
    return index_entries(count, interval) * INDEX_ENTRY_BYTES


def pct_change(new, old):
    return (new / old - 1) * 100


def percentile_nearest_rank(sorted_vals, q):
    return sorted_vals[max(0, math.ceil(q * len(sorted_vals)) - 1)]


def dist(vals):
    s = sorted(vals)
    return {"n": len(s), "mean": statistics.fmean(s), "median": statistics.median(s),
            "p95": percentile_nearest_rank(s, 0.95), "max": s[-1], "min": s[0]}


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


def percentile_nearest_rank(sorted_vals, q):
    return sorted_vals[max(0, math.ceil(q * len(sorted_vals)) - 1)]


def dist(vals):
    s = sorted(vals)
    return {"n": len(s), "mean": statistics.fmean(s), "median": statistics.median(s),
            "p95": percentile_nearest_rank(s, 0.95), "max": s[-1], "min": s[0]}


def v3_read_work_summary(sc):
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

# ---- protocol: identical to V3's, which reuses V2's and V0's ladder ------------------------------------------------
SCALES = list(V0_SCALES)
RESULTS = ROOT / "benchmarks/results"
BITS_PER_KEY = DEFAULT_BLOOM_BITS_PER_KEY  # 10
NUM_HASHES = bloom.num_hashes(BITS_PER_KEY)  # 7
STRUCT_MISSES_V4 = 50  # instrumented miss GETs per run (V3 instrumented 3: a V3 miss decodes thousands of records)
FPR_MIN_KEYS = 20_000  # absent probe keys per table, raised so that every scale has at least FPR_MIN_CHECKS checks
FPR_MIN_CHECKS = 1_000_000
FP_PAIRS = 200  # (table, absent key) pairs whose Bloom answer is "maybe", used for the false-positive cost
HASH_REPS = 20
BUILD_REPS = 3
WORK_FIELDS = ("tables_consulted", "bloom_checks", "bloom_negative", "bloom_maybe", "false_positives",
               "fp_without_file_access", "index_lookups", "index_rule_outs", "tables_opened", "blocks_scanned",
               "records_decoded", "bytes_decoded")


def probe_key(i):
    """An 11-byte key that is in no table: stored keys are b"key%08d"; these sort above every one of them, so a
    "maybe" answer on one leads to the table's last block (the same position the V3/V4 miss workload reads)."""
    return b"zzz%08d" % i


def fpr_keys_per_table(tables):
    return max(FPR_MIN_KEYS, -(-FPR_MIN_CHECKS // max(1, tables)))


def table_filter_bytes(count):
    return bloom.filter_bytes(count, BITS_PER_KEY)


def table_file_bytes(count, interval=DEFAULT_INDEX_INTERVAL):
    """Bytes of a V4 table of `count` 124-byte PUT records with 11-byte keys."""
    return count * PUT_RECORD_BYTES + index_bytes(count, interval) + table_filter_bytes(count) + FOOTER_V4_SIZE


def wilson(successes, n, z=1.96):
    """95% Wilson interval for a binomial proportion. Treats the checks as independent trials, which they are only
    approximately (the same filter answers many probes), so it is a guide to precision, not an exact bound."""
    if n == 0:
        return [None, None]
    p = successes / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [(c - h) / d, (c + h) / d]


# ---- flush instrumentation (no production code changed) ----------------------------------------
class FlushProbe:
    """Observes the flushes of one KVStore instance by wrapping `db.flush` and `lsm_store.sstable.write_sstable`
    (called by `KVStore._write_table` through the module). Records the index interval and Bloom bits per key each
    flush's table was written with. `last` is set while an operation flushes and cleared by the caller before each
    operation."""

    def __init__(self, db):
        self.db = db
        self.last = None
        self._write_ns = 0
        self._kwargs = {}
        self._real_write = sstable_module.write_sstable

        def write_wrapper(path, items, **kwargs):
            t0 = time.perf_counter_ns()
            self._kwargs = kwargs
            try:
                return self._real_write(path, items, **kwargs)
            finally:
                self._write_ns = time.perf_counter_ns() - t0

        real_flush = db.flush

        def flush_wrapper():
            self._write_ns = 0
            self._kwargs = {}
            entries, mem_bytes = len(db._memtable), db._mem_bytes
            t0 = time.perf_counter_ns()
            try:
                real_flush()
            finally:
                self.last = {"flush_ns": time.perf_counter_ns() - t0, "write_sstable_ns": self._write_ns,
                             "memtable_entries": entries, "memtable_bytes": mem_bytes,
                             "index_interval": self._kwargs.get("index_interval"),
                             "bloom_bits_per_key": self._kwargs.get("bloom_bits_per_key")}

        sstable_module.write_sstable = write_wrapper
        db.flush = flush_wrapper

    def remove(self):
        sstable_module.write_sstable = self._real_write
        self.db.__dict__.pop("flush", None)


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


# ---- untimed passes after the timed phases of a run -------------------------------------------------------------
def _per_item_ns(total_ns, items):
    return total_ns / items


def measure_pairs(pairs, reps, file_parts):
    """Median over repetitions of the cost per (table, key) pair of: `bloom` (SSTable.may_contain), `lookup` (the full
    SSTable.lookup) and, if `file_parts`, `locate` (index search), `open_close` (opening and closing the table file)
    and `block_read` (open + seek + read of the located block's bytes, no decoding). Batched timing: one clock pair per
    loop over all pairs, so clock overhead is amortized."""
    names = ["bloom", "lookup"] + (["locate", "open_close", "block_read"] if file_parts else [])
    spans = [(t.path, *t.locate(k)[1:]) for t, k in pairs] if file_parts else []
    samples = {n: [] for n in names}
    n = len(pairs)
    for _ in range(reps):
        t0 = time.perf_counter_ns()
        for t, k in pairs:
            t.may_contain(k)
        t1 = time.perf_counter_ns()
        for t, k in pairs:
            t.lookup(k)
        t2 = time.perf_counter_ns()
        times = [t1 - t0, t2 - t1]
        if file_parts:
            for t, k in pairs:
                t.locate(k)
            t3 = time.perf_counter_ns()
            for path, _s, _e in spans:
                open(path, "rb").close()
            t4 = time.perf_counter_ns()
            for path, start, end in spans:
                with open(path, "rb") as f:
                    f.seek(start)
                    f.read(end - start)
            t5 = time.perf_counter_ns()
            times += [t3 - t2, t4 - t3, t5 - t4]
        for name, ns in zip(names, times):
            samples[name].append(_per_item_ns(ns, n))
    return {"pairs": n, "reps": reps, **{f"{name}_ns_median": statistics.median(v) for name, v in samples.items()},
            **{f"{name}_ns_min_max": [min(v), max(v)] for name, v in samples.items()}}


def hash_cost(keys):
    """Cost of the one BLAKE2b digest + unpack that starts every Bloom query (the first line of bloom._positions),
    median ns per key over HASH_REPS batched repetitions."""
    samples = []
    for _ in range(HASH_REPS):
        t0 = time.perf_counter_ns()
        for k in keys:
            bloom._HALVES.unpack(hashlib.blake2b(k, digest_size=16).digest())
        samples.append((time.perf_counter_ns() - t0) / len(keys))
    return {"keys": len(keys), "reps": HASH_REPS, "hash_ns_median": statistics.median(samples),
            "hash_ns_min_max": [min(samples), max(samples)]}


def build_cost(table):
    """Cost of building one table's filter (what write_sstable adds per flush): bloom.add over every key of the table,
    median over BUILD_REPS repetitions."""
    keys = [k for k, _ in table.items()]
    samples = []
    for _ in range(BUILD_REPS):
        bits = bytearray(table.filter_len)
        t0 = time.perf_counter_ns()
        for k in keys:
            bloom.add(bits, k, table.num_hashes)
        samples.append(time.perf_counter_ns() - t0)
        assert bytes(bits) == table._bloom, "rebuilt filter differs from the stored one"
    return {"keys": len(keys), "reps": BUILD_REPS, "build_ns_median": statistics.median(samples),
            "build_ns_per_key_median": statistics.median(samples) / len(keys)}


def decompose(db, n_load, hit_idx, miss_key):
    """Per-table lookup cost over the `n_load` tables written by the load (each holds the same number of consecutive
    keys, so key i lives in table i // entries_per_table). `miss_no`: the miss key against every table whose filter
    says "no" (the Bloom check is the whole lookup). `hit`: sampled hit keys against the table that holds them
    ("maybe", then index, then block). Also the cost of the hash alone and of building one filter. None if no tables."""
    tables = list(db._sstables[:n_load])
    if not tables:
        return None
    e = tables[0].count
    no_pairs = [(t, miss_key) for t in tables if not t.may_contain(miss_key)]
    hit_pairs = [(tables[i // e], key_of(i)) for i in hit_idx[:DECOMP_HIT_PAIRS] if i // e < n_load]
    assert all(t.may_contain(k) for t, k in hit_pairs), "a stored key was rejected by its table's filter"
    return {"tables": len(tables), "miss_no": measure_pairs(no_pairs, DECOMP_REPS_MISS, False) if no_pairs else None,
            "hit": measure_pairs(hit_pairs, DECOMP_REPS_HIT, True) if hit_pairs else None,
            "miss_no_share_of_tables": len(no_pairs) / len(tables),
            "hash": hash_cost([key_of(i) for i in range(1000)]), "build": build_cost(tables[0])}


def validate_blooms(db):
    """The production Bloom invariant at scale. Flushes the memtable (so tombstones written by the delete phase are in
    a table), then scans every table in full: `SSTable.items()` verifies every record checksum, the key order and that
    the filter rebuilt from the keys equals the stored filter byte for byte; every key read, tombstones included,
    must also get "maybe" from the table's own filter. Nothing here is timed."""
    db.flush()
    keys = tombstones = 0
    for t in db._sstables:
        for key, value in t.items():
            assert t.may_contain(key), f"filter of {t.path} rejected a key it holds"
            keys += 1
            tombstones += value is None
    assert tombstones > 0, "no tombstone reached a table, so the tombstone invariant was not exercised"
    return {"tables": len(db._sstables), "keys_checked": keys, "tombstone_keys_checked": tombstones,
            "false_negatives": 0, "stored_filters_equal_rebuilt": True}


def fpr_probe(db):
    """Measured false-positive rate: every table's filter queried (`SSTable.may_contain`, no file access) with
    absent keys. `maybe` counts are false positives by construction: the probe keys are in no table. Pooled over all
    (table, key) checks; the expected rate comes from bloom.expected_false_positive_rate for each table's own size."""
    tables = list(db._sstables)
    n_keys = fpr_keys_per_table(len(tables))
    keys = [probe_key(i) for i in range(n_keys)]
    per_table = []
    fp_total = 0
    t0 = time.perf_counter()
    for t in tables:
        mc = t.may_contain
        fps = sum(1 for k in keys if mc(k))
        per_table.append(fps)
        fp_total += fps
    checks = n_keys * len(tables)
    expected = statistics.fmean(bloom.expected_false_positive_rate(t.count, t.filter_len, t.num_hashes) for t in tables)
    rates = [x / n_keys for x in per_table]
    return {"tables": len(tables), "keys_per_table": n_keys, "checks": checks, "false_positives": fp_total,
            "measured_fpr": fp_total / checks, "wilson95": wilson(fp_total, checks), "expected_fpr": expected,
            "per_table_fpr_min_median_max": [min(rates), statistics.median(rates), max(rates)],
            "seconds": time.perf_counter() - t0}


def find_fp_pairs(tables):
    """Up to FP_PAIRS (table, absent key) pairs whose filter says "maybe", for costing a false positive."""
    per = max(2, -(-FP_PAIRS // max(1, len(tables))))
    pairs = []
    for t in tables:
        got = 0
        for i in range(10_000):
            k = probe_key(i)
            if t.may_contain(k):
                pairs.append((t, k))
                got += 1
                if got == per:
                    break
        if len(pairs) >= FP_PAIRS:
            break
    return pairs[:FP_PAIRS]


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


# ---- memory probes ---------------------------------------------------------------------------
def memory_probe(mode, records, path, limit):
    """Run inside a fresh process. 'load': build a V4 database of `records` keys; 'open': recover an existing one."""
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
           "filter_bytes": sum(t.filter_len for t in db._sstables),
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
                   "tables": p["tables"], "index_entries": p["index_entries"], "filter_bytes": p["filter_bytes"],
                   "memtable_entries": p["memtable_entries"],
                   "memtable_payload_bytes": p["memtable_payload_bytes"], "persistent": p["persistent"]}
            for mode, p in out.items()}


# ---- V4 scaling run --------------------------------------------------------------------------
def open_components(path):
    """Re-run the steps of KVStore.__init__ (inherited from V2) with the module's own functions to split the open
    time. Read-only. `io_reread_ns` re-reads exactly the bytes SSTable() reads (60-byte footer, index block, filter
    block) without parsing or validating them; `filter_crc_ns` is zlib.crc32 over the filter blocks alone (the check
    SSTable() makes on them)."""
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
    spans = [(t.path, os.path.getsize(t.path), t.data_len, t.index_len, t.filter_len) for t in tables]
    t4 = time.perf_counter_ns()
    blocks = []
    for p, size, data_len, ilen, flen in spans:
        with open(p, "rb") as f:
            f.seek(size - FOOTER_V4_SIZE)
            f.read(FOOTER_V4_SIZE)
            f.seek(data_len)
            f.read(ilen)
            blocks.append(f.read(flen))
    t5 = time.perf_counter_ns()
    for b in blocks:
        zlib.crc32(b)
    t6 = time.perf_counter_ns()
    return {"list_dir_ns": t1 - t0, "sstable_open_ns": t2 - t1, "wal_replay_ns": t3 - t2,
            "io_reread_ns": t5 - t4, "filter_crc_ns": t6 - t5, "tables": len(tables), "wal_segments": len(wal),
            "memtable_entries_replayed": replayed, "index_bytes_read": sum(s[3] for s in spans),
            "index_entries_loaded": sum(len(t._keys) for t in tables),
            "filter_bytes_read": sum(s[4] for s in spans), "footer_bytes_read": len(tables) * FOOTER_V4_SIZE}


def v4_run(records, limit, n_hits, n_misses, v0_gets, probe_fpr):
    value_rng, hit_idx, overwrites, miss_keys, del_keys = workload(records, n_hits, n_misses, v0_gets)
    hit_keys = [key_of(i) for i in hit_idx]

    check_headroom()
    hit_wanted = set(hit_idx)
    expected_hit = {}
    raw = {}
    entries_per_table = -(-limit // ENTRY_PAYLOAD)  # ceil: flush fires on the write after the memtable reaches limit
    table_index_b = index_bytes(entries_per_table)
    filter_b = table_filter_bytes(entries_per_table)
    table_bytes = table_file_bytes(entries_per_table)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "db")
        db = KVStore(path, sync=False, memtable_limit_bytes=limit)
        assert db._index_interval == 16 and db._bloom_bits_per_key == BITS_PER_KEY == 10 and NUM_HASHES == 7
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
        assert all(f["table_records"] == entries_per_table and f["index_interval"] == 16
                   and f["bloom_bits_per_key"] == BITS_PER_KEY for f in load.flushes)
        assert all(f["table_bytes"] == table_bytes for f in load.flushes)
        assert all(t.interval == 16 and len(t._keys) == index_entries(entries_per_table)
                   and t._bloom is not None and t.num_hashes == NUM_HASHES and t.filter_len == filter_b
                   and t.index_len == table_index_b for t in db._sstables), "a table lacks the expected Bloom filter"
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
                     "miss": measure_read_work(db, miss_keys[:min(n_misses, STRUCT_MISSES_V4)])}

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
        index_tables = [[len(t._keys), t.index_len] for t in db._sstables]
        filter_tables = [[t.num_hashes, t.filter_len] for t in db._sstables]
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
        decomposition = decompose(db, n_flushes_load, hit_idx, miss_keys[0])
        sst_paths = [t.path for t in db._sstables]
        validation = validate_blooms(db)  # flushes the memtable, then scans every table
        fpr = fpr_probe(db) if probe_fpr else None
        fp_cost = None
        fp_pairs = find_fp_pairs(db._sstables)
        if fp_pairs:
            fp_cost = measure_pairs(fp_pairs, DECOMP_REPS_HIT, True)
        db.close()
        memory_of_tables = table_memory(sst_paths)
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
        "bloom_bits_per_key": BITS_PER_KEY,
        "bloom_num_hashes": NUM_HASHES,
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
        "filter_tables": filter_tables,  # [num_hashes, filter block bytes] per SSTable, oldest first
        "final_memtable_entries": final_memtable_entries,
        "final_memtable_payload_bytes": final_mem_bytes,
        "read_work": read_work,
        "decomposition": decomposition,
        "false_positive_cost": fp_cost,
        "bloom_validation": validation,
        "fpr_probe": fpr,
        "table_memory": memory_of_tables,
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
            out["tables_after_load"] = [[t.count, len(t._keys), t.filter_len] for t in db._sstables]

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
            "bloom_bits_per_key": BITS_PER_KEY,
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
def v3_reference():
    return json.loads((RESULTS / "v3_benchmark.json").read_text())


def read_work_summary(sc):
    """V4 work per GET by category over the instrumented GETs of all runs at a scale: per field a distribution, plus
    the Bloom totals. Row layout: [category] + WORK_FIELDS."""
    groups = {}
    for run in sc["runs"]:
        for kind in ("hit", "miss"):
            for row in run["read_work"][kind]:
                groups.setdefault(row[0], []).append(row)
                if kind == "hit":
                    groups.setdefault("all_hits", []).append(row)
    out = {}
    for cat, rows in groups.items():
        entry = {"gets": len(rows), **{name: dist([r[i + 1] for r in rows]) for i, name in enumerate(WORK_FIELDS)}}
        totals = {name: sum(r[i + 1] for r in rows) for i, name in enumerate(WORK_FIELDS)}
        # A GET that was answered by a table has exactly one table that holds the key: its "maybe" is a true positive.
        true_positive_checks = sum(1 for r in rows if r[0] != "miss" and r[0] != "memtable_hit")
        absent_checks = totals["bloom_checks"] - true_positive_checks
        entry["totals"] = totals
        entry["true_positive_checks"] = true_positive_checks
        entry["absent_key_checks"] = absent_checks
        entry["skip_rate"] = totals["bloom_negative"] / totals["bloom_checks"] if totals["bloom_checks"] else None
        entry["false_positive_rate_on_absent_checks"] = (totals["false_positives"] / absent_checks
                                                         if absent_checks else None)
        out[cat] = entry
    return out


def analyze(result):
    v3 = v3_reference()
    sc = [result["scales"][k] for k in sorted(result["scales"], key=int)
          if result["scales"][k]["runs_completed"] == result["scales"][k]["runs_planned"]]
    out = {"scales_used": [s["records"] for s in sc]}
    if len(sc) < 2:
        return out
    n = [s["records"] for s in sc]
    s3 = [v3["scales"][str(r)] for r in n]
    tables = [s["runs"][0]["state"]["after_load"]["sst_files"] for s in sc]
    assert tables == [e["runs"][0]["state"]["after_load"]["sst_files"] for e in s3], "table layout differs from V3"
    out["sstables_after_load"] = tables

    # ---- GET latency, V4 vs V3 ----
    for kind in ("get_hit", "get_miss"):
        for p in ("p50_us", "p95_us", "p99_us", "mean_us"):
            a = [s["pooled_all_runs"][kind][p] for s in sc]
            b = [e["pooled_all_runs"][kind][p] for e in s3]
            out[f"{kind}_{p}"] = {"v4_us": a, "v3_us": b, "v4_over_v3": [x / y for x, y in zip(a, b)],
                                  "v3_over_v4": [y / x for x, y in zip(a, b)],
                                  "change_percent": [pct_change(x, y) for x, y in zip(a, b)]}
        out[f"{kind}_p50_run_medians_us"] = {
            "v4": [[r["summary"][kind]["p50_us"] for r in s["runs"]] for s in sc],
            "v3": [[r["summary"][kind]["p50_us"] for r in e["runs"]] for e in s3]}
        spread = lambda m: (m["max"] - m["min"]) / m["median"]  # noqa: E731
        out[f"{kind}_p50_between_run_spread"] = {
            "v4": [spread(s["aggregate_over_runs"][kind]["p50_us"]) for s in sc],
            "v3": [spread(e["aggregate_over_runs"][kind]["p50_us"]) for e in s3]}
        withtab = [i for i, t in enumerate(tables) if t > 0]
        if len(withtab) >= 2:
            v4p50 = out[f"{kind}_p50_us"]["v4_us"]
            v3p50 = out[f"{kind}_p50_us"]["v3_us"]
            nn = [n[i] for i in withtab]
            tt = [tables[i] for i in withtab]
            out[f"{kind}_p50_v4_fit_vs_records"] = fit(nn, nn, [v4p50[i] for i in withtab])
            out[f"{kind}_p50_v3_fit_vs_records"] = fit(nn, nn, [v3p50[i] for i in withtab])
            out[f"{kind}_p50_v4_fit_vs_sstables"] = fit(tt, tt, [v4p50[i] for i in withtab])
            out[f"{kind}_p50_v3_fit_vs_sstables"] = fit(tt, tt, [v3p50[i] for i in withtab])
            out[f"{kind}_p50_per_sstable_us"] = {"v4": [v4p50[i] / tables[i] for i in withtab],
                                                 "v3": [v3p50[i] / tables[i] for i in withtab],
                                                 "scales": nn}
    out["get_samples_pooled"] = {"hit": [s["pooled_all_runs"]["get_hit"]["ops"] for s in sc],
                                 "miss": [s["pooled_all_runs"]["get_miss"]["ops"] for s in sc]}

    # ---- structural read work (instrumented sample), V4 vs V3 ----
    out["read_work_v4"] = [read_work_summary(s) for s in sc]
    out["read_work_v3"] = [v3_read_work_summary(e) for e in s3]
    cmp_rows = []
    for w4, w3 in zip(out["read_work_v4"], out["read_work_v3"]):
        row = {}
        for kind, key in (("hit", "all_hits"), ("miss", "miss")):
            if key not in w4 or key not in w3:
                row[kind] = None
                continue
            a, b = w4[key], w3[key]
            row[kind] = {
                "tables_consulted_v4": a["tables_consulted"]["mean"], "tables_consulted_v3": b["tables_consulted"]["mean"],
                "tables_opened_v4": a["tables_opened"]["mean"], "tables_opened_v3": b["blocks_read"]["mean"],
                "index_lookups_v4": a["index_lookups"]["mean"],
                "index_rule_outs_v4": a["index_rule_outs"]["mean"],
                "index_rule_outs_v3": b["tables_skipped_by_index"]["mean"],
                "records_v4": a["records_decoded"]["mean"], "records_v3": b["records_decoded"]["mean"],
                "bytes_v4": a["bytes_decoded"]["mean"], "bytes_v3": b["bytes_decoded"]["mean"],
                "bloom_checks_v4": a["bloom_checks"]["mean"], "bloom_negative_v4": a["bloom_negative"]["mean"],
                "bloom_maybe_v4": a["bloom_maybe"]["mean"], "false_positives_v4": a["false_positives"]["mean"],
                "fp_without_file_access_v4": a["fp_without_file_access"]["mean"],
                "skip_rate": a["skip_rate"], "fp_rate_on_absent_checks": a["false_positive_rate_on_absent_checks"],
                "tables_opened_v3_over_v4": (b["blocks_read"]["mean"] / a["tables_opened"]["mean"]
                                             if a["tables_opened"]["mean"] else None),
                "records_v3_over_v4": b["records_decoded"]["mean"] / a["records_decoded"]["mean"]
                if a["records_decoded"]["mean"] else None}
        cmp_rows.append(row)
    out["read_work_comparison"] = cmp_rows

    # ---- per-table cost decomposition (untimed pass) ----
    dec = []
    for s in sc:
        runs_dec = [r["decomposition"] for r in s["runs"] if r["decomposition"]]
        if not runs_dec:
            dec.append(None)
            continue
        row = {"tables": runs_dec[0]["tables"], "miss_no_share_of_tables": runs_dec[0]["miss_no_share_of_tables"]}
        if all(d["miss_no"] for d in runs_dec):
            row["miss_no"] = {"pairs_per_rep": runs_dec[0]["miss_no"]["pairs"], "runs": len(runs_dec),
                              **{f"{c}_us_median": statistics.median(d["miss_no"][f"{c}_ns_median"]
                                                                     for d in runs_dec) / 1e3
                                 for c in ("bloom", "lookup")}}
        parts = [d["hit"] for d in runs_dec if d.get("hit")]
        if parts:
            row["hit"] = {"pairs_per_rep": parts[0]["pairs"], "runs": len(parts),
                          **{f"{c}_us_median": statistics.median(p[f"{c}_ns_median"] for p in parts) / 1e3
                             for c in ("bloom", "locate", "open_close", "block_read", "lookup")}}
        fps = [r["false_positive_cost"] for r in s["runs"] if r.get("false_positive_cost")]
        if fps:
            row["false_positive"] = {"pairs_per_rep": fps[0]["pairs"], "runs": len(fps),
                                     **{f"{c}_us_median": statistics.median(p[f"{c}_ns_median"] for p in fps) / 1e3
                                        for c in ("bloom", "locate", "open_close", "block_read", "lookup")}}
        row["hash_us_median"] = statistics.median(d["hash"]["hash_ns_median"] for d in runs_dec) / 1e3
        row["build_ms_median"] = statistics.median(d["build"]["build_ns_median"] for d in runs_dec) / 1e6
        row["build_us_per_key_median"] = statistics.median(d["build"]["build_ns_per_key_median"]
                                                           for d in runs_dec) / 1e3
        dec.append(row)
    out["decomposition"] = dec

    # ---- writes ----
    out["put_load"] = {"v4": [write_row(s) for s in sc], "v3": [write_row(e) for e in s3]}
    for phase in ("put_overwrite", "delete"):
        rows = []
        for s, e in zip(sc, s3):
            p, q = s["pooled_all_runs"][phase], e["pooled_all_runs"][phase]
            rows.append({"records": s["records"],
                         "flushing_ops_total_v4": sum(r["write_split"][phase]["n_flush_triggering_ops"] for r in s["runs"]),
                         "flushing_ops_total_v3": sum(r["write_split"][phase]["n_flush_triggering_ops"] for r in e["runs"]),
                         "v4_p50_p95_p99_us": [p["all"][m] for m in ("p50_us", "p95_us", "p99_us")],
                         "v3_p50_p95_p99_us": [q["all"][m] for m in ("p50_us", "p95_us", "p99_us")],
                         "v4_over_v3_p50": p["all"]["p50_us"] / q["all"]["p50_us"]})
        out[phase] = rows

    # ---- recovery ----
    comp = lambda s, key: statistics.median(r["open_components"][key] for r in s["runs"]) / 1e9  # noqa: E731
    out["open_recovery"] = {
        "v4_seconds": [s["open_recovery_seconds_median"] for s in sc],
        "v3_seconds": [e["open_recovery_seconds_median"] for e in s3],
        "v4_over_v3": [s["open_recovery_seconds_median"] / e["open_recovery_seconds_median"] for s, e in zip(sc, s3)],
        "v4_per_run_seconds": [s["open_recovery_seconds"] for s in sc],
        "v3_per_run_seconds": [e["open_recovery_seconds"] for e in s3],
        "v4_between_run_spread": [(max(s["open_recovery_seconds"]) - min(s["open_recovery_seconds"]))
                                  / s["open_recovery_seconds_median"] for s in sc],
        "v4_final_reopen_seconds_median": [statistics.median(s["open_recovery_final_seconds"]) for s in sc],
        "v3_final_reopen_seconds_median": [statistics.median(e["open_recovery_final_seconds"]) for e in s3],
        "v4_components_median_seconds": {k: [comp(s, k) for s in sc] for k in
                                         ("list_dir_ns", "sstable_open_ns", "wal_replay_ns", "io_reread_ns",
                                          "filter_crc_ns")},
        "v3_components_median_seconds": {k: [comp(e, k) for e in s3] for k in
                                         ("list_dir_ns", "sstable_open_ns", "wal_replay_ns", "index_read_io_ns")},
        "wal_records_replayed": [s["runs"][0]["records_in_wal_at_open"] for s in sc],
        "sstables_opened": tables,
        "index_bytes_read": [s["runs"][0]["open_components"]["index_bytes_read"] for s in sc],
        "index_entries_loaded": [s["runs"][0]["open_components"]["index_entries_loaded"] for s in sc],
        "filter_bytes_read": [s["runs"][0]["open_components"]["filter_bytes_read"] for s in sc],
        "footer_bytes_read": [s["runs"][0]["open_components"]["footer_bytes_read"] for s in sc],
    }

    # ---- memory ----
    mem = []
    for s, e in zip(sc, s3):
        d4_load = [p["load"]["private_delta"] for p in s["memory_probes"]]
        d4_open = [p["open"]["private_delta"] for p in s["memory_probes"]]
        d3_load = [p["load"]["private_delta"] for p in e["memory_probes"]]
        d3_open = [p["open"]["private_delta"] for p in e["memory_probes"]]
        retained = [r["table_memory"]["retained_bytes"] for r in s["runs"]]
        mem.append({"records": s["records"],
                    "v4_private_delta_after_load_mb": metric_range([x / 1e6 for x in d4_load]),
                    "v4_private_delta_after_open_mb": metric_range([x / 1e6 for x in d4_open]),
                    "v3_private_delta_after_load_mb": metric_range([x / 1e6 for x in d3_load]),
                    "v3_private_delta_after_open_mb": metric_range([x / 1e6 for x in d3_open]),
                    "v4_over_v3_load_delta": statistics.median(d4_load) / statistics.median(d3_load),
                    "v4_over_v3_open_delta": statistics.median(d4_open) / statistics.median(d3_open),
                    "v4_open_minus_v3_open_mb": (statistics.median(d4_open) - statistics.median(d3_open)) / 1e6,
                    "v4_load_minus_v3_load_mb": (statistics.median(d4_load) - statistics.median(d3_load)) / 1e6,
                    "v4_peak_working_set_after_load_mb": max(p["load"]["peak_working_set_after"]
                                                             for p in s["memory_probes"]) / 1e6,
                    "v3_peak_working_set_after_load_mb": max(p["load"]["peak_working_set_after"]
                                                             for p in e["memory_probes"]) / 1e6,
                    "v4_tables_after_load": s["memory_probes"][0]["load"]["tables"],
                    "v4_filter_bytes_after_load": s["memory_probes"][0]["load"]["filter_bytes"],
                    "table_heap_retained_bytes": metric_range(retained)})
    out["memory"] = mem

    # ---- index and filter overhead ----
    idx = []
    for s, e in zip(sc, s3):
        r0 = s["runs"][0]
        ibytes = sum(b for _, b in r0["index_tables"])
        fbytes = sum(b for _, b in r0["filter_tables"])
        ientries = sum(c for c, _ in r0["index_tables"])
        sst_bytes = sum(b for _, b in r0["tables_final"])
        records_in_tables = sum(c for c, _ in r0["tables_final"])
        tm = [r["table_memory"] for r in s["runs"]]
        retained = statistics.median(m["retained_bytes"] for m in tm)
        bloom_obj = statistics.median(m["bloom_bytes_objects"] for m in tm)
        v3_ret = statistics.median(r["index_memory"]["retained_bytes"] for r in e["runs"])
        idx.append({"records": s["records"], "sstables": len(r0["tables_final"]), "index_entries": ientries,
                    "index_bytes": ibytes, "filter_bytes": fbytes, "sstable_bytes": sst_bytes,
                    "filter_share_of_sstable_bytes": fbytes / sst_bytes if sst_bytes else None,
                    "index_share_of_sstable_bytes": ibytes / sst_bytes if sst_bytes else None,
                    "records_in_tables": records_in_tables,
                    "filter_bits_per_record": fbytes * 8 / records_in_tables if fbytes else None,
                    "filter_bytes_per_record": fbytes / records_in_tables if fbytes else None,
                    "filter_bytes_per_table": r0["filter_tables"][0][1] if r0["filter_tables"] else None,
                    "num_hashes": r0["filter_tables"][0][0] if r0["filter_tables"] else None,
                    "heap_retained_bytes_median": retained, "heap_bloom_objects_bytes_median": bloom_obj,
                    "heap_retained_minus_bloom_median": retained - bloom_obj,
                    "v3_index_heap_retained_bytes_median": v3_ret,
                    "heap_retained_over_v3_index_heap": retained / v3_ret if v3_ret else None,
                    "bloom_heap_over_bloom_block_bytes": bloom_obj / fbytes if fbytes else None})
    out["storage"] = idx

    # ---- Bloom validation and false-positive rate ----
    val, fp = [], []
    for s in sc:
        vals = [r["bloom_validation"] for r in s["runs"]]
        val.append({"records": s["records"], "runs_validated": len(vals),
                    "keys_checked_per_run": vals[0]["keys_checked"],
                    "tombstone_keys_checked_per_run": vals[0]["tombstone_keys_checked"],
                    "tables_per_run": vals[0]["tables"],
                    "false_negatives": sum(v["false_negatives"] for v in vals),
                    "all_stored_filters_equal_rebuilt": all(v["stored_filters_equal_rebuilt"] for v in vals)})
        probes = [r["fpr_probe"] for r in s["runs"] if r.get("fpr_probe")]
        fp.append({"records": s["records"], **{k: probes[0][k] for k in (
            "tables", "keys_per_table", "checks", "false_positives", "measured_fpr", "wilson95", "expected_fpr",
            "per_table_fpr_min_median_max")}} if probes else None)
    out["bloom_validation"] = val
    out["false_positive_rate"] = fp
    return out


def baseline_analysis(result):
    v3 = v3_reference()
    out = {}
    for tag in ("default_limit", "small_limit"):
        entry = result.get("baseline", {}).get(tag)
        if not entry:
            continue
        ref = v3["baseline"][tag]["aggregate_over_runs"]
        rows = {}
        for name, m in entry["aggregate_over_runs"].items():
            if name in ref:
                rows[name] = {p: {"v4_median": m[p]["median"], "v3_median": ref[name][p]["median"],
                                  "v4_over_v3": m[p]["median"] / ref[name][p]["median"]}
                              for p in ("p50_us", "p95_us", "p99_us")}
        out[tag] = rows
    return out


# ---- metadata / orchestration ------------------------------------------------------------------
def metadata(args):
    ram = ram_bytes()
    overrides = {k: getattr(args, k) for k in ("memtable_limit_bytes", "hits", "misses", "runs")
                 if getattr(args, k) is not None}
    return {
        "benchmark": "v4",
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
            "sstable_footer_bytes_v4": FOOTER_V4_SIZE,
            "index_entry_bytes": INDEX_ENTRY_BYTES,
            "index_interval": DEFAULT_INDEX_INTERVAL,
            "bloom_bits_per_key": BITS_PER_KEY,
            "bloom_num_hashes": NUM_HASHES,
            "bloom_expected_fpr_formula": bloom.expected_false_positive_rate(1000, bloom.filter_bytes(1000, 10),
                                                                              NUM_HASHES),
            "store_sync": False,
            "memtable_limit_bytes": args.memtable_limit_bytes or DEFAULT_MEMTABLE_LIMIT_BYTES,
            "default_memtable_limit_bytes": DEFAULT_MEMTABLE_LIMIT_BYTES,
            "runs_per_scale": {str(r): c["runs"] for r, c in V0_SCALES.items()},
            "gets_hit_per_run": {str(r): c for r, c in GET_HITS.items()},
            "gets_miss_per_run": {str(r): c for r, c in GET_MISSES.items()},
            "instrumented_gets_per_run": {"hit": STRUCT_HITS, "miss": STRUCT_MISSES_V4},
            "decomposition": {"miss_reps": DECOMP_REPS_MISS, "hit_reps": DECOMP_REPS_HIT,
                              "hit_pairs": DECOMP_HIT_PAIRS, "fp_pairs": FP_PAIRS, "hash_reps": HASH_REPS,
                              "build_reps": BUILD_REPS},
            "fpr_probe": {"min_keys_per_table": FPR_MIN_KEYS, "min_checks": FPR_MIN_CHECKS,
                          "probe_key_format": "zzz%08d", "probed_in_first_run_of_each_scale": True},
            "n_overwrites": N_OVERWRITES,
            "n_deletes": N_DELETES,
            "memory_probes_per_scale": MEMORY_PROBES,
            "baseline_runs": BASELINE_RUNS,
            "baseline_small_limit_bytes": SMALL_LIMIT_BYTES,
        },
        "timing_notes": "Only store calls are timed. Percentiles are nearest-rank (same code as V0..V3). Flush "
        "boundaries are observed by wrapping the store instance's flush and lsm_store.sstable.write_sstable; no "
        "production code is changed. A flush-triggering operation is a put/delete that finds memtable bytes >= the "
        "limit and flushes before applying itself; its latency includes the whole flush (here including building the "
        "Bloom filter). Raw per-operation latencies are not stored except for flush-triggering operations; "
        "percentiles of all, non-flush and flush-triggering writes are computed in-process. open_recovery is the "
        "first KVStore(path) after close() in the same process; open_components repeats the open steps with the "
        "module's own functions. Read-work counting, the per-table cost decomposition, the Bloom validation scan, the "
        "false-positive probe and the table-memory (tracemalloc) pass are untimed; read-work counting runs between "
        "the timed GETs and the timed overwrites (as in V2/V3) and the others after every timed phase of the run.",
    }


def checkpoint(result, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(RESULTS / "v4_benchmark.json"))
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
            print(f"scale {records:>10,} V4 run {i + 1}/{n_runs} ...", flush=True)
            t0 = time.perf_counter()
            runs.append(v4_run(records, limit, n_hits, n_misses, V0_SCALES[records]["gets_per_kind"], i == 0))
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
        entry["bloom_bits_per_key"] = BITS_PER_KEY
        result["scales"][str(records)] = entry
        del runs
        checkpoint(result, out_path)
    result["status"] = "complete"
    result["analysis"] = analyze(result)
    checkpoint(result, out_path)
    print("wrote", out_path)


if __name__ == "__main__":
    main()
