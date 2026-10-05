"""The denial differential must fail on a newly-refused golden path, and only then.

``scripts/deny_diff.py`` answers one question about a deny-rule change: does the
tightened rule now refuse an operation the product depends on? The answer is a
before/after classification, so the thing that can silently break is the
COMPARISON -- a gate that classifies one tree twice, or that reads a loosening as
a regression, reports a confident verdict about nothing.

So the exit-code paths are proven against two hand-built classifier trees rather
than against the real matcher. The real one is what the gate measures; making it
also the fixture would mean the tests could only assert whatever it happens to do
today, and a regression could not be staged at all. The fakes travel the SAME
subprocess path as production -- ``PYTHONPATH`` at a materialized tree, one child
per side, verdicts diffed in the parent -- with only the ref-to-checkout resolver
replaced, which is why an exit code proven here is the exit code CI produces.

The fakes carry all three deny checks the tool gate applies to a shell command's
text, because covering only the rule catalog is the specific way this gate could
ship a meaningless green: a tightening of the bash scan or the exfil shapes runs it
(its trigger paths include both) and a catalog-only differential would come back
empty.
One test stages a regression on each non-catalog tier, and another pins the measured
table to the gate's own ``hooks.SHELL_DENY_TIERS`` -- so the fidelity claim is pinned
against the gate it claims to measure rather than asserted in a comment.

Two tests then run the real composite over the corpus the gate itself classifies
-- the security-conductor's committed ``golden-paths.json``, which is also what
``verify_fix.py`` reads, so the fixer's acceptance gate and this one cannot judge a
change against two corpora that disagree. ``base == head == HEAD`` must find zero
regressions, and no corpus row may be refused at HEAD at all. Those are the
properties the fakes cannot check -- that the corpus names operations the shipped
rules actually allow, so a red on a future PR means that PR tightened something
rather than that the corpus was wrong when written.

The gate has no waiver mechanism to test. A row that stops being a golden path is
withdrawn in its own pull request, which is what the base-owned corpus makes
possible.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.agent_sdk.host_auth import home_override_env_vars
from kiro_crew.subprocess_utf8 import UTF8_TEXT

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deny_diff.py"
GOLDEN_PATHS = (
    ROOT / "src" / "kiro_crew" / "builtin_skills" / "security-conductor" / "golden-paths.json"
)


def _load(name: str, path: Path):
    """Import a repo script by path.

    Registered in ``sys.modules`` BEFORE exec: a script's dataclasses resolve their
    own (string) annotations through ``sys.modules[cls.__module__]``, so a module
    executed without a registration raises at class-creation time rather than at
    use.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


deny_diff = _load("deny_diff", SCRIPT)


#: The ``GIT_*`` location variables a hook, wrapper or parent runner may have
#: exported; inherited by a child they redirect ``git -C <repo>`` away from <repo>.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


def _hermetic_git(monkeypatch) -> None:
    """Confine every real ``git`` the script spawns to the repository it names.

    The script's git spawns inherit this process's environment. Two things in it
    must not reach them: the operator's global and system config (a ``core.hooksPath``
    or ``core.fsmonitor`` there would execute a program from inside ``git archive``),
    and any ``GIT_*`` location override, which would make ``-C <repo>`` answer about
    some other repository. Set through ``monkeypatch`` so it unwinds with the test.
    """
    for name in _GIT_LOCATION_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


