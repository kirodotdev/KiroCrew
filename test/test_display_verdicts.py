"""Stored display-redaction verdicts (``dashboard.display_verdicts``).

A verdict stored beside a transcript lets a render after a restart skip the
battery. Every test here is about the two ways that could go wrong: a stored
verdict served after the rules changed, and a stored verdict the reader did not
produce. Both must fall back to scanning.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.dashboard import chat_utils, display_battery
from kiro_crew.dashboard import display_verdicts as dv
from kiro_crew.dashboard.chat_utils import _prepare_messages

KEY = "dashboard_chat-1-1700000000"
#: Storage is off on a platform with no uid to prove the directory is owned
#: (``test_storage_stays_off_without_a_uid`` covers that branch on POSIX).
pytestmark = pytest.mark.skipif(os.name == "nt", reason="stored verdicts are POSIX-only")
SECRET = "AKIAIOSFODNN7EXAMPLE"
HOST = "docs.contoso.example"
#: Clean only while HOST is exempt: its query trips the aggregate-length heuristic.
LINK = f"https://{HOST}/view?" + "&".join(f"p{i}=value{i}" for i in range(30))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "sessions").mkdir(parents=True)
    (h / "sessions" / f"{KEY}.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    monkeypatch.setattr(dv, "rules_version", lambda: b"v" * 16)
    # Read the sidecar on the first miss, so small fixtures exercise it.
    monkeypatch.setattr(dv, "LOAD_AFTER_MISSES", 0)
    dv.disable_for_tests()
    chat_utils._clear_display_redaction_cache()
    yield h
    dv.disable_for_tests()
    with dv._written_guard:
        dv._written.clear()
    chat_utils._clear_display_redaction_cache()


@pytest.fixture
def battery(monkeypatch):
    """Count battery calls (one per string the battery actually scans)."""
    calls = {"n": 0}
    real = display_battery.redact_exfiltration_urls

    def counted(text, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return real(text, **kwargs)

    monkeypatch.setattr(display_battery, "redact_exfiltration_urls", counted)
    return calls


@pytest.fixture
def allowed(monkeypatch):
    """The workspace's allowed link hosts, as the reader set them."""
    hosts: set[str] = set()
    monkeypatch.setattr(chat_utils, "allowed_hosts_for", lambda _ws: frozenset(hosts))
    return hosts


def _rows(n: int = 40) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": f"reply {i} " + (f"leaks {SECRET}" if i % 10 == 0 else f"link {LINK}"),
            "cls": "msg msg-a",
            "ts": f"2026-09-23T22:00:{i:02d}Z",
            "meta": {"mid": f"mid-{i}", "tool_name": f"tool-{i}"},
        }
        for i in range(n)
    ]


def _render(rows: list[dict]) -> str:
    with dv.session(KEY):
        out = _prepare_messages(rows, running=False, live_child="", workspace="ws")
    return json.dumps(out)


def _restart(home: Path) -> None:
    """A new gateway process on the same data home: process caches gone, same key file."""
    dv.disable_for_tests()
    chat_utils._clear_display_redaction_cache()
    assert dv.enable(home)


def test_a_restart_renders_from_the_stored_verdicts(home, battery, allowed):
    assert dv.enable(home)
    rows = _rows()
    cold = _render(rows)
    assert battery["n"] >= len(rows)
    assert SECRET not in cold
    _restart(home)
    before = battery["n"]
    warm = _render(rows)
    assert warm == cold
    assert battery["n"] == before, "every string was judged before the restart"


def test_a_changed_rules_version_invalidates_every_stored_verdict(
    home, battery, allowed, monkeypatch
):
    """A code change moves the rules version, and nothing judged under the old one is served.

    The new rules redact a word the old ones let through. Were the version not
    part of the tag, the stored clean verdict would show the word in plaintext.
    """
    assert dv.enable(home)
    rows = [
        {
            "role": "assistant",
            "content": "plain words with codeword-4471 inside",
            "cls": "msg msg-a",
        }
    ]
    assert "codeword-4471" in _render(rows)

    real = display_battery.redact_credentials
    monkeypatch.setattr(
        display_battery,
        "redact_credentials",
        lambda t: real(t.replace("codeword-4471", "[REDACTED]")),
    )
    monkeypatch.setattr(dv, "rules_version", lambda: b"w" * 16)
    _restart(home)
    before = battery["n"]
    out = _render(rows)
    assert "codeword-4471" not in out
    assert battery["n"] > before, "the old verdict was not used"


