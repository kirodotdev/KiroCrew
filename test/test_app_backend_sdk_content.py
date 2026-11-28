"""The backend SDK's content checks and constants (seam level 17).

``kiro_crew.apps.backend_sdk.content`` and ``DENIED_ROOT_PARTS`` in ``.paths``. What is
pinned is what an app cannot check for itself: each check answers with the gateway's
own scanner (the same matches, the same verdict), looked up at call time so nothing is
a copy; the exfiltration check hands back a bool and never the scan's warning text;
both constants are bound from the gateway's values rather than written out; and every
name is published at seam level 17.
"""

from __future__ import annotations

import ast
import importlib
import string
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.testing.synthetic_credentials import aws_secret

_CONTENT_NAMES = ("CREDENTIAL_PLACEHOLDER", "contains_exfiltration_url", "iter_credential_matches")

#: A labelled secret, built at runtime so no scanner flags a literal in this file.
_CREDENTIAL_LINE = f"aws_secret_access_key={aws_secret(seed=17)}"

#: A URL whose long query carries data out: the scan flags it whatever the host.
_COLLECTOR = "https://collector.example.net/ingest?data="
_DATA_URL = _COLLECTOR + (string.ascii_lowercase * 11)[: 320 - len(_COLLECTOR)]


def test_the_content_names_are_listed_at_seam_level_seventeen() -> None:
    for name in _CONTENT_NAMES:
        assert name in sdk.__all__, name
        assert name in dir(sdk), name
        assert sdk._LAZY_EXPORTS[name] == "content", name
        assert sdk._EXPORT_LEVELS[name] == 17, name
    assert sdk._LAZY_EXPORTS["DENIED_ROOT_PARTS"] == "paths"
    assert sdk._EXPORT_LEVELS["DENIED_ROOT_PARTS"] == 17
    assert sdk.SDK_LEVEL >= 17


# ── the credential scan ─────────────────────────────────────────────────────


def test_the_credential_scan_finds_what_the_gateway_scan_finds() -> None:
    text = f"config:\n  {_CREDENTIAL_LINE}\n  region: us-east-1\n"
    found = [m.span() for m in sdk.iter_credential_matches(text)]
    assert found, "the planted secret was not matched"
    assert found == [m.span() for m in security.iter_credential_matches(text)]
    (match,) = sdk.iter_credential_matches(text)
    assert text[match.start() : match.end()] == match.group(0) == _CREDENTIAL_LINE


def test_the_credential_scan_finds_nothing_in_plain_text() -> None:
    assert list(sdk.iter_credential_matches("a plain sentence about the weather\n")) == []


def test_the_credential_scan_delegates_at_call_time(monkeypatch) -> None:
    seen: list[str] = []

    def recorder(text: str):
        seen.append(text)
        return iter(())

    monkeypatch.setattr("kiro_crew.security.iter_credential_matches", recorder)
    assert list(sdk.iter_credential_matches("probe")) == []
    assert seen == ["probe"]


# ── the exfiltration-URL check ──────────────────────────────────────────────


def test_a_data_carrying_url_is_flagged_and_a_plain_one_is_not() -> None:
    assert security.scan_exfiltration_urls(_DATA_URL)
    assert sdk.contains_exfiltration_url(f"see {_DATA_URL} for details") is True
    assert sdk.contains_exfiltration_url("see https://example.com/docs/page for details") is False


def test_the_check_answers_a_bool_and_never_the_warning_text(monkeypatch) -> None:
    # The private scan's warnings quote part of the flagged URL; only the answer leaves.
    warning = "suspicious URL to collector.example.net: ingest?data=abcdef"
    monkeypatch.setattr("kiro_crew.security.scan_exfiltration_urls", lambda _t: [warning])
    assert sdk.contains_exfiltration_url("anything") is True
    monkeypatch.setattr("kiro_crew.security.scan_exfiltration_urls", lambda _t: [])
    assert sdk.contains_exfiltration_url("anything") is False


def test_a_scan_that_raises_reaches_the_caller(monkeypatch) -> None:
    def boom(_text: str) -> list[str]:
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr("kiro_crew.security.scan_exfiltration_urls", boom)
    with pytest.raises(RuntimeError, match="policy unreadable"):
        sdk.contains_exfiltration_url(_DATA_URL)


# ── the constants ───────────────────────────────────────────────────────────


def test_the_placeholder_is_the_gateway_placeholder() -> None:
    assert sdk.CREDENTIAL_PLACEHOLDER == security.REDACTED_CREDENTIAL_TAG
    masked, _warnings = security.redact_credentials(_CREDENTIAL_LINE)
    assert sdk.CREDENTIAL_PLACEHOLDER in masked


def test_the_denied_root_parts_are_the_gateway_set() -> None:
    assert sdk.DENIED_ROOT_PARTS is security.DENIED_ROOT_PARTS
    assert isinstance(sdk.DENIED_ROOT_PARTS, frozenset)
    assert {".ssh", ".aws"} <= sdk.DENIED_ROOT_PARTS


def _bound_values(module_name: str, name: str) -> list[str]:
    module = importlib.import_module(module_name)
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    values: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                values.append(ast.unparse(node.value))
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                values.append(ast.unparse(node.value))
    return values


@pytest.mark.parametrize(
    ("module_name", "name", "source"),
    [
        (
            "kiro_crew.apps.backend_sdk.content",
            "CREDENTIAL_PLACEHOLDER",
            "security.REDACTED_CREDENTIAL_TAG",
        ),
        ("kiro_crew.apps.backend_sdk.paths", "DENIED_ROOT_PARTS", "security.DENIED_ROOT_PARTS"),
    ],
)
def test_each_constant_is_bound_from_the_gateway_value(module_name, name, source) -> None:
    # Equal values cannot tell the gateway's binding from a hand-copied literal; the
    # module source can.
    assert _bound_values(module_name, name) == [source]
