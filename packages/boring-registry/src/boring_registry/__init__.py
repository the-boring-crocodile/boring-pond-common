"""Registrations kept alive by a heartbeat.

Soft state only: nothing is persisted. The registry (worker side) pings every
registered guest; the watchdog (guest side) registers again when the pings
stop. A worker restart therefore refills the registry within two intervals,
and start order does not matter.

No transport lives here. The caller proves the identity and supplies the
ping and register coroutines for its own protocol. Outcomes are data: a ping
answers True or False, a registration attempt returns the ack or None.

A guest may be pinged by several sources (for example one worker per Pond).
The ack can list them in "sources"; the watchdog then registers again when any
one of them goes silent. Without "sources" there is one implicit source. An exception from a coroutine is a failure; the registry
records it as a missed ping with the reason and keeps serving.

Each guest can have its own ping timeout (a remote one needs more than a
local one); it must leave room before the next round: 0 < timeout < interval.

One slow answer is not an outage: a guest whose only miss is a ping that
ran out of time stays available until the next round decides. A declined
ping, a transport failure or a second miss makes it unavailable at once.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

PING_INTERVAL_SECONDS = 3.0
# How long the Pond waits for one service call, and what a service keeps back
# for sending its answer: a service behind a Pond answers within the difference.
# A service not registered with a Pond has no such bound.
POND_CALL_SECONDS = 30.0
POND_CALL_MARGIN_SECONDS = 2.0
PING_TIMEOUT_SECONDS = 1.0
DROP_AFTER_MISSES = 10
# Heartbeat names are liveness hints, not authentication. Periodically repair
# registration using the guest's own credentialed transport even if pings continue.
RECONCILE_SECONDS = 60.0

Ping = Callable[[], Awaitable[bool]]
Register = Callable[[], Awaitable["dict[str, Any] | None"]]
Clock = Callable[[], float]


@dataclass
class Entry:
    identity: str
    ping: Ping = field(repr=False)
    details: dict[str, Any]
    registered: float
    last_answer: float
    misses: int = 0
    last_miss: str | None = None
    slow: bool = False  # the only miss so far ran out of time
    timeout: float | None = None  # this guest's ping timeout; None: the registry's

    @property
    def available(self) -> bool:
        return self.misses == 0 or (self.misses == 1 and self.slow)

    def view(self) -> dict[str, Any]:
        return {"identity": self.identity, "available": self.available,
                "misses": self.misses, "last_miss": self.last_miss, "timeout": self.timeout,
                "registered": self.registered, "last_answer": self.last_answer,
                "details": self.details}


class Registry:
    """Worker side: who is registered, and did they answer the last ping."""

    def __init__(self, clock: Clock = time.monotonic,
                 interval: float = PING_INTERVAL_SECONDS,
                 timeout: float = PING_TIMEOUT_SECONDS,
                 drop_after: int = DROP_AFTER_MISSES) -> None:
        self.clock = clock
        self.interval = interval
        self.timeout = timeout
        self.drop_after = drop_after
        self._entries: dict[str, Entry] = {}

    def register(self, identity: str, ping: Ping, details: dict[str, Any] | None = None,
                 timeout: float | None = None) -> dict[str, Any]:
        """Add or replace a guest, optionally with its own ping timeout. The ack
        tells the guest the ping interval."""
        if timeout is not None and not (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                                        and 0 < timeout < self.interval):
            raise ValueError(f"ping timeout must be a number of seconds, 0 < timeout < {self.interval:g}")
        now = self.clock()
        self._entries[identity] = Entry(identity, ping, dict(details or {}), now, now,
                                        timeout=None if timeout is None else float(timeout))
        return {"ping_interval_seconds": self.interval}

    def remove(self, identity: str) -> bool:
        """Forget a guest at once (its grant was withdrawn). Whether it was there."""
        return self._entries.pop(identity, None) is not None

    def get(self, identity: str) -> Entry | None:
        return self._entries.get(identity)

    def list(self) -> list[Entry]:
        return sorted(self._entries.values(), key=lambda entry: entry.identity)

    async def ping_all(self) -> None:
        """One heartbeat round: ping every guest concurrently, then settle."""
        entries = list(self._entries.values())
        answers = await asyncio.gather(*(self._ping(entry) for entry in entries))
        now = self.clock()
        for entry, miss in zip(entries, answers):
            if self._entries.get(entry.identity) is not entry:
                continue  # re-registered or dropped during the round
            reason, timed_out = miss
            if reason is None:
                entry.misses, entry.last_miss, entry.last_answer = 0, None, now
                entry.slow = False
                continue
            entry.slow = entry.misses == 0 and timed_out
            entry.misses += 1
            entry.last_miss = reason
            if entry.misses >= self.drop_after:
                del self._entries[entry.identity]

    async def _ping(self, entry: Entry) -> tuple[str | None, bool]:
        """(None, False) when the guest answered; otherwise why the ping
        missed, and whether it only ran out of time."""
        timeout = self.timeout if entry.timeout is None else entry.timeout
        try:
            answered = await asyncio.wait_for(entry.ping(), timeout)
        except asyncio.TimeoutError:
            return f"no answer within {timeout:g} s", True
        except Exception as error:  # a failure in the guest's transport
            return f"ping failed: {type(error).__name__}: {error}", False
        return (None, False) if answered else ("guest declined the ping", False)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.ping_all()


class Watchdog:
    """Register on silence and periodically; source names are only hints."""

    def __init__(self, register: Register, clock: Clock = time.monotonic) -> None:
        self.register = register
        self.clock = clock
        self.interval = PING_INTERVAL_SECONDS  # until an ack says otherwise
        self.registered = False
        self._attempt: float | None = None
        self._registered_at: float | None = None
        self._sources: dict[str, float] = {}  # expected source -> last contact

    def pinged(self, source: str = "") -> None:
        """Call from the guest's ping handler. Unknown sources are ignored."""
        if source in self._sources:
            self._sources[source] = self.clock()

    def due(self) -> bool:
        now = self.clock()
        if not self.registered:
            return self._attempt is None or now - self._attempt >= 2 * self.interval
        return (self._registered_at is None
                or now - self._registered_at >= max(RECONCILE_SECONDS, 2 * self.interval)
                or any(now - last >= 2 * self.interval for last in self._sources.values()))

    async def check(self) -> bool:
        """Register if due. Returns whether the guest is registered now."""
        if not self.due():
            return self.registered
        now = self._attempt = self.clock()  # one attempt per two intervals
        ack = await self.register()
        self.registered = ack is not None
        if ack is not None:
            self._registered_at = now
            self.interval = float(ack["ping_interval_seconds"])
            self._sources = {str(source): now for source in ack.get("sources", [""])}
        return self.registered

    async def run(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(self.interval)
