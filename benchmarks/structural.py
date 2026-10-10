"""Deterministic structural/storage metrics for the V2 SSTable store.

Usage:  python benchmarks/structural.py [--out benchmarks/results/v2_structural.json]

Nothing here is timed. The V2 workload is deterministic (N distinct keys loaded in ascending key order, 1,000
overwrites of existing keys, 1,000 deletes of existing keys; see docs/benchmarks.md), so the SSTable layout, the WAL
sizes and the work of a GET follow from the store's flush rule and the record format. This script derives them and
asserts that they equal what the benchmark measured on disk in every run. "Records examined" and "bytes scanned"
are application-level work counted in the record reader, not physical storage I/O.

How the derivation follows the store: a write first flushes if the memtable's payload bytes (key + value; a tombstone
counts its key) have reached the limit, so with 111 payload bytes per entry the load flushes every
E = ceil(limit / 111) entries. The overwrite and delete phases are replayed operation by operation against the same
rule (no I/O).

Write-side ratios, with exact definitions (measured at the end of the delete phase, before close):
  logical_payload_bytes  key + value bytes of every mutation (a delete counts its key)
  logical_record_bytes   the bytes V1 would append for the same mutations: one encoded record each
  wal_bytes_appended     record bytes appended to WAL segments (all segments ever written)
  sstable_bytes_written  every byte of every SSTable (records + 28-byte footers); tables are never rewritten
  bytes_per_payload_byte (wal_bytes_appended + sstable_bytes_written) / logical_payload_bytes
                         includes the 13-byte record header overhead
  rewrite_factor         (wal_bytes_appended + sstable_bytes_written) / logical_record_bytes
                         excludes header overhead: how many times a record's bytes reach a file. V2 writes a surviving
                         entry once to the WAL and once to a table; nothing rewrites them later.
                         Three effects move it away from exactly 2: footers add bytes, records still in the memtable
                         have only reached the WAL, and overwrites that coalesce in the memtable reach a table once.
"""

import argparse
import json
import statistics
from pathlib import Path

from bench import (
    DELETE_RECORD_BYTES, ENTRY_PAYLOAD, N_DELETES, N_OVERWRITES, PUT_RECORD_BYTES, ROOT,
    STRUCT_HITS, STRUCT_MISSES, V0_SCALES, key_of, workload
)
from lsm_store.sstable import FOOTER_SIZE

KEY_SIZE = len(key_of(0))
TOMBSTONE_PAYLOAD = KEY_SIZE


