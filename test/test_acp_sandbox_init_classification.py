"""A sandbox that refuses to initialize must fail fast and name the layer.

A host whose OS sandbox cannot be built reported only the downstream symptom --
the agent process exited -- while the cause sat one line earlier on the child's
stderr (``sandbox initialization failed: Operation not permitted``). The death
summary now carries that tail, so the line is at least visible, but the failure
is still treated as a transport fault: the reconnect ladder respawns, the same
host refuses identically, and every entry in the UI crash-loops.

The refusal is DETERMINISTIC, which is the whole argument. The repo already has
the shape for that -- ``AcpAuthRequired`` and ``AcpToolGateUnroutable`` are
non-retryable because respawning hits the same wall -- and this is the third
member of that family.

Two things are asserted, and the second is the one that makes the error useful:

* exactly ONE spawn attempt, and a classified type rather than a generic death;
* the layer that wrapped the spawn, plus a remedy whose STRENGTH matches the
  evidence. Signature alone cannot supply the layer: a harness's internal sandbox
  nested inside Crew's wrap fails with the HARNESS's wording while the layer the
  operator must change is CREW's, which is how the field reports were actually
  resolved. So the layer comes from the argv Kiro Crew itself built.

  The switch that turns a layer OFF is a separate question, and the stderr cannot
  answer it: the dead child is the unverified binary the sandbox exists to
  contain, so a planted one could otherwise print a refusal and talk the operator
  into removing the isolation it runs under. The switch is emitted only on a
  verdict from a TRUSTED launcher run.

A control tail must stay retryable throughout. ``Operation not permitted`` and
``Failed to spawn child process`` are ordinary output for a missing binary or a
denied credential file, and latching a permanent verdict onto those would delete
retries that work today.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    AcpSandboxInitFailed,
    is_sandbox_init_failure_output,
    sandbox_init_failure,
)
from kiro_crew.sandbox import (
    SANDBOX_LAYER_CREW,
    SANDBOX_LAYER_HARNESS,
    kiro_internal_sandbox_switch,
    wrapped_by_crew_sandbox,
)

# The reported three-line burst, verbatim. Only the first line is sandbox-specific.
HARNESS_SANDBOX_STDERR = [
    "sandbox initialization failed: Operation not permitted",
    "Error: Failed to spawn child process",
    "Caused by: Invalid argument (os error 22)",
]

# macOS: Crew's own seatbelt wrapper refusing before the child runs.
SANDBOX_EXEC_STDERR = ["sandbox-exec: sandbox_apply: Operation not permitted"]

# Linux: Crew's namespace launcher refusing AFTER the spawn, on its own prefixes.
LAUNCHER_STDERR = [
    "sandbox: BLOCKED -- unshare(NEWUSER) failed: errno 1",
]

# Launcher lines that carry a BLOCKED/FATAL prefix and are NOT this signature.
# `sandbox.launcher_refusal` already classifies each; matching the prefixes alone
# would make all three permanent.
LAUNCHER_NON_REFUSALS = [
    # The sandbox WORKED and found host state it must not paper over.
    "sandbox: BLOCKED -- found hardlink(s) to protected credential ~/.aws/credentials",
    # A failed parent/child pipe handshake: classified transient, self-heals.
    "sandbox: FATAL launcher child did not publish its readiness",
    # The caller invoked the launcher with no command: a defect in us.
    "sandbox_launcher: no command given",
]

# The control. Same errno, same spawn failure, NO sandbox token: a missing
# interpreter, a denied binary, a bad exec bit all look like this, and a respawn
# after a transient mount/fs hiccup legitimately fixes them.
RETRYABLE_STDERR = [
    "Error: Failed to spawn child process",
    "Caused by: Operation not permitted (os error 1)",
    "Caused by: Invalid argument (os error 22)",
]


# ── The classifier ──


@pytest.mark.parametrize(
    "lines",
    [
        HARNESS_SANDBOX_STDERR,
        SANDBOX_EXEC_STDERR,
        LAUNCHER_STDERR,
        # Case sensitivity and leading whitespace must not decide it: the same
        # refusal is printed by three different programs.
        ["  SANDBOX INITIALIZATION FAILED: Operation not permitted"],
        ["sandbox: unshare(NEWNS) failed: errno 22"],
    ],
    ids=["harness", "sandbox-exec", "launcher", "shouty", "launcher-newns"],
)
def test_every_signature_is_classified(lines):
    assert is_sandbox_init_failure_output("\n".join(lines)) is True


@pytest.mark.parametrize(
    "lines",
    [
        RETRYABLE_STDERR,
        ["Access denied: The bearer token included in the request is invalid."],
        ["Starting kiro-cli 2.19.1", "INFO listening on stdio"],
        # Advisory: the launcher warned and then RAN the child. Not a refusal.
        ["sandbox: WARNING could not hide /home/u/.aws, continuing"],
        # The word appears, but as the child's own prose about something else.
        ["note: run with --sandbox to enable the sandbox"],
    ],
    ids=["spawn-failure", "auth", "healthy", "launcher-warning", "prose"],
)
def test_a_control_tail_stays_retryable(lines):
    assert is_sandbox_init_failure_output("\n".join(lines)) is False


def test_the_bare_errno_alone_is_not_enough():
    """The mutation this classifier must not survive.

    Dropping the sandbox anchor -- matching ``Operation not permitted`` or
    ``Failed to spawn child process`` on its own, as the reported burst's other
    two lines invite -- makes every ordinary spawn failure permanent. Asserted as
    its own case because it is the single change that would make the parametrized
    control above pass for the wrong reason.
    """
    assert is_sandbox_init_failure_output("Operation not permitted") is False
    assert is_sandbox_init_failure_output("Failed to spawn child process") is False
    assert is_sandbox_init_failure_output("Invalid argument (os error 22)") is False


# ── The layer, and its remedy ──


@pytest.mark.asyncio
async def test_an_uncorroborated_refusal_never_hands_out_the_disable_switch():
    """The security property, and the reason this is not a message-formatting test.

    The tail is the dead child's own stderr, and that child is the unverified
    binary the sandbox exists to contain. A message answering it with the
    ``agent.sandbox off`` switch would let a planted binary print one line and have
    Kiro Crew instruct the operator to remove the isolation it is running under.
    So the switch is withheld and the operator is routed to the check that can
    reach a verdict.
    """
    for crew_wrap in (True, False):
        exc = await sandbox_init_failure("\n".join(HARNESS_SANDBOX_STDERR), crew_wrap=crew_wrap)
        assert "config set agent.sandbox off" not in str(exc)
        assert '"sandbox": false' not in str(exc)
        # Still actionable: the layer is named, from Kiro Crew's own argv rather
        # than from the child.
        expected = SANDBOX_LAYER_CREW if crew_wrap else SANDBOX_LAYER_HARNESS
        assert expected in str(exc)
        # And it promises no verdict it cannot reach. The trusted run covers Crew's
        # Linux launcher only; the macOS probe validates an (allow default) profile
        # while the real wrap applies the strict one, so pointing the operator at a
        # check there would false-green exactly the failure they are looking at.
        assert "doctor" not in str(exc)
        assert "check the host" in str(exc).lower()


@pytest.mark.asyncio
async def test_a_corroborated_crew_refusal_does_hand_out_the_switch():
    """And a verdict from a TRUSTED run is what unlocks it.

    Patched at the corroboration seam rather than by running a real launcher: what
    is under test is that the switch is gated on that function's answer, and the
    function's own behaviour is pinned by the sandbox suite.
    """
    with patch(
        "kiro_crew.acp.client.corroborate_launcher_refusal",
        return_value=("no_backend", "sandbox: BLOCKED", "install the profile"),
    ):
        exc = await sandbox_init_failure("\n".join(LAUNCHER_STDERR), crew_wrap=True)
    assert "config set agent.sandbox off" in str(exc)
    assert SANDBOX_LAYER_CREW in str(exc)
    # The isolation cost is stated, never implied: a silent downgrade to an
    # unconfined agent is refused by design, so the operator taking the opt-out
    # has to be told what it removes.
    assert "isolation" in str(exc)
    assert "security event log" in str(exc)


@pytest.mark.asyncio
async def test_a_trusted_run_that_clears_the_host_withholds_the_switch():
    """``None`` from corroboration means the child's line was not a host verdict."""
    with patch("kiro_crew.acp.client.corroborate_launcher_refusal", return_value=None):
        exc = await sandbox_init_failure("\n".join(LAUNCHER_STDERR), crew_wrap=True)
    assert "config set agent.sandbox off" not in str(exc)


