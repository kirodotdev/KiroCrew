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
import threading
from concurrent.futures import ThreadPoolExecutor
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


# --- Windows campaign owner simulation ---


def _simulate_windows_owner_branch(monkeypatch) -> None:
    def _descriptor_is_direct_child(parent_fd: int, child_fd: int) -> bool:
        actual_parent = mod.os.open("..", mod.dir_flags(), dir_fd=child_fd)
        try:
            expected = mod.os.fstat(parent_fd)
            actual = mod.os.fstat(actual_parent)
            return (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)
        finally:
            mod.os.close(actual_parent)

    if mod.os.name == "posix":
        monkeypatch.setattr(mod, "_descriptor_is_direct_child", _descriptor_is_direct_child)
    monkeypatch.setattr(mod.os, "name", "nt")


def test_never_created_windows_campaign_acquires_a_valid_owner(isolated: Path, monkeypatch):
    root = isolated / "research"
    root.mkdir()
    campaign_id = "a1b2c3d4"
    directory = root / campaign_id
    _simulate_windows_owner_branch(monkeypatch)

    missing = mod._campaign_identity(campaign_id)
    assert missing is not None
    try:
        assert missing._campaign_fd == -1
        assert missing._owner_key is None
    finally:
        missing.close()

    assert mod._campaign_dir(campaign_id) == directory
    assert (directory / "findings").is_dir()

    identity = mod._campaign_identity(campaign_id)
    assert identity is not None
    key = identity._owner_key
    assert identity._campaign_fd >= 0
    assert key is not None
    try:
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert mod._CAMPAIGN_DIRECTORY_HANDLES[key][1] == 1
    finally:
        identity.close()
    with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
        assert key not in mod._CAMPAIGN_DIRECTORY_HANDLES


def test_concurrent_windows_owner_acquisitions_share_one_handle(isolated: Path, monkeypatch):
    root = isolated / "research"
    directory = root / "b1c2d3e4"
    directory.mkdir(parents=True)
    root_fd = mod.pin_directory(root)
    root_stat = mod.os.fstat(root_fd)
    expected_key = mod._campaign_owner_key(root, root_stat, directory.name)
    real_pin = mod.pin_directory_for_removal
    pin_calls = 0
    pin_calls_lock = threading.Lock()
    start = threading.Barrier(2)
    acquired: list[tuple[int, mod._CampaignOwnerKey]] = []

    def _counted_pin(path: Path) -> int:
        nonlocal pin_calls
        with pin_calls_lock:
            pin_calls += 1
        return real_pin(path)

    def _acquire() -> tuple[int, mod._CampaignOwnerKey]:
        start.wait(timeout=5)
        return mod._acquire_campaign_owner(root_fd, directory, directory.name)

    _simulate_windows_owner_branch(monkeypatch)
    monkeypatch.setattr(mod, "pin_directory_for_removal", _counted_pin)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_acquire) for _ in range(2)]
            for future in futures:
                acquired.append(future.result(timeout=5))

        assert [key for _fd, key in acquired] == [expected_key, expected_key]
        assert pin_calls == 1
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert mod._CAMPAIGN_DIRECTORY_HANDLES[expected_key][1] == 2
    finally:
        for campaign_fd, key in acquired:
            mod._release_campaign_owner(key, campaign_fd)
        mod._close_fds(root_fd)
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            leftover = mod._CAMPAIGN_DIRECTORY_HANDLES.pop(expected_key, None)
            mod._CAMPAIGN_DIRECTORY_IDENTITIES.pop(expected_key, None)
        if leftover is not None:
            mod._close_fds(leftover[0])
    with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
        assert expected_key not in mod._CAMPAIGN_DIRECTORY_HANDLES


