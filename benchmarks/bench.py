"""V1 benchmark: WAL + memtable compared with the recorded V0 baseline. Methodology: docs/benchmarks.md.

Usage:
    python benchmarks/bench.py --scales 10000 --out PATH       # scaling runs for the listed scales
    python benchmarks/bench.py --baseline --out PATH           # 10,000-key head-to-head workloads
    python benchmarks/bench.py --analyze-only --out FINAL.json

Timing convention is the V0 one: only the store call is timed (one perf_counter_ns pair per
operation); workload generation, correctness checks, close and cleanup are not. The V0 results
(benchmarks/results/v0_scaling.json, v0_baseline.json) are read, never written; the V0 store itself
is not run. All numbers here are WARM OS-CACHE numbers.

Per scale, one invocation does:
  1. V1 scaling runs (run count per scale = V0 ladder): put_load, open_recovery, get_hit, get_miss, put_overwrite, delete.
  2. MEMORY_PROBES untimed memory probes, each in fresh Python processes.
"""

import argparse
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
import zlib
from array import array
from pathlib import Path

from lsm_store import KVStore
from lsm_store.record import HEADER_SIZE

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


def timed(fn, *args):
    t0 = time.perf_counter_ns()
    fn(*args)
    return time.perf_counter_ns() - t0


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
GETS_PER_KIND = 100_000  # V1 GETs are far cheaper than V0's, so every scale gets this many hits AND misses
# independent runs per scale (fresh database each): the V0 ladder, 5/5/5/3/3 (V0_SCALES[records]["runs"])
N_DELETES = 1000  # timed DELETEs of keys drawn uniformly from the keyspace (may repeat), per run
MEMORY_PROBES = 3  # untimed probes per scale
DELETE_RECORD_BYTES = HEADER_SIZE + len(key_of(0))  # 24: a DELETE record carries no value
BASELINE_RUNS = 5  # same as the V0 baseline
SCALES = list(V0_SCALES)
RESULTS = ROOT / "benchmarks/results"


def delete_keys(records, seed=SEED):
    rng = random.Random(f"delete:{seed}:{records}")
    return [key_of(rng.randrange(records)) for _ in range(N_DELETES)]


def fingerprint(memtable):
    """Order-independent digest of a memtable (used to check replay reproduces the live state)."""
    total = 0
    for k, v in memtable.items():
        total += zlib.crc32(k + (b"\x00" if v is None else b"\x01" + v))
    return total & (2**64 - 1)


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


def memory_probe(mode, records, path):
    """Run inside a fresh process. 'load': build a V1 database of `records` keys. 'open': replay an existing WAL."""
    import gc
    gc.collect()
    before = process_memory()
    if mode == "load":
        rng = random.Random(SEED)
        db = KVStore(path, sync=False)
        for i in range(records):
            db.put(key_of(i), rng.randbytes(VALUE_SIZE))
    else:
        db = KVStore(path, sync=False)
    gc.collect()
    after = process_memory()
    assert len(db._memtable) == records
    db.close()
    return {"mode": mode, "records": records, "before": before, "after": after}


def run_memory_probe(records):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "data.wal")
        out = {}
        for mode in ("load", "open"):
            cmd = [sys.executable, str(Path(__file__).resolve()), "--probe", mode, "--probe-records", str(records),
                   "--probe-path", path]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            out[mode] = json.loads(proc.stdout.strip().splitlines()[-1])
    return {mode: {"private_delta": p["after"]["private"] - p["before"]["private"],
                   "working_set_delta": p["after"]["working_set"] - p["before"]["working_set"],
                   "peak_working_set_after": p["after"]["peak_working_set"],
                   "private_after": p["after"]["private"], "private_before": p["before"]["private"]}
            for mode, p in out.items()}


