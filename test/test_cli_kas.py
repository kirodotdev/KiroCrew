"""``kirocrew kas install|status|remove`` -- the operator CLI over the engine bundle.

What is pinned, per subcommand:

* ``install``: the pinned version installs from a local ``--from`` directory and from
  a manifest fetched over the URL; a version the data home already holds is a
  success with no fetch (``--force`` replaces it, a copy that does not read back
  still reaches the installer); a manifest that does not hash to the pin, or any
  other version, is refused without ``--unpinned``; an unreadable or malformed local
  manifest is a one-line refusal; an installer error is a one-line refusal; the
  closing advice names the route's state (on / off) and the override env when the
  installed version is not the one the harness runs.
* ``status``: the pin, the route, the root, what is installed and what else is on
  disk; exit 1 only when the route is on and nothing is installed.
* ``remove``: one version or every version, with a confirmation the ``--yes`` flag
  skips; a version that is not installed is refused, and an empty root is a quiet
  no-op. The blank-``--version``, closed-stdin and failed-delete paths are in
  ``test_cli_kas_remove_guard``.
* The parser wires every flag the handlers read.

The installer itself is tested in ``test_kas_bundle``; here it is patched through the
``agent_sdk.kas_bundle`` facade, which is the surface this CLI reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import cli_kas
from kiro_crew.agent_sdk import kas_bundle

_PIN = cli_kas.KAS_PIN_VERSION


def _installed(root: Path, version: str = _PIN) -> kas_bundle.InstalledKasBundle:
    return kas_bundle.InstalledKasBundle(
        version=version,
        node_version="22.23.3",
        root=root / version,
        node_bin=root / version / "node" / "bin" / "node",
        acp_server=root / version / "bundle" / "acp-server.js",
    )


def _manifest_doc(version: str = _PIN) -> dict:
    return {
        "version": version,
        "package": "@kiro/agent",
        "platforms": {"linux-x64": {"file": "kas-linux-x64.tar.xz", "sha256": "0" * 64, "size": 1}},
        "node": {
            "version": "22.23.3",
            "builds": {
                "linux-x64": {
                    "file": "node-v22.23.3-linux-x64.tar.xz",
                    "sha256": "1" * 64,
                    "size": 1,
                }
            },
        },
    }


@pytest.fixture
def cfg(monkeypatch):
    agent = SimpleNamespace(kas_bundle=False, kas_bundle_url="https://kas.test")
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        classmethod(lambda cls: SimpleNamespace(agent=agent)),
    )
    return agent


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(kas_bundle, "host_platform_key", lambda: "linux-x64")
    monkeypatch.setattr(kas_bundle, "host_node_key", lambda: "linux-x64")


@pytest.fixture
def local_source(tmp_path, monkeypatch):
    """A ``--from`` directory whose manifest IS the pinned one, by patching the pin."""
    src = tmp_path / "src"
    src.mkdir()
    raw = json.dumps(_manifest_doc()).encode()
    (src / kas_bundle.KAS_BUNDLE_MANIFEST_NAME).write_bytes(raw)
    monkeypatch.setattr(cli_kas, "KAS_PIN_MANIFEST_SHA256", hashlib.sha256(raw).hexdigest())
    return src


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kirocrew kas")
    cli_kas.add_kas_parser(parser)
    return parser.parse_args(argv)


class TestParser:
    def test_install_flags_reach_the_namespace(self):
        args = _parse(
            "install",
            "--version",
            "0.1.0",
            "--unpinned",
            "--url",
            "https://x",
            "--from",
            "d",
            "--force",
        )
        assert args.kas_action == "install"
        assert (args.version, args.unpinned, args.url, args.from_dir, args.force) == (
            "0.1.0",
            True,
            "https://x",
            "d",
            True,
        )

    def test_status_and_remove(self):
        assert _parse("status").kas_action == "status"
        args = _parse("remove", "--version", "0.1.0", "--yes")
        assert (args.kas_action, args.version, args.yes) == ("remove", "0.1.0", True)

    def test_no_action_is_usage(self, capsys):
        assert cli_kas.kas_cmd(argparse.Namespace()) == 2
        assert "usage: kirocrew kas" in capsys.readouterr().err


class TestInstallFromDir:
    def _args(self, src: Path, **over) -> argparse.Namespace:
        base = dict(
            kas_action="install",
            from_dir=str(src),
            version=None,
            url=None,
            unpinned=False,
            force=False,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def test_the_pinned_manifest_installs_from_the_directory(
        self, cfg, host, local_source, tmp_path, monkeypatch, capsys
    ):
        seen: dict = {}

        def fake_install(manifest, *, source, force, on_progress):
            seen.update(version=manifest.version, source=source, force=force)
            on_progress("engine", 0, 100)
            on_progress("engine", 55, 100)
            on_progress("engine", 56, 100)  # same decile: not printed twice
            on_progress("engine", 100, 100)
            return _installed(tmp_path / "engines")

        monkeypatch.setattr(kas_bundle, "install_bundle", fake_install)
        assert cli_kas.kas_cmd(self._args(local_source, force=True)) == 0
        assert seen == {"version": _PIN, "source": str(local_source.resolve()), "force": True}
        out = capsys.readouterr().out
        assert f"✅ KAS {_PIN} installed" in out
        assert out.count("engine:") >= 1
        assert "  engine:   0%" in out and "  engine:  55%" in out and "  engine: 100%" in out
        assert "  engine:  56%" not in out
        # Route off: the closing advice says how to turn it on.
        assert "kirocrew config set agent.kas_bundle true" in out
        assert "kiro-cli is not used on this route" in out

    def test_route_on_names_the_restart_only(
        self, cfg, host, local_source, tmp_path, monkeypatch, capsys
    ):
        cfg.kas_bundle = True
        monkeypatch.setattr(kas_bundle, "install_bundle", lambda m, **k: _installed(tmp_path))
        assert cli_kas.kas_cmd(self._args(local_source)) == 0
        out = capsys.readouterr().out
        assert "agent.kas_bundle is on; `kirocrew restart` picks the new engine up." in out
        assert "config set agent.kas_bundle true" not in out

    def test_a_version_the_harness_does_not_run_names_the_override_env(
        self, cfg, host, local_source, tmp_path, monkeypatch, capsys
    ):
        other = "0.99.0"
        (local_source / kas_bundle.KAS_BUNDLE_MANIFEST_NAME).write_text(
            json.dumps(_manifest_doc(other))
        )
        monkeypatch.setattr(
            kas_bundle, "install_bundle", lambda m, **k: _installed(tmp_path, other)
        )
        args = self._args(local_source, version=other, unpinned=True)
        assert cli_kas.kas_cmd(args) == 0
        out = capsys.readouterr().out
        assert f"{cli_kas.KAS_VERSION_OVERRIDE_ENV}={other}" in out

    def test_missing_manifest_is_a_refusal(self, cfg, tmp_path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert cli_kas.kas_cmd(self._args(empty)) == 1
        assert "not found" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "payload",
        [b"", b"{not json", b"\xff\xfe\x00broken"],
        ids=["empty", "truncated", "not-utf8"],
    )
    def test_a_malformed_manifest_is_a_refusal_not_a_crash(
        self, cfg, tmp_path, monkeypatch, capsys, payload
    ):
        src = tmp_path / "bundle"
        src.mkdir()
        (src / kas_bundle.KAS_BUNDLE_MANIFEST_NAME).write_bytes(payload)
        monkeypatch.setattr(
            kas_bundle, "install_bundle", lambda *a, **k: pytest.fail("install_bundle reached")
        )
        assert cli_kas.kas_cmd(self._args(src)) == 1
        err = capsys.readouterr().err
        assert "not a readable JSON manifest" in err
        assert "Traceback" not in err

    def test_a_manifest_for_another_version_than_asked_is_refused(self, cfg, local_source, capsys):
        assert cli_kas.kas_cmd(self._args(local_source, version="0.99.0", unpinned=True)) == 1
        assert f"describes KAS {_PIN}, not 0.99.0" in capsys.readouterr().err

    def test_a_manifest_that_does_not_hash_to_the_pin_is_refused_without_unpinned(
        self, cfg, local_source, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_kas, "KAS_PIN_MANIFEST_SHA256", "f" * 64)
        monkeypatch.setattr(
            kas_bundle, "install_bundle", lambda *a, **k: pytest.fail("install_bundle reached")
        )
        assert cli_kas.kas_cmd(self._args(local_source)) == 1
        assert "pass --unpinned to install it anyway" in capsys.readouterr().err

    def test_an_installer_error_is_a_one_line_refusal(
        self, cfg, host, local_source, monkeypatch, capsys
    ):
        def boom(*a, **k):
            raise kas_bundle.KasBundleError("node did not run: exit 127")

        monkeypatch.setattr(kas_bundle, "install_bundle", boom)
        assert cli_kas.kas_cmd(self._args(local_source)) == 1
        err = capsys.readouterr().err
        assert "❌ node did not run: exit 127" in err
        assert "Traceback" not in err


class TestInstallFromUrl:
    def _args(self, **over) -> argparse.Namespace:
        base = dict(
            kas_action="install", from_dir=None, version=None, url=None, unpinned=False, force=False
        )
        base.update(over)
        return argparse.Namespace(**base)

    def test_fetches_the_pinned_manifest_from_the_configured_base(
        self, cfg, host, tmp_path, monkeypatch, capsys
    ):
        seen: dict = {}

        def fake_fetch(base, *, version, sha256):
            seen.update(base=base, version=version, sha256=sha256)
            return kas_bundle.parse_manifest(_manifest_doc()), f"{base}/kas/{version}/manifest.json"

        monkeypatch.setattr(kas_bundle, "fetch_manifest", fake_fetch)
        monkeypatch.setattr(
            kas_bundle,
            "install_bundle",
            lambda m, *, source, force, on_progress: (
                _installed(tmp_path) if seen.update(source=source) is None else None
            ),
        )
        assert cli_kas.kas_cmd(self._args()) == 0
        assert seen == {
            "base": "https://kas.test",
            "version": _PIN,
            "sha256": cli_kas.KAS_PIN_MANIFEST_SHA256,
            "source": f"https://kas.test/kas/{_PIN}/manifest.json",
        }
        assert f"Fetching manifest for KAS {_PIN} from https://kas.test" in capsys.readouterr().out

    def test_url_flag_and_unpinned_drop_the_digest(self, cfg, host, tmp_path, monkeypatch):
        seen: dict = {}

        def fake_fetch(base, *, version, sha256):
            seen.update(base=base, version=version, sha256=sha256)
            return kas_bundle.parse_manifest(_manifest_doc("0.99.0")), "u"

        monkeypatch.setattr(kas_bundle, "fetch_manifest", fake_fetch)
        monkeypatch.setattr(
            kas_bundle, "install_bundle", lambda m, **k: _installed(tmp_path, "0.99.0")
        )
        assert (
            cli_kas.kas_cmd(self._args(version="0.99.0", unpinned=True, url="https://mirror.test"))
            == 0
        )
        assert seen == {"base": "https://mirror.test", "version": "0.99.0", "sha256": None}

    def test_another_version_without_unpinned_is_refused_before_any_fetch(
        self, cfg, monkeypatch, capsys
    ):
        monkeypatch.setattr(kas_bundle, "fetch_manifest", lambda *a, **k: pytest.fail("fetched"))
        assert cli_kas.kas_cmd(self._args(version="0.99.0")) == 1
        assert f"this release pins KAS {_PIN}; pass --unpinned" in capsys.readouterr().err


class TestInstallWhenAlreadyInstalled:
    """A re-run (an installer run on an upgrade) is a success, not a refusal."""

    def _args(self, **over) -> argparse.Namespace:
        base = dict(
            kas_action="install", from_dir=None, version=None, url=None, unpinned=False, force=False
        )
        base.update(over)
        return argparse.Namespace(**base)

    @pytest.fixture
    def present(self, tmp_path, monkeypatch):
        asked: list = []

        def fake_installed_bundle(*, root=None, version=None):
            asked.append((root, version))
            return _installed(tmp_path / "engines", version or _PIN)

        monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: tmp_path / "engines")
        monkeypatch.setattr(kas_bundle, "installed_bundle", fake_installed_bundle)
        return asked

    def test_the_pinned_engine_on_disk_succeeds_without_a_fetch(
        self, cfg, present, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(kas_bundle, "fetch_manifest", lambda *a, **k: pytest.fail("fetched"))
        monkeypatch.setattr(kas_bundle, "install_bundle", lambda *a, **k: pytest.fail("installed"))
        assert cli_kas.kas_cmd(self._args()) == 0
        # Only the data home's own root is asked, never the app's staged copy.
        assert present == [(tmp_path / "engines", _PIN)]
        out = capsys.readouterr().out
        assert f"✅ KAS {_PIN} is already installed at {tmp_path / 'engines' / _PIN}" in out
        assert "kirocrew config set agent.kas_bundle true" in out

    def test_from_a_directory_the_installed_pin_is_not_reinstalled(
        self, cfg, present, local_source, monkeypatch, capsys
    ):
        monkeypatch.setattr(kas_bundle, "install_bundle", lambda *a, **k: pytest.fail("installed"))
        assert cli_kas.kas_cmd(self._args(from_dir=str(local_source))) == 0
        assert "is already installed" in capsys.readouterr().out

    def test_force_still_replaces_it(self, cfg, host, present, tmp_path, monkeypatch, capsys):
        seen: dict = {}
        monkeypatch.setattr(
            kas_bundle,
            "fetch_manifest",
            lambda base, *, version, sha256: (kas_bundle.parse_manifest(_manifest_doc()), "u"),
        )

        def fake_install(manifest, *, source, force, on_progress):
            seen.update(force=force)
            return _installed(tmp_path / "engines")

        monkeypatch.setattr(kas_bundle, "install_bundle", fake_install)
        assert cli_kas.kas_cmd(self._args(force=True)) == 0
        assert present == []
        assert seen == {"force": True}
        assert "already installed" not in capsys.readouterr().out

    def test_a_directory_that_does_not_read_back_still_reaches_the_installer(
        self, cfg, host, tmp_path, monkeypatch
    ):
        # A damaged copy is not "installed": the installer's own refusal names --force.
        monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: tmp_path / "engines")
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
        monkeypatch.setattr(
            kas_bundle,
            "fetch_manifest",
            lambda base, *, version, sha256: (kas_bundle.parse_manifest(_manifest_doc()), "u"),
        )

        def refuse(*a, **k):
            raise kas_bundle.KasBundleError("already installed (use --force to replace it)")

        monkeypatch.setattr(kas_bundle, "install_bundle", refuse)
        assert cli_kas.kas_cmd(self._args()) == 1


class TestStatus:
    def _run(self, capsys) -> tuple[int, str]:
        code = cli_kas.kas_cmd(argparse.Namespace(kas_action="status"))
        return code, capsys.readouterr().out

    def test_nothing_installed_route_off_is_fine(self, cfg, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: tmp_path)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda *, root: [])
        code, out = self._run(capsys)
        assert code == 0
        assert "bundle route:  off" in out
        assert f"pinned:        KAS {_PIN}; runs {_PIN}" in out
        assert "manifest base: https://kas.test" in out
        assert f"engines root:  {tmp_path}" in out
        assert f"installed:     KAS {_PIN} is not installed" in out

    def test_nothing_installed_route_on_is_an_error(self, cfg, tmp_path, monkeypatch, capsys):
        cfg.kas_bundle = True
        monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: tmp_path)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda *, root: [])
        code, out = self._run(capsys)
        assert code == 1
        assert "bundle route:  on" in out

    def test_installed_names_the_engine_and_the_other_versions_on_disk(
        self, cfg, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: tmp_path)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: _installed(tmp_path))
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda *, root: [_PIN, "0.60.0"])
        code, out = self._run(capsys)
        assert code == 0
        assert f"installed:     KAS {_PIN}, Node 22.23.3" in out
        assert "also on disk:  0.60.0  (`kirocrew kas remove --version V`)" in out
        assert str(tmp_path / _PIN / "bundle" / "acp-server.js") in out

    def test_the_override_env_is_named_when_it_moves_the_pin(
        self, cfg, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(cli_kas, "pinned_version", lambda: "0.99.0")
        monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: tmp_path)
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **k: None)
        monkeypatch.setattr(kas_bundle, "installed_versions", lambda *, root: [])
        _, out = self._run(capsys)
        assert f"runs 0.99.0  ({cli_kas.KAS_VERSION_OVERRIDE_ENV} override)" in out


class TestRemove:
    @pytest.fixture
    def root(self, tmp_path, monkeypatch) -> Path:
        root = tmp_path / "engines" / "kas"
        for v in (_PIN, "0.60.0"):
            (root / v).mkdir(parents=True)
            (root / v / "install.json").write_text("{}")
        monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: root)
        monkeypatch.setattr(
            kas_bundle,
            "installed_versions",
            lambda *, root: sorted(p.name for p in root.iterdir()) if root.exists() else [],
        )
        return root

    def _args(self, **over) -> argparse.Namespace:
        base = dict(kas_action="remove", version=None, yes=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_one_version_with_yes(self, root, capsys):
        assert cli_kas.kas_cmd(self._args(version="0.60.0", yes=True)) == 0
        assert not (root / "0.60.0").exists() and (root / _PIN).exists()
        assert f"removed {root / '0.60.0'}" in capsys.readouterr().out

    def test_an_absent_version_is_refused(self, root, capsys):
        assert cli_kas.kas_cmd(self._args(version="9.9.9", yes=True)) == 1
        assert "KAS 9.9.9 is not installed" in capsys.readouterr().err
        assert (root / _PIN).exists()

    def test_everything_asks_first_and_a_no_aborts(self, root, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert cli_kas.kas_cmd(self._args()) == 1
        assert "aborted" in capsys.readouterr().out
        assert root.exists()

    def test_everything_with_a_yes_answer_removes_the_root(self, root, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda prompt: "yes")
        assert cli_kas.kas_cmd(self._args()) == 0
        assert not root.exists()

    def test_an_empty_root_is_nothing_installed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda: tmp_path / "absent")
        assert cli_kas.kas_cmd(self._args(yes=True)) == 0
        assert "nothing installed" in capsys.readouterr().out
