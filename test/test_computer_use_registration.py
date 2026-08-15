"""Managed-MCP registration for ``kirocrew-computer`` — and the parity tax.

Adding a managed MCP server touches EIGHT places, and every one of them is a
place a future author can forget. The second half of this file exists to pay that
debt down permanently: ``test_managed_server_name_appears_in_every_registry``
asserts the name is present in each registry, and — the stronger form — that
``agent._MANAGED_MCP_SERVERS`` and ``mcp_discovery._MANAGED_SERVER_SUBCOMMANDS``
are the SAME set, so the NEXT managed server cannot ship half-registered either.
No such test existed before this feature.

Two properties are security-critical rather than merely tidy:

* **The managed spec carries NO ``autoApprove`` key.** ``mcp_gateway/rewriter.py``
  keeps ``autoApprove`` on the wrapper precisely so kiro-cli reads it, and an
  autoApproved MCP tool is approved LOCALLY by kiro-cli: it emits no permission
  request and therefore **never reaches ``hooks.on_tool_call``**. Auto-approving
  ``computer_click`` would silently delete the entire PreToolUse plane for this
  feature.
* **A fresh install adds ``@kirocrew-computer`` to ``tools`` but NOT to
  ``allowedTools``.** ``agent.py`` documents the rule ("new MCPs may have
  destructive tools; user opts in"), and ``config/defaults.json`` blanket-allows
  ``@kirocrew-core`` — which is exactly why computer use is its own server rather
  than riding inside that one.

Every test patches ``kiro_crew.agent``'s module globals with the
``patch.multiple`` + ``ExitStack`` idiom from ``test_agent.py``, so the real
``~/.kiro`` is never read or written.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import agent, agent_state, mcp_cleanup, mcp_discovery, onboarding_import
from kiro_crew.agent import install_agent
from kiro_crew.platform_compat import IS_POSIX

CU_SERVER = "kirocrew-computer"
CU_REF = f"@{CU_SERVER}"
CU_SUBCOMMAND = "mcp-computer"

# The managed set as the tests stage it: the two pre-existing servers plus ours,
# each in the real ``invocation_fn`` shape so ``build_agent_config`` and
# ``_refresh_dynamic_fields`` take their production code paths.
_MANAGED = {
    "kirocrew-cron": {"invocation_fn": lambda: ("/usr/bin/kirocrew", ["mcp-cron"])},
    "kirocrew-core": {"invocation_fn": lambda: ("/usr/bin/kirocrew", ["mcp-core"])},
    CU_SERVER: {"invocation_fn": lambda: ("/usr/bin/kirocrew", [CU_SUBCOMMAND])},
}

# The same set with the REAL gate on the computer-use row. ``_MANAGED`` above
# deliberately carries none, so the pre-existing emission tests keep exercising
# the ungated path; the gate's own tests stage this one.
_GATED_MANAGED = {
    "kirocrew-cron": {"invocation_fn": lambda: ("/usr/bin/kirocrew", ["mcp-cron"])},
    "kirocrew-core": {"invocation_fn": lambda: ("/usr/bin/kirocrew", ["mcp-core"])},
    CU_SERVER: {
        "invocation_fn": lambda: ("/usr/bin/kirocrew", [CU_SUBCOMMAND]),
        "spec_gate": agent._computer_use_spec_gate,
    },
}


def _bundled_defaults(tmp_path: Path) -> Path:
    """Write a minimal bundled ``defaults.json`` and return its directory.

    Mirrors the shipped file's shape for the keys under test: ``@kirocrew-computer``
    in ``tools`` and deliberately absent from ``allowedTools``.
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "defaults.json").write_text(
        json.dumps(
            {
                "model": "claude-default",
                "tools": ["ReadFile", "@kirocrew-cron", "@kirocrew-core", CU_REF],
                "allowedTools": ["ReadFile", "@kirocrew-core"],
                "mcpServers": {},
                "hooks": {"preToolUse": "audit"},
            }
        )
    )
    (cfg_dir / "prompt.md").write_text("system prompt")
    return cfg_dir


