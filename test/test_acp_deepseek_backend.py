"""DeepSeek Harness as an ACP backend: the decisions that must not drift.

The harness is a plugin host and ACP is one of the profiles it boots, so most of
what this file pins is ordinary onboarding vocabulary. Three things are not:

* it serves ``session/resume`` and REJECTS ``session/load``, so the shared restore
  path has to pick both the capability it reads and the verb it sends from one
  membership set;
* its gate plugin turns each tool decision into an ACP permission request, and a
  bounded no-follow load-marker read verifies that routing before it is offered;
* it advertises reasoning effort under its own option id, so the id is read from a
  table rather than spelled at each site.

The gate-routing pins live here; the restore verb, spawn, effort, provider-key and
enforcement pins live in the ``test_acp_deepseek_backend_*`` siblings.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import threading
from unittest.mock import AsyncMock

import pytest
from acp_deepseek_backend_helpers import (  # noqa: F401 -- fixtures are used by name
    _DeepseekLaunchRig,
    _fixture_frames,
    _pinned_kiro_cli_version,
)

from conftest import make_dir_link
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import deepseek as deepseek_mod
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_MEMBER_DISPATCH,
    ACP_BACKENDS_MEMBER_PANEL,
    ACP_BACKENDS_RESUME_WITHOUT_LOAD,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    ACP_BACKENDS_STEER,
    BASELINE_SELECTABLE_BACKENDS,
    POLICY_ID_BY_BACKEND,
    Routing,
    effort_config_option_id,
    routing_for,
)

# ── Vocabulary ───────────────────────────────────────────────────────────────


def test_the_id_is_known_and_nameable_by_a_policy_author() -> None:
    """A known id must be spellable in a governance rule, or it cannot be denied."""
    assert ACP_BACKEND_DEEPSEEK in ACP_BACKENDS_KNOWN
    assert POLICY_ID_BY_BACKEND[ACP_BACKEND_DEEPSEEK] == "deepseek"


def test_it_is_known_and_shipped_selectable() -> None:
    """Known so it can be registered and denied; selectable because it is gated.

    The two halves of the selectability bar are an install probe and ROUTED tool
    calls. This harness has both, so the switch offers a harness whose tool calls
    reach Crew's gate.
    """
    assert ACP_BACKEND_DEEPSEEK in BASELINE_SELECTABLE_BACKENDS


@pytest.mark.parametrize(
    "membership, expected",
    [
        (ACP_BACKENDS_HARNESS_OWNED_SESSIONS, True),
        (ACP_BACKENDS_LOAD_WITHOUT_MODES, True),
        (ACP_BACKENDS_RESUME_WITHOUT_LOAD, True),
        (ACP_BACKENDS_SESSION_MCP_ARRAY, True),
        (ACP_BACKENDS_ADVERTISED_MODEL_SELECTION, True),
        (ACP_BACKENDS_STEER, False),
        (ACP_BACKENDS_COMPACT, False),
        (ACP_BACKENDS_INTERNAL_SANDBOX, False),
        (ACP_BACKENDS_MEMBER_DISPATCH, True),
        (ACP_BACKENDS_MEMBER_PANEL, True),
    ],
)
def test_every_capability_is_an_explicit_decision(membership: frozenset, expected: bool) -> None:
    """One row per set, so a silently granted capability names its harness.

    Written out rather than derived from the sets themselves, which would pass
    tautologically.
    """
    assert (ACP_BACKEND_DEEPSEEK in membership) is expected


def test_the_resume_set_is_the_only_member_and_says_why() -> None:
    """Sole membership is the claim: no other harness Crew carries lacks the verb."""
    assert ACP_BACKENDS_RESUME_WITHOUT_LOAD == frozenset({ACP_BACKEND_DEEPSEEK})


def test_the_effort_option_id_is_this_harness_own_spelling() -> None:
    """The table answers a spelling; every other harness keeps the default."""
    assert effort_config_option_id(ACP_BACKEND_DEEPSEEK) == "reasoning_effort"
    assert effort_config_option_id(ACP_BACKEND_CLAUDE) == "effort"
    assert effort_config_option_id(ACP_BACKEND_KIRO) == "effort"


def test_the_session_mcp_array_membership_rests_on_a_captured_round_trip() -> None:
    """Membership here is load-bearing, so it is pinned to evidence rather than prose.

    If this harness did NOT mount stdio, ``session/new`` would fail whole rather than
    degrade -- a broken backend, not a tool-less one -- and its ``initialize``
    advertises ``mcpCapabilities: {"http": true}`` with no stdio flag, which reads
    like a refusal. The capture is what settles it: a real stdio MCP server was
    mounted, its tool was called, and its result came back.
    """
    frames = _fixture_frames("mcp-stdio-mount-live.jsonl")

    created = [
        frame
        for frame in frames
        if isinstance(frame.get("result"), dict) and frame["result"].get("sessionId")
    ]
    assert created, "session/new must have SUCCEEDED with the stdio element mounted"

    updates = [
        (frame.get("params") or {}).get("update", {})
        for frame in frames
        if isinstance(frame.get("params"), dict)
    ]
    calls = [u for u in updates if u.get("sessionUpdate") == "tool_call"]
    assert calls, "the capture must carry a tool_call for the mounted MCP tool"
    # The harness's own MCP grammar: mcp__<serverName>__<toolName>. Asserted because
    # the host contract's tool-name-grammar row states it as measured.
    assert calls[0]["title"].startswith("mcp__"), calls[0]["title"]
    assert calls[0]["title"].count("__") >= 2, calls[0]["title"]

    results = [u for u in updates if u.get("sessionUpdate") == "tool_call_update"]
    assert any(u.get("status") == "completed" for u in results), (
        "a mount that is accepted but whose tool never returns proves reachability of "
        "nothing; the capture must carry a completed result"
    )


def test_an_unstartable_mcp_element_fails_the_whole_session() -> None:
    """The hazard half, pinned because it changes how a pooled stub failure behaves.

    codex-acp drops a malformed element and creates the session anyway. This harness
    rolls the whole session back, so ONE pooled broker stub that cannot start costs a
    session entirely. The host contract's loader-strictness row states that; this is
    what makes the statement checkable.
    """
    frames = _fixture_frames("mcp-stdio-rollback-live.jsonl")

    errors = [frame for frame in frames if isinstance(frame.get("error"), dict)]
    assert errors, "the rollback capture must carry the session/new error"
    detail = str((errors[0]["error"].get("data") or {}).get("details", ""))
    assert "initial connection or tool synchronization failed" in detail, detail
    assert not [
        frame
        for frame in frames
        if isinstance(frame.get("result"), dict) and frame["result"].get("sessionId")
    ], "no session may have been created by the rolled-back request"


# ── Routing: the verified gate, asserted ─────────────────────────────────────


def _gate_extension_path(tmp_path):
    """The gate module this test's fake probe speaks for, as a real path under *tmp_path*.

    Both sides of the read-back's file comparison are brought to one spelling with
    ``realpath`` + ``normcase``, so a bare ``/sealed/gate.mjs`` is not a usable
    stand-in: on Windows the expected side becomes ``c:\\sealed\\gate.mjs`` while a
    marker string that is not a ``file://`` URL is never normalised, and the two
    disagree for a reason production never has. An absolute path under ``tmp_path``
    normalises identically on every platform.
    """
    return tmp_path / "gate.mjs"


def _good_gate_marker(module: str = "/sealed/gate.mjs", names: tuple[str, ...] = ()) -> dict:
    """The complete marker emitted by the shipped plugin on a clean ACP profile.

    *module* defaults to a plain path for the callers that hand this marker straight
    to :func:`~kiro_crew.acp_tool_gate.gate_marker_issue`, which compares strings.
    Anything going through ``_verify_deepseek_gate`` must pass the ``file://`` URL of
    :func:`_gate_extension_path` instead, because that is what the shipped plugin
    writes (``import.meta.url``) and the only shape the read-back normalises.

    *names* are the vault-fed env names the probe asked the plugin to check; the
    clean marker reports every one present in the harness's own environment and
    absent from the child it spawned through the harness's subprocess service.
    """
    return {
        "plugin": "kiro-crew-tool-gate",
        "nonce": "n1",
        "module": module,
        "approval": {
            "policy": "ask",
            "answerers": [
                {
                    "entry": "include:acp",
                    "module": "@deepseek-ai/dsh-acp",
                    "plugin": "acp",
                }
            ],
        },
        "tools": {"mode": "native"},
        "child_env": {
            "version": "0.1.5-rc.2",
            "names": list(names),
            "parent_missing": [],
            "child_visible": [],
            "error": None,
        },
    }


class _FakeProbeProcess:
    """A stand-in for the harness the probe boots: writes the marker, then waits for EOF.

    The real plugin writes its marker only once the harness has settled AND its
    child-spawn check has finished, which is why the probe holds stdin open until
    the marker exists and closes it afterwards -- stdin EOF is the profile's own
    shutdown, and one that arrived earlier would tear the harness down under the
    check. ``stdin_closed_after_marker`` records that order.
    """

    instances: list["_FakeProbeProcess"] = []

    def __init__(self, argv, marker_writer, marker_path, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.marker_path = marker_path
        self.stdin = io.BytesIO()
        self.returncode: int | None = None
        self.killed = False
        self.stdin_closed_after_marker: bool | None = None
        _FakeProbeProcess.instances.append(self)
        marker_writer(marker_path)
        self._marker_written = marker_path.exists() or marker_path.is_symlink()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.stdin_closed_after_marker is None:
            self.stdin_closed_after_marker = self.stdin.closed and self._marker_written
        if self.returncode is None:
            # A live harness ends on stdin EOF within a positive budget and never
            # within a zero one; a killed one is already ended.
            if not timeout and not self.killed:
                raise subprocess.TimeoutExpired(self.argv, timeout or 0)
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _deepseek_gate_verifier(tmp_path, monkeypatch, marker_writer, *, names: tuple[str, ...] = ()):
    """Return a verifier closure whose fake probe places the requested marker shape.

    The third element is the fake process the probe drove, carrying the ``Popen``
    keyword arguments (``kwargs``) and the environment the harness was booted with.
    """
    marker_path = tmp_path / "gate-marker.json"
    extension_path = _gate_extension_path(tmp_path)
    extension_path.write_text("// stand-in for the shipped gate plugin\n", encoding="utf-8")
    _FakeProbeProcess.instances.clear()

    def fake_popen(argv, **kwargs):
        return _FakeProbeProcess(argv, marker_writer, marker_path, **kwargs)

    monkeypatch.setattr(deepseek_mod.subprocess_mod, "Popen", fake_popen)
    # The probe waits for the marker up to the read-back budget; a fake that never
    # writes one must fail in test time rather than in a minute.
    monkeypatch.setattr(deepseek_mod, "_DSH_GATE_READBACK_TIMEOUT_S", 0.5)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)

    def verify():
        return deepseek_mod._verify_deepseek_gate(
            client,
            ACP_BACKEND_DEEPSEEK,
            ["/fake/dsh", "--profile", "acp"],
            str(extension_path),
            str(marker_path),
            "n1",
            child_scrub_names=names,
        )

    def probe_process() -> _FakeProbeProcess:
        assert _FakeProbeProcess.instances, "the probe never booted the harness"
        return _FakeProbeProcess.instances[-1]

    return verify, marker_path, probe_process


def test_the_probe_discards_output_and_a_good_bounded_marker_is_admitted(
    tmp_path, monkeypatch
) -> None:
    """The probe's only output is the file; stdin EOF is still its shutdown signal.

    But EOF is sent only AFTER the marker exists: the plugin's child-spawn check runs
    after readiness, and the ACP profile binds EOF to a shutdown that disposes the
    subprocess service -- an EOF handed over at boot would kill the check's child
    under it and refuse every clean session as unverified.
    """

    def write_marker(path):
        marker = _good_gate_marker(_gate_extension_path(tmp_path).as_uri())
        path.write_text(json.dumps(marker), encoding="utf-8")

    verify, _marker_path, probe_process = _deepseek_gate_verifier(
        tmp_path, monkeypatch, write_marker
    )

    assert verify() == ("", "")
    probe = probe_process()
    assert probe.kwargs["stdin"] == subprocess.PIPE
    assert probe.kwargs["stdout"] == subprocess.DEVNULL
    assert probe.kwargs["stderr"] == subprocess.DEVNULL
    assert probe.stdin.closed, "the probe never sent the harness its EOF shutdown"
    assert (
        probe.stdin_closed_after_marker is True
    ), "stdin must be closed only after the marker exists, or the shutdown races the check"
    assert not probe.killed
    assert "input" not in probe.kwargs
    assert "capture_output" not in probe.kwargs
    assert "text" not in probe.kwargs
    assert "errors" not in probe.kwargs


def test_a_probe_whose_plugin_never_writes_a_marker_is_refused_and_ended(
    tmp_path, monkeypatch
) -> None:
    """No marker within the budget is a refusal, and the harness is not left running."""
    verify, _marker_path, probe_process = _deepseek_gate_verifier(
        tmp_path, monkeypatch, lambda path: None
    )

    issue, remedy = verify()
    assert issue and "load marker" in issue
    assert remedy
    probe = probe_process()
    assert probe.killed, "a harness that produced no marker in time must be ended"


def test_the_probe_hands_the_plugin_a_canary_under_each_vault_fed_name_and_never_the_key(
    tmp_path, monkeypatch
) -> None:
    """The scrub PROPERTY is verified per configured name, on a value that is not the key.

    The plugin can only observe whether a name reaches a harness child if the name
    is SET in the harness's own environment, so the probe sets each vault-fed name
    -- to a canary carrying this probe's nonce, never to the vault's plaintext,
    because the probe boots a third-party plugin host that needs no provider key.
    The names themselves ride in a dedicated variable so the plugin checks exactly
    the set Crew configured and the marker cannot claim a different one.
    """
    from kiro_crew.acp import client as client_module

    names = ("DEEPSEEK_API_KEY", "PROBE_SECRET")

    def write_marker(path):
        marker = _good_gate_marker(_gate_extension_path(tmp_path).as_uri(), names)
        path.write_text(json.dumps(marker), encoding="utf-8")

    verify, _marker_path, probe_process = _deepseek_gate_verifier(
        tmp_path, monkeypatch, write_marker, names=names
    )

    assert verify() == ("", "")
    env = probe_process().kwargs["env"]
    assert env[client_module._ENV_DSH_GATE_SCRUB_NAMES] == ":".join(names)
    for name in names:
        assert "n1" in env[name], "the canary must be attributable to this probe"
        assert env[name].startswith(client_module._DSH_GATE_SCRUB_CANARY_PREFIX)
    # Every canary is in the harness's own scrub class -- that is the validator's
    # own rule for a mapping -- so a compliant harness drops it from every child.
    for name in names:
        assert client_module._DEEPSEEK_ENV_CHILD_SCRUB_CLASS.search(name)


def test_the_probe_refuses_a_marker_that_checked_a_different_name_set(
    tmp_path, monkeypatch
) -> None:
    """A plugin that verified nothing -- or something else -- cannot vouch for the key."""

    def write_marker(path):
        marker = _good_gate_marker(_gate_extension_path(tmp_path).as_uri(), ())
        path.write_text(json.dumps(marker), encoding="utf-8")

    verify, _marker_path, _probe = _deepseek_gate_verifier(
        tmp_path, monkeypatch, write_marker, names=("DEEPSEEK_API_KEY",)
    )

    issue, _remedy = verify()
    assert issue and "DEEPSEEK_API_KEY" in issue


def test_an_oversize_but_otherwise_valid_marker_is_refused(tmp_path, monkeypatch) -> None:
    """Size is checked before parsing, so a child cannot allocate an unbounded object."""
    from kiro_crew.acp import client as client_module

    def write_marker(path):
        marker = {
            **_good_gate_marker(_gate_extension_path(tmp_path).as_uri()),
            "padding": "x" * client_module._DSH_GATE_MARKER_MAX_BYTES,
        }
        path.write_text(json.dumps(marker), encoding="utf-8")

    verify, marker_path, _run_kwargs = _deepseek_gate_verifier(tmp_path, monkeypatch, write_marker)

    issue, _remedy = verify()
    assert marker_path.stat().st_size > client_module._DSH_GATE_MARKER_MAX_BYTES
    assert issue, "an otherwise-admissible marker beyond the cap must be malformed"


def test_a_marker_that_grows_past_its_fstat_size_is_refused(tmp_path, monkeypatch) -> None:
    """One extra byte rejects a file changed between metadata capture and its read."""
    from kiro_crew.acp import client as client_module

    def write_marker(path):
        marker = _good_gate_marker(_gate_extension_path(tmp_path).as_uri())
        path.write_text(json.dumps(marker), encoding="utf-8")

    verify, _marker_path, _run_kwargs = _deepseek_gate_verifier(tmp_path, monkeypatch, write_marker)
    real_read = os.read

    def read_with_growth(fd, size):
        return real_read(fd, size) + b"x"

    monkeypatch.setattr(client_module.os, "read", read_with_growth)

    issue, _remedy = verify()
    assert issue, "a marker that outgrew the fstat snapshot must be malformed"


def test_a_symlinked_marker_is_refused_even_when_its_target_is_valid(tmp_path, monkeypatch) -> None:
    """The read-back judges the marker path itself, never a link target's valid JSON."""
    target = tmp_path / "valid-marker-target.json"
    target.write_text(
        json.dumps(_good_gate_marker(_gate_extension_path(tmp_path).as_uri())), encoding="utf-8"
    )

    def link_marker(path):
        path.symlink_to(target)

    verify, _marker_path, _run_kwargs = _deepseek_gate_verifier(tmp_path, monkeypatch, link_marker)

    issue, _remedy = verify()
    assert issue, "a valid marker reached through a symlink must still be malformed"


