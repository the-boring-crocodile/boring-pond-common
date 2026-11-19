"""Authority selectors cross the verified Unix transport without changing token calls."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import tempfile
import unittest

from boring_worker_kit.local_authority import LocalAuthority


class LocalAuthorityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The client refuses a socket below writable /tmp; the home exists on every runner.
        self.temp = tempfile.TemporaryDirectory(dir=Path.home(), prefix="authority-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "authority.sock"
        self.requests = []
        async def handle(reader, writer):
            try:
                request = json.loads(await reader.readline())
                self.requests.append(request)
                writer.write((json.dumps({"ok": True, "result": request}) + "\n").encode())
                await writer.drain()
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
        self.server = await asyncio.start_unix_server(handle, path=self.path)
        self.client = LocalAuthority(self.path, os.getuid())

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_pond_selector_and_existing_service_selectors_are_distinct(self):
        expected = {"operation": "registration", "pond": "garden"}
        self.assertEqual(await self.client.request("registration", pond="garden"), expected)
        self.assertEqual(await self.client.request("list"), {"operation": "list"})
        self.assertEqual(await self.client.request("token", "knowledge"),
                         {"operation": "token", "service": "knowledge"})
        self.assertEqual(self.requests[0], expected)

    async def test_ambiguous_selector_refuses_without_sending(self):
        with self.assertRaisesRegex(ValueError, "select a service or a Pond"):
            await self.client.request("registration", "knowledge", pond="garden")
        self.assertEqual(self.requests, [])
