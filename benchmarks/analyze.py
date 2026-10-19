"""Analysis of a V5 benchmark result: pure functions of the stored runs and the V4 reference result.

`bench.py --combine/--analyze-only` stores their output in the result JSON; tests recompute it and compare. Kept apart
from bench.py so that editing the analysis does not change the hashed measurement sources.

Standard workload: V5 (8 MiB cache) against the V4 reference `v4_benchmark.json`. Locality workload: configurations
against each other (and against `KVStore`, measured in the same runs).
"""

import json
import statistics

from bench import ROOT, metric_range, pct_change, write_row

RESULTS = ROOT / "benchmarks/results"
PCTS = ("p50_us", "p95_us", "p99_us")
LAYOUT_FIELDS = ("wal_files", "wal_bytes", "sst_files", "sst_bytes")


def v4_reference():
    return json.loads((RESULTS / "v4_benchmark.json").read_text())


def completed(result):
    return [result["scales"][k] for k in sorted(result["scales"], key=int)
            if result["scales"][k]["runs_completed"] == result["scales"][k]["runs_planned"]]


def _sum_phases(phases, field):
    return sum(p[field] for p in phases.values())


def cache_row(sc):
    """Cache behaviour of the standard GET workload (hit keys, then miss keys, from an empty cache) at one scale.
    The workload is deterministic, so every run's counters must agree; the first run's are reported."""
    cache = [r["cache"] for r in sc["runs"]]
    first = cache[0]
    identical = all(c["cache_after_timed_gets"] == first["cache_after_timed_gets"]
                    and c["cached"] == first["cached"] and c["uncached"] == first["uncached"] for c in cache)
    live = first["cache_after_timed_gets"]
    cached, uncached = first["cached"], first["uncached"]
    ct, ut = cached["totals"], uncached["totals"]
    lookups = live["hits"] + live["misses"]
    return {
        "records": sc["records"], "runs": len(cache), "counters_identical_across_runs": identical,
        "capacity_bytes": live["capacity_bytes"], "sstables": first["sstables"],
        "sstable_bytes": first["sstable_bytes"], "memtable_entries": first["memtable_entries"],
        "cache_lookups": lookups, "hits": live["hits"], "misses": live["misses"], "hit_rate": live["hit_rate"],
        "insertions": live["insertions"], "evictions": live["evictions"], "bytes": live["bytes"],
        "entries": live["entries"], "bytes_share_of_capacity": live["bytes"] / live["capacity_bytes"],
        "bytes_share_of_sstable_bytes": live["bytes"] / first["sstable_bytes"] if first["sstable_bytes"] else None,
        "gets": {ph: {k: cached["phases"][ph][k] for k in ("gets", "cache_hit_gets", "cache_miss_gets",
                                                           "no_cache_access_gets")} for ph in ("hit", "miss")},
        "physical_block_reads_cached": ct["block_reads"], "physical_block_reads_uncached": ut["block_reads"],
        "block_reads_avoided": ut["block_reads"] - ct["block_reads"],
        "bytes_read_cached": ct["bytes_read"], "bytes_read_uncached": ut["bytes_read"],
        "open_table_calls_cached": ct["open_table_calls"],
        "block_reads_equal_cache_misses": ct["block_reads"] == live["misses"] == ct["open_table_calls"],
        "blocks_scanned": ct["blocks_scanned"], "records_decoded_cached": ct["records_decoded"],
        "records_decoded_uncached": ut["records_decoded"], "bytes_decoded": ct["bytes_decoded"],
        "tables_consulted": ct["tables_consulted"], "bloom_checks": ct["bloom_checks"],
        "bloom_negative": ct["bloom_negative"], "bloom_maybe": ct["bloom_maybe"],
        "false_positives": ct["false_positives"], "index_lookups": ct["index_lookups"],
        "index_rule_outs": ct["index_rule_outs"],
        "hit_gets_with_block_read": _sum_phases(cached["phases"], "hit_gets_with_block_read"),
        "hit_gets_with_file_open": _sum_phases(cached["phases"], "hit_gets_with_file_open"),
        "violations": _sum_phases(cached["phases"], "gets_with_reads_not_equal_misses")
        + _sum_phases(cached["phases"], "reads_without_cache_access"),
        "reads_per_phase": {ph: {"cached": cached["phases"][ph]["block_reads"],
                                 "uncached": uncached["phases"][ph]["block_reads"]} for ph in ("hit", "miss")},
    }