def test_a_marker_path_that_is_a_directory_link_or_reparse_point_is_refused(
    tmp_path, monkeypatch
) -> None:
    """Windows junctions and POSIX directory links are refused at the final name."""
    target = tmp_path / "marker-target-directory"
    target.mkdir()

    def link_marker(path):
        make_dir_link(path, target)

    verify, _marker_path, _run_kwargs = _deepseek_gate_verifier(tmp_path, monkeypatch, link_marker)

    issue, _remedy = verify()
    assert issue, "a marker path that redirects to a directory must be malformed"


def test_a_directory_at_the_marker_path_is_refused(tmp_path, monkeypatch) -> None:
    """Only a regular file can witness the plugin's startup state."""
    verify, _marker_path, _run_kwargs = _deepseek_gate_verifier(
        tmp_path, monkeypatch, lambda path: path.mkdir()
    )

    issue, _remedy = verify()
    assert issue, "a directory at the marker path must be malformed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs")
def test_a_fifo_at_the_marker_path_is_refused_without_blocking(tmp_path, monkeypatch) -> None:
    """A regression fails by name instead of wedging the pytest worker."""
    verify, marker_path, _run_kwargs = _deepseek_gate_verifier(tmp_path, monkeypatch, os.mkfifo)
    outcome: list[tuple[str, str]] = []
    worker = threading.Thread(target=lambda: outcome.append(verify()), daemon=True)
    worker.start()
    worker.join(timeout=5)
    try:
        assert not worker.is_alive(), "the marker open blocked on a FIFO"
    finally:
        if worker.is_alive():
            writer = os.open(marker_path, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            worker.join(timeout=5)
    assert outcome and outcome[0][0], "a FIFO at the marker path must be malformed"


def test_the_routing_is_a_verified_gate_extension_and_therefore_enforced() -> None:
    """``VERIFIED_GATE_EXTENSION`` is the honest member once Crew ships the gate.

    This harness's sandbox decides an action
    itself, and its own ``session/request_permission`` carries only a
    model-initiated escalation, so there is no SETTING to seed -- which is why this
    is not ``VERIFIED_SEEDED_SETTINGS``. What makes it routed is a plugin Kiro Crew
    composes into it, answering the harness's own ``tools/pre-execute`` waterfall,
    and a read-back that confirms the plugin loaded before the first prompt.
    """
    from kiro_crew import acp_tool_gate as gate

    assert routing_for(ACP_BACKEND_DEEPSEEK) is Routing.VERIFIED_GATE_EXTENSION
    assert gate.is_enforced(ACP_BACKEND_DEEPSEEK) is True
    verdict, reason = gate.routing_verdict(ACP_BACKEND_DEEPSEEK)
    assert verdict is gate.Verdict.ROUTED
    # The verdict must name the read-back that actually runs, because the refusal
    # and the doctor row quote it: a harness whose verdict described the sibling
    # mechanism would send an operator to a registry this harness does not publish.
    assert "load marker" in reason


def test_the_gate_is_read_back_through_its_load_marker() -> None:
    """The style is declared as data, and it is the weaker of the two on purpose.

    This harness's ACP profile adds no method, capability or ``_meta`` field, so
    there is no registry to ask the way pi is asked. The marker closes the same
    precondition from the other side, and each of its three fields refuses a
    distinct failure: a foreign marker, a stale one, and one written by a plugin
    that is not Kiro Crew's.
    """
    from kiro_crew import acp_tool_gate as gate
    from kiro_crew.acp_backends import Readback, gate_readback_for

    assert gate_readback_for(ACP_BACKEND_DEEPSEEK) is Readback.LOAD_MARKER
    probe = gate.gate_probe_command_for(ACP_BACKEND_DEEPSEEK)
    assert probe == "kiro-crew-tool-gate"
    good = _good_gate_marker()
    assert gate.gate_marker_issue(ACP_BACKEND_DEEPSEEK, good, "/sealed/gate.mjs", "n1") == ""
    # Absent, foreign, stale, and from another file all refuse.
    assert gate.gate_marker_issue(ACP_BACKEND_DEEPSEEK, None, "/sealed/gate.mjs", "n1")
    assert gate.gate_marker_issue(
        ACP_BACKEND_DEEPSEEK, {**good, "plugin": "someone-else"}, "/sealed/gate.mjs", "n1"
    )
    assert gate.gate_marker_issue(
        ACP_BACKEND_DEEPSEEK, {**good, "nonce": "stale"}, "/sealed/gate.mjs", "n1"
    )
    assert gate.gate_marker_issue(
        ACP_BACKEND_DEEPSEEK, {**good, "module": "/operator/own.mjs"}, "/sealed/gate.mjs", "n1"
    )
    # And a session that issued no nonce cannot attribute any marker to itself.
    assert gate.gate_marker_issue(ACP_BACKEND_DEEPSEEK, good, "/sealed/gate.mjs", "")


def test_the_marker_must_report_the_composed_tool_presentation_as_native() -> None:
    """The ``native`` pin is VERIFIED from the composed service, not trusted to precedence.

    The per-launch patch pins ``tools.mode: native`` and wins because a ``--patch``
    overlay is applied after the operator layer -- an ordering observed at one
    harness version. The plugin snapshots the mode the tools service actually
    composed, and Crew requires ``native`` the same way it requires approval policy
    ``ask``: under ``ptc`` or ``both`` the model reaches Node's own APIs through one
    ``run_code`` call the gate cannot read inside.
    """
    from kiro_crew import acp_tool_gate as gate

    good = _good_gate_marker()
    assert gate.gate_marker_issue(ACP_BACKEND_DEEPSEEK, good, "/sealed/gate.mjs", "n1") == ""
    for mode in ("ptc", "both", None, 7):
        issue = gate.gate_marker_issue(
            ACP_BACKEND_DEEPSEEK, {**good, "tools": {"mode": mode}}, "/sealed/gate.mjs", "n1"
        )
        assert issue and "native" in issue, mode
    no_snapshot = {k: v for k, v in good.items() if k != "tools"}
    assert "native" in gate.gate_marker_issue(
        ACP_BACKEND_DEEPSEEK, no_snapshot, "/sealed/gate.mjs", "n1"
    )
    assert gate.gate_marker_issue(
        ACP_BACKEND_DEEPSEEK, {**good, "tools": "native"}, "/sealed/gate.mjs", "n1"
    )


def test_the_marker_must_prove_each_vault_fed_name_is_absent_from_a_harness_child() -> None:
    """The child-env scrub is a PROPERTY read off a real child, not a regex Crew mirrors.

    ``host_vault`` promises the provider key is invisible to the shells the model
    drives. That rests on the harness's own ``scrubbedParentEnv()``, which a harness
    release can narrow or drop with no in-band signal -- so the plugin spawns one
    trivial child through the harness's own subprocess service during the probe and
    records, per vault-fed name, whether it reached that child. Crew refuses when any
    did, when a name was not even set in the harness's own environment (nothing was
    verified), when the plugin checked a different set of names than Crew
    configured, or when the child could not be run at all.
    """
    from kiro_crew import acp_tool_gate as gate

    names = ("DEEPSEEK_API_KEY",)
    good = _good_gate_marker(names=names)
    ok = gate.gate_marker_issue(
        ACP_BACKEND_DEEPSEEK, good, "/sealed/gate.mjs", "n1", child_scrub_names=names
    )
    assert ok == ""

    def issue_for(child_env: object, expected: tuple[str, ...] = names) -> str:
        marker = {**good, "child_env": child_env}
        return gate.gate_marker_issue(
            ACP_BACKEND_DEEPSEEK, marker, "/sealed/gate.mjs", "n1", child_scrub_names=expected
        )

    base = dict(good["child_env"])
    # The leak itself: the name reached the child, so the key would reach the shells.
    leaked = issue_for({**base, "child_visible": ["DEEPSEEK_API_KEY"]})
    assert leaked and "DEEPSEEK_API_KEY" in leaked and "shell" in leaked
    # Nothing verified: the canary never reached the harness's own environment.
    assert issue_for({**base, "parent_missing": ["DEEPSEEK_API_KEY"]})
    # The check could not run.
    assert issue_for({**base, "error": "spawn failed: ENOENT"})
    # A different set than Crew configured -- fewer, more, or none.
    assert issue_for({**base, "names": []})
    assert issue_for({**base, "names": ["DEEPSEEK_API_KEY", "OTHER_TOKEN"]})
    assert issue_for({**base, "names": ["DEEPSEEK_API_KEY"]}, expected=("OTHER_TOKEN",))
    # Malformed or absent snapshots refuse rather than pass.
    assert issue_for(None)
    assert issue_for("checked")
    assert issue_for({**base, "child_visible": "DEEPSEEK_API_KEY"})
    assert issue_for({k: v for k, v in base.items() if k != "child_visible"})
    # With nothing configured there is nothing to protect, but the snapshot must still
    # be present and honest: an empty set checked, and the child still ran.
    empty = _good_gate_marker()
    assert gate.gate_marker_issue(ACP_BACKEND_DEEPSEEK, empty, "/sealed/gate.mjs", "n1") == ""
    assert gate.gate_marker_issue(
        ACP_BACKEND_DEEPSEEK,
        {**empty, "child_env": {**empty["child_env"], "names": ["DEEPSEEK_API_KEY"]}},
        "/sealed/gate.mjs",
        "n1",
    )
    # The version is recorded, not judged: a harness that reports none still admits.
    assert (
        gate.gate_marker_issue(
            ACP_BACKEND_DEEPSEEK,
            {**good, "child_env": {**base, "version": None}},
            "/sealed/gate.mjs",
            "n1",
            child_scrub_names=names,
        )
        == ""
    )


def test_the_shipped_plugin_snapshots_the_tools_mode_and_proves_the_child_scrub() -> None:
    """The plugin's marker carries both new snapshots, taken through the harness's own seams."""
    from kiro_crew.acp import client as client_module

    source = pathlib.Path(client_module.deepseek_gate_extension_path()).read_text()
    # The subprocess service is injected, so `apply` running is itself the evidence
    # that the harness composed one -- and the check spawns through IT, not through
    # Node's own child_process, because the property under test is that service's.
    assert 'export const inject = ["tools", "approval", "appReady", "subprocess"]' in source
    assert "ctx.subprocess.spawn(" in source
    assert "child_process" not in source
    assert "KIROCREW_DSH_GATE_SCRUB_NAMES" in source
    assert "defaultMode" in source
    # The marker is PUBLISHED atomically -- written beside its path and renamed into
    # place -- because the probe now polls for it while the harness is still alive.
    assert "renameSync(" in source
    # And the marker is written on EVERY outcome of the check, error included: the
    # readiness listener's promise is not awaited by the harness, so a rejection
    # there would be an unhandled rejection rather than a refusal Crew can name.
    assert "error:" in source


def test_the_session_writes_no_forensic_marker_of_its_own(monkeypatch, tmp_path) -> None:
    """The only load marker is the probe's, which is the verification.

    An earlier revision also had the SESSION write a marker into its own private
    window and refused the session when no window had been allocated. Nothing ever
    read that marker -- the gate is verified against the probe's -- so the write was
    a new refusal path with no consumer, justified only by symmetry with the probe.
    It is gone: the plugin skips the write when its marker variable is unset, the
    session sets none, and a session without a private window is treated the way
    every other backend treats it, as hygiene the shared site already tolerates.
    """
    from kiro_crew.acp import client as client_module

    rig = _DeepseekLaunchRig(monkeypatch, tmp_path)
    rig.run()
    env: dict[str, str] = {}
    rig.adapter.apply_spawn_env(env)
    assert set(env) == {
        client_module._ENV_DEEPSEEK_PERMISSION_MODE,
        client_module._ENV_DSH_GATE_SESSION,
    }, "the session's child names a marker (or the probe's canary list) after all"
    assert not hasattr(rig.client, "_deepseek_gate_marker")
    # The probe's marker is the one that is read: it lives in the probe's OWN window
    # and carries this launch's nonce in its name.
    _argv, _extension, marker_path, nonce, _kwargs = rig.args_of("verify")
    assert pathlib.Path(marker_path).parent == rig.probe_dir
    assert nonce and nonce in pathlib.Path(marker_path).name
    assert nonce == rig.client._deepseek_gate_nonce
    # The plugin tolerates the variable being absent, which is what the session
    # relies on: no marker path means no write, not a crash before the gate arms.
    source = pathlib.Path(client_module.deepseek_gate_extension_path()).read_text()
    assert "if (!marker) return;" in source


def test_the_probe_names_the_marker_it_reads(tmp_path, monkeypatch) -> None:
    """The probe boots the harness with the marker path and the nonce it then judges."""

    def write_marker(path):
        marker = _good_gate_marker(_gate_extension_path(tmp_path).as_uri())
        path.write_text(json.dumps(marker), encoding="utf-8")

    verify, marker_path, probe_process = _deepseek_gate_verifier(
        tmp_path, monkeypatch, write_marker
    )
    assert verify() == ("", "")
    env = probe_process().kwargs["env"]
    assert env[deepseek_mod._ENV_DSH_GATE_MARKER] == str(marker_path)
    assert env[deepseek_mod._ENV_DSH_GATE_SESSION] == "n1"


def test_the_shipped_gate_plugin_matches_the_pinned_digest() -> None:
    """Editing the plugin is a deliberate two-file edit, as it is for pi.

    The plugin is package data, which on a source or user install the agent's own
    file tools may be able to write. The seal is what makes the read-back mean
    something: a rewritten package file is refused at spawn rather than composed.
    """
    import hashlib

    from kiro_crew.acp import client as client_module

    path = client_module.deepseek_gate_extension_path()
    with open(path, "rb") as fh:
        payload = fh.read().replace(b"\r\n", b"\n")
    assert (
        hashlib.sha256(payload).hexdigest() == client_module.DEEPSEEK_GATE_EXTENSION_SHA256
    ), "the shipped gate plugin changed without its pinned digest"


def test_the_gate_writers_share_one_seal_and_one_publish_block(monkeypatch, tmp_path) -> None:
    """Three writers, one seal and one publish -- so the two cannot drift again.

    The pi seal, the DeepSeek seal and the DeepSeek patch writer once each carried
    their own read -> LF-normalize -> digest -> write-if-changed -> stage/chmod/replace
    body, and had already diverged: pi's guarded ``chmod`` for Windows, the two new
    copies did not. Each writer is observed going through the one seal and the one
    publish, each resolving the artifact directory through the strict resolver, and
    each artifact lands there read-only.
    """
    artifact_dir = tmp_path / "pi-gate"
    artifact_dir.mkdir()
    resolved: list[str] = []
    sealed_sources: list[str] = []
    published: list[str] = []
    real_seal = pi_mod._seal_gate_extension
    real_publish = pi_mod._publish_gate_artifact

    def _strict() -> str:
        resolved.append("asked")
        return str(artifact_dir)

    def _seal(source, digest, **kwargs):
        sealed_sources.append(source)
        return real_seal(source, digest, **kwargs)

    def _publish(directory, name, payload, **kwargs):
        published.append(name)
        return real_publish(directory, name, payload, **kwargs)

    monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", _strict)
    monkeypatch.setattr(pi_mod, "_seal_gate_extension", _seal)
    monkeypatch.setattr(pi_mod, "_publish_gate_artifact", _publish)

    artifacts = [pi_mod._seal_pi_gate_extension(), deepseek_mod._seal_deepseek_gate_extension()]
    artifacts.append(deepseek_mod._write_deepseek_gate_patch(artifacts[1]))

    assert sealed_sources == [
        pi_mod.pi_gate_extension_path(),
        deepseek_mod.deepseek_gate_extension_path(),
    ], "a seal stopped going through the one shared seal"
    assert sorted(published) == sorted(
        pathlib.Path(path).name for path in artifacts
    ), "a writer stopped landing its artifact through the one publish block"
    assert (
        len(resolved) == 3
    ), "a writer stopped resolving its directory through the strict resolver"
    for path in artifacts:
        assert pathlib.Path(path).parent == artifact_dir
        if os.name != "nt":
            assert (pathlib.Path(path).stat().st_mode & 0o777) == 0o400, path


def test_the_one_publish_block_stages_beside_the_target_and_renames_it_in(
    monkeypatch, tmp_path
) -> None:
    """A gate artifact appears whole or not at all: staged in its own directory, then
    renamed over the target, so a spawn that reads it mid-write never loads half a
    plugin. An in-place write of the target would pass every other test here."""
    renames: list[tuple[str, str]] = []
    real_replace = os.replace

    def _replace(src, dst):
        renames.append((str(src), str(dst)))
        assert not pathlib.Path(dst).exists(), "the target existed before the rename"
        return real_replace(src, dst)

    monkeypatch.setattr(pi_mod.os, "replace", _replace)
    target = pi_mod._publish_gate_artifact(
        str(tmp_path), "gate.mjs", b"export {};\n", stage_prefix=".gate-stage-"
    )

    assert pathlib.Path(target).read_bytes() == b"export {};\n"
    assert len(renames) == 1
    staged, landed = renames[0]
    assert landed == target and pathlib.Path(staged).parent == tmp_path
    assert pathlib.Path(staged).name.startswith(".gate-stage-")
    # Unchanged bytes are not rewritten.
    assert (
        pi_mod._publish_gate_artifact(
            str(tmp_path), "gate.mjs", b"export {};\n", stage_prefix=".gate-stage-"
        )
        == target
    )
    assert len(renames) == 1


def test_the_one_publish_block_leaves_the_mode_to_the_dacl_on_windows(
    monkeypatch, tmp_path
) -> None:
    """The one Windows guard: ``chmod`` is inert there, and the directory DACL is the seal."""
    from kiro_crew import platform_compat

    chmods: list[tuple] = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(pi_mod.os, "chmod", lambda *args, **_kw: chmods.append(args))
    target = pi_mod._publish_gate_artifact(
        str(tmp_path), "kirocrew_dsh_gate_1.mjs", b"// gate\n", stage_prefix="kirocrew_dsh_gate_1_"
    )
    assert pathlib.Path(target).read_bytes() == b"// gate\n"
    assert chmods == [], "the publish block chmods a file where the mode means nothing"


def test_the_marker_waits_until_every_profile_entry_has_settled() -> None:
    """A pre-readiness snapshot can race the ACP bridge and refuse every clean boot."""
    from kiro_crew.acp import client as client_module

    source = pathlib.Path(client_module.deepseek_gate_extension_path()).read_text()
    assert 'export const inject = ["tools", "approval", "appReady", "subprocess"]' in source
    ready = source.index("ctx.appReady.onReady")
    snapshot = source.index("approval: approvalRouting(ctx)", ready)
    assert ready < snapshot


def test_the_per_launch_patch_composes_only_the_sealed_plugin() -> None:
    """The patch is the composition channel, so what it names is load-bearing.

    It must name the SEALED copy rather than the package file, because the sealed
    copy is what the digest was verified over and what the marker must report, and
    it must quote the path so a run directory containing a quote cannot end the
    YAML scalar early.
    """
    import json as _json

    from kiro_crew.acp import client as client_module

    sealed = client_module._seal_deepseek_gate_extension()
    patch = client_module._write_deepseek_gate_patch(sealed)
    body = open(patch, encoding="utf-8").read()
    assert "- insert:" in body
    assert "id: kiro-crew-tool-gate" in body
    assert f"name: {_json.dumps(sealed)}" in body


def test_the_patch_pins_the_tool_presentation_to_native() -> None:
    """``run_code`` must never be on the menu, because the gate cannot read inside it.

    Under ``ptc`` or ``both`` this harness replaces native tool schemas with one
    reserved ``run_code`` transport, and a program inside it reaches Node's own
    filesystem, network and subprocess APIs directly. Those are not tool calls, so
    they never traverse ``tools/pre-execute`` and no command or path rule can apply
    to them -- the gate would see a single opaque call. ``native`` is the harness's
    own default, so this row changes nothing on a default install; it exists so an
    operator layer cannot select a mode that carries side effects around the gate,
    and a ``--patch`` overlay is applied after the profile's own layer so the pin
    wins.
    """
    from kiro_crew.acp import client as client_module

    sealed = client_module._seal_deepseek_gate_extension()
    body = open(client_module._write_deepseek_gate_patch(sealed), encoding="utf-8").read()
    assert "id: tools" in body
    assert "mode: native" in body


def test_the_patch_reasserts_the_approval_service_and_acp_bridge() -> None:
    """The final overlay repairs mutable row fields and read-back catches replacements.

    ``applyEntryPatches`` treats ``name`` as a match guard, not an assignment, so
    these rows add real protection against an earlier disable/config/inject change
    without pretending they can overwrite a replaced module. The marker's strict
    owner check is the fail-closed half for that shape.
    """
    from kiro_crew.acp import client as client_module

    sealed = client_module._seal_deepseek_gate_extension()
    body = open(client_module._write_deepseek_gate_patch(sealed), encoding="utf-8").read()
    assert "id: approval" in body
    assert "name: '@deepseek-ai/dsh-user-approval'" in body
    assert "policy: ask" in body
    assert "id: acp" in body
    assert "name: '@deepseek-ai/dsh-acp'" in body
    assert "disabled: false" in body
    assert "- acpAppStartup" in body


def test_a_spawn_that_cannot_resolve_its_binary_leaves_no_scratch_behind(
    tmp_path, monkeypatch
) -> None:
    """A failed spawn must not leave a scratch directory the sweep will keep forever.

    ``allocate_scratch`` records the SPAWNING process -- the gateway -- as the
    directory's provisional owner, and the sweep deletes a directory only when its
    recorded owner's process group is DEAD and the tree has been idle. The gateway
    is long-lived, so a directory allocated and then abandoned by a failing spawn is
    retained for the gateway's whole lifetime, and one more is retained per retry.

    The kiro construction path is where that matters most: it must gain no failure
    point in service of an adapter (harness-parity H13). So this drives the kiro
    backend to the one failure every host can reach -- an unresolvable binary -- and
    asserts the managed root gained nothing.
    """
    import asyncio

    from kiro_crew import agent_scratch
    from kiro_crew.acp import client as client_module

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    root = agent_scratch.scratch_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = {p.name for p in root.iterdir()}

    client = AcpClient(work_dir=tmp_path)
    # The resolution the kiro arm performs, made to fail the way a host without
    # kiro-cli fails: resolved to nothing rather than raised, so the arm takes its
    # own not-found path instead of an exception from the resolver.
    monkeypatch.setattr(client_module, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value=None))

    with pytest.raises(Exception):
        asyncio.run(client._spawn())

    after = {p.name for p in root.iterdir()}
    leaked = sorted(after - before)
    assert not leaked, (
        f"the failed spawn left {leaked} in the managed scratch root; each carries the "
        "gateway's pid as its provisional owner, so the liveness-keyed sweep will "
        "never reclaim it"
    )


