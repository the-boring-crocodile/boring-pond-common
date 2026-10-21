"""Real process interleavings cannot restore revoked keys or lose additions."""
import concurrent.futures
import multiprocessing
import os
from pathlib import Path
import pytest
from boring_service_kit import keys


def _change(path, remove):
    if remove:
        keys.remove(Path(path), remove)
    else:
        keys.add(Path(path), "new", "reader", 1, "fixture")


def test_concurrent_add_and_revoke_preserve_both_effects(tmp_path):
    path = tmp_path / "keys.json"
    removed, _ = keys.add(path, "old", "writer", 1, issuer="key:fixture")
    context = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        jobs = [pool.submit(_change, str(path), removed.id), pool.submit(_change, str(path), None)]
        for job in jobs:
            job.result(timeout=30)
    loaded = keys.KeyFile.load(path)
    assert len(loaded.keys) == 1 and loaded.keys[0].subject == "new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_key_rewrite_preserves_private_permissions_and_fsyncs(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    key, _ = keys.add(path, "one", "writer", 1, issuer="key:fixture")
    path.chmod(0o640)
    synced = []
    real = os.fsync
    def sync(fd):
        synced.append(fd)
        return real(fd)
    monkeypatch.setattr(keys.os, "fsync", sync)
    keys.renew(path, key.id, 2)
    assert path.stat().st_mode & 0o777 == 0o640
    assert len(synced) == 2


def test_failed_atomic_publication_retains_original(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    key, _ = keys.add(path, "one", "writer", 1, issuer="key:fixture")
    original = path.read_bytes()
    def fail(*args):
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(keys.os, "replace", fail)
    with pytest.raises(OSError):
        keys.renew(path, key.id, 2)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".keys-*"))
