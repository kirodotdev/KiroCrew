"""The mirror contract's ratchet: no backend may leave a concern unanswered.

This file is the mechanism the folder alone cannot provide. ``providers/mirrors/``
makes a spec projection easy to find and easy to copy; it does not make anyone
write one. These tests do, by failing when a backend is registered with a concern
it has not ruled on -- which is exactly the failure that let the same
missing-tools defect ship on KAS and then again on claude-agent-acp.

Design: ``docs/request-for-change/rfc-agent-config-mirror.md``.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from kiro_crew.acp_backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.agent_sdk.backends import BASELINE_SELECTABLE_BACKENDS
from kiro_crew.agent_sdk.mcp_refs import unresolved_server_refs
from kiro_crew.providers.mirrors import (
    MIRRORS,
    PROJECTIONS,
    Concern,
    Disposition,
    McpProjection,
    ProjectionKind,
    Ruling,
    has_mirror,
    mirror_for,
    projection_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where a selectable harness's admission is recorded. A ``no-channel`` backend
#: must be named there as well as here: the declaration says what the transport
#: cannot carry, and the onboarding table is what a human reads BEFORE writing any
#: of it, so a gap recorded in only one of the two is a gap the next author misses.
_ONBOARDING_DOC = Path("docs/system-specs/modules/harness-onboarding.md")

#: Prose that stands in for a decision. A kind is the decision now, so a reason
#: carrying one of these is a schedule wearing a declaration's clothes -- the exact
#: state that let a selectable backend ship with no projection and every check
#: green.
_SCHEDULE_PROSE: tuple[str, ...] = (
    "pending",
    "not yet moved",
    "has not moved",
    "unwritten",
    "not written yet",
    "nobody got round",
    "next pr",
    "coming soon",
    "todo",
)

#: Kinds that describe a finished state. The other two are addressable on purpose
#: (``channel`` / ``projection`` plus ``tracking``), so only these two are held to
#: carrying no schedule at all.
_SETTLED_KINDS = (ProjectionKind.NATIVE, ProjectionKind.MIRROR)


def _slugify_heading(line: str) -> str:
    """A markdown heading's GitHub anchor, near enough for a link check."""
    text = line.lstrip("#").strip().lower()
    text = re.sub(r"[^a-z0-9 \-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


def _tracking_resolves(tracking: str) -> bool:
    """Does *tracking* address something a reader can actually reach?

    An https URL is taken at its word -- a test must not make a network call to
    decide whether a declaration is well formed. A repo-relative ``path#anchor``
    is checked properly, because that is the form a doc reorganisation silently
    breaks, and a tracking pointer that resolves to nothing is the prose this
    record replaced with extra steps.
    """
    if tracking.startswith("https://"):
        return True
    path, _, anchor = tracking.partition("#")
    doc = _REPO_ROOT / path
    if not doc.is_file():
        return False
    if not anchor:
        return True
    headings = [
        _slugify_heading(line)
        for line in doc.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]
    return anchor.lower() in headings


#: A spec that references one server and defines it. Fed to the ref resolver to
#: ask the only question that distinguishes a `native` backend structurally: is
#: this an id whose refs are satisfied by the spec's OWN mcpServers, because the
#: harness reads that file itself? Every other kind is judged against the wire
#: array, so the same spec leaves the ref unresolved there.
_NATIVE_PROBE_SPEC = {"tools": ["@probesrv"], "mcpServers": {"probesrv": {}}}


def _resolver_reads_the_spec_itself(backend: str) -> bool:
    """Does the ref resolver credit *backend* with the spec's own servers?

    This is what makes `native` checkable by something other than a list of
    forbidden words. `native` claims the harness reads
    ``~/.kiro/agents/<name>.json`` itself, and exactly one other module already
    has to know that:
    ``agent_sdk.mcp_refs`` satisfies a ``@server`` ref from the spec's own
    definitions for such a backend and from the wire array for every other. So a
    declaration claiming `native` is cross-checked against the resolver that acts
    on the claim, in a different package, rather than against how its prose is
    worded.
    """
    return unresolved_server_refs(_NATIVE_PROBE_SPEC, [], backend=backend) == []


def projection_complaints(
    *,
    projections: dict,
    mirrors: dict,
    known: frozenset,
    selectable: frozenset,
    onboarding: str,
) -> list[str]:
    """Every way a set of declarations fails the contract, as one list.

    A function over its inputs rather than a test reading the module's globals,
    for one reason: the rules have to be provable in BOTH directions. A suite that
    only asserts the shipped tables pass cannot show that a prose-only entry on a
    selectable backend would fail -- and "the guard would have caught it" is the
    claim this whole file exists to make good on. The negative case feeds doctored
    tables through this same function, so the rule under test is the rule that
    ships.
    """
    complaints: list[str] = []

    for backend in sorted(known | selectable):
        declared = projections.get(backend)
        if declared is None:
            complaints.append(f"{backend!r}: no PROJECTIONS entry")
            continue
        in_mirrors = backend in mirrors
        if declared.kind is ProjectionKind.MIRROR and not in_mirrors:
            complaints.append(f"{backend!r}: declares kind=mirror with no MIRRORS class")
        if in_mirrors and declared.kind is not ProjectionKind.MIRROR:
            complaints.append(
                f"{backend!r}: has a MIRRORS class but declares kind={declared.kind.value}"
            )
        if (declared.kind is ProjectionKind.NATIVE) != _resolver_reads_the_spec_itself(backend):
            claimed = "claims native" if declared.kind is ProjectionKind.NATIVE else "does not"
            complaints.append(
                f"{backend!r}: {claimed} claim native, but agent_sdk.mcp_refs "
                "disagrees about whether this backend resolves refs from the spec "
                "itself -- the kind and the resolver acting on it must not diverge"
            )
        if declared.kind in _SETTLED_KINDS:
            hit = [p for p in _SCHEDULE_PROSE if p in declared.reason.lower()]
            if hit:
                complaints.append(
                    f"{backend!r}: kind={declared.kind.value} reason reads as a "
                    f"schedule ({', '.join(hit)}) -- an unfinished projection has "
                    "its own kind"
                )
        if declared.tracking and not _tracking_resolves(declared.tracking):
            complaints.append(f"{backend!r}: tracking {declared.tracking!r} resolves to nothing")
        if declared.kind is ProjectionKind.EXTERNAL:
            try:
                importlib.import_module(declared.projection)
            except Exception as exc:
                complaints.append(
                    f"{backend!r}: external projection {declared.projection!r} "
                    f"does not import ({exc})"
                )
        if declared.kind is ProjectionKind.NO_CHANNEL and backend in selectable:
            if backend not in onboarding:
                complaints.append(
                    f"{backend!r}: selectable and no-channel, but "
                    f"{_ONBOARDING_DOC} does not name it"
                )
    return complaints


def _onboarding_text() -> str:
    return (_REPO_ROOT / _ONBOARDING_DOC).read_text(encoding="utf-8")


class TestEveryBackendIsAccountedFor:
    def test_every_known_and_selectable_backend_resolves_to_one_declaration(self):
        """The whole point: absence must be a statement, not an oversight.

        A backend with no declaration is the shape of the original defect --
        nobody said it needed a projection, so nobody wrote one, and the session
        came up missing its tools with no error. Selectable ids are checked
        alongside the known ones rather than assumed to be a subset: an edition
        plugin registers its own, and the one that reaches the dashboard switch
        without an entry here is exactly the one this rule is for.
        """
        complaints = projection_complaints(
            projections=PROJECTIONS,
            mirrors=MIRRORS,
            known=ACP_BACKENDS_KNOWN,
            selectable=BASELINE_SELECTABLE_BACKENDS,
            onboarding=_onboarding_text(),
        )
        assert not complaints, "\n".join(complaints)

    def test_every_declaration_states_a_real_reason(self):
        for backend, declared in PROJECTIONS.items():
            assert (
                len(declared.reason.strip()) > 40
            ), f"{backend!r} needs a real reason, not a label"

    def test_an_undeclared_backend_raises_rather_than_returning_none(self):
        """Fail loud. Returning None would read as 'declared to need no mirror'."""
        with pytest.raises(KeyError, match="no agent-config mirror"):
            mirror_for("a-backend-nobody-registered")
        with pytest.raises(KeyError, match="no agent-config mirror"):
            projection_for("a-backend-nobody-registered")

    def test_mirror_for_returns_none_for_every_kind_but_mirror(self):
        for backend, declared in PROJECTIONS.items():
            expected_mirror = declared.kind is ProjectionKind.MIRROR
            assert (mirror_for(backend) is not None) is expected_mirror
            assert has_mirror(backend) is expected_mirror

    def test_kas_is_declared_external_rather_than_carrying_a_schedule(self):
        """KAS is the entry the prose form got wrong, so it is pinned by name.

        Its projection is real and complete and travels as
        ``_meta.kiro.customAgents``; what is outstanding is only which folder the
        code sits in. ``external`` says that and names the module, so the reader
        who wants the projection can go and read it -- where the prose form said
        "not moved here yet", which a test cannot tell apart from "not written".
        """
        declared = projection_for(ACP_BACKEND_KAS)
        assert declared.kind is ProjectionKind.EXTERNAL
        assert declared.projection == "kiro_crew.acp.kas_agents"
        assert declared.tracking

    def test_the_one_no_channel_backend_names_what_would_have_to_exist(self):
        declared = projection_for(ACP_BACKEND_OPENCODE)
        assert declared.kind is ProjectionKind.NO_CHANNEL
        assert declared.channel.strip()
        assert declared.tracking.strip()


class TestRulingsAreComplete:
    @pytest.mark.parametrize("backend", sorted(MIRRORS))
    def test_a_mirror_rules_on_every_concern(self, backend):
        """Completeness is the ratchet.

        Adding a Concern obliges every backend to answer it, which is what stops a
        new setting being delivered to one backend and silently dropped by the rest.
        """
        rulings = mirror_for(backend).rulings()
        missing = sorted(c.value for c in Concern if c not in rulings)
        assert not missing, f"{backend!r} has not ruled on: {missing}"

    @pytest.mark.parametrize("backend", sorted(MIRRORS))
    def test_the_mirror_declares_the_backend_it_serves(self, backend):
        mirror = mirror_for(backend)
        assert mirror.backend == backend
        assert mirror.backend in ACP_BACKENDS_KNOWN

    @pytest.mark.parametrize("backend", sorted(MIRRORS))
    def test_every_no_channel_ruling_names_its_destination(self, backend):
        """A no-channel gap is a backlog item, so it must have an address.

        An unaddressed gap is indistinguishable from a decision, and conflating
        those two is the documented cause of the hooks regression (see
        UNSUPPORTED_SPEC_KEYS in acp/kas_agents.py).
        """
        for concern, ruling in mirror_for(backend).rulings().items():
            if ruling.disposition is Disposition.NO_CHANNEL:
                assert (
                    ruling.channel.strip()
                ), f"{backend!r} {concern.value}: no-channel with no channel named"


class TestRulingRejectsAnIncoherentClaim:
    def test_a_reason_is_required(self):
        with pytest.raises(ValueError, match="needs a reason"):
            Ruling(Disposition.DELIVERED, "   ")

    def test_no_channel_without_a_channel_is_refused(self):
        with pytest.raises(ValueError, match="must name the channel"):
            Ruling(Disposition.NO_CHANNEL, "the backend supports it")

    def test_a_channel_on_any_other_disposition_is_refused(self):
        """A channel on a delivered ruling would read as a gap that is not one."""
        with pytest.raises(ValueError, match="only meaningful"):
            Ruling(Disposition.DELIVERED, "sent on the wire", channel="somewhere")


class TestCodexMirror:
    """Codex is the folder's second mirror, and the first to use ONE face.

    Claude proved both faces are ordinary. Codex proves the wire face alone is
    too, which matters because ``write_files`` is create-or-decline and Crew
    writes no codex file at all -- ``~/.codex/config.toml`` is the operator's.
    """

    def test_codex_uses_only_the_wire_face(self, tmp_path):
        mirror = mirror_for("codex")
        before = set(tmp_path.rglob("*"))
        assert mirror.write_files("kirocrew", work_dir=tmp_path) is None
        assert set(tmp_path.rglob("*")) == before

    def test_hooks_is_the_ONE_open_gap_and_it_is_addressed(self):
        """Exactly one thing codex can do that this transport cannot carry.

        Recorded as ``no-channel`` with a destination, because an unaddressed gap is
        indistinguishable from a decision — the documented cause of the hooks
        regression (``UNSUPPORTED_SPEC_KEYS`` in ``acp/kas_agents.py``).

        ``disabledTools`` is NOT one, and the asymmetry is the point: it is a
        RESTRICTION, and a restriction with no channel is honoured by withholding the
        server it narrows, so it rules ``translated``. A lost capability can be
        recorded as a gap and lived with; a lost restriction cannot.
        """
        rulings = mirror_for("codex").rulings()
        gaps = {c.value for c, r in rulings.items() if r.disposition is Disposition.NO_CHANNEL}
        assert gaps == {"hooks"}

    def test_the_wire_face_returns_the_mcp_servers_key(self):
        params = mirror_for("codex").session_params(None)
        assert isinstance(params["mcpServers"], list)

    def test_the_wire_face_does_NOT_fail_closed_on_claudes_precondition(self):
        """The one place copying claude would have been actively wrong.

        ``permission_surface_owned`` names a claude file. Failing closed on it here
        would withhold every Crew tool from every codex session on the strength of
        a condition that does not describe the backend -- which is the defect this
        folder exists to catch, arriving through the fix for it.
        """
        assert mirror_for("codex").session_params(None) == mirror_for("codex").session_params(
            None, permission_surface_owned=False
        )


class TestClaudeCodeMirror:
    def test_hooks_is_the_one_open_gap_and_it_is_addressed(self):
        """Pins the state the RFC's hooks plan starts from.

        Claude Code runs hooks natively; nothing writes them today. When phase H2
        lands, this flips to delivered/translated and this test is what says so.
        """
        rulings = mirror_for("claude").rulings()
        hooks = rulings[Concern.HOOKS]
        assert hooks.disposition is Disposition.NO_CHANNEL
        assert "settings.local.json" in hooks.channel

    def test_auto_approve_is_withheld_not_missing(self):
        """The gate boundary is a decision, and must not read as an oversight."""
        ruling = mirror_for("claude").rulings()[Concern.AUTO_APPROVE]
        assert ruling.disposition is Disposition.WITHHELD
        assert "gate" in ruling.reason

    def test_the_wire_face_returns_the_mcp_servers_key(self, tmp_path):
        params = mirror_for("claude").session_params(None, permission_surface_owned=True)
        assert "mcpServers" in params
        assert isinstance(params["mcpServers"], list)

    def test_the_wire_face_withholds_everything_by_default(self):
        """The precondition defaults to withholding, so forgetting it fails closed.

        Delivering tools into a permission surface Crew does not own hands the
        session a capability nothing can withhold -- a pre-approved tool never
        sends ``session/request_permission``, so Crew's gate never fires. A caller
        that omits the flag therefore gets nothing rather than everything.
        """
        assert mirror_for("claude").session_params(None) == {"mcpServers": []}
        assert mirror_for("claude").session_params(None, permission_surface_owned=False) == {
            "mcpServers": []
        }

    def test_the_mcp_ruling_states_the_precondition(self):
        """The folder is the inventory, so the condition has to be readable there.

        A ruling that says only "delivered" would let the next backend copy the
        delivery and drop the condition that makes it safe.
        """
        ruling = mirror_for("claude").rulings()[Concern.MCP_SERVERS]
        assert ruling.disposition is Disposition.DELIVERED
        assert "settings.local.json" in ruling.reason


class TestAProseOnlyDeclarationFailsTheParityTest:
    """The negative half, and the reason the rules are a function of their inputs.

    Every assertion above says the shipped tables pass. None of them shows that
    the tables COULD fail, and a guard nobody has watched fail is a guard nobody
    knows the shape of. These feed a fake selectable backend through the same
    checker the real test uses.
    """

    def _tables(self, declared: McpProjection) -> dict:
        return dict(
            projections={**PROJECTIONS, "fakebackend": declared},
            mirrors=MIRRORS,
            known=frozenset(ACP_BACKENDS_KNOWN | {"fakebackend"}),
            selectable=frozenset(BASELINE_SELECTABLE_BACKENDS | {"fakebackend"}),
            onboarding=_onboarding_text(),
        )

    def test_a_selectable_backend_with_no_declaration_at_all_fails(self):
        complaints = projection_complaints(
            projections=PROJECTIONS,
            mirrors=MIRRORS,
            known=frozenset(ACP_BACKENDS_KNOWN | {"fakebackend"}),
            selectable=frozenset(BASELINE_SELECTABLE_BACKENDS | {"fakebackend"}),
            onboarding=_onboarding_text(),
        )
        assert any("no PROJECTIONS entry" in c for c in complaints)

    def test_a_settled_kind_carrying_a_schedule_fails(self):
        """The exact entry the prose form allowed: a paragraph instead of a kind.

        A backend claiming it needs no projection while its own reason explains
        that the projection is pending is the state that shipped four times. It
        parses, it reads plausibly, and nothing could fail on it.
        """
        complaints = projection_complaints(
            **self._tables(
                McpProjection(
                    kind=ProjectionKind.NATIVE,
                    reason=(
                        "this harness has the most complete projection of any backend, "
                        "it simply has not moved into this folder yet -- tracked as the "
                        "next PR in the mirror stack"
                    ),
                )
            )
        )
        assert any("reads as a schedule" in c for c in complaints)

    def test_a_native_claim_the_ref_resolver_does_not_credit_fails(self):
        """The one kind a list of forbidden words could not keep honest.

        `mirror` needs a registered class, `external` needs an importable module,
        `no-channel` needs a channel and an onboarding row -- each is checked
        against something outside the declaration. `native` is checked against
        ``agent_sdk.mcp_refs``, which has to know the same fact to resolve a ref at
        all, so a backend declared `native` that the resolver judges against the
        wire array is a divergence rather than a wording choice.
        """
        complaints = projection_complaints(
            **self._tables(
                McpProjection(
                    kind=ProjectionKind.NATIVE,
                    reason="this harness is handed the agent spec and reads it itself",
                )
            )
        )
        assert any("agent_sdk.mcp_refs disagrees" in c for c in complaints)

    def test_a_no_channel_backend_absent_from_the_onboarding_table_fails(self):
        complaints = projection_complaints(
            **self._tables(
                McpProjection(
                    kind=ProjectionKind.NO_CHANNEL,
                    reason="its initialize advertises no transport the array can use",
                    channel="an http endpoint the shared gateway serves",
                    tracking="docs/request-for-change/rfc-agent-config-mirror.md#5-migration",
                )
            )
        )
        assert any("does not name it" in c for c in complaints)

    def test_a_tracking_pointer_that_resolves_to_nothing_fails(self):
        complaints = projection_complaints(
            **self._tables(
                McpProjection(
                    kind=ProjectionKind.EXTERNAL,
                    reason="its projection lives beside the transport that carries it",
                    projection="kiro_crew.acp.kas_agents",
                    tracking="docs/request-for-change/rfc-agent-config-mirror.md#no-such-heading",
                )
            )
        )
        assert any("resolves to nothing" in c for c in complaints)

    def test_an_external_projection_naming_no_module_fails(self):
        complaints = projection_complaints(
            **self._tables(
                McpProjection(
                    kind=ProjectionKind.EXTERNAL,
                    reason="its projection lives beside the transport that carries it",
                    projection="kiro_crew.acp.no_such_module",
                    tracking="docs/request-for-change/rfc-agent-config-mirror.md#5-migration",
                )
            )
        )
        assert any("does not import" in c for c in complaints)

    def test_a_mirror_kind_with_no_registered_class_fails(self):
        complaints = projection_complaints(
            **self._tables(
                McpProjection(
                    kind=ProjectionKind.MIRROR,
                    reason="a mirror class in this folder projects the spec for it",
                )
            )
        )
        assert any("no MIRRORS class" in c for c in complaints)


class TestMcpProjectionRejectsAnIncoherentClaim:
    def test_a_reason_is_required(self):
        with pytest.raises(ValueError, match="needs a reason"):
            McpProjection(kind=ProjectionKind.NATIVE, reason="  ")

    def test_no_channel_without_a_channel_is_refused(self):
        with pytest.raises(ValueError, match="must name the channel"):
            McpProjection(
                kind=ProjectionKind.NO_CHANNEL,
                reason="its transports cannot carry a stdio server",
                tracking="https://example.invalid/1",
            )

    def test_no_channel_without_tracking_is_refused(self):
        with pytest.raises(ValueError, match="not a kind"):
            McpProjection(
                kind=ProjectionKind.NO_CHANNEL,
                reason="its transports cannot carry a stdio server",
                channel="an http endpoint the gateway serves",
            )

    def test_external_without_a_module_is_refused(self):
        with pytest.raises(ValueError, match="must name the module"):
            McpProjection(
                kind=ProjectionKind.EXTERNAL,
                reason="the projection lives beside its transport",
                tracking="https://example.invalid/1",
            )

    def test_a_channel_on_a_settled_kind_is_refused(self):
        """A channel on a native declaration would read as a gap that is not one."""
        with pytest.raises(ValueError, match="only meaningful for a no-channel"):
            McpProjection(
                kind=ProjectionKind.NATIVE,
                reason="it reads the spec itself",
                channel="somewhere",
            )

    def test_tracking_on_a_settled_kind_is_refused(self):
        """A finished state has nothing outstanding, so it has nothing to track."""
        with pytest.raises(ValueError, match="not a finished state"):
            McpProjection(
                kind=ProjectionKind.MIRROR,
                reason="a mirror in this folder projects it",
                tracking="https://example.invalid/1",
            )


class TestAMirrorMustActuallyDeliverItsServers:
    @pytest.mark.parametrize("backend", sorted(MIRRORS))
    def test_mcp_servers_is_delivered_or_translated_never_a_gap(self, backend):
        """``kind=mirror`` is a claim about the servers, so the ruling must agree.

        A mirror whose own ``mcpServers`` ruling is ``no-channel`` or ``withheld``
        describes a backend that gets none of Crew's tools -- the very state
        ``no-channel`` is the kind FOR. Allowing the two to disagree would let a
        registered mirror stand in for the declaration that says so, which is the
        prose problem again one level down.
        """
        ruling = mirror_for(backend).rulings()[Concern.MCP_SERVERS]
        assert ruling.disposition in (
            Disposition.DELIVERED,
            Disposition.TRANSLATED,
        ), f"{backend!r} is registered as a mirror but rules mcpServers {ruling.disposition.value}"
