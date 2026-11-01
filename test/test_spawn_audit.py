"""Subprocess-spawn audit.

Every subprocess spawn in ``src/kiro_crew`` must be either

* routed through the sandbox chokepoint (its enclosing function calls
  ``sandboxed_spawn_argv``, ``wrap_argv``, or the regression-pinned async
  adapter around ``sandboxed_spawn_argv``), so the spawned process gets
  OS-level filesystem isolation and a credential-scrubbed environment, or
* explicitly listed in ``BENIGN_SPAWNS`` below, the union of
  ``spawn_audit_helpers.BENIGN_PACKAGE_SPAWNS`` and ``spawn_audit_app_spawns.BENIGN_APP_SPAWNS``,
  as a spawn whose command, arguments, and working directory are NOT agent-influenced.

This test is a regression tripwire: adding a NEW unrouted spawn makes it fail
until the author either routes the spawn through the chokepoint or, having
confirmed the command is not agent-influenced, adds its ``file::function`` key
to ``BENIGN_SPAWNS`` with a justification. This is the "lint or unit test
asserting every subprocess spawn is either allow-listed as benign or routed
through that wrapper" the finding asks for.

The agent-influenced sites — the MCP server probe
(``mcp_discovery.probe_server``), the TaskRunner test command
(``task_executor.run_tests``), TaskRunner git operations
(``git_coord._git`` / ``_is_git_repo``), and authenticated source-provider
fetches (``source_providers._run_json``) — are routed through
``sandboxed_spawn_argv`` and MUST stay routed (see
``test_agent_influenced_sites_are_routed``).

The remaining unrouted spawns, the rows of those two tables, are pre-existing and fall into these
groups, none of which is the finding's agent-influenced-spawn vector:

* Operator-invoked CLI / setup / doctor / self-update (fixed argv against our
  own install: git pull, pip, npm, kiro-cli/kirocrew update,
  systemctl/launchctl, node/ollama bootstrap).
* Internal process management (read our own ppid; enumerate/kill our own
  managed/orphaned processes) and system-metrics probes (fixed sysctl/ps/etc).
* Trusted-side gateway/MCP-backend spawns (``mcp_gateway`` — MCP backends sit
  on the trusted side of the sandbox boundary by design) and the Playwright CLI
  toolchain spawns in ``browser_cli`` (fixed argv, operator-triggered install;
  the agent's own browser commands are shell tool calls gated by the approval
  path, not spawns we make).
* Operator-configured state sync (``sync/*`` — git/s3/rsync/litestream
  push/pull against an operator-set remote) and app-registry package install
  of an operator-installed package.

FOLLOW-UP HARDENING CANDIDATES (defense-in-depth, NOT this finding, tracked for
a later pass — they are allowlisted here because their repo/remote is
operator-configured rather than agent-selected in the finding's sense):
``apps/builtins/code_reviewer/git.py`` git against a locally-checked-out CR
repo, and ``sync/*`` push/pull. Routing these would also need their real-git
unit tests to tolerate the sandbox wrapper.

The Design Tweak builtin (``apps/builtins/design_tweak/backend/server.py``)
adds three spawns in the same non-agent-selected categories:
``_lsof_fields`` (fixed-argv ``lsof`` on numeric pids the backend discovered —
a system probe like the sysctl/ps ones above), ``_h_pick_folder`` (fixed-argv
``osascript`` running a hardcoded AppleScript for the macOS folder picker — a
desktop-UI spawn), and ``_start_dev_proc`` (the user's OWN registered project
dev server: cwd is the user-selected project dir and the argv is that project's
package-manager dev script — operator/user-configured, reached only via the
HMAC-signed gateway proxy, not agent-prompt-selected). ``_start_dev_proc`` is
directly analogous to ``code_reviewer/git.py`` and is a follow-up sandbox-routing
candidate; routing a long-lived dev server would need the resource/filesystem
wrapper not to starve it.

OUT OF SCOPE BY SHAPE: a skill's own helper scripts, under ``builtin_skills/**``
or ``apps/builtins/<app>/skills/**`` (see ``_is_bundled_skill_asset``). These are
scripts an agent runs in a shell, gated by the shell approval path, not
subprocesses this package spawns; the gateway neither imports them (pinned by
``test_bundled_skill_assets_are_not_imported``) nor execs them (such an exec
would be a spawn site in a non-exempt file, reviewed there).
"""

from __future__ import annotations

import ast
import functools
import textwrap
from collections import deque
from collections.abc import Callable
from pathlib import Path

import pytest
from source_corpus import (
    candidate_sources,
    iter_candidate_sources,
    iter_statements,
    parsed_candidates,
)
from spawn_audit_app_spawns import BENIGN_APP_SPAWNS
from spawn_audit_helpers import BENIGN_PACKAGE_SPAWNS

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

# One xdist worker for the whole module: every audit test here reads the module-cached
# spawn census (``_spawn_functions_in``, one cached scan per census part). Under
# `--dist loadgroup` an unmarked module is spread across workers and each worker re-pays
# the scan of every part it touches. Grouping keeps each part's cache single-copy per run.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_spawn_audit")


def _is_bundled_skill_asset(path: Path) -> bool:
    """True for a skill's own helper script rather than gateway runtime code.

    Two shapes hold the same kind of thing:

    * ``builtin_skills/**`` -- the top-level bundled skills.
    * ``apps/builtins/<app>/skills/**`` -- skills an app ships with itself
      (dev-fleet's recording and pod-e2e scripts).
    * ``dashboard_templates/builtin/<id>/helpers/**`` -- a Dashboard template's upkeep
      helpers, which ``dashboard_upkeep`` copies into the crewmate's upkeep skill.

    Both are scripts the AGENT runs in the USER's repo/shell, not code the
    gateway imports or spawns; they ship under the package only for packaging,
    and ``test_bundled_skill_assets_are_not_imported`` pins that. The sandbox
    spawn chokepoint governs the gateway's OWN subprocess usage, so these
    assets are out of scope for this audit -- the shell approval path is what
    governs an agent running them, exactly as it governs the agent's own
    browser commands (see ``browser_cli`` in the module docstring).
    """
    parts = path.relative_to(_SRC_ROOT).parts
    if "builtin_skills" in parts:
        return True
    if parts[:2] == ("dashboard_templates", "builtin") and parts[3:4] == ("helpers",):
        # A Dashboard template's upkeep helper: copied into the crewmate's upkeep
        # skill and run by the agent in its shell, exactly like a skill script.
        return True
    return parts[:2] == ("apps", "builtins") and "skills" in parts[2:]


