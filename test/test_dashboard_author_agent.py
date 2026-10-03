"""The dashboard-author crewmate is registered, materialized, and selectable.

``kirocrew-dashboard-author`` authors one dashboard template and lands it as a pull
request. Its charter ships as the ``dashboard-template`` skill's ``agent-spec.md``, and
until it is wired through the installer, the owned-files list and an eager install call,
nothing writes the spec to ``~/.kiro/agents/`` -- so ``session_create`` cannot resolve
the name and the crewmate can never be selected.

These tests pin the registration (the filename is owned), the materialization (a rebuild
writes a loadable spec), and the charter the spec carries: the author writes files and
drives git, so it mounts ``fs_write`` and ``execute_bash`` -- but never auto-approves
them, exactly as ``kirocrew-conductor`` draws the line -- and auto-approves only the
reading core verbs its skill names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.agent_files import DASHBOARD_AUTHOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION


class _Rig:
    """A private agents directory with the machine-specific inputs pinned."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.agents = tmp_path / "agents"
        self.agents.mkdir()
        binary = tmp_path / "bin" / "kirocrew"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr(agent, "_KIROCREW_BIN", str(binary))
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
        monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "no-hooks")
        monkeypatch.setattr(
            "kiro_crew.apps.bridges._mcp_json_path", lambda: self.agents / "kirocrew.json"
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.installed_kiro_cli_version",
            lambda: SPEC_PERMISSIONS_MIN_VERSION,
        )

    def read(self, filename: str) -> dict[str, Any]:
        return json.loads((self.agents / filename).read_text(encoding="utf-8"))


def test_the_dashboard_author_filename_is_owned() -> None:
    """A managed spec Kiro Crew writes must be in the owned-files allowlist, or the
    Playwright convergence sweep and the self-heal pass skip it."""
    assert DASHBOARD_AUTHOR_AGENT_FILENAME == "kirocrew-dashboard-author.json"
    assert DASHBOARD_AUTHOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES


def test_a_rebuild_materializes_the_dashboard_author(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh rebuild writes the spec so ``session_create`` can resolve the name."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert spec["name"] == "kirocrew-dashboard-author"


def test_the_dashboard_author_mounts_but_never_auto_approves_write_or_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fs_write`` and ``execute_bash`` are the job, so they are mounted; a human reads
    the author's diff, so neither is auto-approved -- the line ``kirocrew-conductor``
    draws, and for the same reason (``allowedTools`` has no argument matching)."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert "fs_write" in spec["tools"]
    assert "execute_bash" in spec["tools"]
    assert "fs_write" not in spec["allowedTools"]
    assert "execute_bash" not in spec["allowedTools"]


def test_the_dashboard_author_auto_approves_only_reading_core_verbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every auto-approved entry only reads or recalls -- the safety story on a path
    that never reaches the PreToolUse gate. ``fs_read`` is mounted but filtered off the
    auto-approve list by the governance ceiling, exactly as it is for every spec derived
    from the template, so it reaches the gate like the write and shell tools do."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert set(spec["allowedTools"]) == {
        "tool_search",
        "@kirocrew-core/skill_search",
        "@kirocrew-core/skill_discover",
        "@kirocrew-core/memory_recall",
        "@kirocrew-core/resource_status",
    }
    # No session verb, no work-ledger mount, no publication surface, no file-write grant.
    for ref in spec["allowedTools"]:
        assert not ref.startswith("@kirocrew-dashboard")
        assert not ref.startswith("@kirocrew-work")
        assert ref not in ("fs_write", "execute_bash", "code")