# ---- V1 scaling run -------------------------------------------------------------------------
def v1_run(records):
    v0_gets = V0_SCALES[records]["gets_per_kind"]
    value_rng = random.Random(SEED)
    op_rng = random.Random(SEED)
    # The first v0_gets hit keys, the miss keys and all overwrites are generated exactly as in
    # the V0 scaling run (same RNG roles and order), so they match V0's logical workload.
    hit_idx = [op_rng.randrange(records) for _ in range(v0_gets)]
    overwrites = [(key_of(op_rng.randrange(records)), value_rng.randbytes(VALUE_SIZE))
                  for _ in range(N_OVERWRITES)]
    extra = random.Random(f"extra-hits:{SEED}:{records}")
    hit_idx += [extra.randrange(records) for _ in range(GETS_PER_KIND - v0_gets)]
    miss_keys = [key_of(records + i) for i in range(GETS_PER_KIND)]  # never written
    del_keys = delete_keys(records)
    hit_keys = [key_of(i) for i in hit_idx]

    check_headroom()
    hit_wanted = set(hit_idx)
    expected_hit = {}
    load_lat = array("q")
    raw = {}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "data.wal")
        assert not os.path.exists(path), "stale database"
        db = KVStore(path, sync=False)

        wall0 = time.perf_counter()
        for i in range(records):
            k, v = key_of(i), value_rng.randbytes(VALUE_SIZE)
            if i in hit_wanted:
                expected_hit[i] = v
            load_lat.append(timed_call(db.put, k, v)[0])
        load_wall_s = time.perf_counter() - wall0
        assert len(db._memtable) == records
        fp_load = fingerprint(db._memtable)
        db.close()
        del db  # free the old memtable before replay builds a new one
        bytes_after_load = os.path.getsize(path)
        assert bytes_after_load == records * RECORD_BYTES, (bytes_after_load, records)
        load_summary = full_summary(load_lat)
        del load_lat

        # open_recovery: the first KVStore(path) after close() in this process (replays the whole WAL).
        t0 = time.perf_counter_ns()
        db = KVStore(path, sync=False)
        open_ns = time.perf_counter_ns() - t0
        assert len(db._memtable) == records and fingerprint(db._memtable) == fp_load, "replay != live state"
        assert os.path.getsize(path) == bytes_after_load  # nothing was truncated

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

        # Controls on the same hit keys: an empty Python function call, and a bare dict.get on the
        # memtable. They show the timing floor and how much of GET latency is the store wrapper.
        noop = lambda k: None  # noqa: E731
        raw["control_noop"] = array("q", (timed_call(noop, k)[0] for k in hit_keys))
        raw["control_dict_get"] = array("q", (timed_call(db._memtable.get, k)[0] for k in hit_keys))

        raw["put_overwrite"] = array("q", (timed_call(db.put, k, v)[0] for k, v in overwrites))
        last = {}
        for k, v in overwrites:
            last[k] = v
        for k, v in last.items():
            assert db.get(k) == v, "overwrite did not return the newest value"

        raw["delete"] = array("q", (timed_call(db.delete, k)[0] for k in del_keys))
        deleted = set(del_keys)
        for k in deleted:
            assert db.get(k) is None, "deleted key still readable"
        tombstones = sum(1 for v in db._memtable.values() if v is None)
        assert tombstones == len(deleted) and len(db._memtable) == records
        fp_final = fingerprint(db._memtable)
        db.close()
        del db
        bytes_final = os.path.getsize(path)
        assert bytes_final == (records + N_OVERWRITES) * RECORD_BYTES + N_DELETES * DELETE_RECORD_BYTES, bytes_final

        # Untimed correctness reopen of the final WAL (with overwrites and tombstones), timed for the record.
        t0 = time.perf_counter_ns()
        db = KVStore(path, sync=False)
        reopen_final_ns = time.perf_counter_ns() - t0
        assert fingerprint(db._memtable) == fp_final, "replay of final WAL != live state"
        db.close()
    assert not os.path.exists(d), "temp database not cleaned up"

    summary = {name: full_summary(lats) for name, lats in raw.items()}
    summary["put_load"] = load_summary
    return {
        "summary": summary,
        "_raw": raw,  # pooled across runs by scale_entry, never written to the result file
        "open_recovery_ns": open_ns,
        "open_recovery_final_ns": reopen_final_ns,
        "load_wall_seconds": load_wall_s,
        "file_bytes_after_load": bytes_after_load,
        "file_bytes_final": bytes_final,
        "memtable_entries": records,
        "tombstoned_keys_final": tombstones,
        "records_replayed_at_open": records,
        "records_replayed_at_final_reopen": records + N_OVERWRITES + N_DELETES,
    }


