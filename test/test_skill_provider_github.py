"""Tests for the GitHub-repo skill provider (``skill_providers/github.py``).

Every HTTP call is mocked at this module's own ``_sync_fetch_json`` /
``_sync_fetch_text`` seams, so no test touches the network. The fake responses
mirror GitHub's real shapes: the ``sha`` media type answers with a bare SHA, the
git-trees endpoint answers with ``{"tree": [...], "truncated": bool}``, and the
raw host answers with file bytes.

What each class pins:

- addressing (``TestParseRepoSpec``) — the grammar, the pasted-URL affordance and
  every refusal, since a parse that accepts too much is what would put attacker
  text into a URL and then into a filesystem path;
- discovery (``TestSearch``) — several skills per repository, the commit pin
  travelling in every row's id, and a non-address query costing no request;
- bundles (``TestFetchBundle``) — the pin record, nested-skill exclusion, the
  size ceilings, the non-UTF-8 skip and the container refusal;
- trust posture (``TestNetworkGuards``, ``TestInstallNamespace``) — the shared
  SSRF screen and allowlist really are this provider's, and an install lands in
  the provider-prefixed namespace where it cannot shadow a shipped skill.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from kiro_crew.skill_providers import github as gh
from kiro_crew.skill_providers.base import SkillProvider

# ---- fixtures / fakes ------------------------------------------------------

_COMMIT = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"

_SKILL_MD = """---
name: reviewer
description: Reviews a diff for missing tests.
---

# Reviewer

Body text.
"""

_OTHER_SKILL_MD = """---
name: releaser
description: Cuts a release.
---

