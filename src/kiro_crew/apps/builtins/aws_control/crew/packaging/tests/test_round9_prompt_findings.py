"""Two findings on the prompt reader, both raised by GPT and Opus independently.

V1 the pre-resolution walk ran only on the RELATIVE branch, so ``file:///abs/path`` reached
   ``resolve()`` with nothing having looked at its components. What that costs is Windows-only
   and narrow: resolving a reparse point that names a SHARE is the outbound SMB probe with its
   NTLM exchange, and the UNC gate above only sees a share written literally in the target.

   The first attempt walked the absolute path and refused every redirect. That reddened
   ``test_a_symlink_to_a_legitimate_persona_still_works`` and four more, because a symlink at
   the prompt path is a SUPPORTED case -- the design permits a persona outside the agents
   directory and protects it by checking the RESOLVED target against the sensitive-path fence.
   So the refusal is scoped to the one thing the target check cannot catch: a redirect that
   names a share, read with ``readlink``, which does not traverse.

V2 the read was unbounded. The prompt is inlined into ``agent.json``, so its bytes are held in
   memory, hashed and shipped -- and the path comes from the crew's agent spec, which makes the
   size someone else's choice.
"""

from __future__ import annotations

import errno
import importlib
import os
import pathlib

import pytest

from .test_producer import load_build, make_crew


# ---------------------------------------------------------------------------
# V1
# ---------------------------------------------------------------------------
def test_a_symlinked_persona_outside_the_agents_dir_still_works(tmp_path: pathlib.Path) -> None:
    """The supported case, pinned again here because a fix already broke it once.

    Kept beside the new refusal rather than left in its own file: the two are one decision, and
    a reader deciding whether to tighten the absolute branch needs to see the cost in the same
    place as the benefit.
    """
    mod = load_build()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    persona = tmp_path / "shared" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_bytes(b"You are the front desk.\n")
    link = agents_dir / "linked.md"
    link.symlink_to(persona)

    assert mod._resolve_prompt_path(f"file://{link}", agents_dir) == link


def test_the_absolute_branch_refuses_a_redirect_naming_a_share(tmp_path: pathlib.Path) -> None:
    """The gap the walk was added for, tested through the nt branch.

    ``os.name`` is mutated rather than the test being skipped, because the branch cannot be
    reached on this host and skipping it would leave the fix with no test at all -- which is
    how the previous version of this fence shipped ineffective.
    """
    mod = load_build(mutate=('    elif os.name == "nt":', "    elif True:"))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # A link whose TARGET has UNC shape. The target need not exist: refusing before resolution
    # is the point, and a dangling link proves nothing was resolved.
    link = agents_dir / "persona.md"
    link.symlink_to("//attacker-host/share/persona.md")

    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{link}", agents_dir)
    assert "network share" in str(caught.value)
    assert "attacker-host" in str(caught.value)


