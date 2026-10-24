"""What a spawned app backend is admitted with and can see.

Boot-time admission re-vet, the dev-fleet repo override, the gateway-origin
injection, the sandbox carve-outs for the policy cache and the md-notebook
state files, and the exec backend's shebang shim.
Fixtures come from ``app_backend_helpers.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from app_backend_helpers import app_env_fixture  # noqa: F401
from app_backend_helpers import _make_app_with_backend

from conftest import requires_symlinks
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app


class TestBootAdmissionRevet:
    """start_enabled_app_backends re-vets admission at boot (Kiro Crew parity).

    An app enabled before a policy tightened (banned / now-unsigned) must NOT
    keep running across restarts, but builtins (origin == "builtin") are exempt
    so trusted first-party apps still boot under require_signature.
    """

    def _boot_env(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
        started: list[str] = []

        def _fake_start(name):
            started.append(name)
            return None  # no real spawn; skip the health-gate branch

        monkeypatch.setattr(bmod, "start_app_backend", _fake_start)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda name: None)
        return bmod, started

    def test_banned_third_party_skipped_at_boot(self, tmp_path, app_env, monkeypatch):
        bmod, started = self._boot_env(monkeypatch)
        (app_env / "app_admission.json").write_text(
            json.dumps({"mode": "enforce", "banned": ["evil-app"]})
        )
        apps = [
            {
                "name": "evil-app",
                "enabled": True,
                "origin": "registry",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            }
        ]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        result = bmod.start_enabled_app_backends()
        assert "evil-app" not in result
        assert "evil-app" not in started

    def test_builtin_still_boots_under_require_signature(self, tmp_path, app_env, monkeypatch):
        bmod, started = self._boot_env(monkeypatch)
        (app_env / "app_admission.json").write_text(
            json.dumps(
                {
                    "mode": "enforce",
                    "require_signature": True,
                    "approved": [],
                    "trust_keys": {},
                }
            )
        )
        apps = [
            {
                "name": "core-builtin",
                "enabled": True,
                "origin": "builtin",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            }
        ]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        monkeypatch.setattr(
            bmod,
            "_read_installed",
            lambda _name: SimpleNamespace(origin="builtin"),
        )
        bmod.start_enabled_app_backends()
        # Builtin is exempt from the gate — start_app_backend was invoked for it.
        assert "core-builtin" in started

    def test_deferred_backends_are_admitted_now_and_spawned_later(
        self, tmp_path, app_env, monkeypatch
    ):
        """``defer`` holds a name back from the main spawn wave without skipping its
        vetting; ``start_deferred_app_backends`` then spawns exactly that set, once.
        Dev Fleet is deferred so it is handed the gateway's actually-bound port."""
        bmod, started = self._boot_env(monkeypatch)
        apps = [
            {
                "name": "dev-fleet",
                "enabled": True,
                "origin": "builtin",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            },
            {
                "name": "md-notebook",
                "enabled": True,
                "origin": "builtin",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            },
        ]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        bmod.start_enabled_app_backends()
        assert started == ["md-notebook"]
        bmod.start_deferred_app_backends()
        assert started == ["md-notebook", "dev-fleet"]
        # A second call spawns nothing: the deferred set was consumed.
        bmod.start_deferred_app_backends()
        assert started == ["md-notebook", "dev-fleet"]

    def test_a_deferred_app_that_is_disabled_is_not_spawned_later(
        self, tmp_path, app_env, monkeypatch
    ):
        bmod, started = self._boot_env(monkeypatch)
        apps = [
            {
                "name": "dev-fleet",
                "enabled": False,
                "origin": "builtin",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            }
        ]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        bmod.start_enabled_app_backends()
        bmod.start_deferred_app_backends()
        assert started == []

    def test_a_deferred_app_disabled_between_the_waves_is_not_spawned(
        self, tmp_path, app_env, monkeypatch
    ):
        """The deferral leaves a window (the rest of start_dashboard) in which the
        operator can run `kirocrew app disable dev-fleet`. The cached admission
        must not outlive that: enablement is re-read at spawn time, and an
        unreadable state (None) is treated as not enabled."""
        bmod, started = self._boot_env(monkeypatch)
        apps = [
            {
                "name": "dev-fleet",
                "enabled": True,
                "origin": "builtin",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            }
        ]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        bmod.start_enabled_app_backends()
        assert started == []
        # Operator disables the app while the gateway is still booting.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)
        bmod.start_deferred_app_backends()
        assert started == []
        # And an unreadable state is not a licence to spawn either.
        bmod.start_enabled_app_backends()
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: None)
        bmod.start_deferred_app_backends()
        assert started == []

    def test_a_deferred_app_denied_by_governance_between_the_waves_is_not_spawned(
        self, tmp_path, app_env, monkeypatch
    ):
        import kiro_crew.apps.manager as manager

        bmod, started = self._boot_env(monkeypatch)
        apps = [
            {
                "name": "dev-fleet",
                "enabled": True,
                "origin": "builtin",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            }
        ]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        bmod.start_enabled_app_backends()
        monkeypatch.setattr(manager, "_app_activation_denied", lambda name: "policy tightened")
        bmod.start_deferred_app_backends()
        assert started == []

    def test_dev_fleet_is_the_bound_port_deferred_backend(self):
        import kiro_crew.apps.backend as bmod

        assert bmod.DEV_FLEET_APP_NAME == "dev-fleet"

    def test_spawn_exception_isolated_and_boot_continues(self, tmp_path, app_env, monkeypatch):
        """A per-app spawn failure (e.g. sandbox.wrap_argv fail-closing on macOS 26
        where sandbox-exec is gone) must NOT crash the whole gateway — the loop logs,
        skips the failing app, and still boots the healthy one."""
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        monkeypatch.setattr(bmod, "_reap_stale_app_backends", lambda: 0)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda name: None)
        started: list[str] = []

        def _fake_start(name):
            if name == "boom-app":
                raise RuntimeError(
                    "Sandbox backend unavailable and allow_unsandboxed_exec is not set."
                )
            started.append(name)
            return None

        monkeypatch.setattr(bmod, "start_app_backend", _fake_start)
        apps = [
            {
                "name": "boom-app",
                "enabled": True,
                "origin": "builtin",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            },
            {
                "name": "ok-app",
                "enabled": True,
                "origin": "builtin",
                "manifest": {"backend": {"entryPoint": "server.py"}},
            },
        ]
        monkeypatch.setattr(bmod, "list_apps", lambda: apps)
        # Must not raise despite boom-app's spawn raising.
        result = bmod.start_enabled_app_backends()
        # boom-app was skipped; ok-app still got its spawn attempt.
        assert "boom-app" not in started
        assert "ok-app" in started
        assert "boom-app" not in result


