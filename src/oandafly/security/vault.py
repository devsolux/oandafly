"""macOS Keychain and Windows Credential Manager, with no plaintext fallback.

These bindings need native-platform qualification before shipping signed bundles.
Python and OS APIs necessarily create short-lived token copies in process memory;
this module does not claim complete secret-memory zeroisation.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import c_char_p, c_int32, c_uint32, c_void_p
from typing import Any

from pydantic import SecretStr

from oandafly.domain.errors import Unavailable
from oandafly.security.credentials import CredentialReference, NativeVault, token_bytes

_SERVICE = b"com.devsolux.oandafly"


class MacKeychainVault:
    """Generic-password Keychain binding without command-line secret exposure."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise Unavailable("macOS Keychain is unavailable on this platform")
        self._security: Any = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self._core: Any = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        find = self._security.SecKeychainFindGenericPassword
        find.argtypes = [
            c_void_p,
            c_uint32,
            c_char_p,
            c_uint32,
            c_char_p,
            ctypes.POINTER(c_uint32),
            ctypes.POINTER(c_void_p),
            ctypes.POINTER(c_void_p),
        ]
        find.restype = c_int32
        add = self._security.SecKeychainAddGenericPassword
        add.argtypes = [
            c_void_p,
            c_uint32,
            c_char_p,
            c_uint32,
            c_char_p,
            c_uint32,
            c_void_p,
            ctypes.POINTER(c_void_p),
        ]
        add.restype = c_int32
        self._security.SecKeychainItemModifyContent.argtypes = [c_void_p, c_void_p, c_uint32, c_void_p]
        self._security.SecKeychainItemModifyContent.restype = c_int32
        self._security.SecKeychainItemDelete.argtypes = [c_void_p]
        self._security.SecKeychainItemDelete.restype = c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [c_void_p, c_void_p]
        self._security.SecKeychainItemFreeContent.restype = c_int32
        self._core.CFRelease.argtypes = [c_void_p]
        self._core.CFRelease.restype = None

    def _find(self, reference: CredentialReference) -> tuple[int, c_void_p, c_void_p, int]:
        key = reference.vault_key.encode("utf-8")
        length, data, item = c_uint32(), c_void_p(), c_void_p()
        status = int(
            self._security.SecKeychainFindGenericPassword(
                None,
                len(_SERVICE),
                _SERVICE,
                len(key),
                key,
                ctypes.byref(length),
                ctypes.byref(data),
                ctypes.byref(item),
            )
        )
        return status, data, item, length.value

    def _free(self, data: c_void_p, item: c_void_p) -> None:
        if data.value:
            self._security.SecKeychainItemFreeContent(None, data)
        if item.value:
            self._core.CFRelease(item)

    def store(self, reference: CredentialReference, secret: SecretStr) -> None:
        value = token_bytes(secret)
        buffer = ctypes.create_string_buffer(value)
        status, data, item, _ = self._find(reference)
        try:
            if status == 0:
                result = self._security.SecKeychainItemModifyContent(item, None, len(value), buffer)
            elif status == -25300:  # errSecItemNotFound
                key = reference.vault_key.encode("utf-8")
                result = self._security.SecKeychainAddGenericPassword(
                    None, len(_SERVICE), _SERVICE, len(key), key, len(value), buffer, None
                )
            else:
                raise Unavailable("Keychain credential access was denied or unavailable")
            if result != 0:
                raise Unavailable("Keychain credential storage failed")
        finally:
            ctypes.memset(buffer, 0, len(buffer))
            self._free(data, item)

    def load(self, reference: CredentialReference) -> SecretStr:
        status, data, item, length = self._find(reference)
        try:
            if status != 0 or not data.value or not 1 <= length <= 4096:
                raise Unavailable("Keychain credential is unavailable")
            secret = SecretStr(ctypes.string_at(data, length).decode("ascii"))
            token_bytes(secret)
            return secret
        finally:
            self._free(data, item)

    def delete(self, reference: CredentialReference) -> None:
        status, data, item, _ = self._find(reference)
        try:
            if status == -25300:
                return
            if status != 0 or self._security.SecKeychainItemDelete(item) != 0:
                raise Unavailable("Keychain credential deletion failed")
        finally:
            self._free(data, item)


class _FileTime(ctypes.Structure):
    _fields_ = [("low", c_uint32), ("high", c_uint32)]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", c_uint32),
        ("Type", c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", c_uint32),
        ("CredentialBlob", c_void_p),
        ("Persist", c_uint32),
        ("AttributeCount", c_uint32),
        ("Attributes", c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialVault:
    """Generic Credential Manager entries bound to the current Windows user."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise Unavailable("Windows Credential Manager is unavailable on this platform")
        loader: Any = getattr(ctypes, "WinDLL")
        self._advapi: Any = loader("Advapi32.dll", use_last_error=True)
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(_Credential), c_uint32]
        self._advapi.CredWriteW.restype = c_int32
        self._advapi.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            c_uint32,
            c_uint32,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        self._advapi.CredReadW.restype = c_int32
        self._advapi.CredDeleteW.argtypes = [ctypes.c_wchar_p, c_uint32, c_uint32]
        self._advapi.CredDeleteW.restype = c_int32
        self._advapi.CredFree.argtypes = [c_void_p]
        self._advapi.CredFree.restype = None

    def store(self, reference: CredentialReference, secret: SecretStr) -> None:
        value = token_bytes(secret)
        # Windows generic credentials have a documented 2560-byte blob limit.
        if len(value) > 2560:
            raise Unavailable("Credential exceeds the native Windows vault blob limit")
        buffer = ctypes.create_string_buffer(value)
        credential = _Credential(
            Flags=0,
            Type=1,
            TargetName=reference.vault_key,
            Comment="Oandafly scoped API credential",
            CredentialBlobSize=len(value),
            CredentialBlob=ctypes.cast(buffer, c_void_p),
            Persist=2,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName=reference.authority_id,
        )
        try:
            if not self._advapi.CredWriteW(ctypes.byref(credential), 0):
                raise Unavailable("Windows credential storage failed")
        finally:
            ctypes.memset(buffer, 0, len(buffer))

    def load(self, reference: CredentialReference) -> SecretStr:
        pointer = ctypes.POINTER(_Credential)()
        if not self._advapi.CredReadW(reference.vault_key, 1, 0, ctypes.byref(pointer)):
            raise Unavailable("Windows credential is unavailable")
        try:
            value = pointer.contents
            if value.Type != 1 or not value.CredentialBlob or not 1 <= value.CredentialBlobSize <= 2560:
                raise Unavailable("Windows credential has invalid native metadata")
            secret = SecretStr(
                ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("ascii")
            )
            token_bytes(secret)
            return secret
        finally:
            self._advapi.CredFree(pointer)

    def delete(self, reference: CredentialReference) -> None:
        if not self._advapi.CredDeleteW(reference.vault_key, 1, 0):
            get_error: Any = getattr(ctypes, "get_last_error")
            if get_error() != 1168:  # ERROR_NOT_FOUND; deletion is idempotent.
                raise Unavailable("Windows credential deletion failed")


def native_vault() -> NativeVault:
    if sys.platform == "darwin":
        return MacKeychainVault()
    if sys.platform == "win32":
        return WindowsCredentialVault()
    if sys.platform == "linux":
        from oandafly.security.linux_vault import SecretServiceVault
        return SecretServiceVault()
    raise Unavailable("No qualified native credential vault is provided for this platform")
