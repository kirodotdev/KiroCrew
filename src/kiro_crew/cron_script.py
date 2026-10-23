"""Code-based cron scripts — deterministic Python as cron callbacks.

Scripts under ``<config_dir>/crons/`` are LLM-writeable by design. The sandbox +
path-restriction prevents filesystem escape, but the LLM can register
self-written scripts. Mitigations: SEL audit trail on every invocation,
is_sensitive_path() blocks credential files, auto-pause after 5 consecutive
failures, concurrent execution guard prevents double-fire.

Usage:
    # <config_dir>/crons/my_monitor.py
    from kiro_crew.cron_script import Skip, Done

    def run(ctx):
        data = ctx.call_tool("kirocrew-core", "local_knowledge_search", {"query": "..."})
        if not ready(data):
            raise Skip()  # silent, retry next tick
        ctx.notify("Done: " + summary)
        raise Done()  # remove cron job
"""

from __future__ import annotations

import base64
import importlib as _importlib
import json
import os
import shutil
import subprocess
import sys
import sys as _sys
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from kiro_crew import _facade_mirror, platform_compat
from kiro_crew.config.loader import config_dir
from kiro_crew.cron_script_child_env import (
    _SCRATCH_OWNER_ERROR,
    _allocate_cron_scratch,
    _child_internal_secret,
    _clean_cron_env,
    _default_temp_dir,
    _discard_unused_cron_scratch,
    _publish_script_session_token,
    _record_cron_scratch_owner,
    _resolve_dial_port,
)
from kiro_crew.cron_script_grants import (
    _filter_grant_env,
    _read_script_body,
    _scrub_grant_values,
    _secret_env_precheck,
)
from kiro_crew.cron_script_paths import resolve_script_path
from kiro_crew.cron_script_procs import (
    _abandon_spawn,
    _begin_spawn,
    _drain_after_kill,
    _finish_spawn,
    _kill_proc_group,
    _spawn_cancelled,
    _unregister_proc,
)
from kiro_crew.cron_script_redaction import (
    _MAX_BAD_OUTPUT_HEAD,
    _MAX_SCRIPT_STDERR_TAIL,
    _REDACT_STRADDLE_MARGIN,
    _safe_tail_redaction_window,
)
from kiro_crew.github_runner import prevalidated_gh_env
from kiro_crew.mcp_gateway.backend_tmp import tmp_env
from kiro_crew.sandbox import (
    CRON_SCRIPT_CHILD_ENV,
    SandboxUnavailableError,
    cgroup_scope_argv,
    popen_limited,
    run_limited,
    wrap_argv,
)
from kiro_crew.security import redact


