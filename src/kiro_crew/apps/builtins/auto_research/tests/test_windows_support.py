"""Windows-support contract for the ``auto-research`` builtin.

Two things are pinned here:

1. The manifest's ``platform.os`` declaration -- a published capability label,
   not an enable gate. ``apps/routes.py`` only calls ``supports_platform`` for
   a ``platform.installMode == "client"`` app, which no builtin sets, so this
   value never blocked (or would have blocked) the app from being enabled on
   Windows; declaring it truthfully is still worth doing for the label users
   read on the App Store detail page.
2. That every text read/write in the app pins ``encoding="utf-8"``. This app's
   payloads are LLM prose and user questions — em dashes, curly quotes, CJK — and
   a bare ``Path.read_text()`` / ``write_text()`` uses the process locale
   encoding, which on a zh-CN Windows host is cp936. That produced no clean
   error but four distinct failure shapes: reports that could never be written,
   500s on the report/findings endpoints, and (worst) findings that decoded to
   nothing so the watchdog failed a healthy campaign as stalled. The AST scan is
   the regression gate — a functional round-trip alone passes on Linux whether or
   not the encoding is pinned.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.apps.builtins.auto_research import handlers as mod
from kiro_crew.apps.builtins.auto_research import subquestion_queue as sq

APP_ROOT = Path(mod.__file__).resolve().parent
APP_JSON = APP_ROOT / "app.json"

DECLARED_OS = ["macos", "linux", "windows"]

# Text that breaks on cp936 / cp1252 encode-or-decode, i.e. exactly what an
# agent-written FINDINGS.md contains.
NON_ASCII = "研究结论 — “Ω” café ✅"


@pytest.fixture
def isolated(tmp_path: Path):
    """Isolate the sqlite DB and the research dir, as the top-level suite does."""
    with (
        patch.object(mod, "DB_PATH", tmp_path / "test.db"),
        patch.object(mod, "RESEARCH_DIR", tmp_path / "research"),
    ):
        yield tmp_path


def _new_campaign() -> str:
    return mod.create_campaign(
        {"question": f"A sufficiently long research question about {NON_ASCII}", "sources": ["web"]}
    )["id"]


# --- manifest platform declaration ---


def test_manifest_still_validates_with_the_platform_block():
    """The typed loader must accept the added block.

    A malformed ``platform`` section does not raise at install time — discovery
    silently DROPS an app whose manifest fails validation, so the app would just
    stop appearing anywhere.
    """
    from kiro_crew.apps.discovery import discover_builtin_apps
    from kiro_crew.apps.manifest import AppManifest

    manifest = AppManifest.from_json_file(APP_JSON)
    assert manifest.validate(app_root=APP_ROOT) == []
    assert manifest.name == "auto-research"
    assert "auto-research" in [a.get("name") for a in discover_builtin_apps()]


# --- the encoding regression gate ---


def _app_source_files() -> list[Path]:
    return sorted(
        p
        for p in APP_ROOT.rglob("*.py")
        if "tests" not in p.relative_to(APP_ROOT).parts and "__pycache__" not in p.parts
    )


def test_every_text_io_call_pins_utf8():
    """No ``read_text``/``write_text`` in this app may rely on the locale encoding.

    Checked on the AST, not with a regex: the calls span several lines, and
    ``workflow_template.py`` carries Python source inside a string literal that
    must NOT be scanned as if it were code.
    """
    offenders: list[str] = []
    for path in _app_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("read_text", "write_text"):
                continue
            if not any(kw.arg == "encoding" for kw in node.keywords):
                offenders.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}")
    assert not offenders, (
        "these text I/O calls fall back to the process locale encoding (cp936 on a "
        f"zh-CN Windows host): {offenders}"
    )


# --- functional round trips over the fixed call sites ---


def test_write_then_read_report_round_trips_non_ascii(tmp_path: Path):
    p = tmp_path / "FINDINGS.md"
    mod._write_text(p, NON_ASCII)
    assert p.read_bytes() == NON_ASCII.encode("utf-8")
    assert mod._read_text_or_missing(p) == NON_ASCII


def test_read_text_or_missing_absorbs_bad_bytes_instead_of_raising(tmp_path: Path):
    """A partially corrupt report must degrade, not 500 the export endpoint."""
    p = tmp_path / "FINDINGS.md"
    p.write_bytes(b"ok \xff\xfe tail")
    out = mod._read_text_or_missing(p)
    assert out is not None and "ok" in out and "tail" in out


def test_non_ascii_finding_is_not_read_as_absent(tmp_path: Path):
    """The watchdog's false-stall bug: a valid UTF-8 finding read as ``{}``.

    ``_read_finding_file`` swallows UnicodeDecodeError by design, so under cp936
    a healthy campaign's findings vanished silently and the stall verdict failed
    it. Pinning UTF-8 is what makes the finding visible.
    """
    p = tmp_path / "cycle_001.json"
    p.write_text(
        json.dumps({"cycle": 1, "summary": NON_ASCII, "new_findings_count": 3}),
        encoding="utf-8",
    )
    data = mod._read_finding_file(p)
    assert data.get("cycle") == 1


def test_truly_corrupt_finding_still_reads_as_absent(tmp_path: Path):
    """``_read_finding_file`` stays strict on purpose — no ``errors="replace"``.

    It feeds the stall verdict, so undecodable bytes must remain "absent" rather
    than becoming mojibake that parses as a finding.
    """
    p = tmp_path / "cycle_001.json"
    p.write_bytes(b"\xff\xfe invalid utf8 \x80")
    assert mod._read_finding_file(p) == {}


def test_check_stagnation_sees_progress_in_non_ascii_findings(isolated: Path):
    """check_stagnation reads the raw cycle JSON; a decode failure there returned
    ``True`` (stalled) for a campaign that was in fact producing findings."""
    cid = _new_campaign()
    findings = isolated / "research" / cid / "findings"
    findings.mkdir(parents=True, exist_ok=True)
    for i in range(1, 6):
        (findings / f"cycle_{i:03d}.json").write_text(
            json.dumps({"cycle": i, "summary": NON_ASCII, "new_findings_count": 2}),
            encoding="utf-8",
        )
    assert mod.check_stagnation(cid) is False


def test_get_findings_and_report_serve_non_ascii(isolated: Path):
    cid = _new_campaign()
    d = isolated / "research" / cid
    (d / "findings").mkdir(parents=True, exist_ok=True)
    (d / "findings" / "cycle_001.json").write_text(
        json.dumps({"cycle": 1, "summary": NON_ASCII}), encoding="utf-8"
    )
    (d / "FINDINGS.md").write_text(f"# {NON_ASCII}\n", encoding="utf-8")

    assert [f["cycle"] for f in mod.get_findings(cid)] == [1]
    assert NON_ASCII in mod._read_report(cid)


def test_guidance_and_pending_question_round_trip_non_ascii(isolated: Path):
    """Both sides of the attended-mode conversation are user/agent prose."""
    cid = _new_campaign()
    mod.write_guidance(cid, NON_ASCII)
    d = isolated / "research" / cid
    assert (d / "guidance.txt").read_text(encoding="utf-8") == NON_ASCII

    (d / "questions.json").write_text(
        json.dumps({"question": NON_ASCII}, ensure_ascii=False), encoding="utf-8"
    )
    assert mod._pending_question(cid) == NON_ASCII


def test_fork_copies_non_ascii_parent_findings(tmp_path: Path):
    src = tmp_path / "parent" / "FINDINGS.md"
    src.parent.mkdir(parents=True)
    src.write_text(NON_ASCII, encoding="utf-8")
    dst = tmp_path / "child" / "FINDINGS.md"
    mod._copy_parent_findings(src, dst)
    assert dst.read_text(encoding="utf-8") == NON_ASCII


def test_status_file_is_utf8_encoded(isolated: Path):
    cid = _new_campaign()
    raw = (isolated / "research" / cid / "status.json").read_bytes()
    assert json.loads(raw.decode("utf-8"))["campaign_id"] == cid


def test_subquestion_queue_round_trips_non_ascii(tmp_path: Path):
    queue = sq.new_queue()
    queue["pending"].append({"text": NON_ASCII, "depth": 1})
    sq.save_queue(tmp_path, queue)
    assert sq.load_queue(tmp_path)["pending"][0]["text"] == NON_ASCII


def test_queue_written_as_utf8_by_a_worker_still_loads(tmp_path: Path):
    """The queue file sits in the agent-writable campaign dir, so a worker may
    rewrite it as UTF-8 with real non-ASCII bytes rather than \\u escapes."""
    (tmp_path / sq.QUEUE_FILENAME).write_text(
        json.dumps({"pending": [{"text": NON_ASCII}], "analyzed": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert sq.load_queue(tmp_path)["pending"][0]["text"] == NON_ASCII


# --- delete_campaign residual reporting ---


def test_delete_campaign_keeps_the_row_when_a_path_cannot_be_removed(isolated: Path):
    """Windows refuses to unlink a file another process holds open, so the tree
    removal can fail halfway. The row is kept (not deleted) so a retried
    delete of the same id tries cleanup again instead of returning
    "campaign not found" against an already-vanished row.
    """
    cid = _new_campaign()

    def _failing_rmtree(path, onexc=None, **_kw):
        onexc(None, str(path), OSError("in use"))

    with patch.object(mod.shutil, "rmtree", _failing_rmtree):
        result = mod.delete_campaign(cid)
    assert result == {"error": "cleanup incomplete", "residual": True}
    assert mod.get_campaign(cid) is not None


def test_delete_campaign_reports_no_residual_on_a_clean_removal(isolated: Path):
    cid = _new_campaign()
    result = mod.delete_campaign(cid)
    assert result == {"id": cid, "deleted": True, "residual": False}
    assert not (isolated / "research" / cid).exists()
    assert mod.get_campaign(cid) is None


def test_delete_campaign_retried_after_cleanup_succeeds(isolated: Path):
    """The row survives a failed cleanup, so retrying the same id later -- once
    whatever held the file open has let go -- completes the delete for real.
    """
    cid = _new_campaign()

    def _failing_rmtree(path, onexc=None, **_kw):
        onexc(None, str(path), OSError("in use"))

    with patch.object(mod.shutil, "rmtree", _failing_rmtree):
        first = mod.delete_campaign(cid)
    assert first["error"] == "cleanup incomplete"

    second = mod.delete_campaign(cid)
    assert second == {"id": cid, "deleted": True, "residual": False}
    assert mod.get_campaign(cid) is None
