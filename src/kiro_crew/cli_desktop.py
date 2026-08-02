"""``kirocrew desktop`` -- read the Electron app's debug metrics artifact.

Why this is a reader and not a query: ``app.getAppMetrics()`` is an Electron-main
API, readable only from inside that process. This CLI is a separate Python
process, and a second Electron instance cannot see the first one's metrics, so
there is no way to ask the running app for a live sample without adding a
listener to it. Rather than open one, ``website/electron/perf-metrics.js`` records
a bounded rolling artifact and this command reads it.

The consequence worth stating plainly: the numbers here are the most recent
samples the app wrote, not an instant captured on demand. If the app is not
running with ``KIROCREW_DEBUG`` set, there is no artifact at all.

Debug-only, and gated by the same ``KIROCREW_DEBUG`` check as ``kirocrew perf``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.hooks import safe_read_file
from kiro_crew.perf_sampler import gate_refusal_message, profiling_enabled

# Kept in sync with ARTIFACT_NAME in website/electron/perf-metrics.js. A rename on
# either side makes this command report "not found" against an app that is
# recording perfectly well, so the two names must move together.
ARTIFACT_NAME = "desktop-metrics.json"

# Electron productName values, which determine app.getPath("logs"). Kept in sync
# with website/electron/package.json (the release default) and
# packaging/build-desktop.sh, which overrides it with
# `-c.productName=KiroCrew Nightly` for nightly stamps. Nightly is not a cosmetic
# variant: it installs as a separate app with its own log directory, and its users
# are the ones most likely to be profiling, so omitting it made the command report
# "not found" against an app that was recording correctly.
PRODUCT_NAMES = ("KiroCrew", "KiroCrew Nightly")

SUPPORTED_VERSION = 1


def _home_dir() -> Path:
    """Return the user's home directory.

    Indirection so tests can simulate an unresolvable home without patching
    ``pathlib.Path.home``, which mutates pathlib process-wide and corrupts
    ``WindowsPath`` internals on the Windows CI shard.
    """
    return Path.home()


def desktop_log_dirs(env: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Candidate directories for Electron's ``app.getPath("logs")``.

    Mirrors Electron's own per-platform resolution, for **every** product name:

    * macOS   ``~/Library/Logs/<productName>``
    * Windows ``%APPDATA%/<productName>/logs``
    * Linux   ``$XDG_CONFIG_HOME/<productName>/logs`` (``~/.config`` default)

    Both release and nightly are probed, because they install side by side with
    separate log directories. Returns an empty tuple when the home directory
    cannot be resolved, which is the correct answer rather than an error.
    """
    environ = os.environ if env is None else env
    try:
        home = _home_dir()
    except (RuntimeError, OSError):
        # No HOME and no passwd entry. Nothing to probe; the caller falls back to
        # requiring an explicit --path rather than raising out of a helper.
        return ()

    if platform_compat.IS_MACOS:
        return tuple(home / "Library" / "Logs" / name for name in PRODUCT_NAMES)
    if platform_compat.IS_WINDOWS:
        appdata = environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return tuple(base / name / "logs" for name in PRODUCT_NAMES)
    xdg = environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home / ".config"
    return tuple(base / name / "logs" for name in PRODUCT_NAMES)


def find_metrics_artifact(
    explicit: Path | None = None, env: dict[str, str] | None = None
) -> Path | None:
    """Locate the metrics artifact, honouring an explicit path first.

    When both release and nightly have recorded, the **newest** artifact wins:
    with both installed, the one the operator just reproduced against is the one
    that was written last, and silently preferring release would report stale
    numbers rather than the run being investigated.
    """
    if explicit is not None:
        try:
            return explicit if explicit.is_file() else None
        except OSError:
            # An unreadable or malformed path is "not found" for our purposes.
            return None
    found: list[tuple[float, Path]] = []
    for directory in desktop_log_dirs(env):
        candidate = directory / ARTIFACT_NAME
        try:
            if candidate.is_file():
                found.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    if not found:
        return None
    # Sort by mtime, then by path so ties are deterministic rather than
    # filesystem-order dependent.
    found.sort(key=lambda pair: (pair[0], str(pair[1])))
    return found[-1][1]


