"""One deadline carried through a request, and the one margin between layers.

A request carries its budget in seconds (relative, so no clock is compared
between processes). The receiver's deadline is its arrival time plus that
budget, capped by its own limit for the operation; everything inside uses what
is left. An outer layer waits for an inner one its budget plus
REPLY_MARGIN_SECONDS: the time for the answer to travel back and for owned
cleanup to report. Cleanup reserves inside one owner (the executor's) are that
owner's own and stay there.
"""
from __future__ import annotations

import asyncio
import math

from boring_registry import POND_CALL_SECONDS

REPLY_MARGIN_SECONDS = 5.0
# A service operation: the service's own call (POND_CALL_SECONDS, the service
# contract) plus reading file references before it and save_to after it.
SERVICE_OPERATION_SECONDS = POND_CALL_SECONDS + 15.0


def budget(value: object, cap: float) -> float:
    """A carried budget, capped by the receiver's own limit. Absent or not a number:
    the cap. Zero or negative: 0, no time left, which every limit refuses before
    anything is sent (never widened to the cap)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return cap
    try:
        number = float(value)
    except OverflowError:  # an integer beyond float range (JSON keeps it exact)
        number = math.inf if value > 0 else -math.inf
    if math.isnan(number):
        return cap
    return min(max(number, 0.0), cap)


def deadline_after(seconds: float) -> float:
    """The event-loop time `seconds` from now."""
    return asyncio.get_running_loop().time() + seconds


def left(deadline: float | None) -> float | None:
    """Seconds until the deadline (never negative), or None without one."""
    if deadline is None:
        return None
    return max(0.0, deadline - asyncio.get_running_loop().time())
