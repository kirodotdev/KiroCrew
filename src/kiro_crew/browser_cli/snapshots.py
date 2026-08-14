"""Retention for the snapshot files ``@playwright/cli`` writes and never removes.

The CLI writes one timestamped artifact per command that produces output
(``page-<iso-timestamp>.yml`` for a snapshot, plus screenshots and PDFs) and
documents no pruning, so the directory grows without bound for as long as the
host browses.

**Where the files land, verified against the CLI's own resolution:**

- With no override, output goes to ``<process cwd>/.playwright-cli``. For a
  gateway-spawned CLI that is wherever the agent's turn happened to be, which
  is why the service cannot simply scan one known path.
- ``PLAYWRIGHT_MCP_OUTPUT_DIR`` redirects **all** output-file writes, snapshot
  YAML included. Confirmed end-to-end: with it set, the ``.yml`` appeared under
  it and the working directory stayed empty.
- The equivalent config key, ``outputDir`` in the CLI config file, redirects
  identically, but ``--config`` is accepted only on the session-establishing
  commands (``open``/``attach``) and is rejected by later ones such as
  ``snapshot``. The env var applies to every invocation uniformly, so it is the
  mechanism :func:`cli_env_overrides` uses.
- The value must be **absolute**. The CLI resolves a relative one against its
  own working directory, which reintroduces exactly the cwd dependence the
  override exists to remove.

Pruning belongs to the long-lived service rather than to the agent: the agent
has no reason to know the retention policy, and a per-command prune would race
the daemon. Retention is by age **and** count because either bound alone fails
— a burst of browsing overruns a pure age policy, and an idle host keeps stale
files forever under a pure count policy.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

# Env var the CLI reads to redirect every output file. Named here rather than
# inlined because it is the contract with the CLI, not an implementation detail.
OUTPUT_DIR_ENV = "PLAYWRIGHT_MCP_OUTPUT_DIR"

_SNAPSHOT_DIR_NAME = "playwright-snapshots"

# Snapshots are throwaway state — a stale tree describes a page that has since
# navigated — so the budget is generous enough to cover an active session and
# nothing more.
DEFAULT_MAX_AGE_S = 24 * 60 * 60
DEFAULT_MAX_FILES = 200

# A file younger than this is never pruned regardless of the count or age
# bound. The agent receives a snapshot path and may read it seconds later;
# without a grace window the count bound can race the read when the directory
# is already near capacity.
GRACE_PERIOD_S = 5 * 60

# `state-save` names its default output with this prefix.
_PROTECTED_PREFIX = "storage-state"


def snapshot_dir() -> Path:
    """Directory the CLI is pointed at for its output files.

    Under the data home, so it is a fixed path the service knows regardless of
    which working directory an agent turn ran in. Deriving it from
    ``config_dir()`` also keeps an isolated ``KIROCREW_HOME`` (a pod, a test)
    isolated here too, instead of pruning the live install's files.
    """
    return config_dir() / _SNAPSHOT_DIR_NAME


def cli_env_overrides() -> dict[str, str]:
    """Environment additions that point the CLI at :func:`snapshot_dir`.

    Merged into the environment of **every** CLI invocation. An invocation that
    misses this writes into its own working directory instead, where the service
    never looks and the files accumulate unpruned.

    The path is absolute (``config_dir()`` is), which the CLI requires: it
    resolves a relative value against its own working directory.
    """
    return {OUTPUT_DIR_ENV: str(snapshot_dir())}


# The shape every file the CLI writes here has: a label, a timestamp, an
# extension -- `page-2026-08-13T04-41-46-762Z.yml`,
# `console-2026-08-13T04-41-45-520Z.log`. Pruning is restricted to this shape
# because the alternative -- "delete every regular file that is not
# storage-state" -- makes anything a human ever puts in the output directory
# disposable: a stray `notes.txt` would be deleted by the scheduled prune once it
# aged out.
#
# A SHAPE rather than a list of label prefixes, and that choice is deliberate:
# `page-` and `console-` are the two labels observed directly, but the CLI also
# writes network-response bodies, PDFs and videos here under labels this code has
# not enumerated. A prefix list would silently stop pruning those (an unbounded
# directory); the shape covers them while still refusing a hand-authored name.
# An unrecognized file is KEPT, which is the safe direction for a background
# deleter.
# The time half is the CLI's EXACT shape (`T04-41-46-762Z`), not a loose run of
# digits and dashes. A permissive `T[\d\-.]+Z` also accepted a hand-authored
# `notes-2026-08-13T1Z.txt`, which the pruner would then delete once it aged out --
# defeating the whole point of admitting only files the CLI wrote.
_CLI_OUTPUT_RE = re.compile(
    r"^[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*"
    r"-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z"
    r"\.[A-Za-z0-9]+$"
)


def _is_cli_output(path: Path) -> bool:
    """True when *path* is a file the CLI itself wrote as throwaway output."""
    return bool(_CLI_OUTPUT_RE.match(path.name))


def _candidates(directory: Path) -> list[tuple[float, Path]]:
    """``(mtime, path)`` for each prunable CLI output file directly in *directory*.

    Subdirectories are skipped. The CLI keeps structured output such as traces
    in its own subdirectories, and deleting inside those can break a recording
    that is still in progress.

    Only CLI-owned filenames are returned (see :data:`_CLI_OUTPUT_RE`), so
    a file the service did not create is never a deletion candidate at all.
    """
    out: list[tuple[float, Path]] = []
    for child in directory.iterdir():
        try:
            stat = child.stat()
        except OSError:
            continue
        if not child.is_file():
            continue
        if not _is_cli_output(child):
            continue
        out.append((stat.st_mtime, child))
    return out


def _is_protected(path: Path) -> bool:
    """True for a file in this directory that is NOT throwaway output.

    `state-save` with no filename writes ``storage-state-<timestamp>.json`` here,
    and that file is the product of a human login: someone typed a password, and
    possibly answered a second factor, to produce it. Age is the wrong test for it
    because a saved session is most valuable exactly when it is old enough that
    nobody wants to log in again. Snapshots, console logs and screenshots are
    reproducible by re-running a command; this is not.
    """
    return path.name.startswith(_PROTECTED_PREFIX)


def prune(
    max_age_s: float = DEFAULT_MAX_AGE_S,
    max_files: int = DEFAULT_MAX_FILES,
    grace_s: float = GRACE_PERIOD_S,
) -> int:
    """Delete snapshot files past either bound. Returns how many were removed.

    A file is removed when it is older than *max_age_s* **or** when more than
    *max_files* newer files exist. The newest file is never removed under any
    setting: it is the one the current session most likely still refers to, and
    a policy that can empty the directory would break a live turn. Saved storage
    state is never removed at all (see :func:`_is_protected`).

    Files younger than *grace_s* are unconditionally spared: the agent receives
    a snapshot path and may read it seconds later, so a prune cycle that fires
    mid-turn must not race the read.

    Best-effort by contract. This runs on a schedule with no caller waiting on
    it, so a vanished file or a permission fault is logged and skipped rather
    than raised — a partial prune is strictly better than an unpruned directory
    plus an exception in the service loop.
    """
    directory = snapshot_dir()
    try:
        if not directory.is_dir():
            return 0
        entries = [item for item in _candidates(directory) if not _is_protected(item[1])]
    except OSError as exc:
        logger.debug("snapshot prune could not read %s: %s", directory, exc)
        return 0

    # Newest first, so index 0 is the protected file and the count bound is a
    # simple position test.
    entries.sort(key=lambda item: item[0], reverse=True)

    keep_count = max(1, max_files)
    now = time.time()
    removed = 0

    for index, (mtime, path) in enumerate(entries):
        if index == 0:
            continue
        # A file younger than the grace period cannot be pruned: the agent
        # may still hold its path from the tool response that handed it out.
        if grace_s > 0 and (now - mtime) < grace_s:
            continue
        too_many = index >= keep_count
        too_old = max_age_s > 0 and (now - mtime) > max_age_s
        if not (too_many or too_old):
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.debug("snapshot prune could not remove %s: %s", path, exc)
            continue
        removed += 1

    if removed:
        logger.info("pruned %d snapshot file(s) from %s", removed, directory)
    return removed
