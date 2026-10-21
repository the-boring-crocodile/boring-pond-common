"""The issuer's SQLite store: grants in `records`, clients in `clients`.

`records` holds what a sign-in produces: pending authorization requests, codes,
access and refresh tokens (and, for a consumer that adds them, other bearer
kinds). A record whose key is a bearer value is stored under its SHA-256; the
payload field that repeats the value is stored blank. Nothing ever needs a
bearer value back: clients hold them, the store only verifies them.

`clients` holds who may sign in, with an explicit origin and state:

| origin | how the client got here |
|---|---|
| `admin` | added by the administrator (pre-registration) |
| `metadata` | a Client ID Metadata Document on an allowed host (CIMD) |
| `dynamic` | open dynamic client registration (RFC 7591), when enabled |

`state` is `registered` until the first completed authorization and `active`
after it. A registered client has an `expires`; an active one does not.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

RECORD_COLUMNS = ("kind", "key", "payload", "expires", "pair")
CLIENT_COLUMNS = ("client_id", "origin", "state", "payload", "created", "refreshed", "last_used", "expires")
ORIGINS = ("admin", "metadata", "dynamic")
STATES = ("registered", "active")
# A store that only issues (a service's own issuer). A consumer with more
# tables (the Crocodile) keeps its own version line and calls the functions below.
ISSUER_SCHEMA = 1


class Transaction:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK on a connection of its own.

    A class context manager, not a generator: the SDK's TokenError is frozen,
    and a generator context manager tries to assign its traceback.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> sqlite3.Connection:
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            return self.db
        except BaseException:
            self.db.close()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.close()


def secret_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def private_file(path: Path, *, exclusive: bool = False) -> None:
    """Create or check a private store file: owned, 0600, in an owned 0700 directory."""
    if not path.is_absolute():
        raise ValueError("OAuth database path must be absolute")
    if os.name == "posix":
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise PermissionError("OAuth state directory must be owned by this UID and private (0700)")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("OAuth database must be a regular file with one link")
        if os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise PermissionError("OAuth database must be an owned private regular file (0600)")
    finally:
        os.close(fd)


def create_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE records (
        kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
        expires REAL, pair TEXT, PRIMARY KEY(kind, key))""")
    db.execute("CREATE INDEX record_expiry ON records(expires)")
    db.execute("CREATE INDEX record_pair ON records(pair)")
    create_clients(db)


def create_clients(db: sqlite3.Connection) -> None:
    origins = ",".join(f"'{o}'" for o in ORIGINS)
    states = ",".join(f"'{s}'" for s in STATES)
    db.execute(f"""CREATE TABLE clients (
        client_id TEXT PRIMARY KEY,
        origin TEXT NOT NULL CHECK(origin IN ({origins})),
        state TEXT NOT NULL CHECK(state IN ({states})),
        payload TEXT NOT NULL, created REAL NOT NULL, refreshed REAL NOT NULL,
        last_used REAL, expires REAL,
        CHECK((state = 'active') = (expires IS NULL)))""")
    db.execute("CREATE INDEX client_expiry ON clients(expires)")


def validate_tables(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("OAuth integrity check failed")
    if tuple(row[1] for row in db.execute("PRAGMA table_info(records)")) != RECORD_COLUMNS:
        raise ValueError("unexpected OAuth record schema")
    if tuple(row[1] for row in db.execute("PRAGMA table_info(clients)")) != CLIENT_COLUMNS:
        raise ValueError("unexpected OAuth client schema")


def initialize_issuer_store(db: sqlite3.Connection) -> None:
    """A service's own store: create it new, or check it."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            raise ValueError("refusing unversioned nonempty OAuth database")
        create_tables(db)
        db.execute(f"PRAGMA user_version={ISSUER_SCHEMA}")
    elif version != ISSUER_SCHEMA:
        raise ValueError(f"OAuth store is schema {version}; this release reads schema {ISSUER_SCHEMA}")
    validate_tables(db)
    if {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} != {"records", "clients"}:
        raise ValueError("this is not a service issuer store (unexpected tables)")


def backup(database_file: Path, destination: Path) -> None:
    """Consistent copy into a new private file; never overwrite a backup."""
    private_file(destination, exclusive=True)
    try:
        with closing(sqlite3.connect(database_file)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
    except BaseException:
        destination.unlink()
        raise


Initializer = Callable[[sqlite3.Connection], None]
