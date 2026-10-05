"""The launch of every known harness matches the committed golden.

One file, one question: for each id in ``ACP_BACKENDS_KNOWN``, does
``AcpClient._spawn`` still hand the process factory the same argv, log the spawn
under the same label, drain stderr under the same label, and add the same
environment variables it did before? A change that moves where those are COMPUTED
must not move what they ARE -- for kiro-cli above all, whose construction path
harness-parity H13 keeps free of work added for an adapter.

Three values in the fixture are placeholders, because they are properties of the
host or the run rather than of the harness: the augmented ``PATH``, the interpreter
path, and pi's per-session gate nonce. That each harness RECEIVES them is what is
pinned; their values are not.

STRICTLY READ-ONLY. Nothing here writes the fixture, and nothing here writes
anywhere in the repository (AUTOSDE ``no-test-side-effects``). The writer is
``scripts/update_acp_launch_goldens.py``; run it only when a launch fact is meant to
change, and commit the rewritten fixture with that change so the fixture diff is what
shows a reviewer which harness moved. The capture both share is
``test/acp_launch_capture.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import sys
import textwrap
from collections.abc import MutableMapping
from pathlib import Path
from types import SimpleNamespace

import acp_launch_capture as capture_mod
import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_KNOWN,
)


@pytest.mark.parametrize(
    "backend",
    sorted(ACP_BACKENDS_KNOWN),
    ids=lambda b: b or "kiro",
)
def test_the_launch_of_every_known_harness_matches_the_golden(backend, tmp_path) -> None:
    """One harness per case, so a failure names the harness that moved."""
    golden = capture_mod.read_golden()
    key = capture_mod.golden_key(backend)
    assert key in golden, (
        f"{key} is a known backend with no golden entry -- regenerate with "
        "python3 scripts/update_acp_launch_goldens.py"
    )
    assert capture_mod.capture(backend, tmp_path) == golden[key]


#: Two operator defaults, at values no recording host carries.
_CHILD_ENV_DEFAULTS = {"TOKIO_WORKER_THREADS": "3", "RAYON_NUM_THREADS": "5"}


def _child_env_default_patches() -> tuple:
    """Run the env hop's own body, with its credential and ``/tmp`` reads answered
    here, and hand ``agent.child_env_defaults`` the two values above."""
    from unittest.mock import patch

    from kiro_crew.acp import child_env_defaults as child_env_mod

    return (
        patch.object(client_mod, "_resolve_spawn_env", new=client_mod._resolve_spawn_env),
        patch.object(client_mod, "_resolve_ssh_auth_sock", new=lambda env: None),
        patch.object(client_mod, "resolve_krb5_ccname", new=lambda env: None),
        patch("kiro_crew.config.loader.inject_kiro_cli_api_key", new=lambda env: None),
        patch("kiro_crew.config.loader.strip_kiro_cli_api_key", new=lambda env: None),
        patch.object(child_env_mod, "_configured_defaults", new=lambda: dict(_CHILD_ENV_DEFAULTS)),
    )


def _kiro_family_runtime_gate_patches(tmp_path: Path) -> tuple:
    """kiro-cli's own pre-spawn gates on the runtime path, answered the way the
    client capture answers them, so a kiro-family runtime launch reads and writes
    nothing outside *tmp_path*: its binary, agent materialization, governance and
    freshness checks, skill projection and voice workspace guards."""
    from unittest.mock import AsyncMock, patch

    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    return (
        patch.object(
            client_mod,
            "_resolve_kiro_bin_for_spawn",
            new=AsyncMock(return_value=capture_mod._KIRO_BIN),
        ),
        patch("kiro_crew.agent.ensure_agent_materialized", return_value=None),
        patch.object(runtime_mod, "ensure_agent_materialized", return_value=None),
        patch("kiro_crew.agent.require_fork_governance", return_value=None),
        patch("kiro_crew.agent.require_fresh_derived_spec", return_value=None),
        patch("kiro_crew.sandbox.delegated_workspace_exposes_sealed_target", return_value=None),
        patch(
            "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
            return_value=NativeSkillProjection({"kirocrew": "kirocrew-skill-view-golden"}),
        ),
        patch.object(
            runtime_mod, "assert_voice_runtime_outside_agent_workspace", return_value=None
        ),
        patch.object(
            runtime_mod,
            "bind_voice_safe_agent_workspace_async",
            new=AsyncMock(return_value=(str(tmp_path), None)),
        ),
    )


def _received_defaults(answers: dict) -> dict:
    return {key: answers["env_added"].get(key) for key in _CHILD_ENV_DEFAULTS}


@pytest.mark.parametrize(
    "backend",
    sorted(ACP_BACKENDS_KNOWN - capture_mod.RUNTIME_ONLY_BACKENDS),
    ids=lambda b: b or "kiro",
)
def test_the_client_launch_hands_child_env_defaults_to_kiro_cli_alone(backend, tmp_path) -> None:
    """``agent.child_env_defaults`` reaches a client-launched child only for kiro-cli.

    Through ``AcpClient``'s real launch, with the env hop's own body run rather than
    passed through: a launch that stopped handing that hop the kiro-cli answer drops
    the defaults, and a per-process host (or KAS, which the client starts as a plain
    kiro-cli fall-through) that started reaching it as kiro-cli gains them.
    """
    answers = capture_mod.capture(backend, tmp_path, extra_patches=_child_env_default_patches())
    expected = (
        _CHILD_ENV_DEFAULTS if backend == ACP_BACKEND_KIRO else dict.fromkeys(_CHILD_ENV_DEFAULTS)
    )
    assert _received_defaults(answers) == expected


@pytest.mark.parametrize(
    "backend",
    sorted(ACP_BACKENDS_ACP_RUNTIME),
    ids=lambda b: b or "kiro",
)
def test_the_runtime_launch_hands_child_env_defaults_to_the_kiro_family(backend, tmp_path) -> None:
    """Through ``AcpRuntime``'s real launch the kiro-family harnesses carry them, codex not."""
    answers = capture_mod._capture_runtime_served(
        backend,
        tmp_path,
        capture_mod.fixed_parent_env(),
        _child_env_default_patches() + _kiro_family_runtime_gate_patches(tmp_path),
    )
    expected = (
        _CHILD_ENV_DEFAULTS
        if backend in {ACP_BACKEND_KIRO, ACP_BACKEND_KAS}
        else dict.fromkeys(_CHILD_ENV_DEFAULTS)
    )
    assert _received_defaults(answers) == expected


