"""HTTP routes for the AWS Control builtin, served in-process by the gateway.

Mounted at ``/api/apps/aws-control/`` — the same single-argument
``register_routes(app)`` convention every builtin uses (registered at startup
by the ``BUILTIN_NAMES`` loop; every handler carries its own enabled check).

Surface (owner-only throughout, see below):

READS
``GET /accounts``                              aggregated account list (?refresh=1)
``GET /profiles/available``                    local profiles, with registered flags
``GET /profiles/{name}/reconnect-plan``        what Reconnect can offer, no action
``GET /drive/{account}``                       drive presence + cached usage
``GET /drive/{account}/list``                  one listing page (?section&path&token)
``GET /drive/{account}/download``              short-lived download URL (?section&key)
``GET /drive/{account}/preview``               first bytes of a text file (?section&key)
``GET /drive/{account}/search``                filename search in a section (?section&q)
``GET /costs/{account}``                       cached bill (?refresh=1 re-queries CE)
``GET /library/{account}``                     local artifacts + reconciled push state
``GET /backup/{account}``                      backup state + remote archive listing
``GET /shares``                                live share ledger, objects checked (?account=)
``GET /iam-policy``                            drive-tier policy JSON (local render)

MUTATIONS (also restricted-session refused + SEL-audited)
``POST /profiles/register``                    add local profiles to the registry
``POST /profiles/unregister``                  drop profiles from the registry only
``POST /drive/{account}/bootstrap``            create the bucket (two-call confirm)
``POST /drive/{account}/upload``               upload one file (?section&key, raw body)
``POST /drive/{account}/delete``               delete one object
``POST /drive/{account}/move``                 move one object (drive section only)
``POST /drive/{account}/folder``               create an empty folder (placeholder)
``POST /drive/{account}/folder/delete``        delete a folder and all its objects
``POST /drive/{account}/share``                mint a presigned share + ledger entry
``POST /shares/{id}/forget``                   drop a ledger entry (link lives to expiry)
``POST /library/{account}/push``               push one artifact to the cloud library
``POST /library/{account}/remove``             delete a cloud copy + forget its record
``POST /backup/{account}/run``                 run a backup (snapshot | sessions)
``POST /backup/{account}/nightly``             toggle the nightly snapshot
``POST /backup/{account}/retention``           set or clear the retention count
``POST /backup/{account}/nightly-sessions``    toggle the nightly sessions archive
``POST /backup/{account}/layer-b``             permit unredacted context in the sessions archive
``POST /backup/{account}/restore``             download an archive to the staging dir
``POST /install/label``                        rename THIS install (display only, local)

GUARDS, in order, and why each exists:

1. **Enabled check** — a disabled app answers 403 ``app_disabled`` to
   everyone, so a probe cannot tell "app off" from "not for you".
2. **Owner-only, INCLUDING reads.** Account ids and caller ARNs are what the
   keystone-fenced consent leaf is fenced FROM; a surface printing them for
   any authenticated app token would hand out through one door what is
   locked behind another. ``is_owner_dashboard_request`` encodes the rule
   (same as ``aws_consent`` and ``mcp_apps``).
3. **Consent** — every handler that reaches AWS calls
   ``aws_consent.refuse_and_log`` (``s3`` or ``ce``) with the profile+region
   it is about to use. Fails closed; the page renders the consent card.
4. **Restricted-session refusal + SEL audit** on every mutation, matching
   the deploy handlers' discipline.
5. **Two-call confirm on billable resource creation** (bucket bootstrap):
   the first call returns a preview, only ``{"confirm": true}`` executes.
   There is no agent path to these endpoints at all (guard 2), so the
   dashboard click IS the human confirmation the spec's G1 requires.
"""