def _sidecar_size(home: Path) -> int:
    d = home / "display-verdicts"
    return sum(f.stat().st_size for f in d.iterdir()) if d.is_dir() else 0


def test_restarts_do_not_rescan_or_regrow_the_sidecar_on_save(home, battery, allowed):
    """The first save after a restart offers the whole window; judged rows stay judged.

    Without reading the stored set first, that save re-ran the battery over every
    row and appended every tag again, so the file grew by the window's size on each
    restart until the size cap emptied it.
    """
    assert dv.enable(home)
    lines = [json.dumps(r) for r in _rows(200)]

    def save() -> int:
        before = battery["n"]
        dv.note_written_lines(KEY, lines)
        dv.submit_committed("ws")
        dv.drain_for_tests()
        return battery["n"] - before

    assert save() > 0
    size = _sidecar_size(home)
    assert size > 0
    for _ in range(5):
        _restart(home)
        with dv._written_guard:
            dv._written.clear()
        assert save() == 0, "rows judged before the restart were scanned again"
        assert _sidecar_size(home) == size


def test_a_directory_swapped_after_the_check_does_not_redirect_the_sweep(
    home, tmp_path, monkeypatch
):
    """The boot sweep deletes; it must act on the directory it checked.

    Here the cache directory is replaced by a symlink to another directory the
    moment the sweep starts listing. A path-based sweep follows the link and
    deletes the other directory's ``.dvc`` files; a descriptor-based one does not.
    """
    root = home / dv.DIR_NAME
    root.mkdir(mode=0o700)
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    (victim / "tracked.dvc").write_text("keep me", encoding="utf-8")
    real_scandir = os.scandir
    swapped = {"done": False}

    def swapping_scandir(target):  # type: ignore[no-untyped-def]
        if not swapped["done"]:
            swapped["done"] = True
            root.rename(tmp_path / "moved-aside")
            root.symlink_to(victim, target_is_directory=True)
        return real_scandir(target)

    monkeypatch.setattr(dv.os, "scandir", swapping_scandir)
    dv.enable(home)
    assert swapped["done"]
    assert (victim / "tracked.dvc").read_text(encoding="utf-8") == "keep me"


def test_revoking_a_token_param_prefix_invalidates_its_stored_verdicts(
    home, battery, allowed, monkeypatch
):
    """The companion ``?token=`` prefix set changes the battery's output, so it is in every tag.

    The battery here leaves a word alone only while a prefix is exempt, the way
    pass 4 leaves an exempt URL's token. Were the prefix set not part of the tag,
    the verdict stored while it was exempt would show the word after revocation.
    """
    assert dv.enable(home)
    prefixes: list[str] = ["https://tenant.contoso.example/share"]
    monkeypatch.setattr(chat_utils, "_token_param_exempt_prefixes", lambda: tuple(prefixes))
    real = display_battery.redact_credentials
    monkeypatch.setattr(
        display_battery,
        "redact_credentials",
        lambda t: real(t if prefixes else t.replace("codeword-5823", "[REDACTED]")),
    )
    rows = [{"role": "assistant", "content": "token codeword-5823 here", "cls": "msg msg-a"}]
    assert "codeword-5823" in _render(rows)
    prefixes.clear()
    _restart(home)
    out = _render(rows)
    assert "codeword-5823" not in out


def test_revoking_an_allowed_host_invalidates_its_stored_verdicts(home, battery, allowed):
    """A user-defined rule (a workspace's allowed link host) is part of every tag."""
    assert dv.enable(home)
    allowed.add(HOST)
    rows = [{"role": "assistant", "content": f"see {LINK}", "cls": "msg msg-a"}]
    assert LINK in _render(rows), "an allowed host's link is shown"
    allowed.clear()
    _restart(home)
    out = _render(rows)
    assert LINK not in out
    assert "[REDACTED: suspicious URL" in out


