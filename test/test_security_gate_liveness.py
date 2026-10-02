"""Liveness tests for the shell-command gate.

``is_sensitive_bash_command`` runs synchronously on the gateway's event loop,
under a loop-stall watchdog that hard-exits the process after 25 s of silence
(``dashboard.loop_stall_exit_after_secs``). Path-matching passes over the command
are quadratic, so a ~9 KB command full of ``https://`` URLs can stall the loop
past that budget. The gate does not match paths in command text at all -- and
what it still runs
(the size ceiling, the IMDS detector and the environment-credential detector) is
pinned here at the crash size, under the ceiling, where it is what the loop
actually pays. The ceiling itself is pinned as a refusal, not a skip: a command
too long to scan is denied rather than let through unscanned.
"""

from __future__ import annotations

import inspect
import json
import shlex
import time
from pathlib import Path

from kiro_crew import security
from kiro_crew.security import (
    MAX_SCANNABLE_COMMAND_CHARS,
    MAX_SCANNABLE_SOURCE_BODY_CHARS,
    is_denied,
    is_sensitive_bash_command,
)

# ─────────────────────────────────────────────────────────────────────────────
# Shapes
# ─────────────────────────────────────────────────────────────────────────────

_SEG = "a" * 60


def _double_separator_command(n: int) -> str:
    """n path operands, each with a doubled ``//`` -- the shape that would send
    the command through a separator-collapsed re-scan."""
    return "ls " + " ".join(f"/opt//{_SEG}" for _ in range(n))


def _url_payload_command(n: int) -> str:
    """The field shape: a JSON body of ``https://`` URLs handed to curl."""
    urls = [f"https://tasks.example.test/T{100000 + i}?view=full&x={_SEG[:20]}" for i in range(n)]
    return "curl -s -X POST -d " + json.dumps({"items": urls})


# ─────────────────────────────────────────────────────────────────────────────
# Package shape: the split must not grow back into a monolith
# ─────────────────────────────────────────────────────────────────────────────


