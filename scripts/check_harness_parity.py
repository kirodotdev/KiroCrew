#!/usr/bin/env python3
"""check_harness_parity.py — keep the Kiro harness first-class on lines a change adds.

Kiro Crew drives one first-class agent harness, ``kiro-cli``
(``ACP_BACKEND_KIRO``, spelled ``""``), plus adapted ones: the
``ACP_BACKEND_CLAUDE`` seam, KAS, and whatever a bring-your-own adapter
registers next. An added harness may only adapt itself to the seams the Kiro
harness already runs through; it may not move, widen, or generalize them.

The defect class this gate catches is *silent capture*: a call site that spells
"this is Kiro" as the ABSENCE of another harness. It reads correctly with two
backends and then hands harness number three a capability, a sandbox waiver, or
a session label nobody granted it — and it fails toward the permissive answer,
so nothing goes red. Two such sites shipped before this gate existed
(``AcpProvider.is_session_sharing_eligible`` and ``AcpRuntime.spawn``'s
``is_kiro_cli``); both were one-line positive-test fixes.

The invariants, their ids, and what pins each one:
docs/system-specs/modules/harness-parity.md. Rules here enforce group B (H5-H8);
the structural invariants are pinned by ``test/test_harness_parity.py``.

## Why diff-scoped and not whole-tree

The tree carries eleven pre-existing negative identity tests, most of them in
the claude seam, and converting them all is a separate change with its
own review. A whole-tree gate would fail every PR until that lands and charge
the break to whoever pushed next. Added lines are complete for regression — a
line only reaches ``main`` through a diff that added it — and the whole-tree
count is still printed as a non-failing report so the backlog stays visible.

## Usage

    # enforce on what this branch adds (exit 1 on any violation)
    HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py

    # report the whole-tree backlog, enforce nothing (exit 0)
    python3 scripts/check_harness_parity.py

    # scan explicit files, ignoring git entirely
    python3 scripts/check_harness_parity.py src/kiro_crew/acp/runtime.py

    # self-test: plant one probe per rule, assert each verdict
    python3 scripts/check_harness_parity.py --test

## Escape hatch

A line the rules below model wrongly can opt out with a ``harness-ok`` marker in
a trailing comment, followed by the reason. It is unscoped and silences the
whole line, so a reviewer should ask why the positive form does not work.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Only the backend package and its consumers can hold a harness identity test.
# Widening this to the whole tree buys nothing and costs a scan of 6,000 files.
SCAN_ROOTS = ("src/kiro_crew/",)
SCAN_SUFFIX = ".py"

# This gate and its test spell every forbidden form out literally, and the
# vocabulary module (VOCABULARY_PATH below) is the vocabulary's home rather than a
# consumer of it.
SKIP_PATHS = frozenset(
    {
        "scripts/check_harness_parity.py",
        "test/test_harness_parity.py",
    }
)

# The vocabulary module, the one import path for harness identifiers and membership
# sets; the files allowed to DEFINE them are VOCABULARY_PATHS below.
#
# Moved out of ``acp/types.py`` deliberately: importing anything under
# ``kiro_crew.acp`` executes that package's ``__init__`` (client + runtime), so the
# config loader and the dashboard could not read the vocabulary and each kept a
# literal copy of the selectable list instead — the drift this rule exists to
# prevent, reached by obeying it. The vocabulary module imports nothing from
# ``kiro_crew.acp``, so every consumer can name the constants instead of copying
# them.
#
# Moved AGAIN by RFC PR 3a, from ``kiro_crew/acp_backends.py`` into
# ``kiro_crew/agent_sdk/backends.py``, so the vocabulary sits behind the agent-SDK
# boundary rather than beside it. The invariant is unchanged and this is still ONE
# path, not two: ``acp/types.py`` and ``acp_backends.py`` both re-export from here,
# and a re-export is an import rather than an assignment, so neither matches this
# rule. Pointing the rule at the shim instead would be the drift it exists to
# prevent -- a second place a definition could legally live.
VOCABULARY_PATH = "src/kiro_crew/agent_sdk/backends.py"

# The files that may define it: the vocabulary module and the owners it re-exports every
# name from, one owner per question. Callers still import from one path, so this does not
# reopen the shim question above -- an owner is where a definition lives behind that path,
# never a second path a consumer imports. Named file by file rather than by pattern, so a
# file added beside them is not exempt until it is listed here; ``test_harness_parity``
# pins this tuple to the vocabulary module's own owner imports.
VOCABULARY_PATHS = (
    VOCABULARY_PATH,
    "src/kiro_crew/agent_sdk/backends_launch.py",
    "src/kiro_crew/agent_sdk/backends_mcp.py",
    "src/kiro_crew/agent_sdk/backends_members.py",
    "src/kiro_crew/agent_sdk/backends_models.py",
    "src/kiro_crew/agent_sdk/backends_routing.py",
    "src/kiro_crew/agent_sdk/backends_sessions.py",
    "src/kiro_crew/agent_sdk/backends_turns.py",
)

SUPPRESSION = re.compile(r"harness-ok")

# Harness identifiers as string literals. A bare literal is forbidden even where
# the comparison is positive, because the value of ACP_BACKEND_KIRO is the empty
# string and only the named constant makes that legible.
_HARNESS_LITERAL = r"(?:kiro|claude|kas|claude_code|kiro-cli)"


@dataclass(frozen=True)
class Rule:
    """One forbidden line shape.

    ``fix`` is the positive form to write instead. It is printed with every
    violation, because a gate that only says no teaches nothing.
    """

    rule_id: str
    invariant: str
    pattern: re.Pattern[str]
    message: str
    fix: str
    # Paths this rule does not apply to, matched by exact repo-relative path.
    exempt: frozenset[str] = frozenset()


RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="negative-identity",
        invariant="H5",
        pattern=re.compile(
            r"not\s+(?:[A-Za-z_][\w.]*\.)?(?:is_(?:kiro|claude|kas)_backend"
            r"|_is_(?:kiro|claude|kas))\b"
        ),
        message="harness identity expressed as the absence of another harness",
        fix="test the harness positively (`is_kiro_backend`, "
        "`backend == ACP_BACKEND_KIRO`) or by membership in a named set",
    ),
    Rule(
        rule_id="negative-constant",
        invariant="H5",
        pattern=re.compile(r"(?:!=\s*ACP_BACKEND_[A-Z_]+|ACP_BACKEND_[A-Z_]+\s*!=)"),
        message="harness identity tested by inequality against one backend",
        fix="use `== ACP_BACKEND_<THIS>` or `in ACP_BACKENDS_<CAPABILITY>` — an "
        "inequality silently captures every harness added later",
    ),
    Rule(
        rule_id="bare-literal",
        invariant="H8",
        pattern=re.compile(
            rf"""(?:backend|harness)\s*[=!]=\s*(?P<q>["']){_HARNESS_LITERAL}(?P=q)"""
        ),
        message="harness compared against a bare string literal",
        fix="compare against the named constant from acp/types.py "
        "(ACP_BACKEND_KIRO is the empty string; only the name is legible)",
    ),
    Rule(
        rule_id="sandbox-delegation",
        invariant="H7",
        # The flag makes wrap_argv SKIP Crew's own seatbelt, so it fails OPEN.
        # A bool literal is fine (the argv is known statically at that site);
        # anything derived from a negation is not.
        pattern=re.compile(r"is_kiro_cli\s*=\s*(?!True\b|False\b)(?=.*(?:\bnot\b|!=))"),
        message="sandbox delegation derived from a negative harness test "
        "(fails OPEN: Crew's seatbelt is skipped for a harness with no "
        "internal sandbox of its own)",
        fix="`is_kiro_cli=<backend> in ACP_BACKENDS_INTERNAL_SANDBOX`",
    ),
    Rule(
        rule_id="vocabulary-home",
        invariant="H8",
        pattern=re.compile(r"^\s*ACP_BACKEND(?:S)?_[A-Z_]+\s*(?::[^=]+)?=\s*\S"),
        message="harness identifier or membership set defined outside the " "vocabulary module",
        fix=f"define it in {VOCABULARY_PATH} and add every new identifier to "
        "ACP_BACKENDS_KNOWN, or provider construction will not reject a typo",
        exempt=frozenset(VOCABULARY_PATHS),
    ),
    Rule(
        rule_id="non-kiro-default",
        invariant="H1",
        pattern=re.compile(r"(?:default\s*=\s*|:\s*str\s*=\s*)ACP_BACKEND_(?!KIRO\b)[A-Z_]+"),
        message="a harness other than Kiro used as a default",
        fix="default to ACP_BACKEND_KIRO — an operator who configures nothing, "
        "and one whose configuration is unusable, both get the Kiro harness",
    ),
)


