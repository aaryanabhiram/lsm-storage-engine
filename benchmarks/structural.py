"""Deterministic structural/storage metrics for the V4 Bloom-filter store.

Usage:  python benchmarks/structural.py [--out benchmarks/results/v4_structural.json]

Nothing here is timed. The workload is V3's (N distinct keys loaded in ascending key order, 1,000 overwrites, 1,000
deletes), so the SSTable layout, WAL sizes, index and filter sizes and the work of a GET follow from the store's flush
rule, the record format, the index rule (one entry per 16 records) and the Bloom rule (ceil(10 * records / 8) bytes,
7 hashes). This script derives them and asserts that they equal what the benchmark measured on disk and in the
instrumented GETs of every run.

The memtable, flush rule and WAL are V2's, so the flush sequence and WAL numbers come from `structural.simulate`;
only the table byte size changes. A V4 table is records + index block + filter block + 60-byte footer.

Derived GET work. Tables are loaded in ascending key order, table t holding keys [t*E, (t+1)*E) (E records per table,
f flushed tables). Bloom answers are not a closed form: they are derived exactly by building each table's filter from
its keys with `bloom.add` (BLAKE2b is deterministic, so the bits are the same in every process) and asking
`bloom.may_contain`. That makes the derivation independent of the store's read path and of the benchmark's
instrumentation, but it does use the same `bloom.py` primitives: it checks the counting, not the hash construction
(tests/test_bloom.py pins that).

  GET hit on key i in table j (i < f*E): tables f-1, f-2, ... are consulted newest first until table j.
     table t > j: Bloom check; "no" skips it. "maybe" is a false positive; the key sorts below the table's first key,
                  so the index rules it out with no file access.
     table j:     the filter must say "maybe"; the index selects a block; one table opened, (i - j*E) % 16 + 1 records.
  GET hit with i >= f*E: answered by the memtable, no table consulted.
  GET miss (key above every stored key): every table is consulted newest first. "no" skips it; a false positive
     opens the table's last block and decodes its E - 16 * (ceil(E / 16) - 1) records and finds nothing.
"""


import argparse
import json
import statistics
from pathlib import Path

from bench import (
    BITS_PER_KEY, DELETE_RECORD_BYTES, ENTRY_PAYLOAD, INDEX_ENTRY_BYTES, NUM_HASHES, N_DELETES,
    N_OVERWRITES, PUT_RECORD_BYTES, ROOT, STRUCT_HITS, STRUCT_MISSES_V4, V0_SCALES,
    WORK_FIELDS, index_bytes, index_entries, key_of, table_filter_bytes, workload
)
from lsm_store import bloom
from lsm_store.sstable import FOOTER_V3_SIZE, FOOTER_V4_SIZE
from lsm_store.store import DEFAULT_INDEX_INTERVAL
from lsm_store.sstable import FOOTER_SIZE

KEY_SIZE = len(key_of(0))
INTERVAL = DEFAULT_INDEX_INTERVAL

# ---- carried over from the earlier stages ----------------------------------------

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


def v3_table_bytes(v2_bytes, records, interval=INTERVAL):
    """A V2 table of `records` records and `v2_bytes` bytes becomes: same records + index block + 44-byte footer."""
    return v2_bytes - FOOTER_SIZE + index_bytes(records, interval) + FOOTER_V3_SIZE


def simulate_v3(records, limit, overwrite_keys, delete_keys_, interval=INTERVAL):
    """structural.simulate with every table's size converted to the V3 format."""
    sim = simulate(records, limit, overwrite_keys, delete_keys_)
    tables = [[c, v3_table_bytes(b, c, interval)] for c, b in sim["tables"]]
    flushes = [{**f, "table_bytes": v3_table_bytes(f["table_bytes"], f["table_records"], interval)}
               for f in sim["flushes"]]
    states = {name: {**st, "sst_bytes": sum(b for _, b in tables[:st["sst_files"]])}
              for name, st in sim["states"].items()}
    return {**sim, "tables": tables, "flushes": flushes, "states": states}


def v4_table_bytes(v3_bytes, records):
    """A V3 table of `records` records and `v3_bytes` bytes becomes: same records and index + filter + 60-byte footer."""
    return v3_bytes - FOOTER_V3_SIZE + table_filter_bytes(records) + FOOTER_V4_SIZE


def simulate_v4(records, limit, overwrite_keys, delete_keys_, interval=INTERVAL):
    """structural.simulate_v3 with every table's size converted to the V4 format."""
    sim = simulate_v3(records, limit, overwrite_keys, delete_keys_, interval)
    tables = [[c, v4_table_bytes(b, c)] for c, b in sim["tables"]]
    flushes = [{**f, "table_bytes": v4_table_bytes(f["table_bytes"], f["table_records"])} for f in sim["flushes"]]
    states = {name: {**st, "sst_bytes": sum(b for _, b in tables[:st["sst_files"]])}
              for name, st in sim["states"].items()}
    return {**sim, "tables": tables, "flushes": flushes, "states": states}


