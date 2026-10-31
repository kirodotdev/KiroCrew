"""agent-desktop installer: placement, pinned verify, readiness, npm argv, permissions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from kiro_crew.computer_use import agent_desktop_install as adi
from kiro_crew.computer_use import agent_desktop_locate

_NATIVE = b"\x7fELFnative-binary"
_HELPER = b"\x7fELFhelper-binary"


@pytest.fixture()
def prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ad"
    monkeypatch.setattr(agent_desktop_locate, "INSTALL_PREFIX", lambda: root)
    monkeypatch.delenv(agent_desktop_locate.ENV_BIN, raising=False)
    monkeypatch.setattr(agent_desktop_locate.shutil, "which", lambda name: None)
    monkeypatch.setattr(adi, "platform_binary_name", lambda: "agent-desktop-darwin-arm64")
    monkeypatch.setattr(
        adi, "_helper_binary_name", lambda: "agent-desktop-macos-helper-darwin-arm64"
    )
    # The pin is the real verify's; point it at the fake bytes this test writes.
    monkeypatch.setattr(
        agent_desktop_locate,
        "_KNOWN_DIGESTS",
        frozenset({hashlib.sha256(_NATIVE).hexdigest(), hashlib.sha256(_HELPER).hexdigest()}),
    )
    return root


def _npm_tree(prefix: Path, *, version: str = "1.0.2", native: bytes = _NATIVE) -> Path:
    """What ``npm install --ignore-scripts`` leaves: a Node wrapper plus bundled binaries."""
    pkg = prefix / "node_modules" / "agent-desktop"
    bin_dir = pkg / "bin"
    bin_dir.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (bin_dir / "agent-desktop").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    os.chmod(bin_dir / "agent-desktop", 0o755)
    (bin_dir / "agent-desktop-darwin-arm64").write_bytes(native)
    (bin_dir / "agent-desktop-macos-helper-darwin-arm64").write_bytes(_HELPER)
    link_dir = prefix / "node_modules" / ".bin"
    link_dir.mkdir()
    (link_dir / "agent-desktop").symlink_to("../agent-desktop/bin/agent-desktop")
    return link_dir / "agent-desktop"


def _installed(prefix: Path, **kw) -> str:
    _npm_tree(prefix, **kw)
    assert adi.place_platform_binary()[0]
    path = agent_desktop_locate.locate()
    assert path is not None
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX npm .bin symlink layout")
class TestPlacementAndVerify:
    def test_the_wrapper_alone_does_not_verify(self, prefix):
        _npm_tree(prefix)
        path, reason = adi.verify_installed()
        assert path is None and "SHA-256 mismatch" in reason

    def test_placing_the_bundled_binary_makes_the_link_verify(self, prefix):
        _npm_tree(prefix)
        ok, _ = adi.place_platform_binary()
        assert ok
        bin_dir = prefix / "node_modules/agent-desktop/bin"
        assert (bin_dir / "agent-desktop").read_bytes() == _NATIVE
        assert (bin_dir / "agent-desktop-macos-helper").read_bytes() == _HELPER
        assert os.access(bin_dir / "agent-desktop", os.X_OK)
        path, reason = adi.verify_installed()
        assert reason is None and path == str((bin_dir / "agent-desktop").resolve())

    def test_a_binary_outside_the_pin_fails(self, prefix):
        _installed(prefix, native=b"tampered")
        path, reason = adi.verify_installed()
        assert path is None and "mismatch" in reason

    def test_a_missing_bundled_binary_cannot_be_placed(self, prefix):
        _npm_tree(prefix)
        (prefix / "node_modules/agent-desktop/bin/agent-desktop-darwin-arm64").unlink()
        ok, detail = adi.place_platform_binary()
        assert not ok and "missing" in detail

    def test_an_unknown_platform_cannot_be_placed(self, prefix, monkeypatch):
        monkeypatch.setattr(adi, "platform_binary_name", lambda: None)
        assert adi.place_platform_binary()[0] is False


@pytest.mark.parametrize(
    ("binary", "helper"),
    [
        ("agent-desktop-darwin-arm64", "agent-desktop-macos-helper-darwin-arm64"),
        ("agent-desktop-linux-x64", "agent-desktop-linux-clipboard-owner-linux-x64"),
        ("agent-desktop-win32-x64.exe", None),
    ],
)
def test_helper_names_follow_the_tarball(monkeypatch, binary, helper):
    monkeypatch.setattr(adi, "platform_binary_name", lambda: binary)
    assert adi._helper_binary_name() == helper
    if helper is not None:
        assert helper in agent_desktop_locate.PINNED_SHA256


class TestPermissions:
    def test_the_real_cli_shape_maps_to_two_rows(self):
        out = json.dumps(
            {
                "command": "permissions",
                "ok": True,
                "data": {
                    "accessibility": {"state": "granted"},
                    "automation": {"state": "unknown"},
                    "screen_recording": {"state": "denied"},
                },
            }
        )
        assert adi.parse_permissions(out) == {
            "accessibility": "granted",
            "screen_recording": "denied",
        }

    @pytest.mark.parametrize("out", ["", "not json", '{"ok": false}', '{"ok": true, "data": 3}'])
    def test_anything_else_is_unknown_not_denied(self, out):
        assert adi.parse_permissions(out) == {
            "accessibility": "unknown",
            "screen_recording": "unknown",
        }

    def test_a_failing_probe_is_unknown(self, monkeypatch):
        monkeypatch.setattr(adi, "_run", lambda argv, timeout: (1, "", "boom"))
        assert adi.probe_permissions("/x")["accessibility"] == "unknown"


@pytest.mark.skipif(os.name == "nt", reason="POSIX npm .bin symlink layout")
class TestReadiness:
    def test_not_installed(self, prefix):
        ready = adi.readiness()
        assert ready["installed"] is False and ready["path"] is None
        assert ready["pinned"] == agent_desktop_locate.PINNED_VERSION

    def test_installed_and_verified_probes_permissions(self, prefix, monkeypatch):
        path = _installed(prefix)
        calls = []

        def fake_run(argv, timeout):
            calls.append(argv)
            return 0, '{"ok": true, "data": {"accessibility": {"state": "granted"}}}', ""

        monkeypatch.setattr(adi, "_run", fake_run)
        ready = adi.readiness()
        assert ready["installed"] is True
        assert ready["version"] == "1.0.2"
        assert ready["path"] == path
        assert ready["permissions"]["accessibility"] == "granted"
        assert calls == [[path, "permissions"]]

    def test_an_unverified_binary_reads_as_not_installed_and_is_never_run(
        self, prefix, monkeypatch
    ):
        _installed(prefix, native=b"tampered")
        monkeypatch.setattr(adi, "_run", lambda *a: pytest.fail("ran an unverified binary"))
        ready = adi.readiness()
        assert ready["installed"] is False and ready["path"] is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX npm .bin symlink layout")
class TestInstall:
    def _npm(self, monkeypatch, prefix, seen, **tree):
        monkeypatch.setattr(adi, "find_node_tool", lambda name: "/usr/bin/npm")

        def fake_run(argv, timeout):
            seen.append(argv)
            _npm_tree(prefix, **tree)
            return 0, "added 1 package", ""

        monkeypatch.setattr(adi, "_run", fake_run)

    def test_a_pinned_install_without_scripts_verifies_and_resets_the_backend(
        self, prefix, monkeypatch
    ):
        from kiro_crew.computer_use import backend

        seen: list[list[str]] = []
        resets = []
        self._npm(monkeypatch, prefix, seen)
        monkeypatch.setattr(backend, "reset_shared_backend", lambda: resets.append(1))
        logs: list[str] = []
        result = adi.install(logs.append)
        assert result["ok"], result
        argv = seen[0]
        assert argv[:4] == ["/usr/bin/npm", "install", "--prefix", str(prefix)]
        assert "--ignore-scripts" in argv
        assert argv[-1] == f"agent-desktop@{agent_desktop_locate.PINNED_VERSION}"
        assert not any("registry" in a for a in argv)
        assert (prefix / "state").is_dir()
        assert resets == [1]
        assert any("verified" in line for line in logs)

    def test_no_npm_fails_with_a_remedy(self, prefix, monkeypatch):
        monkeypatch.setattr(adi, "find_node_tool", lambda name: None)
        result = adi.install()
        assert not result["ok"] and "Node.js" in result["error"]

    def test_npm_failure_is_redacted(self, prefix, monkeypatch):
        monkeypatch.setattr(adi, "find_node_tool", lambda name: "/usr/bin/npm")
        monkeypatch.setattr(
            adi, "_run", lambda argv, timeout: (1, "", "E401 //reg/:_authToken=s3cr3tvalue")
        )
        result = adi.install()
        assert not result["ok"] and result["step"] == "npm-install"
        assert "s3cr3tvalue" not in result["error"]

    def test_a_binary_outside_the_pin_fails_and_does_not_reset(self, prefix, monkeypatch):
        from kiro_crew.computer_use import backend

        self._npm(monkeypatch, prefix, [], native=b"tampered")
        monkeypatch.setattr(backend, "reset_shared_backend", lambda: pytest.fail("reset"))
        result = adi.install()
        assert not result["ok"] and result["step"] == "verify"

    def test_a_wrong_version_fails_the_install(self, prefix, monkeypatch):
        self._npm(monkeypatch, prefix, [], version="9.9.9")
        result = adi.install()
        assert not result["ok"] and result["step"] == "version"


@pytest.mark.skipif(os.name == "nt", reason="POSIX npm .bin symlink layout")
class TestGuide:
    def test_guide_runs_permissions_guide_on_macos(self, prefix, monkeypatch):
        path = _installed(prefix)
        monkeypatch.setattr(adi.sys, "platform", "darwin")
        seen = []
        monkeypatch.setattr(adi, "_run", lambda argv, timeout: (seen.append(argv), (0, "", ""))[1])
        assert adi.open_permissions_guide() == {"ok": True, "error": None}
        assert seen == [[path, "permissions", "--guide"]]

    def test_guide_is_refused_off_macos(self, monkeypatch):
        monkeypatch.setattr(adi.sys, "platform", "linux")
        assert adi.open_permissions_guide()["ok"] is False

    def test_guide_refuses_when_not_installed(self, prefix, monkeypatch):
        monkeypatch.setattr(adi.sys, "platform", "darwin")
        assert "not installed" in adi.open_permissions_guide()["error"]
