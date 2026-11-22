"""The browser-recording skill must stay wired and fail loud.

Locks three joints: (1) the SKILL.md is discoverable and cross-references the
frontend-design-workflow evidence rule it implements, (2) the runner rejects
bad input before touching node/playwright, (3) the probe-first dependency
contract (nothing auto-installed, ffmpeg optional) survives edits.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from loopback_servers import LoopbackHTTPServer

from kiro_crew.testing.links import make_dir_link

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "src" / "kiro_crew" / "builtin_skills" / "browser-recording"
SKILL = SKILL_DIR / "SKILL.md"
RUNNER = SKILL_DIR / "scripts" / "record_browser.py"
DRIVER = SKILL_DIR / "scripts" / "driver.mjs"


def _load_runner():
    spec = importlib.util.spec_from_file_location("record_browser", RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSkillDoc:
    def test_exists_with_frontmatter(self) -> None:
        body = SKILL.read_text(encoding="utf-8")
        assert body.startswith("---\n"), "skill needs YAML frontmatter to be discoverable"
        assert "name: browser-recording" in body
        assert "triggers:" in body

    def test_frontmatter_is_single_line_values(self) -> None:
        """The simple key:value parser cannot read multi-line YAML values."""
        body = SKILL.read_text(encoding="utf-8")
        header = body.split("---", 2)[1]
        for line in header.strip().splitlines():
            assert ":" in line, f"frontmatter line is not key:value — {line!r}"

    def test_cross_references_evidence_rule(self) -> None:
        """This skill is the HOW for frontend-design-workflow's evidence rule."""
        body = SKILL.read_text(encoding="utf-8")
        assert "frontend-design-workflow" in body
        assert "screenshot" in body.lower()

    def test_dependency_contract_documented(self) -> None:
        body = SKILL.read_text(encoding="utf-8")
        assert "never auto-installed" in body
        assert "npx playwright install chromium" in body
        assert "ffmpeg" in body

    def test_driver_and_runner_ship_together(self) -> None:
        assert RUNNER.is_file()
        assert DRIVER.is_file()
        # The runner locates the driver as a sibling — keep that invariant.
        assert 'with_name("driver.mjs")' in RUNNER.read_text(encoding="utf-8")


class TestRunnerValidation:
    """Bad input must die before any node/playwright/ffmpeg probing."""

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(RUNNER), *argv], capture_output=True, text=True, encoding="utf-8"
        )

    def test_rejects_non_http_url(self) -> None:
        p = self._run("--url", "file:///etc/hosts")
        assert p.returncode != 0
        assert "http(s)" in p.stderr

    def test_rejects_bad_size(self) -> None:
        p = self._run("--url", "http://127.0.0.1:1/", "--size", "huge")
        assert p.returncode != 0
        assert "1280x800" in p.stderr

    def test_rejects_missing_scenario(self) -> None:
        p = self._run("--url", "http://127.0.0.1:1/", "--scenario", "/nonexistent/s.mjs")
        assert p.returncode != 0
        assert "not found" in p.stderr

    def test_rejects_non_mjs_scenario(self, tmp_path: Path) -> None:
        s = tmp_path / "scenario.js"
        s.write_text("export default async () => {}\n")
        p = self._run("--url", "http://127.0.0.1:1/", "--scenario", str(s))
        assert p.returncode != 0
        assert ".mjs" in p.stderr

    def test_rejects_traversal_name(self) -> None:
        p = self._run("--url", "http://127.0.0.1:1/", "--name", "../../evil")
        assert p.returncode != 0
        assert "plain filename" in p.stderr

    def test_missing_playwright_fails_loud_without_install(self, tmp_path: Path) -> None:
        """Empty project dir → precise remediation message, no auto-install."""
        p = self._run(
            "--url", "http://127.0.0.1:1/", "--project", str(tmp_path)
        )
        assert p.returncode != 0
        assert "npm i -D playwright" in p.stderr
        # Fail-loud means nothing was created in the project.
        assert not (tmp_path / "node_modules").exists()


