"""Extension points for the services that run beside a live gateway and its dashboard.

The public-URL tunnel, the phone connection, remote-instance provisioners, telemetry
export, dashboard contributions, speech providers and the process jail -- the "runtime-service / frontend"
extension points of the Composed Platform Providers contract.

:mod:`kiro_crew.platform.interfaces` re-exports every name here and stays the one import path
callers use; this module never imports it. A new extension point of this kind goes here.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

if TYPE_CHECKING:
    from aiohttp import web

    from kiro_crew.config.loader import TelemetryConfig
    from kiro_crew.diagnostics import BundleResult


# ── runtime-service / frontend extension points ──


class TunnelProvider(Protocol):
    """Public-URL tunnel lifecycle.

    Public default = disabled/no-op (the ``tunnel/manager.py`` stub).  The
    companion supplies the internal tunnel supervisor (the Tunnels primitive
    itself is owned by PartyRock and out of scope).

    WIRED: the ``tunnel/manager.py`` stub ``TunnelManager`` delegates its
    ``start`` / ``stop`` / ``public_url`` UNCONDITIONALLY to
    ``current_context().tunnel`` (no edition/identity branch).  The Default is a
    no-op so the standalone build is byte-identical; a companion drives a real
    tunnel through this same surface.  ``dashboard/server.py`` gates enablement
    on ``enabled()`` (OR-ed with ``cfg.tunnel.enabled``), and
    ``tunnel/setup.py`` evaluates its token-auth deny gate BEFORE ``start()`` is
    reached, so a companion tunnel cannot start without dashboard token auth.

    ``register_callbacks`` and ``status_snapshot`` are the MINIMAL edition-neutral
    additions the CORS-reflection wrapper and ``/api/tunnel/status`` need so they
    can stay wrapped AROUND the provider (v1 addition; no ``CONTRACT_VERSION``
    bump).
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def public_url(self) -> str: ...

    def enabled(self) -> bool: ...

    def register_callbacks(
        self,
        *,
        on_connect: Optional[Callable[[str], Awaitable[None]]] = None,
        on_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        """Register the connect/disconnect callbacks the wrapper wires in.

        ``on_connect(url)`` reflects the live public URL into the dashboard CORS
        allow-list + ``set_tunnel_url`` (presigned-link source); ``on_disconnect()``
        tears that reflection down.  The Default no-op provider never fires them.
        Called by ``TunnelManager.start`` before it delegates ``start()``.
        """
        ...

    def status_snapshot(self) -> Optional[Dict[str, Any]]:
        """Return a live status view, or ``None`` to defer to the caller's own.

        Edition-neutral primitives only (no import of ``kiro_crew.tunnel``): keys
        ``state`` (a ``TunnelState`` value string), ``url``, ``error``,
        ``started_at``, ``connected_at``, ``reconnect_attempt``.  The Default
        returns ``None`` so the stub ``TunnelManager`` keeps reporting its own
        local status (byte-identical standalone); a companion returns its live
        snapshot so ``/api/tunnel/status`` reflects the real tunnel.
        """
        ...

    async def ensure_available(self, *, install: bool = True) -> str:
        """Zero-touch provision: ensure a tunnel CAN serve a remote link, then
        report the resulting state. Distinct from ``start()`` (which only starts
        an ALREADY-enabled tunnel) — this may install the tunnel toolchain and
        flip enablement before starting, for surfaces that mint a remote link on
        demand (the ``/kirocrew dashboard`` command, the setup wizard).

        WIRED: ``slack/allowlist.py::send_dashboard_link`` calls this (via
        ``safe_context_call``) when the box is localhost-only and no live public
        URL exists, so a shared dashboard link can bring the tunnel up on first
        use. Returns a state string: ``"connected"`` (URL live), ``"starting"``
        (provisioning in progress), ``"disabled"`` (not configured / opted out),
        or ``"unavailable"`` (install or start failed). ``install=False`` skips
        any toolchain install (start-only). Only ``"connected"`` guarantees a
        live public URL — the caller adopts a tunnel URL solely on ``"connected"``
        and otherwise keeps the local link, re-issuing later once connected.

        Public default = ``"disabled"`` — a pure no-op, so the standalone build
        never provisions anything and behaves byte-identically. A companion
        drives its real install-and-enable pipeline here (v1 addition; no
        ``CONTRACT_VERSION`` bump).
        """
        ...


@dataclass(frozen=True)
class MobileConnectMethod:
    """One way the current deployment hands a phone a live dashboard session.

    A descriptor says WHICH method exists — never how to mint the credential.
    Minting stays on the method's own endpoint (the tailnet QR handler, the
    one-time mobile-link handler, a companion's own route), where the caller
    bounds, owner checks, restricted-session refusals and SEL audits already
    live; a seam that returned URLs or tokens would move credential minting
    behind an interface the core cannot audit.

    ``id`` is the governed identifier: the ``methods`` ruleset of the
    ``capabilities.mobile_connect`` scope narrows on it, and the methods
    endpoint drops a denied id before the dashboard ever sees it.  ``kind``
    names the frontend renderer; the dashboard skips a kind it does not
    recognise (an older frontend renders an edition's new method as absent,
    never as a broken panel).
    """

    id: str
    kind: str


class MobileConnectProvider(Protocol):
    """Edition-contributed phone-connection methods.

    Public default = the personal-install pair (tailnet QR + one-time login
    link), reproducing today's behavior.  An enterprise companion replaces the
    list with its own method(s) — or returns ``[]``, which hides the
    dashboard's "Connect your phone" entry entirely (governance can also pin
    the capability off without an edition swap; both paths converge on an
    empty methods list).
    """

    def connect_methods(self) -> List[MobileConnectMethod]:
        """Return the deployment's methods, BEFORE governance filtering.

        WIRED: ``dashboard/handlers/mobile_connect.py::api_mobile_connect_methods``
        reads this via ``safe_context_call`` (fallback: ``[]``, hiding the
        entry rather than guessing) and filters each id through the
        ``capabilities.mobile_connect`` governance scope; the mint endpoints
        re-check the same scope per id, so the filtered list is presentation,
        never the control.
        """
        ...


@dataclass(frozen=True)
class RemoteProvisioner:
    """One way this deployment can CREATE a machine that runs a Kiro Crew
    gateway and register it as a remote instance.

    A descriptor says WHICH provisioners exist and how the dashboard should
    draw each one, never how a machine is created: creation stays behind the
    ``LaunchEngine`` the provider hands out for the id, driven by the core's
    durable launch job (``cloud/launch_job.py``), which is where cancel,
    rollback, one-launch-at-a-time and orphan reaping already live. A seam
    that let an edition run its own launch loop would move a billing action
    outside that machinery.

    ``id`` is the identifier a launch request names (``provider_id`` on
    ``POST /api/cloud/launch``) and the one a future governance scope narrows
    on. ``kind`` names the frontend form: ``aws_ec2`` is drawn by the core's
    own prerequisites + size form, any other kind needs a
    ``registerRemoteProvisionerRenderer`` on the SPA side, and the dashboard
    skips a kind it cannot draw (an older frontend renders an edition's new
    provisioner as absent, never as a broken panel).

    ``label`` is the untranslated display name shown on the selector; it is
    edition-owned copy (a product name), so it deliberately does not route
    through the core catalog. ``posix_only`` says whether a launch may run on a
    Windows gateway; the built-in shells to ``bash``/``aws`` and cannot.
    ``step_labels`` overrides the user-facing label of any of the four fixed
    launch steps (``preflight``/``provision``/``signin``/``connect``) so a
    provisioner that does not create an AWS instance is not described as one;
    the step KEYS are fixed because cancel/rollback semantics hang off them.
    """

    id: str
    kind: str
    label: str
    posix_only: bool = True
    step_labels: Tuple[Tuple[str, str], ...] = ()
    #: What the operator must SEE and confirm before this lane may launch, resolved by the
    #: provider, or ``""`` for a lane with nothing to confirm.
    #:
    #: It rides on the DESCRIPTOR because the descriptor is what a launch card is drawn from,
    #: and the point of the confirmation is that the operator reads the value and recognises
    #: a wrong one. A value obtainable only by attempting a launch and reading the refusal
    #: would make confirming a copy-paste ritual rather than a decision.
    #:
    #: ``POST /api/cloud/launch`` requires ``confirm_recipient`` exactly when the resolved
    #: descriptor carries this, so the requirement is derived from the lane rather than
    #: hard-coded to one id, and a client cannot obtain the value without first reading the
    #: list. The built-in EC2 lane leaves it empty: nothing in a configuration file chooses
    #: what its credential reaches.
    confirm_before_launch: str = ""


@dataclass(frozen=True)
class CloudInstancePolicy:
    """What an edition adds to the IAM role of every EC2 crew it launches.

    Some organizations require each EC2 instance to run a host agent (monitoring,
    inventory, patch compliance) whose permissions come from a managed policy that
    already exists in every account. The built-in launcher attaches only Session
    Manager's policy, and its permissions boundary caps the role to that, so such
    an instance is non-compliant from the moment it boots. This descriptor is how
    an edition says what those organizations need, without the public build naming
    any one of them.

    ``managed_policy_arns`` are attached to the instance role beside Session
    Manager's. Each is a full IAM policy ARN; ``{account}`` stands for the launch
    account's id, for a policy that exists at the same path in every account.

    ``boundary_statements`` are ``Allow`` statements appended to the instance
    permissions boundary. A boundary is a ceiling, so without them an attached
    policy is capped back to Session Manager and grants nothing. They do not grant
    anything themselves: the role can use a statement here only if an attached
    policy also allows it.

    Empty by default, and an empty descriptor leaves the launch, the boundary
    document and the launcher policy byte-identical to a build without this seam.
    ``cloud/host_policy.py`` validates both lists and refuses a launch whose
    descriptor fails validation.
    """

    managed_policy_arns: Tuple[str, ...] = ()
    boundary_statements: Tuple[Dict[str, Any], ...] = ()


class CloudInstancePolicyProvider(Protocol):
    """The edition's additions to every EC2 crew's instance role.

    Public default = :class:`CloudInstancePolicy` with both lists empty.
    """

    def instance_policy(self) -> CloudInstancePolicy:
        """Return the managed policies and boundary statements to add.

        WIRED: ``cloud/host_policy.py::instance_policy`` reads this, and through it
        ``cloud/iam.py`` (boundary document and the launcher-policy addendum) and
        ``cloud/ec2.py`` (the template's ``ExtraManagedPolicyArns``).
        """
        ...


class RemoteProvisionerProvider(Protocol):
    """Edition-contributed remote-instance provisioners.

    Public default = the single built-in ``aws_ec2`` descriptor backed by
    ``RealLaunchEngine``, reproducing today's Set-up tab exactly. A companion
    replaces the list with its own (keeping or dropping the built-in) and
    supplies a ``LaunchEngine`` per id: a DevSpace, a Fargate task, a fleet
    API, whatever creates a box that ends up running ``kirocrew gateway``.
    """

    def provisioners(self) -> List[RemoteProvisioner]:
        """Return the deployment's provisioners, in display order.

        WIRED: ``dashboard/handlers_cloud.py::api_cloud_provisioners`` reads
        this via ``safe_context_call`` (fallback: the built-in descriptor
        alone, so a degraded seam read keeps today's tab rather than an empty
        one) and ``api_cloud_launch_create`` resolves the requested
        ``provider_id`` against it before any job is persisted.
        """
        ...

    def engine_for(self, provisioner_id: str, *, confirmed_recipient: str = "") -> Any:
        """Return the ``cloud.launch_job.LaunchEngine`` that drives *provisioner_id*.

        Raise ``KeyError`` for an id not in :meth:`provisioners`; the handler
        answers 400 ``unknown_provisioner``. Typed ``Any`` here only to keep
        this module import-light (``cloud/launch_job.py`` is heavy); the
        contract is the five-method ``LaunchEngine`` Protocol.

        ``confirmed_recipient`` is what the OPERATOR confirmed this launch may hand a
        credential to, taken from the launch request (``confirm_recipient`` on
        ``POST /api/cloud/launch``) and passed through to the engine unresolved and
        unchecked. A lane whose launch delivers a credential to something its own
        configuration names must refuse an empty or mismatched value, and must do the
        comparison against what it is about to run rather than against the configuration it
        just read -- otherwise the configuration confirms itself. A lane with no such
        choice to make ignores it. Default empty so an implementation that has nothing to
        confirm needs no signature change.

        WIRED: ``dashboard/handlers_cloud.py::_engine``.
        """
        ...


@dataclass(frozen=True)
class OtlpDestination:
    """One OTLP/HTTP collector this edition sends telemetry to.

    A destination says WHERE telemetry goes and how to authenticate to it —
    never WHAT may be sent.  The core keeps the signal shape, the consent gate,
    attribute sanitisation, the histogram views, batching and retry; a
    destination cannot widen any of them.

    Deliberately signal-agnostic.  ``signals`` names which OTLP signals this
    collector accepts (``"metrics"`` today), so a later core that also emits
    traces or logs reads the SAME descriptor rather than needing a second
    protocol method — the OTLP/HTTP metric, span and log exporters take the same
    constructor arguments, so one descriptor already describes all three.  A
    signal the core does not emit is simply never built.

    ``name`` is a short, NON-SECRET label used in logs.  It exists because
    ``endpoint`` must never be logged (a URL can carry credentials in userinfo or
    query parameters), which would otherwise leave a multi-destination host with
    no way to tell which collector failed.

    ``session`` is an authenticated transport handed to the exporter (a
    ``requests.Session``; typed loosely so this contract does not depend on the
    HTTP client).  It is the seam a ROTATING credential composes against:
    ``Session.auth`` is re-evaluated on every request, so a short-lived OIDC/SSO
    id_token or STS credential is re-read on each export instead of being frozen
    at construction — which is exactly what ``OTEL_EXPORTER_OTLP_HEADERS``, the
    only injection point before this seam, cannot avoid.  A destination built in
    code that needs fixed headers sets them on ``Session.headers``; one declared
    as data uses ``headers_env`` below.

    ``allow``, ``point_attributes`` and ``resource_allow`` are the three NARROWING
    fields.  They keep this seam add-only in the direction that matters: each one
    can only reduce or label what THIS destination receives, and none of them can
    widen it, reach the local JSONL sink, or touch another destination.  All three
    default to today's behaviour, so an existing destination is byte-identical.

    - ``allow`` -- when set, a metric whose NAME is not in the set is dropped
      before encoding for this destination.  ``None`` means send every metric the
      core collected, which is what an operator's own collector gets.  The set
      holds full instrument names, ``kirocrew.`` prefix included.
    - ``point_attributes`` -- fixed key/value pairs added to every exported data
      point of this destination.  Values are constants fixed at construction; keys
      are checked at construction against a short reviewed allowlist
      (``metrics.destination_filter.ALLOWED_POINT_ATTRIBUTE_KEYS``) and an
      unreviewed key REFUSES the destination rather than silently dropping the
      label.  A pair here wins over a same-named attribute already on the point,
      because a receiving side that routes on the value must not be steerable from
      a call site.
    - ``resource_allow`` -- when set, this destination's exported batches carry a
      Resource reduced to these keys.  The Resource is SHARED by the local sink
      and every destination, and it carries host- and install-scoped keys; a
      destination that may receive only a few of them says so here.  A key absent
      from the shared Resource is simply absent -- nothing is invented.  ``None``
      sends the Resource as built.

    All three are enforced by an exporter WRAPPER the core builds
    (``metrics/destination_filter.py``), outside this descriptor: a destination
    declares the narrowing, the core performs it, and the wrapper rebuilds the
    batch rather than mutating SDK objects another exporter also reads.

    ``headers_env``, ``temporality`` and ``process_env_credentials`` describe how
    THIS destination's exporter is built, for a destination declared as data (a
    ``telemetry.otlp_endpoints`` entry) rather than in code with a ``session``.
    They exist because a ``requests.Session`` per entry would be built on every
    egress-posture read, which must stay cheap:

    - ``headers_env`` -- header name -> ENVIRONMENT VARIABLE name. The core reads
      each variable when it builds the exporter, so the value never sits on this
      descriptor or in a config file. A variable that is unset or blank refuses
      the destination: it gets no reader, and nothing else changes. There is no
      field for a literal header value, so no credential has to be written into
      ``config.json``.
    - ``temporality`` -- ``""`` (default) builds the reader exactly as the core
      always has (``metrics.temporality.otlp_preference``); ``"delta"`` and
      ``"cumulative"`` pin every instrument kind for this reader only, so
      ``OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`` cannot move it.
    - ``process_env_credentials`` -- ``True`` (default) leaves the exporter's own
      fallbacks in place: with no headers of its own it sends the process-wide
      ``OTEL_EXPORTER_OTLP_METRICS_HEADERS`` / ``OTEL_EXPORTER_OTLP_HEADERS``, and
      with no ``session`` it may load the OTel credential-provider session. That
      is what ``telemetry.otlp_endpoint`` and a destination built in code always
      got. ``False`` builds the exporter so neither fallback can apply: the
      destination sends only the headers it declared. Every
      ``telemetry.otlp_endpoints`` entry is ``False``, so a credential meant for
      one collector does not reach another host the operator added later.
    """

    name: str
    endpoint: str
    signals: "frozenset[str]"
    session: Optional[Any] = None
    allow: "Optional[frozenset[str]]" = None
    point_attributes: "Mapping[str, str]" = MappingProxyType({})
    resource_allow: "Optional[frozenset[str]]" = None
    headers_env: "Mapping[str, str]" = MappingProxyType({})
    temporality: str = ""
    process_env_credentials: bool = True


class FeedbackTrackerProvider(Protocol):
    """Where the gateway sends a user to file a problem report.

    The dashboard's **Report a Problem** flow (and ``kirocrew doctor``) collects
    a redacted diagnostics bundle and offers a new-issue link the user opens with
    the bundle attached. The public build points that link at the public GitHub
    repository's bug-report form, pre-filled from the bundle. An edition may run
    where that repository is unreachable, and then the one button a user is given
    after hitting a wall dead-ends. This seam retargets it. It is the backend
    half of the dashboard's ``setFeedbackTracker`` seam (the frontend-built links
    live there; this one is built by the gateway because it carries facts only
    the gateway knows).

    REPLACE-capable, like ``TipsProvider``: a supplied URL takes over from the
    public pre-filled link rather than being merged into it, because the public
    link's query string (``template=bug_report.yml``, ``labels=``, the form's
    field ids) is GitHub's and would be meaningless appended to another tracker.
    """

    def report_issue_url(
        self, bundle: "BundleResult", note: str, *, prefill: bool
    ) -> Optional[str]:
        """The new-issue URL for *bundle*, or ``None`` to keep the public one.

        WIRED: ``diagnostics._tracker_issue_url``, called once per bundle for
        the dashboard's link (``BundleResult.github_issue_url``) and once per
        ``kirocrew doctor`` print (``terminal_issue_url``). ``None`` (the public
        Default) leaves today's behaviour byte-for-byte.

        *bundle* carries the facts a report wants: ``filename``,
        ``total_redactions``, ``path_collapses`` and ``provenance`` (release,
        channel). Never put ``bundle.zip_path`` in the URL: it sits under the
        data home and leaks the OS login. *note* is the user's free text,
        untrusted; percent-encode it or leave it out.

        *prefill* says which surface the link is for. ``True`` is the dashboard,
        which renders the URL from a JSON field no redactor scans, so a long
        pre-filled query is fine. ``False`` is a terminal print: that text gets
        selected into chat and relayed by agents, and ``redact_exfiltration_urls``
        replaces any URL whose query is 200 characters or longer, so keep the
        query short or empty there.

        Read through ``safe_context_call`` at the call site. Diagnostics catches
        ``PlatformCompositionError``, logs a warning and omits the issue link
        while keeping the bundle usable, without a standalone fallback. Any other
        error, a non-string or an empty string degrades to the public link.
        v1 addition (no ``CONTRACT_VERSION`` bump).
        """
        ...


#: The one :class:`ProviderAction` kind: the user must sign in before the
#: provider can run. The dashboard renders it as a button with ``label``.
PROVIDER_ACTION_LOGIN = "login"


@dataclass(frozen=True)
class ProviderAction:
    """A step the user can take to make an unavailable provider available.

    ``kind`` is :data:`PROVIDER_ACTION_LOGIN`; ``label`` is the button text.
    """

    kind: str
    label: str


@dataclass(frozen=True)
class ProviderStatus:
    """What a provider's ``check()`` answers: can it run now, and if not, why.

    ``reason`` is a short sentence the dashboard shows beside an unavailable
    provider. ``label`` is the provider's display name; empty means "show the
    ``name``". ``action`` is set when the user can fix it themselves, for example
    a hosted provider that needs a sign-in first.

    ``error`` is set by core alone, when the check itself timed out or raised:
    the dashboard shows an error notice for it rather than a prerequisite. A
    provider's own ``check()`` cannot set it.
    """

    available: bool
    reason: Optional[str] = None
    label: str = ""
    action: Optional[ProviderAction] = None
    error: bool = False


@dataclass(frozen=True)
class AudioChunk:
    """One piece of synthesized audio from :meth:`TtsProvider.synthesize`.

    ``mime`` is ``audio/mpeg`` or ``audio/wav``, the two the dashboard already
    plays. ``sentence`` is the text this chunk speaks, when the provider knows it.
    """

    audio: bytes
    mime: str
    sentence: str = ""


class SttStreamSession(Protocol):
    """A live dictation session from :meth:`SttProvider.open_stream`.

    The shape the on-device streaming helper already has, so core drives an
    edition session with the same loop: ``start()`` answers ``None`` or a reason
    it could not start; ``feed()`` takes 16 kHz mono Int16 PCM and answers False
    once the session stops accepting audio; ``events()`` yields
    ``{"type": "partial"|"final", "text": str}`` or ``{"type": "error",
    "message": str}``; ``finish()`` ends the input so the trailing finals arrive;
    ``close()`` releases everything and is idempotent.
    """

    async def start(self) -> Optional[str]: ...

    async def feed(self, pcm: bytes) -> bool: ...

    def events(self) -> Any: ...

    async def finish(self) -> None: ...

    async def close(self) -> None: ...


class SttProvider(Protocol):
    """One edition-supplied speech-to-text recogniser.

    ``name`` is the value ``stt.provider`` stores to select it. It must not be a
    built-in name, a retired one or ``off``; boot refuses a context that tries
    (``speech_providers.validate_speech_registry``).

    WIRED: ``transcribe.transcribe_audio`` calls :meth:`transcribe_file`,
    ``transcribe.availability_detail`` and the STT capability list call
    :meth:`check`, and ``/api/ws/stt`` calls :meth:`open_stream` when
    ``streaming`` is true. A built-in name never reaches any of them.

    A provider that identifies languages among ``stt.transcribe_languages`` may
    also set an optional ``identifies_languages = True``. Core then offers the
    mixed-language picker for it; the provider reads the candidates from
    ``stt_config.transcribe_language_identification`` itself.

    Core may call any method from any event loop. A provider must not keep a
    loop-bound client (an aiohttp or httpx session) across calls: it creates one
    per call or keeps one per loop.
    """

    name: str
    #: Whether the provider may join a live ``stt_stream`` session.
    streaming: bool

    async def check(self, stt_config: Any) -> ProviderStatus:
        """Whether this provider can run now, and why not when it cannot."""
        ...

    async def transcribe_file(self, audio_path: str, stt_config: Any) -> Optional[str]:
        """Recognise one recorded clip; ``None`` on failure, ``""`` for silence."""
        ...

    def open_stream(self, stt_config: Any) -> SttStreamSession:
        """Open a streaming session. Called only when ``streaming`` is true."""
        ...


class TtsProvider(Protocol):
    """One edition-supplied text-to-speech engine.

    ``name`` is the value ``voice_reply.provider`` stores to select it. It must
    not be a built-in name; boot refuses a context that tries.

    WIRED: ``voice_reply.synthesize_speech`` and the dashboard's
    ``/api/voice/synthesize`` call :meth:`synthesize`; ``voice_reply.is_available``
    and the voice capability list call :meth:`check`. Core redacts the text
    before :meth:`synthesize` sees it.

    The loop rule on :class:`SttProvider` applies here too.
    """

    name: str

    async def check(self, voice_config: Any) -> ProviderStatus:
        """Whether this provider can run now, and why not when it cannot."""
        ...

    def synthesize(self, text: str, voice_config: Any) -> Any:
        """Speak *text*, yielding :class:`AudioChunk` values (an async iterator)."""
        ...


class SpeechProviderRegistry(Protocol):
    """The edition's extra speech providers and its default choice.

    ADD-only: a registered provider joins the built-in set and can never shadow
    a built-in name or ``off``. A colliding or duplicate name, or a default that
    names no known provider, fails boot with ``PlatformCompositionError``.
    Providers come only from the composed context, never from config strings, so
    config can SELECT a registered provider but cannot load code.

    WIRED (through ``kiro_crew.speech_providers``): the ``stt.provider`` valid
    set and absent-key default in the config loader, and the
    ``voice_reply.provider`` valid set and absent-key default in
    ``voice_reply.resolve_configured_provider``. A stored value always wins over
    ``default_stt()`` / ``default_tts()``; the edition default applies only when
    the key is absent. An invalid stored value keeps today's fallback (STT fails
    closed to ``off``, TTS falls back to the built-in default), so an edition
    default is never reached because a value was mistyped.

    The Default registers nothing and answers ``None`` for both defaults, so a
    standalone install has the same valid sets, defaults and messages as before
    the seam existed. v1 addition (no ``CONTRACT_VERSION`` bump).
    """

    def stt_providers(self) -> Tuple[SttProvider, ...]: ...

    def tts_providers(self) -> Tuple[TtsProvider, ...]: ...

    def default_stt(self) -> Optional[str]:
        """The ``stt.provider`` an absent key resolves to; ``None`` = core default."""
        ...

    def default_tts(self) -> Optional[str]:
        """The ``voice_reply.provider`` an absent key resolves to; ``None`` = core default."""
        ...


class TelemetryProvider(Protocol):
    """Backend telemetry sink + the frontend RUM config blob.

    Public default = no-op; ``frontend_rum_config`` returns ``None`` so the
    SPA's RUM shim stays disabled.  The companion records events and returns the
    Cognito/RUM config the internal frontend host consumes.
    """

    def record_event(self, event_type: str, data: dict) -> None: ...

    def frontend_rum_config(self) -> Optional[dict]: ...

    def otlp_destinations(self, cfg: "TelemetryConfig") -> Sequence[OtlpDestination]:
        """OTLP collectors this edition sends telemetry to (WHERE, never WHAT).

        WIRED: ``metrics/provider.py::_otlp_destinations`` calls this once per
        recorder build — after the consent gate, before any reader is
        constructed — and builds one ``PeriodicExportingMetricReader`` per
        returned destination that names ``"metrics"`` in its ``signals``,
        ALONGSIDE (never instead of) the built-in local JSONL reader.  An empty
        sequence means local-only, with no network egress.

        The public default returns one destination per configured entry:
        ``telemetry.otlp_endpoint`` first when it is a non-empty string (with no
        narrowing, byte-identical to the endpoint-only exporter this replaced),
        then each enabled ``telemetry.otlp_endpoints`` entry
        (``metrics/otlp_endpoints.py``).  Nothing when neither is set.

        An edition that overrides it and should keep the operator's entries
        returns ``metrics.otlp_endpoints.config_destinations(cfg)`` first and its
        own destinations after; an override that does not call it drops them.  A
        destination returned here from code is not config, so config cannot remove
        or turn it off: that is how a build ships a collector that is on by
        default and locked.  It still obeys every gate below, and the Privacy
        panel lists it as set by the build.  An edition overrides it to reach
        its own collector — including one whose credential ROTATES during
        process lifetime, which the once-at-construction
        ``OTEL_EXPORTER_OTLP_HEADERS`` injection cannot express.

        ADD-only by construction: destinations are appended to the core's reader
        list, so this can never remove or replace the local sink, and it cannot
        relax consent, attribute sanitisation, or the histogram views.  A
        destination with an empty ``endpoint``, or one that does not name the
        signal being built, is DROPPED rather than trusted — deny-by-default, so
        a half-populated descriptor cannot start an unintended egress.

        Best-effort: the build path reads it through ``safe_context_call``
        (fallback ``()``), so a provider that raises contributes no destinations
        and telemetry keeps working local-only instead of failing.

        MUST be cheap and side-effect-free per call. It is read once per recorder
        build AND on every egress-posture read — the Privacy panel's status and
        each ``telemetry.enabled`` config write ask it so their disclosure cannot
        disagree with what gets attached. So do not acquire a token, mint a
        credential, or perform any one-shot side effect inside it: build the
        transport once and hand back the same object. v1 method addition (no
        ``CONTRACT_VERSION`` bump).
        """
        ...


class DashboardContributor(Protocol):
    """Edition-contributed dashboard routes + background services + login handler.

    Public default = no-op: contributes no routes, runs no services, and supplies
    no SSO login handler (the public ``/api/sso-login`` stays the core stub).  The
    companion mounts internal gateway routes (e.g. the secretary / taskkeeper
    services) and the real SSO PTY login handler.

    The methods are called once, during ``dashboard.server.start_dashboard``,
    against the live aiohttp application — ``contribute_routes`` BEFORE the SPA
    static catch-all and ``AppRunner.setup()`` freezes the route table, and the
    ``start_services`` / ``stop_services`` pair via ``app.on_startup`` /
    ``app.on_cleanup`` so edition services share the gateway lifecycle.
    """

    def contribute_routes(self, app: "web.Application") -> None:
        """Mount edition routes on the application (Default: no routes)."""
        ...

    async def start_services(self, app: "web.Application") -> None:
        """Start edition background services (Default: no-op)."""
        ...

    async def stop_services(self, app: "web.Application") -> None:
        """Stop edition background services on gateway shutdown (Default: no-op).

        Receives the same ``app`` handle as ``start_services`` (symmetric), so a
        companion can retrieve services/tasks it stashed on the app at startup
        without resorting to process-global state (which would break with more
        than one dashboard app in a process).
        """
        ...

    def sso_login_handler(self) -> Optional[Callable]:
        """Return the SSO-login WS handler, or ``None`` to keep the core stub."""
        ...

    def mixed_internal_api_paths(self) -> "frozenset[str]":
        """Edition paths an internal loopback caller may reach (Default: empty).

        WIRED: ``dashboard/server.py`` unions the returned set into its own
        ``_MIXED_INTERNAL_API_PATHS`` when it builds ``token_auth_middleware`` —
        for the dashboard chain AND the headless ``--slack-only`` one, so the two
        entrypoints cannot drift.

        This exists because ``contribute_routes`` is the only way an edition mounts
        a route, and the core cannot name those paths in a module-level frozenset.
        Without the seam, an edition's own MCP tool authenticating with the loopback
        ``X-Internal-Secret`` handshake is not recognized as internal at all:
        token_auth ignores the secret, falls through to cookie auth, and the tool
        answers ``Token required`` on every call.

        ADD-ONLY, and the core enforces two limits rather than trusting the
        contributor:

        * a contributed path that matches a CORE STRICT entry is DROPPED. Strict
          and mixed differ off-loopback — strict hard-denies, mixed accepts a
          validated cookie — so admitting such a path would soften a route the
          core deliberately keeps loopback-only. The overlap is checked in BOTH
          directions: a contributed ANCESTOR of a strict entry reclassifies it
          just as a child does, because the request is what gets prefix-matched.
        * the union can never remove a core entry, by construction.

        Contribute the AGENT SURFACE, never an app root: ``internal_path_matches``
        matches ``path == entry or path.startswith(entry + "/")`` and carries no
        method, so a root entry admits every route beneath it to any holder of the
        machine secret. Enumerate. Where a dynamic segment forces a prefix entry,
        re-assert at the handler for the legs no tool calls.

        The Default returns an empty set, so public behaviour is unchanged.
        v1 method addition (no ``CONTRACT_VERSION`` bump).
        """
        ...

    def on_user_message(self, app: "web.Application", message: str) -> None:
        """Observe an inbound user chat message (Default: no-op).

        WIRED: ``dashboard/chat_api/chat_send.py::api_chat`` calls this once per user
        message, just before the turn is scheduled, inside a fail-safe guard so
        an observer error never blocks the chat.  Fire-and-forget by contract:
        the observer must not block (schedule its own task if it needs to do
        work).  The public Default is a no-op; a companion uses it e.g. to
        auto-ingest document links pasted into chat.  It is an
        OBSERVER — its return value is ignored and it MUST NOT mutate the message
        or the turn.
        """
        ...

    def on_token_consumed(
        self,
        user_id: str,
        channel: str,
        session_exp: float,
        thread_ts: "str | None",
    ) -> None:
        """Observe a dashboard-session token being consumed on a verified device.

        WIRED: ``dashboard/token_auth.py::token_auth_middleware`` calls this once, right
        after it binds the token to the consuming IP, inside a fail-safe guard so
        an observer error never blocks token consumption. Fire-and-forget: the
        observer must not block.

        This is the anchor the Slack challenge-and-redirect posture needs: when a
        user opens a presigned challenge link on a real device, the token is
        consumed here, and the companion opens the bounded per-``(user, channel)``
        auth window that lets subsequent Slack traffic flow inline (see
        ``SlackEnterpriseGate.intercept_message``). ``session_exp`` is the token's
        session expiry (the window's hard ceiling); ``thread_ts`` threads the
        window to the originating Slack thread. The public Default is a no-op — no
        window is opened, matching today's OSS behavior. v1 method addition (no
        ``CONTRACT_VERSION`` bump).
        """
        ...

    def decorate_reply(self, text: str, *, channel: str, user_id: str) -> str:
        """Transform an outbound Slack reply just before it is sent (Default: identity).

        WIRED: ``slack/handler_runtime/delivery.py`` calls this on the finalized reply text on the
        outbound path, inside a fail-safe guard (a raising decorator falls back to
        the undecorated text). The public Default returns ``text`` unchanged.

        The challenge posture uses it to append a "<5 min left" expiry footer when
        the auth window for ``(user_id, channel)`` is close to lapsing, and to
        refresh the window's activity clock on each reply. It is a PURE
        transform — it returns the (possibly-decorated) text and must not block on
        network I/O. v1 method addition (no ``CONTRACT_VERSION`` bump).
        """
        ...


class JailProvider(Protocol):
    """Process-isolation (jail) lifecycle for agent-bearing commands.

    Public default = no-op: ``available()`` is ``False`` and
    ``maybe_reexec_into_jail`` returns ``None`` (no isolation; the command runs
    in-process exactly as today).  The companion supplies the internal jail
    orchestration that re-execs the process into an isolated namespace before any
    agent work starts, mirroring the ``TunnelProvider`` lifecycle shape.

    **Re-entry contract.** Before a successful re-exec the core gate sets the
    ``KIROCREW_JAILED`` env marker; the jailed CHILD re-runs the gate, sees the
    marker, and short-circuits, so the backend is NOT asked to re-jail an
    already-jailed process.  A companion that spawns the child with a fresh
    environment MUST set this marker to ANY non-empty value (re-entry is detected
    by PRESENCE, not truthiness — ``"1"``, ``"jailed"``, a namespace id all work)
    so the child does not deadlock: under ``mode == "on"`` the child would
    otherwise probe again, get an "already jailed" ``None``, and be refused by the
    on-mode floor.
    """

    def available(self) -> bool:
        """Whether a working jail backend is present on this host.

        SHOULD NOT raise — push any probing ``try/except`` into the adapter and
        return ``False`` on a probe failure.  (The core gate is defensive: it
        treats a raised probe as "availability unknown" and fails closed under
        ``mode == "on"``, but a clean ``bool`` keeps the two consumers — the gate
        and ``doctor`` — in agreement.)
        """
        ...

    def status_detail(self) -> str:
        """One-line human status for ``doctor`` output."""
        ...

    def maybe_reexec_into_jail(self, argv: List[str], mode: str) -> Optional[int]:
        """Re-exec the process into the jail; return the child exit code.

        Returns ``None`` when no re-exec happens (disabled, unavailable, or
        already jailed) — the caller then falls through and runs in-process.
        A non-``None`` int is the jailed child's exit status; the caller exits
        with it.  The Default always returns ``None``.

        **mode contract:** ``"off"`` / already-jailed / disabled → ``None``.
        ``"auto"`` → re-exec if a backend is available, else degrade to ``None``.
        ``"on"`` → the operator demanded isolation: the implementation MUST either
        re-exec (returning the child rc) or fail closed (raise / exit non-zero);
        it MUST NOT return ``None`` when isolation could not be established.  The
        core gate additionally treats a ``None`` return (or a swallowed error)
        under ``mode == "on"`` as fail-closed, so a defensive backend cannot
        accidentally downgrade an on-mode host to an un-jailed run.  (The
        "already jailed → ``None``" case does not deadlock the child because the
        gate's ``KIROCREW_JAILED`` re-entry guard short-circuits before this is
        called a second time — see the class docstring's re-entry contract.)
        """
        ...
