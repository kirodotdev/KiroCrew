"""``SshTunnelManager`` keeps every member it had, and each is bound from the module that owns it.

The manager's behaviour lives in the ``kiro_crew.instances.ssh_tunnel_manager_*`` owner modules:
each member's body is a module-level function there whose first parameter is ``self``, and the
class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The instance state
stays on the instance, so tests, ``SshTunnelManager.__new__`` builds and every patch of a method
see the same object they always did. The module-level helpers, constants and classes live in
the owners too, and ``kiro_crew.instances.ssh_tunnel_manager`` serves each of them. This file
pins that contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled attribute,
  which only work inside a class body;
* no owner imports the facade at run time, and each imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* the owners log under the facade's logger, and a moved class keeps the facade as its
  ``__module__`` (with its annotations still resolving there);
* the facade serves exactly the definitions that moved, a star import binds the names it bound
  before the split, and the facade's own code reads only names it binds;
* a write through the facade reaches every owner that reads the name, for every name the test
  trees patch there (the facade's mirror, ``kiro_crew._facade_mirror``).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import re
import sys
import typing
from pathlib import Path
from types import ModuleType
from typing import Any

import facade_mirror_scan
import pytest

from kiro_crew.instances import ssh_tunnel_manager as stm
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

pytestmark = pytest.mark.xdist_group(name="tree_scan_ssh_tunnel_manager_bindings")

_PACKAGE = "kiro_crew.instances"

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, object, str, str]] = {
    "__init__": ("function", False, "ab30f0d05883", "ssh_tunnel_manager"),
    "_answer_embed_mint": ("function", True, "b5c8b10f6ade", "ssh_tunnel_manager_tokens"),
    "_apply_hop_holds": ("function", False, "fd498410fb8e", "ssh_tunnel_manager_ports"),
    "_await_peer_version_change": (
        "function",
        True,
        "db9ae165516e",
        "ssh_tunnel_manager_remote_ops",
    ),
    "_cancel_recovery": ("function", True, "eebcbb982c22", "ssh_tunnel_manager_self_heal"),
    "_cancel_token_refresh": ("function", False, "eebcbb982c22", "ssh_tunnel_manager_tokens"),
    "_cancel_token_refresh_and_wait": (
        "function",
        True,
        "eebcbb982c22",
        "ssh_tunnel_manager_tokens",
    ),
    "_chain_cycle_reason": ("function", True, "c862846c341a", "ssh_tunnel_manager_transport"),
    "_chained_below": ("function", True, "46f5484c99bb", "ssh_tunnel_manager_lifecycle"),
    "_chained_far_end_holds": ("function", True, "b58644c8fc47", "ssh_tunnel_manager_peer"),
    "_connect_timeout_for": ("function", False, "4c89c0af9684", "ssh_tunnel_manager_transport"),
    "_credential_forward_moved": ("function", False, "aea90e3b1106", "ssh_tunnel_manager_tokens"),
    "_error_status": ("function", False, "6fd89cf378b6", "ssh_tunnel_manager_lifecycle"),
    "_exchange_link": ("function", True, "3e84e3e65aa9", "ssh_tunnel_manager_peer"),
    "_forwarder_identity_hints": ("function", True, "14d8a42cbf05", "ssh_tunnel_manager_ports"),
    "_hold_lent_port_by_number": ("function", False, "b11b34d0417d", "ssh_tunnel_manager_ports"),
    "_hold_lent_port_now": ("function", False, "eebcbb982c22", "ssh_tunnel_manager_ports"),
    "_instance_on_port": ("function", False, "5f271c99d42f", "ssh_tunnel_manager_peer"),
    "_lent_hop_ttl": ("staticmethod", False, "8c9e104443bb", "ssh_tunnel_manager_tokens"),
    "_mark_recovered": ("function", True, "c25e8ef8253f", "ssh_tunnel_manager_self_heal"),
    "_mint_for": ("function", True, "a4a40773e7f0", "ssh_tunnel_manager_transport"),
    "_mint_superseded": ("function", False, "2885ca508f03", "ssh_tunnel_manager_tokens"),
    "_mint_through_parent": ("function", True, "a4a40773e7f0", "ssh_tunnel_manager_transport"),
    "_mint_timeout_for": ("function", False, "4c89c0af9684", "ssh_tunnel_manager_transport"),
    "_mint_token_with_parent_port": ("function", True, "94e525cb401a", "ssh_tunnel_manager_tokens"),
    "_minted_ttl": ("function", False, "438b16f4eb67", "ssh_tunnel_manager_tokens"),
    "_on_tunnel_exit": ("function", False, "eebcbb982c22", "ssh_tunnel_manager_self_heal"),
    "_on_tunnel_healthy": ("function", False, "eebcbb982c22", "ssh_tunnel_manager_self_heal"),
    "_peer_cookie_header": ("function", True, "e1ee83accc2a", "ssh_tunnel_manager_peer"),
    "_peer_forward_stamp": ("function", False, "8fe124bd6762", "ssh_tunnel_manager_peer"),
    "_peer_gateway_id": ("function", True, "5f271c99d42f", "ssh_tunnel_manager_transport"),
    "_peer_headers_for": ("function", True, "b19b8cdc18c4", "ssh_tunnel_manager_peer"),
    "_peer_identity": ("function", True, "d9f2947263b0", "ssh_tunnel_manager_transport"),
    "_peer_target": ("function", False, "eca30c5ba36f", "ssh_tunnel_manager_peer"),
    "_peer_version": ("function", True, "4304a34e8586", "ssh_tunnel_manager_remote_ops"),
    "_persist_hint": ("function", True, "260498a9e1e7", "ssh_tunnel_manager_lifecycle"),
    "_port_recorded_by_another_row": (
        "function",
        False,
        "9c28f4923f64",
        "ssh_tunnel_manager_ports",
    ),
    "_prime_peer_session": ("function", True, "eebcbb982c22", "ssh_tunnel_manager_peer"),
    "_rebuild": ("function", True, "91a4727bfcad", "ssh_tunnel_manager_self_heal"),
    "_reclaim_orphan_forwarder": ("function", True, "f3c3c6ac9e34", "ssh_tunnel_manager_ports"),
    "_reconfigure_locked": ("function", True, "c50b534715b6", "ssh_tunnel_manager_lifecycle"),
    "_recover": ("function", True, "eebcbb982c22", "ssh_tunnel_manager_self_heal"),
    "_recover_after": ("function", True, "76aac80c9a77", "ssh_tunnel_manager_self_heal"),
    "_refresh_delay": ("function", False, "7d420fa6f918", "ssh_tunnel_manager_tokens"),
    "_refresh_token_once": ("function", True, "b58644c8fc47", "ssh_tunnel_manager_tokens"),
    "_remint_parent_under_lock": ("function", True, "7ebc4a186af3", "ssh_tunnel_manager_transport"),
    "_remote_command_target": ("function", True, "5583e028c227", "ssh_tunnel_manager_remote_ops"),
    "_require_peer_forward": ("function", False, "92c38c285cfc", "ssh_tunnel_manager_peer"),
    "_reserved_ports": ("function", False, "6e67e49bb5fd", "ssh_tunnel_manager_ports"),
    "_resolve_chained_transport": (
        "function",
        False,
        "572b657e3683",
        "ssh_tunnel_manager_transport",
    ),
    "_resolve_transport": ("function", False, "e04704ab9320", "ssh_tunnel_manager_transport"),
    "_retire_chained": ("function", True, "c4e68ce7224d", "ssh_tunnel_manager_tokens"),
    "_run_remote_subcommand": ("function", True, "c89d1a190190", "ssh_tunnel_manager_remote_ops"),
    "_schedule_chained_retirement": (
        "function",
        False,
        "c4e68ce7224d",
        "ssh_tunnel_manager_tokens",
    ),
    "_schedule_diagnosis": ("function", False, "eebcbb982c22", "ssh_tunnel_manager_remote_ops"),
    "_schedule_token_refresh": ("function", False, "eebcbb982c22", "ssh_tunnel_manager_tokens"),
    "_store_token": ("function", False, "b559a6fe5963", "ssh_tunnel_manager_tokens"),
    "_teardown_locked": ("function", True, "ce7e791cf0ee", "ssh_tunnel_manager_lifecycle"),
    "_token_refresh_loop": ("function", True, "eebcbb982c22", "ssh_tunnel_manager_tokens"),
    "_track_recovery": ("function", False, "9feb5e629104", "ssh_tunnel_manager_self_heal"),
    "_update_then_restart": ("function", True, "9243d16c3e1e", "ssh_tunnel_manager_remote_ops"),
    "_with_parent": ("function", True, "1d092f289575", "ssh_tunnel_manager_transport"),
    "apply_config": ("function", False, "d2f0f43ef2a5", "ssh_tunnel_manager_lifecycle"),
    "connect": ("function", True, "c390b489e467", "ssh_tunnel_manager_lifecycle"),
    "diagnose": ("function", True, "0f4ffeaa2f07", "ssh_tunnel_manager_remote_ops"),
    "disconnect": ("function", True, "77b2c15f326e", "ssh_tunnel_manager_lifecycle"),
    "get_token": ("function", False, "4304a34e8586", "ssh_tunnel_manager_tokens"),
    "hop_ownership_violations": ("function", False, "4dd1e5721cbf", "ssh_tunnel_manager_ports"),
    "last_error": ("function", False, "427800bb96d3", "ssh_tunnel_manager_lifecycle"),
    "mint_embed_token": ("function", True, "582ddabcd0b7", "ssh_tunnel_manager_tokens"),
    "peer_capability": ("function", True, "4fd9c60dcb9f", "ssh_tunnel_manager_peer"),
    "peer_version": ("function", True, "ca2073978776", "ssh_tunnel_manager_peer"),
    "proxy_request": ("function", False, "185d7a847cc1", "ssh_tunnel_manager_peer"),
    "reconfigure": ("function", True, "c50b534715b6", "ssh_tunnel_manager_lifecycle"),
    "refresh_token": ("function", True, "427800bb96d3", "ssh_tunnel_manager_tokens"),
    "remote_update_job": ("function", False, "0f4ffeaa2f07", "ssh_tunnel_manager_remote_ops"),
    "restart_remote": ("function", True, "5f5f7672543c", "ssh_tunnel_manager_remote_ops"),
    "search_sessions_remote": ("function", True, "5ab0af6948cd", "ssh_tunnel_manager_peer"),
    "send_session_bundle": ("function", True, "e549c097311f", "ssh_tunnel_manager_peer"),
    "shutdown": ("function", True, "b60c5fea256e", "ssh_tunnel_manager_lifecycle"),
    "start_remote_update": ("function", True, "5f5f7672543c", "ssh_tunnel_manager_remote_ops"),
    "status": ("function", False, "c4d8b0fe5305", "ssh_tunnel_manager_lifecycle"),
    "status_all": ("function", False, "954711b52218", "ssh_tunnel_manager_lifecycle"),
    "sync_hop_holds": ("function", False, "6e67e49bb5fd", "ssh_tunnel_manager_ports"),
    "token_ttl_remaining": ("function", False, "f74239bae374", "ssh_tunnel_manager_tokens"),
    "token_ttl_total": ("function", False, "f74239bae374", "ssh_tunnel_manager_tokens"),
    "token_validates": ("function", True, "75c047a9e0c5", "ssh_tunnel_manager_peer"),
}

#: Every definition that left the facade for an owner and is served from it (the two the
#: facade's own code reads, ``_SshTunnel`` and ``TunnelState``, stay bound there).
_MOVED_DEFINITIONS = frozenset("""
    ProxyRequestError TunnelStatus _ANSI_CSI_RE _BANNER_DETAIL_MAX_CHARS
    _BENIGN_SSH_STDERR_MARKERS _C0_CONTROL_RE _FIRST_CONNECT_DROP_HINT _HOP_RETIRED_CODES
    _LOOPBACK _MAX_STDERR_CHARS _MAX_STDOUT_CHARS _Mint _Moved _PEER_CAPABILITY_PATHS
    _PEER_SIZE_REFUSALS _PeerUnavailable _READY_POLL_INTERVAL_SECS _RECLAIM_KILL_GRACE_SECS
    _RECLAIM_POLL_INTERVAL_SECS _RECLAIM_SIG_DOMAIN _RECLAIM_TERM_GRACE_SECS
    _RECOVER_BACKOFF_BASE_SECS _RecoverySuperseded _SSH_AUTH_SIGNALS _SSH_BIND_SIGNALS
    _SSH_TRANSPORT_DROP_SIGNALS _SSM_BIND_SIGNALS _SSM_CLOSED_NO_REASON_ANCHOR
    _SSM_CLOSED_WITH_REASON_ANCHOR _SSM_CREDENTIAL_SIGNALS _SSM_DENIAL_SIGNALS
    _SSM_IDLE_CLOSE_SIGNALS _SSM_PLUGIN_SIGNALS _SSM_REASON_SEPARATOR
    _SSM_RESUME_TIMED_OUT_ANCHOR _SSM_START_FAILED_ANCHOR _SSM_TARGET_SIGNALS
    _STDERR_DRAIN_GRACE_SECS _STDOUT_READ_CHUNK _T _TREE_KILL_UNDELIVERED _TransportParams
    _UPLOAD_CHUNK_BYTES _WINDOWS_SSH_TUNNEL_UNSUPPORTED _build_ssh_tunnel_argv
    _build_ssm_tunnel_argv _first_hit _forwarder_identity_sig _forwarder_orphan_state
    _read_capability_body _read_transfer_reply _reclaim_identity_key _recover_backoff_secs
    _sanitize_banner _sanitize_ssm_stdout _ssm_close_reason _strip_benign_ssh_noise
    _transport_method _unsupported_method_message _upload_chunks _verify_and_reclaim_forwarder
    """.split())

#: The names ``from kiro_crew.instances.ssh_tunnel_manager import *`` bound before the split.
_STAR_NAMES = frozenset("""
    Any Awaitable CONNECTION_METHODS Callable DEFAULT_TUNNEL_BASE_PORT DiagnosisResult
    FARGATE_HEALTH_PATH FARGATE_TURN_PATH HopPortGuard HopRetiredError Instance
    InstanceDisabledError InstancesRegistry LENT_HOP_TTL_CAP MAX_VIA_HOPS NamedTuple
    PHASE_RESTARTING PHASE_VERIFYING PROXY_TOOL_MISSING_SIGNALS Path PortAllocator
    ProxyRequestError REMOTE_UPDATE_TIMEOUT_SECS REMOTE_UPDATE_VERIFY_INTERVAL_SECS
    REMOTE_UPDATE_VERIFY_SECS RemoteUpdateJob RemoteUpdateJobs SSM_TRANSPORT_METHODS
    STATE_SUCCEEDED STATE_UNCHANGED STATE_UNVERIFIED SshTunnelManager SshValidationError
    SsmValidationError TokenMintError TunnelState TunnelStatus TypeVar
    UnsupportedConnectionMethodError aiohttp ancestor_ids annotations asyncio clean_version
    cloud_ssm codecs contextlib dataclass dataclass_replace descendant_ids diagnose_instance
    diagnose_instance_fargate diagnose_instance_ssm enum gateway_id hashlib hmac
    is_headless_provisioner json live logger logging mint_remote_token mint_remote_token_ssm
    platform_compat proxy_tool_missing_message quote re redact run_remote_kirocrew
    run_remote_kirocrew_ssm sel sel_hmac_key_path split_ecs_target ssh_spawn_argv_env time
    tool_spawn_env ttl_to_seconds validate_aws_profile validate_aws_region validate_remote_bin
    validate_ssh_host validate_ssm_run_as validate_ssm_target validate_ttl
    """.split())

#: The classes that moved to an owner and keep the facade as their ``__module__``.
_MOVED_CLASSES = frozenset(
    {
        "ProxyRequestError",
        "TunnelState",
        "TunnelStatus",
        "_Mint",
        "_Moved",
        "_PeerUnavailable",
        "_RecoverySuperseded",
        "_SshTunnel",
        "_TransportParams",
    }
)


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in stm._OWNERS]


def _signature_digest(fn: Any) -> str:
    """The signature with ``self``'s annotation dropped and object addresses stripped (a
    default such as ``mint_token=mint_remote_token`` renders with one)."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    text = re.sub(r" at 0x[0-9a-fA-F]+", "", str(sig.replace(parameters=params)))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(SshTunnelManager)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(
    tree: ast.Module, owner: str = ""
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and f"{_PACKAGE}.{home}" == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(SshTunnelManager)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []
    assert SshTunnelManager.__module__ == stm.__name__


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    value = vars(SshTunnelManager)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    shape = inspect.iscoroutinefunction(fn) or (inspect.isasyncgenfunction(fn) and "agen")
    assert (shape or False) == coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == f"{_PACKAGE}.{owner}"
    if owner != "ssh_tunnel_manager":
        assert vars(sys.modules[f"{_PACKAGE}.{owner}"])[name] is fn


