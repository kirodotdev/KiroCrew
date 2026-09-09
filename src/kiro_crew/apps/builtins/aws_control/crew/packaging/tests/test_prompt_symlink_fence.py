"""The prompt fence must judge the RESOLVED target, not the path as written.

A review pass pointed at the read in ``_inline_prompt`` and proposed routing it
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

import os
import pathlib
from pathlib import Path

import pytest

from .test_producer import load_build, make_crew


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


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics")
def test_an_absolute_symlink_cycle_is_refused_not_a_traceback(tmp_path: pathlib.Path) -> None:
    """``resolve()`` raises on a cycle, and only the absolute branch reaches it.

    The relative branch runs ``_refuse_redirects_in_chain`` first, which rejects a -> b -> a
    at the first link. An absolute ``file://`` target skips that walk, so before this the
    cycle came out of the CLI as ``RuntimeError: Symlink loop from ...``.

    This is the case an earlier guard here was removed for being unable to reach. The
    removal was judged against a RELATIVE cycle test, where the chain walk answers first.
    """
    mod = load_build()
    first = tmp_path / "cycle_a.md"
    second = tmp_path / "cycle_b.md"
    first.symlink_to(second)
    second.symlink_to(first)

    src = make_crew(tmp_path / "home", prompt=f"file://{first}")
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._inline_prompt(spec, crew.name, crew.agent_spec_path.parent, [])
    assert "symlink loop" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics")
def test_a_relative_cycle_is_still_refused_by_the_chain_walk(tmp_path: pathlib.Path) -> None:
    """Pins WHICH guard answers for a relative cycle, so the two stay distinguishable.

    Without this, the new try/except could quietly become the only thing catching cycles and
    the chain walk could be moved or weakened without anything reddening.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file://cycle_a.md")
    crew = mod.resolve_crew("frontdesk", src)
    agents_dir = crew.agent_spec_path.parent
    first = agents_dir / "cycle_a.md"
    second = agents_dir / "cycle_b.md"
    first.symlink_to(second)
    second.symlink_to(first)
    spec = mod.read_agent_spec(crew)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._inline_prompt(spec, crew.name, agents_dir, [])
    assert "symlink loop" not in str(caught.value), "the chain walk should answer first"


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics")
def test_a_relative_source_still_anchors_an_in_tree_persona(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anchored walk must not depend on the SHAPE the operator typed ``--source`` in.

    ``_resolve_prompt_path`` returns an absolute path; ``agents_dir`` keeps the caller's
    shape. Comparing the two directly meant a relative ``--source`` always failed
    containment and sent an in-tree persona down the branch meant for paths outside the
    crew, which anchors at the file's own parent and checks the final component only.

    The parent swap was still refused, but by the chain walk rather than by the anchor. This
    asserts the ANCHOR choice, so the coverage cannot silently move between guards: with the
    persona in the tree, a swapped parent must be refused with the anchored walk's own
    message and not the chain walk's.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file://sub/persona.md")
    crew_probe = mod.resolve_crew("frontdesk", src)
    agents_dir_abs = crew_probe.agent_spec_path.parent
    (agents_dir_abs / "sub").mkdir()
    (agents_dir_abs / "sub" / "persona.md").write_text("in-tree persona\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    relative_source = pathlib.Path(os.path.relpath(src, tmp_path))
    assert not relative_source.is_absolute(), "the point of this test is the relative shape"

    crew = mod.resolve_crew("frontdesk", relative_source)
    agents_dir = crew.agent_spec_path.parent

    # Observe the anchor the CODE picks, not a re-derivation of it. Re-computing the
    # containment question in the test proved nothing: it passed against the broken version
    # too, because the test compared resolved paths while the code did not.
    seen: list[pathlib.Path | None] = []
    import kiro_crew.hooks as _hooks

    real_reader = _hooks.safe_read_file_bytes_nolink

    def recording_reader(raw, within_root=None, **kw):  # type: ignore[no-untyped-def]
        seen.append(pathlib.Path(within_root) if within_root else None)
        return real_reader(raw, within_root, **kw)

    monkeypatch.setattr(_hooks, "safe_read_file_bytes_nolink", recording_reader)
    spec = mod.read_agent_spec(crew)
    mod._inline_prompt(spec, crew.name, agents_dir, [])

    assert seen, "the prompt read did not happen"
    assert (
        seen[-1] == agents_dir.resolve()
    ), f"an in-tree persona must anchor at the agents directory, got {seen[-1]}"


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics")
def test_a_persona_outside_the_crew_still_anchors_at_its_own_parent(
    tmp_path: pathlib.Path,
) -> None:
    """Guards the resolved comparison from collapsing into "always anchor at agents_dir".

    Walking from ``/`` with O_NOFOLLOW would refuse any legitimate absolute persona whose
    ancestors include a symlink, which is most real installs -- so the outside case must
    keep anchoring at its own parent, and it must still be READ.
    """
    mod = load_build()
    persona = tmp_path / "personas" / "real.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("You are the front desk.\n", encoding="utf-8")

    src = make_crew(tmp_path / "home", prompt=f"file://{persona}")
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    mod._inline_prompt(spec, crew.name, crew.agent_spec_path.parent, [])

    assert "front desk" in spec["prompt"]


@pytest.mark.skipif(os.name != "posix", reason="needs hard links and dir_fd")
def test_a_hard_linked_persona_is_refused_even_when_unscannable(
    tmp_path: pathlib.Path,
) -> None:
    """A hard link is invisible to every other fence here, and content cannot cover for it.

    Not a symlink, so O_NOFOLLOW ignores it and the anchored walk sees an ordinary file. The
    location checks judge the NAME, and the name sits inside the crew. Yet the bytes belong
    to another file anywhere the operator can read.

    The persona is an OPAQUE base64 blob on purpose. A hard link to a recognisable AWS key
    is already refused by the content scanner, which is what hid this: measured, that case
    raised while this one was inlined whole. A kubeconfig certificate has exactly this
    shape, which is the reason the location checks refuse before reading at all.
    """
    mod = load_build()
    secret = tmp_path / "opaque_secret"
    secret.write_text("Zm9vYmFyYmF6cXV1eGNvcmdlZ3JhdWx0d2FsZG8=\n", encoding="utf-8")

    src = make_crew(tmp_path / "home", prompt="file://persona.md")
    crew = mod.resolve_crew("frontdesk", src)
    agents_dir = crew.agent_spec_path.parent
    os.link(secret, agents_dir / "persona.md")
    assert (agents_dir / "persona.md").stat().st_nlink == 2
    assert not (agents_dir / "persona.md").is_symlink(), "the point is that it is NOT a symlink"

    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod._inline_prompt(spec, crew.name, agents_dir, [])
    # The shared reader owns this verdict, so the message is its single refusal rather
    # than a local sentence about link counts.
    assert "file-read guard" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="needs hard links and dir_fd")
def test_an_ordinary_persona_with_one_link_still_reads(tmp_path: pathlib.Path) -> None:
    """Guards the link-count refusal from rejecting every persona.

    Without this, refusing on ``st_nlink >= 1`` would pass the test above while making the
    feature unusable -- every regular file has one link.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file://persona.md")
    crew = mod.resolve_crew("frontdesk", src)
    agents_dir = crew.agent_spec_path.parent
    (agents_dir / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")

    spec = mod.read_agent_spec(crew)
    mod._inline_prompt(spec, crew.name, agents_dir, [])
    assert "front desk" in spec["prompt"]


@pytest.mark.skipif(os.name != "posix", reason="needs hard links and dir_fd")
def test_the_shared_reader_gets_the_anchor_as_its_containment_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``within_root`` is what makes the opened descriptor's identity mean anything.

    The shared reader reads the OPENED descriptor's real path back and requires it inside
    ``within_root``. Called without that argument every other check still runs, but the one
    that catches a component swapped after the fences does not -- so passing it IS the
    protection, and that is what this pins. It replaces a test of a local ``lstat`` helper
    that the shared reader made redundant.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file://persona.md")
    crew = mod.resolve_crew("frontdesk", src)
    agents_dir = crew.agent_spec_path.parent
    (agents_dir / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")

    seen: list[tuple[str, str | None]] = []
    import kiro_crew.hooks as _hooks

    real_reader = _hooks.safe_read_file_bytes_nolink

    def recording(raw, within_root=None, **kw):  # type: ignore[no-untyped-def]
        seen.append((raw, within_root))
        return real_reader(raw, within_root, **kw)

    monkeypatch.setattr(_hooks, "safe_read_file_bytes_nolink", recording)
    spec = mod.read_agent_spec(crew)
    mod._inline_prompt(spec, crew.name, agents_dir, [])

    assert seen, "the prompt read did not go through the shared reader"
    _, within = seen[-1]
    assert within is not None, "the reader was called without a containment root"
    assert pathlib.Path(within) == agents_dir.resolve()


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics")
def test_the_containment_root_is_resolved_once_not_per_check(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each extra ``.resolve()`` of the agents directory is another chance to follow a link.

    Measured before this: with the directory reached through a name an attacker controls, a
    second resolution at the read site follows the NEW target, so the reader is handed a
    containment root inside the attacker's tree -- where the escaping file IS contained and
    the check passes. The shared reader returned ``ATTACKER BYTES`` that way and None when
    given the value resolved once.

    Counts resolutions rather than asserting a message, because the number of views of the
    tree is the property: one cannot disagree with itself.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file://persona.md")
    crew = mod.resolve_crew("frontdesk", src)
    agents_dir = crew.agent_spec_path.parent
    (agents_dir / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")

    resolutions: list[str] = []
    real_resolve = pathlib.Path.resolve

    def counting_resolve(self, *a, **kw):  # type: ignore[no-untyped-def]
        if self == agents_dir:
            resolutions.append(str(self))
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "resolve", counting_resolve)
    spec = mod.read_agent_spec(crew)
    mod._inline_prompt(spec, crew.name, agents_dir, [])

    # One in _resolve_prompt_path, one in _inline_prompt. The remaining pair is the window
    # this change deliberately does not close, and it is named in the code comment; a THIRD
    # resolution means a check started re-walking the name again.
    assert len(resolutions) <= 2, (
        f"the agents directory was resolved {len(resolutions)} times; each extra walk can "
        f"follow a link planted since the last one"
    )
