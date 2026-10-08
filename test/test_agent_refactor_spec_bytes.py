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
        on every platform. ``<PKG>`` is the installed ``kiro_crew`` package, which
        the assistant prompt names as the packaged docs index: it is wherever this
        checkout lives, so it is labelled like the scratch roots.
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
            # The team-lead stem records the same kind of ownership digest, over bytes that
            # carry the same scratch paths, so it is folded on the same contract: the value
            # is stable only as "the digest of the team-lead spec now on disk".
            tl_file = self.agents / "kirocrew-team-lead.json"
            tl_digest = None
            if tl_file.is_file():
                tl_digest = agent_state.spec_digest(json.loads(tl_file.read_text(encoding="utf-8")))
            for entry in parsed.values():
                if not isinstance(entry, dict):
                    continue
                if da_digest is not None and entry.get("managed_digest") == da_digest:
                    entry["managed_digest"] = "<DASHBOARD-AUTHOR-DIGEST>"
                if tl_digest is not None and entry.get("managed_digest") == tl_digest:
                    entry["managed_digest"] = "<TEAM-LEAD-DIGEST>"
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
        "events": "9f7cfe04980041ebd795bfd2b6f0c55886be4c04a01d004c3cf142f9be466756",
        "files": {
            "kirocrew-team-lead.json": "5d63c897aad23922ed51ff406f802c02467875c51dbf989211f8d48d40b8e100",
            "kirocrew-conductor.json": "8238f5cdc720a203f57a2da02f77c048842e9b20292f929fcf5e8615fedab510",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "5a4a66b8535ab8f00186ccacf4e4a15fa3c86e36160787720d7f1ba6574a6acc",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "9aa87b292612bc6ab53a62a5b0885ff56857eb1c3e3b7a34ae1a7c57942d4287",
            "kirocrew-research.json": "95db2f43ca38f7033495b8bd79d989fda7bb6147bc79ee0dc3b954b961fe0f94",
            "kirocrew-security-conductor.json": "c34378da19d5b5bddd8509347237821b4c71260db425c2a5397af913b5b7033c",
            "kirocrew-worker.json": "c1cc54cbbca9b171bbaaab82cef51e889f7be834dca5849630fc9bc6fac7b22f",
            "kirocrew.json": "6a600a7aa7c715414f6cb4ac4ff8b07d8d669f9f7c6253e1fbd17f89db3d6391",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "3f05b0e215ac71c35d1915d5bdb3b7461eb29c3bbef4137912eb09e65af51aa3",
        "unrefreshed": [],
    },
    "customized": {
        "events": "5c3307cf47ad0a891ee74ae82bd6061683a5daca3f416f09e93339ce96220668",
        "files": {
            "kirocrew-team-lead.json": "5d63c897aad23922ed51ff406f802c02467875c51dbf989211f8d48d40b8e100",
            "kirocrew-conductor.json": "8238f5cdc720a203f57a2da02f77c048842e9b20292f929fcf5e8615fedab510",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "5a4a66b8535ab8f00186ccacf4e4a15fa3c86e36160787720d7f1ba6574a6acc",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "9aa87b292612bc6ab53a62a5b0885ff56857eb1c3e3b7a34ae1a7c57942d4287",
            "kirocrew-research.json": "95db2f43ca38f7033495b8bd79d989fda7bb6147bc79ee0dc3b954b961fe0f94",
            "kirocrew-security-conductor.json": "c34378da19d5b5bddd8509347237821b4c71260db425c2a5397af913b5b7033c",
            "kirocrew-worker.json": "ceebb48bbabdabaf6864ca19cc60685b253e349cfe268d797c734daf37166c8b",
            "kirocrew.json": "35177a42e7e52b62e39a56371d5ad562c02e8381f331229a9bccfcc62ab8d22b",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "465c9a79d760e863fe8ca1ff75b6881daf31325dc874c00c1c5689ff32eb11ea",
        "unrefreshed": [],
    },
    "forks": {
        "events": "2478c8f186f0bed520463ba9dff2ab170ce78357e6fb6c352e904be5bd567540",
        "files": {
            "kirocrew-team-lead.json": "34dd3f223b889c3b653ec7188ec15404397c38066f87deb9a2b8f06c18952051",
            "kirocrew-conductor.json": "e24b43a5a754dac6db3f3a09c1df61373039985002d279b001348c23847447f9",
            "kirocrew-dashboard-author.json": "359ad2ebfe08f8a558f0323f5eabf2a734d11ca4ed27c2bcfd7c2a49de328941",
            "kirocrew-dashboard-manager.json": "b0acd94a2340838b47536162b679deb66d8d8ff2876d3352437ca15248b18356",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "bfd2735a4e12db74917ae0c17e1d029ca0555f04de71cffba8567f801914bc91",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "259a08a152e0ead5017ec9e9ab3fee2c862fde14c64a45b13f2f4119562ded86",
            "kirocrew-research.json": "7f8cdc2b1236723558fcf2a72e285a516cc57295b5a4d333c60f2abff52cd7e9",
            "kirocrew-security-conductor.json": "bf89752990cd1655f9b4ec5487d7c98c131e5667ef79a388217f32f370a0ab51",
            "kirocrew-worker.json": "8626eaec4dac0a2fc73ed13e0365f718ba835c54b64ce4492dfe9d1527ccfd19",
            "kirocrew.json": "a748ffbecb747e0f2c2b7b762c9a498126f2bf33d391f59070573bf7c38096cf",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "415fa49bfa7d99e56c2008b03c827521c966f764d08e00851373879eb20a8a64",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "c017327cfe021d80142f5e4e1503098b7b144561d77a7779f5bc67d486942a0d",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "4c10427a400719419b66eaf8cad743e8539d6d7c63fa96544bf2973175c69d80",
        "files": {
            "kirocrew-team-lead.json": "5fde71553f627323203653f8d50c697dfc5e59a663adbae851e654b5606170a5",
            "kirocrew-conductor.json": "e24b43a5a754dac6db3f3a09c1df61373039985002d279b001348c23847447f9",
            "kirocrew-dashboard-author.json": "359ad2ebfe08f8a558f0323f5eabf2a734d11ca4ed27c2bcfd7c2a49de328941",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "bfd2735a4e12db74917ae0c17e1d029ca0555f04de71cffba8567f801914bc91",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "259a08a152e0ead5017ec9e9ab3fee2c862fde14c64a45b13f2f4119562ded86",
            "kirocrew-research.json": "73ebc574c2e06451ecf999e408277952084e0ace9c14dbb05d92279441c9e461",
            "kirocrew-security-conductor.json": "bf89752990cd1655f9b4ec5487d7c98c131e5667ef79a388217f32f370a0ab51",
            "kirocrew-worker.json": "676c1c10f58bd45ccf8eb48b300cff8a3f1b277d5bc7c3418fd7c8f149becf21",
            "kirocrew.json": "87dffbb35bbbcc387f098bdafd6e8797227e19f9d755d747e50afd7a67624a72",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "3f05b0e215ac71c35d1915d5bdb3b7461eb29c3bbef4137912eb09e65af51aa3",
        "unrefreshed": [],
    },
    "governed": {
        "events": "f8c6d6df415cfd79ba4988e0dc9e747a69d943d73e03dfc79d805a3f217d5206",
        "files": {
            "kirocrew-team-lead.json": "8e0db7605494ef9746c81b75fad3aa23848c223805a4ea767e84f962851ac8f2",
            "kirocrew-conductor.json": "a1f2a57a1a54e23d4d78a18acb34996af4901fbf53210db6549c08759e016151",
            "kirocrew-dashboard-author.json": "cbe7acbded8bd0255c466d21fd8e990014452f9b3cd4afb9b74d5ee6354b3366",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "8c5c567c4927f214c9ea50503ec86b6e95276c971bd1203ed182c99c8adfa7fc",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "9aa87b292612bc6ab53a62a5b0885ff56857eb1c3e3b7a34ae1a7c57942d4287",
            "kirocrew-research.json": "ad8e9fd513c46576706a7e1e20f457edc7cc12269253f0b1c49e70822ad2c35c",
            "kirocrew-security-conductor.json": "c34378da19d5b5bddd8509347237821b4c71260db425c2a5397af913b5b7033c",
            "kirocrew-worker.json": "828870bf4f67b0c40c06315078f8072c8a7e3a82a5faff52eb8abd283d28d2a7",
            "kirocrew.json": "9ed1357ec13c936a3e94a4281a7d679e5dbfb9c41515cb19eb2e1f40b2093e72",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "465c9a79d760e863fe8ca1ff75b6881daf31325dc874c00c1c5689ff32eb11ea",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "c7f408870abab909bf7b77c61bc8c7ab69d75c89e6dd90b652e6084308db600b",
        "files": {
            "kirocrew-team-lead.json": "86932e9c1aba9f032fc1c9fbaeff11933cac991890a1ed40f9b2c966ea13d0d5",
            "kirocrew-conductor.json": "ad42ea491a771f58e002c35148a1a5ab820b6aad1a0fac9a9d4e41d767820b0d",
            "kirocrew-dashboard-author.json": "816d5e5dd9988c2122f0c2363110deeb3f6384a41400e478ee617e9dfbe4fef3",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "726d2583a97ea66c78b23fefebb0877cea37ca727c6aa58d4f3b0579c3fd435e",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "8f993db1e97034c09237e656f2469c97bcf5dfb86faa94d039f385b9dc4f0ab8",
            "kirocrew-research.json": "bfa2bd9e571af9040c35d0ff1394d5248e504b8ff6888274f8c3b9071ce03e89",
            "kirocrew-security-conductor.json": "45567a2b9cc93fd50bc75bca810eaefc8088802ce3095b852615f5dd17d448d2",
            "kirocrew-worker.json": "845c08c523d2c18e8b2fb497ba2094f3424efce460ed2ad7c217feb755718c83",
            "kirocrew.json": "8d941b898a594406cfe134a4e903bbb23418caf43489653a80922667e0c57a72",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "3f05b0e215ac71c35d1915d5bdb3b7461eb29c3bbef4137912eb09e65af51aa3",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "4c10427a400719419b66eaf8cad743e8539d6d7c63fa96544bf2973175c69d80",
        "files": {
            "kirocrew-team-lead.json": "1d8c737af1c09eec158f983b910193d7640eaf6d6eb193a854df762aaedab105",
            "kirocrew-conductor.json": "3572b03555aedb80ca2a30f00b1d6aec75721d0f5f39ff233442f09d02233164",
            "kirocrew-dashboard-author.json": "d44d7f9bbb91f12cd6107609ca7c80b7bf81d6656bf884192e6c97241d39821d",
            "kirocrew-dashboard-manager.json": "4b96a77d13a14751d940d8f1d25ab578674db0fdf88e14222bf01b3cc5c86cd8",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "6dbd5042238c4b0565f250dd4e235f0f77b01f7d7e6091a127a29ec25e183cc3",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "d9583c36eefeae27f4f57dd8e428cf4b5893bc90882df0e88d88ac66e1dc4cb6",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "15b7e86caacf64e851bf4d00b97df22f52d47dfc2fe5ea3718695cca763b805d",
            "kirocrew-research.json": "76f1490c68081e1b3bc5454f6b6b1a2e7c833b84d19c401652ec03639d5d3d01",
            "kirocrew-security-conductor.json": "28a7df9a998ded3541af9f2f3f7ebd265cba8485be5a2c32d44d213012b82e5a",
            "kirocrew-worker.json": "775bb09a00f895ca24286dd73b4b9ca7ff066f63eb2ba6efe9c213e5e92fffe7",
            "kirocrew.json": "6772085ff729965cbff9b6ae0ad6b18ea983b89fab219eab3ef427613b2d4cd1",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "3f05b0e215ac71c35d1915d5bdb3b7461eb29c3bbef4137912eb09e65af51aa3",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "b3351b1dd6d52bd9d032f80a2a23de5e600f382f4327cd057c554caa85bb6e0b",
        "files": {
            "kirocrew-team-lead.json": "7ccbebff902167ac327ab53b55cc5ea3923990f2e0246388cd18ad2cd7f1caa5",
            "kirocrew-conductor.json": "6ba7388ed8e07194ebe7a2487299561eca8a1289a6ac4be402d0a74ec79c2b27",
            "kirocrew-dashboard-author.json": "d867f2989cb8864b75c88ebfb052c6131ebe1ab123a9020329273edb296cd499",
            "kirocrew-dashboard-manager.json": "19ba319895584c84459f46e7c343b60f098739ea91598f3603a8a97e1088e548",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "7a212fc757669b2be5d1b141d58bac4bafc3dd9e19e206a68994f20f4c4f6ed4",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "7b246eb43407cb2e79444d30951f5d5e4d20f728e88bbc0831582d008232489c",
            "kirocrew-lite.json": "0fda63413108908840a34020f9b11d1418f283a1dca92d14a748f8e25fe3ebee",
            "kirocrew-pipeline-conductor.json": "d14e919951362329ef1d5960a402b30037ee5d95dfe5db604da5a4c047b21fbc",
            "kirocrew-research.json": "6347bfc693d07431fec38ad367d8fef920f9a1831d1d2bc083de293b2b3f7899",
            "kirocrew-security-conductor.json": "fb87477b31545ea61cdad0ec04e3d5de4924e5abbce3609b53e2f77cb79c40d2",
            "kirocrew-worker.json": "eb2af92cfbdd2c7e07f7ff2919367fe0628b095751229dd7d66f88a30a7088b7",
            "kirocrew.json": "e631972a9b994ea6e6b76dd7f253895380b8f42e1d3cb91b5545a763f85179ea",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "3f05b0e215ac71c35d1915d5bdb3b7461eb29c3bbef4137912eb09e65af51aa3",
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
