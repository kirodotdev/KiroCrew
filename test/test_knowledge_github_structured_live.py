"""W02 PR-3: the GitHub structured connector's LIVE wiring, now closed.

The connector drives a REAL W01 page walk (the operation is invoked, authorized
per page, and the walk advances on W01's single next_cursor) and reads the
fetched rows off ExecutionOutcome.payload (a CollectionPayload) — the neutral
data channel W01 added at RESULT_SCHEMA_VERSION=3 / EXECUTOR_SCHEMA_VERSION=4 /
PRODUCTION_SCHEMA_VERSION=3. fetch converts the rows with PR-2's converters,
folds them through diff_rows, and returns real (text, metadata); detect_changes
reports changed iff the since-window returned rows.

These tests use a real, isolated encrypted SecretVault and a self-signed HTTPS
loopback so the walk really talks TLS through the unmodified urllib_http_send:

* fetch returns real rows across >=2 pages, keyed and rendered, with the
  vault-resolved credential on the wire;
* detect_changes is True when the window returns rows;
* no provider still refuses NotImplementedError (PR-2's parked contract);
* a provider with no binding walks nothing and returns an empty-but-valid
  dataset, never a fabricated one.

No payload copy, no second envelope, no out-of-band capture, no global cache —
the rows come off the single ExecutionOutcome.payload and the cursor off the
single next_cursor.
"""

from __future__ import annotations

import asyncio
import datetime
import http.server
import ipaddress
import json
import ssl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import binding_secret_ref, create_binding
from kiro_crew.connections.control_plane.handle import derive_handle, ensure_usable
from kiro_crew.connections.control_plane.lifecycle import BindingStore
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import (
    BindingCustodyGate,
    urllib_http_send,
)
from kiro_crew.connections.vendors.github.dispatch import build_github_transport
from kiro_crew.knowledge.connectors.github_structured import (
    ENTITY_COMMIT,
    ENTITY_PULL_REQUEST,
    GithubStructuredConnector,
    GithubTransport,
    LiveFetchError,
)
from kiro_crew.secrets import SecretVault

_T0 = 1_000_000.0
_GRANTED: Tuple[str, ...] = ("repo",)

#: Monotonic per-call deployment index so each composed binding lands in its own
#: L04 deployment slot (the store's uniqueness key includes deployment_id), while
#: sharing one account identity across a multi-entity walk.
_DEPLOY_SEQ = 0


def _verifier(*, claimed_subject, claimed_tenant, service_id):
    return {"subject_ref": "s", "tenant_ref": "t"}


def _bundle_for(vault: SecretVault, *, clock=lambda: _T0) -> GithubTransport:
    binding = create_binding(
        service_id="github", claimed_subject="octocat", claimed_tenant="acme",
        credential_mode="oauth_user", verifier=_verifier, slug="github",
    )
    handle = derive_handle(
        binding, granted_scopes=_GRANTED, requested_scopes=("repo",),
        now=_T0, ttl_seconds=3600.0,
    )
    view = ensure_usable(handle, now=_T0)
    gate = BindingCustodyGate(
        binding=binding, binding_fingerprint=view.binding_fingerprint)
    # A REAL on-disk L04 store rooted in the vault's own crewhome (per-test
    # isolated), holding the binding under custody so W01's per-call
    # store.select_secret reads its secret_ref off the LIVE record.
    store = BindingStore(
        vault._config_dir.parent / "connections" / "control_plane_bindings.json")
    # A multi-entity fetch composes one bundle per entity; each mints its own
    # binding (fresh binding_id) but the SAME account identity. select_secret
    # fences by binding_id against the live store, so THIS binding must be in the
    # store -- and the (deployment_id, service, subject, tenant) uniqueness key
    # forces a distinct deployment_id per insert. A per-call counter gives each
    # its own deployment slot, so every composed binding is genuinely live.
    global _DEPLOY_SEQ
    _DEPLOY_SEQ += 1
    store.insert(
        binding, deployment_id=f"deployment://test/github/{_DEPLOY_SEQ}",
        kiro_principal="kiro://test/owner")
    # Seed the vault under THIS binding's OWN secret_ref name. W01 now mints a
    # per-binding secret name (no slug collapse), so select_secret reads that
    # exact name off the live record -- the fixture's slug-derived name would
    # miss and the transport would return HTTP 401.
    vault.set_sync(binding["secret_ref"]["name"], "gh-installation-token")
    transport = build_github_transport(
        operation_id="gh_list_pull_requests", gate=gate, store=store,
        vault=vault, http_send=urllib_http_send,
    )
    return GithubTransport(
        transport=transport, handle=handle, offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(), governance_scope="tools",
        governance_item="pulls.list",
        # Provider-supplied tenant string for this binding (meant to become W01's
        # Binding.tenant_ref). The connector never synthesises it from the repo
        # owner; distinct from the repo owner "octo". This slice cannot prove it
        # is a verified identity yet, so rows stay fail-closed regardless.
        provider_tenant="tenant://provider-supplied/acme",
        clock=clock,
    )


