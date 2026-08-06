"""Unit tests for the unified model resolver `resolve_usable_model`.

The resolver is the SUBSTITUTE path (background one-liners, session-default /
inherited apply, warm-pool re-apply): given a preferred model and the backend's
advertised list, it returns a model the account can actually run. Precedence:
preferred-if-usable -> "auto"-if-advertised -> first-advertised -> "auto".

The load-bearing new constraint: "auto" is NOT universally
available — some partitions do not serve it — so it is validated
against the advertised list like any other id, never assumed usable.
"""

from __future__ import annotations

from kiro_crew.acp.client import resolve_usable_model


class TestResolveUsableModel:
    def test_advertised_unknown_trusts_concrete_but_inherits_for_auto(self) -> None:
        # Empty/None advertised = entitlement unknowable: trust a concrete id,
        # but never send a literal "auto" we cannot verify -> "" (inherit default).
        assert resolve_usable_model("claude-haiku-4.5", []) == "claude-haiku-4.5"
        assert resolve_usable_model("anything", []) == "anything"
        assert resolve_usable_model("auto", None) == ""

    def test_preferred_usable_is_kept(self) -> None:
        adv = ["claude-sonnet-4.6", "claude-haiku-4.5", "auto"]
        assert resolve_usable_model("claude-haiku-4.5", adv) == "claude-haiku-4.5"

    def test_concrete_unentitled_inherits_default(self) -> None:
        # A concrete id the account cannot run -> "" (inherit the served backend
        # default), NOT a possibly-unavailable "auto". Free-tier case.
        assert resolve_usable_model("claude-haiku-4.5", ["claude-sonnet-4.6"]) == ""

    def test_auto_sent_only_when_advertised(self) -> None:
        # Mirror _wire_model_id: "auto" IFF the backend advertises it.
        assert resolve_usable_model("auto", ["auto", "claude-sonnet-4.6"]) == "auto"

    def test_auto_not_advertised_inherits_default(self) -> None:
        # Some partitions don't serve auto -> "" (inherit default), never send auto.
        assert resolve_usable_model("auto", ["gpt-5.6-terra", "gpt-5.6-luna"]) == ""

    def test_case_insensitive_membership(self) -> None:
        adv = ["Claude-Haiku-4.5"]
        assert resolve_usable_model("claude-haiku-4.5", adv) == "claude-haiku-4.5"

    def test_empty_preferred_inherits_default(self) -> None:
        assert resolve_usable_model("", ["claude-sonnet-4.6", "gpt-5.6-terra"]) == ""
