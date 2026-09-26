"""``platform.tool_names``: the two engines' built-in vocabularies, both directions."""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.platform.tool_names import (
    KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME,
    KAS_TOOL_IDS_MEASURED_ON_KIRO_CLI,
    KIRO_CLI_NAME_BY_KAS_TOOL,
    POLICY_ALIAS_DENY_ONLY_IDS,
    expand_kiro_cli_tool_exclusions,
    expand_kiro_cli_tool_names,
    policy_alias_split,
    policy_aliases,
)


class TestTheTwoTablesAgree:
    def test_every_mounted_kas_id_reads_back_to_the_name_that_mounted_it(self):
        for kiro_cli_name, family in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.items():
            for kas_id in family:
                assert KIRO_CLI_NAME_BY_KAS_TOOL[kas_id] == kiro_cli_name

    def test_delete_file_is_governed_as_a_write_but_mounted_by_no_family(self):
        """The one asymmetry, and it points the safe way: a deny written as
        ``fs_write`` covers a delete, while mounting ``fs_write`` grants none."""
        assert KIRO_CLI_NAME_BY_KAS_TOOL["delete_file"] == "fs_write"
        assert "delete_file" not in {
            t for fam in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.values() for t in fam
        }

    def test_no_shared_name_is_a_key_of_the_policy_table(self):
        """A name both engines use already IS the policy spelling."""
        for shared in ("fs_write", "execute_bash", "web_fetch", "code", "tool_search"):
            assert shared not in KIRO_CLI_NAME_BY_KAS_TOOL

    def test_the_shell_s_permission_id_is_governed_as_execute_bash(self):
        """KAS registers its shell as ``execute_bash`` but stamps ``run_command``
        on the permission request, so an ``execute_bash`` rule must bind to the
        id the gate actually receives."""
        assert KIRO_CLI_NAME_BY_KAS_TOOL["run_command"] == "execute_bash"
        assert policy_aliases("run_command") == ("execute_bash",)

    def test_no_kiro_cli_key_appears_in_its_own_family(self):
        for kiro_cli_name, family in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.items():
            assert kiro_cli_name not in family


class TestTheTablesMatchTheRecordedEngine:
    """Both tables are pinned to ``test/fixtures/kas_builtin_tool_ids.json``, a
    recording of the engine's built-in ids (source at a named commit plus the
    live captures). Mount-direction drift fails loud on its own (reads vanish);
    POLICY-direction drift fails permissive -- a renamed write id would sail
    past a kiro-cli-spelled deny -- so the pin lives here, where a rename fails
    a test before it fails at the gate. Re-measure per engine bump."""

    @staticmethod
    def _registry() -> dict:
        path = Path(__file__).resolve().parent / "fixtures" / "kas_builtin_tool_ids.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_mounted_id_is_a_registered_engine_tool(self):
        registered = set(self._registry()["registered"])
        for family in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.values():
            assert set(family) <= registered, sorted(set(family) - registered)

    def test_the_runtime_s_measured_version_is_the_fixture_s(self):
        """The doctor compares the installed kiro-cli against this constant; the
        fixture is what was actually measured. One number, two homes."""
        recorded = tuple(int(p) for p in self._registry()["_meta"]["kiro_cli_version"].split("."))
        assert KAS_TOOL_IDS_MEASURED_ON_KIRO_CLI == recorded

    def test_every_policy_key_is_an_id_the_gate_can_receive(self):
        """A policy key is either a registered id or the permission-frame id the
        engine reports instead of one; anything else names nothing."""
        reg = self._registry()
        receivable = set(reg["registered"]) | set(reg["permission_tool_id"].values())
        assert set(KIRO_CLI_NAME_BY_KAS_TOOL) <= receivable, sorted(
            set(KIRO_CLI_NAME_BY_KAS_TOOL) - receivable
        )

    def test_every_engine_write_or_read_or_shell_id_has_a_policy_spelling(self):
        """The permissive direction, closed: each registered filesystem/shell id
        that is NOT itself a kiro-cli name has a policy entry, and so does each
        permission-frame id. A new engine id in one of those families lands here
        first and must be classified or explicitly listed as shared."""
        reg = self._registry()
        shared = {"fs_write", "execute_bash", "execute_pwsh", "code", "web_fetch", "tool_search"}
        governed = set(KIRO_CLI_NAME_BY_KAS_TOOL)
        for kas_id in reg["registered"]:
            assert kas_id in shared or kas_id in governed, kas_id
        for frame_id in reg["permission_tool_id"].values():
            assert frame_id in governed, frame_id


