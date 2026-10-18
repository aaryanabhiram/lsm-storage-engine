from collections import OrderedDict

DEFAULT_BLOCK_CACHE_BYTES = 8 * 1024 * 1024  # about 4,000 blocks of 16 ~124-byte records


class BlockCache:
    # TODO not thread safe, the whole store is single threaded for now. bg threads would need a lock around get/put
    # TODO cache holds raw block bytes so a hit still decodes + crc-checks the whole block again. prob costs more than the file read it saves

    def __init__(self, capacity_bytes=DEFAULT_BLOCK_CACHE_BYTES):
        bad = False
        if not isinstance(capacity_bytes, int):
            bad = True
        else:
            if isinstance(capacity_bytes, bool):
                bad = True
            elif capacity_bytes < 0:
                bad = True
        if bad:
            raise ValueError("block cache capacity must be a non-negative integer (0 disables the cache)")
        self.capacity_bytes = capacity_bytes
        self._entries = OrderedDict()  # (table, block) -> bytes, least recently used first
        self.bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.insertions = 0

    def get(self, table, block):
        key = (table, block)
        data = self._entries.get(key)
        if data is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return data

    def put(self, table, block, data):
        if len(data) > self.capacity_bytes:
            return False
        key = (table, block)
        old = self._entries.get(key)
        if old is not None:
            self.bytes -= len(old)
        while self.bytes + len(data) > self.capacity_bytes:
            _, victim = self._entries.popitem(last=False)
            self.bytes -= len(victim)
            self.evictions += 1
        self._entries[key] = data
        self.bytes += len(data)
        self.insertions += 1
        return True

    def discard(self, table, block):
        old = self._entries.get((table, block))
        if old is not None:
            self.bytes -= len(old)

    def __len__(self):
        return len(self._entries)

    def __contains__(self, item):
        return item in self._entries  # doesn't touch recency or counters

    def keys(self):
        return list(self._entries)

    def stats(self):
        lookups = self.hits + self.misses
        if lookups:
            hit_rate = self.hits / lookups
        else:
            hit_rate = 0.0
        return {"capacity_bytes": self.capacity_bytes, "bytes": self.bytes, "entries": len(self._entries),
                "hits": self.hits, "misses": self.misses, "evictions": self.evictions,
                "insertions": self.insertions, "hit_rate": hit_rate}
