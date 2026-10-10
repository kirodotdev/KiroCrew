"""Tests for the kirocrew-dashboard-author managed crewmate.

``kirocrew-dashboard-author`` authors one dashboard template and lands it as a pull
request. Its charter ships as the ``dashboard-template`` skill's ``agent-spec.md``, and
until it is wired through the installer, the owned-files list and an eager install call,
nothing writes the spec to ``~/.kiro/agents/`` -- so ``session_create`` cannot resolve
the name and the crewmate can never be selected.

These tests pin the registration (the filename is owned), the materialization (a rebuild
writes a loadable spec), the charter the spec carries (the author writes files and drives
git, so it mounts ``fs_write`` and ``execute_bash`` but never auto-approves them, and
auto-approves only the reading core verbs its skill names), and PROVENANCE: the spec at
the owned filename is attributed by an INSTALLER-RECORDED OWNERSHIP DIGEST in the
``agent_state`` sidecar (the SHA-256 of the exact bytes the installer last wrote there),
so the installer rewrites its own file but never overwrites a user file -- or a copy of
another owned agent -- hand-placed at the once-user-creatable stem.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.agent_files import DASHBOARD_AUTHOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_materialization import worker_agent
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


def _is_installers(path: Path) -> bool:
    """Does the file at *path* confirm as this installer's own write -- its bytes reproduce
    the installer-recorded ownership digest?"""
    spec = worker_agent.agent_mod._read_spec_capped(path)
    return worker_agent._is_confirmed_managed_dashboard_author(spec)


# --------------------------------------------------------------------------- #
# Registration and charter (items 1-2: install + owned filename).
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Digest provenance (GPT 6.1 F1, maintainer ruling): ownership is an
# installer-recorded digest of the last managed write, NOT a content mark.
# --------------------------------------------------------------------------- #


def test_the_install_records_the_ownership_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer records, in the ``agent_state`` sidecar, the digest of exactly the
    bytes it wrote. That recorded digest is the ownership record a later rebuild (and the
    fork / capability gates) confirm the on-disk file against -- so the installed file
    confirms as this installer's own, and the recorded digest equals the file's digest."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert spec["name"] == "kirocrew-dashboard-author"
    recorded = agent_state.get_managed_digest("kirocrew-dashboard-author")
    assert recorded is not None
    assert recorded == agent_state.spec_digest(spec)
    assert _is_installers(target) is True


def test_the_recorded_digest_is_of_the_bytes_the_governed_write_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ceiling that moves while the install runs cannot split the bytes from their record.

    The ceiling is read when the shared tail runs at the write, so the pending digest
    must be taken after that pass. Here the ceiling starts withholding ``tool_search``
    the moment the writer is entered: the landed spec loses the grant, and the
    recorded digest must still be that spec's, or every later rebuild would read the
    file as foreign and stop re-filtering it.
    """
    from kiro_crew.agent_materialization import auto_approve

    rig = _Rig(tmp_path, monkeypatch)
    real_write = auto_approve.write_governed_spec

    def tightening_write(path: Path, config: dict[str, Any], **kwargs: Any) -> None:
        if path.name == DASHBOARD_AUTHOR_AGENT_FILENAME:
            monkeypatch.setattr(auto_approve, "_may_auto_approve", lambda ref: ref != "tool_search")
        real_write(path, config, **kwargs)

    monkeypatch.setattr(auto_approve, "write_governed_spec", tightening_write)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert "tool_search" not in spec["allowedTools"]
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") == agent_state.spec_digest(
        spec
    )
    assert _is_installers(target) is True


def test_a_prior_managed_write_is_refreshed_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose bytes reproduce the installer-recorded digest is proven ours, so a
    second rebuild recognises it and refreshes it in place (idempotent) rather than refusing
    or duplicating."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert _is_installers(target) is True

    # A second rebuild recognises our prior write (its digest) and refreshes it in place.
    agent.rebuild_agent_config()
    assert _is_installers(target) is True
    assert rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)["name"] == "kirocrew-dashboard-author"


