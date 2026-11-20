# SPDX-License-Identifier: Apache-2.0
"""The live tree calls the conductor feature "conductor mode", never by its retired name.

The conductor-mode decision record under ``docs/decisions/`` keeps no alias for
the old name, so a reader or an agent searching the code for the feature
must not land on it. The changelog, the archived RFCs under
``docs/request-for-change/``, the decision records and the dated task specs keep
their history and are outside this gate.
"""

from __future__ import annotations

import re

from source_corpus import repo_files, repo_root

# Built from pieces so this file does not match its own pattern.
_RETIRED = re.compile("ul" + r"tra[ _-]?mode", re.IGNORECASE)

_LIVE_ROOTS = ("src/", "test/", "website/src/")
_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".json", ".md", ".css", ".html", ".toml", ".yaml", ".yml")


def test_no_live_file_uses_the_retired_conductor_mode_name():
    root = repo_root()
    offenders = []
    for path in repo_files():
        rel = path.relative_to(root).as_posix()
        if not rel.startswith(_LIVE_ROOTS) or not rel.endswith(_SUFFIXES) or "/_vendor/" in rel:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _RETIRED.search(line):
                offenders.append(f"{rel}:{lineno}")
    assert offenders == [], f"say 'conductor mode' instead: {offenders}"