# This module is the AWS Control routes' registration table and their facade: every
# name is reached as ``kiro_crew.apps.builtins.aws_control.backend.routes.<name>``.
# A read is served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`), so a
# test patches this module and never an owner. Owners never import this module. New
# work goes to the owner of its responsibility:
#
#   ``routes_parts.common`` -- limits, locks, error responses, the audit, the handler
#       wrappers, the request targets, and the re-check a queued write runs
#   ``routes_parts.accounts`` -- accounts, reconnect plans, the profile registry, the
#       bill and the IAM policy
#   ``routes_parts.drive`` -- drive status and bootstrap, and listing, download,
#       preview and search
#   ``routes_parts.drive_writes`` -- upload, delete, move, folders, and share links
#   ``routes_parts.library`` -- the reconciled cloud library, and push and remove
#   ``routes_parts.backups`` -- backup status, runs, toggles, retention, Layer B,
#       restore and the install label

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.accounts import (
    _handle_accounts,
    _handle_costs,
    _handle_iam_policy,
    _handle_profiles_available,
    _handle_profiles_register,
    _handle_profiles_unregister,
    _handle_reconnect_plan,
)
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.backups import (
    _handle_backup_layer_b,
    _handle_backup_nightly,
    _handle_backup_nightly_sessions,
    _handle_backup_restore,
    _handle_backup_retention,
    _handle_backup_run,
    _handle_backup_status,
    _handle_install_label,
)
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.common import (
    _BASE,
    _guarded,
    _mutating,
)
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive import (
    _handle_drive_bootstrap,
    _handle_drive_download,
    _handle_drive_list,
    _handle_drive_preview,
    _handle_drive_search,
    _handle_drive_status,
)
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive_writes import (
    _handle_drive_delete,
    _handle_drive_folder_create,
    _handle_drive_folder_delete,
    _handle_drive_move,
    _handle_drive_share,
    _handle_drive_upload,
    _handle_share_forget,
    _handle_shares_list,
)
from kiro_crew.apps.builtins.aws_control.backend.routes_parts.library import (
    _handle_library_list,
    _handle_library_push,
    _handle_library_remove,
)