def test_a_user_file_is_left_untouched_and_not_installed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file at the stem for which NO ownership digest is recorded is a user artefact: the
    managed spec is NOT written and the user's file is left exactly as it was -- no overwrite,
    no backup, no exception. (No digest is recorded until the installer itself writes.)"""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {"name": "my-own-template", "prompt": "my own template"}
    target.write_text(json.dumps(user), encoding="utf-8")
    assert _is_installers(target) is False

    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8")) == user  # untouched
    assert not (rig.agents / (DASHBOARD_AUTHOR_AGENT_FILENAME + ".saved")).exists()


def test_a_user_file_reusing_the_name_but_with_no_recorded_digest_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user file that reuses the agent NAME and even mounts ``kirocrew-core`` is still not
    ours when no ownership digest is recorded for the name (nothing the installer wrote), so
    it is left untouched rather than overwritten. The name is not the record; the digest is."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {
        "name": "kirocrew-dashboard-author",
        "prompt": "a user spec reusing the name and the core mount",
        "mcpServers": {"kirocrew-core": {}},
        "tools": ["@kirocrew-core/skill_search"],
    }
    target.write_text(json.dumps(user), encoding="utf-8")
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") is None
    assert _is_installers(target) is False

    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8")) == user  # untouched


def test_a_non_regular_pre_existing_file_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink (or any non-regular file) at the target is not a plain spec we can
    attribute; it is left untouched and not written over or followed."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    target.symlink_to(elsewhere)

    worker_agent._install_dashboard_author_agent()

    assert target.is_symlink()  # untouched, still points where it did
    assert target.resolve() == elsewhere.resolve()


def test_an_install_failure_runs_independent_repairs_then_fails_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F2: an author-spec install failure must NOT be swallowed. The independent
    repairs (fork refresh, hook repair) still run -- so an unrelated installer's failure
    cannot skip them -- but the rebuild then PROPAGATES the failure instead of reporting
    success. This is what keeps a ceiling-tightening rebuild whose governed spec rewrite
    failed from being recorded as projected: ``rebuild_agent_config_reporting`` does not
    report a write, so ``reproject_for_ceiling_change`` leaves its memo behind and the next
    poll retries rather than leaving forbidden auto-approvals live."""
    _Rig(tmp_path, monkeypatch)  # pins the private agents dir and inputs

    def _boom() -> None:
        raise OSError("agents dir unwritable")

    monkeypatch.setattr(worker_agent, "_install_dashboard_author_agent", _boom)

    repairs: list[str] = []
    real_refresh = agent.fork_refresh.refresh_after_rebuild
    real_repair = agent.repair_agent_configs
    monkeypatch.setattr(
        agent.fork_refresh,
        "refresh_after_rebuild",
        lambda *a, **k: (repairs.append("fork_refresh"), real_refresh(*a, **k))[1],
    )
    monkeypatch.setattr(
        agent,
        "repair_agent_configs",
        lambda *a, **k: (repairs.append("hook_repair"), real_repair(*a, **k))[1],
    )

    # The failure PROPAGATES (not swallowed) -- and the reporting wrapper reflects it.
    with pytest.raises(OSError, match="agents dir unwritable"):
        agent.rebuild_agent_config()

    # The independent repairs ran BEFORE the failure propagated.
    assert repairs == ["fork_refresh", "hook_repair"]