def _is_container_image_asset(path: Path) -> bool:
    """True for a container image's own source rather than gateway runtime code.

    ``apps/builtins/<app>/crew/runtime/**`` is the build context of a Linux image: a
    Dockerfile, the processes that image runs, and their tests. The gateway does not
    import it, which ``test_container_image_assets_are_not_imported`` pins and which the
    tree itself enforces by assuming an installed layout where ``kiro_crew`` is absent.

    Its subprocess calls are the image's supervisor starting the image's own children,
    inside a container the owner's machine never runs. The sandbox chokepoint governs the
    gateway's OWN spawning, so it has nothing to say about them -- the same reasoning as
    ``_is_bundled_skill_asset`` above, applied to a different shape.
    """
    parts = path.relative_to(_SRC_ROOT).parts
    return parts[:2] == ("apps", "builtins") and "crew" in parts and "runtime" in parts


# Attribute names that actually spawn a child process.
_SPAWN_ATTRS = {
    "Popen",
    "run",
    "call",
    "check_output",
    "check_call",
    "create_subprocess_exec",
    "create_subprocess_shell",
}
# The modules whose attributes actually spawn a child. Only calls whose receiver
# resolves to one of these count, which excludes e.g. ``proc.communicate`` or
# ``pool.run``.
#
# These are the CANONICAL names. The receiver a call site actually spells is
# whatever that file bound the module to, so it is derived per file by
# :func:`_spawn_module_bindings` rather than enumerated here. Naming aliases in a
# literal set is what made this audit blind: ``acp/client.py`` imports
# ``subprocess as subprocess_mod`` and five spawns in it were invisible, while
# ``sp``, ``_sp`` and ``_asyncio`` were live aliases in other scanned files at the
# same time. A scanner that has to be TOLD each alias is a scanner that is blind by
# default, and its greenness says nothing.
_SPAWN_MODULES = {"subprocess", "asyncio"}

# Spawn helpers called as a BARE NAME rather than ``module.attr`` -- they are
# imported directly, so the receiver check above cannot see them. Without this
# the audit goes blind the moment a call site moves to the wrapper.
_SPAWN_NAMES = {
    "_create_ffmpeg_subprocess",
    "create_subprocess_limited",
    "run_limited",
    "popen_limited",
    # kiro_prerequisite's one-shot wrapper; its call sites must stay visible.
    "spawn_supervised_oneshot",
}

# Tokens whose presence anywhere in the enclosing function marks the spawn as
# routed through the sandbox chokepoint. ``_prepare_sandboxed_spawn`` is the
# prerequisite flow's async adapter; the dedicated regression test below pins
# it to ``sandboxed_spawn_argv`` so this indirection cannot weaken the gate.
_ROUTED_TOKENS = (
    "sandboxed_spawn_argv",
    "wrap_argv",
    "_prepare_sandboxed_spawn",
)

# Token marking a routed function as also applying a kernel resource ceiling
# (RLIMIT_NPROC/NOFILE/CPU/AS) to its child — the second layer of the spawn
# guarantee (security-review bdf0d7e5). Every sandbox-routed function must
# reference one: the sandbox gives the child filesystem + credential isolation,
# this gives it a fork-bomb / resource ceiling. Functions whose ONLY spawns are
# fixed-argv internal probes (no agent-influenced child) are exempted in
# ``PREEXEC_EXEMPT`` below.
#
# ``create_subprocess_limited`` (async) and ``run_limited`` / ``popen_limited``
# (sync) are the preferred forms: they deliver the same limits AFTER exec via the
# spawn shim instead of in a fork child of this threaded gateway. The two
# ``*_preexec`` names remain valid for the wrappers' own no-shim fallbacks and
# the terminal's pre-resolved ioctl callback.
#
# Every token is matched as a CALL (trailing paren) rather than a bare name: the
# check scans the enclosing function's raw source, docstrings and comments
# included, so a bare-name match lets prose like "routed through run_limited"
# satisfy the gate while the actual spawn silently reverts to a bare
# ``subprocess.run`` (verified by mutation before the parens were added).
_PREEXEC_TOKENS = (
    "create_subprocess_limited(",
    "run_limited(",
    "popen_limited(",
    # Spawns through create_subprocess_limited, so its callers get the same limits.
    "spawn_supervised_oneshot(",
    "resource_limit_preexec()",
    "session_host_preexec(",
)

# Routed functions exempt from the resource-limit requirement: the enclosing
# function is sandbox-routed (so it appears routed) but the specific spawn is a
# fixed-argv internal probe against our own process/host, not a child running
# agent-influenced code — a resource ceiling adds nothing. Keyed by
# ``<relpath>::<function>`` with a justification, same discipline as
# ``BENIGN_SPAWNS``.
PREEXEC_EXEMPT: frozenset[str] = frozenset(
    {
        # Applies the SAME limits post-exec instead of post-fork. This spawn
        # already prepends the immutable process-group supervisor
        # (`python -I -c <supervisor>`), so it hands the resolved rlimits to that
        # supervisor as `--rlimits=NAME:value,...` (see
        # sandbox.resource_limit_supervisor_argv) and the supervisor calls
        # setrlimit before forking the real child, which inherits the ceiling.
        # The limits are therefore NOT dropped; only the delivery point moved.
        # Why it had to move: `preexec_fn` forces CPython off posix_spawn/vfork
        # onto a plain fork() of the multi-GB, ~118-thread gateway and runs
        # Python in the child before exec. A lock another thread held at fork
        # time is unreleasable there, and a child so wedged deadlocked in a
        # futex, never exec'd, never exited, and pinned every fd it inherited --
        # including gateway.lock and the dashboard listener.
        "kiro_prerequisite_process.py::_run_process",
        # Spawns NOTHING. It PATCHES `sandboxed_spawn_argv` and raises from the
        # replacement, so the argv is captured at the boundary and no child is ever
        # created -- but the name appears in the function body, which is what this scan
        # matches on. A resource ceiling has nothing to apply to. Deliberately NOT in
        # BENIGN_SPAWNS: that set is for an UNROUTED spawn, and the staleness check
        # correctly rejects this key there.
        "apps/builtins/ops_mission_control/tests/test_ledger_sync_git.py"
        "::test_every_git_invocation_carries_the_identity",
    }
)

# Benign spawns: command/args/cwd are fixed or operator-controlled, NOT
# influenced by the agent, a hostile MCP-config entry, or an agent-selected
# repository. Keyed by ``<relpath>::<enclosing function>``. When adding an
# entry, confirm none of the argv, the cwd, or the resolved binary can be
# steered by the LLM/agent before listing it. See the module docstring for the
# category breakdown and follow-up hardening candidates.
#
# The rows live in two tables by the path they key on: a spawn under
# ``apps/builtins/`` goes in ``spawn_audit_app_spawns.BENIGN_APP_SPAWNS``, any
# other in ``spawn_audit_helpers.BENIGN_PACKAGE_SPAWNS``.
BENIGN_SPAWNS: frozenset[str] = BENIGN_PACKAGE_SPAWNS | BENIGN_APP_SPAWNS