def artifact_missing_message() -> str:
    """Explain the two distinct reasons the artifact can be absent."""
    searched = ", ".join(str(d) for d in desktop_log_dirs()) or "(no home directory)"
    return (
        f"No {ARTIFACT_NAME} found (searched: {searched}).\n"
        "The desktop app only records metrics when it is started with "
        f"KIROCREW_DEBUG set, so the usual cause is that the running app was "
        "launched without it -- restart the app with KIROCREW_DEBUG=1 and let it "
        "run for a few seconds. Pass --path to read an artifact from elsewhere."
    )


def summarize(doc: dict, top: int = 5) -> str:
    """Render the artifact as a short human-readable report.

    Reports the latest sample plus the peak across the retained window, because
    the reason to reach for this is usually a spike that has already passed.
    Entries that are not objects are discarded before anything indexes into them.
    A ``samples`` list is attacker-influencable only in the sense that any JSON
    file can be pointed at with ``--path``, but the artifact is also a file on
    disk that could be truncated or hand-edited, and ``[null]`` previously reached
    ``first.get(...)`` and raised an uncaught AttributeError. Three separate
    call sites here assume dict entries (first/last, the peak scan, and the
    process table), so the filter is applied once, up front, rather than guarding
    each.
    """
    raw_samples = doc.get("samples") or []
    samples = [s for s in raw_samples if isinstance(s, dict)]
    if not samples:
        if raw_samples:
            return "The artifact contains no usable samples (entries are malformed)."
        return "The artifact contains no samples yet."

    lines: list[str] = []
    window = f"{len(samples)} sample(s)"
    interval = doc.get("intervalMs")
    if isinstance(interval, (int, float)) and interval > 0:
        window += f" at {interval / 1000:g}s"
    lines.append(
        f"platform={doc.get('platform') or 'unknown'} "
        f"electron={doc.get('electron') or 'unknown'}  {window}"
    )
    first, last = samples[0], samples[-1]
    lines.append(f"window: {first.get('at')} -> {last.get('at')}")

    peak = max(samples, key=lambda s: _num(s.get("totalCpuPercent")))
    lines.append(
        f"latest total: cpu={_fmt_pct(last.get('totalCpuPercent'))} "
        f"rss={_fmt_kb(last.get('totalWorkingSetKb'))}"
    )
    lines.append(
        f"peak total:   cpu={_fmt_pct(peak.get('totalCpuPercent'))} "
        f"rss={_fmt_kb(peak.get('totalWorkingSetKb'))}  at {peak.get('at')}"
    )

    raw_procs = last.get("processes")
    # A non-list "processes" (a dict, a string, null) must not be iterated as one.
    procs = [p for p in raw_procs if isinstance(p, dict)] if isinstance(raw_procs, list) else []
    procs.sort(key=lambda p: _num(p.get("cpuPercent")), reverse=True)
    if procs:
        lines.append("")
        lines.append(f"latest sample, top {min(top, len(procs))} process(es) by CPU:")
        lines.append(f"  {'PID':>7}  {'TYPE':<12} {'CPU':>7}  {'RSS':>10}  NAME")
        for proc in procs[:top]:
            label = proc.get("serviceName") or proc.get("name") or ""
            lines.append(
                f"  {_fmt_int(proc.get('pid')):>7}  {str(proc.get('type') or '-'):<12} "
                f"{_fmt_pct(proc.get('cpuPercent')):>7}  {_fmt_kb(proc.get('workingSetKb')):>10}  {label}"
            )
    return "\n".join(lines)


def _num(value: object) -> float:
    """Coerce a metric value to a number for ordering, treating junk as 0.

    ``max()`` and ``list.sort()`` compare the key values against each other, so a
    single string among numeric entries raises ``TypeError`` mid-sort -- the
    ``or 0`` idiom does not help, because a non-empty string is truthy and is
    returned as-is. bool is excluded deliberately: it is an int subclass, and a
    ``true`` in a CPU field is corruption rather than the number 1.
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) if value == value and value not in (float("inf"), float("-inf")) else 0.0
    return 0.0


def _fmt_pct(value: object) -> str:
    return f"{value:.1f}%" if _is_num(value) else "-"


def _fmt_kb(value: object) -> str:
    if not _is_num(value):
        return "-"
    kb = float(value)  # type: ignore[arg-type]
    return f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"


def _is_num(value: object) -> bool:
    """Whether a value is a finite real number (and not a bool)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _fmt_int(value: object) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "-"


