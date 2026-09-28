"""The live-target pointer's spawn refusal has to reach a HUMAN, not just the log.

``sandbox._materialize_live_target_mask_target`` is fail-closed on every Linux spawn: a
symlink, a non-regular file, or a second hard link on ``live_target.json`` means a bind
mask over that name would leave another writable path to the bytes the gateway
``execve``s into, so the launcher refuses rather than run an agent with the hole open.
That refusal is right. Its VISIBILITY was not:

* the operator's only notice was a ``logger.warning`` in the gateway log plus agents that
  stopped starting -- and the shapes that trigger it are ordinary operation for something
  else on the host (``cp -al``, rsnapshot, a dotfile manager keeping the pointer as a
  link), so nobody did anything wrong and nobody has a reason to read that log;
* on the CLI the refusal is not in the ``AcpError`` hierarchy, so ``_stream_and_print``'s
  handler never saw it and it escaped as a stack trace that reads as a Kiro Crew crash;
* nothing named the condition BEFORE a spawn tried and failed.

So this file pins three surfaces and the one property that keeps them honest: they all
say the SAME sentence, because they all read it from the same formatter. A paraphrase in
any one of them is a second diagnosis of one file, and the drift guard here fails on it.
"""

from __future__ import annotations

import inspect
import os
import sys

import pytest

from kiro_crew import cli_chat, cli_doctor, sandbox

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX link semantics")
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the refusal is on the Linux launcher path"
)


@pytest.fixture(autouse=True)
def confined_host(monkeypatch):
    """Pin the effective sandbox mode, so doctor's wording is not host-dependent.

    ``_doctor_live_target_pointer`` asks ``credential_mask_applies`` whether ``wrap_argv``
    would WRAP the child at all: an unwrapped host never reaches the materialiser, so the
    pointer is unfit there and nothing is refused. Leaving that answer to the developer's
    own config and backend would make every assertion below pass or fail by machine.
    Pinned to a confining host; the tests covering the unwrapped branch override it.
    """
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "strict")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True)


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    """An isolated crew data home, so no test here reads the developer's own pointer."""
    home = tmp_path / ".kiro" / "crew"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: home)
    monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
    return home


def _pointer(crew_home):
    return crew_home / sandbox._LIVE_TARGET_LEAF


class TestTheProbeClassifiesWhatTheLauncherWouldRefuse:
    """``live_target_pointer_unfitness`` is the pre-spawn read: same verdict, no spawn.

    It must agree with the launcher in BOTH directions. A false alarm sends an operator
    to remove a file that was fine; a miss leaves the condition invisible, which is the
    whole defect.
    """

    def test_a_healthy_pointer_reports_nothing(self, crew_home) -> None:
        _pointer(crew_home).write_text('{"checkout": "/real"}\n', encoding="utf-8")

        assert sandbox.live_target_pointer_unfitness() is None

    def test_an_absent_pointer_reports_nothing(self, crew_home) -> None:
        """Absent is FIT: the materialiser publishes the absent-equivalent stub for it,
        which is the reason that function exists. Reporting absence would fire on every
        fresh install."""
        assert not _pointer(crew_home).exists()

        assert sandbox.live_target_pointer_unfitness() is None

    def test_an_absent_data_home_reports_nothing(self, tmp_path, monkeypatch) -> None:
        """A host with no install is not a host with a broken pointer."""
        monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path / "nope")

        assert sandbox.live_target_pointer_unfitness() is None

    @_POSIX_ONLY
    def test_a_second_hard_link_is_reported(self, crew_home) -> None:
        """The snapshot-tool case from the issue: ``cp -al`` / rsnapshot raise link counts
        as normal operation, so this is the shape an operator meets without touching
        anything."""
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "snapshot-alias")

        unfit = sandbox.live_target_pointer_unfitness()

        assert unfit is not None
        # Which SHAPE it found, without matching prose: compared against the formatter
        # that builds this shape's sentence, so a reworded remedy moves both sides at once
        # while a MISclassification (any other formatter) still fails.
        assert unfit.detail == sandbox._live_target_multilink_detail(str(pointer), 2)
        assert unfit.path == str(pointer)
        # The remedy names the command, not just the condition: the operator does not
        # know which OTHER path shares the inode, so "remove the extra link" alone names
        # no file to remove.
        assert "-samefile" in unfit.detail
        assert str(pointer) in unfit.detail

    @_POSIX_ONLY
    def test_a_symlink_is_reported(self, crew_home, tmp_path) -> None:
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text("{}\n", encoding="utf-8")
        _pointer(crew_home).symlink_to(elsewhere)

        unfit = sandbox.live_target_pointer_unfitness()

        assert unfit is not None
        assert unfit.detail == sandbox._live_target_symlink_detail(
            str(_pointer(crew_home)), str(elsewhere)
        )
        assert str(elsewhere) in unfit.detail

    @_POSIX_ONLY
    def test_a_dangling_symlink_is_reported_too(self, crew_home, tmp_path) -> None:
        """A link with no referent is the same hole with the target missing, so it must
        not read as healthy just because ``exists()`` is False."""
        _pointer(crew_home).symlink_to(tmp_path / "gone.json")

        unfit = sandbox.live_target_pointer_unfitness()

        assert unfit is not None
        assert unfit.detail == sandbox._live_target_symlink_detail(
            str(_pointer(crew_home)), str(tmp_path / "gone.json")
        )

    @_POSIX_ONLY
    def test_a_special_file_is_reported(self, crew_home) -> None:
        os.mkfifo(_pointer(crew_home))

        unfit = sandbox.live_target_pointer_unfitness()

        assert unfit is not None
        assert unfit.detail == sandbox._live_target_irregular_detail(str(_pointer(crew_home)))
        assert "non-regular file" in unfit.detail


