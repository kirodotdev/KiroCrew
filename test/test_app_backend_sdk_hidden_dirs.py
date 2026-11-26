"""``extra_hidden_dirs`` on the backend SDK's sandboxed spawns (seam level 18).

What is pinned: the keyword only adds hidden paths (no option that lifts, windows or
unseals a mask is offered next to it), each entry is made absolute once at the SDK
entry and handed to the chokepoint as that one spelling, an entry overlapping one of
the data home's read-only leaves (the governance ceilings among them) is refused, a
spawn that would not carry the paths is refused, and every refusal happens before
anything is prepared, on all four spawn names.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.apps.backend_sdk import spawn as sdk_spawn
from kiro_crew.config.loader import config_dir
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_SPAWN_NAMES = ("sandboxed_argv", "sandboxed_argv_async", "run_sandboxed", "run_sandboxed_async")


class _Prepare:
    """A stand-in chokepoint: records each call and returns a plan with no cleanup."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return (["wrapped", *args[0]], {"PATH": "/usr/bin"}, None)


class _PrepareAsync(_Prepare):
    async def __call__(self, *args, **kwargs):  # type: ignore[override]
        return super().__call__(*args, **kwargs)


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=FALLBACK_TIMEOUT_SECS))


@pytest.fixture
def chokepoint(monkeypatch):
    """Both chokepoints recorded, the mask reported as applied, no child run."""
    prepare, prepare_async = _Prepare(), _PrepareAsync()
    asked: list[str] = []

    def applies(mode):
        asked.append(mode)
        return True

    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv", prepare)
    monkeypatch.setattr("kiro_crew.sandbox.sandboxed_spawn_argv_async", prepare_async)
    monkeypatch.setattr("kiro_crew.sandbox.credential_mask_applies", applies)
    monkeypatch.setattr("kiro_crew.sandbox.run_limited", lambda argv, **kw: ("ran", argv, kw))
    return prepare, prepare_async, asked


def test_the_keyword_is_a_level_eighteen_keyword_on_all_four_names() -> None:
    assert sdk._KEYWORD_LEVELS["extra_hidden_dirs"] == 18 <= sdk.SDK_LEVEL
    assert "extra_hidden_dirs" not in sdk.__all__
    for name in _SPAWN_NAMES:
        parameters = inspect.signature(getattr(sdk, name)).parameters
        assert parameters["extra_hidden_dirs"].default == (), name
        assert parameters["extra_hidden_dirs"].kind is inspect.Parameter.KEYWORD_ONLY, name


@pytest.mark.parametrize(
    "knob", ["extra_visible_dirs", "extra_private_dirs", "extra_writable_dirs", "is_kiro_cli"]
)
def test_no_name_offers_an_option_that_lifts_or_opens_a_mask(knob) -> None:
    for name in _SPAWN_NAMES:
        assert knob not in inspect.signature(getattr(sdk, name)).parameters, name


def test_a_relative_entry_is_made_absolute_once_and_forwarded(chokepoint, tmp_path) -> None:
    prepare, _, asked = chokepoint
    private = tmp_path / "private"
    sdk.sandboxed_argv(
        ["tex", "doc.tex"],
        mode="strict",
        extra_hidden_dirs=["kc-hidden-probe", private, str(private / ".." / "private")],
    )
    ((args, kwargs),) = prepare.calls
    assert args == (["tex", "doc.tex"], "strict")
    assert kwargs["extra_hidden_dirs"] == (
        os.path.join(os.getcwd(), "kc-hidden-probe"),
        str(private),
    )
    assert asked == ["strict"]


def test_no_entry_leaves_the_chokepoint_call_unchanged(chokepoint) -> None:
    prepare, _, asked = chokepoint
    sdk.sandboxed_argv(["ls"], extra_hidden_dirs=[])
    assert prepare.calls == [((["ls"], "standard"), {"env": None, "strip_python_env": False})]
    assert asked == []


def test_a_data_home_secret_beside_the_ceilings_may_be_hidden(chokepoint) -> None:
    prepare, _, _ = chokepoint
    secret = config_dir() / "sel_hmac.key"
    sdk.run_sandboxed(["tex"], mode="strict", extra_hidden_dirs=[secret])
    ((_args, kwargs),) = prepare.calls
    assert kwargs["extra_hidden_dirs"] == (str(secret),)


@pytest.mark.parametrize(
    "entry",
    [
        "a single path, not a list",
        b"bytes",
        Path("one-path"),
    ],
)
def test_one_path_instead_of_a_list_is_refused_before_preparing(chokepoint, entry) -> None:
    prepare, _, _ = chokepoint
    with pytest.raises(TypeError):
        sdk.sandboxed_argv(["ls"], extra_hidden_dirs=entry)
    assert prepare.calls == []


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        (7, TypeError),
        (None, TypeError),
        (b"/x", TypeError),
        ("", ValueError),
        ("a\x00b", ValueError),
    ],
)
def test_a_malformed_entry_is_refused_before_preparing(chokepoint, entry, error) -> None:
    prepare, prepare_async, asked = chokepoint
    with pytest.raises(error):
        sdk.sandboxed_argv(["ls"], extra_hidden_dirs=[entry])
    with pytest.raises(error):
        sdk.run_sandboxed(["ls"], extra_hidden_dirs=[entry])
    with pytest.raises(error):
        _run(sdk.sandboxed_argv_async(["ls"], extra_hidden_dirs=[entry]))
    with pytest.raises(error):
        _run(sdk.run_sandboxed_async(["ls"], extra_hidden_dirs=[entry]))
    assert prepare.calls == [] and prepare_async.calls == [] and asked == []


