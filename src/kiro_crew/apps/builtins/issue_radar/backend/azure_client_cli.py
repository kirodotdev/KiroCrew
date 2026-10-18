"""The ``az`` chokepoints every Azure DevOps call goes through.

The provider-shared exception aliases, the call timeouts, the per-endpoint API
versions and paging limits, the project-URL helpers bound from ``azure_transport``,
the hardened ``az`` spawn ``_az_run`` with its minimal child environment, SEL audit
and launcher-argument defence, the ``az devops invoke`` REST runner and its paging,
and the payload helpers bound from ``azure_normalization``. Callers and tests reach
every name here through :mod:`kiro_crew.apps.builtins.issue_radar.backend.azure_client`,
so a patch of ``_az_run`` or ``_az_invoke`` there reaches every caller.
"""

from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import quote

from kiro_crew.apps.registry import minimal_env
from kiro_crew.github_runner import apply_session_bus_address
from kiro_crew.sel import sel

from . import azure_normalization as _normalization
from . import azure_transport as _transport
from .errors import (
    ProviderCliError,
    ProviderInvalidInputError,
    ProviderPermissionError,
    ProviderSetupError,
    sanitize_cli_stderr,
)

# Historical aliases, mirroring github_client and gitlab_client so
# provider-agnostic callers can use any of the three modules interchangeably.
# ALIASES, never subclasses: ``except GhCliError`` in routes.py must catch an
# Azure failure, otherwise every one becomes a 500 instead of the 502/403 the
# route intends (see errors.py).
GhCliError = ProviderCliError
GhSetupError = ProviderSetupError
GhPermissionError = ProviderPermissionError
GhInvalidInputError = ProviderInvalidInputError

AZ_TIMEOUT_SEC = 25.0
# The work-item and pull-request listings span several calls each (WIQL + batched
# hydrate; PR pages + per-PR enrichment), so the paginating reads get a much
# larger budget than the single-shot ones. The result is cached, so the cost is
# paid once per refresh rather than per view.
AZ_PAGINATE_TIMEOUT_SEC = 150.0

# Mirrors the other two clients' constants so provider-agnostic routes can read
# any client's limits without branching.
CONTRIB_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 3650
PR_SEARCH_MAX = 300

# The one supported host. Azure DevOps Server (on-premises) is out of scope.
AZURE_HOST = _transport.AZURE_HOST
_LEGACY_HOST_SUFFIX = _transport._LEGACY_HOST_SUFFIX

# ``--api-version`` is MANDATORY on every ``az devops invoke`` call and differs
# per endpoint: the CLI's own default is 5.0, which predates most of what this
# module reads (and answers 400 for the rest). There is deliberately no single
# global version -- several of these resources exist only as previews, and
# sending a GA version to them fails, while sending a preview version to a GA
# resource pins us to a shape Microsoft may change.
_API_GIT = "7.1"
_API_WIT = "7.1"
_API_BUILD = "7.1"
_API_CORE = "7.1"
# Preview-only resources. The suffix is part of the version string, not a flag.
#
# These are spelled ``{major}.{minor}-preview`` WITHOUT the ``.{resource-version}``
# revision. The ``azure-devops`` CLI extension parses ``--api-version`` by deleting
# the substring ``-preview`` and calling ``float()`` on what is left, before any
# request is sent, so ``7.1-preview.4`` becomes ``7.1.4`` and every call fails
# with "could not convert string to float". The unrevisioned form is a valid
# REST API version: the service answers it with the newest preview revision of
# the 7.1 resource, which for each of these is the revision the 7.1 reference
# documents (comments .4, the rest .1). ``_PREVIEW_API_VERSIONS`` is what the
# test suite holds to the extension's parse.
_API_WIT_COMMENTS = "7.1-preview"
_API_WIT_TAGS = "7.1-preview"
_API_WIT_STATES = "7.1-preview"
_API_POLICY = "7.1-preview"
_API_IDENTITY = "7.1-preview"
_PREVIEW_API_VERSIONS = (
    _API_WIT_COMMENTS,
    _API_WIT_TAGS,
    _API_WIT_STATES,
    _API_POLICY,
    _API_IDENTITY,
)