@dataclass(frozen=True)
class Violation:
    path: str
    line_no: int
    rule: Rule
    text: str

    def render(self) -> str:
        excerpt = self.text.strip()[:160]
        return (
            f"{self.path}:{self.line_no}: [{self.rule.rule_id} / "
            f"{self.rule.invariant}] {self.rule.message}\n"
            f"    {excerpt}\n"
            f"    fix: {self.rule.fix}"
        )


def in_scope(path: str) -> bool:
    if path in SKIP_PATHS:
        return False
    if not path.endswith(SCAN_SUFFIX):
        return False
    return any(path.startswith(root) for root in SCAN_ROOTS)


# Python 3 has no backtick syntax, so a backtick span is always prose — the
# docstring convention for naming a symbol (``not is_claude_backend``). Naming a
# forbidden form in order to forbid it must not be a violation of it.
_INLINE_CODE = re.compile(r"``[^`]*``|`[^`]*`")


def code_part(text: str) -> str:
    """``text`` up to an unquoted ``#``.

    Quote state is tracked rather than splitting on the first ``#``, because a
    string literal may contain one (``if sep == "#" and not self._is_claude:``)
    and truncating there would hide the real call site behind it. A gate that
    misses a line is worse than one that reports a comment.
    """
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return text[:i]
        i += 1
    return text


def scannable(text: str) -> str:
    """The part of a line that is executable code.

    Length-preserving substitution keeps column positions honest for any future
    caller that wants them.
    """
    return _INLINE_CODE.sub(lambda m: " " * len(m.group()), code_part(text))


def scan_line(path: str, line_no: int, text: str) -> Iterable[Violation]:
    # Suppression is read from the RAW line: the marker lives in a comment, which
    # `scannable` has already removed by the time the rules run.
    if SUPPRESSION.search(text):
        return
    candidate = scannable(text)
    if not candidate.strip():
        return
    for rule in RULES:
        if path in rule.exempt:
            continue
        if rule.pattern.search(candidate):
            yield Violation(path, line_no, rule, text)


def read_lines(path: str) -> list[str] | None:
    """File contents as lines, or None when it cannot be read as UTF-8 text."""
    try:
        with open(os.path.join(REPO_ROOT, path), encoding="utf-8", newline="") as fh:
            return fh.read().split("\n")
    except (OSError, UnicodeDecodeError):
        return None


def scan_file(path: str) -> Iterable[Violation]:
    lines = read_lines(path)
    if lines is None:
        return
    for i, text in enumerate(lines, start=1):
        yield from scan_line(path, i, text)


def scan_lines(path: str, lines: list[str], only: set[int]) -> Iterable[Violation]:
    for line_no in sorted(only):
        if 1 <= line_no <= len(lines):
            yield from scan_line(path, line_no, lines[line_no - 1])


# ---------------------------------------------------------------------------
# git plumbing (same contract as scripts/check_brand_name.py)
# ---------------------------------------------------------------------------


def git(args: list[str]) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def tracked_files() -> list[str]:
    return [p for p in git(["ls-files"]).splitlines() if in_scope(p)]


_SCOPE_MODULE = None


def _scope():
    """The shared diff plumbing (see ``scripts/ratchet_scope.py``).

    Loaded by path, not imported: ``scripts/`` is not a package, so a plain
    import would resolve only by accident of ``sys.path[0]`` — and not at all
    when a test loads this gate by path. Shared so the env-base gates and the
    merge-ref ratchets agree on what a change added; a private copy per gate
    is how they would come to disagree. Loaded lazily so the explicit-file and
    ``--test`` modes, which never touch git, do not require it.
    """
    global _SCOPE_MODULE
    if _SCOPE_MODULE is None:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratchet_scope.py")
        spec = importlib.util.spec_from_file_location("ratchet_scope", script)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SCOPE_MODULE = module
    return _SCOPE_MODULE


def diff_base(base: str) -> str:
    """The commit to measure against (see ``ratchet_scope.resolve_base``)."""
    return _scope().resolve_base(base)


def changed_paths(frm: str) -> list[str]:
    """In-scope paths this change touches (``ratchet_scope.changed_paths_at``)."""
    try:
        paths = _scope().changed_paths_at(frm)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"::error::harness gate: cannot diff against {frm} — the base commit "
            f"is not present. Fetch it before running, or unset HARNESS_BASE_REF "
            f"to report whole-tree counts without enforcing.\n{exc.stderr}"
        )
    return [p for p in paths if in_scope(p)]


def added_lines(frm: str, path: str) -> set[int]:
    """1-based line numbers this change adds to ``path``.

    Delegates to ``ratchet_scope.added_lines_at``: base-to-working-tree (a
    local run sees uncommitted edits), ``--text`` so a ``-diff`` gitattribute
    cannot hide a file, and a pure deletion contributes nothing.
    """
    return _scope().added_lines_at(frm, path)


