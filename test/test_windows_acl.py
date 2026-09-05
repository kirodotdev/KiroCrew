"""Windows provider-CLI trust policy.

Two layers are tested separately, on purpose:

* the **policy** in ``github_runner.check_provider_path_component_windows``,
  driven by synthetic :class:`ComponentSecurity` values so it runs on every
  runner including the Ubuntu one that gates CI, and
* the **ACL read** in ``windows_acl.describe``, which needs a real security
  descriptor and is therefore Windows-only.

The fixtures are synthetic rather than "whatever `gh` install the runner
happens to have", so the suite does not silently change meaning with host
state.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import github_runner as runner
from kiro_crew import platform_compat, windows_acl

ME = "S-1-5-21-1-2-3-1001"
SYSTEM = "S-1-5-18"
ADMINS = "S-1-5-32-544"
EVERYONE = "S-1-1-0"
AUTHENTICATED_USERS = "S-1-5-11"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs a Windows ACL")


def _security(
    *,
    owner: str = SYSTEM,
    writers: tuple[windows_acl.Writer, ...] = (),
    null_dacl: bool = False,
    unparsable: tuple[int, ...] = (),
    volume_is_local: bool = True,
) -> windows_acl.ComponentSecurity:
    return windows_acl.ComponentSecurity(
        owner_sid=owner,
        owner_name="test-principal",
        null_dacl=null_dacl,
        writers=writers,
        unparsable_ace_types=unparsable,
        volume_is_local=volume_is_local,
    )


def _writer(sid: str, *rights: str) -> windows_acl.Writer:
    return windows_acl.Writer(sid=sid, name=f"name-of-{sid}", rights=rights or ("DELETE",))


@pytest.fixture
def described(monkeypatch):
    """Feed ``check_provider_path_component_windows`` a synthetic descriptor."""

    def _install(security: windows_acl.ComponentSecurity):
        monkeypatch.setattr(windows_acl, "describe", lambda path: security)

    return _install


# ── the substitution-right table ─────────────────────────────────────────────
#
# The regression that motivates this module: C:\ grants Authenticated Users the
# ADD_SUBDIRECTORY right by default, so reading bit 0x4 as a write grant on a
# directory refuses every stock Windows install once the walk reaches the root.


class TestSubstitutionRights:
    def test_add_subdirectory_is_not_a_substitution_right_on_a_directory(self) -> None:
        assert windows_acl._substitution_rights(0x00000004, is_dir=True) == ()

    def test_add_file_is_not_a_substitution_right_on_a_directory(self) -> None:
        assert windows_acl._substitution_rights(0x00000002, is_dir=True) == ()

    def test_the_same_bits_are_substitution_rights_on_a_file(self) -> None:
        assert windows_acl._substitution_rights(0x00000002, is_dir=False) == ("WRITE_DATA",)
        assert windows_acl._substitution_rights(0x00000004, is_dir=False) == ("APPEND_DATA",)

    def test_delete_child_is_a_substitution_right_on_a_directory(self) -> None:
        assert windows_acl._substitution_rights(0x00000040, is_dir=True) == ("FILE_DELETE_CHILD",)

    def test_taking_the_dacl_or_the_owner_counts_on_both(self) -> None:
        for is_dir in (True, False):
            assert "WRITE_DAC" in windows_acl._substitution_rights(0x00040000, is_dir=is_dir)
            assert "WRITE_OWNER" in windows_acl._substitution_rights(0x00080000, is_dir=is_dir)

    def test_cosmetic_write_rights_are_not_substitution_rights(self) -> None:
        """WRITE_EA / WRITE_ATTRIBUTES cannot change what the binary does."""
        assert windows_acl._substitution_rights(0x00000010, is_dir=False) == ()
        assert windows_acl._substitution_rights(0x00000100, is_dir=False) == ()


# ── the policy ───────────────────────────────────────────────────────────────


class TestRelaxedPolicy:
    def test_a_system_owned_component_with_only_trusted_writers_passes(self, described) -> None:
        described(_security(owner=SYSTEM, writers=(_writer(SYSTEM), _writer(ADMINS))))
        runner.check_provider_path_component_windows(
            Path("C:/anything"), label="executable", me_sid=ME, strict=False
        )

    def test_a_component_the_gateway_user_owns_passes(self, described) -> None:
        """The Windows analog of accepting a stock ``brew install gh``."""
        described(_security(owner=ME, writers=(_writer(ME),)))
        runner.check_provider_path_component_windows(
            Path("C:/anything"), label="executable", me_sid=ME, strict=False
        )

    def test_authenticated_users_holding_only_add_subdir_passes(self, described) -> None:
        """The stock C:\\ ACL must not refuse the walk.

        ``describe`` filters that ACE out because ADD_SUBDIRECTORY is not a
        substitution right, so the policy sees no writer at all.
        """
        described(_security(owner=SYSTEM, writers=(_writer(SYSTEM),)))
        runner.check_provider_path_component_windows(
            Path("C:/"), label="executable parent", me_sid=ME, strict=False
        )

    def test_a_third_account_owning_it_is_refused(self, described) -> None:
        described(_security(owner="S-1-5-21-9-9-9-1002"))
        with pytest.raises(ValueError, match="owned by another account"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )

    def test_an_untrusted_writer_is_refused_and_named(self, described) -> None:
        described(_security(owner=SYSTEM, writers=(_writer(EVERYONE, "WRITE_DATA", "DELETE"),)))
        with pytest.raises(ValueError, match="can be replaced by") as excinfo:
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )
        # The message must identify the offender: an operator cannot fix
        # "permission denied".
        assert EVERYONE in str(excinfo.value)
        assert "WRITE_DATA" in str(excinfo.value)

    def test_authenticated_users_holding_delete_child_is_still_refused(self, described) -> None:
        """The C:\\ carve-out is about which RIGHT, never about which principal."""
        described(
            _security(owner=SYSTEM, writers=(_writer(AUTHENTICATED_USERS, "FILE_DELETE_CHILD"),))
        )
        with pytest.raises(ValueError, match="can be replaced by"):
            runner.check_provider_path_component_windows(
                Path("C:/"), label="executable parent", me_sid=ME, strict=False
            )


class TestStrictPolicy:
    def test_a_user_owned_component_is_refused(self, described) -> None:
        """Strict mode is the "root-owned" analog: the machine must own it."""
        described(_security(owner=ME))
        with pytest.raises(ValueError, match="not owned by the system"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=True
            )

    def test_the_gateway_user_holding_a_write_right_is_refused(self, described) -> None:
        described(_security(owner=SYSTEM, writers=(_writer(ME, "WRITE_DATA"),)))
        with pytest.raises(ValueError, match="can be replaced by"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=True
            )

    def test_a_system_owned_component_passes(self, described) -> None:
        described(_security(owner=SYSTEM, writers=(_writer(SYSTEM), _writer(ADMINS))))
        runner.check_provider_path_component_windows(
            Path("C:/anything"), label="executable", me_sid=ME, strict=True
        )


class TestFailClosed:
    def test_a_null_dacl_is_refused(self, described) -> None:
        """A NULL DACL grants everyone full control, and reports no writers."""
        described(_security(owner=SYSTEM, null_dacl=True))
        with pytest.raises(ValueError, match="NULL DACL"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )

    def test_an_unparsable_ace_type_is_refused(self, described) -> None:
        """Object/callback ACEs place the SID elsewhere, so an unrecognised type
        means the writers tuple is incomplete -- refuse rather than trust it."""
        described(_security(owner=SYSTEM, unparsable=(9,)))
        with pytest.raises(ValueError, match="cannot evaluate"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )

    def test_an_unreadable_descriptor_is_refused(self, monkeypatch) -> None:
        def _boom(path):
            raise windows_acl.AclUnavailable("access denied")

        monkeypatch.setattr(windows_acl, "describe", _boom)
        with pytest.raises(ValueError, match="security descriptor is unreadable"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )


# ── remote volumes ───────────────────────────────────────────────────────────


class TestRemoteVolumesAreRefused:
    """`WELL_KNOWN_TRUSTED_SIDS` is only meaningful on a local volume.

    `S-1-5-18` and `S-1-5-32-544` are machine-local ALIAS SIDs: the same literal
    string on every machine, denoting a different principal on each. Reading the
    descriptor of a file on a remote share yields the FILE SERVER's SYSTEM and
    Administrators, so trusting them means "whoever administers that server may
    replace the binary this gateway executes".

    The fact rides on `ComponentSecurity` rather than being a second call the
    policy makes, which is what keeps these tests -- and the whole policy suite --
    runnable on the Linux CI runner. An earlier revision of this change called
    `GetDriveTypeW` directly from the policy and broke all 12 sibling policy tests
    with `AclUnavailable` on Linux; the split is load-bearing, not decorative.
    """

    def test_a_remote_volume_is_refused(self, described) -> None:
        described(_security(owner=SYSTEM, volume_is_local=False))
        with pytest.raises(ValueError, match="not on a local volume") as caught:
            runner.check_provider_path_component_windows(
                Path("//server/share/gh.exe"), label="executable", me_sid=ME, strict=False
            )
        assert "machine-local" in str(caught.value), "the reason must name why it matters"

    def test_a_remote_volume_is_refused_even_when_the_acl_is_otherwise_perfect(
        self, described
    ) -> None:
        """System-owned with only trusted writers -- the exact shape that passes locally.

        Without the volume check this is indistinguishable from a clean local
        install, which is the whole point: the SIDs look right because they are
        the SAME STRINGS, just resolved against another machine.
        """
        described(
            _security(
                owner=SYSTEM,
                writers=(_writer(SYSTEM), _writer(ADMINS)),
                volume_is_local=False,
            )
        )
        with pytest.raises(ValueError, match="not on a local volume"):
            runner.check_provider_path_component_windows(
                Path("//server/share/gh.exe"), label="executable", me_sid=ME, strict=False
            )

    def test_strict_mode_refuses_it_too(self, described) -> None:
        described(_security(owner=SYSTEM, volume_is_local=False))
        with pytest.raises(ValueError, match="not on a local volume"):
            runner.check_provider_path_component_windows(
                Path("//server/share/gh.exe"), label="executable", me_sid=ME, strict=True
            )

    def test_a_local_volume_still_passes(self, described) -> None:
        """The guard must not refuse everything, or it would pass vacuously."""
        described(_security(owner=SYSTEM, writers=(_writer(SYSTEM), _writer(ADMINS))))
        runner.check_provider_path_component_windows(
            Path("C:/Program Files/GitHub CLI/gh.exe"),
            label="executable",
            me_sid=ME,
            strict=False,
        )

    def test_the_policy_never_touches_the_platform(self, described, monkeypatch) -> None:
        """The decision must be a pure function of the descriptor. Regression test.

        An earlier revision of this change read `GetDriveTypeW` directly from the
        policy, which calls `_load()` and raises `AclUnavailable` off Windows. On
        the Linux CI shards that failed all 12 sibling policy tests at once, even
        though each of them stubs `describe` -- the new call ran before their stub
        was ever reached.

        `_load` failing the test is the assertion: it is the single door to every
        platform-bound symbol in the module, so if the policy stays clear of it the
        whole suite keeps running wherever CI puts it.
        """
        monkeypatch.setattr(
            windows_acl,
            "_load",
            lambda: pytest.fail("the trust policy reached a platform-bound read"),
        )
        described(_security(owner=SYSTEM, writers=(_writer(SYSTEM), _writer(ADMINS))))
        runner.check_provider_path_component_windows(
            Path("C:/Program Files/GitHub CLI/gh.exe"),
            label="executable",
            me_sid=ME,
            strict=False,
        )


# ── candidate discovery ──────────────────────────────────────────────────────


class TestWellknownWindowsDirs:
    def test_posix_has_none(self, monkeypatch) -> None:
        monkeypatch.setattr(runner.sys, "platform", "linux")
        assert runner._wellknown_windows_dirs("gh") == ()

    def test_windows_names_the_program_files_subdirs(self, monkeypatch) -> None:
        """Built with ``os.path.join``, not literal separators.

        These tests run on the Ubuntu CI runner too, where `sys.platform` is
        monkeypatched but `os.path.join` is still posixpath and joins with `/`.
        A literal backslash in the expectation would assert the runner's path
        flavour rather than the function's behaviour.
        """
        monkeypatch.setattr(runner.sys, "platform", "win32")
        monkeypatch.setenv("ProgramFiles", r"C:\PF")
        monkeypatch.delenv("ProgramW6432", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        root = r"C:\PF"
        assert runner._wellknown_windows_dirs("gh") == (
            os.path.join(root, "GitHub CLI"),
            os.path.join(root, "GitHub CLI", "bin"),
        )

    def test_windows_keeps_azure_cli_install_path_nested(self, monkeypatch) -> None:
        monkeypatch.setattr(runner.sys, "platform", "win32")
        monkeypatch.setenv("ProgramFiles", r"C:\PF")
        monkeypatch.delenv("ProgramW6432", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        root = r"C:\PF"
        install_dir = os.path.join(root, "Microsoft SDKs", "Azure", "CLI2", "wbin")

        assert runner._wellknown_windows_dirs("az") == (
            install_dir,
            os.path.join(install_dir, "bin"),
        )

    def test_an_unset_root_is_skipped_rather_than_joined_as_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(runner.sys, "platform", "win32")
        monkeypatch.delenv("ProgramFiles", raising=False)
        monkeypatch.delenv("ProgramW6432", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        assert runner._wellknown_windows_dirs("gh") == ()


class TestCandidateResolutionUsesTheStdlib:
    """Resolution inside a directory is ``shutil.which``'s job.

    The first draft of this change hand-rolled it and joined the bare name, so
    the scan looked for ``gh`` and never matched ``gh.exe`` -- on Windows it
    found nothing at all, before the trust policy was even consulted. It also
    split ``PATHEXT`` on ``os.pathsep``, which is ``:`` off Windows. Delegating
    removes both mistakes, so the test that matters is that we delegate.
    """

    @pytest.fixture(autouse=True)
    def _only_path_is_searched(self, monkeypatch):
        """Neutralise the well-known dirs so only the fixture's own dir is seen.

        Without this the Windows runner finds its REAL
        ``C:\\Program Files\\GitHub CLI\\gh.EXE`` and the assertions describe the
        host instead of the code.
        """
        monkeypatch.setattr(runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (), "glab": ()})
        for variable in runner.WINDOWS_PROGRAM_ROOT_VARS:
            monkeypatch.delenv(variable, raising=False)

    def _stub(self, directory: Path) -> Path:
        target = directory / ("gh.exe" if sys.platform == "win32" else "gh")
        target.write_text("stub")
        if sys.platform != "win32":
            target.chmod(0o755)
        return target

    @staticmethod
    def _same(found: tuple[str, ...], expected: Path) -> bool:
        """Compare with the platform's own path semantics.

        On Windows ``shutil.which`` returns the name cased as ``PATHEXT`` spells
        it (``gh.EXE``), not as the file is spelled on disk (``gh.exe``). Both
        name the same file, because the filesystem is case-insensitive -- and
        that is precisely why ``validate_provider_executable`` compares
        casefolded before deciding a path is non-canonical.
        """
        if sys.platform == "win32":
            return tuple(p.casefold() for p in found) == (str(expected).casefold(),)
        return found == (str(expected),)

    def test_a_path_entry_hit_is_returned_absolute(self, monkeypatch, tmp_path: Path) -> None:
        target = self._stub(tmp_path)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv(runner.STRICT_PROVIDER_BIN_ENV, raising=False)

        assert self._same(runner.provider_executable_candidates("gh"), target)

    def test_strict_mode_ignores_path(self, monkeypatch, tmp_path: Path) -> None:
        self._stub(tmp_path)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv(runner.STRICT_PROVIDER_BIN_ENV, "1")

        assert runner.provider_executable_candidates("gh") == ()

    @windows_only
    def test_a_bare_name_resolves_to_the_dot_exe_on_windows(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The regression the replaced code caused: PATHEXT must be applied."""
        target = tmp_path / "gh.exe"
        target.write_text("stub")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv(runner.STRICT_PROVIDER_BIN_ENV, raising=False)

        found = runner.provider_executable_candidates("gh")
        assert len(found) == 1
        assert found[0].casefold().endswith("gh.exe")


