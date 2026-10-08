"""Deterministic structural/storage metrics for the V1 WAL + memtable store.

Usage:  python benchmarks/structural.py [--out benchmarks/results/v1_structural.json]

Nothing here is timed. Every number is derived from the record format (docs/v0-design.md, reused
unchanged by the V1 WAL) and the V1 scaling workload (docs/benchmarks.md): N distinct keys loaded,
then 1,000 overwrites of existing keys, then 1,000 DELETEs of keys drawn uniformly from the keyspace.
Derived sizes and counts are cross-checked against the values the benchmark measured. "Bytes
examined" means bytes the application reads from the WAL, not physical storage I/O.

Terminology: stale records are superseded WAL records (physical records minus distinct keys). This is
append-only staleness, not LSM write amplification: V1 never rewrites data.
"""

import argparse
import json
from pathlib import Path

from bench import DELETE_RECORD_BYTES, N_DELETES, N_OVERWRITES, ROOT, VALUE_SIZE, delete_keys, key_of
from lsm_store.record import HEADER_SIZE

CRC_FIELD_SIZE = 4
KEY_SIZE = len(key_of(0))


def structural_metrics(records: int, overwrites: int = N_OVERWRITES, deletes: int = N_DELETES,
                       distinct_deleted: int = None, key_size: int = KEY_SIZE,
                       value_size: int = VALUE_SIZE) -> dict:
    """Metrics for a V1 WAL loaded with `records` distinct keys, then `overwrites` PUTs and `deletes`
    DELETEs of existing keys (`distinct_deleted` distinct keys among them)."""
    if distinct_deleted is None:
        distinct_deleted = len(set(delete_keys(records)))  # the benchmark's delete workload
    user_bytes = key_size + value_size
    put_bytes = HEADER_SIZE + user_bytes
    del_bytes = HEADER_SIZE + key_size

    def state(puts: int, dels: int, tombstoned: int) -> dict:
        physical = puts + dels
        wal_bytes = puts * put_bytes + dels * del_bytes
        live = records - tombstoned
        physical_user = puts * user_bytes + dels * key_size
        live_user = live * user_bytes
        return {
            "physical_records": physical,
            "put_records": puts,
            "delete_records": dels,
            "wal_bytes": wal_bytes,
            "memtable_entries": records,  # one per distinct key, tombstoned keys included
            "live_keys": live,
            "tombstoned_keys": tombstoned,
            "stale_records": physical - records,  # superseded records still in the WAL
            "physical_user_bytes": physical_user,  # key+value of PUTs plus key of DELETEs
            "live_user_bytes": live_user,
            "format_overhead_bytes": physical * HEADER_SIZE,
            "bytes_appended_per_user_byte": wal_bytes / physical_user,
            "space_amplification": wal_bytes / live_user if live_user else None,  # WAL bytes / live user bytes
            "staleness_factor": physical / records,  # physical records / distinct keys
            # Payload bytes the memtable must hold at minimum: every key, plus the value of live keys.
            # Excludes Python object, dict and allocator overhead.
            "memtable_payload_lower_bound_bytes": records * key_size + live * value_size,
        }

    def replay(physical: int, wal_bytes: int) -> dict:
        return {"records_examined": physical, "bytes_examined": wal_bytes, "crc_checks": physical,
                "crc_covered_bytes": wal_bytes - CRC_FIELD_SIZE * physical, "memtable_entries_built": records}

    after_load = state(records, 0, 0)
    after_overwrites = state(records + overwrites, 0, 0)
    after_deletes = state(records + overwrites, deletes, distinct_deleted)
    return {
        "format": {
            "header_bytes_per_record": HEADER_SIZE,
            "put_record_bytes": put_bytes,
            "delete_record_bytes": del_bytes,
            "user_bytes_per_put": user_bytes,
            "overhead_fraction_of_put_record": HEADER_SIZE / put_bytes,
            "bytes_appended_per_user_byte_put": put_bytes / user_bytes,
        },
        "after_load": after_load,
        "after_overwrites": after_overwrites,
        "after_deletes": after_deletes,
        # Timed GETs and the timed open run after the load (before overwrites and deletes).
        "get_wal_work_per_get": {
            "v0_records_scanned": after_load["physical_records"],
            "v0_bytes_scanned": after_load["wal_bytes"],
            "v1_wal_records_read": 0,  # architectural: get() consults only the memtable (enforced by a test)
            "v1_wal_bytes_read": 0,
            "v1_memtable_lookups": 1,
        },
        "open_recovery_as_benchmarked": replay(after_load["physical_records"], after_load["wal_bytes"]),
        "open_recovery_final_wal": replay(after_deletes["physical_records"], after_deletes["wal_bytes"]),
    }


def build_report(result: dict) -> dict:
    """Derive metrics per scale and check them against what the benchmark measured."""
    report = {}
    for key in sorted(result["scales"], key=int):
        entry = result["scales"][key]
        records = entry["records"]
        runs = entry["runs"]
        distinct = {r["tombstoned_keys_final"] for r in runs}
        assert len(distinct) == 1, "tombstone count differs between runs"
        m = structural_metrics(records, distinct_deleted=distinct.pop())
        assert entry["file_bytes_after_load"] == [m["after_load"]["wal_bytes"]] * len(runs)
        assert entry["file_bytes_final"] == [m["after_deletes"]["wal_bytes"]] * len(runs)
        assert {r["memtable_entries"] for r in runs} == {m["after_deletes"]["memtable_entries"]}
        assert {r["tombstoned_keys_final"] for r in runs} == {m["after_deletes"]["tombstoned_keys"]}
        assert {r["records_replayed_at_open"] for r in runs} == {m["open_recovery_as_benchmarked"]["records_examined"]}
        assert {r["records_replayed_at_final_reopen"] for r in runs} == {m["open_recovery_final_wal"]["records_examined"]}
        m["measured_open_recovery_seconds_median"] = entry["open_recovery_seconds_median"]
        report[key] = m
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=str(ROOT / "benchmarks/results/v1_benchmark.json"))
    ap.add_argument("--out", default=str(ROOT / "benchmarks/results/v1_structural.json"))
    args = ap.parse_args()
    bench = json.loads(Path(args.benchmark).read_text())
    result = {
        "kind": "deterministic structural metrics (derived, not timed); sizes and counts cross-checked against measured values",
        "overwrites_after_load": N_OVERWRITES,
        "deletes_after_overwrites": N_DELETES,
        "scales": build_report(bench),
    }
    Path(args.out).write_text(json.dumps(result, indent=1))
    for key, m in result["scales"].items():
        a, d = m["after_load"], m["after_deletes"]
        print(f"{int(key):>10,}  WAL {a['wal_bytes']:>13,} B -> {d['wal_bytes']:>13,} B  stale {d['stale_records']:,}  "
              f"tombstoned {d['tombstoned_keys']:,}  replay {m['open_recovery_as_benchmarked']['records_examined']:,} rec")


if __name__ == "__main__":
    main()