def run_script_sandboxed(
    script_path: str,
    job_id: str,
    job_message: str = "",
    timeout: int = 30,
    secret_env: dict[str, str] | None = None,
    secret_env_pin: str = "",
    delivery: str = "",
    internal_secret_provider: Callable[[], str] | None = None,
) -> dict:
    """Run a cron script in a sandboxed subprocess via wrap_argv().

    Returns: {"status": "ok"|"skip"|"done"|"error", "message": "...", "error": "..."}

    ``internal_secret_provider`` returns the gateway's LIVE in-memory internal
    secret — the one the auth middleware actually compares against. The
    in-process cron scheduler passes it so the child's ``notify()`` credential
    is the running gateway's own value rather than one re-derived from the
    environment or a per-port file. Env/file derivation
    (``_resolve_internal_secret``) is the fallback for a runner constructed
    OUTSIDE a gateway process (tests, ``kirocrew cron preview``) or a gateway
    started with no dashboard (``--no-dashboard`` / API-only), where there is
    no live secret to hand over. A stale ``KIROCREW_INTERNAL_SECRET`` in an
    operator shell or a stale per-port ``.secret`` file otherwise wins the
    derivation and every ``notify()`` 403s.

    ``secret_env``/``secret_env_pin`` carry an operator grant of vault secrets
    (see the grant block near ``_CRON_ENV_DENY``). When a grant is present the
    script body is read ONCE here, pin-verified, and the child executes those
    verified bytes from a private temp copy — the on-disk script (agent-
    writeable by design) is never re-read by the launcher, so a body swapped
    in after the check cannot run with the secrets. The temp copy's own dir —
    not the live ``crons/`` dir — is what goes on ``sys.path``, so a granted
    script cannot ``import helper`` from the agent-writeable dir either: a
    sibling module the operator did not approve fails the import instead of
    running with the secrets. A script that needs siblings must inline them
    (one approved body) or read them as data.
    """

    # A PERSISTED spec (see resolve_script_path): an app cron's stored path
    # points into its bundle, which no authoring path may name.
    file_path_str, func_name = resolve_script_path(script_path, allow_bundle_roots=True)

    import_dir_str = os.path.dirname(file_path_str)
    resolved_secret_env: dict[str, str] = {}
    script_body: bytes | None = None
    pinned_dir: str | None = None
    # Validated BEFORE the first temp file of this run: the pinned dir, the
    # launcher and the secret file below all use ``dir=None``, which is the
    # process-wide default -- a value cached at gateway start that can name a
    # directory reclaimed since. See ``_default_temp_dir``.
    _default_temp_dir()
    if secret_env:
        try:
            script_body = _read_script_body(file_path_str)
        except ValueError as exc:
            return {"status": "error", "error": f"Script unreadable: {exc}"}
        resolved_secret_env, secret_err = _secret_env_precheck(
            secret_env,
            secret_env_pin,
            script=script_path,
            script_body=script_body,
            message=job_message,
            job_id=job_id,
            delivery=delivery,
        )
        if secret_err:
            return {"status": "error", "error": f"❌ {secret_err}"}
        # Granted scripts get an EMPTY private dir as sys.path[0] (sibling
        # imports from the agent-writeable live crons/ dir fail instead of
        # running with the secrets). The verified body itself travels over
        # STDIN — never re-read from any pathname a same-UID writer could
        # swap after verification.
        pinned_dir = tempfile.mkdtemp(prefix="kirocrew_cron_pin_")
        import_dir_str = pinned_dir

    stdin_payload: str | None = None
    if resolved_secret_env:

        stdin_payload = json.dumps(
            {
                "body_b64": base64.b64encode(script_body or b"").decode(),
                "secrets": _filter_grant_env(resolved_secret_env),
            }
        )
    # A granted child starts with ``python -I`` (isolated: PYTHONPATH and
    # user-site are never consulted), so an agent-planted ``sitecustomize.py``
    # cannot run before this launcher and intercept the secrets update below.
    # Isolation also means ``kiro_crew`` may no longer be importable via an
    # inherited PYTHONPATH (dev checkouts), so the TRUSTED package parent —
    # computed here in the gateway from kiro_crew's own location, never from
    # the environment — is seeded explicitly. The child runs sys.executable,
    # which requires-python pins to 3.12+, where ``-I`` implies safe_path (no
    # script-dir prepend). As defense in depth the granted launcher still lives
    # in the private pinned dir (never the shared temp dir, where an agent can
    # park a json.py indefinitely) AND the prelude strips that directory by
    # VALUE — a positional strip would drop a stdlib entry, since nothing was
    # prepended. Both spellings are stripped because CPython
    # realpaths the script dir when computing sys.path[0].
    _kiro_pkg_parent = str(Path(__file__).resolve().parent.parent)
    if stdin_payload is not None:
        assert pinned_dir is not None
        _launcher_dirs = (pinned_dir, os.path.realpath(pinned_dir))
        prelude = (
            "import sys\n"
            f"sys.path[:] = [p for p in sys.path if p not in ('', *{_launcher_dirs!r})]\n"
            f"sys.path.insert(0, {_kiro_pkg_parent!r})\n"
            "import base64, json, os, types\n"
        )
    else:
        prelude = (
            # Import sys first (builtin, unshadowable) and strip the launcher's
            # own /tmp dir from sys.path[0] before importing json/os/types/
            # kiro_crew — otherwise a stray sibling like /tmp/struct.py or
            # /tmp/os.py shadows the stdlib and crashes the cron launcher on
            # startup. The user-script dir is re-added explicitly below.
            "import sys\n"
            "sys.path[:] = [p for p in sys.path if p not in ('', sys.path[0])]\n"
            "import base64, json, os, types\n"
        )
    launcher = prelude + (
        # A granted run receives {body_b64, secrets} over STDIN, before any
        # other work: the verified bytes are executed directly (no pathname to
        # swap between verification and exec), and the secrets enter
        # os.environ AFTER this process's execve — the kernel's
        # /proc/<pid>/environ snapshot is the STARTUP environment, so a
        # same-UID reader of that file never sees them. Ungranted runs get no
        # payload and exec the live file as before.
        f"_payload = json.loads(sys.stdin.readline()) if {bool(stdin_payload)!r} else None\n"
        "if _payload:\n"
        "    os.environ.update(_payload['secrets'])\n"
        "from kiro_crew.config.loader import KiroCrewConfig\n"
        "from kiro_crew.platform.bootstrap import boot_platform\n"
        "boot_platform(KiroCrewConfig.load())\n"
        # Before the script body, so a dead filesystem can never be reported as
        # a successful no-op run.
        "from kiro_crew.cron_script import child_persistence_preflight\n"
        "child_persistence_preflight()\n"
        "from kiro_crew.cron_script import ScriptContext, Skip, Done, Report\n"
        # Record the granted key NAMES so _clean_cron_env strips them from
        # every descendant env (ctx.call_tool's MCP server subprocess): the
        # grant authorizes THIS approved body, not the binaries it calls.
        "if _payload:\n"
        "    import kiro_crew.cron_script as _kcs\n"
        "    _kcs._GRANTED_ENV_KEYS.update(_payload['secrets'])\n"
        f"sys.path.insert(0, {import_dir_str!r})\n"
        f"mod = types.ModuleType('_cron_script')\n"
        f"mod.__file__ = {file_path_str!r}\n"
        # Registered before exec: dataclasses, typing.get_type_hints and pickle
        # resolve a class's names through sys.modules[cls.__module__], which a
        # postponed-annotations script needs at class-definition time.
        "sys.modules['_cron_script'] = mod\n"
        # The compile filename stays the original so tracebacks point at the
        # file the operator knows.
        "if _payload:\n"
        f"    _src = base64.b64decode(_payload['body_b64'])\n"
        "else:\n"
        f"    with open({file_path_str!r}, 'rb') as f:\n"
        "        _src = f.read()\n"
        f"exec(compile(_src, {file_path_str!r}, 'exec'), mod.__dict__)\n"
        f"fn = getattr(mod, {func_name!r}, None)\n"
        "if fn is None:\n"
        f"    print(json.dumps({{'status': 'error', 'error': 'Function not found'}}))\n"
        "    sys.exit(0)\n"
        f"job = types.SimpleNamespace(id={job_id!r}, message={job_message!r})\n"
        "ctx = ScriptContext(job=job)\n"
        "try:\n"
        "    fn(ctx)\n"
        "    _envelope = {'status': 'ok'}\n"
        "except Skip:\n"
        "    _envelope = {'status': 'skip'}\n"
        "except Done as d:\n"
        "    _envelope = {'status': 'done', 'message': d.message}\n"
        "except Report as r:\n"
        "    _envelope = {'status': 'report', 'message': r.message}\n"
        "except Exception as e:\n"
        "    _envelope = {'status': 'error', 'error': str(e)}\n"
        "finally:\n"
        "    ctx.close()\n"
        # Probe again before any non-error envelope: a sandbox torn down WHILE
        # the body ran passed the preamble probe, and its run persisted nothing.
        "if _envelope['status'] != 'error':\n"
        "    child_persistence_preflight()\n"
        "print(json.dumps(_envelope))\n"
    )

    # A granted launcher is born inside the private pinned dir, as defense in
    # depth beside ``-I``'s safe_path: the shared temp dir is somewhere an agent
    # can leave a json.py waiting.
    # Ungranted runs keep the shared temp dir (their prelude strips it).
    fd, launcher_path = tempfile.mkstemp(
        suffix=".py", prefix="kirocrew_cron_", dir=pinned_dir if stdin_payload else None
    )
    sandbox_cleanup: str | None = None
    # Resolve the dial port ONCE: the credential written below and the
    # _KIROCREW_DIAL_PORT the child dials must come from the same resolution, or a
    # --port auto bind between two resolutions would pair a credential with the
    # wrong port and 403 the callback.
    dial_port = _resolve_dial_port()
    # Prefer the gateway's LIVE in-memory secret (the value the auth middleware
    # compares against) when the in-process scheduler supplied a provider;
    # otherwise derive it from env/file. Deriving is correct only OUTSIDE a
    # gateway process (tests, cron preview) or when no dashboard started —
    # inside a live gateway a stale KIROCREW_INTERNAL_SECRET or a stale per-port
    # .secret file would win the derivation and 403 every notify().
    internal_secret = _child_internal_secret(internal_secret_provider, dial_port)
    # Write secret to temp file for ScriptContext (scrubbed from env)
    secret_fd, secret_path = tempfile.mkstemp(prefix="kirocrew_secret_")
    from kiro_crew.session_token_sig import retract_session_token

    script_session_token = ""
    # Per-run temp dir; ``cron_scratch_used`` turns True once a child
    # exists, after which the sweep -- not this function -- reclaims it.
    cron_scratch: Path | None = None
    cron_scratch_used = False
    try:
        try:
            # Tighten the DACL BEFORE writing the secret bytes so the file is
            # never on disk under the parent-inherited %TEMP% DACL on Windows.
            # On POSIX mkstemp already births the file 0600 so ordering is a
            # no-op; on Windows mkstemp cannot set an owner-only DACL, so the
            # interval between create and lockdown is a real window
            # if we wrote first. Matches the fail-loud convention of the other
            # internal-secret writers (token_secret, refresh_tokens, snapshot,
            # server._write_secret_file, token_auth) — chmod_safe swallows
            # OSError and would hide a lockdown failure. Both calls stay inside
            # the outer try so a lockdown failure still hits the finally that
            # unlinks the secret + launcher (otherwise the fd leaks and temp
            # files persist).
            platform_compat.restrict_to_owner(secret_path)
            os.write(secret_fd, internal_secret.encode())
        finally:
            os.close(secret_fd)
        try:
            os.write(fd, launcher.encode())
        finally:
            os.close(fd)

        # Granted children run ISOLATED (-I): PYTHONPATH and user-site are
        # ignored, so agent-planted startup hooks (sitecustomize/usercustomize)
        # cannot execute before the launcher and capture the secrets it loads.
        # The launcher prelude seeds the trusted kiro_crew package parent
        # explicitly, so imports survive isolation on dev checkouts too.
        argv = (
            [sys.executable, "-I", launcher_path]
            if stdin_payload is not None
            else [sys.executable, launcher_path]
        )
        # A granted child must never see the LIVE crons dir OR the script's
        # own parent directory: the launcher's empty-sys.path isolation stops
        # accidental sibling imports, but the verified script itself could
        # re-add either directory and import a sibling the agent rewrites
        # after approval — the pin covers the script's own bytes, not code it
        # chooses to load. The verified bytes travel over STDIN, so the child
        # never needs to read the script file; hiding both trees at the
        # sandbox layer closes the mutable-sibling route regardless of what
        # the script does to sys.path. Granted runs additionally use the
        # STRICT sandbox profile (credential stores and every crew-internal
        # dir hidden), keeping the child's reachable surface as small as the
        # sandbox can make it. Ungranted scripts keep their normal view.
        if stdin_payload is not None:
            hidden = tuple(
                dict.fromkeys(
                    (
                        str(config_dir() / "crons"),
                        str(Path(file_path_str).resolve().parent),
                    )
                )
            )
        else:
            hidden = ()
        # Same tier as ``run_command_sandboxed`` below: a script body is
        # agent-written, so it is the HIGHER-capability cron surface, and it
        # ran the WIDER profile — ``standard`` leaves ~/.aws/credentials, the
        # SSO cache, ~/.kube, ~/.netrc, ~/.git-credentials, ~/.npmrc and
        # ~/.pypirc open to the child, while a fixed command has always run
        # ``cc``. The static body vet cannot be the fence (its own docstring
        # says so and names the sandbox as the runtime control), so the two
        # cron spawn paths are aligned on ``cc`` here. ``cc`` is the Claude
        # Code provider's tier, and on macOS it deliberately leaves ``~/.aws``
        # readable for that provider's Bedrock ``credential_process`` auth
        # (see ``sandbox._seatbelt_profile``); a cron borrowing the tier
        # inherits that residual, which is the same exposure the command path
        # has always had there. A script that needs a host credential takes
        # the existing route an operator already approves per job: a vault
        # secret_env grant, which runs ``strict`` and injects the one approved
        # secret instead of exposing a store.
        sandbox_mode = "strict" if stdin_payload is not None else "cc"
        # ``seal_hidden_dirs=hidden``: a granted script has no business WRITING into
        # the crons dir or its own parent -- they are hidden so it cannot read a mutable
        # sibling, and on Linux a hidden dir's stand-in is a writable empty tmpfs, so a
        # write there would silently vanish on teardown. These masks carry no
        # writable window and the strict tier re-exposes no file into them (expose_files
        # is cc-only), so sealing their stand-in read-only is safe: a stray write now
        # fails loudly with EROFS instead of being lost. The seal is opt-in per mask, so
        # nothing else this spawn hides is affected.
        # The scratch ROOT is masked for every sandboxed process, so this run's
        # own directory is carved back out as a private read-write window.
        cron_scratch = _allocate_cron_scratch(job_id)
        sandboxed_argv, sandbox_cleanup = wrap_argv(
            argv,
            mode=sandbox_mode,
            extra_hidden_dirs=hidden,
            seal_hidden_dirs=True,
            extra_private_dirs=(str(cron_scratch),) if cron_scratch is not None else (),
        )
        if stdin_payload is not None and sandboxed_argv == argv:
            # On a host with no OS sandbox backend, the unsandboxed-exec
            # opt-in makes wrap_argv hand back the argv UNMODIFIED — the
            # crons-dir hiding above silently vanishes with it, and a granted
            # script could import an agent-rewritable sibling with the
            # secrets in reach. A grant's isolation guarantees are part of
            # what the operator approved, so refuse rather than degrade.
            return {
                "status": "error",
                "error": "❌ secret-bearing cron runs require an OS sandbox "
                "backend; this host runs scripts unsandboxed "
                "(sandbox_allow_unsandboxed_exec), where a grant's isolation "
                "cannot be enforced. Revoke the grant to run this script "
                "without secrets.",
            }

        # Build clean env: secrets (Slack tokens, owner id, internal secret)
        # are never inherited; the internal secret is passed via the 0600 file.
        clean_env = _clean_cron_env()
        if cron_scratch is not None:
            # The whole temp triple, not only the keys the gateway carried, so
            # a child under a unit with no TMPDIR stops defaulting to /tmp.
            clean_env.update(tmp_env(cron_scratch))
        # Operator-granted vault secrets are NOT placed in the child's execve
        # environment: /proc/<pid>/environ is the startup-env snapshot and is
        # readable by any same-UID process, so they travel in the stdin
        # payload instead and enter os.environ only after the child starts
        # (see the launcher). The name-deny filter still applies.
        clean_env["_KIROCREW_SECRET_FILE"] = secret_path
        # The child must dial the gateway the credential above was minted for:
        # same dial_port, resolved once above, not a second resolution here.
        clean_env["_KIROCREW_DIAL_PORT"] = str(dial_port)
        # Marks the script child for ``refuse_unaudited_on_dead_fs``: an ENOSYS SEL
        # write is fatal for THIS child, not "proceeding unaudited", and for it only.
        clean_env[CRON_SCRIPT_CHILD_ENV] = "1"
        # Give the child the SAME identity the gateway hands every agent
        # subprocess (acp/client.py injects KIROCREW_SESSION_KEY for agent crons
        # too): the strict resolver behind every state-mutating MCP tool
        # (mcp_core._resolve_session_key_strict) accepts exactly three sources --
        # the gateway-injected caller block, this env var, or KIROCREW_HOST_PID
        # plus its signed sidecar. A script cron had NONE of them: nothing routes
        # its direct MCP spawns through gatewayd, nobody publishes a sidecar for
        # its launcher pid, and this env was never set -- so on Linux the sandbox
        # launcher's KIROCREW_HOST_PID pointed at an unsigned pid and every write
        # was refused with "the signed pid mapping did not verify", while on
        # macOS/Windows there was no channel at all. Read-only calls were
        # unaffected, so the refusal surfaced only as a script that silently did
        # nothing. `cron:<job id>` is the key ScriptContext already presents to
        # the gateway over HTTP (X-Session-Key / caller_session) and the key
        # agent-cron sessions run under, so ownership and audit see one
        # principal per job regardless of which surface the job uses.
        clean_env["KIROCREW_SESSION_KEY"] = f"cron:{job_id}"
        # The key states an identity; the token PROVES it. Session-scoped gateway
        # routes accept a declared key only behind an attestation, and this is the
        # only one a script cron can carry, so its MCP children reach those routes
        # as this job instead of as a caller that merely holds the internal secret.
        script_session_token = _publish_script_session_token(clean_env, job_id)
        # Pre-resolve gh OUTSIDE the sandbox and pin its identity for the
        # child: the sandbox's single-uid user namespace maps every root-owned
        # path component to the overflow uid, so the child's own ownership
        # walk refuses ANY gh on the host. Empty when the host has no usable
        # gh -- scripts that never call gh are unaffected either way.
        clean_env.update(prevalidated_gh_env())

        sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
        # The spawn-in-flight window makes popen_limited's interpreter-ENOENT
        # backoff cancellable: without it a cancel arriving mid-backoff is
        # recorded nowhere, and the retry runs the cancelled job anyway.
        #
        # A refusal means this job is already spawning or running. Return WITHOUT
        # touching any spawn or cancellation state: that state belongs to the
        # other run, and clearing it here is exactly how a rerun eats the
        # cancel aimed at a run still in its backoff.
        #
        # Status is "skipped", NOT "error". This is a second overlap guard behind
        # the scheduler's own (which logs "previous execution still running,
        # skipping" and returns silently), covering the tail where a claimed
        # worker outlives its deadline: the first guard clears while the child
        # runs on. Beyond that tail it earns its place for a different reason --
        # it is what keeps the job-keyed cancel flag unambiguous. Every
        # cancellation surface here takes a job id ALONE, so two concurrent runs
        # of one job make "cancel this job" undecidable however the scheduler
        # arrived at them; refusing the overlap is what gives _CANCELLED_PROC_JOBS
        # a single owner. An overlapping wake is not a job defect, so it must not
        # count a failure strike toward auto-pause -- inflating strikes for a
        # transient condition is the harm this change exists to reduce.
        if not _begin_spawn(job_id):
            return {
                "status": "skipped",
                "error": "Another run of this job is already starting or running",
            }
        try:
            proc = popen_limited(
                sandboxed_argv,
                stdin=subprocess.PIPE if stdin_payload is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=clean_env,
                start_new_session=True,
                abort_retry=lambda: _spawn_cancelled(job_id),
                # Locale decoding, declared deliberate. A cron runs an ARBITRARY
                # command, so its output is in the host's encoding, not
                # necessarily UTF-8. Forcing encoding="utf-8" with
                # errors="replace" turned every non-UTF-8 byte into U+FFFD
                # BEFORE the output was persisted and delivered -- irreversible
                # corruption of the very thing the user asked to see. Locale
                # decoding is what their shell does, and it is what this call
                # did before the encoding gate was satisfied by pinning it.
                text=True,  # subprocess-encoding: locale
            )
        except Exception:
            # No child exists, so a cancel recorded against this spawn can never
            # be signalled -- clear it here rather than let it leak into the next
            # run of this job. Covers abort_retry's deliberate re-raise and a
            # genuinely broken install alike.
            if _abandon_spawn(job_id):
                return {"status": "cancelled", "error": "Cancelled by user"}
            raise
        cron_scratch_used = True
        if _finish_spawn(job_id, proc):
            # The cancel raced the successful spawn, so the child is live and was
            # never registered: this is the only place that can still stop it.
            # Reporting "cancelled" without this kill would claim the work was
            # stopped while it ran on and mutated state.
            _kill_proc_group(proc)
            _drain_after_kill(proc, job_id)
            # Reaped before its owner was recorded: the marker still names this
            # gateway, so the sweep would keep the dir. Nothing uses it now.
            cron_scratch_used = False
            return {"status": "cancelled", "error": "Cancelled by user"}
        try:
            if not _record_cron_scratch_owner(cron_scratch, proc):
                _kill_proc_group(proc)
                _drain_after_kill(proc, job_id)
                # The sweep skips a gateway-named or linked marker; the child is
                # reaped, so remove the dir here (rmtree never follows a link).
                cron_scratch_used = False
                return {"status": "error", "error": _SCRATCH_OWNER_ERROR}
            try:
                stdout, stderr = proc.communicate(
                    input=(stdin_payload + "\n") if stdin_payload is not None else None,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                # Popen.communicate does not kill the child on timeout
                # (unlike subprocess.run) — clean up before re-raising.
                _kill_proc_group(proc)
                _drain_after_kill(proc, job_id)
                raise
        finally:
            cancelled = _unregister_proc(job_id, proc)
        if cancelled:
            return {"status": "cancelled", "error": "Cancelled by user"}

        # Any non-zero exit is a failure, even with stdout: the launcher's own
        # code exits 0 after its envelope, so a non-zero exit means the run
        # failed, and a status-shaped stdout line cannot outvote it.
        if proc.returncode != 0:
            # Report the TERMINAL stderr context, not the head. A process that
            # dies hard leaves its diagnosis LAST -- the traceback is the final
            # thing written -- while whatever a startup path logged first (a
            # data-home migration warning, a deprecation notice, an import
            # banner) sits in front of it. Bounding from the head therefore
            # reports the noise and truncates the cause, and the operator reads
            # a cron failure whose message describes something that did not kill
            # the job.
            #
            # ``rstrip`` first so a trailing newline does not spend part of the
            # budget, and so an all-whitespace stderr still falls through to the
            # exit-code fallback rather than reporting blank text.
            #
            # Redact BEFORE bounding: slicing first would cut a credential that
            # straddles the 500-char boundary in half, and ``redact`` cannot
            # recognise the surviving fragment, so it would reach logs and the
            # persisted ``last_error`` unmasked. The two passes get DIFFERENT
            # inputs, matching what each costs and needs. The grant scrub runs
            # over the WHOLE capture: it is exact-substring replacement of
            # values the parent already holds -- O(len) scans, no base64
            # materialisation -- and a vault value has no shape, so a value
            # straddling any window edge would stop matching ``value in text``
            # and its fragment would leak with nothing downstream able to
            # recognise it. Pattern ``redact`` is the memory amplifier, so ITS
            # input is a TAIL window reaching ``_REDACT_STRADDLE_MARGIN`` back
            # past the kept region (see ``_REDACT_STRADDLE_MARGIN``).
            scrubbed = _scrub_grant_values(stderr.rstrip(), resolved_secret_env)
            tail = redact(_safe_tail_redaction_window(scrubbed, _MAX_SCRIPT_STDERR_TAIL))
            error_text = tail[-_MAX_SCRIPT_STDERR_TAIL:] if tail else f"exit {proc.returncode}"
            return {"status": "error", "error": error_text}

        try:
            parsed = json.loads(stdout.strip().split("\n")[-1])
            # The launcher's status JSON carries str(e) from the script's own
            # exception — the one child-diagnostic path that does NOT flow
            # through the stderr redaction above. Scrub granted values from
            # every diagnostic string field before the result reaches gateway
            # logs and the persisted last_error. ``status`` is exempt: the
            # launcher emits it from a closed vocabulary (ok/skip/done/report/
            # error) and the caller only compares it against those literals,
            # so it can neither carry a secret nor be rendered — while a vault
            # value that happens to equal one of those tokens would otherwise
            # rewrite a successful run into an unrecognised (failed) status.
            if resolved_secret_env and isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k != "status" and isinstance(v, str):
                        parsed[k] = _scrub_grant_values(v, resolved_secret_env)
            return parsed
        except (json.JSONDecodeError, IndexError):
            # Redact BEFORE truncating: slicing first could cut a credential at
            # the boundary, leaving its unredacted head in the diagnostic. Same
            # split as the stderr tail above: the grant scrub reads the WHOLE
            # capture (shapeless values, cheap exact replacement), pattern
            # ``redact`` reads a HEAD window overshooting the kept region by
            # ``_REDACT_STRADDLE_MARGIN``.
            scrubbed_out = _scrub_grant_values(stdout, resolved_secret_env)
            return {
                "status": "error",
                "error": (
                    "Bad output: "
                    f"{redact(scrubbed_out[: _MAX_BAD_OUTPUT_HEAD + _REDACT_STRADDLE_MARGIN])[:_MAX_BAD_OUTPUT_HEAD]}"
                ),
            }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"Script timed out after {timeout}s"}
    except SandboxUnavailableError as exc:
        # Same reasoning as run_command_sandboxed: a host with no sandbox backend
        # must surface a failed job carrying the remedy, not an escaping
        # exception the scheduler cannot attribute to this job.
        return {"status": "error", "error": f"{_SANDBOX_UNAVAILABLE_PREFIX}{exc}"}
    finally:
        retract_session_token(script_session_token)
        Path(launcher_path).unlink(missing_ok=True)
        Path(secret_path).unlink(missing_ok=True)
        if pinned_dir:
            shutil.rmtree(pinned_dir, ignore_errors=True)
        if not cron_scratch_used:
            _discard_unused_cron_scratch(cron_scratch)
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)


