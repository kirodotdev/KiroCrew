"""``python -m packaging.build`` -- curate a local crew into a deployable bundle.

WHY THIS IS A PORT, NOT A COPY
------------------------------
``PACKAGING-CONTRACT.md`` (T1) says to port ``bundle.py`` + ``bundle_source.py``
from ``share-my-crew/build/serving/smc/`` and that those files "carry
``reviewed_by`` / ``reviewed_at`` and a content-hash recheck". Read in full,
they do NOT: ``serving/smc/bundle.py`` is the container's READER (it validates a
bundle at startup) and ``serving/smc/bundle_source.py`` is the S3 FETCH that the
top-level contract explicitly DELETES. Neither enumerates a crew, neither
curates, and neither carries a review signature or a content pin.

The deny-by-default producer the contract describes is
``share-my-crew/build/export/crew_export/`` -- ``candidates.py`` (enumeration,
everything starts excluded), ``plan.py`` (the ``reviewed_by`` / ``reviewed_at``
signature and the per-item sha256 content pin), ``spec.py`` (prompt inlining and
tool/MCP normalisation) and ``bundle.py`` (the layout writer and the digest the
contract points at: ``_bundle_digest``). This module ports THAT, because a port
of the named files would ship no curation at all -- and "a port that loosens
this is worse than no port".

The port is NOT self-contained: it requires ``kiro_crew`` for its security
verdicts. ``crew_export`` imports ``kiro_crew.config.paths``,
``kiro_crew.knowledge.store``, ``kiro_crew.deploy.scan`` and
``kiro_crew.security``; when the app venv lacks PyYAML the curation plan is JSON
rather than YAML, and some credential helpers keep an import-free subset
fallback (see ``_HARD_PATTERNS`` and the report note about it). But the
security-verdict authorities are mandatory: ``kiro_crew.hooks`` (UNC-shape) and
``kiro_crew.security.is_sensitive_path`` own the sensitive-path and credential
verdict, and the build FAILS CLOSED -- it refuses rather than running -- when
either is unimportable, so it must run where ``kiro_crew`` is installed.

THE DENY-BY-DEFAULT SEAM, PRESERVED
-----------------------------------
A skill or MCP server enters the bundle ONLY when a signed review says so and its
content still matches what was reviewed. Two guards, both from
``crew_export/plan.py``:

* **The signature.** ``reviewed_by`` and ``reviewed_at`` start blank; a review
  file that selects anything while either is blank is refused. There is no flag
  to skip review -- a flag fails open when forgotten. Running with no ``--allow``
  at all is a valid outcome: an empty-but-valid bundle (persona + tools, no
  private skills, no owner MCP servers), so the failure direction is
  under-sharing.
* **The content pin.** Every reviewed entry records the sha256 of the content it
  was written from, and the build re-checks that hash for each SELECTED entry. A
  skill or server edited after approval refuses the build and is named.
  Yesterday's approval cannot be laundered across today's content.

INTERFACE (PACKAGING-CONTRACT.md T1)
------------------------------------
    python -m packaging.build --crew <name> --out <dir> [--allow <path>]...
    python -m packaging.build plan  --crew <name> --out <dir> [--allow <path>]...

``build`` (the default verb) writes the four-entry layout into ``<dir>`` and
prints, as the LAST line, ``SMC_BUNDLE_JSON=<path>`` naming a JSON file with
``crew_name``, ``bundle_dir``, ``digest``, ``skill_count``, ``mcp_servers`` and
``denied``. ``plan`` prints the same decision set and writes a fresh
deny-by-default review template, WITHOUT writing a bundle.

``--crew`` names the crew; its source is a "crew home" holding
``agents/<name>.json`` and ``skills/``. ``--source`` overrides that root (a test
points it at a fixture); by default the agent spec resolves under
``$KIRO_HOME`` / ``~/.kiro`` and skills under ``$KIROCREW_HOME`` -- the same
locations Kiro Crew uses (``kiro_crew/config/paths.py:604`` ``kiro_agents_dir`` =
``kiro_home()/agents``, ``:510`` ``kiro_home``; ``config_dir()/skills`` per
``crew_export/candidates.py``). Never defaults to a temp dir.


COMPOSITION
-----------
This module is the builder's one import path and its ``python -m`` entry point. What it
runs lives in the private package ``packaging.pipeline``, one owner per responsibility,
lowest layer first: ``contract`` (versions, names, the read ceiling, ``ExportRefused``),
``scan`` (credential scanning), ``sensitive`` (name and location fences), ``pinned``
(no-follow reads and walks), ``destination`` (writes to paths derived from ``--out``),
``hashing`` (the content pin and the bundle digest), ``crew`` (crew resolution and the
agent-spec read), ``candidates``, ``plan``, ``prompt`` (the external persona read),
``spec``, ``layout`` (the staged leaf writes), ``staging`` (the run marker, the ownership
proof and the private-aside disposal), ``report``, ``transaction`` (``build_bundle``) and
``cli``. An owner imports only owners below it and never this module.

Every name the one-module builder defined still resolves here, read from its owner on each
access, and a write here -- a test's ``monkeypatch.setattr`` -- lands on that owner. An
owner calls a function another owner defines THROUGH that owner's module, so the write
reaches every caller; see the facade below.
"""

