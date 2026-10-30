"""The CLI install explains an npm registry refusal instead of showing npm's text.

When the operator's own npm registry refuses the install (``code E401`` /
``code E403``, usually an expired login), the npm step names that registry and
says how to recover, and the job carries a code the dashboard turns into its own
message and an opt-in retry from the public registry. Any other npm failure
keeps npm's own output exactly as before, and the default install never changes
which registry npm uses.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from browser_cli_install_helpers import _clear_node_bin_dir_caches  # noqa: F401
from browser_cli_install_helpers import _default_no_os_deps  # noqa: F401
from browser_cli_install_helpers import isolated_browser_cache_fixture  # noqa: F401
from browser_cli_install_helpers import _wire

from kiro_crew.browser_cli import install as mod
from kiro_crew.browser_cli import install_job as job_mod

_E401 = (
    "npm error code E401\n"
    "npm error Unable to authenticate, your authentication token seems to be invalid.\n"
    "npm error To correct this please try logging in again with:\n"
    "npm error   npm login\n"
)
_E403 = (
    "npm error code E403\n"
    "npm error 403 403 Forbidden - GET https://registry.example.test/npm/shared/@playwright%2fcli\n"
)
_E404 = (
    "npm error code E404\n"
    "npm error 404 Not Found - GET https://registry.example.test/npm/shared/@playwright%2fcli\n"
)
_CONFIGURED = "https://registry.example.test/npm/shared/"
#: What the message shows for it: the host only, never the path.
_REGISTRY = "https://registry.example.test/"
_PUBLIC = "https://registry.npmjs.org/"


def _npm_fails_with(
    monkeypatch: pytest.MonkeyPatch,
    install_output: str,
    configured: str = _CONFIGURED,
    scoped: str = "undefined",
) -> list[list[str]]:
    """npm's install fails with *install_output*.

    ``npm config get registry`` answers *configured* and ``npm config get
    @playwright:registry`` answers *scoped* (npm prints ``undefined`` when unset).
    """
    _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
    calls: list[list[str]] = []

    def fake_run(argv: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append(list(argv))
        if argv[:4] == ["/n/npm", "config", "get", "@playwright:registry"]:
            return 0, f"{scoped}\n", ""
        if argv[:4] == ["/n/npm", "config", "get", "registry"]:
            return 0, f"{configured}\n", ""
        if argv[:2] == ["/n/npm", "install"]:
            return 1, "", install_output
        return 0, "", ""

    monkeypatch.setattr(mod, "_run", fake_run)
    return calls


class TestARegistryRefusalIsExplained:
    @pytest.mark.parametrize(("output", "code"), [(_E401, "E401"), (_E403, "E403")])
    def test_the_step_names_the_configured_registry_and_the_way_out(
        self, monkeypatch: pytest.MonkeyPatch, output: str, code: str
    ) -> None:
        _npm_fails_with(monkeypatch, output)

        result = mod.install()

        assert result["ok"] is False
        step = result["steps"][-1]
        assert step["name"] == "npm-install-global"
        assert _REGISTRY in step["stderr"]
        assert f"npm {code}" in step["stderr"]
        assert "Sign in to that registry again" in step["stderr"]
        assert _PUBLIC in step["stderr"]
        # npm's own advice is the part that misleads; it is not repeated.
        assert "npm login" not in step["stderr"]
        assert step["error_code"] == "registry_auth"
        assert step["error_params"] == {"registry": _REGISTRY, "npm_code": code}

    def test_the_job_carries_the_code_and_the_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _npm_fails_with(monkeypatch, _E401)

        result = mod.install()

        status, code, detail = job_mod.outcome_of(result, "install")
        assert (status, code) == ("failed", "registry_auth")
        assert detail is not None and _REGISTRY in detail
        assert job_mod.error_params_of(result) == {"registry": _REGISTRY, "npm_code": "E401"}

    def test_a_refusal_past_the_detail_cap_is_still_recognised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """npm prints the code first; a long tail must not cut it out of view."""
        _npm_fails_with(monkeypatch, _E401 + "npm error verbose x\n" * 500)

        step = mod.install()["steps"][-1]

        assert step["error_code"] == "registry_auth"

    def test_a_credential_in_the_registry_url_is_not_shown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _npm_fails_with(
            monkeypatch,
            _E401,
            configured="https://someone:hunter2@registry.example.test/npm/shared/?token=abc#x",
        )

        step = mod.install()["steps"][-1]

        assert "hunter2" not in step["stderr"]
        assert "someone" not in step["stderr"]
        assert "abc" not in step["stderr"]
        assert step["error_params"]["registry"] == _REGISTRY

    def test_a_token_in_the_registry_path_is_not_shown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _npm_fails_with(
            monkeypatch, _E401, configured="https://npm-proxy.example.test/s3cr3tT0k3n/someone/"
        )

        step = mod.install()["steps"][-1]

        assert "s3cr3tT0k3n" not in step["stderr"]
        assert step["error_params"]["registry"] == "https://npm-proxy.example.test/"

    def test_an_unreadable_registry_setting_is_not_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _npm_fails_with(monkeypatch, _E401, configured="undefined")

        step = mod.install()["steps"][-1]

        assert step["error_code"] == "registry_auth"
        assert step["error_params"]["registry"] is None
        assert "your configured npm registry (npm E401)" in step["stderr"]

    def test_a_scoped_registry_for_the_package_is_the_one_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """npm fetches ``@playwright/cli`` from ``@playwright:registry`` when set."""
        _npm_fails_with(monkeypatch, _E401, scoped="https://scoped.example.test/npm/pw/")

        step = mod.install()["steps"][-1]

        assert step["error_params"]["registry"] == "https://scoped.example.test/"
        assert _REGISTRY not in step["stderr"]

    def test_the_lookup_reads_the_configuration_the_install_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project ``.npmrc`` that a global install ignores is not the registry named.

        npm answers the project value unless asked in the install's own global
        context; the fake models that, so naming the project registry fails here.
        """
        _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
        calls: list[list[str]] = []
        project = "https://project.example.test/"

        def fake_run(argv: list[str], timeout: float) -> tuple[int, str, str]:
            calls.append(list(argv))
            if argv[:3] == ["/n/npm", "config", "get"]:
                install_prefix = calls[0][4]
                global_context = argv[4:] == ["--global", "--prefix", install_prefix]
                if argv[3] == "@playwright:registry":
                    return 0, ("undefined\n" if global_context else f"{project}\n"), ""
                return 0, (f"{_CONFIGURED}\n" if global_context else f"{project}\n"), ""
            if argv[:2] == ["/n/npm", "install"]:
                return 1, "", _E401
            return 0, "", ""

        monkeypatch.setattr(mod, "_run", fake_run)

        step = mod.install()["steps"][-1]

        assert step["error_params"]["registry"] == _REGISTRY
        assert "project.example.test" not in step["stderr"]