def test_the_golden_covers_exactly_the_known_backends() -> None:
    """A harness added without a golden entry, or one left behind, is a gap."""
    golden = capture_mod.read_golden()
    expected = {capture_mod.golden_key(backend) for backend in ACP_BACKENDS_KNOWN}
    assert set(golden) == expected


def test_no_golden_entry_carries_a_host_path() -> None:
    """The snapshot is a property of the code, so the recording host must not show.

    Checked against the file rather than a fresh capture: a value that leaked once
    stays in the fixture until someone looks, and this is the look.
    """
    body = capture_mod.GOLDEN_PATH.read_text(encoding="utf-8")
    for marker in (str(Path.home()), os.environ.get("USER") or "\0", "/tmp/pytest"):
        assert marker not in body, f"the golden file carries {marker!r} from a host"


def test_the_fixture_is_written_in_the_shared_spelling() -> None:
    """A rewrite must show as a CONTENT diff, never as a reformatting one.

    The writer renders through ``capture_mod.render``; this pins that the committed
    file is what that function produces, so a hand-edit or a differently-indented
    rewrite is caught here rather than showing up as noise in an unrelated diff.
    """
    assert capture_mod.GOLDEN_PATH.read_text(encoding="utf-8") == capture_mod.render(
        capture_mod.read_golden()
    )


def test_a_capture_leaves_every_resolver_cache_as_it_found_it(tmp_path) -> None:
    """The capture must not leak its synthetic binaries into the worker.

    ``_spawn``'s resolution ladders cache their answer in a module global for the
    life of the process, and a capture stubs those resolvers -- so driving the real
    spawn WRITES this file's ``/opt/bin/...`` fiction into a cache the rest of the
    suite shares. A later test that reads a cache it did not seed would then pass or
    fail on that fiction.

    Every cache the capture touches is checked, adapters and self-served alike, and
    the pre-capture state is read here rather than assumed: an earlier test in the
    same worker may legitimately have left a real resolution in place, and the
    contract is "as it found it", not "empty".
    """
    before = capture_mod.snapshot_bin_caches()

    capture_mod.capture(ACP_BACKEND_OPENCODE, tmp_path / "one")
    capture_mod.capture(ACP_BACKEND_KIRO, tmp_path / "two")

    assert capture_mod.snapshot_bin_caches() == before

    # Two guards so the equality above cannot pass vacuously. The adapter caches are
    # named, because those exist however the self-served resolution is spelled; and
    # the snapshot must carry MORE than them, which is what says the self-served
    # state is covered too without this test naming the shape that holds it.
    for name, module in capture_mod._ADAPTER_CACHES.items():
        assert getattr(module, name) is before[name], f"{name} was not restored"
    assert set(before) > set(capture_mod._ADAPTER_CACHE_NAMES), (
        "the snapshot covers only the adapter caches, so the self-served resolution "
        "is being left as the capture set it"
    )


