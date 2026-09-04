"""The prompt fence must judge the RESOLVED target, not the path as written.

A review round pointed at the read in ``_inline_prompt`` and proposed routing it
through the repository's guarded reader. Investigating that turned up a sharper
hole than the example given, and reproducing it first is what identified the
right fix:

    refused_by_location(link)           -> False   (the link's own path is fine)
    refused_by_location(link.resolve()) -> True    (its target is a kubeconfig)

so a symlink inside the agents directory pointing at ``~/.kube/config`` passed a
fence added specifically to refuse that file, and the read followed the link.

What this deliberately does NOT do is require the resolved path to stay under
``agents_dir``. That would also close the hole, but by breaking a supported case:
an absolute persona path outside that directory has its own passing test. Closing
a hole by removing a documented feature is not a fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .test_producer import load_build


def _kubeconfig(home):
    d = home / ".kube"
    d.mkdir(parents=True)
    p = d / "config"
    p.write_text("apiVersion: v1\nclusters: []\n", encoding="utf-8")
    return p


def test_a_symlink_to_a_kubeconfig_is_refused(tmp_path):
    """The reproduction, as a permanent test."""
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    cfg = _kubeconfig(tmp_path / "home")
    link = agents_dir / "persona.md"
    link.symlink_to(cfg)

    with pytest.raises(mod.ExportRefused) as exc:
        mod._resolve_prompt_path(f"file://{link}", agents_dir)

    # The message must name what it actually refused, or an owner debugging this
    # sees a complaint about a file that looks innocent.
    assert "credential" in str(exc.value)


def test_a_symlink_into_ssh_is_refused_too(tmp_path):
    """Not special-cased to one directory."""
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir(parents=True)
    key = ssh / "id_rsa"
    # Content is deliberately not key-shaped. The fence judges the PATH, so the
    # bytes are irrelevant to what is under test, and a real key header here
    # would trip this repository's credential scanner on every run.
    key.write_text("not a key; the fence never reads this\n", encoding="utf-8")
    link = agents_dir / "role.md"
    link.symlink_to(key)

    with pytest.raises(mod.ExportRefused):
        mod._resolve_prompt_path(f"file://{link}", agents_dir)


def test_a_legitimate_persona_outside_the_agents_dir_still_works(tmp_path):
    """The supported case this fix must not break.

    Duplicated from the sibling module on purpose: it is the constraint that
    ruled out the containment fix, so it belongs next to the reasoning.
    """
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    persona = tmp_path / "crew" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("You are the front desk.", encoding="utf-8")

    assert mod._resolve_prompt_path(f"file://{persona}", agents_dir) == persona


def test_a_symlink_into_a_pseudo_filesystem_is_refused(tmp_path):
    """The hole a reviewer found in the FIRST version of this fix.

    That version resolved the target for the two credential fences but left the
    pseudo-filesystem loop testing the path as written, so this symlink passed all
    three checks: the link is not under /proc, and /proc is not a credential
    location. The read then followed it and inlined the deploy process's own
    environment into the shipped prompt, where scan_text catches only
    credential-SHAPED text -- a secret in any other format survives.
    """
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    link = agents_dir / "persona.md"
    link.symlink_to(Path("/proc/self/environ"))

    with pytest.raises(mod.ExportRefused) as exc:
        mod._resolve_prompt_path(f"file://{link}", agents_dir)

    assert "pseudo-filesystem" in str(exc.value)


def test_MUTATION_the_pseudo_fs_check_on_the_unresolved_path(tmp_path):
    """Put the original bug back and the symlink is accepted again."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    link = agents_dir / "persona.md"
    link.symlink_to(Path("/proc/self/environ"))

    bad = load_build(mutate=("    posix = resolved.as_posix()", "    posix = path.as_posix()"))
    accepted = bad._resolve_prompt_path(f"file://{link}", agents_dir)
    assert accepted == link, "mutation did not take effect; this test proves nothing"


def test_a_symlink_to_a_legitimate_persona_still_works(tmp_path):
    """Resolving must not turn every symlink into a refusal."""
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    persona = tmp_path / "crew" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("You are the front desk.", encoding="utf-8")
    link = agents_dir / "linked.md"
    link.symlink_to(persona)

    assert mod._resolve_prompt_path(f"file://{link}", agents_dir) == link


def test_MUTATION_resolving_before_the_fence(tmp_path):
    """With the target check removed, the symlink is accepted again."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    cfg = _kubeconfig(tmp_path / "home")
    link = agents_dir / "persona.md"
    link.symlink_to(cfg)

    bad = load_build(
        mutate=(
            "if refused_by_location(resolved) or refused_by_location(path):",
            "if refused_by_location(path):",
        )
    )
    accepted = bad._resolve_prompt_path(f"file://{link}", agents_dir)
    assert accepted == link, "mutation did not take effect; this test proves nothing"
    # And it really would have been read: the link resolves to the kubeconfig.
    assert accepted.resolve() == cfg.resolve()
