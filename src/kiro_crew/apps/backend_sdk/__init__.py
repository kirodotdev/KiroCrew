"""The public backend SDK: the host controls an app's own Python may call.

An app backend that needs a host control with no ``ctx`` seam (a sensitive-path
check, the file path gate, a no-follow read, outbound redaction, an audit row, a
sandboxed child, a process or file-lock helper, the public-address check for a URL it
fetches, a media-type sniff, a descriptor-pinned path walk and copy) otherwise looks
up a private ``kiro_crew`` name by string. Those names move between releases, so each
lookup is a gap that keeps a wheel copy alive. This package is the stable spelling of
the most-used of them.

**Reachable from both backend kinds.** An in-process app (``backend.hooks``) and a
spawned one (``backend.entryPoint`` on the gateway's interpreter, the default once
its dependencies are provisioned) import it the same way. A spawned backend on its
own venv interpreter does not see ``kiro_crew`` at all and treats the package as
absent, exactly as it treats an older gateway.

**Same control, by reference.** Every name delegates to the private function the
gateway itself enforces, looked up at call time, so a check tightened in a later
release reaches the app with the wheel. Nothing here is a copy, and no name adds
reach: a spawn is wrapped in the same OS sandbox and can only keep or tighten its
tier, a read or a pinned copy refuses exactly what the private one refuses, and the
URL check only refuses: it opens no connection.

**Level-gated.** ``SDK_LEVEL`` is the App Kit seam level of this build
(``ctx.sdk_level`` for an in-process app); the package exists from level 7. A name
added after the package carries its own level in ``_EXPORT_LEVELS``. An app reads
the package through a feature-detected import, and a later name with
``getattr(sdk, name, None)``, and keeps its own refusal on an older gateway.

Importing the package is cheap: each name is bound on first access, so a spawned
backend that wants one helper does not load the gateway's whole security stack.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL

if TYPE_CHECKING:
    from kiro_crew.apps.backend_sdk.audit import audit_log  # noqa: F401
    from kiro_crew.apps.backend_sdk.files import (  # noqa: F401
        FileTooLargeError,
        atomic_write,
        canonical_file_path,
        ensure_owner_only_dir,
        read_bytes_nofollow,
    )
    from kiro_crew.apps.backend_sdk.media import RASTER_SNIFF_BYTES, sniff_raster_mime  # noqa: F401
    from kiro_crew.apps.backend_sdk.paths import (  # noqa: F401
        contains_sensitive_path,
        is_sensitive_path,
    )
    from kiro_crew.apps.backend_sdk.pinned import (  # noqa: F401
        PinnedPathRefusal,
        copy_file_pinned,
        fd_real_path,
        is_reparse_point,
        pin_parent,
        supports_pinned_walk,
    )
    from kiro_crew.apps.backend_sdk.process import (  # noqa: F401
        IS_LINUX,
        IS_MACOS,
        IS_POSIX,
        IS_WINDOWS,
        file_lock,
        first_linked_ancestor,
        is_link_or_junction,
        kill_and_reap,
        rmtree_force,
    )
    from kiro_crew.apps.backend_sdk.redaction import ScrubResult, scrub_outbound  # noqa: F401
    from kiro_crew.apps.backend_sdk.spawn import (  # noqa: F401
        SandboxUnavailableError,
        run_sandboxed,
        run_sandboxed_async,
        sandboxed_argv,
        sandboxed_argv_async,
    )
    from kiro_crew.apps.backend_sdk.urls import (  # noqa: F401
        UrlRejectedError,
        address_is_not_public,
        vet_public_url,
    )

#: The App Kit seam level this gateway provides; the package exists from level 7.
SDK_LEVEL: int = APP_SDK_LEVEL

#: The seam level that publishes this package.
BACKEND_SDK_LEVEL = 7

#: Public name -> the submodule that binds it.
_LAZY_EXPORTS: dict[str, str] = {
    "audit_log": "audit",
    "FileTooLargeError": "files",
    "atomic_write": "files",
    "canonical_file_path": "files",
    "ensure_owner_only_dir": "files",
    "read_bytes_nofollow": "files",
    "RASTER_SNIFF_BYTES": "media",
    "sniff_raster_mime": "media",
    "contains_sensitive_path": "paths",
    "is_sensitive_path": "paths",
    "PinnedPathRefusal": "pinned",
    "copy_file_pinned": "pinned",
    "fd_real_path": "pinned",
    "is_reparse_point": "pinned",
    "pin_parent": "pinned",
    "supports_pinned_walk": "pinned",
    "IS_LINUX": "process",
    "IS_MACOS": "process",
    "IS_POSIX": "process",
    "IS_WINDOWS": "process",
    "file_lock": "process",
    "first_linked_ancestor": "process",
    "is_link_or_junction": "process",
    "kill_and_reap": "process",
    "rmtree_force": "process",
    "ScrubResult": "redaction",
    "scrub_outbound": "redaction",
    "SandboxUnavailableError": "spawn",
    "run_sandboxed": "spawn",
    "run_sandboxed_async": "spawn",
    "sandboxed_argv": "spawn",
    "sandboxed_argv_async": "spawn",
    "UrlRejectedError": "urls",
    "address_is_not_public": "urls",
    "vet_public_url": "urls",
}

#: Public name -> the App Kit seam level that publishes it, for each name added after
#: ``BACKEND_SDK_LEVEL``. A name absent here is published at ``BACKEND_SDK_LEVEL``.
#: The adoption pin tool (``scripts/adoption_pin.py``) counts a tree's use of a name
#: at this level, so a host older than the name keeps the wheel copy.
_EXPORT_LEVELS: dict[str, int] = {
    "canonical_file_path": 14,
    "RASTER_SNIFF_BYTES": 14,
    "sniff_raster_mime": 14,
    "UrlRejectedError": 14,
    "address_is_not_public": 14,
    "vet_public_url": 14,
    "PinnedPathRefusal": 14,
    "copy_file_pinned": 14,
    "fd_real_path": 14,
    "is_reparse_point": 14,
    "pin_parent": 14,
    "supports_pinned_walk": 14,
}

__all__ = sorted({"SDK_LEVEL", "BACKEND_SDK_LEVEL", *_LAZY_EXPORTS})


def __getattr__(name: str) -> Any:
    submodule = _LAZY_EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{submodule}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