def test_failed_windows_owner_duplication_leaves_no_registry_or_descriptor(
    isolated: Path, monkeypatch
):
    root = isolated / "research"
    directory = root / "c1d2e3f4"
    directory.mkdir(parents=True)
    root_fd = mod.pin_directory(root)
    root_stat = mod.os.fstat(root_fd)
    key = mod._campaign_owner_key(root, root_stat, directory.name)
    real_pin = mod.pin_directory_for_removal
    opened: list[int] = []

    def _recording_pin(path: Path) -> int:
        fd = real_pin(path)
        opened.append(fd)
        return fd

    def _refuse_dup(_fd: int) -> int:
        raise OSError("duplication refused")

    _simulate_windows_owner_branch(monkeypatch)
    monkeypatch.setattr(mod, "pin_directory_for_removal", _recording_pin)
    monkeypatch.setattr(mod.os, "dup", _refuse_dup)
    try:
        with pytest.raises(OSError, match="duplication refused"):
            mod._acquire_campaign_owner(root_fd, directory, directory.name)
        assert len(opened) == 1
        with pytest.raises(OSError):
            mod.os.fstat(opened[0])
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert key not in mod._CAMPAIGN_DIRECTORY_HANDLES
            assert key not in mod._CAMPAIGN_DIRECTORY_IDENTITIES
    finally:
        mod._close_fds(root_fd)
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            leftover = mod._CAMPAIGN_DIRECTORY_HANDLES.pop(key, None)
            mod._CAMPAIGN_DIRECTORY_IDENTITIES.pop(key, None)
        if leftover is not None:
            mod._close_fds(leftover[0])


def test_registry_keys_scope_reused_inode_identity_to_the_resolved_root(
    isolated: Path,
):
    old_root = isolated / "old-research"
    current_root = isolated / "current-research"
    old_root.mkdir()
    current_root.mkdir()
    shared_stat = old_root.stat()
    stale_key = mod._campaign_owner_key(old_root, shared_stat, "d1e2f3a4")
    active_key = mod._campaign_owner_key(old_root, shared_stat, "e1f2a3b4")
    current_key = mod._campaign_owner_key(current_root, shared_stat, "d1e2f3a4")
    owner_file = isolated / "owner-handle"
    owner_fd = mod.os.open(owner_file, mod.os.O_RDWR | mod.os.O_CREAT, 0o600)

    assert stale_key != current_key
    with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
        mod._CAMPAIGN_DIRECTORY_IDENTITIES[stale_key] = (11, 12)
        mod._CAMPAIGN_DIRECTORY_IDENTITIES[active_key] = (21, 22)
        mod._CAMPAIGN_DIRECTORY_HANDLES[active_key] = (owner_fd, 1)

    try:
        mod._retire_inactive_campaign_roots(current_root)
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert stale_key not in mod._CAMPAIGN_DIRECTORY_IDENTITIES
            assert active_key in mod._CAMPAIGN_DIRECTORY_IDENTITIES

        mod._release_campaign_owner(active_key)
        # The last reference closes the owner handle itself, inside the lock.
        with pytest.raises(OSError):
            mod.os.fstat(owner_fd)
        owner_fd = -1
        mod._retire_inactive_campaign_roots(current_root)
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert active_key not in mod._CAMPAIGN_DIRECTORY_IDENTITIES
    finally:
        mod._close_fds(owner_fd)
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            leftover = mod._CAMPAIGN_DIRECTORY_HANDLES.pop(active_key, None)
            mod._CAMPAIGN_DIRECTORY_IDENTITIES.pop(stale_key, None)
            mod._CAMPAIGN_DIRECTORY_IDENTITIES.pop(active_key, None)
        if leftover is not None:
            mod._close_fds(leftover[0])