Body.
"""


def _tree(*paths_and_sizes: tuple[str, int], truncated: bool = False) -> dict:
    """A git-trees response carrying *paths_and_sizes* as blobs."""
    return {
        "sha": _COMMIT,
        "truncated": truncated,
        "tree": [
            {"path": path, "type": "blob", "size": size, "mode": "100644"}
            for path, size in paths_and_sizes
        ],
    }


class _Fake:
    """Routes the provider's two fetch seams to canned responses by URL substring.

    Recording every URL is deliberate: several tests assert on what was NOT
    requested (no call at all for a non-address query, no blob fetch for an
    oversized file), which a return-value-only stub cannot express.
    """

    def __init__(self, *, json_routes: dict, text_routes: dict) -> None:
        self.json_routes = json_routes
        self.text_routes = text_routes
        self.json_urls: list[str] = []
        self.text_urls: list[str] = []

    def fetch_json(self, url: str):
        self.json_urls.append(url)
        for fragment, payload in self.json_routes.items():
            if fragment in url:
                return payload
        return None

    def fetch_text(self, url: str, accept=None):
        self.text_urls.append(url)
        for fragment, payload in self.text_routes.items():
            if fragment in url:
                return payload if isinstance(payload, str) else None
        return None

    def fetch_bytes(self, url: str):
        """Blob fetches go through the bytes seam, not the text one.

        A route holding ``bytes`` is served verbatim, which is how a test supplies
        a body that is not valid UTF-8; a ``str`` route is encoded. An absent route
        is a FAILED fetch (``None``) -- a different thing from undecodable bytes,
        and the provider now treats them differently.
        """
        self.text_urls.append(url)
        for fragment, payload in self.text_routes.items():
            if fragment in url:
                return payload if isinstance(payload, bytes) else payload.encode()
        return None

    def install(self):
        return patch.multiple(
            gh,
            _sync_fetch_json=self.fetch_json,
            _sync_fetch_text=self.fetch_text,
            _sync_fetch_bytes=self.fetch_bytes,
        )


def _one_skill_repo() -> _Fake:
    """acme/widgets with a single skill at ``skills/reviewer``."""
    return _Fake(
        json_routes={
            "/git/trees/": _tree(
                ("SKILL.md", len(_SKILL_MD)),
                ("rules/tests.md", 40),
            )
        },
        text_routes={
            "/commits/": _COMMIT,
            "/SKILL.md": _SKILL_MD,
            "/rules/tests.md": "always ask for a test",
        },
    )


# ---- addressing -----------------------------------------------------------


class TestParseRepoSpec:
    """The address grammar is the provider's outermost gate: every later URL and
    every installed filesystem path is built from what it returns, so it must
    accept exactly the documented forms and nothing adjacent."""

    def test_bare_repo(self):
        spec = gh.parse_repo_spec("acme/widgets")
        assert spec is not None
        assert (spec.owner, spec.repo, spec.ref, spec.path) == ("acme", "widgets", "", "")

    def test_ref_only(self):
        spec = gh.parse_repo_spec("acme/widgets@v2.1")
        assert spec is not None
        assert spec.ref == "v2.1" and spec.path == ""

    def test_path_only(self):
        spec = gh.parse_repo_spec("acme/widgets:skills/reviewer")
        assert spec is not None
        assert spec.ref == "" and spec.path == "skills/reviewer"

    def test_ref_and_path(self):
        spec = gh.parse_repo_spec("acme/widgets@release/2:skills/reviewer")
        assert spec is not None
        assert spec.ref == "release/2" and spec.path == "skills/reviewer"

    def test_slashed_ref_survives_because_path_splits_first(self):
        # The ':' partition runs before the '@' one, so a branch name containing
        # '/' is unambiguous in the @ref form -- this is the documented way to
        # address one, and a pasted tree URL cannot express it.
        spec = gh.parse_repo_spec("acme/widgets@feature/a/b:skills/x")
        assert spec is not None
        assert spec.ref == "feature/a/b" and spec.path == "skills/x"

    @pytest.mark.parametrize(
        "raw",
        [
            "https://github.com/acme/widgets",
            "http://github.com/acme/widgets",
            "https://www.github.com/acme/widgets",
            "github.com/acme/widgets",
            "acme/widgets.git",
            "  acme/widgets/  ",
        ],
    )
    def test_url_and_suffix_forms_normalize(self, raw):
        spec = gh.parse_repo_spec(raw)
        assert spec is not None
        assert spec.repo_slug == "acme/widgets"

    def test_pasted_tree_url(self):
        spec = gh.parse_repo_spec("https://github.com/acme/widgets/tree/main/skills/reviewer")
        assert spec is not None
        assert spec.ref == "main" and spec.path == "skills/reviewer"

    def test_a_pasted_url_is_percent_decoded_before_it_is_judged(self):
        # Decoding is NOT a safety guard here -- the allowlist admits only characters
        # a URL never encodes, so no acceptable name ever arrives encoded. Its value
        # is the MESSAGE: the user learns their file is ``my file.md``, which they can
        # act on, rather than ``my%20file.md``, which reads like a tool bug.
        assert (
            gh.parse_repo_spec("https://github.com/acme/widgets/tree/main/rules/my%20file.md")
            is None
        )
        assert "my file.md" in gh._path_problem("rules/my file.md")
        # An ordinary encoded separator still round-trips to the same spec.
        decoded = gh.parse_repo_spec("https://github.com/acme/widgets/tree/main/rules%2Ftests.md")
        assert decoded is not None and decoded.path == "rules/tests.md"

    def test_a_pasted_url_cannot_smuggle_traversal_through_encoding(self):
        # Decoding happens BEFORE validation, so an encoded ``..`` meets the same
        # refusal as a literal one. Decoding after validating would be the bug.
        assert gh.parse_repo_spec("https://github.com/acme/widgets/tree/main/%2e%2e/etc") is None
        assert gh.parse_repo_spec("https://github.com/acme/widgets/tree/main/a%00b") is None
        assert gh.parse_repo_spec("https://github.com/acme/widgets/tree/%2e%2e/x") is None

    def test_pasted_blob_url(self):
        spec = gh.parse_repo_spec("https://github.com/acme/widgets/blob/main/skills")
        assert spec is not None
        assert spec.ref == "main" and spec.path == "skills"

    def test_tree_url_combined_with_at_ref_is_refused(self):
        # Two refs in one address have no defensible resolution order, so it is
        # refused rather than silently resolved one way.
        assert gh.parse_repo_spec("acme/widgets/tree/main@v2") is None
        assert gh.parse_repo_spec("acme/widgets/tree/main:x") is None

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "acme",
            "acme/",
            "/widgets",
            "docker compose",  # a plain search term
            "react",
            "acme//widgets",
            "acme/widgets/notatree/main",
            "acme/widgets@..",
            "acme/widgets@a/../b",
            "acme/widgets:..",
            "acme/widgets:../../etc/passwd",
            "acme/widgets:skills/../../etc",
            "acme/widgets:/absolute",
            "acme/widgets@-bad",  # a ref segment may not start with '-'
            "-acme/widgets",  # nor may an owner
            "acme/widgets@ref?query=1",
            "acme/wid gets",
        ],
    )
    def test_refusals(self, raw):
        assert gh.parse_repo_spec(raw) is None

    def test_non_string_is_refused(self):
        # search() forwards whatever the caller passed; a non-string must not
        # reach .strip() and raise inside the aggregate fan-out.
        assert gh.parse_repo_spec(None) is None
        assert gh.parse_repo_spec(12) is None

    def test_address_round_trips(self):
        spec = gh.parse_repo_spec("acme/widgets@v2:skills/reviewer")
        assert spec is not None
        assert spec.address() == "acme/widgets@v2:skills/reviewer"
        assert gh.parse_repo_spec(spec.address()) == spec

    def test_address_substitutes_ref_and_path(self):
        spec = gh.parse_repo_spec("acme/widgets")
        assert spec is not None
        assert spec.address(ref="0a1b2c3", path="skills/x") == "acme/widgets@0a1b2c3:skills/x"


# ---- protocol conformance -------------------------------------------------


class TestProviderShape:
    def test_satisfies_the_provider_protocol(self):
        # Registration in _build_registry() only inherits the install plumbing if
        # the provider really is a SkillProvider; the registry's own structural
        # check would otherwise skip it at runtime.
        assert isinstance(gh.GitHubRepoProvider(), SkillProvider)

    def test_identity_and_availability(self):
        p = gh.GitHubRepoProvider()
        assert p.name == "github"
        assert p.display_name == "GitHub repo"
        assert p.api_base == "https://api.github.com"
        # Always available: unauthenticated, so nothing can be unconfigured. The
        # discovery policy gate in _build_registry is what disables the provider,
        # and test_a_refused_policy_keeps_the_provider_unregistered covers that.
        assert p.is_available()


# ---- discovery ------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_non_address_query_costs_no_request(self):
        # This provider sits in the aggregate fan-out, so every unrelated search
        # reaches it. It must answer from the regex alone -- otherwise typing in
        # the Discover box would spend the unauthenticated GitHub rate limit.
        fake = _Fake(json_routes={}, text_routes={})
        with fake.install():
            assert await gh.GitHubRepoProvider().search("docker compose") == []
        assert fake.json_urls == [] and fake.text_urls == []

    @pytest.mark.asyncio
    async def test_several_skills_per_repo(self):
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(
                    ("skills/reviewer/SKILL.md", len(_SKILL_MD)),
                    ("skills/reviewer/rules/a.md", 10),
                    ("skills/releaser/SKILL.md", len(_OTHER_SKILL_MD)),
                    ("README.md", 20),
                )
            },
            text_routes={
                "/commits/": _COMMIT,
                "/skills/reviewer/SKILL.md": _SKILL_MD,
                "/skills/releaser/SKILL.md": _OTHER_SKILL_MD,
            },
        )
        with fake.install():
            results = await gh.GitHubRepoProvider().search("acme/widgets")

        # One row per directory holding a SKILL.md, in sorted order -- README.md
        # is not a skill and the nested rules file is not a second one.
        assert [r.name for r in results] == ["releaser", "reviewer"]
        assert [r.id for r in results] == [
            f"acme/widgets@{_COMMIT}:skills/releaser",
            f"acme/widgets@{_COMMIT}:skills/reviewer",
        ]

    @pytest.mark.asyncio
    async def test_row_carries_frontmatter_and_pinned_urls(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", len(_SKILL_MD)))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")

        # The loader's own frontmatter grammar, so the row matches what the
        # installed skill will show.
        assert row.name == "reviewer"
        assert row.description == "Reviews a diff for missing tests."
        assert row.provider == "github"
        assert row.author == "acme"
        # The FULL commit is browsable from the row; the id abbreviates it.
        assert row.repo_url == (f"https://github.com/acme/widgets/tree/{_COMMIT}/skills/reviewer")
        assert row.id == f"acme/widgets@{_COMMIT}:skills/reviewer"

    @pytest.mark.asyncio
    async def test_id_pins_the_full_resolved_commit_not_the_requested_branch(self):
        # The point of the pin: a row discovered from a branch must not send the
        # preview and the install back to that branch, which may have moved.
        #
        # The FULL commit, not an abbreviation. A 7-hex ref gets re-resolved later,
        # and a branch whose NAME is hex can shadow that prefix -- so an
        # abbreviated pin is a pin that can be substituted.
        fake = _one_skill_repo()
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets@main:skills/reviewer")
        assert "@main" not in row.id
        assert row.id.split("@")[1].split(":")[0] == _COMMIT
        assert len(_COMMIT) == 40

    @pytest.mark.asyncio
    async def test_row_falls_back_to_directory_name_when_skill_md_unreadable(self):
        # The skill IS there (the tree says so); only its frontmatter is missing.
        # Dropping the row would hide an importable skill.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("skills/reviewer/SKILL.md", 10))},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets")
        assert row.name == "reviewer"
        assert row.description == ""

    @pytest.mark.asyncio
    async def test_root_skill_falls_back_to_repo_name(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10))},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets")
        assert row.name == "widgets"
        assert row.id == f"acme/widgets@{_COMMIT}"

    @pytest.mark.asyncio
    async def test_limit_is_honoured(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(*[(f"s{i}/SKILL.md", 10) for i in range(8)])},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            results = await gh.GitHubRepoProvider().search("acme/widgets", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_repo_ceiling_caps_a_huge_repo(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(*[(f"s{i:03d}/SKILL.md", 10) for i in range(60)])},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            results = await gh.GitHubRepoProvider().search("acme/widgets", limit=50)
        assert len(results) == gh._MAX_SKILLS_PER_REPO

    @pytest.mark.asyncio
    async def test_unresolvable_ref_yields_nothing(self):
        fake = _Fake(json_routes={}, text_routes={})  # /commits/ returns None
        with fake.install():
            assert await gh.GitHubRepoProvider().search("acme/widgets@nope") == []

    @pytest.mark.asyncio
    async def test_non_sha_commit_response_is_refused(self):
        # GitHub is external input: only a full 40-hex SHA may become part of a
        # URL and of the recorded pin.
        for bogus in ("<html>404</html>", "main", _COMMIT[:7], _COMMIT + "0"):
            fake = _Fake(
                json_routes={"/git/trees/": _tree(("SKILL.md", 10))},
                text_routes={"/commits/": bogus},
            )
            with fake.install():
                assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_a_ref_named_like_a_commit_is_refused(self):
        # Git resolves a ref NAME before an object name. A branch literally named
        # after 40 hex characters would answer for that "commit" with its own tip,
        # substituting the content a pinned row promised. When we ask for a full
        # SHA, the answer has to be that SHA.
        other = "b" * 40
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10))},
            text_routes={"/commits/": other, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().search(f"acme/widgets@{_COMMIT}") == []
            assert (
                await gh.GitHubRepoProvider().fetch_skill_bundle(f"acme/widgets@{_COMMIT}") is None
            )

    @pytest.mark.asyncio
    async def test_a_branch_ref_still_resolves_to_whatever_it_points_at(self):
        # The check above must not break the ordinary case: a BRANCH name is
        # supposed to resolve to some other SHA.
        fake = _one_skill_repo()
        with fake.install():
            (row,) = await gh.GitHubRepoProvider().search("acme/widgets@main:skills/reviewer")
        assert _COMMIT in row.id

    @pytest.mark.asyncio
    async def test_a_row_that_cannot_be_installed_is_not_offered(self):
        # Discovery drops a skill whose own directory path is unwritable. Offering
        # it would mean the user only learns at the refusal, after choosing it.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("ok/SKILL.md", 10), ("bad:dir/SKILL.md", 10))},
            text_routes={"/commits/": _COMMIT, "SKILL.md": _SKILL_MD},
        )
        with fake.install():
            results = await gh.GitHubRepoProvider().search("acme/widgets")
        assert [r.id for r in results] == [f"acme/widgets@{_COMMIT}:ok"]

    @pytest.mark.asyncio
    async def test_repo_without_any_skill_md_yields_nothing(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("README.md", 10), ("src/a.py", 10))},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_truncated_tree_is_refused(self):
        # A partial listing would install a skill missing files. Refusing is the
        # honest answer; a half-written skill is not.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), truncated=True)},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_malformed_tree_payload_yields_nothing(self):
        for payload in ([], "maintenance", 7, {"tree": None}, {"tree": "x"}, None):
            fake = _Fake(
                json_routes={"/git/trees/": payload},
                text_routes={"/commits/": _COMMIT},
            )
            with fake.install():
                assert await gh.GitHubRepoProvider().search("acme/widgets") == []

    @pytest.mark.asyncio
    async def test_an_unwritable_tree_path_refuses_before_any_fetch(self):
        # A path the writer cannot write is not a file to skip quietly: skipping it
        # installs an incomplete skill and reports success. The refusal lands on
        # the LISTING, so no blob is requested at all.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(
                    ("SKILL.md", 10),
                    ("colon:name.md", 10),
                )
            },
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None
        assert [u for u in fake.text_urls if "/commits/" not in u] == []

    @pytest.mark.asyncio
    async def test_a_space_in_a_filename_refuses_the_bundle_and_is_not_dropped(self):
        # The allowlist is narrower than the filesystem, so this writable file is
        # REFUSED. What must never happen is the third option: importing the skill
        # without it and reporting success. The refusal is the point, not the
        # acceptance -- and no blob is fetched, so the answer comes from the listing.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("rules/my file.md", 20))},
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/rules/my%20file.md": "notes",
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None
        assert [u for u in fake.text_urls if "/commits/" not in u] == []

    @pytest.mark.asyncio
    async def test_git_plumbing_is_dropped_rather_than_refusing_the_import(self):
        # A dotfile cannot pass the allowlist, and ``.gitignore`` is common enough
        # inside a skill directory that refusing on it would block ordinary
        # repositories. It is git plumbing, never skill content, so it is excluded
        # by name BEFORE the allowlist runs -- the one deliberate omission class.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("SKILL.md", 10), (".gitignore", 5), (".gitattributes", 5))
            },
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert [p for p, _ in bundle] == ["SKILL.md", gh.PIN_FILENAME]

    @pytest.mark.asyncio
    async def test_subpath_address_narrows_the_tree_request(self):
        # The tree is asked for as "<commit>:<path>", which both narrows the
        # response under the 1 MiB cap and makes the returned paths the bundle's
        # own relative paths.
        fake = _one_skill_repo()
        with fake.install():
            await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")
        assert any(f"{_COMMIT}:skills/reviewer" in u for u in fake.json_urls)

    @pytest.mark.asyncio
    async def test_blobs_are_fetched_from_the_raw_host_pinned_to_the_commit(self):
        fake = _one_skill_repo()
        with fake.install():
            await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")
        assert any(
            u.startswith(f"https://raw.githubusercontent.com/acme/widgets/{_COMMIT}/")
            for u in fake.text_urls
        )


# ---- bundles --------------------------------------------------------------


class TestFetchBundle:
    @pytest.mark.asyncio
    async def test_bundle_carries_files_and_the_pin_record(self):
        fake = _one_skill_repo()
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle(
                f"acme/widgets@{_COMMIT[:7]}:skills/reviewer"
            )
        assert bundle is not None
        paths = [p for p, _ in bundle]
        assert paths == ["SKILL.md", "rules/tests.md", gh.PIN_FILENAME]
        # Every entry is (str, str) so the install handler's c.encode("utf-8")
        # can never raise.
        assert all(isinstance(p, str) and isinstance(c, str) for p, c in bundle)

    @pytest.mark.asyncio
    async def test_pin_record_records_the_full_commit(self):
        fake = _one_skill_repo()
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle(
                f"acme/widgets@{_COMMIT[:7]}:skills/reviewer"
            )
        assert bundle is not None
        pin = json.loads(dict(bundle)[gh.PIN_FILENAME])
        assert pin["provider"] == "github"
        assert pin["repo"] == "acme/widgets"
        assert pin["repo_url"] == "https://github.com/acme/widgets"
        # The FULL 40-hex commit, not the abbreviation the address carried.
        assert pin["commit"] == _COMMIT
        assert pin["requested_ref"] == _COMMIT[:7]
        assert pin["path"] == "skills/reviewer"
        assert pin["source_url"] == (
            f"https://github.com/acme/widgets/tree/{_COMMIT}/skills/reviewer"
        )
        assert pin["files"] == ["SKILL.md", "rules/tests.md"]
        assert "none" in pin["tracking"]
        assert pin["imported_at"].endswith("Z")

    @pytest.mark.asyncio
    async def test_repo_copy_of_the_pin_filename_refuses_the_bundle(self):
        # Our pin is appended AFTER the collision check, so the check has to
        # RESERVE the name. Dropping the repository's file instead would be one
        # more silent omission; overwriting it would be worse.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), (gh.PIN_FILENAME, 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                f"/{gh.PIN_FILENAME}": '{"commit": "deadbeef", "repo": "evil/repo"}',
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_a_case_variant_of_the_pin_filename_also_refuses(self):
        # Our pin could once be overwritten by a case-variant of its own name. That
        # is impossible now for a simpler reason than a reservation: the pin starts
        # with a dot and the allowlist admits no such repository path, so any
        # spelling of it refuses the bundle outright.
        variant = ".SKILL-IMPORT-SOURCE.JSON"
        assert variant.casefold() == gh.PIN_FILENAME.casefold()
        assert variant != gh.PIN_FILENAME
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), (variant, 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                f"/{variant}": "repository content",
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_nested_skill_files_are_excluded(self):
        # Importing a directory that holds both its own SKILL.md and a nested
        # skill must take only its own files -- the nested one is a separate
        # import with its own pin.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(
                    ("SKILL.md", 10),
                    ("rules/a.md", 10),
                    ("nested/SKILL.md", 10),
                    ("nested/rules/b.md", 10),
                )
            },
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/rules/a.md": "mine",
                "/nested/SKILL.md": _OTHER_SKILL_MD,
                "/nested/rules/b.md": "theirs",
            },
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert [p for p, _ in bundle] == ["SKILL.md", "rules/a.md", gh.PIN_FILENAME]

    @pytest.mark.asyncio
    async def test_container_without_its_own_skill_md_is_refused(self):
        # An address naming a directory that only CONTAINS skills is not a skill.
        # Refusing keeps one install from raking in every skill in the repo.
        #
        # The root-level ``notes.md`` is what makes the refusal observable: it is
        # not excluded as a nested skill's file, so without the up-front refusal
        # it would be fetched and the container address would cost a request
        # before failing later for a different reason.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("a/SKILL.md", 10), ("b/SKILL.md", 10), ("notes.md", 10))
            },
            text_routes={
                "/commits/": _COMMIT,
                "/a/SKILL.md": _SKILL_MD,
                "/notes.md": "loose notes",
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None
        # Refused on the listing alone -- no blob was read.
        assert [u for u in fake.text_urls if "/commits/" not in u] == []

    @pytest.mark.asyncio
    async def test_agents_md_only_bundle_is_accepted(self):
        # The install writer copies AGENTS.md to SKILL.md, so a repo using that
        # convention imports fine.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("AGENTS.md", 10))},
            text_routes={"/commits/": _COMMIT, "/AGENTS.md": _SKILL_MD},
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert [p for p, _ in bundle] == ["AGENTS.md", gh.PIN_FILENAME]

    @pytest.mark.asyncio
    async def test_oversized_blob_refuses_the_bundle_without_fetching_it(self):
        # Skipping it would install a skill whose own instructions may reference
        # the missing file, and report success. The tree already states the size,
        # so the refusal costs no request.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("SKILL.md", 10), ("huge.bin", gh._MAX_BUNDLE_BYTES + 1))
            },
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None
        assert not any("huge.bin" in u for u in fake.text_urls)
        assert not any("SKILL.md" in u for u in fake.text_urls if "/commits/" not in u)

    @pytest.mark.asyncio
    async def test_running_total_ceiling_refuses_the_bundle(self):
        # The cap is the RUNNING total, not a per-file verdict: three individually
        # legal files must not add up past it. Truncating at the ceiling would be
        # the same partial install by another route.
        half = "x" * (gh._MAX_BUNDLE_BYTES // 2 + 10)
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("a.md", 10), ("b.md", 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/a.md": half,
                "/b.md": half,
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_file_count_ceiling_refuses_the_bundle(self):
        # Taking the first N would drop the rest silently -- the same defect as a
        # dropped oversized file, so it gets the same answer.
        many = [(f"f{i:03d}.md", 10) for i in range(gh._MAX_BUNDLE_FILES + 20)]
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), *many)},
            text_routes={"/commits/": _COMMIT, ".md": "body"},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_a_bundle_at_the_file_ceiling_is_accepted(self):
        # The ceiling is a ceiling, not an off-by-one refusal.
        many = [(f"f{i:03d}.md", 10) for i in range(gh._MAX_BUNDLE_FILES - 1)]
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), *many)},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD, ".md": "body"},
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert len([p for p, _ in bundle if p != gh.PIN_FILENAME]) == gh._MAX_BUNDLE_FILES

    @pytest.mark.asyncio
    async def test_undecodable_blob_refuses_the_bundle(self):
        # The bundle contract is text, so a binary asset cannot ride it -- and a
        # skill whose instructions reference an image it did not get is broken in a
        # way the user only discovers later. Refuse, naming the file.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("logo.png", 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/logo.png": b"\x89PNG\r\n\x1a\n\xff\xfe",
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_a_failed_fetch_refuses_the_bundle(self):
        # Distinct from the case above: the file exists and we could not read it.
        # A text-only fetch collapses both to None, which is why blobs are read as
        # bytes and decoded here.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("rules/a.md", 10))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_case_colliding_paths_refuse_the_bundle(self):
        # macOS and Windows fold case: two entries here, one file there, and the
        # second write silently replaces the first. Re-importing reproduces it.
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("SKILL.md", 10), ("Rules.md", 10), ("rules.md", 10))
            },
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/Rules.md": "upper",
                "/rules.md": "lower",
            },
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_an_empty_instruction_file_refuses_the_bundle(self):
        # An empty SKILL.md installs a skill the loader can see and the agent
        # cannot use.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 0))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": ""},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_bundle_without_an_instruction_file_is_refused(self):
        # Every file failed to fetch: an install of just the pin record would
        # create a skill the loader cannot discover.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10))},
            text_routes={"/commits/": _COMMIT},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_bad_address_is_refused_before_any_request(self):
        fake = _Fake(json_routes={}, text_routes={})
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("../../etc") is None
        assert fake.json_urls == [] and fake.text_urls == []

    @pytest.mark.asyncio
    async def test_fetch_skill_content_prefers_skill_md(self):
        fake = _Fake(
            json_routes={
                "/git/trees/": _tree(("AGENTS.md", 10), ("README.md", 10), ("SKILL.md", 10))
            },
            text_routes={
                "/commits/": _COMMIT,
                "/SKILL.md": _SKILL_MD,
                "/AGENTS.md": _OTHER_SKILL_MD,
                "/README.md": "readme",
            },
        )
        with fake.install():
            content = await gh.GitHubRepoProvider().fetch_skill_content("acme/widgets")
        assert content == _SKILL_MD

    @pytest.mark.asyncio
    async def test_fetch_skill_content_falls_back_to_agents_md(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("AGENTS.md", 10), ("README.md", 10))},
            text_routes={
                "/commits/": _COMMIT,
                "/AGENTS.md": _OTHER_SKILL_MD,
                "/README.md": "readme",
            },
        )
        with fake.install():
            content = await gh.GitHubRepoProvider().fetch_skill_content("acme/widgets")
        assert content == _OTHER_SKILL_MD

    @pytest.mark.asyncio
    async def test_fetch_skill_content_returns_none_for_a_bad_address(self):
        fake = _Fake(json_routes={}, text_routes={})
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_content("nope") is None


# ---- trust posture --------------------------------------------------------


class TestNetworkGuards:
    """The SSRF screen and the redirect allowlist are shared code, but they are
    only a control for THIS provider if this provider's bindings really call
    them with its own allowlist and audit label."""

    def test_internal_addresses_are_blocked(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://0xa9fea9fe/",  # hex metadata endpoint
            "http://2852039166/",  # decimal metadata endpoint
            "http://127.0.0.1/x",
            "http://localhost/x",
            "http://10.0.0.5/",
            "http://[::1]/",
            "http:///no-host",
        ):
            assert gh._is_internal_url(url) is True, f"not blocked: {url}"

    def test_github_hosts_pass_the_screen(self):
        assert gh._is_internal_url("https://api.github.com/repos/a/b") is False
        assert gh._is_internal_url(f"https://raw.githubusercontent.com/a/b/{_COMMIT}/S.md") is False

    def test_allowlist_is_exact_and_https_only(self):
        assert gh._is_allowed_host("https://api.github.com/repos/a/b")
        assert gh._is_allowed_host("https://raw.githubusercontent.com/a/b/c/S.md")
        # Plain HTTP, a lookalike suffix and an arbitrary DNS name all fail.
        assert not gh._is_allowed_host("http://api.github.com/repos/a/b")
        assert not gh._is_allowed_host("https://api.github.com.evil.example/x")
        assert not gh._is_allowed_host("https://evil-api.github.com.co/x")
        assert not gh._is_allowed_host("https://metadata.google.internal/x")
        assert not gh._is_allowed_host("https://169.254.169.254/x")

    def test_the_allowlist_holds_only_the_hosts_this_module_fetches(self):
        # Exactly the two endpoints, and nothing inherited. skillsh's CDN hosts
        # (codeload/objects/media/github.com) are ITS bundle-download redirect
        # targets; neither the API nor the raw host has an observed redirect here,
        # so carrying them would widen the boundary for no reason. An unexpected
        # redirect failing the fetch is the answer we want.
        assert gh._ALLOWED_HOSTS == frozenset({"api.github.com", "raw.githubusercontent.com"})
        for inherited in (
            "https://codeload.github.com/a/b/tar.gz/c",
            "https://objects.githubusercontent.com/blob/x",
            "https://media.githubusercontent.com/media/x",
            "https://github.com/acme/widgets",
        ):
            assert not gh._is_allowed_host(inherited), inherited

    def test_a_display_url_is_built_but_never_fetched(self):
        # ``github.com`` is absent from the allowlist even though every row carries
        # a ``https://github.com/...`` repo_url -- that string is shown to a person,
        # never requested, so it needs no fetch permission.
        spec = gh.parse_repo_spec("acme/widgets")
        assert spec is not None
        assert gh._tree_url(spec, _COMMIT, "skills/reviewer").startswith("https://github.com/")
        assert not gh._is_allowed_host(gh._tree_url(spec, _COMMIT, "skills/reviewer"))

    def test_blocked_internal_ip_emits_a_sel_audit_for_this_provider(self):
        with patch.object(gh, "_audit_ssrf_blocked") as m:
            assert gh._is_internal_url("http://0xa9fea9fe/") is True
            assert m.called, "blocked SSRF attempt did not emit a SEL audit event"

    def test_allowed_host_does_not_emit_an_audit(self):
        with patch.object(gh, "_audit_ssrf_blocked") as m:
            assert gh._is_internal_url("https://api.github.com/x") is False
            assert not m.called

    def test_fetch_seams_pass_this_providers_allowlist(self):
        # A provider that reached the shared fetch with the WRONG allowlist would
        # look identical in every behavioural test above, so pin the wiring.
        with patch.object(gh._http, "sync_fetch_json", return_value={"ok": 1}) as m:
            gh._sync_fetch_json("https://api.github.com/x")
        assert m.call_args.kwargs["allowed_hosts"] is gh._ALLOWED_HOSTS
        assert m.call_args.kwargs["internal_check"] is gh._is_internal_url

        with patch.object(gh._http, "sync_fetch_text", return_value="x") as m:
            gh._sync_fetch_text("https://raw.githubusercontent.com/x", "text/plain")
        assert m.call_args.kwargs["allowed_hosts"] is gh._ALLOWED_HOSTS
        assert m.call_args.kwargs["internal_check"] is gh._is_internal_url

    def test_commit_resolution_asks_for_the_sha_media_type(self):
        # The commit OBJECT carries the commit's whole file list, which for a
        # large merge exceeds the 1 MiB cap and would fail to resolve a good ref.
        fake = _one_skill_repo()
        seen: list[str | None] = []

        def _text(url, accept=None):
            seen.append(accept)
            return fake.fetch_text(url, accept)

        async def _run():
            with patch.multiple(gh, _sync_fetch_json=fake.fetch_json, _sync_fetch_text=_text):
                await gh.GitHubRepoProvider().search("acme/widgets:skills/reviewer")

        import asyncio as _asyncio

        _asyncio.run(_run())
        assert "application/vnd.github.sha" in seen


class TestWriterCompatibility:
    """Every path this module accepts must be one the install writer will write.

    The writer (``discover.py``'s ``_write_bundle``) drops a path containing
    ``..``, one starting with ``/``, and one starting with ``./..`` -- silently,
    with no log. A path accepted here and dropped there installs a skill missing a
    file while reporting success, so the grammar has to be the STRICTER of the two.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "foo..bar.md",  # `..` inside a name, not as a segment
            "a/foo..bar/b.md",
            "..",
            "../escape.md",
            "a/../b.md",
            "/absolute.md",
            "./../x.md",
            "notes.",  # Windows strips a trailing dot -> two entries, one file
            "dir./a.md",
            "a//b.md",
            "a/",
            "/",
            "star*.md",
            "pipe|.md",
            "colon:name.md",
            'quote".md',
            "less<than.md",
            "back\\slash.md",
            "trailing space.md ",
            "tab\tname.md",
            # Refused by the allowlist rather than by a rule of their own. Each was
            # a separate check once; the shape covers all of them.
            "rules/my file.md",
            "\u65e5\u672c\u8a9e.md",
            "quote'.md",
            "semi;colon.md",
            "notes#1.md",
            "plus+one.md",
            "bracket[1].md",
            "ok (copy).md",
            ".gitkeep",
            "-dash.md",
            "_under.md",
            "a" * 65 + ".md",
            "a/b/c/d/e.md",
        ],
    )
    def test_paths_the_writer_would_drop_are_refused_here(self, path):
        assert gh._valid_relative_path(path) is False
        # The refusal has to NAME the problem: it reaches the user as the reason a
        # whole import was refused, not as a silent gap.
        assert gh._path_problem(path)

    @pytest.mark.parametrize(
        "path",
        [
            "SKILL.md",
            "rules/tests.md",
            "scripts/run.sh",
            "a/b/c/d.md",
            "v1.2.3/notes.md",
            "AGENTS.md",
            "rules/a-b_c.2.md",
            "a" * 64,
        ],
    )
    def test_ordinary_repository_paths_are_accepted(self, path):
        assert gh._valid_relative_path(path) is True
        assert gh._path_problem(path) == ""

    def test_the_root_is_accepted(self):
        assert gh._valid_relative_path("") is True

    def test_depth_and_component_length_are_bounded_by_the_one_rule(self):
        # No separate total-length rule exists, and none is needed: 64 characters per
        # segment times 4 levels bounds the whole path. ASCII-only means bytes and
        # characters are the same number, so there is no byte limit to state either.
        assert gh._valid_relative_path("a/" * 5000 + "b.md") is False
        assert gh._path_problem("a" * 65)
        assert gh._path_problem("a" * 64) == ""
        assert gh._path_problem("a/b/c/d.md") == ""
        assert "levels deep" in gh._path_problem("a/b/c/d/e.md")

    def test_an_absurdly_long_ref_is_refused(self):
        assert gh._valid_ref("a/" * 5000 + "b") is False


