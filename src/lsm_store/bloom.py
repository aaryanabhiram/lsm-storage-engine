import hashlib
import math
import struct

DEFAULT_BITS_PER_KEY = 10
MAX_BITS_PER_KEY = 64
MAX_HASHES = 32
MIN_FILTER_BYTES = 8  # tiny tables still get 64 bits

_HALVES = struct.Struct(">QQ")


def num_hashes(bits_per_key):
    return max(1, min(MAX_HASHES, round(bits_per_key * math.log(2))))


def filter_bytes(count, bits_per_key):
    return max(MIN_FILTER_BYTES, -(-count * bits_per_key // 8))


def expected_false_positive_rate(count, size_bytes, k):
    return (1.0 - math.exp(-k * count / (size_bytes * 8))) ** k


def _positions(key, m, k):
    # TODO key gets re-hashed for every table even though the digest never changes. hash once per get() and reuse
    h1, h2 = _HALVES.unpack(hashlib.blake2b(key, digest_size=16).digest())
    h2 |= 1
    for i in range(k):
        yield (h1 + i * h2) % m


def add(bits, key, k):
    for p in _positions(key, len(bits) * 8, k):
        bits[p >> 3] |= 1 << (p & 7)


def may_contain(bits, key, k):
    for p in _positions(key, len(bits) * 8, k):
        if not bits[p >> 3] & (1 << (p & 7)):
            return False
    return True