# First-party fixed-argv spawn sites: every call site in ``src/kiro_crew`` that
# passes the ``first_party_fixed_argv`` keyword into the sandbox chokepoint
# (``sandboxed_spawn_argv`` / ``wrap_argv``). The flag buys an UNCONFINED spawn
# on a backend-less host (the carve-out), so "first-party" must be a
# reviewed property, not a copy-pasteable kwarg: a new site must be added here
# WITH a justification proving the full argv is derived inside this package
# with zero agent/repo/user-config influence, OR that it runs the kiro-cli
# Kiro Crew already launches for every ACP session -- resolved only by
# ``kiro_cli.find_kiro_cli_candidates`` -- with a fixed argv. That binary is
# already trusted and run on every ACP launch, so the carve-out adds no new
# trust (owner decision, enterprise sign-in check). Keyed by
# ``<relpath>::<enclosing function>``, same discipline as ``BENIGN_SPAWNS``.
FIRST_PARTY_SPAWNS: frozenset[str] = frozenset(
    {
        # The managed-server probe. The flag value is COMPUTED, not literal:
        # ``_is_first_party_managed_argv`` requires the spec's command+args+env
        # to EQUAL what this package derives for the managed server
        # (``agent._kirocrew_mcp_invocation`` + ``agent._managed_mcp_env``, the
        # single sources of truth the specs are force-re-resolved from) — never
        # user-config text. Env is compared because the probe merges the spec's
        # env into the child environment and ``LD_PRELOAD`` changes what code
        # runs for the same argv. Third-party servers and any customized
        # managed command/args/env compare unequal, pass False, and keep the
        # full fail-close + opt-in behavior.
        "mcp_discovery_probe.py::probe_server",
        # The built-in SAPI synthesizer. The flag value is COMPUTED
        # (``engine == SYSTEM_ENGINE_SAPI``), so only the Windows branch claims
        # it: that argv is a System32 ``powershell.exe`` resolved by
        # ``trusted_system_bin`` (never PATH), four module-constant flags, and a
        # base64 ``-EncodedCommand`` whose script interpolates ONLY
        # internally-derived ``mkstemp`` paths plus an integer from
        # ``_validate_rate``. The two values a user or a model supplies — the
        # reply text and ``system_voice`` — are spilled to files the script
        # reads at runtime, so neither reaches argv. The ``say``/``espeak-ng``
        # branches DO carry the configured voice on argv and evaluate False,
        # which costs nothing: both platforms have a backend, where the
        # carve-out is inert anyway.
        "voice_reply.py::_synthesize_system",
        # The shared local-TTS runner. It DECIDES nothing: it forwards its own
        # parameter to the chokepoint, and its only caller is the reviewed
        # ``_synthesize_system`` entry above. Listed rather than excluded
        # because the scan is deliberately value-blind, so a future caller
        # passing True here would still have to be reviewed as its own entry.
        "voice_reply.py::_run_tts_subprocess",
        # The enterprise sign-in check (owner decision, enterprise smoke test
        # P9; owner-approved widening of this rule). ``_probe`` claims the flag
        # with a literal ``True`` only inside ``edition.is_enterprise()``, only
        # after the sandboxed ``--version`` was refused, and only for two fixed
        # argvs: ``--version`` (via ``_audited_probe``) and ``whoami`` (via
        # ``_audited_identity_probe``, real-home branch of ``_run_auth_command``
        # only; the isolated-home branch raises on the flag). The binary is the
        # candidate ``kiro_cli.find_kiro_cli_candidates`` resolved -- the same
        # kiro-cli ACP launches on every session, so already trusted and run --
        # made absolute with ``abspath``. No agent, repo or user text reaches the
        # args. ``_run_process`` and ``_prepare_sandboxed_spawn`` only forward
        # the flag to ``sandboxed_spawn_argv``; their other callers pass the
        # default False.
        "kiro_prerequisite_probe.py::_probe",
        "kiro_prerequisite_probe.py::_audited_probe",
        "kiro_prerequisite_probe.py::_audited_identity_probe",
        "kiro_prerequisite_probe.py::_run_auth_command",
        "kiro_prerequisite_process.py::_run_process",
        "kiro_prerequisite_process.py::_prepare_sandboxed_spawn",
    }
)

_FIRST_PARTY_KWARG = "first_party_fixed_argv"


@functools.lru_cache(maxsize=1)
def _collect_first_party_flag_sites() -> frozenset[str]:
    """``<relpath>::<func>`` for every call passing the first-party kwarg.

    AST-based rather than a substring scan: it matches any ``ast.Call``
    carrying a keyword named ``first_party_fixed_argv`` REGARDLESS of the value
    expression — a site passing a computed bool must be reviewed exactly like
    one passing a literal ``True`` (the computation is part of the claim).
    Like the sibling scans in this file, ``**kwargs`` indirection is out of
    scope (an aliased spawn already hid from the spawn detector once); the
    PR-review gates cover deliberately obfuscated passes.
    ``sandbox_spawn.py`` is excluded by design: it threads the parameter that
    ``wrap_argv`` (``sandbox_wrap.py``) defines through ``sandboxed_spawn_argv``, so its internal
    forwarding is the mechanism under audit, not a spawn site.

    Parses only files whose text already contains ``first_party_fixed_argv``
    (``test/source_corpus.py``'s shared, narrowed read) instead of re-``rglob``
    + re-``read_text``-ing the whole tree: the literal must appear verbatim for
    a keyword of that name to exist, so no candidate is dropped. Cached like
    the sibling scans, and released with the rest of the corpus by
    ``test/conftest.py::_release_source_corpus_after_module`` at module end.
    """
    out: set[str] = set()
    for path, source in candidate_sources(require_all=(_FIRST_PARTY_KWARG,)):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if (
            rel == "sandbox_spawn.py"
            or _is_bundled_skill_asset(path)
            or _is_container_image_asset(path)
        ):
            continue
        tree = ast.parse(source, str(path))
        funcs = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not any(kw.arg == _FIRST_PARTY_KWARG for kw in node.keywords):
                continue
            enc = "<module>"
            best = -1
            for f in funcs:
                if f.lineno <= node.lineno <= (f.end_lineno or f.lineno) and f.lineno > best:
                    best = f.lineno
                    enc = f.name
            out.add(f"{rel}::{enc}")
    return frozenset(out)


