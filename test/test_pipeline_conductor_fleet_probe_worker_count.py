"""Fleet probe (``fleet_probe.py``): reading a run's worker count from its argv.

Every runner form crossed with the budgeted and unbudgeted count spellings, runner-shaped
arguments after the cap, the lexical count reader, the terminator and ``addopts``
override, and argv-only spellings honouring the budget.

Shared fixtures and helpers are in ``pipeline_conductor_fleet_probe_helpers.py``;
``test_pipeline_conductor_fleet_probe.py`` heads the suite and states how it is organised.
"""

from __future__ import annotations

import pytest
from pipeline_conductor_fleet_probe_helpers import mod_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import (
    BUDGETED_SPELLINGS,
    LAUNCHER_OWN_GRAMMAR,
    banned_pids,
    fleet_pid,
    host_proc,
    install_prefixes,
    with_prefixes,
)

# Every way this repo's runner is spelled on a command line, as an argv PREFIX. The
# pin below is enumerated over this table crossed with the cap spellings, not over a
# list of command lines: the property under test is "any invocation form, bounded or
# not", so a form added here is covered in both directions by construction and a fix
# that only repairs the spelling someone happened to write down cannot pass.
RUNNER_FORMS = {
    "bare": ["pytest"],
    "module": ["python3", "-m", "pytest"],
    "versioned-interpreter-module": ["python3.12", "-m", "pytest"],
    "venv-abspath": ["/wt/.venv/bin/pytest"],
    "py.test": ["py.test"],
    "pytest.exe": ["pytest.exe"],
    "alias": ["pytest-3"],
    "alias-minor": ["pytest-3.12"],
    "alias-abspath": ["/usr/bin/pytest-3"],
    "alias-py.test": ["py.test-3"],
    "alias-shebang": ["python3", "{sys}/bin/pytest-3"],
    "alias-shebang-venv": ["python3", "{venv}/bin/pytest-3"],
    "alias-shebang-behind-value-flag": ["python3", "-W", "ignore", "{sys}/bin/pytest-3"],
    "alias-behind-launcher": ["timeout", "900", "pytest-3"],
    "alias-behind-launcher-with-own-flag": ["nice", "-n", "10", "pytest-3"],
}


# Every spelling of an explicit worker COUNT of two or more: the form that bypasses the
# budget hook (`setup.cfg`: "An explicit -n <N> bypasses the budget") and the one the rule
# reports. `last-wins-four` is last-wins in the reporting direction; the terminator row
# is a 32-worker run pointed at a path called `-n0`; the override rows carry the count in
# the `addopts` an `-o` / `--override-ini` replaces the tree's with; the oversized row is
# a count longer than the interpreter converts, judged lexically like every other.
UNBUDGETED_SPELLINGS = {
    "glued-four": ["-n4"],
    "split-four": ["-n", "4"],
    "equals-four": ["-n=4"],
    "split-two": ["-n", "2"],
    "split-thirty-two": ["-n", "32"],
    "long-equals-four": ["--numprocesses=4"],
    "long-split-two": ["--numprocesses", "2"],
    "last-wins-four": ["-n", "auto", "-n", "4"],
    "count-then-terminator-then-zero": ["-n32", "--", "-n0"],
    "override-long-equals": ["--override-ini=addopts=-n 16"],
    "override-short-split": ["-o", "addopts=-n 16"],
    "override-short-glued": ["-oaddopts=-n 16"],
    "override-auto-then-own-four": ["-o", "addopts=-n auto", "-n", "4"],
    "oversized-count": ["-n", "9" * 5000],
}


# Tokens that appear AFTER the cap and name the runner without being an invocation of
# it. Each one defeated the cap lookahead, which only ever looked forward from the
# token it matched: the bound sits earlier in the line, so from the second occurrence
# it is invisible and a bounded run was reported.
TRAILING_RUNNER_SHAPED_ARGS = {
    "junitxml": ["--junitxml=build/pytest.xml"],
    "log-file": ["--log-file", "/var/tmp/pytest-run.log"],
    "basetemp": ["--basetemp", "/var/tmp/pytest-of-ci"],
    "rootdir": ["--rootdir", "/wt/pytest-sandbox"],
}


