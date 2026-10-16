"""Helpers shared by the sensitive-source test modules.

``_build`` drives one curated build of the ``frontdesk`` crew end to end: resolve, enumerate,
sign a plan, verify it and build the bundle. ``_posix_only`` skips a test off POSIX, where the
builder refuses for lack of an atomic no-follow primitive. Each
``test_sensitive_source_and_report_identity*.py`` module imports what it uses from here.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from .test_producer import sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


def _build(mod, home: pathlib.Path, out: pathlib.Path, select):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, out.parent, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, out)