@pytest.mark.asyncio
async def test_a_transient_trusted_verdict_withholds_the_switch():
    """Corroboration means ``no_backend``, not merely "the trusted run failed".

    ``corroborate_launcher_refusal`` also reports ``transient`` for a failed
    launcher readiness handshake -- a pipe protocol failure that says nothing
    about the host and self-heals on the next spawn. Handing out the disable
    switch for that would remove isolation over a condition that fixes itself.
    """
    with patch(
        "kiro_crew.acp.client.corroborate_launcher_refusal",
        return_value=("transient", "sandbox: FATAL launcher child did not publish", ""),
    ):
        exc = await sandbox_init_failure("\n".join(LAUNCHER_STDERR), crew_wrap=True)
    assert "config set agent.sandbox off" not in str(exc)


@pytest.mark.asyncio
async def test_corroboration_is_not_attempted_without_a_launcher_line():
    """No trusted subprocess for a signature it could not speak to.

    ``sandbox initialization failed`` is the harness's wording; Crew's launcher
    never prints it, so running its corroboration would spend a subprocess on a
    question it cannot answer.
    """
    with patch("kiro_crew.acp.client.corroborate_launcher_refusal") as corroborate:
        await sandbox_init_failure("\n".join(HARNESS_SANDBOX_STDERR), crew_wrap=True)
    corroborate.assert_not_called()


