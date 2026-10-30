"""The feature-demo-recording skill points at files that exist.

The skill runs its steps from the video project directory, not the skill
directory, so a bare ``references/...`` path names nothing there. Its scripts
and its command blocks must name the real script location.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import types

import pytest
from skill_script_helpers import load_skill_script

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKILL = _REPO_ROOT / "src/kiro_crew/apps/builtins/dev_fleet/skills/feature-demo-recording"
_REFS = _SKILL / "references"


@pytest.fixture
def narrate_mod(monkeypatch):
    """Load narrate.py without leaking its ``sys.path`` insert or ``_pathcheck``."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    # Record ``_pathcheck``'s current state so teardown puts it back: absent
    # stays absent, a loaded module is restored.
    monkeypatch.setitem(sys.modules, "_pathcheck", sys.modules.get("_pathcheck"))
    monkeypatch.delitem(sys.modules, "_pathcheck")
    return load_skill_script("kc_video_narrate_paths", _REFS / "narrate.py")


def test_missing_tool_hint_names_the_real_deps_script(narrate_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(narrate_mod.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(narrate_mod, "_home", lambda: tmp_path)
    with pytest.raises(SystemExit) as exc:
        narrate_mod.tool("kc-no-such-tool")
    message = str(exc.value)
    match = re.search(r"run (\S+deps\.py) --install", message)
    assert match, message
    assert pathlib.Path(match.group(1)) == (_REFS / "deps.py").resolve()
    assert pathlib.Path(match.group(1)).is_file()


@pytest.mark.parametrize(("mode", "expected"), [("w", {"encoding": "utf-8"}), ("wb", {})])
def test_safe_open_output_writes_text_as_utf8(monkeypatch, tmp_path, mode, expected):
    # narr.json and the composed HTML are read back as utf-8 by read_json_input,
    # so a text-mode output opened in the locale code page cannot round-trip.
    pathcheck = load_skill_script("kc_video_pathcheck_encoding", _REFS / "_pathcheck.py")
    if not pathcheck._HAVE_DIRFD:
        pytest.skip("safe_open_output refuses every output without dir_fd support")
    real_fdopen = pathcheck.os.fdopen
    seen: list[dict[str, object]] = []

    def spy_fdopen(fd, *args, **kwargs):
        seen.append(kwargs)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(pathcheck.os, "fdopen", spy_fdopen)
    with pathcheck.safe_open_output(tmp_path / "narr.json", workdir=tmp_path, mode=mode):
        pass
    assert seen == [expected]


def test_record_template_reads_narration_as_utf8(monkeypatch, tmp_path):
    # narrate.py now writes narr.json as utf-8, so the recorder that paces the
    # capture from it must decode it as utf-8 too, not in the locale code page.
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    record = load_skill_script("kc_video_record_template", _REFS / "record_template.py")
    narr = tmp_path / "narr.json"
    lines = [
        {"role": "footage", "start": 1.0, "text": "next → step"},
        {"role": "outro", "start": 4.5, "text": "日本語"},
    ]
    narr.write_bytes(json.dumps({"lines": lines}, ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(record, "BASE", tmp_path)
    real_read_text = pathlib.Path.read_text
    seen: list[dict[str, object]] = []

    def spy_read_text(self, *args, **kwargs):
        seen.append(kwargs)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", spy_read_text)
    assert record.beat_targets("narr.json") == ([0.0], 3.5)
    assert seen == [{"encoding": "utf-8"}]


def test_step_commands_name_the_skill_references_dir():
    text = (_SKILL / "SKILL.md").read_text(encoding="utf-8")
    bare = [
        line
        for line in text.splitlines()
        if re.search(r"\$PY references/", line) or line.startswith("Copy `references/")
    ]
    assert bare == []
    for script in re.findall(r"<skill>/references/([\w.]+)", text):
        assert (_REFS / script).is_file(), script
