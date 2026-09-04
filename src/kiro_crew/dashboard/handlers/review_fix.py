"""Dashboard route adapters for the shared review-fix HTTP service."""

from __future__ import annotations


def _adapter():
    # Import through the canonical package path: exec-loading the file under a
    # private alias used to mint a SECOND module identity whose enum classes
    # failed ``is`` comparisons against the task models the Task Runner holds,
    # and whose lines measured as uncovered under --cov=kiro_crew.
    from kiro_crew.apps.builtins.code_review_sage.backend import fix_tasks

    return fix_tasks


async def api_taskrunner_review_fix(request):
    return await _adapter().handle_get_fix_task(request)


async def api_taskrunner_review_fix_actions(request):
    return await _adapter().handle_fix_action(request)
