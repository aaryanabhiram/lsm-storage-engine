"""V0 baseline benchmark. Methodology: docs/benchmarks.md.

Usage:  python benchmarks/bench.py [--out benchmarks/results/v0_baseline.json]

Only the store call itself is timed (one perf_counter_ns pair per operation).
Workload generation, database creation and cleanup are not timed.
"""

import argparse
import json
import os
import platform
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from lsm_store import KVStore

ROOT = Path(__file__).resolve().parent.parent
VALUE_SIZE = 100


def key_of(i: int) -> bytes:
    return b"key%08d" % i


def build_workload(seed: int, keyspace: int, n_get: int, n_mixed: int, n_sync: int) -> dict:
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


def timed(fn, *args) -> int:
    t0 = time.perf_counter_ns()
    fn(*args)
    return time.perf_counter_ns() - t0


def summarize(latencies_ns: list) -> dict:
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


def one_run(wl: dict) -> dict:
    out = {}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "data.lsm")
        db = KVStore(path, sync=False)
        raw = {}

        raw["put_load"] = [timed(db.put, k, v) for k, v in wl["load"]]
        out["file_bytes_after_load"] = os.path.getsize(path)

        raw["get_hit"] = [timed(db.get, k) for k in wl["gets"][0::2]]
        raw["get_miss"] = [timed(db.get, k) for k in wl["gets"][1::2]]

        mixed = {"get": [], "put": [], "delete": []}
        mixed_all = []
        for op, k, v in wl["mixed"]:
            lat = timed(getattr(db, op), *((k, v) if op == "put" else (k,)))
            mixed[op].append(lat)
            mixed_all.append(lat)
        raw["mixed_all"] = mixed_all
        for op, lats in mixed.items():
            raw[f"mixed_{op}"] = lats
        out["file_bytes_after_mixed"] = os.path.getsize(path)
        db.close()

        path2 = os.path.join(d, "synced.lsm")
        db2 = KVStore(path2, sync=True)
        raw["put_sync"] = [timed(db2.put, k, v) for k, v in wl["sync_puts"]]
        db2.close()

    out["raw_latencies_ns"] = raw
    out["summary"] = {name: summarize(lats) for name, lats in raw.items() if lats}
    return out


def metadata(args, out_path: Path) -> dict:
    return {
        "benchmark": "v0",
        "python": sys.version.replace("\n", " "),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "temp_dir": tempfile.gettempdir(),
        "params": {
            "seed": args.seed,
            "runs": args.runs,
            "keyspace": args.keyspace,
            "value_size_bytes": VALUE_SIZE,
            "key_size_bytes": len(key_of(0)),
            "n_get_ops": args.gets,
            "get_hit_fraction": 0.5,
            "n_mixed_ops": args.mixed,
            "mixed_mix": {"get": 0.70, "put_overwrite": 0.25, "delete": 0.05},
            "n_sync_puts": args.sync_puts,
            "put_load_sync": False,
            "put_sync_sync": True,
        },
        "not_available_in_v0": {
            "read_amplification": "not measured: every GET scans the whole file by design",
            "write_amplification": "not measured: V0 writes each record exactly once, no rewrites",
        },
        "timing_notes": "Only store calls are timed. Throughput = ops / sum of per-op latencies. "
        "OS page cache is warm (no cold-cache measurement).",
    }


def aggregate(runs: list) -> dict:
    agg = {}
    for name in runs[0]["summary"]:
        agg[name] = {}
        for metric in runs[0]["summary"][name]:
            vals = [r["summary"][name][metric] for r in runs]
            agg[name][metric] = {"median": statistics.median(vals), "min": min(vals), "max": max(vals)}
    return agg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "benchmarks/results/v0_baseline.json"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--keyspace", type=int, default=10_000)
    ap.add_argument("--gets", type=int, default=200)
    ap.add_argument("--mixed", type=int, default=300)
    ap.add_argument("--sync-puts", type=int, default=300)
    args = ap.parse_args()
    out_path = Path(args.out)

    meta = metadata(args, out_path)
    wl = build_workload(args.seed, args.keyspace, args.gets, args.mixed, args.sync_puts)
    runs = []
    for i in range(args.runs):
        print(f"run {i + 1}/{args.runs} ...", flush=True)
        runs.append(one_run(wl))  # same workload every run; fresh database each run
    result = {"metadata": meta, "aggregate_over_runs": aggregate(runs), "runs": runs}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=1))
    for name, m in result["aggregate_over_runs"].items():
        print(f"{name:12s} p50={m['p50_us']['median']:10.1f}us p95={m['p95_us']['median']:10.1f}us "
              f"p99={m['p99_us']['median']:10.1f}us  {m['throughput_ops_per_s']['median']:10.1f} ops/s")
    print("wrote", out_path)


if __name__ == "__main__":
    main()
