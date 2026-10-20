"""App bridge tests: the materialized app agent configs.

Builtin agent naming and declaration, user-edit preservation, the neutralize merge, prompt
containment, template rendering, pruning, write-before-delete, snapshot upkeep and refresh
I/O failures.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from app_bridges_helpers import _stub_venv_probe  # noqa: F401
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew.apps.bridges import _register_agents, register_app
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest

_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestBuiltinAgentNamesAreNamespaced:
    """Builtin app agents must not squat a generic global agent name.

    kiro-cli resolves an agent by the ``name`` field INSIDE the JSON, not by the
    namespaced link filename ``_register_agents`` writes into ~/.kiro/agents/
    (``<app>--<agent>.json``).  Agent names are therefore ONE FLAT GLOBAL
    namespace: two installed agents claiming the same name collide, and kiro-cli
    only warns and picks one.  Prefixing with the app id (app ids are unique in
    the registry) is what makes a public install collision-proof.
    """

    def _builtin_dirs(self):

        import kiro_crew.apps.builtins as builtins_pkg

        root = Path(builtins_pkg.__file__).parent
        return [p for p in root.iterdir() if (p / "app.json").is_file()]

    def test_every_declared_agent_name_is_app_id_prefixed(self):
        checked = 0
        for app_dir in self._builtin_dirs():
            manifest = json.loads((app_dir / "app.json").read_text(encoding="utf-8"))
            app_id = manifest.get("name") or app_dir.name
            for rel in manifest.get("agents") or []:
                agent_file = app_dir / rel
                assert agent_file.is_file(), f"{app_id}: declared agent missing: {rel}"
                name = json.loads(agent_file.read_text(encoding="utf-8")).get("name", "")
                assert name == app_id or name.startswith(f"{app_id}-"), (
                    f"{app_id}: agent name {name!r} is not app-id-prefixed — it would "
                    f"collide with any other install claiming that global name"
                )
                checked += 1
        if checked == 0:
            # No shipped builtin declares an agent yet, so there is nothing to
            # check — but a guard that passes on an empty sample is worthless, so
            # say so out loud rather than pass silently. The first agent-declaring
            # builtin turns this on.
            pytest.skip("no builtin declares agents yet — guard is vacuous")


class TestUserAgentEditsSurviveRefresh:
    """App agent JSONs are re-materialized every boot; user edits must survive.

    Registration rewrites these files from the packaged template on each
    registration (that is what lets a template change land without a reinstall),
    but a wholesale write silently reverted anything the user had tuned by hand
    — `model`, extra `toolsSettings` — on every gateway start. Same split as the
    managed-MCP refresh: framework-derived keys are refreshed (a stale one is a
    bug), everything else on disk is the user's and wins.
    """

    def test_user_keys_win_and_owned_keys_are_refreshed(self) -> None:
        from kiro_crew.apps.bridges import _preserve_user_agent_edits

        prior = {
            "name": "app--agent",
            "model": "some-pinned-model",  # user's
            "description": "my tweaks",  # user's
            "allowedTools": ["@stale"],  # framework's
            "prompt": "file:///old/path.md",  # framework's
        }
        fresh = {
            "name": "app--agent",
            "model": "auto",
            "allowedTools": ["@fresh"],
            "prompt": "file:///new/path.md",
        }
        out = _preserve_user_agent_edits("app--agent.json", prior, fresh)

        # Theirs survives...
        assert out["model"] == "some-pinned-model"
        assert out["description"] == "my tweaks"
        # ...ours is refreshed, not resurrected from the old file.
        assert out["allowedTools"] == ["@fresh"]
        assert out["prompt"] == "file:///new/path.md"

    def test_no_prior_file_is_a_no_op(self) -> None:
        from kiro_crew.apps.bridges import _preserve_user_agent_edits

        fresh = {"name": "a", "model": "auto"}
        assert _preserve_user_agent_edits("a.json", None, fresh) == fresh

    def test_containment_keys_are_refreshed_not_preserved(self) -> None:
        """`managedToolPolicy` / `includeMcpJson` are the framework's, not the user's.

        Preserving them is wrong in BOTH directions, which is why they are owned:
        a template that later tightens the exclude list would never reach an
        already-enabled install, and anything that edited the file could drop the
        exclude list — which the old rule then preserved forever. There is no
        provenance here to tell a hand edit from last boot's template copy, so the
        line is drawn by meaning: containment is refreshed, preference is kept.
        """
        from kiro_crew.apps.bridges import _preserve_user_agent_edits

        prior = {
            "name": "app--agent",
            "model": "user-pinned",  # preference — must survive
            "managedToolPolicy": {"exclude": []},  # containment dropped on disk
            "includeMcpJson": True,  # containment widened on disk
        }
        fresh = {
            "name": "app--agent",
            "model": "auto",
            "managedToolPolicy": {"exclude": ["spawn_run", "cron_add"]},
            "includeMcpJson": False,
        }
        out = _preserve_user_agent_edits("app--agent.json", prior, fresh)

        assert out["model"] == "user-pinned", "a real preference must still win"
        assert out["managedToolPolicy"] == {"exclude": ["spawn_run", "cron_add"]}
        assert out["includeMcpJson"] is False

    def test_a_corrupt_prior_file_does_not_fail_the_refresh(self, tmp_path) -> None:
        """An unreadable file must not block registration — it means "nothing to
        preserve", not "abort"."""
        from kiro_crew.apps.bridges import _read_agent_config

        broken = tmp_path / "a.json"
        broken.write_text("{not json", encoding="utf-8")
        assert _read_agent_config(broken) is None
        assert _read_agent_config(tmp_path / "absent.json") is None

    def test_the_owned_key_set_names_every_field_the_framework_derives(self) -> None:
        """A field the framework computes but forgets to list here would be
        frozen at whatever the on-disk file happened to hold.

        The set is spelled out rather than derived because the rule is a JUDGEMENT
        per key, not a property of the data: there is no provenance recording what
        the last template wrote, so a key is owned when it means CONTAINMENT (the
        framework must be able to tighten it, and nothing may loosen it by editing
        the file) and unowned when it means PREFERENCE (the user's choice
        outranks the template's default). Adding a key below is a security
        decision; adding one to the template without deciding is the bug this
        guard exists to surface.
        """
        from kiro_crew.apps.bridges import _FRAMEWORK_OWNED_AGENT_KEYS

        assert _FRAMEWORK_OWNED_AGENT_KEYS == {
            "name",
            "mcpServers",
            "tools",
            "allowedTools",
            "prompt",
            "managedToolPolicy",
            "includeMcpJson",
            # A generated `file://` path list into the app's provisioned tree, rendered
            # from `{ENGINE_ROOT}` placeholders by the gateway — CONTAINMENT-shaped like
            # `prompt`: a user-pinned copy would keep pointing at a previous engine root
            # and silently stop resolving after a re-provision.
            "resources",
        }

    def test_every_template_key_is_a_decided_key(self) -> None:
        """Nothing in a shipped agent template may sit outside the two buckets.

        The failure mode is silent: a new template key that is neither owned nor
        a known preference lands in "preserved forever" by default — so a later
        template tightening it never reaches an existing install, and nothing
        anywhere says so.
        """
        import json

        from kiro_crew.apps.bridges import _FRAMEWORK_OWNED_AGENT_KEYS

        # Keys a user is MEANT to be able to pin by hand.
        # Keys a user is MEANT to be able to pin by hand. `welcomeMessage` is
        # user-facing copy with no containment role, so a reworded greeting must
        # survive a template refresh — the same reasoning as `description`.
        #
        # `skills` belongs here too, not in `_FRAMEWORK_OWNED_AGENT_KEYS`: it is a live field
        # (`agent_discovery_parts/agent_roster.py` reads `row.get("skills")` into
        # `AgentInfo.skills`) naming which
        # skills an agent loads, which is exactly the kind of choice an operator should be able
        # to change and keep across a refresh — same category as `model`. Framework ownership is
        # reserved for identity and CONTAINMENT keys, which this is not. Added when the
        # auto-improvement builtin became the first template to declare it.
        preferences = {
            "description",
            "model",
            "toolsSettings",
            "$schema",
            "welcomeMessage",
            "skills",
        }
        root = _REPO_ROOT / "src/kiro_crew/apps/builtins"
        templates = sorted(root.glob("*/agents/*.json"))
        if not templates:
            # Same reasoning as the namespacing guard above: nothing ships an
            # agent template yet, and a silent pass would hide that.
            pytest.skip("no builtin ships an agent template yet")
        for tpl in templates:
            keys = set(json.loads(tpl.read_text(encoding="utf-8")))
            undecided = keys - _FRAMEWORK_OWNED_AGENT_KEYS - preferences
            assert not undecided, (
                f"{tpl}: {sorted(undecided)} is neither framework-owned nor a known "
                f"preference — decide which, then add it to the matching set"
            )


