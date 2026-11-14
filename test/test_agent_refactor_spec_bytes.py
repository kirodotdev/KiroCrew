"""The exact bytes every agent-spec writer puts on disk, pinned as golden digests.

Each scenario runs :func:`kiro_crew.agent.rebuild_agent_config` on the SHIPPED defaults,
prompts and server registry, pinning only the launcher path, the kiro-cli version and
the SEL writer. Spec files, audit records and sidecar state are normalized and compared
to SHA-256 goldens, so a dropped, reordered or re-typed field fails here even when no
field-level test names it; a mismatch prints the differing content.

The goldens carry POSIX paths and exec bits, so these run off Windows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent, agent_state
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

#: One path segment under a normalized root, with the separator run before it: a
#: Windows spec spells ``<TMP>\\bin\\kirocrew`` where POSIX spells ``<TMP>/bin/kirocrew``.
_UNDER_ROOT = re.compile(r"(<TMP>|<HOME>|<PKG>)((?:\\+[^\\\"\s]+)+)")
_SEPARATORS = re.compile(r"\\+")


class _SelRecorder:
    """Stands in for ``sel()``: records each audit call instead of writing it."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def log_api_access(self, **fields: Any) -> None:
        self._events.append({"api": fields})

    def log(self, event: Any) -> None:
        self._events.append(
            {
                "event": {
                    "event_type": event.event_type,
                    "operation": event.operation,
                    "outcome": event.outcome,
                    "source": event.source,
                    "resources": event.resources,
                    "error": getattr(event, "error", None),
                }
            }
        )


