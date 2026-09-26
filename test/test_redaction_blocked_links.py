"""Backend contract for the blocked-link chip's structure-only records.

The exfiltration redactor keeps only the domain in the saved transcript, so a
chip built from that text can show nothing but the site name. Step 3 adds a
one-loop collector that emits, per redacted URL, a JSON-serialisable record
retaining STRUCTURE only -- domain, rule id, a path kept only when both removers
would pass it, and the query's char COUNT -- never the query value. These tests
pin the record shape, the retention rule, the dedupe, the persistence attach
point, and the attacker-writable-meta carve-out.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard.chat_utils import (
    _prepare_messages,
    _redact_meta_for_role,
    adopt_variant_text,
    variant_from_row,
)
from kiro_crew.security import (
    bounded_blocked_links,
    exfil,
    redact_credentials,
    redact_exfiltration_urls,
    redact_exfiltration_urls_with_records,
)
from kiro_crew.security.exfil import _exfil_url_warning, revalidate_blocked_link

_DOMAIN = "collect.example.com"
_LONG_QUERY_TAIL = "A" * 250
# A host label the credential remover rewrites, which is what these cases need,
# and deliberately NOT an access-key id, an ARN or an account id: the scope lane's
# candidate corpus travels as a public artifact and refuses a credential shape it
# cannot attribute to a field, so a fixture spelled like a cloud key costs that
# lane its run without making the test any stronger.
_SECRET_LABEL = "ghp_" + "k7qmz2x9wv4tb8ncr6yd3fj5hs1gpl0aeuo2"


def _placeholder_count(text: str) -> int:
    from kiro_crew.security import EXFILTRATION_REDACTION_TAG_PREFIX

    return text.count(EXFILTRATION_REDACTION_TAG_PREFIX)


class TestCollectorRecordsMatchRedaction:
    def test_one_record_per_redacted_url_in_order(self) -> None:
        url_a = f"https://a.example.com/x?token={_LONG_QUERY_TAIL}"
        url_b = f"https://b.example.com/y?token={_LONG_QUERY_TAIL}"
        text = f"first {url_a} then {url_b}"

        cleaned, warnings, records = redact_exfiltration_urls_with_records(text)

        assert warnings
        assert _placeholder_count(cleaned) == 2
        assert [r["domain"] for r in records] == ["a.example.com", "b.example.com"]

    def test_repeated_url_yields_one_record(self) -> None:
        url = f"https://{_DOMAIN}/x?token={_LONG_QUERY_TAIL}"
        text = f"{url} and again {url}"

        cleaned, _, records = redact_exfiltration_urls_with_records(text)

        # Every occurrence is redacted, but the record set is deduped by the
        # matched URL string in first-appearance order.
        assert _placeholder_count(cleaned) == 2
        assert len(records) == 1

    def test_records_carry_no_positional_field(self) -> None:
        url = f"https://{_DOMAIN}/x?token={_LONG_QUERY_TAIL}"

        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records
        for record in records:
            assert set(record.keys()) == {
                "domain",
                "rule",
                "path",
                "query_chars",
                "url",
                "url_withheld",
            }


class TestPathRetention:
    def test_clean_path_is_kept(self) -> None:
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"

        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records[0]["path"] == "/reports/summary"

    def test_path_that_itself_trips_the_rule_is_not_kept(self) -> None:
        # Heavy percent encoding in the PATH trips the redactor on the path
        # alone -- retention check 1 fails -- so nothing is kept.
        percent_path = "/" + "%41" * 25
        url = f"https://{_DOMAIN}{percent_path}"
        assert _exfil_url_warning(_DOMAIN, percent_path, frozenset()) is not None

        _, warnings, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert warnings
        assert records[0]["path"] is None

    def test_path_carrying_a_credential_is_not_kept(self) -> None:
        # A bare secret run the query heuristics never see (they scan only the
        # query) passes the URL warning on the path alone but is stripped by
        # credential redaction -- retention check 2 is what drops it.
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        cred_path = f"/d/{secret}"
        assert _exfil_url_warning(_DOMAIN, cred_path, frozenset()) is None
        assert redact_credentials(cred_path)[0] != cred_path

        url = f"https://{_DOMAIN}{cred_path}?token={_LONG_QUERY_TAIL}"
        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records[0]["path"] is None


class TestTheFullAddressIsKeptSoItCanBeOpened:
    def test_a_link_without_a_credential_keeps_its_whole_address(self) -> None:
        # The common false positive: an ordinary long query that trips the
        # length rule. The reader needs it back, so it is kept verbatim.
        query = f"leak=S3cr3tExfilPayloadValueThatMustNeverPersist0001{_LONG_QUERY_TAIL}"
        url = f"https://{_DOMAIN}/p?{query}"

        cleaned, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records[0]["query_chars"] == len(query)
        assert records[0]["url"] == url
        assert records[0]["url_withheld"] is None
        # The address lives in the record only; the text keeps the placeholder.
        assert url not in cleaned

    def test_a_cjk_title_link_keeps_its_whole_address(self) -> None:
        title = "%E6%B5%8B%E8%AF%95%E9%A1%B5%E9%9D%A2%E6%A0%87%E9%A2%98%E6%96%87"
        url = f"https://wiki.example.com/view/{title}"

        _, warnings, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert warnings
        assert records[0]["url"] == url

    def test_a_link_carrying_a_credential_is_withheld(self) -> None:
        url = f"https://{_DOMAIN}/dump/AKIAIOSFODNN7EXAMPLE"

        _, warnings, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert warnings
        assert records[0]["query_chars"] == 0
        assert records[0]["url"] is None
        assert records[0]["url_withheld"] == "credential"
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(records)

    def test_a_partially_encoded_credential_is_withheld(self) -> None:
        # Unchanged by the remover as literal text; a browser decodes %5F back
        # into the underscore that makes it a GitHub token.
        token_tail = "A" * 36
        url = f"https://{_DOMAIN}/x/ghp%5F{token_tail}?q={_LONG_QUERY_TAIL}"
        assert redact_credentials(url)[0] == url

        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records[0]["url"] is None
        assert records[0]["url_withheld"] == "credential"
        assert records[0]["path"] is None

    def test_an_address_past_the_bound_is_withheld_not_truncated(self) -> None:
        url = f"https://{_DOMAIN}/p?q=" + "a" * exfil.MAX_BLOCKED_LINK_URL_CHARS

        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records[0]["url"] is None
        assert records[0]["url_withheld"] == "length"

    def test_a_forged_address_on_another_host_is_dropped(self) -> None:
        forged = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": 3,
            "url": "https://attacker.example.net/?q=abc",
            "url_withheld": None,
        }
        assert revalidate_blocked_link(forged) is None

    def test_a_forged_non_http_address_is_dropped(self) -> None:
        forged = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": 3,
            "url": f"javascript://{_DOMAIN}/%0Aalert(1)",
            "url_withheld": None,
        }
        assert revalidate_blocked_link(forged) is None

    def test_a_forged_address_with_userinfo_is_dropped(self) -> None:
        forged = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": 3,
            "url": f"https://{_DOMAIN}@attacker.example.net/",
            "url_withheld": None,
        }
        assert revalidate_blocked_link(forged) is None

    def test_a_record_with_both_or_neither_half_is_dropped(self) -> None:
        base = {"domain": _DOMAIN, "rule": "exfil_query_length", "path": None, "query_chars": 3}
        both = {**base, "url": f"https://{_DOMAIN}/?q=abc", "url_withheld": "length"}
        neither = {**base, "url": None, "url_withheld": None}
        unknown = {**base, "url": None, "url_withheld": "because"}
        assert revalidate_blocked_link(both) is None
        assert revalidate_blocked_link(neither) is None
        assert revalidate_blocked_link(unknown) is None

    def test_a_kept_address_survives_the_read_back(self) -> None:
        url = f"https://{_DOMAIN}/p?q={_LONG_QUERY_TAIL}"
        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert bounded_blocked_links(records) == records

    def test_a_forged_credential_address_is_dropped_on_read_back(self) -> None:
        # A transcript line is attacker-writable: a credential written straight
        # into the url field is re-checked on serve, never trusted.
        forged = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": 0,
            "url": f"https://{_DOMAIN}/dump/AKIAIOSFODNN7EXAMPLE",
            "url_withheld": None,
        }
        assert revalidate_blocked_link(forged) is None


class TestQueryIsCounted:
    def test_query_chars_zero_when_absent(self) -> None:
        url = f"https://{_DOMAIN}/dump/AKIAIOSFODNN7EXAMPLE"

        _, warnings, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert warnings
        assert records[0]["query_chars"] == 0


class TestExistingCallersUnaffected:
    def test_clean_text_returns_unchanged_two_tuple(self) -> None:
        text = "nothing suspicious https://example.com/docs here"

        result = redact_exfiltration_urls(text)

        assert result == (text, [])

    def test_blocked_url_two_tuple_matches_collector(self) -> None:
        from kiro_crew.security import EXFILTRATION_REDACTION_TAG_PREFIX

        url = f"https://{_DOMAIN}/x?token={_LONG_QUERY_TAIL}"
        text = f"see {url}"

        cleaned, warnings = redact_exfiltration_urls(text)
        cleaned2, warnings2, _ = redact_exfiltration_urls_with_records(text)

        assert (cleaned, warnings) == (cleaned2, warnings2)
        assert f"{EXFILTRATION_REDACTION_TAG_PREFIX}{_DOMAIN}]" in cleaned


class TestPersistenceAttachesRecords:
    def test_a_row_keeps_the_records_it_arrives_with(self) -> None:
        # The shape the live path produces: the text is ALREADY the placeholder,
        # because the redaction that made it is where the records were born.
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"
        redacted, _, records = redact_exfiltration_urls_with_records(f"see {url}")
        entry = chat_persistence._build_message_entry_uncached(
            {
                "role": "assistant",
                "content": redacted,
                "ts": "2026-01-01T00:00:00Z",
                "meta": {"blocked_links": records},
            }
        )

        assert entry is not None
        blocked = entry["meta"]["blocked_links"]
        assert blocked[0]["domain"] == _DOMAIN
        assert blocked[0]["path"] == "/reports/summary"
        assert url not in entry["content"]

    def test_persistence_does_not_invent_records_from_a_placeholder(self) -> None:
        # A scan of this row can only see the placeholder, so re-deriving here
        # would describe nothing. The absence is the point: records are carried,
        # never re-derived, and a row that arrives without them stays without.
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"
        redacted, _, _ = redact_exfiltration_urls_with_records(f"see {url}")
        entry = chat_persistence._build_message_entry_uncached(
            {"role": "assistant", "content": redacted, "ts": "2026-01-01T00:00:00Z"}
        )

        assert entry is not None
        assert "blocked_links" not in (entry.get("meta") or {})

    def test_clean_message_omits_the_key(self) -> None:
        entry = chat_persistence._build_message_entry_uncached(
            {"role": "assistant", "content": "no links here", "ts": "2026-01-01T00:00:00Z"}
        )

        assert entry is not None
        assert "meta" not in entry

    def test_user_branch_is_not_redacted(self) -> None:
        url = f"https://{_DOMAIN}/x?token={_LONG_QUERY_TAIL}"
        entry = chat_persistence._build_message_entry_uncached(
            {"role": "user", "content": f"see {url}", "ts": "2026-01-01T00:00:00Z"}
        )

        assert entry is not None
        assert entry["content"] == f"see {url}"
        assert "meta" not in entry


class TestRetentionIsBounded:
    """The store is the transcript line, so both retention points carry a bound.

    A record is cheap to write into a line, and every render of the message that
    holds it re-validates and re-serializes whatever it holds.
    """

    def test_the_record_count_is_capped_but_the_text_is_fully_redacted(self) -> None:
        cap = exfil.MAX_BLOCKED_LINKS_PER_MESSAGE
        urls = [f"https://h{i}.example.com/x?token={_LONG_QUERY_TAIL}" for i in range(cap + 5)]
        text = " ".join(urls)

        cleaned, _, records = redact_exfiltration_urls_with_records(text)

        assert len(records) == cap
        # The cap bounds what is DESCRIBED, never what is removed.
        for url in urls:
            assert url not in cleaned
        assert _placeholder_count(cleaned) == cap + 5

    def test_an_overlong_path_is_dropped_not_truncated(self) -> None:
        long_path = "/" + "a" * (exfil.MAX_BLOCKED_LINK_PATH_CHARS + 1)
        url = f"https://{_DOMAIN}{long_path}?token={_LONG_QUERY_TAIL}"

        _, _, records = redact_exfiltration_urls_with_records(url)

        assert len(records) == 1
        assert records[0]["path"] is None
        assert records[0]["domain"] == _DOMAIN

    def test_revalidation_rejects_an_absurd_query_count(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "long_query",
            "path": None,
            "query_chars": exfil.MAX_BLOCKED_LINK_QUERY_CHARS + 1,
            "url": None,
            "url_withheld": "length",
        }

        assert revalidate_blocked_link(record) is None

    def test_revalidation_rejects_an_overlong_path(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "long_query",
            "path": "/" + "a" * (exfil.MAX_BLOCKED_LINK_PATH_CHARS + 1),
            "query_chars": 256,
            "url": None,
            "url_withheld": "length",
        }

        assert revalidate_blocked_link(record) is None

    def test_a_forged_line_carrying_many_valid_records_is_capped(self) -> None:
        cap = exfil.MAX_BLOCKED_LINKS_PER_MESSAGE
        forged = [
            {
                "domain": f"h{i}.example.com",
                "rule": "long_query",
                "path": None,
                "query_chars": 256,
                "url": None,
                "url_withheld": "length",
            }
            for i in range(cap * 4)
        ]

        out = _redact_meta_for_role("assistant", {"blocked_links": forged})

        assert len(out["blocked_links"]) == cap

    def test_every_retained_string_has_a_bound(self) -> None:
        # The validator reads this table rather than each field by name, so a
        # field added without an entry is not retained. Pinning the table against
        # the record's own key set is what makes that hold for a FUTURE field.
        bounded = set(exfil._BLOCKED_LINK_STRING_BOUNDS)
        assert bounded == exfil._BLOCKED_LINK_RECORD_KEYS - {"query_chars"}
        assert all(limit > 0 for limit in exfil._BLOCKED_LINK_STRING_BOUNDS.values())

    def test_revalidation_rejects_an_overlong_domain_and_rule(self) -> None:
        over_domain = "a" * 300 + ".example.com"
        assert (
            revalidate_blocked_link(
                {
                    "domain": over_domain,
                    "rule": "long_query",
                    "path": None,
                    "query_chars": 1,
                    "url": None,
                    "url_withheld": "length",
                }
            )
            is None
        )
        assert (
            revalidate_blocked_link(
                {
                    "domain": _DOMAIN,
                    "rule": "r" * 200,
                    "path": None,
                    "query_chars": 1,
                    "url": None,
                    "url_withheld": "length",
                }
            )
            is None
        )

    def test_the_dedupe_set_stops_growing_at_the_cap(self) -> None:
        cap = exfil.MAX_BLOCKED_LINKS_PER_MESSAGE
        urls = [f"https://h{i}.example.com/x?token={_LONG_QUERY_TAIL}" for i in range(cap + 6)]
        # Each URL twice, so a dedupe row past the cap would be reachable twice.
        text = " ".join(urls + urls)

        cleaned, _, records = redact_exfiltration_urls_with_records(text)

        assert len(records) == cap
        for url in urls:
            assert url not in cleaned


class TestRecordsReachTheDisplayPath:
    """The display path reads the ROW's meta, so the records have to be on it.

    `_prepare_messages` hands `m["meta"]` to `_redact_meta_for_role`, and
    `slot.append` broadcasts the live frame from inside the call, so the records
    go into the meta that call carries rather than onto the row afterwards.
    """

    def test_segment_meta_carries_the_records(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_decisions_strip_meta", lambda _slot: None)
        records = [
            {
                "domain": _DOMAIN,
                "rule": "exfil_query_length",
                "path": "/p",
                "query_chars": 9,
                "url": None,
                "url_withheld": "length",
            }
        ]

        assert chat_runner._segment_row_meta(object(), records) == {"blocked_links": records}

    def test_segment_meta_is_none_when_there_is_nothing_to_carry(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_decisions_strip_meta", lambda _slot: None)

        assert chat_runner._segment_row_meta(object(), []) is None

    def test_segment_meta_keeps_the_decision_strip_beside_them(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(
            chat_runner, "_decisions_strip_meta", lambda _slot: {"decisions_strip": ["x"]}
        )
        records = [
            {
                "domain": _DOMAIN,
                "rule": "exfil_query_length",
                "path": None,
                "query_chars": 9,
                "url": None,
                "url_withheld": "length",
            }
        ]

        out = chat_runner._segment_row_meta(object(), records)

        assert out == {"decisions_strip": ["x"], "blocked_links": records}

    def test_the_segment_flush_collects_records_at_its_own_redaction(self) -> None:
        # A source guard, because the wiring is the defect: a flush that calls the
        # two-tuple redactor stores the placeholder and the chip never renders,
        # and no unit on the persistence layer can see that.
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._flush_segment)
        assert "_redact_segment(slot, assistant_text)" in src
        assert "_segment_row_meta(slot, blocked_links, redactions)" in src
        helper = inspect.getsource(chat_runner._redact_segment_text)
        assert "redact_exfiltration_urls_with_records(" in helper
        assert "redact_credentials_with_records(" in helper

    def test_an_interrupted_reply_collects_records_at_its_own_redaction(self) -> None:
        # Same guard for the recovery path: a stopped or failed turn persists its
        # partial text through this nested helper, and a two-tuple redactor there
        # would store only the placeholder, losing a wrongly blocked link for good.
        import inspect

        from kiro_crew.dashboard import chat_runner

        lines = inspect.getsource(chat_runner).splitlines()
        start = next(i for i, ln in enumerate(lines) if "def _persist_partial_reply(" in ln)
        indent = len(lines[start]) - len(lines[start].lstrip())
        body = []
        for ln in lines[start + 1 :]:
            if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
                break
            body.append(ln)
        src = "\n".join(body)
        assert "_redact_segment(slot, body)" in src
        assert "_segment_row_meta(slot, _blocked, _redactions)" in src
        assert "redact_exfiltration_urls(body)" not in src


class TestRecordsSurviveAReopenedSession:
    """A row's records travel with it: born at the redaction, carried thereafter.

    The load path redacts content on the way in, so a rehydrated row holds the
    placeholder. Content survives a reopen because redaction is idempotent;
    records are not derivable from it at all, which is why they are carried.
    """

    def test_carried_records_survive_a_reserialisation(self) -> None:
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"
        redacted, _, saved = redact_exfiltration_urls_with_records(f"see {url}")

        rehydrated = chat_persistence._build_message_entry_uncached(
            {
                "role": "assistant",
                "content": redacted,
                "ts": "2026-01-01T00:00:00Z",
                "meta": {"blocked_links": saved},
            }
        )

        assert rehydrated is not None
        assert rehydrated["meta"]["blocked_links"] == saved

    def test_carried_records_are_dropped_without_a_placeholder(self) -> None:
        # Records DESCRIBE placeholders, so a row whose text has none does not
        # keep them -- that pairing is the only thing making them meaningful.
        entry = chat_persistence._build_message_entry_uncached(
            {
                "role": "assistant",
                "content": "no links here",
                "ts": "2026-01-01T00:00:00Z",
                "meta": {
                    "blocked_links": [
                        {
                            "domain": _DOMAIN,
                            "rule": "exfil_query_length",
                            "path": "/reports/summary",
                            "query_chars": 256,
                            "url": None,
                            "url_withheld": "length",
                        }
                    ]
                },
            }
        )

        assert entry is not None
        assert "blocked_links" not in (entry.get("meta") or {})

    def test_a_carried_record_is_still_revalidated(self) -> None:
        url = f"https://{_DOMAIN}/reports/summary?token={_LONG_QUERY_TAIL}"
        redacted, _, saved = redact_exfiltration_urls_with_records(f"see {url}")

        entry = chat_persistence._build_message_entry_uncached(
            {
                "role": "assistant",
                "content": redacted,
                "ts": "2026-01-01T00:00:00Z",
                "meta": {
                    "blocked_links": [
                        {
                            "domain": "not a host",
                            "rule": "exfil_query_length",
                            "path": None,
                            "query_chars": 1,
                            "url": None,
                            "url_withheld": "length",
                        },
                        saved[0],
                    ]
                },
            }
        )

        assert entry is not None
        assert entry["meta"]["blocked_links"] == [saved[0]]


class TestTheDedupeSetIsBoundedByTheRecordsItServes:
    """`seen` holds whole URLs, and only the record cap beside it bounds them.

    So every rejection has to happen before it grows: a dedupe row for a match
    that produces no record is state nothing bounds.
    """

    def test_rejected_urls_do_not_grow_the_dedupe_set(self) -> None:
        secret = _SECRET_LABEL
        # Each URL is unique AND rejected: a credential-shaped host, so no record
        # is retained and the cap never rises to bound the dedupe rows.
        urls = [
            f"https://{secret}.h{i}.example.com/p?token={_LONG_QUERY_TAIL}"
            for i in range(exfil.MAX_BLOCKED_LINKS_PER_MESSAGE * 3)
        ]
        text = " ".join(urls)

        cleaned, _, records = redact_exfiltration_urls_with_records(text)

        assert records == []
        # The removal never depends on any of this.
        for url in urls:
            assert url not in cleaned

    def test_a_host_the_reload_would_drop_is_never_retained(self) -> None:
        # Over the domain bound: retained at collection and dropped on read-back
        # makes the explanation vanish under the reader after a reload.
        label = "a" * 260
        url = f"https://{label}.example.com/p?token={_LONG_QUERY_TAIL}"

        cleaned, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert url not in cleaned
        for record in records:
            assert revalidate_blocked_link(record) == record

    def test_every_collected_record_survives_its_own_read_back(self) -> None:
        # The property the two paths must share, stated once: anything collection
        # keeps, the read-back keeps.
        text = " ".join(
            f"https://h{i}.example.com/reports/summary?token={_LONG_QUERY_TAIL}" for i in range(5)
        )

        _, _, records = redact_exfiltration_urls_with_records(text)

        assert records
        assert bounded_blocked_links(records) == records


class TestARetainedHostObeysTheRemovers:
    """The host answers to the rule the retained path already answers to.

    A retained string is kept only when both removers pass it unchanged. A DNS
    label can be shaped like a credential, and a record carrying one would walk
    that secret past the text redaction into meta and onto the chip.
    """

    def test_a_credential_shaped_host_is_not_retained_at_collection(self) -> None:
        secret = _SECRET_LABEL
        url = f"https://{secret}.collect.example.com/p?token={_LONG_QUERY_TAIL}"

        cleaned, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        # The URL still goes, which is the part that must never depend on this.
        assert url not in cleaned
        assert all(secret not in r["domain"] for r in records)

    def test_a_forged_credential_host_is_dropped_on_read_back(self) -> None:
        record = {
            "domain": f"{_SECRET_LABEL}.collect.example.com",
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": 9,
            "url": None,
            "url_withheld": "length",
        }

        assert revalidate_blocked_link(record) is None
        assert bounded_blocked_links([record]) == []

    def test_an_underscored_host_is_retained_like_any_other(self) -> None:
        # The DNS branch admits `_`, which internal and SRV names carry. Pinned
        # here because the render side mirrors THIS gate: a shape the backend
        # keeps and the renderer drops costs the reader the explanation for a
        # placeholder that is still sitting in the text.
        record = {
            "domain": "my_service.example.com",
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": 9,
            "url": None,
            "url_withheld": "length",
        }

        assert revalidate_blocked_link(record) == record

    def test_an_ordinary_host_still_passes(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": 9,
            "url": None,
            "url_withheld": "length",
        }

        assert revalidate_blocked_link(record) == record


class TestNoRuleIdIsEverInvented:
    """An id nobody can look up is worse than no record at all."""

    def test_a_bracketed_ipv6_host_is_what_the_placeholder_carries(self) -> None:
        # Pins what the renderer has to parse: the placeholder and the record
        # carry the host exactly as the URL spelled it, brackets included.
        url = f"https://[2001:db8::1]/reports?token={_LONG_QUERY_TAIL}"

        cleaned, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert url not in cleaned
        if records:
            assert records[0]["domain"] == "[2001:db8::1]"
            assert "[2001:db8::1]" in cleaned

    def test_collection_emits_only_ids_the_tracer_produces(self) -> None:
        url = f"https://{_DOMAIN}/reports?token={_LONG_QUERY_TAIL}"

        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")

        assert records
        for record in records:
            assert record["rule"] != "exfil_unknown"
            assert record["rule"].startswith("exfil_")

    def test_the_sentinel_id_appears_nowhere_in_the_collector(self) -> None:
        import inspect

        src = inspect.getsource(exfil.redact_exfiltration_urls_with_records)

        assert "exfil_unknown" not in src


class TestOneBoundedConstructor:
    """Every list of records read back off a line comes from one function.

    Each retained field's length is already a property of the record shape; the
    COUNT is a property of this constructor, so a site that reads records cannot
    retain an unbounded number of them by forgetting to slice.
    """

    @staticmethod
    def _records(count: int) -> list[dict]:
        return [
            {
                "domain": f"h{i}.example.com",
                "rule": "exfil_query_length",
                "path": "/p",
                "query_chars": 9,
                "url": None,
                "url_withheld": "length",
            }
            for i in range(count)
        ]

    def test_a_forged_line_cannot_retain_more_than_the_cap(self) -> None:
        over = exfil.MAX_BLOCKED_LINKS_PER_MESSAGE + 8

        kept = bounded_blocked_links(self._records(over))

        assert len(kept) == exfil.MAX_BLOCKED_LINKS_PER_MESSAGE

    def test_the_truncated_tail_is_counted_and_said(self, caplog) -> None:
        # A silently dropped tail reads like a message that never had those links.
        caplog.set_level("WARNING", logger=exfil.logger.name)

        bounded_blocked_links(self._records(exfil.MAX_BLOCKED_LINKS_PER_MESSAGE + 8))

        said = [r.getMessage() for r in caplog.records if "exceed the cap" in r.getMessage()]
        assert len(said) == 1
        assert "8 further record(s)" in said[0]

    def test_a_list_within_the_cap_says_nothing(self, caplog) -> None:
        caplog.set_level("WARNING", logger=exfil.logger.name)

        bounded_blocked_links(self._records(exfil.MAX_BLOCKED_LINKS_PER_MESSAGE))

        assert not [r for r in caplog.records if "exceed the cap" in r.getMessage()]

    def test_it_drops_only_the_records_that_fail(self) -> None:
        raw = [
            {
                "domain": "not a host",
                "rule": "exfil_query_length",
                "path": None,
                "query_chars": 1,
                "url": None,
                "url_withheld": "length",
            }
        ]
        raw.extend(self._records(2))

        kept = bounded_blocked_links(raw)

        assert len(kept) == 2

    def test_anything_that_is_not_a_list_yields_nothing(self) -> None:
        for raw in ({"domain": "a.example.com"}, "records", 7, None):
            assert bounded_blocked_links(raw) == []

    def test_both_retention_points_ask_this_one_function(self) -> None:
        # A source guard: a site that slices and revalidates by hand is a site
        # that can be written without the slice, which is how a new read path
        # escapes the cap.
        import inspect

        from kiro_crew.dashboard import chat_persistence, chat_utils

        for mod in (
            chat_utils._redact_meta_for_role,
            chat_persistence._build_message_entry_uncached,
        ):
            src = inspect.getsource(mod)
            assert "revalidate_blocked_link(" not in src


class TestAVariantCarriesItsOwnRecords:
    """A record describes ONE text, so a variant switch moves both or neither.

    Variants are alternate replies the reader can switch back to. Text and
    records travel as one value through ``adopt_variant_text``, so a row cannot
    end up explaining a link that is absent from the text on screen.
    """

    @staticmethod
    def _record(domain: str) -> dict:
        return {
            "domain": domain,
            "rule": "exfil_query_length",
            "path": "/reports/summary",
            "query_chars": 256,
            "url": None,
            "url_withheld": "length",
        }

    def test_switching_takes_the_variant_s_records_with_its_text(self) -> None:
        row = {
            "role": "assistant",
            "content": "old text",
            "ts": "t0",
            "meta": {"blocked_links": [self._record("old.example.com")]},
        }

        adopt_variant_text(
            row,
            {"content": "new text", "ts": "t1", "blocked_links": [self._record("new.example.com")]},
        )

        assert row["content"] == "new text"
        assert row["ts"] == "t1"
        assert row["meta"]["blocked_links"] == [self._record("new.example.com")]

    def test_switching_to_a_clean_variant_drops_the_stale_records(self) -> None:
        # The alternative is a chip explaining a host this text never held.
        row = {
            "role": "assistant",
            "content": "old text",
            "ts": "t0",
            "meta": {"blocked_links": [self._record("old.example.com")]},
        }

        adopt_variant_text(row, {"content": "nothing blocked here", "ts": "t1"})

        assert "meta" not in row

    def test_other_meta_survives_the_switch(self) -> None:
        row = {
            "role": "assistant",
            "content": "old",
            "ts": "t0",
            "meta": {"mid": "m1", "blocked_links": [self._record("old.example.com")]},
        }

        adopt_variant_text(row, {"content": "new", "ts": "t1"})

        assert row["meta"] == {"mid": "m1"}

    def test_a_variant_gains_records_on_a_row_that_had_none(self) -> None:
        row = {"role": "assistant", "content": "clean", "ts": "t0"}

        adopt_variant_text(
            row,
            {"content": "blocked", "ts": "t1", "blocked_links": [self._record("new.example.com")]},
        )

        assert row["meta"]["blocked_links"] == [self._record("new.example.com")]

    def test_both_variant_directions_bound_the_records_at_retention(self) -> None:
        # The variant list outlives the render that would otherwise bound it, so
        # each move applies the one bounded constructor: count capped, forged
        # records dropped, in both directions.
        from kiro_crew.dashboard.chat_utils import variant_from_row

        cap = exfil.MAX_BLOCKED_LINKS_PER_MESSAGE
        many = [self._record(f"h{i}.example.com") for i in range(cap + 8)]
        forged = {**self._record("x.example.com"), "domain": "a" * 400}

        stashed = variant_from_row(
            {"content": "c", "ts": "t", "meta": {"blocked_links": many + [forged]}}
        )
        assert len(stashed["blocked_links"]) == cap

        row = {"role": "assistant", "content": "old", "ts": "t0"}
        adopt_variant_text(row, {"content": "new", "ts": "t1", "blocked_links": many})
        assert len(row["meta"]["blocked_links"]) == cap

        row = {"role": "assistant", "content": "old", "ts": "t0"}
        adopt_variant_text(row, {"content": "new", "ts": "t1", "blocked_links": [forged]})
        assert "meta" not in row

    def test_a_regenerate_stash_takes_the_records_with_the_text(self) -> None:
        # The round trip that loses them silently: the stashed text is already a
        # placeholder, so a stash without the records leaves nothing any later
        # scan can rebuild -- switching back would hand the reader a bare
        # placeholder with no way to learn what was removed.
        row = {
            "role": "assistant",
            "content": "see [REDACTED: suspicious URL to old.example.com]",
            "ts": "t0",
            "meta": {"blocked_links": [self._record("old.example.com")]},
        }

        stashed = variant_from_row(row)

        assert stashed["blocked_links"] == [self._record("old.example.com")]
        assert stashed["content"] == row["content"]

    def test_a_clean_row_stashes_without_inventing_records(self) -> None:
        stashed = variant_from_row({"role": "assistant", "content": "nothing here", "ts": "t0"})

        assert "blocked_links" not in stashed

    def test_the_round_trip_returns_the_same_records(self) -> None:
        original = {
            "role": "assistant",
            "content": "see [REDACTED: suspicious URL to old.example.com]",
            "ts": "t0",
            "meta": {"blocked_links": [self._record("old.example.com")]},
        }
        stashed = variant_from_row(original)

        # A regenerate replaced the row; the reader switches back to the stash.
        row: dict = {"role": "assistant", "content": "a different reply", "ts": "t1"}
        adopt_variant_text(row, stashed)

        assert row["content"] == original["content"]
        assert row["meta"]["blocked_links"] == [self._record("old.example.com")]

    def test_the_stash_site_moves_the_pair_through_one_function(self) -> None:
        import inspect

        from kiro_crew.dashboard import chat_regenerate

        src = inspect.getsource(chat_regenerate.api_chat_slot_regenerate)
        assert "variant_from_row(ai_msg)" in src
        assert '"content": ai_msg.get("content"' not in src

    def test_the_stash_flush_carries_records_instead_of_rescanning(self) -> None:
        # A rescan here reads already-redacted text, so it can only ever return
        # nothing; a source guard keeps the records variant out of this site.
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner._flush_segment)
        stash = src[src.index("_pending_variants") :]
        assert "redact_exfiltration_urls_with_records(v" not in stash

    def test_the_switch_endpoint_moves_the_pair_through_one_function(self) -> None:
        # A source guard: a site that assigns content directly is a site that can
        # leave the records behind, and no unit on this row can see that.
        import inspect

        from kiro_crew.dashboard import chat_regenerate

        src = inspect.getsource(chat_regenerate.api_chat_slot_switch_variant)
        assert "adopt_variant_text(target_dict, chosen)" in src
        assert 'target_dict["content"] =' not in src


class TestMetaCarveOut:
    @staticmethod
    def _good_record() -> dict:
        return {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": "/reports/summary",
            "query_chars": 42,
            "url": None,
            "url_withheld": "length",
        }

    def test_valid_record_survives_meta_redaction(self) -> None:
        meta = {"blocked_links": [self._good_record()], "other": "value"}

        out = _redact_meta_for_role("assistant", meta)

        assert out["blocked_links"] == [self._good_record()]

    def test_tampered_records_are_dropped_individually(self) -> None:
        good = self._good_record()
        meta = {
            "blocked_links": [
                good,
                {
                    "domain": "not a host!!",
                    "rule": "exfil_query_length",
                    "path": None,
                    "query_chars": 1,
                    "url": None,
                    "url_withheld": "length",
                },
                {
                    "domain": _DOMAIN,
                    "rule": "Bad Rule",
                    "path": None,
                    "query_chars": 1,
                    "url": None,
                    "url_withheld": "length",
                },
                {
                    "domain": _DOMAIN,
                    "rule": "exfil_query_length",
                    "path": "/dump/AKIAIOSFODNN7EXAMPLE",
                    "query_chars": 0,
                    "url": None,
                    "url_withheld": "length",
                },
                {"domain": _DOMAIN, "unexpected": "key", "rule": "x", "path": None},
                {
                    "domain": _DOMAIN,
                    "rule": "exfil_query_length",
                    "path": None,
                    "query_chars": -1,
                    "url": None,
                    "url_withheld": "length",
                },
            ]
        }

        out = _redact_meta_for_role("assistant", meta)

        # Exactly the well-formed record survives; every tampered sibling is
        # dropped on its own, and the message is not discarded.
        assert out["blocked_links"] == [good]

    def test_all_bad_records_drop_the_key(self) -> None:
        meta = {"blocked_links": [{"wrong": "shape"}], "keep": "me"}

        out = _redact_meta_for_role("assistant", meta)

        assert "blocked_links" not in out
        assert out["keep"] == "me"

    def test_revalidate_rejects_payload_bearing_path(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": "/dump/AKIAIOSFODNN7EXAMPLE",
            "query_chars": 0,
            "url": None,
            "url_withheld": "length",
        }

        assert revalidate_blocked_link(record) is None

    def test_revalidate_rejects_boolean_query_chars(self) -> None:
        record = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": True,
            "url": None,
            "url_withheld": "length",
        }

        assert revalidate_blocked_link(record) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-n0"]))


class TestEveryRetentionPointIsBounded:
    """Records read off a transcript line are bounded where the slot KEEPS them.

    A bound applied only where the value is displayed leaves the store holding
    whatever the line carried, so each place that retains a row's meta or a
    variant list goes through ``with_bounded_redaction_records``.
    """

    @staticmethod
    def _many() -> list[dict]:
        return [
            {
                "domain": f"h{i}.example.com",
                "rule": "exfil_query_length",
                "path": None,
                "query_chars": 256,
                "url": None,
                "url_withheld": "length",
            }
            for i in range(exfil.MAX_BLOCKED_LINKS_PER_MESSAGE + 8)
        ]

    def test_the_helper_bounds_and_drops_and_leaves_other_keys(self) -> None:
        from kiro_crew.dashboard.chat_utils import with_bounded_redaction_records

        bounded = with_bounded_redaction_records({"mid": "m1", "blocked_links": self._many()})
        assert bounded["mid"] == "m1"
        assert len(bounded["blocked_links"]) == exfil.MAX_BLOCKED_LINKS_PER_MESSAGE

        assert with_bounded_redaction_records({"blocked_links": [{"domain": "x"}]}) == {}
        plain = {"mid": "m1"}
        assert with_bounded_redaction_records(plain) is plain

    def test_attached_variants_are_bounded_on_load(self) -> None:
        class _Slot:
            messages: list[dict] = [{"role": "assistant", "content": "c"}]

        slot = _Slot()
        chat_persistence._attach_variants(
            slot,  # type: ignore[arg-type]
            {"variants": [{"content": "v", "ts": "t", "blocked_links": self._many()}]},
        )

        assert len(slot.messages[-1]["variants"][0]["blocked_links"]) == (
            exfil.MAX_BLOCKED_LINKS_PER_MESSAGE
        )

    def test_each_retention_site_uses_the_bounded_helper(self) -> None:
        # Source guard over the sites that retain transcript data in the slot:
        # the two rehydrate appends, the variant attach, and the regenerate stash.
        import inspect

        from kiro_crew.dashboard import chat_runner

        persistence = inspect.getsource(chat_persistence)
        assert 'meta=(m["meta"] if' not in persistence
        assert persistence.count('with_bounded_redaction_records(m["meta"])') == 2
        assert "with_bounded_redaction_records(" in inspect.getsource(
            chat_persistence._attach_variants
        )
        stash = inspect.getsource(chat_runner._flush_segment)
        assert "with_bounded_redaction_records({**v" in stash


class TestAVariantsRecordsAreCheckedOnServe:
    """A variant's records can hold a full address a reader may open, so they
    pass the same serve-time check as a row's before reaching a client."""

    def test_a_forged_variant_record_is_dropped_on_emit(self) -> None:
        forged = {
            "domain": _DOMAIN,
            "rule": "exfil_query_length",
            "path": None,
            "query_chars": 0,
            "url": f"https://{_DOMAIN}/dump/AKIAIOSFODNN7EXAMPLE",
            "url_withheld": None,
        }
        msgs = [
            {
                "role": "assistant",
                "content": "x",
                "ts": "t0",
                "variants": [{"content": "v", "ts": "t1", "blocked_links": [forged]}],
            }
        ]

        out = _prepare_messages(msgs, False, live_child="")

        assert "blocked_links" not in out[0]["variants"][0]
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(out)

    def test_a_valid_variant_record_survives_emit(self) -> None:
        url = f"https://{_DOMAIN}/p?q={_LONG_QUERY_TAIL}"
        _, _, records = redact_exfiltration_urls_with_records(f"see {url}")
        msgs = [
            {
                "role": "assistant",
                "content": "x",
                "ts": "t0",
                "variants": [{"content": "v", "ts": "t1", "blocked_links": records}],
            }
        ]

        out = _prepare_messages(msgs, False, live_child="")

        assert out[0]["variants"][0]["blocked_links"] == records