@pytest.mark.parametrize("form", sorted(RUNNER_FORMS))
@pytest.mark.parametrize("count", sorted(BUDGETED_SPELLINGS))
def test_every_runner_form_stays_quiet_when_its_worker_pool_is_budgeted(
    mod, tmp_path, monkeypatch, form, count
):
    """A budgeted or single-process run is never reported, however it is spelled.

    This is the direction that destroys work: the documented answer to a fleet-owned
    ``BANNED`` line is to stop that worker and discard the turn it was in, so a false
    row here costs real work rather than signal -- and the gate runner's own
    ``-n auto`` is exactly such a row under an inverted rule.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    form_argv = with_prefixes(RUNNER_FORMS[form], prefixes)
    argv = [*form_argv, *BUDGETED_SPELLINGS[count], "test/test_x.py"]
    fleet_pid(root, fleet, "401", argv)
    assert banned_pids(mod, root, fleet) == set(), f"{form} + {count} was reported while budgeted"


@pytest.mark.parametrize("form", sorted(RUNNER_FORMS))
@pytest.mark.parametrize("count", sorted(UNBUDGETED_SPELLINGS))
def test_every_runner_form_is_reported_when_it_fixes_a_worker_count(
    mod, tmp_path, monkeypatch, form, count
):
    """A run whose worker count bypasses the budget is reported, however it is spelled.

    The other direction, and the one the probe exists for. A form or a spelling missing
    here is an unbudgeted run the conductor's banned counter cannot see, so intake keeps
    admitting work while the host is being consumed -- ``pytest -n 32`` on a shared box
    is invisible to an inverted rule.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    form_argv = with_prefixes(RUNNER_FORMS[form], prefixes)
    argv = [*form_argv, *UNBUDGETED_SPELLINGS[count], "test/"]
    fleet_pid(root, fleet, "402", argv)
    assert banned_pids(mod, root, fleet) == {"402"}, f"{form} + {count} went unreported"


@pytest.mark.parametrize("form", sorted(RUNNER_FORMS))
@pytest.mark.parametrize("trailing", sorted(TRAILING_RUNNER_SHAPED_ARGS))
def test_a_capped_run_stays_quiet_when_a_later_argument_names_the_runner(
    mod, tmp_path, monkeypatch, form, trailing
):
    """A single-process run stays one when a LATER argument spells the runner's name.

    ``--junitxml=build/pytest.xml`` and ``--log-file /var/tmp/pytest-run.log`` are
    ordinary arguments. A forward-only lookahead re-tries at that second occurrence,
    where the ``-n0`` is behind it, and reports the run --
    two deterministic false rows on a live fleet. The rows stay pinned: a second runner
    token must never be read as a second, differently-counted invocation.
    """
    prefixes = install_prefixes(tmp_path)
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    argv = [
        *with_prefixes(RUNNER_FORMS[form], prefixes),
        "-n0",
        "test/test_x.py",
        *TRAILING_RUNNER_SHAPED_ARGS[trailing],
    ]
    fleet_pid(root, fleet, "403", argv)
    assert banned_pids(mod, root, fleet) == set(), (
        f"{form} was reported while capped because a later argument named the runner "
        f"({trailing})"
    )


