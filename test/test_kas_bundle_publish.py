"""scripts/kas_bundle_publish.py: the Amazon Linux 2 inputs.

What these pin: ``--onnx-overlay-linux-x64`` swaps only the files under
onnxruntime-node's ``bin/napi-v3/linux/x64/`` in the linux-x64 engine, keeps each
member's mode and type, runs after the upstream manifest's sha256 check, and
leaves every other platform's tar bytes as they were; ``--node-linux-x64``
replaces only the linux-x64 Node build and refuses a tarball the installer would
not accept; ``provenance.json`` says where each piece came from; and a release
published this way installs with the installer's own code on a glibc 2.26 host
once the pin carries the AL2 floor.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import lzma
import os
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.acp import kas_bundle

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell stand-ins and exec bits")

_REPO = Path(__file__).resolve().parents[1]
_NODE = "22.23.3"
_ONNX = "node_modules/onnxruntime-node/bin/napi-v3/linux/x64/"


def _load():
    spec = importlib.util.spec_from_file_location(
        "kas_bundle_publish", _REPO / "scripts" / "kas_bundle_publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("kas_bundle_publish", module)
    spec.loader.exec_module(module)
    return module


pub = _load()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar(members: list[tuple[str, bytes | str, int]], compression: str = "") -> bytes:
    """``(name, data or "->target", mode)`` members; a str is a symlink target."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=f"w:{compression}" if compression else "w") as tf:
        for name, data, mode in members:
            info = tarfile.TarInfo(name)
            info.mode, info.mtime, info.uname = mode, 1_700_000_000, "builder"
            if isinstance(data, str):
                info.type, info.linkname = tarfile.SYMTYPE, data
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _engine_members() -> list[tuple[str, bytes | str, int]]:
    pkg = "node_modules/@kiro/agent"
    return [
        (
            f"{pkg}/package.json",
            json.dumps(
                {"name": "@kiro/agent", "bin": {"kiro-agent": "dist/server/acp-server.js"}}
            ).encode(),
            0o644,
        ),
        (f"{pkg}/dist/server/acp-server.js", b"// engine\n", 0o644),
        (_ONNX + "libonnxruntime.so.1", b"stock-lib", 0o644),
        (_ONNX + "libonnxruntime.so.1.21.0", "libonnxruntime.so.1", 0o777),
        (_ONNX + "libonnxruntime_providers_shared.so", b"stock-providers", 0o644),
        (_ONNX + "onnxruntime_binding.node", b"stock-binding", 0o644),
    ]


def _overlay_bytes() -> bytes:
    return _tar(
        [
            ("libonnxruntime.so.1", b"al2-lib", 0o755),
            ("libonnxruntime.so.1.21.0", "libonnxruntime.so.1", 0o777),
            ("onnxruntime_binding.node", b"al2-binding", 0o755),
        ],
        "xz",
    )


