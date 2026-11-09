"""``class ClaudeAcpClient``: the per-session client for claude-agent-acp.

``AcpClient`` is the client every per-session backend shares. Everything that only a
claude-agent-acp session does lives here instead: the ``settings.local.json`` seed and
its ownership, the session envelope (``_meta``), the starting-mode pin, the requested
``auto`` mode it applies and follows, the MCP array claude reads, its compaction notices
and its steering floor. ``ClaudeAcpClient`` binds its Claude-only steps here and
overrides existing ``AcpClient`` seams for the ordering that requested ``auto`` needs.
Building ``AcpClient`` for the claude backend builds this
class (``AcpClient.__new__``), so no caller names it.

The members are bound from the claude owner modules (``client_claude_session``,
``client_claude_seed``, ``client_seed_ownership``, ``client_claude_mode`` and
``client_claude_mode_seams``) the way
``AcpClient`` binds its own owners, and the class keeps the facade as its module for
reprs, pickles and type hints.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs
from kiro_crew.acp import client_claude_mode as _client_claude_mode
from kiro_crew.acp import client_claude_mode_seams as _client_claude_mode_seams
from kiro_crew.acp import client_claude_seed as _client_claude_seed
from kiro_crew.acp import client_claude_session as _client_claude_session
from kiro_crew.acp import client_claude_turn as _client_claude_turn
from kiro_crew.acp import client_new_conversation as _client_new_conversation
from kiro_crew.acp import client_seed_ownership as _client_seed_ownership
from kiro_crew.acp.client_class import AcpClient, logger
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE


class ClaudeAcpClient(AcpClient, serves=ACP_BACKEND_CLAUDE):
    """The ``AcpClient`` of a claude-agent-acp session."""

    # The facade is the class's import path, so pickles and reprs keep naming it.
    __module__ = "kiro_crew.acp.client"

    # Written by the claude adapter at launch, read back by the settings writer
    # and the handshake through ``getattr`` with an empty default.
    _claude_adapter_argv: tuple[str, ...]
    _claude_adapter_disk_version: str
    # Fields first set by a member an owner module defines, declared so the class names them.
    _claude_shipped_restrictions: frozenset[tuple[str, str]]
    _adopted_substitute_model: str

    #: This backend seeds (and must re-seed) a per-session settings file.
    _seeds_local_settings = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The permission mode the caller asked for (``AcpClient._permission_mode``):
        # None = nothing asked for a mode, so _write_claude_local_settings seeds no
        # defaultMode and the session starts in the mode the user's own settings
        # name. With KIROCREW_CC_PIN_INHERITED_MODE on, _pin_claude_starting_mode
        # then moves a session carrying Crew's tools that starts in "auto" or
        # "bypassPermissions" to "default". A Side Chat session
        # (``_side_read_only``) starts with only the read built-ins and no MCP
        # server, and its mode is pinned to ``default``. A sub-agent spawned with
        # ``include_memory=false`` (``_include_memory``) gets an envelope that turns
        # Claude Code's auto-memory off.
        #
        # That Side Chat session's sensitive-path deny rules, built off the loop
        # before its launch (_prepare_claude_side_read_denies). Empty until then,
        # and a side session whose envelope would carry none is not started.
        self._claude_side_read_denies: tuple[str, ...] = ()
        # A Bot worker session's held-limit asks, read off the loop before each
        # envelope (_prepare_claude_held_asks).
        self._claude_held_asks: tuple[str, ...] = ()
        # Whether those held limits cover MCP, so the session runs strictMcpConfig.
        self._claude_held_mcp = False
        # True once this session has CREATED <work_dir>/.claude/settings.local.json
        # itself. Only then does reset remove it, and only then does a re-seed
        # overwrite it. The writer refuses a path that already holds a file it did
        # not author, so Crew never owns the undo for someone else's project
        # settings -- see _write_claude_local_settings.
        self._claude_settings_authored = False
        # True while the project owns settings.local.json and this session leaves
        # that file out of its setting sources, carrying Crew's settings inline in
        # ``_claude_inline_settings`` instead -- see _exclude_foreign_local_settings.
        self._claude_local_settings_excluded = False
        self._claude_inline_settings: dict[str, Any] | None = None
        # The chat notice when the exclusion runs a requested ``auto`` in
        # ``default`` (_exclude_foreign_local_settings); "" otherwise.
        self.claude_mode_pinned_notice = ""
        # The mode the adapter confirmed this session in, or None. A requested-auto
        # session follows every mode report off the wire while watched. A report
        # during start is restored before the first prompt; after start settles, a
        # report that lands in an approving mode stops the captured process before
        # its reader can dispatch an agent-started turn.
        self._claude_confirmed_mode: str | None = None
        self._claude_mode_watched = False
        self._claude_mode_restore_due = False
        # Set by the installed-policy deny hook for the process it queued to stop,
        # until that session is found asking or another one starts. The same task
        # slot stops a settled requested-auto session that unexpectedly reports an
        # approving mode; the captured process keeps either stop from reaching a
        # replacement.
        self._claude_policy_deny_due = False
        self._claude_policy_stop_task: asyncio.Task[None] | None = None
        self._claude_policy_stop_process: Any | None = None
        self._claude_policy_stop_is_policy = False
        # The response whose starting mode was pinned, retained until the
        # Claude-owned drain applies the requested mode after the startup model.
        self._claude_start_response: dict[str, Any] | None = None
        # True from the read-back after the pin until the start's last step: a mode
        # report read meanwhile is restored by that start, not stopped
        # (_track_claude_starting_mode, _note_claude_mode_report).
        self._claude_mode_settling = False
        # A ``set_mode`` ``auto`` this session left unanswered and undone: an ``auto``
        # reported after that is moved back as well (_apply_claude_requested_mode).
        self._claude_auto_withdrawn = False
        # The exact bytes this session last wrote to that path, held as the str
        # whose utf-8 encoding IS those bytes -- the writer emits binary so no
        # newline translation can come between the two on any platform. Creating
        # the file is not sufficient ownership on its own: a user can replace it
        # atomically (write-temp + rename) after the create, and the replacement is
        # theirs. So the flag says "Crew created it" and this says "and it is still
        # Crew's content" -- the re-seed and the reset unlink both require BOTH.
        self._claude_settings_written: str | None = None
        # True while a durable OWNER holder of this client's is still on disk for a
        # file whose bytes are not Crew's: a user replaced the seed and the hand-back
        # (``forget``, then ``release``) could not reach the sidecar. Authorship
        # drops the moment the bytes are observed foreign -- governance is about the
        # file, not the record -- so this carries the one thing teardown still owes:
        # another attempt at the hand-back. It feeds NO governance decision.
        self._claude_settings_claim_unrevoked = False
        # True while this session holds a durable reader lease on a sibling
        # Crew client's seed. The lease pins that file's lifecycle even after a
        # later byte re-validation fails, so it is deliberately distinct from
        # whether the current bytes still govern this session's permission
        # surface. A sharer never removes the owner's file or live claim.
        self._claude_settings_shared = False
        # True only while the sibling seed's CURRENT on-disk bytes have passed
        # byte-for-byte validation against this session's rendered permission
        # surface. Unlike the retained reader lease above, every re-validation
        # clears this flag on entry and only full success re-earns it; the MCP
        # array is withheld after every non-success exit.
        self._permission_surface_share_validated = False
        # Identity this client claims its seed under, in the durable record. Two
        # keyless clients share the default work_dir, so a token is what keeps
        # "Crew wrote it" from collapsing into "any Crew client may take it": the
        # durable record is for adopting an ORPHAN, and a sibling still running in
        # this process does not have one. Per instance and never reset -- a client
        # that re-spawns is the same owner across spawns.
        self._seed_owner = uuid.uuid4().hex
        # The built-ins a crewmate worker keeps (``SessionProjection.builtin_tools``),
        # carried as claude's ``tools`` option; None keeps the harness's preset.
        self._claude_worker_builtin_tools: tuple[str, ...] | None = None
        # Set when the claude backend's "Compacting..." notice is seen inside a
        # turn and cleared by its terminal notice. Only a MANUAL /compact gets a
        # terminal from the adapter (see parse_claude_compaction_notice), so an
        # automatic mid-turn compaction leaves this armed and the dispatch loop
        # settles it with a synthetic `completed` at turn end. Without that, a
        # consumer showing a compacting state would never leave it.
        self._claude_compaction_pending: bool = False
        self._claude_native_compaction_id: str | None = None

    # Prove and hand back Crew's ownership of a claude settings seed on disk: ``kiro_crew.acp.client_seed_ownership``.
    _claude_local_settings_path = _client_seed_ownership._claude_local_settings_path
    _claude_settings_is_still_ours = _client_seed_ownership._claude_settings_is_still_ours
    _settings_path_bytes = staticmethod(_client_seed_ownership._settings_path_bytes)
    _expected_settings_fingerprint = _client_seed_ownership._expected_settings_fingerprint
    _withdraw_shared_reader_lease = _client_seed_ownership._withdraw_shared_reader_lease
    _rename_aside_noreplace = staticmethod(_client_seed_ownership._rename_aside_noreplace)
    _copy_aside_back_without_clobber = staticmethod(
        _client_seed_ownership._copy_aside_back_without_clobber
    )
    _discard_claude_settings_seed = _client_seed_ownership._discard_claude_settings_seed
    _hand_back_unrevoked_claim = _client_seed_ownership._hand_back_unrevoked_claim
    _settle_claude_settings_seed = _client_seed_ownership._settle_claude_settings_seed
    release_settings_seed_claim = _client_seed_ownership.release_settings_seed_claim
    _foreign_settings_owner = _client_seed_ownership._foreign_settings_owner
    _foreign_settings_is_crew_seed = _client_seed_ownership._foreign_settings_is_crew_seed
    settings_withheld_cause = _client_seed_ownership.settings_withheld_cause
    _settle_claude_settings_seed_locked = _client_seed_ownership._settle_claude_settings_seed_locked

    @staticmethod
    def _settings_path_holds(path: Path, expectation: tuple[int, str] | None) -> bool:
        """Whether *path* still holds exactly the ``(size, sha256)`` in *expectation*.

        Split out from :meth:`_claude_settings_is_still_ours` so the teardown
        transaction can be a pure function of arguments captured before it starts.
        It runs in a thread while the client clears its instance flags, so a version
        that re-read ``self`` could decide ownership against state that changed
        underneath it. ``None`` means Crew has no claim to check, which reads as
        "not ours".
        """
        return ClaudeAcpClient._settings_path_bytes(path, expectation) is not None

    @staticmethod
    def _claim_pathname_if_ours(
        path: Path, expectation: tuple[int, str] | None
    ) -> tuple[Path, tuple[int, int]] | None:
        """Atomically move *path* aside into a fresh name; return it IFF it is Crew's.

        Closes the window between an ownership check and the delete or overwrite that
        acts on it. ``_settings_path_holds`` verifies bytes by PATHNAME, but the delete
        or re-seed then mutates that same pathname a moment later -- and a user who
        atomically replaced the file in between would have their settings deleted or
        clobbered. ``os.replace`` captures whatever is at the pathname in ONE atomic
        step, so verifying the MOVED inode cannot then race: its bytes are fixed. A
        match is Crew's own seed (the caller deletes or replaces the moved file); a
        mismatch is a replacement that raced in, and it is put back untouched.

        The capture destination is a FRESH name created by ``mkstemp`` (``O_EXCL``) in
        the same directory, so it is a pathname this process just created and provably
        did NOT pre-exist. A fixed sibling name (e.g. ``<name>.crew-gc``) could name a
        file the project already owns, and ``os.replace`` onto it would clobber that
        file atomically -- relocating the very data loss this helper prevents one
        pathname over. ``os.replace`` onto our own fresh temp destroys only the empty
        temp we just made.

        ``(aside, identity)`` -- the moved file and ``(st_dev, st_ino)`` captured at
        the move, which every later restore and cleanup pins against. ``None``
        (nothing for the caller to mutate) when the path is gone, cannot be moved, or
        holds a file that is not Crew's. A crash between the move and the caller's
        follow-up leaves at most one such ``.crew-gc`` temp beside the target, which
        the next session's fresh seed ignores.
        """
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".crew-gc"
            )
        except OSError:
            return None
        tmp_st = os.fstat(fd)
        os.close(fd)
        aside = Path(tmp_name)
        try:
            os.replace(path, aside)
        except OSError:
            # Nothing to capture (path gone, or it cannot be moved): drop the empty
            # temp so a refusal never litters a stray file beside the target.
            with suppress(OSError):
                pinned_fs.unlink_verified_by_name(
                    path.parent, aside.name, (tmp_st.st_dev, tmp_st.st_ino)
                )
            return None
        try:
            moved = os.lstat(aside)
            moved_ident = (moved.st_dev, moved.st_ino)
        except OSError:
            # The moved entry vanished under us (a racing unlink): nothing left
            # to validate or restore. The empty temp name is gone with it.
            return None
        if ClaudeAcpClient._settings_path_holds(aside, expectation):
            return aside, moved_ident
        # Not Crew's after all: a replacement raced in, or the user's own file --
        # a different regular file, a symlink the verifying open refuses to
        # follow, a file of any size. Put the moved entry back AS IT IS, without
        # clobbering whatever else may have taken the vacated name in the
        # meantime. No-clobber preserves the NEWER occupant and keeps the moved
        # entry recoverable beside it, where a replace-semantics restore would
        # silently destroy the newer one.
        ClaudeAcpClient._restore_aside_without_clobber(aside, path, moved_ident)
        return None

    @property
    def _permission_surface_governed(self) -> bool:
        """Whether Crew controls this session's native permission surface.

        The precondition for delivering the ``mcpServers`` array, in either of
        its two shapes: this client authored ``settings.local.json`` itself
        (``_claude_settings_authored``), or the file's CURRENT on-disk bytes
        passed the sibling-seed validation
        (``_permission_surface_share_validated``). The durable reader lease is
        tracked separately in ``_claude_settings_shared`` because it survives a
        failed re-validation to keep the file pinned for a later re-earn, and the
        owner's un-revoked durable claim (``_claude_settings_claim_unrevoked``) is
        deliberately not read here either: it says a hand-back is still owed for
        a file already observed to be someone else's, never that Crew governs it.
        ``getattr`` on both governed flags because tests build clients without
        ``__init__``.

        A third shape governs without the file: the project owns it, and this
        session leaves it out of its setting sources
        (``_claude_local_settings_excluded``, see
        :meth:`_exclude_foreign_local_settings`). Its ``permissions.allow`` then
        never loads, so it cannot pre-approve a tool either.
        """
        return (
            getattr(self, "_claude_settings_authored", False)
            or getattr(self, "_permission_surface_share_validated", False)
            or getattr(self, "_claude_local_settings_excluded", False)
        )

    @property
    def _follows_requested_mode(self) -> bool:
        """Whether this session requested ``auto`` and follows every mode report it reads.

        Set by the read-back after the pin (:meth:`_track_claude_starting_mode`) and
        cleared when a start opens another session (:meth:`_forget_confirmed_mode`).
        ``getattr`` because tests build clients without ``__init__``.
        """
        return getattr(self, "_claude_mode_watched", False)

    # Author an ``AcpClient`` session's claude ``settings.local.json`` seed: ``kiro_crew.acp.client_claude_seed``.
    _share_settings_seed_if_identical = _client_claude_seed._share_settings_seed_if_identical
    _share_sibling_settings_seed = _client_claude_seed._share_sibling_settings_seed
    _retract_reseed = _client_claude_seed._retract_reseed
    _log_declined_share = _client_claude_seed._log_declined_share
    _render_claude_settings_payload = _client_claude_seed._render_claude_settings_payload
    _write_claude_local_settings = _client_claude_seed._write_claude_local_settings
    _reseed_after_capture = _client_claude_seed._reseed_after_capture
    _seed_session_settings = _client_claude_seed._seed_session_settings

    @staticmethod
    def _restore_aside_without_clobber(aside: Path, path: Path, expect: tuple[int, int]) -> bool:
        """Put a moved-aside entry back under *path* without replacing a new occupant.

        A same-directory no-clobber rename (:meth:`_rename_aside_noreplace`), so a
        symlink, a directory or a file of any size the user placed at the settings
        path is restored exactly as it was captured -- no copy, no size cap, no read
        of its bytes -- and the aside name is consumed by the move. An occupant that
        arrived at the pathname while the entry was out is newer, so the rename
        refuses it and the moved entry stays recoverable beside it as ``.crew-gc``
        litter. Where no no-clobber rename primitive exists the restore falls back
        to the validated byte copy :func:`pinned_fs.put_back_no_clobber`, pinned to
        the moved inode *expect*; that path restores only a regular file.
        """
        try:
            if ClaudeAcpClient._rename_aside_noreplace(aside, path):
                return True
        except FileExistsError:
            back: str | None = pinned_fs.PUT_BACK_NAME_TAKEN
        except OSError:
            logger.warning("could not restore %s; it is at %s", path, aside.name)
            return False
        else:
            back = ClaudeAcpClient._copy_aside_back_without_clobber(aside, path, expect)
            if back is None:
                return True
        if back == pinned_fs.PUT_BACK_NAME_TAKEN:
            logger.info(
                "%s was recreated while Crew's seed was moved aside; leaving the new "
                "file in place (the moved seed remains at %s).",
                path,
                aside.name,
            )
            return False
        logger.warning("could not restore %s; it is at %s", path, aside.name)
        return False

    # The claude-agent-acp session contract an ``AcpClient`` keeps: ``kiro_crew.acp.client_claude_session``.
    _exclude_foreign_local_settings = _client_claude_session._exclude_foreign_local_settings
    _claude_adapter_below_setting_sources_floor = (
        _client_claude_session._claude_adapter_below_setting_sources_floor
    )
    _log_claude_adapter_below_floor = _client_claude_session._log_claude_adapter_below_floor
    _note_claude_adapter_version = _client_claude_session._note_claude_adapter_version
    _claude_session_needs_options = _client_claude_session._claude_session_needs_options
    _refuse_claude_session_below_floor_before_spawn = (
        _client_claude_session._refuse_claude_session_below_floor_before_spawn
    )
    _prepare_claude_side_read_denies = _client_claude_session._prepare_claude_side_read_denies
    _prepare_claude_held_asks = _client_claude_session._prepare_claude_held_asks
    _claude_session_meta = _client_claude_session._claude_session_meta
    _claude_session_excludes_local = _client_claude_session._claude_session_excludes_local
    _claude_set_mode = _client_claude_session._claude_set_mode
    _claude_stop_session = _client_claude_session._claude_stop_session
    _verify_claude_project_denies_unchanged = (
        _client_claude_session._verify_claude_project_denies_unchanged
    )
    _pin_claude_starting_mode = _client_claude_session._pin_claude_starting_mode
    _reassert_adapter_resolved_model = _client_claude_session._reassert_adapter_resolved_model
    _claude_session_mcp_servers = _client_claude_session._claude_session_mcp_servers
    _fork_session = _client_claude_session._fork_session

    # Apply a requested ``auto`` and follow the session's mode reports: ``kiro_crew.acp.client_claude_mode``.
    _track_claude_starting_mode = _client_claude_mode._track_claude_starting_mode
    _note_claude_mode_report = _client_claude_mode._note_claude_mode_report
    _apply_claude_requested_mode = _client_claude_mode._apply_claude_requested_mode
    _restore_claude_asking_mode_if_due = _client_claude_mode._restore_claude_asking_mode_if_due
    _leave_claude_approving_mode_for_model_switch = (
        _client_claude_mode._leave_claude_approving_mode_for_model_switch
    )

    # The ``AcpClient`` seams this class overrides with Claude-owned behavior.
    _pins_starting_mode = True
    _ends_background_only_turns = True
    _harness_session_mcp_servers = _client_claude_session._harness_session_mcp_servers
    _harness_session_meta = _client_claude_session._harness_session_meta
    _retry_after_model_substitution = _client_claude_session._retry_after_model_substitution
    _local_settings_exclusion = _client_claude_session._local_settings_exclusion
    _note_worker_builtin_tools = _client_claude_session._note_worker_builtin_tools
    _ensure_served_default = _client_claude_session._ensure_served_default
    _note_harness_adapter_version = _client_claude_session._note_harness_adapter_version
    _pin_starting_mode = _client_claude_session._pin_starting_mode
    _forget_confirmed_mode = _client_claude_mode_seams._forget_confirmed_mode
    _initialize_params = _client_claude_mode_seams._initialize_params
    _drain_notifications = _client_claude_mode_seams._drain_notifications
    _read_message = _client_claude_mode_seams._read_message
    _push_model_config_option = _client_claude_mode_seams._push_model_config_option
    ensure_ready = _client_claude_mode_seams.ensure_ready
    _unasked_call_attribution = _client_claude_mode._unasked_call_attribution
    _session_excludes_local_settings = _client_claude_session._session_excludes_local_settings
    _write_settings_seed = _client_claude_seed._write_settings_seed
    _discard_settings_seed = _client_seed_ownership._discard_settings_seed
    _reset_settings_seed_state = _client_seed_ownership._reset_settings_seed_state
    _worker_settings_lag = _client_seed_ownership._worker_settings_lag
    # A fresh conversation on the live process: ``kiro_crew.acp.client_new_conversation``.
    new_conversation = _client_new_conversation.new_conversation

    # Read what a claude prompt turn carries: ``kiro_crew.acp.client_claude_turn``.
    _claude_compaction_event = _client_claude_turn._claude_compaction_event
    _settle_claude_compaction = _client_claude_turn._settle_claude_compaction
    _harness_compaction_event = _client_claude_turn._harness_compaction_event
    _settle_harness_compaction = _client_claude_turn._settle_harness_compaction
    _reset_harness_compaction = _client_claude_turn._reset_harness_compaction
    _claims_native_compaction = _client_claude_turn._claims_native_compaction
    _note_unhandled_session_update = _client_claude_turn._note_unhandled_session_update
    _steering_adapter_floor_met = _client_claude_turn._steering_adapter_floor_met
    _settle_background_only = _client_claude_turn._settle_background_only
