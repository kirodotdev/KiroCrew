"""The spine agent runner's tool-request gate: whether a tool request is refused.

The unattended ``SessionAgentRunner`` puts every permission request to three checks, and
this module holds each of them: the platform governance chokepoint
(``_governance_denial``, which fails closed and names its cause on a ``_GovernanceDeny``
reason), the tokenized shell-command denylist (``shell_command_refusal`` and the tables
``_refusal`` reads), and the caller's tool allowlist (``_tool_permitted``). It also holds
what the model is told when the allowlist refuses (``_allowlist_deny_reason``) and the
event shape a refusal notice reads when a caller has no permission event
(``_bare_event``).

``kiro_crew.apps.builtins.auto_improvement.spine.agent_runner`` serves every name here and
is the import path callers use; a patch through it reaches this module
(``kiro_crew._facade_mirror``).
"""

from __future__ import annotations

import logging
import shlex
from types import SimpleNamespace
from typing import Any

from kiro_crew.config import KiroCrewConfig
from kiro_crew.constants import DENY_CAUSE_HOOK_ERROR, DENY_CAUSE_POLICY
from kiro_crew.hooks import (
    TOOL_DENY,
    HookManager,
    hook_gate_kwargs,
    hooks_config_from_config_dict,
)

#: The facade's logger, named from this package so the app loader's copy shares it too.
logger = logging.getLogger(f"{__package__}.agent_runner")


class _GovernanceDeny(str):
    """A deny reason from ``_governance_denial`` that also names WHY it is a deny.

    The gate reaches two different verdicts that both DENY: a hook judged the
    call and said no (``DENY_CAUSE_POLICY``), or the hook layer itself raised
    and the gate failed closed without judging anything
    (``DENY_CAUSE_HOOK_ERROR``). The in-band deny notice must say which -- a
    retry is reasonable after a fault and pointless after a verdict -- so the
    gate carries its ``cause`` on the reason instead of leaving the reject
    funnel to infer it from the reason's wording. A ``str`` subclass rather than
    a pair because every caller reads the gate as a string: truthiness is the
    deny/allow answer, and the text is what gets logged and steered.
    """

    cause: str

    def __new__(cls, reason: str, *, cause: str) -> "_GovernanceDeny":
        self = super().__new__(cls, reason)
        self.cause = cause
        return self


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
    # omitting them leaves a hole: bare `git push` is REFUSED while `command git push` and
    # `exec git push` would both be ALLOWED. `exec` and `builtin` are the same class as
    # `command`, so all three are listed.
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
                # (`env --unset FOO`). Treating every long option as valueless would let
                # `env --unset FOO curl …` leave `FOO` as the command and pass the `curl`
                # behind it — the bypass this table closes. An inline `--opt=value`
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


def _governance_denial(
    ev: object, *, session_key: str, agent: str, tool_kind: str | None = None
) -> str:
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
        if tool_kind is None:
            # ``tool_purpose`` is the agent's own prose about the call (display text
            # only), so it never stands in for the tool's identity.
            tool_kind = getattr(ev, "tool_kind", "") or ""
        command = _requested_command(ev)
        result = manager.on_tool_call(
            (getattr(ev, "title", "") or tool_kind or "").strip(),
            session_key=session_key,
            agent=agent,
            app="auto-improvement",
            # The shared extraction threads every enforcement-relevant event field
            # (params, diff path, trusted MCP identity, and whatever comes next); the
            # three overrides are this runner's provider-agnostic readings of a stream
            # that may not be an ``AcpEvent``: ``tool_kind`` shares the allowlist's
            # resolved kind (direct callers retain the event fallback),
            # ``command`` is recovered by ``_requested_command`` from
            # the raw params rather than ``AcpEvent.shell_command``, and ``is_shell`` is
            # taken from the EVENT, not derived from the command. `HookManager.on_tool_call`
            # denies when `is_shell and not command` — a shell tool whose command could
            # not be recovered must not be judged on its LLM-authored title
            # (`acp/types.py` states that contract). Computing it as `bool(command)`
            # inverted exactly that case: no command meant is_shell=False, so the request
            # was treated as a non-shell tool and skipped the branch written for it.
            **hook_gate_kwargs(
                ev,
                tool_kind=tool_kind,
                command=command or None,
                is_shell=bool(getattr(ev, "is_shell", False)) or bool(command),
            ),
        )
        if getattr(result, "action", "") == TOOL_DENY:
            return _GovernanceDeny(
                (getattr(result, "reason", "") or "denied by governance policy").strip(),
                cause=DENY_CAUSE_POLICY,
            )
    except Exception as exc:  # noqa: BLE001 - a broken gate must DENY, not authorize
        logger.warning("governance hook unavailable; denying (fail-closed)", exc_info=True)
        return _GovernanceDeny(f"governance hook unavailable: {exc}", cause=DENY_CAUSE_HOOK_ERROR)
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


def _bare_event(rid: str, tool: str) -> Any:
    """The event shape ``_steer_host_deny`` reads when a caller has no permission event."""
    return SimpleNamespace(request_id=rid, title=tool or "")


def _allowlist_deny_reason(allowed: list[str] | None) -> str:
    """What the model is told when ``_tool_permitted`` refuses: what this SURFACE permits.

    The caller's allowlist is a property of the run, not a verdict on the call --
    nothing about the action was judged -- so the reason names what can run here
    (the surface-policy notice tells the model to read exactly that) rather than a
    sanctioned alternative. The names are caller-authored, never agent text.
    """
    names = [str(a).strip() for a in (allowed or []) if str(a).strip()]
    if not names:
        return (
            "this unattended auto-improvement run permits no tools at all: answer "
            "from what you already have"
        )
    return (
        "this unattended auto-improvement run permits only the tools its caller "
        f"listed -- {', '.join(names)} -- and a request that names none of them "
        "(or names no tool) is refused"
    )