# ── the process CWD ──────────────────────────────────────────────────────────


class TestCandidateDiscoveryIgnoresTheProcessCwd:
    """`shutil.which(name, path=directory)` also searches the CWD on Windows.

    CPython inserts it at the FRONT of the search list::

        if sys.platform == "win32":
            # The current directory takes precedence on Windows.
            ...
            path.insert(0, curdir)

    So a checkout that happens to contain a `gh.exe` outranks every well-known
    install dir, and the gateway's CWD is not something this module controls.
    Measured on Windows before the fix: with an attacker copy in the CWD,
    `which("gh.exe", path=<real dir>)` returned `.\\gh.exe`.

    These live here, not in `test_github_runner.py`, on purpose. That file is
    skipped wholesale on Windows, so tests placed there cannot be run on the one
    platform whose behaviour this is -- which is how the sibling `str`-vs-bytes
    regression reached CI. They stub `which` rather than relying on the host, so
    they exercise the Windows hazard on the Linux shards as well.
    """

    def _only_wanted(self, monkeypatch, wanted, answer):
        monkeypatch.setattr(runner, "_wellknown_windows_dirs", lambda _e: [str(wanted)])
        monkeypatch.setattr(runner, "strict_provider_bins", lambda: True)
        monkeypatch.setattr(runner.shutil, "which", lambda _n, path=None: answer)

    def test_a_hit_outside_the_requested_directory_is_dropped(self, tmp_path, monkeypatch):
        wanted = tmp_path / "wanted"
        cwd = tmp_path / "cwd"
        wanted.mkdir()
        cwd.mkdir()
        (cwd / "gh").write_text("")

        self._only_wanted(monkeypatch, wanted, str(cwd / "gh"))

        assert str(cwd / "gh") not in runner.provider_executable_candidates("gh"), (
            "a which() hit from outside the requested directory must be dropped; "
            "on Windows that hit is the process CWD and is attacker-controlled"
        )

    def test_a_hit_inside_the_requested_directory_is_kept(self, tmp_path, monkeypatch):
        """The containment check must not reject the legitimate case."""
        wanted = tmp_path / "wanted"
        wanted.mkdir()
        target = wanted / "gh"
        target.write_text("")

        self._only_wanted(monkeypatch, wanted, str(target))

        assert str(target) in runner.provider_executable_candidates("gh")

    def test_the_relative_form_which_returns_for_a_cwd_hit_is_dropped(self, tmp_path, monkeypatch):
        """`which` reports a CWD hit as `.\\gh`, not as an absolute path.

        Pinned separately because the containment check has to normalise before
        comparing: a naive compare against the requested directory would let a
        bare `./gh` through, and that is the exact shape the real call returns.
        """
        wanted = tmp_path / "wanted"
        wanted.mkdir()
        monkeypatch.chdir(tmp_path)
        (tmp_path / "gh").write_text("")

        self._only_wanted(monkeypatch, wanted, os.path.join(".", "gh"))

        found = runner.provider_executable_candidates("gh")
        # Assert against the CWD path specifically. The returned tuple is always
        # seeded with the static well-known absolute paths, so a blanket "nothing
        # outside `wanted`" assertion would trip on those instead of on the hit
        # under test.
        assert os.path.abspath(str(tmp_path / "gh")) not in found


