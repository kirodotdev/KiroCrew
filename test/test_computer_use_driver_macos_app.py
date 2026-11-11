"""The macOS driver app at ``/Applications/CuaDriver.app``: placed with consent, verified.

``install_mod.APPLICATIONS_DIR`` points at a temporary directory in every case, so no
test touches the real ``/Applications``. Code signing is answered by a fake runner
that reads a marker file inside the bundle, so a "foreign" bundle can fail it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from kiro_crew.computer_use.driver_runtime import install as inst
from kiro_crew.computer_use.driver_runtime import pin

BINARY = b"#!/bin/sh\necho cua-driver 0.47.8\n"
TOP = "cua-driver-rs-0.47.8-darwin-universal"
APP_BIN = f"{TOP}/CuaDriver.app/Contents/MacOS/cua-driver"
GOOD_SIG = "Identifier=com.trycua.driver\nTeamIdentifier=YCK386LBJ7\n"


def _archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (
            (APP_BIN, BINARY),
            (f"{TOP}/CuaDriver.app/Contents/Info.plist", b"<plist/>"),
            (f"{TOP}/CuaDriver.app/Contents/sig", b"good"),
        ):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


DATA = _archive()
ASSET = pin.PinnedAsset(
    os=pin.OS_MACOS,
    arch=pin.ARCH_UNIVERSAL,
    name="cua-driver-rs-0.47.8-darwin-universal.tar.gz",
    size=len(DATA),
    sha256=hashlib.sha256(DATA).hexdigest(),
    binary=APP_BIN,
    binary_sha256=hashlib.sha256(BINARY).hexdigest(),
)


class _Resp(io.BytesIO):
    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Runner:
    """Fake ``codesign`` and ``--version``; records every argv it is asked to run."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        if argv[0].endswith("codesign"):
            app = Path(argv[-1])
            sig = app / "Contents" / "sig"
            ok = sig.is_file() and sig.read_bytes() == b"good"
            if "--verify" in argv:
                return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "")
            return subprocess.CompletedProcess(
                argv, 0, "", GOOD_SIG if ok else "TeamIdentifier=OTHER\n"
            )
        assert argv[-1] == "--version"
        version = "0.47.8" if Path(argv[0]).read_bytes() == BINARY else "0.40.0"
        return subprocess.CompletedProcess(argv, 0, f"cua-driver {version}\n", "")

    def executed(self) -> list[str]:
        return [c[0] for c in self.calls if c[-1] == "--version"]


@pytest.fixture
def apps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "Applications"
    root.mkdir()
    monkeypatch.setattr(inst, "APPLICATIONS_DIR", root)
    monkeypatch.setattr(inst, "_pinned_for_host", lambda asset: asset or ASSET)
    return root


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


def _install(home: Path, runner: Runner) -> inst.InstallState:
    return inst.install(
        home,
        asset=ASSET,
        os_id=pin.OS_MACOS,
        **_serve(DATA),
        run=runner,
        progress=inst.InstallProgress(),
    )


def _foreign(apps: Path, *, binary: bytes = BINARY, sig: bytes = b"good") -> Path:
    app = apps / "CuaDriver.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "cua-driver").write_bytes(binary)
    (app / "Contents" / "sig").write_bytes(sig)
    return app


def test_install_places_the_verified_app_and_records_ownership(apps: Path, tmp_path: Path) -> None:
    runner = Runner()
    state = _install(tmp_path / "home", runner)
    app = apps / "CuaDriver.app"
    assert state.installed and state.binary == app / inst.MACOS_APP_BINARY
    assert (app / inst.MACOS_APP_BINARY).read_bytes() == BINARY
    record = json.loads(inst.record_path(tmp_path / "home").read_text())
    assert record["applications_app"] == {
        "path": str(app),
        "owned": True,
        "binary_sha256": ASSET.binary_sha256,
    }
    assert not [p for p in apps.iterdir() if p.name != "CuaDriver.app"]


