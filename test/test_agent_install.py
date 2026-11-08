"""Tests for ``install_agent``: the spec it generates, merges and refreshes.

Fresh installs from the bundled defaults, entry-key and env sanitising, the
managed-model marker, MCP auto-approve seeding, and the raw-name routing of a
slashed server's secret reference.
"""

from __future__ import annotations

import codecs
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401
from agent_helpers import _run_install

# Shared with ``test_mcp_rebuild_reconsumption`` via a dedicated helpers module --
# the repo's convention, since a test module is not importable from another one.
# Re-exported under the original private names so the call sites below are unchanged.
from mcp_merge_helpers import DEFAULT_MANAGED_MCPS as _DEFAULT_MANAGED_MCPS
from mcp_merge_helpers import bundled_defaults as _bundled_defaults

from kiro_crew import agent_state
from kiro_crew.agent import _MANAGED_MCP_ENTRY_KEYS


def _reject_json_constant(name: str):  # pragma: no cover - raises by design
    """Stand in for a strict JSON reader.

    ``NaN``/``Infinity``/``-Infinity`` are Python extensions, not JSON, so a
    conforming parser (kiro-cli's) refuses them. Python's own loader accepts them
    silently, which is exactly why a test that only re-reads with ``json.loads``
    would pass on a spec kiro-cli cannot read.
    """
    raise AssertionError(f"emitted spec carries the non-JSON constant {name!r}")


def _sidecar_unreadable_once(monkeypatch, attr: str = "get_model_managed") -> list[bool]:
    """Make the FIRST call of ``agent_state.<attr>`` see an unreadable sidecar.

    Returns the recorded ``strict`` flags, so a test can assert the strict read was
    hit (a lenient read answers ``None``, same as "nothing recorded"). One-shot because
    the surrounding mutators read strict and would catch a sidecar that stays broken.
    """
    real = getattr(agent_state, attr)
    seen: list[bool] = []

    def _once(name: str, **kwargs):
        if not seen:
            strict = kwargs.get("strict", False) if attr == "get_model_managed" else True
            seen.append(strict)
            if strict:
                raise OSError("sidecar unreadable")
            return None
        return real(name, **kwargs)

    monkeypatch.setattr(agent_state, attr, _once)
    return seen