#: Ceiling on the whole security PACKAGE, not on any one file in it. The controls
#: were one module of about 21,800 lines, and the split adds a re-export block, an
#: export manifest and the mirroring facade on top of the code it relocates, so the
#: budget is that size plus room for the machinery, plus the redaction record,
#: credential-source and allowed-host modules, plus the resolver child script
#: (``_child_realpath.py``, ~190 lines) that lives beside the resolver it serves
#: rather than in the pool package. It is a bound on total volume: relocating a
#: declaration between submodules moves nothing across it.
#:
#: Raised again, from 27,200, when the facade stopped binding re-exported names
#: eagerly and began resolving each through its owner. That trades one import block
#: for two name lists -- an owner table and a ``TYPE_CHECKING`` block, one line per
#: exported name in each -- which measured 618 lines at the current surface and is
#: machinery, not control logic.
#:
#: Raised again, from 27,751, for the write-protected home entries covering the MCP
#: launch-approval directory and ``mcp/resolved``: gatewayd spawns an approved stub's
#: backend outside the sandbox, so a session must not be able to write either path.
#:
#: Raised again, from 27,761, for the ssh self-target refusal note: it says how long
#: a retry can still land inside the background check and names the IP-literal case
#: where this machine's address list cannot be read, so a refused agent knows when
#: to stop retrying and what to use instead.
#:
#: Raised again, from 27,766, for the write-protected home entry covering the kiro-cli
#: global MCP registry (``~/.kiro/settings/mcp.json``) and its ``KIRO_HOME``
#: re-anchoring: an ``autoApprove`` on an entry there is honoured by default and skips
#: the tool gate entirely, while the entry that decides it is admitted on its name
#: shape rather than on who wrote the file -- so an agent-writable registry grants its
#: own verbs a standing bypass. The reasoning for one leaf is most of the cost, which
#: is the shape every entry on this tier has.
#:
#: Raised again, from 27,814, for resolving the ``$HOME``-rooted form of both kiro-cli
#: write-tier leaves rather than only their ``KIRO_HOME`` copies. Anchoring them
#: lexically covered a symlinked ``$HOME`` itself but not one further down the path, so
#: a dotfile-managed ``~/.kiro`` left the real spec dir and the real MCP registry outside
#: the fence while their ``~``-spelled paths stayed inside it. The cost is the reasoning
#: plus one shared tuple, which is what replaces a second per-leaf arm.
#:
#: Lowered to 27,851 by SUBTRACTION. An earlier revision of this branch also emitted
#: every kiro-cli target in a second, all-forward-slash spelling, on the premise that a
#: Windows root could reach the anchor builder carrying the operator's own separators.
#: That premise is false: every root arrives through ``_resolve_root_anchors``, which
#: returns ``_realpath_or_none(expanded) or _lexical_root(expanded)``, and both answer
#: in the native spelling -- ``_lexical_root``'s ``os.path.normpath`` converts
#: ``C:/Users/x`` to ``C:\Users\x``. With no input that fails without it, the second
#: spelling was decoration, and on POSIX it would fence a bogus neighbour for any file
#: whose name contains a backslash. It is removed together with the two tests that
#: existed only for it; the ``$HOME``-symlink coverage passes without it, which is what
#: shows it was never load-bearing.
#:
#: Raised again, from 27,851, because the ``KIRO_HOME`` half was two hardcoded
#: per-leaf arms while the ``$HOME`` half looped the tuple -- so the tuple's own
#: comment ("a third leaf joins both halves by landing here") was false, and a third
#: leaf would have been fenced under ``$HOME`` and writable under the override. Both
#: halves now loop the same tuple, each leaf's tail is spelled once, and a test adds a
#: probe leaf and asserts BOTH spellings refuse -- it fails on the old code.
#:
#: Re-pinned again, on top of every raise above, for the ``panel-dismissals`` leaf
#: added to ``_CREW_SECRET_LEAVES`` in ``paths.py``: one entry plus the comment
#: stating why nothing a run can reach may forge or delete the operator's dismissal
#: records. Ten lines, all of them the fence declaration and its reason -- no new
#: control logic and no new matching pass. This branch's raise and the ones above it
#: are independent additions to the same ratchet, so the number below is re-MEASURED
#: off the tree rather than being the arithmetic sum of the deltas.
#: Raised again, from 27,863, for the own-address startup warm in ``argv_floor``:
#: the gateway starts the netlink read at boot, the worker reads and publishes that
#: table before any DNS lookup, and the publish merges the addresses and opens the
#: IP-literal window in one lock hold while each check reads the window flag before
#: the names, so the first ssh after a restart is not refused as this machine and a
#: secondary own IP is never admitted mid-publish. A dump that ends without
#: NLMSG_DONE, or that the kernel flags NLM_F_DUMP_INTR, counts as unread, so a
#: partial table never opens the window. So does an NLMSG_DONE whose errno is not 0.
#: Three incomplete dumps in a row log one warning, so a host whose table never
#: reads can be told apart from a target that is really this machine.
#: Re-pinned again, from 27,942, for the assignment resolver's whole-script walk in
#: ``shell_normalizer.py``: command-boundary, quoted-separator, bounded ``eval``-join,
#: per-choice guarded-reassignment readings enumerated exactly per co-referenced
#: group (fail-closed past the group cap and the volume budget), one reading per
#: token list, ``|&`` and glued-name binding rules, one reading budget per
#: command, quotes read as syntax only in a quoted whole script, the append
#: form co-referenced, a verb binding live inside a script, each reading charged
#: at its own size, an append read as a leading assignment, compound-command
#: keywords and ``case`` patterns as guards, a guarded append as a choice, one
#: reading budget per payload WALK, an assignment-shaped argument as a choice, an
#: append inside a script read onto the outer value, a pipe joining its sides in
#: the grouping and in a further reading, the fail-closed reading a marker every
#: consuming floor refuses on rather than the mint spelling, a glued pipe read as
#: the operator it spells, every guard its own choice (two guards spelled alike
#: are not one test in case-folded text), a compound body's guard sticky over every
#: statement in it, ``$IFS`` a separator in an expanded use, and a word glued to a
#: ``case`` pattern's ``)`` or a function's ``{`` its own word, and the
#: glued first name's suffix reading serving a use only (a closer glued inside a
#: quoted argument closing nothing), and the FIRST assignment's name glued via
#: ``_SHELL_ASSIGN_RE`` before append handling so a long glued ``name+=<cli>``
#: carrier records under the name its use reads (GPT F1), and an append
#: continuing the assignment run so a command-word-less run's binding is not
#: read as prefix-scoped, and an expansion's inner operator masked before the
#: co-reference split (with the ``$`` test a fast path, not a boundary skip) so
#: two names across it stay one group, and an over-cap guarded group folds to allowed
#: unless a floor recogniser fires on a candidate token -- a protected, self,
#: ssh-family or interpreter program, a nested shell, a protected flag, rsync paired
#: with a remote operand, or a git publish operand -- fail-closed otherwise only on a
#: cost overflow, with a subshell-scoped APPEND read at the command-run end rather than
#: the next token (invert-default found in review, +62 lines: the fold's recogniser
#: delegation and inert-command test), 1128 lines measured -- then the four bypasses the reviewer proved on that
#: fold closed (the full co-referenced group at the call site, rsync gated on a
#: remote host:path, a use inside an assignment value co-referenced, and the line
#: pre-resolver deferring a reassigned name to the guarded resolver), +69 lines net
#: after moving ``_next_stop_indexes`` to the leaf to hold the normalizer at its cap;
#: then the line resolver's deferral regression fixed -- the ssh-family probe gate
#: reads the FULL substitution while only the resolver text defers a reassigned name;
#: then the fold's danger recognisers made the floors' OWN (one _ssh_family_verb_of
#: shared by the ssh-self floor and the fold, glob + .exe by construction), with
#: _strip_redirect and _substitution_program moved to the leaf to hold the cap
#: (38 of them the header
#: of ``shell_assignment_syntax.py``, split out of the normalizer at the per-module
#: cap below) -- the same kind of raise the resolver child script made.
#:
#: Re-pinned from 27,942 for the NUL blanking in ``inline_payload._lex``: one line
#: that swaps each NUL for a space before tokenizing, plus the docstring saying why.
#: CPython 3.12 raises ``SystemError`` for a NUL after an indented block, which
#: escaped the lexer and crashed the gate on an ordinary ``b'\0'`` in a payload.
#: No new rule and no new matching pass.
#: Raised again, from 27,948, by one line: the ``vouched-executions`` entry in
#: ``_SENSITIVE_HOME_DIRS``. Each file there is the gateway's restart-surviving
#: word that a session may reach its member's private store, so no file tool may
#: write it. The fuller reason lives beside its ``sandbox._CREW_HIDDEN_LEAVES`` mask.
#:
#: Re-pinned from 27,949 for ``redaction._DOCUMENT_LINK_RE``: pass 3 skips a run
#: wholly inside a Google Docs, Drive or Confluence link of a fixed route, because a
#: document id is the same random base64 a key is and no gate can split the two.
#: One route regex, one span helper, a four-line check in pass 3, and the comment
#: naming the residual. No pass widened and no threshold moved.
#:
#: Re-pinned from 28,025 for the over-cap guarded-reading fold: an over-cap group
#: folds to allowed unless a floor recogniser fires, which adds the
#: ``shell_assignment_syntax`` helper module and widens the fold's own check. The
#: number is the package's measured total after that work; no gate widened.
#:
#: The number IS the package's measured total, carrying no spare room: a ratchet with
#: headroom admits exactly the unreviewed growth it exists to catch, so the next line
#: added here fails this gate and has to be re-pinned deliberately, with its reason
#: written above. The guards that detect a monolith growing back are the per-file cap
#: and the facade's share below, and both must stay untouched.
_PACKAGE_LINE_BUDGET = 29_501