def _run_install(tmp_path: Path, cfg_dir: Path, *, managed: "dict | None" = None, **kwargs) -> Path:
    """Run ``install_agent`` with every module global redirected into *tmp_path*.

    The ``patch.multiple`` + ``ExitStack`` shape from ``test_agent.py``. This is
    what keeps a test from touching the developer's real ``~/.kiro/agents`` — which
    for THIS feature would mean writing a computer-use MCP entry into their live
    agent config.

    *managed* overrides the staged managed-server set. It defaults to ``_MANAGED``
    (no ``spec_gate``, so the emission tests below exercise the ungated path);
    ``TestSpecEmissionGate`` passes the gated shape.
    """
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    mc_config = tmp_path / "mc_config.json"
    if not mc_config.exists():
        mc_config.write_text(json.dumps({"agent": {"kiro_hooks_autoimport": False}}))

    patches = [
        patch.multiple(
            "kiro_crew.agent",
            KIRO_AGENTS_DIR=kiro_dir,
            _BUNDLED_CFG_DIR=cfg_dir,
            _KIROCREW_BIN="/usr/bin/kirocrew",
            _MANAGED_MCP_SERVERS=_MANAGED if managed is None else managed,
            _KIRO_MCP_JSON=tmp_path / "fake_kiro_mcp.json",
            _CC_MCP_JSON=tmp_path / "fake_cc_mcp.json",
        ),
        patch("kiro_crew.agent._user_dir", lambda: tmp_path / "home"),
        patch("kiro_crew.agent._prompt_path", return_value=cfg_dir / "prompt.md"),
        patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json"),
        patch("kiro_crew.agent._project_dir", return_value=None),
        patch("kiro_crew.agent._aim_skill_paths", return_value=[]),
        patch("kiro_crew.agent.shutil.which", side_effect=lambda c, **kw: c),
        patch("kiro_crew.agent._mc_config_path", return_value=mc_config),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return install_agent(**kwargs)


def _installed(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── build_agent_config ──


def test_managed_spec_is_built_with_a_resolvable_command(tmp_path: Path):
    """``build_agent_config`` emits a command + args kiro-cli can spawn."""
    cfg_dir = _bundled_defaults(tmp_path)
    with ExitStack() as stack:
        stack.enter_context(
            patch.multiple(
                "kiro_crew.agent",
                _BUNDLED_CFG_DIR=cfg_dir,
                _MANAGED_MCP_SERVERS=_MANAGED,
            )
        )
        stack.enter_context(
            patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json")
        )
        stack.enter_context(
            patch("kiro_crew.agent._prompt_path", return_value=cfg_dir / "prompt.md")
        )
        stack.enter_context(patch("kiro_crew.agent._project_dir", return_value=None))
        stack.enter_context(
            patch("kiro_crew.agent._mc_config_path", return_value=tmp_path / "none.json")
        )
        config = agent.build_agent_config()
    spec = config["mcpServers"][CU_SERVER]
    assert spec["command"] == "/usr/bin/kirocrew"
    assert spec["args"] == [CU_SUBCOMMAND]


def test_managed_spec_contains_no_auto_approve(tmp_path: Path):
    """**The managed spec must carry NO ``autoApprove`` key.**

    An autoApproved MCP tool is approved LOCALLY by kiro-cli: it emits no
    permission request and therefore never reaches ``hooks.on_tool_call``, so the
    entire PreToolUse plane — the deny floor, the governance gate, the
    approval-floor clamp — would be skipped for every computer-use call.

    Asserted on the source-of-truth dict as well as the built config, because the
    hole would be introduced by adding the key to the dict.
    """
    assert "autoApprove" not in agent._MANAGED_MCP_SERVERS[CU_SERVER]

    cfg_dir = _bundled_defaults(tmp_path)
    path = _run_install(tmp_path, cfg_dir)
    assert "autoApprove" not in _installed(path)["mcpServers"][CU_SERVER]


def test_real_managed_entry_uses_the_shared_invocation_resolver():
    """The shipped row resolves its command through ``_kirocrew_mcp_invocation``.

    Not a hardcoded path: that helper is the single source of truth for every
    install layout (a console script when one resolves, otherwise
    ``<interpreter> -m kiro_crew``), and the permission-probe shell-out in the
    dashboard handler reuses it so the probe runs the SAME install as the gateway.
    """
    spec = agent._MANAGED_MCP_SERVERS[CU_SERVER]
    assert "invocation_fn" in spec
    command, args = spec["invocation_fn"]()
    assert command
    assert args[-1] == CU_SUBCOMMAND


# ── fresh install ──


def test_fresh_install_adds_the_ref_to_tools_but_not_allowed_tools(tmp_path: Path):
    """**``tools`` yes, ``allowedTools`` no.**

    ``agent.py`` states the rule: "new MCPs may have destructive tools; user opts
    in". This is also the whole reason computer use is its own server —
    ``config/defaults.json`` blanket-allows ``@kirocrew-core``, so riding inside it
    would have inherited that auto-approve for ``computer_click``.
    """
    cfg_dir = _bundled_defaults(tmp_path)
    path = _run_install(tmp_path, cfg_dir)
    config = _installed(path)
    assert CU_REF in config["tools"]
    assert CU_REF not in config["allowedTools"]
    # And no per-tool grant sneaked in either.
    assert not [t for t in config["allowedTools"] if t.startswith(f"{CU_REF}/")]


def test_shipped_defaults_grant_tools_only():
    """The real ``config/defaults.json`` matches the rule (not just the fixture)."""
    shipped = json.loads(
        (Path(agent.__file__).parent / "config" / "defaults.json").read_text(encoding="utf-8")
    )
    assert CU_REF in shipped["tools"]
    assert CU_REF not in shipped["allowedTools"]
    assert not [t for t in shipped["allowedTools"] if t.startswith(f"{CU_REF}/")]


def test_fresh_install_registers_the_server(tmp_path: Path):
    cfg_dir = _bundled_defaults(tmp_path)
    config = _installed(_run_install(tmp_path, cfg_dir))
    assert CU_SERVER in config["mcpServers"]


# ── refresh of an existing config ──


def _existing_config(tmp_path: Path, cu_spec: dict) -> Path:
    """Seed an existing ``kirocrew.json`` whose computer-use entry is *cu_spec*."""
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    path = kiro_dir / "kirocrew.json"
    path.write_text(
        json.dumps(
            {
                "model": "claude-user-custom",
                "tools": ["ReadFile"],
                "allowedTools": ["ReadFile"],
                "mcpServers": {CU_SERVER: cu_spec},
                "hooks": {"preToolUse": "audit"},
            }
        )
    )
    return path


def test_refresh_re_resolves_a_stale_command(tmp_path: Path):
    """A path from a previous install is re-resolved on every refresh."""
    cfg_dir = _bundled_defaults(tmp_path)
    _existing_config(tmp_path, {"command": "/old/dead/path/kirocrew", "args": [CU_SUBCOMMAND]})
    config = _installed(_run_install(tmp_path, cfg_dir))
    assert config["mcpServers"][CU_SERVER]["command"] == "/usr/bin/kirocrew"
    assert config["mcpServers"][CU_SERVER]["args"] == [CU_SUBCOMMAND]


def test_refresh_strips_a_stale_remote_transport(tmp_path: Path):
    """A leftover ``url``/``headers`` from an older build must not shadow the command.

    These servers are stdio-only; a surviving ``url`` propagates into the CC config
    and takes precedence, so the shim would never be spawned at all.
    """
    cfg_dir = _bundled_defaults(tmp_path)
    _existing_config(
        tmp_path,
        {
            "command": "/usr/bin/kirocrew",
            "args": [CU_SUBCOMMAND],
            "url": "http://127.0.0.1:9999/mcp",
            "headers": {"X-Stale": "1"},
        },
    )
    spec = _installed(_run_install(tmp_path, cfg_dir))["mcpServers"][CU_SERVER]
    assert "url" not in spec
    assert "headers" not in spec


def test_refresh_preserves_a_user_added_auto_approve(tmp_path: Path):
    """A user's OWN ``autoApprove`` survives a refresh.

    The managed spec must never SEED it (the test above), but a user who added it
    deliberately owns that decision and a refresh must not silently revert their
    config. The two rules are independent, and both matter.
    """
    cfg_dir = _bundled_defaults(tmp_path)
    _existing_config(
        tmp_path,
        {
            "command": "/usr/bin/kirocrew",
            "args": [CU_SUBCOMMAND],
            "autoApprove": [f"{CU_SERVER}/computer_get_state"],
        },
    )
    spec = _installed(_run_install(tmp_path, cfg_dir))["mcpServers"][CU_SERVER]
    assert spec["autoApprove"] == [f"{CU_SERVER}/computer_get_state"]


def test_refresh_does_not_add_the_ref_to_allowed_tools(tmp_path: Path):
    """An EXISTING config never gains an ``allowedTools`` grant on refresh.

    ``allowedTools`` is kiro-cli's blanket auto-approve list, and an auto-approved
    MCP tool is approved LOCALLY by kiro-cli: it emits no permission request and
    therefore never reaches ``hooks.on_tool_call`` — so the deny floor, the
    governance ceiling and the approval clamp would all be skipped. A refresh may
    make the tools *reachable* (the ``tools`` entry, asserted below); it may never
    pre-approve them.
    """
    cfg_dir = _bundled_defaults(tmp_path)
    _existing_config(tmp_path, {"command": "/usr/bin/kirocrew", "args": [CU_SUBCOMMAND]})
    config = _installed(_run_install(tmp_path, cfg_dir))
    assert CU_REF not in config["allowedTools"]
    assert not [t for t in config["allowedTools"] if t.startswith(f"{CU_REF}/")]


def test_refresh_adds_the_tools_ref_to_an_upgrading_config(tmp_path: Path):
    """**An UPGRADING install must gain ``@kirocrew-computer`` in ``tools``.**

    The fresh-install branch that registers managed refs runs only when the config
    is being CREATED, so without an add-only exception on the refresh path a
    pre-existing user ends up with the server in ``mcpServers`` and none of its 9
    tools exposed by kiro-cli — the feature registers and then silently does
    nothing, for every upgrading user (i.e. everyone). Unlike a third-party MCP the
    user cannot have opted out of a ref that never existed on their install.

    Gaining the ref is NOT the feature turning itself on: the primary enable lives in
    the keystone ``computer_use.json`` the agent can neither read nor write, and the
    shim answers an empty ``tools/list`` until the user opts in from Settings.
    """
    cfg_dir = _bundled_defaults(tmp_path)
    path = _existing_config(tmp_path, {"command": "/usr/bin/kirocrew", "args": [CU_SUBCOMMAND]})
    assert CU_REF not in _installed(path)["tools"]  # the pre-upgrade state
    config = _installed(_run_install(tmp_path, cfg_dir))
    assert CU_REF in config["tools"]
    # And the add is tools-ONLY (the security half of the same change).
    assert CU_REF not in config["allowedTools"]
    # The user's own tools are preserved — this is add-only, never a rewrite.
    assert "ReadFile" in config["tools"]


def test_refresh_does_not_re_add_other_managed_refs(tmp_path: Path):
    """The upgrade exception is scoped to computer use alone.

    ``tools``/``allowedTools`` remain user-owned for every other server, so a user
    who removed ``@kirocrew-cron`` does not get it silently restored. Scoping the
    carve-out is what keeps "the user controls their tool lists" true.
    """
    cfg_dir = _bundled_defaults(tmp_path)
    _existing_config(tmp_path, {"command": "/usr/bin/kirocrew", "args": [CU_SUBCOMMAND]})
    config = _installed(_run_install(tmp_path, cfg_dir))
    assert "@kirocrew-cron" not in config["tools"]
    assert "@kirocrew-core" not in config["tools"]


def test_refresh_respects_a_template_that_drops_computer_use(tmp_path: Path):
    """No ref is added when the shipped template does not grant it.

    The add is gated on the bundled ``defaults.json``, so an edition that ships
    without computer use is not force-fed the ref by the refresh path.
    """
    cfg_dir = _bundled_defaults(tmp_path)
    defaults_path = cfg_dir / "defaults.json"
    defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    defaults["tools"] = [t for t in defaults["tools"] if t != CU_REF]
    defaults_path.write_text(json.dumps(defaults))

    _existing_config(tmp_path, {"command": "/usr/bin/kirocrew", "args": [CU_SUBCOMMAND]})
    config = _installed(_run_install(tmp_path, cfg_dir))
    assert CU_REF not in config["tools"]


def test_refresh_adds_a_missing_managed_server(tmp_path: Path):
    """An existing config from before the feature gains the server entry."""
    cfg_dir = _bundled_defaults(tmp_path)
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    (kiro_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "model": "m",
                "tools": ["ReadFile"],
                "allowedTools": ["ReadFile"],
                "mcpServers": {},
                "hooks": {"preToolUse": "audit"},
            }
        )
    )
    config = _installed(_run_install(tmp_path, cfg_dir))
    assert CU_SERVER in config["mcpServers"]
    assert "autoApprove" not in config["mcpServers"][CU_SERVER]