def scale_entry(records, runs, memory):
    pooled = {}
    for name in runs[0]["_raw"]:
        all_latencies = array("q")
        for r in runs:
            all_latencies.extend(r["_raw"][name])
        pooled[name] = full_summary(all_latencies)
    clean = []
    for r in runs:
        run = {}
        for k, v in r.items():
            if k != "_raw":
                run[k] = v
        clean.append(run)
    opens = [r["open_recovery_ns"] / 1e9 for r in runs]
    return {
        "records": records,
        "gets_per_kind": GETS_PER_KIND,
        "runs_planned": V0_SCALES[records]["runs"],
        "runs_completed": len(runs),
        "file_bytes_after_load": [r["file_bytes_after_load"] for r in runs],
        "file_bytes_final": [r["file_bytes_final"] for r in runs],
        "open_recovery_seconds": opens,
        "open_recovery_seconds_median": statistics.median(opens),
        "open_recovery_final_seconds": [r["open_recovery_final_ns"] / 1e9 for r in runs],
        "load_wall_seconds": [r["load_wall_seconds"] for r in runs],
        "aggregate_over_runs": aggregate([{"summary": r["summary"]} for r in runs]),
        "pooled_all_runs": pooled,
        "runs": clean,
        "memory_probes": memory,
    }


# ---- 10,000-key head-to-head with the V0 baseline workload -----------------------------------
def baseline_run(wl):
    """Mirror of the V0 baseline run for KVStore, plus an untimed reference-dict check of every result."""
    out, raw, ref = {}, {}, {}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "data.wal")
        db = KVStore(path, sync=False)
        raw["put_load"] = [timed(db.put, k, v) for k, v in wl["load"]]
        ref.update(wl["load"])
        out["file_bytes_after_load"] = os.path.getsize(path)

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
        for op, k, v in wl["mixed"]:
            ns, got = timed_call(getattr(db, op), *((k, v) if op == "put" else (k,)))
            if op == "get":
                assert got == ref.get(k), "mixed GET disagrees with the reference dict"
            elif op == "put":
                ref[k] = v
            else:
                ref.pop(k, None)
            mixed[op].append(ns)
            mixed_all.append(ns)
        raw["mixed_all"] = mixed_all
        for op, lats in mixed.items():
            raw[f"mixed_{op}"] = lats
        out["file_bytes_after_mixed"] = os.path.getsize(path)
        db.close()
        db = KVStore(path, sync=False)  # replay must reproduce the reference state
        assert {k: v for k, v in db._memtable.items() if v is not None} == ref
        db.close()

        db2 = KVStore(os.path.join(d, "synced.wal"), sync=True)
        raw["put_sync"] = [timed(db2.put, k, v) for k, v in wl["sync_puts"]]
        db2.close()
    out["summary"] = {name: summarize(lats) for name, lats in raw.items() if lats}
    return out


def baseline_entry(wl_args, runs):
    wl = build_workload(SEED, *wl_args)
    res = []
    for i in range(runs):
        print(f"  baseline workload {wl_args} run {i + 1}/{runs} ...", flush=True)
        res.append(baseline_run(wl))
    return {"workload": {"keyspace": wl_args[0], "n_get_ops": wl_args[1], "n_mixed_ops": wl_args[2],
                         "n_sync_puts": wl_args[3]},
            "runs_completed": runs,
            "aggregate_over_runs": aggregate(res),
            "file_bytes_after_load": [r["file_bytes_after_load"] for r in res],
            "file_bytes_after_mixed": [r["file_bytes_after_mixed"] for r in res],
            "runs": [{"summary": r["summary"]} for r in res]}


# ---- analysis -------------------------------------------------------------------------------
def load_v0():
    scaling = json.loads((RESULTS / "v0_scaling.json").read_text())
    baseline = json.loads((RESULTS / "v0_baseline.json").read_text())
    return scaling, baseline


def ratios(top, bottom):
    out = []
    for t, b in zip(top, bottom):
        out.append(t / b)
    return out


def spread(m):
    return (m["max"] - m["min"]) / m["median"]


