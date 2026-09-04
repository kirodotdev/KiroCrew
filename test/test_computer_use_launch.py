"""``computer_launch_app``: resolution trust, the dispatch path, and the fake seam.

The tests here are about ONE question — what may this verb start? — because it is
the only verb in the package that creates a process, and every other protection in
computer use assumes a window already exists.

Split by what each group can be honest about:

* :class:`TestLaunchResolutionTrust` runs on any OS against a fabricated catalog, so
  the protected-root and basename rules are asserted as LOGIC rather than against
  whatever happens to be installed on the runner;
* :class:`TestLaunchDispatch` drives the real chokepoint through the shipped fake, so
  the denylist ordering and the launch-then-snapshot shape are covered on Linux CI;
* :class:`TestWindowsHostCatalog` is Windows-only and asserts the invariant against
  the host's REAL catalog — it is the one that would catch a host where the rule does
  not hold.
"""

from __future__ import annotations

import inspect
import json
import os

import pytest

from kiro_crew.computer_use import backend as cu_backend
from kiro_crew.computer_use import index as cu_index
from kiro_crew.computer_use import policy
from kiro_crew.computer_use import service as cu_service
from kiro_crew.computer_use import tools
from kiro_crew.computer_use.types import (
    ERR_LAUNCH_ALREADY_RUNNING,
    ERROR_PREFIX,
    TOOL_GET_STATE,
    TOOL_LAUNCH_APP,
    AmbiguousLaunchTarget,
    AppRef,
    ComputerUseError,
    LaunchIdentity,
    NoSuchLaunchTarget,
)
from kiro_crew.platform_compat import IS_WINDOWS
from kiro_crew.testing.fake_computer_use import (
    FAKE_DRAW_APP,
    FAKE_FILES_APP,
    FakeComputerUseBackend,
)

# The registry these tests swap is process-wide, so they must not run beside another
# test that also swaps it.
pytestmark = pytest.mark.xdist_group("computer_use_launch")

_SESSION = "cli_chat"