def test_an_ordinary_absolute_link_is_not_refused_by_that_check(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: on the same branch, a link to a local file must pass.

    Without this the refusal above would be satisfied by banning every link, which is exactly
    the over-broad version that had to be backed out.
    """
    mod = load_build(mutate=('    elif os.name == "nt":', "    elif True:"))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    persona = tmp_path / "shared" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_bytes(b"a local persona\n")
    link = agents_dir / "persona.md"
    link.symlink_to(persona)

    assert mod._resolve_prompt_path(f"file://{link}", agents_dir) == link


# ---------------------------------------------------------------------------
# V2
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    os.name != "posix",
    reason="drives the builder end to end; the builder is POSIX-only until an atomic no-follow primitive lands, so this behaviour is verified on POSIX",
)
def test_an_oversized_prompt_is_refused(tmp_path: pathlib.Path) -> None:
    """Refused rather than read, because the read is what allocates."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    big = home / "agents" / "persona.md"
    big.write_bytes(b"x" * (mod._MAX_PROMPT_BYTES + 1))

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "exceeds the" in str(caught.value)


@pytest.mark.skipif(
    os.name != "posix",
    reason="drives the builder end to end; the builder is POSIX-only until an atomic no-follow primitive lands, so this behaviour is verified on POSIX",
)
def test_a_prompt_at_the_ceiling_is_read(tmp_path: pathlib.Path) -> None:
    """The bound is inclusive, so the ceiling is a size and not an off-by-one."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    body = b"y" * mod._MAX_PROMPT_BYTES
    (home / "agents" / "persona.md").write_bytes(body)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert result.spec["prompt"] == body.decode("utf-8")


@pytest.mark.skipif(
    os.name != "posix",
    reason="drives the builder end to end; the builder is POSIX-only until an atomic no-follow primitive lands, so this behaviour is verified on POSIX",
)
def test_the_bound_is_handed_to_the_shared_guard(tmp_path: pathlib.Path, monkeypatch) -> None:
    """The ceiling reaches the guard as ``max_bytes``, and its refusal becomes ExportRefused.

    ``hooks.safe_read_file_bytes_nolink`` owns the bound, so this module's part is the value
    it hands over, and that value is the observable thing here. It is worth observing because
    handing over no bound leaves every behavioural test green while the allocation runs
    unbounded: the prompt is inlined into ``agent.json``, so its bytes are held in memory and
    hashed, and the size is named by the crew's agent spec rather than by this code.

    Recorded rather than re-derived: the value asserted is the one the call actually carried.
    """
    mod = load_build()
    # Patched on kiro_crew.hooks, not on the module under test: the import is INSIDE the
    # function (it has to be, so a missing hooks module fails closed there), so the name is
    # never a module attribute and patching the module under test silently observes nothing.
    hooks = importlib.import_module("kiro_crew.hooks")
    seen: list[object] = []
    real = hooks.safe_read_file_bytes_nolink

    def _recording(path, anchor, **kwargs):
        # ``read_agent_spec`` reads the spec through this same guard before the persona read,
        # so record only the persona call -- the bound under test is the prompt ceiling.
        if os.path.basename(str(path)) == "persona.md":
            seen.append(kwargs.get("max_bytes"))
        return real(path, anchor, **kwargs)

    monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", _recording)

    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    (home / "agents" / "persona.md").write_text("a small persona\n", encoding="utf-8")
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)

    assert seen, "the prompt read did not go through the shared guard, so this proves nothing"
    assert seen == [mod._MAX_PROMPT_BYTES], f"the bound handed over was {seen}"


@pytest.mark.skipif(
    os.name != "posix",
    reason="drives the builder end to end; the builder is POSIX-only until an atomic no-follow primitive lands, so this behaviour is verified on POSIX",
)
def test_an_ordinary_prompt_is_unaffected_by_the_ceiling(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a persona is prose, and prose must still read verbatim."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    (home / "agents" / "persona.md").write_bytes(b"You are the front desk.\nBe brief.\n")
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert result.spec["prompt"] == "You are the front desk.\nBe brief.\n"


@pytest.mark.parametrize(
    "label,raw",
    [
        ("relative", "file://per\x00sona.md"),
        ("bare", "file://\x00"),
        ("absolute", "file:///tmp/per\x00sona.md"),
    ],
)
def test_a_nul_in_the_reference_is_refused_not_raised(
    tmp_path: pathlib.Path, label: str, raw: str
) -> None:
    """A NUL reaches a syscall as a bare ValueError, so it is refused on the string.

    The target comes from the crew's agent spec, which makes its bytes someone else's choice.
    Measured before the fix: all three of these left ValueError uncaught and it reached the
    CLI as a traceback, naming neither the spec nor the reference.

    All three shapes are covered because the branches differ -- relative and absolute take
    different paths through this function, and a NUL alone leaves an otherwise empty target.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(raw, agents)
    assert "NUL" in str(caught.value)


def test_MUTATION_without_the_nul_guard_the_reference_raises_valueerror(
    tmp_path: pathlib.Path,
) -> None:
    """Removing the guard must restore the bare ValueError, or the test above proves nothing."""
    mod = load_build(mutate=('    if "\\x00" in target:', "    if False:"))
    agents = tmp_path / "agents"
    agents.mkdir()
    with pytest.raises(ValueError) as caught:
        mod._resolve_prompt_path("file://per\x00sona.md", agents)
    assert not isinstance(caught.value, mod.ExportRefused), (
        "mutated to skip the NUL guard: the refusal still came back as ExportRefused, so the "
        "guard under test is not what produces it"
    )


@pytest.mark.parametrize("label,raw", [("relative", "file://persona.md"), ("absolute", None)])
def test_a_cycle_at_the_agents_directory_is_refused_not_raised(
    tmp_path: pathlib.Path, label: str, raw: str | None
) -> None:
    """A link loop AT the agents directory must refuse, on both ways in.

    The anchor is resolved before either branch chooses a path, so a cycle there is reached by
    a relative and an absolute reference alike. ``resolve()`` reports a loop as
    ``OSError(ELOOP)`` on some libcs and ``RuntimeError`` on others; measured on this host it
    was ``RuntimeError``, which is why catching only ``OSError`` left it escaping as a
    traceback that named neither the crew nor the reference.
    """
    mod = load_build()
    agents = tmp_path / "agents"
    other = tmp_path / "other"
    agents.symlink_to(other)
    other.symlink_to(agents)

    reference = raw if raw is not None else f"file://{tmp_path}/agents/persona.md"
    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(reference, agents)
    assert "cannot be resolved" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="uses chmod 000 to make a real file uninspectable")
