"""Checks the ``test_*_config_handlers.py`` files share, written once and run per file.

Each file pins one channel's config save handler through its own ``_client_put`` and
``_accept_token`` helpers, so a validation case two channels share has the same body
in both files. A file opts in by naming the case in a module-level
``SHARED_CONFIG_HANDLER_CHECKS`` tuple; discovery is :mod:`shared_checks_discovery`.
"""

from __future__ import annotations

from pathlib import Path

import shared_checks_discovery

TEST_DIR = Path(__file__).resolve().parent

#: Every check a file can opt into. A name outside this set is a typo that would
#: otherwise opt the file out silently.
KNOWN_CHECKS = frozenset(
    {
        "save_rejects_non_numeric_user_ids",
        "soft_threshold_bounds",
        "save_rejects_whitespace_credentials",
    }
)

_OPTED_IN = shared_checks_discovery.discover(
    TEST_DIR, "test_*_config_handlers.py", "SHARED_CONFIG_HANDLER_CHECKS"
)
_each = _OPTED_IN.each
_load = shared_checks_discovery.load


def test_every_opt_in_is_found_parses_and_names_known_checks() -> None:
    """Discovery finds every file naming the tuple, each tuple parses and names only
    known checks, and every known check runs on some file: a discovery that went blind
    would collect zero params and pass."""
    naming = {
        path.stem
        for path in TEST_DIR.glob("test_*_config_handlers.py")
        if "SHARED_CONFIG_HANDLER_CHECKS" in path.read_text(encoding="utf-8")
    }
    assert naming == set(_OPTED_IN.checks)
    assert _OPTED_IN.errors == []
    assert _OPTED_IN.unknown(KNOWN_CHECKS) == {}
    assert _OPTED_IN.unused(KNOWN_CHECKS) == []


@_each("save_rejects_non_numeric_user_ids")
def test_save_rejects_non_numeric_user_ids(contract: str, tmp_path: Path, monkeypatch) -> None:
    subject = _load(contract)
    import kiro_crew.dashboard.handlers.messaging as mod

    subject._accept_token(monkeypatch, mod)
    status_body, _ = subject._client_put(
        mod, monkeypatch, tmp_path, {"allowed_user_ids": ["@username"]}
    )
    status, body = status_body
    assert status == 400
    assert "numeric" in body["error"]


@_each("soft_threshold_bounds")
def test_soft_threshold_bounds(contract: str, tmp_path: Path, monkeypatch) -> None:
    subject = _load(contract)
    import kiro_crew.dashboard.handlers.messaging as mod

    subject._accept_token(monkeypatch, mod)
    for bad in (0, 101, "80", True):
        status_body, _ = subject._client_put(
            mod, monkeypatch, tmp_path, {"soft_threshold_pct": bad}
        )
        assert status_body[0] == 400, f"soft_threshold_pct={bad!r} should be rejected"
        # A machine-readable code, not prose only: the dashboard renders `error`
        # verbatim into a localized UI, so prose alone is untranslatable.
        assert status_body[1]["code"] == "soft_threshold_pct_invalid"


@_each("save_rejects_whitespace_credentials")
def test_save_rejects_whitespace_credentials(contract: str, tmp_path: Path, monkeypatch) -> None:
    """A secret carrying inner whitespace fails before any write."""
    subject = _load(contract)
    import kiro_crew.dashboard.handlers.messaging as mod

    status_body, env = subject._client_put(mod, monkeypatch, tmp_path, {"bot_token": "two words"})
    status, body = status_body
    assert status == 400
    assert "whitespace" in body["error"]
    assert "two" not in env.read_text(encoding="utf-8")