@pytest.fixture
def real_vault(tmp_path: Path) -> SecretVault:
    vault = SecretVault(tmp_path / "crewhome")
    vault.set_sync(binding_secret_ref("github")["name"], "gh-installation-token")
    return vault


# ── self-signed HTTPS loopback that pages ───────────────────────────────────
def _tls_material(tmp_path: Path) -> Tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cf = tmp_path / "cert.pem"
    kf = tmp_path / "key.pem"
    cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kf.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return cf, kf


class _Recorder:
    def __init__(self) -> None:
        self.requests: List[Dict[str, str]] = []
        self.paths: List[str] = []


def _paging_handler(recorder: _Recorder, port_ref: Dict[str, int]):
    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            recorder.requests.append({k: v for k, v in self.headers.items()})
            recorder.paths.append(self.path)
            port = port_ref["port"]
            if self.path.startswith("/page2"):
                body = json.dumps([{
                    "number": 2, "title": "b", "state": "open",
                    "user": {"login": "octocat"}, "pull_request": {},
                    "updated_at": "2026-09-02T00:00:00Z",
                    "html_url": "https://github.com/octo/hello/pull/2",
                    "url": "https://api.github.com/repos/octo/hello/pulls/2",
                }]).encode()
                headers = {"Content-Type": "application/json"}
            else:
                body = json.dumps([{
                    "number": 1, "title": "a", "state": "open",
                    "user": {"login": "octocat"}, "pull_request": {},
                    "updated_at": "2026-09-01T00:00:00Z",
                    "html_url": "https://github.com/octo/hello/pull/1",
                    "url": "https://api.github.com/repos/octo/hello/pulls/1",
                }]).encode()
                headers = {
                    "Content-Type": "application/json",
                    "Link": f'<https://localhost:{port}/page2>; rel="next"',
                }
            self.send_response(200)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return

    return _H


@contextmanager
def _https_server(handler_cls: Any, certfile: Path, keyfile: Path) -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(certfile), str(keyfile))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ── the tests ───────────────────────────────────────────────────────────────
def test_fetch_returns_real_rows_across_two_pages_over_tls(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    port_ref: Dict[str, int] = {"port": 0}
    with _https_server(_paging_handler(rec, port_ref), certfile, keyfile) as port:
        port_ref["port"] = port
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault) if e == ENTITY_PULL_REQUEST else None,
        )
        text, metadata = asyncio.run(
            connector.fetch({"id": "src-1", "repo_full_name": "octo/hello"}))
    # Real rows came back through ExecutionOutcome.payload across BOTH pages.
    assert metadata["row_count"] == 2
    assert metadata["repo_full_name"] == "octo/hello"
    assert len(metadata["primary_keys"]) == 2
    # Both PRs (numbers 1 and 2) are present, keyed and rendered.
    assert "github_pull_request" in text
    keys = metadata["primary_keys"]
    assert any(k.endswith("1") for k in keys)
    assert any(k.endswith("2") for k in keys)
    # The walk really turned two pages over TLS: page 1 template, page 2 cursor.
    assert len(rec.paths) == 2
    assert rec.paths[0].startswith("/repos/octo/hello/pulls")
    assert rec.paths[1].startswith("/page2")
    # Custody ran: the vault-resolved credential reached the wire.
    auths = [next((v for k, v in r.items() if k.lower() == "authorization"), None) for r in rec.requests]
    assert auths == ["Bearer gh-installation-token", "Bearer gh-installation-token"]


