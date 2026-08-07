"""Headless ``claude -p`` agent runner — the *intelligent* step of the loop.

Ported from the original framework's ``autoloop/claude_runner.py`` (which produced
17 perf + 18 bug draft CRs). The autonomous loop uses the model only for the steps
that need judgement — author a bug fix (reproducing test + source edit) inside an
isolated worktree, or refine a perf edit. Orchestration, gating (build + RED→GREEN /
A-B noise band), verification, dedup, and CR drafting stay deterministic Python in the
spine; THIS is the one place a model is invoked.

The runner is a thin subprocess wrapper that:
  - runs ``claude -p`` with a prompt (cwd'd into the candidate worktree),
  - bypasses interactive permission prompts (the worktree is throwaway + isolated and
    the clone is push-disabled — the blast radius is contained by gating + no-push),
  - enforces a timeout, accumulates cost so the driver's ``--max-cost`` ceiling is live,
  - NEVER raises on model-side failure (returns a result the caller inspects).

It is OPTIONAL and INJECTED: the spine works headless/offline without it (the offline
profile's ``propose`` realizes mechanical perf seeds and the bug ``propose`` returns
False = "awaiting the agent edit"). When a runner is wired (the backend app passes one),
bug candidates get a real authored fix and the loop produces real draft CRs — matching
the original framework end-to-end.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.config import KiroCrewConfig
from kiro_crew.hooks import TOOL_DENY, HookManager, hooks_config_from_config_dict
from kiro_crew.platform_compat import SIGKILL, kill_process_tree
from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv

from .git_safety import GIT_SAFE_CONFIG, require_pinned

logger = logging.getLogger(__name__)

# The headless Claude Code binary. Overridable for tests / alternate installs.
CLAUDE_BIN = os.environ.get("AUTO_IMPROVEMENT_CLAUDE_BIN", "claude")

#: Trusted git config for host-side git run over the agent-writable worktree — identical to the
#: driver/gate/commit `_GIT_SAFE_CONFIG`. The POST-AGENT presence checks below run `git status`
#: on the HOST as the gateway user, in the tree the sandboxed agent just wrote to. `git status`
#: consults `core.fsmonitor` and can SPAWN it, so an agent that set `core.fsmonitor` to a program
#: (or planted a hook via `core.hooksPath`) would get host-side, out-of-sandbox execution on the
#: next status. `-c` overrides on OUR argv beat the repo config. Raised by the GPT review.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG


@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    error: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    raw: dict = field(default_factory=dict)


# Longest single buffered line we'll let accumulate before force-flushing — so a wall of
# prose with no newline still emits readable chunks instead of one giant blob.
_LINE_FLUSH_LEN = 200


class _LineBuffer:
    """Coalesce a stream of tiny text chunks into COHERENT lines for the activity feed.

    The Kiro Crew provider streams assistant text as many sub-word fragments ("ismatched-",
    "ncation would"). Emitting one feed event per fragment makes the live log unreadable
    (the exact symptom the user reported). This buffers fragments and flushes a line only
    on a real boundary — a newline, a sentence end (``. ``/``! ``/``? ``), or when the
    buffer exceeds :data:`_LINE_FLUSH_LEN` — so each emitted ``💭`` line is a whole thought.

    ``emit(line)`` is the sink (called with each completed, stripped, non-empty line)."""

    def __init__(self, emit) -> None:
        self._emit = emit
        self._buf = ""

    def feed(self, chunk: str) -> None:
        self._buf += chunk
        # Flush every complete newline-terminated line.
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._send(line)
        # No newline yet, but a long buffer: flush at the last sentence boundary so we don't
        # sit on a paragraph that never contains a newline.
        if len(self._buf) >= _LINE_FLUSH_LEN:
            cut = max(self._buf.rfind(". "), self._buf.rfind("! "), self._buf.rfind("? "))
            if cut >= 40:  # only split on a boundary that yields a substantial line
                self._send(self._buf[: cut + 1])
                self._buf = self._buf[cut + 2 :]
            else:  # no good boundary — emit the whole overlong buffer as one line
                self._send(self._buf)
                self._buf = ""

    def flush(self) -> None:
        """Emit whatever's left as a final line (call at a tool boundary / turn end)."""
        if self._buf.strip():
            self._send(self._buf)
        self._buf = ""

    def _send(self, line: str) -> None:
        line = line.strip()
        if line:
            self._emit(line[:_LINE_FLUSH_LEN])


# Common rawInput keys that name a tool's TARGET (the thing the operator wants to see in the
# feed: which file was read/edited, which command ran). Checked in order; the first present,
# non-empty value wins. Mirrors how the dashboard derives a tool pill's subtitle.
_TOOL_TARGET_KEYS = (
    "path",
    "file_path",
    "filePath",
    "abs_path",
    "command",
    "cmd",
    "pattern",
    "query",
    "url",
)


def _tool_detail(ev: object) -> str:
    """A human-readable detail for a tool-call feed line — preferring the TARGET (file path /
    command) over a generic label. The initial ``EVENT_TOOL_CALL`` from claude-agent-acp
    often carries only a generic ``title`` ("Read File") with empty ``tool_input``; the real
    path arrives in the ``EVENT_TOOL_CALL_UPDATE`` refinement (``raw_tool_params`` populated).
    This pulls the target from ``raw_tool_params`` first, then falls back to a non-generic
    ``title``, then the raw ``tool_input`` — so the feed shows "Read File · src/.../ruler.py"
    instead of a bare "Read File"."""
    raw = getattr(ev, "raw_tool_params", None)
    target = ""
    if isinstance(raw, dict):
        for k in _TOOL_TARGET_KEYS:
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                target = v.strip()
                break
    title = (getattr(ev, "title", "") or "").strip()
    if target:
        # Combine the label + target when the title is a generic label (no path in it);
        # if the title already contains the target, just use the title.
        if title and target not in title:
            return f"{title} · {target}"[:160]
        return (title or target)[:160]
    # No structured target — fall back to the title, else the raw tool_input snippet.
    if title:
        return title[:160]
    return (getattr(ev, "tool_input", "") or "")[:80]


def _detail_is_richer(refined: str, prior: str) -> bool:
    """True if ``refined`` is a more informative tool detail than ``prior`` — i.e. it is
    LONGER or contains a path-like segment the prior line lacked. Guards the refinement
    re-emit so a tool_call_update that adds nothing (same generic label) does not double the
    feed, but one that adds the file path DOES upgrade the line."""
    if not refined:
        return False
    if not prior:
        return True
    if "/" in refined and "/" not in prior:
        return True
    return len(refined) > len(prior) + 3


# Shared with the gate — see ``push_policy.strip_credential_env``.
from .push_policy import strip_credential_env  # noqa: E402


def _unlink_quietly(path: object) -> None:
    """Remove a sandbox launcher temp file. Never raises — losing the cleanup of a temp
    file must not fail an otherwise-successful agent run."""
    if not path:
        return
    try:
        Path(str(path)).unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove sandbox launcher temp file", exc_info=True)


#: Forbidden ``(binary, subcommand-path)`` pairs. Structured rather than flat strings
#: because a substring match is trivially evaded by a GLOBAL OPTION between the two:
#: ``gh --repo o/r pr ready 123`` and ``git -C /tmp/x push`` both slipped past a
#: ``"gh pr ready"``/``"git push"`` check. Measured before fixing. Raised by review.
#: GLOBAL options that consume the following word, per binary — the only ones for which the
#: subcommand scan may skip ahead. Everything else is treated as valueless.
#:
#: Enumerated rather than guessed. The previous rule assumed any option without `=` took a
#: value, which let a VALUELESS option swallow the subcommand: `git --no-pager push` was
#: ALLOWED while bare `git push` was REFUSED. Getting this list wrong in the other direction
#: is safe for a denylist — an unlisted value-taking option means its value is read as a
#: subcommand, which can only over-refuse.
#:
#: Only GLOBAL (pre-subcommand) options matter here; per-subcommand flags appear after the
#: verb the denylist has already matched.
_VALUE_TAKING_OPTIONS: dict[str, frozenset[str]] = {
    # `git -c k=v`, `-C <path>`, `--git-dir=<p>`, `--work-tree=<p>`, `--namespace=<n>`,
    # `--config-env=<name>=<var>`, `--super-prefix=<p>`.
    #
    # `--exec-path` is deliberately ABSENT: its value is OPTIONAL (`--exec-path[=<path>]`), so
    # listing it here made `git --exec-path push` swallow `push` — the same class of bug this
    # table fixes, just inverted. Measured while writing the test matrix. An option whose value
    # is optional must be treated as valueless; the `=<value>` form still parses correctly
    # because `--opt=value` never consumes the next word.
    "git": frozenset(
        {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
    ),
    # `gh --repo o/r`, `gh --hostname h`.
    "gh": frozenset({"-R", "--repo", "--hostname"}),
    "glab": frozenset({"-R", "--repo", "--hostname"}),
}

_FORBIDDEN_SUBCOMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "gh": (
        ("pr", "merge"),
        ("pr", "ready"),
        ("pr", "close"),
        # PUBLISHING verbs. A watcher's whole job is to READ review comments and PR
        # bodies — attacker-controlled text — so any verb that writes back to GitHub
        # converts "the agent read a malicious comment" into "the agent posted
        # attacker-directed content under the operator's identity". `pr comment` was
        # the one review found; the rest are the same capability under other names, and
        # naming only the reported instance is how this denylist got evaded three times
        # already. The app's own draft PR comes from `pr_recipe.py`, which builds its
        # argv directly and never passes through here, so nothing legitimate is lost.
        ("pr", "comment"),
        ("pr", "review"),
        ("pr", "edit"),
        ("pr", "create"),
        ("issue", "comment"),
        ("issue", "create"),
        ("issue", "edit"),
        ("issue", "close"),
        ("release",),
        ("auth",),
        ("api",),  # can PATCH/POST anything the token reaches
        ("secret",),
        ("workflow", "run"),
    ),
    "git": (
        ("push",),
        ("remote", "set-url"),
    ),
}