class TestInstallAgent:
    def test_fresh_install_generates_from_defaults(self, tmp_path: Path):
        """No existing kirocrew.json → config built from defaults."""
        cfg_dir = _bundled_defaults(tmp_path)
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"
        assert "ReadFile" in config["tools"]

    def test_fresh_install_applies_bom_saved_user_overrides(self, tmp_path: Path):
        """An agent.json saved with a UTF-8 byte-order mark still overrides."""
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_bytes(
            codecs.BOM_UTF8 + json.dumps({"model": "user-pick"}).encode("utf-8")
        )

        path = _run_install(tmp_path, cfg_dir)

        assert json.loads(path.read_text(encoding="utf-8"))["model"] == "user-pick"

    def test_fresh_install_preserves_safe_managed_server_overrides(self, tmp_path: Path):
        """A clean rebuild keeps preferences without ceding invocation ownership."""
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {
                            "command": "/tmp/untrusted",
                            "args": ["other"],
                            "url": "https://example.invalid/mcp",
                            "timeout": 45,
                            "env": {"MINE": "keep", "KIROCREW_HOME": "/tmp/wrong"},
                            "autoApprove": ["unreviewed_tool"],
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        assert entry["command"] == "/usr/bin/kirocrew"
        assert entry["args"] == ["mcp-core"]
        assert "url" not in entry
        assert entry["timeout"] == 45
        assert entry["env"]["MINE"] == "keep"
        assert entry["env"].get("KIROCREW_HOME") != "/tmp/wrong"
        assert "autoApprove" not in entry

    def test_fresh_install_drops_unsupported_entry_keys(self, tmp_path: Path):
        """A managed entry carries only our fields plus the user's own.

        kiro-cli rejects the WHOLE agent for an unknown field, so one stray ``cwd`` on a
        managed server would take every Kiro Crew tool down.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {
                            "cwd": "/tmp/elsewhere",
                            "initializationOptions": {"x": 1},
                            "url": "https://example.invalid/mcp",
                            "headers": {"Authorization": "Bearer x"},
                            "timeout": 45,
                            "disabled": False,
                            "disabledTools": ["delete_file"],
                            "env": {"MINE": "keep"},
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        # The supported customizations survive -- that is the fix.
        assert entry["timeout"] == 45
        assert entry["disabled"] is False
        assert entry["env"]["MINE"] == "keep"
        # A user GUARD, not a preference: dropping it would silently re-expose a
        # tool the user turned off, so it is carried rather than re-derived.
        assert entry["disabledTools"] == ["delete_file"]
        # Everything the user cannot declare on a managed server is gone, and
        # url/headers are dropped as instances of the rule, not as named fields.
        for unsupported in ("cwd", "initializationOptions", "url", "headers"):
            assert unsupported not in entry, unsupported
        # Nothing outside the closed set reaches the spec.
        assert set(entry) <= _MANAGED_MCP_ENTRY_KEYS

    def test_refresh_drops_unsupported_entry_keys(self, tmp_path: Path):
        """The refresh path drops the same keys, since one enforcer owns both."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps(
                {
                    "model": "claude-user-custom",
                    "tools": [],
                    "allowedTools": [],
                    "mcpServers": {
                        "kirocrew-core": {
                            "command": "/old/path/kirocrew",
                            "args": ["mcp-core"],
                            "cwd": "/tmp/elsewhere",
                            "timeout": 30,
                            "disabledTools": ["execute_cmd"],
                            "env": {"MINE": "keep"},
                        },
                    },
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        assert entry["timeout"] == 30
        assert entry["env"]["MINE"] == "keep"
        # The refresh path is the one that would silently widen the tool surface
        # by dropping this, since it is the path an existing config takes.
        assert entry["disabledTools"] == ["execute_cmd"]
        assert "cwd" not in entry
        assert set(entry) <= _MANAGED_MCP_ENTRY_KEYS

    def test_fresh_install_strips_launcher_exec_env_from_a_managed_entry(self, tmp_path: Path):
        """A managed shim's env cannot choose what the shim executes.

        A shebang resolves the interpreter by name, so ``PATH`` or ``BASH_ENV`` replaces
        the program. Case variants matter: Windows env names are case-insensitive.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {
                            "env": {
                                "PATH": "/tmp/evil/bin",
                                "BASH_ENV": "/tmp/evil.sh",
                                "ENV": "/tmp/evil.sh",
                                "SHELLOPTS": "xtrace",
                                "BASHOPTS": "expand_aliases",
                                "NODE_OPTIONS": "--require /tmp/evil.js",
                                "NODE_PATH": "/tmp/evil/node_modules",
                                "Path": "/tmp/evil/bin",
                                "bash_env": "/tmp/evil.sh",
                                "MINE": "keep",
                            }
                        }
                    }
                }
            )
        )

        # Simulated DEFAULT install, so no data-home pin is emitted and the
        # assertion below can be an exact equality on the whole env.
        with patch("kiro_crew.agent._valid_override_home", return_value=None):
            path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        # The user's own variable is still preserved.
        assert entry["env"] == {"MINE": "keep"}

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_fresh_install_drops_a_non_finite_timeout(self, tmp_path: Path, literal: str):
        """A float can be the right type and still not be representable in JSON.

        Python's ``json`` round-trips these literals, which strict parsers and kiro-cli
        reject. All three are listed so a check naming only ``NaN`` fails.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            '{"mcpServers": {"kirocrew-core": {"timeout": ' + literal + "}}}"
        )

        path = _run_install(tmp_path, cfg_dir)
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw)["mcpServers"]["kirocrew-core"]

        assert "timeout" not in entry
        # The emitted file is parseable by a STRICT reader, which is the property
        # that actually matters -- Python's own loader would accept the bad value.
        assert literal not in raw
        json.loads(raw, parse_constant=_reject_json_constant)

    def test_refresh_drops_non_string_tool_list_items(self, tmp_path: Path, monkeypatch):
        """A list of the right type can still hold the wrong items.

        ``disabledTools: [1]`` passes a container check. Filtered per item, since
        dropping the list would re-expose every tool the user named correctly.
        ``mcp.honour_auto_approve`` is on, or the floor drops the key and nothing is filtered.
        """
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        _cfg = KiroCrewConfig()
        _cfg.mcp.honour_auto_approve = True
        monkeypatch.setattr(live, "snapshot", lambda: _cfg)
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps(
                {
                    "model": "claude-user-custom",
                    "tools": [],
                    "allowedTools": [],
                    "mcpServers": {
                        "kirocrew-core": {
                            "command": "/old/path/kirocrew",
                            "args": ["mcp-core"],
                            "disabledTools": ["delete_file", 1, None, {"a": 1}],
                            "autoApprove": ["read_file", 2],
                        },
                    },
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw)["mcpServers"]["kirocrew-core"]

        # The correctly-named guard survives its malformed neighbours.
        assert entry["disabledTools"] == ["delete_file"]
        assert entry["autoApprove"] == ["read_file"]
        assert all(isinstance(i, str) for i in entry["disabledTools"])
        assert all(isinstance(i, str) for i in entry["autoApprove"])

    def test_fresh_install_drops_ill_typed_entry_values(self, tmp_path: Path):
        """A right key with a wrong-typed value is dropped, not shipped.

        The spec is schema-checked, so ``"disabled": "false"`` costs the user every
        Crew tool exactly like an unknown field would. Dropping beats coercing for
        the same reason as env entries: a value we invent is not the one they wrote.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {
                            "disabled": "false",
                            "timeout": "45",
                            "disabledTools": "delete_file",
                            "autoApprove": {"not": "a list"},
                            "env": {"MINE": "keep"},
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        for ill_typed in ("disabled", "timeout", "disabledTools"):
            assert ill_typed not in entry, ill_typed
        # The well-typed sibling in the same entry still survives, so one bad
        # value does not discard the rest of the user's customization.
        assert entry["env"]["MINE"] == "keep"
        assert entry["command"] == "/usr/bin/kirocrew"

    def test_fresh_install_drops_a_boolean_timeout(self, tmp_path: Path):
        """``bool`` is an ``int`` subclass, so a bare isinstance check leaks here."""
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            json.dumps({"mcpServers": {"kirocrew-core": {"timeout": True}}})
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        assert "timeout" not in entry

    def test_fresh_install_strips_reserved_control_env_keys(self, tmp_path: Path):
        """agent.json cannot smuggle control-plane env vars into a managed server.

        ``KIROCREW_APPROVAL_MODE`` on kirocrew-core would give every ``spawn_run``
        subagent auto-approval, and ``PYTHONPATH`` would run a ``sitecustomize`` inside
        the process holding the API secret.

        ``env.sanitize_spec_env`` refuses the ``KIROCREW_`` and ``PYTHON`` namespaces by
        prefix, case-insensitively (Windows env names are). The ``*NOT_YET_INVENTED``
        names stand for variables nobody has added yet.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {
                            "env": {
                                "MINE": "keep",
                                "KIROCREW_APPROVAL_MODE": "auto",
                                "KIROCREW_SESSION_KEY": "forged",
                                "KIROCREW_INTERNAL_SECRET": "forged",
                                "KIROCREW_PRINCIPAL": "forged",
                                "KIROCREW_CLI": "1",
                                "KIROCREW_NOT_YET_INVENTED": "forged",
                                "Kirocrew_Approval_Mode": "auto",
                                "pythonpath": "/x/evil-lowercase",
                                "PYTHONPATH": "/x/evil",
                                "PYTHONHOME": "/x/evil",
                                "PYTHONSTARTUP": "/x/evil.py",
                                "PYTHONEXECUTABLE": "/x/evil-python",
                                "PYTHONWARNINGS": "ignore::evil.Boom",
                                "PYTHONPLATLIBDIR": "evil-lib",
                                "PYTHONBREAKPOINT": "evil.run",
                                "PYTHONUSERBASE": "/x/evil-site",
                                "PYTHONNOTYETINVENTED": "forged",
                                "LD_PRELOAD": "/x/evil.so",
                                "LD_LIBRARY_PATH": "/x/evil",
                                "DYLD_INSERT_LIBRARIES": "/x/evil.dylib",
                                "DYLD_LIBRARY_PATH": "/x/evil",
                            },
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        assert entry["env"]["MINE"] == "keep"
        for reserved in (
            "KIROCREW_APPROVAL_MODE",
            "KIROCREW_SESSION_KEY",
            "KIROCREW_INTERNAL_SECRET",
            "KIROCREW_PRINCIPAL",
            "KIROCREW_CLI",
            "KIROCREW_NOT_YET_INVENTED",
            "Kirocrew_Approval_Mode",
            "pythonpath",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "PYTHONEXECUTABLE",
            "PYTHONWARNINGS",
            "PYTHONPLATLIBDIR",
            "PYTHONBREAKPOINT",
            "PYTHONUSERBASE",
            "PYTHONNOTYETINVENTED",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
        ):
            assert reserved not in entry["env"], reserved

    def test_refresh_strips_reserved_control_env_keys(self, tmp_path: Path):
        """The refresh path (existing kirocrew.json) scrubs the same reserved keys.

        Asserted on this path too because the shared enforcer is what makes the
        two agree -- a key stripped on one emit path and preserved on the other
        is the drift this PR exists to remove.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "kirocrew-core": {
                    "command": "/old/path/kirocrew",
                    "args": ["mcp-core"],
                    "env": {
                        "MINE": "keep",
                        "KIROCREW_APPROVAL_MODE": "auto",
                        "KIROCREW_CLI": "1",
                        "KIROCREW_NOT_YET_INVENTED": "forged",
                        "PYTHONPATH": "/x/evil",
                        "LD_PRELOAD": "/x/evil.so",
                    },
                },
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        assert entry["env"]["MINE"] == "keep"
        for reserved in (
            "KIROCREW_APPROVAL_MODE",
            "KIROCREW_CLI",
            "KIROCREW_NOT_YET_INVENTED",
            "PYTHONPATH",
            "LD_PRELOAD",
        ):
            assert reserved not in entry["env"], reserved

    def test_fresh_install_drops_malformed_non_dict_env(self, tmp_path: Path):
        """A non-object ``env`` override does not crash the rebuild.

        Pre-fix this fed a non-dict straight to ``dict(...)``, which raises
        for values like a plain string or a list of non-pair items and would
        abort the whole config rebuild over a single bad agent.json value.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            json.dumps({"mcpServers": {"kirocrew-core": {"env": "not-a-dict"}}})
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        assert entry["command"] == "/usr/bin/kirocrew"
        # The malformed string is discarded rather than fed to dict(...); any
        # remaining ``env`` key is only Kiro Crew's own KIROCREW_HOME pin (the
        # test harness's tmp_path counts as an override home).
        assert set(entry.get("env", {})) <= {"KIROCREW_HOME"}

    def test_fresh_install_drops_non_string_env_values(self, tmp_path: Path):
        """An ill-typed ``env`` value never reaches the emitted spec.

        The emitted env is a string map, and kiro-cli rejects the whole spec for
        ``{"PORT": 3000}``. Bad entries are dropped, not coerced; good siblings survive.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir()
        (user_home / "agent.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {
                            "env": {
                                "GOOD": "kept",
                                "PORT": 3000,
                                "FLAG": True,
                                "NOTHING": None,
                                "LISTY": ["a", "b"],
                                "NESTED": {"k": "v"},
                            },
                        }
                    }
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-core"]

        assert entry["env"]["GOOD"] == "kept"
        for bad in ("PORT", "FLAG", "NOTHING", "LISTY", "NESTED"):
            assert bad not in entry["env"], bad
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in entry["env"].items())

    def test_fresh_install_pins_our_data_home_against_a_declared_home(self, tmp_path: Path):
        """A declared ``HOME`` cannot relocate a managed shim's data home.

        A managed shim resolves our data home via ``Path.home()``, so a declared ``HOME``
        would move the store (``cron_add`` would write where the gateway never reads).
        Stripped, not pinned: ``TestDataHomePin`` requires a default install to emit no
        ``env``. Case variants matter because Windows env names are case-insensitive.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        user_home = tmp_path / "kirocrew_home"
        user_home.mkdir(exist_ok=True)
        (user_home / "agent.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-cron": {
                            "env": {
                                "HOME": "/x/evil-home",
                                "USERPROFILE": "C:\\x\\evil-home",
                                "userprofile": "C:\\x\\evil-lower",
                                "UserProfile": "C:\\x\\evil-mixed",
                                "Home": "/x/evil-mixed",
                                "MINE": "keep",
                            },
                        }
                    }
                }
            )
        )

        # Simulated DEFAULT install: no override home, so nothing pins the child
        # and the declared HOME would otherwise decide where our data lives.
        with patch("kiro_crew.agent._valid_override_home", return_value=None):
            path = _run_install(tmp_path, cfg_dir)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kirocrew-cron"]

        for spelling in ("HOME", "USERPROFILE", "userprofile", "UserProfile", "Home"):
            assert spelling not in entry["env"], spelling
        # The user's genuine variable still survives, and no pin was invented --
        # the default-install spec is not churned.
        assert entry["env"] == {"MINE": "keep"}

    def test_existing_config_preserves_user_model(self, tmp_path: Path):
        """Existing kirocrew.json → user's model choice survives restart."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["ReadFile", "WriteFile"],
            "allowedTools": ["ReadFile", "WriteFile"],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": ["old"]}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-user-custom"
        assert "WriteFile" in config["tools"]

    def test_fresh_install_marks_model_managed(self, tmp_path: Path):
        """Fresh install tracks the shipped default via the sidecar (not the spec)."""
        cfg_dir = _bundled_defaults(tmp_path)
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"
        # kiro spec must stay schema-clean; managed-state lives in the sidecar.
        assert "model_managed" not in config
        assert agent_state.get_model_managed("kirocrew") is True

    @pytest.mark.parametrize(
        ("model", "managed", "want_model"),
        [
            # A managed (sidecar) config re-syncs model from defaults.json on a bump.
            pytest.param(
                "claude-old-default",
                True,
                "claude-default",
                id="test_managed_config_tracks_defaults_bump",
            ),
            # A frozen pick (model_managed=False) is never re-synced to default.
            pytest.param(
                "claude-user-pinned",
                False,
                "claude-user-pinned",
                id="test_explicitly_frozen_config_not_tracked",
            ),
        ],
    )
    def test_a_spec_model_managed_flag_decides_the_defaults_resync(
        self, tmp_path: Path, model: str, managed: bool, want_model: str
    ):
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Pre-migration polluted spec: model_managed lives in the spec. The
        # install migration lifts it into the sidecar and strips it.
        existing = {
            "model": model,
            "model_managed": managed,
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == want_model
        assert "model_managed" not in config
        assert agent_state.get_model_managed("kirocrew") is managed

    def test_legacy_config_without_marker_frozen(self, tmp_path: Path):
        """A legacy config (no marker) stays frozen: model untouched."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-legacy-4.6",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-legacy-4.6"
        assert "model_managed" not in config
        assert agent_state.get_model_managed("kirocrew") is None

    @pytest.mark.parametrize(
        ("recorded", "expected_after"),
        [
            pytest.param(False, False, id="owner-pick-preserved"),
            pytest.param(None, True, id="empty-sidecar-seeds-on-the-next-pass"),
        ],
    )
    def test_fresh_install_with_an_unreadable_sidecar_seeds_nothing_and_retries(
        self, tmp_path: Path, monkeypatch, recorded, expected_after
    ):
        """A fresh install whose sidecar read fails once writes nothing and tries again.

        The strict claim raises before the spec is written, so a ``None`` hiding a
        recorded pick decides nothing. Next pass: ``False`` stays, unset becomes ``True``.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        if recorded is not None:
            agent_state.set_model_managed("kirocrew", recorded)
        seen = _sidecar_unreadable_once(monkeypatch, "claim_model_managed_if_unset")

        with pytest.raises(OSError):
            _run_install(tmp_path, cfg_dir)

        assert seen == [True], "the seed must reach the sidecar through the strict claim"
        assert agent_state.get_model_managed("kirocrew") is recorded
        assert not (tmp_path / "kiro_agents" / "kirocrew.json").exists()

        path = _run_install(tmp_path, cfg_dir)
        assert json.loads(path.read_text(encoding="utf-8"))["model"] == "claude-default"
        assert agent_state.get_model_managed("kirocrew") is expected_after

    def test_managed_resync_holds_on_an_unreadable_sidecar_then_applies_next_pass(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A managed spec keeps its model for the pass whose sidecar read fails.

        The model is held by decision, with a warning, and the spec is not rebuilt from
        defaults. The next readable pass applies the shipped model.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-old-default",
            "tools": ["ReadFile"],
            "allowedTools": [],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        agent_state.set_model_managed("kirocrew", True)
        seen = _sidecar_unreadable_once(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            path = _run_install(tmp_path, cfg_dir)
        held = json.loads(path.read_text(encoding="utf-8"))

        assert seen == [True], "the re-sync read must reach the sidecar strict"
        assert held["model"] == "claude-old-default"
        assert "ReadFile" in held["tools"], "held, not rebuilt from defaults"
        assert agent_state.get_model_managed("kirocrew") is True
        assert any(
            "leaving the kirocrew spec's model as it is" in record.getMessage()
            for record in caplog.records
        )

        path = _run_install(tmp_path, cfg_dir)
        assert json.loads(path.read_text(encoding="utf-8"))["model"] == "claude-default"

    def test_existing_config_refreshes_security_fields(self, tmp_path: Path):
        """hooks are always overwritten from bundled.

        Command denial lives in the hooks.py PreToolUse gate, so a stale
        ``deniedCommands`` is stripped, or kiro-cli would block a Settings opt-out.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": ["stale"]}},
            "hooks": {"old": "hook"},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # A stale deniedCommands is stripped, with its now-empty toolsSettings.
        assert "toolsSettings" not in config
        assert config["hooks"] == {"preToolUse": "audit"}

    def test_existing_config_refreshes_dynamic_mcp_servers(self, tmp_path: Path):
        """kirocrew-cron and kirocrew-core commands are always refreshed."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "kirocrew-cron": {"command": "/old/path/kirocrew", "args": ["mcp-cron"]},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["mcpServers"]["kirocrew-cron"]["command"] == "/usr/bin/kirocrew"
        assert config["mcpServers"]["kirocrew-core"]["command"] == "/usr/bin/kirocrew"

    def test_existing_config_keeps_a_hand_added_mcp_auto_approve(self, tmp_path: Path):
        """A hand-added ``autoApprove`` survives a restart.

        The managed registry seeds none, so these are the owner's own. kiro-cli approves
        them locally and ``hooks.on_tool_call`` never runs, which is why
        ``mcp.honour_auto_approve: false`` exists to drop them (the test below).
        """
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "kirocrew-cron": {
                    "command": "/old/path/kirocrew",
                    "args": ["mcp-cron"],
                    "autoApprove": ["cron_list", "cron_add"],
                },
                "kirocrew-core": {
                    "command": "/old/path/kirocrew",
                    "args": ["mcp-core"],
                    "autoApprove": ["learn_list"],
                },
                "builder-mcp": {
                    "command": "builder-mcp",
                    "autoApprove": ["ReadInternalWebsites"],
                },
            },
            "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            "hooks": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # kirocrew-cron/core: command still refreshed, and the owner's grant kept
        assert config["mcpServers"]["kirocrew-cron"]["command"] == "/usr/bin/kirocrew"
        assert config["mcpServers"]["kirocrew-cron"]["autoApprove"] == ["cron_list", "cron_add"]
        assert config["mcpServers"]["kirocrew-core"]["autoApprove"] == ["learn_list"]
        # a user's own server is the reported case, and it is kept too
        assert config["mcpServers"]["builder-mcp"]["autoApprove"] == ["ReadInternalWebsites"]
        # hooks are always refreshed from bundled defaults; the retired
        # deniedCommands injection is stripped on refresh, so the emptied
        # toolsSettings scaffolding is removed entirely.
        assert "toolsSettings" not in config
        assert config["hooks"] == {"preToolUse": "audit"}

    def test_opting_out_drops_a_hand_added_mcp_auto_approve(self, tmp_path: Path, monkeypatch):
        """``mcp.honour_auto_approve: false`` restores the strict floor.

        The operator who wants every MCP call to reach the gate still has one
        switch that does it, and the server itself stays reachable: only the
        exemption goes, so the tool shows an approval card instead.
        """
        from kiro_crew.config import live as _live
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        _cfg = _Cfg()
        _cfg.mcp.honour_auto_approve = False
        monkeypatch.setattr(_live, "snapshot", lambda: _cfg)

        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps(
                {
                    "model": "claude-user-custom",
                    "tools": [],
                    "allowedTools": [],
                    "mcpServers": {
                        "builder-mcp": {
                            "command": "builder-mcp",
                            "autoApprove": ["ReadInternalWebsites"],
                        }
                    },
                    "hooks": {},
                }
            )
        )

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert "builder-mcp" in config["mcpServers"], "the server stays reachable"
        assert "autoApprove" not in config["mcpServers"]["builder-mcp"]

    def test_kirocrew_mcp_json_overrides_kiro_mcp(self, tmp_path: Path, monkeypatch):
        """~/.kirocrew/mcp.json overrides ~/.kiro/settings/mcp.json for kirocrew agent.

        The subject is which file wins, so the opt-in is pinned on: the fixture's
        ``autoApprove`` is hand-added and the floor would otherwise drop it from
        both candidates, leaving nothing to compare.
        """
        from kiro_crew.config import live as _live
        from kiro_crew.config.loader import KiroCrewConfig as _Cfg

        _cfg = _Cfg()
        _cfg.mcp.honour_auto_approve = True
        monkeypatch.setattr(_live, "snapshot", lambda: _cfg)
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Pre-existing agent config with builder-mcp from kiro settings
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "builder-mcp": {
                    "command": "builder-mcp",
                    "args": ["--include-tools", "ReadInternalWebsites"],
                    "autoApprove": ["ReadInternalWebsites"],
                },
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        # kirocrew mcp.json overrides args (removes --include-tools)
        mc_home = tmp_path / "kirocrew_home"
        mc_home.mkdir(exist_ok=True)
        (mc_home / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "builder-mcp": {"command": "builder-mcp", "args": []},
                        "new-server": {"command": "new-cmd", "args": ["start"]},
                    }
                }
            )
        )
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # builder-mcp args overridden by kirocrew mcp.json (no tool-tag injection
        # on a public install — that downstream-edition wiring was removed)
        assert config["mcpServers"]["builder-mcp"]["args"] == []
        # autoApprove preserved (not in kirocrew mcp.json)
        assert config["mcpServers"]["builder-mcp"]["autoApprove"] == ["ReadInternalWebsites"]
        # new server added from kirocrew mcp.json
        assert config["mcpServers"]["new-server"]["command"] == "new-cmd"

    def test_new_managed_server_seeds_auto_approve(self, tmp_path: Path):
        """A new managed MCP server with autoApprove gets it seeded on first install."""
        cfg_dir = _bundled_defaults(tmp_path)
        mcps = {
            **_DEFAULT_MANAGED_MCPS,
            "playwright-mcp": {
                "command": "/usr/bin/playwright-mcp",
                "args": ["mcp", "start"],
                "autoApprove": ["browser_navigate"],
            },
        }
        path = _run_install(tmp_path, cfg_dir, managed_mcps=mcps)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["mcpServers"]["playwright-mcp"]["autoApprove"] == ["browser_navigate"]

    def test_new_managed_server_seeds_auto_approve_on_refresh(self, tmp_path: Path):
        """When a managed server is new to an existing config, autoApprove is seeded."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Existing config has kirocrew-cron/core but NOT playwright-mcp
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "kirocrew-cron": {"command": "/old/kirocrew", "args": ["mcp-cron"]},
                "kirocrew-core": {"command": "/old/kirocrew", "args": ["mcp-core"]},
            },
            "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            "hooks": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        mcps = {
            **_DEFAULT_MANAGED_MCPS,
            "playwright-mcp": {
                "command": "/usr/bin/playwright-mcp",
                "args": ["mcp", "start"],
                "autoApprove": ["browser_navigate"],
            },
        }
        path = _run_install(tmp_path, cfg_dir, managed_mcps=mcps)
        config = json.loads(path.read_text(encoding="utf-8"))
        # playwright-mcp is genuinely new → autoApprove should be seeded
        assert config["mcpServers"]["playwright-mcp"]["autoApprove"] == ["browser_navigate"]

    def test_user_removed_auto_approve_not_re_added(self, tmp_path: Path):
        """If user deliberately removed autoApprove, refresh must not re-add it."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Existing config has playwright-mcp but user removed autoApprove
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "playwright-mcp": {
                    "command": "/old/playwright-mcp",
                    "args": ["mcp", "start"],
                },
            },
            "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            "hooks": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        mcps = {
            **_DEFAULT_MANAGED_MCPS,
            "playwright-mcp": {
                "command": "/usr/bin/playwright-mcp",
                "args": ["mcp", "start"],
                "autoApprove": ["browser_navigate"],
            },
        }
        path = _run_install(tmp_path, cfg_dir, managed_mcps=mcps)
        config = json.loads(path.read_text(encoding="utf-8"))
        # command/args refreshed, but autoApprove NOT re-added
        assert config["mcpServers"]["playwright-mcp"]["command"] == "/usr/bin/playwright-mcp"
        assert "autoApprove" not in config["mcpServers"]["playwright-mcp"]

    def test_clean_flag_ignores_existing(self, tmp_path: Path):
        """clean=True → regenerates from defaults even if file exists."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["UserTool"],
            "allowedTools": [],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir, clean=True)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"
        assert "UserTool" not in config["tools"]

    @pytest.mark.parametrize(
        "existing",
        [
            pytest.param("not valid json{{{", id="test_corrupt_existing_falls_back_to_defaults"),
            pytest.param("[]", id="test_non_dict_json_falls_back_to_defaults"),
        ],
    )
    def test_an_unusable_existing_spec_falls_back_to_defaults(self, tmp_path: Path, existing):
        """Corrupt kirocrew.json, or valid JSON that is not a dict, falls back to
        build_agent_config()."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text(existing)

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"

    def test_missing_bundled_defaults_raises_when_existing_config_present(self, tmp_path: Path):
        """Error propagates when bundled defaults are absent during refresh."""
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        # No defaults.json written — bundled config is absent
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir()
        (kiro_dir / "kirocrew.json").write_text(json.dumps({"model": "x", "mcpServers": {}}))

        with pytest.raises(RuntimeError, match="Cannot build agent config"):
            _run_install(tmp_path, cfg_dir)


class TestSecretReferenceRoutedByRawName:
    """A slashed server routed under its raw name stays one live entry across rebuilds."""

    def test_repeated_rebuilds_keep_one_live_entry(self, tmp_path: Path, monkeypatch) -> None:
        from kiro_crew.agent_materialization import mcp_sources

        cfg_dir = _bundled_defaults(tmp_path)
        (tmp_path / "fake_kiro_mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "npm:@acme/mcp": {
                            "command": "acme-mcp",
                            "env": {"API_TOKEN": "secret://ACME"},
                        }
                    }
                }
            )
        )
        monkeypatch.setattr(
            mcp_sources, "_gateway_routed_names", lambda: frozenset({"npm:@acme/mcp"})
        )
        for _ in range(3):
            path = _run_install(tmp_path, cfg_dir)
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        acme = {k: v for k, v in servers.items() if k.startswith("acme-mcp")}
        assert list(acme) == ["acme-mcp"]
        assert "disabled" not in acme["acme-mcp"]
        assert mcp_sources.WITHHELD_KEY not in acme["acme-mcp"]