class TestTheProbeAndTheRefusalSayOneThing:
    """The drift guard, and the reason the formatters exist.

    Three surfaces now report this condition (doctor, the chat card, the CLI). If any of
    them carried its own wording, an operator who saw the doctor line and later hit the
    refusal would read two descriptions of one file and have to work out they are the
    same problem. So the assertion is not "both mention hard links" -- it is that the
    launcher's exception text CONTAINS the probe's sentence verbatim.
    """

    @_POSIX_ONLY
    def test_the_multilink_refusal_carries_the_probe_sentence_verbatim(self, crew_home) -> None:
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "snapshot-alias")
        unfit = sandbox.live_target_pointer_unfitness()
        assert unfit is not None

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_live_target_mask_target()

        assert unfit.detail in str(err.value)

    @_POSIX_ONLY
    def test_the_symlink_refusal_carries_the_probe_sentence_verbatim(
        self, crew_home, tmp_path
    ) -> None:
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text("{}\n", encoding="utf-8")
        _pointer(crew_home).symlink_to(elsewhere)
        unfit = sandbox.live_target_pointer_unfitness()
        assert unfit is not None

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_live_target_mask_target()

        assert unfit.detail in str(err.value)

    @_POSIX_ONLY
    def test_the_irregular_refusal_carries_the_probe_sentence_verbatim(self, crew_home) -> None:
        os.mkfifo(_pointer(crew_home))
        unfit = sandbox.live_target_pointer_unfitness()
        assert unfit is not None

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_live_target_mask_target()

        assert unfit.detail in str(err.value)

    @_POSIX_ONLY
    def test_a_dangling_link_also_refuses_through_the_pointers_own_check(
        self, crew_home, tmp_path
    ) -> None:
        """The pointer's own symlink check replaced two generic helpers, so it must refuse
        the SUPERSET they refused between them -- a dangling link included."""
        _pointer(crew_home).symlink_to(tmp_path / "gone.json")
        unfit = sandbox.live_target_pointer_unfitness()
        assert unfit is not None

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as err:
            sandbox._materialize_live_target_mask_target()

        assert unfit.detail in str(err.value)


