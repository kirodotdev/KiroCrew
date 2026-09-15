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
* :func:`write_pristine_copy` / :func:`read_pristine_copy` -- the unmodified
  template payload at the installed version, kept at
  ``<data home>/member-templates/<member id>.json`` (a gateway-only top-level
  leaf: sandbox-masked, refused to agent file tools) and stamped with the member's id
  and private-store generation: the BASE of the role update's three-way merge
  (MINE = the member's agent file and card fields, THEIRS = the template as
  installed now).
* :func:`resolve_template_ref` -- from a wrapper row's ``template``
  (``<app>/<agent name>``) back to the listing, for the update.
* :func:`plan_role_update` / :func:`merge_role_update` -- the per-field
  three-way merge: only THEIRS changed -> apply, only MINE changed -> keep,
  both -> the user picks. Scope is the template-provided definition (the agent
  file's keys except ``name``, which is the member's id) plus the card's
  ``role`` and ``triggers`` -- never lived state.
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

from kiro_crew import members, platform_compat
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.apps import admission as _admission
from kiro_crew.apps import bridges as _bridges
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.bridges import _namespace, _safe_link_name, render_app_agent_spec
from kiro_crew.apps.manager import _read_installed, app_dir, get_app_manifest
from kiro_crew.apps.manifest import AppManifest, CrewTemplate, _path_escapes_app_root
from kiro_crew.config.paths import data_home
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
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)

#: The pristine copies' directory: a TOP-LEVEL leaf of the data home holding one
#: ``<member id>.json`` per member, bind-masked from every sandboxed process
#: (``sandbox._CREW_HIDDEN_LEAVES``), pre-created before each spawn so the mask
#: has a name to bind over (``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``) and refused
#: to the agent file tools on every OS (``security._CREW_SECRET_LEAVES``). NOT
#: inside ``members/<slug>/``: that directory is agent-writable and its slug is
#: lossy (two ids can share one). NOT under ``trust/`` either: that subtree is
#: sandbox-VISIBLE (its SEL key and log have in-sandbox readers and appenders),
#: so a spawned interpreter's ``open()`` could rewrite a base there, bypassing
#: the file-tool gate. A forged or shared BASE makes the merge skip a template
#: change or overwrite a customization silently, which is the harm the fence
#: exists for; only the gateway reads or writes a base.
PRISTINE_COPIES_DIR_NAME = "member-templates"

#: Largest initial briefing a template may seed (bytes). The member's own
#: briefing is injection-capped downstream; this bounds what a store listing can
#: put on disk in one hire.
INITIAL_BRIEFING_MAX_BYTES = 64 * 1024

#: Largest agent definition a template may ship (bytes) -- the same order as the
#: spec reader caps elsewhere; a store listing is not a place for a novel.
TEMPLATE_SPEC_MAX_BYTES = 512 * 1024
# A file-backed system prompt the card pins: read through the pinned root, inlined.
TEMPLATE_PROMPT_MAX_BYTES = 256 * 1024

#: Largest pristine copy the role update reads back (bytes). The copy wraps a
#: spec that passed ``TEMPLATE_SPEC_MAX_BYTES`` -- as MATERIALIZED, so with the
#: bridge's own servers and managed refs merged in -- in an envelope (member,
#: generation, template, version, the card fields) and is written pretty-printed
#: with ``ensure_ascii``; a compact spec near the cap expands several-fold on
#: the way to disk, and a read cap equal to the spec cap would refuse the very
#: file the hire just wrote and report ``pristine_copy_missing`` for a member
#: that has one. Eight times the spec cap covers the indentation of any JSON
#: shape (each nesting level adds at most a handful of bytes per token) plus
#: the envelope, with room for the plumbing.
PRISTINE_COPY_MAX_BYTES = 8 * TEMPLATE_SPEC_MAX_BYTES

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
    #: The template's agent definition as shipped inside the app.
    spec: dict[str, Any] = field(default_factory=dict)
    #: The definition as MATERIALIZED -- the shipped spec after the app bridge
    #: added the app's own MCP servers, the host's managed refs and its policy --
    #: read ONCE at resolve time (and, for a pinned card, verified against the
    #: bridge's rendering). This is the dict the hire copies into the member's
    #: own file (no path is reopened after the checks), so it is the pristine
    #: BASE a role update merges against and the THEIRS it merges in; comparing
    #: the member against the raw shipped spec would read the bridge's plumbing
    #: as the member's own customization.
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