# ── provider output decoding ─────────────────────────────────────────────────


class TestProviderOutputIsDecodedAsUtf8:
    """`text=True` alone decodes with the LOCALE encoding, not UTF-8.

    On a Windows host whose ANSI codepage is not UTF-8 (cp936, cp932, cp1252 …)
    that turns a non-ASCII issue title into a crash, and a peculiarly bad one:
    the `UnicodeDecodeError` is raised inside subprocess's own reader THREAD, so
    `subprocess.run` returns with `stdout=None` and the caller dies on the None
    at some unrelated line instead of on a decode error it could attribute.

    Reproduced on a cp936 host before the fix:
    `UnicodeDecodeError: 'gbk' codec can't decode byte 0xac in position 27`.

    This is not Windows-only in principle — any non-UTF-8 locale does it — so
    the codec is pinned at the shared chokepoint and asserted here.
    """

    def test_run_gh_pins_utf8_and_never_inherits_the_locale_codec(self, monkeypatch) -> None:
        seen: dict[str, object] = {}

        def _fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout=b"{}", stderr=b"")

        monkeypatch.setattr(runner.subprocess, "run", _fake_run)
        monkeypatch.setattr(runner, "_audit_run", lambda *a, **k: None)

        proc = runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="test")

        assert "encoding" not in seen and seen.get("text") is not True, (
            "run_gh must capture BYTES and decode in its own frame; letting "
            "subprocess decode puts a failure on a reader thread where it "
            "returns stdout=None instead of an attributable error"
        )
        assert proc.stdout == "{}"

    def test_undecodable_output_is_attributable_and_leaks_no_payload(self, monkeypatch) -> None:
        """A strict failure must name the stream, not die on `None` downstream."""

        def _fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=b'{"t": "\xff\xfe"}', stderr=b"")

        monkeypatch.setattr(runner.subprocess, "run", _fake_run)
        monkeypatch.setattr(runner, "_audit_run", lambda *a, **k: None)

        with pytest.raises(runner.SetupError) as caught:
            runner.run_gh(["/usr/bin/gh", "api", "user"], timeout=5, audit_caller="test")

        message = str(caught.value)
        assert "not valid UTF-8" in message
        assert "\\xff" not in message and "\xff" not in message, "must not echo payload bytes"

    def test_replacing_bad_bytes_would_corrupt_a_json_string_value(self) -> None:
        """Why `errors="replace"` is NOT the fix, pinned as a behaviour.

        An earlier revision of this change used `errors="replace"`, on the
        reasoning that U+FFFD would make `json.loads` fail and land in the
        caller's existing error taxonomy. That reasoning only holds when the bad
        byte breaks JSON *syntax*. Inside a string VALUE the document stays
        syntactically valid, `json.loads` succeeds, and the replacement
        character flows on into stored issue records -- silent corruption rather
        than a handled error.
        """
        raw = '{"title": "caf\u00e9"}'.encode("utf-8")[:-3] + b"\xff" + b'"}'

        replaced = raw.decode("utf-8", errors="replace")
        parsed = json.loads(replaced)  # no exception: this is the problem
        assert "\ufffd" in parsed["title"], "corruption reaches the parsed record"

        with pytest.raises(UnicodeDecodeError):
            raw.decode("utf-8")  # strict, which is what run_gh now does

    def test_a_locale_decode_of_utf8_provider_output_is_what_we_avoid(self) -> None:
        """The mechanism itself, asserted without spawning anything.

        Note there are TWO failure modes and the codepage decides which, so this
        asserts the thing they share -- the text does not survive -- rather than
        picking one:

        * a byte the codepage rejects raises `UnicodeDecodeError` (observed on a
          cp936 host: `can't decode byte 0xac in position 27`); while
        * a byte sequence it happens to accept decodes to MOJIBAKE and no
          exception is raised at all, which is the quieter half of the bug.

        A first draft of this test asserted only the raise and failed here,
        because GBK is a wide multibyte codec that accepts these particular
        bytes.
        """
        original = '{"title": "日本語のタイトル", "zh": "中文标题"}'
        payload = original.encode("utf-8")

        assert payload.decode("utf-8") == original
        try:
            mangled = payload.decode("gbk")
        except UnicodeDecodeError:
            return  # the rejecting half of the bug
        assert mangled != original, "locale decode must not be assumed lossless"