def _deepseek_readback_spawn(tmp_path, monkeypatch, *, readback):
    """Drive a deepseek ``_spawn`` through its read-back with nothing real spawned.

    The gate's read-back owns a THROWAWAY scratch window of its own, so
    ``allocate_scratch`` is the one thing here left real -- it is the subject. The
    launch resolution, the credential-mask preflight, the seal/patch writers and the
    sandbox wrap are all replaced, and the wrap raises on its SECOND call (the real
    child's) so the spawn ends immediately after the arm instead of reaching a
    process start.

    Returns ``(client, root, before)``: the client, the managed scratch root, and the
    set of directory names that existed before the spawn.
    """
    import asyncio

    from kiro_crew import agent_scratch
    from kiro_crew.acp import client as client_module

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    root = agent_scratch.scratch_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    before = {p.name for p in root.iterdir()}

    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)
    argv = ["/fake/dsh", "--profile", "acp"]
    monkeypatch.setattr(
        launch_mod,
        "resolve_self_served_launch",
        AsyncMock(return_value=("/fake/dsh", list(argv), "dsh", "dsh")),
    )
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", AsyncMock(return_value=()))
    monkeypatch.setattr(
        deepseek_mod, "_seal_deepseek_gate_extension", lambda: str(tmp_path / "sealed.mjs")
    )
    monkeypatch.setattr(
        deepseek_mod, "_write_deepseek_gate_patch", lambda _ext: str(tmp_path / "patch.json")
    )

    # The probe's wrap answers; the real child's wrap is where this spawn stops.
    wrapped: list[tuple[str, ...]] = []

    async def fake_wrap(call_argv, **kwargs):
        private = tuple(kwargs.get("extra_private_dirs", ()))
        wrapped.append(private)
        if len(wrapped) > 1:
            raise _SpawnStopped("the real child's wrap is not exercised by this test")
        return list(call_argv), None

    # The probe's wrap is the adapter's; the real child's is the launch tail's, which
    # the client hands its own binding.
    monkeypatch.setattr(deepseek_mod, "wrap_argv_async", fake_wrap)
    monkeypatch.setattr(client_module, "wrap_argv_async", fake_wrap)
    monkeypatch.setattr(deepseek_mod, "_verify_deepseek_gate", lambda *a, **k: readback)

    return client, root, before, wrapped, asyncio