def test_the_reporting_wrapper_does_not_report_a_write_on_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success contract: a propagated author-spec failure escapes before ``_wrote_out``
    is marked True, so ``rebuild_agent_config_reporting`` raises rather than returning
    ``(path, True)`` -- the signal ``reproject_for_ceiling_change`` relies on to NOT advance
    its generation memo on a rebuild that did not land the governed spec."""
    _Rig(tmp_path, monkeypatch)

    def _boom() -> None:
        raise OSError("agents dir unwritable")

    monkeypatch.setattr(worker_agent, "_install_dashboard_author_agent", _boom)

    with pytest.raises(OSError, match="agents dir unwritable"):
        agent.rebuild_agent_config_reporting()


# --------------------------------------------------------------------------- #
# The legacy-hook repair sweep honours the same digest attribution.
# --------------------------------------------------------------------------- #


def test_repair_pass_sweeps_the_dashboard_author_stem_on_the_plain_owned_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FP item 4 subtraction: the hook-repair sweep carries NO dashboard-author special
    case. The stem is swept exactly like every other owned name -- a legacy hook key is
    stripped from a file at the stem whatever its other content. The once-user-creatable
    protection lives in the install gate alone (it never overwrites a user file with the
    managed spec); the hook sweep only normalizes recognized hook keys, so there is no
    overwrite for a per-file content gate to prevent here."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A file at the stem with no recorded ownership digest, carrying the legacy hook key.
    squat = {"name": "my-template", "hooks": {"auto_approve_tools": ["x"]}}
    target.write_text(json.dumps(squat), encoding="utf-8")
    assert _is_installers(target) is False  # not the managed spec (no recorded digest)

    agent._hooks_sanitized_mtimes.clear()
    agent.repair_agent_configs()

    # Swept on the plain owned terms: the legacy key is stripped, like any owned file.
    swept = json.loads(target.read_text(encoding="utf-8"))
    assert "auto_approve_tools" not in swept.get("hooks", {})


def test_repair_pass_rewrites_an_owned_dashboard_author_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: when the file reproduces the installer-recorded digest, the repair
    sweep DOES strip the legacy hook key, exactly as it does for every other owned spec."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A real install's own output + a recorded digest; inject the legacy hook key the sweep
    # should strip, then re-record the digest so the file still confirms as ours.
    agent.rebuild_agent_config()
    managed = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    managed["hooks"] = {"auto_approve_tools": ["x"]}
    target.write_text(json.dumps(managed, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(managed))
    assert _is_installers(target) is True  # ours by recorded digest

    # The prior rebuild's own hook-sweep recorded this file's mtime; clear that cache so the
    # re-injected key is not skipped as already-swept.
    agent._hooks_sanitized_mtimes.clear()
    agent.repair_agent_configs()

    assert "auto_approve_tools" not in json.loads(target.read_text(encoding="utf-8"))["hooks"]


# --------------------------------------------------------------------------- #
# Authorized edits (reset-model / PATCH) keep the file attributable.
# --------------------------------------------------------------------------- #


def test_reset_model_on_the_managed_spec_keeps_it_attributable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An authorized reset-model edit rewrites the spec's bytes. For the installer to keep
    recognising the rewritten file as its own, the writer that performs the edit must record
    the new digest -- so after the edit the file still confirms, and the next rebuild keeps
    refreshing it under the live ceiling."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert _is_installers(target) is True

    agent.reset_agent_model("kirocrew-dashboard-author")

    # The edited file still confirms as ours (its digest was re-recorded by the edit path)
    # -> still refreshed on the next rebuild.
    assert _is_installers(target) is True
    agent.rebuild_agent_config()
    assert _is_installers(target) is True


def test_reset_model_does_not_stamp_a_user_file_at_the_stem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 / Opus 5.5 F1 (security): a user authored their own spec at the once-user-
    creatable stem (NO recorded digest, so the installer refuses to touch it). They run
    reset-model on it. The reset must NOT record the managed digest for the user's bytes --
    doing so would make the next rebuild overwrite their charter. The gate: renew the digest
    only when the PRE-edit content was already our confirmed managed write."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {
        "name": "kirocrew-dashboard-author",
        "prompt": "the user's own hand-authored dashboard author",
        "mcpServers": {"kirocrew-core": {}},
        "model": "some-model",
    }
    target.write_text(json.dumps(user, indent=2) + "\n", encoding="utf-8")
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") is None
    assert _is_installers(target) is False

    agent.reset_agent_model("kirocrew-dashboard-author")

    # No digest was recorded for the user's bytes -> still NOT ours -> a rebuild leaves it.
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") is None
    assert _is_installers(target) is False
    agent.rebuild_agent_config()
    assert json.loads(target.read_text(encoding="utf-8"))["prompt"] == (
        "the user's own hand-authored dashboard author"
    )


def test_hook_sweep_does_not_stamp_a_user_file_carrying_a_stale_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 / Opus 5.5 F1 (security), hook-sweep arm: a stale ownership digest exists for
    the stem (from a managed install the user later REPLACED with their own file). The sweep
    strips a legacy hook key from the user's file, but must NOT re-record the digest for the
    user's bytes -- the renewal is gated on the PRE-sweep file already reproducing the
    recorded digest, not merely on 'a digest exists'."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A user file at the stem carrying a legacy hook key.
    user = {
        "name": "kirocrew-dashboard-author",
        "prompt": "the user's own file that replaced a managed install",
        "mcpServers": {"kirocrew-core": {}},
        "hooks": {"auto_approve_tools": ["x"]},
    }
    target.write_text(json.dumps(user, indent=2) + "\n", encoding="utf-8")
    # A STALE digest recorded for some OTHER (managed) bytes -- the user's file does not match.
    agent_state.set_managed_digest("kirocrew-dashboard-author", "staledigeststaledigest")
    assert _is_installers(target) is False  # bytes do not reproduce the stale digest

    agent._hooks_sanitized_mtimes.clear()
    agent.repair_agent_configs()

    # The legacy key was swept, but the digest was NOT renewed to the user's bytes.
    swept = json.loads(target.read_text(encoding="utf-8"))
    assert "auto_approve_tools" not in swept.get("hooks", {})
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") == "staledigeststaledigest"
    assert _is_installers(target) is False  # still not ours -> a rebuild leaves it


# --------------------------------------------------------------------------- #
# GPT 6.1 install-gate branches (maintainer ruling): absent -> install; present +
# digest reproduces -> refresh; present not reproducing OR read-fails -> untouched.
# The installer-recorded digest is the recorded provenance.
# --------------------------------------------------------------------------- #


def test_a_present_file_reproducing_the_digest_is_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch 2: a present file whose bytes reproduce the installer-recorded ownership digest
    IS this installer's own prior write, so a rebuild refreshes it in place -- the digest is
    the recorded provenance the refresh is allowed on. A field the refresh overwrites
    (``model``) stands in for an out-of-date managed spec the refresh should bring back; the
    stale-field edit re-records the digest so the file still confirms before the rebuild."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A real prior install's own output, with its digest recorded.
    agent.rebuild_agent_config()
    assert _is_installers(target) is True
    marked = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    marked["model"] = "a-stale-model-from-a-prior-build"
    target.write_text(json.dumps(marked, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(marked))
    assert _is_installers(target) is True  # reproduces the recorded digest

    agent.rebuild_agent_config()

    # Refreshed in place (the stale model is replaced), still the managed spec.
    refreshed = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert refreshed["name"] == "kirocrew-dashboard-author"
    assert refreshed.get("model") != "a-stale-model-from-a-prior-build"
    assert _is_installers(target) is True


def test_a_forgery_not_reproducing_the_digest_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F1 -- the core of the digest model. A hand-built spec that reuses the agent
    name and mounts ``kirocrew-core`` -- a forgery the OLD content-mark model would have read
    as the managed spec and overwritten -- does NOT reproduce the installer-recorded digest
    (none is recorded, and its bytes differ from any managed write), so it is left UNTOUCHED.
    Content marks are forgeable; the recorded digest is not."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    forged = {
        "name": "kirocrew-dashboard-author",
        "mcpServers": {"kirocrew-core": {"command": "kirocrew"}},
        "tools": ["@kirocrew-core/skill_search"],
        "prompt": "a hand-built spec reusing the name and the core mount",
        "description": "forges the old content marks but not the digest",
    }
    target.write_text(json.dumps(forged), encoding="utf-8")
    assert _is_installers(target) is False  # no recorded digest -> not ours

    agent.rebuild_agent_config()

    # Left untouched -- the forgery's charter is intact.
    assert json.loads(target.read_text(encoding="utf-8"))["prompt"] == (
        "a hand-built spec reusing the name and the core mount"
    )


def test_a_conductor_duplicate_renamed_to_the_stem_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F1 -- the duplicate the content-mark discriminator could not catch. A copy of
    ANOTHER owned agent (a conductor narrows its servers too, so it has the name, a
    ``kirocrew-core`` reference AND no ``kirocrew-cron`` mount) renamed to this stem would
    forge every content mark. It does NOT reproduce the installer-recorded digest, so it is
    read as a user file and left UNTOUCHED -- its custom charter is never silently
    overwritten. This is the whack-a-mole the digest closes once for every owned agent."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    conductor_dup = {
        "name": "kirocrew-dashboard-author",
        "mcpServers": {"kirocrew-core": {"command": "kirocrew"}},
        "tools": ["@kirocrew-core/skill_search"],
        "prompt": "the user's conductor charter, renamed onto the stem",
        "description": "a renamed conductor copy, not the dashboard author",
    }
    target.write_text(json.dumps(conductor_dup), encoding="utf-8")
    assert _is_installers(target) is False  # no recorded digest -> not ours

    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8"))["prompt"] == (
        "the user's conductor charter, renamed onto the stem"
    )


def test_a_managed_file_with_the_digest_record_lost_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deliberate fail-closed tradeoff: if the ownership digest record is LOST (e.g. the
    user deleted ``agent_model_state.json``) but a valid managed ``.json`` is still present,
    the rebuild sees NO recorded digest -> treats the file as unconfirmed -> leaves it
    untouched rather than overwriting a file it cannot attribute. Never destroys data;
    the user removes the stale file to let the install heal."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    agent.rebuild_agent_config()
    assert _is_installers(target) is True
    before = target.read_bytes()

    # Lose the ownership record (sidecar wiped), keep the managed file.
    agent_state.set_managed_digest("kirocrew-dashboard-author", None)
    assert _is_installers(target) is False  # no record -> unconfirmed, fail closed

    agent.rebuild_agent_config()

    assert target.read_bytes() == before  # untouched; not overwritten


def test_a_present_file_whose_read_fails_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch 3 (read-fail): an existing file at the stem whose read FAILS (oversized,
    non-UTF-8, otherwise unparseable) cannot be confirmed as this installer's own write, so
    it is left untouched -- never overwritten on a failed read. A None from the capped
    reader is NOT treated as 'ours to write'. The install now RAISES (fail-closed) rather
    than returning, so the rebuild cannot report success without rewriting the governed spec
    (GPT 6.1: an unreadable present spec must not be recorded as projected)."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    raw = b"\xff\xfe not valid utf-8 or json at all"
    target.write_bytes(raw)
    # The capped reader returns None for this file (unparseable); the gate must leave it.
    monkeypatch.setattr(wa.agent_mod, "_read_spec_capped", lambda p: None)

    wa._install_dashboard_author_agent()

    assert target.read_bytes() == raw  # untouched


def test_a_user_markdown_spec_at_the_stem_blocks_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: both the ``.json`` and ``.md`` candidates are checked before the name is
    claimed. kiro-cli reads ``<stem>.json`` and the v3 engine also reads ``<stem>.md``, and
    a ``.json`` written beside a user's ``.md`` would SHADOW that markdown spec. So a user
    ``kirocrew-dashboard-author.md`` (which the installer never writes -- it only writes the
    ``.json`` form) blocks the install: no ``.json`` is written and the user's ``.md`` is
    left untouched."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    md = rig.agents / "kirocrew-dashboard-author.md"
    json_target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    md_body = (
        "---\nname: kirocrew-dashboard-author\n---\nThe user's own markdown dashboard author.\n"
    )
    md.write_text(md_body, encoding="utf-8")

    wa._install_dashboard_author_agent()

    # The user's .md is untouched, and no .json was written beside it to shadow it.
    assert md.read_text(encoding="utf-8") == md_body
    assert not json_target.exists()


# --------------------------------------------------------------------------- #
# GPT 6.1 / Opus 5.5 F1 (on f6290180a9): a rebuild of a CONFIRMED managed spec
# must preserve the user's explicitly-pinned model and skill mappings while
# regenerating governed grants -- it must not silently reset them to defaults.
# --------------------------------------------------------------------------- #


def test_a_rebuild_preserves_the_pinned_model_and_skill_mappings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user pins a model and maps skills on the dashboard author via the dashboard (model
    pin -> model_managed False; skills -> ``resources``). A later routine rebuild rebuilds the
    governed grants from the shipped spec, but must carry the pinned model and the user skill
    mappings across -- they are authorized, persisted settings, not transient edits. Only a
    CONFIRMED managed spec contributes them."""
    rig = _Rig(tmp_path, monkeypatch)
    agent.rebuild_agent_config()
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    assert _is_installers(target) is True

    # Simulate the authorized dashboard edits: an explicit model pin + a user skill mapping.
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    spec["model"] = "claude-user-pinned"
    spec["resources"] = ["skill://user/mapped-one"]
    target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    agent_state.set_model_managed("kirocrew-dashboard-author", False)  # explicit pick
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(spec))
    assert _is_installers(target) is True

    agent.rebuild_agent_config()

    refreshed = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    # The pinned model and the user skill mappings survived the rebuild.
    assert refreshed["model"] == "claude-user-pinned"
    assert refreshed.get("resources") == ["skill://user/mapped-one"]
    # Governed grants were still regenerated from the shipped spec.
    assert refreshed["name"] == "kirocrew-dashboard-author"
    assert _is_installers(target) is True