def test_the_prompt_and_description_come_from_the_shipped_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The charter has ONE authoritative copy -- the shipped ``agent-spec.md`` -- so the
    installed spec's prompt and description are what ``parse_markdown_spec`` reads from
    it, never a Python constant held equal to it by nothing."""
    from kiro_crew.agent_materialization.worker_agent import _DASHBOARD_AUTHOR_SPEC_PATH
    from kiro_crew.agent_spec_format import parse_markdown_spec

    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    source = parse_markdown_spec(_DASHBOARD_AUTHOR_SPEC_PATH.read_text(encoding="utf-8"))
    assert spec["prompt"] == source["prompt"]
    assert spec["description"] == source["description"]


def test_a_fresh_install_writes_and_stamps_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing at the path -> the managed spec is written and the sidecar stamped, so the
    next rebuild recognises its own file."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)["name"] == "kirocrew-dashboard-author"
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is True


def test_a_prior_managed_write_is_refreshed_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose recorded digest matches its on-disk bytes is proven ours, so a managed
    refresh overwrites it in place and re-records. Recognition travels with what WE last
    wrote: a second rebuild recognises the first rebuild's file (its bytes still hash to
    the recorded digest) and refreshes it. A managed overwrite that CHANGES the bytes
    re-records the new digest, so ownership tracks the live managed bytes rather than one
    frozen version -- no backup, no skip."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()  # writes the spec and records its digest
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is True

    # A second rebuild recognises our prior write and refreshes it in place (idempotent).
    agent.rebuild_agent_config()
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is True

    # A managed overwrite with changed bytes re-records: ownership follows the live bytes.
    import kiro_crew.agent_materialization.worker_agent as wa

    changed = json.loads(target.read_text(encoding="utf-8"))
    changed["prompt"] = "a newer managed version"
    wa.agent_mod._atomic_json_write(target, changed)
    # Record the digest of exactly the bytes now on disk (what the installer does), so the
    # match is correct regardless of platform newline translation in the atomic writer.
    wa.agent_state.set_managed_owned("kirocrew-dashboard-author", target.read_bytes())
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is True
    assert rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)["prompt"] == "a newer managed version"


def test_a_user_file_is_left_untouched_and_not_installed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive-confirmation: a readable sidecar with no record (False) means the file is
    a user artefact. The managed spec is NOT written and the user's file is left exactly
    as it was -- no overwrite, no backup, no exception. The agent is simply unselectable
    under this name until the user removes it."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {"name": "kirocrew-dashboard-author", "prompt": "my own template"}
    target.write_text(json.dumps(user), encoding="utf-8")
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is False

    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8")) == user  # untouched
    assert not (rig.agents / (DASHBOARD_AUTHOR_AGENT_FILENAME + ".saved")).exists()


def test_a_user_copy_of_the_managed_template_is_not_clobbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fenced copy scenario: a user copies the managed template into this name via
    template-create, so the on-disk spec reproduces every managed field (including
    ``mcpServers.kirocrew-core.command``). A content probe would misread it as managed and
    overwrite it. Durable provenance does not: template-create prunes the name, so the
    sidecar reads False and the user's copy is left untouched."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()  # a realistic managed spec exists to copy from
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    managed_copy = json.loads(target.read_text(encoding="utf-8"))
    managed_copy["prompt"] = "the user's hand-edited copy"
    target.write_text(json.dumps(managed_copy), encoding="utf-8")
    agent_state.prune("kirocrew-dashboard-author")  # template-create prunes the name
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is False

    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8"))["prompt"] == "the user's hand-edited copy"


def test_an_unreadable_sidecar_does_not_overwrite_a_pre_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown provenance must NOT overwrite. When the sidecar is present but unreadable,
    ``managed_owned_matches`` is None (not False); with a pre-existing file at the path the
    managed write is skipped and the file left intact -- we never destroy a file we cannot
    prove is ours, and never guess from unreadable state."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {"name": "x", "prompt": "a user file, provenance unknowable"}
    target.write_text(json.dumps(user), encoding="utf-8")
    monkeypatch.setattr(wa.agent_state, "managed_owned_matches", lambda name, path: None)

    wa._install_dashboard_author_agent()  # must not raise and must not overwrite

    assert json.loads(target.read_text(encoding="utf-8")) == user


def test_a_non_regular_pre_existing_file_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink (or any non-regular file) at the target is not a plain spec we can prove
    is ours; it is left untouched and not written over."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    target.symlink_to(elsewhere)

    wa._install_dashboard_author_agent()  # must not raise

    assert target.is_symlink()  # untouched, still points where it did
    assert target.resolve() == elsewhere.resolve()


def test_a_stamp_whose_file_is_gone_does_not_classify_a_later_user_file_as_ours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is a content DIGEST, so a record cannot confirm a file it does not match.
    If a managed write's ``os.replace`` fails after the digest is recorded (a documented
    Windows AV/indexer case, swallowed to a debug line), the record names bytes that are
    not on disk. A user who then hand-places their own spec at this stem -- the very remedy
    the docs name -- must NOT have it classified as ours and silently overwritten. The
    recorded digest does not match the user's bytes, so ``managed_owned_matches`` is False
    and the file is left untouched."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    name = "kirocrew-dashboard-author"

    # A record names bytes that are not on disk (a write that did not land, or a removed
    # spec), simulated by recording a digest with no file present.
    agent_state.set_managed_owned(name, b'{"name": "kirocrew-dashboard-author"}\n')
    assert not target.exists()

    # The user hand-places their OWN spec at the stem.
    user = {"name": "kirocrew-dashboard-author", "prompt": "my own, different, template"}
    target.write_text(json.dumps(user), encoding="utf-8")

    # The record does not match the user's bytes -> not ours -> never overwritten.
    assert agent_state.managed_owned_matches(name, target) is False
    agent.rebuild_agent_config()
    assert json.loads(target.read_text(encoding="utf-8")) == user


def test_a_fresh_install_records_a_matching_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: a managed write records the digest of exactly the bytes it wrote, so
    ``managed_owned_matches`` confirms the file afterwards and a refresh is permitted."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    agent.rebuild_agent_config()
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is True
    # Bytes that differ from the record do not match; a managed refresh re-records the new
    # digest, so ownership tracks the live bytes.
    cfg = json.loads(target.read_text(encoding="utf-8"))
    cfg["prompt"] = "externally mutated"
    target.write_text(json.dumps(cfg), encoding="utf-8")
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is False


def test_a_record_failure_unlinks_the_just_written_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec and its ownership record must land together. If recording the digest fails
    (a corrupt or unwritable sidecar makes the strict ``set_managed_owned`` raise), the
    just-written spec is UNLINKED and the install treated as failed -- a spec on disk with
    no matching record reads as unowned forever (the install gate and the repair sweep both
    skip it, and nothing re-records it), so its grants would never be re-filtered through a
    tightened ceiling. Removing it leaves the path free for a fresh install next boot."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert not target.exists()  # fresh install, path free

    def _raise(_name: str, _raw: bytes | None) -> None:
        raise OSError("sidecar unwritable")

    monkeypatch.setattr(wa.agent_state, "set_managed_owned", _raise)

    wa._install_dashboard_author_agent()  # must not raise

    # The record could not be written, so the spec is removed -- no stranded unowned spec.
    assert not target.exists()
    # No ownership record survives the failed install.
    assert agent_state.managed_owned_digest("kirocrew-dashboard-author") is None


def test_the_install_holds_the_agents_spec_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check -> write -> stamp sequence runs under ``agents_spec_lock`` -- the same
    writer lock the sibling installer holds -- so overlapping multi-process rebuilds cannot
    interleave a write from one with the digest recorded by another."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    held: list[bool] = []
    real_lock = wa.agent_mod.agents_spec_lock

    import contextlib

    @contextlib.contextmanager
    def _spy(agents_dir):
        held.append(True)
        with real_lock(agents_dir):
            yield

    monkeypatch.setattr(wa.agent_mod, "agents_spec_lock", _spy)
    wa._install_dashboard_author_agent()
    assert held == [True]
    assert (rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME).exists()


def test_a_record_failure_on_a_refresh_restores_the_prior_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the file was already ours (a refresh) and recording the new digest fails, the
    PRIOR bytes are restored rather than unlinked -- the old spec and its still-valid record
    stay consistent, so a transient sidecar fault during a refresh does not delete a working
    managed spec."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    agent.rebuild_agent_config()  # a confirmed-owned managed spec exists
    prior = target.read_bytes()
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is True

    calls = {"n": 0}
    real_set = wa.agent_state.set_managed_owned

    def _fail_second(name: str, raw: bytes | None) -> None:
        calls["n"] += 1
        raise OSError("sidecar unwritable")

    monkeypatch.setattr(wa.agent_state, "set_managed_owned", _fail_second)
    wa._install_dashboard_author_agent()  # a refresh whose record fails

    # The prior managed spec is restored (not deleted) and still matches its record.
    assert target.exists()
    assert target.read_bytes() == prior
    # Restored through the atomic writer, so it is complete, parseable JSON -- never a
    # truncated/partial file a kiro-cli reader could choke on.
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "kirocrew-dashboard-author"
    monkeypatch.setattr(wa.agent_state, "set_managed_owned", real_set)
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is True


def test_managed_owned_matches_does_not_follow_a_symlink(tmp_path: Path) -> None:
    """``managed_owned_matches`` must not follow a symlink at the owned stem to read and
    digest a user-controlled target. Even with a record present, a symlinked path reads as
    None (cannot confirm) rather than hashing the link target."""
    import kiro_crew.agent_state as st

    name = "kirocrew-dashboard-author"
    real = tmp_path / "real.json"
    raw = b'{"name": "kirocrew-dashboard-author"}\n'
    real.write_bytes(raw)
    st.set_managed_owned(name, raw)
    assert st.managed_owned_matches(name, real) is True  # a regular file matches

    link = tmp_path / "link.json"
    link.symlink_to(real)
    # The link's TARGET bytes hash to the record, but the no-follow read refuses the link,
    # so the match is None -- the target is never opened or digested.
    assert st.managed_owned_matches(name, link) is None


def test_managed_owned_matches_refuses_a_symlink_without_o_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where ``O_NOFOLLOW`` is unavailable (Windows), the read must FAIL CLOSED: an ``lstat``
    refuses the symlink before the open, so a zero-flag ``os.open`` never follows it. A
    regular file still matches on the same path."""
    import kiro_crew.agent_state as st

    monkeypatch.delattr(st.os, "O_NOFOLLOW", raising=False)
    name = "kirocrew-dashboard-author"
    real = tmp_path / "real.json"
    raw = b'{"name": "kirocrew-dashboard-author"}\n'
    real.write_bytes(raw)
    st.set_managed_owned(name, raw)
    assert st.managed_owned_matches(name, real) is True  # regular file still matches

    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert st.managed_owned_matches(name, link) is None  # symlink refused by the lstat pre-check