def load_table_filters(records, limit):
    """The filter bytes of every table the load flushes, rebuilt from the keys."""
    e = entries_per_table(limit)
    f = (records - 1) // e
    filters = []
    for t in range(f):
        bits = bytearray(table_filter_bytes(e))
        for i in range(t * e, (t + 1) * e):
            bloom.add(bits, key_of(i), NUM_HASHES)
        filters.append(bytes(bits))
    return filters


def expected_get_work(records, limit, hit_idx, miss_keys, filters=None,
                      interval=INTERVAL):
    """Structural work of GETs issued right after the load. Rows match bench.measure_read_work:
    [category] + WORK_FIELDS."""
    e = entries_per_table(limit)
    f = (records - 1) // e
    if filters is None:
        filters = load_table_filters(records, limit)
    assert len(filters) == f
    last_block_records = e - interval * (index_entries(e, interval) - 1)

    def walk(key, holder, in_block_records):
        """GET of `key`; `holder` is the table that holds it (-1 for none), `in_block_records` the records decoded
        in that table's block."""
        w = dict.fromkeys(WORK_FIELDS, 0)
        for t in range(f - 1, -1, -1):
            w["tables_consulted"] += 1
            w["bloom_checks"] += 1
            maybe = bloom.may_contain(filters[t], key, NUM_HASHES)
            if not maybe:
                assert t != holder, "derived filter rejected a key it holds"
                w["bloom_negative"] += 1
                continue
            w["bloom_maybe"] += 1
            w["index_lookups"] += 1
            if t == holder:
                w["tables_opened"] += 1
                w["blocks_scanned"] += 1
                w["records_decoded"] += in_block_records
                break
            w["false_positives"] += 1
            if holder == -1:  # a miss key sorts above the table: its last block is read in full
                w["tables_opened"] += 1
                w["blocks_scanned"] += 1
                w["records_decoded"] += last_block_records
            else:  # a hit key held by an older table sorts below this newer table's first key
                w["index_rule_outs"] += 1
                w["fp_without_file_access"] += 1
        w["bytes_decoded"] = w["records_decoded"] * PUT_RECORD_BYTES
        return w

    def hit(i):
        if i >= f * e:
            return ["memtable_hit"] + [0] * len(WORK_FIELDS)
        j = i // e
        w = walk(key_of(i), j, (i - j * e) % interval + 1)
        cat = "newest_sstable_hit" if w["tables_consulted"] == 1 else "older_sstable_hit"
        return [cat] + [w[x] for x in WORK_FIELDS]

    def miss(key):
        w = walk(key, -1, 0)
        return ["miss"] + [w[x] for x in WORK_FIELDS]

    return {"hit": [hit(i) for i in hit_idx], "miss": [miss(k) for k in miss_keys]}