class TestDoctorNamesItBeforeTheNextSpawn:
    """``kirocrew doctor`` is where an operator looks when agents stop starting."""

    @_LINUX_ONLY
    def test_a_healthy_pointer_prints_nothing(self, crew_home, capsys) -> None:
        """Silent on a healthy host, like the installer-residue section: a line every run
        for the normal state is noise, and noise is what stops people reading doctor."""
        _pointer(crew_home).write_text('{"checkout": "/real"}\n', encoding="utf-8")
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        assert capsys.readouterr().out == ""
        assert issues == []

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_a_multi_linked_pointer_is_reported_with_its_remedy(self, crew_home, capsys) -> None:
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "snapshot-alias")
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        out = capsys.readouterr().out
        assert "Live Target Pointer" in out
        assert str(pointer) in out
        # The remedy has to survive the wrapper, or the operator reads the condition and
        # not the fix. Matched on the distinctive token rather than the whole sentence,
        # which _print_wrapped may break across lines.
        assert "-samefile" in out
        assert issues == ["live-target pointer"]

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_a_symlinked_pointer_is_reported(self, crew_home, tmp_path, capsys) -> None:
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text("{}\n", encoding="utf-8")
        _pointer(crew_home).symlink_to(elsewhere)
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        out = capsys.readouterr().out
        assert "SYMLINK" in out
        assert issues == ["live-target pointer"]

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_the_find_command_is_printed_unbroken(self, crew_home, capsys) -> None:
        """A remedy the operator cannot copy is not a remedy.

        Doctor wraps details at 80 columns, and ``textwrap``'s defaults split long words
        AND break after an embedded hyphen -- so a real data-home path came out cut in the
        middle of a directory name, and again right after a hyphen inside a component,
        turning the ``find <dir> -samefile <file>`` line into fragments that run as
        nothing. Found by rendering the section rather than by reading it, which is why it
        is pinned.
        """
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "snapshot-alias")

        cli_doctor._doctor_live_target_pointer([])

        out = capsys.readouterr().out
        # Every whitespace-separated token of the remedy must survive on ONE line.
        printed_tokens = set(out.split())
        for token in sandbox._live_target_multilink_detail(str(pointer), 2).split():
            assert token in printed_tokens, f"the wrap split {token!r} across lines"

    @_LINUX_ONLY
    def test_a_broken_probe_is_reported_as_unknown_not_as_a_verdict(
        self, monkeypatch, capsys
    ) -> None:
        """Doctor must not turn its own failure into a claim about the host: an
        unreadable data home is "could not check", never "spawns will be refused"."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            sandbox,
            "live_target_pointer_unfitness",
            lambda: (_ for _ in ()).throw(OSError("probe exploded")),
        )
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        out = capsys.readouterr().out
        assert "could not check" in out
        assert "REFUSED" not in out
        assert issues == []

    def test_a_platform_that_cannot_hit_the_refusal_stays_silent(self, monkeypatch, capsys) -> None:
        """The refusal is on the namespace launcher's path. A Seatbelt profile denies by
        path rule and needs no mount target, so naming it on macOS would promise a spawn
        outage that does not happen."""
        monkeypatch.setattr(sys, "platform", "darwin")
        called: list[None] = []
        monkeypatch.setattr(
            sandbox, "live_target_pointer_unfitness", lambda: called.append(None) or None
        )
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        assert capsys.readouterr().out == ""
        assert called == [], "the probe must not even run where the refusal cannot happen"
        assert issues == []

    def test_doctor_runs_the_check_right_after_the_sandbox_section(self) -> None:
        """Wired into the run, not merely defined. Pinned by ORDER too: an operator who
        just read the sandbox verdict is the one who needs to know a spawn will be
        refused for a reason that verdict cannot express."""
        source = inspect.getsource(cli_doctor._doctor)
        assert "_doctor_live_target_pointer(issues)" in source
        assert source.index("_doctor_sandbox(issues)") < source.index(
            "_doctor_live_target_pointer(issues)"
        )


class TestTheRefusalReachesTheChatCard:
    """The dashboard surface for an agent spawn, pinned at the two places it can break.

    The refusal escapes ``_run_chat``'s ACP handler chain (it is a ``RuntimeError``, not
    an ``AcpError``) and lands in its terminal ``except Exception`` handler, which appends
    ``str(exc)`` as the error row. Two things have to hold for the remedy to arrive, and
    each is a separate way the card could go quiet:

    * the handler must keep using the exception's own text -- a generic "agent failed to
      start" there is exactly the substitution the issue asked about;
    * the refusal must not classify as TRANSIENT, or the retry ladder absorbs it into
      "Connection lost -- retrying..." and the operator never sees a reason at all.
    """

    def test_the_terminal_handler_appends_the_exceptions_own_text(self) -> None:
        from kiro_crew.dashboard import chat_runner

        source = inspect.getsource(chat_runner._run_chat)
        terminal = source[source.rindex("except Exception as exc:") :]
        assert "_err_text, _ = redact_exfiltration_urls(str(exc))" in terminal
        assert 'slot.append("error", _err_text' in terminal

    def test_the_refusal_survives_the_cards_redaction_unchanged(self, crew_home) -> None:
        """The card runs the text through the exfiltration-URL and credential scrubbers
        before appending it. A remedy containing a path and a ``find`` invocation must
        come out the other side intact -- a scrubbed ``find`` line is a remedy the
        operator cannot run."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        detail = sandbox._live_target_multilink_detail(str(_pointer(crew_home)), 2)

        scrubbed, _ = redact_exfiltration_urls(detail)
        scrubbed, _ = redact_credentials(scrubbed)

        assert scrubbed == detail

    def test_the_refusal_is_not_retryable(self) -> None:
        """A transient verdict would put this in the "Connection lost -- retrying..."
        ladder: the card would show no reason, and the retries would fail identically
        because nothing about the host changed between them."""
        from kiro_crew.llm_helpers import acp_error_is_transient

        refusal = sandbox.SandboxCeilingUnsealable(
            sandbox._live_target_multilink_detail("/var/lib/kirocrew/live_target.json", 2)
        )

        assert acp_error_is_transient(refusal) is False


