"""Pin table, environment fence and the verified install of the upstream driver."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import types
import zipfile
from pathlib import Path

import pytest

from kiro_crew.computer_use.driver_runtime import env as denv
from kiro_crew.computer_use.driver_runtime import install as inst
from kiro_crew.computer_use.driver_runtime import pin

# ── pin ──


class TestPin:
    def test_every_asset_names_the_pinned_version_and_a_full_digest(self) -> None:
        for (os_id, arch), asset in pin.ASSETS.items():
            assert asset.os == os_id and asset.arch == arch
            assert f"-{pin.DRIVER_VERSION}-" in asset.name
            assert len(asset.sha256) == 64 and int(asset.sha256, 16) >= 0
            assert asset.size > 1_000_000
            assert asset.url.startswith("https://github.com/trycua/cua/releases/download/")
            assert pin.RELEASE_TAG in asset.url

    def test_linux_and_windows_use_the_binary_archives_macos_the_signed_bundle(self) -> None:
        for (os_id, _arch), asset in pin.ASSETS.items():
            if os_id in (pin.OS_LINUX, pin.OS_WINDOWS):
                assert "-binary." in asset.name
        mac = pin.asset_for(pin.OS_MACOS)
        assert mac is not None and mac.name.endswith("darwin-universal.tar.gz")
        assert mac.binary.startswith(
            f"cua-driver-rs-{pin.DRIVER_VERSION}-darwin-universal/CuaDriver.app/"
        )
        assert mac.bundle.endswith("/CuaDriver.app")
        for asset in pin.ASSETS.values():
            assert len(asset.binary_sha256) == 64

    @pytest.mark.parametrize(
        "machine, arch",
        [
            ("x86_64", pin.ARCH_X86_64),
            ("AMD64", pin.ARCH_X86_64),
            ("aarch64", pin.ARCH_ARM64),
            ("arm64", pin.ARCH_ARM64),
        ],
    )
    def test_machine_aliases(self, machine: str, arch: str) -> None:
        asset = pin.asset_for(pin.OS_LINUX, machine)
        assert asset is not None and asset.arch == arch

    def test_unknown_machine_has_no_asset(self) -> None:
        assert pin.asset_for(pin.OS_LINUX, "riscv64") is None
        assert pin.asset_for("solaris", "x86_64") is None


# ── env fence ──


class TestEnvFence:
    BASE = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "DISPLAY": ":1",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus",
        "LC_ALL": "C.UTF-8",
        "AWS_SECRET_ACCESS_KEY": "s3cret",
        "AWS_PROFILE": "prod",
        "GITHUB_TOKEN": "ghp_x",
        "HTTPS_PROXY": "http://user:pw@proxy:3128",
        "https_proxy": "http://user:pw@proxy:3128",
        "CUA_DRIVER_RS_TELEMETRY_ENABLED": "1",
        "DO_NOT_TRACK": "0",
        "CUA_DRIVER_RS_UPDATE_CHECK": "true",
        "CUA_DRIVER_PERMISSION_MODE": "unrestricted",
        "CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS": "1",
        "CUA_DRIVER_RS_HOME": "/home/u/.cua-driver",
        "CUA_DRIVER_POLICY_FILE": "/tmp/p.yaml",
        "KIROCREW_INTERNAL_SECRET": "abc",
    }

    def test_allowlist_keeps_only_desktop_basics(self, tmp_path: Path) -> None:
        env = denv.driver_env(self.BASE, tmp_path, case_insensitive=False)
        for kept in ("PATH", "HOME", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "LC_ALL"):
            assert env[kept] == self.BASE[kept]
        for dropped in (
            "AWS_SECRET_ACCESS_KEY",
            "AWS_PROFILE",
            "GITHUB_TOKEN",
            "HTTPS_PROXY",
            "https_proxy",
            "CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS",
            "CUA_DRIVER_POLICY_FILE",
            "KIROCREW_INTERNAL_SECRET",
        ):
            assert dropped not in env

    def test_telemetry_and_update_switches_are_forced(self, tmp_path: Path) -> None:
        env = denv.driver_env(self.BASE, tmp_path, case_insensitive=False)
        assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
        assert env["CUA_TELEMETRY_ENABLED"] == "0"
        assert env["CUA_TELEMETRY"] == "0"
        assert env["DO_NOT_TRACK"] == "1"
        assert env["CUA_DRIVER_RS_UPDATE_CHECK"] == "false"
        assert env["CUA_DRIVER_PERMISSION_MODE"] == "standard"

    def test_private_home_replaces_any_inherited_home(self, tmp_path: Path) -> None:
        env = denv.driver_env(self.BASE, tmp_path, case_insensitive=False)
        assert env["CUA_DRIVER_RS_HOME"] == str(tmp_path)
        assert env["CUA_HOME"] == str(tmp_path)

    def test_windows_names_match_case_insensitively(self, tmp_path: Path) -> None:
        base = {"Path": "C:\\Windows", "SystemRoot": "C:\\Windows", "Secret": "x"}
        env = denv.driver_env(base, tmp_path, case_insensitive=True)
        assert env["Path"] == "C:\\Windows" and env["SystemRoot"] == "C:\\Windows"
        assert "Secret" not in env


# ── install ──


def _tar_bytes(
    members: "dict[str, bytes]", *, links: "dict[str, str] | None" = None, mode: int = 0o755
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return buf.getvalue()


def _asset_for(
    data: bytes,
    name: str = "cua-driver-rs-0.47.8-linux-x86_64-binary.tar.gz",
    binary: str = "cua-driver",
) -> pin.PinnedAsset:
    return pin.PinnedAsset(
        os=pin.OS_LINUX,
        arch=pin.ARCH_X86_64,
        name=name,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        binary=binary,
        binary_sha256=hashlib.sha256(BINARY).hexdigest(),
    )


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


#: What the faked network answers with: (body, list of requested urls or None).
_SERVED: "list[tuple[bytes, list[str] | None]]" = [(b"", None)]


@pytest.fixture(autouse=True)
def _fake_network(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every download through the shared downloader's opener seam."""
    import types

    from kiro_crew import asset_downloader

    def open_url(req, timeout=None):  # type: ignore[no-untyped-def]
        data, seen = _SERVED[0]
        if seen is not None:
            seen.append(getattr(req, "full_url", req))
        resp = _Resp(data)
        resp.status = 200  # type: ignore[attr-defined]
        resp.headers = {}  # type: ignore[attr-defined]
        return resp

    _floor_monkeypatch.setattr(
        asset_downloader, "build_opener", lambda *_a, **_k: types.SimpleNamespace(open=open_url)
    )
    _SERVED[0] = (b"", None)