# =============================================================================
# KIROCREW_DEVFLEET_REPO forwarding (silent-empty-fleet fix)
# =============================================================================


def test_devfleet_repo_survives_the_app_backend_env_allowlist(monkeypatch):
    """The documented dev-fleet repo override must be ABLE to reach the backend.

    The dev-fleet backend runs as a separate process started with
    ``apps.registry.minimal_env()``, which passes only a fixed safe-key set.
    ``KIROCREW_DEVFLEET_REPO`` is dev-fleet's highest-priority repo discovery
    hint, but until it is added to the explicit platform extras the allowlist
    strips it — the operator sets the documented override, the backend never
    sees it, and the fleet silently renders empty (the remaining hints are
    ``KIROCREW_PROJECT_DIR``, which packaged installs point at the app bundle
    with no ``.git``, and a hardcoded ``~/kirocrew`` fallback).
    """
    from pathlib import Path

    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps.registry import minimal_env

    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/opt/checkouts/kirocrew")
    # The generic allowlist does NOT carry it — that is the trap this guards.
    assert "KIROCREW_DEVFLEET_REPO" not in minimal_env()

    # apps/backend.py must therefore add it to the explicit platform extras
    # (same mechanism that carries KIROCREW_PROJECT_DIR and the
    # KIROCREW_DEVFLEET_BIN_* trusted-binary overrides).
    body = Path(bmod.__file__).read_text(encoding="utf-8")
    assert (
        '_platform_extra["KIROCREW_DEVFLEET_REPO"]' in body
    ), "the KIROCREW_DEVFLEET_REPO override no longer reaches app backends"


def test_devfleet_repo_env_wins_repo_discovery(monkeypatch, tmp_path):
    """dev-fleet honors the forwarded override ahead of every other hint."""
    from kiro_crew.apps.builtins.dev_fleet import server as dfmod

    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    (proj / "src" / "kiro_crew").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname = 'kiro-crew'\n")
    monkeypatch.setenv("KIROCREW_DEVFLEET_REPO", "/opt/checkouts/kirocrew")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    assert dfmod._default_main_repo() == "/opt/checkouts/kirocrew"

    # Without the override the chain falls through to PROJECT_DIR, which is
    # adopted only because it carries the Kiro Crew checkout markers.
    monkeypatch.delenv("KIROCREW_DEVFLEET_REPO")
    assert dfmod._default_main_repo() == str(proj)


