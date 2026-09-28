"""Behavioural tests for scripts/check_builtin_skill_scope.py.

Local backlog f-20260818-08. Everything under ``src/kiro_crew/builtin_skills/``
installs on every machine that installs Kiro Crew, so a skill body naming THIS
repository resolves for exactly one reader and points an agent at a path that does
not exist for anyone else. ``prepare-pr`` already solved it -- repository
specifics live in a profile keyed by repository -- and this gate keeps the rest of
the tree from regressing.

The rule is absolute: no baseline, no recorded exemptions. Three properties carry
it and all three are tested rather than assumed -- the marker set must catch
repository shapes WITHOUT catching the product surface a builtin skill exists to
describe, the exemption must not be inheritable by a nested or look-alike
directory, and a file the gate cannot read must fail rather than pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from skill_script_helpers import load_skill_script

from conftest import requires_symlinks

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_builtin_skill_scope.py"
SKILL_REL = "src/kiro_crew/builtin_skills"


# The name the injection test needs contains LF and ':', both of which Windows
# rejects outright (OSError 22). PROBE for it rather than guessing the platform,
# the same way test/conftest.py's requires_symlinks probes for the symlink
# privilege instead of blanket-skipping Windows: the assertion is about a name a
# fork author could commit, so it should run wherever such a name can exist.
#
# Skipping it loses nothing about the DEFENCE -- the escaping itself is covered by
# formatter tests that run on every platform. What this one adds is proof the
# scanner reaches such a file at all, which only means anything where the file
# can be created.
def _can_create_exotic_filename() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        try:
            Path(tmp, "a\n::b.md").write_text("x", encoding="utf-8")
        except (OSError, ValueError):
            return False
        return True


requires_exotic_filenames = pytest.mark.skipif(
    not _can_create_exotic_filename(),
    reason="this platform rejects a filename containing LF or ':'",
)


@pytest.fixture(scope="module")
def gate():
    return load_skill_script("check_builtin_skill_scope", SCRIPT)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / SKILL_REL / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestTheMarkerSet:
    """Repository shapes are markers; the product surface is not. Getting the
    second half wrong would gate the content builtin skills exist to carry."""

    @pytest.mark.parametrize(
        "text",
        [
            "File it on kirodotdev/KiroCrew please.",
            "Edit `src/kiro_crew/dashboard/state.py`.",
            "Run `test/test_prepare_pr_status.py` first.",
            "Wire it into `.github/workflows/ci.yml`.",
            "See .github/workflows/pr-readiness.yaml",
        ],
    )
    def test_repository_shapes_are_flagged(self, gate, text: str) -> None:
        assert gate.marker_hits(text + "\n"), text

    @pytest.mark.parametrize(
        "text",
        [
            "See https://github.com/kirodotdev/KiroCrew/blob/main/README.md",
            "Contract: [theming](https://github.com/kirodotdev/KiroCrew/blob/main/x.md)",
        ],
    )
    def test_a_resolvable_public_url_is_not_a_marker(self, gate, text: str) -> None:
        """This repository is PUBLIC (checked, not assumed), so a github.com URL
        into it resolves on every machine -- it is a citation, not an instruction
        only a contributor can act on. Treating both alike forces a downgrade: it
        replaces a fetchable link to the authoritative theming contract with prose
        an installed reader cannot follow."""
        assert gate.marker_hits(text + "\n") == [], text

    def test_a_bare_slug_next_to_a_url_is_still_flagged(self, gate) -> None:
        """The narrowing is a lookbehind on `github.com/`, not a whole-line
        exemption: a line may carry both a citation and a real instruction."""
        hits = gate.marker_hits(
            "See https://github.com/kirodotdev/KiroCrew/blob/main/x.md, "
            "then file it on kirodotdev/KiroCrew.\n"
        )
        assert [name for _line, name, _text in hits] == ["repo-slug"]

    @pytest.mark.parametrize(
        "text",
        [
            "Run `kirocrew pod up mypod --provision`.",
            "Scripts must live under `~/.kiro/crew/crons/`.",
            "Set `session.autocompact_pct` in the config.",
            "Call the `monitor_start` MCP tool.",
            # No src/ prefix: this is what an installed wheel actually has, so
            # flagging it would make the remedy impossible to write.
            "See `kiro_crew/dashboard/theme_validate.py`.",
            "Open the dashboard's Browser panel.",
        ],
    )
    def test_the_product_surface_is_not_a_marker(self, gate, text: str) -> None:
        assert gate.marker_hits(text + "\n") == [], text

    def test_each_hit_names_its_class_and_line(self, gate) -> None:
        """A bare match is not actionable -- the remedy differs per class."""
        hits = gate.marker_hits("intro\n`src/kiro_crew/a.py` and `test/test_a.py`\n")
        assert [(line, name) for line, name, _text in hits] == [
            (2, "checkout-path"),
            (2, "test-path"),
        ]

    def test_two_markers_on_one_line_both_report(self, gate) -> None:
        assert len(gate.marker_hits("`src/kiro_crew/a.py` vs `src/kiro_crew/b.py`\n")) == 2


class TestTheExemptionIsNotInheritable:
    def test_the_family_is_exempt(self, gate, tmp_path: Path) -> None:
        """This repository is that family's subject matter, not a leak in it."""
        _write(tmp_path, "kirocrew-dev/prepare-pr/SKILL.md", "Edit `src/kiro_crew/x.py`.\n")
        found, unscannable = gate.scan_tree(tmp_path)
        assert found == {} and unscannable == []

    def test_a_nested_reference_file_is_exempt_too(self, gate, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "kirocrew-dev/prepare-pr/references/gate-floor.md",
            "Run `test/test_prepare_pr_profiles.py`.\n",
        )
        assert gate.scan_tree(tmp_path)[0] == {}

    def test_a_repository_agnostic_family_is_scanned(self, gate, tmp_path: Path) -> None:
        _write(tmp_path, "widgets/SKILL.md", "Edit `src/kiro_crew/x.py`.\n")
        assert list(gate.scan_tree(tmp_path)[0]) == [f"{SKILL_REL}/widgets/SKILL.md"]

    def test_a_nested_family_directory_does_not_inherit_the_exemption(
        self, gate, tmp_path: Path
    ) -> None:
        """Matching any path segment would let an agnostic skill ship a leak by
        putting it in a subdirectory that merely shares the exempt name."""
        _write(tmp_path, "widgets/kirocrew-dev/SKILL.md", "Edit `src/kiro_crew/x.py`.\n")
        assert list(gate.scan_tree(tmp_path)[0]) == [f"{SKILL_REL}/widgets/kirocrew-dev/SKILL.md"]

    def test_a_look_alike_sibling_does_not_inherit_the_exemption(
        self, gate, tmp_path: Path
    ) -> None:
        _write(tmp_path, "kirocrew-devtools/SKILL.md", "Edit `src/kiro_crew/x.py`.\n")
        assert list(gate.scan_tree(tmp_path)[0]) == [f"{SKILL_REL}/kirocrew-devtools/SKILL.md"]

    def test_a_path_outside_the_skill_root_is_never_exempt(self, gate) -> None:
        assert gate.is_exempt(Path("docs/kirocrew-dev/notes.md")) is False