class TestAnyOtherNpmFailureKeepsTodaysBehaviour:
    def test_npm_output_is_shown_as_it_was(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _npm_fails_with(monkeypatch, _E404)

        result = mod.install()

        step = result["steps"][-1]
        assert step["stderr"] == _E404.strip()
        assert set(step) == {"name", "ok", "returncode", "stderr", "hint"}
        assert "error_params" not in step
        # No extra npm call is made to explain a failure that is not a refusal.
        assert [c for c in calls if c[1:3] == ["config", "get"]] == []
        status, code, _detail = job_mod.outcome_of(result, "install")
        assert (status, code) == ("failed", "step_failed")
        assert job_mod.error_params_of(result) is None

    def test_a_lifecycle_script_mentioning_e401_is_not_a_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only npm's own first ``code`` line decides, not text a script printed."""
        output = (
            "> postinstall\n"
            "upstream said: code E401 while fetching a mirror\n"
            "npm error code 1\n"
            "npm error command failed\n"
        )
        calls = _npm_fails_with(monkeypatch, output)

        step = mod.install()["steps"][-1]

        assert step["stderr"] == output.strip()
        assert "error_code" not in step
        assert [c for c in calls if c[1:3] == ["config", "get"]] == []

    def test_older_npm_wording_is_recognised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _npm_fails_with(monkeypatch, "npm ERR! code E401\nnpm ERR! Unable to authenticate\n")

        step = mod.install()["steps"][-1]

        assert step["error_code"] == "registry_auth"


class TestThePublicRegistryIsOnlyEverOptIn:
    def test_the_default_install_does_not_name_a_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _npm_fails_with(monkeypatch, _E401)

        mod.install()

        npm_install = next(c for c in calls if c[1:2] == ["install"])
        assert not any("registry" in arg for arg in npm_install)

    def test_the_opt_in_installs_the_cli_from_the_public_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})

        result = mod.install(public_registry=True)

        assert result["ok"] is True
        assert calls[0][:5] == ["/n/npm", "install", "-g", "--prefix", calls[0][4]]
        assert calls[0][5:] == [
            "--registry",
            _PUBLIC,
            f"--@playwright:registry={_PUBLIC}",
            "@playwright/cli@latest",
        ]

    def test_a_refusal_from_the_public_registry_offers_no_further_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _npm_fails_with(monkeypatch, _E403)

        result = mod.install(public_registry=True)

        step = result["steps"][-1]
        assert step["error_code"] == "public_registry_refused"
        assert step["error_params"] == {"registry": _PUBLIC, "npm_code": "E403"}
        assert "public npm registry" in step["stderr"]
        assert [c for c in calls if c[1:3] == ["config", "get"]] == []
        assert job_mod.outcome_of(result, "install")[1] == "public_registry_refused"


