import glob
import os
import random

import pytest

from lsm_store import CorruptionError, KVStore, sstable
from lsm_store.blockcache import BlockCache
from lsm_store.record import OP_PUT, encode


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "db")  # the store is a directory now


def newest_wal(path):
    return sorted(glob.glob(os.path.join(path, "wal-*.log")))[-1]


def sstables(path):
    return sorted(glob.glob(os.path.join(path, "sst-*.sst")))


def test_put_then_get(path):
    with KVStore(path) as db:
        db.put(b"name", b"alice")
        assert db.get(b"name") == b"alice"


def test_get_missing_key_returns_none(path):
    with KVStore(path) as db:
        assert db.get(b"nope") is None


def test_overwrite_returns_newest_value(path):
    with KVStore(path) as db:
        db.put(b"k", b"1")
        db.put(b"k", b"2")
        db.put(b"k", b"3")
        assert db.get(b"k") == b"3"


def test_delete_removes_key(path):
    with KVStore(path) as db:
        db.put(b"k", b"v")
        db.delete(b"k")
        assert db.get(b"k") is None


def test_empty_value_is_not_a_delete(path):
    with KVStore(path) as db:
        db.put(b"k", b"")
        assert db.get(b"k") == b""


def test_data_survives_reopen(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.put(b"b", b"2")
        db.delete(b"b")
    with KVStore(path) as db:
        assert db.get(b"a") == b"1"
        assert db.get(b"b") is None


def test_small_memtable_flushes_to_sstables(path):
    with KVStore(path, memtable_limit_bytes=100) as db:
        for i in range(50):
            db.put(b"key%03d" % i, b"value%03d" % i)
        assert len(sstables(path)) > 0
        for i in range(50):
            assert db.get(b"key%03d" % i) == b"value%03d" % i


def test_data_survives_reopen_after_flush(path):
    with KVStore(path, memtable_limit_bytes=100) as db:
        for i in range(50):
            db.put(b"key%03d" % i, b"value%03d" % i)
    with KVStore(path, memtable_limit_bytes=100) as db:
        for i in range(50):
            assert db.get(b"key%03d" % i) == b"value%03d" % i


def test_manual_flush_empties_memtable_into_one_table(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.put(b"b", b"2")
        db.flush()
        assert len(sstables(path)) == 1
        assert db.get(b"a") == b"1"
    with KVStore(path) as db:
        assert db.get(b"b") == b"2"


def test_newer_table_overrides_older_table(path):
    with KVStore(path) as db:
        db.put(b"k", b"old")
        db.flush()
        db.put(b"k", b"new")
        db.flush()
        assert len(sstables(path)) == 2
        assert db.get(b"k") == b"new"


def test_delete_hides_value_in_older_sstable(path):
    with KVStore(path) as db:
        db.put(b"k", b"v")
        db.flush()
        db.delete(b"k")
        assert db.get(b"k") is None  # tombstone in memtable
        db.flush()
        assert db.get(b"k") is None  # tombstone in newer table
    with KVStore(path) as db:
        assert db.get(b"k") is None


def test_torn_wal_tail_is_dropped_on_reopen(path):
    with KVStore(path) as db:
        db.put(b"good", b"value")
    with open(newest_wal(path), "ab") as f:  # simulate a crash in the middle of a write
        f.write(encode(OP_PUT, b"torn", b"x" * 50)[:-5])
    with KVStore(path) as db:
        assert db.get(b"good") == b"value"
        assert db.get(b"torn") is None


def test_corrupted_sstable_raises(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.flush()
    table = sstables(path)[0]
    data = bytearray(open(table, "rb").read())
    data[13] ^= 0xFF  # a byte in the first record's key
    with open(table, "wb") as f:
        f.write(data)
    with KVStore(path) as db:
        with pytest.raises(CorruptionError):
            db.get(b"a")


def test_leftover_tmp_file_from_a_crashed_flush_is_removed(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
    tmp = os.path.join(path, "sst-00000099.sst.tmp")
    with open(tmp, "wb") as f:
        f.write(b"half written garbage")
    with KVStore(path) as db:
        assert db.get(b"a") == b"1"
    assert not os.path.exists(tmp)


def test_rejects_bad_arguments(path):
    with pytest.raises(ValueError):
        KVStore(path, memtable_limit_bytes=0)
    with KVStore(path) as db:
        with pytest.raises(TypeError):
            db.put("str-key", b"v")


def test_closed_store_raises(path):
    db = KVStore(path)
    db.close()
    with pytest.raises(ValueError):
        db.put(b"k", b"v")


def test_random_ops_match_a_dict(path):
    rng = random.Random(1)
    expected = {}
    db = KVStore(path, sync=False, memtable_limit_bytes=200)
    for i in range(400):
        key = b"k%d" % rng.randrange(30)
        if rng.random() < 0.7:
            value = b"v%d" % i
            db.put(key, value)
            expected[key] = value
        else:
            db.delete(key)
            expected.pop(key, None)
        if i % 100 == 99:  # reopen now and then
            db.close()
            db = KVStore(path, sync=False, memtable_limit_bytes=200)
    for n in range(30):
        key = b"k%d" % n
        assert db.get(key) == expected.get(key)
    db.close()


def test_index_interval_must_be_positive(path):
    with pytest.raises(ValueError):
        KVStore(path, index_interval=0)


def test_every_key_found_with_a_small_index_interval(path):
    with KVStore(path, memtable_limit_bytes=200, index_interval=2) as db:
        for i in range(0, 100, 2):
            db.put(b"key%03d" % i, b"value%03d" % i)
        assert len(sstables(path)) > 1
        for i in range(0, 100, 2):
            assert db.get(b"key%03d" % i) == b"value%03d" % i


def test_missing_keys_before_between_and_after_the_stored_range(path):
    with KVStore(path, index_interval=3) as db:
        for i in range(10, 40, 2):
            db.put(b"key%03d" % i, b"v")
        db.flush()
        assert db.get(b"key005") is None  # before the first key
        assert db.get(b"key011") is None  # between two keys
        assert db.get(b"key099") is None  # after the last key


def test_old_unindexed_table_is_still_readable(path):
    os.makedirs(path)
    sstable.write_sstable(os.path.join(path, "sst-00000001.sst"), [(b"a", b"1"), (b"b", None)])  # no index
    with KVStore(path) as db:
        assert db.get(b"a") == b"1"
        assert db.get(b"b") is None


def test_corrupted_sstable_footer_raises_on_open(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.flush()
    table = sstables(path)[0]
    data = bytearray(open(table, "rb").read())
    data[-10] ^= 0xFF  # inside the footer
    with open(table, "wb") as f:
        f.write(data)
    with pytest.raises(CorruptionError):
        KVStore(path)


def test_bloom_bits_per_key_must_be_valid(path):
    with pytest.raises(ValueError):
        KVStore(path, bloom_bits_per_key=0)
    with pytest.raises(ValueError):
        KVStore(path, bloom_bits_per_key=1000)


def test_filter_never_hides_a_stored_key_or_a_deleted_one(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        for i in range(200):
            db.put(b"key%04d" % i, b"value%04d" % i)
        for i in range(0, 200, 5):
            db.delete(b"key%04d" % i)
        assert len(sstables(path)) > 2
        for i in range(200):
            expected = None if i % 5 == 0 else b"value%04d" % i
            assert db.get(b"key%04d" % i) == expected


def test_absent_keys_are_not_found(path):
    with KVStore(path, memtable_limit_bytes=300) as db:
        for i in range(0, 200, 2):
            db.put(b"key%04d" % i, b"v")
        for i in range(1, 200, 2):
            assert db.get(b"key%04d" % i) is None  # in range, but never written
        assert db.get(b"zzz") is None


def test_filter_rules_out_a_table_without_opening_the_file(path, monkeypatch):
    table_path = path + "-table.sst"
    sstable.write_sstable(table_path, [(b"key%03d" % i, b"v") for i in range(50)],
                          index_interval=4, bloom_bits_per_key=10)
    table = sstable.SSTable(table_path)
    absent = [b"nope%03d" % i for i in range(100)]
    ruled_out = [k for k in absent if not table.may_contain(k)]
    assert len(ruled_out) > 90  # about 99% of absent keys should be rejected at 10 bits per key

    def no_open(p):
        raise AssertionError("file was opened")

    monkeypatch.setattr(sstable, "_open_table", no_open)
    for k in ruled_out:
        assert table.lookup(k) is sstable.NOT_FOUND


def test_table_without_a_filter_is_still_searched(path):
    table_path = path + "-table.sst"
    sstable.write_sstable(table_path, [(b"a", b"1"), (b"b", b"2")], index_interval=4)  # index only, no filter
    table = sstable.SSTable(table_path)
    assert table.may_contain(b"anything")
    assert table.lookup(b"b") == b"2"


def test_corrupted_filter_block_raises_on_open(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.flush()
    table = sstables(path)[0]
    data = bytearray(open(table, "rb").read())
    data[-61] ^= 0xFF  # the last byte of the filter block, just before the 60-byte footer
    with open(table, "wb") as f:
        f.write(data)
    with pytest.raises(CorruptionError):
        KVStore(path)


def test_block_cache_bytes_must_be_valid(path):
    with pytest.raises(ValueError):
        KVStore(path, block_cache_bytes=-1)


def test_second_get_of_the_same_key_hits_the_cache(path):
    with KVStore(path) as db:
        for i in range(40):
            db.put(b"key%03d" % i, b"value%03d" % i)
        db.flush()
        assert db.get(b"key017") == b"value017"
        after_first = db.cache_stats()
        assert after_first["misses"] == 1 and after_first["hits"] == 0
        assert db.get(b"key017") == b"value017"
        assert db.cache_stats()["hits"] == 1


def test_disabled_cache_gives_the_same_answers(path):
    with KVStore(path, memtable_limit_bytes=300, block_cache_bytes=0) as db:
        for i in range(100):
            db.put(b"key%03d" % i, b"value%03d" % i)
        for i in range(100):
            assert db.get(b"key%03d" % i) == b"value%03d" % i
        assert db.cache_stats()["hits"] == 0 and db.cache_stats()["entries"] == 0


def test_reopening_starts_with_an_empty_cache(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.flush()
        db.get(b"a")
        assert db.cache_stats()["entries"] == 1
    with KVStore(path) as db:
        assert db.cache_stats()["entries"] == 0


def test_cache_evicts_the_least_recently_used_block():
    cache = BlockCache(100)
    cache.put("t", 1, b"a" * 40)
    cache.put("t", 2, b"b" * 40)
    cache.get("t", 1)  # block 1 is now the most recently used
    cache.put("t", 3, b"c" * 40)  # does not fit: block 2 is evicted
    assert ("t", 1) in cache and ("t", 3) in cache and ("t", 2) not in cache
    assert cache.stats()["evictions"] == 1 and cache.bytes == 80


def test_block_bigger_than_the_whole_cache_is_not_stored():
    cache = BlockCache(10)
    assert cache.put("t", 1, b"x" * 11) is False
    assert len(cache) == 0


def test_corrupted_block_is_not_kept_in_the_cache(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.flush()
    table = sstables(path)[0]
    data = bytearray(open(table, "rb").read())
    data[13] ^= 0xFF  # a byte in the first record's key
    with open(table, "wb") as f:
        f.write(data)
    with KVStore(path) as db:
        with pytest.raises(CorruptionError):
            db.get(b"a")
        assert db.cache_stats()["entries"] == 0
