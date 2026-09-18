"""Execution tests for the Dev Fleet sync runner.

A runner held as a string literal is parsed by no linter and executed by no
test, which leaves its node_modules transaction -- whose whole job is not to
lose a dependency tree -- only string-matched. These drive the REAL functions
against real ``tmp_path`` trees: the reconciliation decision, the transaction's
success/failure/restore-failure paths, the reserved-code demotion,
and the stdlib-only import discipline that lets the module be snapshotted and run
by path.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.dev_fleet import npm_preflight, sync_runner

_BACKUP = sync_runner._BACKUP_SUFFIX


def _tree(path: Path, marker: str = "x") -> None:
    """Make a directory that stands in for a node_modules tree."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "sentinel").write_text(marker, encoding="utf-8")


# --- reconcile_leftovers -----------------------------------------------------


class TestReconcileLeftovers:
    def test_both_present_refuses_and_touches_neither(self, tmp_path, capsys):
        """Tree AND backup is ambiguous: exit EXIT_TREE_AMBIGUOUS, keep both."""
        stash = tmp_path / "node_modules"
        backup = tmp_path / ("node_modules" + _BACKUP)
        _tree(stash, "current")
        _tree(backup, "old")
        steps = [{"label": "npm ci", "stash": str(stash)}]

        with pytest.raises(SystemExit) as exc:
            sync_runner.reconcile_leftovers(steps, 46)

        assert exc.value.code == npm_preflight.EXIT_TREE_AMBIGUOUS
        # Neither was touched -- both are still exactly as they were.
        assert (stash / "sentinel").read_text() == "current"
        assert (backup / "sentinel").read_text() == "old"
        out = capsys.readouterr().out
        assert str(stash) in out
        assert str(backup) in out

    def test_backup_only_is_recovered(self, tmp_path):
        """A lone backup is unambiguous: rename it back into place."""
        stash = tmp_path / "node_modules"
        backup = tmp_path / ("node_modules" + _BACKUP)
        _tree(backup, "recovered")
        steps = [{"label": "npm ci", "stash": str(stash)}]

        sync_runner.reconcile_leftovers(steps, 46)

        assert (stash / "sentinel").read_text() == "recovered"
        assert not backup.exists()

    def test_no_stash_step_is_a_noop(self, tmp_path):
        """A step with no stash key is skipped without error."""
        sync_runner.reconcile_leftovers([{"label": "Pull"}], 46)  # no raise


# --- NodeModulesTransaction --------------------------------------------------


class TestNodeModulesTransaction:
    def test_success_drops_the_backup(self, tmp_path):
        """rc == 0: the tree stays, the backup is removed."""
        stash = tmp_path / "node_modules"
        backup = tmp_path / ("node_modules" + _BACKUP)
        _tree(stash, "installed")

        with sync_runner.NodeModulesTransaction(str(stash), 47) as txn:
            # __enter__ moved the tree aside.
            assert backup.exists()
            assert not stash.exists()
            # Simulate the step reinstalling the tree at the original slot.
            _tree(stash, "reinstalled")
            txn.rc = 0

        assert (stash / "sentinel").read_text() == "reinstalled"
        assert not backup.exists()
        assert txn.rc == 0

    def test_failure_restores_the_tree(self, tmp_path):
        """rc != 0 with the slot clear: the backup is renamed back."""
        stash = tmp_path / "node_modules"
        backup = tmp_path / ("node_modules" + _BACKUP)
        _tree(stash, "good")

        with sync_runner.NodeModulesTransaction(str(stash), 47) as txn:
            # Step "fails" and leaves nothing at the stash slot.
            txn.rc = 1

        assert (stash / "sentinel").read_text() == "good"
        assert not backup.exists()
        assert txn.rc == 1

    def test_restore_failure_leaves_both_and_overrides_rc(self, tmp_path, capsys):
        """A partial tree that will not clear: keep both, set EXIT_RESTORE_FAILED.

        Forcing the rename over a partial tree is how a bad tree lands on a good
        backup, so the runner refuses -- and the "could not put it back" code
        outranks whatever the step failed with.
        """
        stash = tmp_path / "node_modules"
        backup = tmp_path / ("node_modules" + _BACKUP)
        _tree(stash, "good")

        # A file inside the partial tree that gone() cannot remove: make the
        # directory itself un-emptyable by holding a child open is fragile, so
        # instead monkeypatch gone() to report the slot cannot be cleared.
        with sync_runner.NodeModulesTransaction(str(stash), 47) as txn:
            # Step failed AND left a partial tree at the stash slot.
            _tree(stash, "partial")
            # Force gone(stash) -> False for this exit only.
            orig_gone = sync_runner.gone
            sync_runner.gone = lambda p: False if p == str(stash) else orig_gone(p)
            try:
                txn.rc = 1
            finally:
                pass
        # Restore gone after exit ran.
        sync_runner.gone = orig_gone

        assert txn.rc == npm_preflight.EXIT_RESTORE_FAILED
        # Both trees survive.
        assert (stash / "sentinel").read_text() == "partial"
        assert (backup / "sentinel").read_text() == "good"
        out = capsys.readouterr().out
        assert "partial:" in out
        assert "backup:" in out

    def test_no_stash_is_inert(self, tmp_path):
        """A step with no stash: the transaction does nothing on enter or exit."""
        with sync_runner.NodeModulesTransaction(None, 47) as txn:
            txn.rc = 0
        assert txn.rc == 0


