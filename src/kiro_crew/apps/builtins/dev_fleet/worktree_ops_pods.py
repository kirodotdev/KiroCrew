"""Pod actions for one Dev Fleet worktree, and the per-worktree mutation lock.

Start, stop, restart, token, logs, status and provisioning of the pod a worktree runs
as -- the actions that reach a pod run behind the pin guard that refuses a pod pinned
to another checkout -- and the host-wide pod listing. ``_wt_lock`` is the lock a pod
start, a removal and a rebase each take for their worktree; it lives here, in the
lowest layer that takes it. Reached through ``worktree_ops``, its facade.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
from pathlib import Path
from typing import Any, TypeGuard

from kiro_crew import hooks
from kiro_crew.apps.builtins.dev_fleet import fleet_state, live, repository, runtime
from kiro_crew.executors import subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sandbox import sandboxed_spawn_argv, shielded_prepare_off_loop


def _pod_env() -> dict:
    """Environment for pod CLI subprocesses (allowlisted base + pod repo)."""
    return {**runtime._build_env(), "KIROCREW_POD_REPO": repository._repo()}


def _read_pin_strict(cfg: Any, name: str) -> tuple[bool, str | None]:
    """Read the pod's pinned CHECKOUT with failures PROPAGATED.

    Returns ``(env_file_exists, checkout_or_none)``. Unlike
    ``rt.read_env_file`` (which swallows OSError and returns ``{}``), a read
    failure raises — the caller must treat "file exists but cannot be
    positively read" as deny, never as "unpinned". The pin file must be a
    regular non-symlink file resolving inside the pods dir and must not be a
    sensitive path (the pods dir is agent-writable; a symlinked ``.env``
    must never pull a protected file into the gateway). Runs on the executor.
    """
    env_path = cfg.env_file(name)
    if not env_path.exists():
        return False, None
    # TOCTOU-safe: O_NOFOLLOW open + fstat validation of the DESCRIPTOR
    # (symlink/regular-file/containment/sensitivity checked atomically
    # against the opened inode, not a raceable path). Raises -> caller denies.
    data = hooks.safe_read_file_bytes_nolink(str(env_path), within_root=str(cfg.pods_dir))
    if data is None:
        # hooks gate refused (symlink/hardlink/containment/sensitive/IO):
        # "exists but cannot be positively read" is a DENY, never "unpinned".
        raise OSError(f"pin file refused by hooks read gate: {env_path}")
    # A leading UTF-8 byte-order mark (a Windows editor's "UTF-8") would
    # otherwise ride on the first key, so a CHECKOUT pin on line one reads as
    # absent -- the same mark ``rt._parse_env_text`` drops.
    text = data.decode("utf-8", errors="replace").removeprefix("\ufeff")
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        if key.strip() != "CHECKOUT":
            continue
        raw = val.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        return True, raw or None
    return True, None


async def _pod_checkout_guard(name: str) -> str | None:
    """Pod identities are global basenames while Dev Fleet scopes worktrees to
    MAIN_REPO. Before ANY pod operation, verify the pod's pinned ``CHECKOUT``
    matches THIS repo's worktree of that name — otherwise the operation would
    land on an unrelated repository's pod (stop it, delete its isolated HOME,
    or provision the wrong checkout). Returns an error string to refuse, or
    None to proceed. Fail closed on any uncertainty."""
    target, ferr = await repository._find_worktree(name)
    if target is None:
        return ferr or f"unknown worktree: {name!r}"
    cfg = runtime._load_cfg()
    if cfg is None:
        if not runtime._POD_AVAILABLE:
            # Pod subsystem entirely absent -> nothing to collide with; the
            # pod op itself will fail with its own clear error.
            return None
        # Pods exist on this host but config cannot be loaded -> we cannot
        # verify pod identity; fail closed.
        return "cannot load pod configuration to verify pod identity"
    loop = asyncio.get_running_loop()
    try:
        env_exists, pinned = await loop.run_in_executor(
            subprocess_executor(), _read_pin_strict, cfg, name
        )
    except Exception as exc:  # noqa: BLE001
        # Pin state exists but cannot be positively read -> deny, never
        # treat as "unpinned" (that ambiguity is exactly the cross-repo hole).
        return f"cannot verify pod checkout pin: {runtime._redact(str(exc))}"
    if not env_exists:
        # No pin file: only safe when no pod under this global name is
        # ACTIVE — an active unit with a missing pin is a foreign pod we
        # cannot attribute; acting on it would stop/expose another repo's
        # gateway. Fail closed on active or unverifiable.
        try:
            active = await loop.run_in_executor(subprocess_executor(), runtime.rt.active_names, cfg)
        except Exception as exc:  # noqa: BLE001
            return f"cannot verify active pods: {runtime._redact(str(exc))}"
        if name in active:
            return (
                f"pod {name!r} is active but has no checkout pin — refusing "
                "pod operation (unattributable pod identity)"
            )
        return None
    if not pinned:
        # Pin file EXISTS but carries no verifiable CHECKOUT -> ambiguous
        # pod identity; refuse rather than risk acting on a foreign pod.
        return (
            f"pod {name!r} has a pin file without a verifiable CHECKOUT — "
            "refusing pod operation (ambiguous pod identity)"
        )
    try:
        if Path(pinned).resolve() != Path(target["path"]).resolve():
            return (
                f"pod {name!r} is pinned to a different checkout — refusing "
                "cross-repository pod operation (basename collision)"
            )
    except OSError as exc:
        return f"cannot resolve checkout paths for pod guard: {runtime._redact(str(exc))}"
    return None


def _reclaim_pod_locked(
    cfg, name: str, expected_checkout: str, *, require_missing_checkout: bool = False
) -> tuple[str, str]:
    """Attribute pod *name* and tear it down as ONE locked transaction.

    Runs entirely inside ``rt.pod_name_mutex``, which is the cross-process flock
    every mutating pod path cooperates on. That is the whole point of this
    helper: checking the checkout pin in one process and then tearing down in
    another leaves a window where a concurrent ``pod up`` from a DIFFERENT
    checkout claims the same global basename, so the teardown would stop that
    pod and delete its isolated HOME. Reading the pin under the same lock the
    teardown holds closes it.

    Necessarily in-process rather than via the ``pod down`` CLI: the lock is held
    per open-file-description and ``stop_pod`` re-acquires it, so a caller that
    held it around a shell-out would block the very child it waits on.
    ``pod_name_mutex`` is reentrant WITHIN A THREAD, and this function is
    submitted to the executor as one callable, so ``stop_pod``'s own acquisition
    nests instead of deadlocking.

    *expected_checkout* identifies this repository's checkout for *name*. The
    missing-checkout teardown passes git's retained worktree record. When
    *require_missing_checkout* is true, the locked transaction refuses if that
    path is back on disk because a new pod may own the name.

    Mirrors :func:`_pod_checkout_guard`'s attribution rules and clears the
    checkout pin after a successful reclaim.

    Returns ``(outcome, detail)`` with outcome one of:
      ``reclaimed``  -- ours, torn down, HOME verified gone
      ``handed_over`` -- a new pod claimed the name mid-teardown; its state (and
                        its pin) is deliberately left untouched. Callers treat
                        this as a REFUSAL, not a success: a live pod may now be
                        running out of the very checkout they are about to
                        delete, and which pod holds the name is unknowable here.
      ``foreign``    -- not attributable to this checkout; nothing was touched
      ``failed``     -- attribution or teardown could not be completed
    """
    with runtime.rt.pod_name_mutex(cfg, name):
        try:
            env_exists, pinned = _read_pin_strict(cfg, name)
        except Exception as exc:  # noqa: BLE001
            # Pin state exists but cannot be positively read -> never treat as
            # "unpinned"; that ambiguity is the cross-repo hole itself.
            return "failed", f"cannot verify pod checkout pin: {runtime._redact(str(exc))}"
        if not env_exists:
            # No pin means the HOME cannot be attributed to ANY checkout, and
            # this path deletes it. ``_pod_checkout_guard`` allows an unpinned
            # name when no unit is live, which is right for OPERATING on a pod
            # the caller located; deletion is the stricter case and demands
            # POSITIVE attribution, because a same-basename leftover from
            # another checkout looks identical from here. Refusing costs an
            # unpinned orphan an automatic reclaim -- `pod prune` still takes
            # it -- while allowing it risks deleting another repo's data.
            return "foreign", (
                f"pod {name!r} has no checkout pin, so its HOME cannot be "
                "attributed to this checkout (unattributable pod identity)"
            )
        if not pinned:
            return "foreign", (
                f"pod {name!r} has a pin file without a verifiable CHECKOUT "
                "(ambiguous pod identity)"
            )
        try:
            mine = Path(pinned).resolve() == Path(expected_checkout).resolve()
        except OSError as exc:
            return "failed", f"cannot resolve checkout paths: {runtime._redact(str(exc))}"
        if not mine:
            return "foreign", (
                f"pod {name!r} is pinned to a different checkout " "(basename collision)"
            )
        if require_missing_checkout and Path(expected_checkout).exists():
            return "handed_over", (
                f"pod {name!r}: its checkout {expected_checkout!r} is back on disk, "
                "so a new pod may own the name"
            )
        cp = runtime.rt.stop_pod(cfg, name)
        if cp.returncode != 0:
            # Redacted like every other detail this helper returns: it reaches the
            # worktree-remove response and therefore the dashboard, and teardown
            # stderr can carry a path or credential from the pod's own output.
            err = runtime._redact((cp.stderr or "").strip())
            return "failed", err or f"stop rc={cp.returncode}"
        if runtime.rt.RECLAIMED_MARKER in (cp.stdout or ""):
            # The env file now pins the NEW pod's checkout -- clearing it would
            # strip that pod's identity.
            return "handed_over", f"pod {name!r} was reclaimed by a new pod mid-teardown"
        # Clear the pinned CHECKOUT=/SEED= so a later `up` re-resolves cleanly,
        # the same post-reclaim step the CLI performs.
        cfg.env_file(name).unlink(missing_ok=True)
        return "reclaimed", ""


def _is_number(value: object) -> TypeGuard[int | float]:
    """True for an int or a float claim; a bool (an int subclass) is not a number here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _token_windows(token: str) -> dict[str, int]:
    """How long a pod token's link and the session it opens last, in seconds.

    A pod token carries two limits: ``exp``, the link window inside which the raw
    token is accepted, and ``session_exp``, the lifetime of the session cookie the
    first request that carries it is given. They differ by design (minutes versus
    hours), so a caller that is told only one of them is told the wrong thing
    about the other.

    The claims are read WITHOUT verifying the signature: the token is signed with
    the pod's own secret, which this gateway never holds. That is sound because the
    numbers are display-only -- they tell the agent when to use the token, and the
    pod enforces the real limits whatever is reported here. Anything malformed
    yields an empty dict, so the caller reports no window rather than a wrong one.
    """
    try:
        encoded = token.split(".", 1)[0]
        data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    iat, exp, session_exp = data.get("iat"), data.get("exp"), data.get("session_exp")
    if not (_is_number(iat) and _is_number(exp) and _is_number(session_exp)):
        return {}
    try:
        link_secs = round(exp - iat)
        session_secs = round(session_exp - iat)
    except (ValueError, OverflowError):
        return {}
    if link_secs <= 0 or session_secs <= 0:
        return {}
    return {"link_ttl_secs": link_secs, "session_ttl_secs": session_secs}