# ── the parity tax, paid down permanently ──


def test_managed_server_name_appears_in_every_registry():
    """The name is present in ALL of the registries a managed server must join.

    Enumerated explicitly because each omission fails DIFFERENTLY and none of them
    is loud: a missing ``mcp_cleanup`` entry leaves a stale binary path after an
    install-method change (and silently drops ``kirocrew doctor``'s probe, which
    imports the same tuple); a missing ``mcp_discovery`` entry makes "Discover &
    Sync" mis-resolve the command; a missing ``handlers/mcp.py`` entry hides the
    server from the dashboard's MCP list; a missing ``onboarding_import`` entry
    lets an imported config carry a foreign copy of it.
    """
    assert CU_SERVER in agent._MANAGED_MCP_SERVERS
    assert CU_SERVER in mcp_cleanup.KIROCREW_BIN_MCP_SERVERS
    assert mcp_discovery._MANAGED_SERVER_SUBCOMMANDS.get(CU_SERVER) == CU_SUBCOMMAND
    assert CU_SERVER in mcp_discovery._MANAGED_SERVER_NAMES
    assert CU_SERVER in onboarding_import._MANAGED_MCP_NAMES

    # The dashboard's builtin list is a literal inside ``api_mcp_active``'s body,
    # so it is asserted through the function's source rather than an importable
    # constant. Scoped to that ONE function, not the whole module, so an unrelated
    # mention elsewhere in the file could not make this pass vacuously.
    import inspect

    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    assert CU_SERVER in inspect.getsource(mcp_handlers.api_mcp_active)


def test_managed_sets_are_identical_across_agent_and_discovery():
    """**The stronger invariant**: the two managed sets must be the SAME set.

    A name-by-name test only protects the server it names. This one protects the
    NEXT managed server too — which is how the eight-site registration tax stops
    being a recurring cost.
    """
    assert set(agent._MANAGED_MCP_SERVERS) == set(mcp_discovery._MANAGED_SERVER_SUBCOMMANDS)


def test_cleanup_tuple_is_ordered_and_covers_every_managed_server():
    """``KIROCREW_BIN_MCP_SERVERS`` is a TUPLE (deterministic doctor order).

    ``cli_doctor`` iterates it to probe each server, so a set would make the
    doctor's output order vary between runs.
    """
    assert isinstance(mcp_cleanup.KIROCREW_BIN_MCP_SERVERS, tuple)
    assert set(mcp_cleanup.KIROCREW_BIN_MCP_SERVERS) == set(agent._MANAGED_MCP_SERVERS)


def test_stale_managed_set_includes_the_predecessor_brands():
    """The rename left ``meshclaw-``/``openclaw-`` copies behind in user configs.

    Following the existing three-brand pattern so an imported config's foreign
    computer-use entry is recognised as managed rather than preserved as a
    user-installed server.
    """
    assert "meshclaw-computer" in onboarding_import._MANAGED_MCP_NAMES
    assert "openclaw-computer" in onboarding_import._MANAGED_MCP_NAMES


def test_server_key_is_slash_free():
    """kiro-cli splits an agent ``@server`` reference on ``/``.

    A slash in the key would make ``@kirocrew-computer/...`` unparseable and the
    whole server unreachable.
    """
    for name in agent._MANAGED_MCP_SERVERS:
        assert "/" not in name


def test_cli_exposes_the_hidden_subcommand():
    """``kirocrew mcp-computer`` must parse and dispatch.

    Hidden (``argparse.SUPPRESS``) because it is spawned by the agent backend, not
    typed by a user — but if the parser lacks it, every managed spec points at a
    subcommand that exits with a usage error.
    """
    import inspect

    from kiro_crew import cli

    src = inspect.getsource(cli)
    assert f'sub.add_parser("{CU_SUBCOMMAND}"' in src
    assert f'args.command == "{CU_SUBCOMMAND}"' in src


def test_subcommand_matches_the_discovery_mapping():
    """The parser name, the managed spec's args and the discovery map must agree."""
    command, args = agent._MANAGED_MCP_SERVERS[CU_SERVER]["invocation_fn"]()
    assert args[-1] == mcp_discovery._MANAGED_SERVER_SUBCOMMANDS[CU_SERVER]
    assert command


