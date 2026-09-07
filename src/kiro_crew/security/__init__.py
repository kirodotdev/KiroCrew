"""Built-in security controls — deny list, sensitive path protection, and audit scanning."""

from __future__ import annotations

import asyncio
import base64
import bisect
import fnmatch
import ipaddress
import json
import logging
import math
import os
import re
import shlex
import socket
import string
import sys
import threading
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse

from kiro_crew.credential_patterns import AWS_KEY_ID, JWT_MULTI_SEGMENT
from kiro_crew.executors import (
    _MAX_PATH_RESOLVE_WORKERS,
    maintenance_executor,
    path_resolve_executor,
)
from kiro_crew.identity_stores import (
    AUTH_SQLITE_DB,
    AUTH_SQLITE_SIDECAR_SUFFIXES,
    fenced_home_dirs,
)
from kiro_crew.sel import SecurityEvent, SecurityEventLog
from kiro_crew.trust_patterns import ENV_ASSIGNMENT_RE

from . import (
    argv_floor,
    denied_rules,
    diagnostics,
    exfil,
    helpers,
    paths,
    redaction,
    shell_normalizer,
    vocabulary,
)
from .argv_floor import (
    _AMBIGUOUS_REFS,
    _AMBIGUOUS_REFSPEC_RE,
    _DEV_MODE_CONFIRM_FLAG,
    _GIT_ARG_FLAGS,
    _GIT_PUBLISH_DENY_LABEL,
    _GIT_PUBLISH_GLUE_RE,
    _GIT_PUBLISH_RE,
    _GIT_PUBLISH_SUBST_PROGRAM_RE,
    _INLINE_DYNAMIC_EXEC_RE,
    _PROCESS_SUBSTITUTION_SAFE_CHARS,
    _PROTECTED_BRANCHES,
    _PUSH_ALL_BRANCHES_OPTS,
    _PUSH_NO_VALUE_OPTS,
    _PUSH_NO_VALUE_SHORTS,
    _PUSH_REPO_OPTS,
    _PUSH_VALUE_OPTS,
    _PUSH_VALUE_SHORTS,
    _RAW_ASSIGNMENT_RE,
    _SELF_CLOUD_DESTRUCTIVE_VERBS,
    _SELF_FLOOR_MACHINERY_RE,
    _SELF_FLOOR_NAME_HINT_RE,
    _SELF_FLOOR_QUOTE_JUNK_RE,
    _SELF_IMPORT_RE,
    _SELF_MODULE_SPELLINGS,
    _SHELL_RESERVED_WORDS,
    _backtick_closer,
    _bare_kill_raw_bodies,
    _git_publish_floor_tags,
    _git_push_args,
    _has_self_importing_inline_program,
    _inline_payload_reaches_cli,
    _is_credential_mint,
    _is_dev_mode_out_of_root_confirm,
    _is_git_publish,
    _is_git_push_via_normalizer,
    _is_kill_by_name_program,
    _is_push_to_protected_branch,
    _is_self_cloud_destructive,
    _is_self_gateway_restart,
    _is_self_kill,
    _is_self_module_flag,
    _is_self_module_invocation,
    _is_self_restart,
    _is_self_update,
    _kill_prefix_keeps_anchor,
    _matches_self_subcommand,
    _normalize_ref,
    _operands_lead_with,
    _process_substitution_word_is_opaque,
    _push_segment_targets_protected,
    _python_reads_stdin,
    _self_cli_operands,
    _self_floor_can_fire,
    _self_module_flag_scan,
    _self_module_name_index,
    _self_program_index,
    _self_token_frames,
    _SelfModuleScan,
    _shell_payload_sources,
    _static_substitution_output,
    _stdin_program_text,
    _stdin_redirect_carriers,
)
from .denied_rules import (
    _AWS_SECRET_VAR_NAMES,
    _AWS_SECRET_WORD_PREFIXES,
    _AWS_SECRET_WORDS,
    _AWS_VAR_SELECTOR,
    _DANGEROUS_AWS_FLAG_RUN,
    _DENY_EXCEPTIONS,
    _DENY_FALLBACK_SCAN_MAX_CHARS,
    _DENY_MATCHER_CACHE,
    _ENV_CRED_DENIAL_REASON,
    _ENV_CRED_PATTERNS,
    _ENV_CRED_SHARED_RULE_IDS,
    _ENV_CRED_SHARED_RULES,
    _ENV_DUMP_GREP_AWS_PATTERN,
    _ENV_DUMP_VERBS,
    _FLOOR_ENFORCED_RULE_IDS,
    _GIT_PUBLISH_FLOOR_BY_ID,
    _GIT_PUBLISH_FLOOR_NOTES,
    _GIT_PUBLISH_RULE_CATEGORY,
    _GIT_PUBLISH_RULE_PATTERNS,
    _GIT_PUBLISH_RULES,
    _GIT_PUBLISH_UNGATED,
    _GIT_PUBLISH_UNGATED_RULE_IDS,
    _INERT_SEARCH_GLOBS,
    _INERT_SEARCH_VERBS,
    _INTERPRETER_RULE_IDS,
    _INTERPRETER_RULE_PATTERNS,
    _LEGACY_RULE_ID_BY_PATTERN,
    _LINEARIZED_AWS_FLAG_RUN,
    _LITERAL_CONCAT_RE,
    _PRINTENV_AWS_SECRET_PATTERN,
    _RULE_ID_BY_PATTERN,
    _RULES_BY_ID,
    _SELF_PROTECTION_FLOOR_BY_ID,
    _SELF_PROTECTION_FLOOR_NOTES,
    _SELF_PROTECTION_FLOOR_PATTERNS,
    _SELF_PROTECTION_FLOOR_RULE_IDS,
    _SELF_PROTECTION_UNGATED_FLOOR_IDS,
    BUILTIN_DENIED_RULES,
    BUILTIN_DENY_PATTERNS,
    DENY_REASON_MATCH_PREFIX,
    DENY_REASON_PREFIX,
    SUSPICIOUS_BASH_PATTERNS,
    DeniedCommandRule,
    _aws_secret_word_prefix_alternation,
    _check_env_credential_access,
    _deny_matcher,
    _deny_pattern_matches,
    _deny_reason,
    _DenyMatcher,
    _exception_eligible,
    _frags_can_underconsume,
    _has_top_level_alternation,
    _linearize_deny_pattern,
    _matches_full_input,
    _polynomial_backtracking_prone,
    _redos_prone,
    _resolved_pin_ids,
    _rule_id_for_pattern,
    _split_deny_frags,
    builtin_denied_rules,
    compute_effective_denied,
    edition_denied_rules,
    enabled_rule_ids,
    floor_enforced_builtin_command_ids,
    is_safe_user_regex,
    pinned_builtin_command_ids,
    pinned_builtin_command_ids_for_snapshot,
)
from .diagnostics import (
    REFUSAL_DIAGNOSTIC_PREFIX,
    RefusalDiagnostic,
    RefusalSpanShape,
    annotate_refusal,
    refusal_diagnostic,
    refusal_span_shape,
)
from .exfil import (
    _BASH_EXFIL_PATTERNS,
    _BASH_EXFIL_RES,
    _BASH_EXFIL_ROW_DISCRIMINATORS,
    _BASH_EXFIL_RULE_BY_LABEL,
    _BASH_EXFIL_RULE_BY_PATTERN,
    _CREDENTIAL_RE,
    _ENDPOINT_EXTENSION_CAP,
    _ENDPOINT_EXTENSION_ENTRIES_KEY,
    _EXFIL_PATTERNS,
    _EXFIL_PERCENT_RE,
    _EXFIL_QUERY_MIN_LEN,
    _HARD_CREDENTIAL_RE,
    _IMDS_IP,
    _IMDS_IPV6,
    _IP_CANDIDATE_RE,
    _IP_COMPONENT,
    _MAX_URL_DECODE_PASSES,
    _OAUTH_AUTHORIZATION_ENDPOINTS,
    _OAUTH_DIAGNOSTIC_PARAMETER_RE,
    _OAUTH_ENTROPY_QUERY_PARAMS,
    _OAUTH_EXTENSION_AUDITED,
    _OAUTH_EXTENSION_HOST_RE,
    _OAUTH_EXTENSION_MEMO,
    _OAUTH_EXTENSION_PATH_BAD,
    _OAUTH_EXTENSION_PATH_MAX_LEN,
    _OAUTH_QUERY_PARAMS,
    _OAUTH_S256_CHALLENGE_RE,
    _OAUTH_URL_SYMBOLS,
    _S3_PRESIGNED_PARAMS,
    _S3_PRESIGNED_RE,
    _SIGNATURE_RE,
    _SLACK_APP_CREATE_PARAMS,
    _STRUCTURAL_VALIDATORS,
    _STS_TOKEN_RE,
    _URL_RE,
    OAuthUrlCredentialDiagnostic,
    OAuthUrlShapeProfile,
    _approved_oauth_authorization_endpoint,
    _check_imds_access,
    _emit_oauth_extension_used_event,
    _exempt_exact_hosts,
    _exfil_exempt_hosts,
    _exfil_rule_id_for_match,
    _exfil_url_warning,
    _is_safe_presigned,
    _kirocrew_slack_app_link_alias,
    _load_operator_oauth_endpoints,
    _oauth_char_class,
    _oauth_credential_scan_target,
    _oauth_diagnostic,
    _oauth_entropy_form_is_protocol_shaped,
    _oauth_entropy_value_is_protocol_shaped,
    _oauth_query_diagnostic,
    _oauth_shape_profile,
    _oauth_url_payload_diagnostic,
    _safe_oauth_parameter_name,
    _slack_manifest_payload_re,
    _slack_manifest_re_slot,
    _valid_oauth_extension_path,
    _validate_operator_oauth_entries,
    audit_bash_exfiltration,
    canonicalize_ip,
    diagnose_oauth_url_credential,
    exfil_query_min_len,
    oauth_url_contains_credential,
    redact_exfiltration_urls,
    scan_exfiltration_urls,
)
from .helpers import (
    _RLIMIT_DEFAULTS,
    _bias_child_oom_score,
    _contains_injection,
    _resource,
    apply_resource_limits,
    contains_injection,
    resource_limit_spec,
)
from .paths import (
    _CREW_HOME_PREFIXES,
    _CREW_SECRET_LEAVES,
    _HOME_TARGETS_TTL_SECS,
    _KEYSTONE_ARTIFACT_PARENTS,
    _KEYSTONE_ARTIFACT_SUFFIXES,
    _KIRO_AGENTS_DIR,
    _ON_WINDOWS,
    _OVERRIDE_ANCHORED_LEAVES,
    _OVERRIDE_ROOT_ENVS,
    _PATH_RESOLVE_COOLDOWN_MAX_SECS,
    _PATH_RESOLVE_COOLDOWN_SECS,
    _PATH_RESOLVE_TIMEOUT_SECS,
    _SENSITIVE_HOME_DIRS,
    _UNC_PREFIX_RE,
    _WRITE_PROTECTED_HOME_PATHS,
    DENIED_ROOT_PARTS,
    MAX_SCANNABLE_COMMAND_CHARS,
    MAX_SCANNABLE_SOURCE_BODY_CHARS,
    PathResolutionStalled,
    _candidate_forms,
    _expanded_env_root,
    _home_dir_targets,
    _home_dir_targets_uncached,
    _home_targets_cache,
    _is_keystone_publish_artifact,
    _is_unc_path,
    _lexical_root,
    _mark_stalled,
    _oversize_refusal,
    _path_in_home_dirs,
    _path_resolve_clock,
    _path_resolve_degraded,
    _path_resolve_lock,
    _path_resolve_wedged,
    _realpath_or_none,
    _rebuild_targets_bounded,
    _resolve_root_anchors,
    _resolved_env_root,
    _resolved_forms_bounded,
    _resolved_root_key,
    _resolved_spellings,
    _ResolvedRoots,
    _run_resolution_bounded,
    _stall_prefix,
    _wedged_workers,
    crew_home_prefixes,
    is_sensitive_bash_command,
    is_sensitive_path,
    is_sensitive_write_path,
    path_contains_sensitive,
    sandbox_credential_targets,
    sensitive_home_dirs,
    write_protected_home_paths,
)
from .redaction import (
    _B64_CHUNK_RE,
    _BARE_SECRET_RUN_RE,
    _CREDENTIAL_PATTERNS,
    _CREDENTIAL_PREFILTER_AUTHORIZATION_RE,
    _CREDENTIAL_PREFILTER_DISCORD_RE,
    _CREDENTIAL_PREFILTER_GH_RE,
    _CREDENTIAL_PREFILTER_LITERALS,
    _CREDENTIAL_PREFILTER_TELEGRAM_RE,
    _CREDENTIAL_PREFILTER_URI_RE,
    _ENTROPY_TERMS_KEY_LEN,
    _HEX_ONLY_RE,
    _LOCAL_PATH_PLACEHOLDER,
    _LOCAL_PATH_RE,
    _PREFILTER_MIN_LEN,
    _PRINTABLE_BYTES,
    _REDACTED_CREDENTIAL_TAG,
    _REDACTED_ENCODED_CREDENTIAL_TAG,
    _SECRET_ENTROPY_MIN,
    _SECRET_KEY_LEN,
    _SECRET_MAX_LOWER_RUN,
    _SECRET_MAX_VOWEL_RATIO,
    _SECRET_PRINTABLE_DECODE_RATIO,
    _VOWELS,
    CREDENTIAL_REDACTION_TAGS,
    REDACTED_CREDENTIAL_TAG,
    _contains_bare_secret,
    _contains_fixed_credential,
    _decode_b64_chunk,
    _decode_b64_safe,
    _decodes_to_printable_text,
    _has_all_three_char_classes,
    _looks_like_secret_key,
    _lowercase_run_exceeds,
    _might_contain_credential,
    _shannon_entropy,
    _text_contains_bare_secret,
    _vowel_ratio,
    get_credential_patterns,
    redact_credentials,
    redact_local_paths,
)
from .shell_normalizer import (
    _AMBIGUOUS_EXPANSION_RE,
    _ANSI_C_LITERAL_ESCAPES,
    _ANSI_C_NUMERIC_ESCAPE_RE,
    _ANSI_C_QUOTE_RE,
    _ANSI_C_SPACE_ESCAPES,
    _ARRAY_ASSIGN_RE,
    _ARRAY_EXPAND_RE,
    _CARRIER_SPLIT_WINDOW,
    _CMD_SEPARATOR_RE,
    _CMD_SPLIT_RE,
    _COMPUTED_VALUE_RE,
    _CONTROL_OPERATOR_RE,
    _DATA_CONSUMER_PROGRAMS,
    _EMPTY_QUOTE_RE,
    _EMPTY_SUBST_RE,
    _ENV_SPLIT_PROGRAMS,
    _FUNC_DEF_RE,
    _GLOB_CHARS_RE,
    _HOME_VAR_RE,
    _INDIRECT_VAR_USE_RE,
    _LOCAL_ASSIGN_RE,
    _NESTED_SHELL_PROGRAMS,
    _NESTED_SHELL_VERBS,
    _NUMERIC_ESCAPE_RE,
    _ONE_CHAR_CLASS_RE,
    _OUTPUT_REDIRECT_RE,
    _PARAM_DEFAULT_RE,
    _PARAM_TRANSFORM_RE,
    _PRINTF_ESCAPES,
    _PROCESS_SUBSTITUTION_OPENERS,
    _PUSH_REDIRECTION_RE,
    _PYTHON_INLINE_PROGRAM_FLAGS,
    _PYTHON_OPERAND_FLAGS,
    _PYTHON_PROGRAM_RE,
    _REDIRECT_START_RE,
    _SCRIPT_EXECUTES_RE,
    _SHELL_ACTIVE_CHARS,
    _SHELL_ASSIGN_RE,
    _SHELL_COMMAND_FLAG_RE,
    _SHELL_COMMAND_GLUED_RE,
    _SHELL_LINE_CONTINUATION_RE,
    _SHELL_OPERATOR_CHARS,
    _SHELL_SEGMENT_SEPARATORS,
    _SHELL_VAR_NAMES,
    _SHELL_VAR_RE,
    _SHELL_WRAPPER_CHARS,
    _VAR_USE_RE,
    _argv_programs,
    _array_assignments,
    _continuation_width,
    _cut_at_operator,
    _data_consumer_command_disqualified,
    _data_consumer_exempt,
    _debracket,
    _decode_ansi_c_body,
    _decode_printf_escapes,
    _decode_shell_quoted_literals,
    _dequote_token,
    _ends_argv,
    _escape_code_is_inert,
    _fold_line_continuations,
    _glob_could_expand_to,
    _glob_to_regex,
    _glued_shell_command_payload,
    _here_string_payload,
    _heredoc_marker,
    _is_computed_value,
    _is_env_split_flag,
    _is_glued_shell_command_token,
    _is_herestring_token,
    _is_mint_verb,
    _is_not_double_dash,
    _is_self_program,
    _is_shell_command_flag,
    _is_shell_variable_reference,
    _iter_shell_chars,
    _matching_close_paren,
    _mint_verb_in_substitution,
    _nested_shell_payloads,
    _next_stop_indexes,
    _normalize_operand,
    _numeric_escape_char,
    _numeric_escape_code,
    _operand_span_end,
    _output_redirect_scan,
    _pipes_into_evaluator,
    _program_basename,
    _protected_name_in_substitution,
    _push_option_matches,
    _push_token_redirection,
    _push_token_shell_read,
    _redirect_consumes_next,
    _redirect_glue_point,
    _resolve_function_aliases,
    _resolve_local_assignments,
    _resolve_param_defaults,
    _sed_exec_replacement,
    _self_tokens,
    _shell_c_carrier_glued,
    _shell_c_carrier_payloads,
    _shell_join_continuations,
    _shell_payload_walk,
    _shell_quote_walk,
    _shell_tokens,
    _ShellChar,
    _ShellWalk,
    _split_glued_operators,
    _split_push_command_segments,
    _split_shell_words,
    _strip_redirect,
    _substitution_bodies,
    _substitution_depth_delta,
    _substitution_program,
    _xargs_reconstructed_command,
    normalize_shell_command,
)
from .vocabulary import (
    _KILL_BY_NAME_PROGRAMS,
    _SELF_NAME_RE,
    _SELF_PROGRAM_RE,
    _SELF_PROGRAM_SPELLINGS,
)

