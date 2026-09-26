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
"""

from __future__ import annotations

import inspect
import io
import json
import os
import pathlib
import subprocess
import threading
from pathlib import PurePath
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import make_dir_link
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_handle import models_from_config_options
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CUSTOM,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_MEMBER_DISPATCH,
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
        (ACP_BACKENDS_MEMBER_DISPATCH, False),
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


def _fixture_frames(name: str) -> list[dict]:
    """Every frame of one committed fixture, header excluded."""
    import json

    path = pathlib.Path(__file__).parent / "fixtures" / "acp_frames" / "deepseek" / name
    return [json.loads(line) for line in path.read_text().splitlines()[1:]]


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
    from kiro_crew.acp import client as client_module

    marker_path = tmp_path / "gate-marker.json"
    extension_path = _gate_extension_path(tmp_path)
    extension_path.write_text("// stand-in for the shipped gate plugin\n", encoding="utf-8")
    _FakeProbeProcess.instances.clear()

    def fake_popen(argv, **kwargs):
        return _FakeProbeProcess(argv, marker_writer, marker_path, **kwargs)

    monkeypatch.setattr(client_module.subprocess_mod, "Popen", fake_popen)
    # The probe waits for the marker up to the read-back budget; a fake that never
    # writes one must fail in test time rather than in a minute.
    monkeypatch.setattr(client_module, "_DSH_GATE_READBACK_TIMEOUT_S", 0.5)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)

    def verify():
        return client._verify_deepseek_gate(
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


def test_the_session_writes_no_forensic_marker_of_its_own() -> None:
    """The only load marker is the probe's, which is the verification.

    An earlier revision also had the SESSION write a marker into its own private
    window and refused the session when no window had been allocated. Nothing ever
    read that marker -- the gate is verified against the probe's -- so the write was
    a new refusal path with no consumer, justified only by symmetry with the probe.
    It is gone: the plugin skips the write when its marker variable is unset, the
    session sets none, and a session without a private window is treated the way
    every other backend treats it, as hygiene the shared site already tolerates.
    """
    import inspect

    from kiro_crew.acp import client as client_module

    body = inspect.getsource(client_module.AcpClient._spawn)
    arm_start = "if self._is_deepseek:\n            # Pinned rather than left to the ambient value"
    assert body.count(arm_start) == 1, "the session-env deepseek block anchor is no longer unique"
    end = "self._apply_session_identity_env(env)"
    block = body[body.index(arm_start) : body.index(end, body.index(arm_start))]
    for token in ("_ENV_DSH_GATE_MARKER", "_deepseek_gate_marker", "no private scratch directory"):
        assert token not in block, f"{token!r} is back in the session's env block"
    # The probe still names the marker path, because the probe's marker is the one
    # that is read.
    probe = inspect.getsource(client_module.AcpClient._verify_deepseek_gate)
    assert "_ENV_DSH_GATE_MARKER" in probe
    client = AcpClient(work_dir=pathlib.Path("."), acp_backend=ACP_BACKEND_DEEPSEEK)
    assert not hasattr(client, "_deepseek_gate_marker")
    # The plugin tolerates the variable being absent, which is what the session
    # relies on: no marker path means no write, not a crash before the gate arms.
    source = pathlib.Path(client_module.deepseek_gate_extension_path()).read_text()
    assert "if (!marker) return;" in source


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


def test_the_gate_writers_share_one_seal_and_one_publish_block() -> None:
    """Three writers, one seal-write block -- so the two cannot drift again.

    The pi seal, the DeepSeek seal and the DeepSeek patch writer once each carried
    their own read -> LF-normalize -> digest -> write-if-changed -> stage/chmod/replace
    body, and had already diverged: pi's guarded ``chmod`` for Windows, the two new
    copies did not. Both seals now call ``_seal_gate_extension`` and all three land
    through ``_publish_gate_artifact``, which is the only place that stages a file;
    each writer still resolves the artifact directory itself, through the strict
    resolver, which is what the pi suite's text pins check per writer.
    """
    import inspect

    from kiro_crew.acp import client as client_module

    for writer in (
        client_module._seal_pi_gate_extension,
        client_module._seal_deepseek_gate_extension,
    ):
        source = inspect.getsource(writer)
        assert "_seal_gate_extension(" in source, writer.__name__
        assert "artifact_dir=_pi_gate_artifact_dir()" in source, writer.__name__
        for token in ("mkstemp", "os.replace", "hashlib", "chmod"):
            assert token not in source, f"{writer.__name__} carries its own {token}"
    patch_writer = inspect.getsource(client_module._write_deepseek_gate_patch)
    assert "_publish_gate_artifact(" in patch_writer
    for token in ("mkstemp", "os.replace", "chmod"):
        assert token not in patch_writer, f"the patch writer carries its own {token}"
    publish = inspect.getsource(client_module._publish_gate_artifact)
    assert "mkstemp" in publish and "os.replace" in publish
    # The one Windows guard, where the mode is inert and the DACL is the seal.
    assert "if not platform_compat.IS_WINDOWS:" in publish
    assert "os.chmod(tmp, 0o400)" in publish


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
        client,
        "_resolve_self_served_launch",
        AsyncMock(return_value=("/fake/dsh", list(argv), "dsh", "dsh")),
    )
    monkeypatch.setattr(client_module, "_run_preflight_bounded", AsyncMock(return_value=()))
    monkeypatch.setattr(
        client_module, "_seal_deepseek_gate_extension", lambda: str(tmp_path / "sealed.mjs")
    )
    monkeypatch.setattr(
        client_module, "_write_deepseek_gate_patch", lambda _ext: str(tmp_path / "patch.json")
    )

    # The probe's wrap answers; the real child's wrap is where this spawn stops.
    wrapped: list[tuple[str, ...]] = []

    async def fake_wrap(call_argv, **kwargs):
        private = tuple(kwargs.get("extra_private_dirs", ()))
        wrapped.append(private)
        if len(wrapped) > 1:
            raise _SpawnStopped("the real child's wrap is not exercised by this test")
        return list(call_argv), None

    monkeypatch.setattr(client_module, "wrap_argv_async", fake_wrap)
    monkeypatch.setattr(client, "_verify_deepseek_gate", lambda *a, **k: readback)

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


def test_the_shared_construction_tail_carries_nothing_from_this_adapter() -> None:
    """harness-parity H13: the Kiro construction path gains no conditional of ours.

    The shared tail between ``apply_pod_bundle_spawn`` and the real child's sandbox
    wrap is the site EVERY backend passes through, the kiro one included, so an
    adapter-specific branch there is evaluated on every Kiro session start. This
    harness's read-back therefore lives in its own arm, and this asserts the tail is
    free of it by TEXT rather than by review: a branch moved back would be caught
    here before it reached the protected path again.

    The start anchor is the call's own argument list rather than the bare function
    name: the arm's justification comment names ``apply_pod_bundle_spawn`` too, and
    an anchor that matched the comment would slice from the wrong place and read the
    arm itself as the shared tail.
    """
    body = inspect.getsource(AcpClient._spawn)
    start = "apply_pod_bundle_spawn, argv, backend=self.backend"
    end = "argv, self._sandbox_cleanup = await wrap_argv_async("
    assert body.count(start) == 1, "the shared-tail start anchor is no longer unique"
    assert body.count(end) == 1, "the shared-tail end anchor is no longer unique"
    tail = body[body.index(start) : body.index(end)]
    for token in ("_is_deepseek", "_deepseek_"):
        assert token not in tail, (
            f"{token!r} is back on the shared construction path; every Kiro session "
            "start would evaluate it (harness-parity H13)"
        )

    # And the read-back itself is inside the arm, which is the other half of the
    # same claim: removed from the tail AND present where it belongs.
    arm = body[body.index("elif self._is_deepseek:") :]
    arm = arm[: arm.index("\n        else:")]
    assert "_verify_deepseek_gate" in arm
    assert "agent_scratch.allocate_scratch" in arm

    # The path AFTER the child exists is shared too, and it is the one every Kiro
    # session start runs unconditionally: pid capture, resume, PID-file appends, the
    # descendant scan. Nothing of this adapter's may sit there either -- not a branch
    # on its identity and not a branch on state only its arm ever sets, which is the
    # same conditional wearing a neutral name (the post-exec clear of the vault-fed
    # key once lived here as ``if self._vault_secret_env_keys:``).
    pid_anchor = "self._pid = self._process.pid"
    assert body.count(pid_anchor) == 1, "the post-spawn anchor is no longer unique"
    after_spawn = body[body.index(pid_anchor) :]
    for token in ("_is_deepseek", "_deepseek_", "_vault_secret", "deepseek"):
        assert token not in after_spawn, (
            f"{token!r} is on the shared post-spawn path; every Kiro session start "
            "would evaluate it (harness-parity H13)"
        )


def test_a_denial_the_harness_reports_does_not_trip_the_ungated_call_guard() -> None:
    """A denied call cannot be mistaken for one that ran without asking.

    Two shapes of denial reach a gated session, and neither trips the guard, for two
    different reasons observed against the harness rather than assumed:

    * a SANDBOX denial happens while the tool body runs, so the call was gated
      normally first -- the plugin upgraded the downstream ``allow`` to ``ask``, the
      client answered, and only then did the sandbox refuse the write. Its id is in
      the asked set, so the ``completed`` it reports is a call the gate saw;
    * a POLICY denial from a downstream ``tools/pre-execute`` listener is preserved
      by the plugin WITHOUT asking, which is the point -- the harness's own refusal
      must not soften into a question. Such a call never runs, and the harness
      reports it ``failed`` rather than ``completed``.

    The guard only ever fires on ``completed``, so the second shape is outside it by
    construction. This pins that, because the alternative -- treating a preserved
    deny as an ungated execution -- would kill the harness every time an operator's
    own policy plugin said no.
    """
    import asyncio

    from kiro_crew.acp.types import JsonRpcMessage

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_DEEPSEEK
    client._deepseek_gate_nonce = "n0nce"
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}
    client._session_id = "s1"
    client._session_key = "k1"
    client._kill_process = AsyncMock(return_value=None)

    def update(tool_call_id: str, status: str) -> JsonRpcMessage:
        return JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "s1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "status": status,
                },
            },
        )

    # A policy denial the plugin preserved without asking: never asked, never ran,
    # reported ``failed``. Must not trip.
    asyncio.run(client._tripwire_pi_gate(update("policy-denied", "failed")))
    assert client._kill_process.await_count == 0, (
        "a call the harness itself denied was counted as an ungated execution; an "
        "operator policy plugin saying no would kill every session"
    )

    # A sandbox denial: gated normally, so its id IS in the asked set, and the
    # ``completed`` it reports carries the sandbox's refusal in the tool result.
    client._pi_gate_asked_ids.add("sandbox-denied")
    asyncio.run(client._tripwire_pi_gate(update("sandbox-denied", "completed")))
    assert client._kill_process.await_count == 0

    # The guard still has teeth for the case it exists for.
    from kiro_crew.acp.client import AcpToolGateUnroutable

    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_pi_gate(update("never-asked", "completed")))
    assert client._kill_process.await_count == 1


def test_the_load_marker_is_never_written_into_a_sealed_leaf() -> None:
    """The marker is written by the CHILD, so it cannot live where the child cannot write.

    The gate-artifact leaf is sealed read-only against every harness child on
    purpose, so that no child can plant what a later session loads
    (``sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES``). That makes it exactly the
    wrong home for the one file the child must create: a marker directed there is
    never written, the read-back reports an absent gate, and every session is
    refused for a gate that had in fact loaded. This pins the pairing itself rather
    than the current path spelling, so moving the marker back under any sealed leaf
    fails here instead of in production.
    """
    from kiro_crew import sandbox as sandbox_module
    from kiro_crew.acp import client as client_module
    from kiro_crew.agent_sdk.tool_gate import PI_GATE_ARTIFACT_LEAF

    # The leaf the gate's own code is sealed into is read-only to the child ...
    assert PI_GATE_ARTIFACT_LEAF in sandbox_module._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    # ... so the marker must not be placed in it, nor in any other sealed leaf.
    artifact_dir = pathlib.Path(client_module._pi_gate_artifact_dir())
    marker_dir = pathlib.Path("/scratch-window/session-1")
    marker = marker_dir / "kirocrew_dsh_gate_deadbeef.marker.json"
    assert artifact_dir not in marker.parents
    for leaf in sandbox_module._CREW_NOFOLLOW_READONLY_DIR_LEAVES:
        assert leaf not in marker.parts, f"the marker sits under the sealed leaf {leaf!r}"
    # The name carries the per-session nonce rather than only a pid, so two clients
    # in one gateway process cannot collide on it.
    assert "deadbeef" in marker.name


def test_every_file_the_gate_writers_leave_in_the_leaf_is_swept(monkeypatch, tmp_path) -> None:
    """The leaf's invariant is a ratchet for this harness too, not a comment.

    The pi suite pins that every name ITS writers produce is claimed by the leaf
    sweep's family table; this harness's writers landed in the same leaf without
    that pin, so the sealed plugin and its patch were never reclaimed after the
    gateway that wrote them exited -- one pair per gateway process, forever. The
    property is asserted here so a future writer dropping a differently-named file
    into the leaf fails this test rather than leaving an unswept file behind.
    """
    from kiro_crew import sandbox as sandbox_module
    from kiro_crew.acp import client as client_module

    artifact_dir = tmp_path / "pi-gate"
    artifact_dir.mkdir()
    monkeypatch.setattr(client_module, "_pi_gate_artifact_dir", lambda: str(artifact_dir))
    sealed = client_module._seal_deepseek_gate_extension()
    client_module._write_deepseek_gate_patch(sealed)
    families = sandbox_module._PI_GATE_DIR_ARTIFACTS
    written = sorted(entry.name for entry in artifact_dir.iterdir())
    assert written, "the writers produced nothing to check"
    # The mkstemp stages are renamed away on success, so they are not on disk to be
    # listed; a stage orphaned by a crash between mkstemp and replace has to be
    # reclaimed too, so both stage spellings are checked alongside the survivors.
    pid = os.getpid()
    written += [f"kirocrew_dsh_gate_{pid}_stage.tmp", f"kirocrew_dsh_patch_{pid}_stage.tmp"]
    for name in written:
        prefix = next((p for p in families if name.startswith(p)), None)
        assert prefix is not None, f"{name} is written to the leaf but no sweep family claims it"
        assert any(
            name.endswith(suffix) for suffix in families[prefix]
        ), f"{name} carries a suffix the sweep family does not reclaim"


def test_stale_dsh_gate_artifacts_are_swept_from_the_leaf(monkeypatch, tmp_path) -> None:
    """A dead gateway's sealed plugin, patch and orphaned stages are all reclaimed."""
    from kiro_crew import sandbox as sandbox_module

    artifact_dir = tmp_path / "pi-gate"
    artifact_dir.mkdir()
    stale = [
        artifact_dir / "kirocrew_dsh_gate_999999.mjs",
        artifact_dir / "kirocrew_dsh_gate_999999.patch.yml",
        artifact_dir / "kirocrew_dsh_gate_999999_abc123.tmp",
        artifact_dir / "kirocrew_dsh_patch_999999_abc123.tmp",
    ]
    for path in stale:
        path.write_text("gate", encoding="utf-8")
    monkeypatch.setattr(sandbox_module.platform_compat, "pid_exists", lambda _pid: False)
    removed = sandbox_module.cleanup_stale_sandbox_profiles(
        data_home=tmp_path, legacy_dir=str(tmp_path / "absent")
    )
    assert removed == len(stale)
    assert not any(path.exists() for path in stale)