# ── the descriptor reader, driven off Windows ────────────────────────────────
#
# Everything above tests the POLICY, which consumes a `ComponentSecurity`. The
# reader that PRODUCES one -- the ACE walk, the mask decode, the SID pointer
# arithmetic -- is pure ctypes, and on the Linux runner none of it executed at
# all: 75 of 127 statements in `windows_acl.py` were unreached, putting the file
# at 41% against an 80% per-file floor.
#
# It is testable anyway, because `_load()` is the single seam where the module
# acquires its two DLL handles. Substituting Python objects for those handles
# runs the real parsing code over a real in-memory ACL on any platform. Only the
# handle acquisition itself (`WinDLL`, the prototype assignments, `GetLastError`)
# is genuinely unreachable off Windows, and that is what carries a `no cover`
# pragma -- not the logic it configures.
#
# Every offset and size below is derived from `ctypes.sizeof` and
# `_ACCESS_ACE.SidStart.offset` rather than hardcoded, because `W.DWORD` is
# `c_ulong`: 4 bytes on Windows, 8 on 64-bit Linux. Deriving keeps the buffer this
# test builds and the struct definitions the module parses with in agreement on
# either platform.


def _build_acl(aces: tuple[tuple[int, int, int, bytes], ...]) -> Any:
    """A real ACL buffer holding *aces* as ``(type, flags, mask, sid_bytes)``.

    Returns the backing buffer; the caller keeps it alive for as long as the
    pointers into it are used.
    """
    ace_blobs = []
    for ace_type, flags, mask, sid in aces:
        size = _ACE_FIXED + len(sid)
        ace_blobs.append((ace_type, flags, mask, sid, size))

    total = ctypes.sizeof(windows_acl._ACL) + sum(b[4] for b in ace_blobs)
    buf = ctypes.create_string_buffer(total)

    acl = ctypes.cast(buf, ctypes.POINTER(windows_acl._ACL)).contents
    acl.AclRevision = 2
    acl.AclSize = total
    acl.AceCount = len(ace_blobs)

    offset = ctypes.sizeof(windows_acl._ACL)
    for ace_type, flags, mask, sid, size in ace_blobs:
        ace = ctypes.cast(
            ctypes.byref(buf, offset), ctypes.POINTER(windows_acl._ACCESS_ACE)
        ).contents
        ace.Header.AceType = ace_type
        ace.Header.AceFlags = flags
        ace.Header.AceSize = size
        ace.Mask = mask
        if sid:
            ctypes.memmove(
                ctypes.addressof(buf) + offset + windows_acl._ACCESS_ACE.SidStart.offset,
                sid,
                len(sid),
            )
        offset += size
    return buf


