"""V0 scaling characterization. Methodology: docs/benchmarks.md.

Usage:  python benchmarks/bench_scaling.py [--scales 10000,100000] [--out PATH]
        python benchmarks/bench_scaling.py --analyze-only [--out PATH]

Same timing convention as bench.py: only the store call is timed (one
perf_counter_ns pair per op). Workload generation, database creation, correctness
checks, close and cleanup are not timed. `open_recovery` times KVStore(path) on an
existing file and is reported separately from operation latency.

All benchmarks here are WARM OS-CACHE benchmarks; no cold-cache number is produced.
"""

import argparse
import json
import math
import os
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
from array import array
from pathlib import Path

from bench import ROOT, VALUE_SIZE, aggregate, key_of, summarize
from lsm_store import KVStore

# records -> (GETs per kind: this many hits AND this many misses, independent runs).
# One GET costs O(file size), so the largest scales get fewer GETs and fewer runs.
SCALES = {
    10_000: {"gets_per_kind": 200, "runs": 5},
    100_000: {"gets_per_kind": 200, "runs": 5},
    1_000_000: {"gets_per_kind": 100, "runs": 5},
    5_000_000: {"gets_per_kind": 50, "runs": 3},
    10_000_000: {"gets_per_kind": 50, "runs": 3},
}
N_OVERWRITES = 1000  # timed PUTs of existing keys after the load, at every scale
N_OVERWRITE_CHECKS = 5  # untimed GETs after the overwrites to verify last-write-wins
RECORD_BYTES = 13 + len(key_of(0)) + VALUE_SIZE  # header + key + value = 124
MIN_FREE_RAM = 3 * 2**30  # refuse to start a run with less available RAM than this
MIN_FREE_DISK = 20 * 2**30


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


def check_headroom() -> None:
    ram = ram_bytes()
    if ram and ram[1] < MIN_FREE_RAM:
        raise SystemExit(f"stopping safely: only {ram[1] / 2**30:.1f} GiB RAM available")
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < MIN_FREE_DISK:
        raise SystemExit(f"stopping safely: only {free / 2**30:.1f} GiB disk free")