def _serve(data: bytes, seen: "list[str] | None" = None) -> "dict[str, object]":
    """Serve *data* to the next downloads; returns no install kwargs."""
    _SERVED[0] = (data, seen)
    return {}


def _version_ok(argv, **kwargs):  # type: ignore[no-untyped-def]
    assert argv[-1] == "--version"
    # The version probe runs with the fenced environment, never the gateway's.
    assert kwargs["env"]["DO_NOT_TRACK"] == "1"
    return subprocess.CompletedProcess(argv, 0, stdout="cua-driver 0.47.8\n", stderr="")


def _version_wrong(argv, **kwargs):  # type: ignore[no-untyped-def]
    return subprocess.CompletedProcess(argv, 0, stdout="cua-driver 0.47.9\n", stderr="")


BINARY = b"#!/bin/sh\necho cua-driver 0.47.8\n"


def _install(home: Path, data: bytes, asset: "pin.PinnedAsset | None" = None, run=_version_ok):  # type: ignore[no-untyped-def]
    return inst.install(
        home,
        asset=asset or _asset_for(data),
        os_id=pin.OS_LINUX,
        **_serve(data),
        run=run,
        progress=inst.InstallProgress(),
    )


class TestInstall:
    def test_happy_path_installs_read_only_and_records_the_binary_hash(
        self, tmp_path: Path
    ) -> None:
        data = _tar_bytes({"cua-driver": BINARY, "cua_driver_abi.h": b"x"})
        seen: list[str] = []
        state = inst.install(
            tmp_path,
            asset=_asset_for(data),
            os_id=pin.OS_LINUX,
            **_serve(data, seen),
            run=_version_ok,
            progress=inst.InstallProgress(),
        )
        assert state.installed and state.binary is not None
        assert seen == [_asset_for(data).url]
        record = json.loads(inst.record_path(tmp_path).read_text())
        assert record["binary_sha256"] == hashlib.sha256(BINARY).hexdigest()
        assert record["version"] == pin.DRIVER_VERSION
        assert not list(inst.driver_root(tmp_path).glob(f"{inst.STAGING_PREFIX}*"))
        if os.name != "nt":
            assert not (state.binary.parent.stat().st_mode & 0o222)
        inst.uninstall(tmp_path)
        assert not inst.install_state(tmp_path, asset=_asset_for(data)).installed

    def test_size_mismatch_refuses_before_extraction(self, tmp_path: Path) -> None:
        data = _tar_bytes({"cua-driver": BINARY})
        with pytest.raises(inst.InstallError) as err:
            _install(tmp_path, data + b"\0", asset=_asset_for(data))
        assert err.value.stage == inst.STAGE_VERIFYING
        assert not inst.version_dir(tmp_path).exists()
        assert not inst.install_state(tmp_path, asset=_asset_for(data)).installed

    def test_digest_mismatch_refuses(self, tmp_path: Path) -> None:
        data = _tar_bytes({"cua-driver": BINARY})
        tampered = data[:-1] + bytes([data[-1] ^ 1])
        with pytest.raises(inst.InstallError, match="sha256"):
            _install(tmp_path, tampered, asset=_asset_for(data))
        assert not inst.version_dir(tmp_path).exists()

    def test_version_mismatch_refuses_to_install(self, tmp_path: Path) -> None:
        data = _tar_bytes({"cua-driver": BINARY})
        with pytest.raises(inst.InstallError) as err:
            _install(tmp_path, data, run=_version_wrong)
        assert err.value.stage == inst.STAGE_VERSION
        assert not inst.version_dir(tmp_path).exists()

    @pytest.mark.parametrize(
        "members, links",
        [
            ({"../evil": b"x"}, None),
            ({"/etc/evil": b"x"}, None),
            ({"a/../../evil": b"x"}, None),
            ({"cua-driver": BINARY}, {"link": "/etc/passwd"}),
            ({"cua-driver": BINARY}, {"sub/link": "../../outside"}),
        ],
    )
    def test_unsafe_tar_member_refused(self, tmp_path: Path, members, links) -> None:  # type: ignore[no-untyped-def]
        data = _tar_bytes(members, links=links)
        home = tmp_path / "home"
        with pytest.raises(inst.UnsafeArchiveMember):
            _install(home, data)
        assert not (tmp_path / "evil").exists()
        assert not inst.version_dir(home).exists()

    def test_extracted_binary_must_match_its_pinned_hash(self, tmp_path: Path) -> None:
        data = _tar_bytes({"cua-driver": b"#!/bin/sh\necho other\n"})
        with pytest.raises(inst.InstallError, match="pinned hash"):
            _install(tmp_path, data)
        assert not inst.version_dir(tmp_path).exists()

    def test_link_inside_the_archive_is_allowed(self, tmp_path: Path) -> None:
        data = _tar_bytes(
            {"cua-driver": BINARY, "lib/real.so": b"x"}, links={"lib/alias.so": "real.so"}
        )
        assert _install(tmp_path, data).installed

    def test_unsafe_zip_member_refused(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("cua-driver.exe", b"MZ")
            zf.writestr("..\\evil.dll", b"x")
        data = buf.getvalue()
        asset = _asset_for(
            data, name="cua-driver-rs-0.47.8-windows-x86_64-binary.zip", binary="cua-driver.exe"
        )
        with pytest.raises(inst.UnsafeArchiveMember):
            _install(tmp_path, data, asset=asset)

    def test_non_https_url_refused(self) -> None:
        real = _asset_for(b"x")
        asset = types.SimpleNamespace(
            url="http://example.invalid/x", sha256=real.sha256, size=real.size, name=real.name
        )
        with pytest.raises(inst.InstallError, match="non-https"):
            inst.download_verified(asset, Path(__file__).with_name("never-written.bin"))

    def test_concurrent_install_refused(self, tmp_path: Path) -> None:
        progress = inst.InstallProgress()
        progress.lock.acquire()
        try:
            with pytest.raises(inst.InstallError, match="already running"):
                inst.install(
                    tmp_path,
                    asset=_asset_for(b"x"),
                    os_id=pin.OS_LINUX,
                    **_serve(b"x"),
                    run=_version_ok,
                    progress=progress,
                )
        finally:
            progress.lock.release()


class TestVerifyInstalled:
    DATA = _tar_bytes({"cua-driver": BINARY})

    def _installed(self, tmp_path: Path) -> Path:
        state = _install(tmp_path, self.DATA)
        assert state.binary is not None
        return state.binary

    def _verify(self, tmp_path: Path, run=_version_ok):  # type: ignore[no-untyped-def]
        return inst.verify_installed(tmp_path, asset=_asset_for(self.DATA), run=run)

    def test_intact_install_verifies(self, tmp_path: Path) -> None:
        binary = self._installed(tmp_path)
        assert self._verify(tmp_path) == (binary, "")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_a_rewritten_record_cannot_bless_a_replaced_binary(self, tmp_path: Path) -> None:
        binary = self._installed(tmp_path)
        os.chmod(binary.parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        os.chmod(binary, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        evil = b"#!/bin/sh\necho cua-driver 0.47.8\n# planted\n"
        binary.write_bytes(evil)
        record = json.loads(inst.record_path(tmp_path).read_text())
        record["binary_sha256"] = hashlib.sha256(evil).hexdigest()
        inst.record_path(tmp_path).write_text(json.dumps(record))
        calls: list[object] = []
        path, reason = self._verify(tmp_path, run=lambda argv, **kw: calls.append(argv))
        assert path is None and "pinned hash" in reason and calls == []

    def test_an_install_for_another_host_is_not_installed(self, tmp_path: Path) -> None:
        self._installed(tmp_path)
        other = pin.ASSETS[(pin.OS_WINDOWS, pin.ARCH_X86_64)]
        assert not inst.install_state(tmp_path, asset=other).installed

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_replaced_binary_never_executes(self, tmp_path: Path) -> None:
        binary = self._installed(tmp_path)
        os.chmod(binary.parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        os.chmod(binary, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
        binary.write_bytes(b"#!/bin/sh\necho pwned\n")
        calls: list[object] = []

        def run(argv, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(argv)
            return _version_ok(argv, **kwargs)

        path, reason = self._verify(tmp_path, run=run)
        assert path is None and "hash" in reason
        assert calls == []

    def test_version_drift_refuses(self, tmp_path: Path) -> None:
        self._installed(tmp_path)
        path, reason = self._verify(tmp_path, run=_version_wrong)
        assert path is None and pin.DRIVER_VERSION in reason

    def test_record_for_another_version_is_not_installed(self, tmp_path: Path) -> None:
        self._installed(tmp_path)
        record = json.loads(inst.record_path(tmp_path).read_text())
        record["version"] = "0.1.0"
        inst.record_path(tmp_path).write_text(json.dumps(record))
        state = inst.install_state(tmp_path, asset=_asset_for(self.DATA))
        assert not state.installed and "0.1.0" in state.detail


class TestSignatures:
    def _cp(self, rc: int, out: str = "", err: str = "") -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)

    def test_macos_bundle_must_match_team_and_identifier(self, tmp_path: Path) -> None:
        good = "Identifier=com.trycua.driver\nTeamIdentifier=YCK386LBJ7\n"
        answers = iter([self._cp(0), self._cp(0, err=good)])
        inst.verify_macos_bundle(tmp_path, run=lambda argv, **kw: next(answers))
        other = "Identifier=com.trycua.driver\nTeamIdentifier=ZZZZZ99999\n"
        wrong_team = iter([self._cp(0), self._cp(0, err=other)])
        with pytest.raises(inst.InstallError, match="team"):
            inst.verify_macos_bundle(tmp_path, run=lambda argv, **kw: next(wrong_team))
        bad_sig = iter([self._cp(1), self._cp(0, err=good)])
        with pytest.raises(inst.InstallError, match="does not verify"):
            inst.verify_macos_bundle(tmp_path, run=lambda argv, **kw: next(bad_sig))

    def test_windows_signer_must_be_valid_and_pinned(self, tmp_path: Path) -> None:
        signed = 'Valid\nCN="Cua AI, Inc.", O="Cua AI, Inc."\n'
        inst.verify_windows_signature(tmp_path, run=lambda argv, **kw: self._cp(0, out=signed))
        with pytest.raises(inst.InstallError, match="not valid"):
            inst.verify_windows_signature(
                tmp_path, run=lambda argv, **kw: self._cp(0, out="NotSigned\n\n")
            )
        with pytest.raises(inst.InstallError, match="publisher"):
            inst.verify_windows_signature(
                tmp_path, run=lambda argv, **kw: self._cp(0, out="Valid\nCN=Someone Else\n")
            )


class TestSubjectCommonName:
    @pytest.mark.parametrize(
        "subject, cn",
        [
            ('CN="Cua AI, Inc.", O="Cua AI, Inc.", L=San Francisco, C=US', "Cua AI, Inc."),
            ("CN=Cua AI, O=Other", "Cua AI"),
            ('O="Cua AI, Inc.", CN=Someone Else', "Someone Else"),
            ('CN="Cua AI, Inc. Evil", O=x', "Cua AI, Inc. Evil"),
            ("O=No CN here", ""),
            ("", ""),
        ],
    )
    def test_cn_is_parsed_not_substring_matched(self, subject: str, cn: str) -> None:
        assert inst.subject_common_name(subject) == cn


def test_windows_signature_check_puts_the_path_inside_the_script(tmp_path: Path) -> None:
    """``powershell -Command <script> <arg>`` appends <arg> to the script; it is never $args."""
    seen: list[list[str]] = []
    exe = tmp_path / "it's" / "cua-driver.exe"

    def run(argv, **kw):  # type: ignore[no-untyped-def]
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='Valid\nCN="Cua AI, Inc."\n', stderr="")

    inst.verify_windows_signature(exe, run=run)
    script = seen[0][-1]
    assert seen[0][-2] == "-Command"
    assert "-LiteralPath '" + str(exe).replace("'", "''") + "'" in script
    assert "$args" not in script


def test_the_runtime_leaf_is_sealed_in_every_fence() -> None:
    """``<data-home>/computer-use`` holds what the gateway executes: same seals as Harness."""
    from kiro_crew import sandbox_leaves, sandbox_precreate
    from kiro_crew.security import paths_lists

    leaf = inst.RUNTIME_LEAF
    assert leaf == "computer-use"
    assert leaf in sandbox_leaves._CREW_READONLY_LEAVES
    assert leaf in sandbox_leaves._CREW_CHILD_WITHHELD_LEAVES
    assert leaf in sandbox_precreate._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._DELEGATED_OVERLAP_LEAF_REASONS
    assert leaf in paths_lists._CREW_SECRET_LEAVES


def test_an_empty_runtime_dir_reads_as_not_installed(tmp_path: Path) -> None:
    """The pre-create rule: an empty sealed directory must mean what an absent one does."""
    inst.runtime_root(tmp_path).mkdir(parents=True)
    assert not inst.install_state(tmp_path).installed
    assert inst.read_record(tmp_path) == {}