_MAX_COMMAND_OUTPUT = 65536  # 64KB cap

# Leads a cron failure caused by the fail-closed sandbox rather than by the job
# itself. The distinction matters to the reader: the job is fine, the host cannot
# isolate it, and the remedy is a config opt-in — which the wrapped message
# carries verbatim.
_SANDBOX_UNAVAILABLE_PREFIX = "❌ Cron could not run in an OS sandbox: "


def _resolve_command_shell() -> str | None:
    """Return an absolute path to a POSIX shell for ``sh -c`` command crons.

    Command crons are authored as POSIX shell one-liners (and vetted by
    ``mcp_cron._vet_shell_command`` under POSIX quoting), so cmd.exe is NOT a
    substitute — a missing shell must fail loudly rather than silently changing
    the command language. Returns ``None`` when no POSIX-strict shell can be
    found, which makes the caller refuse the job with a legible error.

    Windows always returns ``None``, and that is unchanged and deliberate:
    cmd.exe is not POSIX at all, and the Windows shell layout (Git-for-Windows's
    ``sh.exe``, its PATH, its trusted-path story) has none of the fixed
    system-shell guarantees the POSIX branch below relies on. ``script`` crons,
    whose body is scanned in full, remain the supported path there.

    On POSIX the candidate is probed rather than assumed, because a trusted
    ``sh`` may be bash: ``/bin/sh -> bash`` is the default on AL2023, RHEL and
    Fedora, and macOS's ``/bin/sh`` is bash-in-POSIX-mode. Such a shell is
    accepted when invoked so it refuses brace expansion — see
    ``_shell_is_posix_strict`` for the two forms and the order they are tried in.
    """

    # On Windows there is no shipped shell whose language matches what
    # mcp_cron._vet_shell_command was written against: cmd.exe is not POSIX at
    # all, and Git-for-Windows's sh.exe IS bash. Refuse rather than route the
    # vetted string through a shell that widens its language.
    if platform_compat.IS_WINDOWS:
        return None
    # POSIX: NEVER consult PATH (shutil.which("sh")). PATH may contain an
    # agent-writable directory that precedes /bin — an agent can plant
    # ``~/.local/bin/sh`` that fingerprints the probe input, passes the strict
    # test, then does something different when called with the real cron. Even
    # inside `cc`-mode isolation the agent-planted binary runs, and `cc` leaves
    # ``.ssh`` reachable, so this is a private-key-exposure vector. Instead,
    # walk a small fixed list of trusted system shell paths. Some hosts perform
    # brace expansion under the `sh` name (Linux `/bin/sh -> bash` is the default
    # on AL2023 / RHEL / Fedora; macOS /bin/sh is bash-in-POSIX-mode), so PROBE
    # the candidate: dash / ash / a real POSIX sh preserve the literal as
    # invoked, and a bash-as-sh preserves it once brace expansion is switched
    # off. The probe tries the plain form FIRST, so a genuinely POSIX-strict
    # shell resolves exactly as it always has and its argv is unchanged.
    for candidate in ("/bin/sh", "/usr/bin/sh"):
        if os.path.isfile(candidate) and _shell_is_posix_strict(candidate):
            return candidate
    return None