def test_an_uninspectable_agent_spec_is_not_reported_as_missing(tmp_path: pathlib.Path) -> None:
    """Present-but-uninspectable and absent are different facts and get different refusals.

    Reporting the first as "nothing to deploy" sends the operator to look for a missing file
    while the spec sits there unreadable. The distinction is made where the ``lstat`` fails, so
    each outcome is refused at the point that detects it.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    crew = mod.resolve_crew("frontdesk", home)
    parent = crew.agent_spec_path.parent
    os.chmod(parent, 0o000)
    try:
        with pytest.raises(mod.ExportRefused) as caught:
            mod.read_agent_spec(crew)
    finally:
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(parent, 0o755)
    message = str(caught.value)
    assert "could not be inspected" in message, message
    assert "There is nothing to deploy" not in message, (
        "an unreadable spec was reported as absent, which sends the operator after a file "
        "that is present"
    )


@pytest.mark.skipif(os.name != "posix", reason="plants a symlink to open the redirect walk")
def test_a_hop_that_cannot_be_read_refuses_instead_of_ending_the_walk(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A hop that exists and cannot be read must refuse, not quietly end the walk.

    ``readlink`` answers EINVAL for an ordinary file, which is how the walk ends normally. Any
    other error means the hop is there and could not be judged, and ending the walk on it lets
    the ``resolve()`` below traverse a hop nothing looked at -- which on Windows is the SMB
    probe this walk exists to prevent.

    Two things are simulated and neither is the behaviour under test. The walk lives in the
    ``os.name == "nt"`` branch, opened with the mutation this suite already uses. And the
    failure is INJECTED: reaching it needs a hop whose ``lstat`` succeeds while its
    ``readlink`` fails, and on POSIX both need the same parent traverse permission, so no file
    layout here produces it. What is asserted is what the handler does with the error.
    """
    mod = load_build(mutate=('    elif os.name == "nt":', "    elif True:"))
    agents = tmp_path / "home" / "agents"
    agents.mkdir(parents=True)
    target = agents / "real.md"
    target.write_text("a persona\n", encoding="utf-8")
    link = agents / "persona.md"
    link.symlink_to(target)

    real_readlink = mod.os.readlink

    def _readlink(path, *a, **k):
        if str(path).endswith("persona.md"):
            raise PermissionError(13, "Permission denied")
        return real_readlink(path, *a, **k)

    monkeypatch.setattr(mod.os, "readlink", _readlink)

    # ABSOLUTE, because the hop walk is on that branch: a relative reference is judged by the
    # component walk above it, which refuses the link before this code is reached.
    with pytest.raises(mod.ExportRefused) as caught:
        mod._resolve_prompt_path(f"file://{link}", agents)
    assert "could not be inspected" in str(caught.value), str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="swaps a directory for a symlink mid-read")