def sample_comparison(sc, ref):
    """V5's instrumented V4-style sample (cold cache) against V4's for the same keys. Two fields are defined by file
    opens in V4's counters, so a cache hit changes them: `tables_opened`, and `fp_without_file_access` (a Bloom false
    positive that opened no file; a false positive served from the cache opens none). Every other field (tables
    consulted, Bloom answers, index searches, blocks scanned, records and bytes decoded, false positives) must be
    identical."""
    from bench import WORK_FIELDS

    cols = ["category"] + list(WORK_FIELDS)
    opened = cols.index("tables_opened")
    fp_nofile = cols.index("fp_without_file_access")
    skip = {opened, fp_nofile}
    n = same = 0
    opened5 = opened4 = fpn5 = fpn4 = 0
    for r5, r4 in zip(sc["runs"], ref["runs"]):
        for kind in ("hit", "miss"):
            for a, b in zip(r5["read_work"][kind], r4["read_work"][kind]):
                n += 1
                same += [x for i, x in enumerate(a) if i not in skip] == [x for i, x in enumerate(b) if i not in skip]
                opened5 += a[opened]
                opened4 += b[opened]
                fpn5 += a[fp_nofile]
                fpn4 += b[fp_nofile]
    return {"sampled_gets": n, "identical_apart_from_file_open_fields": same, "tables_opened_v5_cold": opened5,
            "tables_opened_v4": opened4, "fp_without_file_access_v5_cold": fpn5, "fp_without_file_access_v4": fpn4}