def test_the_deepseek_capture_writes_its_scratch_windows_only_under_tmp_path(
    monkeypatch, tmp_path
) -> None:
    """Every filesystem write the DeepSeek arm makes lands under the test's temp dir.

    The arm is the one harness whose spawn path WRITES into the scratch window it is
    handed rather than only naming it: ``record_owner`` installs the child's pid as
    ``.owner`` in the session window, and the gate probe's throwaway window is
    ``rmtree``'d in the arm's ``finally`` -- both from an executor thread, so neither
    is attributable to this test by frame. When the capture answered
    ``allocate_scratch`` with a fixed synthetic ``/opt/scratch/...`` path, both became
    writes at a real absolute path on the recording host, outside every sandbox.

    The two writers are wrapped at the names the arm looks up, so a stub that ever
    again answers a path outside ``tmp_path`` fails here with the path named, instead
    of leaving an ``unlink`` and an ``rmtree`` aimed at someone's disk. And the owner
    write must be RECORDED, not merely attempted: an absent directory answers
    ``"unwritable"``, which is exactly what the host path did.
    """
    touched: list[Path] = []
    outcomes: list[str] = []
    real_record_owner = client_mod.agent_scratch.record_owner
    real_rmtree = client_mod.shutil.rmtree

    def _record_owner(path, pid):
        touched.append(Path(path))
        outcome = real_record_owner(path, pid)
        outcomes.append(outcome)
        return outcome

    def _rmtree(path, *args, **kwargs):
        touched.append(Path(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(client_mod.agent_scratch, "record_owner", _record_owner)
    monkeypatch.setattr(client_mod.shutil, "rmtree", _rmtree)

    capture_mod.capture(ACP_BACKEND_DEEPSEEK, tmp_path)

    assert len(touched) >= 2, (
        "the DeepSeek arm neither recorded a scratch owner nor removed its probe "
        f"window, so this test measured nothing: {touched}"
    )
    outside = [path for path in touched if not path.is_relative_to(tmp_path)]
    assert not outside, f"the capture wrote outside its own tmp_path: {outside}"
    assert outcomes == ["recorded"], outcomes


def test_the_env_delta_does_not_depend_on_the_recording_environment(monkeypatch, tmp_path) -> None:
    """The fixture must record what _spawn CONTRIBUTES, not what this host lacked.

    ``env_added`` is a delta, and measuring it against the ambient environment made it
    a property of the recording process: a host that already exported a variable
    ``_spawn`` also sets saw no difference and recorded no key, while a clean runner
    recorded one. That is what made the committed fixture disagree with a Windows
    runner on ``KIROCREW_SPAWNED`` -- Crew exports it into every agent it spawns, so a
    capture taken from inside an agent could never observe ``_spawn`` setting it.

    Driven in BOTH directions against the same backend: once with those variables
    absent from the ambient environment, once with them present and set to the very
    values ``_spawn`` would write. A capture that reads the ambient environment
    answers differently in the two cases; one that pins its parent cannot.
    """
    marker_keys = {
        "KIROCREW_SPAWNED": "1",
        "KIROCREW_SESSION_KEY": "golden-session",
        "KIROCREW_RUNTIME_PYTHON": sys.executable,
    }

    for key in marker_keys:
        monkeypatch.delenv(key, raising=False)
    absent_ambient = capture_mod.capture(ACP_BACKEND_KIRO, tmp_path / "absent")

    for key, value in marker_keys.items():
        monkeypatch.setenv(key, value)
    present_ambient = capture_mod.capture(ACP_BACKEND_KIRO, tmp_path / "present")

    assert absent_ambient == present_ambient, (
        "the capture reads the ambient environment, so what it records depends on the "
        "machine that recorded it"
    )
    # And the key that caused the real failure is present in BOTH, rather than only in
    # the run where the ambient environment happened to lack it.
    for answer in (absent_ambient, present_ambient):
        assert answer["env_added"].get("KIROCREW_SPAWNED") == "1"


def test_the_fixed_parent_never_carries_a_variable_spawn_sets() -> None:
    """The pass-through allowlist must not be able to hide a contributed key again.

    The fix pins the parent environment but carries some OS variables through from the
    host, because Windows cannot resolve a home or temp directory without them. That
    list is only safe while it names nothing ``_spawn`` writes -- otherwise a
    carried-through value would mask a contribution exactly as the ambient
    environment did.
    """
    contributed = {
        "KIROCREW_SPAWNED",
        "KIROCREW_SPAWN_INSTANCE",
        "KIROCREW_SESSION_KEY",
        "KIROCREW_CHANNEL_ID",
        "KIROCREW_RUNTIME_PYTHON",
        "PI_ACP_PI_COMMAND",
        "KIROCREW_PI_GATE_SESSION",
        "OPENCODE_CONFIG_CONTENT",
        "GOOSE_MODE",
        "DSH_PERMISSION_MODE",
        "CLAUDE_CODE_EXECUTABLE",
    }
    overlap = contributed & set(capture_mod._PASSTHROUGH_ENV_KEYS)
    assert not overlap, f"the pass-through list can mask a contributed key: {overlap}"


class _WindowsLikeEnviron(MutableMapping):
    """``os.environ`` as Windows presents it: variable names folded to upper case.

    A plain ``dict`` subclass will not do -- ``dict.update`` and the ``{**d}`` splat
    take a C-level fast path that never calls ``__setitem__``, so the fold would not
    apply. A ``MutableMapping`` routes every write through ``__setitem__``, which is
    what makes ``patch.dict(..., clear=True)`` and ``dict(os.environ)`` fold here the
    way they do on a real Windows host.
    """

    def __init__(self, data=None) -> None:
        self._data: dict[str, str] = {}
        for key, value in dict(data or {}).items():
            self[key] = value

    def __setitem__(self, key, value) -> None:
        self._data[key.upper()] = value

    def __getitem__(self, key):
        return self._data[key.upper()]

    def __delitem__(self, key) -> None:
        del self._data[key.upper()]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def copy(self) -> "_WindowsLikeEnviron":
        return _WindowsLikeEnviron(self._data)


def test_a_windows_case_fold_reports_no_phantom_removal(monkeypatch, tmp_path) -> None:
    """A name the child inherits under a folded case is not recorded as removed.

    On Windows ``os.environ`` folds every variable name to one case, so a name the
    host exports as ``SystemRoot`` reaches the child as ``SYSTEMROOT``. Measuring the
    delta against the pre-roundtrip parent -- which still holds the mixed-case
    ``SystemRoot`` the pass-through allowlist read back -- reports that name as
    removed, which is the Windows-only golden failure this pins. The fold is simulated
    with a case-insensitive ``os.environ`` rather than by flipping ``os.name``, which
    would turn ``pathlib`` into ``WindowsPath`` on this host.
    """
    folded = _WindowsLikeEnviron(os.environ)
    # The host variable whose mixed case ``fixed_parent_env`` reads back, and which the
    # fold then stores under a single name the child inherits.
    folded["SystemRoot"] = r"C:\Windows"
    monkeypatch.setattr(os, "environ", folded)

    answer = capture_mod.capture(ACP_BACKEND_CODEX, tmp_path)

    assert answer["env_removed"] == [], (
        "a name the child inherited under a folded case was reported as removed: "
        f"{answer['env_removed']}"
    )


def test_the_delta_still_reports_a_variable_the_launch_genuinely_dropped() -> None:
    """The fold fix must not blanket-suppress removals -- a dropped name still shows.

    Measuring against the inherited parent fixes the phantom Windows removal; it must
    not hide a real one. A name present in the parent and absent from the child is
    still reported, so the golden keeps catching a launch that strips the environment.
    """
    _env_delta = capture_mod._env_delta
    _added, removed = _env_delta({"KEEP": "1", "DROPPED": "2"}, {"KEEP": "1"})
    assert removed == ["DROPPED"]
    # And a case-sensitive POSIX difference is a real removal, not folded away.
    _added, removed = _env_delta({"Path": "x"}, {"PATH": "x"})
    assert removed == ["Path"]


def test_a_derived_stub_accepts_exactly_what_the_real_collaborator_accepts() -> None:
    """A stub's accepted arguments must track its target's, in BOTH directions.

    A stub narrower than the thing it stubs rejects a call the spawn path really makes,
    which fails every case here for a reason that is about this file rather than about
    any harness. A stub wider than it -- ``**kwargs`` -- accepts a keyword the target
    does not have, so an argument could reach the real collaborator only in production
    and never be measured here. Both are pinned, so neither shape can pass.
    """

    def target(env, *, flavour: bool = False):
        raise AssertionError("the capture must never run the real collaborator")

    stub = capture_mod._stub_for(target, lambda call: call["env"])

    assert stub({"A": "1"}, flavour=True) == {"A": "1"}
    with pytest.raises(TypeError):
        stub({"A": "1"}, nonesuch=True)


def test_every_passthrough_stub_answers_with_an_argument_its_target_has() -> None:
    """Each entry must name a parameter the live collaborator really declares.

    The derivation makes a stub follow its target's signature, which leaves one way to
    get the pairing wrong: answering with a name the target does not have. That stub
    accepts the call and then cannot answer it, so it is checked here against the live
    object rather than against a spelling in this file.
    """
    entries = {**capture_mod._PASSTHROUGH_STUBS, **capture_mod._ASYNC_PASSTHROUGH_STUBS}
    assert entries, "the registry is empty, so this test checks nothing"

    for name, answer in sorted(entries.items()):
        real = getattr(client_mod, name)
        call = {}
        for param in inspect.signature(real).parameters.values():
            if param.default is not inspect.Parameter.empty:
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            # argv is answered by handing the list back, so it has to be a list; every
            # other required argument is only carried, and its value is never read.
            call[param.name] = ["/opt/bin/x"] if param.name == "argv" else {"A": "1"}
        try:
            capture_mod._stub_for(real, answer)(**call)
        except KeyError as exc:
            raise AssertionError(
                f"{name} is answered with {exc}, which it does not declare"
            ) from exc


# Referenced so the ids above are not the only use of the vocabulary this file
# reads; a rename of any one of them fails here rather than silently narrowing
# the parametrisation.
_KNOWN_IDS = (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_KAS,
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_DEEPSEEK,
)


# ── The launch tail, through its own interface ──
#
# ``launch()`` is the one tail both drivers hand their plan to, so it is tested here
# directly: a fake host carrying the driver state the tail writes, and fake tools in
# place of every OS-facing collaborator. Each test asserts what reaches the child (its
# argv, its environment, the sandbox wrap's arguments) or what the tail leaves on the
# host, never how the tail spells it.


#: Variables the tail itself writes on the child, cleared from the test's environment so
#: a host that already exported one (an agent's own shell does) cannot mask its write.
_TAIL_WRITTEN_ENV = (
    "KIROCREW_SPAWNED",
    "KIROCREW_RUNTIME_PYTHON",
    "KIROCREW_SCRATCH",
    "PYTEST_XDIST_AUTO_NUM_WORKERS",
    "AGENT_BROWSER_SESSION",
)


def _launch_env(monkeypatch, **values: str) -> None:
    """The gateway environment a tail test launches from, changed key by key.

    The conftest-pinned data home and the rest of the environment stay in place; only
    the keys a test reasons about are set, and the tail's own keys are cleared.
    """
    for key in _TAIL_WRITTEN_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    for key, value in values.items():
        monkeypatch.setenv(key, value)


class _FakeHost:
    """The driver state :func:`launch` writes, and the three driver helpers it calls."""

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = work_dir
        self._scratch_dir: Path | None = None
        self._shared_scratch: Path | None = None
        self._sandbox_cleanup: str | None = None
        self._sandbox_wrapped_by_crew = False
        self._sandbox_hidden_dirs: tuple[str, ...] = ()
        self._spawn_work_dir = str(work_dir)
        self._bound_workspace_fd: int | None = None
        self.guarded: list[str] = []
        self.workspace_discards = 0
        self.cleanup_discards = 0

    async def _discard_bound_workspace(self) -> None:
        self.workspace_discards += 1

    def _discard_sandbox_cleanup(self) -> None:
        self.cleanup_discards += 1

    async def _to_thread_guarding_sandbox(self, fn, /, *args, **kwargs):
        self.guarded.append(getattr(fn, "__name__", repr(fn)))
        return fn(*args, **kwargs)


class _Scratch:
    """``agent_scratch`` as the tail reads it: allocation, validation, and the env."""

    class ScratchBoundaryError(Exception):
        """The refusal the tail treats like an OSError at allocation."""

    def __init__(self, allocated: Path | None, *, fail: bool = False) -> None:
        self._allocated = allocated
        self._fail = fail
        self.allocated_labels: list[str] = []
        self.validated: list[Path] = []

    def allocate_scratch(self, label: str) -> Path | None:
        self.allocated_labels.append(label)
        if self._fail:
            raise OSError("no scratch root")
        return self._allocated

    def shared_scratch_window(self, path: Path) -> Path | None:
        self.validated.append(path)
        return path

    def scratch_env(self, path: Path, shared: Path | None = None) -> dict[str, str]:
        return {"KIROCREW_SCRATCH": str(path), "TMPDIR": str(path)}


class _Recording:
    """Every collaborator call the tail makes, in the order it made them."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.wrap_kwargs: dict = {}
        self.factory_argv: list[str] = []
        self.factory_kwargs: dict = {}
        self.socket_env_arg: dict | None = None


def _launch_tools(
    rec: _Recording,
    *,
    scratch: _Scratch,
    delegate: bool = False,
    browser_env: dict[str, str] | None = None,
    spawn_error: BaseException | None = None,
    retrying=None,
):
    from kiro_crew.acp.launch import LaunchTools

    platform = SimpleNamespace(
        IS_POSIX=True,
        CREATE_NEW_PROCESS_GROUP=0,
        _SUBPROCESS_NO_WINDOW=0,
        CREATE_SUSPENDED=0,
    )

    async def _owned_process(factory):
        rec.calls.append("create_windows_cleanup_owned_process")
        return await factory()

    platform.create_windows_cleanup_owned_process = _owned_process

    def _pod_bundle(argv, *, backend):
        rec.calls.append("apply_pod_bundle_spawn")
        return list(argv), delegate

    async def _wrap_async(argv, **kwargs):
        rec.calls.append("wrap_argv_async")
        rec.wrap_kwargs = kwargs
        return ["sandbox", *argv], "/run/launcher.sh"

    def _scrub(env, *, forward_ssh_auth_sock=False):
        rec.calls.append("scrub_agent_subprocess_env")
        return {key: value for key, value in env.items() if key != "SECRET_TOKEN"}

    def _pod_remap(env, *, pod_home_remap):
        rec.calls.append(f"apply_pod_home_remap:{pod_home_remap}")
        return dict(env, POD_REMAP=str(pod_home_remap))

    def _browser_session(env):
        rec.calls.append("browser_session_env")
        return dict(browser_env or {})

    def _browser_socket(env):
        rec.calls.append("browser_socket_env")
        rec.socket_env_arg = dict(env)
        return {"BROWSER_SOCKET": "sock"}

    def _xdist(env):
        rec.calls.append("inject_xdist_auto_cap")
        env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = "2"

    async def _bind(work_dir):
        rec.calls.append("bind_voice_safe_agent_workspace_async")
        return str(work_dir), 7

    async def _create(*argv, **kwargs):
        rec.calls.append("create_subprocess_limited")
        rec.factory_argv = list(argv)
        rec.factory_kwargs = kwargs
        if spawn_error is not None:
            raise spawn_error
        return SimpleNamespace(pid=99_999_999_999)

    return LaunchTools(
        logger=logging.getLogger("kiro_crew.acp.client"),
        platform_compat=platform,
        agent_scratch=scratch,
        apply_pod_bundle_spawn=_pod_bundle,
        forward_ssh_auth_sock=lambda: True,
        wrap_argv_async=_wrap_async,
        wrap_argv=lambda *a, **k: None,
        wrapped_by_crew_sandbox=lambda argv: argv[:1] == ["sandbox"],
        cgroup_scope_argv=lambda argv: ["scope", *argv],
        augmented_path=lambda path: f"{path}:/augmented",
        scrub_agent_subprocess_env=_scrub,
        apply_pod_home_remap=_pod_remap,
        browser_session_env=_browser_session,
        browser_socket_env=_browser_socket,
        inject_xdist_auto_cap=_xdist,
        bind_voice_safe_agent_workspace_async=_bind,
        create_subprocess_limited=_create,
        retrying_spawn_factory=retrying,
    )


def _request(**overrides):
    from kiro_crew.acp.launch import LaunchRequest

    async def _before(env, spawned_binary):
        env["BEFORE_SCRUB"] = spawned_binary or ""
        return env

    fields = {
        "argv": ["/opt/bin/host", "acp"],
        "backend": "host",
        "sandbox_mode": "auto",
        "extra_env": {"EXTRA": "1"},
        "scratch_label": "session",
        "env_before_scrub": _before,
    }
    fields.update(overrides)
    return LaunchRequest(**fields)


def _run_launch(host, request, tools):
    from kiro_crew.acp.launch import launch

    return asyncio.run(launch(host, request, tools))


def test_the_tail_hands_the_plan_mask_and_the_scratch_window_to_the_sandbox(
    monkeypatch, tmp_path
) -> None:
    """The mask is APPLIED, not merely carried: it is what the wrap is called with.

    A plan whose mask never reached the wrap would spawn an enforced host unmasked and
    still look right in every adapter test; a scratch window the wrap never saw stays
    masked, and the child gets no temp of its own.
    """
    _launch_env(monkeypatch)
    host = _FakeHost(tmp_path)
    scratch = _Scratch(tmp_path / "scratch")
    rec = _Recording()

    launched = _run_launch(
        host,
        _request(extra_hidden_dirs=("/srv/creds/.aws",), extra_expose_files=("/srv/creds/.x",)),
        _launch_tools(rec, scratch=scratch, delegate=True),
    )

    assert rec.wrap_kwargs["extra_hidden_dirs"] == ("/srv/creds/.aws",)
    assert rec.wrap_kwargs["extra_expose_files"] == ("/srv/creds/.x",)
    assert rec.wrap_kwargs["extra_private_dirs"] == (str(tmp_path / "scratch"),)
    # The delegation verdict is the pod bundle step's, passed through unchanged.
    assert rec.wrap_kwargs["is_kiro_cli"] is True
    assert rec.wrap_kwargs["forward_ssh_auth_sock"] is True
    assert rec.wrap_kwargs["strip_python_env"] is True
    # What the host keeps from the wrap: the launcher to reclaim, the layer it got.
    assert host._sandbox_cleanup == "/run/launcher.sh"
    assert host._sandbox_wrapped_by_crew is True
    assert host._sandbox_hidden_dirs == ("/srv/creds/.aws",)
    # The cgroup scope is OUTERMOST, around the sandboxed command.
    assert rec.factory_argv == ["scope", "sandbox", "/opt/bin/host", "acp"]
    assert launched.scope_unit == ""


def test_the_child_environment_is_built_in_one_fixed_order(monkeypatch, tmp_path) -> None:
    """Each driver step sees exactly what the tail has built before it, and no more.

    The order is the contract: the scrub runs AFTER the driver's credential repair so
    no resolver reintroduces a denied variable, the interpreter is pinned after the
    scrub and after ``extra_env`` so configuration cannot redirect it, and the driver
    marks its child after the orphan-sweep marker it is paired with.
    """
    _launch_env(monkeypatch, SECRET_TOKEN="x")
    seen: dict[str, dict[str, str]] = {}

    async def _before(env, spawned_binary):
        seen["before_scrub"] = dict(env)
        env["REPAIRED"] = spawned_binary or ""
        return env

    def _step(name):
        def _record(env):
            seen[name] = dict(env)

        return _record

    host = _FakeHost(tmp_path)
    rec = _Recording()
    _run_launch(
        host,
        _request(
            env_before_scrub=_before,
            env_after_scrub=_step("after_scrub"),
            env_after_marker=_step("after_marker"),
            env_after_scratch=_step("after_scratch"),
            pod_home_remap=True,
        ),
        _launch_tools(rec, scratch=_Scratch(tmp_path / "scratch"), browser_env={"B": "kc-1"}),
    )

    before = seen["before_scrub"]
    assert before["PATH"] == "/usr/bin:/augmented" and before["EXTRA"] == "1"
    assert before["SECRET_TOKEN"] == "x", "the driver step runs before the scrub"
    after_scrub = seen["after_scrub"]
    assert "SECRET_TOKEN" not in after_scrub and after_scrub["REPAIRED"] == "/opt/bin/host"
    assert after_scrub["KIROCREW_RUNTIME_PYTHON"] == sys.executable
    assert "POD_REMAP" not in after_scrub and "KIROCREW_SPAWNED" not in after_scrub
    after_marker = seen["after_marker"]
    assert after_marker["POD_REMAP"] == "True" and after_marker["KIROCREW_SPAWNED"] == "1"
    assert "B" not in after_marker
    after_scratch = seen["after_scratch"]
    assert after_scratch["B"] == "kc-1"
    assert after_scratch["KIROCREW_SCRATCH"] == str(tmp_path / "scratch")
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" not in after_scratch
    child = rec.factory_kwargs["env"]
    assert child["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "2"
    assert rec.calls.index("scrub_agent_subprocess_env") < rec.calls.index(
        "apply_pod_home_remap:True"
    )


def test_the_browser_session_reaches_the_child_with_its_socket_env(monkeypatch, tmp_path) -> None:
    """Each agent process gets its own named browser session, and its lifecycle socket.

    Without the name, two agents' nameless browser commands resolve to one shared
    ``default`` browser and navigate and close each other's pages. The socket env is
    resolved from the gateway's environment plus that name, off the loop through the
    guarded hop, because it reads the filesystem while the sandbox launcher is live.
    """
    _launch_env(monkeypatch, GATEWAY="g")
    host = _FakeHost(tmp_path)
    rec = _Recording()
    _run_launch(
        host,
        _request(),
        _launch_tools(rec, scratch=_Scratch(None), browser_env={"AGENT_BROWSER_SESSION": "kc-7"}),
    )

    child = rec.factory_kwargs["env"]
    assert child["AGENT_BROWSER_SESSION"] == "kc-7"
    assert child["BROWSER_SOCKET"] == "sock"
    lookup = rec.socket_env_arg
    assert lookup is not None
    assert (lookup["GATEWAY"], lookup["AGENT_BROWSER_SESSION"]) == ("g", "kc-7")
    # The GATEWAY's environment plus the name, never the child's: neither the session
    # overlay nor anything the tail wrote on the child reaches the lookup.
    assert "EXTRA" not in lookup and "KIROCREW_SPAWNED" not in lookup
    assert "_browser_socket" in host.guarded


def test_no_browser_session_means_no_socket_lookup(monkeypatch, tmp_path) -> None:
    _launch_env(monkeypatch)
    rec = _Recording()
    _run_launch(_FakeHost(tmp_path), _request(), _launch_tools(rec, scratch=_Scratch(None)))
    assert "browser_socket_env" not in rec.calls
    assert "BROWSER_SOCKET" not in rec.factory_kwargs["env"]


def test_a_failed_spawn_releases_the_workspace_and_the_sandbox_launcher(
    monkeypatch, tmp_path
) -> None:
    """The process never existed, so nothing else will reclaim what the tail built."""
    _launch_env(monkeypatch)
    for error in (OSError("exec failed"), asyncio.CancelledError()):
        host = _FakeHost(tmp_path)
        with pytest.raises(type(error)):
            _run_launch(
                host,
                _request(),
                _launch_tools(_Recording(), scratch=_Scratch(None), spawn_error=error),
            )
        # One discard before the bind, one for the failure.
        assert host.workspace_discards == 2
        assert host.cleanup_discards == 1


def test_a_respawn_joins_the_tree_its_previous_process_staged(monkeypatch, tmp_path) -> None:
    """A respawn exposes the previous process's directory instead of hiding its work."""
    _launch_env(monkeypatch)
    previous = tmp_path / "previous"
    host = _FakeHost(tmp_path)
    host._scratch_dir = previous
    scratch = _Scratch(tmp_path / "fresh")
    rec = _Recording()
    _run_launch(host, _request(), _launch_tools(rec, scratch=scratch))

    assert host._shared_scratch == previous and scratch.validated == [previous]
    assert host._scratch_dir == tmp_path / "fresh"
    assert rec.wrap_kwargs["extra_private_dirs"] == (str(tmp_path / "fresh"), str(previous))


def test_a_scratch_that_cannot_be_allocated_spawns_with_the_inherited_temp(
    monkeypatch, tmp_path, caplog
) -> None:
    _launch_env(monkeypatch)
    host = _FakeHost(tmp_path)
    rec = _Recording()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
        _run_launch(host, _request(), _launch_tools(rec, scratch=_Scratch(None, fail=True)))
    assert host._scratch_dir is None
    assert rec.wrap_kwargs["extra_private_dirs"] == ()
    assert "KIROCREW_SCRATCH" not in rec.factory_kwargs["env"]
    assert any("could not allocate" in r.getMessage() for r in caplog.records)


def test_only_the_guarding_driver_allocates_scratch_through_its_guarded_hop(
    monkeypatch, tmp_path
) -> None:
    """The runtime has always allocated through its guarded hop; the client never has."""
    _launch_env(monkeypatch)
    for guarded in (False, True):
        host = _FakeHost(tmp_path)
        scratch = _Scratch(tmp_path / "s")
        _run_launch(
            host,
            _request(guard_scratch_hops=guarded, scratch_label="runtime"),
            _launch_tools(_Recording(), scratch=scratch),
        )
        assert ("allocate_scratch" in host.guarded) is guarded
        assert scratch.allocated_labels == ["runtime"]


def test_a_host_with_its_own_sandbox_enters_a_verified_workspace(monkeypatch, tmp_path) -> None:
    _launch_env(monkeypatch)
    for internal in (False, True):
        rec = _Recording()
        _run_launch(
            _FakeHost(tmp_path),
            _request(internal_sandbox=internal),
            _launch_tools(rec, scratch=_Scratch(None)),
        )
        assert ("bind_voice_safe_agent_workspace_async" in rec.calls) is internal
        assert rec.factory_kwargs["chdir_fd"] == (7 if internal else None)


def test_the_driver_names_the_scope_and_the_launch_reports_it(monkeypatch, tmp_path) -> None:
    _launch_env(monkeypatch)
    rec = _Recording()
    launched = _run_launch(
        _FakeHost(tmp_path),
        _request(scope_argv=lambda argv: (["named", *argv], "kirocrew-x.scope")),
        _launch_tools(rec, scratch=_Scratch(None)),
    )
    assert rec.factory_argv[:2] == ["named", "scope"]
    assert launched.scope_unit == "kirocrew-x.scope"


def test_only_a_driver_that_brings_a_retry_retries_the_spawn(monkeypatch, tmp_path) -> None:
    _launch_env(monkeypatch)
    attempts: list[str] = []

    async def _retrying(factory):
        attempts.append("retry-wrapper")
        return await factory()

    rec = _Recording()
    _run_launch(
        _FakeHost(tmp_path),
        _request(),
        _launch_tools(rec, scratch=_Scratch(None), retrying=_retrying),
    )
    assert attempts == ["retry-wrapper"] and rec.calls.count("create_subprocess_limited") == 1


def _per_process_hosts() -> frozenset[str]:
    """The backend ids the client launches through an adapter, read off the sets."""
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ACP_RUNTIME

    return frozenset(ACP_BACKENDS_KNOWN - ACP_BACKENDS_ACP_RUNTIME)


def _host_name_re(hosts: frozenset[str]) -> re.Pattern[str]:
    """An identifier that names one of *hosts*: the bare id (its adapter module),
    its identity test, its backend constant, a host-prefixed helper or field, an
    alias of its adapter module, its adapter class, or a host's vault plumbing."""
    ids = "|".join(sorted(hosts))
    classes = "|".join(sorted(host.capitalize() for host in hosts))
    return re.compile(
        r"^(?:(?:%s)|_?is_(?:%s)|ACP_BACKEND_(?:%s)|_(?:%s)_\w+|(?:%s)_(?:mod|harness|launch)"
        r"|(?:%s)Launch|_vault_secret\w*)$" % (ids, ids, ids.upper(), ids, ids, classes)
    )


def _host_tokens(source: str, hosts: frozenset[str]) -> list[str]:
    """Every CODE token of *source* that names one of *hosts*.

    Read through ``tokenize`` so comments and prose never count: an identifier is
    matched whole against :func:`_host_name_re` (the names in an f-string's fields
    arrive as identifiers too), and a string literal, or one literal part of an
    f-string, counts only when its value IS a host id (``"deepseek"``,
    ``f"{x}deepseek"``) -- which a docstring never is.
    """
    import ast
    import io
    import tokenize

    names = _host_name_re(hosts)
    found: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(textwrap.dedent(source)).readline):
        if token.type == tokenize.NAME and names.match(token.string):
            found.append(token.string)
        elif token.type == tokenize.FSTRING_MIDDLE and token.string in hosts:
            found.append(token.string)
        elif token.type == tokenize.STRING:
            try:
                value = ast.literal_eval(token.string)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, str) and value in hosts:
                found.append(token.string)
    return found


