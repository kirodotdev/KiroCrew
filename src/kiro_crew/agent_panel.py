"""Each crew's own webview: a dashboard the crew itself decides the contents of.

A crew that runs long — a conductor holding a fleet of workers, a research loop,
anything that works unattended — accumulates state that answers the only
questions an operator actually has: how many workers am I holding, which one is
stuck, what is my next step, is anything waiting on a decision. Its drawer today
shows activity counts, a path list, wake sources and config, none of which
answer those. This module is the store behind a webview in that drawer which
does.

One webview per crew, and the crew fills it
-------------------------------------------
The view is free-form HTML and it is configurable: each crew gets a TEMPLATE,
authored once, and at runtime the crew publishes only the DATA that fills it.
A crew whose name matches an installed template gets that template; a crew with
no template of its own falls back to a generic one that renders any data object.

The split that makes free-form HTML safe
----------------------------------------
Only the data comes from the crew.

The template is HTML a human wrote and reviewed — versioned in the repository,
or dropped on disk by the operator — free to lay out whatever it likes. The
crew never writes it: the template directory is fenced from agent file tools
(see ``security._CREW_SECRET_LEAVES``) AND bind-masked from a sandboxed shell
(``sandbox._CREW_HIDDEN_LEAVES`` for the records, ``_CREW_READONLY_LEAVES`` for
the templates), so the only way a crew influences its webview is by publishing a
data object through the MCP tool.

The published DATA is fenced the same way, which is easy to get backwards: the
record is not "the crew's own file". It carries the ownership claim the store
reads back to refuse a colliding write, and it is the REDACTED copy of untrusted
text — so a crew that could write it directly would forge another crew's panel
past both the ownership check and the redactors in one step.

That matters because a conductor reads issue bodies, pull-request descriptions
and review comments on a loop with nobody at the keyboard, so a path exists from
a hostile issue body to whatever renders this. Keeping layout out of the crew's
hands means the untrusted half is *data*, and data can be escaped at a single
boundary -- :func:`compose` below -- rather than trusted to be well-formed
markup. The sandboxed frame the drawer renders into stays as defence in depth,
not as the only defence.

Where a panel lives
-------------------
In a gateway-only records directory, ``crew-panels/<slug>.json`` under the data
home -- NOT in the crew's own member space, which is where an earlier draft of
this module put it. A record is an ownership authority and the redacted copy of
untrusted text, so all three ways of writing it are closed on that one name: the
publishing MCP tool never touches the file (it POSTs to the gateway), the agent's
file tools are fenced by ``security._CREW_SECRET_LEAVES``, and a sandboxed shell
is bind-masked by ``sandbox._CREW_HIDDEN_LEAVES``. ``members/`` could give none of
those, because a crew owning its own published data is exactly what makes that
tree writable.

Published data is therefore FENCED, not readable state: only the gateway reads or
writes it, and the drawer reaches it through a cookie-authed route that checks the
record's stored owner. See :func:`_real_dir_under_data_home` for the invariant all
of that rests on -- one name, one inode, every disposition attached to it.

Deliberately generic
--------------------
Nothing here knows what a "worker" or a "pull request" is. The store holds an
opaque JSON object and the id of a template to render it with; the conductor is
the first consumer, not the schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import release_lock, try_acquire_lock
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

SCHEMA_VERSION = 1

#: Operator-authored template overrides. A DIRECTORY OF ITS OWN, outside every
#: crew's member space, for two reasons: a template is reusable across crews, and
#: it is the one part of a panel that must never be agent-writable -- so it is
#: the thing behind the fence, while the published data (which the crew owns
#: anyway) is not.
TEMPLATES_DIRNAME = "panel-templates"

_LOCK_SUFFIX = ".lock"

#: A template id names a file, so it is validated rather than sanitised: a
#: lenient fold would let ``..%2f`` shaped input pick a path outside the
#: template roots. Lowercase, digit, single dashes, no dots -- nothing that can
#: traverse or hide an extension.
TEMPLATE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

DEFAULT_TEMPLATE_ID = "default"

#: The marker a template must carry, replaced by the data island in
#: :func:`compose`. A template without it renders a crew's data nowhere, which is
#: a template bug worth failing loudly on rather than showing a blank webview the
#: operator cannot explain.
DATA_MARKER = "<!--kirocrew:panel-data-->"

_DATA_ELEMENT_ID = "kirocrew-panel-data"

_MAX_TITLE = 200
#: Data, not layout. A panel carries a few dozen rows of state, so this is two
#: orders of magnitude under the 4 MiB the sandbox document channel accepts --
#: the cap exists to keep one wedged crew from filling the data home, not to
#: leave room for markup.
_MAX_DATA_BYTES = 64 * 1024
#: Bounds the recursion in :func:`_check_depth` and, with the byte cap, bounds
#: the work any renderer has to do. Eight is deeper than a stat row, a table of
#: rows, or a list of nested groups needs.
_MAX_DATA_DEPTH = 8
_MAX_RECORD_BYTES = 128 * 1024

_LOCK_TIMEOUT_SECS = 5.0
_LOCK_POLL_SECS = 0.05


class PanelError(ValueError):
    """A publish was refused. ``code`` is the machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def shipped_templates_dir() -> Path:
    """The in-repository template directory that ships with the package."""
    return Path(__file__).resolve().parent / "agent_panel_templates"


