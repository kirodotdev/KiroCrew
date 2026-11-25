"""Per-platform wheels that carry the pinned KAS engine, and the engine guarantee.

What these pin: the engine inside the installed package is asked first and wins over
the data home only for the pinned version; ``install_pinned`` downloads nothing when
the package has it; the builder writes a platform wheel whose RECORD, tag and exec
bits pip accepts and the gate refuses a wheel without the engine; the gateway start
installs a missing engine and never raises; and the platform tag lists in the
builder, the update, ``cli.sh`` and the two workflows stay one list.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

from kiro_crew.acp import kas_bundle, kas_engine_install, kas_engine_startup
from kiro_crew.acp.kas_pin import KAS_PIN_VERSION
from kiro_crew.platform import wheel_engine_platform

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX exec bits")

_REPO = Path(__file__).resolve().parents[1]


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "kas_platform_wheel", _REPO / "scripts" / "kas_platform_wheel.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("kas_platform_wheel", module)
    spec.loader.exec_module(module)
    return module


def _engine_tree(
    root: Path, version: str = KAS_PIN_VERSION, *, node_key: str = "linux-x64"
) -> Path:
    """A minimal engine tree at *root*/<version>/ that ``_read_installed`` accepts."""
    home = root / version
    server = home / "bundle" / "node_modules" / "@kiro" / "agent" / "dist" / "server"
    server.mkdir(parents=True)
    (server / "acp-server.js").write_text("// engine\n", encoding="utf-8")
    node = home / "node" / f"node-v22.23.4-{node_key}" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\necho v22.23.4\n", encoding="utf-8")
    node.chmod(0o755)
    record = {
        "version": version,
        "package": "@kiro/agent",
        "platform": node_key,
        "node_key": node_key,
        "node_version": "22.23.4",
        "engine_sha256": "e" * 64,
        "node_sha256": "f" * 64,
        "acp_server": server.joinpath("acp-server.js").relative_to(home).as_posix(),
        "node_bin": node.relative_to(home).as_posix(),
    }
    (home / kas_bundle.KAS_BUNDLE_INSTALL_RECORD_NAME).write_text(json.dumps(record), "utf-8")
    return home


@pytest.fixture
def roots(tmp_path, monkeypatch):
    packaged = tmp_path / "pkg"
    home = tmp_path / "home"
    monkeypatch.setattr(kas_bundle, "packaged_kas_root", lambda: packaged)
    monkeypatch.setattr(kas_bundle, "kas_engines_root", lambda home_=None: home)
    monkeypatch.setattr(kas_bundle, "bundled_kas_root", lambda: None)
    return packaged, home


class TestLookupOrder:
    def test_the_engine_inside_the_package_is_asked_first(self, roots):
        packaged, home = roots
        _engine_tree(packaged)
        _engine_tree(home)
        found = kas_bundle.installed_bundle()
        assert found is not None and found.packaged and found.root == packaged / KAS_PIN_VERSION

    def test_without_one_the_data_home_answers(self, roots):
        _packaged, home = roots
        _engine_tree(home)
        found = kas_bundle.installed_bundle()
        assert found is not None and not found.packaged and found.root == home / KAS_PIN_VERSION

    def test_a_version_override_is_never_the_packaged_pin(self, roots):
        packaged, home = roots
        _engine_tree(packaged)
        _engine_tree(home, "0.0.1-test")
        found = kas_bundle.installed_bundle(version="0.0.1-test")
        assert found is not None and found.root == home / "0.0.1-test"

    def test_install_pinned_downloads_nothing_when_the_package_has_it(self, roots, monkeypatch):
        packaged, _home = roots
        _engine_tree(packaged)
        monkeypatch.setattr(
            kas_bundle, "fetch_manifest", lambda *a, **k: pytest.fail("fetched a packaged engine")
        )
        assert kas_bundle.install_pinned("https://kas.test").packaged

    def test_the_harness_knows_the_packaged_node_is_crews_engine(self, roots):
        """The spawn env (system CA, owned endpoint, deferred MCPs) is the bundle's
        for a packaged engine too: one root list feeds the lookup and the check."""
        from kiro_crew.acp.harness.kas import _runs_crew_engine_bundle

        packaged, _home = roots
        _engine_tree(packaged)
        found = kas_bundle.installed_bundle()
        assert found is not None and found.packaged
        assert _runs_crew_engine_bundle(str(found.node_bin))
        assert not _runs_crew_engine_bundle(str(packaged.parent / "kiro-cli"))

    def test_the_package_root_is_inside_the_sealed_install_tree(self):
        """``sandbox_tiers`` seals the ``kiro_crew`` package directory read-only; the
        packaged engine is under it, so no new seal is needed."""
        import kiro_crew

        root = kas_bundle.packaged_kas_root()
        assert root.parent == Path(kiro_crew.__file__).resolve().parent


class TestStartupInstall:
    def test_a_missing_engine_is_installed_at_start(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **_k: None)
        monkeypatch.setattr(
            kas_engine_install,
            "install_pinned_engine",
            lambda base, *, caller: calls.append(caller),
        )
        kas_engine_startup.ensure_engine_at_startup()
        assert calls == ["startup"]

    def test_an_installed_engine_starts_nothing(self, monkeypatch):
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **_k: object())
        monkeypatch.setattr(
            kas_engine_install, "install_pinned_engine", lambda *a, **k: pytest.fail("installed")
        )
        kas_engine_startup.ensure_engine_at_startup()

    def test_a_failed_install_is_logged_loud_and_never_raises(self, monkeypatch, caplog):
        def fail(*_a, **_k):
            raise kas_bundle.KasBundleError("offline")

        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **_k: None)
        monkeypatch.setattr(kas_engine_install, "install_pinned_engine", fail)
        with caplog.at_level("ERROR", logger=kas_engine_startup.__name__):
            kas_engine_startup.ensure_engine_at_startup()
        assert "started without its KAS engine: offline" in caplog.text

    def test_the_skip_knob_makes_the_startup_install_a_no_op(self, monkeypatch):
        monkeypatch.setenv(kas_engine_startup.SKIP_ENGINE_INSTALL_ENV, "1")
        monkeypatch.setattr(
            kas_bundle, "installed_bundle", lambda **_k: pytest.fail("looked up the engine")
        )
        kas_engine_startup.ensure_engine_at_startup()

    def test_a_version_override_is_not_installed_unattended(self, monkeypatch):
        monkeypatch.setattr(kas_bundle, "installed_bundle", lambda **_k: None)
        monkeypatch.setattr(kas_bundle, "pinned_version", lambda: "0.0.1-test")
        monkeypatch.setattr(
            kas_engine_install, "install_pinned_engine", lambda *a, **k: pytest.fail("installed")
        )
        kas_engine_startup.ensure_engine_at_startup()


#: The pure wheel's WHEEL as setuptools writes it: the headers, then a blank line.
_SETUPTOOLS_WHEEL = (
    "Wheel-Version: 1.0\nGenerator: setuptools (84.2.1)\nRoot-Is-Purelib: true\n"
    "Tag: py3-none-any\n\n"
)


def _pure_wheel(tmp_path: Path, version: str = "9.9.9") -> Path:
    wheel = tmp_path / f"kirocrew-{version}-py3-none-any.whl"
    dist = f"kirocrew-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("kiro_crew/__init__.py", "")
        zf.writestr(f"{dist}/WHEEL", _SETUPTOOLS_WHEEL)
        zf.writestr(f"{dist}/RECORD", "")
    return wheel


def _pip_tags(wheel_meta: str) -> list[str]:
    """The tags `pip check` reads: a blank line ends the header block."""
    return Parser().parsestr(wheel_meta).get_all("Tag", [])


class TestBuilder:
    @pytest.fixture
    def builder(self, tmp_path, monkeypatch):
        module = _load_builder()

        def stage(dest, tag, base_url):
            home = _engine_tree(dest, node_key="linux-x64")
            return kas_bundle.installed_bundle(root=dest, version=home.name)

        monkeypatch.setattr(module, "stage_engine", stage)
        monkeypatch.setattr(
            module,
            "_pinned_digests",
            lambda base, system, machine: ("e" * 64, "f" * 64, "linux-x64"),
        )
        return module

    def test_the_platform_wheel_carries_the_engine_pip_can_install(self, builder, tmp_path):
        out = builder.build(_pure_wheel(tmp_path), "manylinux_2_28_x86_64", tmp_path, "https://k")
        assert out.name == "kirocrew-9.9.9-py3-none-manylinux_2_28_x86_64.whl"
        with zipfile.ZipFile(out) as zf:
            wheel_meta = zf.read("kirocrew-9.9.9.dist-info/WHEEL").decode()
            assert _pip_tags(wheel_meta) == ["py3-none-manylinux_2_28_x86_64"]
            assert wheel_meta == (
                "Wheel-Version: 1.0\nGenerator: setuptools (84.2.1)\nRoot-Is-Purelib: true\n"
                "Tag: py3-none-manylinux_2_28_x86_64\n"
            )
            record = zf.read("kirocrew-9.9.9.dist-info/RECORD").decode().splitlines()
            for line in record:
                name, digest, size = line.rsplit(",", 2)
                if not digest:
                    continue
                data = zf.read(name)
                want = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                assert digest == "sha256=" + want.decode() and int(size) == len(data)
            node = next(n for n in zf.namelist() if n.endswith("/bin/node"))
            assert node.startswith(f"kiro_crew/_kas_engine/{KAS_PIN_VERSION}/")
            assert (zf.getinfo(node).external_attr >> 16) & stat.S_IXUSR
        assert "KAS" in builder.verify_wheel(out, "https://k")

    def test_the_gate_checks_every_other_file_against_the_pure_wheel(self, builder, tmp_path):
        pure = _pure_wheel(tmp_path)
        out = builder.build(pure, "manylinux_2_28_x86_64", tmp_path, "https://k")
        assert "KAS" in builder.verify_wheel(out, "https://k", pure=pure)
        changed = tmp_path / "changed"
        changed.mkdir()
        other = changed / pure.name
        with zipfile.ZipFile(pure) as src, zipfile.ZipFile(other, "w") as dst:
            for info in src.infolist():
                data = src.read(info)
                dst.writestr(
                    info, b"# changed\n" if info.filename.endswith("__init__.py") else data
                )
        with pytest.raises(builder.GateError, match="differs from the pure wheel"):
            builder.verify_wheel(out, "https://k", pure=other)

    def test_the_gate_refuses_a_tag_pip_does_not_read(self, builder, tmp_path):
        """A Tag after the blank line is body text: pip reads no tag and calls the
        wheel unsupported, so the gate must refuse it too."""
        out = builder.build(_pure_wheel(tmp_path), "manylinux_2_28_x86_64", tmp_path, "https://k")
        broken = tmp_path / "broken" / out.name
        broken.parent.mkdir()
        wheel_name = "kirocrew-9.9.9.dist-info/WHEEL"
        with zipfile.ZipFile(out) as src, zipfile.ZipFile(broken, "w") as dst:
            for info in src.infolist():
                data = src.read(info)
                if info.filename == wheel_name:
                    data = (
                        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n\n"
                        b"Tag: py3-none-manylinux_2_28_x86_64\n"
                    )
                dst.writestr(info, data)
        with pytest.raises(builder.GateError, match="WHEEL metadata tags are"):
            builder.verify_wheel(broken, "https://k")

    def test_the_gate_refuses_a_wheel_without_the_engine(self, builder, tmp_path):
        bare = tmp_path / "kirocrew-9.9.9-py3-none-manylinux_2_28_x86_64.whl"
        _pure_wheel(tmp_path).rename(bare)
        with pytest.raises(builder.GateError, match="carries no KAS"):
            builder.verify_wheel(bare, "https://k")

    def test_the_gate_refuses_another_node_than_the_tag_needs(self, builder, tmp_path, monkeypatch):
        out = builder.build(_pure_wheel(tmp_path), "manylinux_2_28_x86_64", tmp_path, "https://k")
        monkeypatch.setattr(
            builder, "_pinned_digests", lambda *a: ("e" * 64, "f" * 64, "linux-arm64")
        )
        with pytest.raises(builder.GateError, match="Node is"):
            builder.verify_wheel(out, "https://k")

    def test_a_link_out_of_the_engine_tree_is_refused(self, builder, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tmp_path / "outside").write_text("x", encoding="utf-8")
        (tree / "link").symlink_to(tmp_path / "outside")
        with pytest.raises(builder.GateError, match="links outside"):
            builder._flatten_links(tree)


class TestHostTag:
    @pytest.mark.parametrize(
        ("system", "machine", "glibc", "mac", "tag"),
        [
            ("Linux", "x86_64", (2, 35), "", "manylinux_2_28_x86_64"),
            ("Linux", "aarch64", (2, 28), "", "manylinux_2_28_aarch64"),
            ("Linux", "x86_64", (2, 26), "", None),
            ("Linux", "x86_64", None, "", None),
            ("Darwin", "arm64", None, "14.5", "macosx_11_0_arm64"),
            ("Darwin", "x86_64", None, "11.0", "macosx_11_0_x86_64"),
            ("Darwin", "x86_64", None, "10.15.7", None),
            ("Darwin", "x86_64", None, "", None),
            ("Windows", "AMD64", None, "", None),
            ("Linux", "riscv64", (2, 39), "", None),
        ],
    )
    def test_the_host_tag(self, monkeypatch, system, machine, glibc, mac, tag):
        from kiro_crew import constants

        monkeypatch.setattr(constants, "_host_glibc_version", lambda: glibc)
        assert wheel_engine_platform.host_wheel_tag(system, machine, mac) == tag


def test_every_list_of_platform_tags_is_the_same_list():
    """The builder, the update, cli.sh and both workflows name the same wheels."""
    builder = set(_load_builder().PLATFORM_TAGS)
    assert builder == set(wheel_engine_platform.PLATFORM_WHEEL_TAGS)
    build_yml = (_REPO / ".github/workflows/build-platform-wheels.yml").read_text(encoding="utf-8")
    assert set(re.findall(r"- tag: (\S+)", build_yml)) == builder
    publish_yml = (_REPO / ".github/workflows/publish-cli.yml").read_text(encoding="utf-8")
    want = re.search(r'WANT="([^"]+)"', publish_yml)
    assert want is not None and set(want.group(1).split()) == builder
    cli_sh = (_REPO / "cli.sh").read_text(encoding="utf-8")
    assert set(re.findall(r'"((?:manylinux|macosx)_[a-z0-9_]+)"', cli_sh)) == builder
    assert set(_load_signer().PLATFORM_WHEEL_TAGS) == builder


def test_the_boot_path_calls_the_facade_the_test_floor_stubs():
    """``conftest_floor_services._no_engine_install_at_gateway_start`` stubs the facade
    attribute; a boot path that called the owner directly would download in tests."""
    for rel in (
        "src/kiro_crew/dashboard/server.py",
        "src/kiro_crew/dashboard/server_runtime/api_entrypoint.py",
    ):
        source = (_REPO / rel).read_text(encoding="utf-8")
        assert "_kas_bundle.ensure_engine_at_startup" in source, rel
        assert "kas_engine_startup" not in source, rel


def _load_signer():
    spec = importlib.util.spec_from_file_location(
        "cli_manifest_signer", _REPO / "packaging" / "signing" / "cli-manifest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("kirocrew-9.9.9-py3-none-any.whl", True),
        ("kirocrew-9.9.9-py3-none-manylinux_2_28_x86_64.whl", True),
        ("kirocrew-9.9.9-py3-none-win_amd64.whl", False),
        ("kirocrew_lts-9.9.9-py3-none-any.whl", False),
    ],
)
def test_the_signer_accepts_exactly_the_published_wheel_names(name, ok):
    signer = _load_signer()
    payload = {
        "algorithm": signer.ALGORITHM,
        "channel": "stable",
        "key_id": "sha256:" + "a" * 64,
        "pub_date": "2026-01-01T00:00:00Z",
        "python_requires": ">=3.12",
        "schema": signer.SCHEMA,
        "sha256": "b" * 64,
        "version": "9.9.9",
        "wheel_url": f"https://download.test/cli/stable/9.9.9/{name}",
    }
    if ok:
        assert signer._validate_signed_payload(payload)["wheel_url"].endswith(name)
    else:
        with pytest.raises(signer.ManifestError):
            signer._validate_signed_payload(payload)
