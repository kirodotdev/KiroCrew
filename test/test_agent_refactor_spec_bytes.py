"""The exact bytes every agent-spec writer puts on disk, pinned as golden digests.

Each scenario runs :func:`kiro_crew.agent.rebuild_agent_config` on the SHIPPED defaults,
prompts and server registry, pinning only the launcher path, the kiro-cli version and
the SEL writer. Spec files, audit records and sidecar state are normalized and compared
to SHA-256 goldens, so a dropped, reordered or re-typed field fails here even when no
field-level test names it; a mismatch prints the differing content.

The goldens pin what kiro-cli reads from each spec (``tools``, ``allowedTools``,
``permissions``, ``hooks``, ``mcpServers``, ``toolsSettings``) and the STRUCTURE of each
``prompt``: every shipped prompt piece is replaced by its source name before hashing (see
:func:`_mask_prompt`), so a piece that is dropped, swapped or added fails, and an edit to a
piece's wording does not. Masking names a piece by the constant it is bound to, so it
cannot see two constants trade their text or a role lose its core instruction:
:data:`_CHARTERS` pins those as a contract, the sentences that say what each agent is for.

The goldens carry POSIX paths and exec bits, so these run off Windows.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pkgutil
import re
import string
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent, agent_state, crewmate_prompts
from kiro_crew.agent_spec_format import parse_markdown_spec
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

#: One path segment under a normalized root, with the separator run before it: a
#: Windows spec spells ``<TMP>\\bin\\kirocrew`` where POSIX spells ``<TMP>/bin/kirocrew``.
_UNDER_ROOT = re.compile(r"(<TMP>|<HOME>|<PKG>)((?:\\+[^\\\"\s]+)+)")
_SEPARATORS = re.compile(r"\\+")


#: The packages whose module-level text the spec writers put into a ``prompt``.
_PROMPT_PACKAGES = ("kiro_crew.agent_parts", "kiro_crew.agent_materialization")
#: Shorter text is glue (a separator, a filled-in name) and stays in the digest.
_MIN_PIECE = 40


def _literal_parts(text: str) -> list[str]:
    """*text* and, when it is a ``str.format`` template, each literal run between fields."""
    try:
        parts = [literal for literal, _f, _s, _c in string.Formatter().parse(text)]
    except ValueError:
        return [text]
    return [text] + [part for part in parts if part != text]


@lru_cache(maxsize=1)
def _prompt_pieces() -> tuple[tuple[str, str], ...]:
    """Every shipped prompt piece and its source name, longest first.

    Read from the source itself: module-level strings, ``(name, text)`` section tuples,
    ``{name: text}`` tables and shipped ``.md`` specs of the spec-writer packages and
    :mod:`kiro_crew.agent`, plus every :mod:`kiro_crew.crewmate_prompts` piece. A template
    contributes each literal run between its fields, since the writer fills them. Pieces
    are stripped so two adjacent pieces cannot claim the same blank line. Longest first,
    so a prompt joined from sections is replaced by the one name it is bound to: adding,
    editing or removing one of its sections is an edit to that prompt's wording.
    """
    modules = [agent]
    for package_name in _PROMPT_PACKAGES:
        package = importlib.import_module(package_name)
        modules += [
            importlib.import_module(f"{package_name}.{info.name}")
            for info in pkgutil.iter_modules(package.__path__)
        ]
    names: dict[str, set[str]] = {}

    def add(label: str, text: str) -> None:
        for index, part in enumerate(_literal_parts(text)):
            part = part.strip()
            if len(part) >= _MIN_PIECE:
                names.setdefault(part, set()).add(label if index == 0 else f"{label}#{index}")

    for module in modules:
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            if isinstance(value, str):
                add(name, value)
            elif isinstance(value, tuple):
                for item in value:
                    if isinstance(item, tuple) and len(item) == 2:
                        key, text = item
                        if isinstance(key, str) and isinstance(text, str):
                            add(f"{name}[{key}]", text)
            elif isinstance(value, dict):
                for key, text in value.items():
                    if isinstance(key, str) and isinstance(text, str):
                        add(f"{name}[{key}]", text)
            elif isinstance(value, Path) and value.suffix == ".md" and value.is_file():
                add(value.name, parse_markdown_spec(value.read_text(encoding="utf-8"))["prompt"])
    for key in sorted(crewmate_prompts.keys()):
        add(key, crewmate_prompts.text(key))
    assert names, "no prompt pieces were found in the spec-writer packages"
    return tuple(
        (piece, min(labels))
        for piece, labels in sorted(names.items(), key=lambda p: (-len(p[0]), p[0]))
    )


def _mask_prompt(text: str) -> str:
    """*text* (one spec file) with each shipped piece of its ``prompt`` replaced by its name.

    The glue between pieces, and any text no piece holds, is kept as written, so the
    digest still sees which pieces a spec carries, in which order, and what joins them.
    """
    try:
        spec = json.loads(text)
    except ValueError:
        return text
    prompt = spec.get("prompt") if isinstance(spec, dict) else None
    if not isinstance(prompt, str) or not prompt:
        return text
    masked = prompt
    for piece, label in _prompt_pieces():
        masked = masked.replace(piece, f"<{label}>")
    for spelled in (json.dumps(prompt), json.dumps(prompt, ensure_ascii=False)):
        if spelled in text:
            return text.replace(spelled, json.dumps(masked))
    raise AssertionError("a spec's prompt is not spelled in its file as JSON writes it")


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
            "files": {name: _sha(_mask_prompt(text)) for name, text in sorted(self.files.items())},
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
        # The goldens are the POSIX hooks; the Windows audit-hook form is pinned
        # by test_default_audit_hook_windows.py.
        monkeypatch.setattr(agent, "platform_spec_hooks", lambda hooks: hooks)
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


#: Digests of each scenario's normalized output, prompts masked. See the module docstring.
GOLDEN: dict[str, dict[str, Any]] = {
    "clean_over_customized": {
        "events": "8a1292a50cf6d7b109e4bfdb616cccb735a3e7af58d474f78f45657aa864e06f",
        "files": {
            "kirocrew-conductor.json": "d4ac31e6aff9d99f6b88fde6ee972c992f0312550332d9960457dd5bbb641ab4",
            "kirocrew-dashboard-author.json": "194e669965a46fb03eac5ad4dfdcfc9f682f80f1292fa479785ab93cf45e511c",
            "kirocrew-dashboard-manager.json": "1839dfce0f7fbae4d5fc08a12f73bfba2ac365b56c86c692326473557217e38a",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "f3e4c5e43f8f461c7d9e97455d9a11feb49d321f511bd59e8718def932809bcb",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "b990d93ba20ccb9e74da9c7452e6b01412a60ed9234f68568117566010cedd5b",
            "kirocrew-mate-worker.json": "2fa03ec1edeaaf39fa64573d72b11da71b2231b3642e71cbb4c445612b6da3a9",
            "kirocrew-pipeline-conductor.json": "d0dc4f0a70e6fdea710823da3595075b386e68a39f8b3d74fde6b1eed5b4e58b",
            "kirocrew-research.json": "3dd954206ffa4dcdf8e66d60530398a287de4199680301b5044f945b31a2ca44",
            "kirocrew-security-conductor.json": "92f0065a6dfd2526c7b2ab8a895a7c3f6cd9ae5386f6a1165c8fcbb084f30551",
            "kirocrew-team-lead.json": "dea11499425fa6333fa0d8b3c0650bae7aef040795642491693fd1d159b08571",
            "kirocrew-worker.json": "8d190a91f0a06c5c134b743f03c2569441668e0ffdf8a8dac8ac6abaafd35573",
            "kirocrew.json": "7a20ddcdc9351479cbaf884bdd547c410fb8cdba3bfbd3b824443612ddbfdbbd",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "customized": {
        "events": "689843ecdab4ac610c4ed655d61bde48f8ae9086467f92ec63665b3b5b2ff575",
        "files": {
            "kirocrew-conductor.json": "d4ac31e6aff9d99f6b88fde6ee972c992f0312550332d9960457dd5bbb641ab4",
            "kirocrew-dashboard-author.json": "194e669965a46fb03eac5ad4dfdcfc9f682f80f1292fa479785ab93cf45e511c",
            "kirocrew-dashboard-manager.json": "1839dfce0f7fbae4d5fc08a12f73bfba2ac365b56c86c692326473557217e38a",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "f3e4c5e43f8f461c7d9e97455d9a11feb49d321f511bd59e8718def932809bcb",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "b990d93ba20ccb9e74da9c7452e6b01412a60ed9234f68568117566010cedd5b",
            "kirocrew-mate-worker.json": "2fa03ec1edeaaf39fa64573d72b11da71b2231b3642e71cbb4c445612b6da3a9",
            "kirocrew-pipeline-conductor.json": "d0dc4f0a70e6fdea710823da3595075b386e68a39f8b3d74fde6b1eed5b4e58b",
            "kirocrew-research.json": "3dd954206ffa4dcdf8e66d60530398a287de4199680301b5044f945b31a2ca44",
            "kirocrew-security-conductor.json": "92f0065a6dfd2526c7b2ab8a895a7c3f6cd9ae5386f6a1165c8fcbb084f30551",
            "kirocrew-team-lead.json": "dea11499425fa6333fa0d8b3c0650bae7aef040795642491693fd1d159b08571",
            "kirocrew-worker.json": "e4f3b3122853125989f60e89b1451e664a4b9ac8738aa7d90c1d58ca24b7167f",
            "kirocrew.json": "c922320f44f636a5227b209f94802cb20a52fbb0ca2b49cd6fe186b79dfa82d5",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "forks": {
        "events": "3df8217b2b62a2c0b59480c2c3b7da5f3e3ef2c891b45e490b7f1e9204526252",
        "files": {
            "kirocrew-conductor.json": "99e93c2120d43e6b0ca6f8ebadec0e3c1664624bddaba28e0a819851df3c341e",
            "kirocrew-dashboard-author.json": "8aee5dfc36146bc4a9f2e6b10eaa6a72354246b60bfe1ef0497129ca276eaf49",
            "kirocrew-dashboard-manager.json": "faba27b816509f80e255c09b4097c37870de7202472df51d9a843d41fab5d8b6",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "db64d274c3cc5fb2cc72d5bf5ea2ab51888165e48008669dac7114e9ac7b0838",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "c1d9ea109377981a1230834dee833fc7895a5ff61037bf98a1c0df8d34868691",
            "kirocrew-mate-worker.json": "f99cbf3bbaa425944c933fb85e46294edaf49621bcdcf5663d97ac29c436bd61",
            "kirocrew-pipeline-conductor.json": "f711e57033713c5045927c0dd4438d973f0f49ee041f75e9bb92784f0b3a7303",
            "kirocrew-research.json": "d4ffdd4f5f4da16c397574c3b32542c8d13344a35c5f5af9c1ef413ab398dac2",
            "kirocrew-security-conductor.json": "08ba0921cc29e640ed97685a53994ffc0a13664727c17da66d0a7a9e8cf421e9",
            "kirocrew-team-lead.json": "4c9fe0a93e824cb4493982a01da1344114834e0d34da5f48d09856dfd04a6bc0",
            "kirocrew-worker.json": "9eaf9094b4c1ea6fbd33cb302311314a8a94dd4bec0acb5ccb9774f61b9d39da",
            "kirocrew.json": "12929381963879d117ff0bf4c35238c2b8cff3d8e6aab5c81f6e497a07f64106",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "my-crew.json": "a8fd14659eddb33be9c41215bb3f79890eaccff97392c5bcef560198918aedbf",
            "orphan-crew.json": "30c576d8c4eb514bdbb5139402df6588504cc92cfef8b580ec2e16bc98f74056",
        },
        "state": "cbb4cfdf2675dd8efeed9a8b77fc63ce287172f38fe4db9fad37ecb6654ed485",
        "unrefreshed": ["orphan-crew"],
    },
    "fresh": {
        "events": "93c96177077b06656eaa1c26e81098e451c2a5dcae0d1966b8194e339fec1011",
        "files": {
            "kirocrew-conductor.json": "99e93c2120d43e6b0ca6f8ebadec0e3c1664624bddaba28e0a819851df3c341e",
            "kirocrew-dashboard-author.json": "8aee5dfc36146bc4a9f2e6b10eaa6a72354246b60bfe1ef0497129ca276eaf49",
            "kirocrew-dashboard-manager.json": "1839dfce0f7fbae4d5fc08a12f73bfba2ac365b56c86c692326473557217e38a",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "db64d274c3cc5fb2cc72d5bf5ea2ab51888165e48008669dac7114e9ac7b0838",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "e4acbfd9bd945efea8846d4a0baf17648da58ed5f4cc21a9dbc65497348692d0",
            "kirocrew-mate-worker.json": "59b6973ade3bd405eeb07289131b930a9d6e1034773b98e1e5ce172b55241433",
            "kirocrew-pipeline-conductor.json": "f711e57033713c5045927c0dd4438d973f0f49ee041f75e9bb92784f0b3a7303",
            "kirocrew-research.json": "2164681efac61d047b467101c1ad2e29baf71e325b09661841aac9f90fc89f62",
            "kirocrew-security-conductor.json": "08ba0921cc29e640ed97685a53994ffc0a13664727c17da66d0a7a9e8cf421e9",
            "kirocrew-team-lead.json": "81fb6af33b84891b3f671f438c62e5b778b9f9ac85348e022572960a292930c4",
            "kirocrew-worker.json": "2735eb40a80a4208d661329d89837f3d51444ca65762e9d2fa7dd9d0a855ab93",
            "kirocrew.json": "5bf9b03ec3866d4a040c42e8bf4a6394483623f1eefa7cc9d55263c4db71a837",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "governed": {
        "events": "93bd7d4f65263ee348127b986c34b3fdec387d4f931633394e9d2492aee63182",
        "files": {
            "kirocrew-conductor.json": "b7770cbf97a0853dcccee07bf0e7006a1a529ee98654441c50a59d890c80b4b8",
            "kirocrew-dashboard-author.json": "194e669965a46fb03eac5ad4dfdcfc9f682f80f1292fa479785ab93cf45e511c",
            "kirocrew-dashboard-manager.json": "1839dfce0f7fbae4d5fc08a12f73bfba2ac365b56c86c692326473557217e38a",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "aeeb9cdf15fdabc7583a0c9632456a50b6ff01013d537383d681cd45d5054692",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "67ad8a50b5853647f92ebe86b1b2cfb2274fc5a341de4100a0dc8c9fb4c35c0f",
            "kirocrew-mate-worker.json": "bf03d3edd1fcfff78717ac3dad017f607f3081c2c809a4c68f1e7aa79720dda2",
            "kirocrew-pipeline-conductor.json": "d0dc4f0a70e6fdea710823da3595075b386e68a39f8b3d74fde6b1eed5b4e58b",
            "kirocrew-research.json": "4634ebe96321ddac989e1ecfd70986829ca0946e2e7143eb397b91e9a592b769",
            "kirocrew-security-conductor.json": "92f0065a6dfd2526c7b2ab8a895a7c3f6cd9ae5386f6a1165c8fcbb084f30551",
            "kirocrew-team-lead.json": "63cee1a58a6dab0dcc174aaa5c1d323a3e64375bc7669b1e665ba619d0bb18c6",
            "kirocrew-worker.json": "d4a26ad2c80d6efe99c54bd5b334e21f9012fd3a7740b414ed00eabfd9818c05",
            "kirocrew.json": "f622e960f8f9a823e8451fc40e67401621e879c060b1df43eebedd5e9bf503f6",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "00ad0b59a7575f63d3571802d1424b4146cc87a01e567590508ebd4832f572d1",
        "files": {
            "kirocrew-conductor.json": "fca1e0adfc781f9155f691af1497299e8d2f48849cfc04fd5f3b6834ee8023d4",
            "kirocrew-dashboard-author.json": "e8e23929204e16f79f76a7ce4a8d072e6e427769abf05da7cf3fae0f5baa35bb",
            "kirocrew-dashboard-manager.json": "1839dfce0f7fbae4d5fc08a12f73bfba2ac365b56c86c692326473557217e38a",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "652f40e1b94b6f4b6f5169913ded047967e66a787eedb6e5bc22861ad4ac3359",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "b69673fd758105ab659cde318b3b79739e05813b2c64d94bfb83c2149ce0c40a",
            "kirocrew-mate-worker.json": "53ad9c1de5de84ee8affe700c427384b27f7c2a355747ec4e8374eb86da735f9",
            "kirocrew-pipeline-conductor.json": "e1cd40be339f2ebb7acf897cbed06cd7c52d68e8d70e20344ad05fe60a913c84",
            "kirocrew-research.json": "9861d54d369bd70211ab7753280dea66c388038bedadadc8969567df877efb94",
            "kirocrew-security-conductor.json": "c6f3eab7fdfbe5ebe296154657b8d049a7748f248643854550fefc6c6eb54fc1",
            "kirocrew-team-lead.json": "e4d6109e47607098a44e3a452f21016d62161dd9ef2232d7bc94d8998aa62c93",
            "kirocrew-worker.json": "947bd03dbcb51dfa30d433cbe86acf2b210f9366690976796b808de77bc4a549",
            "kirocrew.json": "88a388bb19437cf0343364aa7b5f0d92a85ede944f519a81fbe265cfe92d8b22",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "93c96177077b06656eaa1c26e81098e451c2a5dcae0d1966b8194e339fec1011",
        "files": {
            "kirocrew-conductor.json": "5d129f9a8b7996fc0036d1d822c8c26ed25d3a6ab32c2767eb5e63953952a783",
            "kirocrew-dashboard-author.json": "5da8c34ed50bb51060668577187f8c40d668b5d7561794dc33760313818cc24c",
            "kirocrew-dashboard-manager.json": "83f6808059c7811a0bbe2372c1241da3941ea47ea77fd99a7fe99ba7402f7122",
            "kirocrew-guest.json": "fcaaecb0023ee4dde10ac4b91acecc02183aa56cfa43b1478cdb103ac9bbc5aa",
            "kirocrew-heartbeat.json": "fe622d21c28e2d592ad3b50cd7d6d602a6b3a4be3c257fc3249d6664abd49d1f",
            "kirocrew-knowledge.json": "69dd6a07083e0060eb8dc1c8bcbbe6a080b4e57aeca4648ce0f421a5a07a1f49",
            "kirocrew-ledger-conductor.json": "c605113658f9561e06065c79bf08eeaea7a60b0f9a721c5201d30e9c17893945",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "0310729030aeb41906d402c2b8f0e07c8c87c8b690e804cac7c4f29b5ae07dce",
            "kirocrew-mate-worker.json": "059c274a76bea320601cde0fb968ff2e0b9ae4419dd6d95cc40184770d987274",
            "kirocrew-pipeline-conductor.json": "7ff830c43ac7bb3614ae8199abc869f5276ec797a5b10570441a2238ee3098e1",
            "kirocrew-research.json": "94d717179e489a77db57bc390f03845583163e511ac3d29609f6ff0c2b86e855",
            "kirocrew-security-conductor.json": "de5189446dd757b4ec5e911ac22e9259f58125d842840c0d0f4c883723ebdba0",
            "kirocrew-team-lead.json": "beca340d7ade925c8a1325a24a1b650406a927763964cc44c76f5f901915ca95",
            "kirocrew-worker.json": "bec63d8c85c254ef2474b738ca200f459917bc206f8d7310bbf5f611db87a6d8",
            "kirocrew.json": "73ed62e33346c2323979203ccfd94ad0321c391f46ee7b9010f51b0411565bd2",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "77c888bf115c62f26548733291ed78336f6421baf700190ea8e44a8c1a3c6d41",
        "files": {
            "kirocrew-conductor.json": "a73fac428726dc7fe9eec6963aa95ff464fe750eb4e58621daa8b3ed8a344c79",
            "kirocrew-dashboard-author.json": "89c1aad0eecd6a09624e12f912b5a05396b282e10465f2cd6dcd5bc593ad333e",
            "kirocrew-dashboard-manager.json": "1839dfce0f7fbae4d5fc08a12f73bfba2ac365b56c86c692326473557217e38a",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "09b171d05fcd12531428602f8dc0f054e7d91b4743816a4cb55f9c74d3bc7af5",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "f78cf48ee793fc32b8a18528f56ea1da371b801fcbab13155324de2af80e2728",
            "kirocrew-mate-worker.json": "c795072e0fa184237f7bffebba5b52712ed07b10f673d4f24d2120cecc1576c6",
            "kirocrew-pipeline-conductor.json": "39a6406bd9dc54ed1f9763316d1c0ac42e075e07bcae22372ba5a8efef2f54d4",
            "kirocrew-research.json": "7fac6930c96c20ddf6b037456e6e5f7d0177c601f04348abcddfbc63187ce0fa",
            "kirocrew-security-conductor.json": "99f01f3536ce1cbae11b8d4072607f6c3514df0ed1ba789f85b78639034a14a8",
            "kirocrew-team-lead.json": "ec67c9f745551d4394d1812c2ae1a5a5bc7eeeea0aeac2db288c1e552f222818",
            "kirocrew-worker.json": "4007af2c034d0cd03b75b0bb82aa09db023db252f5dd46bbca9f29e947e26275",
            "kirocrew.json": "3763dfb562eee8be2deb8a9f55f97012f0d2dea84e8ad09f4875dc5d21d2fd01",
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
        ), f"{scenario}: {name} no longer matches the pre-split bytes:\n{_mask_prompt(got.files[name])}\n{regen}"
    assert digests["events"] == expected["events"], (
        f"{scenario}: the audit record sequence changed:\n"
        + json.dumps(got.events, indent=1, sort_keys=True)
        + f"\n{regen}"
    )
    assert (
        digests["state"] == expected["state"]
    ), f"{scenario}: the agent-state sidecar changed:\n{got.state}\n{regen}"
    assert digests["unrefreshed"] == expected["unrefreshed"], "the fork refresh verdict changed"


#: Contract: the sentences that say what each built-in agent is for, and the specs that carry
#: each one. Matched with whitespace collapsed, so rewrapping a line passes; rewording one of
#: these sentences changes a role and updates this table in the same change.
_CHARTERS: dict[str, frozenset[str]] = {
    "You are `kirocrew-conductor`. You own a long-horizon goal": frozenset(
        {"kirocrew-conductor.json", "kirocrew-ledger-conductor.json"}
    ),
    "You are `kirocrew-dashboard-author`. You author ONE dashboard template": frozenset(
        {"kirocrew-dashboard-author.json"}
    ),
    "You change ONE Bot's Dashboard page, and you change nothing else.": frozenset(
        {"kirocrew-dashboard-manager.json"}
    ),
    "You are answering a guest": frozenset({"kirocrew-guest.json"}),
    "You are `kirocrew-heartbeat`, an unattended polling worker": frozenset(
        {"kirocrew-heartbeat.json"}
    ),
    "You are a knowledge extraction specialist": frozenset({"kirocrew-knowledge.json"}),
    "You ALWAYS output valid JSON.": frozenset({"kirocrew-knowledge.json"}),
    "Treat that block as your system prompt": frozenset(
        {"kirocrew.json", "kirocrew-mate-conductor.json", "kirocrew-mate-worker.json"}
    ),
    "You are the worker a Bot hands its work to": frozenset({"kirocrew-mate-worker.json"}),
    "Start by calling `work_brief`, whatever the first message says.": frozenset(
        {"kirocrew-worker.json", "kirocrew-mate-worker.json"}
    ),
    "You are `kirocrew-worker`. A conductor dispatched you for exactly ONE work item": frozenset(
        {"kirocrew-worker.json"}
    ),
    "You are `kirocrew-pipeline-conductor`. You run ONE pipeline on ONE repository": frozenset(
        {"kirocrew-pipeline-conductor.json"}
    ),
    "You are `kirocrew-research`, an autonomous research worker.": frozenset(
        {"kirocrew-research.json"}
    ),
    "You are `kirocrew-security-conductor`. You run ONE security audit on ONE target": frozenset(
        {"kirocrew-security-conductor.json"}
    ),
    "You are `kirocrew-team-lead`. A person hands you a goal and you own it end to end.": frozenset(
        {"kirocrew-team-lead.json"}
    ),
}


def test_each_spec_prompt_carries_its_own_charter_and_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = materialize(tmp_path, monkeypatch, "fresh")
    prompts: dict[str, str] = {}
    for name, text in got.files.items():
        try:
            spec = json.loads(text)
        except ValueError:
            continue
        if isinstance(spec, dict) and isinstance(spec.get("prompt"), str):
            prompts[name] = " ".join(spec["prompt"].split())
    for sentence, carriers in _CHARTERS.items():
        assert carriers <= prompts.keys(), f"no spec named {sorted(carriers - prompts.keys())}"
        for name, prompt in sorted(prompts.items()):
            if name in carriers:
                assert sentence in prompt, f"{name} lost its charter sentence {sentence!r}"
            else:
                assert sentence not in prompt, f"{name} carries another role's {sentence!r}"
