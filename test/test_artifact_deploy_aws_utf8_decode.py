"""The artifact-deploy backend scripts decode the aws CLI output as UTF-8.

``attach_backend.py`` and ``detach_backend.py`` each spawn the ``aws`` CLI
through their own ``aws()`` helper. The child inherits ``PYTHONUTF8=1`` from
``ensure_utf8_console`` and writes UTF-8, so a text-mode spawn without
``encoding=`` would decode with the locale code page (the ANSI code page on
Windows) and turn non-ASCII names or localized errors into mojibake. Pinned
where it matters: the keyword arguments each helper hands ``run_limited``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import kiro_crew.deploy.engine as engine
import kiro_crew.sandbox as sandbox

_SCRIPTS = (
    Path(__file__).resolve().parent.parent / "src/kiro_crew/deploy/skills/artifact-deploy/scripts"
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_ad_{name}", _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("script", ["attach_backend", "detach_backend"])
def test_aws_helper_decodes_child_output_as_utf8(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    seen: dict[str, object] = {}

    def fake_run_limited(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="caf\u00e9", stderr="")

    monkeypatch.setattr(engine, "resolve_aws_bin", lambda: "aws")
    monkeypatch.setattr(sandbox, "sandboxed_spawn_argv", lambda cmd: (list(cmd), {}, None))
    monkeypatch.setattr(sandbox, "run_limited", fake_run_limited)

    out = _load(script).aws("prof", "us-east-1", "sts", "get-caller-identity")

    assert out == "caf\u00e9"
    assert seen.get("text") is True
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"
    assert seen.get("capture_output") is True
    assert seen.get("timeout") == 300