def _scratch_repo(tmp_path: Path) -> Path:
    """A one-commit repository under *tmp_path* for a test that needs a real ref.

    Built with the hermetic environment above already in place (callers apply
    ``_hermetic_git`` first) and with ``cwd`` inside the temp tree, so the only
    repository git can see or write is this one.
    """
    repo = tmp_path / "scratch-repo"
    (repo / "src" / "kiro_crew").mkdir(parents=True)
    (repo / "src" / "kiro_crew" / "__init__.py").write_text("", encoding="utf-8")
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for args in (
        ["init", "-q", "-b", "main", "."],
        ["add", "src"],
        [*identity, "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, **UTF8_TEXT)
    return repo


#: A whole deny composite, in the shape the worker imports it: a package under
#: ``src/`` exposing the four checks the tool gate applies. Each fake refuses the
#: commands assigned to its own tier, which is how a test can stage a regression on
#: a tier other than the rule catalog and watch the differential attribute it.
_FAKE_HEADER = "TIERS = {tiers!r}\n"

#: Per-tier source, so a tree can OMIT one. A ref that predates a deny check has no
#: such attribute at all, which is a different tree from one whose check allows
#: everything -- and the two must not be conflated.
_FAKE_TIER_SOURCE = {
    "sensitive-bash": """

def is_sensitive_bash_command(command, *, enabled_ids=None):
    return "Blocked: fake sensitive bash" if command in TIERS["sensitive-bash"] else None
""",
    "exfil": """

def audit_bash_exfiltration(command, *, enabled_ids=None):
    return "Blocked: fake exfil shape" if command in TIERS["exfil"] else None
""",
    "deny-rules": """

def is_denied(command, *args, **kwargs):
    return "Blocked by security policy: fake rule" if command in TIERS["deny-rules"] else None
""",
}

_TIER_NAMES = ("sensitive-bash", "exfil", "deny-rules")


def _fake_tree(
    root: Path,
    denied: set[str] | dict[str, set[str]],
    *,
    omit: tuple[str, ...] = (),
) -> Path:
    """A minimal importable ``kiro_crew.security`` under *root*.

    *denied* is either a bare set (assigned to the rule-catalog tier, the common
    case) or a per-tier mapping, which is how a non-catalog regression is staged.
    *omit* leaves those tiers' functions out of the tree entirely, which is what a
    ref predating a deny check looks like.
    """
    tiers: dict[str, list[str]] = {name: [] for name in _TIER_NAMES}
    if isinstance(denied, dict):
        for name, commands in denied.items():
            tiers[name] = sorted(commands)
    else:
        tiers["deny-rules"] = sorted(denied)

    source = _FAKE_HEADER.format(tiers=tiers) + "".join(
        body for name, body in _FAKE_TIER_SOURCE.items() if name not in omit
    )
    package = root / "src" / "kiro_crew" / "security"
    package.mkdir(parents=True, exist_ok=True)
    (root / "src" / "kiro_crew" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text(source, encoding="utf-8")
    return root


def _corpus(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps({"golden_paths": rows}), encoding="utf-8")
    return path


def _shell(command: str, platform: str = "any") -> dict:
    return {
        "kind": "shell",
        "command_or_flow": command,
        "platform": platform,
        "reason": f"golden path: {command}",
    }


@pytest.fixture()
def staged(tmp_path, monkeypatch):
    """Stage two classifier trees and point the resolver at them.

    Returns a callable taking the corpus rows and returning the report. The
    resolver is monkeypatched on the MODULE, which is the same seam production
    reads, so nothing about the child spawn, the environment scrub or the verdict
    parsing is bypassed. The provenance lookup is pinned beside it: with the trees
    hand-built, ``BASE``/``HEAD`` are labels rather than refs, and resolving them
    would spawn the host's git against the real checkout to decorate a report whose
    shas these tests never read.
    """
    runs = 0
    monkeypatch.setattr(deny_diff, "_rev_parse", lambda repo_root, ref: ref)

    def run(
        rows: list[dict],
        *,
        base_denies,
        head_denies,
        platform: str = "posix",
        base_omits: tuple[str, ...] = (),
        head_omits: tuple[str, ...] = (),
    ):
        nonlocal runs
        runs += 1
        base_tree = _fake_tree(tmp_path / f"base-tree-{runs}", base_denies, omit=base_omits)
        head_tree = _fake_tree(tmp_path / f"head-tree-{runs}", head_denies, omit=head_omits)

        def resolver(repo_root: Path, ref: str, dest: Path) -> Path:
            return base_tree if ref == "BASE" else head_tree

        monkeypatch.setattr(deny_diff, "resolve_checkout", resolver)
        workdir = tmp_path / f"work-{runs}"
        workdir.mkdir()
        return deny_diff.differential(
            ROOT,
            "BASE",
            "HEAD",
            _corpus(workdir / "corpus.json", rows),
            platform=platform,
            workdir=workdir,
        )

    return run


def test_newly_refused_golden_path_is_a_regression(staged):
    """A row the base allows and the head refuses fails the gate (exit 1)."""
    push = "git push origin feat/x"
    report = staged(
        [_shell("gh pr view 1 --json state"), _shell(push)],
        base_denies=set(),
        head_denies={push},
    )

    assert report.exit_code == 1
    assert [row.command for row, _ in report.regressions] == [push]
    assert report.loosenings == []
    assert report.unchanged_allowed == 1

    text = deny_diff.render_text(report)
    assert "Regressions -- newly refused at head (1)" in text
    # The report has to carry WHY the row is legitimate, not just that it broke:
    # the reader's next action is to judge the rule against the reason.
    assert f"legitimate because: golden path: {push}" in text
    assert "head refuses at the `deny-rules` tier" in text


@pytest.mark.parametrize("tier", ["sensitive-bash", "exfil"])
def test_a_regression_on_a_non_catalog_tier_is_caught_and_named(staged, tier):
    """The exfil shapes are a deny tier too, and this gate runs on them.

    A catalog-only differential reads a tightening of ``paths.py`` or ``exfil.py``
    as clean -- both are inside this workflow's trigger paths -- and the green then
    stands as evidence the question was asked.
    """
    command = "git status --porcelain"
    report = staged([_shell(command)], base_denies={}, head_denies={tier: {command}})

    assert report.exit_code == 1
    assert [(row.command, verdict.tier) for row, verdict in report.regressions] == [(command, tier)]
    assert f"head refuses at the `{tier}` tier" in deny_diff.render_text(report)


def test_tier_order_matches_the_tool_gate(staged):
    """When several tiers refuse, the reported one is the first the gate would hit."""
    command = "ls -la src"
    report = staged(
        [_shell(command)],
        base_denies={},
        head_denies={"exfil": {command}, "deny-rules": {command}, "sensitive-bash": {command}},
    )

    assert [verdict.tier for _, verdict in report.regressions] == ["sensitive-bash"]


def test_a_tier_this_change_adds_is_measured_against_a_base_that_lacks_it(staged):
    """The gate's own primary use case: a PR that adds a deny check.

    The worker is this file at head, so it iterates head's tier table against
    whichever tree it was pointed at -- and the base tree of a check-adding PR has no
    such attribute. Refusing there would exit 2 on exactly the tightening the gate
    exists to measure. A check that did not exist at base refused nothing at base, so
    the row's refusal at head is a regression and is reported as one.
    """
    command = "git status --porcelain"
    report = staged(
        [_shell(command)],
        base_denies={},
        head_denies={"exfil": {command}},
        base_omits=("exfil",),
    )

    assert report.exit_code == 1
    assert [(row.command, verdict.tier) for row, verdict in report.regressions] == [
        (command, "exfil")
    ]
    assert report.base_absent_tiers == ["exfil"]

    # The reader has to be told, or a whole tier's worth of rows turning up at once
    # looks like the differential misfiring.
    text = deny_diff.render_text(report)
    assert "this change ADDS the `exfil` tier(s)" in text
    assert json.loads(deny_diff.render_json(report))["counts"]["base_absent_tiers"] == ["exfil"]


def test_a_tier_missing_at_head_is_an_error_not_a_skip(staged):
    """At head the same absence is coverage silently lost, so it must not pass."""
    with pytest.raises(deny_diff.DenyDiffError) as caught:
        staged(
            [_shell("git status --porcelain")],
            base_denies={},
            head_denies={},
            head_omits=("exfil",),
        )
    assert "audit_bash_exfiltration is absent" in str(caught.value)


def test_the_measured_checks_are_the_checks_the_tool_gate_applies():
    """The differential measures the gate's own table, not a copy of it.

    The gate's whole claim is that a green means "no golden path is newly refused AT
    THE TOOL GATE". That holds only while the checks measured here are the ones the
    gate applies to a shell command, so the reader returns ``hooks.SHELL_DENY_TIERS``
    exactly -- the shell projection of ``hooks.GATE_TIERS`` -- read as text from the
    tree the harness runs from.
    """
    from kiro_crew import hooks

    measured = deny_diff.load_shell_deny_tiers(ROOT)
    assert measured == hooks.SHELL_DENY_TIERS
    assert measured == tuple((t.name, t.shell_rule) for t in hooks.shell_rules(hooks.GATE_TIERS))
    # No path tier: the gate spares a sandboxed shell's own command text from the
    # path rule, so a shell corpus row can only be refused by these.
    assert [name for name, _ in measured] == ["sensitive-bash", "exfil", "deny-rules"]


def _tree_with_hooks(root: Path, text: str) -> Path:
    (root / "src" / "kiro_crew").mkdir(parents=True, exist_ok=True)
    (root / "src" / "kiro_crew" / "hooks.py").write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("text", "symptom"),
    [
        (None, "cannot read the tool gate's tier table"),
        ("x = (\n", "cannot read the tool gate's tier table"),
        ("OTHER = ()\n", "declares no SHELL_DENY_TIERS"),
        ("SHELL_DENY_TIERS = tuple(TABLE)\n", "is not a literal"),
        ("SHELL_DENY_TIERS = ()\n", "is not a non-empty tuple"),
        ("SHELL_DENY_TIERS = (('exfil',),)\n", "is not a non-empty tuple"),
        ("SHELL_DENY_TIERS = [('exfil', 'audit_bash_exfiltration')]\n", "is not a non-empty tuple"),
    ],
)
def test_an_unreadable_tier_table_measured_nothing(tmp_path, text, symptom):
    """Every way the table can fail to read is an error (exit 2), never a table."""
    root = tmp_path / "harness"
    if text is None:
        root.mkdir()
    else:
        _tree_with_hooks(root, text)
    with pytest.raises(deny_diff.DenyDiffError) as caught:
        deny_diff.load_shell_deny_tiers(root)
    assert symptom in str(caught.value)


def test_the_table_read_is_the_harness_tree_s_and_typed_either_way(tmp_path):
    """An annotated or a bare assignment reads the same, and nothing else in the file
    is evaluated -- the parent never imports the product."""
    root = _tree_with_hooks(
        tmp_path / "harness",
        "raise SystemExit('imported')\n"
        "SHELL_DENY_TIERS: tuple[tuple[str, str], ...] = (\n"
        "    ('exfil', 'audit_bash_exfiltration'),\n"
        ")\n",
    )
    assert deny_diff.load_shell_deny_tiers(root) == (("exfil", "audit_bash_exfiltration"),)


def test_importing_the_harness_alone_reads_no_tier_table(tmp_path):
    """The scope lanes stage ``deny_diff.py`` ALONE into an empty directory and
    import it for its row schema; reading the table at import would break them."""
    lone = tmp_path / "harness" / "deny_diff.py"
    lone.parent.mkdir()
    lone.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    module = _load("deny_diff_alone", lone)
    try:
        assert module.load_corpus(GOLDEN_PATHS)
    finally:
        sys.modules.pop("deny_diff_alone", None)


def test_both_sides_are_measured_against_the_same_table(staged, monkeypatch):
    """One table, the harness's, for both refs: a per-tree table would read a tier a
    change removes as a loosening instead of measuring it."""
    seen: list[tuple[str, object]] = []
    real = deny_diff.classify

    def recording(checkout, commands, *, home, side="head", tiers=None):
        seen.append((side, tiers))
        return real(checkout, commands, home=home, side=side, tiers=tiers)

    monkeypatch.setattr(deny_diff, "classify", recording)
    staged([_shell("git status --porcelain")], base_denies=set(), head_denies=set())
    from kiro_crew import hooks

    assert seen == [("base", hooks.SHELL_DENY_TIERS), ("head", hooks.SHELL_DENY_TIERS)]


@pytest.mark.parametrize(
    ("tiers", "symptom"),
    [(None, "carries no tier table"), ([], "carries an empty tier table")],
)
def test_a_worker_request_without_a_table_is_refused(tmp_path, tiers, symptom):
    """The child measures only the table it is handed; none -- or an empty one, which
    would report every command allowed on both sides -- is an error, not a green."""
    _fake_tree(tmp_path / "real", set())
    request: dict[str, object] = {
        "commands": ["ls"],
        "expect_root": str((tmp_path / "real" / "src").resolve()),
    }
    if tiers is not None:
        request["tiers"] = tiers
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), deny_diff._WORKER_FLAG],
        input=json.dumps(request),
        cwd=tmp_path,
        capture_output=True,
        env=deny_diff._child_env(tmp_path / "real", tmp_path / "home"),
        **UTF8_TEXT,
    )
    assert proc.returncode == 2
    assert symptom in proc.stderr


