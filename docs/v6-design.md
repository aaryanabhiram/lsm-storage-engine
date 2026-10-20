# V6 design: compaction

V2 to V5 only ever add SSTables, so the table set grows without bound and every GET that reaches the tables still touches every table's filter ([v4-design.md](v4-design.md)). V6 adds one operation, `compact()`, that merges every flushed SSTable into a single new one. Everything else is V5's: the WAL, memtable, synchronous flush, SSTable format, Bloom filters, sparse index and block cache ([v5-design.md](v5-design.md)). Compaction is **explicit, synchronous and full**: it runs only when called, in the calling thread, over all tables. There's no background work, concurrency, leveling, scheduling policy or manifest.

V6 adds `compact()` to `KVStore` in `store.py` (V5 plus compaction).

```python
from lsm_store import KVStore

with KVStore("db_dir") as db:
    ...
    stats = db.compact()        # this run's statistics
    db.compaction_stats()       # cumulative since the store was opened
    db.table_count()
```

## What compaction does

1. **Selection.** Every table in the catalogue (V2, V3, V4 and V5 tables alike), in age order. It does nothing if there are no tables, or if the only table is already a compaction output (which has no tombstones and no duplicates). The memtable and the WAL are never read or modified.
2. **Version selection.** `merge_newest` merges the tables' sorted streams with `heapq.merge`. Each stream's records are tagged `(key, rank, value)` with rank 0 for the newest table, so equal keys sort newest first and only the first record of each key is kept. Ordering comes from table age, never from values or dict order. Each input is read through `SSTable.items()`, which validates every checksum, key order, index entry and the Bloom filter, so damage in any input raises `CorruptionError` and aborts the compaction. Nothing is skipped.
3. **Tombstones.** If the newest version of a key is a tombstone, the key is dropped from the output. This is safe only because the input set is *every* table: no older version of the key exists outside the merge, and the memtable (newer than all tables) shadows the output anyway. That argument is why V6 compacts the full set, since a partial compaction would have to keep tombstones.
4. **Two passes.** The Bloom filter must be sized before the first record is written, so the survivor count has to be known. Pass 1 runs the merge and counts survivors and tombstones, and pass 2 repeats the same merge and streams the survivors into `write_sstable(..., expected_count=n)`. The merged database is never held in memory. The cost is that the input record regions are read twice (`bytes_read` and `records_read` count both passes). The simpler alternative is to materialise the merged list, at a memory cost proportional to the database.
5. **Output.** The same table format a V5 flush writes (records, sparse index, Bloom filter, footer), built in `cmp-NNNNNNNN.sst.tmp`, fsynced, renamed, and directory fsynced. `write_sstable` verifies that the number of records it received equals `expected_count` before the rename. A mismatch raises `ValueError`, removes the `.tmp` and installs nothing.
6. **Install and cleanup.** The new table is opened, becomes the only table in memory, the replaced tables' blocks are dropped from the block cache, and the old files are deleted oldest first.

If every key turns out to be deleted, there's nothing to write (an SSTable can't be empty): the inputs are deleted oldest first and no output exists. That's safe because the newest version of every key is a tombstone, so any suffix of the tables still answers "absent" for every key.

## Table names, ordering and the supersede rule

Ids come from the same counter as WAL segments and never repeat. Flushes write `sst-NNNNNNNN.sst`, and a compaction writes `cmp-NNNNNNNN.sst` with a fresh id, higher than every input. Table age is the id, whichever prefix it carries.

**A `cmp-N` table supersedes every table with an id below N.** Recovery applies this rule: it deletes all lower-id tables, then loads what remains. There's no manifest, because the file name carries the fact the manifest would. Without this rule, dropping tombstones would be unsafe: a crash after the output is installed but before the old tables are deleted would leave an old table holding a value whose tombstone was dropped, and it would come back. With it, "output plus old tables" is recovered to "output only".

Table discovery is `KVStore._discover_tables()`. V2 to V5 did this inline in `__init__` (list `sst-*.sst`, delete unfinished `.tmp`); V6 moves it into this method and extends it to handle `cmp-*.sst` compaction outputs.

## Crash windows

| crash point | on disk | recovery result |
|---|---|---|
| during merge or output write | old tables, maybe `cmp-*.sst.tmp` | `.tmp` deleted; old tables unchanged |
| output complete, rename not done | the same | the same |
| after rename, before any deletion | `cmp-N` plus all old tables | old tables below N deleted; output only |
| during deletion of the old tables | `cmp-N` plus some old tables | the same; any subset is covered by the rule |
| all old tables deleted | `cmp-N` | nothing to do |
| older `cmp-M` left next to `cmp-N` (M < N) | two outputs | the lower one is deleted too |
| `cmp-N` damaged | bad footer, index or filter | open raises `CorruptionError` |