@pytest.fixture
def fake_computer_backend(tmp_path, monkeypatch):
    """The shipped fake, registered process-wide, with the keystone enable on.

    ``KIROCREW_HOME`` is redirected first: the dispatcher refuses everything before
    reaching a driver unless the keystone says enabled, and a developer's real
    ``~/.kiro/crew`` must never decide a test's outcome.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "computer_use.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    fake = FakeComputerUseBackend()
    cu_backend.register_computer_use_backend(lambda: fake)
    cu_backend.reset_shared_backend()
    cu_service.reset_shared_service()
    cu_index.reset_shared_index()
    try:
        yield fake
    finally:
        cu_backend.register_computer_use_backend(None)
        cu_backend.reset_shared_backend()
        cu_service.reset_shared_service()
        cu_index.reset_shared_index()


def _running_elevated() -> bool:
    """Whether this process has an administrator token. ``False`` off Windows.

    Two host-catalog assertions are only meaningful for an ORDINARY user: an elevated process
    can write every install root and owns content inside them, so the resolver correctly
    refuses everything and the assertion would be measuring privilege rather than the rule.
    CI's ``windows-latest`` runner is elevated.
    """
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an unknown privilege level is not a test failure
        return False


def _launch(app: str) -> str:
    return tools.dispatch_tool(TOOL_LAUNCH_APP, {"app": app}, session_key=_SESSION)


class TestLaunchResolutionTrust:
    """The protected-root and basename rules, against a FABRICATED catalog.

    Fabricated on purpose. Asserting these against the host's real registry would
    make the test's strength depend on what is installed, and the rules are what has
    to hold for a catalog entry the agent WROTE — which no real host provides.

    **Runs on every platform, and that is what makes the coverage honest.**
    ``launch_windows`` imports anywhere (``winreg`` is deferred into ``_winreg()`` for
    exactly this reason), and every rule here is pure path logic: a protected root is
    whatever ``_protected_roots`` answers, so a ``tmp_path`` directory serves as one. Gating
    the class on ``IS_WINDOWS`` left the module at 22.8% on the Linux shards that measure
    coverage, while the branches that decide what may RUN went unmeasured on 3 of 4 CI
    platforms. The genuinely host-dependent assertions live in
    :class:`TestWindowsHostCatalog`, which stays Windows-only.
    """

    @staticmethod
    def _protected(monkeypatch, root, *, writable=False):
        r"""Treat *root* as a protected install root, with its create-probe scripted.

        Stands in for ``C:\Program Files``: ``_under_protected`` compares against
        whatever ``_protected_roots`` returns, so the rule is assertable without the
        operator's real filesystem — and without the privilege dependence that made three
        of these tests fail on CI's elevated runner, which genuinely can write ``System32``.
        """
        from kiro_crew.computer_use import launch_windows

        monkeypatch.setattr(launch_windows, "_protected_roots", lambda: (str(root),))
        monkeypatch.setattr(launch_windows, "_directory_is_writable", lambda _d: writable)
        monkeypatch.setattr(launch_windows, "_file_is_replaceable", lambda _p: False)
        return launch_windows

    @staticmethod
    def _binary(root, name, *, body=b"MZ"):
        """A file under *root* standing in for an installed executable."""
        root.mkdir(parents=True, exist_ok=True)
        target = root / name
        target.write_bytes(body)
        return str(target)

    @staticmethod
    def _catalog(monkeypatch, entries):
        from kiro_crew.computer_use import launch_windows

        monkeypatch.setattr(launch_windows, "installed_apps", lambda: tuple(entries))
        return launch_windows

    @staticmethod
    def _entry(key: str, executable: str):
        from kiro_crew.computer_use.launch_windows import InstalledApp

        stem = key[:-4] if key.lower().endswith(".exe") else key
        return InstalledApp(key=key, name=stem, executable=executable, source="test")

    def test_a_target_outside_the_protected_roots_is_refused(self, monkeypatch, tmp_path):
        # THE central rule. An agent can write HKCU's App Paths (measured), so a
        # catalog entry naming a binary it dropped in its own directory is the exact
        # attack this verb has to refuse — and tmp_path is that directory.
        planted = tmp_path / "mspaint.exe"
        planted.write_bytes(b"MZ")
        # A protected root elsewhere, so ``tmp_path`` is outside it. Injected because the
        # real ``_protected_roots`` reads the registry, which does not exist on Linux.
        self._protected(monkeypatch, tmp_path / "Program Files")
        launch = self._catalog(monkeypatch, [self._entry("mspaint.exe", str(planted))])
        with pytest.raises(ComputerUseError) as caught:
            launch.resolve_target("mspaint")
        assert "protected install directories" in str(caught.value)

    def test_a_target_whose_basename_disagrees_with_its_key_is_refused(self, monkeypatch, tmp_path):
        # The second half of the rule, and the one that stops a REDIRECT: an agent
        # that rewrites a writable catalog value can only aim it at a file already
        # present under a protected root, so the remaining move is aiming
        # "mspaint.exe" at some other installed binary. Only the basename differs from
        # the passing case below, so a failure here names the rule that broke.
        root = tmp_path / "Program Files"
        real = self._binary(root, "calc.exe")
        self._protected(monkeypatch, root)
        launch = self._catalog(monkeypatch, [self._entry("mspaint.exe", real)])
        with pytest.raises(ComputerUseError) as caught:
            launch.resolve_target("mspaint")
        assert "calc.exe" in str(caught.value)

    def test_a_protected_target_named_after_its_key_resolves(self, monkeypatch, tmp_path):
        # The positive control. Without it the two refusals above would also pass on
        # an implementation that refused everything.
        root = tmp_path / "Program Files"
        real = self._binary(root, "notepad.exe")
        self._protected(monkeypatch, root)
        launch = self._catalog(monkeypatch, [self._entry("notepad.exe", real)])
        assert launch.resolve_target("notepad") == (real, "notepad")

    def test_a_junction_cannot_borrow_a_protected_prefix(self, monkeypatch, tmp_path):
        # ``_under_protected`` realpaths BEFORE comparing, which is what stops a link
        # under a writable directory presenting a protected-looking path. Asserted
        # against the resolver directly because a junction needs no elevation to
        # create, so this is a move the agent really has
        # available — unlike the reverse (a junction UNDER Program Files), which the
        # OS refuses outright.
        launch = self._catalog(monkeypatch, [])
        self._protected(monkeypatch, tmp_path / "Program Files")
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "Program Files"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            pytest.skip("this host does not permit creating a directory link")
        planted = target / "notepad.exe"
        planted.write_bytes(b"MZ")
        assert launch._under_protected(str(link / "notepad.exe")) is False

    @pytest.mark.skipif(not IS_WINDOWS, reason="asserts the real registry-vs-environment source")
    def test_the_protected_roots_do_NOT_come_from_the_environment(self, monkeypatch, tmp_path):
        """THE attack that defeats every other check in one line.

        ``%ProgramFiles%`` and ``%SystemRoot%`` are ordinary environment variables, and
        ``HKCU\\Environment`` is writable without elevation — so anyone who can set one
        can nominate a directory they WRITE as a "protected" root, after which the
        protected-root verification accepts a binary planted there. Both halves were
        live bypasses during development:

        * reading the install roots from ``os.environ`` (fixed by
          :func:`~kiro_crew.computer_use.launch_windows._install_roots_from_registry`);
        * ``platform_compat._windows_system_dirs`` appending
          ``%SystemRoot%\\System32`` *unconditionally* rather than as a fallback, so it
          was added even while ``GetSystemDirectoryW`` answered normally.

        **Every variable is planted at BOTH depths**, and that is the point of the
        parametrization rather than thoroughness for its own sake: the first version of
        this test planted only ``<tmp>/mspaint.exe`` and therefore could not see the
        ``SystemRoot`` bypass at all, because that one injects ``<tmp>/System32`` — one
        level deeper than the file it was checking. A guard test that inspects the wrong
        path is worse than no guard test, since it reads as coverage.
        """
        from kiro_crew import platform_compat
        from kiro_crew.computer_use import launch_windows

        # Both the root itself and the System32 child an env-derived root expands to.
        for relative in ("", "System32", os.path.join("System32", "WindowsPowerShell", "v1.0")):
            (tmp_path / relative).mkdir(parents=True, exist_ok=True)
            (tmp_path / relative / "evil.exe").write_bytes(b"MZ")

        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "SystemRoot"):
            monkeypatch.setenv(var, str(tmp_path))

        roots = launch_windows._protected_roots()
        assert roots, "no protected root resolved at all"
        planted_root = str(tmp_path).casefold()
        for root in roots:
            assert not root.casefold().startswith(planted_root), root
        for relative in ("", "System32", os.path.join("System32", "WindowsPowerShell", "v1.0")):
            candidate = str(tmp_path / relative / "evil.exe")
            assert launch_windows._under_protected(candidate) is False, candidate
        # The shared helper is where the SystemRoot half lived, so it is asserted
        # directly too: a caller-side filter in launch_windows would leave
        # ``trusted_system_bin`` — which has the same distrust goal — still exposed.
        for directory in platform_compat._windows_system_dirs():
            assert not directory.casefold().startswith(planted_root), directory

    @pytest.mark.skipif(not IS_WINDOWS, reason="asserts real writable descendants of a real root")
    def test_a_WRITABLE_DESCENDANT_of_a_protected_root_is_refused(self, monkeypatch):
        """An unwritable root can contain writable children, so a prefix test is not it.

        Measured on this host: ``C:\\Windows\\Temp``, ``C:\\Windows\\Tasks`` and
        ``C:\\Windows\\System32\\spool\\drivers\\color`` are all writable by an
        unprivileged user while their parents are not. A prefix test against
        ``C:\\Windows`` therefore accepted a planted ``C:\\Windows\\Temp\\Evil.exe``, and
        an ``App Paths`` entry in the writable ``HKCU`` hive can name exactly that.

        Two changes close it: ``C:\\Windows`` is no longer a root at all (``System32``
        comes from ``platform_compat`` instead), and the file's own directory is probed
        for writability. This asserts the second, because the first alone would leave the
        two writable directories still under ``System32``.
        """
        from kiro_crew.computer_use import launch_windows

        # ``C:\Windows`` must not be a root at all — that is half the fix, and it is
        # assertable without touching anything: the paths under it are rejected at the
        # prefix stage, before any probe.
        windows_root = os.environ.get("SystemRoot", r"C:\Windows")
        assert launch_windows._under_protected(os.path.join(windows_root, "Temp", "Evil.exe")) is (
            False
        )
        assert launch_windows._under_protected(os.path.join(windows_root, "Tasks", "Evil.exe")) is (
            False
        )

        # The two holes UNDER ``System32`` reach the writability probe, and probing them
        # for real would create files in the operator's system directories — which a test
        # must not do. So the probe is scripted to report what this host actually
        # measured (both writable) and the REFUSAL is what is asserted.
        measured_writable = {
            os.path.join(windows_root, "System32", "spool", "drivers", "color"),
            os.path.join(windows_root, "System32", "Microsoft", "Crypto", "RSA", "MachineKeys"),
        }
        # Compared CASE-FOLDED after ``realpath``: ``_under_protected`` resolves the path
        # first, and ``GetSystemDirectoryW`` reports the on-disk casing (``C:\WINDOWS``)
        # while ``%SystemRoot%`` here yields ``C:\Windows``. A case-sensitive membership
        # test silently matches nothing, which would leave this test passing while
        # asserting the wrong branch.
        folded_writable = {os.path.realpath(d).casefold() for d in measured_writable}
        monkeypatch.setattr(
            launch_windows,
            "_directory_is_writable",
            lambda directory: os.path.realpath(directory).casefold() in folded_writable,
        )
        for writable in measured_writable:
            assert launch_windows._under_protected(os.path.join(writable, "Evil.exe")) is False

    def test_a_writable_directory_under_a_protected_root_is_rejected(self, monkeypatch, tmp_path):
        """The general rule, with the writability answer INJECTED rather than measured.

        The honest version of this test walked every protected root and probed each
        directory for real — which meant creating files across the operator's own
        ``System32`` and ``Program Files`` trees. A test must not touch the host
        (AGENTS.md), and "the probe cleans up after itself" is not the same guarantee:
        an interrupted run leaves litter in a system directory.

        So the shape is asserted instead of the host's ACLs: ``tmp_path`` stands in for
        the root, and ``_directory_is_writable`` is scripted. That covers the branch that
        matters — a directory inside a protected root, writable, must be REFUSED — for
        any directory, not only the ones this host happens to have. The specific
        real-world holes are still named by the sibling test above, which calls only
        ``_under_protected`` and creates nothing.
        """
        from kiro_crew.computer_use import launch_windows

        nested = tmp_path / "Program Files" / "App"
        nested.mkdir(parents=True)
        planted = nested / "app.exe"
        planted.write_bytes(b"MZ")
        monkeypatch.setattr(
            launch_windows, "_protected_roots", lambda: (str(tmp_path / "Program Files"),)
        )

        # The FILE answer is injected as well, so this asserts the directory condition
        # alone. A file under ``tmp_path`` is genuinely rewritable by this user, and the
        # separate file probe correctly refuses on it — which would otherwise mask whichever
        # branch this test is about. Its own coverage is
        # ``test_a_WRITABLE_executable_under_a_protected_root_is_refused``.
        monkeypatch.setattr(launch_windows, "_file_is_replaceable", lambda _p: False)

        monkeypatch.setattr(launch_windows, "_directory_is_writable", lambda _d: True)
        assert launch_windows._under_protected(str(planted)) is False
        # And the same path is accepted once its directory is not writable, so the
        # refusal above is attributable to writability and nothing else.
        monkeypatch.setattr(launch_windows, "_directory_is_writable", lambda _d: False)
        assert launch_windows._under_protected(str(planted)) is True

    def test_a_legitimate_system_binary_still_resolves(self, monkeypatch, tmp_path):
        """The positive control: the probe must not have turned every target into a
        refusal.

        ``_directory_is_writable`` is scripted to ``False`` for the real ``System32``
        rather than measured, because measuring it means creating a file in the
        operator's system directory. What this pins is that a real path under a real
        protected root, in a directory reported unwritable, is ACCEPTED — the branch the
        writability change could have broken.
        """
        from kiro_crew.computer_use import launch_windows

        root = tmp_path / "Program Files"
        real = self._binary(root, "notepad.exe")
        self._protected(monkeypatch, root)
        assert launch_windows._under_protected(real) is True

    def test_a_writable_INTERMEDIATE_directory_is_refused(self, monkeypatch, tmp_path):
        r"""Every level up to the root is probed, not just the executable's own parent.

        Write access to any single directory on the path is enough to substitute the code
        that runs: an agent owning an intermediate ``Program Files\Vendor`` renames its
        ``App`` child aside and recreates it with its own binary inside, and a parent-only
        probe then answers "unwritable" for the leaf it just created. Measured against the
        earlier revision, which trusted it. ``launch_macos`` probes every level of a bundle
        for the same reason.
        """
        launch_windows = self._protected(monkeypatch, tmp_path)
        leaf = tmp_path / "Vendor" / "App"
        planted = self._binary(leaf, "app.exe")
        monkeypatch.setattr(
            launch_windows,
            "_directory_is_writable",
            lambda d: os.path.basename(str(d).rstrip(os.sep)) == "Vendor",
        )
        assert launch_windows._under_protected(planted) is False

    def test_the_walk_stops_at_the_protected_ROOT(self, monkeypatch, tmp_path):
        """The root is probed and nothing above it is.

        Where the guarantee comes from: everything above the protected root is off the
        caller's path by construction, since the prefix test has already placed the target
        inside it. Walking further would make an unrelated ancestor — a writable drive root
        on some hosts — refuse every application under it.
        """
        launch_windows = self._protected(monkeypatch, tmp_path / "Program Files")
        leaf = tmp_path / "Program Files" / "Vendor" / "App"
        planted = self._binary(leaf, "app.exe")
        probed: list[str] = []
        monkeypatch.setattr(
            launch_windows,
            "_directory_is_writable",
            lambda d: probed.append(str(d)) or False,
        )
        assert launch_windows._under_protected(planted) is True
        assert probed == [
            str(leaf),
            str(tmp_path / "Program Files" / "Vendor"),
            str(tmp_path / "Program Files"),
        ]

    def test_resolution_returns_the_VERIFIED_path_not_the_catalog_value(
        self, monkeypatch, tmp_path
    ):
        """What gets spawned must be the string that was checked.

        The catalog value and its ``realpath`` differ in the last component whenever a link
        or an 8.3 alias is involved, so returning the raw value would hand
        ``spawn_detached`` a path whose final component the verification never examined.
        ``launch_macos`` returns its verified bundle path for the same reason.
        """
        root = tmp_path / "Program Files"
        real = self._binary(root, "app.exe")
        short = str(root / "APP~1.EXE")
        launch = self._protected(monkeypatch, root)
        monkeypatch.setattr(launch, "installed_apps", lambda: (self._entry("app.exe", short),))
        monkeypatch.setattr(launch.os.path, "realpath", lambda p: real if p == short else p)
        assert launch.resolve_target("app") == (real, "app")

    def test_a_WRITABLE_executable_under_a_protected_root_is_refused(self, monkeypatch, tmp_path):
        """Create-permission and replace-permission are different questions on Windows too.

        The directory probe asks only *can I create a new file here*; rewriting an existing
        file's bytes needs write on the file itself. So a parent that refuses creates can
        still hold an executable this user rewrites with ``open(path, "r+b")`` — measured —
        and the protected-root test alone accepted it. The macOS sibling makes the same
        distinction, and a target must fail BOTH questions to be trusted.
        """
        from kiro_crew.computer_use import launch_windows

        planted = tmp_path / "app.exe"
        planted.write_bytes(b"MZ")
        monkeypatch.setattr(launch_windows, "_protected_roots", lambda: (str(tmp_path),))
        monkeypatch.setattr(launch_windows, "_directory_is_writable", lambda _d: False)
        assert launch_windows._under_protected(str(planted)) is False

    @pytest.mark.skipif(not IS_WINDOWS, reason="asserts the real System32 ACL")
    def test_a_real_system_binary_is_still_trusted(self, monkeypatch):
        """The control, and the reason ``os.access`` is not the probe.

        On Windows ``os.access(.., W_OK)`` reports the read-only ATTRIBUTE and never consults
        the ACL, so it answers True for every ``System32`` binary — using it would refuse the
        whole catalog. Measured from an unelevated shell: ``os.access`` says writable for
        ``System32\\notepad.exe`` while opening it ``O_RDWR`` is denied. This asserts the real
        binary stays trusted, which an ``os.access``-based check could not.
        """
        from kiro_crew.computer_use import launch_windows

        real = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")
        if not os.path.isfile(real):
            pytest.skip("no System32 notepad.exe on this host")
        if launch_windows._file_is_replaceable(real):
            # An elevated process really can rewrite it, and refusing is then correct.
            pytest.skip("this process can write System32, so the refusal is the right answer")
        monkeypatch.setattr(launch_windows, "_directory_is_writable", lambda _d: False)
        assert launch_windows._under_protected(real) is True

    def test_a_file_this_user_OWNS_is_replaceable_whatever_its_ACL_says(
        self, monkeypatch, tmp_path
    ):
        """A current write denial is not durable, so it cannot be the whole test.

        The OWNER of a Windows object holds ``WRITE_DAC`` implicitly, so an agent that plants
        a binary and then denies itself write can revoke that deny ACE again with no
        privilege. Measured: ``icacls <file> /deny <me>:(WD)`` makes the open fail and
        ``icacls <file> /remove:d <me>`` makes it succeed again, both as an ordinary user. So
        ownership decides, exactly as ``st_uid`` does on macOS.

        The ownership answer is injected rather than staged, because ``takeown`` needs a
        privilege the suite may not have and the rule must hold either way.
        """
        from kiro_crew.computer_use import launch_windows

        target = tmp_path / "app.exe"
        target.write_bytes(b"MZ")
        monkeypatch.setattr(launch_windows, "_owned_by_current_user", lambda _p: True)
        # Even with write denied at the ACL — which is what the raw open would report.
        monkeypatch.setattr(
            launch_windows.os,
            "open",
            lambda *_a, **_k: (_ for _ in ()).throw(PermissionError("Access is denied")),
        )
        assert launch_windows._file_is_replaceable(str(target)) is True

    def test_an_UNKNOWABLE_owner_fails_CLOSED(self, monkeypatch, tmp_path):
        # ``None`` means "could not determine", which is not evidence that the file belongs
        # to someone else — a filesystem with no security information, or a call that errors.
        from kiro_crew.computer_use import launch_windows

        target = tmp_path / "app.exe"
        target.write_bytes(b"MZ")
        monkeypatch.setattr(launch_windows, "_owned_by_current_user", lambda _p: None)
        assert launch_windows._file_is_replaceable(str(target)) is True

    def test_the_owner_probe_never_raises(self, monkeypatch, tmp_path):
        # It is consulted on every launch, so an exception here would fail the tool call
        # rather than the target. Driven through a missing file and a broken native call.
        from kiro_crew.computer_use import launch_windows

        assert launch_windows._owned_by_current_user(str(tmp_path / "absent.exe")) in (
            False,
            None,
        )
        monkeypatch.setattr(launch_windows.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            "ctypes.WinDLL",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("no such library")),
            raising=False,
        )
        assert launch_windows._owned_by_current_user(str(tmp_path)) is None

    def test_the_owner_probe_is_a_NO_OP_off_Windows(self, monkeypatch, tmp_path):
        # ``None`` on a non-Windows host, so the module stays importable and the macOS
        # sibling's own ownership rule is the one that applies there.
        from kiro_crew.computer_use import launch_windows

        monkeypatch.setattr(launch_windows.platform_compat, "IS_WINDOWS", False)
        assert launch_windows._owned_by_current_user(str(tmp_path)) is None

    def test_the_file_write_probe_modifies_nothing(self, tmp_path):
        # It runs on a binary about to be ALLOWED, so it must not damage a real installed
        # application: ``O_RDWR`` with no ``O_TRUNC`` and no write.
        from kiro_crew.computer_use import launch_windows

        target = tmp_path / "app.exe"
        target.write_bytes(b"MZ-ORIGINAL-BYTES")
        assert launch_windows._file_is_replaceable(str(target)) is True
        assert target.read_bytes() == b"MZ-ORIGINAL-BYTES"

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (PermissionError("denied"), False),
            (FileNotFoundError("gone"), False),
            (IsADirectoryError("a directory"), False),
            (OSError("device not ready"), True),
        ],
    )
    def test_the_open_probe_fails_CLOSED_on_anything_but_a_denial(
        self, monkeypatch, tmp_path, error, expected
    ):
        """The ACL half of the rule, and its four outcomes.

        A denial is the answer that means "not replaceable"; a path that is not there is not
        replaceable either, and the basename plus protected-root checks have already vouched
        for the path's shape. Anything else is "assume replaceable", so the caller refuses — a
        file that cannot be examined is not evidence that it is trustworthy.

        Ownership is scripted to ``False`` so this reaches the open at all: an owned file
        short-circuits earlier, which is the point of that check and is covered separately.
        """
        from kiro_crew.computer_use import launch_windows

        def boom(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(launch_windows, "_owned_by_current_user", lambda _p: False)
        monkeypatch.setattr(launch_windows.os, "open", boom)
        assert launch_windows._file_is_replaceable(str(tmp_path / "app.exe")) is expected

    def test_an_ACCEPTED_open_means_replaceable_and_closes_the_handle(self, monkeypatch, tmp_path):
        # The positive half: a file the ACL lets this user write is replaceable, and the
        # descriptor is closed rather than leaked into the gateway's table.
        from kiro_crew.computer_use import launch_windows

        target = tmp_path / "app.exe"
        target.write_bytes(b"MZ")
        closed: list[int] = []
        monkeypatch.setattr(launch_windows, "_owned_by_current_user", lambda _p: False)
        monkeypatch.setattr(launch_windows.os, "close", closed.append)
        assert launch_windows._file_is_replaceable(str(target)) is True
        assert len(closed) == 1

    def test_the_write_probe_is_removed(self, tmp_path):
        # The probe is only ever created where the launch then refuses, but a leftover
        # file in a system directory would still be litter with our name on it.
        from kiro_crew.computer_use import launch_windows

        assert launch_windows._directory_is_writable(str(tmp_path)) is True
        assert list(tmp_path.iterdir()) == []

    def test_an_unwritable_directory_answers_False(self, monkeypatch, tmp_path):
        # Driven through a denial rather than by finding a real unwritable directory, so
        # the fail-closed branches are reachable on any host.
        from kiro_crew.computer_use import launch_windows

        def denied(*_args, **_kwargs):
            raise PermissionError("Access is denied")

        monkeypatch.setattr("builtins.open", denied)
        assert launch_windows._directory_is_writable(str(tmp_path)) is False

    @pytest.mark.parametrize("error", [OSError("io"), FileExistsError("collision")])
    def test_an_unexpected_probe_error_fails_CLOSED(self, monkeypatch, tmp_path, error):
        # "Assume writable" is the safe answer: the caller refuses the launch. An
        # unreadable directory is not evidence that a binary inside it is trustworthy.
        from kiro_crew.computer_use import launch_windows

        def boom(*_args, **_kwargs):
            raise error

        monkeypatch.setattr("builtins.open", boom)
        assert launch_windows._directory_is_writable(str(tmp_path)) is True

    def test_a_command_interpreter_is_refused_even_under_a_protected_root(
        self, monkeypatch, tmp_path
    ):
        # A shell passes the protected-root rule (cmd.exe IS in System32) and the
        # basename rule, so it is refused by name. The no-arguments bound that makes
        # every other target safe buys nothing against a process that takes its work
        # from a subsequent keystroke.
        root = tmp_path / "Program Files"
        real = self._binary(root, "cmd.exe")
        self._protected(monkeypatch, root)
        launch = self._catalog(monkeypatch, [self._entry("cmd.exe", real)])
        with pytest.raises(ComputerUseError) as caught:
            launch.resolve_target("cmd")
        assert "command interpreter" in str(caught.value)

    def test_a_shell_QUERY_is_refused_even_when_it_resolves_elsewhere(self, monkeypatch):
        """THE defect a live run found, and the reason there are two shell checks.

        Asking for ``cmd`` on the measured host matched ``IEDIAGCMD.EXE`` — an
        unrelated Internet Explorer diagnostic — under the old substring tier, and the
        resolved-basename check never saw a shell, so it **launched**. Two independent
        failures met: a 3-character fragment matching inside a 9-character name, and a
        guard that only inspected what the name resolved TO.

        Both are fixed, and this pins the first half: the QUERY is refused before
        resolution, so it does not matter what the catalog would have matched.
        """
        launch = self._catalog(
            monkeypatch, [self._entry("IEDIAGCMD.EXE", r"C:\Windows\System32\IEDIAGCMD.EXE")]
        )
        with pytest.raises(ComputerUseError) as caught:
            launch.resolve_target("cmd")
        assert "command interpreter" in str(caught.value)

    def test_the_fuzzy_tier_is_a_PREFIX_not_a_substring(self, monkeypatch):
        """The second half of the same defect.

        A short fragment matching inside a long name is a coincidence rather than an
        intent, and the ambiguity guard cannot catch it because a coincidence usually
        hits exactly ONE entry — which is precisely how ``cmd`` resolved to a single
        unrelated application and was launched. Asserted with a non-shell name so this
        is about the matching rule rather than about the shell list.
        """
        launch = self._catalog(
            monkeypatch, [self._entry("IEDIAGXYZ.EXE", r"C:\Windows\System32\IEDIAGXYZ.EXE")]
        )
        with pytest.raises(NoSuchLaunchTarget):
            launch.resolve_target("xyz")

    def test_a_near_miss_SUGGESTS_the_real_name(self, monkeypatch, tmp_path):
        # Prefix-only matching would otherwise be a dead end for a model that typed a
        # fragment of a real name, and the retry it would reach for is a path — the one
        # shape that can never be served. Suggestions are never launch TARGETS.
        real = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe")
        if not os.path.isfile(real):
            pytest.skip("no System32 notepad.exe on this host")
        launch = self._catalog(monkeypatch, [self._entry("notepad.exe", real)])
        with pytest.raises(NoSuchLaunchTarget) as caught:
            launch.resolve_target("tepad")
        assert "notepad" in caught.value.near

    def test_an_ambiguous_substring_refuses_rather_than_picking(self, monkeypatch):
        # Launching the wrong application is not undoable, so a substring hitting
        # several apps must not resolve to whichever came first. The ambiguity check
        # runs BEFORE any target verification, so the entries need not name real
        # files — and deliberately do not, since two same-prefixed real applications
        # are not something a runner can be relied on to have.
        #
        # The query is a strict PREFIX of both stems, never equal to either: an exact
        # match is resolved first by design (see the sibling test), so a query equal to
        # one of the names would take that branch and never reach the ambiguity rule.
        launch = self._catalog(
            monkeypatch,
            [
                self._entry("noteappone.exe", r"C:\Windows\System32\noteappone.exe"),
                self._entry("noteapptwo.exe", r"C:\Windows\System32\noteapptwo.exe"),
            ],
        )
        with pytest.raises(AmbiguousLaunchTarget) as caught:
            launch.resolve_target("noteapp")
        assert caught.value.count == 2

    def test_an_exact_name_beats_an_ambiguous_prefix(self, monkeypatch, tmp_path):
        # The positive control for the rule above: without it, "refuse when several
        # match" would also refuse the case where one of them is what was asked for.
        root = tmp_path / "Program Files"
        real = self._binary(root, "notepad.exe")
        self._protected(monkeypatch, root)
        launch = self._catalog(
            monkeypatch,
            [
                self._entry("notepad.exe", real),
                self._entry("notepadplus.exe", str(root / "notepadplus.exe")),
            ],
        )
        assert launch.resolve_target("notepad") == (real, "notepad")

    def test_the_launch_argv_is_exactly_the_executable(self, monkeypatch, tmp_path):
        # No document, no flag, no URL. A launch that accepted an argument would be a
        # way to hand attacker-chosen input to an arbitrary installed application,
        # which is a different capability from "open the drawing app".
        from kiro_crew.computer_use import launch_windows

        seen: list[list[str]] = []

        class _Popen:
            def __init__(self, argv, **kwargs):
                seen.append(list(argv))

        monkeypatch.setattr(launch_windows.subprocess, "Popen", _Popen)
        launch_windows.spawn_detached(str(tmp_path / "app.exe"))
        assert seen == [[str(tmp_path / "app.exe")]]


class TestWindowsProtectedRoots:
    """Where the install roots come FROM, against a fake ``winreg``.

    Runs on every platform, and that matters more here than anywhere else in this file: the
    *source* of the roots is the load-bearing part of the whole boundary — reading them from
    ``%ProgramFiles%`` instead of ``HKLM`` was a live bypass during development — and gating
    these on the platform left the branch unmeasured on the shards that compute coverage.
    """

    @staticmethod
    def _reg(monkeypatch, values, *, open_error=False):
        """Install a fake ``winreg`` whose ``CurrentVersion`` key holds *values*."""
        from kiro_crew.computer_use import launch_windows

        class _Reg:
            HKEY_LOCAL_MACHINE = "HKLM"

            def OpenKey(self, _hive, _path):  # noqa: N802 - mirrors winreg's spelling
                if open_error:
                    raise OSError("hive unreadable")
                return "key"

            def QueryValueEx(self, _key, name):  # noqa: N802
                if name not in values:
                    raise OSError("no such value")
                return values[name], 1

            def CloseKey(self, _key):  # noqa: N802
                return None

        monkeypatch.setattr(launch_windows, "_winreg", lambda: _Reg())
        return launch_windows

    def test_the_roots_are_read_from_the_REGISTRY(self, monkeypatch, tmp_path):
        # ``HKLM\...\CurrentVersion`` is unwritable by an unprivileged user, which is the
        # entire reason it is the source rather than ``os.environ``.
        (tmp_path / "Program Files").mkdir()
        launch = self._reg(monkeypatch, {"ProgramFilesDir": str(tmp_path / "Program Files")})
        assert launch._install_roots_from_registry() == [str(tmp_path / "Program Files")]

    def test_a_MISSING_value_is_skipped_not_fatal(self, monkeypatch, tmp_path):
        # ``ProgramW6432Dir`` is absent on a 32-bit image; one missing value must not cost
        # the others.
        (tmp_path / "PF").mkdir()
        launch = self._reg(
            monkeypatch, {"ProgramFilesDir": str(tmp_path / "PF"), "ProgramFilesDir (x86)": "  "}
        )
        assert launch._install_roots_from_registry() == [str(tmp_path / "PF")]

    def test_an_UNREADABLE_key_degrades_to_the_fallbacks(self, monkeypatch):
        # Never an exception: ``_protected_roots`` then falls back to the hardcoded
        # conventional paths, which is strictly narrower than trusting the environment.
        launch = self._reg(monkeypatch, {}, open_error=True)
        assert launch._install_roots_from_registry() == []

    def test_protected_roots_RESOLVES_and_DEDUPLICATES(self, monkeypatch, tmp_path):
        """Every root is ``realpath``-ed, and a duplicate appears once.

        The resolution is what makes ``_under_protected``'s comparison meaningful — it
        compares two resolved paths — and the de-duplication keeps a root that two sources
        both name from being probed twice on every launch.
        """
        real = tmp_path / "Program Files"
        real.mkdir()
        launch = self._reg(
            monkeypatch,
            {"ProgramFilesDir": str(real), "ProgramFilesDir (x86)": str(real)},
        )
        # The second entry is a path that provably does not exist — ``tmp_path`` is fresh, and
        # a literal like ``/nonexistent`` resolves to a drive-root path that is real on some
        # hosts, including this one.
        monkeypatch.setattr(
            launch.platform_compat,
            "_windows_system_dirs",
            lambda: (str(real), str(tmp_path / "absent")),
        )
        monkeypatch.setattr(launch, "_PROTECTED_FALLBACK_ROOTS", ())
        assert launch._protected_roots() == (str(real.resolve()),)

    def test_a_root_that_does_NOT_EXIST_is_dropped(self, monkeypatch, tmp_path):
        # Kept, it would be a prefix that can never match — dead weight probed on every
        # launch. Dropped, the root list says only what it means.
        launch = self._reg(monkeypatch, {"ProgramFilesDir": str(tmp_path / "absent")})
        monkeypatch.setattr(launch.platform_compat, "_windows_system_dirs", lambda: ())
        monkeypatch.setattr(launch, "_PROTECTED_FALLBACK_ROOTS", ())
        assert launch._protected_roots() == ()


class TestWindowsAppPathsReader:
    """``_app_paths_entries`` against a FAKE ``winreg``, so it runs on every platform.

    The reader is the module's only registry contact and its degradation paths are the ones a
    real host cannot produce on demand: a hive that will not open, a key with no default
    value, a value naming a file that does not exist. Injecting the module — which the
    production code already fetches lazily through ``_winreg()``, for import portability —
    covers all of them and makes the Linux shards measure the branch that decides which
    executable a name means.
    """

    @staticmethod
    def _reg(monkeypatch, hives, *, open_error=None):
        """Install a fake ``winreg``. *hives* maps a hive name to ``{key: value}``."""
        from kiro_crew.computer_use import launch_windows

        class _Key:
            def __init__(self, entries, name=None):
                self.entries, self.name = entries, name

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        class _Reg:
            HKEY_LOCAL_MACHINE = "HKEY_LOCAL_MACHINE"
            HKEY_CURRENT_USER = "HKEY_CURRENT_USER"

            def OpenKey(self, hive, path):  # noqa: N802 - mirrors winreg's own spelling
                entries = hives.get(hive)
                if entries is None:
                    raise OSError("no such hive")
                if open_error is not None and path.endswith(open_error):
                    raise OSError("key vanished between enumeration and open")
                tail = path[len(launch_windows._APP_PATHS_KEY) :].lstrip("\\")
                return _Key(entries, tail or None)

            def QueryInfoKey(self, key):  # noqa: N802
                return (len(key.entries), 0, 0)

            def EnumKey(self, key, position):  # noqa: N802
                return sorted(key.entries)[position]

            def QueryValueEx(self, key, _name):  # noqa: N802
                value = key.entries[key.name]
                if value is None:
                    raise OSError("no default value")
                return value, 1

            def CloseKey(self, key):  # noqa: N802
                return None

        monkeypatch.setattr(launch_windows, "_winreg", lambda: _Reg())
        return launch_windows

    def test_it_reads_BOTH_hives_and_prefers_HKLM_on_a_collision(self, monkeypatch, tmp_path):
        # HKCU is agent-writable, so a machine-wide install must win the name — an agent
        # should not be able to change even WHICH installed app a name selects.
        machine = tmp_path / "machine"
        user = tmp_path / "user"
        for root in (machine, user):
            root.mkdir()
            (root / "app.exe").write_bytes(b"MZ")
        launch = self._reg(
            monkeypatch,
            {
                "HKEY_LOCAL_MACHINE": {"app.exe": str(machine / "app.exe")},
                "HKEY_CURRENT_USER": {"app.exe": str(user / "app.exe")},
            },
        )
        entries = launch._app_paths_entries()
        assert [e.executable for e in entries] == [str(machine / "app.exe")]
        assert entries[0].source.startswith("HKLM")

    def test_a_QUOTED_value_is_accepted(self, monkeypatch, tmp_path):
        # Both shapes are documented, so quotes are stripped rather than treated as a
        # parse failure — an entry lost here is an app the model cannot launch.
        (tmp_path / "app.exe").write_bytes(b"MZ")
        launch = self._reg(
            monkeypatch, {"HKEY_LOCAL_MACHINE": {"app.exe": f'"{tmp_path / "app.exe"}"'}}
        )
        assert [e.name for e in launch._app_paths_entries()] == ["app"]

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_an_EMPTY_or_missing_value_is_skipped(self, monkeypatch, value):
        # A key without a usable default value is ordinary on a real host.
        launch = self._reg(monkeypatch, {"HKEY_LOCAL_MACHINE": {"app.exe": value}})
        assert launch._app_paths_entries() == []

    def test_a_value_naming_a_MISSING_file_is_skipped(self, monkeypatch, tmp_path):
        # The listing is honest only if it shows what the OS said AND the file exists;
        # a stale entry would otherwise become a resolution that fails at spawn time.
        launch = self._reg(
            monkeypatch, {"HKEY_LOCAL_MACHINE": {"app.exe": str(tmp_path / "gone.exe")}}
        )
        assert launch._app_paths_entries() == []

    def test_an_UNREADABLE_hive_degrades_to_a_smaller_catalog(self, monkeypatch, tmp_path):
        # Never an exception inside a tool call: one hive missing must still yield the other.
        (tmp_path / "app.exe").write_bytes(b"MZ")
        launch = self._reg(
            monkeypatch, {"HKEY_CURRENT_USER": {"app.exe": str(tmp_path / "app.exe")}}
        )
        assert [e.source for e in launch._app_paths_entries()] == ["HKCU App Paths"]

    def test_a_SUBKEY_that_vanishes_mid_walk_is_skipped(self, monkeypatch, tmp_path):
        # A registry walk is not atomic: an uninstaller can remove a key between the
        # enumeration and the open, and that must cost one entry rather than the catalog.
        (tmp_path / "kept.exe").write_bytes(b"MZ")
        launch = self._reg(
            monkeypatch,
            {
                "HKEY_LOCAL_MACHINE": {
                    "kept.exe": str(tmp_path / "kept.exe"),
                    "vanishing.exe": str(tmp_path / "kept.exe"),
                }
            },
            open_error="vanishing.exe",
        )
        assert [e.name for e in launch._app_paths_entries()] == ["kept"]

    def test_the_stem_drops_only_a_trailing_exe(self, monkeypatch, tmp_path):
        # ``name`` is what a model handed a ``computer_list_apps`` row would type, and both
        # forms are matched by ``resolve_target`` — so a mangled stem is an unlaunchable app.
        for leaf in ("app.exe", "noext"):
            (tmp_path / leaf).write_bytes(b"MZ")
        launch = self._reg(
            monkeypatch,
            {
                "HKEY_LOCAL_MACHINE": {
                    "app.exe": str(tmp_path / "app.exe"),
                    "noext": str(tmp_path / "noext"),
                }
            },
        )
        assert sorted(e.name for e in launch._app_paths_entries()) == ["app", "noext"]


class TestMacOSCatalogAndResolution:
    """``installed_apps`` and ``resolve_target``, with :data:`_APP_ROOTS` INJECTED.

    Runs on every platform, for the same reason the Windows resolver's tests do: an app
    bundle is a directory whose name ends in ``.app``, and the roots are whatever
    ``_APP_ROOTS`` says — so ``tmp_path`` is a conventional root and the rules are
    assertable without macOS. Gating these on the platform left the module measured only by
    its import on the shards that compute coverage, which is how the branches deciding what
    may RUN went uncovered.
    """

    @staticmethod
    def _roots(monkeypatch, *roots):
        from kiro_crew.computer_use import launch_macos

        monkeypatch.setattr(launch_macos, "_APP_ROOTS", tuple(str(r) for r in roots))
        monkeypatch.setattr(launch_macos, "_writable_component", lambda _app: False)
        return launch_macos

    @staticmethod
    def _bundle(root, name):
        (root / f"{name}.app" / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        return root / f"{name}.app"

    def test_the_catalog_lists_bundles_and_skips_everything_else(self, monkeypatch, tmp_path):
        # A partial catalog is a smaller answer, never an error: a root that does not exist
        # is skipped, and a plain file named ``*.app`` is not a bundle.
        self._bundle(tmp_path, "Paintbrush")
        (tmp_path / "notanapp.txt").write_bytes(b"x")
        (tmp_path / "Fake.app").write_bytes(b"not a directory")
        launch_macos = self._roots(monkeypatch, tmp_path, tmp_path / "does-not-exist")
        assert [a.name for a in launch_macos.installed_apps()] == ["Paintbrush"]

    def test_an_earlier_root_wins_a_name_collision(self, monkeypatch, tmp_path):
        # ``_APP_ROOTS`` is ordered most-trusted first, so a system install must not be
        # displaced by a machine-wide one of the same name.
        first, second = tmp_path / "system", tmp_path / "local"
        self._bundle(first, "Notes")
        self._bundle(second, "Notes")
        launch_macos = self._roots(monkeypatch, first, second)
        apps = launch_macos.installed_apps()
        assert [a.name for a in apps] == ["Notes"]
        assert apps[0].source == str(first)

    def test_an_exact_name_resolves_to_its_VERIFIED_PATH(self, monkeypatch, tmp_path):
        # The path, not the name: ``open -a <name>`` would ask LaunchServices to resolve it
        # again from a database that indexes bundles this module excludes.
        bundle = self._bundle(tmp_path, "Paintbrush")
        launch_macos = self._roots(monkeypatch, tmp_path)
        assert launch_macos.resolve_target("paintbrush") == (str(bundle), "Paintbrush")

    def test_a_unique_PREFIX_resolves_and_a_substring_does_not(self, monkeypatch, tmp_path):
        # A short fragment matching inside a long name is a coincidence rather than an
        # intent, and the ambiguity guard cannot catch it — a coincidence usually hits
        # exactly one entry. So a substring supplies SUGGESTIONS only.
        self._bundle(tmp_path, "Paintbrush")
        launch_macos = self._roots(monkeypatch, tmp_path)
        assert launch_macos.resolve_target("paint")[1] == "Paintbrush"
        with pytest.raises(NoSuchLaunchTarget) as caught:
            launch_macos.resolve_target("brush")
        assert "Paintbrush" in caught.value.near

    def test_an_ambiguous_prefix_RAISES_rather_than_picking(self, monkeypatch, tmp_path):
        # Launching the wrong application is not undoable.
        self._bundle(tmp_path, "Notes")
        self._bundle(tmp_path, "Notability")
        launch_macos = self._roots(monkeypatch, tmp_path)
        with pytest.raises(AmbiguousLaunchTarget) as caught:
            launch_macos.resolve_target("not")
        assert caught.value.count == 2

    def test_an_exact_name_beats_an_ambiguous_prefix(self, monkeypatch, tmp_path):
        # The positive control: "refuse when several match" must not refuse the case where
        # one of them is exactly what was asked for.
        bundle = self._bundle(tmp_path, "Notes")
        self._bundle(tmp_path, "Notability")
        launch_macos = self._roots(monkeypatch, tmp_path)
        assert launch_macos.resolve_target("Notes") == (str(bundle), "Notes")

    def test_a_TERMINAL_is_refused_by_query_and_by_resolved_name(self, monkeypatch, tmp_path):
        # A shell takes its work from a subsequent keystroke, which is the one shape the
        # no-arguments rule cannot bound. Checked on the QUERY as well as the resolved
        # bundle: on Windows, asking for ``cmd`` resolved to an unrelated binary and
        # launched it, passing a resolved-name check that never saw a shell.
        self._bundle(tmp_path, "Terminal")
        launch_macos = self._roots(monkeypatch, tmp_path)
        for query in ("terminal", "Terminal"):
            with pytest.raises(ComputerUseError, match="terminal"):
                launch_macos.resolve_target(query)

    def test_an_empty_query_is_not_installed_rather_than_a_crash(self, monkeypatch, tmp_path):
        launch_macos = self._roots(monkeypatch, tmp_path)
        with pytest.raises(NoSuchLaunchTarget):
            launch_macos.resolve_target("   ")

    def test_a_bundle_that_fails_verification_is_REFUSED(self, monkeypatch, tmp_path):
        # The verification is what makes the catalog untrusted input rather than an
        # authority, so a resolved bundle must not be returned when it fails.
        from kiro_crew.computer_use import launch_macos

        self._bundle(tmp_path, "Paintbrush")
        monkeypatch.setattr(launch_macos, "_APP_ROOTS", (str(tmp_path),))
        monkeypatch.setattr(launch_macos, "_writable_component", lambda _app: True)
        with pytest.raises(ComputerUseError, match="this user can write"):
            launch_macos.resolve_target("Paintbrush")

    def test_the_write_probe_is_removed_and_reports_writable(self, tmp_path):
        # A leftover probe in a system directory would be litter with our name on it.
        from kiro_crew.computer_use import launch_macos

        assert launch_macos._directory_is_writable(str(tmp_path)) is True
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (PermissionError("denied"), False),
            (FileNotFoundError("gone"), False),
            (OSError("io"), True),
            (FileExistsError("collision"), True),
        ],
    )
    def test_the_probe_fails_CLOSED_on_anything_but_a_denial(
        self, monkeypatch, tmp_path, error, expected
    ):
        # "Assume writable" is the safe answer, because the caller then REFUSES. A name
        # collision cannot prove unwritability either, so it fails closed too.
        from kiro_crew.computer_use import launch_macos

        def boom(*_args, **_kwargs):
            raise error

        monkeypatch.setattr("builtins.open", boom)
        assert launch_macos._directory_is_writable(str(tmp_path)) is expected


class TestMacOSBundleVerification:
    """What a ``.app`` must satisfy before ``open -a`` is allowed to run it.

    Runs on every platform: ``launch_macos`` imports anywhere (the whole point of
    ``_OPEN_BIN``'s composed form), and the writability answers are INJECTED rather than
    measured — which is also what lets the interesting case be asserted at all, since no
    CI runner ships a bundle with a writable executable directory.
    """

    _APP = "/Applications/Foo.app"

    @staticmethod
    def _app():
        from kiro_crew.computer_use.launch_macos import InstalledApp

        return InstalledApp(name="Foo", path="/Applications/Foo.app", source="/Applications")

    @staticmethod
    def _no_writable_executable(monkeypatch):
        """Answer both REPLACE probes so a test can assert the CREATE half on its own.

        Create-permission and replace-permission are independent, and are tested
        independently. These directory tests use a synthetic ``/Applications/Foo.app`` —
        deliberately, since a real bundle's parent is not assertable — and both replace
        probes fail CLOSED on a path that does not exist, which is correct behaviour and
        would mask every directory answer. Their own coverage is the ``_REPLACEABLE`` tests,
        which build a real bundle and inject ownership.
        """
        from kiro_crew.computer_use import launch_macos

        monkeypatch.setattr(launch_macos, "_any_executable_is_writable", lambda _d: False)
        monkeypatch.setattr(launch_macos, "_file_is_replaceable", lambda _p: False)

    def test_a_writable_bundle_EXECUTABLE_directory_is_refused(self, monkeypatch):
        """The hole a parent-only probe left, and it is the COMMON case.

        ``/Applications`` is root-owned, so probing only the bundle's parent says nothing
        about who can rewrite the Mach-O inside an existing bundle. A bundle installed
        there by any user-space installer — every drag-install, every Homebrew cask — is
        owned by the installing user, so ``Foo.app/Contents/MacOS/Foo`` can be replaced
        without ``/Applications`` ever being writable. That bundle passed every check and
        launched agent-authored native code.
        """
        from kiro_crew.computer_use import launch_macos

        probed: list[str] = []

        def writable(directory: str) -> bool:
            probed.append(directory)
            return directory.endswith(os.path.join("Contents", "MacOS"))

        monkeypatch.setattr(launch_macos, "_directory_is_writable", writable)
        self._no_writable_executable(monkeypatch)
        assert launch_macos._writable_component(self._app()) is True
        # The parent WAS probed too — the fix adds layers, it does not move the check.
        assert "/Applications" in probed

    def test_an_unwritable_bundle_still_resolves(self, monkeypatch):
        # The positive control: without it the refusal above would also pass on an
        # implementation that refused every bundle.
        from kiro_crew.computer_use import launch_macos

        monkeypatch.setattr(launch_macos, "_directory_is_writable", lambda _d: False)
        self._no_writable_executable(monkeypatch)
        assert launch_macos._writable_component(self._app()) is False

    def test_the_probed_directories_are_FIXED_not_read_from_the_bundle(self, monkeypatch):
        """The bundle must not get to say which directory is judged.

        ``CFBundleExecutable`` lives inside the very directory whose trustworthiness is in
        question, so honouring it would let a crafted value aim the probe outside the
        bundle — ``/tmp/x`` probes ``/tmp``, ``../../..`` walks up out of it — and the
        target would choose its own examiner. The three locations are therefore fixed, and
        every one of them is under the bundle or its parent.
        """
        from kiro_crew.computer_use import launch_macos

        probed: list[str] = []
        monkeypatch.setattr(
            launch_macos, "_directory_is_writable", lambda d: probed.append(d) or False
        )
        self._no_writable_executable(monkeypatch)
        # A hostile plist, which must change nothing: the reader is not consulted here.
        monkeypatch.setattr(
            "kiro_crew.computer_use.apps_macos.read_bundle_plist",
            lambda _b: {"CFBundleExecutable": "/tmp/evil"},
        )
        launch_macos._writable_component(self._app())
        assert probed == [
            "/Applications",
            self._APP,
            os.path.join(self._APP, "Contents"),
            os.path.join(self._APP, "Contents", "MacOS"),
        ]

    @pytest.mark.parametrize(
        "writable",
        ["/Applications", _APP, "Contents", os.path.join("Contents", "MacOS")],
    )
    def test_a_writable_directory_at_ANY_level_refuses(self, monkeypatch, writable):
        """Write access to a SINGLE directory on the path is enough to control what runs.

        So each one has to refuse on its own. ``Contents`` is the one an endpoints-only
        check missed, and it is not "deeper nesting": owning it is enough to
        ``mv Contents/MacOS Contents/MacOS.bak && mkdir Contents/MacOS`` and end up with an
        agent-owned executable directory that every other probe then calls unwritable. It
        also holds the ``Info.plist``, so the same access rewrites ``CFBundleIdentifier``
        and defeats the pre-spawn identity deny as well.
        """
        from kiro_crew.computer_use import launch_macos

        monkeypatch.setattr(launch_macos, "_directory_is_writable", lambda d: d.endswith(writable))
        self._no_writable_executable(monkeypatch)
        assert launch_macos._writable_component(self._app()) is True

    def test_a_REWRITABLE_executable_is_refused_though_no_directory_permits_creates(
        self, monkeypatch, tmp_path
    ):
        """Create-permission and replace-permission are different, so both are tested.

        Directory writability governs create, unlink and rename; rewriting an existing
        file's bytes needs write on the file inode and no directory permission at all. So a
        create-probe answers "unwritable" for the ordinary drag-install and Homebrew-cask
        shape — root-owned directories that deny creates, holding an executable owned by
        the installing user — while ``open(exe, "r+b")`` replaces the binary in place.
        Verified against that revision: all four directory probes answered False and the
        launch was allowed.

        Real files with real modes rather than an injected answer, because the whole defect
        was that the injected question was the wrong one.
        """
        from kiro_crew.computer_use import launch_macos

        bundle = tmp_path / "Foo.app"
        macos = bundle / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        (macos / "Foo").write_bytes(b"ORIGINAL-SIGNED-MACHO")
        app = launch_macos.InstalledApp(name="Foo", path=str(bundle), source=str(tmp_path))
        # Every directory denies creates; only the executable's own mode permits writing.
        monkeypatch.setattr(launch_macos, "_directory_is_writable", lambda _d: False)
        assert os.access(str(macos / "Foo"), os.W_OK), "fixture precondition"
        assert launch_macos._writable_component(app) is True

    @staticmethod
    def _bundle_on_disk(tmp_path):
        """A real ``Foo.app`` with an executable AND an ``Info.plist``."""
        from kiro_crew.computer_use.launch_macos import InstalledApp

        contents = tmp_path / "Foo.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "MacOS" / "Foo").write_bytes(b"MACHO")
        (contents / "Info.plist").write_bytes(b"<plist/>")
        return InstalledApp(name="Foo", path=str(tmp_path / "Foo.app"), source=str(tmp_path))

    @staticmethod
    def _owned(monkeypatch, mine):
        """Report every file as root-owned except those whose path ends with *mine*.

        Injected rather than staged with a real ``chown``: changing a file's owner needs
        privilege, and the test must give the same answer whether or not the suite has it.
        ``st_mode`` is preserved from the real ``stat`` because ``os.path.isdir`` consults it
        on the same paths — a bare object carrying only ``st_uid`` made the walk raise, which
        is how the first version of these tests broke on Linux.

        Directory creates and the mode bit are both answered "no", so ownership is the only
        signal left and each assertion is attributable to it.
        """
        from kiro_crew.computer_use import launch_macos

        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            uid = 501 if (mine and str(path).endswith(mine)) else 0
            return os.stat_result(
                (
                    info.st_mode,
                    info.st_ino,
                    info.st_dev,
                    info.st_nlink,
                    uid,
                    info.st_gid,
                    info.st_size,
                    int(info.st_atime),
                    int(info.st_mtime),
                    int(info.st_ctime),
                )
            )

        monkeypatch.setattr(launch_macos, "_directory_is_writable", lambda _d: False)
        monkeypatch.setattr(launch_macos.os, "geteuid", lambda: 501, raising=False)
        monkeypatch.setattr(launch_macos.os, "access", lambda *_a, **_k: False)
        monkeypatch.setattr(launch_macos.os, "stat", fake_stat)
        return launch_macos

    def test_a_READ_ONLY_mode_on_a_file_this_user_OWNS_is_not_trust(self, monkeypatch, tmp_path):
        """Mode is not durable; ownership is.

        The owner of a file may ``chmod`` it at will, with no privilege, so a read-only mode
        on a file this user owns is a fact the same user undoes between the check and the
        ``exec``: replace the binary, ``chmod a-w``, launch. ``st_uid`` cannot be changed that
        way — ``chown`` to another user requires privilege — so the durable question is "could
        this user have written it", not "can they right now".
        """
        app = self._bundle_on_disk(tmp_path)
        launch_macos = self._owned(monkeypatch, os.path.join("MacOS", "Foo"))
        assert launch_macos._writable_component(app) is True

    def test_a_REPLACEABLE_Info_plist_is_refused(self, monkeypatch, tmp_path):
        """The plist is a launch INPUT, not description, so its writability is load-bearing.

        An unwritable ``Contents`` still holds a plist this user can rewrite — the same
        create-versus-replace distinction one file over — and that file supplies the
        ``CFBundleIdentifier`` ``target_identity`` hands the policy. A forged id passes the
        pre-spawn deny under a name the operator never blocked, and the post-launch check
        re-reads the same forged string, so neither one catches it.
        """
        app = self._bundle_on_disk(tmp_path)
        launch_macos = self._owned(monkeypatch, "Info.plist")
        assert launch_macos._writable_component(app) is True

    def test_a_bundle_owned_ENTIRELY_by_someone_else_resolves(self, monkeypatch, tmp_path):
        # The positive control for both rules above: a bundle whose files all belong to root
        # and deny writing is what a real system install looks like, and it must still launch
        # — otherwise the two refusals would be unconditional.
        app = self._bundle_on_disk(tmp_path)
        launch_macos = self._owned(monkeypatch, "")  # nothing owned by this user
        assert launch_macos._writable_component(app) is False

    def test_a_MISSING_executable_directory_fails_CLOSED(self, monkeypatch, tmp_path):
        # An unverifiable bundle is refused, never admitted. A bundle with no
        # ``Contents/MacOS`` is not one ``open -a`` could run either, so refusing costs
        # nothing and the alternative is a branch that silently skips the check.
        from kiro_crew.computer_use import launch_macos

        bundle = tmp_path / "Foo.app"
        bundle.mkdir()
        app = launch_macos.InstalledApp(name="Foo", path=str(bundle), source=str(tmp_path))
        monkeypatch.setattr(launch_macos, "_directory_is_writable", lambda _d: False)
        assert launch_macos._writable_component(app) is True

    def test_the_bundle_id_is_read_through_the_HARDENED_reader(self):
        # ``target_identity`` DOES read the plist, for ``CFBundleIdentifier``. Not with a
        # bare ``open``: the bundle is agent-choosable, so the read must keep
        # ``apps_macos``' realpath + sensitive-path re-check + ``O_NOFOLLOW`` path. A
        # second reader here would be a second chance to lose those three.
        from kiro_crew.computer_use import launch_macos

        source = inspect.getsource(launch_macos)
        assert "bundle_identity_at" in source
        assert "plistlib" not in source, "launch_macos must not parse a plist itself"


class TestResolvedIdentityReachesThePolicy:
    """The platform half of the pre-spawn check: what ``permit`` is actually handed.

    Split from :class:`TestLaunchDispatch` because the fake supplies its own identity, so
    every test that goes through it passes even when a real driver forwards none. These
    drive ``backend.run_launch`` and each ``target_identity`` directly, which is the only
    place the wiring is observable.
    """

    @staticmethod
    def _run(identity, *, denied="", found=None):
        """``run_launch`` with everything injected. Returns ``(result, spawned, seen)``.

        *found* is what ``find`` answers AFTER the spawn (it always answers ``None`` before,
        so the already-running branch is not taken) — the hook for asserting the check on
        the identity the OS publishes once a window exists.
        """
        from kiro_crew.computer_use import backend as be
        from kiro_crew.computer_use.policy import PolicyConfig, check_app

        spawned: list[str] = []
        seen: list[LaunchIdentity] = []
        cfg = PolicyConfig(extra_denied_apps=(denied,) if denied else ())

        def permit(who: LaunchIdentity) -> "str | None":
            seen.append(who)
            return check_app(who.as_app_ref(), cfg)

        def refuse_launched(ref: AppRef) -> "str | None":
            seen.append(LaunchIdentity(display=ref.name, key=ref.bundle_id))
            return check_app(ref, cfg)

        result = be.run_launch(
            "foo",
            resolve=lambda _q: ("/opt/foo/foo.bin", "Foo"),
            find=lambda _n: found if spawned else None,
            spawn=spawned.append,
            permit=permit,
            identity=identity,
            refuse_launched=refuse_launched,
            window_timeout=0.02,
            window_poll_interval=0.005,
        )
        return result, spawned, seen

    def test_the_PUBLISHED_identity_is_checked_once_a_window_exists(self):
        """The check the pre-spawn one structurally cannot make.

        A packaged app's window is fronted by ``ApplicationFrameHost``, so
        ``apps_windows`` publishes the WINDOW TITLE as both name and bundle id — the
        broker's image name identifies no application. That title is the only spelling an
        operator can write a rule against, and it does not exist until a window does: before
        the spawn the catalog offers ``store.exe`` and nothing else. Measured on a real host,
        ``extra_denied_apps: ["Microsoft Store"]`` matched neither pre-spawn identity and the
        launch reported success.

        This cannot stop the process starting, and does not claim to. What it stops is the
        launch REPORTING success, so nothing downstream snapshots or drives the window.
        """
        # A HOSTED window, which is the shape this covers: ``_app_ref`` puts the title into
        # ``name`` and ``bundle_id``, and those are the two fields an operator pattern is
        # matched against. An app that reports its OWN executable keeps the title in
        # ``window_title`` alone, which no operator pattern reads — a separate, pre-existing
        # limitation of ``policy._matches_operator_pattern`` and not something this check can
        # reach.
        published = AppRef(
            name="Microsoft Store",
            pid=4242,
            bundle_id="Microsoft Store",
            window_title="Microsoft Store",
        )
        result, spawned, seen = self._run(
            lambda target, display: LaunchIdentity(display=display, key="store.exe"),
            denied="Microsoft Store",
            found=published,
        )
        assert result.ok is False, "a denied packaged app was reported as launched"
        assert result.app is None, "the refusal still handed back a drivable window"
        # The process DID start — that is the stated residual, not a silent one.
        assert spawned == ["/opt/foo/foo.bin"]
        # And the pre-spawn identity really did MISS, which is the whole reason this check
        # exists. Asserted rather than assumed: if a future resolver learned the title, the
        # test would otherwise keep passing while covering nothing.
        pre = seen[0].as_app_ref()
        assert (pre.name, pre.bundle_id) == ("Foo", "store.exe")
        assert (
            policy.check_app(pre, policy.PolicyConfig(extra_denied_apps=("Microsoft Store",)))
            is None
        ), "the pre-spawn identity already matched, so this test proves nothing"

    def test_an_allowed_app_is_NOT_refused_after_its_window_appears(self):
        # The positive control for the check above: without it, the third check would
        # refuse every launch and the two assertions above would still pass.
        published = AppRef(name="Foo", pid=7, bundle_id="foo.bin", window_title="Foo — Untitled")
        result, spawned, _seen = self._run(
            lambda target, display: LaunchIdentity(display=display, key="foo.bin"),
            found=published,
        )
        assert result.ok is True
        assert result.app is published
        assert spawned == ["/opt/foo/foo.bin"]

    def test_the_identity_supplier_is_what_the_policy_SEES(self):
        # ``run_launch``'s half of the contract: whatever the platform supplies is what
        # reaches ``permit``. The DRIVERS' half is pinned separately below — this one
        # injects the supplier, so it cannot see a driver that forgets to pass one.
        _result, _spawned, seen = self._run(
            lambda target, display: LaunchIdentity(display=display, key="foo.bin")
        )
        assert [(w.display, w.key) for w in seen] == [("Foo", "foo.bin")]

    @pytest.mark.parametrize("driver_mod", ["windows_driver", "macos_driver"])
    def test_EVERY_driver_forwards_its_platform_identity_supplier(self, driver_mod):
        """A driver that omits ``identity=`` restores the vulnerability, silently.

        ``run_launch``'s ``identity`` parameter defaults to ``None`` and that default
        degrades to the pre-fix display-name-only check, so the omission is invisible: with
        the argument deleted from BOTH drivers the entire launch suite stayed green, because
        every dispatch-level test runs through the fake, which supplies its own identity.

        Asserted against the source rather than by calling the driver, because constructing
        a real one needs the platform's native UI-Automation/AX stack — which is exactly the
        reason the gap existed. Both drivers are checked on every OS for the same reason.
        """
        import importlib

        module = importlib.import_module(f"kiro_crew.computer_use.{driver_mod}")
        source = inspect.getsource(module)
        launcher = "launch_windows" if driver_mod == "windows_driver" else "launch_macos"
        assert f"identity={launcher}.target_identity," in source, (
            f"{driver_mod} does not forward its identity supplier to run_launch, so the "
            "pre-spawn policy check sees only the display name"
        )

    def test_a_deny_on_the_OS_IDENTITY_stops_the_SPAWN(self):
        # The point of the whole mechanism: refused, and no process created.
        result, spawned, _seen = self._run(
            lambda target, display: LaunchIdentity(display=display, key="foo.bin"),
            denied="foo.bin",
        )
        assert result.ok is False
        assert spawned == [], "the denied target was spawned anyway"

    def test_NO_supplier_degrades_to_the_display_name_only(self):
        # The default is the pre-fix behaviour, which is exactly why a driver that forgets
        # to pass ``identity=`` must be caught by the test above rather than by this one.
        _result, _spawned, seen = self._run(None)
        assert [(w.display, w.key) for w in seen] == [("Foo", "")]
        assert seen[0].as_app_ref().bundle_id == "Foo"

    @pytest.mark.skipif(not IS_WINDOWS, reason="launch_windows is the Windows resolver")
    def test_the_windows_key_is_the_RESOLVED_basename(self, tmp_path):
        """An 8.3 alias must not be able to rename the target out of a deny rule.

        Short names exist by default on the system volume, so an ``App Paths`` value (the
        hive is agent-writable) can name ``…\\SOMEVE~1.EXE`` for a file whose real name is
        ``SomeVeryLongName.exe``. ``resolve_target`` accepts it — it compares
        ``basename(realpath(...))`` against the catalog key — so reporting the RAW basename
        handed the policy a string the operator's ``someverylongname.exe`` rule cannot
        match, and the denied application spawned. Verified against that revision.
        """
        from kiro_crew.computer_use import launch_windows

        real = tmp_path / "SomeVeryLongName.exe"
        real.write_bytes(b"MZ")
        short = tmp_path / "SOMEVE~1.EXE"
        if not short.is_file():
            pytest.skip("8.3 short names are disabled on this volume")
        who = launch_windows.target_identity(str(short), "SomeVeryLongName")
        assert who.key == "SomeVeryLongName.exe"

    def test_the_macos_key_is_the_BUNDLE_ID(self, monkeypatch):
        # The macOS spelling that matters: the built-in denylist is bundle PREFIXES and
        # the operator's rules are written the way refusals print them.
        from kiro_crew.computer_use import launch_macos

        monkeypatch.setattr(
            "kiro_crew.computer_use.apps_macos.bundle_identity_at",
            lambda _b: ("com.example.Foo", "Foo"),
        )
        who = launch_macos.target_identity("/Applications/Foo.app", "Foo")
        assert (who.display, who.key) == ("Foo", "com.example.Foo")


class TestLaunchDispatch:
    """The chokepoint, through the shipped fake — so this runs on every platform."""

    def test_launching_returns_the_new_window_tree(self, fake_computer_backend):
        # The launch's own snapshot is what turns "it opened" into "here is what you
        # can click": a fresh window has no cached indices, so without it the model's
        # only possible next call is get_state on the app it just launched.
        out = _launch("Fake Draw")
        assert not out.startswith(ERROR_PREFIX)
        assert "launched Fake Draw" in out
        assert "Refreshed state:" in out
        assert [name for name, _kw in fake_computer_backend.calls] == [
            "launch_app",
            "snapshot",
        ]

    def test_an_already_running_app_is_refused_rather_than_relaunched(self, fake_computer_backend):
        # A second copy of an editor is a second unsaved document, and the model's
        # actual goal (a window to drive) is already met.
        out = _launch(FAKE_FILES_APP.name)
        assert out.startswith(ERROR_PREFIX)
        assert TOOL_GET_STATE in out

    def test_a_process_with_no_window_yet_is_a_SUCCESS(self, fake_computer_backend):
        # The branch that stops a model launching twice. Reporting failure for "the
        # process started but is still loading" is what makes the second attempt
        # happen, and the second attempt is what produces two copies.
        fake_computer_backend.launched_with_window = False
        out = _launch("Fake Draw")
        assert not out.startswith(ERROR_PREFIX)
        assert "do NOT launch it again" in out
        # No snapshot: there is no window to walk.
        assert [name for name, _kw in fake_computer_backend.calls] == ["launch_app"]

    def test_an_uninstalled_app_names_the_rule_not_a_path(self, fake_computer_backend):
        # The refusal has to teach the rule, because "try a path" is the one retry
        # that can never work.
        out = _launch("Nothing Installed")
        assert out.startswith(ERROR_PREFIX)
        assert "filesystem path" in out

    def test_the_self_target_denylist_blocks_a_launch_BEFORE_the_driver(
        self, fake_computer_backend
    ):
        # THE launch-specific security assertion. Every other verb resolves an
        # ``AppRef`` from the window list first, so the denylist sees a real identity;
        # a launch has only the name typed. Kiro Crew's own rule matches on name
        # substrings, so it fires — and it must fire before a process exists, which
        # is what the empty journal proves.
        out = _launch("Kiro Crew")
        assert out.startswith(ERROR_PREFIX)
        assert fake_computer_backend.calls == []

    def test_a_denied_BUNDLE_ID_is_refused_before_the_process_exists(self, fake_computer_backend):
        """A rule that only the resolved identity can match must still gate the spawn.

        The dispatcher's own pre-check sees only the name the caller typed, so an app
        whose OS-reported identity is denied while its display name is innocuous passes
        it. That identity is knowable BEFORE the spawn — the resolver produced it — so
        the refusal belongs there rather than after the fact: a detached spawn cannot be
        undone, and refusing afterwards only stops Kiro Crew driving a process it has
        already started.

        ``launch_app`` appearing alone in the journal, with no ``snapshot`` after it, is
        what distinguishes the two: the driver was reached (that is where the resolved
        identity exists) and nothing was driven.
        """
        fake_computer_backend.launchable = (
            AppRef(
                name="Innocuous",
                pid=4109,
                bundle_id="dev.kiro.crew.dashboard",
                window_id=8809,
                window_title="Dashboard",
            ),
        )
        out = _launch("Innocuous")
        assert out.startswith(ERROR_PREFIX)
        assert "blocked target" in out
        assert [name for name, _kw in fake_computer_backend.calls] == ["launch_app"]
        assert fake_computer_backend.launchable[0] not in fake_computer_backend.apps

    def test_the_operators_deny_rule_matches_the_OS_IDENTITY_not_just_the_name(
        self, fake_computer_backend, tmp_path
    ):
        """An operator's deny entry is written the way the OS names the app.

        Every other computer-use refusal prints the OS identity — ``notepad.exe``,
        ``com.apple.TextEdit`` — so that is the spelling an operator copies into
        ``extra_denied_apps``. A pre-spawn check that knew only the DISPLAY name
        (``notepad``) matched neither that nor a bundle-id rule, so the denied app
        started and was refused only once it was running. Verified against that
        revision: with ``extra_denied_apps: ["dev.kirocrew.fake.draw"]`` the launch
        succeeded.

        The empty ``apps`` list is the assertion that matters: the fake moves a
        successfully launched app into ``apps``, so its absence proves the refusal
        preceded the spawn rather than following it.
        """
        (tmp_path / "computer_use.json").write_text(
            json.dumps({"enabled": True, "extra_denied_apps": ["dev.kirocrew.fake.draw"]}),
            encoding="utf-8",
        )
        out = _launch("Fake Draw")
        assert out.startswith(ERROR_PREFIX)
        assert "blocked list by the operator" in out
        assert FAKE_DRAW_APP not in fake_computer_backend.apps

    def test_the_resolved_check_does_not_turn_an_ALLOW_list_into_a_refusal(self):
        """The union must not make an allow-list refuse the app it names.

        The other direction of the same fix, and the reason the two identities are
        checked as ONE ``AppRef`` rather than once per name: ``check_app`` refuses a name
        absent from a non-empty ``allowed_apps``, so refusing on any individual miss
        would mean an operator who allow-listed the display name was defeated by the
        bundle id failing that very list — and vice versa. Asserted here rather than
        through ``_launch`` because the dispatcher's earlier name-only check has its own
        (pre-existing, fail-closed) behaviour on an allow-list written in a spelling the
        caller did not type; this pins the resolved check alone.
        """
        who = LaunchIdentity(display="Fake Draw", key="dev.kirocrew.fake.draw")
        for spelling in ("fake draw", "dev.kirocrew.fake.draw"):
            cfg = policy.PolicyConfig(allowed_apps=(spelling,))
            assert tools._launch_refusal(who, cfg) is None, f"allowed_apps={spelling!r} refused"
        # The positive control: an allow-list naming a DIFFERENT app still refuses, so
        # the two assertions above cannot pass on an implementation that allows anything.
        narrow = policy.PolicyConfig(allowed_apps=("something else",))
        assert tools._launch_refusal(who, narrow) is not None

    def test_the_operators_DENY_list_gates_the_launch(self, fake_computer_backend, tmp_path):
        """The operator's own lists must gate the one verb that starts a process.

        An earlier draft called ``policy.denied_rule_for`` here — the built-in floor
        ALONE — which silently exempted ``extra_denied_apps`` and ``allowed_apps`` from
        `computer_launch_app`. Verified against that draft: with
        ``extra_denied_apps: ["fake draw"]`` the floor answered ``None`` and the app
        launched. The post-launch re-check cannot help, because the spawn is detached:
        refusing afterwards does not un-launch a process.

        The empty driver journal is the assertion that matters — it proves the refusal
        happened BEFORE anything ran, not after.
        """
        (tmp_path / "computer_use.json").write_text(
            json.dumps({"enabled": True, "extra_denied_apps": ["fake draw"]}), encoding="utf-8"
        )
        out = _launch("Fake Draw")
        assert out.startswith(ERROR_PREFIX)
        assert "blocked list by the operator" in out
        assert fake_computer_backend.calls == [], "the app was launched before being refused"

    def test_the_operators_ALLOW_list_gates_the_launch(self, fake_computer_backend, tmp_path):
        # The other half: an allow-list is a narrowing, and a verb that ignored it would
        # let the agent start anything while every other verb stayed bounded.
        (tmp_path / "computer_use.json").write_text(
            json.dumps({"enabled": True, "allowed_apps": ["something else"]}), encoding="utf-8"
        )
        out = _launch("Fake Draw")
        assert out.startswith(ERROR_PREFIX)
        assert "allowed-apps list" in out
        assert fake_computer_backend.calls == []

    def test_an_allowed_app_still_launches(self, fake_computer_backend, tmp_path):
        # The positive control: without it the two refusals above would also pass on an
        # implementation that refused every launch.
        (tmp_path / "computer_use.json").write_text(
            json.dumps({"enabled": True, "allowed_apps": ["fake draw"]}), encoding="utf-8"
        )
        out = _launch("Fake Draw")
        assert not out.startswith(ERROR_PREFIX), out
        assert "launched Fake Draw" in out

    def test_the_launch_is_audited_with_the_RESOLVED_identity(
        self, fake_computer_backend, monkeypatch
    ):
        """The SEL row for the one process-creating verb must name what was started.

        The upstream ``_audit_allowed`` runs before the launch, when there is no target
        yet, so it records an empty ``resources`` field. "A launch happened, of
        something" is not the record an operator needs, so the branch re-audits with the
        identity the OS reported.
        """
        rows: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kwargs):
                rows.append(kwargs)

        monkeypatch.setattr(tools, "sel", lambda: _Sel())
        out = _launch("Fake Draw")
        assert not out.startswith(ERROR_PREFIX), out
        launches = [r for r in rows if r.get("tool_name") == TOOL_LAUNCH_APP]
        assert launches, rows
        # ``AppRef.label`` — the bundle id plus the pid, which is what every other
        # verb's audit row carries, so the launch row is readable beside them.
        assert any(
            FAKE_DRAW_APP.label == str(row.get("resources") or "") for row in launches
        ), launches

    def test_the_tool_is_in_the_mutating_set_not_the_read_only_one(self):
        # A verb that starts a process is the largest change this tool set can make,
        # so classifying it as read-only would put it on the same footing as reading
        # a tree.
        from kiro_crew.computer_use.types import MUTATING_TOOLS, READ_ONLY_TOOLS

        assert TOOL_LAUNCH_APP in MUTATING_TOOLS
        assert TOOL_LAUNCH_APP not in READ_ONLY_TOOLS

    def test_the_schema_accepts_no_path_or_argument_field(self):
        # Enforced as a SHAPE rather than trusted from the prose: a later edit adding
        # an ``args`` or ``path`` field would silently widen the verb from "open an
        # application" to "run a program with input", and nothing else in the suite
        # would notice.
        from kiro_crew.validation import MCP_COMPUTER_SCHEMAS

        fields = {spec.name for spec in MCP_COMPUTER_SCHEMAS[TOOL_LAUNCH_APP].fields}
        assert fields == {"app"}

    def test_the_advertised_schema_matches_the_validator(self):
        # A tool whose advertised ``required`` list is looser than the validator's
        # teaches the model a call shape that is always refused.
        from kiro_crew.mcp_computer import _tool_definitions

        entry = next(d for d in _tool_definitions() if d["name"] == TOOL_LAUNCH_APP)
        assert entry["inputSchema"]["required"] == ["app"]
        assert set(entry["inputSchema"]["properties"]) == {"app"}


@pytest.mark.skipif(not IS_WINDOWS, reason="asserts the real Windows host catalog")
class TestWindowsHostCatalog:
    """The invariant against the host's REAL catalog.

    The fabricated-catalog tests above prove the rules are implemented; this proves
    they are SATISFIABLE here — that a real installed application actually resolves.
    A resolver that refused everything would pass every test above.
    """

    def test_at_least_one_real_installed_app_resolves(self):
        """Satisfiable on an ORDINARY user's host, which is who this verb is for.

        Skipped when the suite runs elevated, and that is the honest reading rather than a
        convenience: an administrator genuinely can rewrite ``System32`` and genuinely does
        own content under ``Program Files``, so refusing every target is the CORRECT answer
        for that process — the trust question is "could this user have planted it", and for
        an admin the answer is yes everywhere. CI's ``windows-latest`` runner is elevated, so
        without this guard the assertion measures the runner's privilege rather than the
        resolver. The rules themselves are covered on every platform by
        :class:`TestLaunchResolutionTrust`; what this adds is that they are satisfiable on a
        real unelevated desktop.
        """
        from kiro_crew.computer_use import launch_windows

        catalog = launch_windows.installed_apps()
        assert catalog, "the host reported no installed applications at all"
        resolved = []
        for app in catalog:
            try:
                resolved.append(launch_windows.resolve_target(app.name))
            except ComputerUseError:
                continue
        if not resolved and _running_elevated():
            pytest.skip("elevated process: every install root is writable, so refusing is correct")
        assert resolved, "no entry in the host's own catalog survived resolution"

    def test_every_resolvable_entry_lives_under_a_protected_root(self):
        # The rule restated over real data: whatever resolves must be somewhere this
        # user cannot write. A failure here means the protected-root list is missing
        # a root that real applications use, which would be a genuine finding rather
        # than a test to relax.
        from kiro_crew.computer_use import launch_windows

        for app in launch_windows.installed_apps():
            try:
                executable, _name = launch_windows.resolve_target(app.name)
            except ComputerUseError:
                continue
            assert launch_windows._under_protected(executable), executable

    def test_the_local_windowsapps_alias_dir_is_never_a_launch_source(self):
        # Measured: %LOCALAPPDATA%\Microsoft\WindowsApps is ON PATH and writable, and
        # it is what shutil.which("mspaint") returns. Nothing that resolves may come
        # from there — that is the specific hole the protected-root rule closes.
        from kiro_crew.computer_use import launch_windows

        alias_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps")
        if not alias_dir or not os.path.isdir(alias_dir):
            pytest.skip("no execution-alias directory on this host")
        folded = os.path.realpath(alias_dir).casefold()
        for app in launch_windows.installed_apps():
            try:
                executable, _name = launch_windows.resolve_target(app.name)
            except ComputerUseError:
                continue
            assert not os.path.realpath(executable).casefold().startswith(folded)


def test_the_already_running_refusal_names_get_state():
    # A refusal a model cannot act on costs it a turn and teaches it nothing, and the
    # useful move here is specifically get_state rather than a retry.
    assert TOOL_GET_STATE in ERR_LAUNCH_ALREADY_RUNNING.format(
        app="A", title="T", tool=TOOL_GET_STATE
    )


def test_the_denylist_probe_shape_reaches_the_self_target_rule():
    # ``tools`` synthesizes an ``AppRef`` from the requested NAME because no window
    # exists yet. That is only sound if the self-target rule can actually fire on a
    # name-only ref — pinned here so a future denylist change that dropped
    # ``name_substrings`` would fail loudly rather than silently open the launch path
    # to Kiro Crew's own dashboard.
    probe = AppRef(name="Kiro Crew", pid=0, bundle_id="Kiro Crew", window_title="Kiro Crew")
    assert policy.denied_rule_for(probe) is not None


def test_launch_windows_imports_no_platform_module_at_MODULE_SCOPE():
    """``winreg`` does not exist off Windows, so importing it at module scope would
    break EVERY test that transitively touches ``kiro_crew`` on the Linux CI fleet.

    Asserted by AST rather than by importing, because that is the only form that
    fails on a Windows dev box: an ``import winreg`` at module scope succeeds here and
    would only go red on the shard nobody runs locally. The same reasoning
    ``test_computer_use_unsupported.py::test_no_module_scope_native_library_load``
    gives for a module-scope ``CDLL``.
    """
    import ast
    import pathlib

    from kiro_crew.computer_use import launch_windows

    source = pathlib.Path(launch_windows.__file__).read_text(encoding="utf-8")
    module_scope: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            module_scope.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_scope.add(node.module.split(".")[0])
    # The stdlib modules that exist everywhere, plus this package.
    assert "winreg" not in module_scope
    assert module_scope <= {
        "__future__",
        "dataclasses",
        "logging",
        "os",
        "subprocess",
        "typing",
        "kiro_crew",
    }


def test_the_fake_launch_catalog_is_disjoint_from_the_running_list():
    # The fake's three launch outcomes are only distinguishable while these two lists
    # disagree; a fixture edit that put the draw app in both would make the
    # successful-launch test unable to fail.
    from kiro_crew.testing.fake_computer_use import FAKE_APPS, FAKE_LAUNCHABLE

    assert FAKE_DRAW_APP in FAKE_LAUNCHABLE
    assert FAKE_DRAW_APP not in FAKE_APPS