# Per-shell-path memoization for the POSIX-strict probe. The probe itself
# spawns a child, so caching it means each candidate is fingerprinted at most
# once per gateway process; a subsequent command cron with the same resolved
# shell does no extra work.
_POSIX_STRICT_CACHE: dict[str, bool] = {}
# Shells that need `+B` to stop brace-expanding, recorded BY THE PROBE so the
# executor runs the form the probe proved. Separate from the boolean cache above
# because "is this shell usable" and "how must it be invoked" are two answers,
# and collapsing them is what let the probe and the executor drift apart.
_BRACE_OFF_SHELLS: dict[str, bool] = {}
# bash's command-line spelling of `set +B` (brace expansion off). dash/ash reject
# it, which is exactly why it is never tried first: an unknown-option refusal
# would look like a failing shell.
_BRACE_OFF_FLAG = "+B"
# Serializes the check-probe-record sequence in ``_shell_is_posix_strict``. Up to
# ``_MAX_CRON_WORKERS`` command crons resolve the shell concurrently, and the two
# maps above are only coherent if one probe of a shell owns them from the cache
# miss to the record. Unserialized, a second probe that started on the same cold
# cache and then failed transiently would pop the brace-off record a first probe
# had just proved, and the first caller's executor would read the plain form and
# run with brace expansion ON. A cache hit is the steady state, so the lock is
# contended only on each shell's first probe.
_SHELL_PROBE_LOCK = threading.Lock()