# Azure list responses come back as ``{"count": n, "value": [...]}`` and are
# paged with ``$top``/``$skip`` rather than a page number.
_PAGE_SIZE = _transport._PAGE_SIZE
# Hard cap on pages walked by one paginated read, so a pathological project
# cannot make a single request loop indefinitely.
_MAX_PAGES = _transport._MAX_PAGES
# Work-item comments are their own paged resource, keyed by a continuation token
# rather than $skip, and Azure caps this page at 200.
_COMMENT_PAGE_SIZE = 200
# Ceiling on ids one WIQL query returns. WIQL's own default cap is far higher;
# this bounds the hydrate that follows, which is the expensive half.
_WIQL_TOP = 2000
# Documented per-call id cap on the work-item batch endpoint. Exceeding it is a
# 400, so ids are chunked at this size and the results stitched.
_BATCH_MAX_IDS = 200
# Pull requests whose card enrichment (policy evaluations) is fetched per list
# refresh. Rows past this keep ``checks_counts: None``, which keeps them OUT of
# the on-disk cache via ``enrichment_complete`` rather than caching a zeroed
# check state as authoritative.
_ENRICH_MAX_PULLS = 60
# Builds scanned when resolving "the runs for commit X". The builds endpoint has
# no commit filter, so the newest N are read and matched on ``sourceVersion``.
_BUILD_SCAN_TOP = 200
# Pull requests scanned when resolving "the PR whose head is commit X" -- needed
# because Azure keys policy evaluations by PR while the shared signature keys
# checks by sha.
_PR_LOOKUP_TOP = 200

# Azure has no colour on a work-item tag, so one is synthesized. Same neutral
# default the other two clients fall back to, so a tag renders like a label.
_SYNTHETIC_LABEL_COLOR = _normalization._SYNTHETIC_LABEL_COLOR

# Kept importable from the historical facade for direct tests and diagnostics.
_SEGMENT_RE = _transport._SEGMENT_RE
_RESERVED_SEGMENTS = _transport._RESERVED_SEGMENTS
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_GUID_RE = _normalization._GUID_RE
# A login as the person filters accept it: an Azure unique name is usually an
# email/UPN, so "@" and "+" are legal where they would not be on the other
# providers.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@' -]{0,254}$")


def parse_azure_repo_url(link: str) -> tuple[str, str]:
    """Parse the provider key from a supported Azure DevOps URL."""
    return _transport.parse_azure_repo_url(link)


_bad_segment = _transport._bad_segment
_URL_PATH_SEPARATOR = _transport._URL_PATH_SEPARATOR
_url_path_segments = _transport._url_path_segments
_split_owner = _transport._split_owner
_check_repo = _transport._check_repo


def _org_url(org: str) -> str:
    """Return the pinned organization URL through the current quote binding."""
    return _transport._org_url(org, quote_value=quote)


# ── az spawn hardening ───────────────────────────────────────────────────────
#
# Mirrors gitlab_client's model exactly. ``az`` needs the user's OWN
# authenticated session and cannot be sandbox-routed (the sandbox would hide
# ~/.azure and break auth). As defense in depth every spawn goes through
# ``_az_run``, which (1) resolves ``az`` through the shared provider policy --
# accepting the user's own install but refusing one owned by another user, a
# world-writable one, or one inside the agent-writable project tree -- and (2)
# hands the child a MINIMAL environment (PATH/HOME/XDG plus az's own auth and
# network vars) instead of the gateway's full env, so unrelated secrets
# (AWS/Slack/SSH) can never reach a substituted or compromised az.
_AZ_ENV_PASSTHROUGH = _transport._AZ_ENV_PASSTHROUGH

