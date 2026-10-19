# Benchmarks

There are two benchmarks: the original **~1.24 MB baseline** (the V0 benchmark, below) and the **scaling characterization** (the V0 scaling benchmark, further down; 10,000 to 10,000,000 records). They use different workloads, so don't compare their numbers directly. The V1 benchmark is described in [V1 benchmark](#v1-benchmark). The V2 benchmark is described in [V2 benchmark](#v2-benchmark).

The baseline JSON file holds the metadata, per-run summaries, aggregates and the raw per-operation latencies (in ns) of every run.

## Methodology (keep identical across versions)

**Workload** (deterministic; `random.Random(seed=42)`, generated before any timing):

| phase | ops | description | store config |
|---|---|---|---|
| `put_load` | 10,000 | PUT of 10,000 distinct keys (the keyspace) | `sync=False` |
| `get_hit` / `get_miss` | 100 + 100 | GET, alternating existing keys and keys outside the keyspace | `sync=False` |
| `mixed` | 300 | 70% GET, 25% PUT overwrite, 5% DELETE, keys uniform over the keyspace | `sync=False` |
| `put_sync` | 300 | PUT of distinct keys into a separate fresh file | `sync=True` |

Keys are 11 bytes (`key%08d`) and values are 100 random bytes. The phases run in this order on one fresh database per run, except `put_sync`, which gets its own database. The whole benchmark is repeated for 5 runs with the same workload, each on a fresh temp-dir database.

Only the `put`/`get`/`delete` call is timed, using one `time.perf_counter_ns()` pair per operation. Workload generation, database creation and close, and temp-dir cleanup are not timed. Throughput is ops divided by the sum of per-op latencies. Percentiles use nearest-rank on the per-run latencies, and the tables report the median across runs with the min–max across runs in parentheses.

