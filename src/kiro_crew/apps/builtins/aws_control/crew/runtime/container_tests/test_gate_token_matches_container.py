"""The deploy gate greps for a token the container declares. Pin them together.

``container/backup/restore.py`` exports ``SUMMARY_TOKEN`` and documents it as an
interface. ``deploy/smc-deploy.sh`` greps the boot log for that same string, in
bash, where it cannot import the constant. So the string exists in two languages
with nothing joining them, which is the shape of a defect this project has now
shipped once and nearly shipped twice.

Renaming the token is the dangerous edit, because the gate does not break: the
grep simply matches nothing, the judge sees an empty line, and it reports
"restore did not run" for a restore that ran perfectly. A wrong diagnosis is
worse than a missing one, since it sends the reader to the boot path instead of
to the rename that actually happened.

These tests are the join. They read the driver as text, which is the only way to
check a bash literal from Python.
"""

from __future__ import annotations

import re
from pathlib import Path

from container.backup.restore import SUMMARY_TOKEN

# The driver and its gate suite live beside each other under crew/scripts/, one
# level up from this suite's own build-context root.
_CREW = Path(__file__).resolve().parents[2]
DRIVER = _CREW / "scripts" / "smc-deploy.sh"
GATE_TESTS = _CREW / "scripts" / "tests" / "run_gate_tests.sh"


def test_the_driver_greps_the_token_the_container_exports() -> None:
    """MUTATION: change SUMMARY_TOKEN and this reddens instead of the gate lying."""
    assert SUMMARY_TOKEN in DRIVER.read_text(), (
        f"the driver does not grep {SUMMARY_TOKEN!r}. A renamed token makes the "
        "gate report that restore never ran, which is a wrong diagnosis, not a "
        "missing one."
    )


def test_every_field_the_gate_reads_is_a_field_the_container_writes() -> None:
    """The counters are an interface too, and bash reads them by name.

    A renamed field turns the gate's strong assertion into a refusal for the
    wrong reason, so the names must agree, not merely the token.
    """
    log_call = _summary_format_string()
    driver = DRIVER.read_text()
    for field in ("state", "transcripts_restored", "transcripts_available"):
        assert f"{field}=" in log_call, f"restore no longer logs {field}="
        assert f"{field}=" in driver, f"the gate no longer reads {field}="


def test_the_gate_tests_fixture_is_the_shape_restore_really_emits() -> None:
    """A fixture that drifts from the real line tests a line nothing writes.

    The first version of this fixture ended ``missing=[]``; restore renders an
    empty list as ``missing=none``. Harmless there only because no assertion read
    that field, which is luck rather than design.
    """
    fixture = re.search(r'sum_ok="([^"]+)"', GATE_TESTS.read_text())
    assert fixture, "the gate tests no longer define a sum_ok fixture"
    line = fixture.group(1)
    assert line.startswith(SUMMARY_TOKEN), line
    fmt = _summary_format_string()
    for field in re.findall(r"(\w+)=", fmt.replace("%d", "").replace("%s", "")):
        assert f"{field}=" in line, f"the fixture omits {field}=, which restore emits"


def _summary_format_string() -> str:
    """The literal format string restore passes to logger.info.

    Read from source rather than by capturing a log record, because the point is
    to check the names in the code the gate was written against.
    """
    src = (Path(__file__).resolve().parents[1] / "container" / "backup" / "restore.py").read_text()
    start = src.index("def _log_summary")
    body = src[start : start + 900]
    parts = re.findall(r'"([^"]*=%[ds][^"]*)"', body)
    assert parts, "could not find the summary format string in _log_summary"
    return "".join(parts)