class TestMaterialisation:
    """Can this exact SET of files be created in one directory, anywhere?

    Every member of this family has one consequence: a set that validates and then
    fails mid-write. That matters because the install writer removes the previous
    skill BEFORE writing, and a write that raises escapes an ``asyncio.to_thread``
    with no handler -- old skill gone, new one partial, no recovery. Refusing up
    front is the only place it can be prevented, so the check is one question rather
    than a list of individually-discovered special cases.
    """

    @pytest.mark.parametrize(
        "path",
        ["CON.md", "con", "PRN.txt", "aux", "NUL", "com1.md", "lpt9.md", "rules/con.md"],
    )
    def test_reserved_device_names_are_refused_at_every_level(self, path):
        # Windows reserves these with ANY extension and in every directory, so the
        # write raises -- after the old skill has already been removed.
        assert gh._path_problem(path)

    @pytest.mark.parametrize(
        "path", ["console.md", "contents.md", "nullable.md", "conf/settings.md", "auxiliary.md"]
    )
    def test_names_merely_starting_with_a_reserved_word_are_fine(self, path):
        # The reservation is on the whole stem, not on a prefix: refusing
        # ``console.md`` would make an ordinary repository unimportable for nothing.
        assert gh._path_problem(path) == ""

    @pytest.mark.parametrize("path", ["con.d.md", "rules/nul.tar.gz"])
    def test_a_reserved_stem_with_several_extensions_is_still_reserved(self, path):
        # Windows reads the device name as the component before the FIRST dot, so
        # ``con.d.md`` is ``CON`` carrying an extension, not a name called "con.d".
        assert gh._path_problem(path)

    def test_a_name_used_as_both_file_and_directory_is_refused(self):
        # Legal in one git tree, impossible on a case-insensitive filesystem: the
        # mkdir raises because a file already holds the name.
        problem = gh._materialisation_problem(["Rules", "rules/a.md"])
        assert "file and a directory" in problem

    def test_the_conflict_is_caught_at_any_depth(self):
        assert gh._materialisation_problem(["a/Rules", "a/rules/b.md"])
        assert gh._materialisation_problem(["a/b/c", "a/B/c/d.md"])

    def test_case_variant_files_are_refused(self):
        assert "case is ignored" in gh._materialisation_problem(["Rules.md", "rules.md"])

    @pytest.mark.parametrize(
        "paths",
        [
            ["SKILL.md", "rules/a.md"],
            ["x/y.md", "X/z.md"],  # one directory, two distinct files -- fine
            ["a/b.md", "a/c.md"],
            ["SKILL.md", "rules/tests.md", "scripts/run.sh"],
        ],
    )
    def test_ordinary_file_sets_are_accepted(self, paths):
        assert gh._materialisation_problem(paths) == ""

    def test_a_per_path_problem_is_reported_through_the_set_check(self):
        # One refusal covers both questions, so a caller asks once.
        assert "cannot be imported" in gh._materialisation_problem(["SKILL.md", "bad:name.md"])

    @pytest.mark.asyncio
    async def test_a_prefix_conflict_refuses_the_bundle(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("Rules", 10), ("rules/a.md", 10))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_a_reserved_name_refuses_the_bundle(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 10), ("CON.md", 10))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None