from __future__ import annotations

# The standard-library names the one-module builder bound, so each still resolves here
# (the suites read ``build.os``). None of them is re-exported from an owner: each owner
# imports what it uses, so replacing one of these here replaces it for no owner.
import argparse  # noqa: F401
import base64  # noqa: F401
import errno  # noqa: F401
import hashlib  # noqa: F401
import importlib as _importlib
import json  # noqa: F401
import math  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import stat  # noqa: F401
import sys  # noqa: F401
import sys as _sys
import typing as _typing
import uuid  # noqa: F401
from collections.abc import Callable  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path, PurePosixPath  # noqa: F401
from types import ModuleType as _ModuleType
from typing import IO  # noqa: F401

# ---------------------------------------------------------------------------
# Composition: one import path and one patch surface over the pipeline owners
# ---------------------------------------------------------------------------
# A name an owner defines is NOT bound here. ``__getattr__`` reads it from that owner on
# each access, through ``sys.modules``, so the owner's namespace is the one place its value
# lives, and ``_ReExportModule`` sends a write or a delete of it to the same owner. Inside
# the pipeline an owner calls a function another owner defines through that owner's module
# (``_pinned._open_dir_nofollow_pinned(...)``), never a copy bound by ``from`` import, so a
# write landing on the defining owner reaches every call site -- the one namespace a patch
# of the one-module builder had. A class or constant is imported by name, which a write
# here does not reach. ``test_pipeline_composition.py`` resolves every write through this
# module in the repository's tests and refuses one of such a name, of a name not forwarded,
# of an attribute it cannot resolve, or with a ``create`` / ``raising`` that may create it.
#
# Owners are held as dotted names relative to this module's package, never as module
# objects, and resolved from ``sys.modules`` per use. ``__package__`` rather than
# ``__name__``: run as ``python -m packaging.build`` this module is ``__main__``.

