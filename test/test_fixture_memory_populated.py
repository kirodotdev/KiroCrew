"""Consumer coverage for the ``memory-populated`` seed fixture."""

from pathlib import Path

from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.testing.fixtures import seeded_home


def test_memory_populated_fixture_drives_production_readers() -> None:
    with seeded_home("memory-populated") as home:
        lessons = LessonStore(base_dir=home).load_all()
        assert [
            (lesson.rule, lesson.negative, lesson.ts, lesson.category, lesson.repo_scope)
            for lesson in lessons
        ] == [
            (
                "Run the diff-scoped gate before asking for review, not the full suite.",
                "Do not run the whole suite for a two-file change.",
                "2026-01-16T00:00:00+00:00",
                "tool",
                None,
            ),
            (
                "Use the worktree's own venv for every test command.",
                "Do not use the system python; it lacks the dev group.",
                "2026-01-16T00:00:01+00:00",
                "tool",
                "src/kiro_crew",
            ),
            (
                "Say what was verified and what was not.",
                None,
                "2026-01-16T00:00:02+00:00",
                "preference",
                None,
            ),
        ]

        memory = MemoryStore(workspace=home / "workspace")
        snapshot = memory.markdown_snapshot()
        semantic = memory._guarded_entry(  # noqa: SLF001
            home / "workspace" / "memory" / "semantic.md"
        )
        history = {entry["date"]: entry["content"] for entry in snapshot["history"]}
        assert set(history) == {"2026-01-14", "2026-01-15"}

        layers = [
            snapshot["preferences"]["content"],
            snapshot["projects"]["content"],
            semantic["content"],
            history["2026-01-14"],
            history["2026-01-15"],
        ]
        assert all(content.strip() for content in layers)
        assert len(set(layers)) == len(layers)
        assert not list(home.rglob("*.db"))

    assert not Path(home).exists()