_ACE_FIXED = ctypes.sizeof(windows_acl._ACCESS_ACE) - ctypes.sizeof(ctypes.c_ulong)


class _FakeDlls:
    """Python stand-ins for advapi32 and kernel32, driving the real reader.

    Out-parameters are filled through ``byref(x)._obj``, which is how a
    pure-Python callable reaches the object a `byref` reference wraps.
    """

    def __init__(self, *, acl_buf=None, null_dacl=False, rc=0, sids=None, names=None):
        """*sids* maps a SID key to its string form; *names* to ``(name, domain)``."""
        self._acl_buf = acl_buf
        self._null_dacl = null_dacl
        self._rc = rc
        self._sids = sids or {}
        self._names = names or {}
        self.freed: list[object] = []
        self.getace_fails_at: int | None = None
        self.sid_string_fails = False
        self.drive_type = 3  # DRIVE_FIXED

    # -- advapi32 ---------------------------------------------------------
    def GetNamedSecurityInfoW(self, path, obj_type, info, owner_ref, _g, dacl_ref, _s, sd_ref):
        if self._rc != 0:
            return self._rc
        owner_ref._obj.value = _OWNER_PTR
        sd_ref._obj.value = 0xBEEF
        if not self._null_dacl:
            dacl_ref._obj.contents = ctypes.cast(
                self._acl_buf, ctypes.POINTER(windows_acl._ACL)
            ).contents
        return 0

    def GetAce(self, dacl, index, ace_ref):
        if self.getace_fails_at == index:
            return 0
        offset = ctypes.sizeof(windows_acl._ACL)
        for _ in range(index):
            hdr = ctypes.cast(
                ctypes.byref(self._acl_buf, offset), ctypes.POINTER(windows_acl._ACE_HEADER)
            ).contents
            offset += int(hdr.AceSize)
        ace_ref._obj.value = ctypes.addressof(self._acl_buf) + offset
        return 1

    def ConvertSidToStringSidW(self, psid, out_ref):
        if self.sid_string_fails:
            return 0
        out_ref._obj.value = self._sids.get(self._sid_key(psid), "S-1-0-0")
        return 1

    def LookupAccountSidW(self, _sys, psid, name, name_len, domain, domain_len, use):
        entry = self._names.get(self._sid_key(psid))
        if entry is None:
            return 0
        name.value, domain.value = entry
        return 1

    # -- kernel32 ---------------------------------------------------------
    def LocalFree(self, handle):
        self.freed.append(handle)
        return None

    def GetDriveTypeW(self, _root):
        return self.drive_type

    def _sid_key(self, psid) -> int:
        """Identify a SID by the byte it points at, so ACE SIDs stay distinct."""
        value = psid.value if hasattr(psid, "value") else psid
        if value == _OWNER_PTR:
            return _OWNER_PTR
        return ctypes.cast(value, ctypes.POINTER(ctypes.c_byte))[0]


_OWNER_PTR = 0x1000


@pytest.fixture
def reader(monkeypatch):
    """Install a `_FakeDlls` as the module's DLL handles and hand it back."""

    def _install(fake: _FakeDlls) -> _FakeDlls:
        monkeypatch.setattr(windows_acl, "_load", lambda: (fake, fake))
        return fake

    return _install