def _argv_for_form(shell: str, command: str, brace_off: bool) -> list[str]:
    """Build the argv for one invocation form. The only place the form is spelled."""

    if brace_off:
        return [shell, _BRACE_OFF_FLAG, "-c", command]
    return [shell, "-c", command]


def _command_argv(shell: str, command: str) -> list[str]:
    """Build the argv that runs *command* under *shell*, in the PROVEN form.

    The single builder both ``_shell_is_posix_strict`` and
    ``run_command_sandboxed`` go through. Before this existed each wrote its own
    ``[shell, "-c", ...]`` literal, so a resolver that accepted a new invocation
    form would have left the probe proving a form the executor never used — the
    probe would still pass while the command ran under brace expansion.

    A shell absent from ``_BRACE_OFF_SHELLS`` gets the plain form, which is both
    the historical behaviour and the right default for a caller that resolved a
    shell without probing it (tests monkeypatch ``_resolve_command_shell``).
    """

    return _argv_for_form(shell, command, _BRACE_OFF_SHELLS.get(shell, False))


def _shell_is_posix_strict(shell: str) -> bool:
    """Return True iff *shell* can be invoked so it refuses brace expansion.

    Runs ``echo x.{a,a}`` under *shell* in an OS sandbox (strict tier, cron env)
    and requires the OUTPUT to be the literal ``x.{a,a}``. Two forms are tried,
    in this order, and the one that passes is recorded for the executor:

    1. ``<shell> -c ...`` — dash / ash / a real POSIX sh preserve the literal.
       Tried first so a POSIX-strict shell keeps its exact current argv.
    2. ``<shell> +B -c ...`` — bash's brace expansion switched off at the
       command line. A trusted-path bash-as-``sh`` (the Linux default) then
       satisfies the same property, instead of the whole feature being refused
       on the most common Linux configuration.

    Requiring the literal is what protects the vet gate
    (``mcp_cron._vet_shell_command``), which tokenizes the stored string once:
    a runtime re-expansion would widen what a deny-list can see. The gate also
    refuses brace-expansion SYNTAX at storage time, so a command cannot switch
    expansion back on and have anything left to expand — that refusal is what
    makes accepting form 2 safe, and removing either half re-opens the hole.

    The probe is SANDBOX-ROUTED as a defense-in-depth belt on the fixed
    trusted-path lookup in ``_resolve_command_shell``. If a future change ever
    widens that resolver to consult PATH again, the sandbox wrap here still
    denies an agent-planted shim the un-isolated execution it would need.
    """

    with _SHELL_PROBE_LOCK:
        cached = _POSIX_STRICT_CACHE.get(shell)
        if cached is not None:
            return cached
        for brace_off in (False, True):
            if _probe_one_form(shell, brace_off):
                # Record the form only once it has PASSED, and record nothing on the
                # way there: the executor reads this map, so a form written while
                # still being tested would be visible to a concurrent command cron.
                _BRACE_OFF_SHELLS[shell] = brace_off
                _POSIX_STRICT_CACHE[shell] = True
                return True
        # No form worked: leave no brace-off record behind for a shell this resolver
        # refuses, so a later caller cannot inherit the last form tried.
        _BRACE_OFF_SHELLS.pop(shell, None)
        _POSIX_STRICT_CACHE[shell] = False
        return False


