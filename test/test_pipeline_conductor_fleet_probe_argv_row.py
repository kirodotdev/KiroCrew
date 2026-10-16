"""Fleet probe (``fleet_probe.py``): the argv runner shape's switch and printed row.

The ``rule=`` and ``scope=`` an argv row carries, the ``argv_runner_detection`` opt-out and
its config validation, custom rule lists leaving the built-in shape on, and ``cmd=``
printing a label rather than an alias token's digits.

Shared fixtures and helpers are in ``pipeline_conductor_fleet_probe_helpers.py``;
``test_pipeline_conductor_fleet_probe.py`` heads the suite and states how it is organised.
"""

from __future__ import annotations

import pytest
from pipeline_conductor_fleet_probe_helpers import mod_fixture  # noqa: F401 - fixture
from pipeline_conductor_fleet_probe_helpers import UNBUDGETED, banned_pids, fleet_pid, host_proc


def test_the_argv_row_names_the_argv_path_rather_than_a_rule_that_did_not_fire(
    mod, tmp_path, monkeypatch
):
    """``rule=`` on an alias row names the argv path, not the pytest pattern.

    The pattern genuinely did not match -- the alias is invisible to it by design --
    so printing it would send a reader to a lookahead that is working correctly.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "405", ["pytest-3", *UNBUDGETED, "test/"])
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert len(lines) == 1
    assert f"rule={mod.ARGV_RUNNER_RULE_LABEL}" in lines[0]
    assert mod.ARGV_RUNNER_RULE_LABEL not in mod.DEFAULT_BANNED_RES
    # The label has to survive being read as one whitespace-separated field on a line
    # the conductor parses, so it carries no space and nothing that reopens a field.
    assert not any(ch in mod.ARGV_RUNNER_RULE_LABEL for ch in " \t\"'`")


def test_an_alias_run_reports_its_scope_instead_of_declining(mod, tmp_path, monkeypatch):
    """``scope=`` answers for an alias run too.

    Scope keys on the runner's own token standing alone in argv. While the alias was
    absent from that set, every alias row printed ``scope=unknown`` -- the readout that
    says the probe could not tell a whole-suite run from a one-file one, on exactly the
    rows where it matters.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "406", ["pytest-3", *UNBUDGETED, "test/test_x.py"])
    fleet_pid(root, fleet, "407", ["pytest-3", *UNBUDGETED, "--cov", "src/kiro_crew"])
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    scopes = {
        line.split()[1].split("=", 1)[1]: line.split("scope=", 1)[1].split()[0] for line in lines
    }
    assert scopes == {"406": "paths", "407": "suite"}


def test_the_named_opt_out_switches_the_argv_shape_off(mod, tmp_path, monkeypatch):
    """An operator CAN disable the argv shape, by saying so about the argv shape.

    Without an escape hatch this detection is an absolute an operator cannot decline,
    which is a real complaint. With one it is policy -- and the key names the thing it
    disables, so nobody removes it while meaning to change something else.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "450", ["pytest-3", *UNBUDGETED, "test/"])
    off, _host = mod._host_lines({"fleet_worktrees": [str(fleet)], "argv_runner_detection": False})
    assert [line for line in off if line.startswith("BANNED pid=")] == []
    # The control: the same pid with the key absent IS reported, so the silence is the
    # opt-out and not a broken fixture.
    assert banned_pids(mod, root, fleet) == {"450"}


def test_the_opt_out_and_the_rule_list_are_independent(mod, tmp_path, monkeypatch):
    """Neither setting reaches the other, in either direction.

    This is the whole point of the separation. Replacing ``banned_process_res`` must not
    disable the argv shape -- a protection removed as a side effect of an unrelated edit
    is removed by someone who never decided to remove it. And disabling the argv shape
    must not disable the operator's own rules.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "451", ["pytest-3", *UNBUDGETED, "test/"])
    fleet_pid(root, fleet, "452", ["npm", "audit", "--json"])
    custom = [r"\bnpm\b\s+audit"]

    def reported(cfg):
        lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)], **cfg})
        return {
            line.split()[1].split("=", 1)[1] for line in lines if line.startswith("BANNED pid=")
        }

    # A replaced rule list leaves the argv shape ON.
    assert reported({"banned_process_res": custom}) == {"451", "452"}
    # The opt-out leaves the operator's own rule in force.
    assert reported({"banned_process_res": custom, "argv_runner_detection": False}) == {"452"}
    # The opt-out alone leaves the built-in rules in force; 452 is not an npm-audit
    # match under the defaults, so only the argv row disappears.
    assert reported({"argv_runner_detection": False}) == set()
    assert reported({}) == {"451"}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("false", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(1, id="one"),
        pytest.param([], id="empty-list"),
        pytest.param({}, id="empty-dict"),
    ],
)
def test_a_non_boolean_opt_out_is_malformed_config(mod, value):
    """A near-miss value is refused at load, never guessed at.

    ``"false"`` is not False, so a permissive read would leave the shape ON for an
    operator who believes they switched it off. A falsey read would switch a protection
    off for one who wrote nothing of the kind. Both directions are silent, so the value
    has to be a real boolean.
    """
    problem = mod._config_error({"argv_runner_detection": value})
    assert problem == "argv_runner_detection must be true or false"