def override_templates_dir() -> Path:
    """Where an operator drops a template that wins over the shipped one.

    Resolved against the live data home on every call, never captured at import:
    a pod and a test both move the data home after this module is imported, and a
    captured root would read the operator's real home from inside a test.
    """
    return _real_dir_under_data_home(TEMPLATES_DIRNAME)


#: Mirrors ``members.MEMBERS_DIR_NAME`` and ``members._SLUG_RE`` rather than
#: importing them, and a test pins the two pairs equal so they cannot drift.
#:
#: Mirrored because ``members`` reaches ``artifacts -> hooks -> webhooks ->
#: validation``, which imports ``artifacts`` back. That cycle is LATENT while
#: something else has already imported the chain -- true under the test suite and
#: true during a gateway boot -- and raises ``ImportError`` the moment this store
#: is what triggers the chain, which is any script or tool that reaches a crew's
#: panel first. Deferring the import to call time only moved the failure from
#: import to first use; not importing it is what actually fixes it.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

#: Mirrors ``artifacts._SLUG_NORMALIZE_RE`` -- every run of slug-unsafe
#: characters collapses to one hyphen. Pinned by the same anti-drift test.
_SLUG_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

#: Where the records live: ``<data home>/crew-panels/<slug>.json``.
#:
#: A DEDICATED top-level leaf, with all three write paths closed on it at once
#: (``test_agent_panel_store`` pins each one):
#:
#:   1. The MCP tool never touches the file. ``mcp_panel`` does not import this
#:      module at all -- it ``_post``s to ``/api/agent-panel/publish`` -- so the
#:      only writer in the tree is the gateway process.
#:   2. The agent's file tools are fenced by ``security._CREW_SECRET_LEAVES``.
#:   3. A sandboxed shell is bind-masked by ``sandbox._CREW_HIDDEN_LEAVES``, which
#:      is what ``trust/`` could NOT give: ``trust`` is a declared read-write
#:      exception (``verify_session_pid`` reads ``trust/sel_hmac.key`` in-sandbox
#:      and the in-sandbox MCP servers append to the audit log), so a record under
#:      it stayed writable by any command that built the path at runtime -- with no
#:      literal path for command matching to catch.
#:
#: Not under ``trust/`` for that reason, even though the record is an ownership
#: authority like the DM binding: a hidden subtree inside a deliberately-writable
#: one would leave two contradictory-looking dispositions for one directory. This
#: leaf instead joins the group in ``_CREW_HIDDEN_LEAVES`` whose stated model it
#: matches exactly -- cross-session state whose every legitimate reader goes
#: through the gateway, alongside ``ledger``, ``routing`` and ``webhooks``.
_PANELS_DIRNAME = "crew-panels"
_PANEL_SUFFIX = ".json"


class CrewSlugError(ValueError):
    """Raised when a crew slug is unusable as a directory name."""


