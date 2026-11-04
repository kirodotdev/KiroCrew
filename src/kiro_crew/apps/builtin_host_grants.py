"""Host grants a SHIPPED built-in app declares for itself, read through one seam.

Some built-in apps need something from the host that no App Kit manifest field can
ask for: a spawn-time resource profile, an environment variable ``minimal_env()``
would strip, a strict-internal route the agent's MCP tools reach, a config.json
section the unrecognized-key warning must not scold. Spelled as core literals, each
would name the app, and the app could not leave the repository without a core edit.

So the core names no app. Each built-in package listed in ``BUILTIN_NAMES`` MAY ship
a ``host_grants`` submodule holding one ``HOST_GRANTS`` mapping of plain data; this
module reads every such mapping once and answers the core's questions from them. With
no built-in shipping one, every answer is empty and the core behaves as if no app
asked for anything.

Trust. Only packages in ``BUILTIN_NAMES`` are read, so a grant is shipped package code
reviewed in this repository, never a manifest field an installed or registry app could
claim. A row may grant only its OWN app: ``app`` must equal the package's installed
name, and every path it contributes must sit under that app's own
``/api/apps/<app>/`` or ``/apps/<app>/`` namespace, so one built-in cannot widen
another's surface or a core route. A forwarded environment variable must be a
``KIROCREW_`` name, so the seam cannot hand an app backend an arbitrary
host credential. A malformed row raises at load rather than being skipped: a shipped
grant that silently stopped applying would fail toward a weaker or broken app with no
signal.

``host_grants`` is a data module by contract. The loader imports the app's package to
reach it, so a built-in that ships one keeps its package ``__init__`` free of heavy
imports: the readers of this seam run in the MCP servers and the config loader as
well as in the gateway.
"""

from __future__ import annotations

import functools
import importlib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

_GRANTS_SUBMODULE = "host_grants"
_ENV_NAMESPACE = "KIROCREW_"


@dataclass(frozen=True)
class HostGrants:
    """What one built-in app asks of the host. Every field defaults to "nothing"."""

    #: The app's installed name (``app.json`` ``name``), e.g. the hyphenated form of
    #: its package name.
    app: str
    #: Spawn this app's backend with ``RLIMIT_PROFILE_BUILD`` instead of
    #: ``RLIMIT_PROFILE_TOOL``: it is the ANCESTOR of real build workloads (vite,
    #: pip) that a 1024-descriptor hard cap starves.
    build_rlimit: bool = False
    #: Hand this app's backend the port the gateway ACTUALLY bound
    #: (``KIROCREW_BOUND_PORT``). The value does not exist before the bind, so the
    #: boot wave holds this app's spawn back until ``start_deferred_app_backends``.
    bound_port: bool = False
    #: Operator-set variables forwarded to EVERY app backend's environment, which
    #: ``minimal_env()`` would otherwise strip. Exact names.
    backend_env_names: tuple[str, ...] = ()
    #: As ``backend_env_names``, matched by prefix.
    backend_env_prefixes: tuple[str, ...] = ()
    #: Top-level ``config.json`` sections the app reads straight from the file. The
    #: unrecognized-top-level-key warning skips them while they hold a JSON object.
    config_top_keys: tuple[str, ...] = ()
    #: EXACT paths admitted to the strict-internal tier (loopback plus
    #: ``X-Internal-Secret``, no cookie fall-through). Never a prefix: the tier
    #: matches ``path == p or path.startswith(p + "/")``.
    strict_internal_paths: tuple[str, ...] = ()
    #: EXACT paths that mint or return a local credential, refused to every relayed
    #: caller by ``kiro_relay.limits``.
    relay_credential_paths: tuple[str, ...] = ()


_TUPLE_FIELDS = frozenset(
    f.name for f in fields(HostGrants) if f.name not in {"app", "build_rlimit", "bound_port"}
)
_BOOL_FIELDS = frozenset({"build_rlimit", "bound_port"})


def _installed_name(package: str) -> str:
    return package.replace("_", "-")


def _owns_path(app: str, path: str) -> bool:
    return path.startswith((f"/api/apps/{app}/", f"/apps/{app}/"))


