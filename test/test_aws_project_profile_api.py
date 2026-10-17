"""``GET``/``PUT /api/aws/project-profile``: the project chip's AWS profile binding.

Owner-only in both directions, names-only in both directions: the response never
carries an account id, an ARN or the CLI's failure text, and a write stores only a
name the AWS profile-name charset admits.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import aws_consent


@pytest.fixture(autouse=True)
def _no_real_aws_cli(_floor_monkeypatch: pytest.MonkeyPatch):
    """Fail any test that reaches the real AWS CLI through the health probe.

    A GET for a bound project runs ``sts get-caller-identity`` and two ``aws configure``
    reads in a subprocess, which the off-loopback socket hook never sees. A test that
    needs the probe stubs it (or ``_profile_status``) itself, which replaces these.
    Patched through ``_floor_monkeypatch`` so a test's ``monkeypatch.undo()`` cannot lift it.
    """
    from kiro_crew.apps.builtins.aws_control.backend import accounts

    reached: list[str] = []

    def _guard(name: str) -> AsyncMock:
        async def _record(*_args: object, **_kwargs: object) -> object:
            reached.append(name)
            raise RuntimeError(f"unstubbed {name}")

        return AsyncMock(side_effect=_record)

    _floor_monkeypatch.setattr(aws_consent, "probe_identity", _guard("probe_identity"))
    _floor_monkeypatch.setattr(accounts, "configured_region", _guard("configured_region"))
    _floor_monkeypatch.setattr(accounts, "classify_profile", _guard("classify_profile"))
    yield
    assert not reached, f"the test reached the real AWS CLI through {reached}"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    aws_consent._probe_cache.clear()
    yield tmp_path
    aws_consent._probe_cache.clear()


def _request(
    *,
    app: str = "",
    user: str = "owner-1",
    owner: str = "owner-1",
    query: dict | None = None,
    body: object = None,
):
    req = MagicMock()
    req.path = "/api/aws/project-profile"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    req.rel_url.query = query or {}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


def _stored(home: Path) -> dict:
    from kiro_crew.config.loader import config_path

    path = config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _project(home: Path, name: str = "proj") -> Path:
    path = home / "src" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _put(body: object, **kw):
    from kiro_crew.dashboard.handlers import aws_project_profile as handler

    return asyncio.run(handler.api_aws_project_profile_put(_request(body=body, **kw)))


def _get(path: str, **kw):
    from kiro_crew.dashboard.handlers import aws_project_profile as handler

    return asyncio.run(handler.api_aws_project_profile_get(_request(query={"path": path}, **kw)))


class TestOwnerOnly:
    @pytest.mark.parametrize("app,user", [("", "slack-guest"), ("some-app", "owner-1"), ("", "")])
    def test_a_non_owner_cannot_read_or_bind(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, app: str, user: str
    ) -> None:
        import kiro_crew.sel as sel_mod

        audited: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kw: object) -> None:
                audited.append(dict(kw))

        monkeypatch.setattr(sel_mod, "sel", lambda: _Recorder())
        proj = _project(home)
        put = _put({"path": str(proj), "profile": "dev"}, app=app, user=user)
        get = _get(str(proj), app=app, user=user)
        assert put.status == 403 and get.status == 403
        assert json.loads(put.text)["code"] == "owner_only"
        assert "aws" not in _stored(home)
        # Both refusals reach the security event log, the read as well as the write.
        assert [(a["operation"], a["outcome"]) for a in audited] == [
            ("aws_project_profile.write", "denied"),
            ("aws_project_profile.read", "denied"),
        ]


class TestBind:
    def test_bind_then_read_back(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard.handlers import aws_project_profile as handler

        monkeypatch.setattr(handler, "_profile_status", AsyncMock(return_value="ok"))
        proj = _project(home)
        resp = _put({"path": str(proj) + "/", "profile": "billing-prod"})
        assert resp.status == 200
        assert _stored(home)["aws"]["project_profiles"] == {str(proj): "billing-prod"}

        read = json.loads(_get(str(proj / "infra")).text)
        assert read["profile"] == "billing-prod"
        assert read["boundDir"] == str(proj)
        assert read["status"] == "ok"
        assert "billing-prod" in read["profiles"]

    def test_unbind_removes_every_spelling_of_the_directory(self, home: Path) -> None:
        proj = _project(home)
        other = _project(home, "other")
        assert _put({"path": str(proj), "profile": "dev"}).status == 200
        assert _put({"path": str(other), "profile": "ops"}).status == 200
        assert _put({"path": str(proj) + "/", "profile": ""}).status == 200
        assert _stored(home)["aws"]["project_profiles"] == {str(other): "ops"}
        read = json.loads(_get(str(proj)).text)
        assert read["profile"] is None and read["status"] is None

    @pytest.mark.parametrize("profile", ["bad name", "-x", "a;b", 7, "a" * 129])
    def test_an_invalid_profile_is_refused_and_nothing_written(
        self, home: Path, profile: object
    ) -> None:
        proj = _project(home)
        resp = _put({"path": str(proj), "profile": profile})
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_aws_profile"
        assert "aws" not in _stored(home)

    @pytest.mark.parametrize("path", ["relative/dir", "", 5, None])
    def test_a_path_that_is_not_absolute_is_refused(self, home: Path, path: object) -> None:
        resp = _put({"path": path, "profile": "dev"})
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_project_path"
        get = _get(path if isinstance(path, str) else "")
        assert get.status == 400

    @pytest.mark.skipif(os.name == "nt", reason="Windows drops trailing spaces from path names")
    def test_surrounding_whitespace_names_a_different_directory(self, home: Path) -> None:
        proj = _project(home)
        assert _put({"path": str(proj), "profile": "prod"}).status == 200
        assert _put({"path": str(proj) + " ", "profile": "dev"}).status == 200
        assert _stored(home)["aws"]["project_profiles"] == {
            str(proj): "prod",
            str(proj) + " ": "dev",
        }

    def test_binding_through_a_symlink_target_replaces_the_alias(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.handlers import aws_project_profile as handler

        monkeypatch.setattr(handler, "_profile_status", AsyncMock(return_value="ok"))
        proj = _project(home)
        link = home / "src" / "proj-link"
        try:
            link.symlink_to(proj, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this user")
        assert _put({"path": str(link), "profile": "old"}).status == 200
        assert _put({"path": str(proj), "profile": "new"}).status == 200
        assert _stored(home)["aws"]["project_profiles"] == {str(proj): "new"}
        assert json.loads(_get(str(proj)).text)["profile"] == "new"

    def test_a_hand_edited_key_with_a_nul_is_not_a_crash(self, home: Path) -> None:
        from kiro_crew.config.loader import config_path

        proj = _project(home)
        bad = str(proj.parent / "x\x00y")
        config_path().write_text(
            json.dumps({"aws": {"project_profiles": {bad: "dev"}}}), encoding="utf-8"
        )
        resp = _put({"path": str(proj), "profile": "prod"})
        # The writer's own refusal (or a clean write), never an unhandled 500, and the
        # operator's entry is not silently dropped by the alias sweep.
        assert resp.status != 500
        assert bad in _stored(home)["aws"]["project_profiles"]

    def test_a_non_object_body_is_refused(self, home: Path) -> None:
        resp = _put(["/a", "dev"])
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_json"


class TestHealthIsNamesOnly:
    def test_an_expired_sso_profile_reads_expired_with_no_account_or_arn(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        probe = AsyncMock(
            return_value=aws_consent.Identity(
                ok=False,
                account="123456789012",
                arn="arn:aws:sts::123456789012:assumed-role/Admin/me",
                detail="The SSO session associated with this profile has expired",
            )
        )
        monkeypatch.setattr(aws_consent, "probe_identity", probe)
        monkeypatch.setattr(accounts, "configured_region", AsyncMock(return_value="eu-west-1"))
        monkeypatch.setattr(accounts, "classify_profile", AsyncMock(return_value=accounts.KIND_SSO))
        proj = _project(home)
        _put({"path": str(proj), "profile": "sso-dev"})

        resp = _get(str(proj))
        body = json.loads(resp.text)
        assert body["status"] == "expired"
        assert "123456789012" not in resp.text
        assert "arn:" not in resp.text
        probe.assert_awaited_once_with("sso-dev", "eu-west-1")

    def test_a_non_sso_profile_that_does_not_resolve_reads_unavailable(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        monkeypatch.setattr(
            aws_consent, "probe_identity", AsyncMock(return_value=aws_consent.Identity(ok=False))
        )
        monkeypatch.setattr(accounts, "configured_region", AsyncMock(return_value=""))
        monkeypatch.setattr(
            accounts, "classify_profile", AsyncMock(return_value=accounts.KIND_OTHER)
        )
        proj = _project(home)
        _put({"path": str(proj), "profile": "static"})
        assert json.loads(_get(str(proj)).text)["status"] == "unavailable"

    def test_an_unbound_project_runs_no_probe(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = AsyncMock(side_effect=AssertionError("no probe for an unbound project"))
        monkeypatch.setattr(aws_consent, "probe_identity", probe)
        proj = _project(home)
        body = json.loads(_get(str(proj)).text)
        assert body["profile"] is None and body["status"] is None
        probe.assert_not_awaited()