@pytest.mark.asyncio
async def test_an_uncorroborated_harness_refusal_names_no_switch_either():
    """Same rule, and this branch is the sharper case.

    The layer being blamed IS the agent's own sandbox, and the only evidence is
    that agent's own output. Naming its key would let a planted binary talk the
    operator into removing the only isolation this child had -- so an
    uncorroborated harness message names the layer and nothing to turn off.
    """
    path, key = kiro_internal_sandbox_switch()
    exc = await sandbox_init_failure("\n".join(HARNESS_SANDBOX_STDERR), crew_wrap=False)
    assert path not in str(exc)
    assert "NOT confirmed" in str(exc)
    assert SANDBOX_LAYER_HARNESS in str(exc)
    assert key != ""


@pytest.mark.asyncio
async def test_a_crew_launcher_verdict_cannot_unlock_the_harness_switch():
    """No cross-layer inference, and this is the case that tempts one.

    Corroboration re-runs KIRO CREW'S OWN launcher, so its verdict is evidence
    about Crew's layer and says nothing about the harness's internal sandbox. A
    host that cannot build Crew's namespace would otherwise hand the operator the
    key that turns off the OTHER sandbox -- and on this branch that sandbox is the
    only isolation the child had, so the one confirmed outcome would be that the
    isolation is gone.
    """
    path, key = kiro_internal_sandbox_switch()
    with patch(
        "kiro_crew.acp.client.corroborate_launcher_refusal",
        return_value=("no_backend", "sandbox: BLOCKED", ""),
    ):
        exc = await sandbox_init_failure("\n".join(LAUNCHER_STDERR), crew_wrap=False)
    assert path not in str(exc)
    assert "agent.sandbox" not in str(exc)
    assert "NOT confirmed" in str(exc)
    assert key != ""


@pytest.mark.asyncio
async def test_the_crew_switch_states_that_governance_can_refuse_it():
    """``off`` is a REQUEST, not an outcome: a governance floor clamps it back up.

    Promising "spawns agents unconfined here" would be false on a governed host,
    where the operator runs the command, sees no error, and is told the isolation
    is gone when it is not.
    """
    with patch(
        "kiro_crew.acp.client.corroborate_launcher_refusal",
        return_value=("no_backend", "sandbox: BLOCKED", ""),
    ):
        exc = await sandbox_init_failure("\n".join(LAUNCHER_STDERR), crew_wrap=True)
    assert "config set agent.sandbox off" in str(exc)
    assert "GOVERNANCE PERMITS" in str(exc)


@pytest.mark.asyncio
async def test_the_same_signature_yields_different_layers():
    """The reason the layer is NOT derived from the signature.

    Identical stderr, different layer named. A harness sandbox nested inside
    Crew's wrap prints the harness's wording; the layer to look at is Crew's.
    """
    tail = "\n".join(HARNESS_SANDBOX_STDERR)
    crew = str(await sandbox_init_failure(tail, crew_wrap=True))
    harness = str(await sandbox_init_failure(tail, crew_wrap=False))
    assert SANDBOX_LAYER_CREW in crew and SANDBOX_LAYER_HARNESS not in crew
    assert SANDBOX_LAYER_HARNESS in harness


@pytest.mark.asyncio
async def test_the_error_is_never_retryable():
    for crew_wrap in (True, False):
        exc = await sandbox_init_failure(
            "sandbox_apply: Operation not permitted", crew_wrap=crew_wrap
        )
        assert isinstance(exc, AcpError)
        assert exc.transient is False, (
            "transient must be an explicit False: every retry ladder reads the "
            "verdict off the exception, and an unclassified None sends this to a "
            "wording fallback that retries it"
        )


# ── Where the layer comes from: the wrapped argv ──


def test_crew_wrappers_are_recognised_in_a_wrapped_argv():
    """Both shapes ``wrap_argv`` can return, keyed on the constants it uses.

    Spelled from the module's own constants rather than from literals, so a
    rename of either wrapper artifact breaks here instead of silently reporting
    every macOS spawn as unwrapped and handing out the wrong switch.
    """
    from kiro_crew.sandbox import _IN_SANDBOX_MARKER, _SANDBOX_ARTIFACT_PREFIX

    seatbelt = [
        "/usr/bin/env",
        "-u",
        "AWS_PROFILE",
        f"{_IN_SANDBOX_MARKER}=1",
        "KIROCREW_SANDBOX_LEVEL=strict",
        "/usr/bin/sandbox-exec",
        "-f",
        "/tmp/profile.sb",
        "kiro-cli",
    ]
    launcher = [
        "/usr/bin/python3",
        "-I",
        "-S",
        f"/run/user/1000/kirocrew/{_SANDBOX_ARTIFACT_PREFIX}4242_ab12.py",
        "kiro-cli",
    ]
    assert wrapped_by_crew_sandbox(seatbelt) is True
    assert wrapped_by_crew_sandbox(launcher) is True