class TestGatewayOriginInjection:
    """Bound-port-gated gateway-origin/proof injection into entryPoint app backends.

    The gateway hands each entryPoint child two generic variables so the child
    can call BACK to this gateway (for example POST /api/notifications/push on a
    declared channel): ``KIROCREW_GATEWAY_ORIGIN`` (where the gateway listens)
    and ``KIROCREW_GATEWAY_ORIGIN_PROOF`` (an HMAC that lets the child confirm
    the origin came from the gateway that alone holds its secret). The origin is
    injected ONLY from hard evidence of the port the gateway ACTUALLY bound --
    the exported ``KIROCREW_BOUND_PORT``, required to be numeric and in
    1..65535. There is no fallback to an inherited ``KIROCREW_PORT``, the app's
    own ``PORT``, a config value, or a default: without bound-port evidence the
    origin (and therefore the proof) is omitted and a backend that needs a
    callback base fails closed (dormant). The proof additionally requires the
    app's ``.app_secret``; a secret-less backend gets the origin only. No
    app-specific variable is ever injected.
    """

    _SECRET = "test-app-secret-0123456789abcdef"

    def _install_backend_app(self, tmp_path, name="origin-app"):
        src = _make_app_with_backend(tmp_path, name=name)
        install_app(src)
        return name

    def _install_typed_backend_app(self, tmp_path, name, entry_rel, backend_type, body):
        """Install an app whose backend declares an explicit ``type`` + entry file."""
        src = tmp_path / "source" / name
        (src / Path(entry_rel).parent).mkdir(parents=True, exist_ok=True)
        (src / entry_rel).write_text(body)
        (src / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "displayName": name,
                    "description": "typed entry",
                    "author": "tester",
                    "backend": {
                        "entryPoint": entry_rel,
                        "type": backend_type,
                        "healthCheck": "/health",
                    },
                }
            )
        )
        install_app(src)
        return name

    def _write_secret(self, name, *, mode=0o600, secret=None):
        from kiro_crew.apps.manager import app_dir

        path = app_dir(name) / ".app_secret"
        path.write_text(secret if secret is not None else self._SECRET)
        os.chmod(path, mode)
        return path

    def _capture_child_env(self, monkeypatch, app_name):
        """Freeze the spawn at the Popen boundary and return the child env dict.

        Mirrors ``test_a_fixed_port_app_claims_it_before_binding``: passthrough
        wrap_argv (so a host without an OS sandbox still reaches this code) and a
        spy Popen that records the env the gateway would hand the child, then
        raises to stop before a real process is created.
        """
        import kiro_crew.apps.backend as bmod

        captured: dict[str, dict] = {}

        def _spy_popen(*a, **k):
            captured["env"] = dict(k.get("env") or {})
            raise OSError("captured child env; stop before the real spawn")

        monkeypatch.setattr(bmod, "wrap_argv", lambda argv, **kw: (list(argv), None))
        monkeypatch.setattr(bmod.subprocess, "Popen", _spy_popen)
        result = bmod.start_app_backend(app_name)
        return result, captured.get("env")

    def test_valid_bound_port_injects_exact_origin_distinct_from_app_port(
        self, tmp_path, app_env, monkeypatch
    ):
        """A valid KIROCREW_BOUND_PORT yields the exact origin, never the app's own PORT.

        The origin must be the port THIS gateway bound (KIROCREW_BOUND_PORT),
        not the app's own PORT (which lives in the 9100-9200 app range).
        """
        import kiro_crew.apps.backend as bmod

        name = self._install_backend_app(tmp_path)
        self._write_secret(name)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")

        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None, "the spawn never reached the Popen boundary"
        assert env["KIROCREW_GATEWAY_ORIGIN"] == "http://127.0.0.1:8123"
        # The origin is the GATEWAY port, never the app's own bound PORT.
        assert env["PORT"] != "8123"
        assert bmod._MIN_PORT <= int(env["PORT"]) <= bmod._MAX_PORT
        # The proxy secret is still injected when a secret exists.
        assert env["KIROCREW_PROXY_SECRET"] == self._SECRET

    def test_specific_interface_bind_omits_the_origin(self, tmp_path, app_env, monkeypatch):
        """A specific-interface KIROCREW_BOUND_HOST omits the origin (fail closed).

        A backend's callback carries no Origin header, and the gateway's CSRF
        barrier trusts an Origin-less mutating request only from a loopback
        peer — so an origin pointing at a specific interface (10.0.0.7,
        fd00::7) would have every mutating callback refused at the barrier.
        Injecting it produces a half-alive backend; omitting it is the same
        designed dormancy as missing port evidence. The v6-loopback marker
        ("::1") still injects, bracketed per RFC 3986, and absent evidence
        stays loopback (the wildcard/loopback bind shapes).
        """
        name = self._install_backend_app(tmp_path)
        self._write_secret(name)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")

        monkeypatch.setenv("KIROCREW_BOUND_HOST", "10.0.0.7")
        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None, "the spawn never reached the Popen boundary"
        assert "KIROCREW_GATEWAY_ORIGIN" not in env
        # No origin means nothing to prove, even with a secret present.
        assert "KIROCREW_GATEWAY_ORIGIN_PROOF" not in env

        monkeypatch.setenv("KIROCREW_BOUND_HOST", "fd00::7")
        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None
        assert "KIROCREW_GATEWAY_ORIGIN" not in env

        monkeypatch.setenv("KIROCREW_BOUND_HOST", "::1")
        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None
        assert env["KIROCREW_GATEWAY_ORIGIN"] == "http://[::1]:8123"

        monkeypatch.delenv("KIROCREW_BOUND_HOST", raising=False)
        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None
        assert env["KIROCREW_GATEWAY_ORIGIN"] == "http://127.0.0.1:8123"

    def test_no_bound_port_env_omits_the_origin(self, tmp_path, app_env, monkeypatch):
        """With no KIROCREW_BOUND_PORT at all, origin AND proof are omitted (fail closed)."""
        name = self._install_backend_app(tmp_path)
        self._write_secret(name)
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)

        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None
        assert "KIROCREW_GATEWAY_ORIGIN" not in env
        # No origin means nothing to prove: the proof is omitted even with a secret.
        assert "KIROCREW_GATEWAY_ORIGIN_PROOF" not in env
        # The unrelated proxy-secret behavior is unchanged.
        assert env["KIROCREW_PROXY_SECRET"] == self._SECRET

    def test_inherited_kirocrew_port_alone_does_not_produce_an_origin(
        self, tmp_path, app_env, monkeypatch
    ):
        """An inherited KIROCREW_PORT is NOT bound-port evidence: no fallback, origin omitted.

        Proves the injected origin never derives from resolve_serving_port()'s
        KIROCREW_PORT/default chain -- only the exported KIROCREW_BOUND_PORT counts.
        """
        name = self._install_backend_app(tmp_path)
        self._write_secret(name)
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        monkeypatch.setenv("KIROCREW_PORT", "5476")  # inherited / --port guess only

        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None
        assert "KIROCREW_GATEWAY_ORIGIN" not in env

    @pytest.mark.parametrize("bad", ["", "   ", "notaport", "80.5", "0", "-1", "65536", "99999"])
    def test_nonnumeric_zero_or_out_of_range_bound_port_omits_the_origin(
        self, tmp_path, app_env, monkeypatch, bad
    ):
        """Nonnumeric, zero, negative, and out-of-range (>65535) bound ports all omit."""
        name = self._install_backend_app(tmp_path)
        self._write_secret(name)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", bad)

        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None
        assert (
            "KIROCREW_GATEWAY_ORIGIN" not in env
        ), f"KIROCREW_BOUND_PORT={bad!r} is not valid bound-port evidence"

    def test_every_entrypoint_type_gets_the_same_generic_origin(
        self, tmp_path, app_env, monkeypatch
    ):
        """The origin is injected before type dispatch, so every entry type sees it identically.

        Covers the file-based backend types (python, asgi, node, and -- on POSIX
        -- exec). Module-style dotted entries are builtin-only and not installable
        here. The value is the same generic origin regardless of type.
        """
        import kiro_crew.apps.backend as bmod

        # A node backend must find a node binary before it reaches the spawn; the
        # binary is never executed (Popen is spied), so a stub path suffices.
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: sys.executable)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")

        cases = [
            ("py-plain", "backend/server.py", "python", "import http.server\n"),
            (
                "py-asgi",
                "backend/app.py",
                "asgi",
                "from fastapi import FastAPI\nimport uvicorn\napp = FastAPI()\n",
            ),
            ("node-app", "server.js", "node", 'require("http")\n'),
        ]
        if os.name == "posix":
            cases.append(("exec-app", "start.sh", "exec", "#!/bin/sh\nsleep 30\n"))

        origins = {}
        for name, entry_rel, backend_type, body in cases:
            self._install_typed_backend_app(tmp_path, name, entry_rel, backend_type, body)
            _result, env = self._capture_child_env(monkeypatch, name)
            assert env is not None, f"{backend_type} entry never reached the spawn boundary"
            origins[backend_type] = env["KIROCREW_GATEWAY_ORIGIN"]

        assert set(origins.values()) == {
            "http://127.0.0.1:8123"
        }, f"every entry type must get the same generic origin (saw {origins!r})"

    @requires_symlinks
    def test_node_backend_is_spawned_with_the_resolved_entry_path(
        self, tmp_path, app_env, monkeypatch
    ):
        """A symlinked app dir must not reach node's argv[1]: ESM main guards compare realpaths."""
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import app_dir

        monkeypatch.setattr(bmod, "_find_node_binary", lambda: sys.executable)
        self._install_typed_backend_app(tmp_path, "node-link", "server.mjs", "node", "//\n")
        linked = app_dir("node-link")
        real = tmp_path / "real-node-link"
        linked.rename(real)
        linked.symlink_to(real, target_is_directory=True)
        entry_args: list[str] = []
        real_popen = bmod.subprocess.Popen

        def _spy_popen(cmd, *a, **k):
            hits = [str(c) for c in cmd if str(c).endswith("server.mjs")]
            if not hits:  # an unrelated helper process (e.g. a sandbox probe)
                return real_popen(cmd, *a, **k)
            entry_args.extend(hits)
            raise OSError("captured argv; stop before the real spawn")

        monkeypatch.setattr(bmod, "wrap_argv", lambda cmd, **kw: (list(cmd), None))
        monkeypatch.setattr(bmod.subprocess, "Popen", _spy_popen)
        bmod.start_app_backend("node-link")

        assert entry_args, "the node spawn never reached the Popen boundary"
        assert entry_args == [str((real / "server.mjs").resolve())]

    def test_child_gets_exact_proof_keyed_by_the_app_secret(self, tmp_path, app_env, monkeypatch):
        """With a secret and a valid bound port, the proof is HMAC-SHA256(secret, origin)."""
        import hashlib
        import hmac

        name = self._install_backend_app(tmp_path)
        self._write_secret(name)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")

        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None, "the spawn never reached the Popen boundary"
        assert env["KIROCREW_GATEWAY_ORIGIN"] == "http://127.0.0.1:8123"
        # The proof is HMAC-SHA256(app_secret, origin) over the exact origin.
        expected = hmac.new(
            self._SECRET.encode("utf-8"),
            b"http://127.0.0.1:8123",
            hashlib.sha256,
        ).hexdigest()
        assert env["KIROCREW_GATEWAY_ORIGIN_PROOF"] == expected
        assert env["KIROCREW_PROXY_SECRET"] == self._SECRET

    def test_the_proof_verifies_under_the_app_secret_and_not_another(
        self, tmp_path, app_env, monkeypatch
    ):
        """A backend recomputing the proof with its secret accepts it; a wrong key rejects."""
        import hashlib
        import hmac

        name = self._install_backend_app(tmp_path)
        self._write_secret(name)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")

        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None
        origin = env["KIROCREW_GATEWAY_ORIGIN"].encode("utf-8")
        good = hmac.new(self._SECRET.encode("utf-8"), origin, hashlib.sha256).hexdigest()
        bad = hmac.new(b"a-different-secret", origin, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(env["KIROCREW_GATEWAY_ORIGIN_PROOF"], good)
        assert not hmac.compare_digest(env["KIROCREW_GATEWAY_ORIGIN_PROOF"], bad)

    def test_missing_secret_still_injects_origin_but_no_secret(
        self, tmp_path, app_env, monkeypatch
    ):
        """A missing .app_secret is tolerated: origin still set, no proof, no secret."""
        from kiro_crew.apps.manager import app_dir

        name = self._install_backend_app(tmp_path)
        (app_dir(name) / ".app_secret").unlink()  # install writes one; remove it
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")

        _result, env = self._capture_child_env(monkeypatch, name)
        assert env is not None
        assert env["KIROCREW_GATEWAY_ORIGIN"] == "http://127.0.0.1:8123"
        assert "KIROCREW_GATEWAY_ORIGIN_PROOF" not in env
        assert "KIROCREW_PROXY_SECRET" not in env

    def test_a_non_entrypoint_app_is_not_spawned_and_gets_no_injection(
        self, tmp_path, app_env, monkeypatch
    ):
        """An app with no backend.entryPoint is never spawned, so nothing is injected."""
        name = "no-entrypoint-app"
        src = tmp_path / "source" / name
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "displayName": "No Entry",
                    "description": "no backend entryPoint",
                    "author": "tester",
                }
            )
        )
        install_app(src)
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")

        result, env = self._capture_child_env(monkeypatch, name)
        assert result is None
        assert env is None, "a non-entryPoint app must never reach the spawn boundary"