def structural_metrics(records, limit, overwrite_keys, delete_keys_,
                       interval=INTERVAL):
    sim = simulate_v4(records, limit, overwrite_keys, delete_keys_, interval)
    tables = sim["tables"]
    st = sim["states"]["after_deletes"]
    distinct_deleted = len(set(delete_keys_))
    live = records - distinct_deleted
    live_payload = live * ENTRY_PAYLOAD
    table_records = [t[0] for t in tables]
    table_bytes = [t[1] for t in tables]
    idx_entries = [index_entries(c, interval) for c in table_records]
    idx_bytes = [index_bytes(c, interval) for c in table_records]
    flt_bytes = [table_filter_bytes(c) for c in table_records]
    logical_payload = records * ENTRY_PAYLOAD + len(overwrite_keys) * ENTRY_PAYLOAD + len(delete_keys_) * KEY_SIZE
    logical_records = (records + len(overwrite_keys)) * PUT_RECORD_BYTES + len(delete_keys_) * DELETE_RECORD_BYTES
    written = sim["wal_appended_bytes"] + sum(table_bytes)
    persistent = sum(table_bytes) + st["wal_bytes"]
    e = entries_per_table(limit)
    return {
        "memtable_limit_bytes": limit,
        "index_interval": interval,
        "bloom_bits_per_key": BITS_PER_KEY,
        "bloom_num_hashes": NUM_HASHES,
        "entries_per_load_table": e,
        "format": {"put_record_bytes": PUT_RECORD_BYTES, "delete_record_bytes": DELETE_RECORD_BYTES,
                   "sstable_footer_bytes": FOOTER_V4_SIZE, "index_entry_bytes": INDEX_ENTRY_BYTES,
                   "user_bytes_per_put": ENTRY_PAYLOAD},
        "flushes": {"total": len(tables), "during_load": (records - 1) // e,
                    "during_overwrites": sum(1 for f in sim["flushes"] if f["phase"] == "put_overwrite"),
                    "during_deletes": sum(1 for f in sim["flushes"] if f["phase"] == "delete"),
                    "records_flushed_total": sum(table_records)},
        "sstables": {"count": len(tables), "total_records": sum(table_records), "total_bytes": sum(table_bytes),
                     "records": table_records, "bytes": table_bytes},
        "index": {"entries_per_load_table": index_entries(e, interval),
                  "bytes_per_load_table": index_bytes(e, interval),
                  "last_block_records_per_load_table": e - interval * (index_entries(e, interval) - 1),
                  "entries_total": sum(idx_entries), "bytes_total": sum(idx_bytes),
                  "entries": idx_entries, "bytes": idx_bytes,
                  "share_of_sstable_bytes": sum(idx_bytes) / sum(table_bytes) if table_bytes else None},
        "filter": {"bytes_per_load_table": table_filter_bytes(e),
                   "bits_per_record_load_table": table_filter_bytes(e) * 8 / e,
                   "expected_fpr_load_table": bloom.expected_false_positive_rate(e, table_filter_bytes(e), NUM_HASHES),
                   "bytes_total": sum(flt_bytes), "bytes": flt_bytes,
                   "share_of_sstable_bytes": sum(flt_bytes) / sum(table_bytes) if table_bytes else None,
                   "bytes_per_record": sum(flt_bytes) / sum(table_records) if table_records else None,
                   "footer_bytes_total": FOOTER_V4_SIZE * len(tables)},
        "wal": {"segments_at_end": 1, "bytes_at_end": st["wal_bytes"], "records_at_end": st["wal_records"],
                "bytes_appended_total": sim["wal_appended_bytes"]},
        "states": sim["states"],
        "space": {"live_keys": live, "tombstoned_keys": distinct_deleted, "live_user_payload_bytes": live_payload,
                  "sstable_bytes": sum(table_bytes), "surviving_wal_bytes": st["wal_bytes"],
                  "total_persistent_bytes": persistent,
                  "space_amplification": persistent / live_payload,
                  "physical_records": sum(table_records) + st["wal_records"],
                  "stale_records": sum(table_records) + st["wal_records"] - records},
        "writes": {"logical_payload_bytes": logical_payload, "logical_record_bytes": logical_records,
                   "wal_bytes_appended": sim["wal_appended_bytes"], "sstable_bytes_written": sum(table_bytes),
                   "bytes_per_payload_byte": written / logical_payload, "rewrite_factor": written / logical_records},
        "_simulation": sim,
    }


def summarize_work(rows):
    out = {}
    for cat in sorted({r[0] for r in rows}):
        sel = [r for r in rows if r[0] == cat]
        out[cat] = {"gets": len(sel), "fraction": len(sel) / len(rows),
                    **{f"mean_{n}": statistics.fmean(r[i + 1] for r in sel) for i, n in enumerate(WORK_FIELDS)},
                    "max_records_decoded": max(r[WORK_FIELDS.index("records_decoded") + 1] for r in sel)}
    return out