@pytest.fixture
def fake_zstd(tmp_path, monkeypatch):
    """A ``zstd`` that "decompresses" a plain tar: CI hosts need not carry zstd."""
    tool = tmp_path / "bin" / "zstd"
    tool.parent.mkdir()
    tool.write_text('#!/bin/sh\nshift 3\nexec cat "$1"\n', encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setattr(pub.shutil, "which", lambda name: str(tool) if name == "zstd" else None)


@pytest.fixture
def kas_dir(tmp_path) -> Path:
    """A KAS build drop: plain tars named .tar.zst (see fake_zstd) + its manifest."""
    d = tmp_path / "kas"
    d.mkdir()
    x64, arm = _tar(_engine_members()), _tar(_engine_members()[:2])
    (d / "kas-linux-x64.tar.zst").write_bytes(x64)
    (d / "kas-linux-arm64.tar.zst").write_bytes(arm)
    upstream = {
        "platforms": {
            "linux-x64": {"file": "kas-linux-x64.tar.zst", "sha256": _sha(x64)},
            "linux-arm64": {"file": "kas-linux-arm64.tar.zst", "sha256": _sha(arm)},
        }
    }
    (d / "manifest.json").write_text(json.dumps(upstream), encoding="utf-8")
    return d


def _overlay(tmp_path: Path) -> tuple[dict, str]:
    path = tmp_path / "overlay.tar.xz"
    path.write_bytes(_overlay_bytes())
    return pub.read_onnx_overlay(path, _sha(path.read_bytes())), _sha(path.read_bytes())


def _members(xz: Path) -> dict[str, tuple[tarfile.TarInfo, bytes | None]]:
    out = {}
    with tarfile.open(xz, "r:xz") as tf:
        for m in tf.getmembers():
            fh = tf.extractfile(m) if m.isfile() else None
            out[m.name] = (m, fh.read() if fh else None)
    return out


class TestOnnxOverlay:
    def test_swaps_only_the_x64_onnx_files_and_keeps_modes(self, kas_dir, tmp_path, fake_zstd):
        out = tmp_path / "out"
        out.mkdir()
        sources: dict = {}
        pub.engine_platforms(kas_dir, out, _overlay(tmp_path), sources)
        got = _members(out / "kas-linux-x64.tar.xz")
        lib, data = got[_ONNX + "libonnxruntime.so.1"]
        assert data == b"al2-lib" and lib.mode == 0o644 and lib.uname == "builder"
        assert got[_ONNX + "onnxruntime_binding.node"][1] == b"al2-binding"
        link = got[_ONNX + "libonnxruntime.so.1.21.0"][0]
        assert link.issym() and link.linkname == "libonnxruntime.so.1"
        # Not in the overlay, so untouched.
        assert got[_ONNX + "libonnxruntime_providers_shared.so"][1] == b"stock-providers"
        assert got["node_modules/@kiro/agent/dist/server/acp-server.js"][1] == b"// engine\n"
        assert list(got) == [name for name, _, _ in _engine_members()]
        assert sources["linux-x64"]["onnx_overlay_replaced"] == [
            _ONNX + "libonnxruntime.so.1",
            _ONNX + "libonnxruntime.so.1.21.0",
            _ONNX + "onnxruntime_binding.node",
        ]
        # Every other platform is the build's tar bytes, recompressed only.
        arm = lzma.decompress((out / "kas-linux-arm64.tar.xz").read_bytes())
        assert arm == (kas_dir / "kas-linux-arm64.tar.zst").read_bytes()
        assert "onnx_overlay_sha256" not in sources["linux-arm64"]

    def test_upstream_sha_is_checked_before_the_overlay(self, kas_dir, tmp_path, fake_zstd):
        (kas_dir / "kas-linux-x64.tar.zst").write_bytes(b"tampered")
        with pytest.raises(SystemExit, match="differs from the upstream manifest"):
            pub.engine_platforms(kas_dir, tmp_path, _overlay(tmp_path))
        assert not (tmp_path / "kas-linux-x64.tar.xz").exists()

    def test_a_name_the_engine_lacks_is_refused(self, kas_dir, tmp_path, fake_zstd):
        overlay, sha = _overlay(tmp_path)
        overlay["libextra.so"] = overlay["onnxruntime_binding.node"]
        with pytest.raises(SystemExit, match="libextra.so to replace"):
            pub.engine_platforms(kas_dir, tmp_path, (overlay, sha))
        assert not (tmp_path / "kas-linux-x64.tar.xz").exists()

    def test_a_type_change_is_refused(self, kas_dir, tmp_path, fake_zstd):
        overlay, sha = _overlay(tmp_path)
        overlay["libonnxruntime.so.1.21.0"] = overlay["libonnxruntime.so.1"]
        with pytest.raises(SystemExit, match="changes the member's type"):
            pub.engine_platforms(kas_dir, tmp_path, (overlay, sha))

    def test_a_rerun_with_another_overlay_regenerates(self, kas_dir, tmp_path, fake_zstd):
        out = tmp_path / "out"
        out.mkdir()
        pub.engine_platforms(kas_dir, out)
        stock = (out / "kas-linux-x64.tar.xz").read_bytes()
        pub.engine_platforms(kas_dir, out, _overlay(tmp_path))
        assert (out / "kas-linux-x64.tar.xz").read_bytes() != stock

    @pytest.mark.parametrize(
        "member",
        [("sub/lib.so", b"x", 0o644), ("../lib.so", b"x", 0o644), ("lib.so", "../../etc/x", 0o777)],
        ids=["nested", "traversal", "escaping-symlink"],
    )
    def test_overlay_members_must_be_flat(self, tmp_path, member):
        path = tmp_path / "o.tar.xz"
        path.write_bytes(_tar([member], "xz"))
        with pytest.raises(SystemExit):
            pub.read_onnx_overlay(path, _sha(path.read_bytes()))

    def test_overlay_sha_mismatch_is_refused(self, tmp_path):
        path = tmp_path / "o.tar.xz"
        path.write_bytes(_overlay_bytes())
        with pytest.raises(SystemExit, match="expected"):
            pub.read_onnx_overlay(path, "0" * 64)


def _node_tarball(
    tmp_path: Path, *, prints: str = f"v{_NODE}", top: str = f"node-v{_NODE}-linux-x64"
) -> Path:
    path = tmp_path / "local-node.tar.xz"
    script = f"#!/bin/sh\necho {prints}\n".encode()
    path.write_bytes(_tar([(f"{top}/bin/node", script, 0o755)], "xz"))
    return path


@pytest.fixture
def nodejs_org(monkeypatch):
    """Serve every nodejs.org build except linux-x64 from memory; record the gets."""
    files = {
        f"node-v{_NODE}-{suffix}": f"official {suffix}".encode()
        for key, suffix in pub.NODE_BUILDS.items()
        if key != "linux-x64"
    }
    got: list[str] = []

    def fetch(url: str, dest: Path) -> None:
        name = url.rsplit("/", 1)[1]
        got.append(name)
        if name == "SHASUMS256.txt":
            dest.write_text(
                "".join(f"{_sha(b)}  {n}\n" for n, b in files.items()), encoding="utf-8"
            )
        else:
            dest.write_bytes(files[name])

    monkeypatch.setattr(pub, "fetch", fetch)
    monkeypatch.setattr(pub.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pub.platform, "machine", lambda: "x86_64")
    return got


class TestLocalNode:
    def test_replaces_only_linux_x64(self, tmp_path, nodejs_org):
        local = _node_tarball(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        builds = pub.node_builds(_NODE, out, (local, _sha(local.read_bytes())))
        assert builds["linux-x64"]["sha256"] == _sha(local.read_bytes())
        assert builds["linux-x64"]["file"] == f"node-v{_NODE}-linux-x64.tar.xz"
        assert f"node-v{_NODE}-linux-x64.tar.xz" not in nodejs_org
        assert set(builds) == set(pub.NODE_BUILDS)
        assert builds["linux-arm64"]["sha256"] == _sha(b"official linux-arm64.tar.xz")

    def test_sha_mismatch_is_refused(self, tmp_path, nodejs_org):
        with pytest.raises(SystemExit, match="expected"):
            pub.node_builds(_NODE, tmp_path, (_node_tarball(tmp_path), "f" * 64))

    @pytest.mark.parametrize(
        "kw, why",
        [
            ({"top": "node-v22.23.3-linux-arm64"}, "layout"),
            ({"prints": "v22.23.4"}, "printed"),
        ],
        ids=["wrong-top-dir", "wrong-version"],
    )
    def test_a_tarball_the_installer_would_refuse_is_refused(self, tmp_path, nodejs_org, kw, why):
        local = _node_tarball(tmp_path, **kw)
        with pytest.raises(SystemExit, match=why):
            pub.node_builds(_NODE, tmp_path, (local, _sha(local.read_bytes())))

    def test_needs_a_linux_x64_host_to_run_node(self, tmp_path, nodejs_org, monkeypatch):
        monkeypatch.setattr(pub.platform, "system", lambda: "Darwin")
        local = _node_tarball(tmp_path)
        with pytest.raises(SystemExit, match="needs a linux-x64 host"):
            pub.node_builds(_NODE, tmp_path, (local, _sha(local.read_bytes())))


class TestPublishThenInstall:
    """A release published with both AL2 inputs installs on a glibc 2.26 x64 host
    through the installer's own code, once the pin names it with the AL2 floor."""

    def _publish(self, tmp_path, kas_dir, monkeypatch) -> Path:
        local = _node_tarball(tmp_path)
        overlay = tmp_path / "overlay.tar.xz"
        overlay.write_bytes(_overlay_bytes())
        argv = [
            "kas_bundle_publish.py",
            "--kas-dir", str(kas_dir), "--version", "0.66.117-node22.23.3-al2",
            "--node-version", _NODE, "--out", str(tmp_path / "pub"),
            "--node-linux-x64", str(local), "--node-linux-x64-sha256", _sha(local.read_bytes()),
            "--onnx-overlay-linux-x64", str(overlay),
            "--onnx-overlay-sha256", _sha(overlay.read_bytes()),
        ]  # fmt: skip
        monkeypatch.setattr(sys, "argv", argv)
        pub.main()
        return tmp_path / "pub" / "kas" / "0.66.117-node22.23.3-al2"

    def test_installs_and_runs_on_glibc_2_26(
        self, tmp_path, kas_dir, fake_zstd, nodejs_org, monkeypatch
    ):
        release = self._publish(tmp_path, kas_dir, monkeypatch)
        prov = json.loads((release / "provenance.json").read_text(encoding="utf-8"))
        assert prov["node"]["linux-x64"] == "local --node-linux-x64"
        assert prov["node"]["linux-arm64"].startswith("https://nodejs.org/dist/")
        assert prov["engine"]["linux-x64"]["onnx_overlay_sha256"]
        manifest = kas_bundle.parse_manifest(
            json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        )
        monkeypatch.setattr(kas_bundle, "KAS_PIN_VERSION", manifest.version)
        monkeypatch.setitem(kas_bundle.KAS_PIN_LINUX_GLIBC_FLOOR, "linux-x64", (2, 26))
        with patch("kiro_crew.constants._host_glibc_version", return_value=(2, 26)):
            installed = kas_bundle.install_bundle(
                manifest, source=str(release), root=tmp_path / "root",
                system="Linux", machine="x86_64",
            )  # fmt: skip
        (lib,) = installed.root.rglob("libonnxruntime.so.1")
        assert lib.read_bytes() == b"al2-lib"
        assert installed.node_version == _NODE

    def test_the_same_release_is_refused_while_the_pin_is_stock(
        self, tmp_path, kas_dir, fake_zstd, nodejs_org, monkeypatch
    ):
        release = self._publish(tmp_path, kas_dir, monkeypatch)
        manifest = kas_bundle.parse_manifest(
            json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        )
        with patch("kiro_crew.constants._host_glibc_version", return_value=(2, 26)):
            with pytest.raises(kas_bundle.KasHostUnsupported, match="2.28"):
                kas_bundle.install_bundle(
                    manifest, source=str(release), root=tmp_path / "root",
                    system="Linux", machine="x86_64",
                )  # fmt: skip
