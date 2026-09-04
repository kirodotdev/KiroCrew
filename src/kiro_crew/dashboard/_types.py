"""Shared TYPE_CHECKING imports for dashboard modules."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.cron import CronService
    from kiro_crew.history import ConversationLog, HistoryConsolidator
    from kiro_crew.learn import LessonStore
    from kiro_crew.session import SessionManager
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.taskrunner import TaskRunner

__all__ = [
    "ContextBuilder",
    "CronService",
    "ConversationLog",
    "HistoryConsolidator",
    "LessonStore",
    "SessionManager",
    "SubagentManager",
    "TaskRunner",
]