def test_every_start_re_verifies_the_applications_copy(apps: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install(home, Runner())
    runner = Runner()
    sealed = inst.version_dir(home) / inst.asset_for(inst.OS_MACOS).binary  # type: ignore[union-attr]
    # The supervisor gets the SEALED copy: /Applications is checked, never executed.
    assert inst.verify_installed(home, run=runner) == (sealed, "")
    assert all(str(apps) not in cmd[0] for cmd in runner.executed())
    target = apps / "CuaDriver.app" / inst.MACOS_APP_BINARY
    target.write_bytes(b"#!/bin/sh\necho planted\n")
    runner = Runner()
    path, why = inst.verify_installed(home, run=runner)
    assert path is None and "pinned" in why
    assert runner.executed() == []


def test_a_broken_signature_on_the_applications_copy_never_runs(apps: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install(home, Runner())
    (apps / "CuaDriver.app" / "Contents" / "sig").write_bytes(b"tampered")
    runner = Runner()
    path, why = inst.verify_installed(home, run=runner)
    assert path is None and "signature" in why and runner.executed() == []


def test_an_identical_existing_copy_is_used_but_not_owned(apps: Path, tmp_path: Path) -> None:
    _foreign(apps)
    state = _install(tmp_path / "home", Runner())
    assert state.installed
    record = json.loads(inst.record_path(tmp_path / "home").read_text())
    assert record["applications_app"]["owned"] is False


@pytest.mark.parametrize(
    "binary, sig, why",
    [
        (b"#!/bin/sh\necho cua-driver 0.40.0\n", b"good", "pinned"),
        (BINARY, b"other team", "signature"),
    ],
)
def test_a_different_existing_copy_is_never_used_or_touched(
    apps: Path, tmp_path: Path, binary: bytes, sig: bytes, why: str
) -> None:
    app = _foreign(apps, binary=binary, sig=sig)
    runner = Runner()
    with pytest.raises(inst.InstallError) as err:
        _install(tmp_path / "home", runner)
    assert err.value.stage == inst.STAGE_PLACING
    assert "another version of CuaDriver" in err.value.detail and why in err.value.detail
    assert (app / inst.MACOS_APP_BINARY).read_bytes() == binary
    assert (app / "Contents" / "sig").read_bytes() == sig
    assert str(app / inst.MACOS_APP_BINARY) not in runner.executed()
    assert not inst.install_state(tmp_path / "home").installed


def test_a_link_at_the_app_path_is_refused(apps: Path, tmp_path: Path) -> None:
    real = _foreign(tmp_path)
    (apps / "CuaDriver.app").symlink_to(real)
    with pytest.raises(inst.InstallError, match="link"):
        _install(tmp_path / "home", Runner())
    assert (apps / "CuaDriver.app").is_symlink()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_a_read_only_applications_dir_is_reported_never_escalated(
    apps: Path, tmp_path: Path
) -> None:
    os.chmod(apps, 0o555)
    runner = Runner()
    try:
        with pytest.raises(inst.InstallError, match="not writable"):
            _install(tmp_path / "home", runner)
    finally:
        os.chmod(apps, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
    assert not any("sudo" in " ".join(c) for c in runner.calls)
    assert not (apps / "CuaDriver.app").exists()


def test_uninstall_removes_only_an_unchanged_copy_we_placed(apps: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install(home, Runner())
    assert inst.uninstall(home) == "removed the driver app we placed"
    assert not (apps / "CuaDriver.app").exists()


def test_uninstall_leaves_a_copy_changed_after_we_placed_it(apps: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install(home, Runner())
    (apps / "CuaDriver.app" / inst.MACOS_APP_BINARY).write_bytes(b"updated by someone else")
    assert "left in place" in inst.uninstall(home)
    assert (apps / "CuaDriver.app").exists()


def test_uninstall_never_removes_a_copy_we_did_not_place(apps: Path, tmp_path: Path) -> None:
    _foreign(apps)
    home = tmp_path / "home"
    _install(home, Runner())
    assert inst.uninstall(home) == "no driver app of ours to remove"
    assert (apps / "CuaDriver.app" / inst.MACOS_APP_BINARY).read_bytes() == BINARY


def test_a_removed_app_reads_as_not_installed(apps: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    _install(home, Runner())
    inst._remove_tree(apps / "CuaDriver.app")
    state = inst.install_state(home)
    assert not state.installed and "no longer in" in state.detail