class TestDescribeParsesARealAcl:
    """The ACE walk, decoded off Windows over a hand-built ACL."""

    def test_an_allowed_ace_with_a_substitution_right_becomes_a_writer(self, reader) -> None:
        buf = _build_acl(((windows_acl.ACCESS_ALLOWED_ACE_TYPE, 0, 0x10000, b"\x07rest"),))
        fake = reader(
            _FakeDlls(
                acl_buf=buf,
                sids={_OWNER_PTR: SYSTEM, 7: ME},
                names={_OWNER_PTR: ("SYSTEM", "NT AUTHORITY"), 7: ("raymond", "HOST")},
            )
        )

        got = windows_acl.describe(Path("C:/x/gh.exe"))

        assert got.owner_sid == SYSTEM
        assert got.owner_name == "NT AUTHORITY\\SYSTEM"
        assert got.null_dacl is False
        assert got.unparsable_ace_types == ()
        assert got.volume_is_local is (sys.platform == "win32")
        assert [(w.sid, w.name, w.rights) for w in got.writers] == [
            (ME, "HOST\\raymond", ("DELETE",))
        ]
        assert fake.freed, "the security descriptor must be released"

    def test_a_deny_ace_is_ignored(self, reader) -> None:
        """Deny ACEs are deliberately not evaluated -- see the module docstring."""
        buf = _build_acl(((windows_acl.ACCESS_DENIED_ACE_TYPE, 0, 0x10000, b"\x07rest"),))
        reader(_FakeDlls(acl_buf=buf, sids={_OWNER_PTR: SYSTEM}))
        assert windows_acl.describe(Path("C:/x")).writers == ()

    def test_an_unknown_ace_type_is_recorded_not_skipped(self, reader) -> None:
        """A partially-understood descriptor must make the caller refuse."""
        buf = _build_acl(((9, 0, 0x10000, b"\x07rest"),))
        reader(_FakeDlls(acl_buf=buf, sids={_OWNER_PTR: SYSTEM}))
        got = windows_acl.describe(Path("C:/x"))
        assert got.unparsable_ace_types == (9,)
        assert got.writers == ()

    def test_an_inherit_only_ace_is_skipped(self, reader) -> None:
        """INHERIT_ONLY grants nothing on the object itself."""
        buf = _build_acl(
            ((windows_acl.ACCESS_ALLOWED_ACE_TYPE, windows_acl.INHERIT_ONLY_ACE, 0x10000, b"\x07"),)
        )
        reader(_FakeDlls(acl_buf=buf, sids={_OWNER_PTR: SYSTEM}))
        assert windows_acl.describe(Path("C:/x")).writers == ()

    def test_an_ace_with_no_substitution_right_is_skipped(self, reader) -> None:
        buf = _build_acl(((windows_acl.ACCESS_ALLOWED_ACE_TYPE, 0, 0x1, b"\x07"),))  # READ_DATA
        reader(_FakeDlls(acl_buf=buf, sids={_OWNER_PTR: SYSTEM}))
        assert windows_acl.describe(Path("C:/x")).writers == ()

    def test_several_aces_are_walked_in_order(self, reader) -> None:
        """Pins the per-ACE offset arithmetic, which a single-ACE test cannot."""
        buf = _build_acl(
            (
                (windows_acl.ACCESS_ALLOWED_ACE_TYPE, 0, 0x10000, b"\x07aa"),
                (windows_acl.ACCESS_DENIED_ACE_TYPE, 0, 0x10000, b"\x08bbbb"),
                (windows_acl.ACCESS_ALLOWED_ACE_TYPE, 0, 0x40000, b"\x09c"),
            )
        )
        reader(
            _FakeDlls(
                acl_buf=buf,
                sids={_OWNER_PTR: SYSTEM, 7: ME, 9: ADMINS},
                names={7: ("one", "H"), 9: ("three", "H")},
            )
        )
        got = windows_acl.describe(Path("C:/x"))
        assert [w.sid for w in got.writers] == [ME, ADMINS]

    def test_a_null_dacl_is_reported_explicitly(self, reader) -> None:
        reader(_FakeDlls(null_dacl=True, sids={_OWNER_PTR: SYSTEM}))
        got = windows_acl.describe(Path("C:/x"))
        assert got.null_dacl is True
        assert got.writers == ()

    def test_an_unresolvable_sid_degrades_to_a_placeholder(self, reader) -> None:
        """A display name is never load-bearing; the policy compares SIDs."""
        buf = _build_acl(((windows_acl.ACCESS_ALLOWED_ACE_TYPE, 0, 0x10000, b"\x07"),))
        reader(_FakeDlls(acl_buf=buf, sids={_OWNER_PTR: SYSTEM, 7: ME}, names={}))
        assert windows_acl.describe(Path("C:/x")).writers[0].name == "<unresolved>"

    def test_a_name_with_no_domain_is_returned_bare(self, reader) -> None:
        buf = _build_acl(((windows_acl.ACCESS_ALLOWED_ACE_TYPE, 0, 0x10000, b"\x07"),))
        reader(_FakeDlls(acl_buf=buf, sids={_OWNER_PTR: SYSTEM, 7: ME}, names={7: ("local", "")}))
        assert windows_acl.describe(Path("C:/x")).writers[0].name == "local"


