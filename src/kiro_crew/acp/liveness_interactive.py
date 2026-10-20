"""Interactive-command classification: whether a shell command is expected to want a
terminal, decided before it runs.

:func:`classify_interactive_command` reads a cached shell-tool input against a table of
known programs and answers an :class:`InteractiveClassification`; the section comment
below says what the watchdog does with it. A new program or rule goes in this module's
tables; :mod:`kiro_crew.acp.liveness` re-exports every name here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from kiro_crew.acp.liveness_command import _command_text, bounded_command_text

#: The facade's logger: a record from this module keeps the name ``kiro_crew.acp.liveness``.
logger = logging.getLogger("kiro_crew.acp.liveness")


# ── Interactive-command classification (tool layer, pre-dispatch) ──
#
# The tool layer says BEFORE a shell command runs whether it is expected to
# want a terminal: a pager, an editor, a REPL reading stdin, a package
# manager's confirmation, a credential prompt. The classification is a
# TABLE of known programs, not a rewrite of arbitrary flags, and it produces
# three things and nothing else: a risk class, a non-interactive HINT for the
# recovery nudge, and whether a replay could repeat side effects. It never
# rewrites the command (the repo has no env layer on the ACP tool path — the
# hook gate can only allow or deny), never answers a prompt, and is
# deliberately conservative about ``side_effecting``: only a pager over a
# read-only viewer is a safe replay; everything else is assumed to have
# possibly acted (fail-closed, SPEC-ADDENDUM §6 / RFC §14.6).
#
# What the classification feeds: the watchdog narrows the platform-limited
# UNKNOWN window to the ordinary silence budget when the command was
# ``narrowing_eligible`` (a prompt-shaped program that the missing stdin
# evidence would have caught), and a stall on such a command is classified
# ``waiting_input`` instead of an opaque tool stall. A pager is advisory only:
# under the tool the stdout is a pipe, so a pager degrades to ``cat`` on its
# own, and the risk exists to name the hint, not to shorten anything.

INTERACTIVE_NONE = "none"
INTERACTIVE_PAGER = "pager"
INTERACTIVE_EDITOR = "editor"
INTERACTIVE_REPL = "repl"
INTERACTIVE_CONFIRM = "confirm"
INTERACTIVE_PROMPT = "prompt"

#: Risk classes whose blocked read the STUCK_INPUT evidence would have caught
#: on Linux; on a platform without that evidence they justify narrowing the
#: no-progress window. A pager is excluded on purpose (see above).
INTERACTIVE_NARROWING_RISKS: frozenset[str] = frozenset(
    {INTERACTIVE_EDITOR, INTERACTIVE_REPL, INTERACTIVE_CONFIRM, INTERACTIVE_PROMPT}
)

_PAGER_PROGRAMS = frozenset({"less", "more", "most", "man"})
_EDITOR_PROGRAMS = frozenset({"vi", "vim", "nvim", "nano", "pico", "emacs", "ed"})
# Programs that read a script from stdin when given no file, expression or
# command argument — an open, never-closed stdin pipe hangs them forever.
_REPL_PROGRAMS = frozenset(
    {
        "python",
        "python3",
        "python2",
        "node",
        "irb",
        "ruby",
        "perl",
        "psql",
        "mysql",
        "sqlite3",
        "redis-cli",
        "cat",
        "read",
    }
)
# Option prefixes that give a REPL program something to run other than stdin.
_REPL_NONINTERACTIVE_OPTS = ("-c", "-e", "-f", "--command", "--eval", "--file", "-m", "-p")
# Git subcommands that page when stdout is a terminal.
_GIT_PAGED_SUBCOMMANDS = frozenset(
    {"log", "diff", "show", "blame", "reflog", "branch", "tag", "grep", "stash", "shortlog"}
)
# Package-manager confirmations: program -> (subcommands that confirm, flags that skip it).
_CONFIRM_TABLE: dict[str, tuple[frozenset[str], tuple[str, ...], str]] = {
    "apt": (
        frozenset({"install", "remove", "purge", "upgrade", "dist-upgrade", "autoremove"}),
        ("-y", "--yes", "--assume-yes"),
        "apt-get -y … (or DEBIAN_FRONTEND=noninteractive)",
    ),
    "apt-get": (
        frozenset({"install", "remove", "purge", "upgrade", "dist-upgrade", "autoremove"}),
        ("-y", "--yes", "--assume-yes"),
        "apt-get -y … (or DEBIAN_FRONTEND=noninteractive)",
    ),
    "yum": (
        frozenset({"install", "remove", "erase", "update", "upgrade"}),
        ("-y", "--assumeyes"),
        "yum -y …",
    ),
    "dnf": (
        frozenset({"install", "remove", "erase", "update", "upgrade"}),
        ("-y", "--assumeyes"),
        "dnf -y …",
    ),
    "pacman": (frozenset({"-S", "-R", "-Syu", "-Su"}), ("--noconfirm",), "pacman --noconfirm …"),
    "pip": (frozenset({"uninstall"}), ("-y", "--yes"), "pip uninstall -y …"),
    "pip3": (frozenset({"uninstall"}), ("-y", "--yes"), "pip3 uninstall -y …"),
    "conda": (frozenset({"install", "remove", "update", "create"}), ("-y", "--yes"), "conda -y …"),
    "npm": (frozenset({"init", "create"}), ("-y", "--yes"), "npm init -y …"),
    "yarn": (frozenset({"init", "create"}), ("-y", "--yes"), "yarn init -y …"),
}
# Credential / terminal prompts: program -> (flags that make it non-interactive, hint).
#
# ``BatchMode`` is recognised but not proposed: once case is folded it contains a
# permission verb, so a proposed ``ssh -o BatchMode=yes … /usr/…`` is refused by
# the permission deny rows. ssh_config(5) says BatchMode disables password prompts
# and host key confirmation, and the hint proposes options aimed at the same
# prompts. They are proposed, not trusted: BatchMode alone still counts as proof
# of no prompt, because a FIDO/sk key PIN or an encrypted key's passphrase may
# still be asked for under the proposed options.
_SSH_BATCH_FLAGS = ("-o BatchMode=yes", "-oBatchMode=yes")
_SSH_NONINTERACTIVE_OPTIONS = (
    "-o StrictHostKeyChecking=yes -o NumberOfPasswordPrompts=0 "
    "-o PasswordAuthentication=no -o KbdInteractiveAuthentication=no"
)
_PROMPT_TABLE: dict[str, tuple[tuple[str, ...], str]] = {
    "sudo": (
        ("-n", "--non-interactive", "-S", "--stdin"),
        "sudo -n … (fails instead of prompting)",
    ),
    "ssh": (_SSH_BATCH_FLAGS, f"ssh {_SSH_NONINTERACTIVE_OPTIONS} …"),
    "scp": ((*_SSH_BATCH_FLAGS, "-B"), "scp -B …"),
    "sftp": ((*_SSH_BATCH_FLAGS, "-b"), "sftp -b <batchfile> …"),
    "gpg": (("--batch",), "gpg --batch …"),
    "passwd": ((), "passwd cannot run non-interactively under the tool"),
    "su": ((), "su cannot run non-interactively under the tool"),
    "docker": (("--password-stdin", "-p", "--password"), "docker login --password-stdin"),
    "gh": (("--with-token",), "gh auth login --with-token < token-file"),
    "aws": ((), "set AWS_* env or --profile instead of `aws configure`"),
}
# Prompt-table programs whose prompt is bound to ONE subcommand.
_PROMPT_SUBCOMMAND: dict[str, str] = {"docker": "login", "gh": "auth", "aws": "configure"}
# Shell wrappers to skip when finding the program a segment runs.
_WRAPPER_PROGRAMS = frozenset(
    {"env", "nohup", "nice", "ionice", "time", "command", "exec", "stdbuf"}
)
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;]|\n")


@dataclass(frozen=True)
class InteractiveClassification:
    """What the tool layer expects a shell command to want from a terminal.

    ``risk`` is one of the ``INTERACTIVE_*`` classes (``none`` when nothing in
    the table matched). ``program`` names the matched program, ``reason`` says
    which rule fired, ``hint`` is the non-interactive form the recovery nudge
    may PROPOSE (never applied; retries stay inside the granted approval scope
    and the original parameters). ``side_effecting`` is True unless the match
    is a read-only viewer: a replay of anything else could repeat an effect.
    """

    risk: str = INTERACTIVE_NONE
    program: str = ""
    reason: str = ""
    hint: str = ""
    side_effecting: bool = True

    @property
    def narrowing_eligible(self) -> bool:
        return self.risk in INTERACTIVE_NARROWING_RISKS

    @property
    def replay_safe(self) -> bool:
        """A non-interactive retry cannot repeat a side effect of THIS command.

        Only the classifier's own read-only verdict counts; the caller must
        additionally confirm no output/exit was recorded for the call (a command
        that already produced output may have already acted).
        """
        return self.risk != INTERACTIVE_NONE and not self.side_effecting


NOT_INTERACTIVE = InteractiveClassification()


def _segment_program(tokens: list[str]) -> tuple[str, list[str]]:
    """``(program, args)`` of one pipeline segment, skipping env and wrappers.

    ``sudo`` is NOT skipped: it is itself a prompt-shaped program (returned as
    the program so the prompt table sees it), unless it carries a flag that
    makes it non-interactive, in which case the wrapped program is classified.
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.rsplit("/", 1)[-1]
        if "=" in base and not base.startswith("-"):
            i += 1
            continue
        if base in _WRAPPER_PROGRAMS or base == "timeout":
            # Skip the wrapper's own flags and any numeric argument
            # (``nice -n 10``, ``timeout 30``, ``stdbuf -oL``).
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or tokens[i][:1].isdigit()):
                i += 1
            continue
        if base == "sudo":
            rest = tokens[i + 1 :]
            if any(_has_flag(rest, f) for f in _PROMPT_TABLE["sudo"][0]):
                # Non-interactive sudo: classify what it wraps, skipping its flags.
                j = i + 1
                while j < len(tokens) and tokens[j].startswith("-"):
                    j += 1
                i = j
                continue
            return "sudo", rest
        return base, tokens[i + 1 :]
    return "", []


