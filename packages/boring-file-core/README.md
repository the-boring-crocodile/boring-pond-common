# boring-file-core

Descriptor-relative filesystem primitives for the file tools of
boring-crocodile-mcp and boring-pirates. Python >= 3.10.
Disk primitives are Linux only (renameat2); textformat is portable.
No dependencies. Policy belongs to the callers,
except the reserved names `parts()` refuses (`.git`, `.pirate-*`,
`.boring-file-*`).

Developed here only; each consumer installs a wheel built from this directory.
Tests: `PYTHONPATH=src python -m unittest discover -s tests` (31 edge cases:
symlinks, hard links, FIFOs, sockets, races at the target, modes, leftovers).

Anything that takes a verified target's place is `StaleEdit`; a
socket is `NotAllowedFile`; components over 255 bytes and unencodable paths
are `InvalidPath`; a directory in a replaced file's place is `StaleEdit`; a C
library without `renameat2` gives `OSError(ENOSYS)`.

textformat owns decoding, encoding and newline selection for the Pond and
pondNT, with no disk access and no MCP dependency. The package imports without
Linux-only flags; those are evaluated when a Linux disk operation runs.
pondNT's default newline is CRLF; Linux callers use LF.

`atomic_write(mode=None, create=True)` delegates new-file permissions to
the kernel umask/default ACL and setgid inheritance. Explicit modes retain their
existing behavior; replacement requires an explicit mode and preserves metadata.
