"""A member start judges the paths it canonicalised itself on the canonical gate.

``session_capabilities.prepare_runtime`` runs off the event loop and resolves the
member's workspace, each agent spec's name and each spec's opened descriptor before it
asks whether the path is sensitive. Those checks go through
``security.is_sensitive_canonical_path`` (or ``canonical_path_refusal``), which answers
inline off the loop. So a resolver child that misses its deadline cannot refuse the
member its own spec, and a sensitive path is still refused.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
import subagent_continuable_helpers

from kiro_crew import agent, hooks
from kiro_crew.agent_capabilities import CapabilityError
from kiro_crew.config import loader_pipeline
from kiro_crew.session_capabilities import prepare_runtime
from kiro_crew.subprocess_pool import SubprocessPoolExecutor, SubprocessPoolTimeout

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers
continuation_runtime = subagent_continuable_helpers.continuation_runtime


def test_a_member_starts_on_its_own_spec_while_every_resolver_child_misses_its_deadline(
    continuation_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = continuation_runtime
    template, _store = world.enroll_collision()
    submitted: list[int] = []
    caller = threading.get_ident()

    def missed_deadline(self, op, payload, timeout=None):
        # What macOS reports for a child that took the request and missed the
        # deadline: no syscall sample, so the miss is charged as a mount stall.
        if threading.get_ident() == caller:
            submitted.append(op)
        raise SubprocessPoolTimeout("child did not answer within the budget", child_syscall=None)

    monkeypatch.setattr(SubprocessPoolExecutor, "call_op", missed_deadline)

    try:
        prepared = prepare_runtime(None, "worker", world.project)
    except CapabilityError as exc:
        pytest.fail(f"the member start refused its own spec: {exc}")

    assert prepared.template == template
    assert submitted == []


def test_each_canonical_member_start_check_still_refuses_a_sensitive_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The crew home is a per-test tmp dir here; ``.env`` and ``diag`` are on its fence.
    crew_home = Path(os.environ["KIROCREW_HOME"])
    crew_home.mkdir(parents=True, exist_ok=True)
    secret = crew_home / ".env"
    secret.write_text('{"name": "leaked"}', encoding="utf-8")
    fenced_dir = crew_home / "diag"
    fenced_dir.mkdir()
    monkeypatch.setattr(loader_pipeline, "workspace_dir_for", lambda _workspace: fenced_dir)

    assert agent._spec_path_shape_refusal(secret, crew_home) == (
        "the path resolves to a sensitive target"
    )
    assert loader_pipeline.default_project_dir() == ""
    assert (
        hooks.safe_read_file_bytes_nolink(str(secret), str(crew_home), canonical_gate=True) is None
    )
    # With the name check passed, the opened descriptor's own check still refuses the
    # secret and admits a plain file beside it.
    plain = crew_home / "plain.json"
    plain.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hooks, "validate_file_path", lambda raw, **_kw: raw)
    root = os.path.realpath(crew_home)
    read = hooks.safe_read_file_bytes_nolink
    assert read(os.path.realpath(secret), root, canonical_gate=True) is None
    assert read(os.path.realpath(plain), root, canonical_gate=True) == b"{}"