#: Name -> the owner that DEFINES it, relative to this module's package.
_EXPORTS: dict[str, str] = {
    # contract
    "BUNDLE_VERSION": "pipeline.contract",
    "ExportRefused": "pipeline.contract",
    "PLAN_FILENAME": "pipeline.contract",
    "PLAN_VERSION": "pipeline.contract",
    "REPORT_VERSION": "pipeline.contract",
    "_BUILD_WRITES_EMPTY": "pipeline.contract",
    "_MAX_PROMPT_BYTES": "pipeline.contract",
    "_STAGING_OWNED_TOP_LEVEL": "pipeline.contract",
    # scan
    "Leak": "pipeline.scan",
    "_AWS_KEY_PREFIXES": "pipeline.scan",
    "_B64_DECODE_BUDGET": "pipeline.scan",
    "_B64_RUN_RE": "pipeline.scan",
    "_BARE_SECRET_ENTROPY_MIN": "pipeline.scan",
    "_BARE_SECRET_HEX_ONLY_RE": "pipeline.scan",
    "_BARE_SECRET_LEN": "pipeline.scan",
    "_BARE_SECRET_MAX_LOWER_RUN": "pipeline.scan",
    "_BARE_SECRET_MAX_VOWEL_RATIO": "pipeline.scan",
    "_BARE_SECRET_RUN_RE": "pipeline.scan",
    "_BARE_SECRET_VOWELS": "pipeline.scan",
    "_CANONICAL_CREDENTIAL_RE": "pipeline.scan",
    "_CANONICAL_REDACTOR": "pipeline.scan",
    "_HARD_CREDENTIAL_RE": "pipeline.scan",
    "_HARD_PATTERNS": "pipeline.scan",
    "_VENDOR_TOKEN_COMPILED": "pipeline.scan",
    "_VENDOR_TOKEN_PATTERNS": "pipeline.scan",
    "_bare_secret_decodes_to_printable": "pipeline.scan",
    "_bare_secret_window_is_key": "pipeline.scan",
    "_scan_bare_secret_runs": "pipeline.scan",
    "_scan_decoded_runs": "pipeline.scan",
    "redact_credentials": "pipeline.scan",
    "scan_text": "pipeline.scan",
    # sensitive
    "_CREDENTIAL_DIR_PARTS": "pipeline.sensitive",
    "_CREDENTIAL_NAME_RE": "pipeline.sensitive",
    "_SENSITIVE_RELATIVE_DIRS": "pipeline.sensitive",
    "_looks_sensitive_standalone": "pipeline.sensitive",
    "refused_by_location": "pipeline.sensitive",
    "refused_by_name": "pipeline.sensitive",
    # pinned
    "_NOFOLLOW_READ_FLAGS": "pipeline.pinned",
    "_dir_fd_closed": "pipeline.pinned",
    "_dir_fd_supported": "pipeline.pinned",
    "_is_redirecting_entry": "pipeline.pinned",
    "_nofollow_primitive_available": "pipeline.pinned",
    "_open_dir_nofollow_pinned": "pipeline.pinned",
    "_open_leaf_no_reparse": "pipeline.pinned",
    "_open_leaf_nofollow_at": "pipeline.pinned",
    "_read_bytes_openat": "pipeline.pinned",
    "_read_text": "pipeline.pinned",
    "_read_text_nofollow": "pipeline.pinned",
    "_read_text_openat": "pipeline.pinned",
    "_redirect_between": "pipeline.pinned",
    "_refuse_redirects_in_chain": "pipeline.pinned",
    "_refuse_without_nofollow_primitive": "pipeline.pinned",
    "_walk_no_reparse": "pipeline.pinned",
    # destination
    "_is_plain_file_no_follow": "pipeline.destination",
    "_refuse_unc_out": "pipeline.destination",
    "_refuse_unusable_parent": "pipeline.destination",
    "_write_bytes_nofollow": "pipeline.destination",
    "_write_nofollow": "pipeline.destination",
    # hashing
    "_sha": "pipeline.hashing",
    "_staged_tree_hash": "pipeline.hashing",
    "_tree_hash": "pipeline.hashing",
    "bundle_digest": "pipeline.hashing",
    # crew
    "ResolvedCrew": "pipeline.crew",
    "_default_config_dir": "pipeline.crew",
    "_default_kiro_home": "pipeline.crew",
    "_refuse_unless_launchable": "pipeline.crew",
    "_validated_crew_name": "pipeline.crew",
    "read_agent_spec": "pipeline.crew",
    "resolve_crew": "pipeline.crew",
    # candidates
    "Candidate": "pipeline.candidates",
    "_CONTAINER_OWNED_MCP": "pipeline.candidates",
    "_canonical_server": "pipeline.candidates",
    "enumerate_all": "pipeline.candidates",
    "mcp_candidates": "pipeline.candidates",
    "skill_candidates": "pipeline.candidates",
    # plan
    "Drift": "pipeline.plan",
    "Plan": "pipeline.plan",
    "_KINDS": "pipeline.plan",
    "_PLAN_INSTRUCTIONS": "pipeline.plan",
    "_decision_set": "pipeline.plan",
    "_denied_list": "pipeline.plan",
    "_require_plan_include": "pipeline.plan",
    "merge_plans": "pipeline.plan",
    "read_plan": "pipeline.plan",
    "verify": "pipeline.plan",
    "write_plan": "pipeline.plan",
    # prompt
    "_MAX_REDIRECT_HOPS": "pipeline.prompt",
    "_inline_prompt": "pipeline.prompt",
    "_refuse_share_reached_through_ancestors": "pipeline.prompt",
    "_resolve_prompt_path": "pipeline.prompt",
    "_within": "pipeline.prompt",
    # spec
    "SpecResult": "pipeline.spec",
    "_BUILTIN_TOOL_GROUPS": "pipeline.spec",
    "_DROPPED_SPEC_KEYS": "pipeline.spec",
    "_clean_mcp_server": "pipeline.spec",
    "build_spec": "pipeline.spec",
    # layout
    "_copy_skill": "pipeline.layout",
    "_write_guarded": "pipeline.layout",
    # staging
    "_CapturedTree": "pipeline.staging",
    "_RUN_ID": "pipeline.staging",
    "_STAGING_MARKER_BODY": "pipeline.staging",
    "_STAGING_MARKER_TOKEN": "pipeline.staging",
    "_dispose_via_private_aside": "pipeline.staging",
    "_inspect_captured_tree_fd": "pipeline.staging",
    "_is_shape_this_build_never_writes": "pipeline.staging",
    "_marker_is_ours": "pipeline.staging",
    "_marker_lines_are_this_run": "pipeline.staging",
    "_open_captured_dir_fd": "pipeline.staging",
    "_purge_staging_best_effort": "pipeline.staging",
    "_purge_via_private_aside": "pipeline.staging",
    "_read_regular_leaf_fd": "pipeline.staging",
    "_refuse_unless_this_build_wrote_it": "pipeline.staging",
    "_rmtree_pinned": "pipeline.staging",
    "_unlink_out_leaf_best_effort": "pipeline.staging",
    "_verify_build_wrote_captured_fd": "pipeline.staging",
    "_verify_captured_is_staging_fd": "pipeline.staging",
    "_write_marker_exclusive": "pipeline.staging",
    # report
    "BuildReport": "pipeline.report",
    "_HARD_LINK_UNSUPPORTED_ERRNOS": "pipeline.report",
    "_publish_report": "pipeline.report",
    "_refuse_report_dir_without_hard_link_support": "pipeline.report",
    "_refuse_unless_our_report": "pipeline.report",
    "_write_report_temp": "pipeline.report",
    # transaction
    "build_bundle": "pipeline.transaction",
    # cli
    "_cmd_build": "pipeline.cli",
    "_cmd_plan": "pipeline.cli",
    "_print_decision": "pipeline.cli",
    "_source_from": "pipeline.cli",
    "main": "pipeline.cli",
}