def _real_dir_under_data_home(leaf: str) -> Path:
    """The data home's *leaf* directory, refusing an ALIASED name.

    THE INVARIANT, stated once for both protected leaves, because four separate
    findings have now probed the same fence and each was the same divergence:
    **the bytes the gateway reads must be reachable by exactly one name, and every
    disposition must attach to that name.** A fence, a mask and a precreate are all
    assertions about a PATH, so any gap between the path named and the path used
    voids them, whatever opens the gap:

    * ``members/<slug>/`` -- the name sat in a tree with no fence at all;
    * ``trust/`` -- the name sat in a tree with a standing sandbox write exception;
    * an ABSENT directory -- the disposition named a path that did not exist, so the
      Linux bind-mask silently attached to nothing (``_CREW_PRECREATE_*``);
    * a SYMLINKED directory -- this. ``.resolve()`` follows links, so the store wrote
      through to the target while the mask, which guards on ``isdir`` of the leaf,
      attached to the link. The link name stays in the writable data home, so a
      sandboxed process can unlink it and put its own directory there.

    So the leaf is NOT resolved: it is required to be a real directory under this
    name, and a link is refused rather than followed. Ancestors are deliberately
    left alone -- ``config_dir()`` documents that a symlinked data HOME is
    supported, and refusing that would break a layout the product allows.

    Distinct from ``sandbox._warn_if_alias_backed``, which warns rather than
    refuses on the same shape. That tradeoff is right where it lives: a read-only
    ceiling is an operator's config file, and a dotfile manager (chezmoi, stow)
    legitimately symlinks those, so refusing would turn a normal setup into a spawn
    failure. Neither of these two leaves is such a file. One is created on demand by
    the gateway and read by nothing else; the other holds the human-authored
    TEMPLATE whose separation from crew DATA is the containment story -- replacing
    it is authoring markup, not changing a setting.
    """
    target = data_home() / leaf
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        # Absent is fine here: `publish` creates it and the launcher materialises
        # it. Only an existing-but-aliased name is a refusal.
        return target
    except OSError as exc:  # pragma: no cover - unreadable parent
        raise PanelError("panel_dir_unreadable", f"cannot stat {target}: {exc}") from exc
    # ``is_link_or_junction`` rather than ``S_ISLNK``: a Windows directory
    # junction is not a symlink, so ``S_ISLNK`` (and ``os.path.islink``) call it a
    # real directory and this refusal would never fire on Windows -- leaving the
    # fence POSIX-only on a path whose whole purpose is to be unaliasable.
    if platform_compat.is_link_or_junction(target):
        raise PanelError(
            "panel_dir_is_a_symlink",
            f"{target} is a symlink or junction; the sandbox mask and the tool-gate "
            "fence attach to this NAME, so following it would write through to an "
            "unfenced inode while leaving this name replaceable. Remove the link and "
            "use a real directory.",
        )
    if not stat.S_ISDIR(info.st_mode):
        raise PanelError(
            "panel_dir_not_a_directory",
            f"{target} exists but is not a directory",
        )
    return target


def panel_dir() -> Path:
    """The gateway-only directory holding published panel records.

    NOT the crew's own member space, and NOT ``trust/`` either -- see the
    ``_PANELS_DIRNAME`` note for why each was rejected and how all three write
    paths (MCP route, file tools, sandboxed shell) are closed on this one.

    The record carries an OWNERSHIP claim (``crew``) that ``publish`` reads back to
    refuse a colliding write, and it is the redacted copy of untrusted text. Under
    ``members/<slug>/`` neither property survived contact with a second crew:
    ``members/`` is deliberately unfenced because a crew owns its own published
    data, so nothing stopped one crew writing ``members/<other-crew>/panel.json``
    directly -- forging another crew's state past ownership resolution AND past the
    redactors in one write, and the drawer would render it.

    One flat ``<slug>.json`` per crew, which is why this takes no slug -- every
    crew's record shares this directory.

    REAPING: nothing in the tree currently deletes a member space, so holding the
    record outside ``members/<slug>/`` takes nothing away today. Whoever adds that
    reaper has to clear this directory too, so it is stated here rather than left
    to be rediscovered.
    """
    return _real_dir_under_data_home(_PANELS_DIRNAME)


def panel_path(slug: str) -> Path:
    """Absolute path to one crew's published panel record, containment-checked.

    The slug is validated and then the resolved path is containment-checked --
    both steps, because validation and use are separated by a call boundary a
    future caller could bypass, and because a symlinked component must not land
    the record outside its trust-rooted directory. Mirrors
    ``members.dm_binding_path``.

    Does NOT create the directory; :func:`publish` does, on demand.
    """
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise CrewSlugError(f"invalid crew slug {slug!r}: must match {_SLUG_RE.pattern}")
    root = panel_dir()
    target = root / f"{slug}{_PANEL_SUFFIX}"
    # NOT resolved, for the reason in `_real_dir_under_data_home`: resolving is what
    # let an alias decide where the bytes land. The slug pattern already forbids a
    # separator, so the containment check below compares real path objects rather
    # than following anything.
    if target.parent != root:
        raise CrewSlugError(f"crew slug {slug!r} escapes {root}")
    if platform_compat.is_link_or_junction(target):
        raise CrewSlugError(
            f"the record for {slug!r} is a symlink or junction; it would write "
            "through to an unfenced inode"
        )
    return target


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------


