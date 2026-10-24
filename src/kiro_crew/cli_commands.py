"""CLI subcommand handlers: tailnet, telemetry, secrets and pod, and the owners' facade."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import importlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import stat
import sys
import time as _time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import Error as StdlibSQLiteError
from typing import TYPE_CHECKING, NoReturn
from zoneinfo import ZoneInfo

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import (
    __version__,
    app_lifecycle_client,
    beacon,
    crew_teams,
    model_registry,
    platform_compat,
)
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.agent import reset_agent_model
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.apps.backend import recorded_backend_port
from kiro_crew.apps.bridges import (
    SessionPointerCleanup,
    deregister_app,
    deregister_app_crons_from_service,
    discard_app_session_pointers,
    register_app,
    register_app_crons_with_service,
)
from kiro_crew.apps.manager import (
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    list_apps,
    trust_grant_removal_blocked,
    uninstall_app,
)
from kiro_crew.apps.plugin_import import (
    PluginImportError,
    convert_plugin_package,
    find_plugin_manifest,
    normalize_app_name,
    read_manifest_name,
)
from kiro_crew.apps.scaffold import scaffold_app
from kiro_crew.cli_server import _marker_port, resolve_client_port
from kiro_crew.config import config_dir
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    WorkspaceDirUnusable,
    build_provider_factory,
    coerce_dict_section,
    config_local_path,
    config_path,
    materialize_workspace_dir,
    read_config_for_update,
    read_config_text,
    read_local_secret,
    update_config_locked,
)
from kiro_crew.config.sections import normalize_agent_model
from kiro_crew.cron import (
    _JOB_TIMEOUT_SECS,
    _SUBPROC_CLEANUP_ALLOWANCE_SECS,
    CronSchedule,
    CronService,
    CronStoreBusy,
    CronStoreUnreadable,
    format_schedule,
    get_local_tz,
    is_valid_timezone,
    lookup_cron_folder_id,
    parse_time_string,
    validate_managed_by,
)
from kiro_crew.cron_script import resolve_script_path
from kiro_crew.cron_trigger import trigger_cron_job
from kiro_crew.dashboard import tailnet, tailnet_serve
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.embeddings import (
    get_shared_embedder,
    make_sync_embed_fn,
    model_file_present,
    store_embedding_space_is_stale,
)
from kiro_crew.eval.judge import LLMJudge
from kiro_crew.eval.runner import EvalRunner, format_results, score_by_dimension
from kiro_crew.eval.scenario import AssertionType, load_scenario, load_scenarios
from kiro_crew.external_text import external_text_requires_redaction
from kiro_crew.hidden_leaf_staging import EnvStagingRefused
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import safe_read_file
from kiro_crew.learn import LessonStore
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.mcp_cron import (
    _vet_cron_capability_governance,
    _vet_script_file,
    _vet_shell_command,
)
from kiro_crew.member_memory_auth import require_member_memory_creation
from kiro_crew.member_workspace import (
    MemberWorkspaceUnavailable,
    provision_member_workspace,
    retire_unpublished_workspace,
)
from kiro_crew.members import (
    MemberNameError,
    check_crew_label,
    key_new_crew,
    validate_member_name,
)
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    UnknownMemoryStore,
    memory_store_binding_defect,
    memory_store_namespace_lock,
    memory_store_version,
    named_store_or_empty,
    owned_store_path,
    persist_member_config,
    provision_member_memory,
    require_memory_store,
    resolve_declared_store,
    retire_unpublished_allocation,
)
from kiro_crew.platform import redact_log_via_context
from kiro_crew.port_resolution import resolve_client_port_ex
from kiro_crew.project_scope import scope_is_admissible, scope_selector_is_inadmissible
from kiro_crew.secrets.migrate import (
    MigrationConflictError,
    format_report,
    migrate_env_secrets,
)
from kiro_crew.security import (
    BUILTIN_DENIED_RULES,
    BUILTIN_DENY_PATTERNS,
    is_sensitive_path,
    redact,
    redact_credentials,
    redact_exfiltration_urls,
    scan_history,
    scan_memory,
)
from kiro_crew.sel import sel
from kiro_crew.subagent_wait_reasons import queued_wait_text
from kiro_crew.terminal_safe import _TERMINAL_CTRL_RE, safe_terminal_line
from kiro_crew.validation import (
    _AGENT_NAME_RE,
    _MODEL_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    CRON_ADD_SCHEMA,
    MAX_SHORT_STRING,
    TEMPLATE_NAME_RE,
    WORKSPACE_NAME_RE,
    normalize_lesson_category,
)
from kiro_crew.vector_memory import (
    LessonWriteOutcome,
    VectorMemoryStore,
    _lesson_display_text,
    _lesson_scope,
    _lesson_scope_unusable,
    declared_store,
)


def _pod(args: argparse.Namespace) -> None:
    """Dispatch ``kirocrew pod <verb>`` to the pod verb layer (isolated worktree
    test instances)."""
    from kiro_crew.pod.cli import dispatch

    dispatch(args)


def _container_valued_sections() -> dict[str, type]:
    """Top-level config keys the model expects to be a JSON object or array.

    Read off ``KiroCrewConfig``'s own fields rather than hardcoded, so most
    sections added later are covered without an edit here. Which branch does that
    reading is worth knowing, because it bounds the "most": ``config/loader_config.py``
    carries ``from __future__ import annotations``, so every ``field.type``
    arrives as a STRING and the string branch is the one that runs. It matches on
    the annotation's TEXT — a ``list`` prefix, a ``dict`` prefix, a ``Config``
    suffix for a nested config — so a section spelled another way
    (``Optional[list[...]]``, ``Mapping[str, str]``, a nested dataclass not named
    ``*Config``) is not recognised and gets no guard. A new section in one of
    those spellings needs a look here. So does dropping that future import from
    ``config/loader_config.py``: ``field.type`` would then arrive as a resolved object,
    the ``isinstance`` guard would skip every field, and this map would come back
    empty — which stops the model-derived half of the guard below, though not the
    hardcoded ``dashboard.tailscale`` check after it. That is not a silent loss:
    ``test_tailnet_cli.py``'s section-guard tests go red on an empty map.

    Both shapes matter: a nested-config or mapping field must be an object, and a
    ``list[...]`` field must be an array — the loader *iterates* the latter, so a
    scalar there is an uncaught ``TypeError`` rather than a merge that loses data.
    """
    out: dict[str, type] = {}
    for field in dataclasses.fields(KiroCrewConfig):
        ann = field.type
        if not isinstance(ann, str):
            continue
        if ann.startswith("list"):
            out[field.name] = list
        elif ann.endswith("Config") or ann.startswith("dict"):
            out[field.name] = dict
    return out


def _assert_config_sections_are_objects(raw: dict) -> None:
    """Refuse a config whose section values have the wrong shape, before anything loads it.

    Not just the sections this command writes: this runs ahead of
    ``KiroCrewConfig.load()``, and ``load()`` is itself destructive on a file it
    cannot parse — its migration write-back **rewrites the file**, so a section it
    chokes on is replaced by defaults merely because a read-only command like
    ``tailnet status`` was run. ``{"slack": 5}`` was enough to destroy the operator's
    Slack settings that way, and ``{"registries": 5}`` is worse: the loader iterates
    that field, so a scalar ends the command in an uncaught ``TypeError``.

    Coercing a wrong type would discard whatever the operator had there and still
    report success, so a wrong shape is a refusal, never something to normalise.
    """
    for name, expected in sorted(_container_valued_sections().items()):
        value = raw.get(name)
        if value is None or isinstance(value, expected):
            continue
        want = "an object" if expected is dict else "an array"
        raise ConfigReadError(
            f'"{name}" is {type(value).__name__}, not {want}; refusing to run '
            "because loading this file would replace it with defaults"
        )
    tailscale = (raw.get("dashboard") or {}).get("tailscale")
    if tailscale is not None and not isinstance(tailscale, dict):
        raise ConfigReadError(
            f'"dashboard.tailscale" is {type(tailscale).__name__}, not an object; '
            "refusing to replace it"
        )


def _tailnet(args: argparse.Namespace) -> None:
    """Publish, withdraw, or inspect tailnet dashboard access (``kirocrew tailnet``).

    The command that was missing. Reaching the dashboard from another device on
    your tailnet has always taken **two** independent steps — publish it with
    ``tailscale serve``, and tell the gateway to trust the resulting origin — and
    Kiro Crew only ever did the second. Doing one without the other is the failure
    this exists to remove: publish without trusting and every request is refused
    by the Origin check with a bare 403; trust without publishing and there is
    nothing on the tailnet to open.

    So ``up`` does both, in the order that cannot leave a half-state visible: it
    publishes first and only records the config once publishing succeeded. A
    config write followed by a failed publish would leave a host claiming tailnet
    access is on with nothing serving it.
    """
    action = getattr(args, "tailnet_action", None) or "status"
    # Validate the raw file BEFORE anything calls ``KiroCrewConfig.load()``, for
    # EVERY action. ``load()`` performs a migration write-back, so a config that is
    # valid JSON but wrongly typed (``{"dashboard": 5}``) gets normalised — and
    # therefore silently rewritten — by the mere act of reading it. That makes even
    # ``status`` a write, which is indefensible for a command that reports state.
    #
    # An earlier revision guarded only ``up``, reasoning that refusing to *report* or
    # to *withdraw* over a malformed config would be the worse failure. That reasoning
    # had a hole: both paths need the dashboard port, which resolves through
    # ``resolve_client_port`` → ``KiroCrewConfig.load()``, so there is no version of
    # them that reads the file without rewriting it. Given the choice between
    # rewriting the operator's config and declining, declining wins — and withdrawal
    # stays ACHIEVABLE because the refusal prints the exact daemon command.
    #
    # BOTH files, not just the base one. ``load()`` merges ``config.local.json`` over
    # ``config.json``, so a wrongly-typed section in the overlay reaches the loader
    # just as surely -- `config set --local registries 5` is enough -- and a scalar
    # where a list is expected is iterated, ending the command in a traceback rather
    # than a refusal. Guarding only the base file left the overlay as an open door to
    # the very failure the guard exists to prevent.
    bad_path: Path | None = None
    try:
        for candidate in (config_path(), config_local_path()):
            bad_path = candidate
            if candidate.exists():
                _assert_config_sections_are_objects(read_config_for_update(candidate))
        bad_path = None
    except ConfigReadError as exc:
        print(
            f"❌ {bad_path} is not usable ({exc}); refusing to continue, because "
            "even reading it would rewrite it. Fix that file, then retry.",
            file=sys.stderr,
        )
        if action == "down":
            print(
                "   To withdraw right now without touching the config, run: "
                f"`tailscale serve --https {tailnet_serve.SERVE_HTTPS_PORT} "
                f"--set-path={tailnet_serve.SERVE_MOUNT} off`",
                file=sys.stderr,
            )
        sys.exit(1)
    cfg = KiroCrewConfig.load()
    # Evidence before intent. ``resolve_client_port`` ranks the configured
    # ``dashboard.url`` ABOVE the run marker, which is right for a client that wants
    # to talk to the dashboard the operator configured — and wrong here. If the
    # configured port was occupied and the gateway moved (``--port``), publishing in
    # front of the configured port aims `tailscale serve` at whatever unrelated
    # loopback service now holds it, exposing it on the tailnet. So the verified run
    # marker wins: it only reports a port where a Kiro Crew gateway process is actually
    # listening (`_gateway_owns_port`, and it refuses when several are up), which is
    # evidence, whereas ``dashboard.url`` is a statement of intent.
    #
    # An explicit ``--port``/``KIROCREW_PORT`` still wins over both, because that is
    # the operator naming the target directly — hence the marker is consulted only
    # when neither is set.
    # An explicit --port outranks everything: it is the operator naming the target,
    # which no discovery heuristic should override.
    port = int(getattr(args, "port", None) or 0)
    port_source = "the --port you gave" if port else ""
    if not port and os.environ.get("KIROCREW_PORT") is not None:
        port_source = "KIROCREW_PORT"
    if not port and not port_source:
        try:
            port = _marker_port() or 0
        except Exception:  # pragma: no cover - discovery must never break the command
            port = 0
        if port:
            port_source = "the running gateway's run marker"
    if not port:
        # ``resolve_client_port`` also carries the guard for a non-string
        # ``dashboard.url`` (user-editable JSON can hold ``"url": 123``, and urlparse
        # raises TypeError on it), so it stays the fallback rather than a hand-rolled
        # parse that would have to repeat that guard.
        port = resolve_client_port(None)
    enabled = bool(cfg.dashboard.tailscale.enabled)

    if action == "up" and not port_source:
        # Publishing needs EVIDENCE about the port, not a default. Every source above
        # is evidence -- an explicit flag/env is the operator naming the target, and
        # the run marker only reports a port a Kiro Crew gateway is actually listening
        # on. `resolve_client_port` is not: it falls back to the configured
        # `dashboard.url` (or the built-in default) whether or not anything answers
        # there. `tailscale serve` does not care what is behind the port -- so if the
        # gateway is down, or moved after its configured port was taken, publishing
        # that number puts WHATEVER now holds it on the tailnet, for every device on
        # it. A private service exposed tailnet-wide is not a recoverable mistake, so
        # this refuses rather than guesses. `status` and `down` still accept the
        # fallback: one only reports, and the other checks mount ownership before
        # removing anything.
        print(
            f"❌ Cannot tell which port the dashboard is on, so refusing to publish "
            f"{port} — nothing is verified to be listening there, and `tailscale "
            f"serve` would expose whatever is. Start the dashboard "
            f"(`kirocrew dashboard`) and re-run, or name the port yourself with "
            f"`kirocrew tailnet up --port <port>`.",
            file=sys.stderr,
        )
        sys.exit(1)

    if action == "status":
        pinned = tailnet.is_governance_pinned_off()
        # A LIVE read is correct here and would be wrong in the dashboard's status
        # endpoint. This command reports what the machine can do next; the
        # endpoint reports what the running server already trusts, which is the
        # startup value. Conflating them is how "resolvable" gets rendered as
        # "in the allowlist".
        name = tailnet.self_dns_name()
        state = tailnet_serve.serve_state(port)
        print("👻 Tailnet dashboard access")
        if pinned:
            print(
                "   Policy:     PINNED OFF by your administrator " "(capabilities.tailnet_origin)"
            )
        print(
            f"   Trust:      {'enabled' if enabled else 'disabled'} "
            f"(dashboard.tailscale.enabled)"
        )
        print(f"   Name:       {name or '— (no tailnet name resolvable right now)'}")
        published = state.published
        label = {True: "yes", False: "no", None: "unknown"}[published]
        print(f"   Published:  {label} — {state.detail}")
        if name and published is True:
            print(f"   URL:        https://{name}")
        if name and enabled and published is not True:
            print("   Next:       kirocrew tailnet up")
        return

    if action not in ("up", "down"):
        print(f"❌ Unknown tailnet action: {action}", file=sys.stderr)
        sys.exit(1)

    if action == "down":
        result = tailnet_serve.unpublish(port)
        print(("✅ " if result.ok else "❌ ") + result.detail)
        if result.ok:
            # The trust setting is deliberately left ON. It contributes one origin
            # that nothing can reach while serve is off, so clearing it would be
            # an unrequested second change — and would force a gateway restart to
            # undo a withdrawal that took effect immediately.
            print(
                "   dashboard.tailscale.enabled is unchanged; the trusted origin "
                "is unreachable while serve is off."
            )
        sys.exit(0 if result.ok else 1)

    if not enabled:
        # Checked BEFORE publishing, and never written. Three reasons this is a
        # check rather than a config write:
        #
        # 1. A read-modify-write of the shared config cannot be made atomic from a
        #    second process. Every construction tried here -- a caller-side
        #    fingerprint, a lock plus compare-and-swap inside the shared writer, a
        #    lock plus digest local to this command -- leaves some window where a
        #    dashboard save landing mid-flight is replaced by our older snapshot, or
        #    (when the lock went into the shared writer) blocks the gateway's event
        #    loop. Closing it needs one primitive that ALL ~29 writers take, which is
        #    its own change, not this feature.
        # 2. Failing here beats the alternative ordering. Writing after publishing
        #    leaves a published-but-untrusted dashboard whenever the write fails --
        #    reachable on the tailnet and answering 403, which is the confusing state
        #    this command exists to eliminate.
        # 3. The cost is paid once per machine, not per invocation. After the operator
        #    enables the setting, `kirocrew tailnet up` is a single command forever;
        #    the one-time step is the same `config set` they would run anyway.
        #
        # `cfg` is the EFFECTIVE value, so an overlay in config.local.json that
        # disables this is caught here too -- printing "published" while the gateway
        # will still refuse the origin is the exact false promise to avoid.
        print(
            "❌ dashboard.tailscale.enabled is false, so the gateway would refuse "
            "your tailnet origin even once published — refusing to publish a "
            "dashboard that would answer 403.\n"
            "   Enable it once, then re-run this command:\n"
            "     kirocrew config set dashboard.tailscale.enabled true\n"
            "   (If config.local.json disables it, set it there instead: "
            "`kirocrew config set --local dashboard.tailscale.enabled true`.)",
            file=sys.stderr,
        )
        sys.exit(1)

    result = tailnet_serve.publish(port)
    if not result.ok:
        print(f"❌ {result.detail}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ {result.detail}")

    name = tailnet.self_dns_name()
    if name:
        print(f"👻 URL:        https://{name}")
    else:
        print(
            "⚠️  No tailnet name is resolvable right now, so the gateway will not "
            "trust anything on restart. Check `tailscale status`."
        )
    # Said unconditionally, including when the switch was already on: the origin
    # is resolved once at startup, so a gateway that booted before this command
    # has an allowlist that does not contain the name yet.
    print("👻 Restart the gateway for the tailnet origin to be trusted.")


def _telemetry(args: argparse.Namespace) -> None:
    """Inspect or toggle the anonymous usage beacon (``kirocrew telemetry``).

    ``status`` is read-only and never materializes an install id. ``disable`` /
    ``enable`` persist ``telemetry.beacon_enabled`` to config.json, so the choice
    survives restarts and upgrades — an env-var-only opt-out would silently lapse
    the next time the user launched from a different shell.
    """
    action = getattr(args, "telemetry_action", None) or "status"
    cfg = KiroCrewConfig.load()

    if action == "status":
        print(
            beacon.format_status(
                beacon.status(
                    cfg.telemetry.beacon_endpoint,
                    enabled=cfg.telemetry.beacon_enabled,
                    app_version=__version__,
                    acked=cfg.dashboard.privacy_acked,
                )
            )
        )
        return

    if action not in ("disable", "enable"):
        print(f"❌ Unknown telemetry action: {action}", file=sys.stderr)
        sys.exit(1)

    want = action == "enable"
    # Refuse a re-enable an enterprise ceiling has pinned off, mirroring the
    # dashboard PATCH route's 403. Without this the CLI would write
    # beacon_enabled: true and print "ENABLED" on a host where should_send()
    # blocks every heartbeat — the exact false-promise-on-a-privacy-control this
    # command's overlay check below already exists to prevent. Only the ENABLE
    # direction is gated: writing false is always allowed (tightest-wins).
    # audit_tool: this is an ENFORCEMENT decision (it refuses a write), so it
    # routes through the audited seam and lands a governance_decision SEL record —
    # same disposition as the send gate. A distinct tool name per call site keeps
    # the trail readable about WHICH control refused.
    if want and beacon.is_governance_pinned_off(audit_tool="telemetry_enable_cli"):
        print(
            "❌ The anonymous beacon is pinned OFF by your administrator's "
            "security policy (capabilities.telemetry).",
            file=sys.stderr,
        )
        print(
            "   Not writing config.json — the setting would have no effect.",
            file=sys.stderr,
        )
        sys.exit(1)
    path = config_path()

    def _mutate_telemetry(data: dict) -> dict:
        """Apply telemetry toggle inside the config lock."""
        if not isinstance(data, dict):
            # Should not happen (read_config_for_update rejects non-objects),
            # but guard defensively.
            print(
                f"❌ {path} is not a JSON object ({type(data).__name__}); refusing to "
                "overwrite it. Fix or move the file, then retry.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Same rule as the whole-file check above, applied per section: coercing a
        # non-object section to {} would DISCARD whatever the user had there and then
        # print success. Absent is fine (create it); present-but-wrong-type is a
        # refusal, because this command cannot know what the value was meant to be.
        sections: dict[str, dict[str, object]] = {}
        for name in ("telemetry", "dashboard"):
            existing = data.get(name)
            if existing is None:
                sections[name] = {}
                continue
            if not isinstance(existing, dict):
                print(
                    f'❌ {path} has a non-object "{name}" value '
                    f"({type(existing).__name__}); refusing to overwrite it. Fix or "
                    "remove it, then retry.",
                    file=sys.stderr,
                )
                sys.exit(1)
            sections[name] = existing

        sections["telemetry"]["beacon_enabled"] = want
        data["telemetry"] = sections["telemetry"]
        # Running this command IS the informed choice the first-run chapter exists to
        # collect, so record the ack. Otherwise `telemetry enable` on a fresh
        # headless install would write beacon_enabled: true and still send nothing,
        # because the first-egress gate would keep waiting for a dashboard screen the
        # user may never open.
        sections["dashboard"]["privacy_acked"] = True
        data["dashboard"] = sections["dashboard"]
        return data

    try:
        update_config_locked(path, mutate=_mutate_telemetry, fsync=True, stamp_meta=False)
        # restrict_to_owner: the atomic write creates a NEW inode, so without
        # this an operator's tightened mode is silently replaced by the umask
        # default.  config.json can hold inline credentials, so a telemetry
        # toggle must never widen who can read it.  The locked helper preserves
        # mode on POSIX; restrict_to_owner applies the owner-only DACL on
        # Windows (and 0600 on POSIX for new files). Fail-loud so a lockdown
        # that cannot be applied surfaces rather than silently leaving the file
        # wide open.
        try:
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        except OSError:
            mode = 0o600
        if not platform_compat.IS_POSIX or mode == 0o600:
            platform_compat.restrict_to_owner(path)
    except ConfigReadError as exc:
        err_str = str(exc)
        if "not a JSON object" in err_str:
            print(
                f"❌ {path} is not a JSON object; refusing to "
                "overwrite it. Fix or move the file, then retry.",
                file=sys.stderr,
            )
        else:
            print(f"❌ Could not read {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"❌ Could not write {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    # Verify the write actually took EFFECT before claiming success.
    # config.local.json deep-merges OVER config.json at load, so a host that
    # sets this key locally would keep sending while this command
    # printed "DISABLED" — a false promise on a privacy control is worse than an
    # error, so re-read the effective config and report the shadowing file.
    try:
        effective = KiroCrewConfig.load().telemetry.beacon_enabled
    except Exception:  # noqa: BLE001 - diagnostics must not mask the write
        effective = want
    if effective != want:
        state = "ENABLED" if effective else "DISABLED"
        print(
            f"⚠️  Wrote {path.name}, but the beacon is still {state}: an overlay "
            "in config.local.json takes precedence.",
            file=sys.stderr,
        )
        print(
            "   Edit telemetry.beacon_enabled there too, or export "
            f"{beacon.DISABLE_ENV}=1 to override everything.",
            file=sys.stderr,
        )
        sys.exit(1)

    if want:
        print("✅ Anonymous usage beacon ENABLED (one heartbeat per day).")
        print("   Run 'kirocrew telemetry status' to see exactly what is sent.")
    else:
        print("✅ Anonymous usage beacon DISABLED. Nothing will be sent.")
        print(f"   You can also delete {beacon.INSTALL_ID_FILE} from the data home.")


def _handle_secrets(args: argparse.Namespace) -> None:
    """Dispatch secrets subcommands. Currently only ``import`` (migration)."""

    action = getattr(args, "secrets_action", None)

    if action == "import":
        # Import ONLY from the fixed data-home .env — never an arbitrary path.
        # A caller-supplied file would let a sandbox-off agent import attacker
        # Jira values into the vault and have the vault-first consumer trust
        # them, so there is deliberately no --file option.
        # A concurrent .env change or an undecryptable pre-existing vault entry
        # aborts the migration with MigrationConflictError. That is an expected
        # operational condition (retry after the concurrent write settles, or
        # repair the vault entry), so surface it as a clean CLI error with a
        # nonzero exit — never an uncaught traceback.
        try:
            report = migrate_env_secrets(dry_run=not args.apply)
        except (MigrationConflictError, EnvStagingRefused) as exc:
            # EnvStagingRefused is an OSError, so it is caught first: the vault is
            # fine and the message names the staging directory and its fix.
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        except (OSError, ValueError) as exc:
            # A truncated/corrupt `secrets.enc` makes the vault's `list_names()`
            # (or a decrypt) raise `ValueError`/`OSError` rather than
            # `MigrationConflictError`. Surface it as the same concise CLI error
            # with a nonzero exit instead of an uncaught traceback — the store is
            # unreadable, which the operator must repair before importing.
            print(
                f"error: could not read the secrets vault "
                f"({exc.__class__.__name__}: {exc}); repair or remove the vault "
                f"store, then re-run `kirocrew secrets import --apply`.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(format_report(report))
    else:
        print("Usage: kirocrew secrets import [--apply]", file=sys.stderr)
        sys.exit(1)


# The handlers above are this module's own; every other command lives in an owner
# below, a ``cli_commands_<command>`` module beside this one. Every name this module
# held still resolves as ``kiro_crew.cli_commands.<name>``: an owner's name is served
# from the owner that binds it, and a write through this module (``monkeypatch.setattr``,
# ``mock.patch``) reaches every owner holding the name (``kiro_crew._facade_mirror``).
# Owners never import this module; one imports another only from an earlier entry.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.cli_commands_workspace",
    "kiro_crew.cli_commands_memory",
    "kiro_crew.cli_commands_gateway_api",
    "kiro_crew.cli_commands_app",
    "kiro_crew.cli_commands_agent",
    "kiro_crew.cli_commands_cron",
    "kiro_crew.cli_commands_security",
    "kiro_crew.cli_commands_eval",
    "kiro_crew.cli_commands_learn",
)
for _owner in _OWNERS:
    importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(sys.modules[__name__], _OWNERS, excluded=()).served)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace
#: (``TYPE_CHECKING`` is the manifest's glue, not one of them).
__all__ = sorted(
    n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_") and n != "TYPE_CHECKING"
)

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.cli_commands_agent import (  # noqa: F401
        _agent_list_label,
        _agent_list_model,
        _agent_model_or_exit,
        _agent_reset_model,
        _handle_agent,
        _kiro_agent_or_exit,
        _memory_store_or_exit,
    )
    from kiro_crew.cli_commands_app import (  # noqa: F401
        _SESSION_APPROVAL_CONSENT_REQUIRED,
        _SESSION_APPROVAL_DESC,
        _app_already_gone,
        _app_declares_backend,
        _apply_through_gateway,
        _cleanup_app_crons_from_scheduler,
        _handle_app,
        _handle_app_import,
        _install_app_through_gateway,
        _print_file_only_app_result,
        _print_pointer_cleanup,
        _refuse_session_approval_from_cli,
        _register_app_crons_to_scheduler,
        _run_app_action_through_gateway,
        _run_app_mcp_server,
        _warn_backend_not_stopped,
        _warn_hooks_need_restart,
    )
    from kiro_crew.cli_commands_cron import (  # noqa: F401
        _cron,
        _cron_add,
        _cron_add_bounds,
        _cron_add_fail,
        _cron_dispatch,
        _cron_preview,
        _format_schedule,
    )
    from kiro_crew.cli_commands_eval import (  # noqa: F401
        _run_eval,
        write_eval_artifacts,
    )
    from kiro_crew.cli_commands_gateway_api import (  # noqa: F401
        _artifact,
        _gateway_send,
        _internal_secret,
        _peer_refusal,
        _spawn,
        _spawn_row_text,
        _spawn_run,
    )
    from kiro_crew.cli_commands_learn import (  # noqa: F401
        _LEARN_DEDUP_NOTE,
        _LEARN_EMBED_NOTE,
        _learn,
        _lesson_scope_suffix,
    )
    from kiro_crew.cli_commands_memory import (  # noqa: F401
        _EXPORT_EPISODIC_LIMIT,
        _EXPORT_EVENTS_LIMIT,
        _IMPORT_EMBED_NOTE,
        _IMPORT_FACET_KEYS,
        _IMPORTED_COLLECTIONS,
        _SEARCH_KEYWORD_ONLY_NO_MODEL,
        _SEARCH_KEYWORD_ONLY_NOT_READY,
        _SEARCH_KEYWORD_ONLY_PENDING_ROWS,
        _SEARCH_KEYWORD_ONLY_STALE_SPACE,
        _SEARCH_MODEL_LOAD_TIMEOUT_SECS,
        _admitted_store_path,
        _finding_store_suffix,
        _interactive_terminal,
        _markdown_memory_store,
        _memory_backup_cmd,
        _memory_carve,
        _memory_cmd,
        _memory_create_store,
        _memory_forget,
        _memory_search_embedding,
        _memory_search_history,
        _memory_show,
        _memory_verb,
        _refuse_forget_outside_owner_terminal,
        _settle_created_database,
    )
    from kiro_crew.cli_commands_security import (  # noqa: F401
        _TIME_SELECTOR_RE,
        _TIME_SELECTOR_UNITS,
        _policy,
        _policy_fetch,
        _print_denied_command_summary,
        _print_policy_source,
        _security,
        parse_time_selector,
    )
    from kiro_crew.cli_commands_workspace import (  # noqa: F401
        _WS_DIR_OUTSIDE_HOME,
        _cli_validated_workspace_dst,
        _CliConflict,
        _handle_workspace,
        _locked_config_write,
        _ws_dir_composed,
        _ws_dir_error,
        _ws_dir_resolves_inside_home,
    )