def test_only_the_finalized_digest_is_ownership_proof(tmp_path: Path) -> None:
    """Ownership proof is the FINALIZED ``managed_owned`` digest alone. There is no pending
    or staging slot that could confirm a file, so a spec is recognised as ours only after a
    completed ``set_managed_owned`` -- never from a half-finished install's leftover state."""
    import kiro_crew.agent_state as st

    name = "kirocrew-dashboard-author"
    spec = tmp_path / "spec.json"
    raw = b'{"name": "kirocrew-dashboard-author", "prompt": "ours"}\n'
    spec.write_bytes(raw)

    # No record yet -> not ours.
    assert st.managed_owned_digest(name) is None
    assert st.managed_owned_matches(name, spec) is False
    # A completed finalize is the only thing that confirms it.
    st.set_managed_owned(name, raw)
    assert st.managed_owned_matches(name, spec) is True


def test_a_failed_finalize_leaves_no_ownership_for_a_restored_user_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed finalize must not leave any record that classifies a later file as managed.
    The install's finalize fails and rolls back; afterwards there is NO finalized record, so
    a user who restores their own spec at the stem (even one byte-identical to the managed
    spec) is NOT auto-confirmed as ours and is never overwritten."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert not target.exists()  # fresh install, path free

    def _raise(_name: str, _raw: bytes | None) -> None:
        raise OSError("sidecar unwritable")

    monkeypatch.setattr(wa.agent_state, "set_managed_owned", _raise)
    wa._install_dashboard_author_agent()  # finalize fails -> rollback unlinks the fresh write

    # No ownership record survives the failed install.
    assert agent_state.managed_owned_digest("kirocrew-dashboard-author") is None
    # A user later places their own spec at the stem; with no finalized record it is NOT ours.
    user = {"name": "kirocrew-dashboard-author", "prompt": "my own template"}
    target.write_text(json.dumps(user), encoding="utf-8")
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is False


