"""Doctor check: agent specs whose command/args/env paths no longer exist.

Agent spec JSONs in the shared kiro agents dir rot. A spec's ``mcpServers``
entry can point at paths that stop existing — a removed venv, a reaped
temporary checkout, a relocated tool. The symptom is silent and expensive:
every new session selecting that agent starts with its first-party MCP servers
dead (spawn fails on a nonexistent command), or resolves secrets against a dead
data home and surfaces ``internal_auth_mismatch``. Nothing names the dead path.

This module walks every agent spec JSON in :func:`kiro_agents_dir_path` and
stats every absolute ``command`` path, absolute path-like arg, and absolute
path-like env value. Values that only LOOK like paths are screened out first —
separator-joined lists, and the operands of flags that take an opaque identifier
(see :data:`_IDENTIFIER_FLAGS`) — because a false "dead path" on a healthy
install turns the whole check red and teaches operators to ignore it:

* **Managed specs** (the ones ``install_agent`` / ``rebuild_agent_config`` own,
  i.e. :data:`~kiro_crew.agent_files.OWNED_KIRO_AGENT_FILES`) with dead paths are
  repaired automatically via the existing rebuild path, then re-verified.
* **Foreign specs** (other tools' agents sharing the directory) are report-only,
  naming the spec file, the server name, and the dead path. Kiro Crew never
  rewrites what it does not own.
* **Malformed / unreadable spec JSON** is reported, never crashing the whole
  check (fail-open per file).

Kept deliberately self-contained — one module, one public entry point
(:func:`check_dead_paths`) plus a thin doctor renderer
(:func:`doctor_dead_paths`) — so the doctor wiring is a single call and a
sibling change wiring another sweep into doctor rebases trivially.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
from kiro_crew.config.paths import kiro_agents_dir

logger = logging.getLogger(__name__)

# Import-time override hook mirroring ``agent.KIRO_AGENTS_DIR`` /
# ``cli_doctor.KIRO_AGENTS_DIR``: ``None`` means "resolve from the live data
# home"; tests patch this attribute directly. Never captured at import — a
# frozen path would defeat KIRO_HOME resolution and test isolation.
KIRO_AGENTS_DIR: Path | None = None


def kiro_agents_dir_path() -> Path:
    """Kiro agents directory, honoring the override hook, else the live home.

    Resolved live via :func:`kiro_crew.config.paths.kiro_agents_dir`, which
    honors ``KIRO_HOME``. ``config.paths`` is a stdlib-only leaf module, so it
    is imported at module scope (no config-plane import cost, no cycle).
    """
    if KIRO_AGENTS_DIR is not None:
        return KIRO_AGENTS_DIR
    return kiro_agents_dir()


@dataclass
class DeadPath:
    """One dead absolute path found inside an agent spec.

    ``where`` names the location within the spec (e.g. ``command``, ``args[0]``,
    ``env[KIROCREW_HOME]``) so a report points at exactly what to fix.
    """

    spec: str  # spec filename (e.g. "kirocrew.json")
    server: str  # mcpServers entry name, or "" when outside mcpServers
    where: str  # location within the entry (command / args[i] / env[KEY])
    path: str  # the dead absolute path


@dataclass
class SpecResult:
    """Per-spec outcome of the dead-path walk."""

    spec: str
    managed: bool
    dead: list[DeadPath] = field(default_factory=list)
    # Set when the file could not be read or parsed as a JSON object. The walk
    # is fail-open per file: an unreadable spec is reported, never fatal.
    unreadable: str | None = None
    # Set on a managed spec after a repair attempt: True if the rebuild cleared
    # every dead path, False if any survived. None when no repair was attempted.
    repaired: bool | None = None


@dataclass
class DeadPathReport:
    """Aggregate result across every spec in the agents dir."""

    results: list[SpecResult] = field(default_factory=list)

    @property
    def foreign_dead(self) -> list[SpecResult]:
        return [r for r in self.results if not r.managed and r.dead]

    @property
    def managed_dead(self) -> list[SpecResult]:
        return [r for r in self.results if r.managed and r.dead]

    @property
    def unreadable(self) -> list[SpecResult]:
        return [r for r in self.results if r.unreadable]

    @property
    def repair_failed(self) -> list[SpecResult]:
        return [r for r in self.results if r.managed and r.repaired is False]

    @property
    def has_findings(self) -> bool:
        """Whether anything worth surfacing was found.

        A managed spec that was repaired AND re-verified clean is NOT a finding
        — the repair is the whole point, and reporting a self-healed spec as a
        problem would make doctor exit nonzero on a state it just fixed.
        """
        return bool(self.foreign_dead or self.unreadable or self.repair_failed)


def _sanitize_for_terminal(value: str) -> str:
    """Neutralize control characters in an untrusted, spec-derived string.

    Spec files (foreign ones especially) are untrusted content. Their strings —
    server names, paths, and the raw JSON error text that lands in the
    ``unreadable`` reason — are printed to the operator's terminal by ``kirocrew
    doctor``. A path or server name carrying ANSI/OSC escape bytes would let a
    hostile spec drive the terminal (retitle the window, rewrite earlier output,
    inject a pasteable command) the moment doctor renders it. Replace every C0
    control (except tab) and the C1/DEL range with a visible ``\\xNN`` token so
    the value is still readable but inert. ESC in particular can no longer open
    a control sequence.
    """
    out: list[str] = []
    for ch in value:
        codepoint = ord(ch)
        if ch == "\t" or (0x20 <= codepoint <= 0x7E) or codepoint >= 0xA0:
            out.append(ch)
        else:
            out.append(f"\\x{codepoint:02x}")
    return "".join(out)


def _colon_scan_rejects(value: str) -> bool:
    """Whether *value* looks like a POSIX ``PATH``-style colon-joined list.

    A colon flanked by a path separator (``/``), or a trailing colon, is the
    list-separator shape (``/usr/bin:/bin``). A lone drive-letter colon
    (``C:\\Users\\...``) has no adjacent ``/`` and is NOT rejected here, so a
    real Windows path is left alone. Split out from
    :func:`_looks_like_single_absolute_path` so the list-detection can be tested
    without depending on the platform-specific ``os.path.isabs``.
    """
    if "/" not in value:
        return False
    for i, ch in enumerate(value):
        if ch != ":":
            continue
        if i == 1 and value[0].isalpha() and i + 1 < len(value) and value[i + 1] in ("/", "\\"):
            # Drive-letter colon (``C:/x`` or ``C:\x``) — a Windows path, not
            # a list separator, even though a ``/`` follows it.
            continue
        before = value[i - 1] if i > 0 else ""
        after = value[i + 1] if i + 1 < len(value) else ""
        if before == "/" or after == "/" or after == "":
            return True
    return False


def _looks_like_single_absolute_path(value: str) -> bool:
    """Whether *value* looks like exactly ONE absolute path.

    Colon-joined values like ``PATH`` (``/usr/bin:/bin``) are NOT single paths
    and must not be stat-ed as one — that is the explicit false-positive to
    avoid. The Windows list separator ``;`` is rejected for the same reason, as
    is the comma separator that multi-value CLI flags conventionally use
    (``--search-dirs /opt/a,/opt/b``). A Windows drive path
    (``C:\\Users\\...``) legitimately contains a colon, so the colon test is
    scoped to the POSIX list-separator shape (see
    :func:`_colon_scan_rejects`), leaving a bare drive-letter colon alone.

    Only absolute paths are considered: a bare token, a URL, a flag, or a
    relative value is not something whose absence on THIS host is meaningful.
    """
    if not value or not os.path.isabs(value):
        return False
    # Windows PATH-style list — never a single path.
    if ";" in value:
        return False
    # Comma-joined list — the separator multi-value CLI flags conventionally
    # take. A comma is legal in a POSIX filename, so this trades a rare false
    # negative (a real path containing a comma stops being checked) for
    # removing a guaranteed false positive on every list-valued arg — the same
    # blanket trade already made for ``;`` above. (``:`` is screened in scoped
    # form instead, so a Windows drive path survives it.)
    if "," in value:
        return False
    # POSIX PATH-style list.
    if _colon_scan_rejects(value):
        return False
    # A newline-joined blob is not one path either.
    if "\n" in value:
        return False
    return True


def _path_is_dead(value: str) -> bool:
    """Whether an absolute path no longer exists on disk (a stat check).

    ``os.path.exists`` follows symlinks, so a live symlink to a live target
    reads present and a dangling one reads dead — both correct for "would this
    command/arg/env still resolve".
    """
    try:
        return not os.path.exists(value)
    except OSError:
        # A path so malformed the OS refuses to stat it is, for our purpose,
        # unusable — report it as dead rather than crashing the walk.
        return True


#: Env KEY substrings that mark an entry's VALUE as potentially secret.
#: Display-redaction only, biased toward redacting: the locator (``env[KEY]``)
#: still names the entry, so a redacted report stays fully actionable, while a
#: missed redaction prints a live secret into terminal output that routinely
#: gets pasted into bug reports. A slash-prefixed token value can pass the
#: single-absolute-path shape test, so the VALUE shape alone cannot be trusted.
_CREDENTIAL_KEY_MARKERS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "APIKEY",
    "API_KEY",
    "AUTH",
    "PRIVATE",
)

#: Flags whose OPERAND is an opaque identifier, never a filesystem path.
#: Some namespace and scope identifiers are conventionally slash-prefixed
#: (``--scope /spaces/ns_abc123``), which makes them absolute-path-SHAPED while
#: being unresolvable on any host by design. That is indistinguishable from a
#: genuinely removed directory by value alone: such an operand is absolute, is a
#: single value, and does not exist. The only signal that separates them is
#: POSITIONAL — which flag the value is the operand of — so the skip has to read
#: the preceding argument rather than the value.
#:
#: Deliberately a short, literal set rather than a heuristic. A heuristic here
#: would trade a guaranteed false positive for an unbounded false NEGATIVE, and a
#: dead-path check that silently stops reporting real dead paths is worse than one
#: that over-reports.
_IDENTIFIER_FLAGS: frozenset[str] = frozenset({"--scope", "--namespace", "--space"})

_REDACTED_VALUE = "<redacted: credential-shaped key>"


def _credential_shaped_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _CREDENTIAL_KEY_MARKERS)


def _is_identifier_operand(args: list, i: int) -> bool:
    """Whether ``args[i]`` is the operand of an identifier-taking flag.

    Read BEFORE the value is tested, so the operand is never stat-ed. Deciding it
    afterwards reaches the same report but performs the filesystem probe the
    module docstring promises not to perform.
    """
    if i == 0:
        return False
    prev = args[i - 1]
    return isinstance(prev, str) and prev in _IDENTIFIER_FLAGS


def _walk_server_paths(server: str, entry: dict) -> list[DeadPath]:
    """Collect dead absolute paths from one ``mcpServers`` entry."""
    dead: list[DeadPath] = []

    command = entry.get("command")
    if isinstance(command, str) and os.path.isabs(command) and _path_is_dead(command):
        dead.append(DeadPath(spec="", server=server, where="command", path=command))

    args = entry.get("args")
    if isinstance(args, list):
        for i, arg in enumerate(args):
            if (
                isinstance(arg, str)
                and not _is_identifier_operand(args, i)
                and _looks_like_single_absolute_path(arg)
                and _path_is_dead(arg)
            ):
                dead.append(DeadPath(spec="", server=server, where=f"args[{i}]", path=arg))

    env = entry.get("env")
    if isinstance(env, dict):
        for key, val in env.items():
            if (
                isinstance(val, str)
                and _looks_like_single_absolute_path(val)
                and _path_is_dead(val)
            ):
                # Redacted at RECORD CREATION, not at print time, so every
                # consumer (section prints, the issues summary doctor joins
                # into its final line) is safe by construction. The re-verify
                # pass only checks presence, never compares path text.
                shown = _REDACTED_VALUE if _credential_shaped_key(str(key)) else val
                dead.append(DeadPath(spec="", server=server, where=f"env[{key}]", path=shown))

    return dead


def _walk_spec(spec_path: Path) -> tuple[list[DeadPath], str | None]:
    """Read one spec file and collect its dead paths (fail-open per file).

    Returns ``(dead_paths, unreadable_reason)``. An unreadable / non-object /
    malformed spec yields ``([], reason)`` rather than raising, so one bad file
    never aborts the whole check.
    """
    try:
        raw = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"unreadable ({exc.strerror or exc})"
    except UnicodeError as exc:
        # A non-UTF-8 / binary file dropped into the agents dir decodes with a
        # UnicodeDecodeError (a UnicodeError, NOT an OSError) — catch it here so
        # one such file is reported as unreadable rather than aborting the whole
        # walk, keeping the check fail-open per file.
        return [], f"not valid UTF-8 ({exc})"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [], f"malformed JSON ({exc})"
    if not isinstance(data, dict):
        return [], "top-level JSON is not an object"

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return [], None

    dead: list[DeadPath] = []
    for server, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        for dp in _walk_server_paths(str(server), entry):
            dp.spec = spec_path.name
            dead.append(dp)
    return dead, None


def _default_repair() -> None:
    """Repair managed specs by rewriting them from the current install.

    Delegates to the existing authoritative rebuild path
    (:func:`kiro_crew.agent.rebuild_agent_config`), which re-resolves every
    managed server's command/args/env against this install and drops any server
    whose command no longer resolves. Imported lazily to keep this module's
    import graph light and to avoid an import cycle (``agent`` imports the config
    plane this module also reaches).
    """
    from kiro_crew.agent import rebuild_agent_config

    rebuild_agent_config()


def _report_only() -> None:
    """No-op repair: the pass records findings without rewriting anything."""


def check_dead_paths(*, agents_dir: Path | None = None, repair=_default_repair) -> DeadPathReport:
    """Walk every agent spec and report (and repair managed) dead paths.

    Args:
        agents_dir: The agents directory to scan. Defaults to
            :func:`kiro_agents_dir_path`. Callers that resolve the agents dir
            themselves (notably ``kirocrew doctor``, which has its OWN override
            hook) MUST pass their resolved directory so the scan and any repair
            operate on the SAME directory the caller is inspecting — otherwise
            the scan could stat the live ``~/.kiro/agents`` while the caller is
            pointed elsewhere, and a dead managed path would trigger a rebuild
            against specs the caller never meant to touch.
        repair: Zero-arg callable invoked once, only when a managed spec has a
            dead path, to rewrite the managed specs from the current install.
            Defaults to :func:`_default_repair`. Injectable so tests can drive
            the repair + re-verify contract without a full rebuild, and so a
            caller can pass a no-op to get a report-only pass.

    Returns:
        A :class:`DeadPathReport`. Managed specs that had dead paths are
        re-walked after the repair; ``SpecResult.repaired`` records whether the
        rebuild cleared them.
    """
    report = DeadPathReport()
    ambient = kiro_agents_dir_path()
    if agents_dir is None:
        agents_dir = ambient
    elif repair is _default_repair and agents_dir.resolve() != ambient.resolve():
        # The caller redirected the SCAN but kept the default repair, and the
        # default repair (rebuild_agent_config) writes wherever the AMBIENT
        # environment resolves -- not the scanned directory. Running it here
        # would judge specs in one directory and rewrite specs in another
        # (e.g. a test scanning a fixture dir triggering a rebuild of the
        # operator's real specs). Degrade to report-only; a caller that wants
        # repair on a redirected directory must inject a repair bound to it.
        repair = _report_only
    if not agents_dir.is_dir():
        return report

    managed_names = set(OWNED_KIRO_AGENT_FILES)
    managed_needs_repair = False

    for spec_path in sorted(agents_dir.glob("*.json")):
        managed = spec_path.name in managed_names
        dead, unreadable = _walk_spec(spec_path)
        result = SpecResult(spec=spec_path.name, managed=managed, dead=dead, unreadable=unreadable)
        report.results.append(result)
        if managed and dead:
            managed_needs_repair = True

    if not managed_needs_repair:
        return report

    # Repair once for the whole managed set (rebuild rewrites every managed
    # spec), then re-verify each managed spec that had dead paths.
    try:
        repair()
    except Exception:
        logger.warning("dead-path repair via rebuild failed", exc_info=True)
        # Leave repaired=None on the affected specs — the re-verify below still
        # runs and will record whether anything changed on disk regardless.

    for result in report.results:
        if not (result.managed and result.dead):
            continue
        dead_after, unreadable_after = _walk_spec(agents_dir / result.spec)
        result.repaired = not dead_after and unreadable_after is None
        if not result.repaired:
            # Surface what survived so a stuck repair is still diagnosable.
            result.dead = dead_after
            result.unreadable = unreadable_after

    return report


def doctor_dead_paths(issues: list[str], *, agents_dir: Path | None = None) -> None:
    """Render the ``Agent Spec Paths`` section of ``kirocrew doctor``.

    Managed specs with dead paths are repaired in place; a repaired-and-clean
    spec is reported as auto-fixed and is NOT counted as an issue. Foreign specs
    are report-only (naming the spec, server, and dead path). Malformed /
    unreadable specs are reported. Anything that is a real, unresolved finding —
    a foreign dead path, an unreadable spec, or a repair that did not take — is
    appended to *issues* so ``kirocrew doctor`` exits nonzero and the failure
    class is diagnosable in one step.

    Args:
        issues: doctor's exit-code channel; real findings are appended here.
        agents_dir: The agents directory doctor is inspecting. Passed straight
            through to :func:`check_dead_paths` so the scan (and any repair) run
            against the SAME directory the rest of doctor uses, honoring doctor's
            own agent-dir override rather than re-resolving the live home. When
            ``None`` the check resolves the live agents dir itself.

    Best-effort: a failure inside the walk must not abort the whole doctor run,
    whose job is to diagnose exactly this kind of breakage.
    """
    print("\nAgent Spec Paths")
    try:
        report = check_dead_paths(agents_dir=agents_dir)
    except Exception as exc:  # noqa: BLE001 — doctor must survive a broken walk
        print(f"  paths:       ⚠️  could not check ({exc})")
        return

    if not report.results:
        print("  paths:       ⏹ no agent specs found")
        return

    for result in report.managed_dead:
        spec = _sanitize_for_terminal(result.spec)
        if result.repaired:
            print(f"  {spec}: ✅ dead paths repaired from the current install")
        else:
            print(f"  {spec}: ❌ managed spec has dead paths the rebuild did not clear")
            for dp in result.dead:
                loc = f"{dp.server}.{dp.where}" if dp.server else dp.where
                print(f"      {_sanitize_for_terminal(loc)}: {_sanitize_for_terminal(dp.path)}")
            if result.unreadable:
                print(f"      (re-read failed: {_sanitize_for_terminal(result.unreadable)})")
            issues.append(f"agent spec dead paths: {spec}")

    for result in report.foreign_dead:
        spec = _sanitize_for_terminal(result.spec)
        print(f"  {spec}: ⚠️  foreign spec has dead paths (not repaired — not ours)")
        for dp in result.dead:
            loc = f"{dp.server}.{dp.where}" if dp.server else dp.where
            print(f"      {_sanitize_for_terminal(loc)}: {_sanitize_for_terminal(dp.path)}")
        issues.append(f"foreign agent spec dead paths: {spec}")

    for result in report.unreadable:
        # A managed spec whose re-read failed is already reported above.
        if result.managed and result.dead:
            continue
        print(
            f"  {_sanitize_for_terminal(result.spec)}: ⚠️  {_sanitize_for_terminal(result.unreadable or '')}"
        )
        issues.append(f"unreadable agent spec: {_sanitize_for_terminal(result.spec)}")

    if not report.has_findings and not report.managed_dead:
        print("  paths:       ✅ all agent spec paths resolve")