#: Ceiling on any ONE file in the package. This is what the bound is really for --
#: a package total says nothing about a single file growing back into a second
#: monolith, and a per-file cap is what a whole-file bound on the pre-split module
#: could not express. Set with headroom over the largest cluster so ordinary growth
#: does not trip it; a cluster that reaches it is asking to be split, and RAISING
#: the number is not the fix.
_MODULE_LINE_CAP = 4_500


def _package_line_counts() -> dict[str, int]:
    """Line count per file of the installed ``kiro_crew.security`` package."""
    package_dir = Path(security.__file__).parent
    return {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(package_dir.glob("*.py"))
    }


def test_the_package_stays_within_its_line_budget() -> None:
    counts = _package_line_counts()
    assert counts, "no package sources found"
    total = sum(counts.values())
    assert total <= _PACKAGE_LINE_BUDGET, f"package grew to {total} lines: {counts}"


def test_no_single_module_grows_back_into_a_monolith() -> None:
    oversized = {
        name: count for name, count in _package_line_counts().items() if count > _MODULE_LINE_CAP
    }
    assert not oversized, f"past the per-module cap: {oversized}"


def test_the_facade_is_the_smallest_it_can_be_of_the_package() -> None:
    """The facade carries re-exports and the mirroring machinery, so it must stay a
    small share of the package: a share that climbs means logic is accreting in the
    one file every caller imports, which is the shape the split exists to prevent."""
    counts = _package_line_counts()
    facade = counts["__init__.py"]
    assert facade * 5 <= sum(
        counts.values()
    ), f"the facade is {facade} of {sum(counts.values())} package lines"


