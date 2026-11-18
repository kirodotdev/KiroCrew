"""The plugin converter's refusals and failure reports at its other boundaries.

This file pins that a link is checked before anything follows or probes it (the
output path, a skill entry file, the package root and any ancestor of it), that a
copy failure is reported rather than swallowed (a skill whose ``SKILL.md`` did not copy
is not declared, and an unreadable subtree is skipped and recorded), that staging and
publish failures and a copy destination that cannot be created are refused with a
code, that malformed manifests and what the parser raises stay inside the coded
boundary, that case-colliding skills,
relative commands and transportless servers are refused, that installing does not
activate a disabled app and a failed publish leaves the output directory unconsumed,
that neither the report nor the import command writes live terminal controls, and
that the mapping doc cites only this repository. The converter's mapping and output
are pinned in ``test_plugin_import.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest
from plugin_import_helpers import (
    _package,
    _skill,
    _write_json,
)

from kiro_crew.apps import plugin_import
from kiro_crew.apps.plugin_import import PluginImportError, convert_plugin_package


def _probe_before_link_offenders(source: str) -> list[str]:
    """Names each boolean test that probes a path before proving it unlinked.

    A probe is any call that asks the filesystem about the path -- ``is_dir``,
    ``is_file``, ``exists``, ``stat``. On Windows those resolve the path, so
    asking one of a junction whose target is a UNC share makes the OS
    authenticate to a host the PACKAGE chose. Python's ``and``/``or`` evaluate
    left to right, so the link check has to be the earlier operand: putting it
    second means the damage is already done when the refusal runs.

    Returns one string per offending expression so a failure names the line
    rather than only the count.
    """
    import ast

    probes = {"is_dir", "is_file", "exists", "stat", "lstat"}
    offenders: list[str] = []

    def receiver_of_probe(node: ast.AST) -> str | None:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in probes
        ):
            return ast.unparse(node.func.value)
        return None

    def receiver_of_link_check(node: ast.AST) -> str | None:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "is_link_or_junction"
            and node.args
        ):
            return ast.unparse(node.args[0])
        return None

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.BoolOp):
            continue
        first_link: dict[str, int] = {}
        first_probe: dict[str, int] = {}
        for index, operand in enumerate(node.values):
            inner = operand
            while isinstance(inner, ast.UnaryOp) and isinstance(inner.op, ast.Not):
                inner = inner.operand
            for candidate in ast.walk(inner):
                name = receiver_of_link_check(candidate)
                if name is not None:
                    first_link.setdefault(name, index)
                name = receiver_of_probe(candidate)
                if name is not None:
                    first_probe.setdefault(name, index)
        for name, probe_index in first_probe.items():
            link_index = first_link.get(name)
            if link_index is not None and probe_index < link_index:
                offenders.append(
                    f"{name}: probed at operand {probe_index}, link check at {link_index}"
                )
    return offenders


class TestLinkCheckOrdering:
    """The link check must be the earlier operand, at every site, forever.

    This finding came back on three separate heads because each round fixed the
    one call site the reviewer happened to name. The ordering is the property,
    so it is asserted over the whole module rather than per site.
    """

    def test_no_boolean_test_probes_a_path_before_proving_it_unlinked(self):
        from kiro_crew.apps import plugin_import as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        offenders = _probe_before_link_offenders(source)
        assert offenders == [], (
            "a filesystem probe runs before is_link_or_junction on the same path; "
            "on Windows the probe resolves a junction, so the refusal that follows "
            f"is too late: {offenders}"
        )

    def test_the_ordering_check_detects_the_shape_it_forbids(self):
        """A detector that has never fired is indistinguishable from a broken one."""
        offenders = _probe_before_link_offenders(
            "if entry.is_dir() and not is_link_or_junction(entry):\n    pass\n"
        )
        assert len(offenders) == 1, offenders
        assert "entry" in offenders[0]

    def test_the_ordering_check_accepts_the_corrected_shape(self):
        assert (
            _probe_before_link_offenders(
                "if not is_link_or_junction(entry) and entry.is_dir():\n    pass\n"
            )
            == []
        )


class TestCopyFailuresAreReportedNotSwallowed:
    """A copy that declines an entry must not be reported as a success.

    ``_copy_tree_without_links`` deliberately records a failure and carries on
    rather than raising, because one unreadable file should not abort a whole
    import. The consequence is that its CALLER owns the question of whether what
    landed is still the thing it promised, and the emitted ``app.json`` is what an
    operator trusts.
    """

    def test_a_skill_whose_entry_file_did_not_copy_is_not_declared(self, tmp_path):
        """The one file that makes a directory a skill is SKILL.md: it is what
        ``_discover_skill_dirs`` looks for. If it does not reach the output, the
        emitted directory is not a skill, so declaring it ships an app.json naming
        a skill the loader cannot read -- and counts it in "N skill(s) copied"."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")
        out = tmp_path / "out"

        real_copy = plugin_import.shutil.copy2

        def refuse_skill_md(src, dst, *args, **kwargs):
            if Path(src).name == plugin_import.SKILL_ENTRY_FILENAME:
                raise OSError(13, "Permission denied")
            return real_copy(src, dst, *args, **kwargs)

        with mock.patch.object(plugin_import.shutil, "copy2", refuse_skill_md):
            report = plugin_import.convert_plugin_package(root, out)

        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert emitted.get("skills", []) == [], (
            "a skill whose SKILL.md never copied was declared in app.json, so the "
            "emitted app names a skill the loader cannot read"
        )
        assert not any(
            m.detail.endswith("skill(s) copied") for m in report.mapped
        ), "the report counted a skill that was not emitted"
        assert any(
            plugin_import.SKILL_ENTRY_FILENAME in w and "not emitted" in w for w in report.warnings
        ), f"no warning named the un-emitted skill: {report.warnings}"

    def test_a_skill_whose_other_files_fail_is_still_declared(self, tmp_path):
        """The complement, so the guard is a real condition and not a blanket
        refusal: losing a README costs the README, not the skill."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")
        (root / "skills" / "alpha" / "README.md").write_text("hi", encoding="utf-8")
        out = tmp_path / "out"

        real_copy = plugin_import.shutil.copy2

        def refuse_readme(src, dst, *args, **kwargs):
            if Path(src).name == "README.md":
                raise OSError(13, "Permission denied")
            return real_copy(src, dst, *args, **kwargs)

        with mock.patch.object(plugin_import.shutil, "copy2", refuse_readme):
            plugin_import.convert_plugin_package(root, out)

        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert emitted.get("skills", []) == ["skills/alpha"]
        assert (out / "skills" / "alpha" / plugin_import.SKILL_ENTRY_FILENAME).is_file()


class TestStagingAndPublishRefusalsCarryACode:
    """The CLI catches ``PluginImportError`` and nothing else, so any OSError the
    staging or publish step can raise reaches the operator as a traceback."""

    def test_an_unwritable_output_parent_is_refused_with_a_code(self, tmp_path):
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")

        with mock.patch.object(
            plugin_import.tempfile, "mkdtemp", side_effect=OSError(30, "Read-only file system")
        ):
            with pytest.raises(plugin_import.PluginImportError) as exc:
                plugin_import.convert_plugin_package(root, tmp_path / "out")

        assert exc.value.code == "staging_unwritable"
        assert "Read-only file system" in str(exc.value)

    def test_a_failed_publish_is_refused_with_its_own_code(self, tmp_path):
        """Distinct from the staging failure because the conversion SUCCEEDED and
        only the move did, which is a different sentence and a different remedy."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")
        out = tmp_path / "out"

        with mock.patch.object(
            plugin_import.os, "replace", side_effect=OSError(39, "Directory not empty")
        ):
            with pytest.raises(plugin_import.PluginImportError) as exc:
                plugin_import.convert_plugin_package(root, out)

        assert exc.value.code == "output_publish_failed"
        assert "Directory not empty" in str(exc.value)
        assert not list(
            tmp_path.glob(f".{out.name}.partial-*")
        ), "the staging directory outlived the failed publish"