def test_a_platform_exempt_host_change_invalidates_its_stored_verdicts(
    home, battery, allowed, monkeypatch
):
    assert dv.enable(home)
    monkeypatch.setattr(chat_utils, "_exempt_exact_hosts", lambda: frozenset({HOST}))
    rows = [{"role": "assistant", "content": f"see {LINK}", "cls": "msg msg-a"}]
    with dv.session(KEY):
        from kiro_crew.security import exfil

        monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: frozenset({HOST}))
        assert LINK in json.dumps(_prepare_messages(rows, running=False, live_child=""))
    monkeypatch.setattr(chat_utils, "_exempt_exact_hosts", lambda: frozenset())
    monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: frozenset())
    _restart(home)
    assert LINK not in _render(rows)


def test_forged_sidecar_entries_are_never_served(home, battery, allowed):
    """The sidecar is agent-writable; only tags under the gateway's key count."""
    assert dv.enable(home)
    rows = [{"role": "assistant", "content": f"leaks {SECRET}", "cls": "msg msg-a"}]
    root = home / dv.DIR_NAME
    p = dv._persist
    assert p is not None
    # A "clean" tag computed WITHOUT the key, and a redacted record whose output
    # is the raw text but whose MAC is wrong.
    import hashlib

    raw = rows[0]["content"].encode()
    forged = hashlib.sha256(b"tag\0" + p.version + raw).digest()[:16]
    (root / f"{KEY}.dvc").write_bytes(dv._MAGIC + p.version + forged + bytes(range(256)) * 4)
    real_tag = chat_utils._display_redaction_cache_key(rows[0]["content"])[0][0][:16]
    bad = real_tag + len(raw).to_bytes(4, "big") + raw + b"\0" * 16
    (root / f"{KEY}.dvr").write_bytes(dv._MAGIC + p.version + bad)
    _restart(home)
    out = _render(rows)
    assert SECRET not in out
    assert battery["n"] >= 1


def test_a_torn_or_garbage_sidecar_reads_as_absent(home, battery, allowed):
    assert dv.enable(home)
    rows = _rows(5)
    cold = _render(rows)
    root = home / dv.DIR_NAME
    for name in (f"{KEY}.dvc", f"{KEY}.dvr"):
        path = root / name
        if path.exists():
            path.write_bytes(path.read_bytes()[:-7])
    (root / "x.dvc").write_bytes(b"garbage")
    _restart(home)
    assert _render(rows) == cold
    assert SECRET not in cold


def test_a_symlinked_sidecar_is_not_followed(home, battery, allowed, tmp_path):
    assert dv.enable(home)
    target = tmp_path / "elsewhere"
    target.write_bytes(b"keep")
    root = home / dv.DIR_NAME
    os.symlink(target, root / f"{KEY}.dvc")
    _render(_rows(3))
    assert target.read_bytes() == b"keep"


def test_a_hard_linked_sidecar_never_truncates_the_other_file(home, battery, allowed, tmp_path):
    """O_NOFOLLOW does not stop a hard link: a sidecar name linked to a transcript stays intact."""
    assert dv.enable(home)
    transcript = home / "victim.jsonl"
    original = b'{"role": "user", "content": "keep me"}\n' * 50
    transcript.write_bytes(original)
    root = home / dv.DIR_NAME
    os.link(transcript, root / f"{KEY}.dvc")
    os.link(transcript, root / f"{KEY}.dvr")
    _render(_rows(3))
    _restart(home)
    _render(_rows(3))
    assert transcript.read_bytes() == original
    p = dv._persist
    assert p is not None
    dir_fd = dv._open_dir(p)
    assert dir_fd is not None
    try:
        assert dv._read_file(dir_fd, f"{KEY}.dvc") is None
    finally:
        os.close(dir_fd)


def test_the_owner_view_bypass_neither_reads_nor_writes(home, battery, allowed):
    from kiro_crew.security import redaction_switch

    assert dv.enable(home)
    redaction_switch.set_enabled(False, changed_at="2026-10-12T00:00:00Z")
    with redaction_switch.owner_view():
        assert redaction_switch.credential_pass_bypassed()
        assert dv.mac_base() is None
        with dv.session(KEY):
            assert dv.active()[0] is None


