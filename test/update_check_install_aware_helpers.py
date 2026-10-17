"""The autouse fixture every install-aware update-check module shares.

``test_update_check_install_aware.py`` and its ``_auto_apply`` and ``_background_work``
siblings import ``_wheel_install`` by name, so every test in them defaults to a wheel
install on the insider lane, with the module's update cache restored afterwards. A fixture
that is autouse in each of the three has to live in a module they all import.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.handlers import updates
from kiro_crew.platform import update_capability, update_layout


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing module autouse fixture, moved verbatim; no test_update_check_install_aware*.py test calls monkeypatch.undo()
def _wheel_install(monkeypatch, tmp_path):
    """Default every test in this module to a WHEEL install on the insider lane.

    A git checkout is opt-in per test (``_git_install``), because the interesting
    layout is the one skipped entirely without that checkout.
    """
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)
    (tmp_path / "channel").write_text("insider\n")
    monkeypatch.setattr(update_layout, "data_home", lambda: tmp_path)
    # Pin the packaging stamp rather than inheriting the ambient one: a checkout
    # has no `_build_info.py` and reports `source`, but an installed wheel reports
    # `wheel`, and the suite must not read differently depending on where it runs.
    monkeypatch.setattr(update_capability, "distribution", lambda: "wheel")
    original = dict(updates._update_info)
    yield
    updates._update_info.clear()
    updates._update_info.update(original)
