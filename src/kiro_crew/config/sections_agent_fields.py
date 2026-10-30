"""Field coercion for the ``agent`` section and the crew records it shares values with.

Model pins and the per-role model and effort maps, the context-token map, approval modes,
the avatar record and the bot-name filter, the jail and ACP-backend readers, the YOLO
duration labels, the completion-keep modes, and the KAS engine bundle url.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
import re as _re
import unicodedata as _unicodedata
from typing import TYPE_CHECKING

from kiro_crew import model_registry
from kiro_crew.acp_backends import resolve_selected_backend
from kiro_crew.appearance_packs import safe_pack_id as _safe_pack_id
from kiro_crew.config.fields import _safe_color
from kiro_crew.config.sections_defaults import DEFAULT_MODEL
from kiro_crew.effort import is_valid_effort
from kiro_crew.terminal_safe import _is_invisible

if TYPE_CHECKING:
    from kiro_crew.config.sections_session import KiroCrewAgentConfig

logger = logging.getLogger("kiro_crew.config.loader")


#: Where ``kirocrew kas install`` looks by default. The bucket the Kiro Crew
#: project publishes its pinned KAS engine builds to; a mirror goes in
#: ``agent.kas_bundle_url``. The manifest's sha256 pins are the trust anchor,
#: not this host.
KAS_BUNDLE_DEFAULT_URL = "https://kas.crew.kiro.dev"


def coerce_kas_bundle_url(value: object) -> str:
    """``agent.kas_bundle_url`` as a usable https base, else the default.

    The installer refuses anything but https itself; coercing here keeps a
    hand-edited plaintext or empty value from reaching it as "the url", so the
    refusal it would produce names a url the operator never typed.
    """
    if isinstance(value, str):
        candidate = value.strip().rstrip("/")
        if candidate.lower().startswith("https://") and len(candidate) > len("https://"):
            return candidate
        if candidate:
            logger.warning(
                "agent.kas_bundle_url %r is not an https:// url; using the default", value
            )
    return KAS_BUNDLE_DEFAULT_URL


def normalize_agent_model(model: object) -> str:
    """Collapse an "inherit" model spelling to ``""``.

    ``""`` (never set) and ``DEFAULT_MODEL`` ("auto") both mean "do not pin a
    model here, defer to the next tier down". Callers store and compare the
    single ``""`` spelling so a tier set to "auto" keeps inheriting instead of
    hard-pinning the backend's own default and shadowing the tier below it.

    Total on purpose: this is the chokepoint for values that arrive from
    hand-edited config and from request bodies, so a non-string is treated as
    "no pin" rather than raising out of a resolver.
    """
    if not isinstance(model, str):
        return ""
    m = model.strip()
    return "" if m == DEFAULT_MODEL else m


# Per-task-class model overrides (agent.role_models). These are the ONLY
# sanctioned place to pin a model for a class of work — never hardcode a model
# id in code. Every role defaults to "" ("inherit"), which resolves down to
# agent.model and finally to DEFAULT_MODEL ("auto"), so an unpinned role is
# entitlement-safe on every subscription tier (the provider picks a served
# model). An operator who deliberately wants a cheaper model for background /
# sub-agent work pins it here without changing the interactive chat default.
ROLE_MODEL_KEYS: tuple[str, ...] = ("background", "subagent")

# The kiro agents that run the "background" role: auto-titles, memory
# consolidation, heartbeat polls. Named here rather than inline at the one place
# that branched on them, because the effort chain is now read by two callers (the
# provider factory and the crews API's readout) and a second copy of the pair
# would let them disagree about which agents take the role default.
BACKGROUND_WORKER_AGENTS: tuple[str, ...] = ("kirocrew-lite", "kirocrew-heartbeat")


def coerce_role_models(raw: object) -> dict[str, str]:
    """Normalize the per-role model map from hand-edited config / request bodies.

    Only the known :data:`ROLE_MODEL_KEYS` are kept; each value passes through
    :func:`normalize_agent_model`, so an ``"auto"`` or non-string entry collapses
    to ``""`` ("inherit the next tier down"). Empty results are dropped so the
    stored map only ever carries real pins — a role absent from the map and a
    role explicitly set to ``"auto"`` behave identically (both inherit).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for role in ROLE_MODEL_KEYS:
        val = normalize_agent_model(raw.get(role))
        if val:
            out[role] = val
    return out


