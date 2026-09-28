"""Present-tense icacls-subprocess rationale is banned under test/ and docs/.

The Windows owner-only lockdown is an in-process DACL write
(``windows_acl.apply_owner_only``); no lockdown path spawns an ``icacls``
subprocess. A comment or spec sentence that explains the lockdown as an
``icacls`` subprocess is therefore wrong, and this rationale is load-bearing:
it tells a reader why a test fake exists, why an async caller offloads a
write, and what a lockdown failure can look like. A prose-only contract
drifts, so this guard makes the drift a CI failure.

Scope is ``test/`` and ``docs/``; ``src/`` and ``scripts/`` carry their own
conventions (``windows_acl.py`` legitimately uses icacls syntax as DACL
vocabulary). Legitimate mentions stay, by an explicit keep-list: files that
spawn or parse ``icacls`` as a verification or measurement tool, reference its
syntax as vocabulary, or record past-tense history. The keep-list is itself
pinned from both sides: a file on it must still mention icacls (so retired
entries are removed), and the pattern is exercised against seeded stale and
legitimate shapes (so a regex edit that silently stops matching turns the
guard off loudly).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Present-tense claims that the lockdown reaches ``icacls``. The union covers
#: every phrasing the three sweeps actually found -- including the backtick- and
#: quote-wrapped variants (``shells out to ``icacls````) that a plain
#: "shells out to icacls" grep misses.
_STALE_RATIONALE = re.compile(r"""(?ix)
    (?<! used\ to\ )
    (?: shells? \s+ out \s+ to | spawns? | runs | needs )
        \s+ (?: an? \s+ )? [`'"]{0,2} icacls
    | [`'"]{0,2} icacls [`'"]{0,2} \s+ (?: subprocess | spawns? \b | grants? \b )
    | [`'"]{0,2} icacls [`'"]{0,2} \s+ via \b
    """)

#: Files whose icacls mentions are deliberate, with the reason each stays.
_KEEP = {
    # Spawns ``icacls <path>`` on Windows to VERIFY the applied DACL, and
    # parses realistic icacls dump/argv shapes on every platform.
    "test/test_platform_compat.py",
    # References icacls flag syntax as DACL vocabulary next to the in-process
    # constants it documents.
    "test/test_windows_acl.py",
    # Measured ``icacls /deny`` + ``icacls /remove:d`` as an unprivileged
    # measurement tool; icacls is the subject, not the lockdown mechanism.
    "test/test_computer_use_launch.py",
    "docs/system-specs/modules/computer-use.md",
    # Past-tense history: why the helper skips absent files dates from when
    # the lockdown WAS an icacls subprocess.
    "test/test_config_rmw_preserves_settings.py",
    # Comparative reference: the in-process write measured against the
    # equivalent icacls invocation.
    "docs/guides/windows-install.md",
    # This guard: carries the stale shapes as seeded test data.
    "test/test_icacls_rationale_guard.py",
}


def _scan() -> list[str]:
    hits: list[str] = []
    for tree in ("test", "docs"):
        for path in sorted((_REPO_ROOT / tree).rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".md"}:
                continue
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel in _KEEP:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _STALE_RATIONALE.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_no_stale_icacls_subprocess_rationale_under_test_or_docs() -> None:
    """Zero present-tense icacls-subprocess claims outside the keep-list."""
    assert _scan() == [], (
        "stale icacls-subprocess rationale found; the lockdown has been "
        "in-process (windows_acl.apply_owner_only). Rewrite the "
        "rationale to the still-true reason (blocking file IO; the Windows "
        "DACL write can block on a network volume round-trip -- see "
        "restrict_to_owner's docstring), or add a keep-list entry with a "
        "reason if the mention is genuinely deliberate."
    )


def test_keep_list_entries_still_mention_icacls() -> None:
    """A keep-list entry for a file with no icacls mention is dead -- prune it."""
    stale_entries = []
    for rel in sorted(_KEEP):
        path = _REPO_ROOT / rel
        if not path.is_file():
            stale_entries.append(f"{rel} (file gone)")
            continue
        if "icacls" not in path.read_text(encoding="utf-8", errors="replace").lower():
            stale_entries.append(f"{rel} (no icacls mention left)")
    assert stale_entries == []


@pytest.mark.parametrize(
    "seeded",
    [
        "the lockdown shells out to icacls",
        "restrict_to_owner spawns ``icacls`` on Windows",
        "the real one shells out to ``icacls``, which cannot succeed here",
        "on Windows it runs icacls instead",
        "and on Windows an `icacls` subprocess",
        "up to 11 `icacls` spawns per init",
        "the Windows ACL path needs icacls",
        "spawns icacls via restrict_to_owner",
        "its icacls grants carry no (OI)(CI)",
    ],
)
def test_the_pattern_matches_the_stale_shapes(seeded: str) -> None:
    """Seeded violations keep the guard armed: every shape a sweep fixed."""
    assert _STALE_RATIONALE.search(seeded)


@pytest.mark.parametrize(
    "legitimate",
    [
        'raise OSError("icacls failed")',
        'raise OSError("icacls unavailable")',
        "OSError('icacls: transient failure')",
        "the equivalent of icacls /inheritance:r",
        "against 313 ms for the equivalent `icacls` invocation",
        "then re-read it via icacls to",
        "icacls <file> /deny <me>:(WD)",
        "the lockdown used to shell out to `icacls`, a blocking subprocess",
        "PROTECTED_DACL is what replaces icacls' /inheritance:r",
    ],
)
def test_the_pattern_ignores_legitimate_mentions(legitimate: str) -> None:
    """Fake error strings, syntax vocabulary, measurement uses, past tense."""
    assert not _STALE_RATIONALE.search(legitimate)
