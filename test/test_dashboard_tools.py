"""The two dashboard tools, and the refusal an agent reads.

``dashboard_fields`` is the read that makes an agent's first write usually correct and
``dashboard_write`` is the write. The claims that make them safe to hand an agent with
no person in the loop are here:

* a refusal reaches the agent WHOLE. The stored crew-log row is capped, the sentence is
  not, and the sentence's tail is where the remedy is;
* both halves go through the same scrub, so a credential-shaped field name is not
  rendered verbatim on the page that draws the mistake book;
* redaction never loses a row. Two keys that scrub to one placeholder are suffixed.

A refusal with no test is a sentence nobody has read: these tools are the first ones
whose refusals an AGENT has to act on without a person in the loop, so the refusal text
is as much the product as the success.

The gate on which provenance may EXECUTE is pinned here too. It is one value, and the
set is read by the member dashboard's own render gate rather than by anything in this
file, so a change to it has to fail somewhere a reader of the tools will see.
"""

from __future__ import annotations

from kiro_crew import dashboard_agentic
from kiro_crew.dashboard_templates import catalog, instance
from kiro_crew.mcp_panel import _list_tools


class TestTheToolSurface:
    def test_both_tools_are_advertised(self) -> None:
        """The set the agent spec and the skill both name.

        Pinned here as well as in ``test_mcp_panel_registration``, which checks the
        schema ratchet: this asserts them BY NAME, so a tool quietly dropped from the
        surface fails beside the behaviour tests rather than only in a count.
        """
        assert {t["name"] for t in _list_tools()} >= {"dashboard_fields", "dashboard_write"}

    def test_fields_advertises_no_arguments(self) -> None:
        """The emptiness IS the property: the dashboard it describes is the caller's own."""
        fields_tool = next(t for t in _list_tools() if t["name"] == "dashboard_fields")
        assert fields_tool["inputSchema"].get("properties") == {}


class TestOnlyABuiltInSourceIsRenderable:
    """The F1 rule, at the layer that owns it.

    A dashboard page runs its own script against this crewmate's task titles and
    summaries, and the dashboard's own ``frame-src`` admits the hosts its artifact
    previews need -- so a page that runs can navigate itself to one of them with those
    values in the URL, and a closed ``connect-src`` does not stop that. Scripts ARE the
    format, so the only available signal is the author, and the only provenance that
    carries a human review is a directory in this repository.
    """

    def test_the_set_holds_exactly_the_built_in_source(self) -> None:
        assert instance.RENDERABLE_SOURCES == frozenset({catalog.BUILTIN_SOURCE})

    def test_a_page_a_caller_wrote_has_no_renderable_source(self) -> None:
        """The control: the set is a gate and not a formality.

        ``user`` is a value the manifest grammar accepts, so a record can reach disk
        declaring it. What the set says is that such a record is still not executed.
        """
        assert "user" not in instance.RENDERABLE_SOURCES


