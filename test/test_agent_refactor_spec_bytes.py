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
            "kirocrew-conductor.json": "04023a97a34f6386a8653326dd72758c3ee7a0aa2a38ca214e5c7efcec6aca9d",
            "kirocrew-dashboard-author.json": "313577ac2e64702fb11df901b368a3df4ba099e3573fe218eb48d983d8f925ef",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "e85f9706310eed2aaf7f4a98c516e4a6ce727e4d02e121ec84d1fa5b10920426",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "e7b8464533db3bdfb3f54f722e8cbafe2af0748854c7bbfefc1d1102521bd358",
            "kirocrew-research.json": "25650ee52333f4ee78eca2a6b663ddb5e87ac2bc9e1d711e869ab764eb2716c6",
            "kirocrew-security-conductor.json": "3205fa17ede38afd49e9bc67938adfaf4499fe644ef98fb7070afe2308734c0d",
            "kirocrew-worker.json": "811c9a193cc236ad04202c037bd47264f87090e976bac616d46549cdc7415901",
            "kirocrew.json": "8bb2e352d101a0e7e95174d6ac01d5d2e2122a8b3546dcabf08fb57f5b24fc97",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "customized": {
        "events": "e56f172582bf1559f232e7ffdff02aaeb09009f1ea03ab445d8950c71fddb40c",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "04023a97a34f6386a8653326dd72758c3ee7a0aa2a38ca214e5c7efcec6aca9d",
            "kirocrew-dashboard-author.json": "313577ac2e64702fb11df901b368a3df4ba099e3573fe218eb48d983d8f925ef",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "e85f9706310eed2aaf7f4a98c516e4a6ce727e4d02e121ec84d1fa5b10920426",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "e7b8464533db3bdfb3f54f722e8cbafe2af0748854c7bbfefc1d1102521bd358",
            "kirocrew-research.json": "25650ee52333f4ee78eca2a6b663ddb5e87ac2bc9e1d711e869ab764eb2716c6",
            "kirocrew-security-conductor.json": "3205fa17ede38afd49e9bc67938adfaf4499fe644ef98fb7070afe2308734c0d",
            "kirocrew-worker.json": "45015b2ff59bd33a312bdacdabecac38a019c1e0275f3e41452590db4229589f",
            "kirocrew.json": "8b39cb0462f46a8ce74c545bd254c150feb89869d0904188e6c5020f77cbb014",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "forks": {
        "events": "69ccf91fca126b49eb87a7fff1bbc60fcb77257837ba68b80e430e0e7663513e",
        "files": {
            "kirocrew-dashboard-manager.json": "b0acd94a2340838b47536162b679deb66d8d8ff2876d3352437ca15248b18356",
            "kirocrew-conductor.json": "3c0d448066987ff19f4f2cceff6af6d45ff5b350b062dbeaa3851e9069e60263",
            "kirocrew-dashboard-author.json": "46f881720a14a3957f8fb733a80e4dbe81a8c8281593f7bf43679a0b6fe7b2c4",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "d074bc7e1faff93db69cc372757b149126b861c76a43671c83df6c0e6da4550e",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "83df0d741b96f4e4ac8b37b2108411957af3d78d91737dc838060bdd742f3850",
            "kirocrew-research.json": "258a1977b603eec78a78eeada2e91141a9585a8a6561dbf0ab5168ca19710057",
            "kirocrew-security-conductor.json": "346b2efe192ac8770a3ca5dc0b3c6f271f4bb80d04ca54ca74afaa0a04c651ca",
            "kirocrew-worker.json": "c633474274e3bcd21965f85d7335b2d80fe027b60891305399a295e1ce9c134c",
            "kirocrew.json": "774f5ec22ed8faf1f9ba9b53a706038347d23f34769894930e7ca253d4ce7e47",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "59af507b2f5f5380f08a1f145653fbc138e6507c8f10680bd0ebcebbd710c1c7",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "cbb4cfdf2675dd8efeed9a8b77fc63ce287172f38fe4db9fad37ecb6654ed485",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "0a861cfa593edb1b0684989f72d3776369d42cff9307bc35a9b6b403a0149c33",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "3c0d448066987ff19f4f2cceff6af6d45ff5b350b062dbeaa3851e9069e60263",
            "kirocrew-dashboard-author.json": "46f881720a14a3957f8fb733a80e4dbe81a8c8281593f7bf43679a0b6fe7b2c4",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "d074bc7e1faff93db69cc372757b149126b861c76a43671c83df6c0e6da4550e",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "83df0d741b96f4e4ac8b37b2108411957af3d78d91737dc838060bdd742f3850",
            "kirocrew-research.json": "400c605fe5cd1e7e284877c584fdba0cf48c3e0a7e79e11d982444790e002245",
            "kirocrew-security-conductor.json": "346b2efe192ac8770a3ca5dc0b3c6f271f4bb80d04ca54ca74afaa0a04c651ca",
            "kirocrew-worker.json": "b26591b484d21a561acd85aa3cfa9ebf4f49b894d65852a1a28ac55060f16c86",
            "kirocrew.json": "9d54b6fd9e45b56d9389a139295e0bbff1ef928012febe219fffb47f2e4590ef",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "governed": {
        "events": "7475ef17e9a3bf2213ec617929886924d72166b630429b260083cbc967e282a8",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "fce9d900ddd2c2fb4590fb834ba7eb4a6904ca367acd47e9b5f927ca6cd65446",
            "kirocrew-dashboard-author.json": "313577ac2e64702fb11df901b368a3df4ba099e3573fe218eb48d983d8f925ef",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "28533a6878636bc3664b6629df203023d13174c20c8983fa4e024a5c1d91abb5",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "e7b8464533db3bdfb3f54f722e8cbafe2af0748854c7bbfefc1d1102521bd358",
            "kirocrew-research.json": "25843ca1acb8482cf73308e5e2681d840f1f53a67ef3d46c269804f5d1d168c9",
            "kirocrew-security-conductor.json": "3205fa17ede38afd49e9bc67938adfaf4499fe644ef98fb7070afe2308734c0d",
            "kirocrew-worker.json": "cf5429662ef89a57a1207475363b94344bc54052cc17d1d3f651225e0493f866",
            "kirocrew.json": "a8a9957ab3f950db1199d5e2062b5b9d7fbdd9b00dc5428a94eef40e19fbffcf",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "7f89fffedd78c7e8f925b7a24ba93a8151c3a6c28f72a0a3625efc7819873e33",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "c577952096364d2399a4907fe21b1ea55b43e64f33fd4ae58e21017bd8fc95ad",
            "kirocrew-dashboard-author.json": "7a881dba23001b5eaf8c92980f2259b9453f480dc34ac254f7810688f63b0a7f",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "1553992b1ff59c8e353325b61be58ca91ef44a91f35dfc979fae10f7eda6f8e7",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "824d65e322e65a0b1b1023093e15cef29427bfea9ed59c4e119bd61aa653dabb",
            "kirocrew-research.json": "b43ecfb4f6ba83e0133d3705bc9d2ec1e42c0a170754d10794ec309e012a2fb3",
            "kirocrew-security-conductor.json": "ddcc46497f82991ffea56aab04b0904475d4e5d4c96159ff5ef319f610104ed3",
            "kirocrew-worker.json": "b3ad98e8d33d3d52eecd0d84947a1369fb8f5fa7de2e3d0b336f10de2631164f",
            "kirocrew.json": "4fe7ba5186ccc9fb278482972c0687d392149db28213742bd2b7b78ea5123872",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "0a861cfa593edb1b0684989f72d3776369d42cff9307bc35a9b6b403a0149c33",
        "files": {
            "kirocrew-dashboard-manager.json": "4b96a77d13a14751d940d8f1d25ab578674db0fdf88e14222bf01b3cc5c86cd8",
            "kirocrew-conductor.json": "0f30a67f3f03c78feea8821e8224e60562fc4c05853bdd07449862ca343a516a",
            "kirocrew-dashboard-author.json": "19ead2937032ab1f1539b3054a86733425745f9c6b9f56636322ace92d43cb33",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "6dbd5042238c4b0565f250dd4e235f0f77b01f7d7e6091a127a29ec25e183cc3",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "b9f5c5ce1c2416bac50fa1f347ba30f88f19b76a059cdc65125a7dad6b240008",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "6d887a38bf7a907005ed2d04be5e35a66e3caae982769eef378b640340f489bd",
            "kirocrew-research.json": "bd43e88d698c569182957edbc90de7f5cb0042ea3dbb87671d699f5e1c180dfe",
            "kirocrew-security-conductor.json": "a64325e3361df32f42adde3bf83c81f3d693150486034436594262cee01c9bf9",
            "kirocrew-worker.json": "e3677f5a7709f658087779aa5502f12b9578ec5f2e34e0e8b9c0a1d2732d7aa3",
            "kirocrew.json": "1387cdacc145021c962169dbe75a3d75c9977f5b429ebed1cc74dee7e884ce96",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "4331a4594c606ff23230558f9f6a6fc14e0fe3b58842e3365bcd40c4150d5d0f",
        "files": {
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-conductor.json": "5883c108d45846436d1b6fc5afde79e3ff54cdc8a10e3aa471d25f74c18f1f26",
            "kirocrew-dashboard-author.json": "da5eb2aac0f058b91806958012807e63283072a6ed3d49c3dfaed2e9313da405",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "7b3922996c01a04da9f99b917dc1866a69bdb695fe2f2943ebd8077e1734b0c6",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "b409adbdf10da3eb7160fbeef3ba48ac8c8967d7f607ffe1370778ab36595fd8",
            "kirocrew-research.json": "dc19c13b6ba2001ce1e9f5053ff954d03ac0a4271c09e91276c66c74a59dc645",
            "kirocrew-security-conductor.json": "708bf827d963049e0125f9156394ef51e397cb89bfc96cc81af795a0ebd8d380",
            "kirocrew-worker.json": "8499fec8bcbd29981bc38b3275c26705c73da9996aac172b8bfd803d2d0905a9",
            "kirocrew.json": "5cf6d5bd4b231454df2e3cfaa41c4a370da1a7ebea5071cf59eec3d3e1b37c6f",
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