def test_the_shared_launch_code_names_no_per_process_host() -> None:
    """harness-parity H13: the code every host shares carries no host's conditional.

    Kept as a source read on purpose, because the defect has no behavioural signal: a
    branch such as ``if self.backend == "deepseek":`` added to the tail is inert on the
    Kiro path, so no launch of kiro-cli changes and the golden stays green. What a host
    needs at launch belongs in its adapter (``acp/harness/<host>.py``), so the tail, the
    client's spawn and the runtime's spawn must not spell one.
    """
    from kiro_crew.acp import launch as launch_mod
    from kiro_crew.acp import runtime as runtime_mod

    shared = {
        "launch.launch": launch_mod.launch,
        "AcpClient._spawn": client_mod.AcpClient._spawn,
        "acp.client._launch_tools": client_mod._launch_tools,
        "AcpRuntime._spawn_admitted": runtime_mod.AcpRuntime._spawn_admitted,
        "acp.runtime._launch_tools": runtime_mod._launch_tools,
    }
    hosts = _per_process_hosts()
    assert {"claude", "opencode", "goose", "pi", "deepseek"} <= hosts
    named = {
        where: sorted(set(_host_tokens(inspect.getsource(fn), hosts)))
        for where, fn in shared.items()
    }
    assert named == {where: [] for where in shared}


