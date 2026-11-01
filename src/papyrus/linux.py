"""Linux file provenance and copy-on-write copies: the counterparts of macos.py.

* user.xdg.origin.url, user.xdg.referrer.url - the freedesktop "common extended attributes",
  written on a download by Chromium-based browsers, `wget --xattr` and `curl --xattr`:
  [download URL, referrer page URL], as macOS's kMDItemWhereFroms.
* FICLONE - a reflink (btrfs, XFS, bcachefs, OpenZFS 2.2+), which costs no space until one side
  is modified, as an APFS clone does. Other filesystems refuse it, and the caller copies.

Both are no-ops elsewhere: where_froms returns [], reflink raises NotImplementedError.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FICLONE = 0x40049409  # _IOW(0x94, 9, int), linux/fs.h
WHERE_FROM_ATTRS = ("user.xdg.origin.url", "user.xdg.referrer.url")


def reflink(src: Path, dst: Path) -> None:
    """Clone `src` to the new path `dst`. Raises OSError (EXDEV across filesystems, EOPNOTSUPP or
    EINVAL where reflinks are not supported, EEXIST if `dst` exists); NotImplementedError off Linux."""
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("FICLONE is Linux-only")
    import fcntl

    with open(src, "rb") as s:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            fcntl.ioctl(fd, FICLONE, s.fileno())
        except BaseException:
            os.close(fd)
            os.unlink(dst)
            raise
        os.close(fd)


def where_froms(path: Path) -> list[str]:
    """[download URL, referrer page] from the XDG attributes, as far as they are set."""
    if not hasattr(os, "getxattr"):
        return []
    out: list[str] = []
    for name in WHERE_FROM_ATTRS:
        try:
            raw = os.getxattr(path, name)
        except OSError:
            continue
        url = raw.decode("utf-8", "replace").strip("\0").strip()
        if url and url not in out:
            out.append(url)
    return out
