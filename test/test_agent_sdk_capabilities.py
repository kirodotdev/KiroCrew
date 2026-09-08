"""The capability mechanism lives inside the boundary, and answers what main answers.

Three things this file is responsible for, and they fail for different reasons:

1. **Nothing outside the boundary asks a backend's IDENTITY any more.** RFC PR 3's
   exit condition. An identity branch hands each new harness whichever arm the old
   comparison happened to leave behind, so the six that moved must not come back
   and no seventh may appear in the files they lived in.
2. **Every answer is the answer ``origin/main`` gave.** The move is a refactor, so
   each routing verdict, permission config, membership and capability field is
   pinned to a literal copied from a clean main checkout -- ``codex`` and an
   unknown id included, because a fail-closed answer that quietly became
   fail-open is the failure this class of change actually risks.
3. **Every capability set has a recorded disposition.** A set with no row in
   ``agent_sdk/backends``'s docstring is a set nobody decided about, and the next
   reader exposes a driver-internal membership as a consumer-facing question.

The scans are AST-based, so a set name inside a docstring or a comment is not a
read. That matters here: several of these files keep prose explaining why they ask
a capability rather than an identity, and a text scan would either fail on that
prose or force it to be deleted -- and deleting the reason is how the next change
re-introduces the branch.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from source_corpus import parsed_candidates, src_root

from kiro_crew.acp_backends import ACP_BACKEND_CODEX, permission_config_for, routing_for
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.capabilities import (
    MODEL_NAMESPACE_ACP,
    UNKNOWN_BACKEND_CAPABILITIES,
    SessionCapabilities,
    capabilities_for,
    capabilities_of,
)
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, PROVIDER_CLAUDE_CODE

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: The six sites RFC PR 3 names, and the identity spelling each one used.
#:
#: Keyed by file so a regression names the file it landed in. The spellings are
#: what a consumer must never write again: ``is_claude_backend`` (the provider
#: predicate and the property), ``_is_claude`` (the ACP client's private
#: attribute), and a bare comparison against ``ACP_BACKEND_CLAUDE``.
MIGRATED_CONSUMERS = (
    "config/loader.py",
    "dashboard/chat_handlers.py",
    "dashboard/handlers/agents.py",
    "dashboard/chat_runner.py",
    "knowledge/llm_pool.py",
    "subagent.py",
)

#: One field per question a consumer outside the boundary asks.
CAPABILITY_FIELDS = (
    "provider_seam",
    "model_id_namespace",
    "resolves_model_from_advertised_list",
    "effort_via_config_option",
    "compacts_inline",
)


def _tree(rel: str) -> ast.Module:
    return ast.parse((SRC / rel).read_text(encoding="utf-8"))


# ── 1. the identity checks are gone, and stay gone ──────────────────────────


#: The identity reads still standing, keyed by file and by the function they are in.
#:
#: One entry, and it is deliberately still here. ``api_models`` asks a question the
#: six migrated sites do not: does this backend have a ``--list-models`` CATALOG to
#: shell out to, or do its models only exist on a live session's advertised list?
#: That is a pre-session question about a config value, and answering it as a
#: capability means deciding it for KAS and for codex -- both of which take the
#: kiro-cli branch today -- so it is a behaviour decision rather than a
#: translation. PR 3a is scoped to translations; see the RFC's PR 3 section.
#:
#: Pinned by ENCLOSING FUNCTION, not by line number, so the ratchet survives an
#: unrelated edit above it while still failing on a NEW identity read anywhere in
#: the file.
KNOWN_REMAINING_IDENTITY_READS = {
    "dashboard/handlers/agents.py": {"api_models"},
}


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    """Map every line inside a function body to that function's name."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            for line in range(node.lineno, end + 1):
                owner.setdefault(line, node.name)
    return owner


