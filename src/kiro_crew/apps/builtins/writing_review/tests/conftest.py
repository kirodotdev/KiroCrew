"""Shared fixtures for the Writing Review suite.

The repository rootdir ``conftest.py`` already pins ``$KIROCREW_HOME`` per
test and redirects the system tempdir, which is enough to keep every write
this suite performs inside a pytest-managed directory. Writing Review does
not spawn subprocesses that emit SEL audit events (unlike Code Review Sage,
which mutes ``github_runner._audit_run`` here), so no app-specific audit
mute is required.

This module still exists so that pytest recognises the directory as a test
package with its own collection scope, and so that any app-specific
fixtures added later have a home.
"""

import pytest


@pytest.fixture(autouse=True)
def _writing_review_isolation() -> None:
    """Placeholder autouse fixture to keep the suite import stable.

    The rootdir ``conftest.py`` supplies the real isolation. Kept as an
    autouse hook so that future audit or side-effect muting can be added
    here without every test in the suite having to opt in.
    """
    return None
