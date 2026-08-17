"""Disk cache keyed by a hash of the request.

Mandatory because this pipeline gets re-run many times and refetching is both
slow and rude. Every search and every page fetch goes through here.

Layout: data/cache/<namespace>/<ab>/<sha256>.json
The two-char shard keeps directories from growing to 100k entries on one level.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .config import settings


def key_for(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


class DiskCache:
    def __init__(self, namespace: str, root: Path | None = None) -> None:
        self.root = (root or settings.cache_dir) / namespace
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _path(self, k: str) -> Path:
        return self.root / k[:2] / f"{k}.json"

    def get(self, k: str) -> Any | None:
        p = self._path(k)
        if not p.exists():
            self.misses += 1
            return None
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt cache entry is a miss, not a crash.
            self.misses += 1
            return None
        self.hits += 1
        return payload.get("value")

    def set(self, k: str, value: Any, meta: dict[str, Any] | None = None) -> None:
        p = self._path(k)
        p.parent.mkdir(parents=True, exist_ok=True)
        body = {"cached_at": time.time(), "meta": meta or {}, "value": value}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)  # atomic: a killed run never leaves a half-written entry