def coerce_role_efforts(raw: object) -> dict[str, str]:
    """Normalize the per-role reasoning-effort map (agent.role_efforts).

    Same role keys as :data:`ROLE_MODEL_KEYS`. Each value must be a concrete,
    valid effort level; ``""`` / an invalid / non-string entry is dropped so the
    stored map carries only real pins — an absent role and an empty one both
    mean "inherit the chat default effort, then the provider/model default".
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for role in ROLE_MODEL_KEYS:
        val = raw.get(role)
        if isinstance(val, str) and val.strip() and is_valid_effort(val.strip()):
            out[role] = val.strip()
    return out


# agent.model_context_tokens: the context window an operator says an exact model id is
# served with. The value bounds keep a typo from declaring a nonsense window; the entry cap
# bounds the map a hand-edited file can make every process carry.
MODEL_CONTEXT_TOKENS_MIN = 1_024
MODEL_CONTEXT_TOKENS_MAX = 10_000_000
MODEL_CONTEXT_TOKENS_MAX_ENTRIES = 64


def coerce_model_context_tokens(raw: object) -> dict[str, int]:
    """Normalize ``agent.model_context_tokens`` from hand-edited config / request bodies.

    An entry survives when its key is a model id (non-empty, no surrounding whitespace,
    at most :data:`model_registry.ADVERTISED_MODEL_ID_MAX_CHARS` characters) and its value
    is a real ``int`` (not a bool, float or string) from :data:`MODEL_CONTEXT_TOKENS_MIN`
    to :data:`MODEL_CONTEXT_TOKENS_MAX`. A key is taken as written: never stripped, folded
    or read as a path. Only the first :data:`MODEL_CONTEXT_TOKENS_MAX_ENTRIES` entries are
    read. Every entry that is dropped is named in a warning (the key only, quoted
    truncated), so a typo costs that one declaration and never goes unseen.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("agent.model_context_tokens is not a mapping; ignoring it")
        return {}
    if len(raw) > MODEL_CONTEXT_TOKENS_MAX_ENTRIES:
        logger.warning(
            "agent.model_context_tokens: ignoring %d entries past the first %d",
            len(raw) - MODEL_CONTEXT_TOKENS_MAX_ENTRIES,
            MODEL_CONTEXT_TOKENS_MAX_ENTRIES,
        )
    out: dict[str, int] = {}
    for index, (key, value) in enumerate(raw.items()):
        if index >= MODEL_CONTEXT_TOKENS_MAX_ENTRIES:
            break
        if not (
            isinstance(key, str)
            and key
            and key == key.strip()
            and len(key) <= model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
        ):
            logger.warning(
                "agent.model_context_tokens: ignoring %.80r, a key must be a model id of "
                "1 to %d characters with no surrounding whitespace",
                key,
                model_registry.ADVERTISED_MODEL_ID_MAX_CHARS,
            )
        elif (
            not isinstance(value, int)
            or not MODEL_CONTEXT_TOKENS_MIN <= value <= MODEL_CONTEXT_TOKENS_MAX
        ):
            logger.warning(
                "agent.model_context_tokens: ignoring %.80r, the window must be an "
                "integer from %d to %d",
                key,
                MODEL_CONTEXT_TOKENS_MIN,
                MODEL_CONTEXT_TOKENS_MAX,
            )
        else:
            out[key] = value
    return out


def coerce_effort(raw: object) -> str:
    """Normalize ONE reasoning-effort value to a level, or ``""`` for inherit.

    The single-value counterpart of :func:`coerce_role_efforts`, for the crew
    pin (``agents.<name>.reasoning_effort``). ``config.json`` is hand-editable,
    so anything that is not a concrete level collapses to ``""`` — inherit the
    tier below — rather than reaching the provider as a level kiro-cli would
    reject. The API validates instead of coercing, so a caller that sends a
    typo is told; only the file-load path silently falls back.
    """
    if isinstance(raw, str):
        val = raw.strip()
        if val and is_valid_effort(val):
            return val
    return ""


