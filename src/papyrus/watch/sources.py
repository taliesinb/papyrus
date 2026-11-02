"""Where `papyrus watch` hears about changes: FSEvents (macOS), inotify (Linux), or polling.

Every source watches a few directories (not their subdirectories) and *trees* (directories with
their subdirectories, `[watch] dirs` entries ending in `/**`) and reports *paths that may have
changed*: a file in one of them, a directory that appeared in a tree, or a watched directory
itself when the source lost track and the whole of it should be looked at again (FSEvents'
MustScanSubDirs, inotify's queue overflow). The debouncer drops what lies outside them. Sources say nothing about whether a file is finished; `debounce.Debouncer`
decides that by watching its size and times.

All three run a background thread (FSEvents: a dispatch queue) that feeds a queue; `get` waits
for the first path, then drains what else is there. stdlib and ctypes only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import select
import struct
import sys
import threading
from pathlib import Path
from typing import Protocol


class Watcher(Protocol):
    name: str

    def start(self) -> None: ...

    def get(self, timeout: float | None) -> list[Path]:
        """Paths that may have changed, waiting up to `timeout` seconds for the first (0: don't wait)."""
        ...

    def close(self) -> None: ...


class _Queued:
    """The queue every source pushes to, and `get`."""

    name = "?"

    def __init__(self, dirs: list[Path], trees: list[Path] = ()):
        self.dirs = [Path(d) for d in dirs]
        self.trees = [Path(t) for t in trees]
        self._q: queue.Queue[Path] = queue.Queue()

    def _put(self, path: Path) -> None:
        self._q.put(path)

    def get(self, timeout: float | None) -> list[Path]:
        out: dict[Path, None] = {}
        try:
            first = self._q.get(timeout=timeout) if timeout else self._q.get_nowait()
        except queue.Empty:
            return []
        out[first] = None
        while True:
            try:
                out[self._q.get_nowait()] = None
            except queue.Empty:
                return list(out)


# --- polling ------------------------------------------------------------------------------------


def _listing(d: Path) -> dict[str, tuple[int, int, int]]:
    out = {}
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                out[e.name] = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except OSError:
        pass
    return out


def _tree_listing(root: Path) -> dict[str, tuple[int, int, int]]:
    """`_listing` of a tree: every file below it (relative names), as `papyrus scan -r` walks it."""
    from ..scan import TREE_DEPTH, skipped_dir

    out = {}
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not skipped_dir(d) and len(Path(dirpath).parts) - base < TREE_DEPTH]
        rel = Path(dirpath).relative_to(root)
        for name in filenames:
            try:
                st = os.stat(os.path.join(dirpath, name), follow_symlinks=False)
            except OSError:
                continue
            out[str(rel / name)] = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return out