def analyze(result):
    v4 = v4_reference()
    sc = completed(result)
    out = {"scales_used": [s["records"] for s in sc]}
    if not sc:
        return out
    n = out["scales_used"]
    s4 = [v4["scales"][str(r)] for r in n]
    tables = [s["runs"][0]["state"]["after_load"]["sst_files"] for s in sc]
    assert tables == [e["runs"][0]["state"]["after_load"]["sst_files"] for e in s4], "table layout differs from V4"
    for s, e in zip(sc, s4):
        for r5, r4 in zip(s["runs"], e["runs"]):
            for ph in ("after_load", "after_overwrites", "after_deletes", "final_closed"):
                assert r5["state"][ph] == r4["state"][ph], "on-disk state differs from V4"
    out["sstables_after_load"] = tables

    # ---- GET latency ----
    spread = lambda m: (m["max"] - m["min"]) / m["median"]  # noqa: E731
    for kind in ("get_hit", "get_miss"):
        for p in PCTS + ("mean_us",):
            a = [s["pooled_all_runs"][kind][p] for s in sc]
            b = [e["pooled_all_runs"][kind][p] for e in s4]
            out[f"{kind}_{p}"] = {"v5_us": a, "v4_us": b, "v5_over_v4": [x / y for x, y in zip(a, b)],
                                  "change_percent": [pct_change(x, y) for x, y in zip(a, b)]}
        out[f"{kind}_p50_run_medians_us"] = {
            "v5": [[r["summary"][kind]["p50_us"] for r in s["runs"]] for s in sc],
            "v4": [[r["summary"][kind]["p50_us"] for r in s["runs"]] for s in s4]}
        out[f"{kind}_p50_between_run_spread"] = {
            "v5": [spread(s["aggregate_over_runs"][kind]["p50_us"]) for s in sc],
            "v4": [spread(s["aggregate_over_runs"][kind]["p50_us"]) for s in s4]}
        out[f"{kind}_p50_between_run_range_us"] = {
            "v5": [[s["aggregate_over_runs"][kind]["p50_us"][k] for k in ("min", "max")] for s in sc],
            "v4": [[s["aggregate_over_runs"][kind]["p50_us"][k] for k in ("min", "max")] for s in s4]}
    out["get_samples_pooled"] = {"hit": [s["pooled_all_runs"]["get_hit"]["ops"] for s in sc],
                                 "miss": [s["pooled_all_runs"]["get_miss"]["ops"] for s in sc]}

    # ---- cache behaviour, per scale ----
    out["cache"] = [cache_row(s) for s in sc]
    out["get_by_cache_outcome"] = [{"records": s["records"], **s["pooled_get_by_cache_outcome"]} for s in sc]
    out["sample_vs_v4"] = [sample_comparison(s, e) for s, e in zip(sc, s4)]

    # ---- writes ----
    out["put_load"] = {"v5": [write_row(s) for s in sc], "v4": [write_row(e) for e in s4]}
    for phase in ("put_overwrite", "delete"):
        rows = []
        for s, e in zip(sc, s4):
            p, q = s["pooled_all_runs"][phase], e["pooled_all_runs"][phase]
            rows.append({"records": s["records"],
                         "flushing_ops_total_v5": sum(r["write_split"][phase]["n_flush_triggering_ops"] for r in s["runs"]),
                         "flushing_ops_total_v4": sum(r["write_split"][phase]["n_flush_triggering_ops"] for r in e["runs"]),
                         "v5_p50_p95_p99_us": [p["all"][m] for m in PCTS],
                         "v4_p50_p95_p99_us": [q["all"][m] for m in PCTS],
                         "v5_over_v4_p50": p["all"]["p50_us"] / q["all"]["p50_us"]})
        out[phase] = rows

    # ---- recovery ----
    comp = lambda s, key: statistics.median(r["open_components"][key] for r in s["runs"]) / 1e9  # noqa: E731
    out["open_recovery"] = {
        "v5_seconds": [s["open_recovery_seconds_median"] for s in sc],
        "v4_seconds": [e["open_recovery_seconds_median"] for e in s4],
        "v5_over_v4": [s["open_recovery_seconds_median"] / e["open_recovery_seconds_median"] for s, e in zip(sc, s4)],
        "v5_per_run_seconds": [s["open_recovery_seconds"] for s in sc],
        "v4_per_run_seconds": [e["open_recovery_seconds"] for e in s4],
        "v5_between_run_spread": [(max(s["open_recovery_seconds"]) - min(s["open_recovery_seconds"]))
                                  / s["open_recovery_seconds_median"] for s in sc],
        "v4_between_run_spread": [(max(e["open_recovery_seconds"]) - min(e["open_recovery_seconds"]))
                                  / e["open_recovery_seconds_median"] for e in s4],
        "v5_final_reopen_seconds_median": [statistics.median(s["open_recovery_final_seconds"]) for s in sc],
        "v4_final_reopen_seconds_median": [statistics.median(e["open_recovery_final_seconds"]) for e in s4],
        "v5_components_median_seconds": {k: [comp(s, k) for s in sc] for k in
                                         ("list_dir_ns", "sstable_open_ns", "wal_replay_ns")},
        "v4_components_median_seconds": {k: [comp(e, k) for e in s4] for k in
                                         ("list_dir_ns", "sstable_open_ns", "wal_replay_ns")},
        "sstables_opened": tables,
        "wal_records_replayed": [s["runs"][0]["records_in_wal_at_open"] for s in sc],
    }

    # ---- memory ----
    mem = []
    for s, e in zip(sc, s4):
        d5 = {m: [p[m]["private_delta"] for p in s["memory_probes"]] for m in ("load", "open")}
        d4 = {m: [p[m]["private_delta"] for p in e["memory_probes"]] for m in ("load", "open")}
        warm = {k: [p[k] for p in s["memory_probes"]] for k in ("warm_cache", "warm_nocache")}
        growth = {k: [p["private_growth_during_gets"] for p in v] for k, v in warm.items()}
        cache_stats = warm["warm_cache"][0]["cache"]
        tm5 = [r["table_memory"]["retained_bytes"] for r in s["runs"]]
        tm4 = [r["table_memory"]["retained_bytes"] for r in e["runs"]]
        mem.append({"records": s["records"],
                    "v5_private_delta_after_load_mb": metric_range([x / 1e6 for x in d5["load"]]),
                    "v4_private_delta_after_load_mb": metric_range([x / 1e6 for x in d4["load"]]),
                    "v5_private_delta_after_open_mb": metric_range([x / 1e6 for x in d5["open"]]),
                    "v4_private_delta_after_open_mb": metric_range([x / 1e6 for x in d4["open"]]),
                    "growth_during_gets_with_cache_mb": metric_range([x / 1e6 for x in growth["warm_cache"]]),
                    "growth_during_gets_without_cache_mb": metric_range([x / 1e6 for x in growth["warm_nocache"]]),
                    "cache_attributable_growth_mb": (statistics.median(growth["warm_cache"])
                                                     - statistics.median(growth["warm_nocache"])) / 1e6,
                    "cache_payload_bytes": cache_stats["bytes"], "cache_entries": cache_stats["entries"],
                    "cache_payload_mb": cache_stats["bytes"] / 1e6,
                    "cache_attributable_over_payload": (statistics.median(growth["warm_cache"])
                                                        - statistics.median(growth["warm_nocache"]))
                    / cache_stats["bytes"] if cache_stats["bytes"] else None,
                    "table_heap_retained_bytes_v5": metric_range(tm5),
                    "table_heap_retained_bytes_v4": metric_range(tm4),
                    "peak_working_set_after_load_mb_v5": max(p["load"]["peak_working_set_after"]
                                                             for p in s["memory_probes"]) / 1e6,
                    "peak_working_set_after_load_mb_v4": max(p["load"]["peak_working_set_after"]
                                                             for p in e["memory_probes"]) / 1e6})
    out["memory"] = mem
    return out


