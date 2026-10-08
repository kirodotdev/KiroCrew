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
        "events": "01735c00e0c643328e919d7c144abc1ebf0423198c2411f6fff1c1b9d490fed4",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "71ccaf814462a34605d3389990d1347f8ae2c446190e026f671e9eb510aa9e5a",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "f440b450ae2521045ba7f3292ce3ee35a00ab68f50d569729a6096372cc14bea",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "15f792950cbe8a2957df0a7977fc199e6ca77b6ef65826ea9512023d2ca77b68",
            "kirocrew-research.json": "95db2f43ca38f7033495b8bd79d989fda7bb6147bc79ee0dc3b954b961fe0f94",
            "kirocrew-security-conductor.json": "8760687087c7f9b8300d24c08c04970e45382358b0557cdf62687c1340ceedd1",
            "kirocrew-worker.json": "a5fa20aa468cd2d4cf61eb00bf1597de577d7bd4a6f5386bf0c96d177c599e37",
            "kirocrew.json": "c17c9b5642e4ebbd5af51bfa87451f04ef49f575bab53cb255c0e47477ef1e34",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "customized": {
        "events": "e56f172582bf1559f232e7ffdff02aaeb09009f1ea03ab445d8950c71fddb40c",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "71ccaf814462a34605d3389990d1347f8ae2c446190e026f671e9eb510aa9e5a",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "f440b450ae2521045ba7f3292ce3ee35a00ab68f50d569729a6096372cc14bea",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "15f792950cbe8a2957df0a7977fc199e6ca77b6ef65826ea9512023d2ca77b68",
            "kirocrew-research.json": "95db2f43ca38f7033495b8bd79d989fda7bb6147bc79ee0dc3b954b961fe0f94",
            "kirocrew-security-conductor.json": "8760687087c7f9b8300d24c08c04970e45382358b0557cdf62687c1340ceedd1",
            "kirocrew-worker.json": "a049cbc95935acdea4fb9fc227fc1e76f841cb1fceeb819a2801beb299a82312",
            "kirocrew.json": "83d2b8b728cf5f1bdfcc3db34eb7af4d359dc4a4133dfdbfe2f9d0c95c4423a8",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "forks": {
        "events": "69ccf91fca126b49eb87a7fff1bbc60fcb77257837ba68b80e430e0e7663513e",
        "files": {
            "kirocrew-dashboard-manager.json": "b0acd94a2340838b47536162b679deb66d8d8ff2876d3352437ca15248b18356",
            "kirocrew-conductor.json": "533ace62ea3b66cad49c54a8cbc8399972cfdf2e6562d1b082400ab1c3b2adf9",
            "kirocrew-dashboard-author.json": "359ad2ebfe08f8a558f0323f5eabf2a734d11ca4ed27c2bcfd7c2a49de328941",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "5c5a14f08217e1c1dc6598b3852588e0f2da073edddbb6cfb9de64fb9c74ec8f",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "df61fdd14e749c5c5170a8a375f09dcd925126cfa832c651a125684c82f6ec2f",
            "kirocrew-research.json": "7f8cdc2b1236723558fcf2a72e285a516cc57295b5a4d333c60f2abff52cd7e9",
            "kirocrew-security-conductor.json": "deb661929eb4ed8421d9b606b4c7d401573fd23b0db57223d348958b7416f43e",
            "kirocrew-worker.json": "f2fed6b5286788f32b643560b116ac90535956576e3388b55316e3f99bfacffb",
            "kirocrew.json": "7fa7831f9144d5706d7dce7d66e84f325830f39c9ee3238bd3a9f6b3903b8cf0",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "a96a919c845078c29eafe40ee2ef5e224bfd4823c0bf7223547fcc4cc7455e8d",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "cbb4cfdf2675dd8efeed9a8b77fc63ce287172f38fe4db9fad37ecb6654ed485",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "0a861cfa593edb1b0684989f72d3776369d42cff9307bc35a9b6b403a0149c33",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "533ace62ea3b66cad49c54a8cbc8399972cfdf2e6562d1b082400ab1c3b2adf9",
            "kirocrew-dashboard-author.json": "359ad2ebfe08f8a558f0323f5eabf2a734d11ca4ed27c2bcfd7c2a49de328941",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "5c5a14f08217e1c1dc6598b3852588e0f2da073edddbb6cfb9de64fb9c74ec8f",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "df61fdd14e749c5c5170a8a375f09dcd925126cfa832c651a125684c82f6ec2f",
            "kirocrew-research.json": "73ebc574c2e06451ecf999e408277952084e0ace9c14dbb05d92279441c9e461",
            "kirocrew-security-conductor.json": "deb661929eb4ed8421d9b606b4c7d401573fd23b0db57223d348958b7416f43e",
            "kirocrew-worker.json": "a13123b79cc22b9f9698ca7a31a5cb1cba3686dccf3f7ed9cabcc89161633612",
            "kirocrew.json": "568c9b620c78294534f549d80e7151e46d0bf74b98861106528910d0420bd4d0",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "governed": {
        "events": "7475ef17e9a3bf2213ec617929886924d72166b630429b260083cbc967e282a8",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "a6b770deb5c3a033f5d80599b8b9264a9685b5f8351a6807cde6452176810ad0",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "22a29646baadf39c5cc9ed8688a0bfa1eb74204f6f2ad8e5542d51bf456dcfd6",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "15f792950cbe8a2957df0a7977fc199e6ca77b6ef65826ea9512023d2ca77b68",
            "kirocrew-research.json": "ad8e9fd513c46576706a7e1e20f457edc7cc12269253f0b1c49e70822ad2c35c",
            "kirocrew-security-conductor.json": "8760687087c7f9b8300d24c08c04970e45382358b0557cdf62687c1340ceedd1",
            "kirocrew-worker.json": "a298768e65798356e2254964a724fe113780fd77e729d8027b4d2edc0133f202",
            "kirocrew.json": "a859e763ba7b11c6ea9547a9a5d5e45d2bd7c7d7fdb8c2d0df69b799ef9a07e5",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "7f89fffedd78c7e8f925b7a24ba93a8151c3a6c28f72a0a3625efc7819873e33",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "8f55f27f5e1d88f878d5fcd79ecff75669a79879c0e2d7ca8e8a0cd475195b37",
            "kirocrew-dashboard-author.json": "816d5e5dd9988c2122f0c2363110deeb3f6384a41400e478ee617e9dfbe4fef3",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "45596d40d4f158b899dd8968f0bf96300cca5163408b6a829a50f4b1313ee099",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "d2b625fae62dd96700adad19b15a877e98e8eb541ec0f4431ad394fa57848408",
            "kirocrew-research.json": "bfa2bd9e571af9040c35d0ff1394d5248e504b8ff6888274f8c3b9071ce03e89",
            "kirocrew-security-conductor.json": "cdd1d8d0a0004552bc63b727a5e08ec403c57c53b66c9627d2578d89d93e526c",
            "kirocrew-worker.json": "1746ef94ad2011c70f6b29dedbfe2ed2c0168bba05dcd906429836919c4285ec",
            "kirocrew.json": "7027b1de70978314a306158f540b39fa0f2cedbc30777a1c0412b02ddea2be02",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "0a861cfa593edb1b0684989f72d3776369d42cff9307bc35a9b6b403a0149c33",
        "files": {
            "kirocrew-dashboard-manager.json": "4b96a77d13a14751d940d8f1d25ab578674db0fdf88e14222bf01b3cc5c86cd8",
            "kirocrew-conductor.json": "120299108165097a2d1e36e0cf8efc184d1b267bcdb4ceec94c8f581ab4d5733",
            "kirocrew-dashboard-author.json": "d44d7f9bbb91f12cd6107609ca7c80b7bf81d6656bf884192e6c97241d39821d",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "6dbd5042238c4b0565f250dd4e235f0f77b01f7d7e6091a127a29ec25e183cc3",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "e5f181195a312bfe532b835880a5a322de9575d3f4545dc19189aa25299054b0",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "eeb3d7c5c5547a0bde29b6a14f1bbd7daba80ef34ad95faebe316c6f60ca14aa",
            "kirocrew-research.json": "76f1490c68081e1b3bc5454f6b6b1a2e7c833b84d19c401652ec03639d5d3d01",
            "kirocrew-security-conductor.json": "8c2df56ffafc3ab3e9c9613b7a37f9ee6ceb34c1ba2f094afe239aa21f4d3272",
            "kirocrew-worker.json": "e921264b10ba14820dbf8b45daebf679093c00a25584cf870101b7124f5d958f",
            "kirocrew.json": "1b6c280a7aa5772c6ed1d5457158f61e6a58fb5c8c290e25fe97dfe87b5c2bd5",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "4331a4594c606ff23230558f9f6a6fc14e0fe3b58842e3365bcd40c4150d5d0f",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "acaf73c1cced640c6f4358a97b72bc572661ec620c1542fab0af5bf17adcc52e",
            "kirocrew-dashboard-author.json": "d867f2989cb8864b75c88ebfb052c6131ebe1ab123a9020329273edb296cd499",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "f9fef940dbad5d53d8e47256e0f39b9c1ed3e4f14ade516d60d51d73fd9a743e",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "ecdb121fc3044ec746ae1ec6dc1ad587c4267999dc8e6f084a72a4d89b3b9016",
            "kirocrew-research.json": "6347bfc693d07431fec38ad367d8fef920f9a1831d1d2bc083de293b2b3f7899",
            "kirocrew-security-conductor.json": "4bdb33a630d30ffff5cdaf9072e1ff54f3bfc69af5715250040dd70f593a0fcb",
            "kirocrew-worker.json": "f52aa3337a1932038ba870d9a8d1234b3c47f407d9f25ebea01bc035c5e78cb5",
            "kirocrew.json": "8c7296c3080cfb10b6429bd7b6b5650b984a0048cc5a35a04480d5af05bd8f3c",
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