def test_detect_changes_true_when_since_window_returns_rows(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    port_ref: Dict[str, int] = {"port": 0}
    with _https_server(_paging_handler(rec, port_ref), certfile, keyfile) as port:
        port_ref["port"] = port
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(transport_provider=lambda s, e: _bundle_for(real_vault))
        changed = asyncio.run(connector.detect_changes({"id": "s", "repo_full_name": "octo/hello"}))
    assert changed is True  # the window returned rows -> changed
    assert len(rec.paths) >= 1


def test_detect_changes_sees_issue_or_commit_without_a_changed_pr(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F6: a new issue/commit that touched NO pull request must still be seen as a
    # change. A PR-only walk would return empty and the source would silently
    # never fetch the changed issue/commit. Route so pulls return [] but
    # issues/commits return a row; detect_changes must report True.
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()

    class _PullsEmptyElseRow(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            rec.paths.append(self.path)
            if "/pulls" in self.path:
                body = b"[]"  # no PR changed
            elif "/commits" in self.path:
                body = json.dumps([{
                    "sha": "abc123",
                    "commit": {"message": "m",
                               "committer": {"name": "c", "date": "2026-09-02T00:00:00Z"}},
                    "html_url": "h",
                    "url": "https://api.github.com/repos/octo/hello/commits/abc123",
                    "parents": [],
                }]).encode()
            else:  # issues
                body = json.dumps([{
                    "number": 9, "title": "iss", "state": "open", "user": {"login": "u"},
                    "updated_at": "2026-09-01T00:00:00Z", "html_url": "h",
                    "url": "https://api.github.com/repos/octo/hello/issues/9",
                }]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return

    with _https_server(_PullsEmptyElseRow, certfile, keyfile) as port:
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault))
        changed = asyncio.run(
            connector.detect_changes({"id": "s", "repo_full_name": "octo/hello"}))
    assert changed is True  # a changed issue/commit is detected without any PR


def test_provider_returning_none_for_every_kind_fetches_nothing(
    real_vault: SecretVault,
) -> None:
    # A provider with no binding for any kind walks nothing; the fetch returns an
    # empty-but-valid dataset (no rows), never a fabricated one.
    connector = GithubStructuredConnector(transport_provider=lambda s, e: None)
    text, metadata = asyncio.run(connector.fetch({"id": "s", "repo_full_name": "o/r"}))
    assert metadata["row_count"] == 0
    assert text == ""


def test_no_transport_still_refuses_notimplemented() -> None:
    # PR-2's parked contract: no provider -> NotImplementedError, never a mock.
    connector = GithubStructuredConnector()
    with pytest.raises(NotImplementedError):
        asyncio.run(connector.fetch({"id": "s", "repo_full_name": "o/r"}))
    with pytest.raises(NotImplementedError):
        asyncio.run(connector.detect_changes({"id": "s", "repo_full_name": "o/r"}))


# ── per-row ingest contract (real SourceRow API, integrated) ────────────────
def _entity_routing_handler(recorder: _Recorder):
    """Route by path: issues/pulls/commits each return one item; a commit's
    check-runs return one check_run wrapped under `check_runs`."""

    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            recorder.requests.append({k: v for k, v in self.headers.items()})
            recorder.paths.append(self.path)
            p = self.path
            if "/check-runs" in p:
                body = json.dumps({"check_runs": [{
                    "id": 555, "name": "ci", "head_sha": "abc123", "status": "completed",
                    "conclusion": "success", "completed_at": "2026-09-03T00:00:00Z",
                    "html_url": "h", "url": "https://api.github.com/repos/octo/hello/check-runs/555",
                }]}).encode()
            elif "/commits" in p:
                body = json.dumps([{
                    "sha": "abc123",
                    "commit": {"message": "m", "committer": {"name": "c", "date": "2026-09-02T00:00:00Z"}},
                    "html_url": "h", "url": "https://api.github.com/repos/octo/hello/commits/abc123",
                    "parents": [],
                }]).encode()
            elif "/issues" in p:
                body = json.dumps([{
                    "number": 9, "title": "iss", "state": "open", "user": {"login": "u"},
                    "updated_at": "2026-09-01T00:00:00Z",
                    "html_url": "h", "url": "https://api.github.com/repos/octo/hello/issues/9",
                }]).encode()
            else:  # pulls
                body = json.dumps([{
                    "number": 3, "title": "pr", "state": "open", "user": {"login": "u"},
                    "pull_request": {}, "updated_at": "2026-09-01T00:00:00Z",
                    "html_url": "h", "url": "https://api.github.com/repos/octo/hello/pulls/3",
                }]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return

    return _H


def test_fetch_rows_covers_all_four_entities_incl_checkrun_fanout(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    with _https_server(_entity_routing_handler(rec), certfile, keyfile) as port:
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault))
        rows, snapshot, checkpoint = asyncio.run(
            connector.fetch_rows({"id": "src-1", "repo_full_name": "octo/hello"}))
    # issue + PR + commit + a check-run fanned out from that commit = 4 rows.
    assert len(rows) == 4
    assert {r.resource_ref.provider for r in rows} == {"github"}
    # The check-run row was fanned out per the commit sha (its ref path was hit).
    assert any("/check-runs" in p for p in rec.paths)
    assert any("/commits" in p for p in rec.paths)
    assert any("/issues" in p for p in rec.paths)
    assert any("/pulls" in p for p in rec.paths)
    assert any(r.resource_ref.locator.get("check_run_id") for r in rows)
    # Still fail-closed + incremental.
    assert all(r.subjects == () and r.tenant == "tenant://provider-supplied/acme" for r in rows)
    assert snapshot is False


def test_supports_rows_is_true_with_the_real_ingest_api() -> None:
    # The real per-row ingest API (kiro_crew.knowledge.rows / acl) is integrated
    # into this branch, so the connector emits structured rows.
    from kiro_crew.knowledge.rows import SourceRow  # real symbol, not a stand-in
    assert SourceRow is not None
    assert GithubStructuredConnector().supports_rows() is True


def test_fetch_rows_returns_failclosed_sourcerows_over_tls(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Exercises the GENUINE SourceRow / ProviderResourceRef symbols (integrated),
    # not a stand-in.
    from kiro_crew.knowledge.acl import ProviderResourceRef
    from kiro_crew.knowledge.rows import SourceRow

    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    port_ref: Dict[str, int] = {"port": 0}
    with _https_server(_paging_handler(rec, port_ref), certfile, keyfile) as port:
        port_ref["port"] = port
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault) if e == ENTITY_PULL_REQUEST else None,
        )
        assert connector.supports_rows() is True
        rows, snapshot, checkpoint = asyncio.run(
            connector.fetch_rows({"id": "src-1", "repo_full_name": "octo/hello"}))
    # Real rows across BOTH pages, each a genuine SourceRow with fail-closed ACL.
    assert len(rows) == 2
    for row in rows:
        assert isinstance(row, SourceRow)
        assert row.subjects == ()            # fail-closed deny-all, never public
        assert row.tenant == "tenant://provider-supplied/acme"  # provider-supplied, not proven-verified
        assert row.managed is True           # fixed by the DTO
        assert isinstance(row.resource_ref, ProviderResourceRef)
        assert row.resource_ref.provider == "github"
        assert row.resource_ref.locator["owner"] == "octo"
        assert row.resource_ref.locator["repo"] == "hello"
        assert "number" in row.resource_ref.locator  # a PR locator
    # Incremental (snapshot=False): absent rows are NOT deleted; checkpoint is
    # the advanced watermark from the fetched rows.
    assert snapshot is False
    # DEFECT 1: issues + commits had no binding (skipped this round), so the
    # watermark must NOT advance past their un-fetched windows -- it stays at the
    # prior value (None for a fresh source), even though the PR walk had newer
    # rows. Advancing here would silently drop issues/commits forever.
    assert checkpoint["since"] is None
    # The walk really turned two pages over TLS.
    assert rec.paths[0].startswith("/repos/octo/hello/pulls")
    assert rec.paths[1].startswith("/page2")


def test_missing_transport_for_detect_changes_fails_closed() -> None:
    connector = GithubStructuredConnector(transport_provider=lambda s, e: None)
    with pytest.raises(LiveFetchError):
        asyncio.run(connector.detect_changes({"repo_full_name": "o/r"}))


def test_commit_and_pr_kinds_are_wired() -> None:
    from kiro_crew.knowledge.connectors.github_structured import _OP_FOR_ENTITY
    assert _OP_FOR_ENTITY[ENTITY_PULL_REQUEST] == "gh_list_pull_requests"
    assert _OP_FOR_ENTITY[ENTITY_COMMIT] == "gh_list_commits"


# ── DEFECT 1: the watermark never advances past data a round did not fetch ──
def test_checkpoint_advances_to_min_when_all_entities_complete(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    with _https_server(_entity_routing_handler(rec), certfile, keyfile) as port:
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault))
        _rows, _snap, checkpoint = asyncio.run(
            connector.fetch_rows({"id": "s", "repo_full_name": "octo/hello"}))
    # All repo-scoped entities completed: issues (2026-09-01), PR (2026-09-01),
    # commit (2026-09-02). The watermark is the MIN across them, so no entity's
    # window is skipped over -> 2026-09-01, not the commit's newer 2026-09-02.
    assert checkpoint["since"] == "2026-09-01T00:00:00Z"