def test_computer_use_sources_are_scrub_lint_clean():
    """Every computer-use source file passes the De-Amazon scrub-lint pattern.

    ``scripts/scrub-lint.sh`` is a BLOCKING CI job, and its ``INTERNAL_PATTERN``
    includes a bare ``\\.amazon\\.`` — which matches the product's own macOS bundle
    id (``com.amazon.kiro.crew``). That id is genuinely needed by the self-denylist
    (KiroCrew's dashboard can flip this feature's own primary enable, so driving our
    own window must be refused), so the fix is an anchored
    ``scripts/scrub-allowlist.txt`` entry for the one file that needs it — not
    deleting the denylist row and not broadening the pattern.

    This test exists because the scrub gate lives in a shell script that only runs
    as its own CI job: a Python-suite failure here surfaces the same problem in the
    fast per-commit gate, where it is one line to read instead of a red job at the
    end of a PR.
    """
    import re
    from pathlib import Path

    repo = Path(agent.__file__).resolve().parents[2]
    script = repo / "scripts" / "scrub-lint.sh"
    if not script.exists():  # pragma: no cover - python-only checkout
        pytest.skip("scrub-lint.sh not present in this checkout")

    # Read the pattern from the script itself rather than restating it: a copy here
    # would drift and start passing while the real gate failed.
    match = re.search(r"^INTERNAL_PATTERN='([^']+)'", script.read_text(encoding="utf-8"), re.M)
    assert match, "could not read INTERNAL_PATTERN out of scrub-lint.sh"
    pattern = re.compile(match.group(1))

    allowlist_path = repo / "scripts" / "scrub-allowlist.txt"
    allow_patterns = [
        line.strip()
        for line in (
            allowlist_path.read_text(encoding="utf-8").splitlines()
            if allowlist_path.exists()
            else []
        )
        if line.strip() and not line.startswith("#")
    ]

    # Globbed from the FILESYSTEM, not from ``git ls-files``: the real gate scans
    # the working tree, and an untracked-but-present file is exactly the state a
    # feature branch is in before its first ``git add`` — which is when this check
    # is most useful. Deriving the list from git would make the whole test pass
    # vacuously on zero files.
    sources = sorted((repo / "src" / "kiro_crew" / "computer_use").rglob("*.py"))
    mcp_shim = repo / "src" / "kiro_crew" / "mcp_computer.py"
    if mcp_shim.exists():
        sources.append(mcp_shim)
    assert sources, "no computer-use sources found — this check would pass vacuously"

    offenders: list[str] = []
    for path in sources:
        rel = path.relative_to(repo).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not pattern.search(line):
                continue
            hit = f"{rel}:{number}:{line}"
            # The real gate filters through the allowlist as ``grep -v`` patterns.
            if any(re.search(allowed, hit) for allowed in allow_patterns):
                continue
            offenders.append(hit)

    assert offenders == [], (
        "these lines trip the BLOCKING De-Amazon scrub-lint job; add an anchored "
        f"scripts/scrub-allowlist.txt entry for each: {offenders}"
    )


class TestGatedEntryIsNotPreserved:
    """**A withheld entry's user fields are deliberately NOT restored.**

    Retracting the entry is required, and the fields it carries (``autoApprove``,
    the user's own ``env`` keys) are genuinely lost — an off/on cycle resets them
    and the operator re-applies them. That is a real cost, accepted for a reason:
    the only place to stash them would be the ``agent_state`` sidecar, which is an
    ORDINARY file under the data home (not on ``security._CREW_SECRET_LEAVES``,
    not write-protected), so the agent can write it. A restored ``autoApprove``
    would then be an agent-authored auto-approve, and kiro-cli approves an
    auto-approved MCP tool LOCALLY — no permission request, so
    ``hooks.on_tool_call`` and its audit are never reached. For a server that can
    click and type into an authenticated application that is a self-granted gate
    bypass, which is exactly what this feature's own spec forbids.

    Losing an approval is the safe direction; restoring one from a file the agent
    can write is not.
    """

    @pytest.fixture(autouse=True)
    def _isolated_sidecar(self, tmp_path, monkeypatch):
        import kiro_crew.config.paths as paths

        monkeypatch.setattr(paths, "_resolved_home", None)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir(parents=True, exist_ok=True)
        yield
        monkeypatch.setattr(paths, "_resolved_home", None)

    @staticmethod
    def _refresh(cfg: dict, *, macos: bool) -> None:
        with ExitStack() as stack:
            stack.enter_context(
                patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=_GATED_MANAGED)
            )
            stack.enter_context(
                patch("kiro_crew.agent._prompt_path", return_value=Path("/tmp/p.md"))
            )
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", macos))
            if macos:
                stack.enter_context(
                    patch("kiro_crew.computer_use.enable_state.is_enabled", return_value=True)
                )
            agent._refresh_dynamic_fields(cfg)

    def _cfg(self) -> dict:
        return {
            "name": "kirocrew",
            "mcpServers": {
                CU_SERVER: {
                    "command": "/usr/bin/kirocrew",
                    "args": [CU_SUBCOMMAND],
                    "autoApprove": [f"{CU_SERVER}/computer_get_state"],
                    "env": {"MY_VAR": "keep"},
                }
            },
        }

    def test_an_off_then_ON_cycle_does_NOT_restore_autoApprove(self):
        """The security-relevant half: no auto-approve comes back by itself."""
        cfg = self._cfg()
        self._refresh(cfg, macos=False)
        assert CU_SERVER not in cfg["mcpServers"]

        self._refresh(cfg, macos=True)
        entry = cfg["mcpServers"][CU_SERVER]
        assert "autoApprove" not in entry, "an auto-approve grant came back on its own"
        assert "MY_VAR" not in entry.get("env", {})
        # Our own half is regenerated, so the server still works.
        assert entry["command"] == "/usr/bin/kirocrew"
        assert entry["args"] == [CU_SUBCOMMAND]

    def test_NOTHING_about_the_entry_reaches_the_sidecar(self):
        """Asserted on the file, because the shape is the control.

        The sidecar cannot express an entry field at all — there is no code that
        writes one and no key that would hold it — so a future author cannot
        re-introduce the bypass by passing a different argument.
        """
        cfg = self._cfg()
        self._refresh(cfg, macos=False)
        raw = json.dumps(agent_state._read())
        assert "autoApprove" not in raw
        assert "MY_VAR" not in raw

    def test_the_sidecar_has_no_entry_writer_at_all(self):
        """A ratchet on the surface, not just on one call path.

        Nothing about a withheld entry is stashed anywhere. If a future change
        re-adds an entry-field writer, this fails here rather than surfacing as a
        self-granted auto-approve in someone's spec.
        """
        assert not hasattr(agent_state, "set_parked_mcp_state")
        assert not hasattr(agent, "_park_gated_entry")
        assert not hasattr(agent, "_restore_parked_entry")
        assert not hasattr(agent, "_user_owned_entry_fields")