def _mint_pod_token_locked(cfg, name: str, expected_checkout: str) -> dict:
    """Revalidate the boot's checkout and mint under one pod-name lock off-loop."""
    outcome, audit_error = "failure", "mint failed"
    resources = f"name={name} ttl=2h"
    try:
        with runtime.rt.pod_name_mutex(cfg, name):
            env_exists, pinned = _read_pin_strict(cfg, name)
            if not env_exists or not pinned:
                error = f"pod {name!r} has no verifiable checkout pin — token withheld"
            elif Path(pinned).resolve() != Path(expected_checkout).resolve():
                error = f"pod {name!r} is pinned to a different checkout — token withheld"
            else:
                error = ""
            if error:
                outcome, audit_error = "denied", "checkout pin mismatch; credential withheld"
                return {"ok": False, "code": "pod_checkout_mismatch", "error": error}
            port = runtime.rt.derive_port(cfg, name)
            resources = f"name={name} port={port} ttl=2h"
            token = runtime.rt.mint_token(cfg, name, "2h")
            outcome, audit_error = "allowed", ""
            return {"ok": True, "token": token}
    except runtime.rt.PodOwnershipUnproven as exc:
        outcome, audit_error = "denied", "ownership unprovable; credential withheld"
        return {
            "ok": True,
            "token": "",
            "warning": f"pod is up but token withheld: {runtime._redact(str(exc))}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "code": "pod_token_mint_failed",
            "error": f"pod is up but token mint failed: {runtime._redact(str(exc))}",
        }
    finally:
        try:
            runtime._sel().log_api_access(
                caller="dev_fleet",
                operation="pod.token",
                outcome=outcome,
                source="app",
                resources=resources,
                error=audit_error,
            )
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning("SEL audit failed for pod.token: %s", runtime._redact(str(exc)))


