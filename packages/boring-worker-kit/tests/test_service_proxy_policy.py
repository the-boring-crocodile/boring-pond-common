"""Bearer service routing must not inherit ambient proxy configuration."""
import asyncio
from boring_worker_kit.services import Services

def test_service_client_ignores_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    async def scenario():
        services = Services("fixture")
        try:
            assert not services.http._mounts, "ambient proxy installed in bearer transport"
        finally:
            await services.close()
    asyncio.run(scenario())