def _probe_one_form(shell: str, brace_off: bool) -> bool:
    """Run the brace-expansion probe once, in the requested invocation form."""

    sandbox_cleanup: str | None = None
    try:
        argv, sandbox_cleanup = wrap_argv(
            _argv_for_form(shell, "echo x.{a,a}", brace_off), mode="strict"
        )
        # Same discipline as every other sandbox-routed spawn in this module
        # (test_every_routed_spawn_applies_resource_limits / _cgroup_scope): the
        # probe is a child process, so it observes the same fork-bomb / RSS
        # ceilings as a real command cron. run_limited applies them after exec,
        # and is a no-op on Windows where there are no POSIX rlimits.
        argv = cgroup_scope_argv(argv)
        proc = run_limited(
            argv,
            capture_output=True,
            text=True,
            timeout=5,
            env=_clean_cron_env(),
        )
        result = proc.returncode == 0 and proc.stdout.strip() == "x.{a,a}"
    except (OSError, subprocess.SubprocessError, SandboxUnavailableError):
        result = False
    finally:
        if sandbox_cleanup:
            try:
                os.unlink(sandbox_cleanup)
            except OSError:
                pass
    return result


def _no_command_shell_message() -> str:
    """The refusal a command cron gets when ``_resolve_command_shell`` finds nothing.

    Worded per platform because the two refusals have different causes and
    different remedies. On Windows it is by design and permanent. On POSIX it is
    this host: neither trusted ``sh`` passed the probe, and naming Windows there
    would send a macOS or Linux operator looking for a cause that does not apply
    to their machine.
    """

    if platform_compat.IS_WINDOWS:
        return (
            "❌ No POSIX shell available to run this command cron. Command "
            "crons execute with `sh -c` under POSIX-sh semantics (what the "
            "storage-time vet gate assumes); Windows ships no such shell "
            "(Git for Windows's sh.exe is bash and would widen the language "
            "past the vet). Use a script cron or an LLM `message` cron on "
            "this platform, or run the gateway under POSIX."
        )
    return (
        "❌ No usable POSIX shell to run this command cron. Command crons run "
        "only under /bin/sh or /usr/bin/sh (never $PATH), and the one used must "
        "pass a sandboxed probe proving it leaves `echo x.{a,a}` unexpanded when "
        "invoked as `sh -c` or `sh +B -c` (brace expansion would widen the "
        "command past what the storage-time vet gate checked). Neither passed on "
        "this host: the shell is missing, expands braces even with `+B`, or the "
        "OS sandbox refused to start the probe. Use a script cron or an LLM "
        "`message` cron until that is fixed. The probe result is kept for the "
        "life of the gateway process, so restart the gateway after fixing the "
        "shell."
    )