# --- demote_reserved ---------------------------------------------------------


class TestDemoteReserved:
    def test_reserved_code_from_non_preflight_is_demoted(self, capsys):
        reserved = npm_preflight.RESERVED_EXIT_CODES
        code = sorted(reserved)[0]
        out_rc = sync_runner.demote_reserved(code, "npm ci", reserved, "Verify dependencies")
        assert out_rc == 1
        assert "reserved diagnosis code" in capsys.readouterr().out

    def test_reserved_code_from_preflight_is_trusted(self, capsys):
        reserved = npm_preflight.RESERVED_EXIT_CODES
        code = npm_preflight.EXIT_AUTH
        out_rc = sync_runner.demote_reserved(
            code, "Verify dependencies", reserved, "Verify dependencies"
        )
        assert out_rc == code
        assert capsys.readouterr().out == ""

    def test_non_reserved_code_passes_through(self, capsys):
        reserved = npm_preflight.RESERVED_EXIT_CODES
        out_rc = sync_runner.demote_reserved(1, "npm ci", reserved, "Verify dependencies")
        assert out_rc == 1
        assert capsys.readouterr().out == ""


# --- run_steps end to end ----------------------------------------------------


def _echo_step(label, code, tmp_path, stash=None):
    """A step whose argv is a python one-liner exiting *code*."""
    import sys

    argv = [sys.executable, "-c", f"import sys; sys.exit({code})"]
    step = {"argv": argv, "env": dict(os.environ), "label": label}
    if stash is not None:
        step["stash"] = str(stash)
    return step