@pytest.mark.parametrize("rel", MIGRATED_CONSUMERS)
def test_no_migrated_consumer_reads_a_backend_identity(rel: str) -> None:
    """No attribute read, call or comparison in these files names the harness.

    Catches all three spellings at once by walking the AST rather than the text:
    an attribute named ``is_claude_backend``/``_is_claude`` (however it is
    reached, including through ``getattr``), a call to ``is_claude_backend``, and
    a load of ``ACP_BACKEND_CLAUDE``.

    A read inside a function named in :data:`KNOWN_REMAINING_IDENTITY_READS` is
    allowed and every other one is not, so this is a ratchet: the six that moved
    cannot come back, and a seventh cannot appear beside the one that stayed.
    """
    banned_attrs = {"is_claude_backend", "_is_claude"}
    tree = _tree(rel)
    owner = _enclosing_functions(tree)
    allowed = KNOWN_REMAINING_IDENTITY_READS.get(rel, set())
    offenders = []

    def record(node: ast.AST, what: str) -> None:
        line = getattr(node, "lineno", 0)
        if owner.get(line) in allowed:
            return
        offenders.append(f"line {line} (in {owner.get(line, '<module>')}): {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
            record(node, f".{node.attr}")
        elif isinstance(node, ast.Name) and node.id in banned_attrs | {"ACP_BACKEND_CLAUDE"}:
            record(node, node.id)
        elif isinstance(node, ast.Constant) and node.value in banned_attrs:
            # ``getattr(client, "is_claude_backend", False)`` -- a string literal
            # naming the attribute is the same read with the check hidden.
            record(node, f"{node.value!r} as a literal")
    assert not offenders, (
        f"{rel} asks which backend it is: {offenders}. Ask a capability instead "
        f"(SessionCapabilities has one field per question these branches make); "
        f"see docs/request-for-change/rfc-crew-agent-sdk-boundary.md PR 3."
    )


def test_the_allowance_list_is_not_stale() -> None:
    """An allowed function that stops reading an identity must leave the list.

    Otherwise the list quietly becomes a blanket exemption for a whole function,
    and the next identity read added inside it goes unnoticed.
    """
    banned = {"is_claude_backend", "_is_claude", "ACP_BACKEND_CLAUDE"}
    for rel, functions in KNOWN_REMAINING_IDENTITY_READS.items():
        tree = _tree(rel)
        owner = _enclosing_functions(tree)
        still_reading = set()
        for node in ast.walk(tree):
            name = ""
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            if name in banned:
                fn = owner.get(getattr(node, "lineno", 0))
                if fn:
                    still_reading.add(fn)
        stale = sorted(functions - still_reading)
        assert not stale, (
            f"{rel}: {stale} no longer reads a backend identity; drop it from "
            f"KNOWN_REMAINING_IDENTITY_READS so the ratchet tightens"
        )


def test_each_capability_question_has_one_consumer_spelling() -> None:
    """A field is read off a capability record, never off anything else.

    The point of one spelling is that the question is findable: ``grep`` for the
    field name and you have every consumer. A read off some other object -- a
    provider that happens to expose a same-named property, a dict, a shim -- would
    be a second way to ask, and a second way is what drifts.

    Every field in one pass over ``source_corpus``, pre-filtered to the files that
    even mention one. Parametrizing this and walking ``src/`` per field cost five
    full-tree parses (~24s each) for a gate whose real candidate set is a handful
    of files.
    """
    fields = set(CAPABILITY_FIELDS)
    allowed_roots = {"capabilities_for", "capabilities_of"}
    offenders = []
    for path, _text, tree in parsed_candidates(require_any=tuple(CAPABILITY_FIELDS)):
        rel = path.relative_to(src_root()).as_posix()
        if rel.startswith("agent_sdk/"):
            continue  # the definition side; it may name its own fields freely
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr in fields):
                continue
            base = node.value
            ok = (isinstance(base, ast.Call) and getattr(base.func, "id", "") in allowed_roots) or (
                isinstance(base, ast.Attribute) and base.attr == "capabilities"
            )
            if not ok:
                offenders.append(f"{rel}:{node.lineno} reads .{node.attr}")
    assert not offenders, (
        f"a capability field is read off something other than a capability record "
        f"at {offenders}; reach it through capabilities_for(...), "
        f"capabilities_of(...) or provider.capabilities so one grep finds every "
        f"consumer"
    )


