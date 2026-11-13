"""The operator's read-only layer: ``agent.sandbox_readonly_paths`` and its exceptions.

An operator lists directories the sandboxed agent may read but never write, and
directories inside those that stay writable (``/mnt/c`` read-only, its ``Documents``
writable). These tests hold four properties:

* unset, the plan, the launcher and the Seatbelt profile are what they were before
  the keys existed;
* set, both renderers apply the layer, and the live adapter reads it from the
  operator's ``config.json`` without rewriting that file;
* an exception outside the operator's own read-only directories, or one that would
  re-open a crew mask or seal, is refused and logged;
* crew rules win by ORDER as well as by those checks: the launcher seals the layer
  before every crew mount, and the Seatbelt profile emits it before every crew rule.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import sandbox, sandbox_plan, sandbox_seatbelt, sandbox_tiers
from kiro_crew.config.loader_locations import config_path
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    CarveoutProbe,
    PlanHost,
    SandboxRequest,
    plan_confinement,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

HOME = "/srv/u"
CREW = f"{HOME}/.kiro/crew"
RUN = f"{CREW}/run"
DRIVE = "/mnt/c"
DOCS = f"{DRIVE}/Users/me/Documents"


def _probe(raw: str, *, canonical: str | None = None, is_dir: bool = True) -> CarveoutProbe:
    lexical = os.path.normpath(raw) if os.path.isabs(raw) else raw
    return CarveoutProbe(raw=raw, lexical=lexical, canonical=canonical or lexical, is_dir=is_dir)


def _host(
    readonly: tuple[CarveoutProbe, ...] = (),
    exceptions: tuple[CarveoutProbe, ...] = (),
    **overrides: Any,
) -> PlanHost:
    """A small host: one tier credential dir, one crew ceiling, the voice runtime."""
    fields: dict[str, Any] = {
        "home": HOME,
        "cwd": "/work",
        "tier_dirs": (".aws",),
        "crew_readonly_targets": (".kiro/crew/security_policy.json", ".kiro/crew/profiles"),
        "voice_runtime_roots": (f"{RUN}/voice-runtime",),
        "voice_runtime_parents": (RUN,),
        "voice_runtime_ancestor_guards": (RUN, CREW, f"{HOME}/.kiro", HOME, "/srv"),
        "uid": 1000,
        "gid": 1000,
        "operator_readonly_probes": readonly,
        "operator_exception_probes": exceptions,
    }
    fields.update(overrides)
    return PlanHost(**fields)


def _plan(backend: str, host: PlanHost, tier: str = "standard") -> sandbox_plan.ConfinementPlan:
    return plan_confinement(SandboxRequest(tier=tier, backend=backend), host)


def _messages(plan: sandbox_plan.ConfinementPlan) -> list[str]:
    return [refusal.message % refusal.args for refusal in plan.refusals]


BOTH = pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])


# --------------------------------------------------------------------------- #
# Unset: nothing changes.
# --------------------------------------------------------------------------- #


@BOTH
def test_unset_keys_plan_no_layer_and_refuse_nothing(backend: str) -> None:
    plan = _plan(backend, _host())
    assert plan.operator_readonly == ()
    assert plan.operator_writable == ()
    assert plan.operator_ancestor_guards == ()
    assert plan.refusals == ()


def test_unset_keys_leave_the_launcher_payload_without_the_layer() -> None:
    """The keys are emitted only when set, so an unset host renders the launcher it did."""
    payload = sandbox_plan.namespace_payload(_plan(BACKEND_NAMESPACE, _host()))
    assert "operator_readonly_dirs" not in payload
    assert "operator_writable_dirs" not in payload


def test_unset_keys_leave_the_seatbelt_profile_unchanged() -> None:
    """With no layer, no rule the layer emits appears in the profile."""
    profile = sandbox_seatbelt.render_seatbelt_profile(_plan(BACKEND_SEATBELT, _host()))
    assert DRIVE not in profile
    assert "/mnt" not in profile


# --------------------------------------------------------------------------- #
# Set: the /mnt/c-with-Documents shape on both renderers.
# --------------------------------------------------------------------------- #


@BOTH
def test_a_read_only_drive_keeps_its_documents_writable(backend: str) -> None:
    plan = _plan(backend, _host((_probe(DRIVE),), (_probe(DOCS),)))
    assert plan.operator_readonly == (DRIVE,)
    assert plan.operator_writable == (DOCS,)
    assert plan.refusals == ()


def test_the_launcher_payload_carries_the_layer() -> None:
    payload = sandbox_plan.namespace_payload(
        _plan(BACKEND_NAMESPACE, _host((_probe(DRIVE),), (_probe(DOCS),)))
    )
    assert payload["operator_readonly_dirs"] == [DRIVE]
    assert payload["operator_writable_dirs"] == [DOCS]


def test_the_seatbelt_profile_denies_the_drive_and_allows_documents_before_crew_rules() -> None:
    """The layer's rules come first, so every crew rule after them decides where both match."""
    profile = sandbox_seatbelt.render_seatbelt_profile(
        _plan(BACKEND_SEATBELT, _host((_probe(DRIVE),), (_probe(DOCS),)))
    )
    lines = profile.splitlines()
    deny = lines.index(f'(deny file-write* (subpath "{DRIVE}"))')
    link = lines.index(f'(deny file-link (subpath "{DRIVE}"))')
    allow = lines.index(f'(allow file-write* (subpath "{DOCS}"))')
    # Rename guards: the directory itself and each ancestor below the root.
    assert f'(deny file-write* (literal "{DRIVE}"))' in lines
    assert '(deny file-write* (literal "/mnt"))' in lines
    first_crew = lines.index(f'(deny file-read* (subpath "{HOME}/.aws"))')
    assert deny < link < allow < first_crew