#: Binaries a loop/watcher agent must never invoke at all — network fetch and remote shell.
#: No subcommand analysis needed: the binary itself is the capability.
_FORBIDDEN_BINARIES = ("curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "telnet")


def _shell_words(command: str) -> list[str]:
    """Tokenize a command for inspection, tolerating quoting.

    ``shlex`` raises on unbalanced quotes; an unparseable command is treated as a single
    opaque word so the caller falls back to refusing anything suspicious rather than
    accidentally passing it.
    """
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


#: Binaries that RUN ANOTHER COMMAND given as their arguments. Checking only ``words[0]``
#: made every one of these a bypass: measured, ``sudo git push``, ``env git push``,
#: ``timeout 5 git push``, ``nohup git push`` and ``xargs git push`` were all ALLOWED while
#: the bare ``git push`` was refused. The wrapper is stripped and the real command behind it
#: is checked instead.
#:
#: NOT a "forbid these binaries" list — `env` and `timeout` are legitimate on their own.
#: The value is the count of leading NON-OPTION words to drop after the binary (0 for the
#: common case where the very next word is the command).
_COMMAND_WRAPPERS: dict[str, int] = {
    "sudo": 0,
    "doas": 0,
    "env": 0,
    "nohup": 0,
    "setsid": 0,
    "stdbuf": 0,
    "nice": 0,
    "ionice": 0,
    "time": 0,
    "timeout": 1,  # `timeout <duration> <cmd>` — the duration is not the command
    "xargs": 0,
    "watch": 0,
    "script": 0,
    # SHELL BUILTIN wrappers. These are not on PATH, so they only appear inside a shell —
    # but a nested `sh -c "..."` argument is re-analyzed from the top by this same table, so
    # omitting them left a hole. Measured before adding them: bare `git push` was REFUSED
    # while `command git push` and `exec git push` were both ALLOWED. `command` was raised by
    # the GPT review of this branch; `exec` and `builtin` are the same class and were found
    # by testing the neighbours rather than waiting for the next review round.
    "command": 0,
    "exec": 0,
    "builtin": 0,
}

#: Wrapper LONG options that consume the FOLLOWING word as a separate value. Short options
#: are already handled by the general "a short option without `=` takes a value" rule, but a
#: long option was assumed valueless — so ``env --unset FOO curl …`` left ``FOO`` as the
#: apparent command and the real ``curl`` behind it sailed through unexamined (measured). The
#: value is the count of following words the option consumes. ``env``'s value-taking long
#: options are the concrete case; kept as an explicit set rather than "every ``--x`` takes a
#: value" because most wrapper long options are flags (``env --ignore-environment``,
#: ``timeout --preserve-status``) and over-consuming there would drop the real command from
#: view and UNDER-refuse — the wrong direction for a denylist. A ``--opt=value`` form carries
#: its value inline and is not listed. Raised by the GPT review.
_WRAPPER_VALUE_TAKING_LONG_OPTIONS: dict[str, int] = {
    "--unset": 1,  # `env --unset NAME cmd` — NAME is a variable name, not the command
    "--chdir": 1,  # `env --chdir=DIR` is inline, but `env --chdir DIR cmd` is separate
    "--block-signal": 1,  # `env --block-signal=SIG` / separate form
    "--default-signal": 1,
    "--ignore-signal": 1,
}

#: Shells whose ``-c`` argument is a NESTED SCRIPT. `sh -c "git push"` needs the argument
#: re-analyzed from the top (separators, wrappers and all), not just its first word —
#: otherwise `sh -c "sudo git push"` walks straight back out through the same hole.
_SHELL_BINARIES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"})

#: How deep the unwrapping recurses. `sh -c "sh -c \"…\""` is nesting, not a new trick, so
#: it must be followed — but a bounded number of times, so a crafted value cannot spin.
_MAX_UNWRAP_DEPTH = 6


def shell_command_refusal(command: object) -> str:
    """A reason string when ``command`` must be refused, else ``""``.

    TOKENIZED, not substring-matched. Global options are skipped so the SUBCOMMAND is what
    gets checked: ``gh --repo o/r pr ready`` is refused exactly like ``gh pr ready``. Also
    splits on shell separators, so a forbidden verb cannot hide behind ``&&``/``;``/``|``.

    Errs toward refusal — for a denylist that is the safe direction — but the read-only
    diagnostics the watcher's own prompt asks for (``gh pr checks``,
    ``gh pr view --comments``, ``gh run view --log-failed``) are pinned by test.
    """
    return _refusal(str(command or ""), depth=0)


def _refusal(text: str, *, depth: int) -> str:
    """``shell_command_refusal`` proper, with the unwrap recursion depth threaded."""
    if not text.strip():
        return ""
    if depth > _MAX_UNWRAP_DEPTH:
        # Deeper than any legitimate command nests. Refuse rather than give up: this is a
        # denylist, so exhausting the budget must not become a way through it.
        return "command nests shells too deeply to analyze"

    # Each shell-separated segment is its own command; check them all.
    normalized = text.replace("&&", "\n").replace("||", "\n").replace(";", "\n")
    normalized = normalized.replace("|", "\n").replace("$(", "\n").replace("`", "\n")
    # A BARE `&` backgrounds its left side and starts a new command, exactly like `;` —
    # but is NOT caught by the `&&` replace above (that consumed the doubled form). Without
    # this, `true & gh pr comment --body X` tokenizes to `binary='true'` (harmless) and the
    # `gh pr comment`/`curl` behind the `&` slips past the denylist the watcher's
    # outsider-writable prompt exists to enforce. Ordered AFTER `&&` so only the single-`&`
    # separator remains to split on. Same lesson as the wrapper-option fix: a parser that
    # inspects fixed positions is evaded by adding a position. Raised by the Opus review.
    normalized = normalized.replace("&", "\n")
    # Split on the subshell / command-substitution PARENTHESES too. `$(` and backtick above
    # open a substitution, but the CLOSING `)` stayed attached to the last inner token, so
    # `echo $(gh pr ready)` tokenized the verb as `ready)` (≠ `ready`) and slipped past the
    # `("pr","ready")` denylist; a bare subshell `(gh pr ready)` has the same shape. A `(` or
    # `)` is never part of a legitimate command word here, so splitting on both isolates the
    # inner command with no trailing bracket — over-checking at worst, the safe direction for a
    # denylist. Raised by the GPT review.
    normalized = normalized.replace("(", "\n").replace(")", "\n")
    for segment in normalized.splitlines():
        words = _shell_words(segment)
        # Drop leading VAR=value assignments (`GH_TOKEN=x gh api …`).
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]
        if not words:
            continue
        binary = words[0].rsplit("/", 1)[-1].lower()

        # A shell's `-c` argument is a nested script: re-analyze it from the top so
        # separators and further wrappers inside it are seen too.
        if binary in _SHELL_BINARIES:
            for i, word in enumerate(words[1:], start=1):
                # Any bundled short-option group containing `c` (`-c`, `-lc`, `-euxc`).
                if word.startswith("-") and not word.startswith("--") and "c" in word:
                    inner = " ".join(words[i + 1 :])
                    reason = _refusal(inner, depth=depth + 1)
                    if reason:
                        return reason
                    break
            # Fall through: a shell invoked WITHOUT -c still gets its own args checked below.

        # A wrapper runs the command that follows it, so check that command instead.
        if binary in _COMMAND_WRAPPERS:
            wrapped = list(words[1:])
            # Drop the wrapper's own options and any fixed positional it consumes
            # (`timeout <duration> cmd`), plus VAR=value pairs after `env`.
            #
            # An option's VALUE goes too when it is a separate word: `nice -n 5 git push`
            # otherwise leaves `5` as the apparent command and the push sails through
            # (measured). Assume a value follows any short option without `=`, which
            # over-skips at worst — and over-skipping can only make this refuse more,
            # never less, which is the safe direction for a denylist.
            while wrapped and wrapped[0].startswith("-"):
                opt = wrapped[0]
                # A short option without `=` is assumed to take a value (`nice -n 5`); a long
                # option takes one only if it is a KNOWN value-taking wrapper option
                # (`env --unset FOO`). Long options were previously all treated as valueless,
                # which let `env --unset FOO curl …` leave `FOO` as the command and pass the
                # `curl` behind it — the bypass this branch now closes. An inline `--opt=value`
                # carries its own value, so it consumes nothing extra.
                if "=" in opt:
                    takes_value = 0
                elif opt.startswith("--"):
                    takes_value = _WRAPPER_VALUE_TAKING_LONG_OPTIONS.get(opt, 0)
                else:
                    takes_value = 1
                wrapped = wrapped[1:]
                for _ in range(takes_value):
                    if wrapped and not wrapped[0].startswith("-"):
                        # Only consume it if it is not itself a command we know — otherwise a
                        # crafted `env --unset curl git push` would eat the very binary we must
                        # inspect. A recognized command is left in place to be checked.
                        nxt = wrapped[0].rsplit("/", 1)[-1].lower()
                        if nxt in _FORBIDDEN_SUBCOMMANDS or nxt in _FORBIDDEN_BINARIES:
                            break
                        wrapped = wrapped[1:]
            for _ in range(_COMMAND_WRAPPERS[binary]):
                if wrapped:
                    wrapped = wrapped[1:]
            while wrapped and "=" in wrapped[0] and not wrapped[0].startswith("-"):
                wrapped = wrapped[1:]
            if wrapped:
                reason = _refusal(" ".join(wrapped), depth=depth + 1)
                if reason:
                    return reason
            continue
        if binary in _FORBIDDEN_BINARIES:
            return f"{binary!r} cannot be run here"
        forbidden = _FORBIDDEN_SUBCOMMANDS.get(binary)
        if not forbidden:
            continue
        # Skip global options (and their values) to reach the real subcommand path.
        rest: list[str] = []
        skip_next = False
        value_taking = _VALUE_TAKING_OPTIONS.get(binary, frozenset())
        for word in words[1:]:
            if skip_next:
                skip_next = False
                continue
            if word.startswith("-"):
                # Only skip the NEXT word for options actually known to take a value. The
                # previous rule — "assume a value follows any option without `=`" — claimed it
                # "cannot under-skip", and that was exactly backwards: `git --no-pager push`
                # consumed `push` as `--no-pager`'s value, so the denylist matched
                # `['origin','main']` and found nothing. Measured: bare `git push` REFUSED
                # while `git --no-pager push`, `--paginate`, `--bare`, `--literal-pathspecs`
                # and `--no-replace-objects` were all ALLOWED. An UNKNOWN option is treated as
                # valueless, which over-matches at worst (a stray value could be read as a
                # subcommand and refuse a benign command) — the safe direction for a denylist.
                # `--opt=value` never consumes the next word. Raised by the GPT review.
                skip_next = "=" not in word and word.split("=", 1)[0] in value_taking
                continue
            rest.append(word.lower())
        for path in forbidden:
            if tuple(rest[: len(path)]) == path:
                return f"{binary} {' '.join(path)} cannot mutate state here"
    return ""