@pytest.mark.parametrize(
    "argv",
    [
        # Delegated to the harness's internal sandbox: env scrub only.
        ["/usr/bin/env", "-u", "AWS_PROFILE", "kiro-cli", "acp"],
        # Windows delegation and the genuinely unconfined path: untouched argv.
        ["kiro-cli", "acp"],
    ],
    ids=["delegated-posix", "bare"],
)
def test_a_spawn_without_crews_wrapper_is_not_attributed_to_crew(argv):
    assert wrapped_by_crew_sandbox(argv) is False


# ── The retry ladder: exactly one spawn attempt ──


def _client(stderr_lines, *, crew_wrap: bool) -> AcpClient:
    """An ``AcpClient`` whose spawn dies, with *stderr_lines* already captured.

    Seeded the way a real refused spawn leaves the object: the drain has appended
    what the child said, and ``_spawn`` has recorded which layer wrapped it.
    """
    client = AcpClient()
    client._process = None
    client._session_id = None
    client._kill_process = AsyncMock()
    client._cleanup_failed_live_spawn = AsyncMock()

    def _reset():
        # The real ``_reset_state`` drops the process handle, which is what makes
        # the next pass through the loop spawn again. A bare MagicMock leaves it
        # set, and the retry silently becomes a no-op -- so the control test would
        # count one attempt and "pass" against the unfixed tree too.
        client._process = None
        client._session_id = None

    client._reset_state = _reset
    client._snapshot_process_tree = AsyncMock()
    client._sandbox_wrapped_by_crew = crew_wrap
    client._sandbox_hidden_dirs = ()
    client._stderr_lines.extend(stderr_lines)
    return client


def _counting_spawn(client, attempts: list[int]):
    async def _fake():
        attempts.append(1)
        client._process = MagicMock()
        client._process.returncode = None

    return _fake


async def _dies(_client=None):
    # The symptom a refused spawn actually reaches the caller as: the child is
    # gone by the time `initialize` is answered, so the EOF read raises this.
    raise AcpError("ACP process exited (code=1)")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lines", "crew_wrap", "layer"),
    [
        (HARNESS_SANDBOX_STDERR, False, SANDBOX_LAYER_HARNESS),
        (SANDBOX_EXEC_STDERR, True, SANDBOX_LAYER_CREW),
        (LAUNCHER_STDERR, True, SANDBOX_LAYER_CREW),
    ],
    ids=["harness", "sandbox-exec", "launcher"],
)
async def test_a_refused_sandbox_burns_exactly_one_spawn(lines, crew_wrap, layer):
    """The defect: the second attempt rebuilds the same profile on the same host."""
    client = _client(lines, crew_wrap=crew_wrap)
    attempts: list[int] = []
    client._spawn = _counting_spawn(client, attempts)
    client._initialize_session = _dies

    with pytest.raises(AcpSandboxInitFailed) as caught:
        await client.ensure_ready()

    assert len(attempts) == 1, (
        f"a deterministic sandbox refusal was respawned {len(attempts)} times; "
        "the reconnect budget exists for transport faults"
    )
    assert layer in str(caught.value)
    assert caught.value.transient is False


@pytest.mark.asyncio
async def test_a_control_death_still_gets_its_retry():
    """The budget is skipped for THIS class only, not narrowed for everything."""
    client = _client(RETRYABLE_STDERR, crew_wrap=True)
    attempts: list[int] = []
    client._spawn = _counting_spawn(client, attempts)
    client._initialize_session = _dies

    with pytest.raises(AcpError) as caught:
        await client.ensure_ready()

    assert not isinstance(caught.value, AcpSandboxInitFailed)
    assert len(attempts) == 2, "an unclassified init failure must keep its one retry"


@pytest.mark.asyncio
async def test_a_session_retaining_no_stderr_is_not_guessed_at():
    """No stderr, no verdict.

    A restricted-memory session keeps no child output by design. Classifying from
    an empty buffer could only mean inventing an answer, so those sessions keep
    today's retry behaviour instead.
    """
    client = _client([], crew_wrap=True)
    attempts: list[int] = []
    client._spawn = _counting_spawn(client, attempts)
    client._initialize_session = _dies

    with pytest.raises(AcpError) as caught:
        await client.ensure_ready()

    assert not isinstance(caught.value, AcpSandboxInitFailed)
    assert len(attempts) == 2


# ── The shared runtime: the latch, and the provider's translation ──


class _FakeStream:
    def __init__(self, lines):
        self._lines = [f"{line}\n".encode() for line in lines]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeProcess:
    def __init__(self, lines):
        self.stderr = _FakeStream(lines)


