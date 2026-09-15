"""Crew templates: the store listing a crew member is hired from.

A template is an installed app's Custom Agent plus the job card its manifest's
``crew`` section carries (:class:`kiro_crew.apps.manifest.CrewTemplate`). This
module is the read side the hire route composes:

* :func:`resolve_store_template` -- from ``{app, agent}`` to the materialized
  agent file (``<app>--<agent>``, the copy ``apps.bridges`` writes into the
  agents directory when the app is enabled), the card, the version, the agent
  definition and the initial briefing text.
* :func:`template_ref` -- the ``template`` the wrapper row records
  (``<app>/<agent>``), the same namespacing the materialized file uses.
* :func:`write_pristine_copy` -- the unmodified template payload at the
  installed version, kept at ``members/<slug>/template.json`` so a later role
  update can three-way merge (BASE = this file, MINE = the member's agent file,
  THEIRS = the new version). The reader arrives with that update.
* :func:`seed_briefing` -- write ``initial_briefing`` as the new member's own
  ``briefing.md``; from then on the file is the member's lived state and no
  template operation touches it.

Leaf-ish on purpose: imports the manifest, the members module and the agents
directory resolver, never the dashboard handlers.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew import members
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.apps import admission as _admission
from kiro_crew.apps import bridges as _bridges
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.bridges import _namespace, _safe_link_name, render_app_agent_spec
from kiro_crew.apps.manager import _read_installed, app_dir, get_app_manifest
from kiro_crew.apps.manifest import AppManifest, CrewTemplate, _path_escapes_app_root
from kiro_crew.pinned_fs import (
    create_and_open_dir_pinned,
    open_dir_pinned,
    read_bytes_at,
    read_file_pinned,
    supports_pinned_walk,
    unlink_pinned,
    write_file_pinned,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: The pristine copy's file name inside ``members/<slug>/``.
PRISTINE_COPY_FILE = "template.json"

#: Largest initial briefing a template may seed (bytes). The member's own
#: briefing is injection-capped downstream; this bounds what a store listing can
#: put on disk in one hire.
INITIAL_BRIEFING_MAX_BYTES = 64 * 1024

#: Largest agent definition a template may ship (bytes) -- the same order as the
#: spec reader caps elsewhere; a store listing is not a place for a novel.
TEMPLATE_SPEC_MAX_BYTES = 512 * 1024
# A file-backed system prompt the card pins: read through the pinned root, inlined.
TEMPLATE_PROMPT_MAX_BYTES = 256 * 1024

_SAFE_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class TemplateUnavailable(Exception):
    """A store template cannot be hired from right now; ``code`` says why.

    ``code`` is a stable machine-readable word the hire route answers with:
    ``app_not_installed``, ``app_disabled``, ``app_admission_denied``,
    ``template_not_offered``, ``template_invalid``,
    ``template_spec_unreadable``, ``template_not_materialized``,
    ``template_read_unpinned`` (this platform cannot read the app tree through
    a pinned directory descriptor, so a store hire is refused rather than read
    by path).
    """

    def __init__(self, code: str, message: str, *, status: int = 404):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class StoreTemplate:
    """Everything a hire needs from one store listing, resolved once."""

    app: str
    version: str
    card: CrewTemplate
    #: The declared agent name (spec ``name`` else the file stem).
    agent_name: str
    #: The materialized agent file's stem: ``<app>--<agent_name>``. This is the
    #: ``kiro_agent`` the member is created against and then forked from.
    materialized: str
    #: The template's agent definition as shipped (the pristine BASE).
    spec: dict[str, Any] = field(default_factory=dict)
    #: The materialized file's content as read ONCE at resolve time (and, for a
    #: pinned card, verified against the bridge's rendering): what the hire
    #: copies into the member's own file, so no path is reopened after the checks.
    materialized_spec: dict[str, Any] = field(default_factory=dict)
    initial_briefing: str = ""

    @property
    def ref(self) -> str:
        return template_ref(self.app, self.agent_name)


def template_ref(app: str, agent_name: str) -> str:
    """The ``template`` a wrapper row records: ``<app>/<agent>``."""
    return _namespace(app, agent_name)


def _read_app_bytes(root_fd: int, rel: str, cap: int, *, what: str) -> bytes | None:
    """The bytes of an app-tree file named RELATIVE to the app root, or ``None``
    for a missing, refused or oversized one.

    Every component of *rel* is walked ``O_NOFOLLOW`` relative to the root
    descriptor the caller pinned right after validation
    (``pinned_fs.read_bytes_at``): validation resolved the tree by path, and a
    by-path read afterwards re-resolves it -- an ``agents`` directory swapped for
    a link to ``~/.docker`` in between would be pinned AS the parent and its
    files read as the template's. There is no by-path fallback: a platform
    without descriptor-relative ``O_NOFOLLOW`` opens (Windows) never reaches
    this function -- ``resolve_store_template`` refuses the hire there
    (``template_read_unpinned``), the way briefing reads fail closed on it.
    """
    try:
        data = read_bytes_at(root_fd, rel, what=what, max_bytes=cap + 1)
    except (OSError, ValueError):
        return None
    return None if len(data) > cap else data


def _bytes_to_json(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return _parse_spec_bytes(data)


def content_digest(data: bytes) -> str:
    """The form a card's ``digests`` pins: ``sha256:<hex>`` over the file's bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _verify_card_content(
    app: str, card: CrewTemplate, root_fd: int, *, signature_required: bool
) -> dict[str, bytes]:
    """Refuse a card whose agent file or briefing is not the bytes it pins.

    The manifest signature covers the card, not the files the card points at;
    the card's ``digests`` are what carry the publisher's signature to the agent
    definition and the briefing -- text that becomes the member's own definition
    and prompt. So: a declared digest that does not match the file on disk NOW
    refuses the hire (``template_tampered``); a fleet whose admission policy
    requires a signature also refuses a card that pins nothing
    (``template_unverified``) -- for that fleet, a signature that does not reach
    the instructions is not the verification it asks for. A fleet without that
    requirement hires an undigested card as before. Returns the VERIFIED bytes by
    key (``agent``, ``initial_briefing``): the caller parses and copies those,
    never a re-read of the path -- an app that swaps the file after the check
    would otherwise have the swapped bytes hired.
    """
    verified: dict[str, bytes] = {}
    if signature_required:
        # A pinned card is pinned WHOLE: the agent file, and the briefing when
        # the card names one. A card pinning only its briefing (or only its
        # agent) would leave the other file's bytes unauthenticated under a
        # signature that reads as verifying them.
        missing = [
            key
            for key, rel in (("agent", card.agent), ("initial_briefing", card.initial_briefing))
            if rel and not card.digests.get(key)
        ]
        if missing:
            raise TemplateUnavailable(
                "template_unverified",
                f"App '{app}' signs its manifest but pins no content digest for "
                f"{' / '.join(missing)} of {card.agent!r}; this fleet requires every file "
                "the card points at to be pinned",
                status=409,
            )
    if not card.digests:
        return verified
    for key, rel in (("agent", card.agent), ("initial_briefing", card.initial_briefing)):
        want = card.digests.get(key)
        if not want or not rel:
            continue
        data = _read_app_bytes(
            root_fd,
            rel,
            TEMPLATE_SPEC_MAX_BYTES if key == "agent" else INITIAL_BRIEFING_MAX_BYTES,
            what=f"template {key.replace('_', ' ')}",
        )
        if data is None or content_digest(data) != want:
            raise TemplateUnavailable(
                "template_tampered",
                f"App '{app}': {rel!r} is not the file the card's digest pins; "
                "reinstall the app",
                status=409,
            )
        verified[key] = data
    return verified