class TestTheRefusalReachesTheCli:
    """``kirocrew chat`` printed a stack trace for a host with one extra file link."""

    def test_the_refusal_prints_the_remedy_and_exits_nonzero(self, monkeypatch, capsys) -> None:
        detail = sandbox._live_target_multilink_detail("/var/lib/kirocrew/live_target.json", 2)

        def _boom(coro, *_args, **_kwargs):
            # Close it rather than drop it: an abandoned coroutine emits a
            # "never awaited" RuntimeWarning that would make this test dirty the run.
            coro.close()
            raise sandbox.SandboxCeilingUnsealable(detail)

        monkeypatch.setattr(cli_chat.asyncio, "run", _boom)

        with pytest.raises(SystemExit) as exit_info:
            cli_chat._run_chat("hello", None)

        assert exit_info.value.code == 1
        err = capsys.readouterr().err
        assert "-samefile" in err
        assert "kirocrew doctor" in err

    def test_a_sigint_still_exits_cleanly(self, monkeypatch, capsys) -> None:
        """Guard the guard: the new handler must not have displaced the Ctrl-C path."""

        def _interrupt(coro, *_args, **_kwargs):
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_chat.asyncio, "run", _interrupt)

        cli_chat._run_chat("hello", None)

        assert "Bye!" in capsys.readouterr().out


#: A payload with one of each shape ``safe_terminal_line`` has to swallow: an OSC string
#: that retitles the window (and can write the clipboard as OSC 52), a CSI that erases the
#: line the diagnosis is on, and the bare C0 controls that let a target forge extra output.
_CONTROL_PAYLOAD = "\x1b]0;pwned\x07\x1b[2K\x1b[31m\r\n\x00\x7f"

#: The same payload minus ``NUL``, which is the one byte a real symlink target cannot
#: carry -- ``os.symlink`` refuses it before the kernel sees it. Everything else here IS
#: reachable through the filesystem: ``ESC``, ``BEL``, ``CR``, ``LF`` and ``DEL`` are all
#: legal in a path component, so ``os.readlink`` hands them back verbatim. Kept as a
#: separate constant so the formatter tests still cover arbitrary strings while the
#: on-disk tests plant only what an attacker could really plant.
_LINKABLE_PAYLOAD = _CONTROL_PAYLOAD.replace("\x00", "")