class TestDescribeFailsClosed:
    """Every read that cannot complete must raise, never return a clean result."""

    def test_a_failed_descriptor_read_raises(self, reader) -> None:
        reader(_FakeDlls(rc=5))
        with pytest.raises(windows_acl.AclUnavailable, match="error 5"):
            windows_acl.describe(Path("C:/x"))

    def test_a_failed_getace_raises_rather_than_skipping_the_ace(self, reader) -> None:
        """Skipping would let a missed grant read as clean."""
        buf = _build_acl(((windows_acl.ACCESS_ALLOWED_ACE_TYPE, 0, 0x10000, b"\x07"),))
        fake = reader(_FakeDlls(acl_buf=buf, sids={_OWNER_PTR: SYSTEM}))
        fake.getace_fails_at = 0
        with pytest.raises(windows_acl.AclUnavailable, match="GetAce"):
            windows_acl.describe(Path("C:/x"))

    def test_a_failed_sid_stringification_raises(self, reader) -> None:
        fake = reader(_FakeDlls(null_dacl=True))
        fake.sid_string_fails = True
        with pytest.raises(windows_acl.AclUnavailable, match="ConvertSidToStringSid"):
            windows_acl.describe(Path("C:/x"))


class TestVolumeClassification:
    """`_volume_is_local` over the shapes that matter.

    Windows-only, and not by oversight: `posixpath.splitdrive` never returns a
    drive component, so off Windows `_volume_is_local` short-circuits to False
    before `GetDriveTypeW` is ever consulted. The POLICY consequence of a remote
    volume is covered platform-independently in `TestRemoteVolumesAreRefused`,
    which feeds `volume_is_local=False` straight into the decision.
    """

    @windows_only
    @pytest.mark.parametrize("drive_type,expected", [(3, True), (2, True), (6, True), (4, False)])
    def test_drive_type_decides(self, reader, drive_type, expected) -> None:
        fake = reader(_FakeDlls(null_dacl=True, sids={_OWNER_PTR: SYSTEM}))
        fake.drive_type = drive_type
        assert windows_acl.describe(Path("C:/x")).volume_is_local is expected

    @windows_only
    def test_an_unclassifiable_drive_type_is_not_local(self, reader) -> None:
        fake = reader(_FakeDlls(null_dacl=True, sids={_OWNER_PTR: SYSTEM}))
        fake.drive_type = 0  # DRIVE_UNKNOWN
        assert windows_acl.describe(Path("C:/x")).volume_is_local is False

    @windows_only
    def test_a_unc_path_yields_the_share_root(self, reader) -> None:
        """The root handed to GetDriveTypeW must be `\\\\server\\share\\`, not the file."""
        seen: list[str] = []
        fake = reader(_FakeDlls(null_dacl=True, sids={_OWNER_PTR: SYSTEM}))
        fake.GetDriveTypeW = lambda root: (seen.append(root), 4)[1]
        windows_acl.describe(Path("//server/share/gh.exe"))
        assert seen == ["\\\\server\\share" + os.sep]

    def test_no_drive_component_is_not_local(self, reader) -> None:
        """The off-Windows shape, asserted where it actually occurs.

        A path with no drive has no volume to classify, and "not local" is the
        fail-closed answer. On Windows `abspath` always supplies a drive, so this
        drives the predicate directly rather than through `describe`.
        """
        fake = _FakeDlls()
        assert windows_acl._volume_is_local(fake, Path("relative/gh")) is (sys.platform == "win32")


# ── the real ACL read ────────────────────────────────────────────────────────


