#!/usr/bin/env python3
"""Batch fleet probe — the deterministic half of the pipeline conductor's patrol.

One invocation answers, for every watched worker session, "does anything need
judgment this cycle?" — plus host posture — so a quiet patrol cycle costs one
script call and a couple of output lines instead of N transcript reads.

Usage:
    python3 fleet_probe.py --config <probe-config.json>
    python3 fleet_probe.py --config <probe-config.json> --mark-handled KEY TAG DIGEST

Config (JSON):
    {
      "sessions": ["dashboard_chat-601-1788099254", ...],   # slot keys to watch
      "idle_alert_secs": 900,      # silent longer than this -> IDLE alert
      "tail_bytes": 200000,        # per-session transcript PARSE cap
      "load_alert_per_cpu": 1.5,   # 1-min loadavg / cpu above this -> hot
      "err_res": [...],            # extra error-tail regexes (optional)
      "banned_process_res": [...], # cmdline regexes for banned ops (optional)
      "init_timeout_res": [...],   # initialize-timeout tails (optional)
      "watchdog_res": [...],       # turn-ended-by-stall-watchdog tails (optional)
      "fleet_worktrees": [...],    # absolute roots this fleet owns (optional)
      "argv_runner_detection": true # false switches OFF the argv-side runner shape
    }

Every regex key is validated at load time: a bad pattern is malformed config
(exit 2 with the offending pattern), never a crash mid-cycle. ``tail_bytes``
caps how much of a transcript is PARSED, not how much is read -- the whole file
is read either way, which is what makes the tail index monotonic for free.

Paths are DERIVED, never configurable -- the same containment rule for every
location this script touches, because its config is authored by a no-write
agent and a config-chosen path would quietly widen what an approved run can
reach:

  * transcripts are read from ``<data home>/sessions`` only, where the data
    home is ``$KIROCREW_HOME`` (else ``~/.kiro/crew``) -- the gateway this
    conductor belongs to, not an arbitrary directory;
  * the handled-set state file is ALWAYS ``<config path>.state.json``;
  * the banned-process scan reads ``/proc`` (``$KIROCREW_PROBE_PROC_ROOT``
    exists for the test harness).

A session key that does not match a transcript stem directly is also tried as
``dashboard_<key>`` (and with ``:`` as ``_``): ``session_create`` answers slot
keys while the store prefixes the surface, and a raw key must not read as a
missing session -- GONE triggers reclaim, and a false GONE is how an active
item gets duplicate-dispatched.

The data home is ``$KIROCREW_HOME`` when set, else ``~/.kiro/crew`` — the same
resolution every pipeline script uses.

Output (text, one line per FIRING signal; suppressed sessions print nothing):
    🔔 <session-key> <age>s <TAG> i=<index> d=<digest>

``i=`` counts the rows that SESSION PRODUCED -- its own messages and tool calls,
never an inbound nudge, inject or user row, because a supervisor's own nudge
landing in the file must not read as the worker making progress. It is a
monotonic per-session position counted from the start of the file, not from the
window, so it cannot saturate once a transcript passes ``tail_bytes``. An
unchanged count since the conductor last acted is *no progress*, whether or not
a turn is open, and that is the discriminator a self-deadlocked worker cannot
fake -- the probe makes that comparison itself and fires ``NOPROGRESS`` rather
than leaving two numbers for someone to diff. It is a position, so it carries no
transcript content.

Metadata only, by design: transcript-derived text never appears in the output,
so no private session content crosses into the caller's context whatever keys
the config watches. Content, when a ruling needs it, is read through the
workspace-authorized session tools.
    BANNED pid=<pid> rule=<regex|argv:<shape>> cwd=fleet|unknown age=<secs|?>s
       scope=suite|paths|unknown cmd=<program,flags,+withheld>
    OK <n> watched, <m> fired | load/cpu <x> (<posture>) | mem <G>G
       | banned <k> | foreign <k> | deliver init-timeout <a>, watchdog <b>

``banned`` counts fleet-owned matches only -- a banned command shape running in
an unrelated checkout on the same host is somebody else's business, and counting
it made the conductor stop a worker that was not the offender. Those are
summarised as ``foreign`` and not printed. ``deliver`` is the honest admission
instrument: load and memory can both read healthy while the fleet cannot
deliver, so sessions whose tail carries an initialize timeout or a
stall-watchdog turn end are counted every cycle, fired or not.

Tags: the worker protocol words (``WORKING/PR/GREEN/BLOCKED/STANDDOWN/
PROPOSAL``) -- recognised in their protocol form, ``<WORD>:``, so prose that
merely opens with one is not a report -- plus ``ERR`` for an error/throttle
tail, ``IDLE`` for silence past the threshold, ``TERMINAL`` for a session whose
last dispositioned report ENDED its assignment (``GREEN``/``STANDDOWN``/
``PROPOSAL``) and which has since written unprefixed text, ``NOPROGRESS`` for a
session that has produced nothing since the conductor last acted on it, and
``-`` when a tail carries no tag
(never fires on its own). Tool rows never classify: a protocol word or an error
phrase inside a tool card is quoted text, not a report.

The tag is the newest REPORT, not the newest message, because ``BLOCKED`` is
STICKY: a probe samples rather than subscribes, and the protocol requires a
blocked worker to keep reporting status, so its own next message would displace
the only thing a newest-message classifier reads. A heartbeat (``WORKING``) and
unprefixed text leave a sticky report standing; any other report supersedes it.
``TERMINAL`` and sticky ``BLOCKED`` are kept distinct on purpose -- one says
close me, the other says a ruling is owed.

Leading markdown decoration is stripped before the tag is matched, because the
match is anchored at position zero and ``**BLOCKED:**`` puts an asterisk there.
Emphasis, blockquote arrows, heading hashes and list markers all count. The
normalisation applies to the MATCHED text only: the digest is still computed over
what the worker wrote, and the anchor survives, so a bolded protocol word
mid-sentence is still not a report.

THE HANDLED SET replaces the overnight run's hand-grown ``grep -vE`` exclusion
pipe. Every fired line carries a ``d=<digest>`` field; ``--mark-handled KEY TAG
DIGEST`` records exactly that digest into the state file (compare-and-set: if
the tail moved on since the probe, the mark is REFUSED with exit 3 so the
caller re-probes instead of suppressing a payload nobody read). A signal is
then suppressed while (tag, digest) both still match. A new payload under the
same tag re-fires (a second GREEN with a new PR number is a new signal).
``IDLE`` marks expire after another ``idle_alert_secs``
so a nudged-but-still-silent worker re-alerts instead of vanishing.

A handled entry also records the tail ``index`` at the moment of the mark, and
the last dispositioned PAYLOAD report as ``settled`` (its tag AND digest) -- one
field, written on every mark and carried forward when the mark is not itself a
payload, so no later disposition can erase the fact that a report was already
answered. Neither field takes part in the digest, and a state file written before
they existed still suppresses exactly as it did; a legacy ``proto`` field, which
recorded only the tag, is still read so an in-place upgrade keeps its terminal
reading.

Deliberately boring properties, do not weaken:
  * No subprocess, ever. Reads transcripts, ``/proc`` and ``loadavg`` directly.
  * The only write is the probe's own state file, atomically, and only on
    ``--mark-handled``.
  * A per-session problem (unreadable file, malformed line) degrades that one
    row, never the cycle. Exit 0 when the probe ran; 2 on malformed config.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any


def _load_owner(stem: str) -> ModuleType:
    """Load the sibling module *stem* from this file's own directory.

    The directory is not a package, so an owner is loaded by its path rather than
    imported: nothing is added to ``sys.path`` and nothing is registered in
    ``sys.modules``. Bytecode writing is off for the load, because an ordinary import
    leaves a ``__pycache__`` beside the script, and the one file this probe writes is
    its own state file.

    The owner is named under this module's own package, so a harness that loads the
    probe under a dotted name gets its owners under the same prefix -- the name is what
    a coverage run measures by -- and a script run names them by their bare stem."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{stem}.py")
    package = __name__.rpartition(".")[0]
    spec = importlib.util.spec_from_file_location(f"{package}.{stem}" if package else stem, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


# The probe's decision logic lives in two sibling modules, each importing nothing but the
# standard library. A type checker reads them as the plain modules they are; at run time
# they are loaded by path. What stays in this file is the entry point, the patrol cycle
# and the session-store lookup it uses, config validation, the ``/proc`` walk that builds
# a ``BANNED`` line, and the ``cmd=`` redactor. With them stays every name a test
# replaces on this module, so no code in an owner reads one of those. The owners never
# import this module, so they are loaded here, ahead of everything below: any
# module-level statement in this file that reads one of their names finds it bound.
if TYPE_CHECKING:
    import fleet_probe_processes as _processes
    import fleet_probe_sessions as _sessions
else:
    _sessions = _load_owner("fleet_probe_sessions")
    _processes = _load_owner("fleet_probe_processes")

# The session side: protocol tags, the transcript tail, and the handled set.
PROTO_TAGS = _sessions.PROTO_TAGS
PROTO = _sessions.PROTO
_LEADING_DECOR = _sessions._LEADING_DECOR
_proto_tag = _sessions._proto_tag
TOOL_ROLES = _sessions.TOOL_ROLES
_OWN_ROW_NEEDLES = _sessions._OWN_ROW_NEEDLES
_count_own_rows = _sessions._count_own_rows
DEFAULT_ERR_RES = _sessions.DEFAULT_ERR_RES
DEFAULT_INIT_TIMEOUT_RES = _sessions.DEFAULT_INIT_TIMEOUT_RES
DEFAULT_WATCHDOG_RES = _sessions.DEFAULT_WATCHDOG_RES
IDLE_TAG = _sessions.IDLE_TAG
TERMINAL_TAG = _sessions.TERMINAL_TAG
NOPROGRESS_TAG = _sessions.NOPROGRESS_TAG
TERMINAL_TAGS = _sessions.TERMINAL_TAGS
STICKY_TAGS = _sessions.STICKY_TAGS
HEARTBEAT_TAGS = _sessions.HEARTBEAT_TAGS
_PAYLOAD_TAGS = _sessions._PAYLOAD_TAGS
_FIRING = _sessions._FIRING
_EXPIRING_TAGS = _sessions._EXPIRING_TAGS
_KEY_RE = _sessions._KEY_RE
data_home = _sessions.data_home
_atomic_write = _sessions._atomic_write
_text_of = _sessions._text_of
_tail_entries = _sessions._tail_entries
_classify = _sessions._classify
_sticky_pending = _sessions._sticky_pending
_tail_matches = _sessions._tail_matches
_recorded_proto = _sessions._recorded_proto
_stalled_since_disposition = _sessions._stalled_since_disposition
_digest = _sessions._digest
_load_state = _sessions._load_state
_suppressed = _sessions._suppressed

# The process side: banned command shapes, a runner's argv, and who owns a process.
_SHELL_TOKEN_BOUNDARY = _processes._SHELL_TOKEN_BOUNDARY
_CAP_SCAN = _processes._CAP_SCAN
_UNBUDGETED_COUNT = _processes._UNBUDGETED_COUNT
_VITEST_BANNED_RE = _processes._VITEST_BANNED_RE
DEFAULT_BANNED_RES = _processes.DEFAULT_BANNED_RES
_RUNNER_BASES = _processes._RUNNER_BASES
_ALIAS_RUNNER_BASE_RE = _processes._ALIAS_RUNNER_BASE_RE
_ALIAS_PROGRAM_LABELS = _processes._ALIAS_PROGRAM_LABELS
_alias_program_label = _processes._alias_program_label
_ARGV_ONLY_RUNNER_BASES = _processes._ARGV_ONLY_RUNNER_BASES
_ENV_ASSIGNMENT_RE = _processes._ENV_ASSIGNMENT_RE
_VALUE_TAKING_OPTS = _processes._VALUE_TAKING_OPTS
_token_base = _processes._token_base
_is_runner_base = _processes._is_runner_base
_runner_token_index = _processes._runner_token_index
_run_scope = _processes._run_scope
_invokes_bare_vitest_run = _processes._invokes_bare_vitest_run
_PYTHON_BASE_RE = _processes._PYTHON_BASE_RE
_norm_path = _processes._norm_path
_under = _processes._under
_program_path = _processes._program_path
SHELL_PROGRAMS = _processes.SHELL_PROGRAMS
_basename = _processes._basename
_SHELL_OPTS_WITH_OPERAND = _processes._SHELL_OPTS_WITH_OPERAND
_TRUSTED_PROGRAM_DIRS = _processes._TRUSTED_PROGRAM_DIRS
_PROCFS_DELETED_MARKER = _processes._PROCFS_DELETED_MARKER
_trusted_program_base = _processes._trusted_program_base
_kernel_program_path = _processes._kernel_program_path
_is_shell_command_wrapper = _processes._is_shell_command_wrapper
_CAP_FLAGS = _processes._CAP_FLAGS
_BUDGETED_COUNT_WORDS = _processes._BUDGETED_COUNT_WORDS
_OVERRIDE_INI_FLAGS = _processes._OVERRIDE_INI_FLAGS
_ADDOPTS_KEY = _processes._ADDOPTS_KEY
_END_OF_OPTIONS = _processes._END_OF_OPTIONS
_count_is_budgeted = _processes._count_is_budgeted
_read_flag = _processes._read_flag
_addopts_override = _processes._addopts_override
_argv_worker_pool_is_budgeted = _processes._argv_worker_pool_is_budgeted
ARGV_RUNNER_RULE_LABEL = _processes.ARGV_RUNNER_RULE_LABEL
_BUILTIN_SHAPES = _processes._BUILTIN_SHAPES
_argv_only_runner_indices = _processes._argv_only_runner_indices
_TRANSPARENT_LAUNCHER_BASES = _processes._TRANSPARENT_LAUNCHER_BASES
_is_launcher_own_operand = _processes._is_launcher_own_operand
_stands_in_program_position = _processes._stands_in_program_position
_is_installed_entry_point = _processes._is_installed_entry_point
_PYTHON_FLAG_LETTERS = _processes._PYTHON_FLAG_LETTERS
_PYTHON_VALUE_LETTERS = _processes._PYTHON_VALUE_LETTERS
_PYTHON_LONG_VALUE_OPTS = _processes._PYTHON_LONG_VALUE_OPTS
_python_execution_target = _processes._python_execution_target
_kernel_program_confirms = _processes._kernel_program_confirms
_argv_is_uncapped_argv_only_runner = _processes._argv_is_uncapped_argv_only_runner
_venv_root = _processes._venv_root
_program_class = _processes._program_class
_owner_class = _processes._owner_class


#: Characters that END a shell command. A cmdline that carries a script holds many
#: commands in one string, and only the one the rule matched inside is worth
#: printing -- the run of text between two of these.
_COMMAND_SEPARATORS = ";&|\n"

#: The programs that may LAUNCH a runner, echoed by name when one leads the command.
#: A fixed vocabulary rather than a program SHAPE, because the two are the same text:
#: a credential holding no ``/`` and no ``=`` (``wJalrXUtnFEMIbPxRfiCY``) satisfies
#: every rule a shape can state about a program name, so a shape test at the head of
#: the command echoes caller text by construction. An unrecognised leading token is
#: counted instead. Reading the name from the executable's own path would answer the
#: same finding and cost more than it saves: a fleet's runner lives in a venv or in
#: ``node_modules/.bin``, neither of which is a trusted program directory, so the
#: field would go blank on the ordinary case. Matching the BASENAME against a fixed
#: set keeps those lines readable and still admits nothing the caller chose.
_LAUNCHER_BASES = frozenset(
    {
        "bash",
        "coverage",
        "env",
        "hatch",
        "make",
        "nice",
        "node",
        "nox",
        "npm",
        "npx",
        "pdm",
        "pnpm",
        "poetry",
        "py",
        "sh",
        "timeout",
        "tox",
        "uv",
        "uvx",
        "xvfb-run",
        "yarn",
        "zsh",
    }
)

#: The LONG option names this line has a reason to print. A fixed vocabulary because
#: ``--`` followed by letters is a well-formed option name AND a well-formed credential
#: (``--wJalrXUtnFEMIbPxRfiCY``), so no shape separates them. An unrecognised flag is
#: counted like any other withheld token, which costs the line less than it looks --
#: ``rule=`` already names the rule that matched, so a custom program's own option
#: names are not what makes the line actionable.
_SAFE_LONG_FLAG_NAMES = frozenset(
    {
        "--bail",
        "--capture",
        "--co",
        "--collect-only",
        "--color",
        "--concurrency",
        "--config",
        "--cov",
        "--coverage",
        "--dist",
        "--durations",
        "--exitfirst",
        "--ff",
        "--forked",
        "--ignore",
        "--isolate",
        "--jobs",
        "--junitxml",
        "--last-failed",
        "--lf",
        "--max-workers",
        "--maxConcurrency",
        "--maxWorkers",
        "--maxfail",
        "--minWorkers",
        "--no-file-parallelism",
        "--no-header",
        "--no-watch",
        "--numprocesses",
        "--parallel",
        "--pdb",
        "--pool",
        "--poolOptions",
        "--quiet",
        "--reporter",
        "--retry",
        "--rootdir",
        "--run",
        "--runInBand",
        "--sequential",
        "--shard",
        "--silent",
        "--strict-markers",
        "--tb",
        "--threads",
        "--timeout",
        "--verbose",
        "--watch",
        "--workers",
    }
)

#: A SHORT option name, which is one letter and nothing else. A short option's value
#: is routinely GLUED to it with no ``=`` to split at (``-kMyCustomerName``,
#: ``-u1234``), so length is the only thing separating the name from the value: a
#: longer token keeps its two-character prefix and the rest is counted as withheld.
#: This is the one printable shape rather than a fixed list, because what it admits
#: from the command being read is a single letter -- too little to be a secret at any
#: entropy, where a long option's name is as long as its author cares to make it.
_SAFE_SHORT_FLAG_RE = re.compile(r"^-[A-Za-z]$")

#: How many tokens of a command may be printed. A script line can be arbitrarily
#: long and this lands in a conductor's model context one line per match.
_MAX_CMD_TOKENS = 8

#: How many characters of ONE retained token may be printed. A BACKSTOP, not a live
#: path: every printable token is drawn from a fixed vocabulary or is a two-character
#: flag, so nothing reachable today comes near this bound, and the guard exists for
#: the entry a later change adds to one of those lists. A clipped token is marked with
#: a trailing ``~`` for the same reason a withheld one is counted: neither may read as
#: the whole thing.
_MAX_CMD_TOKEN_CHARS = 48


def _redacted_command(cmd: str, span: tuple[int, int]) -> str:
    """The command that matched, reduced to names and flags that can hold no secret.

    A pid alone cannot say whether a match is a real run or a filename that reads
    like one: answering that means opening ``ps`` by hand, and by then the process
    is often gone. So the line carries the command -- but a command line is exactly
    where a credential or a presigned URL rides, which is why the rest of this
    scan derives words from the argv and never echoes it.

    Both are satisfied by printing only shapes that cannot carry a secret:

    * the leading token and any runner token, as a BASENAME drawn from a FIXED
      vocabulary -- the program's name is what identifies the command, while its
      directory is the part that leaks a checkout layout. The vocabulary is the point:
      a program-shaped word is the same text as a credential with no ``/`` and no
      ``=`` in it, so echoing whatever a shape test admitted would put a secret on the
      line, and an unrecognised program is counted instead. A versioned alias is
      RECOGNISED by a shape and PRINTED as a fixed label (``_alias_program_label``), so
      what lands on the line is still drawn from a closed set: the digits the command
      carried never appear. A token holding ``=`` is never a
      program name: it is an inline environment assignment, whose value can itself
      contain ``/``, so the ``=`` is tested BEFORE the directory is dropped --
      otherwise the tail of ``AWS_SECRET_ACCESS_KEY=…/abc`` arrives as ``abc``;
    * option NAMES, with the value dropped at the ``=``. LONG names come from a fixed
      vocabulary for the same reason programs do, since ``--`` and letters is as good
      a credential as it is an option. A SHORT one is exempt because it admits a
      single letter: its value is glued on with nothing to split at
      (``-kMyCustomerName``), so only a bare two-character short flag is echoed whole
      and a longer one keeps its first two characters;
    * nothing else, and no option's value -- not even the cap flag's. ``-n0`` prints as
      ``-n`` like every other flag: its digits are the field the rule judged, but a
      caller-supplied ``banned_process_res`` can point this scan at a program whose
      ``-n`` value is a numeric secret, and no shape separates a worker count from an
      account id. What the line loses is the ``-n0``-versus-``-n auto`` readout, and
      only under a custom rule -- a DEFAULT-rule line never carries a digits-valued cap
      at all, because that is exactly the run the rule declines to report.
    * Targets and bare words are counted, not shown, and the count is
      printed as ``+<n>`` so a truncated command cannot read as a complete one. An
      option's VALUE is not in that count: it is dropped at the name boundary, and a
      flag printed without a value is itself the record that one went. A retained
      token is clipped at ``_MAX_CMD_TOKEN_CHARS`` and marked ``~`` -- a backstop for a
      later vocabulary entry, since nothing printable today approaches it.

    The text comes from the same ``cmdline`` read the rule matched, so it cannot
    disagree with ``rule=`` about what was seen. Splitting on whitespace means an
    argument containing a space arrives as several tokens; that costs nothing,
    because the pieces are judged by the same shapes and an unrecognised piece is
    withheld like any other.

    *span* is where in *cmd* the thing being reported sits, and it selects the one
    command segment to reduce. A joined-line rule passes its own match span, because a
    shell string can hold several commands and only the matched one is worth printing.
    The ARGV path passes the whole of *cmd*: what it reported is a single command read
    from NUL-separated tokens, so there is no other command in there to narrow to, and
    a separator character sitting inside one of its arguments is data rather than an
    end.
    """
    start, end = span
    before = [cmd.rfind(sep, 0, start) for sep in _COMMAND_SEPARATORS]
    after = [idx for idx in (cmd.find(sep, end) for sep in _COMMAND_SEPARATORS) if idx != -1]
    segment = cmd[max(before) + 1 : min(after) if after else len(cmd)]
    kept: list[str] = []
    withheld = 0

    def keep(text: str) -> None:
        """Append *text*, clipped to the per-token bound and marked when clipped."""
        if len(text) > _MAX_CMD_TOKEN_CHARS:
            text = text[:_MAX_CMD_TOKEN_CHARS] + "~"
        kept.append(text)

    for index, token in enumerate(segment.split()):
        bare = token.strip("\"'")
        assigned = "=" in bare
        # Dropping the directory is safe only once the token is known not to be an
        # assignment: the strip is what turns a slash-bearing secret VALUE into a
        # program-shaped word.
        base = "" if assigned else bare.replace("\\", "/").rpartition("/")[2]
        canonical = base.lower()
        name = bare.split("=", 1)[0]
        if len(kept) >= _MAX_CMD_TOKENS:
            withheld += 1
        elif (
            _is_runner_base(canonical)
            or canonical in _ARGV_ONLY_RUNNER_BASES
            or (index == 0 and (canonical in _LAUNCHER_BASES or _PYTHON_BASE_RE.match(canonical)))
        ):
            # The CANONICAL form, not the token as read. What this branch establishes
            # is that the token is one this file calls a runner -- an entry of
            # ``_RUNNER_BASES``, an ``_ARGV_ONLY_RUNNER_BASES`` spelling, or the
            # versioned alias ``_ALIAS_RUNNER_BASE_RE`` admits -- or an entry of
            # ``_LAUNCHER_BASES`` or the ``_PYTHON_BASE_RE`` family, case-insensitively.
            # The alias belongs here for the same reason the rest do: the row this file
            # emits for it names no joined-line rule, so the program name is the only
            # thing that separates a real uncapped run from a command that merely names
            # one. Echoing the token instead would put its casing on the line, and
            # casing is the one caller-chosen thing left in a word already known to be
            # one of those.
            #
            # An alias is the exception, and it is printed as a fixed LABEL rather than
            # as itself. ``_ALIAS_RUNNER_BASE_RE`` is a SHAPE, so what it admits is
            # ``pytest-`` or ``py.test-`` followed by any digits -- and a secret of that
            # shape is a secret this branch would otherwise echo onto the line and into
            # the conductor's context. The label says which stem fired, which is the
            # whole reason the program name is printed, and carries none of the digits.
            # That also keeps the printed vocabulary genuinely FIXED, as the guarantee
            # above states: every word this branch can emit is one of a closed set.
            keep(_alias_program_label(canonical) or canonical)
        elif name in _SAFE_LONG_FLAG_NAMES or _SAFE_SHORT_FLAG_RE.match(name):
            keep(name)
        elif not assigned and _SAFE_SHORT_FLAG_RE.match(bare[:2]):
            # A short flag with its value glued on. Printing the name and dropping
            # the value is what the ``=`` spelling already does one branch up, so
            # the two spellings of one flag read the same and neither adds to the
            # withheld count -- a flag shown without its value says a value went.
            keep(bare[:2])
        else:
            withheld += 1
    if withheld:
        kept.append(f"+{withheld}")
    # Commas, not spaces: the line is read as whitespace-separated fields, and a
    # field that holds spaces stops being one field.
    return ",".join(kept) or "?"


def _proc_starttime_ticks(proc_root: Path, pid: str) -> int | None:
    """The ``starttime`` of the process at *pid* in clock ticks since boot, or None.

    This is the process INCARNATION token: a pid is reused, but boot-relative
    starttime distinguishes one incarnation of that pid from the next. The banned
    scan reads ``cmdline`` at one instant and the age at another, so it captures
    this token beside the cmdline and checks it again before emitting; a mismatch
    means the pid was recycled between the reads and the two facts describe two
    processes, so the age is not printed. The signed pid supervisor reads the same
    field to bind a mapping to a process incarnation; here it is read only to
    compare, never to grant anything.

    ``/proc/<pid>/stat`` field 22 is ``starttime``. The ``comm`` field (field 2)
    can hold spaces and parentheses, so the parse resumes after the LAST ``)``; a
    comm like ``(sh )nasty)`` keeps its own parentheses out of the field split.
    None on any unreadable or malformed input -- the caller treats an unreadable
    token exactly like a mismatch and emits ``age=?s``.
    """
    try:
        stat = (proc_root / pid / "stat").read_text(encoding="ascii", errors="replace")
        rparen = stat.rindex(")")
        return int(stat[rparen + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _proc_age_secs(proc_root: Path, pid: str, expected_start: int | None) -> int | None:
    """How many seconds the process at *pid* has been alive, or None.

    The reader problem the age solves: a bare ``BANNED pid=`` line is the same
    every cycle whether the process the conductor stopped is still running or a
    new offender holds its recycled number -- pids are recycled, so the number
    alone cannot tell those apart, and a re-emitted line reads as either
    "handled, ignore" or "still burning the host" with no way to choose. The
    process's own age settles it: an age that grows across cycles marks one
    process still alive; a small age under a recycled number marks a fresh
    violation. The age is a fact about the running process, so it costs no state
    file and no second writer -- the probe stays read-only outside
    ``--mark-handled``.

    ``expected_start`` is the incarnation token captured before the per-pid reads
    and is REQUIRED. The banned scan reads ``cmdline``/``cwd`` and the age at
    DIFFERENT instants, so a pid recycled between them would splice one process's
    identity onto another process's age -- the fidelity fix's own fidelity defect.
    This re-reads ``starttime`` and returns None (rendered ``age=?s``) unless it
    still matches, so the age is emitted only when it provably describes the same
    process the reads did. A None token refuses as well: an age with no
    incarnation to anchor it cannot be trusted.

    Both reads are world-readable like ``/proc/<pid>/cmdline`` (the field this
    scan trusts), so a process owned by another user answers here even though its
    ``cwd``/``exe`` links do not. ``proc_root`` is threaded through rather than
    ``/proc`` hardcoded, so the test harness's ``KIROCREW_PROBE_PROC_ROOT``
    supplies both files, under the same containment rule as every other path here.

    ``/proc/<pid>/stat`` field 22 is ``starttime`` in clock ticks since boot.
    Any unreadable or malformed input returns None, which the caller renders as
    ``age=?s`` -- the same handling as an unreadable cwd, and never crashes the
    scan.

    The source is ``/proc`` plus ``os.sysconf`` for the clock tick rate, both
    POSIX-only. On a platform without them the age is genuinely uncomputable, so
    this returns None and the caller emits ``age=?s`` there too. The field is
    never omitted: a missing field would read as "no age" and let a reader assume
    the process is new, while ``age=?s`` says the age is unavailable. There is no
    stdlib-only process create-time source on Windows, so ``age=?s`` is the honest
    answer rather than a number from a guessed tick rate.
    """
    starttime_ticks = _proc_starttime_ticks(proc_root, pid)
    if starttime_ticks is None:
        return None
    # Bind the age to the incarnation the caller saw: the token is REQUIRED, so a
    # None token (starttime unreadable when the caller captured it) refuses too --
    # an age with no incarnation to anchor it cannot be trusted. If the pid was
    # recycled between the caller's capture and now, ``starttime`` differs from
    # the token and the age would belong to a different process. Refuse it -- the
    # caller renders ``age=?s``, the same unknown the Windows path already emits,
    # so this needs no new output shape.
    if expected_start is None or starttime_ticks != expected_start:
        return None
    try:
        uptime = float((proc_root / "uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    # The tick rate converts starttime into seconds and comes from ``os.sysconf``,
    # which is POSIX-only. Where it is absent there is no reliable rate, so the
    # age is genuinely uncomputable: return None (rendered ``age=?s``) rather than
    # guess a rate and print a wrong number. A wrong age reads as a real age, so a
    # reader trusts it; ``age=?s`` tells them the answer is unavailable.
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return None
    if hz <= 0:
        return None
    age = uptime - starttime_ticks / hz
    # A negative age means the two reads disagreed (clock skew, or a pid that
    # exited and its number was reused between the two opens); clamp to 0 rather
    # than print a value that reads as nonsense.
    return int(age) if age >= 0 else 0


def _host_lines(cfg: dict[str, Any]) -> tuple[list[str], str]:
    """Banned-process lines plus the host summary fragment."""
    banned_res = [
        re.compile(rx) for rx in cfg.get("banned_process_res") or list(DEFAULT_BANNED_RES)
    ]
    # The argv-side shape is switched off by ONE named key and by nothing else. An
    # operator who wants it gone says so about this shape; supplying, replacing or
    # emptying ``banned_process_res`` does not touch it, because a protection that
    # disappears as a side effect of an edit aimed at something else is removed by
    # someone who never decided to remove it.
    argv_shape_enabled = cfg.get("argv_runner_detection", True) is not False
    fleet = [p for p in cfg.get("fleet_worktrees") or []]
    lines: list[str] = []
    # /proc, with an env seam for the test harness only -- not a config key,
    # for the same containment reason as the other paths.
    proc_root = Path(os.environ.get("KIROCREW_PROBE_PROC_ROOT") or "/proc")
    banned = 0
    foreign = 0
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                # The process incarnation, captured BEFORE any other per-pid read.
                # cmdline, cwd and exe are separate /proc reads at different
                # instants; if the pid is recycled partway through, they describe
                # two processes. Capturing starttime first and re-reading it before
                # emit brackets the whole record: a change means the reads cannot
                # be trusted as one process, so the derived fields are withheld.
                start_tok = _proc_starttime_ticks(proc_root, entry.name)
                # argv is kept as a LIST, not just the space-joined string. The
                # banned-operation rules are arg-shaped and must still see the
                # joined form, but deciding whether this pid is a shell wrapper is
                # a question about ARGUMENTS -- and once the NUL separators are
                # replaced by spaces, an argument containing a space is
                # indistinguishable from two arguments, so `-o pipefail -c` cannot
                # be parsed back out of it.
                #
                # Only the TRAILING empty is dropped, and only because
                # ``/proc/<pid>/cmdline`` is NUL-TERMINATED: its final split element
                # is an artefact of the terminator, never an argument. An INTERIOR
                # empty entry is a real (if unusual) argument, and discarding one
                # silently re-spaces the joined string that a custom
                # ``banned_process_res`` pattern is matched against -- so a rule an
                # operator wrote against the real command line would quietly stop
                # matching.
                argv = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
                while argv and argv[-1] == "":
                    argv.pop()
                cmd = " ".join(argv).strip()
            except OSError:
                continue
            if cmd:
                hit = None
                for rx in banned_res:
                    hit = rx.search(cmd)
                    if hit is not None:
                        break
                if hit is None:
                    # No joined-line rule fired. One shape is invisible from there and
                    # only from there: a versioned alias, whose name in a command line
                    # is indistinguishable from a path component or a package name
                    # unless the token's POSITION is read. Asked here, of the argv,
                    # AFTER every rule has declined -- so this path can only ADD a
                    # line, never change the rule a matching pid reports.
                    #
                    # It is asked whatever the rule list holds, because rule ORIGIN is
                    # what carries built-in authority in this file -- the same basis
                    # ``_is_shell_command_wrapper``'s exemption is written against. A
                    # gate on ``banned_process_res`` merely being SET would let a
                    # config edit switch a built-in protection off, which is a worse
                    # property than the extra line it would suppress. An operator who
                    # wants this shape gone says so with ``argv_runner_detection``,
                    # which names the shape it disables.
                    if not argv_shape_enabled:
                        continue
                    if not _argv_is_uncapped_argv_only_runner(argv, entry):
                        continue
                    matched = ARGV_RUNNER_RULE_LABEL
                    match_span = (0, len(cmd))
                else:
                    # The rule's own text, so a reader can see WHICH shape fired, and the
                    # match position, so the line can name the one command it fired on
                    # rather than the whole script that command sits in.
                    matched = hit.re.pattern
                    match_span = hit.span()
                # The rule matched somewhere in the joined cmdline, which for a
                # shell running a command string is the shell's ARGUMENT and not
                # the program this pid is running. See ``_is_shell_command_wrapper``
                # for why dropping the wrapper costs no coverage, and
                # ``_trusted_program_base`` for why the kernel's idea of the program
                # beats the process's own argv[0].
                #
                # Only a BUILT-IN rule earns the exemption. Its premise is that a
                # genuinely running wrapped tool has its own pid for the next
                # ``/proc`` walk to find, which holds for the two default rules
                # because both name a long-running test runner. An operator's own
                # rule can name a SHORT-LIVED command instead -- ``\bcurl\b`` against
                # a shell that sleeps two minutes and then makes one request is
                # visible for two minutes as the shell and for milliseconds as
                # ``curl`` -- so exempting that wrapper would drop the only sample the
                # probe was ever going to get. A custom rule therefore reports the
                # wrapper, which is the pre-fix behaviour and the fail-closed
                # direction for a monitoring control.
                # A built-in pattern selects a CANDIDATE out of the joined cmdline;
                # for the vitest rule the argv then decides whether the candidate is
                # an invocation, because the joined text cannot tell a phrase inside
                # one argument from two adjacent arguments. See
                # ``_invokes_bare_vitest_run``. A custom ``banned_process_res`` keeps
                # the joined-text decision whole: an operator's rule is a statement
                # about the text they wrote it against, and narrowing it with a
                # built-in runner's argv shape would answer a question they never
                # asked.
                if matched == _VITEST_BANNED_RE and not _invokes_bare_vitest_run(argv):
                    continue
                if matched in _BUILTIN_SHAPES and _is_shell_command_wrapper(
                    argv, _trusted_program_base(entry)
                ):
                    continue
                # A pid that reaches here under the built-in pytest rule and is NOT a
                # shell holding a script IS the runner, so its whole argv is ONE
                # command. The rule's lookahead SELECTED it off a JOINED command line,
                # where a numeric ``-n`` anywhere after the runner is enough; the TOKENS
                # decide, because only they can say which ``-n`` came LAST (argparse
                # last-wins: ``-n 4 -n auto`` is budgeted) and only they cannot be
                # fooled by an argument's own ``|``, ``;`` or ``&`` -- ``/proc``
                # separates arguments with NUL. The scan starts after the RUNNER's own
                # token: a launcher in front of it has its own options.
                if matched == DEFAULT_BANNED_RES[0] and _argv_worker_pool_is_budgeted(argv):
                    continue
                # A banned SHAPE is only a banned OPERATION when the fleet owns
                # it. The same unbounded pytest run in an unrelated checkout is
                # this machine's business, and counting it made the conductor
                # stop a worker that was not the offender -- so the cwd decides
                # which counter it lands in, and only fleet-owned or unreadable
                # matches are printed at all.
                cwd_class = _owner_class(entry, fleet, cmd)
                if cwd_class == "foreign":
                    foreign += 1
                    continue
                banned += 1
                # Re-read the incarnation token now that every per-pid read is
                # done. If it is unreadable or differs from the one captured
                # before the reads, the pid was recycled partway through and the
                # cmdline, cwd and age describe more than one process -- so the
                # WHOLE record is stale, not just the age. Withhold both derived
                # fields: cwd drops to ``unknown`` (the non-stopping class, so a
                # spliced record can never trigger a stop against an innocent
                # worker) and age to ``?s``. pid and rule still print, so the
                # violation is not silently dropped and the next cycle re-observes.
                #
                # EVERY per-pid read in the emission path is bracketed by this one
                # token, so there is no third unguarded read:
                #   * ``/proc/<pid>/stat``  -> start_tok (captured first, above)
                #   * ``/proc/<pid>/cmdline`` (rule + argv)   -- inside the bracket
                #   * ``/proc/<pid>/exe``  (_trusted_program_base) -- inside
                #   * ``/proc/<pid>/cwd``  (_owner_class)          -- inside
                #   * ``/proc/<pid>/stat`` -> end_tok (this line): start==end proves
                #     the four reads above saw ONE incarnation, else cwd->unknown.
                #   * ``/proc/<pid>/stat``+``/proc/uptime`` (age) -- independently
                #     re-bound to start_tok inside ``_proc_age_secs`` (mismatch or
                #     None -> ``age=?s``).
                # starttime is monotonic per boot, so a recycle anywhere in the
                # window necessarily changes it and is caught; a recycle back to
                # the same starttime is impossible.
                end_tok = _proc_starttime_ticks(proc_root, entry.name)
                incarnation_stable = start_tok is not None and end_tok == start_tok
                if not incarnation_stable:
                    cwd_class = "unknown"
                # pid + WHICH RULE fired + the cwd class is everything the
                # conductor needs (stop the owner, re-seed with the directive).
                # The argv is deliberately not echoed: a command line can carry
                # credentials or presigned URLs, and this line lands in the
                # conductor's model context.
                #
                # ``age=`` is what makes a re-emitted line readable across
                # cycles: a bare pid cannot say whether the process the conductor
                # stopped is still running or a new offender holds its recycled
                # number, so the same line reads as either handled-ignore or
                # still-burning with no way to choose. A process age that grows
                # across cycles marks one process still alive; a small age marks a
                # fresh violation. The age comes from the running process, so it
                # costs no state and keeps the probe read-only outside
                # ``--mark-handled``. An unreadable or stale age prints ``age=?s``,
                # like an unknown cwd, and never blocks the line.
                #
                # ``scope=`` answers the orthogonal question. ``age`` says whether
                # this is the same offender as last cycle; ``scope`` says which of
                # two matches to reach for first, because the rule alone cannot
                # separate a whole-suite run from a one-file run that merely omitted
                # a flag. It ranks and never gates -- the stop itself stays keyed to
                # ``cwd=fleet``. It is the one thing derived FROM the argv rather
                # than dropped with it, and it is three fixed words, so it carries
                # severity without carrying content.
                # ``cmd=`` is what makes a match judgeable without opening ``ps``:
                # which program the rule fired on, and which of its flags. It is
                # reduced to shapes that can hold no credential -- see
                # ``_redacted_command`` -- so it answers "is this a run or a
                # filename that reads like one" without echoing the argv.
                age = (
                    _proc_age_secs(proc_root, entry.name, start_tok) if incarnation_stable else None
                )
                age_field = "?" if age is None else str(age)
                lines.append(
                    f"BANNED pid={entry.name} rule={matched} cwd={cwd_class} "
                    f"age={age_field}s scope={_run_scope(argv)} cmd={_redacted_command(cmd, match_span)}"
                )
    per_cpu = None
    if hasattr(os, "getloadavg"):
        try:
            per_cpu = os.getloadavg()[0] / max(os.cpu_count() or 1, 1)
        except OSError:
            per_cpu = None
    mem_gb = None
    meminfo = proc_root / "meminfo"
    try:
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                mem_gb = int(line.split()[1]) / 1_048_576
                break
    except (OSError, ValueError, IndexError):
        mem_gb = None
    hot = per_cpu is not None and per_cpu > float(cfg.get("load_alert_per_cpu", 1.5))
    load_part = (
        f"load/cpu {per_cpu:.2f} ({'hot' if hot else 'ok'})"
        if per_cpu is not None
        else "load/cpu n/a"
    )
    mem_part = f"mem {mem_gb:.0f}G" if mem_gb is not None else "mem n/a"
    return lines, f"{load_part} | {mem_part} | banned {banned} | foreign {foreign}"


def _sessions_dir() -> Path:
    """DERIVED, never configurable: this gateway's own session store."""
    return data_home() / "sessions"


def _transcript_path(sessions_dir: Path, key: str) -> Path | None:
    """The transcript file for ``key``, or None when no safe transcript exists.

    ``session_create`` answers a slot key while the store prefixes the surface
    (``dashboard_<slot>.jsonl``) and colon-form session keys use ``:`` where
    the filename uses ``_``. A raw key must not read as a missing session:
    GONE triggers reclaim, and a false GONE is how an active item gets
    duplicate-dispatched. The first SAFE existing candidate wins.

    Keys are validated against ``_KEY_RE`` before this is called, and an
    existing candidate is returned only if it resolves to a file directly
    under ``sessions_dir`` -- both halves of one rule: a key is a filename
    stem, never a path. A candidate that exists but resolves elsewhere (a
    symlink out of the store) is treated as MISSING, never returned: None is
    the answer, and None reads as GONE.
    """
    candidates = (key, f"dashboard_{key}", key.replace(":", "_"))
    root = sessions_dir.resolve()
    for candidate in candidates:
        path = sessions_dir / f"{candidate}.jsonl"
        if path.exists() and path.resolve().parent == root:
            return path
    return None


def _handled_of(state: dict[str, Any]) -> dict[str, Any]:
    """The handled map, tolerating a corrupted state file: anything that is
    not a dict reads as empty (worst case a handled signal re-fires once),
    never as a crashed patrol."""
    handled = state.get("handled")
    return handled if isinstance(handled, dict) else {}


def run_probe(cfg: dict[str, Any], state_path: Path) -> int:
    sessions: list[str] = list(cfg.get("sessions") or [])
    sessions_dir = _sessions_dir()
    idle_secs = int(cfg.get("idle_alert_secs", 900))
    tail_bytes = int(cfg.get("tail_bytes", 200_000))
    err_res = [re.compile(rx) for rx in (list(DEFAULT_ERR_RES) + list(cfg.get("err_res") or []))]
    init_res = [
        re.compile(rx) for rx in cfg.get("init_timeout_res") or list(DEFAULT_INIT_TIMEOUT_RES)
    ]
    watchdog_res = [re.compile(rx) for rx in cfg.get("watchdog_res") or list(DEFAULT_WATCHDOG_RES)]
    handled = _handled_of(_load_state(state_path))

    fired = 0
    init_timeouts = 0
    watchdogs = 0
    for key in sessions:
        path = _transcript_path(sessions_dir, key)
        age: int | None = None
        if path is not None:
            try:
                age = int(time.time() - path.stat().st_mtime)
            except OSError:
                age = None
        if path is None or age is None:
            # GONE flows through the same suppression as every other tag: an
            # acted-on GONE (item reclaimed, mark-handled) must not re-fire
            # every cycle until the key is dropped from the watch list.
            tag, tail, age_text, index = "GONE", "transcript missing", "?", None
        else:
            entries, index = _tail_entries(path, tail_bytes)
            tag, tail = _classify(entries, err_res)
            # Counted for every watched session, fired or not: an undelivered
            # session is a fleet fact, not a per-tag one.
            init_timeouts += 1 if _tail_matches(entries, init_res) else 0
            watchdogs += 1 if _tail_matches(entries, watchdog_res) else 0
            if tag not in _FIRING:
                # A worker that filed a terminal report and then wrote one
                # unprefixed line is FINISHED. Ageing it into IDLE says the
                # opposite, and the two readings call for opposite actions
                # (close the item vs. nudge or reclaim it), so TERMINAL takes
                # precedence over the clock.
                #
                # ``tag == "-"`` is load-bearing: the non-firing set holds BOTH
                # ``-`` and ``WORKING``, so without it a worker that stood down,
                # was re-seeded, and is now reporting ``WORKING:`` would read as
                # finished and have its live work closed. WORKING is a protocol
                # message and means active work; only an unprefixed tail can
                # inherit a terminal disposition. A WORKING tail that then goes
                # silent still ages into IDLE, which is the correct nudge.
                if tag == "-" and _recorded_proto(handled, key) in TERMINAL_TAGS:
                    tag = TERMINAL_TAG
                elif age > idle_secs:
                    tag = IDLE_TAG
                elif _stalled_since_disposition(handled, key, index, idle_secs):
                    # Ranked BELOW the clock deliberately. A cold transcript is
                    # already fully described by IDLE, whose action -- nudge, then
                    # the intervention ladder -- is the right one. What IDLE
                    # cannot see is the session held WARM by traffic it never
                    # answers: inbound nudges keep the mtime fresh while nothing
                    # comes out. That is the case this tag exists for, and its
                    # action is different: check the EFFECT rather than liveness.
                    tag = NOPROGRESS_TAG
            if tag not in _FIRING:
                continue
            age_text = str(age)
        digest = _digest(f"{tag}:{tail}")
        if _suppressed(handled, key, tag, digest, idle_secs):
            # An answered ERR must not bury a ruling nobody has answered. The
            # error branch outranks the sticky walk, which is right on the first
            # cycle -- a crashing session is the more urgent reading -- but the
            # error row stays LAST for as long as the session is wedged, so the
            # ERR is re-classified and re-suppressed every cycle and the BLOCKED
            # underneath it is never reached. The deferral this was documented as
            # costing (one cycle) was in fact unbounded.
            surfaced = False
            if tag == "ERR":
                pending = _sticky_pending(entries)
                if pending is not None:
                    sticky_digest = _digest(f"{pending[0]}:{pending[1]}")
                    if not _suppressed(handled, key, pending[0], sticky_digest, idle_secs):
                        tag, digest = pending[0], sticky_digest
                        surfaced = True
            if not surfaced:
                # The report itself is dealt with. Whether anything has come OUT
                # of the session since is a different question, and the
                # interesting case for it is exactly here: the ruling was
                # delivered, the tag went quiet, and the worker then produced
                # nothing at all. Without this the named tag masks the stall for
                # as long as it stays suppressed.
                # A finished worker produces nothing BY DEFINITION, so absence of
                # output is not a stall. This guard is separate from the
                # terminal/idle ladder above because that ladder is only reached
                # for a NON-firing tag: a delivered worker whose `GREEN:` is still
                # the newest row in the window keeps a firing tag, so it arrives
                # here instead and would be reclassified as stalled -- then re-fire
                # every cycle, since this tag expires. That is the harm TERMINAL
                # exists to remove, so it is refused on both paths.
                if (
                    tag == NOPROGRESS_TAG
                    or _recorded_proto(handled, key) in TERMINAL_TAGS
                    or not _stalled_since_disposition(handled, key, index, idle_secs)
                ):
                    continue
                tag = NOPROGRESS_TAG
                digest = _digest(f"{tag}:{tail}")
                if _suppressed(handled, key, tag, digest, idle_secs):
                    continue
        fired += 1
        # Metadata ONLY: key, age, tag, index, digest. Transcript-derived text is
        # deliberately never printed -- the conductor's action table is
        # tag-keyed, and content, when a ruling needs it, is read through the
        # workspace-authorized session tools, not through this script. That
        # keeps the probe's output free of private session text no matter
        # which keys an (agent-authored) config watches. The index is a line
        # POSITION, so it carries no content either.
        index_text = "?" if index is None else str(index)
        print(f"🔔 {key:<28} {age_text:>5}s {tag:<9} i={index_text} d={digest}")

    banned_lines, host = _host_lines(cfg)
    for line in banned_lines:
        print(line)
    print(
        f"OK {len(sessions)} watched, {fired} fired | {host} | "
        f"deliver init-timeout {init_timeouts}, watchdog {watchdogs}"
    )
    return 0


def mark_handled(cfg: dict[str, Any], state_path: Path, key: str, tag: str, digest: str) -> int:
    if not _KEY_RE.fullmatch(key):
        print(f"malformed key {key!r}: keys are stems, never paths", file=sys.stderr)
        return 2
    tail_bytes = int(cfg.get("tail_bytes", 200_000))
    err_res = [re.compile(rx) for rx in (list(DEFAULT_ERR_RES) + list(cfg.get("err_res") or []))]
    path = _transcript_path(_sessions_dir(), key)
    index: int | None = None
    if path is not None and path.exists():
        entries, index = _tail_entries(path, tail_bytes)
        current_tag, tail = _classify(entries, err_res)
        # A signal the conductor cannot mark is worse than no signal at all. When
        # the probe reaches PAST a suppressed ERR to surface the sticky ruling
        # underneath it, the digest it prints is over the RULING -- but the error
        # row is still last here, so classifying again yields the ERR payload and
        # the compare-and-set below refuses the mark. The ruling would then fire
        # every cycle, undismissable. Resolving the same payload the probe printed
        # keeps the two halves of the protocol agreeing on what is being marked.
        if tag != current_tag and tag in STICKY_TAGS:
            pending = _sticky_pending(entries)
            if pending is not None and pending[0] == tag:
                tail = pending[1]
        del current_tag  # the digest is keyed on the CALLER's tag, like the probe's
    else:
        tail = "transcript missing"  # mirror run_probe's GONE payload exactly
    current = _digest(f"{tag}:{tail}")
    if current != digest:
        # Compare-and-set: a new same-tag payload arrived between the probe
        # and this mark. Digesting what is there NOW would suppress a signal
        # nobody has read -- refuse, so the caller re-probes and acts on the
        # payload that actually exists.
        print(
            f"refused: {key} payload changed since the probe (re-probe and act on it)",
            file=sys.stderr,
        )
        return 3
    state = _load_state(state_path)
    handled = _handled_of(state)
    state["handled"] = handled
    entry: dict[str, Any] = {
        "tag": tag,
        "digest": current,
        "ts": int(time.time()),
    }
    if index is not None:
        entry["index"] = index
    # The last dispositioned PROTOCOL tag survives a later non-protocol
    # disposition, because "this worker filed a terminal report" and "this
    # worker went quiet" are different facts and the second must not erase the
    # first. Carried forward from the previous entry when this mark is not
    # itself a protocol tag.
    # ONE field records the last payload disposition, and it is written on EVERY
    # mark: set when this mark IS a payload, carried forward when it is not.
    #
    # The carry is unconditional, and one field carries both readings, because
    # the rule is not "condition marks preserve payloads" but "a mark that is not
    # itself a payload cannot erase one". Gating the carry on the condition tags
    # alone leaves the same data loss reachable one door down: an `ERR`
    # disposition on a session whose `BLOCKED` was answered overwrites the
    # answer, and once a heartbeat stops the error row being last, the answered
    # ruling presents again.
    previous = handled.get(key)
    previous = previous if isinstance(previous, dict) else {}
    prior_tag, prior_digest = previous.get("tag"), previous.get("digest")
    if tag in _PAYLOAD_TAGS:
        entry["settled"] = {"tag": tag, "digest": digest}
    elif (
        isinstance(prior_tag, str) and prior_tag in _PAYLOAD_TAGS and isinstance(prior_digest, str)
    ):
        # The previous mark WAS the payload. Reading it off the entry keeps both
        # halves, which matters for a state file written before ``settled``
        # existed: that shape records an answered payload as the entry itself, and
        # suppression needs the digest as well as the tag. Carrying only the
        # legacy tag presented every answered ruling again on the first cycle
        # after an upgrade.
        entry["settled"] = {"tag": prior_tag, "digest": prior_digest}
    elif isinstance(previous.get("settled"), dict):
        entry["settled"] = previous["settled"]
    handled[key] = entry
    state["updated_at"] = int(time.time())
    _atomic_write(state_path, json.dumps(state, indent=1, sort_keys=True) + "\n")
    print(f"handled {key} {tag}")
    return 0


def _config_error(cfg: dict[str, Any]) -> str | None:
    """The first problem with a parsed config, or None. Typed misconfiguration
    is malformed config (exit 2 with a message), never an uncaught crash."""
    for key in (
        "sessions",
        "err_res",
        "banned_process_res",
        "init_timeout_res",
        "watchdog_res",
        "fleet_worktrees",
    ):
        value = cfg.get(key)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            return f"{key} must be a list of strings"
    for item in cfg.get("sessions") or []:
        if not _KEY_RE.fullmatch(item):
            return f"session key {item!r} is not a plain key (keys are stems, never paths)"
    # The opt-out must be a real boolean when it is present at all. A string or a number
    # would be read as "not False" and silently leave the shape ON, so an operator who
    # meant to disable it keeps a protection they decided to remove -- and would find out
    # only from a line they thought they had switched off. The other direction is worse
    # still: a value read as falsey would disable a protection nobody named. ``null`` is
    # deliberately NOT refused: it means the key is not set, which is the same as omitting
    # it, and an unset key leaves the shape ON -- the safe direction either way.
    argv_detection = cfg.get("argv_runner_detection")
    if argv_detection is not None and not isinstance(argv_detection, bool):
        return "argv_runner_detection must be true or false"
    # A relative worktree root would be compared against an absolute
    # /proc/<pid>/cwd target and could never match, so every banned run inside
    # the fleet would be filed as foreign and go unreported. Say so at load time
    # rather than silently muting the signal.
    for item in cfg.get("fleet_worktrees") or []:
        if "\0" in item:
            # A NUL can never appear in a real path, so this entry could only ever
            # fail to match -- and it fails LOUDLY: the path calls raise
            # ValueError, not OSError, so it escapes the scan's exit-race handling
            # and takes the whole cycle down, losing every other session's reading
            # with it. Same reasoning as the relative-path check below: an entry
            # that cannot match is malformed config, said at load time.
            return "fleet_worktrees entry contains a NUL byte"
        if not os.path.isabs(item):
            return f"fleet_worktrees entry {item!r} must be an absolute path"
        # A root that matches EVERYTHING is the dangerous direction, and this key
        # is read from a config an agent authors -- so it is a trust boundary, not
        # a typo class. ``cwd=fleet`` is the one class that stops a session, so a
        # root of ``/`` turns the ownership guard into a false-stop generator
        # against unrelated processes on the same host: precisely the harm that
        # scoping the scan was introduced to prevent, reintroduced through config.
        # Refused rather than narrowed, because silently ignoring an entry would
        # leave the conductor believing a scope it does not have.
        norm = _norm_path(item)
        store = _norm_path(str(_sessions_dir()))
        # BOTH spellings are judged, because the classifier compares both. Its
        # realpath second chance exists so a symlinked worktree still matches --
        # which means a root spelled as a symlink to `/` passes a literal-only
        # check and then matches every cwd on the host. The widening comes back
        # through the door the convenience opened, so validation follows it.
        try:
            candidates = {norm, _norm_path(os.path.realpath(item))}
        except (OSError, ValueError):
            return f"fleet_worktrees entry {item!r} cannot be resolved"
        for candidate in candidates:
            if candidate == _norm_path(os.path.dirname(candidate) or candidate):
                return (
                    f"fleet_worktrees entry {item!r} resolves to a filesystem root "
                    "and matches everything"
                )
            if _under(store, candidate):
                # One level up from the same widening: the session store is the
                # conductor's own data directory, never a worktree, so a root that
                # contains it makes the conductor and every sibling process read as
                # a fleet worker eligible to be stopped.
                return f"fleet_worktrees entry {item!r} contains the session store"
    for key in ("idle_alert_secs", "tail_bytes", "load_alert_per_cpu"):
        value = cfg.get(key)
        if value is None:
            continue
        # bool is an int subclass, and JSON permits NaN/Infinity: neither is a
        # usable threshold, and int(NaN) raises -- reject both up front.
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value != value
            or value in (float("inf"), float("-inf"))
            or value < 0
        ):
            return f"{key} must be a finite non-negative number"
    for rx in (
        list(cfg.get("err_res") or [])
        + list(cfg.get("banned_process_res") or [])
        + list(cfg.get("init_timeout_res") or [])
        + list(cfg.get("watchdog_res") or [])
    ):
        try:
            re.compile(rx)
        except re.error as exc:
            return f"bad regex {rx!r}: {exc}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--mark-handled",
        nargs=3,
        metavar=("KEY", "TAG", "DIGEST"),
        help="record the fired signal as handled; DIGEST is the d= field of the"
        " fired line, and a stale digest is refused (exit 3)",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("config must be a JSON object")
    except (OSError, ValueError) as exc:
        print(f"malformed config: {exc}", file=sys.stderr)
        return 2
    problem = _config_error(cfg)
    if problem is not None:
        print(f"malformed config: {problem}", file=sys.stderr)
        return 2
    # Derived, never configurable -- see the module docstring: a config-chosen
    # destination would make this no-write agent's one approved writer an
    # arbitrary-path file replacer.
    state_path = Path(f"{config_path}.state.json")
    if args.mark_handled:
        return mark_handled(cfg, state_path, *args.mark_handled)
    return run_probe(cfg, state_path)


if __name__ == "__main__":
    sys.exit(main())
