"""OS-derived canonical locations; environment variables cannot fork authority."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from oandafly import APPLICATION_NAME
from oandafly.domain.errors import Unavailable, ValidationFailure


def application_directory() -> Path:
    """Resolve ~/.devsolux/oandafly for all managed data without creating it."""
    if os.name == "posix":
        import pwd

        # Path.home() trusts HOME, which would let separate workers choose a
        # second authority store. The account database is the authority here.
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if not home.is_absolute():
            raise ValidationFailure("Operating-system account has no absolute home directory")
        return home / ".devsolux" / APPLICATION_NAME
    if os.name == "nt":
        loader: Any = getattr(ctypes, "WinDLL")
        shell: Any = loader("shell32.dll", use_last_error=True)
        function = shell.SHGetFolderPathW
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
        ]
        function.restype = ctypes.c_int32
        output = ctypes.create_unicode_buffer(32768)
        if function(None, 0x0028, None, 0, output) != 0:  # CSIDL_PROFILE
            raise Unavailable("Windows could not resolve the current user's profile directory")
        value = Path(output.value)
        if not value.is_absolute():
            raise ValidationFailure("Windows returned a non-absolute profile directory")
        return value / ".devsolux" / APPLICATION_NAME
    raise Unavailable("No canonical state location is qualified for this operating system")


def state_directory() -> Path:
    """Create and validate the private application storage root."""
    return secure_directory(application_directory())


def runtime_environment(directory: Path) -> dict[str, str]:
    """Prepare private cache/temp locations for this application process tree."""
    directory = secure_directory(directory)
    temporary = str(secure_directory(directory / "tmp"))
    cache = secure_directory(directory / "cache")
    return {
        "TMPDIR": temporary, "TMP": temporary, "TEMP": temporary,
        "XDG_CACHE_HOME": str(cache),
        "PYTHONPYCACHEPREFIX": str(secure_directory(cache / "python")),
    }


def configure_runtime_storage() -> Path:
    """Configure the CLI process before importing or starting runtime services."""
    directory = state_directory()
    environment = runtime_environment(directory)
    os.environ.update(environment)
    # tempfile may already have cached the OS default before application startup.
    tempfile.tempdir = environment["TMPDIR"]
    sys.pycache_prefix = environment["PYTHONPYCACHEPREFIX"]
    # multiprocessing spawn reproduces -X options, including under Python -I.
    sys._xoptions["pycache_prefix"] = sys.pycache_prefix
    return directory


def secure_directory(target: Path) -> Path:
    """Create owner-only directories without traversing symlinks on POSIX."""
    target = target.absolute()
    if ".." in target.parts:
        raise ValidationFailure("Application paths must not contain parent traversal")
    if os.name == "posix":
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(target.anchor, flags)
        try:
            for part in target.parts[1:]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    child = os.open(part, flags, dir_fd=descriptor)
                else:
                    child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid():
                raise ValidationFailure("Application state is owned by a different user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ValidationFailure("Application state permissions must be 0700")
            # Preserve the binding after traversal; a concurrent replacement is
            # an error, not permission to initialize a second namespace.
            final = target.lstat()
            if not stat.S_ISDIR(final.st_mode) or (final.st_dev, final.st_ino) != (info.st_dev, info.st_ino):
                raise ValidationFailure("Application state directory changed during validation")
        except OSError:
            raise ValidationFailure("Application state cannot traverse a link or non-directory") from None
        finally:
            os.close(descriptor)
        return target
    if os.name == "nt":
        from oandafly.security.windows_acl import private_directory
        private_directory(target)
        if not target.is_dir():
            raise ValidationFailure("Application state must be a directory")
        return target
    for parent in (target, *target.parents):
        if parent.is_symlink():
            raise ValidationFailure("Application state must not traverse symbolic links")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not target.is_dir():
        raise ValidationFailure("Application state must be a directory")
    return target
