# Benchmarks

There are two benchmarks: the original **~1.24 MB baseline** (the V0 benchmark, below) and the **scaling characterization** (the V0 scaling benchmark, further down; 10,000 to 10,000,000 records). They use different workloads, so don't compare their numbers directly. The V1 benchmark is described in [V1 benchmark](#v1-benchmark).

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

Per run at N records (`bench.py`):

1. **`put_load`**: N PUTs of distinct keys. Afterwards the memtable is checked to hold N entries and the WAL to be exactly N × 124 bytes.
2. **`open_recovery`**: close the store, then time `KVStore(path)` (replay of the whole WAL). The rebuilt memtable is checked untimed against a fingerprint of the live one, and no truncation may occur.
3. **`get_hit` / `get_miss`**: 100,000 each, with every result checked after the timed call. The first V0-count hit keys equal V0's.
4. **Controls** on the same hit keys: an empty Python function call (`control_noop`) and a bare `dict.get` on the memtable (`control_dict_get`). They show how much of a V1 GET figure is timing overhead and wrapper cost.
5. **`put_overwrite`**: the same 1,000 overwrites as V0, last-write-wins verified.
6. **`delete`** (new): 1,000 timed DELETEs of keys drawn uniformly from the keyspace. Repeats are allowed, so there are fewer than 1,000 distinct tombstoned keys at small scales. Every deleted key must read back `None`. V0's results have no such phase, so the V0 reference is measured with the same keys: a fresh V0 file is loaded with N keys (untimed) and the same 1,000 deletes are timed, with the same run counts, in the same invocation as the V1 runs at that scale. V0's file holds only the load at that point, while V1's also holds the overwrites.
7. After the deletes, the WAL is closed and its size is checked (N × 124 + 1,000 × 124 + 1,000 × 24 bytes). The final WAL (with overwrites and tombstones) is then reopened, and its replay is timed and checked against the live state.

**Memory probes** (untimed, separate from the latency runs): per scale, 3 probes, each a pair of fresh Python processes. One loads N keys with `sync=False` and the other opens that WAL. Process *private bytes* (Windows `PrivateUsage`) are read before and after, and the tables report the delta. These are observations for this Python build and this key/value shape, not a universal memory cost.

**10,000-key head-to-head** (`--baseline`): V1 runs the exact V0 baseline workload (`build_workload` in `bench.py`: 10,000 keys, 100 + 100 GETs, 300 mixed ops with 70% GET / 25% PUT overwrite / 5% DELETE, 300 `sync=True` PUTs, 5 runs). Separately, it runs an extended V1-only workload with the same definitions but 100,000 + 100,000 GETs and 100,000 mixed ops (5 runs). Every GET result is checked against a reference dict.

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

Derived (not timed) by `benchmarks/structural.py` from the record format and the workload (N loaded keys, then 1,000 overwrites, then 1,000 deletes), and cross-checked against the measured WAL sizes, memtable entry counts, tombstone counts and replay record counts in the result file. Definitions: *physical records* = every WAL record; *stale records* = physical records − distinct keys (superseded records still in the WAL); *live keys* = distinct keys not tombstoned; *memtable entries* = distinct keys, tombstoned ones included; *space amplification* = WAL bytes / live user bytes (111 × live keys). This is append-only staleness, not LSM write amplification: V1 never rewrites data.

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