@pytest.mark.parametrize("restore", [False, True], ids=["swap", "swap_and_restore"])
def test_an_anchor_swapped_during_the_read_does_not_authorize_its_bytes(
    tmp_path: pathlib.Path, monkeypatch, restore: bool
) -> None:
    """Bytes cleared by the guard must be the bytes inside the anchor this build pinned.

    Every containment answer the shared reader gives is about a NAME it resolves itself, so
    replacing the anchor between the chain walk and the read makes those answers true of the
    replacement. Measured before the pin: the attacker's persona was inlined into agent.json.

    Both shapes are covered because they are caught by different halves. Leaving the swap in
    place fails the identity allowlist: the file reached through the pinned descriptor is not
    the one the path now names. Swapping the original BACK defeats any before-and-after
    identity comparison -- both observations match -- and is caught instead by the bytes
    disagreeing between the two reads.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    agents = home / "agents"
    (agents / "persona.md").write_text("the real persona\n", encoding="utf-8")

    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "persona.md").write_text("ATTACKER BYTES\n", encoding="utf-8")

    hooks = importlib.import_module("kiro_crew.hooks")
    real = hooks.safe_read_file_bytes_nolink
    aside = tmp_path / "home" / "agents.real"
    state = {"swapped": False}

    def _swap_then_read(path, anchor, **kwargs):
        # ``read_agent_spec`` reads the spec through this same guard first; the swap targets
        # the PERSONA read, so pass the spec read straight through and act only on persona.md.
        if os.path.basename(str(path)) != "persona.md":
            return real(path, anchor, **kwargs)
        # Renamed rather than removed, so the real file stays reachable through the pinned
        # descriptor and the identity check is what has to refuse.
        if not state["swapped"]:
            state["swapped"] = True
            os.rename(agents, aside)
            os.symlink(str(attacker), str(agents), target_is_directory=True)
            data = real(path, anchor, **kwargs)
            if restore:
                os.unlink(agents)
                os.rename(aside, agents)
            return data
        return real(path, anchor, **kwargs)

    monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", _swap_then_read)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert state["swapped"], "the swap never happened, so this proves nothing"
    assert "ATTACKER" not in str(spec.get("prompt", "")), "the attacker's bytes reached the spec"
    message = str(caught.value)
    assert (
        "not the file inside the directory this build checked" in message
        or "changed while it was being read" in message
        or "could not be inspected inside the pinned anchor" in message
    ), message


@pytest.mark.skipif(os.name != "posix", reason="replaces a file with a directory mid-read")
@pytest.mark.parametrize("replacement", ["directory", "hard_link"])
def test_a_persona_replaced_between_the_two_reads_is_refused_not_crashed(
    tmp_path: pathlib.Path, monkeypatch, replacement: str
) -> None:
    """The pinned observation authorises an INODE, so it has to say what kind of inode.

    The identity allowlist answers "is this the file I pinned" and nothing else. A persona
    swapped for a directory between the verdict read and the authorised read has a directory's
    inode allowlisted, and the read then fails inside the shared reader: measured, an uncaught
    IsADirectoryError out of a function whose contract is ExportRefused. A second hard link is
    the same shape with a different consequence -- the inode is legitimately the pinned one,
    and can still have its bytes changed through the other name after this read.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    persona = home / "agents" / "persona.md"
    persona.write_text("the real persona\n", encoding="utf-8")

    hooks = importlib.import_module("kiro_crew.hooks")
    real = hooks.safe_read_file_bytes_nolink
    state = {"done": False}

    def _replace_after_first_read(path, anchor, **kwargs):
        # ``read_agent_spec`` reads the spec through this same guard first; the replacement
        # targets the PERSONA read, so pass the spec read straight through.
        if os.path.basename(str(path)) != "persona.md":
            return real(path, anchor, **kwargs)
        data = real(path, anchor, **kwargs)
        if not state["done"]:
            state["done"] = True
            if replacement == "directory":
                os.unlink(persona)
                persona.mkdir()
            else:
                os.link(persona, home / "agents" / "second-name.md")
        return data

    monkeypatch.setattr(hooks, "safe_read_file_bytes_nolink", _replace_after_first_read)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert state["done"], "the replacement never happened, so this proves nothing"
    expected = "not a regular file" if replacement == "directory" else "names inside the anchor"
    assert expected in str(caught.value), str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="fails a read after the guard cleared it")