# NB: kiro_crew.vector_memory is imported lazily inside scan_memory() rather than
# at module top level. vector_memory.py imports redact_credentials/
# redact_exfiltration_urls from this module at ITS top level, so a top-level
# import here would create a circular import — under which the ImportError guard
# would silently set the store to None and disable scan_memory(). The deferred
# import breaks the cycle and also keeps the numpy/faiss/snowballstemmer stack
# off the lightweight import path.

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

logger = logging.getLogger(__name__)


def _schedule_push_allow_audit(command: str) -> None:
    """Fire-and-forget audit write offloaded to the maintenance executor.

    Avoids blocking the event loop on file I/O (same concern as
    ``_emit_deny_event`` — both should be offloaded per the
    no-blocking-call-on-event-loop guideline).  Falls back to an inline
    synchronous write when no event loop is running (sync/test contexts).
    """
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(maintenance_executor(), _emit_push_allow_event, command)
    except RuntimeError:
        # No running loop (called from a sync test or non-async context) —
        # fall back to inline write (acceptable: no event loop to block).
        _emit_push_allow_event(command)


def _emit_push_allow_event(command: str) -> None:
    """Emit a SEL audit event when a feature-branch push is allowed through.

    Best-effort: an audit failure is logged at WARNING and does not affect the
    allow decision (the push already passed the protected-branch gate).
    """
    try:
        sel = SecurityEventLog()
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="push_allowed",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation="git_push",
                outcome="allowed",
                resources="feature_branch_push",
                metadata={
                    "command": redact_and_truncate(command, 200),
                    "mechanism": "BRANCH_GATE",
                },
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for push_allowed (allow stands)",
            exc_info=True,
        )


# Longest path echoed back by ``sanitized_oauth_endpoint``. Real authorization
# endpoint paths are short (the longest builtin is 31 chars); anything past this
# bound is noise at best and smuggled payload at worst, so it is truncated with
# an ellipsis rather than surfaced whole.
_SANITIZED_OAUTH_PATH_MAX_LEN = 200

# DNS caps a full hostname at 253 octets; a longer "host" is not a hostname.
_SANITIZED_OAUTH_HOST_MAX_LEN = 253


def _contains_format_characters(text: str) -> bool:
    """True when *text* carries Unicode format characters (category Cf).

    Zero-width and directional format characters (U+200B ZERO WIDTH SPACE,
    U+200D ZWJ, U+2060 WORD JOINER, RTL/LTR marks, ...) are invisible in a
    rendered banner: a credential split by them fails every substring pattern
    here yet visually reassembles in the browser. Real authorization-endpoint
    components are plain ASCII, so their mere presence is disqualifying.
    """
    return any(unicodedata.category(ch) == "Cf" for ch in text)


