"""Tests for kiro_crew.apps.manifest — AppManifest parser and validator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import make_escaping_link
from kiro_crew.apps.manifest import (
    ARGUMENT_TOKEN,
    AppManifest,
    CapabilityDependencies,
    CommandArgument,
    Dependencies,
    SetupConfig,
)
from kiro_crew.constants import WINDOWS_DEVICE_STEMS

_REPO_ROOT = Path(__file__).resolve().parents[1]


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

    def test_reserved_name_rejected(self):
        """The system.* notification namespace stays un-shadowable."""
        m = AppManifest.from_dict(_valid_manifest(name="system"))
        errors = m.validate()
        assert any("reserved" in e for e in errors)

    @pytest.mark.parametrize("name", ["registry", "registries", "blob", "install", "register"])
    def test_literal_route_segment_name_rejected(self, name):
        """Issue #7111: the literal first-segment routes under /api/apps/ are
        shared routes registered before the /api/apps/{name} catch-all. Reserving
        these names is the defense-in-depth backstop that keeps NEW apps from
        claiming them (the token_auth carve-out is the primary boundary)."""
        from kiro_crew.apps.manifest import app_name_error, is_reserved_app_name

        reason = app_name_error(name)
        assert reason is not None
        assert "reserved" in reason
        assert is_reserved_app_name(name) is True
        m = AppManifest.from_dict(_valid_manifest(name=name))
        errors = m.validate()
        assert any("reserved" in e for e in errors), (name, errors)

    @pytest.mark.parametrize("name", ["file-explorer", "registry-viewer", "installer"])
    def test_names_merely_resembling_a_route_segment_stay_valid(self, name):
        """The reservation matches the exact literal segment only: kebab names
        that merely contain or resemble a segment must still validate."""
        from kiro_crew.apps.manifest import app_name_error, is_reserved_app_name

        assert app_name_error(name) is None
        assert is_reserved_app_name(name) is False
        m = AppManifest.from_dict(_valid_manifest(name=name))
        assert m.validate() == []

    @pytest.mark.parametrize("name", sorted(WINDOWS_DEVICE_STEMS))
    def test_every_windows_device_stem_is_rejected(self, name):
        """The whole documented device-name set, not just the stems that happen
        to fail on one build. An app name is a persistent published identity, so
        admitting a stem is a one-way door while over-refusing is freely
        relaxable."""
        m = AppManifest.from_dict(_valid_manifest(name=name))
        errors = m.validate()
        assert any("not portable" in e for e in errors), (name, errors)

    def test_device_stem_vocabulary_is_not_duplicated(self):
        """One definition, shared with the git-branch grammar. Two copies of a
        22-name set drift, and the branch rule is the precedent this follows."""
        from kiro_crew.apps.manifest import UNPORTABLE_APP_NAMES

        assert UNPORTABLE_APP_NAMES is WINDOWS_DEVICE_STEMS

    @pytest.mark.parametrize("name", ["null-app", "console", "com10", "lpt10", "connect"])
    def test_names_merely_resembling_a_device_stay_valid(self, name):
        """The rule matches the exact stem. ``com10``/``lpt10`` are outside the
        reserved 1-9 range and the rest are ordinary words."""
        m = AppManifest.from_dict(_valid_manifest(name=name))
        assert m.validate() == []

    @pytest.mark.parametrize("name", ["demo\n", "nul\n", "system\n", "demo\r\n", "demo\n\n"])
    def test_a_trailing_newline_cannot_slip_through(self, name):
        """``$`` also matches before a trailing newline, so a ``$``-anchored
        grammar admits ``"demo\\n"`` — and worse, ``"nul\\n"`` and ``"system\\n"``
        evade the reserved-name checks that run after it, because those compare
        against the exact string. ``KEBAB_RE`` is anchored with ``\\Z``."""
        m = AppManifest.from_dict(_valid_manifest(name=name))
        errors = m.validate()
        assert any("kebab-case" in e for e in errors), (name, errors)

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

        from kiro_crew.apps.manifest import AppManifest

        builtins = _REPO_ROOT / "src/kiro_crew/apps/builtins"
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


class TestScalarGrantDoesNotBecomeAWildcard:
    """A list-valued grant given a JSON SCALAR must deny, not be coerced.

    `[str(x) for x in value]` over a STRING iterates its characters, so a manifest
    that wrote a bare string where a list belongs was handed the tokens those
    characters spell -- including the wildcards each of these fields honours:

      exposeToApps  `"*"`         -> ["*"] -> every sibling app may observe my slots
      events        `"*"`         -> ["*"] -> the whole WS scope vocabulary
      api           `"/api/chat"` -> ["/", "a", ...] and `app_token_path_allowed`
                                     matches the prefix "/" against every path
      mcpTools      `"*"`         -> ["*"]

    Same defect class as `bool("false")` on the boolean grants above, and the same
    direction of fix: an unexpected value withholds the grant.
    """

    def test_scalar_star_never_yields_the_wildcard(self):
        from kiro_crew.apps.manifest import Permissions

        for field in ("exposeToApps", "events", "api", "mcpTools"):
            perms = Permissions.from_dict({field: "*"})
            assert getattr(perms, field) == [], (
                f"{field}: a scalar must not be exploded into a wildcard"
            )

    def test_scalar_path_never_yields_a_match_everything_prefix(self):
        from kiro_crew.apps.manifest import Permissions

        assert Permissions.from_dict({"api": "/api/chat"}).api == []

    def test_other_scalar_shapes_also_deny(self):
        from kiro_crew.apps.manifest import Permissions

        for value in (True, 1, {"a": "b"}, None):
            assert Permissions.from_dict({"exposeToApps": value}).exposeToApps == []

    def test_a_real_list_still_works(self):
        from kiro_crew.apps.manifest import Permissions

        perms = Permissions.from_dict(
            {
                "exposeToApps": ["mochi", "", "workflows"],
                "events": ["slots:own", "notification"],
                "api": ["/api/chat", "/api/ws"],
                "mcpTools": ["cron_add"],
            }
        )
        # Falsy entries are still dropped; everything else is preserved verbatim.
        assert perms.exposeToApps == ["mochi", "workflows"]
        assert perms.events == ["slots:own", "notification"]
        assert perms.api == ["/api/chat", "/api/ws"]
        assert perms.mcpTools == ["cron_add"]

    def test_the_wildcard_still_works_when_declared_as_a_list(self):
        from kiro_crew.apps.manifest import Permissions

        assert Permissions.from_dict({"exposeToApps": ["*"]}).exposeToApps == ["*"]
        assert Permissions.from_dict({"events": ["*"]}).events == ["*"]


# ---------------------------------------------------------------------------
# UI overlays
# ---------------------------------------------------------------------------


def _overlay_manifest(*overlays) -> AppManifest:
    return AppManifest.from_dict(_valid_manifest(ui={"overlays": list(overlays)}))


def test_overlay_parses_and_round_trips():
    decl = {"id": "command-bar", "replaces": "quick-search"}
    m = _overlay_manifest(decl)
    assert len(m.ui.overlays) == 1
    assert m.ui.overlays[0].id == "command-bar"
    assert m.ui.overlays[0].replaces == "quick-search"
    assert m.validate() == []
    # Round-trip must preserve the declaration, or the frontend stops seeing the slot.
    assert AppManifest.from_dict(m.to_dict()).ui.overlays == m.ui.overlays
    assert m.to_dict()["ui"]["overlays"] == [decl]


def test_overlay_without_overlays_key_is_empty_not_none():
    m = AppManifest.from_dict(_valid_manifest(ui={"pages": [{"route": "/x", "label": "X"}]}))
    assert m.ui.overlays == []
    assert "overlays" not in m.to_dict()["ui"]


def test_overlay_non_dict_entries_are_dropped():
    m = AppManifest.from_dict(_valid_manifest(ui={"overlays": ["nope", 7, None]}))
    assert m.ui.overlays == []


@pytest.mark.parametrize("value", [None, "overlays", 7, {"id": "x"}])
def test_overlay_non_list_value_parses_to_empty(value):
    """A present-but-not-a-list ``overlays`` must not raise.

    ``dict.get`` returns the stored value rather than the default when the key
    exists, so a hand-edited manifest carrying ``"overlays": null`` would otherwise
    iterate ``None`` and take install validation down with a TypeError.
    """
    m = AppManifest.from_dict(_valid_manifest(ui={"overlays": value}))
    assert m.ui.overlays == []
    assert m.validate() == []


@pytest.mark.parametrize(
    "decl,expected",
    [
        ({"replaces": "quick-search"}, "ui overlay missing required field: id"),
        (
            {"id": "Command-Bar", "replaces": "quick-search"},
            "ui overlay id must be kebab-case: 'Command-Bar'",
        ),
        ({"id": "-lead", "replaces": "quick-search"}, "ui overlay id must be kebab-case: '-lead'"),
        ({"id": "a/b", "replaces": "quick-search"}, "ui overlay id must be kebab-case: 'a/b'"),
        # An overlay is opened only through a slot the host owns, so a declaration
        # without one could never be shown.
        ({"id": "ok"}, "ui overlay 'ok' missing required field: replaces"),
    ],
)
def test_overlay_validation_rejects(decl, expected):
    assert expected in _overlay_manifest(decl).validate()


def test_overlay_duplicate_ids_rejected():
    decl = {"id": "dup", "replaces": "quick-search"}
    assert "ui overlay duplicate id: 'dup'" in _overlay_manifest(decl, dict(decl)).validate()


def test_overlay_replaces_must_be_a_slug():
    errors = _overlay_manifest({"id": "ok", "replaces": "Quick Search"}).validate()
    assert any("replaces must be kebab-case: 'Quick Search'" in e for e in errors)


def test_overlay_builtins_must_not_ship_a_ui_bundle():
    """A builtin declaring ``ui.overlays`` must not also declare ``ui.entry``.

    Slot ownership requires ``origin == "builtin"``, which is the only provenance a
    self-registering app cannot forge. But ``register_builtin_apps`` re-derives origin
    on every startup for an already-registered builtin and downgrades it to ``local``
    when the manifest carries a ``ui.entry`` bundle. Such an app would therefore be
    refused its own slot after the first restart, and the refusal surfaces only as a
    browser console warning. This is a build failure instead, because the combination
    is invisible at runtime until someone notices the surface silently reverted.
    """
    offenders = []
    for entry in sorted((_REPO_ROOT / "src/kiro_crew/apps/builtins").iterdir()):
        manifest_path = entry / "app.json"
        if not manifest_path.is_file():
            continue
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        ui = raw.get("ui") or {}
        if ui.get("overlays") and ui.get("entry"):
            offenders.append(raw.get("name"))
    assert not offenders, (
        "builtins declaring both ui.overlays and ui.entry (origin is downgraded to "
        f"'local' on restart, so the slot is refused): {offenders}"
    )


def test_command_bar_builtin_is_overlay_only_and_default_on():
    """The shipped Command Bar app is the first overlay-only builtin.

    Its shape is load-bearing: no page (it is not routed), no backend entry point (so
    being enabled spawns no process), and default-ON: the launcher owns the
    quick-search gesture on a fresh install, and the legacy palette is what a reader
    opts back into by disabling the app. The default-on decision itself is declared
    in ``manager._DEFAULT_ON_BUILTINS``; the opt-in policy tests read it from there.
    """
    raw = json.loads(
        (
            _REPO_ROOT / "src/kiro_crew/apps/builtins/command_bar/app.json"
        ).read_text(encoding="utf-8")
    )
    m = AppManifest.from_dict(raw)
    assert m.validate() == []
    assert raw["defaultEnabled"] is True
    assert m.ui.pages == []
    assert not m.backend.entryPoint
    assert [(o.id, o.replaces) for o in m.ui.overlays] == [("command-bar", "quick-search")]


class TestSignatureFrozenVocabulary:
    """The contribution vocabulary that a published signature makes unrenamable.

    ``contributes`` is inside ``signing_payload()``, so for any app that has already
    been SIGNED the canonical bytes of its commands are part of what the signature
    authenticates. Renaming an emitted key, or renaming a ``kind`` value, changes those
    bytes and invalidates the signature of an app nobody has touched. Adding a new key
    or a new kind does not, because emission is conditional and absent keys were never
    in the bytes.

    Three reviewers asked a human to confirm this vocabulary before the first signed use
    on the grounds that it cannot be changed afterwards. That is true, and it was being
    protected only by their having said so. These tests turn it into a gate: a rename
    fails HERE, with this docstring attached, instead of failing in a user's install
    months later.

    Deliberately NOT pinned: the caps. A cap is a validation threshold and appears
    nowhere in the payload, so raising or lowering one cannot invalidate a signature.
    They have their own invariant -- both validators must agree -- which the shared
    conformance fixture already enforces. Freezing them here would claim a
    compatibility consequence they do not have.
    """

    #: Every key `CommandContribution.to_dict()` can emit.
    COMMAND_KEYS = frozenset(
        {"id", "title", "subtitle", "icon", "keywords", "prompt", "autoSend", "argument"}
    )
    #: Every key `CommandArgument.to_dict()` can emit.
    ARGUMENT_KEYS = frozenset({"placeholder", "hint", "kind", "hosts", "patternError"})

    @staticmethod
    def _full_command() -> dict:
        return {
            "id": "approve-all",
            "title": "Approve all PRs",
            "subtitle": "Approve every pull request behind a link",
            "icon": "Check",
            "keywords": ["pr"],
            "prompt": "Approve everything behind {argument}.",
            "autoSend": True,
            "argument": {
                "placeholder": "Paste a link",
                "hint": "A PR search or a single pull request.",
                "kind": "url",
                "hosts": ["github.com"],
                "patternError": "Not a GitHub link.",
            },
        }

    def _serialized(self) -> dict:
        m = AppManifest.from_dict(
            {
                "name": "pr-bulk-ops",
                "version": "1.0.0",
                "displayName": "PR Bulk Ops",
                "description": "Bulk operations over the pull requests behind one link.",
                "contributes": {"commands": [self._full_command()]},
            }
        )
        assert m.validate() == [], m.validate()
        return m.to_dict()["contributes"]["commands"][0]

    def test_the_emitted_command_keys_are_exactly_these(self):
        # A RENAME shows up here as one key missing and one added, which is the change
        # that breaks an existing signature. A new OPTIONAL key shows up as an addition
        # only, and is compatible -- so read a failure by which direction it moved.
        assert set(self._serialized()) == self.COMMAND_KEYS

    def test_the_emitted_argument_keys_are_exactly_these(self):
        assert set(self._serialized()["argument"]) == self.ARGUMENT_KEYS

    def test_the_kind_names_are_frozen(self):
        # `kind` is emitted VERBATIM, so its values are part of the signed bytes in a way
        # the other fields' values are not: renaming `url` to `link` would invalidate
        # every signed app that declared a url argument. Adding a third kind is fine.
        assert CommandArgument.KINDS == ("url", "text")

    def test_the_argument_token_spelling_is_frozen(self):
        # The token lives inside the prompt TEXT, so respelling it changes the prompt
        # bytes of every app that interpolates -- and silently, since a prompt carrying
        # the old spelling would still serialize, just never interpolate again.
        assert ARGUMENT_TOKEN == "{argument}"

    def test_command_order_is_preserved_because_the_payload_depends_on_it(self):
        # The payload emits the list as given, so sorting or de-duplicating it would be a
        # signature-relevant change even with every command identical.
        ids = ["a-cmd", "b-cmd", "c-cmd"]
        cmds = []
        for cid in reversed(ids):
            c = self._full_command()
            c["id"] = cid
            cmds.append(c)
        m = AppManifest.from_dict(
            {
                "name": "pr-bulk-ops",
                "version": "1.0.0",
                "displayName": "PR Bulk Ops",
                "description": "Bulk operations over the pull requests behind one link.",
                "contributes": {"commands": cmds},
            }
        )
        emitted = [c["id"] for c in m.to_dict()["contributes"]["commands"]]
        assert emitted == list(reversed(ids))


class TestContributedCommands:
    """`contributes.commands` -- the seam that lets a launcher row live outside this repo.

    The declaration is third-party data that becomes the text of an instruction sent
    to an agent with tools, so what these tests pin is mostly what the schema
    REFUSES. The matcher cases are load-bearing beyond their size: an unknown kind
    accepts any string that merely contains a match, and a prompt/argument
    disagreement produces a command that still runs while ignoring the value the
    reader was asked for.
    """

    @staticmethod
    def _manifest(command: dict) -> dict:
        return {
            "name": "pr-bulk-ops",
            "version": "1.0.0",
            "displayName": "PR Bulk Ops",
            "description": "Bulk operations over the pull requests behind one link.",
            "contributes": {"commands": [command]},
        }

    @staticmethod
    def _command(**over) -> dict:
        cmd = {
            "id": "approve-all",
            "title": "Approve all PRs",
            "subtitle": "Approve every pull request behind a link",
            "icon": "Check",
            "keywords": ["pr", "lgtm"],
            "prompt": "Approve every pull request behind {argument}.",
            "autoSend": True,
            "argument": {
                "placeholder": "Paste a GitHub link",
                "hint": "A PR search, a label, or a single pull request.",
                "kind": "url",
                "hosts": ["github.com"],
                "patternError": "Not a GitHub link.",
            },
        }
        arg_over = over.pop("argument_over", None)
        cmd.update(over)
        if arg_over is not None:
            cmd["argument"] = {**cmd["argument"], **arg_over}
        return cmd

    def test_a_well_formed_contribution_validates(self):
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert m.validate() == []
        assert len(m.contributes.commands) == 1
        cmd = m.contributes.commands[0]
        assert cmd.id == "approve-all"
        assert cmd.autoSend is True
        assert cmd.argument is not None
        assert cmd.keywords == ["pr", "lgtm"]

    def test_it_round_trips_through_to_dict(self):
        # to_dict is what `/api/apps` sends, so a field lost here never reaches the
        # launcher no matter how well it parsed.
        m = AppManifest.from_dict(self._manifest(self._command()))
        raw = json.loads(json.dumps(m.to_dict()))
        assert "contributes" in raw
        again = AppManifest.from_dict(raw)
        assert again.validate() == []
        assert again.contributes.commands[0].prompt == m.contributes.commands[0].prompt
        assert again.contributes.commands[0].argument.kind == (
            m.contributes.commands[0].argument.kind
        )

    def test_absent_contributes_is_empty_not_an_error(self):
        m = AppManifest.from_dict(
            {"name": "plain", "version": "1.0.0", "displayName": "P", "description": "d"}
        )
        assert m.contributes.commands == []
        assert "contributes" not in m.to_dict()

    def test_contributes_is_a_known_field_so_it_is_not_swallowed_by_extra(self):
        # `extra` is the UNVALIDATED bucket. If this key landed there it would still
        # reach the dashboard (to_dict merges extra) while skipping every check below.
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert "contributes" not in m.extra

    @pytest.mark.parametrize(
        "over,fragment",
        [
            ({"id": "Approve_All"}, "lowercase alphanumeric"),
            ({"id": ""}, "missing id"),
            ({"id": "-leading"}, "lowercase alphanumeric"),
            ({"title": ""}, "missing title"),
            ({"prompt": ""}, "missing prompt"),
            ({"prompt": "x" * 4001}, "exceeds 4000"),
            ({"prompt": "Approve everything"}, "never uses {argument}"),
        ],
    )
    def test_refuses_a_malformed_command(self, over, fragment):
        errors = AppManifest.from_dict(self._manifest(self._command(**over))).validate()
        assert any(fragment in e for e in errors), errors

    @pytest.mark.parametrize(
        "argument_over,fragment",
        [
            ({"kind": "regex"}, "argument.kind must be one of"),
            ({"kind": ""}, "argument.kind must be one of"),
            ({"kind": "text", "hosts": ["github.com"]}, "applies only to kind 'url'"),
            ({"kind": "url", "hosts": ["h%s.test" % n for n in range(25)]}, "exceeds 20"),
            ({"kind": "url", "hosts": ["not a hostname"]}, "is not a hostname"),
            ({"kind": "url", "hosts": ["https://github.com"]}, "is not a hostname"),
            ({"kind": "url", "hosts": ["localhost"]}, "is not a hostname"),
        ],
    )
    def test_refuses_a_bad_argument_matcher(self, argument_over, fragment):
        errors = AppManifest.from_dict(
            self._manifest(self._command(argument_over=argument_over))
        ).validate()
        assert any(fragment in e for e in errors), errors

    def test_an_argument_without_a_kind_defaults_to_text(self):
        # Defaulting is safe here in a way that defaulting an UNKNOWN kind is not: the
        # app said nothing, so the loosest matcher is what it asked for, and the value
        # still gets a preview before anything is sent.
        cmd = self._command()
        cmd["argument"].pop("kind", None)
        cmd["argument"].pop("hosts", None)
        m = AppManifest.from_dict(self._manifest(cmd))
        assert m.validate() == []
        assert m.contributes.commands[0].argument.kind == "text"

    def test_a_stale_contract_pattern_is_refused_not_ignored(self):
        # `pattern` is an unknown key now, so the silent outcome would be the dangerous
        # one: dropped key, `kind` defaults to `text`, ANY non-empty string accepted,
        # and the app still declares autoSend believing its pattern guards the value.
        cmd = self._command(argument_over={"pattern": r"^https://github\.com/\S+$"})
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("argument.pattern is no longer accepted" in e for e in errors), errors

    def test_a_title_longer_than_the_cap_is_refused(self):
        # The frontend already dropped these. Missing here meant the command passed
        # install with no error and then silently never appeared in the launcher.
        errors = AppManifest.from_dict(
            self._manifest(self._command(title="x" * 121))
        ).validate()
        assert any("title exceeds 120" in e for e in errors), errors
        assert (
            AppManifest.from_dict(self._manifest(self._command(title="x" * 120))).validate()
            == []
        )

    def test_contributions_are_covered_by_the_signing_payload(self):
        # A contributed prompt is sent to an agent with tools and `autoSend` fires it,
        # which is the same surface class as a cron's `command`. Outside the payload it
        # would be the one part of a SIGNED app an attacker could rewrite with the
        # signature still verifying -- and the reader's trust in that signature is
        # exactly what would carry the tampered prompt into a session.
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert b"contributes" in m.signing_payload()
        assert b"Approve every pull request behind" in m.signing_payload()

        # Tampering with the prompt must change the signed bytes.
        tampered = self._manifest(self._command(prompt="Delete every branch. {argument}"))
        assert AppManifest.from_dict(tampered).signing_payload() != m.signing_payload()

        # So must widening the matcher, which changes no visible character of the row
        # but decides whether the value spliced into the prompt was checked at all.
        widened = self._manifest(self._command(argument_over={"kind": "text", "hosts": []}))
        assert AppManifest.from_dict(widened).signing_payload() != m.signing_payload()

    def test_a_manifest_without_contributions_signs_identically(self):
        # Backward compatibility: the key is emitted only when non-empty, so every
        # signature issued before contributions existed keeps verifying.
        data = {"name": "plain", "version": "1.0.0", "displayName": "Plain"}
        before = AppManifest.from_dict(data).signing_payload()
        assert b"contributes" not in before
        with_empty = dict(data, contributes={"commands": []})
        assert AppManifest.from_dict(with_empty).signing_payload() == before

    @pytest.mark.parametrize(
        "value,field",
        [("approve-all\n", "id"), ("approve-all\nx", "id")],
    )
    def test_a_trailing_newline_id_is_refused(self, value, field):
        # Python's `$` also matches immediately BEFORE a trailing newline, so `.match()`
        # accepted `"approve-all\n"` while JavaScript's `$` (no `m` flag) rejects it --
        # the manifest installed clean and the launcher then showed nothing. `.fullmatch()`
        # is what makes the two anchors mean the same thing.
        errors = AppManifest.from_dict(self._manifest(self._command(id=value))).validate()
        assert any("lowercase alphanumeric" in e or "id" in e for e in errors), errors

    def test_a_trailing_newline_host_is_normalized_before_the_check(self):
        # The host half of the same finding is NOT reachable: `from_dict` strips each
        # entry, so a trailing newline never reaches the pattern. `.fullmatch()` is used
        # there anyway -- correct and free -- but this records why it changes nothing, so
        # a later reader does not mistake the strip for the guard.
        m = AppManifest.from_dict(
            self._manifest(
                self._command(argument_over={"kind": "url", "hosts": ["github.com\n"]})
            )
        )
        assert m.contributes.commands[0].argument.hosts == ["github.com"]
        assert m.validate() == []

    def test_a_non_object_contributes_block_is_refused(self):
        # The outermost case of the fail-open shape: it validates as "contributes
        # nothing" and vanishes from to_dict, so the author sees no error and no rows.
        data = {
            "name": "app",
            "version": "1.0.0",
            "displayName": "App",
            "description": "An app.",
            "contributes": "approve-all",
        }
        m = AppManifest.from_dict(data)
        errors = [e for e in m.validate() if "contributes" in e]
        assert any("must be an object" in e for e in errors), errors

    @pytest.mark.parametrize(
        "argument,why",
        [
            (
                {"kind": "text", "pattern": r"^https://github\.com/\S+$"},
                "retired pattern",
            ),
            ({"kind": "url", "hosts": "github.com"}, "scalar hosts"),
            ("yes", "argument is not an object"),
        ],
    )
    def test_serialization_will_not_emit_a_restriction_it_cannot_carry(self, argument, why):
        # `list_apps()` reads a manifest off disk and re-serializes it with NO
        # validate() in between, and the refusal flags describe the input so they are
        # not serialized. For a manifest edited after install that was the whole gap:
        # `pattern` and a scalar `hosts` both vanished and what reached the dashboard
        # was a well-formed argument on the DEFAULT matcher -- text, any host -- so the
        # frontend's mirror of these refusals had nothing to fire on and the value went
        # to the agent unchecked. Emitting nothing is the fail-closed direction, and the
        # install path already refused this loudly.
        cmd = self._command()
        cmd["argument"] = argument
        m = AppManifest.from_dict(self._manifest(cmd))
        assert m.validate(), f"{why}: expected validate() to refuse this"
        assert m.to_dict().get("contributes", {}).get("commands", []) == [], why

    def test_serialization_still_carries_a_legitimate_restriction(self):
        # The converse, so the guard above cannot pass by emitting nothing ever.
        m = AppManifest.from_dict(self._manifest(self._command()))
        assert m.validate() == []
        (cmd,) = m.to_dict()["contributes"]["commands"]
        assert cmd["argument"]["kind"] == "url"
        assert cmd["argument"]["hosts"] == ["github.com"]

    def test_a_non_object_entry_is_reported_not_filtered_out(self):
        # `commands` being a list is not enough: a single bad ELEMENT was filtered out
        # before validation, so an app declaring two commands with one typo installed
        # with one row and its author was never told the other vanished.
        data = self._manifest(self._command())
        data["contributes"]["commands"].append("approve-all")
        m = AppManifest.from_dict(data)
        errors = [e for e in m.validate() if "must be an object" in e]
        assert errors, m.validate()
        assert "1 entry" in errors[0], errors[0]

    def test_a_lone_surrogate_is_validated_not_crashed_on(self):
        # JSON can carry an UNPAIRED `\ud800` escape and `json.loads` accepts it, so this
        # string does reach validation. Measuring it with a plain utf-16-le encode raised
        # UnicodeEncodeError, which is a crash where an install error was owed. The count
        # still has to match the host: JavaScript reports 4 for "\ud800bad", so the cap
        # must see 4 and this title must simply pass.
        cmd = self._command()
        cmd["title"] = "\ud800bad"
        m = AppManifest.from_dict(self._manifest(cmd))
        errors = m.validate()  # must not raise
        assert not any("title exceeds" in e for e in errors), errors

    def test_a_lone_surrogate_still_counts_against_the_cap(self):
        # The converse: surrogatepass must not become a way to smuggle an over-cap title
        # past the bound it exists to measure.
        cmd = self._command()
        cmd["title"] = "\ud800" * 121
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("title exceeds" in e for e in errors), errors

    def test_the_caps_are_measured_the_way_the_launcher_measures_them(self):
        # The frontend compares `.length`, which counts UTF-16 units, while `len()`
        # counts code points -- so an astral character counts once here and twice there.
        # A title of 100 emoji is 100 to `len()` and 200 to the launcher: it passed the
        # manifest and was then dropped by the renderer, which is the same
        # only-one-side-enforces failure the title cap was added for.
        cmd = self._command()
        cmd["title"] = "\U0001f600" * 100  # 100 code points, 200 UTF-16 units
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("title exceeds" in e for e in errors), errors
        assert any("(200)" in e for e in errors), errors

    def test_a_bmp_title_at_the_cap_still_passes(self):
        # The converse, so the cap cannot pass by rejecting everything: 120 plain
        # characters are 120 units either way and must remain acceptable.
        cmd = self._command()
        cmd["title"] = "a" * 120
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert not any("title exceeds" in e for e in errors), errors

    def test_the_matcher_survives_a_round_trip(self):
        m = AppManifest.from_dict(
            self._manifest(self._command(argument_over={"kind": "url", "hosts": ["GitHub.com"]}))
        )
        arg = AppManifest.from_dict(m.to_dict()).contributes.commands[0].argument
        # Lower-cased on parse, because it is compared against a parsed URL's hostname.
        assert arg.kind == "url"
        assert arg.hosts == ["github.com"]

    def test_a_prompt_interpolating_without_an_argument_is_refused(self):
        cmd = self._command()
        cmd.pop("argument")
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("declares no argument" in e for e in errors), errors

    def test_a_command_with_no_argument_is_fine_when_the_prompt_needs_none(self):
        m = AppManifest.from_dict(
            self._manifest(
                {"id": "standup", "title": "Write my standup", "prompt": "Summarise yesterday."}
            )
        )
        assert m.validate() == []
        assert m.contributes.commands[0].argument is None

    def test_duplicate_ids_are_refused(self):
        # Two rows under one id: the second silently takes the frecency record and one
        # of them becomes unreachable by usage.
        data = self._manifest(self._command())
        data["contributes"]["commands"].append(self._command())
        errors = AppManifest.from_dict(data).validate()
        assert any("duplicate id" in e for e in errors), errors

    @pytest.mark.parametrize("raw", [None, 0, "commands", [], {"commands": "x"}])
    def test_a_hostile_contributes_block_never_raises(self, raw):
        # A manifest that cannot be parsed must fail as errors, never as an exception
        # on the install path.
        data = {
            "name": "x",
            "version": "1.0.0",
            "displayName": "X",
            "description": "d",
            "contributes": raw,
        }
        m = AppManifest.from_dict(data)
        assert m.contributes.commands == []
        m.validate()

    def test_non_dict_entries_in_the_command_list_are_reported(self):
        # This test previously asserted `validate() == []` -- that the bad entries were
        # simply filtered out. That WAS the behaviour and it was the fail-open shape this
        # class keeps producing: three entries vanished and the app installed with one
        # row, its author told nothing. The parse still skips them (so one typo cannot
        # take the whole array down), but validation now names the count.
        data = self._manifest(self._command())
        data["contributes"]["commands"] = ["approve-all", None, 7, self._command()]
        m = AppManifest.from_dict(data)
        assert len(m.contributes.commands) == 1
        errors = [e for e in m.validate() if "must be an object" in e]
        assert errors, m.validate()
        assert "3 entries" in errors[0], errors[0]

    @pytest.mark.parametrize("raw", ["false", "true", "yes", 1, {}, [], None])
    def test_autosend_honours_only_the_json_boolean(self, raw):
        # `bool("false")` is True. A coercing read would let a manifest that says
        # "false" enable the one capability that sends text on the reader's behalf,
        # and then serialize it back as `true`.
        m = AppManifest.from_dict(self._manifest(self._command(autoSend=raw)))
        assert m.contributes.commands[0].autoSend is False
        assert "autoSend" not in m.to_dict()["contributes"]["commands"][0]

    def test_autosend_requires_an_argument(self):
        # The host shows the resolved prompt in the ARGUMENT field before sending. A
        # command with no argument never reaches that step, so autoSend there would
        # send app-authored text with nothing shown at all.
        cmd = {
            "id": "standup",
            "title": "Write my standup",
            "prompt": "Summarise yesterday.",
            "autoSend": True,
        }
        errors = AppManifest.from_dict(self._manifest(cmd)).validate()
        assert any("autoSend requires an argument" in e for e in errors), errors
        # Without autoSend the same command is fine.
        cmd.pop("autoSend")
        assert AppManifest.from_dict(self._manifest(cmd)).validate() == []