def resolve_template_ref(ref: str) -> StoreTemplate:
    """Resolve a wrapper row's ``template`` (``<app>/<agent name>``) to the listing.

    The row records the DECLARED agent name, not the manifest path the card
    names, so the card is found by resolving each card of the app and matching
    the name it declares; the path's stem is tried first because it is the
    common case and costs no extra read. Raises :class:`TemplateUnavailable`
    with the same codes as :func:`resolve_store_template`, plus
    ``template_not_offered`` when no card of the app declares that name.
    """
    app, sep, agent_name = ref.partition("/")
    if not sep or not agent_name or not isinstance(app, str) or not _SAFE_APP_NAME_RE.match(app):
        raise TemplateUnavailable("template_not_offered", f"{ref!r} does not name a template")
    manifest = get_app_manifest(app)
    if manifest is None:
        raise TemplateUnavailable("app_not_installed", f"App '{app}' is not installed")
    cards = list(manifest.crew.templates)
    cards.sort(key=lambda c: Path(c.agent).stem != agent_name)
    matches: list[StoreTemplate] = []
    # The refusal of the card that NAMES this agent, kept aside: a card whose
    # agent file's stem is the name we resolve is the one the ref points at,
    # and its own verdict (not materialized, unreadable spec, tampered) is the
    # answer when no other card turns out to register under that name --
    # swallowing it would report `template_not_offered` for a template the app
    # does offer, and hide a tampering refusal behind a wrong-name message.
    named_refusal: TemplateUnavailable | None = None
    for card in cards:
        try:
            template = resolve_store_template(app, card.agent)
        except TemplateUnavailable as exc:
            # The app's own verdicts are the answer, whichever card raised
            # them: disabled, banned, or a crew section that fails to
            # validate. Another card's own trouble (unreadable spec, not
            # materialized) is skipped in the search for the named agent.
            if exc.code in ("app_disabled", "app_admission_denied", "template_invalid"):
                raise
            if named_refusal is None and Path(card.agent).stem == agent_name:
                named_refusal = exc
            continue
        if template.agent_name == agent_name:
            matches.append(template)
    if len(matches) > 1:
        # Two cards whose agents register under one name: the manifest
        # validator refuses this at install and at every re-validation, but a
        # ref is resolved from a stored string, so the answer is checked here
        # too rather than picking a card by list order.
        raise TemplateUnavailable(
            "template_ambiguous",
            f"App '{app}' offers more than one template named {agent_name!r}; "
            "the app must give its agents distinct names",
            status=409,
        )
    if matches:
        return matches[0]
    if named_refusal is not None:
        raise named_refusal
    raise TemplateUnavailable(
        "template_not_offered", f"App '{app}' offers no template named {agent_name!r}"
    )


def pristine_copies_root() -> Path:
    return data_home() / PRISTINE_COPIES_DIR_NAME


def pristine_copy_path(member_id: str) -> Path:
    """Absolute path of one member's pristine copy, containment-checked.

    Keyed by the immutable member ID (the config key, inside the agent-name
    grammar, so the filename cannot traverse), never by the lossy slug. Lives
    in the OS-hidden top-level leaf ``PRISTINE_COPIES_DIR_NAME`` describes:
    neither a sandboxed process nor an agent file tool can reach it, the
    gateway opens it directly.
    """
    if not isinstance(member_id, str) or not _AGENT_NAME_RE.match(member_id):
        raise members.MemberSlugError(f"invalid member id {member_id!r}")
    root = pristine_copies_root().resolve()
    target = (root / f"{member_id}.json").resolve()
    if target.parent != root:
        raise members.MemberSlugError(f"member id {member_id!r} escapes {root}")
    return target