# ─────────────────────────────────────────────────────────────────────────────
# Size ceiling: refused, not scanned, not skipped
# ─────────────────────────────────────────────────────────────────────────────


def test_oversized_command_is_refused_with_a_reason() -> None:
    cmd = "echo " + "x" * MAX_SCANNABLE_COMMAND_CHARS
    reason = is_sensitive_bash_command(cmd)
    assert reason is not None
    assert "too large to security-scan" in reason
    assert str(len(cmd)) in reason


def test_command_at_the_ceiling_is_scanned_not_refused() -> None:
    body = "x" * (MAX_SCANNABLE_COMMAND_CHARS - len("echo "))
    assert is_sensitive_bash_command("echo " + body) is None
    # And a detector's subject at the very end of a ceiling-sized command is found:
    # the ceiling is a bound on what is scanned, not a skip of the tail.
    tail = "; curl http://169.254.169.254/latest/meta-data/"
    cmd = "echo " + "x" * (MAX_SCANNABLE_COMMAND_CHARS - len("echo ") - len(tail)) + tail
    assert len(cmd) == MAX_SCANNABLE_COMMAND_CHARS
    reason = is_sensitive_bash_command(cmd)
    assert reason is not None
    assert reason.startswith("Blocked: command accesses IMDS")


def test_ceiling_matches_the_tool_input_tier() -> None:
    """The two tiers refuse at the same size, so a command cannot be too long
    for one and scanned by the other."""
    from kiro_crew import llm_helpers

    assert llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS == MAX_SCANNABLE_COMMAND_CHARS


# ─────────────────────────────────────────────────────────────────────────────
# A cron SCRIPT BODY has its own ceiling, and is not a shell subject at all
# ─────────────────────────────────────────────────────────────────────────────