def test_every_owner_holding_members_is_listed_in_the_facade() -> None:
    homes = {owner for _k, _c, _d, owner in _MEMBERS.values() if owner != "ssh_tunnel_manager"}
    assert {f"{_PACKAGE}.{home}" for home in homes} <= set(stm._OWNERS)


@pytest.mark.parametrize("owner", stm._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What breaks when a method body leaves its class: ``super()`` with no arguments, the
    ``__class__`` cell, and a name-mangled ``__attr`` (``__x`` without a trailing ``__``)."""
    found: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "super" and not node.args:
                found.append("super()")
        if isinstance(node, ast.Name) and node.id == "__class__":
            found.append("__class__")
        spelled = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(spelled, str) and spelled.startswith("__") and not spelled.endswith("__"):
            found.append(spelled)
    return found


def test_the_class_only_scan_can_fail() -> None:
    tree = ast.parse("def f(self):\n    super().g()\n    return __class__, self.__hidden\n")
    assert sorted(_class_only_constructs(tree.body[0])) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("owner", stm._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree, owner)} == {
        fn.name: [] for fn in _self_functions(tree, owner)
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import the facade outside a ``TYPE_CHECKING`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == stm.__name__ or (
                node.module == _PACKAGE and any(a.name == "ssh_tunnel_manager" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == stm.__name__ for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.instances.ssh_tunnel_manager import X\n"
        "def f():\n    from kiro_crew.instances.ssh_tunnel_manager import x\n"
        "import kiro_crew.instances.ssh_tunnel_manager\n"
        "from kiro_crew.instances import ssh_tunnel_manager\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", stm._OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


def _owner_imports(tree: ast.Module) -> set[str]:
    """The owner modules *tree* imports, by dotted name."""
    prefix = f"{stm.__name__}_"
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(prefix):
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(
                    f"{_PACKAGE}.{a.name}"
                    for a in node.names
                    if f"{_PACKAGE}.{a.name}".startswith(prefix)
                )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(prefix))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.instances.ssh_tunnel_manager_child import X\n"
        "from kiro_crew.instances import ssh_tunnel_manager_peer as _p, registry\n"
        "import kiro_crew.instances.ssh_tunnel_manager_ports\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.instances.ssh_tunnel_manager_child",
        "kiro_crew.instances.ssh_tunnel_manager_peer",
        "kiro_crew.instances.ssh_tunnel_manager_ports",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_OWNERS`` lists the owners lowest layer first: an owner imports only owners
    earlier in the tuple, so the order is the import order."""
    owners = stm._OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset(
    {"add", "discard", "append", "pop", "update", "clear", "setdefault", "remove"}
)


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _fields(fns: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> tuple[set[str], set[str]]:
    """The ``self`` fields *fns* write and read. Writes: a store or delete of the
    attribute or of a subscript on it, a mutator call on it and ``setattr(self, "<f>",
    ...)``. Reads: every other load and ``getattr``/``hasattr(self, "<f>")``."""
    written: set[str] = set()
    read: set[str] = set()
    for fn in fns:
        for node in ast.walk(fn):
            name = _self_field(node)
            if name is not None:
                (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(name)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                target = _self_field(node.value)
                if target is not None:
                    written.add(target)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
                target = _self_field(func.value)
                if target is not None:
                    written.add(target)
            if (
                isinstance(func, ast.Name)
                and func.id in ("getattr", "hasattr", "setattr")
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "self"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                (written if func.id == "setattr" else read).add(node.args[1].value)
    return written - set(_MEMBERS), read - written - set(_MEMBERS)


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n    setattr(self, 'g', 1)\n"
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e\n"
    )
    assert _fields([tree.body[0]]) == ({"a", "b", "c", "d", "e", "f", "g"}, {"h", "i", "j"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", stm._OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", stm._OWNERS)
def test_owners_log_under_the_facade_logger(owner: str) -> None:
    module = sys.modules[owner]
    assert getattr(module, "logger", stm.logger) is stm.logger
    assert stm.logger.name == stm.__name__


def test_each_moved_class_keeps_the_facade_as_its_module() -> None:
    """Error chains, tracebacks and ``gateway.log`` render a class as
    ``module.qualname``; a class that moved to an owner keeps naming the path callers
    import it from."""
    defined = {
        node.name: owner
        for owner in _owners()
        for node in _tree(owner).body
        if isinstance(node, ast.ClassDef)
    }
    assert set(defined) == _MOVED_CLASSES
    assert {name: getattr(stm, name).__module__ for name in defined} == {
        name: stm.__name__ for name in defined
    }
    assert {name: vars(owner)[name] for name, owner in defined.items()} == {
        name: getattr(stm, name) for name in defined
    }


@pytest.mark.parametrize("name", sorted(_MOVED_CLASSES))
def test_a_moved_class_annotations_still_resolve(name: str) -> None:
    """``typing.get_type_hints`` resolves a class's annotations in its ``__module__``'s
    namespace, which is the facade's: every name they spell is bound there."""
    cls = getattr(stm, name)
    hints = typing.get_type_hints(cls)
    assert set(hints) == set(vars(cls).get("__annotations__", {}))


def test_the_status_annotations_name_the_state_class() -> None:
    assert typing.get_type_hints(stm.TunnelStatus)["state"] is stm.TunnelState


def test_the_facade_serves_exactly_the_definitions_that_moved() -> None:
    index = stm.__facade_mirror__
    assert set(index.exports) == _MOVED_DEFINITIONS == stm._SERVED
    assert set(stm._EXPORTS) == _MOVED_DEFINITIONS
    for name, owner in index.exports.items():
        assert getattr(stm, name) is vars(sys.modules[owner])[name]


def test_a_star_import_binds_what_it_bound_before() -> None:
    assert set(stm.__all__) == _STAR_NAMES
    assert [name for name in sorted(_STAR_NAMES) if not hasattr(stm, name)] == []


def _facade_code_reads() -> set[str]:
    """Every global name the facade's own code reads while it runs or is defined: the class
    body, ``__init__`` (defaults included) and the module level, annotations aside."""
    tree = _tree(stm)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            skip.update(id(inner) for inner in ast.walk(node))
        anns: list[ast.AST] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            anns += [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs if x.annotation]
            anns += [x.annotation for x in (a.vararg, a.kwarg) if x is not None and x.annotation]
            anns += [node.returns] if node.returns else []
        elif isinstance(node, ast.AnnAssign):
            anns.append(node.annotation)
        for ann in anns:
            skip.update(id(inner) for inner in ast.walk(ann))
    local: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            local |= {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
            local |= {
                inner.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store)
            }
        elif isinstance(node, ast.comprehension):
            local |= {inner.id for inner in ast.walk(node.target) if isinstance(inner, ast.Name)}
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and id(node) not in skip
        and node.id not in local
    }


def test_the_facade_code_reads_only_names_the_facade_binds() -> None:
    """A bare global read in the facade's own code never reaches the mirror, so a served
    name read there would be a ``NameError``."""
    import builtins

    unbound = sorted(
        name
        for name in _facade_code_reads()
        if name not in vars(stm) and not hasattr(builtins, name)
    )
    assert unbound == []
    assert "_SshTunnel" in _facade_code_reads()


# --------------------------------------------------------------------------- #
# A write through the facade reaches every owner that reads the name.
# --------------------------------------------------------------------------- #


def _patched_through_the_facade() -> set[str]:
    spellings = {stm.__name__: stm.__name__}
    root = Path(__file__).resolve().parent
    names: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ssh_tunnel_manager" not in text:
            continue
        names |= facade_mirror_scan.text_patched_names(text, spellings).get(stm.__name__, set())
        names |= facade_mirror_scan.patched_names(text, spellings).get(stm.__name__, set())
    return {name for name in names if name.isidentifier()}


def _reads(module: ModuleType) -> set[str]:
    tree = _tree(module)
    imports = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.lineno not in imports
    }


def _reach_gaps(patched: set[str], index: Any) -> list[str]:
    gaps = []
    for module in _owners():
        for name in sorted(patched & _reads(module)):
            if name in vars(module) and module.__name__ not in index.holders.get(name, ()):
                gaps.append(f"{module.__name__}.{name}")
    return gaps


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    assert {"_build_ssh_tunnel_argv", "_is_port_free", "_PROBE_INTERVAL"} <= patched
    assert _reach_gaps(patched, stm.__facade_mirror__) == []


def test_the_reach_scan_can_fail() -> None:
    index = stm.__facade_mirror__
    owner = f"{stm.__name__}_ports"
    holders = dict(index.holders)
    holders["_is_port_free"] = tuple(h for h in holders["_is_port_free"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"_is_port_free"}, index) == []
    assert _reach_gaps({"_is_port_free"}, blind) == [f"{owner}._is_port_free"]


def test_the_mirror_indexes_one_object_per_name() -> None:
    index = stm.__facade_mirror__
    assert index.excluded == ()
    assert index.divergent() == ()
    assert dict(index.forwards) == {}


def test_a_patch_through_the_facade_lands_on_every_reader_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners = [m for m in _owners() if "_is_port_free" in vars(m)]
    assert owners
    before = stm._is_port_free
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(stm, "_is_port_free", sentinel)
        assert [vars(m)["_is_port_free"] for m in owners] == [sentinel] * len(owners)
        assert stm._is_port_free is sentinel
    assert [vars(m)["_is_port_free"] for m in owners] == [before] * len(owners)
    assert stm._is_port_free is before
