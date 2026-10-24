"""Fail-closed KiroCrew 0.10 semantic integration for Kiro BYO.

KiroCrew 0.10 keeps the ACP/runtime registries used by the 0.9 adapter, but
split the dashboard chat handlers into composed owner modules.  Reasoning-effort
and selection-capability handlers now live in ``dashboard/chat_api/slot_effort``
while ``dashboard/chat_handlers`` remains the composed runtime namespace.

The 0.10 transform therefore reuses the proven 0.9 ACP/backend patches, patches
the owner implementation in ``slot_effort.py``, and explicitly imports the BYO
helper functions into ``chat_handlers.py`` so KiroCrew's ``chat_api.compose``
rebinding keeps every global lookup valid.  Unknown source shapes still fail
closed before any integration write.
"""
from __future__ import annotations

from dataclasses import dataclass
import ast

from . import crew09_patch as legacy
from . import crew_patch_common as common

PatchError = common.PatchError

TARGET_VERSION = "0.10.0 adaptive registry/capabilities (composed chat API owners)"
_SLOT_EFFORT = "src/kiro_crew/dashboard/chat_api/slot_effort.py"
_CHAT_HANDLERS = "src/kiro_crew/dashboard/chat_handlers.py"
_CHAT_TITLE = "src/kiro_crew/dashboard/chat_title.py"

# Every 0.9 seam except chat_handlers remains semantically valid on the audited
# 0.10 source.  0.10 adds the slot_effort owner and keeps chat_handlers as the
# namespace/patch surface that chat_api.compose rebinds owner functions onto.
_COMMON_TARGET_FILES = tuple(rel for rel in legacy.TARGET_FILES if rel != _CHAT_HANDLERS)
TARGET_FILES: tuple[str, ...] = (
    *tuple(rel for rel in _COMMON_TARGET_FILES if rel != "src/kiro_crew/dashboard/chat_runner.py"),
    _SLOT_EFFORT,
    _CHAT_HANDLERS,
    _CHAT_TITLE,
    "src/kiro_crew/dashboard/chat_runner.py",
)

_HELPERS = (
    "_kiro_byo_cold_selection_capabilities",
    "_kiro_byo_type_selection_active",
    "_kiro_byo_reconcile_slot_routing",
)
_BACKEND_IMPORT = "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO"
_SLOT_EFFORT_MODULE = "kiro_crew.dashboard.chat_api.slot_effort"


def _patch_chat_handlers_namespace(text: str) -> str:
    """Expose BYO owner helpers in the namespace used by chat_api.compose."""
    tree = common._parse(text)
    compose_call = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compose"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_chat_api"
        ),
        None,
    )
    if compose_call is None or not any(
        isinstance(node, ast.Name) and node.id == "_owner_slot_effort"
        for node in ast.walk(compose_call)
    ):
        raise PatchError("Crew 0.10 chat_api composition seam moved")

    if _BACKEND_IMPORT not in text:
        span, node = common._import_span(text, "kiro_crew.agent_sdk.backends")
        if node.end_lineno is None:
            raise PatchError("Crew 0.10 backend import has no source span")
        text = common._insert_after_line(text, node.end_lineno, _BACKEND_IMPORT)

    for helper in _HELPERS:
        text = common._add_import_name(text, _SLOT_EFFORT_MODULE, helper)
    return text


