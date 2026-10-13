"""Canonical path regressions; Windows shell calls use an explicit fixture."""

import os
import stat
import sys
import tempfile
from pathlib import PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from oandafly import paths
from oandafly.domain.errors import Unavailable, ValidationFailure


@pytest.fixture
def redirected_environment(monkeypatch, tmp_path):
    for name in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "untrusted" / name))


@pytest.mark.parametrize("platform,relative", [
    ("darwin", ".devsolux/oandafly"),
    ("linux", ".devsolux/oandafly"),
])
def test_posix_storage_uses_os_account_home(monkeypatch, redirected_environment, platform, relative):
    def account(uid):
        assert uid == 501
        return SimpleNamespace(pw_dir="/Users/Bäris Aydin")

    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwuid=account))
    monkeypatch.setattr(paths, "os", SimpleNamespace(name="posix", getuid=lambda: 501))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(paths, "Path", PurePosixPath)
    monkeypatch.setattr(paths, "secure_directory", lambda directory: directory)

    assert paths.state_directory() == PurePosixPath("/Users/Bäris Aydin") / relative
    assert all(PurePosixPath(value).is_relative_to(paths.state_directory())
               for value in paths.runtime_environment(paths.state_directory()).values())


@pytest.mark.posix_only
@pytest.mark.skipif(os.name != "posix", reason="Native POSIX directory permissions")
def test_macos_storage_creates_and_reuses_private_directory(monkeypatch, tmp_path):
    home = tmp_path.resolve() / "home"
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_dir=str(home))))
    monkeypatch.setattr(sys, "platform", "darwin")

    directory = paths.state_directory()
    assert directory == home / ".devsolux" / "oandafly"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
    marker = directory / "existing-state"
    marker.write_bytes(b"preserved")
    assert paths.state_directory() == directory
    assert marker.read_bytes() == b"preserved"




@pytest.fixture
def windows_shell(monkeypatch):
    result = SimpleNamespace(home=r"C:\Users\Bäris Aydin", status=0)

    def folder_path(window, folder, token, flags, output):
        assert (window, folder, token, flags) == (None, 0x0028, None, 0)
        output.value = result.home
        return result.status

    def load_library(name, *, use_last_error):
        assert (name, use_last_error) == ("shell32.dll", True)
        return SimpleNamespace(SHGetFolderPathW=folder_path)

    monkeypatch.setattr(paths, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(paths, "Path", PureWindowsPath)
    monkeypatch.setattr(paths.ctypes, "WinDLL", load_library, raising=False)
    monkeypatch.setattr(paths, "secure_directory", lambda directory: directory)
    return result


def test_windows_storage_uses_native_profile(windows_shell, redirected_environment):
    assert paths.state_directory() == PureWindowsPath(r"C:\Users\Bäris Aydin\.devsolux\oandafly")
    assert all(PureWindowsPath(value).is_relative_to(paths.state_directory())
               for value in paths.runtime_environment(paths.state_directory()).values())


def test_runtime_replaces_inherited_cache_and_cached_temporary_locations(monkeypatch, tmp_path):
    root = tmp_path.resolve() / "home" / ".devsolux" / "oandafly"
    monkeypatch.setattr(paths, "application_directory", lambda: root)
    for name in ("TMPDIR", "TMP", "TEMP", "JOBLIB_TEMP_FOLDER", "XDG_CACHE_HOME",
                 "PYTHONPYCACHEPREFIX", "SCIKIT_LEARN_DATA"):
        monkeypatch.setenv(name, str(tmp_path / "external"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "cached-os-temp"))
    monkeypatch.setattr(sys, "pycache_prefix", None)
    monkeypatch.setattr(sys, "_xoptions", dict(sys._xoptions))

    assert paths.configure_runtime_storage() == root
    assert tempfile.gettempdir() == str(root / "tmp")
    assert sys.pycache_prefix == str(root / "cache" / "python")
    assert sys._xoptions["pycache_prefix"] == sys.pycache_prefix
    assert os.environ["XDG_CACHE_HOME"] == str(root / "cache")
    with tempfile.TemporaryFile() as handle:
        handle.write(b"application scratch")
    assert (root / "cache" / "python").is_dir()


def test_windows_profile_lookup_failure_has_no_fallback(windows_shell):
    windows_shell.status = -1
    with pytest.raises(Unavailable, match="profile directory"):
        paths.state_directory()


@pytest.mark.parametrize("home", ["", "relative", r"C:Users\Baris", r"\Users\Baris"])
def test_windows_profile_must_be_absolute(windows_shell, home):
    windows_shell.home = home
    with pytest.raises(ValidationFailure, match="non-absolute"):
        paths.state_directory()
