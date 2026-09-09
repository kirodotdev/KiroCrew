"""An absolute persona OUTSIDE the agents directory is a supported case.

``_resolve_prompt_path`` says so in its own body: "Containment under agents_dir is
deliberately NOT required: an absolute persona path outside that directory is a supported
case with its own test."

The parent-swap fix broke it. That fix reads the prompt through a descendant-wise opener
anchored at a trusted root, and it passed ``agents_dir`` unconditionally -- so an absolute
path that cannot be relativized to ``agents_dir`` aborted the whole bundle with "is not
under the agents directory". Reproduced before this suite existed.

The anchor is now derived from the path: ``agents_dir`` for a prompt inside it, the path's
own parent otherwise. The two buy different things on purpose, and both halves are pinned
here, because a fix that quietly widened the protected set would be the same defect in the
other direction.
"""

from __future__ import annotations

import os

import pytest

from .test_producer import load_build, make_crew


def _build(mod, crew, out):
    spec = mod.read_agent_spec(crew)
    return mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


@pytest.mark.skipif(
    os.name != "posix",
    reason="drives the builder end to end; the builder is POSIX-only until an atomic no-follow primitive lands",
)
def test_an_absolute_prompt_outside_the_agents_dir_still_inlines(tmp_path):
    mod = load_build()
    persona = tmp_path / "personas" / "frontdesk.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("You are the front desk.\n", encoding="utf-8")
    crew = mod.resolve_crew("frontdesk", make_crew(tmp_path / "home", prompt=f"file://{persona}"))

    _build(mod, crew, tmp_path / "bundle")
    spec = (tmp_path / "bundle" / "agent.json").read_text(encoding="utf-8")
    assert "front desk" in spec, "the supported external persona was not inlined"


@pytest.mark.skipif(
    os.name != "posix",
    reason="drives the builder end to end; the builder is POSIX-only until an atomic no-follow primitive lands",
)
def test_a_relative_prompt_inside_the_agents_dir_still_inlines(tmp_path):
    """The common case must not regress while fixing the uncommon one."""
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file://persona.md")
    # A relative URI resolves against the AGENTS dir (``<home>/agents``), which is where
    # make_crew puts the spec, not against the crew home.
    (src / "agents" / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")
    crew = mod.resolve_crew("frontdesk", src)

    _build(mod, crew, tmp_path / "bundle")
    assert "front desk" in (tmp_path / "bundle" / "agent.json").read_text(encoding="utf-8")


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"),
    reason="the per-component opener needs dir_fd",
)
def test_a_swapped_parent_INSIDE_the_agents_dir_is_still_refused(tmp_path):
    """The half the anchor exists for. Widening it everywhere would lose this.

    The agents directory is writable by the agent, so a swapped parent there is a live
    attack: the leaf keeps its name, the link points somewhere else, and a single
    final-component ``O_NOFOLLOW`` sees nothing wrong.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file://sub/persona.md")
    agents = src / "agents"
    (agents / "sub").mkdir(parents=True, exist_ok=True)
    (agents / "sub" / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")
    secret = tmp_path / "secrets"
    secret.mkdir()
    (secret / "persona.md").write_text("PRIVATE-KEY-MATERIAL\n", encoding="utf-8")
    crew = mod.resolve_crew("frontdesk", src)

    os.rename(agents / "sub", agents / "sub.real")
    os.symlink(secret, agents / "sub")

    with pytest.raises(mod.ExportRefused):
        _build(mod, crew, tmp_path / "bundle")

    out = tmp_path / "bundle" / "agent.json"
    if out.exists():
        assert "PRIVATE-KEY" not in out.read_text(encoding="utf-8"), "the swap leaked"


@pytest.mark.skipif(
    os.name != "posix",
    reason="drives the builder end to end; the builder is POSIX-only until an atomic no-follow primitive lands",
)
def test_the_anchor_passed_to_the_reader_is_the_validated_root(tmp_path, monkeypatch):
    """Pins WHICH anchor is chosen, because no behavioural test here distinguishes them.

    Measured: forcing the anchor to ``path.parent`` unconditionally leaves this file AND
    ``test_prompt_parent_swap.py`` fully green, because that suite calls
    ``_read_text_nofollow`` directly with an explicit root and never exercises the choice,
    while the swap this file stages is already refused earlier by
    ``_resolve_prompt_path``'s containment check.

    So the decision is asserted directly rather than through a behaviour that cannot see
    it. Anchoring inside the writable agents directory is what gives the per-component
    ``O_NOFOLLOW`` anything to protect; silently widening it to the leaf's own parent would
    keep every test green and quietly drop that.
    """
    mod = load_build()
    seen: list[tuple[str, str | None]] = []
    import kiro_crew.hooks as _hooks

    real_reader = _hooks.safe_read_file_bytes_nolink

    def spy(raw, within_root=None, **kw):  # type: ignore[no-untyped-def]
        seen.append((str(raw), str(within_root) if within_root else None))
        return real_reader(raw, within_root, **kw)

    # The prompt read goes through the shared guard, so the anchor is observable as that
    # reader's containment root rather than as an argument to a local helper.
    monkeypatch.setattr(_hooks, "safe_read_file_bytes_nolink", spy)

    # inside: anchor must be the agents dir, not the leaf's parent
    src = make_crew(tmp_path / "in" / "home", prompt="file://sub/persona.md")
    (src / "agents" / "sub").mkdir(parents=True, exist_ok=True)
    (src / "agents" / "sub" / "persona.md").write_text("inside\n", encoding="utf-8")
    _build(mod, mod.resolve_crew("frontdesk", src), tmp_path / "in" / "bundle")
    path_in, root_in = seen[-1]
    assert root_in == str(src / "agents"), f"expected the agents dir as anchor, got {root_in}"

    # outside: anchor must fall back to the leaf's parent, or the build aborts
    persona = tmp_path / "out" / "personas" / "frontdesk.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("outside\n", encoding="utf-8")
    src2 = make_crew(tmp_path / "out" / "home", prompt=f"file://{persona}")
    _build(mod, mod.resolve_crew("frontdesk", src2), tmp_path / "out" / "bundle")
    _, root_out = seen[-1]
    assert root_out == str(persona.parent), f"expected the leaf's parent as anchor, got {root_out}"