#: What must never survive into a printed sentence. ``\t`` is deliberately absent:
#: ``safe_terminal_line`` keeps tabs, and a tab cannot move the cursor or address a device.
_FORBIDDEN = frozenset(chr(c) for c in [*range(0x00, 0x09), *range(0x0B, 0x20), 0x7F]) | {
    chr(c) for c in range(0x80, 0xA0)
}


def _detail_formatters() -> list[tuple[str, object]]:
    """Every shared refusal-sentence formatter, DERIVED from the module.

    Discovered by name rather than hand-listed so a FOURTH unfit shape added later is
    covered by the escaping tests below without anyone remembering to extend a literal
    list here. A hand-written enumeration is exactly how the next formatter would ship
    printing raw bytes while this file stayed green.
    """
    found = [
        (name, fn)
        for name, fn in vars(sandbox).items()
        if name.startswith("_live_target_") and name.endswith("_detail") and callable(fn)
    ]
    assert found, "no _live_target_*_detail formatters found -- the discovery broke"
    return sorted(found)


def _hostile_args(fn) -> list[object]:
    """Call *fn* with the payload in every string parameter, from its own signature."""
    args: list[object] = []
    for param in inspect.signature(fn).parameters.values():
        if param.annotation in ("int", int):
            args.append(2)
        else:
            args.append(f"/var/lib/kirocrew/{_CONTROL_PAYLOAD}live_target.json")
    return args


class TestAPlantedPointerCannotDriveTheOperatorsTerminal:
    """The refusal sentence names attacker-chosen bytes, so it must not carry their controls.

    A SYMLINK's target comes back from ``os.readlink`` as arbitrary bytes, and the party
    that plants it is the sandboxed agent this pointer-masking exists to contain. Both
    readers print the sentence verbatim -- doctor through ``_print_wrapped`` and
    ``kirocrew chat`` through ``❌ {exc}`` -- and an operator running ``kirocrew doctor``
    after "agents stopped starting" is the expected trigger, so an OSC/CSI payload would
    reach a terminal on the normal path and act the instant it is emitted.

    Defused where the sentence is BUILT, so a surface added later inherits it.
    """

    @pytest.mark.parametrize("name_and_formatter", _detail_formatters(), ids=lambda p: p[0])
    def test_no_formatter_passes_control_bytes_through(self, name_and_formatter) -> None:
        name, formatter = name_and_formatter

        sentence = formatter(*_hostile_args(formatter))

        leaked = sorted(_FORBIDDEN & set(sentence))
        assert not leaked, f"{name} leaked {[hex(ord(c)) for c in leaked]} to the terminal"

    @pytest.mark.parametrize("name_and_formatter", _detail_formatters(), ids=lambda p: p[0])
    def test_every_formatter_still_names_the_path(self, name_and_formatter) -> None:
        """Stripping controls must not strip the diagnosis: over-escaping is its own bug."""
        _name, formatter = name_and_formatter

        sentence = formatter(*_hostile_args(formatter))

        assert "live_target.json" in sentence
        assert "/var/lib/kirocrew/" in sentence

    def test_an_ordinary_path_is_left_byte_identical(self) -> None:
        """The remedy is meant to be COPIED, which is why this is not ``repr``.

        ``repr`` would quote the whole value and escape its separators -- the same
        unusable-fragment outcome ``_print_wrapped`` avoids by never splitting a token. A real path
        holds no control bytes, so defusing must be a no-op on it.
        """
        target = "/var/lib/kirocrew/live_target.json"

        sentence = sandbox._live_target_multilink_detail(target, 2)

        assert f"find /var/lib/kirocrew -samefile {target}" in sentence

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_doctor_prints_no_control_bytes_for_a_hostile_symlink(
        self, crew_home, tmp_path, capsys
    ) -> None:
        """End to end, through the real reader: plant it, render it, read the bytes."""
        elsewhere = tmp_path / f"{_LINKABLE_PAYLOAD}evil.json"
        _pointer(crew_home).symlink_to(elsewhere)

        cli_doctor._doctor_live_target_pointer([])

        printed = capsys.readouterr()
        assert "SYMLINK" in printed.out
        leaked = sorted(_FORBIDDEN & set(printed.out + printed.err))
        assert not leaked, f"doctor emitted {[hex(ord(c)) for c in leaked]}"

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_the_spawn_refusal_carries_no_control_bytes_either(self, crew_home, tmp_path) -> None:
        """The other sink: ``cli_chat`` prints this exception's text verbatim to stderr."""
        elsewhere = tmp_path / f"{_LINKABLE_PAYLOAD}evil.json"
        _pointer(crew_home).symlink_to(elsewhere)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as refusal:
            sandbox._refuse_if_live_target_symlink(str(_pointer(crew_home)))

        leaked = sorted(_FORBIDDEN & set(str(refusal.value)))
        assert not leaked, f"the refusal leaked {[hex(ord(c)) for c in leaked]}"


