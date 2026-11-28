"""An edition can hide paths from one session's agent process at spawn.

``SandboxPolicy.session_masks`` is asked once per spawn with the session the process
is started for. Its hidden directories join the harness's own mask, and its read-only
directories become live, read-only windows inside them. These tests pin the three
layers that carry the answer: the resolver in the launch tail (what it refuses and
drops), the plan (which windows open, and where), and the two renderers (the namespace
launcher's payload and the Seatbelt profile). The real-kernel proof that two sessions
each see only their own directory is ``test_session_masks_real_sandbox.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import sandbox_plan, sandbox_seatbelt
from kiro_crew.platform import context as context_mod
from kiro_crew.sandbox_plan import BACKEND_NAMESPACE, BACKEND_SEATBELT, PlanHost, SandboxRequest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

HOME = "/srv/u"
STATE = f"{HOME}/.kiro/crew/edition-sessions"


def _plan(backend: str, **request: Any) -> sandbox_plan.ConfinementPlan:
    host = PlanHost(home=HOME, cwd="/work", tier_dirs=(".aws",), uid=1000, gid=1000)
    return sandbox_plan.plan_confinement(
        SandboxRequest(tier="standard", backend=backend, **request), host
    )


class _PerSessionPolicy:
    """Hides the whole state root and hands each session back its own folder."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.asked: list[Any] = []

    def session_masks(self, identity):
        from kiro_crew.platform.interfaces import SessionMasks

        self.asked.append(identity)
        return SessionMasks(
            hidden_dirs=(str(self.state),),
            readonly_dirs=(str(self.state / identity.session_key),),
        )


def _install(monkeypatch: pytest.MonkeyPatch, policy: object, *, applies: bool = True) -> None:
    monkeypatch.setattr(context_mod, "current_context", lambda: SimpleNamespace(sandbox=policy))
    monkeypatch.setattr("kiro_crew.sandbox.credential_mask_applies", lambda mode: applies)


def test_the_default_policy_hides_nothing_per_session() -> None:
    from kiro_crew.platform.defaults import DefaultSandboxPolicy
    from kiro_crew.platform.interfaces import SessionMasks, SpawnIdentity

    assert DefaultSandboxPolicy().session_masks(SpawnIdentity("s1")) == SessionMasks()


def test_each_session_gets_its_own_answer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from kiro_crew.acp.launch import resolve_session_masks
    from kiro_crew.platform.interfaces import SpawnIdentity

    state = tmp_path.resolve() / "edition-sessions"
    for session in ("alpha", "beta"):
        (state / session).mkdir(parents=True)
    policy = _PerSessionPolicy(state)
    _install(monkeypatch, policy)

    alpha = resolve_session_masks(SpawnIdentity("alpha"), "standard")
    beta = resolve_session_masks(SpawnIdentity("beta"), "standard")

    assert alpha == ((str(state),), (str(state / "alpha"),))
    assert beta == ((str(state),), (str(state / "beta"),))
    assert [i.session_key for i in policy.asked] == ["alpha", "beta"]


def test_a_readonly_dir_outside_the_hidden_ones_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.acp.launch import resolve_session_masks
    from kiro_crew.platform.interfaces import SessionMasks, SpawnIdentity

    state = tmp_path.resolve() / "edition-sessions"
    for path in (tmp_path / ".ssh", state / "a", state / "b", tmp_path / "edition-sessionsx/alpha"):
        path.mkdir(parents=True)
    dangling = state / "dangling"
    dangling.symlink_to(state / "absent", target_is_directory=True)
    answer = SessionMasks(
        hidden_dirs=(str(state),),
        readonly_dirs=(
            str(tmp_path / ".ssh"),
            str(state),
            str(tmp_path / "edition-sessionsx/alpha"),
            "relative",
            str(state / "a/../b"),
            str(state / "absent"),
            str(dangling),
        ),
    )
    _install(monkeypatch, SimpleNamespace(session_masks=lambda identity: answer))

    # Only a proper descendant of a hidden dir survives: ~/.ssh would widen the sandbox,
    # the hidden dir itself would lift its own mask, and a sibling prefix is not inside.
    assert resolve_session_masks(SpawnIdentity("alpha"), "standard") == (
        (str(state),),
        (str(state / "b"),),
    )


@pytest.mark.parametrize("dangling", [False, True], ids=["absent name", "absent link target"])
def test_an_absent_hidden_path_refuses_the_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dangling: bool
) -> None:
    from kiro_crew.acp.launch import SessionMaskRefused, resolve_session_masks
    from kiro_crew.platform.interfaces import SpawnIdentity

    hidden = tmp_path.resolve() / "absent"
    if dangling:
        hidden.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    _install(monkeypatch, _PerSessionPolicy(hidden))
    with pytest.raises(SessionMaskRefused, match="does not exist"):
        resolve_session_masks(SpawnIdentity("alpha"), "standard")


