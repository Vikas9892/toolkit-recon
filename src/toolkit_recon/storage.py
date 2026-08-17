"""Evidence storage, JSONL tracing, and crash-safe checkpointing.

Three separate jobs, all on disk, no database:

* `save_evidence`  - every fetched page under data/raw/{slug}/ so any claim in
  the final JSON can be traced back to the bytes it came from.
* `TraceLog`       - one JSONL line per app in logs/trace.jsonl.
* `Checkpoint`     - the completed rows so far, rewritten after every single
  app. A crash at app 87 costs one app, not 87.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from .config import settings
from .schema import AppResearch, AppTrace, FetchedDoc

_UNSAFE = re.compile(r"[^a-z0-9._-]+")


def _safe(name: str, limit: int = 80) -> str:
    s = _UNSAFE.sub("-", name.lower()).strip("-")
    return (s[:limit] or "doc").strip("-")


def save_evidence(slug: str, docs: list[FetchedDoc], meta: dict) -> Path:
    """Write raw fetched text plus a manifest under data/raw/{slug}/."""
    d = settings.raw_dir / slug
    d.mkdir(parents=True, exist_ok=True)

    manifest = {"slug": slug, **meta, "documents": []}
    for i, doc in enumerate(docs, 1):
        fname = f"{i:02d}_{_safe(doc.url.split('//')[-1])}.txt"
        if doc.ok and doc.text:
            (d / fname).write_text(
                f"SOURCE URL: {doc.url}\n{'=' * 72}\n{doc.text}", encoding="utf-8"
            )
        manifest["documents"].append(
            {
                "url": doc.url,
                "file": fname if (doc.ok and doc.text) else None,
                "ok": doc.ok,
                "from_cache": doc.from_cache,
                "is_official": doc.is_official,
                "chars": len(doc.text),
                "error": doc.error,
            }
        )
    (d / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return d


class TraceLog:
    """Append-only JSONL. Serialised by a lock because eight workers share it."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.logs_dir / "trace.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        self.path.write_text("", encoding="utf-8")

    async def write(self, trace: AppTrace) -> None:
        line = json.dumps(trace.model_dump(), ensure_ascii=False)
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class Checkpoint:
    """Rows completed so far, flushed after every app.

    Writes to a temp file then renames, so an interrupt mid-write cannot leave
    a truncated checkpoint behind.
    """

    def __init__(self, pass_number: int) -> None:
        self.path = settings.checkpoint_dir / f"pass{pass_number}.checkpoint.json"
        self._rows: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def load(self) -> dict[str, dict]:
        if self.path.exists():
            try:
                self._rows = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._rows = {}
        return dict(self._rows)

    async def record(self, slug: str, row: AppResearch) -> None:
        async with self._lock:
            self._rows[slug] = row.model_dump()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._rows, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)

    @property
    def rows(self) -> dict[str, dict]:
        return dict(self._rows)


def write_output(rows: list[AppResearch], pass_number: int) -> Path:
    path = settings.data_dir / f"pass{pass_number}.json"
    payload = [r.model_dump() for r in rows]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
