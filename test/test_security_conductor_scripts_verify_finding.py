"""The security conductor's ``verify_finding`` script -- the properties, not the plumbing.

The verifier runs a finding's proof of concept in a scratch git worktree and
records the verdict the rules of engagement allow: a failing proof confirms, a
passing one rejects, an erroring or overlong one needs a human, and a proof the
rules forbid (egress, an escape from the worktree) is refused unrun. The section
also holds the filing tests that read the auditor and verifier verdict rows, and
the pins that span all three scripts (stdlib only, no shell-out but the proof, one
ledger load per invocation, the proof shapes declared once). Every script is driven
through ``main`` with argv, the way the skill invokes it. The ``scope_check`` and
``finding_entry`` sections are in ``test_security_conductor_scripts.py``; the shared
fixtures and builders are in ``security_conductor_scripts_helpers.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from security_conductor_scripts_helpers import db_fixture  # noqa: F401
from security_conductor_scripts_helpers import finding_entry_fixture  # noqa: F401
from security_conductor_scripts_helpers import ledger_fixture  # noqa: F401
from security_conductor_scripts_helpers import missing_json_fixture  # noqa: F401
from security_conductor_scripts_helpers import scope_check_fixture  # noqa: F401
from security_conductor_scripts_helpers import SCRIPTS, file_finding, out_json, seed_roe
from skill_script_helpers import load_skill_script

from kiro_crew import platform_compat


@pytest.fixture
def runnable_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the bare name ``python3`` in a PoC reach a REAL interpreter.

    Every test here that lets a ``cmd::`` proof actually run needs this, and the
    cost of not having it is measured: four runs of this file left NINE processes
    reparented to init, spinning at 1464% CPU between them for six days, each with
    a deleted ``pytest-of-*/garbage-*/scratch-checkout`` cwd.

    The cause is the interaction of two deliberate decisions in
    ``verify_finding.child_env``: PATH is inherited (a PoC needs an interpreter)
    while HOME is repointed at the throwaway worktree (the blast-radius bound). On
    a host whose PATH leads with a version-manager shim directory -- mise, asdf,
    pyenv, volta, nodenv -- the bare name IS the manager, and a manager that
    cannot find its tool state under the substituted HOME never execs an
    interpreter at all; it spins. Verified on such a host: with the shim dir first
    and HOME repointed, ``python3 -c "raise SystemExit(3)"`` had to be SIGKILLed
    at 20s instead of exiting 3 in milliseconds.

    So the interpreter's own directory goes FIRST. It cannot be spelled in the PoC
    instead: ``refusal_reason`` refuses a proof whose argv names an absolute path
    outside the worktree, which is correct and stays. And the tests that are about
    the PARSER deliberately do NOT take this fixture -- there ``python3`` is the
    input under test and nothing is spawned.

    Patched through ``monkeypatch`` on ``os.environ`` because ``child_env`` reads
    PATH at call time, so the pin has to be live during ``main()`` and gone after.
    """
    interpreter_dir = str(Path(sys.executable).resolve().parent)
    monkeypatch.setenv("PATH", os.pathsep.join([interpreter_dir, os.environ.get("PATH", "")]))


@pytest.fixture
def verify_finding():
    return load_skill_script("security_conductor_verify_finding", SCRIPTS / "verify_finding.py")


# --------------------------------------------------------------------------- #
# verify_finding
# --------------------------------------------------------------------------- #