def test_symlinked_session_paths_are_canonicalized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.acp.launch import resolve_session_masks
    from kiro_crew.platform.interfaces import SessionMasks, SpawnIdentity

    state = tmp_path.resolve() / "edition-sessions"
    own = state / "alpha"
    own.mkdir(parents=True)
    alias = tmp_path / "state-alias"
    alias.symlink_to(state, target_is_directory=True)
    own_alias = tmp_path / "own-alias"
    own_alias.symlink_to(own, target_is_directory=True)
    answer = SessionMasks(hidden_dirs=(str(alias),), readonly_dirs=(str(own_alias),))
    _install(monkeypatch, SimpleNamespace(session_masks=lambda identity: answer))

    assert resolve_session_masks(SpawnIdentity("alpha"), "standard") == (
        (os.path.realpath(alias),),
        (os.path.realpath(own_alias),),
    )


def test_a_readonly_symlink_outside_the_hidden_tree_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.acp.launch import resolve_session_masks
    from kiro_crew.platform.interfaces import SessionMasks, SpawnIdentity

    state = tmp_path.resolve() / "edition-sessions"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = state / "alpha"
    alias.symlink_to(outside, target_is_directory=True)
    answer = SessionMasks(hidden_dirs=(str(state),), readonly_dirs=(str(alias),))
    _install(monkeypatch, SimpleNamespace(session_masks=lambda identity: answer))

    assert resolve_session_masks(SpawnIdentity("alpha"), "standard") == ((str(state),), ())


@pytest.mark.parametrize("window", ["harness", "harness/alpha"])
def test_a_readonly_dir_in_a_symlinked_harness_mask_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, window: str
) -> None:
    from kiro_crew.acp.launch import resolve_session_masks
    from kiro_crew.platform.interfaces import SessionMasks, SpawnIdentity

    state = tmp_path.resolve() / "edition-sessions"
    harness = state / "harness"
    (harness / "alpha").mkdir(parents=True)
    alias = tmp_path / "harness-alias"
    alias.symlink_to(harness, target_is_directory=True)
    answer = SessionMasks(hidden_dirs=(str(state),), readonly_dirs=(str(state / window),))
    _install(monkeypatch, SimpleNamespace(session_masks=lambda identity: answer))

    assert resolve_session_masks(SpawnIdentity("alpha"), "standard", (str(alias),)) == (
        (str(state),),
        (),
    )


def test_a_readonly_dir_holding_a_symlinked_harness_mask_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.acp.launch import resolve_session_masks
    from kiro_crew.platform.interfaces import SessionMasks, SpawnIdentity

    state = tmp_path.resolve() / "edition-sessions"
    own = state / "alpha"
    harness = own / "credentials"
    harness.mkdir(parents=True)
    alias = tmp_path / "harness-alias"
    alias.symlink_to(harness, target_is_directory=True)
    answer = SessionMasks(hidden_dirs=(str(state),), readonly_dirs=(str(own),))
    _install(monkeypatch, SimpleNamespace(session_masks=lambda identity: answer))

    assert resolve_session_masks(SpawnIdentity("alpha"), "standard", (str(alias),)) == (
        (str(state),),
        (),
    )
    assert "not re-exposing" in caplog.text


@pytest.mark.parametrize(
    "policy",
    [
        SimpleNamespace(session_masks=lambda identity: 1 / 0),
        SimpleNamespace(
            session_masks=lambda identity: SimpleNamespace(
                hidden_dirs=("relative/state",), readonly_dirs=()
            )
        ),
    ],
    ids=["the policy raised", "a hidden path is relative"],
)
def test_an_answer_that_cannot_be_trusted_refuses_the_spawn(
    monkeypatch: pytest.MonkeyPatch, policy: object
) -> None:
    from kiro_crew.acp.launch import SessionMaskRefused, resolve_session_masks
    from kiro_crew.platform.interfaces import SpawnIdentity

    _install(monkeypatch, policy)
    with pytest.raises(SessionMaskRefused):
        resolve_session_masks(SpawnIdentity("alpha"), "standard")


def test_a_tier_that_drops_masks_refuses_a_masked_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.acp.launch import SessionMaskRefused, resolve_session_masks
    from kiro_crew.platform.interfaces import SpawnIdentity

    _install(monkeypatch, _PerSessionPolicy(tmp_path.resolve()), applies=False)
    with pytest.raises(SessionMaskRefused, match="agent.sandbox"):
        resolve_session_masks(SpawnIdentity("alpha"), "off")


