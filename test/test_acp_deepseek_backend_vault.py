"""The DeepSeek Harness's provider key: fed from Crew's vault rather than carved out of the
credential mask, and the declared message a session with no key gets.
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_deepseek_backend_helpers import (  # noqa: F401 -- fixtures are used by name
    _DeepseekLaunchRig,
    _pinned_kiro_cli_version,
)

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import deepseek as deepseek_mod
from kiro_crew.acp_backends import ACP_BACKEND_DEEPSEEK

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
        launch_mod.launch,
        deepseek_mod.DeepseekLaunch.apply_spawn_env,
        deepseek_mod.DeepseekLaunch.prepare_spawn_env,
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
        launch_mod,
        "resolve_self_served_launch",
        AsyncMock(return_value=("/fake/dsh", ["/fake/dsh", "--profile", "acp"], "dsh", "dsh")),
    )
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", AsyncMock(return_value=()))
    monkeypatch.setattr(
        deepseek_mod, "_seal_deepseek_gate_extension", lambda: str(tmp_path / "sealed.mjs")
    )
    monkeypatch.setattr(
        deepseek_mod, "_write_deepseek_gate_patch", lambda _ext: str(tmp_path / "patch.yml")
    )
    monkeypatch.setattr(deepseek_mod, "_verify_deepseek_gate", lambda *a, **k: ("", ""))
    monkeypatch.setattr(launch_mod, "_unlink_readback_launcher", lambda _p: None)

    async def wrap_ok(call_argv, **kwargs):
        return list(call_argv), None

    # The probe's wrap at the adapter, the real child's at the client's launch tools.
    monkeypatch.setattr(deepseek_mod, "wrap_argv_async", wrap_ok)
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
    client, spawned, asyncio_mod = _dsh_vault_spawn(
        tmp_path,
        monkeypatch,
        mapping=_vault_env_mapping(),
        secret=("dsh-proof", "dummy-vault-fed-key"),
    )
    # The resolver's OWN returned dict, captured by identity so the clear can be
    # observed on the object the contract names rather than on a copy.
    resolved_dicts: list[dict] = []
    real_resolver = deepseek_mod._deepseek_vault_env

    def capturing_resolver():
        resolved, keys = real_resolver()
        resolved_dicts.append(resolved)
        return resolved, keys

    monkeypatch.setattr(deepseek_mod, "_deepseek_vault_env", capturing_resolver)

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
        deepseek_mod, "_verify_deepseek_gate", lambda *a, **k: probed.append(True) or ("", "")
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
    client, spawned, asyncio_mod = _dsh_vault_spawn(tmp_path, monkeypatch, mapping={})
    reads: list[object] = []
    monkeypatch.setattr(
        deepseek_mod,
        "resolve_secret_uris",
        lambda *a, **k: reads.append(a) or ({}, set()),
    )

    asyncio_mod.run(client._spawn())

    assert reads == [], "an empty agent.deepseek_env still read the vault"
    assert "DEEPSEEK_API_KEY" not in spawned["seen"]


def test_the_probe_is_never_handed_a_key(monkeypatch, tmp_path) -> None:
    """The read-back PROBE, which boots the plugin and exits, is handed NAMES, never a key.

    It needs no key, and a probe carrying a live one would widen the secret's exposure
    to a second process for no gain. So its launch reads the mapping's names and
    hands those to the plugin, and never reaches the key resolver or the vault --
    both are booby-trapped in the rig.
    """
    rig = _DeepseekLaunchRig(monkeypatch, tmp_path, names=("DEEPSEEK_API_KEY",))
    rig.run()
    assert rig.args_of("verify")[4] == {"child_scrub_names": ("DEEPSEEK_API_KEY",)}
    assert rig.names().index("names") < rig.names().index("verify")


def test_the_injection_is_inside_the_arm_and_the_probe_is_never_handed_a_key(
    tmp_path, monkeypatch
) -> None:
    """The session's key is placed on its env BEFORE the shared scrub judges it.

    That ordering is the whole reason the validator refuses a name the scrub would
    strip: a key injected after the scrub would never be judged by it, and one
    removed by it would leave a harness that cannot reach its model. The probe half
    of this claim is ``test_the_probe_is_never_handed_a_key``.
    """
    from kiro_crew.acp import client as client_module

    client, spawned, asyncio_mod = _dsh_vault_spawn(
        tmp_path,
        monkeypatch,
        mapping=_vault_env_mapping(),
        secret=("dsh-proof", "dummy-vault-fed-key"),
    )
    real_scrub = client_module.scrub_agent_subprocess_env
    scrubbed_from: list[dict] = []

    def _scrub(env, **kwargs):
        scrubbed_from.append(dict(env))
        return real_scrub(env, **kwargs)

    monkeypatch.setattr(client_module, "scrub_agent_subprocess_env", _scrub)
    asyncio_mod.run(client._spawn())
    assert scrubbed_from, "the launch never scrubbed the session's environment"
    assert (
        scrubbed_from[0].get("DEEPSEEK_API_KEY") == "dummy-vault-fed-key"
    ), "the provider key was placed after the scrub, so the scrub never judged it"
    assert spawned["seen"]["DEEPSEEK_API_KEY"] == "dummy-vault-fed-key"


# ── A harness with no provider key: the declared message, not the raw frame ──

#: The ``session/prompt`` error dsh 0.1.5-rc.3 returns when no provider key
#: reaches it, copied off the live wire. It carries no ``data`` field.
_DSH_NO_KEY_ERROR = {
    "code": -32603,
    "message": (
        'Internal error: turn failed: llm-deepseek: no API key for provider route "'
        'deepseek-official"; store DEEPSEEK_API_KEY through the credentials service '
        "(the web Models page writes it), or export DEEPSEEK_API_KEY in the launching "
        "environment"
    ),
}


def test_a_missing_provider_key_gets_the_declared_message() -> None:
    from kiro_crew.acp.client import _format_acp_error
    from kiro_crew.agent_sdk import host_auth

    formatted = _format_acp_error(_DSH_NO_KEY_ERROR, backend=ACP_BACKEND_DEEPSEEK)
    assert formatted == host_auth.signed_out_message(ACP_BACKEND_DEEPSEEK)


def test_the_request_id_survives_the_rewrite() -> None:
    from kiro_crew.acp.client import _format_acp_error
    from kiro_crew.agent_sdk import host_auth

    error = dict(_DSH_NO_KEY_ERROR, data="request_id: 0f1e2d3c-4b5a")
    formatted = _format_acp_error(error, backend=ACP_BACKEND_DEEPSEEK)
    assert formatted == (
        f"{host_auth.signed_out_message(ACP_BACKEND_DEEPSEEK)} (request_id: 0f1e2d3c-4b5a)"
    )


def test_a_missing_provider_key_is_not_retried() -> None:
    from kiro_crew.acp.client import AcpError, _raise_acp_error

    with pytest.raises(AcpError) as info:
        _raise_acp_error(_DSH_NO_KEY_ERROR, backend=ACP_BACKEND_DEEPSEEK)
    assert info.value.transient is False


def test_the_deepseek_phrase_does_not_classify_another_harness() -> None:
    from kiro_crew.acp.client import _format_acp_error
    from kiro_crew.agent_sdk import host_auth

    formatted = _format_acp_error(_DSH_NO_KEY_ERROR, backend="goose")
    assert host_auth.signed_out_message(ACP_BACKEND_DEEPSEEK) not in formatted
    assert host_auth.signed_out_message("goose") not in formatted


def test_a_blank_signed_out_signature_is_refused() -> None:
    import dataclasses

    from kiro_crew.agent_sdk import host_auth

    declaration = host_auth.declaration_for(ACP_BACKEND_DEEPSEEK)
    with pytest.raises(ValueError, match="blank signed-out signature"):
        dataclasses.replace(declaration, signed_out_signature="  ")
