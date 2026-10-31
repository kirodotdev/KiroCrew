"""The environment an agent process receives, and the policy that filters it.

``agent.env`` (its name policy -- grammar, denied names, prefixes and suffixes, the
credential class -- and its load-time refusals and scrub), ``agent.child_env_defaults``
and ``agent.deepseek_env``.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
import os as _os
import re as _re

from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX

logger = logging.getLogger("kiro_crew.config.loader")


def deepseek_env_plaintext_keys(raw: object) -> tuple[str, ...]:
    """Env-var names in an ``agent.deepseek_env`` shape whose value is NOT a reference.

    The one rule about this mapping that is enforced at WRITE time rather than at
    spawn: a value is a ``secret://<vault name>`` reference, and anything else is a
    provider key about to be persisted in ``config.json`` — the exposure the whole
    route exists to avoid. ``write_config_atomically`` refuses to publish a document
    whose write INTRODUCES one, or changes the mapping while one stays in it
    (:class:`kiro_crew.config.loader.ConfigWriteRefused`), so a plaintext typed into
    ``config set`` never reaches disk; a plaintext an older build already landed,
    left untouched by a write to some other field, publishes with the key named on
    the log, and the spawn-time validator repeats the check as the second line for
    a file edited by hand.

    Returns NAMES only, sorted, never values: the result is destined for an error
    message that must be safe on a terminal and in a log. A non-dict shape has no
    entries and returns empty; :func:`coerce_deepseek_env` is what drops it.
    """
    if not isinstance(raw, dict):
        return ()
    return tuple(
        sorted(
            str(key)
            for key, value in raw.items()
            if not (isinstance(value, str) and value.startswith(SECRET_URI_PREFIX))
        )
    )


def coerce_deepseek_env(raw: object) -> dict[str, str]:
    """Normalize ``agent.deepseek_env`` to a plain env-var-name -> value mapping.

    TYPE coercion ONLY, and that split is deliberate. What a name may BE for this
    mapping — a POSIX identifier, inside the harness's own child-environment scrub
    class, not a name Kiro Crew or the harness owns — is checked at SPAWN, in the
    DeepSeek arm, where a bad entry REFUSES the session with a message naming the
    offending env-var key (``acp/client.py``). Dropping such an entry here instead
    would hand the operator a harness with no provider key, a config file whose
    entry silently disappeared on the next write, and no error naming why. So the
    only thing refused here is a shape the dataclass cannot hold. The VALUE rule —
    a ``secret://`` reference, never a plaintext key — is the exception, enforced
    earlier still, at the publish floor (:func:`deepseek_env_plaintext_keys`),
    because a plaintext that reaches disk is already the defect.

    Nothing is stripped either: a name with surrounding whitespace is not a POSIX
    identifier, and the spawn-time refusal says so by name rather than quietly
    repairing it into a different variable than the operator wrote.
    """
    if not isinstance(raw, dict):
        return {}
    return {
        key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)
    }


#: The environment-variable names the Strands CLI reads for its model providers
#: (``@strands-agents/cli`` ``PROVIDER_ENVIRONMENT_KEYS``), and so the only names
#: a dashboard write may map under ``agent.strands_env``. The file-path variables
#: the CLI also reads (``AWS_CONFIG_FILE``, ``AWS_SHARED_CREDENTIALS_FILE``) are
#: left out because they would point the harness at a file of the writer's
#: choosing; the raw key pair (``AWS_ACCESS_KEY_ID`` and friends) because Kiro
#: Crew's agent environment scrub removes it from every agent child (Bedrock
#: travels as ``AWS_BEARER_TOKEN_BEDROCK``); and ``AWS_PROFILE`` because it names
#: a profile in ``~/.aws``, which the sandbox masks from the child. The one list:
#: the dashboard write check and the harness's spawn check both read it.
STRANDS_ENV_NAMES = frozenset(
    {
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OLLAMA_HOST",
        "OLLAMA_MODEL",
        "LITELLM_BASE_URL",
        "LITELLM_API_KEY",
        "LITELLM_MODEL",
    }
)


def validate_strands_env_patch(value: object) -> dict[str, str | None]:
    """Validate an ``agent.strands_env`` CHANGE written through the dashboard.

    The value is a change, not the whole map: each key it names is set to its
    reference, or removed when the value is ``None``, and every key it does not
    name is kept (:func:`merge_strands_env_patch`, applied under the config lock).
    So two saves of different variables cannot each write back a map without the
    other's entry, whatever order they land in.

    Stricter than :func:`coerce_deepseek_env` on purpose: a dashboard write is
    refused, not repaired, so nothing a person did not see lands on disk. Every
    key must be one of :data:`STRANDS_ENV_NAMES` and every value ``None`` or a
    ``secret://<vault name>`` reference whose name can round-trip through the
    vault (non-empty, no surrounding whitespace). The publish floor refuses a
    plaintext value again at write time; this is the first line.

    Raises ``ValueError`` naming the offending KEY only, never a value or a vault
    name, because the message is returned to the client and logged.
    """
    if not isinstance(value, dict):
        raise ValueError("agent.strands_env must be an object")
    out: dict[str, str | None] = {}
    for key, ref in value.items():
        if not isinstance(key, str) or key not in STRANDS_ENV_NAMES:
            raise ValueError("agent.strands_env names a variable the Strands harness does not read")
        if ref is None:
            out[key] = None
            continue
        if not isinstance(ref, str) or not ref.startswith(SECRET_URI_PREFIX):
            raise ValueError(
                f"agent.strands_env entry {key!r} must be a 'secret://<vault name>' reference"
            )
        name = ref[len(SECRET_URI_PREFIX) :]
        if not name or name != name.strip():
            raise ValueError(f"agent.strands_env entry {key!r} names no usable vault entry")
        out[key] = ref
    return out


def merge_strands_env_patch(stored: object, change: dict[str, str | None]) -> dict[str, str]:
    """``agent.strands_env`` as *stored*, with a validated *change* applied.

    A key the change maps to ``None`` is removed, any other key it names is set,
    and every key it does not name is kept as stored. A stored value that is not
    a mapping reads as empty.
    """
    merged = {k: v for k, v in stored.items()} if isinstance(stored, dict) else {}
    for key, ref in change.items():
        if ref is None:
            merged.pop(key, None)
        else:
            merged[key] = ref
    return merged


#: The only names ``agent.child_env_defaults`` accepts. An allowlist rather than a
#: denylist: each name is a measured kiro-cli tuning knob (its tokio and rayon
#: pools), and any other variable would reach every process the agent runs --
#: tools, MCP servers, the operator's own programs -- where no denylist could
#: enumerate every loader, proxy, certificate or interpreter variable that
#: changes what that code does.
#: ``agent.strands_provider``'s values: the ``provider/`` prefixes the pinned Strands
#: CLI's ``--model`` accepts. The one list both the setting and the harness read.
STRANDS_PROVIDER_CHOICES: tuple[str, ...] = (
    "bedrock",
    "bedrock-mantle",
    "anthropic",
    "openai",
    "google",
    "ollama",
    "litellm",
)


CHILD_ENV_DEFAULT_NAMES = frozenset({"TOKIO_WORKER_THREADS", "RAYON_NUM_THREADS"})

#: Inclusive bounds of a thread-pool size. Zero is refused because the two
#: runtimes disagree on it (rayon reads it as "use the default", tokio refuses
#: it); leaving the key out already gets the default.
CHILD_ENV_DEFAULT_MIN = 1
CHILD_ENV_DEFAULT_MAX = 1024


def coerce_child_env_defaults(raw: object) -> dict[str, str]:
    """Normalize ``agent.child_env_defaults`` to an allowlisted thread-pool map.

    Unlike :func:`coerce_deepseek_env`, an invalid entry here is DROPPED with a
    warning rather than refused at spawn: these are optional tuning defaults
    (thread-pool caps), so a typo must cost that one default, never a session.
    A name must be one of :data:`CHILD_ENV_DEFAULT_NAMES`; a value must be a
    string of ASCII digits whose integer is in
    [:data:`CHILD_ENV_DEFAULT_MIN`, :data:`CHILD_ENV_DEFAULT_MAX`], stored in
    canonical ``str(int)`` form (``"04"`` becomes ``"4"``). Nothing is stripped.
    The warning names the key only, never the value.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("agent.child_env_defaults is not a mapping; ignoring it")
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key not in CHILD_ENV_DEFAULT_NAMES:
            logger.warning(
                "agent.child_env_defaults: ignoring %r, only %s are accepted",
                key if isinstance(key, str) else type(key).__name__,
                ", ".join(sorted(CHILD_ENV_DEFAULT_NAMES)),
            )
            continue
        # isascii()+isdigit() rejects signs, spaces, underscores and non-ASCII
        # digits that int() would otherwise accept; the length cap keeps int()
        # off pathological inputs.
        count = (
            int(value)
            if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 8
            else 0
        )
        if not CHILD_ENV_DEFAULT_MIN <= count <= CHILD_ENV_DEFAULT_MAX:
            logger.warning(
                "agent.child_env_defaults: ignoring %s, the value must be a decimal "
                "integer string from %d to %d",
                key,
                CHILD_ENV_DEFAULT_MIN,
                CHILD_ENV_DEFAULT_MAX,
            )
            continue
        out[key] = str(count)
    return out


