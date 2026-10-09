from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kiro_crew.folder_steering import collect_folder_steering
from kiro_crew.learn import LessonStore
from kiro_crew.personal_insights.insights_canonical import source_content_digest
from kiro_crew.personal_insights.insights_guidance import GuidanceDocument

LESSON_KIND: Final[str] = "lesson"
STEERING_KIND: Final[str] = "steering"


@dataclass(frozen=True)
class GuidanceSnapshot:
    documents: tuple[GuidanceDocument, ...]
    snapshot_digest: str

    def source_digests(self) -> tuple[str, ...]:
        return tuple(document_digest for document_digest in self._digests())

    def _digests(self) -> tuple[str, ...]:
        return tuple(source_content_digest(document.text) for document in self.documents)


def _lesson_scope(repo_scope: str | None) -> str:
    if repo_scope is None:
        return "global"
    return f"repo:{repo_scope}"


def capture_memory_only(
    lessons_base_dir: Path,
    steering_dirs: list[str],
    *,
    home: Path,
    workspace_scope: str,
) -> GuidanceSnapshot:
    documents: list[GuidanceDocument] = []
    store = LessonStore(base_dir=lessons_base_dir)
    for lesson in store.load_all():
        scope = _lesson_scope(lesson.repo_scope)
        if scope != "global" and scope != workspace_scope:
            continue
        text = lesson.rule if not lesson.negative else f"{lesson.rule}\n{lesson.negative}"
        documents.append(
            GuidanceDocument(source_id=f"{LESSON_KIND}:{scope}", text=text, kind=LESSON_KIND)
        )
    collection = collect_folder_steering(
        steering_dirs, project=None, home=home, skip_delivered_roots=False
    )
    for source_path, body in collection.documents:
        documents.append(
            GuidanceDocument(
                source_id=f"{STEERING_KIND}:{source_path}", text=body, kind=STEERING_KIND
            )
        )
    digest = _snapshot_digest(documents)
    return GuidanceSnapshot(documents=tuple(documents), snapshot_digest=digest)


def _snapshot_digest(documents: list[GuidanceDocument]) -> str:
    hasher = hashlib.sha256()
    for digest in sorted(source_content_digest(document.text) for document in documents):
        hasher.update(digest.encode("ascii"))
    return hasher.hexdigest()
