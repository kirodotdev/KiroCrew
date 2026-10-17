"""Agent-spec home isolation: the non-default data home arm.

An instance on its own ``KIROCREW_HOME`` writes the shared agents directory only
when no spec there belongs to another home: the specs it wrote itself, or a
directory with no spec yet. The tests pin how a spec's writer is read (self and
foreign pins; UNC, case-differing and resolution-equivalent spellings; symlinked,
oversized, non-regular, growing and malformed specs; custom server entries; mixed
pins), that a rebuild keeps its own specs and rechecks ownership under its lock,
and that the default home, and an override equal to it, still own the shared
write. The KIRO_HOME seam, the worktree decline guard and the anti-regression guard
are in ``test_agent_home_isolation.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_home_isolation_helpers import _isolate_default_home_fixture  # noqa: F401
from agent_home_isolation_helpers import _pin_default_home_and_breadcrumb  # noqa: F401
from agent_home_isolation_helpers import (
    _capture_sel,
    _durable_checkout,
    _pretend_target_is_shared,
    _spec_pinned_to,
)


# --------------------------------------------------------------------------
# The non-default data home arm
# --------------------------------------------------------------------------
def test_override_home_declines_shared_write_even_from_durable_checkout(monkeypatch, tmp_path):
    """A KIROCREW_HOME-override instance must not overwrite existing shared
    specs.

    The reported writer was a DURABLE checkout — not a worktree, not a temp
    clone, not a pod — booted with a scratch ``KIROCREW_HOME``. Its rebuild
    pinned that home into every managed server entry of ``~/.kiro/agents``, so
    every stub spawned afterwards resolved ``config_dir()`` to a home the real
    gateway never writes and strict identity failed closed in ALL sessions.
    Ephemerality arms never see this shape; the data-home arm must.
    """
    from kiro_crew import agent

    events = _capture_sel(monkeypatch, agent)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # An existing spec is the audience being protected: the arm declines only
    # when there is something to preserve, mirroring the temp arm's rule.
    (shared / agent.AGENT_FILENAME).write_text("{}", encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert (
        agent._decline_shared_agent_home() is not None
    ), "an override-home instance was handed the shared agent home"

    denied = [e for e in events if e.get("outcome") == "denied"]
    assert len(denied) == 1, f"expected exactly one denied event, got {events}"
    assert denied[0]["operation"] == "agent_home_write"
    assert "non-default data home" in denied[0]["error"]


def test_override_home_with_no_existing_spec_still_writes(monkeypatch, tmp_path):
    """A relocated-home install on a spec-less machine is NOT refused.

    With no shared spec present there is no default-home audience to poison,
    and refusing would leave the install with no spec at all — no boot error
    (``missing_required_agent_specs`` is suppressed by the same guard) and
    every turn dying at "Mode 'kirocrew' not found". Same precondition as the
    temp arm: decline only when there is something to preserve.
    """
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-home"))
    _durable_checkout(monkeypatch, agent)
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

    assert agent._decline_shared_agent_home() is None


def test_override_home_declines_when_only_a_sibling_spec_exists(monkeypatch, tmp_path):
    """A surviving SIBLING spec is enough audience to refuse for.

    ``kirocrew.json`` and its siblings are written by separate installers, so a
    deleted main with a surviving ``kirocrew-lite.json`` is a reachable state.
    A main-only precondition would wave the rebuild through and overwrite the
    surviving siblings with the foreign home — the same poison, one file over.
    The precondition therefore covers every ``OWNED_KIRO_AGENT_FILES`` entry.
    """
    from kiro_crew import agent
    from kiro_crew.agent_files import LITE_AGENT_FILENAME

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # Only a sibling survives; the main spec is absent.
    (shared / LITE_AGENT_FILENAME).write_text("{}", encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert (
        agent._decline_shared_agent_home() is not None
    ), "a surviving sibling spec was overwritten by an override-home rebuild"


def test_self_pinned_specs_keep_their_writer(monkeypatch, tmp_path):
    """An override-home install keeps refreshing the specs IT wrote.

    The specs carry their writer's home in every managed server's
    ``env.KIROCREW_HOME``. An ownership-blind existence check would read this
    instance's own first write as "someone's specs" and refuse every later
    rebuild — a self-lockout where the install's specs go permanently stale.
    Provenance matching is what lets it keep writing.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "relocated-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(own_home), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is None