#: The grammar of an ``agent.env`` name: a POSIX shell identifier. Matched with
#: ``fullmatch`` so a trailing newline is not accepted.
AGENT_ENV_NAME_GRAMMAR = _re.compile("[A-Za-z_][A-Za-z0-9_]*")

#: The ``agent.env`` refusal rule. A refused name is one Kiro Crew, the sandbox or
#: the platform sets on the child, or one that makes the dynamic loader, a shell, a
#: language runtime or a tool the agent runs load or run code as a side effect of
#: running some other program: a preload, a start-up file, a hook, an option
#: string, a helper command, or a config file that names a program. A config field
#: is reachable by an agent that can write ``config.json``, so such a name would
#: attach code to every later command, including commands a person approved, and
#: the variables reach the sandbox launcher as well as the agent. Choosing which
#: installed toolchain runs (``JAVA_HOME``, ``CC``) is configuration and is
#: accepted. The class is open-ended: this denylist narrows what a config write
#: can attach and cannot close it, so who may write ``config.json`` stays the
#: boundary. It is refused in three layers: exact names
#: (``AGENT_ENV_DENIED_NAMES``), namespaces (``AGENT_ENV_DENIED_PREFIXES``) and
#: name shapes (``AGENT_ENV_DENIED_SUFFIXES``). A name added later to a known
#: family is then refused without an edit here. Every name compares upper-cased
#: (Windows treats environment names case-insensitively, so ``path`` is ``PATH``
#: there).
AGENT_ENV_DENIED_NAMES = frozenset(
    {
        # Process identity and paths Kiro Crew or the sandbox sets or remaps.
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "TMPDIR",
        "TMP",
        "TEMP",
        "USERPROFILE",
        "SYSTEMROOT",
        "COMSPEC",
        "XDG_RUNTIME_DIR",
        "KRB5CCNAME",
        # Code that runs at process or shell start.
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_REPL_EXTERNAL_MODULE",
        "PERL5OPT",
        "PERL5LIB",
        "PERLLIB",
        "RUBYOPT",
        "RUBYLIB",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
        "CLASSPATH",
        "BUNDLE_GEMFILE",
        "PHPRC",
        "PHP_INI_SCAN_DIR",
        "R_PROFILE",
        "R_PROFILE_USER",
        "R_ENVIRON",
        "R_ENVIRON_USER",
        "R_LIBS_USER",
        "R_LIBS_SITE",
        "DOTNET_STARTUP_HOOKS",
        "DOTNET_ADDITIONAL_DEPS",
        "VIMINIT",
        "GVIMINIT",
        "EXINIT",
        # Shell options, prompts and word splitting: ``SHELLOPTS=xtrace`` with
        # ``PS4='$(cmd)'`` runs ``cmd`` in every bash, ``ZDOTDIR`` picks the
        # start-up files of every zsh, and ``IFS`` re-splits every command line.
        "SHELLOPTS",
        "PS0",
        "PS1",
        "PS2",
        "PS3",
        "PS4",
        "ZDOTDIR",
        "IFS",
        "FCEDIT",
        # A program a tool runs from its environment, or a file it reads one from
        # (the ``*COMMAND``/``*EDITOR``/``*PAGER``/... shapes are suffix rules).
        "VISUAL",
        "CVS_SERVER",
        "SVN_SSH",
        "RSYNC_CONNECT_PROG",
        "HGMERGE",
        "WGETRC",
        "AWS_CONFIG_FILE",
        "KUBECONFIG",
        "DOCKER_CONFIG",
        "CARGO_HOME",
        "GOENV",
        "NPM_CONFIG_USERCONFIG",
        "NPM_CONFIG_GLOBALCONFIG",
        "NPM_CONFIG_GIT",
        "NPM_CONFIG_ONLOAD_SCRIPT",
        # Read by CPython's macOS framework launcher before the interpreter starts.
        "__PYVENV_LAUNCHER__",
        # Read by the C library, or by the launcher tools that run BEFORE the
        # sandbox confines the child (the cgroup scope talks to the user bus).
        "GCONV_PATH",
        "GLIBC_TUNABLES",
        "LOCPATH",
        "GETCONF_DIR",
        "GSS_MECH_CONFIG",
        "DBUS_SESSION_BUS_ADDRESS",
        "DBUS_SYSTEM_BUS_ADDRESS",
    }
)