# ---------------------------------------------------------------------------
# X-feature captures (whole tree, shrink-only allowlist)
# ---------------------------------------------------------------------------
#
# A non-standard (X) capability -- an underscore method, a ``_meta`` field, a
# harness-native verb -- is proven once, on one adapter version. These sets are
# where that proof decides behaviour, so every member must cite, in the comment
# block directly above the set, a live capture under
# ``test/fixtures/acp_frames/<backend>/`` that a test under ``test/`` loads. KAS
# cites ``kas/``: a ``kiro/`` capture proves kiro-cli, not KAS.
# A value of None checks every member; a frozenset checks only those members.
X_FEATURE_SETS: dict[str, frozenset[str] | None] = {
    "ACP_BACKENDS_STEER": None,
    "ACP_BACKENDS_STEERING_REQUEST": None,
    "ACP_BACKENDS_STEER_CONCURRENT_PROMPT": None,
    "ACP_BACKENDS_STEER_RETRACT": None,
    "ACP_BACKENDS_COMPACT": None,
    "ACP_BACKENDS_SIDE_READONLY": None,
    "ACP_BACKENDS_MEMBER_CAPABILITIES": None,
    "ACP_BACKENDS_MARKDOWN_AGENT_SPECS": None,
    "ACP_BACKENDS_KIRO_SLASH_COMMANDS": None,
    "ACP_BACKENDS_MEMBER_DISPATCH": frozenset({"codex", "kas", "opencode"}),
    # pi's membership rides its gate extension's confirm dialog, not a mirror.
    "ACP_BACKENDS_HONOR_ZERO_TOOL_BAN": frozenset({"kas", "kiro", "opencode", "pi"}),
    "ACP_BACKENDS_MATE_WORKER_KIT": None,
    "ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL": None,
    # copilot's drop is off the wire (its log, no frame), so no capture can show it.
    "ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP": frozenset({"deepseek", "goose"}),
    "ACP_BACKENDS_UNMARKED_COMPACTION_PAIR": None,
    "ACP_BACKENDS_MCP_WIRE_REINJECT": None,
    "ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS": None,
    # kiro-cli 2.35.1 / KAS 0.66.70 / codex-acp 2.2.2: each member's teardown
    # verb is captured live, the same id answered before and 'not found' after.
    "ACP_BACKENDS_SESSION_EVICTION": None,
    # KAS 0.66.70: the settings channel's neverDefer flips the probe tool's
    # toolOrigin to "client".
    "ACP_BACKENDS_CLIENT_META_SETTINGS": None,
    # KAS 0.66.70 spawned without --auth-method cli sends _kiro/auth/getAccessToken.
    "ACP_BACKENDS_HOST_AUTH_CALLBACK": None,
    # kiro-cli 2.35.1 --agent off disk; KAS 0.66.70 via customAgents: the spec's
    # own MCP server reached the session with an EMPTY session/new array.
    "ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE": None,
    "ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN": None,
    # KAS 0.66.70 (kiro-cli 2.36.0): the consent URL after a resetServer sign-in.
    "ACP_BACKENDS_OPEN_EXTERNAL_URL": None,
    # KAS 0.66.70: summarization_* sent on its own once a turn crosses the threshold.
    "ACP_BACKENDS_HARNESS_MANAGED_COMPACTION": None,
    # Every member: kiro-cli and KAS credits, claude/opencode/goose prompt-result tokens.
    "ACP_BACKENDS_REPORT_USAGE": None,
    # Strands CLI 0.2.3: the -32603 a disposed backend answers, then a load in a
    # second process.
    "ACP_BACKENDS_RELOAD_DISPOSED_SESSION": None,
    # Strands CLI 0.2.3: a second process with a changed --mcp-config loads the
    # same session; the added server's tool is offered, a withdrawn one's is not.
    "ACP_BACKENDS_MCP_RELAUNCH_RELOAD": None,
}

# Coverage is opt-out: every ``ACP_BACKENDS_*`` set is in X_FEATURE_SETS, here, or
# in HARNESS_EXEMPT_SETS. These are X sets with no capture yet. Shrink-only: an
# entry missing on the base ref fails, and a removed entry must move to X_FEATURE_SETS.
X_REGISTRATION_PENDING: dict[str, str] = {
    "ACP_BACKENDS_HOOKS_LIST": "kas _kiro/hooks/* requests (KAS 0.66.70 asks them only after an initialize with _meta.kiro.hooks.enabled, which KAS_CLIENT_CAPABILITIES does not send; no capture of Crew's own handshake can show one)",
    "ACP_BACKENDS_STRUCTURED_REFUSAL": "kiro/kas _kiro.dev/metadata refusal notification (needs a Kiro service content-filter decline)",
    "ACP_BACKENDS_FORK_AT_MESSAGE": "claude _meta.jetbrains.air.fork.messageId fork",
    "ACP_BACKENDS_META_IDENTITY": "goose _meta tool identity channel",
    "ACP_BACKENDS_NATIVE_TODOS": "claude native /todos prompt command",
    "ACP_BACKENDS_AGENT_STARTED_TURNS": 'claude _meta["_claude/origin"] end-of-turn marker',
}

