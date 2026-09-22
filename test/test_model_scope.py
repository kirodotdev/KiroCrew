"""A model pin belongs to the harness it was chosen in, and only that harness.

The user report these pin: a session ran Claude Code on Opus, the operator
switched the backend to codex, and the opus pin was pushed to the codex adapter,
refused, and the session fell to codex's default — with a warning naming a model
the user had not picked that turn.

``model_scope`` answers "was this pin chosen here?" from the model catalogs,
which means the tests have to drive the catalogs. Two of them exist per
namespace: the static ``model_registry.json`` index (``acp`` and ``claude_code``
only) and the advertised-model cache a live session fills. ``conftest`` isolates
that cache to ``{}`` for every test, so a test that wants a warm harness says so
explicitly — and a test asserting NO scoping happens gets the cold-cache state
for free, which is the state a fresh install is in.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew import model_registry as mr
from kiro_crew import model_scope
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.agent_sdk.backends import model_registry_namespace

# What each harness advertises, in its OWN vocabulary. Spelled with the effort
# and window suffixes the real adapters send, because folding those is half the
# matching problem: the reported log line carried ``openai.gpt-5.6-sol[xhigh]``
# against an advertised ``openai.gpt-5.6-sol[low]``.
CLAUDE_SERVES = ["claude-opus-5[1m]", "claude-sonnet-4-6[1m]"]
CODEX_SERVES = ["openai.gpt-5.6-sol[low]", "openai.gpt-5.4[xhigh]"]

CLAUDE_PIN = "claude-opus-5"
CODEX_PIN = "openai.gpt-5.6-sol[xhigh]"


@pytest.fixture
def both_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both harnesses have run a session, so both catalogs are warm.

    This is the state the report describes: the picker's claude options came from
    claude's advertised list, and codex advertised its own list in the same
    message that refused the opus pin.
    """
    monkeypatch.setattr(
        mr,
        "_ADVERTISED_MODELS",
        {"claude_code": list(CLAUDE_SERVES), "codex": list(CODEX_SERVES)},
    )


class TestCatalogKey:
    """One fold, applied to both sides of every comparison."""

    def test_effort_suffix_is_not_part_of_a_model_identity(self) -> None:
        assert mr.catalog_key("openai.gpt-5.6-sol[xhigh]") == mr.catalog_key(
            "openai.gpt-5.6-sol[low]"
        )

    def test_window_marker_and_prefix_fold_together(self) -> None:
        assert mr.catalog_key("global.anthropic.claude-opus-5[1m]") == mr.catalog_key(
            "claude-opus-5"
        )

    def test_the_absence_of_a_pin_has_no_key(self) -> None:
        assert mr.catalog_key("") == ""
        assert mr.catalog_key("auto") == ""


class TestCatalogNamespaces:
    def test_catalog_namespaces_covers_both_halves(self, both_warm: None) -> None:
        namespaces = mr.catalog_namespaces()
        assert "acp" in namespaces, "the static index must be represented"
        assert "codex" in namespaces, "a warm advertised bucket must be represented"