def analyze(result):
    v0s, v0b = load_v0()
    sc = []
    for k in sorted(result["scales"], key=int):
        entry = result["scales"][k]
        if entry["runs_completed"] == entry["runs_planned"]:
            sc.append(entry)
    out = {"scales_used": [s["records"] for s in sc]}
    if len(sc) < 2:
        return out
    n = [s["records"] for s in sc]
    size = [s["file_bytes_after_load"][0] for s in sc]
    v0 = [v0s["scales"][str(r)] for r in n]

    def fit(values):
        log_size = [math.log(x) for x in size]
        log_values = [math.log(y) for y in values]
        _, slope, r2_log = ols(log_size, log_values)
        a, b, r2 = ols(n, values)
        return {"values": values, "loglog_slope_vs_file_bytes": slope, "loglog_r2": r2_log,
                "linear_intercept": a, "linear_per_record": b, "linear_r2": r2,
                "ratio_largest_over_smallest": values[-1] / values[0], "records_ratio": n[-1] / n[0]}

    for kind in ("get_hit", "get_miss"):
        for p in ("p50_us", "p95_us", "p99_us"):
            v1 = [s["pooled_all_runs"][kind][p] for s in sc]
            v0v = [e["pooled_all_runs"][kind][p] for e in v0]
            out[f"{kind}_{p}"] = {"v1_us": v1, "v0_us": v0v, "v0_over_v1": ratios(v0v, v1)}
        out[f"{kind}_p50_v1_fit"] = fit(out[f"{kind}_p50_us"]["v1_us"])
    out["get_hit_p50_v0_fit"] = fit(out["get_hit_p50_us"]["v0_us"])
    # How much of GET latency is the store itself: compare with a bare dict.get and an empty function call.
    out["get_hit_vs_controls_p50_us"] = {
        "store_get": out["get_hit_p50_us"]["v1_us"],
        "dict_get_control": [s["pooled_all_runs"]["control_dict_get"]["p50_us"] for s in sc],
        "noop_control": [s["pooled_all_runs"]["control_noop"]["p50_us"] for s in sc],
    }
    out["get_hit_p50_between_run_spread"] = [spread(s["aggregate_over_runs"]["get_hit"]["p50_us"]) for s in sc]
    out["get_hit_p99_between_run_spread"] = [spread(s["aggregate_over_runs"]["get_hit"]["p99_us"]) for s in sc]
    out["get_samples_per_kind_pooled"] = [s["pooled_all_runs"]["get_hit"]["ops"] for s in sc]

    v1_open = [s["open_recovery_seconds_median"] for s in sc]
    v0_open = [e["open_recovery_seconds_median"] for e in v0]
    us_per_record_v1 = []
    us_per_record_v0 = []
    open_spread = []
    for i in range(len(sc)):
        us_per_record_v1.append(v1_open[i] * 1e6 / n[i])
        us_per_record_v0.append(v0_open[i] * 1e6 / n[i])
        times = sc[i]["open_recovery_seconds"]
        open_spread.append((max(times) - min(times)) / sc[i]["open_recovery_seconds_median"])
    out["open_recovery"] = {"v1_seconds": v1_open, "v0_seconds": v0_open,
                            "v1_over_v0": ratios(v1_open, v0_open),
                            "v1_us_per_record": us_per_record_v1,
                            "v0_us_per_record": us_per_record_v0,
                            "v1_fit_seconds": fit(v1_open),
                            "v1_between_run_spread": open_spread}

    # put_load: median of the per-run p50s. put_overwrite: p50 of all runs pooled together.
    v1 = [s["aggregate_over_runs"]["put_load"]["p50_us"]["median"] for s in sc]
    v0v = [e["aggregate_over_runs"]["put_load"]["p50_us"]["median"] for e in v0]
    out["put_load_p50_us"] = {"v1": v1, "v0": v0v, "v1_over_v0": ratios(v1, v0v)}
    v1 = [s["pooled_all_runs"]["put_overwrite"]["p50_us"] for s in sc]
    v0v = [e["pooled_all_runs"]["put_overwrite"]["p50_us"] for e in v0]
    out["put_overwrite_p50_us"] = {"v1": v1, "v0": v0v, "v1_over_v0": ratios(v1, v0v)}
    out["put_load_throughput_ops_per_s"] = {
        "v1": [s["aggregate_over_runs"]["put_load"]["throughput_ops_per_s"]["median"] for s in sc],
        "v0": [e["aggregate_over_runs"]["put_load"]["throughput_ops_per_s"]["median"] for e in v0]}
    out["delete_p50_us"] = {"v1": [s["pooled_all_runs"]["delete"]["p50_us"] for s in sc]}  # V0 has no delete phase

    mem = []
    for s in sc:
        per_key = []
        per_key_open = []
        for probe in s["memory_probes"]:
            per_key.append(probe["load"]["private_delta"] / s["records"])
            per_key_open.append(probe["open"]["private_delta"] / s["records"])
        mem.append({"records": s["records"],
                    "private_bytes_per_key_after_load": {"median": statistics.median(per_key), "min": min(per_key), "max": max(per_key)},
                    "private_bytes_per_key_after_replay": {"median": statistics.median(per_key_open), "min": min(per_key_open), "max": max(per_key_open)},
                    "private_delta_over_payload_after_load": statistics.median(per_key) / (len(key_of(0)) + VALUE_SIZE)})
    out["memory"] = mem
    return out


