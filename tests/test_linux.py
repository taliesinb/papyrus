"""Linux counterparts of the macOS file and clipboard code (src/papyrus/linux.py, download.py).

The xattr and reflink tests need Linux (and, for reflink, a filesystem with clones); the clipboard
test fakes the tools, so it runs anywhere.
"""

import errno
import os
import subprocess
import sys

import pytest

from papyrus import download, linux, store

on_linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


@on_linux
def test_where_froms_reads_the_xdg_attributes(tmp_path):
    f = tmp_path / "paper.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    try:
        os.setxattr(f, "user.xdg.origin.url", b"https://arxiv.org/pdf/2401.00001v2")
    except OSError as e:
        if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM):
            pytest.skip(f"no user xattrs on {tmp_path}")
        raise
    assert linux.where_froms(f) == ["https://arxiv.org/pdf/2401.00001v2"]
    os.setxattr(f, "user.xdg.referrer.url", b"https://arxiv.org/abs/2401.00001\0")  # some writers add the NUL
    assert linux.where_froms(f) == ["https://arxiv.org/pdf/2401.00001v2", "https://arxiv.org/abs/2401.00001"]


def test_where_froms_without_attributes(tmp_path):
    f = tmp_path / "plain.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    assert linux.where_froms(f) == []


@on_linux
def test_reflink_clones_or_refuses_cleanly(tmp_path):
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    src.write_bytes(b"%PDF-1.4\n" + os.urandom(4096))
    try:
        linux.reflink(src, dst)
    except OSError as e:
        assert not dst.exists()  # a refusal (tmpfs, ext4) leaves nothing behind
        assert e.errno in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL, errno.EXDEV, errno.ENOTTY)
        return
    assert dst.read_bytes() == src.read_bytes()
    with pytest.raises(FileExistsError):
        linux.reflink(src, dst)


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="checks the refusal elsewhere")
def test_reflink_off_linux(tmp_path):
    with pytest.raises(NotImplementedError):
        linux.reflink(tmp_path / "a", tmp_path / "b")


def test_snapshot_stores_the_bytes_by_clone_or_copy(tmp_path):
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4\nhello")
    tmp, how = store.snapshot(src)
    assert how in ("clone", "copy") and tmp.read_bytes() == src.read_bytes()
    tmp.unlink()


def test_linux_clipboard_prefers_html_links(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        mime = cmd[cmd.index("--type") + 1]
        out = {
            "text/html": b'<a href="https://arxiv.org/abs/2401.00001">Attention, again</a>',
            "text/plain;charset=utf-8": b"Attention, again\nhttps://arxiv.org/abs/2401.00001\nhttps://example.org/x.pdf\n",
        }.get(mime, b"")
        return subprocess.CompletedProcess(cmd, 0 if out else 1, out, b"")

    monkeypatch.setattr(download.subprocess, "run", fake_run)
    links, flavour = download.read_clipboard()
    assert [link.url for link in links] == ["https://arxiv.org/abs/2401.00001", "https://example.org/x.pdf"]
    assert links[0].title == "Attention, again"
    assert flavour == "html+text"
    assert all(c[0] == "wl-paste" for c in calls)


def test_linux_clipboard_without_a_session(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(download.subprocess, "run", lambda *a, **k: pytest.fail("no tool should run"))
    assert download.read_clipboard() == ([], "empty")