def _oauth_component_is_unsafe(text: str) -> bool:
    """True when a URL component carries credential-like material at ANY decode layer.

    Mirrors the rejection gate's decode budget (``_MAX_URL_DECODE_PASSES``): the
    gate rejects a double-encoded credential on a DEEPER decode pass, so a
    sanitizer that scanned only one layer would echo the very bytes the gate
    refused. Fail-closed like the gate: a component still percent-decodable when
    the budget runs out, or one carrying a heavy percent-encoded run, is unsafe
    even when no known pattern matched.
    """
    if _EXFIL_PERCENT_RE.search(text):
        return True
    candidate = text
    for _ in range(_MAX_URL_DECODE_PASSES + 1):
        # Invisible format characters (category Cf) split a credential so no
        # substring pattern below can match it, while the browser renders the
        # fragments visually reassembled. No legitimate endpoint component
        # contains them, so presence alone is unsafe — checked on every decode
        # layer because %E2%80%8B only becomes U+200B after a decode pass.
        if _contains_format_characters(candidate):
            return True
        # _EXFIL_PATTERNS is included because it is a pattern family the
        # REJECTION itself can fire on (plus-delimited private-key headers,
        # SSH keys, token shapes) — a component must never be echoed when it
        # matches what the gate refused. Over-matching only redacts more.
        if (
            _contains_fixed_credential(candidate)
            or _text_contains_bare_secret(candidate)
            or _EXFIL_PATTERNS.search(candidate)
        ):
            return True
        # unquote_plus, not unquote: form-encoded material delimits with "+"
        # (e.g. a plus-separated private-key header), which only matches the
        # credential patterns once folded to spaces. Display never uses this
        # decoded form, so the wider fold cannot distort what is surfaced.
        decoded = unquote_plus(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    # Still decodable after the budget — same deliberate fail-closed posture as
    # the gate's saturation guard: refuse to echo what cannot be fully scanned.
    return True


def sanitized_oauth_endpoint(url: str) -> tuple[str, str] | None:
    """Best-effort ``(host, path)`` of an OAuth URL, safe to surface to users.

    :func:`oauth_url_contains_credential` answers only a boolean, so its
    callers historically could not tell the user WHICH endpoint tripped the
    scanner — the remedy (``oauth_endpoints.json``) needs an exact host+path to
    be actionable. This sibling names the endpoint without weakening the
    rejection:

    * only the lowercase hostname and the path are returned — NEVER the query,
      fragment, port, or userinfo, which is where state/PKCE material and
      smuggled credentials live;
    * both components are scanned at every percent-decode layer up to the
      gate's own budget: a credential-bearing path (raw, encoded, or
      over-encoded past the budget) is replaced with the shared redaction tag,
      and a credential-bearing HOSTNAME makes the whole helper return ``None``
      — a host is an identity, so a redacted host would name nothing;
    * both components are length-capped, so a pathological URL cannot bloat a
      banner or a log line.

    Returns ``None`` when the URL does not parse to a hostname, so callers fall
    back to their existing unnamed message. Deliberately independent of WHY the
    URL was rejected: it never re-runs the credential verdict.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        return None
    if not host:
        return None
    # A userinfo-bearing authority is never named. Raw userinfo is stripped by
    # parsed.hostname, but PERCENT-ENCODED userinfo (user%3Apass%40host, or the
    # double-encoded %2540 form that survives one decode pass) hides inside
    # what urlparse reports as the hostname — check for "@" at EVERY decode
    # layer up to the gate's budget, and refuse to name an authority that is
    # still decodable when the budget runs out.
    netloc_candidate = parsed.netloc
    for _ in range(_MAX_URL_DECODE_PASSES + 1):
        if "@" in netloc_candidate:
            return None
        decoded_netloc = unquote_plus(netloc_candidate)
        if decoded_netloc == netloc_candidate:
            break
        netloc_candidate = decoded_netloc
    else:
        return None
    # Scan BEFORE truncating (both components): a credential split by a length
    # cap must still trigger redaction, not survive in half.
    host = host.lower()
    if _oauth_component_is_unsafe(host):
        return None
    if not host.isascii():
        # Surface an internationalized host in A-label (punycode) form: it
        # defuses homoglyph spoofing in the banner and matches the ASCII-only
        # shape an oauth_endpoints.json entry must take anyway.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        # INVARIANT: the exact byte sequence surfaced must have passed the
        # scan in its FINAL form. IDNA's nameprep folds fullwidth characters
        # to ASCII, so a token-shaped fullwidth host that the pre-IDNA scan
        # could not match can NORMALIZE INTO a credential — re-scan the
        # transformed form and refuse to name it.
        if _oauth_component_is_unsafe(host):
            return None
    host = host[:_SANITIZED_OAUTH_HOST_MAX_LEN]
    path = parsed.path or "/"
    if _oauth_component_is_unsafe(path):
        path = _REDACTED_CREDENTIAL_TAG
    elif len(path) > _SANITIZED_OAUTH_PATH_MAX_LEN:
        path = path[:_SANITIZED_OAUTH_PATH_MAX_LEN] + "…"
    return host, path


# ── Binary File MIME Allowlist ──
# Files whose UTF-8 decode fails are accepted by file_send / outbox only when
# their guessed MIME type is in this allowlist. Deny-by-default; expand only
# when a use case is reviewed for safe rendering on the dashboard origin.
# SVG is intentionally excluded (can carry inline scripts); served as
# attachment by the download handler for defense-in-depth.
BINARY_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/ogg",
        "audio/flac",
        "audio/aac",
        "audio/mp4",
        "audio/webm",
        "audio/opus",
        "video/mp4",
        "video/webm",
        "video/ogg",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "application/pdf",
    }
)


def redact(text: str) -> str:
    """Apply all redaction passes (exfiltration URLs + credentials)."""
    text = redact_exfiltration_urls(text)[0]
    text = redact_credentials(text)[0]
    return text


# ── Streaming redaction (pentest issue 3) ──
# Per-chunk redaction misses a credential split across token/streaming
# boundaries: a chunk ending ``...AKIA`` and the next starting ``IOSFODNN7...``
# each individually escape redact_credentials(), so the raw fragments reach
# WebSocket/SSE consumers even though the final assembled message is redacted.
# StreamRedactor withholds the trailing run of "credential-class" characters
# (which could be the start of a not-yet-complete credential) until a
# terminator arrives or the stream ends, redacting only the confirmed-safe
# prefix before it is emitted on the wire.

# Characters that can appear inside a credential token/pattern. A credential is
# a contiguous run of these; any byte OUTSIDE this set terminates an in-progress
# match, so text up to (and including) such a terminator is safe to redact and
# emit. Includes URL / base64 / connection-string punctuation so exfil URLs and
# DB URIs are also held intact across chunk boundaries — plus quotes and URL
# query delimiters (``"`` ``'`` ``?&#``) so a JSON key/value or query-string
# secret is not committed piecemeal across a chunk edge. (The private-key HEADER
# phrase contains spaces and is the one pattern that can split on a terminator;
# it is a non-secret header string and the final full-text pass still redacts
# the persisted/displayed copy.)
_CRED_CLASS: frozenset[str] = frozenset(
    string.ascii_letters + string.digits + "_-+/=.:@%~" + '"' + "'" + "?&#"
)

# Upper bound on withheld trailing characters. Larger than the longest
# fixed-format credential so a split token is always rejoined before emission;
# bounds latency/memory for a pathologically long unbroken run (only affects a
# single >512-char secret with no delimiter, which no supported provider issues).
_STREAM_HOLDBACK_MAX = 512

# PEM header hold-back: matches an in-progress "BEGIN [type] PRIVATE KEY"
# phrase in the tail of the commit buffer.  When found, we refuse to commit
# at the whitespace boundary so the full multi-word marker stays inside one
# redaction pass (ported from the upstream project).
_PEM_HOLD_RE = re.compile(
    r"BEGIN[\s](?:RSA[\s]?|DSA[\s]?|EC[\s]?|OPENSSH[\s]?)?(?:PRIVATE)?[\s]?$",
    re.IGNORECASE,
)

# JWTs (esp. RS256/ES256 with embedded claims) routinely exceed the 512-char DoS
# floor, so a terminal JWT longer than _STREAM_HOLDBACK_MAX would be bisected by
# the default cap and emitted half-redacted. When the withheld tail *looks like*
# the start of a JWT, we raise the cap to this larger ceiling so the whole token
# is rejoined before emission while still keeping the buffer bounded.
_STREAM_HOLDBACK_JWT_MAX = 4096

# The withheld tail is a partial JWT/JWE when it ends with the `eyJ` base64url
# header prefix optionally followed by up to FOUR `.`-separated base64url segments
# (the final segment may be empty mid-stream). Three segments = a JWS/JWT
# (header.payload.sig); five = a compact JWE (header.key.iv.ciphertext.tag), so the
# `{0,4}` trailing quantifier admits the full JWE shape too — matching the batch
# `_CREDENTIAL_PATTERNS` JWE ceiling — instead of bisecting a >512-char JWE at the
# 512 floor. Anchored to the buffer end (`\Z`).
_PARTIAL_JWT_TAIL_RE = re.compile(r"eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){0,4}\Z")

# Trailing (possibly incomplete) `Authorization: Bearer <token>` anchor at the end
# of the stream buffer. Unlike a bare credential run, this anchor embeds WHITESPACE
# (`Authorization: Bearer `) which is NOT in `_CRED_CLASS`, so the maximal-trailing-
# cred-run holdback in `StreamRedactor.feed` would commit the `Authorization:` /
# `Bearer ` prefix in one chunk and the opaque token in the next — redacting
# neither, since the batch `Authorization:\s*Bearer` pattern only fires when the
# whole anchor is present in a single `redact()` call. We therefore withhold from
# the START of any such trailing anchor so the anchor and its token stay joined
# until a terminator (or stream end) arrives.
#
# `\Z` pins the match to the buffer tail so only a genuinely in-progress anchor is
# held. The `Bearer` word is matched by any of its prefixes (`B`…`Bearer`) so a
# split mid-word (`Authorization: Bear` | `er opaque…`) still holds; a completed
# anchor followed by a token then whitespace no longer matches (`\s+` after the
# token cannot reach `\Z`), so it is committed and redacted whole. Requiring the
# `Bearer` prefix bounds over-holding: ordinary prose like `Authorization: granted`
# fails the match and is released immediately. Case-INSENSITIVE and JSON-aware to
# mirror the batch pattern: HTTP/2 lower-cases header names (`authorization:` /
# `bearer`) and JSON shapes the header as `{"Authorization": "Bearer <tok>"}` (a
# quote before the `:` and before the token), so the anchor tolerates an optional
# quote around `[:=]` and folds the `Authorization`/`Bearer` words — otherwise a
# lowercase or JSON-shaped anchor split across chunks would not be held and its
# token would leak. Opaque OAuth/refresh/SSO Bearer tokens carry no `eyJ` header,
# so without this anchor a >512-char opaque bearer tail would stay on the 512 floor
# and stream its raw tail.
_BEARER_ANCHOR_PARTIAL_RE = re.compile(
    r"""Authorization["']?\s*[:=]\s*["']?"""
    r"(?:Bearer(?:\s+[A-Za-z0-9._~+/=-]*)?|Beare|Bear|Bea|Be|B)?\Z",
    re.IGNORECASE,
)


class StreamRedactor:
    """Rolling-buffer redactor for streamed LLM output.

    Feed raw chunks in order; ``feed`` returns the redacted, safe-to-broadcast
    prefix (possibly empty while a partial credential is buffered). Call
    ``flush`` when the stream/segment ends to redact and return the remainder.
    Adds at most one chunk of latency. A credential is never split across a
    commit boundary because commits only ever end at a non-credential-class
    character, while a credential is a contiguous credential-class run.
    """

    __slots__ = ("_buf", "_redact")

    def __init__(self, redactor: "Callable[[str], str] | None" = None) -> None:
        self._buf = ""
        # Resolve at call time so module-load order is irrelevant.
        self._redact = redactor or redact

    def feed(self, chunk: str) -> str:
        """Accept a chunk; return the redacted prefix that is safe to emit now."""
        if not chunk:
            return ""
        self._buf += chunk
        # Start of the maximal trailing credential-class run.
        i = len(self._buf)
        while i > 0 and self._buf[i - 1] in _CRED_CLASS:
            i -= 1
        # PEM header hold-back (ported from the upstream project): the
        # multi-word phrase "BEGIN RSA PRIVATE KEY" splits on whitespace.  If the
        # tail of the commit window contains an in-progress PEM header prefix,
        # refuse to commit at this boundary.
        if i > 0 and _PEM_HOLD_RE.search(self._buf[max(0, i - 50) : i]):
            i = 0
        # Also withhold from the start of any trailing (possibly incomplete)
        # `Authorization: Bearer <token>` anchor. Its embedded whitespace is not in
        # _CRED_CLASS, so the run scan above would otherwise commit the anchor
        # prefix and the opaque token in separate chunks — leaking the token, since
        # the batch Bearer pattern only fires on the joined anchor.
        anchor = _BEARER_ANCHOR_PARTIAL_RE.search(self._buf)
        if anchor is not None:
            i = min(i, anchor.start())
        # Escalate the holdback cap to the JWT ceiling when the withheld tail is
        # (the start of) a credential that legitimately exceeds the 512-char DoS
        # floor: a partial JWT/JWE (`eyJ…`) OR a trailing `Authorization: Bearer`
        # anchor. Bearer must be included alongside JWT — an opaque OAuth/refresh/
        # SSO Bearer token > 512 chars has no `eyJ` prefix, so keying escalation on
        # `_PARTIAL_JWT_TAIL_RE` alone left its 512-char tail streaming raw. Still
        # bounded: a run with no credential anchor stays on the 512 floor.
        cred_anchored = _PARTIAL_JWT_TAIL_RE.search(self._buf) is not None or anchor is not None
        cap = _STREAM_HOLDBACK_MAX
        if len(self._buf) - i > cap and cred_anchored:
            cap = _STREAM_HOLDBACK_JWT_MAX
        if len(self._buf) - i > cap:
            if cred_anchored:
                # Fail closed: a credential-anchored tail (JWT/JWE/Bearer) has blown
                # past the 4096 ceiling. Bisecting here would emit the token's head
                # raw, so instead redact+emit the safe prefix, append the tag, and
                # DROP the oversized tail. A plain cred-class run with no credential
                # anchor falls through to the bisect below and is committed
                # (bisecting an opaque non-credential run cannot leak a structured
                # secret and preserves the DoS bound with no data loss).
                commit, self._buf = self._buf[:i], ""
                out = self._redact(commit) if commit else ""
                return out + _REDACTED_CREDENTIAL_TAG
            i = len(self._buf) - cap
        if i <= 0:
            return ""  # whole buffer is a (possibly partial) credential run — hold
        commit, self._buf = self._buf[:i], self._buf[i:]
        return self._redact(commit)

    def flush(self) -> str:
        """Redact and return the buffered remainder; clears the buffer."""
        out = self._redact(self._buf) if self._buf else ""
        self._buf = ""
        return out

    def reset(self) -> None:
        """Discard the buffer without emitting (segment abandoned/cleared)."""
        self._buf = ""


def _deny_segment_views(segment: str, emit_self: bool = True) -> tuple[str, ...]:
    """The views of ONE shell segment that the deny tiers are matched against.

    *segment* arrives with its ORIGINAL CASE, and every view returned is
    lowercased.  Case matters for exactly one step: bash's Unicode escape widths
    are case-sensitive (``\\u`` up to 4 hex digits, ``\\U`` up to 8), so decoding
    after a ``lower()`` would read ``$'\\u0072f'`` -- which bash passes as ``rf`` --
    as a single 5-digit code point and miss the rule.  The decode therefore runs
    FIRST, on the text as written, and the lowercasing happens after.

    The first element is always the raw text (lowercased) -- matched exactly as it
    was before this helper existed -- so nothing that was denied can stop being
    denied.  Quote/escape-NORMALIZED re-joins are APPENDED when they differ.

    *emit_self* False walks NESTED PAYLOADS ONLY, emitting no view for *segment*
    itself.  That is how the whole command is inspected without joining across its
    separators: ``_split_segments`` is deliberately quote-unaware, so a newline
    inside a quoted payload (``bash -c 'r\\<newline>m -rf /'``) severs the command
    into pieces before the payload can be extracted from it -- while re-joining the
    whole command would fabricate a command that never ran.  Walking it for
    payloads without emitting its own re-join gets the first without the second.

    ── Why the extra view is needed ──
    Both deny tiers match TEXT, and a shell removes quoting, escaping and
    empty-string splices and collapses whitespace runs before the program ever
    sees its argv.  So every rule authored as a command SHAPE (``rm -rf /``,
    ``dd if=``, ``chmod 777``) was defeated by re-spelling any one token:
    ``rm -rf "/"``, ``"rm" -rf /``, ``'rm' -rf /``, ``rm "-rf" /``,
    ``r''m -rf /`` and ``rm  -rf /`` all run the identical command and none of
    them CONTAINS the pattern's own text.  Of the ~140 built-in rules only the
    six self-protection rules and git-publish had an argv-structural floor
    closing this (see ``_SELF_PROTECTION_FLOOR_PATTERNS``); every other rule was
    spelling-dependent.

    ── Why the tokenizer is ``_shell_tokens`` and not ``normalize_shell_command``
    Both share one tokenizer, but the deny view deliberately stops BEFORE
    ``~``/``$HOME`` expansion, for two reasons.  Expansion is
    platform-dependent, so it would make the view decide differently per host:
    it DELETES the literal ``~`` that ``rm -rf ~.*`` is authored to match, and
    on Windows it yields a drive path (``c:\\users\\…``) that no POSIX-anchored
    rule matches — so ``rm -rf "~"`` would be caught on Linux by the sibling
    ``rm -rf /.*`` rule and missed on Windows.  And a denied view becomes the
    security event log's ``operation`` field, so expanding here would write the
    operator's real home path into the audit trail on every such denial.  Path
    IDENTITY (dot segments, ``..``, ``$HOME`` versus the resolved home) is
    already decided by ``is_sensitive_path`` against the
    sensitive-path keystone, which is the layer that resolves rather than
    matches; this view answers only the narrower question of what the shell
    hands over as argv.

    ── Why this is a per-SEGMENT view, never a whole-command one ──
    Re-joining tokens with single spaces erases the separators a shell uses to
    END a command, so normalizing the whole input would FABRICATE a command that
    was never run: ``echo rm`` + newline + ``-rf /`` is two commands, and a
    whole-input re-join reads as ``echo rm -rf /``.  The heredoc frames pinned by
    ``TestStdinProgramTextScoping.test_benign_neighbour_no_longer_reads_as_a_mint``
    are the concrete case.  Segments come from ``_split_segments``, so no boundary
    is ever crossed — including inside a nested payload, which is split the same
    way before being viewed.

    ── Nested shell payloads ──
    A shell's ``-c`` argument is a COMMAND, and ``shlex`` strips only the OUTER
    quoting level, so ``bash -c 'dd "if=/dev/zero" of=/dev/sda'`` re-joins with
    its inner quotes intact and the ``dd if=`` rule still does not match (found
    by the GPT 5.6 review lane on this change).  Each literal payload is
    therefore walked and viewed in its own right, reusing
    :func:`_nested_shell_payloads` — the extractor the self-protection floor
    already uses, so the ``-c`` / ``eval`` / ``env -S`` / herestring /
    ``$SHELL -c`` spellings and the ``bash -c -- <script>`` form are recognized
    here by construction rather than re-enumerated.  Only LITERAL payloads exist
    to walk: ``eval "$CMD"`` carries no visible script and stays the raw tier's
    job.

    The walk takes NO numeric depth cap, for the reason
    :func:`_self_token_frames` records: whatever the number, one more wrapper
    defeats it.  It terminates structurally instead — a payload is carried inside
    ONE token of its parent, so it is strictly shorter than the parent's source
    text, and a chain of strictly shorter strings is finite.

    ── Fail-closed, and it never raises ──
    This only ever ADDS views.  ``_shell_tokens`` already degrades to whitespace
    splitting with quote stripping when ``shlex`` rejects the input (so even an
    unbalanced-quote segment still normalizes), and every window is built inside a
    guard: this runs in the permission gate, where an exception is a crash rather
    than a security decision, so a failure drops that window and leaves the raw
    view standing.  A failure can therefore lose the EXTRA match but never the raw
    one, so it cannot turn a denied command into an allowed one.

    ── Residual ──
    Three shapes stay outside every view.  A token split by BOTH quoting and a
    separator-shaped glue construct (``"rm"$(echo ' ')-rf /``) is in none of them:
    the raw text is not contiguous and the glue lands on its own segment — the
    whole-string raw pass covers the glue-ONLY spelling (``git$(echo ' ')push``),
    and closing the combination needs a normalizer that models substitution,
    which a re-join is not.  A variable spelling of a path operand
    (``rm -rf $HOME``) is by construction not expanded here, per the note above.
    And a quoted WHITESPACE-ONLY word (``rm -rf " " /home/x``) still renders an
    extra separator.  Adding a render without it would be additive like the one
    above and so could not lose a denial, but it is not the same claim: an empty
    element carries no characters, so a view without it is still the argv the
    shell hands over; a whitespace-only element is a real operand naming a file
    that can exist, so a view without it is an argv ONE OPERAND SHORT of the one
    that runs.  Widening the render to elements that do carry characters changes
    what a view is permitted to assert -- and ``is_denied``'s exception machinery
    (present, and ``_DENY_EXCEPTIONS`` empty today) is matched against views, so
    the direction it would open is ALLOW, not deny.  Recognizing this shape wants
    rules matched against argv STRUCTURE rather than against a rendered line,
    which is what ``_SELF_PROTECTION_FLOOR_PATTERNS`` already does for the six
    self-protection rules — and is why those are not fooled by either shape.
    """
    views: list[str] = [segment.lower()] if emit_self else []
    seen_views: set[str] = set(views)
    # Decode the case-sensitive escapes BEFORE folding case (see the docstring),
    # then work entirely in lowercase from here on -- the tiers compare lowercased
    # text, and the payload extractor recognizes lowercase program names.  Guarded
    # like the walk below: this is the permission gate, so a decoder that raises
    # must cost the extra view, never the decision.
    try:
        start = _decode_shell_quoted_literals(segment).lower()
    except Exception:
        logger.debug("deny-view quote decode failed; raw view only", exc_info=True)
        start = segment.lower()
    seen_sources: set[str] = {start}
    # (source, parent_len, is_root, allow_join): a payload lives inside one token of
    # its parent, so it is strictly shorter than the parent's source text — which is
    # what bounds this walk without a numeric cap.  ``is_root`` marks the source the
    # caller handed in, whose own re-join ``emit_self=False`` suppresses.
    # ``allow_join`` carries the same discipline ``_shell_payload_walk`` applies: a
    # frame produced BY the ``eval`` argument join must not join again, or the two
    # walks each build a chain of shrinking suffixes and this one — which re-lexes
    # and re-splits every frame — dominates the cost (measured on ``"eval " * 640``:
    # 12.0 s of a 14.2 s total here, against 0.04 s before the join existed).
    pending: list[tuple[str, int, bool, bool]] = [(start, len(start) + 1, True, True)]
    while pending:
        source, parent_len, is_root, allow_join = pending.pop()
        try:
            tokens = _shell_tokens(source)
            if not tokens:
                continue
            # No expansion happens above, so an already-lowercased source stays
            # lowercased through the re-join and needs no second fold.
            #
            # The empty-elided re-join is a THIRD view, ADDED beside the plain one
            # rather than replacing it -- this helper only ever adds views, and
            # substituting here broke that invariant in a measurable way.  An
            # empty-quoted word (``""``, ``''``, ``$''``, or any concatenation of
            # them) is a real argv element the shell does hand over, so
            # ``_shell_tokens`` is right to keep it and the payload walk below
            # still sees argv as it was.  What it cannot survive is the RENDER: a
            # single-space join turns a zero-width element into a spurious extra
            # separator, and every rule authored as a command shape with single
            # separators (``rm -rf /``, ``dd if=``) then stops matching its own
            # target -- ``rm -rf "" /home/x`` rendered as ``rm -rf  /home/x``.
            # The element contributes no text to the shape and cannot name a file
            # or carry a flag, so a view without it renders what the command does
            # rather than fabricating something it does not.
            #
            # Keeping the plain join is not defensive tidiness.  A rule that
            # REQUIRES an intervening token (``rm -rf .* ./data``) matched the
            # double-spaced view and matches neither the elided one nor the
            # command's canonical spelling, so dropping it removed a denial that
            # existed before: ``r""m -rf "" ./data`` was refused and became
            # allowed (found by the GPT 5.6 review lane, reproduced against the
            # merge-base).  Emitting both means a rule authored against either
            # whitespace shape still fires, which is the only reading that cannot
            # lose a denial.  Rules whose own pattern already tolerated the extra
            # separator (``chmod.*/etc/.*``) were denying via the plain view all
            # along, which is why the escape was pattern-dependent rather than
            # uniform, and why this belongs here and not in individual rules.
            view = " ".join(tokens)
            candidates = [view]
            elided = " ".join(token for token in tokens if token)
            if elided and elided != view:
                candidates.append(elided)
            for candidate in candidates:
                if not (is_root and not emit_self) and candidate not in seen_views:
                    seen_views.add(candidate)
                    views.append(candidate)
            joined_here: set[str] = set()
            payloads = _nested_shell_payloads(
                tokens, allow_join=allow_join, joined_out=joined_here
            )
            programs = _argv_programs(tokens) if payloads else []
            # Both values below read ONLY ``tokens``, which is fixed for this
            # whole walk, so they are charged ONCE here instead of once per
            # payload.  Asking per payload is what made this loop quadratic in
            # payload count (#8595 -- 18k payloads, ~293s): the exemption's
            # command-level guards sweep the whole argv, and recovering a
            # payload's positions with ``enumerate`` sweeps it again, so N
            # payloads cost N x len(tokens).  Neither hoist can change a verdict:
            # same inputs, same answers, computed once rather than N times.  Both
            # are skipped when there are no payloads so an ordinary command --
            # the common case -- pays nothing new.
            command_disqualified = (
                _data_consumer_command_disqualified(tokens) if payloads else False
            )
            token_positions: dict[str, list[int]] = {}
            if payloads:
                for _pos, _tok in enumerate(tokens):
                    token_positions.setdefault(_tok, []).append(_pos)
            for payload in payloads:
                if len(payload) >= parent_len:
                    continue
                # ``echo bash -c '<script>'`` PRINTS the script, so descending into
                # it refuses a command that runs nothing (raised as an advisory by
                # the GPT 5.6 lane).  The repo's own exemption decides this, rather
                # than a "launcher must be in command position" rule: the launcher
                # is NOT in command position in ``sudo bash -c …``,
                # ``timeout 5 bash -c …``, ``nohup``, ``ssh host``, ``xargs`` or
                # ``env FOO=1 bash -c …``, all of which really do execute, so that
                # rule would trade this false positive for six bypasses.
                # ``_data_consumer_exempt`` is a DENYLIST of consumers with the
                # executing cases already carved out (a piped evaluator, a
                # substitution in program position, an ``awk``/``sed`` script that
                # can execute), so a program it does not know stays walked.
                #
                # A payload is not necessarily a TOKEN.  ``_nested_shell_payloads``
                # also returns SYNTHESIZED text — a ``sed`` ``e``-flag replacement,
                # the tail of a glued herestring (``bash<<<'<script>'``), a glued
                # ``env -S`` argument, an ``alias`` assignment — which is a
                # substring or a re-join, not an element of ``tokens``.  Recovering
                # a position with ``list.index`` therefore raised ``ValueError`` and
                # propagated out of the permission gate on legitimate input
                # (``sed 's/x/y/e' notes.txt``): found independently as BLOCKING by
                # the GPT 5.6 and Opus 4.8 lanes.
                #
                # The exemption is decided per OCCURRENCE and fails closed: it is
                # applied only when the payload appears as a token AND every
                # occurrence sits in the argv of a data consumer.  A payload with no
                # token position cannot be proven inert, so it is DESCENDED into —
                # over-blocking, which is the safe direction here.  Deciding from a
                # single recovered index would not be sound: a short synthesized
                # payload can also be a coincidental substring of an unrelated
                # token, and one wrong position could wrongly exempt a payload that
                # really executes.
                occurrences = token_positions.get(payload, [])
                if occurrences and all(
                    _data_consumer_exempt(
                        i,
                        payload,
                        programs,
                        tokens,
                        command_disqualified=command_disqualified,
                    )
                    for i in occurrences
                ):
                    continue
                # A payload is a command LINE, so it gets the same PRE-LEX treatment
                # the top level got: the shell that runs it folds ITS continuations
                # before lexing, so fold before splitting or the split severs them.
                # ``bash -c 'r\<newline>m -rf /'`` otherwise yields the pieces ``r``
                # and ``m -rf /``, and no view holds the command that runs (BLOCKING
                # from the GPT 5.6 lane).  A view must also not be joined across one
                # of the payload's own separators.  Only the PIECES are recorded as
                # walked — recording the payload itself would filter out the single
                # piece that equals it.
                child_may_join = payload not in joined_here
                for piece in _split_segments(_fold_line_continuations(payload)):
                    piece = piece.strip()
                    if piece and piece not in seen_sources:
                        seen_sources.add(piece)
                        pending.append((piece, len(source), False, child_may_join))
        except Exception:
            # This runs INSIDE the permission gate, where an exception is a crash
            # rather than a security decision — the hazard ``_normalize_search_path``
            # documents for the same reason.  Losing one view only costs the EXTRA
            # match; the raw view is already in ``views`` and the raw tier decides
            # exactly as it did before this helper existed, so a failure here can
            # never turn a denied command into an allowed one.
            logger.debug("deny-view construction failed for a window", exc_info=True)
            continue
    return tuple(views)


# An interpreter binds the halves to its OWN variables
# (``n = "<name>"; v = "<verb>"; run([n, v])``) and then uses the names.  Inlining those
# bindings is the interpreter-side twin of the shell assignment resolution, and it is what
# keeps the argv pattern TIGHT: the alternative -- admitting ``;`` into the separator class
# so the two quoted strings may sit in different statements -- would also match
# ``print('<name>'); log('<verb>')``, which mints nothing.
_INTERP_BINDING_RE = re.compile(r"\b([a-z_]\w*)\s*=\s*('[^']*'|\"[^\"]*\")")
_INTERP_IDENT_RE = re.compile(r"\b[a-z_]\w*\b")


# ``"<name> %s" % "<verb>"`` -- printf-style formatting is the same evasion as adjacent
# literal concatenation, one operator along.  The tuple spelling
# (``"%s %s" % ("<name>", "<verb>")``) is covered by consuming the arguments in order.
_PERCENT_FORMAT_RE = re.compile(
    r"""(['"])([^'"]*)\1\s*%\s*\(?\s*((?:['"][^'"]*['"]\s*,?\s*)+)\)?"""
)
_QUOTED_FRAGMENT_RE = re.compile(r"""['"]([^'"]*)['"]""")
_FORMAT_SPEC_RE = re.compile(r"%[-+ #0]*\d*(?:\.\d+)?[sridfge]")


def _collapse_percent_format(text: str) -> str:
    """Apply ``%`` formatting to a quoted template whose arguments are literals.

    Only literal arguments are substituted -- the point is to see the string the
    interpreter will hand to a sink, exactly as the concatenation collapse does.
    """

    def _apply(match: "re.Match[str]") -> str:
        quote, template, arg_blob = match.group(1), match.group(2), match.group(3)
        args = _QUOTED_FRAGMENT_RE.findall(arg_blob)
        if not args:
            return match.group(0)
        remaining = list(args)

        def _one(_spec: "re.Match[str]") -> str:
            return remaining.pop(0) if remaining else _spec.group(0)

        return f"{quote}{_FORMAT_SPEC_RE.sub(_one, template)}{quote}"

    return _PERCENT_FORMAT_RE.sub(_apply, text)


def _inline_interpreter_bindings(text: str) -> str:
    """Replace identifiers bound to a quoted literal in *text* with that literal."""
    bindings: dict[str, str] = {}
    for match in _INTERP_BINDING_RE.finditer(text):
        bindings.setdefault(match.group(1), match.group(2))
    if not bindings:
        return text
    return _INTERP_IDENT_RE.sub(lambda m: bindings.get(m.group(0), m.group(0)), text)


def is_denied(
    tool_name: str,
    extra_patterns: list[str] | None = None,
    *,
    denied_regexes: list[str] | None = None,
    reason_notes: dict[str, str] | None = None,
) -> str | None:
    """Check tool name against the built-in/effective + extra deny patterns.

    Returns denial reason string, or None if allowed.

    ── Two tiers ──
    * Regex tier (``denied_regexes``): the effective enabled built-in rule
      regexes plus user-added regexes (output of ``compute_effective_denied``),
      matched via ``re.search`` (``re.IGNORECASE``).  When ``None``, FAILS
      CLOSED to all built-ins enabled.
    * Glob tier (``extra_patterns``): legacy ``auto_deny_tools`` + companion
      overlay globs, matched via ``fnmatch`` exactly as before.

    ── Two-pass evaluation ──
    Pass 1 (whole-string): every deny pattern is matched against the
    full input.  If a pattern matches and **no exception pattern also
    matches the full input**, the input is denied immediately.  This
    closes evasion vectors where the deny string spans a separator
    boundary that per-segment splitting would erase, e.g.
    ``git$(echo ' ')push origin main`` (which bash evaluates to
    ``git push origin main``): the whole string contains both ``git`` and
    ``push`` so the broad ``*git*push*`` glob matches, and there is no
    matching exception, so the command is denied at this stage even
    though splitting on ``$(`` / ``)`` would otherwise produce no
    segment containing both substrings.

    Pass 2 (per-segment) only runs if pass 1 found a deny match **and**
    the full input also matched at least one exception for that pattern.
    The input is split on shell command separators (``;``, ``&&``,
    ``||``, ``|``, newlines) and command-substitution boundaries
    (``$(``, ``)``, backticks) into segments, and each segment is
    re-evaluated independently.  This preserves the chaining-bypass
    protection (any embedded real
    publish lives in its own segment and matches the deny pattern in its
    own right) while allowing the legitimate stash-in-pipeline case
    that the prior whole-string design over-blocked.

    Edge cases & limitations:
      - Pass-1 deny is conservative: anything matching a deny glob with
        no exception is blocked, even if the input is structurally
        contorted.
      - Pass-2 splitting is purely textual; quoted strings and escaped
        separators are split anyway (over-blocking is the safer
        direction).
      - Each pass-2 segment is matched in TWO views: the raw text, then a
        quote/escape-normalized re-join of that segment
        (``_deny_segment_views``), so a rule authored as a command shape is
        not defeated by re-quoting a token (``rm -rf "/"``), splicing one
        (``r''m -rf /``) or padding the whitespace.  Strictly additive, and
        never applied across a separator — see that helper.
      - Heredoc bodies, ``eval``, ``bash -c``, etc., are not parsed
        specially.  If those become evasion vectors in practice, add
        explicit deny patterns for them.

    Audit:
      - Every denial path emits a ``deny_event`` SEL event via
        ``_emit_deny_event``.
      - Every granted exception emits a ``deny_exception`` SEL event via
        ``_emit_deny_exception_event`` (fail-closed: if SEL logging
        fails the exception is not granted).

    Args:
        tool_name: The full command line / tool invocation to evaluate.
        extra_patterns: Optional fnmatch glob patterns (glob tier — legacy
            ``auto_deny_tools`` + companion overlay).
        denied_regexes: The effective enabled rule regexes (regex tier).  When
            ``None``, fails closed to all built-in rules enabled.
        reason_notes: Optional ``{pattern: operator note}`` map.  When the pattern
            that matched has a note, the note is appended to the refusal on its
            OWN line.  Presentation only — it never affects whether something is
            denied.

    Returns:
        Denial reason string (mentioning the matched pattern), or
        ``None`` if the input is allowed.
    """
    lower = tool_name.lower()

    def _reason(
        matched: str,
        note_override: str = "",
        *,
        rule: str = "",
        component: str = "",
    ) -> str:
        """Refusal text for *matched* -- see :func:`_deny_reason`, the shared producer.

        *note_override* lets the argv-structural floor say why a pattern the input
        does not literally match was still the rule that fired (see
        ``_SELF_PROTECTION_FLOOR_NOTES``).

        *rule* and *component* add the diagnostic line, and only the STRUCTURAL
        floors below pass them. That is the whole distinction: a pattern-tier
        denial's first line already names the pattern that matched the input, so a
        diagnostic would repeat it on every ordinary refusal, while a floor denial
        reports a pattern the input provably cannot match and is the refusal an
        agent cannot diagnose at all. The span is the whole subject because a floor
        decides on the argv's SHAPE rather than at an offset.
        """
        diagnostic = (
            refusal_diagnostic(rule, component, tool_name) if rule and component else None
        )
        return _deny_reason(
            matched, reason_notes, note_override=note_override, diagnostic=diagnostic
        )

    glob_patterns = list(extra_patterns or [])
    if denied_regexes is None:
        regex_patterns = compute_effective_denied(BUILTIN_DENIED_RULES, (), False, (), ())
    else:
        regex_patterns = list(denied_regexes)
    # Capture which git-publish rules are still ENABLED *before* the strip below
    # removes their patterns from the regex tier. Computing this afterwards would
    # always yield the empty set and the floor would never fire — a silent, total
    # loss of push protection.
    git_publish_enabled = {p for p in regex_patterns if p in _GIT_PUBLISH_RULE_PATTERNS}
    # Never feed git-publish rule patterns to Python ``re`` — they are ReDoS-prone
    # under backtracking and are already enforced by the ``_is_git_publish`` floor
    # below (see ``_GIT_PUBLISH_RULE_PATTERNS``).
    regex_patterns = [p for p in regex_patterns if p not in _GIT_PUBLISH_RULE_PATTERNS]
    # The two self-protection rules get an ADDITIONAL argv-structural floor
    # below, for the reason documented on ``_SELF_PROTECTION_FLOOR_PATTERNS``:
    # only a tokenized view can tell ``kirocrew "token"`` from
    # ``kirocrew-wt-x/test_token_auth.py``.  The floor is a UNION with the regex
    # tier, never a replacement -- the patterns deliberately stay in
    # ``regex_patterns``.  Two independent reasons:
    #   1. The regex still matches raw text, so a payload the tokenizer cannot
    #      see into (``bash -c "kirocrew token"``, ``eval "$CMD"``) is caught.
    #   2. The tokenizer can fail (unbalanced quotes, or a platform bug like the
    #      one fixed in ``normalize_shell_command`` above), and a floor that
    #      REPLACED the regex would then fail OPEN.
    # A rule the operator has DISABLED must stay disabled, so the floor runs
    # only for patterns still present in the effective set.
    floor_enabled = {p for p in regex_patterns if p in _SELF_PROTECTION_FLOOR_PATTERNS}
    # An interpreter CONCATENATES adjacent string literals, so ``'p'+'kill -f <name>'``
    # is one command by the time it reaches the sink.  The two interpreter rules are
    # therefore also matched against a copy with those joins collapsed.  Scoped to those
    # two patterns on purpose: collapsing text for all the other rules would change
    # inputs they were never measured against.
    joined = _inline_interpreter_bindings(
        _collapse_percent_format(_LITERAL_CONCAT_RE.sub("", lower))
    )
    if joined != lower:
        for interpreter_pattern in regex_patterns:
            if interpreter_pattern not in _INTERPRETER_RULE_PATTERNS:
                continue
            try:
                if re.search(interpreter_pattern, joined, re.IGNORECASE):
                    _emit_deny_event(tool_name, interpreter_pattern, lower)
                    return _reason(interpreter_pattern)
            except re.error:  # pragma: no cover - patterns are validated at load
                continue
    # Ordered (pattern, is_regex) pairs so the two passes share one code path;
    # regex tier first (the effective rule set), then the glob tier.
    all_patterns: list[tuple[str, bool]] = [(p, True) for p in regex_patterns] + [
        (p, False) for p in glob_patterns
    ]

    # ── Git publish (verb-anchored, not a glob) ──
    # Checked on the whole string first so command-substitution glue-evasion
    # (e.g. ``git$(echo ' ')push``) is caught even though splitting on ``$(``
    # / ``)`` would otherwise scatter the ``git``/``push`` tokens across
    # segments.  ``_is_git_publish`` is verb-anchored, so a commit message or
    # branch name merely containing "push" does not match.
    #
    # A push to a PROTECTED branch (or a bare/ambiguous push) is denied here;
    # an explicit FEATURE-branch push is allowed to fall through to the normal
    # glob passes (so any other deny pattern in a compound command still
    # applies), and we record the allow INTENT now — the ``push_allowed`` audit
    # is emitted only at a SUCCESS return path below, so the SEL trail reflects
    # the FINAL outcome (never an allow for a command ultimately denied).
    #
    # Evaluated over the whole string AND the source of every nested shell payload
    # (``_shell_payload_sources``), because this floor is the SOLE enforcement for
    # pushes -- every git-publish rule is stripped from the regex tier just above.
    # A top-level-only text match therefore meant one wrapper was a complete
    # bypass: ``bash -c 'git push origin main'`` and ``eval '<push>'`` reached no
    # check at all, while the self-protection floor beside it was already immune
    # because it re-tokenizes payloads. Same walk, same depth guarantee, so a
    # wrapper cannot buy anything here either.
    push_allow_pending = False
    try:
        payload_sources = _shell_payload_sources(lower)
    except Exception:
        # This runs inside the PreToolUse gate, which must return a DECISION and
        # never raise. Degrade to the top-level reading -- precisely what this
        # floor checked before it learned to descend -- so a broken walk costs
        # the nested coverage and nothing else. Failing closed here instead
        # would refuse ordinary commands on any walk hiccup.
        payload_sources = [lower]
    # Tags are collected across EVERY publish source, not just the top-level
    # string, and gated once afterwards. Reading only ``lower`` made one wrapper a
    # complete bypass of the sole enforcement pushes have; gating once at the end
    # keeps the per-rule opt-out semantics exactly as written -- a rule an operator
    # disabled stays disabled at whatever depth it fires.
    publish_sources = [source for source in payload_sources if _is_git_publish(source)]
    if publish_sources:
        floor_tags: frozenset[str] = frozenset()
        for publish_source in publish_sources:
            floor_tags |= _git_publish_floor_tags(publish_source)
        # The ungated tag denies regardless of opt-out: it marks a command whose
        # target could not be verified at all, which is what keeps the gated
        # rules below non-bypassable. Report it under the brace-expansion rule,
        # whose coverage this branch is, so the refusal still names a catalog row.
        if _GIT_PUBLISH_UNGATED in floor_tags:
            ungated_pattern = _GIT_PUBLISH_FLOOR_BY_ID.get(
                "git-publish-push-brace-expansion-refspec", _GIT_PUBLISH_DENY_LABEL
            )
            _emit_deny_event(tool_name, ungated_pattern, lower)
            return _reason(
                ungated_pattern,
                "Matched structurally on the command's argv, not by the pattern text above: "
                "shell substitution or expansion fuses text into the push target, so the "
                "destination branch cannot be determined before the push runs.",
                rule="git-publish-target-unverifiable",
                component="git-publish-floor",
            )
        for tag in sorted(floor_tags):
            gated_pattern = _GIT_PUBLISH_FLOOR_BY_ID.get(tag)
            if gated_pattern is None:
                # A tag naming no catalog row is a MAINTENANCE error, not a policy
                # choice, and the two must not share a branch: skipping here would
                # turn a renamed rule id or tag literal into a silent allow of a
                # protected-branch push, with the failure direction under
                # refactoring being "publish". Deny instead, under the ungated
                # sentinel's row, so the mistake is loud and fail-closed. The
                # structural guard in test_push_branch_gate.py still catches it at
                # build time; this is what happens if that guard is ever removed.
                fallback = _GIT_PUBLISH_FLOOR_BY_ID.get(
                    "git-publish-push-brace-expansion-refspec", _GIT_PUBLISH_DENY_LABEL
                )
                logger.error(
                    "git-publish floor tag %r resolves to no catalog rule; denying "
                    "fail-closed. This is a code defect: the tag and the rule id "
                    "have drifted apart.",
                    tag,
                )
                _emit_deny_event(tool_name, fallback, lower)
                return _reason(
                    fallback,
                    "A protected-branch push shape was recognised but its rule "
                    "could not be resolved, so it is refused rather than allowed.",
                    rule="git-publish-tag-unresolved",
                    component="git-publish-floor",
                )
            if gated_pattern not in git_publish_enabled:
                continue
            # SEL keeps the PATTERN (that is what maps an event to a catalog row),
            # while the human-facing refusal leads with the rule ID: the chip in
            # the dashboard's RecoveryCard is filled verbatim from this first line,
            # and a ~70-char raw regex there is unreadable on the single most
            # frequent denial an agent user hits. The id is both short and the
            # actual toggle identity, so it tells the operator exactly which row to
            # switch off; the regex stays available on the note line below, which
            # the chip parser deliberately ignores.
            _emit_deny_event(tool_name, gated_pattern, lower)
            note = _GIT_PUBLISH_FLOOR_NOTES.get(tag, "")
            return _reason(
                tag,
                f"{note} (rule pattern: {gated_pattern})".strip(),
                rule=tag,
                component="git-publish-floor",
            )
        push_allow_pending = True

    # ── Self-protection floor (argv-structural, not a glob) ──
    # Runs before the pattern passes and on the WHOLE string, for the same reason
    # the git-publish floor does: the evasions live in shell syntax that textual
    # splitting scatters or mis-reads.  Each predicate here is checked only if its
    # catalog row is still in the effective set, so an operator-disabled rule
    # stays disabled.
    for rule_id, predicate in (
        ("credential-exfil-kirocrew-token", _is_credential_mint),
        ("self-protection-kill", _is_self_kill),
        ("self-protection-dev-mode-out-of-root-confirm", _is_dev_mode_out_of_root_confirm),
    ):
        pattern = _SELF_PROTECTION_FLOOR_BY_ID.get(rule_id)
        if pattern is None or pattern not in floor_enabled:
            continue
        if predicate(lower):
            # Report the rule's own pattern, exactly as the regex tier does, so
            # the denial reason and the SEL event still map back to the rule id —
            # plus a second line saying the match was STRUCTURAL, because a floor
            # hit routinely occurs on input that pattern cannot match and the
            # bare identifier reads as a false explanation.
            _emit_deny_event(tool_name, pattern, lower)
            return _reason(
                pattern,
                _SELF_PROTECTION_FLOOR_NOTES.get(rule_id, ""),
                rule=rule_id,
                component="argv-floor",
            )
    # The self-management SUBCOMMAND floors have no catalog row (their
    # product-name-anywhere regex rows were deleted, see
    # ``_SELF_PROTECTION_UNGATED_FLOOR_IDS``), so there is no pattern to gate on
    # and nothing to report but the id: they run unconditionally, like the
    # git-publish anti-obfuscation branches.  Gating them on a row lookup was the
    # trap this replaces -- ``.get(rule_id)`` returning None fell through to
    # ``continue``, so deleting the row silently disabled the floor.
    for rule_id, predicate in (
        ("self-protection-restart", _is_self_restart),
        ("self-protection-update", _is_self_update),
        ("self-protection-gateway-restart", _is_self_gateway_restart),
        ("self-protection-cloud", _is_self_cloud_destructive),
    ):
        if predicate(lower):
            _emit_deny_event(tool_name, rule_id, lower)
            return _reason(
                rule_id,
                _SELF_PROTECTION_FLOOR_NOTES.get(rule_id, ""),
                rule=rule_id,
                component="argv-floor",
            )

    # ── Pass 1: whole-string deny ──
    # If any pattern matches the full input AND no exception matches the
    # full input, deny outright.  A whole-string match that IS covered by an
    # exception falls through to the per-segment Pass 2 carve-out re-check.
    #
    # The regex tier matches the FULL, untruncated string via ``_DenyMatcher``
    # (linear-time, no length bound — see the ReDoS-mitigation notes above), so
    # a destructive needle at any offset within a single un-separated segment is
    # caught here.  ``_is_git_publish`` / the always-on floors also run on the
    # full string before this point.
    for pattern, is_regex in all_patterns:
        if _deny_pattern_matches(pattern, lower, is_regex):
            exceptions = _DENY_EXCEPTIONS.get(pattern, [])
            whole_string_exception_match = (
                exceptions
                and _exception_eligible(lower)
                and any(fnmatch.fnmatch(lower, e.lower()) for e in exceptions)
            )
            if not whole_string_exception_match:
                _emit_deny_event(tool_name, pattern, lower)
                return _reason(pattern)

    # ── Pass 2: per-segment (re-)evaluation ──
    # Split into segments and check each.  This runs UNCONDITIONALLY: besides
    # the exception-carve-out re-check, splitting isolates an embedded real
    # publish/destructive command (e.g. after ``;`` / ``&&`` / inside
    # ``$(...)``) into its own segment so it matches the deny pattern in its own
    # right (chaining-bypass protection).  Segments that match a deny pattern
    # AND an exception are allowed with a SEL audit event.
    #
    # Each segment is evaluated in every view ``_deny_segment_views`` returns:
    # the RAW text first (identical to what this pass matched before that helper
    # existed), then a quote/escape-normalized re-join of the same segment when
    # it differs.  The second view is what makes a rule authored as a command
    # shape hold under re-spelling — ``rm -rf "/"`` and ``"rm" -rf /`` reach the
    # ``rm -rf /`` rule as the one command they both are.  It is strictly
    # additive: see that helper for why it is per-segment and why a
    # normalization failure cannot widen what is allowed.
    # Segments are split from the ORIGINAL-case input, not from ``lower``, so
    # ``_deny_segment_views`` can decode bash's case-sensitive Unicode escape
    # widths before folding case.  The split is unaffected: ``_split_segments``
    # cuts on ``;`` ``&&`` ``||`` ``|`` newlines and substitution boundaries, none
    # of which any case mapping produces, so splitting-then-lowercasing and
    # lowercasing-then-splitting give the same pieces.
    #
    # Line continuations are folded FIRST, because the split cuts on the newline
    # they contain: without this, ``"r\<newline>m" -rf /`` is severed into two
    # segments and neither contains the command bash actually runs.  The fold is
    # quote-aware (see ``_fold_line_continuations``) -- pass 1 above still matches
    # the completely unfolded text, so this only ever adds reach.
    # The WHOLE command is walked for nested payloads first, with its own re-join
    # suppressed.  ``_split_segments`` is deliberately quote-unaware, so a newline
    # inside a quoted payload severs the command before the payload can be
    # extracted from it -- ``bash -c 'r\<newline>m -rf /'`` arrives as the pieces
    # ``bash -c 'r\`` and ``m -rf /'`` and the ``-c`` script is never seen (BLOCKING
    # from the GPT 5.6 lane).  Emitting no view for the command itself is what keeps
    # this from fabricating one across its separators.
    folded = _fold_line_continuations(tool_name)
    segments = [seg.strip() for seg in _split_segments(folded)]
    segments = [seg for seg in segments if seg]
    work: list[tuple[str, tuple[str, ...]]] = []
    # The whole-command payload walk is only needed when the split actually SPLIT
    # something.  With a single segment the whole command IS that segment, so
    # walking it twice doubles the payload scan -- which is quadratic in token
    # count inside ``_nested_shell_payloads`` -- for no view the segment walk does
    # not already produce.  Measured: skipping the duplicate halves the cost on a
    # command padded with thousands of interpreter tokens (raised as a stall risk by
    # the GPT 5.6 lane).
    if len(segments) != 1 or segments[0] != folded.strip():
        work.append(("", _deny_segment_views(tool_name, False)))
    for seg_raw in segments:
        work.append((seg_raw.lower(), _deny_segment_views(seg_raw)))
    for seg_lower, segment_views in work:
        for view in segment_views:
            for pattern, is_regex in all_patterns:
                if _deny_pattern_matches(pattern, view, is_regex):
                    exceptions = _DENY_EXCEPTIONS.get(pattern, [])
                    if (
                        exceptions
                        and _exception_eligible(view)
                        and any(fnmatch.fnmatch(view, e.lower()) for e in exceptions)
                    ):
                        if not _emit_deny_exception_event(tool_name, pattern):
                            _emit_deny_event(tool_name, pattern, view, raw_segment=seg_lower)
                            return _reason(pattern)
                        # Exception granted for this pattern on this segment;
                        # continue to evaluate any remaining patterns against
                        # the same segment (a different pattern without an
                        # exception must still cause a deny).
                        continue
                    _emit_deny_event(tool_name, pattern, view, raw_segment=seg_lower)
                    return _reason(pattern)
    # All windows cleared the deny passes — the input is allowed.  If it was a
    # feature-branch push, emit the deferred allow audit now (final outcome).
    #
    # The RAW input is audited, never ``lower``.  ``lower`` exists for MATCHING;
    # nothing matched on an allow, so the case fold buys the record nothing and
    # costs it two things.  Faithfulness: branch names and remote URLs are
    # case-sensitive, so folding records a push to ``Feature-ABC`` as a push to
    # ``feature-abc``.  And redaction: the credential scrubber inside
    # ``redact_and_truncate`` matches an AWS key ID case-SENSITIVELY on purpose
    # (widening it would false-positive on ordinary prose — ``asia`` is a word —
    # across every egress surface; see ``credential_patterns``), so a key handed
    # in already case-folded slips past the pre-slice redaction, gets cut by the
    # 200-char clip, and the surviving prefix is short enough to escape SEL's own
    # any-case write-path net too — a partial key persisting in the durable log.
    if push_allow_pending:
        _schedule_push_allow_audit(tool_name)
    return None


def is_denied_synthesized_target(
    target: str,
    patterns: list[str] | None = None,
    *,
    extra_patterns: list[str] | None = None,
    reason_notes: dict[str, str] | None = None,
) -> str | None:
    """Evaluate a SYNTHESIZED target against the patterns that participate in one.

    A synthesized target is not a command line.  It is a ``"<namespace> key=value ..."``
    summary this gate mints from a tool call's structured arguments so a rule can see a
    scope that exists nowhere in text (``hooks._search_deny_target``).  Handing it to
    :func:`is_denied` evaluates it against the WHOLE shared rule set, including the ~140
    command-oriented built-ins -- and those match its path text incidentally: the
    ``mkfs.*`` rule denies a read-only search of a directory named ``mkfs-tests``.  The
    only per-rule remedy is disabling that rule by id, which also stops it protecting
    real shell commands, so the collision costs a real control to clear.

    Which patterns participate: exactly the ones the CALLER passes.  The hooks gate
    passes the operator's own enabled regexes, and the companion overlay is evaluated
    separately and unscoped a layer up (``PolicyAuthority``).  The shipped built-in
    catalogue is NOT passed and takes no part in a synthesized target: a built-in cannot
    express a scope rule for one -- none is authored against the grammar, ratcheted by
    ``test_no_shipped_builtin_is_authored_against_the_grammar`` -- so its only possible
    hit here is the incidental one this tier exists to drop.  A future built-in written
    against the grammar fails that ratchet, which is the signal to give it an explicit
    way in.

    This is deliberately a caller-supplied SET rather than a filter applied here.  An
    earlier revision classified the merged effective set by testing each pattern's text
    against the shipped catalogue, and text cannot answer that question: an operator who
    authors a pattern whose text coincides with a shipped one (``mkfs.*`` is a natural
    thing to type) had their OWN rule read as shipped and dropped -- a silent fail-open on
    an explicit deny.  Pattern text is not provenance.  Passing only what participates
    makes provenance structural: there is nothing left to misclassify.

    What this does NOT run, and why:

    * The argv-structural floors (credential mint, self-kill, restart/update/cloud) and
      the verb-anchored git-publish detector.  Each interprets SHELL SYNTAX, and a
      synthesized target has none: its tokens are the namespace and ``key=value`` pairs,
      values are whitespace-encoded by the synthesizer so one cannot split into two
      tokens, and no such target can name a program.  A search of a tree cannot mint a
      credential or kill a process, so these can only produce false positives here.  A
      real command still reaches them through its own ``command`` target.
    * Per-segment (pass 2) re-evaluation.  Segment splitting exists to isolate a chained
      command inside one shell line; a synthesized target has no chaining semantics, so
      splitting it only manufactures pseudo-commands out of path substrings -- the same
      collision class, one layer down.

    Args:
        target: The synthesized target, e.g. ``"file-search path=/srv max_depth=3"``.
        patterns: Regex-tier patterns that participate (the operator's own).  ``None``
            or empty means the regex tier contributes nothing -- NOT that it falls back
            to every built-in, which would be the opposite of this tier's contract.
        extra_patterns: Glob-tier patterns that participate (``auto_deny_tools``).
        reason_notes: Optional ``{pattern: operator note}`` map, presentation only.

    Returns:
        Denial reason string (mentioning the matched pattern), or ``None`` if allowed.
    """
    lower = target.lower()
    all_patterns: list[tuple[str, bool]] = [(p, True) for p in list(patterns or [])] + [
        (p, False) for p in list(extra_patterns or [])
    ]
    for pattern, is_regex in all_patterns:
        if not _deny_pattern_matches(pattern, lower, is_regex):
            continue
        # No ``_DENY_EXCEPTIONS`` carve-out here.  That map ships EMPTY and its machinery
        # is retained in ``is_denied`` only for a future scoped exception, so replicating
        # it here would be dead symmetry.  If it ever gains an entry, this tier has to be
        # revisited deliberately -- ``test_the_deny_exception_map_is_still_empty`` reddens
        # then, so the omission cannot become a silent gap.
        _emit_deny_event(target, pattern, lower)
        return _deny_reason(pattern, reason_notes)
    return None


def _split_segments(command_lower: str) -> list[str]:
    """Split a command into independently-evaluatable segments.

    Splits on shell separators and command-substitution boundaries.
    Returns the list of segments (which may include the empty string for
    adjacent separators; callers should skip empties).
    """
    return _CMD_SPLIT_RE.split(command_lower)


def _emit_deny_event(
    tool_name: str, deny_pattern: str, segment: str, raw_segment: str = ""
) -> None:
    """Emit a SEL audit event when a command is denied.

    Records the operation, matched pattern, and (for pass-2 denials) the
    specific segment that triggered the block.  This satisfies the
    security-controls guideline that every permission decision — both
    grants and denials — must produce an audit trail.

    *raw_segment* is the segment's UNNORMALIZED text, recorded as a separate
    ``raw_segment`` field when it differs from *segment*.  A pass-2 match can now
    come from a quote-normalized view (``_deny_segment_views``), and the view is
    the more useful thing to show — it names the command that would have run —
    but the evasion is only visible in the spelling the caller actually
    submitted, so forensics needs both.  The full raw input is already carried in
    ``operation``; this pins WHICH segment of it normalized into the match, which
    a multi-segment command otherwise leaves the reader to re-derive.  Omitted
    when the two are equal, so an ordinary denial's event does not grow.

    Best-effort: SEL logging failures are logged at WARNING and do not
    affect the deny decision (denials are inherently fail-closed; the
    block stands regardless of audit success).
    """
    try:
        sel = SecurityEventLog()
        # ``redact_and_truncate``, never a bare slice: it redacts over the FULL text
        # BEFORE cutting, which is the rule that function exists to enforce -- a
        # credential straddling the 200-char boundary would otherwise be cut in half,
        # and the fragment no longer matches the credential pattern, so SEL's own
        # write-path redaction cannot catch it and the partial secret persists in a
        # dashboard-readable log.  Both fields take it: ``raw_segment`` is new, and
        # ``segment`` carried the same hazard from a bare slice (found by the GPT 5.6
        # review lane on the new field).
        metadata = {
            "deny_pattern": deny_pattern,
            "segment": redact_and_truncate(segment, 200) if segment else "",
            "mechanism": "BUILTIN_DENY_PATTERNS",
        }
        if raw_segment and raw_segment != segment:
            metadata["raw_segment"] = redact_and_truncate(raw_segment, 200)
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="deny_event",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation=tool_name,
                outcome="denied",
                resources=f"deny_pattern={deny_pattern}",
                metadata=metadata,
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for deny_event on %r (deny stands)",
            tool_name,
            exc_info=True,
        )