def test_a_text_ending_in_a_revoked_host_does_not_reuse_a_stored_verdict(home, battery, allowed):
    """``T`` under hosts {H, Z} and ``T + NUL + H`` under {Z} feed the same bytes to the MAC.

    Only the text's length tells them apart, so it is part of the stored tag.
    """
    other = "zz.example"
    assert sorted([HOST, other]) == [HOST, other], "the revoked host must sort first"
    assert dv.enable(home)
    text = f"see {LINK}"
    allowed.update({HOST, other})
    assert LINK in _render([{"role": "assistant", "content": text, "cls": "msg msg-a"}])
    allowed.discard(HOST)
    _restart(home)
    crafted = [{"role": "assistant", "content": text + "\0" + HOST, "cls": "msg msg-a"}]
    out = _render(crafted)
    assert LINK not in out
    assert "[REDACTED: suspicious URL" in out


def test_a_redacted_tag_copied_into_the_clean_file_is_not_a_clean_verdict(home, battery, allowed):
    """Both files are agent-writable; a tag from ``.dvr`` must not name a clean verdict."""
    assert dv.enable(home)
    rows = [{"role": "assistant", "content": f"leaks {SECRET}", "cls": "msg msg-a"}]
    assert SECRET not in _render(rows)
    root = home / dv.DIR_NAME
    p = dv._persist
    assert p is not None
    red = (root / f"{KEY}.dvr").read_bytes()
    copied = red[dv._HEADER : dv._HEADER + dv._TAG]
    (root / f"{KEY}.dvc").write_bytes(dv._MAGIC + p.version + copied)
    _restart(home)
    before = battery["n"]
    out = _render(rows)
    assert SECRET not in out
    assert battery["n"] == before, "served from the redacted record, not as clean"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_fifo_planted_as_a_sidecar_does_not_block_the_read(home, battery, allowed):
    """No writer ever opens the FIFO; without O_NONBLOCK this read never returns."""
    assert dv.enable(home)
    root = home / dv.DIR_NAME
    os.mkfifo(root / f"{KEY}.dvc")
    p = dv._persist
    assert p is not None
    dir_fd = dv._open_dir(p)
    assert dir_fd is not None
    try:
        assert dv._read_file(dir_fd, f"{KEY}.dvc") is None
    finally:
        os.close(dir_fd)
    assert SECRET not in _render(_rows(3))


def test_a_batch_past_the_queue_budget_is_left_to_the_read(home, battery, allowed, monkeypatch):
    assert dv.enable(home)
    monkeypatch.setattr(dv, "_QUEUE_MAX_CHARS", 100)
    rows = _rows(5)
    lines = [json.dumps(r) + "\n" for r in rows]
    assert sum(map(len, lines)) > 100
    dv.note_written_lines(KEY, lines)
    dv.submit_committed("ws")
    dv.drain_for_tests()
    assert dv._queued_chars == 0
    assert not (home / dv.DIR_NAME / f"{KEY}.dvc").exists(), "nothing was projected"
    _restart(home)
    before = battery["n"]
    assert SECRET not in _render(rows)
    assert battery["n"] > before, "the rows were scanned on read instead"


def test_storage_stays_off_without_a_uid(home, monkeypatch):
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "effective_uid", lambda: None)
    assert not dv.enable(home)
    assert not (home / dv.DIR_NAME).exists()


def test_rows_judged_at_write_render_without_a_scan_after_a_restart(home, battery, allowed):
    assert dv.enable(home)
    rows = _rows()
    lines = [json.dumps(r) + "\n" for r in rows]
    dv.note_written_lines(KEY, lines)
    dv.submit_committed("ws")
    dv.drain_for_tests()
    _restart(home)
    before = battery["n"]
    out = _render(rows)
    assert SECRET not in out
    assert battery["n"] == before, "every row was judged when it was written"


def test_a_save_that_never_committed_leaves_its_rows_unjudged(home, battery, allowed):
    assert dv.enable(home)
    lines = [json.dumps(r) + "\n" for r in _rows(3)]
    dv.note_written_lines(KEY, lines)  # the write then fails: no submit_committed
    dv.note_written_lines(KEY, lines)
    item = dv.take_pending()
    assert item is not None and len(item[1]) == 3, "the next save offers them again"