def test_a_live_gateways_dsh_gate_artifacts_survive_the_sweep(monkeypatch, tmp_path) -> None:
    """The artifacts are written once and REUSED by every later spawn of the gateway.

    Their age therefore says nothing, and the sweep must decide on the owner's
    liveness alone -- an age rule here would delete the sealed plugin from under a
    running gateway's next deepseek spawn.
    """
    from kiro_crew import sandbox as sandbox_module

    artifact_dir = tmp_path / "pi-gate"
    artifact_dir.mkdir()
    live = [
        artifact_dir / "kirocrew_dsh_gate_424242.mjs",
        artifact_dir / "kirocrew_dsh_gate_424242.patch.yml",
    ]
    for path in live:
        path.write_text("gate", encoding="utf-8")
        # Well past the launcher age threshold: age must not be what decides.
        ancient = 1_000_000_000
        os.utime(path, (ancient, ancient))
    monkeypatch.setattr(sandbox_module.platform_compat, "pid_exists", lambda _pid: True)
    removed = sandbox_module.cleanup_stale_sandbox_profiles(
        data_home=tmp_path, legacy_dir=str(tmp_path / "absent")
    )
    assert removed == 0
    assert all(path.exists() for path in live)


def test_a_completed_call_the_gate_never_asked_about_stops_the_session() -> None:
    """The in-band guard that speaks for the SESSION's own child, not for a probe.

    The read-back proves the gate loaded in the child it booted. That the harness
    then asks for every call is its own behaviour, which no client-side read pins
    across upgrades -- and this harness's marker is written by the gate itself, so
    the read-back attests that Crew's code ran rather than that the harness reports
    it running. This is the check that does not depend on either: a
    ``tool_call_update`` reaching ``completed`` for an id no permission frame ever
    named is a call that ran with none of Crew's controls consulted, so the harness
    is stopped and the turn fails rather than continuing ungoverned.

    Shared with pi deliberately -- one tripwire, one state, so a fix to either
    lands in both.
    """
    import asyncio

    from kiro_crew.acp.client import AcpToolGateUnroutable
    from kiro_crew.acp.types import JsonRpcMessage

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_DEEPSEEK
    client._deepseek_gate_nonce = "n0nce"
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}
    client._session_id = "s1"
    client._session_key = "k1"
    client._kill_process = AsyncMock(return_value=None)

    completed = JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "never-asked",
                "status": "completed",
            },
        },
    )
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_pi_gate(completed))
    assert "never-asked" in str(refused.value)
    assert client._kill_process.await_count == 1

    # The same update for a call the gate DID ask about is not a bypass.
    client._kill_process = AsyncMock(return_value=None)
    client._pi_gate_asked_ids.add("asked")
    ok = JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "asked",
                "status": "completed",
            },
        },
    )
    asyncio.run(client._tripwire_pi_gate(ok))
    assert client._kill_process.await_count == 0


def test_the_permission_frame_records_the_call_the_tripwire_will_look_for() -> None:
    """The frame names the call directly here, unlike pi's dialog envelope.

    The tripwire only trusts a completed call the gate asked about first, so the
    recording side has to read this harness's own shape: an ordinary ACP permission
    frame whose ``toolCall`` carries just a ``toolCallId``. Reading it through pi's
    envelope would record nothing and every completed call would trip.
    """
    from kiro_crew.acp.types import JsonRpcMessage

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_DEEPSEEK
    client._deepseek_gate_nonce = "n0nce"
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}

    client._note_pi_gate_asked(
        JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={"sessionId": "s1", "toolCall": {"toolCallId": "call-1"}},
        )
    )
    assert client._pi_gate_asked_ids == {"call-1"}
    assert client._pi_gate_request_tool["7"] == "call-1"


def _deepseek_tripwire_client(monkeypatch):
    """A gated DeepSeek client whose kill is observable, shaped as the pi tripwire tests do."""
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_DEEPSEEK
    client._deepseek_gate_nonce = "n0nce"
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}
    client._permission_options = {}
    client._session_id = "s1"
    client._session_key = "k1"
    killed: list = []

    async def _kill(*, force=False):
        killed.append(force)

    async def _send(request_id, payload):
        pass

    monkeypatch.setattr(client, "_kill_process", _kill)
    monkeypatch.setattr(client, "_send_response", _send)
    return client, killed


def _deepseek_permission_frame(tool_call_id: str, request_id: int):
    """This harness's own permission frame: the call is named directly, with no envelope."""
    from kiro_crew.acp.types import JsonRpcMessage

    return JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={"sessionId": "s1", "toolCall": {"toolCallId": tool_call_id}},
    )