def test_an_empty_table_is_never_handed_to_a_worker(tmp_path):
    with pytest.raises(deny_diff.DenyDiffError) as caught:
        deny_diff.classify(tmp_path, ["ls"], home=tmp_path / "home", tiers=())
    assert "measures nothing" in str(caught.value)


def test_loosening_only_passes_and_is_reported(staged):
    """A row the base refuses and the head allows is informational, not a failure."""
    listing = "ls -la src"
    report = staged(
        [_shell(listing), _shell("git status --porcelain")],
        base_denies={listing},
        head_denies=set(),
    )

    assert report.exit_code == 0
    assert report.regressions == []
    assert [row.command for row, _ in report.loosenings] == [listing]

    text = deny_diff.render_text(report)
    assert "No regressions" in text
    assert "Loosenings -- newly allowed at head (1), informational" in text
    assert f"`{listing}`" in text


def test_unchanged_rows_are_counted_on_both_sides(staged):
    """Refused-at-both is not a regression, and is reported apart from allowed-at-both."""
    blocked = "rm -rf /"
    report = staged(
        [_shell(blocked), _shell("git log --oneline -5")],
        base_denies={blocked},
        head_denies={blocked},
    )

    assert report.exit_code == 0
    assert (report.unchanged_denied, report.unchanged_allowed) == (1, 1)
    assert "Unchanged: 1 allowed at both refs, 1 refused at both." in deny_diff.render_text(report)