class TestCaseCollidingSkillsAreRefused:
    def test_two_skills_differing_only_in_case_do_not_overwrite(self, tmp_path):
        """A case-insensitive destination (macOS, Windows) aliases `Search` and
        `search` to one directory, so without a folded identity the second copy
        overwrites the first while the check sees two distinct skills."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "Search", description="capitalised")
        _skill(root / "skills", "search", description="lowercase")
        # The SOURCE has to actually hold two directories for the collision to
        # exist, and on a case-insensitive filesystem the two _skill calls wrote to
        # ONE directory -- so there is nothing for the guard to refuse and every
        # assertion below would pass for the wrong reason. Asserted rather than
        # assumed, and skipped by measuring the filesystem instead of naming a
        # platform, because Windows and macOS both fold by default while the Linux
        # lanes do not.
        variants = sorted(p.name for p in (root / "skills").iterdir() if p.is_dir())
        if variants != ["Search", "search"]:
            pytest.skip(
                "this filesystem folded Search and search into one directory "
                f"({variants}), so a case-colliding source cannot be built here"
            )
        out = tmp_path / "out"
        report = convert_plugin_package(root, out)
        emitted = json.loads((out / "app.json").read_text(encoding="utf-8"))
        assert len(emitted.get("skills", [])) == 1, (
            "both case variants were emitted, so on a case-insensitive destination "
            "one skill silently overwrites the other"
        )
        assert any("duplicate skill directory name" in w for w in report.warnings)


class TestLinkedOutputPathIsRefused:
    def test_a_symlinked_output_directory_is_refused_before_it_is_resolved(self, tmp_path):
        """Publishing replaces out_dir by rename, so a link reaching that step is
        destroyed rather than followed. The refusal must also precede .resolve(),
        which on Windows traverses a junction."""
        root = _package(tmp_path, skills="./skills")
        _skill(root / "skills", "alpha")
        target = tmp_path / "real-target"
        target.mkdir()
        link = tmp_path / "linked-out"
        link.symlink_to(target, target_is_directory=True)

        resolved: list[str] = []
        real_resolve = Path.resolve

        def recording_resolve(self, *args, **kwargs):
            resolved.append(str(self))
            return real_resolve(self, *args, **kwargs)

        with mock.patch.object(Path, "resolve", recording_resolve):
            with pytest.raises(PluginImportError) as exc:
                convert_plugin_package(root, link)

        assert exc.value.code == "output_is_a_link"
        assert str(link) not in resolved, "the linked output path was resolved before refusal"
        assert link.is_symlink(), "the refusal destroyed the link it was meant to protect"


class TestSkillEntryFileLinkIsRefusedBeforeItIsProbed:
    def test_discovery_does_not_probe_a_linked_skill_entry_file(self, tmp_path):
        """`is_file()` RESOLVES the path, so a SKILL.md symlinked at a UNC target
        makes Windows authenticate to a host the package chose. The directory arm of
        this walk was already guarded; the entry file is the other probe in the same
        loop, so the guard had covered one of two doors.

        Asserts the ORDER rather than the outcome, the same way the declared-path
        test does: a refusal that happens after the probe is too late."""
        root = _package(tmp_path, skills="./skills")
        holder = root / "skills" / "linked"
        holder.mkdir(parents=True)
        target = tmp_path / "outside" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\ndescription: outside\n---\n", encoding="utf-8")
        (holder / plugin_import.SKILL_ENTRY_FILENAME).symlink_to(target)

        probed: list[str] = []
        real_is_file = Path.is_file

        def recording_is_file(self):
            probed.append(str(self))
            return real_is_file(self)

        with mock.patch.object(Path, "is_file", recording_is_file):
            convert_plugin_package(root, tmp_path / "out")

        linked = str(holder / plugin_import.SKILL_ENTRY_FILENAME)
        assert linked not in probed, (
            "the linked SKILL.md was probed with is_file(), which resolves it -- on "
            "Windows that hands credentials to whatever host the link names"
        )


class TestWindowsRelativeCommandsAreRefusedToo:
    """Pinned on ``_package_relative_fields`` directly rather than through a
    converted package: this detector IS the whole finding, and a unit assertion
    names the exact input at stake instead of inferring it from a report.
    """

    def test_a_backslash_relative_command_is_detected_like_its_posix_twin(self):
        """A relative command resolves against the SESSION's working directory, not
        the package, so `.\\tools\\server.exe` launches whatever sits at that path in
        the user's workspace -- the same harm as `./tools/server.exe`, which was
        already detected. Matching only POSIX prefixes left the guard depending on
        which separator the package happened to write, and Windows accepts both.
        """
        posix = plugin_import._package_relative_fields({"command": "./tools/server.exe"})
        windows = plugin_import._package_relative_fields({"command": ".\\tools\\server.exe"})
        assert posix == ["command"], f"the POSIX control regressed: {posix}"
        assert windows == ["command"], (
            "a backslash-relative command was not detected while its POSIX twin "
            "was, so the guard depended on the separator the package chose"
        )

    def test_the_parent_prefix_is_covered_on_both_separators(self):
        assert plugin_import._package_relative_fields({"command": "../up/serve"}) == ["command"]
        assert plugin_import._package_relative_fields({"command": "..\\up\\serve.exe"}) == [
            "command"
        ]

    def test_a_bare_command_is_still_not_read_as_a_path(self):
        """The detector is deliberately narrow, and widening it must not start
        reading a bare command name, a flag or a package specifier as a path."""
        assert (
            plugin_import._package_relative_fields(
                {"command": "npx", "args": ["-y", "some-mcp@latest"]}
            )
            == []
        )
        # Dotted is not the test; carrying a PROGRAM suffix is. An interpreter
        # named for its version stays a bare command, which is what keeps the
        # suffix set load-bearing rather than collapsing to "contains a dot".
        assert plugin_import._package_relative_fields({"command": "python3.11"}) == []
        assert plugin_import._package_relative_fields({"command": "docker-compose"}) == []

    def test_a_flag_carrying_a_package_relative_value_is_read_through(self):
        """A dash does not make the VALUE after ``=`` stop being a path.

        Excluding every token that starts with a dash is what let the shape this
        detector exists to catch back in through option syntax: the value of
        ``--config=./local/thing`` resolves against the session's working directory
        exactly as the bare spelling does, and the package root is no more preserved
        for it. The three non-path shapes stay excluded inside a flag too, because
        the value is re-tested by the same rule rather than by a second one.
        """
        assert plugin_import._package_relative_fields(
            {"command": "npx", "args": ["--config=./local/thing"]}
        ) == ["args[0]"]
        assert plugin_import._package_relative_fields(
            {"command": "npx", "args": ["--plugin=bin/server"]}
        ) == ["args[0]"]
        # A bare flag carries no value to test, and a flag whose value is one of the
        # three excluded shapes is still not a path.
        assert (
            plugin_import._package_relative_fields(
                {
                    "command": "npx",
                    "args": [
                        "-y",
                        "--quiet",
                        "--pkg=@scope/thing",
                        "--src=git+https://example.invalid/x.git",
                        "--level=debug",
                    ],
                }
            )
            == []
        )

    def test_a_bare_program_filename_is_read_as_package_relative(self):
        """A separator-less token carrying a program suffix names a FILE.

        This REVERSES an earlier assertion here that a bare ``server.exe`` command
        stays a bare command. Two reasons, and the second is the decisive one. On
        Windows a bare program name is not a PATH lookup: ``CreateProcess`` searches
        the calling process's directory and the CURRENT directory before consulting
        PATH, so the value resolves against the session's working directory exactly
        as a relative path does. And on every platform such a program cannot start
        after conversion anyway, because the package root is not preserved and the
        program is not a declared resource -- the same ground on which this detector
        already drops a separator-carrying program.
        """
        assert plugin_import._package_relative_fields({"command": "server.exe"}) == ["command"]
        assert plugin_import._package_relative_fields(
            {"command": "node", "args": ["server.js"]}
        ) == ["args[0]"]
        assert plugin_import._package_relative_fields(
            {"command": "python", "args": ["app.py"]}
        ) == ["args[0]"]


class TestMalformedManifestsStayInsideTheCodedBoundary:
    """Every refusal this module makes carries a code, and the CLI's only except arm
    catches PluginImportError. An exception outside that hierarchy reaches the
    operator as a traceback."""

    def test_a_manifest_that_is_not_utf8_is_refused_with_a_code(self, tmp_path):
        """read_text SUCCEEDS at the open and fails at the DECODE, and
        UnicodeDecodeError is not an OSError, so it escaped the first except arm."""
        root = _package(tmp_path)
        (root / ".codex-plugin" / "plugin.json").write_bytes(b'{"name": "\xff\xfe bad"}')
        with pytest.raises(plugin_import.PluginImportError) as exc:
            convert_plugin_package(root, tmp_path / "out")
        assert exc.value.code in {"manifest_unreadable", "manifest_not_json"}

    def test_a_recursion_error_while_parsing_becomes_a_coded_refusal(self, tmp_path):
        """``json.loads`` RECURSES, and a RecursionError is not a JSONDecodeError, so
        it escaped the except arm and reached the operator as a traceback.

        The ARM is driven directly rather than by feeding deep input: CPython's C
        scanner parses thousands of levels without raising, so a depth-based test
        picks a number that does not trigger the arm on this interpreter and passes
        for the wrong reason. What this pins is the conversion, which is the finding.
        """
        real = tmp_path / "plugin.json"
        real.write_text('{"name": "demo"}', encoding="utf-8")
        assert plugin_import._read_json_object(real, "a manifest") == {
            "name": "demo"
        }, "the control regressed: this file must parse, or the patch below proves nothing"

        def exploding_loads(*_args, **_kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        with mock.patch.object(plugin_import.json, "loads", exploding_loads):
            with pytest.raises(plugin_import.PluginImportError) as exc:
                plugin_import._read_json_object(real, "a manifest")
        assert exc.value.code == "manifest_not_json"
        assert "nests too deeply" in str(exc.value)

    def test_the_schema_probe_answers_false_rather_than_raising(self, tmp_path):
        """The probe returns a BOOLEAN, so the same two escapes would crash a caller
        that is only asking a question."""
        bad = tmp_path / "app.json"
        bad.write_bytes(b'{"$schema": "\xff\xfe"}')
        assert plugin_import._is_schema_qualified(bad) is False
        # Same arm, driven directly for the same reason as the test above.
        fine = tmp_path / "fine.json"
        fine.write_text(
            '{"$schema": "' + plugin_import.SCHEMA_NAMESPACE_PREFIX + 'x"}', encoding="utf-8"
        )
        assert plugin_import._is_schema_qualified(fine) is True, "the control regressed"

        def exploding_loads(*_args, **_kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        with mock.patch.object(plugin_import.json, "loads", exploding_loads):
            assert plugin_import._is_schema_qualified(fine) is False


class TestARelativeProgramIsDetectedByItsSeparator:
    """A leading dot is not what makes a value a path. `bin/server` is an
    ordinary manifest spelling, it resolves against the SESSION's working
    directory, and it cannot start after conversion either, because the package
    root is not preserved and the program is not a declared resource.
    """

    def test_every_relative_spelling_is_detected(self):
        from kiro_crew.apps.plugin_import import _package_relative_fields

        for command in [
            "bin/server",  # the spelling the dot-prefixed rule missed
            "tools\\server.exe",
            "./tools/server.exe",
            "../sibling/server",
            ".\\tools\\server.exe",
            "..\\sibling\\server",
            ".",
            "..",
        ]:
            assert _package_relative_fields({"command": command}) == ["command"], (
                f"{command!r} resolves against the session's working directory and "
                "was not detected"
            )

    def test_the_shapes_that_are_not_paths_stay_convertible(self):
        """CONTROL. Widening the rule must not start dropping servers that
        convert correctly: each of these carries no filesystem meaning, and three
        of them carry a slash, which is exactly why a bare separator test needs
        these exclusions stated as tests rather than assumed."""
        from kiro_crew.apps.plugin_import import _package_relative_fields

        for command in [
            "node",  # a bare name resolved through PATH
            "uvx",
            "-y",  # a flag
            "--port=8080",
            "@scope/package",  # an npm scope specifier
            "git+https://example.invalid/pkg.git",  # a URL
            "/usr/local/bin/server",  # absolute resolves against nothing local
        ]:
            assert _package_relative_fields({"command": command}) == [], (
                f"{command!r} is not a filesystem path but was flagged, so a server "
                "that converts correctly would be dropped"
            )

    def test_a_relative_arg_is_detected_too(self):
        from kiro_crew.apps.plugin_import import _package_relative_fields

        found = _package_relative_fields({"command": "node", "args": ["-y", "lib/main.js"]})
        assert found == ["args[1]"], found


class TestRefusalsCoverWhatTheParserActuallyRaises:
    def test_an_oversized_integer_is_refused_not_raised(self, tmp_path):
        """Valid JSON can still fail to parse.

        CPython refuses an integer literal past its digit limit with a plain
        ``ValueError``, not a ``JSONDecodeError``, so a narrower arm let it escape
        the coded boundary as a traceback.
        """
        manifest = tmp_path / "manifest.json"
        manifest.write_text('{"name": ' + "9" * 5000 + "}", encoding="utf-8")
        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import._read_json_object(manifest, "the manifest")
        assert caught.value.code == "manifest_not_json"

    def test_a_syntax_error_is_still_refused_the_same_way(self, tmp_path):
        # CONTROL. Widening to ValueError must keep answering an ordinary syntax
        # error, which is the case the narrower arm existed for.
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{not json", encoding="utf-8")
        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import._read_json_object(manifest, "the manifest")
        assert caught.value.code == "manifest_not_json"


class TestTheSchemaProbeAnswersRatherThanRaising:
    def test_an_oversized_integer_leaves_the_manifest_unqualified(self, tmp_path):
        """The probe is named in ``_read_json_object``'s own comment as carrying the
        same escapes, so the two arms have to be the same width: an integer past the
        interpreter's digit limit raises a PLAIN ValueError from valid JSON, which
        the narrower arm let out as a traceback."""
        manifest = tmp_path / "plugin.json"
        manifest.write_text('{"$schema": ' + "9" * 5000 + "}", encoding="utf-8")
        assert plugin_import._is_schema_qualified(manifest) is False

    def test_a_qualified_manifest_is_still_recognised(self, tmp_path):
        # CONTROL. Widening the arm must not turn the probe into a constant False,
        # which would satisfy the test above while making every package unqualified.
        manifest = tmp_path / "plugin.json"
        manifest.write_text(
            json.dumps({"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1.json"}),
            encoding="utf-8",
        )
        assert plugin_import._is_schema_qualified(manifest) is True


class TestTheCopyWalksFilesystemFailuresStayInsideTheBoundary:
    def test_an_uncreatable_destination_is_refused_with_a_code(self, tmp_path):
        """An output filesystem this walk cannot write to is not a skippable entry:
        no amount of skipping produces the app, so it is refused with the code this
        module already uses for an unwritable staging area."""
        src = tmp_path / "skill"
        src.mkdir()
        (src / "SKILL.md").write_text("hi", encoding="utf-8")
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")

        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import._copy_tree_without_links(src, blocker / "out")
        assert caught.value.code == "staging_unwritable"

    def test_an_unreadable_source_directory_is_skipped_and_recorded(self, tmp_path, monkeypatch):
        """One unreadable SUBTREE is a skippable entry, which is the answer this walk
        gives everything it declines and the answer ``_discover_skill_dirs`` already
        gives for the same call."""
        src = tmp_path / "skill"
        src.mkdir()
        real_iterdir = Path.iterdir

        def _refuse(self):
            if self == src:
                raise PermissionError(13, "Permission denied")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _refuse)
        copied, skipped = plugin_import._copy_tree_without_links(src, tmp_path / "out")

        assert copied == 0
        assert any("unreadable, skipped" in line for line in skipped)


class TestALinkedPackageRootIsRefusedBeforeItIsProbed:
    """A probe RESOLVES the path, so the refusal has to come first.

    On Windows a junction whose target is a UNC share makes the OS authenticate to
    a host the PACKAGE chose, and a refusal that runs after the probe is too late.
    ``resolve_declared_path`` covers every DECLARED path by walking its segments,
    but the root is what those are resolved against, so it is checked here.
    """

    def test_a_linked_root_is_refused(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / plugin_import.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)

        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import.find_plugin_manifest(linked)
        assert caught.value.code == "source_not_a_directory"
        assert "link" in caught.value.message

    def test_the_refusal_runs_before_anything_resolves_the_root(self, tmp_path, monkeypatch):
        # The ORDER is the property, not the refusal: a check after the probe still
        # refuses, and still leaks the credentials the probe offered. So this records
        # what was resolved and fails if the linked root was resolved at all.
        real = tmp_path / "real"
        real.mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)
        probed: list[str] = []
        real_is_dir = Path.is_dir

        def _record(self):
            probed.append(str(self))
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", _record)
        with pytest.raises(plugin_import.PluginImportError):
            plugin_import.find_plugin_manifest(linked)

        assert str(linked) not in probed

    def test_an_ordinary_root_is_still_accepted(self, tmp_path):
        # CONTROL. Without this, refusing every root would satisfy both tests above
        # while making every import fail.
        root = tmp_path / "pkg"
        root.mkdir()
        (root / plugin_import.MANIFEST_FILENAME).write_text(
            json.dumps({"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1.json"}),
            encoding="utf-8",
        )
        found, _fmt = plugin_import.find_plugin_manifest(root)
        assert found.name == plugin_import.MANIFEST_FILENAME


class TestInstallDoesNotActivateADisabledApp:
    def test_the_import_install_path_does_not_register_resources(self):
        """Installing writes the app's files; it does not turn the app on.

        Registering at install activates the app's executable resources -- its agents
        and MCP servers -- for an app that is still disabled, so the next agent
        session launches a command from a third-party package nobody enabled. The
        enable path registers, which is where that decision is made.

        Asserted on the SOURCE: the install branch ends by telling the operator to
        run enable, and driving the CLI here would install a real app into the
        developer's own data home.
        """
        import ast
        from pathlib import Path as _Path

        import kiro_crew.cli_commands_app as cli

        tree = ast.parse(_Path(cli.__file__).read_text(encoding="utf-8"))
        target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_app_import"
        )
        called = {
            n.func.id
            for n in ast.walk(target)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "install_app" in called, "the install branch no longer installs, so this is vacuous"
        assert "register_app" not in called

    def test_the_enable_path_still_registers(self):
        # CONTROL. Removing registration from install is only correct because enable
        # does it; without this, deleting BOTH would satisfy the test above.
        import ast
        from pathlib import Path as _Path

        import kiro_crew.cli_commands_app as cli

        tree = ast.parse(_Path(cli.__file__).read_text(encoding="utf-8"))
        target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_app"
        )
        called = {
            n.func.id
            for n in ast.walk(target)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "register_app" in called


class TestAFailedPublishDoesNotConsumeTheOutputDirectory:
    def test_an_empty_out_dir_the_caller_made_is_restored(self, tmp_path, monkeypatch):
        """A refusal must not also delete something that was already there.

        The publish removes an empty `out_dir` because `os.replace` cannot land on an
        existing directory. If the move then fails, that removal is the only lasting
        effect of a command that reported failure.
        """
        source = _package(tmp_path)
        out_dir = tmp_path / "demo-app"
        out_dir.mkdir()

        def _boom(src, dst):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(plugin_import.os, "replace", _boom)

        with pytest.raises(plugin_import.PluginImportError) as caught:
            plugin_import.convert_plugin_package(source, out_dir)

        assert caught.value.code == "output_publish_failed"
        assert out_dir.is_dir(), "the caller's directory was consumed by a failed import"

    def test_a_successful_publish_still_lands(self, tmp_path):
        # CONTROL. Without this, never removing out_dir would satisfy the test above
        # while making every publish fail.
        source = _package(tmp_path)
        out_dir = tmp_path / "demo-app2"
        out_dir.mkdir()
        plugin_import.convert_plugin_package(source, out_dir)
        assert (out_dir / "app.json").is_file()


class TestTheReportCannotForgeTerminalOutput:
    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/tmp/src",
            manifest_path="/tmp/src/plugin.json",
            source_format=plugin_import.FORMAT_SCHEMA_QUALIFIED,
            app_name="demo",
        )

    def test_manifest_text_reaches_the_terminal_without_live_controls(self):
        """Every rendered field is manifest-derived and printed on its own line.

        Untouched, one carrying an escape could repaint the screen, and one carrying
        a newline could open a line that reads as this tool's own output.
        """
        report = self._report()
        report.warnings.append("\x1b[2Jcleared\nnot mapped:\n  forged -> line")
        text = report.render_text()
        assert "\x1b" not in text, "a live escape reached the terminal"
        assert "\\x0a" in text, "a newline could open a line that reads as our own"

    def test_ordinary_text_is_left_readable(self):
        # CONTROL. Without this, deleting every field would satisfy the test above.
        report = self._report()
        report.warnings.append("no equivalent for hooks")
        text = report.render_text()
        assert "no equivalent for hooks" in text
        assert "demo" in text


class TestTheImportCommandCannotBeMadeToWriteLiveTerminalControls:
    """Every refusal this command prints carries text taken from the package under
    conversion, after a ``❌`` prefix on the operator's terminal.

    The pin is at the PRINT SITE rather than on one upstream message, because which
    refusal happens to embed a package-supplied path changes as the converter grows,
    while the property the operator depends on does not: nothing reaching the
    terminal from a foreign package may move the cursor or open a line that reads as
    this tool's own output. The helpers are imported at ``cli_commands`` module scope,
    so that module holds its own binding and is what a patch has to replace -- patching
    the definition module would leave the command calling the real function.
    """

    HOSTILE = "pkg\x1b[2Kwiped\r\nnot a prefixed line"

    def _assert_inert(self, captured: str) -> None:
        assert "\x1b" not in captured, "a live escape reached the terminal"
        # One printed line: the newline is rendered as its visible literal, so no
        # unprefixed line can pass for this command's own output.
        body = [ln for ln in captured.splitlines() if ln.strip()]
        assert len(body) == 1, f"the refusal spanned {len(body)} lines: {body!r}"
        assert "\\x0a" in body[0]

    def test_a_conversion_refusal_cannot_carry_controls(self, tmp_path, monkeypatch, capsys):
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew.cli_commands import _handle_app_import

        root = _package(tmp_path)

        def _boom(*_a, **_k):
            raise PluginImportError("staging_unwritable", f"cannot write under {self.HOSTILE}")

        # Patched on CLI_COMMANDS, not on plugin_import: the command imports these
        # names at module scope, so it holds its own binding and replacing the
        # definition module's attribute would leave the command calling the real one.
        monkeypatch.setattr(cli_commands, "convert_plugin_package", _boom)
        with pytest.raises(SystemExit):
            _handle_app_import(
                argparse.Namespace(source=str(root), name=None, out=str(tmp_path / "out"))
            )
        self._assert_inert(capsys.readouterr().err)

    def test_a_manifest_read_failure_cannot_carry_controls(self, tmp_path, monkeypatch, capsys):
        """The OSError arm prints ``str(exc)``, which carries the offending
        filename -- and that name comes from the package."""
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew.cli_commands import _handle_app_import

        root = _package(tmp_path)

        def _boom(*_a, **_k):
            raise OSError(f"cannot open {self.HOSTILE}")

        # Patched on CLI_COMMANDS, not on plugin_import: the command imports these
        # names at module scope, so it holds its own binding and replacing the
        # definition module's attribute would leave the command calling the real one.
        monkeypatch.setattr(cli_commands, "read_manifest_name", _boom)
        with pytest.raises(SystemExit):
            _handle_app_import(
                argparse.Namespace(source=str(root), name=None, out=str(tmp_path / "out"))
            )
        self._assert_inert(capsys.readouterr().err)


class TestALinkedANCESTORIsRefusedBeforeAnyProbe:
    """A leaf-only link check still resolves through a linked PARENT.

    The `is_dir()` that follows is the probe that does it: on Windows an ancestor
    junction whose target is a UNC share turns that call on a local-looking path into
    an outbound SMB connection authenticating as this process, to a host the PACKAGE
    chose. A lexical UNC screen cannot catch it, because the path being probed is not
    itself UNC-shaped -- only the link's target is. The repo's own
    `first_linked_ancestor` documents this and walks root-first so the walk never
    traverses a link, and the sibling check in `member_essential_context` already
    pairs it with the leaf test.
    """

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks on this platform")
    def test_a_package_under_a_linked_ancestor_is_refused(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "pkg").mkdir(parents=True)
        (elsewhere / "pkg" / "plugin.json").write_text("{}", encoding="utf-8")
        # The link stands at an ANCESTOR of the root, not at the root.
        link = tmp_path / "via"
        link.symlink_to(elsewhere, target_is_directory=True)
        root = link / "pkg"

        with pytest.raises(plugin_import.PluginImportError) as exc:
            plugin_import.find_plugin_manifest(root)

        assert exc.value.code == "source_not_a_directory"
        # The offending ancestor is deliberately NOT named: which ancestor is a link
        # is filesystem layout the caller supplied, so naming it adds nothing.
        assert "via" not in str(exc.value) or str(root) in str(exc.value)

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks on this platform")
    def test_the_leaf_itself_is_still_refused(self, tmp_path):
        # The ancestor check must ADD to the leaf check, not replace it.
        real = tmp_path / "real"
        real.mkdir()
        (real / "plugin.json").write_text("{}", encoding="utf-8")
        root = tmp_path / "leaf"
        root.symlink_to(real, target_is_directory=True)

        with pytest.raises(plugin_import.PluginImportError) as exc:
            plugin_import.find_plugin_manifest(root)

        assert exc.value.code == "source_not_a_directory"

    def test_an_ordinary_package_is_still_found(self, tmp_path):
        # CONTROL. Without this, refusing every root would satisfy both tests above
        # while making every import impossible.
        root = tmp_path / "pkg"
        root.mkdir()
        _write_json(
            root / "plugin.json",
            {"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1", "name": "demo"},
        )

        found, fmt = plugin_import.find_plugin_manifest(root)

        assert found == root / "plugin.json"
        assert fmt == plugin_import.FORMAT_SCHEMA_QUALIFIED


class TestATransportlessServerIsNotWritten:
    """An entry naming no way to reach the server is not a server.

    Nothing else catches it: an empty or transportless object is a dict, carries no
    package-relative field to refuse, and the bounding pass keeps it because nothing
    in it is over-limit -- so it reached app.json as a server with no command and no
    url, which no reader can launch and no message mentioned. A manifest author
    producing one is an ordinary mistake, not a crafted input.
    """

    @staticmethod
    def _report():
        return plugin_import.ImportReport(
            source_root="/tmp/src",
            manifest_path="/tmp/src/plugin.json",
            source_format=plugin_import.FORMAT_SCHEMA_QUALIFIED,
            app_name="demo",
        )

    def test_an_empty_config_is_dropped_and_named(self, tmp_path):
        report = self._report()

        cleaned = plugin_import._convert_mcp_servers(tmp_path, {"ghost": {}}, report)

        assert cleaned == {}, f"a transportless server was written: {cleaned}"
        assert any("ghost" in w for w in report.warnings), report.warnings

    def test_a_config_with_fields_but_no_transport_is_dropped(self, tmp_path):
        # The empty dict is the obvious case; this is the one that reads as a real
        # entry. Everything here is valid JSON of the right shape and still names
        # nothing to run or connect to.
        report = self._report()
        declared = {"ghost": {"env": {"TOKEN": "x"}, "disabled": False}}

        cleaned = plugin_import._convert_mcp_servers(tmp_path, declared, report)

        assert cleaned == {}, f"a transportless server was written: {cleaned}"

    def test_a_command_server_is_still_written(self, tmp_path):
        # CONTROL. Without this, refusing every entry would satisfy both tests above
        # while dropping every server a manifest legitimately declares.
        report = self._report()
        declared = {"real": {"command": "/usr/bin/thing", "args": ["--serve"]}}

        cleaned = plugin_import._convert_mcp_servers(tmp_path, declared, report)

        assert cleaned == declared
        assert report.warnings == []

    def test_a_url_server_is_still_written(self, tmp_path):
        # The other transport spelling, so the check cannot be read as command-only.
        report = self._report()
        declared = {"remote": {"url": "https://example.invalid/mcp"}}

        cleaned = plugin_import._convert_mcp_servers(tmp_path, declared, report)

        assert cleaned == declared
        assert report.warnings == []


class TestTheMappingDocCitesOnlyThisRepo:
    """The mapping doc is the converter's reason for every kind it skips, so a
    reader has to be able to resolve what it cites.

    It was written against a companion contract that ships in a DIFFERENT pull
    request, and the citations survived the split: every "section 3-5", "section 7"
    and named diff pointed at a document absent from this tree, which reads as a
    broken reference rather than as future work. Pinned by TOKEN rather than by
    reviewing the prose, because the failure is silent -- nothing in a docs lint
    can tell a citation of an absent document from a citation of a present one.
    """

    _DOC = Path(__file__).resolve().parents[1] / (
        "docs/system-specs/modules/harness-plugin-mapping.md"
    )

    #: Each token appeared ONLY in this doc, so each one dangles by construction.
    _ABSENT = (
        "contribution protocol",
        "stale_seq",
        "10,000 events",
        "sections 3-5",
        "the contract",
        "Diff D",
        "our section",
    )

    def test_the_doc_cites_no_document_absent_from_this_tree(self):
        text = self._DOC.read_text(encoding="utf-8")
        dangling = sorted(t for t in self._ABSENT if t in text)
        assert not dangling, (
            f"the mapping doc cites {dangling}, which resolve nowhere in this tree: "
            "either the cited document must land on the base branch first, or the "
            "citation must say what it means without naming it"
        )

    def test_the_doc_still_carries_the_matrix_it_exists_for(self):
        # CONTROL. Without this, deleting the doc outright would satisfy the test
        # above while removing the reason the converter reports for every skip.
        text = self._DOC.read_text(encoding="utf-8")
        assert "## 3. The matrix" in text, "the matrix is what the converter cites"
        for bucket in ("**(a) ", "**(b) ", "**(c) ", "**(d) "):
            assert bucket in text, f"bucket {bucket!r} is gone, so a row can name no bucket"

    def test_a_proposal_set_does_not_live_in_this_spec(self):
        """Proposals belong in docs/request-for-change/, whose README calls itself
        'a proposal and a record of a decision'. A system spec describes what the
        code does, so a section of unapplied diffs here is the wrong genre in the
        wrong directory -- and its own diffs cited the absent contract."""
        text = self._DOC.read_text(encoding="utf-8")
        assert "Proposed diffs" not in text
        assert "Proposals, not applied" not in text