def _runtime(lines):
    from kiro_crew.acp.runtime import AcpRuntime

    runtime = AcpRuntime.__new__(AcpRuntime)
    runtime.recording_allowed = True
    runtime._stderr_lines = []
    runtime._saw_auth_failure = False
    runtime._saw_sandbox_init_failure = False
    runtime._sandbox_wrapped_by_crew = False
    # The sandbox latch arms only while startup is unfinished, so the window has
    # to be open for a startup corpus to land. Startup ends at the first session
    # handle, which is LATER than the handshake -- a sandboxed MCP launcher the
    # child starts for that session can refuse after ``initialize`` returned.
    runtime._initialized = False
    runtime._first_session_ready = False
    runtime._process = _FakeProcess(lines)
    return runtime


@pytest.mark.asyncio
async def test_the_runtime_latches_the_refusal_past_the_ring_buffer():
    """The latch, for the same reason the auth latch exists.

    ``_stderr_lines`` is a 20-line ring and nothing asks why the runtime died
    until a request has already failed. On a chatty startup the sandbox line is
    evicted by then, so a detector that re-scans the buffer answers "not a sandbox
    problem" -- indistinguishable from a real negative.
    """
    runtime = _runtime(HARNESS_SANDBOX_STDERR + [f"noise line {i}" for i in range(40)])
    await runtime._drain_stderr()

    assert len(runtime._stderr_lines) == 20, "precondition: the ring must have trimmed"
    assert not any(
        "sandbox initialization failed" in line for line in runtime._stderr_lines
    ), "precondition: the sandbox line must have been evicted, or this proves nothing"
    assert runtime.saw_sandbox_init_failure() is True


@pytest.mark.asyncio
async def test_a_restricted_runtime_still_latches():
    """Retention is off, so there is no buffer to scan -- and the answer still lands.

    A restricted session keeps no stderr, which is exactly the session that most
    needs the classification: without it the operator gets an exit code and
    nothing else.
    """
    runtime = _runtime(SANDBOX_EXEC_STDERR)
    runtime.recording_allowed = False
    await runtime._drain_stderr()

    assert runtime._stderr_lines == []
    assert runtime.saw_sandbox_init_failure() is True


@pytest.mark.asyncio
async def test_a_healthy_runtime_never_latches():
    runtime = _runtime(["Starting kiro-cli 2.19.1"] + RETRYABLE_STDERR)
    await runtime._drain_stderr()
    assert runtime.saw_sandbox_init_failure() is False


def _startup_runtime(*, latched: bool, crew_wrap: bool):
    """A runtime double shaped like one whose spawn just failed."""
    runtime = MagicMock()
    runtime.settle_stderr = AsyncMock()
    runtime.saw_sandbox_init_failure.return_value = latched
    runtime.sandbox_wrapped_by_crew = crew_wrap
    runtime.sandbox_mode = "standard"
    runtime.sandbox_hidden_dirs = ()
    runtime.death_summary.return_value = (
        "runtime failed [returncode=1] stderr_tail: sandbox initialization failed"
    )
    runtime.saw_not_logged_in.return_value = False
    runtime.acp_backend = ""
    return runtime


@pytest.mark.asyncio
async def test_a_live_sessions_death_is_never_a_sandbox_verdict():
    """The latch is spent at ``initialize``, so the per-turn path cannot read it.

    A session provider only exists once the handshake succeeded, which is the proof
    the sandbox line was not fatal. Any death it then translates is a different
    problem, and answering it with a permanent "your sandbox is broken" would be a
    stale verdict that never expires.
    """
    from kiro_crew.acp.client import AcpProcessDied
    from kiro_crew.acp.runtime import AcpRuntimeDead
    from kiro_crew.acp.session_provider import AcpSessionProvider

    provider = AcpSessionProvider.__new__(AcpSessionProvider)
    runtime = _startup_runtime(latched=True, crew_wrap=True)
    provider._runtime = runtime

    translated = provider._translate_dead(AcpRuntimeDead("process exited (rc=1)"))
    assert isinstance(translated, AcpProcessDied)
    assert not isinstance(translated, AcpSandboxInitFailed)


def test_the_first_session_clears_the_latch_in_the_source():
    """Wiring: the clear sits with the ``_first_session_ready`` it belongs to.

    Asserted on the source because the alternative is driving a real spawn plus a
    real ``session/new``. What can go wrong is the two drifting apart, or the clear
    migrating back to the handshake and re-opening the gap, and both are properties
    of the file.
    """
    from pathlib import Path

    import kiro_crew.acp.runtime as runtime_mod

    source = Path(runtime_mod.__file__).read_text()
    marker = "self._first_session_ready = True"
    assert source.count(marker) == 1, "more than one startup completion to keep in step"
    after = source.split(marker, 1)[1][:400]
    assert "self._saw_sandbox_init_failure = False" in after, (
        "the startup latch is not spent where startup completes, so a refusal seen "
        "during startup can still answer an unrelated later death"
    )
    # And NOT at the handshake, which is the middle of startup: a clear there
    # re-opens the session/new window this guards.
    handshake = source.split("self._initialized = True", 1)[1][:400]
    assert "self._saw_sandbox_init_failure = False" not in handshake, (
        "the latch is spent at the handshake, so a sandbox refusal during "
        "session/new is never classified"
    )


