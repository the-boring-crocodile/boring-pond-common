"""Key publication protects working authentication, with conservative ACL policy."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from boring_service_kit import keys


def test_failed_publication_keeps_valid_key_file(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    _, secret = keys.add(path, "existing", "reader", 1, issuer="key:test")
    before = path.read_bytes()
    def fail(*args):
        raise OSError("injected before publication")
    monkeypatch.setattr(keys.os, "replace", fail)
    with pytest.raises(OSError):
        keys.add(path, "new", "writer", 1)
    assert path.read_bytes() == before
    verifier = keys.KeyVerifier(path, "http://127.0.0.1:18599/mcp")
    assert asyncio.run(verifier.verify_token(secret)) is not None
    assert not list(tmp_path.glob(".keys-*"))


@pytest.mark.parametrize("changed", [
    "O:SYG:BAD:P(A;;FA;;;SY)",  # protection differs
    "O:SYG:BAD:AI(A;;FA;;;SY)",  # inheritance differs
    "O:BAG:BAD:(A;;FA;;;SY)",  # owner differs
    "O:SYG:SYD:(A;;FA;;;SY)",  # group differs
    "O:SYG:BAD:(A;;FR;;;SY)",  # access differs
    "O:SYG:BAD:(A;;FR;;;SY)(D;;FW;;;SY)",  # ACE order differs
])
def test_changed_security_is_refused(monkeypatch, changed):
    original = "O:SYG:BAD:(A;;FA;;;SY)"
    if "(D;" in changed:
        original = "O:SYG:BAD:(D;;FW;;;SY)(A;;FR;;;SY)"
    monkeypatch.setattr(keys, "_windows_security_text",
                        lambda path: original if str(path) == "original" else changed)
    with pytest.raises(ValueError, match="security differs"):
        keys._matching_windows_security(Path("original"), Path("replacement"))


@pytest.mark.parametrize("attributes, refused", [(0x20, False), (0x80, False), (0x2, True)])
def test_attribute_guard_keeps_archive_normal_exception(monkeypatch, attributes, refused):
    monkeypatch.setattr(keys, "_windows_security_text", lambda path: "same")
    target = SimpleNamespace(stat=lambda: SimpleNamespace(st_file_attributes=attributes))
    replacement = SimpleNamespace(stat=lambda: SimpleNamespace(st_file_attributes=0x20))
    if refused:
        with pytest.raises(ValueError, match="attributes"):
            keys._matching_windows_security(target, replacement)
    else:
        keys._matching_windows_security(target, replacement)


@pytest.mark.skipif(os.name != "nt", reason="native Windows security conversion")
def test_binary_layout_does_not_change_security_comparison(tmp_path, monkeypatch):
    import ctypes as C
    from ctypes import wintypes as W
    import struct
    target, replacement = tmp_path / "original", tmp_path / "replacement"
    target.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    real_dll = C.WinDLL
    advapi = real_dll("advapi32", use_last_error=True)
    get = advapi.GetFileSecurityW
    get.argtypes = [W.LPCWSTR, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    get.restype = W.BOOL
    size = W.DWORD()
    get(str(target), 7, None, 0, C.byref(size))
    buffer = C.create_string_buffer(size.value)
    assert get(str(target), 7, buffer, size.value, C.byref(size))
    original = buffer.raw
    padded = bytearray(original[:20] + b"\0" * 8 + original[20:])
    for offset in (4, 8, 12, 16):
        position = struct.unpack_from("<I", padded, offset)[0]
        if position:
            struct.pack_into("<I", padded, offset, position + 8)
    assert original != padded
    def read(path, information, output, capacity, needed):
        data = original if str(path) == str(target) else bytes(padded)
        C.cast(needed, C.POINTER(W.DWORD))[0] = len(data)
        if not output or capacity < len(data):
            return 0
        C.memmove(output, data, len(data))
        return 1
    proxy = SimpleNamespace(GetFileSecurityW=read,
        ConvertSecurityDescriptorToStringSecurityDescriptorW=
            advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW)
    monkeypatch.setattr(C, "WinDLL", lambda name, **kwargs:
                        proxy if name == "advapi32" else real_dll(name, **kwargs))
    keys._matching_windows_security(target, replacement)


@pytest.mark.skipif(os.name != "nt", reason="native Windows key publication")
def test_native_key_update_retains_inherited_security(tmp_path):
    path = tmp_path / "keys.json"
    keys.add(path, "existing", "reader", 1, issuer="key:test")
    before = keys._windows_security_text(path)
    keys.add(path, "new", "writer", 1)
    assert keys._windows_security_text(path) == before
    assert len(keys.KeyFile.load(path).keys) == 2