def _emit_deny_exception_event(tool_name: str, deny_pattern: str) -> bool:
    """Emit an SEL audit event when a deny exception is applied.

    Returns True if the event was logged successfully, False otherwise.
    The caller must NOT grant the exception if this returns False.
    """
    try:
        sel = SecurityEventLog()
        sel.log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="deny_exception",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation=tool_name,
                outcome="allowed",
                resources=f"deny_pattern={deny_pattern}",
                metadata={"deny_pattern": deny_pattern, "mechanism": "_DENY_EXCEPTIONS"},
            )
        )
        return True
    except Exception:
        logger.warning(
            "SEL audit failed for deny_exception — denying %r (fail-closed)",
            tool_name,
            exc_info=True,
        )
        return False


def audit_bash_command(command: str) -> str | None:
    """Check a bash command against suspicious patterns.

    Returns warning string, or None if clean.
    Patterns with ``*`` are matched as globs via fnmatch.
    """
    lower = command.lower()
    for pattern in SUSPICIOUS_BASH_PATTERNS:
        pat = pattern.lower()
        if "*" in pat:
            if fnmatch.fnmatch(lower, f"*{pat}*"):
                return f"Suspicious command detected: matches '{pattern}'"
        elif pat in lower:
            return f"Suspicious command detected: matches '{pattern}'"
    return None