def test_the_retry_deciders_all_refuse_it():
    """One assertion for every consumer, made through the shared predicate.

    ``acp_error_is_transient`` is what the dashboard turn, the cron tick, the
    subagent run and the workflow step all ask, so pinning it here pins them
    without naming each.
    """
    from kiro_crew.llm_helpers import acp_error_is_transient

    exc = AcpSandboxInitFailed(layer=SANDBOX_LAYER_CREW, detail="sandbox_apply")
    assert acp_error_is_transient(exc) is False


# ── Launcher lines that carry a blocking prefix but are not this signature ──


@pytest.mark.parametrize("line", LAUNCHER_NON_REFUSALS, ids=["hardlink", "fatal", "no-command"])
def test_a_launcher_line_that_is_not_a_sandbox_failure_stays_retryable(line):
    """The prefixes are not the test; ``launcher_refusal``'s own verdict is.

    Two of these are actively retryable. ``FATAL ... did not publish`` is a failed
    pipe handshake that says nothing about the host and self-heals on the next
    spawn; the hardlink refusal means the sandbox built fine and found host state
    it must not paper over. Classifying either as a permanent sandbox-init failure
    would hand the operator the opt-out for a layer that is working.
    """
    assert is_sandbox_init_failure_output(line) is False


def test_a_real_refusal_below_a_non_refusal_is_still_found():
    """Per-LINE classification, which is why the reuse is not a one-liner.

    ``launcher_refusal`` returns on its first matching line, so a hardlink line
    above a genuine ``unshare`` refusal would answer ``None`` for the whole tail
    and hide it.
    """
    tail = "\n".join([LAUNCHER_NON_REFUSALS[0]] + LAUNCHER_STDERR)
    assert is_sandbox_init_failure_output(tail) is True


@pytest.mark.asyncio
async def test_a_mid_life_sandbox_line_does_not_latch():
    """The latch arms only while startup is unfinished.

    The harness's own sandbox can refuse when IT spawns a tool subprocess, long
    after the agent process started fine. That says nothing about whether the
    agent process can start, so latching it would turn the NEXT unrelated death --
    a broken pipe, an OOM kill -- into a permanent "your sandbox is broken", and
    permanently is how long the wrong verdict would last.
    """
    runtime = _runtime(HARNESS_SANDBOX_STDERR)
    runtime._first_session_ready = True
    await runtime._drain_stderr()
    assert runtime.saw_sandbox_init_failure() is False


@pytest.mark.asyncio
async def test_a_refusal_after_the_handshake_still_latches():
    """Startup does not end at ``initialize``, and this is the gap that closes.

    ``session/new`` starts the child's own MCP servers, and a sandboxed launcher
    among them can refuse there -- after the handshake returned. Closing the window
    at the handshake would leave every such refusal unclassified on exactly the
    translation site (``create_session``) that exists to catch it, and the caller
    would emit a generic retryable startup failure instead.
    """
    runtime = _runtime(HARNESS_SANDBOX_STDERR)
    runtime._initialized = True
    runtime._first_session_ready = False
    await runtime._drain_stderr()
    assert runtime.saw_sandbox_init_failure() is True


@pytest.mark.asyncio
async def test_a_startup_refusal_survives_a_later_handshake():
    """And a refusal seen during startup stays latched.

    The death that asks about it is always a later event, so the observation has to
    outlive the step that was running when it arrived.
    """
    runtime = _runtime(HARNESS_SANDBOX_STDERR)
    await runtime._drain_stderr()
    runtime._initialized = True
    assert runtime.saw_sandbox_init_failure() is True


# ── The shared-runtime STARTUP paths, alongside the per-turn one ──


@pytest.mark.asyncio
async def test_the_shared_translation_is_what_every_startup_site_reads():
    """One verdict for every startup site, so none of them can disagree."""
    from kiro_crew.acp.client import sandbox_init_failure_for_runtime

    runtime = _startup_runtime(latched=True, crew_wrap=False)
    classified = await sandbox_init_failure_for_runtime(runtime)
    assert isinstance(classified, AcpSandboxInitFailed)
    assert SANDBOX_LAYER_HARNESS in str(classified)

    runtime.saw_sandbox_init_failure.return_value = False
    assert await sandbox_init_failure_for_runtime(runtime) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crew_wrap", "layer"),
    [(True, SANDBOX_LAYER_CREW), (False, SANDBOX_LAYER_HARNESS)],
    ids=["crew", "harness"],
)
async def test_the_shared_runtimes_cold_start_is_classified(crew_wrap, layer):
    """The gap a per-turn-only fix leaves: the pool respawning a refused worker.

    On the default transport the child is spawned by ``AcpProvider``, and a
    ``spawn()`` that dies reached only the auth translation -- so the refusal
    arrived as a generic runtime error and the worker was replaced, which
    reproduces it.
    """
    from kiro_crew.acp.client import sandbox_init_failure_for_runtime

    runtime = _startup_runtime(latched=True, crew_wrap=crew_wrap)
    classified = await sandbox_init_failure_for_runtime(runtime)
    assert isinstance(classified, AcpSandboxInitFailed)
    assert layer in str(classified)


