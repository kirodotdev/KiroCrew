"""Running script hooks: the environment a hook process gets, one hook's sandboxed run
and its result, and ``ScriptHookStore``, which persists the Hooks page's hooks,
validates every edit and fires the hooks an event matches.

Composed onto ``kiro_crew.hooks``; see :mod:`kiro_crew.hook_runtime`.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
import time
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.hooks import (
        _HOOK_BASE_ENV_KEYS,
        _HOOK_STREAM_CAP_BYTES,
        _HOOK_TRUNCATION_MARKER,
        _HOOKS_FILE,
        HOOK_EVENT_AGENT_SPAWN,
        HOOK_EVENT_POST_TOOL_USE,
        HOOK_EVENT_PRE_TOOL_USE,
        HOOK_EVENT_STOP,
        HOOK_EVENT_USER_PROMPT_SUBMIT,
        HOOK_EVENTS_ALL,
        HOOK_EVENTS_KAS_ONLY,
        ScriptHook,
        _audit_governance_hook_decision,
        _communicate_capped,
        _context_matches,
        _decode_capped,
        _read_capped_stream,
        _script_hooks_capability_denied,
        _tool_matches,
        current_context,
        effective_denied_regexes_from_config,
        logger,
        platform_compat,
        redact_via_context,
        validate_hook_fields,
        webhooks,
    )


def _hook_subprocess_env(
    hook: "ScriptHook", context: str, acp_session_id: str | None = None
) -> dict[str, str]:
    """Build the environment a script-hook subprocess runs with.

    A strict allowlist over ``os.environ`` (``_HOOK_BASE_ENV_KEYS``) plus the two
    hook-metadata variables the hook contract exposes — never a copy of the whole
    gateway environment, which would leak the gateway's credentials to every hook
    command (see ``_HOOK_BASE_ENV_KEYS``). The metadata variables are set LAST so
    a same-named ambient variable can never shadow them.

    ``KIROCREW_HOOK_CONTEXT`` is capped at 500 chars on every event: the env
    var is bounded by ARG_MAX, and a large UserPromptSubmit prompt (pasted
    docs) fails subprocess creation the same way a multi-KB Stop segment does.
    For the events that echo their context onto stdin — UserPromptSubmit (the
    ``prompt`` key) and Stop (``assistant_text``) — the full text still reaches
    the hook there and drove matcher evaluation, so a hook reading stdin loses
    nothing. Other events (e.g. AgentSpawn) carry no dedicated context key on
    stdin, so for them this cap bounds what the env var conveys; their in-repo
    callers pass a short context (a session key), well under the cap.

    ``acp_session_id`` is the firing session's ACP session id, given only for an
    agent spec's own hooks that Crew runs in kiro-cli's place (see
    :mod:`kiro_crew.agent_sdk.spec_hooks`). It is set as ``KIRO_SESSION_ID``, the
    name kiro-cli gives the hooks it runs itself, so a spec hook reads the same
    value on either backend. Not known yet, it stays unset rather than empty.
    """
    env = {k: v for k, v in os.environ.items() if k in _HOOK_BASE_ENV_KEYS}
    env["KIROCREW_HOOK_EVENT"] = hook.event
    env["KIROCREW_HOOK_CONTEXT"] = context[:500]
    if acp_session_id:
        env["KIRO_SESSION_ID"] = acp_session_id
    return env


@dataclass
class ScriptHookResult:
    """Result of executing a script hook."""

    hook_id: str
    hook_name: str
    event: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    error: str = ""
    duration_ms: int = 0

    @property
    def blocked(self) -> bool:
        """PreToolUse exit code 2 = block tool."""
        return self.exit_code == 2

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


async def run_script_hook(
    hook: ScriptHook,
    context: str = "",
    hook_event: dict | None = None,
    cwd: str | None = None,
    acp_session_id: str | None = None,
) -> ScriptHookResult:
    """Execute a script hook's command with timeout.

    Passes hook event as JSON via STDIN (Kiro CLI compatible). ``cwd`` is the
    directory the command runs in; ``None`` keeps the gateway's own.
    ``acp_session_id`` becomes ``KIRO_SESSION_ID`` (see ``_hook_subprocess_env``).
    """
    start = time.monotonic()
    # Governance: the ``capabilities.script_hooks`` gate (default OFF) may forbid
    # running script hooks for the active surface. Checked before the subprocess
    # spawns. The session key is carried on the hook_event when a caller threads
    # it — ``parent_session_key`` for a subagent's event (the spawning session
    # governs), else the firing session's own ``session_key``; absent →
    # policy-only resolution.
    sk = ""
    if hook_event:
        sk = str(hook_event.get("parent_session_key") or hook_event.get("session_key") or "")
    # The event this run fires for, read before any await: a dashboard edit
    # rewrites ``hook.event`` in place, so the live field may name another event
    # by the time the check below runs, while the stdin payload names this one.
    firing_event = str((hook_event or {}).get("hook_event_name") or hook.event)
    # Offloaded: resolving the governance scope can walk the profile store, which
    # must not run on the gateway's shared event loop.
    gov_denied = await asyncio.to_thread(_script_hooks_capability_denied, sk)
    if not gov_denied:
        from kiro_crew.hook_event_governance import event_refusal

        # The policy's ``events`` list names the trigger events a hook may fire on.
        gov_denied = await asyncio.to_thread(event_refusal, sk, firing_event)
    if gov_denied:
        hook.last_run = time.time()
        hook.last_status = "blocked"
        hook.last_error = f"Blocked by governance: {gov_denied}"
        hook.run_count += 1
        _audit_governance_hook_decision(
            sk, f"run_script_hook:{hook.name or hook.id}", "denied", gov_denied
        )
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            error=f"Blocked by governance policy: {gov_denied}",
            exit_code=2,  # PreToolUse "block tool" convention
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    # Shell deny-list: the command runs via ``/bin/sh -c`` (``cmd /c`` on Windows)
    # below, OUTSIDE the ACP tool-permission/hook flow -- the same out-of-band
    # position a cron ``command`` runs in -- so the deny gate ``hooks.on_tool_call``
    # applies to a bash tool call never sees it. ``hooks.json`` stays writable
    # in-sandbox for ``register_hook`` (``sandbox._CREW_SANDBOX_VISIBLE_LEAVES``), so a
    # command a bash tool call would be REFUSED for must be refused here too rather
    # than running because it was spelled as a hook. Vetted against the SAME effective
    # deny set the tool gate and cron fire-time vet resolve -- the enabled built-ins
    # plus the user's own rules, honoring the Settings>Security opt-out with governance
    # pins force-re-added (``effective_denied_regexes_from_config``) -- through the
    # active context's PolicyAuthority so a companion edition's ADD-only overlay applies
    # too. A benign command matches nothing and runs unchanged; this is the fire-time
    # twin of cron's ``vet_job_at_fire_time``. ``is_denied`` emits the SEL ``deny_event``
    # itself, so no extra audit here. Off the loop like the governance check above: the
    # resolve reads the keystone file and the match runs regexes, neither of which
    # belongs on the gateway's shared event loop.
    if hook.command:

        def _shell_deny_reason() -> str | None:
            reason = current_context().security.is_denied(
                hook.command, denied_regexes=effective_denied_regexes_from_config()
            )
            return redact_via_context(reason) if reason else None

        deny_reason = await asyncio.to_thread(_shell_deny_reason)
        if deny_reason:
            hook.last_run = time.time()
            hook.last_status = "blocked"
            hook.last_error = deny_reason[:500]
            hook.run_count += 1
            return ScriptHookResult(
                hook_id=hook.id,
                hook_name=hook.name,
                event=hook.event,
                error=deny_reason,
                # PreToolUse "block tool" convention, as the governance deny above.
                exit_code=2,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
    # Build hook event JSON for STDIN
    if hook_event is None:
        hook_event = {"hook_event_name": firing_event, "cwd": os.getcwd()}
    stdin_data = json.dumps(hook_event).encode()

    proc: Any = None
    try:
        # circular import: sandbox → registry → apps → hooks, so import at call time
        from kiro_crew.sandbox import (
            create_subprocess_limited,
            sandboxed_spawn_argv,
            sandboxed_spawn_argv_async,
        )

        # A script hook inherits only the minimum env its shell + command need
        # (``_HOOK_BASE_ENV_KEYS``) plus the two hook-metadata variables — NOT a
        # copy of the whole gateway environment, which would expose the gateway's
        # AWS/model/OAuth/connection-string credentials to every hook command.
        env = _hook_subprocess_env(hook, context, acp_session_id)
        # Shell per platform: POSIX /bin/sh -c, Windows cmd /c (no /bin/sh there).
        # The argv is what the sandbox/cgroup chokepoints below vet, on BOTH
        # platforms — only the eventual spawn form differs (see the Windows
        # branch under the spawn).
        if platform_compat.IS_WINDOWS:
            argv = ["cmd", "/c", hook.command]
        else:
            argv = ["/bin/sh", "-c", hook.command]
        # Route argv and the strict hook allowlist through the shared spawn
        # funnel. Besides filesystem isolation and cgroup limits, this lets an
        # outer systemd-run wrapper receive its user-bus locators while inserting
        # an inner `env -u` shim that removes them before the hook command execs.
        # Calling wrap_argv + cgroup_scope_argv directly would give the wrapper
        # the child-safe allowlist and make it fail before a PreToolUse policy
        # hook could run.
        wrapped_argv, env, cleanup_path = await sandboxed_spawn_argv_async(
            argv, env=env, _prepare=sandboxed_spawn_argv
        )
        # Process-group isolation for clean tree-kill on timeout. Pass both flags
        # explicitly (NOT **dict unpack — breaks mypy's Popen overload resolution
        # on the build fleet): start_new_session=True is a no-op on Windows,
        # creationflags resolves to 0 (no-op) on POSIX. The Windows flag makes the
        # tree taskkill /T-reapable; POSIX setsid -> killpg.
        if platform_compat.IS_WINDOWS and wrapped_argv == argv:
            # cmd.exe must receive the operator's command line VERBATIM. Spawning
            # ``["cmd", "/c", command]`` as an argv routes it through
            # ``subprocess.list2cmdline``, which backslash-escapes every quote the
            # operator wrote — so a command as ordinary as
            # ``"C:\Program Files\Python\python.exe" -c "print(1)"`` arrives as
            # ``\"C:\Program Files\...\"`` and cmd.exe answers "is not recognized
            # as an internal or external command". ``create_subprocess_shell``
            # formats ``%ComSpec% /c "<command>"`` with no argv escaping, which is
            # the same parse the operator gets typing the line at a prompt (and
            # the only form under which ``%VAR%`` and a literal ``%`` both behave
            # as written — a temp ``.cmd`` wrapper would eat both).
            #
            # Guarded on the wrap being a no-op: Windows has no sandbox or cgroup
            # backend, so neither chokepoint can prepend anything today. Should
            # one ever appear, the wrapper MUST own the spawn — that case falls
            # through to the argv path below, choosing isolation over quoting.
            proc = await asyncio.create_subprocess_shell(
                hook.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            proc = await create_subprocess_limited(
                *wrapped_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
        try:
            (
                stdout_b,
                stdout_trunc,
                stderr_b,
                stderr_trunc,
            ) = await asyncio.wait_for(
                _communicate_capped(proc, stdin_data, _HOOK_STREAM_CAP_BYTES),
                timeout=hook.timeout,
            )
        finally:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass
        elapsed = int((time.monotonic() - start) * 1000)
        exit_code = proc.returncode or 0
        # Decode with the upstream byte cap so redaction never sees an unbounded
        # string, THEN redact the full capped streams through the canonical
        # companion-aware shim before any truncation or return. Both fields are
        # returned by the test API, and stdout can also become model context for
        # prompt/spawn hooks; redacting the capped text first prevents a credential
        # that straddles a presentation boundary (e.g. the 500-char stderr cut for
        # last_error) from leaking as an unredacted fragment.
        stdout_text = _decode_capped(stdout_b, stdout_trunc).strip()
        stderr_text = _decode_capped(stderr_b, stderr_trunc).strip()
        stdout_safe = redact_via_context(stdout_text) if stdout_text else ""
        stderr_safe_full = redact_via_context(stderr_text) if stderr_text else ""
        # An exit-2 deny reason is authored text and reads from the head; any
        # other failure is a crash whose diagnosis is printed last, so its
        # last_error excerpt keeps the tail. When the byte cap fired the real
        # tail was discarded before decoding, so the head is the only honest
        # excerpt left, and the truncation marker is re-appended so the excerpt
        # still says it is clipped. Redaction already ran on the full capped
        # stream above, so neither cut can sever a secret.
        if exit_code == 2:
            stderr_safe = stderr_safe_full[:500]
        elif stderr_trunc:
            head_len = 500 - len(_HOOK_TRUNCATION_MARKER)
            stderr_safe = stderr_safe_full[:head_len] + _HOOK_TRUNCATION_MARKER
        else:
            stderr_safe = stderr_safe_full[-500:]
        hook.last_run = time.time()
        if exit_code == 2:
            hook.last_status = "blocked"
            hook.last_error = stderr_safe or "Blocked (exit 2)"
        elif exit_code == 0:
            hook.last_status = "ok"
            hook.last_error = ""
        else:
            hook.last_status = "error"
            hook.last_error = stderr_safe or f"Exited with code {exit_code}"
        hook.run_count += 1
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            stdout=stdout_safe,
            stderr=stderr_safe_full,
            exit_code=exit_code,
            duration_ms=elapsed,
        )
    except asyncio.CancelledError:
        # A cancelled caller (a torn-down session, a cancelled turn) must not leave
        # the hook running: kill its tree, then let the cancellation propagate.
        if proc is not None and proc.returncode is None:
            try:
                await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
            except Exception:
                logger.debug("hook tree kill on cancel failed", exc_info=True)
        raise
    except asyncio.TimeoutError:
        # Kill the whole process tree (shell + grandchildren) to prevent orphans.
        # platform_compat: killpg on POSIX, taskkill /T on Windows (os.killpg /
        # signal.SIGKILL are POSIX-only and would AttributeError on win32).
        try:
            if proc.returncode is None:
                # Async variant offloads the Windows taskkill spawn — the hook
                # timeout path already runs on the event loop, so we never want
                # to stall it further while taskkill.exe walks the tree
                await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
                # Reap the killed tree WITHOUT re-buffering: a hook that timed
                # out having already flooded its pipes must not be able to OOM
                # us during cleanup. Drain both pipes concurrently under
                # the same cap and discard; sequential reads can deadlock when
                # residual data fills the other pipe.
                await asyncio.gather(
                    _read_capped_stream(proc.stdout, _HOOK_STREAM_CAP_BYTES),
                    _read_capped_stream(proc.stderr, _HOOK_STREAM_CAP_BYTES),
                )
                await proc.wait()
        except Exception:
            pass
        elapsed = int((time.monotonic() - start) * 1000)
        hook.last_run = time.time()
        hook.last_status = "timeout"
        hook.last_error = f"Timed out after {hook.timeout}s"
        hook.run_count += 1
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            error=f"Timed out after {hook.timeout}s",
            duration_ms=elapsed,
        )
    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        safe_error = redact_via_context(str(exc))
        hook.last_run = time.time()
        hook.last_status = "error"
        hook.last_error = safe_error[:500]
        hook.run_count += 1
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            error=safe_error,
            duration_ms=elapsed,
        )


class ScriptHookStore:
    """Persist script hooks to ~/.kiro/crew/hooks.json."""

    def __init__(self, config_dir: Path | None = None, *, load: bool = True):
        from kiro_crew.config.loader import config_dir as _cfg_dir

        self._dir = config_dir or _cfg_dir()
        self._path = self._dir / _HOOKS_FILE
        self._hooks: dict[str, ScriptHook] = {}
        # Entries that cannot be deserialized must remain inert, but they still
        # belong to the user. Preserve their raw JSON values across later status
        # and CRUD writes so fail-soft loading does not become silent data loss.
        self._unparsed_hook_entries: list[object] = []
        # Mutations are offloaded with asyncio.to_thread (the persistence takes a
        # file lock and fsyncs, which must not block the loop) rather than being
        # implicitly serialised on the single event-loop thread, so two of them can
        # genuinely interleave: A mutates, B mutates, B persists, then A persists a
        # snapshot taken BEFORE B's change and drops it. Re-entrant because the
        # persist path is called from inside the same held section.
        self._mutex = threading.RLock()
        if load:
            self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load hooks: %s", exc)
            return
        self._load_data(data)

    def _load_data(self, data: object) -> None:
        # Deserialize each hook independently: a single malformed entry (a
        # non-dict, or a dict `from_dict` cannot coerce) must not take down the
        # whole store and drop every OTHER hook the user has. `from_dict` is
        # already fail-soft (it normalizes junk fields), so a raise here would be
        # unexpected — but a foreign / hand-corrupted entry is possible, so keep
        # it inert and preserve its raw value for future rewrites.
        #
        # A malformed root or `hooks` collection cannot be represented by the
        # list-shaped store. Keep it inert so gateway startup remains available;
        # `_write_hooks_file` validates the locked, current bytes and refuses any
        # mutation rather than overwriting data the store cannot preserve.
        if not isinstance(data, dict):
            logger.warning("Failed to load hooks: root is not an object")
            return
        hooks_data = data.get("hooks", [])
        if not isinstance(hooks_data, list):
            logger.warning("Failed to load hooks: hooks collection is not a list")
            return

        for h in hooks_data:
            try:
                if not isinstance(h, dict):
                    raise TypeError("hook entry is not an object")
                # The full authoring vocabulary, not the fired subset: a hook
                # stored against a Kiro Agent trigger must survive a reload,
                # and the narrower set would quarantine it as unparseable.
                if h.get("event", HOOK_EVENT_USER_PROMPT_SUBMIT) not in HOOK_EVENTS_ALL:
                    raise ValueError("hook entry has an invalid event")
                hook = ScriptHook.from_dict(h)
                # Keep insertion inside the per-entry guard: a hand-edited ID
                # can be an unhashable list/dict even when from_dict succeeds.
                self._hooks[hook.id] = hook
            except Exception:
                logger.warning("Skipping unparseable hook entry", exc_info=True)
                self._unparsed_hook_entries.append(h)
                continue

    def _save(self) -> None:
        self._write_hooks_file(
            [
                *(h.to_dict() for h in self._hooks.values()),
                *self._unparsed_hook_entries,
            ]
        )

    def _write_hooks_file(self, hooks_data: Sequence[object]) -> None:
        """Write the ``hooks`` list while PRESERVING every other top-level key.

        ``hooks.json`` is shared: this store owns the ``hooks`` key, but the
        ``register_hook`` MCP tool stores webhook resume contexts as top-level
        keys (one per hook id) in the same file. Writing ``{"hooks": [...]}``
        wholesale erases all of them, so any script-hook create / update /
        toggle / delete would silently drop every pending webhook context. Merge
        instead of replace.

        An unreadable file ABORTS the write rather than proceeding with "no
        foreign keys". Continuing would leave the script hooks recoverable — but
        the foreign keys are not:
        a corrupt read means their contents are unknown, and writing the merged
        result would replace the file with only what this store happens to hold,
        permanently erasing every registered webhook context. Refusing leaves
        both sets on disk for an operator to repair. The caller sees
        :class:`webhooks.WebhookStoreUnreadable`.

        The read-merge-write runs under the SAME ``hooks.json.lock`` the other
        writers take, and lands via atomic replace. Merging without the lock
        still loses data, just through a narrower window: a ``register_hook``
        call that commits between this read and this write is erased by the
        stale snapshot.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        with webhooks.locked(self._path):
            data: dict = {}
            if self._path.exists():
                try:
                    loaded = json.loads(self._path.read_text(encoding="utf-8"))
                    if not isinstance(loaded, dict):
                        raise webhooks.WebhookStoreUnreadable(
                            f"{self._path.name} root is not an object; refusing to overwrite it"
                        )
                    if "hooks" in loaded and not isinstance(loaded["hooks"], list):
                        raise webhooks.WebhookStoreUnreadable(
                            f"{self._path.name} hooks collection is not a list; "
                            "refusing to overwrite it"
                        )
                    data = {k: v for k, v in loaded.items() if k != "hooks"}
                except webhooks.WebhookStoreUnreadable:
                    raise
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("hooks.json unreadable, refusing to overwrite it: %s", exc)
                    raise webhooks.WebhookStoreUnreadable(
                        f"{self._path.name} is unreadable; refusing to overwrite it "
                        "and erase the registered webhook contexts"
                    ) from exc
            data["hooks"] = hooks_data
            webhooks.write_json_atomic(self._path, data)

    def list_all(self) -> list[ScriptHook]:
        return list(self._hooks.values())

    def get(self, hook_id: str) -> ScriptHook | None:
        return self._hooks.get(hook_id)

    @contextmanager
    def _atomic_mutation(self):
        """Undo the in-memory change if persistence fails.

        ``_save`` refuses to overwrite an unreadable ``hooks.json`` rather than
        erasing the webhook contexts kept in the same file, and the write itself
        can fail on a full or read-only disk. Every mutation below edits
        ``self._hooks`` first, so without this the process would keep serving a
        change that never reached disk — a hook toggled on would keep firing, a
        deleted one would keep existing — while the API reported 503.

        A deep copy is used because ``update`` and ``toggle`` mutate the stored
        ``ScriptHook`` in place; a shallow dict copy would share those objects and
        restore nothing. The set is small (tens of hooks), so the copy is cheap
        next to the fsync it guards.
        """
        snapshot = copy.deepcopy(self._hooks)
        try:
            yield
        except BaseException:
            self._hooks = snapshot
            raise

    def create(self, data: dict) -> ScriptHook:
        hook = ScriptHook.from_dict(data)
        if not hook.id:
            hook.id = str(uuid.uuid4())[:8]
        # A hook on an event no event fires is saved OFF unless the caller said
        # otherwise. Nothing runs it either way today, so this costs the author
        # nothing now -- and it is the whole activation contract for later: the
        # change that starts firing these events inherits hooks that are already
        # disabled, so it cannot silently run a shell command somebody wrote
        # months earlier and never reconfirmed. ``fire`` skips a disabled hook;
        # the Test endpoint does not read ``enabled``, so Test still works, which
        # is the only way one of these runs at all. An explicit ``enabled: true``
        # is honoured -- that IS the reconfirmation.
        if "enabled" not in data and hook.event in HOOK_EVENTS_KAS_ONLY:
            hook.enabled = False
        # Enforce the SAME invariants `update` does, via the shared validator:
        # checking them only in `update` lets a direct/internal caller of `create`
        # bypass the command+skills invariant, event membership and timeout bounds,
        # persisting a hook the update path would reject and that later silently
        # fails to fire. `from_dict` clamps the timeout on the way
        # in, but validate against the ORIGINAL `data` so a caller that passed an
        # out-of-range timeout is told rather than having it silently clamped —
        # matching the API schema's reject-don't-clamp behavior. Raises
        # ValueError (mapped to HTTP 400 by the dashboard handler). The matcher is
        # read from `data` for the same reason as the timeout: `from_dict` drops one
        # stored against an event no event fires, which is right for a hand-edited
        # file and wrong for a caller who asked for it -- a POST carrying a matcher
        # must be told, not silently saved without the filter it named. So is the
        # mode: `from_dict` loads an unknown one as glob, and a caller who sent it
        # must be told rather than saved with a mode it did not ask for.
        validate_hook_fields(
            event=hook.event,
            timeout=data.get("timeout", hook.timeout),
            command=hook.command,
            skills=hook.skills,
            matcher=str(data.get("matcher", hook.matcher) or ""),
            matcher_mode=data.get("matcher_mode", hook.matcher_mode),
        )
        with self._mutex, self._atomic_mutation():
            self._hooks[hook.id] = hook
            self._save()
        return hook

    def update(self, hook_id: str, data: dict) -> ScriptHook | None:
        with self._mutex, self._atomic_mutation():
            hook = self._hooks.get(hook_id)
            if not hook:
                return None
            was_dormant = hook.event in HOOK_EVENTS_KAS_ONLY
            for k in ("name", "event", "matcher", "matcher_mode", "command", "timeout", "enabled"):
                if k in data:
                    setattr(hook, k, data[k])
            # The activation contract has to hold on BOTH write paths. `create`
            # stores a hook on one of the six switched off; without this, an edit
            # moving an ALREADY-ENABLED hook from a live event onto one of the six
            # kept it enabled, and the change that starts firing these events would
            # inherit exactly the pre-authorised command the contract exists to
            # prevent -- reached by an ordinary edit rather than anything exotic.
            #
            # Only on the TRANSITION into the set, and only when the caller did not
            # name `enabled`. A hook already on one of the six keeps whatever state
            # it has, so editing the command of one somebody deliberately switched
            # ON does not silently switch it off again -- the edit form always sends
            # `event`, so keying on presence rather than on the transition would do
            # exactly that.
            if (
                "event" in data
                and not was_dormant
                and hook.event in HOOK_EVENTS_KAS_ONLY
                and "enabled" not in data
            ):
                hook.enabled = False
            # A move onto one of the six also drops a matcher the caller did not
            # send. `from_dict` applies the same normalization on load, and its note
            # says why: `update` validates the MERGED fields, so a stored matcher
            # meeting the pairing refusal leaves a hook that cannot be edited -- or
            # even switched off -- without the caller also naming a field it never
            # touched, and the refusal names that field rather than anything the
            # request carried. A matcher present IN `data` still refuses, exactly as
            # `create` refuses one: a caller who asks for a filter these events
            # cannot use is told, not silently saved without it.
            if (
                "event" in data
                and "matcher" not in data
                and hook.matcher
                and hook.event in HOOK_EVENTS_KAS_ONLY
            ):
                logger.warning(
                    "hook %s moved onto %s; dropping its matcher (no event fires "
                    "this, so there is no payload to filter)",
                    hook.id,
                    hook.event,
                )
                hook.matcher = ""
            if "skills" in data:
                skills_raw = data["skills"]
                hook.skills = (
                    [str(s) for s in skills_raw if isinstance(s, str)]
                    if isinstance(skills_raw, list)
                    else []
                )
            # Validate the MERGED hook through the shared validator — the same
            # one `create` uses — so both write paths enforce one contract:
            # event membership, timeout bounds, the command+skills invariant and
            # its event pairing, and regex syntax. Validating post-merge (not the
            # request dict) is what catches a partial update that would otherwise
            # bypass a schema check keyed on the request — e.g. a matcher sent
            # without its matcher_mode, or skills added to a hook already on a
            # tool event. Raises ValueError (mapped to HTTP 400 by the handler).
            validate_hook_fields(
                event=hook.event,
                timeout=hook.timeout,
                command=hook.command,
                skills=hook.skills,
                matcher=hook.matcher,
                matcher_mode=hook.matcher_mode,
            )
            self._save()
        return hook

    def delete(self, hook_id: str) -> bool:
        with self._mutex, self._atomic_mutation():
            if hook_id in self._hooks:
                del self._hooks[hook_id]
                self._save()
                return True
        return False

    def toggle(self, hook_id: str) -> ScriptHook | None:
        with self._mutex, self._atomic_mutation():
            hook = self._hooks.get(hook_id)
            if not hook:
                return None
            hook.enabled = not hook.enabled
            self._save()
        return hook

    async def fire(
        self,
        event: str,
        context: str = "",
        tool_name: str = "",
        tool_input: dict | None = None,
        tool_response: dict | None = None,
        subagent_id: str | None = None,
        parent_session_key: str | None = None,
        agent_role: str | None = None,
        session_key: str | None = None,
        hook_continuation_count: int = 0,
        extra_hooks: Sequence[ScriptHook] = (),
        extra_hooks_cwd: str | None = None,
        extra_hooks_tool_names: Sequence[str] | None = None,
        tool_match_names: Sequence[str] | None = None,
        acp_session_id: str | None = None,
    ) -> list[ScriptHookResult]:
        """Fire all enabled hooks matching the given event. Returns results.

        ``extra_hooks`` run after the stored ones, through the same matcher, gate
        and spawn, and are never persisted: they belong to the caller (an agent
        spec's own ``hooks`` on a backend that cannot run them, see
        :mod:`kiro_crew.agent_sdk.spec_hooks`), not to this store. They run in
        ``extra_hooks_cwd`` -- the session's workspace, where the harness that
        would otherwise run them runs them -- and their payload's ``cwd`` says so.
        ``tool_match_names``, when given, are every name the call is known by (its
        title, its canonical tool name, its ``@server/tool`` form); a tool matcher
        then matches when it matches any of them. ``tool_name`` stays what the
        payload says.

        For PreToolUse/PostToolUse, matcher filters by tool name. When
        ``extra_hooks_tool_names`` is given, an extra hook's tool matcher is
        compared with those names instead: the tool's identity in the vocabulary
        the extra hooks were written in, which ``tool_name`` (the call's title)
        does not carry. It matches when any name does, and an empty sequence
        leaves only an unscoped (``*``) extra hook matching. The first name is
        also the ``tool_name`` an extra hook's stdin payload reports, so a script
        that branches on it reads the same vocabulary its matcher is written in.
        For AgentSpawn/UserPromptSubmit/Stop, all hooks for that event fire.

        Optional ``subagent_id``, ``parent_session_key``, and ``agent_role`` are
        emitted into the hook_event payload so hook scripts can attribute tool
        calls to the specific agent/session that fired them. They describe a
        SPAWNED subagent: ``subagent_id`` is its id and ``parent_session_key`` is
        the session that spawned it. Parent contexts (dashboard chat, generic LLM
        helpers) leave them as ``None`` — a hook may read a present
        ``parent_session_key`` as "this event came from a subagent".

        ``session_key`` is the firing session's OWN key (``dashboard:<slot>``,
        a channel session key, …), emitted as ``session_key`` whenever the caller
        knows it, on top-level and subagent events alike. It is the field for
        per-session attribution; before it existed the dashboard runner reused
        ``parent_session_key`` for that, which made every composer turn look like
        a subagent to a hook keyed on the field's documented meaning.

        ``acp_session_id`` is the firing session's ACP session id. Only the
        ``extra_hooks`` get it, as ``KIRO_SESSION_ID`` in their env and
        ``acp_session_id`` on their stdin: they are the spec hooks kiro-cli would
        run with ``KIRO_SESSION_ID`` set. The stored hooks never had it on any
        backend, so they still do not.

        For the Stop event, the full ``context`` (the final assistant segment) is
        used for matcher evaluation and echoed to stdin as ``assistant_text``;
        only the ``KIROCREW_HOOK_CONTEXT`` env var is length-capped downstream in
        ``run_script_hook`` (ARG_MAX safety), so a hook keying on the tail of the
        segment reads it from stdin JSON rather than the truncated env var.
        """
        results = []
        # Build base hook event (Kiro CLI format)
        hook_event: dict = {"hook_event_name": event, "cwd": os.getcwd()}
        if event == HOOK_EVENT_USER_PROMPT_SUBMIT and context:
            hook_event["prompt"] = context
        elif event == HOOK_EVENT_STOP:
            # Echo the final assistant segment to stdin so a hook keying on the
            # tail — e.g. the harness [OPTIONS:] line, past the env var's cap —
            # reads the whole thing here rather than the truncated env var.
            # Unconditional (even when "") so an empty/no-output Stop turn still
            # carries the key and a hook that always reads it never KeyErrors.
            hook_event["assistant_text"] = context
            # Advisory self-limiting signals: hook_continuation_count is the depth
            # of the current unbroken continuation run (0 on a normal turn), and
            # stop_hook_active is its boolean shorthand (count > 0). Kiro's Stop
            # contract defines no cap and neither field, so these are additive: a
            # hook may self-limit, diagnose, or surface the count to the model,
            # while a real gate hook checks its own condition and ignores them.
            # Stamped unconditionally so the keys are always present.
            hook_event["hook_continuation_count"] = hook_continuation_count
            hook_event["stop_hook_active"] = hook_continuation_count > 0
        if tool_name:
            hook_event["tool_name"] = tool_name
        if tool_input is not None:
            hook_event["tool_input"] = tool_input
        if tool_response is not None:
            hook_event["tool_response"] = tool_response
        if subagent_id:
            hook_event["subagent_id"] = subagent_id
        if parent_session_key:
            hook_event["parent_session_key"] = parent_session_key
        if agent_role:
            hook_event["agent_role"] = agent_role
        if session_key:
            hook_event["session_key"] = session_key

        extra_ids = {id(h) for h in extra_hooks}
        # The extra hooks' own payload: their workspace as ``cwd``, and on a tool
        # event the tool named in their vocabulary rather than the call's title.
        extra_event = dict(hook_event)
        if extra_hooks_cwd:
            extra_event["cwd"] = extra_hooks_cwd
        if extra_hooks_tool_names:
            extra_event["tool_name"] = extra_hooks_tool_names[0]
        extra_run: dict[str, str] = {}
        if extra_hooks_cwd:
            extra_run["cwd"] = extra_hooks_cwd
        if acp_session_id:
            extra_event["acp_session_id"] = acp_session_id
            extra_run["acp_session_id"] = acp_session_id
        # Only a hook that passes its matcher changes the status bookkeeping
        # (last_run / run_count / last_status). A fire that matched nothing has
        # nothing to record, so it must not rewrite hooks.json: that rewrite ran
        # on EVERY tool call and turn, hook or not.
        touched = False
        for hook in [*self._hooks.values(), *extra_hooks]:
            if not hook.enabled or hook.event != event:
                continue
            # Matcher filtering: for tool hooks, match tool name; for others, match context
            if hook.matcher:
                if event in (HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_POST_TOOL_USE):
                    if extra_hooks_tool_names is not None and id(hook) in extra_ids:
                        if hook.matcher != "*" and not any(
                            _tool_matches(hook.matcher, name) for name in extra_hooks_tool_names
                        ):
                            continue
                    elif not any(
                        _tool_matches(hook.matcher, name)
                        for name in (tool_match_names or (tool_name,))
                    ):
                        continue
                elif context:
                    # Offload to a thread: regex mode spawns a bounded subprocess
                    # (_bounded_pattern_search), which must not block the event loop.
                    matched = await asyncio.to_thread(
                        _context_matches, hook.matcher, hook.matcher_mode, context
                    )
                    if not matched:
                        continue
            touched = True
            # Skills-only hooks: inject skill-loading directive without subprocess.
            # Only meaningful for UserPromptSubmit/AgentSpawn — on tool hooks or Stop
            # the synthesized "Load skills:" text has no consumer.
            if (
                hook.skills
                and not hook.command
                and event
                in (
                    HOOK_EVENT_USER_PROMPT_SUBMIT,
                    HOOK_EVENT_AGENT_SPAWN,
                )
            ):
                # Governance: skills-only hooks must respect the same capability
                # gate as command hooks — a disabled capabilities.script_hooks
                # must not be bypassable by omitting the command field.
                sk = parent_session_key or session_key or ""
                # Off the loop, as in run_script_hook: the scope lookup can walk
                # the governance profile store.
                gov_denied = await asyncio.to_thread(_script_hooks_capability_denied, sk)
                if not gov_denied:
                    from kiro_crew.hook_event_governance import event_refusal

                    gov_denied = await asyncio.to_thread(event_refusal, sk, event)
                if gov_denied:
                    hook.last_run = time.time()
                    hook.last_status = "blocked"
                    hook.last_error = f"Blocked by governance: {gov_denied}"
                    hook.run_count += 1
                    _audit_governance_hook_decision(
                        sk, f"skills_only_hook:{hook.name or hook.id}", "denied", gov_denied
                    )
                    logger.info(
                        "Hook %s (%s): skills-only blocked by governance: %s",
                        hook.name,
                        event,
                        gov_denied,
                    )
                    continue
                # Audit the allow decision before proceeding.
                _audit_governance_hook_decision(
                    sk,
                    f"skills_only_hook:{hook.name or hook.id}",
                    "allowed",
                    "skills-only hook permitted",
                )
                skills_directive = " ".join(f"${s.rpartition('/')[2]}" for s in hook.skills)
                hook.last_run = time.time()
                hook.last_status = "ok"
                hook.last_error = ""
                hook.run_count += 1
                result = ScriptHookResult(
                    hook_id=hook.id,
                    hook_name=hook.name,
                    event=hook.event,
                    stdout=f"Load skills: {skills_directive}",
                    exit_code=0,
                    duration_ms=0,
                )
                results.append(result)
                logger.info(
                    "Hook %s (%s): skills-only injection (%d skills)",
                    hook.name,
                    event,
                    len(hook.skills),
                )
                continue
            if id(hook) in extra_ids:
                result = await run_script_hook(hook, context, extra_event, **extra_run)
            else:
                result = await run_script_hook(hook, context, hook_event)
            results.append(result)
            logger.info(
                "Hook %s (%s): %s in %dms (exit=%d)",
                hook.name,
                event,
                hook.last_status,
                result.duration_ms,
                result.exit_code,
            )
        # Snapshot INSIDE the worker under the mutex, not here: capturing on the
        # loop and persisting later leaves the same interleaving window a
        # concurrent CRUD mutation could fall into.
        if touched:
            await asyncio.to_thread(self._persist_current)
        return results

    def _persist_current(self) -> None:
        """Persist the live hook set, serialised against CRUD mutations.

        This path only records status bookkeeping after a fire; the hook set
        itself is unchanged. `_save` refuses to write over an unreadable
        `hooks.json` so it cannot destroy the webhook contexts sharing that
        file, but that refusal must not propagate here: `fire()` is awaited
        from the PRE_TOOL_USE path, which turns an exception into a rejected
        tool call, so a corrupt file would block every tool call in dashboard
        chat until an operator repaired it. Log and continue instead. The CRUD
        paths keep failing loud, where losing the write does change the hook set.
        """
        with self._mutex:
            try:
                self._save()
            except (webhooks.WebhookStoreUnreadable, OSError) as exc:
                logger.warning(
                    "Could not persist hook status bookkeeping: %s. "
                    "Hook execution continues; %s needs repair before "
                    "hook edits can be saved.",
                    exc,
                    self._path,
                )

    def _save_snapshot(self, hooks_data: list[dict]) -> None:
        """Thread-safe save using pre-captured hook snapshot."""
        with self._mutex:
            self._write_hooks_file([*hooks_data, *self._unparsed_hook_entries])
