"""Chat runtime sharing on the AcpRuntime start path: where a chat session lands.

Behind the ``chat_runtime_sharing`` feature preview (off by default). With it off,
or for any start :func:`~kiro_crew.acp.chat_runtime_sharing.chat_sharing_ineligible_reason`
refuses, :func:`resolve_chat_share` answers ``None`` and the start spawns its own
process exactly as before.

An eligible start builds a :class:`~kiro_crew.acp.chat_runtime_sharing.ChatRuntimeKey`
from its process-level spawn inputs and asks the ownership registry for a lease
under it: the registry either hands back a live compatible runtime with room
(JOIN) or runs this start's spawn as its miss path (FOUND). The placement decision
and the spawn are one act, which is why the lease is taken here, before the
provider exists, and handed to ``AcpSessionProvider`` as ``runtime_lease``.

Four inputs a running process cannot shed are read before the acquisition and
RE-READ after it: the agent spec generation, the account era, the governed
sandbox tier and the SSH_AUTH_SOCK-forwarding consent. On codex and claude the
account era is the harness's own sign-in (:func:`codex_spawn_identity`,
:func:`claude_spawn_identity`), because each signs in on its own and never reads
Kiro's sign-in. The acquisition is not
instantaneous (a same-key arrival waits out a founding spawn), so a revocation, a
sign-in to another account, a raised floor or a revoked consent landing in that
window would otherwise place this session on a process started under the older
value. A mismatch releases and retries on the fresh key; a value still moving
after :data:`_PLACEMENT_ATTEMPTS` rounds founds this session its own process. A
JOINER also runs the fork-governance admission the founder ran inside ``spawn()``.

Every release here runs as one task this frame only waits on, through any
cancellation, so a lease is never stranded: a stranded lease keeps its entry
alive, the kill gate refuses every kill of that pid, and the process leaks for
the gateway's life.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.acp.chat_runtime_sharing import (
    ChatRuntimeKey,
    agent_pins_tool_surface,
    agent_spawns_native_subagents,
    agent_spec_generation,
    chat_runtime_cap,
    chat_runtime_sharing_enabled,
    chat_runtime_sharing_harness_enabled,
    chat_sharing_ineligible_reason,
    kas_chat_sharing_ready,
    keys_tool_surface,
    member_scope_of,
    per_session_fields,
    share_tree_root_of,
)
from kiro_crew.acp.runtime import AcpRuntimeError
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
)
from kiro_crew.agent_sdk.codex_transcripts import codex_home
from kiro_crew.metrics.chat_runtime_share import record_placed, record_refused
from kiro_crew.runtime_ownership import CHAT_RUNTIME_CAP, RUNTIME_OWNERSHIP, Acquisition

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.providers.acp import AcpProvider

logger = logging.getLogger(__name__)

#: :func:`codex_spawn_identity`'s random token per account seen by this process.
#: shortcut: never pruned; one entry per distinct codex sign-in this gateway
#: process has seen, which stays a handful. Prune if sign-ins ever churn.
_CODEX_ERA_TOKENS: dict[str, str] = {}

#: :func:`goose_spawn_identity`'s random token per provider and credential set seen
#: by this process. shortcut: never pruned, like ``_CODEX_ERA_TOKENS``; one entry per
#: distinct key rotation, which stays a handful. Prune if rotations ever churn.
_GOOSE_ERA_TOKENS: dict[str, str] = {}

#: Endings of the environment names a goose provider reads its credential from
#: (``OPENAI_API_KEY``, ``DATABRICKS_TOKEN``, ``AWS_SECRET_ACCESS_KEY``,
#: ``AWS_PROFILE`` and the like): goose looks a secret up in the environment first.
_GOOSE_CREDENTIAL_ENDINGS = ("_KEY", "_KEY_ID", "_TOKEN", "_SECRET", "_PROFILE")

#: The environment names goose locates its home from: config, data (sessions) and
#: state directories.
_GOOSE_HOME_VARIABLES = (
    "GOOSE_PATH_ROOT",
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)


def _codex_jwt_claim_user(jwt: object) -> str:
    """The ChatGPT user id inside an ``id_token`` JWT's payload, or ``""``.

    Only read to tell two people in one workspace apart; the signature is not
    checked because codex-acp, not this read, authenticates with the token.
    """
    if not isinstance(jwt, str) or jwt.count(".") != 2:
        return ""
    payload = jwt.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return ""
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    if not isinstance(auth, dict):
        return ""
    user = auth.get("chatgpt_user_id") or auth.get("user_id")
    return user if isinstance(user, str) else ""


def codex_spawn_identity(env_overrides: Mapping[str, str] | None = None) -> str:
    """The account era of a codex start: which codex account a new process loads.

    codex-acp authenticates itself, from ``auth.json`` under the codex home the
    spawn resolves (:func:`codex_transcripts.codex_home` over the start's
    ``extra_env``, *env_overrides*), and never from Kiro's
    sign-in, so the Kiro era says nothing about a codex process and is empty for a
    codex-only operator. The era names the ACCOUNT in that file, not the file's
    bytes: codex rewrites ``tokens`` and ``last_refresh`` there on every token
    refresh (codex-rs ``login/src/auth/manager.rs:persist_tokens``), so a digest of
    the whole file moved on each refresh and a later chat could never join. A
    ChatGPT sign-in is ``tokens.account_id`` plus the ``id_token``'s user id, which
    a refresh keeps; an API key or personal access token is told apart by that key,
    which only a new sign-in changes. Another account then founds a new process
    instead of joining one holding the old login.

    No file is ``""`` too: the adapter then reads an API key from the environment,
    and a running gateway does change that (``config/live.py`` applies an edited
    ``.env`` key), so a home-only answer would let a new chat join a process still
    holding the old key. A file that cannot be read or parsed, or names no account
    this can tell apart, is ``""`` as well, which the placement turns into a key
    nothing can match: fail closed. So is a home that does not resolve (relative,
    or no home directory).
    """
    home = codex_home(env_overrides)
    if home is None:
        return ""
    try:
        data = json.loads((home / "auth.json").read_bytes())
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        account = tokens.get("account_id")
        if not isinstance(account, str) or not account:
            return ""
        user = _codex_jwt_claim_user(tokens.get("id_token"))
        stable = f"chatgpt\0{account}\0{user}"
    else:
        key = data.get("OPENAI_API_KEY") or data.get("personal_access_token")
        if not isinstance(key, str) or not key:
            return ""
        stable = f"key\0{key}"
    # The home is part of it: it holds the config the process loads and the store
    # its threads go to, and a live ``.env`` reload can move ``CODEX_HOME`` to
    # another directory holding the same login.
    stable = f"{home}\0{stable}"
    # A random token per distinct account, drawn the first time this process sees
    # it: the era only has to be equal within this process (the ownership table is
    # in memory), and a random token carries nothing of the key, so no digest of
    # an API key exists to be guessed against.
    token = _CODEX_ERA_TOKENS.get(stable)
    if token is None:
        token = _CODEX_ERA_TOKENS.setdefault(stable, secrets.token_hex(16))
    return "codex:" + token


#: :func:`claude_spawn_identity`'s random token per sign-in seen by this process.
#: shortcut: never pruned, as :data:`_CODEX_ERA_TOKENS`; prune if sign-ins churn.
_CLAUDE_ERA_TOKENS: dict[str, str] = {}

#: Prefixes of the environment variables that choose how a claude process signs in:
#: Anthropic's API (``ANTHROPIC_*``), Claude Code's own switches and token
#: (``CLAUDE_CODE_*``, ``CLAUDE_CODE_OAUTH_TOKEN`` among them, the namespace the
#: sandbox passes to claude children), and Bedrock's AWS identity (``AWS_*``).
_CLAUDE_CREDENTIAL_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "AWS_")
#: Vertex AI's region and credential, which carry no common prefix.
_CLAUDE_CREDENTIAL_ENV = ("CLOUD_ML_REGION", "GOOGLE_APPLICATION_CREDENTIALS")


def claude_spawn_identity(
    environ: Mapping[str, str] | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> str:
    """The account era of a claude start: which Claude Code sign-in a new process uses.

    claude-agent-acp signs in through Claude Code, never from Kiro's sign-in, so the
    Kiro era says nothing about a claude process and is empty for a claude-only
    operator. Claude Code records the signed-in account as ``oauthAccount`` in its
    global config, ``.claude.json`` in ``$CLAUDE_CONFIG_DIR`` (``~/.claude.json``
    when unset), on every platform, including macOS where the token itself sits in
    the keychain. The era names that account's ids, so a sign-in to another account
    founds a new process instead of joining one holding the old token.

    Not ``.credentials.json``: Claude Code rewrites that file on every token refresh
    and every MCP server sign-in, so its digest moved between two chats a few
    seconds apart and no chat ever joined another.

    The environment the process inherits is part of it: an API key, a token, a base
    URL or a cloud provider and its identity (:data:`_CLAUDE_CREDENTIAL_ENV_PREFIXES`,
    :data:`_CLAUDE_CREDENTIAL_ENV`), read from the gateway's own environment with the
    start's ``extra_env`` (*env_overrides*) over it. A running gateway changes that
    environment when an edited ``.env`` is applied (``config/live.py``), so a new
    chat after the edit founds a new process instead of joining one holding the old
    credential. A config file that exists but cannot be read or parsed is ``""``,
    which the placement turns into a key nothing can match: fail closed.
    """
    env = {**(os.environ if environ is None else environ), **(env_overrides or {})}
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    path = Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        data = None
    except (OSError, ValueError):
        return ""
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    signed_in: list[Any] | str
    if isinstance(account, dict):
        signed_in = [account.get("accountUuid"), account.get("organizationUuid")]
    elif data is not None:
        signed_in = "no-account"
    else:
        signed_in = "no-config"
    credentials = sorted(
        (name, value)
        for name, value in env.items()
        if name.startswith(_CLAUDE_CREDENTIAL_ENV_PREFIXES) or name in _CLAUDE_CREDENTIAL_ENV
    )
    stable = json.dumps([str(path), signed_in, credentials])
    # A random token per distinct sign-in, as codex's: equal within this process,
    # and carrying nothing of a key that could be guessed against.
    token = _CLAUDE_ERA_TOKENS.get(stable)
    if token is None:
        token = _CLAUDE_ERA_TOKENS.setdefault(stable, secrets.token_hex(16))
    return "claude:" + token


def _self_signed_era(backend: str, env_overrides: Mapping[str, str] | None = None) -> str | None:
    """The account era of a harness that signs in on its own, or None for one that
    reads Kiro's sign-in. *env_overrides* is the start's ``extra_env``, which can
    move codex's home and carry claude's credential variables. Looked up by name at call time, so a patch of either reader
    reaches it."""
    if backend == ACP_BACKEND_CODEX:
        return codex_spawn_identity(env_overrides)
    if backend == ACP_BACKEND_CLAUDE:
        return claude_spawn_identity(env_overrides=env_overrides)
    if backend == ACP_BACKEND_GOOSE:
        return goose_spawn_identity(env_overrides=env_overrides)
    if backend == ACP_BACKEND_DEEPSEEK:
        return deepseek_spawn_identity(env_overrides=env_overrides)
    return None


#: :func:`deepseek_spawn_identity`'s random token per credential set seen by this
#: process. shortcut: never pruned, like ``_CODEX_ERA_TOKENS``; one entry per vault
#: write of a mapped key, which stays a handful. Prune if rotations ever churn.
_DEEPSEEK_ERA_TOKENS: dict[str, str] = {}

#: The provider-key name dsh's own DeepSeek route reads by default.
_DEEPSEEK_DEFAULT_KEY_ENV = "DEEPSEEK_API_KEY"

#: The non-secret config files of a dsh home that can name another key variable
#: (``apiKeyEnv``): the home layer and the ACP profile's own layers.
_DSH_CONFIG_FILES = (
    "cordis.patch.yml",
    "profiles/acp/cordis.patch.yml",
    "profiles/acp/cordis.yml",
)


def _dsh_home(env: Mapping[str, str]) -> Path | None:
    """The dsh home a child launched with *env* reads, or None when it cannot be named."""
    home = env.get("DSH_HOME")
    if home:
        path = Path(home)
        return path if path.is_absolute() else None
    user_home = env.get("HOME")
    return Path(user_home) / ".dsh" if user_home else None


def _dsh_key_env_names(home: Path) -> set[str] | None:
    """Every provider-key variable name dsh's config under *home* reads, or None.

    ``DEEPSEEK_API_KEY`` plus each ``apiKeyEnv`` the home and ACP profile layers
    name. Only the config layers are read, never ``.credentials.yaml`` or ``.env``.
    A layer that exists but cannot be read or parsed is None: fail closed.
    """
    import yaml  # type: ignore[import-untyped]

    names = {_DEEPSEEK_DEFAULT_KEY_ENV}

    def _walk(node: object, depth: int) -> None:
        if depth > 32:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "apiKeyEnv" and isinstance(value, str) and value:
                    names.add(value)
                else:
                    _walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)

    for relative in _DSH_CONFIG_FILES:
        try:
            text = (home / relative).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            return None
        try:
            _walk(yaml.safe_load(text), 0)
        except yaml.YAMLError:
            return None
    return names


def deepseek_spawn_identity(
    environ: Mapping[str, str] | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> str:
    """The account era of a deepseek start: which provider key a new process holds.

    dsh never reads Kiro's sign-in. Crew hands it its provider key from Crew's own
    vault (``agent.deepseek_env``: variable name -> ``secret://`` reference), and
    both of dsh's own credential files stay masked. The era names the dsh home, the
    mapping, and the vault REVISION of each referenced secret
    (:meth:`SecretVault.list_items`): read from the store in the clear, without
    decrypting, and changed by every write. So a rotated key, a remapped name or
    another home founds a new process instead of joining one that holds the old
    key, and the key itself is never read here.

    A key the child would take from its inherited environment instead (a variable
    dsh reads -- ``DEEPSEEK_API_KEY`` or any ``apiKeyEnv`` the home's config names --
    that the mapping does not set) is ``""``: its rotation cannot be told without
    reading it, so such a chat never shares. So is a mapping Crew would refuse, a
    home that cannot be named, and a config or vault that cannot be read. The
    placement turns ``""`` into a key nothing can match: fail closed.

    With no *environ*, the environment dsh is launched with: the gateway's, under
    ``agent.env``. *env_overrides* is the start's ``extra_env``, over either.
    """
    from kiro_crew.acp.harness.deepseek import _validate_deepseek_env_mapping
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import config_dir
    from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX
    from kiro_crew.secrets import SecretVault
    from kiro_crew.secrets.items import parse_reference

    if environ is None:
        from kiro_crew.acp.agent_env import agent_env_overlay

        env: dict[str, str] = {**os.environ, **agent_env_overlay()}
    else:
        env = dict(environ)
    env.update(env_overrides or {})
    home = _dsh_home(env)
    if home is None:
        return ""
    key_names = _dsh_key_env_names(home)
    if key_names is None:
        return ""
    try:
        mapping = dict(KiroCrewConfig.load().agent.deepseek_env)
        if mapping:
            _validate_deepseek_env_mapping(mapping)
    except (OSError, ValueError):
        return ""
    if any(env.get(name) for name in key_names - set(mapping)):
        return ""
    revisions: list[tuple[str, str]] = []
    if mapping:
        try:
            stored = {
                row["name"]: row["revision"] for row in SecretVault(config_dir()).list_items()
            }
        except (OSError, ValueError):
            return ""
        for ref in sorted(mapping.values()):
            name, _field = parse_reference(ref[len(SECRET_URI_PREFIX) :])
            revision = stored.get(name)
            if revision is None:
                return ""
            revisions.append((name, revision))
    stable = json.dumps([str(home), sorted(mapping.items()), revisions])
    # A random token per distinct value, as codex does: equal within this process,
    # and carrying nothing of the vault entry.
    token = _DEEPSEEK_ERA_TOKENS.get(stable)
    if token is None:
        token = _DEEPSEEK_ERA_TOKENS.setdefault(stable, secrets.token_hex(16))
    return "deepseek:" + token


def goose_spawn_identity(
    environ: Mapping[str, str] | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> str:
    """The account era of a goose start: which model provider a new process talks to.

    goose never reads Kiro's sign-in. It resolves its provider from
    ``GOOSE_PROVIDER`` (the environment over its ``config.yaml``) and the provider's
    endpoint from that provider's ``*_HOST`` keys; its key comes from the
    environment first, else the system keyring or ``secrets.yaml``. The era names
    the goose home, the provider, those hosts and the credential the environment
    hands the child, so another home, provider or endpoint, or a rotated
    environment key, founds a new process instead of joining one that holds the
    old one.

    Not the whole ``config.yaml`` and not ``secrets.yaml``: goose writes config keys
    (the last model chosen among them) while it runs, and a token refresh rewrites
    the secrets, so a digest of either would move between two chats and no chat
    would ever join another.

    A config that cannot be read or parsed, or no provider at all, is ``""``,
    which the placement turns into a key nothing can match: fail closed.

    With no *environ*, the environment goose is launched with: the gateway's, under
    ``agent.env`` (which can name the provider, and which a chat's child takes).
    *env_overrides* is the start's ``extra_env``, over either.
    """
    import yaml  # type: ignore[import-untyped]

    from kiro_crew.acp.harness.goose import goose_config_dir

    if environ is None:
        from kiro_crew.acp.agent_env import agent_env_overlay

        env: dict[str, str] = {**os.environ, **agent_env_overlay()}
    else:
        env = dict(environ)
    env.update(env_overrides or {})
    path = goose_config_dir(env) / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return ""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return ""
    provider = env.get("GOOSE_PROVIDER") or data.get("GOOSE_PROVIDER")
    if not isinstance(provider, str) or not provider:
        return ""
    # Every endpoint key goose could read, from the file and the environment alike,
    # the environment winning as goose resolves it. An unrelated ``*_HOST`` in the
    # environment only makes the key stricter.
    host_keys = {key for key in (*data, *env) if isinstance(key, str) and key.endswith("_HOST")}
    hosts = sorted((key, str(env.get(key) or data.get(key))) for key in host_keys)
    # The credential the child inherits from the environment, where goose looks a
    # secret up first: a running gateway changes it (``config/live.py`` applies an
    # edited ``.env`` key), and a running child keeps the old one, so a rotated key
    # founds a new process instead of joining one holding the revoked key. An
    # unrelated variable with such an ending only makes the key stricter.
    creds = sorted(
        (key, value) for key, value in env.items() if key.endswith(_GOOSE_CREDENTIAL_ENDINGS)
    )
    # Which goose home the child runs on: the resolved config path, and the
    # variables goose finds its data and state directories from (its session store
    # among them). A live ``.env`` reload can move any of them, and a running child
    # keeps the old home, so another home founds a new process.
    homes = [(key, env.get(key, "")) for key in _GOOSE_HOME_VARIABLES]
    stable = json.dumps([str(path), homes, provider, hosts, creds])
    # A random token per distinct value, as codex does: the era only has to be equal
    # within this process, and a random token carries nothing of the key, so no
    # digest of an API key exists to be guessed against.
    token = _GOOSE_ERA_TOKENS.get(stable)
    if token is None:
        token = _GOOSE_ERA_TOKENS.setdefault(stable, secrets.token_hex(16))
    return "goose:" + token


#: How many placements a start makes while a keyed input keeps moving between the
#: read and the acquisition. Small on purpose: an input still moving after this
#: many rounds is being rewritten continuously, and the last round founds this
#: session its own process instead.
_PLACEMENT_ATTEMPTS = 3

#: Prefixes of a spec generation that is a token unique to one read
#: (``agent_spec_generation`` for an unreadable or unresolved spec, and the
#: give-up below). A key carrying one can never be joined, and a re-read never
#: equals it, so it is not re-compared.
_UNIQUE_GENERATION_PREFIXES = ("unobservable-", "unresolved-", "unsettled-")


async def _settle_release(lease: str) -> AcpRuntime | None:
    """Release *lease* and return the runtime the release hands back, through any cancellation.

    The release runs as one task this frame waits on in a shield loop, so a
    second cancellation cannot strand the lease the first one was unwinding. The
    caller re-raises its own exception afterwards.
    """
    task = asyncio.ensure_future(RUNTIME_OWNERSHIP.release(lease))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return cast("AcpRuntime | None", task.result())


async def _kill_stranded(runtime: AcpRuntime | None, reason: str) -> None:
    """Kill a runtime a release handed back (this start held its only lease)."""
    if runtime is None:
        return
    try:
        await runtime.kill(expected=True, reason=reason)
    except Exception:
        logger.debug("chat runtime sharing: stranded-runtime kill failed", exc_info=True)


def _names_owner(tree: Path, pid: int) -> bool:
    """Does *tree*'s owner marker, read back the way the sweep reads it, name *pid*?

    ``adopt_owner`` answers ``"recorded"`` without writing when the tree has no
    marker, so its answer alone does not show the sweep will see *pid*.
    """
    from kiro_crew import agent_scratch

    try:
        pids = agent_scratch._read_owner_pids(tree / agent_scratch.OWNER_FILENAME)
    except (OSError, ValueError, agent_scratch.ScratchBoundaryError):
        return False
    return pid in pids


class ChatSharePlacement:
    """One eligible chat start's placement: its key, its lease, and whether it joined."""

    def __init__(
        self,
        provider: AcpProvider,
        *,
        work_dir: str | Path,
        agent: str,
        sandbox_mode: str,
        extra_env: dict[str, str],
        mcp_gateway_overlay: Any,
        mcp_gateway_socket: Any,
        configured_model: str,
        launch_agent: str,
        session_key: str,
        cap: int,
        spec_generation: str,
        sandbox_tier: str,
        forward_ssh: bool,
        agent_env: dict[str, str] | None = None,
        kas_telemetry: bool = True,
        tree_root: str = "",
        member_scope: tuple[str, str] = ("", ""),
        tool_surface_pinned: bool = False,
        harness_era: str | None = None,
    ) -> None:
        self._provider = provider
        # The sharing scope, read once at this start: sessions share only inside
        # one tree, and a Bot session only with its own member and store.
        self._tree_root = tree_root or session_key
        self._member_scope = member_scope
        # An agent whose spec decides the process's Tool Search keeps its agent and
        # spec keyed (``agent_pins_tool_surface``), so it never joins another spec.
        self._tool_surface_pinned = tool_surface_pinned
        # A harness that takes the spec per session does not key it, so it is not re-read.
        self._keys_spec = tool_surface_pinned or "spec_generation" not in per_session_fields(
            provider._client.backend
        )
        # codex and claude read their own credential, never Kiro's, so their era is
        # their own (:func:`_self_signed_era`), read and re-read in place of Kiro's.
        self._self_signed_backend = (
            str(getattr(getattr(provider, "_client", None), "backend", "") or "")
            if harness_era is not None
            else ""
        )
        # ``agent.env`` as this start read it: a child takes it at spawn and never
        # re-reads it, so a saved change must found a new process rather than join
        # one that carries the old values. A founding spawn launches with this same
        # snapshot (``spawn_agent_env``), so a process always carries what its key says.
        self._agent_env = dict(agent_env or {})
        # The telemetry opt-out as this start read it (KAS only; True elsewhere). KAS
        # takes it once at ``initialize``, so a founding spawn sends this same value
        # (``spawn_kas_telemetry``) and a process always carries what its key says.
        self._kas_telemetry = kas_telemetry
        self._work_dir = work_dir
        self._agent = agent
        self._sandbox_mode = sandbox_mode
        self._extra_env = extra_env
        self._overlay = mcp_gateway_overlay
        self._socket = mcp_gateway_socket
        self._model = configured_model
        self._launch_agent = launch_agent
        self.session_key = session_key
        self.cap = cap
        self._spec_generation = spec_generation
        self._sandbox_tier = sandbox_tier
        self._forward_ssh = forward_ssh
        # The RAW era this start observed. An empty read (no reader wired, or a
        # failed read) never becomes an empty KEY field: two failed reads would
        # compare equal and join across accounts. It becomes a token unique to
        # this start, which no other key can equal, so the session founds its
        # own process. The confirm compares raw against raw.
        self._raw_era = (
            harness_era
            if harness_era is not None
            else str(getattr(provider, "pre_spawn_identity", "") or "")
        )
        self._era = self._raw_era or f"unverified-{uuid.uuid4().hex}"
        self._identity_reader = getattr(provider, "spawn_identity_reader", None)
        # False once placement gave up and made the key unique: nothing can join
        # it, so the spawn reads the forwarding consent fresh instead of the
        # frozen one.
        self.joinable = True
        self.lease: str | None = None
        self.joined = False
        self.leases_on_runtime = 0
        self.key = self._build_key()

    def _build_key(self) -> ChatRuntimeKey:
        """This session's key from the state as it stands NOW."""
        p = self._provider
        return ChatRuntimeKey.build(
            work_dir=self._work_dir,
            agent=self._agent,
            sandbox_mode=self._sandbox_tier,
            spawn_sandbox_mode=self._sandbox_mode,
            extra_env=self._extra_env,
            acp_backend=p._client.backend,
            tool_search=p._tool_search_settings(),
            memory_mode=p.memory_mode,
            mcp_gateway_overlay=self._overlay,
            mcp_gateway_socket=self._socket,
            model=self._model,
            reasoning_effort=p._resolve_effort(),
            spawn_identity=self._era,
            spec_generation=self._spec_generation,
            forward_ssh_auth_sock=self._forward_ssh,
            launch_agent=self._launch_agent,
            harness_pin=getattr(p, "_harness_pin", "") or "",
            agent_env=self._agent_env,
            kas_telemetry=self._kas_telemetry,
            tree_root=self._tree_root,
            member_id=self._member_scope[0],
            memory_store=self._member_scope[1],
            tool_surface_pinned=self._tool_surface_pinned,
        )

    @property
    def tree_kind(self) -> str:
        """``bot`` for a Bot's tree, ``chat`` otherwise: the placement metric's label."""
        return "bot" if self._member_scope[0] else "chat"

    def spawn_agent_env(self) -> dict[str, str]:
        """The ``agent.env`` snapshot a founding spawn launches with: the one keyed on."""
        return dict(self._agent_env)

    def spawn_kas_telemetry(self) -> bool:
        """The telemetry opt-out a founding KAS spawn sends: the one keyed on."""
        return self._kas_telemetry

    def spawn_forward_ssh(self) -> bool | None:
        """The forwarding request a founding spawn uses: frozen on a joinable key, else fresh."""
        return self._forward_ssh if self.joinable else None

    async def _read_confirmation(self) -> tuple[str, str, str, bool, bool]:
        """Re-read the five unsheddable inputs after the acquisition."""
        from kiro_crew.kiro_prerequisite import pre_spawn_identity
        from kiro_crew.sandbox import _forward_ssh_auth_sock, effective_sandbox_mode

        generation = (
            await asyncio.to_thread(agent_spec_generation, self._work_dir, self._agent)
            if self._keys_spec
            else ""
        )
        if self._self_signed_backend:
            era = (
                await asyncio.to_thread(
                    _self_signed_era, self._self_signed_backend, self._extra_env
                )
                or ""
            )
        elif self._identity_reader is not None:
            era = await pre_spawn_identity(self._identity_reader)
        else:
            era = self._raw_era
        forward = await asyncio.to_thread(_forward_ssh_auth_sock, self._sandbox_mode, ())
        tier = await asyncio.to_thread(effective_sandbox_mode, self._sandbox_mode)
        telemetry = await asyncio.to_thread(
            _kas_telemetry_for,
            self._provider._client.backend,
            self._work_dir,
            self._agent_env,
            self._extra_env,
        )
        return generation, era, tier, forward, telemetry

    async def _admit_joiner(self, placed: Acquisition) -> None:
        """Run the fork-governance admission a founder ran inside ``spawn()``.

        A joiner lands on a live runtime without spawning, so it never passed
        that gate. A fork whose refresh failed leaves the spec bytes (and so the
        key) unchanged, which is exactly when a joiner matches. Refused or
        cancelled, the joiner's lease is released before the error propagates.
        """
        from kiro_crew.agent import ForkGovernanceUnresolved, require_fork_governance

        try:
            await asyncio.to_thread(require_fork_governance, self._agent, self._work_dir)
        except ForkGovernanceUnresolved as exc:
            await _kill_stranded(
                await _settle_release(placed.lease), "chat runtime sharing: join refused"
            )
            raise AcpRuntimeError(str(exc)) from exc
        except BaseException:
            await _kill_stranded(
                await _settle_release(placed.lease), "chat runtime sharing: join cancelled"
            )
            raise

    async def place(
        self,
        spawn: Callable[[], Awaitable[AcpRuntime]],
        recorded_scratch: Path | None = None,
    ) -> AcpRuntime:
        """Take a lease on a compatible runtime (or found one), then confirm the placement.

        *recorded_scratch* is the directory a resumed chat was recorded in. It is
        held for the sweep's grace window before the acquisition
        (:func:`~kiro_crew.agent_scratch.shared_scratch_window`, as a spawn holds
        a tree it mounts), and a join that does not mount it adopts it at once
        (:meth:`_keep_unmounted_scratch`).
        """
        if recorded_scratch is not None:
            from kiro_crew import agent_scratch

            await asyncio.to_thread(agent_scratch.shared_scratch_window, recorded_scratch)
        runtime = await self._place(spawn)
        try:
            return await self._keep_unmounted_scratch(recorded_scratch, runtime, spawn)
        except BaseException:
            # The lease is already held (``_accept``) and the caller's release
            # path is not reached from here, so a failure or cancellation in the
            # owner update releases it, through any cancellation, before it
            # propagates -- as ``_admit_joiner`` does.
            lease, self.lease = self.lease, None
            if lease is not None:
                await _kill_stranded(
                    await _settle_release(lease),
                    "chat runtime sharing: scratch retention cancelled",
                )
            raise

    async def _place(self, spawn: Callable[[], Awaitable[AcpRuntime]]) -> AcpRuntime:
        """Take a lease on a compatible runtime (or found one), then confirm the placement."""
        for _ in range(_PLACEMENT_ATTEMPTS):
            placed = await RUNTIME_OWNERSHIP.acquire(
                self.key, self.session_key, spawn, cap=self.cap
            )
            try:
                generation, era, tier, forward, telemetry = await self._read_confirmation()
                # A KAS key carries no spec, so the spec-decided admission (native
                # subagents, tool surface) is read again here, inside the same
                # release guard.
                native = not self._keys_spec and await self._spec_needs_own_process_now()
            except BaseException:
                await _kill_stranded(
                    await _settle_release(placed.lease),
                    "chat runtime sharing: placement cancelled before confirm",
                )
                raise
            # An empty re-read is not evidence the account held: it fails closed
            # to a retry, where the empty era becomes a unique token. A key whose
            # pre-read era was already empty carries such a token and could
            # never have joined, so it is not re-compared.
            era_holds = (not self._raw_era) or (bool(era) and era == self._raw_era)
            generation_holds = (
                self._spec_generation.startswith(_UNIQUE_GENERATION_PREFIXES)
                or generation == self._spec_generation
            )
            if (
                generation_holds
                and era_holds
                and tier == self._sandbox_tier
                and forward == self._forward_ssh
                and telemetry == self._kas_telemetry
            ):
                if native:
                    # The spec changed after the admission read in a way that
                    # fixes the process (native subagents, tool surface), so this
                    # session takes a process nothing else can join.
                    await _kill_stranded(
                        await _settle_release(placed.lease),
                        "chat runtime sharing: spec changed during placement",
                    )
                    return await self._place_alone(spawn, "spec changed during placement")
                if placed.joined:
                    await self._admit_joiner(placed)
                return self._accept(placed)
            logger.info(
                "chat runtime sharing: a keyed input moved during placement "
                "(spec=%s era=%s tier=%s forward=%s telemetry=%s); retrying on the fresh key",
                not generation_holds,
                not era_holds,
                tier != self._sandbox_tier,
                forward != self._forward_ssh,
                telemetry != self._kas_telemetry,
            )
            await _kill_stranded(
                await _settle_release(placed.lease),
                "chat runtime sharing: placement moved before confirm",
            )
            self._spec_generation = generation
            self._raw_era = era
            self._era = era or f"unverified-{uuid.uuid4().hex}"
            self._sandbox_tier = tier
            self._forward_ssh = forward
            self._kas_telemetry = telemetry
            self.key = self._build_key()
        return await self._place_alone(
            spawn, f"keyed inputs kept moving across {_PLACEMENT_ATTEMPTS} placements"
        )

    async def _spec_needs_own_process_now(self) -> bool:
        """Re-read the spec-decided admission after the acquisition (fails closed).

        True when the spec now grants native subagents, or now fixes the process's
        tool surface (``agent_pins_tool_surface``) though the key was built without.
        """
        backend = self._provider._client.backend
        try:
            if await asyncio.to_thread(
                agent_spawns_native_subagents, self._work_dir, self._agent, backend
            ):
                return True
            if not keys_tool_surface(backend):
                return False
            return await asyncio.to_thread(
                agent_pins_tool_surface,
                self._work_dir,
                self._agent,
                self._provider._tool_search_settings(),
            )
        except Exception:  # noqa: BLE001 - unreadable admission keeps its own process
            return True

    async def _place_alone(
        self, spawn: Callable[[], Awaitable[AcpRuntime]], why: str
    ) -> AcpRuntime:
        """Found this session a process under a key nothing can match."""
        # A unique spec generation and tree root: a KAS key leaves the spec
        # generation out, so only a unique root makes that key unmatchable.
        self._spec_generation = f"unsettled-{uuid.uuid4().hex}"
        self._tree_root = self._spec_generation
        self.joinable = False
        self.key = self._build_key()
        logger.warning("chat runtime sharing: %s; this session takes its own process", why)
        placed = await RUNTIME_OWNERSHIP.acquire(self.key, self.session_key, spawn, cap=self.cap)
        return self._accept(placed)

    def _accept(self, placed: Acquisition) -> AcpRuntime:
        self.lease = placed.lease
        self.joined = placed.joined
        self.leases_on_runtime = placed.leases_on_runtime
        runtime = cast("AcpRuntime", placed.runtime)
        if self._provider._client.backend == ACP_BACKEND_CODEX:
            # codex starts an MCP fleet per session that ``session/close`` leaves
            # running (``acp/harness/codex.py``), so a shared codex process stops
            # taking chats once one leaves and dies with its last chat: at most
            # cap-1 departed fleets, never one per chat turnover.
            runtime.chat_closes_on_departure = True
        # One line per placement at WARNING, beside the refusal line: an operator
        # counting processes needs both. The key is not logged (paths, env).
        logger.warning(
            "chat-runtime-sharing %s runtime=%s tenants=%d session=%s",
            "joined" if placed.joined else "spawned",
            runtime.pid,
            placed.leases_on_runtime,
            self.session_key,
        )
        record_placed(
            joined=placed.joined,
            tenants=placed.leases_on_runtime,
            tree=self.tree_kind,
            backend=self._provider._client.backend,
        )
        if not placed.joined:
            _log_why_not_joined(self.key, self.session_key, runtime)
        return runtime

    async def _keep_unmounted_scratch(
        self,
        recorded: Path | None,
        runtime: AcpRuntime,
        spawn: Callable[[], Awaitable[AcpRuntime]],
    ) -> AcpRuntime:
        """Keep a joiner's recorded scratch directory alive as long as the tree's process.

        A JOINER runs in the process's own directory, so the one it recorded
        before (a resumed chat's old tree) is not mounted and its record
        moves to the process's directory. Left alone, that old directory's
        owner is a process that has exited, and the sweep reclaims it an hour
        later. The shared process is added as its owner instead
        (:func:`~kiro_crew.agent_scratch.adopt_owner`), so it lives exactly as
        long as the tree's process and is then swept by the ordinary rules.

        Runs right after the placement, before any session setup, so the hold
        :meth:`place` took on the directory covers the gap. The join stands only
        once the marker is read back naming the shared process. Any other answer
        -- no marker to join (``"recorded"`` on an unowned tree), a write that
        failed (``"unwritable"``, ``"stale"``), a planted link (``"refused"``) or
        a marker this module did not write (``"garbled"``) -- gives up the join,
        and this session founds its own process, which mounts its directory.
        """
        live = getattr(runtime, "work_scratch_dir", None)
        pid = runtime.pid
        if not self.joined or not isinstance(recorded, Path) or not isinstance(live, Path):
            return runtime
        if pid is None or recorded == live or not await asyncio.to_thread(recorded.is_dir):
            return runtime
        from kiro_crew import agent_scratch

        outcome = await asyncio.to_thread(agent_scratch.adopt_owner, recorded, pid)
        if outcome != "recorded" or not await asyncio.to_thread(_names_owner, recorded, pid):
            lease, self.lease = self.lease, None
            if lease is not None:
                await _kill_stranded(
                    await _settle_release(lease),
                    "chat runtime sharing: recorded scratch could not be kept",
                )
            return await self._place_alone(
                spawn,
                f"session {self.session_key} could not keep its scratch directory "
                f"{recorded.name!r} on a shared runtime (owner update: {outcome})",
            )
        logger.warning(
            "chat runtime sharing: session %s joined runtime=%s; its earlier scratch "
            "directory %r is not mounted and stays on disk while that runtime runs "
            "(owner update: %s)",
            self.session_key,
            runtime.pid,
            recorded.name,
            outcome,
        )
        return runtime

    async def replace_dead(
        self,
        spawn: Callable[[], Awaitable[AcpRuntime]],
        recorded_scratch: Path | None = None,
    ) -> AcpRuntime:
        """Place again after the runtime died during a resume.

        The lease on the corpse is released first, so the table forgets the dead
        entry, and the key is rebuilt. Through the same confirmed placement as the
        cold start.
        """
        if self.lease is not None:
            lease, self.lease = self.lease, None
            await _settle_release(lease)
        self.key = self._build_key()
        return await self.place(spawn, recorded_scratch)

    async def abandon(self, runtime: AcpRuntime, session_id: str | None) -> None:
        """Undo a start that failed after placement, without ending co-tenants.

        The session (when one was created) is evicted from the process, keeping
        its record, and the lease released; the runtime is killed only when this
        start held its last lease. Runs to completion through any cancellation: a
        cancellation landing in the eviction still returns the lease (the release
        is in a ``finally``) and is re-raised after it, so the process never leaks.
        """
        try:
            if session_id:
                try:
                    if getattr(runtime, "acp_backend", None) == ACP_BACKEND_KAS:
                        await runtime.evict_session(session_id)
                    else:
                        await runtime.terminate_session(session_id)
                except Exception:
                    logger.debug(
                        "chat runtime sharing: failed-start eviction failed", exc_info=True
                    )
        finally:
            lease, self.lease = self.lease, None
            if lease is not None:
                await _kill_stranded(
                    await _settle_release(lease), "failed session setup cleanup (last holder)"
                )


def _log_why_not_joined(key: ChatRuntimeKey, session_key: str, runtime: object) -> None:
    """One INFO line naming which key fields keep a founded start off each live process.

    Only field names and whether each side is empty: a key holds paths, env and
    an account era, none of which belong in a log. A candidate is a live process
    of the same session tree; with none, the line says so.
    """
    try:
        names = [f.name for f in dataclasses.fields(key)]
        candidates = [
            other
            for other in RUNTIME_OWNERSHIP.live_keys(exclude=runtime)
            if isinstance(other, ChatRuntimeKey) and other.tree_root == key.tree_root
        ]
        if not candidates:
            logger.info(
                "chat-runtime-sharing founded: no live process in this tree session=%s",
                session_key,
            )
            return
        for i, other in enumerate(candidates):
            diffs = [
                f"{n}(ours={'empty' if not getattr(key, n) else 'set'},"
                f"theirs={'empty' if not getattr(other, n) else 'set'})"
                for n in names
                if getattr(key, n) != getattr(other, n)
            ]
            logger.info(
                "chat-runtime-sharing founded: differs from candidate %d in %s session=%s",
                i,
                ", ".join(diffs) or "nothing (full or closed)",
                session_key,
            )
    except Exception:  # noqa: BLE001 - a diagnostic never fails a placement
        logger.debug("chat-runtime-sharing: key diff log failed", exc_info=True)


def _kas_telemetry_for(
    backend: str, work_dir: str | Path, agent_env: dict[str, str], extra_env: dict[str, str]
) -> bool:
    """Whether a KAS process founded for this start would send telemetry on; True elsewhere.

    Judged on the child's environment in launch's order (the gateway's, agent.env,
    then extra_env), as ``_handshake_client_capabilities`` judges it: KAS takes the
    opt-out once, at ``initialize``, so a joiner cannot shed a founder's value.
    """
    if backend == ACP_BACKEND_KAS:
        from kiro_crew.acp.kas_cli_settings import kiro_cli_telemetry_enabled

        return kiro_cli_telemetry_enabled(work_dir, {**os.environ, **agent_env, **extra_env})
    # Every other backend sends no opt-out at initialize, so all its keys agree.
    return True


def _seeds_auto_permission_mode(provider: Any, backend: str) -> bool:
    """Whether this chat's session would start in Claude Code's ``auto`` mode.

    In ``auto`` the backend's classifier approves calls without asking, and only the
    per-chat client writes the audit record for those calls
    (``client_tool_events._maybe_audit_unasked_tool_call``), so such a chat keeps its
    own process.
    shortcut: give the shared session handle that audit to lift this.
    """
    from kiro_crew.acp.types import CC_PERMISSION_MODE_AUTO
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKENDS_SEED_LOCAL_SETTINGS,
        CC_PERMISSION_MODE_ENV,
    )

    if backend not in ACP_BACKENDS_SEED_LOCAL_SETTINGS:
        return False
    # The answer ``resolve_cc_permission_mode`` gives, read without its warning for
    # an unrecognised value: the session's own start says that once.
    asked = getattr(getattr(provider, "_client", None), "_permission_mode", None)
    requested = (
        asked if isinstance(asked, str) and asked else os.environ.get(CC_PERMISSION_MODE_ENV)
    )
    return requested == CC_PERMISSION_MODE_AUTO


async def resolve_chat_share(
    provider: AcpProvider,
    *,
    work_dir: str | Path,
    agent: str,
    sandbox_mode: str,
    extra_env: dict[str, str],
    mcp_gateway_overlay: Any,
    mcp_gateway_socket: Any,
    configured_model: str,
    launch_agent: str,
    remote_workspace: bool,
) -> ChatSharePlacement | None:
    """This start's placement, or ``None`` to spawn its own process as before.

    ``None`` whenever the preview is off (one config read, nothing else), and for
    every start the eligibility rules refuse; a refusal is logged at WARNING only
    while the preview is on, naming the rule. Only an eligible start makes the
    placement reads (KAS engine readiness, spec generation, sandbox tier,
    forwarding consent), in one hop off the event loop.
    """
    try:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = live.snapshot()
        if cfg is None:
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
        enabled = chat_runtime_sharing_enabled(cfg)
        backend = provider._client.backend
        harness_enabled = chat_runtime_sharing_harness_enabled(cfg, backend)
        configured_cap = int(
            getattr(cfg.agent, "chat_runtime_sharing_max_sessions", CHAT_RUNTIME_CAP)
        )
    except Exception:
        # An unreadable config leaves sharing OFF: absence must not enable a
        # feature nobody turned on.
        logger.debug("chat runtime sharing: config unreadable", exc_info=True)
        return None
    if not enabled:
        return None
    session_key = provider._owning_session_key()
    # The rules that need no reads first, with the read-backed inputs at their
    # permissive values: a start they refuse (a backend that never shares, a
    # remote or incognito session) walks no tree and reads no member record. Each
    # read-backed rule only adds refusals, so this pass refuses nothing the full
    # one below would admit.
    refusal = chat_sharing_ineligible_reason(
        session_key=session_key,
        memory_mode=provider.memory_mode,
        sharing_enabled=enabled,
        backend=backend,
        member_context=provider.member_context,
        remote_workspace=remote_workspace,
        kas_engine_ready=True,
        member_shares_root=True,
        harness_enabled=harness_enabled,
    )
    if refusal:
        logger.warning(
            "chat-runtime-sharing ineligible reason=%s backend=%s session=%s",
            refusal,
            backend,
            session_key,
        )
        record_refused(refusal, backend=backend)
        return None
    # Read on the loop: the reader walks the gateway's live slots.
    tree_root = share_tree_root_of(session_key)
    kas_ready = False
    if backend == ACP_BACKEND_KAS and harness_enabled:
        kas_ready = await asyncio.to_thread(
            kas_chat_sharing_ready, getattr(provider, "_harness_pin", "") or ""
        )
    member_scope: tuple[str, str] = ("", "")
    member_shares_root = False
    if provider.member_context:
        # A Bot session shares only inside its own Bot's tree: the root must be
        # the same member on the same store. Both records read off the loop.
        member_scope, root_scope = await asyncio.to_thread(
            lambda: (member_scope_of(session_key), member_scope_of(tree_root))
        )
        member_shares_root = bool(member_scope[0]) and root_scope == member_scope
    refusal = chat_sharing_ineligible_reason(
        session_key=session_key,
        memory_mode=provider.memory_mode,
        sharing_enabled=enabled,
        backend=backend,
        member_context=provider.member_context,
        remote_workspace=remote_workspace,
        kas_engine_ready=kas_ready,
        member_shares_root=member_shares_root,
        harness_enabled=harness_enabled,
    )
    if not refusal and _seeds_auto_permission_mode(provider, backend):
        refusal = "auto_permission_mode"
    if not refusal and await asyncio.to_thread(
        agent_spawns_native_subagents, work_dir, agent, backend
    ):
        refusal = "native_subagents"
    if refusal:
        logger.warning(
            "chat-runtime-sharing ineligible reason=%s backend=%s session=%s",
            refusal,
            backend,
            session_key,
        )
        record_refused(refusal, backend=backend)
        return None
    from kiro_crew.acp.agent_env import agent_env_overlay
    from kiro_crew.sandbox import _forward_ssh_auth_sock, effective_sandbox_mode

    per_session = per_session_fields(backend)

    def _read_placement_inputs() -> tuple[str, str, bool, dict[str, str], bool, bool, str | None]:
        configured_env = agent_env_overlay()
        telemetry = _kas_telemetry_for(backend, work_dir, configured_env, extra_env)
        pinned = keys_tool_surface(backend) and agent_pins_tool_surface(
            work_dir, agent, provider._tool_search_settings()
        )
        return (
            (
                agent_spec_generation(work_dir, agent)
                if pinned or "spec_generation" not in per_session
                else ""
            ),
            effective_sandbox_mode(sandbox_mode),
            _forward_ssh_auth_sock(sandbox_mode, ()),
            configured_env,
            telemetry,
            pinned,
            _self_signed_era(backend, extra_env),
        )

    (
        generation,
        tier,
        forward,
        configured_env,
        telemetry,
        pinned,
        harness_era,
    ) = await asyncio.to_thread(_read_placement_inputs)
    return ChatSharePlacement(
        provider,
        work_dir=work_dir,
        agent=agent,
        sandbox_mode=sandbox_mode,
        extra_env=extra_env,
        mcp_gateway_overlay=mcp_gateway_overlay,
        mcp_gateway_socket=mcp_gateway_socket,
        configured_model=configured_model,
        launch_agent=launch_agent,
        session_key=session_key,
        cap=chat_runtime_cap(sharing_enabled=True, configured=configured_cap),
        spec_generation=generation,
        sandbox_tier=tier,
        forward_ssh=forward,
        agent_env=configured_env,
        kas_telemetry=telemetry,
        tree_root=tree_root,
        member_scope=member_scope,
        tool_surface_pinned=pinned,
        harness_era=harness_era,
    )


__all__ = [
    "ChatSharePlacement",
    "claude_spawn_identity",
    "codex_spawn_identity",
    "deepseek_spawn_identity",
    "goose_spawn_identity",
    "resolve_chat_share",
]