class _SpawnStopped(Exception):
    """Sentinel: the spawn reached the real child's wrap, which the test stops at."""


def _probe_dirs(root) -> list[str]:
    return sorted(p.name for p in root.iterdir() if "dsh-probe" in p.name)


def test_a_successful_read_back_removes_its_own_probe_window(tmp_path, monkeypatch) -> None:
    """The probe's window is removed by the arm, because nothing else ever will.

    ``allocate_scratch`` records the SPAWNING process -- the long-lived gateway --
    as the window's provisional owner, and the sweep reclaims only
    owned-and-dead-and-idle directories. So a probe window left behind is retained
    for the gateway's whole lifetime, and one more per session start. What remains
    after a read-back that PASSED is the session's own window and nothing else: the
    two are deliberately separate allocations, which is what keeps the shared
    construction site free of this adapter (harness-parity H13).
    """
    client, root, before, wrapped, asyncio_mod = _deepseek_readback_spawn(
        tmp_path, monkeypatch, readback=("", "")
    )

    with pytest.raises(_SpawnStopped):
        asyncio_mod.run(client._spawn())

    assert not _probe_dirs(root), (
        f"the read-back left {_probe_dirs(root)} behind; the gateway is its "
        "provisional owner and is alive, so the sweep will never reclaim it"
    )
    # The probe was given a window of its OWN, not the session's: the session's
    # allocation does not exist yet when the probe's wrap is called.
    assert len(wrapped[0]) == 1 and "dsh-probe" in wrapped[0][0]
    remaining = sorted(p.name for p in root.iterdir() if p.name not in before)
    assert (
        len(remaining) == 1 and "dsh-probe" not in remaining[0]
    ), f"expected only the session's own scratch window to remain, got {remaining}"


