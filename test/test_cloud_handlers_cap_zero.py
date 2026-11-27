"""The cloud preflight's 503 body for a host whose user-namespace cap reads as 0."""

from __future__ import annotations

import pytest
from test_cloud_handlers import _body, _posix_host, _req, _state  # noqa: F401

from kiro_crew.dashboard import handlers_cloud as hc


@pytest.mark.asyncio
class TestPreflightCapZeroRefusal:
    @pytest.mark.parametrize("kind", ["transient", "no_backend"])
    async def test_preflight_cap_zero_refusal_names_the_disabled_cap(
        self, tmp_path, monkeypatch, kind
    ):
        """A cap read as 0 is named for any kind, never as a limit a moment clears."""
        import kiro_crew.sandbox
        from kiro_crew.sandbox import SandboxUnavailableError

        def _refused(p, r):
            raise SandboxUnavailableError(
                "refused",
                kind=kind,
                detail="probe",
                remedy=kiro_crew.sandbox.REMEDY_USERNS_CAP_ZERO,
            )

        monkeypatch.setattr(hc.iam, "reachability_check", _refused)
        resp = await hc.api_cloud_preflight(
            _req("GET", "/api/cloud/preflight", state=_state(tmp_path))
        )
        assert resp.status == 503
        body = _body(resp)
        assert body["code"] == "sandbox_unavailable"
        assert body["kind"] == kind
        assert body["detail"] == "probe"
        assert body["remedy"] == "userns_cap_zero"
        assert "user.max_user_namespaces=0" in body["error"]
        assert "kirocrew doctor" in body["error"]
        assert "credentials were not checked" in body["error"]
        error = body["error"].lower()
        for phrase in ("temporary", "try again", "in a moment"):
            assert phrase not in error, phrase