class TestAnUnscannableBodyFails:
    def test_undecodable_markdown_is_reported_not_skipped(self, gate, tmp_path: Path) -> None:
        """A gate that silently passes what it could not decode ships the one
        file nobody scanned."""
        path = tmp_path / SKILL_REL / "widgets" / "binary.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not utf-8 \x80\x81")
        found, unscannable = gate.scan_tree(tmp_path)
        assert found == {}
        assert [rel for rel, _why in unscannable] == [f"{SKILL_REL}/widgets/binary.md"]
        assert "UTF-8" in dict(unscannable)[f"{SKILL_REL}/widgets/binary.md"]

    @requires_symlinks
    def test_a_symlinked_body_is_refused_without_being_read(self, gate, tmp_path: Path) -> None:
        """The rule is "a body is a regular file", which is what makes it safe to
        enforce: deciding per target would mean READING the target to decide, and
        a candidate symlinked to /dev/zero streams nulls until CI kills the job
        (measured out of band -- deliberately not reproduced here, because a test
        that hangs on regression burns the job timeout instead of failing).

        The target is therefore a dangling path: refusal must not depend on the
        target existing, let alone on what it contains."""
        path = tmp_path / SKILL_REL / "widgets" / "linked.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(tmp_path / "nowhere.md")
        found, unscannable = gate.scan_tree(tmp_path)
        assert found == {}
        assert "symlink" in dict(unscannable)[f"{SKILL_REL}/widgets/linked.md"]

    @requires_symlinks
    def test_a_symlink_to_a_clean_body_is_still_refused(self, gate, tmp_path: Path) -> None:
        """The rule is "a body is a regular file", not "a body must not be
        dangerous": deciding per target would mean reading the target to decide."""
        real = tmp_path / SKILL_REL / "widgets" / "SKILL.md"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("Clean prose.\n", encoding="utf-8")
        (tmp_path / SKILL_REL / "widgets" / "alias.md").symlink_to(real)
        _found, unscannable = gate.scan_tree(tmp_path)
        assert [rel for rel, _why in unscannable] == [f"{SKILL_REL}/widgets/alias.md"]

    def test_an_unscannable_file_alone_fails_the_gate(self, gate, capsys) -> None:
        assert gate._report({}, [("a.md", "is a symlink")]) == 1
        out = capsys.readouterr()
        assert "1 unscannable file" in out.err
        assert "is a symlink" in out.out, "the reason must reach the annotation"