def worker_session_defaults(crew: "KiroCrewAgentConfig") -> tuple[str, str]:
    """``(model, reasoning_effort)`` a session this crewmate dispatches starts on.

    The pair ``session_create`` falls back to when the call names neither: the
    crewmate's ``worker_model`` / ``worker_reasoning_effort`` when set, else its
    explicit ``model`` / ``reasoning_effort`` pins. An empty crewmate pin leaves
    that half unpinned, so the child keeps its own agent's or the global default,
    exactly as a create that named nothing did before the pair existed.

    ``worker_model`` is read RAW: ``"auto"`` is a real pin there (workers on the
    backend default while the crewmate stays on a costly model), where the
    crewmate's own ``model`` collapses it to inherit.

    The crewmate's own effort carries over ONLY while the workers run the
    crewmate's own model. With ``worker_model`` set, the level was chosen for a
    different model: carrying a ``max`` picked for an opus conductor onto an
    ``auto`` or haiku worker would pin a level the profile card shows no row
    for, reaching the provider where the person cannot see or clear it. There
    the only effort a worker takes is ``worker_reasoning_effort``.
    """
    worker_model = crew.worker_model.strip()
    worker_effort = coerce_effort(crew.worker_reasoning_effort)
    if worker_model:
        return worker_model, worker_effort
    return normalize_agent_model(crew.model), worker_effort or coerce_effort(crew.reasoning_effort)


#: The approval modes a crewmate's own record may pin. ``yolo`` is absent on
#: purpose: it is process-global, never a property of one crewmate.
MEMBER_APPROVAL_MODES = ("normal", "trust_reads", "trust")

#: The mode a crewmate's thread opens in while its record pins none (``""``).
#: Every reader of an unset ``approval_mode`` resolves through this: the thread
#: seed, the profile's picker, and a change card's "before" column.
MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE = "trust"


def coerce_member_approval_mode(raw: object) -> str:
    """Normalize ``agents.<name>.approval_mode``: a known mode, else ``""`` (unset)."""
    return raw if isinstance(raw, str) and raw in MEMBER_APPROVAL_MODES else ""


def coerce_refusal_fallback_model(raw: object) -> str:
    """Normalize the content-filter fallback model (agent.refusal_fallback_model).

    Same three shapes as :func:`coerce_fallback_model` but with the OPPOSITE
    junk default: ``""`` (the default) disables the feature — a refusal then
    surfaces exactly as it does today — so absent/junk input (``None``,
    non-string) and an id the registry maps to ``""`` all collapse to ``""``
    (off), never to a silently-enabled value. ``"auto"`` means "retry on the
    model the provider's refusal envelope recommends, when it names one"; a
    concrete id is normalized through :func:`model_registry.to_provider_id`
    for the ``acp`` provider.
    """
    if raw is None or not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    if s.lower() == "auto":
        return "auto"
    return model_registry.to_provider_id(s, "acp") or ""


#: String-valued ghost trait axes accepted in a per-crew avatar override.
_AVATAR_GHOST_STR_TRAITS = ("eyes", "brows", "mouth", "accessory", "prop")
#: Boolean-valued ghost trait axes.
_AVATAR_GHOST_BOOL_TRAITS = ("blush", "flip")
#: Cap on a single trait value, so hand-written junk cannot bloat config.json.
_AVATAR_TRAIT_MAX_LEN = 32
#: Formats an uploaded crew picture may be stored in. Shared with the avatar
#: endpoints: the config's ``file`` pin and the files on disk speak this set.
_AVATAR_IMAGE_EXTS = ("png", "jpg", "webp")
#: The config's committed-picture pin: ``<16-hex content digest>.<ext>``.
#: Each install lands at a digest-named path that never collides with the
#: currently committed file, so nothing overwrites a committed picture before
#: the config save that commits its replacement.
_AVATAR_FILE_PIN_RE = _re.compile(r"^[0-9a-f]{16}\.(?:png|jpg|webp)$")
#: Agent lifecycle states a crew face may react to. A per-state override keys
#: on one of these exactly; any other key is dropped, so a version-skewed or
#: typo'd state name cannot smuggle an unbounded key set into config.json.
_AVATAR_STATES = ("working", "done", "error")
#: Built-in reaction motions a GHOST may play, per state. The vocabulary is
#: pinned here rather than left open like a trait value because a motion is a
#: named animation the frontend implements: an unknown name has no rendering to
#: resolve to, so it carries nothing and is dropped. ``"none"`` is a real value
#: (explicit stillness), distinct from an absent state, so one state can opt out
#: of a motion the others use. ``working`` has no entry -- the ghost's working
#: animation is its idle breathing, and a reaction fires on a transition.
_AVATAR_MOTIONS: dict[str, tuple[str, ...]] = {
    "done": ("none", "bounce", "nod", "sparkle"),
    "error": ("none", "shake", "cross-eyes", "droop"),
}
#: Preset cue names a per-state sound may select. `"none"` is a real value
#: (explicit silence), distinct from an absent state (the default, also
#: silent) -- so a crew can opt one state out of a fleet-wide cue.
_AVATAR_SOUNDS = ("none", "chime", "ding", "blip", "pop", "pulse")