class TestInstructionFileMustBeUsable:
    """An installed skill the agent cannot use is not a successful install."""

    @pytest.mark.asyncio
    async def test_an_empty_skill_md_beside_a_good_agents_md_is_refused(self):
        # The writer copies AGENTS.md to SKILL.md only when SKILL.md is ABSENT, so a
        # present-but-empty SKILL.md shadows a perfectly good AGENTS.md. "Either one
        # has content" passes this and installs a skill that does nothing.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 0), ("AGENTS.md", 40))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": "", "/AGENTS.md": _SKILL_MD},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_a_whitespace_only_skill_md_is_refused(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("SKILL.md", 3))},
            text_routes={"/commits/": _COMMIT, "/SKILL.md": "  \n\t "},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_an_empty_agents_md_alone_is_refused(self):
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("AGENTS.md", 0))},
            text_routes={"/commits/": _COMMIT, "/AGENTS.md": ""},
        )
        with fake.install():
            assert await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets") is None

    @pytest.mark.asyncio
    async def test_a_good_agents_md_alone_still_imports(self):
        # The guard must not break the convention it exists to protect.
        fake = _Fake(
            json_routes={"/git/trees/": _tree(("AGENTS.md", 40))},
            text_routes={"/commits/": _COMMIT, "/AGENTS.md": _SKILL_MD},
        )
        with fake.install():
            bundle = await gh.GitHubRepoProvider().fetch_skill_bundle("acme/widgets")
        assert bundle is not None
        assert [p for p, _ in bundle] == ["AGENTS.md", gh.PIN_FILENAME]


