# Benchmarks

There are two benchmarks: the original **~1.24 MB baseline** (`bench.py`, below) and the **scaling characterization** (`bench_scaling.py`, further down; 10,000 to 10,000,000 records). They use different workloads, so don't compare their numbers directly.

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