class TestBuiltinDeclaredResourcesActuallyRegister:
    """A builtin that declares agents/skills must get them registered.

    Two independent framework bugs made this silently fail, and BOTH are
    silent-by-construction (registration only logs a warning), so they need
    executable pins rather than review vigilance:

    1. ``_manifest_to_builtin_dict`` (discovery.py) hand-copied a subset of
       AppManifest fields into the dict that register_builtin_apps() persists as
       the data-home app.json snapshot. ``agents`` and ``skills`` were not in
       that subset, so they never reached the snapshot that register_app() reads.
    2. ``register_app`` resolved manifest-relative paths against the data-home
       app dir. A builtin's code lives in the PACKAGE, so every path missed.
    """

    def test_builtin_dict_carries_every_declarative_manifest_field(self):
        """No AppManifest field may be silently dropped by the conversion.

        Dataclass-field-driven on purpose: adding a field to AppManifest without
        teaching the conversion about it fails here instead of vanishing.
        """
        import dataclasses

        from kiro_crew.apps.discovery import _manifest_to_builtin_dict
        from kiro_crew.apps.manifest import AppManifest

        declared = {f.name for f in dataclasses.fields(AppManifest)}
        # ``extra`` is the catch-all bag, splatted into the dict by key.
        declared.discard("extra")
        # Parser diagnostics reject a manifest; they are not declarations copied
        # into a valid builtin's persisted manifest.
        declared.discard("config_keys_error")

        manifest = AppManifest.from_dict(
            {
                "name": "probe",
                "version": "1.0.0",
                "displayName": "Probe",
                "description": "d",
                "author": "a",
                "license": "MIT",
                "minKiroCrewVersion": "1.0.0",
                "signer": "s",
                "signature": "sig",
                "agents": ["agents/a.json"],
                "skills": ["skills/s"],
                "sops": ["sops/x.md"],
                "jobFamilies": ["jf"],
                "tags": ["t"],
                "mcpServers": {"m": {"command": "c"}},
                "config_keys": ["probe-owned"],
                "platform": {"requiresDesktopApp": True},
                "permissions": {"storage": True},
                "ui": {"entry": "ui/dist/index.js"},
                "backend": {"entryPoint": "backend:app"},
                "crons": [{"name": "c", "schedule": "* * * * *", "message": "m"}],
                "dependencies": {"commands": ["git"]},
                "setup": {"onEnable": "echo hi"},
                "publishProvider": {"id": "p", "label": "P"},
                "notifications": {"channels": [{"id": "n", "name": "N"}]},
                "contributes": {
                    "commands": [
                        {
                            "id": "probe-cmd",
                            "title": "Probe",
                            "prompt": "do the thing",
                        }
                    ]
                },
            }
        )
        # Every declared field must be populated above, otherwise a conditional
        # copy ("if manifest.x") is never exercised and the check goes vacuous.
        for fname in sorted(declared):
            assert getattr(manifest, fname), f"probe manifest leaves {fname!r} empty"
        d = _manifest_to_builtin_dict(manifest)
        missing = sorted(f for f in declared if f not in d)
        assert not missing, (
            f"fields dropped by _manifest_to_builtin_dict: {missing} — they will "
            f"be absent from every builtin's persisted manifest snapshot"
        )

    def test_resource_root_for_builtin_is_the_package_dir(self, app_env):
        """A builtin resolves resource paths against the packaged dir, not $HOME."""
        import json as _json

        from kiro_crew.apps.bridges import _app_resource_root
        from kiro_crew.apps.discovery import _get_builtins_dir
        from kiro_crew.apps.manager import get_app, register_builtin_apps

        register_builtin_apps()
        packaged = _get_builtins_dir()
        # Driven from the packaged DIRS, keyed by each manifest's own name. The
        # registry keys by app name (`auto-research`) while the package dir uses
        # underscores (`auto_research`), so reading names off `iterdir()`
        # conflated the two: the assertion only held for a builtin needing no
        # normalising, and which one came first was filesystem order.
        checked = []
        for d in sorted(packaged.iterdir()):
            manifest = d / "app.json"
            if not manifest.is_file():
                continue
            name = _json.loads(manifest.read_text(encoding="utf-8")).get("name")
            if not name or get_app(name) is None:
                continue  # packaged but not registered in this build
            root = _app_resource_root(name)
            assert root == d, f"{name}: resources must resolve to the packaged dir"
            assert (root / "app.json").is_file(), name
            checked.append(name)

        assert checked, "no registered packaged builtins found — test would be vacuous"
        # The hyphenated case is the one that regressed silently; pin it so a
        # future refactor cannot drop the normalisation while every
        # underscore-free builtin still passes.
        assert any("-" in n for n in checked), f"expected a hyphenated builtin, got {checked}"


