"""Redaction tests for kiro_crew.sel: what the writer persists of caller-supplied values.

Pins metadata redaction before persistence and the escaping of a control character in an
audited value.
``test_sel.py`` holds the core contract and names the other topic modules; the shared
fixtures and helpers live in ``sel_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sel_helpers import (  # noqa: F401 -- fixtures are requested by name
    _make_event,
    log_fixture,
    reset_singleton,
    sel_dir_fixture,
)

from kiro_crew.sel import SecurityEventLog


def _shutdown_writer(log: SecurityEventLog) -> None:
    """Deterministically stop *log*'s background writer inside the test.

    An async SecurityEventLog binds its directory into a daemon writer thread;
    without an explicit shutdown the thread outlives the test while holding a
    reference to a deleted per-test tmp_path. Flush, send the shutdown
    sentinel, and join so nothing survives teardown.
    """
    log.flush()
    log._queue.put(None)
    writer = log._writer
    if writer is not None:
        writer.join(timeout=5)


class TestMetadataRedaction:
    """The writer redacts caller-supplied metadata values before persistence.

    The acceptance is "never reaches disk": every assertion here reads the raw
    on-disk JSONL back, not a mock. The token is assembled from parts so this
    test file itself never contains a credential-shaped literal.
    """

    AKIA_TOKEN = "AKIA" + "IOSFODNN7EXAMPLE"
    EXFIL_URL = "https://attacker.example/upload/" + "AKIA" + "IOSFODNN7EXAMPLE"

    def _disk_text(self, sel_dir: Path) -> str:
        return (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")

    def test_credential_in_metadata_never_reaches_disk(self, log, sel_dir):
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="discover_skills",
            outcome="success",
            metadata={"query": f"find {self.AKIA_TOKEN} docs", "result_count": "3"},
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        assert "[REDACTED: credential]" in data["metadata"]["query"]
        # Non-secret values pass through untouched.
        assert data["metadata"]["result_count"] == "3"

    def test_exfiltration_url_in_metadata_never_reaches_disk(self, log, sel_dir):
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="discover_mcp_servers",
            outcome="success",
            metadata={"query": self.EXFIL_URL},
        )
        raw = self._disk_text(sel_dir)
        assert self.EXFIL_URL not in raw
        assert self.AKIA_TOKEN not in raw

    def test_nested_metadata_values_are_redacted(self, log, sel_dir):
        log.log(
            _make_event(
                event_id="nested1",
                metadata={
                    "outer": {"inner": self.AKIA_TOKEN},
                    "items": [self.AKIA_TOKEN, 7, None],
                },
            )
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        # Keys and non-string values are untouched.
        assert "outer" in data["metadata"] and "inner" in data["metadata"]["outer"]
        assert data["metadata"]["items"][1] == 7
        assert data["metadata"]["items"][2] is None

    def test_caller_metadata_dict_is_not_mutated(self, log):
        caller_metadata = {"query": self.AKIA_TOKEN, "nested": {"k": self.AKIA_TOKEN}}
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="discover_skills",
            outcome="success",
            metadata=caller_metadata,
        )
        assert caller_metadata["query"] == self.AKIA_TOKEN
        assert caller_metadata["nested"]["k"] == self.AKIA_TOKEN

    def test_redacted_events_chain_verifies(self, log):
        """The HMAC chain signs the redacted bytes, so verify stays green."""
        for i in range(3):
            log.log(
                _make_event(
                    event_id=f"chain{i}",
                    metadata={"query": f"{self.AKIA_TOKEN} #{i}"},
                )
            )
        total, valid = log.verify_integrity()
        assert total == 3
        assert valid == 3

    def test_async_writer_path_redacts(self, tmp_path: Path) -> None:
        """The production background-writer path redacts too, not just sync."""
        log = SecurityEventLog(base_dir=tmp_path)  # async (default)
        try:
            log.log_tool_invocation(
                session_key="dashboard:slot1",
                tool_name="discover_skills",
                outcome="success",
                metadata={"query": self.AKIA_TOKEN},
            )
            log.flush()
            raw = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8")
            assert self.AKIA_TOKEN not in raw
        finally:
            _shutdown_writer(log)

    def test_events_without_metadata_are_untouched(self, log, sel_dir):
        log.log(_make_event(event_id="plain1"))
        data = json.loads(self._disk_text(sel_dir).strip())
        assert data["metadata"] == {}

    def test_namedtuple_metadata_value_does_not_cost_the_event(self, log, sel_dir):
        """A sequence subclass with a different constructor (json-legal today)
        must not raise out of the walker; it degrades to a plain array."""
        from collections import namedtuple

        point = namedtuple("point", ["x", "y"])
        log.log(
            _make_event(
                event_id="nt1",
                metadata={"pos": point(self.AKIA_TOKEN, 2)},
            )
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        assert data["metadata"]["pos"][1] == 2

    def test_redaction_failure_persists_placeholder_not_raw(self, log, sel_dir):
        """Fail-closed containment: if the redactor raises, the event lands
        with placeholder metadata and the raw text never reaches disk."""
        with patch(
            "kiro_crew.sel._redacted_metadata_copy",
            side_effect=RuntimeError("simulated redactor failure"),
        ):
            log.log(
                _make_event(
                    event_id="rf1",
                    metadata={"query": self.AKIA_TOKEN},
                )
            )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        assert data["metadata"] == {"redaction_error": "RuntimeError"}

    def test_redaction_failure_does_not_drop_batch_siblings(self, tmp_path: Path) -> None:
        """One pathological metadata value must not cost unrelated events
        queued in the same writer batch."""
        log = SecurityEventLog(base_dir=tmp_path)  # async: events batch together

        def _flaky(metadata: dict) -> dict:
            if "poison" in metadata:
                raise RuntimeError("simulated redactor failure")
            return {k: v for k, v in metadata.items()}

        try:
            with patch("kiro_crew.sel._redacted_metadata_copy", side_effect=_flaky):
                log.log(_make_event(event_id="ok1", metadata={"query": "fine"}))
                log.log(_make_event(event_id="bad1", metadata={"poison": "x"}))
                log.log(_make_event(event_id="ok2", metadata={"query": "also fine"}))
                log.flush()
        finally:
            _shutdown_writer(log)
        lines = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8").splitlines()
        ids = [json.loads(ln)["event_id"] for ln in lines if ln.strip()]
        assert ids == ["ok1", "bad1", "ok2"]

    def test_forward_callback_uses_shared_walker_on_namedtuple(self, log):
        """The forward path shares the walker, so a namedtuple degrades to a
        plain array there too instead of raising."""
        from collections import namedtuple

        received: list[dict] = []
        log.set_forward_callback(received.append)
        point = namedtuple("point", ["x", "y"])
        log.log(_make_event(event_id="fw1", metadata={"pos": point(self.AKIA_TOKEN, 2)}))
        assert len(received) == 1
        assert received[0]["metadata"]["pos"][1] == 2
        assert self.AKIA_TOKEN not in json.dumps(received[0])

    def test_error_and_resources_fields_never_reach_disk_raw(self, log, sel_dir):
        """Top-level free-form strings get the same write-time pass: an
        exception message quoting a credential must not persist raw."""
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="execute_bash",
            outcome="failed",
            resources=f"curl -H 'X-Key: {self.AKIA_TOKEN}'",
            error=f"RuntimeError: auth failed for {self.AKIA_TOKEN}",
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        assert "[REDACTED: credential]" in data["resources"]
        assert "[REDACTED: credential]" in data["error"]
        # The plain parts of the strings survive.
        assert data["error"].startswith("RuntimeError: auth failed for ")

    def test_identity_fields_stay_verbatim(self, log, sel_dir):
        """Identity-shaped fields are constrained vocabularies, not free text —
        the writer must not rewrite them."""
        log.log(
            _make_event(
                event_id="ident1",
                caller_identity="dashboard:slot-AKIA-not-a-key",
                downstream_service="kirocrew-core",
            )
        )
        data = json.loads(self._disk_text(sel_dir).strip())
        assert data["caller_identity"] == "dashboard:slot-AKIA-not-a-key"
        assert data["downstream_service"] == "kirocrew-core"

    def test_lowercased_aws_key_never_reaches_disk(self, log, sel_dir):
        """SEL callers may normalize case before logging (the file-search
        handler lowercases its query); a lowercased key is trivially
        reversible, so the write-time pass must catch it case-insensitively."""
        lowered = self.AKIA_TOKEN.lower()
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="file_search",
            outcome="allowed",
            resources=f"q={lowered} kinds=all",
            metadata={"query": lowered},
        )
        raw = self._disk_text(sel_dir)
        assert lowered not in raw
        data = json.loads(raw.strip())
        assert "[REDACTED: credential]" in data["resources"]
        assert "[REDACTED: credential]" in data["metadata"]["query"]

    def test_anycase_pass_leaves_ordinary_words_alone(self, log, sel_dir):
        """The case-insensitive net is bounded: prose containing 'akia' or
        'asia' inside longer tokens must not be rewritten."""
        text = "asian markets akiary search results for East Asia region"
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="file_search",
            outcome="allowed",
            resources=text,
            metadata={"query": text},
        )
        data = json.loads(self._disk_text(sel_dir).strip())
        assert data["resources"] == text
        assert data["metadata"]["query"] == text

    def test_credential_straddling_the_truncation_boundary_is_gone(self, log, sel_dir):
        """The log_* helpers clip free-form text to _MAX_ARG_LEN. Clipping FIRST
        would cut a credential in half and leave a prefix that no full-token
        grammar matches, so redaction must run on the whole string BEFORE the
        clip. Place the key so the clip lands mid-token."""
        from kiro_crew.sel import _MAX_ARG_LEN

        # Key starts 10 chars before the clip point, so a clip-first order would
        # keep its first 10 characters and drop the rest.
        prefix = "q=" + ("x" * (_MAX_ARG_LEN - 12))
        resources = prefix + self.AKIA_TOKEN + " kinds=all"
        assert len(prefix) < _MAX_ARG_LEN < len(prefix) + len(self.AKIA_TOKEN)
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="file_search",
            outcome="allowed",
            resources=resources,
            error=resources,
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        # No partial prefix of the key survives either.
        assert self.AKIA_TOKEN[:10] not in raw
        data = json.loads(raw.strip())
        # The replacement marker is longer than the token it replaces, so the
        # clip can cut the MARKER — harmless, and proof the order is right: a
        # marker fragment sits where the credential's first characters would be
        # under a clip-first order.
        assert "[REDACTED" in data["resources"]
        assert "[REDACTED" in data["error"]
        # The clip is still enforced.
        assert len(data["resources"]) <= _MAX_ARG_LEN
        assert len(data["error"]) <= _MAX_ARG_LEN

    def test_api_access_helper_also_redacts_before_clipping(self, log, sel_dir):
        from kiro_crew.sel import _MAX_ARG_LEN

        prefix = "path=" + ("y" * (_MAX_ARG_LEN - 15))
        log.log_api_access(
            caller="token:abc",
            operation="GET /api/file-search",
            outcome="allowed",
            resources=prefix + self.AKIA_TOKEN,
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        assert self.AKIA_TOKEN[:10] not in raw
        assert len(json.loads(raw.strip())["resources"]) <= _MAX_ARG_LEN


class TestControlCharacterSerialization:
    """A control character in an audited value is escaped, never written raw.

    Audited values include caller-supplied ones -- a requested file path, for
    instance -- and the record is read back in a terminal (`kirocrew logs`). A
    raw ESC or 8-bit CSI byte in the file would be EXECUTED as a terminal escape
    rather than displayed: screen clears, forged output, hidden lines. The record
    is JSON, so json.dumps escapes those bytes, and this pins that property
    rather than leaving it an incidental consequence of the format.
    """

    @pytest.mark.parametrize(
        "payload",
        [
            "/tmp/(a\x1b[2Jb)",  # ESC: clear-screen
            "/tmp/a\x9bb",  # 8-bit CSI
            "/tmp/a\x85b",  # NEL
            "/tmp/a\rforged: line",  # CR: overwrite the rendered line
            "/tmp/a\nforged: line",  # LF: forge a whole record
        ],
    )
    def test_a_control_character_is_escaped_in_the_written_record(self, log, sel_dir, payload):
        log.log_tool_invocation(
            session_key="dashboard",
            tool_name="file_read",
            outcome="denied",
            resources=payload,
        )
        written = (sel_dir / "security_events.jsonl").read_bytes()
        # Exactly one record: an embedded LF in the value did not forge a second.
        lines = written.splitlines()
        assert len(lines) == 1
        # The RECORD carries no control character for a terminal to act on.
        # splitlines has already removed the line terminator, which is the log's
        # own framing and is \r\n on Windows -- not part of the audited value.
        record = lines[0].decode("utf-8")
        assert not any(ch <= "\x1f" or "\x7f" <= ch <= "\x9f" for ch in record)
        # The value is present and round-trips intact, so escaping is not dropping
        # information -- it is only making it inert.
        assert json.loads(record)["resources"] == payload