@pytest.mark.parametrize("value", [True, False, None])
def test_a_boolean_or_absent_opt_out_is_valid_config(mod, value):
    """Both booleans and omission are accepted, so the check does not reject real use."""
    cfg = {} if value is None else {"argv_runner_detection": value}
    assert mod._config_error(cfg) is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="explicit-null"),
        pytest.param(0, id="zero"),
        pytest.param("", id="empty-string"),
        pytest.param([], id="empty-list"),
    ],
)
def test_a_value_that_is_not_false_never_disables_the_shape(mod, tmp_path, monkeypatch, value):
    """Only ``false`` disables it. Anything merely falsey leaves the protection ON.

    An explicit ``null`` is the live case: config validation treats it as "not set", so
    it reaches here, and a permissive truthiness read would switch the shape off for an
    operator who wrote nothing of the kind. The other values cannot pass validation, so
    for them this is the fail-safe direction held one layer deeper -- a caller reaching
    ``_host_lines`` without validating still keeps the protection.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "453", ["pytest-3", *UNBUDGETED, "test/"])
    lines, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "argv_runner_detection": value}
    )
    reported = {
        line.split()[1].split("=", 1)[1] for line in lines if line.startswith("BANNED pid=")
    }
    assert reported == {"453"}, f"{value!r} disabled the shape without saying false"


def test_a_custom_rule_list_does_not_switch_off_the_argv_shape(mod, tmp_path, monkeypatch):
    """Rule ORIGIN carries built-in authority, not the absence of custom config.

    ``test_pipeline_conductor_agent.py`` already writes this down for the wrapper
    exemption: the gate is written against the rule that MATCHED rather than against
    ``cfg["banned_process_res"]`` being set at all. Standing the argv shape down whenever
    an operator supplies a list would let a config EDIT switch a built-in protection off,
    which is a worse property than one extra line on a replaced policy -- and the line it
    emits is factually true of the process, an unbudgeted alias run in a fleet worktree.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "430", ["pytest-3", *UNBUDGETED, "test/"])
    custom, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "banned_process_res": [r"\bnpm\b\s+audit"]}
    )
    reported = {
        line.split()[1].split("=", 1)[1] for line in custom if line.startswith("BANNED pid=")
    }
    assert reported == {"430"}, "a custom rule list silenced the argv shape"
    # The same pid under the defaults, so the assertion above is about authority rather
    # than about the fixture happening to report everything.
    assert banned_pids(mod, root, fleet) == {"430"}


