from __future__ import annotations

import asyncio
import json
import struct
import socket
from typing import Any

from .protocol_json import decode_object

MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_COMMAND_BYTES = 64 * 1024
# JSON can expand each byte to six ASCII characters. Reserve room for cwd
# and receipt metadata as well as both output streams. The command-sized
# reserve once held the echoed command; it stays as headroom (cap unchanged).
MAX_OUTPUT_BYTES = (MAX_FRAME_BYTES - 6 * MAX_COMMAND_BYTES - 64 * 1024) // 12


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if size > MAX_FRAME_BYTES:
        raise ValueError("frame exceeds the protocol limit")
    payload = await reader.readexactly(size)
    return decode_object(payload)


async def write_frame(
    writer: asyncio.StreamWriter,
    value: dict[str, Any],
) -> None:
    payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("frame exceeds the protocol limit")
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()


def peer_uid(writer: asyncio.StreamWriter) -> int:
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None:
        raise PermissionError("peer credentials unavailable")
    credentials = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                               struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", credentials)
    return uid