#: Shape a stored icon ``pose`` must match: ASCII letters, digits, dash and
#: underscore. Deliberately NOT a membership test against the shipped pose set --
#: an unknown-but-well-formed pose a newer client wrote is kept so an unrelated
#: save does not wipe it (the client resolves it to the default). The bound is
#: applied separately (``_AVATAR_TRAIT_MAX_LEN``); this only rejects a value that
#: could bloat config.json with junk or reach the client's SVG template as markup.
_AVATAR_POSE_RE = _re.compile(r"[A-Za-z0-9_-]+")


#: The only trait axes a per-state ghost expression may move. The identity axes
#: (brows/accessory/prop/tile/blush/flip) are excluded: a crew must stay
#: recognisable as itself while its expression changes.
_AVATAR_EXPRESSION_AXES = ("eyes", "mouth")


def _safe_expressions(value: object) -> dict:
    """Return validated per-state ghost eyes/mouth picks, or ``{}``.

    The key ``motions`` supersedes, kept round-tripping for exactly as long as the
    shipped renderer still draws it: the frontend writes and paints a ghost's
    per-state eyes/mouth today, so a validator that dropped the key would erase a
    visible choice on an unrelated save with no way back. The sibling frontend
    change that removes the picker is where this retires. Same forgiveness as the
    trait coercer -- junk is dropped, never refused.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for state in _AVATAR_STATES:
        raw = value.get(state)
        if not isinstance(raw, dict):
            continue
        axes = {}
        for axis in _AVATAR_EXPRESSION_AXES:
            v = raw.get(axis)
            if isinstance(v, str) and v:
                axes[axis] = v[:_AVATAR_TRAIT_MAX_LEN]
        if axes:
            out[state] = axes
    return out


def _safe_motions(value: object) -> dict:
    """Return validated per-state ghost motions, or ``{}``.

    Same forgiveness as the trait coercer: junk is dropped silently rather than
    refused, because config.json is hand-editable and a malformed motion must
    never cost the crew its otherwise-valid avatar. Only the states
    :data:`_AVATAR_MOTIONS` names carry a motion, and only a value from that
    state's own tuple survives -- ``{"done": "shake"}`` is dropped, because
    ``shake`` is the error vocabulary and a bounce-on-error is a different
    reaction than the author wrote.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for state, allowed in _AVATAR_MOTIONS.items():
        v = value.get(state)
        if isinstance(v, str) and v in allowed:
            out[state] = v
    return out


