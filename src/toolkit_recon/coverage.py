"""Order the work queue for category coverage rather than list order.

When the budget will not reach every app, list order is the wrong priority.
The corpus is grouped by category, so processing it in order spends everything
on the first few categories and leaves the last ones at zero — and a category
with no rows cannot support any claim about it.

This reorders the pending queue so every category climbs toward a minimum
viable count before any category gets an extra app. A run that stops early
then yields a shallow-but-complete picture instead of a deep-but-partial one.

The decision and its reason are recorded in `data/queue_order.json` so it reads
as a deliberate call rather than an accident of ordering.
"""

from __future__ import annotations

import json
from collections import defaultdict

from .apps import AppSpec
from .config import settings

# Below this a category cannot support a per-category claim. Chosen as the
# smallest count where a distribution is worth showing at all; it is a floor,
# not a target.
MIN_VIABLE_PER_CATEGORY = 4


def order_for_coverage(
    pending: list[AppSpec],
    completed_by_category: dict[str, int] | None = None,
    min_per_category: int = MIN_VIABLE_PER_CATEGORY,
) -> list[AppSpec]:
    """Round-robin the pending apps, poorest-covered category first.

    Ordering is deterministic: ties break on category name then app slug, so
    the same inputs always produce the same queue.
    """
    completed = dict(completed_by_category or {})
    by_cat: dict[str, list[AppSpec]] = defaultdict(list)
    for a in pending:
        by_cat[a.category].append(a)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda a: a.slug)

    counts = {cat: completed.get(cat, 0) for cat in by_cat}
    ordered: list[AppSpec] = []

    # Phase 1: lift every category to the floor, always serving the category
    # that is furthest behind.
    while True:
        candidates = [
            c for c, apps in by_cat.items()
            if apps and counts[c] < min_per_category
        ]
        if not candidates:
            break
        cat = min(candidates, key=lambda c: (counts[c], c))
        ordered.append(by_cat[cat].pop(0))
        counts[cat] += 1

    # Phase 2: everything else, still poorest-first so the tail stays level.
    while True:
        candidates = [c for c, apps in by_cat.items() if apps]
        if not candidates:
            break
        cat = min(candidates, key=lambda c: (counts[c], c))
        ordered.append(by_cat[cat].pop(0))
        counts[cat] += 1

    return ordered


def record_ordering(
    ordered: list[AppSpec],
    completed_by_category: dict[str, int],
    reason: str,
    min_per_category: int = MIN_VIABLE_PER_CATEGORY,
) -> dict:
    projected: dict[str, int] = dict(completed_by_category)
    for a in ordered:
        projected[a.category] = projected.get(a.category, 0) + 1

    payload = {
        "strategy": "category_coverage_round_robin",
        "reason": reason,
        "min_viable_per_category": min_per_category,
        "already_completed_by_category": dict(sorted(completed_by_category.items())),
        "queue_length": len(ordered),
        "queue": [{"slug": a.slug, "category": a.category} for a in ordered],
        "projected_if_all_complete": dict(sorted(projected.items())),
    }
    (settings.data_dir / "queue_order.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return payload