def test_retiring_owner_closes_every_descriptor_inside_the_registry_lock(
    isolated: Path, monkeypatch
):
    """Windows share access belongs to the file object, so a duplicate of the owner
    handle that is still open refuses a fresh DELETE-access open by name. The
    registry must therefore never say "no owner" while any such handle lives:
    every close for the retiring reference happens under the lock, or an acquirer
    racing the release reopens by name and gets a sharing violation -- the Windows
    shard read a reachable campaign as UNKNOWN (``get_findings() == []``) exactly so.
    Pinned on the last-reference retire, on the shared-reference decrement, and on
    the POSIX ``None`` key which has no registry to guard."""
    campaign_id = "a9b8c7d6"
    _simulate_windows_owner_branch(monkeypatch)
    campaign_dir = mod._campaign_dir(campaign_id)
    first = mod._campaign_identity(campaign_id)
    second = mod._campaign_identity(campaign_id)
    assert first is not None and second is not None
    key = first._owner_key
    assert key is not None and second._owner_key == key
    with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
        owner_fd = mod._CAMPAIGN_DIRECTORY_HANDLES[key][0]
        assert mod._CAMPAIGN_DIRECTORY_HANDLES[key][1] == 2

    closes: list[tuple[tuple[int, ...], bool, bool]] = []
    real_close = mod._close_fds

    def _observed_close(*fds: int) -> None:
        closes.append(
            (
                tuple(fd for fd in fds if fd >= 0),
                mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK.locked(),
                key in mod._CAMPAIGN_DIRECTORY_HANDLES,
            )
        )
        real_close(*fds)

    monkeypatch.setattr(mod, "_close_fds", _observed_close)
    try:
        first_fds = (first._campaign_fd, first._root_fd)
        first.close()
        # A shared reference: its duplicates close under the lock while the owner
        # stays registered for the surviving identity.
        assert closes == [(first_fds, True, True)]
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert mod._CAMPAIGN_DIRECTORY_HANDLES[key] == (owner_fd, 1)

        second_fds = (second._campaign_fd, second._root_fd, owner_fd)
        second.close()
        # The last reference: the owner handle itself is in the same close, still
        # under the lock, and the registry entry is already gone when it runs --
        # so no acquirer can find the entry missing before the handle is closed.
        assert closes[1] == (second_fds, True, False)
        with pytest.raises(OSError):
            mod.os.fstat(owner_fd)
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert key not in mod._CAMPAIGN_DIRECTORY_HANDLES

        # POSIX identities carry no key: their close is a plain close, no lock.
        posix_fd = mod.os.open(isolated, mod.os.O_RDONLY)
        mod._release_campaign_owner(None, posix_fd)
        assert closes[2] == ((posix_fd,), False, False)
    finally:
        monkeypatch.setattr(mod, "_close_fds", real_close)
        first.close()
        second.close()
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            leftover = mod._CAMPAIGN_DIRECTORY_HANDLES.pop(key, None)
        if leftover is not None:
            real_close(leftover[0])

    (campaign_dir / "findings").rmdir()
    campaign_dir.rmdir()
    with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
        mod._CAMPAIGN_DIRECTORY_IDENTITIES.pop(key, None)
    assert not campaign_dir.exists()


def test_windows_owner_finalizer_never_reenters_the_registry_lock(isolated: Path, monkeypatch):
    campaign_id = "f1a2b3c4"
    _simulate_windows_owner_branch(monkeypatch)
    mod._campaign_dir(campaign_id)
    identity = mod._campaign_identity(campaign_id)
    assert identity is not None
    key = identity._owner_key
    assert key is not None
    campaign_fd = identity._campaign_fd
    root_fd = identity._root_fd
    release_entered = threading.Event()
    allow_release = threading.Event()
    finalizer_returned = threading.Event()
    real_release = mod._release_campaign_owner

    def _blocked_release(owner_key, *fds):
        release_entered.set()
        assert allow_release.wait(5), "test never released owner cleanup"
        return real_release(owner_key, *fds)

    def _run_finalizer() -> None:
        identity.__del__()
        finalizer_returned.set()

    monkeypatch.setattr(mod, "_release_campaign_owner", _blocked_release)
    invoker = threading.Thread(target=_run_finalizer, daemon=True)
    invoker.start()
    try:
        assert release_entered.wait(2)
        assert finalizer_returned.wait(
            2
        ), "__del__ waited on the campaign owner registry instead of deferring release"
    finally:
        allow_release.set()
        invoker.join(timeout=5)

    for _ in range(200):
        try:
            mod.os.fstat(campaign_fd)
        except OSError:
            break
        threading.Event().wait(0.01)
    else:
        pytest.fail("finalizer worker did not close the campaign descriptor")
    with pytest.raises(OSError):
        mod.os.fstat(root_fd)
    with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
        assert key not in mod._CAMPAIGN_DIRECTORY_HANDLES


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