def test_a_companion_without_the_method_asks_for_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.acp.launch import resolve_session_masks
    from kiro_crew.platform.interfaces import SpawnIdentity

    # The tier check is never reached when nothing is hidden.
    _install(monkeypatch, SimpleNamespace(), applies=False)
    assert resolve_session_masks(SpawnIdentity("alpha"), "off") == ((), ())


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_a_readonly_window_opens_inside_a_caller_mask_only(backend: str) -> None:
    own = f"{STATE}/alpha"
    plan = _plan(
        backend,
        extra_hidden_dirs=(STATE,),
        extra_readonly_windows=(own, f"{HOME}/.aws/sso"),
    )
    # ~/.aws is the TIER's mask: a read-only window there would re-open a credential
    # home, so it opens nothing.
    assert plan.windows == (own,)
    assert plan.readonly_windows == (own,)


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize("inner_first", [True, False], ids=["inner first", "outer first"])
def test_nested_readonly_windows_keep_only_the_outermost(backend: str, inner_first: bool) -> None:
    outer = f"{STATE}/alpha"
    inner = f"{outer}/credentials"
    requested = (inner, outer) if inner_first else (outer, inner)
    plan = _plan(backend, extra_hidden_dirs=(STATE,), extra_readonly_windows=requested)

    assert plan.windows == (outer,)
    assert plan.readonly_windows == (outer,)
    if backend == BACKEND_NAMESPACE:
        windows = sandbox_plan.namespace_payload(plan)["private_dirs"]
        assert windows == [outer]
        assert not any(
            window != other and sandbox_plan.path_within(window, other)
            for window in windows
            for other in windows
        )


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_naming_a_tier_mask_again_does_not_let_a_window_reopen_it(backend: str) -> None:
    aws = f"{HOME}/.aws"
    plan = _plan(backend, extra_hidden_dirs=(aws,), extra_readonly_windows=(f"{aws}/sso",))
    assert plan.windows == () and plan.readonly_windows == ()
    assert [r.message for r in plan.refusals] == [sandbox_plan.READONLY_WINDOW_REFUSAL]


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize(
    "private, readonly",
    [(f"{STATE}/alpha", f"{STATE}/alpha/creds"), (f"{STATE}/alpha/tmp", f"{STATE}/alpha")],
    ids=["inside a private window", "holding a private window"],
)
def test_a_readonly_window_never_overlaps_a_writable_one(
    backend: str, private: str, readonly: str
) -> None:
    plan = _plan(
        backend,
        extra_hidden_dirs=(STATE,),
        extra_private_dirs=(private,),
        extra_readonly_windows=(readonly,),
    )
    assert plan.windows == (private,)
    assert plan.readonly_windows == ()


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_a_readonly_window_holding_a_nested_mask_stays_masked(backend: str) -> None:
    own = f"{STATE}/alpha"
    plan = _plan(
        backend,
        extra_hidden_dirs=(STATE, f"{own}/private"),
        extra_readonly_windows=(own,),
    )
    assert plan.readonly_windows == ()
    assert plan.windows == ()
    assert plan.refusals


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize("resolved", [False, True], ids=["tier spelling", "resolved target"])
def test_a_readonly_window_holding_a_tier_mask_stays_masked(backend: str, resolved: bool) -> None:
    own = f"{STATE}/alpha"
    target = f"{own}/aws"
    host = PlanHost(
        home=HOME,
        tier_dirs=(".aws",) if resolved else (target,),
        **({"mask_target_spellings": (target,)} if resolved else {}),
    )
    plan = sandbox_plan.plan_confinement(
        SandboxRequest(
            tier="standard",
            backend=backend,
            extra_hidden_dirs=(STATE,),
            extra_readonly_windows=(own,),
        ),
        host,
    )
    assert plan.readonly_windows == ()
    assert plan.windows == ()
    assert [r.message for r in plan.refusals] == [sandbox_plan.READONLY_WINDOW_REFUSAL]