def test_a_custom_rule_list_still_reports_its_own_shape(mod, tmp_path, monkeypatch):
    """The operator's own rules keep working alongside the built-in shape.

    The two authorities are additive in effect even though the config REPLACES the
    pattern list: a custom rule reports what it names, and the argv shape reports what
    this file detects on its own authority.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "431", ["npm", "audit", "--json"])
    lines, _host = mod._host_lines(
        {"fleet_worktrees": [str(fleet)], "banned_process_res": [r"\bnpm\b\s+audit"]}
    )
    reported = {
        line.split()[1].split("=", 1)[1] for line in lines if line.startswith("BANNED pid=")
    }
    assert reported == {"431"}


@pytest.mark.parametrize(
    ("argv", "program"),
    [
        pytest.param(["pytest-3", "-n4", "test/"], "pytest-<version>", id="alias"),
        pytest.param(["pytest-3.12", "-n4", "test/"], "pytest-<version>", id="alias-minor"),
        pytest.param(["/usr/bin/pytest-3", "-n4", "test/"], "pytest-<version>", id="alias-abspath"),
        pytest.param(["py.test-3", "-n4", "test/"], "py.test-<version>", id="alias-py.test"),
        pytest.param(["pytest-3.exe", "-n4", "test/"], "pytest-<version>", id="alias-exe"),
        pytest.param(["py.test", "-n4", "test/"], "py.test", id="py.test"),
        pytest.param(["pytest.exe", "-n4", "test/"], "pytest.exe", id="pytest.exe"),
    ],
)
def test_an_argv_row_prints_the_program_it_fired_on(mod, tmp_path, monkeypatch, argv, program):
    """``cmd=`` names the runner on an argv row, because ``rule=`` cannot.

    A joined-line row can be judged from its rule text. An argv row names a shape rather
    than a pattern, so the program name is the only field that separates a real uncapped
    run from a command that merely spells one -- and it is the field the owning skill
    says to read before stopping anyone.

    A versioned alias prints as a fixed LABEL rather than as itself, because the pattern
    admitting it is a shape and a shape cannot bound what follows its stem. The label says
    which stem fired, which is what the field is for, and carries none of the digits.
    """
    root = host_proc(tmp_path, monkeypatch)
    fleet = tmp_path / "wt"
    fleet.mkdir()
    fleet_pid(root, fleet, "440", argv)
    lines, _host = mod._host_lines({"fleet_worktrees": [str(fleet)]})
    assert len(lines) == 1
    cmd = lines[0].split("cmd=", 1)[1].split()[0]
    assert cmd.split(",")[0] == program, f"cmd= withheld the program name: {cmd}"


@pytest.mark.parametrize(
    "secret",
    [
        "pytest-123456",
        "pytest-9",
        "pytest-0.0.0.0.1",
        "py.test-99999999",
        "pytest-123456.exe",
        "pytest-" + "1" * 40,
    ],
)
def test_no_digits_a_command_carried_ever_reach_the_printed_command(mod, secret):
    """A token the alias SHAPE admits is printed as a label, so its text cannot escape.

    The pattern admitting a versioned alias is anchored at both ends but open in the
    middle: it accepts ``pytest-`` followed by any digits, so a secret of that spelling
    satisfies it exactly as ``pytest-3.12`` does. The row lands in the conductor's model
    context, so echoing the token would carry the secret there.

    Each case is asserted to BE an alias by the pattern first, so a future narrowing of
    the pattern makes this test stop proving nothing rather than silently pass.
    """
    assert mod._ALIAS_RUNNER_BASE_RE.match(
        secret
    ), f"{secret} is not alias-shaped, so it proves nothing"
    cmd = f"pytest -q --token {secret} test/x.py"
    out = mod._redacted_command(cmd, (0, len(cmd)))
    digits = secret.partition("-")[2]
    assert digits not in out, f"the token's own text reached cmd=: {out}"
    assert mod._alias_program_label(secret) in out.split(","), f"no label stood in for it: {out}"


def test_every_alias_the_pattern_admits_has_a_label(mod):
    """A stem the pattern admits with no label would fall back to echoing the token.

    The label lookup and the pattern are two lists that must agree, so this asserts the
    agreement structurally rather than by naming today's two stems.
    """
    for stem, _label in mod._ALIAS_PROGRAM_LABELS:
        assert mod._ALIAS_RUNNER_BASE_RE.match(f"{stem}3"), f"{stem} is labelled but not admitted"
    for spelling in ("pytest-3", "py.test-3", "pytest-3.12.4", "py.test-12.0"):
        assert (
            mod._alias_program_label(spelling) is not None
        ), f"{spelling} is admitted but not labelled"
    # The lookup is gated on the PATTERN, not on the stem alone. Today's caller only
    # reaches it for a token the pattern already admitted, so this cannot be observed
    # through `cmd=`; it is asserted directly so a future caller admitting a wider set
    # cannot obtain a runner label for a word that is not a versioned alias.
    for not_an_alias in ("pytest-abc", "pytest-", "py.test-v3", "pytest-3x", "pytest"):
        assert mod._alias_program_label(not_an_alias) is None, f"{not_an_alias} got a runner label"
