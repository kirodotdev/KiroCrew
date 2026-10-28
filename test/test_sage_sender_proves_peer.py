"""The Code Review Sage app sender proves the gateway peer before it writes the
internal secret, reusing the same connection-bound proof the CLI internal-secret
senders use (``kiro_crew.cli_server_peer._owner_verified_secret_urlopen``). An
unproven peer -- a foreign process that grabbed the loopback port -- receives
nothing.
"""

from kiro_crew.apps.builtins.code_review_sage.sage_lib import review_driver
from kiro_crew.cli_server_peer import _GatewayPeerRefused


class _RecordingProof:
    """Stands in for the peer proof: records each call, optionally refuses."""

    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[tuple[object, int, str]] = []
        self.refuse = refuse

    def __call__(self, req, port, timeout, *, operation):
        self.calls.append((req, port, operation))
        if self.refuse:
            raise _GatewayPeerRefused("connected peer is not this install's gateway")

        class _Resp:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp()


def _pin_gateway(monkeypatch, proof):
    monkeypatch.setattr(review_driver, "_owner_verified_secret_urlopen", proof, raising=False)
    monkeypatch.setattr(review_driver, "_gateway_base", lambda: "http://127.0.0.1:41234")
    monkeypatch.setattr(review_driver, "_local_secret", lambda port: "s3cr3t")


class TestSageApiRequestProvesThePeer:
    def test_the_send_goes_through_the_peer_proof(self, monkeypatch):
        proof = _RecordingProof()
        _pin_gateway(monkeypatch, proof)
        review_driver._api_request("GET", "/api/spawn")
        assert proof.calls, "the credential-bearing send must run through the peer proof"
        req, port, operation = proof.calls[0]
        assert port == 41234
        assert operation == "sage_api"
        # urllib capitalizes a header's first letter only.
        assert req.get_header("X-internal-secret") == "s3cr3t"

    def test_an_unproven_peer_receives_nothing(self, monkeypatch):
        proof = _RecordingProof(refuse=True)
        _pin_gateway(monkeypatch, proof)
        out = review_driver._api_request("GET", "/api/spawn")
        assert "not verified" in out.get("error", "")
        assert len(proof.calls) == 1  # one attempt, refused before any byte, nothing retried

    def test_a_refusal_keeps_the_proofs_own_reason(self, monkeypatch):
        _pin_gateway(monkeypatch, _RecordingProof(refuse=True))
        monkeypatch.setattr(review_driver, "_sandboxed_tcp_only", lambda port: False)
        out = review_driver._api_request("GET", "/api/spawn")
        assert out["error"] == (
            "gateway peer not verified; request not sent: "
            "connected peer is not this install's gateway"
        )

    def test_a_sandboxed_tcp_only_refusal_adds_the_socket_remedy(self, monkeypatch):
        # A TCP-only gateway cannot be proven from the Linux sandbox, so the error
        # adds how to get the owner-only socket back after the proof's own reason.
        _pin_gateway(monkeypatch, _RecordingProof(refuse=True))
        monkeypatch.setattr(review_driver, "_sandboxed_tcp_only", lambda port: True)
        out = review_driver._api_request("GET", "/api/spawn")
        assert "connected peer is not this install's gateway" in out["error"]
        assert "shorter data home" in out["error"]


class TestSandboxedTcpOnly:
    def _setup(self, monkeypatch, *, platform, sandboxed, socket_path):
        monkeypatch.setattr(review_driver.sys, "platform", platform)
        if sandboxed:
            monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        else:
            monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.setattr(review_driver, "_existing_dashboard_socket", lambda port: socket_path)

    def test_linux_sandbox_without_a_socket_is_the_case(self, monkeypatch):
        self._setup(monkeypatch, platform="linux", sandboxed=True, socket_path="")
        assert review_driver._sandboxed_tcp_only(41234) is True

    def test_a_socket_present_is_not_the_case(self, monkeypatch):
        self._setup(monkeypatch, platform="linux", sandboxed=True, socket_path="/run/d.sock")
        assert review_driver._sandboxed_tcp_only(41234) is False

    def test_outside_the_sandbox_is_not_the_case(self, monkeypatch):
        self._setup(monkeypatch, platform="linux", sandboxed=False, socket_path="")
        assert review_driver._sandboxed_tcp_only(41234) is False

    def test_windows_without_a_socket_is_not_the_case(self, monkeypatch):
        self._setup(monkeypatch, platform="win32", sandboxed=True, socket_path="")
        assert review_driver._sandboxed_tcp_only(41234) is False


class TestSageProbeProvesThePeer:
    def test_probe_runs_the_proof_and_reports_a_gateway(self, monkeypatch):
        proof = _RecordingProof()
        _pin_gateway(monkeypatch, proof)
        assert review_driver._probe("http://127.0.0.1:41234", "s3cr3t") is True
        assert proof.calls[0][2] == "sage_probe"

    def test_probe_treats_an_unproven_peer_as_not_the_gateway(self, monkeypatch):
        proof = _RecordingProof(refuse=True)
        _pin_gateway(monkeypatch, proof)
        # The probe dials candidate ports; a refused peer is a foreign listener,
        # so the secret is not sent and the port reads as "not our gateway".
        assert review_driver._probe("http://127.0.0.1:41234", "s3cr3t") is False