FAILING_TEST = "def test_the_guard_admits_what_it_must_refuse():\n    assert False\n"
PASSING_TEST = "def test_the_guard_refuses_it():\n    assert True\n"
BROKEN_TEST = "import no_such_module_anywhere\n\n\ndef test_x():\n    assert False\n"


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A real git checkout, because that is what the verifier brief asks for."""
    root = tmp_path / "scratch-checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    return root


def a_finding(ledger, db: Path, *, poc: str, title: str = "the guard admits a bad input") -> int:
    conn = ledger.connect(db)
    try:
        ledger.init_schema(conn)
        finding_id, _ = ledger.add_finding(
            conn,
            surface="security.is_denied",
            title=title,
            severity="High",
            paths=["src/kiro_crew/security.py"],
            poc=poc,
            round_id="r1",
        )
    finally:
        conn.close()
    return finding_id


def verdict_rows(ledger, db: Path, finding_id: int) -> list[tuple[str, str]]:
    conn = ledger.connect(db)
    try:
        rows = conn.execute(
            "SELECT role, verdict FROM verdicts WHERE finding_id = ? ORDER BY rowid ASC",
            (finding_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(str(row["role"]), str(row["verdict"])) for row in rows]


def test_a_failing_pytest_proof_confirms_the_finding(ledger, verify_finding, db, worktree, capsys):
    (worktree / "test_poc.py").write_text(FAILING_TEST, encoding="utf-8")
    finding_id = a_finding(
        ledger, db, poc="pytest::test_poc.py::test_the_guard_admits_what_it_must_refuse"
    )

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 0
    assert payload["verdict"] == "confirmed"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "confirmed")]


def test_a_passing_pytest_proof_rejects_the_finding(ledger, verify_finding, db, worktree, capsys):
    (worktree / "test_poc.py").write_text(PASSING_TEST, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="pytest::test_poc.py::test_the_guard_refuses_it")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 10
    assert payload["verdict"] == "rejected"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "rejected")]


def test_a_proof_that_errors_rather_than_fails_is_needs_human(
    ledger, verify_finding, db, worktree, capsys
):
    """pytest exits nonzero for a collection error too.

    Reading the exit status alone would confirm a finding whose proof never
    executed -- the exact false positive this pass exists to catch.
    """
    (worktree / "test_poc.py").write_text(BROKEN_TEST, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="pytest::test_poc.py::test_x")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.parametrize(
    ("platform", "active_python"),
    (
        ("win32", r"C:\runtime\.venv\Scripts\python.exe"),
        ("linux", "/runtime/.venv/bin/python"),
        ("darwin", "/runtime/.venv/bin/python"),
    ),
)
def test_the_canonical_python3_command_resolves_on_every_platform(
    verify_finding, monkeypatch, platform, active_python
):
    """PATH resolution of ``python3`` is untrustworthy on all three platforms.

    Windows can reach the Store app-execution alias; POSIX can reach a
    version-manager shim that loops under the redirected ``HOME``. So the
    rewrite is not gated on ``sys.platform`` at all -- pinning that here is what
    keeps the gate from coming back for one platform and leaving the others.

    Resolution stays deliberately narrower than finding a convenient Python: the
    documented canonical token means "this verifier's Python", while every other
    executable name remains untrusted finding input for ``Popen`` to resolve
    under the existing child environment.
    """
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "executable", active_python)

    canonical = verify_finding.poc_argv('cmd::python3 -c "raise SystemExit(3)"')
    foreign = verify_finding.poc_argv('cmd::python3.12 -c "raise SystemExit(3)"')
    bare = verify_finding.poc_argv("cmd::python -c pass")

    assert canonical == ([active_python, "-c", "raise SystemExit(3)"], "cmd")
    assert foreign == (["python3.12", "-c", "raise SystemExit(3)"], "cmd")
    assert bare == (["python", "-c", "pass"], "cmd")


def test_a_nonzero_command_proof_confirms_and_a_zero_one_rejects(
    ledger, verify_finding, db, worktree, capsys, runnable_python
):
    """Run real Python commands and preserve both verdict directions.

    A Windows Store alias exits 9009 without running either Python program.
    The active interpreter exits 3 for confirmation and 0 for rejection.
    """
    reproduces = a_finding(
        ledger, db, poc='cmd::python3 -c "raise SystemExit(3)"', title="exits nonzero"
    )
    does_not = a_finding(ledger, db, poc="cmd::python3 -c pass", title="exits zero")

    confirmed = verify_finding.main(
        ["--db", str(db), "--finding-id", str(reproduces), "--worktree", str(worktree)]
    )
    capsys.readouterr()
    rejected = verify_finding.main(
        ["--db", str(db), "--finding-id", str(does_not), "--worktree", str(worktree)]
    )
    capsys.readouterr()

    assert (confirmed, rejected) == (0, 10)


def test_an_egress_proof_is_refused_unrun(ledger, verify_finding, db, worktree, capsys):
    """A forbidden shape is REFUSED, not run and then judged.

    The proof of concept below would create ``was-run`` in the worktree if it
    ever executed, so its absence is what proves the screen fired before the
    subprocess rather than after it.
    """
    finding_id = a_finding(ledger, db, poc="cmd::touch was-run curl")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "curl" in payload["reason"]
    assert not (worktree / "was-run").exists()
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.parametrize(
    "poc",
    [
        "cmd::cat ~/.aws/credentials",
        "cmd::cat ~/.ssh/id_rsa",
        "cmd::cat .kiro/crew/config.json",
        "cmd::python3 -c print(token)",
        "cmd::wget http://example.invalid/x",
        "cmd::ssh host true",
        "cmd::nc host 80",
    ],
)
def test_every_named_forbidden_shape_is_refused(ledger, verify_finding, db, worktree, capsys, poc):
    finding_id = a_finding(ledger, db, poc=poc, title=f"forbidden shape {poc}")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    assert code == 20
    assert out_json(capsys)["verdict"] == "needs-human"


def test_a_word_inside_another_word_is_not_an_egress_shape(verify_finding):
    """``nc`` must not fire on ``since``, and ``token`` must not fire on ``tokenize``.

    A screen that refused every innocent PoC would route the whole round to a
    human, which is the same outcome as having no verifier at all.
    """
    assert verify_finding.refusal_reason("cmd::python3 -m tokenize since.py") is None
    assert verify_finding.refusal_reason("pytest::test/test_concurrency.py::test_since") is None


def test_a_directory_that_is_not_a_git_worktree_is_refused(
    ledger, verify_finding, db, tmp_path, capsys
):
    """A scratch checkout is the whole blast-radius bound."""
    plain = tmp_path / "just-a-directory"
    plain.mkdir()
    finding_id = a_finding(ledger, db, poc="cmd::python3 -c pass")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(plain)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "git worktree" in payload["reason"]


def test_a_proof_that_outlives_the_deadline_is_needs_human(
    ledger, verify_finding, db, worktree, capsys, runnable_python
):
    finding_id = a_finding(ledger, db, poc='cmd::python3 -c "import time; time.sleep(30)"')

    code = verify_finding.main(
        [
            "--db",
            str(db),
            "--finding-id",
            str(finding_id),
            "--worktree",
            str(worktree),
            "--timeout",
            "1",
        ]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "did not finish" in payload["reason"]
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


def test_an_unknown_finding_records_nothing(ledger, verify_finding, db, worktree, capsys):
    """A verdict about a finding that is not there has nothing to attach to."""
    a_finding(ledger, db, poc="cmd::python3 -c pass")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", "9999", "--worktree", str(worktree)]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out.strip() == ""
    assert verdict_rows(ledger, db, 9999) == []


def test_a_recorded_finding_with_an_unreadable_proof_shape_records_nothing(
    ledger, verify_finding, db, worktree, capsys
):
    finding_id = a_finding(ledger, db, poc="just some prose")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    assert code == 2
    assert verdict_rows(ledger, db, finding_id) == []


def test_a_nonpositive_timeout_is_refused(ledger, verify_finding, db, worktree, capsys):
    finding_id = a_finding(ledger, db, poc="cmd::python3 -c pass")

    code = verify_finding.main(
        [
            "--db",
            str(db),
            "--finding-id",
            str(finding_id),
            "--worktree",
            str(worktree),
            "--timeout",
            "0",
        ]
    )

    assert code == 2
    assert verdict_rows(ledger, db, finding_id) == []


def test_the_child_environment_does_not_inherit_the_operators_home(verify_finding, tmp_path):
    """``HOME`` points at the worktree, so ``~/.aws`` resolves inside the sandbox.

    The key set is asserted CLOSED, because that is what keeps the operator's
    credentials out of a child the audited checkout controls: a name arrives in
    this environment by being listed, never by being inherited.
    """
    env = verify_finding.child_env(tmp_path)

    assert env["HOME"] == str(tmp_path)
    expected = {
        "PATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "LC_ALL",
    }
    # The three Windows names are forwarded only where the host defines them, so
    # the closed set varies by platform in exactly this one way.
    expected |= {name for name in ("SYSTEMROOT", "PATHEXT", "COMSPEC") if name in os.environ}
    assert set(env) == expected


def test_the_verifier_verdict_is_appended_not_overwritten(
    ledger, verify_finding, db, worktree, capsys
):
    """The ledger keeps the disagreement; two runs leave two rows."""
    (worktree / "test_poc.py").write_text(PASSING_TEST, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="pytest::test_poc.py::test_the_guard_refuses_it")
    conn = ledger.connect(db)
    try:
        ledger.record_verdict(
            conn, finding_id=finding_id, role="auditor", verdict="confirmed", reason="I saw it"
        )
    finally:
        conn.close()

    verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )
    capsys.readouterr()

    assert verdict_rows(ledger, db, finding_id) == [
        ("auditor", "confirmed"),
        ("verifier", "rejected"),
    ]


def test_the_scripts_shell_out_to_nothing_but_the_proof_of_concept(scope_check, finding_entry):
    """Only ``verify_finding`` runs a subprocess; the other two are pure readers.

    Asserted on the source rather than described in a comment, because "this
    script runs nothing" is a property a later edit can quietly take away.
    """
    for module in (scope_check, finding_entry):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        # The import, not the word: both files DISCUSS running nothing in their
        # docstrings, and a scan that matched the prose would pass for the wrong
        # reason and fail the moment someone rewrote a sentence.
        assert "import subprocess" not in source
        assert "os.system" not in source
        assert "os.popen" not in source


def test_every_script_carries_a_shebang_and_is_stdlib_only():
    for name in ("scope_check.py", "finding_entry.py", "verify_finding.py"):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert source.startswith("#!/usr/bin/env python3\n")
        imports = [
            line.split()[1].split(".")[0]
            for line in source.splitlines()
            if line.startswith("import ")
        ] + [
            line.split()[1].split(".")[0]
            for line in source.splitlines()
            if line.startswith("from ") and " import " in line
        ]
        assert set(imports) <= set(sys.stdlib_module_names), sorted(
            set(imports) - set(sys.stdlib_module_names)
        )


def test_a_proof_that_writes_outside_the_worktree_is_refused_unrun(
    ledger, verify_finding, db, worktree, capsys
):
    """The write-escape shape is refused, not run and then judged.

    The command below would create ``escaped`` in the worktree's PARENT if it
    ran, so that file's absence is what proves the screen fired before the
    subprocess. This is a named-shape screen, not containment: it refuses a
    ``..`` segment and an absolute path, and the module docstring says plainly
    that a program reaching outside by another spelling is bounded by the rules
    of engagement and the host's policy gate rather than by this check.
    """
    finding_id = a_finding(ledger, db, poc="cmd::touch ../escaped")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert not (worktree.parent / "escaped").exists()
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.parametrize(
    "poc",
    [
        "cmd::python3 -c \"open('../sibling', 'w').write('x')\"",
        "cmd::cp secrets /etc/passwd",
        "cmd::python3 --config=../outside.cfg",
    ],
)
def test_every_escape_shape_is_refused(ledger, verify_finding, db, worktree, capsys, poc):
    finding_id = a_finding(ledger, db, poc=poc, title=f"escape shape {poc}")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    assert code == 20
    assert out_json(capsys)["verdict"] == "needs-human"


def test_an_ordinary_relative_proof_is_not_read_as_an_escape(verify_finding):
    """The escape screen must not refuse the proofs the verifier exists to run."""
    assert verify_finding.refusal_reason("cmd::python3 -m pytest test/test_x.py") is None
    assert verify_finding.refusal_reason("cmd::python3 poc.py --mode=strict") is None


@pytest.mark.parametrize(
    "nodeid",
    [
        # A parametrised id: `token` then `[`, which IS a word boundary.
        "pytest::test/test_dashboard_auth.py::test_token[query-string]",
        # A module named for the thing under test.
        "pytest::test/token.py::test_rejects_a_query_string",
        # A directory named for it.
        "pytest::test/ssh/test_transport.py::test_refuses_an_unknown_host",
    ],
)
def test_a_pytest_nodeid_naming_a_screened_word_is_not_refused(verify_finding, nodeid):
    """A nodeid is a SELECTOR, not a program.

    Each id here really does contain a screened word as a WHOLE word -- which is
    what the word screen matches -- so word-screening nodeids refused exactly the
    tests this codebase's dominant finding class is written as, and bought
    nothing: what the selected test DOES was never visible to a string screen.
    The credential-PATH screen still applies to both shapes.
    """
    assert verify_finding.refusal_reason(nodeid) is None


def test_the_word_screen_still_applies_to_a_command_proof(verify_finding):
    """Narrowing the screen to ``cmd::`` must not disarm it there."""
    assert verify_finding.refusal_reason('cmd::python3 -c "print(token)"') is not None
    assert verify_finding.refusal_reason("cmd::ssh host true") is not None


def test_the_credential_path_screen_applies_to_a_nodeid_too(verify_finding):
    """No legitimate nodeid names a credential path, so both shapes keep it."""
    assert verify_finding.refusal_reason("pytest::test_x.py::test_reads_from_~/.aws")


def test_a_hostile_checkout_cannot_print_its_way_to_a_confirmation(
    ledger, verify_finding, db, worktree, capsys
):
    """The audited checkout is untrusted, so its STDOUT cannot be the verdict.

    A ``conftest.py`` in the target that prints pytest's own summary text and
    exits nonzero forged ``confirmed`` while the named test never ran. The
    verdict now comes from a JUnit report written outside the worktree and keyed
    to the requested nodeid, so this reaches a human instead.
    """
    (worktree / "conftest.py").write_text(
        "import sys\n\n\ndef pytest_sessionfinish(session, exitstatus):\n"
        "    print('1 failed, 0 passed in 0.01s')\n"
        "    sys.stdout.flush()\n"
        "    import os\n"
        "    os._exit(1)\n",
        encoding="utf-8",
    )
    (worktree / "test_poc.py").write_text(PASSING_TEST, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="pytest::test_poc.py::test_the_guard_refuses_it")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code != 0
    assert payload["verdict"] != "confirmed"


def test_a_report_that_names_another_test_is_needs_human(verify_finding, tmp_path):
    """The report is keyed to the nodeid that was asked for."""
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="1">'
        '<testcase classname="test_other" name="test_something_else">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, reason = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert verdict == "needs-human"
    assert "test_the_real_one" in reason


def test_a_missing_report_is_needs_human(verify_finding, tmp_path):
    """Fail closed: no report means the proof did not run to completion."""
    verdict, _ = verify_finding.judge_report(tmp_path / "absent.xml", "test_poc.py::test_x")

    assert verdict == "needs-human"


def test_a_pytest_selector_may_not_escape_the_worktree(verify_finding):
    """The path screen was wired to the cmd lane only, so a nodeid carried the same
    escape past it -- spelled as a selector instead of as an argument.

    pytest resolves a nodeid's file part against its working directory, which is the
    scratch checkout, so `..` there runs code the audited checkout does not contain.
    """
    for nodeid in (
        "pytest::../outside/test_poc.py::test_x",
        "pytest::../../etc/test_poc.py::test_x",
        "pytest::/abs/test_poc.py::test_x",
        "pytest::..\\outside\\test_poc.py::test_x",
    ):
        reason = verify_finding.refusal_reason(nodeid)
        assert reason is not None, nodeid
        assert "outside the scratch worktree" in reason, nodeid


def test_an_ordinary_nodeid_is_not_caught_by_the_escape_screen(verify_finding):
    """The screen must not cost the dominant real finding shape.

    Only the FILE part is screened, so a parametrised id carrying arbitrary text --
    including a screened word or a dotted value -- still runs.
    """
    for nodeid in (
        "pytest::test/test_security.py::test_echo_is_not_a_program",
        "pytest::test/test_security.py::test_token_auth[query]",
        "pytest::test/sub/dir/test_poc.py::test_x[a..b]",
        "pytest::test/test_poc.py::test_x[/abs/path]",
    ):
        assert verify_finding.refusal_reason(nodeid) is None, nodeid


def test_a_pytest_selector_that_names_only_a_file_is_not_runnable(verify_finding):
    """An unkeyed selector is refused as a SHAPE, before anything runs.

    A file-wide run produces a report keyed to no single test, so ANY failure in
    that file satisfies the finding. Refusing the shape is what makes that verdict
    unreachable rather than merely unlikely.
    """
    assert verify_finding.poc_argv("pytest::test_poc.py") is None
    assert verify_finding.poc_argv("pytest::test_poc.py::") is None
    assert verify_finding.poc_argv("pytest::test_poc.py::test_x") is not None


def test_a_finding_whose_proof_names_only_a_file_is_refused_at_filing(
    ledger, finding_entry, db, missing_json, capsys
):
    """Write-time validation runs the verifier's own parser, so an unkeyed
    selector never reaches the ledger at all."""
    seed_roe(ledger, db)

    code = file_finding(
        finding_entry,
        db,
        missing_json,
        "--surface",
        "security.is_denied",
        "--severity",
        "High",
        "--title",
        "a whole file stands in for one test",
        "--path",
        "src/kiro_crew/security.py",
        "--poc",
        "pytest::test/test_security.py",
    )

    assert code == 2
    assert "pytest::" in capsys.readouterr().err


def test_an_unkeyed_selector_is_never_confirmed_by_a_report(verify_finding, tmp_path):
    """The reader refuses too: a failure belonging to some other test in the same
    file is not evidence for this finding."""
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="1">'
        '<testcase classname="test_poc" name="test_something_unrelated">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, reason = verify_finding.judge_report(report, "test_poc.py")

    assert verdict == "needs-human"
    assert "one test" in reason


def test_the_child_env_carries_what_a_windows_process_needs_to_start(
    verify_finding, tmp_path, monkeypatch
):
    """Withholding these does not harden the child, it stops it existing.

    Without ``SYSTEMROOT`` a Windows CPython dies seeding ``os.urandom`` during
    interpreter startup, so the proof never runs and every pytest finding reads
    ``needs-human``. ``PATHEXT`` and ``COMSPEC`` are how a bare program name
    resolves to an executable there.
    """
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")

    env = verify_finding.child_env(tmp_path)

    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["PATHEXT"] == ".COM;.EXE;.BAT"
    assert env["COMSPEC"] == r"C:\Windows\system32\cmd.exe"


def test_a_name_the_host_does_not_define_is_not_invented(verify_finding, tmp_path, monkeypatch):
    """The passthrough is a filter, not a set of required keys, so a minimal host
    is unaffected rather than handed empty strings."""
    for name in ("SYSTEMROOT", "PATHEXT", "COMSPEC"):
        monkeypatch.delenv(name, raising=False)

    env = verify_finding.child_env(tmp_path)

    assert "SYSTEMROOT" not in env
    assert "PATHEXT" not in env
    assert "COMSPEC" not in env


def test_the_child_scratch_directory_is_the_worktree_in_every_spelling(verify_finding, tmp_path):
    """``tempfile`` reads ``TMPDIR`` on POSIX and ``TEMP``/``TMP`` on Windows, so
    pinning one spelling left a PoC's scratch files outside the throwaway
    checkout on the other platform."""
    env = verify_finding.child_env(tmp_path)

    assert env["TMPDIR"] == str(tmp_path)
    assert env["TEMP"] == str(tmp_path)
    assert env["TMP"] == str(tmp_path)


def test_filing_records_the_auditor_verdict(ledger, finding_entry, db, missing_json, capsys):
    """Filing IS the auditor's claim, and the retrospective reads it.

    ``findings.auditor_verdict`` is a materialised view of the verdict rows, so
    with no row written the column stays NULL and an auditor claim is
    indistinguishable from an absent one -- which is the false-positive signal the
    procedure obliges the conductor to report.
    """
    seed_roe(ledger, db)

    code = file_finding(finding_entry, db, missing_json)

    finding_id = out_json(capsys)["finding_id"]
    assert code == 0
    assert verdict_rows(ledger, db, finding_id) == [("auditor", "confirmed")]
    conn = ledger.connect(db)
    try:
        row = conn.execute(
            "SELECT auditor_verdict FROM findings WHERE id = ?", (finding_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["auditor_verdict"] == "confirmed"


def test_a_duplicate_filing_does_not_append_a_second_auditor_verdict(
    ledger, finding_entry, db, missing_json, capsys
):
    """A re-report returns the original id, and the earlier row's verdict history
    is the record rather than something a second filing appends to."""
    seed_roe(ledger, db)
    assert file_finding(finding_entry, db, missing_json) == 0
    finding_id = out_json(capsys)["finding_id"]

    assert file_finding(finding_entry, db, missing_json) == 3

    assert verdict_rows(ledger, db, finding_id) == [("auditor", "confirmed")]


def test_a_filing_repairs_a_finding_that_carries_no_auditor_claim(
    ledger, finding_entry, db, missing_json, capsys
):
    """``add_finding`` commits on its own, so a crash before the verdict write is
    possible -- and keyed on ``created`` the retry would skip the repair forever.

    This drives that state directly: a finding inserted through the ledger, with no
    verdict row, is what the process would leave behind. Re-filing it must complete
    the record rather than report a duplicate and walk away.
    """
    seed_roe(ledger, db)
    finding_id = a_finding(
        ledger,
        db,
        poc="pytest::test/test_security.py::test_echo_is_not_a_program",
        title="echo text is classified as an executed program",
    )
    assert verdict_rows(ledger, db, finding_id) == []

    code = file_finding(finding_entry, db, missing_json)

    assert code == 3
    assert out_json(capsys)["finding_id"] == finding_id
    assert verdict_rows(ledger, db, finding_id) == [("auditor", "confirmed")]


def test_a_filing_does_not_overwrite_a_verdict_someone_else_recorded(
    ledger, finding_entry, db, missing_json, capsys
):
    """A human ruling outranks a re-report, so the repair is keyed on the auditor
    row's absence and never on the folded column."""
    seed_roe(ledger, db)
    assert file_finding(finding_entry, db, missing_json) == 0
    finding_id = out_json(capsys)["finding_id"]
    conn = ledger.connect(db)
    try:
        ledger.record_verdict(
            conn, finding_id=finding_id, role="human", verdict="rejected", reason="not a defect"
        )
    finally:
        conn.close()

    assert file_finding(finding_entry, db, missing_json) == 3

    assert verdict_rows(ledger, db, finding_id) == [
        ("auditor", "confirmed"),
        ("human", "rejected"),
    ]