def available_templates() -> list[str]:
    """Template ids that resolve, operator overrides included, sorted.

    Best-effort: an unreadable directory yields what the other one has rather
    than failing a read of somebody's webview.
    """
    found: set[str] = set()
    for root in (shipped_templates_dir(), override_templates_dir()):
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix != ".html" or not entry.is_file():
                continue
            if TEMPLATE_ID_RE.match(entry.stem):
                found.add(entry.stem)
    return sorted(found)


def _slug_candidate(name: str) -> str:
    """Slugify *name* the way ``members.slug_for_name`` does.

    Mirrored for the reason ``_SLUG_RE`` is (see its comment): ``members`` and
    ``artifacts`` -- where the real ``slugify`` lives -- are both on the cycle,
    and importing either cold raises ``ImportError`` from a partially
    initialized module. A test pins this against ``members.slug_for_name`` over
    a table of names so the two cannot drift.

    Faithful to that helper's shape: NFKD-normalize and drop combining marks so
    an accented letter becomes ascii, lowercase, collapse every run of
    slug-unsafe characters to a single hyphen, then trim hyphens from the ends.
    """
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _SLUG_NORMALIZE_RE.sub("-", text.lower().strip()).strip("-")
    return text[:80].rstrip("-")


def template_for_crew(name: str) -> str:
    """The template id a crew called *name* gets by default.

    A crew whose own name matches an installed template gets that template --
    which is how a bespoke view reaches its crew with no registry, no mapping
    table and no per-crew config: installing ``<crew-name>.html`` is the whole
    act of wiring it up. Everything else falls back to the generic template.

    The name is SLUGIFIED, not merely lowercased. Lowercasing alone made that
    promise true only for a crew whose name is a single already-lowercase word:
    every multi-word name kept its spaces, ``TEMPLATE_ID_RE`` rejects a space,
    and the match therefore fell back to the generic template no matter what was
    installed. A crew called ``Pipeline Conductor`` could not reach
    ``pipeline-conductor.html`` -- the mechanism was dead for exactly the names
    a bespoke template is worth writing for. Slugifying also makes this agree
    with how the crew's own record path is addressed, which was already a slug.
    """
    candidate = _slug_candidate(name)
    if TEMPLATE_ID_RE.match(candidate) and candidate in available_templates():
        return candidate
    return DEFAULT_TEMPLATE_ID


def resolve_template(template_id: str) -> str:
    """Return the HTML for *template_id*.

    Operator override first, then the shipped template. A template the operator
    dropped on disk is authored by the person who owns the machine, so it
    deliberately wins -- that is the customisation seam.

    Raises :class:`PanelError` for an id that does not validate or does not
    exist, so a typo in a publish call is reported rather than rendering the
    fallback and looking like the template is broken.
    """
    if not TEMPLATE_ID_RE.match(template_id or ""):
        raise PanelError("bad_template_id", f"invalid template id: {template_id!r}")
    for root in (override_templates_dir(), shipped_templates_dir()):
        candidate = root / f"{template_id}.html"
        try:
            # The id is already pinned to a traversal-free pattern; this second
            # check catches a symlink inside the override directory pointing
            # somewhere else entirely.
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root.resolve()):
                continue
            return resolved.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
    raise PanelError("unknown_template", f"no such template: {template_id!r}")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _check_depth(value: Any, limit: int) -> None:
    if limit <= 0:
        raise PanelError("data_too_deep", f"data nests deeper than {_MAX_DATA_DEPTH} levels")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, limit - 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, limit - 1)


def _validate_data(data: Any) -> str:
    """Return the canonical JSON for *data*, or raise :class:`PanelError`.

    A panel's data must be a JSON object: a bare scalar or list gives a template
    no names to bind to, and every template in the tree reads named fields.
    """
    if not isinstance(data, dict):
        raise PanelError("data_not_object", "panel data must be a JSON object")
    _check_depth(data, _MAX_DATA_DEPTH)
    try:
        # allow_nan=False: NaN and Infinity are not JSON, and JSON.parse in the
        # frame would throw on them -- refuse at publish rather than render a
        # panel that dies in the browser.
        #
        # NOT sort_keys: field order is presentation. A crew that publishes
        # cycle, then holding, then credits means that order -- templates render
        # a stat strip in key order, so sorting would silently alphabetise
        # somebody's dashboard and give them no way to control it. Insertion
        # order is already deterministic for a given input, which is all the
        # byte-cap measurement below needs.
        blob = json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PanelError("data_not_serializable", f"panel data is not JSON: {exc}") from exc
    if len(blob.encode("utf-8")) > _MAX_DATA_BYTES:
        raise PanelError("data_too_large", f"panel data exceeds {_MAX_DATA_BYTES} bytes")
    return blob