_az_bin_cache: str | None = None


def _az_bin() -> str:
    """Absolute path to an acceptable ``az``, resolved once and cached.

    Resolution and validation are shared with the Sidebar PR panel
    (``source_providers.provider_executable_candidates`` +
    ``_validate_provider_executable``), exactly as ``gitlab_client._glab_bin``
    does for ``glab``: the well-known install dirs first, then the ambient
    ``PATH``, accepting the user's own install (Homebrew, apt, pipx) while
    refusing a binary owned by another user, a world-writable one, or one inside
    the agent-writable project tree.

    Set ``KIROCREW_ISSUE_RADAR_AZ`` to an absolute path to override (still
    validated), or ``KIROCREW_PROVIDER_BIN_STRICT=1`` to require a system-dir
    binary. ``az`` carries the same well-known-directory entries as ``gh`` and
    ``glab`` (``github_runner.PROVIDER_EXECUTABLE_CANDIDATES``), so strict mode
    resolves a packaged ``/usr/bin/az`` exactly as it does for the other two; what
    it hides is a user-owned install on ``PATH`` alone -- Homebrew, pipx, mise --
    which is what the override is for.

    Windows resolves through the same two helpers, and the resolution is the
    same shape ``_glab_bin`` uses. ``provider_executable_candidates`` delegates
    to :func:`shutil.which`, which applies ``PATHEXT``, so a bare ``az`` matches
    the ``az.cmd`` launcher the Azure CLI installer writes; and
    ``_validate_provider_executable`` answers the ownership and writability
    questions from the object's ACL there instead of from ``st_uid`` and the mode
    bits. ``github_runner.WINDOWS_PROVIDER_EXECUTABLE_SUBDIRS`` carries no ``az``
    entry, so a Windows install is reached through the ambient ``PATH`` that the
    installer sets -- which strict mode does not consult, making the override the
    only strict-mode route on that platform.
    """
    global _az_bin_cache
    if _az_bin_cache:
        return _az_bin_cache

    # Deferred, matching gitlab_client._glab_bin: importing the dashboard handler
    # package pulls in the whole dashboard (~750 modules), and this module is also
    # loaded by the MCP server and the CLI, where none of it is wanted. Not a
    # circular import -- source_providers reaches nothing under issue_radar -- so
    # the only cost of hoisting it would be that import weight, paid by every
    # consumer whether or not it ever resolves a binary.
    from kiro_crew.dashboard.handlers.source_providers import (
        _validate_provider_executable,
        provider_executable_candidates,
    )

    override = os.environ.get("KIROCREW_ISSUE_RADAR_AZ")
    if override:
        try:
            validated = _validate_provider_executable(override)
            _az_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            raise ProviderSetupError(
                f"KIROCREW_ISSUE_RADAR_AZ={override!r} failed validation: {exc}",
                reason="not_installed",
            ) from exc

    last_error = ""
    for cand in provider_executable_candidates("az"):
        if not os.path.isfile(cand):
            continue
        try:
            validated = _validate_provider_executable(cand)
            _az_bin_cache = validated
            return validated
        except (ValueError, OSError) as exc:
            last_error = str(exc)
            continue  # untrusted provenance -- skip

    detail = f" (last check: {last_error})" if last_error else ""
    raise ProviderSetupError(
        "the `az` CLI was not found on this host"
        f"{detail} -- install the Azure CLI, add the Azure DevOps extension "
        "(`az extension add --name azure-devops`) and run `az login`, or set "
        "KIROCREW_ISSUE_RADAR_AZ to an absolute az path",
        reason="not_installed",
    )


_resolve_host = _transport._resolve_host