# Sets that are not X features, each with why. A set joins here only when its
# membership rides standard ACP, routing, or something off the wire.
HARNESS_EXEMPT_SETS: dict[str, str] = {
    "ACP_BACKENDS_KNOWN": "routing-only: the acp_backend vocabulary",
    "ACP_BACKENDS_KIRO_FAMILY": "routing-only: kiro-cli and KAS as a family",
    "ACP_BACKENDS_ACP_RUNTIME": "routing-only: AcpRuntime vs AcpClient transport",
    "ACP_BACKENDS_ACP_CLIENT_SPAWNABLE": "routing-only: which spawn branch runs",
    "ACP_BACKENDS_SELF_SERVED_ACP": "routing-only: launch table membership",
    "ACP_BACKENDS_INDEPENDENT_SETUP": "off the wire: first-run install path",
    "ACP_BACKENDS_INTERNAL_SANDBOX": "off the wire: OS sandbox nesting",
    "ACP_BACKENDS_RELAY_INTERNAL_SANDBOX": "off the wire: OS sandbox nesting on the kiro-cli relay",
    "ACP_BACKENDS_POD_HOME_REMAP": "off the wire: pod HOME relocation",
    "ACP_BACKENDS_SESSION_SHARING": "standard ACP: session/load after teardown",
    "ACP_BACKENDS_HARNESS_OWNED_SESSIONS": "standard ACP: session/load by id",
    "ACP_BACKENDS_RESUME_WITHOUT_LOAD": "standard ACP: session/resume capability",
    "ACP_BACKENDS_LOAD_WITHOUT_MODES": "standard ACP: session/load result shape",
    "ACP_BACKENDS_SESSION_MCP_ARRAY": "standard ACP: session/new mcpServers array",
    "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION": "standard ACP: session/set_config_option",
    "ACP_BACKENDS_MODEL_AT_LAUNCH": "off the wire: the model is a launch argument",
    "ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION": "standard ACP: session/set_config_option",
    "ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION": "standard ACP: advertised config options",
    "ACP_BACKENDS_ADVERTISED_MODEL_SELECTION": "standard ACP: advertised model list",
    "ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS": "standard ACP: advertised model id values",
    "ACP_BACKENDS_INLINE_COMPACTION": "standard ACP: session/prompt terminal frame",
    "ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR": "standard ACP: agent_message_chunk shape",
    "ACP_BACKENDS_SERIAL_SESSION_STARTS": "standard ACP timing; changes only error text",
    "ACP_BACKENDS_EFFORT_OVERLAY": "off the wire: workspace cli.json at spawn",
    "ACP_BACKENDS_TOOL_SEARCH_OVERLAY": "off the wire: workspace cli.json at spawn",
    "ACP_BACKENDS_SEED_LOCAL_SETTINGS": "off the wire: settings.local.json file",
    "ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD": "off the wire: engine file watcher",
    "ACP_BACKENDS_MEMBER_TOOL_LIMITS": "off the wire: saved agent file field",
    "ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY": "off the wire: spec resolution",
    "ACP_BACKENDS_SPEC_FILE_GRANTS": "off the wire: whether a start on an unowned spec is refused",
    "ACP_BACKENDS_MEMBER_PANEL": "Crew-side: panel MCP tools",
    "ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS": "Crew-side: Crew runs the spec hooks",
    "ACP_BACKENDS_LOCAL_CLEAR": "Crew-side: dashboard answers /clear",
    "ACP_BACKENDS_LOCAL_HARNESS_COMMANDS": "Crew-side: dashboard answers harness commands",
    "ACP_BACKENDS_CONTEXT_RECYCLE": "Crew-side: session recycle on full context",
    "ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT": "Crew-side: worker prompt in the context block",
}

# ``initialize.agentInfo.name`` (or the ``_meta.agent_name`` header) a capture
# must carry when it carries one, so a capture of a different adapter package
# cannot stand in for the one Crew installs (``@openma/pi-acp`` for pi).
# A backend absent here is not name-checked.
EXPECTED_AGENT_NAMES: dict[str, frozenset[str]] = {
    "kiro": frozenset({"Kiro CLI Agent"}),
    "claude": frozenset({"@agentclientprotocol/claude-agent-acp"}),
    "codex": frozenset({"@agentclientprotocol/codex-acp"}),
    "opencode": frozenset({"OpenCode"}),
    "goose": frozenset({"goose"}),
    "pi": frozenset({"pi-acp"}),
    "deepseek": frozenset({"deepseek-harness-acp"}),
    "copilot": frozenset({"Copilot"}),
    "strands": frozenset({"@strands-agents/cli"}),
}

# kiro-cli and KAS are one binary speaking one ``_kiro`` dialect, and KAS's
# ``initialize`` carries no ``agentInfo``, so a header cannot tell them apart. Every
# capture either one cites must hold its own ``initialize`` response, read off the
# wire: KAS 0.66.43 answers with ``agentCapabilities._meta.kiro.extensionMethods`` and
# no ``agentInfo``; kiro-cli 2.32.0 with ``agentInfo`` "Kiro CLI Agent" and no such
# list. A capture of either relabeled as the other fails here.
HANDSHAKE_IDENTITY: dict[str, str] = {
    "kiro": "kiro-cli initialize (agentInfo 'Kiro CLI Agent', no _meta.kiro.extensionMethods)",
    "kas": "KAS initialize (agentCapabilities._meta.kiro.extensionMethods, no agentInfo)",
}


def handshake_matches(d: str, meta: dict) -> bool:
    """Whether a capture's own ``initialize`` names the engine its directory claims."""
    if d == "kas":
        return bool(meta.get("wire_marked")) and meta.get("agent_info") is None
    if d == "kiro":
        info = meta.get("agent_info")
        return not meta.get("wire_marked") and info in EXPECTED_AGENT_NAMES["kiro"]
    return True


# (set, fixture dir) -> the capture that is missing. Shrink-only: an entry not on
# the base ref fails, and an entry whose member now passes fails until removed.
X_CAPTURE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("ACP_BACKENDS_STEERING_REQUEST", "claude"): (
        "live 0.94.0 steer during Bash ends the turn early and starts an unowned turn; "
        "under investigation"
    ),
    ("ACP_BACKENDS_STEERING_REQUEST", "goose"): "goose _goose/unstable/session/steer turn",
    ("ACP_BACKENDS_SIDE_READONLY", "goose"): "goose side session read-only turn",
    ("ACP_BACKENDS_MEMBER_CAPABILITIES", "deepseek"): "deepseek saved member spec session",
}

# Sets holding exactly one of kiro-cli and KAS whose comment does not name the
# other. Same shrink-only contract.
KIRO_KAS_PAIR_ALLOWLIST: frozenset[str] = frozenset()

_CITE = re.compile(r"acp_frames/([\w.-]+)/([\w.-]+\.jsonl)")
_NAMES_KAS = re.compile(r"\bKAS\b|\bkas\b")
_NAMES_KIRO = re.compile(r"\bkiro\b|kiro-cli|Kiro CLI")
_FIXTURES = "test/fixtures/acp_frames"


def fixture_dir(backend: str) -> str:
    return backend or "kiro"


def set_comments(root: str = REPO_ROOT) -> dict[str, str]:
    """Each ``ACP_BACKENDS_*`` set -> the ``#`` block directly above its definition."""
    import ast

    out: dict[str, str] = {}
    for rel in VOCABULARY_PATHS:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for node in ast.parse("\n".join(lines)).body:
            target = (
                node.targets[0] if isinstance(node, ast.Assign) else getattr(node, "target", None)
            )
            if not isinstance(target, ast.Name) or not target.id.startswith("ACP_BACKENDS_"):
                continue
            i, block = node.lineno - 2, []
            while i >= 0 and lines[i].lstrip().startswith("#"):
                block.append(lines[i].lstrip().lstrip("#").strip())
                i -= 1
            out[target.id] = " ".join(reversed(block))
    return out


def live_sets(root: str = REPO_ROOT) -> dict[str, frozenset[str]]:
    sys.path.insert(0, os.path.join(root, "src"))
    try:
        from kiro_crew.agent_sdk import backends
    finally:
        sys.path.pop(0)
    return {
        k: frozenset(v)
        for k, v in vars(backends).items()
        if k.startswith("ACP_BACKENDS_") and isinstance(v, frozenset)
    }