class TestGatedRefsAreLeftALONE:
    """**A withheld server keeps its ``@ref``. Nothing prunes it, so nothing has
    to restore it.**

    The entry is the whole control. A ``@server`` ref resolves against the agent's
    own ``mcpServers`` plus the global ``mcp.json``, so a ref whose entry was
    never emitted names nothing, mounts nothing, and spawns nothing — which is the
    entire point of the gate.

    Removing the ref as well looks tidier and is where every widening bug came
    from: the removed set is not reconstructible from the server name (a user can
    narrow ``tools`` to ONE ``@server/tool`` ref, and the re-enable path re-adds
    the BARE ref), so anything that prunes must also preserve, and every failure
    mode of that preservation — an unwritable stash, a stash cleared before the
    write landed, a rebuild path that skipped the restore — silently WIDENS what
    is mounted. Leaving the ref alone has none of those states.

    The assertions below are therefore about what does NOT happen to the spec, and
    the ratchet at the end is on the module surface so the machinery cannot come
    back one helper at a time.
    """

    @pytest.fixture(autouse=True)
    def _isolated_sidecar(self, tmp_path, monkeypatch):
        import kiro_crew.config.paths as paths

        monkeypatch.setattr(paths, "_resolved_home", None)
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir(parents=True, exist_ok=True)
        yield
        monkeypatch.setattr(paths, "_resolved_home", None)

    @staticmethod
    def _refresh(cfg: dict, *, macos: bool) -> None:
        with ExitStack() as stack:
            stack.enter_context(
                patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=_GATED_MANAGED)
            )
            stack.enter_context(
                patch("kiro_crew.agent._prompt_path", return_value=Path("/tmp/p.md"))
            )
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", macos))
            if macos:
                stack.enter_context(
                    patch("kiro_crew.computer_use.enable_state.is_enabled", return_value=True)
                )
            agent._refresh_dynamic_fields(cfg)

    def test_a_NARROWED_ref_survives_an_off_on_cycle_byte_for_byte(self):
        """The defect class this replaces, asserted end to end.

        One per-tool ref, a full disable, a full re-enable. If anything strips and
        re-adds refs, the bare ``@kirocrew-computer`` appears here and the user who
        chose exactly one tool silently gets all of them.
        """
        narrowed = f"{CU_REF}/computer_get_state"
        cfg = {
            "name": "kirocrew",
            "tools": ["ReadFile", narrowed],
            "allowedTools": [narrowed],
            "mcpServers": {CU_SERVER: {"command": "/usr/bin/kirocrew", "args": [CU_SUBCOMMAND]}},
        }

        def _cu_refs() -> list:
            # Scoped to THIS server: the refresh legitimately adds unrelated
            # builtins, and only the computer-use refs are the widening oracle.
            return [
                t
                for t in cfg["tools"]
                if isinstance(t, str) and (t == CU_REF or t.startswith(f"{CU_REF}/"))
            ]

        self._refresh(cfg, macos=False)
        assert CU_SERVER not in cfg["mcpServers"], "the entry must be withheld"
        assert _cu_refs() == [narrowed], "the gate took the user's narrowed ref away"
        assert cfg["allowedTools"] == [narrowed]
        assert "ReadFile" in cfg["tools"]

        self._refresh(cfg, macos=True)
        assert _cu_refs() == [narrowed], (
            "the mounted tool set WIDENED across an off/on cycle: "
            f"expected only {narrowed!r}, got {_cu_refs()!r}"
        )
        assert cfg["allowedTools"] == [narrowed]
        assert CU_SERVER in cfg["mcpServers"], "re-enabling must emit the entry again"

    def test_the_ref_remains_while_the_entry_is_withheld(self):
        """Ref-without-entry is the intended steady state, not a leak.

        Stated as its own test because it is the claim the whole design rests on:
        the ref is inert, so the withheld entry alone is what stops the process
        from being spawned.
        """
        cfg = {
            "name": "kirocrew",
            "tools": [CU_REF],
            "mcpServers": {CU_SERVER: {"command": "/usr/bin/kirocrew", "args": [CU_SUBCOMMAND]}},
        }
        self._refresh(cfg, macos=False)
        assert CU_REF in cfg["tools"]
        assert CU_SERVER not in cfg["mcpServers"]

    def test_a_fresh_build_also_keeps_the_shipped_ref(self):
        """The derived agents take this dict as-is, so it must not diverge."""
        with ExitStack() as stack:
            stack.enter_context(
                patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=_GATED_MANAGED)
            )
            stack.enter_context(
                patch("kiro_crew.agent._prompt_path", return_value=Path("/tmp/p.md"))
            )
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", False))
            cfg = agent.build_agent_config()
        if CU_REF in agent.get_shipped_tools().get("tools", []):
            assert CU_REF in cfg.get("tools", []), "a fresh build stripped the shipped ref"
        assert CU_SERVER not in cfg.get("mcpServers", {})

    def test_no_park_machinery_exists_anywhere(self):
        """A ratchet on the surface, not on one call path.

        Every widening bug in this area came from code that removed a ref and then
        owed the user a restoration. If any of it returns, this fails here rather
        than as a silently widened mount in someone's spec.
        """
        for gone in (
            "_prune_gated_managed_refs",
            "_park_gated_refs",
            "_restore_parked_refs",
            "_clear_parked_refs",
        ):
            assert not hasattr(agent, gone), f"{gone} came back; refs must be left alone"
        for gone in ("get_parked_mcp_refs", "set_parked_mcp_refs"):
            assert not hasattr(agent_state, gone), f"{gone} came back"
        assert "parked_mcp_refs" not in json.dumps(agent_state._read())


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])


# ── The spec-emission gate ──