def read_pristine_copy(member_id: str, *, generation: str) -> dict[str, Any] | None:
    """The pristine copy as :func:`write_pristine_copy` left it, or ``None``.

    ``None`` when the member has none (hired before templates existed, or from
    a local file), when it cannot be read through the pinned path, when it does
    not have the shape the writer produces, or when it was written for another
    member: the file must name THIS member id and THIS private-store
    *generation* (the store name the create minted, unique per creation), so a
    same-id member deleted and re-hired, or a same-name file on a
    case-insensitive filesystem, never lends its BASE to the wrong member.
    """
    try:
        data = _read_json_capped(
            pristine_copy_path(member_id), PRISTINE_COPY_MAX_BYTES, what="member pristine copy"
        )
    except members.MemberSlugError:
        return None
    if data is None:
        return None
    if data.get("member") != member_id or data.get("generation") != generation:
        return None
    if not isinstance(data.get("template"), str) or not isinstance(data.get("version"), str):
        return None
    if not isinstance(data.get("agent"), dict) or not isinstance(data.get("card"), dict):
        return None
    return data


def remove_pristine_copy(member_id: str) -> None:
    """Remove a member's pristine copy; a missing one is nothing to remove."""
    try:
        pristine_copy_path(member_id).unlink(missing_ok=True)
    except members.MemberSlugError:
        return


#: A field one side does not have at all. Distinct from ``None`` (a key set to
#: JSON null is a value) so "removed the key" and "set it to null" merge apart.
MISSING: Any = object()

#: The card fields a role update merges alongside the agent definition.
CARD_FIELDS = ("role", "triggers")

#: Field-state vocabulary the plan reports and the frontend renders.
UNCHANGED = "unchanged"
APPLY = "apply"  # only THEIRS changed: the update applies it
KEEP = "keep"  # only MINE changed: the member's customization stays
AGREE = "agree"  # both changed to the same value: nothing to decide
CONFLICT = "conflict"  # both changed apart: the user picks


def field_id(field: str) -> str:
    """The stable, opaque handle a plan field is resolved by: ``f`` + 12 hex of
    the field name's SHA-256.

    The field NAME (``spec.<key>``) is member- or template-authored text -- a
    hand-edited agent file can carry a credential-shaped top-level key -- so the
    plan ships it through the same redactor as every other leaf. A redacted
    name is not a name the apply could match a resolution back to, and a
    refusal that echoed the original would leak what the redaction withheld.
    So the client resolves by this id, which is derived from the name but
    carries nothing of it, and the refusal names ids.
    """
    return "f" + hashlib.sha256(field.encode("utf-8", "surrogatepass")).hexdigest()[:12]


@dataclass
class FieldDelta:
    """One mergeable field across the three sides."""

    #: ``spec.<key>`` for an agent-file key, ``card.role`` / ``card.triggers``.
    field: str
    state: str
    base: Any = MISSING
    mine: Any = MISSING
    theirs: Any = MISSING

    @property
    def id(self) -> str:
        return field_id(self.field)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "field": self.field, "state": self.state}
        for side in ("base", "mine", "theirs"):
            value = getattr(self, side)
            if value is not MISSING:
                out[side] = value
        return out


class UnresolvedConflicts(Exception):
    """A merge was asked to apply while conflicting fields had no resolution.

    ``fields`` are the conflicting fields' opaque ids (what the client resolves
    by); ``labels`` their names, for the caller to redact before showing.
    """

    def __init__(self, deltas: list["FieldDelta"]):
        self.fields = [d.id for d in deltas]
        self.labels = [d.field for d in deltas]
        super().__init__(f"unresolved conflicts: {len(deltas)} field(s)")


def json_equal(a: Any, b: Any) -> bool:
    """JSON-value equality: same TYPE and same value, through every level.

    Python's ``==`` is not JSON's: ``True == 1``, ``1 == 1.0`` and ``0 == False``,
    so a member that changed ``"strict": true`` to ``"strict": 1`` would read as
    UNCHANGED, and a template flipping the flag to ``false`` would then be
    APPLIED over the member's edit without a conflict. Here a bool is only
    equal to a bool, a number only to a number of the same kind, and lists
    and objects compare member by member the same way. ``MISSING`` is equal
    only to itself. Iterative (an explicit work stack), not recursive: a
    template or member file is JSON the parser accepted, and ``json.loads``
    accepts nesting far deeper than the interpreter's recursion limit -- a
    recursive compare would raise ``RecursionError`` out of the plan and turn a
    legal file into a 500.
    """
    stack: list[tuple[Any, Any]] = [(a, b)]
    while stack:
        x, y = stack.pop()
        if x is MISSING or y is MISSING:
            if x is not y:
                return False
            continue
        if isinstance(x, bool) or isinstance(y, bool):
            if not (isinstance(x, bool) and isinstance(y, bool) and x == y):
                return False
            continue
        if isinstance(x, (int, float)) or isinstance(y, (int, float)):
            if type(x) is not type(y) or x != y:
                return False
            continue
        if isinstance(x, dict) or isinstance(y, dict):
            if not (isinstance(x, dict) and isinstance(y, dict)) or x.keys() != y.keys():
                return False
            stack.extend((x[k], y[k]) for k in x)
            continue
        if isinstance(x, list) or isinstance(y, list):
            if not (isinstance(x, list) and isinstance(y, list)) or len(x) != len(y):
                return False
            stack.extend(zip(x, y))
            continue
        if type(x) is not type(y) or x != y:
            return False
    return True