def test_managed_owned_sidecar_round_trips_and_is_pruned(tmp_path: Path) -> None:
    """The durable provenance signal: ``set_managed_owned`` records the DIGEST of the
    written bytes, ``managed_owned_matches`` confirms a file whose bytes hash to it, and
    :func:`agent_state.prune` (which the template-create copy path calls on the new name)
    drops it. A name never stamped does not match."""
    name = "kirocrew-dashboard-author"
    spec = tmp_path / "spec.json"
    raw = b'{"name": "kirocrew-dashboard-author", "prompt": "ours"}\n'
    spec.write_bytes(raw)

    assert agent_state.managed_owned_matches(name, spec) is False  # never stamped
    agent_state.set_managed_owned(name, raw)
    assert agent_state.managed_owned_matches(name, spec) is True
    assert agent_state.managed_owned_digest(name) is not None
    spec.write_bytes(b'{"name": "x", "prompt": "a different file"}\n')
    assert agent_state.managed_owned_matches(name, spec) is False  # digest does not match
    spec.write_bytes(raw)
    assert agent_state.managed_owned_matches(name, spec) is True

    agent_state.prune(name)  # the copy path drops the record
    assert agent_state.managed_owned_matches(name, spec) is False
    # Explicit clear (None) drops the record too (entry empties, name is removed).
    agent_state.set_managed_owned(name, raw)
    agent_state.set_managed_owned(name, None)
    assert agent_state.managed_owned_matches(name, spec) is False


