"""Restart startup holds the real listener rather than probing and rebinding."""
import asyncio
import errno
import logging
import socket
import time

import pytest

from boring_service_kit import pond


def held_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


def test_delayed_release_starts_on_the_same_port(caplog):
    async def scenario():
        old = held_port()
        port = old.getsockname()[1]
        async def release():
            await asyncio.sleep(0.1)
            old.close()
        task = asyncio.create_task(release())
        try:
            with caplog.at_level(logging.INFO):
                listener = await pond.bind_socket("127.0.0.1", port, timeout=2)
            try:
                assert listener.getsockname() == ("127.0.0.1", port)
                assert "waiting up to" in caplog.text and "is available; starting" in caplog.text
            finally:
                listener.close()
        finally:
            old.close()
            await task
    asyncio.run(scenario())


def test_occupied_port_has_a_bounded_failure(caplog):
    async def scenario():
        with held_port() as old:
            started = time.monotonic()
            with pytest.raises(OSError) as raised:
                await pond.bind_socket("127.0.0.1", old.getsockname()[1], timeout=0.05)
            assert raised.value.errno in (errno.EADDRINUSE, 10048)
            assert time.monotonic() - started < 2
            assert "still in use" in caplog.text
    asyncio.run(scenario())


def test_unrelated_bind_failure_is_not_retried(monkeypatch):
    attempts = []
    class DeniedSocket:
        def setsockopt(self, *args): pass
        def bind(self, address):
            attempts.append(address)
            raise PermissionError(errno.EACCES, "synthetic denial")
        def close(self): pass
    async def scenario():
        monkeypatch.setattr(pond.socket, "socket", lambda *args: DeniedSocket())
        with pytest.raises(PermissionError):
            await pond.bind_socket("127.0.0.1", 0, timeout=20)
    asyncio.run(scenario())
    assert attempts == [("127.0.0.1", 0)]


def test_run_serves_http_after_the_old_listener_releases(monkeypatch):
    servers = []
    original = pond.uvicorn.Server
    class ObservedServer(original):
        def __init__(self, config):
            super().__init__(config)
            servers.append(self)
    monkeypatch.setattr(pond.uvicorn, "Server", ObservedServer)
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-length", b"5")]})
            await send({"type": "http.response.body", "body": b"ready"})
    async def scenario():
        old = held_port()
        port = old.getsockname()[1]
        serving = asyncio.create_task(pond.run(app, None, "127.0.0.1", port))
        try:
            await asyncio.sleep(0.1)
            assert not serving.done() and not servers
            old.close()
            while not servers or not servers[0].started:
                await asyncio.sleep(0.01)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                await writer.drain()
                response = await reader.read()
                assert b"200 OK" in response and response.endswith(b"ready")
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            old.close()
            for server in servers:
                server.should_exit = True
            if not servers:
                serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
    async def bounded():
        await asyncio.wait_for(scenario(), timeout=5)
    asyncio.run(bounded())


def test_run_passes_the_reserved_socket_and_closes_it(monkeypatch):
    captured = []
    class Server:
        def __init__(self, config): pass
        async def serve(self, sockets):
            captured.extend(sockets)
            assert sockets[0].getsockname()[1] > 0
    monkeypatch.setattr(pond.uvicorn, "Server", Server)
    asyncio.run(pond.run(object(), None, "127.0.0.1", 0))
    assert len(captured) == 1 and captured[0].fileno() == -1