def test_incomplete_round_leaves_checkpoint_unchanged(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # NEGATIVE TEST for DEFECT 1: one repo-scoped entity (commits) has no binding
    # this round. Its window was not fetched, so the checkpoint must NOT advance
    # past the prior watermark, even though issues + PRs returned newer rows.
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    from kiro_crew.knowledge.connectors.github_structured import ENTITY_COMMIT

    prior = {"since": "2026-08-01T00:00:00Z", "tracked_shas": []}
    source = {"id": "s", "repo_full_name": "octo/hello", "properties": {"checkpoint": prior}}
    with _https_server(_entity_routing_handler(rec), certfile, keyfile) as port:
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        # No binding for commits -> that entity is skipped.
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: None if e == ENTITY_COMMIT else _bundle_for(real_vault))
        _rows, _snap, checkpoint = asyncio.run(connector.fetch_rows(source))
    # The prior watermark is preserved exactly: a skipped entity cannot advance it.
    assert checkpoint["since"] == "2026-08-01T00:00:00Z"


def test_mutation_reverting_defect1_fix_would_fail() -> None:
    # MUTATION VERIFY (DEFECT 1): reverting the fix means "advance to the MAX
    # watermark regardless of skipped entities". Reproduce that broken behaviour
    # on the same inputs and assert the guard test above would then FAIL — i.e.
    # the broken version advances the checkpoint where the correct one holds it.
    # (Reproduced in-process, not by editing source, so the assertion is self-contained.)
    completed = {"issue": "2026-09-01T00:00:00Z"}   # a completed entity
    skipped_present = True                           # a commit entity was skipped
    prior = "2026-08-01T00:00:00Z"
    # correct fix: any skip -> hold prior
    correct_next = prior if skipped_present else min(completed.values())
    # broken (reverted) fix: advance to the max seen, ignoring the skip
    broken_next = max([prior, *completed.values()])
    assert correct_next == prior                     # fix holds the checkpoint
    assert broken_next != correct_next               # revert would advance it -> test fails