def _deepseek_completed(tool_call_id: str):
    from kiro_crew.acp.types import JsonRpcMessage

    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": "completed",
            },
        },
    )


def test_a_re_asked_call_is_judged_on_its_fresh_verdict_not_the_stale_deny(monkeypatch) -> None:
    """A denial must not outlive the id it was recorded against, and here ids recur.

    This harness forwards the PROVIDER's tool-call id, and an OpenAI-compatible
    provider that mints per-response ids repeats them across the several steps it runs
    in one turn -- so the same id can be denied once and legitimately asked about again.
    A denial that survived the second ask would kill a healthy session on the approved
    call's own ``completed`` frame, which is why a fresh ask clears the deny.
    """
    import asyncio

    from kiro_crew.acp.client import AcpToolGateUnroutable

    client, killed = _deepseek_tripwire_client(monkeypatch)

    first = _deepseek_permission_frame("call_0", 1)
    client._note_pi_gate_asked(first)
    asyncio.run(client.reject_tool(first.id))
    assert "call_0" in client._pi_gate_denied_ids

    second = _deepseek_permission_frame("call_0", 2)
    client._note_pi_gate_asked(second)
    assert "call_0" not in client._pi_gate_denied_ids, "a fresh ask is a fresh verdict"
    asyncio.run(client.approve_tool(second.id))

    asyncio.run(client._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert killed == []

    # And the third link is untouched: with no fresh frame, the denial still holds.
    other, other_killed = _deepseek_tripwire_client(monkeypatch)
    denied = _deepseek_permission_frame("call_1", 1)
    other._note_pi_gate_asked(denied)
    asyncio.run(other.reject_tool(denied.id))
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(other._tripwire_pi_gate(_deepseek_completed("call_1")))
    assert "DENIED" in str(refused.value)
    assert other_killed == [True]


def test_a_completed_call_consumes_its_ask_so_a_reused_id_must_be_asked_again(monkeypatch) -> None:
    """An approval must not outlive the call it was given for, because ids recur here.

    The mirror of the stale-deny case above: this harness forwards the PROVIDER's
    tool-call id, and a provider that mints per-response ids repeats ``call_0`` across
    the steps of one turn. An ask that survived its call's ``completed`` frame would
    vouch for the NEXT ``call_0`` -- one the gate was never asked about -- and the
    tripwire would trust it. So the terminal frame consumes the id's state, and a
    reused id is an unasked call until a fresh permission frame names it.
    """
    import asyncio

    from kiro_crew.acp.client import AcpToolGateUnroutable

    client, killed = _deepseek_tripwire_client(monkeypatch)
    asked = _deepseek_permission_frame("call_0", 1)
    client._note_pi_gate_asked(asked)
    asyncio.run(client.approve_tool(asked.id))
    asyncio.run(client._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert killed == [], "the asked-and-approved call completes without a kill"
    assert "call_0" not in client._pi_gate_asked_ids, "the terminal frame consumed the ask"

    # The same id again, with NO fresh permission frame: an unasked call.
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert "without asking" in str(refused.value)
    assert killed == [True]

    # And a fresh ask for the reused id earns it a fresh, trusted verdict.
    fresh, fresh_killed = _deepseek_tripwire_client(monkeypatch)
    for request_id in (1, 2):
        frame = _deepseek_permission_frame("call_0", request_id)
        fresh._note_pi_gate_asked(frame)
        asyncio.run(fresh.approve_tool(frame.id))
        asyncio.run(fresh._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert fresh_killed == []


def test_a_failed_terminal_consumes_the_gate_state_too(monkeypatch) -> None:
    """``failed`` is the other terminal, and it must not leave a verdict behind either.

    A denied call is EXPECTED to fail, and that failure ends the call. Were the deny
    to survive it, a later reuse of the id that the gate approved afresh would still
    be judged on the old deny -- the stale-state kill the fresh-ask discard exists to
    prevent -- and were an ask to survive its call's failure, the reuse would inherit
    an approval it never earned.
    """
    import asyncio

    from kiro_crew.acp.client import AcpToolGateUnroutable
    from kiro_crew.acp.types import JsonRpcMessage

    def failed(tool_call_id: str) -> JsonRpcMessage:
        frame = _deepseek_completed(tool_call_id)
        frame.params["update"]["status"] = "failed"
        return frame

    client, killed = _deepseek_tripwire_client(monkeypatch)
    denied = _deepseek_permission_frame("call_0", 1)
    client._note_pi_gate_asked(denied)
    asyncio.run(client.reject_tool(denied.id))
    asyncio.run(client._tripwire_pi_gate(failed("call_0")))
    assert killed == [], "a denied call failing is the expected outcome"
    assert "call_0" not in client._pi_gate_denied_ids
    assert "call_0" not in client._pi_gate_asked_ids

    # The id, reused with no fresh ask, is now judged as UNASKED rather than as denied.
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_pi_gate(_deepseek_completed("call_0")))
    assert "without asking" in str(refused.value)
    assert killed == [True]


def test_registering_an_unverified_harness_as_selectable_is_refused(monkeypatch) -> None:
    """KNOWN must not be enough to reach the switch, and this is where that is enforced.

    The guard outlives its original subject. deepseek was the harness that needed
    it -- KNOWN, so a governance rule could deny it, and unrouted, so it must not
    reach the switch -- and now that its routing is VERIFIED there is no KNOWN id
    left that is UNVERIFIED (``test_no_known_backend_is_unverified``). So the
    refusal is exercised by making this same id unrouted again, which keeps the
    guard covered for the next harness onboarded in that state rather than
    deleting it with the condition that motivated it.
    """
    from kiro_crew.agent_sdk import backends as sdk_backends

    monkeypatch.setitem(sdk_backends.ACP_BACKEND_ROUTING, ACP_BACKEND_DEEPSEEK, Routing.UNVERIFIED)
    baseline_before = set(sdk_backends._baseline)
    selectable_before = set(sdk_backends._selectable)
    try:
        with pytest.raises(ValueError) as raised:
            sdk_backends.register_selectable_backend(ACP_BACKEND_DEEPSEEK)
        message = str(raised.value)
        assert "unverified" in message
        # The message names the remedy -- declare routing -- rather than a flag, because
        # there is no flag: an escape hatch here would be a documented way to put an
        # ungated harness on the switch.
        assert "ACP_BACKEND_ROUTING" in message
        assert "allow_unrouted" not in message
        # And it must not half-register: a refusal that mutated either set would leave
        # the harness selectable anyway.
        assert set(sdk_backends._baseline) == baseline_before
        assert set(sdk_backends._selectable) == selectable_before
    finally:
        sdk_backends._baseline.clear()
        sdk_backends._baseline.update(baseline_before)
        sdk_backends._selectable.clear()
        sdk_backends._selectable.update(selectable_before)


def test_the_refusal_has_no_escape_hatch() -> None:
    """No parameter may turn the refusal off, and that is the point of it.

    A keyword flag would be a documented path to an ungated selectable harness, and no
    shipped caller wants one: every known backend but one is routed, and that one is
    deliberately absent from the selectable baseline. An edition that genuinely needs
    otherwise arrives with its own caller and its own justification.
    """
    import inspect

    from kiro_crew.agent_sdk import backends as sdk_backends

    signature = inspect.signature(sdk_backends.register_selectable_backend)
    assert list(signature.parameters) == ["backend"], (
        "register_selectable_backend takes the backend id and nothing else; a second "
        f"parameter would be a way to bypass the routing refusal: {signature}"
    )


def test_a_routed_harness_still_registers() -> None:
    """The other arm: the refusal keys on the ROUTING, not on this harness's id.

    Without this a guard that simply named deepseek would pass, and the next
    ``UNVERIFIED`` harness would walk straight onto the switch. It is also what keeps the
    unconditional refusal from being a blanket one -- a routed harness still registers.
    """
    from kiro_crew.agent_sdk import backends as sdk_backends

    baseline_before = set(sdk_backends._baseline)
    selectable_before = set(sdk_backends._selectable)
    try:
        sdk_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)
        assert ACP_BACKEND_CLAUDE in sdk_backends.selectable_backends()
    finally:
        sdk_backends._baseline.clear()
        sdk_backends._baseline.update(baseline_before)
        sdk_backends._selectable.clear()
        sdk_backends._selectable.update(selectable_before)


def test_no_known_backend_is_unverified() -> None:
    """The audit the refusal rests on, kept as a test so it cannot go quietly stale.

    deepseek was the one member of this set; it left when Crew composed its gate
    plugin and read the load marker back (``Routing.VERIFIED_GATE_EXTENSION``). One
    KNOWN id resolves ``UNVERIFIED`` now, and it is the DELIBERATE one: Custom ACP is
    the explicitly experimental harness, selectable but with its tool routing
    unverified, resting on the OS credential mask alone. It is named here so that if
    ANY OTHER harness ever resolves to ``UNVERIFIED`` -- including by being absent
    from the routing table, which ``routing_for`` answers ``UNVERIFIED`` for -- the
    refusal above starts applying to it and this test notices, rather than the gap
    being discovered when an edition's registration begins failing.
    """
    unverified = {b for b in ACP_BACKENDS_KNOWN if routing_for(b) is Routing.UNVERIFIED}
    assert unverified == {ACP_BACKEND_CUSTOM}
    # Every known id is named EXPLICITLY, so none of them is unverified merely by
    # omission.
    from kiro_crew.acp_backends import ACP_BACKEND_ROUTING

    assert set(ACP_BACKEND_ROUTING) >= ACP_BACKENDS_KNOWN
    # And the shipped baseline now carries this harness, which is the point of the
    # change: every KNOWN harness is selectable because every one of them is routed.
    assert ACP_BACKEND_DEEPSEEK in set(BASELINE_SELECTABLE_BACKENDS)


def test_it_carves_nothing_out_of_the_deny_list_because_its_key_comes_from_the_vault() -> None:
    """An ENFORCED harness that is fed its key names NO leaf, and that is the point.

    ``adapter_hidden_credential_dirs`` denies the whole read-gate floor minus the
    harness's own leaf, so every other enforced harness spares one or it cannot
    authenticate -- and that carve-out applies to the whole sandboxed process TREE,
    which on a harness shipping ``bash`` means the model can ``open()`` the
    operator's provider key. This harness resolves a key from its INHERITED
    ENVIRONMENT above both files, so Crew hands it one from its own vault
    (``agent.deepseek_env``) and both leaves stay masked. Asserted against the
    resolved mask, not just the declaration: a projection that stopped honouring an
    empty ``adapter_own_leaves`` would reopen them.
    """
    from kiro_crew.agent_sdk import host_auth
    from kiro_crew.agent_sdk import tool_gate as gate

    declaration = host_auth.declaration_for(ACP_BACKEND_DEEPSEEK)
    assert declaration.adapter_own_leaves == ()
    assert declaration.entitlement_source == host_auth.ENTITLEMENT_HOST_VAULT
    # ABSENT from the projection rather than present-and-empty, so the ``.get``
    # readers stay unchanged.
    assert ACP_BACKEND_DEEPSEEK not in gate.ADAPTER_OWN_CREDENTIAL_LEAVES
    # The mask is on, which is what makes the absence of a carve-out mean anything.
    hidden = gate.adapter_hidden_credential_dirs(ACP_BACKEND_DEEPSEEK)
    assert hidden != ()

    # Compared by PATH SEGMENTS, not by substring. The declarations are canonical
    # POSIX-spelled leaves, but the targets come back joined with the OS separator,
    # so a ``str.endswith(".pi/agent/auth.json")`` holds on POSIX and is False on
    # Windows for the same correct target -- which reads as a missing entry rather
    # than as a test that cannot see one.
    def _has_leaf(targets: tuple[str, ...], leaf: str) -> bool:
        want = tuple(PurePath(leaf).parts)
        return any(tuple(PurePath(t).parts)[-len(want) :] == want for t in targets)

    # This harness's own store is DENIED to its own child ...
    assert _has_leaf(hidden, ".dsh/.credentials.yaml")
    assert _has_leaf(hidden, ".dsh/.env")
    # ... on the same footing as every other harness's store, so the floor this mask
    # is derived from has no hole in it at all for this harness.
    assert _has_leaf(hidden, ".pi/agent/auth.json")
    # And nothing is re-exposed read-only inside the masked directories either: a
    # re-exposure would hand the same file back through the other door.
    assert gate.adapter_expose_files(ACP_BACKEND_DEEPSEEK, hidden) == ()


def test_the_credential_leaves_are_declared_with_their_override_spelling() -> None:
    """The floor re-anchors by the spelling declared here, so it is stated.

    ``DSH_HOME`` stands in for the leaves' own parent, so each keeps its final
    segment. That happens to be what an empty tuple would anchor; spelling it out
    is what lets a reader check the right file is fenced without re-deriving which
    prefix the variable replaces.
    """
    from kiro_crew.agent_sdk import host_auth

    declaration = host_auth.declaration_for(ACP_BACKEND_DEEPSEEK)
    assert declaration.credential_leaves == (".dsh/.credentials.yaml", ".dsh/.env")
    assert declaration.home_override_env_vars == ("DSH_HOME",)
    assert declaration.override_relative_leaves == (".credentials.yaml", ".env")
    assert declaration.host_logout_retires_children is False


# ── The restore verb ─────────────────────────────────────────────────────────


def _client(tmp_path: pathlib.Path, backend: str) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    return client


def _arm_restore(client: AcpClient, capabilities: dict, restore_result: dict) -> list[str]:
    """Drive one handshake with a resume id pending; return the methods sent."""
    sent: list[str] = []
    responses = [{"protocolVersion": 1, "agentCapabilities": capabilities}, restore_result]

    async def fake_send(method: str, params: dict) -> int:
        sent.append(method)
        return len(sent)

    async def fake_wait(req_id: int, timeout: float = 50.0, *, method="", expected_mcp=None):
        # A handshake that does not restore falls through to session/new, so every
        # id past the scripted ones answers as a fresh session rather than as {}.
        if req_id <= len(responses):
            scripted = responses[req_id - 1]
            if sent and sent[req_id - 1] == "session/new":
                return {"sessionId": "fresh"}
            return scripted
        return {"sessionId": "fresh"}

    client._send_request = AsyncMock(side_effect=fake_send)
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    client._resume_session_id = "prior-session"
    return sent


@pytest.mark.asyncio
async def test_a_member_is_sent_session_resume(tmp_path: pathlib.Path) -> None:
    """The verb follows the membership set, and the restore is adopted."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    sent = _arm_restore(
        client,
        {"sessionCapabilities": {"resume": {}, "list": {}, "close": {}}},
        {"configOptions": []},
    )

    await client._initialize_session()

    assert "session/resume" in sent
    assert "session/load" not in sent
    assert client._session_id == "prior-session"
    assert client._resumed is True


@pytest.mark.asyncio
async def test_a_non_member_is_still_sent_session_load(tmp_path: pathlib.Path) -> None:
    """The other arm, so the set is what decides rather than the new code path.

    Without this the verb swap could be unconditional and every kiro-family resume
    would be sent a method kiro-cli does not serve.
    """
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    sent = _arm_restore(client, {"loadSession": True}, {"modes": ["chat"]})

    await client._initialize_session()

    assert "session/load" in sent
    assert "session/resume" not in sent


@pytest.mark.asyncio
async def test_the_capability_is_read_where_this_harness_advertises_it(
    tmp_path: pathlib.Path,
) -> None:
    """A harness advertising no ``resume`` must not be sent the verb at all.

    ``loadSession`` is absent from this harness's ``initialize`` result, so reading
    that flag would answer False and silently start every reopened session fresh.
    Reading the wrong key is the failure this pins.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    sent = _arm_restore(client, {"sessionCapabilities": {"list": {}}}, {"configOptions": []})

    await client._initialize_session()

    assert "session/resume" not in sent
    assert client._resumed is False


@pytest.mark.asyncio
async def test_a_restore_with_no_modes_block_is_adopted(tmp_path: pathlib.Path) -> None:
    """This harness can never return ``modes``; gating on one would discard the restore."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    _arm_restore(client, {"sessionCapabilities": {"resume": {}}}, {"configOptions": []})

    await client._initialize_session()

    assert client._resumed is True


def test_both_varying_reads_are_keyed_on_the_one_set() -> None:
    """The capability AND the verb, from the same membership, in the shared path.

    Two sets would invite an entry in one and not the other, which reads as "cannot
    restore" and starts every reopened session fresh. A source scan because the
    hazard is a second decision site appearing, which no behavioural test sees.
    """
    body = inspect.getsource(AcpClient._initialize_session)
    assert body.count("ACP_BACKENDS_RESUME_WITHOUT_LOAD") == 2
    assert 'session_capabilities.get("resume")' in body
    assert "METHOD_SESSION_RESUME" in body
    assert "restore_method" in body


def test_the_restore_params_are_built_once_for_either_verb() -> None:
    """One params dict, because the two calls share a contract.

    ``ResumeSessionRequest`` carries the same fields as ``LoadSessionRequest``, so a
    second dict would be a copy that can drift rather than an abstraction.
    """
    body = inspect.getsource(AcpClient._initialize_session)
    assert body.count("load_params: dict = {") == 1


def test_the_restore_method_is_resolved_before_the_try() -> None:
    """The failure log names the verb, so the name must be bound on every path."""
    body = inspect.getsource(AcpClient._initialize_session)
    resolved = body.index("restore_method = (")
    guarded = body.index("try:", resolved - 400)
    assert resolved < guarded, "restore_method must be bound before the guarded block"


# ── Spawn and handshake ──────────────────────────────────────────────────────


def test_a_stalled_restore_keeps_its_mcp_detail() -> None:
    """The timeout message enricher has to know BOTH restore verbs.

    A stalled restore is one of the few failures an operator reads verbatim, and the
    MCP progress detail the caller supplies is what makes it actionable. The enricher
    gates on a set of methods, so a harness sent the other verb loses that detail
    while the message still claims to describe the restore.
    """
    body = inspect.getsource(AcpClient._wait_for_response)
    assert (
        "{METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SESSION_RESUME}" in body
    ), "the enricher must name every restore verb this client can send"


def test_the_handshake_is_the_spec_dialect() -> None:
    """Integer ``protocolVersion`` 1, captured off its own wire."""
    from kiro_crew.acp.client import _PROTOCOL_VERSION_BY_BACKEND

    assert _PROTOCOL_VERSION_BY_BACKEND[ACP_BACKEND_DEEPSEEK] == 1


def test_the_argv_is_the_host_binary_plus_the_shipped_profile() -> None:
    """The ACP package is a plugin with no executable; the host binary boots it."""
    from kiro_crew.agent_sdk.backends import launch_for

    # The RECORD is what is pinned, not a line of source: the spawn arm reads
    # ``_resolve_self_served_launch``, which is shared with the sibling harnesses, so
    # a source-text assertion there would pin their spelling as well as this one's.
    record = launch_for(ACP_BACKEND_DEEPSEEK)
    assert record.binary == "dsh"
    assert record.acp_args == ("--profile", "acp")
    assert record.spawn_label == "dsh --profile acp"
    # The installer names the HOST binary. The ACP package is a plugin with no
    # executable of its own, so advice naming it would not produce a runnable
    # harness -- which is the whole reason this fact is data rather than prose.
    assert record.install_command == "npm i -g @deepseek-ai/dsh"
    assert "dsh" in record.missing_hint or "plugin" in record.missing_hint


def test_the_resolution_ladder_prefers_the_explicit_override(monkeypatch, tmp_path) -> None:
    """Override, then mise, then PATH -- the plain-binary ladder.

    What is pinned is the ORDER, so executability is STUBBED rather than staged on
    disk. A file written and chmod-ed here answers ``is_executable_file`` on POSIX
    and not on Windows, where an executable needs a recognised extension -- so a
    disk-staged override fell through to the mise rung there, and this test was
    asserting the platform's notion of an executable instead of the precedence it
    exists to pin.
    """
    from kiro_crew.acp import client as client_module

    binary = tmp_path / "dsh"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("DSH_BIN", str(binary))
    monkeypatch.setattr(
        client_module.platform_compat,
        "is_executable_file",
        lambda candidate: str(candidate) == str(binary),
    )
    monkeypatch.setattr(client_module, "_mise_which", lambda _name: "/never/reached")

    resolved, _searched = client_module._resolve_self_served_bin(ACP_BACKEND_DEEPSEEK)
    assert resolved == str(binary)


def test_an_absent_binary_reports_what_was_searched(monkeypatch) -> None:
    """A caller must be able to say where it looked rather than raising from inside."""
    from kiro_crew.acp import client as client_module

    monkeypatch.delenv("DSH_BIN", raising=False)
    monkeypatch.setattr(client_module, "_mise_which", lambda _name: None)
    monkeypatch.setattr(client_module.shutil, "which", lambda *_a, **_kw: None)

    resolved, searched = client_module._resolve_self_served_bin(ACP_BACKEND_DEEPSEEK)
    assert resolved is None
    assert searched


def test_the_sandbox_posture_is_pinned_in_the_child_environment() -> None:
    """Defence in depth, and not a routing claim.

    One variable selects both a sandbox mode and an approval policy on this
    harness, and its permissive end runs sensitive actions without asking anyone.
    Pinning the confined value keeps an inherited shell variable from selecting
    that end; it does not make a tool call reach Crew's gate.
    """
    from kiro_crew.acp.client import _ENV_DEEPSEEK_PERMISSION_MODE, DEEPSEEK_PERMISSION_MODE

    assert _ENV_DEEPSEEK_PERMISSION_MODE == "DSH_PERMISSION_MODE"
    assert DEEPSEEK_PERMISSION_MODE == "workspace-write"

    body = inspect.getsource(AcpClient._spawn)
    assert "env[_ENV_DEEPSEEK_PERMISSION_MODE] = DEEPSEEK_PERMISSION_MODE" in body


def test_the_arm_runs_the_credential_mask_preflight_before_its_read_back() -> None:
    """An ENFORCED harness takes a preflight call site, and takes it FIRST.

    ``test_acp_tool_gate.test_every_enforced_harness_reaches_the_spawn_preflight``
    counts one ``_sandbox_preflight`` call per ENFORCED harness, so the call is
    required rather than optional now. ORDER is the part worth pinning here: the
    read-back below boots a child of this harness, and a child that ran before the
    mask was resolved would read the operator's credentials with the gate's own
    blessing.
    """
    body = inspect.getsource(AcpClient._spawn)
    arm = body[body.index("elif self._is_deepseek:") :]
    arm = arm[: arm.index("\n        else:")]
    assert "_sandbox_preflight" in arm
    assert "adapter_expose_files" in arm
    assert arm.index("_sandbox_preflight") < arm.index("_verify_deepseek_gate")
    # And the gate is composed through the harness's own per-launch patch flag,
    # never by editing a profile Crew does not own.
    assert "_DSH_PATCH_FLAG" in arm
    assert "_seal_deepseek_gate_extension" in arm


def test_a_deepseek_readback_issue_becomes_an_acp_routing_refusal() -> None:
    """The DeepSeek-only read-back converts every issue through the enforced gate."""
    body = inspect.getsource(AcpClient._spawn)
    readback = body[body.index("routing_issue, routing_remedy =") :]
    readback = readback[: readback.index("# Resolve the SSH_AUTH_SOCK")]
    assert "if routing_issue:" in readback
    assert "acp_tool_gate.enforce_runtime_routing" in readback
    assert "raise AcpToolGateUnroutable(str(exc)) from None" in readback


# ── The effort option id, read rather than spelled ───────────────────────────


def test_the_effort_levels_are_parsed_under_this_harness_own_option_id(tmp_path) -> None:
    """A hard-coded ``effort`` here reads as "no levels offered" rather than a miss."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    client._acp_config_options = [
        {
            "id": "reasoning_effort",
            "options": [{"value": "off"}, {"value": "low"}, {"value": "high"}],
        }
    ]

    assert client.get_valid_effort_levels() == ["off", "low", "high"]


