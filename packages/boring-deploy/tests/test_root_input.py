"""Operator inputs are opened without links and validated before any read."""
import os
from pathlib import Path
import stat

import pytest

import boring_deploy


@pytest.fixture
def staged(tmp_path):
    root = tmp_path / "staged"
    root.mkdir(mode=0o700)
    (root / "config.json").write_text('{"url":"https://service.example/mcp"}')
    (root / "credentials").mkdir(mode=0o700)
    (root / "credentials/key").write_bytes(b"synthetic credential\n")
    (root / "credentials/key").chmod(0o600)
    return root


def checked(path, **kw):
    # The same rules as root, under the ordinary test owner's synthetic tree.
    return boring_deploy.root_input(path, owner=os.getuid(), **kw)


def test_directory_checks_every_entry_and_reports_only_metadata(staged):
    with checked(staged, directory=True) as source:
        assert {row["path"] for row in source.metadata} == {
            str(staged), str(staged / "config.json"),
            str(staged / "credentials"), str(staged / "credentials/key")}
        assert all(set(row) == {"path", "uid", "gid", "mode", "kind"} for row in source.metadata)
        assert "synthetic credential" not in repr(source)
        assert len(source.describe()) == 4
        assert all("uid " in line and "mode " in line for line in source.describe())
        assert "synthetic credential" not in "\n".join(source.describe())


@pytest.mark.parametrize("where", ["leaf", "parent", "nested-file", "nested-directory"])
def test_links_are_refused_without_reading_the_target(staged, tmp_path, where):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "private"
    secret.write_text("do not copy")
    path, directory = staged / "config.json", False
    if where == "leaf":
        path.unlink()
        path.symlink_to(secret)
    elif where == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(staged, target_is_directory=True)
        path = alias / "config.json"
    elif where == "nested-file":
        (staged / "credentials/key").unlink()
        (staged / "credentials/key").symlink_to(secret)
        path, directory = staged, True
    else:
        (staged / "foreign").symlink_to(outside, target_is_directory=True)
        path, directory = staged, True
    with pytest.raises(boring_deploy.Failure, match="operator input"):
        with checked(path, directory=directory):
            pytest.fail("an unsafe input was admitted")
    assert secret.read_text() == "do not copy"


@pytest.mark.parametrize("where", ["file", "directory", "ancestor"])
def test_another_writer_is_refused(staged, where):
    target = {"file": staged / "config.json", "directory": staged / "credentials",
              "ancestor": staged}[where]
    target.chmod(0o770 if target.is_dir() else 0o660)
    with pytest.raises(boring_deploy.Failure, match="operator input"):
        with checked(staged, directory=True):
            pytest.fail("writable input was admitted")


def test_wrong_owner_is_refused(staged):
    with pytest.raises(boring_deploy.Failure, match="operator input"):
        with boring_deploy.root_input(staged / "config.json", owner=os.getuid() + 1):
            pytest.fail("wrong owner admitted")


def test_special_files_and_multiple_links_are_refused(staged):
    fifo = staged / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(boring_deploy.Failure, match="operator input"):
        with checked(fifo):
            pytest.fail("FIFO admitted")
    fifo.unlink()
    os.link(staged / "config.json", staged / "alias")
    with pytest.raises(boring_deploy.Failure, match="operator input"):
        with checked(staged, directory=True):
            pytest.fail("hard link admitted")


def test_opened_file_is_read_and_copied_without_reopening_its_name(staged, tmp_path):
    path = staged / "credentials/key"
    with checked(path) as source:
        path.unlink()
        path.symlink_to(staged / "config.json")
        assert source.read_text() == "synthetic credential\n"
        target = tmp_path / "copy"
        source.copy_to(target)
        assert target.read_bytes() == b"synthetic credential\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(OSError):
        os.fstat(source.fd)


def test_target_symlink_is_never_followed(staged, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    target = tmp_path / "target"
    target.symlink_to(victim)
    with checked(staged / "credentials/key") as source, pytest.raises(OSError):
        source.copy_to(target)
    assert victim.read_text() == "keep"


def test_relative_paths_are_refused():
    with pytest.raises(boring_deploy.Failure, match="operator input"):
        with checked(Path("config.json")):
            pytest.fail("relative input admitted")


def test_checked_parent_components_support_existing_config_references(staged):
    path = staged / "credentials" / ".." / "config.json"
    with checked(path) as source:
        assert source.read_bytes() == (staged / "config.json").read_bytes()


def test_parent_components_do_not_erase_a_prior_link(staged):
    alias = staged / "alias"
    alias.symlink_to(staged / "credentials", target_is_directory=True)
    with pytest.raises(boring_deploy.Failure, match="operator input"):
        with checked(alias / ".." / "config.json"):
            pytest.fail("link was normalized away")