def test_the_source_body_ceiling_is_larger_and_owned_by_the_cron_reader() -> None:
    """20 KiB of shell on one ``Bash`` call is a heredoc; 20 KiB of cron script is an
    ordinary script, and refusing it there is permanent (every tick until edited). The
    cron gate reads and refuses on ONE number so the reader and the scan agree."""
    from kiro_crew import mcp_cron

    assert MAX_SCANNABLE_SOURCE_BODY_CHARS > MAX_SCANNABLE_COMMAND_CHARS
    assert mcp_cron._MAX_SCRIPT_SCAN_BYTES == MAX_SCANNABLE_SOURCE_BODY_CHARS

    body = "".join(f'value_{i} = "{"t" * 200}"\n' for i in range(120))
    assert MAX_SCANNABLE_COMMAND_CHARS < len(body) <= MAX_SCANNABLE_SOURCE_BODY_CHARS
    assert mcp_cron._vet_script_contents(body) is None
    assert mcp_cron._vet_script_contents(body + 'open("~/.aws/credentials")\n') is not None

    over = "x = 1\n" * MAX_SCANNABLE_SOURCE_BODY_CHARS
    reason = mcp_cron._vet_script_contents(over)
    assert reason is not None and "too large to security-scan" in reason


def test_the_shell_gate_has_no_source_body_entry_point() -> None:
    """RATCHET: ``is_sensitive_bash_command`` takes a shell command line and nothing
    else -- no subject flag, no re-pointed traversal subjects, no per-caller ceiling.
    Every one of those knobs existed once to make a Python source body survive a
    shell-grammar pass, and each pass still produced a false-denial class on ordinary
    scripts. A source body is not this gate's subject; see
    ``mcp_cron._vet_script_contents``."""
    params = inspect.signature(security.is_sensitive_bash_command).parameters
    assert set(params) == {"command", "enabled_ids"}, sorted(params)
    for name in (
        "is_sensitive_source_body",
        "_source_command_subjects",
        "_sensitive_run_in_source_literals",
        "_parse_source_body",
        "_SOURCE_PATTERN_SINKS",
        "_SOURCE_COMMAND_SUBJECT_CAP",
    ):
        assert not hasattr(security, name), name


# ─────────────────────────────────────────────────────────────────────────────
# Liveness at the crash size, under the ceiling
# ─────────────────────────────────────────────────────────────────────────────


def _gate_seconds(command: str) -> float:
    started = time.perf_counter()
    is_sensitive_bash_command(command)
    return time.perf_counter() - started


def test_double_separator_10kb_is_fast() -> None:
    """The crash shape, at the crash size: 15 s on the shipped build."""
    cmd = _double_separator_command(160)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert is_sensitive_bash_command(cmd) is None
    assert _gate_seconds(cmd) < 2.0


def test_url_payload_12kb_is_fast() -> None:
    cmd = _url_payload_command(160)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert is_sensitive_bash_command(cmd) is None
    assert _gate_seconds(cmd) < 2.0


def _nested_guarded_carriers(depth: int, guards: int = 6) -> str:
    """``bash -c`` nested *depth* deep, *guards* guarded bindings per level, the
    innermost script expanding every ancestor name."""
    levels = [[f"n{d}{i}" for i in range(guards)] for d in range(depth)]
    script = "echo " + " ".join(f"${n}" for level in levels for n in level)
    for names in reversed(levels):
        binds = "; ".join(f"{n}=1; false && {n}=2" for n in names)
        script = f"{binds}; bash -c {shlex.quote(script)}"
    return script