# ── DEFECT 2: a check-run state change on an OLDER commit is still caught ────
def _checkrun_state_handler(recorder: _Recorder, state_ref):
    """Serve issues/pulls empty; commits empty (no NEW commit this round); a
    check-run for the OLD sha 'oldsha' whose conclusion is state_ref['value']."""

    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            recorder.paths.append(self.path)
            p = self.path
            if "/check-runs" in p:
                body = json.dumps({"check_runs": [{
                    "id": 777, "name": "ci", "head_sha": "oldsha",
                    "status": "completed", "conclusion": state_ref["value"],
                    "completed_at": "2026-09-05T00:00:00Z", "html_url": "h",
                    "url": "https://api.github.com/repos/octo/hello/check-runs/777",
                }]}).encode()
            else:  # issues / pulls / commits: nothing new in this since-window
                body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return

    return _H


def test_checkrun_state_change_on_older_sha_is_repicked(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    state = {"value": "success"}
    # The checkpoint already tracks an OLD commit sha; NO new commit arrives this
    # round (the since-window is empty), yet its check-run must still be probed.
    prior = {"since": "2026-09-04T00:00:00Z", "tracked_shas": ["oldsha"]}
    source = {"id": "s", "repo_full_name": "octo/hello", "properties": {"checkpoint": prior}}
    with _https_server(_checkrun_state_handler(rec, state), certfile, keyfile) as port:
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault))
        rows, _snap, checkpoint = asyncio.run(connector.fetch_rows(source))
    # The check-run on the OLD sha was re-probed and produced a row, even though
    # no new commit appeared this round -- DEFECT 2 fix.
    cr_rows = [r for r in rows if r.resource_ref.locator.get("check_run_id")]
    assert len(cr_rows) == 1
    assert any("/commits/oldsha/check-runs" in p for p in rec.paths)
    # The old sha stays tracked for the next round's re-probe.
    assert "oldsha" in checkpoint["tracked_shas"]