_KIRO_BYO_SLOT_RECONCILE_HELPER = r'''

async def _kiro_byo_reconcile_slot_routing(state: DashboardState, slot: _ChatSlot | None = None) -> bool:
    """Normalize routing-only model state across a harness transition.

    KiroCrew stores Auto (Jev) as ``slot.model == "auto"`` plus
    ``slot.jev_route == True``. Kiro BYO Type selections are a different
    routing namespace. If the configured backend changes while an existing
    conversation stays open, leaving the old routing flag in place makes the
    composer keep displaying Jev even though the BYO catalogue is already
    active.

    Reconcile only routing sentinels. Explicit concrete-model pins are never
    overwritten. The operation is idempotent and broadcasts only when a slot
    really changes.
    """
    targets = [slot] if slot is not None else list(state._slots.values())
    changed = False
    _byo_default: str | None = None

    async def _default_type() -> str:
        nonlocal _byo_default
        if _byo_default is not None:
            return _byo_default
        _byo_default = ""
        try:
            from kiro_byo.config import load_catalog_config as _load_kiro_byo_catalog
            from kiro_byo.paths import use_app_home as _kiro_byo_use_app_home
            from kiro_byo.protocol import type_selection_id as _kiro_byo_type_selection_id
            from kiro_crew.apps.manager import app_data_dir as _kirocrew_app_data_dir

            _catalog_path = _kirocrew_app_data_dir("kiro-byo") / "config" / "providers.json"
            with _kiro_byo_use_app_home(_catalog_path.parents[1]):
                _cfg = await asyncio.to_thread(_load_kiro_byo_catalog, _catalog_path)
            if getattr(_cfg, "models", None) and _cfg.mode_pack and _cfg.mode_type:
                _byo_default = _kiro_byo_type_selection_id(_cfg.mode_pack, _cfg.mode_type)
        except Exception:
            logger.warning("Kiro BYO slot routing reconciliation could not load default Type", exc_info=True)
        return _byo_default

    for _slot in targets:
        # A live ACP session is pinned to its creating harness, even if the
        # global default changes later. Inspect its actual provider first.
        # Never infer a different backend from the *new* global config while
        # a session is running. The native Kiro backend id is the empty string
        # and must not be mistaken for a missing backend.
        try:
            _live = state.sessions.get_provider(effective_session_key(_slot))
            if _live is not None:
                _capabilities = getattr(_live, "capabilities", None)
                _backend = getattr(_capabilities, "backend", None)
                if not isinstance(_backend, str):
                    # Unknown live provider type: do not mutate its saved model.
                    continue
            else:
                # A cold slot has no owner yet; only then can configured
                # routing select the initial harness.
                _backend = await _configured_backend_for_slot(_slot)
        except Exception:
            logger.debug("Kiro BYO slot backend probe failed during routing reconciliation", exc_info=True)
            continue

        _raw = str(getattr(_slot, "model", "") or "").strip()
        _raw_lower = _raw.lower()
        _jev = getattr(_slot, "jev_route", False) is True

        if _backend == ACP_BACKEND_KIRO_BYO:
            # A stale official routing selection has no meaning under BYO. Map
            # it to the active Mode's default Type. Concrete pins survive.
            if _jev or _raw_lower in {"", "auto", "auto:jev"}:
                _target = await _default_type()
                if _target and (_raw != _target or _jev):
                    _slot.model = _target
                    _slot.jev_route = False
                    _slot._dirty = True
                    changed = True
            continue

        # The reverse transition must not leak a BYO-private routing sentinel
        # into Kiro or another backend. KiroCrew 0.10 represents the official
        # Kiro backend/default selection with the empty value too, so every
        # non-BYO harness is returned to its native empty/default selection.
        if _raw.startswith("kiro-byo-type:"):
            _slot.model = ""
            _slot.jev_route = False
            _slot._dirty = True
            changed = True

    if changed:
        state.push_slots_update()
    return changed
'''


def _patch_slot_effort(text: str) -> str:
    """Apply BYO effort semantics plus harness-transition slot reconciliation."""
    # legacy._patch_chat_handlers expects this marker to already be present or
    # an agent_sdk.backends import to exist.  slot_effort has neither in pristine
    # Crew 0.10, so add a sibling runtime import next to aiohttp first.
    if _BACKEND_IMPORT not in text:
        _span, node = common._import_span(text, "aiohttp")
        if node.end_lineno is None:
            raise PatchError("Crew 0.10 aiohttp import has no source span")
        text = common._insert_after_line(text, node.end_lineno, _BACKEND_IMPORT)
    text = legacy._patch_chat_handlers(text)

    if "async def _kiro_byo_reconcile_slot_routing(" in text:
        # Replace only our owned helper when upgrading a 0.2.2 installation.
        # Otherwise a patched Gateway would retain the old cross-session bug.
        text = common._replace_owned_top_function(
            text, "_kiro_byo_reconcile_slot_routing", _KIRO_BYO_SLOT_RECONCILE_HELPER.lstrip("\r\n")
        )
    else:
        tree = common._parse(text)
        anchor_node = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_configured_backend_for_slot"
            ),
            None,
        )
        if anchor_node is None:
            raise PatchError("Crew 0.10 slot backend resolver seam moved")
        text = common._insert_before_line(
            text, anchor_node.lineno - 1, _KIRO_BYO_SLOT_RECONCILE_HELPER
        )

    marker = "    await _kiro_byo_reconcile_slot_routing(state, slot)\n"
    if marker not in text:
        tree = common._parse(text)
        fn = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "api_chat_slot_selection_capabilities"
            ),
            None,
        )
        if fn is None or fn.end_lineno is None:
            raise PatchError("Crew 0.10 selection-capabilities seam moved")
        lines = text.splitlines(keepends=True)
        block = "".join(lines[fn.lineno - 1 : fn.end_lineno])
        needle = "    if slot is None:\n        return slot_not_found()\n"
        if block.count(needle) != 1:
            raise PatchError("Crew 0.10 selection-capabilities slot guard moved")
        block = block.replace(needle, needle + marker, 1)
        text = "".join(lines[: fn.lineno - 1]) + block + "".join(lines[fn.end_lineno :])
    return text