def test_the_count_reader_reads_every_spelling_its_flags_have(mod):
    """The reader's COVERAGE, enumerated from ``_CAP_FLAGS`` rather than from the reader.

    The implication pin below asks "whatever the reader calls budgeted is never
    reported", which is the asymmetry itself -- but it reads its own subject, so a reader
    that stops recognising a spelling simply drops out of it and the pin stays green while
    a budgeted run starts being reported, or an unbudgeted one stops being. This asserts
    the other half: each flag's spellings are generated from the flag list, and the reader
    must read every one of them, in BOTH directions -- ``0``, ``1``, ``auto`` and
    ``logical`` as budgeted, ``4`` as not.

    Glued digits are asserted only for a SHORT flag. The file documents why:
    ``--numprocessesN`` is not a spelling that option has, so a longer token starting with
    it is a different option; with no other ``-n`` the run inherits ``addopts`` and is
    budgeted.
    """
    assert mod._CAP_FLAGS, "no count flags to enumerate"
    for flag in sorted(mod._CAP_FLAGS):
        short = len(flag) == 2 and flag.startswith("-") and not flag.startswith("--")
        budgeted = [[f"{flag}=0"], [flag, "0"], [flag, "1"], [f"{flag}=auto"], [flag, "logical"]]
        unbudgeted = [[f"{flag}=4"], [flag, "4"], [flag, "32"]]
        if short:
            budgeted += [[f"{flag}0"], [f"{flag}1"], [f"{flag}auto"]]
            unbudgeted += [[f"{flag}4"], [f"{flag}32"]]
        for count in budgeted:
            assert mod._argv_worker_pool_is_budgeted(
                ["pytest-3", *count, "test/"]
            ), f"the reader stopped reading {' '.join(count)!r} as budgeted"
        for count in unbudgeted:
            assert not mod._argv_worker_pool_is_budgeted(
                ["pytest-3", *count, "test/"]
            ), f"the reader stopped reading {' '.join(count)!r} as a budget bypass"
        if not short:
            glued = [f"{flag}0"]
            assert mod._argv_worker_pool_is_budgeted(
                ["pytest-3", *glued, "test/"]
            ), f"{glued[0]!r} is a different option, so the run inherits addopts"
    # The value pytest itself rejects at argument parsing: fail-closed, so it reports.
    assert not mod._argv_worker_pool_is_budgeted(["pytest-3", "-n", "4abc", "test/"])
    assert not mod._argv_worker_pool_is_budgeted(["pytest-3", "-n"])


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("9" * 5000, id="more-digits-than-the-interpreter-converts"),
        pytest.param("\u00b2", id="a-character-isdigit-accepts-and-int-refuses"),
        pytest.param("\u0664", id="a-non-ascii-decimal-digit"),
    ],
)
def test_the_count_is_judged_lexically_so_no_value_can_raise(mod, tmp_path, monkeypatch, value):
    """No ``-n`` value, however long or however spelled, raises out of the reader.

    The reader runs inside ``_host_lines`` with nothing above it catching a ``ValueError``,
    so a value the interpreter refuses to convert -- a run of digits past its conversion
    limit, or a character ``str.isdigit`` accepts and ``int`` does not -- ends the patrol
    cycle, and ends it again on every cycle while that pid lives. The count is compared as
    a string instead. Every such value is also a count no budget bounds, so the answer is
    the reporting one; ``-n0`` under the same fixture stays quiet so the reporting is not
    the fixture's.
    """
    assert mod._count_is_budgeted(value) is False
    assert mod._argv_worker_pool_is_budgeted(["pytest-3", "-n", value, "test/"]) is False
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "451", ["pytest-3", "-n", value, "test/"])
    fleet_pid(root, fleet, "452", ["pytest-3", "-n0", "test/"])
    assert banned_pids(mod, root, fleet) == {"451"}


def test_leading_zeros_do_not_change_which_count_is_single_process(mod):
    """``0``, ``00``, ``01`` and ``001`` are the single-process counts pytest reads them as;
    ``02`` and ``010`` are fixed pools of two and ten. Judged on the digits after the
    leading zeros, so the lexical rule agrees with integer conversion everywhere the
    latter would have answered."""
    for single in ("0", "00", "1", "01", "001"):
        assert mod._count_is_budgeted(single) is True, single
    for fixed in ("2", "02", "010", "32"):
        assert mod._count_is_budgeted(fixed) is False, fixed
    assert mod._count_is_budgeted("") is False


def test_the_terminator_ends_the_walk_for_the_runs_own_count(mod):
    """``--`` is where pytest stops reading options, and so does the reader.

    ``pytest -n32 -- -n0`` is a 32-worker run pointed at a path called ``-n0``; a walk
    that read the second token as the count would let that run through on last-wins.
    The converse holds too: ``-n0 -- -n32`` is one process pointed at a path, and
    ``-- -n 4`` names two paths and inherits ``addopts``. A ``--`` inside an
    ``addopts`` override terminates the run's own options as well, because pytest
    prepends the override to them.
    """
    assert mod._argv_worker_pool_is_budgeted(["pytest", "-n32", "--", "-n0"]) is False
    assert mod._argv_worker_pool_is_budgeted(["pytest", "-n0", "--", "-n32"]) is True
    assert mod._argv_worker_pool_is_budgeted(["pytest", "--", "-n", "4"]) is True
    assert mod._argv_worker_pool_is_budgeted(["pytest", "-o", "addopts=--", "-n", "4"]) is True


