"""Contract for the shared adapter / blast-radius fixtures.

``test_adapters.py``, ``test_blast_radius.py`` and ``test_gap_fixes.py`` all assert
RATINGS over these payloads (SMALL vs MEDIUM, sensitive path, guard removal), so the
fixtures carry the properties those expectations rest on: which paths are sensitive,
which diff removes a guard, which change is a one-liner. If a fixture drifts, those
suites keep passing while asserting something else — the failure lands in the fixture,
not in the code under test — so pin the properties here.

Imported by the canonical package path rather than the suite's usual
``from tests.fixtures import ...``: the app puts its own root on ``sys.path``, so the
sibling spelling loads this file as a foreign top-level module and its execution is
discarded by ``--cov=kiro_crew`` (the same measurement problem ``setup.cfg``
documents for ``sage_lib``). Same file, same objects — just the spelling coverage
can attribute.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.apps.builtins.code_review_sage.tests import fixtures


class TestSensitiveFiles:
    def test_a_gateway_path_carries_a_guard_removal_and_an_import_add(self):
        server = fixtures.SENSITIVE_FILES[0]
        assert server["path"] == "src/kiro_crew/gateway/server.py"
        assert "-    if not self._stopping:" in server["diff"]
        assert "+import logging" in server["diff"]

    def test_the_pair_mixes_a_sensitive_and_a_non_sensitive_file(self):
        paths = [f["path"] for f in fixtures.SENSITIVE_FILES]
        assert paths == ["src/kiro_crew/gateway/server.py", "src/kiro_crew/util/format.py"]
        assert all(f["diff"].startswith("--- a/") for f in fixtures.SENSITIVE_FILES)


class TestRatingFixtures:
    def test_small_change_is_a_single_added_line_on_a_docs_path(self):
        assert len(fixtures.SMALL_FILES) == 1
        small = fixtures.SMALL_FILES[0]
        assert small["path"] == "docs/readme.md"
        added = [
            ln
            for ln in small["diff"].splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        ]
        assert added == ["+new line"]

    def test_sensitive_tiny_change_has_no_guard_to_remove(self):
        tiny = fixtures.SENSITIVE_TINY_FILES[0]
        assert tiny["path"].startswith("src/auth/")
        assert "if " not in tiny["diff"]
        removed = [
            ln
            for ln in tiny["diff"].splitlines()
            if ln.startswith("-") and not ln.startswith("---")
        ]
        assert removed == ["-x = 1"]


#: The fixture is a heterogeneous literal, so it types as ``dict[str, object]``;
#: naming it ``Any`` here keeps the nested reads readable without casting each one.
_PAYLOAD: dict[str, Any] = fixtures.GITHUB_PAYLOAD


class TestGithubPayload:
    def test_payload_has_the_pull_object_shape_the_worker_reads(self):
        payload = _PAYLOAD
        assert payload["number"] == 3361
        assert payload["state"] == "open"
        assert payload["draft"] is False
        assert payload["base"]["repo"]["full_name"] == "kiro-team/kiro-cli"
        assert len(payload["head"]["sha"]) == 40

    def test_every_file_entry_carries_its_own_patch(self):
        files = _PAYLOAD["files"]
        assert [f["filename"] for f in files] == [
            "crates/kiro-cli/src/cli/chat/mod.rs",
            "docs/CHANGELOG.md",
        ]
        assert all(f["patch"].startswith("--- a/") for f in files)
        assert all(f["status"] == "modified" for f in files)

    def test_review_comments_are_present_and_attributed(self):
        comments = _PAYLOAD["comments"]
        assert len(comments) == 1
        assert comments[0]["user"]["login"] == "reviewer"
        assert comments[0]["body"]