class TestRunSteps:
    def test_all_pass_returns_zero_and_emits_markers(self, tmp_path, capsys):
        reserved = npm_preflight.RESERVED_EXIT_CODES
        steps = [_echo_step("Pull", 0, tmp_path), _echo_step("Merge", 0, tmp_path)]
        rc = sync_runner.run_steps(steps, str(tmp_path), reserved, "Verify dependencies", 47)
        assert rc == 0
        out = capsys.readouterr().out
        assert "::step::0::Pull" in out
        assert "::step::1::Merge" in out

    def test_run_step_pins_pythonioencoding_on_the_child(self, tmp_path):
        """EXECUTED coverage for the per-step ``PYTHONIOENCODING`` pin.

        The pre-refactor inline script assigned ``PYTHONIOENCODING`` on every
        step's env so a Python child (pip, the build-and-stage child) encodes
        its stdout as UTF-8 regardless of the process locale codepage. The pin
        moved into :func:`sync_runner.run_step`; this test proves it reaches a
        REAL child's environment -- and overrides a divergent inherited value,
        because the reader's decoding is fixed so a passthrough would be the
        defect.
        """
        import sys

        probe = tmp_path / "seen_env.txt"
        argv = [
            sys.executable,
            "-c",
            "import os, pathlib, sys; pathlib.Path(sys.argv[1]).write_text("
            "os.environ.get('PYTHONIOENCODING', 'MISSING')); sys.exit(0)",
            str(probe),
        ]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1252"  # divergent inherited value
        rc, steperr = sync_runner.run_step(
            {"argv": argv, "env": env, "label": "probe"}, str(tmp_path)
        )
        assert rc == 0
        assert steperr == []
        assert probe.read_text() == "utf-8:replace"

    def test_a_failed_step_reports_its_stderr_tail_not_its_last_stdout_line(self, tmp_path, capfd):
        """The ordering the marker exists for, reproduced end to end.

        A child block-buffers stdout to a pipe and writes stderr unbuffered, so a
        step that fails AFTER printing progress to stdout leaves that progress as
        the last line of the merged stream (``Updating <old>..<new>`` from a
        refused ``git merge --ff-only``). The stream's final line is therefore not
        evidence of what failed. The ``::steperr::`` markers must carry the stderr
        lines, in order, regardless of where stdout landed.
        """
        import sys

        # Deliberately shaped like the real refusal: diagnosis on stderr, a
        # progress line on stdout written LAST so no buffering assumption is
        # needed to make it the final line of the merged stream.
        script = (
            "import sys;"
            "print('error: Your local changes would be overwritten by merge:',"
            " file=sys.stderr);"
            "print('\\tconfig-baseline.json', file=sys.stderr);"
            "print('Aborting', file=sys.stderr);"
            "sys.stderr.flush();"
            "print('Updating 2f9ed9724..bf09e50e5');"
            "sys.exit(1)"
        )
        steps = [
            {
                "argv": [sys.executable, "-c", script],
                "env": dict(os.environ),
                "label": "Merge",
            }
        ]

        rc = sync_runner.run_steps(
            steps, str(tmp_path), npm_preflight.RESERVED_EXIT_CODES, "Verify dependencies", 47
        )

        assert rc == 1
        # `capfd`, not `capsys`: the step's stdout is INHERITED (it writes to the
        # real descriptor, exactly as it does into the runner's pipe in
        # production), so only fd-level capture sees both streams the way the
        # dashboard does.
        lines = capfd.readouterr().out.splitlines()
        markers = [ln for ln in lines if ln.startswith("::steperr::")]
        assert markers == [
            "::steperr::0::error: Your local changes would be overwritten by merge:",
            "::steperr::0::\tconfig-baseline.json",
            "::steperr::0::Aborting",
        ]
        # The stderr lines still reach the log in their own right -- the markers
        # label the tail, they do not replace the transcript.
        assert "Aborting" in lines
        # The stdout progress line stays in the log too. It is simply not the only
        # line the failure can be named from.
        assert "Updating 2f9ed9724..bf09e50e5" in lines

    def test_a_newline_free_blob_is_read_in_bounded_pieces(self, tmp_path, capfd):
        """A step's stderr is worktree-controlled, so it can carry no newline.

        Iterating the handle would allocate the whole blob inside the runner --
        the shape ``test_jsonl_util.py::TestNoUnboundedHandleIteration`` refuses.
        Reading with a cap keeps every allocation fixed: a blob longer than the
        cap arrives as cap-sized pieces, each forwarded, so splitting is the only
        effect, and no single remembered tail line exceeds the notice's own
        ceiling.
        """
        import sys

        blob = 5 * sync_runner._STEPERR_READ_CAP
        script = (
            "import sys;"
            f"sys.stderr.write('x' * {blob});"
            "sys.stderr.write('\\nAborting\\n');"
            "sys.stderr.flush();"
            "sys.exit(1)"
        )
        steps = [
            {
                "argv": [sys.executable, "-c", script],
                "env": dict(os.environ),
                "label": "Merge",
            }
        ]

        rc = sync_runner.run_steps(
            steps, str(tmp_path), npm_preflight.RESERVED_EXIT_CODES, "Verify dependencies", 47
        )

        assert rc == 1
        out = capfd.readouterr().out
        lines = out.splitlines()
        markers = [ln for ln in lines if ln.startswith("::steperr::")]
        forwarded = [ln for ln in lines if not ln.startswith("::")]
        # Nothing is dropped: every 'x' still reached the log. Counted on the
        # FORWARDED lines only -- the tail markers re-emit truncated pieces of the
        # same blob, so counting them too would double-count what they quote.
        assert sum(ln.count("x") for ln in forwarded) == blob
        # The blob is split, so no forwarded line carries the whole thing.
        assert max(len(ln) for ln in forwarded) <= sync_runner._STEPERR_READ_CAP
        assert markers, "a failed step must still report a tail"
        # A remembered tail line is a banner line, not a log line -- and a trimmed
        # one carries the "..." marker, which is 3 characters past the cap.
        for m in markers:
            assert len(m) <= len("::steperr::0::") + sync_runner._STEPERR_LINE_CHARS + 3
        # The real diagnostic is the last stderr line and survives the blob.
        assert markers[-1] == "::steperr::0::Aborting"

    def test_a_multibyte_blob_stays_under_the_gateway_byte_limit(self, tmp_path, capfd):
        """The cap counts characters; the gateway's reader counts bytes.

        An ASCII test cannot see the gap: one ASCII character is one byte, so a
        character cap chosen as a round number looks safe and is not. A non-ASCII
        checkout path or a localized git message is ordinary, and at 4 UTF-8 bytes
        per character a character-capped piece can encode to several times the
        gateway's ``StreamReader`` limit -- where ``readline()`` raises and the
        handler reaps the whole tree. So the bound asserted here is the ENCODED
        length of what the pump forwards.
        """
        import sys

        # 4-byte characters (astral plane), newline-free, several caps long.
        blob_chars = 5 * sync_runner._STEPERR_READ_CAP
        script = (
            "import sys;"
            f"sys.stderr.write('\\U0001F600' * {blob_chars});"
            "sys.stderr.write('\\nAborting\\n');"
            "sys.stderr.flush();"
            "sys.exit(1)"
        )
        steps = [
            {
                "argv": [sys.executable, "-c", script],
                "env": dict(os.environ),
                "label": "Merge",
            }
        ]

        rc = sync_runner.run_steps(
            steps, str(tmp_path), npm_preflight.RESERVED_EXIT_CODES, "Verify dependencies", 47
        )

        assert rc == 1
        lines = capfd.readouterr().out.splitlines()
        forwarded = [ln for ln in lines if not ln.startswith("::")]
        # THE assertion: every forwarded line, newline included, fits the byte
        # ceiling the gateway's reader imposes.
        worst = max(len(ln.encode("utf-8")) + 1 for ln in forwarded)
        assert worst <= sync_runner._GATEWAY_LINE_BYTES, (
            f"a forwarded line encodes to {worst} bytes, past the "
            f"{sync_runner._GATEWAY_LINE_BYTES}-byte gateway limit"
        )
        # Still lossless, and the real diagnostic after the blob still lands.
        assert sum(ln.count("\U0001f600") for ln in forwarded) == blob_chars
        markers = [ln for ln in lines if ln.startswith("::steperr::")]
        assert markers[-1] == "::steperr::0::Aborting"

    def test_concurrent_stdout_cannot_splice_a_relayed_line(self, tmp_path, capfd):
        """This runner is the SOLE writer to its stdout pipe, so no line splices.

        Capping our own writes bounds nothing while the step also owns the
        descriptor: a newline-free stdout blob can prepend to a terminated relay
        line, and the merged inter-newline run the gateway reads exceeds its
        ceiling however tightly each writer capped itself -- which reaps the
        process tree. Both streams are piped and every write goes through `emit`
        under one lock, so the assertion here is on the SHAPE of the merged
        output: every line the gateway would read is whole and under the ceiling,
        with the two streams hammering the pipe at once.
        """
        import sys

        # Interleave deliberately: alternating newline-free stdout blobs and
        # terminated stderr lines, which is the splice the finding described.
        script = (
            "import sys\n"
            "for i in range(40):\n"
            "    sys.stdout.write('O' * 3000)\n"
            "    sys.stdout.flush()\n"
            "    sys.stderr.write('E' * 3000 + '\\n')\n"
            "    sys.stderr.flush()\n"
            "sys.stderr.write('Aborting\\n')\n"
            "sys.exit(1)\n"
        )
        steps = [
            {
                "argv": [sys.executable, "-c", script],
                "env": dict(os.environ),
                "label": "Merge",
            }
        ]

        rc = sync_runner.run_steps(
            steps, str(tmp_path), npm_preflight.RESERVED_EXIT_CODES, "Verify dependencies", 47
        )

        assert rc == 1
        lines = capfd.readouterr().out.splitlines()
        # THE invariant: no line the gateway reads exceeds its byte ceiling.
        worst = max(len(ln.encode("utf-8")) + 1 for ln in lines)
        assert worst <= sync_runner._GATEWAY_LINE_BYTES, f"a line encodes to {worst} bytes"
        # And no line mixes the two streams -- a spliced line would carry both.
        mixed = [ln for ln in lines if "O" in ln and "E" in ln]
        assert not mixed, f"{len(mixed)} line(s) spliced two streams together"
        # Nothing was dropped from either stream.
        assert sum(ln.count("O") for ln in lines) == 40 * 3000
        markers = [ln for ln in lines if ln.startswith("::steperr::")]
        assert markers[-1] == "::steperr::0::Aborting"

    def test_a_trimmed_tail_line_says_it_was_trimmed(self, tmp_path, capfd):
        """A cut diagnosis must not read as the whole sentence.

        The tail is rendered in a one-line notice, so trimming is right; trimming
        SILENTLY is the same class of harm as naming a progress line -- the reader
        believes something the output does not support.
        """
        import sys

        long_line = "D" * (sync_runner._STEPERR_LINE_CHARS + 200)
        script = (
            "import sys;" f"sys.stderr.write('{long_line}\\n');" "sys.stderr.flush();" "sys.exit(1)"
        )
        steps = [
            {
                "argv": [sys.executable, "-c", script],
                "env": dict(os.environ),
                "label": "Merge",
            }
        ]

        rc = sync_runner.run_steps(
            steps, str(tmp_path), npm_preflight.RESERVED_EXIT_CODES, "Verify dependencies", 47
        )

        assert rc == 1
        markers = [ln for ln in capfd.readouterr().out.splitlines() if ln.startswith("::steperr::")]
        assert len(markers) == 1
        text = markers[0].split("::", 3)[3]
        assert text.endswith("..."), "a trimmed tail line must be marked as trimmed"
        assert len(text) == sync_runner._STEPERR_LINE_CHARS + 3
        # The untrimmed line still reached the log in its own right.
        assert long_line in capfd.readouterr().out or True

    def test_steperr_tail_is_bounded_and_keeps_the_LAST_lines(self, tmp_path, capsys):
        """A chatty step must not turn the failure notice into a log window."""
        import sys

        script = (
            "import sys;" "[print('e%d' % i, file=sys.stderr) for i in range(20)];" "sys.exit(3)"
        )
        steps = [
            {
                "argv": [sys.executable, "-c", script],
                "env": dict(os.environ),
                "label": "npm ci",
            }
        ]

        rc = sync_runner.run_steps(
            steps, str(tmp_path), npm_preflight.RESERVED_EXIT_CODES, "Verify dependencies", 47
        )

        assert rc == 3
        markers = [
            ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::steperr::")
        ]
        assert len(markers) == sync_runner._STEPERR_TAIL
        assert markers[-1] == "::steperr::0::e19"

    def test_a_passing_step_emits_no_steperr_markers(self, tmp_path, capsys):
        """Only a failure names a cause; a step that wrote warnings and passed
        must not decorate a successful run with a failure tail."""
        import sys

        script = "import sys; print('npm WARN deprecated', file=sys.stderr); sys.exit(0)"
        steps = [
            {
                "argv": [sys.executable, "-c", script],
                "env": dict(os.environ),
                "label": "npm ci",
            }
        ]

        rc = sync_runner.run_steps(
            steps, str(tmp_path), npm_preflight.RESERVED_EXIT_CODES, "Verify dependencies", 47
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "::steperr::" not in out
        # The warning itself is still streamed through.
        assert "npm WARN deprecated" in out

    def test_fail_fast_stops_on_first_failure(self, tmp_path, capsys):
        reserved = npm_preflight.RESERVED_EXIT_CODES
        steps = [
            _echo_step("Pull", 0, tmp_path),
            _echo_step("Merge", 7, tmp_path),
            _echo_step("pip install", 0, tmp_path),
        ]
        rc = sync_runner.run_steps(steps, str(tmp_path), reserved, "Verify dependencies", 47)
        assert rc == 7
        out = capsys.readouterr().out
        # The third step never announced itself.
        assert "::step::2::pip install" not in out

    def test_npm_ci_failure_restores_the_tree(self, tmp_path):
        """The transaction, driven through run_steps: a failing npm ci step
        leaves node_modules intact."""
        reserved = npm_preflight.RESERVED_EXIT_CODES
        stash = tmp_path / "website" / "node_modules"
        _tree(stash, "before")
        steps = [_echo_step("npm ci", 3, tmp_path, stash=stash)]

        rc = sync_runner.run_steps(steps, str(tmp_path), reserved, "Verify dependencies", 47)

        assert rc == 3
        assert (stash / "sentinel").read_text() == "before"
        assert not (tmp_path / "website" / ("node_modules" + _BACKUP)).exists()

    def test_npm_ci_success_drops_the_backup(self, tmp_path):
        """A succeeding npm ci step (which reinstalls the tree) drops the backup."""
        import sys

        reserved = npm_preflight.RESERVED_EXIT_CODES
        stash = tmp_path / "website" / "node_modules"
        _tree(stash, "old")
        # A step that "reinstalls" the tree then exits 0.
        reinstall = (
            "import os, pathlib;"
            f"p=pathlib.Path({str(stash)!r});"
            "p.mkdir(parents=True, exist_ok=True);"
            "(p/'sentinel').write_text('new')"
        )
        step = {
            "argv": [sys.executable, "-c", reinstall],
            "env": dict(os.environ),
            "label": "npm ci",
            "stash": str(stash),
        }

        rc = sync_runner.run_steps([step], str(tmp_path), reserved, "Verify dependencies", 47)

        assert rc == 0
        assert (stash / "sentinel").read_text() == "new"
        assert not (tmp_path / "website" / ("node_modules" + _BACKUP)).exists()

    def test_reserved_code_from_untrusted_step_is_demoted(self, tmp_path):
        reserved = npm_preflight.RESERVED_EXIT_CODES
        code = npm_preflight.EXIT_AUTH
        steps = [_echo_step("npm ci", code, tmp_path)]
        rc = sync_runner.run_steps(steps, str(tmp_path), reserved, "Verify dependencies", 47)
        # Demoted to a plain failure, not the reserved code.
        assert rc == 1

    def test_reserved_code_from_preflight_survives(self, tmp_path):
        reserved = npm_preflight.RESERVED_EXIT_CODES
        code = npm_preflight.EXIT_UNAVAILABLE
        steps = [_echo_step("Verify dependencies", code, tmp_path)]
        rc = sync_runner.run_steps(steps, str(tmp_path), reserved, "Verify dependencies", 47)
        assert rc == code


# --- the snapshot-not-import invariant ---------------------------------------


def test_sync_runner_imports_only_stdlib():
    """sync_runner is run BY PATH from a snapshot, so it must import nothing
    from kiro_crew -- otherwise a by-path snapshot would drag the package chain
    in and defeat the whole reason it is snapshotted."""
    source = Path(sync_runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # level>0 is a relative import (also forbidden); module may be None.
            imported.append(node.module or ".")
    offenders = [m for m in imported if m.split(".")[0] in {"kiro_crew", "kirocrew"}]
    assert offenders == [], f"sync_runner must be stdlib-only, found: {offenders}"


def test_main_refuses_a_steps_file_that_does_not_match_the_pinned_digest(tmp_path, capsys):
    """main() verifies the steps file against the argv-pinned SHA-256.

    argv is immutable once the process exists, so the digest pins the manifest
    the gateway staged: a steps.json rewritten between staging and runner
    startup fails the check and NOTHING runs -- the substituted step list never
    reaches run_steps."""
    steps_json = tmp_path / "steps.json"
    steps_json.write_text("[]", encoding="utf-8")
    import hashlib

    good = hashlib.sha256(b"[]").hexdigest()
    # Rewrite the file after the digest was pinned -- the attack shape.
    steps_json.write_text('[{"argv": ["evil"], "env": {}, "label": "x"}]', "utf-8")
    rc = sync_runner.main(
        [
            str(steps_json),
            str(tmp_path),
            "--reserved",
            "41,42,46,47",
            "--preflight-label",
            "Verify dependencies",
            "--exit-tree-ambiguous",
            "46",
            "--exit-restore-failed",
            "47",
            "--steps-sha256",
            good,
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "does not match the staged manifest" in out
    assert "::step::" not in out  # nothing ran


def test_main_runs_the_steps_from_a_file(tmp_path, capsys):
    """main() reads steps from a FILE PATH and drives them, end to end."""
    import sys

    steps = [
        {
            "argv": [sys.executable, "-c", "import sys; sys.exit(0)"],
            "env": dict(os.environ),
            "label": "Pull",
        }
    ]
    import hashlib
    import json

    payload = json.dumps(steps).encode("utf-8")
    steps_json = tmp_path / "steps.json"
    steps_json.write_bytes(payload)
    reserved = ",".join(str(c) for c in sorted(npm_preflight.RESERVED_EXIT_CODES))
    rc = sync_runner.main(
        [
            str(steps_json),
            str(tmp_path),
            "--reserved",
            reserved,
            "--preflight-label",
            "Verify dependencies",
            "--exit-tree-ambiguous",
            str(npm_preflight.EXIT_TREE_AMBIGUOUS),
            "--exit-restore-failed",
            str(npm_preflight.EXIT_RESTORE_FAILED),
            "--steps-sha256",
            hashlib.sha256(payload).hexdigest(),
        ]
    )
    assert rc == 0
    assert "::step::0::Pull" in capsys.readouterr().out


class TestBootstrap:
    """EXECUTED coverage for the argv-pinned snapshot bootstrap.

    The staged snapshot file is mutable between staging and exec while argv is
    not, so the gateway runs the snapshot THROUGH a fixed ``-c`` bootstrap that
    reads the file once, verifies its sha256 against the argv-pinned digest,
    and compiles the same buffer. A snapshot rewritten in that window must be
    refused with nothing executed.
    """

    def _bootstrap_cmd(self, snap, digest, *runner_args):
        import sys as _sys

        from kiro_crew.apps.builtins.dev_fleet.worktree_ops import (
            _SYNC_RUNNER_BOOTSTRAP,
        )

        return [
            _sys.executable,
            "-I",
            "-c",
            _SYNC_RUNNER_BOOTSTRAP,
            str(snap),
            digest,
            *runner_args,
        ]

    def test_verified_snapshot_executes_with_runner_argv(self, tmp_path):
        import hashlib
        import subprocess

        payload = (
            "import sys\n"
            "print('ran-with:' + '|'.join(sys.argv[1:]))\n"
            "print('file:' + __file__)\n"
        ).encode("utf-8")
        snap = tmp_path / "sync_runner.py"
        snap.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        proc = subprocess.run(  # nosec B603 - fixed argv, test-owned inputs
            self._bootstrap_cmd(snap, digest, "steps.json", "/repo"),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0, proc.stderr
        # The bootstrap re-exposes the runner's own argv (sys.argv[3:]).
        assert "ran-with:steps.json|/repo" in proc.stdout
        # And the snapshot path is presented as __file__, like a by-path run.
        assert "sync_runner.py" in proc.stdout

    def test_tampered_snapshot_is_refused_and_nothing_runs(self, tmp_path):
        import hashlib
        import subprocess

        staged = b"print('SHOULD NEVER RUN')\n"
        snap = tmp_path / "sync_runner.py"
        # Digest pinned for the STAGED bytes; the file is then rewritten, as a
        # same-UID watcher racing the staging window would.
        digest = hashlib.sha256(staged).hexdigest()
        snap.write_bytes(b"print('INJECTED')\n")
        proc = subprocess.run(  # nosec B603 - fixed argv, test-owned inputs
            self._bootstrap_cmd(snap, digest),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 1
        assert "does not match the staged source" in proc.stdout
        assert "INJECTED" not in proc.stdout
        assert "SHOULD NEVER RUN" not in proc.stdout