def test_an_addopts_override_is_walked_before_the_runs_own_tokens(mod, tmp_path, monkeypatch):
    """``-o addopts=...`` / ``--override-ini addopts=...`` carry the count pytest starts from.

    pytest replaces the tree's ``addopts`` with the override and puts its tokens in front
    of the run's own, so ``-o addopts='-n 16'`` is a 16-worker run with no ``-n`` token of
    its own -- a reader that only read the run's own tokens would call it budgeted and
    drop it. The override is folded in first: its ``-n`` is the run's unless a later token
    of the run's own overrides it, the last override of the same key wins, an override of
    another key is not read, an override that supplies no ``-n`` runs one process, and a
    value the splitter refuses (an unbalanced quote, which pytest refuses too) is answered
    fail-closed. The alias path is where the fail-closed answer is observable: the joined
    rule's scan does not cross an unbalanced quote, so ``pytest-3`` carries it.
    """
    reader = mod._argv_worker_pool_is_budgeted
    assert reader(["pytest", "--override-ini=addopts=-n 16", "test/"]) is False
    assert reader(["pytest", "-o", "addopts=-n 16", "test/"]) is False
    assert reader(["pytest", "-oaddopts=-n 16", "test/"]) is False
    assert reader(["pytest", "-o=addopts=-n 16", "test/"]) is False
    assert reader(["pytest", "--override-ini", "addopts=-n 16", "test/"]) is False
    assert reader(["pytest", "-o", "addopts=--numprocesses=16", "test/"]) is False
    assert reader(["pytest", "-o", "addopts=-n auto", "-n", "4"]) is False
    assert reader(["pytest", "-o", "addopts=-n 16", "-n", "auto"]) is True
    assert reader(["pytest", "-o", "addopts=-n 16", "-o", "addopts=-n auto"]) is True
    assert reader(["pytest", "-o", "addopts=-n auto", "-o", "addopts=-n 16"]) is False
    assert reader(["pytest", "-o", "addopts="]) is True
    assert reader(["pytest", "-o", "addopts=-q"]) is True
    assert reader(["pytest", "-o", "testpaths=test", "test/"]) is True
    assert reader(["pytest", "-o", "addopts=-n '16", "test/"]) is False
    assert mod._addopts_override(["-o", "addopts=-n 16", "-n", "auto"]) == ["-n", "16"]
    assert mod._addopts_override(["-n", "4", "test/"]) is None
    assert mod._addopts_override(["--", "-o", "addopts=-n 16"]) is None
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "461", ["pytest-3", "-o", "addopts=-n '16", "test/"])
    fleet_pid(root, fleet, "462", ["pytest-3", "-o", "addopts=-n '16'", "-n", "auto", "test/"])
    assert banned_pids(mod, root, fleet) == {"461"}


def test_the_reader_answers_from_argv_and_does_not_open_a_named_config(mod):
    """``-c other.ini`` names a file; its ``addopts`` are the checkout's business.

    The reader's contract is the command line: a count it can see there is judged, and a
    count that would have to be read out of a file is not -- the probe keys the stop on
    ``cwd=fleet`` for exactly the reason that the checkout decides what ``auto`` means.
    Pinned so the boundary is a stated one: a run naming another config and no count of
    its own inherits that config's ``addopts``, which this reader reports as budgeted, and
    a count it does see under such a run is judged as everywhere else.
    """
    reader = mod._argv_worker_pool_is_budgeted
    assert reader(["pytest", "-c", "other.ini", "test/"]) is True
    assert reader(["pytest", "-c", "other.ini", "-n", "4", "test/"]) is False
    assert reader(["pytest", "-c", "other.ini", "-n0", "test/"]) is True