class TestPinApplies:
    """The conjunction: a warm harness that omits the pin AND another that claims it."""

    def test_the_reported_case_is_refused(self, both_warm: None) -> None:
        assert model_scope.pin_applies(CLAUDE_PIN, "codex") is False

    def test_the_mirror_of_the_reported_case_is_refused(self, both_warm: None) -> None:
        """Not a codex fix: the rule runs in both directions with no per-backend branch."""
        assert model_scope.pin_applies(CODEX_PIN, "claude_code") is False

    def test_a_harness_keeps_its_own_pin(self, both_warm: None) -> None:
        assert model_scope.pin_applies(CLAUDE_PIN, "claude_code") is True
        assert model_scope.pin_applies(CODEX_PIN, "codex") is True

    def test_nothing_is_refused_before_a_harness_has_advertised(self) -> None:
        """A fresh install must not lose a pin to a static index that is a snapshot.

        The cache is cold here, so ``acp``'s only catalog is the shipped file —
        which does not list every model kiro serves. Reading that gap as a
        refusal would drop a working pin on first run.
        """
        assert model_scope.pin_applies(CLAUDE_PIN, "acp") is True

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_a_live_kiro_family_list_refuses_a_codex_pin(
        self, backend: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"codex": list(CODEX_SERVES)})
        namespace = model_registry_namespace(backend)

        assert namespace == "acp"
        assert mr.advertised_models(namespace) == []
        assert (
            model_scope.pin_applies(
                CODEX_PIN,
                namespace,
                advertised=["claude-opus-4.8"],
            )
            is False
        )

    def test_an_unclaimed_id_still_reaches_the_wire(self, both_warm: None) -> None:
        """A regional profile or a model newer than every catalog is nobody's.

        Unrecognized is not the same as foreign: an id no catalog lists may still
        be real, so it is the caller's to send. Narrowing this would drop real
        pins to buy nothing.
        """
        assert model_scope.pin_applies("us.anthropic.model-from-next-year", "codex") is True

    def test_the_absence_of_a_pin_always_applies(self, both_warm: None) -> None:
        assert model_scope.pin_applies("", "codex") is True
        assert model_scope.pin_applies("auto", "codex") is True

    def test_an_unknown_target_harness_applies(self, both_warm: None) -> None:
        """No namespace means the caller does not know what will run.

        Withholding there trades a wrong model for a missing one.
        """
        assert model_scope.pin_applies(CLAUDE_PIN, "") is True


# What kiro's ``chat --list-models`` catalog names, in kiro's own spelling. This
# is what ``GET /api/models`` feeds into the ``acp`` bucket.
KIRO_CATALOG = ["auto", "claude-opus-4.8", "claude-sonnet-4.6", "gpt-5.6-terra"]
# claude's list: one model kiro also serves, one it does not.
CLAUDE_ONLY_ADVERTISES = [
    "global.anthropic.claude-opus-4-8[1m]",
    "global.anthropic.claude-haiku-9[1m]",
]
CLAUDE_ONLY_PIN = "global.anthropic.claude-haiku-9[1m]"


