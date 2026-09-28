#!/usr/bin/env python3
"""Refuse a frame fixture that carries recording-host data rather than product shape.

A live ACP frame is HOST data until proved otherwise. The fixtures README says so and
asks for a hand review, and this is the part of that review a machine can hold: the
markers whose presence is never legitimate in a committed fixture.

The class this exists for is not a token. Credential scrubbers already catch those,
and a fixture that carried one would be caught by ``internal-content-scan`` too. The
class that gets through is an ENUMERATION -- a provider catalog, an installed-agent
list, a command inventory -- because it reads as ordinary product data on a quick look
while actually describing what the recording host HAS. A grep for home paths and
usernames passes clean over every one of them.

So the patterns below are in two groups. The first is the ordinary host-identity set
(usernames, absolute paths, endpoints). The second is the enumeration set, and it is
spelled as the specific vendor names a catalog carries, because "this looks like an
inventory" is not something a regex can ask.

Exit 0 when every fixture is clean, 1 when one is not, naming the file, the marker and
the reason. Read-only: it writes nothing, so it runs from CI, from the Main Ratchet
Audit lane and from the prepare-pr floor without the no-test-side-effects problem the
snapshot writer has to think about. It always sweeps the WHOLE corpus: a partial sweep
could not tell a cleaned file from one it was not asked about, so the stale-baseline
check would have to be skipped, and the three call sites all want the whole answer.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from typing import Iterable, List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO_ROOT, "test", "fixtures", "acp_frames")

#: ``(pattern, why)``. The why is what a failure prints, so it names the CLASS rather
#: than the match -- an author who sees "a cut catalog entry" knows to prune the
#: catalog, where "found 'anthropic'" invites deleting one word.
PATTERNS: Tuple[Tuple[str, str], ...] = (
    # ── host identity ──
    (r"/(?:local/)?home/[a-z][a-z0-9_-]*", "an absolute host home path"),
    (r"/Users/[A-Za-z][A-Za-z0-9_-]*", "an absolute macOS home path"),
    (r"C:\\\\Users\\\\[A-Za-z]", "an absolute Windows home path"),
    # A recorder's scratch project directory. Not a home path, but the same class: an
    # absolute path that exists only on the machine that recorded the frame.
    (r"/tmp/[A-Za-z0-9._-]+", "an absolute scratch path"),
    (r"(?i)runtime-[0-9a-f]{8}", "a scratch run id"),
    (r"(?i)\b(?:localhost|127\.0\.0\.1)\b", "a local endpoint"),
    (r"\.venv|site-packages|node_modules", "the recording toolchain"),
    (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "a run or request uuid"),
    # ── enumerations: what the host HAS, not what the product IS ──
    (r"(?i)\[internal\]", "an internal-only marker"),
    (
        r"(?i)\b(?:aws_bedrock|azure_openai|azure_foundry|databricks|openrouter|cerebras)\b",
        "a provider-catalog entry",
    ),
    (r"(?i)activeRunId", "a harness run id"),
    (r"(?i)availableCommands", "the harness's own command inventory"),
)

#: The corpus that predates this gate, as ``path -> the reasons it already trips``.
#:
#: SHRINK-ONLY, and per FILE and per REASON rather than per pattern, which is what
#: makes it a ratchet instead of an opt-out: a file may keep exactly the markers
#: recorded here, a NEW marker in the same file still fails, and a file absent from
#: this map must be clean. So a new fixture cannot inherit an exemption and an old one
#: cannot acquire a second.
#:
#: Every entry is a real leak rather than a false positive, which is why the baseline
#: is a list of debts and not a list of exceptions. Three classes are in here: a
#: scratch run id (a fragment of the recording host's directory name), a request or
#: run uuid, and the harness's own command inventory. Burning one down means
#: re-recording that fixture through the capture script with the field pruned, then
#: deleting its line -- and the gate will then hold it clean.
BASELINE: dict = {
    "test/fixtures/acp_frames/claude/session.expected.json": (
        "a scratch run id",
        "a run or request uuid",
    ),
    "test/fixtures/acp_frames/claude/session.jsonl": (
        "a scratch run id",
        "a run or request uuid",
    ),
    "test/fixtures/acp_frames/codex/session-live.expected.json": ("a run or request uuid",),
    "test/fixtures/acp_frames/codex/session-live.jsonl": (
        "a run or request uuid",
        "the harness's own command inventory",
    ),
    "test/fixtures/acp_frames/kas/session.expected.json": ("a run or request uuid",),
    "test/fixtures/acp_frames/kas/session.jsonl": (
        "a scratch run id",
        "a run or request uuid",
    ),
    "test/fixtures/acp_frames/kiro/session.expected.json": ("a run or request uuid",),
    "test/fixtures/acp_frames/kiro/session.jsonl": ("a run or request uuid",),
    "test/fixtures/acp_frames/opencode/permission-request-live.expected.json": (
        "a scratch run id",
    ),
    "test/fixtures/acp_frames/opencode/permission-request-live.jsonl": ("a scratch run id",),
    "test/fixtures/acp_frames/opencode/session-live.jsonl": (
        "the harness's own command inventory",
    ),
    "test/fixtures/acp_frames/opencode/session-load-live.jsonl": (
        "the harness's own command inventory",
    ),
    "test/fixtures/acp_frames/opencode/tool-call-live.expected.json": ("a scratch run id",),
    "test/fixtures/acp_frames/opencode/tool-call-live.jsonl": ("a scratch run id",),
    "test/fixtures/acp_frames/pi/permission-request-live.expected.json": ("a run or request uuid",),
    "test/fixtures/acp_frames/pi/permission-request-live.jsonl": (
        "a run or request uuid",
        "the harness's own command inventory",
        "an absolute scratch path",
    ),
    "test/fixtures/acp_frames/pi/session-live.expected.json": ("a run or request uuid",),
    "test/fixtures/acp_frames/pi/session-live.jsonl": (
        "a run or request uuid",
        "the harness's own command inventory",
    ),
    "test/fixtures/acp_frames/pi/session-load-live.jsonl": (
        "a run or request uuid",
        "the harness's own command inventory",
    ),
}


def fixture_files(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if name.endswith(".jsonl") or name.endswith(".expected.json"):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


def repo_relative(path: str) -> str:
    """*path* as a repo-relative key with FORWARD slashes, or its own path when outside.

    Two Windows facts make this its own function rather than an inline ``relpath``. That
    call RAISES ``ValueError`` across drives, and a test planting a fixture under a temp
    directory gets a different drive from the checkout on a hosted runner. And the
    baseline is authored with ``/`` on every host -- matching how the corpus is spelled
    everywhere else in this repo -- so a key built with ``os.sep`` would miss every
    lookup on Windows, which turns a baselined marker into a failure AND every baseline
    entry into a stale one.
    """
    try:
        rel = os.path.relpath(path, REPO_ROOT)
    except ValueError:
        # A different drive: not under the checkout, so it has no repo-relative name and
        # cannot carry a baseline entry. Its own path is the honest key.
        return _posix(path)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or rel.startswith("../"):
        return _posix(path)
    return _posix(rel)


def _posix(path: str) -> str:
    """*path* with BOTH separators folded to ``/``.

    Both, not just ``os.sep``: the baseline is authored with ``/`` on every host, so the
    key has to be POSIX-spelled whichever spelling the path arrives in. Folding only the
    running platform's separator leaves a backslash path un-normalized on a POSIX host --
    the one case where the same path yields a different key on two platforms, which is
    exactly the drift a baseline lookup cannot survive.
    """
    return path.replace("\\", "/").replace(os.sep, "/")


def sweep(paths: Iterable[str]) -> List[str]:
    failures: List[str] = []
    for path in paths:
        rel = repo_relative(path)
        try:
            text = io.open(path, encoding="utf-8").read()
        except OSError as exc:
            failures.append(f"{rel}: could not be read ({exc})")
            continue
        for pattern, why in PATTERNS:
            if why in BASELINE.get(rel, ()):
                continue
            match = re.search(pattern, text)
            if match:
                failures.append(f"{rel}: {why} -- found {match.group(0)!r}")
    return failures


def _stale_baseline_entries(paths: Iterable[str]) -> List[str]:
    """Baseline lines the corpus no longer needs.

    A stale entry is not harmless: it is a standing permission for a marker the file
    has stopped carrying, so the NEXT leak of that class in that file passes unseen.
    Reported as a failure with the line to delete, which is the only edit it needs.
    """
    stale: List[str] = []
    present = {repo_relative(p) for p in paths}
    for rel, reasons in sorted(BASELINE.items()):
        if rel not in present:
            stale.append(f"{rel}: baselined but no longer in the corpus -- strike its entry")
            continue
        text = io.open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()
        for why in reasons:
            pattern = next((p for p, w in PATTERNS if w == why), None)
            if pattern is None:
                stale.append(f"{rel}: baselines {why!r}, which is not a pattern this gate has")
            elif not re.search(pattern, text):
                stale.append(
                    f"{rel}: no longer carries {why!r} -- strike it from BASELINE so the "
                    "next one fails"
                )
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    paths = fixture_files(CORPUS)
    if not paths:
        print("acp-frame-host-data gate: no fixtures found, which is a corpus problem")
        return 1

    failures = sweep(paths)
    failures.extend(_stale_baseline_entries(paths))
    if failures:
        print(f"acp-frame-host-data gate FAILED over {len(paths)} file(s):")
        for line in failures:
            print("  " + line)
        print(
            "\nA fixture carries what the WIRE carried, not what the recording host had. "
            "Re-record through the capture script and prune there rather than editing a "
            "committed frame: an edited frame is no longer evidence, and its _meta header "
            "claims it is."
        )
        return 1

    print(
        f"acp-frame-host-data gate passed: {len(paths)} fixture file(s) clean over "
        f"{len(PATTERNS)} marker pattern(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