def test_platform_filter_selects_only_matching_rows(staged):
    """A windows-only row is not classified on posix -- and cannot fail the gate there."""
    win_only = "Get-ChildItem -Path src"
    rows = [_shell(win_only, platform="windows"), _shell("ls -la src", platform="posix")]

    posix = staged(rows, base_denies=set(), head_denies={win_only}, platform="posix")
    assert posix.exit_code == 0
    assert posix.skipped_platform == 1
    assert posix.classified == 1

    windows = staged(rows, base_denies=set(), head_denies={win_only}, platform="windows")
    assert windows.exit_code == 1
    assert [row.command for row, _ in windows.regressions] == [win_only]
    assert windows.skipped_platform == 1


def test_non_shell_rows_are_skipped_not_dropped(staged):
    """Flow and cron rows are reported as skipped, so a corpus of them says so."""
    report = staged(
        [
            _shell("git fetch origin main"),
            {"kind": "flow", "command_or_flow": "chat start", "platform": "any", "reason": "r"},
            {"kind": "cron", "command_or_flow": "scanner", "platform": "any", "reason": "r"},
        ],
        base_denies=set(),
        head_denies=set(),
    )

    assert (report.skipped_kind, report.classified, report.total_rows) == (2, 1, 3)
    assert "2 non-shell" in deny_diff.render_text(report)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("{ not json", "not valid JSON"),
        ('{"unrelated": []}', "without a 'golden_paths' key"),
        ('{"golden_paths": []}', "holds no rows"),
        ('{"golden_paths": ["a string"]}', "row 0 is not an object"),
        ('{"golden_paths": [{"kind": "spell", "command_or_flow": "x"}]}', "has kind 'spell'"),
        ('{"golden_paths": [{"kind": "shell", "command_or_flow": "  "}]}', "no non-empty"),
        # A row spelling the command under any other key is a MALFORMED row, not a
        # tolerated alias: silently classifying nothing is the false green.
        ('{"golden_paths": [{"kind": "shell", "command": "ls"}]}', "no non-empty"),
        (
            '{"golden_paths": [{"kind": "shell", "command_or_flow": "x", "platform": "vms"}]}',
            "has platform 'vms'",
        ),
        ('{"golden_paths": 7}', "must hold a list of rows"),
    ],
)
def test_malformed_corpus_is_an_error_not_a_verdict(tmp_path, payload, expected):
    """Every corpus defect exits 2. Silently classifying a subset would be a false green."""
    corpus = tmp_path / "corpus.json"
    corpus.write_text(payload, encoding="utf-8")

    with pytest.raises(deny_diff.DenyDiffError) as caught:
        deny_diff.load_corpus(corpus)
    assert expected in str(caught.value)

    assert deny_diff.main(["--base", "HEAD", "--head", "HEAD", "--corpus", str(corpus)]) == 2