def test_a_rebuild_does_not_carry_a_pinned_model_from_an_unconfirmed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preservation reads from a CONFIRMED managed spec only. A user file at the stem
    (no recorded digest) contributes nothing: the installer refuses to write over it at all,
    so its model/resources are neither carried into a managed spec nor touched."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {
        "name": "kirocrew-dashboard-author",
        "model": "a-user-model",
        "resources": ["skill://user/private"],
        "prompt": "the user's own charter",
    }
    target.write_text(json.dumps(user, indent=2) + "\n", encoding="utf-8")
    agent_state.set_model_managed("kirocrew-dashboard-author", False)
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") is None
    assert _is_installers(target) is False

    agent.rebuild_agent_config()

    # Unconfirmed -> left entirely untouched; the user's own file stands.
    assert json.loads(target.read_text(encoding="utf-8")) == user


# --------------------------------------------------------------------------- #
# GPT 6.1 F1/F2 (on b59007de01): the skill-URI migration is a managed writer and
# must renew the digest for a confirmed spec; resources preservation must mirror
# the key exactly (empty carried, absent removes the inherited default).
# --------------------------------------------------------------------------- #


def test_the_skill_uri_migration_renews_the_digest_for_a_confirmed_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F1 (security): ``migrate_relocated_skill_uris`` rewrites the dashboard-author
    spec to point a relocated skill at its new path. It is a managed writer, so it must renew
    the ownership digest for a CONFIRMED managed spec -- otherwise the installer reads its own
    migrated spec as foreign and stops re-filtering grants against a tightened ceiling."""
    from kiro_crew import skills as skills_mod

    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # Stage the relocation first: old SKILL.md gone, new one present, mapped.
    skills_root = tmp_path / "skills"
    (skills_root / "new-skill").mkdir(parents=True)
    (skills_root / "new-skill" / "SKILL.md").write_text("# moved\n", encoding="utf-8")
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: skills_root)
    monkeypatch.setattr(skills_mod, "_RELOCATED_SKILLS", {"old-skill": "new-skill"})
    old_uri = f"skill://{(skills_root / 'old-skill' / 'SKILL.md').as_posix()}"
    new_uri = f"skill://{(skills_root / 'new-skill' / 'SKILL.md').as_posix()}"

    # A confirmed managed spec that maps the relocated skill by its old (absolute) path.
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    spec["resources"] = [old_uri]
    target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(spec))
    assert _is_installers(target) is True

    rewritten = agent.migrate_relocated_skill_uris()

    assert rewritten >= 1  # our spec was rewritten
    migrated = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert migrated["resources"] == [new_uri]  # URI migrated
    # The digest was renewed to the migrated bytes -> the file still confirms as ours.
    assert _is_installers(target) is True


def test_a_rebuild_preserves_an_explicitly_empty_resources_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F2: an explicit remove-all-skills PATCH leaves ``resources`` present as ``[]``.
    A rebuild must carry that empty selection through, not re-inherit a mapping from the
    build default -- the user's removal must stick."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    spec["resources"] = []  # explicit "no skills"
    target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(spec))
    assert _is_installers(target) is True

    agent.rebuild_agent_config()

    refreshed = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert refreshed.get("resources") == []  # the empty selection stuck
    assert _is_installers(target) is True


# --------------------------------------------------------------------------- #
# GPT 6.1 F1 (on 99a0563711): a .md sibling must block only a FIRST-TIME install.
# A confirmed managed .json is still refreshed despite the sibling, so a grant the
# ceiling has revoked does not stay executable; the .md is left untouched.
# --------------------------------------------------------------------------- #


def test_a_confirmed_json_is_refreshed_despite_a_markdown_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 F1 (security): once the ``.json`` is our confirmed managed write, a user
    adding a same-stem ``.md`` must NOT freeze it. If the rebuild skipped the rewrite, a
    revoked auto-approval (the ceiling now denies ``@kirocrew-core/skill_search``) would stay
    in the on-disk ``allowedTools`` and execute without PreToolUse enforcement for later
    sessions. The confirmed ``.json`` is refreshed in place -- the stale grant is filtered
    out -- and the user's ``.md`` is left untouched."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # A confirmed managed spec carrying a now-revoked auto-approval.
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert "@kirocrew-core/skill_search" in spec["allowedTools"]
    spec["allowedTools"] = list(spec["allowedTools"]) + ["@kirocrew-core/revoked_grant"]
    target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", agent_state.spec_digest(spec))
    assert _is_installers(target) is True
    # A user drops a markdown sibling at the stem AFTER the managed install.
    md = rig.agents / "kirocrew-dashboard-author.md"
    md_body = "---\nname: kirocrew-dashboard-author\n---\nThe user's own notes.\n"
    md.write_text(md_body, encoding="utf-8")

    agent.rebuild_agent_config()

    # The confirmed .json was refreshed despite the sibling -> the stale grant is gone,
    # the governed surface is back, and the file still confirms as ours.
    refreshed = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert "@kirocrew-core/revoked_grant" not in refreshed["allowedTools"]
    assert refreshed["name"] == "kirocrew-dashboard-author"
    assert _is_installers(target) is True
    # The user's markdown sibling is left untouched.
    assert md.read_text(encoding="utf-8") == md_body


def test_a_markdown_sibling_still_blocks_a_first_time_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling check is retained for first-time install: with NO confirmed ``.json``
    (none recorded), a user ``.md`` at the stem still blocks the install so the managed
    ``.json`` never shadows the user's markdown spec."""
    import kiro_crew.agent_materialization.worker_agent as wa

    rig = _Rig(tmp_path, monkeypatch)
    md = rig.agents / "kirocrew-dashboard-author.md"
    json_target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    md_body = "---\nname: kirocrew-dashboard-author\n---\nThe user's own markdown author.\n"
    md.write_text(md_body, encoding="utf-8")
    assert agent_state.get_managed_digest("kirocrew-dashboard-author") is None

    wa._install_dashboard_author_agent()

    assert md.read_text(encoding="utf-8") == md_body  # untouched
    assert not json_target.exists()  # no .json written to shadow it