def _submodule(module: str) -> _ModuleType:
    """Return one owner, read from where modules are stored.

    :data:`sys.modules` answers first, so a purged or replaced owner is seen at once;
    ``importlib.import_module`` answers only a miss, which keeps a test that patches it for
    its own reasons from rerouting every read of this surface.
    """
    module_name = f"{__package__}.{module}"
    try:
        return _sys.modules[module_name]
    except KeyError:
        return _importlib.import_module(module_name)


def _owner(name: str) -> _ModuleType:
    """Return the owner that defines ``name``, resolved on each access."""
    return _submodule(_EXPORTS[name])


def _bound_exports() -> set[str]:
    """Return the exported names their owner binds, which are the ones that resolve here."""
    return {name for name in _EXPORTS if hasattr(_owner(name), name)}


if _typing.TYPE_CHECKING:  # pragma: no cover - read by the type checker, never run
    # The exported names as the type checker sees them: at run time each resolves through
    # ``__getattr__`` below, which the checker is not shown, so a misspelled or mis-called
    # ``build.X`` stays a type error.
    from .pipeline.candidates import (  # noqa: F401
        _CONTAINER_OWNED_MCP,
        Candidate,
        _canonical_server,
        enumerate_all,
        mcp_candidates,
        skill_candidates,
    )
    from .pipeline.cli import (  # noqa: F401
        _cmd_build,
        _cmd_plan,
        _print_decision,
        _source_from,
        main,
    )
    from .pipeline.contract import (  # noqa: F401
        _BUILD_WRITES_EMPTY,
        _MAX_PROMPT_BYTES,
        _STAGING_OWNED_TOP_LEVEL,
        BUNDLE_VERSION,
        PLAN_FILENAME,
        PLAN_VERSION,
        REPORT_VERSION,
        ExportRefused,
    )
    from .pipeline.crew import (  # noqa: F401
        ResolvedCrew,
        _default_config_dir,
        _default_kiro_home,
        _refuse_unless_launchable,
        _validated_crew_name,
        read_agent_spec,
        resolve_crew,
    )
    from .pipeline.destination import (  # noqa: F401
        _is_plain_file_no_follow,
        _refuse_unc_out,
        _refuse_unusable_parent,
        _write_bytes_nofollow,
        _write_nofollow,
    )
    from .pipeline.hashing import (  # noqa: F401
        _sha,
        _staged_tree_hash,
        _tree_hash,
        bundle_digest,
    )
    from .pipeline.layout import (  # noqa: F401
        _copy_skill,
        _write_guarded,
    )
    from .pipeline.pinned import (  # noqa: F401
        _NOFOLLOW_READ_FLAGS,
        _dir_fd_closed,
        _dir_fd_supported,
        _is_redirecting_entry,
        _nofollow_primitive_available,
        _open_dir_nofollow_pinned,
        _open_leaf_no_reparse,
        _open_leaf_nofollow_at,
        _read_bytes_openat,
        _read_text,
        _read_text_nofollow,
        _read_text_openat,
        _redirect_between,
        _refuse_redirects_in_chain,
        _refuse_without_nofollow_primitive,
        _walk_no_reparse,
    )
    from .pipeline.plan import (  # noqa: F401
        _KINDS,
        _PLAN_INSTRUCTIONS,
        Drift,
        Plan,
        _decision_set,
        _denied_list,
        _require_plan_include,
        merge_plans,
        read_plan,
        verify,
        write_plan,
    )
    from .pipeline.prompt import (  # noqa: F401
        _MAX_REDIRECT_HOPS,
        _inline_prompt,
        _refuse_share_reached_through_ancestors,
        _resolve_prompt_path,
        _within,
    )
    from .pipeline.report import (  # noqa: F401
        _HARD_LINK_UNSUPPORTED_ERRNOS,
        BuildReport,
        _publish_report,
        _refuse_report_dir_without_hard_link_support,
        _refuse_unless_our_report,
        _write_report_temp,
    )
    from .pipeline.scan import (  # noqa: F401
        _AWS_KEY_PREFIXES,
        _B64_DECODE_BUDGET,
        _B64_RUN_RE,
        _BARE_SECRET_ENTROPY_MIN,
        _BARE_SECRET_HEX_ONLY_RE,
        _BARE_SECRET_LEN,
        _BARE_SECRET_MAX_LOWER_RUN,
        _BARE_SECRET_MAX_VOWEL_RATIO,
        _BARE_SECRET_RUN_RE,
        _BARE_SECRET_VOWELS,
        _CANONICAL_CREDENTIAL_RE,
        _CANONICAL_REDACTOR,
        _HARD_CREDENTIAL_RE,
        _HARD_PATTERNS,
        _VENDOR_TOKEN_COMPILED,
        _VENDOR_TOKEN_PATTERNS,
        Leak,
        _bare_secret_decodes_to_printable,
        _bare_secret_window_is_key,
        _scan_bare_secret_runs,
        _scan_decoded_runs,
        redact_credentials,
        scan_text,
    )
    from .pipeline.sensitive import (  # noqa: F401
        _CREDENTIAL_DIR_PARTS,
        _CREDENTIAL_NAME_RE,
        _SENSITIVE_RELATIVE_DIRS,
        _looks_sensitive_standalone,
        refused_by_location,
        refused_by_name,
    )
    from .pipeline.spec import (  # noqa: F401
        _BUILTIN_TOOL_GROUPS,
        _DROPPED_SPEC_KEYS,
        SpecResult,
        _clean_mcp_server,
        build_spec,
    )
    from .pipeline.staging import (  # noqa: F401
        _RUN_ID,
        _STAGING_MARKER_BODY,
        _STAGING_MARKER_TOKEN,
        _CapturedTree,
        _dispose_via_private_aside,
        _inspect_captured_tree_fd,
        _is_shape_this_build_never_writes,
        _marker_is_ours,
        _marker_lines_are_this_run,
        _open_captured_dir_fd,
        _purge_staging_best_effort,
        _purge_via_private_aside,
        _read_regular_leaf_fd,
        _refuse_unless_this_build_wrote_it,
        _rmtree_pinned,
        _unlink_out_leaf_best_effort,
        _verify_build_wrote_captured_fd,
        _verify_captured_is_staging_fd,
        _write_marker_exclusive,
    )
    from .pipeline.transaction import (  # noqa: F401
        build_bundle,
    )