def _classify(base: Any, mine: Any, theirs: Any) -> str:
    # JSON equality, not Python's: a type change (``true`` -> ``1``) is a change.
    mine_changed = not json_equal(mine, base)
    theirs_changed = not json_equal(theirs, base)
    if not mine_changed and not theirs_changed:
        return UNCHANGED
    if theirs_changed and not mine_changed:
        return APPLY
    if mine_changed and not theirs_changed:
        return KEEP
    return AGREE if json_equal(mine, theirs) else CONFLICT


def plan_role_update(
    pristine: dict[str, Any],
    mine_spec: dict[str, Any],
    mine_card: dict[str, Any],
    theirs: StoreTemplate,
) -> list[FieldDelta]:
    """Compare BASE (the pristine copy), MINE (the member) and THEIRS (the
    template as installed now) field by field.

    Fields are the union of the agent-definition keys on the three sides --
    THEIRS being the template as MATERIALIZED, the same form the hire copied and
    the pristine copy recorded, so the bridge's plumbing (the app's own MCP
    servers, the host's managed refs) reads as unchanged rather than as the
    member's edit -- except ``name`` -- the member's file declares its own id,
    the template's declares the template's, and neither is anybody's
    customization -- plus
    the card's ``role`` and ``triggers``. Equality is JSON-value equality on
    the whole field: a list of tools that gained one entry is one changed
    field, not a per-entry merge, because a definition is what the author
    reviewed as a whole. Order is stable (spec keys sorted, card last) so the
    plan the client saw is the plan the apply re-derives.
    """
    raw_spec, raw_card = pristine.get("agent"), pristine.get("card")
    base_spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    base_card: dict[str, Any] = raw_card if isinstance(raw_card, dict) else {}
    deltas: list[FieldDelta] = []
    theirs_spec = theirs.materialized_spec
    keys = set(base_spec) | set(mine_spec) | set(theirs_spec)
    keys.discard("name")
    for key in sorted(keys):
        b, m, t = (
            base_spec.get(key, MISSING),
            mine_spec.get(key, MISSING),
            theirs_spec.get(key, MISSING),
        )
        deltas.append(FieldDelta(f"spec.{key}", _classify(b, m, t), b, m, t))
    theirs_card = {"role": theirs.card.role, "triggers": theirs.card.triggers}
    for key in CARD_FIELDS:
        b = base_card.get(key, "")
        m = mine_card.get(key, "")
        t = theirs_card.get(key, "")
        deltas.append(FieldDelta(f"card.{key}", _classify(b, m, t), b, m, t))
    return deltas


def needs_update(deltas: list[FieldDelta]) -> bool:
    """True when applying the plan would change anything on the member."""
    return any(d.state in (APPLY, CONFLICT) for d in deltas)