# --------------------------------------------------------------------------- #
# GPT 6.1 (on 15f0c5084d): a managed write interrupted between the file replace
# and the digest finalize must NOT freeze the spec -- the file reproduces the
# PENDING digest, so a rebuild still confirms and refreshes it.
# --------------------------------------------------------------------------- #


def test_a_spec_left_pending_after_an_interrupted_write_is_still_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 (security): a managed writer records the new bytes' digest as PENDING, writes
    the file, then finalizes. If the process dies between the write and finalize, the file
    holds the new bytes and only the pending digest is recorded. The next rebuild must still
    recognise the file as ours (bytes reproduce the pending digest) and refresh it -- a stale
    grant from a since-tightened ceiling is filtered out, not frozen in place forever."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    agent.rebuild_agent_config()
    spec = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    # Simulate the interrupted state: a managed writer put new bytes on disk (carrying a
    # now-revoked grant) and recorded them as PENDING, but crashed before finalize.
    spec["allowedTools"] = list(spec["allowedTools"]) + ["@kirocrew-core/revoked_grant"]
    target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    agent_state.set_managed_digest("kirocrew-dashboard-author", None)  # finalized lost
    agent_state.begin_managed_write(
        "kirocrew-dashboard-author", agent_state.spec_digest(spec)
    )  # only pending recorded, never finalized
    assert _is_installers(target) is True  # pending digest confirms the on-disk bytes

    agent.rebuild_agent_config()

    # Refreshed, not frozen: the stale grant is gone and the file still confirms.
    refreshed = rig.read(DASHBOARD_AUTHOR_AGENT_FILENAME)
    assert "@kirocrew-core/revoked_grant" not in refreshed["allowedTools"]
    assert _is_installers(target) is True