def read_capture(root: str, rel: str) -> tuple[dict, str | None] | None:
    """(header ``_meta``, agent name) of a capture, or None when it does not exist.

    The ``replayed`` key says whether the capture has its ``.expected.json``
    snapshot: ``test_acp_frame_replay`` replays every corpus file through the
    real dispatch parsers and fails without one, so a snapshot is the replay.
    ``wire_marked`` says whether an ``initialize`` response carries
    ``agentCapabilities._meta.kiro.extensionMethods``, and ``agent_info`` is the name
    in its ``agentInfo`` (header names never count; see ``HANDSHAKE_IDENTITY``).
    """
    import json

    path = os.path.join(root, _FIXTURES, rel)
    if not os.path.isfile(path):
        return None
    meta: dict = {}
    name = None
    info_name = None
    marked = False
    replayed = os.path.isfile(path[: -len(".jsonl")] + ".expected.json")
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh):
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if n == 0 and isinstance(frame.get("_meta"), dict) and "jsonrpc" not in frame:
                meta = frame["_meta"]
                name = meta.get("agent_name")
            result = frame.get("result") if isinstance(frame.get("result"), dict) else {}
            info = result.get("agentInfo")
            if isinstance(info, dict):
                info_name = info.get("name") if info_name is None else info_name
                name = info.get("name") if name is None else name
            # Only the initialize response carries both of these.
            caps = result.get("agentCapabilities") if "protocolVersion" in result else None
            caps_meta = caps.get("_meta") if isinstance(caps, dict) else None
            kiro = caps_meta.get("kiro") if isinstance(caps_meta, dict) else None
            if isinstance(kiro, dict) and isinstance(kiro.get("extensionMethods"), list):
                marked = True
    return dict(meta, replayed=replayed, wire_marked=marked, agent_info=info_name), name


def check_x_captures(
    sets: dict[str, frozenset[str]],
    comments: dict[str, str],
    capture=None,
    root: str = REPO_ROOT,
) -> dict[tuple[str, str], str]:
    """(set, fixture dir) -> why the member has no replayed live capture."""
    capture = capture or (lambda rel: read_capture(root, rel))
    gaps: dict[tuple[str, str], str] = {}
    for name, scope in X_FEATURE_SETS.items():
        if name not in sets:
            gaps[(name, "*")] = "X-feature set no longer defined; update X_FEATURE_SETS"
            continue
        cites = _CITE.findall(comments.get(name, ""))
        for backend in sorted(sets[name]):
            d = fixture_dir(backend)
            if scope is not None and d not in scope:
                continue
            own = [f for cd, f in cites if cd == d]
            if not own:
                kiro_only = d == "kas" and any(cd == "kiro" for cd, _ in cites)
                gaps[(name, d)] = (
                    "KAS cites a kiro/ capture; kiro-cli is not KAS, cite kas/"
                    if kiro_only
                    else f"comment cites no acp_frames/{d}/*.jsonl"
                )
                continue
            why = []
            for f in own:
                got = capture(f"{d}/{f}")
                if got is None:
                    why.append(f"{d}/{f} does not exist")
                    continue
                meta, agent = got
                expected = EXPECTED_AGENT_NAMES.get(d)
                if meta.get("recorded") != "live" or meta.get("backend") != backend:
                    why.append(f"{d}/{f} header is not recorded=live, backend={backend!r}")
                elif not meta.get("agent_version"):
                    why.append(f"{d}/{f} header has no agent_version")
                elif agent is not None and expected and agent not in expected:
                    why.append(f"{d}/{f} is a capture of {agent!r}, not {sorted(expected)}")
                elif d in HANDSHAKE_IDENTITY and not handshake_matches(d, meta):
                    why.append(f"{d}/{f} holds no {HANDSHAKE_IDENTITY[d]}")
                elif not meta.get("replayed"):
                    why.append(f"{d}/{f} has no .expected.json, so no test replays it")
                else:
                    why = []
                    break
            if why:
                gaps[(name, d)] = "; ".join(why)
    return gaps


def check_kiro_kas_pairs(sets: dict[str, frozenset[str]], comments: dict[str, str]) -> set[str]:
    """Sets holding exactly one of kiro-cli/KAS whose comment never names the other."""
    out = set()
    for name, members in sets.items():
        has_kiro, has_kas = "" in members, "kas" in members
        if has_kiro != has_kas:
            other = _NAMES_KAS if has_kiro else _NAMES_KIRO
            if not other.search(comments.get(name, "")):
                out.add(name)
    return out


def _literal(node):
    """A literal, reading ``frozenset(...)`` (empty included) as a set."""
    import ast

    if isinstance(node, ast.Call):
        return set(ast.literal_eval(node.args[0])) if node.args else set()
    if isinstance(node, ast.Dict):
        return {ast.literal_eval(k): _literal(v) for k, v in zip(node.keys, node.values)}
    return ast.literal_eval(node)


def _base_allowlists(base: str) -> tuple[set, set, dict] | None:
    """The base ref's two allowlists and X-set scopes, or None when it predates them."""
    import ast

    try:
        src = git(["show", f"{base}:scripts/check_harness_parity.py"])
    except subprocess.CalledProcessError:
        return None
    names = ("X_CAPTURE_ALLOWLIST", "KIRO_KAS_PAIR_ALLOWLIST", "X_FEATURE_SETS")
    found = {}
    for node in ast.parse(src).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id in names:
            found[target.id] = _literal(node.value)
    if len(found) != len(names):
        return None
    return set(found[names[0]]), set(found[names[1]]), found[names[2]]


def _narrowed(prior: dict) -> list[str]:
    """X sets the base checks that this tree drops, or checks for fewer members."""
    out = []
    for name, was in sorted(prior.items()):
        if name not in X_FEATURE_SETS:
            out.append(f"{name}: dropped from X_FEATURE_SETS; that registry only grows")
            continue
        now = X_FEATURE_SETS[name]
        if now is not None and (was is None or set(was) - now):
            out.append(f"{name}: X_FEATURE_SETS scope narrowed; it only widens")
    return out


def check_coverage(sets: dict[str, frozenset[str]]) -> list[str]:
    """Every live set sits in exactly one of the three lists, each with a reason."""
    lists = {
        "X_FEATURE_SETS": set(X_FEATURE_SETS),
        "X_REGISTRATION_PENDING": set(X_REGISTRATION_PENDING),
        "HARNESS_EXEMPT_SETS": set(HARNESS_EXEMPT_SETS),
    }
    errors = []
    for name in sorted(sets):
        homes = [k for k, v in lists.items() if name in v]
        if not homes:
            errors.append(
                f"{name}: unclassified ACP_BACKENDS_* set. Add it to X_FEATURE_SETS "
                "and cite a live capture above it; to X_REGISTRATION_PENDING if it rides "
                "a non-standard verb or field with no capture yet; or to HARNESS_EXEMPT_SETS "
                "with a one-line reason (standard ACP, routing-only, off the wire)"
            )
        elif len(homes) > 1:
            errors.append(f"{name}: listed in {' and '.join(homes)}; keep it in one")
        scope = X_FEATURE_SETS.get(name)
        if scope is not None and not scope & {fixture_dir(b) for b in sets[name]}:
            errors.append(f"{name}: X_FEATURE_SETS scope checks none of its members")
    for list_name, entries in (
        ("X_REGISTRATION_PENDING", X_REGISTRATION_PENDING),
        ("HARNESS_EXEMPT_SETS", HARNESS_EXEMPT_SETS),
    ):
        for name, why in sorted(entries.items()):
            if name not in sets:
                errors.append(f"{name}: in {list_name} but no longer defined; delete it")
            elif not why.strip():
                errors.append(f"{name}: {list_name} entry needs a one-line reason")
    return errors


