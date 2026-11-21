"""The test floor hands every test a cold vault read-audit cache.

``secrets.read_audit`` caches the audit switch it read from ``config.json``
(``_loaded``) and the per-secret windows (``_seen``) for the whole process. Without
``test/conftest_resets.py``'s ``_cold_vault_read_audit`` a test inherits whatever an
earlier test on the worker left there, which is how
``test_status_loads_config_off_the_event_loop`` counted one config load or two by
order. Each case below asserts the cold state, then warms both caches the way a vault
read does, so whichever case runs second on the worker fails if the reset is gone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.secrets import read_audit

# Same worker for both cases: the leak this guards is per process.
pytestmark = pytest.mark.xdist_group(name="vault_read_audit_floor")


@pytest.mark.parametrize("case", ["first", "second"])
def test_each_test_starts_with_a_cold_read_audit_cache(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert read_audit._loaded is None
    assert read_audit._seen == {}

    cfg = SimpleNamespace(dashboard=SimpleNamespace(audit_secret_reads=True))
    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    assert read_audit._enabled() is True
    # The switch is now cached process-wide, as after a real vault read.
    assert read_audit._loaded is not None
    read_audit._seen[(b"\0" * 32, "test", "get")] = read_audit._Window(
        last=0.0, absorbed=0, shown="API_TOKEN"
    )
