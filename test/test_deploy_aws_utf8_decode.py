"""The deploy-web AWS CLI runner decodes the child's output as UTF-8, leniently.

The ``aws`` child inherits ``PYTHONUTF8=1`` from ``ensure_utf8_console``, so it
writes UTF-8 on every platform. A text-mode spawn without ``encoding=`` decodes
with the locale's code page instead, which on Windows is the ANSI code page: a
non-ASCII bucket key, path or localized error comes back as mojibake. So the call
decodes as UTF-8 with ``errors="replace"``, the shared ``UTF8_TEXT`` shape. Pinned
where it matters: the keyword arguments ``run_aws`` hands ``run_limited``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.deploy import engine


def _capture_run_limited(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake_run_limited(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="caf\u00e9", stderr="")

    monkeypatch.setattr(engine, "wrap_argv", lambda argv, **kw: (list(argv), None))
    monkeypatch.setattr(engine, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr(engine, "run_limited", fake_run_limited)
    return seen


def test_run_aws_decodes_child_output_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_run_limited(monkeypatch)

    rc, out, err = engine.run_aws(["s3api", "list-objects-v2"], "prof")

    assert (rc, out, err) == (0, "caf\u00e9", "")
    assert seen.get("text") is True
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"