def test_an_unrelated_failure_beside_the_requested_test_does_not_confirm(verify_finding, tmp_path):
    """Naming the test is not enough -- the verdict is that test's OWN result.

    A run reports tests beside the cited one (a parametrised sibling, whatever else
    the selector's file collected), and folding those in let an unrelated failure
    confirm this finding.
    """
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2">'
        '<testcase classname="test_poc" name="test_the_real_one"/>'
        '<testcase classname="test_poc" name="test_a_sibling">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, reason = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert verdict == "rejected"
    assert "test_the_real_one" in reason


def test_the_requested_test_failing_still_confirms(verify_finding, tmp_path):
    """The narrowing must not cost the verdict it exists to produce."""
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2">'
        '<testcase classname="test_poc" name="test_a_sibling"/>'
        '<testcase classname="test_poc" name="test_the_real_one">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, _ = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert verdict == "confirmed"


def test_a_parametrised_proof_cited_by_its_base_name_is_judged_on_its_own_cases(
    verify_finding, tmp_path
):
    """``pytest::file::test_foo`` selects every ``test_foo[...]``; so must the report match.

    pytest names a parametrised case ``test_foo[a]`` in the JUnit report, so an
    exact comparison against the bare ``test_foo`` matched nothing, and a finding
    whose proof is a parametrised test -- accepted at filing, since the nodeid
    names a test -- landed on ``needs-human`` on every run, unconfirmable and
    silent about it.
    """
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2">'
        '<testcase classname="test_poc" name="test_the_real_one[a]">'
        "<failure>boom</failure></testcase>"
        '<testcase classname="test_poc" name="test_the_real_one[b]">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, reason = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert verdict == "confirmed"
    assert "2 of 2" in reason


