"""The argv-structural self-protection floor: a floor that reads argv, not text.

Always on, and deliberately not a regex tier. Every predicate here answers a
question about a command's STRUCTURE -- which token is the program, which token
is a subcommand, which token is a redirection target, which payload a nested
interpreter would actually run -- so the product's own name appearing somewhere
in a path, a search pattern or a commit message is not on its own an answer.
That distinction is the whole reason the floor exists beside the catalog: the
catalog matches text and can be pinned or opted out of, while these predicates
are the last word on the handful of actions that must never succeed no matter
what the settings say.

Four families make up the floor:

* Release detection, which recognises a publish invocation by its subcommand
  position rather than by the verb appearing anywhere, then decides whether the
  refspec it carries names a protected branch. It fails CLOSED: an invocation
  whose target cannot be read cleanly is refused rather than guessed at.
* Termination, restart, update and destructive-cloud subcommand floors, which
  recognise the product as the argv's own program and the action as its own
  subcommand.
* The credential-mint predicates, which follow a payload through the carriers
  that can hand it to an interpreter -- an inline program, a here-document, a
  standard-input redirection, a nested shell -- and ask whether the product's
  package is imported in it.
* The ssh self-target floor, which reads an ssh-family command's destination in
  operand position and refuses one that names this host: the agent's shell runs
  inside the sandbox and sshd does not.

Each family lives in an owner module beside this one, and this module is the
import path of every name they define: ``argv_floor_git`` holds release
detection, ``argv_floor_self`` the subcommand floors and the credential-mint
predicates, ``argv_floor_ssh`` the ssh self-target floor, and
``argv_floor_hosts`` the names and addresses that floor counts as this host.
A read through this module answers with the owner's object, and a write
reaches every owner that binds the name (:mod:`kiro_crew._facade_mirror`).

Writes INTO the governance data home are not a floor here: the OS sandbox mounts
the keystone read-only in every mode, so an archive or copy destination flag
aimed at it is refused by the kernel rather than by a matcher over the command's
text.

Layer. The floor sits above the shell reader and reads its word layer, the
shared name vocabulary below it, and one sentinel the rule catalog owns for a
floor denial that no catalog pattern matches. It imports nothing from the package
facade, so the dependency runs one way.

The audit emitter for an allowed feature-branch push is NOT here. It belongs to
the tier that decides to allow, which is the evaluator, and it redacts the
command it records through the output-redaction side; keeping it beside the
evaluator is what keeps this module free of that edge.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

from .host_addresses import (  # noqa: F401  (parser re-imported as a test entry point)
    _parse_netlink_addr_dump,
)

logger = logging.getLogger(__name__)


# This module is the import path of every name the argv floor offers. The definitions live
# in the owner modules below, and a write through this module (a patch) reaches every
# module holding the name (:mod:`kiro_crew._facade_mirror`). Owners never import this
# module. New work goes to the owner of its family:
#   ``argv_floor_self`` -- the product's own subcommand floors and the credential-mint
#   predicates
#   ``argv_floor_hosts`` -- the names and addresses the ssh floor counts as this host
#   ``argv_floor_ssh`` -- the ssh self-target floor
#   ``argv_floor_git`` -- release detection and the protected-branch push target
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.security.argv_floor_self",
    "kiro_crew.security.argv_floor_hosts",
    "kiro_crew.security.argv_floor_ssh",
    "kiro_crew.security.argv_floor_git",
)  # the loop below imports them in this order
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.security.argv_floor_git import (  # noqa: F401
        _AMBIGUOUS_REFS,
        _AMBIGUOUS_REFSPEC_RE,
        _GIT_ARG_FLAGS,
        _GIT_PUBLISH_DENY_LABEL,
        _GIT_PUBLISH_GLUE_RE,
        _GIT_PUBLISH_RE,
        _GIT_PUBLISH_SUBST_PROGRAM_RE,
        _PROCESS_SUBSTITUTION_SAFE_CHARS,
        _PROTECTED_BRANCHES,
        _PUSH_ALL_BRANCHES_OPTS,
        _PUSH_NO_VALUE_OPTS,
        _PUSH_NO_VALUE_SHORTS,
        _PUSH_REPO_OPTS,
        _PUSH_VALUE_OPTS,
        _PUSH_VALUE_SHORTS,
        _SHELL_RESERVED_WORDS,
        _git_publish_floor_tags,
        _git_push_args,
        _is_git_publish,
        _is_git_push_via_normalizer,
        _is_push_to_protected_branch,
        _normalize_ref,
        _process_substitution_word_is_opaque,
        _push_segment_targets_protected,
    )
    from kiro_crew.security.argv_floor_hosts import (  # noqa: F401
        _DNS_CANDIDATE_RE,
        _GLOB_CHARS,
        _HOST_VERDICT_ALLOW_TTL,
        _HOST_VERDICT_CACHE,
        _HOST_VERDICT_CACHE_CAP,
        _HOST_VERDICT_LOCK,
        _HOST_VERDICT_PENDING,
        _HOST_VERDICT_STAMP,
        _HOSTS_FILE_CACHE,
        _HOSTS_FILE_READ_CAP,
        _HOSTS_FILE_READ_CHUNK,
        _HOSTS_KEY_MOVED_ATTEMPTS,
        _HOSTS_WARM_IN_FLIGHT,
        _HOSTS_WARM_LOCK,
        _LOOPBACK_HOST_NAMES,
        _NETLINK_ADDRS_PUBLISHED,
        _NETLINK_MISS_WARN_AT,
        _NETLINK_MISSES,
        _OWN_HOST_NAMES_CACHE,
        _OWN_HOST_REFRESH_SECS,
        _OWN_HOST_RESOLVE_BACKOFF_SECS,
        _OWN_HOST_RESOLVE_DONE,
        _OWN_HOST_RESOLVE_IN_FLIGHT,
        _OWN_HOST_RESOLVE_LOCK,
        _OWN_HOST_RESOLVE_NEXT_TRY,
        _OWN_HOST_RESOLVE_STAMP,
        _fcntl,
        _host_is_self,
        _hosts_file_key,
        _hosts_file_paths,
        _hosts_file_verdict,
        _hosts_file_warm_worker,
        _HostsKey,
        _note_netlink_result,
        _own_host_names,
        _own_host_seed,
        _own_interface_addresses,
        _parse_and_cache_for_key,
        _parse_hosts_file,
        _publish_netlink_addresses,
        _read_hosts_bytes,
        _resolve_host_verdict_into_cache,
        _resolve_own_host_names,
        _resolve_own_host_names_into_cache,
        _resolved_host_verdict,
        _schedule_hosts_file_warm,
        _struct,
        _unverifiable_hosts_file,
        _warm_hosts_file_cache,
        warm_own_host_names,
    )
    from kiro_crew.security.argv_floor_self import (  # noqa: F401
        _DEV_MODE_CONFIRM_FLAG,
        _RAW_ASSIGNMENT_RE,
        _SELF_CLOUD_DESTRUCTIVE_VERBS,
        _SELF_FLOOR_MACHINERY_RE,
        _SELF_FLOOR_NAME_HINT_RE,
        _SELF_FLOOR_QUOTE_JUNK_RE,
        _SELF_IMPORT_RE,
        _SELF_MODULE_SPELLINGS,
        _bare_kill_raw_bodies,
        _is_credential_mint,
        _is_dev_mode_out_of_root_confirm,
        _is_kill_by_name_program,
        _is_self_cloud_destructive,
        _is_self_file_delivery,
        _is_self_gateway_restart,
        _is_self_kill,
        _is_self_module_flag,
        _is_self_module_invocation,
        _is_self_restart,
        _is_self_skills_authority_retire,
        _is_self_update,
        _kill_prefix_keeps_anchor,
        _matches_self_subcommand,
        _operands_lead_with,
        _self_cli_operands,
        _self_floor_can_fire,
        _self_module_flag_scan,
        _self_module_name_index,
        _self_program_index,
        _self_token_frames,
        _SelfModuleScan,
        _shell_payload_sources,
        _static_substitution_output,
    )
    from kiro_crew.security.argv_floor_ssh import (  # noqa: F401
        _ARITH_INT_LITERAL_RE,
        _BINARY_COPY_VERBS,
        _BRACE_EXPANSION_CAP,
        _BRACE_GROUP_RE,
        _BRACE_RANGE_RE,
        _CALL_ASSIGNMENT_RE,
        _CALL_WRAPPER_WORDS,
        _EMPTY_EXPANSION_RE,
        _ENV_ASSIGN_PREFIX_RE,
        _EXPANSION_DEFAULT_RE,
        _FLAT_SUBSTITUTION_RE,
        _FUNCTION_BIND_CAP,
        _FUNCTION_DEF_RE,
        _HOSTNAME_SUBSTITUTION_HINTS,
        _HOSTNAME_VARIABLE_FORMS,
        _LINE_ASSIGNMENT_RE,
        _QUOTED_SEP_SENTINELS,
        _RSYNC_RSH_ASSIGN_RE,
        _SSH_COMMAND_OPTION_KEYS,
        _SSH_FAMILY_VERBS,
        _SSH_FORWARD_OPT_LETTERS,
        _SSH_ROUTING_OPTION_KEYS,
        _SSH_VALUELESS_SHORT_FLAGS,
        _VAR_REFERENCE_RE,
        _VERB_FOLD_AMBIGUOUS_OPT_LETTERS,
        _VERB_VALUE_TAKING_OPT_LETTERS,
        _arith_int_literal_repl,
        _brace_alternatives,
        _brace_expansions,
        _command_value_names_self_endpoint,
        _function_call_binds_self,
        _glob_can_name_ssh_verb,
        _is_ssh_to_self,
        _mask_quoted_separators,
        _operand_targets_self,
        _proxyjump_value_targets_self,
        _remote_forward_value_targets_self,
        _resolve_line_assignments,
        _routing_option_key_value_targets_self,
        _routing_option_value_targets_self,
        _ssh_family_verb,
        _unmask_separators,
    )
if TYPE_CHECKING:  # names it serves from outside its owners, from their homes
    from kiro_crew.atomic_write import on_event_loop as _event_loop_running  # noqa: F401
    from kiro_crew.security import shell_normalizer as _shell_normalizer  # noqa: F401
    from kiro_crew.security.denied_rules import (  # noqa: F401
        _GIT_PUBLISH_UNGATED,
    )
    from kiro_crew.security.host_addresses import (  # noqa: F401
        _darwin_interface_addresses,
        _linux_netlink_addresses,
        _windows_interface_addresses,
    )
    from kiro_crew.security.hosts_file import (  # noqa: F401
        _ctime_is_creation_time,
        _decoded_chunks,
        _hosts_content_digest,
        _HostsFileTooLarge,
        _HostsFileUnreadable,
        _parse_hosts_chunks,
    )
    from kiro_crew.security.inline_payload import (  # noqa: F401
        _INLINE_DYNAMIC_EXEC_RE,
        _decoded_b64_literal_sources,
        _has_self_importing_inline_program,
    )
    from kiro_crew.security.shell_normalizer import (  # noqa: F401
        _AMBIGUOUS_EXPANSION_RE,
        _PROCESS_SUBSTITUTION_OPENERS,
        _PYTHON_INLINE_PROGRAM_FLAGS,
        _PYTHON_OPERAND_FLAGS,
        _PYTHON_PROGRAM_RE,
        _REDIRECT_START_RE,
        _SHELL_WRAPPER_CHARS,
        _argv_programs,
        _backtick_closer,
        _cut_at_operator,
        _data_consumer_exempt,
        _debracket,
        _decode_printf_escapes,
        _decode_shell_quoted_literals,
        _dequote_token,
        _ends_argv,
        _glob_could_expand_to,
        _is_mint_verb,
        _is_self_program,
        _iter_shell_chars,
        _matching_close_paren,
        _nested_shell_payloads,
        _program_basename,
        _push_option_matches,
        _push_token_redirection,
        _push_token_shell_read,
        _redirect_consumes_next,
        _resolve_param_defaults,
        _shell_join_continuations,
        _shell_payload_walk,
        _shell_quote_walk,
        _split_push_command_segments,
        _split_shell_words,
        _strip_redirect,
        _substitution_bodies,
        _substitution_depth_delta,
        _xargs_here_string_rebuild,
    )
    from kiro_crew.security.vocabulary import (  # noqa: F401
        _KILL_BY_NAME_PROGRAMS,
        _SELF_FILE_DELIVERY_VERBS,
        _SELF_NAME_RE,
    )