def test_a_refused_read_back_removes_its_own_probe_window(tmp_path, monkeypatch) -> None:
    """A refusal must not pay for itself with a directory nothing reclaims.

    The refusal is raised from inside the arm, before the shared site allocates the
    session's window, so the managed root gains NOTHING at all here -- the ``finally``
    that removes the probe window runs on the refusal path as well as the passing one.
    """
    client, root, before, _wrapped, asyncio_mod = _deepseek_readback_spawn(
        tmp_path, monkeypatch, readback=("the gate did not load", "reinstall it")
    )

    from kiro_crew.acp.client import AcpToolGateUnroutable

    with pytest.raises(AcpToolGateUnroutable):
        asyncio_mod.run(client._spawn())

    leaked = sorted(p.name for p in root.iterdir() if p.name not in before)
    assert not leaked, (
        f"the refused read-back left {leaked} in the managed scratch root; the "
        "gateway's pid is their provisional owner, so the sweep never reclaims them"
    )


def test_a_session_with_no_private_window_is_started_not_refused(tmp_path, monkeypatch) -> None:
    """A session without a private scratch window is hygiene, exactly as on every backend.

    The read-back refuses a PROBE that gets no window, because the probe's marker is
    the verification and it has to land somewhere. The SESSION writes no marker at
    all -- an earlier revision had it write a forensic one nothing read, and refused
    the session when no window had been allocated, a refusal path with zero
    consumers. So the shared site's fail-open allocation stands for this harness
    too: the child is built with the nonce (the tripwire keys on it) and no marker
    path, and the launcher the wrap wrote is handed to the process start rather than
    discarded.
    """
    from kiro_crew import agent_scratch
    from kiro_crew.acp import client as client_module

    client, root, before, _wrapped, asyncio_mod = _deepseek_readback_spawn(
        tmp_path, monkeypatch, readback=("", "")
    )
    # Every wrap succeeds here (the probe's AND the real child's) so the spawn reaches
    # the env build; the process start itself is never reached.
    calls: list[tuple[str, ...]] = []

    async def wrap_ok(call_argv, **kwargs):
        calls.append(tuple(kwargs.get("extra_private_dirs", ())))
        return list(call_argv), None

    monkeypatch.setattr(deepseek_mod, "wrap_argv_async", wrap_ok)
    monkeypatch.setattr(client_module, "wrap_argv_async", wrap_ok)
    real_allocate = agent_scratch.allocate_scratch

    def allocate(label: str):
        # The probe's window is granted; the SESSION's is what the host cannot give.
        if label.endswith("-dsh-probe"):
            return real_allocate(label)
        raise OSError("no scratch for the session")

    monkeypatch.setattr(agent_scratch, "allocate_scratch", allocate)
    discarded: list[bool] = []
    monkeypatch.setattr(client, "_discard_sandbox_cleanup", lambda: discarded.append(True))
    # The first shared step after the deepseek env block: reaching it means the block
    # let the session through, and the env it carries is what the child would get.
    built: list[dict] = []

    def stop_at_identity(env):
        built.append(dict(env))
        raise _SpawnStopped("the env build completed; the process start is not exercised")

    monkeypatch.setattr(client, "_apply_session_identity_env", stop_at_identity)

    with pytest.raises(_SpawnStopped):
        asyncio_mod.run(client._spawn())
    assert discarded == [], "nothing was refused, so no launcher was discarded"
    assert len(calls) == 2, "the probe was verified and the real child wrapped"
    assert len(built) == 1
    env = built[0]
    assert env[client_module._ENV_DSH_GATE_SESSION] == client._deepseek_gate_nonce
    assert client_module._ENV_DSH_GATE_MARKER not in env, "the session names no marker path"
    assert client_module._ENV_DSH_GATE_SCRUB_NAMES not in env, "the canary list is the probe's"
    assert not _probe_dirs(root)
    assert sorted(p.name for p in root.iterdir() if p.name not in before) == []
