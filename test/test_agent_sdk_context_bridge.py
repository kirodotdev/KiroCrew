"""Functional contracts for the SDK context and projected-MCP bridges."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from kiro_crew.agent_sdk import context_provider_of
from kiro_crew.agent_sdk.drivers.acp import projected_session_mcp_servers
from kiro_crew.essential_delivery import EssentialDelivery
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider
from kiro_crew.testing.wait import default_timeout


def test_only_real_provider_types_can_bind_context():
    provider = object.__new__(AcpProvider)
    assert context_provider_of(provider) is provider
    fake = Mock(spec=LLMProvider)
    fake.essential_delivery = EssentialDelivery()
    # Mock's advertised __class__ fools ordinary isinstance but is not an opt-in.
    assert isinstance(fake, LLMProvider)
    assert context_provider_of(fake) is None
    assert context_provider_of(None) is None

    class Proxy:
        def __getattr__(self, name):
            raise AssertionError("provider classification inspected dynamic attributes")

    assert context_provider_of(Proxy()) is None


def test_projected_mcp_sdk_bridge_preserves_exact_underlying_result(monkeypatch, tmp_path):
    from kiro_crew.acp import session_mcp

    wire = [{"name": "fixture", "command": "fixture-tool", "args": []}]
    projection = Mock(return_value=wire)
    monkeypatch.setattr(session_mcp, "session_mcp_servers", projection)
    assert projected_session_mcp_servers("writer", work_dir=tmp_path) is wire
    projection.assert_called_once_with("writer", work_dir=tmp_path)
    projection.side_effect = ValueError("unreadable fixture")
    with pytest.raises(ValueError, match="unreadable fixture"):
        projected_session_mcp_servers("writer", work_dir=tmp_path)


@pytest.mark.parametrize(
    "modules",
    [
        "kiro_crew.essential_delivery,kiro_crew.providers.base,kiro_crew.context",
        "kiro_crew.providers.base,kiro_crew.essential_delivery,kiro_crew.context",
        "kiro_crew.agent_sdk,kiro_crew.context,kiro_crew.providers.acp",
    ],
)
def test_context_import_orders_do_not_form_a_cycle(tmp_path, modules):
    order = modules.split(",")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")
    bound = default_timeout()
    # A hang writes nothing on its own, so the child dumps its stack shortly
    # before the bound and the assertion below can name the import it stopped in.
    dump_after = bound - min(5.0, bound / 4)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import faulthandler; "
                f"faulthandler.dump_traceback_later({dump_after!r}, exit=False); "
                "import importlib; [importlib.import_module(m) for m in " + repr(order) + "]",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=bound,
        )
    except subprocess.TimeoutExpired as exc:
        # On POSIX the re-raised TimeoutExpired carries undecoded bytes even with text=True.
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise AssertionError(
            f"importing {' -> '.join(order)} in a fresh interpreter did not finish "
            f"within {exc.timeout:g} s; stderr so far:\n{stderr}"
        ) from None
    assert completed.returncode == 0, completed.stderr


def test_projected_mcp_helper_stays_on_driver_surface():
    from kiro_crew import agent_sdk

    assert "projected_session_mcp_servers" not in agent_sdk.__all__
    assert not hasattr(agent_sdk, "projected_session_mcp_servers")
