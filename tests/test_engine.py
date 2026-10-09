import glob
import os
import random

import pytest

from lsm_store import CorruptionError, KVStore
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