class PollingWatcher(_Queued):
    """Lists each directory (each tree, walked) every `interval` seconds and reports the entries
    that changed."""

    name = "polling"

    def __init__(self, dirs: list[Path], interval: float = 5.0, trees: list[Path] = ()):
        super().__init__(dirs, trees)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _list(self, d: Path) -> dict[str, tuple[int, int, int]]:
        return _tree_listing(d) if d in self.trees else _listing(d)

    def start(self) -> None:
        roots = self.dirs + self.trees
        before = {d: self._list(d) for d in roots}

        def loop() -> None:
            nonlocal before
            while not self._stop.wait(self.interval):
                for d in roots:
                    now = self._list(d)
                    for name in now.keys() | before[d].keys():
                        if now.get(name) != before[d].get(name):
                            self._put(d / name)
                    before[d] = now

        self._thread = threading.Thread(target=loop, name="papyrus-watch-poll", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()


# --- inotify (Linux) ----------------------------------------------------------------------------

IN_ATTRIB, IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO = 0x4, 0x8, 0x40, 0x80
IN_CREATE, IN_DELETE, IN_DELETE_SELF, IN_MOVE_SELF = 0x100, 0x200, 0x400, 0x800
IN_Q_OVERFLOW, IN_IGNORED, IN_ONLYDIR, IN_ISDIR = 0x4000, 0x8000, 0x01000000, 0x40000000
IN_NONBLOCK, IN_CLOEXEC = 0o4000, 0o2000000
# IN_MODIFY is left out: it fires on every write of a download. CLOSE_WRITE, MOVED_TO and
# ATTRIB (xattrs written by the browser) say enough, and the debouncer re-checks what is pending.
INOTIFY_MASK = IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_ONLYDIR
_EVENT = struct.Struct("iIII")  # struct inotify_event: wd, mask, cookie, len; then len bytes of name


def parse_inotify(buf: bytes) -> list[tuple[int, int, str]]:
    """(wd, mask, name) of each `struct inotify_event` in `buf`."""
    out, i = [], 0
    while i + _EVENT.size <= len(buf):
        wd, mask, _cookie, n = _EVENT.unpack_from(buf, i)
        i += _EVENT.size
        name = buf[i:i + n].split(b"\0", 1)[0].decode(errors="surrogateescape")
        i += n
        out.append((wd, mask, name))
    return out


class InotifyWatcher(_Queued):
    name = "inotify"

    """inotify watches on each directory, and on every directory of each tree (added as they
    appear; a directory moved in is reported, and the debouncer walks it)."""

    def __init__(self, dirs: list[Path], trees: list[Path] = ()):
        super().__init__(dirs, trees)
        if not sys.platform.startswith("linux"):
            raise OSError("inotify is Linux-only")
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self._fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self._wds: dict[int, Path] = {}
        self._tree_wds: set[int] = set()
        for d in self.dirs:
            self._add(d, fail=True)
        for t in self.trees:
            self._add_tree(t, fail=True)
        self._stop_r, self._stop_w = os.pipe()
        self._thread: threading.Thread | None = None

    def _add(self, d: Path, *, fail: bool = False, tree: bool = False) -> None:
        wd = self._libc.inotify_add_watch(self._fd, os.fsencode(d), INOTIFY_MASK)
        if wd < 0:
            if not fail:  # a directory that went again, or the watch limit: the next scan sees it
                return
            err = ctypes.get_errno()
            os.close(self._fd)
            raise OSError(err, os.strerror(err), str(d))
        self._wds[wd] = d
        if tree:
            self._tree_wds.add(wd)

    def _add_tree(self, root: Path, *, fail: bool = False) -> None:
        from ..scan import TREE_DEPTH, skipped_dir

        self._add(root, fail=fail, tree=True)
        base = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            dirnames[:] = [d for d in dirnames if not skipped_dir(d) and len(Path(dirpath).parts) - base < TREE_DEPTH]
            for d in dirnames:
                self._add(Path(dirpath) / d, tree=True)

    def start(self) -> None:
        def loop() -> None:
            while True:
                ready, _, _ = select.select([self._fd, self._stop_r], [], [])
                if self._stop_r in ready:
                    return
                try:
                    buf = os.read(self._fd, 64 * 1024)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                for wd, mask, name in parse_inotify(buf):
                    if mask & IN_Q_OVERFLOW:  # events were dropped: look at everything again
                        for d in self.dirs + self.trees:
                            self._put(d)
                    elif wd in self._wds and name:
                        p = self._wds[wd] / name
                        if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO) and wd in self._tree_wds:
                            from ..scan import skipped_dir

                            if not skipped_dir(name):
                                self._add_tree(p)  # a new directory in a tree: watch it, and look inside
                        self._put(p)

        self._thread = threading.Thread(target=loop, name="papyrus-watch-inotify", daemon=True)
        self._thread.start()

    def close(self) -> None:
        try:
            os.write(self._stop_w, b"x")
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2)
        for fd in (self._fd, self._stop_r, self._stop_w):
            try:
                os.close(fd)
            except OSError:
                pass


# --- FSEvents (macOS) ---------------------------------------------------------------------------

kFSEventStreamEventIdSinceNow = 0xFFFFFFFFFFFFFFFF
kFSEventStreamCreateFlagNoDefer = 0x02
kFSEventStreamCreateFlagWatchRoot = 0x04
kFSEventStreamCreateFlagFileEvents = 0x10
kFSEventStreamEventFlagMustScanSubDirs = 0x01
kFSEventStreamEventFlagRootChanged = 0x20
kCFStringEncodingUTF8 = 0x08000100

_FSEventCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint64))