#: ``agent.env`` name prefixes refused outright (upper-cased compare): Kiro Crew's
#: and kiro-cli's own namespaces, the DeepSeek Harness's and the opencode
#: harness's, the dynamic loaders', systemd's, Kerberos's, and the namespaces of
#: the interpreters and tools that read code, commands or config locations from
#: their environment. ``GIT_`` covers ``GIT_SSH_COMMAND``, which the sandbox
#: launcher sets itself, ``GIT_CONFIG_*``, which injects any git config key
#: (``core.fsmonitor``, ``core.hooksPath``), and ``GIT_EXEC_PATH``,
#: ``GIT_EXTERNAL_DIFF``, ``GIT_ASKPASS``, ``GIT_PAGER`` and every later addition.
#: ``OPENCODE_`` is where Kiro Crew hands the opencode harness its own config
#: (``OPENCODE_CONFIG_CONTENT``), and ``OPENCODE_CONFIG``/``OPENCODE_CONFIG_DIR``
#: point that harness at a config file whose MCP commands it runs at start-up.
AGENT_ENV_DENIED_PREFIXES = (
    "KIROCREW_",
    "KIRO_",
    "DSH_",
    "OPENCODE_",
    "LD_",
    "DYLD_",
    "PYTHON",
    "SYSTEMD_",
    "GIT_",
    "SSH_",
    "BASH",
    "PERL",
    "RUBY",
    "GEM_",
    "LUA_",
    "LESS",
    "OPENSSL_",
    "XDG_",
    "COR_",
    "CORECLR_",
    "KRB5",
)