def test_the_readers_verdict_decides_every_detectable_shape(mod, tmp_path, monkeypatch):
    """THE ASYMMETRY, as an implication: reader says budgeted, so the scan stays silent.

    A false negative costs a signal. A false positive is a fleet-owned row, which is
    ``session_stop`` and a worker's discarded in-flight turn with no automatic recovery.
    So the one thing that must never happen is a row against a run the reader calls
    budgeted -- and the converse is pinned in the same enumeration, so the two paths
    (joined-line rule, argv-only spelling) cannot drift apart on a spelling.

    The count positions are enumerated FROM SOURCE -- every flag in ``_CAP_FLAGS``,
    every spelling around it -- and which of them are budgeted is decided by
    ``_argv_worker_pool_is_budgeted`` itself rather than by a list written here.
    """
    candidates: list[list[str]] = []
    for flag in sorted(mod._CAP_FLAGS):
        candidates.append([f"{flag}0"])
        candidates.append([f"{flag}4"])
        candidates.append([f"{flag}=0"])
        candidates.append([f"{flag}=4"])
        candidates.append([f"{flag}=auto"])
        candidates.append([flag, "0"])
        candidates.append([flag, "1"])
        candidates.append([flag, "4"])
        candidates.append([flag, "auto"])
        candidates.append([flag, "logical"])
    accepted = [
        count
        for count in candidates
        if mod._argv_worker_pool_is_budgeted(["pytest-3", *count, "test/"])
    ]
    rejected = [count for count in candidates if count not in accepted]
    assert accepted, "the reader called no spelling budgeted, so this pin proves nothing"
    assert rejected, "the reader called every spelling budgeted, so this pin proves nothing"

    prefixes = install_prefixes(tmp_path)
    shapes: dict[str, list[str]] = {
        name: with_prefixes(tokens, prefixes) for name, tokens in RUNNER_FORMS.items()
    }
    for name, (launcher, operands) in LAUNCHER_OWN_GRAMMAR.items():
        shapes[f"grammar-{name}"] = [*launcher, *operands, "pytest-3"]

    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir(exist_ok=True)
    pid = 700
    for shape, prefix in sorted(shapes.items()):
        for count in accepted:
            pid += 1
            fleet_pid(root, fleet, str(pid), [*prefix, *count, "test/test_x.py"])
    assert banned_pids(mod, root, fleet) == set(), (
        f"a budgeted run was reported; {len(shapes)} shapes x {len(accepted)} "
        "budgeted spellings must all stay silent"
    )

    # The converse, beside the quiet pids above: every shape the probe can detect,
    # carrying a spelling the reader calls a budget bypass, is reported -- and only those.
    expected = set()
    pid = 5000
    for shape, prefix in sorted(shapes.items()):
        for count in rejected:
            pid += 1
            expected.add(str(pid))
            fleet_pid(root, fleet, str(pid), [*prefix, *count, "test/test_x.py"])
    assert banned_pids(mod, root, fleet) == expected, (
        f"an unbudgeted run went unreported; {len(shapes)} shapes x {len(rejected)} "
        "budget-bypassing spellings must all be reported"
    )


def test_every_argv_only_spelling_is_also_a_recognised_runner_base(mod):
    """A spelling the argv path admits must also be one the CAP check can see.

    `_runner_token_index` keys on `_is_runner_base`, and `_argv_worker_pool_is_budgeted`
    declines to answer (not budgeted) when no runner token stands alone. So a member of
    `_ARGV_ONLY_RUNNER_BASES` that `_is_runner_base` does not recognise is reported
    while its own `-n0` is invisible -- a single-process run drawing a stop, the most
    expensive direction this file has. The invariant is asserted over the whole set
    rather than one token, so adding a spelling cannot reopen it.
    """
    for base in sorted(mod._ARGV_ONLY_RUNNER_BASES):
        assert mod._is_runner_base(base), f"{base} is admitted but its count cannot be read"


@pytest.mark.parametrize("runner", ["py.test", "py.test.exe", "pytest.exe"])
@pytest.mark.parametrize("count", sorted(BUDGETED_SPELLINGS))
def test_a_budgeted_argv_only_spelling_is_never_reported(mod, tmp_path, monkeypatch, runner, count):
    """Every argv-only spelling honours the budget, through the same check as ``pytest``.

    This is the direction that destroys work: the run is budgeted or single-process, and
    a row against it stops a worker doing exactly what the standing directive asks.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "441", [runner, *BUDGETED_SPELLINGS[count], "test/test_x.py"])
    assert banned_pids(mod, root, fleet) == set(), f"{runner} + {count} was reported while budgeted"


def test_vitest_is_left_to_its_own_rule_rather_than_a_pytest_cap_check(mod):
    """``vitest.cmd`` is a runner base and is deliberately not an argv-only shape.

    Its rule spells an uncapped run as ``vitest run`` with nothing following, not as a
    missing ``-n``. Admitting it here would route it through the pytest cap grammar,
    where a bounded vitest run carries no ``-n`` and would be reported as unbounded.
    """
    assert "vitest.cmd" in mod._RUNNER_BASES
    assert "vitest.cmd" not in mod._ARGV_ONLY_RUNNER_BASES
    assert not mod._ALIAS_RUNNER_BASE_RE.match("vitest.cmd")
    # The two spellings whose dot defeats the rule's token boundary ARE admitted, which
    # is the asymmetry this assertion fixes in place: the reason is the cap grammar, not
    # the spelling.
    assert {"py.test", "pytest.exe"} <= mod._ARGV_ONLY_RUNNER_BASES