def test_missing_corpus_file_exits_two(tmp_path):
    missing = tmp_path / "absent.json"
    assert deny_diff.main(["--base", "HEAD", "--head", "HEAD", "--corpus", str(missing)]) == 2


def test_unresolvable_ref_exits_two(tmp_path, monkeypatch):
    """A ref that cannot be archived is an environment error, never 'no regressions'.

    Against a scratch repository rather than this checkout: the property is git's
    refusal of an unknown ref, which any repository exhibits, and the script's
    ``_repo_root`` is the seam that decides which one it asks.
    """
    _hermetic_git(monkeypatch)
    repo = _scratch_repo(tmp_path)
    monkeypatch.setattr(deny_diff, "_repo_root", lambda: repo)
    corpus = _corpus(tmp_path / "corpus.json", [_shell("git status --porcelain")])
    code = deny_diff.main(
        ["--base", "refs/heads/no-such-ref-deny-diff", "--head", "HEAD", "--corpus", str(corpus)]
    )
    assert code == 2


def test_child_environment_is_scrubbed_of_crew_variables(tmp_path, monkeypatch):
    """The verdict must not be a function of the caller's environment."""
    monkeypatch.setenv("HOME", str(tmp_path / "real-os-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real-windows-home"))
    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "real-home"))
    for name in deny_diff._INHERITED_HOME_OVERRIDE_ENV_VARS:
        monkeypatch.setenv(name, str(tmp_path / name.lower()))

    throwaway = tmp_path / "throwaway-home"
    env = deny_diff._child_env(tmp_path / "checkout", throwaway)

    assert "KIROCREW_SANDBOX_ACTIVE" not in env
    assert env["HOME"] == str(throwaway)
    assert env["USERPROFILE"] == str(throwaway)
    assert env["KIROCREW_HOME"] == str(throwaway)
    assert env["PYTHONPATH"] == str(tmp_path / "checkout" / "src")
    assert throwaway.is_dir()
    assert not set(deny_diff._INHERITED_HOME_OVERRIDE_ENV_VARS) & env.keys()