#: What a pod start answers while its worktree is being removed or rebased.
_POD_START_REFUSAL = (
    "refusing: this worktree is being removed or rebased -- start its pod once that finishes"
)


async def _pod_up(name: str) -> dict:
    """Start the worktree's pod while holding the worktree's lock and reservation.

    The pod runs from the checkout a removal deletes, and its unit goes active from
    the ``pod up`` child at a moment no in-process check can observe. So the start
    holds ``_wt_lock(name)`` from its guard through its verification: a removal or
    rebase already holding the lock refuses this start, and a removal that arrives
    mid-start refuses itself. The check and the acquisition are adjacent with no
    intervening await, as in ``_worktree_remove``.

    ``_wt_lock`` is per process, and an agent's start runs in the gateway
    (``agent_pod_api``) while every removal runs in the backend. So the start also holds
    :func:`live.pod_start_reservation` on the checkout for the same span: the gateway
    refuses a removal lease on it meanwhile, and refuses this start while a removal
    holds one. In the backend the reservation grants and records nothing.

    The name is resolved first, because ``_wt_lock`` keeps its row for good: a name
    that names no worktree gets no row. The guard under the lock resolves it again,
    so a removal that lands in between still refuses the start.
    """
    target, ferr = await repository._find_worktree(name)
    if target is None:
        return {"ok": False, "error": ferr or f"unknown worktree: {name!r}"}
    worktree_lock = _wt_lock(name)
    if worktree_lock.locked():
        return {"ok": False, "error": _POD_START_REFUSAL}
    async with worktree_lock:
        with live.pod_start_reservation(target["path"]) as reserved:
            if not reserved:
                return {"ok": False, "error": _POD_START_REFUSAL}
            return await _pod_up_locked(name)


