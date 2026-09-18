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

import inspect
import os
import sys
from pathlib import Path

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
    for name in capture_mod._ADAPTER_CACHE_NAMES:
        assert getattr(client_mod, name) is before[name], f"{name} was not restored"
    assert set(before) > set(capture_mod._ADAPTER_CACHE_NAMES), (
        "the snapshot covers only the adapter caches, so the self-served resolution "
        "is being left as the capture set it"
    )


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