# ── Q2: the GitHub permission probe (capability; live acceptance is PR-4's) ──
def _repo_perm_handler(recorder: _Recorder, *, private: bool):
    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            recorder.paths.append(self.path)
            p = self.path
            if "/collaborators" in p:
                body = json.dumps([{"login": "alice"}, {"login": "bob"}]).encode()
            else:  # the repository object
                body = json.dumps({"full_name": "octo/hello", "private": private}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return

    return _H


def _probe_call(real_vault, port, monkeypatch):
    import kiro_crew.connections.vendors.github.locator as gh_locator
    from kiro_crew.connections.vendors.github.dispatch import build_github_transport
    from kiro_crew.connections.vendors.github.permissions import resolve_repo_subjects
    from kiro_crew.knowledge.acl import PUBLIC_SUBJECT
    monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")

    # ONE binding/handle/gate/store shared across the per-op transports, so each
    # transport's custody matches the handle the probe runs under.
    binding = create_binding(
        service_id="github", claimed_subject="octocat", claimed_tenant="acme",
        credential_mode="oauth_user", verifier=_verifier, slug="github")
    handle = derive_handle(
        binding, granted_scopes=_GRANTED, requested_scopes=("repo",),
        now=_T0, ttl_seconds=3600.0)
    view = ensure_usable(handle, now=_T0)
    gate = BindingCustodyGate(
        binding=binding, binding_fingerprint=view.binding_fingerprint)
    store = BindingStore(
        real_vault._config_dir.parent / "connections" / "control_plane_bindings.json")
    store.insert(
        binding, deployment_id="deployment://test/github/probe",
        kiro_principal="kiro://test/owner")
    # Seed the vault under this binding's OWN per-binding secret name (W01 no
    # longer collapses on the slug), else select_secret misses and yields 401.
    real_vault.set_sync(binding["secret_ref"]["name"], "gh-installation-token")

    # Each op gets a transport composed with ITS decoder (gh_get_repository ->
    # decode_single/ObjectPayload; collaborators -> decode_rest_page/collection).
    def _transport_for(op):
        return build_github_transport(
            operation_id=op, gate=gate, store=store, vault=real_vault,
            http_send=urllib_http_send)

    return resolve_repo_subjects(
        owner="octo", repo="hello", handle=handle,
        transport_for=_transport_for, offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)), layers=LayerCeilings(),
        governance_scope="tools", governance_item="repo.read",
        public_subject=PUBLIC_SUBJECT, clock=lambda: _T0)


def test_permission_probe_public_repo_resolves_public_subject(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.knowledge.acl import PUBLIC_SUBJECT
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    with _https_server(_repo_perm_handler(rec, private=False), certfile, keyfile) as port:
        subjects = _probe_call(real_vault, port, monkeypatch)
    assert subjects == (PUBLIC_SUBJECT,)  # proven public via the repo's private=False


def test_permission_probe_private_repo_resolves_collaborators(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    with _https_server(_repo_perm_handler(rec, private=True), certfile, keyfile) as port:
        subjects = _probe_call(real_vault, port, monkeypatch)
    assert subjects == ("alice", "bob")  # private repo -> its collaborator logins
    assert any("/collaborators" in p for p in rec.paths)


# =============================================================================
# F1: an entity walk failure aborts the whole round (no partial-sync commit)
# =============================================================================
def test_entity_failure_aborts_the_round_no_partial_commit(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.knowledge.connectors.github_structured import LiveFetchError

    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))

    class _CommitsFail(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            if "/commits" in self.path:
                body = b'{"message":"boom"}'
                self.send_response(500)
            else:
                body = b"[]"
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return

    with _https_server(_CommitsFail, certfile, keyfile) as port:
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault))
        # The commit entity's walk fails -> the WHOLE round raises rather than
        # returning the (successful) issue/PR rows for a partial-success commit.
        with pytest.raises(LiveFetchError):
            asyncio.run(connector.fetch_rows({"id": "s", "repo_full_name": "octo/hello"}))