def scan_history(history_dir: Path, last_n: int = 100) -> list[dict]:
    """Scan recent conversation history for suspicious tool usage.

    Returns list of findings: [{file, line, tool, command, warning}]
    """
    findings: list[dict] = []
    if not history_dir.is_dir():
        return findings

    files = sorted(history_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    checked = 0
    for f in files:
        try:
            for line in f.read_text().splitlines():
                if checked >= last_n:
                    return findings
                checked += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = entry.get("content", "")
                role = entry.get("role", "")
                if role != "assistant" or not isinstance(content, str):
                    continue
                # Check for bash commands in tool calls
                warning = audit_bash_command(content)
                if warning:
                    findings.append(
                        {
                            "file": f.name,
                            "warning": warning,
                            "snippet": content[:200],
                        }
                    )
        except OSError:
            continue
    return findings


def scan_memory() -> list[dict]:
    """Scan vector memory for suspicious content. Returns list of findings."""
    findings: list[dict] = []
    # Lazy import to avoid a circular dependency (vector_memory imports
    # redact_credentials/redact_exfiltration_urls from this module at its top
    # level) and to keep the optional numpy/faiss/snowballstemmer stack off the
    # lightweight import path. Skip the scan cleanly if it is unavailable.
    try:
        from kiro_crew.vector_memory import VectorMemoryStore
    except Exception:  # numpy/faiss/snowballstemmer are optional heavy deps; any
        # import-time failure (ImportError, OSError from a C-extension, etc.)
        # must skip the scan cleanly rather than crash the caller.
        return findings
    try:
        store = VectorMemoryStore()
        store.init()
    except Exception:
        return findings

    # Scan semantic values
    for entry in store.get_all_semantic():
        val = entry.get("value_json", "")
        if _contains_injection(val):
            findings.append(
                {
                    "type": "semantic",
                    "key": entry["key"],
                    "value": val[:200],
                    "warning": "Injection pattern detected",
                }
            )

    # Scan episodic texts
    for entry in store.get_episodic_list(limit=1000):
        text = entry.get("text", "")
        if _contains_injection(text):
            findings.append(
                {
                    "type": "episodic",
                    "key": entry["id"],
                    "value": text[:200],
                    "warning": "Injection pattern detected",
                }
            )

    store.close()
    return findings


def audit_injection_dropped(
    *,
    surface: str,
    session_key: str = "",
    channel_id: str = "",
    thread_ts: str = "",
    agent: str = "kirocrew",
    sample: str = "",
) -> None:
    """Emit an SEL audit event when injection-screened content is dropped.

    Called when :func:`contains_injection` flags untrusted external content
    (e.g. a Slack thread-parent message or thread metadata authored by a
    non-owner) and the content is dropped before reaching the LLM prompt
    Recording the attempt keeps prompt-injection attempts
    visible in the audit trail rather than silently discarded.

    Best-effort: an SEL logging failure is logged at WARNING and never
    propagates — the content is dropped regardless of audit success, so this
    cannot break prompt building.
    """
    try:
        SecurityEventLog().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="prompt_injection_dropped",
                caller_identity=session_key,
                agent=agent,
                source="context",
                operation=surface,
                outcome="dropped",
                resources=f"channel_id={channel_id} thread_ts={thread_ts}",
                metadata={
                    "surface": surface,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "sample": redact_and_truncate(sample, 200),
                    "mechanism": "contains_injection",
                },
            )
        )
    except Exception:
        logger.warning(
            "SEL audit failed for prompt_injection_dropped on %r (content still dropped)",
            surface,
            exc_info=True,
        )


