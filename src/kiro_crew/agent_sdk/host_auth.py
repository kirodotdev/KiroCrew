"""How each agent backend signs in, declared once per harness.

Before this module every auth fact about a harness was an edit to a shared table
or an ``if`` chain in a host module: the credential floor spelled the token leaf,
the sandbox mask spelled the one leaf to leave readable, a membership set spelled
whether a host logout may retire a running child, and the panel spelled the
sign-in advice in thirteen locale files. Nothing in the tree was a per-harness
auth object, so a harness that brought its own sign-in was onboarded by touching
twenty-four files -- and the harness that shipped selectable first touched none
of them, which is how a live agent-readable OAuth token sat off the floor while
that harness was already selectable.

One declaration per harness, and the host layers PROJECT from it:

============================================  ================================
consumer                                      projection it reads
============================================  ================================
``security.paths`` credential floor           :func:`credential_leaves`,
                                              :func:`override_anchored_leaves`,
                                              :func:`home_override_env_vars`
``agent_sdk.tool_gate`` own-leaf exclusion    :data:`AGENT_AUTH_DECLARATIONS`
``agent_sdk.backends`` logout-recycle set     :func:`backends_retired_by_host_logout`
``acp`` auth-required message                 :func:`signed_out_message`
``cli_doctor`` sign-in row                    :func:`declaration_for`
``GET /api/acp-backends`` auth object         :func:`declaration_for`
============================================  ================================

The split is the security property, not a tidiness one. A driver declares WHAT IT
STORES; the host decides what is fenced. A driver that could edit the mask could
exclude its way out of it, so no field here names a path to leave open in general
-- ``adapter_own_leaves`` may only name a leaf this same declaration already put
ON the floor, and :meth:`AgentAuthDeclaration.__post_init__` refuses a
declaration that tries otherwise.

Why this module is stdlib-only
------------------------------
``security.paths`` reads this table while building the credential floor, and it
is imported very early -- which is precisely why it reads
:mod:`kiro_crew.identity_stores`, a stdlib-only leaf, rather than anything
heavier. This table has to sit on the same footing or the floor cannot read it
without closing an import cycle. So the only non-stdlib import here is
:mod:`kiro_crew.agent_sdk.backends`, itself stdlib-only, for the backend ids.
``test/test_agent_sdk_host_auth.py`` pins that: it walks this module's own
module-scope imports against an allowlist AND spawns a cold interpreter to
assert importing it pulls in no third-party module and none of
``kiro_crew.security``, ``kiro_crew.acp``, ``kiro_crew.config`` or
``kiro_crew.sandbox``.

Interactive login is the OPTIONAL half
--------------------------------------
A harness that brings its own sign-in -- Codex, Claude Code -- needs no in-product
login flow, and :class:`AgentInteractiveLogin` is how that absence stays visible.
It is a ``runtime_checkable`` protocol tested with ``isinstance``, never a
boolean field, so a driver that does not implement it is a shape the type system
can see rather than one that answers a flag and then no-ops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Protocol, Tuple, runtime_checkable

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
)

# ── Where a harness's entitlement comes from ──
# Three named sources rather than a free string: the doctor row, the panel and the
# logout policy all branch on this, and a typo'd fourth spelling would read as a
# harness nobody has an answer for instead of failing the parity test.

#: Signed in through the HOST's own identity store -- the one ``kiro-cli login``
#: writes. A logout there invalidates a running child, which is what makes
#: ``host_logout_retires_children`` true for these harnesses and only these.
ENTITLEMENT_HOST_IDENTITY_STORE = "host_identity_store"

#: Signed in through the harness's OWN credential file, which it writes and reads
#: itself. Kiro Crew never reads it and only ever checks that it exists.
ENTITLEMENT_OWN_CREDENTIAL_FILE = "own_credential_file"

# Two sources, because two are constructed. A harness whose entitlement arrives
# from the ambient cloud environment (an AWS profile, an instance role) rather than
# from a file it owns would add a third here, with its label, when it exists.

#: Entitlement source -> what to call it in front of an operator.
#:
#: The identifiers above are code, and doctor printed them raw: a triage row read
#: ``own_credential_file``, and the KAS token row went from "owned by kiro-cli" to
#: ``host_identity_store``. Snake_case internals in output a human is meant to read
#: is a regression whether or not the fact behind it is right, so the label lives
#: beside the identifier rather than being spelled at each print site.
ENTITLEMENT_LABELS: Dict[str, str] = {
    ENTITLEMENT_HOST_IDENTITY_STORE: "kiro-cli's own sign-in",
    ENTITLEMENT_OWN_CREDENTIAL_FILE: "the harness's own credential file",
}

ENTITLEMENT_SOURCES: FrozenSet[str] = frozenset(
    {
        ENTITLEMENT_HOST_IDENTITY_STORE,
        ENTITLEMENT_OWN_CREDENTIAL_FILE,
    }
)


@dataclass(frozen=True)
class AgentAuthDeclaration:
    """Everything one harness's sign-in commits the host to.

    Frozen, and every field is either data or a plain string, so a consumer can
    read it from inside the credential floor's own construction without reaching
    a resolver, the config, or the filesystem.

    ``credential_leaves`` and ``adapter_own_leaves`` are HOME-RELATIVE and
    authored with POSIX ``/`` separators on every host, matching the floor's own
    spelling -- ``security.paths`` splits them and re-joins with
    ``os.path.join`` so the same declaration names the same file under Windows
    separators.
    """

    #: The harness id, as ``acp_backend`` spells it. ``""`` is kiro-cli.
    backend: str

    #: Credential leaves this harness STORES, home-relative. Spliced onto the
    #: read-gate floor, so an agent's file tools can never read one.
    #:
    #: Empty for a harness whose entitlement is the host identity store: those
    #: locations are the HOST's, declared by :mod:`kiro_crew.identity_stores`
    #: and fenced from there. A harness re-declaring them would give a driver a
    #: say over the host's own store.
    credential_leaves: Tuple[str, ...]

    #: Environment variables that relocate this harness's credential HOME.
    #:
    #: An override moves the real file out from under the ``$HOME``-rooted
    #: anchor, so the floor re-anchors every declared leaf under each of these
    #: as well. Naming them here is what makes a relocated token still fenced.
    home_override_env_vars: Tuple[str, ...]

    #: The leaf this harness's OWN child must still be able to read.
    #:
    #: The sandbox mask hides the whole credential floor from an enforced
    #: harness's child; without this the adapter cannot authenticate. MUST be a
    #: subset of :attr:`credential_leaves`: excluding a leaf the declaration
    #: never put on the floor is either a no-op or an attempt to open something
    #: the host fenced for another reason.
    adapter_own_leaves: Tuple[str, ...]

    #: What an operator DOES to sign this harness in. Rendered VERBATIM.
    #:
    #: States the ACTION and never the STATE, because most of the places it
    #: appears have taken no measurement: the backend panel shows it as a standing
    #: caveat, and the doctor row prints it under an explicit "not checked here".
    #: A sentence asserting "is not signed in" in those places tells an operator
    #: who IS signed in something false, which is how a standing line trains its
    #: reader to skip it. Where the host does have evidence, it uses
    #: :attr:`signed_out_message` instead.
    #:
    #: PROSE, not terminal output. The dashboard renders this as plain text in a
    #: div, so it must read as a sentence there: an em dash rather than the ``--``
    #: this codebase writes in comments, and no markdown backticks, both of which
    #: reach the reader literally one line below sibling copy that uses a real em
    #: dash. :attr:`signed_out_message` is under no such rule -- it goes to a
    #: terminal row and to the chat error card, which render both.
    #:
    #: Server-owned and untranslated on purpose. A translated per-harness string
    #: is a per-harness edit to thirteen locale files by construction, and an
    #: untranslated remedy that is correct beats a translated one nobody adds.
    #: Carries no product name for the same reason: the string must survive being
    #: handed to a renderer that does no interpolation.
    sign_in_remedy: str

    #: The message for a harness the host has EVIDENCE is signed out.
    #:
    #: Used only on that evidence -- the auth-required raise sites, which reach it
    #: because the harness's own stderr said so, and the doctor row for the host
    #: identity store, which is the one store this core can read. It may therefore
    #: assert the state, which is exactly what :attr:`sign_in_remedy` may not.
    signed_out_message: str

    #: Whether a HOST logout may retire this harness's already-running children.
    #:
    #: True only for a harness that resolves its tokens from the host identity
    #: store: retiring a child over a store it never reads would end a live turn
    #: for no reason, and NOT retiring one that does read it would keep serving
    #: turns on the previous account's credentials.
    host_logout_retires_children: bool

    #: Which of :data:`ENTITLEMENT_SOURCES` this harness's entitlement comes from.
    entitlement_source: str

    # There is deliberately NO field for re-exposing a file the mask hides.
    #
    # A re-exposure is an EDIT to the mask, and the rule this class exists to
    # enforce is that a driver declares what it stores while the host decides what
    # is fenced -- a driver that could edit the mask could unmask itself. No
    # structural check can recover that: the one legitimate re-exposure in the tree
    # (``.aws/config``, which a Bedrock-backed adapter reads to find its
    # ``credential_process``) and the worst illegitimate one (``.ssh/id_rsa``) are
    # both files under a directory the mask hides, so "must sit under something
    # masked" admits them equally. The host keeps that table in
    # :mod:`kiro_crew.agent_sdk.tool_gate`, where adding an entry is a change to a
    # security control and reads like one.

    def __post_init__(self) -> None:
        """Refuse a declaration the host cannot honour.

        Raising at import time is the point. Every consumer here is a security
        control reading this table during its own construction, so a malformed
        declaration must stop the process rather than reach a floor that quietly
        fences less than the author believed.
        """
        if self.entitlement_source not in ENTITLEMENT_SOURCES:
            raise ValueError(
                f"{self.backend!r} declares unknown entitlement source "
                f"{self.entitlement_source!r}; known: {sorted(ENTITLEMENT_SOURCES)}"
            )
        if not self.sign_in_remedy.strip():
            raise ValueError(f"{self.backend!r} declares no sign-in remedy")
        if not self.signed_out_message.strip():
            raise ValueError(f"{self.backend!r} declares no signed-out message")
        stray = tuple(
            leaf for leaf in self.adapter_own_leaves if leaf not in self.credential_leaves
        )
        if stray:
            raise ValueError(
                f"{self.backend!r} would exclude {stray!r} from the credential mask, but "
                "does not declare it as a credential leaf: a driver may only ask the mask "
                "to spare a leaf its own declaration put on the floor"
            )
        if self.host_logout_retires_children and (
            self.entitlement_source != ENTITLEMENT_HOST_IDENTITY_STORE
        ):
            raise ValueError(
                f"{self.backend!r} would be retired by a host logout while resolving its "
                f"entitlement from {self.entitlement_source!r}: a logout says nothing about "
                "a store the harness never reads"
            )
        if (
            self.entitlement_source == ENTITLEMENT_HOST_IDENTITY_STORE
            and not self.host_logout_retires_children
        ):
            # The reverse direction is refused too. A harness that resolves its
            # tokens from the host store but is NOT retired when that store changes
            # account keeps serving turns on the previous account's credentials,
            # which is the exact failure the recycle set exists to prevent.
            raise ValueError(
                f"{self.backend!r} resolves its entitlement from the host identity store "
                "but would survive a host logout: a child on that store must be retired "
                "when the store starts naming a different account"
            )


#: The fail-closed answer for a harness this build cannot name.
#:
#: Declares nothing, fences nothing, and is NOT retired by a host logout -- an
#: unknown harness must not have a live turn ended over a store there is no
#: evidence it reads. The remedy is generic because a specific one would be a
#: guess, and a wrong sign-in instruction costs an operator more than a vague one.
UNKNOWN_AGENT_AUTH = AgentAuthDeclaration(
    backend="",
    credential_leaves=(),
    home_override_env_vars=(),
    adapter_own_leaves=(),
    sign_in_remedy=(
        "No sign-in instruction is recorded for this agent backend. Complete the "
        "harness's own sign-in, then start a new chat."
    ),
    signed_out_message=(
        "This agent backend is not signed in, and Kiro Crew has no sign-in "
        "instruction recorded for it. Complete the harness's own sign-in, then "
        "start a new chat."
    ),
    host_logout_retires_children=False,
    entitlement_source=ENTITLEMENT_OWN_CREDENTIAL_FILE,
)


#: kiro-cli's action, and its signed-out statement, kept separate for the reason on
#: the two fields. The statement is the string the auth-required path has always
#: raised, unchanged: that path reaches it on the harness's own stderr evidence.
_KIRO_REMEDY = "Run kiro-cli login in your terminal, then start a new chat."
_KIRO_SIGNED_OUT = (
    "kiro-cli is not logged in. Run `kiro-cli login` in your terminal, then start a new chat."
)


#: Every harness this build knows, and how it signs in. Table order is projection
#: order, so the floor, the mask and the panel all read the harnesses in one order.
#:
#: ``test/test_agent_sdk_host_auth.py`` fails when a member of
#: ``ACP_BACKENDS_KNOWN`` is missing here. That is the parity gate: a harness
#: becomes selectable by joining that set, and joining it without an auth answer
#: is exactly how a live OAuth token stayed off the credential floor once already.
AGENT_AUTH_DECLARATIONS: Tuple[AgentAuthDeclaration, ...] = (
    AgentAuthDeclaration(
        backend=ACP_BACKEND_KIRO,
        # None of its own. kiro-cli signs in to the HOST identity store, whose
        # locations ``identity_stores.IDENTITY_STORE_ROOTS`` declares and splices
        # onto the floor itself -- eight directories across three platforms, plus
        # their WAL/SHM/journal sidecars. Re-declaring them here would hand a
        # driver a say over the host's own store.
        credential_leaves=(),
        home_override_env_vars=(),
        adapter_own_leaves=(),
        sign_in_remedy=_KIRO_REMEDY,
        signed_out_message=_KIRO_SIGNED_OUT,
        host_logout_retires_children=True,
        entitlement_source=ENTITLEMENT_HOST_IDENTITY_STORE,
    ),
    AgentAuthDeclaration(
        backend=ACP_BACKEND_KAS,
        # Not an independent harness: it is spawned as ``kiro-cli acp
        # --agent-engine v3 --auth-method cli`` unless Crew's own vault holds an
        # identity, and that ``--auth-method cli`` is the demonstration -- the relay
        # resolves every access token from kiro-cli's own store. So it stores
        # nothing of its own, carries kiro's remedy verbatim, and IS retired by a
        # host logout: excluding it would let a KAS session keep serving turns on
        # the previous account's credentials. In the Crew-owned spawn
        # (``ACP_BACKENDS_HOST_AUTH_CALLBACK``) a recycle on kiro-cli logout is
        # harmless -- the replacement re-probes the vault and comes back
        # Crew-owned -- so the answer stays conservative rather than becoming
        # spawn-dependent.
        credential_leaves=(),
        home_override_env_vars=(),
        adapter_own_leaves=(),
        sign_in_remedy=_KIRO_REMEDY,
        signed_out_message=_KIRO_SIGNED_OUT,
        host_logout_retires_children=True,
        entitlement_source=ENTITLEMENT_HOST_IDENTITY_STORE,
    ),
    AgentAuthDeclaration(
        backend=ACP_BACKEND_CODEX,
        credential_leaves=(".codex/auth.json",),
        home_override_env_vars=("CODEX_HOME",),
        # The one leaf the mask must spare: the adapter authenticates ITSELF, so a
        # mask that took its own token away would fail every session at start with
        # an opaque authentication error. The asymmetry is safe -- the read gate
        # still refuses that leaf to the AGENT's file tools, so the two controls
        # cover different readers rather than cancelling each other.
        adapter_own_leaves=(".codex/auth.json",),
        sign_in_remedy=(
            "Codex is a separate tool that signs in on its own — complete its "
            "sign-in, or name a model provider in ~/.codex/config.toml "
            "(CODEX_HOME moves that folder). Neither is checked here: the adapter "
            "reads them."
        ),
        signed_out_message=(
            "Codex is not signed in. Complete Codex's own sign-in, or name a "
            "model provider in ~/.codex/config.toml (CODEX_HOME moves that "
            "folder), then start a new chat."
        ),
        # Excluded deliberately: it signs in through its own credentials file, so
        # a ``kiro-cli logout`` says nothing about whether a running codex session
        # is still authenticated.
        host_logout_retires_children=False,
        entitlement_source=ENTITLEMENT_OWN_CREDENTIAL_FILE,
    ),
    AgentAuthDeclaration(
        backend=ACP_BACKEND_CLAUDE,
        credential_leaves=(".claude/.credentials.json",),
        home_override_env_vars=("CLAUDE_CONFIG_DIR", "CLAUDE_HOME"),
        # Nothing excluded from the mask, because no mask is applied: this harness
        # is outside ``tool_gate.ENFORCED_ROUTINGS``, so
        # ``adapter_hidden_credential_dirs`` returns empty for it and there is
        # nothing to carve an exception out of. Declaring an exclusion anyway would
        # be an assertion about a control that never runs.
        adapter_own_leaves=(),
        sign_in_remedy=(
            "Claude Code is a separate app you sign into yourself — run claude in "
            "your terminal and complete its sign-in. It is not checked here."
        ),
        signed_out_message=(
            "Claude Code is not signed in. Run `claude` in your terminal and "
            "complete its sign-in, then start a new chat."
        ),
        host_logout_retires_children=False,
        entitlement_source=ENTITLEMENT_OWN_CREDENTIAL_FILE,
    ),
)


_BY_BACKEND: Dict[str, AgentAuthDeclaration] = {
    declaration.backend: declaration for declaration in AGENT_AUTH_DECLARATIONS
}


# ── Projections ──
# Every one is pure, total and free of I/O. That is what lets the credential floor
# call them at module scope, where reaching a resolver would re-enter the very
# import that called it.


def declaration_for(backend: str) -> AgentAuthDeclaration:
    """How *backend* signs in, fail-closed for an id this build cannot name.

    Total: never raises, so a control asking about an unrecognised harness gets
    :data:`UNKNOWN_AGENT_AUTH` -- which fences nothing extra and retires nothing
    -- instead of an exception on a security path.
    """
    return _BY_BACKEND.get(backend, UNKNOWN_AGENT_AUTH)


def credential_leaves() -> Tuple[str, ...]:
    """Every declared credential leaf, home-relative, in table order.

    Spliced into ``security.paths._SENSITIVE_HOME_DIRS`` so an agent's file tools
    can read none of them. Sibling config files -- codex's ``config.toml``,
    claude's ``settings*.json`` -- are deliberately absent and stay readable:
    routing diagnosis needs them and they carry no credential.
    """
    return tuple(
        leaf for declaration in AGENT_AUTH_DECLARATIONS for leaf in declaration.credential_leaves
    )


def home_override_env_vars() -> Tuple[str, ...]:
    """Every declared ``$HOME``-override variable, in table order, deduplicated.

    The floor resolves each of these as an anchor root, so a token an operator
    relocated is still fenced. Deduplicated because two harnesses may legitimately
    honour the same variable, and resolving it twice would cost a second
    filesystem read per gate call for one answer.
    """
    seen: Dict[str, None] = {}
    for declaration in AGENT_AUTH_DECLARATIONS:
        for env_var in declaration.home_override_env_vars:
            seen.setdefault(env_var, None)
    return tuple(seen)


def override_anchored_leaves() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """Each declared leaf paired with the override variables that relocate it.

    The leaf's own ``$HOME``-rooted form is anchored by the ordinary path in the
    floor's builder; this pairing covers only the overrides. A harness with no
    override variable is absent rather than present with an empty tuple, so a
    caller iterating this does no work for one.
    """
    return tuple(
        (leaf, declaration.home_override_env_vars)
        for declaration in AGENT_AUTH_DECLARATIONS
        if declaration.home_override_env_vars
        for leaf in declaration.credential_leaves
    )


def backends_retired_by_host_logout() -> FrozenSet[str]:
    """Harness ids a host logout may retire the running children of.

    Positive membership derived from a declared fact, not "not claude": a harness
    authenticated some other way must never be recycled on a store it never reads.

    A function, and deliberately not a module-level ``ACP_BACKENDS_*`` set. That
    naming is harness VOCABULARY, and vocabulary belongs in
    :mod:`kiro_crew.agent_sdk.backends` so provider construction rejects a typo'd id
    -- which ``scripts/check_harness_parity.py`` enforces. It cannot be defined there
    either: that module supplies the four ids this table is keyed by, so it would have
    to import the module that imports it. Neither home is right because the premise is
    wrong. This is not vocabulary anyone spells; it is derived from each harness's own
    declaration, and the ids it returns were already checked against
    ``ACP_BACKENDS_KNOWN`` by the parity test. So it stays what it is.
    """
    return frozenset(
        declaration.backend
        for declaration in AGENT_AUTH_DECLARATIONS
        if declaration.host_logout_retires_children
    )


def signed_out_message(backend: str) -> str:
    """The message for *backend* when the host has evidence it is signed out.

    Only for a caller holding that evidence -- the harness's own stderr, or the
    host identity store this core can read. Everywhere else,
    :func:`sign_in_remedy` is the honest string.
    """
    return declaration_for(backend).signed_out_message


def entitlement_label(backend: str) -> str:
    """What to call *backend*'s entitlement source in front of an operator.

    Falls back to the raw identifier only for a source this table does not name,
    which :meth:`AgentAuthDeclaration.__post_init__` already refuses -- so the
    fallback is unreachable through a declaration and exists so a direct caller
    passing an unknown string gets something rather than a KeyError.
    """
    source = declaration_for(backend).entitlement_source
    return ENTITLEMENT_LABELS.get(source, source)


def signs_in_separately(backend: str) -> bool:
    """Whether *backend* signs in somewhere other than the host identity store.

    What the panel needs in order to decide whether a standing sign-in caveat is
    worth showing at all: for a harness on the host store the sign-in is the one
    the operator already did, and repeating it there would be noise.
    """
    return declaration_for(backend).entitlement_source != ENTITLEMENT_HOST_IDENTITY_STORE


def missing_declarations() -> Tuple[str, ...]:
    """Known harness ids with no declaration here, sorted.

    Exists so the parity test names the gap rather than asserting a length.
    """
    return tuple(sorted(b for b in ACP_BACKENDS_KNOWN if b not in _BY_BACKEND))


@runtime_checkable
class AgentInteractiveLogin(Protocol):
    """An in-product sign-in flow, for a harness that has one.

    OPTIONAL, and the absence is the point. A harness that brings its own sign-in
    -- Codex, Claude Code -- simply does not implement this, and a consumer finds
    that out with ``isinstance`` rather than by reading a boolean and then calling
    a method that no-ops. A flag says "no flow" and still leaves a method there to
    call; a missing implementation cannot be called by accident.

    Declared here rather than in ``kiro_crew.auth`` so a dashboard handler can
    name the capability without taking an edge on that package -- and so a driver
    satisfies it structurally, with no base class to inherit and no registration
    step to forget.
    """

    def login_flows(self) -> Tuple[str, ...]:
        """The flow ids this harness offers, most preferred first."""
        ...

    async def begin_login(self, flow: str) -> Dict[str, object]:
        """Start *flow* and return what the operator must do next."""
        ...

    async def poll_login(self, flow: str) -> Dict[str, object]:
        """Report whether a started *flow* has completed."""
        ...

    async def logout(self) -> bool:
        """Discard this harness's credential. True when one was discarded."""
        ...


__all__ = [
    "AGENT_AUTH_DECLARATIONS",
    "AgentAuthDeclaration",
    "AgentInteractiveLogin",
    "ENTITLEMENT_HOST_IDENTITY_STORE",
    "ENTITLEMENT_LABELS",
    "ENTITLEMENT_OWN_CREDENTIAL_FILE",
    "ENTITLEMENT_SOURCES",
    "UNKNOWN_AGENT_AUTH",
    "backends_retired_by_host_logout",
    "credential_leaves",
    "declaration_for",
    "entitlement_label",
    "home_override_env_vars",
    "missing_declarations",
    "override_anchored_leaves",
    "signed_out_message",
    "signs_in_separately",
]
