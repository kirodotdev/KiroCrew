"""The bytes every agent-spec writer puts on disk, frozen across the materialization split.

``kiro_crew.agent`` delegates to the owners under ``kiro_crew.agent_materialization``,
and that move is only behaviour-preserving if every spec file a rebuild writes comes out
byte-for-byte as it did before, together with the audit records the rebuild emits and
the sidecar bookkeeping it leaves behind. The existing suites pin individual fields; this
module pins the whole output of one real rebuild per scenario, so a field an extraction
dropped, reordered or re-typed is a red here even when no field-level test names it.

Each scenario drives :func:`kiro_crew.agent.rebuild_agent_config` against the SHIPPED
``defaults.json``, prompts and managed-server registry, in a private agents directory,
with only the machine-specific inputs pinned: the ``kirocrew`` launcher path, the
installed kiro-cli version, and the SEL writer (recorded, not written). Everything the
rebuild writes is read back, the scratch paths are replaced by stable placeholders, and
the result is compared against a SHA-256 digest recorded before the split. A mismatch
prints the normalized content that differs, so the drift is readable from the failure.

The goldens carry POSIX paths and exec bits, so these run off Windows; the same writers
run on Windows through the field-level suites.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

#: One path segment under a normalized root, with the separator run before it: a
#: Windows spec spells ``<TMP>\\bin\\kirocrew`` where POSIX spells ``<TMP>/bin/kirocrew``.
_UNDER_ROOT = re.compile(r"(<TMP>|<HOME>)((?:\\+[^\\\"\s]+)+)")
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

        A root is spelled as-is, JSON-escaped once (inside a spec file) or twice
        (inside a JSON value an event records). A path under a root then keeps the
        host's separator, so it is folded to ``/``: the goldens are the same bytes
        on every platform.
        """
        roots = {
            str(self.tmp): "<TMP>",
            str(self.tmp.resolve()): "<TMP>",
            str(self.home): "<HOME>",
            str(self.home.resolve()): "<HOME>",
        }
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
            for p in sorted(self.agents.iterdir())
            if p.is_file() and not p.name.startswith(".")
        }
        state_path = agent_state._state_path()
        state = state_path.read_text(encoding="utf-8") if state_path.is_file() else ""
        if state:
            # The worker's mirror bookkeeping records the default spec's file identity
            # and content fingerprint, both of which carry this run's scratch paths. What
            # is contractual is that they describe the default spec now on disk.
            parsed = json.loads(state)
            for entry in parsed.values():
                if not isinstance(entry, dict):
                    continue
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
        "events": "e4016eba77605642de911e1089127f33fdb4c60579b4fd040df307c601101128",
        "files": {
            "kirocrew-conductor.json": "3ba214346552098fcaf25451f9f48a1543535463862a66b09caca685062fffd8",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "a7fd63440d6dcc7e8046ed4af1b720fc723fb0793ca6caab1b126797b8cfedd1",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "3a973869cf315d3fa89911334045768e4b36bf55926014e6784705768647ed57",
            "kirocrew-research.json": "c5a74b7abbe736443b195551ffbdd19cc6d3cec2ca0d5a82d62899af6b5afaf8",
            "kirocrew-security-conductor.json": "c247c4fc32c9aeb00ce00a85cfd50851c582e5b565b8455592ca0dd6854e71be",
            "kirocrew-worker.json": "552c5d1fad10dc66c2f7865e1bff25319c82290352f881f415cc0282e6903482",
            "kirocrew.json": "2f5c6cca408b8340cf072b8c7293b5369631a07ee7450369aeba906af87eb115",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "2a54e4a13aa343209039218e7484c4b9036874cfa7f45ad311118dc083ee974e",
        "unrefreshed": [],
    },
    "customized": {
        "events": "ff8139f99f8e5e4b35687ef3b53b613e4af6b12f27c340c749ff2f5ea483ecc5",
        "files": {
            "kirocrew-conductor.json": "3ba214346552098fcaf25451f9f48a1543535463862a66b09caca685062fffd8",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "a7fd63440d6dcc7e8046ed4af1b720fc723fb0793ca6caab1b126797b8cfedd1",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "3a973869cf315d3fa89911334045768e4b36bf55926014e6784705768647ed57",
            "kirocrew-research.json": "c5a74b7abbe736443b195551ffbdd19cc6d3cec2ca0d5a82d62899af6b5afaf8",
            "kirocrew-security-conductor.json": "c247c4fc32c9aeb00ce00a85cfd50851c582e5b565b8455592ca0dd6854e71be",
            "kirocrew-worker.json": "68b3a533874092902870cf9976dc2975c79dccd0ab75ccf7384d77ce59654f62",
            "kirocrew.json": "7b436d6e0f68515cb41696fd90ed27e7ed39fd23254f2671ca2efffe331bbbcc",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "a5a64dd4b041ca89821f29b809610947f2f1f4513c285818ebaa998874c6a148",
        "unrefreshed": [],
    },
    "forks": {
        "events": "10a993d77232f33f21f31d7aa0f7ae9297d826438f2355be401270358b26178e",
        "files": {
            "kirocrew-conductor.json": "3f3dc5dec0b059542e5dd16f2fb61589ecfc06af29f15735cb0076d2336ce77a",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "041e25dade490520408333d83967059338649d29f02b96a6f7fb750ef4d10f7f",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "1368d3abba53d9b9b2e1e83982429666f4c26ed2d63fed86608d11b5bdb499ef",
            "kirocrew-research.json": "f4fbaf4f6d045d700d2f62b5343e6e2788fc3523c2aad2942a995cb1e3035839",
            "kirocrew-security-conductor.json": "9dda6fc9125cda938efaac429c930017705ee137b02f6b440b142d679110451a",
            "kirocrew-worker.json": "0a6506970b8f8818bf0feea55c39a044182dd852aa2aecc1dc5d9dca7937db30",
            "kirocrew.json": "c42c0937404c16fb591600aab4f486373deffe9bc73858900251eab7bfec5178",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "a35844964701bef18228b8e89787e9f8f106e17ba079691e7b668877da2e68c9",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "6f420d973fbdf7e48cc5784b36cdc8692abe727062e16c33798348e3e9c6b09d",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "b2435da9299aec1e692da8225eb50db021d586c221eaa8e54b980598c7ae89c9",
        "files": {
            "kirocrew-conductor.json": "3f3dc5dec0b059542e5dd16f2fb61589ecfc06af29f15735cb0076d2336ce77a",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "041e25dade490520408333d83967059338649d29f02b96a6f7fb750ef4d10f7f",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "1368d3abba53d9b9b2e1e83982429666f4c26ed2d63fed86608d11b5bdb499ef",
            "kirocrew-research.json": "36b7c61186ab1ef942aa6b232d1002ae5cbca780ece138a74c413bd2d0c1951e",
            "kirocrew-security-conductor.json": "9dda6fc9125cda938efaac429c930017705ee137b02f6b440b142d679110451a",
            "kirocrew-worker.json": "5dd0a0ee22572cddc24ccc86e0a45988c9bf85cdf6e8144b80d5e8f120c4b95c",
            "kirocrew.json": "cb2abd969c70c183db542bec3498c8ef73249533712eef441adcd4ec4d0780ef",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "2a54e4a13aa343209039218e7484c4b9036874cfa7f45ad311118dc083ee974e",
        "unrefreshed": [],
    },
    "governed": {
        "events": "1d7b1bc070305b97dd0158857e18eeaa38fd69f4c397e13e99a55f539f2bff9c",
        "files": {
            "kirocrew-conductor.json": "3e9718bcb459bfe419914671489f4d473f24bbff093ae1203fa362ac07a183f1",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "8a0249b0037ec70835b22f76bee77031376df37cff5d86f82ffa2f07bc5148ad",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "3a973869cf315d3fa89911334045768e4b36bf55926014e6784705768647ed57",
            "kirocrew-research.json": "04e6211a19948ea2b471b4e8fad21f936decfa0dc065f15611f633cd0efe5bfd",
            "kirocrew-security-conductor.json": "c247c4fc32c9aeb00ce00a85cfd50851c582e5b565b8455592ca0dd6854e71be",
            "kirocrew-worker.json": "54fe372f7afa09dda4270b826be563553fc29f8ff0c24697ab9e62bd65662e99",
            "kirocrew.json": "795bb986be7c72eb53fb3546f29342ec7746975b0bc09931e2de13a8e3ecee54",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "a5a64dd4b041ca89821f29b809610947f2f1f4513c285818ebaa998874c6a148",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "bdf18bf5093e7d69be03fba107e46b553e77a706c421f1471ecbd6f9f475c956",
        "files": {
            "kirocrew-conductor.json": "4034861d5e7d270a9bae8755d51f8381de116781efebb4d722c58204627bc653",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "533b9ff52ee11f6b3ba5511fcd08cd0879a988dd021a65a8a17cdf8a768e5e74",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "7a3226b71b2d0d092b0f94dbee2748f26be99798b563e9384a1c1f85e36c65fd",
            "kirocrew-research.json": "c996f84110bf8b30295e0b6ec8448ea77518be87f3b770786c860a5532d28405",
            "kirocrew-security-conductor.json": "d20b2d67912f47e56d44db6e662a0c4faad4dce264a84901dc0efe4ed08ad310",
            "kirocrew-worker.json": "a09f164d00abde39af4cb4629a3840df8ad831b83072728efbb9e22d8644ac53",
            "kirocrew.json": "16f62d761811acf8e9eae5d8f226e98e997677bf638d8d4b533b739e73ccfdbf",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "2a54e4a13aa343209039218e7484c4b9036874cfa7f45ad311118dc083ee974e",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "b2435da9299aec1e692da8225eb50db021d586c221eaa8e54b980598c7ae89c9",
        "files": {
            "kirocrew-conductor.json": "f1d21a8b5b1bed1363ad7e6d032d40d72b5f5aa34f975024dd98da9dfcce6fd6",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "6dbd5042238c4b0565f250dd4e235f0f77b01f7d7e6091a127a29ec25e183cc3",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "dd372482edbb7996e9c967aa3a3a593e17560145ef679cad3d4caad5557e5427",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "55010f4df2c6bd781b727855d10f581cee191102773e45477d9b6b47c60f1cd2",
            "kirocrew-research.json": "c449010f3af4f8819892b8e105cdd2fa6a926d0b8c3c6f867e3930179f784a05",
            "kirocrew-security-conductor.json": "d5773445fc13865adf76665a607fec8095e1935a55e4aa04bb85cf58e6b59a03",
            "kirocrew-worker.json": "381da64c786fe87511ae9bfd68a834719a669e839e71eb692f3a534f33ec46ee",
            "kirocrew.json": "1bc59bfa09d6d751b5f5a34d96ed8454963baa4f655e42e34dd6cf7ad01fd972",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "2a54e4a13aa343209039218e7484c4b9036874cfa7f45ad311118dc083ee974e",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "16eedef4f21d31a0c93ab48f5c6a1ad3757c21474e2e282084c791e565a2f871",
        "files": {
            "kirocrew-conductor.json": "ec85e841f56848c1d880a397b47194d7e5e7a4883e7077b7eb78f9aa95666bf8",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "7afa20f65bbff5391693c1c6d27cca8eb5e5da59828a7f254b6db7a4906f8ec4",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "6e75b00721616952638d1440fc4a66dcf9aed03c5845abb321cef3dc940fa953",
            "kirocrew-research.json": "53fbcc83ef297d9cb560ec43b7a8b140c35a0ab814819c980ff19cd16b6af2d6",
            "kirocrew-security-conductor.json": "f716dbec0e45e3165d4c476ed8b11fe80b3c2cf22807a9eb578217ecc9215866",
            "kirocrew-worker.json": "cb7b1500752aa6f921959f389804a3d5091bd00cc737b3c3599bf22d2a90a3e8",
            "kirocrew.json": "7e71a35e225f0f41250b6ca73026183c53fac8f02f992e54384d81981b2d8d74",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "2a54e4a13aa343209039218e7484c4b9036874cfa7f45ad311118dc083ee974e",
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
    for name, digest in expected["files"].items():
        assert (
            digests["files"][name] == digest
        ), f"{scenario}: {name} no longer matches the pre-split bytes:\n{got.files[name]}"
    assert (
        digests["events"] == expected["events"]
    ), f"{scenario}: the audit record sequence changed:\n" + json.dumps(
        got.events, indent=1, sort_keys=True
    )
    assert (
        digests["state"] == expected["state"]
    ), f"{scenario}: the agent-state sidecar changed:\n{got.state}"
    assert digests["unrefreshed"] == expected["unrefreshed"], "the fork refresh verdict changed"
