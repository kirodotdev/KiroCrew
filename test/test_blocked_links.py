"""The blocked-link decision: which links render as a Blocked link card, never a rewrite.

``security.blocked_links.blocked_links`` returns one record per distinct URL the
exfiltration heuristics flag, and the dashboard draws each as a card instead of a
live link. These tests pin that the text is never changed (the decision returns no
text, and the saved row, the live frame and the history keep every URL as written),
the record shape, the full-address rule a card's Open once follows, that the
decision reads neither the owner's redaction setting nor an output seam, and that
the records are computed at display time and never saved or served from a
transcript line.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import redaction_seams
from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard.state_parts.transcript_rows import chat_message_note
from kiro_crew.security import blocked_links as bl
from kiro_crew.security import exfil, redact_credentials, scan_exfiltration_urls
from kiro_crew.security.blocked_links import blocked_links

_DOMAIN = "collect.example.com"
_LONG_QUERY_TAIL = "A" * 250
# A GitHub-token-shaped value: the fixed credential rule reads it, and it is not a
# cloud key the scope lane's corpus would refuse.
_SECRET_LABEL = "ghp_" + "k7qmz2x9wv4tb8ncr6yd3fj5hs1gpl0aeuo2"
_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"


def _urls(records: list[dict]) -> list[str]:
    return [record["url"] for record in records]


class TestTheDecisionNeverChangesText:
    def test_it_only_reads_the_text(self) -> None:
        url = f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}"
        text = f"first {url} then [the link]({url})."
        before = str(text)

        records = blocked_links(text)

        assert text == before
        assert _urls(records) == [url]
        assert "[REDACTED" not in json.dumps(records)

    def test_the_url_masking_keeps_a_flagged_url_whole(self) -> None:
        # The credential pass masks only a credential VALUE inside a URL; a long
        # query is not a credential, so the shown text keeps the URL as written
        # and the decision alone keeps it from rendering live.
        url = f"https://{_DOMAIN}/x?filter={_LONG_QUERY_TAIL}"
        assert exfil.redact_exfiltration_urls(f"see {url}") == (f"see {url}", [])
        assert _urls(blocked_links(f"see {url}")) == [url]


class TestRecordsMatchTheFlaggedLinks:
    def test_one_record_per_flagged_url_in_order(self) -> None:
        url_a = f"https://a.example.com/x?q={_LONG_QUERY_TAIL}"
        url_b = f"https://b.example.com/y?q={_LONG_QUERY_TAIL}"
        text = f"first {url_a} then {url_b}"

        records = blocked_links(text)

        assert scan_exfiltration_urls(text)
        assert [r["domain"] for r in records] == ["a.example.com", "b.example.com"]
        assert _urls(records) == [url_a, url_b]

    def test_repeated_url_yields_one_record(self) -> None:
        url = f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}"

        assert _urls(blocked_links(f"{url} and again {url}")) == [url]

    @pytest.mark.parametrize("longer_first", [False, True])
    def test_a_flagged_link_that_prefixes_a_longer_one_records_both_whole(
        self, longer_first: bool
    ) -> None:
        """A flagged link and the same link with a token appended are two links: each
        record is the exact address a renderer matches, so neither shelters the other."""
        short = f"https://{_DOMAIN}/x?d={_LONG_QUERY_TAIL}"
        longer = short + "&X-Amz-Security-Token=" + "T" * 120
        first, second = (longer, short) if longer_first else (short, longer)

        records = blocked_links(f"see {first} and {second} .")

        assert _urls(records) == [first, second]

    def test_records_carry_exactly_the_known_keys(self) -> None:
        url = f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}"

        [record] = blocked_links(f"see {url}")

        assert tuple(record) == bl.BLOCKED_LINK_RECORD_KEYS
        assert record == {
            "url": url,
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "query_chars": len(f"q={_LONG_QUERY_TAIL}"),
            "withheld": None,
        }

    def test_the_host_is_recorded_lowercase(self) -> None:
        url = f"https://Collect.Example.COM/x?q={_LONG_QUERY_TAIL}"

        [record] = blocked_links(url)

        assert record["domain"] == _DOMAIN and record["url"] == url

    def test_a_bracketed_ipv6_host_is_what_the_record_names(self) -> None:
        url = f"http://[::1]:8080/x?q={_LONG_QUERY_TAIL}"

        [record] = blocked_links(f"see {url}")

        assert record["domain"] == "[::1]" and record["url"] == url

    def test_every_record_names_a_rule_the_verdict_traced(self) -> None:
        texts = [
            f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}",
            f"https://{_DOMAIN}/x?d=" + "QUtJQUlPU0ZPRE5ON0VYQU1QTEU" * 3,
            f"https://{_DOMAIN}/p?q=" + "%41" * 30,
            f"https://{_DOMAIN}/x?k={_SECRET_LABEL}",
            f"https://{_DOMAIN}/x?k={_KEY_ID}",
        ]
        rules = [blocked_links(t)[0]["rule"] for t in texts]
        assert bl.UNTRACED_RULE not in rules
        assert rules[:3] == ["exfil_query_length", "exfil_query_pattern", "exfil_percent_encoding"]

    def test_there_is_no_count_cap_a_padded_reply_could_slip_a_link_past(self) -> None:
        """A link the list left out would render live, so forty decoys before a
        link never push it out of the records."""
        decoys = [f"https://d{i}.example.com/x?q={_LONG_QUERY_TAIL}" for i in range(40)]
        last = f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}"

        records = blocked_links(" ".join([*decoys, last]))

        assert _urls(records) == [*decoys, last]


class TestTheFullAddressRule:
    def test_a_link_without_a_credential_offers_its_address(self) -> None:
        query = f"leak=S3cr3tExfilPayloadValueThatMustNeverPersist0001{_LONG_QUERY_TAIL}"
        url = f"https://{_DOMAIN}/p?{query}"

        [record] = blocked_links(f"see {url}")

        assert record["query_chars"] == len(query)
        assert record["url"] == url and record["withheld"] is None

    def test_a_cjk_title_link_offers_its_address(self) -> None:
        title = "%E6%B5%8B%E8%AF%95%E9%A1%B5%E9%9D%A2%E6%A0%87%E9%A2%98%E6%96%87"
        url = f"https://wiki.example.com/view?title={title}"

        [record] = blocked_links(f"see {url}")

        assert record["url"] == url and record["withheld"] is None

    @pytest.mark.parametrize(
        "url",
        [
            f"https://{_DOMAIN}/dump/{_SECRET_LABEL}",
            f"https://{_DOMAIN}/x?k={_KEY_ID}",
            # Unchanged as literal text; a browser decodes %5F back into the
            # underscore that makes it a GitHub token.
            f"https://{_DOMAIN}/x/ghp%5F{'A' * 36}?q={_LONG_QUERY_TAIL}",
        ],
    )
    def test_a_link_carrying_a_credential_offers_nothing_to_open(self, url: str) -> None:
        [record] = blocked_links(f"see {url}")

        assert record["url"] == url
        assert record["withheld"] == "credential"

    def test_an_address_past_the_bound_offers_nothing_to_open(self) -> None:
        url = f"https://{_DOMAIN}/p?q=" + "a" * bl.MAX_BLOCKED_LINK_URL_CHARS

        [record] = blocked_links(f"see {url}")

        assert record["url"] == url and record["withheld"] == "length"

    def test_query_chars_zero_when_absent(self) -> None:
        [record] = blocked_links(f"see https://{_DOMAIN}/dump/{_KEY_ID}")

        assert record["query_chars"] == 0


class TestTheDecisionIgnoresTheRedactionSetting:
    def test_records_are_the_same_inside_a_bypassed_output_seam(self, monkeypatch) -> None:
        url = f"https://{_DOMAIN}/x?k={_SECRET_LABEL}&d={_LONG_QUERY_TAIL}"
        outside = blocked_links(url)
        monkeypatch.setattr(redaction_seams, "in_bypassed_seam", lambda: True)

        # The credential pass stands down here, as with the owner's setting off...
        assert redact_credentials(_SECRET_LABEL) == (_SECRET_LABEL, [])
        # ...and the decision, its full-address rule included, does not.
        assert blocked_links(url) == outside
        assert outside[0]["withheld"] == "credential"


class TestExemptionsLeaveTheSameLinksLive:
    def test_a_platform_exact_host_skips_the_length_and_base64_rules(self, monkeypatch) -> None:
        monkeypatch.setattr(exfil, "_exempt_exact_hosts", lambda: frozenset({"Tenant.Example.com"}))
        long_link = f"https://tenant.example.com/doc?q={_LONG_QUERY_TAIL}"
        cred_link = f"https://tenant.example.com/doc?k={_KEY_ID}"

        assert blocked_links(long_link) == []
        assert _urls(blocked_links(cred_link)) == [cred_link]

    def test_kiro_crews_own_slack_app_create_link_renders_live(self) -> None:
        from kiro_crew import slack_manifest

        link = slack_manifest.deep_link("kirocrew")
        assert len(link) > 1000

        assert blocked_links(f"Create the app: {link}") == []
        # One byte added to the payload makes it something other than the bundled template.
        assert blocked_links(link.replace("manifest_yaml=", "manifest_yaml=X")) != []

    def test_a_valid_presigned_s3_link_renders_live(self) -> None:
        url = (
            "https://my-bucket.s3.us-east-1.amazonaws.com/report.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            f"&X-Amz-Credential={_KEY_ID}%2F20261008%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20261008T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=" + "b" * 64
        )
        assert blocked_links(f"get {url}") == []

    def test_an_outlook_web_link_renders_live(self) -> None:
        from test_outlook_web_link_redaction import WEB_LINKS

        for link in WEB_LINKS:
            assert blocked_links(f"Draft: [the draft]({link}).") == [], link


class TestRecordsAreComputedWhenShownAndNeverSaved:
    def test_the_saved_row_keeps_the_url_and_holds_no_record(self) -> None:
        url = f"https://{_DOMAIN}/reports/summary?q={_LONG_QUERY_TAIL}"
        entry = chat_persistence._build_message_entry_uncached(
            {"role": "assistant", "content": f"see {url}", "ts": "2026-01-01T00:00:00Z"}
        )

        assert entry is not None
        assert entry["content"] == f"see {url}"
        assert cu.BLOCKED_LINKS_KEY not in (entry.get("meta") or {})

    def test_the_history_render_attaches_records_to_the_unchanged_text(self) -> None:
        url = f"https://{_DOMAIN}/reports/summary?q={_LONG_QUERY_TAIL}"
        rows = [{"role": "assistant", "content": f"see {url}", "ts": "1"}]

        [out] = cu._prepare_messages(rows, False, live_child="")

        assert out["content"] == f"see {url}"
        assert _urls(out["meta"][cu.BLOCKED_LINKS_KEY]) == [url]
        # The stored row is untouched.
        assert rows[0] == {"role": "assistant", "content": f"see {url}", "ts": "1"}

    def test_a_record_a_transcript_line_carries_is_dropped_and_recomputed(self) -> None:
        url = f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}"
        forged = {"url": "https://elsewhere.example.net/", "domain": "elsewhere.example.net"}
        retired = {"domain": _DOMAIN, "rule": "exfil_query_length", "path": None}
        rows = [
            {
                "role": "assistant",
                "content": f"see {url}",
                "ts": "1",
                "meta": {cu.BLOCKED_LINKS_KEY: [forged], "blocked_links": [retired]},
            },
            {
                "role": "assistant",
                "content": "no links",
                "ts": "2",
                "meta": {cu.BLOCKED_LINKS_KEY: [forged]},
            },
            {
                "role": "user",
                "content": f"mine {url}",
                "ts": "3",
                "meta": {cu.BLOCKED_LINKS_KEY: [forged]},
            },
        ]

        first, second, user = cu._prepare_messages(rows, False, live_child="")

        assert _urls(first["meta"][cu.BLOCKED_LINKS_KEY]) == [url]
        assert "blocked_links" not in first["meta"]
        assert cu.BLOCKED_LINKS_KEY not in (second.get("meta") or {})
        # A user's own message is shown as typed, and draws no card.
        assert user["content"] == f"mine {url}"
        assert cu.BLOCKED_LINKS_KEY not in (user.get("meta") or {})

    def test_an_older_builds_placeholder_records_round_trip_and_are_never_sent(
        self, tmp_path, monkeypatch
    ) -> None:
        """A transcript an older build wrote holds a suspicious-URL placeholder with its
        URL kept under ``meta.blocked_links``: a reload and the next save write the row
        back as it was, and no client is sent the retired records."""
        from types import SimpleNamespace

        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_persistence as cp
        from kiro_crew.dashboard.slot_persistence.message_entries import (
            _build_message_entry_uncached,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        retired = [
            {
                "url": f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}",
                "domain": _DOMAIN,
                "rule": "exfil_query_length",
            }
        ]
        meta = {"blocked_links": retired, "blocked_links_ordered": True}
        variant = {"content": "older", "ts": "0", "blocked_links": retired}
        line = {
            "role": "assistant",
            "content": f"see [REDACTED: suspicious URL to {_DOMAIN}] now",
            "ts": "1",
            "meta": meta,
            "variants": [variant, {"content": "see it", "ts": "1"}],
        }
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s-retired")
        cp._load_rehydrated_window(
            state, slot, [dict(line)], SimpleNamespace(settle=lambda rows: None)
        )
        loaded = slot.messages[-1]
        assert loaded["meta"]["blocked_links"] == retired
        assert loaded["meta"]["blocked_links_ordered"] is True

        saved = _build_message_entry_uncached(loaded)
        assert saved["content"] == line["content"]
        assert saved["meta"]["blocked_links"] == retired
        assert saved["meta"]["blocked_links_ordered"] is True
        assert saved["variants"][0]["blocked_links"] == retired

        [shown] = cu._prepare_messages([loaded], False, live_child="")
        assert "blocked_links" not in (shown.get("meta") or {})
        assert "blocked_links_ordered" not in (shown.get("meta") or {})
        assert all("blocked_links" not in v for v in shown.get("variants") or [])

    def test_a_variant_carries_the_records_of_its_own_text(self) -> None:
        url = f"https://{_DOMAIN}/v?q={_LONG_QUERY_TAIL}"
        forged = [{"url": "https://elsewhere.example.net/"}]
        variants = [
            {"content": f"older {url}", "ts": "0", cu.BLOCKED_LINKS_KEY: forged},
            {"content": "newer, clean", "ts": "1", cu.BLOCKED_LINKS_KEY: forged},
        ]
        rows = [{"role": "assistant", "content": "newer, clean", "ts": "1", "variants": variants}]

        older, newer = cu._prepare_messages(rows, False, live_child="")[0]["variants"]

        assert older["content"] == f"older {url}"
        assert _urls(older[cu.BLOCKED_LINKS_KEY]) == [url]
        assert cu.BLOCKED_LINKS_KEY not in newer

    def test_a_streamed_reply_carries_the_records_of_the_text_so_far(self) -> None:
        url = f"https://{_DOMAIN}/s?q={_LONG_QUERY_TAIL}"
        rows = [{"role": "chunk", "content": f"partial {url} "}]

        [out] = cu._prepare_messages(rows, True, live_child="")

        assert out["role"] == "streaming" and out["content"] == f"partial {url} "
        assert _urls(out["meta"][cu.BLOCKED_LINKS_KEY]) == [url]

    def test_the_live_frame_attaches_records_to_the_unchanged_text(self) -> None:
        url = f"https://{_DOMAIN}/live?q={_LONG_QUERY_TAIL}"
        msg = {
            "role": "assistant",
            "content": f"see {url}",
            "ts": "1",
            "meta": {cu.BLOCKED_LINKS_KEY: [{"url": "forged"}], "mid": "m1"},
        }

        note = chat_message_note("s1", msg)

        assert note["content"] == f"see {url}"
        assert _urls(note["meta"][cu.BLOCKED_LINKS_KEY]) == [url]
        assert note["meta"]["mid"] == "m1"
        assert msg["meta"][cu.BLOCKED_LINKS_KEY] == [{"url": "forged"}]

    def test_the_live_frame_of_another_role_draws_no_card(self) -> None:
        url = f"https://{_DOMAIN}/tool?q={_LONG_QUERY_TAIL}"
        note = chat_message_note(
            "s1",
            {
                "role": "tool",
                "content": f"out {url}",
                "ts": "1",
                "meta": {cu.BLOCKED_LINKS_KEY: [1]},
            },
        )
        assert cu.BLOCKED_LINKS_KEY not in (note.get("meta") or {})

    def test_an_allowed_hosts_change_misses_the_row_memo(self, monkeypatch) -> None:
        from kiro_crew.dashboard import wire_render

        wire_render.clear_render_memo()
        hosts = {"value": frozenset()}
        monkeypatch.setattr(cu, "allowed_hosts_for", lambda _ws: hosts["value"])
        url = f"https://{_DOMAIN}/memo?q={_LONG_QUERY_TAIL}"
        rows = [{"role": "assistant", "content": f"see {url}", "ts": "1"}]

        blocked = json.loads(
            wire_render.prepare_messages_json(rows, False, live_child="", workspace="w")
        )
        hosts["value"] = frozenset({_DOMAIN})
        allowed = json.loads(
            wire_render.prepare_messages_json(rows, False, live_child="", workspace="w")
        )
        hosts["value"] = frozenset()
        again = json.loads(
            wire_render.prepare_messages_json(rows, False, live_child="", workspace="w")
        )

        assert _urls(blocked[0]["meta"][cu.BLOCKED_LINKS_KEY]) == [url]
        assert cu.BLOCKED_LINKS_KEY not in (allowed[0].get("meta") or {})
        assert again == blocked
        assert blocked[0]["content"] == allowed[0]["content"] == f"see {url}"
        wire_render.clear_render_memo()


class TestAReplyThroughARealTurn:
    """Through the real ``_run_chat``: the saved row and the stream keep the URL as
    written, and the row's live frame carries the record that keeps it from
    rendering live."""

    @pytest.mark.asyncio
    async def test_the_saved_text_is_unchanged_and_the_live_frame_carries_the_record(self) -> None:
        from turn_harness import TurnScript, run_turn

        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            STOP_REASON_END_TURN,
            AcpEvent,
        )

        url = "https://h.example-sink.net/report?q=" + "x" * 250
        record = await run_turn(
            TurnScript(
                events=[
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text="See "),
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text=url),
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text=" for the report."),
                    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
                ]
            )
        )

        [row] = [r for r in record.rows("assistant") if "for the report" in str(r.get("content"))]
        assert url in row["content"]
        assert cu.BLOCKED_LINKS_KEY not in (row.get("meta") or {})
        streamed = "".join(str(f.payload.get("content", "")) for f in record.frames("chat_chunk"))
        assert url in streamed
        notes = [
            f.payload
            for f in record.frames("chat_message")
            if f.payload.get("role") == "assistant" and url in str(f.payload.get("content"))
        ]
        assert notes, "the reply's row reached no open tab"
        assert all(_urls(n["meta"][cu.BLOCKED_LINKS_KEY]) == [url] for n in notes)


class TestALegacyPlaceholderIsDecidedNow:
    """An older build's suspicious-URL placeholder, with its address kept beside it,
    goes out as that address in the emitted copy, so today's decisions apply."""

    def _legacy(self, url: str, **extra: object) -> dict:
        return {
            "role": "assistant",
            "content": f"see [REDACTED: suspicious URL to {_DOMAIN}] now",
            "ts": "1",
            "meta": {
                "blocked_links": [{"url": url, "domain": _DOMAIN, "rule": "exfil_query_length"}],
                **extra,
            },
        }

    def test_the_address_comes_back_and_draws_its_chip(self) -> None:
        url = f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}"
        row = self._legacy(url)
        [shown] = cu._prepare_messages([row], False, live_child="")
        assert shown["content"] == f"see {url} now"
        assert _urls(shown["meta"][cu.BLOCKED_LINKS_KEY]) == [url]
        assert row["content"].startswith("see [REDACTED: suspicious URL")

    def test_a_record_for_another_host_leaves_the_placeholder(self) -> None:
        row = self._legacy(f"https://elsewhere.example.net/x?q={_LONG_QUERY_TAIL}")
        [shown] = cu._prepare_messages([row], False, live_child="")
        assert "[REDACTED: suspicious URL to " in shown["content"]

    def test_two_placeholders_and_one_record_leave_both(self) -> None:
        url = f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}"
        holder = {"blocked_links": [{"url": url, "domain": _DOMAIN}]}
        text = (
            f"[REDACTED: suspicious URL to {_DOMAIN}] and [REDACTED: suspicious URL to {_DOMAIN}]"
        )
        assert bl.restore_legacy_blocked_links(text, holder) == text

    def test_a_regenerated_reply_keeps_the_address_of_its_older_text(self) -> None:
        url = f"https://{_DOMAIN}/x?q={_LONG_QUERY_TAIL}"
        row = self._legacy(url, blocked_links_ordered=True)
        stashed = cu.variant_from_row(row)
        assert stashed["blocked_links"] == row["meta"]["blocked_links"]
        fresh = {"role": "assistant", "content": "a new reply", "ts": "2", "meta": {}}
        cu.adopt_variant_text(fresh, stashed)
        [shown] = cu._prepare_messages([fresh], False, live_child="")
        assert shown["content"] == f"see {url} now"


class TestASubagentCompletionCarriesItsRecords:
    """A subagent completion's card draws its text as markdown, so its row carries the
    blocked-link records of that text, whatever role the row has."""

    def test_the_history_render_attaches_records_to_a_completion(self) -> None:
        url = f"https://{_DOMAIN}/c?q={_LONG_QUERY_TAIL}"
        content = f"[Subagent completion event]\nAgent `a1` completed: [report]({url})"
        for role in ("user", "subagent", "system"):
            [out] = cu._prepare_messages(
                [{"role": role, "content": content, "ts": "1"}], False, live_child=""
            )
            assert _urls(out["meta"][cu.BLOCKED_LINKS_KEY]) == [url], role

    def test_the_live_frame_attaches_them_too(self) -> None:
        url = f"https://{_DOMAIN}/c?q={_LONG_QUERY_TAIL}"
        msg = {"role": "user", "content": f"[Subagent completion event]\n[r]({url})", "ts": "1"}
        note = chat_message_note("s1", msg)
        assert _urls(note["meta"][cu.BLOCKED_LINKS_KEY]) == [url]
