"""The hosted-clip subsystem must not add work to the gateway boot path.

What this pins, and what it deliberately does not:

* **No work scheduled before readiness.** The transfer is started after
  ``KIROCREW_READY``, inside a ``dashboard.feature_videos_enabled`` test, and the
  starter is imported there rather than at module scope. ``_start_embeddings`` — which
  runs on the boot path — must not reach it at all.

* **Import weight is NOT claimed.** ``feature_videos``, ``feature_videos_cache`` and
  ``feature_videos_manifest`` still load when the gateway is imported, because
  ``dashboard/routes/realtime.py`` imports its handlers at module scope the way every
  route module in this repo does. Deferring that is a repo-wide route-registration
  change, not this feature's to make. So the boot-path property bought here is "no new
  WORK", not "no new bytes imported" — asserting the latter would be a test that
  passes only until someone reads it.

* **Credential text in a log record.** ``http.client.InvalidURL`` puts the offending
  url verbatim in its message, so a credentialed override reaches the log through the
  EXCEPTION even when the url argument beside it is redacted. Asserted on the emitted
  record rather than on the call, so a future ``exc_info=True`` — which renders the
  same text into a traceback — fails here too.
"""

from __future__ import annotations

import http.client
import inspect
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import feature_videos_manifest as manifest_mod

_CREDENTIAL = "sup3rs3cret"
_URL_WITH_CREDENTIAL = f"https://carol:{_CREDENTIAL}@cdn.example.com:notaport/manifest.json"
_STARTER = "start_background_feature_video_download"


def _run(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        # -B: the child imports repo modules and must not leave __pycache__ beside them.
        [sys.executable, "-B", "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


class TestNoWorkOnTheBootPath:
    def test_the_boot_path_method_never_reaches_the_starter(self) -> None:
        """``_start_embeddings`` runs before KIROCREW_READY, so it must not start a transfer."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        src = inspect.getsource(GatewayOrchestrator._start_embeddings)
        assert _STARTER not in src, "a clip transfer is being scheduled on the boot path"

    def test_the_gateway_module_has_no_top_level_import_of_the_starter(self) -> None:
        """A module-scope import runs on every boot, including a default-off one."""
        from kiro_crew.slack import gateway as gw

        for line in inspect.getsource(gw).splitlines():
            # Column 0 only. An INDENTED import is the deferred one inside the
            # flag test, which is the shape this fix wants -- flagging it would
            # make the test contradict the change it exists to protect.
            if line[:1].isspace() or not line.startswith(("import ", "from ")):
                continue
            assert (
                _STARTER not in line and "feature_videos_cache" not in line
            ), f"module-scope import of the clip cache: {line}"

    def test_the_start_site_is_gated_on_the_kill_switch(self) -> None:
        """Off by default, so an install that never wanted this pays nothing for it."""
        from kiro_crew.slack import gateway as gw

        src = inspect.getsource(gw)
        marker = "if self._cfg.dashboard.feature_videos_enabled:"
        assert marker in src, "the start site is not gated on the feature flag"
        gated = src.split(marker, 1)[1][:600]
        assert _STARTER in gated, "the starter is not inside the flag test"

    def test_the_starter_is_reachable_when_something_asks_for_it(self) -> None:
        """The deferral must not have orphaned the entry point it defers."""
        proc = _run("""
            from kiro_crew.feature_videos_cache import start_background_feature_video_download

            assert callable(start_background_feature_video_download)
            print("reachable")
            """)
        assert proc.returncode == 0, proc.stderr
        assert "reachable" in proc.stdout


class TestTheTaskIsHeldAndCancelled:
    def test_the_task_attribute_exists_before_anything_starts_it(self) -> None:
        """Declared in __init__, so a reader that runs first is not an AttributeError."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        src = inspect.getsource(GatewayOrchestrator.__init__)
        assert 'self._feature_video_task: "asyncio.Task[bool] | None" = None' in src

    def test_shutdown_cancels_it_beside_its_sibling(self) -> None:
        """A pending transfer must not outlive the process it was started for."""
        from kiro_crew.slack import gateway as gw

        src = inspect.getsource(gw)
        assert (
            "if self._feature_video_task is not None and not self._feature_video_task.done():"
            in src
        ), "the transfer task is never cancelled on shutdown"


class TestNoCredentialInTheLogRecord:
    def test_a_credential_in_a_malformed_url_never_reaches_the_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """InvalidURL carries the url in its own message, so the type is all we log."""

        def _explode(*_a: object, **_k: object) -> object:
            raise http.client.InvalidURL(f"nonnumeric port: '{_CREDENTIAL}@cdn.example.com'")

        opener = type("_Opener", (), {"open": staticmethod(_explode)})()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: opener)

        with caplog.at_level(logging.INFO, logger=manifest_mod.logger.name):
            assert manifest_mod._fetch_json(_URL_WITH_CREDENTIAL) is None

        assert caplog.records, "the failure should still be reported"
        for record in caplog.records:
            rendered = record.getMessage()
            assert _CREDENTIAL not in rendered, f"credential reached the log: {rendered}"
            assert "InvalidURL" in rendered, f"the type is the diagnostic: {rendered}"
            # exc_info renders the exception's own str(), which is the leaking text.
            assert record.exc_info is None, "a traceback would relocate the leak, not close it"

    def test_the_redacted_url_keeps_only_scheme_and_host(self) -> None:
        """The other half of the same line: the url argument carries no credential."""
        redacted = manifest_mod.asset_downloader.redact_url(_URL_WITH_CREDENTIAL)
        assert _CREDENTIAL not in redacted
        assert "carol" not in redacted
        assert "manifest.json" not in redacted


class TestTheDownloaderLeaksNothingEither:
    """``download_to`` RETURNS its error text, and the caller stores it.

    ``feature_videos_cache.ensure_all`` puts that string in the cache's failure state,
    which ``/api/feature-videos/status`` serves. So an unredacted url in the return
    value travels further than a log line does — it reaches a dashboard reader.
    """

    @pytest.mark.parametrize(
        "raised",
        [
            http.client.InvalidURL(f"nonnumeric port: '{_CREDENTIAL}@cdn.example.com'"),
            OSError(f"tunnel failed for https://carol:{_CREDENTIAL}@cdn.example.com/clip.mp4"),
        ],
        ids=["catch-all-branch", "transport-branch"],
    )
    def test_no_credential_in_the_returned_error_or_the_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        raised: Exception,
    ) -> None:
        from kiro_crew import asset_downloader

        def _explode(*_a: object, **_k: object) -> object:
            raise raised

        opener = type("_Opener", (), {"open": staticmethod(_explode)})()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: opener)

        with caplog.at_level(logging.INFO):
            ok, err = asset_downloader.download_to(
                tmp_path / "clip.mp4",
                _URL_WITH_CREDENTIAL,
                sha256="0" * 64,
                label="clip",
            )

        assert ok is False
        assert _CREDENTIAL not in err, f"credential in the returned error: {err}"
        assert "carol" not in err, f"userinfo in the returned error: {err}"
        assert type(raised).__name__ in err, f"the type is the diagnostic: {err}"
        for record in caplog.records:
            rendered = record.getMessage()
            assert _CREDENTIAL not in rendered, f"credential in a log record: {rendered}"
            assert record.exc_info is None, "a traceback would render the leaking str()"
