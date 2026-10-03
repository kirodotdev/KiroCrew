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
                # ``managed_owned`` is the sha256 of the dashboard-author spec bytes the
                # installer wrote; the contract is that a digest IS recorded, not its exact
                # value (which tracks the spec content), so normalise it to a placeholder.
                if isinstance(entry.get("managed_owned"), str) and entry["managed_owned"]:
                    entry["managed_owned"] = "<DASHBOARD-AUTHOR-DIGEST>"
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
        "events": "62f61aff6c58b82a699a8b33901db0f4a9a791c931e292cc8e24c6cf0c93801b",
        "files": {
            "kirocrew-conductor.json": "6745a4b08dc9735c8818963b7f441675e2556157fd4945e9f2f0559a2f8a7bca",
            "kirocrew-dashboard-author.json": "313577ac2e64702fb11df901b368a3df4ba099e3573fe218eb48d983d8f925ef",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "dde61d9074d56d1916f251eb09660c5854ac5817a1dc66f5de248453654ef2f2",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "e7b8464533db3bdfb3f54f722e8cbafe2af0748854c7bbfefc1d1102521bd358",
            "kirocrew-research.json": "c781dcdfec5610e95921c72f86a6fd8e82e93a7e9bfcc67011e126d579445a02",
            "kirocrew-security-conductor.json": "1ea9124912104d522fe660bf3e0266f8d02b0a6aa330774cc7f05c6a4931aa26",
            "kirocrew-worker.json": "043dc504892b0301d0696511ef18cfb4cea0b99f8955a2256387e80591751858",
            "kirocrew.json": "8bb2e352d101a0e7e95174d6ac01d5d2e2122a8b3546dcabf08fb57f5b24fc97",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "59fd1af5c38fd1264999e35e875368199a8c77e4f8030f1302cfde6f8b0b203b",
        "unrefreshed": [],
    },
    "customized": {
        "events": "547297b4e0b51712afdb1354d4eb58d0940f88af12a63a8ef91f6df50c67be2d",
        "files": {
            "kirocrew-conductor.json": "6745a4b08dc9735c8818963b7f441675e2556157fd4945e9f2f0559a2f8a7bca",
            "kirocrew-dashboard-author.json": "313577ac2e64702fb11df901b368a3df4ba099e3573fe218eb48d983d8f925ef",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "dde61d9074d56d1916f251eb09660c5854ac5817a1dc66f5de248453654ef2f2",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "e7b8464533db3bdfb3f54f722e8cbafe2af0748854c7bbfefc1d1102521bd358",
            "kirocrew-research.json": "c781dcdfec5610e95921c72f86a6fd8e82e93a7e9bfcc67011e126d579445a02",
            "kirocrew-security-conductor.json": "1ea9124912104d522fe660bf3e0266f8d02b0a6aa330774cc7f05c6a4931aa26",
            "kirocrew-worker.json": "23a91f320df166993f86cbcd1e142b5e1e46000e6a252c1e797bfe2e70246da4",
            "kirocrew.json": "8b39cb0462f46a8ce74c545bd254c150feb89869d0904188e6c5020f77cbb014",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "547b05b4c308ed2e6eacee2e5a868853febe4341a1f8b5ff5e2fd2d0868a62a6",
        "unrefreshed": [],
    },
    "forks": {
        "events": "69ccf91fca126b49eb87a7fff1bbc60fcb77257837ba68b80e430e0e7663513e",
        "files": {
            "kirocrew-conductor.json": "96f7570b3a408d11b8692c5aae35ab36fdca58250821de673e694c025fe61753",
            "kirocrew-dashboard-author.json": "46f881720a14a3957f8fb733a80e4dbe81a8c8281593f7bf43679a0b6fe7b2c4",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "1238c4d7d65e84dec8feb3d8c601b63c1260b2552e7d813aee321d58bb27f4d7",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "83df0d741b96f4e4ac8b37b2108411957af3d78d91737dc838060bdd742f3850",
            "kirocrew-research.json": "71650a51e436a8bea81e1afc66421d70d98ba7072c97f4f3447961578da84dd5",
            "kirocrew-security-conductor.json": "8e9d2171e5e435a8ec71b5f6f8912c573746e3ec5a42fa2a5e71a352fb9eeb0d",
            "kirocrew-worker.json": "57eb64a1501532281ef5bbac2600b276e767bac48e17b82e7d2c995c1506d363",
            "kirocrew.json": "774f5ec22ed8faf1f9ba9b53a706038347d23f34769894930e7ca253d4ce7e47",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "59af507b2f5f5380f08a1f145653fbc138e6507c8f10680bd0ebcebbd710c1c7",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "4126ec7ddb02ed036c46ca005bab4e5a5ddcd2ec33c1187f47244192df312512",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "bcf9417c83dc328a51c91ebe0b54a921d063237992a5f02a7eca59b76daca23f",
        "files": {
            "kirocrew-conductor.json": "96f7570b3a408d11b8692c5aae35ab36fdca58250821de673e694c025fe61753",
            "kirocrew-dashboard-author.json": "46f881720a14a3957f8fb733a80e4dbe81a8c8281593f7bf43679a0b6fe7b2c4",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "1238c4d7d65e84dec8feb3d8c601b63c1260b2552e7d813aee321d58bb27f4d7",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "83df0d741b96f4e4ac8b37b2108411957af3d78d91737dc838060bdd742f3850",
            "kirocrew-research.json": "8c4618a99d16f0341bdd2a1f6fe425439ad0c264307b79c1f3c771ac8dba392a",
            "kirocrew-security-conductor.json": "8e9d2171e5e435a8ec71b5f6f8912c573746e3ec5a42fa2a5e71a352fb9eeb0d",
            "kirocrew-worker.json": "f5e4c74fd95a92256bf1f87115b1da7dc9518a8e06e05663573167d4656832f6",
            "kirocrew.json": "9d54b6fd9e45b56d9389a139295e0bbff1ef928012febe219fffb47f2e4590ef",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "59fd1af5c38fd1264999e35e875368199a8c77e4f8030f1302cfde6f8b0b203b",
        "unrefreshed": [],
    },
    "governed": {
        "events": "ff3a093d9e789388ead9e9414e51558e8521d8a8c7be00395baf2b086c21dbec",
        "files": {
            "kirocrew-conductor.json": "36c5fdd13765c6c8519e7115c3f653e963a5c1f1b3cc9f1c6eeb1a1cd71bb65f",
            "kirocrew-dashboard-author.json": "313577ac2e64702fb11df901b368a3df4ba099e3573fe218eb48d983d8f925ef",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "c56ee41d5b73f5376161b3ed89df29c72c4b6400fa503d73319d4b97eac3d282",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "e7b8464533db3bdfb3f54f722e8cbafe2af0748854c7bbfefc1d1102521bd358",
            "kirocrew-research.json": "868abadef31cc73281591c81d863818483bd3c010f5e53595980df0741e7a2f7",
            "kirocrew-security-conductor.json": "1ea9124912104d522fe660bf3e0266f8d02b0a6aa330774cc7f05c6a4931aa26",
            "kirocrew-worker.json": "9dc05cd49a1d818e9cb098055416bd2590730ab7ffd4b320be9f7f429211136d",
            "kirocrew.json": "a8a9957ab3f950db1199d5e2062b5b9d7fbdd9b00dc5428a94eef40e19fbffcf",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "547b05b4c308ed2e6eacee2e5a868853febe4341a1f8b5ff5e2fd2d0868a62a6",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "a77550405fb09cd20d937768fcb51fbd0870739dfdb7a2884403bb5faf848b3d",
        "files": {
            "kirocrew-conductor.json": "2833507c5dc6164428a1e1ad9fce5777b57ac763103266fea0779f90c4eacea3",
            "kirocrew-dashboard-author.json": "7a881dba23001b5eaf8c92980f2259b9453f480dc34ac254f7810688f63b0a7f",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "0c17465a070baa2ad7c45fee6444a0f1f6bc020c931eb9f95bec4a0c6a79ce7e",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "824d65e322e65a0b1b1023093e15cef29427bfea9ed59c4e119bd61aa653dabb",
            "kirocrew-research.json": "d03d68acf6c38bede3cce369ea89bfc6d4fdd8c25017a0128cd4cbb659e169b5",
            "kirocrew-security-conductor.json": "40d0ef38833abfac27cd44698f65383f906073a75faf625f65258116342ea69a",
            "kirocrew-worker.json": "eb144e631608372a18a8026de94c40c058da3bac64a806a14915cb8c76d149bb",
            "kirocrew.json": "4fe7ba5186ccc9fb278482972c0687d392149db28213742bd2b7b78ea5123872",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "59fd1af5c38fd1264999e35e875368199a8c77e4f8030f1302cfde6f8b0b203b",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "bcf9417c83dc328a51c91ebe0b54a921d063237992a5f02a7eca59b76daca23f",
        "files": {
            "kirocrew-conductor.json": "3ec6f4930e439c9ef36901e87d86293fcb10ee6e9accf119a0a5abcd117867f9",
            "kirocrew-dashboard-author.json": "19ead2937032ab1f1539b3054a86733425745f9c6b9f56636322ace92d43cb33",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "6dbd5042238c4b0565f250dd4e235f0f77b01f7d7e6091a127a29ec25e183cc3",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "11d80be0320a3d98fb88335a2795b34206461f7b55c1c05b93abc16ce31ef421",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "6d887a38bf7a907005ed2d04be5e35a66e3caae982769eef378b640340f489bd",
            "kirocrew-research.json": "76f3e18d27af2265cfd299f0130641f6e8afbaf2eb548371a297d25ceb7d7a35",
            "kirocrew-security-conductor.json": "d7a4c9de198ee7552052d34dbeebbf96afda1bc57c0b46162728a4d520243f94",
            "kirocrew-worker.json": "08f43572fba6222a2c38794c961ef4de7c7ab7c3f591731399ecbf3a5ab1f414",
            "kirocrew.json": "1387cdacc145021c962169dbe75a3d75c9977f5b429ebed1cc74dee7e884ce96",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "59fd1af5c38fd1264999e35e875368199a8c77e4f8030f1302cfde6f8b0b203b",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "b56e6fdf497d40905fe7469ef137410d641792419b517e7c7661f9be3bc4d7a1",
        "files": {
            "kirocrew-conductor.json": "8aaec096e2c4321549ec3ecdc2557e03cb30a23a0ca2842053f16ad6a1d0c522",
            "kirocrew-dashboard-author.json": "da5eb2aac0f058b91806958012807e63283072a6ed3d49c3dfaed2e9313da405",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "421520f515e463fe12edd167b1a12ebc9caf29c413ee10759fab9a99a6ee2607",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "b409adbdf10da3eb7160fbeef3ba48ac8c8967d7f607ffe1370778ab36595fd8",
            "kirocrew-research.json": "21034594ecb069270e769451c739c90b9907a6ed67bb938f6efb9a5bd802d7ff",
            "kirocrew-security-conductor.json": "5e694ff2218510a2bb4e17278d7a8b0a9a3503c08f5e94c400f7e32b4008379a",
            "kirocrew-worker.json": "fdf6a48a8eaaef6817af751e0ec6c78b18f6156f84fabc4c77d750c9d7c3a958",
            "kirocrew.json": "5cf6d5bd4b231454df2e3cfaa41c4a370da1a7ebea5071cf59eec3d3e1b37c6f",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "59fd1af5c38fd1264999e35e875368199a8c77e4f8030f1302cfde6f8b0b203b",
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
