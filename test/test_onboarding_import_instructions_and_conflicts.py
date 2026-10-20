"""How import lands instructions as lessons and resolves a destination collision.

``TestInstructionImport`` and the identity, capacity and vector-store tests of
``TestReviewFindings`` pin that directives land in the durable lesson tier
(``lessons.jsonl`` or an available vector store) without evicting or deleting the user's own
lessons, while persona identity lines and credential-bearing text stay out.
``TestConflictStrategies`` and the overwrite tests of ``TestReviewFindings`` pin ``skip``,
``rename`` and ``overwrite``: an overwrite writes a restore copy first, a failed one leaves
the original intact, each destination keeps one ledger record, and a retired tree is never
deleted. ``TestReviewFindings`` also pins the deferred embedding sweep. The preview classes
live in ``test_onboarding_import.py`` and
``test_onboarding_import_preview_predecessor_claude_hermes.py``; per-category writes, skill
re-import included, in ``test_onboarding_import_apply_categories.py``; merge safety and the
parsing floors in ``test_onboarding_import_safety_floors.py``.
"""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest
from onboarding_import_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _categories,
    _register_predecessor_source,
)

from kiro_crew import onboarding_apply, onboarding_plan
from kiro_crew.vector_memory import VectorMemoryStore

# Keep apply_import's MCP sidecar lock off the real ~/.kiro/settings (see the
# shared fixture in test/conftest.py).
pytestmark = pytest.mark.usefixtures("isolate_mcp_host_paths")


def _api() -> ModuleType:
    try:
        return importlib.import_module("kiro_crew.onboarding_import")
    except ModuleNotFoundError:
        pytest.fail("kiro_crew.onboarding_import is not implemented")


