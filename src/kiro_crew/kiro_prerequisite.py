"""Cross-platform Kiro CLI readiness detection.

The public KiroCrew provider is KiroACP-only, so a healthy, authenticated
``kiro-cli`` is a hard runtime prerequisite. This module's job is to answer one
question about the gateway host: **is there a Kiro CLI that runs, and is it
signed in?** It answers that by running the CLI's own read-only probes
(``--version``, then ``whoami``) inside the OS sandbox.

**It performs no setup of its own.** Both setup steps belong to Kiro CLI and are
taken by the user:

* obtaining the CLI — from Kiro's official setup page
  (:data:`OFFICIAL_INSTALL_DOCS_URL`);
* signing in — ``kiro-cli login`` (:data:`KIRO_CLI_LOGIN_COMMAND`), run by the
  user wherever they already use the CLI.

Kiro Crew neither installs nor authenticates on the user's behalf, so there is no
installer download, no installer execution, and no device-flow spawn. The
reasons are the same in both cases: the vendor's own tooling does it better and
stays correct as it changes, owning it here meant owning a large privileged
surface (a remote shell script executed unsandboxed; a credential-writing child
process), and every copy of that logic on this side was one more thing to drift
out of date — the installer's pinned digest silently broke setup on any upstream
change.

What remains is detection, and detection alone: every subprocess this module
spawns is one of the two read-only probes above, sandboxed, with a fixed argv.
No command, argument, URL, or filesystem target is accepted from an HTTP
request, and the dashboard exposes only a ``status`` read plus the agent-spec
repair write (which touches Kiro Crew's own files, never the CLI).
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the Kiro CLI readiness owners, and every name is reached
# through it as ``kiro_crew.kiro_prerequisite.<name>``: a read is served by the owner
# that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches
# every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this
# module. New work goes to the owner of its responsibility:
#
#   ``kiro_prerequisite_status`` -- What a readiness answer says: the status payload and the commands it serves.
#   ``kiro_prerequisite_process`` -- Running one fixed kiro-cli argv in the OS sandbox, bounded, and reaping its tree.
#   ``kiro_prerequisite_candidate`` -- Which kiro-cli is probed, with what environment, and what makes it launchable.
#   ``kiro_prerequisite_identity_store`` -- Kiro's identity stores: where they are, how they are read, and how they are staged.
#   ``kiro_prerequisite_identity`` -- Which account the credential sources name, as one fingerprint.
#   ``kiro_prerequisite_spawn_identity`` -- Which account a spawned child loaded, and how a holder is judged against the live one.
#   ``kiro_prerequisite_agent_specs`` -- Kiro Crew's own agent specs: presence, acceptance by kiro-cli and KAS, and repair.
#   ``kiro_prerequisite_identity_tracking`` -- The service's account baselines: the retirement baseline, the interim latch, absence runs.
#   ``kiro_prerequisite_probe`` -- The audited kiro-cli probes, the failed-turn sign-in check and the self-update.
#   ``kiro_prerequisite_service`` -- The ``KiroPrerequisiteService`` class: its fields, status surface and bindings.


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.kiro_prerequisite_status",
    "kiro_crew.kiro_prerequisite_process",
    "kiro_crew.kiro_prerequisite_candidate",
    "kiro_crew.kiro_prerequisite_identity_store",
    "kiro_crew.kiro_prerequisite_identity",
    "kiro_crew.kiro_prerequisite_spawn_identity",
    "kiro_crew.kiro_prerequisite_agent_specs",
    "kiro_crew.kiro_prerequisite_identity_tracking",
    "kiro_crew.kiro_prerequisite_probe",
    "kiro_crew.kiro_prerequisite_service",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.kiro_prerequisite_agent_specs import (  # noqa: F401
        _SPEC_REJECTION_MARKER,
        _SPECS_STILL_MISSING_ERROR,
        _agent_spec_overlay,
        _default_spec_lister,
        _kas_spec_load_failures,
        _missing_agent_specs,
        _probe_kas_spec_loading,
        _probe_spec_acceptance,
        _repair_agent_specs,
        _repair_auxiliary_specs,
        _unlaunchable_mcp_servers,
        repair_agent_specs,
    )
    from kiro_crew.kiro_prerequisite_candidate import (  # noqa: F401
        _IDENTITY_PROBE_ENV_KEYS,
        _IDENTITY_PROXY_ENV_KEYS,
        _MAX_AUTH_EXECUTABLE_BYTES,
        _OPERATOR_OVERRIDE_ATTESTATIONS,
        _PROBE_ENV_KEYS,
        TrustedAcpExecutableSnapshot,
        _acp_executable_is_runnable,
        _allowlisted_env,
        _binary_sha256,
        _canonical_candidate,
        _existing_binary_digest,
        _identity_probe_env,
        _is_runnable_executable,
        _probe_env,
        _probe_filesystem_state,
        _register_operator_override_attestation,
        register_process_start_override_attestation,
        snapshot_trusted_acp_executable,
    )
    from kiro_crew.kiro_prerequisite_identity import (  # noqa: F401
        _API_KEY_FINGERPRINT_SEP,
        _AUTH_FINGERPRINT_ABSENT,
        _CASE_INSENSITIVE_CLAIM_FIELDS,
        _CREW_VAULT_FINGERPRINT_SEP,
        _IDENTITY_CLAIM_FIELDS,
        _IDENTITY_FINGERPRINT_READ_ID,
        _api_key_identity_claim,
        _claim_digest,
        _combine_identity_fingerprints,
        _crew_vault_fingerprint,
        _crew_vault_holds_no_identity,
        _identity_claims,
        _identity_components,
        _store_choice_is_certain,
        identity_fingerprint,
    )
    from kiro_crew.kiro_prerequisite_identity_store import (  # noqa: F401
        _AUTH_IDENTITY_TABLES,
        _AUTH_SQLITE_DB,
        _AUTH_SQLITE_FILES,
        _AUTH_SQLITE_TIMEOUT_SECS,
        _AUTH_STAGING_RELATIVE,
        _AUTH_STATE_KEY_PREFIXES,
        _AUTH_STATE_TABLE,
        _AUTH_STORE_READ_ERROR,
        _MAX_AUTH_STORE_FILE_BYTES,
        _atomic_write_secret_bytes,
        _auth_store_mappings,
        _AuthStoreMapping,
        _AuthWorkspace,
        _ensure_auth_staging_parent,
        _open_identity_db_readonly,
        _prepare_auth_workspace,
        _project_identity_database,
        _read_bounded_regular_file,
        identity_store_is_relocated,
        kiro_identity_store_path,
    )
    from kiro_crew.kiro_prerequisite_identity_tracking import (  # noqa: F401
        _AUTH_FINGERPRINT_CACHE_SECS,
        _SEED_BASELINE_TIMEOUT_SECS,
        _maybe_latch_interim_identity,
        current_identity_fingerprint,
        identity_changed_since_probe,
        identity_changed_since_sessions,
        note_absence_swept,
        note_sessions_reconciled,
        read_spawn_identity,
        seed_sessions_baseline,
    )
    from kiro_crew.kiro_prerequisite_probe import (  # noqa: F401
        _ACP_SUBCOMMAND,
        _ACP_UNSUPPORTED_MARKERS,
        _IDENTITY_PROBE_TIMEOUT_SECS,
        _KIRO_AUTH_SANDBOX_MODE,
        _PROBE_CACHE_SECS,
        _PROBE_ERROR_MAX_CHARS,
        _PROBE_TIMEOUT_SECS,
        _TURN_AUTH_MAX_AGE_SECS,
        _UPDATE_TIMEOUT_SECS,
        _WHOAMI_SIGNED_OUT_MARKER,
        AuditWriter,
        _audited_identity_probe,
        _audited_probe,
        _probe,
        _probe_acp_support,
        _probe_failure_text,
        _run_auth_command,
        _set_terminal_audit,
        _stamp_probe,
        _terminal_audit_detail,
        _whoami_says_signed_out,
        _write_audit,
        signed_out_confirmed,
        update_cli,
    )
    from kiro_crew.kiro_prerequisite_process import (  # noqa: F401
        _MAX_CAPTURED_OUTPUT,
        _PROCESS_GROUP_SUPERVISOR,
        _PROCESS_GROUP_SUPERVISOR_CODE,
        _PROCESS_GROUP_SUPERVISOR_ERROR,
        _TERMINATION_GRACE_SECS,
        _UNVERIFIED_SANDBOX_MODE,
        _WINDOWS_DESCENDANT_POLL_SECS,
        ProcessResult,
        ProcessRunner,
        _append_capped,
        _host_can_reap,
        _note_unsupervised_once,
        _prepare_sandboxed_spawn,
        _run_on_private_loop,
        _run_process,
        _terminate_process,
        _track_windows_descendants,
        _unlink_off_loop,
        _unsupervised_noted,
        spawn_supervised_oneshot,
    )
    from kiro_crew.kiro_prerequisite_service import (  # noqa: F401
        _FORCED_PROBE_FLOOR_SECS,
        _OWNER_PREFIX,
        _SETUP_COMPLETE_FILENAME,
        _WARM_UP_DELAY_SECS,
        KiroPrerequisiteService,
        _established_installation,
        _own_methods,
        _turn_auth_service,
        kiro_signed_out_confirmed,
        logger,
        register_turn_auth_service,
    )
    from kiro_crew.kiro_prerequisite_spawn_identity import (  # noqa: F401
        IDENTITY_PARK_GRACE_SECS,
        _without_api_key_component,
        identity_component_dropped,
        identity_park_grace_remaining,
        identity_stamp_mismatch,
        mark_identity_parked,
        overlay_sets_api_key,
        pre_spawn_identity,
        receives_kiro_cli_api_key,
        spawn_identity_of,
        spawn_pid,
        spawned_under,
        stamp_spawn_identity,
    )
    from kiro_crew.kiro_prerequisite_status import (  # noqa: F401
        _LEGACY_IDLE_OPERATION,
        _LOGIN_TAIL,
        _MAX_VISIBLE_DETAIL,
        _SSO_LOGIN_TAIL,
        BUNDLED_CLI_UPDATE_REFUSAL,
        FAKE_ACP_TEST_MODE_ENV,
        KIRO_CLI_LOGIN_COMMAND,
        KIRO_CLI_SSO_LOGIN_COMMAND,
        KIRO_CLI_UPDATE_COMMAND,
        OFFICIAL_INSTALL_DOCS_URL,
        PrerequisiteStatus,
        _is_off_path_override,
        _platform_label,
        _sanitize_detail,
        legacy_idle_operation,
        login_commands_for,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import asyncio  # noqa: F401
    import contextlib  # noqa: F401
    import functools  # noqa: F401
    import hashlib  # noqa: F401
    import json  # noqa: F401
    import logging  # noqa: F401
    import os  # noqa: F401
    import shlex  # noqa: F401
    import shutil  # noqa: F401
    import stat  # noqa: F401
    import subprocess  # noqa: F401
    import sys  # noqa: F401
    import tempfile  # noqa: F401
    import time  # noqa: F401
    import urllib.request  # noqa: F401
    import weakref  # noqa: F401
    from collections.abc import (  # noqa: F401
        Awaitable,
        Callable,
        Coroutine,
        Mapping,
        MutableMapping,
    )
    from dataclasses import (  # noqa: F401
        asdict,
        dataclass,
        field,
        replace,
    )
    from pathlib import (  # noqa: F401
        Path,
    )
    from typing import (  # noqa: F401
        Any,
    )

    from kiro_crew import (  # noqa: F401
        _process_group_supervisor,
        hooks,
        identity_stores,
        platform_compat,
        sandbox,
    )
    from kiro_crew._sqlite_compat import (  # noqa: F401
        sqlite3,
    )
    from kiro_crew.agent_files import (  # noqa: F401
        AGENT_FILENAME,
        LITE_AGENT_FILENAME,
    )
    from kiro_crew.atomic_write import (  # noqa: F401
        atomic_write,
    )
    from kiro_crew.config.loader import (  # noqa: F401
        CRED_KIRO_API_KEY,
        read_env_file_credential,
    )
    from kiro_crew.config.paths import (  # noqa: F401
        config_dir,
    )
    from kiro_crew.executors import (  # noqa: F401
        kiro_spawn_executor,
    )
    from kiro_crew.kiro_cli import (  # noqa: F401
        find_kiro_cli_candidates,
        is_bundled_kiro_cli,
        known_kiro_cli_dirs,
    )
    from kiro_crew.mcp_cleanup import (  # noqa: F401
        mcp_entry_is_registry_governed,
    )
    from kiro_crew.owner_only_files import (  # noqa: F401
        ensure_directory,
    )
    from kiro_crew.sandbox import (  # noqa: F401
        SandboxUnavailableError,
        corroborate_launcher_refusal,
        create_subprocess_limited,
        launcher_refusal,
        resource_limit_supervisor_argv,
        sandboxed_spawn_argv,
        shielded_prepare_off_loop,
    )
    from kiro_crew.security import (  # noqa: F401
        redact,
        redact_credentials,
        redact_exfiltration_urls,
    )
