import asyncio
import unittest

from boring_registry import DROP_AFTER_MISSES, Registry, Watchdog


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def answering(answer: bool = True):
    async def ping() -> bool:
        return answer
    return ping


def raising():
    async def ping() -> bool:
        raise ConnectionRefusedError("socket gone")
    return ping


def hanging():
    async def ping() -> bool:
        await asyncio.sleep(5)
        return True
    return ping


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.registry = Registry(clock=self.clock, timeout=0.05)

    def round(self) -> None:
        asyncio.run(self.registry.ping_all())

    def test_register_acknowledges_the_interval(self) -> None:
        ack = self.registry.register("parrot", answering(), {"socket": "/run/parrot.sock"})
        self.assertEqual(ack, {"ping_interval_seconds": 3.0})
        view = self.registry.get("parrot").view()
        self.assertTrue(view["available"])
        self.assertEqual(view["details"], {"socket": "/run/parrot.sock"})

    def test_remove_forgets_the_guest_at_once(self) -> None:
        pings = []

        async def ping() -> bool:
            pings.append("parrot")
            return True
        self.registry.register("parrot", ping)
        self.assertTrue(self.registry.remove("parrot"))
        self.assertIsNone(self.registry.get("parrot"))
        self.assertEqual(self.registry.list(), [])
        self.clock.now += 3
        self.round()
        self.assertEqual(pings, [])  # a removed guest is no longer pinged
        self.assertFalse(self.registry.remove("parrot"))
        self.assertEqual(self.registry.list(), [])

    def test_answer_keeps_guest_available(self) -> None:
        self.registry.register("parrot", answering())
        self.clock.now += 3
        self.round()
        entry = self.registry.get("parrot")
        self.assertTrue(entry.available)
        self.assertEqual(entry.last_answer, 103.0)

    def test_first_miss_marks_unavailable_and_answer_restores(self) -> None:
        answers = [False, True]

        async def ping() -> bool:
            return answers.pop(0)
        self.registry.register("parrot", ping)
        self.round()
        entry = self.registry.get("parrot")
        self.assertFalse(entry.available)
        self.assertEqual(entry.last_miss, "guest declined the ping")
        self.round()
        self.assertTrue(entry.available)
        self.assertIsNone(entry.last_miss)

    def test_timeout_and_failure_are_misses_with_reasons(self) -> None:
        self.registry.register("slow", hanging())
        self.registry.register("broken", raising())
        self.registry.register("fine", answering())
        self.round()
        self.assertEqual(self.registry.get("slow").last_miss, "no answer within 0.05 s")
        self.assertEqual(self.registry.get("broken").last_miss,
                         "ping failed: ConnectionRefusedError: socket gone")
        self.assertTrue(self.registry.get("fine").available)

    def test_one_slow_ping_keeps_available_two_do_not(self) -> None:
        self.registry.register("slow", hanging())
        self.round()
        entry = self.registry.get("slow")
        self.assertEqual(entry.misses, 1)
        self.assertTrue(entry.available)
        self.round()
        self.assertFalse(entry.available)

    def test_failure_then_slow_stays_unavailable(self) -> None:
        pings = [raising(), hanging()]

        async def ping() -> bool:
            return await pings.pop(0)()
        self.registry.register("parrot", ping)
        self.round()
        self.round()
        self.assertFalse(self.registry.get("parrot").available)

    def test_slow_then_answer_resets(self) -> None:
        pings = [hanging(), answering()]

        async def ping() -> bool:
            return await pings.pop(0)()
        self.registry.register("parrot", ping)
        self.round()
        self.round()
        entry = self.registry.get("parrot")
        self.assertTrue(entry.available)
        self.assertEqual((entry.misses, entry.slow), (0, False))

    def test_a_guest_can_have_its_own_timeout(self) -> None:
        async def slow() -> bool:
            await asyncio.sleep(0.1)
            return True

        self.registry.register("local", slow)  # the registry's 0.05 s
        self.registry.register("remote", slow, timeout=0.5)
        self.round()
        self.assertEqual(self.registry.get("local").last_miss, "no answer within 0.05 s")
        self.assertEqual(self.registry.get("remote").misses, 0)
        self.assertEqual(self.registry.get("remote").view()["timeout"], 0.5)

    def test_a_timeout_must_leave_room_before_the_next_round(self) -> None:
        for bad in (0, -1, 3.0, 5, True, "2"):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                self.registry.register("parrot", answering(), timeout=bad)
        self.assertIsNone(self.registry.get("parrot"))

    def test_dropped_after_ten_misses(self) -> None:
        self.registry.register("parrot", answering(False))
        for _ in range(DROP_AFTER_MISSES - 1):
            self.round()
        self.assertEqual(self.registry.get("parrot").misses, DROP_AFTER_MISSES - 1)
        self.round()
        self.assertIsNone(self.registry.get("parrot"))
        self.assertEqual(self.registry.list(), [])

    def test_reregistration_replaces_and_resets(self) -> None:
        self.registry.register("parrot", answering(False))
        self.round()
        self.registry.register("parrot", answering(), {"generation": 2})
        entry = self.registry.get("parrot")
        self.assertTrue(entry.available)
        self.assertEqual(entry.details, {"generation": 2})
        self.assertEqual(len(self.registry.list()), 1)

    def test_reregistration_during_a_round_is_not_overwritten(self) -> None:
        async def scenario() -> None:
            gate = asyncio.Event()

            async def old_ping() -> bool:
                await gate.wait()
                return False
            self.registry.register("parrot", old_ping)
            round_task = asyncio.create_task(self.registry.ping_all())
            await asyncio.sleep(0)
            self.registry.register("parrot", answering())
            gate.set()
            await round_task
        asyncio.run(scenario())
        self.assertTrue(self.registry.get("parrot").available)


class WatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.attempts = 0
        self.ack: dict | None = {"ping_interval_seconds": 3.0}

        async def register() -> dict | None:
            self.attempts += 1
            return self.ack
        self.watchdog = Watchdog(register, clock=self.clock)

    def check(self) -> bool:
        return asyncio.run(self.watchdog.check())

    def test_registers_at_start(self) -> None:
        self.assertTrue(self.check())
        self.assertEqual(self.attempts, 1)

    def test_spoofed_known_name_cannot_suppress_credentialed_reconciliation(self) -> None:
        self.check()
        # Valid anonymous discovery can claim the expected name after the real
        # registry restarted. It must not postpone credentialed repair forever.
        for _ in range(20):
            self.clock.now += 3
            self.watchdog.pinged()
            self.check()
        self.assertEqual(self.attempts, 2)

    def test_pings_keep_it_quiet(self) -> None:
        self.check()
        for _ in range(5):
            self.clock.now += 3
            self.watchdog.pinged()
            self.check()
        self.assertEqual(self.attempts, 1)

    def test_registers_again_after_two_silent_intervals(self) -> None:
        self.check()
        self.clock.now += 5.9
        self.check()
        self.assertEqual(self.attempts, 1)
        self.clock.now += 0.1
        self.check()
        self.assertEqual(self.attempts, 2)

    def test_absent_worker_is_retried_every_two_intervals(self) -> None:
        self.ack = None
        self.assertFalse(self.check())
        self.assertFalse(self.check())
        self.assertEqual(self.attempts, 1)
        self.clock.now += 6
        self.ack = {"ping_interval_seconds": 3.0}
        self.assertTrue(self.check())
        self.assertEqual(self.attempts, 2)

    def test_adopts_the_acknowledged_interval(self) -> None:
        self.ack = {"ping_interval_seconds": 10.0}
        self.check()
        self.clock.now += 19
        self.check()
        self.assertEqual(self.attempts, 1)
        self.clock.now += 1
        self.check()
        self.assertEqual(self.attempts, 2)


class SourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.attempts = 0

        async def register() -> dict | None:
            self.attempts += 1
            return {"ping_interval_seconds": 3.0, "sources": ["pond", "garden"]}
        self.watchdog = Watchdog(register, clock=self.clock)
        asyncio.run(self.watchdog.check())

    def tick(self, *sources: str) -> None:
        self.clock.now += 3
        for source in sources:
            self.watchdog.pinged(source)
        asyncio.run(self.watchdog.check())

    def test_all_sources_pinging_keeps_it_quiet(self) -> None:
        for _ in range(5):
            self.tick("pond", "garden")
        self.assertEqual(self.attempts, 1)

    def test_one_silent_source_triggers_registration(self) -> None:
        self.tick("pond", "garden")
        self.tick("garden")  # pond restarted and forgot the guest
        self.assertEqual(self.attempts, 1)
        self.tick("garden")
        self.assertEqual(self.attempts, 2)
        self.tick("pond", "garden")
        self.tick("pond", "garden")
        self.assertEqual(self.attempts, 2)

    def test_unknown_source_does_not_count(self) -> None:
        self.tick("pond", "intruder")
        self.tick("pond", "intruder")
        self.assertEqual(self.attempts, 2)  # garden stayed silent


class RestartTest(unittest.TestCase):
    def test_worker_restart_refills_from_the_guest(self) -> None:
        clock = Clock()
        worker = [Registry(clock=clock)]

        async def ping() -> bool:
            watchdog.pinged()
            return True

        async def register() -> dict | None:
            return worker[0].register("parrot", ping)
        watchdog = Watchdog(register, clock=clock)

        async def scenario() -> None:
            await watchdog.check()
            for _ in range(3):
                clock.now += 3
                await worker[0].ping_all()
                await watchdog.check()
            worker[0] = Registry(clock=clock)  # restart: memory is gone
            for _ in range(2):
                clock.now += 3
                await worker[0].ping_all()
                await watchdog.check()
        asyncio.run(scenario())
        self.assertTrue(worker[0].get("parrot").available)


if __name__ == "__main__":
    unittest.main()