def should_record_observe_history(
    channel_history: object | None,
    user_authorized: bool,
) -> bool:
    """Return True if an observe-mode message should be recorded.

    Only authorized users' messages are recorded to prevent non-owner
    prompt injection via shared channel traffic.
    """
    return channel_history is not None and user_authorized


def redact_and_truncate(text: str, max_chars: int = 4000) -> str:
    """Redact credentials and exfiltration URLs, then truncate.

    Redaction runs over the full text BEFORE the ``max_chars`` slice so a
    credential (or base64/URL blob) straddling the truncation boundary cannot
    leak as an unredacted partial fragment. Truncating first
    would cut a secret in half, leaving a prefix that no longer matches the
    credential regex and therefore escapes redaction.
    """
    return redact_credentials(redact_exfiltration_urls(text or "")[0])[0][:max_chars]


# ---------------------------------------------------------------------------
# Facade machinery
# ---------------------------------------------------------------------------
# The security controls are split by responsibility across submodules of this
# package, and ``kiro_crew.security`` stays the only import path: every name a
# submodule owns is re-exported here, private helpers included, because callers
# and tests reach them as attributes of the package and patch them by dotted
# string. Two properties have to hold for that to keep being true after a name
# moves out of this file, and only the first is free.
#
# 1. The name resolves here, bound to the SAME object the owning submodule
#    holds. The frozen list in ``_exports`` is what makes that checkable rather
#    than remembered.
# 2. Setting the attribute HERE reaches the owning submodule. A caller inside
#    that submodule resolves the name through its own globals, so a patch
#    applied only to the facade would leave it running the unpatched object --
#    the test passes while testing nothing. ``_MirroringModule`` below is what
#    closes that, so every existing patch site stays as written.
#
# ``_SUBMODULES`` is in dependency order, lowest layer first, and a name belongs
# to the first submodule holding it. That ordering is what stops a name defined
# in a lower layer from being attributed to a higher layer that merely imports
# it. The three bottom entries import nothing from this package and so are peers;
# their relative order settles which of them owns a stdlib name several of them
# import, and nothing else.