class TestNeutralizeEntryShape:
    """A neutralize entry must be a complete server spec, not a bare deny.

    kiro-cli's agent loader parses strictly: one mcpServers entry without a
    command makes it reject the WHOLE agent file — the agent then vanishes from
    the ACP mode list ("Mode not found" at session time) while `agent list` and
    `agent validate` still show it, because those use a lenient parser. A bare
    {"disabledTools": [...]} therefore does not deny a server; it silently
    unregisters the agent.
    """

    def test_neutralize_copies_the_full_spec_from_the_global_file(self, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            bridges,
            "_global_mcp_specs",
            lambda: {"some-server": {"command": "srv", "args": ["--x"], "env": {"A": "1"}}},
        )
        agent = {"name": "a", "tools": ["@some-server"], "mcpServers": {}}
        out = bridges._apply_agent_mcp_policy(
            agent, "a", {"agents": {"a": {"neutralize": {"some-server": ["t1", "t2"]}}}}
        )
        entry = out["mcpServers"]["some-server"]
        assert entry["command"] == "srv", "spec must be copied, not a bare deny"
        assert entry["disabledTools"] == ["t1", "t2"]
        assert "@some-server" not in out["tools"]

    def test_neutralize_drops_per_tool_grants(self, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(
            bridges, "_global_mcp_specs", lambda: {"some-server": {"command": "srv"}}
        )
        agent = {
            "name": "a",
            "tools": ["@some-server"],
            "allowedTools": [
                "@some-server",
                "@some-server/t1",
                "@some-server/t1",
                "@some-server-2/t1",
            ],
            "mcpServers": {},
        }
        out = bridges._apply_agent_mcp_policy(
            agent, "a", {"agents": {"a": {"neutralize": {"some-server": ["t1"]}}}}
        )
        assert out["allowedTools"] == ["@some-server-2/t1"]

    def test_server_without_a_global_spec_is_skipped_not_emitted_bare(self, monkeypatch):
        from kiro_crew.apps import bridges

        monkeypatch.setattr(bridges, "_global_mcp_specs", lambda: {})
        agent = {"name": "a", "tools": [], "mcpServers": {}}
        out = bridges._apply_agent_mcp_policy(
            agent, "a", {"agents": {"a": {"neutralize": {"ghost-server": ["t"]}}}}
        )
        assert "ghost-server" not in out["mcpServers"]

    def test_every_emitted_entry_has_a_command(self, monkeypatch):
        """The invariant itself, over a mixed grant+neutralize merge."""
        from kiro_crew.apps import bridges

        monkeypatch.setattr(bridges, "_global_mcp_specs", lambda: {"n1": {"command": "c1"}})
        agent = {
            "name": "a",
            "tools": [],
            "mcpServers": {"own": {"command": "me", "args": []}},
        }
        out = bridges._apply_agent_mcp_policy(
            agent,
            "a",
            {
                "agents": {
                    "a": {
                        "servers": {"own": {"autoApprove": ["x"]}},
                        "neutralize": {"n1": ["t"], "n2": ["t"]},
                    }
                }
            },
        )
        missing = [k for k, v in out["mcpServers"].items() if not v.get("command")]
        assert missing == [], f"entries without command: {missing}"


class TestAppPromptPathIsContained:
    """An app's prompt path is app-controlled and read verbatim as the SYSTEM
    prompt — so it must resolve inside the app's own directories.

    Without the bound, an app writing ``file:///Users/me/.ssh/id_rsa`` into its
    policy hands a credential file to kiro-cli as the persona, and its contents
    reach the model. The path is only ever legitimately shipped inside the app or
    rendered into the app's data dir, so anything else is dropped.
    """

    def _call(self, tmp_path, raw):
        from kiro_crew.apps import bridges

        app_root = tmp_path / "app"
        app_root.mkdir(exist_ok=True)
        policy = {"agents": {"a": {"prompt": raw}}}
        return bridges._apply_agent_prompt({}, "a", policy, "someapp", app_root), app_root

    def test_a_path_outside_the_app_is_dropped(self, tmp_path, monkeypatch):
        from kiro_crew.apps import bridges, manager

        monkeypatch.setattr(manager, "app_data_dir", lambda n: tmp_path / "data")
        monkeypatch.setattr(bridges, "app_data_dir", lambda n: tmp_path / "data")
        outside = tmp_path / "secret.txt"
        outside.write_text("KEY")
        merged, _ = self._call(tmp_path, f"file://{outside}")
        assert "prompt" not in merged  # escaping path refused

    def test_a_path_inside_the_app_root_is_kept(self, tmp_path, monkeypatch):
        from kiro_crew.apps import bridges, manager

        monkeypatch.setattr(manager, "app_data_dir", lambda n: tmp_path / "data")
        monkeypatch.setattr(bridges, "app_data_dir", lambda n: tmp_path / "data")
        (tmp_path / "app").mkdir(exist_ok=True)
        prompt = tmp_path / "app" / "persona.md"
        prompt.write_text("you are")
        merged, _ = self._call(tmp_path, f"file://{prompt}")
        assert merged["prompt"] == f"file://{prompt.resolve()}"

    def test_a_symlink_escaping_the_app_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.apps import bridges, manager

        monkeypatch.setattr(manager, "app_data_dir", lambda n: tmp_path / "data")
        monkeypatch.setattr(bridges, "app_data_dir", lambda n: tmp_path / "data")
        (tmp_path / "app").mkdir(exist_ok=True)
        secret = tmp_path / "id_rsa"
        secret.write_text("KEY")
        link = tmp_path / "app" / "prompt.md"
        try:
            link.symlink_to(secret)
        except OSError:
            import pytest

            pytest.skip("symlinks unavailable")
        merged, _ = self._call(tmp_path, f"file://{link}")
        # resolve() follows the link out of the app root, so containment fails.
        assert "prompt" not in merged


class TestRegisterPrunesUpgradedAwayResources:
    """A manifest UPGRADE that drops an agent or MCP server must un-register it.
    Pruning lives in the off-loop boot reconcile (reconcile_enabled_app_resources),
    NOT in register_app: register_app is called on the event loop by the
    enable/update handlers, where a directory walk + lock would stall chat, and
    those callers deregister first. The one path that re-registers without a
    preceding deregister — the boot reconcile — does the selective prune.
    """

    def test_stale_app_agent_and_server_are_pruned(self, tmp_path, app_env) -> None:
        import json as _json

        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(tmp_path)  # declares my-agent, no mcpServers
        install_app(src)
        register_app("test-app")
        assert (app_env["kiro_agents"] / "test-app--my-agent.json").is_file()

        # Simulate resources a PRIOR manifest version registered that the current
        # manifest does not declare.
        ghost_link = app_env["kiro_agents"] / "test-app--ghost.json"
        ghost_link.write_text("{}", encoding="utf-8")
        with bridges_mod._mcp_lock():
            data = bridges_mod._read_mcp_json_unlocked()
            data.setdefault("mcpServers", {})["test-app:ghost"] = {"command": "x"}
            bridges_mod._write_mcp_json_unlocked(data)

        # register_app must NOT prune: it runs on the event loop, so the walk+lock
        # is kept off it. The ghosts survive a bare re-register.
        register_app("test-app")
        assert ghost_link.exists(), "register_app must not prune on the event loop"

        # The off-loop boot reconcile is the one path that prunes. Ensure the app
        # is enabled so reconcile processes it (it skips disabled apps).
        from kiro_crew.apps.manager import enable_app

        enable_app("test-app")
        bridges_mod.reconcile_enabled_app_resources()

        assert not ghost_link.exists(), "a removed agent must be pruned by reconcile"
        after = _json.loads(bridges_mod._mcp_json_path().read_text(encoding="utf-8"))
        assert "test-app:ghost" not in after.get(
            "mcpServers", {}
        ), "a removed server must be pruned"
        # The still-declared agent survives the prune.
        assert (app_env["kiro_agents"] / "test-app--my-agent.json").is_file()


class TestRegisterNeverDeletesBeforeReplacement:
    """No destination — regular file OR legacy symlink — is unlinked before the
    atomic write. atomic_write's os.replace swaps the NAME atomically for both, so
    a write that fails (disk full at startup) must leave the prior entry intact.
    """

    def test_a_legacy_symlink_survives_a_failed_rewrite(self, tmp_path, app_env, monkeypatch):
        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        link = app_env["kiro_agents"] / "test-app--my-agent.json"
        legacy_target = tmp_path / "legacy.json"
        legacy_target.write_text('{"name": "my-agent"}', encoding="utf-8")
        try:
            link.symlink_to(legacy_target)
        except OSError:
            import pytest

            pytest.skip("symlinks unavailable")

        def _boom(*a, **k):
            raise OSError("No space left on device")

        monkeypatch.setattr(bridges_mod, "atomic_write", _boom)
        _register_agents("test-app", manifest, app_root)  # swallows the OSError
        assert link.is_symlink(), "a failed write must not have unlinked the legacy symlink"


class TestPruneAbortsOnUnreadableAgent:
    """An unreadable declared agent is NOT a removed one: pruning on an incomplete
    current set would delete a still-declared agent's last-good config over a
    transient IO error. The agent prune aborts when any declared agent can't be
    read.
    """

    def test_unreadable_agent_aborts_the_agent_prune(self, tmp_path, app_env, monkeypatch):
        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        # A materialized config that MUST survive if the prune aborts.
        keep = app_env["kiro_agents"] / "test-app--my-agent.json"
        keep.write_text('{"name": "my-agent"}', encoding="utf-8")
        # Make the declared agent source unreadable.
        (app_root / "agents" / "my-agent.json").write_text("{ not json", encoding="utf-8")

        bridges_mod._prune_stale_app_resources("test-app", manifest, app_root)
        assert keep.is_file(), "prune must abort — not delete a config over an unreadable source"

    @pytest.mark.parametrize("content", ["[1, 2, 3]", "42", "null", "true", '"a string"'])
    def test_a_valid_json_non_object_agent_spec_aborts_the_agent_prune(
        self, tmp_path, app_env, monkeypatch, content
    ):
        """Valid JSON that is not an object parses fine, so the JSONDecodeError
        guard never fires — but ``.get`` on the parsed value would raise
        AttributeError. A spec that cannot be read as an object is the same
        cannot-read != removed situation: the agent must be RETAINED (treated
        as present), never pruned out of its last-good materialized config.
        """
        from kiro_crew.apps import bridges as bridges_mod

        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        # A materialized config that MUST survive if the prune aborts.
        keep = app_env["kiro_agents"] / "test-app--my-agent.json"
        keep.write_text('{"name": "my-agent"}', encoding="utf-8")
        # Make the declared agent source valid JSON but not an object.
        (app_root / "agents" / "my-agent.json").write_text(content, encoding="utf-8")

        bridges_mod._prune_stale_app_resources("test-app", manifest, app_root)
        assert (
            keep.is_file()
        ), "prune must retain the agent — a non-object spec is unreadable, not removed"


class TestShippedAgentTemplatesAreRenderedByTheGateway:
    """A shipped template is rendered BY THE GATEWAY, from values it computes itself.

    An earlier version of this took the app's own provisioned copy from its install dir
    and verified it was "the template with only placeholders substituted". A reviewer
    pointed out why that is unsound and they were right: the check constrained WHERE a
    substitution could appear but not WHAT it could contain — and `{UV_BIN}` is an
    executable path, so an agent with write access to the engine directory could
    substitute its own binary and kiro-cli would run it.

    Every value is computable in the gateway, so nothing is read back from the mutable
    side at all: the bytes come from the immutable package, the values from here.
    """

    def test_placeholders_resolve_to_gateway_computed_values(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import _placeholder_values

        values = _placeholder_values("pptx-maker")
        assert set(values) == {
            "{UV_BIN}",
            "{ENGINE_ROOT}",
            "{ENGINE_MCP_DIR}",
            "{APP_PROMPTS}",
            "{TOOLS_PATH}",
        }
        # Under the data home this fixture set, i.e. derived here rather than read.
        assert str(app_env["home"]) in values["{ENGINE_ROOT}"]
        # `{TOOLS_PATH}` becomes the MCP server's PATH, and an empty element there
        # means the CWD on POSIX — tool resolution would depend on where kiro-cli
        # happened to start the server.
        assert "" not in values["{TOOLS_PATH}"].split(os.pathsep)

    def test_an_unknown_app_resolves_nothing(self, tmp_path, app_env):
        """Fail-closed: adding a placeholder to a new app's config is inert until its
        values are named, rather than silently registering an unrendered config."""
        from kiro_crew.apps.bridges import _placeholder_values

        assert _placeholder_values("some-other-app") == {}

    def test_a_template_is_rendered_into_the_data_home(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import _render_shipped_agent

        shipped = tmp_path / "package" / "pptx-maker" / "agents"
        shipped.mkdir(parents=True)
        template = shipped / "a.json"
        template.write_text(json.dumps({"name": "a", "command": "{UV_BIN}"}))

        out = _render_shipped_agent("pptx-maker", template)
        assert out is not None
        # NOT the app's install dir: the file kiro-cli reads must not be one the app
        # can rewrite after registration.
        assert (app_env["home"] / "apps" / "pptx-maker") not in out.parents
        rendered = json.loads(out.read_text(encoding="utf-8"))
        assert "{UV_BIN}" not in rendered["command"]
        # `Path(...).stem`, not `endswith("uv")`: on Windows `resolve_uv()` returns
        # `uv.exe`, so a suffix check on the bare name passed on POSIX and failed the
        # Windows shard — green on two platforms and red on the third is worse than
        # failing everywhere.
        assert Path(rendered["command"]).stem == "uv"

    def test_the_install_dir_copy_is_never_read(self, tmp_path, app_env):
        """The whole point of the redesign: an attacker-written copy in the install dir
        has no influence, because it is not consulted."""
        from kiro_crew.apps.bridges import _render_shipped_agent

        shipped = tmp_path / "package" / "pptx-maker" / "agents"
        shipped.mkdir(parents=True)
        template = shipped / "a.json"
        template.write_text(json.dumps({"name": "a", "command": "{UV_BIN}"}))

        install = app_env["home"] / "apps" / "pptx-maker" / "agents"
        install.mkdir(parents=True)
        (install / "a.json").write_text(
            json.dumps({"name": "a", "command": "/tmp/attacker-binary"})
        )

        out = _render_shipped_agent("pptx-maker", template)
        assert out is not None
        rendered = json.loads(out.read_text(encoding="utf-8"))
        assert rendered["command"] != "/tmp/attacker-binary"

    def test_a_config_with_no_placeholder_is_returned_untouched(self, tmp_path, app_env):
        from kiro_crew.apps.bridges import _render_shipped_agent

        shipped = tmp_path / "package" / "pptx-maker" / "agents"
        shipped.mkdir(parents=True)
        concrete = shipped / "a.json"
        concrete.write_text(json.dumps({"name": "a", "command": "/real/uv"}))

        assert _render_shipped_agent("pptx-maker", concrete) == concrete

    def test_an_unresolvable_placeholder_registers_nothing(self, tmp_path, app_env):
        """Better to register no agent than one naming a literal `{ENGINE_ROOT}`."""
        from kiro_crew.apps.bridges import _render_shipped_agent

        shipped = tmp_path / "package" / "unknown-app" / "agents"
        shipped.mkdir(parents=True)
        template = shipped / "a.json"
        template.write_text(json.dumps({"name": "a", "dir": "{ENGINE_ROOT}"}))

        assert _render_shipped_agent("unknown-app", template) is None

    def test_a_windows_path_survives_json_escaping(self, tmp_path, app_env):
        """The placeholders sit INSIDE JSON string literals, so a path's backslashes
        must be escaped or the render is invalid JSON (or a mangled separator)."""
        from unittest import mock

        from kiro_crew.apps import bridges

        shipped = tmp_path / "package" / "pptx-maker" / "agents"
        shipped.mkdir(parents=True)
        template = shipped / "a.json"
        template.write_text(json.dumps({"name": "a", "command": "{UV_BIN}"}))

        with mock.patch.object(
            bridges, "_placeholder_values", return_value={"{UV_BIN}": r"C:\Users\me\uv.exe"}
        ):
            out = bridges._render_shipped_agent("pptx-maker", template)
        assert out is not None
        # Parses, and the separator round-trips.
        assert json.loads(out.read_text(encoding="utf-8"))["command"] == r"C:\Users\me\uv.exe"


class TestRegisterAgentsSnapshotUpkeep:
    """`_register_agents` owns keeping the resolver's materialized-agent snapshot
    honest: it publishes what it writes, and it reconciles the directory even when
    it writes nothing, so a name pruned from disk stops being dispatchable."""

    def test_deregister_refreshes_the_snapshot(self, monkeypatch, tmp_path):
        # Removing an app's agent files must drop them from the resolver's
        # snapshot. Otherwise a disabled app's agent stays dispatchable in memory
        # and a slot still bound to it hands kiro-cli a name whose config is gone.
        from kiro_crew.apps import bridges

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "someapp--main.json").write_text(json.dumps({"name": "main"}), encoding="utf-8")

        calls: list[str] = []
        monkeypatch.setattr(bridges, "_kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(
            bridges, "schedule_materialized_agents_refresh", lambda: calls.append("refresh")
        )

        assert bridges._deregister_agents("someapp") == 1
        assert calls == ["refresh"]

    def test_deregister_without_removals_does_not_refresh(self, monkeypatch, tmp_path):
        from kiro_crew.apps import bridges

        agents = tmp_path / "agents"
        agents.mkdir()
        calls: list[str] = []
        monkeypatch.setattr(bridges, "_kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(
            bridges, "schedule_materialized_agents_refresh", lambda: calls.append("refresh")
        )

        assert bridges._deregister_agents("someapp") == 0
        assert calls == []

    def test_refresh_is_scheduled_even_when_nothing_was_registered(self, monkeypatch):
        # A re-registration whose manifest does not declare an agent (or that
        # follows a prune) writes nothing. Skipping the rescan there would leave
        # the removed name dispatchable in memory, and kiro-cli would silently
        # fall back to its own default for a name it cannot load.
        from kiro_crew.apps import bridges

        calls: list[str] = []
        monkeypatch.setattr(
            bridges, "schedule_materialized_agents_refresh", lambda: calls.append("refresh")
        )
        monkeypatch.setattr(
            bridges,
            "publish_materialized_agents",
            lambda names: calls.append("publish"),
        )

        out = bridges._register_agents("someapp", SimpleNamespace(agents=[]), Path("/nonexistent"))

        assert out == []
        # Nothing to publish, but the directory is still reconciled.
        assert calls == ["refresh"]


class TestRefreshAppAgentsReportsIoFailures:
    """A partial agent rewrite is not a reconciled one.

    `_register_agents` skips a failing agent and continues, so a write that hit ENOSPC
    left the agent JSON holding the dead MCP url while the caller was told the refresh
    succeeded. Only the I/O class is collected: this function returns an empty list for
    several PERMANENT reasons — a self-managed app, a denied one, no declared agents —
    and a caller that retried on emptiness would spin forever on any of them.
    """

    def _wire(self, monkeypatch, brmod, tmp_path, *, agents=("a.json",)):
        monkeypatch.setattr(
            brmod,
            "get_app_manifest",
            lambda name: SimpleNamespace(agents=list(agents)),
        )
        monkeypatch.setattr(brmod, "get_app", lambda name: {"resources": "gateway"})
        monkeypatch.setattr(brmod, "_app_resource_root", lambda name: tmp_path)
        monkeypatch.setattr(
            brmod,
            "_registration_denied",
            lambda name, action, app_root: None,
        )

    def test_an_io_failure_is_collected(self, monkeypatch, tmp_path):
        import kiro_crew.apps.bridges as brmod

        self._wire(monkeypatch, brmod, tmp_path)

        def _fake(app_name, manifest, app_root, io_failures=None):
            if io_failures is not None:
                io_failures.append("app--agent.json")
            return []

        monkeypatch.setattr(brmod, "_register_agents", _fake)

        collected: list[str] = []
        brmod.refresh_app_agents("app", io_failures=collected)
        assert collected == ["app--agent.json"]

    def test_a_permanent_skip_collects_nothing(self, monkeypatch, tmp_path):
        # An unsafe agent name or malformed spec registers nothing and never will.
        import kiro_crew.apps.bridges as brmod

        self._wire(monkeypatch, brmod, tmp_path)
        monkeypatch.setattr(
            brmod,
            "_register_agents",
            lambda app_name, manifest, app_root, io_failures=None: [],
        )

        collected: list[str] = []
        brmod.refresh_app_agents("app", io_failures=collected)
        assert collected == []

    def test_a_self_managed_app_is_never_materialized(self, monkeypatch, tmp_path):
        # `resources="app"` means the app registers its own agents; the gateway
        # publishing them too is duplicate dispatchable configuration.
        import kiro_crew.apps.bridges as brmod

        self._wire(monkeypatch, brmod, tmp_path)
        monkeypatch.setattr(brmod, "get_app", lambda name: {"resources": "app"})
        monkeypatch.setattr(
            brmod,
            "_register_agents",
            lambda *a, **k: pytest.fail("must not materialize a self-managed app"),
        )

        collected: list[str] = []
        assert brmod.refresh_app_agents("app", io_failures=collected) == []
        assert collected == []  # nothing to do is not a failure

    def test_a_denied_app_has_its_agents_scrubbed_not_rewritten(self, monkeypatch, tmp_path):
        # Rewriting a revoked app's agents would make them dispatchable again.
        import kiro_crew.apps.bridges as brmod

        self._wire(monkeypatch, brmod, tmp_path)
        monkeypatch.setattr(brmod, "_registration_denied", lambda name, action, app_root: "revoked")
        scrubbed: list[str] = []
        monkeypatch.setattr(brmod, "_deregister_agents", lambda name: scrubbed.append(name))
        monkeypatch.setattr(
            brmod,
            "_register_agents",
            lambda *a, **k: pytest.fail("must not re-register a denied app"),
        )

        collected: list[str] = []
        assert brmod.refresh_app_agents("app", io_failures=collected) == []
        assert scrubbed == ["app"]
        assert collected == []


class TestRenderFailureIsClassifiedByCause:
    """`_render_shipped_agent` returns None for three reasons; one is retryable.

    An unresolved placeholder and invalid rendered JSON are properties of the template
    and fail identically forever — reporting them to a caller that retries would spin
    without converging. Only the write failure can succeed later.
    """

    def _template(self, tmp_path, body: str):
        src = tmp_path / "agent.json"
        src.write_text(body, encoding="utf-8")
        return src

    def test_a_write_failure_is_collected(self, monkeypatch, tmp_path):
        import kiro_crew.apps.bridges as brmod

        src = self._template(tmp_path, '{"name": "a", "root": "{ENGINE_ROOT}"}')
        monkeypatch.setattr(brmod, "_placeholder_values", lambda n: {"{ENGINE_ROOT}": "/x"})
        monkeypatch.setattr(brmod, "_kiro_agents_dir", lambda: tmp_path / "agents")

        def _boom(target, data):
            raise OSError("ENOSPC")

        monkeypatch.setattr(brmod, "atomic_write", _boom)

        collected: list[str] = []
        assert brmod._render_shipped_agent("app", src, io_failures=collected) is None
        assert collected == [str(src)]

    def test_an_unresolved_placeholder_is_not_collected(self, monkeypatch, tmp_path):
        import kiro_crew.apps.bridges as brmod

        src = self._template(tmp_path, '{"name": "a", "root": "{ENGINE_ROOT}"}')
        monkeypatch.setattr(brmod, "_placeholder_values", lambda n: {})  # nothing resolves

        collected: list[str] = []
        assert brmod._render_shipped_agent("app", src, io_failures=collected) is None
        assert collected == []  # permanent: retrying never converges

    def test_invalid_rendered_json_is_not_collected(self, monkeypatch, tmp_path):
        import kiro_crew.apps.bridges as brmod

        src = self._template(tmp_path, '{"name": "a", "root": "{ENGINE_ROOT}"')  # unbalanced
        monkeypatch.setattr(brmod, "_placeholder_values", lambda n: {"{ENGINE_ROOT}": "/x"})

        collected: list[str] = []
        assert brmod._render_shipped_agent("app", src, io_failures=collected) is None
        assert collected == []