def _ceiling_entry(spelling: str) -> Path:
    """*spelling* under the live data home; ``~`` names the home-relative ceiling."""
    if spelling == "~":
        return Path.home() / ".kiro" / "crew" / "security_policy.json"
    return config_dir() / spelling


@pytest.mark.parametrize(
    "spelling",
    [
        "security_policy.json",
        "admission_policy.json",
        "computer_use.json",
        "profiles",
        "profiles/default.json",
        ".",
        "..",
        "~",
    ],
)
def test_an_entry_overlapping_a_read_only_leaf_is_refused_on_every_name(
    chokepoint, spelling
) -> None:
    prepare, prepare_async, asked = chokepoint
    entry = _ceiling_entry(spelling)
    with pytest.raises(ValueError, match="overlaps"):
        sdk.sandboxed_argv(["ls"], extra_hidden_dirs=[entry])
    with pytest.raises(ValueError, match="overlaps"):
        sdk.run_sandboxed(["ls"], extra_hidden_dirs=[entry])
    with pytest.raises(ValueError, match="overlaps"):
        _run(sdk.sandboxed_argv_async(["ls"], extra_hidden_dirs=[entry]))
    with pytest.raises(ValueError, match="overlaps"):
        _run(sdk.run_sandboxed_async(["ls"], extra_hidden_dirs=[entry]))
    assert prepare.calls == [] and prepare_async.calls == [] and asked == []


def test_the_ceilings_checked_are_the_sandbox_read_only_leaves_under_the_live_home() -> None:
    targets = {os.path.realpath(t) for t in sdk_spawn._ceiling_targets()}
    for leaf in ("security_policy.json", "admission_policy.json", "profiles", "computer_use.json"):
        assert leaf in sandbox._CREW_READONLY_LEAVES, leaf
        assert os.path.realpath(config_dir() / leaf) in targets, leaf


def test_a_spawn_that_would_not_carry_the_paths_is_refused(monkeypatch, chokepoint) -> None:
    prepare, prepare_async, _ = chokepoint
    monkeypatch.setattr("kiro_crew.sandbox.credential_mask_applies", lambda mode: False)
    hidden = ["kc-hidden-probe"]
    for call in (
        lambda: sdk.sandboxed_argv(["ls"], extra_hidden_dirs=hidden),
        lambda: sdk.run_sandboxed(["ls"], extra_hidden_dirs=hidden),
        lambda: _run(sdk.sandboxed_argv_async(["ls"], extra_hidden_dirs=hidden)),
        lambda: _run(sdk.run_sandboxed_async(["ls"], extra_hidden_dirs=hidden)),
    ):
        with pytest.raises(sdk.SandboxUnavailableError) as raised:
            call()
        assert raised.value.kind == "no_backend"
    assert prepare.calls == [] and prepare_async.calls == []


def test_another_tier_is_refused_before_the_paths_are_checked(chokepoint) -> None:
    prepare, _, asked = chokepoint
    with pytest.raises(ValueError, match="sandbox mode"):
        sdk.sandboxed_argv(["ls"], mode="off", extra_hidden_dirs=["kc-hidden-probe"])
    assert prepare.calls == [] and asked == []


def test_run_sandboxed_forwards_the_paths_and_runs_the_wrapped_argv(chokepoint) -> None:
    prepare, _, _ = chokepoint
    got = sdk.run_sandboxed(
        ["tex"], mode="strict", extra_hidden_dirs=["kc-hidden-probe"], check=True
    )
    assert got == ("ran", ["wrapped", "tex"], {"env": {"PATH": "/usr/bin"}, "check": True})
    ((_args, kwargs),) = prepare.calls
    assert kwargs["extra_hidden_dirs"] == (os.path.join(os.getcwd(), "kc-hidden-probe"),)


def test_the_async_preparation_forwards_the_same_paths(chokepoint) -> None:
    _, prepare_async, asked = chokepoint
    plan = _run(sdk.sandboxed_argv_async(["tex"], mode="strict", extra_hidden_dirs=["kc-a"]))
    assert plan == (["wrapped", "tex"], {"PATH": "/usr/bin"}, None)
    ((args, kwargs),) = prepare_async.calls
    assert args == (["tex"], "strict")
    assert kwargs == {
        "env": None,
        "strip_python_env": False,
        "extra_hidden_dirs": (os.path.join(os.getcwd(), "kc-a"),),
    }
    assert asked == ["strict"]


def test_the_async_run_forwards_the_paths_to_its_preparation(monkeypatch, chokepoint) -> None:
    _, prepare_async, _ = chokepoint

    class _Done:
        returncode = 0
        pid = 0

        async def communicate(self, _input=None):
            return (b"out", b"")

    async def fake_create(*argv, **kwargs):
        return _Done()

    monkeypatch.setattr("kiro_crew.sandbox.create_subprocess_limited", fake_create)
    monkeypatch.setattr(
        "kiro_crew.platform_compat._isolated_group_of_live_child", lambda pid, *, reaped: None
    )
    done = _run(sdk.run_sandboxed_async(["tex"], mode="strict", extra_hidden_dirs=["kc-b"]))
    assert done.stdout == b"out"
    ((_args, kwargs),) = prepare_async.calls
    assert kwargs["extra_hidden_dirs"] == (os.path.join(os.getcwd(), "kc-b"),)