def test_managed_owned_tri_state_on_unreadable_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tri-state contract directly: a readable sidecar with no record does not match
    (False), a recorded digest matching the file matches (True), and an unreadable/malformed
    sidecar reads None (not False) via ``managed_owned_matches``. ``set_managed_owned`` is
    STRICT: on an unreadable sidecar it RAISES rather than swallowing."""
    import kiro_crew.agent_state as st

    name = "kirocrew-dashboard-author"
    spec = tmp_path / "spec.json"
    raw = b'{"name": "kirocrew-dashboard-author"}\n'
    spec.write_bytes(raw)

    assert st.managed_owned_matches(name, spec) is False
    st.set_managed_owned(name, raw)
    assert st.managed_owned_matches(name, spec) is True
    monkeypatch.setattr(st, "_read", lambda *, strict=False: (_ for _ in ()).throw(OSError("boom")))
    assert st.managed_owned_matches(name, spec) is None  # sidecar unreadable -> unknown
    with pytest.raises(OSError):
        st.set_managed_owned(name, raw)  # strict: an unreadable sidecar propagates


def test_repair_pass_skips_an_unprovable_dashboard_author_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling point 2: a managed REWRITE must honour the same positive-confirmation the
    installer does. The legacy-hook repair sweep is filename-keyed over the owned set, so
    without a gate it would strip keys from a user file squatting this now-owned stem. It
    must skip the dashboard-author file unless the sidecar confirms our ownership."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A user file carrying the legacy hook key the sweep would otherwise strip.
    user = {"name": "kirocrew-dashboard-author", "hooks": {"auto_approve_tools": ["x"]}}
    target.write_text(json.dumps(user), encoding="utf-8")
    agent_state.prune("kirocrew-dashboard-author")  # not ours
    assert agent_state.managed_owned_matches("kirocrew-dashboard-author", target) is False

    agent.repair_agent_configs()

    # Unprovable -> left exactly as the user wrote it, legacy key intact.
    assert json.loads(target.read_text(encoding="utf-8")) == user


def test_repair_pass_rewrites_an_owned_dashboard_author_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: when the sidecar confirms the file is ours, the repair sweep DOES
    strip the legacy hook key, exactly as it does for every other owned spec."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    managed = {"name": "kirocrew-dashboard-author", "hooks": {"auto_approve_tools": ["x"]}}
    target.write_text(json.dumps(managed), encoding="utf-8")
    # Stamp the digest of exactly the bytes on disk, so the record MATCHES this file.
    agent_state.set_managed_owned("kirocrew-dashboard-author", target.read_bytes())  # ours

    agent.repair_agent_configs()

    assert "auto_approve_tools" not in json.loads(target.read_text(encoding="utf-8"))["hooks"]


def test_a_torn_write_leaves_a_pending_marker_and_fails_closed_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install records a PENDING crash-marker, writes the spec, then records the
    FINALIZED ownership digest and clears the pending. A kill between the pending and the
    finalize (modelled by making the spec write raise) must fail CLOSED: ownership proof is
    the finalized digest alone, so the stem reads unowned and no tightened ceiling honours
    its grants. The rollback clears the pending it set, so a clean reinstall (fresh install:
    path absent) completes and the spec lands owned."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    name = "kirocrew-dashboard-author"

    real_write = wa.agent_mod._atomic_bytes_write

    def _torn(path: Path, raw: bytes) -> None:
        raise OSError("simulated kill after the pending marker, before the spec lands")

    monkeypatch.setattr(wa.agent_mod, "_atomic_bytes_write", _torn)
    wa._install_dashboard_author_agent()  # must not raise

    # Fail closed: no spec landed, no finalized ownership, and the rollback cleared the
    # pending marker so it does not block the next rebuild.
    assert not target.exists()
    assert agent_state.managed_owned_matches(name, target) is False
    assert agent_state.has_managed_pending(name) is False

    # Recovery: a clean reinstall completes, owned.
    monkeypatch.setattr(wa.agent_mod, "_atomic_bytes_write", real_write)
    agent.rebuild_agent_config()
    assert agent_state.managed_owned_matches(name, target) is True
    assert agent_state.has_managed_pending(name) is False


def test_a_pending_whose_bytes_landed_is_reconciled_to_owned_and_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """c488: a pending marker is TRANSIENT -- reconciled at rebuild, never left standing.
    When the on-disk bytes MATCH the pending digest, the interrupted write landed its bytes,
    so the reconcile promotes them to the finalized record and the normal refresh runs. The
    stem must NOT be left stuck skipping every refresh (which would strand stale grants
    against a tightened ceiling forever)."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    name = "kirocrew-dashboard-author"

    # A real managed install exists; simulate an interrupted install whose spec DID land but
    # whose finalize did not run: pending set to the on-disk bytes, finalized cleared.
    agent.rebuild_agent_config()
    landed = target.read_bytes()
    agent_state.set_managed_owned(name, None)
    agent_state.set_managed_pending(name, landed)
    assert agent_state.managed_owned_matches(name, target) is False  # no finalized record

    # The reconcile promotes the landed bytes to owned, so the refresh proceeds normally and
    # the pending is cleared -- the refresh is never skipped because of a standing pending.
    agent.rebuild_agent_config()
    assert agent_state.managed_owned_matches(name, target) is True
    assert agent_state.has_managed_pending(name) is False