async def _pod_up_locked(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    # Resolve the node toolchain off the loop before building the pod env:
    # `pod up` runs the provision chain (npm ci + vite) when asked to.
    await runtime._warm_build_path()
    cfg = runtime._load_cfg()
    cmd = runtime._find_cli() + ["pod", "up", name, "--json"]
    # A sandboxed child has its own user namespace, which the pod refuses to
    # certify as the local owner. Let the gateway mint only when it has config;
    # otherwise the CLI owns the whole operation (including Windows pods).
    # The Dev Fleet BACKEND is itself sandboxed, so a mint from there is refused
    # the same way: when the dashboard's Up/Restart buttons run this in the
    # backend, boot with --no-token and leave the token to the gateway's
    # owner-only POST /api/apps/dev-fleet/pod-token (the Open button).
    mint_here = not live.running_in_backend()
    expected_checkout = ""
    if cfg is not None:
        target, ferr = await repository._find_worktree(name)
        if target is None:
            return {"ok": False, "error": ferr or f"unknown worktree: {name!r}"}
        expected_checkout = target["path"]
        cmd.append("--no-token")
    rc, stdout, stderr = await runtime._run_cmd(
        cmd, cwd=repository._repo(), env=_pod_env(), timeout=180
    )
    if rc != 0:
        return {"ok": False, "error": runtime._redact(stderr or stdout)}
    # Post-start verification (symmetry with _pod_down): rc==0 is not proof the
    # pod is up. Confirm the unit is actually active, else fail closed rather
    # than flash a false "started" — the same false-success class as a false
    # "stopped", in the opposite direction.
    if runtime._POD_AVAILABLE and cfg:
        try:
            loop = asyncio.get_running_loop()
            active = await loop.run_in_executor(subprocess_executor(), runtime.rt.active_names, cfg)
            if name not in active:
                return {"ok": False, "error": "pod not active after start"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"cannot verify pod start: {runtime._redact(str(exc))}",
            }
    try:
        handle = json.loads(stdout)
    except ValueError:
        handle = {"output": stdout}
    # Use the same config that selects --no-token, so boot and mint cannot
    # disagree about which process supplies the credential.
    if cfg is not None and mint_here:
        minted = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _mint_pod_token_locked, cfg, name, expected_checkout
        )
        if not minted["ok"]:
            return minted
        handle.update(minted)
    # Both mint paths (the gateway's and the CLI's own) hand back the same token
    # shape, so the windows are read from whichever token the handle carries.
    if isinstance(handle.get("token"), str) and handle["token"]:
        handle.update(_token_windows(handle["token"]))
    handle["ok"] = True
    return handle