def _az_env(host: str) -> dict[str, str]:
    """Build the minimal ``az`` environment through the transport helper."""
    env = _transport._az_env(
        host,
        source_env=os.environ,
        passthrough_keys=_AZ_ENV_PASSTHROUGH,
        minimal_env=minimal_env,
    )
    # az's encrypted token cache reaches the keyring through libsecret; hand it
    # an explicit bus or an inert address (see github_runner.session_bus_address).
    return apply_session_bus_address(env)


def _audit(op: str, target: str, outcome: str, *, error: str = "") -> None:
    """SEL event for an az spawn.

    The ``invoked`` record is written with ``critical=True``, which is what makes
    it a gate rather than a hope: the default path ENQUEUES the event and returns
    successfully even when the log cannot be written, so a pre-spawn audit without
    it would still let a provider mutation run unrecorded. ``critical`` writes
    synchronously and re-raises the filesystem failure, so :func:`_az_run` can
    refuse.

    The post-execution ``ok`` / failure records stay on the default enqueue path.
    By then the command has already run, so raising would replace the caller's real
    error with a logging one and change nothing about what happened.
    """
    sel().log_api_access(
        caller="core:issue-radar",
        operation=f"issue_radar.{op}",
        outcome=outcome,
        source="builtin-app",
        resources=target[:200],
        error=error[:200] if error else "",
        critical=outcome == "invoked",
    )


# Launcher suffixes the Windows command processor executes rather than the
# kernel: ``CreateProcess`` runs a ``.cmd``/``.bat`` through ``%COMSPEC%``, which
# re-parses the command line, so a separator or redirection character inside an
# ARGUMENT is read as command syntax instead of as data. The Azure CLI ships
# exactly such a launcher (``az.cmd``).
_REPARSED_LAUNCHER_SUFFIXES = (".cmd", ".bat")
# Characters MEASURED to be acted on by a real ``.cmd`` launcher on Windows (see
# ``test/test_azure_launcher_reparse.py``): ``&``, ``|`` and ``>`` truncate the argument
# and run or redirect what follows, ``^`` is eaten as an escape so ``a^b`` arrives as
# ``ab``, and ``!`` expands an environment value when the host enables delayed
# expansion (``HKCU\Software\Microsoft\Command Processor\DelayedExpansion``; re-measured
# under ``cmd /V:ON``, where ``a!PATH!b`` arrives as the expanded ``PATH`` split across
# ~70 argv elements). ``"`` measured intact but its parsing depends on surrounding
# quoting this module cannot see.
#
# This tuple is PROVENANCE, not the enforcement path. Enforcement is
# :data:`_ARGUMENT_ALLOWED_CHARACTERS`, which refuses everything these characters are
# examples of. A test asserts every character here is outside the allowlist, so the
# measurements act as a regression check on the allowlist rather than as a list anyone
# has to keep extending.
_MEASURED_HOSTILE_CHARACTERS = ('"', "&", "|", "<", ">", "^", "!", "\r", "\n")

