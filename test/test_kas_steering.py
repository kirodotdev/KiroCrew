"""Workspace steering, inheritance and ``use_aws`` on a KAS session (``acp/kas_steering.py``)."""

from __future__ import annotations

import os

import pytest

from kiro_crew.acp import kas_steering
from kiro_crew.acp.kas_spec_carry import use_aws_notice
from kiro_crew.acp.kas_steering import kas_steering_resources, with_kas_steering
from kiro_crew.testing.links import make_dir_link

GLOB = "file://.kiro/steering/**/*.md"
STEERING = "file://.kiro/steering"


def _write(root, name, front=None):
    path = root / ".kiro" / "steering" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((f"---\n{front}\n---\n" if front else "") + "body\n")
    return path


@pytest.fixture
def covered(tmp_path):
    """A tree KAS's native load covers whole: no auto file, no link."""
    _write(tmp_path, "always.md")
    _write(tmp_path, "manual.md", "inclusion: manual")
    _write(tmp_path, "sub/match.md", "inclusion: fileMatch\nfileMatchPattern: '*.py'")
    return tmp_path


class TestSteeringResources:
    def test_the_glob_is_dropped_when_native_load_covers_the_tree(self, covered):
        out = kas_steering_resources([GLOB, "file://AGENTS.md"], str(covered))
        assert out == ["file://AGENTS.md"]

    @pytest.mark.parametrize(
        "front",
        ["inclusion: auto", "inclusion: auto\ndescription: on demand", '{"inclusion":"AUTO"}'],
    )
    def test_a_file_that_may_be_auto_is_sent_by_its_own_path(self, covered, front):
        _write(covered, "sub/maybe.md", front)
        out = kas_steering_resources(["skill://x", GLOB, "file://AGENTS.md"], str(covered))
        assert out == ["skill://x", f"{STEERING}/sub/maybe.md", "file://AGENTS.md"]

    @pytest.mark.parametrize("front", ['inclusion: "\\x61uto"', '{"\\u0069nclusion":"\\u0061uto"}'])
    def test_an_escaped_auto_is_sent_by_its_own_path(self, covered, front):
        # KAS's YAML and JSON readers decode these to `inclusion: auto`; with no
        # description KAS skips the file, so only Crew's entry still loads it.
        _write(covered, "sub/escaped.md", front)
        assert kas_steering_resources([GLOB], str(covered)) == [f"{STEERING}/sub/escaped.md"]

    def test_a_dot_named_auto_file_is_left_to_the_native_load(self, covered):
        # The entry's glob never matched a dot-named part, so it never sent these.
        _write(covered, ".hidden.md", "inclusion: auto")
        _write(covered, ".drafts/a.md", "inclusion: auto")
        assert kas_steering_resources([GLOB], str(covered)) == []

    @pytest.mark.parametrize(
        "resources",
        [
            ["file://AGENTS.md", GLOB],
            [GLOB, "file://.kiro/steering/sub/*.md"],
            ["file://./.kiro/steering/**/*.md"],
        ],
    )
    def test_another_file_entry_before_or_beside_keeps_every_entry(self, covered, resources):
        # A file read before the glob, or another steering entry, takes KAS's
        # resource budget in an order Crew does not see.
        _write(covered, "sub/maybe.md", "inclusion: auto")
        assert kas_steering_resources(resources, str(covered)) == resources

    @pytest.mark.skipif(os.name == "nt", reason="'*' and '?' cannot be in a Windows file name")
    def test_a_glob_character_in_a_name_keeps_the_entry(self, covered):
        _write(covered, "what?.md", "inclusion: auto")
        assert kas_steering_resources([GLOB], str(covered)) == [GLOB]

    def test_files_past_the_resource_budget_keep_the_entry(self, covered):
        for i in range(kas_steering._MAX_RESOURCE_FILES + 1):
            _write(covered, f"auto/{i}.md", "inclusion: auto")
        assert kas_steering_resources([GLOB], str(covered)) == [GLOB]

    def test_characters_past_the_resource_budget_keep_the_entry(self, covered):
        big = "x" * (kas_steering._MAX_RESOURCE_CHARS // 2)
        _write(covered, "a.md", f"inclusion: auto\n# {big}")
        _write(covered, "b.md", f"inclusion: auto\n# {big}")
        assert kas_steering_resources([GLOB], str(covered)) == [GLOB]

    def test_a_backslash_in_the_body_only_still_drops_the_entry(self, covered):
        (covered / ".kiro" / "steering" / "code.md").write_text(
            "---\ninclusion: manual\n---\na\\b\n"
        )
        assert kas_steering_resources([GLOB], str(covered)) == []

    def test_a_file_link_keeps_the_entry(self, covered, tmp_path_factory):
        target = tmp_path_factory.mktemp("outside") / "x.md"
        target.write_text("body\n")
        (covered / ".kiro" / "steering" / "link.md").symlink_to(target)
        assert kas_steering_resources([GLOB], str(covered)) == [GLOB]

    def test_a_folder_link_keeps_the_entry(self, covered, tmp_path_factory):
        make_dir_link(covered / ".kiro" / "steering" / "link", tmp_path_factory.mktemp("outside"))
        assert kas_steering_resources([GLOB], str(covered)) == [GLOB]

    def test_a_linked_kiro_folder_keeps_the_entry(self, tmp_path, tmp_path_factory):
        real = tmp_path_factory.mktemp("real-kiro")
        (real / "steering").mkdir()
        (real / "steering" / "a.md").write_text("body\n")
        make_dir_link(tmp_path / ".kiro", real)
        assert kas_steering_resources([GLOB], str(tmp_path)) == [GLOB]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes")
    def test_an_unsearchable_folder_keeps_the_entry(self, covered):
        sub = covered / ".kiro" / "steering" / "sub"
        sub.chmod(0o644)
        try:
            if os.access(sub, os.X_OK):
                pytest.skip("this user ignores permission modes")
            assert kas_steering_resources([GLOB], str(covered)) == [GLOB]
        finally:
            sub.chmod(0o755)

    def test_an_upper_case_md_file_keeps_the_entry(self, covered):
        (covered / ".kiro" / "steering" / "Notes.MD").write_text("body\n")
        assert kas_steering_resources([GLOB], str(covered)) == [GLOB]

    def test_an_oversized_file_keeps_the_entry(self, covered):
        (covered / ".kiro" / "steering" / "big.md").write_bytes(b"x" * (1024 * 1024 + 1))
        assert kas_steering_resources([GLOB], str(covered)) == [GLOB]

    def test_no_tree_or_no_work_dir_keeps_the_entry(self, tmp_path):
        assert kas_steering_resources([GLOB], str(tmp_path)) == [GLOB]
        assert kas_steering_resources([GLOB], None) == [GLOB]

    @pytest.mark.parametrize(
        "entry",
        [
            "file://AGENTS.md",
            "file://~/.kiro/steering/**/*.md",
            "file:///abs/.kiro/steering/x.md",
            "file://.kiro/steering/../../secret.md",
            "file://.kiro/steering/context.txt",
            "file://.kiro/steering/**/*",
            "file://.kiro/steering/{a,b}.md",
            "skill://.kiro/skills/**/SKILL.md",
        ],
    )
    def test_entries_native_load_may_not_cover_pass_unchanged(self, covered, entry):
        assert kas_steering_resources([entry], str(covered)) == [entry]

    def test_dot_slash_spelling_and_non_strings(self, covered):
        kb = {"type": "knowledgeBase", "source": "file://docs"}
        assert kas_steering_resources(["file://./.kiro/steering/a.md", kb], covered) == [kb]
        assert kas_steering_resources("x", covered) == "x"

    def test_spec_copy_is_returned_and_input_kept(self, covered):
        spec = {"name": "a", "resources": [GLOB]}
        assert with_kas_steering(spec, covered) == {"name": "a", "resources": []}
        assert spec["resources"] == [GLOB]
        bare = {"name": "a"}
        assert with_kas_steering(bare, covered) is bare


class TestNotices:
    def test_inheritance_off_names_what_still_loads(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers import acp

        monkeypatch.setattr(acp, "inherits_default_resources", lambda work_dir: False)
        notice = kas_steering.inheritance_off_notice("/w")
        assert "AGENTS.md and .kiro/steering" in notice
        # Whether skills are held back depends on the session's scope; a
        # whole-catalog agent keeps them, so the line must not claim either way.
        assert "skill" not in notice
        monkeypatch.setattr(acp, "inherits_default_resources", lambda work_dir: True)
        assert kas_steering.inheritance_off_notice("/w") == ""

    @pytest.mark.parametrize("key", ["tools", "allowedTools"])
    @pytest.mark.parametrize("name", ["use_aws", "aws", "@builtin/use_aws"])
    def test_use_aws_is_named(self, key, name):
        assert "KAS has no AWS tool" in use_aws_notice("a1", {key: [name]})

    @pytest.mark.parametrize("spec", [{}, {"tools": ["*"]}, {"tools": "use_aws"}])
    def test_no_use_aws_no_notice(self, spec):
        assert use_aws_notice("a1", spec) == ""
