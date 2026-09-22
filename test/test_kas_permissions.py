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
    WITHHELD_FROM_AUTO_APPROVE,
    allowed_tools_to_permissions,
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

    def test_a_stale_block_is_the_accepted_cost_and_the_wire_covers_it(self):
        """Seeding-not-refreshing means the file can lag ``allowedTools``.

        Bounded on purpose: the wire projection derives afresh every session and
        outranks the file, so the block on disk is what applies when Crew is not
        injecting an agent at all.
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
