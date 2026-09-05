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

from pathlib import Path

import pytest

from .test_producer import load_build


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


def test_a_file_uri_into_a_credential_dir_is_refused_before_reading(tmp_path):
    """A ``file://`` prompt pointing at ``~/.kube/config`` refuses without reading.

    ``config`` is an innocent basename, so ``refused_by_name`` does not catch it;
    the directory fence (``refused_by_location``) does.
    """
    mod = load_build()
    home = tmp_path / "home"
    cfg = _write_kubeconfig(home)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    # Instrument the read so a fetch of the file is unmistakably visible.
    reads: list[str] = []
    real_read = mod._read_text

    def _recording_read(p):
        # A named function, not ``lambda p: (reads.append(...), real_read(p))[1]``:
        # that spelling smuggles a None-returning call into an expression, which
        # mypy rejects (func-returns-value) and a reader has to decode.
        reads.append(str(p))
        return real_read(p)

    # ``setattr``, because ``load_build()`` hands back a throwaway ``ModuleType``
    # exec'd from source: the attribute is genuinely dynamic, and spelling it as a
    # plain assignment only makes mypy guess at a module it cannot see.
    setattr(mod, "_read_text", _recording_read)

    with pytest.raises(mod.ExportRefused, match="credential directory"):
        mod._resolve_prompt_path(f"file://{cfg}", agents_dir)

    assert reads == [], f"the sensitive file was read despite the fence: {reads}"


def test_the_fence_also_covers_ssh_aws_gnupg_docker(tmp_path):
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    for part, leaf in (
        (".ssh", "id_ed25519_extra"),  # a name refused_by_name would MISS
        (".aws", "credentials.bak"),
        (".gnupg", "trustdb"),
        (".docker", "cfg"),
    ):
        d = tmp_path / "h" / part
        d.mkdir(parents=True)
        f = d / leaf
        f.write_text("x", encoding="utf-8")
        with pytest.raises(mod.ExportRefused, match="credential directory"):
            mod._resolve_prompt_path(f"file://{f}", agents_dir)


def test_a_normal_persona_path_is_not_refused(tmp_path):
    """The fence must not refuse a legitimate persona file under the crew home."""
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    persona = tmp_path / "crew" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("You are the front desk.", encoding="utf-8")
    resolved = mod._resolve_prompt_path(f"file://{persona}", agents_dir)
    assert resolved == persona


def test_MUTATION_sensitive_path_fence(tmp_path):
    """Disable the location fence and the kubeconfig is now READ.

    This is the reddening the fix exists to prevent: with the guard off,
    ``_resolve_prompt_path`` returns the path and a caller reads it.
    """
    home = tmp_path / "home"
    cfg = _write_kubeconfig(home)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    bad = load_build(
        # Anchored to the CURRENT guard line. It gained a resolved-target check
        # after a symlink was found to walk past the link-only form, so the old
        # anchor no longer exists -- see test_prompt_symlink_fence.py.
        mutate=(
            "if refused_by_location(resolved) or refused_by_location(path):",
            "if False:",
        )
    )
    # With the fence disabled, the path is returned and its bytes are readable --
    # exactly the "read the file at all is the wrong shape" the fix removes.
    resolved = bad._resolve_prompt_path(f"file://{cfg}", agents_dir)
    assert resolved == cfg
    assert bad._read_text(resolved) is not None, "mutation must let the file be read"


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


def test_string_false_in_a_plan_is_refused_not_selected(tmp_path):
    mod = load_build()
    plan_path = _write_plan(tmp_path, "false")
    with pytest.raises(mod.ExportRefused, match="non-boolean 'include'"):
        mod.read_plan(plan_path)


def test_a_real_boolean_include_still_reads(tmp_path):
    mod = load_build()
    plan = mod.read_plan(_write_plan(tmp_path, True))
    assert plan.selections["skills"]["faq"] is True
    plan = mod.read_plan(_write_plan(tmp_path, False))
    assert plan.selections["skills"]["faq"] is False


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
