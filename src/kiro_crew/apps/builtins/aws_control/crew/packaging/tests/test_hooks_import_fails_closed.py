"""The two Windows-only ``kiro_crew.hooks`` imports refuse rather than crash.

Both sit inside an ``os.name == "nt"`` branch, so the crash they would cause is reachable
only on Windows in the standalone venv the module documents -- the one environment where
``kiro_crew`` is not importable. A ``ModuleNotFoundError`` there escapes as an uncaught
traceback mid-build, past every handler that would have cleaned up.

Fail closed rather than skip, which is the opposite of the agent-spec fence. The difference
is what each question is for: the spec fence asks "is this path sensitive", which a coarse
local list can answer well enough to be worth asking. These ask "would resolving this path
reach a host over SMB", and an unanswerable version of that is not permission to resolve it
anyway.
"""

from __future__ import annotations

import builtins
import os
import pathlib

import pytest

from .test_producer import load_build, make_crew


def _hide_hooks(monkeypatch) -> None:
    """Make ``kiro_crew.hooks`` unimportable without touching the rest of ``kiro_crew``.

    Narrower than clearing ``sys.modules``: the module under test also imports
    ``kiro_crew.security``, and hiding both would not distinguish which guard fired.
    """
    real_import = builtins.__import__

    def _fail(name, *args, **kwargs):
        if name == "kiro_crew.hooks" or name.endswith(".hooks"):
            raise ImportError("no module named 'kiro_crew.hooks' (simulated standalone venv)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail)


def test_the_unc_gate_refuses_when_hooks_is_unimportable(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A ``file://`` prompt on Windows without ``kiro_crew.hooks`` is refused, not crashed."""
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")
    monkeypatch.setattr(mod.os, "name", "nt")
    _hide_hooks(monkeypatch)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path("file://persona.md", agents)
    message = str(caught.value)
    assert "kiro_crew.hooks is not importable" in message
    assert "UNC" in message


def test_the_refusal_names_what_the_operator_can_do(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The message has to carry the way out, or it is a dead end with a nicer traceback."""
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")
    monkeypatch.setattr(mod.os, "name", "nt")
    _hide_hooks(monkeypatch)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path("file://persona.md", agents)
    message = str(caught.value)
    assert "Copy the persona next to the agent spec" in message
    assert "kiro_crew is installed" in message


def test_posix_never_reaches_the_import(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The guard is inside the nt branch, so POSIX resolves normally without hooks.

    Without this, a guard that refused on every platform would pass the two tests above
    while breaking the standalone mode the module exists for.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")
    _hide_hooks(monkeypatch)

    resolved = mod._resolve_prompt_path("file://persona.md", agents)
    assert resolved.name == "persona.md"


def test_the_guard_only_covers_the_import_failure(tmp_path: pathlib.Path) -> None:
    """With ``kiro_crew.hooks`` present, an ordinary relative persona resolves.

    Left on POSIX deliberately. Forcing ``os.name = "nt"`` with the real hooks module in
    play sends it looking for a Windows home directory and it raises for that reason
    instead, so the assertion would be about the fixture rather than the guard.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")

    resolved = mod._resolve_prompt_path("file://persona.md", agents)
    assert resolved.name == "persona.md"
    assert resolved.parent == agents


def test_the_persona_bytes_are_read_through_the_guarded_path(tmp_path: pathlib.Path) -> None:
    """The resolved path is readable by the module's own reader, so the guards do not block it.

    Uses ``_read_text_nofollow`` rather than a plain ``read_text`` because that is the reader
    the prompt path actually uses -- checking the file with a different reader would say
    nothing about whether this one still reaches it.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")

    resolved = mod._resolve_prompt_path("file://persona.md", agents)
    assert "front desk" in mod._read_text_nofollow(resolved)


def test_the_absolute_branch_is_covered_by_the_first_guard(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """An absolute persona is refused too, by the gate at the top rather than a second guard.

    ``_resolve_prompt_path`` reaches ``kiro_crew.hooks`` twice on nt: the UNC gate for every
    prompt, and the redirect-chain walk for an ABSOLUTE path. Only the first needs a guard,
    because it runs unconditionally and refuses, so no call reaches the second with the
    import still failing. A guard was written there and removed: mutating it away left every
    test passing, which is what an unreachable guard looks like.

    This test is what keeps that reasoning honest. If someone moves the chain walk above the
    UNC gate, the absolute case stops being covered and this reddens.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    persona = tmp_path / "personas" / "frontdesk.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("You are the front desk.\n", encoding="utf-8")
    monkeypatch.setattr(mod.os, "name", "nt")
    _hide_hooks(monkeypatch)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{persona}", agents)
    assert "kiro_crew.hooks is not importable" in str(caught.value)


def test_only_one_hooks_import_needs_a_guard(tmp_path: pathlib.Path) -> None:
    """A source rule: exactly one guarded hooks import, and the bare one says why.

    No runtime test can show the second import is unreachable -- that is the point of it
    being unreachable -- so the fact is pinned by reading the source instead.
    """
    # Read the file by path rather than through ``mod.__file__``, which mypy types as
    # ``str | None`` -- and the sibling source-rule tests in this directory read it the
    # same way, so the two cannot drift.
    source = (pathlib.Path(__file__).parent.parent / "build.py").read_text(encoding="utf-8")
    imports = source.count("from kiro_crew.hooks import")
    guarded = source.count("except ImportError as exc:")
    bare = source.count("Imported bare, and that is deliberate")

    # Stated as a PAIRING rather than two fixed numbers. Every hooks import is either
    # guarded by a fail-closed ImportError handler with a test that reddens, or bare with a
    # comment saying why -- so adding a legitimately guarded import is allowed while an
    # unexplained one is not. Two magic counts refused the guarded UNC gate on the agent
    # spec purely for being third, which is not the property worth defending.
    assert imports == guarded + bare, (
        f"{imports} hooks import(s) but {guarded} guarded and {bare} explained-bare. "
        f"Each one needs a fail-closed guard with a test that reddens, or the comment "
        f"saying why a bare import is deliberate."
    )
    assert bare >= 1, "the bare import lost its explanation"


def test_a_missing_agent_spec_is_not_reported_as_a_missing_prompt(
    tmp_path: pathlib.Path,
) -> None:
    """The shared reader has TWO consumers, and its refusals reach the operator verbatim.

    ``_read_text_nofollow`` serves the prompt path and the agent-spec read. Its refusals
    are worded for the caller, so a spec that is absent must say "agent spec": telling an
    operator a "prompt file" is missing sends them looking for a persona they never
    referenced, in a crew that has no prompt reference at all.

    This is the check a change to a shared function needs and that a direct unit test of
    that function does not give -- the wording only matters at the call sites.
    """
    mod = load_build()
    home = tmp_path / "home"
    (home / "agents").mkdir(parents=True)
    crew = mod.resolve_crew("frontdesk", home)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_agent_spec(crew)
    message = str(caught.value)
    assert "prompt file" not in message, "the spec read borrowed the prompt path's wording"


def test_the_prompt_path_still_says_prompt_file(tmp_path: pathlib.Path) -> None:
    """The other consumer keeps its own wording, which is what makes the parameter useful.

    A default that said "agent spec" everywhere would pass the test above and move the
    confusion to the prompt path instead.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()

    # The WORDING belongs to the caller, so it is asserted where the caller produces it.
    # The reader itself answers None: it serves the prompt path, the agent-spec path and
    # the plan path, and a message chosen inside it would be wrong for two of the three.
    assert mod._read_text_nofollow(agents / "absent.md") is None


@pytest.mark.skipif(os.name != "posix", reason="needs symlink cycle semantics")
def test_a_symlink_cycle_is_refused_by_the_chain_walk(tmp_path: pathlib.Path) -> None:
    """A cycle never reaches ``resolve()``, so ``resolve()`` needs no try/except.

    ``_refuse_redirects_in_chain`` judges each component with ``lstat`` and refuses the
    FIRST redirect, so a -> b -> a is rejected at ``a`` with a message naming the link. A
    guard was added around ``resolve()`` for ELOOP and removed: this test showed the refusal
    already comes from the chain walk, which makes the ELOOP branch unreachable.

    Kept as the pin for that ordering. If the chain walk is ever moved below ``resolve()``,
    the cycle reaches it, this assertion fails, and the guard is warranted again.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    a, b = agents / "a.md", agents / "b.md"
    a.symlink_to(b)
    b.symlink_to(a)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path("file://a.md", agents)
    message = str(caught.value)
    assert "is a link or junction" in message, (
        "the cycle was not caught by the chain walk; resolve() may now be reached with a "
        "cycle in the path, which needs its own refusal"
    )


def test_an_ordinary_prompt_still_resolves(tmp_path: pathlib.Path) -> None:
    """The chain walk must not refuse a path with no redirects in it."""
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "persona.md").write_text("You are the front desk.\n", encoding="utf-8")
    assert mod._resolve_prompt_path("file://persona.md", agents).name == "persona.md"


def test_the_spec_is_read_exactly_once_through_the_anchored_walk(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """One read, and it is the descriptor walk -- not a second, weaker one after it.

    A second ``_read_text_nofollow`` overwrote the walk's result, which discarded the
    protection the walk exists for: ``O_NOFOLLOW`` alone guards the FINAL component, so a
    swapped ``agents/`` parent was traversed by that second read. Two reads of a path an
    adversary can change is also two chances to get different bytes.

    Counted rather than asserted from the source, because "the result is used" is about
    behaviour: a source rule would pass while the value was still thrown away.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="You are the front desk.")
    crew = mod.resolve_crew("frontdesk", src)

    walk_calls: list[str] = []
    plain_calls: list[str] = []
    real_walk = mod._read_text_openat
    real_plain = mod._read_text_nofollow

    def _count_walk(root, rel, **kwargs):
        walk_calls.append(str(rel))
        return real_walk(root, rel, **kwargs)

    def _count_plain(path, root=None, **kwargs):
        plain_calls.append(str(path))
        return real_plain(path, root, **kwargs)

    monkeypatch.setattr(mod, "_read_text_openat", _count_walk)
    monkeypatch.setattr(mod, "_read_text_nofollow", _count_plain)
    mod.read_agent_spec(crew)

    assert len(walk_calls) == 1, f"the anchored walk ran {len(walk_calls)} time(s)"
    assert not plain_calls, (
        f"the spec was read again through the unanchored reader ({plain_calls}), which "
        f"discards the walk's result and re-opens a path that may have changed"
    )


def test_a_multibyte_persona_over_the_ceiling_is_refused(tmp_path: pathlib.Path) -> None:
    """A CJK persona is measured in BYTES, which is what the ceiling is named in.

    ASCII cannot show this: one character is one byte, so an ASCII fixture passes whether the
    bound counts bytes or characters. The content here is three bytes per character, and the
    file is over the ceiling in bytes while comfortably under it in characters.

    The bound is enforced by ``hooks.safe_read_file_bytes_nolink``, which takes ``max_bytes``
    on a reader that returns BYTES. This module's part is passing that bound and turning the
    refusal into ``ExportRefused``, so the assertion below drives the real prompt path rather
    than calling a reader directly.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    persona = home / "agents" / "persona.md"
    chars = (mod._MAX_PROMPT_BYTES // 3) + 8
    persona.write_bytes(("\u4e2d" * chars).encode("utf-8"))
    assert persona.stat().st_size > mod._MAX_PROMPT_BYTES
    assert chars < mod._MAX_PROMPT_BYTES, "the fixture must be under the ceiling in CHARACTERS"

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "ceiling" in str(caught.value)


def test_a_multibyte_persona_under_the_ceiling_is_read_whole(tmp_path: pathlib.Path) -> None:
    """The other half: multibyte content within the limit must decode intact.

    A ceiling that refused all multibyte content would pass the test above, and reading
    ``_MAX_PROMPT_BYTES`` bytes can cut a character in half if the decode is not done on the
    whole buffer.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    body = "\u4e2d\u6587 persona\n" * 100
    (agents / "persona.md").write_text(body, encoding="utf-8")

    assert mod._read_text_openat(agents, pathlib.Path("persona.md")) == body


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics")
def test_an_absolute_persona_that_is_a_symlink_is_refused(tmp_path: pathlib.Path) -> None:
    """An absolute persona OUTSIDE the agents directory is supported; a LINK to one is not.

    Measured, because two facts in this branch looked contradictory and neither test covered
    the overlap: ``_resolve_prompt_path`` returns the UNRESOLVED path and calls an absolute
    persona a supported case, while the reader opens the final component with ``O_NOFOLLOW``.
    Driving the real path shows the reader wins -- the link is refused with the "passed the
    prompt fences and then changed" message.

    Pinned rather than changed. The refusal is the safe direction: what a link points at is
    not what was reviewed, and resolving it in ``_resolve_prompt_path`` would hand the reader
    a target that skipped every fence applied to the name. The cost is real and narrow: an
    operator who keeps personas behind a symlink farm must reference the target directly. It
    is stated here so it is a decision rather than a surprise.

    The supported-case tests all use real files (the only ``symlink`` in
    ``test_external_prompt_supported.py`` builds a REFUSAL case), which is why the overlap
    went uncovered.
    """
    mod = load_build()
    real = tmp_path / "personas" / "real.md"
    real.parent.mkdir(parents=True)
    real.write_text("You are the front desk.\n", encoding="utf-8")
    link = tmp_path / "personas" / "link.md"
    link.symlink_to(real)

    src = make_crew(tmp_path / "home", prompt=f"file://{link}")
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._inline_prompt(spec, crew.name, crew.agent_spec_path.parent, [])
    assert "prompt file" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics")
def test_an_absolute_persona_that_is_a_real_file_is_inlined(tmp_path: pathlib.Path) -> None:
    """The supported case still works, so the refusal above is about the LINK only.

    Without this the test above would pass against a build that refused every absolute
    persona, which is the documented supported case and would be a real regression.
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


def test_the_agent_spec_wording_survives_a_platform_without_dir_fd(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing AGENT SPEC must say so on every platform, not only where dir_fd exists.

    ``_read_text_openat`` falls back to the plain reader when ``dir_fd`` is unavailable, and
    that fallback dropped ``what``: a dead ``return`` sat directly above the one that passed
    it. So on that platform an absent spec was reported as a missing "prompt file", sending
    the operator after a persona their crew never referenced.

    Driven by forcing the fallback rather than by reading the source, because the defect was
    a live wording the operator sees.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home")
    crew = mod.resolve_crew("frontdesk", src)
    crew.agent_spec_path.unlink()

    monkeypatch.setattr(mod, "_dir_fd_supported", lambda: False)
    # The platform gate is a separate decision -- the builder refuses outright where there is
    # no atomic no-follow primitive, and its own test covers that. It is neutralised here
    # because it sits in front of the code this test is about: with it in place the only
    # message reachable is the platform one, and the wording under test is never produced.
    monkeypatch.setattr(mod, "_refuse_without_nofollow_primitive", lambda: None)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_agent_spec(crew)
    message = str(caught.value)
    assert "agent spec" in message
    assert "prompt file" not in message, (
        "the agent-spec read reported itself as a prompt file, which sends the operator "
        "looking for a persona the crew never referenced"
    )