def _parse(package: str, raw: object) -> HostGrants:
    """One ``HOST_GRANTS`` mapping as a :class:`HostGrants`, or ``ValueError``."""
    where = f"kiro_crew.apps.builtins.{package}.{_GRANTS_SUBMODULE}.HOST_GRANTS"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be a mapping, not {type(raw).__name__}")
    unknown = set(raw) - {f.name for f in fields(HostGrants)}
    if unknown:
        raise ValueError(f"{where} has unknown keys: {sorted(unknown)}")
    app = raw.get("app")
    if app != _installed_name(package):
        raise ValueError(f"{where} grants {app!r}; a built-in may grant only itself")
    values: dict[str, object] = {"app": app}
    for name in _BOOL_FIELDS & set(raw):
        if not isinstance(raw[name], bool):
            raise ValueError(f"{where}[{name!r}] must be a bool")
        values[name] = raw[name]
    for name in _TUPLE_FIELDS & set(raw):
        items = raw[name]
        if not isinstance(items, (tuple, list)) or not all(isinstance(i, str) and i for i in items):
            raise ValueError(f"{where}[{name!r}] must be a sequence of non-empty strings")
        values[name] = tuple(items)
    grants = HostGrants(**values)  # type: ignore[arg-type]
    for env in grants.backend_env_names + grants.backend_env_prefixes:
        if not env.startswith(_ENV_NAMESPACE) or env == _ENV_NAMESPACE:
            raise ValueError(f"{where} forwards {env!r}; only {_ENV_NAMESPACE}* names may")
    for path in grants.strict_internal_paths + grants.relay_credential_paths:
        if not _owns_path(grants.app, path) or path.endswith("/"):
            raise ValueError(f"{where} claims {path!r}, outside the app's own namespace")
    return grants


@functools.lru_cache(maxsize=1)
def builtin_host_grants() -> tuple[HostGrants, ...]:
    """Every shipped built-in's grants, in ``BUILTIN_NAMES`` order. Read once."""
    from kiro_crew.apps import builtins

    root = Path(builtins.__file__).parent
    out: list[HostGrants] = []
    for package in builtins.BUILTIN_NAMES:
        # Looked for on disk before importing: an import would run EVERY built-in's
        # package ``__init__`` (some register heavy route modules there) on the MCP
        # server and config-loader paths, for packages that grant nothing.
        if not (root / package / f"{_GRANTS_SUBMODULE}.py").is_file():
            continue
        module = importlib.import_module(f"{builtins.__name__}.{package}.{_GRANTS_SUBMODULE}")
        out.append(_parse(package, getattr(module, "HOST_GRANTS", None)))
    return tuple(out)


def build_rlimit_apps() -> frozenset[str]:
    """Apps whose backend spawns with the BUILD rlimit profile."""
    return frozenset(g.app for g in builtin_host_grants() if g.build_rlimit)


def bound_port_apps() -> frozenset[str]:
    """Apps whose backend receives ``KIROCREW_BOUND_PORT`` and spawns after the bind."""
    return frozenset(g.app for g in builtin_host_grants() if g.bound_port)


def backend_env_names() -> tuple[str, ...]:
    """Exact variable names forwarded to every app backend."""
    return tuple(dict.fromkeys(n for g in builtin_host_grants() for n in g.backend_env_names))


def backend_env_prefixes() -> tuple[str, ...]:
    """Variable-name prefixes forwarded to every app backend."""
    return tuple(dict.fromkeys(p for g in builtin_host_grants() for p in g.backend_env_prefixes))


def config_top_keys() -> frozenset[str]:
    """Top-level ``config.json`` sections a built-in app reads from the file."""
    return frozenset(k for g in builtin_host_grants() for k in g.config_top_keys)


def strict_internal_paths() -> frozenset[str]:
    """Exact strict-internal paths the built-ins contribute."""
    return frozenset(p for g in builtin_host_grants() for p in g.strict_internal_paths)


def relay_credential_paths() -> frozenset[str]:
    """Exact credential-minting paths the built-ins contribute to the relay refusal."""
    return frozenset(p for g in builtin_host_grants() for p in g.relay_credential_paths)
