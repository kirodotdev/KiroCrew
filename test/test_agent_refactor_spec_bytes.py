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

        A root that happens to sit under ``$HOME`` is ALSO spelled ``~/`` relative
        to it, which carries no absolute prefix to match: a ``skill://`` resource is
        written that way for portability, and on Windows the per-test scratch home
        lands under the user profile, so that is the only spelling the spec holds
        there. Both forms are folded to the same label, so a run's own
        worker-and-counter directory name cannot reach a digest.
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
        "events": "5ed66a55abd0152e3532cd31239cb5732cb171f58a57dc249138978a572ebe22",
        "files": {
            "kirocrew-conductor.json": "45b902563b9405cd62a7a095d90c51b0c4514286e582071b21f7bf931d66159d",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "fee22e31aa768d6944911f5207252b16026c612ac04af511fb76c1593666d518",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "cc38d843938887b90ab24e6389d56233b8557cc5e42acad08c7c290738765987",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-pipeline-conductor.json": "f964776a8d3ca68d5d17192597d88fad77d17b3c8903e3c52d9d92aab9a18948",
            "kirocrew-research.json": "e6f3ae6eaa577c909b7d28a9d3637efb8e75548312641b497d4c50418c9130b1",
            "kirocrew-security-conductor.json": "e92405dd1e9e8f1386e97b74d4b1f3f3d608b344351520dbbe43283a23bd0e01",
            "kirocrew-team-lead.json": "a21e9fd970f874d418145df43b535752675248c114525b708aedb1a9594d3a3e",
            "kirocrew-worker.json": "fd2dc72f87626fbffeefd1b69b2bbe17c90fe4a842b0a9a2c76bcee03e771a5c",
            "kirocrew.json": "43142cd2a3dc607bcf433ededeb58f87ee08e400ec9360081376ee06ecdceb87",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "customized": {
        "events": "07221a0131a2b7342e6b0b5e679ab0f2ad1513f02f376fa4bb573c489fcdf56d",
        "files": {
            "kirocrew-conductor.json": "45b902563b9405cd62a7a095d90c51b0c4514286e582071b21f7bf931d66159d",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "fee22e31aa768d6944911f5207252b16026c612ac04af511fb76c1593666d518",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "cc38d843938887b90ab24e6389d56233b8557cc5e42acad08c7c290738765987",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-pipeline-conductor.json": "f964776a8d3ca68d5d17192597d88fad77d17b3c8903e3c52d9d92aab9a18948",
            "kirocrew-research.json": "e6f3ae6eaa577c909b7d28a9d3637efb8e75548312641b497d4c50418c9130b1",
            "kirocrew-security-conductor.json": "e92405dd1e9e8f1386e97b74d4b1f3f3d608b344351520dbbe43283a23bd0e01",
            "kirocrew-team-lead.json": "a21e9fd970f874d418145df43b535752675248c114525b708aedb1a9594d3a3e",
            "kirocrew-worker.json": "5c0e29671eab6e931474863e89c49866408d404e6dba313811ede9e5a2283f55",
            "kirocrew.json": "0d38c977c724256afc08039198ed1456dec2fc99c67b9a66b0f32b3a197dc4c2",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "forks": {
        "events": "7a9e490c9a63e01a04e66830418c74f0f9c5eaebc810788018028983651c2057",
        "files": {
            "kirocrew-conductor.json": "10b7540a48cf0431de491b5a4f4685282f6c03561e767f4de04f68873248dbf3",
            "kirocrew-dashboard-author.json": "13b26acb9db5423062b4ec8a1a27de3887cdb528a93754363ee428a4fea02a14",
            "kirocrew-dashboard-manager.json": "27418f1fc0a4404d687a4df2f7d5d5c966e780dd64774aefcfbd742329d62ba8",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "2e3667734b05b5c3d7ef0aa7da3ed1246e1d5fd2ce73e77d339aa479c22cea83",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-pipeline-conductor.json": "e6391f999ae7f12d8de6ef7e0a76fe1a9cb7d508d215e6a25d4b09980f1cffe4",
            "kirocrew-research.json": "226891438f47950133f9fde98d5d7877321a2af2072ea47a4183337dc15d471b",
            "kirocrew-security-conductor.json": "07cd286733e8ed187b95de3f95e86320c04f5887932847403d231f3a6866a154",
            "kirocrew-team-lead.json": "916c880ca9645ce7abd9ac4b7ce47836999ceb5b66745c0b5aaad43fdcb16e1a",
            "kirocrew-worker.json": "b97f04d09d2faaee3e5cbd5640dc2d0a1540a58376655d9f331586c529117f6f",
            "kirocrew.json": "10e2f264c98fc348c2ff8cdd5f62d7935cd324af3d0e88d2ee9fe6ae4a3b784b",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "a041707d1f911fd7eaae2ca2afbe4304e67d03bb66dc5a3cabd65c8c55538309",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "cbb4cfdf2675dd8efeed9a8b77fc63ce287172f38fe4db9fad37ecb6654ed485",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "256cc2390d115b4cdad4591c48f143cf649e553f333d0c515352200f1396a25b",
        "files": {
            "kirocrew-conductor.json": "10b7540a48cf0431de491b5a4f4685282f6c03561e767f4de04f68873248dbf3",
            "kirocrew-dashboard-author.json": "13b26acb9db5423062b4ec8a1a27de3887cdb528a93754363ee428a4fea02a14",
            "kirocrew-dashboard-manager.json": "fee22e31aa768d6944911f5207252b16026c612ac04af511fb76c1593666d518",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "2e3667734b05b5c3d7ef0aa7da3ed1246e1d5fd2ce73e77d339aa479c22cea83",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-pipeline-conductor.json": "e6391f999ae7f12d8de6ef7e0a76fe1a9cb7d508d215e6a25d4b09980f1cffe4",
            "kirocrew-research.json": "f4ad1ea7fce492420f5e93905ff38ee0ef97cf362089e56aa8a51862c6266921",
            "kirocrew-security-conductor.json": "07cd286733e8ed187b95de3f95e86320c04f5887932847403d231f3a6866a154",
            "kirocrew-team-lead.json": "c3cb33fb7155a95e7dc66b389ed021681de8e2f25abccf455d6a3acdfcfcf139",
            "kirocrew-worker.json": "1f7638b4abbdefbdc252c9d8ee1737283dc96577f4add354a25e6de34dced95c",
            "kirocrew.json": "c9cacdd8d1615af86ce42dc1b2cde4e4bd6d1df26841b631ce1d06bde5c62abc",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "governed": {
        "events": "8ac6c6f999fb65842a7b6948e55311498679aa7ddf8cf164e7f92038c92110cc",
        "files": {
            "kirocrew-conductor.json": "0f98ec27fecae7b27eba89c2d1cd8d8552c605b87de8b6ef5989e6bb8d1b4b50",
            "kirocrew-dashboard-author.json": "e59ffa49b68f1bd5a379b964d94807b958722cd368de4ca24cc3a626ae46f6d6",
            "kirocrew-dashboard-manager.json": "fee22e31aa768d6944911f5207252b16026c612ac04af511fb76c1593666d518",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "7dd42b5e5b91938be88ecd69d1244b8f1504c2522ef754bbc5dd2c53752501ff",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-pipeline-conductor.json": "f964776a8d3ca68d5d17192597d88fad77d17b3c8903e3c52d9d92aab9a18948",
            "kirocrew-research.json": "0cb7038ce95c848b4fd6625a85cf9eccd0ef87224ce2f8ad65c0389990b5c7bb",
            "kirocrew-security-conductor.json": "e92405dd1e9e8f1386e97b74d4b1f3f3d608b344351520dbbe43283a23bd0e01",
            "kirocrew-team-lead.json": "0ad159934a5e5c5414058ebef41dc3685c02abb2658cbb5b7ea0a2af8ff60fbc",
            "kirocrew-worker.json": "534ea64e3b7152f1ab0edb5a742faead62719e9666b53ecabb910223fdc7e233",
            "kirocrew.json": "5d5106adb46783cdaa2418b2285327eff7016d0070407552f72dba1bb8aa00f4",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "6a9c5563feb9cbc42dd4056b60626a35fc577b0ae982b3739731f2cb2e5ff48f",
        "files": {
            "kirocrew-conductor.json": "44763819a4ffc6b85fa20b79a019a2664b14cb8d8af84c16140ec1d1758012d4",
            "kirocrew-dashboard-author.json": "9f1f2a77b3bec1d748134ce04ac3c329df51a15c2d01a8891a4b4ff5d17eb0a9",
            "kirocrew-dashboard-manager.json": "fee22e31aa768d6944911f5207252b16026c612ac04af511fb76c1593666d518",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "4d9ee797f418dac63c2a0c82ecff1634ab673dbb83c1dc21d4786c6b3d1559f8",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-pipeline-conductor.json": "e724508ee90474abe4e4939d3a79b197d22996dea982829748f398ff3602385f",
            "kirocrew-research.json": "7666a438bbdfc5d547687d5b473d04471aaddf9757194e6227669d9e86182980",
            "kirocrew-security-conductor.json": "5d18d21cd5aecc8b725ddc27077db94df3033121a06734360af6d087e8c8c83f",
            "kirocrew-team-lead.json": "d7a5b389d2946675662ef47ac7e08d19d59a90fc69c4c674aad018fc9b722987",
            "kirocrew-worker.json": "f2183e12292ce42964aa714124a6303761fc40cb9ffdbb81ce5a8e7361aa6da8",
            "kirocrew.json": "5f16234af31f5c3904a684dea2e2d90131c2cd806d91401fee26e555554fbe8f",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "256cc2390d115b4cdad4591c48f143cf649e553f333d0c515352200f1396a25b",
        "files": {
            "kirocrew-conductor.json": "55c8d0b464e2260296e8c8f086c1a329e852f5b1618ed13faffc6b9cd105254e",
            "kirocrew-dashboard-author.json": "9474463f6cdc27fc7b2e592a7037a78542a38392cc1a7b350529bb1889110b5a",
            "kirocrew-dashboard-manager.json": "c5bde6c998a0cb21c9f8e9ef8bdd9b3eb2dfd8533ee858995ba5eb0fea423599",
            "kirocrew-guest.json": "2423a7b447fbcedec2a64ab54a89d181cb2357456c8ddcfc189dc2afe3525780",
            "kirocrew-heartbeat.json": "67b72c1885db892bcee38baeeb2b82647b26f21eb67164a56fc80ec058a23bb5",
            "kirocrew-knowledge.json": "5275c0f70b6b42581c9c9841a572c16673b3a5ede1317936f4d4d870e2a883a0",
            "kirocrew-ledger-conductor.json": "34477ce92ec0ed21cb4df0d2e84c8b3d2d089e2167ee93411e962ecdbdeb1d2b",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-pipeline-conductor.json": "2afbd27cb485888dc61f088f30f2b96dba535522b2213f46018a166570007efe",
            "kirocrew-research.json": "4f6418d8353ac284af564ffe464e7e4852f0af510ba05c424387a7f0f6b9d961",
            "kirocrew-security-conductor.json": "01bcf43a6f2e008cd898ef2dad5abd07f833ace6b79fb75f07c1aa1f56b678a0",
            "kirocrew-team-lead.json": "2fca6400fd50db69b978c0a5b9c5712094b367461f8396c1b41d55701f092888",
            "kirocrew-worker.json": "5729557d013d06e9ad99e6e88b83d04ec2703c45aaa6e6f32be7e411d3be40f3",
            "kirocrew.json": "39df0214ce448297f90392a4c0b954146bcd2a3124c112040348f202c12871b8",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "77662b39a7247eafdf1d85553b71b74003d9d00ed27103c8b1132ac919684462",
        "files": {
            "kirocrew-conductor.json": "78fa7cec0c81ba90ef02b6c55e3ebe28571b33c35cda714b68cf2fff1b6359a0",
            "kirocrew-dashboard-author.json": "ed8fa5a35960b358808bd8ba677678ea358a115703c11819f3a37c50aafd2bb2",
            "kirocrew-dashboard-manager.json": "fee22e31aa768d6944911f5207252b16026c612ac04af511fb76c1593666d518",
            "kirocrew-guest.json": "3ac87f33f4968a07c6f3dc45b29922d7002d38e93caabefa5005a7a1794fce38",
            "kirocrew-heartbeat.json": "76c356cf714706d30d0e60062580ae82149ef00a4c9019368df57b1ba3e6506b",
            "kirocrew-knowledge.json": "efcde26b5961417a7c9ed665ee20038461b5283b74099423cb8ee474400335f5",
            "kirocrew-ledger-conductor.json": "b5570908c78885bdc78995a1c4eb81c591688382365e526a48f7e32960568455",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-pipeline-conductor.json": "332243fd3e8cc25eb33407ac8ef3be6b9af729a2e7328e45aa67a50e5a8fbc7e",
            "kirocrew-research.json": "7db3afde79a24d50e572046bc40531b845cfa17972dc2e0c408dc3be648f769a",
            "kirocrew-security-conductor.json": "abcdf7d1fa1e308c06302fd9c6b1d3d8c73698efde0a521f37f01ef4db642a6e",
            "kirocrew-team-lead.json": "f7cfd716ac7bf1ae3c32ac629071db54f9b3f47d6efe59c547ef9b6380fb5835",
            "kirocrew-worker.json": "b7714a8519785d8e78f6c3c528d21fbf27209620387d39189b53f36eeaa0f508",
            "kirocrew.json": "d5ca27c45f81c0754d1c06aedd7ec81b8587f9ab16b33434d3e196b76e94d268",
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