@pytest.fixture
def kiro_catalog_warm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``acp`` bucket is warm from the catalog and claude has advertised too."""
    monkeypatch.setattr(
        mr,
        "_ADVERTISED_MODELS",
        {"acp": list(KIRO_CATALOG), "claude_code": list(CLAUDE_ONLY_ADVERTISES)},
    )


class TestKiroCatalogAsVocabulary:
    """The warm ``acp`` bucket lets the chip and the factory reach the wire's verdict.

    A warm bucket widens what can be refused, so these measure the other
    direction too: nothing the account is entitled to on kiro is withheld by
    the bucket being warm.
    """

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_a_native_pin_is_kept(self, backend: str, kiro_catalog_warm: None) -> None:
        assert model_scope.pin_applies("claude-opus-4.8", model_registry_namespace(backend)) is True

    def test_a_native_pin_in_a_foreign_spelling_is_kept_and_resolves(
        self, kiro_catalog_warm: None
    ) -> None:
        # claude's provider-id spelling of a model kiro serves: `catalog_key`
        # folds it onto the catalog, so it is native, not foreign -- and the
        # spelling fold answers the advertised id the wire should send. The two
        # folds agree, which is what makes the cold start name the right cause.
        from kiro_crew.acp.client import resolve_pin_spelling

        pin = "global.anthropic.claude-opus-4-8[1m]"
        assert model_scope.pin_applies(pin, "acp") is True
        assert resolve_pin_spelling(pin, KIRO_CATALOG) == "claude-opus-4.8"

    def test_a_pin_absent_from_the_catalog_and_claimed_elsewhere_is_refused(
        self, kiro_catalog_warm: None
    ) -> None:
        # With a cold bucket this pin survives to the chip and the factory and
        # only the wire withholds it; the warm bucket gives all three one verdict.
        assert model_scope.pin_applies(CLAUDE_ONLY_PIN, "acp") is False
        assert model_scope.pin_applies(CLAUDE_ONLY_PIN, "acp") is (
            model_scope.pin_applies(CLAUDE_ONLY_PIN, "acp", advertised=["claude-opus-4.8"])
        )

    def test_a_pin_absent_from_the_catalog_and_claimed_by_nobody_is_kept(
        self, kiro_catalog_warm: None
    ) -> None:
        # Entitled-but-unlisted and unclaimed: a regional profile or a model
        # newer than the catalog is still the caller's to send.
        assert model_scope.pin_applies("us.anthropic.model-from-next-year", "acp") is True

    def test_a_catalog_row_the_account_cannot_run_is_still_native(
        self, kiro_catalog_warm: None
    ) -> None:
        # The bucket is a vocabulary, not an entitlement: a native id missing
        # from this session's live list applies here and takes the entitlement
        # arm downstream, never the foreign note.
        assert (
            model_scope.pin_applies("gpt-5.6-terra", "acp", advertised=["claude-opus-4.8"]) is True
        )

    def test_the_bucket_is_never_read_as_a_wire_spelling_for_kiro(
        self, kiro_catalog_warm: None
    ) -> None:
        # The two readers that fold a pin onto an advertised spelling are gated
        # to ACP_BACKENDS_ADVERTISED_MODEL_SELECTION at their call sites; kiro
        # and kas are not members, so a warm bucket cannot rewrite their wire id.
        from kiro_crew.agent_sdk.backends import ACP_BACKENDS_ADVERTISED_MODEL_SELECTION

        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION


class TestVocabularyIsNotEntitlement:
    """A native pin the account cannot run is NOT a foreign pin.

    An advertised list says what the account MAY RUN, so an id missing from it can
    be unentitled rather than foreign. Reading absence as foreignness sends a
    native pin down the harness-scope path, which suppresses the entitlement
    warning that names the real cause and emits a note naming the session's own
    namespace as the other harness.
    """

    KIRO_LIVE = ["claude-sonnet-4.6"]

    def test_a_native_pin_missing_from_the_live_list_still_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``claude-opus-4.8`` is a real kiro id; the live list omitting it is entitlement."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": list(CLAUDE_SERVES)})
        assert model_scope.pin_applies("claude-opus-4.8", "acp", advertised=self.KIRO_LIVE) is True

    def test_a_foreign_pin_missing_from_the_live_list_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same shape with a codex id, which is NOT kiro vocabulary."""
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"codex": list(CODEX_SERVES)})
        assert model_scope.pin_applies(CODEX_PIN, "acp", advertised=self.KIRO_LIVE) is False

    def test_a_harness_is_never_foreign_to_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two namespaces list one model family, so the owner set must exclude self.

        Without the exclusion the note reads "chosen for acp/claude_code, not acp",
        naming the session's own namespace as the harness the pin belongs to.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {})
        assert "acp" not in model_scope.foreign_namespaces("claude-opus-4.8", "acp")

    def test_a_live_list_admits_a_model_newer_than_every_catalog(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live list is the ONLY source that can know a brand-new model.

        `kiro` never fills the advertised cache (it is not a member of
        ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION``) and the static index is a
        snapshot, so a model the account reaches through kiro but that ships in
        neither is vocabulary only by way of this session's own list. Another
        harness listing the same id must not carry it off.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": ["claude-opus-5[1m]"]})
        assert (
            mr.namespace_vocabulary("claude-opus-5", "acp") is False
        ), "precondition: neither the static acp index nor its cache knows this id"
        assert model_scope.pin_applies("claude-opus-5", "acp", advertised=["claude-opus-5"]) is True

    def test_a_harness_that_has_said_nothing_refuses_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No live list and a cold cache means no evidence about this harness.

        Another harness owning the id is not enough on its own: with nothing known
        about the harness that will run the session, a refusal would drop a pin on
        a fresh install purely because a sibling catalog is warmer.
        """
        monkeypatch.setattr(mr, "_ADVERTISED_MODELS", {"claude_code": ["claude-opus-5[1m]"]})
        assert model_scope.foreign_namespaces("claude-opus-5", "acp") == frozenset(
            {"claude_code"}
        ), "precondition: the other harness does own this id"
        assert model_scope.pin_applies("claude-opus-5", "acp") is True

    def test_a_static_alias_for_a_substitute_is_not_vocabulary(self) -> None:
        """``claude-haiku-4.5`` resolves in the claude index but its id is Sonnet.

        Treating that alias as vocabulary lets the pin survive into a silent
        substitution, which is what the round-trip through ``to_provider_id``
        in :func:`model_registry.namespace_vocabulary` prevents.
        """
        assert mr.namespace_vocabulary("claude-haiku-4.5", "claude_code") is False
        assert mr.namespace_vocabulary("claude-haiku-4.5", "acp") is True

    def test_a_canonical_key_is_native_without_a_round_trip(self) -> None:
        assert mr.namespace_vocabulary("opus-4.8-1m", "claude_code") is True
        assert mr.namespace_vocabulary("claude-haiku-4.5", "claude_code") is False


class TestScopedPin:
    def test_an_out_of_scope_pin_reads_as_unset(self, both_warm: None) -> None:
        """``""``, not ``auto``: every resolution tier already treats it as "defer"."""
        assert model_scope.scoped_pin(CLAUDE_PIN, "codex") == ""

    def test_an_in_scope_pin_is_returned_verbatim(self, both_warm: None) -> None:
        assert model_scope.scoped_pin(CLAUDE_PIN, "claude_code") == CLAUDE_PIN

    def test_the_note_is_info_and_names_both_harnesses(
        self, both_warm: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stale setting, not a fault: the session is about to run correctly.

        The WARNING this replaces came from the adapter refusing the id on the
        wire, which is the event the scope gate exists to stop happening.
        """
        with caplog.at_level(logging.INFO, logger="kiro_crew.model_scope"):
            model_scope.scoped_pin(CLAUDE_PIN, "codex", source="agent.model")
        records = [r for r in caplog.records if r.name == "kiro_crew.model_scope"]
        assert records, "an out-of-scope pin must leave a note"
        assert all(r.levelno == logging.INFO for r in records)
        message = records[0].getMessage()
        assert CLAUDE_PIN in message
        assert "claude_code" in message and "codex" in message

    def test_nothing_is_logged_when_the_pin_applies(
        self, both_warm: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="kiro_crew.model_scope"):
            model_scope.scoped_pin(CLAUDE_PIN, "claude_code")
        assert [r for r in caplog.records if r.name == "kiro_crew.model_scope"] == []


class TestEveryBackendIsCovered:
    """The rule is the namespace mechanism, so a new harness is covered by having one."""

    def test_every_known_backend_resolves_a_namespace(self) -> None:
        for backend in ACP_BACKENDS_KNOWN:
            assert model_registry_namespace(backend), f"{backend} has no model namespace"

    def test_kiro_and_kas_share_a_namespace_so_they_share_pins(self) -> None:
        """Two harnesses serving one vocabulary must NOT be scoped apart.

        Scoping on the backend id instead of its namespace would make a kiro pin
        foreign to kas, which is the same defect pointed the other way.
        """
        assert model_registry_namespace(ACP_BACKEND_KIRO) == model_registry_namespace(
            ACP_BACKEND_KAS
        )

    def test_claude_and_codex_do_not_share_a_namespace(self) -> None:
        assert model_registry_namespace(ACP_BACKEND_CLAUDE) != model_registry_namespace(
            ACP_BACKEND_CODEX
        )

    def test_the_default_model_sentinel_comes_from_config_sections(self) -> None:
        """``config.sections`` owns the sentinel used by model scope."""
        from kiro_crew.config.sections import DEFAULT_MODEL

        assert model_scope.DEFAULT_MODEL == DEFAULT_MODEL