else:

    def __getattr__(name: str) -> _typing.Any:
        """Read an exported name from the owner that defines it (:pep:`562`)."""
        # ``from ... import *`` reads ``__all__`` through here. It carries what the one-module
        # builder's star import did -- the public names bound here and the exports their owner
        # binds -- and is computed per read because ``redact_credentials`` exists only where
        # its optional import succeeded. Filtered at import it would load every owner there,
        # but owners load on first access: ``import build`` runs none, and run by file path
        # there is no package to load one from, so the main guard refuses first.
        if name == "__all__":
            return sorted(n for n in set(globals()) | _bound_exports() if not n.startswith("_"))
        if name not in _EXPORTS:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_owner(name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | _bound_exports())


class _ReExportModule(_ModuleType):
    """Send a write or a delete of an exported name to the owner that defines it.

    A binding here instead would shadow the owner for every later read, because
    ``__getattr__`` runs only for a name this module does not hold, and ``monkeypatch``'s
    undo -- which reads the attribute, then writes it back -- would then install the patched
    value for the life of the process. Forwarded, there is one value to remember and one to
    put back, so ``monkeypatch`` and ``mock.patch`` without ``create=True`` round-trip.
    """

    def __setattr__(self, name: str, value: _typing.Any) -> None:
        if name in _EXPORTS:
            setattr(_owner(name), name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _EXPORTS:
            delattr(_owner(name), name)
        else:
            super().__delattr__(name)


# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _ReExportModule

if __name__ == "__main__":  # pragma: no cover - the CLI tests run it in a child process
    if not __package__:
        # Run by file path there is no package to resolve the owners against.
        print(
            "refused: run the crew bundle builder as `python -m packaging.build`, with the "
            "crew directory on the import path; it resolves its pipeline from that package.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    raise SystemExit(_submodule("pipeline.cli").main())