The rename inside `write_sstable` is the commit point. Before it, nothing has changed, and after it, the new table is authoritative. A damaged output is never skipped in favour of the old tables, because the old tables may be partly deleted. Corruption of record bytes inside a table is detected, as for any table, when a read reaches the damaged block.

If the output can't be opened right after the rename, it's removed and the compaction fails with the old state intact. If a deletion fails at run time, the process already serves the new table, and the leftover old files are removed by the next recovery (they can't affect results because of the supersede rule).

## Invariants

- Every key's visible value after compaction equals its value before: the newest version, or absent if that version is a tombstone.
- A compaction output is a complete, valid table under its final name or doesn't exist under it.
- At any instant, the set of files on disk recovers to a database equal to the one acknowledged: either the pre-compaction tables, or the post-compaction table.
- An old version can never outrank a newer one, because order is the table id and the output's id exceeds all inputs'.
- Unflushed data is never lost by compaction: it lives in the WAL and memtable, which compaction doesn't touch.

## WAL and memtable

`compact()` doesn't flush. The memtable and the WAL segment holding its records are left as they are. The compaction output takes an id that may be higher than the active WAL segment's id. On reopen, the existing rule ("append to the newest segment only if it is newer than every table, otherwise start a new one") then starts a fresh segment while still replaying the old one. That's the path an interrupted flush already takes, and the memtable replayed from the WAL is newer than every table regardless of ids.

## Block cache

Cache keys are `(table path, block)`, and the output has a new path, so no old entry could match it. The replaced tables are also removed from the active table list, so they're unreachable. On top of that, `compact()` calls `BlockCache.discard_table(path)` for each replaced table so dead entries stop occupying capacity. The cache stays in memory and is empty on every open.

## Compatibility

- V2 to V5 behavior is unchanged. `write_sstable` gained an optional `expected_count`, and without it behavior is as before.
- Compaction reads V2, V3, V4 and V5 tables and writes the current format.
- A directory that holds `cmp-*` files is **owned by V6**. V2 to V5 recognise only `sst-*.sst`, so they would ignore the compacted table and present an incomplete database. They may read individual compatible SSTable files, but V2 to V5 aren't compatible readers of a V6-compacted directory, and no downgrade path is provided.

## Cost model

Compaction rewrites data that is already on disk, so it **adds write amplification**: every surviving record is written again, and every input record is read twice (two passes). What it's expected to reduce is read amplification (fewer tables to consult) and space amplification (obsolete versions and tombstones removed). The measured effect is in the V6 section of [benchmarks.md](benchmarks.md#v6-benchmark).

Statistics returned by `compact()` and accumulated by `compaction_stats()`:

| field | meaning |
|---|---|
| `input_tables`, `output_tables` | tables merged; tables written (0 or 1) |
| `records_read` | input records read, over both passes |
| `records_emitted` | records written to the output |
| `obsolete_records_removed` | superseded versions (includes tombstones shadowed by newer versions) |
| `tombstones_removed` | tombstones that were a key's newest version and were dropped |
| `bytes_read` | record-region bytes of the inputs, over both passes |
| `bytes_written`, `output_table_bytes` | size of the new table |
| `input_table_bytes` | file sizes of the input tables |
| `write_amplification` | `bytes_written / input_table_bytes` (the rewrite cost relative to the data compacted) |
| `duration_s` | wall time |

Pass 2 doesn't run when nothing survives, so `records_read` and `bytes_read` then count one pass. When it runs, `records_read / 2 = records_emitted + obsolete_records_removed + tombstones_removed`.

## Limitations

- Full compaction rewrites the whole database every time, so cost grows with database size. There's no partial or incremental compaction.
- It must be called explicitly, since nothing triggers it. Writes and reads are blocked while it runs.
- Peak disk use during a compaction is roughly the old tables plus the new one.
- Directory fsync is a no-op on Windows (as in V2), so the durability of renames and deletions there is whatever the filesystem journal provides.
- `compaction_stats()` counts only this store object's compactions.

## Code walkthrough

Everything is in `store.py`: `merge_newest`, `compact`, `_discover_tables` and `_retire`.

**1. `merge_newest(tables)` is a k-way merge that keeps only the newest version of each key:**

```python
streams = [_ranked(table, rank) for rank, table in enumerate(reversed(tables))]  # rank 0 = newest
last = None
for key, _rank, value in heapq.merge(*streams):
    if key == last:
        continue            # an older version of a key already emitted
    last = key
    yield key, value
```

`_ranked` yields `(key, rank, value)`. Tuples compare key first, then rank, so for equal keys the newest table sorts first. The ranking is a separate function on purpose: building it inline would late-bind `rank` and tag every record with the last value.

**2. `compact()` pass 1** consumes the merge only to count survivors and tombstones (every input is fully validated through `SSTable.items()`).

**3. `compact()` pass 2** streams survivors into a new table:

```python
new_path = self._cmp_path(self._take_id())
sstable.write_sstable(new_path, _live_entries(tables), ..., expected_count=survivors)
```

`_live_entries` filters out tombstones. `expected_count` lets the Bloom filter be sized before any record is written without holding the data in memory.

**4. The install step** opens the new table (re-validating footer, index, filter), swaps `self._sstables = [new_table]`, then `_retire(tables)` discards cache entries, deletes old files oldest first, and fsyncs the directory.

**5. `_discover_tables` is the "no manifest" trick.** It finds the highest-id `cmp-*` file (the `barrier`) and deletes every table with a smaller id before loading:

```python
for table_id, kind, _ in found:
    if kind == "cmp" and table_id > barrier:
        barrier = table_id
# tables with id < barrier are obsolete and removed
```

**6. The edge case:** if every key is deleted, nothing is written. The inputs are deleted and the table list becomes empty.

## Interview questions

**Why do we need compaction at all?**
Without it, tables only accumulate: reads touch more filters and files, and obsolete versions and tombstones waste space. Compaction reduces read and space amplification at the cost of write amplification.

**Define read, write and space amplification, and say which way compaction moves each.**
Read amplification: how much work or I/O per read (fewer tables -> lower). Space amplification: disk used vs live data (dropping dead versions -> lower). Write amplification: bytes written vs bytes logically written (rewriting survivors -> higher).

**Why is it safe to drop tombstones here?**
Because the compaction covers *every* table. No older version of a deleted key exists outside the merge, and the memtable (newer than all tables) shadows the output anyway. A partial compaction must keep tombstones.

**Why a `cmp-N` filename and the "supersedes everything below N" rule instead of a manifest?**
After the rename, a crash could leave the new output plus old tables. If tombstones were dropped and an old table survived, a deleted value could come back. The name carries the information a manifest would: on recovery, anything with a lower id than the newest `cmp` is deleted. This avoids a second file and a second atomic update.

**What is the commit point of a compaction?**
The `os.replace` inside `write_sstable`. Before it, the old tables are authoritative. After it, the output is, and recovery cleans up leftovers.

**Walk me through a crash after the rename but before any deletion.**
Disk has `cmp-N` plus all old tables. Recovery sees the barrier `N` and deletes every lower-id table, leaving the output only. Any subset of deletions is covered by the same rule.

**Why two passes over the input?**
The Bloom filter must be sized before the first record is written, so the number of survivors has to be known first. The alternative is materialising the merged data in memory, which costs memory proportional to the database. Trading double reads for bounded memory is a deliberate choice (and noted as a TODO).

**Why `heapq.merge` and why rank by table age?**
Each table is already sorted, so a k-way merge yields globally sorted output in O(N log k). Age, never values or dict order, decides which version is newest.

**Why does the output get an id higher than all inputs?**
Ids are table age. The output must outrank every input so an old version can never beat a newer one, and so the supersede rule is well-defined.

**Why doesn't `compact()` flush the memtable?**
It isn't needed for correctness: the memtable and WAL are newer than all tables and are never touched. Unflushed data is never lost by a compaction.

**What happens if the output can't be opened after the rename?**
It's removed and the compaction fails with the old state intact, since nothing has been deleted yet.

**What are the downsides of full compaction?**
It rewrites the entire database every time (cost grows with size), blocks readers and writers while it runs, and needs roughly old + new disk space at peak. Real systems use leveled or size-tiered strategies to bound this.

**How would you extend this to leveled compaction?**
Add levels with size limits, compact one table (or a few overlapping ones) into the next level, and keep tombstones until they reach the bottom level. That needs a manifest or equivalent to track level membership, which is the thing this version avoids.

**Why are V2-V5 not compatible readers of a V6 directory?**
They only recognise `sst-*.sst`, so they would ignore `cmp-*.sst` and present an incomplete database. No downgrade path is provided.