def entries_per_table(limit):
    return -(-limit // ENTRY_PAYLOAD)


def simulate(records, limit, overwrite_keys, delete_keys_):
    """Replay the store's memtable/flush rule on the benchmark's mutations. Returns the SSTables written (in order),
    one record per flush, and the WAL/memtable state after each phase."""
    e = entries_per_table(limit)
    flushed_in_load = (records - 1) // e
    tables = [[e, e * PUT_RECORD_BYTES + FOOTER_SIZE]] * flushed_in_load
    flushes = [{"phase": "put_load", "op_index": (i + 1) * e, "memtable_entries": e,
                "memtable_bytes": e * ENTRY_PAYLOAD, "table_records": e,
                "table_bytes": e * PUT_RECORD_BYTES + FOOTER_SIZE, "wal_bytes_before": e * PUT_RECORD_BYTES}
               for i in range(flushed_in_load)]
    # Memtable after the load: the keys written since the last flush, all live values.
    mem = {key_of(i): False for i in range(flushed_in_load * e, records)}  # key -> is_tombstone
    wal_bytes = len(mem) * PUT_RECORD_BYTES
    wal_records = len(mem)
    wal_appended = records * PUT_RECORD_BYTES
    states = {"after_load": None}

    def snapshot():
        return {"sst_files": len(tables), "sst_bytes": sum(t[1] for t in tables), "wal_files": 1,
                "wal_bytes": wal_bytes, "wal_records": wal_records, "memtable_entries": len(mem),
                "memtable_payload_bytes": sum(TOMBSTONE_PAYLOAD if t else ENTRY_PAYLOAD for t in mem.values())}

    states["after_load"] = snapshot()
    state_bytes = [sum(TOMBSTONE_PAYLOAD if t else ENTRY_PAYLOAD for t in mem.values())]  # memtable payload bytes

    def apply(phase, index, key, tombstone):
        nonlocal wal_bytes, wal_records, wal_appended, mem
        mem_bytes = state_bytes[0]
        if mem_bytes >= limit:
            t_bytes = sum(24 if t else PUT_RECORD_BYTES for t in mem.values()) + FOOTER_SIZE
            tables.append([len(mem), t_bytes])
            flushes.append({"phase": phase, "op_index": index, "memtable_entries": len(mem),
                            "memtable_bytes": mem_bytes, "table_records": len(mem), "table_bytes": t_bytes,
                            "wal_bytes_before": wal_bytes})
            mem = {}
            wal_bytes = wal_records = 0
            state_bytes[0] = 0
        if key in mem:
            state_bytes[0] -= TOMBSTONE_PAYLOAD if mem[key] else ENTRY_PAYLOAD
        mem[key] = tombstone
        state_bytes[0] += TOMBSTONE_PAYLOAD if tombstone else ENTRY_PAYLOAD
        rec = DELETE_RECORD_BYTES if tombstone else PUT_RECORD_BYTES
        wal_bytes += rec
        wal_records += 1
        wal_appended += rec

    for n, k in enumerate(overwrite_keys):
        apply("put_overwrite", n, k, False)
    states["after_overwrites"] = snapshot()
    for n, k in enumerate(delete_keys_):
        apply("delete", n, k, True)
    states["after_deletes"] = snapshot()
    return {"tables": tables, "flushes": flushes, "states": states, "wal_appended_bytes": wal_appended,
            "final_memtable_tombstones": sum(1 for t in mem.values() if t)}


def get_work(records, limit, hit_idx, miss_count):
    """Structural work of GETs issued right after the load: derived per key. Keys were loaded in ascending order,
    so each table holds a contiguous key range and every newer table is skipped after reading one record."""
    e = entries_per_table(limit)
    f = (records - 1) // e

    def hit(i):
        if i >= f * e:
            return ["memtable_hit", 0, 0, 0]
        j = i // e
        tables = f - j
        recs = (f - 1 - j) + (i - j * e + 1)
        return ["newest_sstable_hit" if tables == 1 else "older_sstable_hit", tables, recs, recs * PUT_RECORD_BYTES]

    miss = ["miss", f, f * e, f * e * PUT_RECORD_BYTES]
    return {"hit": [hit(i) for i in hit_idx], "miss": [miss] * miss_count}


def summarize_work(rows):
    out = {}
    for cat in sorted({r[0] for r in rows}):
        sel = [r for r in rows if r[0] == cat]
        out[cat] = {"gets": len(sel), "fraction": len(sel) / len(rows),
                    "mean_tables_consulted": statistics.fmean(r[1] for r in sel),
                    "mean_records_examined": statistics.fmean(r[2] for r in sel),
                    "mean_bytes_scanned": statistics.fmean(r[3] for r in sel),
                    "max_records_examined": max(r[2] for r in sel)}
    return out


def structural_metrics(records, limit, hit_idx, miss_count, overwrite_keys,
                       delete_keys_):
    sim = simulate(records, limit, overwrite_keys, delete_keys_)
    tables = sim["tables"]
    st = sim["states"]["after_deletes"]
    distinct_deleted = len(set(delete_keys_))
    live = records - distinct_deleted
    live_payload = live * ENTRY_PAYLOAD
    table_records = [t[0] for t in tables]
    table_bytes = [t[1] for t in tables]
    logical_payload = records * ENTRY_PAYLOAD + len(overwrite_keys) * ENTRY_PAYLOAD + len(delete_keys_) * KEY_SIZE
    logical_records = (records + len(overwrite_keys)) * PUT_RECORD_BYTES + len(delete_keys_) * DELETE_RECORD_BYTES
    written = sim["wal_appended_bytes"] + sum(table_bytes)
    persistent = sum(table_bytes) + st["wal_bytes"]
    flush_payload = [f["memtable_bytes"] for f in sim["flushes"]]
    e = entries_per_table(limit)
    return {
        "memtable_limit_bytes": limit,
        "entries_per_load_table": e,
        "format": {"put_record_bytes": PUT_RECORD_BYTES, "delete_record_bytes": DELETE_RECORD_BYTES,
                   "sstable_footer_bytes": FOOTER_SIZE, "user_bytes_per_put": ENTRY_PAYLOAD,
                   "bytes_appended_per_user_byte_put": PUT_RECORD_BYTES / ENTRY_PAYLOAD},
        "flushes": {"total": len(tables), "during_load": (records - 1) // e,
                    "during_overwrites": sum(1 for f in sim["flushes"] if f["phase"] == "put_overwrite"),
                    "during_deletes": sum(1 for f in sim["flushes"] if f["phase"] == "delete"),
                    "records_flushed_total": sum(table_records),
                    "payload_bytes_flushed_total": sum(flush_payload),
                    "footer_bytes_total": FOOTER_SIZE * len(tables),
                    "records_per_flush_min_max_mean": [min(table_records, default=0), max(table_records, default=0),
                                                       statistics.fmean(table_records) if table_records else 0]},
        "sstables": {"count": len(tables), "total_records": sum(table_records), "total_bytes": sum(table_bytes),
                     "bytes_min_max_mean": [min(table_bytes, default=0), max(table_bytes, default=0),
                                            statistics.fmean(table_bytes) if table_bytes else 0],
                     "records": table_records, "bytes": table_bytes},
        "wal": {"segments_at_end": 1, "bytes_at_end": st["wal_bytes"], "records_at_end": st["wal_records"],
                "bytes_appended_total": sim["wal_appended_bytes"],
                "bytes_before_each_flush_min_max": [min((f["wal_bytes_before"] for f in sim["flushes"]), default=0),
                                                    max((f["wal_bytes_before"] for f in sim["flushes"]), default=0)],
                "bytes_after_each_flush": 0},
        "states": sim["states"],
        "space": {"live_keys": live, "tombstoned_keys": distinct_deleted, "live_user_payload_bytes": live_payload,
                  "sstable_bytes": sum(table_bytes), "surviving_wal_bytes": st["wal_bytes"],
                  "total_persistent_bytes": persistent,
                  "space_amplification": persistent / live_payload,
                  "physical_records": sum(table_records) + st["wal_records"],
                  "stale_records": sum(table_records) + st["wal_records"] - records,
                  "record_format_overhead_factor": PUT_RECORD_BYTES / ENTRY_PAYLOAD},
        "writes": {"logical_payload_bytes": logical_payload, "logical_record_bytes": logical_records,
                   "wal_bytes_appended": sim["wal_appended_bytes"], "sstable_bytes_written": sum(table_bytes),
                   "bytes_per_payload_byte": written / logical_payload, "rewrite_factor": written / logical_records},
        "get_work_timed_workload": {"hit": summarize_work(get_work(records, limit, hit_idx, 0)["hit"]),
                                    "miss": summarize_work(get_work(records, limit, [], 1)["miss"])},
        "_simulation": sim,
    }


def build_report(result):
    """Derive per scale and assert agreement with every measured run."""
    report = {}
    for key in sorted(result["scales"], key=int):
        entry = result["scales"][key]
        records, limit = entry["records"], entry["memtable_limit_bytes"]
        value_rng, hit_idx, overwrites, miss_keys, del_keys = workload(
            records, entry["gets_hit_per_run"], entry["gets_miss_per_run"], V0_SCALES[records]["gets_per_kind"])
        m = structural_metrics(records, limit, hit_idx, entry["gets_miss_per_run"], [k for k, _ in overwrites], del_keys)
        sim = m.pop("_simulation")
        expect_work = get_work(records, limit, hit_idx[:STRUCT_HITS], STRUCT_MISSES)
        for run in entry["runs"]:
            assert run["entries_per_table"] == m["entries_per_load_table"]
            assert run["state"]["after_load"] == {k: sim["states"]["after_load"][k] for k in
                                                  ("wal_files", "wal_bytes", "sst_files", "sst_bytes")}
            for name in ("after_overwrites", "after_deletes"):
                assert run["state"][name] == {k: sim["states"][name][k] for k in
                                              ("wal_files", "wal_bytes", "sst_files", "sst_bytes")}, name
            assert run["state"]["final_closed"] == run["state"]["after_deletes"]
            assert run["tables_final"] == sim["tables"], "SSTable layout differs from the derived layout"
            measured = [dict(phase=ph, **{k: f[k] for k in ("op_index", "memtable_entries", "memtable_bytes",
                                                            "table_records", "table_bytes", "wal_bytes_before")})
                        for ph in ("put_load", "put_overwrite", "delete") for f in run["flushes"][ph]]
            assert measured == sim["flushes"], "flush sequence differs from the derived sequence"
            assert all(f["wal_files_before"] == 1 and f["wal_files_after"] == 1
                       for ph in run["flushes"] for f in run["flushes"][ph]), "WAL segments were not reclaimed"
            assert run["final_memtable_entries"] == sim["states"]["after_deletes"]["memtable_entries"]
            assert run["final_memtable_payload_bytes"] == sim["states"]["after_deletes"]["memtable_payload_bytes"]
            assert run["read_work"]["hit"] == expect_work["hit"], "measured hit work differs from derived"
            assert run["read_work"]["miss"] == expect_work["miss"], "measured miss work differs from derived"
            # Total WAL bytes appended, measured from the files: bytes in each segment when its flush ran, plus the
            # surviving segment.
            measured_appended = sum(f["wal_bytes_before"] for ph in run["flushes"] for f in run["flushes"][ph]) \
                + run["state"]["final_closed"]["wal_bytes"]
            assert measured_appended == m["wal"]["bytes_appended_total"], "WAL bytes appended differ"
            assert run["state"]["final_closed"]["sst_bytes"] == m["sstables"]["total_bytes"]
        m["measured_runs_checked"] = len(entry["runs"])
        report[key] = m
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=str(ROOT / "benchmarks/results/v2_benchmark.json"))
    ap.add_argument("--out", default=str(ROOT / "benchmarks/results/v2_structural.json"))
    args = ap.parse_args()
    bench = json.loads(Path(args.benchmark).read_text())
    result = {
        "kind": "deterministic structural metrics (derived, not timed); layout, flush sequence, WAL sizes and GET work "
                "asserted equal to the values measured in every run",
        "overwrites_after_load": N_OVERWRITES,
        "deletes_after_overwrites": N_DELETES,
        "scales": build_report(bench),
    }
    Path(args.out).write_text(json.dumps(result, indent=1))
    for key, m in result["scales"].items():
        print(f"{int(key):>10,}  tables {m['sstables']['count']:>4}  SST {m['sstables']['total_bytes']:>13,} B  "
              f"WAL {m['wal']['bytes_at_end']:>9,} B  space amp {m['space']['space_amplification']:.4f}  "
              f"rewrite factor {m['writes']['rewrite_factor']:.4f}")


if __name__ == "__main__":
    main()