def test_one_failing_case_of_a_parametrised_proof_confirms(verify_finding, tmp_path):
    """The aggregation rule: a proof's cases are its runs, and ONE failing run reproduces.

    A finding claims "the defect is present"; a parametrised proof that fails for
    one input and passes for another has demonstrated it for that input, which is
    the same reading a single unparametrised failure gets. The reason says how
    many of the runs failed, so the human reading the ledger sees the split.
    """
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2">'
        '<testcase classname="test_poc" name="test_the_real_one[a]"/>'
        '<testcase classname="test_poc" name="test_the_real_one[b]">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, reason = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert verdict == "confirmed"
    assert "1 of 2" in reason


def test_a_parametrised_sibling_is_still_not_the_requested_test(verify_finding, tmp_path):
    """Widening to a test's OWN cases must not reach a test whose name merely extends it.

    ``test_the_real_one_too[a]`` shares the prefix but is a different test; only
    the ``[`` that pytest puts between a test's name and its case id counts. And a
    nodeid that names ONE case, ``test_the_real_one[a]``, is judged on that case
    alone, not on its parametrised siblings.
    """
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="4">'
        '<testcase classname="test_poc" name="test_the_real_one[a]"/>'
        '<testcase classname="test_poc" name="test_the_real_one[b]">'
        "<failure>boom</failure></testcase>"
        '<testcase classname="test_poc" name="test_the_real_one_too[a]">'
        "<failure>boom</failure></testcase>"
        '<testcase classname="test_poc" name="test_the_real_one_too">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    one_case, one_reason = verify_finding.judge_report(report, "test_poc.py::test_the_real_one[a]")
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="3">'
        '<testcase classname="test_poc" name="test_the_real_one[a]"/>'
        '<testcase classname="test_poc" name="test_the_real_one_too[a]">'
        "<failure>boom</failure></testcase>"
        '<testcase classname="test_poc" name="test_the_real_one_too">'
        "<failure>boom</failure></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    base_name, base_reason = verify_finding.judge_report(report, "test_poc.py::test_the_real_one")

    assert (one_case, base_name) == ("rejected", "rejected")
    assert "1 run(s)" in one_reason
    assert "1 run(s)" in base_reason


