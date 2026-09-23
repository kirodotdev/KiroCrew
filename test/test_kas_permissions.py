"""``allowedTools`` -> KAS ``permissions``, and the two places it has to land.

The translation is not a preference: each assertion here pins a fact read off
KAS's own policy engine, and getting one wrong is silent in both directions.

* Its evaluator resolves an unmatched request to ``ask``. A capability this
  module fails to emit therefore keeps prompting (safe), and one it emits too
  broadly stops prompting (not safe) — so the vocabulary is pinned, and an
  unclassifiable entry must produce NO rule rather than a guess.
* ``match`` omitted means every resource. That is what a tool-name allowlist
  entry means, so the omission is load-bearing rather than an oversight, and a
  test that expected ``match: ["**"]`` would be pinning the wrong thing.
* An MCP tool is addressed as ``<server>/<tool>``, which is what makes
  per-server and per-action grants expressible at all.
* And the field's PRESENCE decides whether KAS will load the on-disk profile,
  which is why the disk writer keeps an empty policy where the wire projection
  drops the key.
* But the field only reaches disk when the installed kiro-cli accepts it. That
  binary validates specs with ``deny_unknown_fields`` and serves KAS as well as
  its own backend, so a release predating the field refuses the whole spec and
  cannot be the relay the field exists for.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.kas_permissions import (
    CAPABILITY_BY_TOOL,
    CEILING_REFS_BY_CAPABILITY,
    KAS_CAPABILITIES,
    META_CAPABILITY_EXPANSION,
    WITHHELD_CAPABILITIES,
    WITHHELD_FROM_AUTO_APPROVE,
    allowed_tools_to_permissions,
    merge_user_permissions,
)
from kiro_crew.agent import _seed_kas_permissions
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION, spec_permissions_supported

#: A release that accepts the field, and one that refuses it. Expressed against
#: the floor rather than as literals so raising the floor cannot leave a test
#: asserting the old boundary.
_ACCEPTS = SPEC_PERMISSIONS_MIN_VERSION
_REFUSES = (SPEC_PERMISSIONS_MIN_VERSION[0], SPEC_PERMISSIONS_MIN_VERSION[1] - 1, 0)


@pytest.fixture
def installed_cli(monkeypatch):
    """Pin what ``_seed_kas_permissions`` believes the installed kiro-cli is.

    Patched at ``kiro_crew.kiro_cli`` because the seed imports the name
    function-locally, so the lookup happens in the owning module at call time.
    Without this the answer is whatever the test HOST has installed -- which on
    CI is nothing, and "unknown" reads as refusing.
    """

    def _pin(version):
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.installed_kiro_cli_version",
            lambda: version,
        )

    return _pin


def _rule(policy: dict, capability: str) -> dict:
    """The single rule for *capability*, asserting there is exactly one."""
    found = [r for r in policy["rules"] if r["capability"] == capability]
    assert len(found) == 1, f"expected one {capability} rule, got {found}"
    return found[0]


class TestBuiltinToolsBecomeCapabilities:
    def test_a_named_tool_maps_to_its_capability(self):
        policy = allowed_tools_to_permissions(["web_fetch"])
        assert policy == {"rules": [{"capability": "web_fetch", "effect": "allow"}]}

    def test_match_is_omitted_because_a_tool_entry_carries_no_resource_scope(self):
        """KAS reads a missing ``match`` as every resource — the intended meaning."""
        assert "match" not in _rule(allowed_tools_to_permissions(["web_search"]), "web_search")

    def test_rules_are_ordered_deterministically(self):
        """Two rebuilds of the same list must produce byte-identical output."""
        entries = ["web_search", "invoke_sub_agent", "web_fetch"]
        first = allowed_tools_to_permissions(entries)
        second = allowed_tools_to_permissions(list(reversed(entries)))
        assert first == second


class TestTheShellAndFilesystemFamiliesAreRefused:
    """The one place this module declines to translate something it could.

    Auto-approval is not "one fewer prompt", it is the ABSENCE of a permission
    request — and Crew's deny floor and sensitive-path check run on that request.
    A rule derived from a tool-name allowlist is also unscoped, because the
    allowlist carries no resource pattern, so the grant would be "any command" /
    "any path". Refusing means no rule, and no rule means prompt.
    """

    @pytest.mark.parametrize(
        "tool",
        sorted(WITHHELD_FROM_AUTO_APPROVE),
    )
    def test_a_withheld_tool_produces_no_rule(self, tool):
        assert allowed_tools_to_permissions([tool]) is None

    def test_a_withheld_tool_does_not_suppress_the_rest_of_the_list(self):
        policy = allowed_tools_to_permissions(["execute_bash", "web_fetch"])
        assert policy["rules"] == [{"capability": "web_fetch", "effect": "allow"}]

    def test_the_refusal_is_reported_at_info_because_it_reverses_the_spec(self, caplog):
        """Distinct from an unmappable entry: this one the spec explicitly asked for."""
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_permissions"):
            allowed_tools_to_permissions(["execute_bash"], agent_id="kirocrew")
        assert "not auto-approving execute_bash" in caplog.text

    def test_no_withheld_tool_is_also_in_the_capability_table(self):
        """A tool in both places would translate anyway; the two must not overlap."""
        assert WITHHELD_FROM_AUTO_APPROVE.isdisjoint(CAPABILITY_BY_TOOL)


class TestMcpEntries:
    def test_a_bare_server_becomes_a_one_level_glob(self):
        policy = allowed_tools_to_permissions(["@kirocrew-core"])
        assert _rule(policy, "mcp")["match"] == ["kirocrew-core/*"]

    def test_a_named_action_stays_exact(self):
        policy = allowed_tools_to_permissions(["@kirocrew-cron/cron_list"])
        assert _rule(policy, "mcp")["match"] == ["kirocrew-cron/cron_list"]

    def test_all_servers_share_one_rule(self):
        policy = allowed_tools_to_permissions(["@a", "@b"])
        assert _rule(policy, "mcp")["match"] == ["a/*", "b/*"]

    def test_a_server_wildcard_absorbs_its_own_per_tool_entries(self):
        """The real spec carries both; emitting both is misleading to read.

        A reviewer comparing policy against allowlist should not have to work out
        that one line already subsumes another.
        """
        policy = allowed_tools_to_permissions(
            ["@kirocrew-cron/cron_list", "@kirocrew-cron/cron_pause", "@kirocrew-cron"]
        )
        assert _rule(policy, "mcp")["match"] == ["kirocrew-cron/*"]

    def test_another_servers_per_tool_entry_is_not_absorbed(self):
        policy = allowed_tools_to_permissions(["@a", "@b/one"])
        assert _rule(policy, "mcp")["match"] == ["a/*", "b/one"]

    @pytest.mark.parametrize("bad", ["@", "@/tool"])
    def test_an_entry_naming_no_server_grants_nothing(self, bad):
        """Better to prompt than to emit a pattern that could match anything."""
        assert allowed_tools_to_permissions([bad]) is None


class TestToolGlobsTravelAsWritten:
    """A tool-part glob means the same thing on both backends, so it is relayed.

    kiro-cli documents ``@server/read_*`` in ``allowedTools`` (``*`` and ``?``
    over the tool name) and KAS's resource matcher reads ``server/read_*`` the
    same way. Dropping it made one line of text a grant on kiro-cli and a prompt
    on KAS — a user who had auto-approved ``@srv/query_*`` on the CLI got asked
    for every ``query_`` call the moment they switched backend. That asymmetry is
    the defect; the translation is the one that is faithful.
    """

    @pytest.mark.parametrize(
        ("entry", "pattern"),
        [
            ("@ent-a2rm/a2rm___query_*", "ent-a2rm/a2rm___query_*"),
            ("@srv/*_get", "srv/*_get"),
            ("@srv/get_*_info", "srv/get_*_info"),
            ("@srv/cron_?", "srv/cron_?"),
        ],
    )
    def test_a_tool_part_glob_becomes_the_same_kas_pattern(self, entry, pattern):
        policy = allowed_tools_to_permissions([entry])
        assert _rule(policy, "mcp")["match"] == [pattern]

    def test_an_explicit_every_tool_glob_reads_as_the_bare_server(self):
        """``@srv/*`` and ``@srv`` are the same grant on kiro-cli; one pattern."""
        policy = allowed_tools_to_permissions(["@srv/*", "@srv"])
        assert _rule(policy, "mcp")["match"] == ["srv/*"]

    def test_the_server_wildcard_still_absorbs_a_tool_glob_beside_it(self):
        policy = allowed_tools_to_permissions(["@srv/query_*", "@srv"])
        assert _rule(policy, "mcp")["match"] == ["srv/*"]

    def test_it_lands_on_the_wire(self):
        """The projection is where the user actually felt the drop."""
        spec = {"prompt": "p", "tools": ["@ent-a2rm"], "allowedTools": ["@ent-a2rm/a2rm___query_*"]}
        agent = to_client_custom_agent("kirocrew", spec, "p")
        assert agent["permissions"] == {
            "rules": [
                {"capability": "mcp", "match": ["ent-a2rm/a2rm___query_*"], "effect": "allow"}
            ]
        }


class TestTranslationNeverWidensAGrant:
    """A glob over the SERVER must not become a glob in the projected policy.

    ``@*`` translated naively becomes the pattern ``*/*``, which KAS resolves as
    every tool on every server, and a server-part glob is the one shape whose
    kiro-cli reading this module cannot vouch for. Bracket, brace and negation
    syntax in the tool part is refused on the same grounds: KAS honours it and
    kiro-cli does not document it, so the same text could mean two widths. The
    widening is what is under test, not the syntax.
    """

    @pytest.mark.parametrize(
        "entry",
        [
            "@*",
            "@*/*",
            "@kirocrew-*",
            "@kirocrew-*/*",
            "@*/status",
            "@kirocrew-core/[abc]",
            "@kirocrew-core/{a,b}",
            "@kirocrew-core/!x",
            "@{a,b}",
            "@!kirocrew-core",
        ],
    )
    def test_a_server_glob_or_unshared_syntax_yields_no_rule(self, entry):
        assert allowed_tools_to_permissions([entry]) is None

    def test_a_glob_does_not_suppress_the_literal_entries_beside_it(self):
        policy = allowed_tools_to_permissions(["@*", "@kirocrew-core", "web_fetch"])
        assert _rule(policy, "mcp")["match"] == ["kirocrew-core/*"]
        assert _rule(policy, "web_fetch")["effect"] == "allow"

    def test_a_rejected_glob_is_explainable_from_the_log(self, caplog):
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_permissions"):
            allowed_tools_to_permissions(["@*"], agent_id="a")
        assert "@*" in caplog.text


class TestUnclassifiableEntriesFailClosed:
    @pytest.mark.parametrize("entry", ["introspect", "session", "report", "tool_search"])
    def test_a_tool_with_no_kas_capability_emits_no_rule(self, entry):
        """It keeps prompting, which is the same as having no policy for it."""
        assert allowed_tools_to_permissions([entry]) is None

    def test_it_does_not_suppress_the_entries_that_do_map(self):
        policy = allowed_tools_to_permissions(["introspect", "web_fetch"])
        assert policy["rules"] == [{"capability": "web_fetch", "effect": "allow"}]

    def test_the_names_are_reported_so_a_missing_grant_is_explainable(self, caplog):
        # Names the logger: left to the root logger this passes alone and fails in
        # the full suite, once something else has raised the package level.
        with caplog.at_level("DEBUG", logger="kiro_crew.acp.kas_permissions"):
            allowed_tools_to_permissions(["introspect"], agent_id="kirocrew")
        assert "introspect" in caplog.text

    @pytest.mark.parametrize("bad", [None, "fs_read", 42, {}])
    def test_a_non_list_is_not_coerced(self, bad):
        assert allowed_tools_to_permissions(bad) is None

    @pytest.mark.parametrize("junk", [[""], ["   "], [None, 7], []])
    def test_no_usable_entries_yields_no_policy_rather_than_an_empty_one(self, junk):
        """Absent and empty are different claims; only the caller knows which fits."""
        assert allowed_tools_to_permissions(junk) is None


class TestTheRealAllowlist:
    """The spec Crew actually ships, so a drift in it shows up here.

    Pinned as behaviour rather than a literal: what matters is which capabilities
    end up auto-approved and — more importantly — which do not.
    """

    ALLOWED = [
        "web_fetch",
        "web_search",
        "introspect",
        "session",
        "report",
        "@kirocrew-cron/cron_list",
        "@kirocrew-cron/cron_pause",
        "@kirocrew-core",
        "@notes-mcp",
        "@tickets-mcp",
        "@kirocrew-cron",
        "@weather-mcp",
    ]

    def test_the_excluded_tools_gain_no_grant(self):
        """``execute_bash``/``fs_write``/``code`` are held back on purpose.

        They are in ``tools`` but NOT ``allowedTools``, and with no rule KAS
        resolves them to ``ask`` — which is the whole point of the exclusion.
        """
        policy = allowed_tools_to_permissions(self.ALLOWED)
        granted = {r["capability"] for r in policy["rules"]}
        assert granted == {"mcp", "web_fetch", "web_search"}
        assert "shell" not in granted
        assert "fs_write" not in granted
        assert "fs_read" not in granted

    def test_the_computer_use_server_is_not_auto_approved(self):
        """It is absent from the allowlist, and it can drive a logged-in app."""
        policy = allowed_tools_to_permissions(self.ALLOWED)
        assert not any("computer" in p for p in _rule(policy, "mcp")["match"])


class TestTheDiskWriter:
    """What the agent spec on disk must say, which is NOT what the wire says.

    KAS classifies a JSON agent profile that carries kiro-cli-only fields and no
    KAS field as written for the other runtime, and skips it outright. So on disk
    the presence of ``permissions`` is what keeps the agent loadable at all —
    independently of whether it grants anything.
    """

    @pytest.fixture(autouse=True)
    def _accepting_cli(self, installed_cli):
        """Every case here is about WHAT is written, not WHETHER."""
        installed_cli(_ACCEPTS)

    @staticmethod
    def _config(**over) -> dict:
        base: dict = {"name": "kirocrew", "tools": ["fs_read"], "allowedTools": ["web_fetch"]}
        base.update(over)
        return base

    def test_the_policy_is_derived_from_the_allowlist(self):
        config = self._config()
        _seed_kas_permissions(config)
        assert config["permissions"] == {"rules": [{"capability": "web_fetch", "effect": "allow"}]}

    def test_an_empty_policy_is_still_written_so_the_profile_stays_loadable(self):
        """Dropping the key would silently un-register the agent on KAS.

        ``{"rules": []}`` is both true (nothing is pre-approved) and enough to
        keep the file from being classified as kiro-cli-only. This is the one
        place the disk and wire behaviours deliberately differ.
        """
        config = self._config(allowedTools=[])
        _seed_kas_permissions(config)
        assert config["permissions"] == {"rules": []}

    def test_an_allowlist_of_only_unmappable_tools_still_marks_the_profile(self):
        config = self._config(allowedTools=["introspect", "session"])
        _seed_kas_permissions(config)
        assert config["permissions"] == {"rules": []}

    @pytest.mark.parametrize(
        "existing",
        [
            {"rules": [{"capability": "shell", "match": ["rm -rf *"], "effect": "deny"}]},
            {"rules": [{"capability": "shell", "effect": "allow"}]},
            {"rules": [], "policies": ["team-base"]},
            {"rules": []},
            {},
        ],
        ids=["deny", "blanket-allow", "policy-bundle", "empty-rules", "empty-block"],
    )
    def test_an_existing_block_is_never_touched_whatever_its_shape(self, existing):
        """Once the key exists it belongs to whoever edits the file.

        Recognising Crew's own output by shape and regenerating that was the
        first design and is gone: a blanket ``allow`` is exactly what a user
        writes too, so the rule that keeps a derived policy current is the same
        rule that silently destroys a hand-written one.
        """
        config = self._config(allowedTools=["@srv"], permissions=dict(existing))
        _seed_kas_permissions(config)
        assert config["permissions"] == existing

    def test_a_stale_block_cannot_put_back_a_grant_the_allowlist_dropped(self):
        """Seeding-not-refreshing means the file can lag ``allowedTools``.

        Which is why the LIST owns a grant it can express itself: the wire derives
        from it afresh every session, and a block rule of a shape the derivation
        could have emitted does not travel beside it. The stale ``web_fetch`` here is
        exactly what a seeder wrote before the list changed.
        """
        config = self._config(
            allowedTools=["@srv"],
            permissions={"rules": [{"capability": "web_fetch", "effect": "allow"}]},
        )
        _seed_kas_permissions(config)
        assert config["permissions"]["rules"] == [{"capability": "web_fetch", "effect": "allow"}]
        assert to_client_custom_agent("kirocrew", {**config, "prompt": "p"}, "p")[
            "permissions"
        ] == {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}

    def test_a_stale_block_cannot_outlive_the_ceiling_that_now_withholds_it(self):
        """The half of "bounded" that matters: the ceiling is re-asked at
        projection time for the block exactly as it is for the derivation."""
        config = self._config(
            allowedTools=["@srv"],
            permissions={"rules": [{"capability": "web_fetch", "effect": "allow"}]},
        )
        _seed_kas_permissions(config)
        import kiro_crew.acp.kas_agents as kas_agents

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(kas_agents, "may_skip_gate_now", lambda ref: ref == "@srv")
            out = to_client_custom_agent("kirocrew", {**config, "prompt": "p"}, "p")
        assert out["permissions"]["rules"] == [
            {"capability": "mcp", "match": ["srv/*"], "effect": "allow"}
        ]

    def test_the_cli_only_key_is_kept_so_kiro_cli_still_works(self):
        """The spec has to serve both runtimes: KAS ignores what it does not use."""
        config = self._config()
        _seed_kas_permissions(config)
        assert config["allowedTools"] == ["web_fetch"]

    def test_it_is_idempotent(self):
        config = self._config()
        _seed_kas_permissions(config)
        once = dict(config["permissions"])
        _seed_kas_permissions(config)
        assert config["permissions"] == once


class TestTheInstalledCliDecidesWhetherTheFieldIsWrittenAtAll:
    """The field is a total loss on a CLI that refuses it.

    kiro-cli validates agent specs with serde ``deny_unknown_fields``, so a
    release whose schema predates ``permissions`` does not ignore the key: it
    refuses the whole file, drops the agent from its table, and every Kiro Crew
    MCP server is absent from the session. The user sees "agent specs rejected"
    and has no working tools. Nothing is given up by withholding the field
    there, because the same binary serves KAS, so a release that cannot read the
    key cannot be the relay that honours it either.
    """

    @staticmethod
    def _config(**over) -> dict:
        base: dict = {"name": "kirocrew", "tools": ["fs_read"], "allowedTools": ["web_fetch"]}
        base.update(over)
        return base

    def test_an_accepting_cli_gets_the_block(self, installed_cli):
        installed_cli(_ACCEPTS)
        config = self._config()
        _seed_kas_permissions(config)
        assert config["permissions"] == {"rules": [{"capability": "web_fetch", "effect": "allow"}]}

    def test_a_refusing_cli_gets_no_block(self, installed_cli):
        installed_cli(_REFUSES)
        config = self._config()
        _seed_kas_permissions(config)
        assert "permissions" not in config

    def test_an_unknown_version_is_not_treated_as_new_enough(self, installed_cli):
        """Absent, unspawnable or unparseable all arrive here as None.

        The two losses are not symmetric: guessing "new enough" costs the whole
        spec, guessing "too old" costs the KAS mode listing.
        """
        installed_cli(None)
        config = self._config()
        _seed_kas_permissions(config)
        assert "permissions" not in config

    @pytest.mark.parametrize("version", [_REFUSES, None], ids=["refusing", "unknown"])
    def test_a_block_already_on_disk_is_kept_whatever_the_cli_says(self, installed_cli, version):
        """Seed, never refresh -- and never remove either.

        A hand-written policy is the user's; the gate decides only whether a
        NEW block is written. A spec an older release already refuses is
        repaired by ``setup --agent-only --clean``, which rebuilds from defaults
        and, through this same gate, leaves the key out.
        """
        installed_cli(version)
        policy = {"rules": [{"capability": "shell", "match": ["ls *"], "effect": "allow"}]}
        config = self._config(permissions=dict(policy))
        _seed_kas_permissions(config)
        assert config["permissions"] == policy

    def test_an_accepting_cli_still_never_edits_an_existing_block(self, installed_cli):
        """The seed-never-refresh rule is unchanged where the field is legal."""
        installed_cli(_ACCEPTS)
        existing = {"rules": [{"capability": "shell", "effect": "deny"}]}
        config = self._config(permissions=dict(existing))
        _seed_kas_permissions(config)
        assert config["permissions"] == existing

    @pytest.mark.parametrize(
        "version,supported",
        [
            (None, False),
            ((2, 10, 0), False),
            (_REFUSES, False),
            (_ACCEPTS, True),
            ((99, 0, 0), True),
        ],
        ids=["unknown", "reported-2.10.0", "just-below-floor", "at-floor", "far-above"],
    )
    def test_the_gate_itself_is_pure_and_fails_closed(self, version, supported):
        assert spec_permissions_supported(version) is supported


class TestTheVersionProbeItself:
    """Every test above pins the probe's answer. These pin the probe.

    The probe swallows every failure into ``None`` by design, which is also what
    would hide a broken probe: a missing import or a wrong spawn keyword raises
    inside the ``try`` and reads as "unknown", and every caller then withholds
    the field on a CLI that accepts it. So the spawn is observed, not stubbed.
    """

    @pytest.fixture
    def pinned(self, monkeypatch, tmp_path):
        """A pinned binary that exists on disk, and a recorder for the spawn."""
        import subprocess

        from kiro_crew import kiro_cli

        binary = tmp_path / "kiro-cli"
        binary.write_text("")
        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (str(binary), False))
        monkeypatch.setattr(kiro_cli, "_version_cache", {})
        calls: list[dict] = []

        def _install(stdout: str, returncode: int = 0):
            def _run(argv, **kwargs):
                calls.append({"argv": argv, **kwargs})
                return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

            monkeypatch.setattr(kiro_cli.subprocess, "run", _run)
            return calls

        return binary, _install

    def test_the_pinned_binary_is_asked_and_its_answer_parsed(self, pinned):
        from kiro_crew.kiro_cli import installed_kiro_cli_version

        binary, install = pinned
        calls = install("kiro-cli 2.23.0\n")
        assert installed_kiro_cli_version() == (2, 23, 0)
        assert calls[0]["argv"] == [str(binary), "--version"]

    def test_the_output_is_decoded_as_utf8_not_the_platform_locale(self, pinned):
        """A locale decode can mangle the token, and mangled reads as refusing."""
        from kiro_crew.kiro_cli import installed_kiro_cli_version

        _binary, install = pinned
        calls = install("kiro-cli 2.23.0\n")
        installed_kiro_cli_version()
        assert calls[0]["encoding"] == "utf-8"
        assert calls[0]["timeout"] > 0

    def test_a_non_zero_exit_or_garbage_is_unknown(self, pinned):
        from kiro_crew import kiro_cli
        from kiro_crew.kiro_cli import installed_kiro_cli_version

        _binary, install = pinned
        install("kiro-cli 2.23.0\n", returncode=1)
        assert installed_kiro_cli_version() is None
        kiro_cli._version_cache.clear()
        install("no version here\n")
        assert installed_kiro_cli_version() is None

    def test_one_spawn_per_binary_identity(self, pinned):
        """Cached by path and mtime: a swapped binary is asked again."""
        import os

        from kiro_crew.kiro_cli import installed_kiro_cli_version

        binary, install = pinned
        calls = install("kiro-cli 2.23.0\n")
        installed_kiro_cli_version()
        installed_kiro_cli_version()
        assert len(calls) == 1
        stamp = binary.stat().st_mtime_ns + 1_000_000_000
        os.utime(binary, ns=(stamp, stamp))
        installed_kiro_cli_version()
        assert len(calls) == 2

    def test_no_pin_means_no_spawn(self, monkeypatch):
        from kiro_crew import kiro_cli

        monkeypatch.setattr(kiro_cli, "pin_kiro_cli", lambda: (None, True))
        monkeypatch.setattr(kiro_cli, "_version_cache", {})
        monkeypatch.setattr(
            kiro_cli.subprocess, "run", lambda *a, **k: pytest.fail("spawned without a pin")
        )
        assert kiro_cli.installed_kiro_cli_version() is None


#: A ceiling that permits everything, and one that permits nothing. Injected
#: rather than monkeypatched: ``merge_user_permissions`` takes the predicate as an
#: argument precisely so a test can state the ceiling it means in one place.
_UNGOVERNED = lambda ref: True  # noqa: E731
_WITHHOLDS_EVERYTHING = lambda ref: False  # noqa: E731

#: A derived policy to merge into, so every assertion below also shows that the
#: derivation survives whatever the author's block does.
_DERIVED = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}


def _merge(block, ceiling=_UNGOVERNED, derived=None, agent_id="a", allowlist_present=False):
    """A merge with no ``allowedTools`` list by default -- the pure-KAS shape.

    ``allowlist_present`` is the caller's answer to "does this spec carry an
    ``allowedTools`` list", and it decides which channel owns a grant both could
    express (see ``TestTheAllowlistOwnsAGrantItCanExpress``). Most assertions here
    are about a block with no list beside it, so that is the default; the ones that
    are about the interaction say so.
    """
    return merge_user_permissions(
        derived,
        block,
        ceiling_permits=ceiling,
        allowlist_present=allowlist_present,
        agent_id=agent_id,
    )


def _user_rules(policy):
    """The rules a merge added beside :data:`_DERIVED`, which they precede."""
    return policy["rules"][: -len(_DERIVED["rules"])]


class TestThePureKasAgentIsNoLongerAllAsk:
    """The defect this merge exists for.

    A spec that authors ``permissions`` and no ``allowedTools`` has nothing to
    derive from, so the field reached the backend absent -- and absent is not
    neutral: KAS resolves every unmatched request to ``ask``. The author wrote a
    policy and got a prompt for each of the calls it covered.
    """

    def test_an_authored_block_alone_still_produces_a_policy(self):
        block = {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}
        assert _merge(block) == {
            "rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]
        }

    def test_a_block_of_only_denies_travels_with_no_allowlist_at_all(self):
        block = {"rules": [{"capability": "shell", "match": ["rm *"], "effect": "deny"}]}
        assert _merge(block)["rules"] == [
            {"capability": "shell", "match": ["rm *"], "effect": "deny"}
        ]

    def test_no_block_and_no_allowlist_is_still_no_policy(self):
        """Absent says "this spec never described auto-approval", which is true."""
        assert _merge(None) is None

    def test_an_empty_rules_array_adds_nothing(self):
        assert _merge({"rules": []}, derived=_DERIVED) == _DERIVED


class TestRule1TheBlockIsParsedAgainstKasShapeAndRefusedWhole:
    """Reject, do not repair: a half-accepted block is a policy nobody wrote.

    The half that survives a salvage is the half with no ``deny`` in it, and KAS
    is stricter still in the same direction -- a profile policy it cannot parse
    fail-closes its engine to deny-all -- so refusing to forward is also what
    keeps a malformed block from taking the session with it.
    """

    @pytest.mark.parametrize("bad", ["", [], 0, ["rules"], True])
    def test_a_block_that_is_not_an_object_is_refused(self, bad):
        assert _merge(bad, derived=_DERIVED) == _DERIVED

    @pytest.mark.parametrize("bad", [None, {}, "all", [{"capability": "mcp"}, None]])
    def test_rules_must_be_an_array(self, bad):
        assert _merge({"rules": bad}, derived=_DERIVED) == _DERIVED

    @pytest.mark.parametrize(
        "rule",
        [
            "web_fetch",
            ["web_fetch"],
            {"capability": "web_fetch"},
            {"effect": "allow"},
            {"capability": "web_fetch", "effect": "maybe"},
            {"capability": "web_fetch", "effect": "ALLOW"},
            {"capability": "web_fetch", "effect": ["allow"]},
            {"capability": "web_fetch", "effect": {"allow": True}},
            {"capability": "web_fetch", "effect": None},
            {"capability": "typo", "effect": "allow"},
            {"capability": 7, "effect": "allow"},
            {"capability": "web_fetch", "effect": "allow", "scope": "session"},
            {"capability": "web_fetch", "effect": "allow", "match": "srv/*"},
            {"capability": "web_fetch", "effect": "allow", "match": ["", " "]},
            {"capability": "web_fetch", "effect": "allow", "match": [1]},
            {"capability": "web_fetch", "effect": "allow", "exclude": "x"},
        ],
    )
    def test_a_rule_that_does_not_fit_refuses_the_whole_block(self, rule):
        block = {"rules": [rule, {"capability": "web_search", "effect": "deny"}]}
        assert _merge(block, derived=_DERIVED) == _DERIVED

    @pytest.mark.parametrize("effect", [["allow"], {"allow": True}, {"allow"}])
    def test_an_unhashable_effect_refuses_the_block_rather_than_raising(self, effect):
        """The membership test is against a ``frozenset``, so an unhashable value
        raises ``TypeError`` -- and nothing between here and session creation
        catches one, so the whole session would abort on a hand-authoring slip. The
        shape is ordinary: ``match`` and ``exclude`` beside it ARE arrays."""
        block = {"rules": [{"capability": "web_fetch", "effect": effect}]}
        assert _merge(block, derived=_DERIVED) == _DERIVED

    @pytest.mark.parametrize("policies", ["", 0, False, "dev-shell", {"dev-shell": True}])
    def test_a_policies_value_that_is_not_an_array_refuses_the_block(self, policies):
        """``""`` and ``0`` are falsey, so a truth test alone reads a malformed value
        as "no bundles" and forwards the rules beside it."""
        block = {
            "rules": [{"capability": "web_search", "effect": "allow"}],
            "policies": policies,
        }
        assert _merge(block, derived=_DERIVED) == _DERIVED

    def test_an_unknown_key_on_the_block_refuses_it(self):
        block = {"rules": [{"capability": "web_search", "effect": "deny"}], "mode": "allow"}
        assert _merge(block, derived=_DERIVED) == _DERIVED

    def test_an_unknown_key_is_refused_because_it_may_be_the_narrowing_half(self):
        """``exclude`` is the shape of the risk: a key this module cannot read may
        be the part of the rule that makes the grant safe, and relaying the rest
        without it widens what the author wrote."""
        narrowed = {"capability": "web_fetch", "effect": "allow", "exclude": ["*.internal"]}
        assert _user_rules(_merge({"rules": [narrowed]}, derived=_DERIVED)) == [narrowed]
        future = dict(narrowed, excludeHosts=["*.internal"])
        assert _merge({"rules": [future]}, derived=_DERIVED) == _DERIVED

    def test_a_policy_bundle_reference_refuses_the_block(self):
        """``policies`` is well formed, and that is the problem: KAS expands a named
        bundle inline into allow rules held on the backend, so the grant is not in
        this block and there is nothing here to intersect with the ceiling. The
        shipped ``dev-shell`` bundle alone carries shell allows."""
        block = {
            "rules": [{"capability": "web_search", "effect": "allow"}],
            "policies": ["dev-shell"],
        }
        assert _merge(block, derived=_DERIVED) == _DERIVED

    def test_an_empty_policies_array_references_nothing_and_is_not_a_refusal(self):
        block = {"rules": [{"capability": "web_search", "effect": "allow"}], "policies": []}
        assert _user_rules(_merge(block, derived=_DERIVED)) == [
            {"capability": "web_search", "effect": "allow"}
        ]

    @pytest.mark.parametrize(
        "block",
        [
            {"rules": [{"capability": "web_search", "effect": "deny"}] * 201},
            {"rules": [{"capability": "shell", "match": ["x"] * 65, "effect": "deny"}]},
            {"rules": [{"capability": "shell", "exclude": ["x"] * 65, "effect": "deny"}]},
            {"rules": [{"capability": "shell", "match": ["x" * 1025], "effect": "deny"}]},
        ],
    )
    def test_an_oversized_block_is_refused(self, block):
        """KAS's own compiler records that a condition tree past roughly 114 patterns
        overflows cedar-wasm's stack, and ``exclude`` patterns are ANDed onto every
        statement rather than split -- a crash there is a failed session, not a
        refused rule, so the bound belongs on this side."""
        assert _merge(block, derived=_DERIVED) == _DERIVED

    def test_a_block_at_the_bounds_still_travels(self):
        block = {"rules": [{"capability": "shell", "match": ["x"] * 64, "effect": "deny"}] * 200}
        assert len(_user_rules(_merge(block, derived=_DERIVED))) == 200

    def test_the_refusal_is_reported_at_warning_with_its_reason(self, caplog):
        """WARNING, not debug: it is the author's own file being declined, and the
        reason is the only way to find out which rule did it."""
        with caplog.at_level("WARNING", logger="kiro_crew.acp.kas_permissions"):
            _merge({"rules": [{"capability": "web_fetch", "effect": "maybe"}]}, agent_id="kirocrew")
        assert "refusing its whole `permissions` block" in caplog.text
        assert "rule 0" in caplog.text

    def test_every_capability_kas_accepts_is_accepted_here(self):
        """Pinned against KAS's own ``VALID_CAPABILITIES``, so a capability added
        there is a table edit here rather than a silently refused block."""
        for capability in KAS_CAPABILITIES:
            block = {"rules": [{"capability": capability, "effect": "deny"}]}
            assert _user_rules(_merge(block, derived=_DERIVED)) == [
                {"capability": capability, "effect": "deny"}
            ]


class TestRule2NarrowingTravelsUnconditionally:
    """A user who writes ``deny`` is entitled to be obeyed.

    Neither effect can widen anything -- KAS resolves an unmatched request to
    ``ask`` already -- so there is no ceiling question to ask about them.
    """

    @pytest.mark.parametrize("effect", ["deny", "ask"])
    def test_it_travels_even_when_the_ceiling_withholds_everything(self, effect):
        block = {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": effect}]}
        assert _user_rules(_merge(block, _WITHHOLDS_EVERYTHING, derived=_DERIVED)) == [
            {"capability": "mcp", "match": ["srv/*"], "effect": effect}
        ]

    @pytest.mark.parametrize("capability", sorted(WITHHELD_CAPABILITIES) + ["all", "filesystem"])
    def test_it_travels_for_the_families_an_allow_cannot_have(self, capability):
        """Rule 4 refuses the ALLOW, not the capability: a ``deny`` on ``shell`` is
        the author making the backend stricter, which is the safe direction."""
        block = {"rules": [{"capability": capability, "effect": "deny"}]}
        assert _user_rules(_merge(block, derived=_DERIVED)) == [
            {"capability": capability, "effect": "deny"}
        ]

    def test_it_travels_verbatim_with_its_own_match_and_exclude(self):
        rule = {
            "capability": "shell",
            "match": ["git *", "rm *"],
            "exclude": ["git status"],
            "effect": "deny",
        }
        assert _user_rules(_merge({"rules": [rule]}, derived=_DERIVED)) == [rule]

    def test_a_user_deny_overrides_a_derived_allow_for_the_same_capability(self):
        """Position carries no meaning in KAS's evaluator -- rules compile to Cedar
        permit/forbid and the decision is the most restrictive match, deny over ask
        over allow -- so the derived allow staying in the array is not a leak."""
        block = {"rules": [{"capability": "web_fetch", "effect": "deny"}]}
        merged = _merge(block, derived=_DERIVED)
        assert merged["rules"] == [
            {"capability": "web_fetch", "effect": "deny"},
            {"capability": "web_fetch", "effect": "allow"},
        ]

    def test_the_authored_rules_come_first_so_the_array_is_safe_either_way(self):
        """Every derived rule is an ``allow``, so authored-first is also the safe
        order under a first-match-wins evaluator -- one order correct under both
        readings beats a proof that only one reading is live."""
        block = {"rules": [{"capability": "web_search", "effect": "deny"}]}
        assert _merge(block, derived=_DERIVED)["rules"][0] == {
            "capability": "web_search",
            "effect": "deny",
        }


class TestRule3AnAllowTravelsOnlyWhereTheCeilingAlreadyPermitted:
    """Intersected with the ceiling-derived result, never added to it.

    Both halves of the question -- the capability and the resource -- are asked,
    and for ``mcp`` they are one question: the resource IS a server, and the
    ceiling predicate for a server already reads its per-tool rules.
    """

    def test_an_mcp_allow_travels_when_the_ceiling_permits_that_server(self):
        block = {"rules": [{"capability": "mcp", "match": ["srv/one"], "effect": "allow"}]}
        assert _user_rules(_merge(block, derived=_DERIVED)) == [
            {"capability": "mcp", "match": ["srv/one"], "effect": "allow"}
        ]

    def test_the_ceiling_is_asked_about_the_server_the_match_names(self):
        asked: list[str] = []

        def ceiling(ref):
            asked.append(ref)
            return True

        block = {"rules": [{"capability": "mcp", "match": ["a/one", "b/*"], "effect": "allow"}]}
        _merge(block, ceiling)
        assert asked == ["@a", "@b"]

    def test_one_withheld_server_drops_the_whole_rule(self):
        """Not rewritten down to its permitted patterns: a rule narrowed by Crew
        and then relayed is a grant the author did not write."""
        block = {
            "rules": [{"capability": "mcp", "match": ["ok/one", "denied/one"], "effect": "allow"}]
        }
        assert _merge(block, lambda ref: ref != "@denied", derived=_DERIVED) == _DERIVED

    @pytest.mark.parametrize("match", [None, [], ["*"], ["**"], ["*/one"], ["sr?/one"]])
    def test_an_allow_the_ceiling_cannot_be_asked_about_does_not_travel(self, match):
        """No ``match`` and an empty one both mean every resource to KAS, and a glob
        in the server part names servers that do not exist yet. There is no server
        to put to the ceiling, and rule 3 travels only on an affirmative answer."""
        rule = {"capability": "mcp", "effect": "allow"}
        if match is not None:
            rule["match"] = match
        assert _merge({"rules": [rule]}, derived=_DERIVED) == _DERIVED

    @pytest.mark.parametrize("capability,refs", sorted(CEILING_REFS_BY_CAPABILITY.items()))
    def test_a_builtin_capability_is_asked_about_every_tool_it_grants(self, capability, refs):
        asked: list[str] = []
        block = {"rules": [{"capability": capability, "effect": "allow"}]}
        _merge(block, lambda r: asked.append(r) or True)
        assert sorted(asked) == sorted(refs)

    @pytest.mark.parametrize("capability", sorted(CEILING_REFS_BY_CAPABILITY))
    def test_a_builtin_capability_the_ceiling_withholds_does_not_travel(self, capability):
        block = {"rules": [{"capability": capability, "effect": "allow"}]}
        assert _merge(block, _WITHHOLDS_EVERYTHING, derived=_DERIVED) == _DERIVED

    def test_one_withheld_tool_of_a_capability_drops_the_rule(self, monkeypatch):
        """A capability grants every tool mapped to it, so the ceiling has to clear
        every one of them -- an inversion keeping a single ref per capability would
        ask about one and grant the rest."""
        import kiro_crew.acp.kas_permissions as module

        monkeypatch.setitem(module.CEILING_REFS_BY_CAPABILITY, "web_fetch", ("web_fetch", "fetch2"))
        block = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
        assert _merge(block, lambda ref: ref != "fetch2", derived=_DERIVED) == _DERIVED

    @pytest.mark.parametrize("capability", ["power", "context", "diagnostics", "sandbox_network"])
    def test_a_capability_with_no_tool_ref_cannot_be_cleared_so_it_stays_ask(self, capability):
        """The ceiling speaks in refs. A capability with none has no question to
        ask, and silence is the fail-closed direction: no rule means prompt."""
        block = {"rules": [{"capability": capability, "effect": "allow"}]}
        assert _merge(block, derived=_DERIVED) == _DERIVED

    def test_the_ref_table_keeps_every_tool_of_a_many_to_one_capability(self):
        """Two hand-kept tables would answer differently the first time one is
        extended, so this one is derived -- and it holds a TUPLE, because the source
        map is many-to-one and an inversion keeping one ref would drop the others."""
        for tool, capability in CAPABILITY_BY_TOOL.items():
            assert tool in CEILING_REFS_BY_CAPABILITY[capability]
        assert sum(len(v) for v in CEILING_REFS_BY_CAPABILITY.values()) == len(CAPABILITY_BY_TOOL)

    def test_a_withheld_allow_is_reported_so_a_missing_grant_is_explainable(self, caplog):
        block = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_permissions"):
            _merge(block, _WITHHOLDS_EVERYTHING, agent_id="kirocrew")
        assert "withholds auto-approval for web_fetch" in caplog.text


class TestAWithheldAuthoredAllowIsAudited:
    """A withhold is a permission decision, and this is the ordinary case.

    An authored block is the input the merge exists to read, and a governed host
    withholding a server is routine -- so the trail the sibling derived path emits
    deliberately has to cover this path too. Injected rather than imported, for the
    same reason the ceiling predicate is.
    """

    def _events(self, block, ceiling, allowlist_present=False):
        seen: list[tuple[str, str, str]] = []
        merge_user_permissions(
            None,
            block,
            ceiling_permits=ceiling,
            audit_decision=lambda refs, outcome, reason: seen.append((refs, outcome, reason)),
            allowlist_present=allowlist_present,
            agent_id="kirocrew",
        )
        return seen

    def _withholds(self, block, ceiling, allowlist_present=False):
        return [
            (refs, reason)
            for refs, outcome, reason in self._events(block, ceiling, allowlist_present)
            if outcome == "withheld"
        ]

    def test_a_withheld_mcp_server_is_reported_with_its_ref(self):
        block = {"rules": [{"capability": "mcp", "match": ["denied/one"], "effect": "allow"}]}
        assert self._withholds(block, _WITHHOLDS_EVERYTHING) == [("@denied", "governance ceiling")]

    def test_a_withheld_builtin_capability_is_reported_with_its_tool_ref(self):
        block = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
        assert self._withholds(block, _WITHHOLDS_EVERYTHING) == [
            ("web_fetch", "governance ceiling")
        ]

    def test_only_the_servers_the_ceiling_refused_are_named(self):
        block = {
            "rules": [{"capability": "mcp", "match": ["ok/one", "denied/one"], "effect": "allow"}]
        }
        assert self._withholds(block, lambda ref: ref != "@denied") == [
            ("@denied", "governance ceiling")
        ]

    def test_an_unaskable_allow_is_reported_rather_than_dropped_in_silence(self):
        """Two withholds have no ceiling answer to report: an ``mcp`` allow whose
        ``match`` bounds no server, and a capability with no tool ref. Both are
        ordinary authored shapes, so neither may be the one drop with no trail."""
        unbounded = {"rules": [{"capability": "mcp", "effect": "allow"}]}
        assert self._withholds(unbounded, _UNGOVERNED) == [
            ("mcp", "unbounded `match`, ceiling unaskable")
        ]
        no_ref = {"rules": [{"capability": "power", "effect": "allow"}]}
        assert self._withholds(no_ref, _UNGOVERNED) == [("power", "no tool ref, ceiling unaskable")]

    def test_what_travels_is_recorded_too_not_only_what_is_refused(self):
        """A log of refusals alone cannot answer "why was this call not prompted
        for", which is the question the trail exists to answer."""
        block = {
            "rules": [
                {"capability": "mcp", "match": ["ok/one"], "effect": "allow"},
                {"capability": "shell", "match": ["rm *"], "effect": "deny"},
            ]
        }
        assert self._events(block, _UNGOVERNED) == [
            ("mcp:allow, shell:deny", "relayed", "authored `permissions`, ceiling cleared")
        ]

    def test_nothing_is_recorded_when_nothing_survives(self):
        block = {"rules": [{"capability": "shell", "effect": "allow"}]}
        assert [o for _, o, _ in self._events(block, _UNGOVERNED)] == ["withheld"]

    def test_rule_4_is_reported_under_its_own_reason_not_the_ceilings(self):
        """Crew's own standing refusal for those families holds with no ceiling
        installed at all, so it is a different decision from a ceiling withhold and
        the trail has to say which one happened."""
        block = {"rules": [{"capability": "shell", "effect": "allow"}]}
        assert self._withholds(block, _UNGOVERNED) == [
            ("shell", "shell/filesystem family, Crew policy")
        ]

    def test_a_refused_block_is_reported_because_it_withholds_every_grant_in_it(self):
        block = {"rules": [{"capability": "web_fetch", "effect": "maybe"}]}
        events = self._withholds(block, _UNGOVERNED)
        assert len(events) == 1
        refs, reason = events[0]
        assert refs == "the authored `permissions` block"
        assert reason.startswith("refused: ")

    def test_a_grant_the_allowlist_could_express_is_reported_as_withheld(self):
        block = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
        assert self._withholds(block, _UNGOVERNED, allowlist_present=True) == [
            ("web_fetch", "derivable from `allowedTools`")
        ]

    def test_the_callback_is_optional_so_a_translation_only_caller_still_works(self):
        block = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
        assert merge_user_permissions(None, block, ceiling_permits=_WITHHOLDS_EVERYTHING) is None


