"""Append-only, hash-chained evidence ledger.

Every gateway call and every score is one JSON line, and each line commits to the
one before it. Editing or deleting a line breaks the chain. The chain has no
signature, so someone who rewrites the whole file can rebuild it. That is why the
head hash is printed into the report and the policy: publish the head and the
ledger is pinned to it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

GENESIS = "0" * 64


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_hash(seq: int, prev: str, event: str, data: dict) -> str:
    return sha256(canonical({"seq": seq, "prev": prev, "event": event, "data": data}))


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.head = GENESIS
        self.seq = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("x", encoding="utf-8")

    def append(self, event: str, **data: object) -> str:
        digest = _entry_hash(self.seq, self.head, event, data)
        entry = {"seq": self.seq, "prev": self.head, "event": event, "data": data, "hash": digest}
        self._file.write(canonical(entry) + "\n")
        self._file.flush()
        self.head = digest
        self.seq += 1
        return digest

    def close(self) -> None:
        self._file.close()


def read(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def verify_chain(path: Path) -> tuple[bool, str, int]:
    """Return (ok, head hash or the reason it failed, entries read)."""
    head, count = GENESIS, 0
    for entry in read(path):
        if entry["seq"] != count:
            return False, f"entry {count}: sequence number is {entry['seq']}", count
        if entry["prev"] != head:
            return False, f"entry {count}: does not link to the entry before it", count
        if _entry_hash(entry["seq"], entry["prev"], entry["event"], entry["data"]) != entry["hash"]:
            return False, f"entry {count}: contents do not match its hash", count
        head = entry["hash"]
        count += 1
    return True, head, count