def stay_awake() -> None:
    """Process-scoped: ask Windows not to sleep while this process runs (reset on exit)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # CONTINUOUS | SYSTEM_REQUIRED


def timed_call(fn, *args):
    t0 = time.perf_counter_ns()
    result = fn(*args)
    return time.perf_counter_ns() - t0, result


def full_summary(latencies_ns) -> dict:
    s = summarize(sorted(latencies_ns))
    s["mean_us"] = sum(latencies_ns) / len(latencies_ns) / 1e3
    s["min_us"] = min(latencies_ns) / 1e3
    s["max_us"] = max(latencies_ns) / 1e3
    return s


def one_run(records: int, gets_per_kind: int, seed: int) -> dict:
    # Same RNG roles as the earlier scaling benchmark: values are consumed sequentially
    # (scale-independent prefix); hit keys then overwrite keys come from op_rng.
    value_rng = random.Random(seed)
    op_rng = random.Random(seed)
    hit_idx = [op_rng.randrange(records) for _ in range(gets_per_kind)]
    miss_keys = [key_of(records + i) for i in range(gets_per_kind)]  # never written
    overwrites = [(key_of(op_rng.randrange(records)), value_rng.randbytes(VALUE_SIZE))
                  for _ in range(N_OVERWRITES)]  # same generation order as the earlier benchmark

    check_headroom()
    ram_start = ram_bytes()
    hit_wanted = set(hit_idx)
    expected_hit = {}  # index -> value written during the load, for the sampled hit keys only
    load_lat = array("q")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "data.lsm")
        assert not os.path.exists(path), "stale database"
        db = KVStore(path, sync=False)

        # Phase 1: load. Per-op latencies kept in a compact array and summarized only.
        wall0 = time.perf_counter()
        for i in range(records):
            k, v = key_of(i), value_rng.randbytes(VALUE_SIZE)  # generated outside the timed call
            if i in hit_wanted:
                expected_hit[i] = v
            load_lat.append(timed_call(db.put, k, v)[0])
        load_wall_s = time.perf_counter() - wall0
        db.close()
        bytes_after_load = os.path.getsize(path)
        assert bytes_after_load == records * RECORD_BYTES, (bytes_after_load, records)
        load_summary = full_summary(load_lat)
        del load_lat

        # Phase 2: reopen. The recovery scan reads the whole file once, so after this the
        # file is in the OS page cache regardless of what was evicted since the load.
        t0 = time.perf_counter_ns()
        db = KVStore(path, sync=False)
        open_ns = time.perf_counter_ns() - t0

        raw = {}
        # Phase 3: timed GETs. Correctness is checked after each timed call, outside it.
        hit_lat = []
        for idx in hit_idx:
            ns, got = timed_call(db.get, key_of(idx))
            assert got == expected_hit[idx], f"GET hit returned wrong value for index {idx}"
            hit_lat.append(ns)
        raw["get_hit"] = hit_lat
        miss_lat = []
        for k in miss_keys:
            ns, got = timed_call(db.get, k)
            assert got is None, f"GET miss returned a value for {k!r}"
            miss_lat.append(ns)
        raw["get_miss"] = miss_lat

        # Phase 4: timed overwrite PUTs of existing keys.
        raw["put_overwrite"] = [timed_call(db.put, k, v)[0] for k, v in overwrites]

        # Untimed check that the newest value wins (a few full scans; not part of any latency).
        last = {}
        for k, v in overwrites:
            last[k] = v
        for k in list(last)[-N_OVERWRITE_CHECKS:]:
            assert db.get(k) == last[k], "overwrite did not return the newest value"
        db.close()
        bytes_final = os.path.getsize(path)
        assert bytes_final == (records + N_OVERWRITES) * RECORD_BYTES, bytes_final

    assert not os.path.exists(d), "temp database not cleaned up"
    summary = {name: full_summary(lats) for name, lats in raw.items()}
    summary["put_load"] = load_summary
    return {
        "summary": summary,
        "raw_latencies_ns": raw,  # put_load raw latencies are deliberately not stored
        "open_recovery_ns": open_ns,
        "load_wall_seconds": load_wall_s,
        "file_bytes_after_load": bytes_after_load,
        "file_bytes_final": bytes_final,
        "available_ram_bytes_at_start": ram_start[1] if ram_start else None,
    }


def scale_entry(records: int, cfg: dict, runs: list) -> dict:
    agg = aggregate([{"summary": r["summary"]} for r in runs])
    pooled = {name: full_summary([x for r in runs for x in r["raw_latencies_ns"][name]])
              for name in ("get_hit", "get_miss", "put_overwrite")}
    opens = [r["open_recovery_ns"] / 1e9 for r in runs]
    return {
        "records": records,
        "gets_per_kind": cfg["gets_per_kind"],
        "runs_planned": cfg["runs"],
        "runs_completed": len(runs),
        "file_bytes_after_load": [r["file_bytes_after_load"] for r in runs],
        "file_bytes_final": [r["file_bytes_final"] for r in runs],
        "open_recovery_seconds": opens,
        "open_recovery_seconds_median": statistics.median(opens),
        "load_wall_seconds": [r["load_wall_seconds"] for r in runs],
        "aggregate_over_runs": agg,  # per-run percentile -> median/min/max across runs
        "pooled_all_runs": pooled,  # percentiles over all samples from all runs together
        "runs": runs,
    }


def ols(xs, ys):
    """Least squares y = a + b x; returns (a, b, r2)."""
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return a, b, (1 - ss_res / ss_tot) if ss_tot else None  # R^2 undefined for a constant series


def analyze(result: dict) -> dict:
    """Quantify scaling from the measured medians. Pure arithmetic on the stored data."""
    sc = [result["scales"][k] for k in sorted(result["scales"], key=int)
          if result["scales"][k]["runs_completed"] == result["scales"][k]["runs_planned"]]
    if len(sc) < 2:
        return {}
    n = [s["records"] for s in sc]
    size = [s["file_bytes_after_load"][0] for s in sc]
    out = {"scales_used": n}

    def series(getter):
        return [getter(s) for s in sc]

    def fit(name, ys_us):
        # log-log slope: latency ~ size^slope (1.0 = linear). Linear fit: latency = a + b*records.
        _, slope, r2_log = ols([math.log(x) for x in size], [math.log(y) for y in ys_us])
        a, b, r2 = ols(n, ys_us)
        per_rec = [y / x for x, y in zip(n, ys_us)]
        out[name] = {
            "values_us": ys_us,
            "loglog_slope_vs_file_bytes": slope,
            "loglog_r2": r2_log,
            "linear_intercept_us": a,
            "linear_us_per_record": b,
            "linear_r2": r2,
            "us_per_record_at_each_scale": per_rec,
            "ratio_largest_over_smallest": ys_us[-1] / ys_us[0],
            "records_ratio": n[-1] / n[0],
        }

    fit("get_hit_p50", series(lambda s: s["pooled_all_runs"]["get_hit"]["p50_us"]))
    fit("get_miss_p50", series(lambda s: s["pooled_all_runs"]["get_miss"]["p50_us"]))
    fit("open_recovery", series(lambda s: s["open_recovery_seconds_median"] * 1e6))
    fit("put_overwrite_p50", series(lambda s: s["pooled_all_runs"]["put_overwrite"]["p50_us"]))
    fit("put_load_p50", series(lambda s: s["aggregate_over_runs"]["put_load"]["p50_us"]["median"]))
    out["hit_over_miss_p50_ratio"] = series(
        lambda s: s["pooled_all_runs"]["get_hit"]["p50_us"] / s["pooled_all_runs"]["get_miss"]["p50_us"])
    # Between-run spread of the per-run GET-hit median, as (max-min)/median.
    out["get_hit_p50_between_run_spread"] = series(
        lambda s: (lambda m: (m["max"] - m["min"]) / m["median"])(s["aggregate_over_runs"]["get_hit"]["p50_us"]))
    # Records at which the fitted per-record GET cost (hit p50 linear fit through the
    # measured range) would reach 1 s / 10 s / 60 s. Extrapolation, not measurement.
    b = out["get_hit_p50"]["linear_us_per_record"]
    a = out["get_hit_p50"]["linear_intercept_us"]
    out["extrapolated_records_for_get_latency"] = {f"{t}s": (t * 1e6 - a) / b for t in (1, 10, 60)}
    return out


def metadata(args, scales: dict, out_path: Path) -> dict:
    ram = ram_bytes()
    return {
        "benchmark": "v0_scaling",
        "result_set": "authoritative V0 scaling results",
        "command": " ".join([Path(sys.executable).name] + sys.argv),
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "ram_total_bytes": ram[0] if ram else None,
        "ram_available_bytes_at_start": ram[1] if ram else None,
        "disk_free_bytes_at_start": shutil.disk_usage(tempfile.gettempdir()).free,
        "temp_dir": tempfile.gettempdir(),
        "cache_condition": "WARM OS page cache. No cold-cache measurement is made or claimed.",
        "params": {
            "seed": args.seed,
            "key_size_bytes": len(key_of(0)),
            "value_size_bytes": VALUE_SIZE,
            "record_bytes": RECORD_BYTES,
            "store_sync": False,
            "n_overwrites_after_load": N_OVERWRITES,
            "n_overwrite_checks_untimed": N_OVERWRITE_CHECKS,
            "scales": {str(r): c for r, c in scales.items()},
        },
        "not_available_in_v0": {
            "read_amplification": "not measured: every GET scans the whole file by design",
            "write_amplification": "not measured: V0 writes each record exactly once, no rewrites",
        },
        "timing_notes": "Only store calls are timed. Throughput = ops / sum of per-op latencies. "
        "put_load is stored as a summary (mean/min/max/percentiles) only; get_hit, get_miss and "
        "put_overwrite raw latencies are stored. Percentiles are nearest-rank. "
        "open_recovery is the first KVStore(path) after close() in the same process.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "benchmarks/results/v0_scaling.json"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scales", default=",".join(map(str, SCALES)),
                    help="comma-separated record counts; must be keys of SCALES")
    ap.add_argument("--analyze-only", action="store_true", help="recompute the analysis block of --out")
    args = ap.parse_args()
    out_path = Path(args.out)

    if args.analyze_only:
        result = json.loads(out_path.read_text())
        result["analysis"] = analyze(result)
        out_path.write_text(json.dumps(result, indent=1))
        print(json.dumps(result["analysis"], indent=1))
        return

    stay_awake()
    scales = {int(s): SCALES[int(s)] for s in args.scales.split(",")}
    result = {"metadata": metadata(args, scales, out_path), "status": "in_progress", "scales": {}}

    def checkpoint() -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=1))
        tmp.replace(out_path)

    for records, cfg in scales.items():
        runs = []
        for i in range(cfg["runs"]):
            print(f"scale {records:>10,} run {i + 1}/{cfg['runs']} ...", flush=True)
            t0 = time.perf_counter()
            runs.append(one_run(records, cfg["gets_per_kind"], args.seed))
            r = runs[-1]["summary"]
            print(f"  done in {time.perf_counter() - t0:.0f}s  file={runs[-1]['file_bytes_after_load']:,} B  "
                  f"open={runs[-1]['open_recovery_ns'] / 1e9:.2f}s  "
                  f"hit p50={r['get_hit']['p50_us'] / 1e3:.1f}ms  miss p50={r['get_miss']['p50_us'] / 1e3:.1f}ms  "
                  f"overwrite p50={r['put_overwrite']['p50_us']:.1f}us", flush=True)
            result["scales"][str(records)] = scale_entry(records, cfg, runs)
            checkpoint()  # after every run so a late failure keeps earlier results
    result["status"] = "complete"
    result["analysis"] = analyze(result)
    checkpoint()
    print("wrote", out_path)


if __name__ == "__main__":
    main()