@windows_only
class TestDescribeAgainstRealAcls:
    def test_a_user_owned_tree_reports_the_user_as_owner(self, tmp_path: Path) -> None:
        binary = tmp_path / "gh.exe"
        binary.write_text("stub")
        security = windows_acl.describe(binary)
        assert security.owner_sid == platform_compat.current_user_sid()
        assert not security.null_dacl
        assert security.unparsable_ace_types == ()

    def test_granting_everyone_full_control_surfaces_everyone_as_a_writer(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "evil"
        target.mkdir()
        binary = target / "gh.exe"
        binary.write_text("stub")
        subprocess.run(
            ["icacls", str(target), "/grant", "Everyone:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
        sids = {writer.sid for writer in windows_acl.describe(binary).writers}
        assert EVERYONE in sids

        # and the policy must refuse it
        with pytest.raises(ValueError, match="can be replaced by"):
            runner.check_provider_path_component_windows(
                binary,
                label="executable",
                me_sid=platform_compat.current_user_sid(),
                strict=False,
            )

    def test_the_drive_root_does_not_refuse_the_walk(self) -> None:
        """Regression: the stock C:\\ ACL grants Authenticated Users
        ADD_SUBDIRECTORY, which is not a substitution right."""
        root = Path(os.environ.get("SystemDrive", "C:") + os.sep)
        runner.check_provider_path_component_windows(
            root,
            label="executable parent",
            me_sid=platform_compat.current_user_sid(),
            strict=False,
        )

    def test_the_current_user_sid_is_a_well_formed_sid(self) -> None:
        assert platform_compat.current_user_sid().startswith("S-1-")

    def test_elevation_is_reported_as_a_tri_state(self) -> None:
        """``None`` (token unreadable) is distinct from ``False`` (not elevated).

        Lives in ``platform_compat`` rather than here: it already owns reading
        this process's own token, and a second copy of the OpenProcessToken /
        GetTokenInformation prototype pair is plumbing that drifts.
        """
        assert platform_compat.is_token_elevated() in (True, False, None)


class TestLoadRefusesOffWindows:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX behaviour")
    def test_describe_refuses_rather_than_returning_a_permissive_answer(self) -> None:
        with pytest.raises(windows_acl.AclUnavailable):
            windows_acl.describe(Path("/etc/hosts"))


class TestApplyOwnerOnlyOffWindows:
    """The WRITE side, driven off Windows through the same injected-handle seam.

    `_load()` is the module's only platform gate, so substituting plain Python
    objects for the two DLL handles runs the real ACL construction anywhere.

    The volume gate is injected rather than derived, mirroring the split the
    reader already uses: `TestVolumeClassification` owns `_volume_is_local`'s
    platform-bound parsing (Windows-only, because `posixpath.splitdrive` never
    returns a drive so it short-circuits to False off Windows), and the POLICY
    consequence is asserted platform-independently by feeding the decision in.
    Deriving it here instead is what made the first version of this class pass on
    Windows and fail on every POSIX shard: `"C:\\\\x"` has no drive component off
    Windows, so the local-volume cases refused before constructing anything.

    Note this mechanism applies the DACL on ANY volume. The event-loop caller's
    volume gate is NOT here -- it is :func:`windows_acl.volume_is_local`, asked at
    the call site before the write begins, because a refusal raised from inside
    this function would arrive after the caller had already paid the filesystem
    cost the gate exists to avoid.
    """

    @staticmethod
    def _writer():
        """A fake exposing just the write-side surface, plus a call recorder."""

        class _FakeWriter:
            def __init__(self) -> None:
                self.set_calls: list[dict] = []
                self.freed: list[object] = []
                self._next = 0x1000

            # -- kernel32 --
            def LocalFree(self, handle):
                self.freed.append(handle)
                return None

            # -- advapi32 --
            def ConvertStringSidToSidW(self, sid, out_ref):
                self._next += 0x10
                out_ref._obj.value = self._next
                return 1

            def GetLengthSid(self, _psid):
                return 12

            def InitializeAcl(self, _pacl, _size, _rev):
                return 1

            def AddAccessAllowedAceEx(self, _pacl, _rev, flags, mask, psid):
                self.set_calls.append({"kind": "ace", "flags": flags, "mask": mask})
                return 1

            def SetNamedSecurityInfoW(self, path, obj, info, _o, _g, _dacl, _s):
                self.set_calls.append({"kind": "set", "path": path, "info": info})
                return 0

        return _FakeWriter()

    def _install(self, monkeypatch, *, local: bool):
        """Install the fake handles and an injected volume verdict; record calls."""
        fake = self._writer()
        seen: list[str] = []
        monkeypatch.setattr(windows_acl, "_load", lambda: (fake, fake))
        monkeypatch.setattr(
            windows_acl,
            "_volume_is_local",
            lambda _k32, path: (seen.append(str(path)), local)[1],
        )
        return fake, seen

    def test_the_volume_is_never_consulted_by_this_mechanism(self, monkeypatch) -> None:
        """The writer applies the DACL on ANY volume; the gate is not its job.

        local=False would have refused while the gate lived here. It no longer
        does: an on-loop caller has to ask before it starts (see
        :func:`windows_acl.volume_is_local`), because a refusal at this depth
        arrives after the caller already paid the cost it was avoiding.
        """
        fake, seen = self._install(monkeypatch, local=False)
        windows_acl.apply_owner_only("Z:\\home\\config.json", inherit=False, sids=("S-1-3-4",))
        assert seen == [], "the writer must not classify the volume itself"
        assert any(c["kind"] == "set" for c in fake.set_calls), fake.set_calls

    def test_the_public_predicate_reports_a_network_volume(self, monkeypatch) -> None:
        # The seam an on-loop caller uses BEFORE it writes anything. It must
        # classify without touching the platform write surface at all.
        fake, seen = self._install(monkeypatch, local=False)
        assert windows_acl.volume_is_local("Z:\\home\\config.json") is False
        assert seen == ["Z:\\home\\config.json"], seen
        assert fake.set_calls == [], f"classifying must write nothing: {fake.set_calls}"

    def test_the_public_predicate_reports_a_local_volume(self, monkeypatch) -> None:
        fake, seen = self._install(monkeypatch, local=True)
        assert windows_acl.volume_is_local("C:\\config.json") is True
        assert seen == ["C:\\config.json"], seen
        assert fake.set_calls == [], f"classifying must write nothing: {fake.set_calls}"

    def test_a_local_volume_writes_both_grants_and_frees_every_sid(self, monkeypatch) -> None:
        fake, _ = self._install(monkeypatch, local=True)
        windows_acl.apply_owner_only(
            "C:\\config.json", inherit=False, sids=("S-1-3-4", "S-1-5-21-1")
        )
        aces = [c for c in fake.set_calls if c["kind"] == "ace"]
        sets = [c for c in fake.set_calls if c["kind"] == "set"]
        assert len(aces) == 2, aces
        assert len(sets) == 1, sets
        # File shape: no inheritance flags.
        assert all(c["flags"] == 0 for c in aces), aces
        # PROTECTED_DACL is what replaces icacls' /inheritance:r -- without it the
        # new DACL would merge with the parent's instead of replacing it.
        assert sets[0]["info"] & 0x80000000, hex(sets[0]["info"])
        # Every parsed SID is LocalAlloc'd and must be freed.
        assert len(fake.freed) == 2, fake.freed

    def test_a_directory_grant_carries_both_inheritance_flags(self, monkeypatch) -> None:
        fake, _ = self._install(monkeypatch, local=True)
        windows_acl.apply_owner_only("C:\\vault", inherit=True, sids=("S-1-3-4",))
        aces = [c for c in fake.set_calls if c["kind"] == "ace"]
        # OBJECT_INHERIT (0x1) propagates to files, CONTAINER_INHERIT (0x2) to
        # subdirectories; neither sets INHERIT_ONLY, so the directory itself keeps
        # the grant and stays traversable.
        assert all(c["flags"] == 0x03 for c in aces), aces

    def test_a_failed_descriptor_write_still_frees_the_sids(self, monkeypatch) -> None:
        fake, _ = self._install(monkeypatch, local=True)
        monkeypatch.setattr(fake, "SetNamedSecurityInfoW", lambda *a: 5)  # ACCESS_DENIED
        with pytest.raises(windows_acl.AclWriteFailed):
            windows_acl.apply_owner_only("C:\\config.json", inherit=False, sids=("S-1-3-4",))
        assert len(fake.freed) == 1, fake.freed

    def test_empty_sids_is_refused_before_the_platform_is_touched(self, monkeypatch) -> None:
        """An ACL with no ACEs denies everyone, the owner included."""

        def _boom():
            pytest.fail("_load() must not be reached for an empty grant list")

        monkeypatch.setattr(windows_acl, "_load", _boom)
        with pytest.raises(windows_acl.AclWriteFailed, match="no grants"):
            windows_acl.apply_owner_only("C:\\config.json", inherit=False, sids=())
