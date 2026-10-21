"""Analysis of a V6 benchmark result: everything derived from the measured runs (see bench.py). Pure functions of the
result JSON plus the V0..V5 reference results; nothing here measures. Usage: via `bench.py --combine/--analyze-only`."""

import json
import statistics

from bench import ROOT

RESULTS = ROOT / "benchmarks/results"
PHASES = ("get_hit", "get_miss", "get_miss_inrange", "get_deleted")
WORK = ("tables_consulted", "bloom_checks", "bloom_negative", "bloom_maybe", "false_positives", "index_lookups",
        "tables_opened", "blocks_scanned", "records_decoded", "bytes_decoded")
STRUCT_KEYS = ("tables", "table_bytes", "records", "data_bytes", "index_bytes", "filter_bytes", "footer_bytes",
               "index_entries", "memtable_entries", "cmp_tables")
STAT_KEYS = ("input_tables", "output_tables", "records_read", "records_emitted", "obsolete_records_removed",
             "tombstones_removed", "bytes_read", "bytes_written", "input_table_bytes", "output_table_bytes")


def med(xs):
    return statistics.median(xs)


def spread(xs):
    return (max(xs) - min(xs)) / med(xs) if len(xs) > 1 and med(xs) else None


def change(pre, post):
    return (post - pre) / pre * 100 if pre else None


def ratio(a, b):
    return a / b if b else None


def _strip(struct):
    return {k: v for k, v in struct.items() if k not in ("stage", "scan", "table_heap", "dir_closed")}


