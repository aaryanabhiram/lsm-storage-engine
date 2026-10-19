"""Derived (not timed) cache metrics for the V5 benchmark, asserted equal to what the runs measured.

Usage:  python benchmarks/structural.py [--benchmark benchmarks/results/v5_benchmark.json]
                                           [--out benchmarks/results/v5_structural.json]

Nothing here uses `lsm_store.blockcache` or the benchmark's instrumentation. The block accesses of a workload are derived
from the table geometry and the Bloom filters (rebuilt from the keys, as in structural), and replayed through
`simulate_lru`, a separate implementation of the cache's rules (byte capacity, LRU, an oversize block is never admitted).
The derived hits, misses, insertions, evictions, entries and bytes, the physical block reads and bytes read, and the
decode work must equal the measured values of every run.

Standard workload. The timed GETs run from an empty cache: the hit keys in order, then the miss keys.
  hit key i < f*E, held by table j = i // E:  one block access, block (j, (i - j*E) // 16); records decoded: position in
     the block + 1. Newer tables are consulted first; a Bloom "maybe" on them is ruled out by the index (the key sorts
     below the table's first key), so no block access. i >= f*E is in the memtable: no access.
  miss key (above every stored key): every table is consulted; a Bloom "maybe" selects the table's last block, which is
     read in full (E - 16 * (ceil(E/16) - 1) records) and found not to hold the key.
Locality workload. Every GET is for a flushed key: one block access, `idx` -> block (idx // E, (idx % E) // 16).
"""

import argparse
import json
from pathlib import Path

from bench import (
    BITS_PER_KEY, BLOCK_RECORDS, NUM_HASHES, RECORD_BYTES, ROOT, V0_SCALES, key_of,
    locality_plan, workload
)
from lsm_store import bloom
from bench import ENTRY_PAYLOAD, PUT_RECORD_BYTES, WORK_FIELDS, index_entries, table_filter_bytes
from lsm_store.store import DEFAULT_INDEX_INTERVAL

BLOCK_BYTES = BLOCK_RECORDS * RECORD_BYTES

# ---- carried over from the earlier stages ----------------------------------------

INTERVAL = DEFAULT_INDEX_INTERVAL


