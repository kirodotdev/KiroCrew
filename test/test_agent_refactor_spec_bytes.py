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
            "kirocrew-conductor.json": "eadd200cd548667ebf48cd99be4bce33a8cd4341ae270bd7041542a89aeb61c7",
            "kirocrew-dashboard-author.json": "194e669965a46fb03eac5ad4dfdcfc9f682f80f1292fa479785ab93cf45e511c",
            "kirocrew-dashboard-manager.json": "403a3e755e548b1a425d535ffe9e78e9eae066cbadc60a84a3f498a53c4333f6",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "28bc0ca61b8e4ffdf45dd3d4e3b88c3893c849dcef26d54d2ecdded378d7070a",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "88c16f348990aa7cda205a1df1ba22414513ae6179bcb3170fe591ad38cc73bb",
            "kirocrew-mate-worker.json": "8871c7307237ef119fbc9616af6b5c9fa8d28eb062e90c976a8a75bb796fec81",
            "kirocrew-pipeline-conductor.json": "9400c5a9be49b1dc72b5169e7fea8f4203312636a55ca7647eb79b3399fad34c",
            "kirocrew-research.json": "3dd954206ffa4dcdf8e66d60530398a287de4199680301b5044f945b31a2ca44",
            "kirocrew-security-conductor.json": "6ca5219fad6a82cd75fee6016755c81e5fca766ddda1220ac03378e3a6eb6360",
            "kirocrew-team-lead.json": "aebd989fd756c2ea0c4d68b1a03abdcbe4644f8a02b0e066fa49d59d31b9cdf3",
            "kirocrew-worker.json": "8ae16ce90c2bb711e54f0fb88ee75a0aca9cbb78b0231a392e393e5a729b2569",
            "kirocrew.json": "2b3091720efb155bea2e89479ec02f01dee39108d249b103b24630faa62844b0",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "customized": {
        "events": "f21615ae2b4420abba400489a4b233c1f9375fb53e09eddf7c927bb3f3b6de5d",
        "files": {
            "kirocrew-conductor.json": "eadd200cd548667ebf48cd99be4bce33a8cd4341ae270bd7041542a89aeb61c7",
            "kirocrew-dashboard-author.json": "194e669965a46fb03eac5ad4dfdcfc9f682f80f1292fa479785ab93cf45e511c",
            "kirocrew-dashboard-manager.json": "403a3e755e548b1a425d535ffe9e78e9eae066cbadc60a84a3f498a53c4333f6",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "28bc0ca61b8e4ffdf45dd3d4e3b88c3893c849dcef26d54d2ecdded378d7070a",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "88c16f348990aa7cda205a1df1ba22414513ae6179bcb3170fe591ad38cc73bb",
            "kirocrew-mate-worker.json": "8871c7307237ef119fbc9616af6b5c9fa8d28eb062e90c976a8a75bb796fec81",
            "kirocrew-pipeline-conductor.json": "9400c5a9be49b1dc72b5169e7fea8f4203312636a55ca7647eb79b3399fad34c",
            "kirocrew-research.json": "3dd954206ffa4dcdf8e66d60530398a287de4199680301b5044f945b31a2ca44",
            "kirocrew-security-conductor.json": "6ca5219fad6a82cd75fee6016755c81e5fca766ddda1220ac03378e3a6eb6360",
            "kirocrew-team-lead.json": "aebd989fd756c2ea0c4d68b1a03abdcbe4644f8a02b0e066fa49d59d31b9cdf3",
            "kirocrew-worker.json": "0f02f78fac87428b801f4fe33110a644ae895cf7d3ffbfb17d04b6ad6e893043",
            "kirocrew.json": "d45bb2196c7f59cec22e98c8553e92b48b43a12d3d1fcb80370c90fcb7034476",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "forks": {
        "events": "3df8217b2b62a2c0b59480c2c3b7da5f3e3ef2c891b45e490b7f1e9204526252",
        "files": {
            "kirocrew-conductor.json": "ffdb4785aa31b0115063a004846f98cd4ae5213897f91f70856aa8efdd228f39",
            "kirocrew-dashboard-author.json": "8aee5dfc36146bc4a9f2e6b10eaa6a72354246b60bfe1ef0497129ca276eaf49",
            "kirocrew-dashboard-manager.json": "09efb3f9e13df2a3785d18823f2cea68fd6599078b72c6fcdb58c4612280b984",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "8cefbbd676b5622ba5793e1fcb2a6f0891a9fcce19e38655b3000afec830d684",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "54144d7de327acadc6ae2d5d14fd332489d75d5c3b46dd6966f6c1e2d5346e37",
            "kirocrew-mate-worker.json": "02c1125e4c7025f792be99a918adeb88aba796639ef0271829cb9c66701630b9",
            "kirocrew-pipeline-conductor.json": "e86640635f9de15be807a50969b8177bad674e79a2795a38ebb50c9eeadf74c8",
            "kirocrew-research.json": "d4ffdd4f5f4da16c397574c3b32542c8d13344a35c5f5af9c1ef413ab398dac2",
            "kirocrew-security-conductor.json": "cbafae307d15bd2ef9c312453d4c3fe6f8f82efd0b6b668f1056c44456b21016",
            "kirocrew-team-lead.json": "e3360b2bc8fb19238c667a1ac43896b6e4639a067a885aafc21b6558b7825a96",
            "kirocrew-worker.json": "23cc13e6537856f540b2035a229c3749bcfbdae0bf798176fe4842448017355b",
            "kirocrew.json": "ec57e3ffe4b1283c10aa9d4f8649dd84245566dbb9596b6f46ef6b438a2da9da",
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
            "kirocrew-conductor.json": "ffdb4785aa31b0115063a004846f98cd4ae5213897f91f70856aa8efdd228f39",
            "kirocrew-dashboard-author.json": "8aee5dfc36146bc4a9f2e6b10eaa6a72354246b60bfe1ef0497129ca276eaf49",
            "kirocrew-dashboard-manager.json": "403a3e755e548b1a425d535ffe9e78e9eae066cbadc60a84a3f498a53c4333f6",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "8cefbbd676b5622ba5793e1fcb2a6f0891a9fcce19e38655b3000afec830d684",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "660ee4c2408e1a2a6110406ccc684897a6351e7fdeb8ee7c60bd335155db2692",
            "kirocrew-mate-worker.json": "915c86639ee6d2fee51e568d467be4bd5e5cbb18ae4143effde3d7d6097cd326",
            "kirocrew-pipeline-conductor.json": "e86640635f9de15be807a50969b8177bad674e79a2795a38ebb50c9eeadf74c8",
            "kirocrew-research.json": "2164681efac61d047b467101c1ad2e29baf71e325b09661841aac9f90fc89f62",
            "kirocrew-security-conductor.json": "cbafae307d15bd2ef9c312453d4c3fe6f8f82efd0b6b668f1056c44456b21016",
            "kirocrew-team-lead.json": "66edd6e014d121dd9d8237bb502699cfbf7c08f583a43bc7fbbf494ac1237905",
            "kirocrew-worker.json": "ec0a4fcb2d83443c152879100ed99d23e4bbaeeaa96d009c2c4e8eabe4e37b49",
            "kirocrew.json": "a05ad55c22bef37f4e09507988bc6982bbdcd80e1eb6f304cb0a830b0aafacf3",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "governed": {
        "events": "666bce07d1879ed8008fc0f90b45ec22cd94b718c2a874548d42cd3315b6f5a9",
        "files": {
            "kirocrew-conductor.json": "e96553c4ad91a1ce53c76202f4f7152a9e6f75bd81d0d4131aa5483b184984b5",
            "kirocrew-dashboard-author.json": "194e669965a46fb03eac5ad4dfdcfc9f682f80f1292fa479785ab93cf45e511c",
            "kirocrew-dashboard-manager.json": "403a3e755e548b1a425d535ffe9e78e9eae066cbadc60a84a3f498a53c4333f6",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "2326f2631ed0265b758a7ff107035558c03228111682bc391ce641f0dd3c2c86",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "60d797fd70d67ff68f90a10210d7436adf43a03b35e520aaf8430b0b9294c13d",
            "kirocrew-mate-worker.json": "7aa8ce65903f8454b456ce17a81c527e0728e5f9e3b845fb6bd569c5c2834abb",
            "kirocrew-pipeline-conductor.json": "9400c5a9be49b1dc72b5169e7fea8f4203312636a55ca7647eb79b3399fad34c",
            "kirocrew-research.json": "4634ebe96321ddac989e1ecfd70986829ca0946e2e7143eb397b91e9a592b769",
            "kirocrew-security-conductor.json": "6ca5219fad6a82cd75fee6016755c81e5fca766ddda1220ac03378e3a6eb6360",
            "kirocrew-team-lead.json": "129ae2a5631749b6c551ed54ed1bf1232e896e1431ba6e7a1a130d082ba455de",
            "kirocrew-worker.json": "2b320f309e2bf0bd80490322a6f0f68507eecee051a3c6c836962d3240f35ec6",
            "kirocrew.json": "a4c71e1cf3c973bf8c74619019a4a7e059e1b5a84ef9f30c223f3ba3ab30fb99",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "939d5770910a05fc49ac6fff3da064f2ff08347839f39e1eadebc5fa964eb6b5",
        "unrefreshed": [],
    },
    "object_hooks": {
        "events": "00ad0b59a7575f63d3571802d1424b4146cc87a01e567590508ebd4832f572d1",
        "files": {
            "kirocrew-conductor.json": "7256d31148cd57e33fbd8469741ec9d22afc84f966bdb14a2684df3f45b37be7",
            "kirocrew-dashboard-author.json": "e8e23929204e16f79f76a7ce4a8d072e6e427769abf05da7cf3fae0f5baa35bb",
            "kirocrew-dashboard-manager.json": "403a3e755e548b1a425d535ffe9e78e9eae066cbadc60a84a3f498a53c4333f6",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "8fd65925d67dbda6dfdc72439f4c3fa431f21047d097ea4548a62b3850cf2134",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "bd7f18fc01fc130aba5ae093b569627e2e3ce6f7e22e686696ce6bd19a3573b0",
            "kirocrew-mate-worker.json": "61bd1b0e663161be52911f26a1f4d08f7621aa0ff6da5d7dbf017a0cbae17c15",
            "kirocrew-pipeline-conductor.json": "fa2c23eadd4556f5db718d04f4c59f3129fe3f4473a8e1ff9e18f8ac68e78cab",
            "kirocrew-research.json": "9861d54d369bd70211ab7753280dea66c388038bedadadc8969567df877efb94",
            "kirocrew-security-conductor.json": "d4dd227efda865d5e8ef37d4eb6d1a385d8ad176f21cad1cda66dafe345f97f2",
            "kirocrew-team-lead.json": "31879426c9615f17b503cfb3775b47620163bfeb2f03974b8bbb78e049163a8c",
            "kirocrew-worker.json": "46e806aebe904f77cb819d23f8abc62c7b345d72c7e7204b9ba2b5fac056400b",
            "kirocrew.json": "d8e092b2b75db63c216294f873430f6f5dfad87b1f75eef76a964c2c96a073ee",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "registry_mode": {
        "events": "93c96177077b06656eaa1c26e81098e451c2a5dcae0d1966b8194e339fec1011",
        "files": {
            "kirocrew-conductor.json": "74d8b6fdd54f4e7e8508c334b93414815c8e0e1897dcc93efc1c78f35ce992f8",
            "kirocrew-dashboard-author.json": "5da8c34ed50bb51060668577187f8c40d668b5d7561794dc33760313818cc24c",
            "kirocrew-dashboard-manager.json": "432e246d4f3d02f0fd466e3024187a1ad230a4d98f409a4e297dae3af0a6cea6",
            "kirocrew-guest.json": "fcaaecb0023ee4dde10ac4b91acecc02183aa56cfa43b1478cdb103ac9bbc5aa",
            "kirocrew-heartbeat.json": "fe622d21c28e2d592ad3b50cd7d6d602a6b3a4be3c257fc3249d6664abd49d1f",
            "kirocrew-knowledge.json": "69dd6a07083e0060eb8dc1c8bcbbe6a080b4e57aeca4648ce0f421a5a07a1f49",
            "kirocrew-ledger-conductor.json": "12db19db3c76fdff85b134ff44b69b9c06a055c969a366aa70d9b8f6ebaa797c",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "f8185261318c76ecba1dee799b1be95ea12a49bcbefdf03ecb9ed5fab284f6e2",
            "kirocrew-mate-worker.json": "ac47b79b057cb21fb4b444e936fee19dd6031825c1e585de3712b9f746f93ba6",
            "kirocrew-pipeline-conductor.json": "c32eaca526a0b2f467b0e0a94822efeedbb635e8d2abc3aeee65abe179c34adc",
            "kirocrew-research.json": "94d717179e489a77db57bc390f03845583163e511ac3d29609f6ff0c2b86e855",
            "kirocrew-security-conductor.json": "81c39ac499b40e9b3d45a1c9259c123d7f71a7917f545433f70d2abdebd9f5e0",
            "kirocrew-team-lead.json": "11835bd6fb6f235a3e5f4fa1b379aee16ed979a08e8aca26adb160da37eb394a",
            "kirocrew-worker.json": "d0793144e27f710a7f0aece5d29394700fb543db0bc6c79e6f3f9ba7d8025d4b",
            "kirocrew.json": "8c378d3a8804b7a7fa3dba1ad38c17ef54b68497762d20ca71ea4a0a46b53ef6",
            "kirocrew.lock": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        "state": "d704db8fc2311b53ecec035ace4d3145224e00ab26bbedeb0a2e6977a7c16aac",
        "unrefreshed": [],
    },
    "user_hooks": {
        "events": "77c888bf115c62f26548733291ed78336f6421baf700190ea8e44a8c1a3c6d41",
        "files": {
            "kirocrew-conductor.json": "80f8200fb65dff78183e858d2d97a77983057fe3f20a2af74134426d666810a5",
            "kirocrew-dashboard-author.json": "89c1aad0eecd6a09624e12f912b5a05396b282e10465f2cd6dcd5bc593ad333e",
            "kirocrew-dashboard-manager.json": "403a3e755e548b1a425d535ffe9e78e9eae066cbadc60a84a3f498a53c4333f6",
            "kirocrew-guest.json": "f80d828a806d2b1db0ad3d6834b39b3bc4dc44597d513235b7e14e689a2cff92",
            "kirocrew-heartbeat.json": "4b803e0d1fbd47ca8a757e3e45c5a2694d8acd591034ba1ced2750a43a2dd3ac",
            "kirocrew-knowledge.json": "233c8529fd60b101f8a381a943147701921932103147c4b8472579c92037a825",
            "kirocrew-ledger-conductor.json": "e51d42c4ab7a7a20d8c7e42a6da38f73cfbe6751af9cedca4522329d7679dc3a",
            "kirocrew-lite.json": "c24c577f0102ce20ccdd15f28e045888356e3a36db3e5646d86f02f62ca38612",
            "kirocrew-mate-conductor.json": "9524adc7e39cf5dc40d60685a2f6380a8a361c248de3a7af8a1f9f3ea2b0f3ee",
            "kirocrew-mate-worker.json": "96fb5627787efcaa5dc6e450d699df533bcfba238921d2751091202c1c8169c6",
            "kirocrew-pipeline-conductor.json": "66a98d2d2c3c04bb8c6a42c93d5a903c7733d2ad893be32e1216b9d8bf2e63cb",
            "kirocrew-research.json": "7fac6930c96c20ddf6b037456e6e5f7d0177c601f04348abcddfbc63187ce0fa",
            "kirocrew-security-conductor.json": "d816e4748831cc9ce614fb23eecbe0c90e097965f3cd5ed37e31830ef83e1ecd",
            "kirocrew-team-lead.json": "f1dc2bfc98344a6444f20f69776df66eabd23450cae82f18dd2b75a1db0c0902",
            "kirocrew-worker.json": "6353f08035054fe67ccf67080da4e3197fe6c203ca6394bb80962a6a5f97ae02",
            "kirocrew.json": "9004dfed772b9a6223bb66f908dd40c5435a8071b924478b3c16fad7735929ab",
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