def test_without_enable_nothing_is_stored(home, battery, allowed):
    _render(_rows(3))
    assert not (home / dv.DIR_NAME).exists()


def test_a_render_with_few_misses_does_not_read_the_sidecar(home, battery, allowed, monkeypatch):
    """A live session's newest rows are scanned; the long sidecar is not parsed for them."""
    assert dv.enable(home)
    rows = _rows(40)
    _render(rows)
    monkeypatch.setattr(dv, "LOAD_AFTER_MISSES", 1000)
    # Stand in for a long session: the 40-row sidecar is not "small".
    monkeypatch.setattr(dv, "LOAD_BYTES_PER_MISS", 0)
    reads = []
    real = dv._load
    monkeypatch.setattr(dv, "_load", lambda p, name: reads.append(name) or real(p, name))
    newest = [
        {"role": "assistant", "content": f"fresh reply {i} {SECRET}", "cls": "msg msg-a"}
        for i in range(dv.COLD_MISSES + 2)
    ]
    before = battery["n"]
    out = _render(rows + newest)
    assert reads == [], "older rows hit memory, so this is a live render"
    assert battery["n"] > before
    assert SECRET not in out


def test_a_cold_open_reads_the_sidecar_after_a_few_misses(home, battery, allowed, monkeypatch):
    """After a restart nothing hits memory: the sidecar is read, not 64 strings scanned per open."""
    assert dv.enable(home)
    rows = _rows(40)
    cold = _render(rows)
    _restart(home)
    monkeypatch.setattr(dv, "LOAD_AFTER_MISSES", 1000)
    before = battery["n"]
    out = _render(rows)
    assert out == cold
    assert battery["n"] - before == dv.COLD_MISSES - 1, "only the probe misses were scanned"
    assert SECRET not in out


def test_reopening_after_a_restart_does_not_grow_the_sidecar(home, battery, allowed, monkeypatch):
    """The probe misses a cold open scans before reading the sidecar are already stored."""
    monkeypatch.setattr(dv, "LOAD_AFTER_MISSES", 1000)
    assert dv.enable(home)
    rows = _rows(40)
    _render(rows)
    root = home / dv.DIR_NAME
    sizes = [sum(f.stat().st_size for f in root.iterdir())]
    before = battery["n"]
    for _ in range(3):
        _restart(home)
        _render(rows)
        sizes.append(sum(f.stat().st_size for f in root.iterdir()))
    assert battery["n"] - before == 3 * (dv.COLD_MISSES - 1), "each reopen scanned its probes"
    assert len(set(sizes)) == 1, sizes