def test_the_verifier_refuses_the_checkout_it_is_running_from(verify_finding, tmp_path):
    """The checkable half of "the sandbox must be disposable".

    Provenance in general is not checkable from here -- the caller picks the path and
    so controls what is inside it -- but "you pointed me at my own source tree" is
    decidable without trusting the caller, and it is the case an operator reaches by
    accident.
    """
    repo_root = Path(verify_finding.__file__ or "").resolve().parents[5]

    assert verify_finding.is_own_checkout(repo_root)
    assert not verify_finding.is_own_checkout(tmp_path)


def test_a_proof_is_refused_when_the_sandbox_is_the_verifiers_own_repository(verify_finding):
    """Wired into the verdict path, not just available as a helper."""
    repo_root = Path(verify_finding.__file__ or "").resolve().parents[5]

    verdict, reason = verify_finding.decide(
        {"poc": "pytest::test/test_security.py::test_echo_is_not_a_program"}, repo_root, 5
    )

    assert verdict == "needs-human"
    assert "running from" in reason


def test_a_missing_command_records_needs_human_instead_of_crashing(
    ledger, verify_finding, db, worktree, capsys
):
    """A mistyped or hallucinated program name is the verifier's ordinary input.

    ``subprocess.run`` raises ``FileNotFoundError`` for it rather than returning
    a status, and letting that propagate killed the process outside the
    documented ``{0,10,20,2}`` contract with no verdict recorded at all.
    """
    finding_id = a_finding(ledger, db, poc="cmd::definitely-no-such-command-anywhere --flag")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "could not be started" in payload["reason"]
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.parametrize(
    "returncode,verdict",
    [
        (0, "rejected"),
        (1, "needs-human"),
        (2, "confirmed"),
        (3, "confirmed"),
        (125, "confirmed"),
        (126, "needs-human"),
        (127, "needs-human"),
        (137, "needs-human"),
        (139, "needs-human"),
        (255, "needs-human"),
        (3221225477, "needs-human"),
        (-9, "needs-human"),
    ],
)
def test_a_command_that_never_ran_does_not_confirm(verify_finding, returncode, verdict):
    """``cmd::`` may not read its verdict off "nonzero" alone.

    Nonzero covers "the defect reproduced" AND "the command never ran" -- a
    missing interpreter, a wrong cwd, a death by signal. Confirming on the second
    is the errored-proof false positive the pytest lane already rejects, so the
    two lanes now hold the same bar. Exit 1 is in the second set: it is what a
    model-authored PoC exits when it dies on a hallucinated import, and nothing
    in the status separates that from a reproduction.
    """
    assert verify_finding.judge_cmd(returncode)[0] == verdict