def test_another_harness_still_parses_the_default_option_id(tmp_path) -> None:
    """The other arm of the table, so the read is keyed and not simply renamed."""
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    client._acp_config_options = [{"id": "effort", "options": [{"value": "high"}]}]

    assert client.get_valid_effort_levels() == ["high"]


def test_the_levels_parser_reads_the_table(tmp_path) -> None:
    """The id comes from ``effort_config_option_id``, not from a literal at this site."""
    body = inspect.getsource(AcpClient.get_valid_effort_levels)
    assert "effort_config_option_id(self.backend)" in body
    assert '== "effort"' not in body


#: How many sites in ``AcpProvider`` resolve the effort option id per backend, and
#: what each one is. An exact count rather than a floor: a literal at any of them
#: makes the whole channel a silent no-op for a harness that spells the option
#: differently, and a count that only grows cannot tell a new reader from a
#: hard-coded one that slipped in beside a correct one.
_PROVIDER_EFFORT_ID_SITES = (
    "the advertised-option CHECK in ``_set_effort_config_option``",
    "the skip-if-unadvertised check in ``change_effort``",
    "the capability answer in ``supports_effort``, for a harness whose ADVERTISED "
    "option decides that a level applies at all "
    "(``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION``)",
)


def test_no_site_that_asks_about_effort_spells_the_id_itself() -> None:
    """Membership in the effort set is worth nothing if a consumer hard-codes the id.

    The levels parser is one site; ``_PROVIDER_EFFORT_ID_SITES`` names the ones in
    ``providers/acp.py``. A literal at any of them makes the whole channel a silent
    no-op for this harness: the check reports the option unsupported, the push never
    happens, and the dropdown still offers levels that can never be applied.
    """
    from kiro_crew.providers import acp as provider_module

    body = inspect.getsource(provider_module.AcpProvider)
    assert 'supports_config_option("effort")' not in body
    assert 'set_config_option("effort"' not in body
    assert body.count("effort_config_option_id(self._client.backend)") == len(
        _PROVIDER_EFFORT_ID_SITES
    )


