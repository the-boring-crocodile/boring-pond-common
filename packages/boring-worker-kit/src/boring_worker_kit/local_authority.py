"""Bounded UID-authenticated service authority transport; no credential cache."""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import stat
from typing import Any, cast
from contextlib import suppress
from .outcomes import Refusal
from .framing import peer_uid as peer_uid
from .protocol_json import decode_object

MAX_AUTHORITY_FRAME = 65536

def protected_path(path: Path, owner: int) -> None:
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise ValueError("authority socket must be a normalized absolute path")
    for ancestor in path.parents:
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, owner) or info.st_mode & 0o022:
            raise PermissionError("authority socket ancestry is not controlled by its owner/root")

def encode(value: dict[str, Any]) -> bytes:
    data = (json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_AUTHORITY_FRAME:
        raise ValueError("authority response exceeds limit")
    return data

class LocalAuthority:
    def __init__(self, path: Path, uid: int):
        if type(uid) is not int or not 0 < uid < 2**32-1:
            raise ValueError("authority UID must be positive")
        self.path, self.uid = path, uid

    async def request(self, operation: str, service: str | None = None) -> dict[str, Any]:
        protected_path(self.path, self.uid)
        info = self.path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
            raise PermissionError("authority socket has the wrong owner or type")
        async def exchange() -> dict[str, Any]:
            reader, writer = await asyncio.open_unix_connection(self.path, limit=MAX_AUTHORITY_FRAME)
            try:
                if peer_uid(writer) != self.uid:
                    raise PermissionError("authority peer UID mismatch")
                request = {"operation": operation}
                if service is not None:
                    request["service"] = service
                writer.write(encode(request)); await writer.drain()
                response = decode_object(await reader.readline())
                if response.get("ok") is not True:
                    error = response.get("error")
                    if isinstance(error, dict):
                        raise Refusal(error["code"], error["message"])
                    raise RuntimeError("local authority failed")
                return cast(dict[str, Any], response["result"])
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
        return await asyncio.wait_for(exchange(), 2.0)
