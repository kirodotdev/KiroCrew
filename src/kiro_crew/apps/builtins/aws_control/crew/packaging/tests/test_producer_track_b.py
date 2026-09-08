"""Track B pins for the packager: the sensitive-path fence and the plan's
``include`` truthiness.

Both are mutation-tested against ``packaging/build.py`` through the same
exec-load harness the rest of this suite uses (``test_producer.load_build``): the
guard's source is disabled in a throwaway copy of the module and the same
scenario is shown to leak, so each assertion proves the guard is load-bearing
rather than decorative. See that module's ``load_build`` docstring for why a
module-level guard has to be mutation-tested by compiling a variant of the source.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from .test_producer import load_build

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


# ---------------------------------------------------------------------------
# Finding 2: prompt expansion must not READ a path the repo fences off.
#
# The pin asserts the READ NEVER HAPPENS, not merely that the export is refused:
# a kubeconfig's ``client-certificate-data`` is base64 and may match no
# credential pattern, so relying on the post-read ``scan_text`` would embed it.
# ---------------------------------------------------------------------------
def _write_kubeconfig(home: Path) -> Path:
    kube = home / ".kube"
    kube.mkdir(parents=True)
    cfg = kube / "config"
    # A kubeconfig whose secret is base64 -- the shape the content scanner cannot
    # be trusted to recognise, which is why the location must be judged first.
    cfg.write_text(
        "apiVersion: v1\nusers:\n- user:\n    client-certificate-data: "
        "TFMwdExTMUNSVWRKVGlCRFJWSlVTVVpKUTBGVVJTMHRMUzB0Q2c9PQ==\n",
        encoding="utf-8",
    )
    return cfg


# ---------------------------------------------------------------------------
# Finding 3A: a plan whose ``include`` is the STRING "false" must not select.
# ---------------------------------------------------------------------------
def _plan_dict(include_value):
    return {
        "plan_version": 1,
        "crew": "frontdesk",
        "reviewed_by": "someone",
        "reviewed_at": "2026-01-01",
        "skills": [{"id": "faq", "include": include_value, "sha256": "abc"}],
        "mcp": [],
    }


def _write_plan(tmp_path: Path, include_value) -> Path:
    import json

    p = tmp_path / "plan.json"
    p.write_text(json.dumps(_plan_dict(include_value)), encoding="utf-8")
    return p


@_posix_only
def test_string_false_in_a_plan_is_refused_not_selected(tmp_path):
    mod = load_build()
    plan_path = _write_plan(tmp_path, "false")
    with pytest.raises(mod.ExportRefused, match="non-boolean 'include'"):
        mod.read_plan(plan_path)


@_posix_only
def test_a_real_boolean_include_still_reads(tmp_path):
    mod = load_build()
    plan = mod.read_plan(_write_plan(tmp_path, True))
    assert plan.selections["skills"]["faq"] is True
    plan = mod.read_plan(_write_plan(tmp_path, False))
    assert plan.selections["skills"]["faq"] is False


@_posix_only
def test_MUTATION_plan_include_truthiness(tmp_path):
    """Restore the old ``bool(...)`` coercion and the string "false" SELECTS.

    With the strict parser mutated back to ``bool()``, ``bool("false")`` is True,
    so an item the reviewer wrote off with the string "false" ships in the bundle.
    """
    plan_path = _write_plan(tmp_path, "false")
    bad = load_build(
        mutate=(
            'sel[cid] = _require_plan_include(kind, cid, entry.get("include", False))',
            'sel[cid] = bool(entry.get("include", False))',
        )
    )
    plan = bad.read_plan(plan_path)
    assert (
        plan.selections["skills"]["faq"] is True
    ), "mutation must let the string 'false' select the item"