def test_every_capability_field_has_a_consumer() -> None:
    """A field nobody reads is a question nobody asks -- delete it or wire it up.

    The record exists to move six branches off an identity check, so a field with
    no consumer means one of those branches was missed or the field was invented.
    """
    fields = set(CAPABILITY_FIELDS)
    seen = set()
    for path, _text, tree in parsed_candidates(require_any=tuple(CAPABILITY_FIELDS)):
        if path.relative_to(src_root()).as_posix().startswith("agent_sdk/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in fields:
                seen.add(node.attr)
    assert fields == seen, f"capability fields with no consumer: {sorted(fields - seen)}"


def test_the_field_list_matches_the_dataclass() -> None:
    """``CAPABILITY_FIELDS`` must not drift from ``SessionCapabilities``.

    ``backend`` is excluded because it is the identity, and nothing above the
    boundary may branch on it -- every other field is a question, so a new one
    must join the gates above rather than arrive unchecked.
    """
    declared = {f.name for f in SessionCapabilities.__dataclass_fields__.values()}
    assert declared - {"backend"} == set(CAPABILITY_FIELDS)


def test_the_shims_define_nothing() -> None:
    """``acp_backends`` and ``acp_tool_gate`` must stay pure re-export shims.

    A definition left in a shim is a definition reachable without crossing the
    boundary, which is the state the move exists to end. Assignments are the thing
    to catch; ``__all__`` is the one allowed statement.
    """
    for rel in ("acp_backends.py", "acp_tool_gate.py"):
        tree = _tree(rel)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                pytest.fail(f"{rel} defines {node.name}; it must only re-export")
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                assert names == ["__all__"], (
                    f"{rel} assigns {names}; a shim re-exports and defines nothing, "
                    f"or the boundary has a second front door"
                )


# ── 2. the answers are byte-for-byte what main gave ────────────────────────

#: Copied from a clean ``origin/main`` checkout (see the PR body for the probe).
#:
#: ``"kiro"`` is in the table on purpose and is NOT a backend id: it is the
#: POLICY wire spelling of the kiro backend, whose code spelling is ``""``. It
#: must read as an unknown id, because a rule author naming it must not
#: accidentally select a harness.
MAIN_ROUTING = {
    "": ("agent_spec", ("", ""), False),
    "kas": ("agent_spec", ("", ""), False),
    "claude": ("seeded_settings", ("", ""), False),
    "codex": ("session_config", ("mode", "read-only"), True),
    "nope": ("unverified", ("", ""), False),
    "kiro": ("unverified", ("", ""), False),
}


@pytest.mark.parametrize("backend", sorted(MAIN_ROUTING))
def test_routing_and_permission_config_are_unchanged_by_the_move(backend: str) -> None:
    """The tool-gate answers survive the move to ``agent_sdk``, fail-closed included."""
    from kiro_crew import acp_tool_gate

    expected_routing, expected_config, expected_enforced = MAIN_ROUTING[backend]
    assert routing_for(backend).value == expected_routing
    assert permission_config_for(backend) == expected_config
    assert acp_tool_gate.is_enforced(backend) is expected_enforced


def test_codex_still_needs_its_read_only_mode() -> None:
    """The one enforced harness, named rather than only parametrized.

    ``codex`` is the backend whose whole security argument is the session-config
    route plus the credential mask, so its two answers get an assertion a reader
    finds by name.
    """
    assert routing_for(ACP_BACKEND_CODEX) is sdk_backends.Routing.SESSION_CONFIG
    assert permission_config_for(ACP_BACKEND_CODEX) == ("mode", "read-only")


def test_known_membership_is_unchanged_by_the_move() -> None:
    """``ACP_BACKENDS_KNOWN`` gained and lost nothing.

    Membership is the gate on the ``acp_backend`` kwarg, so a widened set means
    provider construction accepts a value it must reject.
    """
    assert sorted(sdk_backends.ACP_BACKENDS_KNOWN) == ["", "claude", "codex", "kas"]


#: Every capability field for every known id, plus an unknown one.
#:
#: Each row is a translation of a table that already existed, so each row is also
#: the answer the branch it replaced already computed. Written out per backend
#: rather than derived from the sets, so a change to a set fails HERE with the
#: backend named instead of passing tautologically.
EXPECTED_CAPABILITIES = {
    "": (PROVIDER_ACP, MODEL_NAMESPACE_ACP, False, False, False),
    "kas": (PROVIDER_ACP, MODEL_NAMESPACE_ACP, False, False, False),
    "claude": (PROVIDER_CLAUDE_CODE, "claude_code", True, True, True),
    "codex": (PROVIDER_ACP, MODEL_NAMESPACE_ACP, False, True, False),
    "nope": (PROVIDER_ACP, MODEL_NAMESPACE_ACP, False, False, False),
}


@pytest.mark.parametrize("backend", sorted(EXPECTED_CAPABILITIES))
def test_capabilities_are_pinned_per_backend(backend: str) -> None:
    """One row per backend, so a silently granted capability names its harness."""
    caps = capabilities_for(backend)
    assert caps.backend == backend
    actual = (
        caps.provider_seam,
        caps.model_id_namespace,
        caps.resolves_model_from_advertised_list,
        caps.effort_via_config_option,
        caps.compacts_inline,
    )
    assert actual == EXPECTED_CAPABILITIES[backend]


def test_an_unknown_backend_is_withheld_every_capability() -> None:
    """Fail closed, and the shared default says the same thing.

    False means "has not demonstrated the capability", so a stranger takes the
    conservative arm rather than whatever the previous member left behind.
    """
    stranger = capabilities_for("some-harness-nobody-registered")
    for field in CAPABILITY_FIELDS:
        expected = getattr(UNKNOWN_BACKEND_CAPABILITIES, field)
        assert getattr(stranger, field) == expected, f"{field} drifted from the default"
    assert stranger.provider_seam == PROVIDER_ACP
    assert stranger.model_id_namespace == MODEL_NAMESPACE_ACP


def test_inline_compaction_is_a_subset_of_manual_compaction() -> None:
    """You cannot finish a ``/compact`` inline on a harness that serves none.

    The new set answers "how does the result arrive"; the older
    ``ACP_BACKENDS_COMPACT`` answers "is a manual /compact offered at all". A
    member of the first that is not a member of the second would be unreachable
    logic pretending to be a capability.
    """
    assert sdk_backends.ACP_BACKENDS_INLINE_COMPACTION <= sdk_backends.ACP_BACKENDS_COMPACT
    assert sdk_backends.ACP_BACKENDS_INLINE_COMPACTION <= sdk_backends.ACP_BACKENDS_KNOWN


# ── 3. every capability set has a recorded disposition ─────────────────────


def test_every_capability_set_has_a_disposition_row() -> None:
    """A set with no row in the docstring is a set nobody decided about.

    Three dispositions exist -- semantic question, pre-session registry query,
    driver-internal -- and which one a set is decides whether a consumer above the
    boundary may read it. Leaving that unrecorded is how a driver-internal
    membership becomes a consumer-facing question by accident.
    """
    doc = sdk_backends.__doc__ or ""
    documented = set(re.findall(r"``(ACP_BACKENDS_\w+)``", doc))
    defined = {name for name in vars(sdk_backends) if name.startswith("ACP_BACKENDS_")}

    missing = sorted(defined - documented)
    assert not missing, (
        f"these capability sets have no disposition row in agent_sdk/backends.py's "
        f"docstring: {missing}. Say whether each is a semantic question (a consumer "
        f"asks it, so SessionCapabilities carries a field), a pre-session registry "
        f"query, or driver-internal."
    )

    stale = sorted(documented - defined)
    assert not stale, (
        f"the disposition table names sets that no longer exist: {stale}; "
        f"remove the rows with the sets"
    )


def test_every_semantic_disposition_names_a_real_capability_field() -> None:
    """A row claiming ``SessionCapabilities.<field>`` must name a field that exists."""
    doc = sdk_backends.__doc__ or ""
    claimed = set(re.findall(r"``SessionCapabilities\.(\w+)``", doc))
    fields = {f.name for f in SessionCapabilities.__dataclass_fields__.values()}
    assert claimed, "the disposition table records no semantic question at all"
    assert claimed <= fields, f"disposition table names missing fields: {sorted(claimed - fields)}"


# ── capabilities_of: the calling convention the predicates had ─────────────


class _FakeProvider:
    def __init__(self, caps: object) -> None:
        self.capabilities = caps


def test_capabilities_of_reads_a_real_record() -> None:
    claude = capabilities_for("claude")
    assert capabilities_of(_FakeProvider(claude)) is claude


@pytest.mark.parametrize(
    "provider",
    [
        object(),
        None,
        "a string",
        _FakeProvider("not a capabilities record"),
        _FakeProvider(None),
    ],
    ids=["plain-object", "none", "string", "wrong-type", "explicit-none"],
)
def test_capabilities_of_fails_closed_on_a_foreign_shape(provider: object) -> None:
    """The convention the six predicates had: not a provider means False everywhere.

    They were ``isinstance(provider, AcpProvider) and provider.is_claude_backend``,
    so a wrapper, an unstarted provider or a test double answered False. Requiring
    a real record keeps that, which a bare ``getattr`` would not.
    """
    assert capabilities_of(provider) is UNKNOWN_BACKEND_CAPABILITIES


def test_a_spec_mock_does_not_read_as_a_member_of_everything() -> None:
    """A ``MagicMock(spec=...)``'s every attribute is truthy -- including a fake record.

    This is the exact trap ``capabilities_of``'s isinstance check exists for: a
    ``getattr``-based lookup would let a spec'd double claim every capability at
    once, and a double that asserts an attribute flag rather than carrying a real
    record is how that goes unnoticed.
    """
    from unittest.mock import MagicMock

    from kiro_crew.providers.acp import AcpProvider

    assert capabilities_of(MagicMock(spec=AcpProvider)) is UNKNOWN_BACKEND_CAPABILITIES


def test_the_provider_property_answers_for_its_own_client_backend() -> None:
    """``AcpProvider.capabilities`` is the record for the backend it is talking to."""
    from types import SimpleNamespace

    from kiro_crew.providers.acp import AcpProvider

    provider = object.__new__(AcpProvider)
    for backend in ("", "kas", "claude", "codex"):
        provider._client = SimpleNamespace(backend=backend)  # type: ignore[attr-defined]
        assert provider.capabilities == capabilities_for(backend)


class TestTheKiroConstructionPathIsUnconditional:
    """harness-parity H13: no capability lookup may change what Kiro does.

    ``acp_effective_model`` reads
    ``namespace = capabilities_for(...).model_id_namespace`` and branches on it. The
    branch carries a drift risk a hardcoded ``to_acp_id`` does not: the kiro arm is
    reached through a TABLE, so an edit to
    ``_MODEL_REGISTRY_NAMESPACE_BY_BACKEND`` can move it.

    So the pin is the branch, not the value: every non-claude backend must reach
    ``to_acp_id`` and must never reach ``to_provider_id``. That is what makes
    "the Kiro path is unconditional" a test result rather than a claim.
    """

    @staticmethod
    def _translations(monkeypatch, backend: str) -> list[tuple]:
        """Record which registry translator ``acp_effective_model`` calls."""
        from kiro_crew.config import loader as loader_mod

        calls: list[tuple] = []

        def _acp_id(model: str) -> str:
            calls.append(("to_acp_id", model))
            return f"acp::{model}"

        def _provider_id(model: str, provider: str) -> str:
            calls.append(("to_provider_id", model, provider))
            return f"{provider}::{model}"

        monkeypatch.setattr(loader_mod.model_registry, "to_acp_id", _acp_id)
        monkeypatch.setattr(loader_mod.model_registry, "to_provider_id", _provider_id)

        cfg = loader_mod.KiroCrewConfig()
        cfg.agent.acp_backend = backend
        cfg.acp_effective_model(None, "opus-4.8-1m")
        return calls

    @pytest.mark.parametrize("backend", ["", "kas", "codex", "some-future-harness"])
    def test_a_non_claude_backend_never_reaches_the_provider_namespace(
        self, monkeypatch, backend: str
    ) -> None:
        calls = self._translations(monkeypatch, backend)
        assert [c[0] for c in calls] == ["to_acp_id"], (
            f"backend {backend!r} left the acp translation path; the capability "
            f"lookup must not move a non-claude backend off to_acp_id"
        )

    def test_the_claude_backend_still_reaches_its_own_namespace(self, monkeypatch) -> None:
        """The other half: the one backend that DID take the other arm still does."""
        calls = self._translations(monkeypatch, "claude")
        assert calls == [("to_provider_id", "opus-4.8-1m", "claude_code")]

    def test_the_namespace_table_cannot_move_kiro_off_the_acp_arm(self) -> None:
        """The drift the reviewer named, pinned at the table rather than the branch.

        The branch tests above would also catch this, but they catch it as a
        translator call; this names the table entry, so the failure message points
        at the line an editor would have changed.
        """
        for backend in ("", "kas", "codex"):
            assert sdk_backends.model_registry_namespace(backend) == MODEL_NAMESPACE_ACP


class TestAForeignProviderIsClassifiedThePreviousWay:
    """harness-parity H14: a provider outside ``AcpProvider`` must not be guessed at.

    The ``LLMProvider`` ABC declares no ``capabilities``, so a non-``AcpProvider``
    implementation falls through to the unknown record. That fall-through is the
    intended answer, and it matches an ``isinstance(provider, AcpProvider)`` gate:
    a foreign provider gets the conservative default either way. This pins that
    equivalence against a REAL ``LLMProvider`` subclass rather than a bare object,
    so the claim is about the ABC and not about a convenient stand-in.
    """

    @staticmethod
    def _foreign_provider() -> object:
        from kiro_crew.providers.base import LLMProvider

        class _Foreign(LLMProvider):
            """A complete LLMProvider that is not an AcpProvider."""

            async def start(self) -> None:  # pragma: no cover - never run
                return None

            async def shutdown(self) -> None:  # pragma: no cover - never run
                return None

            async def stream(self, message: str):  # pragma: no cover - never run
                raise NotImplementedError

            async def approve_tool(self, request_id, *, always: bool = False) -> None:
                raise NotImplementedError  # pragma: no cover - never run

            async def reject_tool(self, request_id) -> None:
                raise NotImplementedError  # pragma: no cover - never run

            def context_usage_pct(self) -> float:  # pragma: no cover - never run
                return 0.0

        return _Foreign()

    def test_it_gets_the_fail_closed_record(self) -> None:
        assert capabilities_of(self._foreign_provider()) is UNKNOWN_BACKEND_CAPABILITIES

    def test_every_capability_reads_the_same_as_the_old_isinstance_gate(self) -> None:
        """The old gate answered False for a foreign provider. So does every field."""
        caps = capabilities_of(self._foreign_provider())
        for field in CAPABILITY_FIELDS:
            value = getattr(caps, field)
            if isinstance(value, bool):
                assert value is False, f"{field} is granted to a foreign provider"
        assert caps.provider_seam == PROVIDER_ACP
        assert caps.model_id_namespace == MODEL_NAMESPACE_ACP

    def test_the_subagent_home_routing_still_answers_acp(self) -> None:
        """The one consumer where a wrong answer picks the wrong home tree.

        ``_is_cc_provider`` decides whether session-file cleanup targets
        ``~/.claude`` or ``~/.kiro``. A foreign provider answered False before and
        must answer False now, or cleanup walks the wrong tree.
        """
        from kiro_crew.subagent import SubagentManager

        assert SubagentManager._is_cc_provider(self._foreign_provider()) is False


def test_the_knowledge_pool_client_takes_the_default_backend() -> None:
    """Pins why swapping ``_is_claude`` for the effort capability changed nothing.

    ``AcpWorker`` constructs its ``AcpClient`` without ``acp_backend``, so the
    backend is the kiro default and both the old identity read and the new
    capability answer False. If a future pool starts selecting a backend this
    fails, which is the moment to check the effort channel deliberately rather
    than inherit whichever arm the old branch left behind.
    """
    tree = _tree("knowledge/llm_pool.py")
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "AcpClient"
    ]
    assert constructions, "llm_pool no longer constructs an AcpClient; re-check this pin"
    for call in constructions:
        passed = {kw.arg for kw in call.keywords}
        assert "acp_backend" not in passed, (
            f"llm_pool.py:{call.lineno} now selects a backend; decide the effort "
            f"channel for it instead of relying on the kiro default"
        )
    assert capabilities_for("").effort_via_config_option is False