def baseline_analysis(result):
    v4 = v4_reference()
    out = {}
    for tag in ("default_limit", "default_limit_extended", "small_limit"):
        entry = result.get("baseline", {}).get(tag)
        if not entry:
            continue
        ref = v4["baseline"][tag]["aggregate_over_runs"]
        rows = {}
        for name, m in entry["aggregate_over_runs"].items():
            if name in ref:
                rows[name] = {p: {"v5_median": m[p]["median"], "v4_median": ref[name][p]["median"],
                                  "v5_over_v4": m[p]["median"] / ref[name][p]["median"]} for p in PCTS}
        c = entry["cache_counters_per_run"]
        lookups = [x["main_store"]["hits"] + x["main_store"]["misses"] for x in c]
        out[tag] = {"latency": rows, "tables_after_load": [len(t) for t in entry["tables_after_load"]],
                    "main_store_cache_hits": [x["main_store"]["hits"] for x in c],
                    "main_store_cache_misses": [x["main_store"]["misses"] for x in c],
                    "main_store_cache_evictions": [x["main_store"]["evictions"] for x in c],
                    "main_store_cache_lookups": lookups,
                    "main_store_hit_rate": [x["main_store"]["hit_rate"] for x in c],
                    "get_outcomes_all_runs": entry["get_outcomes"]}
    return out


