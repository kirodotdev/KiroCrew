"""Kiro Crew's own KAS engine bundle: manifest, install, resolve, and the spawn route.

The route exists so an operator can run KAS with no kiro-cli at all. What these
tests pin: the manifest is validated before anything is fetched; every artifact is
hashed before it is installed; an install is all-or-nothing at the version's name;
the harness spawns the bundle only when ``agent.kas_bundle`` is on, with Crew as
the auth owner unconditionally, and refuses (never falls back) when nothing is
installed; and the data-home leaf the bundle lives in is sealed read-only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.acp import kas_bundle
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.kas import KAS_BUNDLE_SPAWN_LABEL, KasHarness
from kiro_crew.acp.kas_transport import (
    KAS_BUNDLE_AUTH_ARG,
    KAS_BUNDLE_NODE_FLAG,
    KAS_BUNDLE_TRANSPORT_ARG,
    build_kas_bundle_argv,
)
from kiro_crew.acp.session_handle import AcpRuntimeError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX exec bits and tar layout")

_NODE_VERSION = "22.23.3"


# ── fixtures: a fake engine tarball and a fake Node tarball ────────────────────


def _tar_bytes(members: dict[str, bytes], *, mode: int = 0o644, compression: str = "xz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=f"w:{compression}") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _engine_tar() -> bytes:
    pkg = "node_modules/@kiro/agent"
    return _tar_bytes(
        {
            f"{pkg}/package.json": json.dumps(
                {"name": "@kiro/agent", "bin": {"kiro-agent": "dist/server/acp-server.js"}}
            ).encode(),
            f"{pkg}/dist/server/acp-server.js": b"// engine\n",
        }
    )


def _node_tar() -> bytes:
    # A shell stand-in that answers `--version` the way node does, so the install
    # probe runs it exactly as the harness will.
    script = f"#!/bin/sh\necho v{_NODE_VERSION}\n".encode()
    return _tar_bytes({f"node-v{_NODE_VERSION}-linux-x64/bin/node": script}, mode=0o755)


def _ref(name: str, data: bytes) -> dict:
    return {"file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """A local directory shaped like the publishing host: manifest + artifacts."""
    src = tmp_path / "src"
    src.mkdir()
    engine, node = _engine_tar(), _node_tar()
    (src / "kas-linux-x64.tar.xz").write_bytes(engine)
    (src / f"node-v{_NODE_VERSION}-linux-x64.tar.xz").write_bytes(node)
    manifest = {
        "version": "0.66.33",
        "package": "@kiro/agent",
        "platforms": {"linux-x64": _ref("kas-linux-x64.tar.xz", engine)},
        "node": {
            "version": _NODE_VERSION,
            "builds": {"linux-x64": _ref(f"node-v{_NODE_VERSION}-linux-x64.tar.xz", node)},
        },
    }
    (src / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _manifest(source_dir: Path) -> kas_bundle.KasBundleManifest:
    return kas_bundle.parse_manifest(
        json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    )


def _install(source_dir: Path, root: Path, **kw) -> kas_bundle.InstalledKasBundle:
    return kas_bundle.install_bundle(
        _manifest(source_dir),
        source=str(source_dir),
        root=root,
        system="Linux",
        machine="x86_64",
        **kw,
    )


# ── host keys ──────────────────────────────────────────────────────────────────


class TestHostKeys:
    @pytest.mark.parametrize(
        "system, machine, platform_key, node_key",
        [
            ("Linux", "x86_64", "linux-x64", "linux-x64"),
            ("Linux", "aarch64", "linux-arm64", "linux-arm64"),
            ("Darwin", "arm64", "darwin-universal", "darwin-arm64"),
            ("Darwin", "x86_64", "darwin-universal", "darwin-x64"),
            ("Windows", "AMD64", "win32-x64", "win32-x64"),
        ],
    )
    def test_engine_key_is_the_build_matrix_and_node_key_is_per_arch(
        self, system, machine, platform_key, node_key
    ):
        """Mac is one universal JS bundle; Node is one binary per architecture."""
        assert kas_bundle.host_platform_key(system, machine) == platform_key
        assert kas_bundle.host_node_key(system, machine) == node_key

    def test_an_unpublished_host_is_a_named_refusal(self):
        with pytest.raises(kas_bundle.KasBundleError, match="architecture"):
            kas_bundle.host_platform_key("Linux", "riscv64")
        with pytest.raises(kas_bundle.KasBundleError, match="operating system"):
            kas_bundle.host_platform_key("FreeBSD", "x86_64")


# ── manifest ───────────────────────────────────────────────────────────────────


class TestManifest:
    def test_a_valid_manifest_parses(self, source_dir):
        m = _manifest(source_dir)
        assert m.version == "0.66.33"
        assert m.node_version == _NODE_VERSION
        assert m.engine_for("linux-x64").file == "kas-linux-x64.tar.xz"
        assert m.node_for("linux-x64").size > 0

    def test_the_upstream_electron_builtin_shape_is_refused_with_the_remedy(self):
        """The bucket's first publish carried ``"node": "electron-builtin"`` -- a build
        that expected Kiro's own Electron. Crew ships no Electron, so the refusal
        names what to republish rather than installing an engine nothing can run."""
        data = {
            "version": "0.66.33",
            "package": "@kiro/agent",
            "node": "electron-builtin",
            "platforms": {"linux-x64": {"file": "k.tar.zst", "sha256": "a" * 64, "size": 1}},
        }
        with pytest.raises(kas_bundle.KasBundleError, match="electron-builtin.*node.builds"):
            kas_bundle.parse_manifest(data)

    @pytest.mark.parametrize(
        "file",
        ["../escape.tar.xz", "sub/dir.tar.xz", "/abs.tar.xz", "", "a b.tar.xz"],
    )
    def test_an_artifact_name_is_a_bare_object_name(self, file):
        """Names are joined onto the manifest's url AND onto a local directory, so a
        separator or traversal in one would fetch or read outside both."""
        data = {
            "version": "1",
            "platforms": {"linux-x64": {"file": file, "sha256": "a" * 64, "size": 1}},
            "node": {
                "version": "22",
                "builds": {"linux-x64": {"file": "n", "sha256": "a" * 64, "size": 1}},
            },
        }
        with pytest.raises(kas_bundle.KasBundleError, match="bare object name"):
            kas_bundle.parse_manifest(data)

    @pytest.mark.parametrize("sha", ["abc", "G" * 64, 123, None])
    def test_the_pin_must_be_a_sha256(self, sha):
        data = {
            "version": "1",
            "platforms": {"linux-x64": {"file": "k", "sha256": sha, "size": 1}},
            "node": {
                "version": "22",
                "builds": {"linux-x64": {"file": "n", "sha256": "a" * 64, "size": 1}},
            },
        }
        with pytest.raises(kas_bundle.KasBundleError, match="sha256"):
            kas_bundle.parse_manifest(data)

    def test_a_missing_platform_names_what_is_published(self, source_dir):
        with pytest.raises(kas_bundle.KasBundleError, match="win32-x64.*has: linux-x64"):
            _manifest(source_dir).engine_for("win32-x64")

    def test_manifest_url_shapes(self):
        base = "https://example.test/mirror/"
        assert (
            kas_bundle.manifest_url_for(base, "0.66.33")
            == "https://example.test/mirror/kas/0.66.33/manifest.json"
        )

    def test_fetch_refuses_a_plaintext_base(self):
        with pytest.raises(kas_bundle.KasBundleError, match="https://"):
            kas_bundle.fetch_manifest("http://example.test", version="0.66.33")

    def test_fetch_reads_the_versioned_manifest_and_returns_its_url(self, source_dir):
        """Artifacts resolve beside the manifest's own url, which is why that url is
        returned with the manifest."""
        docs = {
            "https://h.test/kas/0.66.33/manifest.json": (source_dir / "manifest.json").read_bytes(),
        }

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        class _Opener:
            def open(self, url, timeout):
                return _Resp(docs[url])

        manifest, url = kas_bundle.fetch_manifest(
            "https://h.test", version="0.66.33", opener=_Opener()
        )
        assert manifest.version == "0.66.33"
        assert url == "https://h.test/kas/0.66.33/manifest.json"

    def test_fetch_from_a_malformed_url_is_a_refusal_not_a_traceback(self):
        """urllib raises ValueError for a url it cannot parse; the operator typed it, so
        the answer is the installer's one error type, which the CLI prints and exits."""
        with pytest.raises(kas_bundle.KasBundleError, match="could not fetch"):
            kas_bundle.fetch_manifest("https://[mirror", version="0.66.33")

    def test_fetch_pinned_version_must_match_the_document(self, source_dir):
        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        class _Opener:
            def open(self, url, timeout):
                return _Resp((source_dir / "manifest.json").read_bytes())

        with pytest.raises(kas_bundle.KasBundleError, match="asked for KAS 9.9.9"):
            kas_bundle.fetch_manifest("https://h.test", version="9.9.9", opener=_Opener())


