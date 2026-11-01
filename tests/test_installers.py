"""The platform installers' shared variant rules (apps/variants.py) and the Linux systemd unit
(apps/linux/install.py). Pure parts only: nothing here builds, installs or calls systemctl."""

import importlib.util
import sys
from pathlib import Path

import pytest

APPS = Path(__file__).resolve().parents[1] / "apps"


def _load(name: str, path: Path):
    sys.path.insert(0, str(APPS))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(APPS))


variants = _load("variants", APPS / "variants.py")
install = _load("papyrus_linux_install", APPS / "linux" / "install.py")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_ROOT", str(tmp_path / "root"))
    return tmp_path / "root"


def test_names():
    assert variants.display_name("release") == "Papyrus"
    assert variants.display_name("big-lab") == "Papyrus Big Lab"
    assert variants.variant_name("personal") == "io.github.taliesinb.papyrus.personal"
    assert variants.scheme("release") == "papyrus" and variants.scheme("personal") == "papyrus-personal"
    assert install.unit_name("release") == "io.github.taliesinb.papyrus.release.server.service"


def test_new_variants_take_the_next_port_and_colour(root, monkeypatch):
    monkeypatch.setattr(variants, "port_free", lambda port: True)
    release = variants.resolve_variant("release", None, None, "test")
    personal = variants.resolve_variant("personal", None, None, "test")
    assert (release["port"], release["servePath"], release["color"]) == (5182, "/papyrus", variants.RELEASE_COLOR)
    assert (personal["port"], personal["servePath"], personal["color"]) == (5192, "/papyrus-personal", variants.PALETTE[0])
    assert variants.read_flat_yaml(root / "personal" / "variant.yml")["port"] == 5192
    with pytest.raises(SystemExit, match="already variant release"):
        variants.resolve_variant("lab", None, 5182, "test")
    with pytest.raises(SystemExit, match="not a variant id"):
        variants.resolve_variant("../x", None, None, "test")


def test_xdg_places(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", "relative/ignored")  # the spec: relative paths are invalid
    assert install.unit_dir() == tmp_path / "cfg" / "systemd" / "user"
    assert install.data_dir("release") == tmp_path / "data" / "io.github.taliesinb.papyrus.release"
    assert install.state_dir("release") == Path.home() / ".local/state/io.github.taliesinb.papyrus.release"


def test_systemd_quoting():
    assert install.systemd_quote("/opt/my node/bin/node") == '"/opt/my node/bin/node"'
    assert install.systemd_quote('a"b\\c') == '"a\\"b\\\\c"'
    assert install.systemd_quote("50%$HOME") == '"50%%$$HOME"'
    assert install.systemd_quote("X=$HOME", dollars=False) == '"X=$HOME"'
    with pytest.raises(SystemExit):
        install.systemd_quote("two\nlines")


def test_unit_text():
    text = install.unit_text(vid="personal", node="/usr/bin/node", web=Path("/d/web"), port=5192, base="/papyrus-personal",
                             log=Path("/s/server.log"), env={"PDFTOPPM": "/usr/bin/pdftoppm", "PAPYRUS_HOME": "/tmp/p h"},
                             tailscale="/usr/bin/tailscale")
    lines = text.splitlines()
    assert lines[0] == "# Papyrus Personal Server: written by apps/linux/install.py"
    assert 'ExecStart="/usr/bin/node" "/d/web/build/index.js"' in lines
    for env in ("PORT=5192", "HOST=127.0.0.1", "PAPYRUS_VARIANT=personal", "PDFTOPPM=/usr/bin/pdftoppm", "PAPYRUS_HOME=/tmp/p h"):
        assert f'Environment="{env}"' in lines
    assert ('ExecStartPost=-"/usr/bin/tailscale" "serve" "--bg" "--yes" "--https=443" "--set-path=/papyrus-personal" '
            '"http://127.0.0.1:5192/papyrus-personal"') in lines
    assert "StandardOutput=append:/s/server.log" in lines and "WantedBy=default.target" in lines
    assert "Restart=on-failure" in lines
    # The node's own directory leads PATH (nvm, Nix), then the usual places, once each.
    path = next(l for l in lines if l.startswith('Environment="PATH='))
    assert path == 'Environment="PATH=/usr/bin:/usr/local/bin:/bin"'
    without = install.unit_text(vid="release", node="/usr/bin/node", web=Path("/d/web"), port=5182, base="/papyrus",
                                log=Path("/s/server.log"), env={}, tailscale=None)
    assert "ExecStartPost" not in without