class TestPolicyAliases:
    def test_a_kas_id_yields_its_kiro_cli_name(self):
        assert policy_aliases("str_replace") == ("fs_write",)
        assert policy_aliases("read_file") == ("fs_read",)

    def test_several_identities_dedupe_and_keep_order(self):
        assert policy_aliases("str_replace", "fs_append", "read_file") == ("fs_write", "fs_read")

    def test_an_input_that_already_is_the_policy_name_is_not_echoed(self):
        """The caller evaluates its inputs already; an alias equal to one of
        them would be the same question asked twice."""
        assert policy_aliases("str_replace", "fs_write") == ()

    def test_unknown_empty_and_shared_names_yield_nothing(self):
        assert policy_aliases("", "execute_bash", "Update the changelog", "@srv/tool") == ()


class TestPolicyAliasSplit:
    """An alias that names the SAME capability may admit; one whose id can do
    more than the kiro-cli tool it reads under may only deny."""

    def test_a_same_capability_alias_may_admit(self):
        assert policy_alias_split("str_replace") == (("fs_write",), ())
        assert policy_alias_split("read_file") == (("fs_read",), ())
        assert policy_alias_split("run_command") == (("execute_bash",), ())

    def test_the_delete_alias_may_only_deny(self):
        assert policy_alias_split("delete_file") == ((), ("fs_write",))

    def test_unknown_and_shared_names_split_to_nothing(self):
        assert policy_alias_split("fs_write") == ((), ())
        assert policy_alias_split("@srv/tool") == ((), ())

    def test_deny_only_is_exactly_the_policy_keys_outside_every_mount_family(self):
        """The mount table bounds a kiro-cli name by what that tool CAN DO, so a
        policy key the mount table files under no family is one whose capability
        the kiro-cli name does not carry -- and that is the id whose alias must
        not admit. Permission-frame-only ids (``run_command``) are the shell
        ``execute_bash`` registers as, the same capability, so they are excluded
        from the comparison rather than counted as unmounted."""
        mounted = {kas_id for fam in KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.values() for kas_id in fam}
        unmounted_policy_keys = {
            kas_id
            for kas_id in KIRO_CLI_NAME_BY_KAS_TOOL
            if kas_id not in mounted and kas_id != "run_command"
        }
        assert POLICY_ALIAS_DENY_ONLY_IDS == unmounted_policy_keys == {"delete_file"}
        assert POLICY_ALIAS_DENY_ONLY_IDS <= set(KIRO_CLI_NAME_BY_KAS_TOOL)


class TestExpand:
    def test_is_pure_and_order_preserving(self):
        entries = ["@a", "fs_read", "@b", "fs_read"]
        assert expand_kiro_cli_tool_names(entries) == [
            "@a",
            "fs_read",
            "read_file",
            "list_directory",
            "@b",
        ]
        assert entries == ["@a", "fs_read", "@b", "fs_read"], "input untouched"
        assert expand_kiro_cli_tool_names([]) == []


class TestTheTwoReadingsHaveOppositePolarity:
    """Mounting grants, so it is bounded by the kiro-cli tool's own verbs.
    Exclusion withholds, so it reaches everything governed under the name.
    ``delete_file`` is the one id where the two differ."""

    def test_mount_reading_never_grants_delete_file(self):
        assert expand_kiro_cli_tool_names(["fs_write"]) == ["fs_write", "str_replace", "fs_append"]

    def test_exclusion_reading_withholds_delete_file_under_fs_write(self):
        assert expand_kiro_cli_tool_exclusions(["fs_write"]) == [
            "fs_write",
            "str_replace",
            "fs_append",
            "delete_file",
        ]

    def test_exclusion_reading_equals_the_gate_s_policy_table_per_name(self):
        """One table, two readers: what the gate folds onto a kiro-cli name is
        exactly what excluding that name withholds, for EVERY policy owner
        (``execute_bash`` included), less the ids the engine's exclusion matcher
        cannot see -- the permission-frame-only ids the fixture records."""
        reg = TestTheTablesMatchTheRecordedEngine._registry()
        registered = set(reg["registered"])
        frame_only = set(reg["permission_tool_id"].values()) - registered
        assert frame_only == {"run_command"}
        owners = set(KIRO_CLI_NAME_BY_KAS_TOOL.values())
        assert "execute_bash" in owners
        for kiro_cli_name in owners:
            excluded = set(expand_kiro_cli_tool_exclusions([kiro_cli_name])) - {kiro_cli_name}
            governed = {k for k, v in KIRO_CLI_NAME_BY_KAS_TOOL.items() if v == kiro_cli_name}
            assert excluded == governed - frame_only, kiro_cli_name
            assert excluded <= registered, kiro_cli_name

    def test_excluding_the_shell_emits_only_its_registered_id(self):
        """``run_command`` is reported, not registered; emitting it would exclude
        nothing, and ``execute_bash`` already withholds the shell."""
        assert expand_kiro_cli_tool_exclusions(["execute_bash"]) == ["execute_bash"]

    def test_exclusion_reading_passes_unknown_names_through(self):
        assert expand_kiro_cli_tool_exclusions(["@srv", "tool_search", ""]) == [
            "@srv",
            "tool_search",
            "",
        ]
