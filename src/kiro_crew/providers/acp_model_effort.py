"""Choose the model and reasoning effort an ``AcpProvider`` session runs at.

The advertised-model revalidation the picker reads, the live model switch, and the
reasoning-effort vocabulary, resolution, live push, rollback and clear.

Each function here whose first parameter is ``self`` is an ``AcpProvider`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_effort_per_model``, ``_picker_probe_at``,
``_unbound_effort``, ``_unbound_effort_drop_logged``.

Instance fields read: ``_client``, ``_effort_defaults``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING

from kiro_crew.acp.client import (
    DEFAULT_MODEL,
    AcpClient,
    AcpError,
    _is_config_value_rejection,
    advertised_model_ids,
    catalog_row_would_drop,
)
from kiro_crew.acp.session_handle import (
    _READ_PATH_PROBE_DEADLINE_SECS,
    _READ_PATH_REPROBE_MIN_INTERVAL_SECS,
    EntitlementRevalidating,
)
from kiro_crew.acp.types import (
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    effort_config_option_id,
    effort_config_option_value,
)
from kiro_crew.acp_backends import POLICY_ID_BY_BACKEND
from kiro_crew.effort import EFFORT_LEVELS, model_supports_effort, resolve_effort_for_model
from kiro_crew.providers.acp_cli_overlay import (
    _await_overlay_write,
    _clear_cli_overlay_effort,
    _hold_workspace_effort_overlay_fence,
)
from kiro_crew.providers.base import LLMProvider
from kiro_crew.workspace_cli_settings import CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS

if TYPE_CHECKING:
    from kiro_crew.providers.acp import AcpProvider

#: The provider's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.providers.acp")


async def maybe_refresh_available_models(
    self: AcpProvider, catalog_ids: list[str]
) -> list[dict[str, str]]:
    """Revalidate the advertised-model snapshot on the picker read path.

    The dashboard model list (`/api/models`) narrows the catalog through this
    provider's snapshot. `self._client` becomes an `AcpSessionProvider` (an
    `LLMProvider`) on the kiro shared-runtime path, which carries the
    read-path revalidation; forward to it (propagating its contract: the read
    deadline raises
    :class:`~kiro_crew.acp.session_handle.EntitlementRevalidating` while the
    probe keeps running, and a probe FAILURE returns the current snapshot,
    fail open).

    On the DEDICATED transport `self._client` stays a plain kiro `AcpClient`
    whose snapshot is the one unconfirmed `session/new` answer a startup race
    can leave at the free tier -- the same defect on the same transport. When
    that snapshot would actually drop a catalog row (the picker's own verdict,
    :func:`catalog_row_would_drop`), it is re-asked through the client's
    :meth:`~kiro_crew.acp.client.AcpClient.refresh_available_models`, which
    asks on a throwaway probe process of its own -- never this session's stream,
    so a read during a streaming turn races nothing (a probe
    -confirmed list inside the probe TTL is not re-probed; a failed probe keeps
    the snapshot). A snapshot that drops nothing, a pre-startup placeholder
    client and a non-kiro direct client are returned unchanged, so this can
    never worsen the picker.
    """
    if isinstance(self._client, LLMProvider):
        return await self._client.maybe_refresh_available_models(catalog_ids)
    snapshot = self.available_models()
    client = self._client
    if not (
        isinstance(client, AcpClient)
        and getattr(client, "_is_kiro", False)
        and getattr(client, "_session_id", "")
    ):
        return snapshot
    advertised = advertised_model_ids(snapshot)
    if not any(catalog_row_would_drop(cid, advertised) for cid in catalog_ids):
        return snapshot
    # A recorded `_picker_probe_at` rate-limits the next poll regardless of
    # whether the last probe CONFIRMED: a failed probe never sets
    # `_available_models_probe_confirmed`, so keying the gate on that flag (as
    # the shared read path can, because its runtime single-flight probe TTL
    # bounds the burst) would leave an unconfirmed narrow snapshot cold-spawning
    # a fresh probe process on every 8s poll -- a self-sustaining kiro-cli +
    # MCP-fleet spawn loop. The dedicated transport has no such runtime TTL
    # backstop, so the interval alone must bound it. Skip the interval entirely
    # while a probe is already in flight: returning the snapshot here would
    # serve the stale list as a live 200 while its own refresh is still landing,
    # so the corrected list this in-flight probe is fetching would never reach
    # the picker -- fall through and await that same shielded probe.
    now = time.monotonic()
    last = getattr(self, "_picker_probe_at", 0.0)
    inflight = getattr(client, "_entitlement_probe_inflight", None)
    probe_in_flight = inflight is not None and not inflight.done()
    recently_probed = last > 0.0 and now - last < _READ_PATH_REPROBE_MIN_INTERVAL_SECS
    if not probe_in_flight and recently_probed:
        return snapshot
    self._picker_probe_at = now
    try:
        # The client owns the probe's single-flight, so shielding the await
        # leaves that one probe RUNNING (it lands and cleans up its throwaway
        # session); we just stop waiting past the deadline. Without this a
        # stalled probe would hold a picker poll or a pin-save for the probe's
        # full init+session timeout (~270s). On a miss, signal revalidation in
        # flight (the endpoint serves its degraded response and the frontend
        # keeps its last-good list and polls again) rather than returning the
        # un-revalidated snapshot as a live answer.
        fresh = await asyncio.wait_for(
            asyncio.shield(client.refresh_available_models()),
            timeout=_READ_PATH_PROBE_DEADLINE_SECS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        raise EntitlementRevalidating from None
    except Exception:
        logger.debug("dedicated-transport picker revalidation failed", exc_info=True)
        return snapshot
    return fresh or snapshot


async def _bind_unbound_effort(self: AcpProvider) -> None:
    """Key a level requested with nothing pinned to the model now serving.

    Runs on every (re)start and every fresh warm-runtime conversation once
    the session is ready and before the effort pushes, because only then
    has the backend named the model it chose, and a fresh session can serve
    another default than the last. The requested level replaces one already
    stored for that model: a live change on this provider moves the
    requested level with it, so a stored level that differs did not come
    from this caller, and keeping it would run the session at a level the
    caller did not ask for. The kiro family reads effort from the
    spawn-time overlay, which could not be keyed before the spawn, so a
    level not yet stored for that model is pushed live there through
    :meth:`change_effort`, which also writes the overlay. A later start
    projects as ``auto`` before its spawn, finds the level stored, and
    :meth:`_reassert_projected_effort` pushes it after the spawn. A level
    the serving model does not take is logged and left unapplied.
    """
    level = self._unbound_effort
    if not level:
        return
    pinned = self._client._model
    if pinned and pinned != DEFAULT_MODEL:
        return
    model = self.served_model
    if model:
        stored = self._effort_per_model.get(model)

        def _restore_stored() -> None:
            if stored is None:
                self._effort_per_model.pop(model, None)
            else:
                self._effort_per_model[model] = stored

        self._effort_per_model[model] = level
        if self._resolve_effort():
            if stored != level and self._client.backend in ACP_BACKENDS_KIRO_SLASH_COMMANDS:
                # change_effort stores the level itself once the overlay
                # write and the live push have both landed, and rolls back
                # to the entry it found when either fails. That entry must
                # be the one this session started with (none on a first
                # start), so a push kiro-cli refuses leaves the map as it
                # was rather than naming a level the session does not run.
                _restore_stored()
                try:
                    await self.change_effort(level)
                except Exception:
                    logger.warning(
                        "ACP unpinned effort apply failed (backend=%s model=%s effort=%s)",
                        self._client.backend,
                        model,
                        level,
                        exc_info=True,
                    )
            return
        _restore_stored()
    # Once per provider: a pooled or restarted session repeats the same fact.
    if self._unbound_effort_drop_logged:
        return
    self._unbound_effort_drop_logged = True
    logger.warning(
        "reasoning effort '%s' will not be applied (session %s) — model '%s' "
        "does not support effort configuration",
        level,
        self._owning_session_key() or "?",
        model or "auto",
    )


def _advertised_effort_levels(self: AcpProvider) -> list[str] | None:
    """The vocabulary this harness is the authority on, or None.

    A member of ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`` advertises the
    effort option itself, so the option it last served answers both which
    levels exist and whether any do. That is the ``session/new`` option, or,
    where the harness rebuilds it per model (goose), the one in the latest
    ``config_option_update`` or ``session/set_config_option`` result.
    ``None`` -- every other harness -- means the model registry answers, which
    is right where the level rides the model.
    """
    if self._client.backend not in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION:
        return None
    return self._client.get_valid_effort_levels()


def accepts_effort_level(self: AcpProvider, level: str) -> bool:
    """False when this session's advertised vocabulary has no spelling of *level*.

    Only a harness in ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`` can answer
    False, and only once it has advertised a non-empty option: there a Crew
    level the harness has no value for -- after its own spelling is applied --
    is HIDDEN rather than mapped onto a neighbour, so a write of it is refused
    before it reaches the session. Everywhere else the model registry and the
    step-down in :meth:`_set_effort_config_option` stay the authority, and this
    answers True.
    """
    advertised = self._advertised_effort_levels()
    if not advertised:
        return True
    return effort_config_option_value(self._client.backend, level) in advertised


def supports_effort(self: AcpProvider) -> bool:
    """True when this session accepts a reasoning-effort level.

    Drives the dashboard effort dropdown, and the single answer the three
    effort verbs below read -- a second copy of this question is how one of
    them comes to offer a control the others refuse.

    WHICH fact answers is per harness. Where the level rides the MODEL --
    kiro-cli refuses it per model, claude-agent-acp rebuilds the option from
    the model's ``supportedEffortLevels`` -- the registry answers. Where the
    harness advertises the option per SESSION
    (``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION``) the option answers,
    because the registry carries no entry for the operator's own model ids and
    its name heuristic recognises none of them, so it reports "no effort" for
    every ordinary session on such a harness and the control never appears.

    An advertised option offering ``off`` and nothing else answers False: the
    harness is saying the current model takes no level (goose does this per
    model), and every write there reads back as ``off``.
    """
    if self._client.backend in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION:
        if not self._client.supports_config_option(effort_config_option_id(self._client.backend)):
            return False
        advertised = self._client.get_valid_effort_levels()
        return not advertised or any(level != "off" for level in advertised)
    return model_supports_effort(self._effort_model)


def _resolve_effort(self: AcpProvider) -> str | None:
    """Resolve effort for the current model via the shared priority chain.

    Both the slot overrides and the workspace defaults key on the model id as
    RECORDED, which on an ``ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS`` harness is the
    advertised suffixed spelling (``openai.gpt-6-astra[max]``). So a default
    stored under the bare ``openai.gpt-6-astra`` does not answer for a session
    that picked the ``[max]`` row, and the row's own effort stands. That is the
    intended precedence -- an explicit pick of one advertised row is more
    specific than a per-model default -- and not an aliasing gap to close.
    ``change_effort`` writes the override under the same recorded spelling
    this reads, so the override path matches by construction.
    """
    # The fold the WRITE path takes, handed in so it runs BEFORE the
    # advertised-list check rather than after it. ``change_effort`` admits any
    # level the dynamic validation set knows, so a stored ``max`` on a harness
    # whose ceiling is ``xhigh`` is this harness's ``xhigh`` -- and a filter
    # that ran first would drop the very level the live push applied.
    return resolve_effort_for_model(
        self._effort_model,
        slot_overrides=self._effort_per_model,
        defaults=self._effort_defaults,
        levels=self._advertised_effort_levels(),
        normalize=functools.partial(effort_config_option_value, self._client.backend),
    )


async def _set_effort_config_option(self: AcpProvider, level: str) -> None:
    """Push an effort level over ``session/set_config_option``, stepping down
    on reject.

    Named for the CHANNEL, not a harness: every member of
    ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION`` arrives here, and the
    adapter-behaviour notes below are what that channel does in practice.

    WHICH option id carries the effort is resolved per backend through
    ``effort_config_option_id``: claude-agent-acp spells it ``effort``,
    codex-acp ``reasoning_effort`` and pi-acp ``thought_level``. Naming one
    spelling here writes an id the other adapters do not know, which comes
    back as "unknown config option" -- and the branch below reads that as "no
    effort selector" and skips, so the session keeps whatever effort it
    already had while the dashboard reports the level the user picked.

    WHICH value carries the level is resolved the same way, through
    ``effort_config_option_value``, and before the descent rather than by it:
    a harness whose vocabulary omits one of Crew's levels is a declared fact,
    and the descent recovers from an omission only when the refusal arrives in
    a shape ``_is_config_value_rejection`` recognises for that adapter.

    claude-agent-acp validates the value against the *current model's*
    ``supportedEffortLevels`` and throws ``Invalid value for config option
    effort: <level>`` (surfaced as an ``AcpError``) for anything the model
    does not advertise — e.g. ``"max"`` on a model whose ceiling is
    ``"xhigh"``. Rather than letting that bubble up (which makes the
    dashboard reset the whole session and silently drop to the adapter
    default), fall back down the effort ladder so the model still lands at
    the highest level it actually supports. Only the value-rejection error
    is retried; any other failure (transport, timeout) propagates. codex-acp
    refuses a value with a bare ``-32602`` and no message instead of naming
    the option, so the shared ``_is_config_value_rejection`` reader answers
    for both shapes.

    An adapter build may expose no effort config option at all and reject the
    push with ``Unknown config option: <id>`` (a -32603 Internal error). When
    the session advertises no effort selector, skip the push entirely — there
    is nothing to set, and attempting it would spam errors and trigger a
    session reset on every turn.
    """
    effort_option = effort_config_option_id(self._client.backend)
    if not self._client.supports_config_option(effort_option):
        logger.debug("adapter exposes no %r config option; skipping effort push", effort_option)
        return
    # The harness's own spelling of the level, resolved BEFORE the write for
    # the reason ``effort_config_option_value`` gives: a vocabulary that omits
    # one of Crew's levels is a declared fact, and the descent below can only
    # recover from it when the refusal shape is one this tree recognises.
    target = effort_config_option_value(self._client.backend, level)
    if target != level:
        logger.info(
            "effort %r spelled %r on backend %s",
            level,
            target,
            self._client.backend,
        )
    # Descend from the requested level through lower levels (e.g.
    # max → xhigh → high). Never escalate above what was asked.
    #
    # On a harness that is the authority on its own vocabulary, the descent
    # walks only the rungs that session advertised, and a target it never
    # advertised is not written at all: such a level is hidden there, not
    # stepped onto a neighbour, and every value this sends is one the harness
    # offered -- so no refusal shape has to be recognised for it.
    advertised = self._advertised_effort_levels()
    if advertised and target not in advertised:
        logger.info(
            "effort %r is not advertised by backend %s; skipping effort push",
            level,
            self._client.backend,
        )
        return
    try:
        start = EFFORT_LEVELS.index(target)
    except ValueError:
        await self._client.set_config_option(effort_option, target)
        return
    ladder = [lvl for lvl in reversed(EFFORT_LEVELS[: start + 1])]
    if advertised:
        ladder = [lvl for lvl in ladder if lvl in advertised]
    last_exc: Exception | None = None
    for candidate in ladder:
        try:
            await self._client.set_config_option(effort_option, candidate)
            if candidate != target:
                logger.info(
                    "CC effort %r unsupported by model %s — applied %r instead",
                    level,
                    self._client._model,
                    candidate,
                )
            return
        except AcpError as exc:
            if "unknown config option" in str(exc).lower():
                # Adapter has no effort option at all (older build) —
                # nothing to set; skip silently rather than reset.
                logger.debug("adapter rejected %r as unknown; skipping", effort_option)
                return
            if not _is_config_value_rejection(exc, effort_option):
                raise  # not a value-rejection — a real failure
            last_exc = exc
            continue
    # Every candidate rejected (unexpected) — surface the last error.
    if last_exc is not None:
        raise last_exc


async def change_effort(self: AcpProvider, level: str) -> bool:
    """Change effort live for the current model. Returns True on success.

    Persists the slot override and (kiro family) projects the overlay for a
    later respawn, then pushes the change to the running session: the
    ``/effort`` slash command for
    ``ACP_BACKENDS_KIRO_SLASH_COMMANDS``, ``session/set_config_option`` for
    ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION``. Returns False when the current
    model does not support effort, or when this harness is in neither set and
    so has no effort channel at all.
    """
    model = self._effort_model
    if not self.supports_effort():
        logger.info(
            "change_effort skipped — no effort level applies to this session (backend=%s model=%s)",
            self._client.backend,
            model,
        )
        return False
    # The capability answers, not raw set membership: KAS sits in both sets
    # and the capability layer is where the config option wins for it.
    via_config_option = self.capabilities.effort_via_config_option
    via_slash_command = self.capabilities.effort_via_slash_command
    if not (via_config_option or via_slash_command):
        # Both channels are opt-in, so a harness in neither has to be reported
        # unsupported here. Guessing one would push into a verb the adapter
        # does not implement and reset the session on a -32601.
        logger.info(
            "change_effort skipped — backend %r implements no effort channel",
            self._client.backend,
        )
        return False
    # An adapter build may advertise no 'effort' config option; attempting to
    # push would fail with 'Unknown config option' and reset the session.
    # Report unsupported so the dashboard leaves the UI as-is.
    _effort_option = effort_config_option_id(self._client.backend)
    if via_config_option and not self._client.supports_config_option(_effort_option):
        logger.info("change_effort skipped — adapter build exposes no %r option", _effort_option)
        return False
    # Accept any level the dynamic validation set knows about — ACP backends
    # can report levels beyond the canonical five (effort.py), and those are
    # already admitted by the dropdown, the API validator and persistence.
    # Gating here on the canonical-only is_valid_effort would raise for such
    # a level, which the caller swallows and turns into a full session reset.
    # The empty string ("use model default") is cleared via clear_effort(),
    # not change_effort(), so it remains invalid here.
    # circular import: chat_persistence → dashboard → session → providers.acp
    from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_values

    if not level or level not in get_reasoning_effort_values():
        raise ValueError(f"invalid effort level {level!r}")
    if not self.accepts_effort_level(level):
        # A level this session's own vocabulary hides is refused before
        # anything persists, so no override re-pushes it on a respawn.
        raise ValueError(
            f"effort level {level!r} is not offered by backend {self._client.backend!r}"
        )
    # Snapshot so a failed live push doesn't leave a poisoned override/
    # overlay that would re-push the rejected level on every respawn.
    _prev = self._effort_per_model.get(model)
    self._effort_per_model[model] = level
    # Off-loop, and with the ACTION ceiling: the FILE is what a respawn
    # reads, so pushing live over a write that did not persist would report
    # a change the next reset undoes. A ceiling far above projection's own
    # critical section makes losing the lock a stuck holder rather than
    # routine contention, and waiting it out on the loop would freeze every
    # session.
    if not await self._write_effort_overlay(
        lambda: self._apply_effort_overlay(timeout=CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS),
        CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
    ):
        if _prev is None:
            self._effort_per_model.pop(model, None)
        else:
            self._effort_per_model[model] = _prev
        raise RuntimeError(
            f"could not persist effort {level!r} for {model!r}: the workspace "
            "overlay is locked, a link or in a folder outside the work dir or on a "
            "sensitive path, unreadable, not a JSON object, or past the 1 MiB read "
            "ceiling; see the log"
        )
    try:
        if via_config_option:
            await self._set_effort_config_option(level)
        else:
            await self._client.send_command("/effort", args={"level": level})
    except Exception:
        # Roll back to the prior state before propagating to the caller.
        if _prev is None:
            self._effort_per_model.pop(model, None)
            if self._client.backend in ACP_BACKENDS_KIRO_SLASH_COMMANDS:
                if not await self._write_effort_overlay(
                    lambda: _clear_cli_overlay_effort(self._client._work_dir, model),
                    CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
                ):
                    # The overlay keeps the level the live push never applied.
                    # Keep it in the map too: it is the level this session was
                    # asked to run, so the next spawn projects it back rather
                    # than this rollback reporting an undo the file does not
                    # show.
                    self._effort_per_model[model] = level
                    logger.warning(
                        "ACP effort rollback left the overlay set (model=%s effort=%s): "
                        "the workspace overlay could not be rewritten because it is locked, "
                        "a link or in a folder outside the work dir or on a sensitive path, "
                        "unreadable, not a JSON object, or past the 1 MiB read ceiling; "
                        "a respawn adopts it",
                        model,
                        level,
                    )
        else:
            self._effort_per_model[model] = _prev
            # The ACTION ceiling, as the write being undone took: the
            # method's own default is the startup ceiling, under which a
            # holder of the settings lock that outlasts a launch's wait
            # keeps the level the live push never applied in the file.
            if not await self._write_effort_overlay(
                lambda: self._apply_effort_overlay(timeout=CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS),
                CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
            ):
                # Same case as the branch above: the file keeps the level the
                # live push never applied, and it is the level this session
                # was asked to run, so the map follows the file and the next
                # spawn projects it rather than this rollback reporting an
                # undo the file does not show.
                self._effort_per_model[model] = level
                logger.warning(
                    "ACP effort rollback left the overlay set (model=%s effort=%s): "
                    "the workspace overlay could not be rewritten because it is locked, "
                    "a link or in a folder outside the work dir or on a sensitive path, "
                    "unreadable, not a JSON object, or past the 1 MiB read ceiling; "
                    "a respawn adopts it",
                    model,
                    level,
                )
        logger.warning(
            "ACP effort live push failed (model=%s effort=%s) — rolled back", model, level
        )
        raise
    if self._unbound_effort:
        # Unpinned: the next restart binds the level picked last.
        self._unbound_effort = level
    logger.info(
        "ACP effort live-changed: model=%s effort=%s backend=%s",
        model,
        level,
        # Log the harness by its policy-facing name: ACP_BACKEND_KIRO is the
        # empty string, so the raw value would read as a missing field, and a
        # two-way "claude or kiro" label would name the wrong harness for
        # every backend added after it.
        POLICY_ID_BY_BACKEND.get(self._client.backend, self._client.backend),
    )
    return True


async def reapply_live_effort(self: AcpProvider, level: str) -> bool:
    """Re-apply *level* (empty: the resolved default) to the model just switched to.

    An in-place switch never respawns, so without this the new model runs at its
    own default. False asks the caller for a reset; errors propagate.
    """
    if not self.supports_effort():
        # No effort selector on this model: the level stays persisted for a
        # capable one, the same "persisted no-op" the effort endpoint applies.
        return True
    if level:
        if not self.accepts_effort_level(level):
            # Hidden by this session's own vocabulary: the same persisted
            # no-op, since a cold start drops it too and a reset gains nothing.
            return True
        return bool(await self.change_effort(level))
    # No override: re-resolve so a workspace default reaches the new model. A
    # False here only means there was no default to push, so nothing is stale.
    # An automatic clear: the ownership guard stays on, so an entry the
    # operator wrote for the model switched to is left where it is.
    await self.clear_effort()
    return True


async def set_model(self: AcpProvider, model: str) -> None:
    """Switch the live session's model, then re-apply the slot's effort to it.

    The effort goes last so it wins over a ``<model>[<effort>]`` pick's own
    level. The target's own override applies as ``reapply_live_effort`` does;
    without one, the level of the model left is pushed live but never stored.
    """
    overrides = self._effort_per_model
    carried = overrides.get(self._effort_model, "")
    await self._client.set_model(model)
    try:
        own = overrides.get(self._client._model) or overrides.get(model, "")
        if own or not carried:
            await self.reapply_live_effort(own)
        elif self.supports_effort():
            # Live only: persisting it would overwrite the target's stored state.
            if self.capabilities.effort_via_config_option:
                await self._set_effort_config_option(carried)
            elif self.capabilities.effort_via_slash_command:
                await self._client.send_command("/effort", args={"level": carried})
    except Exception:
        # The model DID switch; failing here would make a fallback walk skip it.
        logger.warning("Effort re-apply after set_model(%s) failed", model, exc_info=True)


async def clear_effort(
    self: AcpProvider,
    *,
    owned_only: bool = True,
    fence_rewrite: Callable[[], AbstractContextManager[object]] = nullcontext,
) -> bool | None:
    """Clear the slot's effort override for the current model.

    Drops the per-model override. A clear without a workspace default falls
    back to the model's own default after reset. With a workspace default,
    the projected file value or the live post-spawn push carries that default.

    ``owned_only`` decides whose overlay entry may go. The default removes
    only a value Kiro Crew recorded in the same cli.json document, so an
    operator-authored entry survives every automatic clear: a live model
    switch on a Default slot reaches here through ``reapply_live_effort``
    with nothing asked of that model's entry. Only the explicit dashboard
    clear passes ``owned_only=False``, because there the operator has
    directly asked for this model's entry to be removed, whoever wrote it.
    That clear also passes the session manager's fence as ``fence_rewrite``:
    the file is rewritten inside it, and a cancelled clear waits for its
    write to settle, so no warm runtime that may have read the removed entry
    is claimed after it.

    Returns True ONLY when a concrete default was applied LIVE to the
    running session (kiro family with a resolvable workspace default).
    Returns False — signalling the caller to reset the session so a cold
    start re-resolves the true default — in the cases that cannot be reset
    live:
    - the config-option channel has no "reset to default" value (there is no
      level to push; only a respawn drops the override), and
    - kiro with no workspace default (the model's built-in default is only
      re-applied on respawn once the overlay is cleared), and
    - the explicit clear with a workspace default whose live push fails after
      the rewrite (only a respawn applies the default: kiro-cli reads it from
      the file, KAS takes it as the start's ``effortLevel`` push). An automatic
      clear re-raises that failure instead, because its callers read a raise
      as the live re-apply failing.
    Without this, the running session would silently keep its prior effort
    while the UI shows "default".
    Returns None when NOTHING changed because the workspace overlay cannot be
    rewritten: it is locked, a link or in a folder outside the work dir or on a
    sensitive path, unreadable, not a JSON object, or past the 1 MiB read ceiling.
    Neither the file nor this map changes, so the caller commits no slot value and
    resets nothing.
    """
    model = self._effort_model
    # Cleared to the default: a level held for an unpinned start must not
    # be bound again by the next restart.
    self._unbound_effort = ""
    if not self.supports_effort():
        return False
    cleared = self._effort_per_model.pop(model, None)
    if self._client.backend not in ACP_BACKENDS_KIRO_SLASH_COMMANDS:
        # No live "reset to default" — caller must reset the session. Scoped
        # by membership so a harness that reads neither the overlay nor the
        # slash command is not sent down the kiro path below, which would
        # write an overlay it ignores and then push a verb it lacks.
        logger.info(
            "ACP effort cleared (%s); session reset needed for default",
            POLICY_ID_BY_BACKEND.get(self._client.backend, self._client.backend),
        )
        return False
    # kiro family: clear/rewrite the overlay so a respawn doesn't re-apply it.
    level = self._resolve_effort()  # workspace default, or None
    if level:
        # Explicit Default replaces the model's effort entries and projects
        # the workspace default in one locked atomic write. Automatic clears
        # retain the ownership guard and carry the resolved default live.
        async with _hold_workspace_effort_overlay_fence(
            self._client._work_dir, "write", CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS
        ) as acquired:
            if acquired:
                with fence_rewrite():
                    rewritten = await _await_overlay_write(
                        lambda: self._apply_effort_overlay(
                            timeout=CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS,
                            replace_unowned=not owned_only,
                        )
                    )
            else:
                rewritten = False
        if not rewritten:
            if cleared is not None:
                self._effort_per_model[model] = cleared
            logger.warning(
                "ACP effort NOT cleared to workspace default %s (model=%s): the "
                "workspace overlay cannot be rewritten because it is locked, a link or "
                "in a folder outside the work dir or on a sensitive path, unreadable, "
                "not a JSON object, or past the 1 MiB read ceiling; nothing was pushed live",
                level,
                model,
            )
            return None
        try:
            # Same channel choice as change_effort: KAS takes the level as a
            # config option, not the kiro-cli slash command.
            if self.capabilities.effort_via_config_option:
                await self._set_effort_config_option(level)
            else:
                await self._client.send_command("/effort", args={"level": level})
        except Exception:
            if owned_only:
                raise
            # The explicit clear has landed, so a reset carries the default to
            # the session: kiro-cli reads it from the rewritten file and KAS
            # takes it as the start's ``effortLevel`` push. Raising would make
            # its caller carry a removal that the file already holds.
            logger.warning(
                "ACP effort cleared to workspace default %s (kiro), but the live push "
                "failed; session reset needed",
                level,
                exc_info=True,
            )
            return False
        logger.info("ACP effort cleared to workspace default %s (kiro)", level)
        return True
    # No default to push live — clear the overlay and let the caller reset
    # so kiro respawns at the model's built-in default.
    async with _hold_workspace_effort_overlay_fence(
        self._client._work_dir, "write", CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS
    ) as acquired:
        if acquired:
            with fence_rewrite():
                rewritten = await _await_overlay_write(
                    lambda: _clear_cli_overlay_effort(
                        self._client._work_dir, model, owned_only=owned_only
                    )
                )
        else:
            rewritten = False
    if not rewritten:
        # NOTHING changed: not the file, and not this map once the entry
        # goes back. Neither bool can say that -- False resets (and the
        # respawn meets the same held lock, so it spawns into the same
        # level), True reports a live update, and BOTH make
        # the handler commit the cleared slot value, so the UI would read
        # "default" over an overlay that still holds the level. None is the
        # third outcome, which the handler answers with a retryable 409.
        if cleared is not None:
            self._effort_per_model[model] = cleared
        logger.warning(
            "ACP effort NOT cleared (model=%s): the workspace overlay cannot be rewritten "
            "because it is locked, a link or in a folder outside the work dir or on a "
            "sensitive path, unreadable, or past the 1 MiB read ceiling; the file still "
            "holds it and the running session keeps it",
            model,
        )
        return None
    logger.info("ACP effort cleared (kiro); session reset needed for built-in default")
    return False


async def _apply_initial_effort(self: AcpProvider) -> None:
    """Apply the resolved effort to a fresh session on the config-option channel.

    claude-agent-acp does NOT read ``CLAUDE_CODE_EFFORT_LEVEL`` from the
    environment — effort only takes hold via settings.json files or a live
    ``session/set_config_option``. So for the claude backend we push the
    resolved level once after the session is ready. Best-effort: a model
    that does not support effort, or an adapter that rejects the value,
    must not break session start.

    Gated on ``ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION`` -- the set that names
    the CHANNEL -- rather than on ``is_acp_runtime_backend``, for the reason
    harness-parity H6 gives: which harnesses take effort over
    ``session/set_config_option`` is a fact the table already holds, and
    "runs on the shared runtime" is a different fact. The kiro family is
    outside the channel set because it reads effort from the spawn-time
    cli.json overlay instead, so it still skips the push here; its own
    post-spawn push is :meth:`_reassert_projected_effort`, on the
    ``/effort`` channel. KAS is the exception, since it reads no overlay
    and is a member; opencode is
    outside it because its ``session/new`` advertises no ``effort`` option at
    all. Reading the runtime answer here instead drops a codex session's
    configured effort as soon as the preview switch puts codex on the
    runtime, because codex IS in the channel set and takes effort no other
    way.
    """
    if not self.capabilities.effort_via_config_option:
        return
    level = self._resolve_effort()
    if not level:
        return
    try:
        await self._set_effort_config_option(level)
        logger.info(
            "ACP initial effort applied: backend=%s model=%s effort=%s",
            self._client.backend,
            self._client._model,
            level,
        )
    except Exception:
        logger.warning(
            "ACP initial effort apply failed (backend=%s model=%s effort=%s)",
            self._client.backend,
            self._client._model,
            level,
            exc_info=True,
        )


async def _reassert_projected_effort(self: AcpProvider) -> None:
    """Push this session's level live after spawn and each fresh warm-runtime session.

    Only the harnesses that read the overlay and take effort over ``/effort``
    (``capabilities.effort_via_slash_command``) need this. KAS gets the overlay
    written but never reads it; it takes its level as the ``effortLevel``
    option that :meth:`_apply_initial_effort` pushes. The overlay is one file
    per work dir, projected before the spawn and read by kiro-cli at the
    spawn. Between the two, another session
    in the same work dir can project its own Default and remove this
    session's entry: an unpinned session removes every Kiro Crew entry, a
    session on the same model removes this model's, and the warm pool
    constructs such a session in the dashboard's own work dir on every fill
    and every replenish. This session cannot stop a peer from projecting, so
    it pushes its level through the same ``/effort`` channel a live change
    uses, and what it runs is its pick whatever the file said at the read.

    A session that resolves no level pushes nothing. This includes a Default
    session without a workspace default, ``auto``, or a harness that does not
    support effort. A Default session that inherits a workspace default pushes
    that level like a pick. The level is read under ``_effort_model``, so an
    unpinned session pushes the level :meth:`_bind_unbound_effort` stored
    for the model it serves. Kiro-cli has no live "model default" to push to,
    so the Default side of that same race stays with the overlay. Best-effort
    like :meth:`_apply_initial_effort`: a rejected push must not break session
    start.
    """
    if not self.capabilities.effort_via_slash_command:
        return
    model = self._effort_model
    if not model or model == DEFAULT_MODEL or not self.supports_effort():
        return
    level = self._resolve_effort()
    if not level:
        return
    try:
        await self._client.send_command("/effort", args={"level": level})
        logger.debug("ACP effort reasserted after spawn: model=%s effort=%s", model, level)
    except Exception:
        logger.warning(
            "ACP effort reassert after spawn failed (model=%s effort=%s): the session "
            "runs whatever level kiro-cli read from the overlay at spawn",
            model,
            level,
            exc_info=True,
        )