#: The characters a value reaching an ``az`` argv can legitimately contain — the UNION
#: of what the four shape checks upstream of every argv can emit (``_SEGMENT_RE``,
#: ``_LOGIN_RE``, ``_SHA_RE``, ``_GUID_RE``), plus what :func:`_org_url` adds.
#:
#: ``:`` and ``/`` are two of six characters the COMPOSITION contributes, and the
#: universe here is the composed argv ELEMENT rather than the value inside it — naming
#: that universe wrongly is what let an earlier form of this allowlist refuse every real
#: call. Derived from what :func:`azure_transport.invoke` emits, captured by DRIVING it:
#:
#: * ``:`` ``/`` from the constant ``https://dev.azure.com/`` prefix in ``_org_url``
#: * ``=`` from ``f"{key}={value}"`` for ``--route-parameters`` / ``--query-parameters``
#: * ``$`` from OData keys this provider writes, e.g. ``$top``
#: * ``%`` from ``quote(org, safe="")``, further constrained to the octet shape by
#:   :func:`_reject_percent_that_is_not_encoding`
#:
#: The ``--in-file`` path is deliberately NOT covered here: its spelling belongs to the
#: HOST rather than to this code, so it is registered in
#: :data:`azure_transport._GENERATED_BODY_PATHS` and checked for genuinely hostile
#: characters instead. A character allowlist cannot describe every host's temp path — a
#: Windows 8.3 short name contributes ``~`` (``C:\Users\RUNNER~1\...`` on a CI runner),
#: and a ``TMPDIR`` under ``Program Files (x86)`` would contribute parentheses — and
#: chasing those spellings is the same open-ended chase this allowlist exists to end.
#:
#: None of those characters is reachable from caller data: every shape check refuses all
#: of them, so each can only enter from a literal this module wrote. A test asserts exactly
#: that, which is what keeps admitting them from widening what an attacker controls.
#:
#: An allowlist rather than a denylist of command-processor metacharacters, because a
#: denylist is structurally fail-OPEN: every cmd.exe-significant spelling not yet
#: measured passes, and ``!`` demonstrated that the measurement itself flips with a host
#: registry value. Derived empirically, not curated — ``;``, ``,``, tab, ``(`` and ``)``
#: are all cmd-significant and appear in no legitimate argv element, and the allowlist
#: refuses them without naming them or requiring a measurement of each.
#:
#: Widening a shape check OR an argv builder upstream therefore fails
#: ``test_the_allowlist_covers_a_real_composed_argv`` rather than silently opening a
#: hole here.
_ARGUMENT_ALLOWED_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789" " '+-.@_%:/=$"
)

#: A percent that opens a percent-ENCODED octet: ``%`` then exactly two hex digits.
#: ``quote(value, safe="")`` emits nothing else, and for a value matching
#: ``_SEGMENT_RE`` -- alphanumerics, space, dot, underscore, hyphen -- the only sequence
#: it can emit at all is ``%20``.
_PERCENT_ENCODED_OCTET = re.compile(r"%[0-9A-Fa-f]{2}")


def _reject_percent_that_is_not_encoding(value: str) -> str | None:
    """The offending percent in *value*, or ``None`` when every one is an encoded octet.

    A command processor EXPANDS ``%NAME%`` when ``NAME`` is a defined environment
    variable, which was measured putting the value of ``PATH`` into a child's argv and
    splitting one argument into many. So a percent cannot simply be allowed.

    Nor can it simply be refused. ``_SEGMENT_RE`` admits a SPACE, so an organization or
    project whose name carries one is legitimate here, and ``_org_url`` percent-encodes
    it to ``%20`` on every call -- refusing every percent would break a supported name
    rather than an attack.

    The rule that separates them is the encoding contract itself: every percent must
    open an encoded octet. ``%20`` passes; ``%PATH%`` does not, because ``PA`` is not two
    hex digits. A trailing percent fails for the same reason, which is what keeps a
    variable reference from being assembled out of two otherwise-valid octets.
    """
    index = 0
    while True:
        index = value.find("%", index)
        if index < 0:
            return None
        if not _PERCENT_ENCODED_OCTET.match(value, index):
            return value[index : index + 8]
        index += 3