class TestSpecEmissionGate:
    """The server must not reach the EMITTED spec when it cannot be used.

    The two in-process checks (``_list_tools`` and the dispatcher) run inside a
    process the spec already caused kiro-cli to spawn, so they can only make a
    disabled feature advertise zero tools — they cannot make it cost nothing. It
    cost ~109 MB per chat process, including every ``spawn_run`` subagent, and on
    Linux/Windows it cost that for a capability with no driver at all.

    So these assertions are on the SPEC, not on ``tools/list``: that is the
    difference the fix is about, and a ``tools/list``-only test passes just as
    well while the process is still being spawned. The existing ``tools/list``
    tests stay — this gate is defence in depth's partner, not its replacement.
    """

    # The real gate, unlike the ``_MANAGED`` fixture above (which deliberately
    # carries none so the pre-existing emission tests keep exercising the
    # ungated path).
    _GATED = _GATED_MANAGED

    @staticmethod
    def _build(tmp_path: Path, *, macos: bool, keystone: "dict | None") -> dict:
        """Build a fresh spec with *macos* and *keystone* in effect.

        The keystone is written to a real temp file and read through the real
        ``enable_state``, so the four malformed spellings below are judged by the
        production predicate rather than by a stub that could disagree with it.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        state_path = tmp_path / "computer_use.json"
        if keystone is not None:
            state_path.write_text(json.dumps(keystone), encoding="utf-8")

        with ExitStack() as stack:
            stack.enter_context(
                patch.multiple(
                    "kiro_crew.agent",
                    _BUNDLED_CFG_DIR=cfg_dir,
                    _MANAGED_MCP_SERVERS=TestSpecEmissionGate._GATED,
                )
            )
            stack.enter_context(
                patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json")
            )
            stack.enter_context(
                patch("kiro_crew.agent._prompt_path", return_value=cfg_dir / "prompt.md")
            )
            stack.enter_context(patch("kiro_crew.agent._project_dir", return_value=None))
            stack.enter_context(
                patch("kiro_crew.agent._mc_config_path", return_value=tmp_path / "none.json")
            )
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", macos))
            stack.enter_context(
                patch(
                    "kiro_crew.computer_use.enable_state.computer_use_state_path",
                    return_value=state_path,
                )
            )
            return agent.build_agent_config()

    def test_a_non_darwin_platform_gets_no_entry(self, tmp_path: Path):
        """**The Linux/Windows case — the one with no driver at all.**

        Asserted with the keystone ENABLED, so the platform half of the gate is
        proven on its own rather than passing because the feature happened to be
        off too. ``select_default_backend`` has no driver here, so the spawned
        process could never have done anything.
        """
        config = self._build(tmp_path, macos=False, keystone={"enabled": True})
        assert CU_SERVER not in config["mcpServers"]
        # The always-on servers are untouched — this is one gate, not a purge.
        assert "kirocrew-core" in config["mcpServers"]
        assert "kirocrew-cron" in config["mcpServers"]

    @pytest.mark.parametrize(
        "keystone",
        [
            None,  # file absent
            {},  # present, empty
            {"enabled": False},
            {"enabled": "true"},  # truthy NON-True: a hand-edited string
            {"enabled": 1},  # truthy NON-True: a hand-edited int
        ],
        ids=["absent", "empty", "false", "string-true", "int-one"],
    )
    def test_darwin_with_the_keystone_off_gets_no_entry(self, tmp_path: Path, keystone):
        """Mirrors ``test_mcp_computer.py``'s keystone cases, one layer earlier.

        The truthy-non-``True`` spellings matter for the same reason they matter
        in ``enable_state.is_enabled``: a hand-edited ``"enabled": "false"`` is a
        truthy string, and reading it generously would spawn a desktop-control
        backend the operator never enabled.
        """
        config = self._build(tmp_path, macos=True, keystone=keystone)
        assert CU_SERVER not in config["mcpServers"]

    def test_darwin_with_the_keystone_ON_gets_the_entry(self, tmp_path: Path):
        """**Guard against over-gating the one path that must work.**

        Without this, "no entry" could be satisfied by a gate that is simply
        always closed — which would ship the feature dead on its own platform.
        """
        config = self._build(tmp_path, macos=True, keystone={"enabled": True})
        assert config["mcpServers"][CU_SERVER]["args"] == [CU_SUBCOMMAND]
        assert CU_REF in config["tools"]

    def test_the_withheld_entry_is_what_stops_the_spawn_not_the_ref(self, tmp_path: Path):
        """The ``mcpServers`` entry carries the command, so withholding it is enough.

        A ``@server`` ref resolves against the agent's own ``mcpServers`` plus the
        global ``mcp.json``; with no entry in either there is nothing to launch, so
        the surviving ref names nothing and mounts nothing. Asserted here because
        the alternative — stripping the ref too — is what made a narrowed grant
        unreconstructible and silently widened it on re-enable.
        """
        config = self._build(tmp_path, macos=False, keystone={"enabled": True})
        assert CU_SERVER not in config["mcpServers"], "the entry is the control"
        assert CU_REF in config["tools"], "the user's ref must survive the withhold"
        # Scoped: the always-on servers are untouched, entry and ref alike.
        assert "kirocrew-core" in config["mcpServers"]
        assert "@kirocrew-core" in config["tools"]

    def test_an_override_file_cannot_smuggle_the_entry_past_the_gate(self, tmp_path: Path):
        """A user override naming the server does not defeat a PLATFORM gate.

        ``build_agent_config`` merges the user override file over shipped
        defaults, so a ``continue`` alone would let an entry arriving from there
        reach the spec — spawning a backend on an OS where it has no driver.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        override = tmp_path / "override.json"
        override.write_text(
            json.dumps({"mcpServers": {CU_SERVER: {"command": "/x", "args": ["y"]}}}),
            encoding="utf-8",
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch.multiple(
                    "kiro_crew.agent",
                    _BUNDLED_CFG_DIR=cfg_dir,
                    _MANAGED_MCP_SERVERS=self._GATED,
                )
            )
            stack.enter_context(
                patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json")
            )
            stack.enter_context(
                patch("kiro_crew.agent._user_overrides_path", return_value=override)
            )
            stack.enter_context(
                patch("kiro_crew.agent._prompt_path", return_value=cfg_dir / "prompt.md")
            )
            stack.enter_context(patch("kiro_crew.agent._project_dir", return_value=None))
            stack.enter_context(
                patch("kiro_crew.agent._mc_config_path", return_value=tmp_path / "none.json")
            )
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", False))
            config = agent.build_agent_config()
        assert CU_SERVER not in config["mcpServers"]

    def test_a_refresh_RETRACTS_an_entry_written_while_the_gate_was_open(self):
        """Turning the feature OFF must reclaim the process turning it on started.

        A refresh that only skipped would leave the entry an earlier pass wrote,
        so the backend would keep being spawned for a disabled feature — the bug,
        one release later and harder to see.
        """
        cfg = {
            "mcpServers": {
                CU_SERVER: {"command": "/usr/bin/kirocrew", "args": [CU_SUBCOMMAND]},
                "kirocrew-core": {"command": "/usr/bin/kirocrew", "args": ["mcp-core"]},
            },
            "tools": ["ReadFile", CU_REF, "@kirocrew-core"],
            "allowedTools": [f"{CU_REF}/computer_get_state"],
        }
        with ExitStack() as stack:
            stack.enter_context(
                patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=self._GATED)
            )
            stack.enter_context(
                patch("kiro_crew.agent._prompt_path", return_value=Path("/tmp/p.md"))
            )
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", False))
            agent._refresh_dynamic_fields(cfg)
        assert CU_SERVER not in cfg["mcpServers"]
        assert "kirocrew-core" in cfg["mcpServers"]
        # Withholding the ENTRY is the whole control. The refs name a server this
        # spec no longer defines, which resolves to nothing and mounts nothing, so
        # they are left exactly as the user left them.
        assert CU_REF in cfg["tools"]
        assert cfg["allowedTools"] == [f"{CU_REF}/computer_get_state"]
        assert "ReadFile" in cfg["tools"]

    def test_the_gate_fails_CLOSED_when_the_keystone_cannot_be_read(self, tmp_path: Path):
        """An unreadable ceiling is never read generously.

        Same posture as ``enable_state.load_state``, and for a stronger reason
        here: the open position of this gate hands out the operator's whole
        desktop.
        """
        with ExitStack() as stack:
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", True))
            stack.enter_context(
                patch(
                    "kiro_crew.computer_use.enable_state.is_enabled",
                    side_effect=OSError("boom"),
                )
            )
            assert agent._computer_use_spec_gate() is False

    def test_a_gate_that_raises_is_treated_as_closed(self):
        """``_gated_off_servers`` never lets an exception mean "emit it"."""

        def _explode() -> bool:
            raise RuntimeError("gate is broken")

        managed = {CU_SERVER: {"invocation_fn": lambda: ("x", []), "spec_gate": _explode}}
        with patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=managed):
            assert agent._gated_off_servers() == frozenset({CU_SERVER})

    def test_only_computer_use_is_gated(self):
        """The always-on servers carry NO gate.

        A ``spec_gate`` accidentally added to ``kirocrew-core`` would remove the
        agent's whole first-party tool surface, and the failure would look like a
        model problem rather than a config one.
        """
        gated = {n for n, s in agent._MANAGED_MCP_SERVERS.items() if "spec_gate" in s}
        assert gated == {CU_SERVER}

    def test_the_shipped_row_gate_is_the_computer_use_predicate(self):
        """Pinned by identity: a gate is only useful if it is the real one."""
        assert agent._MANAGED_MCP_SERVERS[CU_SERVER]["spec_gate"] is (
            agent._computer_use_spec_gate
        )

    def test_enabling_rebuilds_the_spec_before_resetting_sessions(self):
        """**The enable path must still work in the session the user is sitting in.**

        The enable is now a spec-emission gate too, so a reset alone would restart
        every session into the same spec that omits the server — the tools would
        not appear until the next gateway start. Asserted on ORDER because a
        rebuild after the reset would restart sessions into the OLD spec and be
        just as broken.
        """
        import inspect

        from kiro_crew.dashboard.handlers import computer_use as handler

        src = inspect.getsource(handler.api_computer_use_config_save)
        rebuild_at = src.find("rebuild_agent_config")
        reset_at = src.find("_reset_all_sessions")
        assert rebuild_at != -1, "the enable flip must rebuild the agent spec"
        assert reset_at != -1
        assert rebuild_at < reset_at, "the rebuild must precede the session reset"

    def test_a_malformed_tools_list_does_not_fault_the_withhold(self):
        """A hand-edited ``tools: {}`` must not fault the whole rebuild.

        The gate runs on a config assembled from files we do not fully control,
        and a crash here would take down the one function that produces the agent
        spec at all — a far worse outcome than an odd ``tools`` value surviving.
        """
        cfg = {
            "name": "kirocrew",
            "tools": {"not": "a list"},
            "allowedTools": None,
            "mcpServers": {CU_SERVER: {"command": "x", "args": []}},
        }
        with ExitStack() as stack:
            stack.enter_context(
                patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=self._GATED)
            )
            stack.enter_context(
                patch("kiro_crew.agent._prompt_path", return_value=Path("/tmp/p.md"))
            )
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", False))
            agent._refresh_dynamic_fields(cfg)
        assert CU_SERVER not in cfg["mcpServers"], "the withhold must still happen"
        assert cfg["tools"] == {"not": "a list"}

    def test_the_WHOLE_install_withholds_the_server_and_audits_the_decision(
        self, tmp_path: Path
    ):
        """**End-to-end through ``install_agent``, which is what actually runs.**

        The per-function tests above prove each half; this one proves the file
        written to ``~/.kiro/agents`` — after every merge, validation and
        tools-sync pass — carries neither the entry nor the ref. That is the state
        kiro-cli reads, and it is the only assertion that would have caught the
        original bug.

        Staged as an UPGRADE (an existing config holding both the entry and the
        ref, i.e. what every user who ran a pre-fix build has on disk), because
        that is the case where the retraction has work to do.

        Also asserts the SEL record: "a managed server was withheld from your
        agent spec" is an outcome an operator has to be able to see, exactly like
        the neighbouring ``mcp_auto_approve_withheld`` audit.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps(
                {
                    "model": "claude-user-custom",
                    "tools": ["ReadFile", CU_REF, "@kirocrew-core"],
                    "allowedTools": ["ReadFile", f"{CU_REF}/computer_get_state"],
                    "mcpServers": {
                        CU_SERVER: {"command": "/usr/bin/kirocrew", "args": [CU_SUBCOMMAND]},
                        "kirocrew-core": {"command": "/usr/bin/kirocrew", "args": ["mcp-core"]},
                    },
                    "hooks": {"preToolUse": "audit"},
                }
            )
        )
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

            def __getattr__(self, _name):  # every other SEL call is a no-op here
                return lambda *a, **k: None

        with ExitStack() as stack:
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", False))
            stack.enter_context(patch("kiro_crew.agent.sel", lambda: _Sel()))
            path = _run_install(tmp_path, cfg_dir, managed=self._GATED)

        config = _installed(path)
        assert CU_SERVER not in config["mcpServers"]
        # The refs stay put: no entry means nothing to launch, and removing them
        # is what would lose a hand-narrowed grant.
        assert CU_REF in config["tools"]
        assert f"{CU_REF}/computer_get_state" in config["allowedTools"]
        # The always-on servers still ship — the gate is one row, not a purge.
        assert "kirocrew-core" in config["mcpServers"]
        assert "@kirocrew-core" in config["tools"]
        # And the user's own entries are untouched.
        assert "ReadFile" in config["tools"]

        withheld = [
            e
            for e in events
            if e.get("operation") == "mcp_server_withheld" and CU_REF in str(e.get("resources"))
        ]
        assert withheld, f"no SEL record for the withheld server: {events}"
        # The audit text must describe the config that was actually written. A
        # security trail claiming it removed a grant it left in place is worse
        # than no trail: it is read during incident response, when the config it
        # contradicts is the thing being reconstructed.
        _said = str(withheld[0].get("resources"))
        assert "mcpServers" in _said
        assert "mcpServers/tools" not in _said, (
            "the audit claims the tools ref was withheld, but it is still in the "
            f"written config: {_said!r} vs tools={config['tools']!r}"
        )
        assert CU_REF in config["tools"], "premise of the assertion above"

    def test_a_FRESH_install_audits_the_withheld_server_too(self, tmp_path: Path):
        """**The audit must not depend on there being a ref left to delete.**

        Nothing in the spec changes shape when a gate closes — the ref stays where
        the template put it and only the entry is withheld — so there is no delta
        to derive an audit from. The record comes from the gate plus the shipped
        template instead, so the fresh and existing paths report the same fact.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

            def __getattr__(self, _name):
                return lambda *a, **k: None

        with ExitStack() as stack:
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", False))
            stack.enter_context(patch("kiro_crew.agent.sel", lambda: _Sel()))
            path = _run_install(tmp_path, cfg_dir, managed=self._GATED)

        assert CU_SERVER not in _installed(path)["mcpServers"]
        assert [e for e in events if e.get("operation") == "mcp_server_withheld"], (
            f"a fresh install withheld the server without auditing it: {events}"
        )

    def test_no_withheld_audit_when_the_template_never_granted_it(self, tmp_path: Path):
        """An edition that ships without computer use has nothing to report.

        The record says "a grant the template makes was withheld"; with no grant
        there is no removal, and a per-rebuild event about a server the user was
        never offered is noise an auditor has to learn to ignore.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        defaults_path = cfg_dir / "defaults.json"
        defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
        defaults["tools"] = [t for t in defaults["tools"] if t != CU_REF]
        defaults_path.write_text(json.dumps(defaults))
        events: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                events.append(kw)

            def __getattr__(self, _name):
                return lambda *a, **k: None

        with ExitStack() as stack:
            stack.enter_context(patch("kiro_crew.platform_compat.IS_MACOS", False))
            stack.enter_context(patch("kiro_crew.agent.sel", lambda: _Sel()))
            _run_install(tmp_path, cfg_dir, managed=self._GATED)

        assert [e for e in events if e.get("operation") == "mcp_server_withheld"] == []


# ── The data-home pin ──


class TestDataHomePin:
    """``KIROCREW_HOME`` must reach the stdio shims, or they read a DIFFERENT home.

    A child process does NOT inherit the gateway's ``KIROCREW_HOME``; the spec's
    ``env`` map is the only channel. Without the pin the gateway writes
    ``computer_use.json`` to the override home while ``mcp_computer`` reads the
    DEFAULT one, and the failure is silent AND self-contradictory: Settings shows
    the feature ON while the shim publishes an empty ``tools/list``, so the agent
    truthfully answers "I have no computer-use tools" — which reads as the feature
    being broken. Found by running the feature in dev mode.

    Applies to every managed server, not just this one: the same split would
    desynchronise the cron store and the lessons file.
    """

    @staticmethod
    def _entries(monkeypatch, home: "str | None") -> dict:
        """Build a fresh spec with (or without) an override in effect."""
        import kiro_crew.config.paths as paths

        if home is None:
            monkeypatch.delenv("KIROCREW_HOME", raising=False)
        else:
            monkeypatch.setenv("KIROCREW_HOME", home)
        monkeypatch.setattr(paths, "_resolved_home", None)
        with patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=_MANAGED):
            with patch("kiro_crew.agent._prompt_path", return_value=Path("/tmp/p.md")):
                return agent.build_agent_config()["mcpServers"]

    def test_every_managed_server_is_pinned_under_an_override(self, tmp_path, monkeypatch):
        servers = self._entries(monkeypatch, str(tmp_path))
        for name in _MANAGED:
            assert servers[name]["env"]["KIROCREW_HOME"] == str(tmp_path.resolve()), name

    def test_a_default_install_emits_no_env_at_all(self, monkeypatch):
        """The emitted spec must be byte-for-byte unchanged where there is no override.

        An empty ``env`` is a launch-behaviour no-op but a real diff in the file, and
        ``_prune_empty`` treats present-but-empty as equivalent — so emitting one
        would churn every existing user's ``kirocrew.json`` for nothing.
        """
        servers = self._entries(monkeypatch, None)
        for name in _MANAGED:
            assert "env" not in servers[name], name

    def test_a_ROOT_override_is_not_propagated(self, monkeypatch):
        """``config_dir()`` refuses a filesystem root and falls back to the default.

        Propagating one would INVERT the bug — the shim would honour a home the
        gateway itself declined to use. Asserted on every OS because the resolver's
        root test is the portable ``p == p.parent`` one (``/`` → ``/``, ``D:\\`` →
        ``D:\\``), so ``"/"`` is refused on Windows too.
        """
        servers = self._entries(monkeypatch, "/")
        assert "env" not in servers[CU_SERVER]

    @pytest.mark.skipif(not IS_POSIX, reason="POSIX system-directory prefixes only")
    @pytest.mark.parametrize("bad", ["/usr", "/System"])
    def test_an_INVALID_POSIX_SYSTEM_override_is_not_propagated(self, bad, monkeypatch):
        """The named-system-directory half of the same refusal.

        POSIX-gated because the resolver matches on ``p.parts[:2] == ("/", "usr")``
        AFTER ``resolve()``, and on Windows ``Path("/usr").resolve()`` is
        ``<drive>:\\usr`` — a perfectly ordinary directory the resolver accepts. The
        pin must follow the resolver there, not a hard-coded refusal, which is what
        ``test_the_pin_always_AGREES_with_the_resolver`` asserts on every OS.

        (``/etc`` is deliberately not in this list: on macOS it is a symlink to
        ``/private/etc``, so ``_valid_override_home``'s ``resolve()`` defeats its own
        ``("/", "etc")`` prefix test and the override is ACCEPTED. That is a
        pre-existing quirk of the shared resolver, not of this pin — and it is
        harmless here precisely because the pin reads the same resolver, so the
        gateway and the shim still agree on whatever it decides. Asserting a refusal
        here would be asserting behaviour the resolver does not have.)
        """
        servers = self._entries(monkeypatch, bad)
        assert "env" not in servers[CU_SERVER]

    def test_the_pin_always_AGREES_with_the_resolver(self, tmp_path, monkeypatch):
        """The invariant that actually matters, asserted directly.

        Not "invalid overrides are refused" — that is the resolver's business and it
        has edge cases (see above). What this pin must guarantee is that the home the
        shim is told to use is the SAME one the gateway resolved. Parametrised over
        the awkward values so a future resolver change cannot silently split them.
        """
        import kiro_crew.config.paths as paths

        for candidate in (str(tmp_path), "/", "/usr", "/etc", "/System"):
            monkeypatch.setenv("KIROCREW_HOME", candidate)
            monkeypatch.setattr(paths, "_resolved_home", None)
            expected = paths._valid_override_home()
            pinned = agent._managed_mcp_env().get("KIROCREW_HOME")
            assert pinned == (str(expected) if expected else None), candidate

    def test_a_refresh_pins_an_existing_config_and_keeps_user_env(self, tmp_path, monkeypatch):
        """The pin is OURS to refresh, unlike ``autoApprove`` which is preserved."""
        import kiro_crew.config.paths as paths

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(paths, "_resolved_home", None)
        cfg = {
            "mcpServers": {
                CU_SERVER: {"command": "old", "args": [], "env": {"MY_VAR": "keep"}},
            }
        }
        with patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=_MANAGED):
            with patch("kiro_crew.agent._prompt_path", return_value=Path("/tmp/p.md")):
                agent._refresh_dynamic_fields(cfg)
        env = cfg["mcpServers"][CU_SERVER]["env"]
        assert env["KIROCREW_HOME"] == str(tmp_path.resolve())
        # A user's own variable must survive the merge.
        assert env["MY_VAR"] == "keep"

    def test_a_refresh_CLEARS_a_stale_pin_when_the_override_is_gone(self, monkeypatch):
        """A config written under an override, refreshed on a default install.

        Leaving the old value would point the shims at a home the gateway is no
        longer using — the same desync, one release later.
        """
        import kiro_crew.config.paths as paths

        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(paths, "_resolved_home", None)
        cfg = {
            "mcpServers": {
                CU_SERVER: {"command": "old", "args": [], "env": {"KIROCREW_HOME": "/stale"}},
            }
        }
        with patch.multiple("kiro_crew.agent", _MANAGED_MCP_SERVERS=_MANAGED):
            with patch("kiro_crew.agent._prompt_path", return_value=Path("/tmp/p.md")):
                agent._refresh_dynamic_fields(cfg)
        assert "env" not in cfg["mcpServers"][CU_SERVER]