#: Submodules owning re-exported names, lowest dependency layer first.
_SUBMODULES: tuple[ModuleType, ...] = (
    vocabulary,
    diagnostics,
    helpers,
    shell_normalizer,
    paths,
    denied_rules,
    redaction,
    exfil,
    argv_floor,
)

_OWNER_PROBE_MISSING = object()


def _export_owners() -> dict[str, ModuleType]:
    """Map each re-exported name to the submodule that owns its object."""
    owners: dict[str, ModuleType] = {}
    for submodule in _SUBMODULES:
        for name, value in vars(submodule).items():
            if name.startswith("__") or name in owners:
                continue
            if globals().get(name, _OWNER_PROBE_MISSING) is value:
                owners[name] = submodule
    return owners


class _MirroringModule(ModuleType):
    """Module type that mirrors an attribute write onto the name's owner.

    ``setattr`` and ``delattr`` on the facade are applied to the owning
    submodule as well, so a patch reaches the namespace the owning code actually
    resolves through. Restoration mirrors the same way, which is what keeps the
    undo half of a patch fixture symmetric.
    """

    def __setattr__(self, name: str, value: object) -> None:
        owner = _EXPORT_OWNERS.get(name)
        if owner is not None:
            setattr(owner, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        owner = _EXPORT_OWNERS.get(name)
        if owner is not None and hasattr(owner, name):
            delattr(owner, name)
        super().__delattr__(name)


#: Name to owning submodule, resolved once at import.
_EXPORT_OWNERS: dict[str, ModuleType] = _export_owners()

# Installed last, so the mirroring is live for every caller but never runs while
# this module is still binding its own names.
sys.modules[__name__].__class__ = _MirroringModule