class TestAPointerItCannotReadIsUnknownNotHealthy:
    """``None`` from the probe means FIT, so an unreadable pointer must not return it.

    Swallowing the ``lstat`` failure made doctor print nothing at all for a pointer whose
    shape it never determined, which an operator reads as "checked, healthy" while every
    Linux spawn may still refuse on it. The honest answer is the one doctor's own
    "could not check" branch already renders.
    """

    @_LINUX_ONLY
    def test_an_unreadable_pointer_propagates_instead_of_reading_as_fit(
        self, crew_home, monkeypatch
    ) -> None:
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        real_lstat = os.lstat

        def _deny(path, *args, **kwargs):
            if str(path) == str(pointer):
                raise PermissionError(13, "Permission denied")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(sandbox.os, "lstat", _deny)

        with pytest.raises(OSError):
            sandbox.live_target_pointer_unfitness()

    @_LINUX_ONLY
    def test_doctor_renders_that_as_could_not_check(self, crew_home, monkeypatch, capsys) -> None:
        """The raise is only correct because its one caller turns it into this."""
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        real_lstat = os.lstat

        def _deny(path, *args, **kwargs):
            if str(path) == str(pointer):
                raise PermissionError(13, "Permission denied")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(sandbox.os, "lstat", _deny)
        monkeypatch.setattr(sys, "platform", "linux")
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        out = capsys.readouterr().out
        assert "could not check" in out
        assert "REFUSED" not in out
        assert issues == []

    @_LINUX_ONLY
    def test_an_absent_pointer_is_still_fit(self, crew_home) -> None:
        """Guard the guard: ``FileNotFoundError`` must stay the one fit failure to stat,
        because the materialiser publishes the absent-equivalent stub for it."""
        assert sandbox.live_target_pointer_unfitness() is None


class TestTheRemedySurvivesBeingPasted:
    """The remedy names a command, so it has to RUN, not merely read correctly.

    A data home holding a space makes `find /opt/my data -samefile ...` a two-directory
    search that answers a different question without erroring -- the worst shape for a
    diagnostic, because it looks like it succeeded.
    """

    def test_a_path_with_a_space_is_shell_quoted(self) -> None:
        target = "/opt/my data/kirocrew/live_target.json"

        sentence = sandbox._live_target_multilink_detail(target, 2)

        assert (
            "find '/opt/my data/kirocrew' -samefile "
            "'/opt/my data/kirocrew/live_target.json'" in sentence
        )

    def test_an_ordinary_path_is_not_quoted(self) -> None:
        """Quoting every path would make the common case uglier for no gain."""
        sentence = sandbox._live_target_multilink_detail("/var/lib/kirocrew/lt.json", 2)

        assert "find /var/lib/kirocrew -samefile /var/lib/kirocrew/lt.json" in sentence
        assert "'" not in sentence