def test_every_shared_runtime_startup_site_consults_the_latch():
    """Wiring, asserted on the source: two sites, both converted.

    A behavioural test per site would need the whole ``start()`` preamble; what
    can go wrong here is a site left behind, and that is a property of the file.
    Counted against the auth latch it sits beside, so adding a third startup
    path without the sandbox check fails here.

    One of the two serves both the first spawn and the post-resume respawn: they
    share a single spawn helper, so the check is written once and both paths
    reach it. The receiver is matched as ANY name rather than as ``runtime``,
    because that helper holds its process in a local of its own and a
    name-specific pattern would read the spelling as a site that vanished.
    """
    import re
    from pathlib import Path

    import kiro_crew.providers.acp as provider_mod

    source = Path(provider_mod.__file__).read_text()
    auth_sites = len(re.findall(r"\w+\.saw_not_logged_in\(\)", source))
    sandbox_sites = len(re.findall(r"sandbox_init_failure_for_runtime\(\w+\)", source))
    assert auth_sites == 2, f"the auth latch is read at {auth_sites} sites, not 2"
    assert sandbox_sites == auth_sites, (
        f"{sandbox_sites} of {auth_sites} shared-runtime startup sites classify a "
        "sandbox refusal; a site left behind reports it as a generic death and the "
        "pool respawns into the same wall"
    )


# ── The latch is written by one task and read from another ──


@pytest.mark.asyncio
async def test_the_losing_schedule_is_reproduced_and_closed():
    """The race, driven deterministically rather than hoped against.

    The latch is written by the ``_drain_stderr`` TASK. The death that brings a
    caller to the classifier is discovered on the stdout side, where ``_mark_dead``
    fails the pending ``initialize`` synchronously -- so both are runnable together
    and the read can happen first. That is the ordinary shape of a real refusal:
    the child writes its signature and closes stdout at once.

    Reproduced by creating the drain task and NOT letting it run: the first assert
    IS the losing schedule, and it fails on any implementation that closes the race
    by luck instead of by waiting.
    """
    import asyncio

    runtime = _runtime(HARNESS_SANDBOX_STDERR)
    runtime._stderr_task = asyncio.ensure_future(runtime._drain_stderr())
    try:
        assert (
            runtime.saw_sandbox_init_failure() is False
        ), "precondition: the drain must not have run yet, or this proves nothing"
        await runtime.settle_stderr()
        assert runtime.saw_sandbox_init_failure() is True, (
            "the classification lost the race to the stderr drain, so a real "
            "refusal reports as a generic repeatable process crash"
        )
    finally:
        runtime._stderr_task.cancel()


@pytest.mark.asyncio
async def test_a_finished_or_absent_drain_costs_nothing():
    """The settle is a no-op where there is nothing to wait for."""
    runtime = _runtime([])
    runtime._stderr_task = None
    await runtime.settle_stderr()

    await runtime._drain_stderr()
    done = asyncio.get_event_loop().create_future()
    done.set_result(None)
    await runtime.settle_stderr()


@pytest.mark.asyncio
async def test_a_wedged_drain_cannot_hold_the_failure_path():
    """Bounded: a drain that never finishes must not turn one failure into two."""
    import asyncio

    async def _never() -> None:
        await asyncio.sleep(3600)

    runtime = _runtime([])
    runtime._stderr_task = asyncio.ensure_future(_never())
    try:
        # An OUTER deadline, generously above the inner one: an unbounded settle
        # would otherwise hang this test rather than fail it, and a hang in CI is
        # a timeout nobody attributes.
        await asyncio.wait_for(runtime.settle_stderr(timeout=0.05), timeout=5)
    except asyncio.TimeoutError:  # pragma: no cover - the defect this pins
        raise AssertionError(
            "the stderr settle is unbounded, so a wedged drain holds a failure "
            "path open instead of letting it report"
        ) from None
    finally:
        runtime._stderr_task.cancel()


@pytest.mark.asyncio
async def test_the_shared_translation_settles_before_it_reads():
    """Wiring: the helper waits, rather than each of its three callers remembering to."""
    from kiro_crew.acp.client import sandbox_init_failure_for_runtime

    runtime = _startup_runtime(latched=False, crew_wrap=True)

    async def _settle() -> None:
        # The drain landing its line, as a side effect of the wait.
        runtime.saw_sandbox_init_failure.return_value = True

    runtime.settle_stderr = AsyncMock(side_effect=_settle)
    classified = await sandbox_init_failure_for_runtime(runtime)
    runtime.settle_stderr.assert_awaited()
    assert isinstance(classified, AcpSandboxInitFailed)


# -- The drain must be read before the cleanup that discards it --