def _frameworks():
    cs = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    vp = ctypes.c_void_p
    cf.CFStringCreateWithCString.restype = vp
    cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFArrayCreate.restype = vp
    cf.CFArrayCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.c_long, vp]
    cf.CFRelease.argtypes = [vp]
    cs.FSEventStreamCreate.restype = vp
    cs.FSEventStreamCreate.argtypes = [vp, _FSEventCallback, vp, vp, ctypes.c_uint64, ctypes.c_double, ctypes.c_uint32]
    cs.FSEventStreamSetDispatchQueue.argtypes = [vp, vp]
    cs.FSEventStreamStart.restype = ctypes.c_bool
    cs.FSEventStreamStart.argtypes = [vp]
    for fn in ("FSEventStreamStop", "FSEventStreamInvalidate", "FSEventStreamRelease"):
        getattr(cs, fn).argtypes = [vp]
    system.dispatch_queue_create.restype = vp
    system.dispatch_queue_create.argtypes = [ctypes.c_char_p, vp]
    system.dispatch_release.argtypes = [vp]
    callbacks = ctypes.addressof(ctypes.c_void_p.in_dll(cf, "kCFTypeArrayCallBacks"))
    return cs, cf, system, callbacks


class FSEventsWatcher(_Queued):
    """An FSEvents stream with per-file events, delivered on a private dispatch queue. FSEvents
    streams cover subdirectories anyway: for plain directories the debouncer drops deeper paths."""

    name = "fsevents"

    def __init__(self, dirs: list[Path], latency: float = 0.5, trees: list[Path] = ()):
        super().__init__(dirs, trees)
        if sys.platform != "darwin":
            raise OSError("FSEvents is macOS-only")
        self.latency = latency
        self._cs, self._cf, self._system, self._array_callbacks = _frameworks()
        self._stream = self._queue = None
        self._callback = _FSEventCallback(self._on_events)  # kept alive as long as the stream

    def _on_events(self, _stream, _info, n, paths, flags, _ids) -> None:
        names = ctypes.cast(paths, ctypes.POINTER(ctypes.c_char_p))
        for i in range(n):
            p = Path(os.fsdecode(names[i]))
            if flags[i] & (kFSEventStreamEventFlagMustScanSubDirs | kFSEventStreamEventFlagRootChanged):
                for d in self.dirs + self.trees:  # coalesced or the root moved: look again at what it covers
                    if p == d or d.is_relative_to(p) or p.is_relative_to(d):
                        self._put(d)
            else:
                self._put(p)

    def start(self) -> None:
        cf = self._cf
        strings = [cf.CFStringCreateWithCString(None, os.fsencode(d), kCFStringEncodingUTF8)
                   for d in self.dirs + self.trees]
        arr = (ctypes.c_void_p * len(strings))(*strings)
        paths = cf.CFArrayCreate(None, arr, len(strings), self._array_callbacks)
        flags = kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagNoDefer | kFSEventStreamCreateFlagWatchRoot
        self._stream = self._cs.FSEventStreamCreate(None, self._callback, None, paths,
                                                    kFSEventStreamEventIdSinceNow, self.latency, flags)
        for s in strings:
            cf.CFRelease(s)
        cf.CFRelease(paths)
        if not self._stream:
            raise OSError("FSEventStreamCreate failed")
        self._queue = self._system.dispatch_queue_create(b"papyrus.watch.fsevents", None)
        self._cs.FSEventStreamSetDispatchQueue(self._stream, self._queue)
        if not self._cs.FSEventStreamStart(self._stream):
            self.close()
            raise OSError("FSEventStreamStart failed")

    def close(self) -> None:
        if self._stream:
            self._cs.FSEventStreamStop(self._stream)
            self._cs.FSEventStreamInvalidate(self._stream)
            self._cs.FSEventStreamRelease(self._stream)
            self._stream = None
        if self._queue:
            self._system.dispatch_release(self._queue)
            self._queue = None


def make_watcher(dirs: list[Path], kind: str = "auto", trees: list[Path] = ()) -> Watcher:
    """FSEvents on macOS, inotify on Linux, polling elsewhere, when asked (`kind='polling'`), or
    when the native source can't start."""
    if kind == "polling":
        return PollingWatcher(dirs, trees=trees)
    try:
        if sys.platform == "darwin":
            return FSEventsWatcher(dirs, trees=trees)
        if sys.platform.startswith("linux"):
            return InotifyWatcher(dirs, trees=trees)
    except OSError as e:
        print(f"watch: native file events unavailable ({type(e).__name__}); polling instead", file=sys.stderr)
    return PollingWatcher(dirs, trees=trees)