class TestInstallKey:
    """The local key comes from ``install_slug``, not from the address.

    The handler's default is ``_slugify(id)``, which lowercases and folds ``/``,
    ``@`` and ``:`` all onto ``-``. That is not injective over these ids, so two
    distinct GitHub skills could share one on-disk key and installing the second
    would delete the first. Bounding the address length does not help: the
    collision is in the character folding, not in the length.
    """

    def _key(self, path: str, *, owner: str = "acme", repo: str = "widgets", commit: str = _COMMIT):
        suffix = f":{path}" if path else ""
        return gh.GitHubRepoProvider().install_slug(f"{owner}/{repo}@{commit}{suffix}")

    def test_a_dash_and_a_separator_do_not_share_a_key(self):
        # `foo-bar` and `foo/bar` both slugify to "...foo-bar..." under the
        # handler's default. They must not share a key.
        assert self._key("foo-bar") != self._key("foo/bar")

    def test_case_is_part_of_the_identity(self):
        # GitHub paths are case-sensitive; _slugify lowercases. Two real skills
        # differing only in case are two skills.
        assert self._key("Foo/bar") != self._key("foo/bar")
        assert self._key("", repo="Widgets") != self._key("", repo="widgets")
        assert self._key("", owner="Acme") != self._key("", owner="acme")

    def test_different_repos_with_the_same_skill_name_do_not_collide(self):
        assert self._key("skills/reviewer", repo="widgets") != self._key(
            "skills/reviewer", repo="gadgets"
        )
        assert self._key("skills/reviewer", owner="acme") != self._key(
            "skills/reviewer", owner="other"
        )

    def test_the_commit_is_not_part_of_the_identity(self):
        # A commit is the VERSION, not the identity. Re-importing the same skill at
        # a newer commit has to land on the SAME key so it meets the install
        # handler's 409 and the user is asked to confirm an update -- otherwise
        # "re-import to update" would silently accumulate copies instead.
        assert self._key("skills/reviewer", commit=_COMMIT) == self._key(
            "skills/reviewer", commit="f" * 40
        )

    def test_the_key_survives_the_handlers_slugify_unchanged(self):
        # If slugify altered or truncated it, the collision resistance would be
        # gone again -- so the key is built from characters slugify keeps verbatim.
        from kiro_crew.dashboard.handlers.discover import _SAFE_SLUG_RE, _slugify

        for path in ("", "skills/reviewer", "a/b/c/deeply-nested", "UPPER/Mixed_Case.d"):
            key = self._key(path)
            assert _slugify(key) == key, key
            assert _SAFE_SLUG_RE.match(key), key
            assert len(key) <= 64, key

    def test_the_longest_allowed_address_still_yields_a_short_key(self):
        # The key's size comes from the digest, not the address, so even the longest
        # address the allowlist admits produces a short, distinct key.
        key = self._key("/".join(["d" * 64] * 4), owner="o" * 39, repo="r" * 90)
        assert len(key) <= 64
        assert key != self._key("/".join(["e" * 64] * 4), owner="o" * 39, repo="r" * 90)

    def test_the_key_is_readable(self):
        # The digest carries uniqueness; the label is there so a directory listing
        # is legible to a human.
        assert self._key("skills/reviewer").startswith("reviewer-")
        assert self._key("").startswith("widgets-")

    @pytest.mark.parametrize("repo", [".github", "_internal", "...", "__", ".Dot.Repo.", "_x_"])
    def test_a_dotfile_style_repo_name_still_yields_an_installable_key(self, repo):
        # The key must START with an alphanumeric or the install handler's
        # _SAFE_SLUG_RE refuses it -- a 400 on a skill that imports fine. Repos named
        # `.github` are common, and a root skill in one has an empty path, so the
        # label comes straight from the repo name.
        from kiro_crew.dashboard.handlers.discover import _SAFE_SLUG_RE, _slugify

        key = gh.GitHubRepoProvider().install_slug(f"acme/{repo}@{_COMMIT}")
        assert key, repo
        assert _SAFE_SLUG_RE.match(key), key
        assert _slugify(key) == key, key

    def test_a_label_that_survives_nothing_falls_back_to_the_digest(self):
        # Digest-only is still a valid key: hex always starts with an alphanumeric.
        from kiro_crew.dashboard.handlers.discover import _SAFE_SLUG_RE

        key = gh.GitHubRepoProvider().install_slug(f"acme/...@{_COMMIT}")
        assert _SAFE_SLUG_RE.match(key)
        assert len(key) == gh._KEY_DIGEST_CHARS

    def test_an_unparseable_id_yields_no_key(self):
        # The handler falls back to its own derivation rather than installing
        # something named after a parse failure.
        assert gh.GitHubRepoProvider().install_slug("not an address") == ""
        assert gh.GitHubRepoProvider().install_slug("") == ""


