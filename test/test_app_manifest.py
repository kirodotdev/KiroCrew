"""Tests for kiro_crew.apps.manifest — AppManifest parser and validator."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import make_escaping_link
from kiro_crew.apps.manifest import (
    AppManifest,
    CapabilityDependencies,
    Dependencies,
    SetupConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_manifest(**overrides) -> dict:
    """Return a minimal valid manifest dict with optional overrides."""
    base = {
        "name": "test-app",
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
        "author": "tester",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_minimal(self):
        m = AppManifest.from_dict(_valid_manifest())
        assert m.validate() == []

    def test_missing_name(self):
        m = AppManifest.from_dict(_valid_manifest(name=""))
        errors = m.validate()
        assert any("name" in e for e in errors)

    def test_missing_version(self):
        m = AppManifest.from_dict(_valid_manifest(version=""))
        errors = m.validate()
        assert any("version" in e for e in errors)

    def test_missing_display_name(self):
        m = AppManifest.from_dict(_valid_manifest(displayName=""))
        errors = m.validate()
        assert any("displayName" in e for e in errors)

    def test_missing_description(self):
        m = AppManifest.from_dict(_valid_manifest(description=""))
        errors = m.validate()
        assert any("description" in e for e in errors)

    def test_invalid_name_format(self):
        m = AppManifest.from_dict(_valid_manifest(name="Not_Kebab"))
        errors = m.validate()
        assert any("kebab-case" in e for e in errors)

    def test_invalid_version_format(self):
        m = AppManifest.from_dict(_valid_manifest(version="not-semver"))
        errors = m.validate()
        assert any("semver" in e for e in errors)

    def test_path_traversal_agents(self):
        m = AppManifest.from_dict(_valid_manifest(agents=["../evil.json"]))
        errors = m.validate()
        assert any("path traversal" in e for e in errors)

    def test_path_traversal_skills(self):
        m = AppManifest.from_dict(_valid_manifest(skills=["../../etc"]))
        errors = m.validate()
        assert any("path traversal" in e for e in errors)

    def test_path_traversal_ui_entry(self):
        m = AppManifest.from_dict(
            _valid_manifest(
                ui={"pages": [{"route": "/x", "label": "X", "entryPoint": "../bad.js"}]}
            )
        )
        errors = m.validate()
        assert any("path traversal" in e for e in errors)

    def test_path_traversal_backend_entrypoint(self):
        m = AppManifest.from_dict(_valid_manifest(backend={"entryPoint": "../../etc/evil.py"}))
        errors = m.validate()
        assert any("path traversal" in e for e in errors)

    def test_absolute_path_agents(self):
        m = AppManifest.from_dict(_valid_manifest(agents=["/etc/passwd"]))
        errors = m.validate()
        assert any("path traversal" in e for e in errors)

    def test_absolute_backend_entrypoint(self):
        m = AppManifest.from_dict(_valid_manifest(backend={"entryPoint": "/tmp/evil.py"}))
        errors = m.validate()
        assert any("path traversal" in e for e in errors)

    def test_module_style_entrypoint_ok(self):
        # A dotted module-style backend entryPoint has no '..' and is not
        # absolute, so the containment helper must not false-positive on it.
        m = AppManifest.from_dict(
            _valid_manifest(backend={"entryPoint": "kiro_crew.apps.builtins.x.server"})
        )
        assert m.validate() == []

    def test_canonical_containment_with_app_root(self, tmp_path):
        # A link whose target escapes the app root must be flagged when
        # app_root is known; a plain relative path inside the root passes.
        app_root = tmp_path / "app"
        app_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("x = 1\n")
        (app_root / "ok.py").write_text("y = 2\n")
        entry_point = make_escaping_link(app_root, outside)

        escaping = AppManifest.from_dict(_valid_manifest(backend={"entryPoint": entry_point}))
        errors = escaping.validate(app_root=app_root)
        assert any("path traversal" in e for e in errors)

        contained = AppManifest.from_dict(_valid_manifest(backend={"entryPoint": "ok.py"}))
        assert contained.validate(app_root=app_root) == []

    @pytest.mark.parametrize(
        "entry",
        [
            "/tmp/evil.py",  # POSIX-absolute
            "\\\\server\\share\\evil.py",  # UNC
            "C:/evil.py",  # drive + root, forward slashes
            "C:\\evil.py",  # drive + root, backslashes
            "D:evil.py",  # drive-RELATIVE: no root, but relocates the join
            "..\\evil.py",  # backslash traversal
            "../evil.py",  # forward-slash traversal
            "ui/../../evil.py",  # traversal in a non-leading segment
        ],
    )
    def test_rooted_or_traversing_entrypoint_rejected(self, entry, tmp_path):
        # Rooted and traversing paths must be refused identically whether or not
        # app_root is known, and on either host OS -- an app-resource path is
        # joined onto the app root, so anything carrying a drive, a root anchor
        # or a ".." segment can relocate that join. Asserting BOTH call forms is
        # what pins host-independence: a manifest is portable data validated on
        # whichever host installs the app, and "..\evil.py" resolves *inside* a
        # POSIX app_root, so a validator that leaned on canonical containment
        # for traversal would accept on POSIX what it rejects on Windows.
        m = AppManifest.from_dict(_valid_manifest(backend={"entryPoint": entry}))
        assert any("path traversal" in e for e in m.validate())
        assert any("path traversal" in e for e in m.validate(app_root=tmp_path))

    @pytest.mark.parametrize(
        "entry",
        [
            "index.mjs",
            "backend/server.py",
            "ui\\index.mjs",  # backslash separator is not a traversal
            "kiro_crew.apps.builtins.x.server",  # dotted module-style
            "a..b/c.py",  # ".." inside a segment, not a segment itself
        ],
    )
    def test_plain_relative_entrypoint_accepted(self, entry):
        # Guards the flip side of the containment rule: widening it must not
        # start refusing the ordinary relative paths every shipped app declares.
        m = AppManifest.from_dict(_valid_manifest(backend={"entryPoint": entry}))
        assert m.validate() == []

    def test_cron_missing_name(self):
        m = AppManifest.from_dict(_valid_manifest(crons=[{"every": 60, "message": "hi"}]))
        errors = m.validate()
        assert any("cron" in e and "name" in e for e in errors)

    def test_cron_missing_schedule(self):
        m = AppManifest.from_dict(_valid_manifest(crons=[{"name": "job1"}]))
        errors = m.validate()
        assert any("every" in e or "cron_expr" in e for e in errors)

    def test_cron_enabled_non_boolean_rejected(self):
        # The string "false" is truthy under bool() — a type slip here would
        # silently re-enable a disabled-by-design cron. Manifest validation
        # must reject non-boolean values with a clear error.
        m = AppManifest.from_dict(
            _valid_manifest(
                crons=[{"name": "j1", "every": 300, "message": "go", "enabled": "false"}]
            )
        )
        errors = m.validate()
        assert any("'enabled' must be a JSON boolean" in e for e in errors)
        # The flagged manifest must not accidentally register the cron
        # disabled either — the parse falls back to the default.
        assert m.crons[0].enabled is True

    def test_cron_enabled_boolean_values_accepted(self):
        for value, expected in ((False, False), (True, True)):
            m = AppManifest.from_dict(
                _valid_manifest(
                    crons=[{"name": "j1", "every": 300, "message": "go", "enabled": value}]
                )
            )
            assert m.validate() == []
            assert m.crons[0].enabled is expected
        # Absent key: default enabled, no error.
        m = AppManifest.from_dict(
            _valid_manifest(crons=[{"name": "j1", "every": 300, "message": "go"}])
        )
        assert m.validate() == []
        assert m.crons[0].enabled is True

    def test_ui_page_missing_route(self):
        m = AppManifest.from_dict(_valid_manifest(ui={"pages": [{"label": "X"}]}))
        errors = m.validate()
        assert any("route" in e for e in errors)

    def test_ui_page_missing_label(self):
        m = AppManifest.from_dict(_valid_manifest(ui={"pages": [{"route": "/x"}]}))
        errors = m.validate()
        assert any("label" in e for e in errors)

    def test_ui_page_icon_inactive_url_roundtrips(self):
        # The optional INACTIVE-state icon variant (a muted/dark image the sidebar
        # swaps in when the nav row is not active) survives from_dict -> to_dict,
        # and is omitted when unset (back-compat with manifests that lack it).
        m = AppManifest.from_dict(
            _valid_manifest(
                ui={
                    "pages": [
                        {
                            "route": "/x",
                            "label": "X",
                            "iconUrl": "icon.svg",
                            "iconInactiveUrl": "icon-inactive.svg",
                        }
                    ]
                }
            )
        )
        page = m.ui.pages[0]
        assert page.iconInactiveUrl == "icon-inactive.svg"
        assert page.to_dict()["iconInactiveUrl"] == "icon-inactive.svg"
        bare = AppManifest.from_dict(
            _valid_manifest(ui={"pages": [{"route": "/x", "label": "X"}]})
        ).ui.pages[0]
        assert "iconInactiveUrl" not in bare.to_dict()

    def test_valid_with_all_fields(self):
        m = AppManifest.from_dict(
            {
                "name": "oncall-watchtower",
                "version": "0.2.0",
                "displayName": "Oncall Watch Tower",
                "description": "Unified oncall dashboard",
                "author": "zezhexu",
                "license": "MIT",
                "minKiroCrewVersion": "1.3.0",
                "agents": ["agents/ticket-analyst.json"],
                "skills": ["skills/ticket-triage"],
                "sops": ["sops/ticket-rca.sop.md"],
                "mcpServers": {"cw-mcp": {"command": "capmgr", "args": ["mcp", "run", "cw"]}},
                "crons": [{"name": "refresh", "every": 3600, "message": "refresh data"}],
                "ui": {"pages": [{"route": "/apps/owt", "label": "Dashboard", "icon": "Shield"}]},
                "backend": {"entryPoint": "backend/app.py"},
                "permissions": {"mcpTools": ["GetPipelineHealth"], "storage": True},
                "setup": {"onInstall": "backend/setup.py:on_install"},
                "tags": ["oncall"],
                "jobFamilies": ["SDE"],
            }
        )
        assert m.validate() == []
        assert m.name == "oncall-watchtower"
        assert len(m.crons) == 1
        assert len(m.ui.pages) == 1
        assert m.permissions.storage is True


# ---------------------------------------------------------------------------
# Serialization round-trip tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_minimal_round_trip(self):
        original = _valid_manifest()
        m = AppManifest.from_dict(original)
        serialized = m.to_dict()
        m2 = AppManifest.from_dict(serialized)
        assert m2.to_dict() == serialized

    def test_full_round_trip(self):
        original = {
            "name": "my-app",
            "version": "2.1.0",
            "displayName": "My App",
            "description": "Does things",
            "author": "dev",
            "license": "Apache-2.0",
            "minKiroCrewVersion": "2.0.0",
            "agents": ["agents/a.json", "agents/b.json"],
            "skills": ["skills/s1"],
            "sops": ["sops/s.sop.md"],
            "mcpServers": {"srv": {"command": "run"}},
            "crons": [{"name": "j1", "every": 300, "agent": "a", "message": "go"}],
            "ui": {
                "pages": [
                    {
                        "route": "/apps/my-app",
                        "label": "Main",
                        "icon": "Star",
                        "entryPoint": "ui/bundle.js",
                        "mountFunction": "mountMain",
                    }
                ],
                "sidebar": {"section": "Tools", "order": 5},
            },
            "backend": {
                "entryPoint": "backend/app.py",
                "port": "9000",
                "healthCheck": "/ping",
                "routes": "/api/apps/my-app",
            },
            "permissions": {
                "mcpTools": ["ToolA"],
                "storage": True,
                "network": True,
                "memory": "shared",
                "cron": True,
            },
            "setup": {
                "onInstall": "setup.py:init",
                "configSchema": {"type": "object", "properties": {"key": {"type": "string"}}},
            },
            "tags": ["dev", "tools"],
            "jobFamilies": ["SDE", "SDM"],
        }
        m = AppManifest.from_dict(original)
        serialized = json.loads(m.to_json())
        m2 = AppManifest.from_dict(serialized)
        assert m2.to_dict() == m.to_dict()

    def test_extra_fields_preserved(self):
        data = _valid_manifest(customField="hello", anotherOne=42)
        m = AppManifest.from_dict(data)
        assert m.extra == {"customField": "hello", "anotherOne": 42}
        serialized = m.to_dict()
        assert serialized["customField"] == "hello"
        assert serialized["anotherOne"] == 42
        # Round-trip preserves extra
        m2 = AppManifest.from_dict(serialized)
        assert m2.extra == m.extra


# ---------------------------------------------------------------------------
# Parsing edge cases
# ---------------------------------------------------------------------------


class TestParsing:
    def test_from_empty_dict(self):
        m = AppManifest.from_dict({})
        assert m.name == ""
        assert m.version == ""
        errors = m.validate()
        assert len(errors) >= 4  # all 4 required fields missing

    def test_crons_non_dict_entries_skipped(self):
        m = AppManifest.from_dict(
            _valid_manifest(crons=["not-a-dict", {"name": "ok", "every": 60}])
        )
        assert len(m.crons) == 1
        assert m.crons[0].name == "ok"

    def test_ui_non_dict_ignored(self):
        m = AppManifest.from_dict(_valid_manifest(ui="not-a-dict"))
        assert m.ui.pages == []

    def test_backend_non_dict_ignored(self):
        m = AppManifest.from_dict(_valid_manifest(backend="not-a-dict"))
        assert m.backend.entryPoint == ""

    def test_from_json_file(self, tmp_path):
        data = _valid_manifest()
        p = tmp_path / "app.json"
        p.write_text(json.dumps(data))
        m = AppManifest.from_json_file(p)
        assert m.name == "test-app"
        assert m.validate() == []

    def test_from_json_file_not_object(self, tmp_path):
        p = tmp_path / "app.json"
        p.write_text(json.dumps([1, 2, 3]))
        with pytest.raises(ValueError, match="JSON object"):
            AppManifest.from_json_file(p)


# ---------------------------------------------------------------------------
# Property-based tests (hypothesis)
# ---------------------------------------------------------------------------

# Strategy for valid kebab-case names
_kebab_name = st.from_regex(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", fullmatch=True).filter(
    lambda s: 1 <= len(s) <= 60
)

# Strategy for semver strings
_semver = st.tuples(st.integers(0, 99), st.integers(0, 99), st.integers(0, 99)).map(
    lambda t: f"{t[0]}.{t[1]}.{t[2]}"
)

# Strategy for simple JSON-safe extra values
_extra_value = st.one_of(
    st.text(max_size=20),
    st.integers(-1000, 1000),
    st.booleans(),
    st.lists(st.text(max_size=10), max_size=5),
)


class TestPropertyBased:

    @given(
        name=st.one_of(st.just(""), _kebab_name),
        version=st.one_of(st.just(""), _semver),
        display_name=st.one_of(st.just(""), st.text(min_size=1, max_size=30)),
        description=st.one_of(st.just(""), st.text(min_size=1, max_size=50)),
    )
    @settings(max_examples=100)
    def test_validation_detects_missing_required_fields(
        self, name: str, version: str, display_name: str, description: str
    ):
        """Property 1: validate() returns an error for each missing required field."""
        m = AppManifest(
            name=name,
            version=version,
            displayName=display_name,
            description=description,
        )
        errors = m.validate()
        if not name:
            assert any("name" in e for e in errors)
        if not version:
            assert any("version" in e for e in errors)
        if not display_name:
            assert any("displayName" in e for e in errors)
        if not description:
            assert any("description" in e for e in errors)

    @given(
        name=_kebab_name,
        version=_semver,
        display_name=st.text(min_size=1, max_size=30),
        description=st.text(min_size=1, max_size=50),
        extra_keys=st.lists(
            st.text(
                alphabet=st.characters(whitelist_categories=("L", "N")),
                min_size=1,
                max_size=15,
            ).filter(
                lambda k: k
                not in {
                    "name",
                    "version",
                    "displayName",
                    "description",
                    "author",
                    "license",
                    "minKiroCrewVersion",
                    "agents",
                    "skills",
                    "sops",
                    "mcpServers",
                    "crons",
                    "ui",
                    "backend",
                    "permissions",
                    "setup",
                    "tags",
                    "jobFamilies",
                }
            ),
            max_size=5,
            unique=True,
        ),
        extra_vals=st.lists(_extra_value, max_size=5),
    )
    @settings(max_examples=100)
    def test_serialization_round_trip(
        self,
        name: str,
        version: str,
        display_name: str,
        description: str,
        extra_keys: list[str],
        extra_vals: list,
    ):
        """Property 2: from_dict(json.loads(to_json())) produces equivalent to_dict()."""
        extra = dict(zip(extra_keys, extra_vals))
        data = {
            "name": name,
            "version": version,
            "displayName": display_name,
            "description": description,
            **extra,
        }
        m1 = AppManifest.from_dict(data)
        serialized = json.loads(m1.to_json())
        m2 = AppManifest.from_dict(serialized)
        assert m2.to_dict() == m1.to_dict()


# ---------------------------------------------------------------------------
# SetupConfig lifecycle hooks tests
# ---------------------------------------------------------------------------


class TestSetupConfigHooks:
    def test_new_hooks_round_trip(self):
        cfg = SetupConfig(
            onInstall="bash setup.sh",
            onUpdate="bash update.sh",
            onUninstall="bash uninstall.sh",
            onEnable="bash enable.sh",
            onDisable="bash disable.sh",
        )
        d = cfg.to_dict()
        assert d["onUpdate"] == "bash update.sh"
        assert d["onEnable"] == "bash enable.sh"
        assert d["onDisable"] == "bash disable.sh"
        restored = SetupConfig.from_dict(d)
        assert restored.onUpdate == cfg.onUpdate
        assert restored.onEnable == cfg.onEnable
        assert restored.onDisable == cfg.onDisable

    def test_empty_hooks_omitted(self):
        cfg = SetupConfig(onInstall="bash setup.sh")
        d = cfg.to_dict()
        assert "onUpdate" not in d
        assert "onEnable" not in d
        assert "onDisable" not in d

    def test_configurable_timeouts(self):
        cfg = SetupConfig(onEnable="bash e.sh", onEnableTimeout=120, onDisableTimeout=60)
        d = cfg.to_dict()
        assert d["onEnableTimeout"] == 120
        assert d["onDisableTimeout"] == 60
        restored = SetupConfig.from_dict(d)
        assert restored.onEnableTimeout == 120
        assert restored.onDisableTimeout == 60

    def test_default_timeouts_omitted(self):
        cfg = SetupConfig(onEnable="bash e.sh")
        d = cfg.to_dict()
        assert "onEnableTimeout" not in d
        assert "onDisableTimeout" not in d

    def test_manifest_with_new_hooks(self):
        m = AppManifest.from_dict(
            _valid_manifest(
                setup={
                    "onInstall": "bash setup.sh",
                    "onUpdate": "bash update.sh",
                    "onEnable": "bash enable.sh",
                    "onDisable": "bash disable.sh",
                    "onEnableTimeout": 90,
                }
            )
        )
        assert m.setup.onUpdate == "bash update.sh"
        assert m.setup.onEnable == "bash enable.sh"
        assert m.setup.onEnableTimeout == 90


# ---------------------------------------------------------------------------
# Dependencies tests
# ---------------------------------------------------------------------------


class TestDependencies:
    def test_empty_dependencies(self):
        deps = Dependencies.from_dict({})
        assert deps.managedBy == "gateway"
        assert deps.capabilities.mcp == []
        assert deps.commands == []

    def test_full_dependencies_round_trip(self):
        data = {
            "managedBy": "app",
            "capabilities": {
                "mcp": ["aws-docs-mcp"],
                "skills": ["SomeSkill"],
                "agents": ["SomeAgent"],
            },
            "commands": ["node", "python3"],
        }
        deps = Dependencies.from_dict(data)
        assert deps.managedBy == "app"
        assert deps.capabilities.mcp == ["aws-docs-mcp"]
        assert deps.commands == ["node", "python3"]
        d = deps.to_dict()
        restored = Dependencies.from_dict(d)
        assert restored.managedBy == deps.managedBy
        assert restored.capabilities.mcp == deps.capabilities.mcp
        assert restored.commands == deps.commands

    def test_default_managed_by_omitted(self):
        deps = Dependencies(capabilities=CapabilityDependencies(mcp=["x"]))
        d = deps.to_dict()
        assert "managedBy" not in d  # default "gateway" omitted

    def test_optional_commands_survive_the_round_trip(self):
        """The field was declared by two shipped manifests and read by nobody.

        `from_dict` ignored `optionalCommands`, so `papyrus` — whose ONLY
        dependency declaration is that key — round-tripped to `{}` and its
        "needs pdflatex or tectonic" requirement was invisible to every consumer.
        """
        deps = Dependencies.from_dict({"optionalCommands": ["pdflatex", "tectonic"]})
        assert deps.optionalCommands == ["pdflatex", "tectonic"]
        assert deps.to_dict() == {"optionalCommands": ["pdflatex", "tectonic"]}

    def test_optional_commands_are_independent_of_required_ones(self):
        deps = Dependencies.from_dict(
            {"commands": ["gh"], "optionalCommands": ["glab"]}
        )
        assert deps.commands == ["gh"]
        assert deps.optionalCommands == ["glab"]
        restored = Dependencies.from_dict(deps.to_dict())
        assert restored.commands == ["gh"]
        assert restored.optionalCommands == ["glab"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"optionalCommands": None},
            {"commands": None},
            {"commands": None, "optionalCommands": None},
        ],
    )
    def test_a_json_null_list_degrades_to_empty(self, payload):
        """A manifest is UNTRUSTED input, so a null list must not crash the parser.

        `.get(key, [])` returns `None` for an explicit `"commands": null` — the
        default only applies to an ABSENT key — and the comprehension then raised
        `TypeError`, which the install endpoint surfaced as an unhandled 500 instead
        of a validation error. A hand-written or generated app.json can easily carry
        a JSON null for an empty list.
        """
        deps = Dependencies.from_dict(payload)
        assert deps.commands == []
        assert deps.optionalCommands == []
        assert deps.to_dict() == {}

    def test_every_shipped_builtin_manifest_keeps_its_declared_commands(self):
        """No shipped manifest may declare a dependency key the parser drops.

        A guard rather than two literal assertions: the failure mode here was
        silent, so the useful thing to pin is the general property.
        """
        import json
        from pathlib import Path

        from kiro_crew.apps.manifest import AppManifest

        builtins = Path("src/kiro_crew/apps/builtins")
        for app_json in sorted(builtins.glob("*/app.json")):
            raw = json.loads(app_json.read_text(encoding="utf-8"))
            declared = raw.get("dependencies") or {}
            if not declared:
                continue
            parsed = AppManifest.from_dict(raw).dependencies
            for key in ("commands", "optionalCommands"):
                assert list(declared.get(key, [])) == list(
                    getattr(parsed, key)
                ), f"{app_json.parent.name}: {key} was dropped by the parser"

    def test_mixed_string_and_object_entries(self):
        deps = Dependencies.from_dict(
            {
                "capabilities": {
                    "mcp": [
                        "simple-mcp",
                        {"id": "custom-mcp", "managedBy": "app"},
                    ]
                }
            }
        )
        assert len(deps.capabilities.mcp) == 2
        assert deps.capabilities.mcp[0] == "simple-mcp"
        assert deps.capabilities.mcp[1] == {"id": "custom-mcp", "managedBy": "app"}

    def test_manifest_with_dependencies(self):
        m = AppManifest.from_dict(
            _valid_manifest(
                dependencies={
                    "managedBy": "gateway",
                    "capabilities": {"mcp": ["aws-docs"]},
                    "commands": ["node"],
                }
            )
        )
        assert m.dependencies.managedBy == "gateway"
        assert m.dependencies.capabilities.mcp == ["aws-docs"]
        assert m.dependencies.commands == ["node"]
        # Round-trip through manifest
        d = m.to_dict()
        assert "dependencies" in d
        m2 = AppManifest.from_dict(d)
        assert m2.dependencies.capabilities.mcp == ["aws-docs"]


# ---------------------------------------------------------------------------
# Property tests for new dataclasses
# ---------------------------------------------------------------------------


class TestSignatureFields:
    def test_signature_fields_roundtrip(self):
        m = AppManifest.from_dict(
            _valid_manifest(
                signer="acme",
                signature="deadbeef",
            )
        )
        assert m.signer == "acme"
        assert m.signature == "deadbeef"
        d = m.to_dict()
        assert d["signer"] == "acme"
        assert d["signature"] == "deadbeef"
        m2 = AppManifest.from_dict(d)
        assert m2.signer == "acme"
        assert m2.signature == "deadbeef"

    def test_signature_fields_omitted_when_empty(self):
        m = AppManifest.from_dict(_valid_manifest())
        d = m.to_dict()
        assert "signer" not in d
        assert "signature" not in d

    def test_signing_payload_stable(self):
        # Payload is deterministic regardless of source dict field ordering and
        # is independent of the signature field itself.
        base = _valid_manifest(
            signer="acme",
            signature="sig-A",
            permissions={"mcpTools": ["B", "A"], "network": True},
        )
        m1 = AppManifest.from_dict(base)
        reordered = {k: base[k] for k in reversed(list(base.keys()))}
        m2 = AppManifest.from_dict(reordered)
        assert m1.signing_payload() == m2.signing_payload()

        # Changing the signature does NOT change the signed payload.
        m3 = AppManifest.from_dict(
            _valid_manifest(
                signer="acme",
                signature="sig-B",
                permissions={"mcpTools": ["B", "A"], "network": True},
            )
        )
        assert m1.signing_payload() == m3.signing_payload()
        assert isinstance(m1.signing_payload(), bytes)


class TestManifestNewProperties:
    # Feature: app-classification-redesign, Property 3: Manifest dataclass serialisation round-trips
    @given(
        on_install=st.text(max_size=30),
        on_update=st.text(max_size=30),
        on_uninstall=st.text(max_size=30),
        on_enable=st.text(max_size=30),
        on_disable=st.text(max_size=30),
        enable_timeout=st.integers(1, 600),
        disable_timeout=st.integers(1, 600),
    )
    @settings(max_examples=200)
    def test_setup_config_round_trip_property(
        self,
        on_install,
        on_update,
        on_uninstall,
        on_enable,
        on_disable,
        enable_timeout,
        disable_timeout,
    ):
        """**Validates: Requirements 4.2**"""
        cfg = SetupConfig(
            onInstall=on_install,
            onUpdate=on_update,
            onUninstall=on_uninstall,
            onEnable=on_enable,
            onDisable=on_disable,
            onEnableTimeout=enable_timeout,
            onDisableTimeout=disable_timeout,
        )
        d = cfg.to_dict()
        restored = SetupConfig.from_dict(d)
        assert restored.onInstall == cfg.onInstall
        assert restored.onUpdate == cfg.onUpdate
        assert restored.onUninstall == cfg.onUninstall
        assert restored.onEnable == cfg.onEnable
        assert restored.onDisable == cfg.onDisable
        assert restored.onEnableTimeout == cfg.onEnableTimeout
        assert restored.onDisableTimeout == cfg.onDisableTimeout

    # Feature: app-classification-redesign, Property 3: Dependencies serialisation round-trips
    @given(
        managed_by=st.sampled_from(["gateway", "app"]),
        mcp_deps=st.lists(st.from_regex(r"[a-z][a-z0-9\-]{0,20}", fullmatch=True), max_size=5),
        skill_deps=st.lists(
            st.from_regex(r"[A-Za-z][A-Za-z0-9]{0,20}", fullmatch=True), max_size=5
        ),
        commands=st.lists(st.from_regex(r"[a-z][a-z0-9]{0,10}", fullmatch=True), max_size=5),
    )
    @settings(max_examples=200)
    def test_dependencies_round_trip_property(self, managed_by, mcp_deps, skill_deps, commands):
        """**Validates: Requirements 5.2**"""
        deps = Dependencies(
            managedBy=managed_by,
            capabilities=CapabilityDependencies(mcp=mcp_deps, skills=skill_deps),
            commands=commands,
        )
        d = deps.to_dict()
        restored = Dependencies.from_dict(d)
        # Semantic equivalence: field values match even if dict structure differs
        assert restored.managedBy == deps.managedBy
        assert restored.capabilities.mcp == deps.capabilities.mcp
        assert restored.capabilities.skills == deps.capabilities.skills
        assert restored.commands == deps.commands

    # Feature: app-classification-redesign, Property 4: a single dependency can override managedBy
    @given(
        default_managed=st.sampled_from(["gateway", "app"]),
        override_managed=st.sampled_from(["gateway", "app"]),
    )
    @settings(max_examples=100)
    def test_managed_by_override_property(self, default_managed, override_managed):
        """**Validates: Requirements 5.5**"""
        deps = Dependencies.from_dict(
            {
                "managedBy": default_managed,
                "capabilities": {
                    "mcp": [
                        "simple-dep",
                        {"id": "override-dep", "managedBy": override_managed},
                    ]
                },
            }
        )
        # String entry uses default
        entry0 = deps.capabilities.mcp[0]
        assert isinstance(entry0, str)
        # Object entry preserves its own managedBy
        entry1 = deps.capabilities.mcp[1]
        assert isinstance(entry1, dict)
        assert entry1["managedBy"] == override_managed


class TestCapabilityDepTypesContract:
    """``CAPABILITY_DEP_TYPES`` drives the resolver loop via ``getattr``, so it
    must name the ``CapabilityDependencies`` fields exactly — a drift would
    silently resolve a whole dependency type to nothing."""

    def test_types_match_dataclass_fields(self):
        import dataclasses

        from kiro_crew.apps.dependency_ledger import CAPABILITY_DEP_TYPES

        fields = {f.name for f in dataclasses.fields(CapabilityDependencies)}
        assert set(CAPABILITY_DEP_TYPES) == fields

    def test_installable_subset_is_derived(self):
        from kiro_crew.apps.dependencies import _INSTALLABLE_TYPES
        from kiro_crew.apps.dependency_ledger import CAPABILITY_DEP_TYPES

        assert set(_INSTALLABLE_TYPES) <= set(CAPABILITY_DEP_TYPES)


class TestRequiresDesktopApp:
    """``platform.requiresDesktopApp`` — the surface axis, distinct from ``os``.

    ``os`` constrains the machine the gateway runs on; this constrains the
    surface the user views from (Electron shell vs browser tab). It is a UX
    gate, so the only contract worth pinning is that it round-trips faithfully
    and stays absent-by-default (an omitted flag must never read as True, or
    every app would silently become desktop-only).
    """

    def test_defaults_to_false(self):
        from kiro_crew.apps.manifest import PlatformConfig

        assert PlatformConfig().requiresDesktopApp is False
        assert PlatformConfig.from_dict({}).requiresDesktopApp is False

    def test_omitted_from_dict_when_false(self):
        from kiro_crew.apps.manifest import PlatformConfig

        # Absent-not-null: the wire form stays minimal, matching how the other
        # PlatformConfig fields serialize.
        assert "requiresDesktopApp" not in PlatformConfig().to_dict()

    def test_round_trips_when_true(self):
        from kiro_crew.apps.manifest import PlatformConfig

        cfg = PlatformConfig.from_dict({"requiresDesktopApp": True})
        assert cfg.requiresDesktopApp is True
        assert cfg.to_dict()["requiresDesktopApp"] is True
        assert PlatformConfig.from_dict(cfg.to_dict()).requiresDesktopApp is True

    def test_non_bool_values_are_coerced(self):
        from kiro_crew.apps.manifest import PlatformConfig

        # Manifests are user-authored JSON; a truthy string must not crash the
        # parse, and a falsy value must not enable the gate.
        assert PlatformConfig.from_dict({"requiresDesktopApp": "yes"}).requiresDesktopApp is True
        assert PlatformConfig.from_dict({"requiresDesktopApp": 0}).requiresDesktopApp is False
        assert PlatformConfig.from_dict({"requiresDesktopApp": None}).requiresDesktopApp is False

    def test_independent_of_os_axis(self):
        from kiro_crew.apps.manifest import PlatformConfig

        # Declaring a desktop surface must not narrow the gateway OS list.
        cfg = PlatformConfig.from_dict({"requiresDesktopApp": True})
        assert cfg.supports_platform("darwin") is True
        assert cfg.supports_platform("linux") is True

    def test_survives_full_manifest_round_trip(self):
        manifest = AppManifest.from_dict(_valid_manifest(platform={"requiresDesktopApp": True}))
        assert manifest.platform.requiresDesktopApp is True
        assert AppManifest.from_dict(manifest.to_dict()).platform.requiresDesktopApp is True

    def test_mochi_builtin_declares_it(self):
        """Mochi is the first consumer: its panel needs the Electron shell."""
        from pathlib import Path

        import kiro_crew.apps.builtins as builtins_pkg

        app_json = Path(builtins_pkg.__file__).parent / "mochi" / "app.json"
        manifest = AppManifest.from_dict(json.loads(app_json.read_text()))
        assert manifest.platform.requiresDesktopApp is True

    def test_windows_is_expressible(self):
        """KiroCrew runs natively on Windows, so a manifest must be able to say so.

        Without the mapping row `"windows"` was accepted into the list and then
        matched NOTHING — a declaring app was silently unsupported everywhere,
        which is the worst of both answers.
        """
        from kiro_crew.apps.manifest import PlatformConfig

        cfg = PlatformConfig(os=["macos", "linux", "windows"])
        assert cfg.supports_platform("win32") is True
        assert cfg.supports_platform("darwin") is True
        assert cfg.supports_platform("linux") is True

    def test_current_os_names_windows_in_the_manifest_vocabulary(self, monkeypatch):
        """`current_os()` must return a name manifests compare against, not `win32`."""
        from kiro_crew.apps import manifest as manifest_mod

        monkeypatch.setattr(manifest_mod.sys, "platform", "win32", raising=False)
        assert manifest_mod.PlatformConfig.current_os() == "windows"

    def test_the_default_still_excludes_windows(self):
        """Opt-in, not opt-out: widening the default would promise Windows for
        every existing app that never declared it."""
        from kiro_crew.apps.manifest import PlatformConfig

        assert PlatformConfig().supports_platform("win32") is False