def run_command_sandboxed(
    command: str,
    timeout: int = 300,
    job_id: str | None = None,
    secret_env: dict[str, str] | None = None,
    secret_env_pin: str = "",
) -> dict:
    """Run a shell command in a sandboxed subprocess via wrap_argv().

    Returns: {"status": "ok"|"error"|"cancelled", "output": "...", "exit_code": N}

    ``secret_env``/``secret_env_pin`` exist only as a fail-closed guard:
    secret grants apply to SCRIPT jobs exclusively (a pin over the command
    TEXT cannot cover the bytes of a helper file the command invokes), and
    every product surface refuses to store one for a command job — so a
    non-empty grant here means the store was edited outside the product, and
    the run refuses rather than executing with or without the secrets.
    """
    if secret_env:
        return {
            "status": "error",
            "output": "❌ secret grants apply only to script jobs; this "
            "command job carries a grant the product could not have written — "
            "remove it from the cron store.",
            "exit_code": -1,
        }
    # Claimed BEFORE the shell probe, not merely before the spawn. Resolving the
    # shell (_resolve_command_shell -> _shell_is_posix_strict -> run_limited)
    # carries run_limited's own interpreter-ENOENT backoff, so on a cold
    # _POSIX_STRICT_CACHE it can sleep for seconds while this job is registered
    # NOWHERE. A cancel landing in that window found the job in neither
    # _SPAWNING_JOBS nor _RUNNING_PROCS, so kill_running_process returned False
    # and DISCARDED it -- and this function then launched the very command the
    # user cancelled, side effects and all. Holding the claim across the probe
    # makes such a cancel RECORDED; the pre-spawn check below turns it into a
    # launch that never happens.
    #
    # The secret-grant refusal above stays AHEAD of the claim: it fails closed
    # without doing any work, so it must not take a slot it would only release.
    #
    # A refusal returns WITHOUT touching spawn or cancellation state -- that state
    # belongs to the run already in flight -- and reports "skipped" rather than
    # "error" so an overlapping wake costs no auto-pause strike. See the script
    # path for the full rationale.
    if not _begin_spawn(job_id):
        return {
            "status": "skipped",
            "output": "⏭️ Another run of this job is already starting or running",
            "exit_code": -1,
        }
    # True while the claim is still ours to release. _finish_spawn and
    # _abandon_spawn each end the spawn and clear it; the finally below covers
    # every OTHER exit, all of which now happen with the claim held. Without that
    # release an early return -- no POSIX shell, wrap_argv fail-closing, any
    # raised error -- would leak the claim and _begin_spawn would then refuse
    # every future wake of this job for the life of the process.
    spawn_claimed = True
    # mode="cc" (not "standard"): the command string is fully model-supplied via
    # cron_add and executes outside the kiro-cli ACP permission/hook flow, so this
    # is a low-trust exec path. "cc" hides the credential dirs/files (.aws, .kube,
    # .netrc, .git-credentials, .npmrc, .pypirc, .kirocrew/.env) and scrubs the
    # agent-denied env keys, while deliberately leaving ~/.ssh reachable so a
    # legitimate command cron can still do git/scp/rsync over SSH. "strict" would
    # additionally hide ~/.ssh but break those workflows; the residual .ssh
    # exposure is covered by the storage-time deny-list (mcp_cron._vet_shell_command,
    # which blocks any .ssh reference) — the primary control. This sandbox is
    # defense-in-depth and is bypassed when the OS backend falls back to "none"
    # (e.g. macOS >= 26 — see _clean_cron_env).
    #
    # wrap_argv is INSIDE the try: on a host with no OS sandbox backend (every
    # Windows host) it fail-closes by raising, and outside the try that escaped
    # this function entirely — the scheduler's caller saw a bare exception
    # instead of a job it could mark failed, so the remedy never reached the user.
    sandbox_cleanup: str | None = None
    # Per-run temp dir, as in run_script_sandboxed.
    cron_scratch: Path | None = None
    cron_scratch_used = False
    try:
        # Inside the claim (see above) AND inside the try: the probe can raise on
        # a host with no OS sandbox backend, and that has to reach the handlers
        # below as a job the scheduler can mark failed.
        shell = _resolve_command_shell()
        if shell is None:
            return {"status": "error", "output": _no_command_shell_message(), "exit_code": -1}
        argv = _command_argv(shell, command)
        cron_scratch = _allocate_cron_scratch(job_id)
        sandboxed_argv, sandbox_cleanup = wrap_argv(
            argv,
            mode="cc",
            extra_private_dirs=(str(cron_scratch),) if cron_scratch is not None else (),
        )
        sandboxed_argv = cgroup_scope_argv(sandboxed_argv)  # cgroup DoS ceiling
        clean_env = _clean_cron_env()
        if cron_scratch is not None:
            # The whole temp triple, not only the keys the gateway carried, so
            # a child under a unit with no TMPDIR stops defaulting to /tmp.
            clean_env.update(tmp_env(cron_scratch))
        if _spawn_cancelled(job_id):
            # Cancelled while the probe above sat in its interpreter-ENOENT
            # backoff. Report it WITHOUT spawning: not launching is the entire
            # point, and a spawn-then-kill would still have run the command for
            # however long the signal took to land -- long enough to delete a
            # file or push a commit. No child exists, so the flag is consumed
            # here rather than left to leak into this job's next run.
            spawn_claimed = False
            _abandon_spawn(job_id)
            return {
                "status": "cancelled",
                "output": "Cancelled by user",
                "exit_code": -1,
            }
        try:
            proc = popen_limited(
                sandboxed_argv,
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=clean_env,
                start_new_session=True,
                abort_retry=lambda: _spawn_cancelled(job_id),
                # Locale decoding, declared deliberate. A cron runs an ARBITRARY
                # command, so its output is in the host's encoding, not
                # necessarily UTF-8. Forcing encoding="utf-8" with
                # errors="replace" turned every non-UTF-8 byte into U+FFFD
                # BEFORE the output was persisted and delivered -- irreversible
                # corruption of the very thing the user asked to see. Locale
                # decoding is what their shell does, and it is what this call
                # did before the encoding gate was satisfied by pinning it.
                text=True,  # subprocess-encoding: locale
            )
        except Exception:
            # See the script path: no child exists, so clear any recorded cancel
            # rather than let it leak into this job's next run.
            spawn_claimed = False
            if _abandon_spawn(job_id):
                return {
                    "status": "cancelled",
                    "output": "Cancelled by user",
                    "exit_code": -1,
                }
            raise
        spawn_claimed = False
        cron_scratch_used = True
        if _finish_spawn(job_id, proc):
            # Cancel raced the successful spawn; the child is live and
            # unregistered, so kill it rather than report a stop that never
            # happened.
            _kill_proc_group(proc)
            _drain_after_kill(proc, job_id)
            cron_scratch_used = False  # owner never recorded; see the script path
            # Same shape as the post-communicate cancellation below: a cancelled
            # command cron reports the CHILD's returncode (the signal that
            # stopped it), not a synthetic -1. Reporting -1 here diverged from
            # that contract for the raced-cancel case alone.
            return {
                "status": "cancelled",
                "output": "Cancelled by user",
                "exit_code": proc.returncode,
            }
        cancelled = False
        try:
            if not _record_cron_scratch_owner(cron_scratch, proc):
                _kill_proc_group(proc)
                _drain_after_kill(proc, job_id)
                cron_scratch_used = False  # see the script path
                return {"status": "error", "output": _SCRATCH_OWNER_ERROR, "exit_code": -1}
            try:
                output, stderr_out = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_proc_group(proc)
                _drain_after_kill(proc, job_id)
                return {
                    "status": "error",
                    "output": f"❌ Command timed out after {timeout}s",
                    "exit_code": -1,
                }
        finally:
            if job_id:
                cancelled = _unregister_proc(job_id, proc)
        if cancelled:
            return {
                "status": "cancelled",
                "output": "Cancelled by user",
                "exit_code": proc.returncode,
            }
        if len(output) > _MAX_COMMAND_OUTPUT:
            output = output[:_MAX_COMMAND_OUTPUT] + "\n\n[truncated — output exceeded 64KB]"
        if proc.returncode != 0:
            output = f"⚠️ Exit code {proc.returncode}\n\n{output}"
            if stderr_out:
                # A command that dies hard leaves its diagnosis last: report the
                # stderr tail, not the head, so a chatty startup warning can't
                # displace the terminal error. Redact the complete stderr BEFORE
                # truncating: slicing first could cut off a credential's
                # detectable prefix (e.g. the scheme of a token-bearing URL),
                # letting the raw secret tail through redaction.
                output += f"\n\nstderr:\n{redact(stderr_out.rstrip())[-1000:]}"
        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "output": output,
            "exit_code": proc.returncode,
        }
    except SandboxUnavailableError as exc:
        return {
            "status": "error",
            "output": f"{_SANDBOX_UNAVAILABLE_PREFIX}{exc}",
            "exit_code": -1,
        }
    except Exception as exc:
        return {"status": "error", "output": f"❌ Command failed: {exc}", "exit_code": -1}
    finally:
        if spawn_claimed:
            # Left this function without ever reaching _finish_spawn or
            # _abandon_spawn -- an early return or a raised error. Release the
            # claim, or _begin_spawn refuses every future wake of this job.
            _abandon_spawn(job_id)
        if not cron_scratch_used:
            _discard_unused_cron_scratch(cron_scratch)
        if sandbox_cleanup:
            Path(sandbox_cleanup).unlink(missing_ok=True)