def _parse_spec_bytes(data: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _verify_materialized_matches(
    app: str,
    root: Path,
    agent_path: str,
    on_disk: dict[str, Any],
    agent_name: str,
    *,
    shipped_text: str | None,
) -> None:
    """Refuse a hire whose materialized file (*on_disk*: read once by the caller,
    the very dict the hire copies) is not the bridge's rendering of the verified
    shipped definition. The rendering starts from *shipped_text* -- the bytes the
    digest check verified -- when the card pinned them, never from a re-read of
    the shipped path an app could swap in between. Compared as parsed JSON, so
    formatting is free and every field -- prompt, tools, hooks, MCP commands --
    is not."""
    rendered = render_app_agent_spec(
        app, root, agent_path, keep_user_edits=False, shipped_text=shipped_text
    )
    if rendered is None or rendered[1] != on_disk:
        raise TemplateUnavailable(
            "template_tampered",
            f"App '{app}': the installed agent {agent_name!r} is not the app's shipped "
            "definition as this host renders it; re-enable the app to restore it",
            status=409,
        )


def _authenticate_runtime_prompt(
    app: str,
    root: Path,
    root_fd: int,
    card: CrewTemplate,
    spec: dict[str, Any],
    agent_name: str,
    *,
    signature_required: bool,
) -> None:
    """Under pinning, a system prompt the agent takes from a FILE must be the
    publisher's bytes -- and once verified it becomes the member's own text.

    The bridge's prompt seam (``bridges._apply_agent_prompt``) lets an app's
    per-user policy file -- in the app's mutable data dir, user state the
    publisher never signed -- point the agent's ``prompt`` at a file the app
    renders there. The materialized-matches check above re-renders through
    that same seam, so a data-dir prompt matches on both sides and the digest
    over the shipped definition says nothing about the instructions the member
    would actually run. So a ``file://`` prompt is accepted only when it
    resolves INSIDE the app's packaged root and OUTSIDE its data dir
    (``app_dir/data`` lives inside the root for an installed app, so
    containment in the root alone would admit it); anything else is
    ``template_prompt_unverified``.

    Containment alone is not authentication either: the manifest signature
    covers the card, not the files the card points at, and an installed app's
    tree is the app's to rewrite after signing. So the prompt file is treated
    like the agent file and the briefing (``_verify_card_content``): a fleet
    that requires signatures refuses a card that pins no ``digests.prompt``
    for it (``template_unverified``); a declared digest is checked against the
    bytes read through the pinned root descriptor (``template_tampered`` on a
    mismatch); and the VERIFIED text replaces the ``file://`` URI in *spec* --
    the dict the hire copies and the pristine copy records -- so the member's
    instructions are its own bytes from the hire on, and a later rewrite of the
    app's file reaches nothing the member runs. An inline prompt is the
    shipped file's own and is covered by its digest already.
    """
    prompt = spec.get("prompt")
    if not isinstance(prompt, str) or not prompt.startswith("file://"):
        return
    path = Path(prompt[len("file://") :])
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        data_resolved = (app_dir(app) / "data").resolve()
    except OSError:
        resolved, root_resolved, data_resolved = path, root, app_dir(app) / "data"
    in_root = resolved == root_resolved or root_resolved in resolved.parents
    in_data = resolved == data_resolved or data_resolved in resolved.parents
    if not in_root or in_data or resolved == root_resolved:
        raise TemplateUnavailable(
            "template_prompt_unverified",
            f"App '{app}': the installed agent {agent_name!r} takes its instructions from a "
            "file the app renders at runtime, which the publisher's signature does not "
            "cover; a hire under signature enforcement refuses it",
            status=409,
        )
    want = card.digests.get("prompt")
    if not want:
        if signature_required:
            raise TemplateUnavailable(
                "template_unverified",
                f"App '{app}' signs its manifest but pins no content digest for the prompt "
                f"file of {agent_name!r}; this fleet requires every file the card points at "
                "to be pinned",
                status=409,
            )
        # A fleet without the requirement hires an undigested card as before;
        # the URI stays, like its unpinned agent file stays the app's.
        return
    rel = resolved.relative_to(root_resolved).as_posix()
    data = _read_app_bytes(root_fd, rel, TEMPLATE_PROMPT_MAX_BYTES, what="template prompt")
    if data is None or content_digest(data) != want:
        raise TemplateUnavailable(
            "template_tampered",
            f"App '{app}': the prompt file of {agent_name!r} is not the file the card's "
            "digest pins; reinstall the app",
            status=409,
        )
    spec["prompt"] = data.decode("utf-8", errors="replace")


def _read_text_pinned(path: Path, cap: int, *, what: str) -> str | None:
    """Read an app- or member-controlled file without following a planted link.

    An installed app's tree is the app's to change between install and hire, and
    a member's directory is agent-writable: a by-name ``read_text`` on either is
    a disclosure primitive pointed at whatever the name resolves to by then --
    replace the briefing with a symlink to a credential file and the next hire
    copies that credential into a prompt-visible briefing. ``read_file_pinned``
    pins the ancestors and refuses a non-regular final component. ``None`` for a
    missing, refused, oversized or undecodable file.
    """
    try:
        text = read_file_pinned(path, what=what, max_bytes=cap + 1)
    except (OSError, ValueError):
        return None
    if len(text.encode("utf-8", "surrogatepass")) > cap:
        return None
    return text


def _read_json_capped(path: Path, cap: int, *, what: str) -> dict[str, Any] | None:
    text = _read_text_pinned(path, cap, what=what)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def resolve_store_template(app: str, agent_path: str) -> StoreTemplate:
    """Resolve ``{app, agent}`` to a hireable :class:`StoreTemplate`.

    ``agent_path`` is the manifest ``agents`` entry the card names. Raises
    :class:`TemplateUnavailable` for every way the listing cannot be hired
    from: the app is not installed or not enabled (only an enabled app has its
    agents materialized), the manifest offers no card for that agent, the
    shipped spec is unreadable, or the materialized file is missing.
    """
    if not isinstance(app, str) or not _SAFE_APP_NAME_RE.match(app):
        raise TemplateUnavailable("app_not_installed", "source.app must name an installed app")
    # The manifest and the root every card path is read under come from the
    # same source the resource registration reads (``bridges._registration_source``):
    # a shipped builtin's IMMUTABLE package directory, a third-party app's
    # installed snapshot. Builtin-ness is therefore a fact about the package
    # tree (``shipped_builtin_app_root``), never about ``installed.json`` --
    # that file is the app's own to rewrite, so an ``origin`` it claims cannot
    # buy the builtin exemption below, and a forged builtin NAME resolves to
    # the genuine shipped files rather than the forger's.
    shipped_root = _bridges.shipped_builtin_app_root(app)
    builtin = shipped_root is not None
    manifest: AppManifest | None
    if shipped_root is not None:
        root = shipped_root
        try:
            manifest = AppManifest.from_json_file(shipped_root / "app.json")
        except (OSError, ValueError):
            manifest = None
    else:
        root = app_dir(app)
        manifest = get_app_manifest(app)
    if manifest is None:
        raise TemplateUnavailable("app_not_installed", f"App '{app}' is not installed")
    meta = _read_installed(app)
    if meta is None or not meta.enabled:
        raise TemplateUnavailable(
            "app_disabled", f"App '{app}' is disabled; enable it to hire from it", status=409
        )
    # Admission again, against the manifest on disk NOW. Install, update and
    # enable each run it, but the card's role, triggers and briefing become
    # prompt-adjacent text on the member, and the manifest they come from is the
    # app's to rewrite after enable -- a signed manifest edited afterwards
    # carries a signature that fails to verify, and a fleet that requires one
    # must not see its text hired in. Shipped builtins are exempt exactly as
    # enable exempts them -- decided by the package tree above, not by the
    # mutable ``origin`` field.
    signature_required = False
    if not builtin:
        # ONE policy snapshot for both decisions: the deny, and what the deny's
        # absence implies for the card (``require_signature`` -> every file the
        # card points at must be pinned). Read twice, a policy edited between
        # the two reads would have the hire admitted under one policy and its
        # content verified under another.
        policy = _admission.load_app_admission_policy()
        denied = app_admission_denied(app, manifest=manifest, action="hire", policy=policy)
        if denied:
            raise TemplateUnavailable(
                "app_admission_denied",
                f"App '{app}' is blocked by the admission policy: {denied}",
                status=409,
            )
        signature_required = policy.require_signature
    card = next((t for t in manifest.crew.templates if t.agent == agent_path), None)
    if card is None:
        raise TemplateUnavailable(
            "template_not_offered", f"App '{app}' offers no template for {agent_path!r}"
        )
    # The manifest on disk NOW, not the one install validated: an app's tree is
    # the app's to change afterwards, and a card path rewritten to ``../../.env``
    # would otherwise be read (pinned reads contain links, not traversal). The
    # same checks install runs, against the same root (resolved above).
    problems = manifest.crew.validate(manifest.agents, root)
    if problems or any(
        _path_escapes_app_root(p, root) for p in (agent_path, card.initial_briefing) if p
    ):
        raise TemplateUnavailable(
            "template_invalid",
            f"App '{app}' ships a crew section that fails validation; reinstall it",
            status=409,
        )
    # Everything below reads the app's tree through ONE descriptor of its root,
    # pinned here, right after the validation above walked the same tree by
    # path: a by-path read after validation re-resolves every component, so an
    # ``agents`` directory swapped for a link to a credential directory in
    # between would be pinned as the parent and its files hired as the
    # template's. Relative components are walked ``O_NOFOLLOW`` from this
    # descriptor instead (``pinned_fs.read_bytes_at``). A platform that cannot
    # open relative to a directory descriptor with ``O_NOFOLLOW`` (Windows)
    # has no read that closes that window, so the hire is REFUSED there rather
    # than read by path -- the same fail-closed rule the member briefing
    # applies (``members.member_briefing_supported``). The gallery lists the
    # card with this code, so the refusal is visible before the click.
    if not supports_pinned_walk():
        raise TemplateUnavailable(
            "template_read_unpinned",
            f"App '{app}': store templates can be hired only where the app's files can be "
            "read through a pinned directory descriptor; this platform cannot pin the "
            "app tree against a swap, so the hire is refused",
            status=409,
        )
    try:
        root_fd = open_dir_pinned(root, what="app root")
    except OSError as exc:
        raise TemplateUnavailable(
            "template_invalid",
            f"App '{app}': its directory could not be opened as a plain directory",
            status=409,
        ) from exc
    try:
        return _resolve_from_root(
            app,
            manifest,
            card,
            agent_path,
            root,
            root_fd,
            signature_required=signature_required,
        )
    finally:
        os.close(root_fd)


def _resolve_from_root(
    app: str,
    manifest: Any,
    card: CrewTemplate,
    agent_path: str,
    root: Path,
    root_fd: int,
    *,
    signature_required: bool,
) -> StoreTemplate:
    """The read half of :func:`resolve_store_template`, every app-tree read
    anchored to *root_fd* (see there)."""
    # The signature reaches the FILES only through the card's digests: verified
    # against the bytes on disk now, before anything is read as a spec.
    verified = _verify_card_content(app, card, root_fd, signature_required=signature_required)
    # Parsed from the VERIFIED bytes when the card pinned them; otherwise ONE
    # read through the root descriptor -- the same bytes the materialized
    # comparison below renders from, never a second read of the path.
    spec_bytes = (
        verified["agent"]
        if "agent" in verified
        else _read_app_bytes(
            root_fd, agent_path, TEMPLATE_SPEC_MAX_BYTES, what="template agent file"
        )
    )
    spec = _bytes_to_json(spec_bytes)
    if spec is None or spec_bytes is None:
        raise TemplateUnavailable(
            "template_spec_unreadable",
            f"The template's agent file {agent_path!r} could not be read",
            status=409,
        )
    declared = spec.get("name")
    agent_name = declared if isinstance(declared, str) and declared else Path(agent_path).stem
    materialized = _safe_link_name(_namespace(app, agent_name))
    materialized_path = kiro_agents_dir_path() / f"{materialized}.json"
    if not materialized_path.is_file():
        raise TemplateUnavailable(
            "template_not_materialized",
            f"App '{app}' has not installed its agent {agent_name!r} yet; re-enable the app",
            status=409,
        )
    # Read ONCE: this dict is what the hire copies into the member's own file
    # (the create takes it as ``copy_spec``), so no path is reopened after the
    # checks below and the bytes checked are the bytes copied.
    materialized_spec = _read_json_capped(
        materialized_path, TEMPLATE_SPEC_MAX_BYTES, what="materialized template agent file"
    )
    if materialized_spec is None:
        raise TemplateUnavailable(
            "template_not_materialized",
            f"App '{app}': the installed agent file {materialized!r} could not be read; "
            "re-enable the app",
            status=409,
        )
    if card.digests or signature_required:
        # The digests authenticate the SHIPPED file; the hire COPIES the
        # materialized one, which lives in the agent-writable agents directory.
        # So the materialized file must be exactly what the bridge renders from
        # the verified shipped definition NOW (own servers, managed refs, the
        # per-app MCP policy and prompt -- host state, none of it the file's to
        # carry) -- a hand edit to a pinned app's shared template, or an agent's
        # rewrite of it, is refused rather than copied as the publisher's.
        _verify_materialized_matches(
            app,
            root,
            agent_path,
            materialized_spec,
            agent_name,
            shipped_text=spec_bytes.decode("utf-8", errors="replace"),
        )
        _authenticate_runtime_prompt(
            app,
            root,
            root_fd,
            card,
            materialized_spec,
            agent_name,
            signature_required=signature_required,
        )
    briefing = ""
    if card.initial_briefing:
        # The verified bytes when the card pinned them, else one read through
        # the root descriptor; decoded the way the pinned read decodes.
        raw = (
            verified["initial_briefing"]
            if "initial_briefing" in verified
            else _read_app_bytes(
                root_fd,
                card.initial_briefing,
                INITIAL_BRIEFING_MAX_BYTES,
                what="template initial briefing",
            )
        )
        text: str | None = raw.decode("utf-8", errors="replace") if raw is not None else None
        if text is None:
            logger.warning(
                "template %s/%s: initial_briefing missing, oversized or not a regular file; "
                "not seeded",
                app,
                agent_name,
            )
        else:
            briefing = text
    return StoreTemplate(
        app=app,
        version=manifest.version,
        card=card,
        agent_name=agent_name,
        materialized=materialized,
        spec=spec,
        materialized_spec=materialized_spec,
        initial_briefing=briefing,
    )


def pristine_copy_path(slug: str) -> Path:
    return members.member_dir(slug) / PRISTINE_COPY_FILE


def _ensure_members_root() -> None:
    members.members_root().mkdir(parents=True, exist_ok=True)


def write_pristine_copy(slug: str, template: StoreTemplate) -> Path:
    """Record the unmodified template payload at the installed version.

    The BASE of a later three-way merge: the agent definition as MATERIALIZED
    -- the very dict the hire copied into the member's own file
    (``StoreTemplate.materialized_spec``), never the raw shipped spec -- plus
    the card's mergeable fields (role, triggers). BASE and the copy are the
    same bytes at the hire, so a shipped definition rewritten under an unsigned
    app whose materialized file was not re-rendered (nothing verifies the two
    agree for an unpinned card: a user's edits to an unsigned app's shared
    template are preserved by design) cannot leave a member whose BASE never
    matched what it started from; and a later merge that compared the member
    against the raw shipped spec would read the bridge's plumbing (own MCP
    servers, managed refs, policy) as the member's own customization. Written
    atomically; the member directory is created if the member has not written
    anything yet.
    """
    path = pristine_copy_path(slug)
    payload = {
        "template": template.ref,
        "version": template.version,
        "agent": template.materialized_spec,
        "card": {"role": template.card.role, "triggers": template.card.triggers},
    }
    # The member directory is agent-writable: a by-name atomic replace there is
    # a truncation primitive pointed at whatever the name resolves to when the
    # rename lands. ``write_file_pinned`` creates the member directory through
    # the PINNED members root and refuses a planted link at either name; only
    # the root itself -- trust-rooted under the data home, not agent-named --
    # is created by name, as the pinned helpers require of their callers.
    _ensure_members_root()
    write_file_pinned(
        path, json.dumps(payload, indent=2, ensure_ascii=False), what="member pristine copy"
    )
    return path


def remove_step_four_files(slug: str, *, pristine: bool, briefing: bool) -> None:
    """Undo what a store hire's step 4 wrote when the step did not complete.

    The pristine copy and the seeded briefing are slug-keyed files in the
    agent-writable member directory: left behind by a hire whose enrollment
    failed (its row is rolled back, so no crewmate ever existed), the NEXT
    member to take the slug -- a local hire, say -- would inherit them as its
    own base and briefing. Each is removed through the pinned member
    directory (``unlink_pinned``: a link planted at an ancestor is never
    followed), only the ones this step wrote (*pristine* / *briefing*), and
    only while the caller still holds the config lock the step published
    under, so the files are provably still this hire's. Best-effort: a
    failure is logged, since the caller is already unwinding.
    """
    if pristine:
        try:
            unlink_pinned(pristine_copy_path(slug), what="member pristine copy")
        except (OSError, ValueError, members.MemberSlugError):
            logger.warning(
                "could not remove the pristine copy of %s on unwind", slug, exc_info=True
            )
    if briefing:
        try:
            unlink_pinned(members.member_briefing_path(slug), what="member briefing")
        except (OSError, ValueError, members.MemberSlugError):
            logger.warning(
                "could not remove the seeded briefing of %s on unwind", slug, exc_info=True
            )


RETIRED_BRIEFINGS_DIR_NAME = ".retired"
"""Under ``members_root()``: where a regular briefing found at a NEW member's
name is moved before the template's is seeded (``_replace_preexisting_briefing``).
One entry per hire, ``<slug>--<UTC stamp>-<4 hex>``, holding ``briefing.md``."""


def _replace_preexisting_briefing(slug: str, name: str, dir_fd: int) -> None:
    """Clear the briefing name of a member that did not exist, keeping what a
    regular file there holds.

    The seed runs inside the hire's locked publication, for a member whose row
    landed in this same hold and whose slug no row held before (the slug
    admission refuses a collision), so nothing at the name is the NEW member's
    lived state. What it can be, by kind:

    * a LINK -- the member directory is agent-writable and the slug is a
      deterministic derivation of the display name, so a link planted ahead of
      the hire is an attempt to hand the new member an assignment of the
      planter's choosing (or to have the seed write through it). Unlinked BY
      ITS LITERAL NAME relative to the pinned directory: the link goes, its
      target is never touched;
    * a REGULAR FILE -- the same plant in file form, OR the briefing a member
      that once held this slug wrote and left behind (a fire archives, a bare
      row delete does not). The two cannot be told apart here, and a member's
      own notes are not this hire's to destroy, so the file is MOVED out of the
      new member's space into ``members/.retired/<slug>--<stamp>-<rand>/
      briefing.md`` (by literal name, relative to pinned directories; the
      entry is created fresh, never reused) where it is out of the new
      member's reach and recoverable by the owner;
    * anything else -- refused; the hire fails closed rather than guess.

    Each case is recorded in the security event log, and the template's
    briefing is created at the cleared name by the caller.
    """
    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        kind, outcome = "link", "replaced"
    elif stat.S_ISREG(st.st_mode):
        kind, outcome = "file", "archived"
    else:
        raise OSError(errno.EEXIST, f"unexpected entry at the briefing name of {slug!r}")
    archived_to = ""
    if kind == "file":
        archived_to = _archive_preexisting_briefing(slug, name, dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)
    logger.warning(
        "member %s: a %s already sat at the briefing name of a member that did not "
        "exist; %s%s, and the template's briefing takes the name",
        slug,
        kind,
        outcome,
        f" to {archived_to}" if archived_to else "",
    )
    # The audit trail is the durable trace; never let it change the outcome
    # (the name is cleared either way, and the file's bytes are kept).
    try:
        sel().log_api_access(
            caller="system",
            operation=f"member_briefing_preexisting_{outcome}",
            outcome=outcome,
            source="member_hire",
            resources=f"member {slug}: pre-existing {kind} at the briefing name"
            + (f" moved to {archived_to}" if archived_to else ""),
        )
    except Exception:  # noqa: BLE001 - audit failure must not decide the hire
        logger.debug("could not record the cleared briefing for %s", slug, exc_info=True)


def _archive_preexisting_briefing(slug: str, name: str, dir_fd: int) -> str:
    """Move the regular file at *name* (relative to the pinned member directory
    *dir_fd*) into a fresh ``members/.retired/<slug>--<stamp>-<rand>/`` entry as
    ``briefing.md``; returns the entry's name. Every step is by literal name
    against a pinned descriptor: the retired root and the entry are opened
    ``O_NOFOLLOW`` (a link planted at either name is refused), the entry is
    created here (``mkdir`` fails on an existing name, so a planted entry is
    never written into), and the rename moves the member's file without
    resolving anything."""
    retired_fd = create_and_open_dir_pinned(
        members.members_root() / RETIRED_BRIEFINGS_DIR_NAME, what="retired member briefings"
    )
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for _ in range(8):
            entry = f"{slug}--{stamp}-{secrets.token_hex(2)}"
            try:
                os.mkdir(entry, 0o700, dir_fd=retired_fd)
            except FileExistsError:
                continue
            break
        else:
            raise OSError(errno.EEXIST, f"could not create an archive entry for {slug!r}")
        entry_fd = os.open(entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=retired_fd)
        try:
            os.rename(name, "briefing.md", src_dir_fd=dir_fd, dst_dir_fd=entry_fd)
        finally:
            os.close(entry_fd)
        return f"{RETIRED_BRIEFINGS_DIR_NAME}/{entry}"
    finally:
        os.close(retired_fd)


def seed_briefing(slug: str, text: str) -> bool:
    """Write a template's initial briefing as the new member's own ``briefing.md``.

    True when the file was written. Called for a member that did not exist
    until the hire's locked publication (see ``_link_member_to_template``), so
    the template's briefing is what the member starts with, whatever sat at
    the name before: a pre-existing link there is a plant and is unlinked, a
    pre-existing regular file (a plant, or the notes of a member that once
    held the slug) is moved into ``members/.retired/`` rather than destroyed
    -- each by its literal name, recorded in the security event log
    (:func:`_replace_preexisting_briefing`). The write itself is the CREATE
    (``O_CREAT | O_EXCL | O_NOFOLLOW`` through the pinned member directory):
    it never follows a link and never truncates a target, and a name that is
    taken again after the replacement is refused rather than raced. A platform
    where briefings are not read (no ``O_NOFOLLOW``) gets none, so the member
    is not handed a file the runtime will never inject.
    """
    if not text or not members.member_briefing_supported():
        return False
    path = members.member_briefing_path(slug)
    _ensure_members_root()
    dir_fd = create_and_open_dir_pinned(path.parent, what="member directory")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            _replace_preexisting_briefing(slug, path.name, dir_fd)
            # Once: a name taken again between the unlink and this create is
            # something racing the hire at the member's directory; fail closed.
            fd = os.open(path.name, flags, 0o600, dir_fd=dir_fd)
        try:
            payload = text.encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            # A short write or ENOSPC must not leave a truncated briefing that
            # ``O_EXCL`` then reports as the member's own on every retry.
            os.close(fd)
            fd = -1
            try:
                os.unlink(path.name, dir_fd=dir_fd)
            except OSError:
                logger.warning("could not remove a partially seeded briefing for %s", slug)
            raise
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(dir_fd)
    return True