class TestTheCacheOnlyChildCanSeeTheCacheItMustBootFrom:
    """``policy_cache`` is bind-mount-hidden in every sandbox tier.

    That is deliberate — it keeps the AGENT's own subprocesses from reading or rewriting
    the ceiling — but an app backend in cache-only mode resolves the fleet ceiling FROM
    that file and FAILS CLOSED without it. Without the visibility carve-out the two
    controls contradict each other and every app backend on a centrally-governed host
    exits at boot.
    """

    def test_the_spawn_passes_the_cache_as_a_visible_dir(self, app_env, tmp_path, monkeypatch):
        import kiro_crew.apps.backend as bmod
        from kiro_crew.platform import policy_distribution as pd

        # A gateway whose OWN ceiling came from the central tier is the only one that
        # flags a child cache-only, so that is the state to reproduce.
        pd._record_installed(b'{"version": 1, "boot": {}}')
        try:
            seen: dict = {}

            def _spy_wrap(argv, **kwargs):
                seen["visible"] = kwargs.get("extra_visible_dirs")
                return (list(argv), None)

            monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
            monkeypatch.setattr(
                bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
            )

            src = tmp_path / "source" / "cache-only-app"
            src.mkdir(parents=True)
            (src / APP_MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "name": "cache-only-app",
                        "version": "1.0.0",
                        "displayName": "Cache only",
                        "description": "policy cache visibility",
                        "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
                    }
                )
            )
            (src / "server.py").write_text("import time\ntime.sleep(30)\n")
            install_app(src)

            bmod.start_app_backend("cache-only-app")

            assert seen.get("visible"), "the spawn passed no visible dirs at all"
            assert str(pd.cache_dir()) in seen["visible"], (
                "the policy cache is hidden from a child that fails closed without it, "
                f"so every app backend would exit at boot (saw {seen['visible']!r})"
            )
        finally:
            pd.reset_process_state()

    @staticmethod
    def _spawn_with_wrap(bmod, tmp_path, monkeypatch, name, wrap):
        monkeypatch.setattr(bmod, "wrap_argv", wrap)
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
        )
        src = tmp_path / "source" / name
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": name,
                    "version": "1.0.0",
                    "displayName": name,
                    "description": "confinement reporting",
                    "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
                }
            )
        )
        (src / "server.py").write_text("import time\ntime.sleep(30)\n")
        install_app(src)
        bmod.start_app_backend(name)

    def test_an_unconfined_host_says_so_once_and_names_the_control(
        self, app_env, tmp_path, monkeypatch, caplog
    ):
        """A centrally governed host with no OS sandbox runs app code unconfined, so the
        read-only seal on the cache does not exist. NOT a refusal: an unconfined process can
        rewrite security_policy.json and the admission policy directly — the keystone gate
        covers TOOL CALLS, not an arbitrary process's ``open()`` — so forbidding an app to READ
        the ceiling while it can REPLACE the ceiling protects nothing. What does still hold is
        provenance, so the warning names it.
        """
        import logging

        import kiro_crew.apps.backend as bmod
        from kiro_crew.platform import policy_distribution as pd

        pd._record_installed(b'{"version": 1, "boot": {}}')
        monkeypatch.setattr(bmod, "_warned_unconfined_cache", False, raising=False)
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.backend"):
                # An unwrapped argv is exactly what wrap_argv returns with no sandbox backend.
                self._spawn_with_wrap(
                    bmod,
                    tmp_path,
                    monkeypatch,
                    "unconfined-app",
                    lambda argv, **kw: (list(argv), None),
                )
            messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
            said = [m for m in messages if "no OS sandbox" in m]
            assert said, f"the combination must be named (saw {messages!r})"
            assert "require_policy_signature" in said[0], "and the actionable control with it"
        finally:
            pd.reset_process_state()

    def test_a_confined_host_says_nothing(self, app_env, tmp_path, monkeypatch, caplog):
        """The control: where the seal applies there is nothing to warn about."""
        import logging

        import kiro_crew.apps.backend as bmod
        from kiro_crew.platform import policy_distribution as pd

        pd._record_installed(b'{"version": 1, "boot": {}}')
        monkeypatch.setattr(bmod, "_warned_unconfined_cache", False, raising=False)
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.backend"):
                # A CHANGED argv is what a real wrap returns.
                self._spawn_with_wrap(
                    bmod,
                    tmp_path,
                    monkeypatch,
                    "confined-app",
                    lambda argv, **kw: (["sandbox-exec", "-f", "/tmp/p.sb", *argv], None),
                )
            assert not [
                r for r in caplog.records if "no OS sandbox" in r.getMessage()
            ], "a confined spawn must not warn"
        finally:
            pd.reset_process_state()

    def test_an_ungoverned_host_passes_no_visible_dirs(self, app_env, tmp_path, monkeypatch):
        """No central ceiling means no cache-only flag and nothing to un-hide."""
        import kiro_crew.apps.backend as bmod
        from kiro_crew.platform import policy_distribution as pd

        pd.reset_process_state()
        seen: dict = {}

        def _spy_wrap(argv, **kwargs):
            seen["visible"] = kwargs.get("extra_visible_dirs")
            return (list(argv), None)

        monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
        )

        src = tmp_path / "source" / "plain-app"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": "plain-app",
                    "version": "1.0.0",
                    "displayName": "Plain",
                    "description": "no central policy",
                    "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
                }
            )
        )
        (src / "server.py").write_text("import time\ntime.sleep(30)\n")
        install_app(src)

        bmod.start_app_backend("plain-app")

        assert seen.get("visible") == ()