def build_report(result):
    """Derive per scale and assert agreement with every measured run."""
    report = {}
    for key in sorted(result["scales"], key=int):
        entry = result["scales"][key]
        records, limit = entry["records"], entry["memtable_limit_bytes"]
        value_rng, hit_idx, overwrites, miss_keys, del_keys = workload(
            records, entry["gets_hit_per_run"], entry["gets_miss_per_run"], V0_SCALES[records]["gets_per_kind"])
        m = structural_metrics(records, limit, [k for k, _ in overwrites], del_keys)
        sim = m.pop("_simulation")
        filters = load_table_filters(records, limit)
        expect_work = expected_get_work(records, limit, hit_idx[:STRUCT_HITS],
                                        miss_keys[:min(entry["gets_miss_per_run"], STRUCT_MISSES_V4)], filters)
        n_load = sim["states"]["after_load"]["sst_files"]
        e = m["entries_per_load_table"]
        for run in entry["runs"]:
            assert run["index_interval"] == INTERVAL and run["bloom_bits_per_key"] == BITS_PER_KEY
            assert run["bloom_num_hashes"] == NUM_HASHES
            assert run["entries_per_table"] == e
            assert run["state"]["after_load"] == {k: sim["states"]["after_load"][k] for k in
                                                  ("wal_files", "wal_bytes", "sst_files", "sst_bytes")}
            for name in ("after_overwrites", "after_deletes"):
                assert run["state"][name] == {k: sim["states"][name][k] for k in
                                              ("wal_files", "wal_bytes", "sst_files", "sst_bytes")}, name
            assert run["state"]["final_closed"] == run["state"]["after_deletes"]
            assert run["tables_final"] == sim["tables"], "SSTable layout differs from the derived layout"
            assert [c for c, _ in run["index_tables"]] == m["index"]["entries"], "index entry counts differ"
            assert [b for _, b in run["index_tables"]] == m["index"]["bytes"], "index block sizes differ"
            assert [b for _, b in run["filter_tables"]] == m["filter"]["bytes"], "filter block sizes differ"
            assert all(k == NUM_HASHES for k, _ in run["filter_tables"]), "a table has an unexpected hash count"
            oc = run["open_components"]
            assert oc["tables"] == n_load
            assert oc["index_bytes_read"] == sum(m["index"]["bytes"][:n_load])
            assert oc["index_entries_loaded"] == sum(m["index"]["entries"][:n_load])
            assert oc["filter_bytes_read"] == sum(m["filter"]["bytes"][:n_load])
            assert oc["footer_bytes_read"] == FOOTER_V4_SIZE * n_load
            tm = run["table_memory"]
            assert tm["index_entries"] == m["index"]["entries_total"]
            assert tm["bloom_block_bytes"] == m["filter"]["bytes_total"]
            measured = [dict(phase=ph, **{k: f[k] for k in ("op_index", "memtable_entries", "memtable_bytes",
                                                            "table_records", "table_bytes", "wal_bytes_before")})
                        for ph in ("put_load", "put_overwrite", "delete") for f in run["flushes"][ph]]
            assert measured == sim["flushes"], "flush sequence differs from the derived sequence"
            assert all(f["index_interval"] == INTERVAL and f["bloom_bits_per_key"] == BITS_PER_KEY
                       for ph in run["flushes"] for f in run["flushes"][ph]), \
                "a flush wrote a table with different index/Bloom parameters"
            assert all(f["wal_files_before"] == 1 and f["wal_files_after"] == 1
                       for ph in run["flushes"] for f in run["flushes"][ph]), "WAL segments were not reclaimed"
            assert run["final_memtable_entries"] == sim["states"]["after_deletes"]["memtable_entries"]
            assert run["final_memtable_payload_bytes"] == sim["states"]["after_deletes"]["memtable_payload_bytes"]
            assert run["read_work"]["hit"] == expect_work["hit"], "measured hit work differs from derived"
            assert run["read_work"]["miss"] == expect_work["miss"], "measured miss work differs from derived"
            measured_appended = sum(f["wal_bytes_before"] for ph in run["flushes"] for f in run["flushes"][ph]) \
                + run["state"]["final_closed"]["wal_bytes"]
            assert measured_appended == m["wal"]["bytes_appended_total"], "WAL bytes appended differ"
            assert run["state"]["final_closed"]["sst_bytes"] == m["sstables"]["total_bytes"]
            val = run["bloom_validation"]
            assert val["false_negatives"] == 0 and val["stored_filters_equal_rebuilt"]
            assert val["keys_checked"] == sum(m["sstables"]["records"]) + run["final_memtable_entries"], \
                "the validation scan did not cover every key in every table"
            assert 0 < val["tombstone_keys_checked"] <= N_DELETES
            assert val["tables"] == len(sim["tables"]) + 1
        m["get_work_sample"] = {"hit": summarize_work(expect_work["hit"]), "miss": summarize_work(expect_work["miss"])}
        m["measured_runs_checked"] = len(entry["runs"])
        report[key] = m
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=str(ROOT / "benchmarks/results/v4_benchmark.json"))
    ap.add_argument("--out", default=str(ROOT / "benchmarks/results/v4_structural.json"))
    args = ap.parse_args()
    bench = json.loads(Path(args.benchmark).read_text())
    result = {
        "kind": "deterministic structural metrics (derived, not timed); layout, index and filter sizes, flush "
                "sequence, WAL sizes and GET work (including every Bloom answer, from filters rebuilt from the keys) "
                "asserted equal to the values measured in every run",
        "index_interval": INTERVAL,
        "bloom_bits_per_key": BITS_PER_KEY,
        "bloom_num_hashes": NUM_HASHES,
        "overwrites_after_load": N_OVERWRITES,
        "deletes_after_overwrites": N_DELETES,
        "scales": build_report(bench),
    }
    Path(args.out).write_text(json.dumps(result, indent=1))
    for key, m in result["scales"].items():
        print(f"{int(key):>10,}  tables {m['sstables']['count']:>4}  SST {m['sstables']['total_bytes']:>13,} B  "
              f"index {m['index']['bytes_total']:>11,} B  filter {m['filter']['bytes_total']:>11,} B "
              f"({(m['filter']['share_of_sstable_bytes'] or 0) * 100:.2f}%)  space amp {m['space']['space_amplification']:.4f}")


if __name__ == "__main__":
    main()
