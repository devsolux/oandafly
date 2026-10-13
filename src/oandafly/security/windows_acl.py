"""Windows-only owner DACL. Missing native support fails closed."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from oandafly.domain.errors import Unavailable, ValidationFailure


def private_directory(path: Path) -> None:
    _private_path(path, directory=True)


def private_file(path: Path) -> None:
    _private_path(path, directory=False)


def _private_path(path: Path, *, directory: bool) -> None:
    try:
        security: Any = importlib.import_module("win32security")
        api: Any = importlib.import_module("win32api")
        constants: Any = importlib.import_module("win32con")
        token = security.OpenProcessToken(api.GetCurrentProcess(), constants.TOKEN_QUERY)
        try:
            user = security.GetTokenInformation(token, security.TokenUser)[0]
        finally:
            token.Close()
        # Reparse points (including junctions) cannot choose another state root.
        for parent in (path, *path.parents):
            if parent.exists() and getattr(parent.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValidationFailure("Windows state cannot traverse a reparse point")
        if path.exists():
            original = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
                security.OWNER_SECURITY_INFORMATION)
            if original.GetSecurityDescriptorOwner() != user:
                raise ValidationFailure("Windows state belongs to a different user")
        elif not directory:
            raise ValidationFailure("Windows private file does not exist")
        flags = constants.OBJECT_INHERIT_ACE | constants.CONTAINER_INHERIT_ACE if directory else 0
        acl = security.ACL()
        acl.AddAccessAllowedAceEx(security.ACL_REVISION,
                                 flags,
                                 constants.FILE_ALL_ACCESS, user)
        descriptor = security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorOwner(user, False)
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
        descriptor.SetSecurityDescriptorControl(security.SE_DACL_PROTECTED, security.SE_DACL_PROTECTED)
        if not path.exists():
            if not path.parent.exists():
                private_directory(path.parent)
            attributes: Any = importlib.import_module("pywintypes").SECURITY_ATTRIBUTES()
            attributes.SECURITY_DESCRIPTOR = descriptor
            importlib.import_module("win32file").CreateDirectory(str(path), attributes)
        security.SetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION
            | security.PROTECTED_DACL_SECURITY_INFORMATION, user, None, acl, None)
        actual = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
            security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
        actual_acl = actual.GetSecurityDescriptorDacl()
        if (actual.GetSecurityDescriptorOwner() != user or actual_acl is None
                or actual_acl.GetAceCount() != 1 or actual_acl.GetAce(0)[2] != user
                or actual_acl.GetAce(0)[1] != constants.FILE_ALL_ACCESS
                or actual_acl.GetAce(0)[0] != (security.ACCESS_ALLOWED_ACE_TYPE, flags)
                or not actual.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED):
            raise ValidationFailure("Windows private state DACL verification failed")
    except (Unavailable, ValidationFailure):
        raise
    except Exception:
        raise Unavailable("Private Windows state requires working native pywin32 ACL support") from None
