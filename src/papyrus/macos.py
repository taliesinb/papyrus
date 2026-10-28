"""macOS file provenance: extended attributes written by browsers/Finder.

* com.apple.metadata:kMDItemWhereFroms - binary plist list of URLs:
  [download URL, referrer page URL] (Safari/Chrome/Firefox set this).
* com.apple.quarantine - 'flags;hex-time;agent;uuid' (agent = 'Safari', 'Chrome' ...).
* st_birthtime - creation time of the file on this volume (≈ download time).

Also `clonefile(2)`: an APFS copy-on-write clone, which costs no space until
one side is modified. (Plain `cp` and `shutil.copy*` make full copies; `cp -c`
clones.)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import sys
from datetime import UTC, datetime
from pathlib import Path

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True) if sys.platform == "darwin" else None
if _libc is not None:
    _libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                               ctypes.c_uint32, ctypes.c_int]
    _libc.getxattr.restype = ctypes.c_ssize_t
    _libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    _libc.clonefile.restype = ctypes.c_int

CLONE_NOFOLLOW = 0x0001
CLONE_NOOWNERCOPY = 0x0002


def clonefile(src: Path, dst: Path) -> None:
    """Clone `src` to the new path `dst` (APFS only). Raises OSError (EXDEV across volumes,
    ENOTSUP on other filesystems, EEXIST if `dst` exists); NotImplementedError off macOS."""
    if _libc is None:
        raise NotImplementedError("clonefile is macOS-only")
    if _libc.clonefile(str(src).encode(), str(dst).encode(), CLONE_NOFOLLOW | CLONE_NOOWNERCOPY) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(dst))


def getxattr(path: Path, name: str) -> bytes | None:
    if _libc is None:
        return None
    p, n = str(path).encode(), name.encode()
    size = _libc.getxattr(p, n, None, 0, 0, 0)
    if size <= 0:
        return None
    buf = ctypes.create_string_buffer(size)
    got = _libc.getxattr(p, n, buf, size, 0, 0)
    return buf.raw[:got] if got > 0 else None


def where_froms(path: Path) -> list[str]:
    raw = getxattr(path, "com.apple.metadata:kMDItemWhereFroms")
    if not raw:
        return []
    try:
        val = plistlib.loads(raw)
    except Exception:
        return []
    if isinstance(val, str):
        val = [val]
    out: list[str] = []
    for u in val if isinstance(val, list) else []:
        if isinstance(u, str) and u and u not in out:
            out.append(u)
    return out


def quarantine(path: Path) -> dict | None:
    raw = getxattr(path, "com.apple.quarantine")
    if not raw:
        return None
    parts = raw.decode(errors="replace").split(";")
    info: dict = {"raw": raw.decode(errors="replace")}
    if len(parts) >= 3:
        info["agent"] = parts[2] or None
        try:
            info["time"] = datetime.fromtimestamp(int(parts[1], 16), UTC).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return info