def _clamp(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


# --------------------------------------------------------------------------
# the escaping boundary
# --------------------------------------------------------------------------

#: Escaped inside the JSON string literals so the serialized data cannot end the
#: script element that carries it, cannot open a tag, and cannot terminate a
#: JavaScript string across a line break. ``</script>`` is the one that matters:
#: without the ``<`` rewrite, data holding that literal closes the island early
#: and everything after it parses as markup.
_JSON_HTML_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
}


def escape_json_for_html(blob: str) -> str:
    """Make a JSON document safe to embed in an HTML script element.

    The output is still valid JSON -- the replacements are JSON's own ``\\u``
    escapes inside string literals, so ``JSON.parse`` returns exactly the
    original values. Only the HTML parser's view changes.
    """
    for raw, escaped in _JSON_HTML_ESCAPES.items():
        blob = blob.replace(raw, escaped)
    return blob


def compose(template_html: str, data_json: str) -> str:
    """Substitute *data_json* into *template_html* at :data:`DATA_MARKER`.

    The data lands as an inert ``application/json`` island rather than being
    interpolated into markup or into executable JavaScript. A template reads it
    with ``JSON.parse`` and renders through DOM text APIs, which is what keeps a
    crew's string from becoming an element.

    Raises :class:`PanelError` when the template carries no marker: rendering
    the template anyway would silently drop every value the crew published.
    """
    if DATA_MARKER not in template_html:
        raise PanelError(
            "template_missing_marker",
            f"template does not contain the {DATA_MARKER} marker",
        )
    island = (
        f'<script type="application/json" id="{_DATA_ELEMENT_ID}">'
        f"{escape_json_for_html(data_json)}</script>"
    )
    # Only the first marker is filled; a template with two would otherwise
    # define the same element id twice and getElementById would pick one
    # arbitrarily.
    return template_html.replace(DATA_MARKER, island, 1)


