"""Tests for the enriched ``task_models`` dataclass fields.

Preserved from the deleted ``test/test_aidlc_store.py`` (the orphaned aidlc
package's test file): its ``TestEnrichedDataclasses`` class covered live
``kiro_crew.task_models`` code, not the deleted store, so the assertions move
here instead of dying with the package.
"""

from __future__ import annotations

from kiro_crew.task_models import Project, Task


class TestEnrichedDataclasses:
    def test_task_new_fields(self):
        t = Task(
            index=0,
            title="t",
            description="d",
            priority="high",
            story_points=5,
            task_type="fix",
        )
        assert t.priority == "high"
        assert t.story_points == 5
        assert t.task_type == "fix"

    def test_project_new_fields(self):
        p = Project(
            spec_path="",
            spec_content="",
            mode="spec",
            source_spec="do stuff",
            skip_planning=True,
        )
        assert p.mode == "spec"
        assert p.source_spec == "do stuff"
        assert p.skip_planning is True
