import os
import random

import pytest

from lsm_store import CorruptionError, KVStore
from lsm_store.record import OP_PUT, encode


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "data.wal")


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


def test_delete_missing_key_is_ok(path):
    with KVStore(path) as db:
        db.delete(b"never-written")
        assert db.get(b"never-written") is None


def test_empty_value_is_not_a_delete(path):
    with KVStore(path) as db:
        db.put(b"k", b"")
        assert db.get(b"k") == b""


def test_data_survives_reopen(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.put(b"b", b"2")
    with KVStore(path) as db:
        assert db.get(b"a") == b"1"
        assert db.get(b"b") == b"2"


def test_overwrite_and_delete_survive_reopen(path):
    with KVStore(path) as db:
        db.put(b"a", b"old")
        db.put(b"a", b"new")
        db.put(b"b", b"gone")
        db.delete(b"b")
    with KVStore(path) as db:
        assert db.get(b"a") == b"new"
        assert db.get(b"b") is None


def test_torn_tail_is_dropped_on_reopen(path):
    with KVStore(path) as db:
        db.put(b"good", b"value")
    good_size = os.path.getsize(path)
    # simulate a crash in the middle of writing a record
    with open(path, "ab") as f:
        f.write(encode(OP_PUT, b"torn", b"x" * 50)[:-5])
    with KVStore(path) as db:
        assert db.get(b"good") == b"value"
        assert db.get(b"torn") is None
    assert os.path.getsize(path) == good_size


def test_corrupted_record_raises(path):
    with KVStore(path) as db:
        db.put(b"a", b"1")
        db.put(b"b", b"2")
    data = bytearray(open(path, "rb").read())
    data[13] ^= 0xFF  # flip a byte in the first record's key (right after the 13-byte header)
    with open(path, "wb") as f:
        f.write(data)
    with pytest.raises(CorruptionError):
        KVStore(path)


def test_rejects_non_bytes(path):
    with KVStore(path) as db:
        with pytest.raises(TypeError):
            db.put("str-key", b"v")
        with pytest.raises(TypeError):
            db.get("str-key")


def test_closed_store_raises(path):
    db = KVStore(path)
    db.close()
    with pytest.raises(ValueError):
        db.put(b"k", b"v")


def test_random_ops_match_a_dict(path):
    rng = random.Random(1)
    expected = {}
    db = KVStore(path, sync=False)
    for i in range(300):
        key = b"k%d" % rng.randrange(20)
        if rng.random() < 0.7:
            value = b"v%d" % i
            db.put(key, value)
            expected[key] = value
        else:
            db.delete(key)
            expected.pop(key, None)
        if i % 100 == 99:  # reopen now and then
            db.close()
            db = KVStore(path, sync=False)
    for n in range(20):
        key = b"k%d" % n
        assert db.get(key) == expected.get(key)
    db.close()


def test_wal_only_grows(path):
    sizes = []
    with KVStore(path) as db:
        db.put(b"k", b"v1")
        sizes.append(os.path.getsize(path))
        db.put(b"k", b"v2")  # overwrite is a new record, not an in-place edit
        sizes.append(os.path.getsize(path))
        db.delete(b"k")
        sizes.append(os.path.getsize(path))
    assert sizes[0] < sizes[1] < sizes[2]