class TestTheMdNotebookBackendCanSeeItsOwnStateFiles:
    """The md-notebook spawn's isolated startup, which rides with its mask carve-out.

    The carve-out itself is main's (``app_backend_visible_targets``, pinned in
    ``test_sandbox_governance_mask.py``). What is pinned HERE is the startup shape that
    has to accompany it: this is the one spawn whose namespace holds an unmasked PAT, so
    a bare ``python -m`` — which runs ``sitecustomize`` / ``usercustomize`` and the user
    site's ``.pth`` files from an agent-writable directory — would execute that injected
    code right where the token is readable. The spawn therefore starts with ``-I`` and
    re-states its import root explicitly, and that rewrite is scoped to this spawn alone.
    """

    @staticmethod
    def _spy(bmod, monkeypatch):
        seen: dict = {}

        def _spy_wrap(argv, **kwargs):
            seen["visible"] = kwargs.get("extra_visible_dirs")
            seen["argv"] = list(argv)
            return (list(argv), None)

        monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
        monkeypatch.setattr(
            bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
        )
        return seen

    def test_the_shipped_builtin_spawn_starts_isolated_with_the_carveout(
        self, app_env, monkeypatch
    ):
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import register_builtin_apps
        from kiro_crew.sandbox import MD_NOTEBOOK_APP_NAME, app_backend_visible_targets

        register_builtin_apps()
        seen = self._spy(bmod, monkeypatch)

        bmod.start_app_backend("md-notebook")

        assert seen.get("visible"), "the spawn passed no visible dirs at all"
        for path in app_backend_visible_targets(MD_NOTEBOOK_APP_NAME):
            assert path in seen["visible"], (
                "md-notebook's own state stays masked from the one spawn that "
                f"owns it, so attach/clone still EPERMs (missing {path!r}, "
                f"saw {seen['visible']!r})"
            )
        # Startup isolation rides with the carve-out: a bare `python -m` runs
        # sitecustomize/usercustomize, and the default user site is an
        # agent-writable injection path that would execute inside the one
        # namespace where the PAT is unmasked.
        argv = seen["argv"]
        assert "-I" in argv, f"module builtin spawned without isolated startup: {argv!r}"
        assert "-m" not in argv and any(
            "runpy" in a and "kiro_crew.apps.builtins.md_notebook" in a for a in argv
        ), f"the module must run via runpy with an explicit import root: {argv!r}"

    def test_other_module_builtins_keep_the_bare_module_launch(self, app_env, monkeypatch):
        """The isolated-startup rewrite is scoped to the spawn that carries the
        carve-out: the other module builtins have no unmasked secret in their
        namespace, and rewriting their import environment would be a rider on a
        fix scoped to one (First Principles review)."""
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import register_builtin_apps

        register_builtin_apps()
        seen = self._spy(bmod, monkeypatch)

        bmod.start_app_backend("file-explorer")

        argv = seen["argv"]
        assert (
            "-I" not in argv and "-m" in argv
        ), f"a non-md-notebook module builtin changed launch shape: {argv!r}"
        assert seen.get("visible") == (), "a non-md-notebook builtin received the state carve-out"

    def test_a_third_party_app_wearing_the_name_keeps_the_mask(
        self, app_env, tmp_path, monkeypatch
    ):
        """The carve-out is bought with provenance, never with a name: an app
        NAMED md-notebook that executes from the mutable installed tree (here
        via the blanket third-party toggle the fixture enables) must not see
        the GitHub token."""
        import kiro_crew.apps.backend as bmod

        seen = self._spy(bmod, monkeypatch)

        src = tmp_path / "source" / "md-notebook"
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text(
            json.dumps(
                {
                    "name": "md-notebook",
                    "version": "9.9.9",
                    "displayName": "Impostor",
                    "description": "wears the builtin's name",
                    "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
                }
            )
        )
        (src / "server.py").write_text("import time\ntime.sleep(30)\n")
        install_app(src)

        bmod.start_app_backend("md-notebook")

        assert seen.get("visible") == (), (
            "a third-party app bought the md-notebook state carve-out with its "
            f"name alone (saw {seen.get('visible')!r})"
        )


