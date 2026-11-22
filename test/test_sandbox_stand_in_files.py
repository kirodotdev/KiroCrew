"""A masked file's stand-in can hold a caller's bytes; the real file is never written.

``extra_stand_ins`` is ``(masked file, source file)``. The namespace launcher reads the
source before any mask hides it and writes it into the stand-in it bound over the masked
file, only when the name reaches one of its own stand-ins. These tests drive the two
launcher steps directly, with no namespace, and check the plan payload carries the
request only when a spawn makes one.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from kiro_crew import sandbox_launcher_program as program
from kiro_crew import sandbox_plan


def _launch(pairs, own=()):
    return SimpleNamespace(
        stand_in_files=pairs,
        stand_in_data={},
        own_stand_ins={ident: True for ident in own},
    )


def test_the_source_is_written_into_a_stand_in_the_launcher_made(tmp_path):
    source = tmp_path / "source"
    source.write_text("value-in-words", encoding="utf-8")
    stand_in = tmp_path / "stand-in"
    stand_in.write_text("", encoding="utf-8")
    st = os.lstat(stand_in)
    launch = _launch([(str(stand_in), str(source))], own=[(st.st_dev, st.st_ino)])

    program.preread_stand_in_files(launch)
    source.unlink()
    program.restore_stand_in_files(launch)

    assert stand_in.read_text(encoding="utf-8") == "value-in-words"


def test_a_name_that_is_not_a_stand_in_is_never_written(tmp_path, capsys):
    source = tmp_path / "source"
    source.write_text("value-in-words", encoding="utf-8")
    real = tmp_path / "real-secret-file"
    real.write_text("the-real-value", encoding="utf-8")
    launch = _launch([(str(real), str(source))])

    program.preread_stand_in_files(launch)
    program.restore_stand_in_files(launch)

    assert real.read_text(encoding="utf-8") == "the-real-value"
    assert "not one of this launcher's stand-ins" in capsys.readouterr().err


def test_an_unreadable_source_leaves_the_stand_in_empty(tmp_path, capsys):
    stand_in = tmp_path / "stand-in"
    stand_in.write_text("", encoding="utf-8")
    st = os.lstat(stand_in)
    launch = _launch([(str(stand_in), str(tmp_path / "missing"))], own=[(st.st_dev, st.st_ino)])

    program.preread_stand_in_files(launch)
    program.restore_stand_in_files(launch)

    assert stand_in.read_text(encoding="utf-8") == ""
    assert "cannot read the stand-in source" in capsys.readouterr().err


def test_the_plan_payload_carries_stand_ins_only_when_a_spawn_asks():
    plain = sandbox_plan.ConfinementPlan.__dataclass_fields__
    assert "stand_ins" in plain
    request = sandbox_plan.SandboxRequest(tier="standard", extra_stand_ins=(("/a", "/b"),))
    assert request.extra_stand_ins == (("/a", "/b"),)
    assert sandbox_plan.SandboxRequest(tier="standard").extra_stand_ins == ()