def _seatbelt_literals(profile: str) -> list[str]:
    """Decode every quoted literal, requiring balanced strings on each profile line."""
    decoder = json.JSONDecoder()
    literals: list[str] = []
    for line in profile.splitlines():
        offset = 0
        while (start := line.find('"', offset)) != -1:
            literal, consumed = decoder.raw_decode(line[start:])
            assert isinstance(literal, str)
            literals.append(literal)
            offset = start + consumed
    return literals


@pytest.mark.parametrize("readonly", ["/tmp/archive\\", '/tmp/archive"\\'])
def test_seatbelt_operator_paths_stay_single_string_literals(readonly: str) -> None:
    exception = f'{readonly}/Writable"\\'
    plan = _plan(BACKEND_SEATBELT, _host((_probe(readonly),), (_probe(exception),)))
    assert plan.refusals == ()
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)
    literals = _seatbelt_literals(profile)
    assert readonly in literals
    assert exception in literals
    lines = profile.splitlines()
    for operation in ("file-write*", "file-link"):
        assert f"(deny {operation} (subpath {json.dumps(readonly)}))" in lines
        assert f"(allow {operation} (subpath {json.dumps(exception)}))" in lines
    for guard in plan.operator_ancestor_guards:
        assert f"(deny file-write* (literal {json.dumps(guard)}))" in lines


def test_seatbelt_crew_home_with_backslash_stays_single_string_literals() -> None:
    home = "/srv/u\\"
    profile = sandbox_seatbelt.render_seatbelt_profile(_plan(BACKEND_SEATBELT, _host(home=home)))
    literals = _seatbelt_literals(profile)
    credential_dir = f"{home}/.aws"
    assert credential_dir in literals
    assert f"(deny file-read* (subpath {json.dumps(credential_dir)}))" in profile.splitlines()


def test_seatbelt_denies_a_read_only_entry_nested_in_an_exception_after_the_allow() -> None:
    """``/mnt/c`` read-only, ``Documents`` writable, ``Documents/Taxes`` read-only again.

    The later rule decides, so the nested entry's denies come after the exception's
    allow, and every rename guard after both.
    """
    taxes = f"{DOCS}/Taxes"
    plan = _plan(BACKEND_SEATBELT, _host((_probe(DRIVE), _probe(taxes)), (_probe(DOCS),)))
    lines = sandbox_seatbelt.render_seatbelt_profile(plan).splitlines()
    allow = lines.index(f'(allow file-write* (subpath "{DOCS}"))')
    after = lines[allow + 1 :]
    assert f'(deny file-write* (subpath "{taxes}"))' in after
    assert f'(deny file-link (subpath "{taxes}"))' in after
    # Every rename guard comes after the allows, the exception's own included: renaming
    # Documents away would carry Taxes out from under its path rule.
    assert f'(deny file-write* (literal "{taxes}"))' in after
    assert f'(deny file-write* (literal "{DOCS}"))' in after
    assert f'(deny file-write* (subpath "{DRIVE}"))' not in after


def test_seatbelt_denies_writes_to_an_exception_s_own_name_after_its_allow() -> None:
    """The exception's subpath allow matches its own name, so the name takes a literal deny.

    Without it the agent could move ``Documents`` aside and plant a link to a read-only
    sibling under that name, which the next plan would then re-open.
    """
    plan = _plan(BACKEND_SEATBELT, _host((_probe(DRIVE),), (_probe(DOCS),)))
    lines = sandbox_seatbelt.render_seatbelt_profile(plan).splitlines()
    allow = lines.index(f'(allow file-write* (subpath "{DOCS}"))')
    assert f'(deny file-write* (literal "{DOCS}"))' in lines[allow + 1 :]


@BOTH
def test_an_exception_reached_through_a_link_below_its_entry_is_refused(backend: str) -> None:
    """``Documents`` swapped for a link to ``Payroll``: its canonical path is a sibling."""
    payroll = f"{DRIVE}/Payroll"
    plan = _plan(backend, _host((_probe(DRIVE),), (_probe(DOCS, canonical=payroll),)))
    assert plan.operator_writable == ()
    assert any(
        f"{DOCS!r}: it passes through a symbolic link below {DRIVE!r}" in m for m in _messages(plan)
    )