def merge_role_update(
    mine_spec: dict[str, Any],
    mine_card: dict[str, Any],
    deltas: list[FieldDelta],
    resolutions: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Produce the member's new agent definition and card fields.

    ``resolutions`` maps a conflicting field's opaque ``id`` (:func:`field_id`,
    what the plan ships) to ``"mine"`` or ``"theirs"``; a conflict without one
    raises :class:`UnresolvedConflicts` before anything is decided, so a
    partial resolution never half-applies. A resolution for a field that is
    not in conflict is ignored: the plan, not the client, says what is in
    conflict. The member's ``name`` is never touched, and a field the template
    REMOVED (THEIRS missing) is removed from the member when it applies.
    """
    unresolved = [
        d for d in deltas if d.state == CONFLICT and resolutions.get(d.id) not in ("mine", "theirs")
    ]
    if unresolved:
        raise UnresolvedConflicts(unresolved)
    spec = dict(mine_spec)
    card = {key: str(mine_card.get(key, "") or "") for key in CARD_FIELDS}
    for d in deltas:
        take_theirs = d.state == APPLY or (
            d.state == CONFLICT and resolutions.get(d.id) == "theirs"
        )
        if not take_theirs:
            continue
        kind, _, key = d.field.partition(".")
        if kind == "spec":
            if d.theirs is MISSING:
                spec.pop(key, None)
            else:
                spec[key] = d.theirs
        elif kind == "card":
            card[key] = str(d.theirs or "")
    return spec, card


def _ensure_members_root() -> None:
    members.members_root().mkdir(parents=True, exist_ok=True)


def write_pristine_copy(member_id: str, template: StoreTemplate, *, generation: str) -> Path:
    """Record the unmodified template payload at the installed version.

    The BASE of a later three-way merge: the agent definition as MATERIALIZED
    -- the very dict the hire copied into the member's own file
    (``StoreTemplate.materialized_spec``), never the raw shipped spec -- plus
    the card's mergeable fields (role, triggers), stamped with the member id
    and the private-store *generation* :func:`read_pristine_copy` checks.
    BASE and the copy are the same bytes at the hire, so a shipped definition
    rewritten under an unsigned app whose materialized file was not
    re-rendered (nothing verifies the two agree for an unpinned card: a user's
    edits to an unsigned app's shared template are preserved by design) cannot
    leave a member whose BASE never matched what it started from; and a merge
    that compared the member against the raw shipped spec would read the
    bridge's plumbing (own MCP servers, managed refs, policy) as the member's
    own customization. Published through the pinned writer (the root itself is
    created by name -- it is the gateway's, not agent-named, and normally
    already materialized owner-only before the first sandbox spawn -- and a
    planted link at the final name is refused rather than written through).
    """
    path = pristine_copy_path(member_id)
    payload = {
        "member": member_id,
        "generation": generation,
        "template": template.ref,
        "version": template.version,
        "agent": template.materialized_spec,
        "card": {"role": template.card.role, "triggers": template.card.triggers},
    }
    root = pristine_copies_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(root)
    except OSError:
        logger.debug("could not tighten mode on %s", root, exc_info=True)
    # ``ensure_ascii=True``: a lone surrogate the template's JSON carried
    # (json.loads accepts the escape; ``str.encode`` does not) serializes
    # back to its escape instead of raising ``UnicodeEncodeError`` on the
    # write -- which no caller treats as an I/O failure, so it would escape
    # as a 500 after the apply committed and leave the base behind for good.
    write_file_pinned(path, json.dumps(payload, indent=2), what="member pristine copy")
    return path


def remove_step_four_files(member_id: str, *, pristine: bool, briefing: bool) -> None:
    """Undo what a store hire's step 4 wrote when the step did not complete.

    The pristine copy (id-keyed, in the gateway-only leaf) and the seeded
    briefing (slug-keyed, in the agent-writable member directory): left behind
    by a hire whose enrollment failed (its row is rolled back, so no crewmate
    ever existed), the NEXT member to take the id or the slug -- a rehire, a
    local hire of the same name -- would inherit them as its own base and
    briefing. Only the ones this step wrote go (*pristine* / *briefing*), the
    briefing through the pinned member directory (``unlink_pinned``: a link
    planted at an ancestor is never followed), and only while the caller still
    holds the config lock the step published under, so the files are provably
    still this hire's. Best-effort: a failure is logged, since the caller is
    already unwinding.
    """
    if pristine:
        try:
            remove_pristine_copy(member_id)
        except OSError:
            logger.warning(
                "could not remove the pristine copy of %s on unwind", member_id, exc_info=True
            )
    if briefing:
        try:
            unlink_pinned(
                members.member_briefing_path(members.slug_for_name(member_id)),
                what="member briefing",
            )
        except (OSError, ValueError, members.MemberSlugError):
            logger.warning(
                "could not remove the seeded briefing of %s on unwind", member_id, exc_info=True
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