class _Materialized:
    """One rebuild's full output, normalized for comparison."""

    def __init__(
        self, files: dict[str, str], events: list[Any], state: str, unrefreshed: list[str]
    ) -> None:
        self.files = files
        self.events = events
        self.state = state
        self.unrefreshed = unrefreshed

    def digests(self) -> dict[str, Any]:
        return {
            "files": {name: _sha(text) for name, text in sorted(self.files.items())},
            "events": _sha(json.dumps(self.events, sort_keys=True)),
            "state": _sha(self.state),
            "unrefreshed": self.unrefreshed,
        }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Rig:
    """A private agents directory plus the pinned machine-specific inputs."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.agents = tmp_path / "agents"
        self.agents.mkdir()
        # The mate conductor and worker specs are written to the crewmate folder
        # (``mate_agents_dir``), not here; their bytes are pinned all the same.
        self.mates = tmp_path / "mates" / "agents"
        bindir = tmp_path / "bin"
        bindir.mkdir()
        self.bin = bindir / "kirocrew"
        self.bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.bin.chmod(0o755)
        self.home = Path(os.environ["KIROCREW_HOME"])
        self.kiro_mcp = tmp_path / "kiro-global-mcp.json"
        self.hooks_dir = tmp_path / "hooks"
        self.hooks_dir.mkdir()
        self.events: list[dict[str, Any]] = []
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", self.agents)
        monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: self.mates)
        monkeypatch.setattr(agent, "_KIROCREW_BIN", str(self.bin))
        monkeypatch.setattr(agent, "_KIRO_MCP_JSON", self.kiro_mcp)
        monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", self.hooks_dir)
        monkeypatch.setattr(agent, "sel", lambda: _SelRecorder(self.events))
        monkeypatch.setattr(
            "kiro_crew.apps.bridges._mcp_json_path", lambda: self.agents / "kirocrew.json"
        )
        monkeypatch.setattr(
            "kiro_crew.kiro_cli.installed_kiro_cli_version",
            lambda: SPEC_PERMISSIONS_MIN_VERSION,
        )

    def executable(self, name: str) -> Path:
        path = self.tmp / "bin" / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def config(self, data: dict[str, Any]) -> None:
        self.write_json(self.home / "config.json", data)

    def normalize(self, text: str) -> str:
        """Replace this run's scratch and home roots with labels, in every spelling.

        Roots appear as-is or JSON-escaped once or twice; separators under a root fold
        to ``/``. ``<PKG>`` is the installed package (the prompt names its docs index).
        A root under ``$HOME`` is also spelled ``~/...`` (``skill://`` resources, and the
        Windows scratch home), and both forms fold to one label.
        """
        package = Path(agent.__file__).resolve().parent
        bases = {
            self.tmp: "<TMP>",
            self.tmp.resolve(): "<TMP>",
            self.home: "<HOME>",
            self.home.resolve(): "<HOME>",
            package: "<PKG>",
        }
        # A spec may also write a root forward-slashed (``Path.as_posix()``, as a
        # ``skill://`` resource does), which on Windows differs from ``str()``.
        roots = {str(p): label for p, label in bases.items()}
        roots.update({p.as_posix(): label for p, label in bases.items()})
        for base, label in bases.items():
            try:
                relative = base.relative_to(Path.home())
            except ValueError:
                continue
            roots[f"~/{relative.as_posix()}"] = label
            roots[f"~/{relative}"] = label
        spellings: dict[str, str] = {}
        for root, label in roots.items():
            once = json.dumps(root)[1:-1]
            for spelled in (root, once, json.dumps(once)[1:-1]):
                spellings[spelled] = label
        for spelling in sorted(spellings, key=len, reverse=True):
            text = text.replace(spelling, spellings[spelling])
        return _UNDER_ROOT.sub(lambda m: m.group(1) + _SEPARATORS.sub("/", m.group(2)), text)

    def snapshot(self) -> _Materialized:
        files = {
            p.name: self.normalize(p.read_text(encoding="utf-8"))
            for folder in (self.agents, self.mates)
            if folder.is_dir()
            for p in sorted(folder.iterdir())
            if p.is_file() and not p.name.startswith(".")
        }
        state_path = agent_state._state_path()
        state = state_path.read_text(encoding="utf-8") if state_path.is_file() else ""
        if state:
            # The worker's mirror bookkeeping records the default spec's file identity
            # and content fingerprint, both of which carry this run's scratch paths. What
            # is contractual is that they describe the default spec now on disk.
            parsed = json.loads(state)
            # The dashboard-author ownership digest is the SHA-256 of the installed spec's
            # bytes, which carry this run's scratch paths (the kirocrew-core launcher). What
            # is contractual is that it equals the digest of the dashboard-author spec now on
            # disk -- so fold it to a stable label when it does, exactly as the worker's
            # mirror bookkeeping below is folded.
            da_file = self.agents / "kirocrew-dashboard-author.json"
            da_digest = None
            if da_file.is_file():
                da_digest = agent_state.spec_digest(json.loads(da_file.read_text(encoding="utf-8")))
            for entry in parsed.values():
                if not isinstance(entry, dict):
                    continue
                if da_digest is not None and entry.get("managed_digest") == da_digest:
                    entry["managed_digest"] = "<DASHBOARD-AUTHOR-DIGEST>"
                if entry.get("mirrored_stat") == agent.default_spec_identity():
                    entry["mirrored_stat"] = "<DEFAULT-SPEC-IDENTITY>"
                if entry.get("mirrored_from") == agent.default_spec_fingerprint():
                    entry["mirrored_from"] = "<DEFAULT-SPEC-FINGERPRINT>"
            state = json.dumps(parsed, indent=2, sort_keys=True)
        events = json.loads(self.normalize(json.dumps(self.events, sort_keys=True, default=str)))
        unrefreshed = sorted(agent._fork_refresh_failed)
        return _Materialized(files, events, self.normalize(state), unrefreshed)


# ── scenarios ────────────────────────────────────────────────────────────────


def _fresh(rig: _Rig) -> dict[str, Any]:
    """A first install: no spec on disk, no MCP sources, no user config."""
    return {}


def _customized(rig: _Rig) -> dict[str, Any]:
    """An existing spec a user has customized, with every MCP source populated."""
    tool = rig.executable("some-mcp")
    rig.write_json(
        rig.agents / "kirocrew.json",
        {
            "name": "kirocrew",
            "description": "customized",
            "model": "claude-opus-4.6-1m",
            "prompt": "file:///somewhere/else/prompt.md",
            "tools": ["fs_read", "@kirocrew-cron", "@kirocrew-core", "@user-srv", "@gone/tool"],
            "allowedTools": ["fs_read", "@kirocrew-core", "@user-srv/do_it", "@gone/tool"],
            "resources": [],
            "toolsSettings": {
                "execute_bash": {
                    "deniedCommands": ["rm -rf /"],
                    "autoAllowReadonly": True,
                    "allowedCommands": ["ls"],
                },
                "subagent": {
                    "availableAgents": ["kirocrew-worker", "review-*"],
                    "trustedAgents": ["kirocrew-worker"],
                },
                "fs_write": {"allowedPaths": ["~/work"]},
            },
            "mcpServers": {
                "kirocrew-cron": {
                    "command": "/stale/kirocrew",
                    "args": ["mcp-cron"],
                    "timeout": 90000,
                    "url": "http://stale",
                    "env": {"FOO": "bar", "HOME": "/elsewhere", "PATH": "/x"},
                    "autoApprove": ["cron_list"],
                },
                "user-srv": {"command": str(tool), "args": ["--serve"], "disabledTools": ["x"]},
            },
            "hooks": {"preToolUse": [{"command": "/bin/true"}]},
            "unknownTopLevel": {"kept": True},
        },
    )
    rig.write_json(
        rig.kiro_mcp,
        {
            "mcpServers": {
                "global-srv": {"command": str(tool), "args": ["g"], "timeout": 5},
                "npm:@scope/pkg": {"command": str(tool), "args": ["scoped"]},
                "missing-bin": {"command": "definitely-not-on-path-b08", "args": []},
                "no-command": {"args": ["x"]},
                "muted-srv": {"command": str(tool), "disabled": True},
                "remote-srv": {
                    "url": "https://mcp.example.test/mcp",
                    "oauth": {"scopes": ["read"], "clientId": "cid"},
                },
            }
        },
    )
    rig.write_json(
        rig.home / "mcp.json",
        {
            "mcpServers": {
                "store-srv": {"command": str(tool), "args": ["store"], "env": {"A": "1"}},
                "global-srv": {"env": {"B": "2"}},
            }
        },
    )
    rig.write_json(rig.home / "agent.json", {"toolsSettings": {"custom_tool": {"k": "v"}}})
    return {}


def _governed(rig: _Rig) -> dict[str, Any]:
    """The customized install under a ceiling that denies some auto-approvals."""
    _customized(rig)
    return {
        "may_auto_approve": lambda ref: ref
        not in {"fs_read", "@kirocrew-core", "@global-srv", "@kirocrew-core/select_crew"}
    }


def _clean_over_customized(rig: _Rig) -> dict[str, Any]:
    """A ``--clean`` rebuild over the customized install."""
    _customized(rig)
    return {"clean": True}


def _user_hooks(rig: _Rig) -> dict[str, Any]:
    """Explicit hooks in both spec shapes plus an autoimported script."""
    guard = rig.executable("guard.sh")
    script = rig.hooks_dir / "audit-post.sh"
    script.write_text("#!/bin/sh\n# matcher: fs_write\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    off = rig.hooks_dir / "off-pre.sh"
    off.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    off.chmod(0o755)
    rig.config(
        {
            "agent": {
                "kiro_hooks": [
                    {
                        "name": "guard",
                        "trigger": "PreToolUse",
                        "matcher": "execute_bash",
                        "action": {"type": "command", "command": str(guard)},
                    },
                    {
                        "trigger": "PostFileSave",
                        "action": {"type": "command", "command": str(guard)},
                    },
                    {
                        "trigger": "Stop",
                        "enabled": False,
                        "action": {"type": "command", "command": str(off)},
                    },
                    {"trigger": "nope", "action": {"type": "command", "command": "x"}},
                ],
                "kiro_hooks_autoimport": True,
            }
        }
    )
    return {}


def _object_hooks(rig: _Rig) -> dict[str, Any]:
    """The object-of-arrays hook shape, with the rejections it audits."""
    guard = rig.executable("guard2.sh")
    rig.config(
        {
            "agent": {
                "kiro_hooks": {
                    "preToolUse": [
                        {"command": str(guard), "matcher": "fs_*"},
                        {"command": str(guard), "matcher": "fs_*"},
                        {"command": "relative.sh"},
                        {"matcher": "x"},
                    ],
                    "fileEdited": [{"command": str(guard)}],
                    "bogusEvent": [{"command": str(guard)}],
                    "stop": "not-a-list",
                },
                "kiro_hooks_autoimport": False,
            }
        }
    )
    return {}


def _registry_mode(rig: _Rig) -> dict[str, Any]:
    """An install the operator declared registry-governed."""
    rig.config({"agent": {"mcp_registry_mode": True, "model": "claude-sonnet-4.5"}})
    return {}


def _forks(rig: _Rig) -> dict[str, Any]:
    """Two private template copies: one corroborated by a crew binding, one orphaned."""
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agents = {"my-crew": KiroCrewAgentConfig(kiro_agent="my-crew")}
    cfg.save()
    for name in ("my-crew", "orphan-crew"):
        rig.write_json(
            rig.agents / f"{name}.json",
            {
                "name": name,
                "prompt": "file:///old-home/.kiro/crew/prompt.md",
                "tools": ["fs_read", "@kirocrew-core"],
                "allowedTools": ["fs_read", "@kirocrew-core", 7],
                "toolsSettings": {
                    "execute_bash": {"deniedCommands": ["rm"]},
                    "subagent": {"availableAgents": "not-a-list"},
                },
                "mcpServers": {"kirocrew-core": {"command": "/old", "autoApprove": ["x"]}},
                "hooks": {"old": "hook"},
            },
        )
        agent_state.set_fork_info(name, forked_from="kirocrew", private_to=name)
    return {"may_auto_approve": lambda ref: ref != "@kirocrew-core"}


SCENARIOS: dict[str, Callable[[_Rig], dict[str, Any]]] = {
    "fresh": _fresh,
    "customized": _customized,
    "governed": _governed,
    "clean_over_customized": _clean_over_customized,
    "user_hooks": _user_hooks,
    "object_hooks": _object_hooks,
    "registry_mode": _registry_mode,
    "forks": _forks,
}


def materialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> _Materialized:
    """Run one scenario's rebuild in a private rig and return its normalized output."""
    rig = _Rig(tmp_path, monkeypatch)
    options = SCENARIOS[scenario](rig)
    if "may_auto_approve" in options:
        monkeypatch.setattr(agent, "_may_auto_approve", options["may_auto_approve"])
    agent.rebuild_agent_config(clean=options.get("clean", False))
    return rig.snapshot()