# --------------------------------------------------------------------------
# read and write
# --------------------------------------------------------------------------


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Bounded exclusive lock over one crew's panel, failing closed.

    The lock lives on its own inode so a state write never replaces the file
    another process is holding. Bounded because a publish runs inside a crew's
    turn: waiting forever on a stuck holder would wedge the turn, so after the
    deadline the caller is told to try again on its next cycle.

    Scoped to the SLUG rather than to the records directory: the collision this
    serialises is two crews reaching one slug, so a per-slug lock is exactly as
    correct as a directory-wide one and does not make every crew's publish wait
    behind every other crew's.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
        while not try_acquire_lock(fd, exclusive=True):
            if time.monotonic() >= deadline:
                raise OSError("panel lock is held by another process; try again")
            time.sleep(_LOCK_POLL_SECS)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def _now_iso() -> str:
    """The publish time as UTC with an explicit ``+00:00`` offset.

    Zone-LESS local time was wrong for a value that leaves the host: the drawer
    hands ``published_at`` to ``new Date()``, which reads an offset-free string as
    the BROWSER's local time. On the loopback dashboard that is the same clock that
    wrote it and the bug is invisible; from a remote browser in another zone every
    age is skewed by the offset between them, so `2m ago` could read as hours.

    Fixed now rather than later because schema v1 has no consumers yet, so this
    costs nothing today and would be a migration once a stored record has readers.
    An offset-carrying stamp also parses identically in JS, Python
    (``datetime.fromisoformat``) and CLDR, so no reader needs to know the
    convention.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _RAW:
    """A pre-serialized JSON fragment, spliced in by :class:`_RawEncoder`.

    Exists so the stored record can be dumped as a whole while its ``data``
    member keeps the key order the crew published.
    """

    __slots__ = ("json",)

    def __init__(self, json_text: str) -> None:
        self.json = json_text


class _RawEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:  # pragma: no cover - exercised via publish
        if isinstance(o, _RAW):
            # ``json.JSONEncoder`` has no verbatim hook, so the fragment is
            # parsed back rather than string-spliced: splicing would let a
            # malformed fragment corrupt the whole document, and this fragment
            # was produced by json.dumps one step earlier so the round trip is
            # order-preserving and cheap.
            return json.loads(o.json, object_pairs_hook=dict)
        return super().default(o)


def _scrub(text: str) -> str:
    """Apply the shared credential + exfiltration-URL chain to one string.

    Same pair, in the same order, as every other capture-side scrubber in the
    repo (see ``acp/mcp_session_report._clean``): URLs first, then credentials.
    """
    scrubbed, _ = redact_exfiltration_urls(text)
    scrubbed, _ = redact_credentials(scrubbed)
    return scrubbed


def _scrub_published(value: Any) -> Any:
    """Recursively scrub every string a crew published -- KEYS included.

    A panel's data is assembled unattended from issue bodies, review comments and
    command output, so it is untrusted text on a path that ends at the operator's
    dashboard. Redacting at PUBLISH rather than at render is deliberate: it means
    a credential never enters the record at all, so it cannot be read back by
    a later reader, cannot survive in halves across the record's byte ceiling, and
    is not sitting on disk waiting for the next code path that forgets to scrub.

    Keys are scrubbed as well as values because a key is rendered as a heading --
    the template labels every field it shows, so a crew that put a token in a
    field NAME would print it just as surely as one that put it in the value.
    Non-string scalars are returned unchanged: there is nothing in a number or a
    boolean for either redactor to find.

    CALLER MUST CHECK DEPTH FIRST. This recurses without a limit of its own, so
    ``publish`` runs ``_check_depth`` before calling it -- see the note there.
    """
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for k, v in value.items():
            key = _scrub(str(k))
            if key in scrubbed:
                # Redaction is MANY-TO-ONE: two distinct credential-shaped keys
                # both become "[REDACTED: credential]". A dict comprehension would
                # keep only the last and the operator would read a panel silently
                # missing a field, with nothing on screen saying so.
                #
                # Refused rather than de-duplicated, because there is no honest
                # merge: the two source keys are different fields and the redacted
                # name cannot tell the reader which survived. Same failure as the
                # slug collision one layer down -- a lossy mapping used as an
                # identity -- and answered the same way.
                raise PanelError(
                    "redacted_key_collision",
                    f"two published field names both redact to {key!r}; "
                    "rename them so they stay distinguishable after redaction",
                )
            scrubbed[key] = _scrub_published(v)
        return scrubbed
    if isinstance(value, list):
        return [_scrub_published(v) for v in value]
    return value


def crew_key(name: str) -> str:
    """A stable digest of a crew's EXACT name, for ownership comparison.

    Two of this module's own guards collided without it. Redaction scrubs the
    stored ``crew`` because a crew name is untrusted text rendered to the
    operator; the ownership check compares the EXACT name because slugification is
    lossy. Together they broke a real case: a crew whose name happens to look
    credential-shaped (an ``AKIA``-prefixed 20-character name is enough) had its
    stored owner replaced by ``[REDACTED: credential]``, which then matched no
    exact name at all -- so that crew could never read its own panel.

    Splitting the two jobs fixes it: ``crew`` stays the redacted DISPLAY text, and
    this digest is what ownership is decided on. Comparing digests is exactly as
    strict as comparing names, because the input is the exact name either way.

    Hashed rather than stored raw for the same reason the display text is
    redacted: if the name IS credential-shaped, the record must not persist it.
    Plain SHA-256 mirrors ``kiro_prerequisite._claim_digest``, which hashes a claim
    "so no credential material can leave the reader" -- the established answer to
    this exact question, rather than a keyed scheme of this module's own with a key
    to manage and nothing else in the tree to compare against. A low-entropy name
    is recoverable from its digest by brute force, which is worth stating plainly:
    the digest lives only in the gateway-only records directory, and the
    alternative on offer -- keeping the exact name in the file -- is strictly worse
    than a preimage-able hash of it.
    """
    return hashlib.sha256(name.encode("utf-8", "replace")).hexdigest()


def publish(
    slug: str,
    *,
    # Explicit, ALONGSIDE name matching, and not a workaround for it.
    #
    # Name matching used to be broken (it lowercased instead of slugifying, so a
    # multi-word crew never found its own file), which made this field look like
    # a patch over that defect. It is not: matching now works, and this survives
    # because the two answer different questions. Matching answers "which
    # template is THIS crew's", and cannot express a template SHARED by several
    # crews -- three conductor crews pointing at one `conductor.html` would each
    # need their own copy of the file, and a crew could only change view by being
    # renamed. This field is how a caller says which of the installed templates
    # it wants; `template_for_crew` is what it asks when it has no opinion.
    template: str,
    data: Any,
    title: str = "",
    crew: str = "",
    owner_is_live: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Replace the crew's panel with a new one. Returns the stored record.

    Whole-document replacement rather than a merge: a panel describes the state
    of one cycle, and a partial update would leave last cycle's rows sitting
    beside this cycle's counters with nothing marking which is which.

    The template is resolved and the document composed here, at publish time, so
    a bad template id or a template missing its marker is reported to the crew
    that made the call -- while it can still fix the call -- instead of surfacing
    later as a broken webview with no author present.

    OWNERSHIP IS THE EXACT CREW NAME, not the slug, and it takes TWO things to
    hold -- they are one story, not two half-guards:

    1. The record lives in the gateway-only records directory (see
       :func:`panel_dir`), which no agent file tool and no sandboxed command can
       write, so ``publish`` is the ONLY way a panel is written. In the crew's own
       unfenced member space a colliding crew could skip this function entirely
       and write the file, and any check here would be theatre.
    2. Inside the lock, a stored ``crew`` that differs from the caller's is
       refused. Slugification is lossy -- ``Oncall`` and ``oncall`` reach one slug
       -- so without this either would overwrite the other and the operator would
       read one crew's state under the other's name.

    The READ side enforces the same claim: ``api_member_panel`` requires the exact
    crew name and refuses to hand back a record another crew owns. Guarding only
    the write left the loser of a collision still reading the winner's dashboard.

    Following the member layer's existing answer to the same lossiness rather than
    inventing a second convention: ``members.slug_for_name`` documents that it is
    not unique, and ``record_activity`` matches on BOTH the session and the exact
    ``member`` name for exactly this reason.
    """
    if not TEMPLATE_ID_RE.match(template or ""):
        raise PanelError("bad_template_id", f"invalid template id: {template!r}")
    # SHAPE AND DEPTH BEFORE ANYTHING RECURSES.
    #
    # ``_scrub_published`` walks the payload with no depth limit of its own, so a
    # 1000-level nested object blew the stack inside the SCRUBBER -- an uncaught
    # RecursionError and a 500, on a payload the depth cap exists to refuse
    # politely. Adding redaction is what inverted the order that made the cap
    # meaningful, so the cap now runs first.
    #
    # Safe to run first precisely because ``_check_depth`` is bounded: it counts
    # DOWN from the limit and raises on reaching zero, so it never descends further
    # than the cap allows. ``_validate_data`` re-checks the scrubbed copy -- it is
    # the canonical validator and has other callers -- and the second pass is
    # cheap once this one has already bounded the input.
    if not isinstance(data, dict):
        raise PanelError("data_not_object", "panel data must be a JSON object")
    _check_depth(data, _MAX_DATA_DEPTH)
    data_json = _validate_data(_scrub_published(data))
    template_html = resolve_template(template)
    # Composed eagerly and thrown away: this is the validation that the pair
    # actually renders. The reader composes again from the stored parts so an
    # edited template takes effect without the crew republishing.
    compose(template_html, data_json)

    crew_name = _clamp(_scrub(crew), _MAX_TITLE)
    # A publish MUST name its crew. Ownership is the only thing separating one
    # crew's drawer from another's, and a record stored without it was readable by
    # any crew and overwritable by any crew -- so "no name given" is refused at the
    # source rather than defaulted to "no owner". The route already refuses a
    # session with no crew binding (``no_crew``); this closes the same gap for a
    # direct caller, and makes "every stored record has an owner" true by
    # construction instead of by convention.
    if not (crew or "").strip():
        raise PanelError(
            "crew_required",
            "publishing a panel requires the crew's name: ownership is what keeps "
            "one crew's webview out of another's drawer",
        )
    # Derived from the RAW name, before redaction and before the display clamp:
    # those two transforms are lossy and many-to-one, which is precisely what made
    # the redacted display text unusable as an identity. See :func:`crew_key`.
    owner_key = crew_key(crew)

    record = {
        "schema": SCHEMA_VERSION,
        "template": template,
        "title": _clamp(_scrub(title), _MAX_TITLE),
        "crew": crew_name,
        "crew_key": owner_key,
        "data": None,
        "published_at": _now_iso(),
    }
    blob = json.dumps({**record, "data": _RAW(data_json)}, ensure_ascii=False, cls=_RawEncoder)
    if len(blob.encode("utf-8")) > _MAX_RECORD_BYTES:
        raise PanelError("record_too_large", "panel record exceeds its byte ceiling")

    target = panel_path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _locked(target.with_suffix(_LOCK_SUFFIX)):
        # OWNERSHIP IS CHECKED INSIDE THE LOCK, with the write, because the two
        # are one decision. Read before acquiring it and the check is a TOCTOU
        # race: two crews colliding on one slug both observe "no owner", both
        # pass, and the later write silently overwrites the first -- which is the
        # exact outcome the check exists to prevent, so a check outside the lock
        # is not a weaker guard but no guard at all.
        existing = read(slug)
        if existing is not None:
            # Compared on the DIGEST of the exact name, not on the redacted display
            # text: a credential-shaped name redacts to the same string for every
            # such crew, so display text would make two unrelated crews look like
            # one owner AND stop each from matching itself.
            owner = str(existing.get("crew_key") or "")
            # An UNOWNED existing record is replaceable, while an unowned record is
            # NOT readable (see the read route's guard). The asymmetry is the whole
            # point: serving a record nobody owns shows a viewer content they cannot
            # attribute, so a forgery succeeds; REPLACING it destroys those bytes and
            # stamps a real owner, so a forgery fails. Refusing the write instead
            # would wedge the slug -- a single forged record would permanently deny
            # the real crew its own panel, turning a containment breach into a
            # denial of service. ``owner_key`` is non-empty by construction now: an
            # empty crew is refused above.
            if owner and owner != owner_key:
                # An ORPHANED record is taken over, for the same anti-wedge reason.
                #
                # Strict ownership alone made a renamed or deleted crew permanent:
                # its record kept the slug, every later crew reaching that slug was
                # refused, and the refusal advised renaming a crew that no longer
                # exists. The only recovery left was hand-deleting a file inside a
                # directory that is gateway-only AND hidden from the agent sandbox.
                #
                # The liveness question is asked through a CALLBACK, and asked HERE,
                # inside the exclusive lock. The store cannot read the roster itself
                # (``members`` is on the import cycle this module stays off), and a
                # caller that read the record, decided, and then called publish would
                # be deciding on a snapshot the lock had not yet frozen -- the same
                # TOCTOU shape the ownership check itself was moved in here to avoid.
                if owner_is_live is None or owner_is_live(owner):
                    raise PanelError(
                        "crew_slug_collision",
                        f"panel {slug!r} belongs to crew {existing.get('crew')!r}, "
                        f"not {crew_name!r}; rename one of the crews so their names "
                        "do not collide",
                    )
        atomic_write(target, blob + "\n", mode=0o600)
    record["data"] = json.loads(data_json)
    return record