def test_a_command_proof_that_crashes_is_not_confirmed(
    ledger, verify_finding, db, worktree, capsys, runnable_python
):
    """End to end: a PoC dying on a missing import must not persist `confirmed`."""
    finding_id = a_finding(ledger, db, poc='cmd::python3 -c "import no_such_module_anywhere_xyz"')

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert "exited 1" in payload["reason"]
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGKILL is POSIX-only. Windows reports no negative returncode, so the"
    " signal-death shape this drives end to end cannot arise there; judge_cmd's"
    " mapping for it is covered directly by the unit test above.",
)
def test_a_signal_killed_command_records_needs_human(
    ledger, verify_finding, db, worktree, capsys, runnable_python
):
    """The launch-failure mapping, driven end to end rather than unit-only."""
    finding_id = a_finding(
        ledger, db, poc='cmd::python3 -c "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"'
    )

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "needs-human")]


def test_the_child_path_fallback_names_no_posix_only_directory(
    verify_finding, tmp_path, monkeypatch
):
    """A hardcoded ``/usr/bin:/bin`` fallback names nothing on Windows."""
    monkeypatch.delenv("PATH", raising=False)

    assert verify_finding.child_env(tmp_path)["PATH"] == os.defpath


def test_the_ledger_is_loaded_once_per_invocation(finding_entry, scope_check, verify_finding):
    """Three scripts, one ledger module -- not one load per sibling.

    ``finding_entry`` loads both siblings, and a module-level ledger load in each
    executed ``ledger.py`` three times for one filing. The siblings load it
    lazily and ``finding_entry`` takes ``scope_check``'s instance.
    """
    assert finding_entry._ledger is finding_entry._scope_check.ledger()
    assert scope_check.ledger() is scope_check.ledger()
    assert verify_finding.ledger() is verify_finding.ledger()


def test_the_proof_shapes_are_declared_once(finding_entry, verify_finding):
    """Write-time validation and the verifier cannot drift apart.

    ``finding_entry`` validates a PoC with ``verify_finding``'s own parser, so a
    third proof shape cannot leave filing rejecting what the verifier runs.
    """
    source = Path(finding_entry.__file__ or "").read_text(encoding="utf-8")
    assert "POC_PREFIXES = (" not in source
    assert finding_entry.validate_poc("pytest::test_x.py::test_y") is None
    assert finding_entry.validate_poc("cmd::python3 -c pass") is None
    assert finding_entry.validate_poc("prose about the bug") is not None
    assert verify_finding.POC_PREFIXES == ("pytest::", "cmd::")


