"""Wait for an admitted operation before releasing its owning task."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")


async def settled(operation: Awaitable[_T],
                  on_cancelled_result: Callable[[_T], None] | None = None) -> _T:
    """Defer cancellation until work settles; never cancel its inner owner.

    Use only where the caller owns cleanup (jobs/assignments). A bounded
    foreground request must instead report an unknown outcome on disconnect.
    """
    work = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        # Retrieve an inner failure even when cancellation remains the outcome.
        if not work.cancelled() and work.exception() is None:
            if on_cancelled_result is not None:
                on_cancelled_result(work.result())
        raise