class TestAnnotationsCannotBeForged:
    """The runner parses `::` commands on every output line, so an untrusted
    filename reaching an annotation unescaped forges annotations rather than
    merely looking odd. Reachable from a fork: ci.yml runs on `pull_request`, so
    the checkout contains the PR's files."""

    def test_a_newline_in_a_name_cannot_open_a_second_annotation(self, gate, capsys) -> None:
        forged = "widgets/a\n::error::forged.md"
        assert gate._report({forged: [(1, "test-path", "test/test_a.py")]}, []) == 1
        out = capsys.readouterr().out
        assert "%0A" in out
        assert "\n::error::forged" not in out
        # Exactly one annotation line, which is the property under test.
        assert len([ln for ln in out.splitlines() if ln.startswith("::error")]) == 1

    def test_a_colon_or_comma_cannot_split_the_property_list(self, gate, capsys) -> None:
        assert gate._report({}, [("widgets/a:b,c.md", "is a symlink")]) == 1
        line = capsys.readouterr().out.splitlines()[0]
        assert "file=widgets/a%3Ab%2Cc.md::" in line

    def test_a_percent_is_escaped_first(self, gate, capsys) -> None:
        """`%` must go first or the escapes would be double-decoded by the runner."""
        assert gate._report({}, [("widgets/100%.md", "is a symlink")]) == 1
        assert "widgets/100%25.md" in capsys.readouterr().out

    def test_the_marker_text_is_escaped_too(self, gate, capsys) -> None:
        """Marker text comes from file CONTENT, so it is untrusted as well."""
        assert gate._report({"a.md": [(1, "repo-slug", "kirodotdev/KiroCrew%0A")]}, []) == 1
        assert "%250A" in capsys.readouterr().out

    @requires_exotic_filenames
    def test_a_real_newline_named_file_is_scanned_and_annotated_safely(
        self, gate, tmp_path: Path, capsys
    ) -> None:
        """End to end, not just the formatter: git can carry a name containing a
        newline, so the scanner must reach one and still emit one annotation."""
        d = tmp_path / SKILL_REL / "widgets"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a\n::error::forged.md").write_text("Edit `src/kiro_crew/x.py`.\n", encoding="utf-8")
        found, unscannable = gate.scan_tree(tmp_path)
        assert len(found) == 1 and unscannable == []
        assert gate._report(found, unscannable) == 1
        out = capsys.readouterr().out
        assert len([ln for ln in out.splitlines() if ln.startswith("::error")]) == 1


class TestTheGateFailsOnAMarker:
    def test_a_single_marker_fails(self, gate, capsys) -> None:
        current = {"a.md": [(1, "checkout-path", "src/kiro_crew/a.py")]}
        assert gate._report(current, []) == 1
        err = capsys.readouterr().err
        assert "1 marker(s) in 1 file(s)" in err
        assert "prepare-pr/profiles" in err, "the remedy must name the profile pattern"

    def test_a_clean_tree_passes(self, gate) -> None:
        assert gate._report({}, []) == 0


class TestTheCommittedTreeAgrees:
    def test_the_real_tree_is_clean(self, gate) -> None:
        """No baseline exists, so this is the whole ratchet: the shipped tree
        must carry zero markers outside the exempt family."""
        found, unscannable = gate.scan_tree(ROOT)
        assert found == {}, f"markers present: {found}"
        assert unscannable == []

    def test_there_is_no_baseline_to_rot(self, gate) -> None:
        """The shrink-only baseline was deleted deliberately -- ~90 lines of
        machinery to forgive two markers is a worse trade than fixing them, and
        a gate with nothing to forgive cannot drift into forgiving everything."""
        assert not (ROOT / ".github" / "builtin-skill-scope-baseline.txt").exists()
        source = SCRIPT.read_text(encoding="utf-8")
        assert "--update-baseline" not in source

    def test_the_self_test_passes(self, gate, capsys) -> None:
        assert gate.self_test() == 0
        assert "FAIL" not in capsys.readouterr().out

    def test_the_gate_is_wired_into_ci(self) -> None:
        """A scanner nothing runs is documentation. Also pinned in the prepare-pr
        gate floor by test_prepare_pr_profiles.py.

        The gate lives in fast-gate.yml, not ci.yml: the eleven cheap blocking
        gates were split into their own workflow so ci.yml's heavy matrix can
        wait on their verdict instead of racing it. ci.yml still BLOCKS on this
        gate transitively, through its ``await-fast-gate`` job, so the wiring
        assertion has to read the file that now carries the invocation.
        """
        gate = (ROOT / ".github" / "workflows" / "fast-gate.yml").read_text(encoding="utf-8")
        assert "python3 scripts/check_builtin_skill_scope.py --test" in gate
        assert "python3 scripts/check_builtin_skill_scope.py\n" in gate

    def test_the_gate_is_unconditional(self) -> None:
        """Both halves of the wiring: the invocation above, and the job carrying
        it running on every event the workflow fires on. A ``needs:`` would let a
        failed sibling skip it, and an ``if:`` (a path filter's surface output,
        say) would let a diff shape dodge it -- either one turns the absolute,
        baseline-free rule into one that only sometimes runs.
        """
        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "fast-gate.yml").read_text(encoding="utf-8")
        )
        job = workflow["jobs"]["builtin-skill-scope"]
        assert "needs" not in job, "builtin-skill-scope gained a dependency and can now be skipped"
        assert "if" not in job, "builtin-skill-scope gained a condition and can now be dodged"