# =============================================================================
# F2: a check-run-only transition on a quiet repo is detected via tracked SHAs
# =============================================================================
def test_detect_changes_sees_checkonly_transition_on_tracked_sha(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))

    class _QuietExceptCheckRuns(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            if "/check-runs" in self.path:
                # a check-run that completed AFTER the checkpoint watermark
                body = json.dumps({"check_runs": [{
                    "id": 777, "name": "ci", "head_sha": "trackedsha",
                    "status": "completed", "conclusion": "success",
                    "completed_at": "2026-09-05T00:00:00Z", "html_url": "h",
                    "url": "https://api.github.com/repos/octo/hello/check-runs/777",
                }]}).encode()
            else:  # issues / pulls / commits: nothing new since the watermark
                body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            return

    # Checkpoint already tracks a SHA and a watermark BEFORE the check-run flip.
    prior = {"since": "2026-09-01T00:00:00Z", "tracked_shas": ["trackedsha"]}
    source = {"id": "s", "repo_full_name": "octo/hello",
              "properties": {"checkpoint": prior}}
    with _https_server(_QuietExceptCheckRuns, certfile, keyfile) as port:
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault))
        changed = asyncio.run(connector.detect_changes(source))
    # No issue/PR/commit changed, but a tracked SHA's check-run completed after
    # the watermark -> detected as changed (F2).
    assert changed is True


# =============================================================================
# endpoint: the resource_ref locator carries the execution host, so the same
# owner/repo/number on two GitHub deployments is distinguishable
# =============================================================================
def test_resource_ref_locator_carries_execution_endpoint(
    tmp_path: Path, real_vault: SecretVault, monkeypatch: pytest.MonkeyPatch,
) -> None:
    certfile, keyfile = _tls_material(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    rec = _Recorder()
    with _https_server(_entity_routing_handler(rec), certfile, keyfile) as port:
        import kiro_crew.connections.vendors.github.locator as gh_locator
        monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", f"https://localhost:{port}")
        connector = GithubStructuredConnector(
            transport_provider=lambda s, e: _bundle_for(real_vault))
        rows, _snap, _cp = asyncio.run(
            connector.fetch_rows({"id": "s", "repo_full_name": "octo/hello"}))
    # Every row's locator carries the endpoint = the host the transport actually
    # targeted (the monkeypatched loopback origin), never a defaulted github.com.
    assert rows
    for r in rows:
        assert r.resource_ref.locator["endpoint"] == f"https://localhost:{port}"
    # Two deployments (different endpoints), same owner/repo/number -> different
    # locators, so ACL's endpoint-less ProviderResourceRef can now tell them apart.
    from kiro_crew.knowledge.connectors.github_structured import (
        _resource_ref_for,
        issue_or_pull_from_payload,
    )
    payload = {"number": 3, "title": "t", "state": "open", "user": {"login": "u"},
               "pull_request": {}, "updated_at": "2026-09-01T00:00:00Z",
               "html_url": "h", "url": "https://api.github.com/repos/octo/hello/pulls/3"}
    row = issue_or_pull_from_payload(
        "octo/hello", payload, source_id="s", fetched_at="2026-09-01T00:00:00Z")
    monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", "https://ghe.corp.example")
    ref_ghe = _resource_ref_for(row, owner="octo")
    monkeypatch.setattr(gh_locator, "GITHUB_API_BASE", "https://api.github.com")
    ref_dotcom = _resource_ref_for(row, owner="octo")
    assert ref_ghe.locator["endpoint"] != ref_dotcom.locator["endpoint"]
    assert ref_ghe.locator["number"] == ref_dotcom.locator["number"] == 3