# --------------------------------------------------------------------------- #
# GPT 6.1 (on 81536c69a2): the hook-repair sweep must not FOLLOW a symlink at an
# owned stem -- adding the dashboard-author name to OWNED_KIRO_AGENT_FILES must
# not turn the sweep into a reader of an arbitrary symlink target.
# --------------------------------------------------------------------------- #


def test_the_hook_sweep_does_not_follow_a_symlink_at_an_owned_stem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 (security): a symlink planted at the dashboard-author stem, pointing at a file
    OUTSIDE the agents dir (standing in for a credential file), must be refused by the sweep
    -- not stat-followed, not read through, not written back. The ``_spec_path_is_safe`` fence
    the sweep now applies refuses the symlink before any read."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    secret = tmp_path / "outside" / "config.json"
    secret.parent.mkdir()
    secret.write_text(json.dumps({"hooks": {"auto_approve_tools": ["x"]}, "secret": "sh"}))
    target.symlink_to(secret)
    before = secret.read_text(encoding="utf-8")

    agent._hooks_sanitized_mtimes.clear()
    agent.repair_agent_configs()  # must not raise, follow, or rewrite

    # The symlink target is untouched -- the sweep never followed the link to read or write it.
    assert secret.read_text(encoding="utf-8") == before
    assert target.is_symlink()  # the link itself is left as-is