def _base_pending(base: str) -> set[str] | None:
    """The base ref's X_REGISTRATION_PENDING names, or None when it predates the list."""
    import ast

    try:
        src = git(["show", f"{base}:scripts/check_harness_parity.py"])
    except subprocess.CalledProcessError:
        return None
    for node in ast.parse(src).body:
        # Annotated or plain: a plain rewrite must not switch the ratchet off.
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "X_REGISTRATION_PENDING" for t in targets):
            return set(_literal(node.value))
    return None


def _pending_ratchet(prior: set[str], sets: dict[str, frozenset[str]]) -> list[str]:
    """Pending only shrinks, and a set leaving it lands in X_FEATURE_SETS (or is gone)."""
    out = [
        f"{n}: X_REGISTRATION_PENDING entry added; that list only shrinks"
        for n in sorted(set(X_REGISTRATION_PENDING) - prior)
    ]
    out += [
        f"{n}: left X_REGISTRATION_PENDING without joining X_FEATURE_SETS for every member"
        for n in sorted(prior - set(X_REGISTRATION_PENDING))
        if n in sets and (n not in X_FEATURE_SETS or X_FEATURE_SETS[n] is not None)
    ]
    return out


def enforce_x_captures(base: str | None) -> int:
    sets, comments = live_sets(), set_comments()
    gaps = check_x_captures(sets, comments)
    pairs = check_kiro_kas_pairs(sets, comments)
    errors = [
        f"{s} [{d}]: {why}"
        for (s, d), why in sorted(gaps.items())
        if (s, d) not in X_CAPTURE_ALLOWLIST
    ]
    errors += [
        f"{s} [{d}]: allowlisted but now proven; delete the entry"
        for s, d in sorted(set(X_CAPTURE_ALLOWLIST) - set(gaps))
    ]
    errors += [
        f"{s}: holds one of kiro-cli/KAS and never names the other"
        for s in sorted(pairs - KIRO_KAS_PAIR_ALLOWLIST)
    ]
    errors += [
        f"{s}: allowlisted but now names both; delete the entry"
        for s in sorted(KIRO_KAS_PAIR_ALLOWLIST - pairs)
    ]
    prior = _base_allowlists(base) if base else None
    if prior is not None:
        added = sorted(set(X_CAPTURE_ALLOWLIST) - prior[0]) + sorted(
            KIRO_KAS_PAIR_ALLOWLIST - prior[1]
        )
        errors += [f"{e}: allowlist entry added; the allowlist only shrinks" for e in added]
        errors += _narrowed(prior[2])
    errors += check_coverage(sets)
    pending = _base_pending(base) if base else None
    if pending is not None:
        errors += _pending_ratchet(pending, sets)
    if not errors:
        print(
            f"x-capture gate: every X-feature member cites a replayed live capture "
            f"({len(X_CAPTURE_ALLOWLIST)} + {len(KIRO_KAS_PAIR_ALLOWLIST)} allowlisted); "
            f"{len(sets)} sets classified ({len(X_REGISTRATION_PENDING)} pending) ✓"
        )
        return 0
    print(
        "::error::x-capture gate: every ACP_BACKENDS_* set is classified, and a "
        "non-standard capability set must cite, above the set, a live capture under "
        "test/fixtures/acp_frames/<backend>/ that a test replays. See "
        "docs/ci/harness-parity-gate.md."
    )
    for e in errors:
        print(f"  {e}")
    return 1


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

