"""Bounds on what the plugin converter reads and retains.

Everything the converter keeps from a manifest lands in the emitted ``app.json``, so
this file pins a bound on each retained foreign field -- the MCP servers and the
other manifest fields, the name (refused rather than shortened) and the provenance
block with its final warnings. It also pins the bounds on the server count, one
skill tree and the walk that discovers skills, the manifest and the schema probe
before either is read, a directory listing at its iterator, a container by what it
inspects, and hook documents by what is read. The converter's mapping and output
are pinned in
``test_plugin_import.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from plugin_import_helpers import (
    _package,
    _skill,
    _write_json,
)

from kiro_crew.apps import plugin_import
from kiro_crew.apps.plugin_import import PluginImportError, convert_plugin_package


class TestForeignManifestRetentionIsBounded:
    """Everything this converter retains from a manifest is written into the emitted
    app.json, so a bound on the input is not enough -- the bound has to constrain
    what gets written."""

    def test_the_server_count_is_bounded(self, tmp_path):
        servers = {f"s{i}": {"command": "x"} for i in range(plugin_import.MAX_MCP_SERVERS + 20)}
        root = _package(tmp_path, mcpServers=servers)
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert len(emitted.get("mcpServers", {})) == plugin_import.MAX_MCP_SERVERS
        assert any("mcpServers declared" in w for w in report.warnings)

    def test_a_retained_string_is_truncated(self, tmp_path):
        long = "a" * (plugin_import.MAX_MANIFEST_STRING_CHARS + 500)
        root = _package(tmp_path, mcpServers={"s": {"command": "x", "note": long}})
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        kept = emitted["mcpServers"]["s"]["note"]
        assert len(kept) == plugin_import.MAX_MANIFEST_STRING_CHARS
        assert any("characters; truncated" in w for w in report.warnings)

    def test_a_deeply_nested_value_is_dropped(self, tmp_path):
        deep: object = "leaf"
        for _ in range(plugin_import.MAX_MANIFEST_DEPTH + 4):
            deep = {"n": deep}
        root = _package(tmp_path, mcpServers={"s": {"command": "x", "deep": deep}})
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        # The server survives; only the over-deep branch is dropped, so one
        # pathological field does not cost the whole entry.
        assert emitted["mcpServers"]["s"]["command"] == "x"
        assert any("levels; dropped" in w for w in report.warnings)

    def test_a_non_finite_number_is_dropped_so_the_emitted_json_stays_strict(self, tmp_path):
        """``json.loads`` accepts the bare words NaN/Infinity and ``json.dumps``
        re-emits them, but RFC 8259 defines neither -- so retaining one costs the
        whole emitted app.json to any strict reader, not just the one field.

        The discriminating assertion is a STRICT parse, not the field's absence: a
        reader that refuses the undefined constants is exactly the consumer the
        corruption breaks.
        """
        manifest = {
            "name": "demo-plugin",
            "mcpServers": {"s": {"command": "x", "timeout": float("nan")}},
        }
        root = tmp_path / "src" / "demo-plugin"
        (root / ".codex-plugin").mkdir(parents=True)
        # json.dumps writes the bare word NaN, which is what a malformed manifest
        # in the wild carries; round-tripping through the fixture helper would not
        # produce it any other way.
        (root / ".codex-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        raw = (out / "app.json").read_text(encoding="utf-8")

        def _reject(_constant: str) -> object:
            raise ValueError("the emitted app.json carries a JSON constant RFC 8259 omits")

        emitted = json.loads(raw, parse_constant=_reject)
        # The server survives; only the unrepresentable field is dropped, matching
        # how an over-deep branch costs the branch and not the entry.
        assert emitted["mcpServers"]["s"]["command"] == "x"
        assert "timeout" not in emitted["mcpServers"]["s"]
        assert any("not a finite number" in w for w in report.warnings)

    def test_a_long_container_is_bounded(self, tmp_path):
        args = [str(i) for i in range(plugin_import.MAX_MANIFEST_CONTAINER_ITEMS + 50)]
        root = _package(tmp_path, mcpServers={"s": {"command": "x", "flags": args}})
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert (
            len(emitted["mcpServers"]["s"]["flags"]) == plugin_import.MAX_MANIFEST_CONTAINER_ITEMS
        )
        assert any("items; the rest are dropped" in w for w in report.warnings)


class TestNonMcpManifestFieldsAreBounded:
    """The mcpServers path is bounded; these are the other foreign-field retention
    points, and they land in the same app.json read at every runtime load."""

    def test_a_carried_interface_value_is_bounded(self, tmp_path):
        long = "z" * (plugin_import.MAX_MANIFEST_STRING_CHARS + 400)
        root = _package(tmp_path, interface={"homepage": long})
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        carried = json.dumps(emitted)
        assert long not in carried, "an unbounded interface value reached app.json"
        assert any("characters; truncated" in w for w in report.warnings)

    def test_the_keyword_list_is_bounded(self, tmp_path):
        many = [f"k{i}" for i in range(plugin_import.MAX_MANIFEST_CONTAINER_ITEMS + 60)]
        root = _package(tmp_path, keywords=many)
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert (
            len(emitted.get("keywords", [])) <= plugin_import.MAX_MANIFEST_CONTAINER_ITEMS
        ), "the keyword list grew past the bound"
        assert any("items; the rest are dropped" in w for w in report.warnings)


class TestEveryEmittedManifestFieldIsBounded:
    def test_display_name_and_description_cannot_grow_the_emitted_manifest(self, tmp_path):
        """Three rounds found three different unbounded fields in this one dict,
        so the bound is applied to the ASSEMBLED manifest rather than per field."""
        over = "z" * (plugin_import.MAX_MANIFEST_STRING_CHARS + 500)
        root = _package(tmp_path, display_name=over, description=over)
        out = tmp_path / "out"
        convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert over not in json.dumps(emitted), "an unbounded field reached app.json"
        for key in ("displayName", "description"):
            if key in emitted:
                assert len(emitted[key]) <= plugin_import.MAX_MANIFEST_STRING_CHARS, key

    def test_the_apps_name_is_not_silently_shortened(self, tmp_path):
        """`name` is an IDENTITY, so the bound must not reach it: a truncated name
        emits a DIFFERENT app rather than a smaller one. Its own validator owns it.
        """
        root = _package(tmp_path)
        out = tmp_path / "out"
        convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert emitted["name"] == plugin_import.normalize_app_name(emitted["name"])


class TestTheNameIsRefusedRatherThanShortened:
    def test_an_overlong_derived_name_is_refused(self):
        """KEBAB_RE bounds the ALPHABET and hyphen placement, not the length, so a
        100k-character kebab string satisfied the contract. The name is retained in
        app.json AND used as a directory segment, and it is an identity, so it is
        refused: a truncated name designates a different app and could collide with
        a real one.
        """
        ok = "a" * plugin_import.MAX_APP_NAME_CHARS
        assert plugin_import.normalize_app_name(ok) == ok, "the control regressed"
        with pytest.raises(plugin_import.PluginImportError) as exc:
            plugin_import.normalize_app_name("a" * (plugin_import.MAX_APP_NAME_CHARS + 1))
        assert exc.value.code == "invalid_app_name"


class TestTheProvenanceBlockIsBoundedToo:
    def test_the_bound_covers_the_field_attached_last(self, tmp_path):
        """The bounding pass ran BEFORE importedPlugin was attached, so provenance
        escaped it -- the pass covered what was assembled above it and nothing added
        below. Bounding the finished object is the placement where a field added
        later is inside it by construction.

        Driven through an unknown manifest KEY rather than an oversized VALUE: the
        carried block is bounded upstream as it is read, so an oversized value never
        reaches provenance and would make this test vacuous. A key name lands in the
        report's own warning TEXT, which is built inside the converter and is only
        ever bounded by the final pass.
        """
        over = "k" * (plugin_import.MAX_MANIFEST_STRING_CHARS + 400)
        root = _package(tmp_path, **{over: "x"})
        out = tmp_path / "out"
        convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        provenance = emitted["importedPlugin"]
        assert any(
            "not read by this converter" in w for w in provenance["warnings"]
        ), "the control is gone: the unknown key no longer reaches provenance"
        assert over not in json.dumps(
            provenance
        ), "an unbounded key name survived inside the provenance block"


class TestProvenanceCarriesTheFinalBoundingWarnings:
    """The bounding pass runs LAST and appends its own warnings, so a provenance
    block snapshotted before it persists a warning list missing exactly the
    truncations that pass performed. A reader needs those: a silently shortened
    list is the dangerous direction.
    """

    def test_a_truncation_by_the_final_pass_reaches_the_persisted_provenance(self, tmp_path):
        import json as _json

        from kiro_crew.apps import plugin_import as pi

        long_description = "d" * (pi.MAX_MANIFEST_STRING_CHARS + 50)
        root = _package(tmp_path, skills="./skills", description=long_description)
        _skill(root, "skills")
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)

        # CONTROL: the final pass must actually have warned. `description` is
        # bounded ONLY by that pass, which is why it is the field used here -- a
        # field bounded upstream would warn before provenance is built and the
        # assertion below would pass with the bug still in place.
        truncation_warnings = [w for w in report.warnings if "description" in w]
        assert truncation_warnings, (
            "control failed: the final bounding pass emitted no warning for an "
            f"over-long description, so this test proves nothing. warnings={report.warnings}"
        )

        emitted = _json.loads((out / "app.json").read_text(encoding="utf-8"))
        persisted = emitted["importedPlugin"]["warnings"]
        missing = [w for w in truncation_warnings if w not in persisted]
        assert not missing, (
            "the persisted provenance omits warnings the final bounding pass "
            f"added, so the record does not say what was truncated: {missing}"
        )

    def test_an_unwritable_output_is_refused_not_a_traceback(self, tmp_path, monkeypatch):
        """The app.json write was unguarded, so an OSError escaped past the CLI's
        only except arm as a traceback. It is refused with the code the staging
        directory already uses, since the write lands inside that directory."""
        import pathlib

        root = _package(tmp_path, skills="./skills")
        _skill(root, "skills")

        real_write = pathlib.Path.write_text

        def _deny(self, *a, **k):
            if self.name == "app.json":
                raise OSError(28, "No space left on device")
            return real_write(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "write_text", _deny)
        with pytest.raises(PluginImportError) as exc:
            convert_plugin_package(root, tmp_path / "out")
        assert exc.value.code == "staging_unwritable", exc.value.code
        assert "app.json" in str(exc.value)


class TestTheServerCapBoundsWhatIsRetained:
    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/src", manifest_path="/src/manifest.json", source_format="x", app_name="a"
        )

    def test_refused_servers_count_against_the_cap(self):
        """A cap on EMITTED servers bounds nothing a refused entry retains.

        Every refusal path appends a warning or an unmapped record, so a manifest
        of nothing but refused servers leaves the emitted dict empty forever while
        the report grows with the input.
        """
        report = self._report()
        servers = {
            f"s{i}": {"command": "./bin/server"} for i in range(plugin_import.MAX_MCP_SERVERS + 50)
        }
        cleaned = plugin_import._convert_mcp_servers(Path("/src"), servers, report)

        assert cleaned == {}
        assert len(report.unmapped) <= plugin_import.MAX_MCP_SERVERS
        assert any("the rest are dropped" in w for w in report.warnings)

    def test_servers_within_the_cap_are_all_still_emitted(self):
        # CONTROL. Counting inspected entries must not start dropping entries that
        # convert correctly, which a cap applied one entry too early would do.
        report = self._report()
        servers = {f"s{i}": {"command": "npx"} for i in range(plugin_import.MAX_MCP_SERVERS)}
        cleaned = plugin_import._convert_mcp_servers(Path("/src"), servers, report)

        assert len(cleaned) == plugin_import.MAX_MCP_SERVERS
        assert not [w for w in report.warnings if "the rest are dropped" in w]


class TestOneSkillTreeIsBoundedNotJustItsDepthAndItsFiles:
    def test_a_wide_tree_stops_at_the_file_budget(self, tmp_path):
        """Depth bounds how DEEP the walk goes, never how wide.

        The per-file size bound answers a different question again, so a directory
        of many small files copied without limit.
        """
        src = tmp_path / "skill"
        src.mkdir()
        for i in range(plugin_import.MAX_SKILL_FILES + 25):
            (src / f"f{i}.txt").write_text("x", encoding="utf-8")

        copied, skipped = plugin_import._copy_tree_without_links(src, tmp_path / "out")

        assert copied == plugin_import.MAX_SKILL_FILES
        assert any("files in this skill" in line for line in skipped)

    def test_the_budget_is_shared_across_the_whole_tree(self, tmp_path):
        # The budget has to be ONE allowance for the tree: a per-directory cap is
        # not a cap on a tree, because breadth is unbounded either way.
        src = tmp_path / "skill"
        per_dir = (plugin_import.MAX_SKILL_FILES // 2) + 10
        for d in ("a", "b"):
            (src / d).mkdir(parents=True)
            for i in range(per_dir):
                (src / d / f"f{i}.txt").write_text("x", encoding="utf-8")

        copied, _skipped = plugin_import._copy_tree_without_links(src, tmp_path / "out")

        assert copied == plugin_import.MAX_SKILL_FILES

    def test_an_ordinary_skill_tree_is_copied_whole(self, tmp_path):
        # CONTROL. Without this, a budget of zero would satisfy both tests above
        # while making every import empty.
        src = tmp_path / "skill"
        (src / "nested").mkdir(parents=True)
        (src / "SKILL.md").write_text("hello", encoding="utf-8")
        (src / "nested" / "ref.txt").write_text("ref", encoding="utf-8")

        copied, skipped = plugin_import._copy_tree_without_links(src, tmp_path / "out")

        assert copied == 2
        assert skipped == []


class TestSkillDiscoveryIsBoundedWhileItWalks:
    def test_discovery_stops_at_the_skill_cap(self, tmp_path):
        """A package can be WIDE as well as deep, and a walk that finds the whole
        population before anyone counts it has already spent the memory the cap
        exists to bound."""
        root = tmp_path / "skills"
        for i in range(plugin_import.MAX_SKILLS + 40):
            d = root / f"s{i}"
            d.mkdir(parents=True)
            (d / plugin_import.SKILL_ENTRY_FILENAME).write_text("x", encoding="utf-8")

        found = plugin_import._discover_skill_dirs(root)

        assert len(found) == plugin_import.MAX_SKILLS

    def test_an_ordinary_package_is_discovered_whole(self, tmp_path):
        # CONTROL. Without this, a cap of zero would satisfy the test above while
        # making every package look empty.
        root = tmp_path / "skills"
        for name in ("alpha", "beta", "gamma"):
            d = root / name
            d.mkdir(parents=True)
            (d / plugin_import.SKILL_ENTRY_FILENAME).write_text("x", encoding="utf-8")

        found = plugin_import._discover_skill_dirs(root)

        assert sorted(p.name for p in found) == ["alpha", "beta", "gamma"]

    def test_the_frontier_is_bounded_when_no_directory_carries_a_marker(
        self, tmp_path, monkeypatch
    ):
        """MAX_SKILLS bounds the ANSWER, and a marker-free tree never advances it.

        Asserted on DIRECTORIES VISITED, because the return value cannot express
        this: a marker-free tree answers with an empty list whether the frontier is
        bounded or not, so an assertion about the result passes with the bound
        removed. What the bound governs is how much of the tree is held at once.
        """
        root = tmp_path / "skills"
        # Wide at every level and carrying no entry file anywhere: 3 levels of 30
        # gives 27,900 directories, above MAX_SKILL_TREE_DIRS, and `found` stays
        # empty throughout so MAX_SKILLS can never fire.
        for i in range(30):
            for j in range(30):
                for k in range(30):
                    (root / f"a{i}" / f"b{j}" / f"c{k}").mkdir(parents=True)

        visited: list = []
        real = plugin_import._bounded_sorted_entries

        def counting(path):
            visited.append(path)
            return real(path)

        monkeypatch.setattr(plugin_import, "_bounded_sorted_entries", counting)

        found = plugin_import._discover_skill_dirs(root)

        assert found == []
        assert len(visited) <= plugin_import.MAX_SKILL_TREE_DIRS + 1, (
            f"the walk visited {len(visited)} directories against a frontier bound "
            f"of {plugin_import.MAX_SKILL_TREE_DIRS}"
        )

    def test_an_over_deep_directory_is_never_enqueued(self, tmp_path, monkeypatch):
        """The depth limit is applied where a directory is ENQUEUED.

        Popping first and discarding afterwards holds the whole over-deep level in
        the frontier before deciding none of it is wanted, which is the memory the
        limit exists not to spend. Both orders VISIT the same set, so a spy on the
        listing cannot tell them apart -- what separates them is whether an
        over-deep child is probed at all, and a child that is never probed cannot
        have been appended. Asserted on the link probe each candidate passes
        through immediately before it joins the frontier.
        """
        deep = tmp_path / "skills"
        for i in range(plugin_import.MAX_SKILL_TREE_DEPTH + 4):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)
        root = tmp_path / "skills"

        probed: list = []
        real = plugin_import.is_link_or_junction

        def counting(path):
            probed.append(path)
            return real(path)

        monkeypatch.setattr(plugin_import, "is_link_or_junction", counting)

        plugin_import._discover_skill_dirs(root)

        # The marker probe fires for every directory VISITED and says nothing about
        # the frontier; the entry probes are the enqueue candidates.
        candidates = [p for p in probed if p.name != plugin_import.SKILL_ENTRY_FILENAME]
        depths = [len(p.relative_to(root).parts) for p in candidates]
        assert depths, "no directory was probed at all, so this measures nothing"
        assert max(depths) <= plugin_import.MAX_SKILL_TREE_DEPTH, (
            f"a directory at depth {max(depths)} was probed for the frontier against "
            f"a limit of {plugin_import.MAX_SKILL_TREE_DEPTH}"
        )


class TestTheManifestIsBoundedBeforeItIsRead:
    """The bound has to run before the read, not after it.

    Every other bound in the module applies to a value already parsed, which is the
    right place for a bound on what is RETAINED -- but a manifest is read whole in
    one call, so a bound that runs afterwards has already let the allocation happen.
    """

    def test_an_oversized_manifest_is_refused_without_being_read(self, tmp_path, monkeypatch):
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(plugin_import, "MAX_MANIFEST_BYTES", 1)
        reads: list[str] = []
        real_read_text = Path.read_text

        def _record(self, *args, **kwargs):
            reads.append(str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _record)

        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import._read_json_object(manifest, "the manifest")

        assert caught.value.code == "manifest_unreadable"
        # UNREAD is the property. A refusal that reads first still spends the memory.
        assert str(manifest) not in reads

    def test_an_ordinary_manifest_is_still_read(self, tmp_path):
        # CONTROL. Without this, a bound of zero would satisfy the test above while
        # refusing every manifest there is.
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"name": "demo"}), encoding="utf-8")
        assert plugin_import._read_json_object(manifest, "the manifest") == {"name": "demo"}


class TestTheSchemaProbeIsBoundedLikeItsSibling:
    """The probe and `_read_json_object` read the same kind of file the same way.

    The probe's own comment already says the two must match on their exception arms.
    The SIZE bound is the other half of matching, and the probe is reached by MORE
    inputs than its sibling: it runs on a root `plugin.json` on every import,
    including a directory that turns out not to be a plugin at all.
    """

    def test_an_oversized_root_manifest_is_not_read(self, tmp_path, monkeypatch):
        manifest = tmp_path / "plugin.json"
        _write_json(manifest, {"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1"})
        monkeypatch.setattr(plugin_import, "MAX_MANIFEST_BYTES", 1)
        reads: list[str] = []
        real_read_text = Path.read_text

        def _record(self, *args, **kwargs):
            reads.append(str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _record)

        assert plugin_import._is_schema_qualified(manifest) is False
        assert str(manifest) not in reads

    def test_an_ordinary_root_manifest_still_qualifies(self, tmp_path):
        # CONTROL. Without this, refusing every root manifest would satisfy the test
        # above while making schema-qualified discovery impossible.
        manifest = tmp_path / "plugin.json"
        _write_json(manifest, {"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1"})
        assert plugin_import._is_schema_qualified(manifest) is True


class TestADirectoryListingIsBoundedAtTheIterator:
    """`sorted()` exhausts its iterator before it can order anything.

    So a cap applied to the RESULT runs after the whole directory is already in
    memory: a package shipping a directory of a million names is materialised in
    full by the sort, whatever the later limit says. Asserted on how many entries
    are CONSUMED, which is the only thing that distinguishes the two.
    """

    def test_it_stops_consuming_at_the_cap(self, tmp_path, monkeypatch):
        d = tmp_path / "wide"
        d.mkdir()
        for i in range(60):
            (d / f"e{i:03d}").touch()

        monkeypatch.setattr(plugin_import, "MAX_DIR_ENTRIES", 10)
        consumed = {"n": 0}
        real_iterdir = Path.iterdir

        def _counting(self):
            for item in real_iterdir(self):
                consumed["n"] += 1
                yield item

        monkeypatch.setattr(Path, "iterdir", _counting)
        entries, complete = plugin_import._bounded_sorted_entries(d)

        assert len(entries) == 10
        assert complete is False
        # 10 taken plus the ONE lookahead that decides completeness. A result-side
        # cap would have consumed all 60 and still returned 10.
        assert consumed["n"] == 11, f"consumed {consumed['n']} entries against a cap of 10"

    def test_a_directory_within_the_cap_reads_complete(self, tmp_path, monkeypatch):
        # CONTROL. Without this, reporting every listing as truncated would satisfy
        # the test above while making every import look partial.
        d = tmp_path / "narrow"
        d.mkdir()
        for name in ("b", "a", "c"):
            (d / name).touch()
        monkeypatch.setattr(plugin_import, "MAX_DIR_ENTRIES", 10)
        entries, complete = plugin_import._bounded_sorted_entries(d)
        assert complete is True
        assert [e.name for e in entries] == ["a", "b", "c"], "and the order still sorts"

    def test_the_vendor_scan_reads_only_what_the_bound_handed_it(self, tmp_path, monkeypatch):
        """The helper being bounded says nothing about its CALLERS.

        `find_plugin_manifest` searches for a vendor-prefixed directory, and a glob
        walks the directory exactly as a listing does, so the call site needs its own
        pin. Asserted by narrowing what the BOUND reports and requiring the scan to
        honour that: a site globbing the directory itself would find the vendor
        manifest anyway and never notice the bound at all.

        Deliberately not asserted by counting consumed entries. `Path.glob` does not
        go through `Path.iterdir`, so a spy on the latter is blind to the very
        mutation this exists to catch: it stays green with the bound removed.
        """
        root = tmp_path / "pkg"
        vendor = root / ".acme-plugin"
        vendor.mkdir(parents=True)
        (vendor / plugin_import.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")

        real = plugin_import._bounded_sorted_entries
        calls: list[Path] = []

        def _narrow(path, *a, **k):
            calls.append(path)
            # The bound reported nothing for this directory, e.g. because the vendor
            # entry sorts past MAX_DIR_ENTRIES in a package with many children.
            return [], False

        monkeypatch.setattr(plugin_import, "_bounded_sorted_entries", _narrow)
        with pytest.raises(plugin_import.PluginImportError):
            plugin_import.find_plugin_manifest(root)
        assert root in calls, "the vendor scan never consulted the bound"

        # CONTROL. The scan must still resolve the manifest when the bound reports
        # it, or the assertion above would also pass on a scan that finds nothing.
        monkeypatch.setattr(plugin_import, "_bounded_sorted_entries", real)
        found, fmt = plugin_import.find_plugin_manifest(root)
        assert found == vendor / plugin_import.MANIFEST_FILENAME
        assert fmt == plugin_import.FORMAT_VENDOR_DIRECTORY


class TestAContainerIsBoundedByWhatItInspects:
    """A cap on the RETAINED result does not bound what a dropped entry produces.

    Every branch that drops an entry appends a warning, and a dropped entry never
    advances the retained count, so a container of nothing but dropped entries grows
    the report once per input entry with the cap never firing. This is the rule
    ``_convert_mcp_servers`` states in its own comment, applied to the two branches
    that were still counting the result.
    """

    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/tmp/src",
            manifest_path="/tmp/src/plugin.json",
            source_format=plugin_import.FORMAT_SCHEMA_QUALIFIED,
            app_name="demo",
        )

    def test_a_dict_of_dropped_keys_does_not_grow_the_report_per_entry(self):
        report = self._report()
        over = plugin_import.MAX_MANIFEST_CONTAINER_ITEMS * 20
        # Non-string keys: dropped by the key check, so ``out`` stays empty and a
        # cap on ``out`` can never fire.
        value = {i: "v" for i in range(over)}

        bounded, kept = plugin_import._bounded_manifest_value(value, "x", report, 0)

        assert kept and bounded == {}
        assert len(report.warnings) <= plugin_import.MAX_MANIFEST_CONTAINER_ITEMS + 1, (
            f"{len(report.warnings)} warnings from {over} dropped entries against a "
            f"cap of {plugin_import.MAX_MANIFEST_CONTAINER_ITEMS}"
        )

    def test_a_list_of_dropped_items_does_not_grow_the_report_per_item(self):
        report = self._report()
        over = plugin_import.MAX_MANIFEST_CONTAINER_ITEMS * 20
        # Non-finite numbers: ``kept`` is False, so ``items`` stays empty and a cap
        # on ``items`` can never fire.
        value = [float("nan")] * over

        bounded, kept = plugin_import._bounded_manifest_value(value, "x", report, 0)

        assert kept and bounded == []
        assert len(report.warnings) <= plugin_import.MAX_MANIFEST_CONTAINER_ITEMS + 1, (
            f"{len(report.warnings)} warnings from {over} dropped items against a "
            f"cap of {plugin_import.MAX_MANIFEST_CONTAINER_ITEMS}"
        )

    def test_an_ordinary_container_is_carried_whole(self):
        # CONTROL. Without this, counting every entry against a cap of zero would
        # satisfy both tests above while emitting nothing a manifest declared.
        report = self._report()
        value = {"a": 1, "b": [2, 3], "c": {"d": "e"}}

        bounded, kept = plugin_import._bounded_manifest_value(value, "x", report, 0)

        assert kept and bounded == value
        assert report.warnings == []


class TestHookDocumentsAreBoundedByWhatIsRead:
    """Nothing rejects a repeat, so the per-file byte budget bounds one read only.

    A manifest may declare one file any number of times and each declaration is
    opened, parsed and retained again. A cap on the retained list would not help
    either: a missing path appends a warning and retains no document, so the list
    stays empty while the report grows.
    """

    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/tmp/src",
            manifest_path="/tmp/src/plugin.json",
            source_format=plugin_import.FORMAT_SCHEMA_QUALIFIED,
            app_name="demo",
        )

    def test_one_file_declared_many_times_is_read_a_bounded_number_of_times(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
        over = plugin_import.MAX_HOOK_DOCUMENTS * 20

        reads: list = []
        real = plugin_import._read_json_object

        def counting(path, what):
            reads.append(path)
            return real(path, what)

        monkeypatch.setattr(plugin_import, "_read_json_object", counting)

        plugin_import._hook_files(root, ["./hooks.json"] * over, self._report())

        assert len(reads) <= plugin_import.MAX_HOOK_DOCUMENTS, (
            f"the same file was parsed {len(reads)} times against a bound of "
            f"{plugin_import.MAX_HOOK_DOCUMENTS}"
        )

    def test_missing_paths_do_not_grow_the_report_per_entry(self, tmp_path):
        root = tmp_path / "pkg"
        root.mkdir()
        report = self._report()
        over = plugin_import.MAX_HOOK_DOCUMENTS * 20

        documents = plugin_import._hook_files(root, ["./absent.json"] * over, report)

        assert documents == []
        assert len(report.warnings) <= plugin_import.MAX_HOOK_DOCUMENTS + 1, (
            f"{len(report.warnings)} warnings from {over} missing paths against a "
            f"bound of {plugin_import.MAX_HOOK_DOCUMENTS}"
        )

    def test_an_ordinary_declaration_is_read_whole(self, tmp_path):
        # CONTROL. A bound of zero would satisfy both tests above while reading no
        # hooks file a manifest declared.
        root = tmp_path / "pkg"
        root.mkdir()
        (root / "hooks.json").write_text('{"hooks": {"onSave": [1]}}', encoding="utf-8")
        report = self._report()

        documents = plugin_import._hook_files(root, ["./hooks.json"], report)

        assert documents == [{"hooks": {"onSave": [1]}}]
        assert report.warnings == []