def test_nested_guarded_carriers_share_one_reading_budget() -> None:
    """Six guards per level yield 64 readings per frame and one nested payload per
    reading: spent per frame, the budget let ~64 ** depth frames each pay 64
    rescans (14 s at depth 2, the watchdog at depth 3).  One budget per walk
    reads the tree fail-closed as soon as it is spent; a shallow tree that fits
    is still read every way.  The bound is in the command's own units -- the
    depth-4 tree costs no more than a few times the depth-1 one that reads every
    reading (3x here, 62x at depth 2 before the fix) -- because a shared CI
    worker runs this 10x slower than a quiet host and a wall-clock number alone
    was a flake."""
    shallow = _nested_guarded_carriers(1)
    started = time.perf_counter()
    assert is_denied(shallow) is None
    every_reading = time.perf_counter() - started

    deep = _nested_guarded_carriers(4)
    assert len(deep) <= MAX_SCANNABLE_COMMAND_CHARS
    started = time.perf_counter()
    assert is_denied(deep) is not None
    past_the_budget = time.perf_counter() - started
    assert past_the_budget < 8 * every_reading + 1.0, (every_reading, past_the_budget)
    assert is_denied(_nested_guarded_carriers(3, guards=1)) is None


def _guarded_groups_command(groups: int) -> str:
    return "; ".join(f"v{i}=a{'x' * 10}; false && v{i}=b; echo $v{i}" for i in range(groups))


def test_a_long_contiguous_assignment_run_is_not_quadratic() -> None:
    """A long ``x=1 x=2 ...; $x token`` run must scale LINEARLY, not O(n^2).

    The resolver's command-position check re-walked the whole run per token
    (~13M regex matches on a run at the scan ceiling), stalling the gate; a
    precomputed run-start table makes it linear. Asserted as a sub-quadratic
    RATIO rather than a wall-clock bound, so a contended CI runner does not
    flake it (found in review).
    """
    import time

    from kiro_crew.security import is_denied

    def elapsed(n: int) -> float:
        run = " ".join("x=%d" % (i % 2 + 1) for i in range(n)) + "; $x token"
        start = time.perf_counter()
        is_denied(run)
        return time.perf_counter() - start

    small = elapsed(1000)
    big = elapsed(5000)
    # 5x the tokens: linear ~5x, quadratic ~25x. Generous slack for constants and
    # runner contention; a re-introduced O(n^2) walk blows straight past it.
    assert big < 12 * small + 1.0, (small, big)


def test_an_over_cap_group_with_many_uses_is_bounded() -> None:
    """An over-cap group repeating a group-referencing word many times is bounded.

    The fold re-enumerated its candidates per word with no memoization, stalling
    the gate; memoization plus a per-fold work budget bounds it. Asserted as a
    sub-quadratic RATIO so a contended CI runner does not flake it (found in review).
    """
    import time

    from kiro_crew.security import is_denied

    guards = "".join("%s=1; false && %s=2; " % (c, c) for c in "abcdefgh")

    def elapsed(reps: int) -> float:
        seg = " ".join("$a$b$c$d$e$f$g$h" for _ in range(reps))
        start = time.perf_counter()
        is_denied(guards + "echo " + seg)
        return time.perf_counter() - start

    small = elapsed(200)
    big = elapsed(1200)
    # 6x the uses: memoization keeps it near-flat, so a modest multiple catches a
    # regression to per-word re-enumeration while tolerating runner contention.
    assert big < 12 * small + 1.0, (small, big)


def test_many_guarded_groups_12kb_is_fast() -> None:
    """300 independent guarded reassignments on the deny floor: ~28 s of per-group
    rescans before the reading budget became the command's (refused past 64
    readings in all).  The bound is in the command's own units -- the 300-group
    command costs no more than a few times the 63-group one that reads every
    reading -- because a shared CI worker runs this 8x slower than a quiet host
    and a wall-clock number alone was a flake; the per-group rescan was 20x."""
    read_every = _guarded_groups_command(63)
    started = time.perf_counter()
    assert is_denied(read_every) is None
    every_reading = time.perf_counter() - started

    cmd = _guarded_groups_command(300)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    started = time.perf_counter()
    assert is_denied(cmd) is not None
    past_the_cap = time.perf_counter() - started
    assert past_the_cap < 4 * every_reading + 1.0, (every_reading, past_the_cap)