def test_the_host_token_scan_can_fail() -> None:
    hosts = _per_process_hosts()
    for spelling in (
        "if self._is_deepseek:\n    pass\n",
        "if self._deepseek_gate_nonce:\n    pass\n",
        "ok = backend == ACP_BACKEND_PI\n",
        "keys = self._vault_secret_env_keys\n",
        'if self.backend == "deepseek":\n    pass\n',
        "deepseek_mod.after_spawn(self)\n",
        "x = 'opencode'\n",
        "if self.is_deepseek:\n    pass\n",
        "ok = plan.is_pi\n",
        "from kiro_crew.acp.harness import deepseek\n",
        "adapter = deepseek.DeepseekLaunch()\n",
        "adapter = OpencodeLaunch()\n",
        'if self.backend == f"deepseek":\n    pass\n',
        'name = f"{prefix}opencode"\n',
        'name = f"{opencode_mod.x}"\n',
    ):
        assert _host_tokens(spelling, hosts), spelling
    for spelling in (
        "ok = self._is_kiro\n",
        "ok = self.is_kiro_cli\n",
        "ok = backend == ACP_BACKEND_KIRO\n",
        "label = spawn_label\n",
        "pipe = 1  # deepseek and pi need no step here\n",
        'def f():\n    """The deepseek host is launched elsewhere."""\n',
        'name = "kiro"\n',
        "pid = self._process.pid\n",
        "stdin = asyncio.subprocess.PIPE\n",
        'msg = f"{binary} not found (deepseek-free)"\n',
        "adapter = KiroHarness()\n",
    ):
        assert not _host_tokens(spelling, hosts), spelling