@pytest.mark.skipif(sys.platform == "win32", reason="shebang semantics are POSIX-only")
class TestExecBackendShebangShim:
    def _spawn_cmd(self, tmp_path, shebang_line: str):
        """Build the exec-arm inputs and return the resolved cmd."""
        import kiro_crew.apps.backend as bk
        from kiro_crew.apps.interpreter import app_deps_dir

        root = tmp_path / "app"
        root.mkdir()
        (root / "requirements.txt").write_bytes(b"requests\n")
        d = app_deps_dir(root)
        d.mkdir(parents=True)
        (d / bk._DEPS_STAMP_NAME).write_text(bk._deps_digest(b"requests\n"))
        (d / bk._DEPS_ABI_NAME).write_text(bk._deps_abi_tag())
        script = root / "run"
        script.write_text(f"{shebang_line}\nimport requests\n")
        script.chmod(0o755)
        return bk, root, script

    def test_an_abi_matched_shebang_script_launches_through_deps_boot(self, tmp_path):
        import sys as _sys

        bk, root, script = self._spawn_cmd(tmp_path, f"#!{_sys.executable}")
        got = bk._abi_shebang_of(root, str(script))
        assert got == _sys.executable

    def test_an_argument_bearing_shebang_keeps_its_flags(self, tmp_path):
        """#!<python> -I keeps its kernel launch: the shared reader answers
        None for argument-bearing shebangs, so no rewrite happens - and the
        flag REMAINS in what actually executes. Both halves are asserted:
        not a candidate, and the on-disk launch still carries -I exactly as
        written (the kernel, not a rewrite, interprets the shebang)."""
        import sys as _sys

        bk, root, script = self._spawn_cmd(tmp_path, f"#!{_sys.executable} -I")
        assert bk._abi_shebang_of(root, str(script)) is None
        first_line = script.read_bytes().split(b"\n", 1)[0]
        assert first_line == f"#!{_sys.executable} -I".encode()
        # And a no-candidate script is launched as-is: simulate the exec-arm
        # decision the spawn makes with this answer.
        cmd = [str(script), "--serve"]
        si = bk._abi_shebang_of(root, cmd[0])
        assert si is None
        # the arm leaves cmd untouched when there is no shim candidate
        assert cmd == [str(script), "--serve"]

    def test_a_foreign_shebang_is_not_a_shim_candidate(self, tmp_path):
        bk, root, script = self._spawn_cmd(tmp_path, "#!/opt/foreign/python3.11")
        assert bk._abi_shebang_of(root, str(script)) is None
