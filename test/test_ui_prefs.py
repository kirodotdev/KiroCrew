"""Tests for the browser UI preference backup store (kiro_crew.ui_prefs)."""

from __future__ import annotations

import json

import pytest

from kiro_crew import ui_prefs
from kiro_crew.ui_prefs import (
    MAX_KEYS,
    MAX_TOTAL_BYTES,
    MAX_VALUE_BYTES,
    UiPrefsError,
    load_ui_prefs,
    merge_ui_prefs,
    ui_prefs_path,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point the data home at a temp dir so tests never touch the real one."""
    monkeypatch.setattr(ui_prefs, "config_dir", lambda: tmp_path)
    return tmp_path


def test_load_returns_empty_when_no_file():
    assert load_ui_prefs() == {}


def test_merge_persists_and_reloads():
    merged = merge_ui_prefs({"mc-chat-config": '{"sendOnEnter":"ctrl-enter"}'})
    assert merged == {"mc-chat-config": '{"sendOnEnter":"ctrl-enter"}'}
    assert load_ui_prefs() == merged


def test_merge_is_a_patch_not_a_replace():
    merge_ui_prefs({"a": "1", "b": "2"})
    merge_ui_prefs({"b": "3"})
    assert load_ui_prefs() == {"a": "1", "b": "3"}


def test_null_deletes_a_key():
    merge_ui_prefs({"mc-dev-mode": "true"})
    assert merge_ui_prefs({"mc-dev-mode": None}) == {}
    assert load_ui_prefs() == {}


def test_deleting_an_absent_key_is_not_an_error():
    assert merge_ui_prefs({"never-stored": None}) == {}


def test_envelope_shape_on_disk():
    """`{"prefs": ...}` is the whole contract: no version stamp, no timestamp.

    Both existed in an earlier revision with zero readers. The loader is tolerant
    of any shape it does not recognize, so a future reshape needs no version
    field to be safe, and a stamp nothing checks is just a claim."""
    merge_ui_prefs({"mc-zoom": "1.1"})
    doc = json.loads(ui_prefs_path().read_text(encoding="utf-8"))
    assert doc == {"prefs": {"mc-zoom": "1.1"}}


def test_file_is_owner_only(_isolated_home):
    merge_ui_prefs({"mc-zoom": "1.0"})
    mode = ui_prefs_path().stat().st_mode & 0o777
    # Windows does not honour POSIX bits; assert only where they mean something.
    if hasattr(__import__("os"), "getuid"):
        assert mode == 0o600


def _doc_bytes(prefs: dict) -> int:
    """Byte length of the document as the store serializes it, newline excluded."""
    return len(
        json.dumps({"prefs": prefs}, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    )


def test_a_document_exactly_at_the_cap_is_refused_not_written_one_byte_over():
    """The size bound must measure the EXACT bytes written, trailing newline
    included. The old check measured the document and then appended "\\n", so a
    document of precisely MAX_TOTAL_BYTES landed on disk one byte over the read
    cap, was refused on the next read, and the merge after that treated the
    backup as empty -- discarding every preference in it."""
    base = {f"k{i}": "y" * MAX_VALUE_BYTES for i in range(7)}
    merge_ui_prefs(base)
    fill = MAX_TOTAL_BYTES - _doc_bytes({**base, "k7": ""})
    assert _doc_bytes({**base, "k7": "z" * fill}) == MAX_TOTAL_BYTES  # the exact boundary

    with pytest.raises(UiPrefsError):
        merge_ui_prefs({"k7": "z" * fill})
    # Nothing was written, and the existing backup is intact and readable.
    assert ui_prefs_path().stat().st_size <= MAX_TOTAL_BYTES
    assert load_ui_prefs() == base


def test_a_document_one_byte_under_the_cap_round_trips():
    base = {f"k{i}": "y" * MAX_VALUE_BYTES for i in range(7)}
    merge_ui_prefs(base)
    fill = MAX_TOTAL_BYTES - _doc_bytes({**base, "k7": ""}) - 1
    merged = merge_ui_prefs({"k7": "z" * fill})
    # Exactly the measured bytes, on every platform: text-mode newline
    # translation on Windows once added one byte per line here.
    assert ui_prefs_path().stat().st_size == MAX_TOTAL_BYTES  # newline included
    assert b"\r" not in ui_prefs_path().read_bytes()
    assert load_ui_prefs() == merged


# ── Rejections ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["kiro_crew_token", "some_api_key", "GITHUB_TOKEN", "user-password", "aws-credential"],
)
def test_credential_shaped_keys_are_refused(key):
    with pytest.raises(UiPrefsError):
        merge_ui_prefs({key: "value"})
    assert load_ui_prefs() == {}


@pytest.mark.parametrize("bad", ["\ud800", "ok\udfff", "\ud800\ud800"])
def test_a_lone_surrogate_is_refused_not_a_500(bad):
    """Valid JSON, ordinary str, but not UTF-8 encodable. Left to reach encode()
    it raised UnicodeEncodeError -- a ValueError but not a UiPrefsError -- so it
    escaped the handler as a 500 and took the whole patch with it."""
    with pytest.raises(UiPrefsError):
        merge_ui_prefs({"mc-zoom": bad})
    assert load_ui_prefs() == {}


def test_a_lone_surrogate_in_a_key_is_refused():
    with pytest.raises(UiPrefsError):
        merge_ui_prefs({"mc-\ud800": "1"})


def test_non_string_value_is_refused():
    with pytest.raises(UiPrefsError):
        merge_ui_prefs({"mc-zoom": 1.0})


def test_empty_key_is_refused():
    with pytest.raises(UiPrefsError):
        merge_ui_prefs({"": "x"})


def test_oversized_value_is_refused():
    with pytest.raises(UiPrefsError):
        merge_ui_prefs({"big": "x" * (MAX_VALUE_BYTES + 1)})


def test_too_many_keys_is_refused():
    merge_ui_prefs({f"k{i}": "v" for i in range(MAX_KEYS)})
    with pytest.raises(UiPrefsError):
        merge_ui_prefs({"one-too-many": "v"})


def test_a_rejected_patch_writes_nothing():
    merge_ui_prefs({"keep": "me"})
    with pytest.raises(UiPrefsError):
        merge_ui_prefs({"keep": "changed", "kiro_crew_token": "leak"})
    # Whole-patch rejection: the legitimate half must not have landed either.
    assert load_ui_prefs() == {"keep": "me"}


# ── Corruption tolerance: a bad file means "no backup", never a crash ───────


@pytest.mark.parametrize("body", ["", "not json", "[]", '{"prefs": 5}', '{"prefs": {"a": 5}}'])
def test_unusable_file_degrades_to_empty(body):
    ui_prefs_path().write_text(body, encoding="utf-8")
    assert load_ui_prefs() == {}


def test_credential_key_already_on_disk_is_not_served():
    """A file written by an older or hostile client must not vend a token back."""
    ui_prefs_path().write_text(
        json.dumps({"version": 1, "prefs": {"mc-zoom": "1", "kiro_crew_token": "leak"}}),
        encoding="utf-8",
    )
    assert load_ui_prefs() == {"mc-zoom": "1"}


def test_a_value_carrying_a_credential_is_not_served(_isolated_home):
    """The key denylist catches a credential filed under an honest name; this
    catches one smuggled inside an innocent key by whoever can write the data
    home. Dropped, not redacted: a redacted preference is unusable and would be
    re-synced as the sentinel."""
    ui_prefs_path().write_text(
        json.dumps(
            {
                "prefs": {
                    "mc-crews-view": "grid",
                    "kc:file-explorer:state:v2": '{"note":"AKIAIOSFODNN7EXAMPLE"}',
                }
            }
        ),
        encoding="utf-8",
    )
    served = load_ui_prefs()
    assert served == {"mc-crews-view": "grid"}
    assert "AKIA" not in json.dumps(served)


def test_a_value_carrying_an_exfiltration_url_is_not_served(_isolated_home):
    from kiro_crew.security import redact_exfiltration_urls

    # Use whatever the shared redactor itself considers suspicious, so this test
    # tracks its vocabulary rather than hard-coding one URL shape.
    candidate = "see https://evil.example/collect?d=" + "x" * 300
    cleaned, _ = redact_exfiltration_urls(candidate)
    if cleaned == candidate:
        pytest.skip("redactor does not flag this shape; nothing to assert")
    ui_prefs_path().write_text(json.dumps({"prefs": {"mc-nav": candidate}}), encoding="utf-8")
    assert load_ui_prefs() == {}


def test_a_symlink_is_refused(_isolated_home):
    """The data home is agent-writable, so a planted symlink must not be followed
    (pointed at /dev/zero it would read until the gateway died)."""
    import os

    real = _isolated_home / "elsewhere.json"
    real.write_text(json.dumps({"prefs": {"leaked": "value"}}), encoding="utf-8")
    try:
        os.symlink(real, ui_prefs_path())
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks unavailable on this platform/user")
    assert load_ui_prefs() == {}


def test_a_non_regular_file_is_refused(_isolated_home):
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("POSIX-only")
    os.mkfifo(ui_prefs_path())
    assert load_ui_prefs() == {}


def test_an_oversized_file_is_refused(_isolated_home):
    """A backup this store would refuse to WRITE is one it has no reason to READ."""
    ui_prefs_path().write_text("x" * (ui_prefs.MAX_TOTAL_BYTES + 1), encoding="utf-8")
    assert load_ui_prefs() == {}


def test_a_merge_replaces_a_planted_symlink_with_a_real_file(_isolated_home):
    """Remediation: the atomic write renames ONTO the path, so the planted object
    is replaced rather than written through."""
    import os

    real = _isolated_home / "elsewhere.json"
    real.write_text(json.dumps({"prefs": {"leaked": "value"}}), encoding="utf-8")
    try:
        os.symlink(real, ui_prefs_path())
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks unavailable on this platform/user")

    merge_ui_prefs({"mc-zoom": "1"})
    assert not ui_prefs_path().is_symlink()
    assert load_ui_prefs() == {"mc-zoom": "1"}
    # The symlink target is untouched.
    assert json.loads(real.read_text(encoding="utf-8"))["prefs"] == {"leaked": "value"}


def _deny_reads_of(monkeypatch, target):
    """Make opening ``target`` fail, and return a switch to stop denying.

    Injected at ``os.open`` because that is what the hardened reader uses (it
    refuses symlinks and caps the size, which ``Path.read_text`` cannot do).

    Deliberately not ``monkeypatch.undo()``: that would also revert the autouse
    fixture's ``config_dir`` override and send the follow-up read to the real
    data home.
    """
    state = {"deny": True}
    real_open = ui_prefs.os.open

    def _open(path, *a, **kw):
        if state["deny"] and str(path) == str(target):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(ui_prefs.os, "open", _open)
    return state


def test_an_unreadable_file_does_not_get_replaced_by_the_patch(monkeypatch):
    """A read-modify-write must not start from {} when the read FAILED.

    Doing so would atomically replace a file whose contents it never saw, losing
    every other preference in it. Missing and malformed still mean empty."""
    merge_ui_prefs({"keep": "me", "and": "this"})

    state = _deny_reads_of(monkeypatch, ui_prefs_path())
    with pytest.raises(OSError):
        merge_ui_prefs({"new": "value"})

    state["deny"] = False
    # Untouched: the write never happened.
    assert load_ui_prefs() == {"keep": "me", "and": "this"}


def test_a_plain_read_still_degrades_to_empty(monkeypatch):
    """The non-merge path must never raise: an unreadable backup is just absent."""
    merge_ui_prefs({"keep": "me"})
    _deny_reads_of(monkeypatch, ui_prefs_path())
    assert load_ui_prefs() == {}


def test_merge_over_a_corrupt_file_recovers():
    ui_prefs_path().write_text("{ truncated", encoding="utf-8")
    assert merge_ui_prefs({"mc-zoom": "1"}) == {"mc-zoom": "1"}


class TestReplacingTheFile:
    def test_a_put_cannot_land_while_the_file_is_being_swapped(self, tmp_path, monkeypatch):
        """The reported loss: a PUT from another tab read the old document during an
        import's swap and then published that stale copy over the restored file."""
        import threading

        monkeypatch.setattr(ui_prefs, "ui_prefs_path", lambda: tmp_path / "ui-prefs.json")
        ui_prefs.merge_ui_prefs({"mc-nav": "old"})
        landed = threading.Event()

        def _put() -> None:
            ui_prefs.merge_ui_prefs({"mc-other": "x"})
            landed.set()

        with ui_prefs.replacing_file():
            worker = threading.Thread(target=_put)
            worker.start()
            assert not landed.wait(0.3)  # held off by the writer lock
            (tmp_path / "ui-prefs.json").write_text(
                json.dumps({"prefs": {"mc-nav": "restored"}}), encoding="utf-8"
            )
        worker.join(5)
        assert landed.is_set()
        assert ui_prefs.load_ui_prefs() == {"mc-nav": "restored", "mc-other": "x"}


class TestImportingAnArchiveCopy:
    """`parse_imported_ui_prefs` / `install_imported_ui_prefs`: the settings import's path in."""

    def _archive(self, tmp_path, body):
        src = tmp_path / "archive-ui-prefs.json"
        src.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return src

    def test_each_entry_is_held_to_what_a_put_is_held_to(self, tmp_path):
        src = self._archive(
            tmp_path,
            {
                "prefs": {
                    "mc-nav": "kept",
                    "kiro_crew_token": "bearer",
                    "my_api_key": "x",
                    "mc-null": None,
                    "mc-number": 3,
                    "mc-huge": "x" * (MAX_VALUE_BYTES + 1),
                }
            },
        )
        patch, dropped = ui_prefs.parse_imported_ui_prefs(src)
        assert patch == {"mc-nav": "kept"}
        assert dropped == 5

    @pytest.mark.parametrize("body", ["{not json", "[]", '{"prefs": []}', '{"other": {}}'])
    def test_a_document_of_the_wrong_shape_is_refused_whole(self, tmp_path, body):
        with pytest.raises(UiPrefsError):
            ui_prefs.parse_imported_ui_prefs(self._archive(tmp_path, body))

    def test_more_keys_than_the_store_holds_is_refused_whole(self, tmp_path):
        """Each entry alone passes what a PUT is held to; the document as a whole
        does not. A Replace copies the staged file instead of going through the
        store's writer, so without the aggregate check it installed a file over
        MAX_KEYS and every PUT after the import failed with "too many keys"."""
        prefs = {f"mc-k{i}": "v" for i in range(MAX_KEYS + 1)}
        src = self._archive(tmp_path, {"prefs": prefs})
        assert src.stat().st_size <= MAX_TOTAL_BYTES  # the read path admits it
        with pytest.raises(UiPrefsError, match="too many keys"):
            ui_prefs.parse_imported_ui_prefs(src)
        # Exactly at the cap is fine: the bound is the PUT's, not one tighter.
        patch, dropped = ui_prefs.parse_imported_ui_prefs(
            self._archive(tmp_path, {"prefs": {f"mc-k{i}": "v" for i in range(MAX_KEYS)}})
        )
        assert (len(patch), dropped) == (MAX_KEYS, 0)

    def test_a_document_the_store_would_not_write_is_refused_whole(self, tmp_path):
        """The archive is compact JSON, so it fits under the read cap while the
        store's own rendering of the same entries (indented, newline-terminated)
        does not -- the file a Replace would have installed and the next read
        would have refused, losing every preference in it."""
        base = {f"k{i}": "y" * MAX_VALUE_BYTES for i in range(7)}
        fill = MAX_TOTAL_BYTES - _doc_bytes({**base, "k7": ""})
        prefs = {**base, "k7": "z" * fill}
        assert _doc_bytes(prefs) == MAX_TOTAL_BYTES  # one over once the newline lands
        src = self._archive(tmp_path, {"prefs": prefs})
        assert src.stat().st_size <= MAX_TOTAL_BYTES  # the read path admits it
        with pytest.raises(UiPrefsError, match="would exceed"):
            ui_prefs.parse_imported_ui_prefs(src)
        # Dropped entries do not count: the bound is on what would be written.
        prefs["k7"] = "z" * (fill - 1)
        prefs["kiro_crew_token"] = "bearer"
        src = self._archive(tmp_path, {"prefs": prefs})
        assert src.stat().st_size <= MAX_TOTAL_BYTES
        patch, dropped = ui_prefs.parse_imported_ui_prefs(src)
        assert (len(patch), dropped) == (8, 1)

    def test_an_archive_copy_is_installed_only_where_the_host_has_none(self):
        assert ui_prefs.install_imported_ui_prefs({"mc-nav": "archive"}) is True
        assert load_ui_prefs() == {"mc-nav": "archive"}
        # The host now keeps a file: a second install leaves it whole.
        merge_ui_prefs({"mc-ui": "cli"})
        before = ui_prefs_path().read_bytes()
        assert ui_prefs.install_imported_ui_prefs({"mc-nav": "other", "mc-x": "y"}) is False
        assert ui_prefs_path().read_bytes() == before


# ── Composite chat-config child keys clear the server key checks ────────────


def _chat_config_fields() -> list[str]:
    """Field names of the dashboard's ChatConfig, read from its source so this
    pin tracks the real shape rather than a copy that drifts."""
    import re
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "website"
        / "src"
        / "pages"
        / "chat"
        / "ChatSettings.tsx"
    ).read_text(encoding="utf-8")
    match = re.search(r"const DEFAULTS: ChatConfig = \{(.*?)\}", src, re.DOTALL)
    assert match, "could not locate the ChatConfig DEFAULTS object"
    return re.findall(r"(\w+)\s*:", match.group(1))


def _encode_field(field: str) -> str:
    """Mirror of the client's encodeField in website/src/lib/uiPrefs.ts: four
    lowercase hex digits per character. Hex carries none of the server's denied
    key substrings and no separator character, so every field name is safe on
    the wire whatever it is called."""
    return "".join(f"{ord(c):04x}" for c in field)


def test_every_chat_config_child_key_passes_the_server_key_checks():
    """The composite is synced as `mc-chat-config.<hex(field)>` child keys. A
    raw field name would put a credential substring on the wire -- the real
    `showContextTokens` carries `token`, which `DENY_SUBSTRINGS` rejects, 400ing
    the whole patch. The hex projection must clear the real validator for EVERY
    field, so the fix is proven against the server rather than a mock."""
    fields = _chat_config_fields()
    assert "showContextTokens" in fields, "the field that motivated the fix must be present"
    patch = {f"mc-chat-config.{_encode_field(f)}": "true" for f in fields}
    # Not denied, and the whole patch lands as one merge (no per-key rejection).
    merged = merge_ui_prefs(patch)
    for field in fields:
        assert merged[f"mc-chat-config.{_encode_field(field)}"] == "true"