def _has_flag(args: list[str], flag: str) -> bool:
    if " " in flag:
        joined = " ".join(args)
        return flag in joined
    for a in args:
        if a == flag or a.startswith(flag + "="):
            return True
        # Bundled short flags: ``-ny`` contains ``-y``.
        if len(flag) == 2 and flag.startswith("-") and a.startswith("-") and not a.startswith("--"):
            if flag[1] in a[1:]:
                return True
    return False


# Global git options that take their value as the NEXT token, so the
# subcommand scan must skip that token too (``git -C /x rebase``).
_GIT_VALUE_OPTIONS = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
)
# Editor values that exit at once and keep the message git prepared.
_NON_INTERACTIVE_EDITORS = frozenset({"true", ":"})
# Sequencer steps that write a commit message, keyed by subcommand. ``git am
# --continue`` is absent: it commits with the patch's own message.
_GIT_CONTINUE_EDITS = frozenset({"rebase", "cherry-pick", "merge", "revert"})
# ``git revert`` flags that skip the message editor or end the revert.
_GIT_REVERT_KEEPS_MESSAGE = ("--no-edit", "-n", "--no-commit", "--abort", "--quit", "--skip")


def _git_editor_settings(env_prefix: list[str], args: list[str]) -> tuple[dict[str, str], int]:
    """Editor settings visible in one git command line, and the subcommand index.

    Returns ``({"editor": ..., "sequence": ...}, sub_i)``. ``editor`` and
    ``sequence`` hold the effective value only when the command line itself
    sets it: an inline ``GIT_EDITOR=`` wins over ``-c core.editor=``, and
    ``GIT_SEQUENCE_EDITOR=`` over ``-c sequence.editor=``; the sequence editor
    falls back to the editor. ``sub_i`` is ``len(args)`` when ``-P`` /
    ``--no-pager`` is present, so no subcommand is read.
    """
    env = {}
    for tok in env_prefix:
        name, sep, value = tok.partition("=")
        if sep:
            env[name] = value.strip("'\"")
    config = {}
    sub_i = 0
    while sub_i < len(args) and args[sub_i].startswith("-"):
        opt = args[sub_i]
        if opt in ("-P", "--no-pager"):
            return {}, len(args)
        if opt in _GIT_VALUE_OPTIONS:
            if opt == "-c" and sub_i + 1 < len(args):
                key, _, value = args[sub_i + 1].partition("=")
                config[key.lower()] = value.strip("'\"")
            sub_i += 2
            continue
        sub_i += 1
    settings = {}
    editor = env.get("GIT_EDITOR", config.get("core.editor"))
    if editor is not None:
        settings["editor"] = editor
    sequence = env.get("GIT_SEQUENCE_EDITOR", config.get("sequence.editor", editor))
    if sequence is not None:
        settings["sequence"] = sequence
    return settings, sub_i


