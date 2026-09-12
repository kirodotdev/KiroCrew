"""Dashboard route adapters for the shared review-fix HTTP service."""

# Enablement for both routes below lives in the shared handlers themselves
# (``fix_tasks._require_enabled``), so the Sage app's own fix-task surface and
# this core-dashboard surface cannot drift apart. This module must not import
# Sage-side modules at load time — the deferred import below is the boundary.


from __future__ import annotations


def _adapter():
    # Import through the canonical package path: exec-loading the file under a
    # private alias mints a SECOND module identity whose enum classes fail
    # ``is`` comparisons against the task models the Task Runner holds, and
    # whose lines measure as uncovered under --cov=kiro_crew.
    from kiro_crew.apps.builtins.code_review_sage.backend import fix_tasks

    return fix_tasks


async def api_taskrunner_review_fix(request):
    return await _adapter().handle_get_fix_task(request)


async def api_taskrunner_review_fix_actions(request):
    return await _adapter().handle_fix_action(request)