class _Hostile:
    """A provider returning whatever it likes from ``install_slug``."""

    def __init__(self, value: object) -> None:
        self._value = value

    def install_slug(self, skill_id: str) -> object:
        return self._value


class TestInstallSlugHook:
    """The handler's side of the same fix.

    ``_install_slug`` lives in the discover handler but the GitHub provider is its
    only caller, so the two are tested together. What matters is that it is purely
    ADDITIVE: a provider without the method -- every provider but this one --
    behaves exactly as before.
    """

    def test_a_provider_without_the_method_keeps_the_old_derivation(self):
        from kiro_crew.dashboard.handlers.discover import _install_slug, _slugify

        class _NoHook:
            pass

        assert _install_slug(_NoHook(), "owner/repo/skill", "owner/repo/skill") == _slugify(
            "owner/repo/skill"
        )
        assert _install_slug(None, "a/b", "a/b") == _slugify("a/b")

    def test_the_skillsh_provider_is_unaffected(self):
        # The regression that would matter most: the other built-in must not change.
        from kiro_crew.dashboard.handlers.discover import _install_slug, _slugify
        from kiro_crew.skill_providers.skillsh import SkillsShProvider

        skill_id = "vercel/ai/react-perf"
        assert _install_slug(SkillsShProvider(), skill_id, skill_id) == _slugify(skill_id)

    def test_the_providers_key_is_used_when_it_supplies_one(self):
        from kiro_crew.dashboard.handlers.discover import _install_slug

        provider = gh.GitHubRepoProvider()
        skill_id = f"acme/widgets@{_COMMIT}:skills/reviewer"
        assert _install_slug(provider, skill_id, skill_id) == provider.install_slug(skill_id)

    @pytest.mark.parametrize("bad", [None, 7, b"bytes", [], ""])
    def test_a_useless_return_falls_back_instead_of_failing(self, bad):
        from kiro_crew.dashboard.handlers.discover import _install_slug, _slugify

        class _Bad:
            def install_slug(self, skill_id):
                return bad

        assert _install_slug(_Bad(), "a/b", "a/b") == _slugify("a/b")

    def test_a_raising_descriptor_falls_back_instead_of_breaking_discovery(self):
        # Reading the attribute EXECUTES the property, so this fails on getattr, not
        # on call -- a guard wrapped around the call alone never sees it, and the
        # whole discover response 500s.
        from kiro_crew.dashboard.handlers.discover import _install_slug, _slugify

        class _RaisingDescriptor:
            @property
            def install_slug(self):
                raise RuntimeError("descriptor exploded")

        assert _install_slug(_RaisingDescriptor(), "a/b", "a/b") == _slugify("a/b")

    def test_a_non_callable_attribute_falls_back(self):
        from kiro_crew.dashboard.handlers.discover import _install_slug, _slugify

        class _NotCallable:
            install_slug = "not a method"

        assert _install_slug(_NotCallable(), "a/b", "a/b") == _slugify("a/b")

    def test_a_raising_hook_falls_back_instead_of_breaking_install(self):
        # Provider code must not be able to break installing.
        from kiro_crew.dashboard.handlers.discover import _install_slug, _slugify

        class _Boom:
            def install_slug(self, skill_id):
                raise RuntimeError("provider exploded")

        assert _install_slug(_Boom(), "a/b", "a/b") == _slugify("a/b")

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "/absolute",
            "a/b",
            "..",
            ".hidden",
            "-leading-dash",
            "C:\\windows\\system32",
            "x" * 400,
        ],
    )
    def test_a_hostile_return_never_becomes_a_path(self, hostile):
        # A provider NAMES a key; it does not get to decide what a key may be. Two
        # things hold that line and both belong to the handler, not the provider:
        # slugify strips every separator, and the call site then demands
        # _SAFE_SLUG_RE. So whatever comes back, the key is one safe segment or the
        # install is refused -- never a path.
        from kiro_crew.dashboard.handlers.discover import _SAFE_SLUG_RE, _install_slug

        slug = _install_slug(_Hostile(hostile), "a/b", "a/b")
        assert "/" not in slug and "\\" not in slug
        if _SAFE_SLUG_RE.match(slug):
            # Accepted: then it is a single segment that cannot traverse.
            assert ".." not in slug
            assert len(slug) <= 128
        # Otherwise the install handler answers 400 before touching the disk.

    def test_a_traversal_return_is_refused_by_the_call_site_gate(self):
        # The concrete half of the test above: slugify leaves "..-..-etc-passwd",
        # which fails _SAFE_SLUG_RE because a slug must START with an alphanumeric.
        # That refusal is what the install handler turns into a 400.
        from kiro_crew.dashboard.handlers.discover import _SAFE_SLUG_RE, _install_slug

        slug = _install_slug(_Hostile("../../etc/passwd"), "a/b", "a/b")
        assert slug == "..-..-etc-passwd"
        assert _SAFE_SLUG_RE.match(slug) is None


