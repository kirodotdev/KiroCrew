"""The backend SDK's descriptor-pinned path walk and copy (``backend_sdk.pinned``).

What is pinned is what an app cannot check for itself: each name answers with the
gateway's own ``kiro_crew.pinned_fs`` function, looked up when it is called, and
raises the gateway's own refusal class; the copy refuses what the private copy
refuses and offers none of the private keywords no app uses; and a spawned child
reaches the names without loading the gateway's security stack.
"""

from __future__ import annotations

import inspect
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.apps.backend_sdk import pinned as sdk_pinned
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_SRC = Path(__file__).resolve().parents[1] / "src"

#: The App Kit seam level that publishes the pinned path names.
_PINNED_LEVEL = 14

_needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="no dir_fd or O_NOFOLLOW on this platform"
)


class _Reasons:
    """An ``on_skip`` reporter that keeps the reason codes it is handed."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, reason: str, _path: str) -> None:
        self.seen.append(reason)


def _identity(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


# ── the names and their level ───────────────────────────────────────────────


def test_every_pinned_name_is_published_at_level_fourteen() -> None:
    assert sdk.SDK_LEVEL >= _PINNED_LEVEL
    for name in sdk_pinned.__all__:
        assert name in sdk.__all__ and name in dir(sdk), name
        assert sdk._LAZY_EXPORTS[name] == "pinned", name
        assert sdk._EXPORT_LEVELS[name] == _PINNED_LEVEL, name
        assert getattr(sdk, name) is getattr(sdk_pinned, name), name
    routed = {name for name, module in sdk._LAZY_EXPORTS.items() if module == "pinned"}
    assert routed == set(sdk_pinned.__all__)


def test_the_refusal_is_the_gateway_class() -> None:
    assert sdk.PinnedPathRefusal is pinned_fs.PinnedPathRefusal
    assert not issubclass(sdk.PinnedPathRefusal, OSError)


def test_a_spawned_child_reaches_the_pinned_names_without_the_security_stack(tmp_path) -> None:
    probe = (
        "import sys\n"
        "import kiro_crew.apps.backend_sdk as sdk\n"
        "print(sdk.supports_pinned_walk() in (True, False), sdk.is_reparse_point(sys.argv[1]))\n"
        "print(*(m in sys.modules for m in "
        "('kiro_crew.hooks', 'kiro_crew.sandbox', 'kiro_crew.security')))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(_SRC),
        "KIROCREW_SPAWNED": "1",
        "KIROCREW_APP_NAME": "probe-app",
    }
    done = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    answered, loaded = done.stdout.split("\n")[:2]
    assert answered == "True False"
    assert loaded == "False False False"


# ── the platform probe and the name screen ──────────────────────────────────


@pytest.mark.parametrize("answer", [False, True])
def test_supports_pinned_walk_asks_the_gateway_at_call_time(monkeypatch, answer) -> None:
    monkeypatch.setattr("kiro_crew.pinned_fs.supports_pinned_walk", lambda: answer)
    assert sdk.supports_pinned_walk() is answer


def test_is_reparse_point_sees_a_link_and_not_a_real_directory(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    make_dir_link(link, real)
    assert sdk.is_reparse_point(link) is True
    assert sdk.is_reparse_point(str(link)) is True
    assert sdk.is_reparse_point(real) is False


def test_is_reparse_point_asks_the_gateway_at_call_time(monkeypatch, tmp_path) -> None:
    seen: list[object] = []
    monkeypatch.setattr("kiro_crew.pinned_fs.is_reparse_point", lambda p: seen.append(p) or 1)
    assert sdk.is_reparse_point(tmp_path) is True
    assert seen == [os.fspath(tmp_path)]


# ── the pinned walk ─────────────────────────────────────────────────────────


@_needs_pinned_walk
def test_pin_parent_opens_a_real_chain_and_refuses_a_linked_component(tmp_path) -> None:
    base = Path(os.path.realpath(tmp_path))
    real = base / "real"
    real.mkdir()
    make_dir_link(base / "link", real)
    fd = sdk.pin_parent(str(real), what="probe directory")
    try:
        opened = os.fstat(fd)
    finally:
        os.close(fd)
    assert (opened.st_dev, opened.st_ino) == _identity(real)
    with pytest.raises(sdk.PinnedPathRefusal):
        sdk.pin_parent(str(base / "link"), what="probe directory")


def test_pin_parent_forwards_only_what(monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return 7

    monkeypatch.setattr("kiro_crew.pinned_fs.pin_parent", fake)
    assert sdk.pin_parent("/p", what="w") == 7
    assert calls == [(("/p",), {"what": "w"})]
    assert "refusal" not in inspect.signature(sdk.pin_parent).parameters


# ── the pinned copy ─────────────────────────────────────────────────────────


@_needs_pinned_walk
def test_copy_file_pinned_copies_through_a_pinned_parent_and_reports_its_witness(
    tmp_path,
) -> None:
    base = Path(os.path.realpath(tmp_path))
    source_dir = base / "in"
    source_dir.mkdir()
    source = source_dir / "recording.wav"
    source.write_bytes(b"RIFF-probe-bytes")
    out = base / "out"
    out.mkdir()
    dst = out / "recording.wav"
    reasons = _Reasons()
    created: list[os.stat_result] = []
    fd = sdk.pin_parent(str(source_dir), what="probe source")
    try:
        copied = sdk.copy_file_pinned(
            str(source),
            str(dst),
            dir_fd=fd,
            name=source.name,
            force_mode=0o600,
            max_bytes=1024,
            expected_src_ident=_identity(source),
            on_skip=reasons,
            on_created=created.append,
        )
    finally:
        os.close(fd)
    assert copied is True
    assert reasons.seen == []
    assert dst.read_bytes() == b"RIFF-probe-bytes"
    assert [(c.st_dev, c.st_ino) for c in created] == [_identity(dst)]
    assert stat.S_IMODE(os.stat(dst).st_mode) == 0o600


def test_copy_file_pinned_refuses_an_inode_other_than_the_vetted_one(tmp_path) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"vetted")
    other = tmp_path / "b.bin"
    other.write_bytes(b"other")
    dst = tmp_path / "copy.bin"
    reasons = _Reasons()
    copied = sdk.copy_file_pinned(
        str(source), str(dst), expected_src_ident=_identity(other), on_skip=reasons
    )
    assert copied is False
    assert reasons.seen == ["identity_changed"]
    assert not dst.exists()


def test_copy_file_pinned_refuses_past_its_cap_before_creating_the_copy(tmp_path) -> None:
    source = tmp_path / "big.bin"
    source.write_bytes(b"0123456789A")
    dst = tmp_path / "copy.bin"
    reasons = _Reasons()
    assert sdk.copy_file_pinned(str(source), str(dst), max_bytes=10, on_skip=reasons) is False
    assert reasons.seen == ["too_large"]
    assert not dst.exists()
    # Without a reporter the refusal is the same answer, not a crash.
    assert sdk.copy_file_pinned(str(source), str(dst), max_bytes=10) is False
    assert not dst.exists()


def test_copy_file_pinned_refuses_a_hardlinked_source(tmp_path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"x")
    hard = tmp_path / "hard.bin"
    os.link(target, hard)
    dst = tmp_path / "copy.bin"
    reasons = _Reasons()
    assert sdk.copy_file_pinned(str(hard), str(dst), on_skip=reasons) is False
    assert reasons.seen == ["not_regular"]
    assert not dst.exists()


@pytest.mark.skipif(
    platform_compat.IS_WINDOWS, reason="no O_NOFOLLOW, and a file symlink needs a privilege"
)
def test_copy_file_pinned_refuses_a_link_at_the_final_name(tmp_path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"x")
    soft = tmp_path / "soft.bin"
    soft.symlink_to(target)
    reasons = _Reasons()
    first, judged = tmp_path / "first.bin", tmp_path / "judged.bin"
    assert sdk.copy_file_pinned(str(soft), str(first), on_skip=reasons) is False
    assert (
        sdk.copy_file_pinned(
            str(soft), str(judged), expected_src_ident=_identity(target), on_skip=reasons
        )
        is False
    )
    assert reasons.seen == ["symlink", "identity_changed"]
    assert not first.exists() and not judged.exists()


def test_copy_file_pinned_never_overwrites_an_existing_destination(tmp_path) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"new")
    dst = tmp_path / "copy.bin"
    dst.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        sdk.copy_file_pinned(str(source), str(dst))
    assert dst.read_bytes() == b"old"


@pytest.mark.parametrize(
    "knob", ["src_fd", "dst_dir_fd", "dst_name", "skip_existing", "skip_unreadable", "on_opened"]
)
def test_copy_file_pinned_offers_only_the_keywords_the_apps_use(knob) -> None:
    assert knob in inspect.signature(pinned_fs.copy_file_pinned).parameters
    assert knob not in inspect.signature(sdk.copy_file_pinned).parameters


def test_copy_file_pinned_delegates_at_call_time_with_private_keywords(monkeypatch) -> None:
    private = set(inspect.signature(pinned_fs.copy_file_pinned).parameters)
    public = set(inspect.signature(sdk.copy_file_pinned).parameters) - {"src", "dst"}
    calls: list[tuple[tuple, dict]] = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return 1

    def report(_reason: str, _path: str) -> None:
        return None

    def witness(_st: os.stat_result) -> None:
        return None

    monkeypatch.setattr("kiro_crew.pinned_fs.copy_file_pinned", fake)
    copied = sdk.copy_file_pinned(
        "/s",
        "/d",
        dir_fd=3,
        name="s",
        force_mode=0o600,
        max_bytes=9,
        expected_src_ident=(1, 2),
        on_skip=report,
        on_created=witness,
    )
    assert copied is True
    ((args, kwargs),) = calls
    assert args == ("/s", "/d")
    assert kwargs == {
        "dir_fd": 3,
        "name": "s",
        "force_mode": 0o600,
        "max_bytes": 9,
        "expected_src_ident": (1, 2),
        "on_skip": report,
        "on_created": witness,
    }
    assert set(kwargs) == public
    assert set(kwargs) <= private


# ── the descriptor's real path ──────────────────────────────────────────────


def test_fd_real_path_names_the_opened_file_and_fails_closed(tmp_path) -> None:
    target = tmp_path / "witness.txt"
    target.write_bytes(b"x")
    fd = os.open(target, os.O_RDONLY)
    try:
        got = sdk.fd_real_path(fd)
    finally:
        os.close(fd)
    assert got is not None
    assert os.path.normcase(os.path.normpath(got)) == os.path.normcase(os.path.realpath(target))
    # A descriptor number far above any real table: no route may guess.
    assert sdk.fd_real_path(2**30) is None
