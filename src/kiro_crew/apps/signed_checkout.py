"""Publisher-tracked files excluded from a signed app's packaged inventory."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from kiro_crew.apps.registry_pipeline.git_targets import _GIT_EXEC_NEUTRALIZER_PAIRS
from kiro_crew.apps.registry_pipeline.subprocess_env import anonymous_git_env
from kiro_crew.sandbox import cgroup_scope_argv, run_limited, wrap_argv

logger = logging.getLogger(__name__)


def tracked_unpackaged_entry(root: Path) -> str | None:
    """Refuse tracked excluded content, without rejecting generated dependencies.

    Git's NUL-delimited names are relative to the app directory, including when
    the app lives in a repository subdirectory. The read is credential-free and
    disables repository-selected hooks and filesystem monitors. Blocking.
    """
    from kiro_crew.apps.manager import is_uncopied_name
    from kiro_crew.apps.signed_content import _ROOT_EXCLUDED

    neutralizers = [
        part for key, value in _GIT_EXEC_NEUTRALIZER_PAIRS for part in ("-c", f"{key}={value}")
    ]
    try:
        argv, _cleanup = wrap_argv(
            ["git", *neutralizers, "ls-files", "--cached", "-z", "--", "."],
            mode="strict",
        )
        result = run_limited(
            cgroup_scope_argv(argv),
            cwd=str(root),
            env=anonymous_git_env(),
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return "publisher tracked files could not be inspected"
        names = result.stdout.decode("utf-8", "strict").split("\x00")
    except Exception:
        logger.debug("publisher tracked-file inspection failed", exc_info=True)
        return "publisher tracked files could not be inspected"
    for name in names:
        if not name:
            continue
        parts = PurePosixPath(name).parts
        if any(is_uncopied_name(part) for part in parts) or (
            parts[0] in _ROOT_EXCLUDED and parts[0] != "app.json"
        ):
            return name
    return None
