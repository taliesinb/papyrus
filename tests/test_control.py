"""`papyrus start | stop | open | status | pause | resume` (control.py), with a fake command runner:
no app is launched, no unit started."""

import signal
import subprocess

import pytest
from click.testing import CliRunner

from papyrus import control, db
from papyrus.cli import main


class Runner:
    """Answers lsappinfo / open / systemctl / xdg-open like the real ones; records the commands."""

    def __init__(self, running=(), fail=()):
        self.running = dict(running)  # bundle id -> pid
        self.fail = set(fail)
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        out, code = "", 0
        if cmd[:2] == ["lsappinfo", "find"]:
            bid = cmd[2].split("=", 1)[1]
            out = f'ASN:0x0-0x1-"{bid}":' if bid in self.running else ""
        elif cmd[:2] == ["lsappinfo", "info"]:
            bid = cmd[-1].split('"')[1]
            out = f"pid = {self.running[bid]} !cgsConnection" if bid in self.running else ""
        elif cmd[0] in self.fail:
            code = 1
        return subprocess.CompletedProcess(cmd, code, out, "no such app" if code else "")


def _variant(tmp_path, vid="personal", port=5192):
    home = tmp_path / vid
    home.mkdir()
    (home / "variant.yml").write_text(f"# x\nid: {vid}\nport: {port}\nservePath: /papyrus-{vid}\nwatch: true\n")
    return control.Variant(vid, home, port, f"/papyrus-{vid}")


def test_the_variant_comes_from_variant_yml(monkeypatch, tmp_path):
    home = db.library_home()
    home.mkdir(parents=True)
    (home / "variant.yml").write_text("port: 5202\nservePath: /papyrus-x\n")
    v = control.variant()
    assert (v.id, v.port, v.base, v.url) == ("release", 5202, "/papyrus-x", "http://127.0.0.1:5202/papyrus-x/")
    assert (v.app_id, v.server_id) == ("io.github.taliesinb.papyrus.release", "io.github.taliesinb.papyrus.release.server")


def test_macos_start_stop_open(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "macos", lambda: True)
    v = _variant(tmp_path)
    run = Runner()
    assert control.start(v, run).startswith("started io.github.taliesinb.papyrus.personal.server")
    assert ["open", "-g", "-b", "io.github.taliesinb.papyrus.personal.server"] in run.cmds
    assert "not running" in control.stop(v, run)
    run.running[v.server_id] = 4242
    assert "already running (pid 4242)" in control.start(v, run)
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))
        run.running.pop(v.server_id)  # it quits

    assert control.stop(v, run, kill=kill, sleep=lambda s: None) == f"stopped {v.server_id}"
    assert killed == [(4242, signal.SIGTERM)]
    assert control.open_viewer(v, run) == f"opened {v.app_id}" and ["open", "-b", v.app_id] in run.cmds
    with pytest.raises(control.ControlError, match="build.py --install"):
        control.open_viewer(v, Runner(fail={"open"}))


def test_macos_stop_reports_an_app_that_wont_quit(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "macos", lambda: True)
    v = _variant(tmp_path)
    run = Runner(running={v.server_id: 7})
    with pytest.raises(control.ControlError, match="still running"):
        control.stop(v, run, wait_s=0.01, kill=lambda pid, sig: None, sleep=lambda s: None)


def test_linux_units(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "macos", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    v = _variant(tmp_path)
    with pytest.raises(control.ControlError, match="apps/linux/install.py --variant personal"):
        control.start(v, Runner())
    d = control.unit_dir()
    d.mkdir(parents=True)
    (d / f"{v.name}.server.service").write_text("")
    (d / f"{v.name}.watch.service").write_text("")
    run = Runner()
    control.start(v, run)
    control.stop(v, run)
    control.open_viewer(v, run)
    units = [f"{v.name}.server.service", f"{v.name}.watch.service"]
    assert run.cmds == [["systemctl", "--user", "start", *units], ["systemctl", "--user", "stop", *units],
                        ["xdg-open", "http://127.0.0.1:5192/papyrus-personal/"]]


def test_pause_resume_and_status(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "macos", lambda: True)
    db.library_home().mkdir(parents=True)
    r = CliRunner().invoke(main, ["pause"])
    assert r.exit_code == 0 and "ingestion paused" in r.output and (db.library_home() / "watch.pause").exists()
    monkeypatch.setattr(control, "_run", Runner())  # (not the real lsappinfo: it would find your apps)
    monkeypatch.setattr(control, "server_answers", lambda v, timeout=1.5: None)
    r = CliRunner().invoke(main, ["status"])
    assert r.exit_code == 0, r.output
    assert "server app  not running" in r.output and "web server  not answering" in r.output
    assert "watcher     stopped" in r.output and "ingestion   paused" in r.output
    assert "ingestion resumed" in CliRunner().invoke(main, ["resume"]).output
    assert not (db.library_home() / "watch.pause").exists()
    assert "wasn't paused" in CliRunner().invoke(main, ["resume"]).output