@pytest.mark.asyncio
async def test_the_effort_push_uses_this_harness_own_option_id(tmp_path) -> None:
    """The push reaches the wire under the id the harness advertised."""
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider.__new__(AcpProvider)
    provider._client = MagicMock()
    provider._client.backend = ACP_BACKEND_DEEPSEEK
    provider._client.supports_config_option = MagicMock(return_value=True)
    provider._client.set_config_option = AsyncMock()
    provider._client._model = "deepseek-v4-flash"

    await provider._set_effort_config_option("high")

    asked = provider._client.supports_config_option.call_args[0][0]
    pushed = provider._client.set_config_option.await_args[0][0]
    assert asked == "reasoning_effort"
    assert pushed == "reasoning_effort"


def test_a_grouped_model_select_is_flattened_before_the_value_filter(tmp_path) -> None:
    """This harness groups its model options, and the capture is the only vocabulary.

    Its ``session/new`` nests the real choices under a ``{"group": …, "options": […]}``
    wrapper that carries no ``value`` of its own. A filter keeping only entries with a
    ``value`` empties on that shape, and an empty list reads as "advertised no
    models" -- which for a harness whose ids exist nowhere else means no model can be
    offered or resolved at all.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    grouped = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": '["deepseek-official","deepseek-v4-flash"]',
                "options": [
                    {
                        "group": "deepseek-official",
                        "name": "DeepSeek",
                        "options": [
                            {
                                "value": '["deepseek-official","deepseek-v4-flash"]',
                                "name": "DeepSeek-V4-Flash",
                            }
                        ],
                    }
                ],
            }
        ]
    }

    envelope = models_from_config_options(grouped, client.backend)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == [
        '["deepseek-official","deepseek-v4-flash"]'
    ]
    assert envelope["currentModelId"] == '["deepseek-official","deepseek-v4-flash"]'


def test_a_flat_model_select_is_still_read_unchanged(tmp_path) -> None:
    """The other arm: a harness that does not group must be unaffected by the flatten."""
    client = _client(tmp_path, ACP_BACKEND_CLAUDE)
    flat = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "currentValue": "sonnet",
                "options": [{"value": "sonnet", "name": "Sonnet"}],
            }
        ]
    }

    envelope = models_from_config_options(flat, client.backend)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == ["sonnet"]


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(123, id="nested-options-is-a-number"),
        pytest.param(True, id="nested-options-is-a-bool"),
        pytest.param("abc", id="nested-options-is-a-string"),
        pytest.param({"a": {"value": "x"}}, id="nested-options-is-a-mapping"),
        pytest.param([None, 7, "x"], id="nested-options-holds-non-objects"),
    ],
)
def test_a_malformed_group_degrades_instead_of_failing_the_session(tmp_path, malformed) -> None:
    """Every level of this payload comes off the wire, so none of it may be trusted.

    A truthy non-iterable in a group's ``options`` raised ``TypeError`` from inside
    ``session/new`` handling, so a malformed or hostile agent response failed session
    initialization rather than degrading to "this harness advertised no models".
    Parametrized over the shapes a wire payload can actually take, because the outer
    list and the nested one need the SAME narrowing and only the outer one had it.
    """
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    payload = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "options": [{"group": "g", "name": "G", "options": malformed}],
            }
        ]
    }

    assert models_from_config_options(payload, client.backend) is None


def test_a_malformed_group_beside_a_good_one_keeps_the_good_one(tmp_path) -> None:
    """Degrading must not mean discarding: one bad group cannot cost the whole list."""
    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    payload = {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "options": [
                    {"group": "broken", "options": 123},
                    {"group": "good", "options": [{"value": "real-id", "name": "Real"}]},
                ],
            }
        ]
    }

    envelope = models_from_config_options(payload, client.backend)

    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == ["real-id"]


def test_the_committed_fixture_is_the_shape_the_capture_must_read(tmp_path) -> None:
    """Read the real captured frame, so the parser is pinned against the wire itself.

    A hand-written grouped payload could drift from what the harness sends; the
    fixture cannot, because it IS what the harness sent.
    """
    import json

    fixture = (
        pathlib.Path(__file__).parent
        / "fixtures"
        / "acp_frames"
        / "deepseek"
        / "handshake-live.jsonl"
    )
    session_new = next(
        frame["result"]
        for frame in (json.loads(line) for line in fixture.read_text().splitlines()[1:])
        if isinstance(frame.get("result"), dict) and "configOptions" in frame["result"]
    )

    client = _client(tmp_path, ACP_BACKEND_DEEPSEEK)
    envelope = models_from_config_options(session_new, client.backend)

    assert envelope is not None
    assert envelope["availableModels"], "the captured select must yield at least one model"


# ── The provider key: fed from Crew's vault, not carved out of the mask ───────


def test_the_override_anchored_credential_spellings_are_masked_too(tmp_path) -> None:
    """``DSH_HOME`` moves the real files, and the mask has to move with them.

    A mask anchored only under ``$HOME`` would deny two paths the harness never
    writes while leaving the relocated key readable -- which is the failure the
    declaration's ``override_relative_leaves`` exists to prevent, asserted here
    against the resolved mask rather than against the declaration.
    """
    from kiro_crew.agent_sdk import tool_gate

    home = tmp_path / "dsh-home"
    home.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DSH_HOME", str(home))
        hidden = tool_gate.adapter_hidden_credential_dirs(ACP_BACKEND_DEEPSEEK)

    resolved = os.path.realpath(str(home))
    assert os.path.join(resolved, ".credentials.yaml") in hidden
    assert os.path.join(resolved, ".env") in hidden


def test_the_remedy_names_the_vault_and_the_mapping_not_a_file() -> None:
    """The operator's action changed, so the standing advice had to change with it.

    Telling this operator to "save a key in the harness's configuration" would send
    them to a file the child mask now hides. Both strings name the two steps that do
    work: store the secret, then map it.
    """
    from kiro_crew.agent_sdk import host_auth

    declaration = host_auth.declaration_for(ACP_BACKEND_DEEPSEEK)
    for text in (declaration.sign_in_remedy, declaration.signed_out_message):
        assert "Secrets" in text
        assert "deepseek_env" in text
    # A locally served model needs no key at all, so the standing remedy still
    # states the action and asserts no state.
    assert "needs no key" in declaration.sign_in_remedy


def _vault_env_mapping(name: str = "DEEPSEEK_API_KEY", value: str = "secret://dsh-proof") -> dict:
    return {name: value}


@pytest.mark.parametrize(
    "mapping, fragment",
    [
        pytest.param(
            {"DEEPSEEK_API_KEY": "sk-live-not-a-reference"},
            "holds a literal value",
            id="plaintext",
        ),
        pytest.param(
            {"DEEPSEEK API KEY": "secret://dsh-proof"},
            "POSIX shell identifiers",
            id="not-an-identifier",
        ),
        pytest.param(
            {"DEEPSEEK_CREDENTIAL": "secret://dsh-proof"},
            "withholds from its own shell children",
            id="outside-the-child-scrub-class",
        ),
        pytest.param(
            {"DSH_API_KEY": "secret://dsh-proof"},
            "sets on this child itself",
            id="reserved-dsh-prefix",
        ),
        pytest.param(
            {"KIRO_API_KEY": "secret://dsh-proof"},
            "sets on this child itself",
            id="crew-owned-name",
        ),
        pytest.param(
            {"KIROCREW_SESSION_KEY": "secret://dsh-proof"},
            "session credentials",
            id="session-key-identity-credential",
        ),
        pytest.param(
            {"KIROCREW_STUB_SESSION_TOKEN": "secret://dsh-proof"},
            "session credentials",
            id="stub-token-identity-credential",
        ),
        pytest.param(
            {"KIROCREW_FUTURE_TOKEN": "secret://dsh-proof"},
            "KIROCREW_ namespaces",
            id="crew-namespace-prefix",
        ),
        pytest.param(
            {"AWS_SECRET_ACCESS_KEY": "secret://dsh-proof"},
            "agent environment scrub removes",
            id="crew-scrubbed-prefix",
        ),
    ],
)
def test_a_provider_key_mapping_the_harness_would_not_honour_is_refused(
    mapping: dict, fragment: str
) -> None:
    """Every refusal names the env-var KEY and nothing about the secret.

    Each of these fails SILENTLY if injected instead: a plaintext value puts a live
    key in config.json, a non-identifier is not a reference the harness resolves, a
    name outside the harness's child-scrub class is forwarded into the model's own
    shell, a reserved or Crew-owned name collides with a variable Crew writes here,
    and a name Crew's agent scrub strips is removed again on the shared tail. So each
    is a refusal, and the message has to be specific enough that the operator knows
    which rule they hit.
    """
    from kiro_crew.acp import client as client_module

    with pytest.raises(ValueError) as refused:
        client_module._validate_deepseek_env_mapping(mapping)

    message = str(refused.value)
    assert fragment in message, message
    key = next(iter(mapping))
    assert repr(key) in message, "the refusal must name the operator's own env-var key"
    # Never the vault name, and never the value: this message reaches a log and a
    # chat error card unsanitised.
    assert "dsh-proof" not in message
    assert "sk-live-not-a-reference" not in message


def test_every_scrub_class_name_crew_writes_on_the_child_is_reserved() -> None:
    """Derived, so the reserved set cannot lag the spawn pipeline.

    The provider-key injection lands on the child's ``env`` BEFORE the shared tail
    writes Crew's own variables onto it, so any name Crew writes there that is also
    inside the harness's scrub class is a name an operator mapping could collide
    with -- and the later write wins, handing the harness's provider whatever Crew
    put there. ``KIROCREW_SESSION_KEY`` and the signed stub token are exactly that
    shape: both are session credentials, both contain ``KEY``/``TOKEN``, and both
    are written by ``_apply_session_identity_env`` after the arm.

    So the validator's reserved names are checked against a DERIVATION here: every
    upper-case env-name literal in the functions that compose the child's
    environment, filtered to the scrub class, must be refused by the validator.
    A new Crew variable in that class fails this test until it is reserved.
    """
    import re

    from kiro_crew import agent_scratch, sandbox
    from kiro_crew.acp import client as client_module

    composers = (
        AcpClient._spawn,
        AcpClient._apply_session_identity_env,
        client_module._resolve_spawn_env,
        sandbox.scrub_agent_subprocess_env,
        agent_scratch.scratch_env,
        client_module.browser_session_env,
        client_module.browser_socket_env,
        client_module._apply_pod_home_remap,
        client_module.inject_xdist_auto_cap,
    )
    names: set[str] = set()
    for fn in composers:
        for literal in re.findall(r'"([A-Z][A-Z0-9_]{2,})"', inspect.getsource(fn)):
            if client_module._DEEPSEEK_ENV_CHILD_SCRUB_CLASS.search(literal):
                names.add(literal)
    # The constants the identity writer spells by name rather than by literal.
    names.add(client_module.STUB_SESSION_TOKEN_ENV)
    assert {"KIROCREW_SESSION_KEY", "KIROCREW_STUB_SESSION_TOKEN"} <= names, names

    for name in sorted(names):
        with pytest.raises(ValueError, match="sets on this child itself"):
            client_module._validate_deepseek_env_mapping({name: "secret://dsh-proof"})


def test_a_provider_key_mapping_the_harness_honours_is_accepted() -> None:
    """The happy shape passes every rule, so the refusals are not vacuous."""
    from kiro_crew.acp import client as client_module

    client_module._validate_deepseek_env_mapping(_vault_env_mapping())
    # Not DeepSeek-specific: a credential reference is any provider name the harness
    # knows, and the only constraint is the harness's own scrub class.
    client_module._validate_deepseek_env_mapping({"ANTHROPIC_API_KEY": "secret://other"})
    client_module._validate_deepseek_env_mapping({"OPENAI_API_TOKEN": "secret://other"})


def test_a_plaintext_value_never_reaches_disk_through_the_locked_write(tmp_path) -> None:
    """The publish floor: ``config set`` cannot persist a provider key in plaintext.

    Without this floor a plaintext value typed into ``config set agent.deepseek_env``
    lands in ``config.json`` and sits there until the next DeepSeek spawn refuses
    it -- a live credential persisted in an agent-readable file, which is the
    exposure the vault route exists to close. So the refusal sits at the moment of
    publication: ``write_config_atomically`` refuses the document, the locked
    read-modify-write propagates that, and the file on disk is exactly what it was.
    """
    from kiro_crew.config.loader import ConfigWriteRefused, update_config_locked

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"agent": {"yolo": True}}), encoding="utf-8")
    before = path.read_bytes()

    def plant_plaintext(existing: dict) -> dict:
        existing.setdefault("agent", {})["deepseek_env"] = {
            "DEEPSEEK_API_KEY": "sk-live-not-a-reference"
        }
        return existing

    with pytest.raises(ConfigWriteRefused) as refused:
        update_config_locked(path, mutate=plant_plaintext)

    message = str(refused.value)
    assert "'DEEPSEEK_API_KEY'" in message, "the refusal must name the operator's own env-var key"
    assert "holds a literal value" in message
    assert "sk-live-not-a-reference" not in message, "the refusal must never carry the value"
    assert path.read_bytes() == before, "a refused write must leave the file byte-identical"
    assert not list(tmp_path.glob("*.tmp*")), "no temp file may survive a refused publish"


def test_a_pre_existing_plaintext_value_does_not_refuse_an_unrelated_write(
    tmp_path, caplog
) -> None:
    """The floor refuses what a write INTRODUCES, not what an older build already landed.

    Every config writer lands on ``write_config_atomically`` -- the Slack allowlist,
    the dashboard PUT and PATCH, ``config set``, ``save()`` -- and most of them have
    no ``ConfigWriteRefused`` handler because they never touch this field. A
    ``config.json`` hand-edited (or written by a build before the floor) with a
    plaintext ``agent.deepseek_env`` value must not turn every one of those writes
    into an unhandled exception: a write that leaves the mapping exactly as it
    found it publishes, and the plaintext is named on the log so it is not silent.
    The spawn-time validator still refuses the DeepSeek session that would use it.
    """
    import logging

    from kiro_crew.config.loader import update_config_locked

    path = tmp_path / "config.json"
    stale = {"DEEPSEEK_API_KEY": "sk-live-not-a-reference"}
    path.write_text(json.dumps({"agent": {"deepseek_env": stale}}), encoding="utf-8")

    def unrelated(existing: dict) -> dict:
        existing.setdefault("agent", {})["yolo"] = True
        return existing

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        update_config_locked(path, mutate=unrelated)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["agent"]["yolo"] is True, "the unrelated write must land"
    assert written["agent"]["deepseek_env"] == stale, "the write leaves the mapping as it found it"
    warned = [r.getMessage() for r in caplog.records if "deepseek_env" in r.getMessage()]
    assert warned, "a plaintext left on disk must be named on the log"
    assert "'DEEPSEEK_API_KEY'" in warned[0]
    assert "sk-live-not-a-reference" not in warned[0], "the log must never carry the value"


def test_a_write_that_touches_the_mapping_while_keeping_plaintext_is_refused(tmp_path) -> None:
    """Keeping a plaintext entry while changing the mapping is the write's own doing."""
    from kiro_crew.config.loader import ConfigWriteRefused, update_config_locked

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"agent": {"deepseek_env": {"DEEPSEEK_API_KEY": "sk-live-not-a-reference"}}}),
        encoding="utf-8",
    )
    before = path.read_bytes()

    def add_a_reference_beside_it(existing: dict) -> dict:
        existing["agent"]["deepseek_env"]["ANTHROPIC_API_KEY"] = "secret://anthropic"
        return existing

    with pytest.raises(ConfigWriteRefused) as refused:
        update_config_locked(path, mutate=add_a_reference_beside_it)
    assert "'DEEPSEEK_API_KEY'" in str(refused.value)
    assert "sk-live-not-a-reference" not in str(refused.value)
    assert path.read_bytes() == before


