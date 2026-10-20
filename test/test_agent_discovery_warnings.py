"""Tests for systematic-scan-failure warnings, fork lineage enrichment, and
``spec_by_declared_name``.

Split out of ``test_agent_discovery.py`` (file-line-cap gate); see that module's
docstring for the full sibling-file map. Shares fixtures/doubles from
``agent_discovery_test_helpers``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from agent_discovery_test_helpers import (
    _DISCOVERY_LOGGER,
    _agents_dir,
    _discovery_warnings,
    _project_agents_dir,
    fake_home,
)

from kiro_crew import agent_state
from kiro_crew.agent_discovery import (
    AmbiguousAgentSpecError,
    clear_list_agents_cache,
    clear_project_agent_cache,
    list_agents,
    project_agent_names,
    spec_by_declared_name,
)

__all__ = ["fake_home"]


class TestSystematicScanFailureWarning:
    """A scan that rejects EVERY candidate spec warns once; anything less stays quiet.

    Regression: `_read_agent_spec` degrades per file to ``None`` at debug level, so
    a systematic refusal (e.g. the trusted-root gate rejecting an entire home
    layout) is indistinguishable at default log levels from an empty
    agents directory — discovery lists nothing and nothing says why.
    """

    def test_all_unreadable_user_specs_emit_one_warning(self, fake_home, caplog):
        d = _agents_dir(fake_home)
        for i in range(3):
            (d / f"broken-{i}.json").write_bytes(b"\xff\xfe\x00\x01\xa3")
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=d) == []
        warnings = _discovery_warnings(caplog)
        assert len(warnings) == 1, "exactly one warning per scan, not per file"
        # Count asserted via the record's args: a digit-in-string match is
        # satisfiable by digits in the pytest tmp path.
        assert warnings[0].args[0] == 3
        assert str(d) in warnings[0].getMessage()

    def test_mixed_directory_emits_no_warning(self, fake_home, caplog):
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        (d / "broken.json").write_bytes(b"\xff\xfe\x00\x01\xa3")
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]
        assert _discovery_warnings(caplog) == []

    def test_empty_and_absent_directories_emit_no_warning(self, fake_home, caplog):
        empty = _agents_dir(fake_home)
        absent = fake_home / "nowhere" / "agents"
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=empty) == []
            clear_list_agents_cache()
            assert list_agents(agents_dir=absent) == []
        assert _discovery_warnings(caplog) == [], "N=0 is not systematic failure"

    def test_all_unreadable_project_specs_emit_one_warning(self, fake_home, tmp_path, caplog):
        d = _agents_dir(fake_home)
        proj = tmp_path / "repo"
        pd = _project_agents_dir(proj)
        (pd / "bad-a.json").write_text("not json at all")
        (pd / "bad-b.json").write_text(json.dumps(["top-level", "array"]))
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=d, project_dir=str(proj)) == []
        warnings = _discovery_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].args[0] == 2
        assert str(pd) in warnings[0].getMessage()

    def test_project_agent_names_warns_on_systematic_failure(self, tmp_path, caplog):
        """The per-turn resolver's scan warns too — this is the exact path whose
        silence lets model resolution fall back to auto."""
        proj = tmp_path / "repo"
        pd = _project_agents_dir(proj)
        (pd / "bad.json").write_text("{broken")
        clear_project_agent_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert project_agent_names(str(proj)) == frozenset()
        warnings = _discovery_warnings(caplog)
        assert len(warnings) == 1
        assert str(pd) in warnings[0].getMessage()

    def test_project_agent_names_mixed_directory_emits_no_warning(self, tmp_path, caplog):
        proj = tmp_path / "repo"
        pd = _project_agents_dir(proj)
        (pd / "good.json").write_text(json.dumps({"name": "good"}))
        (pd / "bad.json").write_text("{broken")
        clear_project_agent_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert project_agent_names(str(proj)) == frozenset({"good"})
        assert _discovery_warnings(caplog) == []

    def test_sidecar_only_directory_emits_no_warning(self, fake_home, caplog):
        """AppleDouble sidecars are rejected by design, not by failure — a
        directory holding only ``._*.json`` is empty of specs, not broken."""
        d = _agents_dir(fake_home)
        (d / "._ghost.json").write_bytes(b"\x02\x00\x00\x00\xa3\x80\x81 not utf-8")
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=d) == []
        assert _discovery_warnings(caplog) == []

    def test_row_construction_failure_counts_as_unparsed(self, fake_home, caplog, monkeypatch):
        """A spec that parses but whose row construction raises still ends in
        "discovery listed nothing" — the warning must cover that path too, so
        the parsed counter means "produced a row", not "JSON loaded"."""
        import kiro_crew.agent_discovery as mod

        d = _agents_dir(fake_home)
        (d / "parses.json").write_text(json.dumps({"name": "parses"}))

        def _boom(f, data):
            raise RuntimeError("row construction failed")

        monkeypatch.setattr(mod, "_global_agent_info", _boom)
        clear_list_agents_cache()
        with caplog.at_level(logging.WARNING, logger=_DISCOVERY_LOGGER):
            assert list_agents(agents_dir=d) == []
        warnings = _discovery_warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].args[0] == 1


class TestForkLineageEnrichment:
    """list_agents stamps forked_from/private_to onto global-scope rows from the
    agent_state sidecar (global scope only — forks are recorded against
    user-level templates). The sidecar lives under the isolated KIROCREW_HOME."""

    def test_forked_row_is_enriched(self, tmp_path):
        d = tmp_path / "agents"
        d.mkdir()
        (d / "design-crew.json").write_text(json.dumps({"name": "design-crew"}))
        (d / "plain.json").write_text(json.dumps({"name": "plain"}))
        agent_state.set_fork_info("design-crew", forked_from="kirocrew", private_to="design-crew")

        clear_list_agents_cache()
        by_name = {a.name: a for a in list_agents(agents_dir=d)}

        assert by_name["design-crew"].forked_from == "kirocrew"
        assert by_name["design-crew"].private_to == "design-crew"
        # An un-forked sibling keeps the empty defaults.
        assert by_name["plain"].forked_from == ""
        assert by_name["plain"].private_to == ""

    def test_unforked_rows_have_empty_lineage_when_no_sidecar(self, tmp_path):
        d = tmp_path / "agents"
        d.mkdir()
        (d / "solo.json").write_text(json.dumps({"name": "solo"}))

        clear_list_agents_cache()
        (agent,) = list_agents(agents_dir=d)
        assert agent.forked_from == ""
        assert agent.private_to == ""


class TestSpecByDeclaredName:
    """The shared declared-name scan two session-start surfaces resolve through."""

    @staticmethod
    def _write(agents_dir: Path, filename: str, **fields: object) -> Path:
        path = agents_dir / filename
        path.write_text(json.dumps({"name": "kirocrew", **fields}), encoding="utf-8")
        return path

    def test_a_namespaced_spec_resolves_by_its_declared_name(self, tmp_path: Path) -> None:
        self._write(tmp_path, "SomePackage-kirocrew.json", description="namespaced")

        spec = spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert spec is not None and spec["description"] == "namespaced"

    def test_no_declared_match_is_none(self, tmp_path: Path) -> None:
        self._write(tmp_path, "SomePackage-other.json", name="other")
        (tmp_path / "other.json").write_text(json.dumps({"name": "other"}), encoding="utf-8")

        assert spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test") is None

    def test_two_differing_specs_declaring_one_name_are_refused_naming_both(
        self, tmp_path: Path
    ) -> None:
        self._write(tmp_path, "Alpha-kirocrew.json", description="alpha")
        self._write(tmp_path, "Beta-kirocrew.json", description="beta")

        with pytest.raises(AmbiguousAgentSpecError) as exc:
            spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert "Alpha-kirocrew.json" in str(exc.value)
        assert "Beta-kirocrew.json" in str(exc.value)
        assert "differing content" in str(exc.value)
        assert [p.name for p in exc.value.paths] == ["Alpha-kirocrew.json", "Beta-kirocrew.json"]

    def test_identical_specs_declaring_one_name_resolve_to_that_one_spec(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A package installer that flattens dependencies writes the same spec
        twice under two package prefixes. Both parse to one dict, so whichever
        the backend runs, the policy and the projection read from it are the
        same; the scan answers with that dict and warns naming both files."""
        self._write(
            tmp_path,
            "Alpha-kirocrew.json",
            description="shared",
            managedToolPolicy={"exclude": ["shell"]},
        )
        self._write(
            tmp_path,
            "Beta-kirocrew.json",
            description="shared",
            managedToolPolicy={"exclude": ["shell"]},
        )

        with caplog.at_level("WARNING", logger="kiro_crew.agent_discovery"):
            spec = spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert spec == {
            "name": "kirocrew",
            "description": "shared",
            "managedToolPolicy": {"exclude": ["shell"]},
        }
        (warning,) = [r for r in caplog.records if "identical specs" in r.getMessage()]
        assert "Alpha-kirocrew.json" in warning.getMessage()
        assert "Beta-kirocrew.json" in warning.getMessage()

    def test_the_twin_warning_redacts_a_credential_shaped_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning names the files and the declared name, both untrusted
        input from a user-writable directory, and it persists in gateway.log;
        it goes through the same log-egress redaction as every other line
        carrying text this process did not author."""
        token = "ghp_" + "C" * 36
        for stem in ("Alpha", "Beta"):
            (tmp_path / f"{stem}-{token}.json").write_text(
                json.dumps({"name": token, "description": "shared"}), encoding="utf-8"
            )

        with caplog.at_level("WARNING", logger="kiro_crew.agent_discovery"):
            spec = spec_by_declared_name(tmp_path, token, operation="t", source="test")

        assert spec is not None and spec["description"] == "shared"
        (warning,) = [r for r in caplog.records if "identical specs" in r.getMessage()]
        assert token not in warning.getMessage(), "the token reached the gateway log"
        assert "REDACTED" in warning.getMessage()

    def test_a_pair_equal_on_one_field_but_not_all_is_still_refused(self, tmp_path: Path) -> None:
        """Equality is of the whole parse. Two specs that agree on the policy a
        caller reads but differ in their tools are two agents, and which one
        is live still decides what the session can do."""
        self._write(
            tmp_path, "Alpha-kirocrew.json", tools=["read"], managedToolPolicy={"exclude": []}
        )
        self._write(
            tmp_path, "Beta-kirocrew.json", tools=["shell"], managedToolPolicy={"exclude": []}
        )

        with pytest.raises(AmbiguousAgentSpecError):
            spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

    def test_a_pair_differing_only_in_a_scalar_type_is_refused(self, tmp_path: Path) -> None:
        """``True == 1`` in Python, but ``true`` and ``1`` are two documents to
        a backend that parses the file itself, so they are two specs here too."""
        self._write(tmp_path, "Alpha-kirocrew.json", includeMcpJson=True)
        self._write(tmp_path, "Beta-kirocrew.json", includeMcpJson=1)

        with pytest.raises(AmbiguousAgentSpecError):
            spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        self._write(tmp_path, "Beta-kirocrew.json", includeMcpJson=True)
        spec = spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")
        assert spec is not None and spec["includeMcpJson"] is True

    def test_a_deeply_nested_identical_pair_resolves_without_a_recursion_error(
        self, tmp_path: Path
    ) -> None:
        """The reader accepts a spec nested hundreds of levels deep, and the
        comparison must answer for it rather than raise ``RecursionError`` out
        of the scan: that exception is neither a resolution nor the typed
        refusal, so a caller would fail its request instead of answering."""
        deep: object = "leaf"
        for _ in range(600):
            deep = {"m": [deep]}
        for stem in ("Alpha", "Beta"):
            self._write(tmp_path, f"{stem}-kirocrew.json", metadata=deep)

        spec = spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert spec is not None and "metadata" in spec

    def test_an_identical_pair_beside_a_third_differing_claimant_is_refused(
        self, tmp_path: Path
    ) -> None:
        """Three claimants, two of them twins: the odd one makes the set
        divergent, so the refusal names all three."""
        self._write(tmp_path, "Alpha-kirocrew.json", description="shared")
        self._write(tmp_path, "Beta-kirocrew.json", description="shared")
        self._write(tmp_path, "Gamma-kirocrew.json", description="odd")

        with pytest.raises(AmbiguousAgentSpecError) as exc:
            spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert [p.name for p in exc.value.paths] == [
            "Alpha-kirocrew.json",
            "Beta-kirocrew.json",
            "Gamma-kirocrew.json",
        ]

    def test_only_the_first_matching_parse_is_held(self, tmp_path: Path, monkeypatch) -> None:
        """The refusal needs the duplicates' PATHS, not their parses.

        Each file is capped by the reader, so the parsed-spec memory the scan
        holds at its peak is set by how many parses it keeps at once. Holding
        one per match makes that the number of same-name files in a
        user-writable directory times the cap; holding one total makes it the
        cap. (Paths are kept one per candidate either way; they are small and
        the refusal message needs them.) The bound is a property of
        the scan WHILE it runs -- once it raises, any list it held dies with its
        frame either way -- so the probe sits inside the reader: on every read,
        each earlier parse except the first and the one the loop body last
        assigned must already be unreachable.
        """
        import gc
        import weakref

        from kiro_crew import agent_discovery

        class _Spec(dict):
            """A dict that can be weakly referenced."""

        for stem in ("Alpha", "Beta", "Gamma", "Delta"):
            self._write(tmp_path, f"{stem}-kirocrew.json")

        handed_out: list[weakref.ref] = []
        retained_mid_scan: list[str] = []

        def _reader(path: Path, *, operation: str, source: str) -> dict:
            # Reads 0..n-2 are done; read n-2's parse is still the loop's own
            # ``spec`` local until this call returns, so it is exempt. Read 0 is
            # the match the scan may return, so it is exempt. Everything else
            # must be gone.
            gc.collect()
            for ref in handed_out[1:-1]:
                spec = ref()
                if spec is not None:
                    retained_mid_scan.append(spec["origin"])
            spec = _Spec(name="kirocrew", origin=path.name)
            handed_out.append(weakref.ref(spec))
            return spec

        monkeypatch.setattr(agent_discovery, "_read_agent_spec", _reader)

        with pytest.raises(AmbiguousAgentSpecError) as exc:
            spec_by_declared_name(tmp_path, "kirocrew", operation="t", source="test")

        assert len(handed_out) == 4, "the scan must have read every candidate"
        assert retained_mid_scan == [], f"parses held past their read: {retained_mid_scan}"
        # The refusal still names every duplicate: paths are kept, parses are not.
        for stem in ("Alpha", "Beta", "Gamma", "Delta"):
            assert f"{stem}-kirocrew.json" in str(exc.value)