def test_child_environment_scrubs_every_declared_home_override():
    assert set(deny_diff._INHERITED_HOME_OVERRIDE_ENV_VARS) == {
        "KIRO_HOME",
        *home_override_env_vars(),
    }


def test_worker_refuses_a_tree_it_was_not_pointed_at(tmp_path):
    """The shadowing guard: one tree answering for both refs is an error, not a green.

    Without this the differential's failure mode is invisible -- both sides would
    load the same installed package and every comparison would come back empty.
    """
    _fake_tree(tmp_path / "real", set())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    request = {"commands": ["ls"], "expect_root": str((elsewhere / "src").resolve())}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), deny_diff._WORKER_FLAG],
        input=json.dumps(request),
        cwd=tmp_path,
        capture_output=True,
        env=deny_diff._child_env(tmp_path / "real", tmp_path / "home"),
        **UTF8_TEXT,
    )

    assert proc.returncode == 2
    assert "outside" in proc.stderr


def test_platform_auto_resolves_to_the_running_host(monkeypatch):
    monkeypatch.setattr(deny_diff.os, "name", "nt")
    assert deny_diff.resolve_platform("auto") == "windows"
    monkeypatch.setattr(deny_diff.os, "name", "posix")
    assert deny_diff.resolve_platform("auto") == "posix"
    # An explicit selector is never overridden by the host.
    assert deny_diff.resolve_platform("windows") == "windows"