# (label, path, line, expected rule id or None)
PROBES: tuple[tuple[str, str, str, str | None], ...] = (
    (
        "negative-property",
        "src/kiro_crew/providers/acp.py",
        "        return not self.is_claude_backend",
        "negative-identity",
    ),
    (
        "negative-private",
        "src/kiro_crew/acp/client.py",
        "        if not self._is_claude:",
        "negative-identity",
    ),
    (
        "negative-module-helper",
        "src/kiro_crew/session.py",
        "    if not provider.is_kas_backend:",
        "negative-identity",
    ),
    (
        "negative-constant",
        "src/kiro_crew/acp/runtime.py",
        "        if self._acp_backend != ACP_BACKEND_KAS:",
        "negative-constant",
    ),
    (
        "negative-constant-reversed",
        "src/kiro_crew/acp/runtime.py",
        "        if ACP_BACKEND_KAS != self._acp_backend:",
        "negative-constant",
    ),
    (
        "bare-literal",
        "src/kiro_crew/session.py",
        '    return backend == "claude"',
        "bare-literal",
    ),
    (
        "sandbox-delegation",
        "src/kiro_crew/acp/runtime.py",
        "            is_kiro_cli=self._acp_backend != ACP_BACKEND_KAS,",
        "sandbox-delegation",
    ),
    (
        "sandbox-delegation-negation",
        "src/kiro_crew/acp/client.py",
        "            is_kiro_cli=not self._is_claude,",
        "negative-identity",
    ),
    (
        "sandbox-delegation-negation-also-flags-the-flag",
        "src/kiro_crew/acp/client.py",
        "            is_kiro_cli=not self._is_claude,",
        "sandbox-delegation",
    ),
    (
        "hash-inside-string-does-not-truncate",
        "src/kiro_crew/acp/client.py",
        '        if sep == "#" and not self._is_claude:',
        "negative-identity",
    ),
    (
        "vocabulary-elsewhere",
        "src/kiro_crew/providers/acp.py",
        'ACP_BACKEND_BYO = "byo"',
        "vocabulary-home",
    ),
    (
        "membership-set-elsewhere",
        "src/kiro_crew/subagent.py",
        "ACP_BACKENDS_FAST = frozenset({ACP_BACKEND_KIRO})",
        "vocabulary-home",
    ),
    (
        "membership-set-beside-the-vocabulary-owners",
        "src/kiro_crew/agent_sdk/backends_extra.py",
        "ACP_BACKENDS_FAST = frozenset({ACP_BACKEND_KIRO})",
        "vocabulary-home",
    ),
    (
        "non-kiro-default",
        "src/kiro_crew/acp/runtime.py",
        "        acp_backend: str = ACP_BACKEND_KAS,",
        "non-kiro-default",
    ),
    (
        "non-kiro-field-default",
        "src/kiro_crew/config/loader.py",
        "        default=ACP_BACKEND_CLAUDE,",
        "non-kiro-default",
    ),
    # ── allowed forms: each must produce NO hit ──
    (
        "positive-property",
        "src/kiro_crew/providers/acp.py",
        "        return self.is_kiro_backend",
        None,
    ),
    (
        "positive-membership",
        "src/kiro_crew/providers/acp.py",
        "        return self._client.backend in ACP_BACKENDS_SESSION_SHARING",
        None,
    ),
    (
        "positive-constant",
        "src/kiro_crew/acp/runtime.py",
        "        if self._acp_backend == ACP_BACKEND_KAS:",
        None,
    ),
    (
        "sandbox-membership",
        "src/kiro_crew/acp/runtime.py",
        "            is_kiro_cli=self._acp_backend in ACP_BACKENDS_INTERNAL_SANDBOX,",
        None,
    ),
    (
        "sandbox-literal",
        "src/kiro_crew/dashboard/handlers/agents.py",
        "    return wrap_argv(argv, mode=configured_sandbox_mode(), is_kiro_cli=True)",
        None,
    ),
    (
        "vocabulary-at-home",
        VOCABULARY_PATH,
        'ACP_BACKEND_BYO = "byo"',
        None,
    ),
    *(
        (
            f"membership-set-at-{os.path.basename(path)}",
            path,
            "ACP_BACKENDS_FAST = frozenset({ACP_BACKEND_KIRO})",
            None,
        )
        for path in VOCABULARY_PATHS[1:]
    ),
    (
        "kiro-default",
        "src/kiro_crew/acp/runtime.py",
        "        acp_backend: str = ACP_BACKEND_KIRO,",
        None,
    ),
    (
        "unrelated-backend-word",
        "src/kiro_crew/sandbox.py",
        '    if backend == "namespace":',
        None,
    ),
    (
        "comment-naming-the-form",
        "src/kiro_crew/acp/client.py",
        "        # never write `not self._is_claude` here",
        None,
    ),
    (
        "docstring-prose-naming-the-form",
        "src/kiro_crew/providers/acp.py",
        "        inferring it from ``not is_claude_backend`` — an inference that",
        None,
    ),
    (
        "trailing-comment-naming-the-form",
        "src/kiro_crew/acp/runtime.py",
        "        flag = True  # not self._is_claude, historically",
        None,
    ),
    (
        "suppressed",
        "src/kiro_crew/acp/client.py",
        "        x = not self._is_claude  # harness-ok: dormant seam, see H5",
        None,
    ),
)


def self_test() -> int:
    failures = 0
    for label, path, line, expected in PROBES:
        got = {v.rule.rule_id for v in scan_line(path, 1, line)}
        # A line can violate several rules at once (a negative constant used as
        # the sandbox flag violates two), so assert membership rather than
        # first-hit: asserting order would pin the RULES tuple's ordering, which
        # is not part of the contract.
        ok = (expected in got) if expected else not got
        if not ok:
            print(
                f"  FAIL {label}: expected {expected!r}, got {sorted(got) or 'no hit'} "
                f"— {line.strip()}"
            )
            failures += 1
        else:
            print(f"  ok   {label}")

    # Every rule must be exercised by at least one probe, or a typo that
    # disables one ships green.
    covered = {expected for _, _, _, expected in PROBES if expected}
    missing = sorted({r.rule_id for r in RULES} - covered)
    if missing:
        print(f"  FAIL rule-coverage: no probe exercises {missing}")
        failures += 1
    else:
        print(f"  ok   rule-coverage ({len(RULES)} rules)")

    failures += _x_capture_self_test()
    print("self-test passed" if not failures else f"self-test FAILED ({failures})")
    return 1 if failures else 0


