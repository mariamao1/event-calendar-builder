from __future__ import annotations

from collections.abc import Callable, Hashable
from contextlib import AbstractContextManager
from typing import Any

from .errors import ApiError


def run_each(
    keys: list[Any],
    savepoint: Callable[[], AbstractContextManager[None]],
    fn: Callable[[Any], str],
    *,
    event_id: Callable[[Any], Any] = lambda key: key,
    dedupe_key: Callable[[Any], Hashable] = lambda key: key,
) -> dict:
    """Run `fn` once per distinct key, each inside its own savepoint.

    An `ApiError` rolls back only that item and is reported in its result;
    any other exception fails the whole request. `fn` returns the item's
    success status (for example "approved"), which is also counted.
    """
    seen: set[Hashable] = set()
    results: list[dict] = []
    counts: dict[str, int] = {}
    for key in keys:
        marker = dedupe_key(key)
        if marker in seen:
            continue
        seen.add(marker)
        result: dict[str, Any] = {"event_id": str(event_id(key))}
        try:
            with savepoint():
                status = fn(key)
        except ApiError as error:
            result.update(
                ok=False,
                status="failed",
                error={"code": error.code, "message": error.message},
            )
        else:
            result.update(ok=True, status=status)
            counts[status] = counts.get(status, 0) + 1
        results.append(result)
    counts["failed"] = sum(1 for result in results if not result["ok"])
    return {"results": results, "counts": counts}