def test_json_rendering_carries_the_rows_and_their_tiers(staged):
    """The machine surface of the report, for a caller that wants rows rather than prose.

    CI reads the text rendering; this is the shape a local caller or a later consumer
    gets, and it has to carry the same three facts the prose does -- the command, why
    it is a golden path, and which tier refused it.
    """
    push = "git push origin feat/x"
    report = staged([_shell(push)], base_denies=set(), head_denies={push})

    payload = json.loads(deny_diff.render_json(report))

    assert payload["exit_code"] == 1
    assert payload["counts"]["regressions"] == 1
    assert payload["regressions"][0]["command"] == push
    assert payload["regressions"][0]["why_legitimate"] == f"golden path: {push}"
    assert payload["regressions"][0]["head_tier"] == "deny-rules"
    assert "fake rule" in payload["regressions"][0]["head_refusal"]


def test_real_composite_finds_no_regressions_between_head_and_itself(tmp_path, monkeypatch):
    """The corpus names operations the SHIPPED rules allow.

    base == head means every verdict is identical by construction, so this cannot
    fail on a comparison bug -- it fails when a row of the corpus is not actually a
    golden path under the current rules, or when the harness cannot materialize a
    ref and classify it at all. Both are things the fake trees never touch.

    This one deliberately archives THIS checkout's HEAD -- that is the property --
    so the real git it spawns runs under the hermetic environment.
    """
    _hermetic_git(monkeypatch)
    code = deny_diff.main(
        ["--base", "HEAD", "--head", "HEAD", "--corpus", str(GOLDEN_PATHS), "--json"]
    )
    assert code == 0


def test_corpus_rows_are_all_allowed_by_the_shipped_composite(tmp_path, monkeypatch):
    """Stronger than the differential above: no row is refused at HEAD at all.

    A row refused at BOTH refs is 'unchanged' to the differential, so it would ride
    along green forever while claiming to be a golden path the product depends on.
    This also proves all four real tiers run outside an event loop in a hermetic
    child, which the differential alone would not show.
    """
    rows = deny_diff.load_corpus(GOLDEN_PATHS)
    platform = deny_diff.resolve_platform("auto")
    shell_rows = [r for r in rows if r.kind == "shell" and r.applies_to(platform)]
    assert shell_rows, f"the corpus has no {platform}-applicable shell rows"

    _hermetic_git(monkeypatch)
    checkout = deny_diff.resolve_checkout(ROOT, "HEAD", tmp_path / "head")
    verdicts, absent = deny_diff.classify(
        checkout, [r.command for r in shell_rows], home=tmp_path / "home"
    )
    assert absent == [], f"HEAD is missing declared tiers: {absent}"

    refused = [
        (row.command, verdict.tier, verdict.reason)
        for row, verdict in zip(shell_rows, verdicts)
        if verdict.denied
    ]
    assert refused == []
