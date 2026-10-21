"""Secret delivery must not overwrite files or inherit a permissive POSIX mode."""
import os
import stat

import pytest

from boring_service_kit import keys


def issue(store, output):
    keys.main(["add", "--file", str(store), "--issuer", "key:fixture",
               "--subject", "fixture", "--role", "reader", "--key-out", str(output)])


def test_secret_output_private_and_not_printed(tmp_path, capsys):
    store, output = tmp_path / "keys.json", tmp_path / "secret"
    previous = os.umask(0)
    try:
        issue(store, output)
    finally:
        os.umask(previous)
    secret = output.read_text().strip()
    assert keys.KeyVerifier(store, "http://localhost/mcp").caller(secret) is not None
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_existing_output_refused_before_new_grant(tmp_path):
    store, output = tmp_path / "keys.json", tmp_path / "secret"
    issue(store, output)
    before = store.read_bytes(), output.read_bytes()
    with pytest.raises(FileExistsError):
        issue(store, output)
    assert (store.read_bytes(), output.read_bytes()) == before


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs separate Windows privileges")
def test_symlink_output_refused_without_touching_target(tmp_path):
    store, output, target = tmp_path / "keys.json", tmp_path / "link", tmp_path / "target"
    target.write_text("untouched")
    output.symlink_to(target)
    with pytest.raises(FileExistsError):
        issue(store, output)
    assert target.read_text() == "untouched"
    assert not store.exists()
