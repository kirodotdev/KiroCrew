"""MiMo Code with no sign-in must not answer from Xiaomi's hosted model.

MiMo Code 0.1.17, driven over ACP from a scratch home with no sign-in, no key and no
``model`` in its config, puts the session on ``xiaomi/mimo-v2.6-pro-ultraspeed``,
advertises the three models below, and sends the first prompt to Xiaomi's API with no
``Authorization`` header. A host signed in with ``mimo providers login`` advertises the
SAME three models, so the refusal also reads the read-back's credential presence.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import model_registry as mr
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import mimocode as mimocode_mod
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.harness.base import HarnessReadBack
from kiro_crew.acp.no_model_provider import AcpNoModelProvider
from kiro_crew.acp.types import ACP_BACKEND_MIMOCODE
from kiro_crew.agent_sdk import host_auth

#: The ``model`` select of ``session/new``, signed in or not.
_ADVERTISED = (
    "xiaomi/mimo-v2.6-pro-ultraspeed",
    "xiaomi/mimo-v2.6-pro",
    "xiaomi/mimo-v2.6-flash",
)
_DEFAULT = "xiaomi/mimo-v2.6-pro-ultraspeed"


@pytest.fixture(autouse=True)
def _isolate_advertised_cache(_floor_monkeypatch):
    _floor_monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})


def _client(tmp_path, *, advertised=_ADVERTISED, model="", config_model="", credential=False):
    client = AcpClient(work_dir=tmp_path, model=model, acp_backend=ACP_BACKEND_MIMOCODE)
    client._session_id = "ses-1"
    client._available_models = [{"modelId": m, "name": m} for m in advertised]
    client._resolved_model_id = _DEFAULT
    client._process_adapter = SimpleNamespace(  # type: ignore[assignment]
        read_back=HarnessReadBack(config_model=config_model, hosted_credential=credential)
    )
    client.set_config_option = AsyncMock()  # type: ignore[method-assign]
    return client


class TestTheUnsignedFallbackIsRefused:
    @pytest.mark.asyncio
    async def test_no_sign_in_refuses_before_any_prompt(self, tmp_path):
        client = _client(tmp_path)

        with pytest.raises(AcpNoModelProvider) as info:
            await client._apply_startup_model()

        text = str(info.value)
        assert _DEFAULT in text
        assert host_auth.signed_out_message(ACP_BACKEND_MIMOCODE) in text
        assert info.value.transient is False
        client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_pin_that_leaves_the_hosted_default_is_refused(self, tmp_path):
        client = _client(tmp_path, model="xiaomi/mimo-v2.6-flash")
        client._push_model_config_option = AsyncMock(return_value="")  # type: ignore[method-assign]

        with pytest.raises(AcpNoModelProvider):
            await client._apply_startup_model()


class TestASignedInOrChosenModelIsLeftAlone:
    @pytest.mark.asyncio
    async def test_a_sign_in_is_present(self, tmp_path):
        client = _client(tmp_path, credential=True)
        await client._apply_startup_model()
        assert client._resolved_model_id == _DEFAULT

    @pytest.mark.asyncio
    async def test_the_harness_config_names_the_model(self, tmp_path):
        client = _client(tmp_path, config_model=_DEFAULT)
        await client._apply_startup_model()
        assert client._resolved_model_id == _DEFAULT

    @pytest.mark.asyncio
    async def test_a_provider_of_the_operators_own_is_reachable(self, tmp_path):
        client = _client(tmp_path, advertised=(*_ADVERTISED, "local/fake-model"))
        await client._apply_startup_model()
        assert client._resolved_model_id == _DEFAULT


class TestTheCredentialIsReadByPresence:
    """What the read-back records, from the same child env the spawn uses."""

    def _env(self, tmp_path, **extra):
        return {"HOME": str(tmp_path / "home"), **extra}

    def test_nothing_signed_in(self, tmp_path):
        assert mimocode_mod._mimocode_hosted_credential_present(self._env(tmp_path)) is False

    def test_the_sign_in_file_exists(self, tmp_path):
        leaf = tmp_path / "home" / ".local" / "share" / "mimocode" / "auth.json"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("not json: the check never opens it")
        assert mimocode_mod._mimocode_hosted_credential_present(self._env(tmp_path)) is True

    def test_the_sign_in_file_under_xdg_data_home(self, tmp_path):
        leaf = tmp_path / "data" / "mimocode" / "auth.json"
        leaf.parent.mkdir(parents=True)
        leaf.touch()
        env = self._env(tmp_path, XDG_DATA_HOME=str(tmp_path / "data"))
        assert mimocode_mod._mimocode_hosted_credential_present(env) is True

    def test_xiaomi_api_key_is_set(self, tmp_path):
        env = self._env(tmp_path, XIAOMI_API_KEY="sk-test")
        assert mimocode_mod._mimocode_hosted_credential_present(env) is True

    def _read_back(self, tmp_path, monkeypatch, stdout):
        completed = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        # No shell variable steers the case: the read-back refuses on MIMOCODE_HOME,
        # and a key or data home on the runner would answer for the test.
        for name in [n for n in os.environ if n.startswith("MIMOCODE_")]:
            monkeypatch.delenv(name, raising=False)
        for name in ("XIAOMI_API_KEY", "XDG_DATA_HOME"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(mimocode_mod.subprocess_mod, "run", lambda *_a, **_kw: completed)
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        client._process_adapter = process_adapter_for(ACP_BACKEND_MIMOCODE)
        mimocode_mod._verify_mimocode_routing(
            client, ACP_BACKEND_MIMOCODE, ["mimo", "debug", "config"], "{}"
        )
        return client._harness_read_back()

    def test_the_read_back_records_it(self, tmp_path, monkeypatch):
        read_back = self._read_back(tmp_path, monkeypatch, '{"model": "xiaomi/mimo-v2.6-flash"}')
        assert read_back.config_model == "xiaomi/mimo-v2.6-flash"
        assert read_back.hosted_credential is False

    def test_a_key_in_the_harness_config(self, tmp_path, monkeypatch):
        stdout = '{"provider": {"xiaomi": {"options": {"apiKey": "sk-test"}}}}'
        assert self._read_back(tmp_path, monkeypatch, stdout).hosted_credential is True

    def test_an_empty_key_in_the_harness_config(self, tmp_path, monkeypatch):
        stdout = '{"provider": {"xiaomi": {"options": {"apiKey": " "}}}}'
        assert self._read_back(tmp_path, monkeypatch, stdout).hosted_credential is False