class TestRecordedLineContainment:
    """A forged RECORDED line must never make the runner move a host file.

    Scenario/page output is forwarded to stdout, so the marker is
    attacker-influenceable. The
    runner must (a) take the LAST marker line, (b) reject paths that resolve
    outside --out.
    """

    def _main_with_driver_stdout(self, stdout: str, out_dir: Path, monkeypatch) -> None:
        mod = _load_runner()
        monkeypatch.setattr(mod, "_probe_node", lambda: "node")
        monkeypatch.setattr(mod, "_probe_playwright", lambda project: None)
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: fake)
        mod.main(["--url", "http://127.0.0.1:1/", "--out", str(out_dir), "--name", "x"])

    def test_forged_outside_path_rejected(self, tmp_path: Path, monkeypatch) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("do not move me")
        with pytest.raises(SystemExit):
            self._main_with_driver_stdout(
                f"RECORDED {victim}\n", tmp_path / "out", monkeypatch
            )
        assert victim.is_file(), "runner must not touch a path outside --out"

    def test_last_marker_wins(self, tmp_path: Path, monkeypatch) -> None:
        """A scenario-forged early marker is ignored in favor of the driver's."""
        out = tmp_path / "out"
        out.mkdir()
        real = out / "realvideo.webm"
        real.write_bytes(b"webm")
        forged = tmp_path / "forged.webm"
        forged.write_bytes(b"evil")
        monkeypatch.setattr("shutil.which", lambda name: None)  # skip ffmpeg
        self._main_with_driver_stdout(
            f"RECORDED {forged}\nRECORDED {real}\n", out, monkeypatch
        )
        assert (out / "x.webm").is_file(), "driver's (last) marker must be used"
        assert forged.is_file(), "forged path must be untouched"

    def test_default_out_dir_under_a_linked_temp_root(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """mkdtemp keeps the temp root's own spelling while the RECORDED path is resolved.

        A directory link stands in for any root resolve() spells differently (an 8.3
        TEMP on Windows, macOS's /var). The driver's own video must be accepted, and a
        path outside the folder must still be refused.
        """
        real_tmp = tmp_path / "real-tmp"
        real_tmp.mkdir()
        make_dir_link(tmp_path / "link-tmp", real_tmp)
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "link-tmp"))
        victim = tmp_path / "victim.webm"
        victim.write_bytes(b"do not move me")
        mod = _load_runner()
        monkeypatch.setattr(mod, "_probe_node", lambda: "node")
        monkeypatch.setattr(mod, "_probe_playwright", lambda project: None)
        monkeypatch.setattr(mod, "_ffmpeg", lambda: None)

        def run_with_marker(marker):
            def driver(argv, **kw):
                # Like Playwright: the video path joins outDir as given, unresolved.
                out_dir = Path(json.loads(Path(argv[2]).read_text(encoding="utf-8"))["outDir"])
                (out_dir / "abc.webm").write_bytes(b"webm")
                return subprocess.CompletedProcess(
                    argv, 0, stdout=f"RECORDED {marker(out_dir)}\n", stderr=""
                )

            monkeypatch.setattr(mod.subprocess, "run", driver)
            try:
                return mod.main(["--url", "http://127.0.0.1:1/", "--name", "x"])
            except SystemExit as exc:
                return exc.code

        assert run_with_marker(lambda out_dir: out_dir / "abc.webm") == 0
        webm = Path(capsys.readouterr().out.split("WEBM ", 1)[1].strip())
        assert webm.name == "x.webm" and webm.is_file()
        assert run_with_marker(lambda out_dir: victim) == 2
        assert "outside the output dir" in capsys.readouterr().err
        assert victim.read_bytes() == b"do not move me"


class TestConversionRecipe:
    """Pin the ffmpeg recipe constants so quality does not silently drift."""

    def test_mp4_and_gif_flags(self) -> None:
        src = RUNNER.read_text(encoding="utf-8")
        for token in ("libx264", "yuv420p", "+faststart", "palettegen", "paletteuse", "fps=12"):
            assert token in src, f"ffmpeg recipe lost {token!r}"

    def test_ffmpeg_absent_is_nonfatal(self) -> None:
        mod = _load_runner()
        assert mod._ffmpeg() is None or isinstance(mod._ffmpeg(), str)
        src = RUNNER.read_text(encoding="utf-8")
        assert "webm only" in src, "ffmpeg-absent path must still deliver the webm"


@pytest.mark.skipif(
    not (ROOT / "website" / "node_modules" / "playwright").is_dir(),
    reason="project-local playwright not installed",
)
class TestEndToEnd:
    """Real recording against a local page when the environment allows it."""

    def test_records_a_webm(self, tmp_path: Path) -> None:
        import http.server
        import threading

        page = tmp_path / "index.html"
        page.write_text("<html><body><h1 id='t'>hello</h1></body></html>")
        httpd = LoopbackHTTPServer(
            ("127.0.0.1", 0),
            lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
                *a, directory=str(tmp_path), **kw
            ),
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        # Playwright makes its profile and artifacts dirs under the child's temp dir
        # before it looks for the browser, and leaves both behind when the browser
        # build is missing. Pointing the child at this test's tmp_path keeps them out
        # of the run's temp root on the skip path too.
        child_tmp = tmp_path / "child-tmp"
        child_tmp.mkdir()
        env = {**os.environ, **dict.fromkeys(("TMPDIR", "TEMP", "TMP"), str(child_tmp))}
        try:
            out = tmp_path / "out"
            p = subprocess.run(
                [
                    sys.executable, "-X", "utf8", str(RUNNER),
                    "--url", f"http://127.0.0.1:{httpd.server_port}/",
                    "--project", str(ROOT / "website"),
                    "--out", str(out), "--name", "e2e", "--size", "640x480",
                    "--settle-ms", "200", "--tail-ms", "200",
                ],
                capture_output=True, text=True, encoding="utf-8", timeout=300, env=env,
            )
            if p.returncode != 0 and "Executable doesn't exist" in p.stderr:
                pytest.skip("playwright package present but chromium binaries not installed")
            assert p.returncode == 0, p.stderr
            assert (out / "e2e.webm").is_file()
            assert (out / "e2e.webm").stat().st_size > 0
        finally:
            httpd.shutdown()