def test_non_ascii_finding_is_not_read_as_absent(isolated: Path):
    """Valid UTF-8 stays visible through the owned-path security gate.

    The reader now accepts only campaign-owned cycle files. Exercise that real
    shape under a non-ASCII root as well as a non-ASCII JSON payload so Windows
    path and locale behavior are both covered without weakening containment.
    """
    unicode_root = isolated / f"research-{NON_ASCII}"
    with patch.object(mod, "RESEARCH_DIR", unicode_root):
        cid = _new_campaign()
        p = unicode_root / cid / "findings" / "cycle_001.json"
        p.write_text(
            json.dumps(
                {"cycle": 1, "summary": NON_ASCII, "new_findings_count": 3},
                ensure_ascii=False,
            ),
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

    with patch.object(mod, "_remove_campaign_tree", return_value=["in use"]):
        result = mod.delete_campaign(cid)
    assert result == {"error": "cleanup incomplete", "residual": True}
    assert mod.get_campaign(cid) is not None


def test_delete_campaign_reports_no_residual_on_a_clean_removal(isolated: Path):
    cid = _new_campaign()
    result = mod.delete_campaign(cid)
    assert result == {"id": cid, "deleted": True, "residual": False}
    assert not (isolated / "research" / cid).exists()
    assert mod.get_campaign(cid) is None


@pytest.mark.skipif(mod.os.name != "nt", reason="Windows share-delete semantics")
def test_final_campaign_removal_stays_bound_to_the_validated_handle(isolated: Path):
    cid = _new_campaign()
    target = isolated / "research" / cid
    replacement = isolated / "replacement"
    replacement.mkdir()
    marker = replacement / "keep.txt"
    marker.write_text("replacement survives", encoding="utf-8")
    attempts: list[str] = []
    real_remove = mod.remove_pinned_directory

    def _attempt_substitution(fd: int) -> None:
        try:
            target.rename(isolated / "parked-owned")
        except OSError:
            attempts.append("refused")
        else:  # pragma: no cover - mutation control below proves this old failure mode
            attempts.append("substituted")
            replacement.rename(target)
        real_remove(fd)

    with patch.object(mod, "remove_pinned_directory", side_effect=_attempt_substitution):
        result = mod.delete_campaign(cid)

    assert attempts == ["refused"]
    assert result == {"id": cid, "deleted": True, "residual": False}
    assert not target.exists()
    assert marker.read_text(encoding="utf-8") == "replacement survives"


@pytest.mark.skipif(mod.os.name != "nt", reason="Windows share-delete semantics")
def test_concurrent_campaign_identities_share_one_removal_pin(isolated: Path):
    cid = _new_campaign()
    first = mod._campaign_identity(cid)
    second = mod._campaign_identity(cid)
    assert first is not None
    assert second is not None
    key = first._owner_key
    assert key is not None
    assert second._owner_key == key
    try:
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert mod._CAMPAIGN_DIRECTORY_HANDLES[key][1] == 2
        first.close()
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert mod._CAMPAIGN_DIRECTORY_HANDLES[key][1] == 1
        second.close()
        with mod._CAMPAIGN_DIRECTORY_IDENTITIES_LOCK:
            assert key not in mod._CAMPAIGN_DIRECTORY_HANDLES
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(mod.os.name != "nt", reason="Windows share-delete semantics")
def test_MUTATION_closing_the_pin_before_path_rmdir_removes_a_replacement(isolated: Path):
    cid = _new_campaign()
    target = isolated / "research" / cid
    identity = mod._campaign_identity(cid)
    assert identity is not None
    failures: list[str] = []
    mod._remove_campaign_contents_path(target, failures)
    assert failures == []
    parked = isolated / "parked-owned"
    replacement = isolated / "replacement"
    replacement.mkdir()

    try:
        owner_key = identity._owner_key
        identity._owner_key = None
        campaign_fd = identity._campaign_fd
        identity._campaign_fd = -1
        mod._release_campaign_owner(owner_key, campaign_fd)
        final_check = mod.pin_directory(target)
        mod.os.close(final_check)
        target.rename(parked)
        replacement.rename(target)
        target.rmdir()
    finally:
        identity.close()

    assert parked.is_dir()
    assert not target.exists(), "the old path rmdir unexpectedly preserved the replacement"


def test_delete_campaign_retried_after_cleanup_succeeds(isolated: Path):
    """The row survives a failed cleanup, so retrying the same id later -- once
    whatever held the file open has let go -- completes the delete for real.
    """
    cid = _new_campaign()

    with patch.object(mod, "_remove_campaign_tree", return_value=["in use"]):
        first = mod.delete_campaign(cid)
    assert first["error"] == "cleanup incomplete"

    second = mod.delete_campaign(cid)
    assert second == {"id": cid, "deleted": True, "residual": False}
    assert mod.get_campaign(cid) is None
