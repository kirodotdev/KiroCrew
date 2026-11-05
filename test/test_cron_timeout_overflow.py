"""An infinite cron ``timeout`` or ``timeout_secs`` is refused with ``ValueError``.

``int(float("inf"))`` raises ``OverflowError``, which is not a ``ValueError``.
The cron store documents ``ValueError`` for every refused field, and
``every_secs`` already maps the overflow to it; the two timeouts did not, so an
app passing one through the cron SDK got a different exception and nothing was
written either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.cron import CronService


def _refusal(fn, *args, **kwargs) -> str:
    """The refusal's type name, so an escaped ``OverflowError`` fails the assert."""
    try:
        fn(*args, **kwargs)
    except ValueError:
        return "ValueError"
    except OverflowError:
        return "OverflowError"
    return "accepted"


@pytest.mark.parametrize("field", ["timeout", "timeout_secs"])
def test_update_refuses_an_infinite_timeout_with_value_error(tmp_path: Path, field: str) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("n", "m", every_secs=300)

    assert _refusal(svc.update_job, job.id, **{field: float("inf")}) == "ValueError"
    stored = svc.get_job(job.id)
    assert stored is not None and stored.timeout_secs == job.timeout_secs


@pytest.mark.parametrize("field", ["timeout", "timeout_secs"])
def test_the_app_sdk_refuses_an_infinite_timeout_with_value_error(
    tmp_path: Path, field: str
) -> None:
    """The app cron SDK reads both fields before the store does, the same way."""
    from kiro_crew.apps.cron_sdk import CronSDK

    svc = CronService(base_dir=tmp_path)
    sdk = CronSDK("some-app", svc)

    outcome = _refusal(sdk.add_job, "n", "m", every_secs=300, **{field: float("inf")})

    assert outcome == "ValueError"
    assert svc.list_jobs(include_disabled=True) == []


@pytest.mark.parametrize("field", ["timeout", "timeout_secs"])
def test_add_refuses_an_infinite_timeout_with_value_error(tmp_path: Path, field: str) -> None:
    svc = CronService(base_dir=tmp_path)

    assert _refusal(svc.add_job, "n", "m", every_secs=300, **{field: float("inf")}) == "ValueError"
    assert svc.list_jobs(include_disabled=True) == []
