import glob
import os

import pytest

from lsm_store import KVStore, sstable


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "db")


def table_files(path):
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(path, "*.sst")))


def build(db):
    expected = {}
    for i in range(60):
        db.put(b"key%03d" % i, b"first%03d" % i)
        expected[b"key%03d" % i] = b"first%03d" % i
    for i in range(0, 60, 3):
        db.put(b"key%03d" % i, b"second%03d" % i)
        expected[b"key%03d" % i] = b"second%03d" % i
    for i in range(0, 60, 5):
        db.delete(b"key%03d" % i)
        expected.pop(b"key%03d" % i)
    db.flush()
    return expected


def check(db, expected):
    for i in range(60):
        key = b"key%03d" % i
        assert db.get(key) == expected.get(key)


def test_compacting_an_empty_store_does_nothing(path):
    with KVStore(path) as db:
        assert db.compact()["compactions"] == 0
        assert db.table_count() == 0


def test_compaction_merges_all_tables_into_one(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        expected = build(db)
        assert db.table_count() > 2
        db.compact()
        assert db.table_count() == 1
        assert table_files(path)[0].startswith("cmp-") and len(table_files(path)) == 1
        check(db, expected)


def test_compaction_removes_old_versions_and_tombstones(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        build(db)
        stats = db.compact()
        assert stats["obsolete_records_removed"] > 0
        assert stats["tombstones_removed"] == 12  # every 5th key of 60 was deleted
        assert stats["records_emitted"] == 48
        assert stats["output_table_bytes"] < stats["input_table_bytes"]


def test_data_survives_reopen_after_compaction(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        expected = build(db)
        db.compact()
    with KVStore(path, memtable_limit_bytes=300) as db:
        check(db, expected)
        assert db.table_count() == 1


def test_compacting_when_everything_is_deleted_leaves_no_tables(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        for i in range(30):
            db.put(b"key%03d" % i, b"v")
        for i in range(30):
            db.delete(b"key%03d" % i)
        db.flush()
        db.compact()
        assert db.table_count() == 0 and table_files(path) == []
        assert db.get(b"key005") is None


def test_a_single_compacted_table_is_left_alone(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        build(db)
        db.compact()
        before = table_files(path)
        assert db.compact()["compactions"] == 0
        assert table_files(path) == before


def test_compaction_does_not_touch_the_memtable(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        build(db)
        db.put(b"key001", b"newest")  # still only in the memtable
        db.compact()
        assert db.get(b"key001") == b"newest"


def test_a_failed_compaction_changes_nothing(path, monkeypatch):
    with KVStore(path, memtable_limit_bytes=300) as db:
        expected = build(db)
        before = table_files(path)

        def disk_full(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(sstable, "write_sstable", disk_full)
        with pytest.raises(OSError):
            db.compact()
        monkeypatch.undo()
        assert table_files(path) == before
        check(db, expected)


def test_a_crash_after_installing_the_new_table_is_finished_on_open(path):
    db = KVStore(path, memtable_limit_bytes=300)
    expected = build(db)
    old_tables = table_files(path)

    def crash(old_tables):
        raise RuntimeError("simulated crash before the old tables were deleted")

    db._retire = crash
    with pytest.raises(RuntimeError):
        db.compact()
    db.close()
    assert len(table_files(path)) == len(old_tables) + 1  # the new cmp- table sits next to the old ones
    with KVStore(path, memtable_limit_bytes=300) as db:  # opening deletes everything the cmp- table replaced
        assert table_files(path) == [name for name in table_files(path) if name.startswith("cmp-")]
        check(db, expected)


def test_compaction_drops_the_cached_blocks_of_the_old_tables(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        expected = build(db)
        check(db, expected)
        assert db.cache_stats()["entries"] > 0
        db.compact()
        assert db.cache_stats()["entries"] == 0
        check(db, expected)


def test_stats_add_up_over_several_compactions(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        build(db)
        db.compact()
        for i in range(60, 120):
            db.put(b"key%03d" % i, b"more")
        db.flush()
        db.compact()
        assert db.compaction_stats()["compactions"] == 2