# ── install ────────────────────────────────────────────────────────────────────


class TestInstall:
    def test_installs_probes_and_makes_current(self, source_dir, tmp_path):
        root = tmp_path / "engines" / "kas"
        installed = _install(source_dir, root)

        assert installed.version == "0.66.33"
        assert installed.node_version == _NODE_VERSION
        assert installed.acp_server.is_file()
        assert installed.node_bin.stat().st_mode & stat.S_IXUSR
        assert installed.root == root / "0.66.33"
        assert not (root / "current.json").exists()  # the pin chooses, not a file
        record = json.loads((installed.root / "install.json").read_text())
        assert record["acp_server"] == "bundle/node_modules/@kiro/agent/dist/server/acp-server.js"
        assert record["node_bin"].endswith("/bin/node")
        # The archives are gone; only the extracted trees remain.
        assert not (installed.root / "downloads").exists()
        # No staging residue beside the published version.
        assert [p.name for p in root.iterdir() if p.name.startswith(".staging-")] == []

    def test_resolves_what_it_installed(self, source_dir, tmp_path):
        root = tmp_path / "root"
        installed = _install(source_dir, root)
        assert kas_bundle.installed_bundle(root=root, version="0.66.33") == installed
        assert kas_bundle.installed_versions(root=root) == ["0.66.33"]

    def test_nothing_installed_is_none(self, tmp_path):
        assert kas_bundle.installed_bundle(root=tmp_path / "absent") is None

    def test_a_tampered_artifact_installs_nothing(self, source_dir, tmp_path):
        """The sha256 pin is the trust anchor: a byte flipped after the manifest was
        written leaves no version directory and no current.json."""
        target = source_dir / "kas-linux-x64.tar.xz"
        target.write_bytes(target.read_bytes() + b"\0")
        root = tmp_path / "root"
        with pytest.raises(kas_bundle.KasBundleError, match="sha256|bytes"):
            _install(source_dir, root)
        assert not (root / "0.66.33").exists()
        assert kas_bundle.installed_versions(root=root) == []

    def test_a_node_that_does_not_run_installs_nothing(self, source_dir, tmp_path):
        """A runtime for the wrong host is caught by the probe at install time, not by
        the first session's handshake timeout."""
        bad = _tar_bytes(
            {f"node-v{_NODE_VERSION}-linux-x64/bin/node": b"#!/bin/sh\nexit 7\n"}, mode=0o755
        )
        (source_dir / f"node-v{_NODE_VERSION}-linux-x64.tar.xz").write_bytes(bad)
        m = json.loads((source_dir / "manifest.json").read_text())
        m["node"]["builds"]["linux-x64"] = _ref(f"node-v{_NODE_VERSION}-linux-x64.tar.xz", bad)
        (source_dir / "manifest.json").write_text(json.dumps(m))
        root = tmp_path / "root"
        with pytest.raises(kas_bundle.KasBundleError, match="exited 7"):
            _install(source_dir, root)
        assert kas_bundle.installed_bundle(root=root, version="0.66.33") is None

    def test_a_node_of_another_version_is_refused(self, source_dir, tmp_path):
        m = json.loads((source_dir / "manifest.json").read_text())
        m["node"]["version"] = "22.0.0"
        (source_dir / "manifest.json").write_text(json.dumps(m))
        with pytest.raises(kas_bundle.KasBundleError, match="reports v22.23.3.*22.0.0"):
            _install(source_dir, tmp_path / "root")

    def test_reinstalling_a_present_version_needs_force(self, source_dir, tmp_path):
        root = tmp_path / "root"
        _install(source_dir, root)
        with pytest.raises(kas_bundle.KasBundleError, match="already installed"):
            _install(source_dir, root)
        _install(source_dir, root, force=True)
        assert kas_bundle.installed_bundle(root=root, version="0.66.33") is not None

    def test_a_concurrent_install_that_won_is_a_refusal_not_a_swap(
        self, source_dir, tmp_path, monkeypatch
    ):
        """Two non-force installs of the same absent version: the loser finds the
        winner's directory at publish time and refuses, leaving it in place."""
        root = tmp_path / "root"
        winner_marker = root / "0.66.33" / "winner"

        real_probe = kas_bundle._probe_node
        raced = False

        def probe_then_race(node_bin, version):
            nonlocal raced
            real_probe(node_bin, version)
            if raced:
                return
            raced = True
            # The other install publishes between this one's probe and its rename.
            _install(source_dir, root)
            winner_marker.write_text("won")

        monkeypatch.setattr(kas_bundle, "_probe_node", probe_then_race)
        with pytest.raises(kas_bundle.KasBundleError, match="installed by another process"):
            _install(source_dir, root)
        assert winner_marker.read_text() == "won"
        assert kas_bundle.installed_bundle(root=root, version="0.66.33") is not None
        assert [p.name for p in root.iterdir() if p.name.startswith(".staging-")] == []

    def test_a_zstd_engine_archive_installs_like_the_published_one(
        self, source_dir, tmp_path, monkeypatch
    ):
        """The live manifest publishes every engine as .tar.zst; the fixture here is
        the same shape, compressed with whichever decoder this host has, and the
        archive directory is removed right after extraction -- which needs every
        decoder handle closed and the zstd child reaped first."""
        import subprocess

        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tf:
            pkg = "node_modules/@kiro/agent"
            for name, data in {
                f"{pkg}/package.json": json.dumps(
                    {"name": "@kiro/agent", "bin": {"kiro-agent": "dist/server/acp-server.js"}}
                ).encode(),
                f"{pkg}/dist/server/acp-server.js": b"// engine\n",
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        try:
            import zstandard

            engine = zstandard.ZstdCompressor().compress(raw.getvalue())
        except ImportError:
            import shutil as _shutil

            if not _shutil.which("zstd"):
                pytest.skip("no zstd decoder on this host")
            engine = subprocess.run(
                ["zstd", "-c", "-q"], input=raw.getvalue(), capture_output=True, check=True
            ).stdout
        (source_dir / "kas-linux-x64.tar.zst").write_bytes(engine)
        manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["platforms"]["linux-x64"] = _ref("kas-linux-x64.tar.zst", engine)
        (source_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        root = tmp_path / "root"
        installed = _install(source_dir, root)
        assert installed.acp_server.is_file()
        assert not (installed.root / "downloads").exists()

    def test_a_zstd_archive_with_no_decoder_is_a_named_refusal(self, tmp_path, monkeypatch):
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "zstandard", None)
        monkeypatch.setattr(kas_bundle.shutil, "which", lambda name: None)
        monkeypatch.delattr(tarfile.TarFile, "zstopen", raising=False)
        archive = tmp_path / "kas-linux-x64.tar.zst"
        archive.write_bytes(b"\x28\xb5\x2f\xfd")
        with pytest.raises(kas_bundle.KasBundleError, match="neither the 'zstandard'"):
            with kas_bundle._open_tar(archive):
                pass

    def test_a_tar_member_that_escapes_is_refused(self, source_dir, tmp_path):
        evil = _tar_bytes({"../../outside.js": b"x"})
        (source_dir / "kas-linux-x64.tar.xz").write_bytes(evil)
        m = json.loads((source_dir / "manifest.json").read_text())
        m["platforms"]["linux-x64"] = _ref("kas-linux-x64.tar.xz", evil)
        (source_dir / "manifest.json").write_text(json.dumps(m))
        with pytest.raises(kas_bundle.KasBundleError):
            _install(source_dir, tmp_path / "root")
        assert not (tmp_path / "outside.js").exists()

    def test_remote_source_goes_through_the_pinned_transfer_engine(self, source_dir, tmp_path):
        """An https manifest url makes every artifact a ``download_to`` call carrying
        the manifest's sha256 and size, from the url beside the manifest."""
        calls: list[dict] = []

        def fake_download_to(path, url, **kw):
            calls.append({"path": path, "url": url, **kw})
            name = url.rsplit("/", 1)[1]
            path.write_bytes((source_dir / name).read_bytes())
            return True, ""

        with patch.object(kas_bundle.asset_downloader, "download_to", fake_download_to):
            installed = kas_bundle.install_bundle(
                _manifest(source_dir),
                source="https://h.test/kas/0.66.33/manifest.json",
                root=tmp_path / "root",
                system="Linux",
                machine="x86_64",
            )
        assert installed.version == "0.66.33"
        urls = sorted(c["url"] for c in calls)
        assert urls == [
            "https://h.test/kas/0.66.33/kas-linux-x64.tar.xz",
            f"https://h.test/kas/0.66.33/node-v{_NODE_VERSION}-linux-x64.tar.xz",
        ]
        for call in calls:
            assert call["sha256"] and call["size"] > 0
            assert "allow_cross_host_redirects" not in call  # manifest url: strict policy

    def test_a_damaged_record_reads_as_not_installed(self, source_dir, tmp_path):
        root = tmp_path / "root"
        installed = _install(source_dir, root)
        (installed.root / "install.json").write_text("{not json")
        assert kas_bundle.installed_bundle(root=root, version="0.66.33") is None
        _install(source_dir, root, force=True)
        installed = kas_bundle.installed_bundle(root=root, version="0.66.33")
        installed.acp_server.unlink()
        assert kas_bundle.installed_bundle(root=root, version="0.66.33") is None

    @pytest.mark.parametrize("field", ["node_bin", "acp_server"])
    @pytest.mark.parametrize(
        "entry", ["/usr/bin/env", "C:\\evil\\node.exe", "\\rooted\\node", "../outside", "a/../../b"]
    )
    def test_a_record_entry_outside_its_root_reads_as_not_installed(
        self, source_dir, tmp_path, field, entry
    ):
        # ``home / "/abs"`` is ``/abs``: an absolute entry would make the gateway run a
        # program the record merely names, so the per-spawn reader refuses it.
        root = tmp_path / "root"
        installed = _install(source_dir, root)
        record_path = installed.root / "install.json"
        record = json.loads(record_path.read_text())
        record[field] = entry
        record_path.write_text(json.dumps(record))
        assert kas_bundle.installed_bundle(root=root, version="0.66.33") is None


# ── argv and the spawn route ───────────────────────────────────────────────────


class TestBundledRoot:
    """The desktop app's staged copy (KIROCREW_BUNDLED_KAS_DIR) is a SECOND root:
    asked only when the data home has nothing at the pin, so an operator's own
    install always wins, and flagged so status and doctor can say where it came from."""

    def test_unset_or_blank_is_not_bundled(self, monkeypatch):
        monkeypatch.delenv(kas_bundle.BUNDLED_KAS_DIR_ENV, raising=False)
        assert kas_bundle.bundled_kas_root() is None
        monkeypatch.setenv(kas_bundle.BUNDLED_KAS_DIR_ENV, "   ")
        assert kas_bundle.bundled_kas_root() is None
        monkeypatch.setenv(kas_bundle.BUNDLED_KAS_DIR_ENV, "/r")
        assert kas_bundle.bundled_kas_root() == Path("/r")

    def test_the_bundled_copy_answers_when_the_data_home_is_empty(
        self, source_dir, tmp_path, monkeypatch
    ):
        app_root = tmp_path / "app" / "kas"
        _install(source_dir, app_root)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv(kas_bundle.BUNDLED_KAS_DIR_ENV, str(app_root))
        found = kas_bundle.installed_bundle(version="0.66.33")
        assert found is not None
        assert found.bundled is True
        assert found.root == app_root / "0.66.33"

    def test_the_data_home_install_wins_over_the_bundled_copy(
        self, source_dir, tmp_path, monkeypatch
    ):
        app_root = tmp_path / "app" / "kas"
        _install(source_dir, app_root)
        home = tmp_path / "home"
        _install(source_dir, home / "engines" / "kas")
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setenv(kas_bundle.BUNDLED_KAS_DIR_ENV, str(app_root))
        found = kas_bundle.installed_bundle(version="0.66.33")
        assert found is not None
        assert found.bundled is False
        assert found.root == home / "engines" / "kas" / "0.66.33"

    def test_an_explicit_root_never_consults_the_bundled_copy(
        self, source_dir, tmp_path, monkeypatch
    ):
        app_root = tmp_path / "app" / "kas"
        _install(source_dir, app_root)
        monkeypatch.setenv(kas_bundle.BUNDLED_KAS_DIR_ENV, str(app_root))
        assert kas_bundle.installed_bundle(root=tmp_path / "empty", version="0.66.33") is None


class TestArgv:
    def test_bundle_argv_is_the_engine_cli(self):
        argv = build_kas_bundle_argv("/n/bin/node", "/e/acp-server.js")
        assert argv == [
            "/n/bin/node",
            KAS_BUNDLE_NODE_FLAG,
            "/e/acp-server.js",
            KAS_BUNDLE_TRANSPORT_ARG,
            KAS_BUNDLE_AUTH_ARG,
        ]
        assert "--auth-method" not in " ".join(argv)  # no kiro-cli owner on this route

    def test_empty_paths_are_refused(self):
        with pytest.raises(ValueError):
            build_kas_bundle_argv("", "/e/acp-server.js")
        with pytest.raises(ValueError):
            build_kas_bundle_argv("/n/bin/node", "")


def _ctx(tmp_path: Path) -> SpawnContext:
    return SpawnContext(agent="kirocrew", work_dir=tmp_path, model=None, environ={}, home=tmp_path)


class TestSpawnRoute:
    @pytest.mark.asyncio
    async def test_bundle_on_spawns_the_installed_engine_with_crew_as_auth_owner(
        self, source_dir, tmp_path
    ):
        """``host_auth`` is True whatever the vault holds: there is no kiro-cli store
        to fall back to, and a process started expecting Crew to answer the
        credential callback is the only correct shape on this route."""
        root = tmp_path / "root"
        installed = _install(source_dir, root)
        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=True),
            patch.object(kas_bundle, "installed_bundle", return_value=installed),
            patch(
                "kiro_crew.acp.harness.kas.vault_holds_identity_off_loop",
                side_effect=AssertionError("the vault is not consulted on the bundle route"),
            ),
        ):
            plan = await KasHarness().resolve_spawn(_ctx(tmp_path))
        assert plan.argv == build_kas_bundle_argv(
            str(installed.node_bin), str(installed.acp_server)
        )
        assert plan.host_auth is True
        assert plan.spawn_label == KAS_BUNDLE_SPAWN_LABEL
        assert plan.stderr_label == KAS_BUNDLE_SPAWN_LABEL

    @pytest.mark.asyncio
    async def test_bundle_on_with_nothing_installed_refuses_and_names_the_install(self, tmp_path):
        """A refusal, not a fallback to the relay: the operator chose the route, and a
        silent relay spawn would make a kiro-cli problem look like a bundle one."""
        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=True),
            patch.object(kas_bundle, "installed_bundle", return_value=None),
            patch.object(kas_bundle, "kas_engines_root", return_value=tmp_path),
        ):
            with pytest.raises(AcpRuntimeError, match="kirocrew kas install"):
                await KasHarness().resolve_spawn(_ctx(tmp_path))

    @pytest.mark.asyncio
    async def test_bundle_off_keeps_the_relay(self, tmp_path):
        """The default route is byte-for-byte what it was: kiro-cli's relay, with the
        auth owner decided from the vault."""
        import kiro_crew.acp.client as client_mod

        async def _bin(*, environ=None, home=None):
            return "/opt/kiro/kiro-cli"

        with (
            patch("kiro_crew.acp.harness.kas._bundle_route_selected", return_value=False),
            patch.object(
                kas_bundle,
                "installed_bundle",
                side_effect=AssertionError("the bundle is not consulted on the relay route"),
            ),
            patch.object(client_mod, "_resolve_kiro_bin_for_spawn", _bin),
            patch("kiro_crew.acp.harness.kas.vault_holds_identity_off_loop", return_value=False),
        ):
            plan = await KasHarness().resolve_spawn(_ctx(tmp_path))
        assert plan.argv == [
            "/opt/kiro/kiro-cli",
            "acp",
            "--agent-engine",
            "v3",
            "--auth-method",
            "cli",
        ]
        assert plan.host_auth is False

    def test_the_route_reads_the_config_field(self, tmp_path, monkeypatch):
        from kiro_crew.acp.harness.kas import _bundle_route_selected
        from kiro_crew.config.loader import KiroCrewConfig

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        KiroCrewConfig.reset_cache() if hasattr(KiroCrewConfig, "reset_cache") else None
        assert _bundle_route_selected() is False
        (tmp_path / "config.json").write_text(json.dumps({"agent": {"kas_bundle": True}}))
        KiroCrewConfig.reset_cache() if hasattr(KiroCrewConfig, "reset_cache") else None
        assert _bundle_route_selected() is True