@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize("tier", ["standard", "cc", "strict"])
def test_the_live_host_records_resolved_tier_mask_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str, tier: str
) -> None:
    from kiro_crew import sandbox, sandbox_argv

    home = tmp_path.resolve() / "home"
    own = home / "sessions/alpha"
    target = own / "aws"
    target.mkdir(parents=True)
    (home / ".aws").symlink_to(target, target_is_directory=True)
    (home / "plain").mkdir()
    (home / "dangling").symlink_to(home / "absent", target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    dirs = (".aws", "plain", "dangling", "absent")
    monkeypatch.setattr(sandbox, "_STANDARD_DIRS", dirs)
    monkeypatch.setattr(
        sandbox,
        "_sandbox_policy",
        lambda: SimpleNamespace(cc_dirs=lambda: dirs, strict_dirs=lambda: dirs),
    )
    host = sandbox_argv._live_plan_host(
        SandboxRequest(
            tier=tier,
            backend=backend,
            extra_hidden_dirs=(str(home / "sessions"),),
            extra_readonly_windows=(str(own),),
        )
    )
    assert str(target) in host.mask_target_spellings
    assert str(home / "plain") not in host.mask_target_spellings
    assert str(home / "absent") not in host.mask_target_spellings


@pytest.mark.parametrize("hidden", [(), ("/state/sessions",)])
def test_the_pool_preflight_asks_for_the_claiming_sessions_masks(
    monkeypatch: pytest.MonkeyPatch, hidden: tuple[str, ...]
) -> None:
    from kiro_crew.platform.interfaces import SessionMasks, SpawnIdentity
    from kiro_crew.session_allocation_parts.cold_start_reads import (
        _session_masks_need_cold_start as session_masks_need_cold_start,
    )

    asked = []

    def masks(identity):
        asked.append(identity)
        return SessionMasks(hidden_dirs=hidden)

    _install(monkeypatch, SimpleNamespace(session_masks=masks))
    assert session_masks_need_cold_start("alpha") is bool(hidden)
    assert asked == [SpawnIdentity("alpha")]


def test_a_failed_pool_mask_preflight_requires_a_cold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.session_allocation_parts.cold_start_reads import (
        _session_masks_need_cold_start as session_masks_need_cold_start,
    )

    _install(monkeypatch, SimpleNamespace(session_masks=lambda identity: 1 / 0))
    assert session_masks_need_cold_start("alpha") is True


def test_a_pool_companion_without_session_masks_keeps_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.session_allocation_parts.cold_start_reads import (
        _session_masks_need_cold_start as session_masks_need_cold_start,
    )

    _install(monkeypatch, SimpleNamespace())
    assert session_masks_need_cold_start("alpha") is False


def test_a_pool_mask_preflight_propagates_composition_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.session_allocation_parts.cold_start_reads import (
        _session_masks_need_cold_start as session_masks_need_cold_start,
    )

    def masks(identity):
        raise context_mod.PlatformCompositionError("invalid policy")

    _install(monkeypatch, SimpleNamespace(session_masks=masks))
    with pytest.raises(context_mod.PlatformCompositionError, match="invalid policy"):
        session_masks_need_cold_start("alpha")


def test_the_launcher_payload_names_the_readonly_windows_only_when_asked() -> None:
    own = f"{STATE}/alpha"
    payload = sandbox_plan.namespace_payload(
        _plan(BACKEND_NAMESPACE, extra_hidden_dirs=(STATE,), extra_readonly_windows=(own,))
    )
    assert payload["private_dirs"] == [own]
    assert payload["readonly_windows"] == [own]
    plain = sandbox_plan.namespace_payload(_plan(BACKEND_NAMESPACE, extra_hidden_dirs=(STATE,)))
    assert "readonly_windows" not in plain


def test_seatbelt_lets_a_readonly_window_be_read_and_nothing_else() -> None:
    own = f"{STATE}/alpha"
    scratch = f"{STATE}/scratch"
    profile = sandbox_seatbelt.render_seatbelt_profile(
        _plan(
            BACKEND_SEATBELT,
            extra_hidden_dirs=(STATE,),
            extra_private_dirs=(scratch,),
            extra_readonly_windows=(own,),
        )
    ).splitlines()
    mask = f'(subpath "{STATE}")'
    read_ok = f'(require-not (subpath "{scratch}")) (require-not (subpath "{own}"))'
    assert f"(deny file-read* (require-all {mask} {read_ok}))" in profile
    # The private window stays writable; the read-only one does not.
    for operation in ("file-write*", "file-link"):
        assert (
            f'(deny {operation} (require-all {mask} (require-not (subpath "{scratch}"))))'
            in profile
        )


def test_seatbelt_denies_every_write_when_the_only_window_is_readonly() -> None:
    own = f"{STATE}/alpha"
    profile = sandbox_seatbelt.render_seatbelt_profile(
        _plan(BACKEND_SEATBELT, extra_hidden_dirs=(STATE,), extra_readonly_windows=(own,))
    ).splitlines()
    for operation in ("file-write*", "file-link"):
        assert f'(deny {operation} (subpath "{STATE}"))' in profile
    assert f'(allow file-read-metadata (literal "{STATE}"))' in profile