def entries_per_table(limit):
    return -(-limit // ENTRY_PAYLOAD)


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


def block_records(e, block):
    """Records in block `block` of a table of `e` records."""
    return min(BLOCK_RECORDS, e - block * BLOCK_RECORDS)


def simulate_lru(accesses, size_of, capacity):
    """Byte-bounded LRU over `accesses` (block ids), written independently of lsm_store.blockcache: a plain dict whose
    insertion order is the recency order (first = least recently used). `size_of(block)` is a block's bytes."""
    d = {}
    used = hits = misses = evictions = insertions = bytes_read = 0
    miss_flags = []
    for blk in accesses:
        if blk in d:
            hits += 1
            d[blk] = d.pop(blk)
            miss_flags.append(False)
            continue
        misses += 1
        size = size_of(blk)
        bytes_read += size
        miss_flags.append(True)
        if size > capacity:
            continue
        while used + size > capacity:
            victim = next(iter(d))
            used -= d.pop(victim)
            evictions += 1
        d[blk] = size
        used += size
        insertions += 1
    lookups = hits + misses
    return {"hits": hits, "misses": misses, "insertions": insertions, "evictions": evictions, "bytes": used,
            "entries": len(d), "hit_rate": hits / lookups if lookups else 0.0, "bytes_read": bytes_read,
            "miss_flags": miss_flags}


def standard_accesses(records, limit, hit_idx, miss_keys, filters):
    """Block accesses (table, block) of the standard GET workload, and the decode work of each access."""
    e = entries_per_table(limit)
    f = (records - 1) // e
    bpt = -(-e // BLOCK_RECORDS)
    last_records = block_records(e, bpt - 1)
    hit_acc, hit_dec = [], []
    for i in hit_idx:
        if i < f * e:
            j = i // e
            hit_acc.append((j, (i - j * e) // BLOCK_RECORDS))
            hit_dec.append((i - j * e) % BLOCK_RECORDS + 1)
    miss_acc, miss_dec = [], []
    for key in miss_keys:
        for t in range(f - 1, -1, -1):
            if bloom.may_contain(filters[t], key, NUM_HASHES):
                miss_acc.append((t, bpt - 1))
                miss_dec.append(last_records)
    return {"hit": hit_acc, "miss": miss_acc, "hit_decoded": hit_dec, "miss_decoded": miss_dec,
            "entries_per_table": e, "tables": f, "blocks_per_table": bpt}


def standard_expected(records, limit, n_hits, n_misses, v0_gets, capacity):
    _, hit_idx, _, miss_keys, _ = workload(records, n_hits, n_misses, v0_gets)
    filters = load_table_filters(records, limit)
    acc = standard_accesses(records, limit, hit_idx, miss_keys, filters)
    e, bpt = acc["entries_per_table"], acc["blocks_per_table"]
    size_of = lambda blk: block_records(e, blk[1]) * RECORD_BYTES  # noqa: E731
    seq = acc["hit"] + acc["miss"]
    cached = simulate_lru(seq, size_of, capacity)
    uncached = simulate_lru(seq, size_of, 0)
    flags = cached.pop("miss_flags")
    uncached.pop("miss_flags")
    n_h = len(acc["hit"])
    work = expected_get_work(records, limit, hit_idx, miss_keys, filters)
    totals = {}
    for kind in ("hit", "miss"):
        rows = work[kind]
        names = ("tables_consulted", "bloom_checks", "bloom_negative", "bloom_maybe", "false_positives",
                 "index_lookups", "index_rule_outs", "records_decoded", "bytes_decoded")
        from bench import WORK_FIELDS

        totals[kind] = {n: sum(r[WORK_FIELDS.index(n) + 1] for r in rows) for n in names}
        totals[kind]["blocks_scanned"] = sum(r[WORK_FIELDS.index("tables_opened") + 1] for r in rows)
    assert totals["hit"]["blocks_scanned"] == len(acc["hit"]) and totals["miss"]["blocks_scanned"] == len(acc["miss"])
    assert totals["hit"]["records_decoded"] == sum(acc["hit_decoded"])
    assert totals["miss"]["records_decoded"] == sum(acc["miss_decoded"])
    return {"cache": cached, "uncached": uncached, "accesses": {"hit": len(acc["hit"]), "miss": len(acc["miss"])},
            "misses_per_phase": {"hit": sum(flags[:n_h]), "miss": sum(flags[n_h:])},
            "work": totals, "tables": acc["tables"], "entries_per_table": e}


def check_standard_scale(entry, v0_gets=None):
    records, limit = entry["records"], entry["memtable_limit_bytes"]
    n_hits, n_misses = entry["gets_hit_per_run"], entry["gets_miss_per_run"]
    capacity = entry["block_cache_bytes"]
    exp = standard_expected(records, limit, n_hits, n_misses, v0_gets or V0_SCALES[records]["gets_per_kind"],
                            capacity)
    c = exp["cache"]
    for run in entry["runs"]:
        acct = run["cache"]
        live, cached, uncached = acct["cache_after_timed_gets"], acct["cached"], acct["uncached"]
        for k in ("hits", "misses", "insertions", "evictions", "bytes", "entries"):
            assert live[k] == c[k], f"{records}: derived cache {k} {c[k]} != measured {live[k]}"
        assert live["hit_rate"] == c["hit_rate"]
        assert cached["cache"] == live
        for name, derived_reads, derived_bytes in (("cached", c["misses"], c["bytes_read"]),
                                                   ("uncached", exp["uncached"]["misses"],
                                                    exp["uncached"]["bytes_read"])):
            t = acct[name]["totals"]
            assert t["block_reads"] == t["open_table_calls"] == derived_reads, f"{records}: {name} block reads"
            assert t["bytes_read"] == derived_bytes, f"{records}: {name} bytes read"
        for name in ("cached", "uncached"):
            t = acct[name]["totals"]
            for field in ("tables_consulted", "bloom_checks", "bloom_negative", "bloom_maybe", "false_positives",
                          "index_lookups", "index_rule_outs", "blocks_scanned", "records_decoded", "bytes_decoded"):
                want = exp["work"]["hit"][field] + exp["work"]["miss"][field]
                assert t[field] == want, f"{records}: {name} {field} {t[field]} != derived {want}"
        for phase in ("hit", "miss"):
            assert cached["phases"][phase]["block_reads"] == exp["misses_per_phase"][phase]
        assert uncached["cache"]["hits"] == 0
    return {"records": records, "tables": exp["tables"], "entries_per_table": exp["entries_per_table"],
            "block_accesses": exp["accesses"], "cache": c, "uncached_block_reads": exp["uncached"]["misses"],
            "uncached_bytes_read": exp["uncached"]["bytes_read"], "misses_per_phase": exp["misses_per_phase"],
            "work": exp["work"], "runs_checked": len(entry["runs"])}


def locality_expected(loc):
    """Derived cache behaviour of every configuration of the locality workload."""
    p = loc["params"]
    limit = loc["memtable_limit_bytes"]
    plan = locality_plan(p["records"], limit, p["ops"], p["hot_blocks"], p["hot_fraction"], p["seed"])
    g = plan["geometry"]
    e, bpt, f = g["entries_per_table"], g["blocks_per_table"], g["tables"]
    seq = plan["block_of"]  # (table, block) packed as table * bpt + block
    size_of = lambda blk: block_records(e, blk % bpt) * RECORD_BYTES  # noqa: E731
    filters = load_table_filters(p["records"], limit)
    work = dict.fromkeys(("tables_consulted", "bloom_checks", "bloom_negative", "bloom_maybe", "false_positives",
                          "index_lookups", "index_rule_outs", "blocks_scanned", "records_decoded", "bytes_decoded"), 0)
    for i in plan["idx"]:
        j = i // e
        key = key_of(i)
        for t in range(f - 1, j - 1, -1):
            work["tables_consulted"] += 1
            work["bloom_checks"] += 1
            if not bloom.may_contain(filters[t], key, NUM_HASHES):
                assert t != j
                work["bloom_negative"] += 1
                continue
            work["bloom_maybe"] += 1
            work["index_lookups"] += 1
            if t == j:
                work["blocks_scanned"] += 1
                work["records_decoded"] += (i - j * e) % BLOCK_RECORDS + 1
            else:
                work["false_positives"] += 1
                work["index_rule_outs"] += 1
    work["bytes_decoded"] = work["records_decoded"] * RECORD_BYTES
    out = {"work": work, "configs": {}}
    for name, capacity in loc["configs"]:
        sim = simulate_lru(seq, size_of, 0 if capacity is None else capacity)
        sim.pop("miss_flags")
        sim["capacity_bytes"] = capacity
        out["configs"][name] = sim
    return out


def check_locality(loc):
    exp = locality_expected(loc)
    ops = loc["params"]["ops"]
    for name, capacity in loc["configs"]:
        sim = exp["configs"][name]
        for run in loc["runs"]:
            c = run["configs"][name]
            w = c["work"]
            for field, want in exp["work"].items():
                assert w[field] == want, f"locality {name}: {field} {w[field]} != derived {want}"
            assert w["block_reads"] == w["open_table_calls"] == sim["misses"], f"locality {name}: physical reads"
            assert w["bytes_read"] == sim["bytes_read"], f"locality {name}: bytes read"
            if capacity is None:
                assert c["timed_cache"] is None
                continue
            t = c["timed_cache"]
            for k in ("hits", "misses", "insertions", "evictions", "bytes", "entries"):
                assert t[k] == sim[k], f"locality {name}: cache {k} {t[k]} != derived {sim[k]}"
            assert t["hits"] + t["misses"] == ops
    return exp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", default=str(ROOT / "benchmarks/results/v5_benchmark.json"))
    ap.add_argument("--out", default=str(ROOT / "benchmarks/results/v5_structural.json"))
    args = ap.parse_args()
    bench = json.loads(Path(args.benchmark).read_text())
    result = {
        "kind": "derived cache metrics (not timed): block accesses derived from table geometry and rebuilt Bloom "
                "filters, replayed through an independent LRU model; asserted equal to the measured cache counters, "
                "physical block reads, bytes read and decode work of every run",
        "block_records": BLOCK_RECORDS, "block_bytes_full": BLOCK_BYTES, "bloom_bits_per_key": BITS_PER_KEY,
        "bloom_num_hashes": NUM_HASHES, "standard": {}, "locality": None,
    }
    for key in sorted(bench["scales"], key=int):
        result["standard"][key] = check_standard_scale(bench["scales"][key])
        c = result["standard"][key]["cache"]
        print(f"standard {int(key):>10,}  hits {c['hits']:>6}  misses {c['misses']:>6}  evictions {c['evictions']}  "
              f"entries {c['entries']}  OK", flush=True)
    if bench.get("locality"):
        loc = check_locality(bench["locality"])
        result["locality"] = {"work": loc["work"], "configs": loc["configs"]}
        for name, sim in loc["configs"].items():
            print(f"locality {name:<10} hits {sim['hits']:>7}  misses {sim['misses']:>7}  evictions {sim['evictions']:>7}  OK")
    Path(args.out).write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