#: ``agent.env`` name endings refused outright (upper-cased compare). A search
#: path a loader, shell or runtime loads code from (``*PATH``, ``*LIB``,
#: ``*LIBS``) or a module it loads (``*MODULE``, ``*MODULES``); an option or flag
#: string a runtime, JVM, compiler or build tool parses on every run, which can
#: load an agent or plugin (``*OPT``, ``*OPTS``, ``*OPTIONS``, ``*FLAGS``,
#: ``*_PRELOAD``); a helper program a tool runs from its environment
#: (``*COMMAND``, ``*SHELL``, ``*EXECUTABLE``, ``*EDITOR``, ``*PAGER``,
#: ``*ASKPASS``, ``*BROWSER``, ``*_RSH``, ``*WRAPPER``, ``*RUNNER``); and the
#: location of a config file or directory a tool reads its settings from, which
#: can name a program for it to run -- an MCP server, a credential helper, an
#: alias, a plugin (``*CONFIG``, ``*CONFIG_DIR``, ``*CONFIG_FILE``,
#: ``*CONFIG_HOME``: ``OPENCODE_CONFIG_DIR``, ``GH_CONFIG_DIR``,
#: ``TF_CLI_CONFIG_FILE``).
AGENT_ENV_DENIED_SUFFIXES = (
    "PATH",
    "LIB",
    "LIBS",
    "MODULE",
    "MODULES",
    "OPT",
    "OPTS",
    "OPTIONS",
    "FLAGS",
    "_PRELOAD",
    "COMMAND",
    "SHELL",
    "EXECUTABLE",
    "EDITOR",
    "PAGER",
    "ASKPASS",
    "BROWSER",
    "_RSH",
    "WRAPPER",
    "RUNNER",
    "CONFIG",
    "CONFIG_DIR",
    "CONFIG_FILE",
    "CONFIG_HOME",
)