def test_a_plaintext_with_no_readable_prior_document_is_refused(tmp_path) -> None:
    """Fail closed: a plaintext that cannot be shown pre-existing is treated as introduced."""
    from kiro_crew.config.loader import ConfigWriteRefused, write_config_atomically

    data = {"agent": {"deepseek_env": {"DEEPSEEK_API_KEY": "sk-live-not-a-reference"}}}
    # No file at all: the plaintext is being introduced.
    with pytest.raises(ConfigWriteRefused):
        write_config_atomically(tmp_path / "new.json", data)
    # An unreadable prior: nothing shows the value pre-existed, so it is refused too.
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigWriteRefused):
        write_config_atomically(corrupt, data)
    assert corrupt.read_text(encoding="utf-8") == "{ not json"


def test_a_reference_passes_the_publish_floor_and_the_whole_document_save_is_gated_too(
    tmp_path, monkeypatch
) -> None:
    """Both halves of the floor: a ``secret://`` value publishes; ``save()`` is gated.

    The locked RMW is not the only writer that lands a config document --
    :meth:`KiroCrewConfig.save` publishes in-memory state whole -- so the rule sits
    in the one function both reach. A reference is what the mapping is FOR, so it
    has to pass, or the floor would refuse the operator's correct configuration.
    """
    from kiro_crew.config.loader import ConfigWriteRefused, KiroCrewConfig, update_config_locked

    path = tmp_path / "config.json"

    def plant_reference(existing: dict) -> dict:
        existing.setdefault("agent", {})["deepseek_env"] = _vault_env_mapping()
        return existing

    update_config_locked(path, mutate=plant_reference)
    assert json.loads(path.read_text(encoding="utf-8"))["agent"]["deepseek_env"] == (
        _vault_env_mapping()
    )

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    cfg = KiroCrewConfig()
    cfg.agent.deepseek_env = {"ANTHROPIC_API_KEY": "sk-ant-not-a-reference"}
    with pytest.raises(ConfigWriteRefused) as refused:
        cfg.save()
    assert "'ANTHROPIC_API_KEY'" in str(refused.value)
    assert "sk-ant-not-a-reference" not in str(refused.value)