**Caveats:**
- The OS page cache is warm, and there's no cold-cache measurement.
- Python garbage collection is left at its default.
- Run-to-run spread is visible in the ranges. Don't claim an improvement smaller than that spread from a single measurement.
- `mixed: DELETE` has only 11 ops per run, so its p95 and p99 equal its max.
- Physical read/write I/O is not measured. Structural scan, size and overhead metrics for the scaling workload are derived in [Structural metrics](#structural-metrics); the baseline workload has none.

## V0 baseline results

All latencies are in microseconds. Values are the median (min–max) over 5 runs.

| metric | ops/run | p50 (µs) | p95 (µs) | p99 (µs) | throughput (ops/s) |
|---|---|---|---|---|---|
| PUT, sync=False | 10000 | 4.0 (3.9–5.1) | 9.6 (4.7–15.1) | 13.1 (10.8–18.9) | 194,277 (141,508–227,617) |
| PUT, sync=True | 300 | 2,249 (2,152–2,280) | 2,697 (2,559–2,781) | 4,149 (3,844–4,443) | 436 (419–454) |
| GET hit | 100 | 8,672 (7,145–9,041) | 13,521 (8,547–14,425) | 21,358 (18,119–23,445) | 107 (102–135) |
| GET miss | 100 | 8,781 (7,392–9,156) | 12,931 (8,721–13,744) | 14,120 (13,420–15,127) | 108 (107–133) |
| mixed, all ops | 300 | 8,144 (7,317–8,532) | 22,175 (18,871–23,219) | 26,210 (22,271–26,743) | 112 (110–127) |
| mixed: GET | ~209 | 10,515 (8,738–10,606) | 23,086 (20,109–23,999) | 26,210 (22,271–26,743) | 78 (77–89) |
| mixed: PUT | ~80 | 15.2 (14.5–16.2) | 38.4 (32.4–47.7) | 46.6 (41.0–119.9) | 58,798 (52,036–62,819) |
| mixed: DELETE | ~11 | 15.5 (13.4–22.5) | 30.8 (17.3–304.5) | 30.8 (17.3–304.5) | 66,305 (24,625–73,628) |

Dataset: 10,000 keys × (11 B key + 100 B value) = 1,240,000 bytes on disk after the load (124 bytes per record, including the 13-byte header), and 1,250,184 bytes after the mixed phase.

What the numbers show: PUT cost is dominated by the `flush` (~4 µs) or by `fsync` (~2.2 ms). A GET is a full scan of ~1.2 MB (~8–9 ms) whether or not the key exists. The mixed-phase GETs are slower than the standalone GETs, and that wasn't investigated.

**Run metadata** (also in the JSON): Python 3.10.6 (MSC v.1932, 64-bit), Windows 11 (`Windows-10-10.0.26200-SP0`), Intel64 Family 6 Model 183, 24 logical CPUs, seed 42, 5 runs.

---

# V0 scaling characterization

Purpose: show how V0 behaves as the database grows. V0's `get` scans the whole file, so its cost should grow with file size. This benchmark measures how it actually grows, along with PUT latency and open/recovery time. It doesn't change the V0 implementation, and the results are the quantitative baseline for later versions.

**This is a warm OS-cache benchmark.** See [Cache condition](#cache-condition).

## Methodology

Timing works the same way as in the baseline: only the `put`/`get` call is timed (`perf_counter_ns` per op). Workload generation, database creation, correctness checks, close and temp-dir cleanup are not timed. The setup is seed **42**, 11-byte keys (`key%08d`), 100 random bytes per value, 124-byte records on disk (13-byte header + key + value), `sync=False`, and a **fresh database for every run** in a temp directory that is deleted afterwards. Each run's keys and values come from `random.Random(42)`, so every run at every scale builds the same data.

Per run, at each scale of N records:

1. **`put_load`**: N PUTs of distinct keys (keyspace = N). Keys and values are generated just before each call, outside the timed region. Per-op latencies are summarized (percentiles, mean, min, max) rather than stored individually.
2. **`open_recovery`**: close the store, then time `KVStore(path)` on the finished file (the open-time validation scan). This is reported separately because it isn't an operation latency.
3. **`get_hit`** and **`get_miss`**: G GETs each. Hit keys are drawn uniformly from the keyspace, and miss keys (`key_of(N + i)`) are never written. Every hit is checked to return the exact value written during the load, and every miss is checked to return `None`. The check happens after the timed call.
4. **`put_overwrite`**: 1,000 timed PUTs of existing keys (drawn uniformly), run after the GETs. Afterwards, 5 untimed GETs verify that the newest value wins.

After every run the file size is checked to be exactly `N × 124` bytes after the load and `(N + 1,000) × 124` after the overwrites.

| scale | records N | runs | GETs per run (hit + miss) | file after load (bytes) | after overwrites (bytes) |
|---|---|---|---|---|---|
| S1 | 10,000 | 5 | 200 + 200 | 1,240,000 | 1,364,000 |
| S2 | 100,000 | 5 | 200 + 200 | 12,400,000 | 12,524,000 |
| S3 | 1,000,000 | 5 | 100 + 100 | 124,000,000 | 124,124,000 |
| S4 | 5,000,000 | 3 | 50 + 50 | 620,000,000 | 620,124,000 |
| S5 | 10,000,000 | 3 | 50 + 50 | 1,240,000,000 | 1,240,124,000 |

File sizes are measured. One GET costs O(file size), so the largest scales get fewer GETs and runs (a single GET takes about 8 s at S5). Each scale still has at least 150 samples per kind. The largest file (1.24 GB) fits in RAM on this machine (15.8 GB).

### Cache condition

The primary benchmark is a **warm OS-cache** benchmark. The file is written during the load, so it sits in the OS page cache. The timed `open_recovery` then reads it end to end once more, and every GET afterwards scans it from the cache. Nothing is evicted between steps, and the same procedure is used at every scale. So the GET numbers measure CPU and Python scan cost (read, checksum, parse), not SSD read latency.

Opening a new file handle doesn't make the cache cold, so `open_recovery` is a warm-cache number too. **No cold-cache measurement is made or claimed.** The benchmark has no reliable, controlled way to drop the Windows file cache for a single file, and an unreliable procedure would only produce misleading numbers.

## Results

GET percentiles are computed over all samples from all runs at a scale (pooled), and the range of the per-run p50 values is shown too. Percentiles use nearest-rank. Throughput is operations divided by the sum of per-operation latencies.

**GET hit** (latency in ms)

| scale | samples | p50 | p95 | p99 | max | per-run p50 range | GETs/s |
|---|---|---|---|---|---|---|---|
| S1 | 1,000 | 7.8 | 9.4 | 10.4 | 13.5 | 7.3–8.1 | 125 |
| S2 | 1,000 | 80.7 | 89.5 | 92.6 | 111.4 | 79.0–82.2 | 12.3 |
| S3 | 500 | 813.0 | 884.1 | 900.5 | 914.1 | 803.8–841.1 | 1.23 |
| S4 | 150 | 4,100.7 | 4,336.1 | 4,410.3 | 4,484.0 | 4,063.2–4,128.1 | 0.244 |
| S5 | 150 | 7,841.7 | 8,061.0 | 8,200.1 | 8,230.7 | 7,782.5–7,901.7 | 0.127 |

**GET miss** (latency in ms)

| scale | samples | p50 | p95 | p99 | max | per-run p50 range | GETs/s |
|---|---|---|---|---|---|---|---|
| S1 | 1,000 | 7.9 | 9.5 | 11.0 | 16.9 | 7.6–8.6 | 123 |
| S2 | 1,000 | 81.5 | 90.4 | 92.7 | 104.7 | 79.8–83.0 | 12.2 |
| S3 | 500 | 825.7 | 886.5 | 917.1 | 993.6 | 803.3–856.9 | 1.22 |
| S4 | 150 | 4,051.0 | 4,238.9 | 4,313.5 | 4,325.7 | 3,984.9–4,080.1 | 0.247 |
| S5 | 150 | 7,901.7 | 8,181.3 | 8,376.8 | 8,527.3 | 7,692.9–7,974.5 | 0.127 |

**PUT, `sync=False`** (latency in µs; load figures are the median over runs)

| scale | load p50 / p95 / p99 | load ops/s (range over runs) | overwrite samples | overwrite p50 / p95 / p99 | overwrite ops/s |
|---|---|---|---|---|---|
| S1 | 4.7 / 5.6 / 10.6 | 203,513 (197,774–214,416) | 5,000 | 4.2 / 6.1 / 17.4 | 206,801 |
| S2 | 4.3 / 5.3 / 10.6 | 210,559 (202,041–224,399) | 5,000 | 4.2 / 5.2 / 26.3 | 208,444 |
| S3 | 4.1 / 5.2 / 10.6 | 214,416 (211,366–220,798) | 5,000 | 4.5 / 5.7 / 23.1 | 195,877 |
| S4 | 4.1 / 5.3 / 10.2 | 215,768 (212,646–218,368) | 3,000 | 4.0 / 4.8 / 15.2 | 221,938 |
| S5 | 4.1 / 5.4 / 9.5 | 216,574 (212,815–216,957) | 3,000 | 4.3 / 10.1 / 26.8 | 192,850 |

**Open / recovery** (`KVStore(path)` on an existing file)

| scale | seconds per run | median (s) | µs per record |
|---|---|---|---|
| S1 | 0.02, 0.02, 0.02, 0.02, 0.01 | 0.02 | 1.565 |
| S2 | 0.09, 0.08, 0.09, 0.09, 0.10 | 0.09 | 0.926 |
| S3 | 0.76, 0.84, 0.79, 0.76, 0.86 | 0.79 | 0.788 |
| S4 | 3.84, 3.80, 4.00 | 3.84 | 0.767 |
| S5 | 7.76, 8.20, 7.81 | 7.81 | 0.781 |

## Analysis

- **GET latency is linear in database size.** From S1 to S5 the record count grows 1,000× and median hit latency grows 1,003× (7.8 ms to 7.84 s). A least-squares fit of log latency against log file size has a slope of **1.003** for hits and **1.000** for misses (1.0 means exactly linear). A linear fit of median latency against record count gives R² = 0.9995 (hit) and 0.9998 (miss), at about **0.79 µs per record** for both. The median hit cost per record is 0.78, 0.81, 0.81, 0.82 and 0.78 µs at S1–S5, so the cost per record stays flat across three orders of magnitude.
- **Hit and miss cost the same.** The hit/miss ratio of median latency is 0.98, 0.99, 0.99, 1.01 and 0.99 across scales, with no consistent direction. Both scan the whole file, as the design predicts.
- **Where reads become impractical.** A GET crosses 100 ms between S2 (81 ms) and S3 (813 ms), and crosses 1 s between S3 and S4 (4.1 s). At about 0.8 µs per record, 1 s works out to roughly 1.25 million records. At S5 a single GET takes about 8 s (0.13 GETs/s), and the same arithmetic puts 10 s at roughly 12.5 million records. That last figure is extrapolated, since S5 is the largest scale measured.
- **PUT cost doesn't depend on database size.** Overwrite p50 is 4.0–4.5 µs at every scale and load p50 is 4.1–4.7 µs. The log-log slope of overwrite p50 against file size is 0.0001. Overwrite throughput is 193k–222k ops/s with no trend across scales, so the differences between scales are within run-to-run noise. Tails (p99 15–27 µs) are noisier than the medians and rest on 3,000–5,000 overwrite samples per scale. This is the cost of an append plus one `flush`. With `sync=True` it would be dominated by `fsync` (see the baseline above).
- **Open/recovery is the same linear scan.** Open time grows from 0.02 s to 7.81 s. From S3 up it costs 0.77–0.79 µs per record, the same rate as a GET scan. At S1 and S2, fixed per-open costs push it higher per record (1.57 and 0.93 µs).
- **Variability.** The spread of per-run hit p50, (max − min) / median, is 9.8%, 4.0%, 4.6%, 1.6% and 1.5% for S1–S5. Small scales vary more in relative terms because individual GETs are short. Don't read differences smaller than about 5% between two measurements of the same scale as real. For the small scales the threshold is closer to 10%.

## Structural metrics

These metrics are recorded in `benchmarks/results/v0_structural.json`. **These numbers are derived, not timed.** Each one follows from the fixed record format and the workload above (key 11 B + value 100 B = 111 B user payload, 13-byte header, 124-byte record, N loaded records, then 1,000 overwrites of existing keys). The script checks the derived file sizes against the file sizes the scaling benchmark measured, and all runs at all scales agree. The tests check the scan, CRC and size formulas against a real V0 file at small scale.

"Bytes scanned" is **application-level**: it's the bytes the `read_records` loop reads from the file. It isn't physical storage I/O, which this benchmark doesn't measure (it runs warm-cache).

**Records and bytes a GET examines.** Every GET (hit or miss) scans the whole file. The timed GETs run after the load and before the overwrites, so they scan N records. Only the 5 untimed verification GETs run after the overwrites, and they scan N + 1,000.

| scale | live keys | physical records at timed GETs | bytes scanned per timed GET | physical records after overwrites | bytes scanned per GET after overwrites |
|---|---|---|---|---|---|
| S1 | 10,000 | 10,000 | 1,240,000 | 11,000 | 1,364,000 |
| S2 | 100,000 | 100,000 | 12,400,000 | 101,000 | 12,524,000 |
| S3 | 1,000,000 | 1,000,000 | 124,000,000 | 1,001,000 | 124,124,000 |
| S4 | 5,000,000 | 5,000,000 | 620,000,000 | 5,001,000 | 620,124,000 |
| S5 | 10,000,000 | 10,000,000 | 1,240,000,000 | 10,001,000 | 1,240,124,000 |

**Sizing** (all in bytes unless noted; the concepts are kept separate). *Live* means the newest version of each key. *Physical* means every record in the file, including superseded versions. The 1,000 overwrite PUTs add 1,000 physical records and no live keys, so live keys stay at N.

| scale | live user bytes (N × 111) | physical user bytes after overwrites | format overhead after load (N × 13) | format overhead after overwrites | file bytes after overwrites |
|---|---|---|---|---|---|
| S1 | 1,110,000 | 1,221,000 | 130,000 | 143,000 | 1,364,000 |
| S2 | 11,100,000 | 11,211,000 | 1,300,000 | 1,313,000 | 12,524,000 |
| S3 | 111,000,000 | 111,111,000 | 13,000,000 | 13,013,000 | 124,124,000 |
| S4 | 555,000,000 | 555,111,000 | 65,000,000 | 65,013,000 | 620,124,000 |
| S5 | 1,110,000,000 | 1,110,111,000 | 130,000,000 | 130,013,000 | 1,240,124,000 |

**Record-format overhead.** Each record carries a 13-byte header (CRC 4, op 1, key length 4, value length 4), and the file has no file-level header or footer (0 fixed bytes). A 111-byte payload becomes a 124-byte record. The header is 10.48% of every record, and V0 appends **1.117 bytes per user byte** (124 / 111). That's storage overhead from the record format, **not** LSM-style write amplification: V0 never rewrites data, so every byte is written once.

**Space amplification** is defined here as physical file bytes divided by live user bytes (N × 111). It's the product of two separate factors. One is the record-format overhead (124 / 111 = 1.1171, constant). The other is staleness (physical records / live keys = 1 + 1,000 / N), because overwritten records stay in the file.

| scale | space amplification after load | staleness factor after overwrites | space amplification after overwrites |
|---|---|---|---|
| S1 | 1.1171 | 1.1000 | 1.2288 |
| S2 | 1.1171 | 1.0100 | 1.1283 |
| S3 | 1.1171 | 1.0010 | 1.1182 |
| S4 | 1.1171 | 1.0002 | 1.1173 |
| S5 | 1.1171 | 1.0001 | 1.1172 |

The overwrite workload is a fixed 1,000 records, so its effect on space amplification shrinks as the database grows (10% at S1, 0.01% at S5). It isn't a fixed fraction of the data, so these numbers don't predict the effect of a proportional overwrite rate.

**Open/recovery work.** `open_recovery` is timed after the load and before the overwrites, so it validates N records. It reads N × 124 bytes and makes N CRC checks (one per record, each over the 120 bytes after the CRC field, so N × 120 bytes are checksummed). Then it truncates nothing. That's exactly the work of one GET scan of the same file: the same `read_records` pass, except that the GET also compares each key and the open doesn't. The structural work per record is therefore identical for open and GET, which matches the measured open-time cost per record being close to the GET cost per record (0.77–0.79 vs 0.78–0.82 µs at S3–S5). The measured open times are in the table above. The work counts are separate from them and aren't timings.

| scale | records validated | bytes read | CRC checks | CRC-covered bytes | measured open (s, median) |
|---|---|---|---|---|---|
| S1 | 10,000 | 1,240,000 | 10,000 | 1,200,000 | 0.02 |
| S2 | 100,000 | 12,400,000 | 100,000 | 12,000,000 | 0.09 |
| S3 | 1,000,000 | 124,000,000 | 1,000,000 | 120,000,000 | 0.79 |
| S4 | 5,000,000 | 620,000,000 | 5,000,000 | 600,000,000 | 3.84 |
| S5 | 10,000,000 | 1,240,000,000 | 10,000,000 | 1,200,000,000 | 7.81 |

Reopening after the overwrites would validate N + 1,000 records. The benchmark doesn't do that, so there's no measured time for it.

**Torn-tail recovery** is covered by tests, not by the scaling benchmark (`tests/test_engine.py`). The tests reopen a valid file followed by a partial record (cut inside the header, after the header, or inside the value). Recovery truncates the file to the end of the last valid record, all earlier data reads back correctly, and later appends land on a clean boundary. The extra work is the torn bytes themselves being read as part of the file. The torn record is never CRC-checked, because the scan stops at the truncation before reaching the checksum. No timing is reported for it.

## Limitations

- **Tail estimates.** p99 at S4 and S5 rests on 150 samples per kind, so it's determined by the top one or two observations. Treat it as approximate. S3 has 500 samples, and S1 and S2 have 1,000. Three runs at S4 and S5 give only a coarse view of between-run spread.
- **Warm cache only.** See [Cache condition](#cache-condition). These numbers say nothing about cold-cache or SSD-bound performance.
- **Sensitive to machine load.** Latencies depend on what else the machine is doing. These runs were made on an otherwise idle machine with Windows Defender's default real-time protection on. Compare later versions under the same conditions.
- **One machine.** Windows 11, Python 3.10, one CPU and one filesystem. No other platforms were tried.
- **Physical I/O not measured.** Bytes scanned, space amplification and recovery work are derived structural quantities (see [Structural metrics](#structural-metrics)), not storage-level measurements. The JSON's `not_available_in_v0` entries refer to LSM-style amplification, which V0 doesn't have. The `put_load` per-op latencies are summarized, not stored. GET and overwrite raw latencies are stored in the JSON.

## Environment

**Environment:** Python 3.10.6 (MSC v.1932, 64-bit); Windows 11 (`Windows-10-10.0.26300-SP0`); Intel Core i7-14650HX (`Intel64 Family 6 Model 183`), 24 logical CPUs; 15.8 GB RAM; seed 42; key 11 B; value 100 B; `sync=False`. The V0 implementation (`record.py`, `store.py`) was unchanged between its first version and these runs.

---

# V1 benchmark

Purpose: measure what moving GET from a full-file scan (V0) to a memtable lookup (V1, [v1-design.md](v1-design.md)) actually changes, and what it costs: open/replay time, write latency, memory and WAL growth. V0's results above are the baseline and aren't modified. V1 numbers are measured with the same workload definitions, timing convention and scale ladder. **All numbers are warm OS-cache numbers.** Every figure in this section comes from `benchmarks/results/v1_benchmark.json` or `v1_structural.json`.

## Methodology

**Same as V0:** seed 42, key 11 B, value 100 B, 124-byte PUT records, `sync=False` for the scaling runs, a fresh database in a temp directory for every run, and only the store call timed (a `perf_counter_ns` pair per op). Correctness checks, close and cleanup aren't timed. Percentiles are nearest-rank using the V0 code, and the scale ladder and run counts are V0's. The V0-sized set of hit keys, all miss keys and all 1,000 overwrites are generated exactly as in the V0 scaling benchmark (same RNG roles and order), so the logical workload matches V0's.

**Different from V0 (intentionally):**

| item | V0 | V1 |
|---|---|---|
| GET samples per run | 200 / 200 / 100 / 50 / 50 hits and the same number of misses (S1–S5) | **100,000 hits + 100,000 misses at every scale** (a V1 GET takes microseconds; a V0 GET takes up to 8 s) |
| runs per scale | 5 / 5 / 5 / 3 / 3 | 5 / 5 / 5 / 3 / 3 (same) |
| pooled GET samples per kind | 1,000 / 1,000 / 500 / 150 / 150 | 500,000 / 500,000 / 500,000 / 300,000 / 300,000 |

The sample counts and protocol were fixed before the final run. Each scale was run as its own fresh Python process, one after another, and every completed run is reported. GET, overwrite and delete percentiles are pooled over all runs at a scale (as for V0); `put_load` percentiles are the per-run median.

Per run at N records (the V1 benchmark):

1. **`put_load`**: N PUTs of distinct keys. Afterwards the memtable is checked to hold N entries and the WAL to be exactly N × 124 bytes.
2. **`open_recovery`**: close the store, then time `KVStore(path)` (replay of the whole WAL). The rebuilt memtable is checked untimed against a fingerprint of the live one, and no truncation may occur.
3. **`get_hit` / `get_miss`**: 100,000 each, with every result checked after the timed call. The first V0-count hit keys equal V0's.
4. **Controls** on the same hit keys: an empty Python function call (`control_noop`) and a bare `dict.get` on the memtable (`control_dict_get`). They show how much of a V1 GET figure is timing overhead and wrapper cost.
5. **`put_overwrite`**: the same 1,000 overwrites as V0, last-write-wins verified.
6. **`delete`** (new): 1,000 timed DELETEs of keys drawn uniformly from the keyspace. Repeats are allowed, so there are fewer than 1,000 distinct tombstoned keys at small scales. Every deleted key must read back `None`. V0's results have no such phase, so the V0 reference is measured with the same keys: a fresh V0 file is loaded with N keys (untimed) and the same 1,000 deletes are timed, with the same run counts, in the same invocation as the V1 runs at that scale. V0's file holds only the load at that point, while V1's also holds the overwrites.
7. After the deletes, the WAL is closed and its size is checked (N × 124 + 1,000 × 124 + 1,000 × 24 bytes). The final WAL (with overwrites and tombstones) is then reopened, and its replay is timed and checked against the live state.

**Memory probes** (untimed, separate from the latency runs): per scale, 3 probes, each a pair of fresh Python processes. One loads N keys with `sync=False` and the other opens that WAL. Process *private bytes* (Windows `PrivateUsage`) are read before and after, and the tables report the delta. These are observations for this Python build and this key/value shape, not a universal memory cost.

**10,000-key head-to-head** (`--baseline`): V1 runs the exact V0 baseline workload (`build_workload` in the V1 benchmark: 10,000 keys, 100 + 100 GETs, 300 mixed ops with 70% GET / 25% PUT overwrite / 5% DELETE, 300 `sync=True` PUTs, 5 runs). Separately, it runs an extended V1-only workload with the same definitions but 100,000 + 100,000 GETs and 100,000 mixed ops (5 runs). Every GET result is checked against a reference dict.

**V0 reference numbers** come from `v0_scaling.json` and `v0_baseline.json` as recorded. Apart from the V0 delete runs, they were measured in earlier invocations than the V1 runs, so don't read differences of a few percent between V1 and those V0 figures as real.

**Timing floor.** `perf_counter` ticks every 100 ns on this machine (`time.get_clock_info`), so V1 GET latencies of 0.4–0.8 µs are 4–8 ticks and percentiles move in 0.1 µs steps. Every timed figure includes the timer calls: the no-op call control has a median of 0.1 µs (one tick), so the V1 GET numbers are upper bounds on the store-only cost. The V0/V1 ratios below inherit a quantization of ±0.05 µs (about ±6–12% at 0.4–0.8 µs). Means, which aren't quantized, are given alongside.

## Results

### GET

**GET hit** (µs unless noted; V1 percentiles pooled over all runs at the scale)

| scale (records) | samples | p50 | p95 | p99 | max | mean | V0 p50 (ms) | V0 p50 / V1 p50 |
|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.4 | 0.6 | 0.8 | 1,111 | 0.477 | 7.8 | 19,540 |
| S2 (100,000) | 500,000 | 0.5 | 0.8 | 1.0 | 434 | 0.581 | 80.7 | 161,413 |
| S3 (1,000,000) | 500,000 | 0.7 | 1.2 | 1.6 | 1,051 | 0.776 | 813.0 | 1,161,418 |
| S4 (5,000,000) | 300,000 | 0.7 | 1.2 | 1.6 | 253 | 0.826 | 4,100.7 | 5,858,189 |
| S5 (10,000,000) | 300,000 | 0.8 | 1.3 | 1.7 | 256 | 0.880 | 7,841.7 | 9,802,071 |

**GET miss**

| scale | samples | p50 | p95 | p99 | max | mean | V0 p50 (ms) | V0 p50 / V1 p50 |
|---|---|---|---|---|---|---|---|---|
| S1 | 500,000 | 0.4 | 0.6 | 0.8 | 288 | 0.440 | 7.9 | 19,853 |
| S2 | 500,000 | 0.4 | 0.6 | 0.7 | 321 | 0.427 | 81.5 | 203,703 |
| S3 | 500,000 | 0.5 | 0.7 | 0.9 | 719 | 0.506 | 825.7 | 1,651,356 |
| S4 | 300,000 | 0.5 | 0.8 | 1.0 | 244 | 0.557 | 4,051.0 | 8,102,062 |
| S5 | 300,000 | 0.6 | 0.8 | 1.0 | 237 | 0.605 | 7,901.7 | 13,169,420 |

**Controls** (hit keys; µs). `store GET` is `KVStore.get`, `dict.get` is the bare memtable lookup, and `no-op` is an empty function call.

| scale | store GET p50 / mean | `dict.get` p50 / mean | no-op p50 / mean |
|---|---|---|---|
| S1 | 0.4 / 0.477 | 0.1 / 0.124 | 0.1 / 0.090 |
| S2 | 0.5 / 0.581 | 0.2 / 0.233 | 0.1 / 0.085 |
| S3 | 0.7 / 0.776 | 0.4 / 0.430 | 0.1 / 0.083 |
| S4 | 0.7 / 0.826 | 0.4 / 0.509 | 0.1 / 0.084 |
| S5 | 0.8 / 0.880 | 0.5 / 0.551 | 0.1 / 0.091 |

### Open / recovery (replay of the WAL)

`KVStore(path)` on the finished WAL (N records), compared with V0's `KVStore(path)` scan of the same-sized file. The last column reopens the final WAL after 1,000 overwrites and 1,000 deletes (N + 2,000 records).

| scale | V1 seconds per run | V1 median (s) | V1 µs/record | V0 median (s) | V0 µs/record | V1 / V0 | V1 reopen after overwrites + deletes (median s) |
|---|---|---|---|---|---|---|---|
| S1 | 0.02, 0.02, 0.02, 0.02, 0.02 | 0.02 | 1.982 | 0.02 | 1.565 | 1.27 | 0.02 |
| S2 | 0.10, 0.10, 0.11, 0.11, 0.11 | 0.11 | 1.051 | 0.09 | 0.926 | 1.13 | 0.11 |
| S3 | 1.06, 1.18, 1.14, 1.00, 1.01 | 1.06 | 1.061 | 0.79 | 0.788 | 1.35 | 1.05 |
| S4 | 5.35, 5.22, 5.37 | 5.35 | 1.071 | 3.84 | 0.767 | 1.40 | 5.34 |
| S5 | 11.02, 10.97, 11.29 | 11.02 | 1.102 | 7.81 | 0.781 | 1.41 | 10.78 |

The V1 / V0 column uses unrounded medians. Replay matched the live state at every scale.

### PUT and DELETE (`sync=False`)

**Load** (µs, per-run median of the percentiles; throughput is the median over runs; max is the median over runs of the slowest single PUT, in ms).

| scale | V1 p50 / p95 / p99 | V1 ops/s | V0 p50 / p95 / p99 | V0 ops/s | V1 max (ms) | V0 max (ms) |
|---|---|---|---|---|---|---|
| S1 | 4.7 / 5.6 / 15.2 | 194,314 | 4.7 / 5.6 / 10.6 | 203,513 | 0.4 | 0.1 |
| S2 | 4.4 / 5.7 / 13.8 | 195,597 | 4.3 / 5.3 / 10.6 | 210,559 | 1.8 | 0.7 |
| S3 | 4.6 / 6.0 / 12.7 | 192,294 | 4.1 / 5.2 / 10.6 | 214,416 | 15.5 | 8.1 |
| S4 | 4.5 / 5.9 / 14.1 | 194,561 | 4.1 / 5.3 / 10.2 | 215,768 | 76.6 | 12.0 |
| S5 | 4.6 / 6.1 / 11.6 | 192,683 | 4.1 / 5.4 / 9.5 | 216,574 | 172.5 | 13.8 |

**Overwrite and delete** (µs, p50 / p95 / p99, pooled over runs; the V0 delete is the same-invocation reference described above).

| scale | overwrite samples | V1 overwrite | V0 overwrite | delete samples | V1 delete | V0 delete |
|---|---|---|---|---|---|---|
| S1 | 5,000 | 4.9 / 5.9 / 16.1 | 4.2 / 6.1 / 17.4 | 5,000 | 4.6 / 5.4 / 7.6 | 4.2 / 4.9 / 6.6 |
| S2 | 5,000 | 4.6 / 8.1 / 27.2 | 4.2 / 5.2 / 26.3 | 5,000 | 4.5 / 5.9 / 9.4 | 3.9 / 4.6 / 7.2 |
| S3 | 5,000 | 4.7 / 6.2 / 12.2 | 4.5 / 5.7 / 23.1 | 5,000 | 5.0 / 6.1 / 10.9 | 4.1 / 4.9 / 7.0 |
| S4 | 3,000 | 4.7 / 6.5 / 24.1 | 4.0 / 4.8 / 15.2 | 3,000 | 4.8 / 5.7 / 7.6 | 4.2 / 4.5 / 5.6 |
| S5 | 3,000 | 4.8 / 6.0 / 22.7 | 4.3 / 10.1 / 26.8 | 3,000 | 4.7 / 6.1 / 16.0 | 3.7 / 4.1 / 6.1 |

### 10,000-key head-to-head (V0 baseline workload)

V1 values are medians over 5 runs (p50 with min–max over runs); V0 values are from `v0_baseline.json`. µs.

| phase | ops/run | V1 p50 | V1 p95 | V1 p99 | V0 p50 | V0 p95 | V0 p99 |
|---|---|---|---|---|---|---|---|
| PUT, sync=False | 10,000 | 4.2 (3.9–4.4) | 5.3 | 7.9 | 4.0 (3.9–5.1) | 9.6 | 13.1 |
| PUT, sync=True | 300 | 1,898.5 (1,885.2–1,954.2) | 2,254.4 | 3,873.4 | 2,249.3 (2,152.4–2,279.7) | 2,697.4 | 4,148.9 |
| GET hit | 100 | 0.5 (0.4–0.7) | 0.7 | 3.4 | 8,672.1 (7,144.9–9,041.2) | 13,520.6 | 21,358.4 |
| GET miss | 100 | 0.3 (0.3–0.4) | 0.6 | 0.7 | 8,781.1 (7,391.7–9,156.3) | 12,930.7 | 14,119.7 |
| mixed, all ops | 300 | 0.5 (0.4–0.6) | 4.6 | 8.8 | 8,143.5 (7,316.7–8,532.3) | 22,175.2 | 26,209.8 |
| mixed: GET | ~209 | 0.5 (0.4–0.5) | 0.6 | 0.8 | 10,515.4 (8,738.4–10,605.5) | 23,086.2 | 26,209.8 |
| mixed: PUT | ~80 | 4.4 (4.1–4.9) | 6.8 | 28.5 | 15.2 (14.5–16.2) | 38.4 | 46.6 |
| mixed: DELETE | ~11 | 4.4 (4.1–4.9) | 16.8 | 16.8 | 15.5 (13.4–22.5) | 30.8 | 30.8 |

The same workload definitions with more samples (V1 only: 100,000 + 100,000 GETs, 100,000 mixed ops; medians over 5 runs), p50 / p95 / p99 in µs: GET hit 0.4 / 0.5 / 0.7; GET miss 0.3 / 0.4 / 0.5; mixed GET (70,184 ops) 0.4 / 0.6 / 0.8; mixed PUT (24,813) 4.5 / 5.5 / 10.1; mixed DELETE (5,003) 4.4 / 5.2 / 9.1; mixed all ops 0.5 / 5.0 / 5.9; `sync=True` PUT (300 ops, as above) p50 1,883.5 µs.

### Memory (observed process private bytes)

Delta in private bytes of a fresh process from before the load to after loading N keys, and separately after replaying the WAL of N keys. Median (min–max) over 3 probes per scale. "Payload" is the 111 bytes of key + value per key.

| scale | bytes per key after load | bytes per key after replay | total delta after load (MB, median) | delta / payload (after load) | peak working set after load (MB, max of probes) |
|---|---|---|---|---|---|
| S1 | 240.4 (240.4–240.4) | 267.9 (240.8–269.1) | 2 | 2.17 | 23 |
| S2 | 247.5 (246.5–251.7) | 247.7 (246.9–264.6) | 25 | 2.23 | 45 |
| S3 | 236.3 (236.0–236.4) | 236.0 (235.9–237.0) | 236 | 2.13 | 256 |
| S4 | 227.1 (227.1–227.1) | 227.1 (227.0–227.1) | 1,136 | 2.05 | 1,154 |
| S5 | 227.3 (227.3–227.3) | 227.3 (227.3–227.3) | 2,273 | 2.05 | 2,288 |

### Structural metrics

Derived (not timed) by the V1 structural analysis from the record format and the workload (N loaded keys, then 1,000 overwrites, then 1,000 deletes), and cross-checked against the measured WAL sizes, memtable entry counts, tombstone counts and replay record counts in the result file. Definitions: *physical records* = every WAL record; *stale records* = physical records − distinct keys (superseded records still in the WAL); *live keys* = distinct keys not tombstoned; *memtable entries* = distinct keys, tombstoned ones included; *space amplification* = WAL bytes / live user bytes (111 × live keys). This is append-only staleness, not LSM write amplification: V1 never rewrites data.

| scale | WAL bytes after load | physical records final | WAL bytes final | stale records | live keys | tombstoned keys | memtable entries | memtable payload lower bound after load (bytes) | space amplification final |
|---|---|---|---|---|---|---|---|---|---|
| S1 | 1,240,000 | 12,000 | 1,388,000 | 2,000 | 9,039 | 961 | 10,000 | 1,110,000 | 1.3834 |
| S2 | 12,400,000 | 102,000 | 12,548,000 | 2,000 | 99,004 | 996 | 100,000 | 11,100,000 | 1.1418 |
| S3 | 124,000,000 | 1,002,000 | 124,148,000 | 2,000 | 999,002 | 998 | 1,000,000 | 111,000,000 | 1.1196 |
| S4 | 620,000,000 | 5,002,000 | 620,148,000 | 2,000 | 4,999,000 | 1,000 | 5,000,000 | 555,000,000 | 1.1176 |
| S5 | 1,240,000,000 | 10,002,000 | 1,240,148,000 | 2,000 | 9,999,000 | 1,000 | 10,000,000 | 1,110,000,000 | 1.1174 |

- **Format overhead:** a PUT record is 124 bytes for 111 bytes of payload (13-byte header, 10.48% of the record; 1.117 bytes appended per user byte), and a DELETE record is 24 bytes (13-byte header + 11-byte key). Overhead is 13 bytes per physical record.
- **GET WAL work:** V0 scans every physical record and byte per GET (N records, N × 124 bytes at the timed GETs). V1 reads **0 WAL records and 0 WAL bytes** per normal GET, because `get` is one dict lookup. A test enforces this architectural property. It isn't a measurement of physical I/O, which wasn't measured.
- **Recovery work:** the timed open replays N records, reads N × 124 bytes and makes N CRC checks (over N × 120 bytes) while building N memtable entries. Reopening the final WAL replays N + 2,000 records.
- **Memtable payload lower bound** = N × 11 bytes of keys + live keys × 100 bytes of values. It excludes Python object, dict and allocator overhead. The observed memory above is 2.05–2.23 times the 111 bytes per key of payload.

## Analysis

Everything below refers to the measurements above.

1. **GET scaling, V0 vs V1.** V0's median hit latency grows 1,003× across the 1,000× growth in records (log-log slope 1.003 against file size). V1's median hit latency grows from 0.4 to 0.8 µs (2.0×) over the same range, and the log-log slope of V1 p50 against file size is 0.098 (misses: 0.055). Mean hit latency goes from 0.477 to 0.880 µs. The V0 p50 / V1 p50 ratio is 19,540 at S1 and 9,802,071 at S5 for hits (19,853 and 13,169,420 for misses). These ratios use V1 p50 values quantized to 0.1 µs (see Timing floor) and V0 figures from earlier invocations.
2. **Is V1 GET flat?** Nearly, but not exactly. It rises about 2× over three orders of magnitude, against 1,003× for V0. The controls locate the rise: the bare `dict.get` p50 grows from 0.1 to 0.5 µs (mean 0.124 to 0.551), while the gap between store GET and bare `dict.get` stays at about 0.3 µs at every scale. So the growth is in the dict lookup itself, not in V1's wrapper code. The measurements don't say why a dict lookup slows as the dict grows. CPU cache misses on a larger table are a plausible explanation, but it wasn't tested. Misses are faster than hits at the large scales (p50 0.6 vs 0.8 µs at S5; mean 0.605 vs 0.880 µs), and the cause wasn't investigated. For the V1 hit p50 the linear-fit R² is 0.64 (log-log R² 0.96). Because values move in 0.1 µs steps, the fit statistics are crude.
3. **Tail latency.** Pooled p99 is 0.8–1.7 µs for hits and 0.7–1.0 µs for misses, each based on 300,000–500,000 samples. The between-run spread of the per-run hit p99 is 29%, 10%, 36%, 6% and 6% for S1–S5. At these values that's 0.1–0.2 µs, one or two clock ticks, so p99 differences of that size between neighboring scales aren't meaningful. The slowest single GET at each scale is 0.24–1.1 ms, which is consistent with occasional OS scheduling or interrupt delays. Maxima aren't stable statistics.
4. **Startup/recovery cost.** Replay takes 0.02 s at 10,000 records and 11.02 s at 10,000,000, linear in WAL size (linear-fit R² 0.9998; 1.05–1.10 µs per record from S2 up). That's 1.13–1.41× V0's open scan of the same-sized file (V0: 0.77–0.93 µs per record from S2 up). The extra per-record work in V1 is building the memtable, but the benchmark doesn't separate that from the scan. At S5 one V1 open costs 11.02 s, about 1.4 V0 GETs (7.84 s each). By that arithmetic, a workload that does at least two GETs per open is cheaper in total on V1 at S3–S5.
5. **PUT and DELETE.** V1 write p50 is the same as or slightly higher than V0's: load 4.4–4.7 µs vs 4.1–4.7 µs (identical at S1), overwrite 4.6–4.9 µs vs 4.0–4.5 µs, and delete 4.5–5.0 µs vs 3.7–4.2 µs (V0 delete measured in the same invocation; V1/V0 = 1.10–1.27). Load throughput is 192–196k ops/s for V1 vs 204–217k for V0, so V1 is about 4.5–11% lower. The gap is up to about 1 µs per write, and the measurements don't isolate its cause. V1 adds a dict assignment and an extra method layer around the same append and flush. V1's slowest single load PUT grows with N (0.4 ms at S1 to 172.5 ms at S5, against 0.1 to 13.8 ms for V0). That fits memtable dict resizes, but it wasn't isolated. Overwrite p95/p99 are noisy and not consistently different between versions.
6. **Mixed workload and `sync=True`.** At 10,000 keys, V1's mixed-phase GET p50 is 0.5 µs (V0: 10,515 µs), and V1's mixed PUT and DELETE p50 (4.4 µs) equal its standalone write latency. V0's mixed PUT and DELETE p50 (15.2 and 15.5 µs) were about 3.8× its standalone PUT. That wasn't investigated for V0, and the V1 data can't say whether the interleaved multi-millisecond scans are the reason. The `sync=True` PUT p50 is 1,898.5 µs for V1 and 2,249.3 µs for the V0 reference (ratio 1.18). Both versions run the same flush + `fsync` path, the V0 figure comes from an earlier invocation, and `fsync` latency depends on device and OS state, so don't infer any V1 effect.
7. **Memory.** Keeping the current state in RAM costs 227.3 bytes of private memory per key at 10,000,000 keys (2.05× the 111 bytes of payload; 2.27 GB in total, peak working set 2.29 GB) and 236–248 bytes per key at 10,000–1,000,000 keys. Replaying the WAL in a fresh process gives the same per-key figure as loading (227.3 at S5). These are observations for CPython 3.10.6 on Windows with 11-byte keys and 100-byte values.
8. **WAL growth.** The WAL is never reclaimed. After the load it's exactly N × 124 bytes, and 1,000 overwrites plus 1,000 deletes added 148,000 bytes (2,000 stale records). Space amplification is 1.117 after the load and 1.1174 at S5 at the end, but 1.383 at S1, because the same 2,000 extra records are a larger fraction of 10,000 keys (and the deletes remove live keys). The benchmark applies a fixed 2,000 extra mutations, so it doesn't show the effect of a proportional overwrite or delete rate.
9. **Tradeoff.** The read work moved from every GET (V0: a scan costing 0.79 µs per record) to a one-time replay at open (V1: 1.05–1.10 µs per record from S2 up). In exchange, V1 holds all current data in memory (about 227 bytes per key here), still has a WAL that only grows, makes startup proportional to that WAL, and has writes that are equal or slightly slower in these measurements. GET no longer depends on the WAL, but it isn't constant: it's about twice as slow at 10M keys as at 10K.

## Limitations

- **Resolution.** V1 GET latencies are a few clock ticks of 100 ns and include timer overhead. Ratios against V0 are coarse, and the V1 p50 values are upper bounds on store-only cost.
- **Warm cache only.** See [Cache condition](#cache-condition). These numbers say nothing about cold-cache or storage-bound performance, including the cost of replaying a WAL that isn't in the page cache.
- **One machine, one configuration.** Windows 11, Python 3.10.6, one CPU and filesystem. 3 runs at S4 and S5 give only a coarse view of between-run spread. Machine load beyond the available-RAM check wasn't controlled, and Windows Defender and other normal desktop software were left running. Timings are sensitive to machine load (see the V0 limitations).
- **V0 reference.** All V0 comparisons except the delete phase use V0 results from earlier invocations, not from the same session as the V1 runs.
- **Statistics.** Quantized values make log-log and linear fits crude (Analysis item 2). The between-run spread of per-run p99 is up to 36% at S1–S3.
- **Memory.** Private-byte deltas include everything Python allocated between the two readings. They aren't a per-object breakdown and aren't portable across Python versions or key/value sizes.
- **Not measured:** physical I/O, concurrent access, cold-cache replay, a proportional overwrite or delete rate, and other key or value sizes. Raw per-operation latencies aren't stored (the result file keeps summaries, and percentiles are computed over pooled samples).
- **Causes.** Where the text says a cause "fits" or is "plausible" (dict resizes, cache misses, an extra call layer), the benchmark didn't test it.

## Environment

**Environment:** Python 3.10.6 (MSC v.1932, 64-bit); Windows 11 (`Windows-10-10.0.26300-SP0`); Intel Core i7-14650HX (`Intel64 Family 6 Model 183`), 24 logical CPUs; 15.8 GB RAM; seed 42; key 11 B; value 100 B; `sync=False` for the scaling runs.

---

# V2 benchmark

Purpose: characterize the V2 store **as implemented** ([v2-design.md](v2-design.md)): WAL segments + memtable + immutable SSTables, with a **synchronous flush** inside the write that finds the memtable at its limit. V0's and V1's results above are the references and aren't modified. **All numbers are warm OS-cache numbers.** Every figure in this section comes from `benchmarks/results/v2_benchmark.json` or `v2_structural.json`.

## Methodology

**Same as V0/V1:** seed 42, key 11 B, value 100 B, 124-byte PUT records, `sync=False` for the scaling runs, a fresh database in a temp directory for every run, and only the store call timed (a `perf_counter_ns` pair per op). Correctness checks, bookkeeping, close and cleanup aren't timed. Percentiles are nearest-rank using the V0 code, with V0's scale ladder and run counts (5 / 5 / 5 / 3 / 3), and each scale runs as its own fresh Python process, one after another. The hit keys, overwrite keys and values, and delete keys are generated exactly as for V1 (`workload` in the V2 benchmark).

**Configuration:** the store's default flush threshold, `memtable_limit_bytes` = 4 MiB (4,194,304 bytes of key + value payload; 111 bytes per entry here, so a flush every 37,787 entries and tables of 4,685,616 bytes). Nothing was tuned. At 10,000 records the whole database (1,110,000 payload bytes) never reaches the limit, so **S1 never flushes**.

**GET sample counts** differ from V1 because V2 GET cost depends on the data layout. They were fixed before the final run:

| scale | hits per run | misses per run | pooled hit / miss samples |
|---|---|---|---|
| S1 (10,000) | 100,000 | 100,000 | 500,000 / 500,000 |
| S2 (100,000) | 5,000 | 200 | 25,000 / 1,000 |
| S3 (1,000,000) | 2,000 | 100 | 10,000 / 500 |
| S4 (5,000,000) | 1,000 | 50 | 3,000 / 150 |
| S5 (10,000,000) | 1,000 | 50 | 3,000 / 150 |

Misses use V0's per-kind counts from S2 up (a miss in V2 scans every table to its end, like V0's full-file scan, so it costs about as much). S1 has no tables and uses V1's count. Hits are cheaper and get larger counts. Pooled p99 of misses at S4 and S5 rests on 150 samples and is approximate. In this workload keys are loaded in ascending order, so each table covers a contiguous key range, and the hit and miss costs below depend on that (see Limitations). Miss keys (`key_of(N + i)`) are larger than every stored key, as in V0 and V1.

**Flush instrumentation (no production code changed).** The benchmark wraps the store instance's `flush` (which the store calls from `_flush_if_full`) and the module-level `write_sstable`. Before each write it reads the memtable byte count outside the timed region, and if the write will flush, it snapshots the directory first. After the write it checks that the probe saw a flush exactly when the rule predicted one (an assertion that never fired). A **flush-triggering write** is a PUT or DELETE that finds the memtable at its limit and flushes before applying itself, so its latency includes the whole flush. For every flush the result keeps one record: operation index, operation latency, flush duration, time inside `write_sstable` (build + fsync + rename), memtable entries and bytes, table records and bytes, and WAL files and bytes before and after. Percentiles of all, non-flush and flush-triggering writes are computed in-process. Raw per-operation latencies are not stored, except those of flush-triggering writes. The wrappers add two timer pairs, only inside operations that flush.

**Other V2 measurements:**
- *Read work:* per run, 200 hit and 3 miss GETs are repeated untimed with `SSTable.lookup` and the record reader wrapped, counting tables consulted, records examined and bytes scanned. This is application-level work, not physical I/O.
- *Open breakdown:* after the timed open, the steps of the open are repeated with the module's own functions (directory listing, `SSTable` footer validation for every table, WAL replay) to split the time. It's a separate warm repetition, not instrumentation of the timed open.
- *Memory:* 3 untimed probes per scale, each a pair of fresh processes (load N keys; open that database), reading process private bytes before and after, as for V1. The V2 probe uses the same procedure and the same key/value stream, so V1 and V2 deltas are comparable.
- *Correctness:* every GET result is checked. The table layout, WAL sizes and flush counts are asserted after the load, and overwrites and deletes are verified live and after the final reopen (all overwritten and deleted keys plus 200 sampled hit keys).

**10,000-key head-to-head** (`--baseline`): V0's baseline workload and V1's extended workload (see the V1 section) on V2 with the default limit, and V0's baseline workload with a 16 KiB limit (`SMALL_LIMIT_BYTES`). The small limit spreads the 10,000 keys over many tables, so the mixed workload and `sync=True` PUTs run against SSTables. 5 runs each, and every GET is checked against a reference dict.

**References:** V1 values come from `v1_benchmark.json` and V0 values from `v0_scaling.json` / `v0_baseline.json`, read-only. They were measured in earlier invocations, so small differences from them aren't meaningful.

**Timing floor:** as for V1, `perf_counter` ticks every 100 ns. V2 operations that reach a table take milliseconds, but memtable operations are a few ticks.

## Results

### Flush behavior (the `put_load` phase)

Load of N distinct keys, `sync=False`. Flush-triggering writes are pooled over runs, and non-flush percentiles are the median over runs of each run's value. Durations in the second block are the in-store flush time measured by the wrapper.

| scale | flushes per run | flush-triggering writes (pooled) | trigger p50 / p95 / p99 / max (ms) | non-flush p50 / p95 / p99 (µs) | trigger p50 / non-flush p50 |
|---|---|---|---|---|---|
| S1 (10,000) | 0 | 0 | — | 4.6 / 5.8 / 11.2 | — |
| S2 (100,000) | 2 | 10 | 59.0 / 64.1 / 64.1 / 64.1 | 4.8 / 6.1 / 14.0 | 12,283 |
| S3 (1,000,000) | 26 | 130 | 50.5 / 82.1 / 122.4 / 175.2 | 4.8 / 6.2 / 11.4 | 10,518 |
| S4 (5,000,000) | 132 | 396 | 53.2 / 65.1 / 104.3 / 118.0 | 4.8 / 6.1 / 10.3 | 11,081 |
| S5 (10,000,000) | 264 | 792 | 55.8 / 64.5 / 111.7 / 138.2 | 4.8 / 6.0 / 10.1 | 11,630 |

| scale | flush duration p50 / p95 / max (ms) | inside `write_sstable` (median share) | flush share of the triggering write (median) | spacing between flushes (ops) | amortized flush cost (µs per PUT) | non-flush writes at least as slow as the fastest flush-triggering write in the same run |
|---|---|---|---|---|---|---|
| S2 (100,000) | 58.6 / 64.1 / 64.1 | 70% | 99.9% | 37,787 (first at op 37,787) | 1.15 | 0 of 499,990 |
| S3 (1,000,000) | 50.4 / 82.0 / 175.1 | 67% | 99.9% | 37,787 (first at op 37,787) | 1.36 | 2 of 4,999,870 |
| S4 (5,000,000) | 53.1 / 65.0 / 117.9 | 68% | 99.9% | 37,787 (first at op 37,787) | 1.46 | 0 of 14,999,604 |
| S5 (10,000,000) | 55.7 / 64.5 / 138.2 | 69% | 99.9% | 37,787 (first at op 37,787) | 1.48 | 0 of 29,999,208 |

Per-flush time split: the median flush takes 50.4 ms at S3, 53.1 ms at S4 and 55.7 ms at S5. Of that, `write_sstable` (write, fsync, rename) is 33.4, 37.1 and 38.9 ms, and the remainder (16.7, 17.0 and 17.0 ms) is everything else in `flush`: sorting the memtable, opening the new table, creating the new WAL segment, closing and fsyncing the old one, and deleting it. The remainder wasn't broken down further. With the 16 KiB limit (tables of 18,380 bytes) the median flush is 14.3 ms, of which 11.7 ms is outside `write_sstable`.

### Write latency, V2 vs V1 (`put_load`, all writes including flush-triggering ones)

| scale | V2 p50 / p95 / p99 (µs) | V1 p50 / p95 / p99 (µs) | V2 ops/s | V1 ops/s | V2 / V1 | V2 slowest write (ms) | V1 slowest write (ms) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 4.6 / 5.8 / 11.2 | 4.7 / 5.6 / 15.2 | 190,458 | 194,314 | 0.98 | 1.3 | 0.4 |
| S2 (100,000) | 4.8 / 6.1 / 14.1 | 4.4 / 5.7 / 13.8 | 155,797 | 195,597 | 0.80 | 59.0 | 1.8 |
| S3 (1,000,000) | 4.8 / 6.2 / 11.4 | 4.6 / 6.0 / 12.7 | 150,075 | 192,294 | 0.78 | 122.4 | 15.5 |
| S4 (5,000,000) | 4.8 / 6.1 / 10.3 | 4.5 / 5.9 / 14.1 | 149,882 | 194,561 | 0.77 | 104.5 | 76.6 |
| S5 (10,000,000) | 4.8 / 6.0 / 10.1 | 4.6 / 6.1 / 11.6 | 149,106 | 192,683 | 0.77 | 131.7 | 172.5 |

(Slowest write: median over runs of each run's maximum.)

**Overwrite and delete** (1,000 each per run, µs, p50 / p95 / p99, pooled over runs). No overwrite or delete triggered a flush in any run at any scale. The memtable held between 12,116 and 24,426 entries after the load, and the 2,000 extra writes stayed under the limit. So the overwrite and delete phases contain no flush-triggering writes, and **a DELETE that triggers a flush was never observed in the scaling runs**.

| scale | V2 overwrite | V1 overwrite | V2 delete | V1 delete |
|---|---|---|---|---|
| S1 (10,000) | 5.0 / 6.0 / 15.5 | 4.9 / 5.9 / 16.1 | 5.0 / 5.7 / 9.3 | 4.6 / 5.4 / 7.6 |
| S2 (100,000) | 5.6 / 7.0 / 19.0 | 4.6 / 8.1 / 27.2 | 5.1 / 5.9 / 7.8 | 4.5 / 5.9 / 9.4 |
| S3 (1,000,000) | 5.6 / 7.3 / 20.5 | 4.7 / 6.2 / 12.2 | 5.0 / 5.9 / 8.2 | 5.0 / 6.1 / 10.9 |
| S4 (5,000,000) | 5.1 / 6.1 / 16.0 | 4.7 / 6.5 / 24.1 | 4.5 / 5.3 / 8.3 | 4.8 / 5.7 / 7.6 |
| S5 (10,000,000) | 5.0 / 7.8 / 23.3 | 4.8 / 6.0 / 22.7 | 4.7 / 5.8 / 9.8 | 4.7 / 6.1 / 16.0 |

### GET

V2 percentiles are pooled over all runs at the scale. µs unless noted.

**GET hit**

| scale | samples | V2 p50 | p95 | p99 | max | mean | V1 p50 | V0 p50 (ms) | V2 p50 / V0 p50 |
|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.4 | 0.6 | 0.7 | 203 | 0.4 | 0.4 | 7.8 | 0.0001 |
| S2 (100,000) | 25,000 | 17,902.2 | 48,398.7 | 53,719.7 | 72,867 | 19,917.7 | 0.5 | 80.7 | 0.22 |
| S3 (1,000,000) | 10,000 | 24,978.3 | 48,292.8 | 52,936.1 | 64,091 | 25,098.4 | 0.7 | 813.0 | 0.0307 |
| S4 (5,000,000) | 3,000 | 29,844.3 | 51,565.9 | 55,890.3 | 69,932 | 28,896.4 | 0.7 | 4,100.7 | 0.0073 |
| S5 (10,000,000) | 3,000 | 29,664.4 | 54,513.8 | 60,100.8 | 64,514 | 30,377.5 | 0.8 | 7,841.7 | 0.0038 |

**GET miss**

| scale | samples | V2 p50 | p95 | p99 | max | mean | V1 p50 | V0 p50 (ms) | V2 p50 / V0 p50 |
|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.4 | 0.5 | 0.6 | 156 | 0.4 | 0.4 | 7.9 | 0.0001 |
| S2 (100,000) | 1,000 | 100,632.2 | 111,010.2 | 114,326.4 | 126,058 | 101,229.8 | 0.4 | 81.5 | 1.24 |
| S3 (1,000,000) | 500 | 1,307,443.2 | 1,366,791.5 | 1,392,107.7 | 1,882,698 | 1,307,980.9 | 0.5 | 825.7 | 1.58 |
| S4 (5,000,000) | 150 | 6,568,458.3 | 6,812,175.9 | 6,895,766.1 | 6,921,543 | 6,575,770.5 | 0.5 | 4,051.0 | 1.62 |
| S5 (10,000,000) | 150 | 13,167,708.6 | 13,675,204.8 | 13,887,549.9 | 13,889,142 | 13,223,037.6 | 0.6 | 7,901.7 | 1.67 |

Miss cost per stored record at the median: 1.01 µs (S2), 1.31 µs (S3), 1.31 µs (S4) and 1.32 µs (S5). V0's full-file scan cost 0.81, 0.83, 0.81 and 0.79 µs per record at the same scales.

### Read work per GET (application-level, derived and measured)

Derived by the V2 structural analysis over the timed GET workload. The instrumented sample (200 hits and 3 misses per run) was **asserted equal to the derived values for every sampled GET in every run**. "Records examined" counts records the scan read and checksummed. V1 reads no WAL and no table records on a hit or a miss that the memtable answers.

| scale | tables | hits answered by memtable / newest SSTable / older SSTable | tables consulted per hit (mean) | records examined per hit (mean) | bytes scanned per hit (mean, MB) | tables consulted per miss | records examined per miss | bytes scanned per miss (MB) |
|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 100.0% / 0.0% / 0.0% | 0.0 | 0 | 0.00 | 0 | 0 | 0.0 |
| S2 (100,000) | 2 | 23.8% / 38.0% / 38.1% | 1.1 | 14,558 | 1.81 | 2 | 75,574 | 9.4 |
| S3 (1,000,000) | 26 | 2.1% / 3.5% / 94.5% | 13.1 | 18,550 | 2.30 | 26 | 982,462 | 121.8 |
| S4 (5,000,000) | 132 | 0.2% / 1.1% / 98.7% | 67.3 | 19,674 | 2.44 | 132 | 4,987,884 | 618.5 |
| S5 (10,000,000) | 264 | 0.3% / 0.4% / 99.3% | 135.2 | 19,038 | 2.36 | 264 | 9,975,768 | 1,237.0 |

### Open / recovery

`KVStore(path)` on the closed database after the load: footer validation of every table plus replay of the surviving WAL. The last column reopens after the overwrites and deletes. The component columns come from a separate warm repetition of the same steps (see Methodology) and don't add up to the timed total. The difference is the rest of the open (temporary-file cleanup, directory listings, opening the WAL, memtable accounting) plus first-open effects, which weren't separated.

| scale | SSTables validated | WAL records replayed | V2 open per run (ms) | V2 median (ms) | listing / footers / WAL replay (ms, median) | V1 median (s) | V2 / V1 | V2 reopen after mutations (ms, median) |
|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 10,000 | 21, 22, 19, 19, 21 | 21.3 | 0.06 / 0.00 / 9.1 | 0.020 | 1.0767 | 19.0 |
| S2 (100,000) | 2 | 24,426 | 33, 38, 32, 33, 33 | 33.0 | 0.07 / 0.18 / 23.2 | 0.105 | 0.3141 | 39.2 |
| S3 (1,000,000) | 26 | 17,538 | 26, 29, 31, 28, 29 | 29.0 | 0.14 / 1.86 / 14.9 | 1.061 | 0.0273 | 32.3 |
| S4 (5,000,000) | 132 | 12,116 | 32, 33, 34 | 32.9 | 1.10 / 8.18 / 10.3 | 5.353 | 0.0062 | 37.2 |
| S5 (10,000,000) | 264 | 24,232 | 74, 78, 79 | 78.1 | 0.60 / 16.92 / 21.2 | 11.022 | 0.0071 | 68.2 |

### Memory (observed process private bytes)

Delta in private bytes of a fresh process from before the load to after loading N keys, and separately after opening the resulting database (median and min–max over 3 probes). The memtable at the end of a load holds whatever was written since the last flush, which differs by scale (the entries column).

| scale | SSTables | memtable entries at probe | V2 delta after load (MB) | V2 delta after open (MB) | V1 delta after load (MB) | V2 / V1 | peak working set after load (MB, max of probes) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 10,000 | 1.7 (1.6–1.8) | 1.4 (1.4–1.6) | 2.4 | 0.704 | 24.0 |
| S2 (100,000) | 2 | 24,426 | 7.4 (7.2–7.6) | 5.2 (4.9–5.3) | 24.8 | 0.297 | 33.0 |
| S3 (1,000,000) | 26 | 17,538 | 8.6 (8.6–8.7) | 4.4 (2.7–5.0) | 236.3 | 0.036 | 33.4 |
| S4 (5,000,000) | 132 | 12,116 | 11.1 (10.3–11.5) | 3.9 (3.8–4.1) | 1,135.7 | 0.010 | 34.6 |
| S5 (10,000,000) | 264 | 24,232 | 11.5 (11.0–12.1) | 5.4 (4.3–5.4) | 2,272.5 | 0.005 | 34.5 |

### Structural metrics

Derived (not timed) by the V2 structural analysis and **asserted equal to the measured values in every run**: the SSTable layout (count, records and bytes of every table), the flush sequence (operation index, memtable entries and bytes, table size, WAL bytes before the flush), the WAL sizes after each phase, the total WAL bytes appended, and the work of the instrumented GETs. Definitions: *space amplification* = (SSTable bytes + surviving WAL bytes) / live user payload (111 × live keys); *stale records* = physical records (in tables and the WAL) − distinct keys; *rewrite factor* = (WAL bytes appended + SSTable bytes written) / the bytes of one encoded record per mutation, i.e. how many times a record's bytes reach a file (tables are written once); *bytes per payload byte* = the same numerator over key + value bytes, which includes the 13-byte record header. The rewrite factor is the structural write-amplification measure for V2. The record-format overhead (124 / 111 = 1.117) is a separate factor. The measurement point is the end of the delete phase, before close.

| scale | SSTables | table size (bytes) | SSTable bytes | SSTable records | footer bytes | surviving WAL bytes | WAL bytes before each flush | WAL segments after each flush | total persistent bytes | space amplification (V1) | stale records |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | — | 0 | 0 | 0 | 1,388,000 | — | 1 (no flush) | 1,388,000 | 1.3834 (1.3834) | 2,000 |
| S2 (100,000) | 2 | 4,685,616 | 9,371,232 | 75,574 | 56 | 3,176,824 | 4,685,588 | 1 | 12,548,056 | 1.1418 (1.1418) | 2,000 |
| S3 (1,000,000) | 26 | 4,685,616 | 121,826,016 | 982,462 | 728 | 2,322,712 | 4,685,588 | 1 | 124,148,728 | 1.1196 (1.1196) | 2,000 |
| S4 (5,000,000) | 132 | 4,685,616 | 618,501,312 | 4,987,884 | 3,696 | 1,650,384 | 4,685,588 | 1 | 620,151,696 | 1.1176 (1.1176) | 2,000 |
| S5 (10,000,000) | 264 | 4,685,616 | 1,237,002,624 | 9,975,768 | 7,392 | 3,152,768 | 4,685,588 | 1 | 1,240,155,392 | 1.1174 (1.1174) | 2,000 |

| scale | logical payload bytes | logical record bytes (= V1's WAL bytes) | WAL bytes appended | SSTable bytes written | bytes per payload byte | rewrite factor | records flushed / records written |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 1,232,000 | 1,388,000 | 1,388,000 | 0 | 1.1266 | 1.0000 | 0 / 12,000 |
| S2 (100,000) | 11,222,000 | 12,548,000 | 12,548,000 | 9,371,232 | 1.9532 | 1.7468 | 75,574 / 102,000 |
| S3 (1,000,000) | 111,122,000 | 124,148,000 | 124,148,000 | 121,826,016 | 2.2135 | 1.9813 | 982,462 / 1,002,000 |
| S4 (5,000,000) | 555,122,000 | 620,148,000 | 620,148,000 | 618,501,312 | 2.2313 | 1.9973 | 4,987,884 / 5,002,000 |
| S5 (10,000,000) | 1,110,122,000 | 1,240,148,000 | 1,240,148,000 | 1,237,002,624 | 2.2314 | 1.9975 | 9,975,768 / 10,002,000 |

All load tables have identical size (every flush happens after exactly 37,787 entries), so the table-size distribution is a single value, and the overwrite and delete phases wrote no tables. The WAL is reclaimed at every flush: every flush record shows one WAL file before and after, and the WAL bytes before each flush are the bytes of exactly one table's worth of records.

### 10,000-key head-to-head

V2 with the default limit on V0's baseline workload and on V1's extended workload, compared with V1 on the same workloads (medians over 5 runs; µs). No flush occurs in these runs (the database is 1.24 MB), so they compare V2's memtable-only path with V1's.

| phase | V2 p50 / p95 / p99 | V1 p50 / p95 / p99 | V2 p50 / V1 p50 |
|---|---|---|---|
| baseline workload: get_hit | 0.5 / 0.7 / 2.5 | 0.5 / 0.7 / 3.4 | 1.00 |
| baseline workload: get_miss | 0.4 / 0.6 / 2.3 | 0.3 / 0.6 / 0.7 | 1.33 |
| baseline workload: mixed_all | 0.5 / 5.4 / 7.1 | 0.5 / 4.6 / 8.8 | 1.00 |
| baseline workload: mixed_get | 0.5 / 0.7 / 0.8 | 0.5 / 0.6 / 0.8 | 1.00 |
| baseline workload: mixed_put | 5.0 / 6.9 / 24.6 | 4.4 / 6.8 / 28.5 | 1.14 |
| baseline workload: mixed_delete | 5.1 / 14.5 / 14.5 | 4.4 / 16.8 / 16.8 | 1.16 |
| baseline workload: put_load | 4.5 / 5.4 / 12.5 | 4.2 / 5.3 / 7.9 | 1.07 |
| baseline workload: put_sync | 1914.4 / 2178.7 / 3775.4 | 1898.5 / 2254.4 / 3873.4 | 1.01 |
| extended workload: get_hit | 0.4 / 0.5 / 0.6 | 0.4 / 0.5 / 0.7 | 1.00 |
| extended workload: get_miss | 0.4 / 0.5 / 0.6 | 0.3 / 0.4 / 0.5 | 1.33 |
| extended workload: mixed_all | 0.5 / 5.8 / 7.1 | 0.5 / 5.0 / 5.9 | 1.00 |
| extended workload: mixed_get | 0.5 / 0.7 / 0.9 | 0.4 / 0.6 / 0.8 | 1.25 |
| extended workload: mixed_put | 4.9 / 6.7 / 16.7 | 4.5 / 5.5 / 10.1 | 1.09 |
| extended workload: mixed_delete | 4.8 / 6.4 / 12.0 | 4.4 / 5.2 / 9.1 | 1.09 |
| extended workload: put_load | 4.5 / 5.5 / 13.7 | 4.7 / 5.8 / 10.5 | 0.96 |
| extended workload: put_sync | 1906.5 / 2096.0 / 3899.1 | 1883.5 / 2177.3 / 3962.2 | 1.01 |

**With a 16 KiB limit** (tables of 18,380 bytes; the 10,000 keys end up in 67 tables after the load; flushes per run: 67 during the load, 1 during the mixed PUTs, 0 during the mixed DELETEs, 2 during the 300 `sync=True` PUTs). V0's baseline workload; V1 values from the V1 baseline run; µs.

| phase | V2 p50 / p95 / p99 | V1 p50 / p95 / p99 | note |
|---|---|---|---|
| put_load | 4.6 / 10.3 / 89.7 | 4.2 / 5.3 / 7.9 | flush-triggering writes: p50 14,496 µs (5 of 5 runs had one) |
| put_sync | 1,906.9 / 2,249.3 / 4,071.7 | 1898.5 / 2254.4 / 3873.4 | flush-triggering writes: p50 20,789 µs (5 of 5 runs had one) |
| get_hit | 1,288.3 / 2,879.7 / 3,255.7 | 0.5 / 0.7 / 3.4 |  |
| get_miss | 18,380.3 / 20,952.4 / 21,916.5 | 0.3 / 0.6 / 0.7 |  |
| mixed_all | 857.5 / 2,757.8 / 3,248.9 | 0.5 / 4.6 / 8.8 |  |
| mixed_get | 1,422.4 / 2,848.6 / 3,150.5 | 0.5 / 0.6 / 0.8 |  |
| mixed_put | 12.0 / 46.7 / 29,144.3 | 4.4 / 6.8 / 28.5 | flush-triggering writes: p50 29,144 µs (5 of 5 runs had one) |
| mixed_delete | 12.9 / 21.5 / 21.5 | 4.4 / 16.8 / 16.8 |  |

## Analysis

Everything below refers to the measurements above.

1. **Flush behavior: periodic spikes on a flat baseline.** Writes that trigger a flush take 50–59 ms at the median (p99 64–122 ms, slowest 175 ms), against a non-flush median of 4.8 µs. A flush-triggering write is about 10,518–12,283 times a normal PUT, and the flush accounts for 99.9% or more of that write's latency. The spikes are exactly periodic (37,787 operations apart at every scale). Spike and baseline are cleanly separated: out of 50,498,672 non-flush writes in all runs, 2 were as slow as the fastest flush-triggering write of their run (all in one run at S3, where a single non-flush write took 234 ms, an unexplained stall). In every other run the slowest non-flush write was faster than the fastest flush. Because the pattern is isolated spikes at a fixed interval, "periodic spikes" describes it. This benchmark didn't measure a within-cycle ramp, so it doesn't establish a sawtooth in the sense of latency growing between flushes.
2. **Flush cost is nearly independent of database size and mostly fixed overhead.** The median flush is 50, 53 and 56 ms at S3, S4 and S5, with the same 4,685,616-byte table every time. At the 16 KiB limit (a table 255 times smaller) it's 14.3 ms. A 255× larger table costs 3.9× the time, so most of a small flush isn't proportional to table size. The part outside `write_sstable` is 12 ms for the small tables and 17, 17 and 17 ms for the large ones. Why isn't known: fsync and file creation/deletion on this filesystem with Defender running are candidates, but they weren't tested. Only these two table sizes were measured.
3. **Write throughput and latency vs V1.** Between flushes a V2 write costs about the same as a V1 write (non-flush p50 4.8 µs vs V1 4.6 µs at S5). Averaged over the load, the flush adds 1.15–1.48 µs per PUT (1.48 at S5). That matches the throughput gap, since the mean time per PUT is 1.52 µs higher than V1's at S5: V2 sustained 149,106 ops/s against V1's 192,683 at S5 (0.77×; 0.77–0.78× at S3–S5). The slowest single write at S5 is 132 ms for V2 (always a flush) and 173 ms for V1 (V1's slowest write grew with N, and the V1 section notes this fits memtable dict resizes, which wasn't isolated). At S2 V2's slowest write (59 ms) is 33× V1's (1.8 ms). Overwrite and delete p50 are 1.02–1.22× (overwrite) and 0.94–1.13× (delete) of V1's. Those phases never flushed, so they compare only the memtable-resident write path, in invocations made at different times.
4. **GET hits.** V2 hit p50 is 17.9 ms at S2 and 29.7 ms at S5, against V1's 0.5–0.8 µs (V1 never reads files on a GET) and V0's 80.7 ms to 7,842 ms. Against V0 a V2 hit is 0.22× at S2 and 0.0038× at S5, because a hit scans about half a table (19,038 records on average at S5) instead of the whole file. Across the scales that have tables, the log-log slope of the V2 hit p50 against the record count is 0.115 (R² 0.96). The p50 grows 1.66× while the record count grows 100×, because the number of records examined per hit stays at 18,550–19,674 from S3 up and only the number of newer tables opened (one record each) grows (13.1 at S3, 135.2 at S5). The fit over all five scales isn't meaningful, since S1 has no tables and is a memtable lookup identical to V1 (p50 0.4 µs). In this layout a hit is cheap because newer tables are skipped after one record (see Limitations). The p99 of a hit is 2.0× its p50 at S5.
5. **GET misses.** A miss scans every record of every table, so it grows linearly with the data: the log-log slope of the miss p50 against the record count over the scales with tables is 1.06 (R² 0.9992), at 1.32 µs per record. A miss takes 13.2 s at S5. **That is 1.58–1.67× V0's GET at S3–S5 (1.67× at S5), so V2 is slower than V0 for a key that isn't present.** V2 examines almost the same records as V0's scan (9,975,768 at S5, the flushed ones, against V0's 10,000,000), with 264 files opened instead of one, and the per-record cost measured here (1.32 µs) is higher than V0's (0.79 µs). Why wasn't tested. Extra work per record in the table reader (the bounded view, the key-order check) and the file opens are candidates. The S2 miss (101 ms) is 1.24× V0's.
6. **Recovery.** V2's open takes 29–78 ms from S2 to S5, against V1's 0.11–11.0 s (0.31× at S2, 0.0071× at S5). It stays within a narrow band because the WAL it replays is only the unflushed tail (12,116–24,426 records here, which varies with where the last flush fell), plus one footer read per table. The footer validation grows with the table count (16.9 ms for 264 tables at S5, about 64 µs per table). The open at S5 (78 ms) is the largest and is 2.4× that at S4, and the measured components account for about 50% of it. The rest wasn't attributed. These opens last tens of milliseconds, so the between-run spread (6–19%) is large in relative terms. At S1 nothing was flushed and V2's open (21 ms) equals V1's (20 ms), since it replays the whole WAL. This is a warm-cache measurement of a clean shutdown, and it doesn't include the cost of a first GET after the open (which scans tables, above).
7. **Memory.** The private-memory delta after loading doesn't follow the database size: 1.7, 7.4, 8.6, 11.1 and 11.5 MB at S1, S2, S3, S4 and S5, against V1's 2, 25, 236, 1,136 and 2,273 MB (V2 / V1 = 0.704, 0.297, 0.036, 0.010, 0.005). From S3 to S5 the database grows 10× and the delta grows 1.34×. The numbers aren't a clean function of N. They depend on how full the memtable is when the probe ends (the entries column) and on allocator state after flushes, and a straight-line fit against N has R² 0.55. After open the delta is 3.9–5.4 MB at S2–S5. The V2 process peak working set after load is 33–35 MB for S2–S5, including the interpreter. These are observations for CPython 3.10.6 on Windows with this key/value shape.
8. **WAL reclamation and SSTable growth.** The WAL never holds more than one table's worth of records. It was 4,685,588 bytes before every flush and one segment at all times, ending at 3,152,768 bytes at S5, against V1's WAL of 1,240,148,000 bytes. The table count grows linearly with the data (0, 2, 26, 132 and 264 tables at S1, S2, S3, S4 and S5), and tables are never merged or deleted.
9. **Space and write amplification.** Total persistent bytes are 1,240,155,392 at S5 and space amplification is 1.1174, the same as V1's (1.1174), because every record is stored once, in a table or in the WAL. The 2,000 stale records (one per overwrite and delete) are the only duplication, and each table adds a 28-byte footer (7,392 bytes in all at S5). Each record's bytes are written twice, once to the WAL and once to a table. The rewrite factor is 1.000, 1.747, 1.981, 1.997 and 1.997 at S1, S2, S3, S4 and S5 (V1: 1 by definition, one WAL copy), and V2 appends 2.23 bytes per payload byte against V1's 1.12. The factor tends to 2 as the unflushed WAL tail becomes a smaller share of the data, and at S1, which never flushes, it's 1. No data is rewritten beyond this one copy.
10. **10,000-key workloads.** Without a flush, V2's operations are within noise of V1's, or slightly higher on writes (PUT p50 4.5 vs 4.2 µs; `sync=True` PUT 1,914 vs 1,898 µs; mixed PUT 5.0 vs 4.4 µs). Reads are within one 100 ns clock tick of V1's at the median (the 100,000-miss p50 is 0.4 µs against V1's 0.3 µs). With the 16 KiB limit the same workload shows the cost of tables: GET hit p50 1.29 ms and miss 18.4 ms (V1: 0.5 and 0.3 µs), and a mixed workload whose GETs take 1.42 ms at the median. A `sync=True` PUT that triggers a flush took a median 20.8 ms against 1.91 ms for one that didn't.
11. **Tradeoffs.** V2 trades read cost and write latency for memory and recovery. On the plus side, it bounds memory (a few MB of private bytes after the load at every scale measured, against V1's growth with the key count), cuts open time to tens of milliseconds, and reclaims the WAL. On the cost side, it has milliseconds-to-seconds GETs that reach the tables (a hit about 30 ms and a miss 13 s at S5, where V1 answers in under a microsecond), a miss that's slower than V0's full scan, periodic write stalls of about 50 ms, roughly 1.29× the write time per record on a sustained load, and twice the bytes written. V2 isn't better than V1 on every metric. On read latency it's far worse once data leaves the memtable, and relative to V0 it improves hits and worsens misses.

## Limitations

- **Workload layout.** Keys are loaded in ascending order, so tables hold disjoint, ordered key ranges and a hit skips every newer table after one record. A load in random key order would make table key ranges overlap and change the hit cost, but that wasn't measured. Miss keys exceed every stored key, so a miss scans everything. A miss key inside the key range would stop early in each table that has a larger key. Hits are uniformly distributed over the keyspace.
- **Memtable-resident keys.** At the end of the load, 0.2–24% of the keys sit in the memtable and are answered without file access (the percentages in the read-work table). The overwrite and delete phases also hit the memtable.
- **One configuration.** Only the default 4 MiB limit was measured at scale, plus one 16 KiB limit on the 10,000-key workloads. Flush frequency, table counts and GET cost all depend on it.
- **Machine state.** The operator kept the machine idle during the ladder (not independently verified). Windows Defender and other resident security software stayed enabled, no CPU affinity was set, and timings remain sensitive to machine load (see the V0 and V1 limitations).
- **Resolution and caches.** As for V1. All numbers are warm-cache, so nothing here says anything about cold reads of tables.
- **Delete-triggered flushes.** None occurred in any scaling run, so the flush-triggering DELETE latency wasn't measured at scale. In the 16 KiB runs, flush-triggering writes came from PUTs.
- **Open breakdown.** The component times are from a separate warm repetition and cover only part of the timed open.
- **Memory.** Private-byte deltas depend on the memtable fill level and allocator state at the moment of reading, so they aren't a function of N alone. The V2 deltas are a few MB, and the three probes at a scale differ by up to 1.2 MB.
- **Statistics.** Tails of misses at S4 and S5 rest on 150 samples, open times of tens of milliseconds have large relative spread, and the V0 and V1 references come from earlier invocations. In one run at S3, a non-flush write stalled for 234 ms for a reason that wasn't identified.
- **Causes.** Where the text says a cause is a candidate (file opens, extra per-record work, fixed costs of fsync/creation/deletion), the benchmark didn't test it.
- **Not measured:** physical I/O, concurrent access, cold-cache behavior, a random-order load, other flush limits at scale, flush-triggering deletes, and sustained workloads longer than the ladder.

## Environment

**Environment:** Python 3.10.6 (MSC v.1932, 64-bit); Windows 11 (`Windows-10-10.0.26300-SP0`); Intel Core i7-14650HX (`Intel64 Family 6 Model 183`), 24 logical CPUs; 15.8 GB RAM; seed 42; key 11 B; value 100 B; `sync=False` for the scaling runs; default `memtable_limit_bytes` = 4 MiB.

# V3 benchmark

Purpose: measure what the sparse index buys over V2 ([v3-design.md](v3-design.md)). V3 has the same WAL, memtable and synchronous flush, but every SSTable carries one index entry per **16 records** (the store's default interval, not tuned). The main question is whether the index reduces V2's SSTable read amplification and improves GET performance. V0, V1 and V2 results are references and aren't modified. **All numbers are warm OS-cache numbers.** Every figure in this section comes from `benchmarks/results/v3_benchmark.json`, `v3_structural.json` and the V2 reference `v2_benchmark.json`.

## Methodology

**Identical to V2:** seed 42, key 11 B, value 100 B, 124-byte PUT records, `sync=False` for the scaling runs, a fresh database in a temp directory for every run, and only the store call timed (a `perf_counter_ns` pair per operation). Correctness checks, bookkeeping, close and cleanup aren't timed. Percentiles are nearest-rank using the V0 code. The scale ladder is 10,000, 100,000, 1,000,000, 5,000,000 and 10,000,000 records with 5, 5, 5, 3 and 3 runs, and each scale runs in its own fresh Python process, one after another on an idle machine. The memtable limit is V2's 4 MiB (4,194,304 bytes of key + value payload, 111 bytes per entry here, so 37,787 entries per table and a flush every 37,787 writes). Hit, overwrite and delete keys are generated by the same `workload` in the V3 benchmark, so V2 and V3 GET the **same keys**. GET sample counts per scale are V2's: hits per run 100,000, 5,000, 2,000, 1,000 and 1,000, and misses per run 100,000, 200, 100, 50 and 50. The index interval is 16 (the `KVStore` default), asserted on the store and on every table written. V2 reference values are read from `v2_benchmark.json` (measured in an earlier invocation).

**Workload semantics that determine what a GET costs (unchanged from V2).** Keys are loaded in ascending order, so every table holds a disjoint, ascending key range and newer tables hold larger keys. *Hits* are drawn uniformly from the loaded keyspace. A hit on a key in table *j* consults every newer table (each is excluded by the index because the key sorts below that table's first key, with no file access) and then opens one block of table *j*. A hit on a key still in the memtable consults no table. *Misses* use keys above the stored range (`key_of(N + i)`), so the key sorts after every table's last record. The index selects the **last block of every table**, and a miss opens one block in each table. The miss workload is therefore the worst case for the number of tables opened, and the hit workload is the best case for tables skipped (see Limitations).

**V3-specific measurements (all untimed; none inside a timed phase):**
- *Read work per GET:* per run, 200 hit and 3 miss GETs are repeated with `SSTable.lookup`, `SSTable.locate`, `SSTable._scan_block` and the record reader wrapped (monkeypatched, no production code changed). This counts tables consulted (`lookup` calls), tables ruled out by the index with no file access, blocks opened, records decoded and bytes decoded. It's application-level work, not physical I/O (the OS and Python's file buffering may read more than a block). The V3 structural analysis derives the same counts from the layout and asserts equality for every sampled GET of every run.
- *Cost decomposition:* after all timed phases of each run, the cost of one table lookup is split into `locate` (index search), opening and closing the file, opening + seeking + reading the located block's bytes, and the full `SSTable.lookup`. It uses 20 repetitions over all tables with a miss key and 10 repetitions over up to 200 (table, hit key) pairs, with one clock pair per loop, the median over repetitions, then the median over runs.
- *Index memory:* `tracemalloc` bytes retained by opening all tables of the finished database (the in-memory indexes plus the `SSTable` objects), and process private bytes in the same fresh-process load / open probes as V2 (3 per scale).
- *Open work:* tables opened, index entries and bytes loaded, footer bytes read, and V2's open-component repetition plus a re-read of the index bytes without parsing.
- *Flush instrumentation:* as in V2, except that the wrapped function is `lsm_store.sstable.write_sstable` (V3 calls it through the module). Each flush records the interval its table was written with.
- *Correctness:* every GET result is checked. Layout, index sizes, WAL sizes and flush counts are asserted after the load, and overwrites and deletes are verified live and after the final reopen.

**10,000-key head-to-head** (`--baseline`): V0's baseline workload and V1's extended workload with the default limit (no flush occurs), and V0's baseline workload with the 16 KiB limit. The small limit spreads the 10,000 keys over 67 tables of 148 records, so GETs, the mixed workload and `sync=True` PUTs run against SSTables. 5 runs each, and every GET is checked against a reference dict.

**Timing floor:** `perf_counter` ticks every 100 ns, so sub-microsecond memtable operations are quantized. Operations that reach a table take tens of microseconds or more.

## Results

### Table structure and index size

Derived by the V3 structural analysis and asserted equal to the measured layout in every run: each flushed table holds 37,787 records, 2,362 index entries (54,326 bytes, 23 bytes per entry) and a 44-byte footer. The table count equals V2's at every scale.

| scale | SSTables | index entries per table | index entries (total) | index bytes (total) | SSTable bytes (total, incl. index and footers) | index share of SSTable bytes | index bytes per table record |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | — | 0 | 0 | 0 | — | — |
| S2 (100,000) | 2 | 2,362 | 4,724 | 108,652 | 9,479,916 | 1.15% | 1.438 |
| S3 (1,000,000) | 26 | 2,362 | 61,412 | 1,412,476 | 123,238,908 | 1.15% | 1.438 |
| S4 (5,000,000) | 132 | 2,362 | 311,784 | 7,171,032 | 625,674,456 | 1.15% | 1.438 |
| S5 (10,000,000) | 264 | 2,362 | 623,568 | 14,342,064 | 1,251,348,912 | 1.15% | 1.438 |

### GET

V3 and V2 percentiles are pooled over all runs at each scale (µs). Change = V3 relative to V2 (negative = faster). Ratios are computed from unrounded measured values.

**GET hit**

| scale | samples (pooled) | V3 p50 / p95 / p99 (µs) | V2 p50 / p95 / p99 (µs) | V3 / V2 (p50 / p95 / p99) | change (p50 / p95 / p99) | V3 max (µs) | V3 mean (µs) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.4 / 0.6 / 0.8 | 0.4 / 0.6 / 0.7 | 1.00 / 1.00 / 1.14 | +0.0% / +0.0% / +14.3% | 171.9 | 0.5 |
| S2 (100,000) | 25,000 | 46.6 / 76.0 / 154.9 | 17,902 / 48,399 / 53,720 | 0.00 / 0.00 / 0.00 | -99.7% / -99.8% / -99.7% | 610.7 | 42.2 |
| S3 (1,000,000) | 10,000 | 54.2 / 84.6 / 174.1 | 24,978 / 48,293 / 52,936 | 0.00 / 0.00 / 0.00 | -99.8% / -99.8% / -99.7% | 623.7 | 58.4 |
| S4 (5,000,000) | 3,000 | 76.8 / 135.9 / 271.8 | 29,844 / 51,566 / 55,890 | 0.00 / 0.00 / 0.00 | -99.7% / -99.7% / -99.5% | 529.8 | 84.3 |
| S5 (10,000,000) | 3,000 | 107.7 / 185.9 / 286.0 | 29,664 / 54,514 / 60,101 | 0.00 / 0.00 / 0.00 | -99.6% / -99.7% / -99.5% | 689.0 | 114.2 |

**GET miss**

| scale | samples (pooled) | V3 p50 / p95 / p99 (µs) | V2 p50 / p95 / p99 (µs) | V3 / V2 (p50 / p95 / p99) | change (p50 / p95 / p99) | V3 max (µs) | V3 mean (µs) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.4 / 0.6 / 0.9 | 0.4 / 0.5 / 0.6 | 1.00 / 1.20 / 1.50 | +0.0% / +20.0% / +50.0% | 206.4 | 0.5 |
| S2 (100,000) | 1,000 | 103.7 / 173.3 / 315.4 | 100,632 / 111,010 / 114,326 | 0.00 / 0.00 / 0.00 | -99.9% / -99.8% / -99.7% | 506.3 | 111.7 |
| S3 (1,000,000) | 500 | 1,375 / 1,871 / 2,276 | 1,307,443 / 1,366,792 / 1,392,108 | 0.00 / 0.00 / 0.00 | -99.9% / -99.9% / -99.8% | 3,377 | 1,411 |
| S4 (5,000,000) | 150 | 7,502 / 8,833 / 9,206 | 6,568,458 / 6,812,176 / 6,895,766 | 0.00 / 0.00 / 0.00 | -99.9% / -99.9% / -99.9% | 9,324 | 7,586 |
| S5 (10,000,000) | 150 | 14,500 / 18,534 / 19,400 | 13,167,709 / 13,675,205 / 13,887,550 | 0.00 / 0.00 / 0.00 | -99.9% / -99.9% / -99.9% | 19,501 | 15,228 |

### Read work per GET (application-level; sampled GETs, all runs)

Hits (all hits, including those answered by the memtable). V2 columns come from the V2 run on the same sampled keys.

| scale | GETs sampled | tables consulted: mean / median / p95 | of which skipped by the index (mean) | blocks read (mean) | V3 records decoded: mean / median / p95 / max | V3 bytes decoded (mean) | V2 records examined (mean) | V2 bytes scanned (mean) | records V2 / V3 |
|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | — | — | — | — | — | — | — | — | — |
| S2 (100,000) | 1,000 | 1.1 / 1 / 2 | 0.4 | 0.72 | 6.06 / 5 / 16 / 16 | 751 | 14,157 | 1,755,500 | 2,336 |
| S3 (1,000,000) | 1,000 | 13.6 / 14 / 26 | 12.6 | 1.00 | 8.72 / 9 / 16 / 16 | 1,082 | 18,999 | 2,355,860 | 2,178 |
| S4 (5,000,000) | 600 | 67.5 / 70 / 125 | 66.5 | 0.99 | 8.39 / 9 / 16 / 16 | 1,040 | 20,041 | 2,485,115 | 2,389 |
| S5 (10,000,000) | 600 | 136.4 / 136 / 253 | 135.4 | 1.00 | 8.68 / 9 / 15 / 16 | 1,076 | 19,236 | 2,385,305 | 2,217 |

Misses (key above the stored range):

| scale | GETs sampled | tables consulted: mean / median / p95 | of which skipped by the index (mean) | blocks read (mean) | V3 records decoded: mean / median / p95 / max | V3 bytes decoded (mean) | V2 records examined (mean) | V2 bytes scanned (mean) | records V2 / V3 |
|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | — | — | — | — | — | — | — | — | — |
| S2 (100,000) | 15 | 2.0 / 2 / 2 | 0.0 | 2.00 | 22.00 / 22 / 22 / 22 | 2,728 | 75,574 | 9,371,176 | 3,435 |
| S3 (1,000,000) | 15 | 26.0 / 26 / 26 | 0.0 | 26.00 | 286.00 / 286 / 286 / 286 | 35,464 | 982,462 | 121,825,288 | 3,435 |
| S4 (5,000,000) | 9 | 132.0 / 132 / 132 | 0.0 | 132.00 | 1452.00 / 1452 / 1452 / 1452 | 180,048 | 4,987,884 | 618,497,616 | 3,435 |
| S5 (10,000,000) | 9 | 264.0 / 264 / 264 | 0.0 | 264.00 | 2904.00 / 2904 / 2904 / 2904 | 360,096 | 9,975,768 | 1,236,995,232 | 3,435 |

### Cost of one table lookup (untimed decomposition)

Median per (table, key) pair. `locate` is the binary search of the in-memory index, and the file columns include the cost of Python's `open`.

| scale | key kind | (table, key) pairs per repetition | `locate` (µs) | open + close (µs) | open + seek + read block (µs) | full `SSTable.lookup` (µs) |
|---|---|---|---|---|---|---|
| S2 (100,000) | miss | 2 | 0.57 | 30.2 | 36.2 | 55.5 |
| S2 (100,000) | hit | 144 | 0.41 | 28.4 | 34.7 | 53.7 |
| S3 (1,000,000) | miss | 26 | 0.37 | 27.3 | 32.1 | 47.2 |
| S3 (1,000,000) | hit | 200 | 0.65 | 27.6 | 34.9 | 51.0 |
| S4 (5,000,000) | miss | 132 | 0.59 | 29.4 | 36.9 | 56.5 |
| S4 (5,000,000) | hit | 199 | 0.76 | 29.0 | 34.7 | 53.2 |
| S5 (10,000,000) | miss | 264 | 0.83 | 31.9 | 38.9 | 59.0 |
| S5 (10,000,000) | hit | 200 | 0.79 | 29.6 | 35.9 | 54.1 |

### Scaling

Fits over the scales that have SSTables (S2–S5; four points, so descriptive only).

| GET | fit over the scales that have SSTables | log-log slope | R² | p50 largest / smallest scale | x ratio largest / smallest |
|---|---|---|---|---|---|
| hit | V3 p50 vs records | 0.170 | 0.8635 | 2.31 | 100.0 |
| hit | V2 p50 vs records | 0.115 | 0.9636 | 1.66 | 100.0 |
| hit | V3 p50 vs SSTables | 0.159 | 0.8461 | 2.31 | 132.0 |
| miss | V3 p50 vs records | 1.077 | 0.9993 | 139.83 | 100.0 |
| miss | V2 p50 vs records | 1.058 | 0.9992 | 130.85 | 100.0 |
| miss | V3 p50 vs SSTables | 1.016 | 0.9999 | 139.83 | 132.0 |

Miss p50 divided by the number of SSTables:

| scale | V3 miss p50 per SSTable (µs) | V2 miss p50 per SSTable (µs) |
|---|---|---|
| S2 (100,000) | 51.85 | 50,316 |
| S3 (1,000,000) | 52.88 | 50,286 |
| S4 (5,000,000) | 56.83 | 49,761 |
| S5 (10,000,000) | 54.92 | 49,878 |

### Writes (`put_load`, all writes including flush-triggering ones)

| scale | V3 p50 / p95 / p99 (µs) | V2 p50 / p95 / p99 (µs) | V3 ops/s | V2 ops/s | V3 / V2 ops/s | V3 slowest write (ms) | V2 slowest write (ms) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 4.5 / 5.8 / 10.9 | 4.6 / 5.8 / 11.2 | 198,470 | 190,458 | 1.042 | 1.4 | 1.3 |
| S2 (100,000) | 4.8 / 6.2 / 14.3 | 4.8 / 6.1 / 14.1 | 155,752 | 155,797 | 1.000 | 61.0 | 59.0 |
| S3 (1,000,000) | 4.9 / 6.3 / 11.7 | 4.8 / 6.2 / 11.4 | 145,812 | 150,075 | 0.972 | 97.5 | 122.4 |
| S4 (5,000,000) | 4.8 / 6.3 / 11.2 | 4.8 / 6.1 / 10.3 | 146,793 | 149,882 | 0.979 | 95.8 | 104.5 |
| S5 (10,000,000) | 4.9 / 6.2 / 11.0 | 4.8 / 6.0 / 10.1 | 144,622 | 149,106 | 0.970 | 122.7 | 131.7 |

Flush-triggering writes (pooled over runs) and flush duration:

| scale | flush-triggering writes (pooled, V3) | V3 trigger p50 / p95 / p99 / max (ms) | V2 trigger p50 / p95 / p99 / max (ms) | median flush (ms): V3 / V2 | median inside `write_sstable` (ms): V3 / V2 | median rest of flush (ms): V3 / V2 | amortized flush (µs per PUT): V3 / V2 |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | — | — | — | — | — | — |
| S2 (100,000) | 10 | 60.7 / 67.1 / 67.1 / 67.1 | 59.0 / 64.1 / 64.1 / 64.1 | 60.6 / 58.6 | 42.9 / 41.0 | 18.3 / 18.4 | 1.18 / 1.15 |
| S3 (1,000,000) | 130 | 57.7 / 66.9 / 106.2 / 113.4 | 50.5 / 82.1 / 122.4 / 175.2 | 57.7 / 50.4 | 40.1 / 33.4 | 17.7 / 16.7 | 1.53 / 1.36 |
| S4 (5,000,000) | 396 | 57.2 / 64.3 / 95.8 / 115.2 | 53.2 / 65.1 / 104.3 / 118.0 | 57.1 / 53.1 | 39.6 / 37.1 | 18.0 / 17.0 | 1.50 / 1.46 |
| S5 (10,000,000) | 792 | 57.5 / 65.1 / 106.9 / 122.9 | 55.8 / 64.5 / 111.7 / 138.2 | 57.4 / 55.7 | 39.7 / 38.9 | 17.7 / 17.0 | 1.50 / 1.48 |

Non-flush writes and table size:

| scale | V3 non-flush p50 / p95 / p99 (µs) | V2 non-flush p50 / p95 / p99 (µs) | V3 table bytes | V2 table bytes |
|---|---|---|---|---|
| S1 (10,000) | 4.5 / 5.8 / 10.9 | 4.6 / 5.8 / 11.2 | — | — |
| S2 (100,000) | 4.8 / 6.2 / 14.3 | 4.8 / 6.1 / 14.0 | 4,739,958 | 4,685,616 |
| S3 (1,000,000) | 4.9 / 6.3 / 11.7 | 4.8 / 6.2 / 11.4 | 4,739,958 | 4,685,616 |
| S4 (5,000,000) | 4.8 / 6.3 / 11.2 | 4.8 / 6.1 / 10.3 | 4,739,958 | 4,685,616 |
| S5 (10,000,000) | 4.9 / 6.2 / 11.0 | 4.8 / 6.0 / 10.1 | 4,739,958 | 4,685,616 |

**Overwrite** (1,000 per run, pooled; none triggered a flush):

| scale | V3 p50 / p95 / p99 (µs) | V2 p50 / p95 / p99 (µs) | V3 / V2 p50 | flush-triggering ops (V3 / V2) |
|---|---|---|---|---|
| S1 (10,000) | 5.4 / 7.0 / 25.3 | 5.0 / 6.0 / 15.5 | 1.08 | 0 / 0 |
| S2 (100,000) | 5.0 / 6.5 / 25.2 | 5.6 / 7.0 / 19.0 | 0.89 | 0 / 0 |
| S3 (1,000,000) | 4.7 / 6.1 / 17.9 | 5.6 / 7.3 / 20.5 | 0.84 | 0 / 0 |
| S4 (5,000,000) | 4.9 / 6.2 / 22.3 | 5.1 / 6.1 / 16.0 | 0.96 | 0 / 0 |
| S5 (10,000,000) | 4.6 / 6.3 / 16.6 | 5.0 / 7.8 / 23.3 | 0.92 | 0 / 0 |

**Delete** (1,000 per run, pooled; none triggered a flush):

| scale | V3 p50 / p95 / p99 (µs) | V2 p50 / p95 / p99 (µs) | V3 / V2 p50 | flush-triggering ops (V3 / V2) |
|---|---|---|---|---|
| S1 (10,000) | 5.0 / 7.9 / 13.0 | 5.0 / 5.7 / 9.3 | 1.00 | 0 / 0 |
| S2 (100,000) | 4.9 / 5.9 / 10.5 | 5.1 / 5.9 / 7.8 | 0.96 | 0 / 0 |
| S3 (1,000,000) | 4.4 / 6.0 / 10.0 | 5.0 / 5.9 / 8.2 | 0.88 | 0 / 0 |
| S4 (5,000,000) | 5.0 / 5.6 / 14.4 | 4.5 / 5.3 / 8.3 | 1.11 | 0 / 0 |
| S5 (10,000,000) | 5.0 / 5.9 / 9.4 | 4.7 / 5.8 / 9.8 | 1.06 | 0 / 0 |

### Open / recovery

`KVStore(path)` on the closed database after the load: footer and index validation of every table plus replay of the surviving WAL. V2 values from `v2_benchmark.json`.

| scale | SSTables opened | index entries loaded | index bytes read | V3 open per run (ms) | V3 median (ms) | V2 median (ms) | V3 / V2 | V3 listing / SSTable opens / WAL replay / index re-read I/O only (ms, median) | reopen after mutations (ms, median): V3 / V2 |
|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 0 | 0 | 21, 22, 18, 24, 15 | 20.9 | 21.3 | 0.98 | 0.07 / 0.0 / 8.5 / 0.0 | 20.5 / 19.0 |
| S2 (100,000) | 2 | 4,724 | 108,652 | 34, 37, 37, 35, 39 | 37.2 | 33.0 | 1.13 | 0.08 / 2.5 / 24.8 / 0.1 | 37.6 / 39.2 |
| S3 (1,000,000) | 26 | 61,412 | 1,412,476 | 55, 52, 58, 57, 52 | 55.2 | 29.0 | 1.90 | 0.14 / 27.2 / 17.6 / 1.2 | 55.0 / 32.3 |
| S4 (5,000,000) | 132 | 311,784 | 7,171,032 | 171, 181, 165 | 170.9 | 32.9 | 5.19 | 1.51 / 157.1 / 13.7 / 6.3 | 168.2 / 37.2 |
| S5 (10,000,000) | 264 | 623,568 | 14,342,064 | 353, 323, 375 | 353.0 | 78.1 | 4.52 | 0.47 / 301.1 / 23.1 / 12.3 | 338.5 / 68.2 |

### Memory (observed process private bytes, and index heap)

Delta in private bytes of a fresh process after loading N keys and after opening the resulting database (median and min–max over 3 probes), and the Python heap retained by opening the tables (tracemalloc).

| scale | SSTables | V3 private delta after load (MB) | V2 after load (MB) | V3 after open (MB) | V2 after open (MB) | V3 − V2 after open (MB, medians) | peak working set after load (MB): V3 / V2 | index heap retained by opening the tables (MB, tracemalloc) |
|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 1.3 (1.0–1.8) | 1.7 (1.6–1.8) | 1.7 (1.6–2.5) | 1.4 (1.4–1.6) | +0.3 | 25.6 / 24.0 | — |
| S2 (100,000) | 2 | 6.9 (6.6–7.4) | 7.4 (7.2–7.6) | 5.1 (4.1–5.2) | 5.2 (4.9–5.3) | -0.1 | 34.4 / 33.0 | 0.42 |
| S3 (1,000,000) | 26 | 16.2 (16.2–16.9) | 8.6 (8.6–8.7) | 10.3 (8.9–10.6) | 4.4 (2.7–5.0) | +5.9 | 41.5 / 33.4 | 5.50 |
| S4 (5,000,000) | 132 | 41.2 (40.2–41.4) | 11.1 (10.3–11.5) | 33.1 (32.8–33.3) | 3.9 (3.8–4.1) | +29.2 | 66.6 / 34.6 | 27.90 |
| S5 (10,000,000) | 264 | 72.3 (72.1–73.1) | 11.5 (11.0–12.1) | 66.6 (66.1–67.1) | 5.4 (4.3–5.4) | +61.2 | 97.3 / 34.5 | 55.80 |

| scale | index entries | index bytes on disk | heap retained in memory (bytes, tracemalloc) | heap bytes per entry | heap / on-disk index bytes |
|---|---|---|---|---|---|
| S2 (100,000) | 4,724 | 108,652 | 423,648 | 89.7 | 3.90 |
| S3 (1,000,000) | 61,412 | 1,412,476 | 5,496,416 | 89.5 | 3.89 |
| S4 (5,000,000) | 311,784 | 7,171,032 | 27,901,080 | 89.5 | 3.89 |
| S5 (10,000,000) | 623,568 | 14,342,064 | 55,801,032 | 89.5 | 3.89 |

### Space and write amplification (derived)

| scale | total persistent bytes (V3) | space amplification V3 | V2 | rewrite factor V3 | V2 | bytes per payload byte V3 | V2 |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 1,388,000 | 1.3834 | 1.3834 | 1.0000 | 1.0000 | 1.1266 | 1.1266 |
| S2 (100,000) | 12,656,740 | 1.1517 | 1.1418 | 1.7555 | 1.7468 | 1.9629 | 1.9532 |
| S3 (1,000,000) | 125,561,620 | 1.1323 | 1.1196 | 1.9927 | 1.9813 | 2.2263 | 2.2135 |
| S4 (5,000,000) | 627,324,840 | 1.1305 | 1.1176 | 2.0089 | 1.9973 | 2.2442 | 2.2313 |
| S5 (10,000,000) | 1,254,501,680 | 1.1303 | 1.1174 | 2.0090 | 1.9975 | 2.2443 | 2.2314 |

### 10,000-key head-to-head

**V0 baseline workload, default 4 MiB limit (no flush occurs)** (medians over 5 runs; V3 / V2 / V3 ÷ V2; µs)

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 0.5 / 0.5 / 1.00 | 0.8 / 0.7 / 1.14 | 3.8 / 2.5 / 1.52 |
| get_miss | 0.4 / 0.4 / 1.00 | 0.6 / 0.6 / 1.00 | 2.0 / 2.3 / 0.87 |
| mixed_all | 0.6 / 0.5 / 1.20 | 5.3 / 5.4 / 0.98 | 14.5 / 7.1 / 2.04 |
| mixed_get | 0.5 / 0.5 / 1.00 | 0.7 / 0.7 / 1.00 | 0.8 / 0.8 / 1.00 |
| mixed_put | 5.0 / 5.0 / 1.00 | 7.5 / 6.9 / 1.09 | 31.1 / 24.6 / 1.26 |
| mixed_delete | 4.9 / 5.1 / 0.96 | 13.7 / 14.5 / 0.94 | 13.7 / 14.5 / 0.94 |
| put_load | 4.6 / 4.5 / 1.02 | 5.5 / 5.4 / 1.02 | 14.6 / 12.5 / 1.17 |
| put_sync | 1,882.6 / 1,914.4 / 0.98 | 2,093.4 / 2,178.7 / 0.96 | 3,824.1 / 3,775.4 / 1.01 |

**V1 extended workload, default limit (no flush occurs)** (medians over 5 runs; V3 / V2 / V3 ÷ V2; µs)

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 0.4 / 0.4 / 1.00 | 0.5 / 0.5 / 1.00 | 0.7 / 0.6 / 1.17 |
| get_miss | 0.4 / 0.4 / 1.00 | 0.5 / 0.5 / 1.00 | 0.6 / 0.6 / 1.00 |
| mixed_all | 0.5 / 0.5 / 1.00 | 5.2 / 5.8 / 0.90 | 7.1 / 7.1 / 1.00 |
| mixed_get | 0.4 / 0.5 / 0.80 | 0.6 / 0.7 / 0.86 | 0.8 / 0.9 / 0.89 |
| mixed_put | 4.7 / 4.9 / 0.96 | 6.0 / 6.7 / 0.90 | 17.7 / 16.7 / 1.06 |
| mixed_delete | 4.7 / 4.8 / 0.98 | 5.9 / 6.4 / 0.92 | 12.6 / 12.0 / 1.05 |
| put_load | 4.4 / 4.5 / 0.98 | 5.4 / 5.5 / 0.98 | 14.8 / 13.7 / 1.08 |
| put_sync | 1,887.4 / 1,906.5 / 0.99 | 2,137.7 / 2,096.0 / 1.02 | 3,730.7 / 3,899.1 / 0.96 |

**V0 baseline workload, 16 KiB limit (10,000 keys spread over many tables)** (medians over 5 runs; V3 / V2 / V3 ÷ V2; µs)

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 54.0 / 1,288.3 / 0.04 | 82.9 / 2,879.7 / 0.03 | 140.6 / 3,255.7 / 0.04 |
| get_miss | 3,031.5 / 18,380.3 / 0.16 | 3,751.2 / 20,952.4 / 0.18 | 4,322.0 / 21,916.5 / 0.20 |
| mixed_all | 63.7 / 857.5 / 0.07 | 129.1 / 2,757.8 / 0.05 | 186.1 / 3,248.9 / 0.06 |
| mixed_get | 72.9 / 1,422.4 / 0.05 | 130.5 / 2,848.6 / 0.05 | 185.8 / 3,150.5 / 0.06 |
| mixed_put | 7.8 / 12.0 / 0.65 | 27.3 / 46.7 / 0.58 | 36,425.2 / 29,144.3 / 1.25 |
| mixed_delete | 7.7 / 12.9 / 0.60 | 21.7 / 21.5 / 1.01 | 21.7 / 21.5 / 1.01 |
| put_load | 4.5 / 4.6 / 0.98 | 11.6 / 10.3 / 1.13 | 87.3 / 89.7 / 0.97 |
| put_sync | 1,890.5 / 1,906.9 / 0.99 | 2,269.1 / 2,249.3 / 1.01 | 5,891.0 / 4,071.7 / 1.45 |

## Analysis

Everything below refers to the measurements above. Where a cause is offered, it's marked as a candidate or as measured.

1. **GET hits.** The sparse index cut the hit p50 from 17,902, 24,978, 29,844 and 29,664 µs (V2) to 46.6, 54.2, 76.8 and 107.7 µs (V3) at S2, S3, S4 and S5. V3 / V2 = 0.0026, 0.0022, 0.0026, 0.0036, i.e. -99.7%, -99.8%, -99.7%, -99.6% (V3 is 384, 461, 389 and 275 times faster). p95: 76.0, 84.6, 135.9, 185.9 µs against 48,399, 48,293, 51,566, 54,514 µs. p99: 154.9, 174.1, 271.8, 286.0 µs against 53,720, 52,936, 55,890, 60,101 µs. At S1 nothing is flushed, so V3 and V2 run the same memtable-only path: the p50 is 0.4 µs for both, and the p95/p99 differences (0.6 → 0.6, 0.7 → 0.8 µs) are one or two 0.1 µs clock ticks between separate invocations.

2. **GET misses.** The miss p50 fell from 100,632, 1,307,443, 6,568,458 and 13,167,709 µs to 103.7, 1,375, 7,502 and 14,500 µs (V3 / V2 = 0.0010, 0.0011, 0.0011, 0.0011; -99.9%, -99.9%, -99.9%, -99.9%), i.e. 970, 951, 876 and 908 times faster. At S5 a miss takes 14.5 ms instead of 13.2 s, and p95 / p99 there are 18.5 / 19.4 ms (V2: 13.7 / 13.9 s). Sample counts are V2's: pooled misses are 500,000, 1,000, 500, 150 and 150 and hits 500,000, 25,000, 10,000, 3,000 and 3,000 at S1–S5. So the p99 of a miss rests on 150 samples at S4 and S5 (the 149th of 150 values) and on 500 at S3, and those p99 values are approximate.

3. **Structural read amplification fell far more than latency did.** Per GET, V3 consults the same tables as V2 (a hit consults 0.0, 1.1, 13.6, 67.5 and 136.4 tables on average at S1–S5, and a miss consults all of them: 2, 26, 132 and 264 at S2, S3, S4 and S5). But a table is only *opened* if the index doesn't rule the key out. A hit opens 0.00, 0.72, 1.00, 0.99 and 1.00 blocks on average (never more than 1, and a hit in the memtable opens none), because every newer table is excluded by its first key. A miss opens one block in every table. Records decoded per hit: 6.1, 8.7, 8.4 and 8.7 on average (maximum 16, 16, 16, 16, bounded by the interval of 16) against V2's 14,157, 18,999, 20,041 and 19,236, a factor of 2,336, 2,178, 2,389 and 2,217. Per miss: 22, 286, 1,452 and 2,904 records (the 11-record last block of each table) against V2's 75,574, 982,462, 4,987,884 and 9,975,768, a factor of 3,435. Bytes decoded are 2,728, 35,464, 180,048 and 360,096 against 9,371,176, 121,825,288, 618,497,616 and 1,236,995,232. Records decoded fell about 2,178–2,389× for hits and 3,435× for misses, but latency fell only 275–461× and 876–970×. Derived counts equal the instrumented counts for every sampled GET in every run (the V3 structural analysis).

4. **Where the remaining time goes (separate untimed measurement).** A V3 lookup in one table at S5 takes a median 59.0 µs for the miss key (54.1 µs for hit keys). The in-memory index search (`locate`) is 0.83 µs (0.79 µs on hit pairs). Opening and closing the file alone is 31.9 µs (29.6), and opening, seeking and reading the block's bytes without decoding is 38.9 µs (35.9). So the per-table cost is dominated by touching the file, not by scanning: opening and closing is 54% of the table lookup and `locate` is 1.4%. The remaining 20.2 µs (decoding and checksumming up to 11–16 records, the bounded view and generator overhead) wasn't separated further. As a consistency check, 264 tables × 59.0 µs = 15.6 ms, against a measured miss p50 of 14.5 ms. The measured miss p50 per SSTable is 51.9–56.8 µs (V2: 49,761–50,316 µs). This is evidence that fixed per-file cost, not record scanning, now bounds the speed-up on this system. *Why* a file open costs what it does here (file system, resident security software) wasn't tested, and cold-cache behavior wasn't measured.

5. **Scaling with database size.** Over the scales that have SSTables (S2, S3, S4, S5) the log-log slope of the V3 miss p50 against the record count is 1.077 (R² 0.9993; V2: 1.058), and against the SSTable count it's 1.016 (R² 0.9999). **A miss is still linear in the number of tables**, because it visits every table. The index changed the constant (about 55.2 µs per table, down from about 49,761–50,316 µs), not the growth. The hit p50 grows 2.31× from S2 to S5 while the record count grows 100× (log-log slope 0.170, R² 0.86; V2: 0.115). It doesn't grow with records scanned (bounded by 16) but with the number of newer tables the lookup passes (mean 0.4 → 135.4 tables ruled out per hit). The p50 growth of 61.1 µs over 135.0 more skipped tables is 0.45 µs per skipped table, the same order as the measured `locate` cost (0.79 µs). The Python call overhead of `lookup` itself wasn't separated. A miss is 135× a hit at S5 (V2: 444×).

6. **Run-to-run variation.** The between-run spread of the V3 hit p50 ((max − min) / median of the per-run p50s) is 14%, 15%, 10% and 30% at S2, S3, S4 and S5, against 5%, 2%, 1% and 5% for V2. For misses it's 22%, 9%, 2% and 31% against 1%, 3%, 3% and 2%. At S5 one of the three runs is slower than the other two for both hits and misses (run p50s: hits 103, 96, 127 µs, misses 14.1, 13.4, 17.8 ms). The cause wasn't identified and is **unexplained**. This benchmark doesn't establish a noise floor. The V2 → V3 differences reported above are two to three orders of magnitude larger than these spreads, but differences the size of the spread couldn't be interpreted.

7. **Write path.** Between flushes a V3 write costs what a V2 write does (non-flush p50 4.9 µs at S5, V2 4.8). Sustained load throughput is 1.000, 0.972, 0.979 and 0.970× V2's at S2, S3, S4 and S5 (144,622 vs 149,106 ops/s at S5). The S1 value (1.042) is the same memtable-only code path and shows the size of the difference between invocations. The median flush takes 60.6, 57.7, 57.1 and 57.4 ms in V3 against 58.6, 50.4, 53.1 and 55.7 ms in V2 at S2, S3, S4 and S5, and inside `write_sstable` it takes 42.9, 40.1, 39.6 and 39.7 vs 41.0, 33.4, 37.1 and 38.9 ms. The V3 table is 4,739,958 bytes against 4,685,616 (1.16% larger). The part of the flush attributable to building and writing the index wasn't isolated, and V2 wasn't re-measured in the same invocation series, so a few ms of difference in flush time isn't attributed. Flush-triggering writes at S5: p50 / p99 / max 57.5 / 106.9 / 122.9 ms (V2 55.8 / 111.7 / 138.2 ms). Overwrites and deletes never triggered a flush at any scale (as in V2), so a flush-triggering delete was again not observed at scale.

8. **Recovery got slower.** Opening a V3 database takes 37, 55, 171 and 353 ms at S2, S3, S4 and S5, against 33, 29, 33 and 78 ms for V2 (V3 / V2 = 1.13, 1.90, 5.19, 4.52). At S1 there are no tables and the two are the same (21 vs 21 ms). V2 read one footer per table, while V3 reads every table's footer *and its whole index block* and parses and validates every entry: at S5, 623,568 entries (14,342,064 bytes) across 264 tables. The table-open step takes 301 ms at S5 (V2's footer validation: 17 ms), of which re-reading the same bytes without parsing takes 12 ms. The remainder, about 0.46 µs per entry, is index parsing and validation in Python plus per-table open overhead (attributed by difference, not separated further). WAL replay is unchanged (23 ms vs 21 ms at S5). Startup now grows with total index size, i.e. with the data, so V2's property that open time stays in a narrow band no longer holds. Open times of tens of milliseconds have a large relative between-run spread (S5: 15%).

9. **Memory and index overhead.** The index adds one entry per 16 records: 623,568 entries and 14,342,064 bytes at S5 (1.15% of the SSTable bytes, 1.44 bytes per table record; space amplification 1.1303 against V2's 1.1174). In memory the index is much larger than on disk: opening the tables retains 55.8 MB of Python heap at S5 (89.5 bytes per entry against 23 on disk, 3.89× the on-disk index). The figure is the same in every run because tracemalloc counts allocations. Observed process private bytes after opening are 66.6 MB (V2: 5.4 MB), a difference of +61.2 MB, and after the load 72.3 MB (V2: 11.5 MB), in line with the retained heap. V2's load-time delta never exceeded 11.5 MB, but **V3's grows with the number of SSTables**: the post-open difference from V2 is -0.1, +5.9, +29.2 and +61.2 MB at S2, S3, S4 and S5 (about 0.23 MB per SSTable at S5, and the retained index heap is 0.21 MB per SSTable). With a fixed interval of 16 the index is a constant fraction of the data (about 5.6 bytes of heap per flushed record). Private-byte deltas depend on allocator state and memtable fill, so they're less exact than the tracemalloc figure.

10. **10,000-key workloads.** With the default limit nothing flushes, so V3 runs V2's memtable-only path. No phase's p50 differs from V2's by more than 20% (baseline workload) or 20% (extended), and the largest differences are one 0.1 µs clock tick (e.g. 0.6 vs 0.5 µs). With the 16 KiB limit (67 tables of 148 records and 10 index entries each), V3's GET hit p50 is 54.0 µs against V2's 1,288.3 µs (0.042×), the miss is 3,032 µs against 18,380 µs (0.165×), and the mixed workload's GETs are 72.9 µs against 1,422.4 µs. The miss improvement is smaller than at scale (a factor of 6.1) because a table of 148 records has little to skip: ten blocks of at most 16 records against 148. `sync=True` PUTs are unchanged (1,890 vs 1,907 µs p50). The mixed PUT and DELETE p50 are lower than V2's in the 16 KiB runs (7.8 vs 12.0 µs; 7.7 vs 12.9 µs). Why wasn't investigated and is **unexplained** (the write path is unchanged, so it isn't attributed to the index).

11. **Tradeoffs.** The sparse index answered the main question: it removed the table scan, cutting records decoded per GET by 2,178–2,389× (hits) and 3,435× (misses) and GET latency by 275–461× (hits) and 876–970× (misses) at S2, S3, S4 and S5. It left read amplification in *tables* unchanged: a miss still visits every table, so miss latency still grows linearly with the data (14.5 ms at S5, 135× a hit). It costs startup time (4.5× V2's at S5), memory (about 61 MB more at S5, growing with the data) and 1.16% more disk, and it left write throughput within 3% of V2's at S2–S5 (separate invocations).

## Limitations

- **Workload layout.** Keys are loaded in ascending order, so tables have disjoint ranges: a hit opens at most one table and skips all newer ones by their first key. A random-order load would make table ranges overlap, and the number of tables opened per hit would be larger, but that wasn't measured. Miss keys exceed every stored key, so every table opens its last block. A miss key inside the range would skip tables whose first key is larger. Hits are uniform over the keyspace.
- **One index interval.** Only the default interval (16) was measured. How block scan work, index size, memory and open time trade off as the interval changes wasn't measured.
- **Memtable-resident keys.** At the end of the load, 0.2–24% of keys sit in the memtable and are answered without file access. The overwrite and delete phases also hit the memtable.
- **One memtable limit at scale.** Only the 4 MiB limit was measured at scale, and the 16 KiB limit on the 10,000-key workloads.
- **Sample counts.** GET sample counts are V2's, kept for comparability. Pooled misses are 1,000, 500, 150 and 150 at S2–S5, so the p99 of a miss at S4 and S5 rests on 150 samples (the 149th value) and is approximate. V3's misses are cheap enough that larger counts would have been affordable, but they weren't used. Read work is measured on 200 hits and 3 misses per run, and it equals the derived values.
- **Run-to-run variation.** Between-run spreads of the V3 GET p50 are 10–30% at S2–S5, larger than V2's, and one S5 run is slower than the other two for reasons that weren't identified (analysis item 6). The benchmark didn't establish a noise floor. The V2 reference was measured in an earlier invocation, so differences of a few percent between V2 and V3 (flush time, write throughput, anything at S1) can't be interpreted.
- **Machine state.** The operator kept the machine idle during the ladder (not independently verified). Windows Defender and other resident security software stayed enabled, no CPU affinity was set, and timings remain sensitive to machine load.
- **Application-level read work.** Records and bytes decoded are counted in the record reader. Physical I/O wasn't measured, and Python's buffered reader may transfer more than the block.
- **Cost decomposition.** It's a separate warm, batched measurement. It explains the per-table cost but isn't an instrumented trace of the timed GETs. Reasons for the file-open cost weren't tested.
- **Cold cache.** All numbers are warm-cache, so nothing here says how the index changes cold reads.
- **Memory.** Private-byte deltas depend on allocator state and memtable fill and aren't a function of N alone. The tracemalloc figure counts Python allocations only.
- **Open breakdown.** Component times come from a separate warm repetition and don't add up to the timed open. The split of the table-open step into I/O and parsing is by difference.
- **Delete-triggered flushes.** None occurred in any scaling run.
- **Not measured:** physical I/O, concurrent access, cold-cache behavior, random-order loads, other intervals or flush limits at scale, flush-triggering deletes, and directories mixing V2 and V3 tables.

## Environment

**Environment:** Python 3.10.6; Windows-10-10.0.26300-SP0; Intel64 Family 6 Model 183 Stepping 1, GenuineIntel; 24 logical CPUs; 17.0 GB RAM; seed 42; key 11 B; value 100 B; `sync=False` for the scaling runs; default `memtable_limit_bytes` = 4 MiB; index interval 16.

# V4 benchmark

Purpose: measure what the Bloom filter buys over V3 ([v4-design.md](v4-design.md)). V4 has the same WAL, memtable, synchronous flush and sparse index, but every SSTable also carries a Bloom filter of **10 bits per key and 7 hash functions** (the store's defaults, not tuned; expected false-positive rate 0.82% for a full-size table). The main question is whether the filter reduces the number of SSTables that actually have to be searched, and whether that turns into GET latency beyond V3. V0–V3 results are references and aren't modified. **All numbers are warm OS-cache numbers.** Every figure in this section comes from `benchmarks/results/v4_benchmark.json`, `v4_structural.json` and the V3 reference `v3_benchmark.json` / `v3_structural.json`.

## Methodology

**Identical to V3:** seed 42, key 11 B, value 100 B, 124-byte PUT records, `sync=False` for the scaling runs, a fresh database in a temp directory for every run, and only the store call timed (a `perf_counter_ns` pair per operation). Correctness checks, bookkeeping, close and cleanup aren't timed. Percentiles are nearest-rank using the V0 code. The scale ladder is 10,000, 100,000, 1,000,000, 5,000,000 and 10,000,000 records with 5, 5, 5, 3 and 3 runs, and each scale runs in its own fresh Python process, one after another on an idle machine. The memtable limit is 4 MiB (4,194,304 bytes of key + value payload, 111 bytes per entry here, so 37,787 entries per table and a flush every 37,787 writes). Hit, overwrite and delete keys are generated by the same `workload` in the V4 benchmark, so V3 and V4 GET the **same keys**. GET sample counts per scale are V3's: hits per run 100,000, 5,000, 2,000, 1,000 and 1,000, and misses per run 100,000, 200, 100, 50 and 50. Index interval 16 and Bloom 10 bits per key / 7 hashes are the `KVStore` defaults, asserted on the store and on every table written. V3 reference values are read from `v3_benchmark.json` (measured in an earlier invocation).

**Workload semantics that determine what a GET costs (unchanged from V3).** Keys are loaded in ascending order, so every table holds a disjoint, ascending key range and newer tables hold larger keys. *Hits* are drawn uniformly from the loaded keyspace. A hit on a key in table *j* consults every newer table and then table *j*, and a hit on a key still in the memtable consults no table. In V3 each newer table was ruled out by the index (the key sorts below the table's first key) at no file cost, so a V3 hit already opens at most one file. *Misses* use keys above the stored range (`key_of(N + i)`, never written), and every table is consulted. In V3 the key sorts after every table's last record, so the index selects the last block of **every** table and a miss opens one file per table. In V4 each of those tables is first asked its filter. The miss workload is therefore the worst case for V3's files opened and the best case for a filter. The hit workload is the case where V3 already opened one file and the filter can only add a check (see Limitations).

**V4-specific measurements (all untimed; none inside a timed phase):**
- *Read work per GET:* per run, 200 hit and 50 miss GETs are repeated with `SSTable.lookup`, `SSTable.may_contain`, `SSTable.locate`, `SSTable._scan_block`, `sstable._open_table` and the record reader wrapped (monkeypatched, no production code changed). This counts tables consulted (`lookup` calls), Bloom checks and their "no" / "maybe" answers, **false positives** (a "maybe" whose lookup returned not-found), index searches, tables opened, blocks scanned, records decoded and bytes decoded. A "maybe" is counted as a false positive only after the table lookup returned not-found. This is application-level work, not physical I/O. The V4 structural analysis derives the same counts, including every Bloom answer (by rebuilding each table's filter from its keys), and asserts equality for every sampled GET of every run.
- *False-positive rate:* in the first run of each scale, after all timed phases, every table's filter is asked about 20,000 keys that are in no table (`zzz%08d`). The count is raised if needed so each scale makes at least 1,000,000 checks. `may_contain` is called directly, so no file is touched, and every "maybe" is a false positive by construction. The rate is pooled over (table, key) checks and reported with a 95% Wilson interval that treats the checks as independent trials. They're only approximately independent (a table's filter is fixed and answers every probe), so the interval is a guide to precision, not an exact bound. The expected rate is `(1 − e^(−k·n/m))^k` evaluated with each table's own record count `n`, filter bits `m` and `k`. Because the workload is deterministic, the filters are identical in every run, so one probe per scale isn't a sample of different filters. The tables probed are the finished database's tables: the load tables, any flushed during the overwrite and delete phases, and the table written by the validation flush below (which holds the memtable's contents; at S1 it's the only table).
- *Bloom invariant at scale:* in every run, after the final reopen, the memtable (which then holds the delete phase's tombstones) is flushed and every table is scanned in full with `SSTable.items()`. That verifies every record checksum, the key order and that the filter rebuilt from the keys read equals the stored filter byte for byte. Each key read, tombstones included, must also get "maybe" from its own table's filter.
- *Cost decomposition:* the cost of one table lookup is split into `may_contain` (Bloom), `locate` (index search), opening and closing the file, opening + seeking + reading the located block's bytes, and the full `SSTable.lookup`. It uses 20 repetitions over every load table the filter rules out for a miss key (the filter check is then the whole lookup), 10 repetitions over up to 200 (table, hit key) pairs, and 10 repetitions over up to 200 (table, absent key) pairs whose filter answers "maybe" (the cost of a false positive that reaches the last block). It also measures the BLAKE2b digest alone (the first line of the filter query) and the cost of building one table's filter (`bloom.add` over every key, which is what a flush adds). One clock pair per loop, the median over repetitions, then the median over runs.
- *Filter memory:* `tracemalloc` bytes retained by opening all tables of the finished database (index, filter blocks and `SSTable` objects), with the filter `bytes` objects' exact size subtracted to separate them, and process private bytes in the same fresh-process load / open probes as V3 (3 per scale).
- *Open work:* tables opened, index and filter bytes read, footer bytes read, V3's open-component repetition plus a re-read of the index and filter bytes without parsing, and `zlib.crc32` over the filter blocks alone.
- *Flush instrumentation:* as in V3. Each flush records the index interval and Bloom bits per key its table was written with, and a flush now includes building the filter.
- *Correctness:* every GET result is checked. Layout, index and filter sizes, WAL sizes and flush counts are asserted after the load, and overwrites and deletes are verified live and after the final reopen.

**10,000-key head-to-head** (`--baseline`): V0's baseline workload and V1's extended workload with the default limit (no flush occurs), and V0's baseline workload with the 16 KiB limit. The small limit spreads the 10,000 keys over 67 tables of 148 records, so GETs, the mixed workload and `sync=True` PUTs run against SSTables. 5 runs each, and every GET is checked against a reference dict.

**Timing floor:** `perf_counter` ticks every 100 ns, so sub-microsecond memtable operations are quantized. Operations that reach a table take microseconds or more.

## Results

### Table structure and filter size

Derived by the V4 structural analysis and asserted equal to the measured layout in every run: each flushed table holds 37,787 records, a 47,234-byte filter and a 60-byte footer. The table count equals V3's at every scale.

| scale | SSTables | filter bytes per table | filter bytes (total) | index bytes (total) | SSTable bytes (total, incl. index, filters, footers) | filter share of SSTable bytes | index share | filter bits per table record |
|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | — | 0 | 0 | 0 | — | — | — |
| S2 (100,000) | 2 | 47,234 | 94,468 | 108,652 | 9,574,416 | 0.99% | 1.13% | 10.000 |
| S3 (1,000,000) | 26 | 47,234 | 1,228,084 | 1,412,476 | 124,467,408 | 0.99% | 1.13% | 10.000 |
| S4 (5,000,000) | 132 | 47,234 | 6,234,888 | 7,171,032 | 631,911,456 | 0.99% | 1.13% | 10.000 |
| S5 (10,000,000) | 264 | 47,234 | 12,469,776 | 14,342,064 | 1,263,822,912 | 0.99% | 1.13% | 10.000 |

### GET

V4 and V3 percentiles are pooled over all runs at each scale (µs). Change = V4 relative to V3 (negative = faster). Ratios are computed from unrounded measured values.

**GET hit**

| scale | samples (pooled) | V4 p50 / p95 / p99 (µs) | V3 p50 / p95 / p99 (µs) | V4 / V3 (p50 / p95 / p99) | change (p50 / p95 / p99) | V4 max (µs) | V4 mean (µs) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.4 / 0.6 / 0.9 | 0.4 / 0.6 / 0.8 | 1.00 / 1.00 / 1.12 | +0.0% / +0.0% / +12.5% | 359.9 | 0.5 |
| S2 (100,000) | 25,000 | 52.6 / 86.9 / 177.8 | 46.6 / 76.0 / 154.9 | 1.13 / 1.14 / 1.15 | +12.9% / +14.3% / +14.8% | 4,160 | 48.0 |
| S3 (1,000,000) | 10,000 | 75.1 / 137.1 / 246.7 | 54.2 / 84.6 / 174.1 | 1.39 / 1.62 / 1.42 | +38.6% / +62.1% / +41.7% | 692.2 | 81.8 |
| S4 (5,000,000) | 3,000 | 161.9 / 301.5 / 465.9 | 76.8 / 135.9 / 271.8 | 2.11 / 2.22 / 1.71 | +110.8% / +121.9% / +71.4% | 1,240 | 169.9 |
| S5 (10,000,000) | 3,000 | 282.4 / 520.4 / 701.3 | 107.7 / 185.9 / 286.0 | 2.62 / 2.80 / 2.45 | +162.2% / +179.9% / +145.2% | 1,084 | 289.5 |

**GET miss**

| scale | samples (pooled) | V4 p50 / p95 / p99 (µs) | V3 p50 / p95 / p99 (µs) | V4 / V3 (p50 / p95 / p99) | change (p50 / p95 / p99) | V4 max (µs) | V4 mean (µs) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.4 / 0.6 / 0.8 | 0.4 / 0.6 / 0.9 | 1.00 / 1.00 / 0.89 | +0.0% / +0.0% / -11.1% | 192.1 | 0.5 |
| S2 (100,000) | 1,000 | 3.4 / 4.8 / 55.5 | 103.7 / 173.3 / 315.4 | 0.03 / 0.03 / 0.18 | -96.7% / -97.2% / -82.4% | 174.2 | 4.4 |
| S3 (1,000,000) | 500 | 34.7 / 112.1 / 161.9 | 1,375 / 1,871 / 2,276 | 0.03 / 0.06 / 0.07 | -97.5% / -94.0% / -92.9% | 585.8 | 49.8 |
| S4 (5,000,000) | 150 | 269.7 / 494.1 / 712.8 | 7,502 / 8,833 / 9,206 | 0.04 / 0.06 / 0.08 | -96.4% / -94.4% / -92.3% | 820.3 | 288.8 |
| S5 (10,000,000) | 150 | 556.0 / 909.6 / 1,154 | 14,500 / 18,534 / 19,400 | 0.04 / 0.05 / 0.06 | -96.2% / -95.1% / -94.0% | 1,387 | 584.3 |

### The Bloom → index → block pipeline, per GET (application-level; sampled GETs, all runs)

Every table consulted is asked its filter first. "No" skips the table (no index search, no file access), while "maybe" goes to the index and, if the index selects a block, to a file open and a bounded scan. V3 columns come from the V3 run on the same sampled keys (V3 "tables opened" is its blocks read).

Hits (all hits, including those answered by the memtable):

| scale | GETs sampled | tables consulted (V4 = V3) | Bloom "no" (skipped) | Bloom "maybe" | false positives | tables opened: V4 | V3 | records decoded: V4 | V3 | bytes decoded: V4 | V3 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 1,000 | 0.0 | 0.0 | 0.0 | 0.000 | 0.00 | 0.00 | 0.00 | 0.0 | 0 | 0 |
| S2 (100,000) | 1,000 | 1.1 | 0.4 | 0.7 | 0.005 | 0.72 | 0.72 | 6.06 | 6.1 | 751 | 751 |
| S3 (1,000,000) | 1,000 | 13.6 | 12.5 | 1.1 | 0.115 | 1.00 | 1.00 | 8.72 | 8.7 | 1,082 | 1,082 |
| S4 (5,000,000) | 600 | 67.5 | 66.0 | 1.5 | 0.525 | 0.99 | 0.99 | 8.39 | 8.4 | 1,040 | 1,040 |
| S5 (10,000,000) | 600 | 136.4 | 134.3 | 2.1 | 1.090 | 1.00 | 1.00 | 8.68 | 8.7 | 1,076 | 1,076 |

Misses (key above the stored range):

| scale | GETs sampled | tables consulted (V4 = V3) | Bloom "no" (skipped) | Bloom "maybe" | false positives | tables opened: V4 | V3 | records decoded: V4 | V3 | bytes decoded: V4 | V3 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 250 | 0.0 | 0.0 | 0.0 | 0.000 | 0.00 | 0.00 | 0.00 | 0.0 | 0 | 0 |
| S2 (100,000) | 250 | 2.0 | 2.0 | 0.0 | 0.000 | 0.00 | 2.00 | 0.00 | 22.0 | 0 | 2,728 |
| S3 (1,000,000) | 250 | 26.0 | 25.8 | 0.2 | 0.200 | 0.20 | 26.00 | 2.20 | 286.0 | 273 | 35,464 |
| S4 (5,000,000) | 150 | 132.0 | 130.8 | 1.2 | 1.200 | 1.20 | 132.00 | 13.20 | 1,452.0 | 1,637 | 180,048 |
| S5 (10,000,000) | 150 | 264.0 | 261.7 | 2.3 | 2.320 | 2.32 | 264.00 | 25.52 | 2,904.0 | 3,164 | 360,096 |

### Bloom skip rate and false positives in the GET workloads

| scale | GET kind | Bloom checks (sampled GETs, pooled over runs; same keys in every run) | "no" answers | skip rate | checks on a table that does not hold the key | false positives | false-positive share | of which resolved by the index (no file opened) | tables opened |
|---|---|---|---|---|---|---|---|---|---|
| S2 (100,000) | hit | 1,120 | 395 | 35.27% | 400 | 5 | 1.25% | 5 | 720 |
| S2 (100,000) | miss | 500 | 500 | 100.00% | 500 | 0 | 0.00% | 0 | 0 |
| S3 (1,000,000) | hit | 13,630 | 12,515 | 91.82% | 12,630 | 115 | 0.91% | 115 | 1,000 |
| S3 (1,000,000) | miss | 6,500 | 6,450 | 99.23% | 6,500 | 50 | 0.77% | 0 | 50 |
| S4 (5,000,000) | hit | 40,521 | 39,609 | 97.75% | 39,924 | 315 | 0.79% | 315 | 597 |
| S4 (5,000,000) | miss | 19,800 | 19,620 | 99.09% | 19,800 | 180 | 0.91% | 0 | 180 |
| S5 (10,000,000) | hit | 81,849 | 80,595 | 98.47% | 81,249 | 654 | 0.80% | 654 | 600 |
| S5 (10,000,000) | miss | 39,600 | 39,252 | 99.12% | 39,600 | 348 | 0.88% | 0 | 348 |

### Measured false-positive rate (absent-key probe)

| scale | tables probed | absent keys per table | checks | false positives | measured rate | 95% Wilson interval | expected from formula | per-table rate: min / median / max |
|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 1 | 1,000,000 | 1,000,000 | 8,107 | 0.811% | 0.793–0.828% | 0.819% | 0.81 / 0.81 / 0.81% |
| S2 (100,000) | 3 | 333,334 | 1,000,002 | 8,127 | 0.813% | 0.795–0.830% | 0.819% | 0.79 / 0.82 / 0.84% |
| S3 (1,000,000) | 27 | 37,038 | 1,000,026 | 8,256 | 0.826% | 0.808–0.844% | 0.819% | 0.75 / 0.82 / 0.93% |
| S4 (5,000,000) | 133 | 20,000 | 2,660,000 | 22,044 | 0.829% | 0.818–0.840% | 0.819% | 0.70 / 0.83 / 1.04% |
| S5 (10,000,000) | 265 | 20,000 | 5,300,000 | 43,478 | 0.820% | 0.813–0.828% | 0.819% | 0.64 / 0.82 / 1.04% |

### Bloom invariant at scale

| scale | runs validated | tables scanned per run | keys checked per run (incl. tombstones) | tombstone keys checked per run | false negatives | stored filter = filter rebuilt from keys |
|---|---|---|---|---|---|---|
| S1 (10,000) | 5 | 1 | 10,000 | 961 | 0 | yes |
| S2 (100,000) | 5 | 3 | 101,502 | 996 | 0 | yes |
| S3 (1,000,000) | 5 | 27 | 1,001,968 | 998 | 0 | yes |
| S4 (5,000,000) | 3 | 133 | 5,001,993 | 1,000 | 0 | yes |
| S5 (10,000,000) | 3 | 265 | 10,001,994 | 1,000 | 0 | yes |

### Cost of one table lookup (untimed decomposition)

Median per (table, key) pair. The file columns include the cost of Python's `open`.

| scale | load tables | table ruled out by the filter: `may_contain` / full `lookup` (µs) | table that holds the key: `may_contain` (µs) | `locate` (µs) | open + close (µs) | open + seek + read block (µs) | full `lookup` (µs) | false positive: full `lookup` (µs) | BLAKE2b digest alone (µs) | build filter (µs per key) | build filter (ms per table) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S2 (100,000) | 2 | 1.00 / 1.00 | 2.13 | 0.44 | 29.5 | 37.0 | 56.2 | 54.8 | 0.39 | 2.33 | 88.0 |
| S3 (1,000,000) | 26 | 1.19 / 1.24 | 2.30 | 0.61 | 28.3 | 35.5 | 56.0 | 55.9 | 0.40 | 2.37 | 89.6 |
| S4 (5,000,000) | 132 | 1.14 / 1.19 | 2.60 | 0.89 | 30.8 | 36.7 | 63.3 | 55.7 | 0.38 | 2.41 | 90.9 |
| S5 (10,000,000) | 264 | 1.37 / 1.45 | 2.69 | 0.94 | 30.8 | 35.8 | 58.8 | 61.7 | 0.40 | 2.32 | 87.8 |

### Scaling

Fits over the scales that have SSTables (four points, so descriptive only).

| GET | fit over the scales that have SSTables | log-log slope | R² | p50 largest / smallest scale | linear slope (µs per unit of x) |
|---|---|---|---|---|---|
| hit | V4 p50 vs records | 0.351 | 0.9015 | 5.37 | 2.306e-05 |
| hit | V3 p50 vs records | 0.170 | 0.8635 | 2.31 | 6.07e-06 |
| hit | V4 p50 vs SSTables | 0.328 | 0.8856 | 5.37 | 0.8715 |
| hit | V3 p50 vs SSTables | 0.159 | 0.8461 | 2.31 | 0.2294 |
| miss | V4 p50 vs records | 1.118 | 0.9973 | 163.53 | 5.664e-05 |
| miss | V3 p50 vs records | 1.077 | 0.9993 | 139.83 | 0.00146 |
| miss | V4 p50 vs SSTables | 1.053 | 0.9941 | 163.53 | 2.14 |
| miss | V3 p50 vs SSTables | 1.016 | 0.9999 | 139.83 | 55.19 |

p50 divided by the number of SSTables:

| scale | V4 miss p50 per SSTable (µs) | V3 | V4 hit p50 per SSTable (µs) | V3 |
|---|---|---|---|---|
| S2 (100,000) | 1.700 | 51.85 | 26.300 | 23.300 |
| S3 (1,000,000) | 1.335 | 52.88 | 2.888 | 2.085 |
| S4 (5,000,000) | 2.043 | 56.83 | 1.227 | 0.582 |
| S5 (10,000,000) | 2.106 | 54.92 | 1.070 | 0.408 |

### Writes (`put_load`, all writes including flush-triggering ones)

| scale | V4 p50 / p95 / p99 (µs) | V3 p50 / p95 / p99 (µs) | V4 ops/s | V3 ops/s | V4 / V3 ops/s | V4 slowest write (ms) | V3 slowest write (ms) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 4.7 / 5.8 / 13.3 | 4.5 / 5.8 / 10.9 | 189,834 | 198,470 | 0.956 | 1.2 | 1.4 |
| S2 (100,000) | 5.0 / 6.5 / 13.0 | 4.8 / 6.2 / 14.3 | 116,529 | 155,752 | 0.748 | 154.5 | 61.0 |
| S3 (1,000,000) | 4.8 / 6.3 / 12.5 | 4.9 / 6.3 / 11.7 | 108,143 | 145,812 | 0.742 | 181.4 | 97.5 |
| S4 (5,000,000) | 4.8 / 6.3 / 11.6 | 4.8 / 6.3 / 11.2 | 107,186 | 146,793 | 0.730 | 212.8 | 95.8 |
| S5 (10,000,000) | 4.8 / 6.2 / 11.4 | 4.9 / 6.2 / 11.0 | 107,845 | 144,622 | 0.746 | 219.4 | 122.7 |

Flush-triggering writes (pooled over runs) and flush duration:

| scale | flush-triggering writes (pooled, V4) | V4 trigger p50 / p95 / p99 / max (ms) | V3 trigger p50 / p95 / p99 / max (ms) | median flush (ms): V4 / V3 | median inside `write_sstable` (ms): V4 / V3 | median rest of flush (ms): V4 / V3 | amortized flush (µs per PUT): V4 / V3 |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | — | — | — | — | — | — |
| S2 (100,000) | 10 | 153.8 / 185.4 / 185.4 / 185.4 | 60.7 / 67.1 / 67.1 / 67.1 | 153.4 / 60.6 | 133.3 / 42.9 | 18.8 / 18.3 | 3.06 / 1.18 |
| S3 (1,000,000) | 130 | 149.6 / 167.5 / 187.4 / 195.0 | 57.7 / 66.9 / 106.2 / 113.4 | 149.4 / 57.7 | 130.7 / 40.1 | 18.2 / 17.7 | 3.91 / 1.53 |
| S4 (5,000,000) | 396 | 149.6 / 164.0 / 185.6 / 244.0 | 57.2 / 64.3 / 95.8 / 115.2 | 149.5 / 57.1 | 131.7 / 39.6 | 18.3 / 18.0 | 3.98 / 1.50 |
| S5 (10,000,000) | 792 | 149.3 / 164.0 / 200.8 / 222.2 | 57.5 / 65.1 / 106.9 / 122.9 | 149.2 / 57.4 | 130.8 / 39.7 | 18.3 / 17.7 | 3.95 / 1.50 |

Non-flush writes and table size:

| scale | V4 non-flush p50 / p95 / p99 (µs) | V3 non-flush p50 / p95 / p99 (µs) | V4 table bytes | V3 table bytes |
|---|---|---|---|---|
| S1 (10,000) | 4.7 / 5.8 / 13.3 | 4.5 / 5.8 / 10.9 | — | — |
| S2 (100,000) | 5.0 / 6.5 / 13.0 | 4.8 / 6.2 / 14.3 | 4,787,208 | 4,739,958 |
| S3 (1,000,000) | 4.8 / 6.3 / 12.4 | 4.9 / 6.3 / 11.7 | 4,787,208 | 4,739,958 |
| S4 (5,000,000) | 4.8 / 6.3 / 11.6 | 4.8 / 6.3 / 11.2 | 4,787,208 | 4,739,958 |
| S5 (10,000,000) | 4.8 / 6.2 / 11.4 | 4.9 / 6.2 / 11.0 | 4,787,208 | 4,739,958 |

**Overwrite** (1,000 per run, pooled):

| scale | V4 p50 / p95 / p99 (µs) | V3 p50 / p95 / p99 (µs) | V4 / V3 p50 | flush-triggering ops (V4 / V3) |
|---|---|---|---|---|
| S1 (10,000) | 5.2 / 6.5 / 20.1 | 5.4 / 7.0 / 25.3 | 0.96 | 0 / 0 |
| S2 (100,000) | 5.0 / 6.2 / 18.7 | 5.0 / 6.5 / 25.2 | 1.00 | 0 / 0 |
| S3 (1,000,000) | 4.7 / 6.0 / 13.5 | 4.7 / 6.1 / 17.9 | 1.00 | 0 / 0 |
| S4 (5,000,000) | 5.1 / 5.9 / 18.8 | 4.9 / 6.2 / 22.3 | 1.04 | 0 / 0 |
| S5 (10,000,000) | 4.9 / 6.1 / 18.8 | 4.6 / 6.3 / 16.6 | 1.07 | 0 / 0 |

**Delete** (1,000 per run, pooled):

| scale | V4 p50 / p95 / p99 (µs) | V3 p50 / p95 / p99 (µs) | V4 / V3 p50 | flush-triggering ops (V4 / V3) |
|---|---|---|---|---|
| S1 (10,000) | 5.0 / 5.7 / 8.2 | 5.0 / 7.9 / 13.0 | 1.00 | 0 / 0 |
| S2 (100,000) | 5.1 / 6.4 / 8.9 | 4.9 / 5.9 / 10.5 | 1.04 | 0 / 0 |
| S3 (1,000,000) | 4.5 / 6.4 / 7.8 | 4.4 / 6.0 / 10.0 | 1.02 | 0 / 0 |
| S4 (5,000,000) | 4.8 / 5.5 / 8.4 | 5.0 / 5.6 / 14.4 | 0.96 | 0 / 0 |
| S5 (10,000,000) | 4.7 / 5.2 / 8.5 | 5.0 / 5.9 / 9.4 | 0.94 | 0 / 0 |

### Open / recovery

`KVStore(path)` on the closed database after the load: footer, index and filter validation of every table plus replay of the surviving WAL. V3 values from `v3_benchmark.json`.

| scale | SSTables opened | index bytes read | filter bytes read | V4 open per run (ms) | V4 median (ms) | V3 median (ms) | V4 / V3 | V4 listing / SSTable opens / WAL replay / index + filter re-read I/O only / filter CRC only (ms, median) | reopen after mutations (ms, median): V4 / V3 |
|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 0 | 0 | 18, 17, 20, 18, 19 | 18.3 | 20.9 | 0.87 | 0.08 / 0.0 / 10.1 / 0.0 / 0.00 | 19.1 / 20.5 |
| S2 (100,000) | 2 | 108,652 | 94,468 | 36, 40, 36, 40, 37 | 37.2 | 37.2 | 1.00 | 0.09 / 2.5 / 27.1 / 0.2 / 0.03 | 41.5 / 37.6 |
| S3 (1,000,000) | 26 | 1,412,476 | 1,228,084 | 51, 54, 55, 52, 56 | 53.8 | 55.2 | 0.97 | 0.15 / 27.9 / 16.5 / 1.6 / 0.39 | 55.4 / 55.0 |
| S4 (5,000,000) | 132 | 7,171,032 | 6,234,888 | 172, 193, 172 | 172.5 | 170.9 | 1.01 | 1.27 / 152.0 / 11.5 / 9.0 / 1.85 | 173.7 / 168.2 |
| S5 (10,000,000) | 264 | 14,342,064 | 12,469,776 | 378, 337, 359 | 358.6 | 353.0 | 1.02 | 0.62 / 329.8 / 27.0 / 18.3 / 3.82 | 336.9 / 338.5 |

### Memory (observed process private bytes, and table heap)

Delta in private bytes of a fresh process after loading N keys and after opening the resulting database (median and min–max over 3 probes), and the Python heap retained by opening the tables (tracemalloc).

| scale | SSTables | V4 private delta after load (MB) | V3 after load (MB) | V4 after open (MB) | V3 after open (MB) | V4 − V3 after open (MB, medians) | peak working set after load (MB): V4 / V3 | heap retained by opening the tables: index + filters (MB, tracemalloc) |
|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 1.6 (1.6–1.9) | 1.3 (1.0–1.8) | 2.3 (1.8–2.6) | 1.7 (1.6–2.5) | +0.6 | 27.2 / 25.6 | — |
| S2 (100,000) | 2 | 7.4 (6.9–7.5) | 6.9 (6.6–7.4) | 5.8 (5.8–5.9) | 5.1 (4.1–5.2) | +0.7 | 35.8 / 34.4 | 0.52 |
| S3 (1,000,000) | 26 | 17.9 (17.3–17.9) | 16.2 (16.2–16.9) | 10.5 (9.7–11.6) | 10.3 (8.9–10.6) | +0.2 | 43.6 / 41.5 | 6.73 |
| S4 (5,000,000) | 132 | 48.1 (47.6–48.5) | 41.2 (40.2–41.4) | 39.7 (39.3–40.3) | 33.1 (32.8–33.3) | +6.6 | 74.1 / 66.6 | 34.15 |
| S5 (10,000,000) | 264 | 86.2 (86.0–87.0) | 72.3 (72.1–73.1) | 81.7 (81.5–82.9) | 66.6 (66.1–67.1) | +15.2 | 112.0 / 97.3 | 68.29 |

| scale | filter bytes on disk | filter heap (`bytes` objects, exact) | total heap retained (tracemalloc) | of which index + table objects (by difference) | V3 heap retained (index + table objects) | V4 total / V3 total |
|---|---|---|---|---|---|---|
| S2 (100,000) | 94,468 | 94,534 | 518,510 | 423,976 | 423,648 | 1.224 |
| S3 (1,000,000) | 1,228,084 | 1,228,942 | 6,727,030 | 5,498,088 | 5,496,416 | 1.224 |
| S4 (5,000,000) | 6,234,888 | 6,239,244 | 34,147,932 | 27,908,688 | 27,901,080 | 1.224 |
| S5 (10,000,000) | 12,469,776 | 12,478,488 | 68,294,520 | 55,816,032 | 55,801,032 | 1.224 |

### Space and write amplification (derived)

| scale | total persistent bytes (V4) | space amplification V4 | V3 | rewrite factor V4 | V3 | bytes per payload byte V4 | V3 |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 1,388,000 | 1.3834 | 1.3834 | 1.0000 | 1.0000 | 1.1266 | 1.1266 |
| S2 (100,000) | 12,751,240 | 1.1603 | 1.1517 | 1.7630 | 1.7555 | 1.9713 | 1.9629 |
| S3 (1,000,000) | 126,790,120 | 1.1434 | 1.1323 | 2.0026 | 1.9927 | 2.2373 | 2.2263 |
| S4 (5,000,000) | 633,561,840 | 1.1418 | 1.1305 | 2.0190 | 2.0089 | 2.2555 | 2.2442 |
| S5 (10,000,000) | 1,266,975,680 | 1.1415 | 1.1303 | 2.0191 | 2.0090 | 2.2556 | 2.2443 |

### 10,000-key head-to-head

**V0 baseline workload, default 4 MiB limit (no flush occurs)** (medians over 5 runs; V4 / V3 / V4 ÷ V3; µs)

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 0.5 / 0.5 / 1.00 | 0.7 / 0.8 / 0.87 | 3.2 / 3.8 / 0.84 |
| get_miss | 0.4 / 0.4 / 1.00 | 0.6 / 0.6 / 1.00 | 1.9 / 2.0 / 0.95 |
| mixed_all | 0.6 / 0.6 / 1.00 | 6.4 / 5.3 / 1.21 | 9.7 / 14.5 / 0.67 |
| mixed_get | 0.5 / 0.5 / 1.00 | 0.8 / 0.7 / 1.14 | 1.4 / 0.8 / 1.75 |
| mixed_put | 4.9 / 5.0 / 0.98 | 7.3 / 7.5 / 0.97 | 12.7 / 31.1 / 0.41 |
| mixed_delete | 5.1 / 4.9 / 1.04 | 18.6 / 13.7 / 1.36 | 18.6 / 13.7 / 1.36 |
| put_load | 4.3 / 4.6 / 0.93 | 5.4 / 5.5 / 0.98 | 13.5 / 14.6 / 0.92 |
| put_sync | 1,903.4 / 1,882.6 / 1.01 | 2,183.1 / 2,093.4 / 1.04 | 3,964.2 / 3,824.1 / 1.04 |

**V1 extended workload, default limit (no flush occurs)** (medians over 5 runs; V4 / V3 / V4 ÷ V3; µs)

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 0.4 / 0.4 / 1.00 | 0.5 / 0.5 / 1.00 | 0.7 / 0.7 / 1.00 |
| get_miss | 0.4 / 0.4 / 1.00 | 0.5 / 0.5 / 1.00 | 0.6 / 0.6 / 1.00 |
| mixed_all | 0.5 / 0.5 / 1.00 | 5.1 / 5.2 / 0.98 | 7.3 / 7.1 / 1.03 |
| mixed_get | 0.5 / 0.4 / 1.25 | 0.7 / 0.6 / 1.17 | 0.9 / 0.8 / 1.12 |
| mixed_put | 4.7 / 4.7 / 1.00 | 6.2 / 6.0 / 1.03 | 16.7 / 17.7 / 0.94 |
| mixed_delete | 4.6 / 4.7 / 0.98 | 6.0 / 5.9 / 1.02 | 12.8 / 12.6 / 1.02 |
| put_load | 4.3 / 4.4 / 0.98 | 5.3 / 5.4 / 0.98 | 13.3 / 14.8 / 0.90 |
| put_sync | 1,888.0 / 1,887.4 / 1.00 | 2,172.6 / 2,137.7 / 1.02 | 3,912.1 / 3,730.7 / 1.05 |

**V0 baseline workload, 16 KiB limit (10,000 keys spread over many tables)** (medians over 5 runs; V4 / V3 / V4 ÷ V3; µs)

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 93.1 / 54.0 / 1.72 | 156.3 / 82.9 / 1.89 | 219.7 / 140.6 / 1.56 |
| get_miss | 102.4 / 3,031.5 / 0.03 | 219.0 / 3,751.2 / 0.06 | 314.6 / 4,322.0 / 0.07 |
| mixed_all | 81.4 / 63.7 / 1.28 | 146.8 / 129.1 / 1.14 | 295.9 / 186.1 / 1.59 |
| mixed_get | 99.6 / 72.9 / 1.37 | 150.8 / 130.5 / 1.16 | 198.8 / 185.8 / 1.07 |
| mixed_put | 7.1 / 7.8 / 0.91 | 33.0 / 27.3 / 1.21 | 27,260.7 / 36,425.2 / 0.75 |
| mixed_delete | 7.2 / 7.7 / 0.94 | 20.4 / 21.7 / 0.94 | 20.4 / 21.7 / 0.94 |
| put_load | 4.6 / 4.5 / 1.02 | 10.8 / 11.6 / 0.93 | 84.9 / 87.3 / 0.97 |
| put_sync | 1,884.9 / 1,890.5 / 1.00 | 2,236.3 / 2,269.1 / 0.99 | 4,063.7 / 5,891.0 / 0.69 |

## Analysis

Everything below refers to the measurements above. Where a cause is offered, it's marked as a candidate or as measured.

1. **Primary question, answered for the miss workload: the filter removes almost all of the SSTable work and the latency follows.** A V4 miss consults every table, as V3 does (2, 26, 132 and 264 at S2, S3, S4 and S5), but the filter answers "no" for 99.1%–100.0% of those checks. So a miss opens 0.00, 0.20, 1.20 and 2.32 tables on average (V3: 2, 26, 132, 264) and decodes 0.0, 2.2, 13.2 and 25.5 records (V3: 22, 286, 1,452, 2,904). Bytes decoded are 0, 273, 1,637 and 3,164 against V3's 2,728, 35,464, 180,048 and 360,096. The miss p50 fell from 103.7, 1,375, 7,502 and 14,500 µs to 3.4, 34.7, 269.7 and 556.0 µs at S2, S3, S4 and S5 (V4 / V3 = 0.033, 0.025, 0.036, 0.038; -96.7%, -97.5%, -96.4%, -96.2%; V4 is 30.5, 39.6, 27.8 and 26.1 times faster). p95: 4.8, 112.1, 494.1, 909.6 µs against 173.3, 1,871, 8,833, 18,534 µs. p99: 55.5, 161.9, 712.8, 1,154 µs against 315.4, 2,276, 9,206, 19,400 µs. At S5 a miss takes 0.6 ms instead of 14.5 ms. At S1 nothing is flushed and V3 and V4 run the same memtable-only path (p50 0.4 vs 0.4 µs). Pooled misses are 500,000, 1,000, 500, 150 and 150 at S1–S5, so the p99 of a miss rests on 150 samples at S4 and S5 (the 149th of 150 values) and is approximate. The structural counts are asserted equal to counts derived from the layout and from filters rebuilt from the keys with `bloom.py` (the V4 structural analysis), for every sampled GET of every run.

2. **Skip rate and false positives.** Over the instrumented GETs, the filter said "no" to 99.09%–100.00% of the checks made for misses and 35.27%–98.47% of those made for hits. A hit checks every table newer than the one holding the key, which should answer "no", and then the holding table, which must answer "maybe". A "maybe" counts as a false positive only when the table lookup then returned not-found. Of the checks on tables that don't hold the key, 0.00%–0.91% (misses) and 0.79%–1.25% (hits) were false positives. Per miss that's 0.00, 0.20, 1.20 and 2.32 false positives on average (each opens a table and decodes its last block of 11 records). The sampled GETs use the same keys in every run, so these totals repeat the same GETs 3–5 times and aren't independent samples. For hits, every false positive is on a table newer than the one holding the key, where the sparse index then rules the key out by its first key with no file access: 0.01, 0.12, 0.53 and 1.09 per hit on average, all of them resolved without opening a file. **Measured false-positive rate (absent-key probe).** Asking each table's filter about 20,000+ keys that are in no table gives 0.811%, 0.813%, 0.826%, 0.829% and 0.820% at S1–S5 (1,000,000, 1,000,002, 1,000,026, 2,660,000 and 5,300,000 checks), against 0.819% from the formula (10 bits per key, 7 hashes; each table's own size is used). The 95% Wilson interval contains the formula value at 5 of 5 scales, and at S5 it's 0.813%–0.828%. Individual tables vary: the per-table rate at S5 ranges 0.64%–1.04% (median 0.82%). The binomial sampling noise of 20,000 probes at 0.82% is about 0.06 percentage points (1 σ), so part of that spread is probe noise, and the rest wasn't separated. The precision claim is limited to these tables (37,787 records each, plus the table written by the validation flush, which holds the memtable's contents), and the probe says nothing about other table sizes.

3. **The invariant held at scale.** In 21 runs, after the final reopen, every table was scanned in full (a table-by-table check that the filter rebuilt from the keys equals the stored filter byte for byte) and each key, tombstones included, was asked of its own table's filter. That's 50,579,311 key checks, 20,775 of them tombstone keys (the delete phase's tombstones, moved into a table by a flush before the scan), with **0 false negatives**. Every sampled hit GET returned the expected value and every deleted key stayed deleted after the reopen, so no stored key was lost to a filter answer. This is a check on the keys of this workload, not a proof. The proof sketch is in v4-design.md.

4. **Hits did not improve and got slower with table count.** The hit p50 went from 46.6, 54.2, 76.8 and 107.7 µs (V3) to 52.6, 75.1, 161.9 and 282.4 µs (V4) at S2, S3, S4 and S5: V4 / V3 = 1.13, 1.39, 2.11, 2.62, +12.9%, +38.6%, +110.8%, +162.2%. p95: 86.9, 137.1, 301.5, 520.4 against 76.0, 84.6, 135.9, 185.9 µs. p99: 177.8, 246.7, 465.9, 701.3 against 154.9, 174.1, 271.8, 286.0 µs (V4 / V3 1.15, 1.42, 1.71, 2.45). At S1 nothing is flushed, so there's no difference in the code path (0.4 vs 0.4 µs p50). The structural counts show why there was nothing to gain: a V3 hit already opened 0.72, 1.00, 0.99 and 1.00 tables on average (never more than one, since every newer table is ruled out by its first key at no file cost), and V4 opens the same 0.72, 1.00, 0.99 and 1.00. Records decoded per hit are identical (6.1, 8.7, 8.4, 8.7). What changed is the work on the tables the hit passes. V3 asked each newer table's index (an in-memory binary search), while V4 asks each table's filter first (1.1, 13.6, 67.5 and 136.4 Bloom checks per hit on average). In the separate decomposition at S5, a table the filter rules out costs 1.37 µs for `may_contain` (1.45 µs for the whole `lookup`), of which the BLAKE2b digest alone is 0.40 µs. The V3 index search on the (table, hit key) pairs costs 0.94 µs there. As a consistency check from those parts, (136.4 tables consulted per hit minus the fraction answered by a table) × 1.37 µs plus that fraction × the 58.8 µs lookup of the table that holds the key = 244.8 µs, against a measured mean hit of 289.5 µs at S5 (the 44.7 µs difference wasn't attributed). Between S2 and S5 the V4 hit p50 grows by 229.8 µs while the tables passed per hit grow by 135.3. That's 1.70 µs per additional table passed in V4 against 0.45 µs in V3 (same two scales). The V4 figure is above the 1.37 µs decomposition cost of a check, and the difference wasn't attributed. In this decomposition the filter check costs more than the index search, and the extra cost is per table passed, so the hit regression grows with the number of tables. A hit still pays for the file open of the one table that holds it (open + close 30.8 µs), and the filter doesn't reduce that.

5. **Where the remaining miss time goes (separate untimed measurement).** At S5, a table the filter rules out costs 1.37 µs to check, and a false positive that reaches the last block costs a full lookup of 61.7 µs (`may_contain` 2.65 µs, `locate` 0.47, open + close 29.9, open + seek + read the block 35.6). A miss is therefore 264 filter checks plus 2.32 false positives on average: 264 × 1.37 + 2.32 × 61.7 = 505.8 µs, against a measured miss mean of 584.3 µs (p50 556.0 µs). The difference (78.5 µs, 13% of the mean) wasn't attributed. The filter checks are about 72% of the modelled time, and false positives are the rest. The cost of a filter check grows with the table count in the decomposition (1.00–1.37 µs from S2 to S5, while the BLAKE2b digest is constant at 0.38–0.40 µs), and why wasn't investigated.

6. **Scaling with database size.** A miss is still linear in the number of tables. Over the scales that have SSTables (S2, S3, S4, S5) the log-log slope of the V4 miss p50 against the SSTable count is 1.053 (R² 0.9941; V3: 1.016, R² 0.9999), and against the record count it's 1.118 (V3: 1.077). The filter changed the per-table constant, not the growth: the miss p50 per SSTable is 1.33–2.11 µs in V4 (1.70, 1.33, 2.04 and 2.11 µs at S2, S3, S4 and S5) against 51.9–56.8 µs in V3. V4's per-table figure rises from 1.70 to 2.11 µs across the scales while V3's stays within 51.9–56.8. The decomposition accounts for part of it (the per-table filter check rises 1.00 → 1.37 µs) and the false positives per table are about constant (0.00%–0.91% of checks), but the full rise was **not explained**. The hit p50 grows 5.37× from S2 to S5 (V3: 2.31×) while records grow 100×: log-log slope against records 0.351 (R² 0.90; V3: 0.170), against SSTables 0.328 (V3: 0.159), and hit p50 per SSTable 26.30, 2.89, 1.23, 1.07 µs (V3: 23.30, 2.08, 0.58, 0.41). With four scales, these are descriptive. With a filter per table, every GET that reaches the tables still touches every table's filter. The work per GET is no longer dominated by file opens, but it's still O(tables).

7. **Run-to-run variation.** The between-run spread of the V4 hit p50 ((max − min) / median of the per-run p50s) is 14%, 34%, 7% and 7% at S2, S3, S4 and S5 (V3: 14%, 15%, 10%, 30%). For misses it's 31%, 21%, 35% and 5% (V3: 22%, 9%, 2%, 31%). The five run p50s at S3 are hits 71, 71, 93, 69, 80 µs and misses 34, 33, 33, 33, 40 µs. The causes weren't identified and are **unexplained**, and this benchmark doesn't establish a noise floor. The V3 → V4 miss differences are much larger than these spreads. The hit difference at S2 (+12.9% p50) is about the size of the spreads and can't be interpreted on its own, while at S4–S5 (+110.8%, +162.2%) the differences are larger than the between-run spreads.

8. **Write path: flushes got more expensive.** Between flushes a V4 write costs what a V3 write does (non-flush p50 4.8 µs at S5, V3 4.9). Each flush now builds a filter. The median flush takes 153.4, 149.4, 149.5 and 149.2 ms in V4 against 60.6, 57.7, 57.1 and 57.4 ms in V3 at S2, S3, S4 and S5, and inside `write_sstable` it takes 133.3, 130.7, 131.7, 130.8 vs 42.9, 40.1, 39.6, 39.7 ms. At S5 that's +91.1 ms per table. A separate measurement of `bloom.add` over one table's 37,787 keys gives 87.8 ms (2.32 µs per key), the same size as the increase (the V3 time is from an earlier invocation, so the match is approximate). Flush-triggering writes at S5: p50 / p99 / max 149.3 / 200.8 / 222.2 ms (V3 57.5 / 106.9 / 122.9 ms), and the amortized flush cost is 3.95 µs per PUT against 1.50. Sustained load throughput is 0.748, 0.742, 0.730 and 0.746× V3's at S2, S3, S4 and S5 (107,845 vs 144,622 ops/s at S5). At S1, with no flush, the ratio is 0.956, which is the size of the difference between separate invocations of the same code path. The V4 table is 4,787,208 bytes against 4,739,958 (1.00% larger). Overwrites and deletes never triggered a flush at any scale (as in V3), so a flush-triggering delete was again not observed at scale, and their latencies are unaffected within the spread (overwrite p50 V4 / V3 1.00–1.07; delete 0.94–1.04).

9. **Recovery.** Opening a V4 database takes 37, 54, 172 and 359 ms at S2, S3, S4 and S5, against 37, 55, 171 and 353 ms for V3 (V4 / V3 = 1.00, 0.97, 1.01, 1.02). Open now also reads and validates every table's filter: at S5, 12,469,776 filter bytes (V3 read 14,342,064 index bytes, which V4 reads too). In the separate repetition, re-reading the footers, index blocks and filter blocks without parsing takes 18 ms at S5 (V3, index only: 12 ms), and the filter CRC alone takes 3.8 ms. The table-open step takes 330 ms (V3: 301 ms) of the 359 ms. The between-run spread of the V4 open time is 12% at S5 and the V3 reference is from an earlier invocation, so differences of a few percent between V3 and V4 can't be interpreted. The filter CRC alone is 1.1% of the S5 open. The cost of reading and keeping the filter blocks wasn't separated from the rest of the table-open step.

10. **Storage and memory.** The filter adds 10.00 bits (1.25 bytes) per table record: 12,469,776 bytes at S5 (0.99% of the SSTable bytes; the sparse index is 1.13%; space amplification 1.1415 against V3's 1.1303). Filters are held in memory. Opening the tables retains 68.3 MB of Python heap at S5, of which 12.48 MB is the filter `bytes` objects (1.001× the on-disk filter bytes) and the remainder (55.8 MB) is the index and table objects, equal to V3's 55.8 MB within 15 KB (total 1.224× V3's). Observed process private bytes after opening are 81.7 MB (V3: 66.6 MB), +15.2 MB, and after the load 86.2 MB (V3: 72.3 MB), +13.9 MB. The post-open difference from V3 is +0.7, +0.2, +6.6 and +15.2 MB at S2, S3, S4 and S5. These are in line with the filter bytes (12.5 MB at S5), but private-byte deltas depend on allocator state and are less exact than the tracemalloc figure. Filter memory is a constant 1.25 bytes per flushed record and grows with the data.

11. **10,000-key workloads.** With the default limit nothing flushes, so V4 runs V3's memtable-only path. No phase's p50 differs from V3's by more than 7% (baseline workload) or 25% (extended), and the largest differences are 0.1 µs clock ticks. With the 16 KiB limit (67 tables of 148 records each, filters of 185 bytes), V4's GET hit p50 is 93.1 µs against V3's 54.0 µs (1.72×), the miss is 102.4 µs against 3,032 µs (0.034×), and the mixed workload's GETs are 99.6 µs against 72.9 µs (1.37×). `sync=True` PUTs are unchanged (1,885 vs 1,890 µs p50). The tail (p99) of the mixed PUT and the sync PUT is lower than V3's in these runs (27,261 vs 36,425 µs; 4,064 vs 5,891 µs). The store's write path is unchanged apart from the flush, and this was **not investigated**, so it isn't attributed to the filter.

12. **Tradeoffs.** The filter did what it was added for on the workload that needs it. It removed 99.1%–100.0% of the table opens of a miss, cut records decoded per miss by 110–130× (at S2 no miss decoded any record) and cut miss latency by 26.1–39.6× (p50) at S2, S3, S4 and S5. It did nothing for hits, whose one file open it can't avoid, and its per-table check cost made hits 1.13–2.62× V3's p50 (growing with the table count), because V3's index already ruled out the newer tables almost for free. It made each flush about 2.60× as slow at S5 (149.2 vs 57.4 ms; load throughput 0.75× V3's), added 0.99% to the disk size and about 12.5 MB of memory at S5, and left open time within the run-to-run spread. A miss is still linear in the table count (0.6 ms at S5, 2.0× a hit; V3: 135×). This workload is the best case for the filter on misses (every miss key is absent from every table) and a case where V3's index already made hits cheap (ascending load, disjoint table ranges). See Limitations.

## Limitations

- **Workload layout.** Keys are loaded in ascending order, so tables have disjoint ranges. A hit on a key in table *j* checks the filters of the newer tables, where V3 already skipped them by their first key at no file cost. A random-order load would make table ranges overlap, so V3's index could no longer rule out as many newer tables and the filter would have more to skip on hits. That wasn't measured. Miss keys exceed every stored key, so V3 opens the last block of every table, and a miss key inside the stored range, or a deleted key, would behave differently. Hits are uniform over the keyspace.
- **Absent keys only above the range in the GET workload.** The false-positive probe uses keys in no table, but the timed miss GETs and the instrumented misses use `key_of(N + i)`. For a key from a different distribution, the false-positive count of a GET could differ.
- **One filter configuration.** Only 10 bits per key and 7 hashes were measured. How filter size, false-positive rate, memory, open time and flush time trade off as bits per key changes wasn't measured. The index interval was also fixed at 16.
- **Memtable-resident keys.** At the end of the load, 0.2–24% of keys sit in the memtable and are answered without a table. The overwrite and delete phases also hit the memtable.
- **One memtable limit at scale.** Only the 4 MiB limit was measured at scale, and the 16 KiB limit on the 10,000-key workloads.
- **Sample counts.** GET sample counts are V3's, kept for comparability. Pooled misses are 1,000, 500, 150 and 150 at S2–S5, so the p99 of a miss at S4 and S5 rests on 150 samples (the 149th value) and is approximate. V4 misses are cheap enough that larger counts would have been affordable, but they weren't used. Read work is measured on 200 hits and 50 misses per run, and it equals the derived values. Since the keys are the same in every run, the runs don't add independent samples of the structural counts.
- **False-positive rate precision.** One probe per scale, on fixed filters, with a Wilson interval that assumes independent trials. It bounds the sampling noise of the probe, not the variation between filters built from other key sets. The probe keys have a different prefix from the stored keys. The hash is BLAKE2b, whose output shouldn't depend on such structure, but that wasn't tested with other key shapes.
- **Run-to-run variation.** The benchmark didn't establish a noise floor. The V3 reference was measured in an earlier invocation, so differences of a few percent between V3 and V4 (flush time, write throughput, anything at S1) can't be interpreted.
- **Machine state.** The operator kept the machine idle during the ladder (not independently verified). Windows Defender and other resident security software stayed enabled, no CPU affinity was set, and timings remain sensitive to machine load.
- **Application-level read work.** Records and bytes decoded are counted in the record reader. Physical I/O wasn't measured, and Python's buffered reader may transfer more than the block.
- **Cost decomposition.** It's a separate warm, batched measurement. It explains the per-table cost but isn't an instrumented trace of the timed GETs.
- **Cold cache.** All numbers are warm-cache, so nothing here says how the filter changes cold reads, where skipping a file open matters most.
- **Memory.** Private-byte deltas depend on allocator state and memtable fill and aren't a function of N alone. The tracemalloc figure counts Python allocations only.
- **Open breakdown.** Component times come from a separate warm repetition and don't add up to the timed open.
- **Delete-triggered flushes.** None occurred in any scaling run.
- **Not measured:** physical I/O, concurrent access, cold-cache behavior, random-order loads, other filter or index parameters, other flush limits at scale, flush-triggering deletes, and directories mixing V2, V3 and V4 tables.

## Environment

**Environment:** Python 3.10.6; Windows-10-10.0.26300-SP0; Intel64 Family 6 Model 183 Stepping 1, GenuineIntel; 24 logical CPUs; 17.0 GB RAM; seed 42; key 11 B; value 100 B; `sync=False` for the scaling runs; default `memtable_limit_bytes` = 4 MiB; index interval 16; Bloom 10 bits per key, 7 hashes.

# V5 benchmark

Purpose: measure where the block cache ([v5-design.md](v5-design.md)) helps, where it doesn't, and how that depends on locality and cache capacity. V5 is V4 plus an in-memory LRU cache of raw SSTable block bytes (8,388,608 bytes by default). The WAL, memtable, flush, recovery, Bloom filters, sparse index and on-disk formats are V4's. The cache can only help a workload that revisits blocks, so two workloads are measured and **never mixed in one table**: the **standard workload** (the V0–V4 comparability workload, unchanged) and a separate **controlled locality workload** built to revisit a small set of flushed blocks. V0–V4 results are references and aren't modified. **All numbers are warm OS-cache numbers.** Every figure in this section comes from `benchmarks/results/v5_benchmark.json`, `v5_structural.json` and the V4 reference `v4_benchmark.json`.

## Methodology

### Standard workload (V0–V5 comparability)

The timed protocol is V4's, executed by V4's own code: `v4_run` and `baseline_run` in the benchmark script run unchanged with the V5 store (default cache), and only the per-GET timing, read-work, decomposition and false-positive helpers are replaced by cache-aware versions through module-level hooks. No production code is changed. The setup is the same: seed 42, key 11 B, value 100 B, 124-byte PUT records, `sync=False`, a fresh database in a temp directory for every run, only the store call timed (one `perf_counter_ns` pair per operation), and nearest-rank percentiles using the V0 code. The scale ladder is 10,000, 100,000, 1,000,000, 5,000,000 and 10,000,000 records with 5, 5, 5, 3 and 3 runs, each scale in its own fresh Python process, one after another on an idle machine. The memtable limit is 4 MiB (4,194,304 bytes of payload, 37,787 entries per table), the index interval is 16, and Bloom is 10 bits per key / 7 hashes. GET samples per run are hits 100,000, 5,000, 2,000, 1,000 and 1,000, and misses 100,000, 200, 100, 50 and 50, using the same keys V4 used. The workload semantics are the same as V3/V4: keys are loaded in ascending order, hits are uniform over the keyspace, and misses use keys above the stored range. The OS page cache is warm and is never manipulated, and nothing runs in parallel. The cache starts empty on every open, so the timed GETs run from an empty cache, hit keys first and then miss keys.

Added around the unchanged protocol, outside every timed interval:
- *Per-GET cache outcome.* The GET timing hook (`timed_get`) is replaced by a function that reads the cache's hit and miss counters before the clock starts and after it stops, and keeps a checksum of each result. A GET is classed **cache hit** (it made at least one block access and every one was a hit), **cache miss** (at least one physical block read) or **no block access** (answered by the memtable, or every table ruled out by its Bloom filter or the index).
- *Replays.* After the timed GET phases, the same GET sequence is replayed twice (untimed) with counters on `SSTable.lookup`, `may_contain`, `locate`, `_scan_block`, `_read_block`, `sstable._open_table` and the record reader: once with a fresh cache of the same capacity, once with capacity 0. The cached replay's cache counters must equal the timed run's (the run is deterministic), its physical block reads must equal its cache misses and its file opens, a GET whose accesses were all hits must open no file, and both replays must return exactly the timed run's results. All of this is asserted in every run.
- *V4-style structural sample.* 200 hit and 50 miss GETs per run, with a fresh cache, with V4's own counters; compared with V4's recorded sample for the same keys.
- *Memory probes* (3 per scale, fresh processes): process private bytes after load and after open, as for V4, plus the growth in private bytes while running the standard GET workload with the 8 MiB cache and with capacity 0. The difference is the cache's cost.
- *Derived model.* `structural.py` derives the block accesses from the table geometry and Bloom filters rebuilt from the keys, replays them through a separate implementation of the cache rules, and asserts agreement with every run's cache counters, physical reads, bytes read and decode work.

**10,000-key head-to-head** (`--baseline`): V0's baseline workload and V1's extended workload with the default limit (no flush occurs, so the cache is never consulted), and V0's baseline workload with the 16 KiB limit (many small tables). 5 runs each; every GET is checked against a reference dict.

### Controlled locality workload

Everything about the locality workload was fixed before it was measured.

- **Database.** 1,000,000 keys loaded in ascending order with the standard key and value format and the same 4 MiB memtable limit, giving 26 flushed tables of 37,787 records (61,412 blocks of up to 16 records) and a memtable holding the last keys. A fresh database per run.
- **Hot set (A, B).** 1,000 distinct flushed blocks drawn without replacement from all flushed blocks: 1,984,000 bytes of block data.
- **Operations and mix (C, D, E).** 200,000 GETs per configuration per run, in one fixed shuffled order. 90% go to the hot set (a uniformly random hot block, then a uniformly random key inside it). The other 10% are *cold*: each goes to a different block outside the hot set, so a cold block is never revisited. Every key is in a flushed table (none is answered by the memtable; asserted), and every GET scans exactly one block (asserted). The structural ceiling on the hit rate is therefore the number of repeat hot accesses divided by the GETs.
- **Capacities tested (F).** V4 (no cache), 0 (disabled), 256 KiB, 1 MiB, 2 MiB, 4 MiB and 8 MiB. V4 is the same store with no cache code on the read path, and V5 with capacity 0 runs the cache code but never stores a block. 1 MiB is below the hot set's bytes, 2 MiB just above it, and 4 and 8 MiB (the default) well above it.
- **Warm-up and first access (G, H).** None: each configuration starts from an empty cache, and the first access of every block is part of the measurement. GETs are classed hot-first-access, hot-repeat and cold, and by cache outcome, so first accesses can be read separately.
- **Cache reset (I).** Every configuration opens a new store instance on the run's database, so its cache is new and empty. The OS page cache is warm and isn't touched. Each run measures all configurations, in an order rotated from run to run, and there are 5 runs, each on a freshly built database.
- **Randomness.** `random.Random(47)` chooses blocks, keys and the order; load values come from the standard `Random(42)` stream. The plan is identical in every run.
- **Per configuration, two passes.** A timed pass (one `perf_counter_ns` pair per GET, with the cache outcome read outside it and every value checked), then an untimed pass on a fresh instance with the counters of the standard replays and per-GET invariants: a cache hit reads no block and opens no file, and a cache miss is exactly one block read and one file open. The two passes must give identical cache counters and the same hit/miss sequence. In run 1 only, a third pass measures Python heap retained by the cache (`tracemalloc`).

**Timing floor:** `perf_counter` ticks every 100 ns, so sub-microsecond memtable operations are quantized. Operations that reach a table take microseconds or more.

## Results

### 1. V4 → V5, standard workload

V5 and V4 percentiles are pooled over all runs at each scale (µs). Change = V5 relative to V4 (negative = faster). Ratios are computed from unrounded measured values. V4 values are from `v4_benchmark.json` (an earlier invocation).

**GET hit**

| scale | samples (pooled) | V5 p50 / p95 / p99 (µs) | V4 p50 / p95 / p99 (µs) | V5 / V4 (p50 / p95 / p99) | change (p50 / p95 / p99) | between-run spread of the p50 (max−min over median): V5 / V4 |
|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.5 / 0.6 / 0.8 | 0.4 / 0.6 / 0.9 | 1.25 / 1.00 / 0.89 | +25.0% / +0.0% / -11.1% | 0.0% / 25.0% |
| S2 (100,000) | 25,000 | 43.7 / 78.5 / 139.1 | 52.6 / 86.9 / 177.8 | 0.83 / 0.90 / 0.78 | -16.9% / -9.7% / -21.8% | 4.1% / 13.7% |
| S3 (1,000,000) | 10,000 | 73.9 / 111.0 / 189.1 | 75.1 / 137.1 / 246.7 | 0.98 / 0.81 / 0.77 | -1.6% / -19.0% / -23.3% | 11.3% / 33.9% |
| S4 (5,000,000) | 3,000 | 157.9 / 277.0 / 436.3 | 161.9 / 301.5 / 465.9 | 0.98 / 0.92 / 0.94 | -2.5% / -8.1% / -6.4% | 3.6% / 7.0% |
| S5 (10,000,000) | 3,000 | 265.5 / 489.1 / 681.3 | 282.4 / 520.4 / 701.3 | 0.94 / 0.94 / 0.97 | -6.0% / -6.0% / -2.9% | 8.1% / 7.2% |

**GET miss**

| scale | samples (pooled) | V5 p50 / p95 / p99 (µs) | V4 p50 / p95 / p99 (µs) | V5 / V4 (p50 / p95 / p99) | change (p50 / p95 / p99) | between-run spread of the p50 (max−min over median): V5 / V4 |
|---|---|---|---|---|---|---|
| S1 (10,000) | 500,000 | 0.5 / 0.6 / 0.8 | 0.4 / 0.6 / 0.8 | 1.25 / 1.00 / 1.00 | +25.0% / +0.0% / +0.0% | 20.0% / 25.0% |
| S2 (100,000) | 1,000 | 3.3 / 4.4 / 22.0 | 3.4 / 4.8 / 55.5 | 0.97 / 0.92 / 0.40 | -2.9% / -8.3% / -60.4% | 21.2% / 31.2% |
| S3 (1,000,000) | 500 | 32.0 / 93.5 / 124.1 | 34.7 / 112.1 / 161.9 | 0.92 / 0.83 / 0.77 | -7.8% / -16.6% / -23.3% | 2.8% / 21.3% |
| S4 (5,000,000) | 150 | 251.5 / 383.4 / 481.8 | 269.7 / 494.1 / 712.8 | 0.93 / 0.78 / 0.68 | -6.7% / -22.4% / -32.4% | 26.0% / 34.6% |
| S5 (10,000,000) | 150 | 507.5 / 869.9 / 1,003 | 556.0 / 909.6 / 1,154 | 0.91 / 0.96 / 0.87 | -8.7% / -4.4% / -13.1% | 11.4% / 4.6% |

**What the cache did in this workload** (the timed GET phases; the workload is deterministic, so the counters are the same in every run):

| scale | SSTables | SSTable MB | cache lookups (block accesses) | hits | misses | hit rate | insertions | evictions | entries | cache bytes | share of the 8 MiB capacity |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 0.0 | 0 | 0 | 0 | — | 0 | 0 | 0 | 0 | 0.0% |
| S2 (100,000) | 2 | 9.6 | 3,811 | 1,231 | 2,580 | 32.30% | 2,580 | 0 | 2,580 | 5,117,480 | 61.0% |
| S3 (1,000,000) | 26 | 124.5 | 1,982 | 37 | 1,945 | 1.87% | 1,945 | 0 | 1,945 | 3,848,960 | 45.9% |
| S4 (5,000,000) | 132 | 631.9 | 1,058 | 15 | 1,043 | 1.42% | 1,043 | 0 | 1,043 | 2,038,932 | 24.3% |
| S5 (10,000,000) | 264 | 1,263.8 | 1,113 | 20 | 1,093 | 1.80% | 1,093 | 0 | 1,093 | 2,107,752 | 25.1% |

Where the block accesses came from, and what the cache saved (same GET sequences replayed with the cache and with capacity 0):

| scale | GET phase | GETs | no block access (memtable, or ruled out by Bloom/index) | all block accesses were cache hits | at least one physical read | physical block reads with the cache | without a cache (capacity 0) | avoided |
|---|---|---|---|---|---|---|---|---|
| S2 (100,000) | hit keys | 5,000 | 1,191 | 1,230 | 2,579 | 2,579 | 3,809 | 1,230 |
| S2 (100,000) | miss keys | 200 | 198 | 1 | 1 | 1 | 2 | 1 |
| S3 (1,000,000) | hit keys | 2,000 | 41 | 30 | 1,929 | 1,929 | 1,959 | 30 |
| S3 (1,000,000) | miss keys | 100 | 79 | 5 | 16 | 16 | 23 | 7 |
| S4 (5,000,000) | hit keys | 1,000 | 2 | 4 | 994 | 994 | 998 | 4 |
| S4 (5,000,000) | miss keys | 50 | 14 | 2 | 34 | 49 | 60 | 11 |
| S5 (10,000,000) | hit keys | 1,000 | 3 | 1 | 996 | 996 | 997 | 1 |
| S5 (10,000,000) | miss keys | 50 | 5 | 4 | 41 | 97 | 116 | 19 |

Latency by cache outcome (pooled over runs; the cache-hit and cache-miss classes aren't randomized subsets of the workload, so their difference describes those GETs, not a controlled comparison):

| scale | GET phase | class (pooled over runs) | GETs | p50 / p95 / p99 (µs) | mean (µs) |
|---|---|---|---|---|---|
| S2 (100,000) | hit keys | all | 25,000 | 43.7 / 78.5 / 139.1 | 38.0 |
| S2 (100,000) | hit keys | cache hit | 6,150 | 20.0 / 32.8 / 46.3 | 20.8 |
| S2 (100,000) | hit keys | cache miss (physical read) | 12,895 | 58.3 / 96.7 / 175.8 | 63.3 |
| S2 (100,000) | hit keys | no block access | 5,955 | 1.0 / 1.5 / 2.2 | 1.1 |
| S2 (100,000) | miss keys | all | 1,000 | 3.3 / 4.4 / 22.0 | 3.8 |
| S2 (100,000) | miss keys | cache hit | 5 | 26.5 / 30.2 / 30.2 | 25.6 |
| S2 (100,000) | miss keys | cache miss (physical read) | 5 | 67.1 / 119.2 / 119.2 | 75.0 |
| S2 (100,000) | miss keys | no block access | 990 | 3.3 / 4.3 / 5.1 | 3.4 |
| S3 (1,000,000) | hit keys | all | 10,000 | 73.9 / 111.0 / 189.1 | 76.7 |
| S3 (1,000,000) | hit keys | cache hit | 150 | 39.5 / 59.2 / 93.3 | 40.8 |
| S3 (1,000,000) | hit keys | cache miss (physical read) | 9,645 | 74.6 / 112.0 / 190.9 | 78.9 |
| S3 (1,000,000) | hit keys | no block access | 205 | 1.2 / 1.6 / 2.3 | 1.2 |
| S3 (1,000,000) | miss keys | all | 500 | 32.0 / 93.5 / 124.1 | 43.4 |
| S3 (1,000,000) | miss keys | cache hit | 25 | 49.6 / 58.0 / 58.5 | 50.6 |
| S3 (1,000,000) | miss keys | cache miss (physical read) | 80 | 87.6 / 124.1 / 342.4 | 94.1 |
| S3 (1,000,000) | miss keys | no block access | 395 | 31.4 / 35.8 / 64.0 | 32.6 |
| S4 (5,000,000) | hit keys | all | 3,000 | 157.9 / 277.0 / 436.3 | 164.3 |
| S4 (5,000,000) | hit keys | cache hit | 12 | 124.2 / 162.3 / 162.3 | 117.1 |
| S4 (5,000,000) | hit keys | cache miss (physical read) | 2,982 | 158.5 / 277.0 / 436.3 | 164.8 |
| S4 (5,000,000) | hit keys | no block access | 6 | 1.2 / 2.5 / 2.5 | 1.5 |
| S4 (5,000,000) | miss keys | all | 150 | 251.5 / 383.4 / 481.8 | 257.7 |
| S4 (5,000,000) | miss keys | cache hit | 6 | 205.2 / 295.8 / 295.8 | 219.0 |
| S4 (5,000,000) | miss keys | cache miss (physical read) | 102 | 279.8 / 400.4 / 481.8 | 289.4 |
| S4 (5,000,000) | miss keys | no block access | 42 | 176.3 / 219.2 / 393.0 | 186.3 |
| S5 (10,000,000) | hit keys | all | 3,000 | 265.5 / 489.1 / 681.3 | 272.4 |
| S5 (10,000,000) | hit keys | cache hit | 3 | 208.9 / 219.9 / 219.9 | 198.2 |
| S5 (10,000,000) | hit keys | cache miss (physical read) | 2,988 | 266.4 / 489.1 / 681.3 | 273.3 |
| S5 (10,000,000) | hit keys | no block access | 9 | 1.5 / 2.1 / 2.1 | 1.6 |
| S5 (10,000,000) | miss keys | all | 150 | 507.5 / 869.9 / 1,003 | 551.4 |
| S5 (10,000,000) | miss keys | cache hit | 12 | 425.4 / 503.2 / 503.2 | 423.4 |
| S5 (10,000,000) | miss keys | cache miss (physical read) | 123 | 535.1 / 872.8 / 1,003 | 584.9 |
| S5 (10,000,000) | miss keys | no block access | 15 | 378.2 / 419.8 / 419.8 | 379.2 |

Work per workload, replayed (application-level counters; decode work is identical with and without the cache):

| scale | tables consulted | Bloom "no" | Bloom "maybe" | false positives | index searches | blocks scanned (cache lookups) | physical block reads: cached | uncached | bytes read: cached | uncached | records decoded: cached / uncached |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S2 (100,000) | 6,116 | 2,293 | 3,823 | 14 | 3,823 | 3,811 | 2,580 | 3,811 | 5,117,480 | 7,558,544 | 32,217 / 32,217 |
| S3 (1,000,000) | 28,883 | 26,695 | 2,188 | 229 | 2,188 | 1,982 | 1,945 | 1,982 | 3,848,960 | 3,918,028 | 17,009 / 17,009 |
| S4 (5,000,000) | 73,941 | 72,363 | 1,578 | 580 | 1,578 | 1,058 | 1,043 | 1,058 | 2,038,932 | 2,061,872 | 9,106 / 9,106 |
| S5 (10,000,000) | 148,443 | 146,260 | 2,183 | 1,186 | 2,183 | 1,113 | 1,093 | 1,113 | 2,107,752 | 2,135,652 | 9,859 / 9,859 |

V4-style sampled GETs against V4's recorded sample for the same keys. `tables_opened` and `fp_without_file_access` are defined by file opens in V4's counters, so cache hits change them; every other field must be identical:

| scale | sampled GETs (all runs) | GETs identical to V4's apart from the two file-open fields | tables opened, V5 with a cold cache | tables opened, V4 | false positives with no file opened: V5 / V4 |
|---|---|---|---|---|---|
| S1 (10,000) | 1,250 | 1,250 | 0 | 0 | 0 / 0 |
| S2 (100,000) | 1,250 | 1,250 | 700 | 720 | 5 / 5 |
| S3 (1,000,000) | 1,250 | 1,250 | 1,045 | 1,050 | 120 / 115 |
| S4 (5,000,000) | 750 | 750 | 741 | 777 | 348 / 315 |
| S5 (10,000,000) | 750 | 750 | 894 | 948 | 708 / 654 |

### 2. Writes and flush

The cache isn't on the write path. PUT latency over the whole load (all writes including flush-triggering ones):

| scale | V5 p50 / p95 / p99 (µs) | V4 p50 / p95 / p99 (µs) | V5 ops/s | V4 ops/s | V5 / V4 ops/s | V5 slowest write (ms) | V4 slowest write (ms) |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 4.4 / 5.8 / 10.7 | 4.7 / 5.8 / 13.3 | 195,850 | 189,834 | 1.032 | 1.2 | 1.2 |
| S2 (100,000) | 4.9 / 6.3 / 10.3 | 5.0 / 6.5 / 13.0 | 116,803 | 116,529 | 1.002 | 150.3 | 154.5 |
| S3 (1,000,000) | 4.7 / 6.1 / 9.2 | 4.8 / 6.3 / 12.5 | 111,019 | 108,143 | 1.027 | 162.7 | 181.4 |
| S4 (5,000,000) | 4.8 / 6.1 / 10.1 | 4.8 / 6.3 / 11.6 | 108,443 | 107,186 | 1.012 | 209.4 | 212.8 |
| S5 (10,000,000) | 4.7 / 6.0 / 9.8 | 4.8 / 6.2 / 11.4 | 110,377 | 107,845 | 1.023 | 214.5 | 219.4 |

Flush-triggering writes:

| scale | flush-triggering writes (pooled, V5) | V5 trigger p50 / p95 / p99 / max (ms) | V4 trigger p50 / p95 / p99 / max (ms) | median flush (ms): V5 / V4 | median inside `write_sstable` (ms): V5 / V4 | amortized flush (µs per PUT): V5 / V4 |
|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | — | — | — | — | — |
| S2 (100,000) | 10 | 148.4 / 180.2 / 180.2 / 180.2 | 153.8 / 185.4 / 185.4 / 185.4 | 147.6 / 153.4 | 129.6 / 133.3 | 2.97 / 3.06 |
| S3 (1,000,000) | 130 | 148.0 / 160.3 / 166.5 / 174.5 | 149.6 / 167.5 / 187.4 / 195.0 | 147.8 / 149.4 | 130.4 / 130.7 | 3.85 / 3.91 |
| S4 (5,000,000) | 396 | 150.5 / 163.1 / 199.5 / 232.8 | 149.6 / 164.0 / 185.6 / 244.0 | 150.4 / 149.5 | 132.1 / 131.7 | 3.97 / 3.98 |
| S5 (10,000,000) | 792 | 147.1 / 160.7 / 182.6 / 220.6 | 149.3 / 164.0 / 200.8 / 222.2 | 147.1 / 149.2 | 128.6 / 130.8 | 3.91 / 3.95 |

Overwrite phase (1,000 PUTs of existing keys):

| scale | flush-triggering ops (all runs) | V5 p50 / p95 / p99 (µs) | V4 p50 / p95 / p99 (µs) | V5 / V4 p50 |
|---|---|---|---|---|
| S1 (10,000) | 0 | 5.2 / 7.7 / 19.0 | 5.2 / 6.5 / 20.1 | 1.00 |
| S2 (100,000) | 0 | 4.8 / 6.7 / 17.3 | 5.0 / 6.2 / 18.7 | 0.96 |
| S3 (1,000,000) | 0 | 4.7 / 6.7 / 19.5 | 4.7 / 6.0 / 13.5 | 1.00 |
| S4 (5,000,000) | 0 | 4.8 / 5.9 / 15.0 | 5.1 / 5.9 / 18.8 | 0.94 |
| S5 (10,000,000) | 0 | 4.9 / 5.9 / 15.1 | 4.9 / 6.1 / 18.8 | 1.00 |

Delete phase (1,000 DELETEs):

| scale | flush-triggering ops (all runs) | V5 p50 / p95 / p99 (µs) | V4 p50 / p95 / p99 (µs) | V5 / V4 p50 |
|---|---|---|---|---|
| S1 (10,000) | 0 | 4.8 / 6.0 / 7.7 | 5.0 / 5.7 / 8.2 | 0.96 |
| S2 (100,000) | 0 | 4.7 / 7.6 / 11.2 | 5.1 / 6.4 / 8.9 | 0.92 |
| S3 (1,000,000) | 0 | 4.5 / 5.1 / 7.0 | 4.5 / 6.4 / 7.8 | 1.00 |
| S4 (5,000,000) | 0 | 4.5 / 5.6 / 7.3 | 4.8 / 5.5 / 8.4 | 0.94 |
| S5 (10,000,000) | 0 | 4.6 / 5.1 / 8.0 | 4.7 / 5.2 / 8.5 | 0.98 |

### 3. Recovery

| scale | SSTables opened | V5 open per run (ms) | V5 median (ms) | V4 median (ms) | V5 / V4 | between-run spread: V5 / V4 | reopen after mutations (ms, median): V5 / V4 |
|---|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 20, 20, 20, 21, 23 | 20.1 | 18.3 | 1.10 | 16% / 21% | 22.3 / 19.1 |
| S2 (100,000) | 2 | 38, 34, 41, 38, 38 | 38.2 | 37.2 | 1.03 | 20% / 9% | 39.1 / 41.5 |
| S3 (1,000,000) | 26 | 52, 51, 48, 57, 54 | 52.0 | 53.8 | 0.97 | 18% / 8% | 55.4 / 55.4 |
| S4 (5,000,000) | 132 | 162, 156, 156 | 156.1 | 172.5 | 0.90 | 4% / 12% | 163.8 / 173.7 |
| S5 (10,000,000) | 264 | 355, 344, 336 | 343.8 | 358.6 | 0.96 | 6% / 12% | 336.6 / 336.9 |

### 4. Memory (observed process private bytes)

Open and load as for V4 (the cache is empty after open):

| scale | V5 private delta after load (MB) | V4 after load (MB) | V5 after open (MB) | V4 after open (MB) | heap retained by opening the tables (MB): V5 / V4 |
|---|---|---|---|---|---|
| S1 (10,000) | 1.6 (1.4–1.8) | 1.6 (1.6–1.9) | 2.0 (1.8–2.4) | 2.3 (1.8–2.6) | 0.00 / 0.00 |
| S2 (100,000) | 7.7 (7.5–7.8) | 7.4 (6.9–7.5) | 5.6 (4.4–5.7) | 5.8 (5.8–5.9) | 0.52 / 0.52 |
| S3 (1,000,000) | 18.2 (17.9–18.5) | 17.9 (17.3–17.9) | 11.6 (10.3–12.1) | 10.5 (9.7–11.6) | 6.73 / 6.73 |
| S4 (5,000,000) | 47.8 (46.9–47.8) | 48.1 (47.6–48.5) | 40.0 (38.3–40.1) | 39.7 (39.3–40.3) | 34.16 / 34.15 |
| S5 (10,000,000) | 86.7 (86.0–86.9) | 86.2 (86.0–87.0) | 81.1 (80.9–81.3) | 81.7 (81.5–82.9) | 68.32 / 68.29 |

Cost of the cache once the standard GET workload has filled it (fresh process; same workload with and without the cache):

| scale | cache entries after the GET workload | cache payload (MB) | process private growth during the GET workload, cache on (MB) | cache off (capacity 0) | difference (medians, MB) | difference ÷ payload |
|---|---|---|---|---|---|---|
| S1 (10,000) | 0 | 0 | 11.23 (10.73–11.98) | 10.30 (9.71–10.74) | — | — |
| S2 (100,000) | 2,580 | 5.12 | 7.20 (7.07–7.30) | 1.60 (1.43–1.88) | 5.60 | 1.09 |
| S3 (1,000,000) | 1,945 | 3.85 | 4.21 (4.21–4.29) | 0.00 (0.00–0.00) | 4.21 | 1.09 |
| S4 (5,000,000) | 1,043 | 2.04 | 3.24 (3.24–3.25) | 1.05 (1.05–1.05) | 2.20 | 1.08 |
| S5 (10,000,000) | 1,093 | 2.11 | 1.17 (1.14–1.18) | 0.00 (0.00–0.00) | 1.17 | 0.56 |

### 5. 10,000-key head-to-head

**V0 baseline workload, default 4 MiB limit (no flush occurs; the cache is never consulted)** (medians over 5 runs; V5 / V4 / V5 ÷ V4; µs).

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 0.5 / 0.5 / 1.00 | 0.8 / 0.7 / 1.14 | 2.7 / 3.2 / 0.84 |
| get_miss | 0.4 / 0.4 / 1.00 | 0.6 / 0.6 / 1.00 | 1.9 / 1.9 / 1.00 |
| mixed_all | 0.6 / 0.6 / 1.00 | 5.5 / 6.4 / 0.86 | 9.4 / 9.7 / 0.97 |
| mixed_get | 0.6 / 0.5 / 1.20 | 0.8 / 0.8 / 1.00 | 0.8 / 1.4 / 0.57 |
| mixed_put | 5.1 / 4.9 / 1.04 | 7.1 / 7.3 / 0.97 | 11.8 / 12.7 / 0.93 |
| mixed_delete | 5.1 / 5.1 / 1.00 | 13.8 / 18.6 / 0.74 | 13.8 / 18.6 / 0.74 |
| put_load | 4.5 / 4.3 / 1.05 | 5.3 / 5.4 / 0.98 | 8.0 / 13.5 / 0.59 |
| put_sync | 1,916.1 / 1,903.4 / 1.01 | 2,175.5 / 2,183.1 / 1.00 | 3,903.5 / 3,964.2 / 0.98 |

**V1 extended workload, default limit (no flush occurs; the cache is never consulted)** (medians over 5 runs; V5 / V4 / V5 ÷ V4; µs).

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 0.4 / 0.4 / 1.00 | 0.6 / 0.5 / 1.20 | 0.7 / 0.7 / 1.00 |
| get_miss | 0.4 / 0.4 / 1.00 | 0.6 / 0.5 / 1.20 | 0.7 / 0.6 / 1.17 |
| mixed_all | 0.6 / 0.5 / 1.20 | 5.7 / 5.1 / 1.12 | 6.8 / 7.3 / 0.93 |
| mixed_get | 0.5 / 0.5 / 1.00 | 0.7 / 0.7 / 1.00 | 0.9 / 0.9 / 1.00 |
| mixed_put | 5.0 / 4.7 / 1.06 | 6.4 / 6.2 / 1.03 | 12.4 / 16.7 / 0.74 |
| mixed_delete | 5.0 / 4.6 / 1.09 | 6.2 / 6.0 / 1.03 | 9.3 / 12.8 / 0.73 |
| put_load | 4.5 / 4.3 / 1.05 | 5.4 / 5.3 / 1.02 | 9.7 / 13.3 / 0.73 |
| put_sync | 1,910.0 / 1,888.0 / 1.01 | 2,144.4 / 2,172.6 / 0.99 | 3,741.3 / 3,912.1 / 0.96 |

**V0 baseline workload, 16 KiB limit (10,000 keys spread over many tables)** (medians over 5 runs; V5 / V4 / V5 ÷ V4; µs). Cache of the main store per run: 72 hits, 294 misses, 0 evictions (hit rate 19.7%; identical in all runs: True); 67 tables.

| phase | p50 | p95 | p99 |
|---|---|---|---|
| get_hit | 92.6 / 93.1 / 0.99 | 140.1 / 156.3 / 0.90 | 215.7 / 219.7 / 0.98 |
| get_miss | 85.3 / 102.4 / 0.83 | 201.3 / 219.0 / 0.92 | 312.0 / 314.6 / 0.99 |
| mixed_all | 75.9 / 81.4 / 0.93 | 159.6 / 146.8 / 1.09 | 294.8 / 295.9 / 1.00 |
| mixed_get | 97.8 / 99.6 / 0.98 | 163.4 / 150.8 / 1.08 | 246.2 / 198.8 / 1.24 |
| mixed_put | 7.6 / 7.1 / 1.07 | 33.6 / 33.0 / 1.02 | 28,007.9 / 27,260.7 / 1.03 |
| mixed_delete | 8.9 / 7.2 / 1.24 | 17.2 / 20.4 / 0.84 | 17.2 / 20.4 / 0.84 |
| put_load | 4.5 / 4.6 / 0.98 | 10.3 / 10.8 / 0.95 | 80.5 / 84.9 / 0.95 |
| put_sync | 1,903.1 / 1,884.9 / 1.01 | 2,372.0 / 2,236.3 / 1.06 | 4,221.5 / 4,063.7 / 1.04 |

### 6. Controlled locality workload

Design (also in Methodology):

|  |  |
|---|---|
| database | 1,000,000 keys; 26 flushed tables of 37,787 records (61,412 blocks of up to 16 records), the rest in the memtable |
| GETs per configuration per run | 200,000 |
| hot set | 1,000 distinct flushed blocks = 1,984,000 bytes; 180,000 GETs (90%) go to it, to a uniformly random key of a uniformly random hot block |
| cold GETs | 20,000 (10%), each to a different block (20,000 distinct blocks, none in the hot set) |
| first accesses of hot blocks | 1,000 (one per hot block; included in the measurement); repeat hot accesses: 179,000 |
| GETs answered by the memtable | 0 (every key is below the first memtable key) |

Capacity comparison. Latencies are pooled over runs; counters are per configuration per run (identical in every run):

| configuration | cache capacity | capacity ÷ hot-set bytes | cache hits | cache misses | hit rate | physical block reads | bytes read | evictions | cache bytes / entries at the end | all GETs p50 / p95 / p99 (µs, pooled over runs) | p50 ÷ V4 | p50 ÷ V5 (0 B) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| v4 | none (V4) | — | — | — | — | 200,000 | 396,795,040 | — | — | 73.1 / 114.4 / 219.5 | 1.00 | 0.99 |
| v5_0 | 0 (disabled) | — | 0 | 200,000 | 0.0% | 200,000 | 396,795,040 | 0 | 0 / 0 | 73.5 / 112.3 / 210.8 | 1.01 | 1.00 |
| v5_256KiB | 256 KiB | 0.13 | 21,028 | 178,972 | 10.5% | 178,972 | 355,075,488 | 178,840 | 261,888 / 132 | 72.7 / 113.1 / 217.1 | 0.99 | 0.99 |
| v5_1MiB | 1 MiB | 0.53 | 82,616 | 117,384 | 41.3% | 117,384 | 232,884,896 | 116,856 | 1,047,552 / 528 | 62.8 / 110.9 / 197.1 | 0.86 | 0.85 |
| v5_2MiB | 2 MiB | 1.06 | 151,884 | 48,116 | 75.9% | 48,116 | 95,457,184 | 47,059 | 2,097,088 / 1,057 | 40.2 / 102.7 / 168.7 | 0.55 | 0.55 |
| v5_4MiB | 4 MiB | 2.11 | 178,990 | 21,010 | 89.5% | 21,010 | 41,678,880 | 18,896 | 4,193,556 / 2,114 | 36.1 / 92.8 / 150.6 | 0.49 | 0.49 |
| v5_8MiB | 8 MiB | 4.23 | 179,000 | 21,000 | 89.5% | 21,000 | 41,659,040 | 16,772 | 8,387,112 / 4,228 | 36.2 / 92.0 / 146.6 | 0.50 | 0.49 |

Latency by GET class (hot first access, hot repeat, cold):

| configuration | GET class | GETs (pooled over runs) | p50 / p95 / p99 (µs) |
|---|---|---|---|
| v4 | hot, first access | 5,000 | 74.3 / 114.7 / 206.1 |
| v4 | hot, repeat access | 895,000 | 73.0 / 114.2 / 219.2 |
| v4 | cold | 100,000 | 74.5 / 116.4 / 224.9 |
| v5_0 | hot, first access | 5,000 | 74.3 / 117.1 / 236.2 |
| v5_0 | hot, repeat access | 895,000 | 73.4 / 112.2 / 210.0 |
| v5_0 | cold | 100,000 | 75.0 / 113.5 / 216.0 |
| v5_256KiB | hot, first access | 5,000 | 76.4 / 120.9 / 237.6 |
| v5_256KiB | hot, repeat access | 895,000 | 72.2 / 112.6 / 215.8 |
| v5_256KiB | cold | 100,000 | 76.0 / 117.3 / 227.6 |
| v5_1MiB | hot, first access | 5,000 | 78.5 / 120.7 / 207.2 |
| v5_1MiB | hot, repeat access | 895,000 | 59.4 / 109.1 / 192.3 |
| v5_1MiB | cold | 100,000 | 78.3 / 125.8 / 234.4 |
| v5_2MiB | hot, first access | 5,000 | 76.6 / 128.7 / 237.6 |
| v5_2MiB | hot, repeat access | 895,000 | 37.6 / 94.2 / 151.8 |
| v5_2MiB | cold | 100,000 | 82.3 / 146.6 / 246.7 |
| v5_4MiB | hot, first access | 5,000 | 76.2 / 125.1 / 239.8 |
| v5_4MiB | hot, repeat access | 895,000 | 34.0 / 58.9 / 94.1 |
| v5_4MiB | cold | 100,000 | 87.2 / 159.9 / 257.7 |
| v5_8MiB | hot, first access | 5,000 | 76.3 / 124.8 / 233.9 |
| v5_8MiB | hot, repeat access | 895,000 | 34.1 / 58.9 / 89.5 |
| v5_8MiB | cold | 100,000 | 86.8 / 157.1 / 252.2 |

Latency by cache outcome:

| configuration | outcome | GETs (pooled over runs) | p50 / p95 / p99 (µs) | mean (µs) |
|---|---|---|---|---|
| v5_0 | cache miss (physical read) | 1,000,000 | 73.5 / 112.3 / 210.8 | 78.5 |
| v5_0 | hot repeat, cache miss | 895,000 | 73.4 / 112.2 / 210.0 | 78.4 |
| v5_256KiB | cache hit | 105,140 | 34.7 / 59.0 / 94.5 | 36.7 |
| v5_256KiB | cache miss (physical read) | 894,860 | 75.0 / 115.9 / 225.3 | 80.6 |
| v5_256KiB | hot repeat, cache hit | 105,140 | 34.7 / 59.0 / 94.5 | 36.7 |
| v5_256KiB | hot repeat, cache miss | 789,860 | 74.8 / 115.7 / 224.8 | 80.4 |
| v5_1MiB | cache hit | 413,080 | 34.7 / 59.6 / 92.3 | 36.7 |
| v5_1MiB | cache miss (physical read) | 586,920 | 77.4 / 125.1 / 232.4 | 83.5 |
| v5_1MiB | hot repeat, cache hit | 413,080 | 34.7 / 59.6 / 92.3 | 36.7 |
| v5_1MiB | hot repeat, cache miss | 481,920 | 77.2 / 125.0 / 232.2 | 83.3 |
| v5_2MiB | cache hit | 759,420 | 34.3 / 59.6 / 98.1 | 36.5 |
| v5_2MiB | cache miss (physical read) | 240,580 | 81.7 / 144.9 / 246.4 | 89.2 |
| v5_2MiB | hot repeat, cache hit | 759,420 | 34.3 / 59.6 / 98.1 | 36.5 |
| v5_2MiB | hot repeat, cache miss | 135,580 | 81.5 / 144.1 / 246.5 | 88.9 |
| v5_4MiB | cache hit | 894,950 | 34.0 / 58.9 / 94.0 | 36.1 |
| v5_4MiB | cache miss (physical read) | 105,050 | 86.6 / 159.0 / 256.9 | 95.2 |
| v5_4MiB | hot repeat, cache hit | 894,950 | 34.0 / 58.9 / 94.0 | 36.1 |
| v5_4MiB | hot repeat, cache miss | 50 | 79.2 / 126.0 / 319.8 | 84.6 |
| v5_8MiB | cache hit | 895,000 | 34.1 / 58.9 / 89.5 | 36.0 |
| v5_8MiB | cache miss (physical read) | 105,000 | 86.3 / 156.2 / 250.8 | 94.8 |
| v5_8MiB | hot repeat, cache hit | 895,000 | 34.1 / 58.9 / 89.5 | 36.0 |

Work (per configuration per run; records decoded are equal across configurations because a hit still decodes):

| configuration | tables consulted | Bloom "no" | Bloom "maybe" | index searches | blocks scanned | file opens (`_open_table`) | physical block reads | records decoded | bytes decoded | cache-hit GETs that read a block |
|---|---|---|---|---|---|---|---|---|---|---|
| v4 | 2,630,257 | 2,410,198 | 220,059 | 220,059 | 200,000 | 200,000 | 200,000 | 1,700,582 | 210,872,168 | 0 |
| v5_0 | 2,630,257 | 2,410,198 | 220,059 | 220,059 | 200,000 | 200,000 | 200,000 | 1,700,582 | 210,872,168 | 0 |
| v5_256KiB | 2,630,257 | 2,410,198 | 220,059 | 220,059 | 200,000 | 178,972 | 178,972 | 1,700,582 | 210,872,168 | 0 |
| v5_1MiB | 2,630,257 | 2,410,198 | 220,059 | 220,059 | 200,000 | 117,384 | 117,384 | 1,700,582 | 210,872,168 | 0 |
| v5_2MiB | 2,630,257 | 2,410,198 | 220,059 | 220,059 | 200,000 | 48,116 | 48,116 | 1,700,582 | 210,872,168 | 0 |
| v5_4MiB | 2,630,257 | 2,410,198 | 220,059 | 220,059 | 200,000 | 21,010 | 21,010 | 1,700,582 | 210,872,168 | 0 |
| v5_8MiB | 2,630,257 | 2,410,198 | 220,059 | 220,059 | 200,000 | 21,000 | 21,000 | 1,700,582 | 210,872,168 | 0 |

Between-run variation of the all-GET p50:

| configuration | p50 of each run (µs, in run order) | between-run spread (max−min over median) |
|---|---|---|
| v4 | 70.8, 74.1, 71.8, 74.8, 74.3 | 5.4% |
| v5_0 | 74.5, 71.7, 73.3, 74.8, 73.5 | 4.2% |
| v5_256KiB | 72.8, 71.6, 72.3, 73.0, 73.6 | 2.7% |
| v5_1MiB | 62.3, 62.9, 63.5, 62.6, 62.7 | 1.9% |
| v5_2MiB | 38.7, 40.1, 40.0, 41.6, 40.9 | 7.2% |
| v5_4MiB | 35.8, 36.4, 35.8, 36.8, 36.0 | 2.8% |
| v5_8MiB | 35.1, 36.8, 36.0, 36.4, 36.5 | 4.7% |

Python heap retained by the cache after the whole sequence (run 1, `tracemalloc`):

| configuration | entries | cache payload bytes | Python heap retained (tracemalloc) | difference | per entry (B) | retained ÷ payload |
|---|---|---|---|---|---|---|
| v5_256KiB | 132 | 261,888 | 294,592 | 32,704 | 248 | 1.125 |
| v5_1MiB | 528 | 1,047,552 | 1,177,600 | 130,048 | 246 | 1.124 |
| v5_2MiB | 1,057 | 2,097,088 | 2,357,909 | 260,821 | 247 | 1.124 |
| v5_4MiB | 2,114 | 4,193,556 | 4,714,818 | 521,262 | 247 | 1.124 |
| v5_8MiB | 4,228 | 8,387,112 | 9,429,820 | 1,042,708 | 247 | 1.124 |

### 7. Structural cache behavior

| scale | runs | cache counters identical in every run | physical block reads = cache misses = file opens | all-hit GETs that read a block or opened a file | per-GET invariant violations | derived by an independent LRU model (structural.py) |
|---|---|---|---|---|---|---|
| S1 (10,000) | 5 | True | n/a (no block access) | 0 | 0 | yes |
| S2 (100,000) | 5 | True | True | 0 | 0 | yes |
| S3 (1,000,000) | 5 | True | True | 0 | 0 | yes |
| S4 (5,000,000) | 3 | True | True | 0 | 0 | yes |
| S5 (10,000,000) | 3 | True | True | 0 | 0 | yes |

## Analysis

**1. "The cache works" is established structurally, and holds in both workloads.** In every replay of every run, physical block reads equal cache misses equal file opens (`block_reads_equal_cache_misses` is true at every scale that touches a table). 1,277 standard-workload GETs per run were served entirely from the cache and none of them read a block or opened a file, and in the locality workload 0 cache-hit GETs read a block, across 7 configurations and 5 runs. Records decoded, Bloom answers, index searches and tables consulted are identical with and without the cache (the cache saves the open and read, not the decode), and every returned value equals the uncached result. The derived model in `structural.py` (a separate LRU implementation fed by the table geometry and rebuilt Bloom filters) reproduces every measured hit, miss, insertion, eviction, entry count, byte count, physical read and decode count exactly.

**2. "The cache improves latency" is measured in the locality workload, where it is large.** With 179,000 of 200,000 GETs revisiting 1,000 hot blocks, all-GET p50 was 73.1 µs on V4, 73.5 µs on V5 with the cache disabled, and 72.7 µs at 256 KiB, 62.8 µs at 1,024 KiB, 40.2 µs at 2,048 KiB, 36.1 µs at 4,096 KiB and 36.2 µs at 8,192 KiB (hit rates 10.5%, 41.3%, 75.9%, 89.5%, 89.5%). p95 and p99 moved less than p50 (114.4 → 92.0 µs and 219.5 → 146.6 µs, V4 → 8 MiB). The 10.5% of GETs that are cold or first accesses are misses in every cached configuration, which puts the p95 and p99 inside the miss class. Within one cached configuration a cache-hit GET had p50 34.1 µs against 86.3 µs for a cache miss (8 MiB). The two classes decode the same records, and the structural difference between them is that a hit opens no file and reads no block.

**Cost of the cache code when it cannot help.** With the cache disabled (capacity 0: every lookup is a miss, nothing is inserted), V5's locality p50 was 73.5 µs against V4's 73.1 µs (ratio 1.005; mean ratio 0.999). The between-run p50 spread was 4.2% (V5, 0 B) and 5.4% (V4), so this benchmark doesn't resolve the difference. A cache that is enabled but rarely hit does insert and evict on every miss. In the standard workload at 5M and 10M records (1.4% and 1.8% hit rate) the pooled GET p50 changes were -2.5% and -6.0% (hits) and -6.7% and -8.7% (misses), i.e. no slowdown was measured.

**3. "The standard benchmark benefits" depends on locality, and only the 100,000-record scale has any.** Scale by scale against V4: S1 has no SSTables, so no GET reaches the cache (0 lookups). S2 has 2 tables, 3,811 block accesses, hit rate 32.30%, GET-hit p50 -16.9% and GET-miss p50 -2.9%. S3 has 26 tables, 1,982 block accesses, hit rate 1.87%, GET-hit p50 -1.6% and GET-miss p50 -7.8%. S4 has 132 tables, 1,058 block accesses, hit rate 1.42%, GET-hit p50 -2.5% and GET-miss p50 -6.7%. S5 has 264 tables, 1,113 block accesses, hit rate 1.80%, GET-hit p50 -6.0% and GET-miss p50 -8.7%. The standard GET keys are uniform draws over the keyspace, so a block is revisited only by chance. From 1,000,000 records up the hit rate is 1.42%–1.87%, and almost every GET performs the physical block read V4 performs. At 100,000 records the 2 tables hold 4,724 blocks and the 3,811 block accesses of a run fall on only 2,580 distinct blocks, so blocks are revisited by chance and nothing is evicted. There, GETs served from the cache had p50 20.0 µs against 58.3 µs for GETs that read a block, and the pooled GET-hit p50 changed -16.9% (between-run p50 spread 4.1% V5, 13.7% V4). Each pooled p50 change is judged against the larger of the V5 and V4 between-run p50 spreads at the same scale. Changes within that spread: S2 miss (-2.9% against spreads 21.2% V5 / 31.2% V4), S3 hit (-1.6% against 11.3% V5 / 33.9% V4), S3 miss (-7.8% against 2.8% V5 / 21.3% V4), S4 hit (-2.5% against 3.6% V5 / 7.0% V4), S4 miss (-6.7% against 26.0% V5 / 34.6% V4), S5 hit (-6.0% against 8.1% V5 / 7.2% V4) and S5 miss (-8.7% against 11.4% V5 / 4.6% V4). Change beyond it: S2 hit (-16.9% against spreads 4.1% V5 / 13.7% V4). A change within the spread isn't evidence of a cache effect, and none is claimed. The scales with a hit rate under 2% have nothing in the cache data that would predict a change. The V4 reference numbers come from an earlier invocation, and machine state differs between invocations, which the between-run spreads bound only partly. At 10,000 records nothing is flushed (no SSTables), and GETs are memtable operations with p50 0.5 µs (V5) and 0.4 µs (V4) for hits, within a few ticks of the 0.1 µs clock.

**4. Capacity, hit rate, physical reads and latency move together in this workload.** 256 KiB (0.13× the hot set): hit rate 10.5%, 178,972 physical reads, 178,840 evictions, p50 72.7 µs. 1,024 KiB (0.53× the hot set): hit rate 41.3%, 117,384 physical reads, 116,856 evictions, p50 62.8 µs. 2,048 KiB (1.06× the hot set): hit rate 75.9%, 48,116 physical reads, 47,059 evictions, p50 40.2 µs. 4,096 KiB (2.11× the hot set): hit rate 89.5%, 21,010 physical reads, 18,896 evictions, p50 36.1 µs. 8,192 KiB (4.23× the hot set): hit rate 89.5%, 21,000 physical reads, 16,772 evictions, p50 36.2 µs. The ceiling for this workload is 89.5% (179,000 repeat accesses of 200,000): the cold blocks are never revisited, so no capacity can hit them. The 4 MiB and 8 MiB configurations are at that ceiling (178,990 and 179,000 hits), and their latencies are the same within their between-run spreads (2.8%, 4.7%). A capacity just above the hot set (2 MiB, 1.06×) didn't reach the ceiling, because the 20,000 cold blocks are inserted too and push hot blocks out ahead of their next use under LRU (47,059 evictions). This establishes the shape for this one workload (uniform reuse of a fixed hot set polluted by single-use cold blocks). It doesn't establish an optimal cache size in general, and no optimum is claimed. In particular, 8 MiB gained nothing over 4 MiB here because the workload has nothing more to hit.

**5. Structural invariants.** Block reads = cache misses = file opens in every run, and a GET whose block accesses were all hits opened no file. Hot and cold keys are all in flushed tables (`all_keys_flushed`: True; none in the memtable), every GET scans exactly one block, and the cache state is rebuilt empty for each configuration of each run, so the first access of every block is a miss (1,000 hot first accesses and 20,000 cold accesses are compulsory misses in every cached configuration).

**6. Write and flush path.** The cache isn't on the write path (a flush writes the same table V4 writes). PUT throughput during the load was 1.002–1.032× V4's across scales, and the median flush took 147.1–150.4 ms on V5 and 149.2–153.4 ms on V4. The overwrite and delete phases are in the tables above. No write-path effect of the cache is claimed.

**7. Recovery.** The cache starts empty on every open and nothing is loaded into it. Median open time V5 ÷ V4 is 1.10, 1.03, 0.97, 0.90 and 0.96 for the five scales, against between-run spreads of 15.7%, 19.7%, 17.6%, 4.0%, 5.6% (V5) and 20.7%, 9.3%, 8.5%, 12.3%, 11.6% (V4).

**8. Memory.** An open store with an empty cache costs what V4 costs (process private delta after open, V5 vs V4, in the table above). The cache's cost appears as it fills. For the standard GET workload, the difference in process private growth between a run with the 8 MiB cache and one with capacity 0 was 5.60 MB at S2 against 5.12 MB of cached block bytes, 4.21 MB at S3 against 3.85 MB, 2.20 MB at S4 against 2.04 MB, and 1.17 MB at S5 against 2.11 MB of cached block bytes. In the locality workload, Python heap retained by a full cache (tracemalloc) was 1.124–1.125× the cached payload across the five cached capacities. The capacity counts block bytes only, so the cache's memory is somewhat larger than its configured capacity.

## Limitations and unexplained findings

- **Warm OS page cache only.** Every figure is a warm-OS-cache number. A physical block read here is a read served from the OS cache, so the saving per avoided read on a cold disk isn't measured and isn't claimed.
- **Locality is by construction.** The locality workload is designed to revisit a small set of blocks. Its hit rates show what the cache does under that access pattern, not what a real workload would see. The standard workload's locality is whatever its uniform key draws give.
- **Standard-workload sample sizes at the large scales are small.** 5M and 10M have 3 runs and 1,000 hit and 50 miss GETs per run (3,000 hit and 150 miss samples pooled at 10M), so their p99 values rest on the slowest 30 hit and 2 miss samples and aren't stable estimates.
- **The V4 reference was measured in an earlier invocation.** This benchmark can't tell V4 → V5 differences smaller than the between-run spreads (tables above) from run-to-run variation. Timings on this machine are sensitive to concurrent load, and only mild background load (a video player and a desktop-widget program) was present.
- **The cache's byte capacity excludes per-entry Python overhead**, so actual memory is above the capacity (measured above for the locality workload).
- **No concurrency, scans, range reads or write-heavy mixed workloads with locality were tested.** A long scan would evict a working set under plain LRU, and this wasn't measured.
- **Unexplained, measured but not attributed.** (a) In the locality workload the latency of GETs that miss grows with cache capacity (cache-miss p50 75.0 µs at 256 KiB, 77.4 µs at 1,024 KiB, 81.7 µs at 2,048 KiB, 86.6 µs at 4,096 KiB and 86.3 µs at 8,192 KiB; cold-GET p50 76.0, 78.3, 82.3, 87.2, 86.8 µs in the same order, against 74.5 µs on V4), even though each such GET does the same physical read, decode and a cache insertion. The benchmark didn't measure why, and candidates such as eviction work or memory effects weren't tested. (b) At 5M and 10M records the pooled GET p50 is lower than V4's (S4 hit -2.5%, miss -6.7%; S5 hit -6.0%, miss -8.7%), with a hit rate under 2%. The analysis section compares each change with the between-run spreads, and a change inside them isn't attributed to the cache. No cache effect that would produce a change of this size at that hit rate was identified. (c) The cache-attributable process-memory growth divided by the cached payload was 1.09, 1.09, 1.08 and 0.56 at 100K, 1M, 5M and 10M records. The probe measures whole-process private bytes at megabyte scale, and the benchmark didn't isolate why the ratio differs between scales.

## Environment

**Environment:** Python 3.10.6; Windows-10-10.0.26300-SP0; Intel64 Family 6 Model 183 Stepping 1, GenuineIntel; 24 logical CPUs; 17.0 GB RAM; seed 42; key 11 B; value 100 B; `sync=False`; default `memtable_limit_bytes` = 4 MiB; index interval 16; Bloom 10 bits per key, 7 hashes; block cache 8 MiB (default).