# ---- locality workload -------------------------------------------------------------------------------------------
def locality_analysis(result):
    loc = result["locality"]
    names = [n for n, _ in loc["configs"]]
    runs = loc["runs"]
    plan = loc["plan"]
    ops = plan["ops"]
    rows = {}
    for name in names:
        cfgs = [r["configs"][name] for r in runs]
        first = cfgs[0]
        cap = first["capacity_bytes"]
        identical = all(c["work"] == first["work"] and c["timed_cache"] == first["timed_cache"]
                        and c["outcome_ops"] == first["outcome_ops"] for c in cfgs)
        work, ocount = first["work"], first["outcome_ops"]
        cache = first["timed_cache"]
        pooled = loc["pooled"][name]
        rows[name] = {
            "capacity_bytes": cap, "counters_identical_across_runs": identical,
            "cache_hits": cache["hits"] if cache else 0, "cache_misses": cache["misses"] if cache else None,
            "hit_rate": cache["hit_rate"] if cache else 0.0, "miss_rate": 1 - cache["hit_rate"] if cache else None,
            "evictions": cache["evictions"] if cache else 0, "insertions": cache["insertions"] if cache else 0,
            "cache_bytes": cache["bytes"] if cache else 0, "cache_entries": cache["entries"] if cache else 0,
            "physical_block_reads": work["block_reads"], "bytes_read": work["bytes_read"],
            "open_table_calls": work["open_table_calls"], "blocks_scanned": work["blocks_scanned"],
            "records_decoded": work["records_decoded"], "bytes_decoded": work["bytes_decoded"],
            "tables_consulted": work["tables_consulted"], "bloom_negative": work["bloom_negative"],
            "bloom_maybe": work["bloom_maybe"], "index_lookups": work["index_lookups"],
            "physical_reads_per_get": work["block_reads"] / ops,
            "hit_ops_with_block_read": first["hit_ops_with_block_read"],
            "reads_equal_cache_misses": (work["block_reads"] == cache["misses"]) if cache else None,
            "latency_us": {g: {m: pooled[g][m] for m in PCTS + ("mean_us", "max_us")} | {"ops": pooled[g]["ops"]}
                           for g in pooled if pooled[g]},
            "p50_run_medians_us": [r["summary_all"][name]["p50_us"] for r in runs],
            "p50_between_run_spread": (lambda v: (max(v) - min(v)) / statistics.median(v))(
                [r["summary_all"][name]["p50_us"] for r in runs]),
            "heap_retained": first.get("heap_retained"),
            "ops_by_group": {g: pooled[g]["ops"] // len(runs) for g in pooled if pooled[g]},
        }
        assert ocount["hit"] + ocount["miss"] == (ops if cache else 0)
    # The committed result also has a "v4" row (the store before the cache existed). The benchmark no longer
    # runs that store, so a fresh result has no such row and only the comparison with the cache off remains.
    base, nocache = rows.get("v4"), rows["v5_0"]
    for name, row in rows.items():
        if base is not None:
            row["p50_over_v4"] = row["latency_us"]["all"]["p50_us"] / base["latency_us"]["all"]["p50_us"]
        row["p50_over_v5_0"] = row["latency_us"]["all"]["p50_us"] / nocache["latency_us"]["all"]["p50_us"]
        if base is not None:
            row["mean_over_v4"] = row["latency_us"]["all"]["mean_us"] / base["latency_us"]["all"]["mean_us"]
            row["reads_avoided_vs_v4"] = base["physical_block_reads"] - row["physical_block_reads"]
            row["bytes_read_avoided_vs_v4"] = base["bytes_read"] - row["bytes_read"]
    return {"configs": names, "plan": plan, "rows": rows, "ops_per_config_per_run": ops, "runs": len(runs),
            "records_decoded_equal_across_configs": len({r["records_decoded"] for r in rows.values()}) == 1,
            "tables_consulted_equal_across_configs": len({r["tables_consulted"] for r in rows.values()}) == 1,
            "all_counters_identical_across_runs": all(r["counters_identical_across_runs"] for r in rows.values()),
            "hit_ops_with_block_read_total": sum(r["hit_ops_with_block_read"] for r in rows.values())}
