"""Allowed hosts: per workspace, in a sealed leaf, relaxing only length/base64 checks."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from kiro_crew.platform_compat import IS_POSIX
from kiro_crew.security import redaction_allow
from kiro_crew.security.exfil import redact_exfiltration_urls_with_records

_LONG = "https://reviews.corp.example/reviews?filter=" + "a" * 260


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "redaction-allow" / "hosts.json"
    monkeypatch.setattr(redaction_allow, "_path_override", path)
    monkeypatch.setattr(redaction_allow, "_snapshot", None)
    monkeypatch.setattr(redaction_allow, "_loading", False)
    monkeypatch.setattr(redaction_allow, "_load_thread", None)
    yield path
    # This teardown runs before monkeypatch restores the override, so a loader
    # thread this test started resolves the path while it still names tmp_path.
    thread = redaction_allow._load_thread
    if thread is not None:
        thread.join(timeout=10)
        assert not thread.is_alive(), "redaction-allow loader outlived its test"


def test_allow_list_and_revoke_round_trip(_store: Path) -> None:
    assert redaction_allow.allow_host("ws1", "Reviews.Corp.Example")
    assert redaction_allow.allowed_hosts_for("ws1") == {"reviews.corp.example"}
    assert redaction_allow.allowed_hosts_for("ws2") == frozenset()
    if IS_POSIX:  # Windows has no owner-only mode bits to read back.
        assert oct(_store.stat().st_mode & 0o777) == "0o600"
    assert redaction_allow.revoke_host("ws1", "reviews.corp.example")
    assert redaction_allow.list_allowed() == {}


def test_the_workspace_count_is_capped_on_insert_and_on_load(_store: Path) -> None:
    import json

    cap = redaction_allow.MAX_WORKSPACES
    for i in range(cap):
        assert redaction_allow.allow_host(f"ws{i}", "a.example")
    assert not redaction_allow.allow_host("one-more", "a.example")
    assert redaction_allow.allow_host("ws0", "b.example")
    assert len(redaction_allow.list_allowed()) == cap

    _store.write_text(json.dumps({f"w{i}": ["a.example"] for i in range(cap + 10)}))
    assert len(redaction_allow.list_allowed()) == cap


def test_off_shape_host_is_refused() -> None:
    assert not redaction_allow.allow_host("ws1", "evil example")
    assert not redaction_allow.allow_host("ws1", "https://x.example")


def test_a_corrupt_file_means_nothing_is_allowed(_store: Path) -> None:
    _store.parent.mkdir(parents=True)
    _store.write_text("{not json")
    assert redaction_allow.allowed_hosts_for("ws1") == frozenset()


def test_an_allowed_host_keeps_its_long_query_link() -> None:
    blocked, _, records = redact_exfiltration_urls_with_records(_LONG)
    assert records and records[0]["rule"] == "exfil_query_length"
    kept, _, none = redact_exfiltration_urls_with_records(
        _LONG, extra_exempt_hosts=frozenset({"reviews.corp.example"})
    )
    assert kept == _LONG and none == []


def test_an_allowed_host_still_loses_a_credential() -> None:
    url = "https://reviews.corp.example/x?k=AKIA" + "IOSFODNN7EXAMPLE"
    out, _, _ = redact_exfiltration_urls_with_records(
        url, extra_exempt_hosts=frozenset({"reviews.corp.example"})
    )
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_display_pass_keeps_an_allowed_link_only_inside_its_scope() -> None:
    from kiro_crew.dashboard.chat_utils import (
        _clear_display_redaction_cache,
        redact_display_content,
    )
    from kiro_crew.security.exfil import scoped_exempt_hosts

    _clear_display_redaction_cache()
    assert _LONG not in redact_display_content(_LONG)
    with scoped_exempt_hosts(frozenset({"reviews.corp.example"})):
        assert redact_display_content(_LONG) == _LONG
    # The cache is keyed on the scope, so the relaxed result does not leak out.
    assert _LONG not in redact_display_content(_LONG)


def test_prepare_messages_relaxes_the_slot_workspace_hosts() -> None:
    from kiro_crew.dashboard.chat_utils import _clear_display_redaction_cache, _prepare_messages

    _clear_display_redaction_cache()
    rows = [{"role": "assistant", "content": _LONG, "ts": "1"}]
    assert _LONG not in _prepare_messages(rows, False, live_child="", workspace="ws1")[0]["content"]
    redaction_allow.allow_host("ws1", "reviews.corp.example")
    assert _prepare_messages(rows, False, live_child="", workspace="ws1")[0]["content"] == _LONG
    assert _LONG not in _prepare_messages(rows, False, live_child="", workspace="ws2")[0]["content"]


def test_a_variant_shows_an_allowed_link_like_its_row() -> None:
    from kiro_crew.dashboard.chat_utils import _clear_display_redaction_cache, _prepare_messages

    _clear_display_redaction_cache()
    saved, _warnings, records = redact_exfiltration_urls_with_records(_LONG)
    assert _LONG not in saved and records
    variant = {"content": saved, "ts": "0", "blocked_links": records}
    rows = [{"role": "assistant", "content": "current", "ts": "1", "variants": [variant]}]
    [before] = _prepare_messages(rows, False, live_child="", workspace="ws1")[0]["variants"]
    assert _LONG not in before["content"] and before["blocked_links"]
    redaction_allow.allow_host("ws1", "reviews.corp.example")
    [after] = _prepare_messages(rows, False, live_child="", workspace="ws1")[0]["variants"]
    assert after["content"] == _LONG
    assert "blocked_links" not in after


def test_what_is_saved_never_depends_on_the_allow_list() -> None:
    """Saving and reloading redact every blocked link with its record whatever
    the reader allowed, so a list that has not loaded yet cannot lose a link."""
    import inspect

    from kiro_crew.dashboard import chat_persistence, chat_runner

    redaction_allow.allow_host("ws1", "reviews.corp.example")
    for module in (chat_persistence, chat_runner):
        src = inspect.getsource(module)
        assert "allowed_hosts_for" not in src
        assert "scoped_exempt_hosts" not in src
    row = {"role": "assistant", "content": _LONG, "ts": "1", "cls": "msg msg-a"}
    assert _LONG not in chat_persistence._build_message_entry(dict(row))["content"]


def test_the_live_frame_restores_an_allowed_link(monkeypatch) -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard.state import DashboardState

    row = _saved_row()
    sent: list[dict] = []
    state = DashboardState.__new__(DashboardState)
    monkeypatch.setattr(
        state, "get_slot", lambda _k: SimpleNamespace(workspace="ws1"), raising=False
    )
    monkeypatch.setattr(state, "_broadcast", sent.append, raising=False)
    redaction_allow.allow_host("ws1", "reviews.corp.example")
    state._broadcast_chat_message("s1", row)
    assert sent[-1]["content"] == f"See [the reviews]({_LONG})."
    assert "blocked_links" not in (sent[-1].get("meta") or {})
    assert _LONG not in row["content"]


def _saved_row() -> dict:
    """An assistant row as saved before its host was allowed."""
    text, _, records = redact_exfiltration_urls_with_records(f"See [the reviews]({_LONG}).")
    assert records and records[0]["url"] == _LONG
    return {"role": "assistant", "content": text, "ts": "1", "meta": {"blocked_links": records}}


def test_a_saved_placeholder_shows_its_link_once_the_host_is_allowed() -> None:
    from kiro_crew.dashboard.chat_utils import _clear_display_redaction_cache, _prepare_messages

    _clear_display_redaction_cache()
    row = _saved_row()
    before = _prepare_messages([row], False, live_child="", workspace="ws1")[0]
    assert _LONG not in before["content"] and before["meta"]["blocked_links"]
    redaction_allow.allow_host("ws1", "reviews.corp.example")
    after = _prepare_messages([row], False, live_child="", workspace="ws1")[0]
    assert after["content"] == f"See [the reviews]({_LONG})."
    assert "blocked_links" not in after["meta"]
    # Another workspace, and the stored row itself, are untouched.
    other = _prepare_messages([row], False, live_child="", workspace="ws2")[0]
    assert _LONG not in other["content"]
    assert _LONG not in row["content"]


def test_restore_keeps_links_the_allow_does_not_cover() -> None:
    from kiro_crew.security.exfil import restore_allowed_links

    text, _, records = redact_exfiltration_urls_with_records(f"a {_LONG} b")
    assert len(records) == 1
    out, left = restore_allowed_links(text, records, frozenset({"reviews.corp.example"}))
    assert out == f"a {_LONG} b" and left == []
    same, kept = restore_allowed_links(text, records, frozenset({"other.example"}))
    assert same == text and kept == records
    withheld = [{**r, "url": None} for r in records]
    assert restore_allowed_links(text, withheld, frozenset({"reviews.corp.example"}))[0] == text
    other_rule = [{**r, "rule": "exfil_credential"} for r in records]
    assert restore_allowed_links(text, other_rule, frozenset({"reviews.corp.example"}))[0] == text


def test_restore_leaves_a_host_whose_links_differ() -> None:
    """A placeholder names only its host, so with two addresses on one host none
    is restored rather than one landing where the other stood."""
    from kiro_crew.security.exfil import restore_allowed_links

    second = _LONG.replace("filter=", "sort=")
    text, _, records = redact_exfiltration_urls_with_records(f"a {_LONG} b {_LONG} c {second}")
    assert len(records) == 2
    out, left = restore_allowed_links(text, records, frozenset({"reviews.corp.example"}))
    assert out == text and left == records


def test_an_allowed_host_is_neither_redacted_nor_reported() -> None:
    text, warnings, records = redact_exfiltration_urls_with_records(
        f"see {_LONG}", extra_exempt_hosts=frozenset({"reviews.corp.example"})
    )
    assert text == f"see {_LONG}" and warnings == [] and records == []


def test_the_allow_list_lives_in_a_sealed_read_only_leaf(monkeypatch) -> None:
    """An agent that could write the list could allow its own exfiltration host."""
    from kiro_crew import sandbox

    monkeypatch.setattr(redaction_allow, "_path_override", None)
    leaf = redaction_allow._path().parent.name
    assert leaf == "redaction-allow"
    assert leaf in sandbox._CREW_READONLY_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert leaf in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES


def test_an_unsupported_workspace_name_is_refused_not_folded_into_default() -> None:
    assert redaction_allow.allow_host("default", "reviews.corp.example")
    assert not redaction_allow.allow_host("team/dev", "other.corp.example")
    assert redaction_allow.allowed_hosts_for("team/dev") == frozenset()
    assert redaction_allow.allowed_hosts_for("default") == {"reviews.corp.example"}
    assert redaction_allow.allowed_hosts_for(None) == {"reviews.corp.example"}


def test_render_reads_come_from_memory_after_preload(monkeypatch) -> None:
    """The render paths run on the event loop, so they never read the disk."""
    assert redaction_allow.allow_host("ws1", "reviews.corp.example")
    redaction_allow.preload()

    def _no_disk(*_a, **_k):
        raise AssertionError("allow-list read touched the disk")

    monkeypatch.setattr(Path, "read_text", _no_disk)
    assert redaction_allow.allowed_hosts_for("ws1") == {"reviews.corp.example"}
    assert redaction_allow.list_allowed() == {"ws1": ["reviews.corp.example"]}


def test_reads_allow_nothing_while_the_background_load_is_pending() -> None:
    """Nothing loads the list at boot: the first read starts a background load
    and allows no host until it lands."""
    assert redaction_allow.allow_host("ws1", "reviews.corp.example")
    redaction_allow._snapshot = None
    assert redaction_allow.allowed_hosts_for("ws1") == frozenset()
    thread = redaction_allow._load_thread
    assert thread is not None
    thread.join(timeout=5)
    assert redaction_allow.allowed_hosts_for("ws1") == {"reviews.corp.example"}


def test_a_write_during_the_background_load_keeps_existing_entries(_store: Path) -> None:
    """A write builds on the file, not on the empty answer a pending load gives."""
    assert redaction_allow.allow_host("ws1", "reviews.corp.example")
    redaction_allow._snapshot = None
    redaction_allow._loading = True  # a load is pending; reads answer empty
    assert redaction_allow.allowed_hosts_for("ws1") == frozenset()
    assert redaction_allow.allow_host("ws1", "docs.corp.example")
    assert redaction_allow.list_allowed() == {"ws1": ["reviews.corp.example", "docs.corp.example"]}


def test_the_settings_list_reads_the_file_even_before_the_load_lands() -> None:
    assert redaction_allow.allow_host("ws1", "reviews.corp.example")
    redaction_allow._snapshot = None
    redaction_allow._loading = True
    assert redaction_allow.list_allowed() == {"ws1": ["reviews.corp.example"]}


def test_restore_leaves_a_host_with_a_placeholder_no_record_stands_behind() -> None:
    """A placeholder the model echoed in prose has no record, so it must not
    become a link to an address that never stood there."""
    from kiro_crew.security.exfil import restore_allowed_links

    text, _, records = redact_exfiltration_urls_with_records(f"a {_LONG}")
    echoed = text + " and again " + text.split("a ", 1)[1]
    out, left = restore_allowed_links(echoed, records, frozenset({"reviews.corp.example"}))
    assert out == echoed and left == records


def test_the_file_edit_tool_cannot_write_the_allow_list(monkeypatch) -> None:
    """The sandbox seal covers a shell; the write gate covers the file-edit tool
    on a host with no OS sandbox."""
    from kiro_crew.security import is_sensitive_write_path

    monkeypatch.setattr(redaction_allow, "_path_override", None)
    assert is_sensitive_write_path(str(redaction_allow._path()))