async def _pod_down(name: str) -> dict:
    target, ferr = await repository._find_worktree(name)
    if target is not None:
        guard = await _pod_checkout_guard(name)
        if guard:
            return {"ok": False, "error": guard}
        await runtime._warm_build_path()
        cmd = runtime._find_cli() + ["pod", "down", name]
        rc, stdout, stderr = await runtime._run_cmd(
            cmd, cwd=repository._repo(), env=_pod_env(), timeout=30
        )
        if rc != 0:
            return {"ok": False, "error": runtime._redact(stderr or stdout)}
        # Post-stop verification: a CLI exit 0 is NOT proof the unit stopped (a
        # broken `-m` entry point can no-op with rc 0, and a real stop
        # can still fail or time out). Re-check the live unit state and fail CLOSED
        # if the pod is still active -- mirrors the post-shutdown recheck in
        # _worktree_remove so "Stopped" is never reported for a pod still running.
        cfg = runtime._load_cfg()
        if runtime._POD_AVAILABLE and cfg:
            try:
                loop = asyncio.get_running_loop()
                active = await loop.run_in_executor(
                    subprocess_executor(), runtime.rt.active_names, cfg
                )
                if name in active:
                    return {"ok": False, "error": "pod still active after shutdown"}
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": f"cannot verify pod shutdown: {runtime._redact(str(exc))}",
                }
        return {"ok": True, "error": None}

    recorded, rerr = await repository._find_retained_worktree_path(name)
    if recorded is None:
        return {"ok": False, "error": ferr or rerr or f"worktree not found: {name!r}"}
    cfg = runtime._load_cfg()
    if cfg is None or not runtime._POD_AVAILABLE:
        return {"ok": False, "error": ferr or f"worktree not found: {name!r}"}
    try:
        outcome, detail = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            functools.partial(
                _reclaim_pod_locked,
                cfg,
                name,
                recorded,
                require_missing_checkout=True,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"cannot reclaim pod: {runtime._redact(str(exc))}"}
    if outcome == "reclaimed":
        return {"ok": True, "error": None}
    return {"ok": False, "error": detail}


async def _pod_restart(name: str) -> dict:
    """Restart a pod: down, then up only after a successful shutdown."""
    r = await _pod_down(name)
    if not r.get("ok"):
        return {"ok": False, "error": f"pod shutdown failed: {r.get('error')}"}
    return await _pod_up(name)


async def _pod_token(name: str) -> dict:
    if live.running_in_backend():
        # The pod's /api/token/local refuses a caller outside the gateway's
        # namespaces (member_owner_token_refused), and this backend is sandboxed.
        return {
            "ok": False,
            "code": "wrong_process",
            "error": (
                "the pod token must be minted in the gateway process (POST "
                "/api/apps/dev-fleet/pod-token), not in the Dev Fleet backend"
            ),
        }
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    cfg = runtime._load_cfg()
    if cfg is None:
        return {"ok": False, "error": "PodConfig unavailable"}
    try:
        loop = asyncio.get_running_loop()
        token = await loop.run_in_executor(
            subprocess_executor(), runtime.rt.mint_token, cfg, name, "2h"
        )
        port = await loop.run_in_executor(subprocess_executor(), runtime.rt.derive_port, cfg, name)
        return {"ok": True, "token": token, "url": f"http://127.0.0.1:{port}/?token={token}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _pod_logs(name: str, n: int = 120) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    cfg = runtime._load_cfg()
    if cfg is None:
        return {"ok": False, "error": "PodConfig unavailable"}
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(subprocess_executor(), runtime.rt.recent_journal, cfg, name, n)
    return {"ok": True, "logs": runtime._redact(raw)}


async def _pod_status(name: str) -> dict:
    """One pod's unit state, port and health, as ``kirocrew pod status`` reports it.

    Delegated to the CLI rather than recomposed from ``rt.is_active`` +
    ``rt.derive_port`` + ``rt.health`` so there is ONE definition of what a pod's
    status is. A second composition here would drift from the CLI's the first time
    either side learned a new health verdict (``HEALTH_FOREIGN`` is already one),
    and an agent comparing the two would be told different things about one pod.
    """
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    await runtime._warm_build_path()
    rc, stdout, stderr = await runtime._run_cmd(
        runtime._find_cli() + ["pod", "status", name, "--json"],
        cwd=repository._repo(),
        env=_pod_env(),
        timeout=30,
    )
    if rc != 0:
        return {"ok": False, "error": runtime._redact(stderr or stdout)}
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return {"ok": False, "error": "pod status returned unparseable JSON"}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "pod status returned an unexpected shape"}
    return {"ok": True, **parsed}