# ── the leaf is sealed ─────────────────────────────────────────────────────────


class TestTheLeafIsSealed:
    def test_engines_is_read_only_in_the_sandbox_and_fenced_at_the_tool_gate(self):
        """A sandboxed KAS process must read and execute the engine; nothing in a
        sandbox may rewrite the code the next session runs. Same disposition as
        ``playwright-cli``, and the same precreate + no-follow pins so an absent
        directory on a fresh install is not a name the agent gets to create."""
        from kiro_crew import sandbox, security

        leaf = kas_bundle.KAS_BUNDLE_ENGINES_LEAF
        assert leaf in sandbox._CREW_READONLY_LEAVES
        assert leaf in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
        assert leaf in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        assert leaf in sandbox._CREW_CHILD_READABLE_LEAVES
        assert leaf not in sandbox._CREW_HIDDEN_LEAVES
        assert any(rel.endswith(f"/{leaf}") for rel in security.sensitive_home_dirs())

    def test_the_root_is_under_that_leaf(self, tmp_path):
        assert kas_bundle.kas_engines_root(tmp_path) == tmp_path / "engines" / "kas"


# ── the pin ────────────────────────────────────────────────────────────────────


class TestThePin:
    """Each Kiro Crew release runs exactly one engine version, fixed by one digest."""

    def test_the_pin_is_well_formed(self):
        from kiro_crew.acp import kas_pin

        assert kas_bundle._VERSION_RE.match(kas_pin.KAS_PIN_VERSION)
        assert kas_bundle._SHA256_RE.match(kas_pin.KAS_PIN_MANIFEST_SHA256)

    def test_the_harness_looks_up_the_pinned_version_not_the_newest(
        self, source_dir, tmp_path, monkeypatch
    ):
        """Two versions on disk (an update ran, the old release is still around): the
        release asks for ITS version. This is what lets an old process keep its engine
        and a rollback find its engine."""
        from kiro_crew.acp import kas_pin

        root = tmp_path / "root"
        _install(source_dir, root)  # 0.66.33
        m = json.loads((source_dir / "manifest.json").read_text())
        m["version"] = "0.99.0"
        (source_dir / "manifest.json").write_text(json.dumps(m))
        _install(source_dir, root)
        assert kas_bundle.installed_versions(root=root) == ["0.66.33", "0.99.0"]

        monkeypatch.delenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, raising=False)
        monkeypatch.setattr(kas_bundle, "pinned_version", lambda: "0.66.33")
        assert kas_bundle.installed_bundle(root=root).version == "0.66.33"
        monkeypatch.setattr(kas_bundle, "pinned_version", lambda: "0.99.0")
        assert kas_bundle.installed_bundle(root=root).version == "0.99.0"

    def test_the_override_env_names_another_version(self, monkeypatch):
        from kiro_crew.acp import kas_pin

        monkeypatch.delenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, raising=False)
        assert kas_pin.pinned_version() == kas_pin.KAS_PIN_VERSION
        monkeypatch.setenv(kas_pin.KAS_VERSION_OVERRIDE_ENV, "0.70.0")
        assert kas_pin.pinned_version() == "0.70.0"

    def test_a_manifest_that_does_not_match_the_digest_is_refused_before_parsing(self, source_dir):
        body = (source_dir / "manifest.json").read_bytes()

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        class _Opener:
            def open(self, url, timeout):
                return _Resp(body)

        good = hashlib.sha256(body).hexdigest()
        manifest, _ = kas_bundle.fetch_manifest(
            "https://h.test", version="0.66.33", sha256=good, opener=_Opener()
        )
        assert manifest.version == "0.66.33"
        with pytest.raises(kas_bundle.KasBundleError, match="does not match the sha256"):
            kas_bundle.fetch_manifest(
                "https://h.test", version="0.66.33", sha256="0" * 64, opener=_Opener()
            )

    def test_install_pinned_is_a_no_op_when_the_pin_is_installed(
        self, source_dir, tmp_path, monkeypatch
    ):
        root = tmp_path / "root"
        installed = _install(source_dir, root)
        monkeypatch.setattr(kas_bundle, "KAS_PIN_VERSION", "0.66.33")
        monkeypatch.setattr(
            kas_bundle,
            "fetch_manifest",
            lambda *a, **k: pytest.fail("fetched with the pin installed"),
        )
        assert kas_bundle.install_pinned("https://h.test", root=root) == installed

    def test_install_pinned_passes_the_digest(self, source_dir, tmp_path, monkeypatch):
        seen = {}

        def fake_fetch(base, *, version, sha256, opener=None):
            seen.update(version=version, sha256=sha256)
            raise kas_bundle.KasBundleError("stop here")

        monkeypatch.setattr(kas_bundle, "fetch_manifest", fake_fetch)
        with pytest.raises(kas_bundle.KasBundleError, match="stop here"):
            kas_bundle.install_pinned("https://h.test", root=tmp_path / "empty")
        assert seen == {
            "version": kas_bundle.KAS_PIN_VERSION,
            "sha256": kas_bundle.KAS_PIN_MANIFEST_SHA256,
        }


class TestExistingRoutesAreUntouched:
    """Default config: kiro-cli users and KAS-over-relay users see no change."""

    def test_kas_bundle_defaults_off(self):
        from kiro_crew.config.sections import AgentConfig

        assert AgentConfig().kas_bundle is False

    @pytest.mark.asyncio
    async def test_the_kiro_harness_never_consults_the_bundle(self, tmp_path):
        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.harness import KiroHarness

        async def _bin(*, environ=None, home=None):
            return "/opt/kiro/kiro-cli"

        with (
            patch.object(
                kas_bundle, "installed_bundle", side_effect=AssertionError("bundle consulted")
            ),
            patch(
                "kiro_crew.acp.harness.kas._bundle_route_selected",
                side_effect=AssertionError("kas route read on the kiro harness"),
            ),
            patch.object(client_mod, "_resolve_kiro_bin_for_spawn", _bin),
        ):
            try:
                plan = await KiroHarness().resolve_spawn(_ctx(tmp_path))
            except AssertionError:
                raise
            except Exception:
                return  # an unrelated pre-spawn gate on a bare tmp home; not our concern
        assert plan.argv[:2] == ["/opt/kiro/kiro-cli", "acp"]
        assert "--agent-engine" not in plan.argv