def test_every_first_party_spawn_is_allowlisted():
    """A new site passing ``first_party_fixed_argv`` must be reviewed here.

    The flag buys an unconfined spawn on a backend-less host, so passing it
    from an unreviewed site is a sandbox bypass. Add the ``file::function`` key
    to ``FIRST_PARTY_SPAWNS`` ONLY after confirming the full argv is derived
    inside this package with zero agent/repo/user-config influence, or that it
    runs the ACP kiro-cli (resolved only by ``find_kiro_cli_candidates``) with a
    fixed argv, and record that reasoning in the allowlist comment.
    """
    unexpected = _collect_first_party_flag_sites() - FIRST_PARTY_SPAWNS
    assert not unexpected, (
        "New site(s) passing first_party_fixed_argv into the sandbox "
        "chokepoint:\n  "
        + "\n  ".join(sorted(unexpected))
        + "\n\nThis flag permits an UNCONFINED spawn on a host with no sandbox "
        "backend (issue #1563 carve-out). Confirm the full argv is derived "
        "inside this package with zero agent/repo/user-config influence (or is "
        "the ACP kiro-cli from find_kiro_cli_candidates with a fixed argv), then "
        "add the file::function key to FIRST_PARTY_SPAWNS with a justification."
    )


def test_first_party_allowlist_has_no_stale_entries():
    """Every FIRST_PARTY_SPAWNS entry must still name a real flag-passing site,
    so the allowlist cannot silently accumulate dead exemptions that would mask
    a future regression at the same key."""
    stale = FIRST_PARTY_SPAWNS - _collect_first_party_flag_sites()
    assert not stale, (
        "Stale FIRST_PARTY_SPAWNS entries (no longer a first-party flag site — "
        "remove them):\n  " + "\n  ".join(sorted(stale))
    )