def test_a_small_sidecar_is_read_even_when_shared_strings_hit_memory(
    home, battery, allowed, monkeypatch
):
    """Strings other sessions share can hit memory on a restart; a small sidecar is read anyway."""
    assert dv.enable(home)
    rows = _rows(40)
    cold = _render(rows)
    _restart(home)
    monkeypatch.setattr(dv, "LOAD_AFTER_MISSES", 1000)
    # Every string a cold open serves from memory counts as a hit, as a string
    # shared with an already-open session would.
    shared = [
        {"role": "assistant", "content": f"shared preamble {i}", "cls": "msg msg-a"}
        for i in range(200)
    ]
    with dv.session("other-session"):
        _prepare_messages(shared, running=False, live_child="", workspace="ws")
    reads = []
    real = dv._load
    monkeypatch.setattr(dv, "_load", lambda p, name: reads.append(name) or real(p, name))
    size = sum(f.stat().st_size for f in (home / dv.DIR_NAME).iterdir())
    before = battery["n"]
    out = _render(shared + rows)
    assert SECRET not in out
    assert cold[1:-1] in out
    assert reads, "the small sidecar was read"
    scans = battery["n"] - before
    assert scans <= max(dv.COLD_MISSES, size // dv.LOAD_BYTES_PER_MISS + 1) - 1
    assert scans < len(rows), "far fewer scans than the session's strings"


def test_a_render_with_many_large_redactions_keeps_its_sidecar_readable(
    home, battery, allowed, monkeypatch
):
    """Redacted outputs past the file cap are dropped whole, so the file stays readable."""
    monkeypatch.setattr(dv, "MAX_FILE_BYTES", 8192)
    assert dv.enable(home)
    rows = [
        {
            "role": "assistant",
            "content": "x" * 880 + f" leaks {SECRET} {i}",
            "cls": "msg msg-a",
            "ts": "2026-09-23T22:00:00Z",
        }
        for i in range(20)
    ]
    cold = _render(rows)
    dvr = home / dv.DIR_NAME / (dv._safe_name(KEY) + dv._REDACTED_SUFFIX)
    assert 0 < dvr.stat().st_size <= 8192, "the redacted sidecar stays under its cap"
    cold_scans = battery["n"]
    _restart(home)
    before = battery["n"]
    assert _render(rows) == cold
    assert battery["n"] - before < cold_scans, "the stored redactions were reused"


def test_a_failed_save_leaves_nothing_staged(home, tmp_path, monkeypatch):
    """A save whose write raises drops its staged rows instead of keeping them on the thread."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_persistence as cp

    assert dv.enable(home)
    state = _make_state(tmp_path / "state")
    slot = state.get_or_create_slot("fail1")
    slot._titled = True
    slot.append("user", "hello")
    slot.drain()

    def boom(*_a, **_k):
        raise OSError("disk full")

    staged = []
    real_note = dv.note_written_lines

    def spy(key, lines):  # type: ignore[no-untyped-def]
        real_note(key, lines)
        staged.append(getattr(dv._written_local, "pending", None))

    monkeypatch.setattr(dv, "note_written_lines", spy)
    monkeypatch.setattr(cp, "atomic_write", boom)
    with pytest.raises(OSError):
        cp._save_slot_to_history(state, slot, closed=False)
    assert staged and staged[-1] is not None, "the save staged its rows before writing"
    assert dv.take_pending() is None, "the failed save cleared what it staged"


def test_a_window_past_the_queue_budget_is_not_staged(home, monkeypatch):
    monkeypatch.setattr(dv, "_QUEUE_MAX_CHARS", 100)
    assert dv.enable(home)
    lines = ["x" * 80 + "\n", "y" * 80 + "\n"]
    dv.note_written_lines(KEY, lines)
    item = dv.take_pending()
    assert item is not None
    assert item[1] == [], "rows over the budget are left to the first read"
    assert len(item[2]) == 2, "the window is still remembered as written"


def test_the_boot_sweep_keeps_no_per_file_list(home, monkeypatch):
    """Orphans go during the scan; an over-budget directory is cleared by a second scan."""
    root = home / dv.DIR_NAME
    root.mkdir(mode=0o700)
    for i in range(50):
        (root / f"gone-{i}{dv._CLEAN_SUFFIX}").write_bytes(b"")
    (root / f"{KEY}{dv._CLEAN_SUFFIX}").write_bytes(b"k" * 64)
    events = []
    real_unlink = os.unlink
    real_scandir = os.scandir

    def tracked(path, *a, **k):  # type: ignore[no-untyped-def]
        events.append("unlink")
        return real_unlink(path, *a, **k)

    class Scan:
        def __init__(self, p):  # type: ignore[no-untyped-def]
            self.it = real_scandir(p)

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *exc):  # type: ignore[no-untyped-def]
            self.it.close()

        def __iter__(self):  # type: ignore[no-untyped-def]
            for e in self.it:
                events.append("entry")
                yield e

    monkeypatch.setattr(os, "unlink", tracked)
    monkeypatch.setattr(os, "scandir", Scan)
    dv._sweep(root, home / "sessions")
    assert sorted(p.name for p in root.iterdir()) == [f"{KEY}{dv._CLEAN_SUFFIX}"]
    first_unlink = events.index("unlink")
    last_entry = len(events) - 1 - events[::-1].index("entry")
    assert first_unlink < last_entry, "orphans are removed while the scan runs"
    monkeypatch.setattr(dv, "MAX_DIR_BYTES", 10)
    dv._sweep(root, home / "sessions")
    assert list(root.iterdir()) == []
    assert events.count("unlink") == 51