def _reject_reparsed_launcher_args(az: str, args: list[str]) -> None:
    """Refuse an argument a command-processor launcher would read as syntax.

    Every value this module puts in an argv is already shape-checked
    (``_SEGMENT_RE``, ``_LOGIN_RE``, ``_SHA_RE``, ``_GUID_RE``) and a request body
    travels in a file rather than an argument, so this is the fail-closed
    backstop for a value that reaches argv without passing one of those: it
    refuses instead of quoting, because quoting for two parsers at once is what
    the argv list exists to avoid.

    Fail-closed is meant literally, which is why the check is an ALLOWLIST
    (:data:`_ARGUMENT_ALLOWED_CHARACTERS`) rather than a list of metacharacters. A
    denylist would pass every cmd.exe spelling nobody has measured yet, and ``!`` showed
    that whether a spelling is hostile can depend on a host registry value rather than
    on the launcher — so enumerating hostile characters is a chase with no end. The
    allowlist instead names what a legitimate value can contain, and refuses the
    complement without having to know why each member of it is dangerous.
    """
    if not az.lower().endswith(_REPARSED_LAUNCHER_SUFFIXES):
        return
    for value in args:
        if value in _transport._GENERATED_BODY_PATHS:
            # A path this module created. Its spelling belongs to the HOST, not to this
            # code, so the allowlist cannot describe it -- check it for characters that
            # genuinely break a command line instead.
            hostile = sorted({c for c in value if c in _MEASURED_HOSTILE_CHARACTERS})
            if hostile:
                raise ProviderInvalidInputError(
                    "refusing to run `az` through a command-processor launcher with a "
                    f"request-body path containing {', '.join(repr(c) for c in hostile)}"
                    ": the launcher re-parses its command line, so the path would be "
                    "read as command syntax. Point TMPDIR at a directory whose name "
                    "carries no command-processor metacharacter.",
                    values=[value[:80]],
                )
        else:
            forbidden = sorted({char for char in value if char not in _ARGUMENT_ALLOWED_CHARACTERS})
            if forbidden:
                raise ProviderInvalidInputError(
                    "refusing to run `az` through a command-processor launcher with an "
                    f"argument containing {', '.join(repr(c) for c in forbidden)}: the "
                    "launcher re-parses its command line, and no value this provider "
                    "builds can contain that character, so it would be read as command "
                    "syntax",
                    values=[value[:80]],
                )
        stray = _reject_percent_that_is_not_encoding(value)
        if stray is not None:
            raise ProviderInvalidInputError(
                "refusing to run `az` through a command-processor launcher with an "
                f"argument whose {stray!r} is not a percent-encoded octet: the launcher "
                "expands a variable reference, substituting an environment value into "
                "the command line",
                values=[value[:80]],
            )