def _spawn_module_bindings(tree: ast.Module) -> set[str]:
    """Every local name in one file that refers to a spawn-capable module.

    Reads the file's OWN import statements -- including function-local ones, which
    is where an alias most often hides -- so a receiver is recognized by what it
    BINDS rather than by what it happens to be called. ``import subprocess`` binds
    ``subprocess``; ``import subprocess as sp`` binds ``sp``; ``from concurrent
    import futures`` binds neither.

    The canonical names are always included, so a file that imports nothing under an
    alias is scanned exactly as before. An import is a statement, so the walk reads
    statement bodies only and skips the expressions that make up most of a module.
    """
    bound = set(_SPAWN_MODULES)
    for node in iter_statements(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # ``from x import subprocess as y`` is rare but binds a name that
            # spawns, and skipping it would leave the same shape of hole this
            # function exists to close.
            for alias in node.names:
                if alias.name in _SPAWN_MODULES:
                    bound.add(alias.asname or alias.name)
    return bound


def test_a_spawn_module_alias_is_derived_not_enumerated() -> None:
    """The scanner must recognize a receiver it has never been told about.

    This is the general form of a hole this audit shipped with: it matched receivers
    against a hardcoded name set, so ``import subprocess as subprocess_mod`` hid five
    spawns in ``acp/client.py`` and a function-local ``import subprocess as sp`` hid
    eight more in an app's test module -- while the audit stayed green, because it was
    not finding them and accepting them, it was not finding them at all.

    Naming each alias as it is discovered cannot fix that: the next alias is invisible
    again. So bindings are derived from each file's own imports, and this pins that a
    NAME nobody has ever written down is still recognized.
    """
    tree = ast.parse(textwrap.dedent("""
            import asyncio as _never_seen_before
            import subprocess as _also_novel

            def f():
                import subprocess as _function_local

                _function_local.run(["true"])
            """))
    bound = _spawn_module_bindings(tree)
    assert {"_never_seen_before", "_also_novel", "_function_local"} <= bound
    assert _SPAWN_MODULES <= bound, "the canonical names must always be recognized"


def test_an_unrelated_import_binds_no_spawn_receiver() -> None:
    """Derivation must not widen the scan to modules that do not spawn.

    A receiver set that grew on every import would flag ``pool.run`` and
    ``proc.communicate``, and an audit that cries wolf gets its findings dismissed.
    """
    tree = ast.parse(textwrap.dedent("""
            import json as subprocess_lookalike
            from concurrent import futures as pool
            """))
    bound = _spawn_module_bindings(tree)
    assert "subprocess_lookalike" not in bound
    assert "pool" not in bound


#: How many parts the spawn census is read in. The per-test timeout bounds ONE test, and
#: the whole census -- every candidate file parsed and walked, under coverage, on a shared
#: runner -- is more than one test's budget. Each part is one test of each per-part audit,
#: so the budget bounds a quarter of the tree; the parts hold about the same amount of
#: source, and the sum of the work does not change.
CENSUS_PARTS = 4

#: Nodes with no call inside and no binding: a name, a constant, an expression context.
_LEAVES = frozenset({ast.Name, ast.Constant, ast.Load, ast.Store, ast.Del})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@functools.lru_cache(maxsize=1)
def _spawn_census_parts() -> tuple[tuple[Path, ...], ...]:
    """The files the spawn census parses, dealt into ``CENSUS_PARTS`` parts of about
    equal source size: each file, largest first, joins the lightest part so far.

    Only files whose text already contains one of the spawn attribute or bare-name
    tokens are candidates (``test/source_corpus.py``'s shared, narrowed read): the
    matched ``ast.Call`` always spells one of these tokens verbatim in the source, so
    narrowing cannot drop a real spawn site. A skill's own helper scripts and a
    container image's own source are not gateway runtime code paths -- see
    ``_is_bundled_skill_asset`` and ``_is_container_image_asset`` for why they are out
    of scope. Only the paths are kept; the text is read again by the part that parses
    it, so nothing here retains the tree's source for the process lifetime.
    """
    needles = tuple(_SPAWN_ATTRS | _SPAWN_NAMES)
    sizes: list[tuple[int, Path]] = []
    for path, source in iter_candidate_sources(require_any=needles):
        if _is_bundled_skill_asset(path) or _is_container_image_asset(path):
            continue
        sizes.append((len(source), path))
    parts: list[list[Path]] = [[] for _ in range(CENSUS_PARTS)]
    loads = [0] * CENSUS_PARTS
    for size, path in sorted(sizes, key=lambda item: (-item[0], item[1])):
        lightest = loads.index(min(loads))
        parts[lightest].append(path)
        loads[lightest] += size
    return tuple(tuple(sorted(part)) for part in parts)


def _spawn_functions_in_file(path: Path, source: str) -> dict[str, str]:
    """Map ``<relpath>::<func>`` -> the enclosing function's source, for every function
    in one file that contains a subprocess spawn. ``<module>`` marks a module-level
    spawn (no enclosing function), with an empty source.

    One walk of the tree, in ``ast.walk``'s own order, carrying the innermost enclosing
    ``def`` down to each call. A decorator belongs to the scope that holds the ``def``,
    not to the function it decorates; every other child of a ``def`` -- its parameters
    and their defaults, its annotations, its body -- belongs to that function. A lambda
    opens no scope. Names, constants and expression contexts hold no call and are not
    entered: they are most of a module's nodes, and this walk runs over every candidate
    file in the tree.
    """
    tree = ast.parse(source, str(path))
    spawn_bases = _spawn_module_bindings(tree)
    lines = source.splitlines()
    rel = path.relative_to(_SRC_ROOT).as_posix()
    out: dict[str, str] = {}
    pending: deque[tuple[ast.AST, ast.FunctionDef | ast.AsyncFunctionDef | None]] = deque(
        [(tree, None)]
    )
    while pending:
        node, enclosing = pending.popleft()
        if isinstance(node, ast.Call) and _is_spawn_site(node, spawn_bases):
            if enclosing is None:
                out[f"{rel}::<module>"] = ""
            else:
                start, end = enclosing.lineno - 1, enclosing.end_lineno or enclosing.lineno
                out[f"{rel}::{enclosing.name}"] = "\n".join(lines[start:end])
        inner = node if isinstance(node, _FUNCTIONS) else enclosing
        for field in node._fields:
            child = getattr(node, field, None)
            scope = enclosing if field == "decorator_list" else inner
            if isinstance(child, ast.AST):
                if type(child) not in _LEAVES:
                    pending.append((child, scope))
            elif isinstance(child, list):
                pending.extend(
                    (item, scope)
                    for item in child
                    if isinstance(item, ast.AST) and type(item) not in _LEAVES
                )
    return out


def _is_spawn_site(node: ast.Call, spawn_bases: set[str]) -> bool:
    """Whether *node* calls a spawn helper by bare name, or a spawn attribute of a
    receiver the file binds to a spawn-capable module (``subprocess.run``,
    ``sp.Popen``, ``asyncio.create_subprocess_exec``); ``proc.communicate`` and
    ``pool.run`` are not."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _SPAWN_NAMES
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _SPAWN_NAMES:
        return True  # e.g. sandbox.create_subprocess_limited(...)
    if func.attr not in _SPAWN_ATTRS:
        return False
    base = func.value
    base_name = (
        base.id
        if isinstance(base, ast.Name)
        else base.attr if isinstance(base, ast.Attribute) else ""
    )
    return base_name in spawn_bases


@functools.lru_cache(maxsize=CENSUS_PARTS)
def _spawn_functions_in(part: int) -> dict[str, str]:
    """Map ``<relpath>::<func>`` -> the enclosing function's source, for every
    function containing a subprocess spawn in the files of census part *part*.

    Cached: every audit test derives from these scans, so re-scanning per test
    multiplies pure duplicated wall-clock. The source tree cannot change mid-run
    and callers only read the mapping, so a shared instance is safe. The mapping
    holds the spawning functions' source only, never a whole file's.
    """
    out: dict[str, str] = {}
    for path in _spawn_census_parts()[part]:
        out.update(_spawn_functions_in_file(path, path.read_text(encoding="utf-8")))
    return out


def _collect_spawn_functions(part: int | None = None) -> dict[str, str]:
    """The spawn census of part *part*, or of the whole tree: the union of the parts,
    each read on its first use. A key names one function of one file, so the parts
    hold disjoint keys and the union is order-free."""
    parts = range(CENSUS_PARTS) if part is None else (part,)
    out: dict[str, str] = {}
    for index in parts:
        out.update(_spawn_functions_in(index))
    return out


def _collect_unrouted_spawns(part: int | None = None) -> set[str]:
    """Return ``<relpath>::<func>`` for every spawn whose enclosing function
    does NOT reference the sandbox chokepoint, in census part *part* or the whole."""
    return {
        key
        for key, fsrc in _collect_spawn_functions(part).items()
        if not any(tok in fsrc for tok in _ROUTED_TOKENS)
    }


def _collect_routed_spawns_without_preexec(part: int | None = None) -> set[str]:
    """Return ``<relpath>::<func>`` for every sandbox-routed spawn function that
    does NOT also apply the resource-limit ``preexec_fn``, in census part *part* or
    the whole."""
    return {
        key
        for key, fsrc in _collect_spawn_functions(part).items()
        if any(tok in fsrc for tok in _ROUTED_TOKENS)
        and not any(tok in fsrc for tok in _PREEXEC_TOKENS)
    }


# A routed spawn function applies the cgroup v2 DoS ceiling either directly
# (``cgroup_scope_argv``) or via the ``sandboxed_spawn_argv`` chokepoint, which
# wraps every routed argv in the scope internally.
_CGROUP_TOKENS = (
    "cgroup_scope_argv",
    "sandboxed_spawn_argv",
    "_prepare_sandboxed_spawn",
)


def _collect_routed_spawns_without_cgroup(part: int | None = None) -> set[str]:
    """Return ``<relpath>::<func>`` for every sandbox-routed spawn function that
    does NOT also apply the cgroup v2 scope (pids.max / memory.max), in census part
    *part* or the whole."""
    return {
        key
        for key, fsrc in _collect_spawn_functions(part).items()
        if any(tok in fsrc for tok in _ROUTED_TOKENS)
        and not any(tok in fsrc for tok in _CGROUP_TOKENS)
    }


def test_the_scoped_walk_attributes_a_call_the_way_the_line_ranges_do() -> None:
    """A spawn is keyed by the innermost ``def`` whose lines hold it: a decorator's
    spawn belongs to the scope around the ``def``, a default argument's and a nested
    function's to the ``def`` itself, a lambda's to the function holding the lambda,
    a method's to the method, and a module-level one to ``<module>``."""
    source = textwrap.dedent("""
        import subprocess as sp
        subprocess.run(["top"])
        @decorate(sp.run(["deco"]))
        def outer(arg=sp.call(["default"])):
            sp.check_call(["body"])
            handler = lambda: sp.Popen(["lam"])
            def inner():
                return sp.check_output(["nested"])
        class C:
            def method(self):
                return sp.run(["method"])
        """)
    tree = ast.parse(source)
    assert _spawn_module_bindings(tree) == {"subprocess", "asyncio", "sp"}
    path = _SRC_ROOT / "planted.py"
    found = _spawn_functions_in_file(path, source)
    assert set(found) == {
        "planted.py::<module>",
        "planted.py::outer",
        "planted.py::inner",
        "planted.py::method",
    }
    assert found["planted.py::<module>"] == ""
    assert found["planted.py::outer"].startswith("def outer(")
    assert found["planted.py::outer"].endswith('["nested"])')
    assert (
        found["planted.py::inner"].strip()
        == 'def inner():\n        return sp.check_output(["nested"])'
    )
    assert (
        found["planted.py::method"].strip()
        == 'def method(self):\n        return sp.run(["method"])'
    )


@pytest.mark.parametrize("part", range(CENSUS_PARTS))
def test_every_spawn_is_routed_or_allowlisted(part: int):
    """No spawn may be unrouted-and-unlisted (security-review 92e24570 tripwire). One
    census part per test keeps each test's work a bounded share of the tree; a new
    unrouted spawn is visible in the part that holds its file."""
    unrouted = _collect_unrouted_spawns(part)
    unexpected = unrouted - BENIGN_SPAWNS
    assert not unexpected, (
        "New unrouted subprocess spawn(s) found in src/kiro_crew:\n  "
        + "\n  ".join(sorted(unexpected))
        + "\n\nRoute agent-influenced spawns through "
        "kiro_crew.sandbox.sandboxed_spawn_argv (OS sandbox + scrubbed env), "
        "or, if the command/args/cwd are NOT agent-influenced, add the "
        "file::function key to BENIGN_SPAWNS in this test with a justification. "
        "See security-review finding 92e24570."
    )


def test_prerequisite_async_adapter_keeps_sandbox_chokepoint():
    """The off-loop prerequisite adapter must remain a thin sandbox wrapper.

    The off-loop hop itself now lives one level down, in
    ``sandbox.shielded_prepare_off_loop`` (the single owner of the
    shield-and-recover pattern), so this pins both halves where they actually
    live: the adapter must still name the chokepoint and route through that
    owner, and the owner must still take the work off the loop.
    """

    path = _SRC_ROOT / "kiro_prerequisite_process.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, str(path))
    adapter = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_prepare_sandboxed_spawn"
    )
    adapter_source = ast.get_source_segment(source, adapter) or ""
    assert "shielded_prepare_off_loop" in adapter_source
    assert "sandboxed_spawn_argv" in adapter_source

    sandbox_path = _SRC_ROOT / "sandbox_spawn.py"
    sandbox_source = sandbox_path.read_text(encoding="utf-8")
    sandbox_tree = ast.parse(sandbox_source, str(sandbox_path))
    owner = next(
        node
        for node in ast.walk(sandbox_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "shielded_prepare_off_loop"
    )
    owner_source = ast.get_source_segment(sandbox_source, owner) or ""
    assert "asyncio.to_thread" in owner_source or "run_in_executor" in owner_source


def test_benign_allowlist_has_no_stale_entries():
    """Every BENIGN_SPAWNS entry must still name a real unrouted spawn, so the
    allowlist cannot silently accumulate dead exemptions (e.g. after a spawn is
    later routed through the chokepoint). A stale entry is visible only to the whole
    census, read here from the parts' caches."""
    unrouted = _collect_unrouted_spawns()
    stale = BENIGN_SPAWNS - unrouted
    assert not stale, (
        "Stale BENIGN_SPAWNS entries (no longer an unrouted spawn — remove "
        "them or they mask future regressions):\n  " + "\n  ".join(sorted(stale))
    )


def test_agent_influenced_sites_are_routed():
    """Agent-influenced spawns must stay routed through the sandbox."""
    unrouted = _collect_unrouted_spawns()
    for key in (
        "mcp_discovery_probe.py::probe_server",
        "task_executor.py::run_tests",
        "git_coord.py::_git",
        "git_coord.py::_is_git_repo",
        "dashboard/source_providers/runner.py::_run_provider",
    ):
        assert key not in unrouted, (
            f"{key} must route its spawn through sandboxed_spawn_argv "
            "(security-review 92e24570) but is no longer sandbox-wrapped."
        )


@pytest.mark.parametrize("part", range(CENSUS_PARTS))
def test_every_routed_spawn_applies_resource_limits(part: int):
    """Every sandbox-routed spawn must ALSO cap the child's resources, checked one
    census part per test.

    The sandbox chokepoint gives a child filesystem + credential isolation; a
    ``preexec_fn`` from ``resource_limit_preexec()`` gives it a kernel-enforced
    ceiling (RLIMIT_NPROC/NOFILE/CPU/AS) so a fork bomb or runaway allocation in
    a compromised tool / MCP server cannot exhaust the host. This is the
    regression tripwire for security-review bdf0d7e5: the helper was merged
    once as dead code (defined, zero callers). If you add a new agent-influenced
    spawn, pass ``preexec_fn=resource_limit_preexec()`` — or, if the spawn is a
    fixed-argv internal probe with no agent-influenced child, add its
    ``file::function`` key to ``PREEXEC_EXEMPT`` with a justification.
    """
    missing = _collect_routed_spawns_without_preexec(part) - PREEXEC_EXEMPT
    assert not missing, (
        "Sandbox-routed spawn(s) missing a resource-limit preexec_fn:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nPass preexec_fn=kiro_crew.sandbox.resource_limit_preexec() to the "
        "spawn (kernel RLIMIT ceiling — fork bomb / FD / mem / CPU), or add the "
        "file::function key to PREEXEC_EXEMPT with a justification. "
        "See security-review finding bdf0d7e5."
    )


def test_preexec_exempt_has_no_stale_entries():
    """Every PREEXEC_EXEMPT entry must still name a routed spawn function that
    lacks the preexec token, so the exemption list cannot accumulate dead
    entries that would mask a future regression. Read from the whole census."""
    routed_missing = _collect_routed_spawns_without_preexec()
    stale = PREEXEC_EXEMPT - routed_missing
    assert not stale, (
        "Stale PREEXEC_EXEMPT entries (no longer a routed spawn lacking the "
        "preexec token — remove them):\n  " + "\n  ".join(sorted(stale))
    )


@pytest.mark.parametrize("part", range(CENSUS_PARTS))
def test_every_routed_spawn_applies_cgroup_scope(part: int):
    """Every sandbox-routed spawn must ALSO be placed in a cgroup v2 scope, checked
    one census part per test.

    The RLIMIT preexec caps a single process's FDs; the cgroup scope
    (``cgroup_scope_argv`` → pids.max + memory.max) is the actual default-on
    fork-bomb + memory-DoS ceiling the finding's headline threats require
    (security-review bdf0d7e5). A function satisfies this by calling ``cgroup_scope_argv``
    directly or by routing through ``sandboxed_spawn_argv`` (which applies the
    scope internally). The ``PREEXEC_EXEMPT`` fixed-argv internal probes are
    also exempt here — same rationale (no agent-influenced child to bound).
    """
    missing = _collect_routed_spawns_without_cgroup(part) - PREEXEC_EXEMPT
    assert not missing, (
        "Sandbox-routed spawn(s) missing a cgroup v2 scope:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nWrap the final argv with kiro_crew.sandbox.cgroup_scope_argv() "
        "(pids.max + memory.max fork-bomb / memory-DoS ceiling), or route the "
        "spawn through sandboxed_spawn_argv which applies it. "
        "See security-review finding bdf0d7e5."
    )


def test_the_census_reads_enough_of_the_tree() -> None:
    """The narrowed read still parses a real share of the package, and the census
    saw enough spawn sites to have measured anything: a prefilter that dropped the
    tree, or a walk that stopped recording, would otherwise pass every audit green.
    In file order this follows the per-part audits and reads their cached parts."""
    parts = _spawn_census_parts()
    assert sum(len(part) for part in parts) > 500, "the census parses too few files"
    assert all(parts), "an empty census part parses nothing and so cannot fail"
    assert len(_collect_spawn_functions()) > 100, "the census saw too few spawn sites"


@functools.lru_cache(maxsize=1)
def _import_sites() -> tuple[tuple[Path, str, tuple[tuple[int, str], ...]], ...]:
    """``(path, relpath, ((line, module), ...))`` per file in the package, listing every
    absolute import: each name of an ``import a.b`` and the module of a ``from a.b
    import x``; a relative import names nothing this audit lists. One parse of the
    tree, read from statements only (an import is a statement), shared by the two
    asset-import tests below so the tree is parsed once per run rather than once per
    test; a file that does not parse is a hole in both tests' coverage and raises
    rather than being skipped."""
    files: list[tuple[Path, str, tuple[tuple[int, str], ...]]] = []
    for path, _source, tree in parsed_candidates(skip_syntax_errors=False):
        sites: list[tuple[int, str]] = []
        for node in iter_statements(tree):
            if isinstance(node, ast.Import):
                sites.extend((node.lineno, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                sites.append((node.lineno, node.module))
        files.append((path, path.relative_to(_SRC_ROOT).as_posix(), tuple(sites)))
    return tuple(files)


def _asset_imports(assets: list[Path], exempt: Callable[[Path], bool]) -> list[str]:
    """``<relpath>:<line> imports <module>`` for every import, outside the files *exempt*
    names, of one of *assets* or of a package holding one."""
    asset_modules = {
        "kiro_crew." + p.relative_to(_SRC_ROOT).with_suffix("").as_posix().replace("/", ".")
        for p in assets
    }
    asset_packages = {
        "kiro_crew." + p.relative_to(_SRC_ROOT).parent.as_posix().replace("/", ".") for p in assets
    }
    return [
        f"{rel}:{line} imports {name}"
        for path, rel, sites in _import_sites()
        if not exempt(path)
        for line, name in sites
        if name in asset_modules or name in asset_packages
    ]


def test_bundled_skill_assets_are_not_imported():
    """The skill-asset exemption is only honest while the gateway never imports one.

    ``_is_bundled_skill_asset`` takes a skill's helper scripts out of the spawn
    audit on the premise that they are scripts an agent runs in a shell, not
    code this package runs. That premise has one failure mode: someone imports
    such a script as a module, and its unrouted spawns become gateway spawns
    while staying invisible to the audit. This test forbids that.

    The sibling claim -- that the gateway never EXECS one either -- needs no
    test: a gateway function spawning ``narrate.py`` would itself be a spawn
    site in a non-exempt file, so the audit reviews it there.

    Reading these files as DATA is expected and not what this pins: the skills
    loader lists and reads skill directories, which is the whole point of
    shipping them.
    """
    assets = [p for p in _SRC_ROOT.rglob("*.py") if _is_bundled_skill_asset(p)]
    # Non-vacuity: a predicate matching nothing would make this pass while
    # pinning nothing, and would mean the exemption itself is dead.
    assert assets, "no bundled skill assets found -- the exemption matches nothing"
    app_bundled = [p for p in assets if "builtin_skills" not in p.relative_to(_SRC_ROOT).parts]
    assert app_bundled, "app-bundled skill assets (apps/builtins/*/skills/**) not matched"

    offenders = _asset_imports(assets, _is_bundled_skill_asset)
    assert not offenders, (
        "Gateway code imports a bundled skill asset, which the spawn audit "
        "exempts:\n  " + "\n  ".join(sorted(offenders)) + "\n\nEither move the "
        "shared logic into a real module under src/kiro_crew (where the spawn "
        "audit reviews it), or drop the import."
    )


def test_container_image_assets_are_not_imported():
    """The container-image exemption is only honest while the gateway never imports one.

    ``_is_container_image_asset`` takes a Linux image's own source out of the spawn audit
    on the premise that the image runs it, not this package. That premise has one failure
    mode: gateway code imports one of those modules, and its unrouted spawns become
    gateway spawns while staying invisible to the audit.

    The tree also cannot be imported in practice: it assumes an installed layout where
    ``kiro_crew`` is absent, so an import would fail at runtime rather than quietly work.
    This test makes the premise explicit instead of leaving it to that accident.

    Reading these files as DATA is expected: the image build copies the tree, and tests in
    the tree's own suite compare its source text against the gateway's copy of a duplicated
    helper. Neither is an import.
    """
    assets = [p for p in _SRC_ROOT.rglob("*.py") if _is_container_image_asset(p)]
    # Non-vacuity: a predicate matching nothing would make this pass while pinning
    # nothing, and would mean the exemption itself is dead.
    assert assets, "no container image assets found -- the exemption matches nothing"

    offenders = _asset_imports(assets, _is_container_image_asset)
    assert not offenders, (
        "Gateway code imports a container image asset, which the spawn audit "
        "exempts:\n  " + "\n  ".join(sorted(offenders)) + "\n\nThat tree runs inside a "
        "Linux image on an installed copy where kiro_crew is absent, so the import would "
        "fail there anyway. Move the shared logic into a real module under src/kiro_crew, "
        "or duplicate it deliberately and pin the copies equal by source text."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Gateway spawn timeout discipline
#
# `slack/gateway.py` spawns children on the boot and auto-update paths. The
# discipline for a spawn that can time out: own process group
# (`start_new_session` on POSIX), tree-kill + bounded reap on TimeoutError AND
# on CancelledError. These two ratchets make the discipline structural: the
# NEXT spawn added to the file cannot silently regress to an
# abandoned-child-on-timeout, because the audit below fails until it carries
# the same treatment.
# ═══════════════════════════════════════════════════════════════════════════

_GATEWAY_REL = "slack/gateway.py + slack/gateway_runtime/*.py"

#: Functions that own the kill-the-tree + bounded-reap contract. The shared
#: helper is `platform_compat.kill_and_reap` (`_kill_and_reap` is its delegate
#: in `platform/update_provider.py`); the two `_startup_child` methods are the
#: boot-path equivalent (kill and reap split in two).
_TREE_KILL_FUNCS = frozenset({"kill_and_reap", "_kill_and_reap", "_kill_startup_child"})


@functools.cache
def _gateway_tree() -> ast.Module:
    facade = _SRC_ROOT / "slack" / "gateway.py"
    paths = (facade, *sorted((_SRC_ROOT / "slack" / "gateway_runtime").glob("*.py")))
    return ast.parse("\n".join(p.read_text(encoding="utf-8") for p in paths), str(facade))


def _is_spawn_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("create_subprocess_")
    )


def _handler_catches(handler: ast.ExceptHandler, name: str) -> bool:
    """Whether *handler* names *name* (bare or attribute-qualified) explicitly.

    A blanket ``except Exception`` deliberately does NOT count: the discipline
    requires the timeout/cancel arm to be explicit so the kill is visibly tied
    to the abandonment hazard, and `_auto_apply_update`'s outer
    ``except Exception`` (which does not kill) must not satisfy this audit.
    """
    types = handler.type
    if types is None:
        return False
    parts = types.elts if isinstance(types, ast.Tuple) else [types]
    for part in parts:
        if isinstance(part, ast.Name) and part.id == name:
            return True
        if isinstance(part, ast.Attribute) and part.attr == name:
            return True
    return False


def _handler_kills(handler: ast.ExceptHandler, proc_name: str) -> bool:
    """Whether *handler*'s body calls a tree-kill helper on *proc_name*."""
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = (
            fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
        )
        if fname not in _TREE_KILL_FUNCS:
            continue
        if any(isinstance(a, ast.Name) and a.id == proc_name for a in node.args):
            return True
    return False


def test_gateway_spawns_all_own_session():
    """Every subprocess spawn in slack/gateway.py starts its own session.

    `proc.kill()` signals only the direct child; without
    ``start_new_session`` the process-group tree kill in the timeout/cancel
    arms has no group of its own to address, so grandchildren survive the
    kill and keep running.
    """
    missing: list[str] = []
    spawns = 0
    for node in ast.walk(_gateway_tree()):
        if not _is_spawn_call(node):
            continue
        spawns += 1
        if not any(kw.arg == "start_new_session" for kw in node.keywords):
            missing.append(f"{_GATEWAY_REL}:{node.lineno}")
    assert spawns >= 9, (
        f"only {spawns} spawn sites found in {_GATEWAY_REL} — the audit's "
        "spawn matcher no longer matches the file's spawn idiom; fix the "
        "matcher rather than deleting the ratchet. (The floor dropped from 10 "
        "to 9 when the unattended apply's inline fetch moved to the shared "
        "update_fetch.fetch_and_pin_upstream helper, which carries the same "
        "start_new_session discipline and is covered by the spawn allowlist.)"
    )
    assert not missing, (
        "Spawn(s) in slack/gateway.py without start_new_session — a timeout "
        "kill would reach only the direct child, abandoning its descendants "
        "(issue #4210):\n  " + "\n  ".join(missing) + "\n\nAdd "
        "start_new_session=platform_compat.IS_POSIX to the spawn."
    )


def test_gateway_proc_waits_all_kill_on_timeout_and_cancel():
    """Every ``wait_for(<proc>.communicate()|.wait())`` in slack/gateway.py
    sits under explicit TimeoutError AND CancelledError arms that tree-kill
    that proc.

    Without the arm, the child is ABANDONED on timeout — the exception
    propagates (or is swallowed) while the process keeps running with no
    supervisor, which on the auto-update path means a `git reset` or
    `kiro-cli update` still mutating the installation.
    """
    offenders: list[str] = []
    audited = 0
    for func in ast.walk(_gateway_tree()):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Names assigned from a spawn IN THIS function. A proc received as a
        # parameter (the reap helpers) is that caller's site, audited there.
        proc_names = {
            t.id
            for stmt in ast.walk(func)
            if isinstance(stmt, ast.Assign)
            and isinstance(stmt.value, ast.Await)
            and _is_spawn_call(stmt.value.value)
            for t in stmt.targets
            if isinstance(t, ast.Name)
        }
        if not proc_names:
            continue
        # Map each wait_for-on-a-proc to the Trys enclosing it in their body.
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(func):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(func):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "wait_for"
                and node.args
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Attribute)
                and node.args[0].func.attr in ("communicate", "wait")
                and isinstance(node.args[0].func.value, ast.Name)
                and node.args[0].func.value.id in proc_names
            ):
                continue
            audited += 1
            proc_name = node.args[0].func.value.id
            timeout_ok = cancel_ok = False
            cursor: ast.AST | None = node
            while cursor is not None and cursor is not func:
                parent = parents.get(cursor)
                if isinstance(parent, ast.Try) and cursor in ast.walk(parent):
                    # Only count Trys where the wait sits in the BODY (an
                    # already-handling arm re-waiting is the reap, not a site).
                    in_body = any(
                        cursor is stmt or cursor in ast.walk(stmt) for stmt in parent.body
                    )
                    if in_body:
                        for handler in parent.handlers:
                            if _handler_kills(handler, proc_name):
                                if _handler_catches(handler, "TimeoutError"):
                                    timeout_ok = True
                                if _handler_catches(handler, "CancelledError"):
                                    cancel_ok = True
                cursor = parent
            if not (timeout_ok and cancel_ok):
                lacking = " and ".join(
                    what
                    for what, ok in (("TimeoutError", timeout_ok), ("CancelledError", cancel_ok))
                    if not ok
                )
                offenders.append(
                    f"{_GATEWAY_REL}:{node.lineno} ({func.name}: {proc_name}) lacks a "
                    f"{lacking} arm that tree-kills the proc"
                )
    assert audited >= 10, (
        f"only {audited} proc wait sites found in {_GATEWAY_REL} — the audit's "
        "wait matcher no longer matches the file's idiom; fix the matcher "
        "rather than deleting the ratchet"
    )
    assert not offenders, (
        "wait_for on a spawned proc without kill+reap discipline (issue "
        "#4210):\n  " + "\n  ".join(offenders) + "\n\nWrap the wait in explicit "
        "TimeoutError and CancelledError arms that call _kill_and_reap (or the "
        "startup-child kill/reap pair) on the proc before returning/re-raising."
    )


def test_harness_preflight_seed_is_fixed_argv():
    """The preflight exemption covers one fixed seed call, not dynamic MCP."""
    source = (_SRC_ROOT / "testing" / "harness.py").read_text(encoding="utf-8")
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "spawn_feature_gateway"
    )
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr in _SPAWN_ATTRS
    ]
    assert len(calls) == 1
    expected = ast.parse(
        "subprocess.run(platform_compat.isolated_python_argv('-c', "
        "'from kiro_crew.seed import seed; import sys; seed(sys.argv[1])', fixture, "
        "force_isolation=True), "
        "cwd=str(spawn_cwd), env=env, capture_output=True, text=True, "
        "encoding='utf-8', errors='replace', timeout=30)",
        mode="eval",
    ).body
    assert ast.dump(calls[0]) == ast.dump(expected)
    assert "testing/harness.py::_launch_gateway" in BENIGN_SPAWNS
    assert "testing/harness.py::spawn_feature_gateway" in BENIGN_SPAWNS