def test_an_output_flood_does_not_buffer_into_the_verifier(
    ledger, verify_finding, db, worktree, capsys, runnable_python
):
    """The PoC's output is discarded, so a printing PoC cannot exhaust memory.

    No verdict reads the child's output -- the pytest lane reads the report file
    and the command lane reads the status -- but capturing it through a pipe
    buffered the whole stream in this process, and ``--timeout`` bounds wall time,
    never bytes. This PoC prints ~64MB and must still reach a verdict.
    """
    program = (
        "import sys\n"
        "block = 'x' * 65536\n"
        "for _ in range(1000):\n"
        "    sys.stdout.write(block)\n"
        "raise SystemExit(7)\n"
    )
    (worktree / "flood.py").write_text(program, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="cmd::python3 flood.py")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 0
    assert payload["verdict"] == "confirmed"
    assert verdict_rows(ledger, db, finding_id) == [("verifier", "confirmed")]


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX-only")
def test_a_fork_outliving_a_SUCCEEDING_proof_is_still_reaped(
    ledger, verify_finding, db, worktree, capsys, runnable_python
):
    """A proof that exits cleanly says nothing about a half it forked first.

    This is the shape the measured incident actually took. A version-manager
    shim forks, and the fork is what spins; the wrapper can exit with a perfectly
    good status while its child keeps a core busy. So the group sweep cannot hang
    off the deadline path alone -- on this run there IS no deadline, the verdict
    is decided, and the survivor would simply be abandoned.
    """
    program = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "with open('forked.pid', 'w', encoding='utf-8') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "    fh.flush()\n"
        "    os.fsync(fh.fileno())\n"
        # Exit a deliberate in-range status, so the verdict is `confirmed` and
        # nothing about this run is a timeout.
        "raise SystemExit(3)\n"
    )
    (worktree / "forker.py").write_text(program, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="cmd::python3 forker.py")

    code = verify_finding.main(
        ["--db", str(db), "--finding-id", str(finding_id), "--worktree", str(worktree)]
    )

    payload = out_json(capsys)
    assert code == 0
    assert payload["verdict"] == "confirmed", "the proof must have exited normally"

    forked = int((worktree / "forked.pid").read_text(encoding="utf-8").strip())
    # Identity captured WITH the pid, so the finally can prove the number still
    # names this fork before signalling it; see the sibling test.
    forked_start_id = platform_compat.get_process_start_id(forked)
    reaped = False
    try:
        # flake-ok: pre-existing 10 s real-time deadline on the poll for the fork's exit, which fails loudly, moved verbatim; tracked for a de-flake pass
        give_up_at = time.monotonic() + 10.0
        while True:
            # Liveness through the repo's own probe (AGENTS.md "Cross-platform"),
            # not a raw ``os.kill(pid, 0)``: that TERMINATES the target on Windows,
            # and the sweep's caller filter recognises only the sanctioned helper.
            if not platform_compat.pid_exists(forked):
                reaped = True
                return
            assert (
                time.monotonic() < give_up_at
            ), f"fork {forked} outlived a SUCCEEDING proof -- the group was never swept"
            # flake-ok: pre-existing 50 ms pacing of the bounded poll for the fork's exit, moved verbatim; tracked for a de-flake pass
            time.sleep(0.05)
    finally:
        # Same rule as the sibling test: never signal a pid whose death this test
        # already proved, because that number is free to be reassigned.
        if (
            not reaped
            and forked_start_id is not None
            and platform_compat.get_process_start_id(forked) == forked_start_id
        ):
            with contextlib.suppress(OSError):
                os.kill(forked, signal.SIGKILL)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX-only")
def test_a_wedged_grandchild_does_not_outlive_the_deadline(
    ledger, verify_finding, db, worktree, capsys, runnable_python
):
    """The timeout path reaps the proof's whole GROUP, not just the pid it spawned.

    A PoC is routinely a wrapper that forks -- a version-manager shim is one, and
    that is how four runs of this file left nine processes reparented to init,
    spinning at 1464% CPU between them for six days. Killing only the direct
    child leaves the forked half burning a core with nothing left on the machine
    that can name what it belonged to.

    The grandchild here outlives its parent deliberately, so it survives iff the
    reap is a single ``kill``. Asserted by polling for its death rather than by
    reading a status: the parent's own exit says nothing about a process it left
    behind.
    """
    program = (
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "with open('grandchild.pid', 'w', encoding='utf-8') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "    fh.flush()\n"
        "    os.fsync(fh.fileno())\n"
        # Outlive the verifier's deadline so the reap, not a natural exit, is what
        # ends this tree.
        "time.sleep(300)\n"
    )
    (worktree / "wedged.py").write_text(program, encoding="utf-8")
    finding_id = a_finding(ledger, db, poc="cmd::python3 wedged.py")

    code = verify_finding.main(
        [
            "--db",
            str(db),
            "--finding-id",
            str(finding_id),
            "--worktree",
            str(worktree),
            "--timeout",
            # flake-ok: pre-existing 1 s verifier deadline the PoC must report its grandchild's pid inside, moved verbatim; tracked for a de-flake pass
            "1",
        ]
    )

    payload = out_json(capsys)
    assert code == 20
    assert payload["verdict"] == "needs-human"

    pid_file = worktree / "grandchild.pid"
    assert pid_file.exists(), "the PoC never reported its grandchild"
    grandchild = int(pid_file.read_text(encoding="utf-8").strip())
    # Identity alongside the pid: the reap below must not signal a number the
    # kernel has since handed to an unrelated host process. `None` means unknown,
    # and unknown means do not signal.
    grandchild_start_id = platform_compat.get_process_start_id(grandchild)
    # This test's OWN cleanup, because the state it asserts about is a live
    # process: when the property is broken the grandchild is by definition still
    # running, and a bare assert would leave the 300s sleeper behind on every
    # failing run -- the exact leak this file is here to close, reintroduced by
    # the test for it. The finally reaps it on every exit path, including the
    # assertion's.
    reaped = False
    try:
        # Generous, and only reached when the property is broken: a killpg is
        # immediate, so this bound exists to fail by name rather than be waited on.
        # flake-ok: pre-existing 10 s real-time deadline on the poll for the grandchild's exit, which fails loudly, moved verbatim; tracked for a de-flake pass
        give_up_at = time.monotonic() + 10.0
        while True:
            # Liveness through the repo's own probe, as in the sibling test above.
            if not platform_compat.pid_exists(grandchild):
                reaped = True
                return
            assert (
                time.monotonic() < give_up_at
            ), f"grandchild {grandchild} outlived the reap -- the kill did not reach the group"
            # flake-ok: pre-existing 50 ms pacing of the bounded poll for the grandchild's exit, moved verbatim; tracked for a de-flake pass
            time.sleep(0.05)
    finally:
        # Only when the poll did NOT prove it dead. A pid is free for the kernel to
        # reassign the moment it exits, so signalling one whose death this test just
        # confirmed aims at whatever process now holds the number -- on every
        # passing run, which is worse than the leak it would be guarding against.
        if (
            not reaped
            and grandchild_start_id is not None
            and platform_compat.get_process_start_id(grandchild) == grandchild_start_id
        ):
            with contextlib.suppress(OSError):
                os.kill(grandchild, signal.SIGKILL)