def _classify_segment(segment: str, *, last: bool) -> InteractiveClassification:
    stripped = segment.strip()
    if not stripped:
        return NOT_INTERACTIVE
    stdin_redirected = "<" in stripped
    stdout_redirected = ">" in stripped or not last
    tokens = [t for t in re.split(r"\s+", stripped) if t]
    program, args = _segment_program(tokens)
    if not program:
        return NOT_INTERACTIVE
    env_prefix = tokens[: len(tokens) - len(args) - 1]
    positional = [a for a in args if not a.startswith("-") and not a.startswith("<")]

    if program in _EDITOR_PROGRAMS:
        if program == "emacs" and (_has_flag(args, "--batch") or _has_flag(args, "-batch")):
            return NOT_INTERACTIVE
        return InteractiveClassification(
            INTERACTIVE_EDITOR,
            program,
            "editor needs a terminal",
            "write the file directly (heredoc / file tool) instead of opening an editor",
            side_effecting=True,
        )
    if program in _PAGER_PROGRAMS:
        if stdout_redirected:
            return NOT_INTERACTIVE
        return InteractiveClassification(
            INTERACTIVE_PAGER,
            program,
            "pager on a terminal stdout",
            f"{program} … | cat  (or PAGER=cat)",
            side_effecting=False,
        )
    if program == "git" and args:
        settings, sub_i = _git_editor_settings(env_prefix, args)
        sub = args[sub_i] if sub_i < len(args) else ""
        rest = args[sub_i + 1 :]
        editor_quiet = settings.get("editor") in _NON_INTERACTIVE_EDITORS
        sequence_quiet = settings.get("sequence") in _NON_INTERACTIVE_EDITORS
        # Built from the command's own arguments, so bounded like the command:
        # the classification is retained beside the tool-call row for the
        # length of the call, and every other hint in this table is a literal.
        editor_hint = bounded_command_text("GIT_EDITOR=true git " + " ".join(args))
        if (
            not editor_quiet
            and sub == "commit"
            and not any(
                _has_flag(rest, f)
                for f in ("-m", "--message", "-F", "--file", "--no-edit", "-C", "-c")
            )
        ):
            return InteractiveClassification(
                INTERACTIVE_EDITOR,
                "git commit",
                "git commit without -m opens an editor",
                "git commit -m '<message>' (or GIT_EDITOR=true)",
                side_effecting=True,
            )
        if (
            not editor_quiet
            and sub in _GIT_CONTINUE_EDITS
            and "--continue" in rest
            and "--no-edit" not in rest
        ):
            return InteractiveClassification(
                INTERACTIVE_EDITOR,
                f"git {sub} --continue",
                f"{sub} --continue opens an editor for the commit message",
                editor_hint,
                side_effecting=True,
            )
        if (
            not editor_quiet
            and sub == "revert"
            and any(not a.startswith("-") for a in rest)
            and not any(_has_flag(rest, f) for f in _GIT_REVERT_KEEPS_MESSAGE)
        ):
            return InteractiveClassification(
                INTERACTIVE_EDITOR,
                "git revert",
                "git revert without --no-edit opens an editor for the commit message",
                editor_hint,
                side_effecting=True,
            )
        if (
            sub == "rebase"
            and not sequence_quiet
            and (_has_flag(rest, "-i") or _has_flag(rest, "--interactive"))
        ):
            return InteractiveClassification(
                INTERACTIVE_EDITOR,
                "git rebase -i",
                "interactive rebase opens an editor",
                "non-interactive rebase (no -i), or GIT_SEQUENCE_EDITOR=true with a scripted todo",
                side_effecting=True,
            )
        if sub == "add" and (
            _has_flag(rest, "-p") or _has_flag(rest, "--patch") or _has_flag(rest, "-i")
        ):
            return InteractiveClassification(
                INTERACTIVE_PROMPT,
                "git add -p",
                "patch-mode add prompts per hunk",
                "git add <paths> (no -p / -i)",
                side_effecting=True,
            )
        if sub in _GIT_PAGED_SUBCOMMANDS and not stdout_redirected:
            if sub == "stash" and rest[:1] != ["list"]:
                return NOT_INTERACTIVE
            return InteractiveClassification(
                INTERACTIVE_PAGER,
                f"git {sub}",
                "git pages this subcommand on a terminal stdout",
                f"git -P {sub} …  (or GIT_PAGER=cat)",
                side_effecting=False,
            )
        return NOT_INTERACTIVE
    if program in _CONFIRM_TABLE:
        subs, skip_flags, hint = _CONFIRM_TABLE[program]
        sub = next((a for a in args if not a.startswith("-") or program == "pacman"), "")
        if sub in subs and not any(_has_flag(args, f) for f in skip_flags):
            return InteractiveClassification(
                INTERACTIVE_CONFIRM,
                program,
                f"{program} {sub} asks for confirmation",
                hint,
                side_effecting=True,
            )
        return NOT_INTERACTIVE
    if program in _PROMPT_TABLE:
        flags, hint = _PROMPT_TABLE[program]
        bound_sub = _PROMPT_SUBCOMMAND.get(program)
        if bound_sub is not None and (not positional or positional[0] != bound_sub):
            return NOT_INTERACTIVE
        if any(_has_flag(args, f) for f in flags):
            return NOT_INTERACTIVE
        # ``ssh host <remote command>`` is still prompt-shaped without
        # BatchMode: the risk is the host-key / password prompt, not the
        # remote work. The proposed options are NOT treated as proof, so the
        # classifier stays on the safe side for the prompts they may miss.
        return InteractiveClassification(
            INTERACTIVE_PROMPT,
            program,
            f"{program} may prompt for a credential or confirmation",
            hint,
            side_effecting=True,
        )
    if program in _REPL_PROGRAMS:
        if stdin_redirected or positional:
            return NOT_INTERACTIVE
        if any(a.startswith(_REPL_NONINTERACTIVE_OPTS) for a in args):
            return NOT_INTERACTIVE
        if program in ("cat", "read"):
            hint = f"{program} <file>  (or </dev/null)"
        else:
            hint = f"{program} -c '<code>' or {program} <file>  (or </dev/null)"
        return InteractiveClassification(
            INTERACTIVE_REPL,
            program,
            f"{program} with no script reads stdin",
            hint,
            side_effecting=program not in ("cat", "read"),
        )
    return NOT_INTERACTIVE


def classify_interactive_command(command: str) -> InteractiveClassification:
    """Classify a cached shell-tool input against the interactive table.

    The command is split into pipeline segments (``|``, ``;``, ``&&``,
    ``||``, newline) and the FIRST segment with a non-``none`` risk wins,
    except that a narrowing-eligible class outranks a pager found earlier: the
    pager is advisory, the prompt is what would hang. Never raises — any
    parse failure is ``NOT_INTERACTIVE`` (fail toward the standard window).
    """
    try:
        text = _command_text(command)
        segments = [s for s in _SEGMENT_SPLIT_RE.split(text) if s.strip()]
        best = NOT_INTERACTIVE
        for idx, seg in enumerate(segments):
            found = _classify_segment(seg, last=idx == len(segments) - 1)
            if found.risk == INTERACTIVE_NONE:
                continue
            if best.risk == INTERACTIVE_NONE or (
                found.narrowing_eligible and not best.narrowing_eligible
            ):
                best = found
        return best
    except Exception:
        logger.debug("interactive classification failed", exc_info=True)
        return NOT_INTERACTIVE


def non_interactive_hint(command: str) -> str:
    """The recovery nudge's suggestion for *command*, or "" when none applies."""
    return classify_interactive_command(command).hint