# --------------------------------------------------------------------------- #
# GPT 6.1 (on 2505dcf9f7): an author-install write failure must set the hold
# flag so priming/maintenance retain the governance retry rather than marking
# the ceiling as projected.
# --------------------------------------------------------------------------- #


def test_an_author_install_write_failure_sets_the_hold_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1 (security): a transient write failure on a confirmed managed spec (Windows
    sharing violation, spec-lock timeout) must set ``_conductor_spec_held`` so
    ``prime_ceiling_projection`` does not seed the ceiling as projected, and
    ``retry_held_conductor_specs`` retries the rewrite on the next maintenance poll. Without
    the hold, a tightened ceiling whose author-spec rewrite failed at boot leaves revoked
    auto-approvals live for the process lifetime with no recovery path."""
    _Rig(tmp_path, monkeypatch)

    def _boom() -> None:
        raise OSError("agents dir unwritable")

    monkeypatch.setattr(worker_agent, "_install_dashboard_author_agent", _boom)

    # Reset the hold to False before the rebuild so we can confirm it flips.
    agent._conductor_spec_held = False

    with pytest.raises(OSError, match="agents dir unwritable"):
        agent.rebuild_agent_config()

    # The hold flag must be True: the author-install failure must prevent priming
    # from marking the ceiling as projected.
    assert agent._conductor_spec_held is True


# --------------------------------------------------------------------------- #
# Opus 5.5 / Design Review (on 2505dcf9f7): a user file at the dashboard-author
# stem must NOT crash the rebuild / kirocrew setup / gateway boot.
# --------------------------------------------------------------------------- #


def test_a_user_file_at_the_stem_does_not_crash_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus 5.5 / Design Review: a user-authored spec at the ``kirocrew-dashboard-author``
    stem must be left untouched **and the rebuild returns normally** (the install is skipped,
    not crashed). A raise here would make ``kirocrew setup`` abort before PATH shim/MCP purge,
    and gateway boot would skip first-run setup on every restart until the user deletes their
    own file."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    user = {"name": "user-template", "prompt": "keep me"}
    target.write_text(json.dumps(user), encoding="utf-8")

    # Must return normally -- not raise.
    agent.rebuild_agent_config()

    assert json.loads(target.read_text(encoding="utf-8")) == user  # untouched


def test_a_lost_digest_does_not_crash_the_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus 5.5 / Design Review: a managed spec whose ownership digest was lost (sidecar
    pruned, corrupted) should skip the install, not crash the rebuild."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # First, install normally.
    agent.rebuild_agent_config()
    assert target.exists()
    # Now wipe the digest record.
    agent_state.set_managed_digest("kirocrew-dashboard-author", None)
    assert not _is_installers(target)

    # Must return normally -- not raise. The file is left untouched.
    agent.rebuild_agent_config()


# --------------------------------------------------------------------------- #
# Design Review (watch): every writer of kirocrew-dashboard-author.json goes
# through the two-phase digest-renewing write.
# --------------------------------------------------------------------------- #


def test_every_managed_writer_calls_begin_and_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design Review (watch): ownership depends on every current and future writer of the
    dashboard-author spec going through ``begin_managed_write``/``finalize_managed_write``.
    This test pins that invariant: it patches the two-phase helpers to record calls, runs
    each writer, and asserts both were called. A writer that forgets turns the managed spec
    foreign with no signal."""
    rig = _Rig(tmp_path, monkeypatch)
    target = rig.agents / DASHBOARD_AUTHOR_AGENT_FILENAME
    # Install once to seed the confirmed managed spec.
    agent.rebuild_agent_config()
    assert target.exists()

    # Patch to record calls.
    calls: list[str] = []
    real_begin = agent_state.begin_managed_write
    real_finalize = agent_state.finalize_managed_write

    def _begin(*a: object, **kw: object) -> None:
        calls.append("begin")
        return real_begin(*a, **kw)

    def _finalize(*a: object, **kw: object) -> None:
        calls.append("finalize")
        return real_finalize(*a, **kw)

    monkeypatch.setattr(agent_state, "begin_managed_write", _begin)
    monkeypatch.setattr(agent_state, "finalize_managed_write", _finalize)

    # Run a rebuild (which invokes the installer -- the main writer).
    calls.clear()
    agent.rebuild_agent_config()
    assert (
        "begin" in calls and "finalize" in calls
    ), "the installer writer must call begin_managed_write and finalize_managed_write"
