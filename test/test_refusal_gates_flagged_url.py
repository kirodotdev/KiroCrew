"""Every refusal gate refuses a URL the exfiltration scan flags, as well as a credential.

A refusal gate decides by asking whether its redactor would change a value. The
redactor masks a credential and never rewrites a URL, so a gate that asked only the
redactor would let a URL whose query carries data out through: a name, a slug, a file
that leaves for a channel, an MCP tool argument, a workflow source. ``security.refuses``
asks both: the gate's own redactor, run in full whatever the owner's setting, and
``scan_exfiltration_urls``. These tests pin it and a representative gate of each kind.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kiro_crew import redaction_seams
from kiro_crew.platform import context
from kiro_crew.platform.context import binary_content_is_flagged, wide_content_is_flagged
from kiro_crew.security import redact, refuses, scan_exfiltration_urls
from kiro_crew.testing.synthetic_credentials import github_classic

_COLLECTOR = "https://collector.example.net/ingest?data="
#: A 320-character URL whose query carries data out, built at run time. Its payload
#: is a plain repeated phrase, so no entropy or credential test finds it: only the
#: exfiltration scan does.
FLAGGED_URL = _COLLECTOR + ("hello-world-" * 30)[: 320 - len(_COLLECTOR)]
#: A short link with an ordinary query: no gate refuses it.
SHORT_URL = "https://docs.example.com/guide?page=2"


def test_the_flagged_url_is_flagged_and_left_as_written() -> None:
    assert len(FLAGGED_URL) == 320
    assert scan_exfiltration_urls(FLAGGED_URL)
    assert redact(FLAGGED_URL) == FLAGGED_URL
    assert not scan_exfiltration_urls(SHORT_URL)


@pytest.fixture
def off_seam(monkeypatch: pytest.MonkeyPatch):
    """Redaction published off, and the chat output seam open: the pass stands down."""
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    with redaction_seams.output_seam(redaction_seams.CHAT):
        yield


class TestRefuses:
    def test_a_flagged_url_is_refused(self) -> None:
        assert refuses(f"see {FLAGGED_URL}", redact)

    def test_a_credential_is_refused(self) -> None:
        assert refuses(f"token {github_classic(seed=71)}", redact)

    def test_plain_text_and_a_short_url_pass(self) -> None:
        assert not refuses("plain words", redact)
        assert not refuses(f"read {SHORT_URL}", redact)
        assert not refuses("", redact)

    def test_inside_an_off_seam_both_are_still_refused(self, off_seam: None) -> None:
        token = github_classic(seed=72)
        assert redact(token) == token  # the seam's pass stands down
        assert refuses(token, redact)
        assert refuses(FLAGGED_URL, redact)
        assert not refuses("plain words", redact)

    def test_the_redactor_decides_alone_when_it_masks(self) -> None:
        seen: list[str] = []

        def masks(text: str) -> str:
            seen.append(text)
            return "[masked]"

        assert refuses("anything", masks)
        assert seen == ["anything"]


class TestFileContentReaders:
    """The shared upload and outbox content scans in ``platform.context``."""

    @pytest.fixture(autouse=True)
    def _fresh_verdicts(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        from collections import OrderedDict

        _floor_monkeypatch.setattr(context, "_SCAN_VERDICTS", OrderedDict())

    @staticmethod
    def _binary(text: str) -> bytes:
        return b"\x89PNG\r\n\x1a\n\xff\xfe" + text.encode() + b"\x00\xff"

    @staticmethod
    def _wide(text: str) -> bytes:
        return b"\xff\xfe" + text.encode("utf-16-le")

    def test_the_binary_scan_flags_a_url_in_the_bytes(self) -> None:
        assert binary_content_is_flagged(self._binary(FLAGGED_URL)) is True
        assert binary_content_is_flagged(self._binary(SHORT_URL)) is False

    def test_the_wide_scan_flags_a_url_at_utf16_spacing(self) -> None:
        assert wide_content_is_flagged(self._wide(f"link {FLAGGED_URL}")) is True
        assert wide_content_is_flagged(self._wide(f"link {SHORT_URL}")) is False

    def test_the_binary_scan_reaches_the_wide_text_too(self) -> None:
        assert binary_content_is_flagged(self._wide(FLAGGED_URL)) is True

    def test_both_scans_flag_it_inside_an_off_seam(self, off_seam: None) -> None:
        assert binary_content_is_flagged(self._binary(FLAGGED_URL)) is True
        assert wide_content_is_flagged(self._wide(FLAGGED_URL)) is True
        assert binary_content_is_flagged(self._binary(SHORT_URL)) is False


def _raises(call: Callable[[], object], error: type[BaseException]) -> bool:
    try:
        call()
    except error:
        return True
    return False


def _member_label(value: str, _tmp: Path) -> bool:
    from kiro_crew.external_text import external_text_requires_redaction

    return external_text_requires_redaction(value)


def _package_slug(value: str, _tmp: Path) -> bool:
    from kiro_crew.dashboard.handlers.member_dashboard import _name_unsafe

    return _name_unsafe(value)


def _mcp_tool_argument(value: str, _tmp: Path) -> bool:
    from kiro_crew.cron_script_context import _scanned_tool_args

    return _raises(lambda: _scanned_tool_args("srv", "post", {"q": value}), ValueError)


def _workflow_source(value: str, tmp: Path) -> bool:
    from kiro_crew.workflows.library import (
        SensitiveWorkflowSourceError,
        WorkflowDefinitionLibrary,
    )

    library = WorkflowDefinitionLibrary(base_dir=tmp)
    source = f"LINK = {value!r}\n"
    return _raises(lambda: library.create(source=source, name="w"), SensitiveWorkflowSourceError)


def _job_param(value: str, _tmp: Path) -> bool:
    from kiro_crew.apps.job_sdk_records import _validated_params

    return _raises(lambda: _validated_params({"link": value}), ValueError)


def _package_field_name(value: str, _tmp: Path) -> bool:
    from kiro_crew.artifact_store.dashboard_package import (
        ArtifactValidationError,
        _refuse_credential_shaped_name,
    )

    return _raises(lambda: _refuse_credential_shaped_name(value), ArtifactValidationError)


def _stderr_tail(value: str, _tmp: Path) -> bool:
    from kiro_crew.acp.stderr_tail import STDERR_WITHHELD, safe_stderr_tail

    return safe_stderr_tail(["harness failed", value], " | ") == STDERR_WITHHELD


def _agent_capabilities_view(value: str, _tmp: Path) -> bool:
    from kiro_crew.agent_capabilities import _safe_view

    return _safe_view({"description": value}, ())["description"] == "[REDACTED]"


def _roster_value(value: str, _tmp: Path) -> bool:
    from kiro_crew.dashboard.handlers.agents import _roster_mask
    from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

    return _roster_mask(value) == _SENSITIVE_MASK


def _code_channel_bar_url(value: str, _tmp: Path) -> bool:
    from kiro_crew.slack.code_channels.tools import ArgumentError, _bar_items

    # A url item takes only an https link, so plain text stands in as the short one.
    url = value if value.startswith("https://") else SHORT_URL
    item = {"key": "docs", "label": "Docs", "url": url}
    return _raises(lambda: _bar_items({"items": [item]}), ArgumentError)


def _elicitation_value(value: str, _tmp: Path) -> bool:
    from kiro_crew.dashboard import chat_runner

    return chat_runner._value_tainted(value)


def _telemetry_attribute(value: str, _tmp: Path) -> bool:
    from kiro_crew.metrics.schema import redact as redact_attribute

    return redact_attribute(value) == "[REDACTED]"


def _slide_text(value: str, _tmp: Path) -> bool:
    from kiro_crew.dashboard.handlers.office_slides import _ContentRedacted, _screen_rendered_text

    return _raises(lambda: _screen_rendered_text(f"Slide 1\n{value}"), _ContentRedacted)


GATES = [
    pytest.param(_member_label, id="name-member-label"),
    pytest.param(_package_slug, id="slug-member-dashboard-package"),
    pytest.param(_mcp_tool_argument, id="mcp-tool-argument"),
    pytest.param(_workflow_source, id="workflow-source"),
    pytest.param(_job_param, id="job-param"),
    pytest.param(_package_field_name, id="name-package-field"),
    pytest.param(_stderr_tail, id="stderr-tail"),
    pytest.param(_agent_capabilities_view, id="agent-capabilities-view"),
    pytest.param(_roster_value, id="roster-value"),
    pytest.param(_code_channel_bar_url, id="channel-context-bar-url"),
    pytest.param(_elicitation_value, id="elicitation-value"),
    pytest.param(_telemetry_attribute, id="telemetry-attribute"),
    pytest.param(_slide_text, id="slide-render"),
]


class TestRefusalGates:
    """Each gate refuses the flagged URL, and lets plain text and a short URL through."""

    @pytest.mark.parametrize("refused", GATES)
    def test_a_flagged_url_is_refused(
        self, refused: Callable[[str, Path], bool], tmp_path: Path
    ) -> None:
        assert refused(FLAGGED_URL, tmp_path)

    @pytest.mark.parametrize("refused", GATES)
    @pytest.mark.parametrize("value", ["plain words", SHORT_URL])
    def test_a_benign_value_passes(
        self, refused: Callable[[str, Path], bool], value: str, tmp_path: Path
    ) -> None:
        assert not refused(value, tmp_path)