async def _pod_ls() -> dict:
    """Every pod ACTIVE on this host, not only this repo's.

    Deliberately unscoped: the answer's whole use is collision reasoning (which
    ports are taken, what is already running), and a list filtered to one repo's
    worktrees would omit exactly the pod that explains a refusal. Read-only and
    name/port/health only -- it exposes no other checkout's contents.

    ``repository._repo()`` raises when no main checkout is configured. That is NOT
    caught here: it is raised on every branch of every pod verb (cwd, ``_pod_env``,
    and inside ``_pod_checkout_guard``'s discovery chain), so the agent surface
    catches it once at the handler instead of each helper catching its own branch.
    """
    await runtime._warm_build_path()
    rc, stdout, stderr = await runtime._run_cmd(
        runtime._find_cli() + ["pod", "ls", "--json"],
        cwd=repository._repo(),
        env=_pod_env(),
        timeout=30,
    )
    if rc != 0:
        return {"ok": False, "error": runtime._redact(stderr or stdout)}
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return {"ok": False, "error": "pod ls returned unparseable JSON"}
    if not isinstance(parsed, list):
        return {"ok": False, "error": "pod ls returned an unexpected shape"}
    return {"ok": True, "pods": parsed}


async def _pod_provision(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    # Check, start, and record under ONE lock — releasing between the check
    # and the record lets two queued requests both observe "no active run".
    async with fleet_state._PROVISION_LOCK:
        prev = fleet_state._PROVISION_INFLIGHT.get(name)
        if prev:
            async with runtime._RUNS_LOCK:
                running = runtime._RUNS.get(prev, {}).get("status") == "running"
            if running:
                return {"ok": False, "error": "provision already running", "run_id": prev}
        await runtime._warm_build_path()
        p_argv, p_env, p_cleanup = await shielded_prepare_off_loop(
            functools.partial(
                sandboxed_spawn_argv,
                runtime._find_cli() + ["pod", "provision", name],
                "strict",
                env=_pod_env(),
            ),
            executor=subprocess_executor(),
        )
        rid = await runtime._start_run(
            "provision " + name,
            p_argv,
            cwd=repository._repo(),
            env=p_env,
            cleanup_paths=[p_cleanup] if p_cleanup else None,
            # Provisioning builds .venv and the SPA dist INSIDE the worktree
            # that `du -sm` measures, so a completed (or killed-partway) run
            # materially changes disk use: drop the disk cache's freshness
            # stamp at every terminal state so the next /disk poll
            # re-aggregates instead of serving pre-provision totals.
            on_finish=fleet_state._disk_invalidate,
        )
        fleet_state._PROVISION_INFLIGHT[name] = rid
    return {"ok": True, "run_id": rid}


async def _pod_provision_dismiss(name: str, run_id: str) -> dict:
    """Forget one terminal provision run without racing a replacement run."""
    async with fleet_state._PROVISION_LOCK:
        if fleet_state._PROVISION_INFLIGHT.get(name) != run_id:
            return {"ok": True, "dismissed": False}
        async with runtime._RUNS_LOCK:
            if runtime._RUNS.get(run_id, {}).get("status") == "running":
                return {"ok": False, "error": "cannot dismiss a running provision"}
        fleet_state._PROVISION_INFLIGHT.pop(name, None)
    return {"ok": True, "dismissed": True}


# Per-worktree mutation locks: two concurrent /rebase requests for the same
# checkout could both pass the clean-state check, then one's failure path
# would `rebase --abort` the OTHER's in-flight rebase.
_WT_LOCKS: dict[str, LoopBoundLock] = {}


def _wt_lock(name: str) -> LoopBoundLock:
    return _WT_LOCKS.setdefault(name, LoopBoundLock())