#: Names inside a refused family that are data, never code: git's prompt switch
#: and its CA bundle path, which corporate proxy setups need. Exempt from the three
#: lists above only; the home-override and credential checks still apply.
AGENT_ENV_DATA_ONLY_NAMES = frozenset({"GIT_TERMINAL_PROMPT", "GIT_SSL_CAINFO"})

#: Names in this class look like credentials. ``agent.env`` values are stored in
#: plaintext in ``config.json`` and reach every process the agent runs, so a
#: credential belongs in the vault (``secret://`` on the MCP server that needs it).
#: Matched on ``_``-separated name SEGMENTS, not anywhere in the name: a segment
#: that is, or ends in, one of the words (``API_KEY``, ``GITHUB_TOKEN``,
#: ``DB_PASSWORD``, ``APIKEY``, ``PRIVATEKEY``), optionally plural or numbered.
#: A substring match would refuse plain data whose name merely contains a word:
#: ``GOPRIVATE`` (the private-module list the registry case needs),
#: ``TOKENIZERS_PARALLELISM``, ``KEYCLOAK_URL``.
AGENT_ENV_CREDENTIAL_CLASS = _re.compile(
    r"(?:^|_)[A-Z0-9]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)S?[0-9]*(?:_|\Z)",
    _re.IGNORECASE,
)


def _agent_env_home_override_names() -> frozenset[str]:
    """Upper-cased ``$HOME``-override variables the harness auth table declares.

    The credential read gate anchors each harness's sign-in directory from these
    variables in the gateway's own environment, so a value set only for the agent
    child would move that directory out from under the fence. Derived from the
    table, so a harness added there is refused here too.
    """
    from kiro_crew.agent_sdk.host_auth import home_override_env_vars
    from kiro_crew.agent_sdk.native_allow_stores import override_env_vars

    # Also each variable that moves a harness's own permission files, fenced the same way.
    names = (*home_override_env_vars(), *override_env_vars())
    return frozenset(name.upper() for name in names)


def _agent_env_denied(upper: str) -> bool:
    """Whether the upper-cased *upper* falls in the refused class (see the lists)."""
    if upper in AGENT_ENV_DATA_ONLY_NAMES:
        return False
    return (
        upper in AGENT_ENV_DENIED_NAMES
        or upper.startswith(AGENT_ENV_DENIED_PREFIXES)
        or upper.endswith(AGENT_ENV_DENIED_SUFFIXES)
    )


def agent_env_refusal(name: str) -> str | None:
    """Why ``agent.env`` refuses *name*, or ``None`` when it is accepted.

    The reason is a short phrase for a log line; it never carries the value.
    """
    if not AGENT_ENV_NAME_GRAMMAR.fullmatch(name):
        return "it is not an environment-variable name ([A-Za-z_][A-Za-z0-9_]*)"
    upper = name.upper()
    if _agent_env_denied(upper):
        return (
            "Kiro Crew or the sandbox owns that name, or it can make a shell, the "
            "loader, a runtime or a tool run code the command did not name"
        )
    if upper in _agent_env_home_override_names():
        return (
            "it relocates a harness's sign-in directory, which the credential "
            "read gate resolves from the gateway's own environment"
        )
    if AGENT_ENV_CREDENTIAL_CLASS.search(name):
        return (
            "the name looks like a credential; agent.env is stored in plaintext, "
            "so keep credentials in Settings > Secrets"
        )
    return None