def _load(path: Path) -> tuple[dict | None, str | None]:
    """Read and parse the artifact. Returns ``(doc, error)``.

    Reads through :func:`hooks.safe_read_file` rather than ``Path.read_text``.
    ``--path`` is caller-supplied, and printing a file's contents is exactly the
    capability the centralized ``is_sensitive_path`` gate exists to bound, so this
    goes through the same helper every other caller-path JSON read in the CLI uses
    (see ``cli_config``/``cli_commands``). It also canonicalizes and opens with
    O_NOFOLLOW, which closes a symlink swap the plain ``is_file()`` probe would
    have followed.

    The auto-discovered path is read the same way, deliberately: the app's log
    directory is user-writable, so a symlink planted there would otherwise be
    followed just as readily as one passed on the command line.
    """
    try:
        raw = safe_read_file(str(path))
    except PermissionError:
        return None, (
            f"Refusing to read {path}: the path is protected or resolves to a "
            "sensitive location."
        )
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"Could not read {path}: {exc}"
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        # A torn read should be impossible (the writer renames into place), so a
        # parse failure means the file is genuinely malformed.
        return None, f"{path} is not valid JSON: {exc}"
    if not isinstance(doc, dict):
        return None, f"{path} does not contain a metrics object."
    samples = doc.get("samples")
    if samples is not None and not isinstance(samples, list):
        return None, f"{path} has a malformed 'samples' field (expected a list)."
    return doc, None


def _desktop_metrics(args: argparse.Namespace) -> int:
    if not profiling_enabled():
        print(gate_refusal_message(), file=sys.stderr)
        return 1

    path = find_metrics_artifact(getattr(args, "path", None))
    if path is None:
        print(artifact_missing_message(), file=sys.stderr)
        return 3

    doc, error = _load(path)
    if doc is None:
        print(error, file=sys.stderr)
        return 5

    version = doc.get("version")
    if version != SUPPORTED_VERSION:
        # Fail loudly rather than misreading a future shape as v1.
        print(
            f"{path} has version {version!r}, but this build understands "
            f"version {SUPPORTED_VERSION}. Update kirocrew to read it.",
            file=sys.stderr,
        )
        return 5

    if args.json:
        print(json.dumps(doc, indent=2))
        return 0

    print(f"source: {path}")
    print(summarize(doc, top=args.top))
    return 0


def desktop_cmd(args: argparse.Namespace) -> int:
    """Dispatch a ``kirocrew desktop`` subcommand."""
    if args.desktop_cmd == "metrics":
        return _desktop_metrics(args)
    print(f"Unknown desktop subcommand: {args.desktop_cmd}", file=sys.stderr)
    return 2


def register_desktop_parser(sub: argparse._SubParsersAction) -> None:
    """Register ``kirocrew desktop`` and its subcommands."""
    parser = sub.add_parser(
        "desktop",
        help="Debug-only desktop app diagnostics (requires KIROCREW_DEBUG)",
    )
    # dest must NOT be "command": that collides with the top-level subparsers'
    # dest and silently overwrites the "desktop" value, so dispatch falls through
    # to the argparse help instead of running the subcommand.
    desktop_sub = parser.add_subparsers(dest="desktop_cmd", required=True)

    metrics = desktop_sub.add_parser(
        "metrics",
        help="Show the desktop app's recorded per-process CPU and memory metrics",
    )
    metrics.add_argument(
        "--path",
        type=Path,
        default=None,
        help=f"Read a specific {ARTIFACT_NAME} instead of probing the app's log directory",
    )
    metrics.add_argument("--json", action="store_true", help="Emit the raw artifact as JSON")
    metrics.add_argument(
        "--top", type=int, default=5, help="How many processes to list (default: 5)"
    )