@BOTH
def test_an_entry_under_a_symlinked_root_keeps_its_exceptions(backend: str) -> None:
    """``/tmp`` -> ``/private/tmp`` on macOS: only the part below the entry is judged."""
    entry, exception = "/tmp/drive", "/tmp/drive/docs"
    plan = _plan(
        backend,
        _host(
            (_probe(entry, canonical="/private/tmp/drive"),),
            (_probe(exception, canonical="/private/tmp/drive/docs"),),
        ),
    )
    assert "/private/tmp/drive/docs" in plan.operator_writable
    assert not any("symbolic link" in m for m in _messages(plan))


def test_seatbelt_lets_each_nested_level_decide_over_the_one_holding_it() -> None:
    """Read-only X, writable X/A, read-only X/A/B, writable X/A/B/C, read-only X/A/B/C/D."""
    root = DRIVE
    a, b, c, d = f"{root}/A", f"{root}/A/B", f"{root}/A/B/C", f"{root}/A/B/C/D"
    plan = _plan(
        BACKEND_SEATBELT, _host((_probe(root), _probe(b), _probe(d)), (_probe(a), _probe(c)))
    )
    assert plan.refusals == ()
    lines = sandbox_seatbelt.render_seatbelt_profile(plan).splitlines()
    order = [
        lines.index(f'(deny file-write* (subpath "{root}"))'),
        lines.index(f'(allow file-write* (subpath "{a}"))'),
        lines.index(f'(deny file-write* (subpath "{b}"))'),
        lines.index(f'(allow file-write* (subpath "{c}"))'),
        lines.index(f'(deny file-write* (subpath "{d}"))'),
    ]
    assert order == sorted(order)
    # An exception's own files may be hardlinked; a crew deny later still wins.
    assert f'(allow file-link (subpath "{c}"))' in lines


def test_the_namespace_plan_applies_the_canonical_spelling_only() -> None:
    """A bind mount follows the name, so one spelling covers the directory either reaches."""
    plan = _plan(
        BACKEND_NAMESPACE,
        _host(
            (_probe("/data/drive", canonical=DRIVE),),
            (_probe("/data/drive/Users/me/Documents", canonical=DOCS),),
        ),
    )
    assert plan.operator_readonly == (DRIVE,)
    assert plan.operator_writable == (DOCS,)


def test_seatbelt_keeps_both_spellings_because_its_rules_match_paths() -> None:
    plan = _plan(
        BACKEND_SEATBELT,
        _host(
            (_probe("/data/drive", canonical=DRIVE),),
            (_probe("/data/drive/Users/me/Documents", canonical=DOCS),),
        ),
    )
    assert plan.operator_readonly == ("/data/drive", DRIVE)
    assert plan.operator_writable == ("/data/drive/Users/me/Documents", DOCS)


# --------------------------------------------------------------------------- #
# Refusals.
# --------------------------------------------------------------------------- #


@BOTH
def test_an_exception_outside_every_read_only_entry_is_refused(backend: str) -> None:
    plan = _plan(backend, _host((_probe(DRIVE),), (_probe("/opt/elsewhere"),)))
    assert plan.operator_writable == ()
    assert _messages(plan) == [
        "SECURITY: ignoring agent.sandbox_readonly_exceptions entry '/opt/elsewhere': "
        "'/opt/elsewhere' is outside every agent.sandbox_readonly_paths entry the "
        "sandbox applies"
    ]


@BOTH
def test_an_exception_equal_to_its_read_only_entry_is_refused(backend: str) -> None:
    """Equal would only cancel the entry; the operator removes it instead."""
    plan = _plan(backend, _host((_probe(DRIVE),), (_probe(DRIVE),)))
    assert plan.operator_readonly == (DRIVE,)
    assert plan.operator_writable == ()
    assert "is itself a read-only entry" in _messages(plan)[0]


@BOTH
def test_an_exception_that_would_reopen_a_crew_seal_is_refused(backend: str) -> None:
    """The data home's governance ceiling stays sealed whatever the operator lists."""
    plan = _plan(backend, _host((_probe(HOME),), (_probe(CREW),)))
    assert plan.operator_readonly == (HOME,)
    assert plan.operator_writable == ()
    (message,) = _messages(plan)
    assert message.startswith("SECURITY: ignoring agent.sandbox_readonly_exceptions entry")
    assert "would be re-opened by it" in message


@BOTH
def test_an_exception_inside_a_crew_mask_is_refused(backend: str) -> None:
    """A tier credential dir cannot grow a writable window through the layer."""
    plan = _plan(backend, _host((_probe(HOME),), (_probe(f"{HOME}/.aws/sso"),)))
    assert plan.operator_writable == ()
    assert f"it lies inside sealed subtree '{HOME}/.aws'" in _messages(plan)[0]


@BOTH
def test_an_exception_inside_a_crew_ceiling_entry_is_refused(backend: str) -> None:
    """A read-only entry that IS a crew guard opens no exception inside it."""
    plan = _plan(backend, _host((_probe(f"{CREW}/profiles"),), (_probe(f"{CREW}/profiles/a"),)))
    assert plan.operator_writable == ()
    assert "is outside every agent.sandbox_readonly_paths entry" in _messages(plan)[0]