def _agent_env_entry_refusal(key: object, value: object) -> str | None:
    """Why ``agent.env`` drops the entry *key* -> *value*, or ``None`` when it is kept.

    The one per-entry rule both :func:`coerce_agent_env` (at load) and
    :func:`agent_env_write_refusals` (at save) apply, so what a save turns away and
    what a load drops cannot disagree. The reason never carries the value.
    """
    if not isinstance(key, str):
        return f"a {type(key).__name__} key is not an environment-variable name"
    reason = agent_env_refusal(key)
    if reason is not None:
        return reason
    if not isinstance(value, str) or "\x00" in value:
        return "the value must be a string with no NUL"
    try:
        # The spawn encodes environment values exactly this way; a value it
        # cannot encode (a lone surrogate on POSIX) would fail every agent launch.
        _os.fsencode(value)
    except UnicodeEncodeError:
        return "the value cannot be encoded for a process environment"
    return None


def agent_env_scrub_removes(name: str) -> bool:
    """Whether the agent environment scrub strips *name* from an agent child.

    Upper-cased compare, so a lower-case spelling of a scrubbed name is caught on
    every platform. The list lives in the sandbox module, so it is imported here
    on first use rather than by every config load.
    """
    from kiro_crew.sandbox import agent_env_scrub_prefixes

    upper = name.upper()
    return any(upper.startswith(prefix) for prefix in agent_env_scrub_prefixes())


def coerce_agent_env(raw: object) -> dict[str, str]:
    """Normalize ``agent.env`` to the name -> value map the agent child receives.

    An invalid entry is DROPPED with a warning naming the key, never the value,
    following :func:`coerce_child_env_defaults`: a typo or a refused name costs
    that one variable, never a session. Refused (:func:`_agent_env_entry_refusal`):
    a name outside the POSIX identifier grammar, a name :func:`agent_env_refusal`
    reserves, a non-string value, a value containing a NUL (no process environment
    can hold one), and a value the spawn cannot encode. Nothing is stripped. The
    agent environment scrub runs after this map is applied, so a name it removes
    is also dropped -- at spawn, by ``acp.agent_env``, so that a config load never
    imports the sandbox module. A config write refuses all of these up front
    (:func:`agent_env_write_refusals`); this drop is for a hand edit.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("agent.env is not a mapping; ignoring it")
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        reason = _agent_env_entry_refusal(key, value)
        if reason is None and isinstance(key, str) and isinstance(value, str):
            out[key] = value
        else:
            logger.warning("agent.env: ignoring %r, %s", key, reason)
    return out


def agent_env_write_refusals(raw: object) -> tuple[str, ...]:
    """Each ``agent.env`` entry in *raw* that a spawn would never apply, with why.

    For the config publish floor (``config.loader._refuse_unpublishable``), which
    every config writer reaches -- ``kirocrew config set`` in all its forms and
    every dashboard write -- so a refused name is turned away with its reason when
    it is saved instead of being stored, listed, and dropped at load with only a
    gateway-log line. The same per-entry rule as :func:`coerce_agent_env`, plus the
    agent environment scrub check ``acp.agent_env`` makes at spawn. Each phrase
    names the key, never the value. ``()`` for an absent mapping; a non-mapping is
    one refusal.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        return (f"agent.env must be an object of NAME: value strings, not a {type(raw).__name__}",)
    refusals: list[str] = []
    for key, value in raw.items():
        reason = _agent_env_entry_refusal(key, value)
        if reason is None and isinstance(key, str) and agent_env_scrub_removes(key):
            reason = "the agent environment scrub removes that name"
        if reason is not None:
            refusals.append(f"{key!r}: {reason}")
    return tuple(refusals)