def _requested_command(ev: object) -> str:
    """The shell command a permission request is asking to run, if any."""
    raw = getattr(ev, "raw_tool_params", None)
    if isinstance(raw, dict):
        for key in ("command", "cmd"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _governance_denial(ev: object, *, session_key: str, agent: str) -> str:
    """A deny reason from the PLATFORM's governance chokepoint, or "" to allow.

    The unattended runner had its OWN approval gate (the allowlist + shell denylist
    below) and never consulted ``hooks.on_tool_call`` — so the enterprise governance
    PROFILE ceiling, the builtin denied-command rules, and the ``~/.aws``/``~/.ssh``
    sensitive-path blocks were all silently INERT on this path. That matters because this
    agent is UNATTENDED and its prompt embeds outsider-writable PR-comment text, so an
    injected instruction could drive an auto-approved call the central gate would deny.
    Routing every request through the same ``HookManager`` the dashboard/Slack paths use
    closes that gap; the app-local allowlist/denylist stays as an ADDITIONAL restriction
    on top (defense in depth). Raised by the Arbiter's long-term review of this branch.

    Fail-CLOSED on an unavailable/broken hook layer: this gate catches cases the app-local
    allowlist/denylist does NOT (sensitive-path reads of ~/.aws/~/.ssh, the enterprise
    governance ceiling), so silently skipping it on a hook error would drop those
    protections for an UNATTENDED agent. A hook-layer failure therefore DENIES with a
    reason; the operator fixes the hooks config and re-runs. Raised by the GPT review of
    this branch (the earlier fail-open let a broken hook authorize the tool).
    """
    try:
        cfg = KiroCrewConfig.load()
        # `hooks_config_from_config_dict`, NOT `HooksConfig.from_dict`: the latter reads only
        # the config.json `hooks` section, while the operator's denied-command state lives in
        # the keystone `denied_commands.json` — "the sole source, so an agent that edits
        # config.json cannot affect the deny ceiling". Measured with an operator rule in the
        # keystone file: `from_dict` yielded [] where the helper yielded ['^curl\s'], so a
        # custom denied command was silently unenforced for the UNATTENDED agent — the one
        # caller that most needs it. Raised by the GPT review.
        manager = HookManager(hooks_config_from_config_dict(getattr(cfg, "hooks", {}) or {}))
        tool_kind = getattr(ev, "tool_kind", "") or getattr(ev, "tool_purpose", "")
        command = _requested_command(ev)
        result = manager.on_tool_call(
            (getattr(ev, "title", "") or tool_kind or "").strip(),
            session_key=session_key,
            agent=agent,
            app="auto-improvement",
            tool_kind=tool_kind,
            raw_params=getattr(ev, "raw_tool_params", None),
            command=command or None,
            # From the EVENT, not derived from the command. `HookManager.on_tool_call` denies
            # when `is_shell and not command` — a shell tool whose command could not be
            # recovered must not be judged on its LLM-authored title (`acp/types.py` states
            # that contract). Computing it as `bool(command)` inverted exactly that case: no
            # command meant is_shell=False, so the request was treated as a non-shell tool and
            # skipped the branch written for it.
            is_shell=bool(getattr(ev, "is_shell", False)) or bool(command),
        )
        if getattr(result, "action", "") == TOOL_DENY:
            return (getattr(result, "reason", "") or "denied by governance policy").strip()
    except Exception as exc:  # noqa: BLE001 - a broken gate must DENY, not authorize
        logger.warning("governance hook unavailable; denying (fail-closed)", exc_info=True)
        return f"governance hook unavailable: {exc}"
    return ""


def _tool_permitted(tool: object, allowed: list[str] | None) -> bool:
    """Whether ``tool`` is inside the caller's allowlist.

    ``None`` means "the caller did not restrict tools" and everything is permitted — that
    is the pre-existing behavior for callers that never passed a list, and narrowing it
    here would silently break them.

    An EMPTY LIST is not the same thing: ``agent_discovery`` passes ``allowed_tools=[]``
    to mean "no tools at all — you must answer from what you already have". Treating empty
    as unrestricted would invert that call site into granting everything, which is the
    opposite of what it asks for. Empty therefore denies every tool.

    Matching is case-insensitive and tolerant of the provider's naming: the event carries a
    ``tool_kind`` like ``"bash"`` or ``"execute_bash"`` while callers write ``"Bash"``, so a
    prefix/substring match in either direction counts. Deliberately generous on MATCHING
    and strict on MEMBERSHIP — a near-miss name must not become an accidental denial of a
    legitimate tool, but an absent name is still refused.
    """
    if allowed is None:
        return True  # unset: caller imposed no restriction
    names = [str(a).strip().lower() for a in allowed if str(a).strip()]
    if not names:
        return False  # explicitly empty: deny every tool
    got = str(tool or "").strip().lower()
    if not got:
        # An unnamed request cannot be checked against the allowlist. Refuse: an
        # unidentifiable tool is exactly what a crafted request would look like.
        return False
    return any(got == n or got.startswith(n) or n.startswith(got) or n in got for n in names)


def _audit_unattended_agent(*, cwd: str | None, model: str | None, max_turns: int) -> bool:
    """Record the launch of a permissionless agent in the SEL. False = do not launch.

    `--dangerously-skip-permissions` makes the whole subprocess a single unattended
    approval covering every tool it will use, so this one event is the audit trail for
    all of them. `critical=True` writes it synchronously and re-raises on a filesystem
    failure, which is what lets the caller deny rather than run untraced — the same
    audit-or-deny contract as the session path's per-tool approval.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="auto-improvement-subprocess",
            agent="auto-improvement",
            source="auto_improvement_loop",
            tool_name="claude-cli",
            tool_kind="agent_subprocess",
            outcome="auto_approved",
            resources=str(cwd or ""),
            metadata={
                "unattended": True,
                "skip_permissions": True,
                "model": str(model or ""),
                "max_turns": int(max_turns),
                "containment": "worktree+push_disabled_clone",
            },
            critical=True,
        )
        return True
    except Exception:  # noqa: BLE001 - audit failure must deny, not launch
        logger.warning("SEL audit failed for the unattended agent launch — refusing to launch")
        return False


class AgentRunner:
    """A cost-accumulating ``claude -p`` invoker. One instance per run so the driver's
    cost meter can read cumulative spend (``--max-cost`` ceiling). Thread-safe cost add
    so a future parallel fan-out stays correct."""

    def __init__(
        self,
        *,
        model: str | None = None,
        default_timeout_s: float = 1800.0,
        stop_check=None,
        on_activity=None,
    ) -> None:
        self.model = model
        self.default_timeout_s = default_timeout_s
        self._cost_lock = threading.Lock()
        self._total_cost_usd = 0.0
        # Optional callable() -> bool polled while waiting on the agent subprocess so a
        # clean-stop request terminates the in-flight ``claude -p`` instead of letting it
        # run to ``--max-turns`` (each turn is real cost). Default: no stop check; the
        # subprocess runs to its own timeout. The driver wires this to its ``_stop``
        # flag so a UI Stop click actually aborts pending agent calls.
        self._stop_check = stop_check
        # Optional callable(dict) -> None invoked for each streamed agent turn (assistant
        # text, a tool_use, a tool_result). This is what turns the dead "proposing
        # fixes…" placeholder into a live feed of what the agent is actually doing — the
        # backend wires it to a ring buffer the UI polls. When set, the runner uses
        # ``--output-format stream-json`` and parses stdout line-by-line; when None it
        # keeps the cheaper one-shot ``json`` path.
        self._on_activity = on_activity if callable(on_activity) else None

    def total_cost_usd(self) -> float:
        with self._cost_lock:
            return self._total_cost_usd

    @staticmethod
    def _terminate_group(popen: subprocess.Popen) -> None:
        """Kill the agent subprocess AND any children it spawned. ``claude -p`` forks an
        editor + helpers; a plain ``popen.kill()`` only signals the parent and leaves the
        children running (the symptom we hit: agents kept costing money after Stop).
        Using ``start_new_session=True`` made the child a process-group leader, so a
        SIGTERM to the negative pid signals every descendant cleanly.

        Uses platform_compat.kill_process_tree for cross-platform support (Windows
        lacks os.killpg/os.getpgid)."""
        try:
            kill_process_tree(popen.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError, ValueError):
            try:
                popen.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            popen.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                kill_process_tree(popen.pid, SIGKILL)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def available() -> bool:
        """True iff the headless agent binary is on PATH — lets the backend decide
        whether to wire a real runner or stay offline (no fabricated fixes)."""
        return shutil.which(CLAUDE_BIN) is not None

    def _spawn_sandboxed_agent(self, cmd: list[str], cwd: str | None):
        """Launch the agent CLI inside the OS sandbox. Returns ``(popen, cleanup_path)``.

        A separate, uniquely-named method rather than inline in ``run``: the spawn audit
        keys findings by ``file::function``, and this module has TWO ``run`` methods
        (``AgentRunner`` and ``SessionAgentRunner``). Inline, the sandboxed spawn was
        attributed to the wrong one and read as unrouted — a real reporting gap, since the
        audit is what guarantees every agent-influenced spawn is sandboxed.

        This agent runs with ``--dangerously-skip-permissions``, so its Bash tool is
        unattended: the worktree stays VISIBLE (there would be nothing to edit otherwise)
        while the operator's credential directories are hidden and the environment is
        scrubbed. Review of this branch asked for the fallback to be DELETED; sandboxing
        addresses the same concern — a malicious repository prompt can no longer reach
        credentials outside the worktree — without removing the only path that works when
        no in-process provider is configured.
        """
        root = str(Path(cwd).resolve()) if cwd else None
        # STRICT mode, not the default "standard": "standard" deliberately leaves ~/.aws
        # visible so a test suite can use the AWS CLI, but the fix-AUTHORING agent has no
        # such need and runs `--dangerously-skip-permissions`, so it should see no
        # credential dir at all. Verified on this host that "standard" left ~/.aws
        # readable to the child; "strict" hides it (+ .gnupg, gh, gcloud, kube, …).
        # strip_python_env so the agent's own tooling can't inherit Kiro Crew's interpreter
        # paths and import from this process's tree.
        sandboxed, scrubbed_env, cleanup = sandboxed_spawn_argv(
            list(cmd),
            mode="strict",
            strip_python_env=True,
            extra_visible_dirs=((root,) if root else ()),
        )
        # Strip credential-shaped names the SHARED scrub list misses. Measured on this
        # host: `GITHUB_TOKEN` survives `scrub_env` (its list covers AWS_SECRET/SLACK_*/
        # TELEGRAM_* but not GITHUB_*), so an exported token reached this agent — which runs
        # `--dangerously-skip-permissions`, meaning a repository instruction could use it.
        # Review raised this repeatedly; the gate already defends this way
        # (`profiles/github_repo/profile.py:strip_credential_env`) and the agent spawn is
        # the other place untrusted content executes, so it gets the same treatment.
        scrubbed_env = strip_credential_env(scrubbed_env)
        popen = subprocess.Popen(
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- argv LIST, no shell=True, so there is no shell to inject into and shlex.quote() does not apply. Every element is either a literal or a value the app itself computed (prompt text, model name, worktree paths); an argv element can only ever be one argument to CLAUDE_BIN. The marker sits on the tainted ARGUMENT line because that is where semgrep anchors this rule.  # noqa: E501
            sandboxed,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered so streamed lines arrive promptly
            env=scrubbed_env,
            preexec_fn=resource_limit_preexec(),
            # Make the child its own process group so we can kill it (and any children it
            # spawned) cleanly on stop without taking the parent down.
            start_new_session=True,
        )
        return popen, cleanup

    def run(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        append_system: str | None = None,
        max_turns: int = 40,
        timeout_s: float | None = None,
        add_dirs: list[str] | None = None,
    ) -> AgentResult:
        """Invoke ``claude -p``. Returns an :class:`AgentResult`; never raises on a
        model-side failure (mirrors the original ``run_claude`` contract)."""
        timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
        streaming = self._on_activity is not None
        cmd = (
            [
                CLAUDE_BIN,
                "-p",
                prompt,
                # stream-json emits one JSON object per line as the agent works (assistant
                # turns, tool_use, tool_result), so the UI can show a live feed instead of a
                # frozen "proposing fixes…". --verbose is required for stream-json with -p.
                # Without an activity sink we keep the cheaper one-shot json envelope.
                "--output-format",
                "stream-json",
                "--verbose",
            ]
            if streaming
            else [
                CLAUDE_BIN,
                "-p",
                prompt,
                "--output-format",
                "json",
            ]
        )
        cmd += [
            "--max-turns",
            str(max_turns),
            # Unattended loop; worktrees are throwaway + isolated and the clone is
            # push-disabled, so bypass interactive prompts. Blast radius = the worktree.
            "--dangerously-skip-permissions",
            # NO MCP servers. The fix-authoring agent only needs file/shell tools
            # (Bash/Read/Edit/Write/Grep/Glob); without this it inherits the user's full
            # ~/.claude.json MCP config (15+ servers — aim, badger, node servers, …) and
            # spends MINUTES booting that stack per candidate (the "stuck on write-a-fix"
            # symptom). An empty strict config boots with zero servers → fast startup.
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ]
        # `if allowed_tools:` was WRONG for an empty list: `[]` means "no tools at all"
        # (agent_discovery's forced-answer pass), but falsy-empty omitted the flag entirely
        # and this path also passes `--dangerously-skip-permissions` — so the supposedly
        # tool-free agent got everything. Same inversion I had just fixed in the SESSION
        # path's `_tool_permitted`; review caught that the subprocess path has its own copy
        # of the decision. `None` still means "no restriction imposed".
        if allowed_tools is not None:
            if allowed_tools:
                cmd += ["--allowed-tools", *allowed_tools]
            else:
                # Deny every tool. `--allowed-tools` with an empty value is not a reliable
                # way to say that, so name a tool that does not exist: nothing can match it.
                cmd += ["--allowed-tools", "__none__"]
        if append_system:
            cmd += ["--append-system-prompt", append_system]
        if self.model:
            cmd += ["--model", self.model]
        for d in add_dirs or []:
            cmd += ["--add-dir", d]

        t0 = time.monotonic()
        # AUDIT-OR-DENY, matching `SessionAgentRunner._approve`. This fallback passes
        # `--dangerously-skip-permissions`, so the whole subprocess is one unattended
        # blanket approval and the SEL entry is the only record it happened. Without it
        # the two runner paths disagree: the session path refuses an unauditable tool
        # while this one ran a permissionless agent silently. Raised by review of this
        # branch, which read that asymmetry as a sandbox bypass.
        #
        # The containment argument for the spawn itself is unchanged and recorded in
        # test_spawn_audit (argv[0] is a module constant, the agent edits only files
        # inside a throwaway worktree of a push-disabled clone, and executing those files
        # is routed separately through the sandbox). What was missing was the trail.
        if not _audit_unattended_agent(cwd=cwd, model=self.model, max_turns=max_turns):
            return AgentResult(
                ok=False,
                error="refusing to launch an unattended agent that cannot be audited",
                duration_s=time.monotonic() - t0,
            )
        # Route the fallback agent through the OS sandbox, the same chokepoint the gate's
        # test execution uses (`profiles/github_repo/profile.py::_run`). This agent runs
        # with `--dangerously-skip-permissions`, so its Bash tool is unattended: the
        # worktree must stay VISIBLE (there would be nothing to edit otherwise) while the
        # operator's credential directories are hidden and the environment is scrubbed.
        #
        # Raised by review of this branch, which asked for the fallback to be DELETED.
        # Sandboxing it addresses the same concern — a malicious repository prompt can no
        # longer reach credentials outside the worktree — without removing the only path
        # that works when no in-process provider is configured. Deleting it would turn
        # "no provider" from "degraded but functional" into "silently does nothing".
        try:
            popen, cleanup = self._spawn_sandboxed_agent(cmd, cwd)
        except FileNotFoundError:
            _unlink_quietly(cleanup)
            return AgentResult(
                ok=False,
                error=f"agent binary not found: {CLAUDE_BIN}",
                duration_s=time.monotonic() - t0,
            )
        except Exception as e:  # noqa: BLE001
            _unlink_quietly(cleanup)
            return AgentResult(
                ok=False, error=f"{type(e).__name__}: {e}", duration_s=time.monotonic() - t0
            )

        # `sandboxed_spawn_argv` may write a launcher script the caller owns; its contract
        # is that the caller unlinks it once the child no longer needs it. The streaming
        # path has many exits, so this is a finally rather than a call per return.
        try:
            if streaming:
                return self._run_streaming(popen, t0, timeout_s, cwd=cwd)
        finally:
            _unlink_quietly(cleanup)

        # ── one-shot json path (no activity sink) ──────────────────────────────
        deadline = t0 + timeout_s
        try:
            while True:
                try:
                    stdout, stderr = popen.communicate(timeout=2.0)
                    break  # process exited; stdout/stderr captured
                except subprocess.TimeoutExpired:
                    pass  # not done yet
                if self._stop_check is not None and self._stop_check():
                    self._terminate_group(popen)
                    return AgentResult(
                        ok=False, error="stopped by request", duration_s=time.monotonic() - t0
                    )
                if time.monotonic() > deadline:
                    self._terminate_group(popen)
                    return AgentResult(
                        ok=False,
                        error=f"timeout after {timeout_s}s",
                        duration_s=time.monotonic() - t0,
                    )
        except Exception as e:  # noqa: BLE001
            self._terminate_group(popen)
            return AgentResult(
                ok=False, error=f"{type(e).__name__}: {e}", duration_s=time.monotonic() - t0
            )

        proc = type("P", (), {"returncode": popen.returncode, "stdout": stdout, "stderr": stderr})()

        dur = time.monotonic() - t0
        if proc.returncode != 0:
            return AgentResult(
                ok=False, error=f"exit {proc.returncode}: {proc.stderr[-400:]}", duration_s=dur
            )
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return AgentResult(
                ok=False,
                error="unparseable claude json envelope",
                duration_s=dur,
                raw={"stdout": proc.stdout[-400:]},
            )

        result_text = envelope.get("result", "")
        cost = float(envelope.get("total_cost_usd", 0.0) or 0.0)
        with self._cost_lock:
            self._total_cost_usd += cost
        if envelope.get("is_error"):
            return AgentResult(
                ok=False,
                text=str(result_text),
                error="claude reported is_error",
                cost_usd=cost,
                duration_s=dur,
                raw=envelope,
            )
        return AgentResult(
            ok=True,
            text=result_text if isinstance(result_text, str) else json.dumps(result_text),
            cost_usd=cost,
            duration_s=dur,
            raw=envelope,
        )

    def _emit_activity(self, ev: dict) -> None:
        try:
            self._on_activity(ev)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — a sink error must never break the run
            pass

    def _run_streaming(
        self, popen, t0: float, timeout_s: float, *, cwd: str | None = None
    ) -> AgentResult:
        """Read ``stream-json`` stdout line-by-line, emit one activity event per turn,
        and assemble the final result. Honors stop + timeout the same as the one-shot
        path. The final ``type:"result"`` line carries the cost + result text."""
        result_text = ""
        cost = 0.0
        is_error = False
        deadline = t0 + timeout_s
        # Drain stderr CONCURRENTLY on a daemon thread into a bounded tail. Reading only
        # stdout below would let a chatty child fill the 64KB stderr pipe and BLOCK —
        # a silent deadlock that only the timeout SIGTERM would break.
        stderr_chunks: deque = deque(maxlen=16)

        def _drain_stderr() -> None:
            try:
                if popen.stderr is None:
                    return
                for chunk in popen.stderr:
                    stderr_chunks.append(chunk)
            except Exception:  # noqa: BLE001 — draining must never raise into the run
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()
        try:
            for line in popen.stdout:  # iterates as lines arrive (line-buffered)
                if self._stop_check is not None and self._stop_check():
                    self._terminate_group(popen)
                    return AgentResult(
                        ok=False, error="stopped by request", duration_s=time.monotonic() - t0
                    )
                if time.monotonic() > deadline:
                    self._terminate_group(popen)
                    return AgentResult(
                        ok=False,
                        error=f"timeout after {timeout_s}s",
                        duration_s=time.monotonic() - t0,
                    )
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                act = _summarize_stream_event(obj)
                if act:
                    self._emit_activity(act)
                    # AUDIT each tool the agent actually used, not just the one blanket
                    # launch event. The launch is logged `critical=True` before spawn, but
                    # that records "an unattended agent started" — it says nothing about
                    # WHICH tools it then ran, so a forensic query could not answer "did
                    # this run touch a shell?". The stream already carries `tool_use`
                    # blocks (that is what drives the UI feed), so the information was
                    # present and simply not persisted. This brings the subprocess
                    # fallback to the same per-tool visibility the session path gets from
                    # its approval hook. Raised repeatedly by the GPT review of this branch.
                    if act.get("kind") == "tool":
                        _audit_fallback_tool(
                            tool=str(act.get("tool") or "tool"),
                            detail=str(act.get("detail") or ""),
                            cwd=cwd,
                        )
                if obj.get("type") == "result":
                    result_text = obj.get("result", "") or ""
                    cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
                    is_error = bool(obj.get("is_error"))
        except Exception as e:  # noqa: BLE001
            self._terminate_group(popen)
            return AgentResult(
                ok=False, error=f"{type(e).__name__}: {e}", duration_s=time.monotonic() - t0
            )
        try:
            popen.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self._terminate_group(popen)
        stderr_thread.join(timeout=2.0)  # let the drain finish; tail comes from its buffer
        stderr_tail = ("".join(stderr_chunks))[-400:]

        dur = time.monotonic() - t0
        with self._cost_lock:
            self._total_cost_usd += cost
        if popen.returncode not in (0, None):
            return AgentResult(
                ok=False,
                error=f"exit {popen.returncode}: {stderr_tail}",
                cost_usd=cost,
                duration_s=dur,
            )
        if is_error:
            return AgentResult(
                ok=False,
                text=str(result_text),
                error="claude reported is_error",
                cost_usd=cost,
                duration_s=dur,
            )
        return AgentResult(
            ok=True,
            text=result_text if isinstance(result_text, str) else "",
            cost_usd=cost,
            duration_s=dur,
        )


def _audit_fallback_tool(*, tool: str, detail: str, cwd: str | None) -> None:
    """Record ONE tool the subprocess fallback's agent used.

    NOT ``critical=True``, unlike the launch event: by the time this fires the tool has
    already run inside the sandbox, so there is nothing left to deny and raising here would
    only turn an audit-sink problem into a failed run. The audit-or-DENY decision for this
    path is the launch event plus the pre-spawn governance gate
    (:func:`_governance_denial`) and the shell denylist; this is the audit-or-RECORD half.

    ``detail`` is the same short target hint the UI feed shows (a path, a pattern, a
    command) — agent-influenced text, so it is redacted before it reaches a log that is
    signed as-written, and truncated because a tool input can be arbitrarily long.
    """
    try:
        from kiro_crew.security import redact

        safe_detail = redact(detail)[:200]
    except Exception:  # noqa: BLE001 — never let a redactor failure emit raw agent text
        safe_detail = "[redaction unavailable]"
    try:
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="auto-improvement-subprocess",
            agent="auto-improvement",
            source="auto_improvement_loop",
            tool_name=tool,
            tool_kind=tool,
            outcome="invoked",
            resources=str(cwd or ""),
            metadata={"unattended": True, "target": safe_detail},
        )
    except Exception:  # noqa: BLE001 — the tool already ran; a log failure must not fail it
        logger.debug("SEL audit failed for fallback tool %r", tool, exc_info=True)


def _summarize_stream_event(obj: dict) -> dict | None:
    """Turn one ``stream-json`` line into a concise activity event for the UI feed, or
    None to skip. We surface the human-meaningful turns: assistant text (truncated), a
    tool invocation (which tool + a short target), and the final result line. We avoid
    dumping raw tool *output* (can be huge / noisy) — the UI feed wants "what is the
    agent doing", not full payloads."""
    t = obj.get("type")
    # Assistant turn: may carry text and/or tool_use blocks.
    if t == "assistant":
        msg = obj.get("message", {}) or {}
        content = msg.get("content", []) or []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input", {}) or {}
                # a short, safe target hint per common tool
                hint = (
                    inp.get("file_path")
                    or inp.get("path")
                    or inp.get("pattern")
                    or inp.get("command")
                    or inp.get("description")
                    or ""
                )
                if isinstance(hint, str) and len(hint) > 80:
                    hint = hint[:77] + "…"
                return {"kind": "tool", "tool": name, "detail": str(hint)}
            if bt == "text":
                txt = (block.get("text") or "").strip()
                if txt:
                    return {"kind": "text", "detail": txt[:200]}
        return None
    if t == "result":
        return {"kind": "result", "detail": ("done" if not obj.get("is_error") else "error")}
    return None


class SessionAgentRunner:
    """A backend-AGNOSTIC agent runner that drives a Kiro Crew **provider/session** instead
    of shelling out to ``claude -p`` directly (task #23). The provider is whatever the
    Kiro Crew config selects (whichever LLM provider backend the user configured), so ANY
    backend can power the autonomous loop — not just a single CLI on PATH.

    It matches :class:`AgentRunner`'s surface exactly (``run`` → :class:`AgentResult`,
    ``available``, ``total_cost_usd``, ``stop_check``, ``on_activity``), so it is a drop-in
    where the spine constructs a runner. Internally it bridges the spine's SYNC ``run`` to
    the provider's ASYNC ``stream``: it spawns the provider cwd'd into the throwaway
    worktree, streams the task, auto-approves ONLY the allowlisted tools, accumulates the
    assistant text + cost, and honors stop/timeout — then tears the provider down.

    SAFETY / PERMISSIONS: the subagent runs UNATTENDED, so it AUTO-APPROVES every tool/MCP
    the provider requests — blocking on a permission prompt would stall the loop (and there
    is no human to answer). This is safe because the blast radius is already contained: the
    agent works ONLY inside the throwaway, push-disabled worktree, and the spine's edit
    allowlist + RED→GREEN / A-B gate decide what actually ships. It is the session-layer
    equivalent of the subprocess path's ``--dangerously-skip-permissions``."""

    def __init__(
        self,
        *,
        model: str | None = None,
        agent_name: str = "auto-improvement-discovery",
        default_timeout_s: float = 1800.0,
        stop_check=None,
        on_activity=None,
        provider_factory=None,
    ) -> None:
        self.model = model
        # The KIRO AGENT this runner drives. It MUST be an app agent whose tool set
        # is empty (``agents/discovery.json`` ships ``"tools": []``), so the session
        # gets only file/shell tools and NOT the gateway's kirocrew-core MCP tools.
        # Without this the session inherited the default agent's full toolset,
        # including ``spawn_sub_agents`` — and the discovery agent would spawn
        # subagents that orphan (they get an empty parent_session because this
        # runner has no dashboard session), then hang the run polling for them.
        self.agent_name = agent_name
        self.default_timeout_s = default_timeout_s
        self._cost_lock = threading.Lock()
        self._total_cost_usd = 0.0
        self._stop_check = stop_check
        self._on_activity = on_activity if callable(on_activity) else None
        # The Kiro Crew provider factory (``cfg.create_provider_factory()``). Injectable for
        # tests; resolved lazily from config when None so importing this module never loads
        # the whole config/provider stack.
        self._provider_factory = provider_factory

    def total_cost_usd(self) -> float:
        with self._cost_lock:
            return self._total_cost_usd

    @staticmethod
    def available() -> bool:
        """True iff a Kiro Crew provider factory can be built (a backend is configured).
        Lets the backend prefer this runner and fall back to the subprocess ``claude -p``
        runner only when no provider is available."""
        try:

            cfg = KiroCrewConfig.load()
            return cfg.create_provider_factory() is not None
        except Exception:  # noqa: BLE001 — any failure → not available, caller falls back
            return False

    def ensure_agent_registered(self) -> bool:
        """Make ``self.agent_name`` resolvable to kiro-cli by its bare name.

        kiro-cli reads agent definitions from ``~/.kiro/agents/`` and activates one
        via ``session/set_mode`` — an unknown name silently falls back to the
        DEFAULT agent, which carries the full kirocrew-core toolset (including
        ``spawn_sub_agents``). That fallback is exactly what made the discovery
        agent spawn subagents that orphan and hang the run.

        The app bridge namespaces builtin agents (``app--name``) AND does not copy
        a builtin's ``agents/`` into its install dir at all, so neither the bare
        name nor the namespaced one is reliably present. Rather than depend on
        that, write the app's own agent JSON straight to ``~/.kiro/agents/<name>.json``
        under the exact bare name this runner requests. Idempotent, best-effort:
        on failure the run still proceeds (with the default agent), so this
        hardens the tool scoping without becoming a hard dependency.
        """

        try:
            src = Path(__file__).resolve().parent.parent / "agents" / "discovery.json"
            if not src.is_file():
                return False
            data = json.loads(src.read_text(encoding="utf-8"))
            if str(data.get("name") or "") != self.agent_name:
                # Only self-register the agent this runner actually drives.
                return False
            # kiro_agents_dir() honors KIRO_HOME; hard-coding ~/.kiro would write
            # into the real agent home during tests and break agent-home isolation.
            from kiro_crew.config.paths import kiro_agents_dir

            dest_dir = kiro_agents_dir()
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{self.agent_name}.json"
            desired = src.read_bytes()
            # Do NOT clobber a DIFFERENT existing file. `~/.kiro/agents/<name>.json` is the
            # user's own agent directory; overwriting a file they wrote — because it happens
            # to share this app's agent name — destroys their data. Write only when the file
            # is absent or already byte-identical (idempotent re-register). On a real
            # conflict, refuse and warn: the run proceeds with the default agent exactly as
            # it does on any other registration failure, so tool-scoping is best-effort, not
            # a licence to overwrite. Raised by the GPT review of this branch.
            if dest.exists():
                try:
                    if dest.read_bytes() == desired:
                        return True  # already ours — nothing to do
                except OSError:
                    pass
                logger.warning(
                    "not self-registering agent %s: %s already exists with different "
                    "content (leaving the user's file intact; using the default agent)",
                    self.agent_name,
                    dest,
                )
                return False
            shutil.copyfile(src, dest)
            return True
        except Exception:  # noqa: BLE001 — never block a run on agent registration
            logger.warning("could not self-register agent %s", self.agent_name, exc_info=True)
            return False

    def _resolve_factory(self):
        if self._provider_factory is not None:
            return self._provider_factory

        cfg = KiroCrewConfig.load()
        self._provider_factory = cfg.create_provider_factory()
        return self._provider_factory

    def _emit_activity(self, ev: dict) -> None:
        if self._on_activity is None:
            return
        try:
            self._on_activity(ev)
        except Exception:  # noqa: BLE001 — a sink error must never break the run
            pass

    def run(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        allowed_tools: list[str] | None = None,
        append_system: str | None = None,
        max_turns: int = 40,
        timeout_s: float | None = None,
        add_dirs: list[str] | None = None,
    ) -> AgentResult:
        """Drive the configured provider through the agentic tool-loop for ``prompt``,
        cwd'd into ``cwd``. Returns an :class:`AgentResult`; never raises on a provider-side
        failure (mirrors :meth:`AgentRunner.run`)."""
        timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
        t0 = time.monotonic()
        try:
            factory = self._resolve_factory()
        except Exception as e:  # noqa: BLE001
            return AgentResult(
                ok=False,
                error=f"provider factory unavailable: {e}",
                duration_s=time.monotonic() - t0,
            )
        if factory is None:
            return AgentResult(
                ok=False, error="no Kiro Crew provider configured", duration_s=time.monotonic() - t0
            )
        try:
            return asyncio.run(
                self._run_async(
                    prompt,
                    factory=factory,
                    cwd=cwd,
                    allowed_tools=allowed_tools,
                    append_system=append_system,
                    timeout_s=timeout_s,
                    t0=t0,
                    max_turns=max_turns,
                )
            )
        except Exception as e:  # noqa: BLE001 — never raise into the spine loop
            return AgentResult(
                ok=False, error=f"{type(e).__name__}: {e}", duration_s=time.monotonic() - t0
            )

    async def _run_async(
        self,
        prompt,
        *,
        factory,
        cwd,
        append_system,
        timeout_s,
        t0,
        max_turns=0,
        allowed_tools=None,
    ) -> AgentResult:
        # Build a provider for THIS task. The factory's FIRST positional is the
        # session_key (namespaces the provider's work dir); ``agent`` selects the
        # KIRO AGENT — which is what scopes the tool set. Passing the app's
        # tool-restricted agent here is the fix for the subagent-orphan hang: an
        # agent with ``"tools": []`` gets file/shell tools only, so it cannot reach
        # ``spawn_sub_agents``. (The previous call passed the agent NAME as the
        # session_key and the MODEL as ``agent``, so no scoped agent was selected
        # and the session inherited the default agent's full kirocrew-core toolset.)
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
        )

        # Stable across processes (Python's builtin hash() is per-run salted): the
        # session key only needs to be unique per worktree, not secret.
        digest = hashlib.sha256((cwd or self.agent_name).encode()).hexdigest()[:12]
        session_key = f"auto-improvement-{digest}"
        provider = None
        try:
            try:
                provider = factory(session_key, agent=self.agent_name, cwd=cwd)
            except TypeError:
                # Older/other factories may not accept cwd / agent kwargs.
                try:
                    provider = factory(session_key, agent=self.agent_name)
                except TypeError:
                    provider = factory(session_key)
            await provider.start()
            text_parts: list[str] = []
            # Streamed assistant text arrives as MANY tiny provider chunks (often sub-word
            # token slivers like "ismatched-" / "ncation would"). Emitting one activity
            # event per raw chunk floods the feed with unreadable fragments. Instead BUFFER
            # the chunks and flush only COHERENT lines (on a newline / sentence boundary, or
            # when the buffer gets long), so the live feed reads as whole thoughts.
            text_buf = _LineBuffer(
                lambda line: self._emit_activity({"kind": "text", "detail": line})
            )
            cost = 0.0
            tool_calls = 0  # ACP turn counter — enforces max_turns on the session path
            # Per-tool_call_id detail already shown in the feed, so the EVENT_TOOL_CALL_UPDATE
            # refinement only emits an UPGRADED line when it adds a real target (the file
            # path), not a duplicate. Fixes "⚙ read · Read File" (no filename): the initial
            # tool_call has empty rawInput; the path arrives in the refinement.
            announced_tool_detail: dict[str, str] = {}
            deadline = t0 + timeout_s
            full_prompt = (append_system + "\n\n" + prompt) if append_system else prompt
            # HARD WALL-CLOCK WATCHDOG (operator directive 2026-06-15): the old `async for`
            # only checked the deadline BETWEEN events, so a long in-turn await (the agent
            # thinking/reading for minutes inside one stream step) could blow past timeout_s
            # unbounded — the runaway 10-min discovery run. We instead drive the stream as an
            # explicit async iterator and bound EACH event fetch with asyncio.wait_for on the
            # REMAINING budget. A stall is force-cancelled and we return the accumulated text
            # (so any findings already streamed survive). Falls back to the plain async-for if
            # the provider stream isn't a true async iterator.
            stream = provider.stream(full_prompt)
            ait = stream.__aiter__() if hasattr(stream, "__aiter__") else None

            def _finish(*, ok: bool, error: str = "") -> AgentResult:
                # Single exit point: fold the cost ALREADY SPENT into the running total on
                # EVERY path (stop / timeout / max_turns / success), so the driver's live
                # ``--max-cost`` meter (cost_meter=runner.total_cost_usd) sees real spend.
                # The tool calls billed before an early return are real money — omitting
                # them (the old behavior, which only accrued on success) made the ceiling
                # under-count for timeout/max_turns, the EXPECTED common outcomes.
                with self._cost_lock:
                    self._total_cost_usd += cost
                return AgentResult(
                    ok=ok,
                    error=error,
                    text="".join(text_parts),
                    cost_usd=cost,
                    duration_s=time.monotonic() - t0,
                )

            while True:
                if self._stop_check is not None and self._stop_check():
                    return _finish(ok=False, error="stopped by request")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Wall-clock expired between events — return accumulated text (the
                    # discovered=0 fix: never discard a late/partial JSON answer).
                    return _finish(ok=False, error=f"timeout after {timeout_s}s")
                if ait is None:  # non-iterator stream — can't watchdog; degrade gracefully
                    break
                try:
                    ev = await asyncio.wait_for(ait.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    break
                except (asyncio.TimeoutError, TimeoutError):
                    # In-turn stall exceeded the budget — force-cancel and harvest text.
                    return _finish(ok=False, error=f"timeout after {timeout_s}s")
                kind = getattr(ev, "kind", "")
                if getattr(ev, "cost_usd", 0.0):
                    cost = float(ev.cost_usd) or cost
                if kind == EVENT_PERMISSION_REQUEST:
                    # AUTO-APPROVE EVERY tool/MCP the provider asks for — never block on a
                    # permission prompt (the subagent runs unattended; a blocked tool would
                    # stall the whole loop). The blast radius is already contained: the
                    # agent works ONLY inside the throwaway, push-disabled worktree, and the
                    # spine's edit-allowlist + RED→GREEN/A-B gate decide what actually ships.
                    # This is the session-layer equivalent of the old subprocess path's
                    # ``--dangerously-skip-permissions``.
                    #
                    # Approve INLINE (await), exactly as the production task-executor /
                    # channel consumers do (``async for event in client.stream(): await
                    # client.approve_tool(event.request_id)``). approve_tool is just a stdin
                    # write + drain — it does NOT require the stream generator to advance, so
                    # it cannot deadlock. An earlier ``create_task`` variant let the tool run
                    # (the file got written) but the turn never reached EVENT_COMPLETE: the
                    # approval landed out-of-order relative to the read loop, the agent never
                    # saw its tool result, and the run hung to the timeout. Inline await is
                    # the proven pattern and completes the turn.
                    tool = getattr(ev, "tool_kind", "") or getattr(ev, "tool_purpose", "")
                    rid = getattr(ev, "request_id", "")
                    # ENFORCE the caller's allowlist. `allowed_tools` was accepted by `run`
                    # and never forwarded here, so every request was granted whatever it
                    # asked for. That matters most for a WATCHER: it reads PR comments
                    # through `gh`, so a malicious comment could ask for a shell and get it,
                    # against an authenticated GitHub session. Raised by review of this
                    # branch. Deny-by-default only when a caller supplied a list — an
                    # absent list keeps the previous behavior for callers that never set one.
                    # FIRST gate: the platform governance chokepoint (`hooks.on_tool_call`)
                    # — the enterprise ceiling, builtin denied rules, and sensitive-path
                    # (~/.aws/~/.ssh) blocks that the dashboard/Slack paths honor. This
                    # unattended runner previously skipped it and relied only on the
                    # app-local checks below. Raised by the Arbiter's review of this branch.
                    gov = _governance_denial(ev, session_key=session_key, agent=self.agent_name)
                    if gov:
                        logger.warning("refusing tool %r — governance: %s", tool, gov)
                        await self._reject(provider, rid, tool=tool, session_key=session_key)
                        self._emit_activity(
                            {"kind": "tool", "tool": tool or "tool", "detail": f"refused: {gov}"}
                        )
                        continue
                    refusal = shell_command_refusal(_requested_command(ev))
                    if refusal:
                        logger.warning("refusing tool %r — %s", tool, refusal)
                        await self._reject(provider, rid, tool=tool, session_key=session_key)
                        self._emit_activity(
                            {
                                "kind": "tool",
                                "tool": tool or "tool",
                                "detail": f"refused: {refusal}",
                            }
                        )
                        continue
                    if not _tool_permitted(tool, allowed_tools):
                        logger.warning("refusing tool %r — not in the caller's allowed_tools", tool)
                        await self._reject(provider, rid, tool=tool, session_key=session_key)
                        self._emit_activity(
                            {"kind": "tool", "tool": tool or "tool", "detail": "refused"}
                        )
                        continue
                    await self._approve(provider, rid, tool=tool, session_key=session_key)
                    if tool:
                        text_buf.flush()  # close the current thought before the tool line
                        self._emit_activity({"kind": "tool", "tool": tool, "detail": "approved"})
                elif kind == EVENT_TOOL_CALL:
                    # The INITIAL tool_call from claude-agent-acp often has empty rawInput, so
                    # the title is the GENERIC tool label ("Read File") with no path and
                    # tool_input is "" — that's the bare "⚙ read · Read File" the operator
                    # kept seeing. The real target (e.g. the file path) arrives in the
                    # follow-up EVENT_TOOL_CALL_UPDATE refinement. So: emit the best detail we
                    # have NOW, remember what we showed per tool_call_id, and let the
                    # refinement below UPGRADE the line once the path lands.
                    detail = _tool_detail(ev)
                    tcid = getattr(ev, "tool_call_id", "") or ""
                    if tcid:
                        announced_tool_detail[tcid] = detail
                    text_buf.flush()  # close the current thought before the tool line
                    self._emit_activity(
                        {"kind": "tool", "tool": getattr(ev, "tool_kind", "tool"), "detail": detail}
                    )
                    # ENFORCE max_turns on the ACP/session path. Unlike the subprocess runner
                    # (which passes --max-turns to claude), the streaming provider has no turn
                    # limit of its own — so a thinking agent with no terminal commitment reads
                    # tools until the wall-clock (the discovered=0 over-investigation, validated
                    # 2026-06-16). Counting tool calls and stopping at the cap is the real
                    # convergence lever: it ends the stream and returns the accumulated text
                    # (a late JSON answer survives). max_turns<=0 disables the cap.
                    tool_calls += 1
                    if max_turns and tool_calls >= max_turns:
                        text_buf.flush()
                        return _finish(ok=False, error=f"max_turns ({max_turns}) reached")
                elif kind == EVENT_TOOL_CALL_UPDATE:
                    # The refinement carries the REAL target (rawInput now populated → the
                    # file path / command). If it adds detail the initial bare line lacked,
                    # emit an UPGRADED line so the feed shows WHICH file was read — the fix for
                    # "⚙ read · Read File" with no filename. Dedup by tool_call_id so a
                    # refinement that adds nothing new doesn't double the feed.
                    refined = _tool_detail(ev)
                    tcid = getattr(ev, "tool_call_id", "") or ""
                    prior = announced_tool_detail.get(tcid, "") if tcid else ""
                    if refined and refined != prior and _detail_is_richer(refined, prior):
                        if tcid:
                            announced_tool_detail[tcid] = refined
                        text_buf.flush()
                        self._emit_activity(
                            {
                                "kind": "tool",
                                "tool": getattr(ev, "tool_kind", "tool"),
                                "detail": refined,
                            }
                        )
                elif kind == EVENT_TEXT_CHUNK:
                    txt = getattr(ev, "text", "") or ""
                    if txt:
                        text_parts.append(txt)  # full text kept for the return value
                        text_buf.feed(txt)  # feed → flushes whole lines to the feed
                elif kind == EVENT_COMPLETE:
                    break
            text_buf.flush()  # emit any trailing partial line at turn end
            return _finish(ok=True)
        finally:
            if provider is not None:
                try:
                    await provider.shutdown()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    async def _reject(provider, rid, *, tool: str = "", session_key: str = "") -> None:
        """Refuse a tool request that is outside the caller's allowlist, and RECORD it.

        A refusal is more interesting than an approval — it is what an injected instruction
        looks like — so it is audited even though the tool never ran. Not ``critical``: the
        tool is already being denied, so failing the whole run because the log is
        unwritable would trade a working refusal for an outage.
        """
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=session_key or "auto-improvement",
                agent="auto-improvement",
                source="auto_improvement_loop",
                tool_name=tool or "tool",
                tool_kind=str(tool or ""),
                outcome="denied",
                request_id=rid,
                error="not_in_allowed_tools",
            )
        except Exception:  # noqa: BLE001 - the denial stands even if the audit fails
            logger.warning("SEL audit failed for a REFUSED tool %r", tool)
        try:
            await provider.reject_tool(rid)
        except Exception:  # noqa: BLE001 - the agent's own timeout covers this
            logger.debug("reject_tool failed for %r", tool, exc_info=True)

    @staticmethod
    async def _approve(provider, rid, *, tool: str = "", session_key: str = "") -> None:
        """Auto-approve a tool permission, but only once the approval is on the record.

        AUDIT-OR-DENY. The approval is unattended and unconditional, so the Security
        Event Log is the only remaining record that it happened — which is exactly the
        case ``sel().log_tool_invocation(critical=True)`` documents. If the event cannot
        be written we REJECT the tool instead of running it unaudited: an unattended
        auto-approve that leaves no trace is the thing the log exists to prevent.

        The blast radius of the approval itself is still contained elsewhere (throwaway
        push-disabled worktree, edit allowlist, RED→GREEN gate); this adds the audit
        trail those controls cannot provide.

        Never raises: a failed approval just means that one tool waits out the agent's
        own retry/timeout, which is a stalled candidate rather than a broken run.
        """
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=session_key or "auto-improvement",
                agent="auto-improvement",
                source="auto_improvement_loop",
                tool_name=tool or "tool",
                tool_kind=tool,
                outcome="auto_approved",
                request_id=rid,
                metadata={"unattended": True, "containment": "worktree+allowlist+gate"},
                critical=True,
            )
        except Exception as exc:  # noqa: BLE001 - audit failure must deny, not approve
            logger.warning("SEL audit failed for tool %r — rejecting instead of approving", tool)
            try:
                await provider.reject_tool(rid)
            except Exception:  # noqa: BLE001 - the agent's own timeout covers this
                logger.debug("reject_tool after audit failure also failed: %s", exc)
            return
        try:
            # ONE-SHOT, never `always=True`. Persistent approval tells the provider to stop
            # sending permission requests for matching calls (the base contract: "the user
            # picked 'always allow'", and ACP backends may turn it into an `addRules`
            # suggestion) — so every LATER call would skip both gates above: the per-tool
            # allowlist/denylist check AND the `critical=True` audit-or-deny write. The
            # unattended loop is exactly the caller that must not buy a blanket exemption
            # with its first approval; re-deciding per call is the whole point of routing
            # through here. Raised by the GPT review of this branch.
            await provider.approve_tool(rid)
        except Exception:  # noqa: BLE001
            pass