@BOTH
def test_the_whole_filesystem_is_refused(backend: str) -> None:
    plan = _plan(backend, _host((_probe("/"),)))
    assert plan.operator_readonly == ()
    assert "the whole filesystem cannot be made read-only" in _messages(plan)[0]


@BOTH
def test_a_relative_or_missing_entry_is_refused(backend: str) -> None:
    plan = _plan(
        backend,
        _host(
            (_probe("relative/dir", is_dir=False), _probe("/gone", is_dir=False)),
            (_probe("rel", is_dir=False),),
        ),
    )
    assert plan.operator_readonly == ()
    assert _messages(plan) == [
        "SECURITY: ignoring agent.sandbox_readonly_paths entry 'relative/dir': "
        "not an absolute path",
        "SECURITY: ignoring agent.sandbox_readonly_paths entry '/gone': "
        "not an existing directory",
        "SECURITY: ignoring agent.sandbox_readonly_exceptions entry 'rel': " "not an absolute path",
    ]


def test_an_entry_holding_the_launchers_stand_in_root_is_refused() -> None:
    """Every Linux mask is created under these, so sealing one would kill every spawn."""
    plan = _plan(BACKEND_NAMESPACE, _host((_probe("/run"), _probe("/dev"))))
    assert plan.operator_readonly == ()
    assert _messages(plan) == [
        "SECURITY: ignoring agent.sandbox_readonly_paths entry '/run': it holds "
        "'/run/user/1000', where the sandbox places its masks",
        "SECURITY: ignoring agent.sandbox_readonly_paths entry '/dev': it holds "
        "'/dev/shm', where the sandbox places its masks",
    ]


def test_the_crew_carve_outs_are_validated_exactly_as_before() -> None:
    """The layer does not widen what ``extra_writable_dirs`` may re-open."""
    probe = f"{RUN}/mcp-tmp/probe-1"
    host = _host(
        (_probe(HOME),),
        carveout_probes=(_probe(probe), _probe("/mnt/c/x")),
    )
    request = SandboxRequest(
        tier="standard", backend=BACKEND_NAMESPACE, extra_writable_dirs=(probe, "/mnt/c/x")
    )
    plan = plan_confinement(request, host)
    assert plan.writable == (probe,)
    assert any("outside every carveable runtime parent" in m for m in _messages(plan))


# --------------------------------------------------------------------------- #
# The live adapter reads the operator's config.json, without rewriting it.
# --------------------------------------------------------------------------- #


def _write_config(agent: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": agent}), encoding="utf-8")
    return path


def test_the_config_keys_reach_the_live_plan(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    docs = drive / "Users" / "me" / "Documents"
    docs.mkdir(parents=True)
    _write_config(
        {"sandbox_readonly_paths": [str(drive)], "sandbox_readonly_exceptions": [str(docs)]}
    )
    plan = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard")
    assert plan.operator_readonly == (os.path.realpath(drive),)
    assert plan.operator_writable == (os.path.realpath(docs),)


@pytest.mark.skipif(sys.platform == "win32", reason="the namespace plan is POSIX-only")
def test_the_parents_of_a_read_only_entry_are_pinned_on_linux(tmp_path: Path) -> None:
    """The next session resolves the entry by name, so a renamable parent is pinned."""
    proj = tmp_path / "proj"
    (proj / "data").mkdir(parents=True)
    _write_config({"sandbox_readonly_paths": [str(proj / "data")]})
    plan = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard")
    assert os.path.normpath(proj) in plan.runtime_ancestor_guards
    assert os.path.normpath(tmp_path) in plan.runtime_ancestor_guards


@pytest.mark.skipif(sys.platform == "win32", reason="the namespace plan is POSIX-only")
def test_a_refused_read_only_entry_pins_no_parent(tmp_path: Path) -> None:
    """A missing directory or a file is ignored by the plan, so nothing above it is pinned.

    The accepted entry beside them is the control: its parent is still pinned.
    """
    gone, notes, kept = tmp_path / "archive", tmp_path / "proj", tmp_path / "keep"
    gone.mkdir()
    notes.mkdir()
    (notes / "notes.txt").write_text("x", encoding="utf-8")
    (kept / "data").mkdir(parents=True)
    _write_config(
        {
            "sandbox_readonly_paths": [
                str(gone / "old"),
                str(notes / "notes.txt"),
                str(kept / "data"),
            ]
        }
    )
    plan = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard")
    assert plan.operator_readonly == (os.path.realpath(kept / "data"),)
    assert os.path.normpath(kept) in plan.runtime_ancestor_guards
    assert os.path.normpath(gone) not in plan.runtime_ancestor_guards
    assert os.path.normpath(notes) not in plan.runtime_ancestor_guards


def test_an_unset_layer_pins_no_extra_parents(tmp_path: Path) -> None:
    """The control: without the keys, the operator directory's parent is not pinned."""
    proj = tmp_path / "proj"
    (proj / "data").mkdir(parents=True)
    plan = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard")
    assert os.path.normpath(proj) not in plan.runtime_ancestor_guards


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX modes that bind a non-root user",
)
def test_a_read_only_entry_behind_a_locked_parent_stays_in_the_plan(tmp_path: Path) -> None:
    """``EACCES`` on the probe keeps the entry, so the launcher seals it rather than skips.

    An exception that cannot be probed is still refused: its path stays read-only.
    """
    parent = tmp_path / "parent"
    archive = parent / "archive"
    docs = archive / "docs"
    docs.mkdir(parents=True)
    _write_config(
        {"sandbox_readonly_paths": [str(archive)], "sandbox_readonly_exceptions": [str(docs)]}
    )
    expected = os.path.join(os.path.realpath(parent), "archive")
    parent.chmod(0)
    try:
        plan = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard")
    finally:
        parent.chmod(0o755)
    assert plan.operator_readonly == (expected,)
    assert plan.operator_writable == ()


def test_reading_the_keys_does_not_rewrite_config_json() -> None:
    """The pre-spawn pass records config.json's identity, so the plan must not replace it."""
    path = _write_config({"sandbox_readonly_paths": ["/nonexistent-ro"]})
    before = os.stat(path)
    assert sandbox_tiers.configured_readonly_layer() == (("/nonexistent-ro",), ())
    after = os.stat(path)
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )


@pytest.fixture(autouse=True)
def _reset_readonly_layer_state(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(sandbox_tiers, "_READONLY_LAYER_CACHE", {}, raising=False)
    _floor_monkeypatch.setattr(sandbox_tiers, "_READONLY_LAYER_WARNED", set(), raising=False)


def test_truncated_config_keeps_the_last_readonly_layer(caplog: pytest.LogCaptureFixture) -> None:
    path = _write_config({"sandbox_readonly_paths": ["/mnt/c"]})
    assert sandbox_tiers.configured_readonly_layer() == (("/mnt/c",), ())
    path.write_text('{"agent": {"sandbox_readonly_', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        assert sandbox_tiers.configured_readonly_layer() == (("/mnt/c",), ())
        assert sandbox_tiers.configured_readonly_layer() == (("/mnt/c",), ())
    assert len(caplog.records) == 1
    assert f"SECURITY: {path} could not be read" in caplog.text
    assert "keeping the last agent.sandbox_readonly_paths this process read" in caplog.text


def test_unparseable_config_without_a_cached_layer_warns(caplog: pytest.LogCaptureFixture) -> None:
    path = _write_config({})
    path.write_text("{", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        assert sandbox_tiers.configured_readonly_layer() == ((), ())
    assert f"SECURITY: {path} could not be read" in caplog.text
    assert "applying agent.sandbox_readonly_paths from the documents that parsed" in caplog.text


def test_unparseable_overlay_without_a_cached_layer_applies_config_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fresh process whose overlay is broken still applies ``config.json``'s layer."""
    from kiro_crew.config.loader_locations import config_local_path

    _write_config(
        {
            "sandbox_readonly_paths": ["/mnt/c"],
            "sandbox_readonly_exceptions": ["/mnt/c/Documents"],
        }
    )
    path = config_local_path()
    expected = (("/mnt/c",), ("/mnt/c/Documents",))
    for broken in ("{", "[]", json.dumps({"agent": []})):
        path.write_text(broken, encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            assert sandbox_tiers.configured_readonly_layer() == expected
        assert "layer" not in sandbox_tiers._READONLY_LAYER_CACHE
    assert f"SECURITY: {path} could not be read" in caplog.text


@pytest.mark.parametrize("tamper", ["rewrite", "delete"])
def test_an_agent_rewrite_after_a_rename_save_keeps_the_endorsed_layer(
    tamper: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The layer is read as the loader reads ``config.json``: the gateway-endorsed copy.

    A gateway save by rename detaches a running agent's read-only bind on ``config.json``,
    so the agent can then rewrite or delete it. Where that bind can detach
    (``enforcement_active``) the next spawn must still plan the layer the gateway wrote.
    """
    from kiro_crew.config import loosening_provenance

    monkeypatch.setattr(loosening_provenance, "enforcement_active", lambda: True)
    path = _write_config({"sandbox_readonly_paths": ["/mnt/c"]})
    loosening_provenance.record(loosening_provenance.LEAF_CONFIG, path.read_bytes())
    if tamper == "rewrite":
        path.write_text(json.dumps({"agent": {"sandbox_readonly_paths": []}}), encoding="utf-8")
    else:
        path.unlink()
    assert sandbox_tiers.configured_readonly_layer() == (("/mnt/c",), ())


def test_unparseable_overlay_keeps_the_last_readonly_layer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from kiro_crew.config.loader_locations import config_local_path

    _write_config({"sandbox_readonly_paths": ["/mnt/c"]})
    path = config_local_path()
    path.write_text(
        json.dumps({"agent": {"sandbox_readonly_exceptions": ["/mnt/c/Documents"]}}),
        encoding="utf-8",
    )
    expected = (("/mnt/c",), ("/mnt/c/Documents",))
    assert sandbox_tiers.configured_readonly_layer() == expected
    path.write_text("{", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        assert sandbox_tiers.configured_readonly_layer() == expected
    assert f"SECURITY: {path} could not be read" in caplog.text


def test_a_leading_tilde_is_expanded_and_non_strings_are_dropped(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/someone")
    _write_config(
        {"sandbox_readonly_paths": ["~/ro", 7, "", None], "sandbox_readonly_exceptions": "x"}
    )
    assert sandbox_tiers.configured_readonly_layer() == (("/home/someone/ro",), ())


def test_an_unset_config_renders_the_same_launcher_as_explicitly_empty_keys(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
    _write_config({})
    unset = sandbox._build_launcher_script("standard")
    _write_config({"sandbox_readonly_paths": [], "sandbox_readonly_exceptions": []})
    empty = sandbox._build_launcher_script("standard")
    assert unset == empty
    from test_sandbox_launcher_program import rendered_payload

    assert "operator_readonly_dirs" not in rendered_payload(unset)


def test_the_loader_coerces_both_keys_to_lists_of_strings() -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    _write_config({"sandbox_readonly_paths": ["/a", 3], "sandbox_readonly_exceptions": "/b"})
    agent = KiroCrewConfig.load().agent
    assert agent.sandbox_readonly_paths == ["/a"]
    assert agent.sandbox_readonly_exceptions == []


# --------------------------------------------------------------------------- #
# Windows: no backend, so the keys are reported as not enforced.
# --------------------------------------------------------------------------- #


def test_the_unenforced_warning_fires_once_and_only_when_set(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        sandbox_tiers.warn_readonly_layer_unenforced, "_warned", False, raising=False
    )
    caplog.set_level(logging.WARNING, logger="kiro_crew.sandbox")
    _write_config({})
    sandbox_tiers.warn_readonly_layer_unenforced("no backend")
    assert "sandbox_readonly_paths" not in caplog.text
    _write_config({"sandbox_readonly_paths": ["/mnt/c"]})
    sandbox_tiers.warn_readonly_layer_unenforced("no backend")
    sandbox_tiers.warn_readonly_layer_unenforced("no backend")
    assert caplog.text.count("agent.sandbox_readonly_paths is set but NOT enforced") == 1


# --------------------------------------------------------------------------- #
# The launcher stage, driven with a recording libc.
# --------------------------------------------------------------------------- #

_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the namespace launcher is Linux-only"
)
_MS_RDONLY = 1
_MS_REMOUNT = 32
_MS_BIND = 4096
_MS_REC = 16384
_SHAPE = _MS_RDONLY | _MS_REMOUNT | _MS_BIND | _MS_REC


def _mount_shapes(libc) -> list[tuple[str, int]]:
    """Each mount as (the path its target reached, its bind/remount/read-only bits)."""
    return [(call.target_path, call.flags & _SHAPE) for call in libc.calls]


@pytest.fixture
def drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A read-only drive with a filesystem mounted below it and a writable Documents."""
    from kiro_crew import sandbox_launcher_program as program

    root = tmp_path / "drive"
    sub = root / "mounted"
    docs = root / "Users" / "me" / "Documents"
    sub.mkdir(parents=True)
    docs.mkdir(parents=True)
    info = tmp_path / "mountinfo"
    escaped = str(sub).replace(" ", "\\040").encode()
    info.write_bytes(
        b"22 1 0:21 / / rw - ext4 /dev/root rw\n"
        b"30 22 0:5 / /proc rw - proc proc rw\n"
        + b"41 22 0:40 / "
        + escaped
        + b" rw - drvfs C: rw\n"
        + b"42 41 0:40 / "
        + escaped
        + b" rw - drvfs C: rw\n"
    )
    monkeypatch.setattr(program, "_MOUNTINFO", str(info))
    # Every pinned descriptor reports the top mount listed at ``sub``.
    monkeypatch.setattr(program, "_mount_id_of", lambda fd: b"42")
    return {"root": root, "sub": sub, "docs": docs}


@_LINUX_ONLY
class TestSealOperatorReadonly:
    def test_unset_mounts_nothing(self, tmp_path: Path) -> None:
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        libc = RecordingLibc()
        program.seal_operator_readonly(launch(tmp_path, payload(), libc=libc))
        assert libc.calls == []

    def test_the_drive_and_each_mount_below_it_go_read_only_then_documents_reopen(
        self, tmp_path: Path, drive: dict[str, Path]
    ) -> None:
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        libc = RecordingLibc()
        run = launch(
            tmp_path,
            payload(
                operator_readonly_dirs=[str(drive["root"])],
                operator_writable_dirs=[str(drive["docs"])],
            ),
            libc=libc,
        )
        program.seal_operator_readonly(run)
        root, sub, docs = (str(drive[k]) for k in ("root", "sub", "docs"))
        assert _mount_shapes(libc) == [
            # Recursive, so a filesystem mounted below the drive stays visible...
            (root, _MS_BIND | _MS_REC),
            (root, _MS_REMOUNT | _MS_BIND | _MS_RDONLY),
            # ...and is made read-only by its own name, once although listed twice.
            (sub, _MS_REMOUNT | _MS_BIND | _MS_RDONLY),
            # The exception: a fresh recursive bind, remounted WITHOUT read-only.
            (docs, _MS_BIND | _MS_REC),
            (docs, _MS_REMOUNT | _MS_BIND),
        ]

    def test_a_mount_the_namespace_cannot_reach_is_skipped_not_refused(
        self, tmp_path: Path, drive: dict[str, Path]
    ) -> None:
        """A mount below the drive under a directory this process cannot traverse."""
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        if os.geteuid() == 0:
            pytest.skip("root traverses a mode-000 directory")
        locked = drive["root"] / "locked"
        (locked / "inner").mkdir(parents=True)
        locked.chmod(0)
        unreachable = locked / "inner"
        info = Path(program._MOUNTINFO)
        info.write_bytes(
            info.read_bytes() + b"43 22 0:41 / " + str(unreachable).encode() + b" rw - x x rw\n"
        )
        libc = RecordingLibc()
        run = launch(tmp_path, payload(operator_readonly_dirs=[str(drive["root"])]), libc=libc)
        try:
            program.seal_operator_readonly(run)
        finally:
            locked.chmod(0o700)
        reached = [path for path, _flags in _mount_shapes(libc)]
        assert str(drive["sub"]) in reached
        assert str(unreachable) not in reached

    def test_an_exception_with_an_unreachable_mount_stays_read_only(
        self, tmp_path: Path, drive: dict[str, Path], capfd
    ) -> None:
        """An unreachable submount blocks only its containing write exception."""
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        if os.geteuid() == 0:
            pytest.skip("root traverses a mode-000 directory")
        work = drive["root"] / "work"
        locked = work / "locked"
        unreachable = locked / "vol"
        unreachable.mkdir(parents=True)
        info = Path(program._MOUNTINFO)
        info.write_bytes(
            info.read_bytes() + b"43 22 0:41 / " + str(unreachable).encode() + b" rw - x x rw\n"
        )
        libc = RecordingLibc()
        run = launch(
            tmp_path,
            payload(
                operator_readonly_dirs=[str(drive["root"])],
                operator_writable_dirs=[str(work), str(drive["docs"])],
            ),
            libc=libc,
        )
        locked.chmod(0)
        try:
            program.seal_operator_readonly(run)
        finally:
            locked.chmod(0o700)
        shapes = _mount_shapes(libc)
        assert all(path != str(work) for path, _flags in shapes)
        assert all(path != str(unreachable) for path, _flags in shapes)
        assert (str(drive["docs"]), _MS_BIND | _MS_REC) in shapes
        assert (str(drive["docs"]), _MS_REMOUNT | _MS_BIND) in shapes
        assert capfd.readouterr().err == (
            "sandbox: WARNING -- read-only exception %s left read-only: "
            "a mount below it (%s) could not be reached to seal\n" % (work, unreachable)
        )

    def test_a_hidden_mount_record_is_skipped_not_refused(
        self, tmp_path: Path, drive: dict[str, Path], capfd
    ) -> None:
        """A record a later mount over its parent hides: its name reaches another mount.

        The name resolves (an ordinary directory of the covering filesystem), so only the
        mount ID tells it apart. Remounting it would fail with ``EINVAL`` and refuse every
        spawn; it is skipped instead, the visible mount below the drive is still sealed,
        and an exception holding the hidden record stays read-only.
        """
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        work = drive["root"] / "work"
        hidden = work / "old"
        hidden.mkdir(parents=True)
        info = Path(program._MOUNTINFO)
        info.write_bytes(
            info.read_bytes() + b"43 22 0:41 / " + str(hidden).encode() + b" rw - x x rw\n"
        )
        libc = RecordingLibc()
        run = launch(
            tmp_path,
            payload(
                operator_readonly_dirs=[str(drive["root"])],
                operator_writable_dirs=[str(work), str(drive["docs"])],
            ),
            libc=libc,
        )
        program.seal_operator_readonly(run)
        shapes = _mount_shapes(libc)
        assert (str(drive["sub"]), _MS_REMOUNT | _MS_BIND | _MS_RDONLY) in shapes
        assert all(path not in (str(hidden), str(work)) for path, _flags in shapes)
        assert (str(drive["docs"]), _MS_REMOUNT | _MS_BIND) in shapes
        assert capfd.readouterr().err == (
            "sandbox: WARNING -- read-only exception %s left read-only: "
            "a mount below it (%s) could not be reached to seal\n" % (work, hidden)
        )

    def test_an_unreadable_mount_id_is_judged_as_reached(
        self, tmp_path: Path, drive: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without an ID to compare, the remount runs and fails closed as before."""
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        monkeypatch.setattr(program, "_mount_id_of", lambda fd: None)
        libc = RecordingLibc()
        run = launch(tmp_path, payload(operator_readonly_dirs=[str(drive["root"])]), libc=libc)
        program.seal_operator_readonly(run)
        assert (str(drive["sub"]), _MS_REMOUNT | _MS_BIND | _MS_RDONLY) in _mount_shapes(libc)

    def test_a_mount_below_the_drive_that_cannot_go_read_only_refuses_the_spawn(
        self, tmp_path: Path, drive: dict[str, Path]
    ) -> None:
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        run = launch(
            tmp_path,
            payload(operator_readonly_dirs=[str(drive["root"])]),
            libc=RecordingLibc(fail_at=3),
        )
        with pytest.raises(SystemExit) as exc:
            program.seal_operator_readonly(run)
        assert "sealing read-only mount" in str(exc.value)
        assert str(drive["sub"]) in str(exc.value)

    def test_a_refused_exception_leaves_documents_read_only_and_the_spawn_running(
        self, tmp_path: Path, drive: dict[str, Path], capfd
    ) -> None:
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        libc = RecordingLibc(fail_at=4)
        run = launch(
            tmp_path,
            payload(
                operator_readonly_dirs=[str(drive["root"])],
                operator_writable_dirs=[str(drive["docs"])],
            ),
            libc=libc,
        )
        program.seal_operator_readonly(run)
        # The rw remount never ran after its bind failed.
        assert len(libc.calls) == 4
        assert "read-only exception bind" in capfd.readouterr().err

    def test_an_absent_read_only_entry_is_skipped(self, tmp_path: Path) -> None:
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        libc = RecordingLibc()
        run = launch(tmp_path, payload(operator_readonly_dirs=[str(tmp_path / "gone")]), libc=libc)
        program.seal_operator_readonly(run)
        assert libc.calls == []

    def test_the_layer_lands_before_every_crew_seal(
        self, tmp_path: Path, drive: dict[str, Path]
    ) -> None:
        """A crew ceiling inside the drive is bound on top of the drive's seal."""
        from test_sandbox_launcher_program import RecordingLibc, launch, payload

        from kiro_crew import sandbox_launcher_program as program

        ceiling = drive["root"] / "ceiling"
        ceiling.mkdir()
        libc = RecordingLibc()
        run = launch(
            tmp_path,
            payload(operator_readonly_dirs=[str(drive["root"])], readonly_dirs=[str(ceiling)]),
            libc=libc,
        )
        program.place_masks(run)
        targets = [path for path, _flags in _mount_shapes(libc)]
        assert targets.index(str(drive["root"])) < targets.index(str(ceiling))


def test_mount_points_are_read_from_field_five_with_their_escapes_decoded() -> None:
    from kiro_crew import sandbox_launcher_program as program

    info = (
        b"1 0 0:1 / /mnt rw - x x rw\n"
        b"2 1 0:2 / /mnt/c rw - x x rw\n"
        b"3 1 0:3 / /mnt/my\\040drive rw - x x rw\n"
        b"4 1 0:4 / /mnt-other rw - x x rw\n"
        b"5 2 0:2 / /mnt/c rw - x x rw\n"
        b"short line\n"
    )
    assert program._mount_points_below(b"/mnt", info) == [b"/mnt/c", b"/mnt/my drive"]
    assert program._mount_ids_at([b"/mnt/c", b"/mnt/my drive"], info) == {
        b"/mnt/c": {b"2", b"5"},
        b"/mnt/my drive": {b"3"},
    }


@_LINUX_ONLY
def test_a_descriptor_s_mount_id_is_the_one_mountinfo_lists_for_its_mount(
    tmp_path: Path,
) -> None:
    """``fdinfo``'s ``mnt_id`` names a mount ``mountinfo`` lists; a bad fd gives ``None``."""
    from kiro_crew import sandbox_launcher_program as program

    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        reached = program._mount_id_of(fd)
    finally:
        os.close(fd)
    with open("/proc/self/mountinfo", "rb") as info:
        listed = {line.split(b" ")[0] for line in info.read().splitlines()}
    assert reached in listed
    closed = os.open(tmp_path, os.O_RDONLY)
    os.close(closed)
    assert program._mount_id_of(closed) is None


def _pre_dedupe_literal_ancestor_guards(paths: tuple[str, ...]) -> tuple[str, ...]:
    """``sandbox_workspace._literal_ancestor_guards`` as it was before it delegated."""
    guards: dict[str, None] = {}
    for item in paths:
        current = os.path.normpath(item)
        while current not in guards and os.path.dirname(current) != current:
            guards[current] = None
            current = os.path.dirname(current)
    return tuple(guards)


@pytest.mark.parametrize(
    "paths",
    [
        ("/a/b/c", "/a/x", "/a/b"),
        ("/srv/data/", "/srv//data/old", "/"),
        (f"{HOME}/.kiro/crew/run", f"{HOME}/.kiro", "/tmp/drive/docs"),
        (),
    ],
)
def test_the_crew_and_operator_ancestor_guards_are_one_walk(paths: tuple[str, ...]) -> None:
    """The crew's literal guards and the operator layer's come from one implementation.

    Each crew caller gets exactly the guards, in the order, it got before the dedupe.
    """
    from kiro_crew import sandbox_workspace

    expected = _pre_dedupe_literal_ancestor_guards(paths)
    assert sandbox_workspace._literal_ancestor_guards(paths) == expected
    assert tuple(sandbox_plan.path_ancestor_guards(paths)) == expected
