"""Service access keys: access without an OAuth authority.

An administrator grants a person a key for one service instance. The key is
shown once; the key file stores only its SHA-256 fingerprint with the person
(subject), role, expiry and a label. The service re-reads the file when it
changes, so adding, renewing and removing keys take effect at the next call.

A key produces the same verified caller as OAuth introspection --
(issuer, subject, author, role) with the file's own issuer such as
``key:knowledge`` -- so reader/writer checks, attribution and ownership
work unchanged. Keys are accepted over HTTPS, or plain HTTP on loopback only
(enforced by ServiceConfig).

    boring-service-keys add --file keys.json --issuer key:knowledge \\
        --subject robin@devbox --role writer --days 90 --label "Robin, Visual Studio on the dev box"
    boring-service-keys list --file keys.json
    boring-service-keys renew --file keys.json --id k3f9a1c2 --days 90
    boring-service-keys remove --file keys.json --id k3f9a1c2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
import time
import stat
from functools import wraps
from .filelock import file_lock
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mcp.server.auth.provider import AccessToken

from .access import Caller

log = logging.getLogger("boring_service_kit.keys")

PREFIX = "bsk_"
DAY = 86400
DEFAULT_DAYS = 90


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("times are ISO 8601 UTC strings like 2027-01-06T00:00:00Z")
    return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


@dataclass(frozen=True)
class Key:
    id: str
    sha256: str
    subject: str
    role: str
    expires: int
    created: int
    label: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "sha256": self.sha256, "subject": self.subject, "role": self.role,
                "expires": _iso(self.expires), "created": _iso(self.created), "label": self.label}

    @classmethod
    def from_json(cls, data: Any) -> Key:
        if not isinstance(data, dict) or set(data) - {"id", "sha256", "subject", "role", "expires", "created", "label"}:
            raise ValueError("a key has id, sha256, subject, role, expires, created and label")
        key = cls(id=data.get("id"), sha256=data.get("sha256"), subject=data.get("subject"),
                  role=data.get("role"), expires=_epoch(data.get("expires")),
                  created=_epoch(data.get("created")), label=data.get("label", ""))
        if not isinstance(key.id, str) or not key.id:
            raise ValueError("key id must be nonempty")
        if not isinstance(key.sha256, str) or len(key.sha256) != 64 or set(key.sha256) - set("0123456789abcdef"):
            raise ValueError(f"key {key.id}: sha256 must be 64 lowercase hex digits")
        if not isinstance(key.subject, str) or not key.subject:
            raise ValueError(f"key {key.id}: subject must be nonempty")
        if key.role not in {"reader", "writer"}:
            raise ValueError(f"key {key.id}: role must be reader or writer")
        if not isinstance(key.label, str):
            raise ValueError(f"key {key.id}: label must be text")
        return key


@dataclass(frozen=True)
class KeyFile:
    """The key file: an issuer and its keys. Only fingerprints, never keys."""
    issuer: str
    keys: tuple[Key, ...]

    @classmethod
    def load(cls, path: Path) -> KeyFile:
        return cls.parse(path.read_text(encoding="utf-8"))

    @classmethod
    def parse(cls, text: str) -> KeyFile:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != {"issuer", "keys"}:
            raise ValueError("a key file has exactly issuer and keys")
        issuer = data["issuer"]
        if not isinstance(issuer, str) or not issuer.startswith("key:") or len(issuer) < 5:
            raise ValueError("issuer must look like key:<instance>")
        if not isinstance(data["keys"], list):
            raise ValueError("keys must be a list")
        keys = tuple(Key.from_json(k) for k in data["keys"])
        ids = [k.id for k in keys]
        if len(set(ids)) != len(ids) or len({k.sha256 for k in keys}) != len(keys):
            raise ValueError("key ids and fingerprints must be unique")
        return cls(issuer, keys)

    def save(self, path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                                      or getattr(metadata, "st_file_attributes", 0) & 0x400):
            raise ValueError("key file must be a regular single-link non-reparse file")
        text = json.dumps({"issuer": self.issuer, "keys": [k.as_json() for k in self.keys]}, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".keys-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                if metadata is not None:
                    if os.name == "nt":
                        _matching_windows_security(path, Path(tmp))
                    else:
                        os.fchown(handle.fileno(), metadata.st_uid, metadata.st_gid)
                        os.fchmod(handle.fileno(), stat.S_IMODE(metadata.st_mode))
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            if os.name != "nt":
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _windows_security_text(path: Path) -> str:
    """Owner/group and ordered DACL, independent of binary descriptor layout."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    get = advapi.GetFileSecurityW
    get.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    get.restype = wintypes.BOOL
    size = wintypes.DWORD()
    get(str(path), 7, None, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(size.value)
    if not get(str(path), 7, buffer, size.value, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    output = ctypes.c_void_p()
    if not convert(buffer, 1, 7, ctypes.byref(output), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.wstring_at(output.value)
    finally:
        free(output)


def _matching_windows_security(target: Path, replacement: Path) -> None:
    """Refuse changed access/inheritance rather than repairing security after rename.

    SDDL keeps owner/group, ACE order and inheritance/protection flags. It ignores
    descriptor offsets/padding and defaulted provenance bits; this is deliberately
    stricter than comparing just the effective grants of the current user.
    """
    if _windows_security_text(target) != _windows_security_text(replacement):
        raise ValueError("key-file security differs from its private parent inheritance; update refused")
    mask = ~(0x20 | 0x80)  # archive/normal may change on a content update
    if target.stat().st_file_attributes & mask != replacement.stat().st_file_attributes & mask:
        raise ValueError("key file has unsupported Windows attributes; update refused")


def _serialized_update(function):
    @wraps(function)
    def call(path, *args, **kwargs):
        path = Path(path)
        with file_lock(path.with_name("." + path.name + ".lock")):
            return function(path, *args, **kwargs)
    return call


class KeyVerifier:
    """SDK TokenVerifier for keys: fingerprint lookup in a file re-read when it changes.

    The file is read on every key call and parsed again when its content differs:
    a few kilobytes, and no timestamp or inode granularity can hide a removal.

    An unreadable or invalid file accepts nothing (fail closed) and says so in the log.
    """

    def __init__(self, path: Path, own_url: str, scopes: list[str] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.own_url = own_url
        self.scopes = list(scopes or [])
        self.clock = clock
        self._seen: str | None = None
        self._file: KeyFile | None = None
        self._by_fingerprint: dict[str, Key] = {}

    def _refresh(self) -> None:
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            if self._seen != "unreadable":
                log.error("key file %s unreadable (%s): no key is accepted", self.path, type(exc).__name__)
            self._seen, self._file, self._by_fingerprint = "unreadable", None, {}
            return
        seen = hashlib.sha256(raw).hexdigest()
        if seen == self._seen:
            return
        self._seen = seen
        try:
            self._file = KeyFile.parse(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            log.error("key file %s invalid (%s): no key is accepted", self.path, exc)
            self._file, self._by_fingerprint = None, {}
            return
        self._by_fingerprint = {k.sha256: k for k in self._file.keys}

    def caller(self, token: str) -> tuple[Key, Caller] | None:
        if not token.startswith(PREFIX):
            return None
        self._refresh()
        key = self._by_fingerprint.get(fingerprint(token))
        if key is None or self._file is None or key.expires <= self.clock():
            return None
        issuer = self._file.issuer
        author = json.dumps([issuer, key.subject], separators=(",", ":"))
        return key, Caller(issuer, key.subject, author, key.role)

    async def verify_token(self, token: str) -> AccessToken | None:
        found = self.caller(token)
        if found is None:
            return None
        key, caller = found
        return AccessToken(token=token, client_id=f"key:{key.id}", scopes=list(self.scopes),
                           expires_at=key.expires, resource=self.own_url, subject=caller.subject,
                           claims={"boring_caller": asdict(caller)})


class EitherVerifier:
    """Keys first (recognised by their prefix), then OAuth introspection when configured."""

    def __init__(self, keys: KeyVerifier | None, introspection: Any | None) -> None:
        if keys is None and introspection is None:
            raise ValueError("configure oauth, keys or both")
        self.keys = keys
        self.introspection = introspection

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.keys is not None and token.startswith(PREFIX):
            return await self.keys.verify_token(token)
        if self.introspection is not None:
            return await self.introspection.verify_token(token)
        return None


# ---------------------------------------------------------------- administration

def _load_or_new(path: Path, issuer: str | None) -> KeyFile:
    if path.exists():
        current = KeyFile.load(path)
        if issuer is not None and issuer != current.issuer:
            raise SystemExit(f"{path} belongs to {current.issuer}, not {issuer}")
        return current
    if issuer is None:
        raise SystemExit(f"{path} does not exist yet: name its issuer with --issuer key:<instance>")
    if not issuer.startswith("key:") or len(issuer) < 5:
        raise SystemExit("issuer must look like key:<instance>")
    return KeyFile(issuer, ())


@_serialized_update
def add(path: Path, subject: str, role: str, days: int, label: str = "", issuer: str | None = None,
        now: float | None = None) -> tuple[Key, str]:
    if days < 1:
        raise SystemExit("--days must be at least 1")
    current = _load_or_new(path, issuer)
    created = int(now if now is not None else time.time())
    secret = new_key()
    used = {k.id for k in current.keys}
    key_id = next(i for i in (f"k{secrets.token_hex(4)}" for _ in range(100)) if i not in used)
    key = Key(key_id, fingerprint(secret), subject, role, created + days * DAY, created, label)
    Key.from_json(key.as_json())  # the same checks as loading
    KeyFile(current.issuer, current.keys + (key,)).save(path)
    return key, secret


@_serialized_update
def remove(path: Path, key_id: str) -> Key:
    current = KeyFile.load(path)
    found = [k for k in current.keys if k.id == key_id]
    if not found:
        raise SystemExit(f"no key {key_id} in {path}")
    KeyFile(current.issuer, tuple(k for k in current.keys if k.id != key_id)).save(path)
    return found[0]


@_serialized_update
def renew(path: Path, key_id: str, days: int, now: float | None = None) -> Key:
    if days < 1:
        raise SystemExit("--days must be at least 1")
    current = KeyFile.load(path)
    if key_id not in {k.id for k in current.keys}:
        raise SystemExit(f"no key {key_id} in {path}")
    expires = int(now if now is not None else time.time()) + days * DAY
    keys = tuple(Key(**{**asdict(k), "expires": expires}) if k.id == key_id else k for k in current.keys)
    KeyFile(current.issuer, keys).save(path)
    return next(k for k in keys if k.id == key_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boring-service-keys", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "list", "remove", "renew"):
        sub = commands.add_parser(name)
        sub.add_argument("--file", type=Path, required=True, help="the service's key file")
        if name == "add":
            sub.add_argument("--issuer", help="key:<instance>; needed when the file is new")
            sub.add_argument("--subject", required=True, help="the person, e.g. robin@devbox")
            sub.add_argument("--role", required=True, choices=["reader", "writer"])
            sub.add_argument("--days", type=int, default=DEFAULT_DAYS)
            sub.add_argument("--label", default="")
            sub.add_argument("--key-out", type=Path,
                             help="create a new key file instead of printing it (0600 on POSIX; private parent DACL required on Windows)")
        if name in ("remove", "renew"):
            sub.add_argument("--id", required=True)
        if name == "renew":
            sub.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args(argv)
    if args.command == "add":
        # Reserve the destination before granting a key. Exclusive creation
        # refuses existing files/symlinks and cannot silently replace a secret.
        # On Windows the operator must supply a private parent DACL.
        destination = (open(args.key_out, "x", encoding="utf-8", newline="\n",
                            opener=lambda path, flags: os.open(path, flags, 0o600))
                       if args.key_out else nullcontext(None))
        with destination as output:
            try:
                key, secret = add(args.file, args.subject, args.role, args.days, args.label, args.issuer)
            except BaseException:
                if output is not None:
                    output.close()
                    os.unlink(args.key_out)  # only the empty file reserved above
                raise
            if output is not None:
                output.write(secret + "\n")
                output.flush()
                os.fsync(output.fileno())
                print(f"added {key.id} for {key.subject} ({key.role}), expires {_iso(key.expires)}; "
                      f"key written to {args.key_out}")
            else:
                print(f"added {key.id} for {key.subject} ({key.role}), expires {_iso(key.expires)}")
                print("the key (shown once, give it to the person; it is not stored):", file=sys.stderr)
                print(secret)
    elif args.command == "list":
        current = KeyFile.load(args.file)
        now = time.time()
        print(f"issuer {current.issuer}")
        for k in current.keys:
            state = "expired" if k.expires <= now else "valid"
            print(f"{k.id}  {k.subject}  {k.role}  {state} until {_iso(k.expires)}  {k.label}")
    elif args.command == "remove":
        key = remove(args.file, args.id)
        print(f"removed {key.id} ({key.subject})")
    else:
        key = renew(args.file, args.id, args.days)
        print(f"renewed {key.id} ({key.subject}) until {_iso(key.expires)}")


if __name__ == "__main__":
    main()