def workload_analysis(entry, kind):
    runs = [r for r in entry["runs"] if kind in r["workloads"]]
    if not runs:
        return None
    rs = [r["workloads"][kind] for r in runs]
    first = rs[0]
    out = {"runs": len(rs), "records": entry["records"], "memtable_limit_bytes": entry["memtable_limit_bytes"]}

    # ---- structure, identical across runs?
    out["pre"] = _strip(first["pre"])
    out["post"] = _strip(first["post"])
    out["structure_identical_across_runs"] = all(
        all(r[stage][k] == first[stage][k] for k in STRUCT_KEYS) for r in rs for stage in ("pre", "post"))

    # ---- compaction
    st = first["compaction"]
    noop = st["compactions"] == 0
    out["noop"] = noop
    out["stats"] = {k: st[k] for k in STAT_KEYS}
    out["stats_identical_across_runs"] = all(all(r["compaction"][k] == st[k] for k in STAT_KEYS) for r in rs)
    wall = [r["compaction"]["wall_s"] for r in rs]
    inside = [r["compaction"]["duration_s"] for r in rs]
    out["compaction_wall_s"] = {"values": wall, "median": med(wall), "min": min(wall), "max": max(wall), "spread": spread(wall)}
    out["compaction_duration_s_median"] = med(inside)
    if not noop:
        n_in = st["records_read"] // 2
        out["per_input_record_us"] = med(wall) * 1e6 / n_in
        out["input_mb_per_s"] = st["input_table_bytes"] / 1e6 / med(wall)
        out["records_read_per_s"] = n_in / med(wall)
        out["write_amplification"] = st["bytes_written"] / st["input_table_bytes"]
        out["read_over_input"] = st["bytes_read"] / st["input_table_bytes"]

    # ---- scan (classification), from the first run that has one
    scan = next((r["pre"]["scan"] for r in rs if r["pre"]["scan"]), None)
    scan_post = next((r["post"]["scan"] for r in rs if r["post"]["scan"]), None)
    out["scan_pre"], out["scan_post"] = scan, scan_post
    if scan and not noop:
        pre, post = out["pre"], out["post"]
        live_payload = scan["live_payload_bytes"]
        out["space"] = {
            "live_payload_bytes": live_payload,
            "live_record_bytes": scan["live_record_bytes"],
            "stale_record_bytes_removed": scan["obsolete_record_bytes"],
            "tombstone_record_bytes_removed": scan["tombstone_record_bytes"],
            "table_bytes_pre": pre["table_bytes"], "table_bytes_post": post["table_bytes"],
            "amplification_pre": pre["table_bytes"] / live_payload, "amplification_post": post["table_bytes"] / live_payload,
            "bytes_freed": pre["table_bytes"] - post["table_bytes"],
            "freed_share_of_pre": 1 - post["table_bytes"] / pre["table_bytes"],
            "header_bytes_live": scan["live"] * 13,
            "index_filter_footer_pre": pre["index_bytes"] + pre["filter_bytes"] + pre["footer_bytes"],
            "index_filter_footer_post": post["index_bytes"] + post["filter_bytes"] + post["footer_bytes"],
            "stale_share_of_records_pre": scan["obsolete"] / scan["physical"],
            "tombstone_share_of_records_pre": scan["tombstone"] / scan["physical"],
        }
        sp = out["space"]
        # table file = live key+value payload + live record headers + stale + tombstone records + index/filter/footer
        sp["identity_pre"] = (sp["live_payload_bytes"] + sp["header_bytes_live"] + sp["stale_record_bytes_removed"]
                              + sp["tombstone_record_bytes_removed"] + sp["index_filter_footer_pre"] == pre["table_bytes"])
        sp["identity_post"] = (sp["live_payload_bytes"] + sp["header_bytes_live"] + sp["index_filter_footer_post"]
                               == post["table_bytes"])

    # ---- GETs, pooled over runs, before vs after
    gets = {}
    for ph in PHASES:
        if ph not in entry["pooled"][kind]["pre"]:
            continue
        pre, post = entry["pooled"][kind]["pre"][ph], entry["pooled"][kind]["post"][ph]
        p50s = {s: [r[s]["stage"]["phases"][ph]["summary"]["p50_us"] for r in rs] for s in ("pre", "post")}
        outcome = {s: {k: sum(r[s]["stage"]["phases"][ph]["cache_outcome"][k] for r in rs)
                       for k in ("none", "cache_hit", "cache_miss")} for s in ("pre", "post")}
        gets[ph] = {"samples_pooled": pre["ops"], "samples_per_run": first["pre"]["stage"]["phases"][ph]["gets"],
                    "pre": {m: pre[m] for m in ("p50_us", "p95_us", "p99_us", "mean_us", "max_us")},
                    "post": {m: post[m] for m in ("p50_us", "p95_us", "p99_us", "mean_us", "max_us")},
                    "change_percent": {m: change(pre[m], post[m]) for m in ("p50_us", "p95_us", "p99_us", "mean_us")},
                    "post_over_pre": {m: ratio(post[m], pre[m]) for m in ("p50_us", "p95_us", "p99_us", "mean_us")},
                    "p50_between_run_spread": {s: spread(p50s[s]) for s in p50s},
                    "run_p50_us": p50s, "cache_outcome": outcome}
        if not noop and not entry["records"] == 0:
            saving = pre["mean_us"] - post["mean_us"]
            gets[ph]["mean_saving_us"] = saving
            gets[ph]["break_even_gets"] = (med(wall) * 1e6 / saving) if saving > 0 else None
    out["gets"] = gets

    # ---- cache over the GET stage (counters are deterministic; first run)
    out["cache"] = {s: first[s]["stage"]["cache"] for s in ("pre", "post")}

    # ---- read work per GET (sampled, untimed, cold cache), mean over runs
    work = {}
    for ph in PHASES:
        if ph not in first["pre"]["stage"]["read_work"]:
            continue
        work[ph] = {"n": first["pre"]["stage"]["read_work"][ph]["n"]}
        for s in ("pre", "post"):
            work[ph][s] = {f: statistics.fmean(r[s]["stage"]["read_work"][ph]["fields"][f]["mean"] for r in rs)
                           for f in WORK}
            work[ph][s]["max_tables_consulted"] = max(r[s]["stage"]["read_work"][ph]["fields"]["tables_consulted"]["max"]
                                                      for r in rs)
            work[ph][s]["max_records_decoded"] = max(r[s]["stage"]["read_work"][ph]["fields"]["records_decoded"]["max"]
                                                     for r in rs)
    out["read_work"] = work

    # ---- recovery
    rec = {}
    for s in ("pre", "post"):
        ns = [r["recovery"][s]["open_ns"] for r in rs]
        comps = [r["recovery"][s]["components"] for r in rs]
        rec[s] = {"open_seconds": [x / 1e9 for x in ns], "median_s": med(ns) / 1e9, "min_s": min(ns) / 1e9,
                  "max_s": max(ns) / 1e9, "spread": spread(ns),
                  "components_median_s": {k: med([c[k] for c in comps]) / 1e9
                                          for k in ("list_dir_ns", "sstable_open_ns", "wal_replay_ns")},
                  "work": {k: comps[0][k] for k in ("tables", "wal_segments", "memtable_entries_replayed",
                                                    "index_entries_loaded", "index_bytes_read", "filter_bytes_read",
                                                    "footer_bytes_read")}}
    rec["post_over_pre"] = ratio(rec["post"]["median_s"], rec["pre"]["median_s"])
    out["recovery"] = rec

    # ---- heap held by the opened tables (tracemalloc, untimed)
    out["table_heap"] = {s: {"retained_bytes_median": med([r[s]["table_heap"]["retained_bytes"] for r in rs]),
                             "bloom_block_bytes": first[s]["table_heap"]["bloom_block_bytes"],
                             "index_entries": first[s]["table_heap"]["index_entries"]} for s in ("pre", "post")}

    # ---- write amplification and I/O (per run 0: deterministic accounting)
    run0 = runs[0]
    user = run0["load"]["user_payload_bytes"] + (run0["churn_phase"]["user_payload_bytes"] if kind == "churn" else 0)
    wal = run0["load"]["wal_record_bytes"] + (run0["churn_phase"]["wal_record_bytes"] if kind == "churn" else 0)
    flush = first["pre"]["table_bytes"]
    comp = st["bytes_written"]
    out["write_amplification_total"] = {
        "user_payload_bytes": user, "wal_record_bytes": wal, "flush_bytes": flush, "compaction_bytes": comp,
        "without_compaction": (wal + flush) / user, "with_compaction": (wal + flush + comp) / user,
        "flush_only": flush / user, "compaction_only": comp / user,
        "compaction_over_flush": ratio(comp, flush)}

    # ---- memory and I/O probes (fresh processes; churn workload only)
    probes = [r["memory_probe"] for r in runs if r.get("memory_probe")]
    if probes and kind == "churn":
        out["memory"] = {
            "probes": len(probes),
            "pre_open_private_delta": [p["pre_open"]["private_delta"] for p in probes],
            "post_open_private_delta": [p["post_open"]["private_delta"] for p in probes],
            "pre_open_working_set_delta": [p["pre_open"]["working_set_delta"] for p in probes],
            "post_open_working_set_delta": [p["post_open"]["working_set_delta"] for p in probes],
            "compaction_peak_ws_over_open": [p["compaction"]["peak_working_set_over_open"] for p in probes],
            "compaction_private_change": [p["compaction"]["private_change"] for p in probes],
            "compaction_peak_working_set": [p["compaction"]["peak_working_set_after"] for p in probes],
            "pre_filter_bytes": probes[0]["pre_open"]["filter_bytes"], "post_filter_bytes": probes[0]["post_open"]["filter_bytes"],
        }
        io = probes[0]["compaction"]["io"]
        s0 = probes[0]["compaction"]["stats"]
        out["io"] = {"counted": io, "store_bytes_read": s0["bytes_read"], "store_bytes_written": s0["bytes_written"],
                     "counted_equals_store_bytes_read": io["bytes_read"] == s0["bytes_read"],
                     "opens_equal_two_per_input_table": io["table_opens"] == 2 * s0["input_tables"],
                     "reads_per_input_pass_bytes": io["bytes_read"] // 2 if io["bytes_read"] else 0}

    # ---- checks
    out["checks"] = {
        "reconciliation_runs": len(rs),
        "identities_checked_per_run": len(first["reconciliation"]["identities"]),
        "full_scan_runs": sum(1 for r in rs if r["pre"]["scan"]),
        "verification_keys_checked_per_run": sum(first["verification"]["checked"].values()),
        "only_cmp_tables_after_reopen": all(r["verification"]["audit"]["only_cmp_tables"] or noop for r in rs),
        "no_tmp_files": all(r["verification"]["audit"]["tmp_files"] == 0 for r in rs),
        "cache_empty_after_reopen": all(r["cache_after_reopen"]["entries"] == 0 for r in rs),
    }
    out["churn_phase"] = run0["churn_phase"]
    out["load"] = {k: run0["load"][k] for k in ("wall_s", "user_payload_bytes", "wal_record_bytes", "puts")}
    return out


def scale_analysis(entry):
    return {"records": entry["records"], "runs": entry["runs_completed"], "control_runs": entry["control_runs"],
            "memtable_limit_bytes": entry["memtable_limit_bytes"],
            "churn": workload_analysis(entry, "churn"), "control": workload_analysis(entry, "control")}


# ---- architecture-wide summary from the V0..V6 results ----------------------------------------------------------------
def _load(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def _ref_work(entry, version):
    """Mean tables consulted and records decoded per sampled GET, from a V2..V5 result's per-run samples."""
    out = {}
    for ph in ("hit", "miss"):
        rows = [row for r in entry["runs"] for row in r["read_work"][ph]]
        if version <= 3:  # [category, tables, records, bytes]
            t, rec = 1, 2
        else:  # [category, tables_consulted, ..., records_decoded, bytes_decoded]
            t, rec = 1, 11
        out[ph] = {"tables_consulted": statistics.fmean(r[t] for r in rows), "records_decoded": statistics.fmean(r[rec] for r in rows),
                   "samples": len(rows)}
    return out


def architecture(result):
    refs = {0: _load("v0_scaling.json"), 1: _load("v1_benchmark.json"), 2: _load("v2_benchmark.json"),
            3: _load("v3_benchmark.json"), 4: _load("v4_benchmark.json"), 5: _load("v5_benchmark.json")}
    out = {"scales": {}}
    for key in sorted(result["scales"], key=int):
        e = result["scales"][key]
        row = {"get": {}, "recovery_s": {}, "memory_open_private_bytes": {}, "read_work": {}}
        for v, d in refs.items():
            sc = d["scales"].get(key)
            if sc is None:
                continue
            pooled = sc["pooled_all_runs"]
            row["get"][f"V{v}"] = {ph: {m: pooled[ph][m] for m in ("p50_us", "p95_us", "p99_us")} | {"ops": pooled[ph]["ops"]}
                                   for ph in ("get_hit", "get_miss")}
            row["recovery_s"][f"V{v}"] = sc["open_recovery_seconds_median"]
            if v >= 1:
                row["memory_open_private_bytes"][f"V{v}"] = sc["memory_probes"][0]["open"]["private_delta"]
            if v >= 2:
                row["read_work"][f"V{v}"] = _ref_work(sc, v)
        a = scale_analysis(e)
        for kind in ("control", "churn"):
            w = a[kind]
            if not w:
                continue
            for stage in ("pre", "post"):
                name = f"V6_{kind}_{stage}"
                pool = e["pooled"][kind][stage]
                row["get"][name] = {ph: {m: pool[ph][m] for m in ("p50_us", "p95_us", "p99_us")} | {"ops": pool[ph]["ops"]}
                                    for ph in ("get_hit", "get_miss")}
                row["recovery_s"][name] = w["recovery"][stage]["median_s"]
                row["read_work"][name] = {ph[4:]: {"tables_consulted": w["read_work"][ph][stage]["tables_consulted"],
                                                   "records_decoded": w["read_work"][ph][stage]["records_decoded"],
                                                   "samples": w["read_work"][ph]["n"] * w["runs"]}
                                          for ph in ("get_hit", "get_miss")}
                if w.get("memory"):
                    m = w["memory"]
                    row["memory_open_private_bytes"][name] = med(m[f"{stage}_open_private_delta"])
        out["scales"][key] = row
    return out


def analyze(result):
    out = {"scales": {k: scale_analysis(result["scales"][k]) for k in sorted(result["scales"], key=int)}}
    if result.get("small_limit"):
        out["small_limit"] = scale_analysis(result["small_limit"])
    out["crash_sanity"] = result.get("crash_sanity") or []
    out["architecture"] = architecture(result) if result["scales"] else None
    return out
