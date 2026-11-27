"""narrate.py refuses on a host it cannot write outputs on before doing any work.

``_pathcheck.safe_open_output`` refuses every output on a host without
directory-descriptor support (Windows). Each spoken line is a speech call, and
with Polly a billed request, so narrate.py must refuse before the first
``synthesize()`` and before any clip is published, not at its first text write
after the whole loop has run.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest
from skill_script_helpers import load_skill_script

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REFS = (
    _REPO_ROOT / "src/kiro_crew/apps/builtins/dev_fleet/skills/feature-demo-recording/references"
)

_SCRIPT = {
    "lines": [
        {"role": "intro", "say": "Hello.", "cap": "Hello", "title": "Demo"},
        {"role": "footage", "say": "Click here.", "cap": "Click", "dur": 1.5},
        {"role": "outro", "say": "Bye.", "cap": "Bye", "title": "End"},
    ]
}


@pytest.fixture
def narrate_mod(monkeypatch):
    """Load narrate.py without leaking its ``sys.path`` insert or its helpers."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    for helper in ("_pathcheck", "_toolpath"):
        monkeypatch.setitem(sys.modules, helper, sys.modules.get(helper))
        monkeypatch.delitem(sys.modules, helper)
    return load_skill_script("kc_video_narrate_platform", _REFS / "narrate.py")


def _run(narrate_mod, monkeypatch, tmp_path, *, have_dirfd, extra=()):
    """Run narrate.main in tmp_path with the speech and media tools faked."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "script.json").write_text(json.dumps(_SCRIPT), encoding="utf-8")
    monkeypatch.setattr(sys.modules["_pathcheck"], "_HAVE_DIRFD", have_dirfd)
    calls = {"synthesize": 0, "ffmpeg": 0}

    def fake_synthesize(provider, text, base, **_kw):
        calls["synthesize"] += 1
        raw = pathlib.Path(f"{base}.mp3")
        raw.write_bytes(b"raw")
        return raw

    def fake_ffmpeg(_ff, argv, **_kw):
        calls["ffmpeg"] += 1
        pathlib.Path(argv[-1]).write_bytes(b"mp3")

    monkeypatch.setattr(narrate_mod, "resolve_provider", lambda *_a: "polly")
    monkeypatch.setattr(narrate_mod, "synthesize", fake_synthesize)
    monkeypatch.setattr(narrate_mod, "_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(narrate_mod, "measure", lambda _p: 1.0)
    monkeypatch.setattr(narrate_mod, "tool", lambda name: name)
    argv = ["narrate.py", "script.json", "--out-dir", "assets/audio", "--provider", "polly"]
    monkeypatch.setattr(sys, "argv", [*argv, *extra])
    return calls


def _outputs(tmp_path):
    aud = tmp_path / "assets" / "audio"
    return sorted(p.name for p in aud.iterdir()) if aud.is_dir() else []


def test_no_dirfd_refuses_before_any_speech_call(narrate_mod, monkeypatch, tmp_path, capsys):
    calls = _run(narrate_mod, monkeypatch, tmp_path, have_dirfd=False)
    with pytest.raises(SystemExit) as exc:
        narrate_mod.main()
    assert exc.value.code == 78
    assert "run the pipeline on a POSIX host" in capsys.readouterr().err
    assert calls == {"synthesize": 0, "ffmpeg": 0}
    assert _outputs(tmp_path) == []


def test_no_dirfd_silent_run_still_refuses_without_narr_json(
    narrate_mod, monkeypatch, tmp_path, capsys
):
    script = {"lines": [dict(line, dur=1.0) for line in _SCRIPT["lines"]]}
    calls = _run(narrate_mod, monkeypatch, tmp_path, have_dirfd=False, extra=["--silent"])
    (tmp_path / "script.json").write_text(json.dumps(script), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        narrate_mod.main()
    assert exc.value.code == 78
    assert "run the pipeline on a POSIX host" in capsys.readouterr().err
    assert calls == {"synthesize": 0, "ffmpeg": 0}
    assert "narr.json" not in _outputs(tmp_path)


@pytest.mark.skipif(
    not hasattr(os, "O_DIRECTORY"),
    reason="the full run writes through dir_fd, which only POSIX hosts have",
)
def test_supported_host_still_narrates_every_line(narrate_mod, monkeypatch, tmp_path):
    calls = _run(narrate_mod, monkeypatch, tmp_path, have_dirfd=True)
    assert narrate_mod.main() == 0
    assert calls == {"synthesize": 3, "ffmpeg": 5}
    assert _outputs(tmp_path) == [
        "concat.txt",
        "gap.mp3",
        "line00.mp3",
        "line01.mp3",
        "line02.mp3",
        "narr.json",
        "narration.mp3",
    ]
    narr = json.loads((tmp_path / "assets/audio/narr.json").read_text(encoding="utf-8"))
    assert [line["start"] for line in narr["lines"]] == [0.0, 1.4, 2.8]
