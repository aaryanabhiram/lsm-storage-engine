"""Deterministic structural/storage metrics for the V0 append-only store.

Usage:  python benchmarks/structural.py [--out benchmarks/results/v0_structural.json]

Nothing here is timed or measured by running a benchmark. Every number is derived
from the fixed record format (docs/v0-design.md) and the scaling-benchmark
configuration (docs/benchmarks.md), then cross-checked against the file sizes that
the scaling benchmark actually measured. "Bytes scanned" means bytes the application
reads from the file; it is not physical storage I/O (the benchmark is warm-cache).
"""

import argparse
import json
from pathlib import Path

from bench import ROOT, VALUE_SIZE, key_of
from bench_scaling import N_OVERWRITES
from lsm_store.record import HEADER_SIZE

CRC_FIELD_SIZE = 4  # the CRC covers every record byte except this leading field
KEY_SIZE = len(key_of(0))


def structural_metrics(records: int, overwrites: int = N_OVERWRITES,
                       key_size: int = KEY_SIZE, value_size: int = VALUE_SIZE) -> dict:
    """Structural metrics for a V0 file loaded with `records` distinct keys, then
    `overwrites` PUTs of existing keys (the scaling benchmark's workload)."""
    user_bytes = key_size + value_size  # payload per record
    record_bytes = HEADER_SIZE + user_bytes

    def state(physical: int) -> dict:
        live = records  # overwrites only touch existing keys, so live keys never change
        file_bytes = physical * record_bytes
        live_user_bytes = live * user_bytes
        return {
            "live_keys": live,
            "physical_records": physical,
            "stale_records": physical - live,  # superseded versions still in the file
            "file_bytes": file_bytes,
            "physical_user_bytes": physical * user_bytes,  # key+value bytes of ALL records
            "live_user_bytes": live_user_bytes,  # key+value bytes of the newest version of each key
            "format_overhead_bytes": physical * HEADER_SIZE,
            # file bytes / live user bytes = format overhead x staleness
            "space_amplification": file_bytes / live_user_bytes,
            "staleness_factor": physical / live,  # physical user bytes / live user bytes
        }

    def scan(physical: int) -> dict:
        # One pass of read_records over the whole file: every record is read and
        # CRC-checked exactly once; every file byte is read once.
        return {
            "records_examined": physical,
            "bytes_scanned": physical * record_bytes,
            "crc_checks": physical,
            "crc_covered_bytes": physical * (record_bytes - CRC_FIELD_SIZE),
        }

    after_load, final = records, records + overwrites
    return {
        "format": {
            "header_bytes_per_record": HEADER_SIZE,
            "file_level_fixed_bytes": 0,  # no file header or footer
            "key_bytes": key_size,
            "value_bytes": value_size,
            "user_bytes_per_record": user_bytes,
            "record_bytes": record_bytes,
            "overhead_fraction_of_record": HEADER_SIZE / record_bytes,
            "bytes_appended_per_user_byte": record_bytes / user_bytes,
        },
        "after_load": state(after_load),
        "after_overwrites": state(final),
        # Timed GETs run after the load and before the overwrites.
        "get_scan_during_benchmark": scan(after_load),
        "get_scan_after_overwrites": scan(final),  # e.g. the untimed verification GETs
        # open_recovery is timed after the load and before the overwrites.
        "open_recovery_as_benchmarked": scan(after_load),
    }


def build_report(scaling: dict) -> dict:
    """Derive metrics per scale and check them against the measured file sizes."""
    report = {}
    for key in sorted(scaling["scales"], key=int):
        entry = scaling["scales"][key]
        m = structural_metrics(entry["records"])
        assert entry["file_bytes_after_load"] == [m["after_load"]["file_bytes"]] * len(entry["file_bytes_after_load"])
        assert entry["file_bytes_final"] == [m["after_overwrites"]["file_bytes"]] * len(entry["file_bytes_final"])
        m["measured_open_recovery_seconds_median"] = entry["open_recovery_seconds_median"]
        report[key] = m
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scaling", default=str(ROOT / "benchmarks/results/v0_scaling.json"))
    ap.add_argument("--out", default=str(ROOT / "benchmarks/results/v0_structural.json"))
    args = ap.parse_args()
    scaling = json.loads(Path(args.scaling).read_text())
    result = {
        "kind": "deterministic structural metrics (derived, not timed); file sizes cross-checked against measured values",
        "overwrites_after_load": N_OVERWRITES,
        "scales": build_report(scaling),
    }
    Path(args.out).write_text(json.dumps(result, indent=1))
    for key, m in result["scales"].items():
        a, b = m["after_load"], m["after_overwrites"]
        print(f"{int(key):>10,}  GET scans {m['get_scan_during_benchmark']['records_examined']:>10,} rec "
              f"{m['get_scan_during_benchmark']['bytes_scanned']:>13,} B   "
              f"space amp {a['space_amplification']:.5f} -> {b['space_amplification']:.5f}")


if __name__ == "__main__":
    main()