def test_the_cli_reports_the_refusal_by_key_and_writes_nothing(tmp_path, capsys) -> None:
    """``config set`` surfaces the floor as a refusal line, not a traceback."""
    import argparse
    from unittest.mock import patch

    from kiro_crew.cli_config import _config_cmd

    local = tmp_path / "config.local.json"
    args = argparse.Namespace(
        config_action="set",
        key="agent.deepseek_env",
        value=json.dumps({"DEEPSEEK_API_KEY": "sk-live-not-a-reference"}),
        file=None,
        local=True,
    )
    with patch("kiro_crew.cli_config.config_local_path", return_value=local):
        with patch("kiro_crew.cli_config.sel"):
            with pytest.raises(SystemExit) as exited:
                _config_cmd(args)

    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert "agent.deepseek_env" in err and "'DEEPSEEK_API_KEY'" in err
    assert "sk-live-not-a-reference" not in err
    assert not local.exists(), "a refused config set must not create the file"


def test_the_cli_file_import_reports_the_refusal_and_leaves_config_json_as_it_was(
    tmp_path, capsys
) -> None:
    """``config set --file`` is the third writer behind the floor; it refuses the same way.

    The whole-document import lands through the same locked write as the keyed
    paths, so a file carrying a plaintext ``agent.deepseek_env`` value reaches
    ``_refuse_unpublishable`` too. Without a handler on this branch the operator
    gets a ``ConfigWriteRefused`` traceback instead of the one-line refusal the
    keyed paths print -- and a traceback is where the file path and the exception
    class leak while the actual instruction (map it as ``secret://``) is buried.
    Nothing was written either way; this pins that the report is controlled.
    """
    import argparse
    from unittest.mock import patch

    from kiro_crew.cli_config import _config_cmd

    config_dir = tmp_path / "kiro"
    config_dir.mkdir()
    target = config_dir / "config.json"
    target.write_text(json.dumps({"agent": {"yolo": True}}), encoding="utf-8")
    before = target.read_bytes()

    imported = tmp_path / "import.json"
    imported.write_text(
        json.dumps({"agent": {"deepseek_env": {"DEEPSEEK_API_KEY": "sk-live-not-a-reference"}}}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        config_action="set", key=None, value=None, file=str(imported), local=False
    )
    with (
        patch("kiro_crew.cli_config.config_path", return_value=target),
        patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
        patch("kiro_crew.cli_config.sel"),
    ):
        with pytest.raises(SystemExit) as exited:
            _config_cmd(args)

    assert exited.value.code == 1, "a refused import exits 1 through the CLI's own refusal path"
    captured = capsys.readouterr()
    assert "'DEEPSEEK_API_KEY'" in captured.err, "the refusal names the operator's env-var key"
    assert "holds a literal value" in captured.err
    assert "sk-live-not-a-reference" not in captured.err, "the value never reaches the terminal"
    assert "Traceback" not in captured.err
    assert "Config loaded" not in captured.out, "the success line must not print on a refusal"
    assert target.read_bytes() == before, "a refused import leaves config.json byte-identical"
    assert not list(config_dir.glob("*.tmp*")), "no temp file may survive a refused import"


def test_the_publish_floor_predicate_names_keys_only_and_leaves_shape_to_the_coercer() -> None:
    """The predicate is what the floor reads, so its contract is pinned directly.

    Names only and sorted, because the result is destined for a terminal and a log;
    a non-dict shape is not this function's problem -- ``coerce_deepseek_env`` drops
    it -- so it returns empty rather than inventing an entry to refuse.
    """
    from kiro_crew.config.sections import deepseek_env_plaintext_keys

    assert deepseek_env_plaintext_keys(None) == ()
    assert deepseek_env_plaintext_keys("secret://not-a-mapping") == ()
    assert deepseek_env_plaintext_keys({}) == ()
    assert deepseek_env_plaintext_keys(_vault_env_mapping()) == ()
    assert deepseek_env_plaintext_keys(
        {"Z_KEY": "plain", "A_KEY": "secret://ok", "M_KEY": 7, "K_KEY": "secret:/one-slash"}
    ) == ("K_KEY", "M_KEY", "Z_KEY")


def test_the_child_scrub_class_is_read_from_the_harness_not_guessed() -> None:
    """The class is the harness's own ``SENSITIVE_ENV_PATTERN``, pinned as a constant.

    ``@deepseek-ai/dsh-subprocess`` drops every inherited variable matching
    ``/KEY|PASSWORD|SECRET|TOKEN/i`` before spawning any child, on both of its spawn
    paths. That pattern is the ENTIRE reason an env-fed key is safer here than a
    spared file, so it is pinned rather than paraphrased: a Crew-side copy that
    drifted wider would admit a name the harness forwards to the model's shell.
    """
    from kiro_crew.acp import client as client_module

    pattern = client_module._DEEPSEEK_ENV_CHILD_SCRUB_CLASS
    for name in ("DEEPSEEK_API_KEY", "MY_PASSWORD", "A_SECRET", "GH_TOKEN", "api_key"):
        assert pattern.search(name), name
    for name in ("DEEPSEEK_CREDENTIAL", "PROVIDER_AUTH", "DEEPSEEK_PAT"):
        assert not pattern.search(name), name
    # And the identifier grammar rejects a trailing newline, which a ``$``-anchored
    # Python pattern would have accepted.
    assert client_module._DEEPSEEK_ENV_NAME_GRAMMAR.fullmatch("DEEPSEEK_API_KEY")
    assert not client_module._DEEPSEEK_ENV_NAME_GRAMMAR.fullmatch("DEEPSEEK_API_KEY\n")


def test_a_missing_vault_secret_refuses_rather_than_starting_without_a_key() -> None:
    """Fail closed, the way MCP spawn does, and name only the key.

    A reference to a secret that is not in the vault cannot be honoured, and starting
    the harness anyway would produce an opaque provider auth error on the first turn
    instead of a message the operator can act on.
    """
    from kiro_crew.acp import client as client_module
    from kiro_crew.config.paths import config_dir

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            client_module,
            "_validate_deepseek_env_mapping",
            client_module._validate_deepseek_env_mapping,
        )
        with pytest.raises(ValueError) as refused:
            client_module.resolve_secret_uris(
                _vault_env_mapping(),
                pathlib.Path(config_dir()),
                subject="agent.deepseek_env",
            )

    message = str(refused.value)
    assert "agent.deepseek_env env var 'DEEPSEEK_API_KEY'" in message
    assert "does not exist in the vault" in message
    assert "dsh-proof" not in message, "the refusal must not name the vault secret"


