"""``kirocrew doctor``: detect agent specs whose managed MCP env pins a foreign data home.

Every managed Kiro Crew MCP server entry in an agent spec (``kirocrew-core``,
``kirocrew-cron``, ...) is launched by kiro-cli with the spec's ``env`` laid over
the inherited environment, and ``agent._managed_mcp_env`` pins ``KIROCREW_HOME``
there whenever the WRITER ran under a data-home override. A spec written by one
instance and read by another therefore makes the reader's shims resolve
``config_dir()`` to the writer's home: they look up their signed session-pid
mapping, the SEL trust root, the cron store and the lessons file somewhere the
running gateway never writes. Every strict-identity tool is then refused with
"signed pid mapping did not verify" while ``kirocrew doctor``'s trust-root check
-- run against the gateway's own home -- reports green.

This module answers the question that check cannot: *do the specs this gateway's
sessions will spawn from agree with this gateway about where the data home is?*
It walks every ``*.json`` in the agents dir and compares each ``mcpServers.*.env
.KIROCREW_HOME`` PIN against the data home THIS process resolves. A spec with no
pin is not a finding: a default-home writer emits none, so under the default
home that is the correct shape (see :func:`check_spec_home_drift`).

Report-only. The remedy for a managed spec is one command
(``kirocrew setup --agent-only``, run from the gateway's own home, rewrites the
managed specs from THIS install), and doctor names it rather than running it: a
rebuild is a write to the shared kiro-cli home and belongs to a verb the operator
invokes on purpose. A foreign spec (another tool's agent that happens to declare
a Kiro Crew server) is named for the operator to edit by hand; Kiro Crew never
rewrites what it does not own.

Same shape as :mod:`kiro_crew.doctor_deadpath`: one public check returning a
report, one thin renderer, and an ``agents_dir`` argument so doctor scans the
directory it is inspecting rather than re-resolving the live home.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.doctor_deadpath import _sanitize_for_terminal
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

logger = logging.getLogger(__name__)

#: Kiro Crew's own managed MCP server names -- the entries ``rebuild_agent_config``
#: rewrites. Taken from :mod:`kiro_crew.mcp_cleanup`, which pins this tuple to
#: ``agent._MANAGED_MCP_SERVERS`` with a ratchet test and stays off the heavy
#: import chain, rather than spelling the names again here.
MANAGED_MCP_SERVER_NAMES = frozenset(KIROCREW_BIN_MCP_SERVERS)

#: The env key the check is about. A module constant rather than a literal in
#: three places: the writer (``agent._managed_mcp_env``) and this reader must
#: agree on the spelling, and a test can assert against the same name.
PINNED_HOME_KEY = "KIROCREW_HOME"


@dataclass
class HomeDrift:
    """One managed-server entry whose pinned data home disagrees with ours."""

    spec: str  # spec filename (e.g. "kirocrew.json")
    server: str  # mcpServers entry name
    pinned: str  # the KIROCREW_HOME value the spec carries (never empty)
    managed: bool  # spec is one of OWNED_KIRO_AGENT_FILES (ours to rewrite)


@dataclass
class HomeDriftReport:
    """Everything the doctor renderer needs, split by who may fix it."""

    expected: str = ""  # the data home this process resolved
    scanned: int = 0  # spec files that parsed as JSON objects
    drift: list[HomeDrift] = field(default_factory=list)

    @property
    def managed(self) -> list[HomeDrift]:
        return [d for d in self.drift if d.managed]

    @property
    def foreign(self) -> list[HomeDrift]:
        return [d for d in self.drift if not d.managed]


def _lexical(path: str | Path) -> str:
    """One spelling for a home WITHOUT touching the filesystem.

    Spec-provided pins are untrusted: a foreign spec can carry any string, and
    on Windows ``Path.resolve()`` OPENS the path -- for a UNC-shaped pin
    (``\\\\host\\share``) that is an SMB connection that authenticates to the
    named host before doctor has decided anything. So pins are normalised
    lexically only: ``~`` expansion (reads ``$HOME``, not the disk), separator
    and ``..`` folding, trailing-slash removal, and the platform's case fold.
    No ``resolve()``, no ``stat``.

    The expected side is normalised the same way (see :func:`_expected_forms`),
    so ``~/x`` and ``/home/u/x/`` still compare equal; only a SYMLINKED spelling
    of the same home stops matching, and that is the correct trade: a false
    "drift" line on a symlink-spelled pin costs one doctor line, a network probe
    of an attacker-named host costs a credential.

    ``~`` expansion is limited to a bare ``~`` or ``~/`` prefix: ``~name/...``
    would make :func:`os.path.expanduser` consult the account database for a
    spec-supplied name, and that lookup is exactly the kind of external probe on
    untrusted text this function exists to avoid. Such a pin is compared as
    written and simply does not match.
    """
    text = str(path)
    if text == "~" or text.startswith(("~/", "~\\")):
        text = os.path.expanduser(text)
    return os.path.normcase(os.path.normpath(text))


def _expected_forms(home: Path) -> frozenset[str]:
    """Every spelling a pin may use to name OUR data home.

    Our own home is trusted, so it may be resolved: the pin a default-home writer
    emits is ``_valid_override_home()``'s RESOLVED form, while an operator's
    hand-written ``KIROCREW_HOME`` may be the raw spelling. Both, lexically
    folded, are accepted.
    """
    forms = {_lexical(home)}
    try:
        forms.add(_lexical(home.resolve()))
    except (OSError, RuntimeError):  # pragma: no cover - defensive
        pass
    raw = os.environ.get("KIROCREW_HOME")
    if raw:
        forms.add(_lexical(raw))
    return frozenset(forms)


def _pinned_home(entry: dict) -> str | None:
    """The ``KIROCREW_HOME`` a server entry pins, ``""`` when it pins none.

    ``None`` when the entry has no usable ``env`` at all (not an object), so the
    caller can skip it rather than read a malformed spec as "default home".
    Matched case-insensitively on the key because kiro-cli's env is applied to a
    process environment, and ``env.sanitize_spec_env`` folds case the same way.
    """
    env = entry.get("env")
    if env is None:
        return ""
    if not isinstance(env, dict):
        return None
    for key, value in env.items():
        if isinstance(key, str) and key.upper() == PINNED_HOME_KEY:
            return value if isinstance(value, str) else None
    return ""


def check_spec_home_drift(*, agents_dir: Path | None = None) -> HomeDriftReport:
    """Compare every spec's pinned ``KIROCREW_HOME`` against this process's data home.

    Args:
        agents_dir: The agents directory to scan. Defaults to the live
            :func:`kiro_agents_dir`. ``kirocrew doctor`` passes its OWN resolved
            directory so the scan covers exactly what it is inspecting.

    The expected home is :func:`data_home` -- the override when this process
    runs under one, else the default. Only a spec that PINS a home, and pins a
    different one, is drift. A spec with no pin is left alone on purpose: a
    default-home writer emits none (``_managed_mcp_env`` returns ``{}`` there),
    so under the default home an unpinned spec is correct, and under an override
    it means at worst that the shims derive the default home -- a different,
    quieter defect than the one this check exists to name, and one that does not
    arise once a non-default instance owns its own agents dir. Flagging it would
    also turn every hand-written fixture spec into a finding.

    Fail-open per file: an unreadable or malformed spec is skipped (the dead-path
    check already reports those), never aborting the walk.
    """
    if agents_dir is None:
        agents_dir = kiro_agents_dir()
    home = data_home()
    report = HomeDriftReport(expected=str(home))
    expected_forms = _expected_forms(home)
    if not agents_dir.is_dir():
        return report

    managed_names = set(OWNED_KIRO_AGENT_FILES)
    try:
        entries = sorted(
            (Path(e.path) for e in os.scandir(agents_dir) if e.name.endswith(".json")),
            key=lambda p: p.name,
        )
    except OSError as exc:  # pragma: no cover - defensive: dir vanished mid-scan
        logger.debug("agents dir %s unreadable: %s", agents_dir, exc)
        return report

    for spec_path in entries:
        # The one hardened reader every spec consumer uses: refuses a symlink
        # whose resolved target is sensitive, a non-UTF-8 or oversized file, and
        # anything that is not a JSON object -- all as ``None``, fail-open per
        # file. The agents dir is user-writable and shared with other tools, so
        # a bare ``read_text`` here would be the one spec read outside the gate.
        data = _read_agent_spec(spec_path, operation="doctor", source="cli")
        if data is None:
            continue
        report.scanned += 1
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for server, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            pinned = _pinned_home(entry)
            if not pinned:
                continue
            if _lexical(pinned) in expected_forms:
                continue
            report.drift.append(
                HomeDrift(
                    spec=spec_path.name,
                    server=str(server),
                    pinned=pinned,
                    # "Managed" -- rewritable by ``setup --agent-only`` -- needs BOTH
                    # an owned spec file AND one of Kiro Crew's own server names. A
                    # user-added custom server inside kirocrew.json is preserved by
                    # the rebuild, so a drifted pin there would survive the remedy
                    # doctor recommends; it is reported as foreign (edit by hand).
                    managed=spec_path.name in managed_names
                    and str(server) in MANAGED_MCP_SERVER_NAMES,
                )
            )
    return report


def doctor_spec_home_drift(issues: list[str], *, agents_dir: Path | None = None) -> None:
    """Render the ``Agent Spec Data Home`` section of ``kirocrew doctor``.

    Silent-ish on a healthy install (one ✅ line). A managed spec whose pinned
    home disagrees with this process's is a real finding -- it is the exact state
    under which every strict-identity MCP tool is refused -- so it is appended to
    *issues* and doctor exits nonzero, with the one-command remedy on the line.
    A foreign entry is printed with the same ⚠️ but appended to nothing: it is
    not this install's to repair, and a nonzero exit it cannot clear would only
    teach operators to ignore the section.

    Best-effort: a failure inside the walk must not abort the doctor run.
    """
    print("\nAgent Spec Data Home")
    try:
        report = check_spec_home_drift(agents_dir=agents_dir)
    except Exception as exc:  # noqa: BLE001 — doctor must survive a broken walk
        print(f"  pins:        ⚠️  could not check ({exc})")
        return

    if not report.scanned:
        print("  pins:        ⏹ no agent specs found")
        return

    if not report.drift:
        print(
            f"  pins:        ✅ every managed MCP server resolves this data home ({report.expected})"
        )
        return

    print(f"  expected:    {report.expected}")
    for d in report.managed:
        print(
            f"  {_sanitize_for_terminal(d.spec)}: ⚠️  {_sanitize_for_terminal(d.server)} pins "
            f"KIROCREW_HOME={_sanitize_for_terminal(d.pinned)}"
        )
    if report.managed:
        print(
            "               Sessions spawned from these specs verify their identity "
            "against THAT home, so every"
        )
        print(
            "               strict-identity tool (session_ledger_*, monitor_*, "
            "list_sessions, ...) is refused"
        )
        print(
            "               while this gateway's trust root looks healthy. Another "
            "instance rebuilt the shared"
        )
        print(
            "               specs from a different KIROCREW_HOME. Fix: "
            "`kirocrew setup --agent-only` from this home."
        )
        issues.append("agent specs pin a different KIROCREW_HOME than this data home")
    # Foreign entries are named, never counted: doctor's nonzero exit is a
    # promise that something on THIS install is broken and fixable, and a
    # third-party spec (or a user-added server inside an owned one) is neither
    # rewritten by ``setup --agent-only`` nor this install's to repair.
    for d in report.foreign:
        print(
            f"  {_sanitize_for_terminal(d.spec)}: ⚠️  {_sanitize_for_terminal(d.server)} pins "
            f"KIROCREW_HOME={_sanitize_for_terminal(d.pinned)} "
            f"(foreign spec — not rewritten by setup; edit it by hand)"
        )
