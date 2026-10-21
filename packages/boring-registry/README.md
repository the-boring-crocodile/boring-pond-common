# boring-registry

Registrations kept alive by a heartbeat. Python >= 3.10, no dependencies. One module: `src/boring_registry/__init__.py`.

Soft state only. Nothing is persisted: the registry lives in the worker's
memory, and after a worker restart guests register again within about two
intervals, so start order does not matter. No transport lives here either:
the caller proves identity and supplies the `ping` and `register` coroutines
for its own protocol. Outcomes are data: a ping answers `True` or `False`, a
registration attempt returns the ack or `None`. An exception from a coroutine
counts as a missed ping with its reason; the registry keeps serving.

Used by boring-crocodile-mcp (worker side, for Pirates over `register.sock`
and for services over MCP `server/discover`) and by the canvas service and
boring-pirates (guest side).

Developed here only; every consumer installs its Pond Common release wheel.

## Registry (worker side)

- `Registry(clock=time.monotonic, interval=3.0, timeout=1.0, drop_after=10)`.
- `register(identity, ping, details=None, timeout=None)` adds or replaces an
  entry, optionally with its own ping timeout (0 < timeout < interval; a remote
  service needs more than a local one), and
  returns the ack `{"ping_interval_seconds": 3.0}`. An ack may also list
  `"sources"`; the MCP control plane's answer to a service registration does
  (the granted Ponds).
- `remove(identity)` forgets an entry at once and says whether it was there
  (for a withdrawn grant, which no ping would notice).
- `get(identity)`, `list()`: `Entry` objects; `Entry.view()` gives
  `identity`, `available`, `misses`, `last_miss`, `registered`,
  `last_answer`, `details`.
- `ping_all()`: one round; pings every entry concurrently with the timeout.
  An answer resets `misses`; a miss records why in `last_miss` (declined,
  no answer within the timeout, or the transport's exception) and makes the
  entry unavailable -- except a first miss that only ran out of time: one
  slow answer keeps the entry available until the next round (0.2.1); after `drop_after` misses the entry is dropped. A
  re-registration during a round is not overwritten by the old entry's result.
- `run()`: `ping_all()` every interval, forever.

## Watchdog (guest side)

- `Watchdog(register, clock=time.monotonic)`; `register` is a coroutine
  returning the ack or `None` (worker absent or refused).
- `pinged(source="")`: call from the guest's ping handler; unknown sources
  are ignored.
- `check()`: registers when due: not yet registered (one attempt per two
  intervals), or any source silent for two intervals. Adopts the acked
  interval.
- `run()`: `check()` every interval, forever.

**Sources.** A guest may be pinged by several sources, for example one worker
per Pond. If the ack lists `"sources"`, the watchdog expects a ping from each
and registers again when any one is silent. Without `"sources"` there is one
implicit source (`""`).

## Tests

`tests/test_registry.py` runs on a fake clock (worker restart refilled by the
guest, re-registration during a round, sources). The module is in the -mcp
strict mypy gate.