def register_routes(app: web.Application) -> None:
    """Register on the gateway's aiohttp Application (single-arg convention)."""
    r = app.router
    # Reads
    r.add_get(f"{_BASE}/accounts", _guarded(_handle_accounts))
    # Registered BEFORE the {name} route: aiohttp matches in registration order
    # and a literal "available" would otherwise be captured as a profile name.
    r.add_get(f"{_BASE}/profiles/available", _guarded(_handle_profiles_available))
    r.add_get(f"{_BASE}/profiles/{{name}}/reconnect-plan", _guarded(_handle_reconnect_plan))
    r.add_get(f"{_BASE}/drive/{{account}}", _guarded(_handle_drive_status))
    r.add_get(f"{_BASE}/drive/{{account}}/list", _guarded(_handle_drive_list))
    r.add_get(f"{_BASE}/drive/{{account}}/download", _guarded(_handle_drive_download))
    r.add_get(f"{_BASE}/drive/{{account}}/preview", _guarded(_handle_drive_preview))
    r.add_get(f"{_BASE}/drive/{{account}}/search", _guarded(_handle_drive_search))
    r.add_get(f"{_BASE}/costs/{{account}}", _guarded(_handle_costs))
    r.add_get(f"{_BASE}/library/{{account}}", _guarded(_handle_library_list))
    r.add_get(f"{_BASE}/backup/{{account}}", _guarded(_handle_backup_status))
    r.add_get(f"{_BASE}/shares", _guarded(_handle_shares_list))
    r.add_get(f"{_BASE}/iam-policy", _guarded(_handle_iam_policy))
    # Mutations
    r.add_post(
        f"{_BASE}/profiles/register",
        _guarded(_mutating("profiles_register")(_handle_profiles_register)),
    )
    r.add_post(
        f"{_BASE}/profiles/unregister",
        _guarded(_mutating("profiles_unregister")(_handle_profiles_unregister)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/bootstrap",
        _guarded(_mutating("drive_bootstrap")(_handle_drive_bootstrap)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/upload",
        _guarded(_mutating("drive_upload")(_handle_drive_upload)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/delete",
        _guarded(_mutating("drive_delete")(_handle_drive_delete)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/move",
        _guarded(_mutating("drive_move")(_handle_drive_move)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/folder",
        _guarded(_mutating("drive_folder_create")(_handle_drive_folder_create)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/folder/delete",
        _guarded(_mutating("drive_folder_delete")(_handle_drive_folder_delete)),
    )
    r.add_post(
        f"{_BASE}/drive/{{account}}/share",
        _guarded(_mutating("drive_share")(_handle_drive_share)),
    )
    r.add_post(
        f"{_BASE}/shares/{{id}}/forget",
        _guarded(_mutating("share_forget")(_handle_share_forget)),
    )
    r.add_post(
        f"{_BASE}/library/{{account}}/push",
        _guarded(_mutating("library_push")(_handle_library_push)),
    )
    r.add_post(
        f"{_BASE}/library/{{account}}/remove",
        _guarded(_mutating("library_remove")(_handle_library_remove)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/run",
        _guarded(_mutating("backup_run")(_handle_backup_run)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/nightly",
        _guarded(_mutating("backup_nightly")(_handle_backup_nightly)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/retention",
        _guarded(_mutating("backup_retention")(_handle_backup_retention)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/nightly-sessions",
        _guarded(_mutating("backup_nightly_sessions")(_handle_backup_nightly_sessions)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/layer-b",
        _guarded(_mutating("backup_layer_b")(_handle_backup_layer_b)),
    )
    r.add_post(
        f"{_BASE}/backup/{{account}}/restore",
        _guarded(_mutating("backup_restore")(_handle_backup_restore)),
    )
    r.add_post(
        f"{_BASE}/install/label",
        _guarded(_mutating("install_label")(_handle_install_label)),
    )


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.aws_control.backend.routes_parts.common",
    "kiro_crew.apps.builtins.aws_control.backend.routes_parts.accounts",
    "kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive",
    "kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive_writes",
    "kiro_crew.apps.builtins.aws_control.backend.routes_parts.library",
    "kiro_crew.apps.builtins.aws_control.backend.routes_parts.backups",
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
    from kiro_crew.apps.builtins.aws_control.backend.routes_parts.accounts import (  # noqa: F401
        _MAX_REGISTERED,
    )
    from kiro_crew.apps.builtins.aws_control.backend.routes_parts.backups import (  # noqa: F401
        _account_jobs,
        _job_view,
        _toggle_nightly,
    )
    from kiro_crew.apps.builtins.aws_control.backend.routes_parts.common import (  # noqa: F401
        _DOWNLOAD_URL_SECS,
        _LIBRARY_RECONCILE_LOCK_WAIT_SECS,
        _MAX_UPLOAD_BYTES,
        _PREVIEW_MAX_BYTES,
        _PREVIEW_REDACT_LOOKAHEAD,
        _PREVIEW_TRIM_SEARCH,
        _PUBLISH_PROVIDER_ID,
        _USAGE_TTL_SECS,
        APP_NAME,
        Handler,
        _account_target,
        _audit,
        _audit_sync,
        _aws_failed,
        _bad_request,
        _body,
        _bootstrap_lock,
        _conflict,
        _consent,
        _drive_bucket,
        _drive_key_lock_refs,
        _drive_key_lock_unref,
        _drive_key_locks,
        _drive_section_lock,
        _drive_section_locks,
        _forbidden,
        _ledger_corrupt,
        _library_lock,
        _locked_drive_keys,
        _locked_drive_sweep,
        _locked_drive_write,
        _not_found,
        _publish_gate,
        _reauthorize_in_lock,
        _resolve_target,
        _response_code,
        _safe_error,
        _SectionRWLock,
        _unavailable,
        _unsupported,
        _usage_cache,
        _valid_section,
        logger,
    )
    from kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive import (  # noqa: F401
        _redact_preview,
        _require_drive,
        _trim_preview,
    )
    from kiro_crew.apps.builtins.aws_control.backend.routes_parts.drive_writes import (  # noqa: F401
        _drive_object_keys,
    )
    from kiro_crew.apps.builtins.aws_control.backend.routes_parts.library import (  # noqa: F401
        _reconciled_remote_slugs,
    )


if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import aws_consent  # noqa: F401
    from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod  # noqa: F401
    from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod  # noqa: F401
    from kiro_crew.apps.builtins.aws_control.backend import costs as costs_mod  # noqa: F401
    from kiro_crew.apps.builtins.aws_control.backend import library as library_mod  # noqa: F401
    from kiro_crew.apps.builtins.aws_control.backend import shares as shares_mod  # noqa: F401
    from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod  # noqa: F401
    from kiro_crew.apps.job_sdk import JobError  # noqa: F401
    from kiro_crew.apps.job_sdk import UnknownJobKind  # noqa: F401
    from kiro_crew.apps.job_sdk import get_sdk as get_job_sdk  # noqa: F401
    from kiro_crew.apps.manager import is_app_enabled  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response  # noqa: F401
    from kiro_crew.dashboard.handlers.source_providers import (  # noqa: F401
        is_owner_dashboard_request,
    )
    from kiro_crew.deploy import profiles as deploy_profiles  # noqa: F401
    from kiro_crew.deploy.engine import AWSError  # noqa: F401
    from kiro_crew.loop_lock import LoopBoundLock  # noqa: F401
    from kiro_crew.publish_governance import publish_denied_reason  # noqa: F401
    from kiro_crew.security import redact_credentials  # noqa: F401
    from kiro_crew.security import redact_exfiltration_urls  # noqa: F401
    from kiro_crew.sel import sel  # noqa: F401