def test_the_failed_handshake_settles_the_drain_before_teardown():
    """Ordering, asserted on the source: settle FIRST, then tear down.

    ``spawn()``'s failure handler tears the runtime down, and that teardown
    cancels the stderr drain. A line still in the pipe when it does is a line
    nobody will ever read -- the caller's own settle then finds the task already
    done and learns nothing, so a real refusal reports as a generic crash and the
    ladder retries into the same wall.

    Asserted on the file because what can go wrong is the two statements swapping
    places, which no behavioural double can pin without driving a whole real spawn.
    """
    from pathlib import Path

    import kiro_crew.acp.runtime as runtime_mod

    source = Path(runtime_mod.__file__).read_text()
    handler = source.split("await self._snapshot_descendants(retry_when_empty=True)", 1)[1]
    handler = handler.split("    #: One retry for a descendant scan", 1)[0]

    settle = handler.index("await self.settle_stderr()")
    teardown = handler.index('reason="failed init handshake cleanup"')
    assert settle < teardown, (
        "the failed-handshake cleanup tears the runtime down before draining "
        "stderr, so the child's own account of why it could not start is discarded "
        "unread"
    )


@pytest.mark.asyncio
async def test_a_cancelled_drain_is_what_the_early_settle_prevents():
    """The mechanism, driven: a cancelled drain consumes nothing.

    This is why settling at the CALLER is not enough. End the task the way the
    cleanup teardown does, and the latch stays false for a line that was in the
    pipe -- ``settle_stderr`` then returns at once because the task is done.
    """
    runtime = _runtime(HARNESS_SANDBOX_STDERR)
    runtime._stderr_task = asyncio.ensure_future(runtime._drain_stderr())
    runtime._stderr_task.cancel()
    try:
        await runtime._stderr_task
    except asyncio.CancelledError:
        pass

    await runtime.settle_stderr()
    assert runtime.saw_sandbox_init_failure() is False, (
        "precondition: a drain that never ran must consume nothing, or this test "
        "is not about the ordering it exists to justify"
    )


# -- Corroboration classifies the stderr LINES, not the display summary --


@pytest.mark.asyncio
async def test_the_summary_shape_does_not_hide_the_launcher_line():
    """The display string and the classified string are different shapes.

    The shared runtime's retained summary folds the tail onto ONE line behind a
    ``returncode`` prefix, for a person to read. The launcher's refusal is
    recognised per LINE, so classifying that summary matches nothing --
    corroboration would silently never run and the switch would be unreachable on
    the very path the Linux launcher case arrives by.
    """
    summary = (
        "runtime failed [returncode=1] stderr_tail: sandbox: BLOCKED -- "
        "unshare(NEWUSER) failed: errno 1"
    )
    lines = "\n".join(LAUNCHER_STDERR)

    with patch(
        "kiro_crew.acp.client.corroborate_launcher_refusal",
        return_value=("no_backend", "sandbox: BLOCKED", ""),
    ) as corroborate:
        folded = await sandbox_init_failure(summary, crew_wrap=True)
    corroborate.assert_not_called()
    assert "config set agent.sandbox off" not in str(folded)

    with patch(
        "kiro_crew.acp.client.corroborate_launcher_refusal",
        return_value=("no_backend", "sandbox: BLOCKED", ""),
    ) as corroborate:
        split = await sandbox_init_failure(summary, crew_wrap=True, corroboration_output=lines)
    corroborate.assert_called_once()
    assert "config set agent.sandbox off" in str(split)
    # The DISPLAYED detail is still the summary the operator reads.
    assert "returncode=1" in str(split)


@pytest.mark.asyncio
async def test_the_shared_translation_passes_the_lines_for_classification():
    """Wiring: the runtime's per-line reader is what reaches corroboration."""
    from kiro_crew.acp.client import sandbox_init_failure_for_runtime

    runtime = _startup_runtime(latched=True, crew_wrap=True)
    runtime.redacted_stderr_tail.return_value = "\n".join(LAUNCHER_STDERR)

    with patch(
        "kiro_crew.acp.client.corroborate_launcher_refusal",
        return_value=("no_backend", "sandbox: BLOCKED", ""),
    ) as corroborate:
        classified = await sandbox_init_failure_for_runtime(runtime)

    corroborate.assert_called_once()
    assert LAUNCHER_STDERR[0] in corroborate.call_args.args[0]
    assert "config set agent.sandbox off" in str(classified)


@pytest.mark.asyncio
async def test_the_runtime_tail_is_redacted_and_per_line():
    """The reader itself: lines as lines, secrets scrubbed, empty when nothing kept."""
    runtime = _runtime(LAUNCHER_STDERR + ["Authorization: Bearer abcdef1234567890"])
    await runtime._drain_stderr()

    tail = runtime.redacted_stderr_tail()
    assert LAUNCHER_STDERR[0] in tail.splitlines()
    assert "abcdef1234567890" not in tail

    runtime._stderr_lines = []
    assert runtime.redacted_stderr_tail() == ""