#: Digests recorded from the pre-split ``kiro_crew.agent``. See the module docstring.
GOLDEN: dict[str, dict[str, Any]] = {
    "clean_over_customized": {
        "events": "8a1292a50cf6d7b109e4bfdb616cccb735a3e7af58d474f78f45657aa864e06f",
        "files": {
            "kirocrew-conductor.json": "ce4ad37b1c03c893b717b39404692291ce55acbf13a4b70d97aa9b475e4add97",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "460cc18f6f016620e3867bfd231b916c9834b5ce04a892dc48a1bc80c7d8c891",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "b81902318c386504da2a9af9556f3a357c8656ef650e9a205fe55d51c1521b9c",
            "kirocrew-mate-worker.json": "946db9c064d3cb95ad5075e1c8ce52f42154fb5cfe703109023ecce137c861ec",
            "kirocrew-pipeline-conductor.json": "34085c2879f750abdfe39e75cacc1ed5010ec461be48248cf12479c72a13f53c",
            "kirocrew-research.json": "e6f3ae6eaa577c909b7d28a9d3637efb8e75548312641b497d4c50418c9130b1",
            "kirocrew-security-conductor.json": "60e9d78a6125fb02dc2e8a30bc3df8de70da7587c86dd83b852a1c75efd5789b",
            "kirocrew-team-lead.json": "381dc67bf4b76aca83cf446e67a1f57f88a5313a3ca091831d4164908e044acb",
            "kirocrew-worker.json": "16da3f4a9a5261872162436880a3293f9ce2dcd24e27d19626e91833d85b9328",
            "kirocrew.json": "87525484764ecfa820f2e8997998af593f147f5836b6b2b97b69fef0a491e332",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "customized": {
        "events": "f21615ae2b4420abba400489a4b233c1f9375fb53e09eddf7c927bb3f3b6de5d",
        "files": {
            "kirocrew-conductor.json": "ce4ad37b1c03c893b717b39404692291ce55acbf13a4b70d97aa9b475e4add97",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "460cc18f6f016620e3867bfd231b916c9834b5ce04a892dc48a1bc80c7d8c891",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "b81902318c386504da2a9af9556f3a357c8656ef650e9a205fe55d51c1521b9c",
            "kirocrew-mate-worker.json": "946db9c064d3cb95ad5075e1c8ce52f42154fb5cfe703109023ecce137c861ec",
            "kirocrew-pipeline-conductor.json": "34085c2879f750abdfe39e75cacc1ed5010ec461be48248cf12479c72a13f53c",
            "kirocrew-research.json": "e6f3ae6eaa577c909b7d28a9d3637efb8e75548312641b497d4c50418c9130b1",
            "kirocrew-security-conductor.json": "60e9d78a6125fb02dc2e8a30bc3df8de70da7587c86dd83b852a1c75efd5789b",
            "kirocrew-team-lead.json": "381dc67bf4b76aca83cf446e67a1f57f88a5313a3ca091831d4164908e044acb",
            "kirocrew-worker.json": "a23417eacc52d4c051c198388a3bb6fe0b4397dd28989b6386b674dfdb46ef55",
            "kirocrew.json": "40bd42855deef2b045a474f0ee2244f6436e3c2047efabe96abe8100d3f65cd0",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "forks": {
        "events": "3df8217b2b62a2c0b59480c2c3b7da5f3e3ef2c891b45e490b7f1e9204526252",
        "files": {
            "kirocrew-conductor.json": "b324b79ff7796be36366beae67b081efa30b09d61b0a476a4c57265c7d158480",
            "kirocrew-dashboard-author.json": "13b26acb9db5423062b4ec8a1a27de3887cdb528a93754363ee428a4fea02a14",
            "kirocrew-dashboard-manager.json": "3e9ab1723cfef7458566b28bab684e8df7c8d073c5d166cd3d2df39ed3eb6586",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "7e9802b7c60257f085a588294dc93925267777a0a3d024d59266be2bf92cdb53",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "03471f71e2e758ccaccde5ecdf8183c91f3f8b0203f23053a624c2e62e2d13e1",
            "kirocrew-mate-worker.json": "ea79171326945785f21730a383bc0cace9abdb7f0b0e227c1f308d816e84f151",
            "kirocrew-pipeline-conductor.json": "ec858a90ea7aa6b846b169a8090d6d4cb4ba5dd0c883738a91bf04ad1af8f5c0",
            "kirocrew-research.json": "226891438f47950133f9fde98d5d7877321a2af2072ea47a4183337dc15d471b",
            "kirocrew-security-conductor.json": "f9ae12d011e1c9243a30367544032f2d605412f79ce1be7cec6a5df062aec9c3",
            "kirocrew-team-lead.json": "ee55667d96bf57a0e4295d564e62404a3fa058a1c8dc3b4513f22708690af3ab",
            "kirocrew-worker.json": "b1d3400b0e7495160512b0c404cd28d2a6c9e738f1a7c822469eb7fc79a70fc1",
            "kirocrew.json": "9d0783af8f9da1a0d3f7602fdd7ee0746171be63926bfc53978b3e2609bade2f",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "a041707d1f911fd7eaae2ca2afbe4304e67d03bb66dc5a3cabd65c8c55538309",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "cbb4cfdf2675dd8efeed9a8b77fc63ce287172f38fe4db9fad37ecb6654ed485",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "93c96177077b06656eaa1c26e81098e451c2a5dcae0d1966b8194e339fec1011",
        "files": {
            "kirocrew-conductor.json": "b324b79ff7796be36366beae67b081efa30b09d61b0a476a4c57265c7d158480",
            "kirocrew-dashboard-author.json": "13b26acb9db5423062b4ec8a1a27de3887cdb528a93754363ee428a4fea02a14",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "7e9802b7c60257f085a588294dc93925267777a0a3d024d59266be2bf92cdb53",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "da334f8566ef749495b9668a76c7ab99dae0040e0be6c988c982e0e85957369b",
            "kirocrew-mate-worker.json": "6ef7c53b6496a79508f205d9549cf689c1e0109ec36ebcf7d46f98a63458df71",
            "kirocrew-pipeline-conductor.json": "ec858a90ea7aa6b846b169a8090d6d4cb4ba5dd0c883738a91bf04ad1af8f5c0",
            "kirocrew-research.json": "f4ad1ea7fce492420f5e93905ff38ee0ef97cf362089e56aa8a51862c6266921",
            "kirocrew-security-conductor.json": "f9ae12d011e1c9243a30367544032f2d605412f79ce1be7cec6a5df062aec9c3",
            "kirocrew-team-lead.json": "5ab03f58283944b652d7293a969053c5524535326afa5fb30dedae3604a7485e",
            "kirocrew-worker.json": "288f8ac9392b1130b4386048fc6955d2b5b85ea6215c86b0e306fac434011928",
            "kirocrew.json": "8781b5ae68578a28a167334ed4bd9c66ed93374185fb3d0b11b08ca8302cc222",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "governed": {
        "events": "666bce07d1879ed8008fc0f90b45ec22cd94b718c2a874548d42cd3315b6f5a9",
        "files": {
            "kirocrew-conductor.json": "b0143c35a4017f0cb956ee1805c4dff1aadf4ae8fa150f2ae1d8dc80b8429c08",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "e226e881e8349eae48df6539b2b8f06cf2e1e0e8a708df1abb16e2241909f655",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "ca9a7c1447dfd7cc6c46b86a0b5313856937f5c5d66bf4b9210c1d19b17f8b50",
            "kirocrew-mate-worker.json": "3e31ba208c734b23b30bd93ad28e3ed0bf41b0f686e334d859316e870fd1cd51",
            "kirocrew-pipeline-conductor.json": "34085c2879f750abdfe39e75cacc1ed5010ec461be48248cf12479c72a13f53c",
            "kirocrew-research.json": "0cb7038ce95c848b4fd6625a85cf9eccd0ef87224ce2f8ad65c0389990b5c7bb",
            "kirocrew-security-conductor.json": "60e9d78a6125fb02dc2e8a30bc3df8de70da7587c86dd83b852a1c75efd5789b",
            "kirocrew-team-lead.json": "0fe8e47005da456253700fc23adc332cb445cafd809f0e38350e3f514272cef6",
            "kirocrew-worker.json": "d040da78c69d4b01f34315feb088188a4dc12a1e12d488cec4be1fc6f08d6ab4",
            "kirocrew.json": "0dc8734e6cf2eb9ece78ebe1a72c28ca2b80f1a780ef07287d1640b5d201a9b3",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "00ad0b59a7575f63d3571802d1424b4146cc87a01e567590508ebd4832f572d1",
        "files": {
            "kirocrew-conductor.json": "e5c496c10f41a2dc8095530c26a055857dc62d32a762054f37c8081bb942fae9",
            "kirocrew-dashboard-author.json": "9f1f2a77b3bec1d748134ce04ac3c329df51a15c2d01a8891a4b4ff5d17eb0a9",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "8e93c923fe3246e3b248d41e90bd484ab6d99b2a22d2c7f5fb9af8cefe25e2d5",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "1567a56945c0d6f4866ede268a7c9408e236e4de53e9aadbdb78c71a8d7d466f",
            "kirocrew-mate-worker.json": "82458a8fb923878cbb92eda81316840a7e3ebde2fa550ce7d0deec205356a535",
            "kirocrew-pipeline-conductor.json": "f85b7bc5279d60b70c08c607a3e96f8e16d923ff247bd9dc7122d1b725b3cb86",
            "kirocrew-research.json": "7666a438bbdfc5d547687d5b473d04471aaddf9757194e6227669d9e86182980",
            "kirocrew-security-conductor.json": "35bae9635bf2cb3964df11bb30e957e742db2360bb22a9db580ed2d5fc14a287",
            "kirocrew-team-lead.json": "b963e007b4f836467cd201f283a5e91fa46ea1df96d01ae286a0125b7794d5b7",
            "kirocrew-worker.json": "56441f7a4833a8d1e84fd3877e83d1f85442169e779e46f63dfc5e74762eea56",
            "kirocrew.json": "3ab4930fa620e88feb1b25022777965434cf8b95942ad2a8f36586c574c75fe8",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "93c96177077b06656eaa1c26e81098e451c2a5dcae0d1966b8194e339fec1011",
        "files": {
            "kirocrew-conductor.json": "dbf098eeaf245a0254cc350992ec69964fcedb102eaed0519691d237153c0bf5",
            "kirocrew-dashboard-author.json": "9474463f6cdc27fc7b2e592a7037a78542a38392cc1a7b350529bb1889110b5a",
            "kirocrew-dashboard-manager.json": "77f2f143beeba05ea8d04c7a954a584bac30ef56ddfd28f497698b714f7a4647",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "67b72c1885db892bcee38baeeb2b82647b26f21eb67164a56fc80ec058a23bb5",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "d94de6ff2dbd19aded2cd4089e51a7d4b001bde5986231124e04b0be4fd1537c",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "e646f13c752d0046bd943f7ab6157b8e708cd123c20c386149460e6147c1018a",
            "kirocrew-mate-worker.json": "9bde55d468872d3dba385b666956056bd21ad4ed2dc4d8e5a9d43f222fba1579",
            "kirocrew-pipeline-conductor.json": "74a9cbc00cc1fc482ba7773568b7cf36993d98229c994d9aa7c4d9d264458cc3",
            "kirocrew-research.json": "4f6418d8353ac284af564ffe464e7e4852f0af510ba05c424387a7f0f6b9d961",
            "kirocrew-security-conductor.json": "3f756ce0a18f71487da7aea2d6e87d7811cfb659711945ee316070d86208ac33",
            "kirocrew-team-lead.json": "71d4beeeafec2feb52f9ce80d5f717eee8c57e40cde80fb991f9d8bd3ded6a16",
            "kirocrew-worker.json": "dcdcbe197349887856a752f1ba8c92c0fe5c99cbc7b6ba3c592faf44f3dad5a7",
            "kirocrew.json": "7632bfd1cc68721772a6e67ccf9b42a5a45d7a8bbdc40ca96de739da68280641",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "77c888bf115c62f26548733291ed78336f6421baf700190ea8e44a8c1a3c6d41",
        "files": {
            "kirocrew-conductor.json": "8a371ef9f8e9ca2a9139e001a8115fc36c33df1231d20a7f9a08fbd4c4b48e73",
            "kirocrew-dashboard-author.json": "ed8fa5a35960b358808bd8ba677678ea358a115703c11819f3a37c50aafd2bb2",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "3b3bc8e613c7f01654536d670a20f5612523afea675856d0a5ff22c81373c13a",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "a0cebaa2e4f06bd97e2006a07e74e8487b8c1fc2958317833ddefc97ef17716e",
            "kirocrew-mate-worker.json": "53ce544ab2be35ca21f4f3c89ce0b14f1474466226e6a1c70d61b04a5dd5f8d9",
            "kirocrew-pipeline-conductor.json": "7311b58489c28abc327c99329524ea1b0b340c416664005122472b26141d4020",
            "kirocrew-research.json": "7db3afde79a24d50e572046bc40531b845cfa17972dc2e0c408dc3be648f769a",
            "kirocrew-security-conductor.json": "6ca9e0531f16c3ba9552d40356e85cde3306d47dfb5ca5702de6b54f42b858d5",
            "kirocrew-team-lead.json": "7897fc7e02dc9469b2af896180b9e60c48be95bd1f5bd23cff177a3eb2e4d49f",
            "kirocrew-worker.json": "84aea3eefcc860b010913f190140f7d0879a3207e2c56b26bf72970e9a7e0a78",
            "kirocrew.json": "1a97ecab3ec5f142700b7c3a10d41343daa59d9f98d555da515203a208ac5dd3",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_written_spec_matches_the_pre_split_bytes(
    scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = materialize(tmp_path, monkeypatch, scenario)
    expected = GOLDEN[scenario]
    digests = got.digests()
    assert sorted(digests["files"]) == sorted(expected["files"]), "a spec file appeared or vanished"
    # The goldens are CI's bytes: a developer host resolves different binaries and
    # gates, so they cannot be regenerated locally. A mismatch therefore prints
    # every digest this run produced, so one CI round is enough to re-pin a
    # deliberate change.
    regen = f"this run's digests for {scenario!r}:\n" + json.dumps(
        digests, indent=1, sort_keys=True
    )
    for name, digest in expected["files"].items():
        assert (
            digests["files"][name] == digest
        ), f"{scenario}: {name} no longer matches the pre-split bytes:\n{got.files[name]}\n{regen}"
    assert digests["events"] == expected["events"], (
        f"{scenario}: the audit record sequence changed:\n"
        + json.dumps(got.events, indent=1, sort_keys=True)
        + f"\n{regen}"
    )
    assert (
        digests["state"] == expected["state"]
    ), f"{scenario}: the agent-state sidecar changed:\n{got.state}\n{regen}"
    assert digests["unrefreshed"] == expected["unrefreshed"], "the fork refresh verdict changed"