def test_a_read_that_fails_part_way_refuses_instead_of_escaping(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The authorised read has a third outcome, and it needs its own refusal.

    The identity reader raises ``PermissionError`` when the bytes are not the pinned file's,
    and that is a different fact from the read of the RIGHT file failing part way, which a
    disconnected NFS or FUSE mount produces as a plain ``OSError``. Without an arm for it the
    error leaves a function contracted to raise ``ExportRefused`` as a bare traceback.

    Handler ORDER carries this: ``PermissionError`` is a subclass of ``OSError``, so the
    broad arm placed first would make the identity refusal unreachable and report a
    substituted file as a truncated read.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    (home / "agents" / "persona.md").write_text("the real persona\n", encoding="utf-8")

    hooks = importlib.import_module("kiro_crew.hooks")

    def _fail_mid_read(raw, allowed):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(hooks, "safe_read_file_bytes_with_identity", _fail_mid_read)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    message = str(caught.value)
    assert "could not be read through to the end" in message, message
    assert (
        "not the file inside the directory" not in message
    ), "a mid-read failure reported itself as an identity mismatch: handler order is wrong"


@pytest.mark.skipif(os.name != "posix", reason="swaps an ancestor of the anchor mid-build")
def test_an_ancestor_of_the_anchor_swapped_after_resolution_is_not_followed(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The pin has to cover the path that reaches the anchor, not only the anchor.

    ``O_NOFOLLOW`` guards the FINAL component, so opening the anchor by pathname pins the leaf
    and follows every directory above it. A writable ancestor replaced between the resolution
    that produced the path and that open is therefore traversed, and every question asked
    through the descriptor is answered about the replacement -- the attacker's persona reaches
    ``agent.json`` with the anchor's own name never having changed.

    The swap targets the CREW HOME, one level above ``agents/``, because the anchor itself was
    already pinned and that is exactly why the hole moved up.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    (home / "agents" / "persona.md").write_text("the real persona\n", encoding="utf-8")

    attacker = tmp_path / "attacker"
    (attacker / "agents").mkdir(parents=True)
    (attacker / "agents" / "persona.md").write_text("ATTACKER BYTES\n", encoding="utf-8")

    # The swap has to land in the window the pin exists to close: AFTER the resolution that
    # produced the anchor and BEFORE the anchor is opened. ``_within`` is called between the
    # two to choose the anchor branch, so hooking it puts the replacement exactly there.
    # Swapping later instead lands after the descriptor already exists, where the identity
    # allowlist refuses and the pin is never consulted -- measured, and the reason an earlier
    # version of this test passed with the pin removed.
    real_within = mod._within
    state = {"done": False}

    def _swap_then_answer(candidate, root):
        if not state["done"]:
            state["done"] = True
            os.rename(home, tmp_path / "home.real")
            os.symlink(str(attacker), str(home), target_is_directory=True)
        return real_within(candidate, root)

    monkeypatch.setattr(mod, "_within", _swap_then_answer)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert state["done"], "the swap never happened, so this proves nothing"
    assert "ATTACKER" not in str(spec.get("prompt", "")), "the attacker's bytes reached the spec"
    # WHICH guard refused, not merely that one did. Downstream the identity allowlist and the
    # byte comparison both catch this swap too, so an assertion that something refused passes
    # with the pin removed and proves nothing about the pin. The walk is the only guard that
    # can refuse before the descriptor exists, and its wording is what distinguishes it.
    assert "cannot be pinned" in str(caught.value), (
        f"a downstream guard refused instead of the anchor walk, so this does not pin the "
        f"walk: {caught.value}"
    )


@pytest.mark.skipif(
    os.name != "posix",
    reason="drives the builder end to end; the builder is POSIX-only until an atomic no-follow primitive lands, so this behaviour is verified on POSIX",
)
def test_the_agents_tree_is_resolved_once_for_the_whole_operation(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Two resolutions can be separately self-consistent about DIFFERENT trees.

    The validator resolved the agents directory and so did its caller. Each answer was
    internally consistent, so a writable agents directory replaced between them let the
    replacement's anchor clear containment while the replacement's persona cleared the read,
    and the attacker's bytes were signed into ``agent.json``. One reading for the whole
    operation removes the disagreement rather than trying to detect it.

    Counted by OBSERVING ``resolve`` calls on the agents directory during a real build, not by
    reading the source: a second resolution reintroduced anywhere below would show up here.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://persona.md")
    agents = home / "agents"
    (agents / "persona.md").write_text("the real persona\n", encoding="utf-8")

    real_resolve = pathlib.Path.resolve
    seen: list[str] = []

    def _counting_resolve(self, *args, **kwargs):
        out = real_resolve(self, *args, **kwargs)
        if str(self) == str(agents):
            seen.append(str(self))
        return out

    monkeypatch.setattr(pathlib.Path, "resolve", _counting_resolve)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    seen.clear()
    mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)

    assert len(seen) == 1, (
        f"the agents directory was resolved {len(seen)} times in one operation; two answers "
        f"can be separately consistent about different trees"
    )