def _repro_test_dir(worktree: Path) -> str:
    """The directory this repo actually keeps tests in — ``tests`` or ``test``.

    The prompt used to hard-code ``test/``, but a repo using ``tests/`` (plural) then got
    a reproducing test written into a directory that does not exist, so T2 could never
    collect it and EVERY candidate failed ``test_invalid`` regardless of fix quality.
    Found by running docs/system-specs/modules/auto-improvement-test-plan.md against Zedmor/chess_test, which uses ``tests/``.

    The edit fence already permits both (``_ADDABLE_TEST_GLOBS``), so only the
    instruction was wrong. Prefers an EXISTING directory; falls back to ``test``.
    """
    counts = {
        name: len(list((Path(worktree) / name).glob("test_*.py")))
        for name in ("test", "tests")
        if (Path(worktree) / name).is_dir()
    }
    if not counts:
        return "test"
    # By file count, not alphabetical: a repo can have BOTH, and the minor one is
    # outside the suite the gate runs. Deterministic tie-break on (count, name).
    return max(counts, key=lambda n: (counts[n], n))


def author_bug_fix(
    # ``Any``, not ``AgentRunner``: the runner is DUCK-TYPED — anything exposing
    # ``run(prompt, ...) -> AgentResult`` qualifies, and the in-process
    # ``SessionAgentRunner`` (the default path) is deliberately NOT a subclass. The
    # narrower annotation was simply wrong about what the spine passes.
    runner: Any,
    *,
    candidate,
    worktree: Path,
    test_cmd_hint: str | None = None,
) -> bool:
    """Drive the agent to author a bug fix (reproducing test + minimal source edit) in
    ``worktree``. Returns True iff the agent left a real diff. Mirrors the original
    framework's ``implement()`` prompt shape (05_*.md §1.3: the agent writes the RED
    test + the fix; the spine's RED→GREEN gate then verifies the boolean transition).

    SAFETY: the agent edits inside the throwaway worktree only; the spine's edit
    allowlist + RED→GREEN gate + push-disabled clone contain the blast radius. We do
    NOT trust the agent's word that it fixed anything — the deterministic gate decides.

    ``test_cmd_hint`` is the exact, ready-to-run test command the gate itself uses
    (same interpreter + flags + PYTHONPATH). Handing it to the agent removes the
    ~20-minute environment hunt seen in practice — the agent should NOT rediscover
    which interpreter has the deps / why a plugin won't load; the gate already knows.
    """
    tdir = _repro_test_dir(worktree)
    rt = getattr(candidate, "reproducing_test", None)
    test_hint = ""
    if rt is not None:
        test_hint = (
            f"\n  suggested reproducing test id: {getattr(rt, 'test_id', '')}"
            f"\n  it must FAIL on the current (buggy) code and PASS after your fix."
        )
    # The known-good test invocation. Without this the agent burns ~20 min per candidate
    # hunting for a working interpreter; with it, it runs tests in seconds.
    run_block = ""
    if test_cmd_hint:
        run_block = (
            "\nHOW TO RUN TESTS (use EXACTLY this — do not hunt for another interpreter, "
            "do not install plugins, do not switch Python; this command already has every "
            "dependency the suite needs):\n"
            f"  {test_cmd_hint}\n"
            "Replace <test_path> with the test file (or file::case) you are running.\n"
        )
    prompt = (
        "You are the implementation step of an autonomous Kiro Crew BUG-fix loop.\n"
        f"Working dir is an isolated git worktree: {worktree}\n"
        "Investigate this candidate defect surface and, ONLY IF a real defect exists, "
        "fix it:\n"
        f"  defect surface: {candidate.target}\n"
        f"  signature: {candidate.signature}\n"
        f"  hypothesis: {candidate.hypothesis}{test_hint}\n"
        f"{run_block}\n"
        "Procedure (keep it BOUNDED — do not exceed a few investigation steps):\n"
        "  1. Read the named surface and write a MINIMAL deterministic test that would "
        "reproduce the hypothesised defect.\n"
        f"     NAME THE TEST FILE ``{tdir}/test_bug_<short_slug>.py`` (it MUST start with "
        f"``{tdir}/test_bug_`` — e.g. ``{tdir}/test_bug_format_schedule_tz.py``). This "
        "exact naming is REQUIRED: the edit fence permits a NEW reproducing test only "
        f"under ``{tdir}/test_bug_*.py``, and ``{tdir}`` is the directory THIS repo "
        "actually keeps its tests in — writing to any other directory means the gate "
        "cannot collect your test and the whole fix is rejected unmeasured. Any other "
        "path is treated as editing the existing suite. Do NOT add to or modify an "
        "existing test file — create this new one.\n"
        "  2. RUN that test against the CURRENT (unmodified) code FIRST, BEFORE editing "
        "any source.\n"
        "     - If it FAILS (defect confirmed): apply the smallest source fix that makes "
        "it pass, preserving all other behavior, then re-run to confirm GREEN.\n"
        "     - If it PASSES on the current code (the hypothesis does NOT reproduce — the "
        "code already handles this case): the surface has NO real defect. STOP "
        "IMMEDIATELY. Make NO source or test edits, revert anything you wrote, and reply "
        "with exactly: NO DEFECT FOUND — <one-line reason>.\n"
        "CRITICAL — the reproducing test is re-run by a downstream gate against the "
        "ORIGINAL un-fixed code, where it MUST COLLECT and FAIL (this is what proves the "
        "bug is real). So the test must NOT depend on ANYTHING your fix introduces:\n"
        "  • do not import, reference, monkeypatch, or attribute-access any name/symbol/"
        "import that only EXISTS after your fix (e.g. patching ``module.datetime`` when "
        "the buggy code has no ``datetime`` import yet — that is a COLLECTION error on the "
        "original code, not a failure, and the gate will reject your fix as 'cannot be "
        "RED').\n"
        "  • patch/observe only symbols ALREADY present in the unmodified module; assert "
        "on OUTPUT/behavior, not on the fix's internal helpers.\n"
        "  • if you used ``monkeypatch.setattr(mod, 'X', ...)`` make sure ``mod.X`` exists "
        "BEFORE your edit (use ``raising=True``, the default — if it errors on the base "
        "tree your test is collection-coupled to the fix and is INVALID).\n"
        "  After writing the test, VERIFY this yourself: it must import/collect and FAIL "
        "on the current code with NO edits applied.\n"
        "Do NOT fabricate a fix or a passing test for a defect you could not reproduce — "
        "an honest 'NO DEFECT FOUND' is a correct, valued outcome and costs nothing "
        "downstream (the candidate is simply skipped). A made-up fix wastes a human "
        "reviewer's time and is worse than no fix.\n"
        "Constraints: edit only what a confirmed fix + its test require; keep it minimal "
        "and reviewable; do NOT touch build config or auth/security code.\n"
        "BEFORE YOU FINISH — the T1 gate rejects the whole candidate if your diff adds ANY "
        "new lint finding, INCLUDING one in the test you just wrote. This is the single "
        "most common way a correct, fully-verified fix gets thrown away (observed live: a "
        "real fix lost to one unused ``import pytest``). So lint what you changed and "
        "clean it up:\n"
        "  • run ``ruff check --output-format=concise <files you touched>`` if ruff is "
        "available, else ``python -m pyflakes <files>``;\n"
        "  • remove unused imports and unused local variables — do NOT silence them with "
        "``# noqa``, which reads as gaming the gate;\n"
        "  • only findings YOUR diff introduced matter; the repo's pre-existing ones are "
        "not yours to fix and must be left alone.\n"
        "When you DO fix, reply with a one-line summary of the edit."
    )
    res = runner.run(
        prompt,
        cwd=str(worktree),
        allowed_tools=["Bash", "Read", "Edit", "Write", "Grep", "Glob"],
        # 10-min cap (was 1800s/30min). In practice the agent finishes the real work —
        # write reproducing test, apply minimal fix, confirm GREEN — in well under 10
        # minutes, then tends to OVER-iterate (re-verifying a fix that is already GREEN)
        # instead of returning. The worktree already holds the finished fix when the cap
        # fires, so the deterministic RED→GREEN gate (which is the real verdict) still
        # evaluates the completed work — a shorter cap just stops paying for the agent's
        # redundant self-checking and keeps cycle throughput up.
        max_turns=40,
        timeout_s=600,
    )
    # The worktree git state — NOT res.ok — is the source of truth. A TIMEOUT or a
    # MAX_TURNS cap is expected here (the agent often finishes the fix, then over-iterates
    # until the cap fires): the completed fix is already on disk, so we still harvest it and
    # let the deterministic RED→GREEN gate decide. Both are the runner's bounded "expected"
    # exits — the session path returns ``max_turns (N) reached`` for the ACP turn cap, the
    # subprocess path returns ``timeout after Ns`` — so they are treated identically. Only a
    # genuine failure (provider died, agent binary missing, explicit stop) means there's
    # nothing trustworthy to harvest — bail so we don't gate a half-written tree.
    err = (res.error or "").lower()
    is_bounded_exit = "timeout after" in err or "max_turns" in err
    if not res.ok and not is_bounded_exit:
        return False
    # Require an ACTUAL change — the agent's prose is not trusted; the worktree state is.
    # A "NO DEFECT FOUND" exit leaves the tree clean → no change → candidate skipped.
    #
    # We must detect ALL of: modified tracked files, NEW untracked files (the reproducing
    # test is always a new file), AND staged changes (the agent may ``git add`` its work
    # while running base-relative verification — observed live: an agent stashed/staged
    # during its own STAYGREEN check, leaving the fix staged). A bare ``git diff --stat``
    # sees ONLY unstaged tracked modifications and silently MISSED both the new test and
    # any staged fix → a real fix was dropped as "no diff produced". ``git status
    # --porcelain`` reports every change kind (modified, added, staged, untracked), so it
    # is the correct presence check.
    require_pinned(worktree)
    st = subprocess.run(
        ["git", "-C", str(worktree), *_GIT_SAFE_CONFIG, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if not st.stdout.strip():
        return False
    # RECONCILE the candidate's reproducing-test path to the file the agent ACTUALLY
    # wrote. The candidate arrives carrying a path the spine INVENTED from the target
    # slug (profile._candidate_from), but the prompt tells the agent to choose its own
    # ``test/test_bug_<short_slug>.py``. Those names almost never match, and the invented
    # one also COLLIDES: the slug is the target path lowercased and truncated to 40 chars,
    # so two loci sharing a long directory prefix produce the SAME filename. The gate then
    # ran ``pytest --collect-only`` on a path that does not exist, so T2 failed
    # ("reproducing test does not collect") for every candidate no matter how good the fix
    # — observed live: two independent, verified RED→GREEN fixes both rejected. Point the
    # candidate at the real file so the gate verifies the test that was actually authored.
    _adopt_authored_test(candidate, worktree, st.stdout)
    return True


def _adopt_authored_test(candidate, worktree: Path, porcelain: str) -> None:
    """Retarget ``candidate.reproducing_test`` at the repro test the agent added.

    Best-effort and non-fatal: if no added ``test_bug_*.py`` is found we leave the
    candidate untouched and let the gate judge what is there. Never raises — a
    reconciliation failure must not lose an otherwise-good fix.
    """
    rt = getattr(candidate, "reproducing_test", None)
    if rt is None:
        return
    try:
        # Porcelain fields: XY <path>, with '-> ' for renames and quoting when needed.
        added: list[str] = []
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:  # rename/copy: take the destination
                path = path.split(" -> ", 1)[1].strip()
            path = path.strip('"')
            name = Path(path).name
            if name.startswith("test_bug_") and name.endswith(".py"):
                added.append(path)
        if not added:
            return
        # Deterministic pick when the agent wrote more than one: the shortest path,
        # then lexicographic — stable across runs so a re-run gates the same file.
        chosen = sorted(added, key=lambda p: (len(p), p))[0]
        if not (worktree / chosen).is_file():
            return
        current = str(getattr(rt, "test_path", "") or "")
        if current == chosen:
            return
        # test_id doubles as the pytest nodeid for the RED/GREEN runs; keep any
        # ``::case`` selector the spine already had only if it named THIS file.
        for attr, value in (("test_path", chosen), ("test_id", chosen)):
            try:
                setattr(rt, attr, value)
            except Exception:  # noqa: BLE001 — frozen/slotted dataclass: nothing to do
                return
        logger.info(
            "author_bug_fix: adopted authored repro test %s (was %s)", chosen, current or "<unset>"
        )
    except Exception:  # noqa: BLE001 — never lose a fix over path reconciliation
        logger.warning("author_bug_fix: could not reconcile repro test path", exc_info=True)


def author_perf_fix(
    runner: Any,
    *,
    candidate,
    worktree: Path,
    test_cmd_hint: str | None = None,
) -> bool:
    """Drive the agent to author a BEHAVIOR-PRESERVING speedup in ``worktree``.

    The perf-track counterpart of :func:`author_bug_fix`, and the reason the track was
    dead: :meth:`GitHubRepoProfile.propose` returns False for every candidate ("no
    mechanical seed"), and the proposer only escalated to an agent for ``TRACK_BUG`` —
    so a perf candidate produced no diff, was recorded ``no_defect``, and the loop could
    never keep, measure, or file a perf win. Upstream had 24 hand-written mechanical
    seeds per target; a target-agnostic profile cannot ship those, so the agent authors
    the edit and the spine's A/B measurement decides — the same division of labour the
    bug track already uses (the model proposes, the deterministic gate disposes).

    Returns True iff the agent left a real diff. Never trusts the agent's claim of a
    speedup: the keeper requires the measured delta to clear the calibrated noise band,
    reproduced by a SECOND independent A/B, with the suite-still-passes guardrail and the
    test-count reward-hack guard both holding. So an imagined win costs one bounded
    cycle, never a fabricated keep.

    SAFETY: edits land only in the throwaway worktree; the edit allowlist forbids the
    tests-of-record (the ruler's own measurement subject) and the build/CI config, and
    the clone cannot push.
    """
    run_block = ""
    if test_cmd_hint:
        run_block = (
            "\nHOW TO RUN TESTS (use EXACTLY this — it already has every dependency; do "
            "not hunt for another interpreter):\n"
            f"  {test_cmd_hint}\n"
        )
    prompt = (
        "You are the implementation step of an autonomous PERFORMANCE loop.\n"
        f"Working dir is an isolated git worktree: {worktree}\n"
        "Make ONE behavior-preserving change that makes this locus measurably faster:\n"
        f"  target: {candidate.target}\n"
        f"  observation: {candidate.signature}\n"
        f"  hypothesis: {candidate.hypothesis}\n"
        f"{run_block}\n"
        "WHAT COUNTS as behavior-preserving: identical observable outputs, return "
        "shapes, exceptions, and side effects for every input the code already handles. "
        "Good moves: hoist repeated work out of a loop, avoid re-reading/re-parsing the "
        "same data, replace an O(n^2) scan with a dict/set lookup, cache a pure "
        "computation, drop a redundant copy, avoid building a large intermediate list. "
        "Do NOT change an algorithm's RESULT, relax a check, or trade correctness for "
        "speed.\n\n"
        "HARD CONSTRAINTS — a violation wastes the whole cycle because the gate rejects "
        "it mechanically:\n"
        "  • Do NOT touch any test file, conftest, or the build/CI/dependency config. "
        "The test suite is the RULER that measures you; editing it is metric gaming and "
        "is auto-rejected. A test-count change trips the reward-hack guard.\n"
        "  • Do NOT delete, skip, or weaken tests to go faster.\n"
        "  • Keep the diff MINIMAL and reviewable — one locus, not a refactor.\n"
        "  • Run the suite before you finish; it MUST still pass (that is a guardrail).\n"
        "  • LINT what you changed before finishing: the T1 gate rejects the candidate for ANY new lint finding your diff adds (``ruff check --output-format=concise <files>``, else ``python -m pyflakes``). Remove unused imports/variables rather than silencing them with ``# noqa``; the repo's pre-existing findings are not yours to fix.\n\n"
        "You do NOT decide whether this is kept. The spine measures your edit A/B against "
        "the unmodified base, requires the win to clear a calibrated noise band, and "
        "reproduces it independently. So do NOT report an estimated speedup as a fact — "
        "state what you changed and why it should be faster; the measurement is not "
        "yours to make.\n"
        "If, after reading the code, there is NO behavior-preserving win here, make NO "
        "edits and reply with exactly: NO WIN FOUND — <one-line reason>. That is a "
        "correct, valued outcome and costs nothing downstream; a fabricated 'optimization' "
        "wastes a reviewer's time and pollutes the measurement record.\n"
        "When you DO change something, reply with a one-line summary of the edit."
    )
    res = runner.run(
        prompt,
        cwd=str(worktree),
        allowed_tools=["Bash", "Read", "Edit", "Write", "Grep", "Glob"],
        max_turns=40,
        timeout_s=600,
    )
    # Same harvest rule as the bug track: the WORKTREE, not the agent's prose, is the
    # source of truth, and a bounded exit (timeout / max_turns) is an EXPECTED outcome
    # whose finished work is already on disk. Only a genuine runner failure means there
    # is nothing trustworthy to measure.
    err = (res.error or "").lower()
    is_bounded_exit = "timeout after" in err or "max_turns" in err
    if not res.ok and not is_bounded_exit:
        return False
    require_pinned(worktree)
    st = subprocess.run(
        ["git", "-C", str(worktree), *_GIT_SAFE_CONFIG, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    return bool(st.stdout.strip())
