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
import time
from pathlib import Path

from kiro_crew import security
from kiro_crew.security import (
    MAX_SCANNABLE_COMMAND_CHARS,
    MAX_SCANNABLE_SOURCE_BODY_CHARS,
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
#: rather than in the pool package. It is a bound on total volume:
#: relocating a declaration between submodules moves nothing across it.
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
#:
#: The number IS the package's measured total, carrying no spare room: a ratchet with
#: headroom admits exactly the unreviewed growth it exists to catch, so the next line
#: added here fails this gate and has to be re-pinned deliberately, with its reason
#: written above. The guards that detect a monolith growing back are the per-file cap
#: and the facade's share below, and both must stay untouched.
_PACKAGE_LINE_BUDGET = 27_863

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