def _safe_sounds(value: object) -> dict:
    """Return validated per-state sound cues, or ``{}``.

    Unlike a trait value, a cue name IS pinned to a vocabulary: it selects a
    shipped preset rather than naming an option the renderer can resolve to
    absent, so an unknown name has no meaning to carry and is dropped.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for state in _AVATAR_STATES:
        v = value.get(state)
        if isinstance(v, str) and v in _AVATAR_SOUNDS:
            out[state] = v
    return out


def safe_imported(value: object) -> dict:
    """A crew's provenance stamp from a ``.crewmate`` import, or ``{}`` on junk.

    ``{"format_version": int, "sender_label": str, "sha256": str, "imported_at": str}``
    (rfc-crewmate-share.md §5.4 step 5). A stamp, not a parent: nothing reads it to
    reach the sender. The label is self-declared and capped like the card's.
    """
    if not isinstance(value, dict) or set(value) != {
        "format_version",
        "sender_label",
        "sha256",
        "imported_at",
    }:
        return {}
    version, label = value["format_version"], value["sender_label"]
    sha, at = value["sha256"], value["imported_at"]
    if type(version) is not int or not 1 <= version <= 1000:
        return {}
    if not isinstance(label, str) or len(label) > 64:
        return {}
    if not isinstance(sha, str) or not _re.fullmatch(r"(?:[0-9a-f]{64})?", sha):
        return {}
    if not isinstance(at, str) or not _re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", at):
        return {}
    return {"format_version": version, "sender_label": label, "sha256": sha, "imported_at": at}


def _safe_avatar(value: object) -> dict:
    """Return a validated per-crew avatar override, or ``{}`` on junk.

    Each tier owns its own source of motion and sound, so what a record may
    carry depends on its ``kind``:

    - ``{"kind": "ghost", "traits"?: {...}, "motions"?: {...}, "sounds"?: {...},
      "expressions"?: {...}}`` — the built-in face. ``traits`` pins it trait-by-trait instead of deriving
      it from the crew name, and may be absent (or empty) when the override
      carries only reactions: that spelling means "name-derived face, plus these
      per-state reactions", and the record omits the key entirely rather than
      storing ``{}``. ``motions`` picks a built-in reaction animation per state
      from :data:`_AVATAR_MOTIONS`; ``sounds`` picks a synthesized preset cue per
      state from :data:`_AVATAR_SOUNDS`; ``expressions`` (a per-state eyes/mouth
      pick, which ``motions`` supersedes) still round-trips because the shipped
      renderer still draws it -- see :func:`_safe_expressions`.
    - ``{"kind": "image"}`` (optional int ``v``, optional ``file``, optional
      ``sounds``) — the crew wears an uploaded picture, served from
      ``GET /api/agents/{name}/avatar``. A picture has no animation to play, so it
      carries no ``motions``; it does still carry a cue, because the shipped
      renderer plays a crew's cue whatever face it wears, and dropping the key
      here would silence a crew on an unrelated save with no way to restore it.
      The file itself lives under the data home's agent-fenced ``run/avatars/``
      dir; the config field only marks the choice. ``v`` is the upload's cache-busting stamp (file
      mtime, nanoseconds): the frontend appends it as ``?v=`` so a replaced
      picture is re-fetched without waiting out the browser cache. ``file`` pins
      the exact committed file — a ``<digest>.<ext>`` suffix under the crew's
      stem. Every install lands at a digest-named path, so a replacement never
      overwrites the committed file before the config save commits it, and
      serving resolves only the pinned file.
    - ``{"kind": "pack", "id": "<pack id>"}`` — the crew wears an appearance
      pack from the crew library (``GET /api/appearances``). ``id`` is
      validated by :func:`kiro_crew.appearance_packs.safe_pack_id`, the same
      rule the pack store applies to a directory name, so a value stored here
      can always be looked up. A junk id collapses the WHOLE override to
      ``{}``: an unrenderable pack reference is worse than the default face.
      Whether the pack still EXISTS is deliberately not checked — config load
      must not touch the disk — so a dangling id renders as the name-derived
      ghost on the client. A pack carries its own per-state art
      (``GET /api/appearances/{id}/slot/{slot}``) and its own per-state audio
      (``GET /api/appearances/{id}/sound/{state}``), so it needs no ``motions``
      here. It keeps accepting ``sounds`` for now, for the same reason the picture
      tier does: the shipped renderer plays a crew-record cue on every tier, so
      retiring the key before that renderer stops reading it would take away a
      sound the user chose. Once the pack's own audio is what plays, the crew
      record has no cue to hold and the key retires with that change.

    ``<state>`` is one of ``working``, ``done``, ``error``. A key is omitted
    from the record when validation leaves it empty, so a stored avatar never
    carries ``{}`` for one, and a key illegal on this tier is DROPPED rather
    than refused — the same forgiveness every other field here has, because a
    hand-written or version-skewed record must not cost the crew its face. Two
    keys are deliberately NOT retired that way even though ``motions`` supersedes
    one of them: ``sounds`` is audible on every tier today, and a ghost's
    ``expressions`` is drawn today, and a value a user can see or hear is not
    dropped ahead of the renderer that shows it. ``expressions`` round-trips on
    picture and pack too, even though neither has a face to move: the shipped
    builder still SUBMITS it there, and a crew that switches from ghost to
    picture and back would otherwise lose the picks in between. Only ``motions``
    is tier-gated, because nothing has ever written it outside the ghost.

    Empty means "no override" — the frontend keeps rendering the name-seeded
    face. config.json is hand-editable (and agent-writable), so junk collapses
    to ``{}`` rather than crashing the load.

    Trait *values* are deliberately not checked against the frontend's trait
    vocabulary: the renderer resolves an unknown option to "absent"
    (``EYES[k] ?? ''``), and keeping the vocabulary in one place (the style
    module) means a new hat needs no backend release. ``tile`` is the one
    exception — it is interpolated into SVG markup, so it is pinned to a hex
    color by the same validator session_color uses. A motion or cue name is
    pinned, because unlike a trait it names an animation or a synthesizer
    preset rather than an option a renderer can resolve to nothing.
    """
    if not isinstance(value, dict):
        return {}
    # Legal on every tier: the shipped renderer reads a crew-record cue
    # kind-agnostically, so this is the one reaction key that is not the ghost's
    # alone. `motions` IS ghost-only -- a picture has no face to move and a pack
    # animates from its own files.
    sounds = _safe_sounds(value.get("sounds"))
    expressions = _safe_expressions(value.get("expressions"))
    if value.get("kind") == "image":
        out: dict[str, object] = {"kind": "image"}
        v = value.get("v")
        # bool is an int subclass; a hand-written `"v": true` must not pass.
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            out["v"] = v
        f = value.get("file")
        if isinstance(f, str) and _AVATAR_FILE_PIN_RE.fullmatch(f):
            out["file"] = f
        if expressions:
            out["expressions"] = expressions
        if sounds:
            out["sounds"] = sounds
        return out
    if value.get("kind") == "icon":
        # The ICON tier: a shipped pose over a solid background. The whole
        # override is `pose` + `bg` -- no face to move and no cue of its own, so
        # it carries none of the reaction keys (a static drawing like a picture).
        #
        # `pose` is a bounded, charset-safe string rather than a value checked
        # against the shipped pose set: like a ghost trait, an unknown value is
        # the renderer's to resolve (it falls back to the default pose), so
        # pinning it here would drop a pose a newer client wrote and wipe the
        # face on the next unrelated save. The bound and charset exist only so a
        # hand-written junk value cannot bloat config.json or reach a template as
        # markup. `bg` is pinned to a hex colour by the same validator `tile`
        # uses, because it IS interpolated into SVG markup on the client.
        pose = value.get("pose")
        if not isinstance(pose, str) or not pose:
            # A pose is the whole of an icon avatar's identity -- an override that
            # names none has nothing to draw, so it collapses like a pack with no
            # id rather than storing a faceless `{"kind": "icon"}`.
            return {}
        safe_pose = pose[:_AVATAR_TRAIT_MAX_LEN]
        if _AVATAR_POSE_RE.fullmatch(safe_pose) is None:
            return {}
        bg = _safe_color(value.get("bg", ""))
        icon: dict[str, object] = {"kind": "icon", "pose": safe_pose}
        if bg:
            icon["bg"] = bg
        return icon
    if value.get("kind") == "pack":
        ident = _safe_pack_id(value.get("id"))
        if ident is None:
            # No canonical "pack with no id" spelling exists: a pack avatar IS
            # its id, so a missing or malformed one leaves nothing to render.
            return {}
        pack: dict[str, object] = {"kind": "pack", "id": ident}
        if expressions:
            pack["expressions"] = expressions
        if sounds:
            pack["sounds"] = sounds
        return pack
    if value.get("kind") != "ghost":
        return {}
    raw = value.get("traits")
    traits: dict[str, object] = {}
    if isinstance(raw, dict):
        for key in _AVATAR_GHOST_STR_TRAITS:
            v = raw.get(key, "")
            traits[key] = v[:_AVATAR_TRAIT_MAX_LEN] if isinstance(v, str) else ""
        for key in _AVATAR_GHOST_BOOL_TRAITS:
            # `is True`, not bool(): config.json is hand-editable and
            # bool("false") is True, so a string-typed value would render the
            # opposite of what its author wrote. Only a real boolean counts.
            traits[key] = raw.get(key, False) is True
        traits["tile"] = _safe_color(raw.get("tile", ""))
        # An all-empty trait set (every axis absent) is indistinguishable in
        # intent from "no override" but would render a featureless ghost. The
        # builder cannot produce it (Apply always carries the seeded
        # defaults), so it only arrives via hand-written config or direct API
        # use — drop it to the one canonical "no traits" spelling instead of
        # storing a third state.
        if all(not v for v in traits.values()):
            traits = {}
    ghost: dict[str, object] = {"kind": "ghost"}
    if traits:
        ghost["traits"] = traits
    motions = _safe_motions(value.get("motions"))
    if motions:
        ghost["motions"] = motions
    if expressions:
        ghost["expressions"] = expressions
    if sounds:
        ghost["sounds"] = sounds
    # `kind` alone carries no override — a bare ghost is the name-derived face,
    # which is what an absent field already means.
    if len(ghost) == 1:
        return {}
    return ghost


_BOT_NAME_MAX = 50

#: Unicode general categories a bot name is made of: letters, the marks that
#: attach to them, and digits, in any script. This is the character policy of
#: the artifact tag rule (``artifact_store.rules.normalize_tag``), applied as a
#: filter that drops instead of a validator that refuses: a name is a display
#: label, never a file name or a key. Nonspacing and spacing marks (``Mn``,
#: ``Mc``) are the combining characters NFC leaves standing beside their base
#: (Devanagari vowel signs, Thai tone marks, Arabic harakat); without them whole
#: scripts could not be written. Letter numbers (``Nl``: IDEOGRAPHIC NUMBER
#: ZERO, the Roman numerals) are admitted with the decimal digits, as the tag
#: rule admits them. Enclosing marks (``Me``: the keycap that turns ``1`` into an
#: emoji, the enclosing circle) are not admitted, as the tag rule refuses them:
#: they are symbols by another route. Neither are other numbers (``No``: VULGAR
#: FRACTION ONE HALF, SUPERSCRIPT TWO, CIRCLED DIGIT ONE), which are symbols
#: drawn from a digit. Everything else is dropped: punctuation other than the
#: separators below, symbols and emoji (which the tag rule refuses too), and
#: every control (``Cc``), format (``Cf``: the zero-width characters and the
#: bidi controls), line and paragraph separator, surrogate, private-use and
#: unassigned code point, plus the letters and marks that render as nothing
#: (``terminal_safe._is_invisible``: the variation selectors, the Hangul
#: fillers), which the category test alone would keep as an invisible residue.
#: The tag rule's helper is not imported: it lives in ``artifact_store.rules``,
#: which reaches ``kiro_crew.deploy`` and through it this package, so the
#: category test is written here against the stdlib. Three tag rules are not
#: applied, each for the reason a filter differs from a validator or a chip
#: differs from a prompt: a compatibility form (full-width letters) is kept as
#: typed because a drop cannot name the plain spelling the rule asks for; a name
#: may open with a separator; and a run of combining marks is not bounded, since
#: the bound guards a chip's drawn width and nothing draws this name, which is
#: only substituted into the prompt.
_BOT_NAME_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc", "Nd", "Nl"})

#: The ASCII separators a name may carry.
_BOT_NAME_SEPARATORS = frozenset(" _-.")


def _bot_name_char(ch: str) -> str:
    """The character ``ch`` contributes to a bot name: itself, a space, or nothing.

    A brace is nothing: the ``{bot_name}`` placeholder must not recurse. The
    category test already drops it as punctuation; naming it here keeps that
    rule visible beside the one it protects.
    """
    if ch in _BOT_NAME_SEPARATORS:
        return ch
    if ch in "{}" or _is_invisible(ch):
        return ""
    category = _unicodedata.category(ch)
    if category in _BOT_NAME_CATEGORIES:
        return ch
    # A space separator that is not the ASCII space (NO-BREAK SPACE, the
    # IDEOGRAPHIC SPACE a Japanese keyboard types between words) keeps the words
    # it separated apart instead of joining them. NFKC maps these to the plain
    # space; the name is NFC-normalized, so the one mapping is made here.
    if category == "Zs":
        return " "
    return ""


def _sanitize_bot_name(raw: str) -> str:
    """Sanitize bot_name: NFC-normalize, cap the length, drop braces and non-name characters.

    A name keeps the letters, combining marks and digits of any script
    (``_BOT_NAME_CATEGORIES``) plus the ASCII space, ``_``, ``-`` and ``.``, and
    is stored in its NFC spelling so ``Jos\u00e9`` typed with a precomposed or a
    combining accent loads as one string. Everything else is dropped: braces
    (the ``{bot_name}`` placeholder must not recurse), markdown and other
    punctuation, symbols and emoji, and every control, format, invisible,
    surrogate and private-use character. A space run that a removal leaves
    behind folds to one space and a leading or trailing one is stripped; a double
    space the user typed is kept. The cap is :data:`_BOT_NAME_MAX` code points of
    the NFC form, applied before the filter.
    """
    if not isinstance(raw, str):
        return ""
    name = _unicodedata.normalize("NFC", raw).strip()[:_BOT_NAME_MAX]
    kept: list[str] = []
    removed_since_last_kept = False
    for ch in name:
        out = _bot_name_char(ch)
        if not out:
            removed_since_last_kept = True
            continue
        if out == " " and removed_since_last_kept and kept and kept[-1] == " ":
            # The removal left two spaces touching: the name keeps one.
            continue
        kept.append(out)
        removed_since_last_kept = False
    # A removal can leave a base letter and a combining mark adjacent that the
    # input kept apart (``e`` + ZERO WIDTH SPACE + COMBINING ACUTE ACCENT), and
    # NFC composes such a pair, so the stored spelling is normalized again on
    # the way out. Composition only shortens, so the cap still holds.
    return _unicodedata.normalize("NFC", "".join(kept).strip())


# Process-isolation jail modes (``agent.jail``).  Single source of truth shared by
# ``_normalize_jail``, the ``AgentConfig.jail`` field metadata enum, and tests —
# a new mode added in one place can't silently normalize back to the default.
JAIL_MODE_AUTO = "auto"
JAIL_MODE_ON = "on"
JAIL_MODE_OFF = "off"
_VALID_JAIL_MODES = (JAIL_MODE_AUTO, JAIL_MODE_ON, JAIL_MODE_OFF)

# Standard work-tree roots for ``agent.subagent_cwd_allowed_roots``.  Single
# source of truth shared by the field default and the fallback in ``from_dict``.
# Both use the same four roots.  The fallback is the value real configs get:
# ``from_dict`` always passes an explicit value and an absent key reaches the
# same branch as a malformed one.  Four is what the product ships; narrowing to
# two would revoke ~/workspaces and ~/workplaces from every config that omits
# the field.
DEFAULT_CWD_ALLOWED_ROOTS = [
    "~/workspace",
    "~/workspaces",
    "~/workplace",
    "~/workplaces",
]

_VALID_COMPLETION_KEEP = ("head", "tail", "both")


def _validated_completion_keep(value: object) -> str:
    """Return *value* if it is one of head/tail/both, else raise ValueError."""
    if isinstance(value, str) and value in _VALID_COMPLETION_KEEP:
        return value
    raise ValueError(
        f"agent.completion_keep must be one of {list(_VALID_COMPLETION_KEEP)}, " f"got {value!r}"
    )


_YOLO_DURATION_SECS: dict[str, int] = {
    "30m": 1800,
    "1h": 3600,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
}

# Not a timed value: an ad-hoc grant that stays on with no expiry until the
# gateway process stops. In-memory only, so it cannot survive a restart.
YOLO_UNTIL_SHUTDOWN = "until_shutdown"


def _normalize_yolo_duration(value: object, default: str) -> str:
    """Coerce ``agent.yolo_duration`` to a supported ad-hoc duration label.

    Anything unrecognised (typo, removed value, wrong type) falls back to
    *default*, the field's, rather than failing the whole config load — the value
    only widens or narrows an already-bounded ad-hoc grant, and the 24h ceiling on
    timed values is enforced independently in ``SafetyOverride``.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _YOLO_DURATION_SECS or v == YOLO_UNTIL_SHUTDOWN:
            return v
    return default