def test_an_interrupted_refresh_then_user_edit_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """c488: a refresh is interrupted (pending marker set, finalized digest intact), the user
    then replaces the file at the stem with their own, and the next rebuild must NOT overwrite
    it. The reconcile sees the on-disk bytes match NEITHER the pending digest nor anything we
    wrote, clears the pending, and treats the spec as user-owned; the gate then leaves it
    untouched. Ownership proof stays the finalized digest alone, and the pending never stands."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    name = "kirocrew-dashboard-author"

    # A managed install exists, then a refresh is interrupted: the pending marker is set
    # (the digest-deterministic install means it equals the finalized digest), spec unchanged.
    agent.rebuild_agent_config()
    landed = target.read_bytes()
    agent_state.set_managed_pending(name, landed)

    # The user replaces the file at the stem with their OWN content.
    user = {"name": "kirocrew-dashboard-author", "prompt": "my own edited author"}
    target.write_text(json.dumps(user), encoding="utf-8")

    # The reconcile clears the pending (on-disk != pending digest) and the gate refuses the
    # write (on-disk != finalized digest) -> the user's file stands, pending gone.
    monkeypatch.setattr(wa.agent_mod, "_atomic_bytes_write", _should_not_write)
    agent.rebuild_agent_config()  # must not raise and must not overwrite
    assert json.loads(target.read_text(encoding="utf-8")) == user
    assert agent_state.has_managed_pending(name) is False


def _should_not_write(path: Path, raw: bytes) -> None:
    raise AssertionError("the managed spec must not be written in this case")


def test_an_authorized_edit_of_the_owned_spec_renews_the_ownership_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F1: an authorized dashboard model/skills PATCH rewrites the owned spec's
    bytes. Without renewing the ownership digest the installer bound to the PRIOR bytes,
    the positive-confirmation gate reads the edited file as unowned and SKIPS every future
    rebuild -- so a later tightened governance ceiling never re-applies and the spec's
    gate-bypassing ``allowedTools`` keeps auto-approving a now-forbidden tool.

    ``renew_managed_ownership_after_authorized_write`` (called by the PATCH path right after
    its atomic write, under ``agents_spec_lock``) re-stamps the digest to the bytes just
    written WHEN the prior bytes were our confirmed managed spec, so the file stays
    confirmable and the per-boot refresh keeps running."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    name = "kirocrew-dashboard-author"
    assert agent_state.managed_owned_matches(name, target) is True

    # Simulate the authorized PATCH write path: capture prior bytes under the lock, rewrite
    # the spec in place (a model/skills edit), then renew the ownership digest.
    prior = target.read_bytes()
    edited = json.loads(target.read_text(encoding="utf-8"))
    edited["model"] = "a-different-model"
    wa.agent_mod._atomic_json_write(target, edited)
    # Before the renewal the digest is stale -> the file reads as unowned (the F1 bug).
    assert agent_state.managed_owned_matches(name, target) is False

    wa.renew_managed_ownership_after_authorized_write(target, prior)

    # After the renewal the edited bytes are confirmable again, so a subsequent rebuild
    # recognises and refreshes the spec rather than skipping it forever.
    assert agent_state.managed_owned_matches(name, target) is True
    agent.rebuild_agent_config()
    assert agent_state.managed_owned_matches(name, target) is True


def test_an_authorized_edit_of_a_user_file_does_not_adopt_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The renewal is gated on POSITIVE prior ownership: when the bytes being replaced are a
    genuinely user-authored file at the owned stem (no matching finalized digest), the renewal
    does NOTHING. A managed edit must never ADOPT a user file by stamping it owned -- the file
    stays exactly as unowned as before, so the installer continues to leave it untouched."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    name = "kirocrew-dashboard-author"
    user = {"name": "kirocrew-dashboard-author", "prompt": "my own template"}
    target.write_text(json.dumps(user), encoding="utf-8")
    assert agent_state.managed_owned_matches(name, target) is False

    # The same write+renew sequence the PATCH path runs, but the prior bytes are the user's.
    prior = target.read_bytes()
    edited = dict(user, prompt="my own template, edited")
    wa.agent_mod._atomic_json_write(target, edited)

    wa.renew_managed_ownership_after_authorized_write(target, prior)

    # Not adopted: still unowned, so a rebuild still leaves the (edited) user file untouched.
    assert agent_state.managed_owned_matches(name, target) is False
    agent.rebuild_agent_config()
    assert json.loads(target.read_text(encoding="utf-8")) == edited