def baseline_analysis(result):
    _, v0b = load_v0()
    out = {}
    v0agg = v0b["aggregate_over_runs"]
    for tag in ("same_workload_as_v0",):
        entry = result.get("baseline", {}).get(tag)
        if not entry:
            continue
        rows = {}
        for name, m in entry["aggregate_over_runs"].items():
            if name not in v0agg:
                continue
            rows[name] = {}
            for p in ("p50_us", "p95_us", "p99_us"):
                v1_median = m[p]["median"]
                v0_median = v0agg[name][p]["median"]
                rows[name][p] = {"v1_median": v1_median, "v0_median": v0_median, "v0_over_v1": v0_median / v1_median}
        out[tag] = rows
    return out


# ---- metadata / orchestration ----------------------------------------------------------------
def metadata(args, out_path):
    ram = ram_bytes()
    return {
        "benchmark": "v1",
        "command": " ".join([Path(sys.executable).name] + sys.argv),
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
            "store_sync": False,
            "runs_per_scale": {str(r): c["runs"] for r, c in V0_SCALES.items()},
            "gets_per_kind_per_run": GETS_PER_KIND,
            "v0_gets_per_kind": {str(r): c["gets_per_kind"] for r, c in V0_SCALES.items()},
            "n_overwrites": N_OVERWRITES,
            "n_deletes": N_DELETES,
            "memory_probes_per_scale": MEMORY_PROBES,
            "baseline_runs": BASELINE_RUNS,
        },
        "timing_notes": "Only store calls are timed. Percentiles are nearest-rank (same code as V0). "
        "Raw per-operation latencies are not stored; get/overwrite/delete percentiles are pooled over all "
        "runs at a scale, and the per-run values are kept. open_recovery is the first KVStore(path) after "
        "close() in the same process.",
    }


def checkpoint(result, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(RESULTS / "v1_benchmark.json"))
    ap.add_argument("--scales", default=None, help="comma-separated record counts from the V0 scale ladder")
    ap.add_argument("--baseline", action="store_true", help="10,000-key head-to-head workloads")
    ap.add_argument("--analyze-only", action="store_true")
    ap.add_argument("--probe", choices=["load", "open"])
    ap.add_argument("--probe-records", type=int)
    ap.add_argument("--probe-path")
    args = ap.parse_args()
    out_path = Path(args.out)

    if args.probe:
        print(json.dumps(memory_probe(args.probe, args.probe_records, args.probe_path)))
        return

    if args.analyze_only:
        result = json.loads(out_path.read_text())
        result["analysis"] = analyze(result)
        result["baseline_analysis"] = baseline_analysis(result)
        checkpoint(result, out_path)
        print(json.dumps({"analysis_keys": list(result["analysis"]), "scales": list(result["scales"])}))
        return

    result = {"metadata": metadata(args, out_path), "status": "in_progress", "scales": {}, "baseline": {}}

    if args.baseline:
        result["baseline"]["same_workload_as_v0"] = baseline_entry((10_000, 200, 300, 300), BASELINE_RUNS)
        result["status"] = "complete"
        checkpoint(result, out_path)
        print("wrote", out_path)
        return

    for records in [int(s) for s in (args.scales or ",".join(map(str, SCALES))).split(",")]:
        assert records in V0_SCALES, "scale must be on the V0 ladder"
        n_runs = V0_SCALES[records]["runs"]
        runs = []
        for i in range(n_runs):
            print(f"scale {records:>10,} V1 run {i + 1}/{n_runs} ...", flush=True)
            t0 = time.perf_counter()
            runs.append(v1_run(records))
            s = runs[-1]["summary"]
            print(f"  done in {time.perf_counter() - t0:.0f}s  open={runs[-1]['open_recovery_ns'] / 1e9:.2f}s  "
                  f"hit p50={s['get_hit']['p50_us']:.2f}us  miss p50={s['get_miss']['p50_us']:.2f}us  "
                  f"overwrite p50={s['put_overwrite']['p50_us']:.1f}us  delete p50={s['delete']['p50_us']:.1f}us", flush=True)
        memory = []
        for i in range(MEMORY_PROBES):
            print(f"scale {records:>10,} memory probe {i + 1}/{MEMORY_PROBES} ...", flush=True)
            memory.append(run_memory_probe(records))
        result["scales"][str(records)] = scale_entry(records, runs, memory)
        del runs
        checkpoint(result, out_path)
    result["status"] = "complete"
    result["analysis"] = analyze(result)
    checkpoint(result, out_path)
    print("wrote", out_path)


if __name__ == "__main__":
    main()