@_POSIX_ONLY
class TestTheSiblingRefusalsAreDefusedToo:
    """The new CLI print takes ANY ``SandboxCeilingUnsealable`` verbatim, not just ours.

    ``cli_chat._run_chat`` prints the exception's own text, so every builder of that
    exception is now a terminal sink. Three siblings interpolate ``os.readlink`` output the
    same way the live-target formatter did, and leaving them raw would make this change's
    stated rule ("defused where the sentence is BUILT") true only of the sentences it
    happens to own.
    """

    def test_the_dangling_ceiling_refusal_is_defused(self, tmp_path) -> None:
        link = tmp_path / "ceiling"
        link.symlink_to(tmp_path / f"{_LINKABLE_PAYLOAD}gone")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as refusal:
            sandbox._refuse_if_dangling_symlink(str(link))

        leaked = sorted(_FORBIDDEN & set(str(refusal.value)))
        assert not leaked, f"dangling-ceiling refusal leaked {[hex(ord(c)) for c in leaked]}"

    def test_the_masked_directory_refusal_is_defused(self, tmp_path) -> None:
        real = tmp_path / f"{_LINKABLE_PAYLOAD}real"
        real.mkdir()
        link = tmp_path / "leaf"
        link.symlink_to(real)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as refusal:
            sandbox._refuse_if_symlink_leaf(str(link))

        leaked = sorted(_FORBIDDEN & set(str(refusal.value)))
        assert not leaked, f"masked-directory refusal leaked {[hex(ord(c)) for c in leaked]}"

    def test_the_create_race_refusal_is_defused(self, tmp_path) -> None:
        """The third site, which the review did not name -- found by grepping for all of
        them rather than trusting the count."""
        real = tmp_path / f"{_LINKABLE_PAYLOAD}raced"
        real.mkdir()
        link = tmp_path / "staging"
        link.symlink_to(real)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as refusal:
            sandbox._require_real_dir_nofollow(str(link))

        leaked = sorted(_FORBIDDEN & set(str(refusal.value)))
        assert not leaked, f"create-race refusal leaked {[hex(ord(c)) for c in leaked]}"


class TestAnUnconfinedHostIsNotToldItIsBroken:
    """An outage claim is false when nothing wraps a spawn.

    ``wrap_argv`` returns before ``_materialize_live_target_mask_target`` for an effective
    mode of "off", so the launcher path the refusal lives on is never reached. Claiming an
    outage there is the same false promise this section already refuses to make on macOS;
    the mode is the second axis of that one rule.
    """

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_an_off_host_is_warned_about_a_latent_outage_not_a_current_one(
        self, crew_home, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False)
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "snapshot-alias")
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        out = capsys.readouterr().out
        assert "REFUSED" not in out
        # The condition and its remedy are still reported -- it is latent, not absent.
        assert str(pointer) in out
        assert "-samefile" in out
        # And it does not fail the run, because nothing is broken yet.
        assert issues == []

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_a_confined_host_still_gets_the_outage_wording(
        self, crew_home, monkeypatch, capsys
    ) -> None:
        """Guard the guard: the predicate must not silence the real case."""
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: True)
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "snapshot-alias")
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        assert "REFUSED" in capsys.readouterr().out
        assert issues == ["live-target pointer"]

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_an_unreadable_mode_reports_the_outage_rather_than_hiding_it(
        self, crew_home, monkeypatch, capsys
    ) -> None:
        """Fail towards reporting: a mode this process cannot read must not become a
        reason to downgrade a real refusal to a warning."""
        monkeypatch.setattr(
            sandbox,
            "credential_mask_applies",
            lambda mode: (_ for _ in ()).throw(OSError("config unreadable")),
        )
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "snapshot-alias")
        issues: list[str] = []

        cli_doctor._doctor_live_target_pointer(issues)

        assert "REFUSED" in capsys.readouterr().out
        assert issues == ["live-target pointer"]