class TestInstructionImport:
    """Instructions land in the durable memory tiers, never in prefs/projects.

    See docs/system-specs/modules/onboarding-import.md → "Destination mapping".
    """

    def test_soul_directives_import_but_persona_role_does_not(self, tmp_path: Path) -> None:
        api = _api()
        home = tmp_path / "home"
        hermes = home / ".hermes"
        hermes.mkdir(parents=True)
        (hermes / "SOUL.md").write_text(
            "# Identity\n\nYou are Aria, a laconic assistant.\n\n"
            "Always cite a file path when you reference code.\n",
            encoding="utf-8",
        )

        plan = api.preview_import(home=home, env={})
        destination = tmp_path / "destination"
        result = api.apply_import(plan, data_home=destination)

        assert result["imported"]["instructions"] >= 1
        lessons = (destination / "lessons.jsonl").read_text(encoding="utf-8")
        assert "Always cite a file path" in lessons
        # The DIRECTIVE text is imported as a lesson; the persona ROLE is not —
        # nothing is written to a persona/system-prompt surface. That surface is
        # theme-pack persona, gated by capabilities.theme_persona.
        for forbidden in ("persona.md", "prompt.md", "SOUL.md"):
            assert not (destination / forbidden).exists()

    def test_instruction_import_cannot_evict_the_users_own_lessons(self, tmp_path: Path) -> None:
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        # Far more directives than import is allowed to contribute.
        paragraphs = "\n\n".join(
            f"Directive number {index} that is long enough to be kept." for index in range(200)
        )
        (codex / "AGENTS.md").write_text(paragraphs + "\n", encoding="utf-8")

        plan = api.preview_import(home=home, env={})
        count = _categories(plan, "codex")["instructions"]

        # LessonStore prunes OLDEST-first at 200, so an unbounded import would
        # silently evict the user's own accumulated corrections.
        assert count == onboarding_plan._MAX_IMPORTED_LESSONS
        assert any(
            item["category_id"] == "instructions" and item["reason"] == "instruction_count_limit"
            for item in plan["skipped"]
        )

    def test_credential_bearing_instruction_is_dropped(self, tmp_path: Path) -> None:
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "AGENTS.md").write_text(
            "Deploy with the key AKIAIOSFODNN7EXAMPLE when releasing.\n",
            encoding="utf-8",
        )

        plan = api.preview_import(home=home, env={})

        assert "instructions" not in _categories(plan, "codex")
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(plan)
        assert any(
            item["category_id"] == "instructions"
            and item["reason"] == "credential_bearing_instruction"
            for item in plan["skipped"]
        )

    def test_reimport_of_the_same_directive_is_idempotent(self, tmp_path: Path) -> None:
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "AGENTS.md").write_text("Prefer stdlib over new deps.\n", encoding="utf-8")
        destination = tmp_path / "destination"

        first = api.apply_import(api.preview_import(home=home, env={}), data_home=destination)
        second = api.apply_import(api.preview_import(home=home, env={}), data_home=destination)

        assert first["imported"]["instructions"] == 1
        assert second["imported"]["instructions"] == 0
        assert second["already_imported"] >= 1
        lines = [
            line
            for line in (destination / "lessons.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1


def _skill_source(home: Path, source_dir: str, name: str, body: str) -> None:
    skill = home / source_dir / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(body, encoding="utf-8")


class TestConflictStrategies:
    """A destination collision is a user decision, not a terminal failure.

    See docs/system-specs/modules/onboarding-import.md -> "Conflict strategy".
    ``skip`` is the default; ``rename`` and ``overwrite`` require an explicit
    choice, and ``overwrite`` always writes a restore copy first.
    """

    def _import_skill(self, tmp_path: Path, body: str, **kwargs: object) -> dict:
        api = _api()
        home = tmp_path / "home"
        _skill_source(home, ".codex", "review", body)
        plan = api.preview_import(home=home, env={})
        return api.apply_import(plan, data_home=tmp_path / "destination", **kwargs)

    def _installed(self, destination: Path) -> dict[str, str]:
        root = destination / "skills" / "imported"
        return {
            path.parent.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
            for path in root.rglob("SKILL.md")
        }

    def test_skip_is_the_default_and_preserves_the_existing_item(self, tmp_path: Path) -> None:
        destination = tmp_path / "destination"
        first = self._import_skill(tmp_path, "# Original\n")
        assert first["imported"]["skills"] == 1
        assert first["conflict_strategy"] == "skip"

        # Same name, different content: an upstream edit.
        second = self._import_skill(tmp_path, "# Edited upstream\n")

        assert second["imported"]["skills"] == 0
        assert second["conflicts"] == [
            {
                "source_id": "codex",
                "category_id": "skills",
                "reason": "destination_conflict",
                "resolvable": True,
            }
        ]
        # Kiro Crew's copy is untouched.
        assert self._installed(destination) == {"codex/review": "# Original\n"}

    def test_rename_installs_alongside_and_reports_the_new_name(self, tmp_path: Path) -> None:
        destination = tmp_path / "destination"
        self._import_skill(tmp_path, "# Original\n")

        result = self._import_skill(tmp_path, "# Edited upstream\n", conflict_strategy="rename")

        assert result["imported"]["skills"] == 1
        assert result["conflicts"] == []
        assert self._installed(destination) == {
            "codex/review": "# Original\n",
            "codex/review-codex": "# Edited upstream\n",
        }
        renamed = [
            entry for entry in result["item_outcomes"] if entry.get("renamed_to") == "review-codex"
        ]
        assert len(renamed) == 1
        assert renamed[0]["outcome"] == "accepted"

    def test_overwrite_replaces_only_after_writing_a_restore_copy(self, tmp_path: Path) -> None:
        destination = tmp_path / "destination"
        self._import_skill(tmp_path, "# Original\n")

        result = self._import_skill(tmp_path, "# Edited upstream\n", conflict_strategy="overwrite")

        assert result["imported"]["skills"] == 1
        assert self._installed(destination) == {"codex/review": "# Edited upstream\n"}
        restored = [
            entry["restored_to"] for entry in result["item_outcomes"] if entry.get("restored_to")
        ]
        assert len(restored) == 1
        # The replaced bytes are recoverable, under a per-run stamped dir.
        preserved = Path(restored[0]) / "SKILL.md"
        assert preserved.read_text(encoding="utf-8") == "# Original\n"
        assert (destination / "imports" / "replaced") in preserved.parents

    def test_overwrite_refuses_when_the_restore_copy_cannot_be_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / "destination"
        self._import_skill(tmp_path, "# Original\n")

        def fail_copytree(*_args: object, **_kwargs: object) -> None:
            raise OSError("no space left on device")

        monkeypatch.setattr(shutil, "copytree", fail_copytree)

        result = self._import_skill(tmp_path, "# Edited upstream\n", conflict_strategy="overwrite")

        # An unrecoverable replace is worse than a reported conflict.
        assert result["imported"]["skills"] == 0
        assert result["conflicts"][0]["reason"] == "destination_conflict"
        assert self._installed(destination) == {"codex/review": "# Original\n"}

    def test_unchanged_reimport_is_still_deduplicated_under_every_strategy(
        self, tmp_path: Path
    ) -> None:
        for strategy in ("skip", "rename", "overwrite"):
            root = tmp_path / strategy
            self._import_skill_at(root, "# Same\n")
            again = self._import_skill_at(root, "# Same\n", conflict_strategy=strategy)

            assert again["imported"]["skills"] == 0, strategy
            assert again["conflicts"] == [], strategy
            # No rename, no restore copy: nothing collided.
            assert not (root / "destination" / "imports" / "replaced").exists(), strategy
            assert self._installed(root / "destination") == {"codex/review": "# Same\n"}, strategy

    def _import_skill_at(self, root: Path, body: str, **kwargs: object) -> dict:
        api = _api()
        home = root / "home"
        _skill_source(home, ".codex", "review", body)
        plan = api.preview_import(home=home, env={})
        return api.apply_import(plan, data_home=root / "destination", **kwargs)

    def test_unknown_strategy_falls_back_to_skip_in_the_backend(self, tmp_path: Path) -> None:
        # The API rejects an unknown strategy with a 400; the backend is the
        # second line of defence for a non-HTTP caller and must fail SAFE.
        destination = tmp_path / "destination"
        self._import_skill(tmp_path, "# Original\n")

        result = self._import_skill(tmp_path, "# Edited upstream\n", conflict_strategy="obliterate")

        assert result["conflict_strategy"] == "skip"
        assert self._installed(destination) == {"codex/review": "# Original\n"}

    def test_mcp_rename_avoids_shadowing_another_sources_server(self, tmp_path: Path) -> None:
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text(
            '[mcp_servers.helper]\ncommand = "codex-helper"\n', encoding="utf-8"
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        # A server the user already has under that name, with a different spec.
        (destination / "mcp.json").write_text(
            json.dumps({"mcpServers": {"helper": {"command": "mine"}}}), encoding="utf-8"
        )

        plan = api.preview_import(home=home, env={})
        result = api.apply_import(plan, data_home=destination, conflict_strategy="rename")

        servers = json.loads((destination / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        assert result["imported"]["mcp_servers"] == 1
        # The user's own entry is untouched; the import lands beside it.
        assert servers["helper"] == {"command": "mine"}
        # Imported servers always land disabled.
        assert servers["helper-codex"] == {"command": "codex-helper", "disabled": True}

    def test_mcp_overwrite_preserves_the_replaced_spec(self, tmp_path: Path) -> None:
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text(
            '[mcp_servers.helper]\ncommand = "codex-helper"\n', encoding="utf-8"
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        (destination / "mcp.json").write_text(
            json.dumps({"mcpServers": {"helper": {"command": "mine"}}}), encoding="utf-8"
        )

        plan = api.preview_import(home=home, env={})
        result = api.apply_import(plan, data_home=destination, conflict_strategy="overwrite")

        servers = json.loads((destination / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        assert servers["helper"] == {"command": "codex-helper", "disabled": True}
        restored = next(
            entry["restored_to"] for entry in result["item_outcomes"] if entry.get("restored_to")
        )
        assert json.loads(Path(restored).read_text(encoding="utf-8")) == {
            "helper": {"command": "mine"}
        }

    def test_workspace_name_collision_needs_rename_not_silent_suffixing(
        self, tmp_path: Path
    ) -> None:
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        project = tmp_path / "shared" / "demo"
        project.mkdir(parents=True)
        (codex / "config.toml").write_text(
            f'[projects."{str(project).replace(chr(92), chr(92) * 2)}"]\n'
            'trust_level = "trusted"\n',
            encoding="utf-8",
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        other = tmp_path / "other" / "demo"
        other.mkdir(parents=True)
        # The name "demo" is taken by a DIFFERENT directory.
        (destination / "config.json").write_text(
            json.dumps({"workspaces": {"demo": {"dir": str(other)}}}), encoding="utf-8"
        )

        plan = api.preview_import(home=home, env={})
        skipped = api.apply_import(plan, data_home=destination)

        assert skipped["imported"]["workspaces"] == 0
        assert skipped["conflicts"][0]["category_id"] == "workspaces"
        config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
        assert config["workspaces"] == {"demo": {"dir": str(other)}}

        renamed = api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="rename",
        )
        config = json.loads((destination / "config.json").read_text(encoding="utf-8"))

        assert renamed["imported"]["workspaces"] == 1
        assert config["workspaces"]["demo"] == {"dir": str(other)}
        assert config["workspaces"]["demo-codex"] == {"dir": str(project.resolve())}


class TestReviewFindings:
    """Import guards: a lesson is not evicted near capacity, and related edge cases."""

    def test_import_never_evicts_a_lesson_when_the_store_is_near_capacity(
        self, tmp_path: Path
    ) -> None:
        """A per-import cap alone is not enough.

        LessonStore prunes OLDEST-first past its own ceiling, and the user's own
        corrections are the oldest rows. 151 existing + 50 imported would still
        delete one, so the writer refuses past the store's REMAINING capacity.
        """
        api = _api()
        from kiro_crew.learn import _MAX_LESSONS_TOTAL, Lesson, LessonStore

        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "AGENTS.md").write_text(
            "\n\n".join(f"Imported directive {i} long enough to keep." for i in range(60)),
            encoding="utf-8",
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        store = LessonStore(base_dir=destination)
        for index in range(_MAX_LESSONS_TOTAL - 5):
            store.save(
                Lesson(
                    ts="2026-01-01T00:00:00+00:00", rule=f"user rule {index}", category="preference"
                )
            )
        oldest = store.load_all()[0].rule

        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)

        after = LessonStore(base_dir=destination).load_all()
        assert len(after) <= _MAX_LESSONS_TOTAL
        # The user's oldest lesson survived.
        assert any(lesson.rule == oldest for lesson in after)
        assert sum(1 for lesson in after if lesson.rule.startswith("user rule")) == (
            _MAX_LESSONS_TOTAL - 5
        )

    def test_failed_overwrite_leaves_the_original_skill_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed replace must be a no-op, never a half-deleted skill.

        Deleting in place before installing meant a partial delete (a locked
        file on Windows) left the user with neither version.
        """
        api = _api()
        home = tmp_path / "home"
        skill = home / ".codex" / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Original\n", encoding="utf-8")
        destination = tmp_path / "destination"
        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)
        (skill / "SKILL.md").write_text("# Edited upstream\n", encoding="utf-8")

        # The install step fails AFTER the original has been moved aside.
        monkeypatch.setattr(onboarding_apply, "_install_skill_tree", lambda *_a, **_k: "rejected")
        result = api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="overwrite",
        )

        assert result["imported"]["skills"] == 0
        installed = destination / "skills" / "imported" / "codex" / "review" / "SKILL.md"
        assert installed.read_text(encoding="utf-8") == "# Original\n"
        # No half-retired debris left behind under the skills tree.
        assert not any(
            path.name.startswith(".review.replaced-")
            for path in (destination / "skills" / "imported" / "codex").iterdir()
        )

    def test_persona_identity_paragraphs_are_not_imported_as_lessons(self, tmp_path: Path) -> None:
        """Only a persona document's DIRECTIVES are in scope, not its identity.

        Importing "You are Aria" into an always-injected lesson would make
        foreign text act as the agent's persona through a path that bypasses
        capabilities.theme_persona.
        """
        api = _api()
        home = tmp_path / "home"
        hermes = home / ".hermes"
        hermes.mkdir(parents=True)
        (hermes / "SOUL.md").write_text(
            "You are Aria, a laconic senior engineer.\n\n"
            "Your persona is terse and never uses exclamation marks.\n\n"
            "I am a helpful assistant that always double-checks.\n\n"
            "Always cite a file path when you reference code.\n\n"
            "Always tell the user when you skip a test.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"

        plan = api.preview_import(home=home, env={})
        api.apply_import(plan, data_home=destination)

        lessons = (destination / "lessons.jsonl").read_text(encoding="utf-8")
        # Directives import -- including one that merely MENTIONS "you".
        assert "Always cite a file path" in lessons
        assert "Always tell the user when you skip a test" in lessons
        # Identity statements do not.
        assert "You are Aria" not in lessons
        assert "Your persona is terse" not in lessons
        assert "I am a helpful assistant" not in lessons
        assert any(
            item["category_id"] == "instructions" and item["reason"] == "persona_identity_excluded"
            for item in plan["skipped"]
        )

    def test_persona_identity_under_a_heading_is_still_excluded(self, tmp_path: Path) -> None:
        """A heading above the identity line must not bypass the gate.

        Anchoring on the raw paragraph tested "# Persona", not the identity line
        beneath it, so `# Persona\\nYou are Aria` leaked into always-injected
        lessons.
        """
        api = _api()
        home = tmp_path / "home"
        hermes = home / ".hermes"
        hermes.mkdir(parents=True)
        (hermes / "SOUL.md").write_text(
            "# Persona\nYou are Aria, a laconic senior engineer.\n\n"
            "## Voice\nYour persona never uses exclamation marks.\n\n"
            "# Rules\nAlways cite a file path when you reference code.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"

        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)

        lessons = (destination / "lessons.jsonl").read_text(encoding="utf-8")
        assert "Always cite a file path" in lessons
        assert "You are Aria" not in lessons
        assert "Your persona never uses" not in lessons

    def test_two_sources_overwriting_the_same_mcp_name_keep_distinct_restores(
        self, tmp_path: Path
    ) -> None:
        """Restore copies must not collide across sources.

        A bare-name restore file let the second source's overwrite replace the
        user's only copy of the original spec.
        """
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text(
            '[mcp_servers.helper]\ncommand = "codex-helper"\n', encoding="utf-8"
        )
        predecessor = home / ".predecessor"
        predecessor.mkdir(parents=True)
        (predecessor / "mcp.json").write_text(
            json.dumps({"mcpServers": {"helper": {"command": "predecessor-helper"}}}),
            encoding="utf-8",
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        (destination / "mcp.json").write_text(
            json.dumps({"mcpServers": {"helper": {"command": "the-users-own"}}}),
            encoding="utf-8",
        )

        result = api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="overwrite",
        )

        restores = sorted(
            entry["restored_to"] for entry in result["item_outcomes"] if entry.get("restored_to")
        )
        # Two overwrites -> two DISTINCT restore files, not one clobbered file.
        assert len(restores) == 2
        assert len(set(restores)) == 2
        specs = [json.loads(Path(path).read_text(encoding="utf-8")) for path in restores]
        # The user's original spec is recoverable from at least one of them.
        assert any(spec == {"helper": {"command": "the-users-own"}} for spec in specs)

    def test_overwrite_restores_the_original_when_installation_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception during install must restore too, not just a failed return.

        Handling only the non-"imported" return left the original stranded under
        its retired name with nothing installed at the real path.
        """
        api = _api()
        home = tmp_path / "home"
        skill = home / ".codex" / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Original\n", encoding="utf-8")
        destination = tmp_path / "destination"
        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)
        (skill / "SKILL.md").write_text("# Edited upstream\n", encoding="utf-8")

        def boom(*_a: object, **_k: object) -> str:
            raise OSError("no space left on device")

        monkeypatch.setattr(onboarding_apply, "_install_skill_tree", boom)
        result = api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="overwrite",
        )

        assert result["imported"]["skills"] == 0
        installed = destination / "skills" / "imported" / "codex" / "review" / "SKILL.md"
        assert installed.read_text(encoding="utf-8") == "# Original\n"
        assert not any(
            path.name.startswith(".review.replaced-")
            for path in (destination / "skills" / "imported" / "codex").iterdir()
        )

    @pytest.mark.parametrize(
        "identity",
        [
            "Act as Aria, a laconic senior engineer.",
            "Roleplay as a terse code reviewer.",
            "Pretend to be Aria when answering.",
            "Assume the role of a security reviewer.",
            "Adopt the voice of a patient mentor.",
        ],
        ids=["act-as", "roleplay", "pretend", "assume-role", "adopt-voice"],
    )
    def test_subjectless_identity_imperatives_are_excluded(
        self, tmp_path: Path, identity: str
    ) -> None:
        """A persona doc often writes identity as a COMMAND, not a statement.

        "Act as Aria" reads as a directive, so nothing but the identity guard
        would catch it — and it would then act as the persona from an
        always-injected lesson, bypassing capabilities.theme_persona.
        """
        api = _api()
        home = tmp_path / "home"
        hermes = home / ".hermes"
        hermes.mkdir(parents=True)
        (hermes / "SOUL.md").write_text(
            f"{identity}\n\nAlways cite a file path when you reference code.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"

        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)

        lessons = (destination / "lessons.jsonl").read_text(encoding="utf-8")
        assert "Always cite a file path" in lessons
        assert identity not in lessons

    def test_directives_starting_with_an_identity_verb_still_import(self, tmp_path: Path) -> None:
        """The identity guard must not swallow ordinary directives.

        "Act on review feedback" and "Respond as soon as CI finishes" open with
        the same verbs but are instructions, not identity.
        """
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "AGENTS.md").write_text(
            "Act on review feedback before pushing again.\n\n"
            "Always tell the user when you skip a test.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"

        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)

        lessons = (destination / "lessons.jsonl").read_text(encoding="utf-8")
        assert "Act on review feedback" in lessons
        assert "Always tell the user when you skip a test" in lessons

    def test_mcp_overwrite_keeps_one_ledger_record_per_destination_name(
        self, tmp_path: Path
    ) -> None:
        """Two sources overwriting one MCP name must leave ONE live record.

        Otherwise the loser's stale fingerprint keeps deduplicating: when the
        winner's definition later changes it re-imports under a new fingerprint,
        while the other source's selected definition silently never lands.
        """
        api = _api()
        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text(
            '[mcp_servers.helper]\ncommand = "codex-helper"\n', encoding="utf-8"
        )
        predecessor = home / ".predecessor"
        predecessor.mkdir(parents=True)
        (predecessor / "mcp.json").write_text(
            json.dumps({"mcpServers": {"helper": {"command": "predecessor-helper"}}}),
            encoding="utf-8",
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        (destination / "mcp.json").write_text(
            json.dumps({"mcpServers": {"helper": {"command": "the-users-own"}}}),
            encoding="utf-8",
        )

        api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="overwrite",
        )

        ledger = json.loads(
            (destination / onboarding_apply._LEDGER_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        helper_records = [
            record
            for record in ledger["records"].values()
            if record.get("destination_key") == "helper"
        ]
        assert len(helper_records) == 1

    def test_identity_on_any_line_of_a_paragraph_excludes_it(self, tmp_path: Path) -> None:
        """One identity line taints the whole paragraph.

        A paragraph is imported WHOLE, so checking only the first content line
        let "Always cite paths.\\nYou are Aria." through with the identity
        attached. A multi-line paragraph of pure directives must still import.
        """
        api = _api()
        home = tmp_path / "home"
        hermes = home / ".hermes"
        hermes.mkdir(parents=True)
        (hermes / "SOUL.md").write_text(
            "Always cite paths.\nYou are Aria, a laconic engineer.\n\n"
            "# Persona\nAct as Aria at all times.\n\n"
            "Never force-push a shared branch.\nAlways squash before a PR.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"

        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)

        lessons = (destination / "lessons.jsonl").read_text(encoding="utf-8")
        # Both identity-tainted paragraphs are dropped ENTIRELY -- including the
        # directive that shared a paragraph with the identity line.
        assert "You are Aria" not in lessons
        assert "Act as Aria" not in lessons
        assert "Always cite paths" not in lessons
        # A multi-line paragraph of pure directives still imports.
        assert "Never force-push a shared branch" in lessons
        assert "Always squash before a PR" in lessons

    def test_bulleted_and_quoted_identity_lines_are_excluded(self, tmp_path: Path) -> None:
        """Markdown markers must not shield an identity line.

        A persona doc routinely bullets or blockquotes its identity, and an
        anchored match would test the marker rather than the sentence.
        """
        api = _api()
        home = tmp_path / "home"
        hermes = home / ".hermes"
        hermes.mkdir(parents=True)
        (hermes / "SOUL.md").write_text(
            "- You are Aria, a laconic engineer.\n\n"
            "> Act as Aria at all times.\n\n"
            "1. Assume the role of a reviewer.\n\n"
            "- Always cite a file path when you reference code.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"

        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)

        lessons = (destination / "lessons.jsonl").read_text(encoding="utf-8")
        assert "You are Aria" not in lessons
        assert "Act as Aria" not in lessons
        assert "Assume the role" not in lessons
        # A BULLETED directive still imports -- stripping the marker must not
        # make every list item look like identity.
        assert "Always cite a file path" in lessons

    def test_instructions_route_to_the_vector_store_when_it_owns_lessons(
        self, tmp_path: Path
    ) -> None:
        """ContextBuilder ignores lessons.jsonl once vector lessons exist.

        Writing only the JSONL there would record the item as imported while the
        agent never sees it (context.py reads `vector_store.get_lessons()` first
        and never falls back when it is non-empty).
        """
        api = _api()
        from kiro_crew.vector_memory import VectorMemoryStore

        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "AGENTS.md").write_text(
            "Always squash before opening a pull request.\n", encoding="utf-8"
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        store = VectorMemoryStore(db_path=destination / "memory.db")
        store.init()
        # A pre-existing vector lesson makes the vector store authoritative.
        store.write_lesson("Never force-push a shared branch.", category="preference")
        try:
            result = api.apply_import(
                api.preview_import(home=home, env={}),
                data_home=destination,
                vector_store=store,
            )
            rules = " ".join(str(lesson.get("value_json", "")) for lesson in store.get_lessons())
        finally:
            store.close()

        assert result["imported"]["instructions"] == 1
        # The imported directive is where the agent will actually read it.
        assert "Always squash before opening a pull request" in rules

    def test_vector_import_never_deletes_a_user_authored_lesson(self, tmp_path: Path) -> None:
        """Import is merge-only, including into the vector store.

        ``write_lesson`` deletes on exact-substring OR >50% topic overlap
        ("newer replaces older"), so routing an import through it let a foreign
        directive delete a correction the USER taught the agent.
        """
        api = _api()
        from kiro_crew.vector_memory import VectorMemoryStore

        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        # Heavily overlapping with the user's own lesson below.
        (codex / "AGENTS.md").write_text(
            "Always squash commits before opening a pull request.\n", encoding="utf-8"
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        store = VectorMemoryStore(db_path=destination / "memory.db")
        store.init()
        store.write_lesson(
            "Always squash commits before opening a pull request upstream.",
            category="preference",
        )
        before = len(store.get_lessons())
        try:
            api.apply_import(
                api.preview_import(home=home, env={}),
                data_home=destination,
                vector_store=store,
            )
            after = store.get_lessons()
        finally:
            store.close()

        # The user's lesson is still there; nothing was replaced.
        assert len(after) >= before
        rules = " ".join(str(lesson.get("value_json", "")) for lesson in after)
        assert "opening a pull request upstream" in rules

    def test_skill_overwrite_keeps_one_ledger_record_per_destination(self, tmp_path: Path) -> None:
        """Import V1 -> overwrite V2 -> revert to V1 must reinstall V1.

        Without a stable destination key, V1's stale fingerprint deduplicated the
        revert and left V2 installed.
        """
        api = _api()
        home = tmp_path / "home"
        skill = home / ".codex" / "skills" / "review"
        skill.mkdir(parents=True)
        destination = tmp_path / "destination"
        installed = destination / "skills" / "imported" / "codex" / "review" / "SKILL.md"

        (skill / "SKILL.md").write_text("# V1\n", encoding="utf-8")
        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)
        (skill / "SKILL.md").write_text("# V2\n", encoding="utf-8")
        api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="overwrite",
        )
        assert installed.read_text(encoding="utf-8") == "# V2\n"

        # Revert the source and overwrite again.
        (skill / "SKILL.md").write_text("# V1\n", encoding="utf-8")
        api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="overwrite",
        )

        assert installed.read_text(encoding="utf-8") == "# V1\n"
        ledger = json.loads(
            (destination / onboarding_apply._LEDGER_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        records = [
            record
            for record in ledger["records"].values()
            if record.get("destination_key") == "skills:codex/review"
        ]
        assert len(records) == 1

    def test_identity_written_as_a_heading_is_excluded(self, tmp_path: Path) -> None:
        """A heading can BE the identity statement.

        Every previous narrowing of this scan (paragraph-start, first content
        line, non-heading lines only) left a hole; the exclusion was itself the
        bug, since "# You are Aria" is both a heading and an identity claim.
        """
        api = _api()
        home = tmp_path / "home"
        hermes = home / ".hermes"
        hermes.mkdir(parents=True)
        (hermes / "SOUL.md").write_text(
            "# You are Aria\nAlways cite paths.\n\n"
            "## Act as Aria\nAlways run the tests.\n\n"
            "# Rules\nNever force-push a shared branch.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"

        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)

        lessons = (destination / "lessons.jsonl").read_text(encoding="utf-8")
        assert "You are Aria" not in lessons
        assert "Act as Aria" not in lessons
        # Those paragraphs are dropped whole, directives included.
        assert "Always cite paths" not in lessons
        assert "Always run the tests" not in lessons
        # An ordinary headed directive still imports.
        assert "Never force-push a shared branch" in lessons

    def test_instruction_cap_is_shared_across_sources(self, tmp_path: Path) -> None:
        """The cap is a total, not a per-source allowance.

        A per-call counter let N sources each contribute _MAX_IMPORTED_LESSONS,
        so two sources could fill the store and start pruning user lessons.
        """
        api = _api()
        home = tmp_path / "home"
        many = "\n\n".join(
            f"Directive {index} long enough to be kept as a lesson." for index in range(80)
        )
        for source_dir, marker in ((".codex", "AGENTS.md"), (".claude", "CLAUDE.md")):
            root = home / source_dir
            root.mkdir(parents=True)
            (root / marker).write_text(many.replace("Directive", f"{source_dir} rule"), "utf-8")

        plan = api.preview_import(home=home, env={})
        total = sum(
            category["count"]
            for source in plan["sources"]
            for category in source["categories"]
            if category["id"] == "instructions"
        )

        # Each source is individually capped, and the store-capacity refusal at
        # write time bounds the total -- but the scan must not offer more than the
        # cap per source either.
        for source in plan["sources"]:
            for category in source["categories"]:
                if category["id"] == "instructions":
                    assert category["count"] <= onboarding_plan._MAX_IMPORTED_LESSONS
        destination = tmp_path / "destination"
        result = api.apply_import(plan, data_home=destination)
        from kiro_crew.learn import _MAX_LESSONS_TOTAL, LessonStore

        assert len(LessonStore(base_dir=destination).load_all()) <= _MAX_LESSONS_TOTAL
        assert result["imported"]["instructions"] <= total

    def test_upstream_deleted_skill_file_is_not_reported_as_deduplicated(
        self, tmp_path: Path
    ) -> None:
        """A stale extra file at the destination is a conflict, not "existing".

        "Every file I carry is present and identical" is not "the destination
        matches": an upstream DELETION would otherwise leave the removed file
        installed forever while the import reported deduplicated.
        """
        api = _api()
        home = tmp_path / "home"
        skill = home / ".codex" / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        (skill / "helper.py").write_text("print('old')\n", encoding="utf-8")
        destination = tmp_path / "destination"
        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)
        installed = destination / "skills" / "imported" / "codex" / "review"
        assert (installed / "helper.py").is_file()

        # Upstream drops the supporting file.
        (skill / "helper.py").unlink()

        skipped = api.apply_import(api.preview_import(home=home, env={}), data_home=destination)
        assert skipped["imported"]["skills"] == 0
        assert skipped["conflicts"][0]["category_id"] == "skills"
        # Nothing changed under the default strategy.
        assert (installed / "helper.py").is_file()

        # Overwrite replaces the whole tree, so the stale file goes.
        api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="overwrite",
        )
        assert not (installed / "helper.py").exists()
        assert (installed / "SKILL.md").read_text(encoding="utf-8") == "# Review\n"

    def test_instructions_use_an_available_vector_store_even_when_empty(
        self, tmp_path: Path
    ) -> None:
        """Route on availability, not current emptiness.

        An empty vector store becomes authoritative the moment any native lesson
        lands, and ContextBuilder then stops reading lessons.jsonl -- so a JSONL
        write made while it happened to be empty would disappear later, with the
        ledger preventing a re-import.
        """
        api = _api()
        from kiro_crew.vector_memory import VectorMemoryStore

        home = tmp_path / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        (codex / "AGENTS.md").write_text(
            "Always squash before opening a pull request.\n", encoding="utf-8"
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        store = VectorMemoryStore(db_path=destination / "memory.db")
        store.init()
        assert store.get_lessons() == []
        try:
            result = api.apply_import(
                api.preview_import(home=home, env={}),
                data_home=destination,
                vector_store=store,
            )
            rules = " ".join(str(lesson.get("value_json", "")) for lesson in store.get_lessons())
        finally:
            store.close()

        assert result["imported"]["instructions"] == 1
        # Written where ContextBuilder will read once any native lesson exists.
        assert "Always squash before opening a pull request" in rules

    def test_episodic_import_defers_embedding_and_reports_the_pending_count(
        self, tmp_path: Path
    ) -> None:
        """Embedding is the slow part, so it must not run on the caller's thread.

        A caller that passed its own store gets a non-zero
        ``embedding_backfill_pending`` and MUST schedule the sweep itself.
        """
        api = _api()
        home = tmp_path / "home"
        memory = home / ".predecessor" / "workspace" / "memory"
        memory.mkdir(parents=True)
        (memory / "notes.md").write_text(
            "The release checklist requires a canary stage before production.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"
        destination.mkdir()
        store = VectorMemoryStore(db_path=destination / "memory.db")
        store.init()
        embed_calls: list[str] = []
        store.embed_fn = lambda text: embed_calls.append(text) or [0.1] * store._embedding_dim
        try:
            result = api.apply_import(
                api.preview_import(home=home, env={}),
                data_home=destination,
                vector_store=store,
            )
            # Snapshot BEFORE sweeping — the sweep embeds by design, so reading
            # embed_calls afterwards would measure the sweep, not the apply.
            calls_during_apply = list(embed_calls)
            null_rows = store.db.execute(
                "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0 "
                "AND embedding IS NULL"
            ).fetchone()[0]
            # The sweep the caller is told to schedule fills them in.
            embedded = store.backfill_missing_embeddings()
            null_after = store.db.execute(
                "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0 "
                "AND embedding IS NULL"
            ).fetchone()[0]
        finally:
            store.close()

        assert result["imported"]["memories"] == 1
        assert result["embedding_backfill_pending"] == 1
        # No inline embed during apply — that is the whole point.
        assert calls_during_apply == []
        assert null_rows == 1
        assert embedded == 1
        assert null_after == 0

    def test_apply_sweeps_embeddings_itself_when_it_owns_the_store(self, tmp_path: Path) -> None:
        """No caller means nobody else can schedule the sweep — run it here.

        Reporting a pending count that nothing will ever act on would leave the
        rows NULL forever on the CLI path.
        """
        api = _api()
        home = tmp_path / "home"
        memory = home / ".predecessor" / "workspace" / "memory"
        memory.mkdir(parents=True)
        (memory / "notes.md").write_text(
            "The release checklist requires a canary stage before production.\n",
            encoding="utf-8",
        )
        destination = tmp_path / "destination"

        result = api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
        )

        assert result["imported"]["memories"] == 1
        # Already swept, so the caller is told there is nothing left to schedule.
        assert result["embedding_backfill_pending"] == 0

    def test_overwrite_never_deletes_a_leftover_retired_tree(self, tmp_path: Path) -> None:
        """A leftover retired tree is the only copy of an earlier version.

        Clearing it to make room would destroy exactly what the move-aside swap
        exists to preserve.
        """
        api = _api()
        home = tmp_path / "home"
        skill = home / ".codex" / "skills" / "review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# V1\n", encoding="utf-8")
        destination = tmp_path / "destination"
        api.apply_import(api.preview_import(home=home, env={}), data_home=destination)
        codex_dir = destination / "skills" / "imported" / "codex"

        # Simulate an interrupted earlier overwrite: a retired tree left behind
        # under the name this import's fingerprint would choose.
        (skill / "SKILL.md").write_text("# V2\n", encoding="utf-8")
        plan = api.preview_import(home=home, env={})
        fingerprint = next(
            item["item_hash"]
            for item in api.apply_import(
                plan, data_home=tmp_path / "probe", conflict_strategy="skip"
            )["item_outcomes"]
            if item["category_id"] == "skills"
        )
        leftover = codex_dir / f".review.replaced-{fingerprint[:8]}"
        leftover.mkdir(parents=True)
        (leftover / "SKILL.md").write_text("# V0 precious\n", encoding="utf-8")

        api.apply_import(
            api.preview_import(home=home, env={}),
            data_home=destination,
            conflict_strategy="overwrite",
        )

        # The leftover survives; the overwrite used a different retired path.
        assert (leftover / "SKILL.md").read_text(encoding="utf-8") == "# V0 precious\n"
        assert (codex_dir / "review" / "SKILL.md").read_text(encoding="utf-8") == "# V2\n"
