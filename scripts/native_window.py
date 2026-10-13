"""Native visible-window probes for the desktop build gate (not a security sandbox)."""
from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
import sys
from typing import Any


def visible_window(pid: int, window_id: int) -> dict[str, Any]:
    if sys.platform == "win32":
        return _windows(pid)
    if sys.platform == "darwin":
        return _macos(pid)
    if sys.platform == "linux":
        return _linux(window_id)
    raise ValueError("No native window probe for this target")


def _linux(window: int) -> dict:
    # X11/XWayland visibility is independently queried through the server. X11
    # properties/IDs are client-supplied, so this is NOT hostile-process identity proof.
    tool = shutil.which("xwininfo")
    if not tool:
        raise ValueError("Install x11-utils for the native Linux visible-window gate")
    result = subprocess.run([tool, "-id", hex(window)], capture_output=True, text=True, timeout=3)
    if result.returncode:
        return {"visible": False, "method": "X11_XWININFO"}
    width = re.search(r"Width:\s*(\d+)", result.stdout)
    height = re.search(r"Height:\s*(\d+)", result.stdout)
    return {"visible": "Map State: IsViewable" in result.stdout and width is not None and height is not None
                        and int(width.group(1)) >= 400 and int(height.group(1)) >= 300,
            "method": "X11_XWININFO", "window_id": window,
            "scope": "Server visibility of child-reported ID; not process-security attestation"}


def _windows(pid: int) -> dict:
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    found = []
    @callback_type
    def inspect(handle: Any, extra: Any) -> bool:
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(handle):
            bounds = wintypes.RECT()
            if user.GetWindowRect(handle, ctypes.byref(bounds)) and bounds.right-bounds.left >= 400 and bounds.bottom-bounds.top >= 300:
                found.append(int(handle))
        return True
    if not user.EnumWindows(inspect, 0):
        raise OSError("User32 EnumWindows failed")
    return {"visible": bool(found), "method": "USER32_PID_VISIBLE_BOUNDS", "windows": found}


def _macos(pid: int) -> dict:
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    pointer = ctypes.c_void_p
    cf.CFRelease.argtypes = [pointer]
    cf.CFRelease.restype = None
    cf.CFArrayGetCount.argtypes = [pointer]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetValueAtIndex.argtypes = [pointer, ctypes.c_long]
    cf.CFArrayGetValueAtIndex.restype = pointer
    cf.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = pointer
    cf.CFDictionaryGetValue.argtypes = [pointer, pointer]
    cf.CFDictionaryGetValue.restype = pointer
    cf.CFNumberGetValue.argtypes = [pointer, ctypes.c_int, pointer]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGWindowListCopyWindowInfo.restype = pointer
    keys = {name: cf.CFStringCreateWithCString(None, name.encode(), 0x08000100)
            for name in ("kCGWindowOwnerPID", "kCGWindowNumber", "kCGWindowLayer", "kCGWindowBounds", "Width", "Height")}
    values = cg.CGWindowListCopyWindowInfo(1 | 16, 0)  # on-screen only; exclude desktop elements
    if not values or not all(keys.values()):
        raise OSError("WindowServer metadata unavailable in this login session")
    def number(dictionary: Any, key: str) -> int:
        value = cf.CFDictionaryGetValue(dictionary, keys[key])
        output = ctypes.c_longlong()
        if not value or not cf.CFNumberGetValue(value, 4, ctypes.byref(output)):
            return -1
        return output.value
    found = []
    try:
        for index in range(cf.CFArrayGetCount(values)):
            entry = cf.CFArrayGetValueAtIndex(values, index)
            bounds = cf.CFDictionaryGetValue(entry, keys["kCGWindowBounds"])
            if (number(entry, "kCGWindowOwnerPID") == pid and number(entry, "kCGWindowLayer") == 0 and bounds
                    and number(bounds, "Width") >= 400 and number(bounds, "Height") >= 300):
                found.append(number(entry, "kCGWindowNumber"))
    finally:
        cf.CFRelease(values)
        for value in keys.values():
            if value:
                cf.CFRelease(value)
    return {"visible": bool(found), "method": "COREGRAPHICS_PID_ONSCREEN_BOUNDS", "windows": found}
