"""Canonical service address and heartbeat admission."""
import re
import ipaddress
from urllib.parse import urlsplit
from boring_registry import PING_INTERVAL_SECONDS

SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")


def service_url(value: str) -> str:
    """An absolute http(s) URL with a host and nothing a token audience could vary by;
    plain http only on this host, because call tokens travel with every request."""
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ValueError("service URL must be an absolute http(s) URL without credentials, "
                         "query or fragment")
    if parts.scheme == "http" and not _loopback(parts.hostname):
        raise ValueError("a service URL off this host must be https (call tokens travel with "
                         "every request); plain http only for loopback")
    return value


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


# At most this, so a slow answer still leaves time before the next round.
MAX_PING_TIMEOUT_SECONDS = PING_INTERVAL_SECONDS - 0.5


def service_ping_timeout(value: object) -> float:
    """Seconds a worker waits for this service's heartbeat: more than 0, at most 2.5 (interval 3)."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 < value <= MAX_PING_TIMEOUT_SECONDS):
        raise ValueError(f"ping timeout must be a number of seconds, more than 0 and at most "
                         f"{MAX_PING_TIMEOUT_SECONDS:g} (the ping interval is {PING_INTERVAL_SECONDS:g} s)")
    return float(value)