def _dsh_vault_spawn(tmp_path, monkeypatch, *, mapping, secret=None):
    """Drive a REAL deepseek ``_spawn`` to the process factory and capture its env.

    Everything between the arm and the factory is left real -- the vault read, the
    validator, the env build -- because those are the subject. Only the collaborators
    that would touch a live harness or a live process are replaced.

    Returns ``(client, spawned, run)`` where *spawned* is a dict the factory fills
    with the env OBJECT it was handed (not a copy), so the caller can assert both
    what the child inherited and what the parent dropped afterwards.
    """
    import asyncio

    from kiro_crew import agent_scratch
    from kiro_crew.acp import client as client_module
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets import SecretVault

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    root = agent_scratch.scratch_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (config_dir() / "config.json").write_text(
        json.dumps({"agent": {"deepseek_env": mapping}}), encoding="utf-8"
    )
    if secret is not None:
        SecretVault(config_dir()).set_sync(*secret)

    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DEEPSEEK)
    monkeypatch.setattr(
        client,
        "_resolve_self_served_launch",
        AsyncMock(return_value=("/fake/dsh", ["/fake/dsh", "--profile", "acp"], "dsh", "dsh")),
    )
    monkeypatch.setattr(client_module, "_run_preflight_bounded", AsyncMock(return_value=()))
    monkeypatch.setattr(
        client_module, "_seal_deepseek_gate_extension", lambda: str(tmp_path / "sealed.mjs")
    )
    monkeypatch.setattr(
        client_module, "_write_deepseek_gate_patch", lambda _ext: str(tmp_path / "patch.yml")
    )
    monkeypatch.setattr(client, "_verify_deepseek_gate", lambda *a, **k: ("", ""))
    monkeypatch.setattr(client_module, "_unlink_readback_launcher", lambda _p: None)

    async def wrap_ok(call_argv, **kwargs):
        return list(call_argv), None

    monkeypatch.setattr(client_module, "wrap_argv_async", wrap_ok)

    spawned: dict = {}
    proc = MagicMock()
    proc.pid = os.getpid()
    proc.returncode = None
    proc.stdout = MagicMock()
    proc.stderr = None
    proc.stdin = MagicMock()

    async def factory(*argv, **kwargs):
        # The env OBJECT, deliberately not a copy: the post-spawn clear mutates this
        # same dict, which is what the caller asserts against.
        spawned["env"] = kwargs.get("env")
        spawned["seen"] = dict(kwargs.get("env") or {})
        return proc

    monkeypatch.setattr(client_module, "create_subprocess_limited", factory)

    # A MagicMock owns no OS child, so the Windows cleanup layer has no handle to pin
    # and would quarantine the whole test process (``WindowsCleanupCapacityError`` for
    # every later spawn in the worker). Mirror the wrapper's own POSIX branch instead,
    # the way every other fake-process ``_spawn`` drive does (``test_acp_spawn_offload``,
    # ``acp_launch_capture``); the native pin/admission contract has its own suite.
    async def windows_cleanup_passthrough(spawn_factory):
        return await spawn_factory()

    monkeypatch.setattr(
        client_module.platform_compat,
        "create_windows_cleanup_owned_process",
        windows_cleanup_passthrough,
    )
    monkeypatch.setattr(client_module, "finish_suspended_spawn", lambda *a, **k: True)
    monkeypatch.setattr(client_module, "_get_child_pids", lambda _pid: [])
    monkeypatch.setattr(client_module, "browser_session_env", lambda _env: {})
    monkeypatch.setattr(client_module, "inject_xdist_auto_cap", lambda _env: None)
    monkeypatch.setattr(client_module.agent_scratch, "record_owner", lambda *a, **k: "recorded")
    monkeypatch.setattr("kiro_crew.session._track_pid", lambda *a, **k: None)
    monkeypatch.setattr("kiro_crew.session._track_session_pid", lambda *a, **k: None)

    return client, spawned, asyncio


def test_the_vault_key_reaches_the_child_and_leaves_the_parent(tmp_path, monkeypatch) -> None:
    """The whole route, end to end through ``_spawn``: vault -> child env -> cleared.

    Three facts in one drive, because they are one mechanism. The key the operator
    stored reaches the harness process under the name they mapped; the plaintext
    never touches the gateway's own ``os.environ`` and is never kept on the client;
    and the dict the resolver returned is emptied INSIDE the deepseek arm as soon as
    its entries are on the child's env -- the contract
    ``secret_uri.resolve_secret_uris`` states, honoured where this adapter's code
    lives rather than on the shared post-spawn path (harness-parity H13). The
    child's own env dict is a local of ``_spawn`` and dies with the frame, so there
    is no later site for a clear to run at.
    """
    from kiro_crew.acp import client as client_module

    client, spawned, asyncio_mod = _dsh_vault_spawn(
        tmp_path,
        monkeypatch,
        mapping=_vault_env_mapping(),
        secret=("dsh-proof", "dummy-vault-fed-key"),
    )
    # The resolver's OWN returned dict, captured by identity so the clear can be
    # observed on the object the contract names rather than on a copy.
    resolved_dicts: list[dict] = []
    real_resolver = client_module._deepseek_vault_env

    def capturing_resolver():
        resolved, keys = real_resolver()
        resolved_dicts.append(resolved)
        return resolved, keys

    monkeypatch.setattr(client_module, "_deepseek_vault_env", capturing_resolver)

    asyncio_mod.run(client._spawn())

    assert spawned["seen"]["DEEPSEEK_API_KEY"] == "dummy-vault-fed-key"
    # The reference template is what stays in config; the plaintext is only ever in
    # the dict handed to the child.
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert resolved_dicts and resolved_dicts[0] == {}, (
        "the resolver's returned dict still holds the plaintext after the arm copied "
        "it onto the child's env"
    )
    for name, value in vars(client).items():
        assert "dummy-vault-fed-key" not in repr(
            value
        ), f"the plaintext provider key is retained on the client under {name!r}"


def test_a_refused_mapping_stops_the_session_before_any_child_starts(tmp_path, monkeypatch) -> None:
    """The refusal is a session refusal, raised before the harness process exists.

    Fail-closed means no child, not a child with a warning: a harness started without
    its key would take a turn, fail at the provider, and leave the operator reading a
    vendor error instead of the rule they broke.
    """
    from kiro_crew.acp.client import AcpToolGateUnroutable

    client, spawned, asyncio_mod = _dsh_vault_spawn(
        tmp_path, monkeypatch, mapping={"DEEPSEEK_API_KEY": "sk-live-not-a-reference"}
    )
    discarded: list[bool] = []
    monkeypatch.setattr(client, "_discard_sandbox_cleanup", lambda: discarded.append(True))
    probed: list[bool] = []
    monkeypatch.setattr(
        client, "_verify_deepseek_gate", lambda *a, **k: probed.append(True) or ("", "")
    )

    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio_mod.run(client._spawn())

    assert "holds a literal value" in str(refused.value)
    assert "sk-live-not-a-reference" not in str(refused.value)
    assert not spawned, "the refusal reached the process factory"
    # The mapping is validated in the arm, BEFORE the probe boots a harness on it and
    # before any sandbox launcher exists: nothing to reclaim, nothing booted.
    assert probed == [], "a mapping this harness would not honour still booted the probe"
    assert discarded == [], "no launcher existed yet, so none may be discarded"


def test_an_empty_mapping_costs_the_spawn_no_vault_read(tmp_path, monkeypatch) -> None:
    """The default is no key, and it must not pay for one.

    A locally served model needs no provider key, so the common configuration is an
    empty mapping -- and an empty mapping must not open the vault, both because the
    read is filesystem work on every DeepSeek session start and because a vault that
    cannot be loaded must not refuse a session that referenced nothing in it.
    """
    from kiro_crew.acp import client as client_module

    client, spawned, asyncio_mod = _dsh_vault_spawn(tmp_path, monkeypatch, mapping={})
    reads: list[object] = []
    monkeypatch.setattr(
        client_module,
        "resolve_secret_uris",
        lambda *a, **k: reads.append(a) or ({}, set()),
    )

    asyncio_mod.run(client._spawn())

    assert reads == [], "an empty agent.deepseek_env still read the vault"
    assert "DEEPSEEK_API_KEY" not in spawned["seen"]


def test_the_injection_is_inside_the_arm_and_the_probe_is_never_handed_a_key() -> None:
    """Placement, pinned by TEXT: the deepseek env block, and nowhere else.

    Two claims. The injection sits inside ``if self._is_deepseek:`` -- so the Kiro
    construction path gains nothing (harness-parity H13) and the key lands BEFORE
    ``scrub_agent_subprocess_env``, which is the whole reason the validator refuses a
    name that scrub would strip. And the read-back PROBE, which boots the plugin and
    exits, is never handed one: it needs no provider key, and a probe carrying a live
    key would widen the secret's exposure to a second process for no gain.
    """
    body = inspect.getsource(AcpClient._spawn)
    assert body.count("_deepseek_vault_env)") == 1
    block = body[body.index("if self._is_deepseek:") :]
    block = block[: block.index("self._apply_session_identity_env(env)")]
    assert "_deepseek_vault_env)" in block
    assert "env.update(deepseek_env)" in block
    # ... and before the shared tail's two env passes.
    assert body.index("_deepseek_vault_env)") < body.index("scrub_agent_subprocess_env(")

    # The probe's own env build is the arm's, and it names no vault resolution: the
    # arm derives the NAMES only, which the probe hands the plugin under canaries.
    arm = body[body.index("elif self._is_deepseek:") :]
    arm = arm[: arm.index("\n        else:")]
    assert "_deepseek_vault_env)" not in arm
    assert "_deepseek_vault_env_names" in arm
    assert "resolve_secret_uris" not in arm