def _az_run(argv: list[str], *, host: str, timeout: float) -> subprocess.CompletedProcess:
    """Single spawn chokepoint for every ``az`` call -- replaces argv[0] with the
    validated az and passes the minimal env for the resolved host.

    Order matters: the host is re-resolved, the binary is re-validated, the
    arguments are checked against the resolved launcher's parser, and the
    ``invoked`` audit is written, all BEFORE anything executes. A failure at any of
    those points means no spawn happens.
    """
    resolved_host = _resolve_host(host)
    az = _az_bin()
    _reject_reparsed_launcher_args(az, argv[1:])
    # The body-path registry exists only for the check above, so retire this call's
    # entries as soon as it has run rather than letting the set grow for the process's
    # lifetime. An entry that is gone falls through to the strict allowlist, which
    # refuses -- so this is safe in the fail-closed direction.
    for element in argv[1:]:
        _transport._GENERATED_BODY_PATHS.discard(element)
    operation = f"az {' '.join(argv[1:3])}"  # e.g. "az devops invoke" (bounded)
    try:
        _audit("az_run", operation, "invoked")
    except Exception as exc:
        # Refuse rather than run unrecorded. Surfaced as a provider error so the
        # route answers 502 instead of an opaque 500.
        raise ProviderCliError(
            "refusing to run an az command because the security event log could "
            f"not record it: {exc}"
        ) from exc
    try:
        proc = subprocess.run(
            [az, *argv[1:]],
            capture_output=True,
            text=True,
            # `az devops invoke` answers with work-item JSON -- titles, descriptions
            # and comments written by people -- so non-ASCII is the normal case, not
            # an edge one. `text=True` alone decodes it with
            # `locale.getpreferredencoding(False)`, which on a Windows console code
            # page is cp950 / cp932 / cp1252. A `UnicodeDecodeError` is neither
            # `FileNotFoundError` nor `TimeoutExpired`, so it would escape both
            # handlers below and this chokepoint's own error contract with it; a
            # lenient code page is quieter and worse, mojibaking every title that
            # reaches the board. az writes UTF-8, so name it.
            encoding="utf-8",
            timeout=timeout,
            check=False,
            env=_az_env(resolved_host),
        )
    except FileNotFoundError as exc:  # pragma: no cover -- _az_bin guards first
        _audit("az_run", operation, "failure", error="az not found")
        raise ProviderSetupError(
            "the `az` CLI is not installed on this host", reason="not_installed"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        _audit("az_run", operation, "failure", error=f"timeout after {timeout}s")
        raise ProviderCliError(f"`az` timed out after {timeout}s") from exc
    if proc.returncode != 0:
        _audit("az_run", operation, "failure", error=f"exit {proc.returncode}")
    else:
        _audit("az_run", operation, "ok")
    return proc


# Markers az prints when it has no usable credential for Azure DevOps, matched
# case-insensitively against the stderr tail so the connect dialog can offer a
# login instruction instead of an opaque exit code. TF400813 is Azure DevOps's
# own "the user is not authorized" code, which it also returns for an expired or
# absent session.
_AZ_AUTH_MARKERS = _transport._AZ_AUTH_MARKERS

# Markers that mean the CLI or its azure-devops EXTENSION is missing. A missing
# extension is reported as ``not_installed`` too: the fix is the same class of
# user action (install something), and the connect dialog names it.
_AZ_MISSING_MARKERS = _transport._AZ_MISSING_MARKERS


_raise_if_setup_failure = _transport._raise_if_setup_failure


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    """Return sanitized stderr through the current facade binding."""
    return _transport._stderr_tail(proc, sanitize=sanitize_cli_stderr)


_is_forbidden = _transport._is_forbidden
_body_file = _transport._body_file


def _az_invoke(
    *,
    org: str,
    area: str,
    resource: str,
    host: str,
    timeout: float = AZ_TIMEOUT_SEC,
    route: dict[str, object] | None = None,
    query: dict[str, object] | None = None,
    method: str = "GET",
    body: object | None = None,
    api_version: str,
    media_type: str = "",
) -> object:
    """Invoke through the extracted transport using this facade's patch seams."""
    return _transport.invoke(
        run=_az_run,
        body_file=_body_file,
        org_url=_org_url,
        raise_if_setup_failure=_raise_if_setup_failure,
        stderr_tail=_stderr_tail,
        is_forbidden=_is_forbidden,
        org=org,
        area=area,
        resource=resource,
        host=host,
        timeout=timeout,
        route=route,
        query=query,
        method=method,
        body=body,
        api_version=api_version,
        media_type=media_type,
    )


def _az_invoke_paged(
    *,
    org: str,
    area: str,
    resource: str,
    host: str,
    timeout: float,
    route: dict[str, object] | None = None,
    query: dict[str, object] | None = None,
    api_version: str,
    limit: int = 0,
) -> list[dict]:
    """Page through the extracted transport using the current invoke binding."""
    return _transport.invoke_paged(
        invoke_one=_az_invoke,
        values=_values,
        page_size=_PAGE_SIZE,
        max_pages=_MAX_PAGES,
        org=org,
        area=area,
        resource=resource,
        host=host,
        timeout=timeout,
        route=route,
        query=query,
        api_version=api_version,
        limit=limit,
    )


_obj = _normalization._obj
_values = _normalization._values
_identity_login = _normalization._identity_login
_identity_logins = _normalization._identity_logins
_identity_id = _normalization._identity_id
_tag_names = _normalization._tag_names
_tags_field = _normalization._tags_field
_check_label = _normalization._check_label
_shape_labels = _normalization._shape_labels
_work_item_url = _normalization._work_item_url
_field = _normalization._field


_norm_issue = _normalization._norm_issue
_norm_issue_detail = _normalization._norm_issue_detail