class TestTheAllowlistOwnsAGrantItCanExpress:
    """A block must not add a grant the ``allowedTools`` list does not carry.

    Crew's own seeder writes a derived block into the specs it manages and then
    PRESERVES it (``agent._seed_kas_permissions``), so a block on disk can lag the
    list it came from. Content cannot tell that block apart from an authored one --
    which is why the answer is the CHANNEL: where a list exists it is the governed
    input, re-derived on every projection, and a rule the derivation could have
    emitted itself does not travel from the block. Where no list exists there is no
    derivation to be authoritative, and the rule is the author's own.
    """

    def test_a_derivable_allow_does_not_travel_beside_an_allowlist(self):
        """The revoked-grant case: the list drops ``web_fetch``, the block still
        names it, and a fresh derivation is what decides."""
        block = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
        assert _merge(block, derived=_DERIVED, allowlist_present=True) == _DERIVED

    def test_a_derivable_mcp_allow_does_not_travel_beside_an_allowlist(self):
        block = {"rules": [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}]}
        assert _merge(block, derived=_DERIVED, allowlist_present=True) == _DERIVED

    def test_an_empty_allowlist_still_counts_as_present(self):
        """An empty list is the operator saying "auto-approve nothing", and a block
        must not answer that for them."""
        block = {"rules": [{"capability": "web_search", "effect": "allow"}]}
        assert _merge(block, allowlist_present=True) is None

    @pytest.mark.parametrize(
        "rule",
        [
            {"capability": "web_search", "effect": "deny"},
            {"capability": "web_search", "effect": "ask"},
            {"capability": "web_search", "match": ["example.com/*"], "effect": "allow"},
            {"capability": "web_search", "exclude": ["*.internal"], "effect": "allow"},
            {"capability": "mcp", "match": ["srv/*"], "exclude": ["srv/drop"], "effect": "allow"},
        ],
    )
    def test_a_rule_the_allowlist_cannot_express_travels_either_way(self, rule):
        """A tool-name allowlist carries no effect but ``allow`` and no resource
        pattern at all, so none of these can have come from one."""
        assert _user_rules(_merge({"rules": [rule]}, derived=_DERIVED, allowlist_present=True)) == [
            rule
        ]

    @pytest.mark.parametrize(
        "rule",
        [
            {"capability": "web_search", "effect": "allow", "exclude": []},
            {"capability": "web_search", "effect": "allow", "match": []},
            {"capability": "web_search", "effect": "allow", "match": [], "exclude": []},
        ],
    )
    def test_an_empty_match_or_exclude_does_not_buy_a_rule_past_the_list(self, rule):
        """``exclude: []`` excludes nothing and ``match: []`` matches every resource,
        so both mean what the bare form means -- a key-presence test would read them
        as narrowed and hand the grant back to the block."""
        assert _merge({"rules": [rule]}, derived=_DERIVED, allowlist_present=True) == _DERIVED

    def test_with_no_allowlist_a_derivable_allow_is_the_authors_own(self):
        """The pure-KAS agent: no list, so no derivation to be authoritative."""
        block = {"rules": [{"capability": "web_fetch", "effect": "allow"}]}
        assert _merge(block)["rules"] == [{"capability": "web_fetch", "effect": "allow"}]


