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

from kiro_crew import agent, mcp_cleanup, mcp_discovery, onboarding_import
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


def _run_install(tmp_path: Path, cfg_dir: Path, **kwargs) -> Path:
    """Run ``install_agent`` with every module global redirected into *tmp_path*.

    The ``patch.multiple`` + ``ExitStack`` shape from ``test_agent.py``. This is
    what keeps a test from touching the developer's real ``~/.kiro/agents`` — which
    for THIS feature would mean writing a computer-use MCP entry into their live
    agent config.
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
            _MANAGED_MCP_SERVERS=_MANAGED,
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


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])


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