class TestInstallNamespace:
    """Where an import lands, expressed against the install handler's own key
    derivation rather than restated -- decision: an imported skill may never
    shadow a shipped one."""

    def test_key_is_provider_prefixed_and_cannot_shadow_a_shipped_skill(self):
        from kiro_crew.dashboard.handlers.discover import _SAFE_SLUG_RE, _slugify

        provider = gh.GitHubRepoProvider()
        skill_id = f"acme/widgets@{_COMMIT[:7]}:skills/reviewer"
        slug = _slugify(skill_id)
        # The handler demands a separator-free slug, so the key can only ever be
        # one segment under the provider's own directory.
        assert _SAFE_SLUG_RE.match(slug)
        key = f"{provider.name}/{slug}"
        assert key.startswith("github/")
        assert key.count("/") == 1
        # A shipped skill's key has no "github/" prefix, so no import can occupy
        # it; re-importing the same address hits the handler's 409 instead.
        assert slug == "acme-widgets-0a1b2c3-skills-reviewer"

    def test_two_repos_with_the_same_skill_name_do_not_collide(self):
        from kiro_crew.dashboard.handlers.discover import _slugify

        a = _slugify(f"acme/widgets@{_COMMIT[:7]}:skills/reviewer")
        b = _slugify(f"other/tools@{_COMMIT[:7]}:skills/reviewer")
        assert a != b

    def test_the_provider_module_is_not_imported_until_the_policy_admits_it(self):
        # An optional subsystem must cost the gateway's import path nothing when the
        # deployment's policy refuses it. Two things make that true and BOTH are
        # load-bearing: the import sits inside the policy check in
        # ``_build_registry``, and the package ``__init__`` does not re-export the
        # provider -- a re-export would pull it in for every consumer of the package
        # and defeat the deferral entirely, which is what it did at first.
        import subprocess
        import sys

        probe = (
            "import sys;"
            "from kiro_crew.dashboard.handlers import discover;"
            "print('github' if 'kiro_crew.skill_providers.github' in sys.modules else 'absent')"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        ).stdout
        assert "absent" in out, out

    def test_the_gated_identity_matches_the_provider(self):
        # ``_build_registry`` consults the policy with literals so it can decide
        # BEFORE importing. Pinned here: gating one identity while registering
        # another would make the policy allowlist meaningless.
        from kiro_crew.dashboard.handlers.discover import _GITHUB_API_BASE, _GITHUB_NAME

        provider = gh.GitHubRepoProvider()
        assert _GITHUB_NAME == provider.name
        assert _GITHUB_API_BASE == provider.api_base

    def test_registry_registers_the_provider_under_its_vetted_name(self):
        from kiro_crew.dashboard.handlers import discover

        registry = discover._build_registry()
        assert "github" in registry.provider_names
        assert isinstance(registry.get("github"), gh.GitHubRepoProvider)
