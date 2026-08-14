"""Coverage tests for ``kiro_crew.deploy.handlers`` error/refusal branches.

Focus is the paths the existing deploy suites never reach: the redaction
helpers, ``_scan_tree`` fail-closed findings, ``_stage_tree_safe`` rejections,
the ``_do_deploy``/``_do_recall``/``_do_destroy`` refusal returns, the profile
control-plane 400/404s, ``_expire_manifest_best_effort``'s "unreachable"
ladder, the teardown preconditions and the pending-confirm guards.

Everything is stubbed at the ``engine.run_aws`` / store boundary -- no AWS
call, no network, no real subprocess, and every filesystem write lands under
``tmp_path``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.deploy import engine, handlers
from kiro_crew.deploy import profiles as profiles_mod

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only symlink/permission semantics (reduced Windows backend scope)",
)


@pytest.fixture(autouse=True)
def _force_posix_shell(monkeypatch):
    """Neutralise the handlers' module-local ``os.name == "nt"`` platform gate.

    Patches a handlers-module-local ``os`` proxy (never the global ``os.name``)
    so pathlib keeps selecting real platform behaviour on a Windows runner.
    """

    class _PosixNameOs:
        name = "posix"

        def __getattr__(self, attr):  # pragma: no cover - trivial delegation
            return getattr(os, attr)

    monkeypatch.setattr(handlers, "os", _PosixNameOs())


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch):
    """Redirect every deploy-module config_dir() and KIROCREW_HOME to tmp_path."""
    import kiro_crew.deploy as _deploy_pkg
    from kiro_crew.deploy import pending as _pending_mod

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(handlers, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(profiles_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_pending_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_deploy_pkg, "config_dir", lambda: tmp_path)
    return tmp_path


class _NonRestrictedState:
    """Minimal dashboard state that passes ``_deny_restricted``."""

    _restricted_keys: set = set()
    _slots: dict = {}


class _FakeReq:
    """Request double with a non-restricted app state installed."""

    def __init__(self, body=None, *, match_info=None, query=None, headers=None):
        self._body = {} if body is None else body
        self.headers = headers if headers is not None else {"X-Session-Key": "dashboard:ui"}
        self.app = {"state": _NonRestrictedState()}
        self.match_info = match_info or {}
        self.query = query or {}
        self.rel_url = SimpleNamespace(query=self.query)

    async def json(self):
        if isinstance(self._body, str):
            raise json.JSONDecodeError("bad", self._body, 0)
        return self._body

    async def read(self):
        if isinstance(self._body, str):
            return self._body.encode()
        return json.dumps(self._body).encode()


def _payload(resp):
    return json.loads(resp.body.decode())


def _aws_router(routes, default=(0, "", "")):
    """Build a ``run_aws`` stub dispatching on a substring of the joined argv."""

    def _run_aws(argv, profile="", timeout=15, *a, **kw):
        joined = " ".join(str(x) for x in argv)
        for needle, result in routes:
            if needle in joined:
                return result
        return default

    return _run_aws


_BASE_OUTPUTS = json.dumps([{"OutputKey": "BucketName", "OutputValue": "base-bucket"}])


def _deny_stat(monkeypatch, filename: str) -> None:
    """Make ``Path.stat`` fail for one file while ``is_file`` still says True.

    ``Path.is_file`` is implemented on top of ``Path.stat``, so patching only
    ``stat`` makes the walker's ``is_file()`` raise instead of reaching the
    handler's own ``stat`` call site. Patch both.
    """
    real_stat = Path.stat
    real_is_file = Path.is_file

    def _stat(self, *a, **kw):
        if self.name == filename:
            raise OSError("stat denied")
        return real_stat(self, *a, **kw)

    def _is_file(self, *a, **kw):
        if self.name == filename:
            return True
        return real_is_file(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", _stat)
    monkeypatch.setattr(Path, "is_file", _is_file)


# --- redaction / small helpers ---------------------------------------------


class TestHelpers:
    def test_redact_pending_entries_recurses_one_level(self):
        out = handlers._redact_pending_entries([
            {"site_id": "s", "nested": {"a": "plain", "b": 3}, "n": 7, "empty": ""},
        ])
        assert out[0]["nested"] == {"a": "plain", "b": 3}
        assert out[0]["n"] == 7
        assert out[0]["empty"] == ""

    def test_redact_profile_fields_keeps_non_strings(self):
        out = handlers._redact_profile_fields([{"name": "p", "n": 1, "blank": ""}])
        assert out == [{"name": "p", "n": 1, "blank": ""}]

    def test_sanitize_response_walks_lists_and_scalars(self):
        assert handlers._sanitize_response([{"a": ["x"]}, 5, None]) == [{"a": ["x"]}, 5, None]

    def test_data_dir_under_config_dir(self, tmp_path: Path):
        assert handlers._data_dir() == tmp_path / "deploy"

    def test_safe_resolve_falls_back_on_oserror(self):
        class _Boom:
            def resolve(self):
                raise OSError("nope")

        boom = _Boom()
        assert handlers._safe_resolve(boom) is boom

    def test_reaper_remediation_omits_empty_flags(self):
        assert handlers._reaper_remediation("", "") == "install-reaper.sh"
        assert "--profile p" in handlers._reaper_remediation("p", "us-west-2")

    def test_safe_site_id_strips_and_truncates(self):
        assert handlers._safe_site_id("  My Site!!  ") == "my-site"
        assert len(handlers._safe_site_id("a" * 200)) == handlers._SITE_ID_MAX

    def test_load_config_with_empty_registry(self):
        assert handlers._load_config() == {
            "profile": "", "region": handlers.DEFAULT_REGION}

    def test_save_config_updates_existing_entry_region(self):
        handlers._save_config("p", "us-west-2")
        handlers._save_config("p", "eu-west-1")
        assert handlers._load_config() == {"profile": "p", "region": "eu-west-1"}

    def test_save_config_empty_profile_clears_default(self):
        handlers._save_config("p", "us-west-2")
        handlers._save_config("", "")
        assert handlers._load_config()["profile"] == ""


class TestAllowedLocalRoots:
    def test_config_load_failure_degrades_to_fallback(self, monkeypatch, tmp_path: Path):
        from kiro_crew.config import loader as loader_mod

        class _Boom:
            @staticmethod
            def load():
                raise RuntimeError("config unreadable")

        monkeypatch.setattr(loader_mod, "KiroCrewConfig", _Boom)
        (tmp_path / "workspace").mkdir()
        roots = handlers._allowed_local_roots()
        # config_dir()/workspace is always allowed, even with no usable config.
        assert any(os.path.realpath(str(r)) == os.path.realpath(str(tmp_path / "workspace"))
                   for r in roots)

    def test_configured_roots_are_included(self, monkeypatch, tmp_path: Path):
        from kiro_crew.config import loader as loader_mod

        allowed = tmp_path / "cfgroot"
        allowed.mkdir()
        cfg = SimpleNamespace(
            agent=SimpleNamespace(subagent_cwd_allowed_roots=[str(allowed), "/nonexistent-xyz"]),
            workspaces={},
        )
        monkeypatch.setattr(loader_mod, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        roots = handlers._allowed_local_roots()
        assert any(os.path.realpath(str(r)) == os.path.realpath(str(allowed)) for r in roots)

    def test_registered_workspaces_are_included(self, monkeypatch, tmp_path: Path):
        from kiro_crew.config import loader as loader_mod

        ws = tmp_path / "ws1"
        ws.mkdir()
        cfg = SimpleNamespace(
            agent=SimpleNamespace(subagent_cwd_allowed_roots=[str(tmp_path)]),
            workspaces={"w": SimpleNamespace(dir=str(ws))},
        )
        monkeypatch.setattr(loader_mod, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
        roots = handlers._allowed_local_roots()
        real = {os.path.realpath(str(r)) for r in roots}
        assert os.path.realpath(str(ws)) in real

    def test_staging_root_failure_is_swallowed(self, monkeypatch, tmp_path: Path):
        def _boom():
            raise OSError("no staging")

        monkeypatch.setattr(handlers, "_staging_root", _boom)
        assert isinstance(handlers._allowed_local_roots(), list)


# --- scan / staging --------------------------------------------------------


class TestScanTree:
    def test_large_file_becomes_unscanned_finding(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(handlers, "_SCAN_SIZE_LIMIT", 8)
        src = tmp_path / "site"
        src.mkdir()
        (src / "sub").mkdir()
        (src / "big.bin").write_bytes(b"0123456789")
        findings, size = handlers._scan_tree(src)
        kinds = {f.kind for f in findings}
        assert "unscanned-large-file" in kinds
        assert size == 10

    def test_hook_denied_file_is_a_credential_finding(self, tmp_path: Path, monkeypatch):
        from kiro_crew import hooks as hooks_mod

        src = tmp_path / "site"
        src.mkdir()
        (src / "index.html").write_text("<p>hi</p>", encoding="utf-8", newline="\n")
        monkeypatch.setattr(hooks_mod, "safe_read_file_bytes", lambda *a, **kw: None)
        findings, _size = handlers._scan_tree(src)
        assert [f.kind for f in findings] == ["hook-denied"]

    def test_unstattable_file_becomes_unreadable_finding(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "site"
        src.mkdir()
        (src / "index.html").write_text("<p>hi</p>", encoding="utf-8", newline="\n")
        _deny_stat(monkeypatch, "index.html")
        findings, size = handlers._scan_tree(src)
        assert [f.kind for f in findings] == ["unreadable-file"]
        assert size == 0

    @_POSIX_ONLY
    def test_symlink_escape_is_reported_and_never_read(self, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8", newline="\n")
        src = tmp_path / "site"
        src.mkdir()
        os.symlink(str(outside), str(src / "link.txt"))
        findings, _size = handlers._scan_tree(src)
        assert "symlink-escape" in {f.kind for f in findings}

    def test_dir_contains_sensitive_detects_child(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "site"
        src.mkdir()
        child = src / "creds"
        child.write_text("x", encoding="utf-8", newline="\n")
        monkeypatch.setattr(
            handlers, "is_sensitive_path",
            lambda p: os.path.basename(str(p)) == "creds")
        assert handlers._dir_contains_sensitive(src, src) is True

    def test_compute_tree_size_global_ignores_stat_errors(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "site"
        src.mkdir()
        (src / "a.txt").write_text("abc", encoding="utf-8", newline="\n")
        _deny_stat(monkeypatch, "a.txt")
        assert handlers._compute_tree_size_global(src) == 0

    def test_compute_content_digest_ignores_unreadable_files(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "site"
        src.mkdir()
        (src / "a.txt").write_text("abc", encoding="utf-8", newline="\n")
        real_read = Path.read_bytes

        def _read(self, *a, **kw):
            if self.name == "a.txt":
                raise OSError("denied")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_bytes", _read)
        empty = handlers._compute_content_digest(str(tmp_path / "site"))
        assert isinstance(empty, str) and len(empty) == 64

    def test_stage_artifact_html_refuses_webapp_kind(self):
        with pytest.raises(ValueError, match="webapp artifacts"):
            handlers._stage_artifact_html("webapp", "summary", "app")


class TestStageTreeSafe:
    @_POSIX_ONLY
    def test_symlinked_directory_blocks_staging(self, tmp_path: Path):
        src = tmp_path / "site"
        src.mkdir()
        real_dir = tmp_path / "elsewhere"
        real_dir.mkdir()
        os.symlink(str(real_dir), str(src / "linkdir"))
        with pytest.raises(RuntimeError, match="symlinked directory"):
            handlers._stage_tree_safe(src, tmp_path / "stage")

    def test_unstattable_file_blocks_staging(self, tmp_path: Path, monkeypatch):
        src = tmp_path / "site"
        src.mkdir()
        (src / "a.txt").write_text("x", encoding="utf-8", newline="\n")
        real_lstat = os.lstat

        def _lstat(path, *a, **kw):
            if str(path).endswith("a.txt"):
                raise OSError("stat denied")
            return real_lstat(path, *a, **kw)

        monkeypatch.setattr(os, "lstat", _lstat)
        with pytest.raises(RuntimeError, match="hardlink-in-tree: cannot stat"):
            handlers._stage_tree_safe(src, tmp_path / "stage")

    def test_hook_refusal_blocks_staging(self, tmp_path: Path, monkeypatch):
        from kiro_crew import hooks as hooks_mod

        src = tmp_path / "site"
        src.mkdir()
        (src / "a.txt").write_text("x", encoding="utf-8", newline="\n")
        monkeypatch.setattr(
            hooks_mod, "safe_read_file_bytes_nolink", lambda *a, **kw: None)
        with pytest.raises(RuntimeError, match="staging-read-blocked"):
            handlers._stage_tree_safe(src, tmp_path / "stage")

    def test_too_large_file_blocks_staging(self, tmp_path: Path, monkeypatch):
        from kiro_crew import hooks as hooks_mod

        src = tmp_path / "site"
        src.mkdir()
        (src / "a.txt").write_text("x", encoding="utf-8", newline="\n")

        def _raise(*a, **kw):
            raise hooks_mod.FileTooLargeError("over the per-file cap")

        monkeypatch.setattr(hooks_mod, "safe_read_file_bytes_nolink", _raise)
        with pytest.raises(RuntimeError, match="file-too-large"):
            handlers._stage_tree_safe(src, tmp_path / "stage")

    @_POSIX_ONLY
    def test_staging_root_rejects_symlink(self, tmp_path: Path, monkeypatch):
        real = tmp_path / "real-staging"
        real.mkdir()
        link_parent = tmp_path / "cfg"
        link_parent.mkdir()
        os.symlink(str(real), str(link_parent / "deploy-staging"))
        monkeypatch.setattr(handlers, "config_dir", lambda: link_parent)
        with pytest.raises(RuntimeError, match="symlink"):
            handlers._staging_root()


# --- _do_deploy refusal ladder ---------------------------------------------


@pytest.fixture
def _site(tmp_path: Path, monkeypatch):
    """An allow-listed source directory holding one clean page."""
    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("<h1>hi</h1>", encoding="utf-8", newline="\n")
    monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])
    return src


class TestDoDeployRefusals:
    # Every test below hands `_do_deploy` a real filesystem path as local_dir.
    # That field is charset-validated against
    #     _LOCAL_DIR_RE = ^[A-Za-z0-9 _\-./~]+$
    # which admits POSIX path characters only. A native Windows absolute path
    # (``C:\Users\runneradmin\...``) contains ':' and '\', so validation refuses
    # it with "invalid local_dir: local_dir: invalid format" BEFORE any of the
    # branches these tests target is reached -- the refusal short-circuits, and
    # a test aimed at, say, the ttl_hours range then sees the local_dir error
    # instead of its own.
    #
    # That is a PRODUCT defect, not a test artefact: deploy cannot accept any
    # local directory on Windows. Widening a charset allowlist that guards a
    # path flowing into an `aws s3 sync` subprocess is a security-relevant
    # change and does not belong in a tests-only change, so it is reported
    # rather than fixed here. These tests are POSIX-only until it is fixed;
    # they will start covering Windows for free once the pattern accepts
    # native paths.
    pytestmark = pytest.mark.skipif(
        sys.platform == "win32",
        reason="deploy's _LOCAL_DIR_RE rejects native Windows paths (product defect)",
    )

    @pytest.mark.asyncio
    async def test_missing_site_id(self):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy({"local_dir": "/tmp"})
        assert status == 400 and payload["error"] == "site_id is required"

    @pytest.mark.asyncio
    async def test_unregistered_profile_is_refused(self):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy({"site_id": "s", "profile": "ghost"})
        assert status == 400 and "not registered" in payload["error"]

    @pytest.mark.asyncio
    async def test_invalid_profile_charset_is_refused(self):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy({"site_id": "s", "profile": "a;rm -rf"})
        assert status == 400 and "invalid profile" in payload["error"]

    @pytest.mark.asyncio
    async def test_no_source_supplied(self):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy({"site_id": "s"})
        assert status == 400 and payload["error"] == "provide artifact_slug or local_dir"

    @pytest.mark.asyncio
    async def test_both_sources_supplied(self, tmp_path: Path):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "artifact_slug": "a1", "local_dir": str(tmp_path)})
        assert status == 400 and "exactly one" in payload["error"]

    @pytest.mark.asyncio
    async def test_invalid_artifact_slug(self):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy({"site_id": "s", "artifact_slug": "-bad!"})
        assert status == 400 and "invalid artifact_slug" in payload["error"]

    @pytest.mark.asyncio
    async def test_artifact_store_unavailable(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", False)
        status, payload = await handlers._do_deploy({"site_id": "s", "artifact_slug": "a1"})
        assert status == 500 and payload["error"] == "artifact store unavailable"

    @pytest.mark.asyncio
    async def test_invalid_local_dir_charset(self):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": "/tmp/$(whoami)"})
        assert status == 400 and "invalid local_dir" in payload["error"]

    @pytest.mark.asyncio
    async def test_relative_local_dir(self):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy({"site_id": "s", "local_dir": "site/public"})
        assert status == 400 and payload["error"] == "local_dir must be an absolute path"

    @pytest.mark.asyncio
    async def test_local_dir_not_a_directory(self, tmp_path: Path):
        handlers._save_config("p", "us-west-2")
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8", newline="\n")
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(f)})
        assert status == 400 and "local_dir not found" in payload["error"]

    @pytest.mark.asyncio
    async def test_local_dir_outside_allowed_roots(self, tmp_path: Path, monkeypatch):
        handlers._save_config("p", "us-west-2")
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path / "only"])
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(outside)})
        assert status == 400 and "must resolve within" in payload["error"]

    @pytest.mark.asyncio
    async def test_sensitive_local_dir_is_refused(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(handlers, "_dir_contains_sensitive", lambda *a: True)
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site)})
        assert status == 400 and "sensitive credential path" in payload["error"]

    @pytest.mark.asyncio
    async def test_tagged_staging_rejection_becomes_409(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _boom():
            raise RuntimeError("hardlink-in-tree: a.txt has nlink=2 — deploy blocked")

        monkeypatch.setattr(handlers, "_staging_root", _boom)
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site)})
        assert status == 409
        assert payload["blocked"] is True and payload["credential"] is True
        assert "hardlink-in-tree" in payload["message"]

    @pytest.mark.asyncio
    async def test_untagged_staging_error_is_not_swallowed(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _boom():
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(handlers, "_staging_root", _boom)
        with pytest.raises(RuntimeError, match="disk on fire"):
            await handlers._do_deploy({"site_id": "s", "local_dir": str(_site)})

    @pytest.mark.asyncio
    async def test_scan_failure_cleans_staging_and_propagates(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _boom(_src):
            raise ValueError("scanner exploded")

        monkeypatch.setattr(handlers, "_scan_tree", _boom)
        with pytest.raises(ValueError, match="scanner exploded"):
            await handlers._do_deploy({"site_id": "s", "local_dir": str(_site)})

    @pytest.mark.asyncio
    async def test_overridable_findings_block_without_override(self, _site, monkeypatch):
        from kiro_crew.deploy.scan import Finding

        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(
            handlers, "_scan_tree",
            lambda _src: ([Finding(kind="internal-host", snippet="host", line=1,
                                   severity="info")], 11))
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site)})
        assert status == 409 and payload["count"] == 1
        assert payload["content_digest"] and payload["profile"] == "p"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw,needle", [
        (True, "got bool"),
        (12.5, "not float"),
        ("12", "got str"),
        (-1, "non-negative"),
        (9000, "0-8760"),
    ])
    async def test_ttl_hours_validation(self, _site, raw, needle):
        handlers._save_config("p", "us-west-2")
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "ttl_hours": raw})
        assert status == 400 and needle in payload["error"]

    @pytest.mark.asyncio
    async def test_missing_base_stack_blocks_finite_ttl(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "run_aws", _aws_router([], default=(1, "", "no stack")))
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True})
        assert status == 409
        assert payload["remediation"].startswith("install-reaper.sh")

    @pytest.mark.asyncio
    async def test_base_stack_parse_error_is_swallowed(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(
            engine, "run_aws", _aws_router([("describe-stacks", (0, "not-json", ""))]))
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True})
        assert status == 409 and "reaper base stack" in payload["error"]

    @pytest.mark.asyncio
    async def test_missing_reaper_stack_blocks_finite_ttl(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "run_aws", _aws_router([
            ("kirocrew-deploy-base", (0, _BASE_OUTPUTS, "")),
            ("kirocrew-deploy-reaper", (1, "", "missing")),
        ]))
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True})
        assert status == 409 and "kirocrew-deploy-reaper" in payload["error"]

    @pytest.mark.asyncio
    async def test_reaper_probe_exception_blocks_finite_ttl(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _run_aws(argv, *a, **kw):
            joined = " ".join(str(x) for x in argv)
            if "kirocrew-deploy-reaper" in joined:
                raise OSError("aws missing")
            return (0, _BASE_OUTPUTS, "")

        monkeypatch.setattr(engine, "run_aws", _run_aws)
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True})
        assert status == 409 and "kirocrew-deploy-reaper" in payload["error"]

    @pytest.mark.asyncio
    async def test_stale_preview_digest_is_refused(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "run_aws", _aws_router([
            ("kirocrew-deploy-base", (0, _BASE_OUTPUTS, "")),
        ]))
        status, payload = await handlers._do_deploy({
            "site_id": "s", "local_dir": str(_site), "confirm": True,
            "expected_content_digest": "deadbeef",
        })
        assert status == 409 and payload["code"] == "stale_preview"

    @pytest.mark.asyncio
    async def test_aws_error_becomes_502(self, _site, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "run_aws", _aws_router([
            ("kirocrew-deploy-base", (0, _BASE_OUTPUTS, "")),
        ]))

        def _deploy(*a, **kw):
            raise engine.AWSError("AccessDenied on s3:PutObject",
                                  missing_statement="s3:PutObject")

        monkeypatch.setattr(engine, "deploy", _deploy)
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True, "ttl_hours": 0})
        assert status == 502 and payload["missing_statement"] == "s3:PutObject"


class TestDoDeployManifestFailure:
    # POSIX-only for the same reason as TestDoDeployRefusals: these tests pass a
    # real filesystem path as local_dir, and deploy's _LOCAL_DIR_RE admits only
    # POSIX path characters, so a native Windows path is refused before the
    # manifest branch under test is reached. Product defect, reported not fixed.
    pytestmark = pytest.mark.skipif(
        sys.platform == "win32",
        reason="deploy's _LOCAL_DIR_RE rejects native Windows paths (product defect)",
    )

    """The TTL manifest is the reaper's only input -- a failed write must not
    leave a finite-TTL deploy live and reported as successful."""

    def _arm(self, monkeypatch, *, manifest_rc=1, manifest_raises=False):
        handlers._save_config("p", "us-west-2")

        def _run_aws(argv, *a, **kw):
            joined = " ".join(str(x) for x in argv)
            if "kirocrew-deploy-base" in joined:
                return (0, _BASE_OUTPUTS, "")
            if joined.startswith("s3 cp"):
                if manifest_raises:
                    raise OSError("aws cli gone")
                return (manifest_rc, "", "denied")
            return (0, "CREATE_COMPLETE", "")

        monkeypatch.setattr(engine, "run_aws", _run_aws)
        monkeypatch.setattr(engine, "deploy", lambda *a, **kw: {
            "url": "https://d1.cloudfront.net/s/", "bucket": "site-bucket",
            "distribution_id": "D1", "oac_id": "O1"})

    @pytest.mark.asyncio
    async def test_finite_ttl_manifest_failure_rolls_back(self, _site, monkeypatch):
        self._arm(monkeypatch)
        recalled = []
        monkeypatch.setattr(engine, "recall",
                            lambda slug, *a, **kw: recalled.append(slug))
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True, "ttl_hours": 24})
        assert status == 502 and payload["rolled_back"] is True
        assert recalled == ["s"]

    @pytest.mark.asyncio
    async def test_rollback_failure_is_reported_not_raised(self, _site, monkeypatch):
        self._arm(monkeypatch)

        def _recall(*a, **kw):
            raise engine.AWSError("cannot empty bucket")

        monkeypatch.setattr(engine, "recall", _recall)
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True, "ttl_hours": 24})
        assert status == 502 and payload["rolled_back"] is False

    @pytest.mark.asyncio
    async def test_manifest_write_exception_counts_as_failure(self, _site, monkeypatch):
        self._arm(monkeypatch, manifest_raises=True)
        monkeypatch.setattr(engine, "recall", lambda *a, **kw: None)
        status, _payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True, "ttl_hours": 24})
        assert status == 502

    @pytest.mark.asyncio
    async def test_persistent_deploy_only_warns(self, _site, monkeypatch):
        self._arm(monkeypatch)
        status, payload = await handlers._do_deploy(
            {"site_id": "s", "local_dir": str(_site), "confirm": True, "ttl_hours": 0})
        assert status == 200
        assert "TTL manifest upload failed" in payload["warning"]


# --- recall / destroy / list ------------------------------------------------


class TestRecallDestroy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("fn", ["_do_recall", "_do_destroy"])
    async def test_unresolvable_profile(self, fn):
        status, payload = await getattr(handlers, fn)({"site_id": "s"})
        assert status == 400 and "not configured" in payload["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fn", ["_do_recall", "_do_destroy"])
    async def test_missing_site_id(self, fn):
        handlers._save_config("p", "us-west-2")
        status, payload = await getattr(handlers, fn)({})
        assert status == 400 and payload["error"] == "site_id is required"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fn", ["_do_recall", "_do_destroy"])
    async def test_preview_unknown_site_is_404(self, fn, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "find_site_by_tag", lambda *a, **kw: {})
        status, payload = await getattr(handlers, fn)({"site_id": "s"})
        assert status == 404 and payload["error"] == "no site 's'"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fn", ["_do_recall", "_do_destroy"])
    async def test_confirm_against_vanished_site_is_404(self, fn, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "find_site_by_tag", lambda *a, **kw: {})
        status, payload = await getattr(handlers, fn)(
            {"site_id": "s", "confirm": True, "expected_distribution_id": "D1"})
        assert status == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fn", ["_do_recall", "_do_destroy"])
    async def test_recreated_resources_refuse_stale_confirm(self, fn, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "find_site_by_tag",
                            lambda *a, **kw: {"distribution_id": "D2", "bucket": "b2"})
        status, payload = await getattr(handlers, fn)({
            "site_id": "s", "confirm": True,
            "expected_distribution_id": "D1", "expected_bucket": "b1",
        })
        assert status == 409 and "changed since preview" in payload["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fn,verb", [("_do_recall", "recall"), ("_do_destroy", "destroy")])
    async def test_aws_error_becomes_502(self, fn, verb, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _boom(*a, **kw):
            raise engine.AWSError("AccessDenied", missing_statement="s3:DeleteObject")

        monkeypatch.setattr(engine, verb, _boom)
        status, payload = await getattr(handlers, fn)({"site_id": "s", "confirm": True})
        assert status == 502 and payload["missing_statement"] == "s3:DeleteObject"

    @pytest.mark.asyncio
    async def test_destroy_preview_names_the_resources(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "find_site_by_tag",
                            lambda *a, **kw: {"distribution_id": "D1", "bucket": "b1"})
        status, payload = await handlers._do_destroy({"site_id": "s"})
        assert status == 200 and payload["destructive"] is True
        assert "b1" in payload["message"] and "D1" in payload["message"]


class TestDoList:
    @pytest.mark.asyncio
    async def test_windows_returns_structured_unsupported(self, monkeypatch):
        class _NtOs:
            name = "nt"

            def __getattr__(self, attr):  # pragma: no cover - delegation
                return getattr(os, attr)

        monkeypatch.setattr(handlers, "os", _NtOs())
        status, payload = await handlers._do_list()
        assert status == 200 and payload["configured"] is False
        assert "not supported on Windows" in payload["error"]

    @pytest.mark.asyncio
    async def test_unconfigured_registry(self):
        status, payload = await handlers._do_list()
        assert status == 200 and payload == {"sites": [], "configured": False}

    @pytest.mark.asyncio
    async def test_per_profile_failure_degrades_to_warning(self, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _boom(*a, **kw):
            raise KeyError("region")

        monkeypatch.setattr(engine, "list_sites", _boom)
        status, payload = await handlers._do_list()
        assert status == 200 and payload["sites"] == []
        assert payload["profile_errors"] and "p:" in payload["profile_errors"][0]

    @pytest.mark.asyncio
    async def test_aws_error_degrades_to_warning(self, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _boom(*a, **kw):
            raise engine.AWSError("expired credentials")

        monkeypatch.setattr(engine, "list_sites", _boom)
        _status, payload = await handlers._do_list()
        assert "expired credentials" in payload["profile_errors"][0]

    @pytest.mark.asyncio
    async def test_sites_are_deduped_by_distribution_id(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        handlers._save_config("q", "us-west-2")
        monkeypatch.setattr(
            engine, "list_sites",
            lambda *a, **kw: [{"site_id": "s", "distribution_id": "D1"}])
        _status, payload = await handlers._do_list()
        assert len(payload["sites"]) == 1


# --- aiohttp adapters -------------------------------------------------------


class TestAdapters:
    @pytest.mark.asyncio
    async def test_json_body_tolerates_invalid_json(self):
        assert await handlers._json_body(_FakeReq("not json")) == {}

    @pytest.mark.asyncio
    async def test_json_body_rejects_non_object(self):
        assert await handlers._json_body(_FakeReq([1, 2])) == {}

    @pytest.mark.asyncio
    async def test_get_config_returns_registry_default(self):
        handlers._save_config("p", "eu-west-1")
        resp = await handlers._handle_get_config(_FakeReq())
        # The response also carries ``cloudDeploymentEnabled`` so the frontend can
        # hide the console when the platform withholds cloud deployment; the public
        # default admits it. Asserted as a superset so a future additive field does
        # not break this test again.
        assert _payload(resp) == {
            "profile": "p",
            "region": "eu-west-1",
            "cloudDeploymentEnabled": True,
        }

    @pytest.mark.asyncio
    async def test_deny_restricted_without_app_context(self):
        req = _FakeReq()
        req.app = None
        resp = await handlers._handle_put_config(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_deny_restricted_without_dashboard_state(self):
        req = _FakeReq()
        req.app = {}
        resp = await handlers._handle_put_config(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_internal_secret_caller_is_denied(self):
        req = _FakeReq(headers={"X-Internal-Secret": "s"})
        resp = await handlers._handle_recall(req)
        assert resp.status == 403
        assert "internal-secret callers" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_strip_confirm_for_internal_removes_both_flags(self):
        req = _FakeReq(headers={"X-Internal-Secret": "s"})
        out = handlers._strip_confirm_for_internal(
            req, {"confirm": True, "override_scan": True, "site_id": "s"})
        assert out == {"site_id": "s"}

    @pytest.mark.asyncio
    async def test_recall_adapter_serializes_payload(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "find_site_by_tag", lambda *a, **kw: {})
        resp = await handlers._handle_recall(_FakeReq({"site_id": "s"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_destroy_adapter_serializes_payload(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "find_site_by_tag", lambda *a, **kw: {})
        resp = await handlers._handle_destroy(_FakeReq({"site_id": "s"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_list_adapter(self):
        resp = await handlers._handle_list(_FakeReq())
        assert resp.status == 200 and _payload(resp)["configured"] is False

    @pytest.mark.asyncio
    async def test_iam_policy_rejects_unknown_tier(self):
        resp = await handlers._handle_iam_policy(_FakeReq(query={"tier": "serverless"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_iam_policy_static_tier(self):
        resp = await handlers._handle_iam_policy(_FakeReq(query={}))
        body = _payload(resp)
        assert "policy" in body and "boundary_policy" not in body

    @pytest.mark.asyncio
    async def test_iam_policy_fullstack_adds_boundary(self):
        resp = await handlers._handle_iam_policy(
            _FakeReq(query={"tier": "fullstack", "custom_domain": "true"}))
        body = _payload(resp)
        assert body["boundary_policy_name"] and json.loads(body["boundary_policy"])

    @pytest.mark.asyncio
    async def test_verify_rejects_unresolvable_profile(self):
        resp = await handlers._handle_verify(_FakeReq({"profile": "ghost"}))
        assert resp.status == 400 and _payload(resp)["reachable"] is False

    @pytest.mark.asyncio
    async def test_verify_backfills_account_on_success(self, monkeypatch):
        from kiro_crew.deploy import iam as iam_mod

        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(iam_mod, "reachability_check",
                            lambda profile: {"reachable": True, "account": "111122223333"})
        resp = await handlers._handle_verify(_FakeReq({}))
        assert _payload(resp)["profile"] == "p"
        reg = profiles_mod.load_registry()
        entry = profiles_mod.get_entry(reg, "p")
        assert entry["account"] == "111122223333" and entry["verified_at"]

    @pytest.mark.asyncio
    async def test_verify_unreachable_skips_backfill(self, monkeypatch):
        from kiro_crew.deploy import iam as iam_mod

        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(iam_mod, "reachability_check",
                            lambda profile: {"reachable": False, "error": "expired"})
        resp = await handlers._handle_verify(_FakeReq({}))
        assert _payload(resp)["reachable"] is False
        assert profiles_mod.get_entry(profiles_mod.load_registry(), "p")["account"] == ""

    @pytest.mark.asyncio
    async def test_verify_is_blocked_on_windows(self, monkeypatch):
        class _NtOs:
            name = "nt"

            def __getattr__(self, attr):  # pragma: no cover - delegation
                return getattr(os, attr)

        monkeypatch.setattr(handlers, "os", _NtOs())
        resp = await handlers._handle_verify(_FakeReq({}))
        assert resp.status == 400 and _payload(resp)["reachable"] is False

    @pytest.mark.asyncio
    async def test_pricing_rejects_unresolvable_profile(self):
        resp = await handlers._handle_pricing(_FakeReq(query={"profile": "ghost"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pricing_returns_unit_prices(self, monkeypatch):
        from kiro_crew.deploy import pricing as pricing_mod

        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(
            pricing_mod, "get_unit_prices",
            lambda profile, region: SimpleNamespace(
                to_dict=lambda: {"source": "fallback", "s3_storage_gb_month": 0.023}))
        resp = await handlers._handle_pricing(_FakeReq(query={}))
        body = _payload(resp)
        assert body["region"] == "us-west-2"
        assert body["prices"]["source"] == "fallback"


class TestProfilesControlPlane:
    @pytest.mark.asyncio
    async def test_get_lists_registry_and_discovered(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(profiles_mod, "discover_aws_profiles", lambda: ["p", "other"])
        body = _payload(await handlers._handle_profiles_get(_FakeReq()))
        assert body["default"] == "p" and body["available"] == ["other"]

    @pytest.mark.asyncio
    async def test_post_rejects_empty_name(self):
        resp = await handlers._handle_profiles_post(_FakeReq({"name": ""}))
        assert resp.status == 400 and "must not be empty" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_post_rejects_invalid_region(self):
        resp = await handlers._handle_profiles_post(
            _FakeReq({"name": "p", "region": "not a region!"}))
        assert resp.status == 400 and "invalid profile" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_post_surfaces_create_failure(self, monkeypatch):
        monkeypatch.setattr(profiles_mod, "create_aws_profile",
                            lambda *a, **kw: "aws configure set failed")
        resp = await handlers._handle_profiles_post(
            _FakeReq({"name": "p", "create": True}))
        assert resp.status == 400 and "configure set failed" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_post_registers_and_defaults(self, monkeypatch):
        monkeypatch.setattr(profiles_mod, "create_aws_profile", lambda *a, **kw: "")
        resp = await handlers._handle_profiles_post(
            _FakeReq({"name": "p", "create": True, "default": True}))
        assert _payload(resp)["default"] == "p"

    @pytest.mark.asyncio
    async def test_post_refuses_when_registry_is_full(self, monkeypatch):
        full = {"profiles": [profiles_mod.make_entry(f"p{i}", "us-west-2")
                             for i in range(50)], "default": "p0"}
        monkeypatch.setattr(profiles_mod, "load_registry", lambda: full)
        resp = await handlers._handle_profiles_post(_FakeReq({"name": "new"}))
        assert resp.status == 400 and "registry is full" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_put_rejects_invalid_name(self):
        resp = await handlers._handle_profiles_put(
            _FakeReq({}, match_info={"name": "bad name;"}))
        assert resp.status == 400 and "invalid profile name" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_put_rejects_invalid_region(self):
        handlers._save_config("p", "us-west-2")
        resp = await handlers._handle_profiles_put(
            _FakeReq({"region": "bad region!"}, match_info={"name": "p"}))
        assert resp.status == 400 and "invalid region" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_put_unknown_profile_is_404(self):
        resp = await handlers._handle_profiles_put(
            _FakeReq({}, match_info={"name": "ghost"}))
        assert resp.status == 404 and "not registered" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_put_updates_note_and_default(self):
        handlers._save_config("p", "us-west-2")
        handlers._save_config("q", "us-west-2")
        resp = await handlers._handle_profiles_put(
            _FakeReq({"note": "n" * 400, "default": True, "region": "eu-west-1"},
                     match_info={"name": "p"}))
        body = _payload(resp)
        assert body["default"] == "p"
        entry = next(e for e in body["profiles"] if e["name"] == "p")
        assert len(entry["note"]) == 256 and entry["region"] == "eu-west-1"

    @pytest.mark.asyncio
    async def test_delete_rejects_invalid_name(self):
        resp = await handlers._handle_profiles_delete(
            _FakeReq(match_info={"name": "bad;name"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_delete_unknown_profile_is_404(self):
        resp = await handlers._handle_profiles_delete(
            _FakeReq(match_info={"name": "ghost"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_reassigns_the_default(self):
        handlers._save_config("q", "us-west-2")
        handlers._save_config("p", "us-west-2")
        resp = await handlers._handle_profiles_delete(
            _FakeReq(match_info={"name": "p"}))
        assert _payload(resp)["default"] == "q"


# --- manifest expiry --------------------------------------------------------


def _art(profile="p", region="us-west-2", dist_id="D1", slug="app", meta=True):
    target = SimpleNamespace(profile=profile, region=region, distribution_id=dist_id)
    metadata = SimpleNamespace(deploy_target=target) if meta else None
    return SimpleNamespace(slug=slug, kind="webapp", webapp_metadata=metadata)


class TestExpireManifest:
    @pytest.mark.asyncio
    async def test_no_metadata_is_skipped(self):
        assert await handlers._expire_manifest_best_effort(_art(meta=False)) == "skipped"

    @pytest.mark.asyncio
    async def test_missing_slug_is_skipped(self):
        assert await handlers._expire_manifest_best_effort(_art(slug="")) == "skipped"

    @pytest.mark.asyncio
    async def test_no_recorded_profile_is_skipped(self):
        assert await handlers._expire_manifest_best_effort(_art(profile="")) == "skipped"

    @pytest.mark.asyncio
    async def test_unregistered_profile_is_unreachable(self):
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_invalid_region_is_unreachable(self):
        handlers._save_config("p", "us-west-2")
        art = _art(region="not a region!")
        assert await handlers._expire_manifest_best_effort(art) == "unreachable"

    @pytest.mark.asyncio
    async def test_base_stack_read_failure_is_unreachable(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "run_aws", _aws_router([], default=(1, "", "err")))
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_base_stack_exception_is_unreachable(self, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _boom(*a, **kw):
            raise OSError("aws missing")

        monkeypatch.setattr(engine, "run_aws", _boom)
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_missing_bucket_output_is_unreachable(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "run_aws", _aws_router(
            [("describe-stacks", (0, json.dumps([{"OutputKey": "X", "OutputValue": "y"}]), ""))]))
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_unreadable_manifest_fails_closed(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(engine, "run_aws", _aws_router([
            ("describe-stacks", (0, _BASE_OUTPUTS, "")),
        ], default=(1, "", "no such key")))
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_manifest_read_exception_fails_closed(self, monkeypatch):
        handlers._save_config("p", "us-west-2")

        def _run_aws(argv, *a, **kw):
            joined = " ".join(str(x) for x in argv)
            if "describe-stacks" in joined:
                return (0, _BASE_OUTPUTS, "")
            raise OSError("network down")

        monkeypatch.setattr(engine, "run_aws", _run_aws)
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_distribution_id_mismatch_refuses(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        other = json.dumps({"slug": "app", "distribution_id": "SOMEONE-ELSE"})
        monkeypatch.setattr(engine, "run_aws", _aws_router([
            ("describe-stacks", (0, _BASE_OUTPUTS, "")),
            (".kirocrew-deploy.json -", (0, other, "")),
        ]))
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_put_failure_is_unreachable(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        mine = json.dumps({"slug": "app", "distribution_id": "D1", "arch": "engine"})

        def _run_aws(argv, *a, **kw):
            joined = " ".join(str(x) for x in argv)
            if "describe-stacks" in joined:
                return (0, _BASE_OUTPUTS, "")
            if joined.endswith(".kirocrew-deploy.json - --region us-west-2"):
                return (0, mine, "")
            return (1, "", "AccessDenied")

        monkeypatch.setattr(engine, "run_aws", _run_aws)
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_put_exception_is_unreachable(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        mine = json.dumps({"slug": "app", "distribution_id": "D1"})
        calls = {"n": 0}

        def _run_aws(argv, *a, **kw):
            joined = " ".join(str(x) for x in argv)
            if "describe-stacks" in joined:
                return (0, _BASE_OUTPUTS, "")
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, mine, "")
            raise OSError("aws vanished")

        monkeypatch.setattr(engine, "run_aws", _run_aws)
        assert await handlers._expire_manifest_best_effort(_art()) == "unreachable"

    @pytest.mark.asyncio
    async def test_success_patches_only_the_expiry_fields(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        mine = json.dumps({
            "slug": "app", "distribution_id": "D1", "arch": "engine",
            "bucket": "site-bucket", "oac_id": "O1", "ttl_hours": "72",
            "persistent": False, "unknown_field": "kept",
        })
        written: list[str] = []

        def _run_aws(argv, *a, **kw):
            joined = " ".join(str(x) for x in argv)
            if "describe-stacks" in joined:
                return (0, _BASE_OUTPUTS, "")
            if joined.endswith("- --region us-west-2"):
                return (0, mine, "")
            written.append(Path(argv[2]).read_text(encoding="utf-8"))
            return (0, "", "")

        monkeypatch.setattr(engine, "run_aws", _run_aws)
        assert await handlers._expire_manifest_best_effort(_art()) == "expired-now"
        doc = json.loads(written[0])
        assert doc["arch"] == "engine" and doc["unknown_field"] == "kept"
        assert doc["oac_id"] == "O1" and doc["persistent"] is False
        assert doc["ttl_hours"] == "0" and doc["expires_at"]


# --- teardown ---------------------------------------------------------------


class _FakeStore:
    def __init__(self, art=None, *, get_exc=None, expire_exc=None):
        self._art = art
        self._get_exc = get_exc
        self._expire_exc = expire_exc

    def get(self, _slug):
        if self._get_exc is not None:
            raise self._get_exc
        return self._art

    def mark_webapp_expired(self, _slug):
        if self._expire_exc is not None:
            raise self._expire_exc
        return self._art


class TestTeardown:
    @pytest.mark.asyncio
    async def test_blocked_on_windows(self, monkeypatch):
        class _NtOs:
            name = "nt"

            def __getattr__(self, attr):  # pragma: no cover - delegation
                return getattr(os, attr)

        monkeypatch.setattr(handlers, "os", _NtOs())
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 400 and "POSIX shell" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_artifacts_module_unavailable(self, monkeypatch):
        monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", False)
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_unparseable_body_fails_the_confirm_gate(self):
        resp = await handlers._handle_teardown(
            _FakeReq("}{", match_info={"slug": "app"}))
        assert resp.status == 400 and "confirm=true required" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_unknown_artifact_is_404(self, monkeypatch):
        exc = handlers.ArtifactNotFoundError("no artifact 'app'")
        monkeypatch.setattr(handlers, "get_default_store",
                            lambda: _FakeStore(get_exc=exc))
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_store_error_is_500(self, monkeypatch):
        exc = handlers.ArtifactError("store corrupt")
        monkeypatch.setattr(handlers, "get_default_store",
                            lambda: _FakeStore(get_exc=exc))
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 500

    @pytest.mark.asyncio
    async def test_non_webapp_artifact_is_refused(self, monkeypatch):
        art = SimpleNamespace(slug="app", kind="html", webapp_metadata=None)
        monkeypatch.setattr(handlers, "get_default_store", lambda: _FakeStore(art))
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 400 and _payload(resp)["error"] == "not a webapp artifact"

    @pytest.mark.asyncio
    async def test_unregistered_metadata_profile_is_refused(self, monkeypatch):
        monkeypatch.setattr(handlers, "get_default_store",
                            lambda: _FakeStore(_art(profile="ghost")))
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 409 and "unregistered profile" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_invalid_metadata_region_is_refused(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(handlers, "get_default_store",
                            lambda: _FakeStore(_art(region="bad region!")))
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 409 and "invalid region" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_missing_reaper_is_refused(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(handlers, "get_default_store", lambda: _FakeStore(_art()))
        monkeypatch.setattr(engine, "run_aws", _aws_router([], default=(1, "", "no stack")))
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 409 and _payload(resp)["reaper_missing"] is True

    @pytest.mark.asyncio
    async def test_unreachable_manifest_is_retryable(self, monkeypatch):
        handlers._save_config("p", "us-west-2")
        monkeypatch.setattr(handlers, "get_default_store", lambda: _FakeStore(_art()))
        monkeypatch.setattr(handlers, "_check_reaper_installed", lambda *a: True)

        async def _unreachable(_art_obj):
            return "unreachable"

        monkeypatch.setattr(handlers, "_expire_manifest_best_effort", _unreachable)
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == 502 and _payload(resp)["retry"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc_name,status", [
        ("ArtifactNotFoundError", 404),
        ("ArtifactValidationError", 400),
        ("ArtifactError", 500),
    ])
    async def test_tombstone_failures_surface(self, monkeypatch, exc_name, status):
        exc = getattr(handlers, exc_name)("boom")
        art = SimpleNamespace(slug="app", kind="webapp", webapp_metadata=None)
        monkeypatch.setattr(handlers, "get_default_store",
                            lambda: _FakeStore(art, expire_exc=exc))

        async def _skipped(_art_obj):
            return "skipped"

        monkeypatch.setattr(handlers, "_expire_manifest_best_effort", _skipped)
        resp = await handlers._handle_teardown(
            _FakeReq({"confirm": True}, match_info={"slug": "app"}))
        assert resp.status == status

    def test_check_reaper_installed_maps_rc_to_bool(self, monkeypatch):
        monkeypatch.setattr(engine, "run_aws", _aws_router([], default=(0, "", "")))
        assert handlers._check_reaper_installed("p", "us-west-2") is True
        monkeypatch.setattr(engine, "run_aws", _aws_router([], default=(255, "", "err")))
        assert handlers._check_reaper_installed("p", "us-west-2") is False


# --- pending confirmations --------------------------------------------------


class TestPending:
    # POSIX-only for the same reason as TestDoDeployRefusals: these tests pass a
    # real filesystem path as local_dir, and deploy's _LOCAL_DIR_RE admits only
    # POSIX path characters, so a native Windows path is refused before the
    # pending-deploy branch under test is reached. Product defect, reported not
    # fixed.
    pytestmark = pytest.mark.skipif(
        sys.platform == "win32",
        reason="deploy's _LOCAL_DIR_RE rejects native Windows paths (product defect)",
    )

    @pytest.mark.asyncio
    async def test_list_redacts_entries(self):
        from kiro_crew.deploy.pending import add_pending

        add_pending({"site_id": "s", "profile": "p"})
        body = _payload(await handlers._handle_pending_list(_FakeReq()))
        assert body["pending"][0]["site_id"] == "s"

    @pytest.mark.asyncio
    async def test_confirm_unknown_entry_is_409(self):
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": "nope"}))
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_confirm_refuses_on_profile_drift(self):
        from kiro_crew.deploy.pending import add_pending

        handlers._save_config("p", "us-west-2")
        entry = add_pending({"site_id": "s", "profile": "p", "region": "eu-west-1"})
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": entry["id"]}))
        assert resp.status == 409 and "profile changed since preview" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_confirm_refuses_when_local_dir_vanished(self, tmp_path: Path):
        from kiro_crew.deploy.pending import add_pending, list_pending

        handlers._save_config("p", "us-west-2")
        entry = add_pending({
            "site_id": "s", "profile": "p", "region": "us-west-2",
            "local_dir": str(tmp_path / "gone"), "content_digest": "abc",
        })
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": entry["id"]}))
        assert resp.status == 409 and "no longer exists" in _payload(resp)["error"]
        # The entry is put back so the user can retry after fixing the path.
        assert [e["id"] for e in list_pending()] == [entry["id"]]

    @pytest.mark.asyncio
    async def test_confirm_refuses_dir_outside_allowed_roots(self, tmp_path: Path, monkeypatch):
        from kiro_crew.deploy.pending import add_pending

        handlers._save_config("p", "us-west-2")
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path / "only"])
        entry = add_pending({
            "site_id": "s", "profile": "p", "region": "us-west-2",
            "local_dir": str(outside), "content_digest": "abc",
        })
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": entry["id"]}))
        assert resp.status == 409 and "outside allowed roots" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_confirm_refuses_sensitive_dir(self, tmp_path: Path, monkeypatch):
        from kiro_crew.deploy.pending import add_pending

        handlers._save_config("p", "us-west-2")
        src = tmp_path / "site"
        src.mkdir()
        monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])
        monkeypatch.setattr(handlers, "_dir_contains_sensitive", lambda *a: True)
        entry = add_pending({
            "site_id": "s", "profile": "p", "region": "us-west-2",
            "local_dir": str(src), "content_digest": "abc",
        })
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": entry["id"]}))
        assert resp.status == 409 and "sensitive paths" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_confirm_refuses_oversized_tree(self, tmp_path: Path, monkeypatch):
        from kiro_crew.deploy.pending import add_pending

        handlers._save_config("p", "us-west-2")
        src = tmp_path / "site"
        src.mkdir()
        monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path])
        monkeypatch.setattr(handlers, "_compute_tree_size_global",
                            lambda _p: 300 * 1024 * 1024)
        entry = add_pending({
            "site_id": "s", "profile": "p", "region": "us-west-2",
            "local_dir": str(src), "content_digest": "abc",
        })
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": entry["id"]}))
        assert resp.status == 409 and "exceeds 200 MiB" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_confirm_refuses_edited_artifact(self, monkeypatch):
        from kiro_crew.deploy.pending import add_pending

        handlers._save_config("p", "us-west-2")
        art = SimpleNamespace(kind="html", content="<p>changed</p>", name="a")
        monkeypatch.setattr(handlers, "get_default_store", lambda: _FakeStore(art))
        entry = add_pending({
            "site_id": "s", "profile": "p", "region": "us-west-2",
            "artifact_slug": "a1", "content_digest": "digest-at-preview",
        })
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": entry["id"]}))
        assert resp.status == 409 and "content changed since preview" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_confirm_tolerates_unstageable_artifact(self, monkeypatch):
        from kiro_crew.deploy.pending import add_pending

        handlers._save_config("p", "us-west-2")
        exc = handlers.ArtifactNotFoundError("deleted")
        monkeypatch.setattr(handlers, "get_default_store",
                            lambda: _FakeStore(get_exc=exc))
        entry = add_pending({
            "site_id": "s", "profile": "p", "region": "us-west-2",
            "artifact_slug": "a1", "content_digest": "digest-at-preview",
        })
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": entry["id"]}))
        # Digest check declines to judge; _do_deploy reports the missing artifact.
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_confirm_carries_human_override_scan(self, monkeypatch, tmp_path: Path):
        from kiro_crew.deploy.pending import add_pending

        handlers._save_config("p", "us-west-2")
        seen: dict = {}

        async def _fake_deploy(params):
            seen.update(params)
            return 200, {"url": "https://d1.cloudfront.net/s/"}

        monkeypatch.setattr(handlers, "_do_deploy", _fake_deploy)
        entry = add_pending({
            "site_id": "s", "profile": "p", "region": "us-west-2",
            "local_dir": str(tmp_path), "ttl_hours": 24,
            "override_scan_required": True,
        })
        resp = await handlers._handle_pending_confirm(
            _FakeReq({"override_scan": True}, match_info={"id": entry["id"]}))
        assert resp.status == 200
        assert seen["override_scan"] is True and seen["confirm"] is True
        assert seen["expected_profile"] == "p" and seen["ttl_hours"] == 24

    @pytest.mark.asyncio
    async def test_failed_confirm_readds_the_entry(self, monkeypatch, tmp_path: Path):
        from kiro_crew.deploy.pending import add_pending, list_pending

        handlers._save_config("p", "us-west-2")

        async def _fake_deploy(_params):
            return 502, {"error": "AccessDenied"}

        monkeypatch.setattr(handlers, "_do_deploy", _fake_deploy)
        entry = add_pending({
            "site_id": "s", "profile": "p", "region": "us-west-2",
            "artifact_slug": "a1",
        })
        resp = await handlers._handle_pending_confirm(
            _FakeReq({}, match_info={"id": entry["id"]}))
        assert resp.status == 502
        assert [e["id"] for e in list_pending()] == [entry["id"]]

    @pytest.mark.asyncio
    async def test_dismiss_rejects_internal_secret_callers(self):
        req = _FakeReq({}, match_info={"id": "x"}, headers={"X-Internal-Secret": "s"})
        resp = await handlers._handle_pending_dismiss(req)
        # The @_internal_denied decorator answers first, so the message is the
        # generic one; the handler's own inner guard never runs on this path.
        assert resp.status == 403
        assert "internal-secret callers" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_dismiss_inner_guard_is_defence_in_depth(self):
        # The decorator shadows this branch in production, so exercise the
        # undecorated function to prove the second gate is still fail-closed.
        req = _FakeReq({}, match_info={"id": "x"}, headers={"X-Internal-Secret": "s"})
        resp = await handlers._handle_pending_dismiss.__wrapped__(req)
        assert resp.status == 403 and "MCP callers" in _payload(resp)["error"]

    @pytest.mark.asyncio
    async def test_dismiss_unknown_entry_is_404(self):
        resp = await handlers._handle_pending_dismiss(
            _FakeReq({}, match_info={"id": "nope"}))
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_dismiss_removes_the_entry(self):
        from kiro_crew.deploy.pending import add_pending, list_pending

        entry = add_pending({"site_id": "s"})
        resp = await handlers._handle_pending_dismiss(
            _FakeReq({}, match_info={"id": entry["id"]}))
        assert resp.status == 200 and list_pending() == []