def test_foreign_pinned_specs_refuse(monkeypatch, tmp_path):
    """Specs pinned to a DIFFERENT home are someone else's — refuse."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(
        _spec_pinned_to(tmp_path / "someone-elses-home"), encoding="utf-8"
    )
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_unc_pin_refuses_without_touching_the_filesystem(monkeypatch, tmp_path):
    """A recorded pin is compared, never interpreted — no OS call sees it.

    On Windows, resolving a UNC-valued pin (``\\\\attacker\\share\\...``) fires
    outbound SMB authentication to the named host — spec content choosing a
    network destination. The provenance check must decide ownership by string
    comparison alone, so the hostile value must never reach ``resolve()``,
    ``expanduser()``, ``stat()``, ``realpath()`` or any other filesystem
    interpretation. Every guarded primitive asserts, so a future refactor
    that routes the value through a different OS call fails here rather than
    reopening the class.
    """
    import os as os_mod
    from pathlib import Path

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(
        _spec_pinned_to(r"\\attacker\share\kirocrew-home"), encoding="utf-8"
    )
    _pretend_target_is_shared(monkeypatch, agent, shared)

    def _guard(real, label):
        def guarded(*args, **kwargs):
            for arg in args:
                assert "attacker" not in str(arg), f"spec content reached {label}"
            return real(*args, **kwargs)

        return guarded

    monkeypatch.setattr(os_mod, "stat", _guard(os_mod.stat, "os.stat"))
    monkeypatch.setattr(os_mod, "lstat", _guard(os_mod.lstat, "os.lstat"))
    monkeypatch.setattr(os_mod.path, "realpath", _guard(os_mod.path.realpath, "os.path.realpath"))
    monkeypatch.setattr(Path, "resolve", _guard(Path.resolve, "Path.resolve"))
    monkeypatch.setattr(Path, "expanduser", _guard(Path.expanduser, "Path.expanduser"))
    monkeypatch.setattr(Path, "stat", _guard(Path.stat, "Path.stat"))
    monkeypatch.setattr(Path, "exists", _guard(Path.exists, "Path.exists"))

    # Direct: the provenance reader itself judges the pin foreign.
    assert agent._existing_specs_are_mine(shared, own_home) is False
    # Integration: the guard consumes that verdict and refuses the write.
    assert agent._decline_shared_agent_home() is not None


def test_symlinked_spec_is_present_and_unvouchable(monkeypatch, tmp_path):
    """A symlink at a spec path reads as a refusal, not as absence.

    ``exists()`` follows symlinks, so a dangling planted link would read as
    "no spec here" — an absence verdict an attacker can manufacture in the
    same-uid agents directory, flipping the guard to a fresh write. The
    no-follow probe sees it, and the :func:`_spec_path_is_safe` fence every
    other spec reader in the module applies refuses to read through it.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # Dangling symlink: exists() would say "absent", lexists says "present".
    (shared / agent.AGENT_FILENAME).symlink_to(tmp_path / "does-not-exist")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_case_differing_pin_reads_foreign(monkeypatch, tmp_path):
    """The comparison is case-preserving — a case-folded match is fail-open.

    ``normcase`` would fold two homes that differ only by letter case (legal
    and distinct on case-sensitive filesystems, including case-sensitive NTFS
    directories) into one, reading the other home's spec as this instance's
    own. Distinct spellings must compare foreign; the writer and reader share
    one resolver, so a self-written pin never differs by case.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "CrewHome").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(
        _spec_pinned_to(str(own_home).lower()), encoding="utf-8"
    )
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_resolution_equivalent_pin_reads_foreign(monkeypatch, tmp_path):
    """Equivalence-by-resolution does not vouch — comparison is lexical.

    The writer pins ``str(_valid_override_home())``, which is already
    resolved, so a pin that only becomes this home after following a symlink
    was not written by this instance. Reading it as "mine" would require
    handing spec content to the filesystem — the interpretation the
    provenance check forbids — so it reads foreign and refuses, the
    conservative direction every unparseable shape already takes.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "real-home").resolve()
    own_home.mkdir()
    alias = tmp_path / "alias-home"
    alias.symlink_to(own_home)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # Resolution-equivalent spelling: same directory, different string.
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(alias), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_oversized_spec_refuses_before_parsing(monkeypatch, tmp_path):
    """The ownership probe is bounded — an over-cap spec refuses, unread.

    The boot-path rebuild can run on the event loop, so the probe must never
    be the place a pathological "spec" gets slurped into memory. A file over
    the per-spec cap refuses via lstat before any open — even when its
    CONTENT would vouch as this instance's own — the same conservative
    direction every unparseable shape takes. The sentinel proves the
    ordering: the reader is never invoked for an over-cap spec, so a
    refactor that reads first and checks later fails here, not in
    production.
    """
    import pytest as _pytest

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # A self-pinned spec whose content vouches, padded past the per-spec cap
    # (JSON tolerates trailing whitespace, so a parse WOULD succeed).
    padded = _spec_pinned_to(own_home) + " " * (agent._PROVENANCE_SPEC_CAP_BYTES + 1)
    (shared / agent.AGENT_FILENAME).write_text(padded, encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    def no_read(*args, **kwargs):
        _pytest.fail("the probe read an over-cap spec instead of refusing on lstat")

    monkeypatch.setattr(agent, "safe_read_file_bytes_nolink", no_read)
    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_non_regular_spec_refuses_without_opening(monkeypatch, tmp_path):
    """A FIFO at a spec path refuses via lstat — it is never opened.

    Opening a FIFO with no writer blocks indefinitely; on the boot-path
    rebuild that parks the gateway's event loop before readiness. The probe
    rejects non-regular files from the no-follow lstat, so the open never
    happens. The sentinels fail fast (instead of hanging the suite) if a
    regression routes the FIFO into either reader.
    """
    import stat as _stat

    import pytest as _pytest

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    spec_path = shared / agent.AGENT_FILENAME
    if hasattr(os, "mkfifo"):
        os.mkfifo(spec_path)
    else:
        # No FIFO primitive on this platform (Windows shards). The ratchet —
        # non-regular specs refuse from the no-follow lstat, never opened —
        # must still be verified here, so plant a regular file and present a
        # FIFO ``st_mode`` for it through ``lstat``, the exact evidence the
        # probe judges. Everything downstream of the mode check stays real.
        spec_path.write_text("{}", encoding="utf-8")
        real_lstat = Path.lstat

        def fifo_lstat(self, *args, **kwargs):
            st = real_lstat(self, *args, **kwargs)
            if self == spec_path:
                return os.stat_result((_stat.S_IFIFO | 0o600,) + tuple(st)[1:])
            return st

        monkeypatch.setattr(Path, "lstat", fifo_lstat)
    _pretend_target_is_shared(monkeypatch, agent, shared)

    def no_open(*args, **kwargs):
        _pytest.fail("the probe tried to open a non-regular spec (a FIFO parks the open)")

    monkeypatch.setattr(agent, "safe_read_file_bytes_nolink", no_open)
    monkeypatch.setattr(agent, "_read_spec_capped", no_open)
    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_spec_growing_after_lstat_fails_closed(monkeypatch, tmp_path):
    """A spec that outgrows the bound between lstat and read refuses.

    The descriptor reader raises ``FileTooLargeError`` when the opened file
    exceeds ``max_bytes`` — a concurrent writer can grow the file after the
    lstat passed. Memory stays capped either way; the probe must refuse
    (``False``) rather than let the raise abort the whole boot-path rebuild
    over a spec that changed underneath it.
    """
    from kiro_crew import agent
    from kiro_crew.hooks import FileTooLargeError

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(own_home), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    def grew(*args, **kwargs):
        raise FileTooLargeError("file grew past max_bytes")

    monkeypatch.setattr(agent, "safe_read_file_bytes_nolink", grew)
    assert agent._existing_specs_are_mine(shared, own_home) is False
    assert agent._decline_shared_agent_home() is not None


def test_dashboard_author_stem_is_always_skipped_in_the_home_probe(monkeypatch, tmp_path):
    """The dashboard-author stem must never drive the shared-home verdict, regardless of
    sidecar confirmation.

    Its installer pins no ``KIROCREW_HOME`` in the managed server entry on either branch --
    the installer's own write omits it, and a leftover user file at the stem never had one.
    So this stem can never contribute a legitimate home-pin signal: letting it into the
    probe would make a foreign/absent pin read as foreign and decline every rebuild. That
    strands the governance ceiling both for OUR OWN file (ours, pin-less) AND for an
    untouched USER leftover at the stem (whose presence must not refuse creating every
    other required spec). It is therefore excluded from the pin probe unconditionally; its
    provenance is the digest record enforced at the gated sites, not a home pin.
    """
    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    foreign = (tmp_path / "someone-elses-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    # Our own self-pinned spec + a foreign-pinned dashboard-author file at the stem.
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(str(own_home)), encoding="utf-8")
    da = shared / agent._DASHBOARD_AUTHOR_FILENAME
    da.write_text(_spec_pinned_to(str(foreign)), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    # The dashboard-author stem is excluded from the home-pin probe unconditionally (it
    # carries no KIROCREW_HOME pin on either branch), so the foreign-pinned file at the stem
    # never declines the rebuild -- the remaining self-pinned spec reads as ours.
    assert agent._existing_specs_are_mine(shared, own_home) is True


def test_unnormalized_spelling_of_own_pin_stays_mine(monkeypatch, tmp_path):
    """Lexical means normalized, not byte-identical.

    ``os.path.normpath`` folds redundant separators and dot segments — pure
    string work — so a pin differing only in that way still reads as this
    writer's. Anything beyond normalization (a symlink, a moved home) is
    interpretation and reads foreign.
    """
    import os

    from kiro_crew import agent

    own_home = (tmp_path / "relocated-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    unnormalized = f"{own_home}{os.sep}{os.curdir}"
    (shared / agent.AGENT_FILENAME).write_text(_spec_pinned_to(unnormalized), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is None


def test_malformed_specs_refuse(monkeypatch, tmp_path):
    """An unparseable spec proves nothing about ownership — refuse."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    (shared / agent.AGENT_FILENAME).write_text("{not json", encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_custom_server_entries_cannot_vouch_ownership(monkeypatch, tmp_path):
    """Only MANAGED entries carry the writer's signature.

    Custom server entries flow in from user configuration, so a pin inside one
    must not be read as provenance: a spec whose only matching pin lives in an
    unmanaged entry stays foreign.
    """
    import json

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "my-custom-server": {"command": "x", "env": {"KIROCREW_HOME": str(own_home)}}
        },
    }
    (shared / agent.AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_garbage_recorded_home_refuses_without_crashing(monkeypatch, tmp_path):
    """A garbage recorded value reads foreign, never raises.

    Spec content is not trusted input. The comparison is lexical, so a NUL
    byte never reaches path resolution at all: the value is refused by the
    explicit NUL guard (some platforms' C ``normpath`` can raise on it) and
    would compare unequal regardless — either way agent setup is never
    aborted by garbage in a spec.
    """
    import json

    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "my-home"))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    spec = {
        "name": "kirocrew",
        "mcpServers": {"kirocrew-core": {"command": "x", "env": {"KIROCREW_HOME": "bad\x00home"}}},
    }
    (shared / agent.AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None


def test_mixed_pins_within_one_spec_refuse(monkeypatch, tmp_path):
    """EVERY managed entry must vouch — one matching entry is not ownership.

    The writer pins all managed entries in one rebuild, so a spec whose
    entries disagree (one pinned to this home, another pinned elsewhere or
    not at all) was not written whole by this instance and must refuse.
    """
    import json

    from kiro_crew import agent

    own_home = (tmp_path / "my-home").resolve()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    shared.mkdir()
    spec = {
        "name": "kirocrew",
        "mcpServers": {
            "kirocrew-core": {"command": "x", "env": {"KIROCREW_HOME": str(own_home)}},
            "kirocrew-cron": {
                "command": "x",
                "env": {"KIROCREW_HOME": str(tmp_path / "someone-else")},
            },
        },
    }
    (shared / agent.AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, shared)

    assert agent._decline_shared_agent_home() is not None

    spec["mcpServers"]["kirocrew-cron"] = {"command": "x"}
    (shared / agent.AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    assert (
        agent._decline_shared_agent_home() is not None
    ), "an unpinned managed entry beside a pinned one must refuse"


def test_rebuild_round_trip_keeps_self_ownership(monkeypatch, tmp_path):
    """Specs written by the REAL writer round-trip as this instance's own.

    Writes via ``rebuild_agent_config()`` under an override home (fresh shared
    target), then asserts the same instance's guard still returns ``None`` —
    proving ``_managed_mcp_env``'s pin and the ownership read agree, rather
    than both being asserted against hand-fabricated fixtures.
    """
    from kiro_crew import agent

    own_home = tmp_path / "relocated-home"
    own_home.mkdir()
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    _pretend_target_is_shared(monkeypatch, agent, shared)

    written = agent.rebuild_agent_config()

    assert written == shared / agent.AGENT_FILENAME
    assert written.exists(), "a fresh relocated-home install must write its specs"
    assert (
        agent._decline_shared_agent_home() is None
    ), "the instance's own freshly written specs must read back as its own"
    # And through the real guard again: a landed write reports wrote=True,
    # exactly once per rebuild.
    reported, wrote = agent.rebuild_agent_config_reporting()
    assert reported == shared / agent.AGENT_FILENAME
    assert wrote is True, "a landed rebuild must report wrote=True"
    probe: list[bool] = []
    agent.rebuild_agent_config(_wrote_out=probe)
    assert probe == [True], "a landed rebuild must report exactly one True verdict"


def test_rebuild_rechecks_ownership_under_the_lock_before_its_write(monkeypatch, tmp_path):
    """A default-home gateway that writes the shared spec while an override-home
    rebuild is past its first check wins: the override rebuild re-asks under the
    shared agents-dir lock, reads the new owner's spec and writes nothing."""
    import json

    from kiro_crew import agent
    from kiro_crew.agent_materialization import auto_approve

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    own_home = tmp_path / "relocated-home"
    own_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(own_home))
    _durable_checkout(monkeypatch, agent)
    shared = tmp_path / "agents"
    _pretend_target_is_shared(monkeypatch, agent, shared)
    owner_spec = json.dumps({"name": "kirocrew", "mcpServers": {"kirocrew-cron": {"command": "x"}}})
    real_pass = auto_approve.final_ceiling_pass

    def owner_writes_first(config):
        # The other gateway lands its unpinned spec after this rebuild was admitted.
        shared.mkdir(parents=True, exist_ok=True)
        (shared / agent.AGENT_FILENAME).write_text(owner_spec, encoding="utf-8")
        return real_pass(config)

    monkeypatch.setattr(auto_approve, "final_ceiling_pass", owner_writes_first)
    probe: list[bool] = []

    returned = agent.rebuild_agent_config(_wrote_out=probe)

    assert returned == shared / agent.AGENT_FILENAME
    assert (shared / agent.AGENT_FILENAME).read_text(encoding="utf-8") == owner_spec
    assert probe == [False], "a rebuild refused at the re-check must report it did not write"


def test_rebuild_under_override_home_never_touches_shared_dir(monkeypatch, tmp_path):
    """``rebuild_agent_config`` under an override home must not rewrite an
    existing shared spec — the guard stops the write, not merely warns."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "scratch-home"))
    _durable_checkout(monkeypatch, agent)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    sentinel = '{"name": "kirocrew", "sentinel": "pre-existing"}'
    (agents_dir / agent.AGENT_FILENAME).write_text(sentinel, encoding="utf-8")
    _pretend_target_is_shared(monkeypatch, agent, agents_dir)

    returned = agent.rebuild_agent_config()

    assert returned == agents_dir / agent.AGENT_FILENAME
    assert (agents_dir / agent.AGENT_FILENAME).read_text(
        encoding="utf-8"
    ) == sentinel, "override-home rebuild must leave the existing shared spec byte-identical"


def test_default_home_instance_still_owns_shared_write(monkeypatch, tmp_path):
    """The other half of the contract: the DEFAULT-home instance keeps
    owning the shared specs — the new arm must not widen into refusing it."""
    from kiro_crew import agent

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    _durable_checkout(monkeypatch, agent)
    agents_dir = tmp_path / "agents"
    _pretend_target_is_shared(monkeypatch, agent, agents_dir)

    assert agent._decline_shared_agent_home() is None


def test_override_equal_to_default_home_still_owns_shared_write(monkeypatch, tmp_path):
    """A belt-and-braces ``KIROCREW_HOME=<default>`` export IS the default-home
    instance in substance; refusing it would leave its specs never refreshed."""
    from kiro_crew import agent
    from kiro_crew.config import paths

    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    default_home = (tmp_path / "default-home").resolve()
    monkeypatch.setattr(paths, "_resolved_home", default_home)
    monkeypatch.setenv("KIROCREW_HOME", str(default_home))
    _durable_checkout(monkeypatch, agent)
    _pretend_target_is_shared(monkeypatch, agent, tmp_path / "agents")

    assert agent._decline_shared_agent_home() is None


def test_shared_kiro_agents_writable_predicate(monkeypatch, tmp_path):
    """The paths-level predicate: POD refuses, override refuses, default allows."""
    from kiro_crew.config import paths

    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "override"))
    assert not paths.shared_kiro_agents_writable()

    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    assert paths.shared_kiro_agents_writable()

    monkeypatch.setenv("KIROCREW_POD", "1")
    assert not paths.shared_kiro_agents_writable(), "the settings-guard predicate is reused"
