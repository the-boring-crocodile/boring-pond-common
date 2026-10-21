"""Ownership remains with admitted work through cancellation and inner failure."""
import asyncio
import unittest

from boring_worker_kit.settlement import settled


class SettlementTest(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_waits_and_observes_inner_failure(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def operation():
            started.set()
            await release.wait()
            raise OSError("synthetic write failure")
        inner = asyncio.create_task(operation())
        owner = asyncio.create_task(settled(inner))
        await started.wait()
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        self.assertFalse(owner.done())
        self.assertFalse(inner.cancelled())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertIsInstance(inner.exception(), OSError)

    async def test_cancelled_success_reports_the_actual_result(self):
        started, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def operation():
            started.set()
            await release.wait()
            return {"written": True}
        owner = asyncio.create_task(settled(operation(), seen.append))
        await started.wait()
        owner.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertEqual(seen, [{"written": True}])