def test_the_windows_reap_names_taskkill_by_absolute_path(verify_finding):
    """Asserted on the source, because the branch only runs on Windows.

    ``reap`` fires while an untrusted checkout's proof of concept is executing,
    and Windows resolves a bare ``argv[0]`` starting from the calling process's
    own directory -- so a bare ``"taskkill"`` is a command-hijack surface. The
    rest of the tree already refuses to reach ``git`` or ``gh`` through PATH for
    the same reason; this pins the same rule here, where no Windows test on the
    POSIX matrix can.
    """
    source = Path(verify_finding.__file__ or "").read_text(encoding="utf-8")
    assert '"taskkill"' not in source, "taskkill must never be spelled as a bare argv[0]"
    # Every candidate is an absolute literal naming the executable, and each is
    # probed with `isfile` -- so an absent binary fails CLOSED to the direct kill
    # rather than falling through to the OS search order.
    assert "taskkill.exe" in source, "the reap must name the System32 binary"
    assert "os.path.isfile(candidate)" in source
    assert "System32" in source
    # No environment value may reach argv[0]: reading the Windows root out of the
    # environment and joining it is the shape
    # `dangerous-subprocess-use-tainted-env-args` refuses. Matched on the CODE
    # spelling, not the word, because `child_env` legitimately forwards
    # `SYSTEMROOT` to the child and the comments here name it to explain the ban.
    assert 'os.environ.get("SystemRoot")' not in source
    assert 'environ["SystemRoot"]' not in source
    # A nonzero taskkill is a FAILED tree kill, not a reap, so the status is read
    # and anything but success falls back to the direct kill.
    assert ".returncode" in source
    assert "if not reaped:" in source


def test_run_poc_returns_a_bare_status_and_captures_nothing(verify_finding):
    """Asserted on the source: a pipe re-added is the whole defect coming back."""
    source = Path(verify_finding.__file__ or "").read_text(encoding="utf-8")
    assert "stdout=subprocess.DEVNULL" in source
    assert "stderr=subprocess.DEVNULL" in source
    assert "subprocess.PIPE" not in source


def test_a_test_named_like_an_outcome_element_cannot_forge_one(verify_finding, tmp_path):
    """The test file is in the untrusted checkout, so a test NAME is attacker text.

    XML escapes a ``<`` inside an attribute as ``&lt;``, so a raw ``<failure`` can
    only be markup -- which is why the reader matches the tag and not the word.
    """
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="c" name="test_x&lt;failure/&gt;"'
        ' time="0.0"></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )

    verdict, _ = verify_finding.judge_report(report, "test_poc.py::test_x&lt;failure/&gt;")

    assert verdict == "rejected"


def test_the_case_name_is_not_read_off_classname(verify_finding, tmp_path):
    """``name="`` is a SUBSTRING of ``classname="``.

    An unanchored search returned the module name for every case, so the
    nodeid check compared the wrong string and every real run came back
    ``needs-human``. Caught by the suite before it shipped.
    """
    chunk = ' classname="test_poc" name="test_boom" time="0.001"><failure>E</failure></testcase>'

    assert verify_finding.case_name(chunk) == "test_boom"


def test_a_name_inside_a_nested_element_is_not_the_case_name(verify_finding):
    """Only the start tag is searched, so a nested attribute cannot supply one."""
    chunk = ' classname="c" time="0.0"><failure name="not-the-test">E</failure></testcase>'

    assert verify_finding.case_name(chunk) == "c" or verify_finding.case_name(chunk) is None


def test_an_element_name_must_be_a_whole_tag(verify_finding, tmp_path):
    """``<failures>`` is not ``<failure``."""
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="c" name="test_x">'
        "<failures>0</failures></testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    verdict, _ = verify_finding.judge_report(report, "test_poc.py::test_x")

    assert verdict == "rejected"


def test_verifying_against_a_missing_ledger_creates_nothing(
    verify_finding, tmp_path, worktree, capsys
):
    """Verifying never CREATES a ledger.

    A finding must be filed to be verified, and filing is what creates the
    database -- so a missing one is a mistyped ``--db``. Creating it there left an
    empty ledger behind, which ``scope_check`` then reads as a ledger with zero
    rules rather than as no ledger at all.
    """
    absent = tmp_path / "nested" / "findings.db"

    code = verify_finding.main(
        ["--db", str(absent), "--finding-id", "1", "--worktree", str(worktree)]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert not absent.exists()
    assert not absent.parent.exists()
    assert "no ledger at" in captured.err


def test_a_ledger_that_is_not_a_database_exits_two(verify_finding, tmp_path, worktree, capsys):
    """A file that is not a ledger is an input error, not a verdict."""
    bogus = tmp_path / "findings.db"
    bogus.write_bytes(b"not a sqlite database")

    code = verify_finding.main(
        ["--db", str(bogus), "--finding-id", "1", "--worktree", str(worktree)]
    )

    assert code == 2
    assert "cannot read findings" in capsys.readouterr().err


def test_a_json_file_alone_still_carries_its_own_round_id(
    ledger, finding_entry, db, missing_json, tmp_path, capsys
):
    """Refusing the COMBINATION must not refuse the file's own round id."""
    seed_roe(ledger, db)
    payload = tmp_path / "finding.json"
    payload.write_text(
        json.dumps(
            {
                "surface": "webhooks.ingest",
                "severity": "High",
                "title": "unbounded frame accepted",
                "paths": ["src/kiro_crew/webhooks.py"],
                "poc": "cmd::python3 -c pass",
                "round_id": "kirocrew-dogfood-round-1",
            }
        ),
        encoding="utf-8",
    )

    code = finding_entry.main(
        ["--db", str(db), "--roe-json", str(missing_json), "--json-file", str(payload)]
    )
    filed = out_json(capsys)

    conn = ledger.connect(db)
    try:
        row = conn.execute(
            "SELECT round_id FROM findings WHERE id = ?", (filed["finding_id"],)
        ).fetchone()
    finally:
        conn.close()

    assert code == 0
    assert row["round_id"] == "kirocrew-dogfood-round-1"