# The rest of the module lives in the owners below, lowest layer first. A name an owner
# binds is read from it, and a patch through this module reaches every module holding
# the name (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New
# script-cron code goes to the owner of its job, or to a new ``cron_script_<role>.py``.
_OWNERS = (
    "kiro_crew.cron_script_child_env",
    "kiro_crew.cron_script_redaction",
    "kiro_crew.cron_script_paths",
    "kiro_crew.cron_script_grants",
    "kiro_crew.cron_script_procs",
    "kiro_crew.cron_script_mcp",
    "kiro_crew.cron_script_context",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # the names an owner serves, for type checkers and IDEs
    from kiro_crew import agent_scratch  # noqa: F401
    from kiro_crew.agent_discovery import _read_agent_spec  # noqa: F401
    from kiro_crew.atomic_write import replace_with_retry  # noqa: F401
    from kiro_crew.config.loader import read_local_secret  # noqa: F401
    from kiro_crew.config.paths import data_home, kiro_agents_dir  # noqa: F401
    from kiro_crew.cron_script_child_env import (  # noqa: F401
        _CRON_ENV_DENY,
        _GRANTED_ENV_KEYS,
        CHILD_PERSISTENCE_EXIT_CODE,
        CHILD_PERSISTENCE_PREFIX,
        _resolve_internal_secret,
        child_persistence_preflight,
    )
    from kiro_crew.cron_script_context import (  # noqa: F401
        _REFUSAL_CODE,
        Done,
        DoneError,
        Report,
        ReportError,
        ScriptContext,
        Skip,
        SkipError,
        _refusal_code,
        logger,
    )
    from kiro_crew.cron_script_grants import (  # noqa: F401
        _GRANT_EPOCHS_LOCK,
        _MAX_GRANTED_SCRIPT_BYTES,
        _SECRET_ENV_DENIED_EXACT,
        _SECRET_ENV_DENIED_PREFIXES,
        _SECRET_ENV_MAX_ENTRIES,
        _SECRET_ENV_NAME_RE,
        _SECRET_ENV_VAULT_NAME_RE,
        _grant_epoch,
        _grant_epochs_guard,
        _grant_epochs_path,
        _grant_pin_key,
        _pin_digest,
        _pin_payload,
        _read_grant_epochs,
        _resolve_secret_env,
        _write_grant_epochs,
        bump_grant_epoch,
        commit_grant_epoch,
        compute_secret_env_pin,
        delivery_fingerprint,
        grant_epoch_ids,
        peek_grant_epoch,
        validate_secret_env_grant,
    )
    from kiro_crew.cron_script_mcp import (  # noqa: F401
        _STDERR_TAIL_READ_SPAN,
        KeptMcpServers,
        McpToolClient,
        McpToolError,
        _resolve_mcp_server,
    )
    from kiro_crew.cron_script_paths import (  # noqa: F401
        _bundle_relative_spec,
        _split_script_spec,
        _trusted_script_bundle_roots,
    )
    from kiro_crew.cron_script_procs import (  # noqa: F401
        _CANCELLED_PROC_JOBS,
        _KILL_ESCALATION_GRACE_SECS,
        _PROCS_LOCK,
        _RUNNING_PROCS,
        _SPAWNING_JOBS,
        _resolve_safe_pgid,
        kill_running_process,
    )
    from kiro_crew.cron_script_redaction import (  # noqa: F401
        _PEM_BEGIN_LITERAL,
        _PEM_KEY_MARKER_RE,
        _PEM_SCAN_CARRY,
        _PEM_SCAN_CHUNK,
        _PEM_SEVERED_LABEL_RE,
        _mask_from_open_block,
        _may_end_inside_begin_marker,
        _pem_open_label,
        _pem_state_before,
    )
    from kiro_crew.env import sanitize_spec_env  # noqa: F401
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink  # noqa: F401
    from kiro_crew.json_line import parse_json_object_line  # noqa: F401
    from kiro_crew.loopback_http import loopback_urlopen  # noqa: F401
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV  # noqa: F401
    from kiro_crew.port_resolution import resolve_serving_port  # noqa: F401
    from kiro_crew.sandbox import (  # noqa: F401
        _AGENT_DENIED_ENV_KEYS,
        CANONICAL_TEMP_KEYS,
        refuse_unaudited_on_dead_fs,
    )
    from kiro_crew.secrets import SecretVault  # noqa: F401
    from kiro_crew.security import (  # noqa: F401
        _REDACTED_CREDENTIAL_TAG,
        _STREAM_HOLDBACK_JWT_MAX,
        is_unverifiable_path_refusal,
        sensitive_path_refusal,
    )
    from kiro_crew.sel import sel  # noqa: F401
    from kiro_crew.subprocess_utf8 import UTF8_TEXT  # noqa: F401
