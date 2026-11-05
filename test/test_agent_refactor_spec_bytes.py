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
            "kirocrew-conductor.json": "88738b4c4552753bf4beffed31420397d1fad17b8f2e907197c00d0cccfe2dba",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "9dee4c7024fe8c5da01b07f23d65530517bb6c9a6df500bfe53d20ff29fe3cfd",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "d127665356bfa3cc011838298bb733a4acd88e030f9c6d1ed24da5dc65a90ebd",
            "kirocrew-mate-worker.json": "0a4a9067c034668d79adc5d62514d2acb9b4fff0df71a5d521d36d5ebd5f3fbc",
            "kirocrew-pipeline-conductor.json": "614f7957f829a429ca89b2b958b17c508b9b083b5a002c7dfa61b407fea08bcd",
            "kirocrew-research.json": "e6f3ae6eaa577c909b7d28a9d3637efb8e75548312641b497d4c50418c9130b1",
            "kirocrew-security-conductor.json": "e9ee2176a1118a6a904d9ca49c537204c8cbfea97112dadf4cdd581a8415b06c",
            "kirocrew-team-lead.json": "aea6c8aca27e4f99d243cc17e4e015da840dc7a5c55f44ddefcf3b16873650dd",
            "kirocrew-worker.json": "193ada41741be200e97cca8dc0aedfe546a3f735d1802c2aaa1f907246c9e666",
            "kirocrew.json": "87525484764ecfa820f2e8997998af593f147f5836b6b2b97b69fef0a491e332",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "customized": {
        "events": "f21615ae2b4420abba400489a4b233c1f9375fb53e09eddf7c927bb3f3b6de5d",
        "files": {
            "kirocrew-conductor.json": "88738b4c4552753bf4beffed31420397d1fad17b8f2e907197c00d0cccfe2dba",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "9dee4c7024fe8c5da01b07f23d65530517bb6c9a6df500bfe53d20ff29fe3cfd",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "d127665356bfa3cc011838298bb733a4acd88e030f9c6d1ed24da5dc65a90ebd",
            "kirocrew-mate-worker.json": "0a4a9067c034668d79adc5d62514d2acb9b4fff0df71a5d521d36d5ebd5f3fbc",
            "kirocrew-pipeline-conductor.json": "614f7957f829a429ca89b2b958b17c508b9b083b5a002c7dfa61b407fea08bcd",
            "kirocrew-research.json": "e6f3ae6eaa577c909b7d28a9d3637efb8e75548312641b497d4c50418c9130b1",
            "kirocrew-security-conductor.json": "e9ee2176a1118a6a904d9ca49c537204c8cbfea97112dadf4cdd581a8415b06c",
            "kirocrew-team-lead.json": "aea6c8aca27e4f99d243cc17e4e015da840dc7a5c55f44ddefcf3b16873650dd",
            "kirocrew-worker.json": "df8fbfa183a452b91e9e339751fb26578414413d85a6602cce8e0c7e0c9181a3",
            "kirocrew.json": "40bd42855deef2b045a474f0ee2244f6436e3c2047efabe96abe8100d3f65cd0",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "forks": {
        "events": "3df8217b2b62a2c0b59480c2c3b7da5f3e3ef2c891b45e490b7f1e9204526252",
        "files": {
            "kirocrew-conductor.json": "8c9319c3dd229ccc8f35358b1b11927b09d2641a25737bd13b24b6e5a063e64d",
            "kirocrew-dashboard-author.json": "13b26acb9db5423062b4ec8a1a27de3887cdb528a93754363ee428a4fea02a14",
            "kirocrew-dashboard-manager.json": "3e9ab1723cfef7458566b28bab684e8df7c8d073c5d166cd3d2df39ed3eb6586",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "982bcba581171c979bf90e61f8285d1191d8deb771a1ad4cb280696c9c425fbc",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "da1ebbd2d5a9d709d6c1a267f08a40e396df4b725ab0ed601deddfa013b230f9",
            "kirocrew-mate-worker.json": "5935f617325543ebea6f892efb98ae3fe4f034a39ea1a80feee3561274b4297c",
            "kirocrew-pipeline-conductor.json": "d6d6bcd4eae08f1938eb662f6a6c94a6d187c3740b06326c5b531298f3ce0516",
            "kirocrew-research.json": "226891438f47950133f9fde98d5d7877321a2af2072ea47a4183337dc15d471b",
            "kirocrew-security-conductor.json": "1d856d0d6fe86e9739ef87c970ef9fff58022b48d7391ac5f8cc68b7bb653a40",
            "kirocrew-team-lead.json": "13183803ed83e192aca7edb02f504a9a0fae385b0e58228f5a5f4a7430df4122",
            "kirocrew-worker.json": "5a204f86863170bd74353b4252ec890ccf886cbc77020308cd1a7b524cada917",
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
            "kirocrew-conductor.json": "8c9319c3dd229ccc8f35358b1b11927b09d2641a25737bd13b24b6e5a063e64d",
            "kirocrew-dashboard-author.json": "13b26acb9db5423062b4ec8a1a27de3887cdb528a93754363ee428a4fea02a14",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "982bcba581171c979bf90e61f8285d1191d8deb771a1ad4cb280696c9c425fbc",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "82c8439561560ff12b0dcea7a42888c533256d05cecadcaa787b6405f333b2f1",
            "kirocrew-mate-worker.json": "3489d9a8cfc2365143ddf73af11a2020b77d2b8bed6c27bfd5e30411132a587e",
            "kirocrew-pipeline-conductor.json": "d6d6bcd4eae08f1938eb662f6a6c94a6d187c3740b06326c5b531298f3ce0516",
            "kirocrew-research.json": "f4ad1ea7fce492420f5e93905ff38ee0ef97cf362089e56aa8a51862c6266921",
            "kirocrew-security-conductor.json": "1d856d0d6fe86e9739ef87c970ef9fff58022b48d7391ac5f8cc68b7bb653a40",
            "kirocrew-team-lead.json": "1fe2eec8145614c4b7e61d750a0c67adbe33c276d1087e18abcc86b421ba8b2b",
            "kirocrew-worker.json": "20516bd4bd193b114c73a0cb3fee95fbdcf62bf7aef57500de670126b4f279fa",
            "kirocrew.json": "8781b5ae68578a28a167334ed4bd9c66ed93374185fb3d0b11b08ca8302cc222",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "governed": {
        "events": "666bce07d1879ed8008fc0f90b45ec22cd94b718c2a874548d42cd3315b6f5a9",
        "files": {
            "kirocrew-conductor.json": "33dd7aed889dded7525e2d9555eba8786dc530142ef4cdf4b9b72dd2165a5014",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "12420808155bcce04c6be0e18d05d9be90d04823ed16d30502f9c0035fbaf32e",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "b11c54be2b00a40bf732d9a646b35d843acd1e3e6627f04b74fe00910cdb0f92",
            "kirocrew-mate-worker.json": "d590deb13daedc9b9d647d44d8d8b2777f3d414083e09c1b2120ddbbb7c2d6e8",
            "kirocrew-pipeline-conductor.json": "614f7957f829a429ca89b2b958b17c508b9b083b5a002c7dfa61b407fea08bcd",
            "kirocrew-research.json": "0cb7038ce95c848b4fd6625a85cf9eccd0ef87224ce2f8ad65c0389990b5c7bb",
            "kirocrew-security-conductor.json": "e9ee2176a1118a6a904d9ca49c537204c8cbfea97112dadf4cdd581a8415b06c",
            "kirocrew-team-lead.json": "bdec8a0b56bae1ec26866a98ffa83c1d7bd6a5244f1c7de08db6b62e0d094356",
            "kirocrew-worker.json": "1dd8aceb09643a62129e93cfe70fa7bfea870081fc565839bd1976049575b4a7",
            "kirocrew.json": "0dc8734e6cf2eb9ece78ebe1a72c28ca2b80f1a780ef07287d1640b5d201a9b3",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "00ad0b59a7575f63d3571802d1424b4146cc87a01e567590508ebd4832f572d1",
        "files": {
            "kirocrew-conductor.json": "1781865478ac2faa0e9b29b17b4e843b2a19377ce04d12d7e1ed27e2b5137c22",
            "kirocrew-dashboard-author.json": "9f1f2a77b3bec1d748134ce04ac3c329df51a15c2d01a8891a4b4ff5d17eb0a9",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "533f22349593f2f333108d7127eeec03f6e58e62e855c5ca3d0e1e0350ccc801",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "60d756197b1dd976a8c56e7ff65d61a680f589daf172f06053a9d8c6ca36a497",
            "kirocrew-mate-worker.json": "c60d3ae4a642053da7b03240a78545045a39d8d696b9c43784b34c4834b19d46",
            "kirocrew-pipeline-conductor.json": "f51d2fdc1841d3a583fdea41b493d9ca20fde1f49da16a35fa037c06f055f401",
            "kirocrew-research.json": "7666a438bbdfc5d547687d5b473d04471aaddf9757194e6227669d9e86182980",
            "kirocrew-security-conductor.json": "5163b308dfb14758da40a7485343ad6565072b9bdd353b09df8d5010f9a88566",
            "kirocrew-team-lead.json": "c21be55f04982f2fdc6df1833e5ce64fabaa3855f4f6252104fceb366aca90f7",
            "kirocrew-worker.json": "b6b84ae38b174f0f093439ed0ca50324419320a0c80223b6ea31c3ddc4b6d116",
            "kirocrew.json": "3ab4930fa620e88feb1b25022777965434cf8b95942ad2a8f36586c574c75fe8",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "93c96177077b06656eaa1c26e81098e451c2a5dcae0d1966b8194e339fec1011",
        "files": {
            "kirocrew-conductor.json": "f2ecf62f0961461a4bc882765ef6a2d996b4c483e6062b183df8e540afdb62a1",
            "kirocrew-dashboard-author.json": "9474463f6cdc27fc7b2e592a7037a78542a38392cc1a7b350529bb1889110b5a",
            "kirocrew-dashboard-manager.json": "77f2f143beeba05ea8d04c7a954a584bac30ef56ddfd28f497698b714f7a4647",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "67b72c1885db892bcee38baeeb2b82647b26f21eb67164a56fc80ec058a23bb5",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "574b2bcad80fdabe7b117de16c609a9ac48eb76b8568fcf92b87e3bc2e63f96d",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "b288394068648228d66508d4d23751046686068676db298a76cae9af3c4927b8",
            "kirocrew-mate-worker.json": "353a12795c35e39dd35832d1c3e34372301f7a2541b4f41855b711b3c96a17fe",
            "kirocrew-pipeline-conductor.json": "e66da354ac321483c1355da36087b3bc06810a8e72ed57cdaad9914430b1e821",
            "kirocrew-research.json": "4f6418d8353ac284af564ffe464e7e4852f0af510ba05c424387a7f0f6b9d961",
            "kirocrew-security-conductor.json": "28c91b87981fceaa967a2611e7af85b9bf7ee5eb731c3b669c0e1761e0372dd7",
            "kirocrew-team-lead.json": "4363d670adc172f144fda5f98a579c97cdaeecfde0b991417cbf405dd7b81bce",
            "kirocrew-worker.json": "a1bd604299229e35fcfad9c16a75cc1ae456499279f0b4ba29e9992860512146",
            "kirocrew.json": "7632bfd1cc68721772a6e67ccf9b42a5a45d7a8bbdc40ca96de739da68280641",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "77c888bf115c62f26548733291ed78336f6421baf700190ea8e44a8c1a3c6d41",
        "files": {
            "kirocrew-conductor.json": "9058cdf92acf17de7e14e41f27c96d0b2aaa8496c893bcbced91efc96985b0f4",
            "kirocrew-dashboard-author.json": "ed8fa5a35960b358808bd8ba677678ea358a115703c11819f3a37c50aafd2bb2",
            "kirocrew-dashboard-manager.json": "5b54ed295115a122b696bdd05f8741ba3f869d0321d2f36b9e935371bddbc5d1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "06bef672b51f8817bff97bd82c8c71d339a9960d59cdc418cb3a354f8f2dfc02",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "2bc4b434cec3f1a7c128a7a151cb9f7019c28832baa52589779ed3cde853b1b8",
            "kirocrew-mate-worker.json": "cbe8a9a2440cb9af898f1fdd96299d9a2a7567d08b75650ccbe5e0f654d166d5",
            "kirocrew-pipeline-conductor.json": "1afe81bb3e89514ed38cdaf72bfa64ce940ed24cfb2c3ed47aeb396716595cae",
            "kirocrew-research.json": "7db3afde79a24d50e572046bc40531b845cfa17972dc2e0c408dc3be648f769a",
            "kirocrew-security-conductor.json": "fb5eff9bcb189963b44f10f9a5d5222aa3aa4f9d2f2ce1205517a815fdbf46c4",
            "kirocrew-team-lead.json": "e81c7173accb59f35ee827a93b1ee6ac745c1fd2d018b14ebcc999a3218bbe1e",
            "kirocrew-worker.json": "78641fd57030813470728c00c75c8442f18e5e6fa6caf18186cc17d5aa98c6e5",
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