class TestTheSinkDefusesBuildersItDoesNotOwn:
    """The last line of defence, and the duplication with the formatters is deliberate.

    ``cli_chat._run_chat`` prints ANY ``SandboxCeilingUnsealable`` verbatim, including ones
    raised in code it does not own. The md-notebook staging sweep is one: its refusal
    embeds a filename read from ``os.listdir`` of agent-writable state, where every byte
    but "/" and NUL is a legal filename, so an agent that plants a control-bearing
    ``.tmp`` and makes the unlink fail reaches the operator's terminal through it.

    Defusing at each builder keeps a new SINK safe. Defusing at the sink keeps a new
    BUILDER safe. Only both cover both, which is why this is not redundant with the
    formatter tests above.
    """

    def test_a_refusal_from_any_builder_is_defused_at_the_print(self, monkeypatch, capsys) -> None:
        hostile = f"cannot remove the legacy md-notebook staging temp /x/{_LINKABLE_PAYLOAD}a.tmp"

        def _refuse(*_args, **_kwargs):
            raise sandbox.SandboxCeilingUnsealable(hostile)

        monkeypatch.setattr(cli_chat.asyncio, "run", _refuse)

        with pytest.raises(SystemExit):
            cli_chat._run_chat("hello", None)

        err = capsys.readouterr().err
        leaked = sorted(_FORBIDDEN & set(err))
        assert not leaked, f"the sink emitted {[hex(ord(c)) for c in leaked]}"
        # Defused, not swallowed: the operator still gets the diagnosis and the path.
        assert "md-notebook staging temp" in err
        assert "a.tmp" in err

    def test_an_ordinary_refusal_reaches_the_terminal_unchanged(self, monkeypatch, capsys) -> None:
        """The escape must be invisible on every real message, or it is a new defect."""
        plain = "cannot mask /var/lib/kirocrew/live_target.json: the live-target pointer"

        def _refuse(*_args, **_kwargs):
            raise sandbox.SandboxCeilingUnsealable(plain)

        monkeypatch.setattr(cli_chat.asyncio, "run", _refuse)

        with pytest.raises(SystemExit):
            cli_chat._run_chat("hello", None)

        assert plain in capsys.readouterr().err


class TestTheRemedyAdmitsWhereItLooked:
    """A search of the wrong subtree reads as proof there is no second link.

    The tools that leave a hard link on the pointer -- snapshot and backup runs, a dotfile
    manager -- keep their copy OUTSIDE the data home, so a `find` rooted there usually
    reports only the pointer itself. An operator reasonably reads that as "no other name
    exists" and stops, which is worse than being given no command at all.
    """

    def test_the_remedy_names_its_scope_and_how_to_widen_it(self) -> None:
        sentence = sandbox._live_target_multilink_detail("/var/lib/kirocrew/lt.json", 2)

        assert "searches the data home only" in sentence
        # The widening is actionable, not a caveat: it names the flag and the search root.
        assert "-xdev" in sentence
        assert "mount point" in sentence

    def test_the_remedy_says_why_the_filesystem_is_the_bound(self) -> None:
        """Without this an operator would widen to `/` and wait for a pointless scan."""
        sentence = sandbox._live_target_multilink_detail("/var/lib/kirocrew/lt.json", 2)

        assert "cannot cross a filesystem" in sentence


class TestTheUnconfinedLineClaimsNothingItCannotKnow:
    """``credential_mask_applies`` answers False for two hosts, not one.

    One hands the command over unwrapped; the other has no backend and may be refusing
    every spawn for its own reason. A line reading "nothing is refused right now" is true
    of the first and false of the second, so this section says only what the predicate
    actually establishes -- that THIS pointer is not the thing stopping a spawn -- and
    sends the operator to the section that does answer it.
    """

    @_LINUX_ONLY
    @_POSIX_ONLY
    def test_it_does_not_claim_spawns_are_working(self, crew_home, monkeypatch, capsys) -> None:
        monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: False)
        pointer = _pointer(crew_home)
        pointer.write_text('{"checkout": "/real"}\n', encoding="utf-8")
        os.link(pointer, crew_home / "snapshot-alias")

        cli_doctor._doctor_live_target_pointer([])

        out = capsys.readouterr().out
        assert "Nothing is refused" not in out
        assert "not what stops a spawn" in out
        # And it names where the real answer lives, rather than guessing at it.
        assert "Sandbox section" in out