def _normalize_jail(value: object) -> str:
    """Coerce a persisted ``agent.jail`` value to a valid mode, deny-by-default.

    Valid persisted modes are ``auto`` / ``on`` / ``off``.  An unknown or
    non-string value normalizes to ``auto`` (the safe default — let the active
    edition decide; the public edition's jail provider is a no-op regardless).
    ``off`` per-invocation is expressed via ``--no-jail`` / ``KIROCREW_NO_JAIL``,
    not persisted config.
    """
    if isinstance(value, str) and value in _VALID_JAIL_MODES:
        return value
    return JAIL_MODE_AUTO


def _normalize_acp_backend(value: object) -> str:
    """Coerce a persisted ``agent.acp_backend`` to a backend this build can serve.

    Delegates to :func:`kiro_crew.acp_backends.resolve_selected_backend`, which owns
    the selectable registry, so the load path, the dashboard PATCH allowlist and the
    schema endpoint cannot disagree about which harnesses exist.

    The import is at module scope rather than deferred: ``acp_backends`` is a leaf
    that imports nothing from ``kiro_crew.acp``, so it does not reproduce the
    package-init cycle (``kiro_crew.acp.__init__`` -> client + runtime -> this
    module) that the old local import of ``acp.types`` existed to dodge.
    """
    return resolve_selected_backend(value)