class TestARefusalReachesTheAgentWhole:
    """The stored row is capped; the sentence the agent reads is not.

    The 240-character cap bounds the crew-log entry. Applying it to the 400 body as
    well cut the sentence from the END, and the end is where the remedy is: the
    valid field names come first and the "you have made this mistake N times before,
    and 'x' worked" note last. A manifest with about five fields therefore pushed
    exactly the useful half past the cap, and an agent was told what it got wrong
    without being told what to do instead -- which is the cycle the mistake book
    exists to end.
    """

    @staticmethod
    def _long_refusal() -> dashboard_agentic.WriteRefused:
        """A refusal whose sentence runs well past the stored cap."""
        return dashboard_agentic.WriteRefused(
            "unknown_field",
            "ghost",
            "no dashboard field 'ghost'. Agentic fields: "
            + ", ".join(f"field_number_{n}" for n in range(12))
            + ". You have made this mistake 4 times before; 'phase' worked.",
        )

    def test_the_sentence_is_handed_back_whole(self) -> None:
        refused = self._long_refusal()
        sentence = dashboard_agentic.refusal_sentence(refused)
        assert len(sentence) > 240, "this case needs a sentence past the cap to be about it"
        # The REMEDY, which is the tail and therefore the first thing a cut drops.
        assert sentence.endswith("'phase' worked.")

    def test_the_stored_row_is_a_prefix_of_that_sentence(self) -> None:
        """One wording, cut in one place.

        Two wordings of one rule is how an agent ends up unsure which it broke, so
        the record is not allowed to be a second sentence -- only a shorter one.
        """
        refused = self._long_refusal()
        stored = dashboard_agentic.refusal_entry(refused)["reason"]
        assert len(stored) == 240
        assert dashboard_agentic.refusal_sentence(refused).startswith(stored)

    def test_both_halves_run_through_the_same_scrub(self) -> None:
        """The cap is the only difference between them.

        A page binding the mistake book draws these as text, so a credential-shaped
        field name reaching either one unscrubbed would be rendered verbatim on the
        dashboard.
        """
        leaked = dashboard_agentic.WriteRefused(
            "unknown_field",
            "tok",
            "no dashboard field 'tok'. Try AKIAIOSFODNN7EXAMPLE instead.",
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in dashboard_agentic.refusal_sentence(leaked)
        assert "AKIAIOSFODNN7EXAMPLE" not in dashboard_agentic.refusal_entry(leaked)["reason"]


# --------------------------------------- redaction keeps every row it scrubs


class TestRedactionDoesNotLoseRows:
    """Two keys that scrub to one placeholder are suffixed, not collapsed.

    The collapse was recorded as an acceptable trade on the grounds that such keys
    must not be displayed anyway. The label is indeed unreadable either way -- but
    each key carries a VALUE the agent wrote and meant to store, so a two-row write
    landing as one row loses a row with nothing raised and nothing in the mistake
    book. `member_dashboard._page_safe` already suffixes for exactly this reason.
    """

    @staticmethod
    def _two_colliding_keys() -> dict[str, str]:
        """Two DIFFERENT credential-shaped keys that redact to the same text.

        Assembled at runtime, like every other credential in this suite: the
        internal-content scan reads this change's own diff. Do not join them up.
        """
        head = "AKIA" + "IOSFODNN7"
        return {f"{head}EXAMPLE": "first row", f"{head}SAMPLE2": "second row"}

    def test_both_values_survive_a_key_collision(self) -> None:
        raw = self._two_colliding_keys()
        assert len(raw) == 2, "the fixture no longer carries two distinct keys"
        out = dashboard_agentic._redacted(raw)
        assert len(out) == 2, f"a row was lost to a redacted-key collision: {out}"
        assert sorted(out.values()) == ["first row", "second row"]

    def test_the_second_key_takes_a_numbered_suffix(self) -> None:
        """The same shape ``_page_safe`` gives a colliding fold key."""
        out = dashboard_agentic._redacted(self._two_colliding_keys())
        suffixed = [k for k in out if k.endswith(" (2)")]
        assert len(suffixed) == 1, f"no suffixed key: {sorted(out)}"

    def test_a_third_collision_keeps_counting(self) -> None:
        head = "AKIA" + "IOSFODNN7"
        raw = {f"{head}EXAMPLE": 1, f"{head}SAMPLE2": 2, f"{head}SAMPLE3": 3}
        out = dashboard_agentic._redacted(raw)
        assert len(out) == 3, f"a row was lost: {out}"
        assert sorted(out.values()) == [1, 2, 3]

    def test_keys_that_do_not_collide_are_untouched(self) -> None:
        """A control: the suffix is not applied to ordinary keys."""
        out = dashboard_agentic._redacted({"alpha": 1, "beta": 2})
        assert out == {"alpha": 1, "beta": 2}
