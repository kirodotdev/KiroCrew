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
            "kirocrew-conductor.json": "6c3eb74f2be73dc62530be47600d77fec73ffae327aa9f044390d1c5c1d22abd",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "19c9d41fffd6a1633e312efea994e155da1183f059b98ecf8668cb3fe2967cad",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "9aa87b292612bc6ab53a62a5b0885ff56857eb1c3e3b7a34ae1a7c57942d4287",
            "kirocrew-research.json": "95db2f43ca38f7033495b8bd79d989fda7bb6147bc79ee0dc3b954b961fe0f94",
            "kirocrew-security-conductor.json": "c34378da19d5b5bddd8509347237821b4c71260db425c2a5397af913b5b7033c",
            "kirocrew-worker.json": "c2ea62542bc5fa5f861cd280893bcc125cf30a4e2a619c9d319b5dc69e855e76",
            "kirocrew.json": "57eb02f38a59adae838ce3c1a71a6fd899a7a1961b5b4c43676ffcdefbc78ef2",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "customized": {
        "events": "ccb4dcb92db9fdca56b77127e98ba2b0218bcf8d4d93c79e529851c5178d454a",
        "files": {
            "kirocrew-conductor.json": "6c3eb74f2be73dc62530be47600d77fec73ffae327aa9f044390d1c5c1d22abd",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "19c9d41fffd6a1633e312efea994e155da1183f059b98ecf8668cb3fe2967cad",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "9aa87b292612bc6ab53a62a5b0885ff56857eb1c3e3b7a34ae1a7c57942d4287",
            "kirocrew-research.json": "95db2f43ca38f7033495b8bd79d989fda7bb6147bc79ee0dc3b954b961fe0f94",
            "kirocrew-security-conductor.json": "c34378da19d5b5bddd8509347237821b4c71260db425c2a5397af913b5b7033c",
            "kirocrew-worker.json": "0a7048f96f22eeb8a52de6512ea414947e3baebeaa41aa070fe534579c553860",
            "kirocrew.json": "93687935b0eb4553f37adc8430b0f1b033705b70c7083a00ebe48e57ee22a10f",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "forks": {
        "events": "69ccf91fca126b49eb87a7fff1bbc60fcb77257837ba68b80e430e0e7663513e",
        "files": {
            "kirocrew-conductor.json": "c2a0607613976f235e6a4754bb2bc6a735a7d57772b5669d6a42faa0eac277dc",
            "kirocrew-dashboard-author.json": "359ad2ebfe08f8a558f0323f5eabf2a734d11ca4ed27c2bcfd7c2a49de328941",
            "kirocrew-dashboard-manager.json": "b0acd94a2340838b47536162b679deb66d8d8ff2876d3352437ca15248b18356",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "57291287becd9330e63c67107c5af12d37e4b102ea01af80bc2506641a90a186",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "259a08a152e0ead5017ec9e9ab3fee2c862fde14c64a45b13f2f4119562ded86",
            "kirocrew-research.json": "7f8cdc2b1236723558fcf2a72e285a516cc57295b5a4d333c60f2abff52cd7e9",
            "kirocrew-security-conductor.json": "bf89752990cd1655f9b4ec5487d7c98c131e5667ef79a388217f32f370a0ab51",
            "kirocrew-worker.json": "ec67cd1e44465ad0768ba140895085dbf4cd7e85d7e8b952a0a4b2e8aa4b16e4",
            "kirocrew.json": "047c17cebfa9f25e47bb586070994e322ccb5ddadbec4d8c05a9faddb1df65a9",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "415fa49bfa7d99e56c2008b03c827521c966f764d08e00851373879eb20a8a64",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "cbb4cfdf2675dd8efeed9a8b77fc63ce287172f38fe4db9fad37ecb6654ed485",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "0a861cfa593edb1b0684989f72d3776369d42cff9307bc35a9b6b403a0149c33",
        "files": {
            "kirocrew-conductor.json": "c2a0607613976f235e6a4754bb2bc6a735a7d57772b5669d6a42faa0eac277dc",
            "kirocrew-dashboard-author.json": "359ad2ebfe08f8a558f0323f5eabf2a734d11ca4ed27c2bcfd7c2a49de328941",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "57291287becd9330e63c67107c5af12d37e4b102ea01af80bc2506641a90a186",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "259a08a152e0ead5017ec9e9ab3fee2c862fde14c64a45b13f2f4119562ded86",
            "kirocrew-research.json": "73ebc574c2e06451ecf999e408277952084e0ace9c14dbb05d92279441c9e461",
            "kirocrew-security-conductor.json": "bf89752990cd1655f9b4ec5487d7c98c131e5667ef79a388217f32f370a0ab51",
            "kirocrew-worker.json": "669b576905b5296bfdecb7a59322cd4c0d9c71b228f907124f2a9d5adf7a4122",
            "kirocrew.json": "19b897550374bd00cade8940f9abdcf3fa79992f7c6b5649cbe9979441bdfca0",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "governed": {
        "events": "f18e3508ac26841a9b85f2a9fcbabf7eba23c9d135231f80ba3601ea21addd9c",
        "files": {
            "kirocrew-conductor.json": "d4a3f1ad69ebdcf1f8896c0d1bc2e6ffe92b338d4f191da76824cc64e3d4947d",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "ed0c30aa37762f875a313f93ab432cbca6b16c9680c7e121a6842d6b6a62b99d",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "9aa87b292612bc6ab53a62a5b0885ff56857eb1c3e3b7a34ae1a7c57942d4287",
            "kirocrew-research.json": "ad8e9fd513c46576706a7e1e20f457edc7cc12269253f0b1c49e70822ad2c35c",
            "kirocrew-security-conductor.json": "c34378da19d5b5bddd8509347237821b4c71260db425c2a5397af913b5b7033c",
            "kirocrew-worker.json": "1db24e8ae4feeb80786feca414350f23c864c62521b10c7179125c9fe4f72a26",
            "kirocrew.json": "9e84f7fdcb9901bee42b842caeb67bc89211a47e49d27713cabe3ebbe26d043b",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "7f89fffedd78c7e8f925b7a24ba93a8151c3a6c28f72a0a3625efc7819873e33",
        "files": {
            "kirocrew-conductor.json": "ff157070bce2d11c2bf46dbb10258bc14a1ee0afee07a7c0cfe13c4ed5213703",
            "kirocrew-dashboard-author.json": "816d5e5dd9988c2122f0c2363110deeb3f6384a41400e478ee617e9dfbe4fef3",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "06e753d4b89f68f0c966046f3ce13c67d19c5dd1c54000850de308a95a3588fb",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "8f993db1e97034c09237e656f2469c97bcf5dfb86faa94d039f385b9dc4f0ab8",
            "kirocrew-research.json": "bfa2bd9e571af9040c35d0ff1394d5248e504b8ff6888274f8c3b9071ce03e89",
            "kirocrew-security-conductor.json": "45567a2b9cc93fd50bc75bca810eaefc8088802ce3095b852615f5dd17d448d2",
            "kirocrew-worker.json": "e5930ab1b19a811d7044ab41c1cb1e0073d2143ebc993a4ddfff2712d1b57641",
            "kirocrew.json": "080669fe63f7bac92688c1fad714a43aaa8dd6e653bcc89fc4ce64c0636bd3db",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "0a861cfa593edb1b0684989f72d3776369d42cff9307bc35a9b6b403a0149c33",
        "files": {
            "kirocrew-conductor.json": "bed9802f359bf7e7b4bbc6b02d98be081029da1d1e31a73e5c084cdf1bba3662",
            "kirocrew-dashboard-author.json": "d44d7f9bbb91f12cd6107609ca7c80b7bf81d6656bf884192e6c97241d39821d",
            "kirocrew-dashboard-manager.json": "4b96a77d13a14751d940d8f1d25ab578674db0fdf88e14222bf01b3cc5c86cd8",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "6dbd5042238c4b0565f250dd4e235f0f77b01f7d7e6091a127a29ec25e183cc3",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "7e933941f087c639f2b4e650857d5d2a815edb3da4474a4ae122873ecf883481",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "15b7e86caacf64e851bf4d00b97df22f52d47dfc2fe5ea3718695cca763b805d",
            "kirocrew-research.json": "76f1490c68081e1b3bc5454f6b6b1a2e7c833b84d19c401652ec03639d5d3d01",
            "kirocrew-security-conductor.json": "28a7df9a998ded3541af9f2f3f7ebd265cba8485be5a2c32d44d213012b82e5a",
            "kirocrew-worker.json": "cbe77244dfd295a1383d57449c66ec21016aaa2515017d933e11c5610410ad56",
            "kirocrew.json": "2b7f05bb5fc71105cd498cefdf0121e916046ed25691f33cacd66d3f97d5472d",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "4331a4594c606ff23230558f9f6a6fc14e0fe3b58842e3365bcd40c4150d5d0f",
        "files": {
            "kirocrew-conductor.json": "4013281fd6b269f71f2027c75e3aaf2a18394083b3a54409903d331267d30468",
            "kirocrew-dashboard-author.json": "d867f2989cb8864b75c88ebfb052c6131ebe1ab123a9020329273edb296cd499",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "41b60d1287ae97af679d5e73931b2be32aedf58f522ef9b9e748c71b12cf5168",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "d14e919951362329ef1d5960a402b30037ee5d95dfe5db604da5a4c047b21fbc",
            "kirocrew-research.json": "6347bfc693d07431fec38ad367d8fef920f9a1831d1d2bc083de293b2b3f7899",
            "kirocrew-security-conductor.json": "fb87477b31545ea61cdad0ec04e3d5de4924e5abbce3609b53e2f77cb79c40d2",
            "kirocrew-worker.json": "f9774e7a257465d0f8a338276fe25abb65567076901317d4cbc01afe0ebf5665",
            "kirocrew.json": "d0d2ece9be5ccc18371147af7aec39a253ad7a9db3377f929686f00610ec695f",
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