def _drive_start(monkeypatch: pytest.MonkeyPatch, body: Any) -> tuple[Any, list[bool]]:
    """POST /api/browser/install with *body*; return the response and each install's opt-in."""
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import messaging as msg

    seen: list[bool] = []

    def fake_install(on_stage: Any = None, *, public_registry: bool = False) -> dict[str, Any]:
        seen.append(public_registry)
        return {"ok": True, "steps": []}

    monkeypatch.setattr(msg.browser_cli_install, "install", fake_install)
    monkeypatch.setattr(msg.browser_cli_install, "detect", lambda: {"installed": True})
    monkeypatch.setattr(msg.browser_cli_token, "has_token", lambda: False)

    async def _go() -> Any:
        state = type("S", (), {})()
        state.owner_id = "the-owner"
        state._browser_install_task = None
        state._browser_install_job = None
        req = MagicMock()
        req.path = "/api/browser/install"
        claims = {"app": "", "user": "the-owner"}
        req.get = lambda key, default=None: claims.get(key, default)
        req.__contains__ = lambda _self, key: key in claims
        req.__getitem__ = lambda _self, key: claims[key]

        async def _json() -> Any:
            if isinstance(body, Exception):
                raise body
            return body

        req.json = _json
        req.app = {"state": state}
        resp = await msg.api_browser_install_start(req)
        if state._browser_install_task is not None:
            await state._browser_install_task
        return resp

    return asyncio.run(_go()), seen


class TestTheInstallRouteOptIn:
    def test_public_registry_is_used_only_when_asked_for(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resp, seen = _drive_start(monkeypatch, {"registry": "public"})
        assert resp.status == 200
        assert seen == [True]

    @pytest.mark.parametrize("body", [{}, None, ValueError("no body"), [1]])
    def test_any_other_body_is_the_default_install(
        self, monkeypatch: pytest.MonkeyPatch, body: Any
    ) -> None:
        resp, seen = _drive_start(monkeypatch, body)
        assert resp.status == 200
        assert seen == [False]

    def test_an_unknown_registry_is_refused_with_a_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        resp, seen = _drive_start(monkeypatch, {"registry": "https://elsewhere.test/"})
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "unknown_registry"
        assert seen == []