def read(slug: str) -> dict[str, Any] | None:
    """Return the crew's stored record, or ``None`` if it has no panel.

    Best-effort by design: a missing, unreadable, oversized or malformed file
    reads as "no panel published". A reader is rendering somebody's drawer, and
    an unparseable record on disk must show an empty state rather than breaking
    the page.
    """
    try:
        path = panel_path(slug)
        if path.stat().st_size > _MAX_RECORD_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    # ``MemberSlugError`` is a ``ValueError``, so a bad slug is caught here too.
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
        return None
    if not TEMPLATE_ID_RE.match(str(raw.get("template", ""))):
        return None
    return raw


def render_record(record: dict[str, Any] | None) -> str | None:
    """Compose an ALREADY-READ record, or ``None``.

    Takes an already-read record so a caller that also needs the record's fields
    can serve both halves from ONE snapshot. Reading twice -- once here and once
    for the metadata -- let a publish land in between and returned the OLD document
    beside the NEW summary: the docked chip and the expanded view would then
    disagree, which is the one thing a drawer showing both must not do.

    Composition still happens on READ rather than at publish, so an edited template
    takes effect on the next drawer open with no republish.
    """
    if record is None:
        return None
    try:
        template_html = resolve_template(str(record["template"]))
        data_json = json.dumps(record["data"], ensure_ascii=False, allow_nan=False)
        return compose(template_html, data_json)
    except (PanelError, TypeError, ValueError):
        # A template that was valid at publish and has since been deleted or
        # broken by an edit: show the empty state, not a stack trace.
        return None