class TestRule4TheShellAndFilesystemFamiliesNeverTravelAsAnAllow:
    """The one place the merge is deliberately not symmetric with kiro-cli.

    Auto-approval is the absence of a permission request, and Crew's deny floor
    and sensitive-path check run ON that request. A dropped rule leaves the
    capability unmatched, and unmatched is ``ask`` -- so the gate runs.
    """

    @pytest.mark.parametrize("capability", sorted(WITHHELD_CAPABILITIES))
    def test_a_named_family_allow_is_dropped_on_an_ungoverned_host(self, capability):
        block = {"rules": [{"capability": capability, "effect": "allow"}]}
        assert _merge(block, derived=_DERIVED) == _DERIVED

    @pytest.mark.parametrize("capability", ["all", "builtin", "filesystem"])
    def test_a_meta_capability_carrying_the_family_is_dropped_too(self, capability):
        """The sharp case. ``{"capability": "all"}`` is four words that carry
        ``shell`` and both filesystem capabilities inside them, so a rule read at
        face value hands over exactly what the named form is refused."""
        block = {"rules": [{"capability": capability, "effect": "allow"}]}
        assert _merge(block, derived=_DERIVED) == _DERIVED

    def test_a_scoped_family_allow_is_dropped_as_well(self):
        """A ``match`` does not buy it through: the refusal is about the missing
        permission request, which a narrower pattern does not restore."""
        block = {"rules": [{"capability": "shell", "match": ["git status"], "effect": "allow"}]}
        assert _merge(block, derived=_DERIVED) == _DERIVED

    def test_it_does_not_suppress_the_rules_beside_it(self):
        block = {
            "rules": [
                {"capability": "shell", "effect": "allow"},
                {"capability": "web_search", "effect": "allow"},
            ]
        }
        assert _user_rules(_merge(block, derived=_DERIVED)) == [
            {"capability": "web_search", "effect": "allow"}
        ]

    def test_the_refusal_is_reported_because_it_reverses_the_spec(self, caplog):
        block = {"rules": [{"capability": "all", "effect": "allow"}]}
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_permissions"):
            _merge(block, agent_id="kirocrew")
        assert "fs_read, fs_write, shell" in caplog.text

    def test_the_capability_form_covers_every_withheld_tool(self):
        """The two tables are the same refusal in two currencies, so a tool added
        to one must have its capability in the other."""
        assert WITHHELD_CAPABILITIES == {"shell", "fs_read", "fs_write"}
        assert not WITHHELD_CAPABILITIES & set(CEILING_REFS_BY_CAPABILITY)
        for tool in WITHHELD_FROM_AUTO_APPROVE:
            assert tool not in CAPABILITY_BY_TOOL

    def test_the_expansion_table_matches_kas(self):
        """Read off ``policy/capabilities.ts``: ``all`` is every builtin plus
        ``mcp``, ``builtin`` excludes ``mcp``, ``filesystem`` is the two concrete
        filesystem capabilities. Each expansion must stay inside KAS's vocabulary."""
        assert META_CAPABILITY_EXPANSION["filesystem"] == ("fs_read", "fs_write")
        assert set(META_CAPABILITY_EXPANSION["all"]) == set(
            META_CAPABILITY_EXPANSION["builtin"]
        ) | {"mcp"}
        assert "mcp" not in META_CAPABILITY_EXPANSION["builtin"]
        for expansion in META_CAPABILITY_EXPANSION.values():
            assert set(expansion) <= KAS_CAPABILITIES
            assert not set(expansion) & set(META_CAPABILITY_EXPANSION)