def _x_capture_self_test() -> int:
    """Plant each shape the X-capture rule exists for and assert its verdict."""
    live = {"recorded": "live", "agent_version": "0.0.34", "replayed": True}
    pi_capture = {"pi/steer-live.jsonl": (dict(live, backend="pi"), "pi-acp")}
    openma = {"pi/steer-live.jsonl": (dict(live, backend="pi"), "@openma/pi-acp")}
    unreplayed = {"pi/steer-live.jsonl": (dict(live, backend="pi", replayed=False), "pi-acp")}
    synthesized = {
        "pi/steer-live.jsonl": (dict(live, backend="pi", recorded="synthesized"), "pi-acp")
    }
    kas_capture = {"kas/steering-live.jsonl": (dict(live, backend="kas", wire_marked=True), None)}
    kas_unmarked = {"kas/steering-live.jsonl": (dict(live, backend="kas"), None)}
    kiro_info = dict(live, agent_info="Kiro CLI Agent")
    kiro_relabeled = {"kas/steering-live.jsonl": (dict(kiro_info, backend="kas"), "Kiro CLI Agent")}
    kiro_capture = {"kiro/compaction-live.jsonl": (dict(kiro_info, backend=""), "Kiro CLI Agent")}
    kas_relabeled = {"kiro/compaction-live.jsonl": (dict(live, backend="", wire_marked=True), None)}
    # One clause each: a kas capture that also names kiro-cli, a kiro capture that also marks KAS.
    kas_with_info = {
        "kas/steering-live.jsonl": (dict(kiro_info, backend="kas", wire_marked=True), None)
    }
    kiro_with_marker = {
        "kiro/compaction-live.jsonl": (dict(kiro_info, backend="", wire_marked=True), None)
    }
    steer_req = "ACP_BACKENDS_STEERING_REQUEST"
    cite_pi = "proof: ``test/fixtures/acp_frames/pi/steer-live.jsonl``."
    probes = (
        # label, set, members, comment, captures, expected gap key or None
        (
            "pi-joins-steering-request-with-no-capture",
            steer_req,
            {"pi"},
            "pi steers, measured live.",
            {},
            (steer_req, "pi"),
        ),
        ("capture-of-another-package", steer_req, {"pi"}, cite_pi, openma, (steer_req, "pi")),
        ("cited-capture-missing", steer_req, {"pi"}, cite_pi, {}, (steer_req, "pi")),
        ("capture-not-replayed", steer_req, {"pi"}, cite_pi, unreplayed, (steer_req, "pi")),
        ("synthesized-capture", steer_req, {"pi"}, cite_pi, synthesized, (steer_req, "pi")),
        (
            "pi-joins-side-with-no-capture",
            "ACP_BACKENDS_SIDE_READONLY",
            {"pi"},
            "",
            {},
            ("ACP_BACKENDS_SIDE_READONLY", "pi"),
        ),
        (
            "kas-cites-kiro",
            "ACP_BACKENDS_STEER",
            {"kas"},
            "``acp_frames/kiro/compaction-live.jsonl``",
            kiro_capture,
            ("ACP_BACKENDS_STEER", "kas"),
        ),
        (
            "kas-capture-without-its-initialize",
            "ACP_BACKENDS_STEER",
            {"kas"},
            "``acp_frames/kas/steering-live.jsonl``",
            kas_unmarked,
            ("ACP_BACKENDS_STEER", "kas"),
        ),
        (
            "kiro-capture-relabeled-kas",
            "ACP_BACKENDS_STEER",
            {"kas"},
            "``acp_frames/kas/steering-live.jsonl``",
            kiro_relabeled,
            ("ACP_BACKENDS_STEER", "kas"),
        ),
        (
            "kas-capture-relabeled-kiro",
            "ACP_BACKENDS_STEER",
            {""},
            "``acp_frames/kiro/compaction-live.jsonl``",
            kas_relabeled,
            ("ACP_BACKENDS_STEER", "kiro"),
        ),
        (
            "kas-capture-with-an-agentinfo",
            "ACP_BACKENDS_STEER",
            {"kas"},
            "``acp_frames/kas/steering-live.jsonl``",
            kas_with_info,
            ("ACP_BACKENDS_STEER", "kas"),
        ),
        (
            "kiro-capture-with-the-kas-marker",
            "ACP_BACKENDS_STEER",
            {""},
            "``acp_frames/kiro/compaction-live.jsonl``",
            kiro_with_marker,
            ("ACP_BACKENDS_STEER", "kiro"),
        ),
        (
            "kiro-proven",
            "ACP_BACKENDS_STEER",
            {""},
            "``acp_frames/kiro/compaction-live.jsonl``",
            kiro_capture,
            None,
        ),
        ("pi-proven", steer_req, {"pi"}, cite_pi, pi_capture, None),
        (
            "kas-proven",
            "ACP_BACKENDS_STEER",
            {"kas"},
            "``acp_frames/kas/steering-live.jsonl``",
            kas_capture,
            None,
        ),
    )
    failures = 0
    for label, name, members, comment, captures, expected in probes:
        sets = {s: frozenset() for s in X_FEATURE_SETS}
        sets[name] = frozenset(members)
        gaps = check_x_captures(sets, {name: comment}, captures.get)
        ok = (expected in gaps) if expected else not gaps
        if label == "kas-cites-kiro":
            ok = ok and "kiro-cli is not KAS" in gaps.get(expected, "")
        print(f"  {'ok  ' if ok else 'FAIL'} x-capture {label}: {sorted(gaps.items()) or 'no gap'}")
        failures += not ok
    pair_probes = (
        ("kas-only-names-no-kiro", {"S": frozenset({"kas"})}, {"S": "Only KAS."}, {"S"}),
        ("kiro-only-names-no-kas", {"S": frozenset({""})}, {"S": "Only kiro-cli."}, {"S"}),
        ("kas-only-names-kiro", {"S": frozenset({"kas"})}, {"S": "kiro-cli lacks it."}, set()),
        ("both-held", {"S": frozenset({"", "kas"})}, {"S": ""}, set()),
    )
    for label, sets, comments, expected in pair_probes:
        got = check_kiro_kas_pairs(sets, comments)
        ok = got == expected
        print(f"  {'ok  ' if ok else 'FAIL'} kiro-kas-pair {label}: {sorted(got) or 'no gap'}")
        failures += not ok
    return failures


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def report(violations: Iterable[Violation], *, enforcing: bool, base: str | None) -> int:
    violations = list(violations)
    if not violations:
        scope = f"lines added since {base}" if enforcing else "whole tree"
        print(f"harness gate: Kiro identity tested positively in the {scope} ✓")
        return 0

    if enforcing:
        print(
            f"::error::harness gate: {len(violations)} line(s) added by this change "
            f"let a harness other than Kiro inherit something by default. The Kiro "
            f"harness is first-class: an added harness adapts to these seams, it "
            f"does not widen them. See docs/system-specs/modules/harness-parity.md."
        )
    else:
        print(
            f"::notice::harness gate report: {len(violations)} pre-existing line(s) "
            f"test harness identity negatively. Not enforced here; only lines a "
            f"change adds are gated."
        )
    shown = 200 if enforcing else 40
    for v in violations[:shown]:
        print(v.render())
    if len(violations) > shown:
        print(f"... and {len(violations) - shown} more")
    if enforcing:
        print(
            "\nIf a rule models your line wrongly, a trailing `harness-ok: <reason>` "
            "comment silences the whole line — a reviewer will ask why the positive "
            "form does not work."
        )
    return 1 if enforcing else 0


def enforce_diff(base: str) -> int:
    """Enforce on the lines this change adds. Fails closed on anything unreadable."""
    frm = diff_base(base)
    found: list[Violation] = []
    unreadable: list[str] = []
    for path in changed_paths(frm):
        lines = added_lines(frm, path)
        if not lines:
            continue
        content = read_lines(path)
        if content is None:
            unreadable.append(path)
            continue
        found.extend(scan_lines(path, content, lines))

    if unreadable:
        # Reporting nothing for a file the change touched is how a gate quietly
        # stops gating, so refuse to pass instead of skipping.
        print(
            "::error::harness gate: cannot read these changed files as UTF-8 text, "
            "so the harness identity tests in them were never checked:"
        )
        for path in unreadable:
            print(f"  {path}")
        return 1

    return report(found, enforcing=True, base=base)


def force_utf8_output() -> None:
    """Print UTF-8 whatever the console's default encoding is.

    The clean verdict ends in a check mark, and a Windows cp1252 console raises
    ``UnicodeEncodeError`` on it — turning a PASS into a traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    force_utf8_output()
    if "--test" in argv:
        return self_test()

    explicit = [a for a in argv if not a.startswith("-")]
    if explicit:
        unreadable = [p for p in explicit if read_lines(p) is None]
        if unreadable:
            print(
                "::error::harness gate: cannot read these paths as UTF-8 text, so "
                "the harness identity tests in them were never checked:"
            )
            for path in unreadable:
                print(f"  {path}")
            return 1
        found = [v for p in explicit for v in scan_file(p)]
        return report(found, enforcing=True, base=None)

    base = os.environ.get("HARNESS_BASE_REF", "").strip()
    if base:
        lines = enforce_diff(base)
        # The merge-base, as enforce_diff measures: main proving a member after this
        # branch forked must not read as this branch adding an allowlist entry.
        return enforce_x_captures(diff_base(base)) or lines

    found = [v for path in tracked_files() for v in scan_file(path)]
    lines = report(found, enforcing=False, base=None)
    # Whole-tree with an allowlist, so it is enforced in this mode too.
    return enforce_x_captures(None) or lines


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