_CHAT_TITLE_HELPER = r'''

async def _kiro_byo_title_model() -> str:
    """Use Crew's background-role model when the active harness is Kiro BYO."""
    def _read() -> str:
        cfg = KiroCrewConfig.load()
        if str(getattr(cfg.agent, "acp_backend", "") or "") == "kiro-byo":
            return str(cfg.agent.resolve_model("background") or "auto")
        return _TITLE_MODEL

    try:
        return await asyncio.to_thread(_read)
    except Exception:
        logger.debug("Kiro BYO title background-model resolution failed; using auto", exc_info=True)
        return _TITLE_MODEL
'''


def _patch_chat_title(text: str) -> str:
    """Make dashboard auto-title honor Crew's background role under Kiro BYO.

    Native Crew behavior stays untouched for every other backend.  This aligns
    dashboard titling with status-card/summary workers, which already resolve
    ``agent.role_models.background``.
    """
    common._parse(text)
    marker = "async def _kiro_byo_title_model() -> str:"
    patched_call = "model=await _kiro_byo_title_model(),"
    if marker not in text:
        title_anchor = '_TITLE_MODEL = "auto"'
        if text.count(title_anchor) != 1:
            raise PatchError("Crew 0.10 title model anchor moved")
        text = text.replace(title_anchor, title_anchor + _CHAT_TITLE_HELPER, 1)

    call_count = text.count("model=_TITLE_MODEL,")
    patched_count = text.count(patched_call)
    if call_count:
        if call_count != 2 or patched_count:
            raise PatchError("Crew 0.10 title one-liner call shape moved")
        text = text.replace("model=_TITLE_MODEL,", patched_call)
    elif patched_count != 2:
        raise PatchError("Crew 0.10 title one-liner call shape moved")
    return text


def patch_text(relative: str, text: str) -> str:
    rel = str(relative).replace("\\", "/")
    if rel == _CHAT_HANDLERS:
        patched = _patch_chat_handlers_namespace(text)
    elif rel == _SLOT_EFFORT:
        patched = _patch_slot_effort(text)
    elif rel == _CHAT_TITLE:
        patched = _patch_chat_title(text)
    elif rel in _COMMON_TARGET_FILES:
        patched = legacy.patch_text(rel, text)
    else:
        raise PatchError(f"no Crew 0.10 semantic patch registered for {rel}")
    common._parse(patched)
    return patched


@dataclass(frozen=True)
class TransformInspection:
    state: str  # clean | patched | upgradeable | incompatible
    detail: str = ""


_CUSTOM_MARKERS: dict[str, tuple[str, ...]] = {
    _CHAT_HANDLERS: (
        _BACKEND_IMPORT,
        "_kiro_byo_cold_selection_capabilities,",
        "_kiro_byo_type_selection_active,",
        "_kiro_byo_reconcile_slot_routing,",
    ),
    _SLOT_EFFORT: (
        "def _kiro_byo_cold_selection_capabilities(",
        "def _kiro_byo_type_selection_active(",
        "async def _kiro_byo_reconcile_slot_routing(",
        "await _kiro_byo_reconcile_slot_routing(state, slot)",
        "_byo_capabilities = await _kiro_byo_cold_selection_capabilities(slot)",
        "# Kiro BYO Type owns reasoning effort; suppress Crew backend fallback.",
        '"code": "effort_managed_by_type"',
        _BACKEND_IMPORT,
    ),
    _CHAT_TITLE: (
        "async def _kiro_byo_title_model() -> str:",
        "Kiro BYO title background-model resolution failed; using auto",
        "model=await _kiro_byo_title_model(),",
    ),
}


def inspect(relative: str, text: str) -> TransformInspection:
    rel = str(relative).replace("\\", "/")
    if rel in _COMMON_TARGET_FILES:
        report = legacy.inspect(rel, text)
        return TransformInspection(report.state, report.detail)

    markers = _CUSTOM_MARKERS.get(rel)
    if markers is None:
        return TransformInspection("incompatible", f"no Crew 0.10 semantic patch registered for {rel}")

    present = [marker in text for marker in markers]
    if any(present) and not all(present):
        return TransformInspection("incompatible", "partial/foreign Kiro BYO 0.10 patch")

    try:
        patched = patch_text(rel, text)
    except PatchError as exc:
        return TransformInspection("incompatible", str(exc))

    if all(present):
        if patched == text:
            return TransformInspection("patched")
        return TransformInspection(
            "upgradeable",
            "older Kiro BYO 0.10 dashboard integration can be upgraded in place",
        )
    if patched == text:
        return TransformInspection("incompatible", "clean source produced no Kiro BYO changes")
    return TransformInspection("clean")


def is_patched(relative: str, text: str) -> bool:
    return inspect(relative, text).state == "patched"
