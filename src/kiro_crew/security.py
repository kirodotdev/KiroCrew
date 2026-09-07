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
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass

try:
    import resource as _resource
except ImportError:
    _resource = None  # type: ignore[assignment]  # Windows/non-POSIX
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar
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
from kiro_crew.vector_memory_constants import _contains_injection

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

# ── Built-in Denied-Command Rules ──
# The canonical catalog of built-in denied commands.  Each rule is a Python
# REGEX (matched case-insensitively via ``re.search``) with a stable ``id``
# (the opt-out key + SEL audit key), a ``category`` for UI grouping, and a
# human ``description``.  Rules are DEFAULT-ON but user-disableable from
# Settings → Security; a governance ``commands``-scope policy can force-pin a
# rule as un-opt-out-able (see ``platform/governance.py``).  Enforcement is at
# KiroCrew's own ``hooks.py`` PreToolUse gate — these are NOT injected into the
# kiro-cli agent spec.
#
# The always-on keystone controls (``_is_git_publish``,
# ``is_sensitive_bash_command``, ``audit_bash_exfiltration``,
# ``_check_imds_access``, ``_ENV_CRED_PATTERNS``, ``_SENSITIVE_HOME_DIRS``) are
# independent and un-disableable; they run BEFORE the rule tiers.


@dataclass(frozen=True)
class DeniedCommandRule:
    """A single built-in denied-command rule.

    ``pattern`` is a Python regex string matched via ``re.search`` with
    ``re.IGNORECASE`` (NOT an fnmatch glob).  ``id`` is a stable slug used as
    the opt-out key in config and as the SEL audit ``rule_id``.
    """

    id: str
    pattern: str
    category: str
    description: str


# Variable-name words that make an AWS variable secret-BEARING, i.e. printing it
# prints a credential. ``ACCESS`` counts: ``AWS_ACCESS_KEY_ID`` is half of a key
# pair and is the name an exfiltrator selects for first.
_AWS_SECRET_WORDS: tuple[str, ...] = ("SECRET", "SESSION", "SECURITY", "ACCESS")
_AWS_SECRET_VAR_NAMES = r"(?:" + "|".join(_AWS_SECRET_WORDS) + r")"


def _aws_secret_word_prefix_alternation() -> str:
    """Alternation over every PROPER prefix of an :data:`_AWS_SECRET_WORDS` entry.

    ``grep`` selects by SUBSTRING, so ``env | grep AWS_S`` prints
    ``AWS_SECRET_ACCESS_KEY``'s value exactly as ``env | grep AWS_SECRET`` does. A
    selector that only recognised whole words would therefore treat a one-keystroke
    truncation as benign. Matching a truncation is only sound where the operand ENDS
    there, though -- ``AWS_SDK_LOAD_CONFIG`` also begins ``AWS_S`` and leads nowhere
    secret -- which is why the caller pairs this alternation with a boundary
    lookahead and keeps the whole-word alternative separate (a whole word may be
    followed by more name characters, a truncation may not).

    Longest prefix first so the engine settles on the longest match without
    backtracking through the shorter ones.
    """
    prefixes = {word[:i] for word in _AWS_SECRET_WORDS for i in range(1, len(word))}
    return "|".join(sorted(prefixes, key=lambda prefix: (-len(prefix), prefix)))


# The name a text filter selects on, when selecting it can print a credential:
# the bare ``AWS`` / ``AWS_`` prefix (which selects every AWS variable, secrets
# included), a secret-bearing word, or a truncation of one that ends the operand.
# Selecting a named non-secret variable (``AWS_REGION``, ``AWS_PROFILE``,
# ``AWS_SDK_LOAD_CONFIG``) is allowed -- it cannot print a secret.
#
# The boundary classes include DIGITS: ``env | grep AWS1`` selects a variable whose
# name contains ``AWS1``, which no secret-bearing name does, so treating a digit as
# the end of the bare prefix would deny a command that cannot leak.
_AWS_SECRET_WORD_PREFIXES = _aws_secret_word_prefix_alternation()
_AWS_VAR_SELECTOR = (
    r"AWS(?:(?![A-Za-z0-9_])"
    r"|_(?![A-Za-z0-9])"
    rf"|_{_AWS_SECRET_VAR_NAMES}"
    rf"|_(?:{_AWS_SECRET_WORD_PREFIXES})(?![A-Za-z0-9_]))"
)

# Spellings that DUMP the environment. ``environ`` is one because
# ``/proc/<pid>/environ`` IS the process environment under a path, and ``typeset``
# because with no operand it prints every variable WITH its value. Both are here
# explicitly rather than by accident: a substring matcher caught them only because
# ``environ`` contains ``env`` and ``typeset`` contains ``set``, so bounding the
# verb as a word -- which is what stops ``pyenv``, ``dotenv``, ``src/environment``
# and ``settings.py`` from counting -- would otherwise DROP two real dumps.
# Longest spelling first so the alternation settles on ``environ`` rather than on
# the ``env`` prefix inside it.
_ENV_DUMP_VERBS = r"(?:environ|printenv|typeset|export\s+-p|env|set)"

# An environment dump PIPED through a text filter that selects AWS variables.
# Backs the disableable ``credential-exfil-env-grep-aws`` rule, which the always-on
# keystone re-enforces by id (``_ENV_CRED_SHARED_RULE_IDS``) so the two tiers cannot
# drift apart.
#
# The narrowing this rule carries over a plain substring match is entirely in its
# two anchors, because those are the two an attacker cannot rewrite around:
# * the dump verb must both BEGIN and END a word (``(?<![\w-])`` / ``(?!\w)``), so
#   ``unset``, ``offset``, ``pyenv``, ``dotenv``, ``virtualenv``,
#   ``src/environment`` and ``settings.py`` are not dumps. A ``.`` or ``/`` before
#   the verb is deliberately allowed: ``/usr/bin/env``, ``/bin/printenv`` and
#   ``/proc/self/environ`` are the same dumps under a path and are the most
#   ordinary spelling of the command. The filter word is bounded on its right the
#   same way, so a quoted filter (``env | 'grep' AWS_SECRET``) still counts while
#   ``grepfoo`` does not;
# * the selector must be a name whose selection can PRINT a credential
#   (``_AWS_VAR_SELECTOR``) -- ``env | grep AWS_REGION`` cannot, and is allowed.
# A ``|`` must appear between the dump and the filter, which is what keeps ``env``
# as a wrapper (``env FOO=1 cmd``), ``set -e; grep AWS_ file.txt`` and
# ``cat .env; grep AWS_ config.py`` out.
#
# The gaps are deliberately plain ``.*`` -- ordered existence within one LINE, with
# no attempt to confine the match to a single shell statement or pipeline stage.
# A statement-scoped span has to treat ``;`` and ``&`` as separators, and a regex
# cannot tell a separator from the identical character inside a quoted argument:
# ``env | sed 's/;/x/' | grep AWS_SECRET_ACCESS_KEY`` and
# ``env | grep -E 'a&b|AWS_SECRET'`` are ordinary credential dumps whose only
# unusual feature is a quoted separator, and a span that stops there fails OPEN.
# Guessing the other way costs an over-block instead: a ``set …`` earlier in the
# line makes any later ``… | grep AWS_`` in the same line a match. That is the
# residual, it is the safe direction, and it is the reason the gaps are not spans.
#
# What this rule does NOT cover, on purpose: a dump REDIRECTED to a file and read
# back with no pipe (``env > f; grep AWS_SECRET f``). Correlating the sink with the
# reader needs a backreference, which the RE2-style engine these built-ins are
# authored for does not have; and blocking only the ``grep`` spelling would be no
# control at all, since ``awk``, ``sed`` and a plain ``cat`` of the same file read
# it just as well and are equally unmatched. The output layer's
# ``redact_credentials`` (AKIA/ASIA plus high-entropy detection) is what stands
# between that shape and a chat surface.
_ENV_DUMP_GREP_AWS_PATTERN = (
    rf"(?<![\w-]){_ENV_DUMP_VERBS}(?!\w)"
    + r".*\|.*"
    + r"(?:grep|awk|sed)(?!\w)"
    + r".*"
    + _AWS_VAR_SELECTOR
)

# ``printenv NAME...`` prints the named variables' VALUES, so naming a
# secret-bearing variable is a credential read. Naming a non-secret one
# (``printenv AWS_REGION``) is not. Unlike ``grep``, ``printenv`` takes EXACT
# names, so a truncation (``printenv AWS_S``) prints nothing and is not denied --
# which is why this pattern uses the whole-word alternation and the piped form
# (``printenv | grep ...``) is ``_ENV_DUMP_GREP_AWS_PATTERN``'s job.
_PRINTENV_AWS_SECRET_PATTERN = r"(?<![\w-])printenv(?!\w).*AWS_" + _AWS_SECRET_VAR_NAMES


BUILTIN_DENIED_RULES: list[DeniedCommandRule] = [
    DeniedCommandRule(
        id="credential-exfil-s3-cp",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+cp .* s3://.*",
        category="credential-exfil",
        description=(
            "Blocks `aws s3 cp` uploads to an s3:// destination, which can exfiltrate local "
            "files or credentials into an attacker-controlled bucket."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-s3-mv",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+mv .* s3://.*",
        category="credential-exfil",
        description=(
            "Blocks `aws s3 mv` moves to an s3:// destination, which can exfiltrate local files "
            "or credentials into an attacker-controlled bucket."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-s3-sync",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+sync .* s3://.*",
        category="credential-exfil",
        description=(
            "Blocks `aws s3 sync` to an s3:// destination, which can bulk-exfiltrate a local "
            "directory tree into an attacker-controlled bucket."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-echo-aws-secret",
        pattern=".*echo.*\\$AWS_SECRET.*",
        category="credential-exfil",
        description=(
            "Blocks echoing the $AWS_SECRET* environment variable, which would print the AWS "
            "secret access key to stdout/logs where it can be captured."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-echo-aws-session",
        pattern=".*echo.*\\$AWS_SESSION.*",
        category="credential-exfil",
        description=(
            "Blocks echoing the $AWS_SESSION* environment variable, which would print the AWS "
            "session token to stdout/logs where it can be captured."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-echo-aws-access",
        pattern=".*echo.*\\$AWS_ACCESS.*",
        category="credential-exfil",
        description=(
            "Blocks echoing the $AWS_ACCESS* environment variable, which would print the AWS "
            "access key ID to stdout/logs where it can be captured."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-printenv-aws",
        pattern=_PRINTENV_AWS_SECRET_PATTERN,
        category="credential-exfil",
        description=(
            "Blocks `printenv` naming a secret-bearing AWS variable (`AWS_SECRET*`, "
            "`AWS_SESSION*`, `AWS_SECURITY*`, `AWS_ACCESS*`), which prints the credential "
            "held in the environment. Naming a non-secret variable such as `AWS_REGION` "
            "is allowed."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-kirocrew-token",
        # Enforced by BOTH the regex tier and the argv-structural floor
        # (``_is_credential_mint``) -- a union, so neither can fail open alone.
        # This pattern is the raw-text half: it still sees inside a nested shell
        # payload (``bash -c "… token"``) and covers the case where tokenizing
        # fails outright.  The name must be in COMMAND POSITION -- start of input or
        # after a separator, optionally quoted or path-qualified -- so the word
        # merely APPEARING in another command's arguments (``echo kirocrew token``,
        # ``git commit -m '… token …'``) is not a mint.  The gap then accepts
        # anything up to a command separator (``; & |``), a comment (``#``), a
        # redirect (``>``), a path separator (``/``) or a glob (``*``); the last two
        # keep an ordinary product-named path, and a regex LITERAL quoting this very
        # rule, from reading as a mint.  ``\btoken\b`` keeps ``tokens`` and
        # ``token_auth.py`` from matching at all.  The forms this half misses on
        # purpose (a redirect between name and verb, a quoted verb) are the floor's.
        pattern=(
            "(?:\\A|[;&|\\n`]|\\$\\()[\\s\"'(]*"
            "[\\w.:/\\\\-]*kiro[-.]?crew\\b[^|;&#>/*]*\\btoken\\b"
        ),
        category="credential-exfil",
        description=(
            "Blocks the `kirocrew token` CLI, which mints a signed dashboard access token an "
            "attacker could use to authenticate to the gateway. Matches the CLI name and the "
            "token verb within one command segment -- including nested forms such as `kirocrew "
            "pod token` and the hyphenated `kiro-crew` spelling -- so an incidental mention of "
            "the word in a later command, a comment, or a file path is not a mint. The argv "
            "floor additionally covers `python -m kiro_crew ... token`, which mints the same "
            "token through the interpreter rather than the console script."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-kirocrew-token-argv",
        # Companion to the rule above, for the case a command-text matcher cannot
        # otherwise reach: an INTERPRETER payload that spawns the CLI through a
        # library call rather than as a shell word --
        # ``python -c "subprocess.run(['kirocrew','token'])"``,
        # ``node -e 'execFileSync("kirocrew",["token"])'``,
        # ``perl -e 'system("kirocrew","token")'``.  The floor cannot help here: the
        # payload is one opaque token to the shell tokenizer and its contents are
        # Python/JS, not shell.
        #
        # Scoped to the two words as ADJACENT QUOTED ARGUMENTS, which is what every
        # such argv literal looks like.  The separator class admits only the
        # punctuation that appears BETWEEN argv elements (quote, comma, whitespace,
        # an opening bracket or paren) PLUS the characters an intervening quoted FLAG
        # is made of, since an argv literal may carry global options between the
        # program and the verb -- deliberately NOT ``.``, ``*``, ``/`` or
        # ``>``.  That is what keeps a regex LITERAL quoting this very rule
        # (``re.search(r'.*kirocrew.*token', cmd)``) and prose mentioning both words
        # from matching, both of which are recorded false positives.
        #
        # Accepted over-block from that widening: a quoted LIST that merely contains
        # both words as data (``print(['kirocrew', 'x', 'token'])``) also matches.
        # That direction is the safe one -- a visible refusal, not a silent bypass.
        # Residual limit, stated rather than implied: an interpreter that ASSEMBLES the
        # name at runtime (string concatenation, a base64 blob, an HTTP call to the
        # gateway) never contains it for any pattern to find.  The un-disableable
        # guarantee for this credential remains the sensitive-path floor over the
        # signing key, not this rule.
        pattern=(
            "(?:"
            # (a) argv literal: the two words as adjacent QUOTED arguments.
            "['\"][\\w.:/\\\\-]*kiro[-.]?crew[\\w.]*['\"][\\s,\\[\\]\\(\\)+*'\"=\\w-]*['\"]token['\"]"
            # (b) SINK-QUALIFIED single string: the two words inside ONE quoted
            # string, but only as the argument of a call that EXECUTES it.  The
            # sink prefix is what makes this safe -- it is precisely what a regex
            # literal (``re.search(...)``), a commit message and prose lack, so
            # they stay allowed while ``os.system(\"... token\")`` does not.
            "|" "(?:os\\.system|os\\.popen|os\\.exec\\w*|(?:asyncio\\.)?create_subprocess_\\w*"
            "|(?:\\w+\\.)?(?:run|call|check_call|check_output|popen|Popen|getoutput|getstatusoutput)"
            "|commands\\.getoutput|popen\\d?|system|shell_exec|passthru|proc_open"
            "|child_process\\.exec\\w*|exec\\w*sync|spawn\\w*"
            "|kernel\\.system|io\\.popen)"
            "\\s*\\(?\\s*[a-z]{0,2}['\"][^'\"]*\\b(?:kiro[-.]?crew|irocrew)\\b"
            "[^'\"]*\\btoken\\b"
            ")"
        ),
        category="credential-exfil",
        description=(
            "Blocks an interpreter payload that spawns the `kirocrew token` credential mint "
            "through a library call rather than as a shell command -- the CLI name and the "
            "token verb as adjacent QUOTED arguments, as in "
            "`python -c \"subprocess.run(['kirocrew','token'])\"`. Scoped to the argv-literal "
            "shape so a regex literal or prose mentioning both words is not a mint; a "
            "single-string spelling is out of reach of command-text matching and is covered by "
            "the sensitive-path floor over the signing key instead."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-kill-interpreter",
        # Companion to ``self-protection-kill`` for the shape a shell-command matcher
        # cannot reach: an INTERPRETER payload that terminates the gateway through a
        # library call -- ``os.system("pkill -f kirocrew")``,
        # ``execSync("pkill -f kirocrew")``.  The argv floor cannot help; the payload is
        # one opaque token to the shell tokenizer and its contents are Python/JS.
        #
        # SINK-QUALIFIED on purpose: the two words are matched inside ONE quoted string
        # only when that string is the argument of a call that EXECUTES it.  The sink
        # prefix is what keeps this from becoming the co-occurrence rule this PR
        # removed -- prose, a commit message and a regex literal have no sink, so they
        # stay allowed.
        pattern=(
            "(?:"
            # --- sink-qualified: a shell command handed to a call that EXECUTES it ---
            "(?:os\\.system|os\\.popen|os\\.exec\\w*|(?:asyncio\\.)?create_subprocess_\\w*"
            "|(?:\\w+\\.)?(?:run|call|check_call|check_output|popen|Popen|getoutput|getstatusoutput)"
            "|commands\\.getoutput|popen\\d?|system|shell_exec|passthru|proc_open"
            "|child_process\\.exec\\w*|exec\\w*sync|spawn\\w*"
            "|kernel\\.system|io\\.popen)"
            "(?:"
            # (a) the command as a single quoted string.
            "\\s*\\(?\\s*[a-z]{0,2}['\"][^'\"]*\\b(?:pkill|killall)\\b"
            "[^'\"]*\\b(?:kiro[-.]?crew|irocrew)\\b"
            # (b) the command as an argv LIST -- verb and target as separate quoted
            # elements (``run(['pkill','-f','kirocrew'])``), list concatenation included.
            "|[\\s\\(\\[]*['\"][\\w.:/\\\\-]*(?:pkill|killall)['\"]"
            "[\\s,\\[\\]\\(\\)+*'\"=\\w-]*['\"][^'\"]*(?:kiro[-.]?crew|irocrew)"
            ")"
            # --- a DIRECT process-kill API, which IS the sink and therefore stands on
            # its own rather than behind the list above: ``os.kill(pid_from("[k]irocrew
            # gateway"), 9)``.  The signal is the kill API and the product name in the
            # same call.  Matched on ``irocrew`` rather than the full name so the
            # standard "don't match my own lookup" bracket idiom (``[k]irocrew``), which
            # still resolves to the gateway, is not a free pass.
            "|(?:os\\.kill(?:pg)?|process\\.kill|\\bkillpg)\\s*\\([^)]*irocrew"
            ")"
        ),
        category="self-protection",
        description=(
            "Blocks an interpreter payload that terminates a kirocrew process through a "
            "library call rather than as a shell command -- a pkill/killall command and the "
            "product name inside one quoted string passed to an executing sink such as "
            "`os.system(...)` or `execSync(...)`. Sink-qualified so prose, a commit message "
            "or a regex literal naming both is not a kill."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-env-grep-aws",
        pattern=_ENV_DUMP_GREP_AWS_PATTERN,
        category="credential-exfil",
        description=(
            "Blocks piping an environment dump (`env`, `printenv`, `set`, `export -p`, "
            "`typeset`, `/proc/<pid>/environ`) through grep/awk/sed for the bare "
            "`AWS`/`AWS_` prefix, a secret-bearing AWS variable, or a truncation of one "
            "such as `AWS_S`, which leaks any credentials stored there. Selecting a named "
            "non-secret variable such as `AWS_REGION` or `AWS_SDK_LOAD_CONFIG` is allowed."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-python-boto3-get-credentials",
        pattern=".*python.*boto3.*get_credentials.*",
        category="credential-exfil",
        description=(
            "Blocks a Python/boto3 one-liner calling get_credentials(), which resolves and can "
            "print the active AWS credentials from the credential chain."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-python-botocore-credentials",
        pattern=".*python.*botocore.*credentials.*",
        category="credential-exfil",
        description=(
            "Blocks a Python/botocore one-liner accessing the credentials module, which can "
            "resolve and expose the active AWS credentials."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-curl-imds",
        pattern=".*curl.*169\\.254\\.169\\.254.*",
        category="credential-exfil",
        description=(
            "Blocks `curl` to the 169.254.169.254 instance metadata service (IMDS), a classic "
            "path to steal EC2 role credentials."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-wget-imds",
        pattern=".*wget.*169\\.254\\.169\\.254.*",
        category="credential-exfil",
        description=(
            "Blocks `wget` to the 169.254.169.254 instance metadata service (IMDS), a classic "
            "path to steal EC2 role credentials."
        ),
    ),
    # ── Floor-PRIMARY exfil rules ──
    # Enforcement for these seven is the always-on gate
    # (``audit_bash_exfiltration`` / ``_check_imds_access``), not the regex tier:
    # the gate sees flag spellings and IP encodings no single human-auditable
    # regex can cover. Each ``pattern`` below is a readable SUBSET that exists so
    # the rule has a catalog identity — an id to switch off, a row in Settings, and
    # a ``rule_id`` in the SEL trail. Same shape as
    # ``credential-exfil-kirocrew-token``.
    DeniedCommandRule(
        id="credential-exfil-imds-any",
        pattern=".*169\\.254\\.169\\.254.*",
        category="credential-exfil",
        description=(
            "Blocks reaching the instance metadata service by ANY verb and ANY IP encoding "
            "(decimal, hex, octal, IPv6-mapped, and the fd00:ec2::254 endpoint) — the "
            "curl/wget rules above only cover those two verbs and the literal dotted quad."
        ),
    ),
    DeniedCommandRule(
        id="data-exfil-curl-file-body",
        pattern=".*curl.*--?data(-binary|-ascii|-urlencode)?[= ]@.*",
        category="credential-exfil",
        description=(
            "Blocks a `curl` request whose body is read from a LOCAL FILE (`-d @file` and every "
            "--data variant), the tell-tale shape of pushing local data out to a remote."
        ),
    ),
    DeniedCommandRule(
        id="data-exfil-curl-multipart-upload",
        pattern=".*curl.*(-F|--form)\\s*\\S*=@.*",
        category="credential-exfil",
        description=(
            "Blocks a `curl` multipart upload that attaches a local file (`-F field=@file`), "
            "for any field name."
        ),
    ),
    DeniedCommandRule(
        id="data-exfil-curl-upload",
        pattern=".*curl.*(--upload-file|(^|\\s)-T\\s*\\S).*",
        category="credential-exfil",
        description=(
            "Blocks a `curl` file upload (`--upload-file` / `-T file`), which sends a local file "
            "to a remote endpoint."
        ),
    ),
    DeniedCommandRule(
        id="data-exfil-wget-post-file",
        pattern=".*wget.*--post-file.*",
        category="credential-exfil",
        description=(
            "Blocks `wget --post-file`, which posts the contents of a local file to a remote "
            "endpoint."
        ),
    ),
    DeniedCommandRule(
        id="data-exfil-nc-file-redirect",
        pattern=".*(^|\\s)nc(at)?\\s+\\S.*<.*",
        category="credential-exfil",
        description=(
            "Blocks piping a local file into `nc`/`ncat` via input redirection, a plain-socket "
            "way to ship data off the host."
        ),
    ),
    DeniedCommandRule(
        id="reverse-shell-devtcp",
        pattern=".*/dev/(tcp|udp)/.*",
        category="reverse-shell",
        description=(
            "Blocks bash's /dev/tcp and /dev/udp pseudo-devices, which open a raw socket to a "
            "remote host without any external tool — the classic dependency-free reverse shell."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-curl-aws-secret",
        pattern=".*curl.*\\$AWS_SECRET.*",
        category="credential-exfil",
        description=(
            "Blocks `curl` invocations that reference $AWS_SECRET*, which would send the AWS "
            "secret access key to a remote endpoint."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-curl-aws-access",
        pattern=".*curl.*\\$AWS_ACCESS.*",
        category="credential-exfil",
        description=(
            "Blocks `curl` invocations that reference $AWS_ACCESS*, which would send the AWS "
            "access key ID to a remote endpoint."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-curl-aws-session",
        pattern=".*curl.*\\$AWS_SESSION.*",
        category="credential-exfil",
        description=(
            "Blocks `curl` invocations that reference $AWS_SESSION*, which would send the AWS "
            "session token to a remote endpoint."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-autoscaling-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+autoscaling(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws autoscaling delete-*' command, which tears down Auto Scaling "
            "groups, policies, or launch configurations and can permanently disrupt capacity "
            "management."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-cfn-delete-stack",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+cloudformation(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-stack.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws cloudformation delete-stack', which destroys an entire CloudFormation "
            "stack and every resource it manages."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-cfn-deploy-mutate",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+cloudformation(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+(deploy|create-stack|update-stack|create-change-set|execute-change-set).*",
        category="aws-destructive",
        description=(
            "Blocks CloudFormation "
            "deploy/create-stack/update-stack/create-change-set/execute-change-set, which "
            "create or mutate infrastructure stacks and can overwrite live production "
            "resources."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ec2-run-instances",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ec2(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+run-instances.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws ec2 run-instances', which launches new EC2 instances that incur cost "
            "and can be abused for resource sprawl or cryptomining."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ec2-create-security-group",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ec2(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+create-security-group.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws ec2 create-security-group', which creates new network access-control "
            "groups that can widen the attack surface."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ec2-authorize-security-group",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ec2(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+authorize-security-group-(ingress|egress).*",
        category="aws-destructive",
        description=(
            "Blocks 'aws ec2 authorize-security-group-ingress/egress', which opens firewall "
            "rules and can expose resources to the public internet."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-iam-privilege-mutate",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+iam(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+(create-role|create-policy|create-policy-version|put-role-policy|attach-role-policy|create-instance-profile|add-role-to-instance-profile|pass-role).*",
        category="aws-destructive",
        description=(
            "Blocks IAM role/policy creation, attachment, and pass-role operations, which grant "
            "or escalate privileges and are a classic privilege-escalation vector."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-cfn-termination-protection",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+cloudformation(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+update-termination-protection.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws cloudformation update-termination-protection', which can disable the "
            "safeguard that prevents accidental stack deletion."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-dynamodb-delete-table",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+dynamodb(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-table.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws dynamodb delete-table', which permanently deletes a DynamoDB table and "
            "every item it holds."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ec2-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ec2(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws ec2 delete-*' command, which removes EC2 resources such as VPCs, "
            "subnets, volumes, snapshots, or security groups."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ec2-terminate-instances",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ec2(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+terminate-instances.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws ec2 terminate-instances', which permanently shuts down and deletes "
            "running EC2 instances."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ssm-send-command",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ssm(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+send-command.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws ssm send-command', which executes arbitrary commands on managed "
            "instances (remote code execution across the fleet)."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ssm-start-session",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ssm(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+start-session.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws ssm start-session', which opens an interactive shell onto a managed "
            "instance, bypassing normal access controls."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ssm-get-command-invocation",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ssm(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+get-command-invocation.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws ssm get-command-invocation', which reads the output of remotely "
            "executed SSM commands (used to harvest results of injected commands)."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ssm-list-command-invocations",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ssm(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+list-command-invocations.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws ssm list-command-invocations', which enumerates remote-command "
            "execution history on managed instances."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ecr-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ecr(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws ecr delete-*' command, which removes container image repositories "
            "or images and can break deployments relying on them."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-ecs-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+ecs(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws ecs delete-*' command, which tears down ECS clusters, services, or "
            "task definitions and can cause service outages."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-eks-delete-cluster",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+eks(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-cluster.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws eks delete-cluster', which destroys an entire Kubernetes control plane "
            "and all workloads running on it."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-elasticache-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+elasticache(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws elasticache delete-*' command, which removes Redis/Memcached "
            "clusters and destroys their cached data."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-elb-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+elb(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws elb delete-*' command (classic load balancers), which can drop "
            "traffic routing and cause an outage."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-elbv2-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+elbv2(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws elbv2 delete-*' command (ALB/NLB load balancers, listeners, target "
            "groups), which can drop traffic routing and cause an outage."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-glue-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+glue(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws glue delete-*' command, which removes Glue databases, tables, "
            "jobs, or crawlers and can break data pipelines."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-iam-create-access-key",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+iam(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+create-access-key.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws iam create-access-key', which mints long-lived programmatic "
            "credentials that can be exfiltrated for persistent access."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-iam-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+iam(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws iam delete-*' command, which removes roles, users, policies, or "
            "access keys and can lock out legitimate access."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-kinesis-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+kinesis(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws kinesis delete-*' command, which removes data streams and discards "
            "in-flight records."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-kms-schedule-key-deletion",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+kms(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+schedule-key-deletion.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws kms schedule-key-deletion', which queues a KMS key for deletion and "
            "can permanently render all data encrypted under it unrecoverable."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-lambda-delete-function",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+lambda(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-function.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws lambda delete-function', which removes a serverless function and can "
            "break dependent workflows."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-logs-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+logs(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws logs delete-*' command, which deletes CloudWatch log "
            "groups/streams and can destroy audit and forensic evidence."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-opensearch-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+opensearch(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws opensearch delete-*' command, which removes OpenSearch domains and "
            "destroys their indexed data."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-rds-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+rds(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws rds delete-*' command, which removes RDS instances, clusters, or "
            "snapshots and can cause irreversible data loss."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-redshift-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+redshift(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws redshift delete-*' command, which removes Redshift clusters or "
            "snapshots and can cause irreversible data-warehouse loss."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-route53-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+route53(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws route53 delete-*' command, which removes DNS hosted zones or "
            "records and can take domains offline."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-s3-rb",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+rb.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws s3 rb', which removes an S3 bucket (with --force, deleting all its "
            "objects)."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-s3-rm",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+rm.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws s3 rm', which deletes S3 objects (recursively with --recursive) and "
            "can wipe stored data."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-s3api-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3api(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws s3api delete-*' command, which removes buckets, objects, object "
            "versions, or bucket configs and can cause data loss."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-s3api-put-object",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3api(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+put-object.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws s3api put-object', which writes/overwrites S3 objects and can corrupt "
            "data or stage exfiltrated content."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-s3api-copy-object",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3api(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+copy-object.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws s3api copy-object', which overwrites S3 objects or duplicates data "
            "across buckets (a data-movement/exfil vector)."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-s3api-multipart-upload",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3api(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+(create-multipart-upload|upload-part|upload-part-copy|complete-multipart-upload).*",
        category="aws-destructive",
        description=(
            "Blocks S3 multipart-upload operations "
            "(create/upload-part/upload-part-copy/complete), which write large objects into S3 "
            "and can overwrite data or stage exfiltration."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-s3api-put-bucket",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+s3api(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+put-bucket-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws s3api put-bucket-*' command, which mutates bucket configuration "
            "such as policy, ACL, encryption, or public-access settings and can weaken data "
            "protections."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-secretsmanager-delete-secret",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+secretsmanager(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-secret.*",
        category="aws-destructive",
        description=(
            "Blocks 'aws secretsmanager delete-secret', which removes stored secrets and can "
            "break every service depending on them."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-sns-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+sns(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws sns delete-*' command, which removes SNS topics or subscriptions "
            "and can silently break notification delivery."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-sqs-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+sqs(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws sqs delete-*' command, which removes SQS queues or purges messages "
            "and can drop in-flight work."
        ),
    ),
    DeniedCommandRule(
        id="aws-destructive-stepfunctions-delete",
        pattern="aws(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+stepfunctions(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+delete-.*",
        category="aws-destructive",
        description=(
            "Blocks any 'aws stepfunctions delete-*' command, which removes Step Functions "
            "state machines or activities and can break orchestration workflows."
        ),
    ),
    DeniedCommandRule(
        id="iac-teardown-cdk-destroy",
        pattern="cdk destroy.*",
        category="iac-teardown",
        description=(
            "Blocks `cdk destroy`, which tears down an entire AWS CDK stack and all its "
            "provisioned cloud resources — irreversible infrastructure and data loss."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chmod-777",
        pattern="chmod 777.*",
        category="local-destructive",
        description=(
            "Blocks chmod 777, which grants world read/write/execute permissions and creates a "
            "serious security exposure."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chmod-usr",
        pattern="chmod.*/usr/.*",
        category="local-destructive",
        description=(
            "Blocks chmod changes to /usr, which can corrupt permissions on system binaries and "
            "break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chmod-etc",
        pattern="chmod.*/etc/.*",
        category="local-destructive",
        description=(
            "Blocks chmod changes to /etc, which can corrupt permissions on critical system "
            "config files and break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chmod-sbin",
        pattern="chmod.*/sbin/.*",
        category="local-destructive",
        description=(
            "Blocks chmod changes to /sbin, which can corrupt permissions on privileged system "
            "binaries and break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chmod-boot",
        pattern="chmod.*/boot/.*",
        category="local-destructive",
        description=(
            "Blocks chmod changes to /boot, which can corrupt permissions on boot/kernel files "
            "and render the system unbootable."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chmod-lib",
        pattern="chmod.*/lib/.*",
        category="local-destructive",
        description=(
            "Blocks chmod changes to /lib, which can corrupt permissions on shared system "
            "libraries and break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chmod-lib64",
        pattern="chmod.*/lib64/.*",
        category="local-destructive",
        description=(
            "Blocks chmod changes to /lib64, which can corrupt permissions on 64-bit shared "
            "system libraries and break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chown-usr",
        pattern="chown.*/usr/.*",
        category="local-destructive",
        description=(
            "Blocks chown changes to /usr, which can corrupt ownership on system binaries and "
            "break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chown-etc",
        pattern="chown.*/etc/.*",
        category="local-destructive",
        description=(
            "Blocks chown changes to /etc, which can corrupt ownership on critical system "
            "config files and break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chown-sbin",
        pattern="chown.*/sbin/.*",
        category="local-destructive",
        description=(
            "Blocks chown changes to /sbin, which can corrupt ownership on privileged system "
            "binaries and break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chown-boot",
        pattern="chown.*/boot/.*",
        category="local-destructive",
        description=(
            "Blocks chown changes to /boot, which can corrupt ownership on boot/kernel files "
            "and render the system unbootable."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chown-lib",
        pattern="chown.*/lib/.*",
        category="local-destructive",
        description=(
            "Blocks chown changes to /lib, which can corrupt ownership on shared system "
            "libraries and break the OS."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-chown-lib64",
        pattern="chown.*/lib64/.*",
        category="local-destructive",
        description=(
            "Blocks chown changes to /lib64, which can corrupt ownership on 64-bit shared "
            "system libraries and break the OS."
        ),
    ),
    DeniedCommandRule(
        id="pipe-to-shell-curl-bash",
        pattern="curl .* \\| bash",
        category="pipe-to-shell",
        description=(
            "Blocks piping a curl download directly into bash, which executes arbitrary remote "
            "code with no chance to inspect the script first."
        ),
    ),
    DeniedCommandRule(
        id="pipe-to-shell-curl-sh",
        pattern="curl .* \\| sh",
        category="pipe-to-shell",
        description=(
            "Blocks piping a curl download directly into sh, which executes arbitrary remote "
            "code with no chance to inspect the script first."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-dd-if",
        pattern="dd if=.*",
        category="local-destructive",
        description=(
            "Blocks dd invocations with an input file, which can overwrite raw disks/partitions "
            "and cause irreversible data loss."
        ),
    ),
    DeniedCommandRule(
        id="sql-drop-database",
        pattern="(?i:DROP\\s+DATABASE.*)",
        category="sql",
        description=(
            "Blocks SQL DROP DATABASE statements, which irreversibly delete an entire database "
            "and all its tables and data."
        ),
    ),
    DeniedCommandRule(
        id="sql-drop-table",
        pattern="(?i:DROP\\s+TABLE.*)",
        category="sql",
        description=(
            "Blocks SQL DROP TABLE statements, which permanently delete a table and every row "
            "it contains."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-export-aws-access",
        pattern="export AWS_ACCESS.*",
        category="credential-exfil",
        description=(
            "Blocks `export AWS_ACCESS...`, which injects an attacker-chosen AWS access key ID "
            "into the environment for later use by AWS tooling."
        ),
    ),
    DeniedCommandRule(
        id="credential-exfil-export-aws-secret",
        pattern="export AWS_SECRET.*",
        category="credential-exfil",
        description=(
            "Blocks `export AWS_SECRET...`, which injects an attacker-chosen AWS secret access "
            "key into the environment for later use by AWS tooling."
        ),
    ),
    DeniedCommandRule(
        id="git-publish-push-bare",
        pattern=".*git\\s+(-\\S+\\s+[^-]\\S*\\s+|-\\S+\\s+)*push\\s*$",
        category="git-publish",
        description=(
            "Blocks a bare 'git push' with no explicit remote or branch, which pushes the "
            "current branch to its default upstream (often a protected branch like main) "
            "without confirmation."
        ),
    ),
    DeniedCommandRule(
        id="git-publish-push-single-arg",
        pattern=".*git\\s+(-\\S+\\s+[^-]\\S*\\s+|-\\S+\\s+)*push\\s+\\S+\\s*$",
        category="git-publish",
        description=(
            "Blocks 'git push <remote>' with a single argument (no branch), which pushes to the "
            "configured upstream and can publish to a protected branch unattended."
        ),
    ),
    DeniedCommandRule(
        id="git-publish-push-ambiguous-ref",
        pattern=".*git\\s+(-\\S+\\s+[^-]\\S*\\s+|-\\S+\\s+)*push\\s+.*[\\s:]\\+?(head|@|fetch_head)(\\s.*|$)",
        category="git-publish",
        description=(
            "Blocks 'git push' whose destination is a symbolic ref that resolves at run time "
            "(HEAD, @, FETCH_HEAD) — if the checked-out branch is a protected one, this "
            "publishes to it, and the target cannot be verified before the push runs."
        ),
    ),
    DeniedCommandRule(
        id="git-publish-push-protected-branch-name",
        pattern=".*git\\s+(-\\S+\\s+[^-]\\S*\\s+|-\\S+\\s+)*push\\s+.*[\\s:]\\+?(main|mainline|master)(\\s.*|$)",  # wokeignore:rule=master
        category="git-publish",
        description=(
            "Blocks 'git push' whose refspec targets a protected default branch, including "
            "force-push '+' refspecs, preventing unreviewed writes to the trunk."
        ),
    ),
    DeniedCommandRule(
        id="git-publish-push-protected-ref-path",
        pattern=".*git\\s+(-\\S+\\s+[^-]\\S*\\s+|-\\S+\\s+)*push\\s+.*(refs/)?(heads/|remotes/[^/\\s]+/)(main|mainline|master)(\\s.*|$)",  # wokeignore:rule=master
        category="git-publish",
        description=(
            "Blocks 'git push' targeting a fully-qualified ref path (refs/heads/ or "
            "remotes/<remote>/) for a protected default branch, catching path-style "
            "evasions of the trunk-push guard."
        ),
    ),
    DeniedCommandRule(
        id="git-publish-push-wildcard-refspec",
        pattern=".*git\\s+(-\\S+\\s+[^-]\\S*\\s+|-\\S+\\s+)*push\\s+[^&|;\\n]*\\*",
        category="git-publish",
        description=(
            "Blocks 'git push' with a wildcard '*' refspec, which can mass-publish many "
            "branches (potentially including protected ones) in a single command."
        ),
    ),
    DeniedCommandRule(
        id="git-publish-push-brace-expansion-refspec",
        pattern=".*git\\s+(-\\S+\\s+[^-]\\S*\\s+|-\\S+\\s+)*push\\s+[^&|;\\n]*\\{[^{}]*(,|\\.\\.)[^{}]*\\}",
        category="git-publish",
        description=(
            "Blocks 'git push' using shell brace-expansion (e.g. {a,b} or {1..3}) in the "
            "refspec, which expands to multiple branch targets and could push to a protected "
            "branch."
        ),
    ),
    DeniedCommandRule(
        id="git-publish-push-mirror-all",
        pattern=".*git\\s+(-\\S+\\s+[^-]\\S*\\s+|-\\S+\\s+)*push\\s+.*--(mirror|all)(\\s.*|$)",
        category="git-publish",
        description=(
            "Blocks 'git push --mirror' and 'git push --all', which push every local ref/branch "
            "to the remote and can overwrite or publish protected branches wholesale."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-git-reset-hard",
        pattern="git reset --hard.*",
        category="local-destructive",
        description=(
            "Blocks git reset --hard, which discards uncommitted changes and rewrites the "
            "working tree, causing irreversible loss of local work."
        ),
    ),
    DeniedCommandRule(
        id="iac-teardown-kubectl-delete-namespace",
        pattern="kubectl delete namespace.*",
        category="iac-teardown",
        description=(
            "Blocks `kubectl delete namespace`, which deletes a Kubernetes namespace and "
            "cascades to every workload, service, and volume inside it — irreversible "
            "cluster-wide teardown."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-mkfs",
        pattern="mkfs.*",
        category="local-destructive",
        description=(
            "Blocks mkfs (and mkfs.* variants), which formats a filesystem and destroys all "
            "existing data on the target device."
        ),
    ),
    DeniedCommandRule(
        id="reverse-shell-nc",
        pattern="nc -e.*",
        category="reverse-shell",
        description=(
            "Blocks 'nc -e', which spawns a netcat reverse shell handing remote command "
            "execution to an attacker."
        ),
    ),
    DeniedCommandRule(
        id="reverse-shell-ncat",
        pattern="ncat -e.*",
        category="reverse-shell",
        description=(
            "Blocks 'ncat -e', which spawns an ncat reverse shell handing remote command "
            "execution to an attacker."
        ),
    ),
    DeniedCommandRule(
        id="iac-teardown-pulumi-destroy",
        pattern="pulumi destroy.*",
        category="iac-teardown",
        description=(
            "Blocks `pulumi destroy`, which deletes all cloud resources managed by a Pulumi "
            "stack — irreversible infrastructure teardown."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-rm-rf-root",
        pattern="rm -rf /.*",
        category="local-destructive",
        description=(
            "Blocks recursive force-deletion rooted at the filesystem root (rm -rf /...), which "
            "can wipe the entire operating system and all data."
        ),
    ),
    DeniedCommandRule(
        id="local-destructive-rm-rf-home",
        pattern="rm -rf ~.*",
        category="local-destructive",
        description=(
            "Blocks recursive force-deletion of the user home directory (rm -rf ~...), which "
            "would destroy all personal files and config."
        ),
    ),
    DeniedCommandRule(
        id="iac-teardown-terraform-destroy",
        pattern="terraform destroy.*",
        category="iac-teardown",
        description=(
            "Blocks `terraform destroy`, which destroys every resource tracked in the Terraform "
            "state — irreversible infrastructure and data loss."
        ),
    ),
    DeniedCommandRule(
        id="sql-truncate-table",
        pattern="(?i:TRUNCATE\\s+TABLE.*)",
        category="sql",
        description=(
            "Blocks SQL TRUNCATE TABLE statements, which delete all rows in a table in one "
            "unrecoverable operation."
        ),
    ),
    DeniedCommandRule(
        id="pipe-to-shell-wget-bash",
        pattern="wget .* \\| bash",
        category="pipe-to-shell",
        description=(
            "Blocks piping a wget download directly into bash, which executes arbitrary remote "
            "code with no chance to inspect the script first."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-aws",
        pattern=".*cat.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read files under ~/.aws, which holds AWS access keys and "
            "session credentials that could be exfiltrated."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-ssh",
        pattern=".*cat.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read files under ~/.ssh, which holds private SSH keys and "
            "known-hosts data granting remote access."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-gnupg",
        pattern=".*cat.*/\\.gnupg/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read files under ~/.gnupg, which holds GPG private keyrings "
            "and trust data used to sign or decrypt secrets."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-gpg",
        pattern=".*cat.*/\\.gpg/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read files under ~/.gpg, which holds GPG key material used to "
            "sign or decrypt secrets."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-netrc",
        pattern=".*cat.*/\\.netrc.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read ~/.netrc, which stores plaintext login/password "
            "credentials for FTP, HTTP, and other services."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-git-credentials",
        pattern=".*cat.*/\\.git-credentials.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read ~/.git-credentials, which stores plaintext Git remote "
            "usernames and access tokens."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-npmrc",
        pattern=".*cat.*/\\.npmrc.*",
        category="sensitive-file-read",
        description="Blocks using cat to read ~/.npmrc, which can contain npm registry auth tokens.",
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-pypirc",
        pattern=".*cat.*/\\.pypirc.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read ~/.pypirc, which can contain PyPI upload usernames and "
            "API tokens."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-docker-config",
        pattern=".*cat.*/\\.docker/config\\.json.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read ~/.docker/config.json, which holds base64-encoded "
            "container registry credentials."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-kube-config",
        pattern=".*cat.*/\\.kube/config.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read ~/.kube/config, which holds Kubernetes cluster tokens, "
            "client certs, and API credentials."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cat-kirocrew-env",
        # Match both the LIVE ~/.kiro/crew/.env and the legacy ~/.kirocrew/.env,
        # since a box that still has a legacy home holds live secrets at the
        # legacy path.
        pattern=".*cat.*/(?:\\.kiro/crew|\\.kirocrew)/\\.env.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read Kiro Crew's own credential file (~/.kiro/crew/.env, "
            "or the pre-move ~/.kirocrew/.env), which holds Kiro Crew's own secrets and "
            "environment credentials."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-head-aws",
        pattern=".*head.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using head to read files under ~/.aws, which holds AWS access keys and "
            "session credentials."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-tail-aws",
        pattern=".*tail.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using tail to read files under ~/.aws, which holds AWS access keys and "
            "session credentials."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-less-aws",
        pattern=".*less.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using less to read files under ~/.aws, which holds AWS access keys and "
            "session credentials."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-more-aws",
        pattern=".*more.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using more to read files under ~/.aws, which holds AWS access keys and "
            "session credentials."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-strings-aws",
        pattern=".*strings.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using strings to extract text from files under ~/.aws, which holds AWS "
            "access keys and session credentials."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-base64-aws",
        pattern=".*base64.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using base64 to encode/dump files under ~/.aws, a common way to exfiltrate "
            "AWS credentials past text filters."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-head-ssh",
        pattern=".*head.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using head to read files under ~/.ssh, which holds private SSH keys "
            "granting remote access."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-tail-ssh",
        pattern=".*tail.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using tail to read files under ~/.ssh, which holds private SSH keys "
            "granting remote access."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-less-ssh",
        pattern=".*less.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using less to read files under ~/.ssh, which holds private SSH keys "
            "granting remote access."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-more-ssh",
        pattern=".*more.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using more to read files under ~/.ssh, which holds private SSH keys "
            "granting remote access."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-strings-ssh",
        pattern=".*strings.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using strings to extract text from files under ~/.ssh, which holds private "
            "SSH keys granting remote access."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-base64-ssh",
        pattern=".*base64.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks using base64 to encode/dump files under ~/.ssh, a common way to exfiltrate "
            "private SSH keys past text filters."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cp-aws",
        pattern=".*cp.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks copying files out of ~/.aws, which would duplicate AWS credentials to an "
            "unprotected location for exfiltration."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-cp-ssh",
        pattern=".*cp.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks copying files out of ~/.ssh, which would duplicate private SSH keys to an "
            "unprotected location for exfiltration."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-python-aws",
        pattern=".*python.*open.*/\\.aws/.*",
        category="sensitive-file-read",
        description=(
            "Blocks a Python open() of files under ~/.aws, a scripted path to read AWS "
            "credentials past shell-verb filters."
        ),
    ),
    DeniedCommandRule(
        id="sensitive-file-read-python-ssh",
        pattern=".*python.*open.*/\\.ssh/.*",
        category="sensitive-file-read",
        description=(
            "Blocks a Python open() of files under ~/.ssh, a scripted path to read private SSH "
            "keys past shell-verb filters."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-restart",
        # The CLI accepts top-level flags BEFORE the subcommand (``-v``/``--verbose``
        # is ``action="count"`` and ``--no-jail`` is declared on the top-level parser),
        # so the four self-protection patterns below allow an interposed flag run
        # between the program name and the subcommand. The flag-run construct is
        # byte-identical to the ``credential-exfil-s3-cp``/aws idiom on purpose:
        # ``_linearize_deny_pattern`` rewrites exactly that spelling into its
        # linear-time equivalent, so reusing it keeps these rules ReDoS-safe (#4799).
        pattern=".*kiro.?crew(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+restart.*",
        category="self-protection",
        description=(
            "Blocks 'kirocrew restart' so the agent cannot restart its own gateway process and "
            "disrupt the running session or evade in-flight controls."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-update",
        pattern=".*kiro.?crew(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+update.*",
        category="self-protection",
        description=(
            "Blocks 'kirocrew update' so the agent cannot self-update (git pull + rebuild + "
            "execv restart) and swap out its own running code without operator oversight."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-cloud",
        pattern=(
            ".*kiro.?crew(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+cloud\\s+"
            "(destroy|stop|start|launch|connect|tunnel|log(in|out)).*"
        ),
        category="self-protection",
        description=(
            "Blocks 'kirocrew cloud' lifecycle subcommands "
            "(destroy/stop/start/launch/connect/tunnel/login/logout) so the agent cannot tear "
            "down, provision, re-authenticate, or sign out its own cloud instance."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-cron-adopt",
        pattern=".*kiro.?crew\\b(?:(?!&&)[^;|])*?\\bcron\\b(?:(?!&&)[^;|])*?\\badopt\\b.*",
        category="self-protection",
        description=(
            "Blocks 'kirocrew cron adopt' so the agent cannot assign itself ownership of a "
            "scheduled job. A cron's owning session both manages the job and receives its "
            "output, and the MCP cron tools deliberately cannot write that field -- without "
            "this rule a session could reach the same power through bash and claim a job that "
            "belongs to another session. The gaps between the words tolerate anything that is "
            "not a command separator, rather than enumerating what may sit there: the CLI "
            "accepts '-v'/'--verbose' and '--no-jail' before a subcommand, a shell redirection "
            "is legal anywhere in a simple command, and $IFS is a word separator too, so an "
            "allow-list of interlopers would need extending on each new spelling. A single '&' "
            "is allowed through because '2>&1' is a redirection, while '&&' still ends the "
            "match: the three words have to belong to ONE simple command."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-gateway-restart",
        pattern=".*kiro.?crew(?:\\s+--?[a-z-]+(?:[= ]\\S+)?)*\\s+gateway restart.*",
        category="self-protection",
        description=(
            "Blocks 'kirocrew gateway restart' so the agent cannot bounce its own gateway "
            "server and interrupt the active session or supervision."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-kill",
        # Scoped to the KILL TARGET, not to co-occurrence anywhere in the command.
        # The alternation is wrapped in a non-capturing group deliberately: a
        # TOP-LEVEL ``|`` fails ``is_safe_user_regex``, which would DISABLE this
        # rule outright (``_DenyMatcher`` skips unsafe patterns) rather than
        # narrow it.
        #
        # Each gap stops at a command separator (``; &``), a comment (``#``) or a
        # redirect (``>``), which is what the false positives this replaced always
        # crossed -- a bare ``kill <pid>`` followed by an unrelated command that
        # merely mentions the product, or a trailing comment naming it.  ``|`` and
        # ``/`` stay INSIDE the gap on purpose: ``pkill -f 'x|kirocrew'`` and
        # ``pkill -f /usr/local/bin/kirocrew`` are both real by-name kills, and
        # treating those characters as boundaries would let them through.
        pattern=(
            "(?:"
            # pkill/killall select processes BY NAME, so the product name as an
            # argument in the same segment IS the kill target.  The verb must be in
            # COMMAND POSITION -- start of input or after a separator, optionally
            # quoted or path-qualified -- so the word merely appearing in another
            # command's arguments (``echo pkill kirocrew``) is not a kill.
            "(?:\\A|[;&|\\n`]|\\$\\()[\\s\"'(]*[\\w.:/\\\\-]*"
            "(?:pkill|killall)\\b[^;&#>]*\\bkiro[-.]?crew\\b"
            # Bare ``kill`` takes PIDs, so it can only aim at the product through
            # a command substitution that resolves the name to one.  The gap after
            # the opener is deliberately NOT stopped at ``)``: a nested
            # substitution (``$(pgrep -f "$(printf '')kirocrew")``) closes an inner
            # paren first, and stopping there would let that form through.
            "|(?:\\A|[;&|\\n`]|\\$\\()[\\s\"'(]*[\\w.:/\\\\-]*"
            "kill\\b[^;&#>]*(?:\\$\\(|`)[^;&#>]*\\bkiro[-.]?crew\\b"
            ")"
        ),
        category="self-protection",
        description=(
            "Blocks pkill/killall naming a kirocrew process, and a bare kill whose PID comes "
            "from a command substitution that resolves the kirocrew name, so the agent cannot "
            "terminate its own gateway or supervisor and disable the controls governing it. "
            "Scoped to the kill target within one command segment: an incidental mention of the "
            "product in a later command or a comment (a file being restored, a log path) is not "
            "a kill."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-dev-mode-out-of-root-confirm",
        # Two tiers, because neither alone closes the class.  The regex tier
        # matches the flag as RAW TEXT, which covers the direct CLI form and
        # nested shell payloads (``bash -c "kirocrew app dev x --confirm-…"``)
        # — but raw text is exactly what quote-splitting defeats:
        # ``--confirm-out-of-install-'root'`` reaches argparse as the accepted
        # flag while the raw command never contains the literal.  The paired
        # argv floor (``_is_dev_mode_out_of_root_confirm``, always run by
        # ``is_denied`` while this rule is enabled) therefore re-checks the
        # DE-ESCAPED text and every tokenized argv frame, so enforcement
        # evaluates what the shell actually executes.  Deliberately broad
        # (same posture as the printenv-AWS rule): the flag is the operator's
        # out-of-install dev-mode attestation, and an agent command that
        # merely *mentions* it is at best editing security-sensitive surface,
        # which a human should drive.
        pattern=".*--confirm-out-of-install-root.*",
        category="self-protection",
        description=(
            "Blocks any agent command carrying the `--confirm-out-of-install-root` flag, "
            "the operator's explicit attestation for granting app dev mode on a UI root "
            "outside the app's install directory. The dev-mode grant relaxes the "
            "unauthenticated UI route's root containment, so an agent able to pass the "
            "flag itself would convert an auto-approved shell into a self-granted serving "
            "grant on an arbitrary host directory. The confirmation must come from the "
            "operator's own terminal, which these rules do not govern."
        ),
    ),
    # ── Legacy security.py deny globs (converted to regex) ──
    # These predate the agent-config ``deniedCommands`` list and were NOT part
    # of it, so they are not in the 130 ported patterns.  They cover explicit
    # secret-fetching tool names and the boto3 UNDERSCORE spellings of
    # destructive AWS calls (``client.delete_stack(...)``) that the hyphenated
    # CLI rules above do not match.  ``is_denied`` (notably the ``mcp_cron``
    # command path) relies on these to block prompt-injected destructive shell.
    # ``.*foo.*`` is the re.search equivalent of the old ``*foo*`` glob.
    DeniedCommandRule(
        id="legacy-get-secret",
        pattern="get_secret.*",
        category="credential-exfil",
        description=(
            "Blocks explicit secret-fetching tool names such as `get_secret_value`, which "
            "read credential material that could be exfiltrated."
        ),
    ),
    DeniedCommandRule(
        id="legacy-read-secret",
        pattern="read_secret.*",
        category="credential-exfil",
        description=(
            "Blocks explicit secret-reading tool names such as `read_secret`, which read "
            "credential material that could be exfiltrated."
        ),
    ),
    DeniedCommandRule(
        id="legacy-delete-stack-underscore",
        pattern=".*delete_stack.*",
        category="aws-destructive",
        description=(
            "Blocks the boto3 underscore form `delete_stack`, which destroys a CloudFormation "
            "stack and every resource it manages."
        ),
    ),
    DeniedCommandRule(
        id="legacy-terminate-instance-underscore",
        pattern=".*terminate_instance.*",
        category="aws-destructive",
        description=(
            "Blocks the boto3 underscore form `terminate_instance(s)`, which permanently shuts "
            "down and deletes running EC2 instances."
        ),
    ),
    DeniedCommandRule(
        id="legacy-drop-table-underscore",
        pattern=".*drop_table.*",
        category="sql",
        description=(
            "Blocks the underscore form `drop_table`, which permanently deletes a database "
            "table and all its rows."
        ),
    ),
    DeniedCommandRule(
        id="legacy-delete-table-underscore",
        pattern=".*delete_table.*",
        category="aws-destructive",
        description=(
            "Blocks the boto3 underscore form `delete_table`, which permanently deletes a "
            "DynamoDB table and every item it holds."
        ),
    ),
    DeniedCommandRule(
        id="legacy-delete-bucket-underscore",
        pattern=".*delete_bucket.*",
        category="aws-destructive",
        description=(
            "Blocks the boto3 underscore form `delete_bucket`, which removes an S3 bucket and "
            "can cause irreversible data loss."
        ),
    ),
]

_RULES_BY_ID: dict[str, DeniedCommandRule] = {r.id: r for r in BUILTIN_DENIED_RULES}

# Reverse map (pattern → rule id) for SEL audit enrichment on a regex-tier match.
_RULE_ID_BY_PATTERN: dict[str, str] = {r.pattern: r.id for r in BUILTIN_DENIED_RULES}

# Legacy spellings of rules whose patterns were later widened (#4799).  A
# governance policy persists the pattern STRING it pinned, and the pin resolvers
# treat a pattern as pinning a built-in rule only when it maps back to a rule id
# — so a ceiling or profile written against a pre-widening catalog must keep
# resolving to the rule id after an upgrade (upgrade monotonicity).  Without
# these aliases a stale pin falls out of the id map: the force-re-add is lost
# and a user opt-out would drop the rule even though the administrator pinned
# it.  LOOKUP-ONLY: consulted by :func:`_rule_id_for_pattern` (the pin
# resolvers), never merged into ``_RULE_ID_BY_PATTERN`` — the legacy spellings
# must not count as built-ins for ``_DenyMatcher``'s fast-path election or SEL
# enrichment, and they never enter ``BUILTIN_DENY_PATTERNS`` or the golden
# manifest.
_LEGACY_RULE_ID_BY_PATTERN: dict[str, str] = {
    ".*kiro.?crew restart.*": "self-protection-restart",
    ".*kiro.?crew update.*": "self-protection-update",
    ".*kiro.?crew\\s+cloud\\s+(destroy|stop|start|launch|connect|tunnel|log(in|out)).*": (
        "self-protection-cloud"
    ),
    ".*kiro.?crew gateway restart.*": "self-protection-gateway-restart",
}


def _rule_id_for_pattern(pattern: str) -> "str | None":
    """Resolve a governance-pinned pattern string to a built-in rule id.

    Current catalog spellings first, then the legacy (pre-widening) spellings,
    so a persisted policy keeps its pin across a pattern change.
    """
    return _RULE_ID_BY_PATTERN.get(pattern) or _LEGACY_RULE_ID_BY_PATTERN.get(pattern)


# ── Git-publish rule patterns are NOT evaluated in the Python regex tier ──
# The ``git-publish`` category rules exist in the catalog for UI display /
# opt-out parity, but git-publish enforcement is done UNCONDITIONALLY by the
# verb-anchored ``_is_git_publish`` / ``_is_push_to_protected_branch`` floor
# (evaluated BEFORE the tiers below).  Their patterns were authored for
# kiro-cli's linear-time (RE2-style) engine; under Python's backtracking
# ``re`` the nested ``(?:...)*`` quantifiers are catastrophic (ReDoS) on
# pathological flag-spam input, so they must never reach ``re.search``.  The
# always-on floor already covers every case these patterns would (protected
# targets denied, feature branches allowed), so skipping them loses no coverage.
_GIT_PUBLISH_RULE_CATEGORY = "git-publish"
# Single filtered view of the catalog so the pattern set and the id set below
# cannot drift apart (both must cover exactly the git-publish rules).
_GIT_PUBLISH_RULES: tuple[DeniedCommandRule, ...] = tuple(
    r for r in BUILTIN_DENIED_RULES if r.category == _GIT_PUBLISH_RULE_CATEGORY
)
_GIT_PUBLISH_RULE_PATTERNS: frozenset[str] = frozenset(r.pattern for r in _GIT_PUBLISH_RULES)

# Tag returned by ``_git_publish_floor_tags`` for the anti-obfuscation branches
# (substitution glue, unparseable push, no clean push segment). Deliberately not
# a rule id: these are what make the gated rules non-bypassable, so no opt-out
# may reach them. The leading NUL-ish sentinel shape cannot collide with a slug.
_GIT_PUBLISH_UNGATED = "\x00git-publish-unverifiable"

# ``id -> pattern`` for the git-publish rules, so a floor denial can report the
# rule's own pattern (as the regex tier does) instead of an opaque label. Before
# this, a git-publish denial reported the human string "git push" and mapped back
# to NO rule id in the SEL audit trail.
_GIT_PUBLISH_FLOOR_BY_ID: dict[str, str] = {r.id: r.pattern for r in _GIT_PUBLISH_RULES}

# Why a git-publish floor denial happened, in words. Same role as
# ``_SELF_PROTECTION_FLOOR_NOTES``: the floor routinely fires on input the
# catalog pattern does NOT literally match, because these patterns are kept out
# of ``re`` entirely (ReDoS) and the verb-anchored floor is the only enforcement.
# Presentation-only SECOND line — ``RecoveryCard.tsx`` parses the pattern from
# the first line with a per-line end-anchored regex.
_GIT_PUBLISH_FLOOR_NOTES: dict[str, str] = {
    "git-publish-push-bare": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "the push names no branch, so it publishes whatever branch is checked out."
    ),
    "git-publish-push-single-arg": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "the push names a remote but no branch, so it publishes to the configured upstream."
    ),
    "git-publish-push-protected-branch-name": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "a refspec resolves to a protected branch after shell quoting is collapsed."
    ),
    "git-publish-push-protected-ref-path": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "a refs/heads, heads/ or remotes/ ref path resolves to a protected branch."
    ),
    "git-publish-push-wildcard-refspec": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "a wildcard refspec expands to many refs, which can include a protected branch."
    ),
    "git-publish-push-mirror-all": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "--mirror/--all push every local ref regardless of any explicit refspec."
    ),
    "git-publish-push-ambiguous-ref": (
        "Matched structurally on the parsed push arguments, not by the pattern text above: "
        "the destination is a symbolic ref that only resolves when the push runs."
    ),
}

# The one git-publish rule whose coverage is an UNGATED branch: brace expansion
# is caught by ``_AMBIGUOUS_EXPANSION_RE`` inside the unverifiable-glue check, so
# disabling this row would change nothing and the Settings surface must keep
# rendering it locked (see ``floor_enforced_builtin_command_ids``).
_GIT_PUBLISH_UNGATED_RULE_IDS: frozenset[str] = frozenset(
    {"git-publish-push-brace-expansion-refspec"}
)

# Catalog rules whose ENFORCEMENT is an always-on floor rather than the
# configurable regex tier.  Derived from the category (never a hand-maintained
# id list) so a future git-publish rule is covered automatically.
_FLOOR_ENFORCED_RULE_IDS: frozenset[str] = _GIT_PUBLISH_UNGATED_RULE_IDS


def floor_enforced_builtin_command_ids() -> frozenset[str]:
    """Built-in rule ids enforced by an always-on floor (not opt-out-able).

    These rules exist in the catalog for display parity, but their enforcement
    is the unconditional verb-anchored git-publish floor (``_is_git_publish`` /
    ``_is_push_to_protected_branch``) evaluated before the configurable tiers,
    which consults no opt-out state.  Persisting one of these ids into
    ``disabled_ids`` therefore changes nothing — the Settings surface must
    render them locked/forced-on and the toggle API must reject a disable, or
    the opt-out is a silent no-op (UI reports success, the floor still denies).

    DISPLAY/API accessor only: nothing in the enforcement path reads it, so it
    cannot weaken the floor.  Pure and deterministic (module-scope derivation
    from the catalog category), safe to call from any thread.
    """
    return _FLOOR_ENFORCED_RULE_IDS


# Self-protection rules that get the argv-structural floor (``_self_token_frames``
# -> a per-rule predicate), which sees the de-escaped, de-quoted argv that the
# raw-text regex tier cannot. Two enforcement stories share the mechanism:
#   * credential-mint / self-kill: floor-PRIMARY. Their catalog ``pattern`` is a
#     human-auditable SUBSET; a raw-string match cannot resolve shell quoting or
#     redirection, and a pattern loose enough to try would re-block ordinary
#     paths, so the floor carries enforcement.
#   * restart / update / gateway restart / cloud <destructive>: regex+floor UNION.
#     The widened regex (#4799) catches the real-flag and raw-text forms (incl.
#     ``bash -c`` payloads and the ``python -m kiro_crew`` module form); the floor
#     (#4824) additionally catches shell de-escaping the regex cannot -- e.g.
#     ``kirocrew -\v restart``, ``kirocrew \restart``, a ``\<newline>`` continuation.
# All members stay in the regex tier (only git-publish is removed from ``re``);
# the floor is a union with it, never a replacement.
_SELF_PROTECTION_FLOOR_RULE_IDS: frozenset[str] = frozenset(
    {
        "credential-exfil-kirocrew-token",
        "self-protection-kill",
        "self-protection-restart",
        "self-protection-update",
        "self-protection-gateway-restart",
        "self-protection-cloud",
        "self-protection-dev-mode-out-of-root-confirm",
    }
)
_SELF_PROTECTION_FLOOR_BY_ID: dict[str, str] = {
    r.id: r.pattern for r in BUILTIN_DENIED_RULES if r.id in _SELF_PROTECTION_FLOOR_RULE_IDS
}
_SELF_PROTECTION_FLOOR_PATTERNS: frozenset[str] = frozenset(_SELF_PROTECTION_FLOOR_BY_ID.values())

# Why a floor denial happened, in words, for the rules whose floor can fire on
# input the catalog ``pattern`` provably does NOT match.
#
# The refusal's first line reports that pattern (see the floor branch in
# ``is_denied``) so the reason and the SEL event still map back to a rule id.
# That identifier is not an explanation, though, and for a floor hit it is a
# misleading one: ``python -c "import kiro_crew"`` is denied by the argv floor,
# while the pattern it names requires a ``token`` word the command does not
# contain. A reader who trusts the line looks for the wrong thing — and the
# refusal reason is now handed to the MODEL in-band on a tool deny
# (``chat_runner._steer_policy_notice``), so a wrong explanation actively
# misdirects the agent's next attempt rather than merely reading oddly in a log.
#
# Presentation only, on the refusal's SECOND line, which both consumers ignore:
# ``RecoveryCard.tsx`` extracts the pattern with a per-line end-anchored regex
# and the suite's ``_denied_by`` partitions on the first line's separator.
_SELF_PROTECTION_FLOOR_NOTES: dict[str, str] = {
    "credential-exfil-kirocrew-token": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "the product CLI is invoked to mint a dashboard token, or an inline "
        "interpreter program imports it (an imported CLI can construct the token verb "
        "itself, so the import is the gate and no 'token' word need appear)."
    ),
    "self-protection-kill": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "the command signals or kills this gateway's own process."
    ),
    "self-protection-restart": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "shell de-escaping resolves the command to a restart of this gateway."
    ),
    "self-protection-update": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "shell de-escaping resolves the command to a self-update."
    ),
    "self-protection-gateway-restart": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "shell de-escaping resolves the command to a gateway restart."
    ),
    "self-protection-cloud": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "shell de-escaping resolves the command to a destructive cloud operation."
    ),
    "self-protection-dev-mode-out-of-root-confirm": (
        "Matched structurally on the command's argv, not by the pattern text above: "
        "shell de-escaping resolves an argument to the operator's "
        "`--confirm-out-of-install-root` attestation flag, which agent commands "
        "may never carry."
    ),
}

# The two INTERPRETER-payload rules.  They are ordinary regex-tier rules, but an
# interpreter CONCATENATES adjacent string literals, so they are additionally matched
# against a copy of the text with those joins collapsed.
_INTERPRETER_RULE_IDS: frozenset[str] = frozenset(
    {"credential-exfil-kirocrew-token-argv", "self-protection-kill-interpreter"}
)
_INTERPRETER_RULE_PATTERNS: frozenset[str] = frozenset(
    r.pattern for r in BUILTIN_DENIED_RULES if r.id in _INTERPRETER_RULE_IDS
)
# ``'p' + 'kill'`` is ONE string by the time the interpreter runs it.
_LITERAL_CONCAT_RE = re.compile(r"""['"]\s*\+\s*['"]""")

# ── Back-compat alias ──
# Retained as a DERIVED flat string list so ``platform/security_authority`` and
# ``cli_commands`` keep importing a ``list[str]``.  Its members are now REGEX
# strings (string identity only — the match semantics moved to ``re.search``).
BUILTIN_DENY_PATTERNS: list[str] = [r.pattern for r in BUILTIN_DENIED_RULES]


def compute_effective_denied(
    rules: "list[DeniedCommandRule]",
    disabled_ids: "Iterable[str]",
    disable_all: bool,
    user_added: "Iterable[str]",
    governance_pins: "Iterable[str]",
) -> list[str]:
    """Resolve the effective regex-tier deny list (pure, deterministic).

    Returns the ordered, de-duplicated list of REGEX strings to enforce:

    1. For each rule in ``rules`` (input order), include ``rule.pattern`` if
       ``(not disable_all and rule.id not in disabled_ids) or rule.id in
       governance_pins``.  A governance pin re-adds a rule even when the user
       individually disabled it OR set disable-all — tightest-wins: an
       enterprise pin cannot be opted out.
    2. Append every entry of ``user_added`` verbatim (the user's own regexes).
    3. De-duplicate preserving first-seen order.

    No I/O, no config reads, no globals mutated — callers (the hooks gate) own
    where ``disabled_ids`` / ``disable_all`` / ``user_added`` / ``governance_pins``
    come from.
    """
    disabled = set(disabled_ids)
    pins = set(governance_pins)
    out: list[str] = []
    for rule in rules:
        if (not disable_all and rule.id not in disabled) or rule.id in pins:
            out.append(rule.pattern)
    out.extend(user_added)
    return list(dict.fromkeys(out))


def enabled_rule_ids(denied_regexes: "list[str] | None") -> "frozenset[str] | None":
    """Resolve an effective REGEX list to the set of enabled built-in rule ids.

    The always-on gates (``audit_bash_exfiltration``, ``_check_imds_access``,
    ``is_sensitive_path_for_agent``) are keyed by rule id, while the hooks gate
    holds the effective set as patterns. This is the one translation, done once
    per tool call.

    ``None`` in, ``None`` out — and ``None`` means "all enabled" to every consumer,
    so the fail-closed default survives the round trip. A pattern with no catalog
    id (a user-added regex) contributes nothing, which is correct: those rules have
    no always-on branch to gate.
    """
    if denied_regexes is None:
        return None
    ids = {_RULE_ID_BY_PATTERN.get(p) for p in denied_regexes}
    return frozenset(rid for rid in ids if rid is not None)


def builtin_denied_rules() -> list[dict]:
    """Return the built-in rule catalog as plain dicts for API serialization.

    Each entry has exactly ``{id, pattern, category, description}``.  Handlers
    consume this so they never need to import the ``DeniedCommandRule`` dataclass.
    """
    return [
        {
            "id": r.id,
            "pattern": r.pattern,
            "category": r.category,
            "description": r.description,
        }
        for r in BUILTIN_DENIED_RULES
    ]


def edition_denied_rules() -> list[DeniedCommandRule]:
    """Denied-command rules contributed by the composed edition, validated.

    Reads ``current_context().denied_rules.denied_rules()`` (the
    ``DeniedRuleProvider`` seam) and returns only entries safe to union into the
    DISABLEABLE regex tier.  Unlike the ``SecurityOverlay`` floor these rules ARE
    user-disableable — the whole point of the seam — so they are resolved by
    ``compute_effective_denied`` exactly like a built-in and honour
    ``disabled_ids`` / ``disable_all``.

    Rejected (skipped with a warning, never raised):

    * a non-:class:`DeniedCommandRule` entry, or one with a blank ``id`` /
      ``pattern`` — there would be nothing to key an opt-out on;
    * an ``id`` colliding with a BUILT-IN rule id — ``disabled_ids`` is one flat
      set, so a collision would make one rule's toggle silently move the other.
      The built-in wins, mirroring the ADD-only de-dupe the skill-discovery seam
      uses for provider names;
    * a duplicate ``id`` within the edition's own list (first occurrence wins).

    Fail-soft: an ungoverned/standalone host, a provider that does not implement the
    protocol, or one that raises all yield ``[]`` — the built-in catalog stands on
    its own and the un-weakenable overlay floor is untouched either way, so
    degrading here loses only an additive rule.  ``PlatformCompositionError``
    still propagates fail-closed, as everywhere else in this module.
    """
    # Function-local by necessity, not style: importing ``kiro_crew.platform.context``
    # at module scope executes ``kiro_crew.platform.__init__``, which imports
    # ``platform.security_authority``, which imports THIS module — a genuine cycle.
    # The pre-existing local import in ``installed_context``'s caller below has the
    # same cause. (``top-level-imports``, documented exception; GPT 5.6 asked for
    # this note on #7705.)
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    try:
        ctx = current_context()
        contributed = list(ctx.denied_rules.denied_rules())
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("edition denied_rules lookup failed; using built-ins only", exc_info=True)
        return []

    out: list[DeniedCommandRule] = []
    seen: set[str] = set()
    for rule in contributed:
        rid = getattr(rule, "id", None)
        pattern = getattr(rule, "pattern", None)
        if not isinstance(rid, str) or not rid.strip():
            logger.warning("edition denied rule with no id skipped")
            continue
        if not isinstance(pattern, str) or not pattern.strip():
            logger.warning("edition denied rule %s has no pattern; skipped", rid)
            continue
        if rid in _RULES_BY_ID:
            logger.warning(
                "edition denied rule %s collides with a built-in rule id; built-in wins", rid
            )
            continue
        if rid in seen:
            logger.warning("edition denied rule %s is a duplicate; first occurrence wins", rid)
            continue
        if not is_safe_user_regex(pattern):
            # The matcher DISABLES a malformed or ReDoS-prone pattern and only logs
            # (see ``_DeniedMatcher.__init__``). Publishing it anyway would put a
            # row in Settings → Security that reads enabled and toggles cleanly
            # while matching nothing — a control that looks present and is not,
            # which is the exact failure this seam exists to remove. Skip it so the
            # panel's enabled set and the matcher's cannot disagree.
            logger.warning(
                "edition denied rule %s has an unsafe or malformed pattern; skipped", rid
            )
            continue
        if not _matches_full_input(pattern):
            # The matcher would route this one to the length-bounded window (see
            # ``_DENY_FALLBACK_SCAN_MAX_CHARS``), so padding the command past the
            # cap defeats it. Same reasoning as the check above: a rule that scans
            # only a prefix is bypassable, and publishing it as enforcing would
            # make the panel claim a guarantee the matcher does not give. An
            # edition wanting this pattern rewrites it without a top-level ``.*``
            # (a bounded gap such as ``[^;&|\n]*`` keeps it one fragment), or uses
            # the un-weakenable overlay if it truly needs the loose form.
            logger.warning(
                "edition denied rule %s would only scan the first %d chars "
                "(top-level '.*' or an over-consuming gap); skipped",
                rid,
                _DENY_FALLBACK_SCAN_MAX_CHARS,
            )
            continue
        seen.add(rid)
        out.append(
            DeniedCommandRule(
                id=rid,
                pattern=pattern,
                category=str(getattr(rule, "category", "") or "edition"),
                description=str(getattr(rule, "description", "") or ""),
            )
        )
    return out


def pinned_builtin_command_ids() -> set[str]:
    """Return built-in rule ids force-pinned by the ACTIVE governance ceiling.

    A governance ``commands``-scope deny policy can pin a built-in rule as
    un-opt-out-able.  A pattern is treated as pinning a built-in rule when it is
    string-identical to that rule's regex.

    Scope: the **active** Level-1 ceiling (``current_context().governance``)
    ONLY.  This is the ENFORCEMENT accessor (the hooks gate force-re-adds these
    ids so a user opt-out cannot weaken a ceiling pin, tightest-wins).  It does
    NOT union other profiles' pins — a rule pinned only for profile A must not be
    force-enforced for profile B or a no-profile session (that would break
    profile-scoped governance).  Per-profile command enforcement is handled
    separately by the gate's ``_governance_denial`` commands-scope deny plane,
    which resolves the *bound* profile.  For the surface-agnostic Settings
    snapshot (which must over-lock across all profiles) use
    :func:`pinned_builtin_command_ids_for_snapshot`.

    Fail-soft: returns an empty set on a standalone/ungoverned host or if
    governance resolution fails (mirrors the degrade discipline elsewhere in this
    module; ``PlatformCompositionError`` still propagates fail-closed).
    """
    from kiro_crew.platform import governance as _governance
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    try:
        ceiling = current_context().governance
        if ceiling is None:
            return set()
        # ``resolve_pinned_commands`` is provided by the governance module (a
        # sibling change-set); resolve it dynamically so this module composes
        # regardless of build order.  Missing symbol → no pins (fail-soft).
        resolver = getattr(_governance, "resolve_pinned_commands", None)
        if resolver is None:
            return set()
        pins = resolver(ceiling)
        ids = (_rule_id_for_pattern(p) for p in pins)
        return {rid for rid in ids if rid is not None}
    except PlatformCompositionError:
        raise
    except Exception:
        return set()


def pinned_builtin_command_ids_for_snapshot() -> set[str]:
    """Built-in rule ids pinned by the ceiling OR by ANY loaded profile.

    DISPLAY accessor for the surface-agnostic Settings > Security snapshot, which
    has no session/agent/app to resolve a single *active* profile.  It unions the
    active ceiling pins (:func:`pinned_builtin_command_ids`) with the pins from
    ALL loaded profiles, so a rule pinned by ANY profile renders locked and is
    never presented as freely disableable — otherwise a profile-pinned rule would
    surface as a no-op opt-out (UI reports success, but the bound-profile gate
    still denies).  Conservative by design (over-locks, never under-locks).

    This is DISPLAY-only: the ENFORCEMENT gate uses the ctx-scoped
    :func:`pinned_builtin_command_ids` (active ceiling) + the bound-profile deny
    plane, so unioning all profiles here does NOT widen enforcement.

    Fail-soft like :func:`pinned_builtin_command_ids`.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        ids = pinned_builtin_command_ids()
    except PlatformCompositionError:
        raise
    except Exception:
        ids = set()
    try:
        from kiro_crew.platform.governance_profiles import all_profile_pinned_commands

        for p in all_profile_pinned_commands():
            rid = _rule_id_for_pattern(p)
            if rid is not None:
                ids.add(rid)
    except Exception:
        pass
    return ids


# Exceptions keyed by the deny pattern they apply to. If an input matches
# a deny pattern AND one of that pattern's exceptions, the deny is skipped.
# This avoids a blanket allowlist that could bypass unrelated deny rules.
# Exceptions are NOT applied when the input contains command separators
# (;, &&, ||, |, newlines) to prevent chaining bypasses.
#
# Scoped carve-out for INERT MENTIONS of a destructive literal (see #8802).
#
# A read-only search verb cannot execute its own operands, so a destructive
# string handed to it as a pattern is text, not an action:
#
#     grep -rn "rm -rf /" tests/     <- searching FOR the rule, not running it
#
# Before this carve-out those were denied identically to the real command,
# which prevented nothing (the same work completes by moving the payload into
# a file, which is not scanned -- see the #2660 thread) while blocking anyone
# working ON these rules, and surfaced to the agent as
# ``User denied tool execution`` -- indistinguishable from a human cancelling.
#
# Two conditions make this safe, and BOTH are load-bearing -- an earlier
# revision of this carve-out got each one wrong and re-allowed a real wipe:
#
#   1. The glob must be ANCHORED AT THE VERB. ``fnmatch`` is a full match but
#      ``*`` crosses spaces, so a LEADING ``*`` is an unanchored substring
#      test: ``*/grep *`` matches ``rm -rf / /bin/grep x`` -- a genuine
#      root-wipe with a ``/grep `` fragment anywhere in its operand list --
#      and would exonerate it. Only the bare ``<verb> *`` form is safe, because
#      it forces the segment to BEGIN with the verb. Path-qualified
#      invocations (``/usr/bin/grep ...``) are therefore NOT exonerated: a
#      glob cannot express "the first token's basename is the verb", and
#      losing a denial is a worse outcome than a search that still needs
#      rewording.
#
#   2. The view must contain NO shell-active character at all, enforced by
#      :func:`_exception_eligible`. An earlier revision blocklisted the opener
#      it knew about (``(``) and was promptly defeated by the next one -- a
#      bash 5.3 funsub, ``grep x ${ rm -rf /;}``, which the splitter cuts only
#      at the ``;`` so the destructive command stays glued to the search verb.
#      Enumerating opener SPELLINGS is the losing side of that game (#8074
#      makes the same point about a spelling-based recognizer), so the guard is
#      inverted: an eligible view may contain none of ``$`` ``(`` ``)`` ``{``
#      ``}`` `` ` `` ``<`` ``>``. That covers command substitution, process
#      substitution, funsubs, subshells and redirection as a CLASS rather than
#      one opener at a time. Gating the EXCEPTION rather than widening
#      ``_CMD_SPLIT_RE`` keeps the blast radius to this carve-out; the splitter
#      feeds every other rule, which were measured against its behaviour.
#
# The verb list is confined to the ``grep`` family for the same reason. The
# premise of this whole carve-out is that the verb CANNOT execute its operands,
# and that is a property of the specific tool: ``rg --pre <cmd>`` runs a
# preprocessor and ``ack --pager <cmd>`` runs a pager, so
# ``rg --pre sh "rm -rf /tmp/victim" payload.sh`` really does execute. ``grep``
# / ``egrep`` / ``fgrep`` have no flag that spawns a helper, so for them the
# premise holds rather than merely being asserted.
#
# With all of this in force the chaining cases stay denied on the destructive
# SEGMENT in Pass 2 (``grep "x" && <destructive>``); the Pass 1 whole-string
# exception only ever defers to that pass.
#
# Because nothing in an eligible segment executes, whether the literal was
# quoted is irrelevant -- so this needs no quote awareness, which is what keeps
# it compatible with #7013 (quote-NORMALIZED matching, added to close evasion:
# quoting must never exculpate a command that does run).
#
# Deliberately NOT included, pending the maintainer decision asked for on
# #8802: ``echo``/``printf`` (inert to execute, but ``>`` is not a segment
# separator, so an exoneration there also covers writing the string to a file)
# and ``git commit -m`` (the arguable non-search verb).
_INERT_SEARCH_VERBS = ("grep", "egrep", "fgrep")
#: Verb-anchored ONLY -- see condition 1 above. A leading ``*`` here would be a
#: bypass, not a convenience.
_INERT_SEARCH_GLOBS: list[str] = [f"{verb} *" for verb in _INERT_SEARCH_VERBS]

#: Any one of these in a view means it is not a single plain command: it can
#: open a command, an expansion or a redirection, or chain to another command.
#: An ALLOWLIST of inert text, not a blocklist of opener spellings -- see
#: condition 2.  The separators are here for the PASS 1 view specifically: a
#: Pass 2 segment never contains one (the splitter consumed it), but the
#: whole-string view does, and an exception must not speak for a compound
#: command whose later stage is an interpreter
#: (``grep '<destructive>' payload.py | python``).
_SHELL_ACTIVE_CHARS = frozenset("$`(){}<>|;&\n\r")


def _exception_eligible(view: str) -> bool:
    """Whether a deny-exception may be consulted for ``view`` at all.

    An eligible view must be a SINGLE PLAIN COMMAND: no character from
    :data:`_SHELL_ACTIVE_CHARS`, which covers command substitution, process
    substitution, funsubs, subshells, redirection AND chaining.

    Two independent reasons, each of which was a reachable bypass during review:

    * ``_CMD_SPLIT_RE`` is not a complete execution-boundary oracle -- it does
      not treat ``<(``, ``>(``, ``${`` or a bare ``(`` as a boundary -- so such a
      construct stays glued INSIDE a segment instead of being isolated into its
      own command position, and an exception keyed to the segment's leading verb
      would exonerate the command hiding in it (``grep x <(rm -rf /tmp/v)``,
      ``grep x ${ rm -rf /;}``).
    * A pipeline's later stage can EXECUTE what the search emitted
      (``grep '<destructive>' payload.py | python``).  The pipe is a splitter
      boundary, so the Pass 2 grep segment looks innocent on its own; refusing
      the separators here keeps the Pass 1 whole-string match denying outright
      instead of deferring to that segment.

    Deliberately a character-class test rather than a list of opener spellings:
    the spelling list lost twice during review, once to ``<(`` and once to a
    bash 5.3 funsub.

    Fails CLOSED: an unrecognised construct means no exception, i.e. the deny
    stands.  This gates only the exception path, so no other rule's matching
    behaviour changes.
    """
    return not _SHELL_ACTIVE_CHARS.intersection(view)


# Maps a deny pattern to the globs that exonerate it.  When an input matches
# a deny pattern AND one of that pattern's exceptions, the deny is skipped.
# This avoids a blanket allowlist that could bypass unrelated deny rules.
#
# Scoped to the two ``local-destructive`` rm rules: they are plain literal
# strings, so they are the ones an ordinary search for their own subject
# matter trips over.
_DENY_EXCEPTIONS: dict[str, list[str]] = {
    "rm -rf /.*": list(_INERT_SEARCH_GLOBS),
    "rm -rf ~.*": list(_INERT_SEARCH_GLOBS),
}

# Used to *split* a command into independently-evaluatable segments.
# Splits on every shell separator that can chain commands or carve out a
# subshell:
#   ;  - sequential
#   |  - pipe (single)
#   || - OR
#   && - AND
#   &  - background operator (when not part of `&&`)
#   $( - subshell open
#   )  - subshell close
#   `  - backtick subshell (open AND close)
#   \n - statement separator in scripts / heredoc bodies
# The alternation is ordered so the multi-character forms (`&&`, `||`) are
# tried before their single-character counterparts (`&`, `|`).  The
# negative lookahead on `&(?!&)` is defensive — it ensures a lone `&`
# doesn't accidentally consume the leading `&` of a literal `&&` if the
# regex engine chose this branch first under some future reordering.
# Literal whitespace is NOT a separator — flag values (e.g. `-C /path`)
# must stay attached to their flag token.
_CMD_SPLIT_RE = re.compile(r"[;\n`]|\|\|?|&&|&(?!&)|\$\(|\)")

# ── ReDoS mitigation for the regex deny tier ──
# The 137 built-in rule patterns were authored for kiro-cli's linear-time
# (RE2-style) engine.  Under Python's backtracking ``re`` two independent
# pathologies appear on hostile input, so the raw patterns must never be fed to
# ``re.search`` verbatim.  ``_DenyMatcher`` compiles each pattern into a
# behaviourally-identical but linear-time matcher and matches against the FULL
# (untruncated) string, so a destructive needle at any offset is always found.
# All of this is EVALUATION-LAYER only — ``BUILTIN_DENIED_RULES`` (and the
# golden fixture the parity test pins to) stay byte-for-byte unchanged, and the
# human-readable denial reason / SEL audit still report the ORIGINAL pattern.
#
# Pathology 1 — catastrophic (exponential) backtracking.
#   The 46 ``aws-*`` patterns embed the nested-star flag run
#   ``(?:\s+--?[a-z-]+(?:[= ]\S+)?)*``.  Two internal ambiguities make it
#   exponential: (a) ``--?`` and ``[a-z-]+`` can both claim the leading dashes
#   of a flag; (b) a space-separated value ``[= ]\S+`` can equally be read as
#   the next flag.  On input like ``aws -x -x -x …`` (only ~40 repeats / ~124
#   chars) the engine explores 2ⁿ parses before failing — a length bound does
#   NOT help because the blow-up happens well below any sane bound.  We rewrite
#   the run to ``_LINEARIZED_AWS_FLAG_RUN`` which removes BOTH ambiguities
#   (``--?``→``-`` for the flag name, and a negative lookahead so a space value
#   cannot itself be a flag token).  This is provably language-equivalent — see
#   ``test_denied_commands_security`` ReDoS tests and the exhaustive
#   brute-force/directional equivalence checks documented there.
#
# Pathology 2 — polynomial (O(n²)/O(n³)) backtracking.
#   Every ``.*``-prefixed pattern (~50 of them) and the multi-``.*`` chains
#   (e.g. ``python.*open.*/\.ssh/``) are linear/polynomial per pattern but scan
#   the whole string, so across ~123 effective patterns a 20k-char input costs
#   seconds.  These ``.*`` occur ONLY at the TOP LEVEL of the ported patterns
#   (none has a top-level alternation or a top-level ``.+``), so we SPLIT each
#   pattern on its top-level ``.*`` into fixed fragments and existence-match
#   them in order with a monotonically-advancing ``re.search(text, pos)``.  A
#   top-level ``.*`` matches "anything", so "fragment₀ then fragment₁ then …"
#   at leftmost advancing positions is exactly equivalent to the whole regex —
#   verified by exhaustive brute-force + a 40k-input equivalence harness — but
#   runs in O(n) with NO backtracking across the gaps and NO length bound, so a
#   padded needle inside a single un-separated segment (the bypass this fixes)
#   is still caught.  A pattern that is NOT safe to split this way (a top-level
#   alternation, only possible via a user-supplied custom regex — no built-in
#   has one) falls back to a length-bounded ``re.search`` on the linearized
#   form (``_DENY_FALLBACK_SCAN_MAX_CHARS``): correct for short commands and
#   ReDoS-safe, at the cost of not scanning a needle past the bound in such an
#   exotic custom pattern (built-ins are unaffected).
_DENY_FALLBACK_SCAN_MAX_CHARS = 2000

# The dangerous nested-star flag run as it appears (raw) in the aws-* patterns.
_DANGEROUS_AWS_FLAG_RUN = r"(?:\s+--?[a-z-]+(?:[= ]\S+)?)*"
# Linear, language-equivalent replacement (see Pathology 1 above).
_LINEARIZED_AWS_FLAG_RUN = r"(?:\s+-[a-z-]+(?:=\S+| (?!-[a-z-]+(?:[= ]|$))\S+)?)*"


def _linearize_deny_pattern(pattern: str) -> str:
    """Rewrite the exponential aws flag-run into its linear-time equivalent.

    Pure / idempotent.  Only touches Pathology 1 (the nested-star flag run);
    the top-level ``.*`` gaps are handled structurally by ``_split_deny_frags``.
    """
    return pattern.replace(_DANGEROUS_AWS_FLAG_RUN, _LINEARIZED_AWS_FLAG_RUN)


# ── ReDoS-safety gate for USER-supplied deny regexes ──
# The 137 built-in patterns are ReDoS-safe by construction (the one dangerous
# construct — the aws flag run — is rewritten by ``_linearize_deny_pattern``,
# and the git-publish patterns never reach the regex tier).  But a USER can add
# an ARBITRARY regex via ``POST /api/security/denied-commands/user``; a
# catastrophic-backtracking pattern such as ``(a+)+$`` would then run inside the
# synchronous PreToolUse gate on the event loop and could freeze the gateway
# (2ⁿ backtracking is NOT bounded by scanning a length-limited prefix — the
# blow-up happens far below any byte bound).  ``is_safe_user_regex`` is a
# conservative, stdlib-only STRUCTURAL check used both at the add boundary
# (reject with HTTP 400) and as runtime defense-in-depth in ``_DenyMatcher``
# (an already-stored unsafe pattern is skipped, never executed).
#
# Heuristic (the classic exponential family): a pattern is UNSAFE if it contains
# a QUANTIFIED GROUP — one whose quantifier permits >1 repetitions (``*``,
# ``+``, ``{m,}``, ``{m,n}`` with n>1, ``{n}`` with n>1) — whose body itself
# contains EITHER (a) another quantifier (nested quantifier: ``(X+)+``,
# ``(X*)*``, ``(X?)*`` …) OR (b) a top-level alternation (branch-overlap risk:
# ``(a|a)+``, ``(ab|a)+``).  We deliberately err toward REJECTING a suspicious
# user pattern: built-ins are unaffected (they are added programmatically, never
# through this gate), and a user who hits a false positive can rephrase without
# the nested quantifier.  We first strip the known-safe linearized aws flag run
# so the (harmless) built-in construct is never mistaken for the dangerous
# signature if this ever runs over the effective set.


def _redos_prone(pattern: str) -> bool:
    """Structural exponential-ReDoS heuristic (see the section comment above).

    Robust to malformed / unbalanced input — never raises; returns ``False`` for
    a structure it cannot reason about (``re.compile`` is validated separately by
    callers, and the runtime fallback is length-bounded regardless).
    """
    n = len(pattern)
    i = 0
    # One frame per open group; base frame is the whole pattern.
    stack: list[dict] = [{"has_inner_quant": False, "has_alt": False}]

    def read_quantifier(idx: int) -> "tuple[str | None, int]":
        """Return (kind, new_idx): kind is ``"multi"`` (>1 repetitions possible),
        ``"opt"`` (``?`` or ``{0,1}``), or ``None`` (no quantifier at ``idx``)."""
        if idx >= n:
            return None, idx
        ch = pattern[idx]
        if ch in "*+":
            j = idx + 1
            if j < n and pattern[j] in "?+":  # lazy / possessive-style modifier
                j += 1
            return "multi", j
        if ch == "?":
            j = idx + 1
            if j < n and pattern[j] in "?+":
                j += 1
            return "opt", j
        if ch == "{":
            k = idx + 1
            body: list[str] = []
            while k < n and pattern[k] != "}":
                body.append(pattern[k])
                k += 1
            if k >= n:  # unterminated ``{`` — treat as a literal, no quantifier
                return None, idx + 1
            k += 1  # consume ``}``
            spec = "".join(body)
            if "," in spec:
                _, _, hi = spec.partition(",")
                hi = hi.strip()
                multi = hi == "" or not hi.isdigit() or int(hi) > 1
            else:
                multi = not spec.isdigit() or int(spec) > 1
            return ("multi" if multi else "opt"), k
        return None, idx

    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            kind, i = read_quantifier(i)
            if kind is not None:
                stack[-1]["has_inner_quant"] = True
            continue
        if c == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                if pattern[i] == "\\":
                    i += 1
                i += 1
            i += 1  # consume ``]``
            kind, i = read_quantifier(i)
            if kind is not None:
                stack[-1]["has_inner_quant"] = True
            continue
        if c == "(":
            i += 1
            if i < n and pattern[i] == "?":
                nxt = pattern[i + 1] if i + 1 < n else ""
                if nxt in "=!":  # (?= (?! lookahead — a normal group frame
                    i += 2
                elif nxt == "<" and i + 2 < n and pattern[i + 2] in "=!":
                    i += 3  # (?<= (?<! lookbehind
                else:
                    # (?: , (?i: , (?P<name> … — skip the prefix up to ``:``/``>``
                    j = i + 1
                    while j < n and pattern[j] not in ":>)":
                        j += 1
                    i = j + 1 if j < n and pattern[j] in ":>" else j
            stack.append({"has_inner_quant": False, "has_alt": False})
            continue
        if c == ")":
            grp = stack.pop() if len(stack) > 1 else {"has_inner_quant": False, "has_alt": False}
            i += 1
            kind, i = read_quantifier(i)
            if kind == "multi" and (grp["has_inner_quant"] or grp["has_alt"]):
                return True
            if kind is not None:
                # A quantified group is itself a quantifier in the parent frame.
                stack[-1]["has_inner_quant"] = True
            continue
        if c == "|":
            stack[-1]["has_alt"] = True
            i += 1
            continue
        if c in "*+?{":
            kind, i = read_quantifier(i)
            if kind is not None:
                stack[-1]["has_inner_quant"] = True
            continue
        i += 1
    return False


def is_safe_user_regex(pattern: str) -> bool:
    """Return ``True`` if a USER-supplied deny regex is safe to run on the gate.

    A pattern is safe when it (a) compiles and (b) is NOT flagged by the
    structural exponential-ReDoS heuristic (``_redos_prone``).  Callers — the
    dashboard ``POST /denied-commands/user`` handler and ``_DenyMatcher`` —
    reject/skip a pattern that fails this check so a catastrophic user regex can
    never freeze the synchronous PreToolUse gate.

    The known-safe aws flag runs are stripped before the structural check only
    when the complete pattern is a built-in.  A user pattern wrapping the same
    fragment receives no exemption.

    A pattern with a TOP-LEVEL alternation (``a|b``) is also rejected: it cannot
    be split on ``.*`` for the linear full-length fragment matcher, so it would
    fall back to a length-bounded whole-string scan — which a padded command
    (a needle beyond the bound in one segment) could slip past. No built-in rule
    has top-level alternation; a user can express the same intent as separate
    rules, so rejecting it here closes the truncation-bypass with no coverage
    loss.
    """
    try:
        re.compile(pattern)
    except re.error:
        return False
    scrubbed = pattern
    if pattern in BUILTIN_DENY_PATTERNS:
        scrubbed = pattern.replace(_DANGEROUS_AWS_FLAG_RUN, "").replace(
            _LINEARIZED_AWS_FLAG_RUN, ""
        )
    if _redos_prone(scrubbed):
        return False
    return not _has_top_level_alternation(scrubbed)


def _polynomial_backtracking_prone(pattern: str) -> bool:
    """True if ``pattern`` has two ADJACENT quantified units at one nesting level.

    ``_redos_prone`` catches the EXPONENTIAL family (a quantified group whose
    body quantifies or alternates). This catches the POLYNOMIAL one it lets
    through — ``a+a+$``, ``(a+)(a+)$``, ``\\w+\\d+$``, ``.*.*!`` — where the engine
    redistributes one input run across two greedy units, O(n) ways per start
    position.

    That family is harmless on a length-capped window and NOT harmless without
    one: measured on CPython, ``a+a+$`` against 2,000 ``a``s takes ~3.5s, 4,000
    ~27s, 8,000 ~228s, and the grouped spelling ``(a+)(a+)$`` ~4.1s / ~35s. So
    this predicate gates ONLY the unbounded full-input path. A pattern it flags is
    still enforced, on the bounded engine — the behaviour every such pattern
    already had. It is deliberately not folded into ``is_safe_user_regex``:
    refusing these outright would drop rules that work today, and a rule silently
    not published is the defect this module is fighting, not a fix for it.

    A GROUP is a unit, and counts as quantified when it carries its own
    quantifier (``(ab)+``) OR when its content merely ENDS in one (``(a+)``):
    parentheses do not change how the engine redistributes the run, so
    ``(a+)(a+)$`` backtracks exactly like ``a+a+$``. Resetting state at a group
    boundary — treating a group as opaque — is what let the grouped spelling
    through; GPT 5.6 caught that on #7705.

    Conservative and syntactic: adjacency is judged on quantified units with no
    literal between them, so ``a+b+`` (disjoint runs, linear) is flagged too.
    Cheap over-rejection costs a fast path, never enforcement.
    """
    # One frame per nesting level. ``end`` is where the frame's most recent unit
    # ended; ``quantified`` says whether that unit was quantified.
    stack: list[dict] = [{"end": None, "quantified": False, "start": 0}]
    i, n = 0, len(pattern)

    def read_multi_quantifier(idx: int) -> "int | None":
        """Index past a >1-repetition quantifier at ``idx``, or None if absent."""
        if idx >= n:
            return None
        if pattern[idx] in "*+":
            j = idx + 1
            if j < n and pattern[j] in "?+":  # lazy / possessive modifier
                j += 1
            return j
        if pattern[idx] == "{":
            close = pattern.find("}", idx)
            if close != -1:
                body = pattern[idx + 1 : close]
                if body and all(c.isdigit() or c == "," for c in body):
                    hi = body.split(",")[-1] or "inf"
                    if hi == "inf" or (hi.isdigit() and int(hi) > 1):
                        return close + 1
        return None

    def record(frame: dict, start: int, end: int, quantified: bool) -> bool:
        """Add a unit to ``frame``; True if it abuts a quantified predecessor."""
        adjacent = quantified and frame["quantified"] and frame["end"] == start
        frame["end"] = end
        frame["quantified"] = quantified
        return adjacent

    while i < n:
        ch = pattern[i]
        if ch == "(":
            stack.append({"end": None, "quantified": False, "start": i})
            i += 1
            # Skip the group's opening construct — (?:, (?=, (?P<name>, …
            if i < n and pattern[i] == "?":
                i += 1
                while i < n and pattern[i] not in ":)":
                    i += 2 if pattern[i] == "\\" else 1
                if i < n and pattern[i] == ":":
                    i += 1
            continue
        if ch == ")" and len(stack) > 1:
            frame = stack.pop()
            after = read_multi_quantifier(i + 1)
            quantified = after is not None or frame["quantified"]
            end = after if after is not None else i + 1
            if record(stack[-1], frame["start"], end, quantified):
                return True
            i = end
            continue
        # A plain unit: an escape, a bracket class, or a single character.
        start = i
        if ch == "\\":
            i += 2
        elif ch == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":  # literal ']' first in the class
                i += 1
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            i += 1
        else:
            i += 1
        after = read_multi_quantifier(i)
        end = after if after is not None else i
        if record(stack[-1], start, end, after is not None):
            return True
        i = end
    return False


def _has_top_level_alternation(pattern: str) -> bool:
    """True if ``pattern`` has a ``|`` at nesting depth 0.

    A top-level alternation binds looser than concatenation (``a.*b|c`` is
    ``(a.*b)|(c)``), so splitting on top-level ``.*`` would be INCORRECT — such
    a pattern must use the bounded-scan fallback instead.  Bracket classes and
    escapes are skipped so a ``|`` inside ``[...]`` or a literal ``\\|`` does
    not count.
    """
    depth = 0
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                if pattern[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return True
        i += 1
    return False


def _split_deny_frags(pattern: str) -> list[str]:
    """Split ``pattern`` on its TOP-LEVEL ``.*`` gaps into fixed fragments.

    Only an unescaped ``.`` immediately followed by ``*`` at nesting depth 0 is
    treated as a gap (a ``.?`` / ``.+`` / a nested ``.*`` inside ``(...)`` stays
    inside its fragment and is matched by the real engine).  A lazy (``.*?``) or
    possessive (``.*+``) modifier on the gap is consumed with it — all three
    spellings mean "any run of characters" for an ordered existence-match split,
    and leaving the dangling ``?`` / ``+`` behind would produce a fragment that
    starts with a bare quantifier and fails to compile, silently disabling an
    otherwise-valid user rule.  Empty fragments (from a leading/trailing/adjacent
    ``.*``) are dropped — a leading/trailing ``.*`` is redundant under
    ``re.search`` and an interior empty cannot occur because two adjacent
    top-level ``.*`` collapse to one gap.
    """
    frags: list[str] = []
    cur: list[str] = []
    depth = 0
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            cur.append(pattern[i : i + 2])
            i += 2
            continue
        if c == "[":
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                if pattern[j] == "\\":
                    j += 1
                j += 1
            j += 1
            cur.append(pattern[i:j])
            i = j
            continue
        if c == "(":
            depth += 1
            cur.append(c)
            i += 1
            continue
        if c == ")":
            depth -= 1
            cur.append(c)
            i += 1
            continue
        if c == "." and depth == 0 and i + 1 < n and pattern[i + 1] == "*":
            frags.append("".join(cur))
            cur = []
            i += 2
            # Absorb a lazy/possessive modifier on the gap (``.*?`` / ``.*+``);
            # otherwise the dangling ``?`` / ``+`` becomes a fragment-leading
            # quantifier that fails to compile and disables the whole rule.
            if i < n and pattern[i] in "?+":
                i += 1
            continue
        cur.append(c)
        i += 1
    frags.append("".join(cur))
    return [f for f in frags if f]


def _frags_can_underconsume(frags: list[str]) -> bool:
    """True if the forward-only fragment matcher could MISS a real match.

    The linear matcher searches each fragment in order with an advancing
    ``re.search(text, pos)`` and cannot backtrack across a ``.*`` gap boundary.
    So if a NON-FINAL fragment ends in a greedy, variable-width quantifier
    (``.+`` / ``x*`` / ``\\S+`` / ``(...)+`` / ``a{2,}``), that fragment greedily
    consumes characters the NEXT fragment needs — e.g. ``rm .+`` in
    ``rm .+ .* --no-preserve-root`` eats ``x--no-preserve-root`` so the tail
    fragment never matches, a FALSE NEGATIVE that lets a denied command through.
    Real ``re.search`` would backtrack; the linear matcher won't.

    A lazy (``*?`` / ``+?`` / ``{m,}?``) trailing quantifier consumes minimally,
    so it CANNOT over-consume — those are safe. Only the FINAL fragment's greedy
    tail is harmless (nothing follows it). When this returns True the matcher
    routes to the bounded whole-regex path (exact ``re.search`` semantics on a
    length-capped window) instead of the linear split.
    """
    for frag in frags[:-1]:
        s = frag.rstrip()
        if not s:
            continue
        last = s[-1]
        # A lazy modifier (``*?`` / ``+?`` / ``}?``) consumes minimally → safe.
        if last == "?" and len(s) >= 2 and s[-2] in "*+}":
            continue
        if last in "*+":
            # Count preceding backslashes: an odd count means the quantifier is
            # escaped (a literal ``\*``/``\+``), which does not over-consume.
            j = len(s) - 2
            bs = 0
            while j >= 0 and s[j] == "\\":
                bs += 1
                j -= 1
            if bs % 2 == 0:
                return True
        elif last == "}":
            # ``{m,}`` / ``{m,n}`` — an open-ended (``,``-bearing) count is
            # variable-width and greedy; ``{m}`` (exact) is fixed-width, safe.
            open_idx = s.rfind("{")
            if open_idx >= 0 and "," in s[open_idx + 1 : -1]:
                return True
    return False


def _matches_full_input(pattern: str) -> bool:
    """True when :class:`_DenyMatcher` scans the WHOLE input for ``pattern``.

    A pattern reaches the length-bounded window (``_DENY_FALLBACK_SCAN_MAX_CHARS``)
    unless it is a parity-tested built-in OR splits into exactly one fragment. One
    fragment means no top-level ``.*``, hence no gap the forward-only matcher could
    fail to backtrack across, so its single ``re.search`` is full-input and exactly
    equivalent to the bounded path's semantics without the cap.

    This exists so a caller can ask the question BEFORE publishing a rule, rather
    than discovering after the fact that the row it advertised as enforcing is
    bypassable by padding the command past the cap. ``_DenyMatcher.__init__``
    decides the same thing from the fragments it already computed;
    ``test_deny_matcher_full_input_agreement`` pins the two to the same answer so
    this predicate cannot drift away from the matcher it describes.
    """
    linear = _linearize_deny_pattern(pattern)
    if _has_top_level_alternation(linear):
        return False
    try:
        frags = _split_deny_frags(linear)
    except re.error:
        return False
    return len(frags) == 1 and not _frags_can_underconsume(frags)


class _DenyMatcher:
    """A ReDoS-safe, full-length matcher for a single deny regex.

    Built once per pattern (memoized in ``_DENY_MATCHER_CACHE``).  ``match``
    returns whether the ORIGINAL pattern would match anywhere in ``text``:

    * Fragment path (BUILT-INS ONLY) — the pattern is split on its top-level
      ``.*`` gaps and the fragments are searched in order with an advancing
      ``re.search(text, pos)`` (equivalent to ``frag0.*frag1.*…`` but linear-time,
      no length bound). The 137 built-ins were authored for kiro-cli's RE2-style
      engine and are parity-tested to be fragment-safe (no backtracking-dependent
      construct — no ``(a|b)`` before a ``.*``, no greedy variable-width tail on a
      non-final fragment).
    * Bounded path (USER CUSTOM REGEXES + any built-in with a top-level
      alternation) — compiled whole and matched against a length-bounded prefix,
      giving EXACT ``re.search`` semantics (backtracking preserved). A
      user-supplied pattern is NEVER run through the forward-only fragment
      matcher: that matcher commits to each fragment's first match and cannot
      backtrack across a ``.*`` gap, so a pattern like ``(ab|a).*b`` (or a greedy
      ``rm .+.*x``) would UNDER-match and let a denied command through. Routing
      all user patterns to the exact bounded engine closes that fidelity class
      entirely — and it is ReDoS-safe because ``is_safe_user_regex`` already
      rejected catastrophic-backtracking patterns at add-time and here.

    Defense-in-depth: a pattern that fails ``is_safe_user_regex`` (a
    catastrophic-backtracking construct, only reachable via an already-stored
    USER custom regex — built-ins are safe by construction) is DISABLED — the
    matcher never runs it and never matches, logged once.  This guarantees the
    synchronous PreToolUse gate cannot be frozen even if such a pattern slipped
    into the config before the add-time check existed.  A malformed pattern
    (``re.error``) is likewise disabled so one bad rule cannot wedge the gate.
    """

    __slots__ = ("_frag_res", "_whole_re", "_bounded", "_disabled")

    def __init__(self, pattern: str) -> None:
        self._frag_res: "list[re.Pattern[str]]" = []
        self._whole_re: "re.Pattern[str] | None" = None
        self._bounded = False
        self._disabled = False
        if not is_safe_user_regex(pattern):
            # Either malformed or ReDoS-prone — refuse to run it (built-ins never
            # reach this branch; they are safe by construction).
            logger.warning("Disabling unsafe/malformed denied-command regex %r", pattern)
            self._disabled = True
            return
        linear = _linearize_deny_pattern(pattern)
        # A USER custom pattern (not one of the built-ins) is matched by the exact
        # bounded engine, never the forward-only fragment matcher — the latter
        # cannot faithfully emulate ``re.search`` backtracking (``(ab|a).*b``,
        # greedy ``.+`` before ``.*``, etc.), which would UNDER-match and let a
        # denied command through.  Built-ins are RE2-authored + parity-tested, so
        # they keep the fast fragment path.
        is_builtin = pattern in _RULE_ID_BY_PATTERN
        try:
            frags = None if _has_top_level_alternation(linear) else _split_deny_frags(linear)
            # A pattern with NO top-level ``.*`` splits into exactly one fragment,
            # and a one-fragment match IS ``re.search`` over the whole input: there
            # is no gap to fail to backtrack across, and ``_frags_can_underconsume``
            # inspects only ``frags[:-1]``, which is empty. So the under-match risk
            # that restricts the fragment path to parity-tested built-ins cannot
            # arise, and such a pattern gets FULL-INPUT matching whoever authored
            # it. This is what keeps an edition-contributed or user-added rule from
            # being silently capped at ``_DENY_FALLBACK_SCAN_MAX_CHARS`` — a rule
            # that only scans a 2000-char prefix is bypassed by padding, which is
            # not a control the Settings panel should show as enforcing.
            #
            # Gated on ``_polynomial_backtracking_prone``: removing the length cap
            # also removes what made POLYNOMIAL backtracking harmless. ``a+a+$``
            # passes ``is_safe_user_regex`` (it is not the exponential shape) and
            # measures ~3.5s against 2,000 characters, which is a stall of the
            # synchronous gate — GPT 5.6 flagged exactly this on #7705 and was
            # right. Such a pattern keeps the bounded engine it already had; only
            # patterns that are free to run unbounded get the full-input path.
            single_fragment = (
                frags is not None
                and len(frags) == 1
                and not _polynomial_backtracking_prone(linear)
            )
            if (
                frags is None
                or _frags_can_underconsume(frags)
                or not (is_builtin or single_fragment)
            ):
                # Bounded whole-regex: exact ``re.search`` semantics on a
                # length-capped window.  ReDoS-safe because ``is_safe_user_regex``
                # above already rejected catastrophic patterns.
                self._whole_re = re.compile(linear, re.IGNORECASE)
                self._bounded = True
            else:
                self._frag_res = [re.compile(f, re.IGNORECASE) for f in frags]
        except re.error:
            logger.warning("Skipping malformed denied-command regex %r", pattern)
            self._disabled = True

    def match(self, text: str) -> bool:
        if self._disabled:
            return False
        if self._bounded:
            if self._whole_re is None:
                return False
            # DOCUMENTED TRADE-OFF: the bounded path scans only the first
            # ``_DENY_FALLBACK_SCAN_MAX_CHARS`` chars. Python's backtracking ``re``
            # cannot give exact ``re.search`` semantics AND full-input AND
            # ReDoS-safety at once — a polynomial (non-catastrophic, so
            # is_safe_user_regex-accepted) user pattern like ``(ab|a).*b`` is
            # O(n²), which would freeze the gate on a large input without this
            # cap (true full-input would need a linear RE2 engine — a dependency
            # the project deliberately avoids). Scope of the residual: this path
            # is reached only by a pattern that NEEDS the exact engine — one with
            # a top-level alternation, or whose fragments can over-consume across
            # a ``.*`` gap. A pattern that splits into ONE fragment takes the
            # full-input path whoever authored it (built-in, edition-contributed
            # or user-added), because with no gap the single ``re.search`` already
            # has exact semantics; and an edition rule that WOULD land here is not
            # published at all (``edition_denied_rules``), since a rule enforced
            # only over a prefix is bypassable by padding. See security.md.
            return self._whole_re.search(text[:_DENY_FALLBACK_SCAN_MAX_CHARS]) is not None
        # An empty fragment list means the pattern reduced to ``.*`` (matches
        # everything).  No built-in does this, but stay fail-open-safe: only a
        # literal ``.*`` custom rule would, and it legitimately matches all.
        pos = 0
        for frag_re in self._frag_res:
            m = frag_re.search(text, pos)
            if m is None:
                return False
            pos = m.end()
        return True


_DENY_MATCHER_CACHE: dict[str, _DenyMatcher] = {}


def _deny_matcher(pattern: str) -> _DenyMatcher:
    """Return the memoized :class:`_DenyMatcher` for ``pattern``."""
    matcher = _DENY_MATCHER_CACHE.get(pattern)
    if matcher is None:
        matcher = _DenyMatcher(pattern)
        _DENY_MATCHER_CACHE[pattern] = matcher
    return matcher


# ── Git publish detection (verb-anchored) ──
# ``git push`` must be blocked, but ``push`` appearing anywhere in arbitrary
# command text (a commit message, a branch name, a grep pattern, an ssh remote
# payload) must NOT trip the deny.  We therefore require ``push`` to be the git
# *subcommand* — i.e. the first non-flag/non-option token after ``git`` — rather
# than a substring.  Mirrors the anchored regex in
# ``config/defaults.json`` deniedCommands.
#
# ``git [<-c k=v>...] [<-C path>...] push ...`` is a publish.  Intervening
# tokens may only be options (``-x``) or option-with-value pairs
# (``-C /path``, ``-c core.x=y``) — a bare non-flag token before ``push``
# (e.g. ``stash``) means ``push`` is NOT the subcommand, so ``git stash push``
# is correctly allowed.  Anchored to a segment start (optionally preceded by a
# command separator) so ``git log --grep push`` is not matched.
#
# The trailing terminator is a lookahead that accepts whitespace, end-of-string,
# OR a shell metacharacter that closes/terminates the segment — so a bare
# ``git push`` (no remote/branch, valid: pushes current branch to the default
# remote) is still caught inside ``$(git push)``, `` `git push` ``, ``git push|cat``,
# ``git push&``, etc., not just when followed by a space.
_GIT_PUBLISH_RE = re.compile(
    # ``[^-\s]`` (not ``[^-]``): the optional non-flag arg after a flag must
    # NOT start with whitespace, otherwise inter-token whitespace could be
    # matched either by the preceding ``\s+`` or by this group's leading char —
    # an ambiguity that backtracks exponentially (ReDoS) on whitespace-laden
    # flag runs when the trailing ``push`` is absent.
    # ``(`` is in the leading class because bash treats it as an operator, so
    # ``(git push`` runs git exactly as ``; git push`` does -- without it the
    # glued subshell form ``(git push origin main)`` matched no branch and the
    # only enforcement for git-publish (this floor) never fired.
    r"(?:^|[;&|`\n(]|\$\()\s*git\s+(?:-\S+\s+(?:[^-\s]\S*\s+)?)*push(?=\s|[)`;&|]|$)"
)

# Glue-evasion guard: bash command-substitution / quoting tricks that evaluate
# to ``git push`` but break the token sequence above, e.g.
# ``git$(echo ' ')push``, ``git`echo`push``, ``git$()push``.  After stripping
# empty substitutions/backticks the residue is ``gitpush``; we also match a
# literal ``git_push`` (kiro-cli historically denied that form).
_GIT_PUBLISH_GLUE_RE = re.compile(r"git(?:\$\([^)]*\)|`[^`]*`)+push|git_push")

# Program NAME produced by an expansion the shell resolves to the git binary
# BEFORE exec, so the literal ``git`` token never appears in the source text and
# neither the regex above nor the normalizer (which does not expand arbitrary
# vars) sees it:
#   ``$(echo git) push``, `` `echo git` push ``, ``${GIT} push``, ``$GIT push``
# (where e.g. ``GIT=/usr/bin/git``).  We cannot execute the expansion to recover
# the program, so a ``push`` subcommand immediately following an unresolvable
# program token is treated as a publish (FAIL CLOSED); ``_is_push_to_protected_branch``
# then reads the push target and denies a protected / bare / ambiguous one while
# still allowing an explicit feature-branch target.  Ported from the upstream
# project.
_GIT_PUBLISH_SUBST_PROGRAM_RE = re.compile(
    r"(?:^|[;&|`\n])\s*"
    r"(?:\$\([^)]*\)|`[^`]*`|\$\{[^}]*\}|\$[A-Za-z_]\w*)"
    r"\s+push(?=\s|$|[)`;&|])"
)

# Human-readable label recorded in the denial reason + SEL audit event when
# a git-publish invocation is blocked (the regexes above are the mechanism).
_GIT_PUBLISH_DENY_LABEL = "git push"

#: The refusal prefix, exported so guards cannot drift from the producer.
#: ``RecoveryCard.tsx`` parses refusals with
#: ``/Blocked by security policy:\s*(.+?)\s*$/gm`` — GLOBAL and per-line — so any
#: line carrying this literal is read as a deny pattern.  An operator note is
#: emitted on its own line, which means a note containing this literal would be
#: parsed as a SECOND, fabricated pattern.  Callers that accept operator text
#: reject or drop it on this constant (see ``hooks.resolve_denied_notes`` and the
#: dashboard add handler) rather than hardcoding the string again.
DENY_REASON_PREFIX = "Blocked by security policy: "

#: The form to GUARD against, which is NOT the form we emit. ``RecoveryCard``'s
#: regex is ``Blocked by security policy:\s*`` — the whitespace after the colon is
#: optional — so ``"Blocked by security policy:forged"`` parses as a refusal line
#: while NOT containing :data:`DENY_REASON_PREFIX` (which carries a trailing
#: space). Guarding on the full prefix therefore leaves a bypass. Derived from the
#: same string so the two can never drift apart.
DENY_REASON_MATCH_PREFIX = DENY_REASON_PREFIX.rstrip()


# ── Self-protection floor (argv-structural, not a regex) ──
# The two self-protection rules below are enforced by TOKENIZING the command
# rather than by matching its raw text.  A raw-string regex cannot decide these:
# the gap between the product name and the verb has to step over ordinary shell
# noise (a quoted verb, global flags, a redirect), but every character class wide
# enough to do that also steps over a filesystem path -- and "a path that
# contains the product name" is exactly the false positive these rules exist to
# stop.  Tokenizing resolves quoting and redirection BEFORE matching, so both
# sides can be exact.  See ``_is_credential_mint`` / ``_is_self_kill``.
_SELF_NAME_RE = re.compile(r"kiro[-.]?crew")
# ``[k]irocrew`` -- a one-character bracket class expands to that character, so it names
# the protected program.  Collapsed before comparison rather than folded into every name
# pattern, so a single rule covers the idiom wherever it appears in the word.
_ONE_CHAR_CLASS_RE = re.compile(r"\[(\w)\]")


def _debracket(text: str) -> str:
    """Collapse one-character bracket classes (``[k]irocrew`` -> ``kirocrew``)."""
    return _ONE_CHAR_CLASS_RE.sub(r"\1", text)


# The product name as a WHOLE program name (bare or the tail of a path), which is
# what distinguishes ``bin/kirocrew token`` from ``cd kirocrew-wt-x``.


_SELF_PROGRAM_RE = re.compile(r"\Akiro[-.]?crew(?:\.(?:exe|cmd|bat|sh|py))?\Z")
# Shell glob metacharacters, and the concrete spellings a glob could expand to.  A
# glob in the program name (``kiro[c]rew``) is resolved by the shell BEFORE exec, so
# it has to be tested for expandability rather than compared literally.
_GLOB_CHARS_RE = re.compile(r"[\[\]?*{}]")
_SELF_PROGRAM_SPELLINGS = ("kirocrew", "kiro-crew", "kiro.crew")
# The kill programs that select their target BY NAME.  Bare ``kill`` takes PIDs
# and is handled separately (it can only reach the product through a command
# substitution that resolves the name), and both verbs are matched on TOKENS via
# ``_program_basename`` so a path-qualified or expansion-produced spelling counts.
_KILL_BY_NAME_PROGRAMS = frozenset({"pkill", "killall"})


# Characters a shell uses to WRAP a program name rather than to spell it: the
# quote marks and the parentheses of a command substitution.  Peeled to a fixed
# point in ``_program_basename`` so no interleaving with a redirect hides a name.
_SHELL_WRAPPER_CHARS = "`\"'()"


def _strip_redirect(token: str) -> str:
    """The token with any ATTACHED redirection suffix removed.

    ``shlex`` keeps a redirect glued to its neighbour as one token, so
    ``kirocrew>/tmp/out`` arrives as a single word and a program comparison against
    it fails.  bash splits the redirect off before exec, so the program is the part
    before the first ``>``/``<``; the same applies to an operand
    (``token>/tmp/out``).  A leading fd number (``2>``) leaves an empty program,
    which no comparison matches -- correct, since that word is not a program.
    """
    for op in (">", "<"):
        if op in token:
            token = token.split(op, 1)[0]
    return token


def _substitution_program(token: str) -> str:
    """The program a command-substitution body resolves to.

    A substitution in program position is a RESOLVER -- ``$(which pkill)``,
    ``$(command -v bash)``, ``$(type -p kirocrew)`` -- and the program it resolves to
    is the resolver's final argument.  ``shlex`` splits an UNQUOTED body on its
    own spaces, so ``$(which pkill)`` already arrives as two words; a QUOTED body
    (``"$(command -v pkill)"``) arrives as one multi-word token instead.  Taking the
    last word makes both spellings compare as the same program.
    """
    return token.rsplit(None, 1)[-1] if token.split() else token


def _program_basename(token: str) -> str:
    """The program name a token invokes, with shell wrappers stripped.

    Strips quoting, command-substitution wrappers and any attached redirection
    before taking the basename, so an expansion-produced program name
    (``$(which pkill)``, ``"$(command -v bash)"``) or a redirect-glued one
    (``kirocrew>/tmp/out``) is compared as the program it resolves to rather than as
    literal punctuation.  Every program check goes through this -- comparing a raw
    ``os.path.basename`` lets ``$(which pkill) -f <name>`` past the kill rule.

    The layers are peeled to a FIXED POINT rather than once in a fixed order.
    A wrapper and a redirect interleave freely, and any single ordering leaves a
    hole for the interleavings it does not match: ``$(which kirocrew)>/tmp/out`` needs
    the redirect gone before its closing paren reaches the end of the word, while
    ``kirocrew)`` needs the paren gone with no redirect in play at all.  Looping until
    nothing changes makes the peel order-independent, which closes the class
    instead of whichever spelling a fixed order happened to cover.
    """
    if not token:
        return ""
    previous = ""
    substituted = False
    while token != previous:
        previous = token
        token = _strip_redirect(token)
        token = _resolve_param_defaults(token)
        token = _EMPTY_SUBST_RE.sub("", token)
        if token.startswith("$(") or token.startswith("`"):
            substituted = True
        token = token.removeprefix("$(").strip(_SHELL_WRAPPER_CHARS)
        # ANSI-C / locale quoting: ``$'name'`` and ``$"name"`` are just quoting
        # forms, so the ``$`` left behind after the quotes come off is not part of
        # the program name.  ``$(`` and ``${`` are handled above and below.
        if token.startswith("$") and not token.startswith(("$(", "${")):
            token = token[1:]
    if substituted:
        token = _substitution_program(token)
    # A control operator GLUED to the name (``true;kirocrew``, ``x&&kirocrew``)
    # means the program that actually runs is what follows the LAST operator --
    # ``shlex`` splits on whitespace only, so it hands the whole run over as one
    # word and a comparison against it matches nothing.  Taking the trailing
    # segment is what bash does; a trailing operator leaves an empty tail, so the
    # last NON-EMPTY segment is the one that names a program.
    segments = [s for s in _CONTROL_OPERATOR_RE.split(token) if s]
    if segments:
        token = segments[-1]
    return os.path.basename(token.rstrip("/"))


def _glob_could_expand_to(base: str, names: "tuple[str, ...] | frozenset[str]") -> bool:
    """True if *base* carries a shell glob that could expand to one of *names*.

    A glob in the program name is resolved by the shell BEFORE exec, so it has to be
    tested for expandability rather than compared literally.  ``[...]`` and ``?`` stand
    for one character and ``*`` for any run, so only a pattern that CAN name the target
    counts -- ``kiro[x]few`` still does not.
    """
    if not _GLOB_CHARS_RE.search(base):
        return False
    try:
        expandable = re.compile(_glob_to_regex(base), re.IGNORECASE)
    except re.error:
        return False
    return any(expandable.fullmatch(name) for name in names)


#: Interpreter names that accept ``-m <module>``. Versioned spellings (``python3``,
#: ``python3.12``) and the Windows launcher included; ``.exe`` is stripped by
#: ``_program_basename`` before this is applied.
_PYTHON_PROGRAM_RE = re.compile(r"\Apy(?:thon)?[0-9.]*(?:\.exe)?\Z")

#: Interpreter flags that consume the NEXT token as their operand. Their operand must be
#: skipped when scanning for ``-m <module>``, or it terminates the scan and the mint slips
#: through (``python -X dev -m kiro_crew token``). ``-c`` and ``-m`` are deliberately absent:
#: both END the option list, and `-m` is what this scan is looking for.
#:
#: LOWERCASE, because the floor runs over an already-lowercased command (`_is_credential_mint`
#: takes `text_lower`), so a `-X` in the operator's shell reaches this set as `-x`. Storing the
#: uppercase spelling made every separate-operand form match nothing — the bypass stayed open
#: while the ATTACHED spellings (`-Xdev`) passed, which is the shape of a fix that looks tested.
#: Python's real flags are case-sensitive (`-x` skips the first line, `-X` sets an
#: implementation option), so this over-matches `-x` slightly: `python -x -m kiro_crew token`
#: would skip `-m` as an operand and MISS. Guarded by also treating a bare `-m` as the marker
#: on the next iteration — see the loop.
_PYTHON_OPERAND_FLAGS = frozenset({"-x", "-w", "-q", "--check-hash-based-pycs"})

#: Module path of the product package, for the ``python -m kiro_crew ... token`` form.
#: Underscored, because that is the IMPORT name — `_SELF_PROGRAM_SPELLINGS` covers the
#: console script (`kirocrew`, `kiro-crew`) and deliberately does not admit `_`, since no
#: executable is spelled that way.
_SELF_MODULE_SPELLINGS = ("kiro_crew",)

#: Interpreter flags that take an INLINE PROGRAM as their operand: ``-c`` a statement string,
#: ``-`` / no flag a stdin script. ``python -c "from kiro_crew.cli import main; main()" token``
#: mints the identical token as ``python -m kiro_crew token`` — the payload is one argv word
#: carrying the import name, so the ``-m`` marker scan never fires and the "not a flag ⇒ not
#: the module shape" bail treated the payload as a script name and returned False. Same escape,
#: one flag over. Found in review (GPT 5.6).
_PYTHON_INLINE_PROGRAM_FLAGS = ("-c",)

#: The import name as it appears INSIDE a ``-c`` payload. A payload that both names the package
#: and calls something is the module form written longhand; matching the bare package name is
#: enough, because reaching the CLI at all requires importing it under one of these spellings —
#: PROVIDED the name is written literally, which the split/base64 forms below deliberately avoid.
_SELF_IMPORT_RE = re.compile(r"\bkiro_crew\b")

#: Dynamic-execution primitives that let an inline Python payload REACH the CLI without the
#: package name ever appearing as a literal token: string-concatenated imports
#: (``__import__('kiro'+'_crew')``), name-computed imports (``importlib.import_module(...)``),
#: and second-stage decode/eval (``exec(base64.b64decode(...))``). ``_SELF_IMPORT_RE`` cannot
#: see through any of these, so a payload combining an inline-program interpreter with one of
#: them is treated as opaque and DENIED — the same fail-closed reading as a literal import,
#: because "I cannot tell what this imports" is not "it is safe". Kept as a NARROW list of the
#: dynamic-exec verbs, not a blanket deny on all inline Python: ``python -c "print(1)"`` and
#: routine one-liners stay allowed, and the residual — arbitrary code that avoids even these
#: (``perl``, a written-then-run script, a renamed interpreter) — is out of a string matcher's
#: reach and is documented as such rather than papered over. Found in review (GPT 5.6).
_INLINE_DYNAMIC_EXEC_RE = re.compile(
    r"\b__import__\s*\(|\bimportlib\b|\bimport_module\b|\bexec\s*\(|\beval\s*\(|"
    r"\bcompile\s*\(|\bb64decode\b|\bmarshal\b|\bgetattr\s*\("
)


def _inline_payload_reaches_cli(payload: str) -> bool:
    """True if an inline-program payload could import this package, LITERALLY or opaquely.

    Two ways: it names ``kiro_crew`` outright, or it uses a dynamic-execution primitive that
    could construct that import from pieces a static matcher cannot follow. The second is a
    deliberate over-match — a payload doing ``exec(...)`` or ``__import__(...)`` might import
    something else entirely — but on the credential-mint path "I cannot tell what this runs" is
    the fail-closed answer, and the cost is refusing an inline one-liner that happens to use
    ``exec``/``eval``, which is not a shape ordinary tooling relies on.
    """
    return bool(_SELF_IMPORT_RE.search(payload) or _INLINE_DYNAMIC_EXEC_RE.search(payload))


def _is_self_module_invocation(tokens: list[str], i: int) -> bool:
    """True if ``tokens[i]`` is a ``python`` that runs the product IN-PROCESS.

    ``python -m kiro_crew token`` mints exactly the same signed dashboard token as
    ``kirocrew token``, but its argv PROGRAM is the interpreter, so neither the
    command-position regex (which matches ``kiro[-.]?crew``, not the underscored import
    name) nor ``_is_self_program`` sees it. The escalation is the point: that token
    authenticates every gateway route, including the ops autonomy-ceiling PUT, so the
    bypass let a prompt-injected agent raise its own security ceiling.

    Matched structurally, like the rest of the floor: an interpreter, then ``-m``
    (possibly after other interpreter flags), then the module. ``-m`` must be a separate
    token — ``python -mkiro_crew`` is also valid, so that spelling is checked too.

    ``-c`` is the SAME escape one flag over, and is matched here for that reason:
    ``python -c "from kiro_crew.cli import main; main()" token`` reaches the identical mint
    with the import name buried in an inline-program payload. The two forms differ only in
    how the interpreter is told to import the package, so they cannot be gated separately —
    the earlier "anything that is not a flag means this is not the module shape" bail read the
    payload as a script name and returned False. Found in review.

    Interpreter flags that take a SEPARATE OPERAND (``-X dev``, ``-W ignore``, ``-Q new``)
    have their operand skipped. An earlier version stopped at the first token that did
    not begin with ``-``, so ``python -X dev -m kiro_crew token`` bailed on ``dev`` and the
    mint went through — the bypass this whole function exists to close, reintroduced one flag
    deeper. Modelling which flags consume an operand is the fix; "stop at the
    first non-flag" is not expressible as a heuristic here, because an operand and a script
    path look identical.
    """
    if not _PYTHON_PROGRAM_RE.match(_program_basename(tokens[i])):
        return False
    skip_next = False
    inline_program_next = False
    for later in tokens[i + 1 :]:
        stripped = _normalize_operand(later).strip("\"'")
        if inline_program_next:
            # The payload of a `-c`: an inline program naming the package IS an import of it.
            # Checked before the flag logic because the payload is arbitrary text that may
            # begin with anything, including a `-`.
            #
            # Matched on the RAW token, not `stripped`: `_normalize_operand` truncates at the
            # first control operator, which is right for an operand the shell will split but
            # wrong for a quoted Python program whose `;` is a statement separator. Normalising
            # `"import sys; ...; from kiro_crew.cli import main"` down to `import sys` hid the
            # import and made this return False for a payload that plainly runs our code.
            if _SELF_IMPORT_RE.search(later.strip(_SHELL_WRAPPER_CHARS)):
                return True
            inline_program_next = False
            continue
        if skip_next:
            skip_next = False
            # `-m` is never a flag's operand: `python -x -m mod` passes `-m mod` to the
            # interpreter, so a token that IS the marker must be honoured rather than eaten.
            # This is what keeps the deliberate `-x` over-match above from opening a hole.
            if stripped != "-m" and not stripped.startswith("-m"):
                continue
        if stripped == "-m":
            continue
        if stripped.startswith("-m") and stripped[2:] in _SELF_MODULE_SPELLINGS:
            return True
        if stripped in _SELF_MODULE_SPELLINGS:
            return True
        if stripped in _PYTHON_INLINE_PROGRAM_FLAGS:
            inline_program_next = True
            continue
        # `-c<payload>` attached, the one-token spelling of the same thing. Raw for the same
        # truncation reason as the separate operand above.
        _raw = later.strip(_SHELL_WRAPPER_CHARS)
        if len(_raw) > 2 and _raw[:2] in _PYTHON_INLINE_PROGRAM_FLAGS:
            if _SELF_IMPORT_RE.search(_raw):
                return True
            continue
        if stripped in _PYTHON_OPERAND_FLAGS:
            skip_next = True
            continue
        # An attached operand (`-Xdev`, `-Wignore`) needs no skip: it is one token.
        if len(stripped) > 2 and stripped[:2] in _PYTHON_OPERAND_FLAGS:
            continue
        # Only interpreter FLAGS may sit between; anything else means this is neither the
        # `-m <product>` nor the `-c <payload>` shape (`python script.py`).
        if not stripped.startswith("-"):
            return False
    return False


def _is_self_program(token: str) -> bool:
    """True if *token* names the KiroCrew CLI itself, bare or via a path.

    Also true when the name carries a shell GLOB the shell would expand to the
    executable -- ``./bin/kiro[c]rew``, ``kiro?rew``, ``kiro*rew``.
    """
    base = _program_basename(token)
    if _SELF_PROGRAM_RE.match(base):
        return True
    return _glob_could_expand_to(base, _SELF_PROGRAM_SPELLINGS)


def _is_kill_by_name_program(token: str) -> bool:
    """True if *token* invokes ``pkill``/``killall``, including a globbed spelling."""
    base = _program_basename(token)
    if base in _KILL_BY_NAME_PROGRAMS:
        return True
    return _glob_could_expand_to(base, _KILL_BY_NAME_PROGRAMS)


def _glob_to_regex(pattern: str) -> str:
    """Translate a shell glob into a regex that matches what it could expand to."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            out.append(".")
            i = close + 1
            continue
        if ch == "{":
            # ``kiro{c..c}rew`` expands to the real name, so a brace group stands for
            # whatever it can produce -- same treatment as a bracket class.
            close = pattern.find("}", i + 1)
            if close == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            out.append(".*")
            i = close + 1
            continue
        if ch == "?":
            out.append(".")
        elif ch == "*":
            out.append(".*")
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _self_tokens(text_lower: str) -> "list[str]":
    """Tokenize the WHOLE command, resolving quoting before any splitting.

    Splitting the raw text into segments first (as the pattern passes do) is
    unsafe for these rules: it cuts on a ``;`` or ``|`` that is INSIDE a quoted
    argument, so ``pkill -f '[;]*kirocrew'`` loses its own target. ``shlex``
    resolves the quotes first, so a quoted separator stays part of one token.
    """
    try:
        return _resolve_function_aliases(
            _resolve_local_assignments(normalize_shell_command(text_lower))
        )
    except Exception:
        return []


# Programs whose ``-c`` argument is a shell script: its text is a COMMAND, so a
# self-protection check has to look inside it rather than treat it as an operand.
_NESTED_SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"})
_NESTED_SHELL_VERBS = frozenset({"eval", "source", "."})
# ``env -S`` splits its argument into a command and execs it.
_ENV_SPLIT_PROGRAMS = frozenset({"env"})
# Programs that treat their arguments as DATA rather than executing them, so the
# product name appearing in their argv is a mention, not an invocation:
# ``echo kirocrew token`` prints two words.
#
# This list is deliberately a DENYLIST of data consumers rather than an ALLOWLIST
# of executors, because the two fail in opposite directions.  Many commands pass
# their remaining argv to an executor -- ``ssh host …``, ``docker exec c …``,
# ``sudo``, ``env``, ``nohup``, ``timeout``, ``runuser``, ``chroot``, ``pkexec``,
# ``systemd-run``, ``nice``, ``xargs`` -- and enumerating THOSE means a forgotten
# entry is a silent BYPASS.  Enumerating data consumers instead means a forgotten
# entry is a false positive: annoying, visible, and safe.  So the default for an
# unrecognised program is "this could execute the name".
_DATA_CONSUMER_PROGRAMS = frozenset(
    {
        "echo", "printf", "print", "cat", "tac", "tee", "head", "tail", "less", "more",
        "grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "awk", "cut", "tr", "sort",
        "uniq", "wc", "nl", "fold", "column", "comm", "diff", "strings", "jq", "yq",
        "base64", "md5sum", "sha256sum", "xxd", "od",
    }
)
# Control operators that end one command and begin another.  Used to find the
# program in a run that ``shlex`` handed over as a single word.
_CONTROL_OPERATOR_RE = re.compile(r"[;&|\n]+")
# An EMPTY substitution expands to nothing, so ``p$()kill`` runs ``pkill`` -- the
# same glue-evasion as the empty-quote form (``ca""t`` -> ``cat``) that
# ``normalize_shell_command`` already undoes, but spelled with a substitution and
# placed MID-WORD, where a prefix-only strip never sees it.
_EMPTY_SUBST_RE = re.compile(r"\$\(\s*\)|`\s*`|\$\{\s*\}")
# An OUTPUT redirect. Two small sets, enumerated from the shells' own grammars rather
# than grown one spelling per review round, so the boundary is stated instead of implied:
#
#   DESCRIPTOR (optional prefix)  digits -- every shell
#                                 ``&``    both streams (bash, zsh, ksh)
#                                 ``{name}`` automatic descriptor (bash 4.1+, zsh)
#                                 ``*``    all streams (PowerShell)
#   OPERATOR                      ``>`` or ``>>``
#   MODIFIER (optional suffix)    ``&``  duplicate (bash, zsh, ksh, csh)
#                                 ``|``  noclobber override (bash, zsh, ksh)
#                                 ``!``  noclobber override (zsh, csh, tcsh)
#
# NOT covered, deliberately and on the record: fish's historical ``^`` stderr prefix
# (removed in fish 3.0 and this module has no fish handling), and cmd.exe's ``n>&m``
# which the digit prefix already matches. If a shell outside that list reaches this gate,
# this set is where it has to be added.
#
# ``*`` is included on the FAIL-CLOSED rule this floor states for itself ("any maybe
# answers True -- the gate can over-trigger but never under-trigger"), because the two
# shells disagree and only one of them is safe to be wrong about. In PowerShell ``*>`` is
# the all-streams redirect, so the program arrives on stdin and must be scanned. In bash
# ``*`` is a GLOB that expands to filenames, so the first becomes the script -- measured:
# ``python *> out`` runs the globbed file, and it still does with a here-string present.
# Reading ``*>`` as a redirect therefore over-triggers under bash, which costs a denial
# of a command combining a glob-redirect with a payload-bearing carrier; reading it as a
# positional under PowerShell lets a credential mint through. Recorded as an accepted
# residual rather than left implicit.
#
# Matched on the RAW token because ``_normalize_operand`` leaves the descriptor behind
# (``2>&1`` -> ``2``, ``{fd}>&1`` -> ``{fd}``), which reads as an ordinary file name.
# The brace form requires a real identifier inside: ``{a,b}`` is a brace EXPANSION the
# shell resolves before redirect parsing, and must not be mistaken for a descriptor.
# No token matching this is ever a positional argument in the shell that spells it.
#
# No ``\A`` anchor: ``.match(raw, pos)`` anchors at *pos*, which is how a word holding a
# chain of glued redirects is walked in one pass instead of being re-sliced per operator.
_OUTPUT_REDIRECT_RE = re.compile(r"(?:\d+|&|\*|\{[A-Za-z_][A-Za-z0-9_]*\})?>{1,2}[&|!]?")
# The same descriptor vocabulary, widened to INPUT redirects and process
# substitution, for use where a redirect ENDS an argument list rather than hiding
# a program. Testing only the first character missed every descriptor-prefixed
# spelling (``2>``, ``&>``, ``{fd}>``, ``1>``), which is exactly where a
# redirection is most often written -- so the descriptor read as an ordinary
# refspec and the command after the redirect was absorbed as arguments.
_REDIRECT_START_RE = re.compile(
    r"(?:\d+|&|\*|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:>{1,2}[&|!]?|<{1,3})"
)
# ``X=kirocrew; $X token`` assigns the program name to a variable and invokes it
# through the expansion, so neither the literal name nor the expansion alone looks
# dangerous.  The assignment and the use are in the SAME command text, so the
# literal can be substituted back before any comparison.
_LOCAL_ASSIGN_RE = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.DOTALL)
_VAR_USE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


_COMPUTED_VALUE_RE = re.compile(r"\$\(|`|\$\{[^}]*\}")


def _is_computed_value(value: str) -> bool:
    """True if an assignment's right-hand side is produced by a substitution."""
    return bool(_COMPUTED_VALUE_RE.search(value))


def _protected_name_in_substitution(tokens: "list[str]", start: int) -> str:
    """The protected program name a substitution starting at *start* could produce.

    Scans forward until the substitution closes (``shlex`` splits it across tokens
    because it splits on whitespace only) and returns the product name, or a
    by-name kill program, if either appears inside it.  Returns "" when neither does.

    The depth is read from the shared quote-aware walk, with the quote state
    carried ACROSS tokens because ``shlex`` splits on whitespace and a quoted word
    can span one. A private ``str.count`` walk closed the substitution at a QUOTED
    ``)`` and stopped scanning there, so a name hidden after it was never seen --
    an UNDER-deny for this rule, not the over-deny a previous audit of this line
    recorded.
    """
    depth = 0
    state = 0
    ansi = False
    for token in tokens[start:]:
        walk = _shell_quote_walk(token, state=state, ansi=ansi)
        depth += walk.paren_delta
        state, ansi = walk.end_state, walk.end_ansi
        m = _SELF_NAME_RE.search(token)
        if m:
            return m.group(0)
        for verb in _KILL_BY_NAME_PROGRAMS:
            if verb in token:
                return verb
        if depth <= 0 and state == 0 and token is not tokens[start]:
            break
    return ""


def _split_glued_operators(tokens: "list[str]") -> "list[str]":
    """Split tokens on control operators glued to their neighbours.

    ``shlex`` splits on whitespace only, so ``X=<name>;$X`` arrives as one token and an
    assignment glued to the command that uses it is invisible to both.  Splitting keeps
    the operator itself as a token so argv-boundary logic still sees it.
    """
    out: list[str] = []
    for token in tokens:
        # ONLY split a token that begins with an assignment.  Splitting any token
        # carrying a separator would destroy a QUOTED target -- ``shlex`` has already
        # removed the quotes, so ``pkill -f '[;]*<name>'`` arrives as the single token
        # ``[;]*<name>`` and is indistinguishable from a real separator at this point.
        # The reported evasion is specifically an assignment glued to its use, so that
        # is the only shape split here.
        if not _LOCAL_ASSIGN_RE.match(token) or not _CONTROL_OPERATOR_RE.search(token):
            out.append(token)
            continue
        for piece in _CONTROL_OPERATOR_RE.split(token):
            if piece:
                out.append(piece)
            out.append(";")
        if out and out[-1] == ";":
            out.pop()
    return out


_FUNC_DEF_RE = re.compile(r"\A(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)")


def _resolve_function_aliases(tokens: "list[str]") -> "list[str]":
    """Substitute a shell FUNCTION name with the protected program it forwards to.

    ``x(){ <name> "$@";}; x <verb>`` never puts the program and the verb in one argv --
    the function body holds the program and the call site holds the verb.  A function
    whose body invokes a protected program is therefore treated as an alias for it, so
    the ordinary argv checks see ``<name> <verb>`` at the call site.

    Only a LITERAL body is inspected, and only the program it invokes is carried over;
    no attempt is made to model parameter positions.  Over-approximating is the safe
    direction -- the alias only matters where the function is called as a program.
    """
    aliases: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        # ``alias x=<name>`` then ``x <verb>`` is the same evasion as a function
        # wrapper: the definition holds the program and the call site holds the verb.
        if tokens[i] == "alias" and i + 1 < len(tokens):
            spec = _LOCAL_ASSIGN_RE.match(tokens[i + 1])
            if spec and spec.group(2):
                target = _normalize_operand(spec.group(2))
                if _is_self_program(target):
                    aliases[spec.group(1)] = "kirocrew"
                elif _program_basename(target) in _KILL_BY_NAME_PROGRAMS:
                    aliases[spec.group(1)] = _program_basename(target)
        m = _FUNC_DEF_RE.match(tokens[i])
        if m:
            # Walk the body until the closing brace, taking the first protected program.
            for body_token in tokens[i + 1 :]:
                base = _program_basename(body_token)
                if _is_self_program(body_token):
                    aliases[m.group(1)] = "kirocrew"
                    break
                if base in _KILL_BY_NAME_PROGRAMS:
                    aliases[m.group(1)] = base
                    break
                if "}" in body_token:
                    break
        i += 1
    if not aliases:
        return tokens
    return [aliases.get(tk, tk) for tk in tokens]


# ``${VAR:0}`` / ``${VAR^^}`` / ``${VAR/x/y}`` and friends TRANSFORM a variable's own
# value.  The ``:-``/``:+``/``:=``/``:?`` default forms are deliberately NOT matched here --
# those carry a literal of their own and are handled by ``_resolve_param_defaults``.
_PARAM_TRANSFORM_RE = re.compile(
    r"\$\{([A-Za-z_]\w*)(?::(?![-+=?])[^}]*|[#%^,/@][^}]*)\}"
)


# ``${!VAR}`` expands to the value of the variable NAMED by ``VAR`` -- one more hop
# than ``${VAR}``, through the same table.
_INDIRECT_VAR_USE_RE = re.compile(r"\$\{!([A-Za-z_][A-Za-z0-9_]*)\}")


def _mint_verb_in_substitution(tokens: "list[str]", idx: int) -> bool:
    """True if the substitution starting at *idx* prints the credential-minting verb.

    The program-name twin of this check answers "does this compute a protected
    PROGRAM?".  This one answers "does it compute the VERB?", for the spelling that
    hides that half instead (``T=$(printf <verb>); <name> $T``).
    """
    joined = " ".join(tokens[idx:])
    for body in _substitution_bodies(joined):
        if any(_is_mint_verb(word) for word in body.split()):
            return True
    return False


def _resolve_local_assignments(tokens: "list[str]") -> "list[str]":
    """Substitute ``$VAR`` uses with a literal assigned earlier in the same command.

    Only LITERAL right-hand sides are tracked, and only assignments that appear in
    this same command text -- there is no attempt to model the ambient environment.
    That is enough for the evasion it closes, where the attacker must supply both
    halves themselves.

    A value that REFERENCES an already-tracked variable is expanded before it is
    classified, so a name assembled across several assignments still resolves to the
    literal the shell will run.
    """
    values: dict[str, str] = {}
    out: list[str] = []
    # ``X=<name>;$X <verb>`` glues the assignment and the next command into ONE token,
    # because ``shlex`` splits on whitespace only.  Split on top-level control operators
    # first so the assignment is seen as an assignment and the use as a use.
    tokens = _split_glued_operators(tokens)
    for idx, token in enumerate(tokens):
        assign = _LOCAL_ASSIGN_RE.match(token)
        # ``NAME+=tail`` APPENDS, and the pattern above cannot match it at all (``+`` is
        # not a name character), so an appended PROGRAM word was invisible here: `F=fi;
        # F+=nd; $F <fenced> -exec cat {} +` ran a `find` this resolver never saw, while
        # its single-assignment twin `F=fin; ${F}d` -- closed in an earlier round --
        # denied (found in review). Seven spellings did it, including an empty initial
        # value, repeated appends, a quoted tail and an append feeding a braced use.
        #
        # Append is in the same closed class as those: the tail is a LITERAL, so what the
        # shell will run is decided by the text alone. That is what separates it from
        # `$(printf find)`, whose value needs a program run and is out of scope here.
        #
        # `_SHELL_ASSIGN_RE` is REUSED rather than a second append pattern being added:
        # it already carries the optional ``+`` group for this exact problem on the
        # path-tracking side, so there is one spelling of the rule in the module. It is
        # matched only in the branch the assignment pattern rejects, which keeps this
        # additive -- renumbering the shared pattern's groups instead would have rewritten
        # every existing reader of them for no behavioural gain.
        append = None if assign else _SHELL_ASSIGN_RE.match(token)
        if append and append.group(2):
            name, tail = append.group(1), append.group(3)
            if values and "$" in tail:
                # Same reason the assignment path expands first: a tail built from an
                # already-tracked variable is a literal once expanded, and left
                # unexpanded it reads as computed and the append is dropped.
                tail = _VAR_USE_RE.sub(
                    lambda m: values.get(m.group(1) or m.group(2), m.group(0)), tail
                )
            if _is_computed_value(tail):
                # No literal to concatenate. Over-approximate exactly as the assignment
                # path does: the value only matters where it is later used as a program,
                # so a wrong guess there is a refusal rather than a bypass.
                produced = _protected_name_in_substitution(tokens, idx)
                if produced:
                    values[name] = produced
                out.append(token)
                continue
            piece = tail.strip("\"'").rstrip(";&|")
            # `F=` records nothing, so an append to an unset name starts from empty
            # rather than being discarded -- that was one of the seven spellings, and
            # bash builds the value the same way.
            values[name] = values.get(name, "") + piece
            out.append(token)
            continue
        if assign and values and "$" in (assign.group(2) or ""):
            # A new value may be built FROM a variable already tracked
            # (``x=p; x=${x}kill``).  Expanding before classifying is what makes the
            # result a literal at all: left unexpanded it looks computed, the earlier
            # binding stays in place, and the reassignment is silently ignored.
            expanded = _VAR_USE_RE.sub(
                lambda m: values.get(m.group(1) or m.group(2), m.group(0)),
                assign.group(2),
            )
            if expanded != assign.group(2):
                token = f"{assign.group(1)}={expanded}"
                assign = _LOCAL_ASSIGN_RE.match(token)
        if assign and _is_computed_value(assign.group(2)):
            # ``X=$(printf <name>); $X <verb>`` COMPUTES the value, so there is no
            # literal to carry forward.  Resolve it conservatively instead: if the
            # substitution that produces it names a protected program anywhere, treat
            # the variable as holding that name.  Over-approximating here is the safe
            # direction -- the value only matters when ``$X`` is later used as a
            # program, and a wrong guess there is a refusal, not a bypass.
            produced = _protected_name_in_substitution(tokens, idx)
            if produced:
                values[assign.group(1)] = produced
            elif _mint_verb_in_substitution(tokens, idx):
                # ``T=$(printf <verb>); <name> $T`` computes the VERB rather than the
                # program.  Same reasoning as the program case: the value only matters
                # where it is later used, so binding it to the verb is the safe
                # over-approximation.
                values[assign.group(1)] = "token"
            out.append(token)
            continue
        if assign and assign.group(2):
            # A trailing ``;``/``&&`` belongs to the command structure, not the
            # value: ``shlex`` splits on whitespace only, so ``X=name;`` arrives
            # with the operator attached.
            value = assign.group(2).strip("\"'").rstrip(";&|")
            if value:
                values[assign.group(1)] = value
            out.append(token)
            continue
        if values and "$" in token:
            # ``${!V}`` is INDIRECT: it expands to the value of the variable NAMED by
            # ``V``, so resolving it takes two hops through the same table.  Done before
            # the ordinary substitution so what remains afterwards is a plain literal.
            # A TRANSFORMATION on a tracked variable (``${K:0}``, ``${K^^}``, ``${K/x/y}``)
            # still expands to something derived from the tracked value, but none of those
            # spellings are a plain ``${K}``.  Resolved to the value itself: the
            # transformation is not modelled, and over-approximating here is the safe
            # direction for the same reason it is for a computed value -- the result only
            # matters where it is used as a program or verb, and a wrong guess there is a
            # refusal, not a bypass.  The ``:-``/``:+``/``:=``/``:?`` DEFAULT forms are
            # excluded: they carry their own literal and are resolved separately.
            token = _PARAM_TRANSFORM_RE.sub(
                lambda m: values.get(m.group(1), m.group(0)), token
            )
            token = _INDIRECT_VAR_USE_RE.sub(
                lambda m: values.get(values.get(m.group(1), ""), m.group(0)), token
            )
            token = _VAR_USE_RE.sub(
                lambda m: values.get(m.group(1) or m.group(2), m.group(0)), token
            )
        out.append(token)
    return out


# ``${VAR:-kirocrew}`` / ``${VAR:+kirocrew}`` / ``${VAR-kirocrew}`` carry a LITERAL
# program name that the shell substitutes in.  The literal is the program that can
# actually run, so it is what the comparison must see.
_PARAM_DEFAULT_RE = re.compile(r"\$\{[^{}:+=?-]*(?::?[-+=?])([^{}]*)\}")


def _resolve_param_defaults(token: str) -> str:
    """Replace ``${VAR:-literal}`` style expansions with their literal text.

    Only the LITERAL branch is resolved -- that is the spelling that hands the shell
    a runnable program name without the name appearing bare in the command.  A
    variable-only expansion (``$X``, ``${X}``) carries no literal and is left alone;
    that case is covered by the raw-text half of the union, not here.
    """
    previous = ""
    while token != previous:
        previous = token
        token = _PARAM_DEFAULT_RE.sub(lambda m: m.group(1), token)
    return token


# NO depth cap.  A cap is a bypass: whatever number is chosen, one more nesting
# level defeats it.  Termination is guaranteed structurally instead -- a payload is
# a proper substring of the token that carried it, so it is STRICTLY SHORTER than
# its parent's source text, and a chain of strictly shorter strings is finite.  A
# visited set stops sibling wrappers re-walking the same payload.
# ``-c`` may arrive inside a COMBINED short-flag cluster: ``bash -xc '<script>'``
# and ``sh -ec '<script>'`` both run the next token as a script.  Matching only
# the exact spellings ``-c``/``-lc`` leaves every other cluster as a bypass.
# LOWERCASE only, deliberately: widening this class to ``[A-Za-z]`` made an
# uppercase-clustered decoy (``-Cc``) the FIRST flag stop, which ate the stop
# through which a following ``--command``'s payload was found (Opus review lane
# on #8197).  Uppercase-clustered spellings are covered instead by
# ``_SHELL_COMMAND_GLUED_RE`` (glued) and the every-carrier sweep (spaced), so
# the flag stop set stays byte-identical to what it always was.  The
# protection is for the CASE-PRESERVING callers (the alt-traversal pass): the
# deny tiers lowercase their input first, where ``-Cc`` folds to ``-cc`` and
# eats the ``--command`` stop exactly as it always has -- a pre-existing
# residual there, not one this pattern can close.
_SHELL_COMMAND_FLAG_RE = re.compile(r"\A-[a-z]*c[a-z]*\Z")


# ``-c`` takes a VALUE, so a getopt-convention shell (``ksh``, ``zsh``) ends
# option parsing at the ``c`` and runs everything GLUED after it -- and the
# reading is deliberately OVER-approximated for the shells whose own parsers
# keep consuming cluster letters (bash's ``parse_shell_options``, dash's
# ``options()``), because extraction must cover the strictest interpreter the
# command could reach.  ``sh -c'rg . /path'`` reaches the token walk as
# ``-crg . /path`` once ``shlex`` strips the quotes.  ``_SHELL_COMMAND_FLAG_RE``
# anchors the WHOLE token as a bare flag cluster, so a token carrying the
# payload's own characters was rejected and the payload never yielded (#8197).
# This companion pattern CAPTURES the glued remainder instead of weakening the
# flag pattern where it is used for pure flag detection.  Non-greedy, so the
# split happens at the FIRST lowercase ``c`` (``-ec'x'`` runs ``x`` under the
# getopt convention; the letters before the ``c`` are flags, either case:
# ``-Cc'x'`` clusters noclobber before the ``c``).
_SHELL_COMMAND_GLUED_RE = re.compile(r"\A-[A-Za-z]*?c(.+)\Z", re.DOTALL)


# Variables that conventionally hold a shell (or the running script) path.  Piping
# into ``$SHELL`` runs the piped text exactly as piping into ``bash`` does, and the
# expansion hides the program name from any basename comparison.
_SHELL_VAR_NAMES = frozenset({"shell", "bash", "zsh", "ksh", "0", "bash_execution_string"})
_SHELL_VAR_RE = re.compile(r"\A\$\{?([A-Za-z_0-9]+)\}?\Z")


def _is_shell_variable_reference(token: str) -> bool:
    """True if *token* is a variable that conventionally expands to a shell."""
    m = _SHELL_VAR_RE.match(token.strip(_SHELL_WRAPPER_CHARS))
    if m is None:
        return False
    return m.group(1).lower() in _SHELL_VAR_NAMES


def _pipes_into_evaluator(tokens: "list[str]") -> bool:
    """True if this command pipes into a shell or evaluator.

    ``echo <name> <verb> | sh`` produces the dangerous command as TEXT and then
    hands it to something that runs it, so the "arguments are just data" reasoning
    does not hold: the data IS the command.
    """
    seen_pipe = False
    for token in tokens:
        if "|" in token:
            seen_pipe = True
        if seen_pipe and (
            _program_basename(token) in _NESTED_SHELL_PROGRAMS
            or _program_basename(token) in _NESTED_SHELL_VERBS
            or _program_basename(token) == "xargs"
            or _is_shell_variable_reference(token)
        ):
            return True
    return False


# Constructs by which a text-processing tool RUNS a command rather than printing it:
# ``awk``'s ``system()`` and pipe-to-command, and GNU ``sed``'s ``e`` flag.
_SCRIPT_EXECUTES_RE = re.compile(
    r"system\s*\(|\|\s*[\"']|\|&|print\s*\||\bclose\s*\(|/e\b|\be\s*$"
)


def _data_consumer_command_disqualified(tokens: "list[str]") -> bool:
    """True where NO token in *tokens* can claim the data-consumer exemption.

    The three guards collected here read ONLY *tokens*, so their answer is a
    property of the whole command and is the same for every token in it.  They
    live in one function so a caller holding a fixed argv can charge them ONCE
    instead of once per candidate token: :func:`_data_consumer_exempt` is called
    per payload inside a loop over a fixed argv, and the ``_SCRIPT_EXECUTES_RE``
    sweep below is itself O(len(tokens)), so re-asking made the enclosing walk
    quadratic in payload count (#8595 -- 18k payloads, ~293s).

    Splitting them out cannot change any verdict: each is a pure function of
    *tokens* and each REFUSES the exemption, so hoisting alters only how often
    the same answer is computed, never what it is.
    """
    if _pipes_into_evaluator(tokens):
        return True
    # ``$(printf <name>) <verb>`` puts the consumer INSIDE a substitution that occupies
    # program position, so its OUTPUT is what runs -- the words are not inert data.
    if tokens and tokens[0].lstrip("\"'").startswith("$(") or (
        tokens and tokens[0].lstrip("\"'").startswith("`")
    ):
        return True
    # A "data consumer" that can EXECUTE is not one for this command.  ``awk`` has
    # ``system()`` and pipe-to-command; GNU ``sed`` has the ``e`` flag.  The exemption is
    # withdrawn per-command when the script text carries such a construct, rather than
    # dropping ``awk`` from the list entirely -- that would also refuse ordinary
    # ``awk '{print $1}' <file>``, and the list is deliberately a denylist of consumers so
    # that a mistake here costs a false positive, never a bypass.
    if any(_SCRIPT_EXECUTES_RE.search(tok) for tok in tokens):
        return True
    return False


def _data_consumer_exempt(
    index: int,
    token: str,
    programs: "list[str]",
    tokens: "list[str]",
    *,
    command_disqualified: "bool | None" = None,
) -> bool:
    """True if *token* is an ARGUMENT of a command that treats arguments as data.

    ``echo <name> <verb>`` prints two words -- a mention, not an invocation.

    The exemption is refused in two cases:

    * the token itself carries a control operator (``echo foo;kirocrew>/tmp/x``).
      ``shlex`` splits on whitespace only, so such a token is attributed to the
      PRECEDING command while the part after the operator is a new command that
      really runs.
    * the command pipes into a shell or evaluator (``echo … | sh``), where the
      printed text is executed rather than displayed.

    Inheriting the exemption in either case would turn a precision fix into a
    bypass.
    """
    if index <= 0:
        return False
    if _CONTROL_OPERATOR_RE.search(token):
        return False
    # Command-level guards, hoisted into ``_data_consumer_command_disqualified``.
    # *command_disqualified* lets a caller iterating one fixed argv charge them
    # once; ``None`` means "compute them here", which is what every caller that
    # asks about a single token does, so their behaviour is unchanged.
    if command_disqualified is None:
        command_disqualified = _data_consumer_command_disqualified(tokens)
    if command_disqualified:
        return False
    return programs[index] in _DATA_CONSUMER_PROGRAMS


def _argv_programs(tokens: "list[str]") -> "list[str]":
    """For each token, the program name of the command that token belongs to.

    Walks the argv tracking command boundaries (``_ends_argv``) and skipping
    leading ``VAR=value`` assignments, which precede the program rather than being
    it.  Used to ask "what command is this name an argument OF?" -- the difference
    between ``echo <name> <verb>`` (data) and ``ssh host <name> <verb>`` (executed).
    """
    programs: list[str] = []
    current = ""
    expect_program = True
    for token in tokens:
        if expect_program and token and not ENV_ASSIGNMENT_RE.match(token):
            current = _program_basename(token)
            expect_program = False
        programs.append(current)
        if _ends_argv(token):
            current = ""
            expect_program = True
    return programs


def _is_shell_command_flag(token: str) -> bool:
    """True if *token* is the shell flag whose next argument is a script."""
    return token == "--command" or bool(_SHELL_COMMAND_FLAG_RE.match(token))


def _glued_shell_command_payload(token: str) -> "str | None":
    """The script glued onto a ``-c`` short-option cluster, or ``None``.

    The quoted spellings (``-c'x'``, ``-c"x"``) normally lose their quotes to
    ``shlex`` before tokens reach here, but the fallback tokenizer keeps them, so
    one surviving outer quote layer is removed -- the payload comes back exactly
    as the spaced ``-c 'x'`` spelling would deliver it.  Only a layer that is
    provably a WRAPPER is removed: when the quote character also occurs inside
    the payload, the first and last characters may be two unrelated quotes
    (``'a' 'b'``), and stripping them would corrupt the reading.
    """
    match = _SHELL_COMMAND_GLUED_RE.match(token)
    if match is None:
        return None
    payload = match.group(1)
    if (
        len(payload) >= 2
        and payload[0] == payload[-1]
        and payload[0] in "'\""
        and payload[0] not in payload[1:-1]
    ):
        payload = payload[1:-1]
    return payload or None


def _is_glued_shell_command_token(token: str) -> bool:
    """MIRROR of the inline glued stop table in :func:`_nested_shell_payloads`.

    That table is built from the per-token payload cache
    (``glued_payloads[i] is not None``) rather than through a predicate call,
    so the payloads many shell tokens share are extracted once.  This mirror
    exists so the stop condition can be pinned directly by the predicate test;
    both spellings reduce to the same expression below, which is what keeps
    them from drifting.
    """
    return _glued_shell_command_payload(token) is not None


def _shell_c_carrier_glued(token: str) -> "str | None":
    """The remainder after the first lowercase ``c`` of a short-option carrier.

    The LOOSE recognition: ANY characters may precede the ``c`` (``-1c…``, a
    long cluster like ``-onoclobber`` glued ahead of it), because a
    getopt-convention parser consumes unknown letters rather than stopping, and
    because extraction deliberately over-approximates -- a junk payload
    re-tokenizes to text that matches no rule, while a missed one is a command
    nothing examines.  Returns ``""`` for a bare carrier (the payload is the
    NEXT token), or ``None`` when *token* is not a carrier at all.  Its one
    consumer is the every-carrier sweep in :func:`_nested_shell_payloads`.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return None
    position = token.find("c", 1)
    if position == -1:
        return None
    return token[position + 1 :]


# How far back from the end of a carrier's leading letter region the ambiguous
# ``c``-split scan reaches.  The true split's distance to the region's end is
# the payload's FIRST-WORD length -- a real program name -- so 64 covers any
# rule-relevant program with room to spare, while keeping the per-token scan
# O(window) and immune to cluster padding (padding only adds fake splits
# farther from the end, whose program words are runs of flag letters).
_CARRIER_SPLIT_WINDOW = 64


def _shell_c_carrier_payloads(token: str) -> "list[str]":
    """Every plausible split of a glued ``-c`` carrier, fold-ambiguity safe.

    The deny tiers LOWERCASE input before the walk, so ``-Cc'<script>'`` (a real
    zsh/ksh spelling: ``-C`` is noclobber, ``-c`` takes the script) folds to
    ``-cc<script>`` and the first-``c`` split misreads the boundary -- the
    payload comes back as ``c<script>``, whose program word matches no rule
    (found by the GPT 5.6 CI lane on this change; the attacker can also write
    the folded spelling directly).  Which ``c`` was the option letter is
    unrecoverable after the fold, so the split is over-approximated: the FIRST
    ``c`` (getopt-correct for the unfolded spelling), plus, for every maximal
    run of consecutive ``c``\\ s, the split after the run's LAST and
    SECOND-TO-LAST ``c``.  The last-``c`` split reads the run as all flags; the
    second-to-last covers a payload whose own program name begins with one
    ``c`` (``cat``, ``curl``, ``cp``, ``chmod``, ``crontab`` -- no rule-covered
    program carries two).  Split positions are BOUNDED to the last
    ``_CARRIER_SPLIT_WINDOW`` characters of the leading letter region, which
    keeps the function linear WITHOUT opening a padding bypass: the true
    split's distance to the region's end equals the payload's first-word
    length -- a real program name, never longer than the window -- while
    cluster padding only pushes FAKE splits farther from the end, and a fake
    split's program word is a run of flag letters that matches no rule.
    Without the bound, a ~3 KB ``-acac…`` token made the candidate set
    quadratic and the synchronous deny scan outlived the loop watchdog (found
    by the GPT 5.6 CI lane).  The first-``c`` split is always yielded
    regardless of the window: it is the LONGEST suffix, so the unanchored
    regex tier sees every shorter reading as a substring of it.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return []
    first = token.find("c", 1)
    if first == -1:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(payload: str) -> None:
        if payload and payload not in seen:
            seen.add(payload)
            out.append(payload)

    _add(token[first + 1 :])
    # Option letters are letters: the first non-letter character ends the
    # region where a ``c`` could have been the option that takes the script.
    # A ``c`` beyond it belongs to the payload's own text and splitting there
    # would shred the payload.
    region_end = 1
    limit = len(token)
    while region_end < limit:
        ch = token[region_end]
        if not ("a" <= ch <= "z" or "A" <= ch <= "Z"):
            break
        region_end += 1
    index = max(1, region_end - _CARRIER_SPLIT_WINDOW)
    while index < region_end:
        if token[index] == "c":
            run_start = index
            while index + 1 < region_end and token[index + 1] == "c":
                index += 1
            _add(token[index + 1 :])
            if index > run_start:
                _add(token[index:])
        index += 1
    return out


def _is_herestring_token(token: str) -> bool:
    """True where the herestring scan in :func:`_nested_shell_payloads` stops.

    Covers the spaced operator and the operator glued to its payload.  A
    SEPARATE stop from the command flag (they used to share one predicate):
    with one shared table a herestring token EATS the stop through which a
    later ``-c``'s payload was found -- ``bash <<<'x' -c '<script>'`` yielded
    only ``x`` while a real shell runs the script.  Independent tables scan
    each spelling in its own right, which is purely additive.
    """
    return token == "<<<" or token.startswith("<<<")


def _is_env_split_flag(token: str) -> bool:
    """True where the ``env -S`` scan in :func:`_nested_shell_payloads` stops."""
    flag = token.lower()
    return (
        flag in {"-s", "--split-string"}
        or (flag.startswith("-s") and len(token) > 2)
        or flag.startswith("--split-string=")
    )


def _is_not_double_dash(token: str) -> bool:
    """True where the ``--`` skip in :func:`_nested_shell_payloads` stops.

    Named for the same reason the other two stop conditions are: the precomputed
    index and the token the caller then reads must not drift apart.
    """
    return token != "--"


def _next_stop_indexes(tokens: "list[str]", is_stop: "Callable[[str], bool]") -> "list[int]":
    """For each index, the first index at or after it where *is_stop* holds.

    One backward pass, so a forward scan per program token becomes a lookup and the
    caller stays linear in token count.  Position ``len(tokens)`` means "no such
    token", which reads the same as the original loops running off the end.
    """
    limit = len(tokens)
    table = [limit] * (limit + 1)
    for index in range(limit - 1, -1, -1):
        table[index] = index if is_stop(tokens[index]) else table[index + 1]
    return table


def _nested_shell_payloads(
    tokens: "list[str]",
    *,
    allow_join: bool = True,
    joined_out: "set[str] | None" = None,
) -> "list[str]":
    """Literal shell-script payloads carried as an argument inside *tokens*.

    Covers ``sh -c '<script>'`` / ``bash -c '<script>'`` (the payload is the
    first non-flag token after ``-c``) and ``eval '<script>'``.  Only LITERAL
    payloads are returned -- ``eval "$CMD"`` carries no visible script, and that
    case is covered by the regex tier running alongside this floor rather than by
    this function.

    *allow_join* suppresses the ``eval`` argument join, and *joined_out* collects
    the joined payloads this call produced. Both exist for
    :func:`_shell_payload_walk`, which must not let a JOINED frame join again: the
    joined text is strictly shorter than its parent, so it becomes a frame of its
    own, and if that frame joins too the walk builds a chain of shrinking suffixes
    -- N frames each costing an O(N) tokenize and an O(N) join. Measured on
    ``"eval " * 1280``: 65 s and growing ~5x per doubling, against 0.13 s before
    the join existed, which stalls the synchronous permission gate long enough for
    the watchdog to fire. Declining the second join costs no detection, because
    the join FUSES already-dequoted words in one step -- ``eval eval 'git' 'push
    origin main'`` is fused to ``git push origin main`` by the first join, so the
    publish is visible at the first joined frame and the chain only re-derived
    suffixes of an answer already in hand.
    """
    payloads: list[str] = []
    # Both scans below look for the FIRST token after a program that satisfies a stop
    # predicate, handle it, and stop.  Walking forward per program made the function
    # QUADRATIC in token count: in a run of interpreter tokens with no flag among them
    # every one of them re-walks the whole tail, so a command padded with them stalls
    # the synchronous permission gate (measured: 13.2 s for 16 000 tokens, ~4x per
    # doubling).  The first-stop index is precomputed once per predicate in a single
    # backward pass instead, which makes the whole function O(N) while returning the
    # identical payload list -- the loops' only exits were that first stop token or the
    # end of the list, so nothing else can change.
    env_stop = _next_stop_indexes(tokens, _is_env_split_flag)
    # The herestring and the GLUED ``-c`` spelling each get their OWN stop table
    # rather than sharing the flag's: with a shared table, whichever spelling
    # comes first EATS the stop through which a later spelling's payload was
    # found -- ``bash <<<'x' -c '<script>'`` yielded only ``x``.  Splitting the
    # tables fixes the CROSS-spelling case; WITHIN one class each table still
    # reads only its first stop per shell token, which for short-cluster ``-c``
    # carriers is closed by the every-carrier sweep at the bottom of this
    # function.  Two stated residuals: ``--command`` carriers stay
    # first-stop-only (the sweep is scoped to short clusters), and herestrings
    # keep a first-occurrence residual (``bash <<<'a' <<<'b'`` yields ``a``; a
    # real shell applies the LAST redirect).
    #
    # The flag, herestring and glued stops are SPARSE (sorted index lists read
    # through bisect, payloads cached in dicts keyed by stop position), built in
    # one forward pass gated on a cheap prefix check.  Dense per-token tables
    # (and a regex call per token to fill them) made this function's constant
    # measurably heavier than the merge-base on interpreter-run shapes, and the
    # pre-existing linearity tests bound ABSOLUTE seconds on CI runners, not
    # growth rate.  A carrier-free command now allocates three empty lists and
    # runs one `startswith` per token -- less than the merge-base's own
    # per-token predicate regex.  Payloads are cached at their stop position
    # because many shell tokens can share one stop -- re-extracting there copies
    # the same length-M substring once per shell token, O(N*M) on
    # ``["bash"]*N + ["-c<payload>"]`` (GPT 5.6 lane); the cached string is one
    # object, so downstream dedup-set hashing stays linear too.
    limit = len(tokens)
    flag_stops: "list[int]" = []
    herestring_stops: "list[int]" = []
    glued_stops: "list[int]" = []
    glued_at: "dict[int, str]" = {}
    herestring_tail_at: "dict[int, str]" = {}
    for index, token in enumerate(tokens):
        if token.startswith("-"):
            if _is_shell_command_flag(token):
                flag_stops.append(index)
            if not token.startswith("--"):
                glued = _glued_shell_command_payload(token)
                if glued is not None:
                    glued_stops.append(index)
                    glued_at[index] = glued
        elif token.startswith("<<<"):
            herestring_stops.append(index)
            if token != "<<<":
                herestring_tail_at[index] = token[3:]

    def _first_stop_at_or_after(stops: "list[int]", start: int) -> int:
        position = bisect.bisect_left(stops, start)
        return stops[position] if position < len(stops) else limit

    # ``--`` runs are precomputed for the same reason: a long run of them after the
    # command flag is walked once per program token otherwise, which is quadratic even
    # though the two scans above are not.
    past_dashes = _next_stop_indexes(tokens, _is_not_double_dash)
    # ``eval``'s argument join is bounded to one per walk; see the verb branch.
    joined_eval = False
    first_shell: "int | None" = None
    for i, token in enumerate(tokens):
        base = _program_basename(token)
        # A shell reached through a VARIABLE (``$SHELL -c '<payload>'``) runs the
        # payload exactly as a named shell does.  The recognizer already used for the
        # ``| $SHELL`` evaluator sink applies here too.
        if base in _NESTED_SHELL_PROGRAMS or _is_shell_variable_reference(token):
            if first_shell is None:
                # Recorded here, where the shell test has already been paid,
                # so the every-carrier sweep below needs no second scan that
                # re-derives program basenames token by token.
                first_shell = i
            j = _first_stop_at_or_after(flag_stops, i + 1)
            if j < limit:
                # ``bash -c -- '<script>'`` is legal: ``--`` ends option parsing
                # and the script is the token AFTER it.  Skip any run of them.
                k = past_dashes[j + 1]
                if k < limit:
                    payloads.append(tokens[k])
            # A HERESTRING feeds the script on stdin instead of as an argument
            # (``bash <<< '<script>'``), so its text is a command just the same.
            # Both the spaced and glued spellings arrive here.
            h = _first_stop_at_or_after(herestring_stops, i + 1)
            if h < limit:
                tail = herestring_tail_at.get(h)
                if tail is not None:
                    payloads.append(tail)
                elif h + 1 < limit:
                    payloads.append(tokens[h + 1])
            # The glued ``-c`` spelling (``-c'<script>'``, one token once shlex
            # strips the quotes) is looked up independently as well.  An
            # all-alpha cluster like ``-ecfoo`` satisfies BOTH ``-c`` readings --
            # it matches the bare-flag pattern (yielding the next token, as
            # before) AND carries a glued remainder a real shell would run -- so
            # both payloads are yielded rather than picking one interpretation.
            g = _first_stop_at_or_after(glued_stops, i + 1)
            if g < limit:
                payloads.append(glued_at[g])
        elif base in _ENV_SPLIT_PROGRAMS:
            # ``env -S '<script>'`` / ``env --split-string '<script>'`` splits the
            # payload into a command and runs it, so its text is a command line.
            j = env_stop[i + 1]
            if j < limit:
                # ``is_denied`` lowercases its input, so compare case-insensitively:
                # the real flag is ``-S`` but it arrives here as ``-s``.
                flag = tokens[j].lower()
                if flag in {"-s", "--split-string"}:
                    if j + 1 < limit:
                        payloads.append(tokens[j + 1])
                elif flag.startswith("-s") and len(tokens[j]) > 2:
                    payloads.append(tokens[j][2:])
                elif flag.startswith("--split-string="):
                    payloads.append(tokens[j].split("=", 1)[1])
        elif base in _NESTED_SHELL_VERBS or token in _NESTED_SHELL_VERBS:
            # ``--`` ends option parsing, so ``eval -- '<script>'`` runs the token
            # AFTER it. Taking ``tokens[i + 1]`` blindly yielded the literal ``--``
            # as the payload and the real script was never walked. Reuse the same
            # precomputed run-skip the ``-c`` branch above uses, so this stays O(1)
            # rather than becoming the third forward walk this function was made
            # linear to remove.
            j = past_dashes[i + 1]
            if j < limit:
                payloads.append(tokens[j])
                # ``eval`` CONCATENATES all of its arguments with a space and
                # evaluates the RESULT, so a command split across several words is
                # one command line at run time while no single word looks like one.
                # Taking only the first argument let
                # ``eval '<program>' '<verb and args>'`` through: the hooks saw the
                # bare program name and the publish never appeared. The joined form
                # is added ALONGSIDE the first argument, so the single-argument
                # reading is unchanged.
                #
                # ``eval`` only. ``source``/``.`` take a FILE as their first
                # argument and pass the rest as positional parameters, so joining
                # them would invent a command line bash never runs.
                #
                # Joined at most ONCE per walk: a join is O(N), so one per verb
                # token would be quadratic. One is enough, because it runs to the
                # END of the token list and therefore already spans every later
                # verb's own suffix.
                verb = base if base in _NESTED_SHELL_VERBS else token
                if verb == "eval" and j + 1 < limit and not joined_eval and allow_join:
                    joined_eval = True
                    joined = " ".join(tokens[j:])
                    payloads.append(joined)
                    if joined_out is not None:
                        joined_out.add(joined)
    # ``bash<<<'<payload>'`` glues the program, the operator and the payload into ONE
    # token, so the program never appears as a token of its own for the walk above to
    # recognise.  Split on the operator and check the left half.
    for token in tokens:
        if "<<<" not in token:
            continue
        head, _, tail = token.partition("<<<")
        if tail and _program_basename(head) in _NESTED_SHELL_PROGRAMS:
            payloads.append(tail)
    # EVERY ``-c`` carrier past the first shell token is swept, not only the
    # first-stop one.  The stop tables above read one token per spelling class,
    # so a decoy that satisfies the same predicate EATS the stop through which a
    # later carrier's payload was found (``ksh -onoclobber -c'<script>'`` stops
    # the glued table at ``-onoclobber``; ``bash -c 'a' -c '<script>'`` stops the
    # flag table at the first ``-c``).  The loose recognition here is the one the
    # alt-traversal pass's deleted local extractor used -- any prefix before the
    # first lowercase ``c`` (``-1c…``), payload glued or in the next token -- and
    # the sweep is a single forward pass from the first shell token (recorded
    # for free inside the main loop above), so the function stays O(N).
    # Additive only: a payload already collected above is not re-appended, so
    # consumers pinning exact payload lists are unchanged.
    if first_shell is not None:
        collected = set(payloads)
        for index in range(first_shell + 1, limit):
            token = tokens[index]
            if not token.startswith("-") or token.startswith("--"):
                continue
            glued = _shell_c_carrier_glued(token)
            if glued is None:
                continue
            if glued:
                # Glued payload: yield EVERY plausible split, not just the
                # first-``c`` one -- after the deny tiers' case fold, which
                # ``c`` took the argument is unrecoverable, and the wrong
                # split hid a protected payload behind one junk letter
                # (``-Cc'<script>'`` folded to ``-cc<script>``).
                for candidate in _shell_c_carrier_payloads(tokens[index]):
                    if candidate not in collected:
                        collected.add(candidate)
                        payloads.append(candidate)
            else:
                k = past_dashes[index + 1]
                bare_next = tokens[k] if k < limit else None
                if bare_next and bare_next not in collected:
                    collected.add(bare_next)
                    payloads.append(bare_next)
    # ``a=(<name> <verb>); "${a[@]}"`` runs the array's elements AS a command line.  The
    # expansion is one token, so the argv checks have no adjacent operands to compare --
    # the joined elements are handed to the payload walk instead, which re-tokenizes them.
    arrays = _array_assignments(tokens)
    if arrays:
        programs = _argv_programs(tokens)
        for index, token in enumerate(tokens):
            # Only an expansion in COMMAND position runs the elements.  As an ARGUMENT
            # they are just words -- ``echo ${a[@]}`` prints them -- so requiring the
            # expansion to be its own command's program keeps the data cases inert.
            if index >= len(programs) or programs[index] != token:
                continue
            for match in _ARRAY_EXPAND_RE.finditer(token):
                value = arrays.get(match.group(1))
                if value:
                    payloads.append(value)
    # GNU ``sed`` runs the REPLACEMENT of an ``s///e`` command as a shell command, so
    # that text is a payload.  It lives INSIDE one token, which is why withdrawing the
    # data-consumer exemption is not enough on its own: there are no two adjacent
    # operands for the argv checks to compare.
    for token in tokens:
        replacement = _sed_exec_replacement(token)
        if replacement:
            payloads.append(replacement)
    # A MULTIWORD alias replacement is a whole command line, not just a program name
    # (``alias x='kirocrew token'`` then ``x``), so hand it to the payload walk.
    for i, token in enumerate(tokens):
        if token == "alias" and i + 1 < len(tokens):
            spec = _LOCAL_ASSIGN_RE.match(tokens[i + 1])
            if spec and spec.group(2) and " " in spec.group(2).strip():
                payloads.append(spec.group(2))
    # A payload only matters if it looks like a command line rather than a bare
    # operand; a single word is already covered by the direct token scan.
    if _pipes_into_evaluator(tokens):
        # ``echo '<script>' | sh`` produces the command as TEXT and then hands it to
        # something that runs it, so the printed text is a payload exactly as a
        # ``-c`` argument is.
        # An escape can stand in for the separator (``printf '<name>\\040<verb>'``),
        # so the "is this a command line?" test is applied to the DECODED text -- otherwise
        # the token still looks like a single word and is never recognised as a payload at
        # all.  Splitting on any whitespace (not just a space) also admits a tab escape.
        for token in tokens:
            decoded = _decode_printf_escapes(token)
            if len(decoded.split()) > 1:
                payloads.append(decoded)
        # ``xargs`` is different in shape: it does not read a whole script, it APPENDS
        # the piped words to its own command.  ``echo <verb> | xargs <name>`` therefore
        # runs ``<name> <verb>`` even though neither half contains a space.  Reconstruct
        # what it will run: the xargs command line plus the producer's literal words.
        reconstructed = _xargs_reconstructed_command(tokens)
        if reconstructed:
            payloads.append(reconstructed)
    return [p for p in payloads if p.strip()]


def _sed_exec_replacement(token: str) -> str:
    """The replacement text of a ``sed`` ``s///e`` command, which GNU sed EXECUTES.

    ``sed 's/x/<name> <verb>/e'`` runs the replacement as a shell command.  Returns an
    empty string for any token that is not such a command, including an ordinary
    substitution without the ``e`` flag.
    """
    body = token.strip("\"'")
    if not body.startswith("s") or len(body) < 2:
        return ""
    delim = body[1]
    if delim.isalnum() or delim.isspace():
        return ""
    parts = body[2:].split(delim)
    if len(parts) < 3:
        return ""
    flags = parts[2]
    if "e" not in flags:
        return ""
    return parts[1]


# ``a=(<name> <verb>)`` -- a literal array assignment.  ``shlex`` splits on whitespace
# only, so the elements arrive as separate tokens with the parens glued on.
_ARRAY_ASSIGN_RE = re.compile(r"\A([A-Za-z_]\w*)=\((.*)\Z", re.DOTALL)
# ``${a[@]}`` / ``${a[*]}`` / ``$a[@]`` -- the whole array as separate words.
_ARRAY_EXPAND_RE = re.compile(r"\$\{?([A-Za-z_]\w*)\[[@*]\]\}?")


def _array_assignments(tokens: "list[str]") -> "dict[str, str]":
    """Literal array assignments in *tokens*, as name -> the elements joined by a space.

    ``a=(<name> <verb>)`` tokenizes to ``['a=(<name>', '<verb>)']``, so the elements are
    gathered from the opening token up to the one that closes the paren.
    """
    arrays: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        match = _ARRAY_ASSIGN_RE.match(tokens[i])
        if not match:
            i += 1
            continue
        name, first = match.group(1), match.group(2)
        elements: list[str] = []
        closed = False
        for part in [first] + tokens[i + 1 :]:
            # The closing paren usually arrives with the next control operator glued on
            # (``<verb>);``), so split at the paren rather than testing the token's end.
            if ")" in part:
                stripped = part[: part.index(")")]
                if stripped:
                    elements.append(stripped)
                closed = True
                break
            if part:
                elements.append(part)
        if closed and elements:
            arrays.setdefault(name, " ".join(elements))
        i += 1
    return arrays


def _xargs_reconstructed_command(tokens: "list[str]") -> str:
    """The command ``xargs`` will run, rebuilt from its argv plus the piped words.

    ``xargs`` appends the words it reads on stdin to the command given as its own
    arguments, so ``echo <verb> | xargs <name>`` executes ``<name> <verb>``.  Neither
    side contains a space, so the whole-token payload scan cannot see it; rebuilding the
    effective command line makes it visible to the ordinary argv checks.
    """
    pipe = next((i for i, tk in enumerate(tokens) if "|" in tk), -1)
    if pipe <= 0:
        return ""
    xargs_at = next(
        (i for i in range(pipe + 1, len(tokens)) if _program_basename(tokens[i]) == "xargs"),
        -1,
    )
    if xargs_at == -1:
        return ""
    # Skip xargs' own options; everything after them is the command it runs.
    command = [tk for tk in tokens[xargs_at + 1 :] if not tk.startswith("-")]
    # The producer's literal words (its program name is not piped through).
    piped = [tk for tk in tokens[1:pipe] if not tk.startswith("-")]
    if not command:
        return ""
    return " ".join(command + piped)


_PRINTF_ESCAPES = (("\\n", " "), ("\\t", " "), ("\\r", " "), ("\\v", " "), ("\\f", " "))


# ``printf`` / ``$'…'`` numeric escapes: octal (``\\NNN``, ``\\0NNN``), hex
# (``\\xHH``) and Unicode (``\\uHHHH``, ``\\UHHHHHHHH``).
#
# The Unicode widths are EXACT and CASE-SENSITIVE, as bash defines them: ``\\u``
# consumes at most 4 hex digits and ``\\U`` at most 8, so ``$'\\u0072f'`` is ``r``
# followed by a literal ``f`` -- NOT a 5-digit code point.  Reading more digits than
# the spelling allows is a bypass, because the wrong character replaces the two the
# shell actually passes.  The pattern therefore carries no ``re.IGNORECASE`` (which
# would conflate the two widths) and spells its own classes; ``\\x`` keeps accepting
# either case, as it always did.
#
# This requires the caller to have preserved case: see ``_deny_segment_views``,
# which decodes BEFORE lowercasing for exactly this reason.
_NUMERIC_ESCAPE_RE = re.compile(
    r"\\(?:[xX]([0-9a-fA-F]{1,2})"
    r"|u([0-9a-fA-F]{1,4})"
    r"|U([0-9a-fA-F]{1,8})"
    r"|0?([0-7]{1,3}))"
)

# ANSI-C quoting (``$'...'``) spells octal as ``\nnn`` -- one to three octal digits
# TOTAL, where a leading zero is simply one of the three.  The ``\0nnn`` form above
# (zero plus up to three more digits) belongs to ``echo -e``/``printf %b`` ONLY;
# sharing that pattern here consumed a fourth digit, so ``$'\06777'`` -- which bash
# reads as ``\067`` ('7') followed by the literal ``77``, i.e. ``777`` -- decoded to
# a single non-ASCII byte and the deny view diverged from what the shell runs
# (BLOCKING from the GPT 5.6 lane).  Group order matches _NUMERIC_ESCAPE_RE so
# ``_numeric_escape_code`` reads either match.
_ANSI_C_NUMERIC_ESCAPE_RE = re.compile(
    r"\\(?:[xX]([0-9a-fA-F]{1,2})"
    r"|u([0-9a-fA-F]{1,4})"
    r"|U([0-9a-fA-F]{1,8})"
    r"|([0-7]{1,3}))"
)


def _escape_code_is_inert(code: int) -> bool:
    """True for a code point that must be left ENCODED rather than decoded.

    A NUL cannot appear in an argv the shell builds.  A LONE SURROGATE is refused
    for a second reason: it is not a character bash can pass either, and a decoded
    one would travel into the SEL audit record, whose JSON encoder raises on it --
    turning a denial into a crash.
    """
    return code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF


def _numeric_escape_code(match: "re.Match[str]") -> "int | None":
    """The code point an octal / hex / Unicode escape resolves to, or None.

    Split out so :func:`_numeric_escape_char` and :func:`_decode_ansi_c_body` cannot
    disagree about what a match means -- the body decoder has to recognise a NUL to
    truncate at it, and re-deriving that separately is how two code paths drift.

    Octal is masked to ONE BYTE, which is bash's semantics and was measured rather
    than assumed: ``$'\\555'`` is ``m`` (0o555 & 0xFF == 0x6D), ``$'\\777'`` is
    0xFF, and ``$'\\400'`` masks to a NUL.  Converting the full value instead gave
    ``$'r\\555'`` the character ``u``-breve where bash passes ``rm``, so
    ``$'r\\555' -rf /`` ran while the view matched nothing (BLOCKING from the GPT
    5.6 lane).
    """
    hex_digits, u4_digits, u8_digits, octal_digits = match.groups()
    digits = hex_digits or u4_digits or u8_digits
    try:
        return int(digits, 16) if digits else (int(octal_digits, 8) & 0xFF)
    except (TypeError, ValueError):  # pragma: no cover - the pattern admits only digits
        return None


def _numeric_escape_char(match: "re.Match[str]") -> str:
    """One decoded character for an octal, hex or Unicode escape."""
    code = _numeric_escape_code(match)
    if code is None or _escape_code_is_inert(code):
        return match.group(0)
    return chr(code)


def _decode_printf_escapes(text: str) -> str:
    """Turn literal ``\\n``-style escapes into whitespace.

    ``printf 'kirocrew token\\n' | bash`` carries the newline as two characters, so
    re-tokenizing the payload glues the escape onto the verb and the comparison misses.
    ``printf`` (and ``echo -e``) expand these before the shell sees them, so the payload
    is decoded the same way first.

    Numeric escapes are decoded too, not just the named ones: ``\\040`` and ``\\x20`` are
    both a SPACE, so leaving them literal reopens exactly the separator gap the named
    escapes closed, and ``\\x6b`` can spell a character of the program name itself.  What
    the shell will actually run is the decoded text, so the comparison is made against
    that.

    The Unicode forms (``\\uHHHH``, ``\\UHHHHHHHH``) are decoded for the same reason and
    were missing: ``$'\\u0074\\u006f\\u006b\\u0065\\u006e'`` is the same word as the
    ``\\x``-spelled form this already caught, so the argv-structural floors compared
    against an encoded string and a spelling of a self-protection verb slipped past
    while its hex twin was refused (found by the GPT 5.6 lane on the deny-view change).
    """
    for esc, sub in _PRINTF_ESCAPES:
        text = text.replace(esc, sub)
    return _NUMERIC_ESCAPE_RE.sub(_numeric_escape_char, text)


def _shell_payload_walk(text_lower: str) -> "list[tuple[str, list[str]]]":
    """``(source, argv)`` for *text_lower* and every nested shell payload in it.

    ``bash -c "kirocrew token"`` tokenizes to ``['bash', '-c', 'kirocrew token']``
    -- the dangerous command is a single opaque token, so a direct scan cannot
    see it.  Re-tokenizing the payload and checking that view too closes the
    class rather than one spelling of it.

    Both the SOURCE text and its argv are returned because the two floors that
    consume this need different views of the same frame: the self-protection
    predicates match argv structurally, while the git-publish gate is a
    verb-anchored scan over command text.  Walking once and handing out both is
    what keeps the two floors from drifting -- the publish gate previously did
    its own top-level-only text match, so every wrapper form
    (``bash -c '<push>'``, ``eval '<push>'``) bypassed the ONLY enforcement
    pushes have.

    Descends to ANY depth.  A numeric depth cap is itself a bypass -- whatever the
    number, one more wrapper defeats it -- so the walk is bounded structurally: a
    payload lives inside one token of its parent and is therefore strictly shorter
    than the parent's source text, and a chain of strictly shorter strings is
    finite.
    """
    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    # The third field is whether this frame may perform the ``eval`` argument join.
    # A frame PRODUCED by a join may not, which is what bounds the walk: see
    # ``_nested_shell_payloads``.
    pending: list[tuple[str, int, bool]] = [(text_lower, len(text_lower) + 1, True)]
    while pending:
        source, parent_len, allow_join = pending.pop()
        tokens = _self_tokens(source)
        if not tokens:
            continue
        out.append((source, tokens))
        # Every substitution body is itself a command line -- command substitution
        # (``$( )``, backticks) and PROCESS substitution (``<( )``, ``>( )``) alike, since
        # bash runs the inner command in all of them.  Walking them here means the
        # ordinary argv checks see ``cat <(kirocrew token)`` as the inner invocation.
        joined_here: set[str] = set()
        nested = _nested_shell_payloads(tokens, allow_join=allow_join, joined_out=joined_here)
        for payload in list(nested) + _substitution_bodies(source):
            # Descend through EVERY literal payload, to any depth.  Termination is
            # structural, not a cap: a payload is carried inside one token of its
            # parent, so it is strictly shorter than the parent's source text.
            payload = _decode_printf_escapes(payload)
            if len(payload) >= parent_len or payload in seen:
                continue
            seen.add(payload)
            pending.append((payload, len(source), payload not in joined_here))
    return out


def _self_token_frames(text_lower: str) -> "list[list[str]]":
    """The command's own argv plus the argv of every nested shell payload."""
    return [tokens for _source, tokens in _shell_payload_walk(text_lower)]


def _shell_payload_sources(text_lower: str) -> "list[str]":
    """*text_lower* plus the source text of every nested shell payload in it."""
    return [source for source, _tokens in _shell_payload_walk(text_lower)]


def _substitution_depth_delta(token: str) -> int:
    """Net change in command-substitution nesting contributed by *token*.

    Used so a separator INSIDE ``$( … )`` is not mistaken for the end of the argv
    being scanned -- ``<name> $(true; echo <verb>)`` is one command, not two.

    KNOWN LIMIT: this counts characters on tokens ``normalize_shell_command``
    has already stripped the quotes from, so a QUOTED paren or backtick is
    indistinguishable from a real one here and a window bounded by this delta
    under-runs on decoyed input.  The bare-``kill`` window recovers by
    re-deriving its bodies from the raw text, where the quotes still exist
    (:func:`_bare_kill_raw_bodies`, #8633).
    """
    return token.count("$(") + token.count("`") // 2 - token.count(")")


def _ends_argv(token: str) -> bool:
    """True if *token* ends the current command's argv.

    ``|`` and ``;`` always separate commands.  ``&`` only does so as a token of
    its own -- ``2>&1`` is a redirection, and a redirection does not end an argv
    (bash accepts one anywhere in a simple command).  A ``#`` comment ends the
    argv too: everything after it is prose, not arguments.

    A function-body opener (``x(){`` or a bare ``{``) is a boundary as well: the words
    after it are a NEW command, not arguments to the definition.  Without that,
    ``x(){ echo <name> <verb>;}`` attributes the body to ``x(){`` instead of to ``echo``,
    and the data-consumer exemption that makes ``echo`` inert never applies.
    """
    if token.startswith("#"):
        return True
    if token.rstrip("{") in {"", "("} or token.rstrip("{").endswith("()"):
        return True
    if token in {"&", "&&", "||", ";", ";;", "\n"}:
        return True
    return "|" in token or ";" in token


def _substitution_bodies(text: str) -> "list[str]":
    """The body of each command substitution in *text*.

    ``$(...)`` is scanned with paren nesting so a nested substitution closing
    first does not truncate the outer body; backticks are taken pairwise.  Only
    the BODY is returned -- a bare ``kill`` must not be attributed a name that
    merely appears in a LATER, unrelated command of the same line.

    The nesting walk is QUOTE-AWARE, through the same
    :func:`_matching_close_paren` span the git-publish boundary walk uses. A
    private, quote-unaware copy of it truncated the body at a QUOTED ``)``, and
    that lost the nested command entirely rather than merely mis-sizing the span:
    ``git push origin my-feature > >(X=')' git push origin main)`` extracted the
    body ``X='``, so the nested publish of a protected branch was never scanned
    and ``is_denied`` returned None for a command bash executes. An UNPROVEN span
    yields the whole remainder, which is the fail-closed direction -- scanning
    text that is not really in the body can only add findings.
    """
    bodies: list[str] = []
    i = 0
    while i < len(text):
        # PROCESS substitutions run their body as a command just as a command
        # substitution does -- ``cat <(kirocrew token)`` executes the inner command and
        # feeds its output through a pipe.  Same paren-nesting walk.
        if text.startswith(("<(", ">(", "$("), i):
            end, proven = _matching_close_paren(text, i + 2)
            bodies.append(text[i + 2 : end - 1] if proven else text[i + 2 :])
            i = end
            continue
        if text[i] == "`":
            j = text.find("`", i + 1)
            bodies.append(text[i + 1 : j if j != -1 else len(text)])
            i = len(text) if j == -1 else j + 1
        else:
            i += 1
    return bodies


def _redirect_glue_point(word: str) -> "int | None":
    """Index where an OUTPUT redirect glued to the END of another word begins, else None.

    A redirect needs no whitespace in front of it, so it can ride on the back of any
    word: ``python -u> /dev/null <<< '<program>'`` is the flag ``-u`` plus ``> /dev/null``,
    and bash runs the here-string. The detector only recognised a redirect at the START of
    a word, so ``-u>`` fell through to "an ordinary interpreter flag", the redirect target
    in the next token became the script path, and the stdin program went unscanned. The
    ``<`` branch has always looked for its operator ANYWHERE in the word; this is the same
    rule for the ``>`` family, and that asymmetry was the gap.

    The word is SPLIT rather than skipped, because what precedes the redirect decides the
    answer and only the caller's own branches can classify it: ``-u`` is a flag and the
    scan continues, but ``script.py>out`` means the script supplies the program and the
    answer is False. Measured in bash: ``python script.py> out <<< '<program>'`` runs the
    script, not the here-string. Splitting and re-reading both halves reuses that
    classification instead of duplicating it, so the two cannot drift apart.

    None when the word has no ``>`` at all, or already begins with a redirect -- a leading
    file descriptor belongs to the redirect, and the shell only reads digits as one when
    they are the whole prefix (``2>err`` is fd 2; ``x2>err`` is the word ``x2``).
    """
    position = word.find(">")
    if position <= 0:
        return None
    if _OUTPUT_REDIRECT_RE.match(word) is not None:
        return None
    return position


def _output_redirect_scan(raw: str, start: int = 0) -> "tuple[str, int] | None":
    """``(target, end)`` for the OUTPUT redirect at *start* in *raw*, or None.

    ``python 2>&1 <<< '<program>'`` runs the here-string, but the detector had no branch
    for the ``>`` family at all: it handles ``<`` and heredocs off the raw token and let
    everything else fall through to "this is a script path". The unnumbered glued form
    only survived by accident, because ``_normalize_operand`` reduces ``>out.txt`` to the
    empty string and the loop skips empties -- while ``2>&1`` reduces to ``2``, a
    perfectly good file name, so the interpreter looked like it was running a script
    called ``2`` and the program on its stdin went unscanned.

    Every spelling is a redirect and none is ever a positional: an optional leading file
    DESCRIPTOR -- a number, ``&`` for both streams, or a ``{name}`` automatic descriptor
    -- then ``>`` or ``>>``, then an optional ``&`` for the duplicating form or ``|`` for
    the noclobber override.

    The target STOPS at the next redirect operator, and *end* is that position, because
    the shell starts a new redirect there: in ``python 2>/dev/null<<EOF`` the word is one
    token, and taking all of ``/dev/null<<EOF`` as the target swallows the heredoc marker
    and loses the program that arrives on stdin.

    Only at substitution depth ZERO, though. A redirect inside ``$(...)``, ``${...}`` or
    backticks belongs to that inner command and is not a boundary of this word:
    ``python 2>$(echo>/dev/null;printf /dev/null) <<< '<program>'`` really is
    ``python 2>/dev/null`` once the shell has run the substitution, and cutting the target
    at the inner ``>`` left the tail of the substitution to be read as a script path,
    which put the stdin program back out of view. The whole substitution is one shell
    WORD, and :func:`_operand_span_end` is what carries it across the tokens it spans.

    Depth counts every ``(`` and ``{`` INSIDE a substitution, but at depth zero only a
    ``$``-prefixed opener starts one. A subshell nested inside a substitution
    (``$( (true); printf /dev/null)``) closes with its own ``)`` -- counting the opener
    but not that one would drop the depth to zero early and reopen exactly the hole
    this closes. A BARE opener at depth zero is different: the tokenizer that feeds
    this scan strips quotes (``_self_token_frames`` shlex-splits, and the caller also
    edge-strips), so a quoted ``(`` -- one filename character to bash -- arrived here
    bare, opened a span that never closed, and the target ran past the ``<<<`` that
    should have ended it. The here-string was absorbed into the target and the program
    arriving on stdin went unscanned: measured in bash, ``python 2>'a)(b'<<<'<program>'``
    runs the program, and so does the unquoted-brace spelling ``python 2>a{b<<<'<program>'``
    (a bare ``{`` is an ordinary filename character). An UNQUOTED bare ``(`` cannot
    reach execution at all -- bash rejects ``2>a(b`` as a syntax error -- so at depth
    zero the only executable meaning of a bare opener is a filename character, and the
    walk now reads it as one. *raw* must reach here with its substitution delimiters
    intact; see the caller.

    Quote CHARACTERS in *raw* are data, never grammar. The tokenizer resolved quoting
    before this scan runs, so a quote character that survives is literal text from a
    spelling like ``2>"a'b"`` -- and reading it as grammar is the same defect in the
    opposite direction: a single-quote state opened on that data quote consumed the
    ``<<<`` to the end of the text and hid the operator (found in review, First
    Principles lane). The walk therefore steps over quote characters like any other
    filename character. Residuals, tracked rather than chased: a quoted ``'$('`` or
    a quoted backtick in a filename de-quotes to the same characters as real grammar
    and still holds or toggles a span -- indistinguishable without the quoting the
    tokenizer already destroyed, and closing that class needs quote-preserving
    tokenization at the frame level, not another rule here.

    An INDEX is returned rather than the remaining text so a word holding a chain of
    them (``>a>a>a...``) can be walked once. Re-slicing the word per operator was
    quadratic in its length, on a floor that runs for every command -- the same defect
    class this module pins against elsewhere, so it is not reintroduced here.
    """
    match = _OUTPUT_REDIRECT_RE.match(raw, start)
    if match is None:
        return None
    cut = match.end()
    depth = 0
    in_backtick = False
    while cut < len(raw):
        char = raw[cut]
        if char == "`":
            in_backtick = not in_backtick
        elif char in "({":
            # At depth ZERO an opener counts only when `$` precedes it: `$(`, `${` and
            # `$((` start substitutions, while a BARE `(` mid-word is never grammar in
            # a command that runs -- unquoted it is a bash syntax error, so the only
            # spelling that reaches execution is a quoted one, and the tokenizer that
            # feeds this scan strips quotes (see above). A bare `{` is an ordinary
            # filename character (measured: `python 2>a{b<<<'<program>'` runs the
            # program and writes the file `a{b`). INSIDE a substitution every opener
            # still counts, because a nested subshell closes with its own `)`.
            if depth or (cut > match.end() and raw[cut - 1] == "$"):
                depth += 1
        elif char in ")}" and depth:
            depth -= 1
        elif char in "<>" and not depth and not in_backtick:
            break
        cut += 1
    return raw[match.end() : cut], cut


def _here_string_payload(raw: str) -> "str | None":
    """The operand of a HERE-STRING (``<<<WORD``), ``""`` when the word is the next token.

    ``None`` when this is not a here-string.  A here-string feeds its operand to stdin
    verbatim, so for a stdin-reading interpreter that operand IS the program.

    Kept distinct from :func:`_heredoc_marker` because ``<<<`` also starts with ``<<``:
    reading it as a heredoc turned the payload into a DELIMITER and dropped it from the
    search entirely, so ``python - <<<'import kiro_crew'`` went unmatched (caught in
    review, GPT 5.6).
    """
    if not raw.startswith("<<<"):
        return None
    return raw[3:]


def _heredoc_marker(raw: str) -> "str | None":
    """The delimiter TAG of a heredoc redirect token.

    Returns the tag for the attached spellings (``<<PY``, ``<<-PY``), ``""`` for a
    bare ``<<`` whose tag is the NEXT token, and ``None`` when this is not a heredoc --
    including a here-string (``<<<``), which is :func:`_here_string_payload`'s and must
    not be mistaken for a heredoc whose tag happens to start with ``<``.

    Read off the RAW token deliberately: ``_normalize_operand`` strips a redirection
    down to the empty string, which is why the heredoc branch in
    :func:`_python_reads_stdin` was unreachable -- a bare ``python << 'PY' … PY`` was
    misread as running a SCRIPT named by the first word of the body (#2660).  Shared
    by the stdin DETECTOR and the program-text SCOPE so the two cannot disagree about
    where a heredoc body starts and ends.
    """
    if not raw.startswith("<<") or raw.startswith("<<<"):
        return None
    return raw[3:] if raw.startswith("<<-") else raw[2:]


def _operand_span_end(run: list[str], idx: int, text: str) -> int:
    """Index just past a redirect OPERAND that continues into later tokens.

    A redirect operand can open a substitution -- ``$( )``, ``<( )``, ``${ }`` or a
    backtick pair -- whose text carries whitespace, and the tokenizer splits on
    whitespace only.  So the operand is one shell WORD spread over several tokens, and
    scanning just the first of them read only ``$(printf`` out of
    ``<<<$(printf %s "import kiro_crew")`` (caught in review, GPT 5.6).

    Spans to the LAST token carrying a matching closer, not to the first that balances
    the count.  Balancing is not decidable here: ``normalize_shell_command`` strips
    quoting BEFORE this runs, so a quoted delimiter (``$(true ')'; printf …)``) is
    indistinguishable from a real one and a counting walk stopped early, leaving the
    payload after it unscanned (caught in review, GPT 5.6).  The last closer cannot be
    undershot that way; it over-yields only when a LATER token happens to carry a closing
    character, which is the safe direction.
    """
    closers = ""
    if text.count("(") > text.count(")"):
        closers += ")"
    if text.count("{") > text.count("}"):
        closers += "}"
    if text.count("`") % 2 == 1:
        closers += "`"
    if not closers:
        return idx
    for j in range(len(run) - 1, idx - 1, -1):
        if any(c in run[j] for c in closers):
            return j + 1
    return len(run)


def _stdin_redirect_carriers(tokens: list[str], start: int, stop: int) -> "Iterator[str]":
    """Program text from the stdin REDIRECTIONS in ``tokens[start:stop]``.

    One walk over a token run, yielding whatever each stdin redirection puts on this
    interpreter's stdin.  The redirection families, from the shell grammar:

    * ``<<TAG`` / ``<<-TAG`` -- a heredoc; the BODY up to the matching tag is the program.
      An unterminated one runs to the end of the run, which over-yields, not under.
    * ``<<<WORD`` -- a here-string; the WORD itself is the program.
    * ``<WORD`` -- a file whose CONTENT is the program.
    * ``< <(cmd)`` -- process substitution; the command text is visible and spans tokens
      up to its closing paren, so it is yielded as a run.
    * ``<&N`` -- an fd dup, which carries no text at all; a documented residual.

    Walked as a RUN rather than "everything after the interpreter" because a
    redirection may appear ANYWHERE in a simple command -- BEFORE the program name
    (``<<'PY' python -``), after it, and GLUED TO IT with no space
    (``python3<<<'…'``, ``python3<prog.py``), all of which are ordinary bash reaching
    the same mint (each caught in review, GPT 5.6).  A token that carries a redirect
    after some other text is therefore classified from its first ``<`` onward: the
    text before it is the program name or an earlier operand, and the shell reads the
    rest as the redirection.

    The left-hand run is not split on a newline, so an earlier command's own stdin
    redirect is yielded too -- the same deliberate over-block the pipe producer has,
    and for the same reason.

    A heredoc's body ends at the LAST token equal to its tag, not the first.  Bash
    closes a heredoc only on a line that holds the delimiter ALONE, and line structure
    does not survive tokenizing -- so a body line that merely CONTAINS the word
    (``# EOF``, an ordinary Python comment) produced a token equal to the tag and closed
    the body early, leaving the real payload after it unscanned (caught in review, GPT
    5.6).  The last occurrence is the delimiter that actually ends it; taking it
    over-yields only when the tag word recurs in a LATER command, which is the safe
    direction.
    """
    run = tokens[start:stop]
    idx = 0
    while idx < len(run):
        raw = run[idx].strip(_SHELL_WRAPPER_CHARS)
        if "<" in raw and not raw.startswith("<"):
            # A redirect GLUED to a preceding word: the shell reads everything from the
            # first `<` as the redirection, so classify that suffix. Without this the
            # interpreter's own token was excluded from the walk and
            # `python3<<<'import kiro_crew'` -- one word, no space -- was never scanned.
            raw = raw[raw.index("<") :]
        here = _here_string_payload(raw)
        if here is not None:
            # Checked before the heredoc branch, which would otherwise read `<<<payload`
            # as a tag and drop the payload.
            idx += 1
            if not here:  # a bare `<<<` puts its word next
                if idx >= len(run):
                    return
                here = run[idx].strip(_SHELL_WRAPPER_CHARS)
                yield run[idx]
                idx += 1
            else:
                yield here
            end = _operand_span_end(run, idx, here)
            yield from run[idx:end]
            idx = end
            continue
        marker = _heredoc_marker(raw)
        if marker is not None:
            # Checked before the plain-redirect branch below, which would otherwise read
            # the first `<` of `<<` as a stdin redirect.
            idx += 1
            if not marker:  # a bare `<<` splits its tag into the next token
                if idx >= len(run):
                    return
                marker = run[idx].strip(_SHELL_WRAPPER_CHARS)
                idx += 1
            end = len(run)
            for j in range(len(run) - 1, idx - 1, -1):
                if run[j].strip(_SHELL_WRAPPER_CHARS) == marker:
                    end = j
                    break
            yield from run[idx:end]
            idx = end + 1
            continue
        if "<" in raw:
            target = raw.rsplit("<", 1)[1]
            if target.startswith("&"):
                idx += 1  # `<&N` fd dup: nothing on the command line to match
                continue
            idx += 1
            if not target:
                if idx >= len(run):
                    return
                target = run[idx].strip(_SHELL_WRAPPER_CHARS)
                yield run[idx]
                idx += 1
            else:
                yield target
            end = _operand_span_end(run, idx, target)
            yield from run[idx:end]
            idx = end
            continue
        idx += 1


def _stdin_program_text(tokens: list[str], i: int) -> "Iterator[str]":
    """The tokens that can carry the PROGRAM a stdin-reading ``python`` will run.

    ``tokens[i]`` is an interpreter that reads its program from stdin.  The shell can
    fill that stdin from exactly two families, and this yields those and nothing else:

    * a stdin REDIRECTION -- heredoc body, here-string word, redirected file or process
      substitution -- anywhere in the command: before the program name, after it, or
      glued to it (:func:`_stdin_redirect_carriers`).  Walked over the WHOLE frame in ONE
      pass, not per side of the interpreter: a marker and its body can straddle the
      program name (``<<EOF python - … EOF``), and splitting the walk lost that
      association entirely (caught in review, GPT 5.6).  Only REDIRECT OPERANDS are
      yielded, so a neighbouring command's ordinary argument is still never program text;
    * a PIPE PRODUCER -- the tokens left of this interpreter, when a pipe feeds it.
      The pipe is NOT reliably its own token: the tokenizer splits on whitespace only,
      so ``echo '…'|python -`` glues the operator into a neighbouring word and
      ``_program_basename`` resolves the program from the LAST control-operator
      segment.  So the pipe is detected as a CHARACTER anywhere left of, or glued
      into, the interpreter token, and that token's own leading segment is producer
      text.  Requiring a standalone ``|`` token missed all four no-space spellings and
      let the producer's payload through (caught in review, GPT 5.6).

    Both families over-yield on the left: any pipe, or any earlier command's own stdin
    redirect, qualifies.  That is the safe direction -- a missed carrier is a bypass,
    an extra token is only a visible refusal (pinned by a test).

    Everything else in the frame is another command's argv.  Scanning THAT was the
    defect (#2660): a frame is not split on a newline, so an unrelated neighbour that
    merely names this package in a FILE PATH (``isort src/kiro_crew/mcp_core.py``
    followed by any ``python - <<'PY' … PY``) made a harmless heredoc read as a
    credential mint -- with no ``token`` word anywhere in the command.

    Yields lazily so the caller's ``any()`` short-circuits: the cost stays O(frame)
    per interpreter token, the same bound the frame-wide scan had.
    """
    # A PIPE PRODUCER writes this interpreter's stdin, so its argv IS program text.
    glued_head, pipe_glued, _ = tokens[i].strip(_SHELL_WRAPPER_CHARS).rpartition("|")
    if pipe_glued or any("|" in t for t in tokens[:i]):
        yield from tokens[:i]
        if pipe_glued:
            yield glued_head
    yield from _stdin_redirect_carriers(tokens, 0, len(tokens))


def _has_self_importing_inline_program(tokens: list[str], i: int) -> bool:
    """True if ``tokens[i]`` is an interpreter given a ``-c`` payload that imports this package.

    Separate from ``_is_self_module_invocation`` because the two answer different questions.
    That one asks "does this argv run our code?", which admits ``-m`` and ``-c`` alike and is
    the right input to a verb-gated decision. This one asks "is the code inline?", which is the
    case where the verb gate cannot hold: an inline payload can append to ``sys.argv``, call
    ``main(['token'])``, or reach the token-minting function directly, so no argv word has to
    say ``token``.

    Only the interpreter's own inline-program operand counts — the separate (``-c PAYLOAD``)
    and attached (``-cPAYLOAD``) spellings. A later positional that happens to mention the
    import name is data for whatever the payload does with it, not code we are about to run.

    The STDIN forms are the same escape without an operand: ``python -`` (and a bare ``python``
    with no script) read the program from stdin, so a ``python - <<'PY' … PY`` heredoc or an
    ``echo '…' | python -`` pipe reaches the CLI with the payload nowhere in argv. When that
    program text is visible on the command line, matching the import is the same fail-closed
    decision as for ``-c`` — but it is matched only in the tokens that actually CARRY that
    program (see :func:`_stdin_program_text`), not anywhere in the frame. When it is NOT
    visible (a bare ``python -`` fed by an unseen producer) there is nothing to match and the
    gate cannot see it; that residual is noted, not silently claimed as covered.
    """
    if not _PYTHON_PROGRAM_RE.match(_program_basename(tokens[i])):
        return False
    later_tokens = tokens[i + 1 :]
    glued = tokens[i].strip(_SHELL_WRAPPER_CHARS)
    if "<" in glued:
        # A redirect GLUED to the program name is still this command's redirect, and the
        # detector only ever saw the tokens AFTER the interpreter -- so `python<<EOF … EOF`
        # had no marker in view and its body read as a script path. Hand the suffix over as
        # its own token (caught in review, GPT 5.6).
        later_tokens = [glued[glued.index("<") :], *later_tokens]
    # STDIN program: the text is not an operand of this interpreter — the shell fills stdin from
    # a heredoc body, a redirected file, or a pipe producer — so the search space is those
    # carriers rather than this position's operands. `_python_reads_stdin` is precise so this
    # does not fire for `python script.py`, `python -c …`, or `python -m …`.
    if _python_reads_stdin(later_tokens) and any(
        _inline_payload_reaches_cli(t.strip(_SHELL_WRAPPER_CHARS))
        for t in _stdin_program_text(tokens, i)
    ):
        return True
    expect_payload = False
    skip_next = False
    for later in later_tokens:
        # The PAYLOAD is matched RAW, not through `_normalize_operand`. That helper truncates at
        # the first control operator, which is correct for an operand the shell will split — but
        # a `-c` payload is a quoted program, so its `;` is Python, not a command separator.
        # Normalising `"import sys; ...; from kiro_crew.cli import main; main()"` down to
        # `import sys` hid the import entirely and let the bypass through.
        raw = later.strip(_SHELL_WRAPPER_CHARS)
        if expect_payload:
            if _inline_payload_reaches_cli(raw):
                return True
            expect_payload = False
            continue
        # The FLAG itself is a plain token, so it is safe (and more accurate) to normalise.
        stripped = _normalize_operand(later).strip("\"'")
        if skip_next:
            skip_next = False
            continue  # value consumed by an operand-taking flag (`-X dev`)
        if stripped in _PYTHON_INLINE_PROGRAM_FLAGS:
            expect_payload = True
            continue
        if len(raw) > 2 and raw[:2] in _PYTHON_INLINE_PROGRAM_FLAGS:
            if _inline_payload_reaches_cli(raw):
                return True
        if stripped in _PYTHON_OPERAND_FLAGS:
            skip_next = True
            continue
        if len(stripped) > 2 and stripped[:2] in _PYTHON_OPERAND_FLAGS:
            continue  # attached operand, e.g. `-Xdev`
        # Only interpreter flags precede a `-c` operand. The first token that is neither a flag
        # nor a flag's operand is the interpreter's own positional (a script path or `-`), and
        # nothing after it is a `-c` payload — so stop, rather than scan the rest of the frame.
        # Without this bail the loop was O(tokens) for EACH python token, i.e. O(n²) on a
        # `python open python open …` spam input, which the ReDoS-resistance test caught.
        if not stripped.startswith("-"):
            break
    return False


def _python_reads_stdin(later_tokens: list[str]) -> bool:
    """True if this ``python`` invocation runs its PROGRAM from stdin (a script/module does not).

    CPython reads its program from stdin for a bare interpreter (no positional) or an explicit
    ``-`` argument; ``-c CODE``, ``-m MOD``, and ``FILE`` all supply the program elsewhere.
    Walks the argument stream the way ``_is_self_module_invocation`` does so the corner cases
    line up: an operand-taking flag consumes its value (``-X dev`` — ``dev`` is not a script),
    a heredoc (the ``<<TAG`` marker, its BODY and the closing tag) is not an argument, and a
    pipe/redirect token ends this command's own arguments.

    The heredoc structure is read off the RAW token via :func:`_heredoc_marker`, because
    ``_normalize_operand`` strips a redirection to the empty string — which made the heredoc
    branch here unreachable and had ``python << 'PY' … PY`` (no ``-``) report FALSE, reading
    the first word of the BODY as a script path (#2660).  A redirect OPERAND is consumed
    through :func:`_operand_span_end` for the same reason the carrier scan uses it: a
    substitution operand is one shell WORD over several tokens, and skipping only the first
    left ``python <<< $(printf …)`` reading ``%s`` as a script path (caught in review, GPT
    5.6).  The two functions share that helper so the detector and the carrier scope agree
    on where an operand ends.
    """
    skip_next = False
    heredoc_tag: str | None = None
    expect_tag = False
    idx = 0
    while idx < len(later_tokens):
        tok = later_tokens[idx]
        idx += 1
        raw = tok.strip(_SHELL_WRAPPER_CHARS)
        if heredoc_tag is not None:
            # The body is program text on stdin, not an argument, and its CLOSING TAG
            # ends this command: the tokenizer drops the newline that follows, so
            # whatever comes after the tag belongs to the NEXT command. Reading it as
            # this interpreter's positional made `python <<PY … PY; echo ok` report
            # "runs a script named echo" and skipped the whole branch, so the heredoc's
            # payload went unscanned (caught in review, GPT 5.6). The heredoc has
            # already supplied the program, so the answer here is simply True.
            if raw == heredoc_tag:
                return True
            continue
        if expect_tag:
            expect_tag = False
            heredoc_tag = raw
            continue
        here = _here_string_payload(raw)
        if here is not None:
            # A here-string supplies the program on stdin exactly as a heredoc does; its
            # operand is a redirect word, never this interpreter's positional -- and the
            # WHOLE operand, which a substitution spreads over several tokens.
            if not here:  # a bare `<<<` puts its word in the next token
                if idx >= len(later_tokens):
                    break
                here = later_tokens[idx].strip(_SHELL_WRAPPER_CHARS)
                idx += 1
            idx = _operand_span_end(later_tokens, idx, here)
            continue
        marker = _heredoc_marker(raw)
        if marker is not None:
            if marker:
                heredoc_tag = marker
            else:
                expect_tag = True  # a bare `<<` splits its tag into the next token
            continue
        # Scanned on a form that keeps the SUBSTITUTION delimiters. `raw` has had
        # `_SHELL_WRAPPER_CHARS` stripped, and those include `(` and `)` -- so the word
        # `2>$(` (the tokenizer splits on the space inside `$( (true); printf x)`) arrived
        # here as `2>$`, with the opener gone. The scan then saw an ordinary one-character
        # target, never entered a substitution, and the tail of the substitution was read
        # as a script path, putting the stdin program back out of view. Quotes still come
        # off, since a quoted redirect is still a redirect.
        redirect_word = tok.strip("\"'")
        glue = _redirect_glue_point(redirect_word)
        if glue is not None:
            # The redirect rides on the back of another word (`-u>`). Split it and let the
            # loop read both halves, so the part BEFORE the redirect is classified by the
            # same flag/positional branches as any other word -- `-u` continues the scan,
            # `script.py` ends it. Once per word, since neither half can split again.
            later_tokens = [
                *later_tokens[:idx],
                redirect_word[:glue],
                redirect_word[glue:],
                *later_tokens[idx:],
            ]
            continue
        redirect = _output_redirect_scan(redirect_word)
        if redirect is not None:
            # An OUTPUT redirect and its target are not this command's arguments, and
            # neither says anything about where the program comes from -- so the walk has
            # to step over both and keep looking, exactly as it does for a stdin
            # redirect. Falling through instead read the leftover descriptor digits of
            # `2>&1` as a script path and answered False, so `python 2>&1 <<< '<program>'`
            # had its stdin program go unscanned. Bash runs every one of these.
            redirect_target, position = redirect
            # A chain of output redirects glued into ONE word (`>a>a>a...`) is walked
            # here, in place. Re-injecting each remainder into the token stream instead
            # re-sliced the word per operator, which is quadratic in its length on a
            # floor that runs for every command.
            while position < len(redirect_word):
                further = _output_redirect_scan(redirect_word, position)
                if further is None:
                    break
                redirect_target, position = further
            remainder = redirect_word[position:]
            if remainder:
                # What is left starts with a STDIN operator (`2>/dev/null<<EOF`), which
                # the branches above know how to read. Hand it back as its own token --
                # once per word, not once per operator -- because swallowing it loses the
                # heredoc and with it the program on stdin.
                later_tokens = [*later_tokens[:idx], remainder, *later_tokens[idx:]]
            elif not redirect_target:
                if idx >= len(later_tokens):
                    break
                redirect_target = later_tokens[idx].strip(_SHELL_WRAPPER_CHARS)
                idx += 1
            if redirect_target:
                idx = _operand_span_end(later_tokens, idx, redirect_target)
            continue
        if "<" in raw:
            # A stdin REDIRECT and its operand are not this command's arguments either,
            # and the redirect is what supplies the program: `python < prog.py` reads its
            # program from that file. The earlier walk stopped at the redirect and then
            # read the operand as a script path, so `python3 < $(printf …)` answered False.
            target = raw[raw.index("<") :].rsplit("<", 1)[1]
            if not target:
                if idx >= len(later_tokens):
                    break
                target = later_tokens[idx].strip(_SHELL_WRAPPER_CHARS)
                idx += 1
            idx = _operand_span_end(later_tokens, idx, target)
            continue
        norm = _normalize_operand(tok).strip("\"'")
        if skip_next:
            skip_next = False
            continue  # value consumed by an operand-taking flag (`-X dev`)
        if not norm:
            continue
        if norm.startswith("<") or norm.startswith("|"):
            break  # a redirect/pipe boundary ends this command's argument list
        if norm == "-":
            return True
        if norm in _PYTHON_INLINE_PROGRAM_FLAGS or norm.startswith("-m") or norm.startswith("-c"):
            return False  # `-c`/`-m` supply the program, not stdin
        if norm in _PYTHON_OPERAND_FLAGS:
            skip_next = True
            continue
        if len(norm) > 2 and norm[:2] in _PYTHON_OPERAND_FLAGS:
            continue  # attached operand, e.g. `-Xdev`
        if norm.startswith("-"):
            continue  # an ordinary interpreter flag
        return False  # a positional that is not `-` is a script path
    return True  # nothing but flags → bare interpreter reads stdin


# ── Self-protection floor short-circuit (perf, issue #3603) ──
# The floor predicates below re-tokenize the command and descend every nested
# shell payload (`_self_token_frames`), which is where the cost of the deny
# scan concentrates: it scales with NESTING COMPLEXITY, and each `is_denied`
# call runs the descent three times (mint once, kill twice). The common tool
# call — a tool name plus a path — can never fire either predicate, so the
# descent is pure waste there.
#
# The gate is a NECESSARY condition, deliberately wider than the issue's
# proposal of a raw `_SELF_NAME_RE` search. That proposal is UNSOUND: the
# predicates fire on inputs whose raw text never matches `kiro[-.]?crew` —
# `python -m kiro_crew token` (the underscored import spelling), `[k]irocrew
# token` (one-char bracket class), `kiro$()crew` (empty substitution),
# `kiro${x:-crew}` (parameter default), `bash -c "\x6birocrew token"` (printf
# escapes), `kiro?rew` (glob the shell expands before exec), and a `-c`
# payload reaching the CLI through `exec`/`b64decode` with no name at all.
# Every one of those was verified to be denied by the floor today, so a gate
# that skipped them would be a real bypass, not an optimization.
#
# Sound formulation: the floor can only fire if, after the normalizations the
# predicates themselves apply (shlex quote-stripping, `_debracket`,
# `_resolve_param_defaults`, `_EMPTY_SUBST_RE`, `_decode_printf_escapes`,
# `_glob_could_expand_to`), the text yields a self name/module — or an inline
# dynamic-exec primitive stands in for it. Each normalization needs specific
# MACHINERY characters present in the raw text, so the union below is a
# superset of every firing path:
#   * the literal name in any spelling (`kiro[-._]?crew` — underscore included
#     for the module/import form, which `_SELF_NAME_RE` deliberately omits);
#   * any machinery character that lets a normalization synthesize the name or
#     a program spelling: glob/brace chars (`? * [ ] { }` — `_glob_could_expand_to`
#     admits e.g. `k*w` for the program AND `*kill` for the kill verbs),
#     `$` (substitutions, parameter defaults, ANSI-C quoting), backticks, and
#     `~` (tilde expansion — the kill predicates expanduser their targets, so
#     `pkill -f ~` IS a self-kill whenever $HOME lies under the product tree,
#     with no name and no other machinery in the raw text);
#   * printf numeric escapes (`\xHH`, `\NNN`) that can spell arbitrary
#     characters once `_decode_printf_escapes` runs on a nested payload;
#   * the dynamic-exec markers `_inline_payload_reaches_cli` accepts in place
#     of a literal import — checked on the raw text AND on the quote-stripped
#     text, because empty-quote glue hides the verb exactly as it hides the
#     name (`python -c "ex""ec(...)"` carries no name and no other machinery,
#     yet the floor denies it: pre-merge review finding);
#   * the literal name after stripping quotes/backslashes (`k""iro""crew`,
#     `ki\rocrew` — shlex removes those before the predicates compare).
# When none of these is present, no predicate can return True, so the descent
# is skipped. False positives (e.g. any `$VAR` in a command) merely fall back
# to the full scan — the safe direction.
#
# Matched WITHOUT re.IGNORECASE on purpose: the floor's own contract is that
# callers pass already-lowercased text (`is_denied` lowercases once), and the
# predicates' regexes are lowercase-only too.
_SELF_FLOOR_NAME_HINT_RE = re.compile(r"kiro[-._]?crew")
_SELF_FLOOR_MACHINERY_RE = re.compile(r"[?*\[\]{}$`~]|\\x[0-9a-f]|\\0?[0-7]{1,3}")
_SELF_FLOOR_QUOTE_JUNK_RE = re.compile(r"[\"'\\\\]")


def _self_floor_can_fire(text_lower: str) -> bool:
    """Cheap O(n) necessary condition for the self-protection floor predicates.

    Returns False only when ``_is_credential_mint`` and ``_is_self_kill`` are
    PROVABLY unable to fire on *text_lower*, so both can skip the recursive
    payload descent. Any "maybe" answers True and runs the full scan — the
    gate can over-trigger but never under-trigger (see the block comment above
    for the case analysis).
    """
    if _SELF_FLOOR_NAME_HINT_RE.search(text_lower):
        return True
    if _SELF_FLOOR_MACHINERY_RE.search(text_lower):
        return True
    if _INLINE_DYNAMIC_EXEC_RE.search(text_lower):
        return True
    # Quote/backslash glue is removable by the tokenizer, so the name AND the
    # dynamic-exec verb may only materialize once those come off:
    # `k""iro""crew token`, `"kirocrew" token`, `python -c "ex""ec(...)"`.
    # Both must be re-checked here -- testing only the name would let a glued
    # `exec(` payload skip the descent while the floor still denies it.
    stripped = _SELF_FLOOR_QUOTE_JUNK_RE.sub("", text_lower)
    if _SELF_FLOOR_NAME_HINT_RE.search(stripped):
        return True
    return bool(_INLINE_DYNAMIC_EXEC_RE.search(stripped))


def _is_credential_mint(text_lower: str) -> bool:
    """True if *text_lower* invokes the ``kirocrew token`` credential mint.

    The mint prints a signed dashboard access URL, so it is the escalation path
    this rule exists to close.  Matched on argv, which is what makes the
    ordinary shell forms unbypassable: ``kirocrew "token"`` (quoted verb),
    ``kiro""crew token`` (empty-string concatenation), ``kirocrew -v --no-jail
    token`` (global flags) and ``kirocrew >/tmp/out token`` (bash accepts a
    redirection anywhere in a simple command) all tokenize to an argv whose
    program is the product CLI and one of whose words is exactly ``token``.

    Does NOT match the word appearing in a path or another program's arguments:
    ``cd /workplace/user/kirocrew-wt-x && pytest test/test_token_auth.py`` has no
    argv whose PROGRAM is the CLI, and ``kirocrew doctor | grep token`` puts the
    word in ``grep``'s argv, not the CLI's.
    """
    # Perf short-circuit (#3603): the tokenize-and-descend below is the deny
    # scan's dominant cost, and it cannot produce a hit when the gate says the
    # input carries neither a self name nor the machinery to synthesize one.
    if not _self_floor_can_fire(text_lower):
        return False
    for tokens in _self_token_frames(text_lower):
        programs = _argv_programs(tokens)
        for i, token in enumerate(tokens):
            # AN INLINE PROGRAM THAT IMPORTS OUR CLI IS DENIED WITHOUT NEEDING THE VERB, and
            # this is checked FIRST because it does not depend on the self-program/module gate
            # below. Everywhere else the verb is the trigger, because ``kirocrew doctor`` is
            # legitimate and only ``kirocrew token`` mints. That reasoning does not survive an
            # inline program: ``-c`` and stdin (``python -``) both run arbitrary Python with
            # the interpreter's full authority, so it can BUILD the verb rather than pass it —
            # ``python -c "import sys; sys.argv.append('token'); from kiro_crew.cli import main;
            # main()"`` names no ``token`` argv word, and ``python - <<'PY' … PY`` puts the
            # program on stdin, off argv entirely. The honest gate is the import. Scoped to
            # ``_SELF_IMPORT_RE``, so ``python -c "print(1)"`` and a bare ``python -`` running
            # unrelated code are untouched. Found in review (GPT 5.6).
            if _has_self_importing_inline_program(tokens, i):
                return True
            # Either the console script IS the program, or an interpreter runs the product as
            # a MODULE (`python -m kiro_crew ... token`). The module form mints the identical
            # token, and its argv program is the interpreter, so `_is_self_program` alone
            # missed it — the underscored import name is not a console-script spelling either,
            # so the regex tier could not see it. Found in review.
            if not _is_self_program(token) and not _is_self_module_invocation(tokens, i):
                continue
            # The name is an ARGUMENT of a command that treats arguments as data
            # (``echo <name> <verb>`` prints two words) -- a mention, not a mint.
            if _data_consumer_exempt(i, token, programs, tokens):
                continue
            # Check each argument for the verb BEFORE testing whether it ends the
            # argv, then stop.  Order matters for the same reason it does in the kill
            # scan: ``if true; then <name> <verb>; fi`` hands the verb over as
            # ``<verb>;`` -- one token that both IS the verb and carries the boundary,
            # so testing the boundary first discards the very argument that names it.
            depth = 0
            inline_payload_next = False
            for later in tokens[i + 1 :]:
                if _is_mint_verb(later):
                    return True
                # The operand of `-c` is a quoted PROGRAM, so its `;` is data, not a command
                # separator. Letting `_ends_argv` see it ended the scan on the payload of
                # `python -c "from kiro_crew.cli import main; main()" token` — one token before
                # the verb — so the mint was permitted even though the interpreter check had
                # already matched. Found in review.
                #
                # This skip is no longer what protects the `-c` form: a payload that imports
                # the CLI is denied above, before this loop runs, because it can construct the
                # verb internally. The skip remains correct for the case it was written for —
                # a payload that does NOT import us, followed by a real `token` argument.
                if inline_payload_next:
                    inline_payload_next = False
                    continue
                _operand = _normalize_operand(later).strip("\"'")
                if _operand in _PYTHON_INLINE_PROGRAM_FLAGS:
                    inline_payload_next = True
                    continue
                # `-c<payload>` attached: the payload is already inside this token, so it is
                # data in the same way — skip it without expecting a following one.
                if len(_operand) > 2 and _operand[:2] in _PYTHON_INLINE_PROGRAM_FLAGS:
                    continue
                # A separator NESTED in a command substitution is part of that
                # substitution, not the end of this argv: ``<name> $(true; echo <verb>)``
                # is still one command.  Only a top-level separator ends the scan.
                depth += _substitution_depth_delta(later)
                if depth <= 0 and _ends_argv(later):
                    break
                depth = max(depth, 0)
    return False


def _normalize_operand(token: str) -> str:
    """A token reduced to the text the shell will actually pass along.

    Removes quoting, an attached redirection and empty substitutions, and truncates at
    the first control operator -- every wrapper that can sit on an operand without
    changing what the shell hands to the program.  Used for both the credential verb and
    the kill target, so a wrapper closed in one place cannot reopen in the other.

    The operator is a boundary, not a trailing nuisance: in ``<verb>;echo ok`` the shell
    passes ``<verb>`` and starts a new command, so stripping only from the END leaves the
    operand unrecognisable while the shell still runs it.
    """
    token = _resolve_param_defaults(token.strip(_SHELL_WRAPPER_CHARS))
    token = _EMPTY_SUBST_RE.sub("", token)
    token = _debracket(_strip_redirect(token).strip(_SHELL_WRAPPER_CHARS))
    return _CONTROL_OPERATOR_RE.split(token, 1)[0].strip(_SHELL_WRAPPER_CHARS)


def _is_mint_verb(token: str) -> bool:
    """True if *token* is the credential-minting verb, however it is dressed."""
    return _normalize_operand(token) == "token"


def _static_substitution_output(body: str) -> str:
    """The word a command substitution STATICALLY expands to, else a marker.

    ``$(echo kill)`` and ``$(printf kill)`` put the verb in COMMAND position
    through their output; the undecoyed spelling is already detected by the
    token walk (``kill)`` strips to a ``kill`` basename), so only the decoyed
    combination slipped -- the raw window had no anchor for it (server-side
    GPT review round 6, bash-measured).  Resolution is deliberately narrow:
    ``echo``/``printf`` with a literal first operand, flags and format words
    skipped.  Anything dynamic returns ``"\x00"``, a word no program name
    matches, so an unresolvable generator can only under-anchor (miss goes to
    the remainder ledger), never conjure one.
    """
    tokens = body.split()
    if tokens and _program_basename(tokens[0]) in {"echo", "printf"}:
        for arg in tokens[1:]:
            operand = _normalize_operand(arg)
            if operand.startswith("-") or "%" in operand:
                continue
            return operand
    return "\x00"


# An assignment word (``k=kill``): bash only honours these BEFORE the first
# non-assignment word of a command, and their value is visible to LATER
# commands only (expansion happens before the assignment takes effect).
_RAW_ASSIGNMENT_RE = re.compile(r"([a-z_][a-z0-9_]*)=(.*)\Z")


def _kill_prefix_keeps_anchor(words: "list[tuple[str, bool]]", word: "list[str]") -> bool:
    """True when a glued substitution must NOT cost the word its kill anchor.

    ``kill$(B)`` runs the program ``kill`` whenever B expands to NOTHING at
    runtime -- ``$(:)``, ``$(true)``, any silent command -- which no static
    scan can decide, so the anchor decision fails toward detection: a FIRST
    word whose pre-glue prefix is exactly ``kill`` keeps its anchor (server-
    side GPT review round 3, bash-measured: ``kill$(:) $(pgrep -f <name>)``
    kills).  Only the first word, because the program position is what makes
    the prefix a program: ``echo kill$(printf x) $(pgrep -f <name>)`` hands
    every ``kill...`` word to echo as data, and eating the anchor there is
    what keeps that spelling allowed.  The glued word's OWN body sits at the
    anchor's index, inside the forward bound, so ``kill$(pgrep -f <name>)``
    -- whose program is ``kill<pids>``, not ``kill`` -- still attributes
    nothing from its glue.
    """
    return not words and _program_basename("".join(word)) == "kill"


def _bare_kill_raw_bodies(source: str) -> "list[str]":
    """Substitution bodies inside a bare ``kill``'s own argv, read from the RAW text.

    The token walk in :func:`_is_self_kill` bounds the same window with
    :func:`_substitution_depth_delta`, which counts parens on tokens
    ``normalize_shell_command`` has already stripped the quotes from -- so by the
    time the counter runs, a QUOTED close-paren is indistinguishable from a real
    closer and it ends the window early: ``kill $(printf ')' ; pgrep -f
    kirocrew)`` scored the quoted paren as depth -1, cut the argv at the ``;``,
    and dropped the ``pgrep`` clause that names the target -- while bash, whose
    substitution scan is quote-aware, runs that ``pgrep`` (measured).

    The raw text still HAS the quotes, so the window is re-derived here from the
    same quote-aware machinery the extractor uses (:func:`_iter_shell_chars`
    for the walk, :func:`_matching_close_paren` for each span): a separator
    splits a segment only OUTSIDE quotes and OUTSIDE any substitution span, and
    a body is attributed only when it sits AFTER a top-level word that resolves
    to ``kill`` in the SAME segment.  Both bounds carry weight: the segment
    bound keeps ``kill 123; echo $(cat /tmp/kirocrew)`` allowed (the
    substitution belongs to the ``echo``), and the forward bound keeps
    ``LOG=$(ls /tmp/kirocrew.log) kill 4242`` allowed (the substitution
    precedes the kill, so it is an environment word's value, not the kill's
    operand) -- the exact false positive the token walk's own scoping replaced.

    Three quoting rules are load-bearing, each bash-measured (pre-push review):

    * A substitution body is parsed in its OWN NEUTRAL quote context, because
      that is how bash reads ``$( )`` -- a ``$(`` inside double quotes closes at
      an interior ``)`` even though the outer quote is still open.  The walk
      then RESUMES with the outer quote state it carried into the opener, so
      ``echo "$(date)" ; kill $(...)`` keeps its ``;`` as a real separator and
      the kill segment is still scanned.
    * A backtick closer is found through an escape-skipping scan: within
      backticks a backslash escapes ``\\``` and the escaped backtick is DATA,
      so taking it as the closer would truncate the body before the clause
      that names the target.
    * ``&>`` / ``&>>`` (and the trailing ``2>&1`` form) are redirects of the
      SAME simple command, not separators -- splitting there discarded the
      ``kill`` word before its substitution was attributed.

    A word GLUED to a substitution (``kill$(x)``, ``LOG=$(x)``) is never the
    bare ``kill`` this scan attributes to: bash joins the expansion into the
    word, so the program it runs is not the literal word prefix.  Glued words
    are flagged and excluded from the kill match, which keeps
    ``echo kill$(printf kirocrew)`` -- where ``kill...`` is an argument of
    ``echo`` -- out of the deny set.

    Words collect what the shell PASSES, not what the operator typed: a quote
    character that is quote SYNTAX (an opener or closer -- the state machine's
    own transitions say which) is dropped, while a quote character that is DATA
    (inside the other quote type, or escaped) is kept.  Without that,
    ``k''ill`` reached the comparison spelled with its splice and the kill was
    missed (server-side GPT review, bash-measured: the spliced spelling runs
    ``kill``).  A syntax quote still OPENS a word -- ``''#`` is the word ``#``,
    not a comment -- which the ``open_word`` flag carries.

    An UNPROVEN span (parens that never balance) takes the whole remainder as
    the body and ends the walk.  That cannot under-detect: bash cannot execute
    past an unterminated substitution either (the whole line is a syntax
    error), so there is no later command to lose -- while the remainder still
    reaches the name search attributed to the CURRENT segment, which is what
    keeps the decoyed-and-unbalanced spelling detected.

    A top-level ``#`` starting a word begins a comment, which ends at the next
    newline; the skip lands ON that newline so the segment boundary it carries
    is still honoured.

    This pass is a UNION with the token walk, never a replacement: the tokens
    carry resolutions the raw text does not (``p=$(pgrep -f kirocrew); kill $p``
    resolves ``$p`` at tokenization) and the raw text carries the quoting the
    tokens lost.  Keeping both is what guarantees no previously-detected
    spelling is dropped.
    """
    bodies: list[str] = []
    words: list[tuple[str, bool]] = []  # (word, glued-to-a-substitution)
    tagged: list[tuple[int, str]] = []  # (index of the word the body belongs to, body)
    word: list[str] = []
    glued = False
    open_word = False  # a word has begun, even if only as quote syntax (``''``)

    def end_word() -> None:
        nonlocal glued, open_word
        if word:
            words.append(("".join(word), glued))
            word.clear()
        glued = False
        open_word = False

    aliases: dict[str, str] = {}

    def resolves_to_kill(w: str) -> bool:
        # The literal spelling, or a variable an EARLIER command assigned the
        # verb to: ``k=kill; $k $(...)`` reaches this walk spelled ``$k``,
        # while the token walk sees it resolved -- so the decoyed alias
        # spelling slipped both union halves (server-side GPT review round 5,
        # bash-measured).  Both ``$k`` and ``${k}`` count; the value check
        # goes through the same basename read as the literal.
        if _program_basename(w) == "kill":
            return True
        if not w.startswith("$"):
            return False
        name = w[1:]
        if name.startswith("{") and name.endswith("}"):
            name = name[1:-1]
        return _program_basename(aliases.get(name, "")) == "kill"

    def end_segment() -> None:
        end_word()
        # Anchor selection BEFORE recording this segment's assignments: bash
        # expands ``$k`` before the same command's ``k=...`` takes effect, so
        # ``k=kill $k ...`` must not see its own assignment.
        kill_at = next(
            (k for k, (w, g) in enumerate(words) if not g and resolves_to_kill(w)),
            None,
        )
        if kill_at is not None:
            bodies.extend(body for idx, body in tagged if idx > kill_at)
        # Only the assignment PREFIX is real: a ``k=kill`` in argument
        # position (``echo k=kill``) assigns nothing, and recording it would
        # let a later ``$k`` conjure a kill anchor out of printed text.
        for w, _g in words:
            assignment = _RAW_ASSIGNMENT_RE.match(w)
            if assignment is None:
                break
            aliases[assignment.group(1)] = assignment.group(2)
        words.clear()
        tagged.clear()

    def record_body(body: str) -> None:
        # A body glued onto an open word belongs to THAT word's index; a body
        # starting a word of its own sits at the next index.  Either way the
        # forward bound above compares against the kill word's index.
        tagged.append((len(words), body))

    i = 0
    n = len(source)
    state = 0
    ansi = False
    while i < n:
        jumped = False
        for step in _iter_shell_chars(source[i:], state, ansi):
            off = i + step.offset
            ch = step.char
            escaped = len(step.text) == 2
            in_single = step.state == 1 and not (ch == "'" and step.active)
            if not escaped and not in_single and ch == "$" and source.startswith("$(", off):
                # bash parses the body in a fresh context, so the span is
                # proven from the slice at NEUTRAL state -- and the walk
                # resumes with the OUTER state carried across the jump.
                rel, proven = _matching_close_paren(source[off + 2 :], 0)
                body = source[off + 2 : off + 1 + rel] if proven else source[off + 2 :]
                # An EMPTY substitution expands to NOTHING, so the word
                # CONTINUES across it -- ``kill$()`` runs ``kill`` (the same
                # glue-evasion ``_EMPTY_SUBST_RE`` undoes for the token walk;
                # server-side GPT review, bash-measured).  Marking it glued
                # instead handed the evasion a free pass: the glued word was
                # excluded from the kill match and the segment lost its anchor.
                if proven and not body.strip():
                    # The word is OPEN even when the expansion vanishes: a
                    # ``#`` right after ``$()`` is a word to bash (comments
                    # are lexed before expansion), not a comment.
                    open_word = True
                    i = off + 2 + rel
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
                fresh_word = not word and not open_word
                if not fresh_word and not _kill_prefix_keeps_anchor(words, word):
                    glued = True
                record_body(body)
                if fresh_word:
                    # A word that IS a substitution stands where its OUTPUT
                    # stands: ``$(echo kill) $(pgrep -f <name>)`` runs kill.
                    # The synthetic word keeps positions honest too -- later
                    # bodies in the segment no longer share this one's index.
                    words.append((_static_substitution_output(body), False))
                end_word()
                if not proven:
                    i = n
                    jumped = True
                    break
                i = off + 2 + rel
                state, ansi = step.state, step.ansi
                jumped = True
                break
            if not escaped and not in_single and ch == "`":
                closer = _backtick_closer(source, off + 1)
                body = source[off + 1 : closer if closer != -1 else n]
                # Empty backticks: same word-continuity rule as ``$()``.
                if closer != -1 and not body.strip():
                    open_word = True
                    i = closer + 1
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
                fresh_word = not word and not open_word
                if not fresh_word and not _kill_prefix_keeps_anchor(words, word):
                    glued = True
                record_body(body)
                if fresh_word:
                    words.append((_static_substitution_output(body), False))
                end_word()
                if closer == -1:
                    i = n
                    jumped = True
                    break
                i = closer + 1
                state, ansi = step.state, step.ansi
                jumped = True
                break
            if step.active:
                if ch in "<>" and source.startswith("(", off + 1):
                    rel, proven = _matching_close_paren(source[off + 2 :], 0)
                    fresh_word = not word and not open_word
                    if not fresh_word and not _kill_prefix_keeps_anchor(words, word):
                        glued = True
                    record_body(source[off + 2 : off + 1 + rel] if proven else source[off + 2 :])
                    if fresh_word:
                        # A process substitution expands to a /dev/fd PATH,
                        # never to its own stdout -- no static output here.
                        words.append(("\x00", False))
                    end_word()
                    if not proven:
                        i = n
                        jumped = True
                        break
                    i = off + 2 + rel
                    state, ansi = step.state, step.ansi
                    jumped = True
                    break
                if ch in "&|" and (
                    (word and word[-1] in "<>") or (ch == "&" and not word and source.startswith(">", off + 1))
                ):
                    # The full redirect grammar audited against the separator
                    # set (this class produced three review rounds one spelling
                    # at a time -- ``2>&1``, ``&>``, ``>|``): a ``&`` or ``|``
                    # riding a trailing ``<``/``>`` is a descriptor duplication
                    # or the noclobber override, and a leading ``&>``/``&>>``
                    # redirects both streams -- all redirects of THIS command,
                    # never separators of it.  ``;`` and newline appear in no
                    # redirect spelling, which closes the enumeration.
                    word.append(ch)
                    continue
                if ch in ";|&\n":
                    end_segment()
                    continue
                if ch == "#" and not word and not open_word:
                    newline = source.find("\n", off)
                    i = n if newline == -1 else newline
                    state, ansi = 0, False
                    jumped = True
                    break
                if ch in "()" or ch.isspace():
                    end_word()
                    continue
            if not escaped and ch in "'\"" and (step.active or step.state == 0):
                # Quote SYNTAX: an opener (active) or a closer (back at state
                # 0).  bash does not pass these on, so the word must not carry
                # them -- ``k''ill`` is the word ``kill``.  A quote that is
                # DATA (inside the other quote type, or escaped) falls through
                # and stays in the word.
                open_word = True
                continue
            word.append(ch)
            open_word = True
        if not jumped:
            break
    end_segment()
    return bodies


def _backtick_closer(source: str, start: int) -> int:
    """Index of the backtick that CLOSES a substitution opened before *start*.

    Within backticks bash strips a backslash before ``$``, ``\\``` and ``\\\\``,
    so an escaped backtick is data and must not be taken as the closer --
    ``str.find`` did, and it truncated ``kill `printf '\\`' ; pgrep -f <name>```
    one clause short of the target's name (found in pre-push review, bash-
    measured: the inner command past the escaped backtick runs).  Quotes do NOT
    protect a backtick from closing, so this scan honours backslashes only.

    -1 when no unescaped closer exists before the text ends.
    """
    j = start
    n = len(source)
    while j < n:
        if source[j] == "\\":
            j += 2
            continue
        if source[j] == "`":
            return j
        j += 1
    return -1


def _is_self_kill(text_lower: str) -> bool:
    """True if *text_lower* terminates a KiroCrew process.

    Two shapes, matched separately because the two kill families take different
    kinds of target:

    * ``pkill``/``killall`` select processes BY NAME, so the product name in any
      argument IS the target -- including inside a quoted pattern such as
      ``pkill -f '[;]*kirocrew'``, where a raw-string regex mis-reads the quoted
      ``;`` as a command separator and stops scanning short of the name.
    * bare ``kill`` takes PIDs, so it can only aim at the product through a
      command substitution that resolves the name to one (``kill $(pgrep -f
      kirocrew)``, ``kill $(pidof kirocrew)``, ``kill $(cat /run/kirocrew.pid)``,
      backticks).  A ``kill <pid>`` alongside a command that merely mentions a
      product path is NOT a self-kill -- that is the false positive this
      replaced.
    """
    # Perf short-circuit (#3603): both loops below re-run the payload descent.
    # A kill can only target the product if the gate's necessary condition
    # holds, so a miss skips both descents.
    if not _self_floor_can_fire(text_lower):
        return False
    for tokens in _self_token_frames(text_lower):
        programs = _argv_programs(tokens)
        for i, token in enumerate(tokens):
            if not _is_kill_by_name_program(token):
                continue
            # ``echo pkill kirocrew`` prints two words; it does not kill anything.
            if _data_consumer_exempt(i, token, programs, tokens):
                continue
            # Check each argument for the target BEFORE testing whether it ends the
            # argv, then stop.  Order matters: the target is often a quoted pattern
            # whose own characters look like separators (``pkill -f '[;]*kirocrew'``),
            # so testing the boundary first would discard the very argument that
            # names the target.  Stopping after it keeps an unrelated later command
            # out of the match (``pkill other; echo kirocrew`` is not a self-kill).
            depth = 0
            for arg in tokens[i + 1 :]:
                # Search the raw arg AND its normalized form.  Normalizing alone is
                # not enough: a pkill pattern is an ERE, so a ``>`` inside it is part
                # of the TARGET (``pkill -f '>kirocrew'``) and stripping it as a
                # redirect would discard the name.  Raw alone is not enough either --
                # an empty substitution (``kiro$()crew``) only reads as the name once
                # removed.  Either match is a hit.
                if _SELF_NAME_RE.search(_debracket(arg)) or _SELF_NAME_RE.search(
                    _normalize_operand(arg)
                ):
                    return True
                depth += _substitution_depth_delta(arg)
                if depth <= 0 and _ends_argv(arg):
                    break
                depth = max(depth, 0)
    # Bare ``kill`` whose PID comes out of a substitution naming the product.
    # The VERB is matched on tokens (so ``/usr/bin/kill``, ``$(which kill)`` and a
    # quoted spelling all count -- a raw-text pattern anchored on separators sees
    # the ``/`` and misses the path-qualified form), while the substitution BODY is
    # taken from the whole string: segment splitting cuts on ``$(`` and ``)``,
    # which would separate the verb from its own substitution.
    for source, frame in _shell_payload_walk(text_lower):
        for i, token in enumerate(frame):
            if _program_basename(token) != "kill":
                continue
            # Scan only the substitutions in THIS kill's own argv.  Scanning the whole
            # command associated every substitution with any ``kill`` on the line, so
            # ``kill 123; echo $(cat /tmp/kirocrew)`` was denied for a substitution
            # belonging to a different command.
            own = [token]
            depth = 0
            for later in frame[i + 1 :]:
                own.append(later)
                # A separator INSIDE a substitution belongs to the substitution, not to
                # this command line: ``kill $(echo x; pgrep <name>)`` is ONE argument, so
                # ending the scan at that ``;`` would drop the half naming the target.
                depth += _substitution_depth_delta(later)
                if depth <= 0 and _ends_argv(later):
                    break
            # An operand of THIS kill that resolves to the protected name is a self-kill.
            # `kill` takes PIDs, so a bare name is not something a person types -- it gets
            # there by expansion, and the expansion that produces it is a lookup of our own
            # processes (``P=$(pgrep <name>); kill $P``).  Scoped to the kill's own argv by
            # the same walk that keeps ``kill 8123 && cp /tmp/<name>.json ~/`` allowed:
            # there the name is an operand of ``cp``, not of the kill.
            for operand in own[1:]:
                if _SELF_NAME_RE.search(_normalize_operand(operand)):
                    return True
            for body in _substitution_bodies(" ".join(own)):
                # ``kill $(pgrep -f kiro${x:-crew})`` hides the name behind an
                # expansion whose literal branch the shell substitutes back in, so
                # resolve those defaults before searching.
                if _SELF_NAME_RE.search(_debracket(body)) or _SELF_NAME_RE.search(
                    _resolve_param_defaults(body)
                ):
                    return True
        # The window above is bounded by ``_substitution_depth_delta`` on
        # DE-QUOTED tokens, so a quoted close-paren reads as a real closer and
        # closes the window early, dropping the clause that names the target
        # (``kill $(printf ')' ; pgrep -f kirocrew)``).  Re-derive the same
        # window from the RAW text, where the quotes still exist (#8633).
        for body in _bare_kill_raw_bodies(source):
            if _SELF_NAME_RE.search(_debracket(body)) or _SELF_NAME_RE.search(
                _resolve_param_defaults(body)
            ):
                return True
    return False


# ── Self-protection subcommand floor (argv-structural) ──────────────────────
# ``restart`` / ``update`` / ``gateway restart`` / ``cloud <destructive>`` each
# run a privileged self-action. The regex tier matches these on raw text, which
# the shell's own de-escaping defeats: ``kirocrew -\v restart`` (backslash escape
# -> ``-v``), ``kirocrew \restart`` (escaped subcommand letter) and
# ``kirocrew -\<newline>v restart`` (line continuation) all reach the shell as the
# plain command but split a token in the raw string the regex sees. Matching on
# the tokenized argv -- the same de-escaped, de-quoted view the kill/token floors
# use (``_self_token_frames``) -- resolves every such spelling before the check.
# The floor is a UNION with the regex tier, never a replacement: the regex still
# catches a payload the tokenizer cannot see into (``bash -c "kirocrew restart"``)
# and the ``python -m kiro_crew restart`` module form (``kiro.?crew`` + verb) (#4824).
_SELF_CLOUD_DESTRUCTIVE_VERBS: frozenset[str] = frozenset(
    {"destroy", "stop", "start", "launch", "connect", "tunnel", "login", "logout"}
)

# A shell removes ``backslash + newline`` while lexing (line continuation), so
# ``kirocrew \<newline>restart`` runs ``kirocrew restart``. ``shlex`` instead keeps
# the escaped newline as a literal in the token, so the floor pre-joins it to
# model the shell before tokenizing. Scoped to the floor's own tokenizer input
# (NOT a catalog-wide rewrite of the matched text): it only shapes the argv the
# self-protection predicates see, so it cannot over-block an unrelated rule.
_SHELL_LINE_CONTINUATION_RE = re.compile(r"\\\r?\n")


def _shell_join_continuations(text: str) -> str:
    """Collapse bash ``\\<newline>`` line continuations, as the shell does pre-lex."""
    return _SHELL_LINE_CONTINUATION_RE.sub("", text)


def _fold_line_continuations(text: str) -> str:
    """Remove ``\\<newline>`` exactly where a shell removes it -- quote-aware.

    A shell folds a backslash-newline while lexing, so ``"r\\<newline>m" -rf /``
    runs ``rm -rf /``.  The deny tiers match text and ``_split_segments`` cuts on
    the newline, so without folding the continuation is severed before any view is
    built and every rule authored as a command shape misses that spelling.

    ``_shell_join_continuations`` above looks like the answer and is NOT: it is a
    bare regex that folds inside SINGLE quotes too, and its comment scopes it
    deliberately to the self-protection floor's tokenizer input rather than to the
    matched text of the whole catalog.  Applying it here would fold
    ``echo 'r\\<newline>m -rf /'`` -- which bash prints literally -- into a denial.

    The contexts were measured against bash rather than assumed (``printf %q`` on
    the resulting argv):

    ======================  ==================  ========
    spelling                bash argv           folded?
    ======================  ==================  ========
    ``A\\<nl>A BB``          ``<AA><BB>``        yes
    ``"A\\<nl>A" BB``        ``<AA><BB>``        yes
    ``'A\\<nl>A' BB``        ``<A\\<nl>A><BB>``   no
    ``$'A\\<nl>A' BB``       ``<A\\<nl>A><BB>``   no
    ======================  ==================  ========

    So: fold unquoted and inside double quotes; preserve inside single quotes and
    inside ANSI-C (``$'…'``) spans.  ``$"…"`` follows the double-quote rule, which
    falls out of the scan because only ``$'`` opens a preserving span.

    Runs BEFORE the ANSI-C decode, which is the shell's own order: continuations
    are removed while lexing, and the escape body is interpreted after -- so a
    preserved ``\\<newline>`` inside ``$'…'`` stays part of that literal.

    An unterminated quote simply runs to the end in that state; the scan never
    raises, because it feeds the permission gate.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    # None = unquoted, "'" = single, '"' = double, "$'" = ANSI-C.
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is None:
            if text.startswith("$'", i):
                quote = "$'"
                out.append("$'")
                i += 2
                continue
            if ch in "'\"":
                quote = ch
                out.append(ch)
                i += 1
                continue
            if ch == "\\" and i + 1 < n:
                folded = _continuation_width(text, i)
                if folded:
                    i += folded
                    continue
                # The backslash escapes the next character, so that character
                # cannot open a quote -- consume the pair together.
                out.append(text[i : i + 2])
                i += 2
                continue
            out.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                folded = _continuation_width(text, i)
                if folded:
                    i += folded
                    continue
                out.append(text[i : i + 2])
                i += 2
                continue
            if ch == '"':
                quote = None
            out.append(ch)
            i += 1
            continue
        if quote == "$'":
            # A backslash escapes the next character (including the closing quote),
            # and a continuation inside this span is LITERAL -- both are consumed
            # as a pair, which preserves them.
            if ch == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if ch == "'":
                quote = None
            out.append(ch)
            i += 1
            continue
        # Single quotes: nothing is special, not even a backslash.
        if ch == "'":
            quote = None
        out.append(ch)
        i += 1
    return "".join(out)


def _continuation_width(text: str, i: int) -> int:
    """Characters to drop for a continuation at *i*, or 0 if there is none.

    ``text[i]`` is known to be a backslash.  Handles both ``\\n`` and ``\\r\\n``
    line endings so a CRLF command is folded the same way.
    """
    if text.startswith("\\\n", i):
        return 2
    if text.startswith("\\\r\n", i):
        return 3
    return 0


def _redirect_consumes_next(token: str) -> "tuple[bool, bool]":
    """Classify *token* as a shell redirection sitting in argv position.

    Returns ``(is_redirect, expects_separate_target)``. A redirection is removed
    from argv by the shell and may appear ANYWHERE in a simple command, so it is
    never a CLI operand: ``kirocrew 2>/tmp/x restart`` and ``kirocrew > /tmp/x
    restart`` both run ``restart``, and the residue (the fd ``2``, or the target
    ``/tmp/x``) must not be mistaken for the leading subcommand.

    Quoting is already resolved by tokenization, so a remaining ``<``/``>`` is an
    operator. The target rides in the SAME token for ``2>/tmp/x`` / ``2>&1`` /
    ``>>/tmp/x`` (``expects_separate_target`` False); a bare ``>`` / ``2>`` / ``>&``
    takes the NEXT token as its target (True). A leading fd number is part of the
    operator, not an operand.
    """
    cut = min((token.find(c) for c in "<>" if c in token), default=-1)
    if cut == -1:
        return (False, False)
    return (True, token[cut:].lstrip("<>&") == "")


def _self_cli_operands(tokens: "list[str]", i: int) -> "list[str]":
    """Non-flag operand words the product CLI at program index *i* receives, in order.

    A token that stays a ``-``/``--`` word after quote/redirect normalization is a
    global flag and is skipped -- the self-protection top-level flags are all
    valueless (``-v``/``--verbose`` count, ``--no-jail`` bool), so a skipped flag
    never hides an operand behind it. A shell redirection (and its separate
    target, if any) is removed from argv by the shell and is skipped too, so
    ``kirocrew 2>/tmp/x restart`` still reads ``restart`` as the leading operand.
    Quoting is resolved by ``_normalize_operand``; the walk stops at the argv
    boundary so a chained later command's words are not attributed here.
    """
    operands: "list[str]" = []
    depth = 0
    skip_target = False
    for later in tokens[i + 1 :]:
        is_redirect, expects_target = _redirect_consumes_next(later)
        if skip_target:
            # A separate redirection target (``> FILE``) is a filename: its bytes are
            # data, not an argv boundary, so a quoted ``;``/``|`` in it (``> 'a;b'``)
            # must NOT end the scan. Consume it without the boundary/depth bookkeeping.
            skip_target = False
            continue
        if is_redirect:
            # The redirection operator itself is not an operand and never ends the argv.
            skip_target = expects_target
            continue
        operand = _normalize_operand(later)
        # ANSI-C ($'...') and locale ($"...") quoting: shlex strips the quotes
        # but leaves the leading ``$``, so a flag hidden as ``$'-v'`` / the hex
        # ``$'\x2d\x76'`` reads as a non-flag operand and shoves the subcommand
        # to second place. Drop the ``$`` and decode the escapes to the value the
        # shell actually passes -- the same de-quoting _program_basename already
        # does for the program name.
        if operand.startswith("$") and not operand.startswith(("$(", "${")):
            operand = _decode_printf_escapes(operand[1:])
        if operand and not operand.startswith("-"):
            operands.append(operand)
        depth += _substitution_depth_delta(later)
        if depth <= 0 and _ends_argv(later):
            break
        depth = max(depth, 0)
    return operands


def _operands_lead_with(operands: "list[str]", spec: "tuple[object, ...]") -> bool:
    """True if *operands* begins with the subcommand sequence *spec*.

    Each element of *spec* is an exact word, or a ``frozenset`` of accepted words
    (used for ``cloud <one of the destructive lifecycle subcommands>``).
    """
    if len(operands) < len(spec):
        return False
    for got, want in zip(operands, spec):
        if isinstance(want, frozenset):
            if got not in want:
                return False
        elif got != want:
            return False
    return True


class _SelfModuleScan(NamedTuple):
    """One token list's normalized forms plus its module-flag stop index.

    ``norm[j]`` is what :func:`_normalize_operand` makes of token *j*, and ``stops[j]``
    is the first index at or after *j* where the module-flag scan in
    :func:`_self_module_name_index` stops.  Both are computed once per token list so
    the scan does not repeat them for every interpreter token in it.
    """

    norm: "list[str]"
    stops: "list[int]"


def _is_self_module_flag(tok: str) -> bool:
    """True where the module-flag scan in :func:`_self_module_name_index` stops.

    The attached spelling only stops when the regex actually matches: ``-msomething``
    that is not our module is an ordinary interpreter flag and the scan continues past
    it, so the regex is part of the stop condition rather than a check made after it.
    """
    return tok == "-m" or (
        tok.startswith("-m") and len(tok) > 2 and bool(_SELF_IMPORT_RE.search(tok[2:]))
    )


def _self_module_flag_scan(tokens: "list[str]") -> "_SelfModuleScan":
    """Precompute one token list's normalized forms and module-flag stop indexes.

    ``_self_module_name_index`` walked forward from each interpreter token to the first
    module flag, normalizing every token it passed.  Called once per interpreter token
    by ``_self_program_index``, that made the self-protection floor QUADRATIC in token
    count: a command of interpreter words with no module flag among them re-walked and
    re-normalized the whole tail every time.  Measured on the floor path, with one
    product word present so its keyword gate opens: 0.03 s / 0.12 s / 0.49 s / 1.92 s
    at 250 / 500 / 1000 / 2000 tokens -- about 4x per doubling, which reaches the
    gateway's loop watchdog well inside a command an agent could emit.  Both passes
    here are single and linear.
    """
    limit = len(tokens)
    norm = [_normalize_operand(token).strip("\"'") for token in tokens]
    stops = [limit] * (limit + 1)
    for index in range(limit - 1, -1, -1):
        stops[index] = index if _is_self_module_flag(norm[index]) else stops[index + 1]
    return _SelfModuleScan(norm=norm, stops=stops)


def _self_module_name_index(tokens: "list[str]", i: int, scan: "_SelfModuleScan") -> "int | None":
    """Index of the product module-name token in a ``python -m kiro_crew ...``
    invocation whose interpreter is at *i*, or None.

    Handles the separate (``-m kiro_crew``) and attached (``-mkiro_crew``) spellings,
    scanning past other interpreter flags. The ``-c`` inline-program form has no
    positional subcommand token (the program builds its own argv), so it is left to
    the credential-mint import gate rather than matched here.

    *scan* is REQUIRED, and must be :func:`_self_module_flag_scan` of the same *tokens*.
    It is not optional-with-a-fallback on purpose: this function is called once per
    token by a loop over those tokens, so a caller that could omit the scan could
    silently reintroduce the quadratic this precompute exists to remove.  Requiring it
    makes that a type error instead of a performance regression nobody notices.
    """
    limit = len(tokens)
    j = scan.stops[i + 1]
    if j >= limit:
        return None
    if scan.norm[j] == "-m":
        nxt = scan.norm[j + 1] if j + 1 < limit else ""
        return j + 1 if _SELF_IMPORT_RE.search(nxt) else None
    return j  # attached -mkiro_crew


def _self_program_index(tokens: "list[str]", i: int, scan: "_SelfModuleScan") -> "int | None":
    """The argv index whose trailing operands the product CLI receives when the token
    at *i* launches it: *i* itself for the direct ``kirocrew`` form, or the module-name
    index for ``python -m kiro_crew``; else None.

    *scan* is threaded through to :func:`_self_module_name_index` and is required for
    the reason given there.
    """
    if _is_self_program(tokens[i]):
        return i
    if _PYTHON_PROGRAM_RE.match(_program_basename(tokens[i])):
        return _self_module_name_index(tokens, i, scan)
    return None


def _matches_self_subcommand(text_lower: str, spec: "tuple[object, ...]") -> bool:
    """True if the product CLI is invoked with leading operand words *spec*.

    Covers the direct form (``kirocrew`` as the argv program) and the module form
    (``python -m kiro_crew``), collecting operands after the CLI/module so the same
    shell de-escaping the regex tier cannot see is caught for both -- e.g.
    ``python -m kiro_crew -\\v restart``, which the interpreter-position regex misses.
    """
    if not _self_floor_can_fire(text_lower):
        return False
    for tokens in _self_token_frames(_shell_join_continuations(text_lower)):
        programs = _argv_programs(tokens)
        # Once per FRAME, not once per token: this is the loop whose per-token scan
        # made the floor quadratic.
        scan = _self_module_flag_scan(tokens)
        for i in range(len(tokens)):
            prog_idx = _self_program_index(tokens, i, scan)
            if prog_idx is None:
                continue
            # ``echo kirocrew restart`` / ``echo python -m kiro_crew restart`` print words.
            if _data_consumer_exempt(prog_idx, tokens[prog_idx], programs, tokens):
                continue
            if _operands_lead_with(_self_cli_operands(tokens, prog_idx), spec):
                return True
    return False


def _is_self_restart(text_lower: str) -> bool:
    """``kirocrew restart`` behind any shell dressing of interposed flags."""
    return _matches_self_subcommand(text_lower, ("restart",))


def _is_self_update(text_lower: str) -> bool:
    """``kirocrew update`` behind any shell dressing of interposed flags."""
    return _matches_self_subcommand(text_lower, ("update",))


def _is_self_gateway_restart(text_lower: str) -> bool:
    """``kirocrew gateway restart`` behind any shell dressing of interposed flags."""
    return _matches_self_subcommand(text_lower, ("gateway", "restart"))


def _is_self_cloud_destructive(text_lower: str) -> bool:
    """``kirocrew cloud <destructive>`` behind any shell dressing of interposed flags."""
    return _matches_self_subcommand(text_lower, ("cloud", _SELF_CLOUD_DESTRUCTIVE_VERBS))


_DEV_MODE_CONFIRM_FLAG = "--confirm-out-of-install-root"


def _is_dev_mode_out_of_root_confirm(text_lower: str) -> bool:
    """True if the operator's out-of-install confirm flag materializes after de-escaping.

    The regex tier matches the flag in RAW text, so quote-splitting inside the
    token (``--confirm-out-of-install-'root'``) reaches argparse as the accepted
    flag while the raw command never contains the literal.  This floor closes
    that class two ways: the whole string with quote/backslash glue removed
    (covers every quoting spelling in one O(n) pass), and every tokenized argv
    frame — the same descent the other floors use — whose payload walk also
    decodes printf/``$'…'`` escapes the glue-strip cannot see.

    Unlike the subcommand floors this predicate keys on the FLAG token, not on
    the product CLI being the argv program: the rule is deliberately broad (see
    its catalog comment), so a mention inside any command is a deny.  It matches
    the flag only as a token PREFIX boundary — ``--confirm-out-of-install-root``
    itself or with an attached ``=…``/wrapper — never as a substring of prose,
    because the leading ``--`` and full spelling make accidental prose hits
    implausible and the regex tier already denies them anyway.
    """
    # Cheap necessary condition: the flag cannot materialize from text that,
    # after glue removal, carries neither of its distinctive words unless an
    # escape encoding (backslash / ANSI-C quoting) could synthesize them.
    stripped = _SELF_FLOOR_QUOTE_JUNK_RE.sub("", text_lower)
    if _DEV_MODE_CONFIRM_FLAG in stripped:
        return True
    if "confirm" not in stripped and "install" not in stripped and "\\" not in text_lower:
        return False
    for tokens in _self_token_frames(text_lower):
        for token in tokens:
            if _DEV_MODE_CONFIRM_FLAG in _SELF_FLOOR_QUOTE_JUNK_RE.sub(
                "", _normalize_operand(token)
            ):
                return True
    return False


def _is_git_publish(text_lower: str) -> bool:
    """Return True if *text_lower* invokes ``git push`` (verb-anchored).

    Uses a two-pass approach:

    1. **Fast first-pass (regex):** ``_GIT_PUBLISH_RE`` and
       ``_GIT_PUBLISH_GLUE_RE`` catch normal ``git push`` invocations and
       command-substitution glue-evasion (e.g. ``git$(echo ' ')push``);
       ``_GIT_PUBLISH_SUBST_PROGRAM_RE`` catches expansion-produced program
       names (``$(echo git) push``, ``${GIT} push``, ``$GIT push``).
    2. **Normalizer second-pass:** ``normalize_shell_command`` strips quotes
       and empty-string concatenation so evasions like ``"git" push``,
       ``g""it push``, or ``'g'it push`` are resolved to their true tokens.

    Does NOT match ``git stash push``, ``git commit -m '...push...'``,
    ``git log --grep push``, etc.

    Operates on an already-lowercased string.
    """
    # Pass 1: regex fast-path
    if (
        _GIT_PUBLISH_RE.search(text_lower)
        or _GIT_PUBLISH_GLUE_RE.search(text_lower)
        or _GIT_PUBLISH_SUBST_PROGRAM_RE.search(text_lower)
    ):
        return True

    # Pass 2: normalizer-based detection (catches quote evasions like
    # "git" push, g""it push, 'g'it push)
    return _is_git_push_via_normalizer(text_lower)


# Git global flags that consume a separate argument token (appear between
# `git` and the subcommand).
_GIT_ARG_FLAGS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace"})


def _is_git_push_via_normalizer(text_lower: str) -> bool:
    """Normalizer-based git push detection (second pass).

    Tokenizes the command via ``normalize_shell_command``, then checks if
    any token sequence resolves to ``git`` followed by ``push`` as the
    subcommand (skipping flags and their arguments, and skipping empty or
    whitespace-only words in the subcommand seek, which git never resolves
    a command name from -- issue #8115).

    Avoids false positives on ``git stash push`` by requiring ``push`` to
    be the FIRST non-flag token after ``git`` (the subcommand position).
    """
    try:
        tokens = normalize_shell_command(text_lower)
    except Exception:
        return False

    if not tokens:
        return False

    # Glued operators are not part of the word: ``(git`` is the git program and
    # ``push)`` is the push subcommand. But these tokens come from
    # ``normalize_shell_command``, which has ALREADY tokenized and dequoted, so
    # punctuation surviving inside a token is part of the WORD -- and cutting
    # there truncated a legal executable path (``/opt/my(dir)/git`` ->
    # ``/opt/my``, whose basename is not ``git``), which NARROWED detection and
    # let a protected push through. Replacing the token was therefore not the
    # widen-only step its previous comment claimed.
    #
    # Both spellings are consulted instead, so the claim actually holds: a token
    # counts when EITHER its raw form or its operator-cut form resolves to the
    # word. That is a superset of both readings, and detection can only ever
    # grow -- the allow/deny decision still rests with
    # ``_is_push_to_protected_branch``.
    def _resolves_to(token: str, word: str) -> bool:
        for candidate in (token, _cut_at_operator(token)):
            if candidate == word or os.path.basename(candidate) == word:
                return True
        return False

    i = 0
    while i < len(tokens):
        token = tokens[i]
        # Check if this token resolves to "git"
        if _resolves_to(token, "git"):
            # Skip global flags and their arguments to find the subcommand.
            #
            # A zero-width or whitespace-only word is also skipped (issue
            # #8115).  It is a real argv element the shell hands over, and git
            # does NOT ignore it -- git takes it as its command name and
            # exits.  Skipping it is deliberate fail-closed OVER-detection: it
            # widens only DETECTION, and a spelling it newly reaches either
            # fails to run at all (git rejects the zero-width command name) or
            # was already reached in its adjacent spelling, so no runnable
            # push gains an escape.  What the floor DOES with a newly-detected
            # spelling is the ungated anti-obfuscation branch, not the
            # protected-branch rule: ``_git_push_args`` anchors on the raw
            # split and does not skip the empty word, so the parse fails and
            # ``_git_publish_floor_tags`` denies unconditionally
            # (``_GIT_PUBLISH_UNGATED``) -- the right treatment for a spelling
            # git itself cannot run.  ``str.strip()``'s whitespace set is
            # wider than POSIX IFS (NBSP, U+2000..200A, ...) and deliberately
            # so: every extra character it treats as skippable is still a word
            # git takes as its command name and rejects, and a skipped token
            # can never be the subcommand token, so the breadth only ever ADDS
            # detection -- do not narrow it to a literal space/tab set.  No
            # matching guard is needed in program position: a zero-width word
            # never resolves to the program word (``_resolves_to`` cannot
            # yield ``git`` from it), so the outer loop already steps past it.
            j = i + 1
            while j < len(tokens):
                if not tokens[j].strip():
                    j += 1  # zero-width/whitespace-only word (issue #8115)
                elif tokens[j] in _GIT_ARG_FLAGS:
                    j += 2  # skip flag + its argument
                elif tokens[j].startswith("-"):
                    j += 1  # skip simple flag
                else:
                    break
            if j < len(tokens) and _resolves_to(tokens[j], "push"):
                return True
        i += 1
    return False


# ── Feature-branch push gate ──
# ``_is_git_publish`` only detects that a command IS a ``git push``.  The
# decision of whether to ALLOW it is made by ``_is_push_to_protected_branch``
# at the single enforcement point in ``is_denied``.  The push detector is a
# pure predicate (no side effects); the deny audit (``_emit_deny_event``) and
# the allow audit (``_schedule_push_allow_audit``) are emitted by the caller so
# the SEL trail always reflects the FINAL outcome (never an allow for a command
# that is ultimately denied by a later glob pattern).

# Protected branch names that ``git push`` must never target directly.  A push
# to any of these (or a bare push, which may resolve to one) is blocked so the
# change goes through the normal PR/code-review flow.  KiroCrew (OSS) uses
# ``main``; ``mainline``/``master`` are covered for internal/mirror clones.
#: Shell metacharacters that can be GLUED to a word without whitespace, so they
#: appear inside a naive ``split()`` token while bash treats them as operators.
#: ``(git push origin main)&`` hands the ref token ``main)&``, and stripping only
#: the parens left ``main)&``, which never equalled ``main`` -- so a
#: protected-branch push was allowed AND audited as a feature-branch push. A git
#: ref cannot legally contain any of these, so stripping them cannot swallow a
#: real branch name; a QUOTED paren is still preserved because both call sites
#: strip before removing quotes.
_SHELL_OPERATOR_CHARS = "()&;|<>"


class _ShellChar(NamedTuple):
    """One step of the shell's quote/escape state machine."""

    #: Offset in the source text where this step's raw ``text`` begins. Named
    #: ``offset`` rather than ``index`` because a NamedTuple field cannot shadow
    #: ``tuple.index``.
    offset: int
    #: The raw text consumed -- TWO characters for a backslash escape pair, so a
    #: consumer that rebuilds a word by appending ``text`` preserves the spelling.
    text: str
    #: The SIGNIFICANT character: for an escape pair, the character escaped.
    char: str
    #: True only where shell SYNTAX lives: unquoted and unescaped. An operator, a
    #: separator or whitespace is structure here and data everywhere else.
    active: bool
    #: Quote state AFTER this step: 0 normal, 1 single-quoted, 2 double-quoted.
    state: int
    #: Whether an open single quote is an ANSI-C ``$'...'``, where a backslash
    #: escapes rather than standing for itself.
    ansi: bool
    #: The text ended on a backslash with nothing to escape -- so whatever split
    #: this text off was itself escaped, and the word continues past it.
    trailing_escape: bool


def _iter_shell_chars(text: str, state: int = 0, ansi: bool = False) -> "Iterator[_ShellChar]":
    """THE shell quote/escape state machine. Every push-path reading of shell
    quoting walks through this one generator.

    Four readings used to keep their own copy, and they did not agree. The word
    splitter had no ANSI-C awareness while the boundary walk did, so in
    ``git push origin feature > >(echo $'a\\'b') main`` the splitter read the
    ESCAPED quote as a real closer, reopened on the next quote, and fused the
    trailing ``main`` into one unterminated word -- the boundary walk then proved
    its parenthesis correctly, but the protected refspec was already trapped
    inside the word it had been handed. That is the same two-scanners defect this
    module was already cured of twice (two tokenizers, then two paren counters),
    so the cure is structural: one machine, several consumers, no second opinion
    to drift from.

    Bash's rules, once: a backslash escapes the next character outside quotes,
    inside double quotes, and inside ``$'...'``, but is LITERAL inside a plain
    single quote; an escaped quote is data and closes nothing. The ``$'``
    lookback is within-text, which is correct because whitespace cannot sit
    between the ``$`` and its quote, and it is EXACT: only a literal, unpaired
    ``$`` (an odd run -- ``$$`` is the PID parameter, ``\\$`` is data) opens
    ANSI-C. That exactness is load-bearing now that the segment split and the
    program anchor read this state in the allow direction: a walk that ends
    "still open" where bash closed hides a separator or a ``git`` word.

    *state* and *ansi* resume a walk, which is what lets a quoted word spanning
    whitespace be read without desyncing.
    """
    i = 0
    n = len(text)
    dollar_run = 0  # consecutive LITERAL ``$`` immediately before this char
    while i < n:
        ch = text[i]
        if ch == "\\" and (state != 1 or ansi):
            dollar_run = 0  # an escaped ``$`` is data and introduces nothing
            if i + 1 >= n:
                yield _ShellChar(i, ch, ch, False, state, ansi, True)
                return
            yield _ShellChar(i, text[i : i + 2], text[i + 1], False, state, ansi, False)
            i += 2
            continue
        was_unquoted = state == 0
        if state == 0:
            if ch == "'":
                state = 1
                # ``$'`` opens an ANSI-C string only when the ``$`` is itself
                # literal and unpaired: an escaped ``\$`` is data, and in a run
                # of dollars the shell pairs them off (``$$`` is the PID
                # parameter), so only an ODD run leaves a ``$`` to introduce the
                # quote. The plain ``text[i - 1] == "$"`` lookback read
                # ``\$'foo\'`` as ANSI-C, kept the quote open across a ``;``, and
                # hid the publish behind it -- with the segment split and the
                # program anchor now reading this state, a false "still open" is
                # an ALLOW-direction error, not a mere over-flag.
                ansi = dollar_run % 2 == 1
            elif ch == '"':
                state = 2
        elif state == 1:
            if ch == "'":
                state = 0
        elif ch == '"':
            state = 0
        dollar_run = dollar_run + 1 if was_unquoted and ch == "$" else 0
        yield _ShellChar(i, ch, ch, was_unquoted, state, ansi, False)
        i += 1


def _matching_close_paren(text: str, open_end: int) -> "tuple[int, bool]":
    """``(index just past the matching ``)``, proven)`` for a paren opened before
    *open_end*, walked QUOTE-AWARELY through :func:`_iter_shell_chars`.

    THE one span computation for a substitution or subshell body. Both the
    git-publish boundary walk and the nested-payload EXTRACTOR read it, because a
    body extracted over a different span than the boundary proved is exactly how a
    nested command escapes the scan: ``git push origin my-feature > >(X=')' git
    push origin main)`` truncated the extracted body at the QUOTED ``)``, so the
    payload came back as ``X='`` and the nested publish of a protected branch was
    never scanned at all -- ``is_denied`` allowed a command bash runs.

    ``proven`` is False when the parens never balance before the text ends. The
    caller must fail CLOSED on that: for an extractor the safe reading is the
    whole remainder (scan more, never less).
    """
    depth = 1
    for step in _iter_shell_chars(text, 0, False):
        if step.offset < open_end:
            continue
        if step.active:
            if step.char == "(":
                depth += 1
            elif step.char == ")":
                depth -= 1
                if depth == 0:
                    return (step.offset + len(step.text), True)
    return (len(text), False)


def _cut_at_operator(token: str) -> str:
    """*token* up to the first GLUED shell operator, with leading ones removed.

    ``strip`` is not enough: an operator can sit in the MIDDLE of a naive
    ``split()`` token, so ``(git push origin mainline)>log`` hands the ref token
    ``mainline)>log`` -- which ends in ``g``, so stripping removed nothing and the
    ref never equalled ``mainline``. bash parses that as the ref ``mainline``
    followed by the operator ``)`` and the redirection ``>log``, so cutting at the
    first operator is what reproduces its reading.

    Quote state is TRACKED rather than bailed on. Inside quotes these characters
    are literal and a ref may legitimately contain them -- ``git push origin
    '(main)'`` targets a branch actually named ``(main)``, which is not protected
    and must stay pushable. But a quoted ref can still carry an operator OUTSIDE
    its quotes: ``(git push origin 'main')`` hands the ref token ``'main')``,
    whose trailing ``)`` is unquoted. Returning early on the mere PRESENCE of a
    quote left that ``)`` in place, so the ref resolved to ``main)``, never
    equalled ``main``, and the protected push was allowed AND audited as a
    feature-branch push -- reopening the exact class this cut exists to close.
    Cutting only at operators outside quotes satisfies both readings at once.

    An UNBALANCED quote leaves the remainder read as quoted, so nothing is cut.
    That is safe because such a token is not executable as written: bash has an
    unterminated quote and never runs the push. If a later quote in the command
    balances it, the shell folds the span into one word whose ref likewise no
    longer equals a protected name.

    Quotes are PRESERVED in the result; both call sites remove them afterwards
    (see ``_dequote_token``), which is what keeps ``'(main)'`` a literal ref.

    Escapes are honoured through the shared walk, so ``ma\\)in`` keeps its
    literal paren instead of being cut at it -- bash hands git the ref ``ma)in``.
    """
    out: list[str] = []
    for step in _iter_shell_chars(token):
        if step.trailing_escape:
            out.append(step.text)
            break
        if step.active and step.char in _SHELL_OPERATOR_CHARS:
            if not out:
                # Leading operator, e.g. the ``(`` of ``(git push ...``: bash
                # treats it as punctuation before the word, so drop and continue.
                continue
            break
        out.append(step.text)
    return "".join(out)


_PROTECTED_BRANCHES = {"main", "mainline", "master"}

# Push flags that push EVERY local branch (protected ones included) regardless
# of any explicit refspec, so a per-branch target check cannot vouch for them.
# Presence of any of these denies the push outright (kept in lockstep with the
# ``--(mirror|all)`` regex in config/defaults.json).
_PUSH_ALL_BRANCHES_OPTS = frozenset({"mirror", "all", "branches"})

#: Flags that CARRY the repository as their own value, so the repository is not
#: among the positional tokens. Git accepts ``--repo=<x>`` (and the separated
#: ``--repo <x>``), and both spellings start with ``-`` — so a naive "strip the
#: flags, the first positional is the remote" read treats the sole remaining
#: token as the REMOTE when it is really the refspec. That mis-parse routes
#: ``git push --repo=origin main`` to the single-arg rule instead of the
#: protected-branch rule, which was harmless only while the whole floor was
#: unconditional: once the rules became individually disableable, switching the
#: single-arg rule off published to ``main``.
_PUSH_REPO_OPTS = frozenset({"repo"})

#: The ARITY table (#7796): push options that take a REQUIRED value git also
#: accepts as a SEPARATED token (``--push-option ci.skip``). The token scan
#: must consume that value or it leaks into the positional list, where it is
#: read as a remote/refspec — and because an option value like ``ci.skip``
#: normalizes to a non-protected name, the tag set for an otherwise-bare
#: publish came back EMPTY. An empty tag set IS the allow decision, so one
#: extra flag switched the protected-branch floor off. Same shape as
#: ``_PUSH_REPO_OPTS`` (which stays separate because its value being the
#: REMOTE also shifts the positional split), resolved through
#: ``_push_option_matches`` so abbreviations keep working (finding 2).
#: Attached forms (``--push-option=x``) bind the value inside the token and
#: never disturb the split, so they need no entry here. (``repo`` itself is
#: deliberately NOT unioned in: the dedicated ``_PUSH_REPO_OPTS`` branch runs
#: first and would make the member unreachable — First Principles review.)
_PUSH_VALUE_OPTS = frozenset({"push-option", "receive-pack", "exec"})

#: Long push options that never consume the NEXT token: booleans, plus the
#: optional-value options (``--signed``, ``--force-with-lease``) whose value
#: git binds in ATTACHED form only. ``--no-*`` negations are recognised
#: structurally (git's negation never takes a separate value), so they are not
#: enumerated. ``recurse-submodules`` is deliberately ABSENT: listing an
#: option here vouches that its separated neighbour is a positional, and being
#: wrong about that is exactly the #7796 erasure — so an option whose arity is
#: not modelled with confidence falls to the protective fallback instead.
_PUSH_NO_VALUE_OPTS = frozenset(
    {
        "atomic",
        "delete",
        "dry-run",
        "follow-tags",
        "force",
        "force-if-includes",
        "force-with-lease",
        "ipv4",
        "ipv6",
        "porcelain",
        "progress",
        "prune",
        "quiet",
        "set-upstream",
        "signed",
        "tags",
        "thin",
        "verbose",
        "verify",
    }
)

#: Short-option arity, resolved the way git resolves a bundle: booleans may
#: stack (``-fq``), and the first value-taking short consumes the REST of the
#: token as its attached value (``-oci.skip``) or, when the rest is empty, the
#: NEXT token (``-o ci.skip`` — or ``-fo ci.skip``, which is ``-f -o ci.skip``).
_PUSH_VALUE_SHORTS = frozenset({"o"})
_PUSH_NO_VALUE_SHORTS = frozenset({"f", "n", "q", "v", "u", "d", "4", "6"})


class _ShellWalk(NamedTuple):
    """What one pass of the shell's quote/escape state machine observed."""

    #: The text split at its UNQUOTED ``<`` ``>`` ``&`` ``(`` ``)`` operators.
    pieces: list[str]
    #: True when at least one such operator was seen outside quotes.
    saw_operator: bool
    #: True when the text ends on a backslash that escapes the next character —
    #: i.e. whatever split this text off was itself escaped.
    trailing_escape: bool
    #: Quote state at the end: 0 normal, 1 single-quoted, 2 double-quoted. Feed
    #: it back in to resume the walk across a whitespace boundary.
    end_state: int
    #: Whether the still-open single quote was an ANSI-C ``$'...'``.
    end_ansi: bool
    #: Unquoted ``(`` minus unquoted ``)``. Quoted parens contribute NOTHING,
    #: which is what makes a process-substitution boundary provable.
    paren_delta: int


def _shell_quote_walk(text: str, state: int = 0, ansi: bool = False) -> _ShellWalk:
    """The ONE quote/escape state machine the push scan reads shell text with.

    Every signal the scan derives from shell quoting comes from this single pass,
    so no two readings of the same text can disagree: the operator split and the
    fragment signal (:func:`_push_token_shell_read`), and the unquoted paren
    depth that proves a process-substitution boundary
    (:func:`_git_push_args`). A second, quote-UNAWARE paren count is exactly how
    ``git push origin feature > >(echo '(' ) main`` published a protected branch:
    the quoted ``(`` inflated the depth, so the closing ``)`` never returned it
    to zero and the trailing ``main`` was swallowed into the substitution.

    Uses the shell's own rules, via :func:`_iter_shell_chars`, which owns the
    state machine: a backslash escapes the next character outside quotes, inside
    double quotes, and inside ``$'...'`` ANSI-C strings, but is LITERAL inside
    plain single quotes; an ESCAPED quote is data, not a delimiter.

    *state* and *ansi* resume a walk across a whitespace boundary, because a
    quoted word can span one (``>(echo 'a b')``).
    """
    pieces: list[str] = []
    buf: list[str] = []
    saw_operator = False
    trailing_escape = False
    paren_delta = 0
    for step in _iter_shell_chars(text, state, ansi):
        state, ansi = step.state, step.ansi
        if step.trailing_escape:
            trailing_escape = True
            break
        if step.active and step.char in "<>&()":
            saw_operator = True
            if step.char == "(":
                paren_delta += 1
            elif step.char == ")":
                paren_delta -= 1
            if buf:
                pieces.append("".join(buf))
                buf = []
            continue
        buf.append(step.text)
    if buf:
        pieces.append("".join(buf))
    return _ShellWalk(pieces, saw_operator, trailing_escape, state, ansi, paren_delta)


def _push_token_shell_read(token: str) -> "tuple[list[str] | None, bool]":
    """One quote/escape-state walk over a RAW (pre-dequote) token, returning
    ``(operator_pieces, open_state)``.

    ``operator_pieces`` — the token split at unquoted ``<`` ``>`` ``&``, or
    None when it carries none (the common case). A mid-word operator means
    the shell hands git a DIFFERENT word than this scan sees: ``main>log`` is
    the argument ``main`` plus the redirection ``>log``, i.e. it pushes main.
    The caller scans each piece as a refspec candidate so a protected name
    cannot hide behind operator glue; quoted operators are data and produce
    no split.

    ``open_state`` — True when the shell's quote/escape state has not
    RETURNED TO NORMAL by the token's end: an open quote or a trailing escape
    means the whitespace that split this token was itself quoted or escaped —
    the token is a FRAGMENT of a word fused across the split, the shape that
    let ``--push-option='ci skip'`` erase the floor tag. A complete word with
    escaped quotes therefore keeps its precise reading, both directions.

    A thin view over :func:`_shell_quote_walk`, which owns the state machine.
    ONE walk serves every signal (a review subtraction: the identical state
    machine briefly shipped twice); both consequences here are protective-only
    — a hit poisons the positional split, never widens an allow.
    """
    walk = _shell_quote_walk(token)
    return (
        walk.pieces if walk.saw_operator else None,
        walk.end_state != 0 or walk.trailing_escape,
    )


#: A token that BEGINS with a redirection: optional fd number, ``&``, or bash
#: NAMED descriptor ``{name}`` prefix, then ``<`` or ``>`` (doubled, or ``>|``
#: clobber, or ``>&``/``<&`` fd-dup). ``{name}>...`` is ALL redirection — read
#: as a word, the ``{name}`` became a phantom refspec and erased every tag
#: (GPT 5.6 round 11 on #7808). ``<<-`` (the tab-stripping heredoc) folds its
#: ``-`` INTO the operator — left in the remainder it faked a self-contained
#: token and the separated delimiter word became a phantom refspec (round 7)
#: — while a ``-`` after an fd-dup (``>&-`` close, ``2>&1-`` move) is a
#: disposition the remainder correctly keeps. group(3) is whatever follows
#: the operator run — an ATTACHED target/fd makes the token self-contained;
#: an empty remainder means the shell takes the NEXT word as the
#: target/delimiter.
_PUSH_REDIRECTION_RE = re.compile(r"^([0-9]*|&|\{[A-Za-z_][A-Za-z0-9_]*\})(<<-|[<>][<>&|]*)(.*)$")


def _push_token_redirection(token: str) -> "tuple[bool, bool]":
    """(is_redirection, consumes_next_word) for a RAW token.

    Quotes and escapes are refused only where they could FOOL the grammar —
    the prefix/operator span. The redirection operator grammar itself admits
    no quote characters, so a quote can only ever sit in the TARGET group:
    ``>'log'`` is a plain redirection with a quoted target, and refusing the
    whole token for it pushed the shape into the fallback with the WRONG
    catalog identity (GPT 5.6 round 11 on #7808). A token that is a fragment
    (open quote state / trailing escape) is still refused — the caller's walk
    poisons the split for those. The shell consumes a redirection before the
    program runs, so such a token is never an argv word — treating it as a
    positional is how ``git push origin </dev/null`` erased the single-arg
    tag (round 4, verified real and pre-existing on main).
    """
    m = _PUSH_REDIRECTION_RE.match(token)
    if m is None:
        return (False, False)
    if _push_token_shell_read(token)[1]:
        return (False, False)  # fragment: the walk handles it protectively
    return (True, m.group(3) == "")


def _push_option_matches(token: str, names: "frozenset[str]") -> bool:
    """True when ``token`` is ``--`` plus a PREFIX of any option in ``names``.

    Git resolves an unambiguous long-option prefix to that option, so ``--mirr``
    is ``--mirror`` and ``--rep=origin`` is ``--repo=origin``. Matching flag
    literals exactly therefore missed every abbreviation, and the consequence is
    not "an unrecognised flag" but a MIS-CLASSIFICATION: an unmatched flag is
    skipped, the positional read shifts, and the push is attributed to a different
    (individually disableable) rule than the one that covers it.

    Testing the prefix against only the options we care about is EQUIVALENT to
    resolving against git's full option list and then intersecting, because a
    non-dangerous option can only add a candidate, never remove a dangerous one.
    So there is no need to carry git's whole option table here — verified over
    every prefix of every ``git push`` long option, and pinned by
    ``test_the_prefix_test_matches_a_full_option_table``.

    An ambiguous abbreviation therefore reads as dangerous (``--a`` matches
    ``all``), which is free: git refuses an ambiguous abbreviation itself, so the
    command never runs, and denying it cannot lose a push that would have
    succeeded. A fully-spelled unrelated flag is unaffected — ``--atomic`` is not a
    prefix of any dangerous option.
    """
    if not token.startswith("--"):
        return False
    name = token[2:].split("=", 1)[0]
    return bool(name) and any(opt.startswith(name) for opt in names)


# Symbolic refs that resolve at runtime — cannot statically verify safety.
# If the agent is on main and pushes HEAD, it pushes to main on the remote.
_AMBIGUOUS_REFS = {"head", "@", "fetch_head"}

# Refspec spellings that resolve only at runtime: ``@{upstream}`` / ``@{u}``
# git-revision syntax. (The ``$``/backtick branches this once carried are now
# subsumed upstream — the per-token ``$`` check and the segment-level
# expansion ungate both run before any refspec reaches this — so they were
# removed as shadowed duplicates per First Principles review on #7808.)
_AMBIGUOUS_REFSPEC_RE = re.compile(r"@\{")

# TRUE shell command separators (NOT command-substitution boundaries). Used to
# scan the PRE-SPLIT text for substitution glued into a push target — see
# ``_is_push_to_protected_branch``. Applied through
# ``_split_push_command_segments``, which honours quoting; the pattern itself is
# retained as the separator vocabulary.
_CMD_SEPARATOR_RE = re.compile(r"&&|\|\||[;|\n]")

#: The same separators as spellings, longest first so ``&&``/``||`` win over a
#: single ``|``. A LONE ``&`` is deliberately absent: it is not a segment
#: separator here, and the argument scan reads it as operator glue that poisons
#: the positional split.
_SHELL_SEGMENT_SEPARATORS = ("&&", "||", ";", "|")


def _split_push_command_segments(text: str) -> list[str]:
    """Split *text* into the shell's TRUE command segments, honouring quoting.

    A separator only separates where the SHELL reads one. Inside quotes, or
    escaped with a backslash, ``;`` / ``|`` / ``&&`` / ``||`` are ordinary
    characters in the word — ``git push origin 'feature|x'`` publishes a branch
    literally named ``feature|x`` — and splitting there truncated the word
    mid-quote. The fragment then arrived with the shell state still open, which
    ``_push_segment_targets_protected`` reads as a word fused across the
    boundary, so an ordinary unprotected refname was denied by the protective
    fallback (and by its ungated sentinel, which no catalog row can switch off).

    A NEWLINE always separates, escaped or not, and the backslash is KEPT in the
    segment it ends. That is not an inconsistency: a backslash-newline VANISHES
    in bash, fusing the words on either side into one that neither segment can
    reconstruct (``origin ma\\`` + newline + ``in`` publishes MAIN), and the
    retained trailing escape is exactly the signal the cumulative-open-state
    check ungates on. Every other escaped separator survives as a LITERAL
    character in the word, so the fused word necessarily contains it and can
    never equal a protected branch name.

    Distinct from :func:`_split_shell_segments`, which serves the ``cd``-tracking
    pass: that one wants FEWER segments (a wrong split corrupts the tracked
    directory), leaves pipes joined because they do not move the directory, and
    emits an unquoted paren as its own segment. This one needs a pipe to separate
    — each side is a command whose push must be judged on its own — and needs the
    parens left inside the word, because the argument scan reads them as the
    operator glue they are. They are not interchangeable.

    Quote state comes from :func:`_iter_shell_chars`, the module's one shell
    state machine, so this cannot disagree with the word split or the boundary
    walk about where a quote ends.
    """
    segments: list[str] = []
    rest = text
    while True:
        buf: list[str] = []
        resume_at: int | None = None
        for step in _iter_shell_chars(rest):
            if step.trailing_escape:
                buf.append(step.text)
                break
            if step.char == "\n":
                # A NEWLINE always separates. When it was ESCAPED the backslash
                # is kept, because that is the splice signal: bash makes the
                # backslash-newline vanish, fusing the words on either side into
                # one that neither segment can reconstruct.
                if step.text.startswith("\\"):
                    buf.append("\\")
                resume_at = step.offset + len(step.text)
                break
            if step.active:
                separator = next(
                    (s for s in _SHELL_SEGMENT_SEPARATORS if rest.startswith(s, step.offset)),
                    None,
                )
                if separator is not None:
                    resume_at = step.offset + len(separator)
                    break
            buf.append(step.text)
        segments.append("".join(buf))
        if resume_at is None:
            return segments
        rest = rest[resume_at:]


# Shell expansions that fuse text INTO a word, so the literal command hides the
# real push target. Any of these inside a git-publish command is unverifiable
# -> deny (fail closed):
#   - command substitution   $(...)   and backticks  `...`
#   - parameter expansion     ${...}
#   - PROCESS substitution   <(...) / >(...)  is NOT here, deliberately: the
#     shell substitutes a /dev/fd path WORD, so it is unverifiable only where it
#     SURVIVES as an argv word. Matching it on the whole segment also denied the
#     shape where the shell REMOVES it — ``git push origin my-feature >
#     >(tee log.txt)`` is an ordinary feature push whose output is teed — so the
#     word-position reading lives in ``_push_segment_targets_protected``, which
#     sees the tokens that survive redirection removal.
#   - BRACE expansion         {a,b} / {1..5}  -- bash expands ``ma{i,i}n`` to
#     ``main`` and ``{main,x}`` to ``main x`` BEFORE git sees the token, so a
#     brace group containing a comma or ``..`` must be treated as ambiguous.
_AMBIGUOUS_EXPANSION_RE = re.compile(r"\$\(|\$\{|`|\{[^{}]*(?:,|\.\.)[^{}]*\}")

#: Process substitution, which the shell replaces with a ``/dev/fd`` path WORD.
#: Read in a word position it is unverifiable — mis-reading it as a removable
#: redirection shifted a value option's consumption onto the remote and
#: downgraded a protected push to the disableable single-arg row (GPT 5.6 round 8
#: on #7808). The operator adjacency is required, so a parenthesis inside a
#: refname stays data; a QUOTED spelling still matches and over-denies, the same
#: fail-closed posture the expansion regex takes for a quoted ``$(``.
_PROCESS_SUBSTITUTION_OPENERS = ("<(", ">(")


def _dequote_token(token: str) -> str:
    """Collapse shell quoting/escaping to the literal the shell passes to git.

    bash merges adjacent quoted/unquoted fragments into ONE word, so
    ``ma"in"``, ``m''ain`` and ``ma\\in`` all reach git as the literal
    ``main``. ``str.strip`` removes only the OUTERMOST quotes, leaving interior
    quote/backslash characters that make the token compare unequal to a
    protected name — an evasion of this gate. Remove ALL single/double quotes
    and backslash escapes so the comparison sees the shell-resolved word.

    Shell OPERATORS glued to the word are NOT cut here. ``_cut_at_operator`` is
    applied where an operator would hide a PROGRAM name (the ``git`` anchor in
    ``_git_push_args``); for an ARGUMENT, cutting destroyed the very evidence the
    argument scan reads. ``_push_token_shell_read`` splits a token at its
    unquoted operators and ``_push_segment_targets_protected`` scans every piece
    as a refspec candidate, so ``mainline)>log`` still resolves to the protected
    ``mainline`` — while an uncut token keeps the shape the scan classifies by:
    ``@(main)`` is extglob pathname expansion, ``origin>/dev/null`` is a
    remote-only push, and a lone ``&`` is a command boundary. Cutting first
    reduced all three to bare words and erased their tags.
    """
    return token.replace("'", "").replace('"', "").replace("\\", "")


def _split_shell_words(segment: str) -> list[str]:
    """Split *segment* into words at UNQUOTED, unescaped whitespace.

    ``str.split`` splits inside quotes too, which tears one shell word into
    fragments the scan then reads as separate arguments. Two consequences, both
    seen in practice: a wrapper's quoted payload (``bash -c '(cd /tmp && git push
    origin my-feature)'``) yielded a bare ``git`` token, so the OUTER line — which
    is not itself a push — was parsed as one and its fragmented ref denied an
    ordinary feature push; and a legitimately quoted refname arrived with the
    shell state open, which the fragment rule reads as a word fused across the
    split. Splitting the way the shell does removes the class: a quoted payload is
    ONE word, so it is left to the nested-payload reading that judges it properly.

    Quotes and escapes are PRESERVED in the words; the callers strip them
    (``_dequote_token``) and walk them (``_push_token_shell_read``) themselves.

    Quote state comes from :func:`_iter_shell_chars`, so ANSI-C ``$'...'`` reads
    the same here as everywhere else. A private copy of the state machine WITHOUT
    that awareness is what let ``git push origin feature > >(echo $'a\\'b') main``
    publish a protected branch: the escaped quote closed its state, the next quote
    reopened it, and the trailing ``main`` fused into one unterminated word --
    which the boundary walk, reading the same text correctly, could no longer
    rescue because the refspec was already inside the word it was handed.
    """
    words: list[str] = []
    buf: list[str] = []
    for step in _iter_shell_chars(segment):
        if step.trailing_escape:
            buf.append(step.text)  # trailing escape: the fragment signal
            break
        if step.active and step.char.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
            continue
        buf.append(step.text)
    if buf:
        words.append("".join(buf))
    return words


def _git_push_args(segment: str) -> list[str] | None:
    """Return the tokens AFTER the ``push`` subcommand if *segment* is a git push.

    Pure-Python (no regex backtracking — CodeQL ReDoS-safe) replacement for a
    ``\\bpush\\b`` scan. It anchors ``push`` as the git subcommand — the first
    non-flag token after ``git`` — so a segment that merely contains the word
    "push" (e.g. ``echo remember-to-push``) is NOT treated as a push and
    returns None. Skips leading flags, and a single non-flag value that a flag
    may take (e.g. ``-C <path>``) — but never swallows ``push`` itself.
    """
    # Strip glued shell operators for the same reason as ``_dequote_token``:
    # ``(git`` IS the git program to bash, and ``main)&`` IS the ref ``main``.
    raw_tokens = _split_shell_words(segment)
    tokens = [_cut_at_operator(t) for t in raw_tokens]
    # Anchoring compares against a DEQUOTED view, because a quoted ``"git"`` is
    # still the git program to bash. Matching the raw token missed it and
    # anchored on a LATER unquoted ``git push`` instead, returning only that
    # push's arguments -- so appending a benign second push hid the first one's
    # protected ref entirely and turned a fail-closed segment into an allow.
    #
    # The view is separate on purpose: the RETURNED tokens keep their quoting,
    # because callers dequote them once more, and dequoting twice would read a
    # literal ``'(main)'`` ref as the operators ``(``/``)`` around ``main`` and
    # deny a branch that is legitimately pushable.
    anchors = [_dequote_token(t) for t in tokens]

    # Resolution mirrors the publish floor's ``_resolves_to``: a token IS git
    # when either its raw or its operator-cut spelling equals the word or has it
    # as a basename. An exact ``== "git"`` test skipped a path-qualified
    # ``/usr/bin/git`` and anchored on a NESTED ``>(git push origin
    # my-feature)`` instead, so the feature branch that process substitution
    # pushes vouched for the protected push in front of it. Selecting the FIRST
    # resolving anchor can only move the anchor earlier than the exact test did,
    # which is the fail-closed direction: the push that must be judged is the
    # leading one.
    def _anchor_is_git(index: int) -> bool:
        # The untouched spelling is consulted as well: both ``_cut_at_operator``
        # and ``_dequote_token`` truncate at an operator that lives INSIDE a path
        # component (``/opt/my(dir)/git`` -> ``/opt/my``), which is exactly the
        # narrowing already fixed in the publish floor. Quotes are stripped
        # without cutting so a quoted absolute path still resolves.
        raw = raw_tokens[index]
        for candidate in (anchors[index], raw, raw.strip("'\"")):
            if candidate == "git" or os.path.basename(candidate) == "git":
                return True
        return False

    start = next((k for k in range(len(anchors)) if _anchor_is_git(k)), None)
    if start is None:
        return None
    i = start + 1
    while i < len(anchors) and anchors[i].startswith("-"):
        i += 1  # skip the flag
        # A flag may take one separate non-flag value (e.g. ``-C <path>``);
        # never consume the ``push`` subcommand as a flag value.
        if i < len(anchors) and not anchors[i].startswith("-") and anchors[i] != "push":
            i += 1
    if i < len(anchors) and anchors[i] == "push":
        # A redirection is SKIPPED, not treated as the end of the argument list.
        #
        # Its words are not refspecs -- stripping glued operators made
        # ``>(git push origin my-feature)`` read as ordinary refspecs, so a bare
        # ``git push``, which must fail closed, inherited a branch it never named
        # -- but the words AFTER it are. Truncating there dropped them, and bash
        # keeps them: ``git push origin feature 2>/dev/null main`` really runs
        # ``git push origin feature main``, so a trailing protected ref left the
        # gate while still reaching the server.
        #
        # Boundaries are read off the RAW spelling, because that is where the
        # redirection character still exists. A file target is one word, glued
        # (``2>/dev/null``) or spaced (``> out``); a PROCESS SUBSTITUTION target
        # is a whole command line, so it is skipped to its matching ``)`` rather
        # than by one word.
        #
        # A token that OPENS with ``<(`` / ``>(`` is process substitution, which
        # bash reads as a WORD, not a redirection -- so it is returned rather than
        # skipped. Skipping it consumed an option's value (``-o <(echo)``) and
        # shifted the positional split onto the remote, downgrading a push of a
        # protected branch to the disableable single-arg row.
        #
        # Redirection arity comes from ``_push_token_redirection``, the model the
        # argument scan itself uses, rather than a second reading of the same
        # grammar here. The local reading treated the ``-`` of ``<<-`` as an
        # ATTACHED target, so the tab-stripping heredoc's separated delimiter word
        # survived as a phantom refspec and erased the tag -- the shape #7808 had
        # already closed one layer down. One model, one place.
        #
        # The tokens are returned in their RAW spelling. The caller's scan is
        # defined over raw words -- it splits each one at its own unquoted
        # operators and models redirection arity itself -- so handing it
        # operator-CUT words erased the shapes it classifies by (``origin>`` read
        # as a plain remote, ``@(main)`` as the ambiguous ref ``@``, a lone ``&``
        # as an empty token).
        args: list[str] = []
        raw_args = raw_tokens[i + 1 :]
        k = 0
        while k < len(raw_args):
            if raw_args[k].startswith(_PROCESS_SUBSTITUTION_OPENERS):
                args.append(raw_args[k])
                k += 1
                continue
            is_redirection, consumes_next = _push_token_redirection(raw_args[k])
            if not is_redirection:
                args.append(raw_args[k])
                k += 1
                continue
            if not consumes_next and "(" in raw_args[k]:
                # A redirection whose ATTACHED target opens a paren
                # (``2>(cat ... )``): not the bare process-substitution word
                # (that is caught above) and not a file target -- the parens
                # span later words, and skipping this one as a self-contained
                # redirection left the body's remainder (``>/dev/null # fake
                # )``) to be read as argv, where the ``#`` truncated the real
                # refspecs (GPT 5.6 review on #8719). Whatever bash makes of
                # the spelling, this gate cannot read it: fail CLOSED.
                return None
            k += 1
            if not consumes_next or k >= len(raw_args):
                continue
            following = _REDIRECT_START_RE.match(raw_args[k])
            target = (raw_args[k][following.end() :] if following else raw_args[k]) or raw_args[k]
            if not target.startswith("("):
                k += 1  # ordinary file target -- one word
                continue
            # PROCESS-SUBSTITUTION BOUNDARY, walked QUOTE-AWARELY and proven.
            #
            # The target is a whole command line, so it ends at its matching
            # unquoted ``)``. Counting the parens per word with str.count was
            # quote-UNAWARE, and that was a live bypass: in
            # ``git push origin feature > >(echo '(' ) main`` the QUOTED ``(``
            # inflated the depth to 2, the real ``)`` only returned it to 1, and
            # the trailing ``main`` was swallowed into the substitution -- so a
            # protected-branch push came back with the remaining words alone and
            # was allowed. ``> >(printf "(") main`` is the same shape with double
            # quotes. Both traced independently by the GPT 5.6 and Opus lanes on
            # #8712. The shared state machine ignores quoted parens, so the
            # boundary lands where bash puts it.
            #
            # FAIL CLOSED when the boundary cannot be PROVEN complete -- the
            # words ran out with the substitution still open (``>(echo main``),
            # or a quote is still open at the end. Silently swallowing the rest
            # of the segment is precisely how an unterminated construct hid a
            # refspec. Returning None routes the segment to the caller's
            # unparseable branch, which emits the non-opt-out-able ambiguity
            # sentinel: the same posture the whole-segment expansion regex gave
            # process substitution before it moved to this walk. A PROVEN
            # boundary is a word; an UNPROVABLE one is ambiguous.
            #
            # PROVEN is also refused for a body word the quote walk cannot READ
            # (``_process_substitution_word_is_opaque``): the paren count models
            # quoting, and nothing else -- so a construct outside quoting moves
            # the real closer or hides the program without the count noticing. A
            # word-initial ``#`` comments out the ``)`` after it (``>(cat
            # >/dev/null # fake )`` + newline + ``) main`` pushed main); a
            # reserved word makes the body a compound command whose ``)`` is
            # SYNTAX (``>(case x in x) git push;; esac)`` ran the bare push); an
            # unquoted glob or expansion in a body word means the program the
            # payload walk judges the skipped body by resolves only at run time
            # (``>(/usr/bin/g?t push origin main)`` closed cleanly and the walk
            # saw no ``git``). Each was one GPT 5.6 round on #8719, and modelling
            # them one at a time is unbounded, so the rule is the class: a body
            # word the walk cannot read is not a redirection the gate may skip.
            depth = 0
            state = 0
            ansi = False
            proven = False
            opener = k
            while k < len(raw_args):
                if _process_substitution_word_is_opaque(
                    raw_args[k], state, ansi, first=k == opener
                ):
                    return None
                walk = _shell_quote_walk(raw_args[k], state=state, ansi=ansi)
                depth += walk.paren_delta
                state, ansi = walk.end_state, walk.end_ansi
                k += 1
                if state == 0 and depth <= 0:
                    proven = True
                    break
            if not proven:
                return None
        return args
    return None


#: bash reserved words. Any of them as an UNQUOTED word inside a
#: process-substitution body means the body is a compound command whose ``)``
#: may be SYNTAX (``case x in x)``) rather than the closer.
_SHELL_RESERVED_WORDS = frozenset(
    {
        "!",
        "[[",
        "]]",
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "select",
        "then",
        "time",
        "until",
        "while",
        "{",
        "}",
    }
)

#: The characters an UNQUOTED process-substitution body word may consist of and
#: still be one the redirect skip may step over: the alphabet of an ordinary
#: program invocation -- letters, digits, and ``/ - _ . = :`` (paths, flags,
#: ``KEY=value``, ``host:port``). Everything else is refused as OPAQUE. This is
#: an ALLOWLIST on purpose: the earlier denylist (``*?[``, then extglob ``(``,
#: then ``#``, then ``& ; |``) grew by one shell metacharacter per review round
#: on #8719, and an enumeration of what bash can do with a character is never
#: finished. Quoted text is not judged here at all -- #8712's quote walk owns
#: it -- and ``$'`` (the ANSI-C quote that walk models) is the one active ``$``
#: admitted, so ``> >(echo '(' ) main`` and ``> >(echo $'a\'b') main`` keep
#: their precise reading while a glob, an extglob or nested paren, a comment, a
#: control operator, a tilde, a history ``!``, a brace or an expansion in a
#: body word all fail closed: with such a word the shell either moves the real
#: closer or resolves the program only at run time, and either way the payload
#: walk that judges the skipped body cannot see what bash runs.
_PROCESS_SUBSTITUTION_SAFE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-_.=:"
)


def _process_substitution_word_is_opaque(word: str, state: int, ansi: bool, *, first: bool) -> bool:
    """True when *word*, read from quote state ``(state, ansi)`` inside a
    process-substitution body, is NOT plainly readable: an unquoted reserved
    word, or any unquoted character outside
    :data:`_PROCESS_SUBSTITUTION_SAFE_CHARS` other than the ``$`` of an ANSI-C
    ``$'...'`` and a ``)`` that ends the word (the construct's closer, which
    the caller's depth walk accounts for). *first* marks the opener word, whose
    text up to and including the ``(`` is the ``>(`` / ``2>(`` opener rather
    than body.
    """
    body = word[word.index("(") + 1 :] if first else word
    if state == 0 and body.rstrip(")") in _SHELL_RESERVED_WORDS:
        return True
    steps = list(_iter_shell_chars(body, state, ansi))
    for index, step in enumerate(steps):
        if not step.active and step.state == 0 and step.text.startswith("\\"):
            # An UNQUOTED escape pair: ``\g\i\t`` reaches the program as
            # ``git`` while no scanner word spells it (GPT 5.6 review on
            # #8719). Inside quotes the walk owns the backslash; outside them
            # it is a spelling the allowlist must not see through.
            return True
        if not step.active and step.state == 2 and step.text == step.char and step.char in "$`":
            # Double quotes do NOT suspend expansion: ``"$GIT" push origin
            # main`` runs whatever ``$GIT`` names (GPT 5.6 review on #8719).
            # An unescaped ``$`` or backtick inside double quotes is an
            # expansion the scan cannot resolve, so the word is opaque; an
            # escaped ``\$`` (text ``\$``) stays data.
            return True
        if not step.active or step.char in _PROCESS_SUBSTITUTION_SAFE_CHARS:
            continue
        if step.char in "'\"":
            continue  # a quote DELIMITER: the quote walk owns what it encloses
        if step.char == ")" and index == len(steps) - 1:
            continue  # the closer, accounted for by the caller's depth walk
        if step.char == "$":
            following = steps[index + 1] if index + 1 < len(steps) else None
            if following is not None and following.char == "'" and following.ansi:
                continue  # ``$'...'`` -- the ANSI-C quote, modelled by the walk
        return True
    return False


def _normalize_ref(ref: str) -> str:
    """Reduce a push destination ref to the bare branch name git resolves it to.

    Git accepts several destination-side spellings that all resolve to the same
    branch server-side: ``main``, ``heads/main``, ``refs/heads/main``,
    ``remotes/<remote>/main``, ``refs/remotes/<remote>/main``. Stripping only
    ``refs/heads/`` let ``heads/main`` and the ``remotes/`` forms dodge the
    protected-name check (they still resolve to a protected branch on the
    server). Normalize every spelling to the bare name so the comparison cannot
    be evaded by ref-path spelling.
    """
    ref = ref.removeprefix("refs/")
    if ref.startswith("remotes/"):
        parts = ref.split("/", 2)  # remotes/<remote>/<branch>
        if len(parts) == 3:
            return parts[2]
    return ref.removeprefix("heads/")


def _push_segment_targets_protected(arg_tokens: list[str]) -> frozenset[str]:
    """Return the git-publish rule tags a single push's argument tokens trip.

    *arg_tokens* are the tokens following the ``push`` subcommand within ONE
    shell segment (separators already removed).  An EMPTY result means this
    segment is an explicit feature-branch push and is allowed.

    Each returned tag is either a ``git-publish`` catalog rule id (the caller
    denies only while that rule is still enabled, so an operator opt-out is
    honoured) or :data:`_GIT_PUBLISH_UNGATED` for the anti-obfuscation branches,
    which are NOT opt-out-able: they are what makes the gated tags
    non-bypassable, since a refspec the shell fuses together cannot be checked
    against a branch name at all.

    ALL refspecs are collected rather than short-circuiting on the first hit: a
    refspec that trips a DISABLED rule must not allow the push when a sibling
    refspec trips an enabled one.

    A bare push (no explicit branch) is reported because the current branch
    might be a protected one.  Force flags (``--force``/``-f``/
    ``--force-with-lease``) do NOT by themselves make a feature-branch push
    protected — force-push to a feature branch is a normal PR/rebase workflow —
    but a force-push to a protected branch is still reported, because the target
    check below fires regardless of any flags (force flags are stripped first).
    """
    tags: set[str] = set()
    tokens = [_dequote_token(t) for t in arg_tokens]
    # Flags that push ALL local branches (protected ones included) bypass any
    # per-branch target check.  Detected BEFORE stripping flags, and resolved the
    # way GIT resolves them, so an abbreviation (``--mirr``) counts.
    if any(_push_option_matches(tok, _PUSH_ALL_BRANCHES_OPTS) for tok in tokens):
        tags.add("git-publish-push-mirror-all")
    # Skip flags (tokens starting with -); non_flags[0] is the remote and
    # non_flags[1:] are the refspecs/branches. Option ARITY is modelled
    # explicitly (#7796): a flag that CARRIES the repository (``--repo=x`` /
    # ``--repo x``) means the remote is NOT positional, a value-taking option's
    # SEPARATED value is consumed so it is never read as a remote/refspec, and
    # any option the scan does not recognise poisons the positional split
    # entirely (see the fail-protective fallback below) — because trusting a
    # split that may contain a leaked option value is how the floor tag was
    # erased. A bare ``--`` ends option parsing, exactly as git reads it.
    repo_in_flag = False
    positional_only = False
    non_flags: list[str] = []
    skip_next = False
    # One shared quote/escape walk per raw token yields both shell signals:
    # operator PIECES (unquoted < > & split the word) and OPEN STATE (an
    # unterminated quote or trailing escape means the shell fused a
    # whitespace-spanning word this whitespace tokenizer split apart). Either
    # signal means no per-token reading of the split can be trusted.
    shell_reads = [_push_token_shell_read(t) for t in arg_tokens]
    # ``#`` at the start of a WORD comments out the REST of the segment, so
    # the shell never passes those tokens to git: truncate before any other
    # reading, or ``git push origin #main`` scans a phantom refspec while the
    # shell runs a remote-only push. A ``#`` is word-initial only when the
    # whitespace before it was a REAL separator: if ANY earlier token leaves
    # the shell state open (trailing escape / unterminated quote fuses across
    # the split), the ``#`` may be mid-word — truncating there discarded a
    # real trailing refspec (GPT 5.6 round 5 on #7808, verified: an
    # escaped-space option value fused into ``#x`` dropped ``main`` from the
    # scan, leaving only the disableable bare tag). With an open token seen,
    # truncation is skipped entirely: the open state already poisons the
    # split protectively and the superset scan keeps every later positional
    # visible.
    _open_seen = False
    for _idx, _raw in enumerate(arg_tokens):
        if _raw.startswith("#") and not _open_seen:
            arg_tokens = arg_tokens[:_idx]
            tokens = tokens[:_idx]
            shell_reads = shell_reads[:_idx]
            break
        _open_seen = _open_seen or shell_reads[_idx][1]
    unrecognised_option = any(open_state for _pieces, open_state in shell_reads)
    # A segment whose CUMULATIVE quote/escape state is still open at its end
    # continues into the NEXT line: bash line continuation (backslash-newline
    # vanishes entirely) and quoted newlines splice words ACROSS the segment
    # split, so the real refspec may be assembled from pieces this segment
    # cannot see — ``origin ma\`` + newline + ``in`` pushes MAIN while no
    # token here spells it (GPT 5.6 round 6 on #7808, verified real). An
    # unreconstructable name gets the same posture as ``ma$in``: the ungated
    # sentinel, which no catalog row can switch off. Deliberately NARROWER
    # than ungating on any per-token open state: a MID-segment open (a quoted
    # value containing a space, whose quote closes before segment end) stays
    # on the DISABLEABLE fallback, because joining within one segment can
    # only fuse whitespace into the word — never a valid refname — and every
    # piece stays visible to the superset scan below. The cumulative state is
    # the per-token walk run over the joined segment (whitespace is inert to
    # the state machine).
    if arg_tokens and _push_token_shell_read(" ".join(arg_tokens))[1]:
        tags.add(_GIT_PUBLISH_UNGATED)

    def _classify_word(word: str) -> None:
        """Read ONE argv word exactly as git's option parser would.

        The single place a word becomes either an option (with its arity) or a
        positional. Stripping shell punctuation off a word changes WHERE the
        word came from, never WHAT it is, so every branch that recovers a word
        from operator glue routes it through here instead of appending it to
        ``non_flags`` directly. Appending unconditionally is how ``(git push
        --repo=origin -f)`` erased the floor: the ``)`` was stripped, ``-f`` was
        filed as a refspec, it matched no protected name, and the segment came
        back with NO tags at all — a force push to a possibly-protected current
        branch, admitted by adding one parenthesis (GPT 5.6 security finding on
        #8712). The same spelling without parens is correctly bare.
        """
        nonlocal skip_next, positional_only, repo_in_flag, unrecognised_option
        if skip_next:
            # The separated value of a value-taking option: consumed, so it is
            # never read as a remote or a refspec.
            skip_next = False
            return
        if not word:
            return
        if positional_only or word == "-" or not word.startswith("-"):
            # A lone ``-`` is an OPERAND to git's option parser (a repository
            # spelled ``./-`` is addressable) — skipping it as a flag shifted
            # the real refspec into the remote slot and downgraded the row
            # (GPT 5.6 round 13 on #7808).
            non_flags.append(word)
            return
        if word == "--":
            positional_only = True
            return
        if _push_option_matches(word, _PUSH_REPO_OPTS):
            repo_in_flag = True
            skip_next = "=" not in word
            return
        if "=" in word:
            # An attached value binds inside the token — whatever the option is,
            # it cannot disturb the positional split.
            return
        if word.startswith("--"):
            if _push_option_matches(word, _PUSH_VALUE_OPTS):
                skip_next = True
            elif not (
                word.startswith("--no-")
                or _push_option_matches(word, _PUSH_NO_VALUE_OPTS)
                or _push_option_matches(word, _PUSH_ALL_BRANCHES_OPTS)
            ):
                unrecognised_option = True
            return
        # Short-option token: resolve the bundle char by char like git does.
        for i, ch in enumerate(word[1:]):
            if ch in _PUSH_VALUE_SHORTS:
                # Rest of the token is the attached value; consume the NEXT
                # token only when there is no rest.
                skip_next = i == len(word) - 2
                break
            if ch not in _PUSH_NO_VALUE_SHORTS:
                unrecognised_option = True
                break

    pending_redirection_target = False
    for raw, tok, (operator_pieces, _open) in zip(arg_tokens, tokens, shell_reads):
        if tok:
            # Word-producing shell syntax makes ANY token unverifiable, no
            # matter which slot the split assigns it (GPT 5.6 round 3 on
            # #7808, verified real): ``V='ci.skip main'; git push
            # --repo=origin --push-option $V`` expands and word-splits AFTER
            # this scan, handing git a ``main`` refspec the split never saw —
            # and consuming the literal ``$V`` had REGRESSED that case from
            # the ungated posture (the leaked value used to hit the refspec
            # ambiguity check) to the disableable bare rule. A ``$`` anywhere
            # therefore lands on the ungated branch, the same posture as
            # ``ma$in``; ``$(``/``${``/backticks never reach here because the
            # caller's expansion regex already ungated the whole segment.
            # Glob characters (``* ? [``) are pathname expansion — a file
            # named ``main`` makes ``ma[i]n`` push main — and none of them is
            # legal in a refname, so they keep the wildcard-refspec identity
            # the leaked-value scan used to give them, at zero cost to real
            # commands.
            if "$" in tok or tok.startswith("~"):
                # Tilde expansion is env-driven text, not path syntax: bare
                # ``~`` IS ``$HOME`` (``HOME=main`` publishes main), ``~±``
                # and ``~N`` read PWD/OLDPWD/DIRSTACK, and even ``~/main``
                # resolves to ``refs/heads/main`` under a crafted
                # ``HOME=refs/heads`` — so a leading unquoted ``~`` is as
                # unverifiable as ``$`` (GPT 5.6 round 15 on #7808, verified
                # real). Mid-word ``~`` is literal in an argv word and stays
                # data.
                tags.add(_GIT_PUBLISH_UNGATED)
            # Extglob patterns (``@( +( !(`` — and ``?( *(``, already covered
            # by their leading glob char) are pathname expansion too when the
            # shell has extglob on, so they take the same wildcard identity:
            # like a glob, they can only ever match existing FILE names
            # (GPT 5.6 round 9 on #7808, verified: ``@(main)`` beside a file
            # named ``main`` expands to a push of main with no tag at all).
            if any(ch in tok for ch in "*?[") or any(op in tok for op in ("@(", "+(", "!(")):
                tags.add("git-publish-push-wildcard-refspec")
            # Process substitution that SURVIVED redirection removal is an argv
            # word the shell replaces with a ``/dev/fd`` path, so the split
            # cannot model it — the ungated posture, like ``ma$in``. Tested here
            # rather than on the whole segment because a process substitution the
            # shell REMOVES (the target of ``> >(tee log.txt)``) never reaches
            # git's argv and must keep the precise reading.
            if any(op in tok for op in _PROCESS_SUBSTITUTION_OPENERS):
                tags.add(_GIT_PUBLISH_UNGATED)
        # Shell operators are consumed by the SHELL, so they are handled
        # before every argv-level reading — including after ``--``, which is
        # git's end-of-options, not the shell's (GPT 5.6 round 4 on #7808).
        if pending_redirection_target:
            # The word a bare redirection operator takes as its target; the
            # shell removes it from argv.
            pending_redirection_target = False
            continue
        is_redirection, consumes_next = _push_token_redirection(raw)
        if is_redirection:
            # Modelled with the shell's own arity so ``2>&1`` keeps a feature
            # push allowed while ``origin </dev/null`` reads as the precise
            # remote-only shape instead of scanning a phantom refspec.
            pending_redirection_target = consumes_next
            continue
        if operator_pieces is not None:
            # A word GLUED to its redirection: bash reads ``origin>/dev/null``
            # as the word ``origin`` plus a redirection, i.e. a remote-only
            # push whose true row is SINGLE-ARG — the protective fallback
            # emitted BARE for it, and a wrong identity is itself a hazard
            # under per-rule opt-out (GPT 5.6 round 10 on #7808, verified
            # real). When the token decomposes cleanly — a non-flag word,
            # then a well-formed redirection (no risky ``&`` beyond an
            # fd-dup) — keep the word positional and consume the redirection
            # exactly as the shell does, with no fallback. Anything murkier
            # (a bare ``&`` command boundary, a flag-shaped prefix, quotes
            # inside the redirection) keeps the protective fallback below.
            prefix = operator_pieces[0] if operator_pieces else ""
            rest = raw[len(prefix) :] if prefix and raw.startswith(prefix) else ""
            dequoted_prefix = _dequote_token(prefix)
            # A flag GLUED to a redirection: the flag identity must not be
            # lost to the fallback — ``--all>/dev/null`` is an all-branches
            # push, and emitting only the disableable no-refspec rows let an
            # operator who disabled those admit it while mirror-all stayed
            # enabled (GPT 5.6 round 16 on #7808, verified real). The
            # all-branches check is the one whose MISSED identity is a
            # bypass; other flag prefixes stay on the fallback, which only
            # ever over-protects.
            if _push_option_matches(dequoted_prefix, _PUSH_ALL_BRANCHES_OPTS):
                tags.add("git-publish-push-mirror-all")
            if (
                (rest[:1] in ("<", ">") or rest.startswith(("&>", "&>>")))
                and dequoted_prefix
                and not dequoted_prefix.startswith("-")
                and _push_token_redirection(rest)[0]
                and (
                    "&" not in rest
                    # Glued all-output redirection: the & is the operator head.
                    or rest.startswith("&")
                    # Glued fd-dup / fd-close / fd-move: >&2, >&-, >&1-.
                    or re.fullmatch(r"[<>]{1,2}&([0-9]+-?|-)", rest)
                )
            ):
                # The glued WORD is exactly what the shell hands git as the
                # argv word, so it must flow wherever a plain word would: a
                # pending option value first (GPT 5.6 round 13 — appending it
                # as a positional while ``skip_next`` stayed armed let the
                # NEXT real word be eaten as the "value" and erased the
                # tags), else through the ordinary option-vs-positional
                # reading. The guard above already keeps a flag-shaped prefix
                # off this branch; classifying rather than appending means a
                # future loosening of that guard cannot turn a flag into a
                # refspec behind the gate's back.
                _classify_word(dequoted_prefix)
                pending_redirection_target = _push_token_redirection(rest)[1]
                continue
            # A word carrying only SUBSHELL PUNCTUATION: ``(cd /tmp; git push
            # origin my-feature)`` hands the ref token ``my-feature)``, whose
            # ``)`` merely closes the subshell. It removes nothing from argv and
            # adds nothing to the word, so the word keeps its exact identity and
            # the split stays trusted — routing it to the fallback below denied
            # every legitimate refname pushed inside a subshell. The word itself
            # is still read as a refspec candidate, so a protected name inside the
            # parens is caught exactly as it is without them. A leading paren
            # leaves no prefix and keeps the protective fallback.
            #
            # Read through the SAME classifier a bare word takes: stripping the
            # paren must not change what the word IS. Appending it as a
            # positional turned ``(git push --repo=origin -f)`` into a push of a
            # refspec named ``-f`` — no tags at all, so a force push to a
            # possibly-protected current branch was admitted by one parenthesis,
            # while ``(git push -f)`` reported the wrong row (remote-only instead
            # of bare).
            if rest and dequoted_prefix and all(ch in "()" for ch in rest):
                _classify_word(dequoted_prefix)
                continue
            # A bare control operator (``&`` — a single ampersand is NOT a
            # segment separator upstream, only ``&&`` is) or operator glue
            # mid-word (``main>log`` = the word ``main`` plus a redirection:
            # it pushes main). The split is untrusted, and the operator-
            # delimited pieces are scanned as refspec candidates so a
            # protected name cannot hide behind the glue.
            #
            # This branch appends the pieces DIRECTLY, deliberately: it has
            # already set ``unrecognised_option``, so the fallback below reads
            # the whole segment protectively (both no-refspec rows fire) and
            # treats every positional as a refspec candidate. A flag landing in
            # that candidate list can only ADD a tag, never remove one.
            unrecognised_option = True
            non_flags.extend(p for p in (_dequote_token(pc) for pc in operator_pieces) if p)
            continue
        _classify_word(tok)
    if unrecognised_option:
        # Fail-protective invariant (#7796, shape C): an option this scan does
        # not model might take a separated value, so the positional split
        # cannot be trusted — the "remote" it would drop may really be a
        # leaked option value. Read the segment protectively instead: the
        # current branch might be protected (the bare tag — unless an
        # all-branches flag already names the target set exhaustively, the
        # finding-3 suppression, in which case mirror-all covers a superset of
        # bare), and EVERY positional is scanned as a refspec candidate so an
        # actual protected name still reports its own precise catalog row. A
        # mis-parse can therefore only ever OVER-protect: a future value-taking
        # push option cannot silently reopen the erasure class.
        if "git-publish-push-mirror-all" not in tags:
            tags.add("git-publish-push-bare")
            if non_flags:
                # An untrusted split cannot distinguish the bare shape from
                # the remote-only shape — the visible positionals may all be
                # option values, or one may be the remote. Three review
                # rounds (10, 13, 14 on #7808) each turned that ambiguity
                # into a bypass by disabling whichever single row the
                # fallback happened to emit, so the fallback now names BOTH
                # no-refspec rows: admitting an unparseable spelling takes
                # disabling both. (With no positionals at all the remote-only
                # shape is impossible and bare stands alone; an all-branches
                # flag still suppresses both, since mirror-all covers a
                # superset.)
                tags.add("git-publish-push-single-arg")
        refspecs = non_flags
    else:
        # With the repository supplied by a flag there is no positional remote
        # to drop, so the refspecs start at index 0.
        refspecs = non_flags if repo_in_flag else non_flags[1:]
    if not refspecs and "git-publish-push-mirror-all" not in tags:
        # Bare ``push`` or ``push <remote>`` with no explicit branch — the
        # current branch might be protected.  The two spellings are separate
        # catalog rules, so report them separately. ``--repo=x`` with no refspec
        # is the bare form: the flag named the remote, nothing named a branch.
        #
        # Skipped when an all-branches flag is present, because then the absence
        # of a refspec is not the "which branch is this?" shape at all — the flag
        # already names the target set exhaustively. Tagging both meant
        # ``push --all origin`` also carried the single-arg tag, so disabling
        # mirror-all left the command blocked by its sibling and the toggle read
        # as enabled-and-off while enforcement never changed.
        tags.add("git-publish-push-bare" if not non_flags else "git-publish-push-single-arg")
        return frozenset(tags)
    if not refspecs:
        return frozenset(tags)
    for refspec in refspecs:
        # Refspecs with shell expansion ($, `) or git-revision syntax
        # (@{upstream}, @{u}) cannot be statically verified — never opt-out-able.
        if _AMBIGUOUS_REFSPEC_RE.search(refspec):
            tags.add(_GIT_PUBLISH_UNGATED)
            continue
        clean = refspec.lstrip("+")  # strip force-push '+' ref prefix
        # Wildcard refspec (refs/heads/*:refs/heads/*, *:*, feat*) expands to
        # MANY refs — like --mirror/--all it can include a protected branch and
        # cannot be statically verified.
        if "*" in clean:
            tags.add("git-publish-push-wildcard-refspec")
            continue
        # Handle "local:remote" refspec format — the remote side is the target.
        target_branch = clean.split(":")[-1] if ":" in clean else clean
        # Normalize every ref spelling git resolves server-side (heads/main,
        # remotes/<remote>/main, refs/... ) to the bare name so the path form
        # cannot dodge the protected-name check.
        normalized = _normalize_ref(target_branch)
        if normalized in _AMBIGUOUS_REFS:
            tags.add("git-publish-push-ambiguous-ref")
        elif normalized in _PROTECTED_BRANCHES:
            # Distinguish the bare-name spelling from the ref-PATH spelling:
            # they are separate catalog rules, and reporting the wrong one would
            # let an operator disable a row that is not what fired.
            tags.add(
                "git-publish-push-protected-ref-path"
                if normalized != target_branch
                else "git-publish-push-protected-branch-name"
            )
    return frozenset(tags)


def _git_publish_floor_tags(text_lower: str) -> frozenset[str]:
    """Return the git-publish rule tags a command trips, EMPTY if it is allowed.

    Same analysis as :func:`_is_push_to_protected_branch` (which is now a thin
    boolean view of this), but it reports WHICH rule each denial belongs to so
    the enforcement site can honour an operator opt-out per rule. A tag is
    either a ``git-publish`` catalog rule id or :data:`_GIT_PUBLISH_UNGATED`.

    Three branches emit the ungated tag, deliberately: substitution / expansion
    glue in a push command, a segment detected as a push that does not parse
    cleanly, and a push detected on the whole string with no clean push segment
    surviving the split. None of them is a user-facing rule — they are the
    anti-obfuscation backstop, and gating them would let ``git push origin
    ma$(echo)in`` be allowed by disabling ONE row, defeating the protected-branch
    rule without disabling it.

    Iterates the command's TRUE shell segments (split only on ``;`` / ``&&`` /
    ``||`` / ``|`` / newline — NOT on ``$(`` / backtick, which are glued into a
    single word by the shell), and collects across ALL of them: a benign feature
    push cannot vouch for a sibling protected one.
    """
    tags: set[str] = set()
    saw_push = False
    for command in _split_push_command_segments(text_lower):
        # ``_is_git_publish`` (not ``_git_push_args``) gates the checks so that
        # glue-evasion forms — which do NOT tokenize to a clean ``git`` token —
        # are still recognized as pushes and cannot slip past the ambiguity /
        # fail-closed guards below.
        if not _is_git_publish(command):
            continue
        saw_push = True
        # Substitution / expansion glue anywhere in a push command makes it
        # unverifiable (the shell fuses it into the verb or the target word).
        # This is also what covers brace expansion, which is why
        # ``git-publish-push-brace-expansion-refspec`` stays floor-enforced.
        if _AMBIGUOUS_EXPANSION_RE.search(command):
            tags.add(_GIT_PUBLISH_UNGATED)
            continue
        args = _git_push_args(command)
        if args is None:
            # Detected as a push but not cleanly parseable. That normally means
            # OBFUSCATION (``git$(echo ' ')push``) -> ungated deny.
            #
            # One exception: a shell WRAPPER carrying the push inside a quoted
            # argument. Admitting ``(`` as a leading separator makes the outer
            # line match the detector, because the ``(`` sits right after the
            # wrapper's quote -- but the outer line is not itself a push, so
            # there is no ``git`` token here to parse and this is not evasion.
            # Denying it blocked ordinary work: a FEATURE-branch push inside a
            # subshell inside ``bash -c`` was refused along with a protected one.
            #
            # The caller evaluates every nested payload source on its own, so
            # defer to that reading rather than guessing from a line that cannot
            # carry the answer.
            #
            # Defer only when a payload is ITSELF a publish, because that is the
            # source the caller will actually judge. Asking merely whether a
            # payload EXISTS was a bypass: an ARGUMENT that happens to share a
            # name with a shell verb (a remote or refspec called ``eval``) makes
            # the walk report a payload, and quoting the program defeats the
            # ``git`` anchor so the args come back None -- together those allowed
            # a protected-branch publish that nothing downstream ever judged. A
            # payload that is not a publish answers nothing, so it no longer buys
            # a pass, and with no payload at all there is nothing to wait for.
            #
            # Guarded because this runs inside the PreToolUse gate, which must
            # return a security DECISION and never raise. Failing CLOSED is the
            # only sound answer here: an exception means we cannot tell whether a
            # payload reading exists to defer to.
            try:
                defer_to_payload = any(
                    _is_git_publish(payload)
                    for payload in _nested_shell_payloads(normalize_shell_command(command))
                )
            except Exception:
                tags.add(_GIT_PUBLISH_UNGATED)
                continue
            if not defer_to_payload:
                tags.add(_GIT_PUBLISH_UNGATED)
            continue
        tags |= _push_segment_targets_protected(args)
    if not saw_push:
        # A push was detected upstream (e.g. glue-evasion ``git_push``) but no
        # clean ``push`` segment survived splitting — deny to be safe.
        tags.add(_GIT_PUBLISH_UNGATED)
    return frozenset(tags)


def _is_push_to_protected_branch(text_lower: str) -> bool:
    """Return True if ANY ``git push`` in the command targets a protected branch.

    A bare ``git push`` (no explicit branch) is BLOCKED because the current
    branch might be main/mainline. Only explicit non-protected branch targets
    are allowed. ALL refspecs of ALL push sub-invocations are checked: git
    accepts multiple refspecs, and a shell command can chain multiple pushes
    (``push origin feat && push origin main``). Force pushes to feature
    branches are allowed (normal PR workflow); force pushes to protected
    branches are blocked by the target check.

    Iterates the command's TRUE shell segments (split only on ``;`` / ``&&`` /
    ``||`` / ``|`` / newline — NOT on ``$(`` / backtick, which are glued into a
    single word by the shell). Each segment that is a git-publish (detected via
    ``_is_git_publish``, so glue-evasion like ``git$(echo ' ')push`` is seen) is
    validated and FAILS CLOSED:

    * any command-substitution / brace-expansion / backtick glue in the segment
      — in the verb OR the target (``origin ma$(echo)in`` -> ``main``) — is
      unverifiable -> deny;
    * a segment that ``_is_git_publish`` flags as a push but ``_git_push_args``
      cannot cleanly parse (obfuscated) -> deny;
    * a bare push, ambiguous ref, or explicit protected target -> deny.

    Only an explicit non-protected branch target is allowed. EVERY push segment
    is checked (a benign feature push cannot vouch for a sibling protected one).
    Force pushes to feature branches stay allowed (normal PR workflow). If a
    push was detected upstream but no segment here parses as one, denies.

    FLOOR SEMANTICS: this ignores opt-out state, so it answers "would the floor
    deny this at all". Enforcement in :func:`is_denied` uses
    :func:`_git_publish_floor_tags` instead, which reports WHICH rule fired so a
    disabled rule stays disabled.
    """
    return bool(_git_publish_floor_tags(text_lower))


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


# ── Sensitive Paths ──
# Directories and files that must never be read by the agent.
# Patterns are resolved relative to $HOME at check time.

_SENSITIVE_HOME_DIRS: list[str] = [
    # Gateway-owned Kiro auth staging. Owner-only filesystem mode does not
    # isolate another process running as the same UID, so every agent sandbox
    # and the shared read/write hook floor hide this fixed parent.
    ".kiro/crew-auth-staging",
    ".aws",
    ".ssh",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker/config.json",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # ACP adapter OAuth token stores. Each adapter owns its own sign-in flow and
    # persists its own tokens; Kiro Crew never reads them, and only ever checks
    # that the file EXISTS so it can name the right sign-in command. An agent
    # that could ``fs_read`` one could impersonate the operator against that
    # vendor, so both are on the floor "precisely so nothing else does".
    #
    # Only the token leaf is classified. The sibling config files — codex's
    # ``config.toml``, claude's ``settings*.json`` — deliberately stay readable:
    # routing diagnosis needs them and they carry no credential.
    #
    # These are ``$HOME``-rooted defaults. Both adapters honour a home override
    # (``CODEX_HOME``; ``CLAUDE_CONFIG_DIR`` / ``CLAUDE_HOME``), re-anchored in
    # ``_home_dir_targets_uncached`` so an override cannot move the token out
    # from under the gate.
    ".codex/auth.json",
    ".claude/.credentials.json",
    # (The Notes builtin's GitHub PAT lives under the crew data-home at
    # ``<prefix>/workspace/md-notebook/pat``; it is added below via
    # ``_CREW_SECRET_LEAVES`` so BOTH ``.kiro/crew`` and the legacy ``.kirocrew``
    # data-home are covered — ``config_dir()`` can resolve to either.)
    # Enterprise SSO cookie store. The public core ships no bundled SSO
    # integration, but the browser-auth layer already references this cookie
    # path (browser/auth.py), an edition CredentialPolicy redacts its session
    # token, and a companion IdentityProvider watches it for rotation. The cookie
    # is a live bearer credential: an agent that could fs_read it could
    # impersonate the user against every SSO-gated service. Classify the whole
    # directory so the cookie and its sidecars are covered. Generic and inert on
    # a host that does not have it — legitimate readers (the companion cookie
    # jar) open it directly + SEL-audited, not through this shared gate.
    ".midway",
    # kiro-cli / amazon-q auth stores hold the live SSO bearer token, read by
    # the dashboard credit pill via the audited kiro_usage_api._token_from_sqlite
    # helper. Classify the WHOLE data directories (not just data.sqlite3) so the
    # WAL/SHM/journal sidecars — which can hold the same credential bytes — are
    # covered too. Agent file tools must not read them through the shared gate.
    # The internal reader opens the DB read-only + SEL-audited (NOT via
    # is_sensitive_path), so it still works; the sandbox bind-mount list
    # (sandbox.py) is SEPARATE, so kiro-cli's own auth is unaffected.
    # The identity-store directories come from the single canonical table
    # (``identity_stores.IDENTITY_STORE_ROOTS``) so this fence and the five other
    # readers cannot drift apart (#6352). The splice emits all eight in table
    # order (``.local/share`` -> ``Library/Application Support`` ->
    # ``AppData/Local`` -> ``AppData/Roaming``, kiro-cli before amazon-q), which
    # is the exact order this list carried before the refactor -- a golden test
    # freezes that the final list is unchanged.
    #
    # Windows layouts: current kiro-cli writes the local, non-roaming app-data
    # directory (%LOCALAPPDATA% defaults to ~/AppData/Local); the Roaming entries
    # cover layouts that used %APPDATA% (defaults to ~/AppData/Roaming). These
    # matchers are home-anchored, so a profile redirected outside the home
    # directory is not covered -- the default location is what agent file tools
    # can reach by a fixed relative path.
    *fenced_home_dirs(),
]

# ── KiroCrew's own data-home secrets & governance trust-root ──
# These leaves live directly under KiroCrew's data home and must never be
# readable OR writable through the shared file gate (``is_sensitive_path``):
#
#   .env                          channel/API credentials (Slack/Discord/…)
#   browser-cookies.txt           reusable browser-auth session cookies …
#   playwright-storage-state.json … and the Playwright storage-state they become
#   sel_hmac.key                  Security Event Log HMAC key — signs the
#   security_events.jsonl         tamper-evident audit chain (``sel.py``);
#   trust                         the key now lives at ``trust/sel_hmac.key``
#                                 (owner-only dir OUTSIDE the log's directory);
#                                 the bare leaf covers pre-migration installs
#   app_admission.json            App Kit admission ceiling (apps/admission.py)
#   security_policy.json          governance ceiling (KEYSTONE, governance.py)
#   profiles                      per-surface governance profiles
#   admission_policy.json         signed-plugin admission trust root
#   denied_commands.json          denied-command opt-out ceiling — if an
#                                 auto-approved/YOLO agent shell could WRITE it,
#                                 it could set ``disable_all=true`` and defeat
#                                 the whole deny gate after a restart, so it must
#                                 be neither readable nor writable via any shell
#                                 form (operator edits it out-of-band via the
#                                 dashboard ``/api/security/…`` endpoints)
#   oauth_endpoints.json          operator OAuth consent-endpoint extension —
#                                 each entry widens the banner-only OAuth
#                                 entropy carve-out, so a writable file would
#                                 let the agent exempt an attacker host from
#                                 the exfiltration heuristics (operator edits
#                                 it out-of-band by hand)
#   token_signing.key             dashboard access/refresh token signing key
#   refresh_chains.json           refresh-token chain state
#   .local_secret                 internal MCP/cron/hook callback auth secret
#
# An agent that could fs_read any of these could forge auth tokens, impersonate
# internal callers, exfiltrate live sessions, or rewrite its OWN security
# ceiling; one that could fs_write them could neuter its governance/admission
# limits. Every legitimate reader/writer (token_secret.py, refresh_tokens.py,
# sel.py, apps/admission.py, governance.py, cli_commands.py, mcp_core.py, …)
# opens these directly (NOT via this gate), so real functionality is unaffected.
#
# Each leaf is expanded under EVERY known crew data-home prefix so the secret is
# gated identically whether it lives in the current home (``~/.kiro/crew``) or a
# pre-move legacy home (``~/.kirocrew``) that a user still has on disk. Keeping
# one leaf list means a new secret is added once and covered in both locations.
_CREW_HOME_PREFIXES: tuple[str, ...] = (".kiro/crew", ".kirocrew")
_CREW_SECRET_LEAVES: list[str] = [
    ".env",
    # Owner-authored meetings edits are deliberately outside the meeting
    # directories agents write. They are returned verbatim to the owner and may
    # contain credential-shaped examples or private corrections, so an agent must
    # neither read nor overwrite them through file tools. The Meetings backend
    # opens this directory directly, so its save/overlay/revert flow is unaffected.
    "apps/meetings/data/edits",
    # The Notes builtin stores a GitHub Personal Access Token here so it can
    # push a vault. Owner-only mode (0600) does not isolate another process
    # running as the same UID, and the token is a live bearer credential for the
    # user's repositories, so it belongs behind the shared floor like every other
    # credential store. The app's own backend opens it directly rather than
    # through this gate, so it keeps working. It is a leaf here (not a flat
    # ``~/.kiro/crew`` entry) so it is generated for BOTH ``_CREW_HOME_PREFIXES``:
    # a user may still have a pre-move legacy ``.kirocrew`` home on disk holding a
    # live PAT, so it must be protected there too. A vault relocated with
    # ``MD_NOTEBOOK_HOME`` falls
    # outside a home-relative entry; the default path is what ships and what an
    # agent would find.
    "workspace/md-notebook/pat",
    # The WhatsApp channel's linked-device session store (whatsmeow's sqlite
    # keys). It IS the credential: anything that can read it can act as the
    # operator on WhatsApp, read every chat and send as them, with no second
    # factor and nothing on the phone to notice. Owner-only file modes do not
    # isolate another process running as the same UID, and a prompt-injected
    # agent's fs_read is exactly that process, so it belongs behind the shared
    # floor like every other credential store. Classified as the whole DIRECTORY
    # so the WAL and SHM sidecars, which hold the same key bytes, are covered
    # too. The channel's own client opens it directly rather than through this
    # gate, so pairing keeps working.
    "whatsapp",
    # The Notes builtin's vault registry. It is not a secret, but it stores each
    # vault's on-disk ``localPath``, which auto-sync trusts and runs ``git
    # add``/``commit``/``push`` against. A prompt-injected agent that could
    # rewrite this file would repoint a vault at an unrelated repository and have
    # the app commit and push work from it outside the hook controls, so the
    # agent must not be able to write it. The app's own backend opens it directly
    # rather than through this gate, so it keeps working.
    "workspace/md-notebook/vaults.json",
    # The Notes builtin's sync settings. ``autoSync`` here is the bit that
    # AUTHORIZES the background loop's unattended ``git push`` (using the
    # app's stored PAT), and ``autoSyncMins`` sets its cadence. A prompt-injected
    # agent that could write this file would flip on unattended pushing without
    # the operator's consent — the same escalation the ``vaults.json`` entry
    # above guards against, one step earlier. The user toggles it through the
    # HMAC-gated ``PUT /api/settings``; the app's own backend opens the file
    # directly rather than through this gate, so it keeps working.
    "workspace/md-notebook/settings.json",
    # The AWS Control builtin's app data directory. ``backup.json`` in here holds
    # ``nightly``, the bit that AUTHORIZES the app's startup loop to upload the
    # gateway's memory and workspace to S3 unattended, so a prompt-injected agent
    # that could write it would schedule an owner-billed export the owner never
    # asked for -- routing around the owner-only HTTP surface that is supposed to
    # be the only way to turn it on. Exactly the ``autoSync`` escalation above,
    # one app over.
    #
    # Classified as the whole DIRECTORY, not that one file, for the reason the
    # ``whatsapp`` entry above is: an atomic write goes through a temporary in the
    # same directory and is then renamed, so fencing only the final name leaves a
    # writable path to the same bytes. Its siblings (the cost cache, the library
    # ledger) have no legitimate file-tool reader either -- the app's own backend
    # opens every one of them directly rather than through this gate, so the app
    # keeps working and future state files are covered without a new entry.
    "apps/aws-control/data",
    # The same app's gateway-owned transfer staging. Not a secret store: it holds
    # object bytes in flight between the AWS CLI's write and the gateway's
    # read-back. It is fenced because the destination path is what a same-UID
    # agent would swap for a link to make the CLI -- which writes with the
    # gateway's reach -- land those bytes on a credential file. Every agent
    # sandbox masks the directory as well (``sandbox._CREW_HIDDEN_LEAVES``); the
    # one CLI spawn that must write into it is granted its per-call subdirectory
    # explicitly.
    "aws-control-staging",
    "browser-cookies.txt",
    "playwright-storage-state.json",
    # Per-session work ledgers (session_ledger.py). Not credentials, but each
    # directory is one session's private work state, and the ledger's whole
    # authorization model is "a session reaches only its OWN ledger" (the HTTP
    # routes derive the target from the vetted caller identity). An agent's
    # auto-approved file tools would bypass that boundary sideways — any
    # session could read or corrupt any other session's ledger straight off
    # disk. Unlike the transcript files beside it, the ledger has no
    # legitimate file-tool reader: every legitimate access goes through the
    # backend module, which opens paths directly rather than through this
    # gate, so nothing breaks by fencing the whole subtree.
    "ledger",
    # The optional Playwright extension token. It removes the browser-side approval
    # click for an attach, so a process that could read it could attach to the
    # operator's logged-in browser without them seeing a prompt. The gateway hands
    # it to the CLI through the environment, so nothing legitimate opens the file.
    "playwright-extension-token",
    # Legacy SEL HMAC key location (pre-``trust/`` installs, and any stale file
    # a backup restore resurrects). Kept alongside the ``trust``
    # directory entry below so the key is gated at BOTH locations.
    "sel_hmac.key",
    # SEL trust-root directory: sel.py stores/migrates the audit chain's HMAC
    # signing key at ``trust/sel_hmac.key`` — OUTSIDE the log's directory, so
    # write access to the log dir no longer implies re-signing power. The whole
    # dir is gated (like ``profiles``/``run``) so future trust-root material is
    # covered without a new entry. sel.py opens the key directly, not through
    # this gate.
    #
    # Spec Builder's decision record (``trust/spec-builder-decisions.json``) relies
    # on that whole-directory gating. The app refuses a second answer for a decision
    # it has recorded, so an agent able to write the file could erase an entry to
    # make a settled decision answerable again, or forge one to lock a decision the
    # user never answered. Gating the leaf alone was not enough: its parent under
    # ``workspace/`` was itself replaceable with one ``ln -s``, and the app opens the
    # path directly (as keystone writers must), so it would have followed the link.
    "trust",
    "security_events.jsonl",
    # Rotated SEL segments. sel.py closes the live log at a size cap and renames
    # it into this directory, so a segment holds exactly the same audit records
    # the live file does and must be gated identically — a rotated log that the
    # agent could read (or rewrite, then let the chain re-anchor from) would make
    # rotation itself the way around the fence. Directory entry, so every
    # segment is covered without a per-name matcher. sel.py opens segments
    # directly, not through this gate.
    "security_events.d",
    "app_admission.json",
    "security_policy.json",
    "profiles",
    # The centrally-distributed ceiling's last-known-good cache
    # (``platform/policy_distribution.py``). Gated as a DIRECTORY, and the reason
    # is not just that it holds a copy of the policy: the sidecar metadata records
    # the SOURCE the copy came from, and the loader honours that source when
    # deciding whether the cache is this host's last-known-good. An agent able to
    # write here could publish itself a ceiling AND the provenance to make the next
    # boot adopt it — a full escape from ``security_policy.json`` being unwritable,
    # around the side. Read matters as much as write: the cache is a verbatim copy
    # of the policy the trust-root entry above exists to keep unreadable, so
    # leaving it open would hand over the exact deny patterns an evasion needs.
    # policy_distribution.py opens both files directly, not through this gate.
    "policy_cache",
    "admission_policy.json",
    "denied_commands.json",
    # The cron store. It holds access-control state, not just scheduling data:
    # ``session_key`` decides which session may manage a job through the MCP cron
    # tools and where the job's output is delivered, ``approval_mode`` is a
    # per-job auto-approval decision, and ``command``/``script`` decide what
    # gets executed on the host on a schedule. While the store sat outside the
    # protected leaves, an auto-approved shell could reassign ownership, flip a
    # job to auto-approve, or rewrite what a scheduled job runs with an ordinary
    # file edit — an open side door around the MCP tools' deliberate
    # cannot-write-``session_key`` rule and the ``self-protection-cron-adopt``
    # denied command, because those controls match command strings while the
    # state lives in the file. The gateway's own writers open the store
    # directly, not through this gate, so the cron service is unaffected; the
    # cost is that a human hand-edit through an agent shell is refused, the
    # same trade-off every other keystone leaf makes. The ``cron-history``
    # sidecar directory (per-job records plus the index) sits on the same floor:
    # it is a tamperable audit trail of those runs, and one directory rule
    # covers the records, the index, and the lock/temp files — the same
    # treatment ``webhooks`` and ``profiles`` already get.
    "crons.json",
    "cron-history",
    # The in-flight run markers (``cron_inflight``) belong on the same floor for
    # a sharper reason than the two above: the boot-time loop-stall breaker
    # TRUSTS them. One marker whose PID matches a cron-surface crash dump is
    # what makes the breaker park that job, so a marker an agent could write is
    # an unauthorized "pause this job" primitive that routes around both the MCP
    # cron tools and the owner-only HTTP surface, and a marker it could DELETE
    # disables the breaker for a crash loop that is about to recur. The evidence
    # an automatic state change rests on has to be at least as protected as the
    # state it changes, which is ``crons.json`` directly above. The service and
    # the doctor open the directory directly rather than through this gate, so
    # both keep working; nothing legitimate reads a marker through a file tool.
    "cron-running",
    # Saved workflow definitions are executable capabilities whose presence is
    # authorized only by an explicit dashboard action. Same-UID owner-only file
    # modes do not stop an agent file tool from planting or rewriting a valid
    # definition, so fence the whole directory, including atomic-write temp
    # files. The dashboard and workflow service open it directly and remain able
    # to create, list, update, and execute definitions.
    "workflow_library",
    # The operator's OAuth consent-endpoint extension
    # ({additional_authorization_endpoints: [{host, path}]}). Each entry widens
    # the banner-only OAuth entropy carve-out (_OAUTH_AUTHORIZATION_ENDPOINTS),
    # so this is a trust boundary of the same class as ``denied_commands.json``
    # directly above: an agent that could WRITE it could exempt an
    # attacker-controlled host from the exfiltration heuristics — widening its
    # own trust ceiling — and one that could READ it would learn which extra
    # hosts are exempt and aim there. Read+write blocked on both the tool path
    # (``is_sensitive_path``) and the shell forms. The only legitimate reader
    # (``_load_operator_oauth_endpoints`` in this module) opens the file
    # directly, not through this gate; the operator hand-edits it out-of-band
    # (there is deliberately no dashboard writer).
    "oauth_endpoints.json",
    # Per-session AgentCore Gateway inbound JWTs (directory name reserved
    # before the writer lands). Owner-only ``0600`` does not isolate another
    # process running as the same UID, so the directory belongs behind the
    # shared floor like every other credential store. Classified as the
    # whole DIRECTORY so atomic-write temps and every sidecar file are
    # covered.
    "agentcore-inbound",
    # Which checkout the gateway executes (Dev Fleet "Make live"). The pointer is
    # resolved during startup and exec'd into, so a writable one is arbitrary
    # code execution in the gateway's own identity — the agent must not be able
    # to author it, and must not be able to read it back to discover a target to
    # aim at either. Only the human-driven dashboard cutover writes it, and the
    # gateway's own startup reader opens it directly rather than through this
    # gate, so both keep working.
    "live_target.json",
    # Holds `backup/redaction.json`, the switch that decides whether a bundle
    # leaving this machine is redacted first. An agent that could write it would
    # turn redaction off and every later upload would carry the operator's
    # secrets verbatim; an agent that could read it learns whether the memory
    # store is currently being scrubbed. Flipping it is the attack and reading it
    # is reconnaissance, so this needs read AND write protection, not just write.
    #
    # The DIRECTORY is classified, not just the leaf inside it. Naming only the
    # leaf leaves the container writable, and a writable container is the same
    # hole one level up: replace `backup/` with a symlink and the protected leaf
    # now resolves somewhere unprotected, where the switch can be rewritten at
    # will. Restore's rollback copies live at `pre-restore-<ts>/`, not here, so
    # nothing legitimate is shut out, and the product's own reader opens the file
    # directly rather than through this gate.
    "backup",
    # The computer-use primary enable ({enabled, allowed_apps, extra_denied_apps}).
    # Same class of control as ``denied_commands.json`` directly above, and here
    # for the same reason: flipping ``enabled`` grants full desktop observation
    # plus keystroke/click synthesis into the operator's real applications — a
    # security ceiling, not a preference. Storing it in the agent-readable
    # ``config.json`` would leave it writable by any auto-approved agent shell
    # (verified: ``is_sensitive_bash_command("echo x > …/config.json")`` is None),
    # so it lives here and gets read+write protection on BOTH the tool path
    # (``is_sensitive_path``) and the shell forms (``cat``, ``>``, ``tee``,
    # ``tar -C`` / ``unzip -d`` extraction into the trust root). The dashboard PUT
    # handler is the only writer and it opens the path directly, not through this
    # gate, so the operator's Settings toggle still works.
    "computer_use.json",
    # Browser Mode's durable ENABLE gate. Same class of control as
    # ``computer_use.json`` directly above: while it is present the browse proxy
    # is registered and the ``browser_*`` tools are in the agent's tool list,
    # which lets the agent operate a real browser — and in attach mode that is
    # the operator's own running, logged-in browser. Presence alone is the
    # authorization, so a bare ``touch`` of this file would be a prompt-injected
    # self-grant of browser operation. It gets read+write keystone protection on
    # both the tool path (``is_sensitive_path``) and the shell forms (``touch``,
    # ``>``, ``tee``, extraction verbs). The dashboard PUT handler is the only
    # writer and opens the path directly, not through this gate, so the Settings
    # toggle still works. The sibling ``browser-engine`` leaf is protected too:
    # it selects the browser Playwright launches, so an agent-authored value
    # could steer the launch, and it must not diverge from the enable beside it.
    "browser-mode-enabled",
    "browser-engine",
    # Ops Mission Control's third-party provider tokens (PagerDuty / Datadog
    # API + application keys). These are live credentials against a user's
    # production incident tooling: a leaked one can acknowledge or resolve real
    # pages. They are here rather than in ``config.json`` for two concrete
    # reasons — an app's ``data/config.json`` is served over
    # ``/api/apps/<name>/config`` WITHOUT session auth, and ``config.json``
    # itself is writable by any auto-approved agent shell. The read+write
    # keystone floor is the only placement where the agent can neither read the
    # tokens nor overwrite them. The authenticated dashboard PUT handler is the
    # sole writer and opens the path directly, so Settings still works.
    "ops_mission_control_secrets.json",
    # Ops Mission Control's AUTONOMY CEILING: the app mode (observe/propose/act) and
    # the per-signal act-rules. This is the exact same class of control as
    # ``computer_use.json`` above — flipping ``mode`` to ``act`` plus adding a matching
    # rule is what authorizes a write against the user's production incident tooling —
    # and it was living in the agent-writable ``data/config.json``. A prompt-injected
    # agent could therefore mint the dashboard token, PUT ``mode=act`` with a rule
    # matching a signal, and unlock provider actions the operator never granted, which
    # defeats the app's central safety property (``effective = min(app_mode, rule_mode)``
    # is only a ceiling if the agent cannot raise it). Found in review. Here for the same
    # reasons as the secrets leaf directly above — served unauthenticated over
    # ``/config`` and writable by any auto-approved shell in ``config.json`` — so it moves
    # to the read+write keystone floor. Dashboard PUT is the sole writer and opens the
    # path directly.
    "ops_mission_control_policy.json",
    # Recorded consent to call a PAID AWS service (Amazon Polly for TTS, Amazon
    # Transcribe for STT). Same class of control as ``computer_use.json`` above:
    # the record is what AUTHORIZES billable requests against a specific AWS
    # account, so an agent that could write it would consent on the operator's
    # behalf to spending the operator's money — and one that could write it
    # could also point the grant at an account of its choosing, which is the
    # unintended-account outcome the gate exists to prevent. Reading it is
    # fenced too: the file names the account id and caller ARN that a profile
    # resolves to, which is reconnaissance an agent should not get for free from
    # the shared gate. The authenticated dashboard ``/api/aws/consent`` handler
    # and the ``kirocrew aws-consent`` CLI are the only writers and open the
    # path directly, not through this gate, so both keep working.
    "aws_service_consent.json",
    # Recorded consent to deliver a file whose contents the credential scanner
    # flagged. Same class of control as ``aws_service_consent.json`` above: the
    # record is what AUTHORIZES a flagged file past four independent content
    # gates, so an agent that could write it would consent on the owner's behalf
    # to shipping the owner's secrets. Reading it is fenced too -- the file says
    # which delivery destinations the owner has already blessed, which tells an
    # agent where a flagged file would land unrefused, and that is reconnaissance
    # it should not get for free from the shared gate. The authenticated,
    # owner-gated dashboard ``/api/file-delivery/consent`` handler is the ONLY
    # writer and opens the path directly, not through this gate, so it keeps
    # working; there is deliberately no CLI verb to fence.
    "file_delivery_consent.json",
    "token_signing.key",
    "refresh_chains.json",
    ".local_secret",
    # Durable channel transport state: Teams' conversation -> serviceUrl and
    # identity -> conversation maps, and Telegram's getUpdates cursor. Two shapes of
    # the same control -- where a message GOES, and which messages are SEEN. Calling
    # getUpdates with an offset is also the ack for everything below it, so an agent
    # that could write that cursor would make the gateway skip every queued and
    # future message, durably, past the restart that would otherwise clear it.
    #
    # Same class of control as ``workspace/md-notebook/vaults.json`` above: neither
    # is a secret, both are PLUMBING. ``teams/transport.py`` resolves an explicit
    # ``user:<upn>`` send target through the identity map, so an agent that could
    # write it could point one operator's UPN at a different person's conversation
    # and have the next cron result, subagent notice or ``send_message`` delivered
    # there instead. The inbound path binds a ``serviceUrl`` to the JWT's own
    # ``serviceurl`` claim and ``connector_host_allowed`` re-checks it wherever the
    # Connector token is attached, but neither attestation survives PERSISTENCE,
    # and no host check can tell one legitimate conversation id from another.
    # Reading is fenced with writing because the file enumerates the operator's
    # UPNs and the conversations they use.
    #
    # A DIRECTORY entry, not the file leaf, and that is the load-bearing part: a
    # file leaf matches only its exact name, while ``atomic_write`` publishes
    # through a ``tempfile.mkstemp`` sibling (``tmpXXXXXXXX.tmp``) in the same
    # parent. With the store loose in the data-home root an agent watching that
    # directory could overwrite the temp file in the window before the rename and
    # have the rename publish its own routing. A directory entry covers every
    # child, random temp names included. (``trust``, ``profiles`` and
    # ``cron-history`` above are directories for the same reason among others.)
    # ``ServiceUrlStore`` and ``TelegramClient`` open their paths directly, not
    # through this gate, so
    # proactive routing across a restart is unaffected.
    "routing",
    # Inbound-webhook credential store directory. It holds the bearer HASHES and
    # the recoverable HMAC signing secrets for /api/hooks/agent, which is on the
    # dashboard-auth bypass list because it authenticates itself. An agent that
    # could WRITE this store could append a token hash it chose and then drive
    # arbitrary agent turns through that route from outside; one that could READ
    # it could sign requests as an existing integration. The store's own
    # reader/writer (webhooks.WebhookTokenStore) opens it directly, not through
    # this gate, so the feature is unaffected.
    #
    # The DIRECTORY is named, not the file, because the store is published with
    # mkstemp + os.replace: gating only ``tokens.json`` left the not-yet-renamed
    # ``*.tmp`` inode writable by a same-UID agent (0600 does not stop the same
    # user), and the rename would then publish agent-chosen content as the live
    # credential store. One directory rule covers the store, its lock file and
    # every temp file — the same treatment ``profiles`` and ``run`` already get.
    "webhooks",
    # Pinned installer provenance authorizes an executable to receive staged
    # Kiro identity credentials. Agent reads/writes must not be able to replace
    # this trust decision.
    ".kiro_cli_binary_trust.json",
    # MCP Apps spool (SEP-1865). Defense-in-depth: the per-render callback
    # capability (`callback_secret`) is delivered owner-WS-only and never
    # written to model-visible text, but the spool records also hold app HTML
    # and tool data, so the whole directory sits on the sensitive floor —
    # agent file tools cannot read it. Legitimate readers (gatewayd writer,
    # dashboard render/relay) open it directly in-process.
    "mcp-apps",
    # Runtime exec dir. ``run/`` holds paths the gateway executes OUTSIDE the
    # agent sandbox: the sandbox launcher scripts (``sandbox.py`` execs
    # ``python <home>/run/kirocrew_sandbox_*.py``) and the remote-instance
    # run-marker ``gateway-<port>.bin`` (``instances/run_marker.py``), whose
    # contents the SSH token-mint reads and ``exec``s on the remote host. A
    # prompt-injected / sandboxed agent that could WRITE into this dir could point
    # a marker (or a launcher) at an attacker-controlled binary and, on the next
    # routine token refresh, get it executed unsandboxed — a reachable sandbox
    # escape (owner + ``-x`` checks don't help; agent writes run as the same user).
    # Classify the whole dir read+write, like the other trust roots above. The
    # gateway's own writers open these paths directly and do NOT route through this
    # gate, so legitimate startup/spawn writes still work.
    "run",
    # Encrypted secret vault directory — denylists the entire subdirectory so
    # the key file, ciphertext store, lock, and atomic-write temp files are all
    # unreadable to the agent through any Kiro Crew-mediated channel (PR 1 of
    # #2351). The verb-independent sensitive-path backstop covers a scripted
    # ``python -c "open('~/.kiro/crew/.vault/...')"`` too.
    ".vault",
    # KAS-mode auth token store. In the KAS-embedded runtime Kiro Crew performs the
    # Kiro OIDC lifecycle itself (there is no kiro-cli), and persists the resulting
    # access/refresh tokens as ``0600`` files under this dir. They are live bearer
    # credentials for the model service, so — like every other credential store —
    # they sit behind the shared read+write floor: an auto-approved or sandboxed
    # agent must not be able to read the token back or overwrite it. The auth
    # module's own store opens these paths directly rather than through this gate,
    # so login/refresh keep working. Fence the whole ``kas`` dir (not just
    # ``kas/auth``): fencing only the leaf would let the agent rename ``kas`` and
    # then read the relocated token store from outside the fence.
    "kas",
    # The identity/auth SQLite store, named by the canonical filename constant
    # (``identity_stores.AUTH_SQLITE_DB``) rather than a fresh literal, so this fence
    # cannot drift from the readers that resolve the same store. It holds live bearer
    # tokens, so an agent that could read it could act as the user against the model
    # service, and one that could write it could forge the identity rows.
    #
    # The kiro-cli and amazon-q stores are fenced by DIRECTORY (``fenced_home_dirs()``
    # above), which covers each store's sidecars and temporaries for free. The crew
    # data home cannot be fenced the same way -- reading ``config.json`` and
    # ``sessions.db`` there is routine and intended -- so the store is named as a leaf
    # here, and the name is fenced BEFORE a writer for that location exists (the
    # treatment ``agentcore-inbound`` above gets): a fence that arrives with the
    # writer arrives one release after the first bytes it should have covered.
    #
    # The WAL/SHM/journal sidecars are spelled out for the reason the directory
    # entries do not have to be: a file leaf matches its exact name only, and a
    # sidecar carries the store's credential bytes -- ``kiro_cli`` documents the same
    # fact from the other side, that identity rows read as absent when the ``-wal``
    # sidecar is missing. (``.tmp``/``.lock`` publish artifacts in the same parent are
    # already covered by ``_KEYSTONE_ARTIFACT_SUFFIXES`` below.)
    #
    # Scoped to the crew data-home prefixes and NOT matched by basename:
    # ``data.sqlite3`` is a generic filename, so a basename rule would refuse an
    # unrelated application database anywhere under the home directory. No legitimate
    # reader is affected -- every identity-store reader (``kiro_usage_api``,
    # ``kiro_cli``, ``kiro_prerequisite``) resolves its path through
    # ``identity_stores`` and opens it directly, not through this gate.
    AUTH_SQLITE_DB,
    *(f"{AUTH_SQLITE_DB}{suffix}" for suffix in AUTH_SQLITE_SIDECAR_SUFFIXES),
]
_SENSITIVE_HOME_DIRS += [
    f"{prefix}/{leaf}" for prefix in _CREW_HOME_PREFIXES for leaf in _CREW_SECRET_LEAVES
]

# ── Publish artifacts of a keystone leaf ──
# Every leaf above is published through ``atomic_write``, which writes a
# ``tempfile.mkstemp(dir=path.parent, suffix=".tmp")`` sibling and renames it over the
# target; several stores also take a lock file beside the leaf they guard
# (``.policy.lock`` for the ops autonomy ceiling, ``ops_mission_control_secrets.json.lock``,
# ``.crons.lock``). Those siblings carry the SAME bytes as the leaf -- the temp holds the
# full payload for the whole write -- but a leaf entry matches its exact name only, so
# they sat outside the fence while the guarantee was stated as absolute.
#
# A DIRECTORY leaf never had this gap: its temps land INSIDE the fenced directory, where
# the ``startswith(target + os.sep)`` rule already covers them. That is exactly why
# ``webhooks``, ``routing``, ``.vault``, ``kas``, ``run``, ``cron-history`` and
# ``apps/aws-control/data`` are written as directories, and their comments say so. The gap
# is the leaves whose parent is NOT itself fenced -- in practice the crew data-home root,
# which cannot simply be fenced wholesale because reading ``config.json`` and
# ``sessions.db`` there is routine and intended (see ``_WRITE_PROTECTED_HOME_PATHS``).
#
# So the fence is DERIVED FROM the leaf declarations rather than restated per leaf: an
# artifact-shaped name sitting in the parent directory of any keystone leaf is protected.
# A leaf added later inherits the protection with no second entry to remember, which is
# the only version of this that stays true -- the reason the gap existed at all is that
# the exception was invisible at every call site.
#
# Derived from ``_CREW_SECRET_LEAVES``, deliberately NOT from ``_SENSITIVE_HOME_DIRS``:
# that list also carries ``.aws``, ``.ssh`` and the kiro-cli identity stores, whose parent
# is ``$HOME`` ITSELF, so deriving from it would fence ``~/*.tmp`` and ``~/*.lock`` across
# the user's entire home directory.
#
# Keyed on the artifact SHAPE, not on ``<leaf>.tmp``: the real mkstemp name is
# ``tmpXXXXXXXX.tmp`` and carries no leaf name at all, so a leaf-derived temp name would
# fence a spelling no writer produces. ``<leaf>.lock`` IS a real shape
# (``ops_mission_control_secrets.json.lock``), and the suffix rule covers both.
#
# Not included: ``deploy/pending-deploys.lock``. Its directory holds no keystone leaf, so
# there is no keystone payload beside it for the fence to protect.
_KEYSTONE_ARTIFACT_SUFFIXES: tuple[str, ...] = (".tmp", ".lock")
_KEYSTONE_ARTIFACT_PARENTS: list[str] = sorted(
    {
        # Every entry is ``<crew-prefix>/<leaf>`` so it always contains a separator,
        # making the rsplit safe: a bare leaf yields the crew home root, a path-shaped
        # leaf yields its own directory (``workspace/md-notebook``).
        f"{prefix}/{leaf}".rsplit("/", 1)[0]
        for prefix in _CREW_HOME_PREFIXES
        for leaf in _CREW_SECRET_LEAVES
    }
)

# ── Write-protected paths (block modification, allow reads) ──
# Runtime config files carry security-relevant resource ceilings (concurrent
# subagents, per-agent turn budget, warm-pool size). A prompt-injected agent
# with file-write access must not be able to rewrite these to inflate its own
# limits and drive host resource exhaustion (pentest — config-loader bound
# bypass, recommendation: block agent tools from modifying config files).
#
# They are DELIBERATELY NOT in ``_SENSITIVE_HOME_DIRS`` above: that list is the
# shared read+write gate, and reading config.json is routine and intended (the
# dashboard file viewer, ``cat``, and knowledge indexing all read it). We
# instead block only WRITES, at the agent file-edit tool gate
# (hooks.on_tool_call), via ``is_sensitive_write_path``. This is defense in
# depth on top of the loader's load-time clamp, which already neutralizes any
# inflated on-disk value no matter how it was written. The operator edits config
# out-of-band (dashboard config API / CLI), which do NOT route through this
# gate, so legitimate config changes still work.
# (The denied-command opt-out state does NOT live here — it is a security
# ceiling and lives on the read+write keystone floor in ``denied_commands.json``
# above, so no bash-level write matcher is needed for it. The computer-use primary
# enable is on that same floor, for the same reason.)
#
# SCOPE LIMIT worth stating where the matchers live: every path matcher in this
# module reasons about a PATH STRING. Computer use reaches state that has no path
# — a password field's ``AXValue``, a logged-in banking tab, an editor window
# already showing ``~/.aws/credentials`` as pixels and as accessibility text. No
# addition to either list here can see any of it. That is why
# ``computer_use/policy.py``'s bundle-id denylist (terminals, password managers,
# keychains) and its secure-subrole refusal are load-bearing security controls in
# their own right rather than conveniences, and why the always-on secure-field
# redaction has no policy key.
_WRITE_PROTECTED_HOME_PATHS: list[str] = [
    f"{prefix}/{leaf}"
    for prefix in _CREW_HOME_PREFIXES
    # config.json / config.local.json: security-relevant resource ceilings.
    # playwright-cli-config.json: the browse launch config
    # (browser_cli/launch.py). It holds no secret and the CLI must READ it on
    # every invocation, so it is write-protected rather than sensitive. But it is
    # an INPUT TO A SECURITY DECISION: the schema accepts
    # ``launchOptions.chromiumSandbox``, so an agent that could rewrite it would
    # turn the browser sandbox OFF for every later browse, and the change persists
    # until the next gateway start re-converges the file. Kiro Crew generates it
    # directly and does NOT route through this gate, so its own write still works.
    # Paired with the same leaf in _WRITE_PROTECTED_BASH_LEAVES — protected on one
    # path only is not protected.
    for leaf in ("config.json", "config.local.json", "playwright-cli-config.json")
] + [
    # Ops Mission Control's on-call schedule. WRITE-protected, not read+write
    # sensitive: it holds no secret and every teammate's instance must READ it to
    # answer "am I on call?", so classifying it as sensitive would break the
    # feature. But it is an INPUT TO AN AUTHORIZATION DECISION — an agent that
    # could rewrite it to name its own login would make
    # ``rotation.authorize_action`` -> ``_definitely_off_shift`` accept its own
    # forged shift and execute an off-shift production write against a teammate's
    # tooling. Found in review.
    #
    # This is the last of five instances of one class on this app's off-shift
    # refusal (the others: the GitHub login, the strict-gating flag, the
    # provider-config field list, and ``providers.<id>.enabled``). The fix is
    # placement, not logic: the app READS the schedule exactly as before, and only
    # the agent's own file/bash tools are refused. `ledger_sync` writes it through
    # a direct `git checkout` on the merge path, not through this gate, so team
    # sync still converges.
    f"{prefix}/apps/ops-mission-control/data/rotation.yaml"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The Ops Mission Control incident INDEX, for the same reason as the schedule above and
    # with the same read/write asymmetry: every teammate's instance reads it constantly (it is
    # the claim ledger and the board), so classifying it sensitive would break the app, but it
    # is an INPUT TO AN AUTHORIZATION DECISION.
    #
    # ``/incident/action`` looks the incident up by id and hands ``incident.signal`` to
    # ``rotation.authorize_action``, whose ``AutonomyRule.matches`` keys on
    # ``signal.source``/``resource``/``labels``. An agent that can rewrite this file can pair a
    # resource an operator's rule authorizes (``resource="prod-db-1"`` matching
    # ``resource_glob="prod-*"``) with a DIFFERENT provider target in ``labels`` — so the gate
    # approves one signal while the sink mutates another, and the authorization describes a
    # signal that does not exist. That is the same defect already fixed on ``/incident/claim``
    # by resolving the signal server-side; this is the same forgery reached through the store
    # instead of the request body, which server-side resolution cannot help with because the
    # store IS the server's copy. Found in review (GPT 5.6).
    #
    # The gateway's own writers (``store.claim``/``update_fields``, the reconcile SOP) open
    # this path directly and do not route through this gate, so the app keeps working; only
    # the agent's file-edit and shell tools are refused.
    f"{prefix}/apps/ops-mission-control/data/incidents/index.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # Downloaded MODEL WEIGHTS (speech recognition and embeddings both land here).
    # WRITE-protected as a whole directory, not read+write sensitive: the weights hold
    # no secret, and the settings surface and `kirocrew doctor` both read the directory
    # to report what is installed.
    #
    # They are an INPUT TO A TRUST DECISION. Each store verifies its file against a
    # pinned sha256 and then hands the PATH to a native loader, so a writable directory
    # leaves a window between the digest and the open in which the bytes can be
    # swapped -- and no amount of re-hashing closes it, because the loader re-opens by
    # name. Removing the writability removes the window instead: the agent cannot
    # modify the file at all, so the verified bytes are the loaded bytes. A poisoned
    # model is persistent and invisible, and for speech it means the user's own words
    # reaching the agent as something they did not say.
    #
    # Kiro Crew's own downloaders write here directly and do not route through this
    # gate, so first-run fetches, re-downloads after a failed check and the embedding
    # model install all keep working; only the agent's file-edit and shell tools are
    # refused. Paired with the same entry in _WRITE_PROTECTED_BASH_LEAVES -- protected
    # on one path only is not protected.
    f"{prefix}/models"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The dashboard session-history store, fourth instance of the
    # input-to-an-authorization-decision class (rotation.yaml, the alias
    # ownership record, the OMC index). Each slot's persisted metadata carries
    # ``created_by`` — the creator attribution that chat_persistence restores
    # on gateway restart and that ``authorize_target`` then trusts as the
    # member ownership boundary. An agent that could rewrite a victim
    # transcript's metadata to name a member's caller key would, after one
    # restart, hand that member send/read/stop over the victim session. The
    # same file also carries the companion-artifact binding and the slot mode,
    # both re-validated on restore for exactly this reason.
    #
    # WRITE-protected, not read+write sensitive: transcripts are the user's
    # own conversations, and reading them (grep for an old error message, a
    # path, a decision) is routine and legitimate. There is NO legitimate
    # agent WRITE — the gateway persists turns through direct Python calls,
    # which do not route through this gate, so persistence keeps working.
    # Deliberately NOT paired in _WRITE_PROTECTED_BASH_LEAVES: that matcher
    # blocks a command NAMING the path, which would deny the routine bash
    # reads above (same reasoning as the app-source entry) — the file-edit
    # tool gate is the enforcement point, and shell writes sit on the same
    # footing as config.json's.
    f"{prefix}/sessions"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The Connections tool-alias OWNERSHIP RECORD, third instance of the same class as the
    # two above and with the same read/write asymmetry. It holds no secret and the rebuild
    # reads it on every run, so classifying it sensitive would break the feature — but it is
    # an INPUT TO AN AUTHORIZATION DECISION, and by its own module's invariant 2 it is the
    # thing that AUTHORIZES DELETION: ``alias_record.load_claimed`` returns the pairs the
    # alias pass may strip from the agent spec, and nothing else grants that permission.
    #
    # An agent that can write this file can forge a ``committed`` record naming a
    # ``@slug/tool -> alias`` triple the user hand-wrote, together with the fingerprint of
    # the spec currently on disk (the spec is readable, so the fingerprint is computable).
    # The next rebuild then resolves the forgery as its own emission and deletes the user's
    # alias — laundering the edit through Kiro Crew's own trusted writer, which is what makes
    # it worse than editing the spec directly: the deletion is performed and persisted by the
    # legitimate owner of that file. The generation fingerprint cannot defend this, because a
    # forger reads the same spec it does. Found in review (GPT 5.6).
    #
    # ``alias_record._write`` opens the path directly via ``atomic_write`` and does not route
    # through this gate, so both record writes still work; only the agent's own file-edit and
    # shell tools are refused.
    f"{prefix}/connections-tool-aliases.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The settings-seed PROVENANCE RECORD (``acp.seed_provenance``), the alias
    # record's twin one seam over: it is what authorizes Kiro Crew to OVERWRITE and
    # then DELETE ``<work_dir>/.claude/settings.local.json``. The ACP client seeds
    # that file for a claude-agent-acp session and touches only the seed it owns;
    # ownership is this record plus the file on disk still hashing to the digest in
    # it. So an agent that can write this file can enter ``<path>: {size, sha256}``
    # for a settings file the USER hand-wrote — the file is readable, so both values
    # are computable — and the next session adopts it: the user's project settings
    # are overwritten with Crew's seed and unlinked on reset. As with the alias
    # record, the damage is done by Crew's own trusted writer, and the digest check
    # cannot defend it because the forger reads the same bytes it does.
    #
    # WRITE-protected, not read+write sensitive: the record holds no secret (it
    # names work dirs and digests), so the file-READ tools keep working and an
    # operator can still see why a seed was or was not adopted. There is no
    # legitimate agent WRITE at all — ``seed_provenance.record`` writes
    # the path directly through ``atomic_write``, which does not route through this
    # gate. Paired with the same leaf in _WRITE_PROTECTED_BASH_LEAVES and in
    # _BARE_TOKEN_PROTECTED_LEAVES below: protected on one path only is not
    # protected, and for a DELETION grant a ``cd`` must not be the whole bypass.
    f"{prefix}/settings_seeds.json"
    for prefix in _CREW_HOME_PREFIXES
]
_WRITE_PROTECTED_HOME_PATHS += [
    # The app-sources checkout root — the persistent tree every installed app
    # EXECUTES from (``apps.registry.app_source_dir``). This is a whole DIRECTORY
    # rather than a leaf, which the shared matcher already supports: it compares a
    # resolved path against the entry and its ``entry + os.sep`` prefix, so every
    # file under every checkout is covered without enumerating them.
    #
    # It is the strongest instance of the write-protection class, because the
    # protected file IS the executed code rather than an input to a decision about
    # it: an agent session with ordinary file-write tools could edit an installed
    # app's source, and that source then runs with the app's privileges on the
    # app's next launch. Nothing downstream neutralizes it — unlike ``config.json``,
    # whose inflated values the loader clamps at load time, a modified checkout is
    # simply run. Provenance does not catch it either: ``install_from_registry``
    # records ``_resolved_clone_commit`` (the tree's real ``HEAD``), and an agent
    # write dirties the worktree without moving ``HEAD``, so a modified tree still
    # reports the pinned SHA.
    #
    # Write-only, NOT ``_SENSITIVE_HOME_DIRS``, and the asymmetry is load-bearing:
    # app source carries no secret and is legitimately READ all the time — the
    # dashboard file viewer lists ``app-sources`` as a browsable root
    # (``apps.builtins.file_explorer.server``), knowledge indexing walks it, and
    # reading an installed app's code is how anyone debugs one. Classifying it
    # read+write sensitive would break those.
    #
    # Deliberately NOT added to ``_WRITE_PROTECTED_BASH_LEAVES`` below: that
    # matcher blocks on a command NAMING the path, which denies bash reads too.
    # That is harmless for the marker and the two Ops Mission Control files, whose
    # only legitimate readers are Python; it is not harmless here, where reading
    # app source with ``grep``/``cat`` is routine. Shell writes therefore sit on
    # the same footing as ``config.json``'s, with the file-edit tool gate as the
    # enforcement point.
    #
    # The gateway's own installer is unaffected: ``_clone_build_app`` clones,
    # builds and prunes through direct Python/subprocess calls, which are not
    # agent tool calls and never reach ``hooks.on_tool_call``.
    f"{prefix}/app-sources"
    for prefix in _CREW_HOME_PREFIXES
]

# ── kiro-cli agent-spec directory (~/.kiro/agents) ──
# The user-level directory kiro-cli reads its ``--agent <name>`` specs from
# (config.paths.kiro_agents_dir()). Each spec's ``mcpServers.<name>.command``
# is materialised by the MCP-gateway rewriter into a
# ``KIROCREW_MCP_TARGET_<SERVER>`` env value the gateway resolves and EXECS, and
# a stubbed server can be routed to a pooled backend that gatewayd spawns
# OUTSIDE the per-session sandbox, as the user. A prompt-injected agent that
# could WRITE a spec here — under any filename, so the whole DIRECTORY is fenced,
# not one leaf — would plant an attacker-chosen command that the gateway runs
# unsandboxed on the next start and re-arms on every restart. So the agent's
# file-edit tool must not be able to author or modify anything under it.
#
# WRITE-protection, NOT read+write sensitive: Kiro Crew and kiro-cli both
# legitimately READ specs (agent_discovery, session mtime scan, the dashboard MCP
# rows, kiro-cli's own ``--agent`` resolution), so this stays OFF
# ``_SENSITIVE_HOME_DIRS`` and reads are unaffected — only the write side is
# refused. Every INTERNAL writer (agent.rebuild_agent_config,
# apps.bridges._register_agents, the rewriter, the dashboard PUT handlers,
# connections/mint) opens these paths directly with ``os``/``Path`` and does NOT
# route through this gate, so managed-spec generation keeps working; only the
# agent's own file-edit/bash tools hit it.
#
# Kept as a literal (mirroring ``.data-home-ready`` below) to avoid a
# config->security import cycle; a drift guard in the tests pins it to
# ``kiro_agents_dir()``'s tail. The default lives under the real home
# (``~/.kiro/agents``) and is anchored there like every other entry;
# ``KIRO_HOME`` (kiro-cli's own home override, which ``kiro_agents_dir()``
# honours) is re-anchored in ``_home_dir_targets_uncached`` so an instance that
# relocates its agents dir is covered the same way ``KIROCREW_HOME`` re-anchors
# the crew secrets.
_KIRO_AGENTS_DIR = ".kiro/agents"
_WRITE_PROTECTED_HOME_PATHS += [_KIRO_AGENTS_DIR]

# ── Bash-layer protection for write-protected leaves ──
# Leaf files under the crew home that a bash command must not be able to
# CREATE/MODIFY/DELETE. The file-edit tool gate already blocks tool writes to
# these via ``is_sensitive_write_path``; this closes the SHELL path, which the
# sensitive-command regex below otherwise only enforces for
# ``_SENSITIVE_HOME_DIRS``.
#
# We block ANY bash command that NAMES one of these leaves — the same
# verb-INDEPENDENT backstop the sensitive-dir matcher uses — rather than an
# enumerated write-verb allowlist: a narrow allowlist is inherently bypassable
# (quoted redirects ``> "$HOME/.../marker"``, ``cp``, ``python -c "open(...,'w')"``,
# or any novel write verb slip past it). Naming-based blocking incidentally
# denies bash READS of these leaves too, which is harmless: they carry no secret
# (so this is NOT in ``_SENSITIVE_HOME_DIRS`` — file-read tools and
# ``is_sensitive_path`` stay unaffected), and the legitimate readers
# (``kirocrew doctor``, Kiro Crew's own writers) use Python ``os`` calls, not bash.
#
# SCOPE NOTE (please do NOT flag incremental regex gaps as new HIGHs): this
# bash gate is DEFENSE-IN-DEPTH, not the primary control. The primary control
# is the file-edit tool gate (`is_sensitive_write_path`, above).
# Like the credential/sensitive-dir rules, the bash matcher is HOME-ANCHORED and
# shares their intrinsic limits — a `cd <home> && touch <leaf>` bare-relative
# write, or an unusual `${VAR}`/quoting form, can evade the regex exactly as it
# can for `~/.aws/credentials`. Chasing shell-parser completeness here is a
# losing game. Widen this only via the SHARED matcher (so
# credentials benefit too), not with per-leaf special cases.
#
# ONE ENTRY IS EXEMPT from that anchoring limit, and the exemption is about
# severity rather than parser completeness: the alias ownership record's residual
# threat is the DELETION of a user-authored alias by Kiro
# Crew's own trusted writer, so its filename is additionally matched
# anchor-independently as a bare path segment (see
# ``_BARE_TOKEN_PROTECTED_LEAVES`` below). That widening is affordable only
# because the name is globally distinctive; it is NOT a template for the other
# leaves, and the anchoring limit above still describes them.
#
# ``rotation.yaml`` is the first entry, and it meets the bar the scope note sets rather than
# being a special case: it is an INPUT TO AN AUTHORIZATION DECISION, not merely a
# security-relevant setting. An agent that rewrites it to name its own login makes
# ``rotation.authorize_action`` -> ``_definitely_off_shift`` accept a forged shift and execute a
# production write against a teammate's incident tooling — and unlike ``config.json``, whose
# inflated values the loader clamps, nothing downstream neutralizes a forged schedule entry.
# The tool-path gate (``is_sensitive_write_path``) is still the primary control; this closes the
# shell path so a redirect cannot reach what the file-edit tool is already refused. Reads stay allowed on BOTH paths:
# every teammate's instance must read the file to answer "am I on call?", and it holds no
# secret. ``ledger_sync`` converges the file with a direct ``git checkout``, not through this
# gate, so team sync is unaffected.
# Entries are HOME-RELATIVE PATHS under the crew home, not bare filenames: the matcher
# builds ``<home>/<crew-prefix>/<entry>``. `rotation.yaml` therefore carries its
# ``apps/.../data/`` subpath — spelling it as a bare leaf silently matched nothing, which is
# the failure mode where a security addition reads as done and enforces nothing.
#
# The incident INDEX is the second, on the same reasoning one step over: it is what
# ``/incident/action`` reads to decide WHICH signal the autonomy gate is authorizing, so an
# agent that rewrites it can have the gate approve one signal while the sink mutates another.
# Reads stay allowed for the same reason as the schedule — it is the board every instance
# renders, and it holds no secret.
#
# The Connections tool-alias OWNERSHIP RECORD is the third, and it clears the same bar: by
# its module's invariant 2 it is the grant that AUTHORIZES DELETION —
# ``alias_record.load_claimed`` returns the pairs the alias pass may strip from the agent
# spec, and nothing else confers that permission. A shell-planted ``committed`` record can
# name a ``@slug/tool -> alias`` triple the user hand-wrote alongside the fingerprint of the
# spec on disk (readable, so computable), and the next rebuild deletes the user's alias as
# its own emission. Nothing downstream neutralizes the forgery, since the fingerprint check
# reads the same spec the forger does. The tool-path gate above is the primary control; this
# closes the shell path so a redirect (``echo … > ~/.kiro/crew/connections-tool-aliases.json``)
# cannot reach what the file-edit tool is already refused. ``alias_record._write`` writes the
# path directly through ``atomic_write`` in Python, not via bash, so both record writes are
# unaffected. Reads stay allowed on the tool path for the same reason as the two entries
# above: the record holds no secret.
_WRITE_PROTECTED_BASH_LEAVES: tuple[str, ...] = (
    "apps/ops-mission-control/data/rotation.yaml",
    "apps/ops-mission-control/data/incidents/index.json",
    "connections-tool-aliases.json",
    # The settings-seed provenance record, on the same reasoning as the alias record
    # above and paired with its entry in _WRITE_PROTECTED_HOME_PATHS: a forged entry
    # makes Crew's own writer overwrite and then delete a user-authored
    # ``.claude/settings.local.json``. ``seed_provenance`` writes it through
    # ``atomic_write`` in Python, so the record's own writes are unaffected; bash
    # reads are incidentally denied, which is harmless here (no secret, and the only
    # readers are Python).
    "settings_seeds.json",
    # The browse launch config (browser_cli/launch.py), paired with the same leaf
    # in _WRITE_PROTECTED_HOME_PATHS so the file-edit and shell paths agree — a
    # leaf on only one of the two is reachable through the other.
    #
    # Deliberately ANCHORED, not bare-token (see the SCOPE note below). The name
    # is distinctive enough to qualify on that test, but it does not earn the wider
    # blast radius: the
    # agent can already point PLAYWRIGHT_MCP_CONFIG at a file of its own, so this
    # filename is not the grant the way the alias record's is. What the anchored
    # entry removes is the DURABLE form — silently rewriting the config the product
    # installed, which every later browse consumes until the next gateway start
    # re-converges it. The residual ``cd``-relative form is the low-severity case
    # the scope note already accepts on purpose.
    "playwright-cli-config.json",
    # Downloaded model weights, paired with the same entry in
    # _WRITE_PROTECTED_HOME_PATHS so the file-edit and shell paths agree. A directory
    # rather than a leaf: the trailing separator the pattern already accepts makes this
    # cover everything beneath it, which is what the trust decision needs (any file the
    # loader might open, not one filename).
    "models",
)

# ── Anchor-INDEPENDENT leaf matching ──
# Every pattern above (POSIX and Windows alike) is HOME-ANCHORED, so one ``cd``
# defeats all of them: ``cd ~/.kiro/crew && echo forged >
# connections-tool-aliases.json`` names no home, no crew prefix and no
# separator, and reaches the very file the anchored entry exists to fence.
#
# For the alias ownership record that gap is not a residual limit to accept, the
# way it is for credential paths: this filename IS the deletion grant.
# ``alias_record.load_claimed`` returns the ``@slug/tool -> alias`` pairs the
# rebuild may STRIP from the agent spec, and nothing else confers that
# permission — so a forged ``committed`` record makes Kiro Crew's own trusted
# writer delete an alias the user hand-wrote. The invariant is therefore about
# the FILENAME and not about how a command spells the way to it: ANY shell
# command naming this file as a path segment is refused. Anchoring is not part
# of the contract — relative, ``cd``-prefixed, subdir-relative, either
# separator, quoted or not, all refused identically.
#
# The settings-seed provenance record (``acp.seed_provenance``) is here for the
# identical reason, one seam over: an entry in it IS the grant to OVERWRITE and
# then DELETE ``<work_dir>/.claude/settings.local.json``. ``recorded()`` returns
# the ``(size, sha256)`` the re-seed compares the file against, and a match is the
# only thing that moves the writer off its leave-it-alone branch — so an entry
# naming a settings file the USER hand-wrote makes Kiro Crew's own trusted writer
# replace it and unlink it on reset. Same conclusion, same shape: the invariant is
# the filename, not the spelling of the way to it.
#
# SCOPE: bare-token matching is for filenames of THIS kind ONLY — an ownership
# record whose contents ARE a deletion grant, spelled distinctively enough that
# the name occurs nowhere in ordinary command lines (a long hyphenated name, or a
# ``_``-joined one that is not a word anyone types), so the false-positive cost is
# confined to commands that genuinely mean that record. A generic leaf must NEVER
# be added here: unanchored ``index.json`` or ``config.json`` would refuse a large
# fraction of routine commands in any repository. The other write-protected leaves
# are not distinctive at all (their distinguishing part is the ``apps/.../data/``
# subpath) and must stay anchored.
_BARE_TOKEN_PROTECTED_LEAVES: tuple[str, ...] = (
    "connections-tool-aliases.json",
    "settings_seeds.json",
)

# Whisper weight files, matched as a NAME with no anchor, for the same reason as the
# alias record above: the filename IS the grant. `stt.models` verifies a file's sha256
# and then hands its PATH to a native loader that re-opens it by name, so the bytes a
# C++ GGML parser actually consumes are whatever sits at ``ggml-<model>.bin`` at open
# time, not the bytes that were hashed. The ``models`` entry in
# _WRITE_PROTECTED_BASH_LEAVES fences the crew-home spelling of that path and is what
# the file tools go through, but an anchored pattern falls to a single ``cd``:
# ``cd ~/.kiro/crew/models; cp evil.bin ggml-base.bin`` names no home, no crew prefix
# and no separator. Anchoring cannot be part of this contract, so it is not.
#
# A pattern rather than the four catalog filenames, so a model row added to
# ``stt.models.CATALOG`` later is fenced without a second edit here -- a new row is
# exactly the change nobody would think to mirror into this module.
#
# The SCOPE test above is met and the cost is stated rather than assumed: ``ggml-``
# plus ``.bin`` is the whisper.cpp/llama.cpp artifact convention and appears in no
# ordinary command line, but it is deliberately wider than the crew home, so an
# unrelated checkout of someone else's GGML weights cannot be copied or renamed from
# the agent's SHELL either. That is a denial rather than a grant, and the file tools
# are untouched, which is the affordable direction for the trade.
_WHISPER_WEIGHT_NAME = r"ggml-[A-Za-z0-9][A-Za-z0-9._-]*\.bin"

# Regex for bash commands that read sensitive paths.
# Matches: cat, head, tail, less, more, strings, xxd, base64, cp, scp, open,
# awk, od, nl, sed, perl (read verbs that can access file contents via path args)
# followed by a path containing any sensitive dir.
_READ_CMDS = r"(?:cat|head|tail|less|more|strings|xxd|base64|cp|scp|open|vi|vim|nano|code|awk|od|nl|sed|perl)\s"

# Regex for bash commands that WRITE/MODIFY a path argument.  Reads alone were
# not enough: a prompt-injected agent could rewrite the governance trust-root
# (or plant a credential) with a write verb that carries no redirect char and
# is not a read verb — e.g. ``tee ~/.kirocrew/security_policy.json``,
# ``mv evil ~/.kirocrew/profiles/x.json``, ``sed -i ... ~/.aws/credentials``,
# ``dd of=...``, ``truncate``, ``ln -sf``, ``install``, plus archive-extraction
# and VCS-checkout verbs that materialise a file at a destination
# (``tar -xf … -C``, ``unzip -d``, ``git checkout/restore -- <path>``).  This
# list is defense-in-depth; the verb-independent catch-all below is the real
# backstop, so a write verb we forgot is still caught when it names a
# sensitive path as an argument.
# NOTE: ``git`` is narrowed to the verbs that actually MATERIALISE a file —
# a bare ``git`` would over-block read-only inspection (``git log/status/diff/
# show/blame/grep -- <sensitive path>``) that operators run during incident
# triage. The verb-independent catch-all still flags a sensitive-path token
# regardless of git verb, so this only trims false positives.
_WRITE_CMDS = (
    r"(?:tee|mv|dd|truncate|ln|install|sed|chmod|chown|rm|rmdir|touch|mkdir|rsync"
    r"|tar|unzip|gunzip|gzip|cpio|patch"
    r"|git\s+(?:checkout|restore|reset|apply|clean|rm|mv|stash))\s"
)

# Matches python/ruby/perl one-liners that open sensitive paths. Spelled as the
# two ordered pieces the linear verb-anchored check walks (see
# ``_verb_anchored_sensitive_hit``): the interpreter word, then ``open(``
# somewhere later on the same line, then the sensitive path later still.
_SCRIPT_OPEN_INTERP = r"(?:python|ruby|perl)\S*\s"
_SCRIPT_OPEN_CALL = r"open\s*\("
_SCRIPT_OPEN = rf"{_SCRIPT_OPEN_INTERP}.*{_SCRIPT_OPEN_CALL}"

#: Longest command ``is_sensitive_bash_command`` will scan. Longer input is
#: REFUSED, not skipped and not scanned: every matcher below is linear in the
#: subject, so this bound is what turns "linear" into a hard wall-clock ceiling
#: for a gate that runs synchronously on the event loop under a 25 s watchdog
#: (``dashboard.loop_stall_exit_after_secs``). Measured after the linearization:
#: ~10 ms at this size for the pattern pass, ~50 ms for the whole gate. The same
#: number bounds each tool_input string in ``llm_helpers``; a legitimate
#: command this long is a heredoc writing a file, and the tool-input tier
#: already refuses it, so the tiers agree.
MAX_SCANNABLE_COMMAND_CHARS = 20 * 1024

#: Ceiling on a cron SCRIPT BODY the source-body detectors in ``mcp_cron`` will scan.
#: It is a different number from the command ceiling because the two subjects have
#: different legitimate sizes: 20 KiB of shell on one ``Bash`` call is a heredoc, while
#: 20 KiB of cron script is an ordinary script, and a size-keyed refusal there is
#: permanent (re-fired on every tick until someone edits the file). Still a hard
#: ceiling: the full-text detectors are linear, so this bounds their wall time on the
#: event loop. ``mcp_cron._MAX_SCRIPT_SCAN_BYTES`` aliases it so the reader admits
#: exactly what the detectors will scan.
MAX_SCANNABLE_SOURCE_BODY_CHARS = 256 * 1024


def _oversize_refusal(length: int, limit: int) -> str:
    """The pass-0 refusal, in ONE spelling.

    Both entry points refuse above their own ceiling and both must say so the same
    way, because an operator reading the reason is being told which number to compare
    their input against.
    """
    return (
        "Blocked: input is too large to security-scan "
        f"({length} chars > {limit} limit); refused rather than left unscanned"
    )


def _sensitive_path_pattern() -> str:
    """The home-anchored fenced-directory path, as a regex fragment.

    Shared by the compiled alternation and by the linear verb-anchored check so
    the two cannot drift on what "a sensitive path" spells.
    """
    home = re.escape(str(Path.home()))
    tilde = re.escape("~")
    home_var = re.escape("$HOME")
    # Generic home roots so a literal "/home/<user>" or "/Users/<user>" token
    # (not just the running user's resolved home) is anchored too.
    generic_home = r"/home/[^/\s]+|/Users/[^/\s]+"
    home_alts = f"(?:{home}|{tilde}|{home_var}|{generic_home})"
    dirs_pattern = "|".join(re.escape(d) for d in _SENSITIVE_HOME_DIRS)
    # What may TERMINATE a sensitive path token. A path is very often the last thing
    # before a shell metacharacter, and accepting only whitespace, a quote, ``/`` or
    # end-of-string let punctuation defeat the gate outright: ``cd ~/.aws;`` and
    # ``cd ~/.kiro/crew/models;`` were allowed, while the same commands written with
    # ``&&`` were blocked -- for no better reason than that ``&&`` is preceded by a
    # space and ``;`` is not. The asymmetry is the tell; nothing about a semicolon
    # makes the path less named. So the class is every character a shell itself treats
    # as the end of a word. Widening a DENY boundary can only ever deny more, which is
    # the safe direction for this gate, and the rule it enforces is unchanged: naming a
    # fenced path is the signal.
    path_end = r"(?:/|\s|$|['\"]|[;&|()<>,:`])"
    return rf"{home_alts}/(?:{dirs_pattern}){path_end}"


def _build_sensitive_regex() -> re.Pattern[str]:
    """Build a compiled regex matching bash reads OR writes of sensitive paths.

    Matching strategies, OR'd:
      1. a READ verb / WRITE verb / script-open / shell-redirect followed by a
         sensitive path (the original verb-anchored form);
      2. a verb-INDEPENDENT catch-all: a sensitive path appearing ANYWHERE in
         the command as an argument token.  This is the real backstop — a write
         verb the allowlist forgot (or a novel one) is still blocked because the
         destination path is sensitive.  Reading a sensitive path is itself
         already blocked by is_sensitive_path on the file-read title, so flagging
         any command that *names* the trust-root/credential path is correct and
         fail-safe.
      3. a write-protected LEAF under the crew home, in POSIX and in
         Windows-native spelling, matched verb-independently;
      4. an anchor-INDEPENDENT bare path SEGMENT for the distinctive leaves in
         ``_BARE_TOKEN_PROTECTED_LEAVES``, and for a whisper weight filename
         (``_WHISPER_WEIGHT_NAME``) — the only strategy that survives a ``cd``
         into the crew home followed by a relative filename.
    The home anchor accepts ``~`` / ``$HOME`` / the literal ``Path.home()`` AND a
    generic ``/home/<user>`` / ``/Users/<user>`` literal so an unexpanded
    ``/home/$USER/...`` or another user's literal path is still caught.
    """
    home = re.escape(str(Path.home()))
    tilde = re.escape("~")
    home_var = re.escape("$HOME")
    # Generic home roots so a literal "/home/<user>" or "/Users/<user>" token
    # (not just the running user's resolved home) is anchored too.
    generic_home = r"/home/[^/\s]+|/Users/[^/\s]+"
    home_alts = f"(?:{home}|{tilde}|{home_var}|{generic_home})"
    # The terminator class and the fenced-dir path itself live in
    # ``_sensitive_path_pattern`` (shared with the linear verb-anchored check);
    # its docstring carries the rationale for the terminator class.
    path_end = r"(?:/|\s|$|['\"]|[;&|()<>,:`])"
    sensitive_path = _sensitive_path_pattern()
    # Write-protected leaves (e.g. the on-call schedule): a full home-anchored
    # path to a specific leaf file, matched verb-INDEPENDENTLY (below) so no
    # write form can bypass it. See _WRITE_PROTECTED_BASH_LEAVES for why reads
    # are blocked too (harmless: no secret; legitimate readers use Python).
    wp_prefixes = "|".join(re.escape(p) for p in _CREW_HOME_PREFIXES)
    wp_leaves = "|".join(re.escape(leaf) for leaf in _WRITE_PROTECTED_BASH_LEAVES)
    write_protected_path = (
        # trailing ``/`` is included so a ``mkdir -p <home>/<crew-prefix>/<leaf>/x``
        # (which also MATERIALISES the leaf as a directory) is caught, not just
        # the exact-leaf forms.
        rf"{home_alts}/(?:{wp_prefixes})/(?:{wp_leaves}){path_end}"
    )
    # Publish artifacts of a keystone leaf, mirroring the tool-path clause in
    # ``_is_keystone_publish_artifact``. Required, not optional: "protected on one path
    # only is not protected" is stated three times in this module, and the leaf's temp
    # holds the leaf's own bytes.
    #
    # Why ``sensitive_path`` above does not already catch these: ``path_end`` is the class
    # of characters a SHELL treats as the end of a word, and ``.`` is deliberately not in
    # it, so the literal leaf name followed by ``.tmp`` never satisfies the terminator.
    # Matched on the artifact SHAPE rather than a leaf-derived name because the real
    # mkstemp form (``tmpXXXXXXXX.tmp``) contains no leaf name at all.
    #
    # The filename run excludes ``/`` so this stays exactly one level deep -- a direct
    # child of a keystone leaf's own parent, matching the equality test the tool path
    # makes on the parent directory.
    #
    # The tail is a name-character LOOKAHEAD, not one of the enumerated terminator
    # classes, and the separator before the filename is the generalized ``gsep`` that
    # absorbs canonical no-op chains (``/./``, ``/x/../``). Both follow the
    # ``bare_protected_path`` branch further down, whose comment states the reasoning:
    # excluding name characters after the match keeps a DIFFERENT file out
    # (``tmpAB.tmpx`` stays allowed) while a trailing ``.``, ``$``, metacharacter or
    # separator is still a match. An enumerated class has to name every spelling a shell
    # or filesystem treats as equivalent, and review found three it had missed in
    # succession -- a metacharacter, an expanded-away ``$var``, and a ``/./`` segment.
    # The lookahead closes that whole family instead of the members discovered so far,
    # which is why this branch does not reuse ``path_end`` / ``win_path_end``.
    artifact_parents_pattern = "|".join(re.escape(d) for d in _KEYSTONE_ARTIFACT_PARENTS)
    artifact_suffix_alt = "|".join(
        re.escape(suffix.lstrip(".")) for suffix in _KEYSTONE_ARTIFACT_SUFFIXES
    )
    artifact_path = (
        rf"{home_alts}/(?:{artifact_parents_pattern})"
        rf"/[^/\s'\"]*\.(?:{artifact_suffix_alt})(?![\w-])"
    )
    # Windows-native spellings of the same fenced dirs, matched in the RAW
    # command text. POSIX shlex consumes unquoted backslashes during
    # tokenization, and an embedded interpreter script
    # (``python -c "open(r'C:\\Users\\u\\.aws\\credentials')"``) never
    # tokenizes into a path at all — so this raw pass, which already catches
    # embedded scripts for the POSIX spellings above, is the only layer that
    # can see a native spelling. Anchors: the resolved home literal (on
    # Windows it contains backslashes), a generic drive-letter home with
    # either separator, a UNC prefix, ``%USERPROFILE%``, and ``~``/``$HOME``.
    # Entry-internal separators accept both slashes; naming a fenced dir is
    # itself the signal (same fail-safe posture as the branches above), so
    # over-matching an odd mixed-separator spelling is the safe direction.
    win_sep = r"[\\/]"
    # Win32 collapses a repeated separator run, so ``kiro-cli`` and
    # ``\\kiro-cli`` and ``//kiro-cli`` name the same entry. Matching only the
    # single-separator spelling was a bypass of every store branch at once
    # (#6350): a doubled separator at an inter-segment boundary named the fenced
    # path while matching no branch.
    #
    # The patterns below deliberately still spell ONE separator, and the run is
    # collapsed in the SUBJECT instead -- once, linearly, in
    # ``is_sensitive_bash_command`` via ``_collapse_separator_runs``.
    #
    # Admitting a run in the PATTERNS (``{win_sep}+``) was tried first and is a
    # denial-of-service on this very gate: the run appears inside the starred
    # generalized separator below and again after it, so a long run can be split
    # between them many ways and the engine consumes the whole run at every start
    # offset. Measured, 6,000 backslashes in one command: 33s against 1.3s on
    # base, past the gateway's 25s watchdog (found in review). Making the run
    # maximal with a lookahead only halved it, and capping it at 64 bought speed
    # by letting a 65-separator spelling escape the fence outright -- trading a
    # hang for a bypass. Collapsing the subject is complete for any run length
    # and leaves every pattern here exactly as tight as it already was.
    # Generalized separator: a plain separator, optionally preceded by any
    # chain of canonical no-ops — single-dot segments (``\.``) and same-level
    # down-up excursions (``\X\..``). This is what makes traversal spellings
    # that re-enter the same location match
    # (``AppData\Roaming\..\Roaming\kiro-cli``: the excursion consumes
    # ``Roaming\..`` and the literal segment matches the re-entry). A
    # multi-level ``..`` chain can over-match a path that actually ends
    # elsewhere — the safe direction for this gate, which blocks on naming
    # alone. The name run is length-capped to bound backtracking.
    win_gsep = rf"(?:{win_sep}(?:\.|[^\\/\s'\"]{{1,64}}{win_sep}\.\.))*{win_sep}"
    # Shell word-end terminator for the Windows-native branches below. Mirrors the POSIX
    # ``path_end`` above, INCLUDING the shell metacharacters, and for the reason stated
    # there: the class is every character a shell itself treats as the end of a word, and
    # widening a DENY boundary can only ever deny more, which is the safe direction for a
    # gate that blocks on naming alone.
    #
    # Until this existed each Windows branch spelled its own ``(?:sep|space|$|quote)``,
    # which a metacharacter walked straight through -- ``type <fenced path>&whoami`` named
    # the file and was not matched, while the POSIX spelling of the same command was. Found
    # by the GPT review lane on the artifact branch; applied to the whole family, because a
    # fence that is tight on an atomic-write temp and loose on the keystone leaf beside it
    # protects the transient copy and not the secret.
    # ``$`` is a literal member of the class, not the regex end-anchor that appears
    # earlier in the alternation: PowerShell (and cmd.exe with ``$env:``) EXPANDS a
    # variable reference, so ``Get-Content <fenced path>$null`` removes the ``$null`` and
    # reads the fenced file, while the matcher saw an unterminated path and allowed it.
    # A literal ``$`` therefore ends a path for matching purposes. The POSIX side is
    # already covered here by its own branches -- measured, not assumed -- so this is
    # deliberately a Windows-only addition rather than a change to ``path_end``.
    # ``.`` is deliberately NOT a member, though Windows does strip a trailing dot when
    # opening a file. Adding it here refused ``ls -d ~/.kiro/crew/backup.tar``: these
    # branches accept forward slashes too, so they also govern POSIX spellings, and
    # ``backup`` is a fenced DIRECTORY leaf whose name prefixes unrelated filenames. A
    # terminator sitting after a directory name cannot tell the alias ``backup.`` from the
    # different file ``backup.tar``, and refusing the latter regressed the read-only
    # listing that #6021 exists to allow. The artifact branches solve their own version of
    # this with a name-character LOOKAHEAD instead, which is anchored at the end of a
    # complete filename and so can make the distinction. The leaf branches' trailing-dot
    # alias is therefore left open here rather than closed with a rule that costs a
    # legitimate read.
    win_path_end = rf"(?:{win_sep}|\s|$|['\"]|[;&|()<>,:`$])"
    win_dirs_pattern = "|".join(
        win_gsep.join(re.escape(part) for part in d.split("/"))
        for d in _SENSITIVE_HOME_DIRS
    )
    generic_win_home = rf"[A-Za-z]:{win_sep}(?:Users|home){win_sep}[^\\/\s'\"]+"
    unc_prefix = r"\\\\[^\s'\"]+"
    # cmd.exe and PowerShell spellings of the profile variable both anchor a
    # home-relative fenced path. The cmd.exe form tolerates expansion
    # modifiers (``%USERPROFILE:~0%``, ``%USERPROFILE:a=b%``), and the
    # PowerShell form is accepted braced (``${env:USERPROFILE}``) or bare —
    # all expand to the same location. HOMEDRIVE+HOMEPATH concatenated (either
    # shell's spelling) is the same home by definition.
    userprofile = (
        r"(?:%USERPROFILE(?::[^%\s]*)?%"
        # cmd.exe delayed expansion (`cmd /V:ON`) names the same home as the `%…%`
        # form, exactly as it does for `%APPDATA%` below. Without it every
        # home-anchored branch here missed `!USERPROFILE!\.kiro\crew\…`.
        r"|!USERPROFILE(?::[^!\s]*)?!"
        rf"|{re.escape('$env:USERPROFILE')}"
        rf"|{re.escape('${env:USERPROFILE}')}"
        r"|%HOMEDRIVE(?::[^%\s]*)?%%HOMEPATH(?::[^%\s]*)?%"
        r"|!HOMEDRIVE(?::[^!\s]*)?!!HOMEPATH(?::[^!\s]*)?!"
        rf"|{re.escape('$env:HOMEDRIVE$env:HOMEPATH')}"
        rf"|{re.escape('${env:HOMEDRIVE}${env:HOMEPATH}')})"
    )
    win_home_alts = (
        f"(?:{home}|{generic_win_home}|{userprofile}"
        f"|{tilde}|{home_var})"
    )
    # Between the anchor and the fenced remainder, accept the same
    # canonical-no-op chains (``\.\``, ``\X\..\``): they are equivalent to a
    # plain separator, so ``%APPDATA%\.\kiro-cli\data.sqlite3`` and
    # ``...\AppData\Roaming\..\Roaming\kiro-cli\...`` still name the store.
    #
    # The UNC anchor is the one anchor that takes a PLAIN separator instead.
    # Its ``[^\s'\"]+`` already absorbs every character a no-op chain can
    # contain (separators, dots, name characters), so ``unc_prefix win_gsep``
    # and ``unc_prefix win_sep`` match exactly the same strings -- and only
    # the second is linear. Combined, the greedy run backtracks one character
    # at a time and re-walks the whole ``\X\..`` chain from every separator it
    # lands on: a single 10 KB UNC token cost 0.3 s, 40 KB cost 5 s (measured),
    # quadratic in the token. With a plain separator each backtrack step is a
    # constant-time literal check.
    win_anchor = rf"(?:{win_home_alts}{win_gsep}|{unc_prefix}{win_sep})"
    win_sensitive_path = rf"{win_anchor}(?:{win_dirs_pattern}){win_path_end}"
    # Windows-native spelling of the publish artifacts above. The pairing invariant
    # applies to this spelling too, not only to POSIX-versus-tool: a native path is the
    # one form the tokenizing passes cannot see, so leaving it out would fence the temp
    # everywhere except in an embedded-script literal.
    win_artifact_parents_pattern = "|".join(
        win_gsep.join(re.escape(part) for part in d.split("/"))
        for d in _KEYSTONE_ARTIFACT_PARENTS
    )
    win_artifact_path = (
        rf"{win_anchor}(?:{win_artifact_parents_pattern})"
        rf"{win_gsep}[^\\/\s'\"]*\.(?:{artifact_suffix_alt})(?![\w-])"
    )
    # ``%APPDATA%`` already points INTO ``AppData\Roaming``, so a spelling like
    # ``%APPDATA%\kiro-cli\data.sqlite3`` names a fenced store WITHOUT the
    # ``AppData\Roaming`` text the branch above anchors on. Map the variable
    # directly onto that prefix: entries under ``AppData/Roaming/`` are matched
    # by their remainder.
    appdata_var = (
        r"(?:%APPDATA(?::[^%\s]*)?%"
        # cmd.exe delayed expansion (`cmd /V:ON`): `!APPDATA!` names the same
        # location as `%APPDATA%`, with the same expansion modifiers.
        r"|!APPDATA(?::[^!\s]*)?!"
        rf"|{re.escape('$env:APPDATA')}"
        rf"|{re.escape('${env:APPDATA}')})"
    )
    appdata_remainders = "|".join(
        win_gsep.join(re.escape(part) for part in d.split("/")[2:])
        for d in _SENSITIVE_HOME_DIRS
        if d.startswith("AppData/Roaming/")
    )
    # ``%APPDATA%`` ends in ``Roaming`` by definition, so ``\..\Roaming``
    # right after it is a canonical no-op specific to this anchor.
    appdata_sensitive_path = (
        rf"{appdata_var}(?:{win_sep}\.\.{win_sep}Roaming)*"
        rf"{win_gsep}(?:{appdata_remainders}){win_path_end}"
    )
    # ``%LOCALAPPDATA%`` is the same shape one directory over: it points INTO
    # ``AppData\Local``, so ``%LOCALAPPDATA%\kiro-cli\data.sqlite3`` names a
    # fenced store without the ``AppData\Local`` text the home-anchored branch
    # requires. Without this branch the shell tier would not cover the very
    # spelling that names the CURRENT kiro-cli store on Windows, while the
    # tuple in ``kiro_usage_api._CLI_SQLITE_DBS`` treats that store as a trust
    # anchor — the fence the trust claim rests on must hold at this tier too.
    localappdata_var = (
        r"(?:%LOCALAPPDATA(?::[^%\s]*)?%"
        # cmd.exe delayed expansion (`cmd /V:ON`): `!LOCALAPPDATA!` names the
        # same location as `%LOCALAPPDATA%`, with the same expansion modifiers.
        r"|!LOCALAPPDATA(?::[^!\s]*)?!"
        rf"|{re.escape('$env:LOCALAPPDATA')}"
        rf"|{re.escape('${env:LOCALAPPDATA}')})"
    )
    localappdata_remainders = "|".join(
        win_gsep.join(re.escape(part) for part in d.split("/")[2:])
        for d in _SENSITIVE_HOME_DIRS
        if d.startswith("AppData/Local/")
    )
    # ``%LOCALAPPDATA%`` ends in ``Local`` by definition, so ``\..\Local``
    # right after it is this anchor's canonical no-op.
    localappdata_sensitive_path = (
        rf"{localappdata_var}(?:{win_sep}\.\.{win_sep}Local)*"
        rf"{win_gsep}(?:{localappdata_remainders}){win_path_end}"
    )
    # Windows-native spelling of the write-protected leaves. The POSIX leaf
    # branch above anchors on ``/`` separators, so on Windows the resolved home
    # literal (``C:\Users\u``) never matches it and ``echo forged >
    # C:\Users\u\.kiro\crew\connections-tool-aliases.json`` reaches the very file
    # the leaf list exists to fence — the same bypass the fenced DIRS already
    # close through ``win_sensitive_path``. Built from the same anchors and
    # generalized separator, so both spellings of every leaf are gated
    # identically and a leaf added to the tuple is covered in both.
    win_wp_prefixes = "|".join(
        win_gsep.join(re.escape(part) for part in p.split("/"))
        for p in _CREW_HOME_PREFIXES
    )
    win_wp_leaves = "|".join(
        win_gsep.join(re.escape(part) for part in leaf.split("/"))
        for leaf in _WRITE_PROTECTED_BASH_LEAVES
    )
    win_write_protected_path = (
        rf"{win_anchor}(?:{win_wp_prefixes}){win_gsep}"
        rf"(?:{win_wp_leaves}){win_path_end}"
    )
    # A native spelling whose LEAF is an expansion: ``%USERPROFILE%\.kiro\crew\%F%``
    # names the keystone without spelling any of its literal leaves, so no branch
    # above can match it. This branch is now the ONLY cover for that shape: the
    # token-level rule that used to back it up resolved variables by simulating the
    # shell and went with the rest of the traversal simulation, and it only ever
    # caught the QUOTED spelling anyway -- POSIX shlex consumes unquoted backslashes
    # before a token rule runs, which is the same blind spot every branch here
    # exists to cover.
    #
    # Anchored on the keystone's PARENT directory, because the leaf being a variable
    # means there is no literal leaf to match: naming the parent, a separator, and
    # any expansion is the signal. Scoped to the crew trust root and NOT to every
    # entry in `_SENSITIVE_LEAF_PARENT_DIRS`, since that list also holds
    # ``AppData/Roaming`` and ``Library/Application Support`` -- directories whose
    # variable-leaf spellings (``%APPDATA%\%APP%``) are ordinary and constant.
    win_crew_leaf_parents = "|".join(
        win_gsep.join(re.escape(part) for part in d.split("/"))
        for d in _SENSITIVE_LEAF_PARENT_DIRS
        if any(d == p or d.startswith(f"{p}/") for p in _CREW_HOME_PREFIXES)
    )
    # Every spelling of "something that is computed at run time", because the LEAF
    # being computed is what this branch exists to catch: the value cannot be read from
    # the command text, so the only safe reading is that it might name a keystone file.
    #
    # The substitution forms are not optional. Without them
    # ``…\.kiro\crew\$(Write-Output security_policy.json)`` read the governance
    # policy: the token-level rule catches that shape through `_SHELL_SUBST_RE`, but
    # only when the path is QUOTED, and this branch exists precisely for the unquoted
    # spellings POSIX shlex destroys before any token rule runs.
    #
    # The bracketing forms match their OPENER and do not describe a body, which is the
    # difference between a deny gate and a parser. This question is only ever "does an
    # unresolved expansion start here", and any answer that has to model the contents
    # can be out-nested: a body permitting one level missed
    # ``$(a $(b $(c)))``, and `${[^}\s]+}` missed ``${My Var}`` because a PowerShell
    # variable name may legally contain a space. Matching the opener cannot be
    # out-nested, and it can only ever deny MORE -- which for the keystone directory
    # costs nothing, since a resolvable leaf there is fenced by name anyway.
    #
    # The delimited forms below keep their closers on purpose: an unterminated ``%``,
    # ``!`` or backtick is a LITERAL to cmd, PowerShell and sh respectively, so it
    # names no expansion and matching it would refuse ordinary filenames.
    any_expansion = (
        r"(?:%[A-Za-z_][A-Za-z0-9_]*(?::[^%\s]*)?%"
        r"|![A-Za-z_][A-Za-z0-9_]*(?::[^!\s]*)?!"
        rf"|{re.escape('$')}\{{?env:[A-Za-z_][A-Za-z0-9_]*\}}?"
        # PowerShell subexpression / POSIX command substitution, PowerShell's
        # array-subexpression sibling, and the brace-delimited variable form.
        r"|\$\{"
        r"|\$\("
        r"|@\("
        # POSIX backtick substitution.
        r"|`[^`]*`"
        r"|\$[A-Za-z_][A-Za-z0-9_]*)"
    )
    win_crew_var_leaf_path = (
        rf"{win_anchor}(?:{win_crew_leaf_parents})"
        rf"{win_sep}{any_expansion}"
    )
    # ── ~/.kiro/agents WRITE-protection (a whole DIRECTORY, not a leaf) ──
    # A spec under this dir becomes a KIROCREW_MCP_TARGET_<SERVER> command the
    # gateway execs — pooled backends run OUTSIDE the per-session sandbox — so an
    # agent-planted spec is a persistent, unsandboxed command run as the user. The
    # tool-path gate (``is_sensitive_write_path``) is the primary control and
    # keeps READS allowed there (the dir is on the write-only tier, not in
    # ``_SENSITIVE_HOME_DIRS``); this branch closes the shell write path.
    #
    # Matched verb-INDEPENDENTLY, exactly like the sensitive dirs and the
    # write-protected leaves — NOT with a write-verb allowlist. An enumerated verb
    # set is inherently bypassable: ``curl -o`` / ``wget -O`` output-file writers,
    # ``python -c "open(...,'w')"``, ``dd``, ``install`` or any novel write verb
    # slip past it (found in review). Naming the dir is the signal. This
    # incidentally blocks bash READS of the dir too, which is harmless for the same
    # reason it is for the write-protected leaves: a spec carries no secret and
    # every legitimate reader (the rewriter, agent_discovery, kiro-cli itself) uses
    # Python/direct file I/O, not bash. Tool-path reads (file viewer, knowledge
    # indexing, ``is_sensitive_path``) are unaffected. The trailing class matches
    # the dir itself and anything beneath it.
    #
    # Anchored on the home forms AND on a literal ``$KIRO_HOME`` reference:
    # ``KIRO_HOME`` (honoured by ``kiro_agents_dir()``) relocates the dir to
    # ``$KIRO_HOME/agents``, so ``tee $KIRO_HOME/agents/x`` must be caught too
    # (found in review). An already-expanded absolute override path carries no
    # anchor and is the accepted residual — the same limit the crew leaves have —
    # but the tool gate resolves and covers it. ``_KIRO_HOME_LEAF`` is the segment
    # under the override (``agents``), sliced from the same literal so the two
    # spellings cannot drift.
    _KIRO_HOME_LEAF = _KIRO_AGENTS_DIR.split("/", 1)[1]
    agents_dir_alt = re.escape(_KIRO_AGENTS_DIR)
    agents_leaf_alt = re.escape(_KIRO_HOME_LEAF)
    kiro_home_var = r"(?:\$KIRO_HOME|\$\{KIRO_HOME\})"
    agents_write_path = (
        rf"(?:{home_alts}/(?:{agents_dir_alt})"
        rf"|{kiro_home_var}/(?:{agents_leaf_alt})){path_end}"
    )
    win_agents_dir_alt = win_gsep.join(
        re.escape(part) for part in _KIRO_AGENTS_DIR.split("/")
    )
    # cmd.exe ``%KIRO_HOME%`` (with expansion modifiers) and the two PowerShell
    # spellings, mirroring ``userprofile``/``appdata_var`` above.
    win_kiro_home_var = (
        r"(?:%KIRO_HOME(?::[^%\s]*)?%"
        rf"|{re.escape('$env:KIRO_HOME')}"
        rf"|{re.escape('${env:KIRO_HOME}')})"
    )
    win_agents_write_path = (
        rf"(?:{win_anchor}(?:{win_agents_dir_alt})"
        rf"|{win_kiro_home_var}{win_gsep}(?:{agents_leaf_alt})){win_path_end}"
    )
    # Bare path-SEGMENT match for the globally distinctive leaves. Both branches
    # above require a home anchor and a crew prefix, so both are defeated by a
    # single ``cd``; this one requires neither, which is the whole point — the
    # filename authorizes deletion, so naming it is the signal regardless of how
    # the command spells the way there.
    #
    # The boundary is expressed as filename-character NEGATIVES rather than the
    # ``[\s'\"=:,;]`` token anchor the branches above use, because a bare
    # relative spelling is normally preceded by a path SEPARATOR (``./name``,
    # ``.\name``, ``sub/name``) or by a redirect operator with no intervening
    # space (``>name``) — none of which that class admits. Excluding
    # alphanumerics, ``_``, ``-`` and ``.`` before the name keeps a DIFFERENT
    # file whose name merely ends with this one out (``my-connections-tool-
    # aliases.json`` stays allowed); excluding name characters after it keeps
    # ``…jsonx`` out. A trailing ``.`` or separator is deliberately still a
    # match, so a suffixed spelling (``…json.tmp``) and the mkdir-as-directory
    # form are covered — over-matching is the safe direction for a gate that
    # blocks on naming alone.
    bare_leaves = "|".join(re.escape(leaf) for leaf in _BARE_TOKEN_PROTECTED_LEAVES)
    bare_protected_path = rf"(?<![\w.\-])(?:{bare_leaves})(?![\w\-])"
    # Same token boundaries, and for the same reasons: the lookbehind keeps a name that
    # merely ENDS with one of these out (``my-ggml-base.bin`` stays allowed), while a
    # trailing ``.`` or separator still matches, so ``ggml-base.bin.tmp`` and the
    # mkdir-as-directory form are covered.
    bare_weight_path = rf"(?<![\w.\-]){_WHISPER_WEIGHT_NAME}(?![\w\-])"
    return re.compile(
        # (1) redirect-anchored, OR (2) verb-independent: the sensitive path
        # appears anywhere as a token.  The token anchor accepts start-of-string
        # plus the separators that precede a path argument: whitespace, quote,
        # ``=`` (VAR=path), AND ``:``/``,``/``;`` (option:path, PATH-style
        # colon lists, comma/semicolon-joined args) — without the latter a
        # ``FOO=bar:~/.aws/credentials`` or ``PATH=/x:~/.ssh/id_rsa`` token slips
        # past the backstop while no verb branch fires either.
        #
        # The anchor is written ``(?:^|[\s'\"=:,;])`` with NO leading ``.*``: this
        # pattern is only ever used via ``.search`` (see ``_get_sensitive_re``
        # callers), which already retries at every offset, so a leading ``.*``
        # matched nothing extra while making the scan quadratic in the longest
        # line. Note ``\n`` is in the class, so a path at the start of a later
        # line still matches even though ``.`` never crossed a newline anyway.
        # Do NOT reintroduce ``.*`` here.
        #
        # The redirect form ``[<>|]\s*<path>`` is spelled the same way, for the
        # same reason: it used to read ``.*[<>|]\s*``, and THAT ``.*`` is tried
        # at every offset of every line regardless of content -- the one
        # construct in this alternation whose cost did not depend on the input
        # looking like a path at all. Measured on a 10 KB command with no
        # redirect in it: 0.3 s; 40 KB: 5 s; quadratic. Under ``.search`` the
        # leading ``.*`` is redundant exactly as it is on the token anchor.
        #
        # The VERB-anchored form (``cat .*<path>``, ``tee .*<path>``,
        # ``python … open( … <path>``) is deliberately NOT in this alternation
        # any more: ``verb.*path`` re-walks the rest of the line from every verb
        # occurrence, quadratic on a verb-dense line, and no regex spelling of
        # "a verb somewhere earlier on this line" is linear. It lives in
        # ``_verb_anchored_sensitive_hit``, which walks each line once. Both run
        # from ``_sensitive_pattern_hit``; neither is a complete gate alone.
        # (3) write-protected leaf: matched verb-INDEPENDENTLY too (same token
        # anchor), so a quoted redirect (``> "$HOME/.../marker"``), ``cp``,
        # ``python -c "open(...,'w')"`` or any novel write verb is still caught.
        rf"(?:[<>|]\s*{sensitive_path}"
        rf"|(?:^|[\s'\"=:,;]){sensitive_path}"
        rf"|(?:^|[\s'\"=:,;]){write_protected_path}"
        # (3b) publish artifacts of a keystone leaf -- the atomic-write temp and the lock
        # sibling -- in both the POSIX and the Windows-native spelling. Verb-independent
        # like (2)/(3): naming the artifact is the signal, so a redirect, a ``cp``, or an
        # embedded ``open(...,'w')`` is caught without enumerating write verbs.
        rf"|(?:^|[\s'\"=:,;]){artifact_path}"
        # (4) Windows-native spelling, verb-independent (same token anchor):
        # covers quoted backslash paths AND embedded-script literals that the
        # tokenizing passes cannot see. (5) the %APPDATA% / %LOCALAPPDATA%
        # aliases of the fenced Roaming/Local stores. (6) the write-protected
        # leaves in that same native spelling, which branch (3) cannot see.
        # (7) the distinctive leaves as a bare path SEGMENT, with no anchor at
        # all, because branches (3) and (6) both fall to a ``cd`` plus a
        # relative name.
        rf"|(?:^|[\s'\"=:,;]){win_sensitive_path}"
        rf"|(?:^|[\s'\"=:,;]){win_artifact_path}"
        rf"|(?:^|[\s'\"=:,;]){appdata_sensitive_path}"
        rf"|(?:^|[\s'\"=:,;]){localappdata_sensitive_path}"
        rf"|(?:^|[\s'\"=:,;]){win_write_protected_path}"
        rf"|(?:^|[\s'\"=:,;]){win_crew_var_leaf_path}"
        # (8) ~/.kiro/agents (POSIX and Windows-native spelling, plus the
        # ``$KIRO_HOME`` override), matched verb-INDEPENDENTLY with the same token
        # anchor as (2)/(3): naming the dir is the signal, so ``curl -o``/``wget
        # -O`` output-file writers, ``python -c "open(...,'w')"`` and any novel
        # write verb are caught, not just an enumerated allowlist. Bash reads of
        # the dir are blocked incidentally (harmless — no secret, Python readers
        # only); tool-path reads stay allowed.
        rf"|(?:^|[\s'\"=:,;]){agents_write_path}"
        rf"|(?:^|[\s'\"=:,;]){win_agents_write_path}"
        # (10) whisper weight FILENAMES, also with no anchor, because the digest the
        # model store checks only binds the bytes if the name it then loads cannot be
        # rewritten by a ``cd``-relative command.
        rf"|{bare_protected_path}"
        rf"|{bare_weight_path})",
        re.IGNORECASE,
    )


_SENSITIVE_RE: re.Pattern[str] | None = None


def _get_sensitive_re() -> re.Pattern[str]:
    global _SENSITIVE_RE
    if _SENSITIVE_RE is None:
        _SENSITIVE_RE = _build_sensitive_regex()
    return _SENSITIVE_RE


# ── The verb-anchored form of the sensitive-path match, walked linearly ──
#
# ``(?:READ|WRITE)\s.*<path>`` and ``interp\S*\s.*open\s*\(.*<path>`` say "a
# verb, and later on the same line a fenced path". The regex engine evaluates
# that by running ``.*`` to the end of the line from EVERY verb occurrence and
# retrying the path at every position on the way back, so a line with k verbs
# costs k times its length: quadratic on ``cat a cat b cat c …`` (0.13 s at
# 40 KB, measured, and it is the only super-linear term left after the
# redirect and UNC rewrites). Because ``.`` never crosses a newline, the
# statement decomposes per line into "the earliest end of a verb match" and
# "any path match starting at or after it" -- two ``search`` calls, each
# linear, and exactly the language the old branch accepted:
#
# * for the verb alternations every alternative is a word followed by ``\s``,
#   so a match ends at the whitespace after its word and the LEFTMOST match
#   has the earliest end (a later start inside the same word shares that
#   whitespace; a later start past it ends later);
# * for the interpreter form the same holds for ``\S*\s`` (ends at the first
#   whitespace after the start) and for ``open\s*\(`` (ends at its ``(``), and
#   the leftmost ``open(`` at or after the interpreter's end is the earliest;
# * the path is searched from that end with ``pos``: ``$`` in its terminator
#   class matches at the line's end exactly as it matched before ``\n`` (which
#   is also in the class) or at end of string.
#
# ``_SENSITIVE_RE`` no longer carries this branch; ``_sensitive_pattern_hit``
# runs both and is what the gate calls. Neither half is a complete gate alone.
_VERB_ANCHOR_RE: re.Pattern[str] | None = None
_SCRIPT_OPEN_INTERP_RE = re.compile(_SCRIPT_OPEN_INTERP, re.IGNORECASE)
_SCRIPT_OPEN_CALL_RE = re.compile(_SCRIPT_OPEN_CALL, re.IGNORECASE)
_SENSITIVE_PATH_RE: re.Pattern[str] | None = None


def _get_verb_anchor_re() -> re.Pattern[str]:
    global _VERB_ANCHOR_RE
    if _VERB_ANCHOR_RE is None:
        _VERB_ANCHOR_RE = re.compile(rf"(?:{_READ_CMDS}|{_WRITE_CMDS})", re.IGNORECASE)
    return _VERB_ANCHOR_RE


def _get_sensitive_path_re() -> re.Pattern[str]:
    global _SENSITIVE_PATH_RE
    if _SENSITIVE_PATH_RE is None:
        _SENSITIVE_PATH_RE = re.compile(_sensitive_path_pattern(), re.IGNORECASE)
    return _SENSITIVE_PATH_RE


def _verb_anchored_sensitive_hit(command: str) -> bool:
    """True iff some line has a read/write verb, or ``interp … open(``, and a
    fenced path starting at or after it -- the verb-anchored branch, in linear
    time (see the block comment above for why this is the same language)."""
    verb_re = _get_verb_anchor_re()
    path_re = _get_sensitive_path_re()
    for line in command.split("\n"):
        verb = verb_re.search(line)
        if verb is not None and path_re.search(line, verb.end()) is not None:
            return True
        interp = _SCRIPT_OPEN_INTERP_RE.search(line)
        if interp is None:
            continue
        call = _SCRIPT_OPEN_CALL_RE.search(line, interp.end())
        if call is not None and path_re.search(line, call.end()) is not None:
            return True
    return False


def _sensitive_pattern_hit(command: str) -> bool:
    """The pattern tier of the shell gate: the compiled alternation plus the
    linear verb-anchored branch. Every caller of the old single ``search`` goes
    through here so the two halves cannot be applied to different subjects."""
    return bool(_get_sensitive_re().search(command)) or _verb_anchored_sensitive_hit(command)


# ── Bounded symlink resolution for the sensitive-path gates ──
#
# ``os.path.realpath`` / ``Path.resolve`` ``lstat`` every component of the path
# they are handed.  The path gates hand them AGENT-SUPPLIED tokens -- including
# tokens that name nothing on this host at all, like the remote side of
# ``ssh host 'cd /home/user/ws && ...'`` -- and a component that lands on a
# stalled automount (macOS ``/home`` is an autofs map resolved through
# opendirectoryd; a dead NFS/SSHFS mount; a disconnected mapped drive) blocks in
# the kernel for as long as the mount does.  No exception is raised, so the
# ``except OSError`` around the call never fired: the call simply never
# returned.  Because :func:`is_sensitive_path` / :func:`is_sensitive_bash_command`
# run synchronously inside ``on_tool_call`` on the event loop, that was a loop
# wedge and the stall watchdog's dump-then-exit -- ten identical crash dumps on
# a corp macOS during a VPN transition, the loop parked in ``_joinrealpath`` for
# the full watchdog budget.  Widening the budget only moved the crash.
#
# So resolution runs on its own tiny pool and the caller waits a BOUNDED time.
# A timeout is NOT treated like the ``OSError`` fallback (lexical forms only):
# that would make the degraded state a lever -- stall one token under a wedged
# mount and, for the cooldown, a workspace symlink into a credential store
# would pass on its lexical spelling.  A path whose canonical form cannot be
# established is instead REFUSED (:class:`PathResolutionStalled`, fail-closed
# in every gate), the same posture the rest of this module takes when a proof
# is missing.  The cost is a false refusal of paths under a wedged mount for
# the cooldown window -- the ``ssh`` command above is refused for 30s during a
# VPN transition instead of killing the gateway -- and the refusal names why.
#
# A timeout also opens a short cooldown during which paths under the SAME
# prefix are refused without touching the filesystem: one bash command can
# carry many path tokens against the same wedged mount, each of which would
# otherwise pay the full timeout -- ten tokens at 2s would put the loop back
# past the watchdog.  The cooldown is scoped to the stalled prefix
# (:func:`_stall_prefix`), never process-wide, so a stall on ``/home/<user>``
# leaves ``/tmp`` and the workspace fully resolved.  It doubles on every
# repeat stall under the same prefix (up to the cap below) and a re-probe is
# only attempted while it leaves a worker free, because a timed-out worker is
# NOT reclaimed: a mount that stays dead would otherwise be handed a fresh
# worker every cooldown until every worker is pinned and every healthy path
# queues behind wedged futures -- the per-prefix isolation would hold only
# while free workers remained.
#
# The thread is NOT freed by the timeout (a started future cannot be cancelled);
# that is why this has its own pool -- see ``executors.path_resolve_executor``.
_PATH_RESOLVE_TIMEOUT_SECS = 2.0
_PATH_RESOLVE_COOLDOWN_SECS = 30.0
_PATH_RESOLVE_COOLDOWN_MAX_SECS = 1800.0
# stall prefix -> (monotonic deadline until which paths under it are refused,
# consecutive stalls recorded under it -- drives the exponential backoff)
_path_resolve_degraded: dict[str, tuple[float, int]] = {}
# futures that timed out and still hold an mc-pathres worker; pruned as they finish
# (candidate spellings, root anchors and target rebuilds all land here)
_path_resolve_wedged: list[Future[Any]] = []
_path_resolve_lock = threading.Lock()
_path_resolve_clock: Callable[[], float] = time.monotonic  # tests advance this


def _resolved_spellings(expanded: str) -> set[str]:
    """Symlink-resolved spellings of *expanded*; runs on the ``mc-pathres`` pool."""
    out: set[str] = set()
    try:
        out.add(os.path.realpath(expanded))
    except (OSError, ValueError):
        pass
    try:
        # Guarded false-positive: this resolve() is INSIDE is_sensitive_path — the
        # sanitizer itself — building candidate forms to CHECK a path against the
        # sensitive denylist. It performs no read/write. CodeQL surfaces
        # py/path-injection here only because a new caller (artifact relocate)
        # reaches it with user input; the function's whole purpose is to vet that
        # input, so suppress the alert on the resolution step.
        out.add(str(Path(expanded).resolve()))  # lgtm[py/path-injection]
    except (OSError, ValueError, RuntimeError):
        pass
    return out


class PathResolutionStalled(RuntimeError):
    """Symlink resolution of an agent-supplied path did not complete in time.

    Raised by :func:`_resolved_forms_bounded` when the bounded ``realpath``
    times out, and for the cooldown that follows under the same path prefix.
    The sensitive-path gates treat it as FAIL-CLOSED: a path whose canonical
    form cannot be established is refused, never matched on its lexical
    spelling alone -- a lexical-only match would let a workspace symlink into a
    credential store pass while the mount it does not even live on is wedged.
    """

    def __init__(self, path: str, prefix: str) -> None:
        super().__init__(
            f"symlink resolution of {path!r} is unavailable (stalled mount under {prefix!r})"
        )
        self.path = path
        self.prefix = prefix


def _stall_prefix(expanded: str) -> str:
    """The path prefix a stall is charged to: the first two components.

    A wedged mount stalls everything beneath its mount point, and mount points
    sit at depth one or two (``/home/<user>`` autofs, ``/Volumes/<share>``,
    ``/net/<host>``, ``C:\\Users``), so two components is the narrowest key
    that still covers the whole stalled subtree.  Scoping the cooldown here is
    what keeps a stall on the REMOTE half of an ``ssh`` command from switching
    resolution off for the local workspace where a bypass symlink would live.
    """
    normalized = os.path.normpath(expanded)
    parts = normalized.split(os.sep)
    keep = 3 if parts and parts[0] == "" else 2  # leading "" for an absolute path
    return os.sep.join(parts[:keep]) or normalized


def _wedged_workers() -> int:
    """How many ``mc-pathres`` workers are still pinned by a timed-out resolution.

    A future that timed out is not cancelled -- its thread stays in the kernel
    until the mount answers -- so it is kept here and forgotten once it finally
    completes.  The count gates re-probes: a mount that stays dead (hard NFS,
    not the transient VPN case) must not be handed a fresh worker every cooldown
    until none is left and every healthy path queues behind wedged futures.
    """
    with _path_resolve_lock:
        _path_resolve_wedged[:] = [f for f in _path_resolve_wedged if not f.done()]
        return len(_path_resolve_wedged)


def _mark_stalled(prefix: str, budget: float) -> None:
    """Record an OBSERVED stall under *prefix*: back off exponentially on repeats.

    Only a resolution that actually timed out is recorded.  A refusal issued
    because every worker was already pinned costs nothing (nothing is
    submitted) and must not charge the refused prefix -- often the local
    workspace -- a backoff it never earned, or a transient dual-mount outage
    would keep refusing healthy paths for the accrued window after the mounts
    recover.  The log line deliberately omits the path: the token is
    agent-supplied and is what the gates exist to keep out of clear-text logs.
    """
    now = _path_resolve_clock()
    with _path_resolve_lock:
        if len(_path_resolve_degraded) > 64:
            _path_resolve_degraded.clear()
        _, stalls = _path_resolve_degraded.get(prefix, (0.0, 0))
        stalls += 1
        cooldown = min(
            _PATH_RESOLVE_COOLDOWN_SECS * (2 ** (stalls - 1)),
            _PATH_RESOLVE_COOLDOWN_MAX_SECS,
        )
        _path_resolve_degraded[prefix] = (now + cooldown, stalls)
    logger.warning(
        "sensitive-path symlink resolution did not complete in %.1fs (stalled "
        "mount?); refusing paths under the stalled prefix for the next %.0fs "
        "(stall #%d, %d resolver worker(s) pinned)",
        budget,
        cooldown,
        stalls,
        len(_path_resolve_wedged),
    )


_UNC_PREFIX_RE = re.compile(r"^[\\/]{2}[^\\/]")
_ON_WINDOWS = os.name == "nt"


def _is_unc_path(expanded: str) -> bool:
    """``\\\\server\\share\\...`` in either separator spelling.

    On Windows ``os.path.realpath`` on a UNC path opens it
    (``GetFinalPathNameByHandle``), which is a network round-trip to the named
    host -- a dead or slow host stalls the caller for the SMB timeout, and a
    UNC token in an agent's command is the ordinary way to name a share, not a
    symlink-bypass vector: the fence's targets are local drive spellings that a
    UNC realpath never produces (``\\\\?\\UNC\\...``).  So a UNC token is matched
    lexically and never probed, the same stance the mapped-drive fence below
    takes for a foreign drive letter.
    """
    return bool(_UNC_PREFIX_RE.match(expanded))


_ResolvedT = TypeVar("_ResolvedT")


def _run_resolution_bounded(
    expanded: str, worker: Callable[[str], _ResolvedT]
) -> _ResolvedT | None:
    """Run *worker(expanded)* on the ``mc-pathres`` pool within the resolve budget.

    The shared core under :func:`_resolved_forms_bounded` (the agent-supplied
    CANDIDATE), :func:`_resolved_root_key` and :func:`_rebuild_targets_bounded`
    (the TARGET anchors: ``$HOME``, the
    override roots and the keystone leaves).  Both kinds of resolution stat the
    same filesystem from the event loop, so they share one pool, one budget and
    one per-prefix cooldown: a stall observed while anchoring ``$HOME`` refuses
    candidate resolution under that prefix for the same window, and a stall on
    a candidate keeps the anchors from re-probing the same wedged mount every
    time the target cache expires.

    Returns the worker's value, or ``None`` when resolution FAILED -- the pool
    refused work at interpreter exit, or faulted.  A resolution that does not
    COMPLETE is different and raises
    :class:`PathResolutionStalled` instead, both on the timing-out call and,
    without touching the filesystem, for every later call under the same
    :func:`_stall_prefix` until the cooldown lapses.  Repeated stalls under one
    prefix double the cooldown up to ``_PATH_RESOLVE_COOLDOWN_MAX_SECS``, and a
    prefix with a stall history is only re-probed while that leaves at least one
    worker free for everything else -- so a permanently dead mount is probed
    rarely and can never pin the whole pool.  Never blocks the caller for longer
    than ``_PATH_RESOLVE_TIMEOUT_SECS``.

    The UNC shortcut is NOT here: skipping a ``\\\\server\\share`` token is a
    stance about agent-supplied CANDIDATES (:func:`_resolved_forms_bounded`),
    whose fence targets a UNC realpath never produces.  The anchors are the
    fence itself, and a UNC home with a junction inside ``KIROCREW_HOME`` must
    still be canonicalised or a canonical-spelling request would miss the
    governance file (found in review); the bound makes that probe safe.
    """
    budget = _PATH_RESOLVE_TIMEOUT_SECS
    now = _path_resolve_clock()
    prefix = _stall_prefix(expanded)
    with _path_resolve_lock:
        history = _path_resolve_degraded.get(prefix)
    if history is not None and now < history[0]:
        raise PathResolutionStalled(expanded, prefix)
    wedged = _wedged_workers()
    if wedged >= _MAX_PATH_RESOLVE_WORKERS or (history is not None and wedged >= _MAX_PATH_RESOLVE_WORKERS - 1):
        # Every worker is pinned, or this re-probe of a known-stalled prefix
        # would pin the last free one.  Queueing behind a wedged future can only
        # time out, so refuse now.  Nothing was submitted, so nothing is charged
        # to the prefix: the next call re-evaluates the gate for free.
        logger.debug(
            "sensitive-path symlink resolution refused without probing: %d of %d "
            "resolver worker(s) pinned by earlier stalls",
            wedged,
            _MAX_PATH_RESOLVE_WORKERS,
        )
        raise PathResolutionStalled(expanded, prefix)
    try:
        future = path_resolve_executor().submit(worker, expanded)
    except RuntimeError:
        # Pool already shut down (interpreter exit).  Lexical forms only.
        return None
    try:
        value = future.result(timeout=budget)
    except FutureTimeoutError:
        with _path_resolve_lock:
            _path_resolve_wedged.append(future)
        _mark_stalled(prefix, budget)
        raise PathResolutionStalled(expanded, prefix) from None
    except Exception:
        # The worker's own exceptions are already swallowed inside the worker;
        # anything else here is a pool fault, and the gate's contract is to keep
        # the lexical forms rather than fail the tool call.
        logger.debug("sensitive-path symlink resolution failed", exc_info=True)
        return None
    if history is not None:
        # The mount answered again: forget the stall history so the next stall
        # starts from the base cooldown rather than an inherited backoff.
        with _path_resolve_lock:
            _path_resolve_degraded.pop(prefix, None)
    return value


def _resolved_forms_bounded(expanded: str) -> set[str]:
    """Return the symlink-resolved spellings of *expanded*, or an empty set.

    Empty means resolution FAILED (see :func:`_run_resolution_bounded`) or was
    deliberately not attempted -- a UNC path on Windows, see
    :func:`_is_unc_path`: the caller keeps the lexical forms, exactly as before
    the bound existed.  A resolution that does not COMPLETE raises
    :class:`PathResolutionStalled` through here, and every gate turns that into
    a refusal.  Tests swap :func:`_resolved_spellings` at module level for a
    blocking stub and advance ``_path_resolve_clock``.
    """
    if _ON_WINDOWS and _is_unc_path(expanded):
        return set()
    forms = _run_resolution_bounded(expanded, _resolved_spellings)
    return set() if forms is None else forms


def _realpath_or_none(path: str) -> str | None:
    """``os.path.realpath`` for a target anchor; runs on the ``mc-pathres`` pool."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _candidate_forms(path_str: str, base_dir: str | None = None) -> set[str]:
    """Expand *path_str* into every candidate form the sensitive-path gates match.

    Symlink-resolved forms defeat a link bypass; the lexical forms are the
    fail-safe fallback when resolution cannot complete (over-matching a
    sensitive-looking path is the safe direction). ``base_dir`` anchors a
    relative input against the caller's known working directory. Shared by
    :func:`_path_in_home_dirs` (is the path INSIDE a protected location?) and
    :func:`path_contains_sensitive` (does the path CONTAIN one?) so the
    symlink/anchoring hardening cannot drift between the two directions.
    """
    # Expand ~ and $HOME
    expanded = os.path.expanduser(os.path.expandvars(path_str))

    # Anchor a relative input against the supplied workspace dir so it resolves
    # to the real file rather than the gateway's CWD.  Absolutize base_dir
    # itself first — if a caller passes a relative base_dir, os.path.join would
    # re-anchor against the process CWD (the very thing the parameter exists to
    # avoid), giving zero protection when CWD is unrelated to the workspace.
    if base_dir and not os.path.isabs(expanded):
        expanded = os.path.join(os.path.abspath(base_dir), expanded)

    # Build the candidate forms.  Symlink-resolved forms defeat a link bypass;
    # the lexical forms are the fail-safe fallback when resolution FAILS
    # (over-matching a sensitive-looking path is the safe direction).
    # Resolution is BOUNDED -- see _resolved_forms_bounded: an unbounded lstat on
    # a stalled automount used to wedge the event loop from inside on_tool_call.
    # A resolution that does not COMPLETE raises PathResolutionStalled through
    # here, and every gate turns that into a refusal: no lexical-only matching
    # of a path whose canonical form is unknown.
    candidates: set[str] = _resolved_forms_bounded(expanded)
    candidates.add(os.path.normpath(expanded))
    candidates.add(expanded)
    return candidates


def _home_dir_targets_uncached(
    home_dirs: list[str],
    roots: _ResolvedRoots | None = None,
) -> set[str]:
    """Anchor the ``$HOME``-relative *home_dirs* entries into absolute, casefolded
    on-disk targets.

    Every per-anchor resolved form comes from :func:`_realpath_or_none`, looked
    up at call time so a test can stand in a recording or wedged resolver at
    module level.  It touches the filesystem, so in production this function
    runs on the ``mc-pathres`` pool via :func:`_home_dir_targets` (see there
    for why); only direct callers and tests run it inline.

    *roots* optionally supplies the already-resolved :class:`_ResolvedRoots`
    already resolved by the caller. The TTL cache in :func:`_home_dir_targets` MUST pass
    it: resolving the roots here as well would read the filesystem a second
    time, and a root symlink repointed between the two reads would file this
    set under a key naming the OTHER root — caching one root's targets against
    another root's key, which fails OPEN. ``None`` (direct callers and tests)
    resolves them here as before.

    Anchors against BOTH the logical home and its realpath.  On macOS the
    per-user temp/home prefix can itself be reached via OS symlinks (``/var`` →
    ``/private/var``); folding both roots in means a resolved candidate under
    either spelling is still matched.

    ``home_dirs`` entries are authored with POSIX "/" separators, and some are
    multi-segment now (e.g. ".kiro/crew/security_policy.json"). Split on "/"
    and re-join with ``os.path.join`` so the target uses the running OS's
    separator — otherwise on Windows the target keeps a literal "/" in the
    leaf while the candidate forms (realpath/normpath) are all-backslash, they
    never compare equal, and the keystone would silently stop gating its own
    secrets. On POSIX a single-segment entry splits to a 1-element list, so
    this is a no-op there.
    """
    resolved = roots if roots is not None else _resolved_root_key()
    home = resolved.home
    crew_home = resolved.crew_home
    kiro_home_override = resolved.kiro_home
    logical_home = resolved.logical_home

    def _anchor(root: str, d: str) -> str:
        return os.path.join(root, *d.split("/")).casefold()

    sensitive_targets: set[str] = {_anchor(home, d) for d in home_dirs}
    # ``home`` arrives RESOLVED from the cache key, so this is normally a no-op;
    # it still opens the directory on Windows, which is why the whole rebuild
    # runs off the loop.  None degrades to the lexical anchors already in the set.
    home_real = _realpath_or_none(home) or home
    if home_real.casefold() != home.casefold():
        sensitive_targets |= {_anchor(home_real, d) for d in home_dirs}
    # ``home`` arrives RESOLVED (the cache is keyed on the resolved roots), so
    # the realpath above is normally a no-op and the LOGICAL spelling of a
    # symlinked ``$HOME`` was never anchored -- a gap masked as long as every
    # candidate was itself resolved.  Candidate resolution is now bounded and
    # degrades to the lexical spelling, so anchor the logical home explicitly:
    # ``~/.ssh/id_rsa`` spelled through ``/home/x`` must match even when
    # ``/home/x -> /local/home/x`` could not be followed in time.
    if logical_home.casefold() != home.casefold():
        sensitive_targets |= {_anchor(logical_home, d) for d in home_dirs}
    # When KIROCREW_HOME points to a non-default path, the keystone secrets
    # (token_signing.key, refresh_chains.json, .local_secret, sel_hmac.key,
    # security_policy.json etc.) live directly under it — NOT under either of
    # the default crew home prefixes (~/.kiro/crew, ~/.kirocrew). Without this
    # expansion any "<crew-prefix>/X" entry in the home_dirs list would miss
    # the real file location, letting the agent read/write its own signing key
    # or governance ceiling via the custom KIROCREW_HOME. Strip whichever crew
    # prefix an entry carries and re-anchor the leaf under the env-override
    # root ADDITIONALLY (the ~/-rooted default forms stay, so every location is
    # always covered).
    if crew_home:
        kiro_home = crew_home
        for d in home_dirs:
            for _prefix in _CREW_HOME_PREFIXES:
                # Compare with POSIX separators (home_dirs entries are authored
                # that way) so this matches regardless of the running os.sep.
                if d == _prefix or d.startswith(_prefix + "/"):
                    leaf = d[len(_prefix) :].lstrip("/")
                    full = os.path.join(kiro_home, *leaf.split("/")) if leaf else kiro_home
                    sensitive_targets.add(full.casefold())
                    # Also add the resolved form in case the env value itself has
                    # symlinks (matches the home/home_real duality above).
                    full_real = _realpath_or_none(full)
                    if full_real is not None:
                        sensitive_targets.add(full_real.casefold())
                    break
    # The agents dir (``~/.kiro/agents``) follows ``KIRO_HOME`` — kiro-cli's own
    # home override, honoured by ``kiro_agents_dir()``. When it is set, the specs
    # the gateway execs live at ``<KIRO_HOME>/agents``, NOT under the real home,
    # so the ``.kiro/agents`` entry anchored above misses them and an agent write
    # there would bypass the gate. Re-anchor the leaf under the override, mirroring
    # the ``KIROCREW_HOME`` expansion directly above (the ~/-rooted default form
    # stays, so both locations are always covered). Only added when the agents dir
    # is actually in *home_dirs* — it is on the write-only tier
    # (``_WRITE_PROTECTED_HOME_PATHS``) and NOT in ``_SENSITIVE_HOME_DIRS``, so
    # this must not leak an agents target into the read gate. No validity check on
    # the override: an unsafe ``KIRO_HOME`` falls back to ``~/.kiro`` in
    # ``kiro_home()`` (already covered by the default form), so an extra target
    # under a bogus value is harmless and fail-safe.
    if kiro_home_override and _KIRO_AGENTS_DIR in home_dirs:
        agents_full = os.path.join(kiro_home_override, "agents")
        sensitive_targets.add(agents_full.casefold())
        agents_real = _realpath_or_none(agents_full)
        if agents_real is not None:
            sensitive_targets.add(agents_real.casefold())
    # An ACP adapter's OAuth token follows that adapter's own home override, so
    # the ``$HOME``-rooted entry anchored above covers only the documented
    # default. Re-anchor the token leaf under each override the adapter honours
    # (the default form stays, so every location is always covered). Guarded on
    # membership in *home_dirs* for the same reason as the agents dir above: a
    # write-tier build must not gain a read-tier target.
    for _leaf, _root_fields in _OVERRIDE_ANCHORED_LEAVES:
        if _leaf not in home_dirs:
            continue
        _basename = _leaf.split("/")[-1]
        for _field in _root_fields:
            _root = getattr(resolved, _field, None)
            if not _root:
                continue
            _full = os.path.join(_root, _basename)
            sensitive_targets.add(_full.casefold())
            _full_real = _realpath_or_none(_full)
            if _full_real is not None:
                sensitive_targets.add(_full_real.casefold())
    return sensitive_targets


# How long a built target set stays reusable. ``_home_dir_targets_uncached``
# rebuilds a ~75-entry set on EVERY ``is_sensitive_path`` call and measured at
# 1.14ms of that call's 1.25ms (91%) on a dev desktop, because it realpath()s
# ``$HOME`` and each KIROCREW_HOME-anchored leaf. Callers hit it per FILE — one
# skills-tree walk made thousands of identical calls and took 4.2s, of which
# 3.5s was this rebuild.
#
# Deliberately TTL-bounded rather than a plain ``lru_cache``: part of the set is
# derived from FILESYSTEM state, so an unbounded cache would keep matching a
# stale target if a symlink were repointed after the cache warmed — a gate that
# fails OPEN. A few seconds bounds that window.
#
# The key is built from the RESOLVED roots (``Path.home().resolve()`` and the
# resolved ``KIROCREW_HOME``), NOT from the raw env vars, because those two
# values are exactly what the builder anchors its targets on. Keying on the raw
# ``$HOME`` string was wrong twice over:
#   1. Repointing a symlink AT ``$HOME`` leaves ``$HOME`` unchanged while every
#      target moves, so the gate returned False for a credential path the
#      uncached code blocked (a real, reproduced bypass — see the regression
#      test ``test_repointed_home_symlink_is_not_served_from_cache``).
#   2. ``Path.home()`` reads ``USERPROFILE`` on Windows and never ``HOME``, so on
#      that platform the key omitted the one variable that decides the anchor.
# Resolving the roots costs ~2 realpath calls (~0.06ms) against the ~1.14ms
# rebuild it replaces, so the win survives. Those calls -- and the rebuild
# itself -- run on the ``mc-pathres`` pool under the resolve budget, one thread
# hop each (``_resolved_root_key`` resolves all six roots in one job,
# ``_rebuild_targets_bounded`` resolves every leaf in one job), because
# an inline ``realpath($HOME)`` on a loaded Windows desktop blocked past the
# loop-stall watchdog; see ``_rebuild_targets_bounded``.
#
# Residual, accepted: a symlink swapped DEEPER inside the crew home (an
# individual keystone leaf, or an intermediate directory on the way to one) can
# still be served stale for up to the TTL. Detecting that needs the per-leaf
# realpath calls that ARE the expense — measured 45 realpath calls per build,
# 94% of its 1.39ms — so there is no cheap way to keep the cache and revalidate
# them.
#
# The TTL is therefore sized as small as it can be while still doing its job.
# The value is 0.1s, NOT a "few seconds", because a skills walk issues thousands
# of calls in a burst and one build serves the whole burst either way. Measured
# cold-walk cost against this constant:
#     5.0s -> 0.95s    1.0s -> 0.93s    0.1s -> 0.95s    0.0s -> 4.66s
# So 0.1s keeps the entire win while cutting the stale window 50x versus 5.0s.
# Only 0.0 (no cache) closes the window completely, and that reverts to the 4.7s
# scan whose GIL-held cost wedges the event loop — the defect this exists to fix.
_HOME_TARGETS_TTL_SECS = 0.1
# key -> (expiry_monotonic, targets)
_home_targets_cache: dict[tuple[object, ...], tuple[float, set[str]]] = {}


class _ResolvedRoots(NamedTuple):
    """The roots the sensitive-target set is anchored on, AND its cache key.

    Those two jobs are the same object on purpose: every field is part of the
    key, so an override that would move a target invalidates the cached set
    instead of serving targets anchored on the previous value. Keying on fewer
    fields than the builder anchors on is the fail-OPEN shape the resolved-home
    key already exists to prevent.

    A new adapter with its own credential home adds a field here and an entry in
    ``_OVERRIDE_ANCHORED_LEAVES``; nothing else changes, because the tuple is
    unpacked by FIELD rather than by position.
    """

    home: str
    crew_home: str | None
    kiro_home: str | None
    codex_home: str | None
    claude_config_dir: str | None
    claude_home: str | None
    logical_home: str


#: Sensitive leaf → the override roots its parent directory can be moved to.
#:
#: The leaf's own ``$HOME``-rooted form is anchored by the ordinary path in
#: ``_home_dir_targets_uncached``; this table only covers the overrides. A leaf
#: absent from the *home_dirs* list being built is skipped, so a write-tier
#: build never leaks a read-tier target.
_OVERRIDE_ANCHORED_LEAVES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (".codex/auth.json", ("codex_home",)),
    (".claude/.credentials.json", ("claude_config_dir", "claude_home")),
)


def _resolved_env_root(name: str) -> str | None:
    """Resolve an environment home override, or ``None`` when it is unset.

    Falls back to the unresolved absolute form on OSError/ValueError the same way
    the builder does. No validity check: an unsafe override falls back to its
    default inside the owning helper, and that default is already covered by the
    ``$HOME``-rooted entry, so an extra target under a bogus value is harmless
    and fail-safe.

    The value is read VERBATIM -- deliberately not stripped. Whitespace is a legal
    POSIX path character, and the owning resolvers take the variable raw
    (``_valid_override_home`` does ``Path(os.environ.get("KIROCREW_HOME"))``, and
    ``config_dir`` then ``mkdir``s whatever that names). Stripping here would
    anchor the target set on ``<root>`` while the process actually runs out of
    ``"<root> "``, leaving the real ``.env``, signing keys and governance files
    outside the floor this set defines. Emptiness is the only test, so an unset
    or empty override still resolves to ``None``.
    """
    expanded = _expanded_env_root(name)
    if expanded is None:
        return None
    # Runs on the ``mc-pathres`` pool via _resolve_root_anchors -- never call it
    # from the event loop directly; go through _resolved_root_key.  A failure
    # keeps the lexical form, exactly as the OSError arm did.  ``Path.resolve()``
    # is ``os.path.realpath`` underneath, so the resolved spelling is unchanged.
    return _realpath_or_none(expanded) or _lexical_root(expanded)


def _expanded_env_root(name: str) -> str | None:
    """The ``~``-expanded value of an environment home override, or ``None``."""
    raw = os.environ.get(name, "")
    if not raw:
        return None
    return os.path.expanduser(raw)


def _lexical_root(expanded: str) -> str:
    """Absolute, normalized spelling of *expanded* WITHOUT touching the filesystem.

    Deliberately not ``os.path.abspath``: on Windows that calls
    ``GetFullPathName``, which strips a trailing space or dot -- and the
    verbatim contract above says the anchor must keep it, because the owning
    resolvers run out of exactly the spelling the variable carries.
    """
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)
    return os.path.normpath(expanded)


#: The override roots :func:`_resolve_root_anchors` resolves, in field order.
_OVERRIDE_ROOT_ENVS: tuple[tuple[str, str], ...] = (
    ("crew_home", "KIROCREW_HOME"),
    ("kiro_home", "KIRO_HOME"),
    ("codex_home", "CODEX_HOME"),
    ("claude_config_dir", "CLAUDE_CONFIG_DIR"),
    ("claude_home", "CLAUDE_HOME"),
)


def _resolve_root_anchors(logical_home: str) -> _ResolvedRoots:
    """Resolve every root the target set anchors on; runs on the ``mc-pathres`` pool.

    One worker call resolves ``$HOME`` and all five override roots together,
    so :func:`_resolved_root_key` -- which runs once per ``is_sensitive_path``
    call, on the event loop -- pays a single thread hop rather than six.  The
    stall bookkeeping is charged to the logical home's prefix: that is the
    mount every root ordinarily lives under, and it is the one the crash dumps
    named.
    """
    home = _realpath_or_none(logical_home) or logical_home
    overrides = {field: _resolved_env_root(env) for field, env in _OVERRIDE_ROOT_ENVS}
    return _ResolvedRoots(home=home, logical_home=logical_home, **overrides)


def _resolved_root_key() -> _ResolvedRoots:
    """Return the roots the target set is anchored on.

    Mirrors how :func:`_home_dir_targets_uncached` derives its anchors, so the
    cache key changes exactly when the anchors would. Falls back to the
    unresolved form on OSError/ValueError the same way the builder does.

    ``kiro_home`` is the resolved ``KIRO_HOME`` override (kiro-cli's own home
    override, honoured by ``kiro_agents_dir()``), or ``None`` when unset — it
    re-anchors the ``~/.kiro/agents`` write-protection, so a changed ``KIRO_HOME``
    must invalidate the cache. No validity check here (an unsafe value falls back
    to ``~/.kiro`` in ``kiro_home()``, already covered by the default form); it is
    resolved only so a symlinked override keys and anchors identically.

    The three adapter roots do the same for the OAuth token leaves in
    ``_OVERRIDE_ANCHORED_LEAVES``.

    ``logical_home`` is ``Path.home()`` UNRESOLVED.  It is a separate anchor, not
    a duplicate: on a host where ``$HOME`` is itself a symlink (``/home/x`` ->
    ``/local/home/x`` on cloud desktops) the resolved home spells every target
    one way while an agent-supplied ``~/.ssh/id_rsa`` spells it the other.  The
    resolved CANDIDATE normally bridges that -- but candidate resolution is
    bounded (:func:`_resolved_forms_bounded`) and degrades to the lexical
    spelling, which must still hit a target or the gate fails OPEN on exactly
    the hosts where ``$HOME`` is a link.  Keyed here so an env change that
    moves the logical spelling invalidates the cache like any other anchor.
    """
    logical_home = str(Path.home())
    # Bounded (see _rebuild_targets_bounded): this runs on the event loop once per
    # is_sensitive_path call, and an inline resolve of a slow-to-stat $HOME is
    # exactly the stall the watchdog dumps caught.  All six roots resolve in
    # ONE pool hop (_resolve_root_anchors).
    #
    # INVARIANT: the gate only ever compares against anchors resolved FRESH,
    # canonically, within the budget.  Anything else -- a stall, an open
    # cooldown, a pinned or faulted pool -- raises, and every gate turns that
    # into a refusal, exactly as it does for a stalled candidate.  Three
    # weaker fallbacks were each found open in review: lexical spellings (a
    # symlinked override root on another mount loses its canonical target), a
    # UNC skip (same, via a junction in a UNC home), and serving the previous
    # canonical resolution (a symlink repointed during the stall moves the
    # credential out from under the stale anchor).  Refusing for the cooldown
    # is the one outcome none of those reach.
    try:
        roots = _run_resolution_bounded(logical_home, _resolve_root_anchors)
    except PathResolutionStalled:
        roots = None
    if roots is None:
        raise PathResolutionStalled(logical_home, _stall_prefix(logical_home))
    return roots


def _home_dir_targets(home_dirs: list[str]) -> set[str]:
    """TTL-cached :func:`_home_dir_targets_uncached`.

    Keyed on the *home_dirs* list plus the RESOLVED home and crew-home roots
    (see the note above the constant for why the raw env vars are not enough).

    ponytail: the returned set is the cached instance, not a copy — both
    callers only iterate it. A future caller that MUTATES the result would
    poison the cache for every other caller; copy here if that ever happens.
    """
    # Resolve the roots ONCE and use the same tuple for both the key and the
    # build. Resolving separately let a root symlink repointed between the two
    # reads file one root's targets under the other root's key — a fail-OPEN
    # TOCTOU. Local review caught this; see the regression test
    # test_roots_are_resolved_once_for_key_and_build.
    roots = _resolved_root_key()
    key = (tuple(home_dirs),) + roots
    now = time.monotonic()
    cached = _home_targets_cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    targets = _rebuild_targets_bounded(home_dirs, roots)
    # Bound the dict: the key space is tiny (two constant home_dirs lists ×
    # roots), but a test or embedder that churns KIROCREW_HOME must not grow it
    # without limit.
    if len(_home_targets_cache) > 32:
        _home_targets_cache.clear()
    _home_targets_cache[key] = (now + _HOME_TARGETS_TTL_SECS, targets)
    return targets


def _rebuild_targets_bounded(home_dirs: list[str], roots: _ResolvedRoots) -> set[str]:
    """Rebuild the target set on the ``mc-pathres`` pool.

    The anchors -- ``$HOME``, the ``KIROCREW_HOME`` / ``KIRO_HOME`` / adapter
    override roots and the ~40 keystone leaves under them -- are the paths the
    sensitive-target set is built FROM, as opposed to the agent-supplied
    candidate checked AGAINST it.  They used to be ``realpath``'d inline, on
    the event loop, every time the 0.1s cache expired: on a Windows desktop
    under heavy disk load (a full test run plus several subagents, all being
    scanned by real-time antivirus) ``realpath($HOME)`` blocked past the 25s
    loop-stall watchdog from inside ``on_tool_call``, and the gateway exited
    with every in-flight turn -- the same crash the bounded candidate
    resolution already prevents for the OTHER half of the check.

    The whole rebuild is ONE pool job, not one per leaf: a single bash command
    can drive ~200 rebuilds (``test_chained_cd_expansions_do_not_blow_up_the_gate``),
    and 40 thread hops per rebuild is what turns a 9s gate into a 15s one.  The
    stall bookkeeping is charged to ``roots.home``'s prefix, the mount every
    anchor ordinarily lives under and the one the crash dumps named.

    A rebuild that does not complete canonically within the budget RAISES, and
    every gate turns that into a refusal -- the same invariant as
    :func:`_resolved_root_key` (see the comment there for the three weaker
    fallbacks review found open: lexical spellings, a UNC skip, and serving the
    previous canonical set).  A pool fault is treated exactly like a stall, and
    a UNC home is probed here too (bounded): the candidate-side UNC shortcut is
    about tokens, not about the fence.  The stall is recorded, so the rebuild
    does not re-probe the wedged mount every 0.1s -- it refuses without touching
    the filesystem until the cooldown lapses.
    """
    try:
        targets = _run_resolution_bounded(
            roots.home, lambda _home: _home_dir_targets_uncached(home_dirs, roots)
        )
    except PathResolutionStalled:
        targets = None
    if targets is None:
        raise PathResolutionStalled(roots.home, _stall_prefix(roots.home))
    return targets


def _path_in_home_dirs(path_str: str, home_dirs: list[str], base_dir: str | None = None) -> bool:
    """Return True if *path_str* resolves under any of *home_dirs* (``$HOME``-relative).

    Shared matching core for :func:`is_sensitive_path` (read+write gate,
    ``_SENSITIVE_HOME_DIRS``) and :func:`is_sensitive_write_path` (write-only
    gate, the read+write set PLUS ``_WRITE_PROTECTED_HOME_PATHS``). Keeping one
    implementation means the symlink/casefold hardening below cannot drift
    between the two gates.

    ── Symlink robustness (pentest AWS-345 / AWS-62) ──
    A workspace symlink pointing at ``~/.aws/credentials`` (absolute OR relative
    ``../../.aws/credentials`` traversal) must NOT be readable through the link.
    We therefore check MULTIPLE candidate forms of the input and return True if
    ANY of them lands in a matched location:

      1. the fully symlink-RESOLVED canonical target (``realpath`` /
         ``Path.resolve`` — follows every symlink in the chain, including
         intermediate directories and the final component).  This is what
         defeats the symlink bypass: the resolved target of the link is
         ``~/.aws/credentials`` even though the link's own name is benign.
      2. the LEXICALLY-normalized path (no symlink following) and the raw
         expanded string — so a path that *textually* names a matched dir is
         still caught when resolution fails (dangling link, permission error).

    ``base_dir`` anchors a *relative* input against the caller's known working
    directory (e.g. the agent's workspace cwd) so a relative title like
    ``sub/cfg.ini`` resolves against the real directory rather than whatever CWD
    the gateway process happens to have.  Absolute inputs are unaffected;
    ``base_dir=None`` preserves the historical CWD-relative behavior.
    """
    if not path_str:
        return False

    try:
        candidates = _candidate_forms(path_str, base_dir)
        # The anchors are bounded the same way (see _rebuild_targets_bounded):
        # a stall with no prior canonical resolution to serve refuses too.
        sensitive_targets = _home_dir_targets(home_dirs)
    except PathResolutionStalled:
        # Canonical form unavailable (wedged mount under the path): refuse.  A
        # lexical-only match here would pass a workspace symlink into a
        # credential store for the length of the stall.
        return True

    # Case-fold both sides for the membership test.  On a case-insensitive
    # filesystem (macOS APFS/HFS+ default — a supported platform) the OS opens
    # ``~/.kirocrew/Security_Policy.json`` and ``~/.kirocrew/security_policy.json``
    # as the SAME file, so a byte-exact comparison would let the agent write its
    # own governance ceiling via an alternate-case path. Folding is strictly more
    # protective (it can only ever over-match an alternate-case variant of an
    # already-sensitive path, which is itself suspicious), so it is safe on
    # case-sensitive Linux too — matching the IGNORECASE bash-read matcher.
    for cand in candidates:
        cand_cf = cand.casefold()
        for sensitive_path in sensitive_targets:
            if cand_cf == sensitive_path or cand_cf.startswith(sensitive_path + os.sep):
                return True
    return False


def _is_keystone_publish_artifact(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if *path_str* is the atomic-write temp or lock beside a keystone leaf.

    Closes the gap between a keystone leaf's FINAL name, which
    :data:`_SENSITIVE_HOME_DIRS` fences, and the intermediate inodes its publish
    actually goes through -- see :data:`_KEYSTONE_ARTIFACT_PARENTS` for why the rule is
    derived from the leaf list instead of restated per leaf.

    Two properties are load-bearing:

    - It reuses :func:`_candidate_forms` and :func:`_home_dir_targets`, so the
      symlink-resolution, casefolding and ``KIROCREW_HOME`` re-anchoring cannot drift
      from the main gate. A relocated crew home is covered because a
      ``<crew-prefix>``-rooted entry hits the prefix-stripping arm in
      :func:`_home_dir_targets_uncached`; a symlink aimed at a live temp is covered
      because the resolved form is one of the candidates.
    - The parent is compared for EQUALITY, not by prefix. An artifact is a direct child
      of the leaf's own directory, and a prefix test would sweep every descendant of the
      crew home whose name happens to end in ``.tmp`` -- far wider than this needs, in a
      directory that must stay readable.
    """
    if not path_str:
        return False
    try:
        artifact_parents = _home_dir_targets(_KEYSTONE_ARTIFACT_PARENTS)
        candidates = _candidate_forms(path_str, base_dir)
    except PathResolutionStalled:
        return True  # fail closed: see _path_in_home_dirs
    for cand in candidates:
        cand_cf = cand.casefold()
        # Suffixes are authored lowercase and the candidate is casefolded, so this is
        # the same case-insensitive comparison the rest of the gate makes -- on
        # macOS/Windows ``FOO.TMP`` and ``foo.tmp`` are the same file.
        if not cand_cf.endswith(_KEYSTONE_ARTIFACT_SUFFIXES):
            continue
        if os.path.dirname(cand_cf) in artifact_parents:
            return True
    return False


# Credential dot-dirs denied as a path COMPONENT anywhere in an app-picked local
# folder. This broadens the `is_sensitive_path()` floor below, which resolves its
# entries relative to $HOME and pins `.kube`/`.docker` to single leaf files
# (`config`, `config.json`): membership here denies these directory names at any
# depth and covers those two dirs whole. `path_contains_sensitive()` supplies the
# complementary ancestor/root protection. Owned here so every consumer
# (design_critique's local-target guard, design_tweak's project-folder guard)
# screens against the same set — a credential directory added for one app is
# automatically denied by the others.
DENIED_ROOT_PARTS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})


def is_sensitive_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path points to a read+write-sensitive location.

    Used across every file-access surface (hooks.on_tool_call, validate_file_path,
    artifacts, dashboard file I/O, knowledge indexing) to block BOTH reads and
    writes of credential files and the governance trust-root
    (:data:`_SENSITIVE_HOME_DIRS`). See :func:`_path_in_home_dirs` for the
    symlink/casefold matching contract.

    Also covers a protected leaf's publish artifacts
    (:func:`_is_keystone_publish_artifact`): the temp an ``atomic_write`` renames over
    the leaf holds the leaf's full payload, so READ is blocked alongside write -- a
    write-only fence there would still disclose ``.env`` or ``token_signing.key`` to a
    reader that wins the race.
    """
    return _path_in_home_dirs(
        path_str, _SENSITIVE_HOME_DIRS, base_dir
    ) or _is_keystone_publish_artifact(path_str, base_dir)


def path_contains_sensitive(dir_str: str, base_dir: str | None = None) -> bool:
    """Return True if a read+write-sensitive location lies UNDER *dir_str*.

    The REVERSE direction of :func:`is_sensitive_path`: that gate answers "is
    this path inside a protected location?", this one answers "does this
    directory CONTAIN one?". A bulk operation rooted at *dir_str* — e.g. the
    Notes builtin's ``git add -A`` over an attached vault — sweeps every file
    below the root, so a root that is an ANCESTOR of a credential store (the
    home directory itself, or a parent of ``~/.ssh``) would stage and push the
    credentials wholesale even though the root is not itself a sensitive path.

    List-based, no filesystem walk: the known sensitive roots
    (:data:`_SENSITIVE_HOME_DIRS`, including the crew data-home secret leaves
    and any ``KIROCREW_HOME`` re-anchoring) are prefix-compared against the
    directory's candidate forms, so the check is O(sensitive entries) even when
    *dir_str* is a huge tree. Shares :func:`_candidate_forms` and
    :func:`_home_dir_targets` with :func:`_path_in_home_dirs` so the
    symlink/casefold hardening cannot drift between the two directions.
    """
    if not dir_str:
        return False
    try:
        sensitive_targets = _home_dir_targets(_SENSITIVE_HOME_DIRS)
        candidates = _candidate_forms(dir_str, base_dir)
    except PathResolutionStalled:
        return True  # fail closed: see _path_in_home_dirs
    for cand in candidates:
        # Normalize away a trailing separator so `/home/u/` and `/home/u`
        # produce the same prefix (a bare `/` or `C:\` root rstrips to ""/"C:",
        # whose prefix form still matches everything under it — correct: every
        # sensitive path is inside the filesystem root).
        cand_cf = cand.casefold().rstrip(os.sep)
        prefix = cand_cf + os.sep
        for target in sensitive_targets:
            # Equality (the dir IS the sensitive path) is is_sensitive_path's
            # job, but including it here fails safe for callers using only this
            # gate.
            if target == cand_cf or target.startswith(prefix):
                return True
    return False


def is_sensitive_write_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path must not be MODIFIED by an agent tool.

    Superset of :func:`is_sensitive_path`: everything that is read+write blocked
    PLUS the write-only-protected runtime config files
    (:data:`_WRITE_PROTECTED_HOME_PATHS`), which stay readable but must not be
    written by the agent. Enforced at the file-edit tool gate
    (``hooks.on_tool_call`` on the ACP ``edit`` kind) — see
    :data:`_WRITE_PROTECTED_HOME_PATHS` for the rationale.

    The publish-artifact clause is repeated from :func:`is_sensitive_path` rather than
    left to be inherited, because this gate is documented as a SUPERSET of it: omitting
    it here would leave a keystone temp writable through the edit gate while the
    read+write gate refused it, the same one-path-only hole the pairing notes above warn
    about.
    """
    return _path_in_home_dirs(
        path_str, _SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS, base_dir
    ) or _is_keystone_publish_artifact(path_str, base_dir)


def sensitive_home_dirs() -> tuple[str, ...]:
    """Public, read-only view of the read+write-blocked home-relative paths.

    Lets the security-posture surface (``security_posture.py``) enumerate what
    :func:`is_sensitive_path` actually blocks without coupling to the private
    ``_SENSITIVE_HOME_DIRS`` name — the same rationale as
    :func:`get_credential_patterns`. Returned as a tuple so a caller cannot
    mutate the live blocklist.
    """
    return tuple(_SENSITIVE_HOME_DIRS)


def write_protected_home_paths() -> tuple[str, ...]:
    """Public, read-only view of the write-only-protected home-relative paths.

    Companion to :func:`sensitive_home_dirs` — these stay readable but must not
    be written by an agent tool.
    """
    return tuple(_WRITE_PROTECTED_HOME_PATHS)


def crew_home_prefixes() -> tuple[str, ...]:
    """Public view of the known crew data-home prefixes.

    Used to classify a sensitive path as a KiroCrew trust root vs. a third-party
    credential store when describing the posture.
    """
    return tuple(_CREW_HOME_PREFIXES)


def sandbox_credential_targets(exclude_leaves: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Absolute, on-disk-case credential targets for an OS sandbox deny list.

    Applies the SAME anchoring as :func:`_home_dir_targets_uncached` -- the
    ``$HOME`` projection of every :data:`_SENSITIVE_HOME_DIRS` leaf, plus each
    env-override re-anchor -- so a caller building a sandbox mask inherits the
    read gate's anchor rules instead of re-deriving them. That is the whole point
    of this living here: a mask that projected leaves under ``Path.home()`` only
    would silently miss a credential the operator relocated with
    ``KIROCREW_HOME``, ``CLAUDE_CONFIG_DIR`` or ``CLAUDE_HOME``, which is exactly
    the drift a hand-maintained list already produced once.

    Unlike the read gate's target set the paths are NOT casefolded: that set
    exists to COMPARE against candidate paths, while these are handed to a
    sandbox backend to deny on disk, and a casefolded path denies nothing on a
    case-sensitive filesystem.

    *exclude_leaves* drops a ``$HOME``-relative leaf (and its override
    re-anchors) from the result -- for an adapter whose own OAuth token it must
    still be able to read in order to authenticate. Excluding a leaf here only
    removes it from THIS mask; the read gate still fences it for the agent's own
    file tools, so the two controls keep covering different readers.

    Returns logical paths. The launcher ``os.path.abspath``es what it is handed,
    and the macOS profile emits both a ``subpath`` and a ``literal`` deny for each
    entry, so both a directory and a plain file leaf are valid entries.
    """
    excluded = set(exclude_leaves)
    leaves = [d for d in _SENSITIVE_HOME_DIRS if d not in excluded]
    # Resolved INLINE, not through _resolved_root_key: that one is bounded for
    # the event loop and RAISES on a stall, and this runs off the loop already
    # (``_sandbox_preflight`` wraps it in ``asyncio.to_thread``). A sandbox mask
    # must be canonical whatever the disk is doing, so the worker that resolves
    # the roots for the gate is called here directly and waits; the spawn side
    # bounds that wait (``_run_preflight_bounded``, 60 s) and refuses the
    # adapter on expiry rather than starting it unmasked (found in review).
    resolved = _resolve_root_anchors(str(Path.home()))
    # BOTH home spellings, reusing the two anchors the read gate already keys on.
    # On a host whose home is itself a symlink (``/home/u`` -> ``/local/home/u``) the
    # resolved and logical spellings differ, and the read gate can absorb that because
    # it realpaths a candidate BEFORE comparing. A sandbox deny list gets no such
    # normalisation -- it denies the paths it is handed -- so denying only the resolved
    # form would leave every credential reachable through the symlinked one.
    home_anchors = {resolved.home, resolved.logical_home}
    targets: set[str] = {
        os.path.join(anchor, *d.split("/")) for anchor in home_anchors for d in leaves
    }
    # KIROCREW_HOME: the crew secrets (signing keys, governance ceiling, .env)
    # live directly under the override, not under either default crew prefix.
    if resolved.crew_home:
        for d in leaves:
            for prefix in _CREW_HOME_PREFIXES:
                if d == prefix or d.startswith(prefix + "/"):
                    leaf = d[len(prefix) :].lstrip("/")
                    targets.add(
                        os.path.join(resolved.crew_home, *leaf.split("/"))
                        if leaf
                        else resolved.crew_home
                    )
                    break
    # An adapter's OAuth token follows that adapter's own home override.
    for leaf, root_fields in _OVERRIDE_ANCHORED_LEAVES:
        if leaf in excluded or leaf not in _SENSITIVE_HOME_DIRS:
            continue
        basename = leaf.split("/")[-1]
        for field in root_fields:
            root = getattr(resolved, field, None)
            if root:
                targets.add(os.path.join(root, basename))
    return tuple(sorted(targets))


def exfil_query_min_len() -> int:
    """Public view of the long-query exfiltration threshold (chars)."""
    return _EXFIL_QUERY_MIN_LEN


# Archive/extraction destination flags (tar -C, unzip -d, rsync dest) pointing
# INTO the governance trust-root parent (the crew data home) — an extraction
# there can drop/overwrite ``security_policy.json`` or a ``profiles/`` entry even
# though the bare home dir is not itself a sensitive-path entry.  Match the
# destination-dir form specifically so normal home access (sessions.db,
# config.json) is not over-blocked.  Covers every crew home root: the current
# ``~/.kiro/crew`` and a pre-move legacy ``~/.kirocrew``.
_CREW_HOME_ALT = "|".join(re.escape("/" + p) for p in _CREW_HOME_PREFIXES)
_EXTRACT_INTO_TRUST_ROOT_RE = re.compile(
    r"-(?:C|d)\s+(?:~|\$HOME|/home/[^/\s]+|/Users/[^/\s]+|"
    + re.escape(str(Path.home()))
    + r")(?:"
    + _CREW_HOME_ALT
    + r")(?:/[^\s]*)?(?:\s|$|['\"])",
    re.IGNORECASE,
)

# The destination half above is deliberately left as the DEFAULT verdict, and
# this carve-out is the only thing that overrides it.  That direction is the
# whole design: ``-d`` is also ``ls``'s "show the directory entry itself, not its
# contents", so the flag-only rule refused a read-only listing of the crew home
# (issue #6021) while the same read spelled ``-l``, ``-lt``, a grep or a Python
# ``open`` stayed allowed -- the flag character was never the boundary.
#
# Two earlier attempts tried to name the WRITERS instead (an archive-program
# word, then that word in "command position").  Both were rejected because the
# set of programs that write through ``-c``/``-C``/``-d``/``-D`` is open-ended,
# so every program not enumerated became a silent permit against the old rule:
# ``patch -d``, ``git -C ... apply`` and ``make -C`` were re-admitted into the
# governance trust root, and a quote-split ``t""ar`` defeated the word match.
# Enumerating writers fails OPEN, which is the wrong direction for this gate.
#
# So the exoneration is an allow-list of READ-ONLY listers instead, and it is
# narrow by construction: a command qualifies only if it has no shell
# composition at all AND its program is one of these.  Anything else -- an
# unknown program, a pipeline, a quoted subshell, a redirection -- keeps the
# destination-half verdict byte-for-byte, so a shape nobody anticipated
# over-blocks rather than opening the trust root.
#
# Every entry here must be a program that cannot WRITE to the directory it is
# given.  Do not add a program because it "usually" reads: ``find -delete`` and
# ``install -d`` are why this is a hand-audited list and not a heuristic.
_TRUST_ROOT_READ_LISTERS: frozenset[str] = frozenset(
    {
        "ls",  # -d: the directory entry itself, the reported false positive
        "stat",
        "du",
        "readlink",
        "basename",
        "dirname",
        "wc",
    }
)
# ``file`` is deliberately ABSENT.  It looks like a pure reader but it is not:
# ``file -C`` compiles a magic database, and ``file -C <crewhome> -m
# <crewhome>/evil.magic`` writes ``evil.magic.mgc`` INTO the trust root while the
# destination half matches on the ``-C`` argument.  That is a real write this
# carve-out would have exonerated.  Every candidate for this set has to be
# checked for a compile/output mode, not just for its usual reading role.

# The exonerated shape is validated POSITIVELY: every character of the command
# must come from a set that carries no meaning to any shell.  This replaced a
# deny-list of metacharacters (``| & ; newline CR backtick < > ( )`` and
# ``$(``), which lost four rounds in a row -- each review found one more spelling
# the screen did not enumerate (a quoted program, an ``&`` inside a quoted
# filename, a bare CR, then a PowerShell parenthesised group that RUNS in
# argument position with no ``$`` sigil).  Enumerating what is dangerous cannot
# terminate against an untrusted string and an unknown target shell; enumerating
# what is INERT does, because a character absent from this set is refused whether
# or not anyone has thought of a way to abuse it.
#
# So the set is deliberately tiny: letters, digits, and the punctuation a path or
# a flag actually needs.  Everything else is out, including quotes, ``$``,
# backslash, glob characters and every bracket -- a bare read listing needs none
# of them.
#
# ``$HOME`` is the ONE exception, stripped before the check, because the
# destination half of this rule enumerates that spelling itself
# (``_EXTRACT_INTO_TRUST_ROOT_RE`` matches ``$HOME`` alongside ``~``), so
# refusing it here would leave half of #6021 unfixed.  It is matched only when
# followed by ``/``, whitespace or end of string, so ``$HOMEX``, ``${HOME}`` and
# ``$(...)`` all keep their ``$`` and are refused.
#
# The residual cost is over-blocking a read whose PATH contains an excluded
# character -- ``ls -d ~/.kiro/crew/foo(1)``, or a quoted path with a space.
# Those keep the destination-half refusal exactly as they did at base: an
# unfixed false positive of the #6021 family, not a new one. Fixing them would
# mean telling a literal parenthesis from a grouping one inside an untrusted
# string, which is the inference this rule has stopped making.
# Named distinctly on purpose: this module already binds ``_HOME_VAR_RE`` further
# down for the normalizer, with different semantics (it also accepts
# ``${HOME}``, case-insensitively).  Reusing that name here silently redefined
# it -- harmless only by accident of definition order -- so this rule carries its
# own anchored spelling and cannot be moved out from under by an edit to the
# other one.
_TRUST_ROOT_HOME_VAR_RE = re.compile(r"\$HOME(?=[/\s]|\Z)")
_SHELL_INERT_COMMAND_RE = re.compile(r"\A[A-Za-z0-9_@%+=:,./~^ \t-]+\Z")


def _is_bare_trust_root_read(command: str) -> bool:
    """True for a single simple command whose program only READS its argument.

    Fails closed on anything it does not recognise, because the caller treats a
    False here as "keep the destination-half refusal".
    """
    # Positive validation first: if the command carries a character that could
    # mean anything to a shell, nothing below is trustworthy.
    if not _SHELL_INERT_COMMAND_RE.match(_TRUST_ROOT_HOME_VAR_RE.sub("", command)):
        return False
    # Past that gate the string provably holds no quote, backslash or
    # metacharacter, so a plain whitespace split IS the tokenisation -- there is
    # no shlex-versus-shell disagreement left to exploit.
    tokens = command.split()
    if not tokens:
        return False
    program = tokens[0]
    # A PATHNAME is never classified, because a basename says nothing about what
    # the binary is: ``/tmp/ls`` and ``./ls`` end in ``ls`` and can write the
    # trust root, so accepting them for the convenience of ``/bin/ls`` would
    # exonerate an attacker-placed executable.  Only a bare command word counts;
    # a path falls through to the destination-half refusal, which over-blocks a
    # legitimate ``/bin/ls`` and is the direction this gate must fail in.
    # A backslash cannot survive the charset above, so only ``/`` needs testing.
    if "/" in program:
        return False
    # A bare word still resolves through PATH at execution time, so this
    # carve-out cannot pin WHICH binary runs -- no string matcher can.  That is
    # not a boundary this rule ever held: a planted shim named ``ls`` in an
    # agent-writable PATH entry executes through every spelling this matcher
    # never sees (``ls``, ``ls -l <crewhome>``), so refusing exactly the
    # ``-d <crewhome>`` form defends nothing against it.  PATH integrity is the
    # write-path policy's boundary, not this matcher's.
    return program.lower() in _TRUST_ROOT_READ_LISTERS


def _extracts_into_trust_root(command: str) -> bool:
    """True when a command writes INTO the crew data home via a dest flag.

    The destination match (:data:`_EXTRACT_INTO_TRUST_ROOT_RE`) is the verdict;
    :func:`_is_bare_trust_root_read` is the single narrow exoneration for the
    read-only listing that flag spelling made indistinguishable from a write.
    """
    if not _EXTRACT_INTO_TRUST_ROOT_RE.search(command):
        return False
    return not _is_bare_trust_root_read(command)


# ── Symlink-staging to a sensitive target via RELATIVE traversal ──
# The home-anchored ~/$HOME/absolute forms of ``ln -sf ~/.aws/credentials link``
# are already caught by _build_sensitive_regex (the sensitive path appears as an
# argument token).  What that matcher CANNOT see is a sensitive dir named through
# pure relative traversal — ``ln -sf ../../../.aws/credentials link`` — because
# it has no home anchor.  Creating such a symlink is the staging step of the
# pentest attack chain (AWS-345 / AWS-62, recommendation item 3): a pre-existing
# link to a credential file lets a later in-workspace read follow it.  We block
# the CREATION verbs (``ln``, ``cp -s``/``--symbolic-link``) when any token
# names a sensitive dir via dot-slash traversal.
_SENSITIVE_SEGMENT_ALT = "|".join(re.escape(d) for d in _SENSITIVE_HOME_DIRS)
# Same alternation with either separator accepted between segments, so the
# Windows-native relative spelling (``..\..\.aws\credentials``) is caught by
# the traversal matcher below alongside the POSIX one. Forward-slash-only
# entries still match (the class includes ``/``), so this strictly widens.
# A repeated separator run is handled by collapsing the SUBJECT before this
# matcher runs, not by admitting a run here (#6350) -- see
# ``_collapse_separator_runs``.
_SENSITIVE_SEGMENT_ALT_ANYSEP = "|".join(
    r"[\\/]".join(re.escape(part) for part in d.split("/"))
    for d in _SENSITIVE_HOME_DIRS
)
_RELATIVE_SENSITIVE_RE = re.compile(
    rf"(?:^|[\s'\"=:,;])(?:\.\.?[\\/])+(?:{_SENSITIVE_SEGMENT_ALT_ANYSEP})"
    rf"(?:[\\/]|\s|$|['\"])",
    re.IGNORECASE,
)


_SEPARATOR_RUN_RE = re.compile(r"[\\/]{2,}")
#: The PUNCTUATION a path token may start after, used together with
#: ``str.isspace()`` to recognise a LEADING separator run (a UNC prefix) as
#: opposed to an interior one. Whitespace is derived rather than enumerated:
#: the hand-written class here used to spell out only space and tab, so a UNC
#: path on a CONTINUATION LINE (newline before it, as in a multi-line
#: PowerShell command) was read as interior, every emitted variant destroyed
#: the UNC anchor, and the doubled spelling of a fenced file was permitted
#: while its single-separator spelling was blocked -- the #6350 class surviving
#: at a newline boundary. ``isspace()`` is exactly the class the patterns
#: themselves accept before a path operand, so the two cannot drift apart
#: again (\r, \v and \f were missing for the same reason).
_PATH_TOKEN_BOUNDARY_PUNCTUATION = "\"'=:,;(<>|&`"


def _separator_collapsed_variants(command: str) -> tuple[str, ...]:
    """Return *command* with separator runs collapsed, one copy per spelling.

    Win32 collapses a repeated separator run, so ``%LOCALAPPDATA%\\\\kiro-cli``
    and ``%LOCALAPPDATA%\\kiro-cli`` open the same file. The fence matches raw
    text, so without this the doubled spelling named a fenced store while
    matching no branch (#6350).

    Collapsing is done to the SUBJECT rather than by admitting a run in the
    patterns, because a run inside the patterns is a denial-of-service on this
    gate: it appears both inside the starred generalized separator and after it,
    so the engine walks the splits and re-consumes the whole run at every start
    offset (measured 33s on 6,000 backslashes against 1.3s on base, past the 25s
    watchdog).

    Up to FOUR copies, along two axes, because a single rewrite loses cases:

    * **Which separator.** Not every pattern accepts either character -- the
      resolved home literal is ``re.escape``-d and requires the platform's exact
      separator -- so collapsing to one fixed character left a MIXED run
      (``D:/\\profiles\\u``) matching neither spelling (found in review).
    * **Whether a LEADING run stays a pair.** A UNC path begins with two
      separators that its anchor requires, so collapsing them broke every UNC
      spelling that ALSO had an interior run:
      ``\\\\server\\share\\.kiro\\\\crew\\security_policy.json`` matched neither
      the original (interior run) nor the collapsed copy (no UNC prefix left),
      and the keystone read was permitted (found in review). The boundary form
      keeps a run that starts a token at two characters and still collapses the
      interior ones.

    Empty tuple when there is no run to collapse, so the common command costs one
    search and nothing else. Duplicates are dropped, so a command with only
    interior backslash runs yields two copies rather than four.
    """
    if not _SEPARATOR_RUN_RE.search(command):
        return ()

    variants: list[str] = []
    for sep in ("/", "\\"):
        for keep_leading_pair in (False, True):

            def _replace(
                match: "re.Match[str]",
                sep: str = sep,
                keep_leading_pair: bool = keep_leading_pair,
            ) -> str:
                start = match.start()
                prev = command[start - 1] if start else ""
                leading = start == 0 or prev.isspace() or prev in _PATH_TOKEN_BOUNDARY_PUNCTUATION
                return sep * 2 if (keep_leading_pair and leading) else sep

            variant = _SEPARATOR_RUN_RE.sub(_replace, command)
            if variant != command and variant not in variants:
                variants.append(variant)
    return tuple(variants)


def _fence_hit_in_collapsed(command: str) -> str | None:
    """The three pass-1b checks over the separator-collapsed copies of ``command``.

    Only the COLLAPSED copies are checked, never the unmodified command: pass 1 put
    those exact bytes through these same three matchers and missed, and the
    sensitive-path regex over a long newline-free line is the most expensive matcher
    on this gate, so re-running it here doubled the wall time of a linearity-guarded
    path for no coverage.

    The path check is ``_sensitive_pattern_hit``, not ``_get_sensitive_re().search``:
    the verb-anchored branch is no longer spelled inside that alternation, so
    searching the compiled pattern alone would let a verb-anchored fenced path reach
    the fence through a doubled separator -- the exact shape this layer exists for.
    """
    for candidate in _separator_collapsed_variants(command):
        reason = _fence_hit(candidate)
        if reason:
            return reason
    return None


def _fence_hit(value: str) -> str | None:
    """The three fence checks over EXACTLY ``value`` -- no variants, no collapsing.

    Factored out so the fence has ONE spelling across its two callers, which need
    different subjects: :func:`_fence_hit_in_collapsed` deliberately skips the
    unmodified command (pass 1 already scanned those bytes), while pass 1c hands in an
    assignment-resolved view that NO earlier pass has seen and so must be checked as
    given, normalized copies included.
    """
    if _sensitive_pattern_hit(value):
        return "Blocked: command accesses sensitive credential path"
    if _extracts_into_trust_root(value):
        return "Blocked: command extracts into the governance trust-root directory"
    if _RELATIVE_SENSITIVE_RE.search(value):
        return (
            "Blocked: command references a sensitive credential path "
            "via relative traversal"
        )
    return None


def is_sensitive_bash_command(
    command: str,
    *,
    enabled_ids: "frozenset[str] | None" = None,
) -> str | None:
    """Check if a bash command reads sensitive paths, accesses IMDS, or leaks env creds.

    The subject is a SHELL COMMAND LINE. Every pass below reads it with shell grammar
    -- separator runs are redundant, newlines and ``|`` split pipeline stages, an
    ``env | grep`` pipeline is one command -- and none of that holds for a Python
    source file. A caller with a source body in hand must not route it here: it was
    tried (#7912, #8563, #8643, #8812) and every shell pass produced a class of false
    denial on ordinary scripts, each closed by a further layer of AST analysis that
    still could not tell ``open(a + b)`` from ``re.compile(a)``. The cron script gate
    (``mcp_cron._vet_script_contents``) now runs only full-text detectors that are
    meaningful on source, and the sandbox is the runtime control for what a script may
    open.

    Matches the subject against the literal fence: the sensitive-path patterns, the
    trust-root extraction control and the relative-traversal matcher, each of which
    names a path it can point at. It does NOT simulate the shell to work out where a
    traversal would end up -- the OS sandbox confines the agent process, and the
    keystone paths are unreadable and unwritable there whatever spelling reaches them,
    so a text simulation of ``cd``, variable expansion, brace expansion and ``find``
    filter grammar bought false denials on read-only commands rather than protection.

    NO indirection is resolved here, and that is a deliberate boundary rather than a
    gap left open. This gate reads the command TEXT, while a shell subprocess reaches a
    file through an ``open()`` that never routes through the tool gate at all -- so a
    path fenced only here is readable through any sandbox mode, whatever this function
    matches. Resolving one spelling (a local assignment, a ``/./`` segment, a ``cd``
    into a fenced directory, an operator welded to its operand) therefore does not
    bound anything: it narrows an unbounded set of spellings by one, and the next
    spelling arrives with the next shell feature. The enforcement that DOES bound the
    subprocess is the OS layer in :mod:`kiro_crew.sandbox`, which gives every crew-home
    leaf one of three dispositions -- HIDDEN (bind-masked in every mode, which is what
    the credential homes get), READONLY (in-sandbox code reads it and a write would let
    the agent choose its own ceiling), or VISIBLE -- and masks the credential homes out
    of the subprocess tree entirely.

    Between the matchers runs **pass 1b**, which repeats the pass-1 matchers over
    separator-run-COLLAPSED copies of the subject. That is a Win32 *shell grammar*
    heuristic: a shell opens the store ``%LOCALAPPDATA%\\kiro-cli`` names when
    handed ``%LOCALAPPDATA%\\\\kiro-cli``, so the run carries no meaning and
    collapsing it closes the doubled spelling (#6350).

    Returns denial reason string, or None if clean.
    """
    # ── Pass 0: size ceiling ──
    # Every matcher below is linear in the subject, and this bound is what makes
    # that a wall-clock ceiling: the gate runs synchronously on the event loop,
    # so its worst case IS the loop's worst case. An oversized subject is
    # refused, never scanned partially and never let through unscanned -- a
    # denied long command is recoverable by the operator, a stalled gateway and
    # an unscanned command are not.
    if len(command) > MAX_SCANNABLE_COMMAND_CHARS:
        return _oversize_refusal(len(command), MAX_SCANNABLE_COMMAND_CHARS)
    # ── Pass 1: regex fast-path ──
    if _sensitive_pattern_hit(command):
        return "Blocked: command accesses sensitive credential path"
    if _extracts_into_trust_root(command):
        return "Blocked: command extracts into the governance trust-root directory"
    # Block ANY command referencing a sensitive path via relative traversal,
    # regardless of verb.  The home-anchored/absolute forms are already caught
    # by the matcher above; this covers the relative-traversal forms that escape
    # it (was gated on ln/cp only, so dd/base64/xxd/head/tail slipped past).
    if _RELATIVE_SENSITIVE_RE.search(command):
        return "Blocked: command references a sensitive credential path via relative traversal"

    # ── Pass 1b: the pass-1 matchers again over separator-COLLAPSED copies ──
    # Win32 collapses a repeated separator run, so ``%LOCALAPPDATA%\\kiro-cli``
    # opens the fenced store that ``%LOCALAPPDATA%\kiro-cli`` names -- and the
    # patterns above spell one separator, so the doubled form matched no branch
    # (#6350). Collapsing the subject closes that for every run length at linear
    # cost; admitting a run in the patterns instead was measured as a
    # watchdog-crossing hang on this gate (see ``_separator_collapsed_variants``).
    #
    # ALL THREE pass-1 checks are repeated, not just the path matcher: the
    # extraction check is a separate control, and omitting it let
    # ``tar -xf evil.tar -C $HOME//.kiro/crew`` overwrite governance files
    # through the doubled separator (found in review).
    #
    # Run only after the original missed, so nothing that needs the run intact
    # (a UNC ``\\server\share`` anchor) loses its match.
    collapsed_reason = _fence_hit_in_collapsed(command)
    if collapsed_reason:
        return collapsed_reason

    # ── Pass 2: native-shell entry-then-relative-read scan ──
    native_result = _check_native_home_entry_then_fenced_read(command)
    if native_result:
        return native_result

    # No traversal SIMULATION runs here. Working out where `find`, `grep -r`, a brace
    # expansion or a `cd` chain would land requires re-implementing shell and
    # find-utils grammar in regex, and the passes that did it denied ordinary
    # read-only commands (`grep -r pattern .`, `find . -name '*.py'`) far more often
    # than they caught an access the fence did not already name. The fence itself is
    # enforced where it cannot be talked around: the keystone paths are refused by
    # `is_sensitive_path` on every resolved path this gate's callers open, and by the
    # OS sandbox for the agent process as a whole.

    # IMDS access via any IP encoding (decimal, hex, octal, IPv6-mapped)
    imds_result = _check_imds_access(command, enabled_ids=enabled_ids)
    if imds_result:
        return imds_result

    # Environment credential exfiltration (declare -p, env|grep, printenv, etc.)
    env_result = _check_env_credential_access(command)
    if env_result:
        return env_result
    return None


# `NAME=value` prefix. `normalize_shell_command` keeps it as a single token, and
# the value is already $HOME-expanded by the time we see it.
#: ``NAME=value`` and ``NAME+=value``. The append form is a separate group so a
#: caller can add to what it already recorded instead of replacing it. Matching
#: only ``=`` means the whole ``NAME+=`` token fails to match, so the segment
#: reads as a command word rather than an assignment.
_SHELL_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)$", re.DOTALL)


#: Every spelling of "change the working directory" that
#: `_check_native_home_entry_then_fenced_read` has to recognise, in ONE place.
#:
#: This gate runs on the raw command string of every shell tool call, whatever
#: shell will execute it -- ``hooks.py`` hands it the title and the command with
#: no per-platform branch -- and the absolute-path pass in
#: `_build_sensitive_regex` already models cmd.exe and PowerShell spellings
#: (``%USERPROFILE%``, ``$env:USERPROFILE``, backslash separators). A bash-only
#: verb list would leave the Windows spelling of the enter-then-relative-read
#: chain unmodelled and reading clean::
#:
#:     Set-Location ~; Get-Content .aws/credentials
#:     chdir %USERPROFILE%; type .kiro/crew/token_signing.key
#:
#: ``sl`` is also a real (joke) program on some Linux boxes. Reading it as a
#: chdir there can only ADD an entry into the home directory, and an entry only
#: ever produces more denials, so the collision fails in the safe direction --
#: the same posture the rest of this gate takes, where naming a fenced path is
#: itself the signal.
#:
#: ``popd`` / ``Pop-Location`` are deliberately absent. This pass asks only
#: whether an entry into the home directory was seen ANYWHERE on the line, so
#: modelling a verb that undoes a move could only walk a denial back.
_CHDIR_VERBS: frozenset[str] = frozenset(
    {
        "cd",  # bash builtin; also a PowerShell alias and a cmd.exe builtin
        "pushd",  # bash builtin; PowerShell alias of Push-Location; cmd.exe
        "chdir",  # cmd.exe builtin; PowerShell alias of Set-Location
        "sl",  # PowerShell alias of Set-Location
        "set-location",
        "push-location",
    }
)


#: Parent directories of every MULTI-SEGMENT sensitive entry, i.e. the directories
#: whose sensitivity lives in their leaves rather than in themselves.
#:
#: ``~/.aws`` is sensitive as a whole directory, so `is_sensitive_path` answers
#: "yes" for it. ``~/.kiro/crew`` is not: only ``~/.kiro/crew/token_signing.key``
#: and its siblings are. Any check that asked `is_sensitive_path` about a
#: DIRECTORY therefore answered "no" for the keystone -- and ``~/.aws`` hid it,
#: because every test written against that spelling passed.
#:
#: Derived from the single-segment / multi-segment split in the list itself rather
#: than hand-listed, so a new keystone secret is covered the day it is added.
#: General-purpose directories whose only sensitive content is a specific child
#: (e.g. `.config/gcloud`, `.docker/config.json`).  These routinely hold benign
#: content (`~/.config/starship.toml`, `~/.docker/daemon.json`, `~/.kube/cache/`),
#: so tainting a `cd` into them produces false positives.  Exclude them from the
#: parent set; their sensitive CHILDREN are still protected by `is_sensitive_path`.
_GENERAL_PURPOSE_PARENT_DIRS: frozenset[str] = frozenset(
    {
        ".config",
        ".docker",
        ".kube",
        ".kiro",
        ".local/share",
    }
)

#: Single-segment entries are excluded on purpose: their parent is the home
#: directory, and treating ``~`` as holding a secret would taint ``cd ~``.
_SENSITIVE_LEAF_PARENT_DIRS: list[str] = sorted(
    {d.rsplit("/", 1)[0] for d in _SENSITIVE_HOME_DIRS if "/" in d}
    - _GENERAL_PURPOSE_PARENT_DIRS
)


#: ---------------------------------------------------------------------------
#: Native-shell path reading: cut into words, NORMALIZE, compare.
#:
#: The first six review rounds of this pass were spent matching SPELLINGS, and the
#: boundary half of that converged -- one lookaround on "what a path is" replaced a
#: growing list of punctuation. The PATH half never converged, for a reason worth
#: writing down rather than rediscovering: ``%USERPROFILE%\.``, ``~/``, ``C:.aws``,
#: ``C:/`` versus ``C:\``, ``a\..\``, ``a\b\..\..\`` and ``.aw^s`` all name ONE
#: file. Path identity is a COMPUTATION -- collapse ``.``, net ``..`` against
#: depth, unify separators, apply the shell's escape -- and a pattern can only
#: enumerate the spellings someone thought to write down, so every round supplied
#: one more.
#:
#: So this scan no longer matches path spellings. It cuts the command into words,
#: normalizes each word as a path, and compares the RESULT. An unbounded family of
#: patterns becomes one bounded function, and the shells' escape characters stop
#: being special cases -- see `_native_words`, which owns quoting and escaping so
#: path shaping never has to.
#:
#: It stays grammar-free, which is the property that made this pass worth having: a
#: word ends at an operator, but WHICH operator -- sequencer, background, pipe,
#: redirect -- is never asked. Only target selection stays inside one
#: operator-delimited run; the fenced-path search deliberately crosses every
#: boundary, because a monotone scan may only ever widen.
_WORD_SPACE = frozenset(" \t")
_WORD_QUOTE = frozenset("\"'")
#: The two shells' escape characters: cmd.exe uses ``^``, PowerShell uses a
#: backtick. Both protect exactly the next character, INCLUDING a space, so they
#: belong to the word layer rather than to path normalization. The backtick is
#: therefore not an operator here even though bash reads it as command
#: substitution -- this is the native-Windows pass, and bash's substitution is
#: handled by the segment splitter the earlier passes use.
_WORD_ESCAPE = frozenset("^`")
#: Braces are deliberately NOT boundaries: PowerShell spells an environment
#: variable ``${env:USERPROFILE}``, so splitting on ``{`` would cut a home anchor
#: in half. A brace-delimited block (``ForEach-Object { ... }``) is already broken
#: by the ``|`` or ``;`` in front of it, so nothing needs them to end a run.
_WORD_OPERATOR = frozenset(";&|()<>\r\n")

_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
#: A flag carrying its value in the same word (``-Path:~``, ``--path=~``, ``/D:x``).
#: The payload after the first ``:`` or ``=`` is what names the directory.
_BOUND_PAYLOAD_RE = re.compile(r"^[-/][A-Za-z][\w-]*[:=](?P<value>.+)$")
_SWITCH_WORD_RE = re.compile(r"^[-/][A-Za-z][\w-]*(?:[:=]\S*)?$")
_GLUED_SWITCH_RE = re.compile(r"^[A-Za-z]$")

#: One path SEGMENT naming the home directory, in every spelling these shells
#: accept -- including cmd.exe delayed expansion (``!USERPROFILE!``) and the
#: optional substring/substitution payload after the first delimiter. Anchor
#: spellings ARE a closed set, because the shells define them; that is why an
#: alternation is the right tool for this part and the wrong one for path identity.
_HOME_SEGMENT_RE = re.compile(
    r"^(?:"
    r"~"
    r"|\$HOME|\$\{HOME\}"
    r"|%USERPROFILE(?::[^%]*)?%"
    r"|!USERPROFILE(?::[^!]*)?!"
    r"|(?:%HOMEDRIVE(?::[^%]*)?%|!HOMEDRIVE(?::[^!]*)?!)"
    r"(?:%HOMEPATH(?::[^%]*)?%|!HOMEPATH(?::[^!]*)?!)"
    r"|\$\{env:USERPROFILE\}|\$env:USERPROFILE"
    r"|\$\{env:HOMEDRIVE\}\$\{env:HOMEPATH\}|\$env:HOMEDRIVE\$env:HOMEPATH"
    r")$",
    re.IGNORECASE,
)

_CHDIR_VERBS_LOWER = frozenset(verb.lower() for verb in _CHDIR_VERBS)


class _PathShape(NamedTuple):
    """What one word means as a path, after normalization."""

    segments: tuple[str, ...]
    home_anchored: bool
    absolute: bool
    #: A ``..`` climbed above the directory the path was spelled from, so it names
    #: something OUTSIDE that directory. This scan has no claim on those: denying
    #: ``project\..\..\.aws`` would deny a different file than the fenced one.
    escaped: bool


def _strip_windows_component_padding(segment: str) -> str:
    """Windows drops trailing dots and spaces from every path component.

    So ``.aws.`` and ``.aws`` are the SAME directory, and ``type .aws.\\credentials``
    after entering home genuinely reads the credential -- a whole-segment
    comparison would otherwise let the trailing dot walk past every entry in
    `_SENSITIVE_HOME_DIRS` at once.

    A segment made only of dots is left alone: ``.`` and ``..`` are navigation
    rather than a name carrying padding, and stripping them would erase the very
    netting that decides whether a path escapes its directory.
    """
    if not segment.strip("."):
        return segment
    return segment.rstrip(". ")


def _native_words(command: str) -> list[tuple[int, str, bool]]:
    """Cut the command into ``(offset, word, starts_new_run)`` triples.

    This layer implements the shells' QUOTING and ESCAPING, and nothing else. It
    used to approximate them -- quotes were skipped and escapes were removed later,
    during path shaping -- and each approximation cost a review round: a quoted
    ``C:\\Users\\John Doe`` was split at the space, a fenced entry with a space of
    its own was too, and PowerShell's backtick escape went unread while cmd.exe's
    caret was handled. All three are the same omission, so they are fixed in the
    same place.

    The rules, both closed sets the shells define:

    * An escape (``^`` in cmd.exe, a backtick in PowerShell) protects exactly the
      next character, which reaches the word while the escape does not. A doubled
      escape therefore yields one literal escape character, so ``.a^^ws`` stays the
      distinct file ``.a^ws``.
    * A quote does not reach the word. Whitespace inside one is AMBIGUOUS in a way
      no amount of lexing settles: ``"C:\\Users\\John Doe"`` is one path, while
      ``cmd /C "cd ~ & type .aws\\credentials"`` is a whole command line that must
      still be cut apart. So both readings are emitted -- the whitespace-separated
      parts AND, when a quoted region holds whitespace, the joined region as one
      extra word. Taking both is sound precisely because this scan is monotone:
      an extra reading can only add a denial, never remove one.

    ``starts_new_run`` is True when an operator (or the start of the command)
    preceded the word -- the only structural fact this scan needs, since a chdir's
    target cannot live across an operator while the fenced-path search ignores
    operators entirely.
    """
    words: list[tuple[int, str, bool]] = []
    buffer: list[str] = []
    start = -1
    word_new_run = True
    next_new_run = True
    quote = ""
    region: list[str] = []
    region_start = -1
    index = 0
    length = len(command)

    def begin(at: int) -> None:
        nonlocal start, word_new_run, next_new_run
        if not buffer:
            start = at
            word_new_run = next_new_run
            next_new_run = False

    def flush() -> None:
        nonlocal buffer
        if buffer:
            words.append((start, "".join(buffer), word_new_run))
            buffer = []

    while index < length:
        char = command[index]
        if char in _WORD_ESCAPE:
            index += 1
            if index < length:
                begin(index)
                buffer.append(command[index])
                if quote:
                    region.append(command[index])
                index += 1
            continue
        if char in _WORD_QUOTE:
            if quote == char:
                joined = "".join(region)
                if any(space in joined for space in _WORD_SPACE):
                    words.append((region_start, joined, False))
                quote = ""
                region = []
            elif not quote:
                quote = char
                region = []
                region_start = index + 1
            else:
                begin(index)
                buffer.append(char)
                region.append(char)
            index += 1
            continue
        if char in _WORD_SPACE or char in _WORD_OPERATOR:
            flush()
            if char in _WORD_OPERATOR:
                next_new_run = True
            if quote:
                region.append(char)
            index += 1
            continue
        begin(index)
        buffer.append(char)
        if quote:
            region.append(char)
        index += 1
    flush()
    if quote:
        joined = "".join(region)
        if any(space in joined for space in _WORD_SPACE):
            words.append((region_start, joined, False))
    return words


def _shape_path_token(word: str) -> _PathShape:
    """Normalize one word as a path and report what it names.

    Every equivalence that cost a review round is decided here, once: a bound flag
    payload, a drive-relative prefix, either separator, a no-op ``.``, the trailing
    dots Windows itself drops, and ``..`` netted against depth so
    ``a\\b\\..\\..\\.aws`` and ``a\\..\\.aws`` and ``.aws`` all arrive as the same
    segments.

    Escapes and quoting are deliberately NOT handled here -- `_native_words` owns
    them. Keeping the two layers separate is what makes the split safe: an escape
    removed twice would turn ``.a^^ws`` (a real file named ``.a^ws``) into the
    fenced ``.aws`` and deny a command that touches nothing.
    """
    text = word
    bound = _BOUND_PAYLOAD_RE.match(text)
    if bound:
        text = bound.group("value")
    absolute = False
    if _DRIVE_PREFIX_RE.match(text):
        # A drive letter FOLLOWED by a separator is absolute on that drive. With
        # nothing after it, it means "the current directory there" -- which is
        # precisely the relative form this scan exists for, so the prefix is
        # dropped rather than treated as evidence the path is not relative.
        absolute = text[2:3] in ("/", "\\")
        text = text[2:]
    text = text.replace("\\", "/")
    if text.startswith("/"):
        absolute = True
    raw = [
        _strip_windows_component_padding(segment)
        for segment in text.split("/")
    ]
    raw = [segment for segment in raw if segment not in ("", ".")]
    home_anchored = False
    if raw and _HOME_SEGMENT_RE.match(raw[0]):
        home_anchored = True
        raw = raw[1:]
    stack: list[str] = []
    escaped = False
    for segment in raw:
        if segment == "..":
            if stack:
                stack.pop()
            else:
                escaped = True
        else:
            stack.append(segment)
    return _PathShape(tuple(stack), home_anchored, absolute, escaped)


def _names_home_directory(word: str) -> bool:
    """Does this word name the home directory ITSELF, not something under it?

    ``cd ~/project`` is deliberately False: entering a subdirectory of home is not
    entering home, and a later ``.aws`` there resolves to ``~/project/.aws``, which
    is not fenced. That distinction used to be a lookahead refusing a path
    continuation; it is now just "no segments left after the anchor".

    The resolved home is read PER CALL. Binding it at import time freezes it for
    the life of the process, which `test_host_isolation_floor`'s shared-path
    ratchet forbids and which would make a repointed home invisible here.
    """
    shape = _shape_path_token(word)
    if shape.escaped:
        return False
    if shape.home_anchored:
        return not shape.segments
    if shape.absolute:
        # Windows paths are case-insensitive, so `c:\users\u` is the same entry as
        # `C:\Users\u`. `_fenced_relative_prefix` and `_HOME_SEGMENT_RE` already
        # fold; this was the one comparison in the pass that did not, which made
        # a case-varied spelling of the resolved home invisible.
        home = _shape_path_token(str(Path.home()))
        return (
            shape.absolute == home.absolute
            and shape.home_anchored == home.home_anchored
            and tuple(segment.lower() for segment in shape.segments)
            == tuple(segment.lower() for segment in home.segments)
        )
    return False


def _fenced_relative_prefix(shape: _PathShape) -> str | None:
    """Which fenced directory a RELATIVE path names, if any.

    Read from `_SENSITIVE_HOME_DIRS` at call time, not folded into a pattern at
    import, so leaves appended to that list later (the crew data-home secrets) are
    covered with no second edit. Segments are compared whole, which is what keeps
    ``x.aws/credentials`` and ``.npmrcnotes`` out.
    """
    if shape.absolute or shape.home_anchored or shape.escaped or not shape.segments:
        return None
    lowered = tuple(segment.lower() for segment in shape.segments)
    for fenced in _SENSITIVE_HOME_DIRS:
        parts = tuple(part.lower() for part in fenced.split("/") if part)
        if parts and lowered[: len(parts)] == parts:
            return fenced
    return None


def _is_chdir_verb_word(word: str) -> bool:
    """A change-directory verb, including cmd.exe's glued ``cd/d``.

    Sourced from the same `_CHDIR_VERBS` the walk reads, so a spelling added there
    reaches this scan with no second edit.
    """
    base, slash, glued = word.partition("/")
    if base.lower() not in _CHDIR_VERBS_LOWER:
        return False
    return not slash or bool(_GLUED_SWITCH_RE.match(glued))


def _chdir_target_is_home(words: list[tuple[int, str, bool]], verb_index: int) -> bool:
    """Within the verb's own run, does any word name the home directory?

    The whole run is scanned rather than a bounded window of candidates. A window
    was wrong for a nameable reason: a PowerShell parameter can take its value as a
    separate word, so an arbitrary number of words can sit between the verb and its
    positional target (``Set-Location -ErrorAction Stop -WarningAction Stop ~``),
    and any cap stops short of some legitimate spelling. Scanning the run cannot
    over-reach, because an operator ends the run and the fenced-path search -- which
    is the half that actually decides a denial -- is monotone anyway.

    The order of the two checks below is LOAD-BEARING and easy to invert by
    accident. ``-Path:~`` satisfies both predicates -- it looks like a switch AND it
    names home, because the shape reads the payload after the colon. The home check
    therefore has to run FIRST; skipping switches first would silently stop
    detecting a parameter-bound target.

    The running JOIN exists for one cmd.exe quirk: ``cd`` takes the rest of the line
    as its path, so ``cd /d C:\\Users\\John Doe`` is a valid entry with no quotes at
    all. Switch-shaped words are left out of the join so a leading ``/d`` does not
    poison it.
    """
    parts: list[str] = []
    for _offset, word, new_run in words[verb_index + 1 :]:
        if new_run:
            return False
        if _names_home_directory(word):
            return True
        if _SWITCH_WORD_RE.match(word):
            continue
        parts.append(word)
        if len(parts) > 1 and _names_home_directory(" ".join(parts)):
            return True
    return False


def _check_native_home_entry_then_fenced_read(command: str) -> str | None:
    """Did the command enter the home directory, then name a fenced path relative to it?

    The removed normalizer answered "where is the shell now" by WALKING the command:
    split into segments, track the chdir target, join relative operands onto it. That
    walk had to agree with the shell's grammar, and for a NATIVE WINDOWS command line it
    did not. POSIX-mode tokenization treats a backslash as an escape rather than a
    separator and a single ``&`` as backgrounding rather than sequencing; a segment
    split rightly declines to break on ``|`` because in bash the ``cd`` would run in a
    subshell, while a PowerShell pipeline does move the directory; and cmd.exe's ``^``
    escape and glued ``/D`` switch are two more spellings a POSIX tokenizer reads as
    something else.

    Closing those one at a time is unbounded -- four consecutive review rounds each
    named one more element, and the whole walk is gone for that reason. So this pass
    asks the question that needs NO grammar: was an entry into
    the home directory seen ANYWHERE, and does a fenced path spelled relative to
    it appear AFTER that? Both halves are read off the raw text::

        cd ~ & type .aws\\credentials                    sequencer + separator
        Set-Location ~ | ForEach-Object { cat .aws/x }   pipeline
        cd/d %USERPROFILE% && type .aws^\\credentials     glued switch + caret

    Monotone and position-ordered: once the entry is seen no later token can
    clear it, which is exactly the property a positional tracker lacks.

    Cost, stated plainly: naming a fenced RELATIVE path after entering the home
    directory is denied even when the command would not have read it (a
    ``grep`` for that text in a note). That is the posture the absolute-path pass
    already takes -- naming a fenced path is itself the signal -- extended to the
    spelling that only makes sense once the shell is in the home directory.

    Returns a denial reason, or None when clean.
    """
    words = _native_words(command)
    entry_offset: int | None = None
    for index, (offset, word, _new_run) in enumerate(words):
        if not _is_chdir_verb_word(word):
            continue
        # A bare chdir -- nothing after it in its own run -- lands in the home
        # directory in every shell this pass covers.
        bare = index + 1 >= len(words) or words[index + 1][2]
        if bare or _chdir_target_is_home(words, index):
            entry_offset = offset
            break
    if entry_offset is None:
        return None
    for offset, word, _new_run in words:
        if offset <= entry_offset:
            continue
        fenced = _fenced_relative_prefix(_shape_path_token(word))
        if fenced is not None:
            return (
                "Blocked: command enters the home directory and then names a "
                f"sensitive credential path relative to it ({fenced})"
            )
    return None


# ── URL Exfiltration Detection ──
# Detects URLs whose path/query contain credential-like data. We flag the
# PAYLOAD, not the destination: any URL with secrets is suspicious regardless of
# host. The general redactors have one narrow carve-out for companion-supplied
# exact tenant hosts. A separate, opt-in carve-out for standard OAuth params is
# available only to ``oauth_url_contains_credential`` on the ACP banner path.
# Fixed/encoded credentials and heavy percent encoding remain unconditional.

# Host group (group 1) matches THREE host shapes so a raw-IP exfil destination
# is not silently skipped: a DNS name with a letter TLD, a raw
# IPv4 literal (``192.168.1.1``, incl. link-local/metadata ``169.254.169.254``),
# or a bracketed IPv6 literal (``[::1]``, ``[fd00::1]``). The prior regex required
# a ``.<letters>`` TLD, so ``http://169.254.169.254/latest/…/<secret>`` never
# matched _URL_RE and its path/query was never scanned. Group 3 stays the
# path+query so the scan/redact call sites are unchanged.
_URL_RE = re.compile(
    r"https?://"
    r"("
    r"[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}"  # DNS name with a letter TLD
    r"|\d{1,3}(?:\.\d{1,3}){3}"  # raw IPv4 literal
    r"|\[[0-9A-Fa-f:.]+\]"  # bracketed IPv6 literal (incl. IPv4-mapped ::ffff:d.d.d.d)
    # Group 3 = path AND/OR query. It must start with ``/`` (path) OR ``?``
    # (a query attached directly to the host, no path segment). The prior
    # ``/[...]*`` required a leading slash, so ``https://host?leak=<secret>``
    # yielded group(3)=None and both scan/redact bailed on ``qmark == -1``,
    # never inspecting the query — a real exfil bypass. ``[/?]`` admits both;
    # the ``path_and_query.find("?")`` split at the call sites is unchanged.
    r")(:\d+)?([/?][^\s)\"'>]*)?"
)

# Query string length threshold — normal URLs rarely exceed this
_EXFIL_QUERY_MIN_LEN = 200

# Patterns that indicate secrets or encoded data in query params
_EXFIL_PATTERNS = re.compile(
    r"(?:"
    r"[A-Za-z0-9+/=]{40,}"  # base64-like blob (40+ chars)
    r"|%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}"  # heavy URL-encoding (20+ encoded chars)
    f"|{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)

# Heavy URL-encoding detector — the same "20+ consecutive percent-encoded
# octets" branch carved out of _EXFIL_PATTERNS. This stays UNCONDITIONAL: the
# context-specific exemptions below skip only the base64-blob and query-length
# heuristics (which false-positive on legitimate document pointers or banner
# state/PKCE), NOT this detector, so a heavily encoded payload is still caught.
_EXFIL_PERCENT_RE = re.compile(
    r"%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}",
    re.IGNORECASE,
)

# Percent-decoding passes applied when re-scanning a URL for encoded
# credentials. More than one is required because a double-encoded payload
# survives a single pass; the bound stops a deliberately over-encoded URL from
# making the scan loop indefinitely.
_MAX_URL_DECODE_PASSES = 3

_OAUTH_DIAGNOSTIC_PARAMETER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_OAUTH_URL_SYMBOLS = frozenset("-._~:/?#[]@!$&'()*+,;=")


@dataclass(frozen=True)
class OAuthUrlShapeProfile:
    """Non-sensitive character-class profile for one rejected URL component."""

    length: int
    ascii_uppercase: int
    ascii_lowercase: int
    digits: int
    percent_signs: int
    symbols: int
    other: int


@dataclass(frozen=True)
class OAuthUrlCredentialDiagnostic:
    """Privacy-safe explanation of the first OAuth URL rejection rule."""

    rule: str
    component: str
    parameter: str | None
    shape: OAuthUrlShapeProfile

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _oauth_char_class(char: str) -> str:
    if char in string.ascii_uppercase:
        return "ascii_uppercase"
    if char in string.ascii_lowercase:
        return "ascii_lowercase"
    if char in string.digits:
        return "digits"
    if char == "%":
        return "percent_signs"
    if char in _OAUTH_URL_SYMBOLS:
        return "symbols"
    return "other"


def _oauth_shape_profile(value: str) -> OAuthUrlShapeProfile:
    counts = Counter(_oauth_char_class(char) for char in value)
    return OAuthUrlShapeProfile(
        length=len(value),
        ascii_uppercase=counts["ascii_uppercase"],
        ascii_lowercase=counts["ascii_lowercase"],
        digits=counts["digits"],
        percent_signs=counts["percent_signs"],
        symbols=counts["symbols"],
        other=counts["other"],
    )


def _safe_oauth_parameter_name(name: str | None) -> str | None:
    if (
        name is None
        or name not in _OAUTH_QUERY_PARAMS
        or not _OAUTH_DIAGNOSTIC_PARAMETER_RE.fullmatch(name)
    ):
        return None
    if _contains_fixed_credential(name) or _text_contains_bare_secret(name):
        return None
    return name


def _oauth_diagnostic(
    rule: str,
    component: str,
    value: str,
    *,
    parameter: str | None = None,
) -> OAuthUrlCredentialDiagnostic:
    return OAuthUrlCredentialDiagnostic(
        rule=rule,
        component=component,
        parameter=_safe_oauth_parameter_name(parameter),
        shape=_oauth_shape_profile(value),
    )


def _oauth_query_diagnostic(
    rule: str,
    query: str,
    *,
    predicate: Callable[[str], bool] | None = None,
    decoder: Callable[[str], str] | None = None,
    fallback: bool = True,
) -> OAuthUrlCredentialDiagnostic | None:
    segments = query.split("&")
    for segment in segments:
        key, separator, value = segment.partition("=")
        if not separator:
            continue
        candidate = decoder(value) if decoder is not None else value
        if predicate is not None and predicate(candidate):
            return _oauth_diagnostic(
                rule, "query_parameter", candidate, parameter=key
            )
        if predicate is None and len(segments) == 1:
            return _oauth_diagnostic(
                rule, "query_parameter", candidate, parameter=key
            )
    if not fallback:
        return None
    target = decoder(query) if decoder is not None else query
    return _oauth_diagnostic(rule, "query", target)


def _oauth_url_payload_diagnostic(
    rule: str,
    url: str,
    target: str,
    predicate: Callable[[str], bool],
    *,
    decoder: Callable[[str], str] | None = None,
) -> OAuthUrlCredentialDiagnostic:
    try:
        parsed = urlparse(url)
        if parsed.query:
            query_diagnostic = _oauth_query_diagnostic(
                rule,
                parsed.query,
                predicate=predicate,
                decoder=decoder,
                fallback=False,
            )
            if query_diagnostic is not None:
                return query_diagnostic
        for component, value in (
            ("scheme", parsed.scheme),
            ("authority", parsed.netloc),
            ("path", parsed.path),
            ("path_params", parsed.params),
            ("fragment", parsed.fragment),
        ):
            candidate = decoder(value) if decoder is not None else value
            if candidate and predicate(candidate):
                return _oauth_diagnostic(rule, component, candidate)
    except Exception:
        pass
    return _oauth_diagnostic(rule, "url", target)


# Exact, code-owned OAuth authorization endpoints whose standard front-channel
# parameters may legitimately contain high-entropy state/PKCE values on the ACP
# banner-safety path. This is deliberately NOT configurable and never uses
# suffix matching: an agent-owned
# setting or ``api.notion.com.attacker.example`` must not lower the redaction
# ceiling. Paths are exact and case-sensitive; explicit ports and HTTP are not
# exempted.
_OAUTH_AUTHORIZATION_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("accounts.google.com", "/o/oauth2/v2/auth"),
        ("api.notion.com", "/v1/oauth/authorize"),
        ("app.asana.com", "/-/oauth_authorize"),
        ("auth.atlassian.com", "/authorize"),
        ("github.com", "/login/oauth/authorize"),
        ("linear.app", "/oauth/authorize"),
        ("login.microsoftonline.com", "/common/oauth2/v2.0/authorize"),
        ("slack.com", "/oauth/v2/authorize"),
        # MCP-server authorization servers. A provider's *MCP* server usually
        # runs its own authorization server, distinct from the classic web-OAuth
        # endpoint above -- so the pairs above are NOT sufficient for the
        # Connections launch set. Each pair below was taken from the provider's
        # own advertised `authorization_endpoint` (RFC 8414 metadata reached via
        # RFC 9728 protected-resource discovery from the registry's mcp_url) and
        # independently corroborated by an authorize URL kiro-cli actually
        # minted. A launch provider missing from this set cannot be connected at
        # all: its banner fails closed with "authentication failed: URL
        # contained credential or exfiltration pattern", which is how the gap
        # was found. Every entry added to the Connections registry needs its
        # MCP authorization server here too.
        ("access.stripe.com", "/mcp/oauth2/authorize"),
        ("gitlab.com", "/oauth/authorize"),
        ("mcp.auth.mail.superhuman.com", "/oauth2/authorize"),
        ("mcp.linear.app", "/authorize"),
        # Maintainer-verified 2026-09-01 via RFC 8414 metadata at
        # https://mcp.miro.com/.well-known/oauth-authorization-server
        # (authorization_endpoint: https://mcp.miro.com/authorize), matching the
        # reporter's independent RFC 8414 read in issue #7578. Not (yet) a
        # Connections registry entry; the fail-closed banner blocked every
        # attempt to connect the Miro remote MCP server.
        ("mcp.miro.com", "/authorize"),
        ("mcp.notion.com", "/authorize"),
        ("vercel.com", "/oauth/authorize"),
    }
)

# OAuth 2.0 / OIDC front-channel parameters whose values are expected to be
# opaque and high-entropy. The banner-only exemption is valid ONLY at an exact
# endpoint above. Every unknown parameter still receives the full query
# heuristics, even when it shares an otherwise-approved authorization URL.
_OAUTH_QUERY_PARAMS = frozenset(
    {
        "access_type",
        "acr_values",
        "allow_signup",
        "audience",
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "display",
        "domain_hint",
        "id_token_hint",
        "login",
        "login_hint",
        "max_age",
        "nonce",
        "prompt",
        "redirect_uri",
        "request_uri",
        "resource",
        "response_mode",
        "response_type",
        "scope",
        "state",
        "team",
        "ui_locales",
        "user_scope",
    }
)

# ── Operator-owned OAuth endpoint extension (keystone oauth_endpoints.json) ──
# ``_OAUTH_AUTHORIZATION_ENDPOINTS`` above is deliberately code-owned and
# exact-match, but that leaves no remedy short of a code release when a user's
# identity provider (Okta, Auth0, self-hosted OIDC, tenant-scoped Entra) is not
# in the launch set: its real consent URL routinely exceeds the query-length
# heuristic and the gate fails closed. The extension below restores an
# OPERATOR-owned escape hatch without weakening the ceiling for the agent:
#
# * the file lives on ``_CREW_SECRET_LEAVES`` (read+write keystone), so the
#   agent can neither read nor author its own trust widening;
# * a missing/unreadable/corrupt/non-object file yields the EMPTY set — a
#   mangled file must never widen trust (same posture as
#   ``computer_use.enable_state.load_state``);
# * every entry is strictly validated (exact host+path, no wildcards, no
#   ports/userinfo/percent-escapes, no ``..``), and invalid entries are
#   SKIPPED with a warning rather than failing the whole file;
# * HTTPS-only / no-explicit-port stays enforced by the gate logic at both
#   call sites and is NOT relaxable via the file;
# * the exemption granted is identical to the builtin set's: only the
#   base64-blob/query-length heuristics on known ``_OAUTH_QUERY_PARAMS`` are
#   skipped — fixed-credential patterns, heavy percent-encoding, userinfo,
#   fragments, backslashes, and unknown-param heuristics remain unconditional.
_ENDPOINT_EXTENSION_ENTRIES_KEY = "additional_authorization_endpoints"

# Bounds the accepted set AND the validation walk (the entry list is sliced to
# this before iteration), so a pathological file cannot amplify into an
# unbounded parse/warn loop or turn the endpoint check into a large probe.
_ENDPOINT_EXTENSION_CAP = 50

# Strict DNS-name shape for an operator entry, matched against the
# lowercase-normalized host: dot-separated LDH labels ending in a letter TLD.
# The letter-TLD requirement rejects raw IPv4 literals; the character class
# rejects wildcards, schemes, ports, userinfo, percent-escapes, whitespace,
# backslashes, and bracketed IPv6. Empty labels reject leading/trailing dots.
# The lookahead bounds total length to the DNS maximum.
_OAUTH_EXTENSION_HOST_RE = re.compile(
    r"\A(?=.{1,253}\Z)"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"\.[a-z]{2,}\Z"
)

# Paths are exact and case-sensitive (same semantics as the builtin set).
_OAUTH_EXTENSION_PATH_MAX_LEN = 512

# Rejected anywhere in an operator path entry: query/fragment/path-param
# delimiters and percent-escapes would let one entry smuggle structure the
# exact-match comparison is not built to normalize, and ``..`` plus backslash
# invite parser-differential games. The comparison is byte-exact, so a benign
# provider path never needs any of these.
_OAUTH_EXTENSION_PATH_BAD = (";", "?", "#", "%", "\\", "..")


def _valid_oauth_extension_path(path: str) -> bool:
    """True when *path* is safe to compare exactly against a consent URL path."""
    if not path.startswith("/") or len(path) > _OAUTH_EXTENSION_PATH_MAX_LEN:
        return False
    if any(marker in path for marker in _OAUTH_EXTENSION_PATH_BAD):
        return False
    return not any(ch.isspace() for ch in path)


# Memo for the parsed extension file, keyed on the file's identity + stat
# (path, mtime_ns, size) so a hand-edit takes effect on the next check without
# a gateway restart, while repeated checks against an unchanged file cost one
# ``stat`` instead of a read+parse+validate pass. (path, None) memoizes the
# absent-file case; any stat/read error bypasses the memo and fails soft.
_OAUTH_EXTENSION_MEMO: dict[
    tuple[str, tuple[int, int] | None], frozenset[tuple[str, str]]
] = {}


def _load_operator_oauth_endpoints() -> frozenset[tuple[str, str]]:
    """Load the operator's OAuth-endpoint extension set (fail-soft to EMPTY).

    Reads ``<config_dir>/oauth_endpoints.json`` and returns the validated
    ``(lowercase host, exact path)`` pairs. Absent, unreadable, corrupt, or
    non-object files — and any entry that fails the strict per-entry
    validation — yield nothing: a mangled extension file must never widen
    trust. The ``config.loader`` import stays function-local to keep this
    module's import graph independent of the loader's: ``config/loader.py``
    itself imports ``security`` symbols function-locally to avoid a cycle, and
    a module-level import here would quietly re-arm that cycle the moment the
    loader hoists its own.
    """
    from kiro_crew.config import loader as config_loader

    try:
        path = config_loader.oauth_endpoints_path()
        try:
            stat = path.stat()
            stat_key: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stat_key = None
        memo_key = (str(path), stat_key)
        cached = _OAUTH_EXTENSION_MEMO.get(memo_key)
        if cached is not None:
            return cached
        if stat_key is None:
            _OAUTH_EXTENSION_MEMO.clear()
            _OAUTH_EXTENSION_MEMO[memo_key] = frozenset()
            return frozenset()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug(
            "oauth_endpoints.json unreadable; ignoring extension file", exc_info=True
        )
        return frozenset()

    approved = _validate_operator_oauth_entries(raw)
    # One live entry per file: the memo never outgrows a handful of keys, but a
    # test suite that rewrites the file hundreds of times should not accrete.
    _OAUTH_EXTENSION_MEMO.clear()
    _OAUTH_EXTENSION_MEMO[memo_key] = approved
    return approved


def _validate_operator_oauth_entries(raw: object) -> frozenset[tuple[str, str]]:
    """Strictly validate a parsed extension document into ``(host, path)`` pairs."""
    if not isinstance(raw, dict):
        logger.warning("oauth_endpoints.json is not a JSON object; ignoring it")
        return frozenset()
    entries = raw.get(_ENDPOINT_EXTENSION_ENTRIES_KEY)
    if not isinstance(entries, list):
        if entries is not None:
            logger.warning(
                "oauth_endpoints.json: %r is not a list; ignoring it",
                _ENDPOINT_EXTENSION_ENTRIES_KEY,
            )
        return frozenset()
    if len(entries) > _ENDPOINT_EXTENSION_CAP:
        logger.warning(
            "oauth_endpoints.json: %d entries exceed the cap (%d); extra entries ignored",
            len(entries),
            _ENDPOINT_EXTENSION_CAP,
        )

    approved: set[tuple[str, str]] = set()
    for entry in entries[:_ENDPOINT_EXTENSION_CAP]:
        host = entry.get("host") if isinstance(entry, dict) else None
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(host, str) or not isinstance(path, str):
            logger.warning(
                "oauth_endpoints.json: skipping malformed entry (need host+path strings)"
            )
            continue
        host_norm = host.lower()
        if not _OAUTH_EXTENSION_HOST_RE.fullmatch(host_norm) or not _valid_oauth_extension_path(
            path
        ):
            # The host is operator-authored config, not secret material, and
            # naming it is what makes the warning actionable.
            logger.warning(
                "oauth_endpoints.json: skipping invalid endpoint entry host=%r", host[:64]
            )
            continue
        approved.add((host_norm, path))
    return frozenset(approved)


# Per-process dedupe for the extension-used audit event, so repeated checks of
# the same URL (every banner emit/redraw re-validates) do not spam the SEL.
_OAUTH_EXTENSION_AUDITED: set[tuple[str, str]] = set()


def _emit_oauth_extension_used_event(host: str, path: str) -> None:
    """SEL-audit that an OPERATOR extension entry approved a consent endpoint.

    Best-effort: an audit failure must not break the user's ability to
    authorize their MCP server — the operator explicitly allowlisted the
    endpoint, so the approval stands regardless of audit success.
    """
    if (host, path) in _OAUTH_EXTENSION_AUDITED:
        return
    _OAUTH_EXTENSION_AUDITED.add((host, path))
    try:
        # Function-local for the same loader-cycle reason as
        # _load_operator_oauth_endpoints.
        from kiro_crew.config import loader as config_loader

        SecurityEventLog().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="oauth_endpoint_extension_used",
                caller_identity="",
                agent="kirocrew",
                source="security",
                operation="oauth_banner_check",
                outcome="allowed",
                resources=f"{host}{path}",
                metadata={
                    "host": host,
                    "path": path,
                    "file": str(config_loader.oauth_endpoints_path()),
                    "mechanism": "OAUTH_ENDPOINT_EXTENSION",
                },
            )
        )
    except Exception:
        logger.debug(
            "SEL audit failed for oauth_endpoint_extension_used (allow stands)",
            exc_info=True,
        )


def _approved_oauth_authorization_endpoint(host: str, path: str) -> bool:
    """Exact-match endpoint approval for the banner-only OAuth entropy carve-out.

    Union of the code-owned builtin set and the operator's keystone extension,
    computed at check time so a hand-edited file takes effect without a
    restart. The builtin set is consulted first so the common providers never
    touch the disk; an approval that came from an operator entry is SEL-audited
    (deduped per process). Callers keep enforcing HTTPS-only / no-explicit-port
    — this helper only answers endpoint identity.
    """
    key = (host.lower(), path)
    if key in _OAUTH_AUTHORIZATION_ENDPOINTS:
        return True
    if key in _load_operator_oauth_endpoints():
        _emit_oauth_extension_used_event(*key)
        return True
    return False


# S3 presigned URLs contain X-Amz-Signature (a 64-char hex string) that
# matches the base64-like blob pattern above.  These are intentional
# time-limited access tokens, not leaked credentials.  Skip the exfil
# check when ALL standard presigned-URL query params are present on an
# amazonaws.com domain.  Values are validated to prevent spoofing.
_S3_PRESIGNED_RE = re.compile(
    r"X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f".*X-Amz-Credential={AWS_KEY_ID}"  # shared spelling: credential_patterns
    r"(?:%2F|/)"
    r".*X-Amz-Expires=\d{1,6}"
    r".*X-Amz-Signature=[0-9a-f]{64}",
    re.IGNORECASE,
)

# Only these parameter keys are allowed in a presigned URL.  Any extra
# keys cause the fast-path to reject, falling through to normal checks.
_S3_PRESIGNED_PARAMS = frozenset(
    {
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Signature",
        "X-Amz-Security-Token",
    }
)


# Structural validators for presigned param values that would otherwise
# false-positive against _EXFIL_PATTERNS.  Each value is validated rather
# than exempted, so attacker-controlled data cannot be smuggled through.
_STS_TOKEN_RE = re.compile(r"^(?:FwoGZX|IQoJb3JpZ2lu)[A-Za-z0-9+/=%]{1,2000}$")
_CREDENTIAL_RE = re.compile(
    f"^{AWS_KEY_ID}"  # shared spelling: credential_patterns
    r"(?:%2F|/)[0-9]{8}"
    r"(?:%2F|/)[a-z0-9-]+(?:%2F|/)s3(?:%2F|/)aws4_request$"
)
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")

_STRUCTURAL_VALIDATORS = {
    "X-Amz-Credential": _CREDENTIAL_RE,
    "X-Amz-Signature": _SIGNATURE_RE,
    "X-Amz-Security-Token": _STS_TOKEN_RE,
}


def _is_safe_presigned(domain: str, query: str) -> bool:
    """Return True if the URL is a valid S3 presigned URL with no extra parameters."""
    if not domain.endswith(".amazonaws.com"):
        return False
    if not _S3_PRESIGNED_RE.search(query):
        return False
    params = parse_qs(query, keep_blank_values=True)
    if not _S3_PRESIGNED_PARAMS.issuperset(params.keys()):
        return False
    # Structurally validate params that would false-positive against
    # _EXFIL_PATTERNS.  No values are fully exempt — each is checked.
    for key, values in params.items():
        validator = _STRUCTURAL_VALIDATORS.get(key)
        if validator:
            for val in values:
                if not validator.match(val):
                    return False
        else:
            for val in values:
                if _EXFIL_PATTERNS.search(val):
                    return False
    return True


# Hard, unambiguous credential markers scanned across the FULL URL path+query
# — a real AWS key / SSH-or-PEM header / Slack token in a URL is
# exfil even to an otherwise-safe host, and even with no ``?`` query (secret in
# the PATH). Distinct from the broader _EXFIL_PATTERNS base64/length heuristics,
# which stay query-only (long base64 PATH segments — CDN asset ids, git object
# hashes — are benign).
_HARD_CREDENTIAL_RE = re.compile(
    r"(?:"
    f"{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    r'|(?:SecretAccessKey|aws_secret_access_key)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:SessionToken|aws_session_token)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:AccessKeyId|aws_access_key_id)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)


def _exempt_exact_hosts() -> frozenset[str]:
    """Exact-match hosts that skip ONLY the exfil base64/length heuristics.

    Sourced from the active ``PlatformContext``'s ``CredentialPolicy`` — the
    public Default returns an empty set (no exemptions), a loaded companion
    supplies its trusted-tenant host list.  NEVER read from ``config.json``: an
    agent-writable exemption would be a hole in the redaction ceiling.

    Import is FUNCTION-LOCAL (deferred, mirroring the ``sel.py`` pattern) so
    ``security`` never reaches ``kiro_crew.platform`` at module-load time — the
    CPP import-direction invariant (``platform/defaults.py`` imports ``security``
    at top level).

    Degrade semantics: EVERY failure degrades to ``frozenset()`` — the empty set
    means MORE redaction (every host runs the heuristics), the SAFE direction
    here, and it is stricter than any companion-supplied exemption list could
    be.  This lookup can only ever RELAX the heuristics, so there is no
    fail-closed to protect: propagating an error would convert "redact slightly
    more aggressively" into "the calling operation aborts", which took down every
    pooled MCP backend spawn in ``gatewayd`` (an unbooted worker that calls
    ``redact()`` on the spawn-log and stderr-drain paths).  Deliberately INVERTED
    vs ``redact_via_context``'s propagation: that seam substitutes a companion's
    redaction for the baseline, so a missing context there must not fail open.

    NO-CONTEXT FAST PATH: when no context is INSTALLED this returns the empty set
    without resolving one, via ``installed_context()``.  That is not merely an
    optimization, it is the only way to keep this off the event loop.  Resolving
    would load config + discover plugin entry points, and on a non-standalone
    profile ``current_context()`` never memoizes its fail-closed verdict, so a
    per-line caller (``_pump_stderr`` redacting backend stderr) would re-pay that
    synchronous I/O for every line.  The answer is unchanged either way: the
    public ``DefaultCredentialPolicy`` exempts no hosts, so a lazily-composed
    standalone default yields this same empty set, and an unbooted
    non-standalone process must not be handed exemptions at all.

    A pre-method companion adapter (no ``exempt_exact_hosts``) degrades to the
    empty set via ``getattr`` rather than raising.  NO logging on the degrade
    path: this runs inside the stdio MCP servers whose stray writes corrupt the
    JSON-RPC stream.
    """
    from kiro_crew.platform.context import installed_context

    ctx = installed_context()
    if ctx is None:
        return frozenset()

    try:
        policy = ctx.credentials
        getter = getattr(policy, "exempt_exact_hosts", None)
        if getter is None:
            return frozenset()
        raw = getter()
        # Normalize INSIDE the guarded block: a buggy companion adapter may return
        # None or a set with non-string members, and callers (_exfil_exempt_hosts)
        # iterate + .lower() the result. If that raised outside this try, it would
        # break EVERY redaction path (chat/Slack/MCP/dashboard) instead of degrading
        # to maximum redaction. Keep only str members; anything malformed degrades
        # to the empty set (the SAFE direction — more redaction).
        return frozenset(h for h in raw if isinstance(h, str))
    except Exception:
        return frozenset()


def _exfil_exempt_hosts() -> frozenset[str]:
    """Companion exempt-host set normalized to lowercase for case-insensitive match.

    Hostnames are case-insensitive (RFC 4343); Office apps commonly emit
    mixed-case hosts (``Contoso.SharePoint.com``). _URL_RE captures the host
    verbatim, so both the captured host and the companion-supplied members must
    be lowercased before comparison or a legitimate document pointer to an
    exempted tenant is wrongly redacted. Delegates fail-closed / degrade
    semantics to _exempt_exact_hosts().
    """
    return frozenset(host.lower() for host in _exempt_exact_hosts())


# ── Kiro Crew's own Slack app-create deep link ──
# ``kirocrew manifest --url`` and ``GET /api/slack/manifest`` both hand the user
# Slack's new-app deep link carrying the bundled app manifest percent-encoded
# into ``manifest_yaml``. That payload is ~1.9 KB, so the aggregate query-length
# heuristic classifies it as exfiltration and the user is shown
# ``[REDACTED: suspicious URL to api.slack.com]`` instead of the link the setup
# guide tells them to click.
#
# The carve-out VALIDATES rather than trusts the destination: the decoded payload
# must reproduce the bundled template, so an approved (host, path) carries no
# arbitrary bytes. A different path, an extra or missing parameter, a repeated
# parameter, or a payload that does not rebuild the template all keep the full
# heuristics. This is deliberately NOT a host exemption: ``_exempt_exact_hosts``
# is companion-owned tenant trust, and widening it here would exempt every URL at
# api.slack.com including a model-authored one.
#
# The ALIAS is the one caller-controlled span, so it does NOT ride free: the
# caller feeds it back through the base64-blob heuristic (see
# ``_exfil_url_warning``) instead of zeroing the heuristic payload. Zeroing it was
# a real bypass — the alias slot accepted 64 chars of ``[A-Za-z0-9_-]``, which is
# wide enough for a 40-char alphanumeric secret, and ``_EXFIL_PATTERNS`` needs a
# 40+ char run to fire. ``slack_manifest.ALIAS_MAX`` (32) now makes such a run
# impossible AND the surviving span is still scanned, so an ``AKIA…`` id or an
# ``xox…`` token short enough to fit is caught on the alias alone.
#
# Residual, stated rather than implied: an alias of up to ALIAS_MAX chars that
# resembles no known credential is exempt from the base64/length heuristics. That
# opens no NEW capability — any URL at any host may already carry a query under
# _EXFIL_QUERY_MIN_LEN (200) chars without tripping either heuristic, so this
# span is strictly narrower than what is available without the carve-out.
#
# Every unconditional check runs BEFORE this point and is unaffected:
# hard-credential markers, canonical provider tokens, the multi-pass decode (and
# its fail-closed saturation branch), and heavy percent-encoding.
_SLACK_APP_CREATE_PARAMS = frozenset({"new_app", "manifest_yaml"})
# Single-slot cache for the derived pattern. A plain module constant would read
# packaged data at import time, which ``security`` avoids: it is imported by the
# stdio MCP servers, where import-time file I/O is on the critical path.
_slack_manifest_re_slot: list[re.Pattern[str] | None] = []


def _slack_manifest_payload_re() -> re.Pattern[str] | None:
    """Pattern matching the bundled Slack manifest rendered with any one alias.

    Derived from ``slack_manifest.stripped_template()`` — the SAME procedure both
    emitters use to build the payload — so the accepted payload cannot drift from
    the emitted one. Every ``{{ALIAS}}`` after the first must be the same alias
    (backreference), so a payload that varies them is rejected. Returns None when
    the template cannot be read, which fails closed (no exemption).
    """
    if _slack_manifest_re_slot:
        return _slack_manifest_re_slot[0]
    compiled: re.Pattern[str] | None = None
    try:
        from kiro_crew import slack_manifest

        rendered = slack_manifest.stripped_template()
        placeholder_token = slack_manifest.ALIAS_PLACEHOLDER
        alias_body = slack_manifest.ALIAS_PATTERN
    except Exception:
        rendered = ""
        placeholder_token = ""
        alias_body = ""
    if rendered and placeholder_token in rendered:
        parts = rendered.split(placeholder_token)
        pattern = re.escape(parts[0])
        for index, part in enumerate(parts[1:]):
            slot = f"(?P<alias>{alias_body})" if index == 0 else "(?P=alias)"
            pattern += slot + re.escape(part)
        compiled = re.compile(pattern)
    _slack_manifest_re_slot.append(compiled)
    return compiled


def _kirocrew_slack_app_link_alias(
    domain: str,
    path: str,
    query: str,
    *,
    is_https: bool,
    port: str,
) -> str | None:
    """The alias when this is our own Slack app-create link, else None.

    Returns the captured alias rather than a bool so the caller can keep that one
    caller-controlled span under the heuristics. An empty-string alias is
    impossible (the pattern requires at least one char), so a truthiness test on
    the result would be safe — but callers should compare against None to keep
    that dependence explicit.

    ``domain`` is expected already lowercased by the caller. HTTPS-only and no
    explicit port, matching the OAuth gate's posture.
    """
    if not is_https or port:
        return None
    from kiro_crew import slack_manifest

    if domain != slack_manifest.APP_CREATE_HOST or path != slack_manifest.APP_CREATE_PATH:
        return None
    params = parse_qs(query, keep_blank_values=True)
    # Exact param set — an extra parameter is the obvious smuggling shape, so a
    # superset is refused rather than ignored.
    if set(params) != _SLACK_APP_CREATE_PARAMS:
        return None
    if params["new_app"] != ["1"]:
        return None
    payloads = params["manifest_yaml"]
    if len(payloads) != 1:
        return None
    pattern = _slack_manifest_payload_re()
    if pattern is None:
        return None
    match = pattern.fullmatch(payloads[0])
    if match is None:
        return None
    return match.group("alias")


def _exfil_url_warning(
    domain: str,
    path_and_query: str,
    exempt_hosts: frozenset[str],
    *,
    port: str = "",
    is_https: bool = True,
    allow_safe_presigned: bool = True,
    allow_oauth_entropy: bool = False,
    _rule_out: list[str] | None = None,
) -> str | None:
    """Classify one matched URL — the single per-URL exfil verdict.

    Shared by scan_exfiltration_urls (which collects the warnings) and
    redact_exfiltration_urls (which redacts every URL that returns non-None), so
    the two paths can never drift. Returns the warning string, or None if clean.
    ``_rule_out`` receives only a stable rule id, never URL-derived text.
    """

    def trace(rule: str) -> None:
        if _rule_out is not None:
            _rule_out.append(rule)

    qmark = path_and_query.find("?")
    query = path_and_query[qmark + 1 :] if qmark != -1 else ""

    # Valid S3 presigned URLs carry AKIA in X-Amz-Credential legitimately. This
    # exemption is disabled for OAuth-banner validation.
    if allow_safe_presigned and query and _is_safe_presigned(domain, query):
        return None

    # Hard credential markers are unconditional across the full path/query.
    if _HARD_CREDENTIAL_RE.search(path_and_query):
        trace("exfil_hard_credential")
        return f"Suspicious URL with credential in path/query: {domain}"

    # Fixed credential signatures ANYWHERE in the full authority/path/query are
    # unconditional. This uses canonical provider-token patterns (GitHub,
    # Stripe, etc.) in addition to the older AWS/SSH/Slack hard floor, but NOT
    # the bare-secret entropy classifier that false-positives on OAuth state.
    full_payload = f"{domain}{port}{path_and_query}"
    if _contains_fixed_credential(full_payload):
        trace("exfil_fixed_credential")
        return f"Suspicious URL with credential in path/query: {domain}"

    # Decode the whole authority/path/query payload as one invariant. Component-
    # specific passes risk leaving newly handled URL structure outside the scan.
    # Decoding ONCE is not enough: a double-encoded payload ("%2542" -> "%42" ->
    # "B") survives a single pass, so decode until the text stops changing.
    # Bounded so a deliberately over-encoded URL cannot spin here.
    decoded_payload = full_payload
    for _ in range(_MAX_URL_DECODE_PASSES):
        next_payload = unquote_plus(decoded_payload)
        if next_payload == decoded_payload:
            break
        decoded_payload = next_payload
        if _HARD_CREDENTIAL_RE.search(
            decoded_payload
        ) or _contains_fixed_credential(decoded_payload):
            trace("exfil_encoded_credential")
            return f"Suspicious URL with encoded credential in path/query: {domain}"

    # Fail closed when the budget above ran out with layers still to go. A
    # payload that is STILL decodable was never seen in plaintext, and neither
    # remaining check covers it: the credential patterns match literal markers
    # rather than percent text, and _EXFIL_PERCENT_RE needs 20+ CONSECUTIVE
    # octets, which the intermediate forms of a wrapped payload ("%252520") do
    # not form. Treating saturation as clean therefore made the bound an escape
    # hatch -- wrap a credential in one more layer than the cap and it passed.
    # Raising the cap only moves that line, so the bound is priced as lost
    # precision (a pathologically encoded URL is refused) instead of lost
    # soundness. Benign traffic reaches a stable payload in one or two passes
    # and never gets here.
    if unquote_plus(decoded_payload) != decoded_payload:
        trace("exfil_decode_saturated")
        return f"Suspicious URL with encoded credential in path/query: {domain}"

    # Heavy percent-encoding is always suspicious, including inside a standard
    # OAuth parameter at an approved endpoint. It runs before either
    # host-sensitive heuristic exemption below.
    if _EXFIL_PERCENT_RE.search(path_and_query):
        trace("exfil_percent_encoding")
        return f"Suspicious URL with credential-like query data: {domain}"

    if qmark == -1:
        return None

    # Choose the exact payload that receives generic base64/entropy + aggregate
    # length heuristics. The OAuth-param carve-out is available ONLY to the
    # dedicated ACP banner-safety path. General text redactors leave the flag
    # false and remain strict for arbitrary agent/model text.
    _dom = domain.lower()
    _oauth_endpoint = (
        allow_oauth_entropy
        and is_https
        and not port
        and _approved_oauth_authorization_endpoint(_dom, path_and_query.split("?", 1)[0])
    )
    if _oauth_endpoint:
        # Names are matched literally and case-sensitively; encoded/mixed-case
        # aliases fail closed as unknown parameters.
        heuristic_query = "&".join(
            segment
            for segment in query.split("&")
            if segment.partition("=")[0] not in _OAUTH_QUERY_PARAMS
        )
    elif (
        _slack_alias := _kirocrew_slack_app_link_alias(
            _dom,
            path_and_query.split("?", 1)[0],
            query,
            is_https=is_https,
            port=port,
        )
    ) is not None:
        # Our own app-create link: the payload reproduces the bundled template,
        # so the constant bytes are what caused the false positive and are
        # excluded. The alias is the one caller-controlled span, so it STAYS
        # under the heuristics rather than riding free — zeroing this was a
        # bypass wide enough for a 40-char alphanumeric secret.
        heuristic_query = _slack_alias
    elif _dom in exempt_hosts:
        heuristic_query = ""
    else:
        heuristic_query = query

    if heuristic_query:
        if len(heuristic_query) >= _EXFIL_QUERY_MIN_LEN:
            trace("exfil_query_length")
            return (
                f"Suspicious URL with long query params ({len(heuristic_query)} chars): "
                f"{domain}{path_and_query[:60]}..."
            )
        if _EXFIL_PATTERNS.search(heuristic_query) or _EXFIL_PATTERNS.search(
            unquote_plus(heuristic_query)
        ):
            trace("exfil_query_pattern")
            return f"Suspicious URL with credential-like query data: {domain}"
    return None


def scan_exfiltration_urls(text: str) -> list[str]:
    """Scan text for URLs that may be exfiltrating data via query params.

    Flags the PAYLOAD, not the destination: fixed credentials and the
    base64/length heuristics inspect the URL path+query regardless of host. Only
    companion-supplied exact tenant hosts skip the base64/length heuristics here;
    the OAuth-param carve-out is disabled for this general text scanner. Returns
    list of warning strings, empty if clean.
    """
    exempt_hosts = _exfil_exempt_hosts()
    warnings: list[str] = []
    for match in _URL_RE.finditer(text):
        warning = _exfil_url_warning(
            match.group(1),
            match.group(3) or "",
            exempt_hosts,
            port=match.group(2) or "",
            is_https=match.group(0).lower().startswith("https://"),
        )
        if warning:
            warnings.append(warning)
    return warnings


def redact_exfiltration_urls(text: str) -> tuple[str, list[str]]:
    """Scan and redact suspicious exfiltration URLs from text.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings = scan_exfiltration_urls(text)
    if not warnings:
        return text, []

    exempt_hosts = _exfil_exempt_hosts()
    result = text
    for match in _URL_RE.finditer(text):
        domain = match.group(1)
        if _exfil_url_warning(
            domain,
            match.group(3) or "",
            exempt_hosts,
            port=match.group(2) or "",
            is_https=match.group(0).lower().startswith("https://"),
        ):
            result = result.replace(match.group(0), f"[REDACTED: suspicious URL to {domain}]")
    return result, warnings


# ── Credential Output Redaction ──
# Catches raw credential patterns in LLM output / tool results,
# including base64-encoded variants.  Applied on all output paths
# alongside redact_exfiltration_urls().
#
# ⚠ THIS PATTERN HAS A DEPENDENT PRE-FILTER. `_might_contain_credential` below
# gates the scan of this pattern on a cheap necessary condition, and
# `redact_credentials` SKIPS the scan entirely when that gate returns False. The
# gate is therefore part of the redaction boundary, not an optimisation detail:
# any input a branch here accepts but the gate rejects is a silent leak.
#
# So EDITING A BRANCH IS A TWO-SITE CHANGE:
#   * ADDING a branch     -> register a sample in `test_credential_prefilter.py`
#                            and an anchor in `_might_contain_credential`.
#                            `test_every_pattern_branch_has_a_prefilter_anchor`
#                            fails on the branch count until you do.
#   * WIDENING a branch   -> widen the corresponding anchor to match, because the
#                            anchor must stay a SUPERSET of the branch. A widened
#                            branch does NOT change the branch count, so the count
#                            assertion cannot see it. Two tests cover this:
#                            `test_a_widened_branch_cannot_outgrow_its_anchor`
#                            enumerates each branch's own alternatives, so a NEW
#                            alternative (a second token prefix) is caught; and
#                            `test_widening_a_branch_cannot_outgrow_its_anchor`
#                            perturbs each sample, so a case-fold or homoglyph
#                            relaxation is caught.
#   * Making a branch CASE-INSENSITIVE -> the anchor MUST use the same regex
#                            engine. A case-sensitive literal cannot gate a
#                            `(?i:…)` branch, and neither can `str.lower()` —
#                            see `_CREDENTIAL_PREFILTER_AUTHORIZATION_RE` for the
#                            bypass that cost.
_CREDENTIAL_PATTERNS = re.compile(
    r"(?:"
    # ── AWS ──
    f"{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    # key-value forms: tolerate an optional closing quote after the key name and an
    # optional opening quote before the value so JSON (`"aws_secret_access_key": "v"`)
    # is redacted, not just bare `key=v` / `key: v`. Without the `["']?` the closing
    # quote in JSON sits between the key and `:` and defeats the match → secret leaks.
    # The value class is [^\s"',}]+ (NOT \S+): \S+ is greedy and, in compact JSON
    # like {"aws_secret_access_key":"SECRET","region":"x"}, swallows everything
    # through the closing brace (`"`, `,`, `}` all match \S) — destroying adjacent
    # fields and consuming a following credential key so it's never matched/counted.
    # Stopping at JSON structural delimiters bounds the value while still matching
    # bare key=value forms.
    r'|(?:SecretAccessKey|aws_secret_access_key)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:SessionToken|aws_session_token)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:AccessKeyId|aws_access_key_id)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    # PEM private key: match the ENTIRE block (header + base64 body), not just
    # the header phrase. redact_credentials() replaces the matched SPAN, so a
    # header-only match (the original form) left the secret base64 body verbatim.
    # Two mutually exclusive tails after the header:
    #   1. Full block — ``[\s\S]*?`` (any char, incl. newlines) spans the body
    #      lazily to the first END marker. ``[\s\S]`` (not a base64 char class)
    #      is required so encrypted keys — whose ``Proc-Type:``/``DEK-Info:``
    #      headers carry ``:`` and ``,`` — are fully spanned rather than cut
    #      short at the first non-base64 char.
    #   2. Truncated block (no END) — consume only *subsequent* PEM body lines:
    #      each continuation must start with a newline and be a base64 line or a
    #      ``Proc-Type:``/``DEK-Info:`` metadata header. This deliberately does
    #      NOT use ``$``/``\Z``: without re.MULTILINE ``$`` means end-of-STRING,
    #      so a lazy ``[\s\S]*?`` with a ``|$`` fallback swallowed everything
    #      from a header mentioned inline in prose (LLM output, docs) to the end
    #      of the string — silently deleting all trailing lines. Requiring a
    #      leading newline per line means an inline header in prose (real key
    #      material always begins on the line *after* the header) matches only
    #      the header phrase, leaving trailing content intact, while a genuine
    #      truncated key still has its body lines redacted.
    #      The final ``(?=\r?\n[A-Za-z0-9+/=])`` lookahead alternative lets the
    #      run cross a SINGLE blank line when the *next* line begins with base64
    #      material. RFC 1421 ENCRYPTED PEMs put a MANDATORY blank line between
    #      the ``DEK-Info:`` header and the base64 body; without this lookahead
    #      the per-line "every continuation must contain a base64 char" rule
    #      stopped at that blank line and leaked the whole encrypted body (for
    #      both a truncated key AND a complete encrypted key whose body exceeds
    #      the full-block cap). Because the lookahead consumes nothing, TWO+
    #      consecutive blank lines still terminate the run — trailing prose is
    #      preserved (no over-redaction).
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"(?:"
    r"[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?:\r?\n(?:Proc-Type:[^\n]*|DEK-Info:[^\n]*|[A-Za-z0-9+/=]+(?=\r?\n|\Z)"
    r"|(?=\r?\n[A-Za-z0-9+/=])))*"
    r")"
    r"|xox[bpas]-[0-9a-zA-Z-]{10,}"  # Slack token
    # Telegram bot token: ``<bot_id>:<secret>`` — bot_id is 6+ digits, secret is
    # ~35 URL-safe base64 chars. The ``{30,}`` floor sits deliberately below the
    # real length so shortened/rotated test tokens are still caught. Analogue to
    # the Slack token above. Telegram tokens can live in ``config.json``
    # (agent-readable), so an echoed config would otherwise leak a full
    # bot-control credential unredacted. The value class ``[A-Za-z0-9_-]`` stops
    # at structural delimiters (space, quote, comma, brace), so it can't swallow
    # adjacent fields; over-redacting a rare ``digits:token`` lookalike is the
    # safe direction.
    r"|[0-9]{6,}:[A-Za-z0-9_-]{30,}"  # Telegram bot token
    # Discord bot token: three base64url segments — ``base64(application_id)``,
    # a 6-char timestamp, and an HMAC. The first segment is base64 of a decimal
    # snowflake, so its leading character is fixed by the id's first digit
    # (``M``/``N``/``O`` for the 1-9 range every live snowflake starts with), and
    # the timestamp segment is always EXACTLY 6 characters. Both anchors matter:
    # the same rule written as three open-ended runs matches an ordinary dotted
    # identifier or a base64 blob with periods in it, and a redactor that eats
    # arbitrary text is a different bug. Length floors sit below the real ones so
    # a shortened/rotated test token is still caught. Same reasoning as Telegram
    # above — ``discord.bot_token`` can live in ``config.json``, which the agent
    # can read, so an echoed config would otherwise leak bot control verbatim.
    # The boundary guards keep the leading ``[MNO]`` from landing mid-run inside
    # a longer base64 blob and redacting an arbitrary tail of it, the same way
    # the link-token branch below guards its own ``eyJ`` anchor.
    r"|(?<![A-Za-z0-9_-])[MNO][A-Za-z0-9_-]{22,30}"
    r"\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}(?![A-Za-z0-9_-])"  # Discord bot token
    # ── Third-party developer credentials (AWS-345 / AWS-59) ──
    # Distinctive, fixed-case prefixes → very low false-positive risk.  Minimum
    # lengths are kept slightly below the real token lengths so shortened test /
    # rotated variants are still redacted (over-redaction on a prefix match is the
    # safe direction).  Case-sensitive by design (these prefixes are issued in a
    # fixed case); do NOT fold — folding would broaden false positives.
    r"|gh[opsur]_[A-Za-z0-9]{30,255}"  # GitHub PAT (ghp_) + oauth/user/server/refresh
    r"|github_pat_[A-Za-z0-9_]{40,}"  # GitHub fine-grained PAT
    r"|glpat-[A-Za-z0-9_-]{16,}"  # GitLab PAT
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"  # Stripe secret / restricted keys
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"  # SendGrid API key
    r"|sk-proj-[A-Za-z0-9_-]{16,}"  # OpenAI project key
    r"|sk-ant-[A-Za-z0-9_-]{16,}"  # Anthropic API key
    r"|npm_[A-Za-z0-9]{24,}"  # npm access token
    r"|pypi-[A-Za-z0-9_-]{16,}"  # PyPI API token
    r"|do[opr]_v1_[A-Za-z0-9]{40,}"  # DigitalOcean PAT/OAuth/refresh
    r"|GOCSPX-[A-Za-z0-9_-]{20,}"  # Google OAuth client secret
    # Connection/fetch URIs with embedded credentials — redact the
    # ``scheme://user:pass@`` prefix (the password lives here). http(s)/ftp(s)
    # are included because URL userinfo is a credential wherever it appears
    # (e.g. a token-bearing artifact CDN base quoted by an update-failure
    # message); the user:pass@ shape cannot false-positive on a bare URL — a
    # port (``:8080``) is never followed by ``@`` within the authority.
    r"|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|amqp(?:s)?"
    r"|https?|ftps?)"
    # User portion is `*` (not `+`): empty-user connection strings (e.g. MongoDB
    # Atlas IAM `mongodb+srv://:secret@…`) still redact the password (ported
    # from the upstream project).
    # Password segment allows ``@`` (``[^\s/]`` not ``[^\s/@]``): an unencoded
    # ``@`` inside a password is common, and stopping the match at the FIRST
    # ``@`` would redact only the head and leak the rest (``…ss@host``) to
    # logs. ``/`` still bounds the authority, so greedy ``+`` consumes through
    # the FINAL ``@`` — the real userinfo/host separator — and never past it.
    r"://[^\s:/@]*:[^\s/]+@"
    # ── JWT / JWE / OAuth Bearer tokens ──
    # `eyJ` is the base64url encoding of every JWT header's `{"` prefix; a signed
    # JWT (JWS) is three `.`-separated base64url segments (header.payload.sig), an
    # encrypted JWT (JWE, RFC 7516) is five (header.key.iv.ciphertext.tag), and our
    # OWN dashboard link token is two — `base64url(payload).base64url(hmac_sig)`,
    # see `dashboard.token_auth.generate_token`. The 3-and-5-segment shapes are
    # matched by the `{2,4}` quantifier below; the 2-segment link token has its
    # OWN separately bounded alternative.
    #
    # The floor stays at 2: at 2 the two-segment dashboard token did not
    # match here at all and fell through to the bare-secret entropy pass, whose run
    # class `[A-Za-z0-9+/]` is STANDARD base64 and excludes base64url's `-`/`_`.
    # That made redaction depend on which characters a random HMAC signature
    # happened to contain. That rate is derivable, so it is stated as a closed form
    # rather than as a sample. HMAC-SHA256 is 256 bits and base64url-unpadded gives
    # 43 chars. The first 42 each carry a full 6 bits, so each is uniform over the
    # 64-char alphabet, of which exactly 2 are `-`/`_`. The 43rd carries only the
    # leftover 4 bits (256 - 42*6), and they land in the HIGH bits of its 6-bit
    # group with the low 2 bits zero, so it spans exactly the 16 alphabet indices
    # divisible by 4 (`048AEIMQUYcgkosw`) and can never be `-`/`_`, which sit at
    # 62/63. Hence P(no `-`/`_`) = (62/64)^42 = 26.4%, verified by encoding all
    # 256 possible final digest bytes.
    # So roughly a quarter of tokens had only the signature replaced (leaving the
    # payload claims verbatim in a URL that still looked complete but no longer
    # authenticated), and the other ~74% streamed out entirely unredacted. Matching the whole token here makes
    # the outcome deterministic and replaces it as one unit. The 2-segment token gets
    # its OWN alternative rather than relaxing the segment floor to `{1,4}`. Relaxing
    # the floor over-redacts ordinary code and prose, because the pattern has no left
    # boundary and post-header segments allow an EMPTY match: `keyJson.get(raw)` then
    # redacts to `k[REDACTED…](raw)`, and a JWT quoted at the end of a sentence loses
    # its trailing period. The 2-segment alternative therefore carries a left boundary
    # (`(?<![A-Za-z0-9_.-])`, as `_BARE_SECRET_RUN_RE` already does, plus `.` so an
    # attribute access `obj.eyJ…` is excluded too) and per-segment lengths taken from
    # the generator, not from guesswork, because a length FLOOR alone is beatable by a
    # sufficiently verbose identifier: at `{40,}` the 40-char
    # `eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue` matched.
    #
    # `token_auth._sign` is HMAC-SHA256 base64url-unpadded, so the signature is
    # EXACTLY 43 chars for every token ever minted; that is a property of the digest,
    # not of the payload, so it is pinned as `{43}` rather than a floor. See
    # `test_link_token_signature_is_43_chars`, which fails loudly if `_sign` changes
    # digest, instead of letting redaction silently stop matching.
    #
    # `generate_token` always emits 6 claims (`sub`/`exp`/`session_exp`/`iat`/`nonce`/
    # `gen`), with a 16-hex-char nonce and float timestamps; `app`, `prompt` and
    # `extra` only ADD. Payload length is NOT fixed. It scales with `len(sub)`, and
    # `json.dumps` writes each float timestamp at its own repr width, which base64
    # then quantises into 4-char steps. So the floor is derived, not sampled: a
    # 1-char `sub` (the narrowest a caller passes: the app validator requires at
    # least one char and the other call sites supply a literal fallback), `gen=0`,
    # and all three timestamps at their shortest 12-char repr (an exactly-integral
    # `time.time()` in the current 10-digit epoch era) measures 145 chars past
    # `eyJ`, which leaves the `{96,}` floor 49 chars of headroom against a future
    # shorter claim set while still excluding `eyJ2IjoxfQ.json`. ONLY that derived
    # floor is pinned, by `test_link_token_payload_clears_the_96_char_floor`, which
    # reads the bound from the compiled pattern and the claim keys from a real mint
    # so a dropped claim fails loudly instead of silently disabling redaction. Live
    # payloads are much larger and are NOT pinned, because the exact spread moves
    # with float reprs and caller mix: measured 168-185 for the mandatory-only
    # callers and 192-223 for the two that also pass `app=` (`handlers/core.py`,
    # `token_auth.py`), which adds an `"app"` claim.
    #
    # Order matters: the 3-to-5-segment
    # alternative is tried first at each position, so a real JWS still redacts whole
    # instead of matching `header.payload` and leaving `.signature` exposed.
    # The 3-to-5-segment alternative keeps `*` (not `+`) on post-header segments so an
    # EMPTY segment still counts: a compact JWE with direct
    # (`alg:dir`) or key-agreement (`ECDH-ES`) key management has an empty Encrypted
    # Key (2nd) segment — shape `header..iv.ciphertext.tag` — which a `+` quantifier
    # would fail to match, leaking the ciphertext + tag.
    # The HTTP `Authorization: Bearer <token>` header carries opaque or JWT bearer
    # creds. The JWT alternative is case-sensitive (`eyJ` is a fixed base64url
    # prefix). The header name + scheme are matched case-insensitively via scoped
    # `(?i:…)` groups because HTTP header names are case-insensitive (RFC 7230
    # §3.2), HTTP/2 mandates lowercase names, and the `Bearer` scheme is
    # case-insensitive (RFC 6750 §2.1) — so `authorization: bearer …` emitted by
    # requests / net/http / HTTP2 frame logs is redacted too. The separator is
    # JSON-aware: an optional quote may precede the
    # `:`/`=` and the token, so a serialized header `{"Authorization": "Bearer
    # <tok>"}` in a structured-log/JSON request dump is redacted as well. Both
    # alternatives are scoped tightly: the JWT segment class cannot cross the
    # literal `.` separators and the Bearer token class (`[A-Za-z0-9._~+/-]`, RFC
    # 6750 `b64token`) stops at whitespace/quotes, so neither over-captures. A
    # Bearer header carrying a JWT redacts as one match (the Bearer class subsumes
    # the JWT); a bare JWT is still caught independently (defense in depth).
    f"|{JWT_MULTI_SEGMENT}"  # JWS (3-seg) / JWE (5-seg incl. dir/ECDH-ES), shared spelling
    r"|(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]{96,}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"  # 2-seg link token
    r"|(?i:Authorization)[\"\']?\s*[:=]\s*[\"\']?(?i:Bearer)\s+[A-Za-z0-9._~+/-]+=*"  # HTTP/JSON bearer
    r")",
)


def get_credential_patterns() -> list[re.Pattern[str]]:
    """Public accessor for the canonical credential regexes.

    Lets other modules (e.g. deploy-web's pre-publish content scan) reuse the
    same patterns without coupling to the private ``_CREDENTIAL_PATTERNS`` name,
    so a future rename here can't silently turn a downstream scan into a no-op.
    Returns a list so callers can iterate uniformly; the fork keeps a single
    combined compiled regex, so the list has one element.
    """
    return [_CREDENTIAL_PATTERNS]


# ── Cheap pre-filter for `_CREDENTIAL_PATTERNS` (performance only) ──
# `_CREDENTIAL_PATTERNS` is a 23-branch alternation, so `re` retries every branch
# at essentially every position: measured 117 ns/char, and it is the single
# hottest line in the gateway's event loop (38.2% of all py-spy samples, reached
# per message per dirty-slot flush). The scan cost is paid in full even though
# real text almost never contains a credential — measured 0 matches across 1,804
# live session-history messages (1.47 MB).
#
# So `_might_contain_credential` answers the cheap question "could a match exist
# at all?" and lets `redact_credentials` skip the expensive scan when the answer
# is no. It is a strict SUPERSET of `_CREDENTIAL_PATTERNS`, i.e. for every string
# the pattern matches, this returns True. That direction is the security
# property: a false POSITIVE only costs a scan we would have run anyway, while a
# false NEGATIVE would skip redaction and leak a credential into persisted chat
# history. Every condition below is therefore a NECESSARY condition of a branch,
# never a restatement of it — each is deliberately looser than the branch it
# stands in for.
#
# THE MAINTENANCE HAZARD this is built against: adding a 24th branch to
# `_CREDENTIAL_PATTERNS` without adding a matching anchor here would silently
# disable redaction for it. Nothing about the pattern edit would look wrong, and
# the failure is invisible in output — the branch simply stops firing. So
# `test_credential_prefilter.py` splits `_CREDENTIAL_PATTERNS.pattern` on its
# top-level `|`, asserts the branch count equals the number of registered sample
# credentials, and asserts the pre-filter fires for each. A new branch fails that
# count assertion loudly instead of quietly widening the leak.
#
# Literals are case-sensitive because the branches they stand for are (these
# prefixes are issued in a fixed case); the sole case-insensitive branch
# (`Authorization: Bearer`) is handled separately below.
_CREDENTIAL_PREFILTER_LITERALS: tuple[str, ...] = (
    "AKIA",  # AWS access key ID
    "ASIA",  # AWS access key ID (STS)
    "AccessKey",  # SecretAccessKey + AccessKeyId (shared substring)
    "aws_secret_access_key",
    "aws_session_token",
    "aws_access_key_id",
    "SessionToken",
    "PRIVATE KEY-----",  # PEM header AND footer both carry it
    "xox",  # Slack token
    "github_pat_",
    "glpat-",
    "k_live_",  # sk_live_ / rk_live_ (shared substring)
    "k_test_",  # sk_test_ / rk_test_ (shared substring)
    "SG.",  # SendGrid
    "sk-proj-",  # OpenAI
    "sk-ant-",  # Anthropic
    "npm_",
    "pypi-",
    "_v1_",  # do[opr]_v1_ DigitalOcean
    "GOCSPX-",  # Google OAuth client secret
    "eyJ",  # JWS / JWE / 2-segment link token
)

# Branches with no usable literal anchor. Each is the branch's own leading shape
# with its expensive tail dropped, so it stays a superset while keeping a narrow
# first-character set that `re` can skip on.
#   `gh[opsur]_`     — GitHub PAT family; a bare "gh" literal matches ordinary
#                      prose ("through", "might"), so the class is kept.
#   `[0-9]{6,}:…{30}` — Telegram bot token. The trailing 30-char run matters: a
#                      bare `[0-9]{6,}:` matches an epoch timestamp followed by a
#                      colon, which fired on 29 of 614 real messages.
#   `[MNO]…\.`        — Discord bot token (first segment is base64 of a snowflake).
#   `://…:…@`         — URI userinfo. The scheme alternation is dropped, which is
#                      what leaves a `://` literal prefix for `re` to search on;
#                      a bare `://` would match every ordinary URL.
_CREDENTIAL_PREFILTER_GH_RE = re.compile(r"gh[opsur]_")
_CREDENTIAL_PREFILTER_TELEGRAM_RE = re.compile(r"[0-9]{6,}:[A-Za-z0-9_-]{30}")
_CREDENTIAL_PREFILTER_DISCORD_RE = re.compile(r"[MNO][A-Za-z0-9_-]{22,30}\.")
_CREDENTIAL_PREFILTER_URI_RE = re.compile(r"://[^\s:/@]*:[^\s/]+@")

# The `Authorization: Bearer` branch is the ONLY case-insensitive branch, and it is
# spelled `(?i:Authorization)`. This anchor reuses that exact sub-pattern, so it is
# a superset of the branch BY CONSTRUCTION — same engine, same folding rules.
#
# `"authorization" in text.lower()` is NOT a valid anchor for it, because
# `str.lower()` and `re.IGNORECASE` are two DIFFERENT case-folding
# implementations and they disagree. `re` folds via `sre_compile._equivalences`,
# which treats U+0131 (LATIN SMALL LETTER DOTLESS I) and U+0130 (LATIN CAPITAL
# LETTER I WITH DOT ABOVE) as equivalent to `i`/`I`; `str.lower()` leaves U+0131
# unchanged and expands U+0130 to two code points. So the branch MATCHES
# `Authorızation: Bearer <token>` while a `.lower()` anchor MISSES it, which skips
# pass 1 and leaves the bearer token verbatim in persisted chat history. The same
# disagreement holds for U+017F/`s` and U+212A/`k`, so it is a class of defect
# rather than one homoglyph: a case-insensitive branch is only safely anchored by
# the SAME regex engine, never by a hand-rolled fold.
# Pinned by `test_unicode_case_folding_cannot_bypass_the_prefilter`.
_CREDENTIAL_PREFILTER_AUTHORIZATION_RE = re.compile(r"(?i:Authorization)")


def _might_contain_credential(text: str) -> bool:
    """Return True if *text* could contain a `_CREDENTIAL_PATTERNS` match.

    A strict superset of `_CREDENTIAL_PATTERNS.search(text) is not None`: it may
    return True where the pattern would not match, but it MUST NOT return False
    where the pattern would match. Callers use it only to skip a scan whose
    result is already known to be empty, so output is unchanged either way.
    """
    for literal in _CREDENTIAL_PREFILTER_LITERALS:
        if literal in text:
            return True
    return (
        _CREDENTIAL_PREFILTER_GH_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_TELEGRAM_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_DISCORD_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_URI_RE.search(text) is not None
        or _CREDENTIAL_PREFILTER_AUTHORIZATION_RE.search(text) is not None
    )


# Minimum string length at which `_might_contain_credential` is cheaper than the
# `_CREDENTIAL_PATTERNS` alternation it gates. The pre-filter has a fixed ~590 ns
# floor (21 substring searches plus 5 anchored regex calls) that does not shrink
# with the input, so on a very short string the alternation simply wins: measured
# 684 ns against 494 ns at 8 characters, crossing over at 12 and reaching 3.4x by
# 256. Callers scanning SHORT strings -- a decoded base64 blob is typically 16-30
# characters -- must gate on this rather than assume the pre-filter is
# unconditionally cheaper.
#
# Held at 16 rather than the measured crossover of 12, deliberately: the gate is
# verdict-neutral (the pre-filter is a proven superset, so either route reaches the
# same answer), which makes a conservative threshold cost at most one alternation
# scan on a 12-15 character blob and makes it robust to the crossover drifting as
# the pre-filter's own cost changes. It has already drifted once -- adding the
# case-insensitive Authorization anchor moved it from 16 to 12.
_PREFILTER_MIN_LEN = 16


# Base64 alphabet: at least 40 chars of [A-Za-z0-9+/] ending with optional =
_B64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


# ── Label-independent bare-secret detection ──
# A 40-char AWS *secret access key* (the value paired with an AKIA/ASIA access
# key ID) is a bare run of the base64 alphabet with NO distinctive prefix and NO
# key= label, so none of the labelled/prefixed patterns in _CREDENTIAL_PATTERNS
# catch it when it appears standalone (e.g. echoed alone, in a log line, or in a
# JSON array element). We add a conservative, entropy-gated detector for this
# shape. This is the HIGHEST false-positive-risk redaction rule in the module, so
# it is deliberately over-gated: a token must clear EVERY gate below to be
# redacted. The gates are ordered cheapest-first.
#
# AWS secret access keys are exactly 40 base64 characters. We match ANY isolated
# run of >=40 base64-alphabet chars (word-boundary look-arounds keep surrounding
# prose intact and stop a longer high-entropy blob from being split and missed),
# then require the *specific 40-char secret shape* per token.
#
# NO LONGER CONSULTED BY `redact_credentials`. Pass 3 derives its runs from
# `_B64_CHUNK_RE` instead (`run = chunk.rstrip("=")`), because that one scan feeds
# both pass 2 and pass 3 and the two patterns select identical spans. The only
# remaining consumer here is `_text_contains_bare_secret`. That split is a
# desync hazard: WIDENING THIS PATTERN ALONE (adding base64url `-_`, say) would
# change the URL scan and leave the redactor untouched, silently. Any edit to the
# character class or the `{40,}` floor must be mirrored in `_B64_CHUNK_RE` above.
# `test_the_two_base64_run_patterns_stay_structurally_coupled` pins both literals
# so such an edit fails loudly rather than drifting.
_BARE_SECRET_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}(?![A-Za-z0-9+/])")

# Exactly-40 is the AWS secret-key length. Keeping the shape check length-exact
# (rather than ">=40") is what lets the structural gates below cleanly separate
# real keys from 64-char sha256 hex, base64 document blobs, etc.
_SECRET_KEY_LEN = 40

# Shannon-entropy floor (bits/char). A uniformly-random 40-char base64 string
# averages ~4.78 bits/char and empirically almost never drops below ~4.4;
# English-word identifiers, hex digests, and repeated/low-alphabet runs sit
# below this. 4.3 is a conservative floor that admits real keys (the canonical
# AWS example scores 4.66) while rejecting camelCase code identifiers and file
# paths, which cluster around 4.0-4.3.
_SECRET_ENTROPY_MIN = 4.3

# Even after the entropy floor, camelCase / PascalCase code identifiers and
# slash-delimited file paths (e.g. src/main/java/com/Example/FooBarBazClas1) can
# survive on entropy ALONE. Two structural signals separate a random secret from
# a word-based identifier or path: (a) a random key almost never contains a long
# unbroken lowercase run, whereas identifiers/paths are built from dictionary
# words that do; (b) a random key has a low vowel ratio, whereas English words
# do not. NOTE: unlike a naive design we deliberately do NOT treat the presence
# of '/' or '+' as a free pass to redact — 40-char mixed-case file paths contain
# '/' yet are benign, so a '/' token must still clear both structural gates.
# Thresholds are chosen from measured distributions (see test_security.py) with a
# wide margin toward NOT redacting.
_SECRET_MAX_LOWER_RUN = 5
_SECRET_MAX_VOWEL_RATIO = 0.30

# A token that base64-decodes to >=85% printable ASCII is encoded *text*, not a
# random key (random 40-char keys decode to mostly non-printable bytes). Such a
# token is left to the existing base64 decode-and-scan path in redact_credentials
# so we do not double-count or mis-classify it here.
_SECRET_PRINTABLE_DECODE_RATIO = 0.85

_VOWELS: frozenset[str] = frozenset("aeiouAEIOU")

# All-hex runs are git SHAs (40 hex), sha256 (64 hex), md5 (32 hex), etc. — never
# an AWS secret key (which uses the full base64 alphabet). Reject them outright.
_HEX_ONLY_RE = re.compile(r"\A[0-9a-fA-F]+\Z")

# The Shannon term ``(c / _SECRET_KEY_LEN) * log2(c / _SECRET_KEY_LEN)``, indexed by
# the character count ``c``. Element 0 is a ``0.0`` placeholder that keeps ``c``
# usable as a direct index; it is never read, because a count of zero cannot appear
# in a :class:`~collections.Counter` built from an iterable, and ``log2(0)`` would
# raise.
#
# Built for ONE length rather than parameterised over lengths, because
# :func:`_looks_like_secret_key` reaches the entropy gate only through its
# exactly-``_SECRET_KEY_LEN`` check, so that is the only length any production call
# can ask about. A per-length table would need a size cap and an eviction policy to
# bound what an arbitrary caller could materialise -- machinery guarding a caller
# that does not exist. Any other length falls through to the inline formula, which
# is what this table was derived from, so the general path is exactly as it was
# before the table existed.
#
# The terms are computed with the same operations the inline expression used, which
# is what makes this a pure precomputation rather than a re-derivation.
_ENTROPY_TERMS_KEY_LEN: tuple[float, ...] = (0.0,) + tuple(
    (c / _SECRET_KEY_LEN) * math.log2(c / _SECRET_KEY_LEN) for c in range(1, _SECRET_KEY_LEN + 1)
)


def _shannon_entropy(token: str) -> float:
    """Return the Shannon entropy of *token* in bits per character.

    The result is compared against :data:`_SECRET_ENTROPY_MIN` by
    :func:`_looks_like_secret_key`, so this is a gate on a redaction verdict and
    NOT a statistic anybody displays. A one-ULP drift at the boundary flips that
    verdict, and a flip in the permissive direction leaks a credential. The
    optimisation below is therefore built to be BIT-IDENTICAL, not merely close,
    and is pinned that way by ``TestShannonEntropyIsBitIdentical``.

    Each addend is ``(c / length) * log2(c / length)``. The sole production caller
    reaches this only through the exactly-``_SECRET_KEY_LEN`` check in
    :func:`_looks_like_secret_key`, and reaches it over and over --
    :func:`_contains_bare_secret` slides a 40-char window byte by byte across each
    base64-alphabet run that clears its prefilters -- so at that one length every
    addend is drawn from the fixed set :data:`_ENTROPY_TERMS_KEY_LEN` holds. That
    retires TWO true divisions and one ``math.log2`` call per DISTINCT CHARACTER per
    call -- ``c / length`` appears twice in the expression and CPython evaluates it
    twice, and a 40-char base64 window holds ~30 distinct characters -- plus the
    generator frames, in favour of a C-level ``map`` over a tuple index.

    Any other length takes the inline formula, unchanged from before the table
    existed. That keeps the fast path to the single length that is actually asked
    for, so no size cap or cache-eviction policy is needed to bound what an
    arbitrary caller could make this allocate.

    Why this is bit-identical rather than approximately equal:

    * Each addend is produced by the same three IEEE-754 operations on the same
      operands as before -- divide, ``log2``, multiply -- so each addend carries
      the same bit pattern. Precomputation changes WHEN a term is computed, never
      HOW.
    * ``Counter(token).values()`` still supplies the addends, in the same
      first-occurrence order, and ``map`` is consumed in order, so ``sum``
      accumulates identical addends in an identical sequence. The equality
      therefore does not rest on float addition being associative, which it is
      not. An algebraic rearrangement such as
      ``log2(length) - sum(c * log2(c)) / length`` IS mathematically equal and is
      measurably NOT bit-equal, which is why it is not used here.
    """
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    if length != _SECRET_KEY_LEN:
        return -sum((c / length) * math.log2(c / length) for c in counts.values())
    return -sum(map(_ENTROPY_TERMS_KEY_LEN.__getitem__, counts.values()))


def _has_all_three_char_classes(text: str) -> bool:
    """Return True if *text* holds at least one lowercase, uppercase AND digit.

    One pass with early exit, rather than three ``any()`` scans. Semantically
    identical, but this is the hottest predicate in the redaction path:
    :func:`_contains_bare_secret` slides a 40-char window BYTE BY BYTE across a
    base64-alphabet run that clears its prefilters, so a 512-char run reaching that
    loop asks this question 473 times. Three ``any()`` scans build three generators
    per call and cost the SUM of their three first-match offsets; one loop breaks on
    completion and costs the MAX. Both forms short-circuit, so the saving is
    generator frames plus that sum-vs-max difference.

    Absence of a class is closed under substring, which is what lets
    :func:`_contains_bare_secret` ask this about a whole run and retire every
    window at once.
    """
    has_lower = has_upper = has_digit = False
    for ch in text:
        if not has_lower and ch.islower():
            has_lower = True
        elif not has_upper and ch.isupper():
            has_upper = True
        elif not has_digit and ch.isdigit():
            has_digit = True
        if has_lower and has_upper and has_digit:
            return True
    return False


# The byte set counted as "printable" by :func:`_decodes_to_printable_text`: tab,
# LF, CR and the printable ASCII range 0x20-0x7E. Held as ``bytes`` so the count
# can be delegated to ``bytes.translate``, which runs in C.
_PRINTABLE_BYTES: bytes = bytes(sorted({0x09, 0x0A, 0x0D} | set(range(0x20, 0x7F))))


def _decodes_to_printable_text(token: str) -> bool:
    """Return True if *token* base64-decodes to mostly-printable ASCII.

    Encoded human-readable text (a base64 document blob) decodes to printable
    bytes; a random 40-char secret key decodes to mostly non-printable bytes. We
    use this to exclude encoded-text blobs from the bare-secret heuristic (they
    are handled by the existing decode-and-scan pass instead).
    """
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=False)
    except Exception:
        return False
    if not raw:
        return False
    # Count the printable bytes by DELETING them in C and measuring what is left,
    # rather than testing every byte in a Python loop. ``translate(None, set)``
    # returns exactly the bytes NOT in *set*, so ``len(raw) - len(...)`` is the
    # member count -- an integer identity, so the ratio and the comparison below
    # are bit-identical to the previous per-byte sum (asserted against a verbatim
    # copy of that sum in ``test_printable_count_matches_the_per_byte_sum``,
    # including all 256 single-byte inputs exhaustively).
    #
    # This is the single most expensive operation in pass 3, because the helper
    # runs once per base64-alphabet run AND again per 40-char window as gate 7 of
    # `_looks_like_secret_key`, and the old loop cost scaled with the DECODED byte
    # count rather than with the 40-char window. Measured 14.6x at 48 bytes rising
    # to 69x at 1500; a 2 KB encoded blob fell from 86.3 us to 1.2 us, which is
    # 98% of what `_contains_bare_secret` spent on such a run.
    printable = len(raw) - len(raw.translate(None, _PRINTABLE_BYTES))
    return printable / len(raw) >= _SECRET_PRINTABLE_DECODE_RATIO


def _lowercase_run_exceeds(token: str, cap: int) -> bool:
    """Return True if any run of consecutive lowercase letters is longer than *cap*.

    Dictionary-word identifiers and file-path segments contain long lowercase
    word runs; a uniformly random base64 secret almost never does. This is the
    primary discriminator that keeps camelCase identifiers and mixed-case file
    paths out of the bare-secret heuristic.

    The only question the caller asks is whether the longest run EXCEEDS a
    threshold, so this stops at cap+1 rather than scanning the whole token to
    find the true maximum. On the tokens this gate exists to reject -- the ones
    with a long lowercase run -- it exits after a handful of characters instead
    of all 40, which measured 3.97 -> 1.65 us per window.
    """
    current = 0
    for ch in token:
        if ch.islower():
            current += 1
            if current > cap:
                return True
        else:
            current = 0
    return False


def _vowel_ratio(token: str) -> float:
    """Return the fraction of alphabetic characters in *token* that are vowels.

    Deliberately left in this two-pass comprehension form. A single-pass rewrite
    measured 1.18x -- about 0.4 us on a 2.89 us gate -- which does not justify
    replacing the clearest possible expression of "fraction of letters that are
    vowels", and would owe its own independent-oracle test. Its neighbour
    :func:`_lowercase_run_exceeds` WAS rewritten because that one measured 2.4x.
    Do not optimise this unmeasured.
    """
    letters = [ch for ch in token if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch in _VOWELS) / len(letters)


def _looks_like_secret_key(token: str) -> bool:
    """Return True if *token* has the shape of a bare AWS secret access key.

    Conservative, multi-gate classifier for a label-less 40-char base64 secret.
    Every gate must pass; the design bias is toward NOT
    redacting (a false negative merely reverts to today's behavior, a false
    positive corrupts benign output).

    Gates are ordered by MEASURED cost per rejection, cheapest-per-reject first.
    Every gate is a pure predicate whose failure returns False, so the order is
    verdict-neutral and can be chosen purely for cost. Measured on a corpus of
    1705 windows that clear gates 1-3 (cost per window, share of windows that
    gate rejects on its own):

        lowercase run   1.65 us   66.5%  ->  2.5 us per rejection
        vowel ratio     2.89 us   62.3%  ->  4.6 us per rejection
        entropy         8.48 us   54.5%  -> 15.5 us per rejection
        decode          3.01 us    0.0%  ->  rejected nothing in that corpus

    These numbers are a SNAPSHOT from one corpus on one machine: treat them as a
    relative ranking, not a budget, and do not turn them into assertions (this
    repo's CI enables coverage on 3.12 only, so absolute durations are not
    comparable across shards). The ordering is the durable claim, and it is
    guarded by a test that counts which gates get evaluated -- see
    ``TestSecretGateOrderIsCostOrdered``.

    Putting the two cheap structural gates ahead of the entropy computation, and
    the decode check last, halves the cost of gates 4-7 and measured -47% on
    ``redact_credentials`` end to end. Do not reorder these back into
    "structural last" without re-measuring: the structural gates are both
    cheaper AND higher-yield than entropy, which is the opposite of the
    intuition that entropy is the primary discriminator.

    1. Length is EXACTLY 40 (AWS secret-key length).
    2. Contains all three of lower + upper + digit (rejects all-lower prose runs,
       all-upper CONSTANT_NAMES, base32, digit strings).
    3. Not an all-hex run (rejects git SHAs, sha256/md5 digests).
    4. No lowercase run longer than _SECRET_MAX_LOWER_RUN.
    5. Vowel ratio <= _SECRET_MAX_VOWEL_RATIO. Gates 4 and 5 are the
       structural-randomness pair: they separate a random key from word-based
       identifiers and slash-delimited file paths that survive the entropy
       floor. Both apply to EVERY token (a '/' or '+' does not exempt a token,
       so 40-char mixed-case file paths stay intact).
    6. Shannon entropy >= _SECRET_ENTROPY_MIN (rejects low-entropy repeats/prose
       and most code identifiers, which cluster below 4.3).
    7. Does not base64-decode to printable text (rejects encoded-text blobs).
       Last because it is the lowest-yield gate, not because it is optional --
       it is what keeps legitimate OAuth ``code_challenge`` values in sign-in
       URLs from being redacted (guarded by the OAuth-URL corpus).

    BOUNDARY ASSUMPTION: this classifier deliberately evaluates an EXACTLY-40-char
    window (gate 1). It does NOT itself scan longer runs — a real key glued to an
    adjacent base64 char with no delimiter (e.g. ``X`` + key, key + ``A``,
    ``SECRET=`` + key + ``ABC``, key + ``X`` + key) forms a 41+ char run that would
    fail the exact-40 gate and leak verbatim. Callers that receive raw ``{40,}``
    runs MUST use :func:`_contains_bare_secret`, which slides a 40-char window
    across the run so a glued secret is still caught. Keep the exact-40 shape here:
    it is what lets the structural gates cleanly separate real keys from 64-char
    sha256 hex, base64 document blobs, etc.
    """
    if len(token) != _SECRET_KEY_LEN:
        return False
    if not _has_all_three_char_classes(token):
        return False
    if _HEX_ONLY_RE.match(token):
        return False
    if _lowercase_run_exceeds(token, _SECRET_MAX_LOWER_RUN):
        return False
    if _vowel_ratio(token) > _SECRET_MAX_VOWEL_RATIO:
        return False
    if _shannon_entropy(token) < _SECRET_ENTROPY_MIN:
        return False
    return not _decodes_to_printable_text(token)


def _contains_bare_secret(run: str) -> bool:
    """Return True if any 40-char window of *run* looks like a bare secret key.

    :func:`_looks_like_secret_key` only accepts an EXACTLY-40-char token, but the
    ``_BARE_SECRET_RUN_RE`` boundary look-arounds capture the longest possible run
    of base64-alphabet chars. A genuine 40-char secret glued to an adjacent
    base64 char with no delimiter (``X`` + key, key + ``A``, ``SECRET=`` + key +
    ``ABC``, key + ``X`` + key) produces a 41+ char run that would fail the
    exact-40 gate and leak verbatim. We slide a 40-char window across the run and
    report a hit if ANY window clears every gate. This stays linear in the run
    length (the regex yields disjoint spans), so cost is bounded overall.

    ENCODED-TEXT-BLOB EXCLUSION: if the WHOLE run base64-decodes to printable
    text it is a cohesive encoded blob (e.g. an OAuth/PKCE ``code_challenge``,
    which is ``base64(sha256-hex)``), not a bare secret — those are handled by
    the decode-and-scan pass instead. We must skip it here because sliding a
    40-char window byte-by-byte across such a blob creates base64-*misaligned*
    sub-windows whose garbage decode looks high-entropy and would clear every
    per-window gate, wrongly redacting a legitimate sign-in URL (regression
    guarded by the OAuth-URL corpus). This is the same bias-toward-not-redacting
    that :func:`_looks_like_secret_key` already applies per-window (gate 7),
    lifted to run granularity so a misaligned window cannot defeat it. A genuine
    glued secret (``X`` + key, key + ``ABC``, key + ``X`` + key) does NOT decode
    cleanly as a whole run, so it still reaches the sliding window below.
    """
    if len(run) < _SECRET_KEY_LEN:
        return False
    # RUN-LEVEL FAST PATH. Two of the per-window gates reject on a property that
    # is closed under substring, so asking about the whole run once can retire
    # every window without classifying any of them:
    #   gate 2 -- a character class absent from the run is absent from all of its
    #             substrings, so no window can hold all three;
    #   gate 3 -- every substring of an all-hex run is itself all-hex.
    # Both answers are False either way, so this only reorders WHICH check
    # returns False, never the verdict. Guarded on a run longer than one window,
    # because at exactly 40 chars the sole window pays the same two gates anyway
    # and the pre-check would be pure duplicate work. This is what keeps the
    # slide affordable on long non-secret runs (hex digests, lowercase blobs),
    # which are the common shape in tool output.
    if len(run) > _SECRET_KEY_LEN:
        if not _has_all_three_char_classes(run):
            return False
        if _HEX_ONLY_RE.match(run):
            return False
    if _decodes_to_printable_text(run):
        return False
    for start in range(len(run) - _SECRET_KEY_LEN + 1):
        if _looks_like_secret_key(run[start : start + _SECRET_KEY_LEN]):
            return True
    return False


def _decode_b64_chunk(chunk: str) -> str:
    """Decode ONE `_B64_CHUNK_RE` match; return decoded credential text or ''.

    Equivalent to `_decode_b64_safe(chunk)` when *chunk* is itself a
    `_B64_CHUNK_RE` match, but without re-scanning it. `_decode_b64_safe` exists
    to find chunks inside arbitrary text; re-running that scan over a string that
    IS already one chunk can only rediscover the same single span —
    `[A-Za-z0-9+/]{40,}` is greedy so it consumes the whole run, and `={0,2}`
    takes the padding — so the inner `finditer` was pure duplicate work on the
    hot path, once per base64-looking run in every redacted message.
    """
    # NO LENGTH SHORT-CIRCUIT HERE, deliberately. It is tempting to skip the decode
    # when `len(chunk) % 4` is non-zero, on the reasoning that `validate=True`
    # rejects a length that is not a multiple of 4. That reasoning is INTERPRETER
    # DEPENDENT and would be a redaction bypass: `binascii.a2b_base64`'s padding
    # leniency changed with `strict_mode`, so on Python 3.10 and 3.11 a chunk of 40
    # data characters plus one `=` (length 41) DECODES, while on 3.12 it raises.
    # Skipping it would leave a base64-encoded credential in that shape unredacted
    # on exactly the interpreters CI still builds. No version-invariant form of the
    # test exists either -- 43 data characters plus `==` decodes on 3.10 while
    # failing both a total-length and a stripped-length predicate. Pinned by
    # `test_a_decode_length_precondition_would_be_version_dependent`.
    try:
        decoded = base64.b64decode(chunk, validate=True).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # Gate the alternation behind the cheap superset pre-filter, exactly as pass 1
    # does. `_might_contain_credential` may return True where the pattern would not
    # match but never False where it would, so the verdict cannot move -- only the
    # cost. Real decoded blobs almost never look like credentials: 0 of 18 in the
    # session corpus and 1 of 849 in a hash-heavy corpus reach the alternation.
    #
    # LENGTH-GATED, because here the pre-filter is NOT unconditionally cheaper. Its
    # ~540 ns floor is fixed while the alternation's cost scales with length, so
    # below `_PREFILTER_MIN_LEN` the alternation wins outright. A decoded blob is
    # exactly the size where that matters -- 48 raw bytes from a 64-char run, and
    # shorter once `errors="ignore"` drops invalid sequences, measured 12-31
    # characters -- so this straddles the crossover instead of sitting above it.
    if len(decoded) >= _PREFILTER_MIN_LEN and not _might_contain_credential(decoded):
        return ""
    return decoded if _CREDENTIAL_PATTERNS.search(decoded) else ""


def _decode_b64_safe(text: str) -> str:
    """Try to base64-decode chunks in text; return decoded content or ''.

    Deliberately left UNOPTIMISED. `_decode_b64_chunk` above is the hot-path
    single-chunk form, and this function is what pins it: the differential test
    asserts the two agree on every chunk in the corpus, and the pre-optimisation
    reference oracle calls this one. Applying the same gates here would make both
    sides of that comparison share the change and the check would stop detecting
    anything.
    """
    for m in _B64_CHUNK_RE.finditer(text):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
            if _CREDENTIAL_PATTERNS.search(decoded):
                return decoded
        except Exception:
            continue
    return ""


def _contains_fixed_credential(text: str) -> bool:
    """Return True for canonical literal or base64-encoded credentials.

    Deliberately excludes the bare 40-character entropy heuristic. OAuth
    front-channel state and PKCE values are high-entropy by design, while the
    canonical signatures and decoded credentials remain unambiguous.
    """
    return bool(_CREDENTIAL_PATTERNS.search(text) or _decode_b64_safe(text))


def _text_contains_bare_secret(text: str) -> bool:
    """Return True when *text* contains an isolated bare AWS-secret run."""
    return any(
        _contains_bare_secret(match.group())
        for match in _BARE_SECRET_RUN_RE.finditer(text)
    )


# Markerless 40-character values collide with OAuth entropy only for these
# authorization-request fields. ``code_verifier`` is intentionally absent: it
# is sent to the token endpoint, not on this front channel.
_OAUTH_ENTROPY_QUERY_PARAMS = frozenset({"code_challenge", "nonce", "state"})

# The exemption is bounded to shapes the protocol itself can emit, so an
# AWS-secret-shaped run cannot ride a front-channel parameter into the blanked
# set. base64url (RFC 4648 s5) emits `-`/`_` and never `+`/`/`, and an S256
# challenge is base64url of a 32-byte digest -- exactly 43 characters.
_OAUTH_S256_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _oauth_entropy_form_is_protocol_shaped(key: str, form: str) -> bool:
    """Return True when ONE decoded form of a value keeps a protocol shape."""
    if key == "code_challenge":
        return bool(_OAUTH_S256_CHALLENGE_RE.fullmatch(form))
    return "+" not in form and "/" not in form


def _oauth_entropy_value_is_protocol_shaped(key: str, value: str) -> bool:
    """Return True when *value* has a shape OAuth entropy can legitimately take.

    EVERY decoded form must keep the shape, not just the raw one. Decoding once
    is not enough for the same reason it is not enough in `_exfil_url_warning`:
    a double-encoded payload (`%252F` -> `%2F` -> `/`) survives a single pass,
    so a raw-plus-one-decode test would let the base64-standard alphabet smuggle
    an AWS-secret-shaped run into the blanked set. Decode until the text stops
    changing, bounded by `_MAX_URL_DECODE_PASSES` so an over-encoded value
    cannot spin here.
    """
    candidate = value
    for _ in range(_MAX_URL_DECODE_PASSES):
        if not _oauth_entropy_form_is_protocol_shaped(key, candidate):
            return False
        decoded = unquote(candidate)
        if decoded == candidate:
            return True
        candidate = decoded
    # Budget ran out with a layer still to go. A value that is STILL decodable
    # was never seen in plaintext, so it cannot earn the exemption: refuse it
    # and let the markerless scan judge the value as written.
    return False


def _oauth_credential_scan_target(
    url: str,
    query: str,
    *,
    approved_endpoint: bool,
) -> str:
    """Blank entropy-bearing OAuth values before the markerless URL scan.

    Fixed credential signatures are checked against the raw and decoded URL
    before this target is built. At an exact approved endpoint, only the
    code-owned state, nonce, and PKCE challenge fields are omitted from the
    markerless bare-secret heuristic, and only when the value carries a shape
    the protocol can emit (see
    :func:`_oauth_entropy_value_is_protocol_shaped`). Other recognized values,
    parameter names, unknown parameters, and every non-query URL component
    remain in the scan target.
    """
    if not approved_endpoint or not query:
        return url

    sanitized_segments: list[str] = []
    for key, separator, value in (
        segment.partition("=") for segment in query.split("&")
    ):
        approved_value = (
            bool(separator)
            and key in _OAUTH_ENTROPY_QUERY_PARAMS
            and _oauth_entropy_value_is_protocol_shaped(key, value)
        )
        sanitized_segments.append(
            f"{key}{separator}" if approved_value else f"{key}{separator}{value}"
        )

    query_start = url.find("?")
    if query_start == -1:
        return url
    fragment_start = url.find("#", query_start + 1)
    suffix = "" if fragment_start == -1 else url[fragment_start:]
    sanitized_query = "&".join(sanitized_segments)
    return url[: query_start + 1] + sanitized_query + suffix


def diagnose_oauth_url_credential(url: str) -> OAuthUrlCredentialDiagnostic | None:
    """Return a safe rejection signature, never URL/value bytes or derivatives."""
    if not url:
        return None

    decoded_url = unquote(url)
    if "\\" in url:
        return _oauth_url_payload_diagnostic(
            "backslash_raw",
            url,
            url,
            lambda value: "\\" in value,
        )
    if "\\" in decoded_url:
        return _oauth_url_payload_diagnostic(
            "backslash_decoded",
            url,
            decoded_url,
            lambda value: "\\" in value,
            decoder=unquote,
        )
    if _contains_fixed_credential(url):
        return _oauth_url_payload_diagnostic(
            "fixed_credential_raw",
            url,
            url,
            _contains_fixed_credential,
        )
    if _contains_fixed_credential(decoded_url):
        return _oauth_url_payload_diagnostic(
            "fixed_credential_decoded",
            url,
            decoded_url,
            _contains_fixed_credential,
            decoder=unquote,
        )

    try:
        parsed = urlparse(url)
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return _oauth_diagnostic("parse_error", "url", url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return _oauth_diagnostic("invalid_endpoint", "scheme", parsed.scheme)
    if not parsed.hostname:
        return _oauth_diagnostic("invalid_endpoint", "authority", parsed.netloc)

    # Browsers and RFC-style parsers disagree on userinfo handling.
    if "@" in parsed.netloc:
        return _oauth_diagnostic(
            "userinfo",
            "userinfo",
            parsed.netloc.rpartition("@")[0],
        )
    decoded_netloc = unquote(parsed.netloc)
    if "@" in decoded_netloc:
        return _oauth_diagnostic(
            "userinfo",
            "userinfo",
            decoded_netloc.rpartition("@")[0],
        )

    approved_endpoint = (
        parsed.scheme.lower() == "https"
        and not port
        and _approved_oauth_authorization_endpoint(parsed.hostname.lower(), parsed.path)
    )
    scan_target = _oauth_credential_scan_target(
        url,
        parsed.query,
        approved_endpoint=approved_endpoint,
    )
    for candidate, suffix, decoder in (
        (scan_target, "raw", None),
        (unquote(scan_target), "decoded", unquote),
    ):
        if _contains_fixed_credential(candidate):
            return _oauth_url_payload_diagnostic(
                f"credential_scan_fixed_{suffix}",
                url,
                candidate,
                _contains_fixed_credential,
                decoder=decoder,
            )
        if _text_contains_bare_secret(candidate):
            return _oauth_url_payload_diagnostic(
                f"credential_scan_bare_secret_{suffix}",
                url,
                candidate,
                _text_contains_bare_secret,
                decoder=decoder,
            )

    # Provider consent URLs need neither path params nor fragments. Keep these
    # parser-differential forms fail-closed after the whole URL has been scanned.
    if parsed.params:
        return _oauth_diagnostic("path_params", "path_params", parsed.params)
    if ";" in parsed.path:
        return _oauth_diagnostic("path_semicolon", "path", parsed.path)
    if parsed.fragment:
        return _oauth_diagnostic("fragment", "fragment", parsed.fragment)

    path_and_query = parsed.path
    if parsed.query:
        path_and_query += f"?{parsed.query}"
    rules: list[str] = []
    warning = _exfil_url_warning(
        parsed.hostname,
        path_and_query,
        frozenset(),
        port=port,
        is_https=parsed.scheme.lower() == "https",
        allow_safe_presigned=False,
        allow_oauth_entropy=True,
        _rule_out=rules,
    )
    if warning is None:
        return None
    rule = rules[0] if rules else "exfil_unknown"

    heuristic_query = parsed.query
    if approved_endpoint:
        heuristic_query = "&".join(
            segment
            for segment in parsed.query.split("&")
            if segment.partition("=")[0] not in _OAUTH_QUERY_PARAMS
        )
    else:
        slack_alias = _kirocrew_slack_app_link_alias(
            parsed.hostname.lower(),
            parsed.path,
            parsed.query,
            is_https=parsed.scheme.lower() == "https",
            port=port,
        )
        if slack_alias is not None:
            heuristic_query = slack_alias

    if rule == "exfil_query_length":
        return _oauth_query_diagnostic(rule, heuristic_query)
    if rule == "exfil_query_pattern":
        query_decoder: Callable[[str], str] | None = (
            None if _EXFIL_PATTERNS.search(heuristic_query) else unquote_plus
        )
        return _oauth_query_diagnostic(
            rule,
            heuristic_query,
            predicate=lambda value: bool(_EXFIL_PATTERNS.search(value)),
            decoder=query_decoder,
        )
    if rule == "exfil_hard_credential":
        return _oauth_url_payload_diagnostic(
            rule,
            url,
            url,
            lambda value: bool(_HARD_CREDENTIAL_RE.search(value)),
        )
    if rule == "exfil_fixed_credential":
        return _oauth_url_payload_diagnostic(
            rule,
            url,
            url,
            _contains_fixed_credential,
        )
    if rule == "exfil_percent_encoding":
        return _oauth_url_payload_diagnostic(
            rule,
            url,
            url,
            lambda value: bool(_EXFIL_PERCENT_RE.search(value)),
        )

    target = url
    if rule in {"exfil_encoded_credential", "exfil_decode_saturated"}:
        for _ in range(_MAX_URL_DECODE_PASSES):
            decoded = unquote_plus(target)
            if decoded == target:
                break
            target = decoded
    return _oauth_diagnostic(rule, "url", target)


def oauth_url_contains_credential(url: str) -> bool:
    """Return True when an ACP-provided OAuth banner URL is unsafe."""
    diagnostic = diagnose_oauth_url_credential(url)
    if diagnostic is None:
        return False
    shape = diagnostic.shape
    logger.warning(
        "OAuth URL rejected rule=%s component=%s parameter=%s "
        "length=%d upper=%d lower=%d digits=%d percent=%d symbols=%d other=%d",
        diagnostic.rule,
        diagnostic.component,
        diagnostic.parameter or "-",
        shape.length,
        shape.ascii_uppercase,
        shape.ascii_lowercase,
        shape.digits,
        shape.percent_signs,
        shape.symbols,
        shape.other,
    )
    return True


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


# Standard replacement tag for a redacted credential. Shared between the batch
# redactor (`redact_credentials`) and the streaming fail-closed path
# (`StreamRedactor.feed`) so the on-the-wire marker is identical everywhere.
_REDACTED_CREDENTIAL_TAG = "[REDACTED: credential]"

# Public alias for modules that must emit the SAME tag rather than duplicate the
# literal — e.g. the pptx-maker preview, which excises a credential-bearing bitmap
# itself because this module's redactor recognises a narrower token set than that
# scan matches.
REDACTED_CREDENTIAL_TAG = _REDACTED_CREDENTIAL_TAG

#: Replacement tag for pass 2 (a base64-encoded credential). DISTINCT from
#: ``_REDACTED_CREDENTIAL_TAG`` and deliberately not a superstring of it, so a
#: consumer counting one tag does not accidentally match the other. Kept PRIVATE:
#: consumers should ask ``CREDENTIAL_REDACTION_TAGS`` below rather than name
#: individual tags, which is the whole point of that registry.
_REDACTED_ENCODED_CREDENTIAL_TAG = "[REDACTED: encoded credential]"

#: EVERY tag :func:`redact_credentials` can substitute for a credential, owned
#: HERE beside the passes that emit them rather than enumerated by each caller.
#: A consumer that needs to answer "did the CREDENTIAL redactor replace something
#: in this text" must check all of them: pass 1 (plaintext patterns) and pass 3
#: (bare secret runs) write ``_REDACTED_CREDENTIAL_TAG``, pass 2 (base64-encoded)
#: writes ``_REDACTED_ENCODED_CREDENTIAL_TAG``.
#:
#: Scope is deliberately CREDENTIALS ONLY, and a consumer must not read it as "was
#: this text rewritten at all". :func:`redact_exfiltration_urls` is a separate
#: rewriter that substitutes ``[REDACTED: suspicious URL to <domain>]`` -- a
#: variable string, so it is prefix-matched rather than compared, which is why it
#: is not a member here. Text can therefore be rewritten with every tag in this
#: tuple absent.
#:
#: This tuple exists because the enumeration used to live at the call site, where
#: it silently missed the encoded tag and under-reported redactions on the
#: dashboard chat notice. Co-locating it means a NEW tag is added next to the list
#: that must name it; ``test_every_redaction_tag_constant_is_registered`` fails if
#: one is added without registering it, so the drift cannot recur silently.
#:
#: Invariant relied on by callers that SUM per-tag counts: no tag is a substring
#: of another, so one substitution cannot be counted twice.
CREDENTIAL_REDACTION_TAGS = (_REDACTED_CREDENTIAL_TAG, _REDACTED_ENCODED_CREDENTIAL_TAG)


def redact_credentials(text: str) -> tuple[str, list[str]]:
    """Redact raw credential patterns from text, including base64-encoded.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings: list[str] = []
    result = text

    # 1. Redact plaintext credential patterns
    #
    # Gated on the cheap superset pre-filter: when no branch of
    # `_CREDENTIAL_PATTERNS` can possibly match, `finditer` would yield nothing
    # and the loop body would not run, so skipping it cannot change the output.
    # This is the hot path — the alternation is 23 branches retried at nearly
    # every position, and real text almost never contains a credential.
    if _might_contain_credential(result):

        def _redact_one(m: re.Match[str]) -> str:
            # Emit ONLY non-sensitive metadata (length). Do NOT slice any part of
            # the match into the warning: `_CREDENTIAL_PATTERNS` matches the raw
            # secret value itself (e.g. `ghp_…`, `sk-ant-…`), so even a short prefix
            # is genuine plaintext key material — a fixed-length token prefix leaves
            # ~12-16 secret chars in a 20-char slice. The warnings list is a
            # redaction-subsystem output expected to be safe to log/surface, so it
            # must carry no secret bytes. Mirrors the base64 / bare-secret branches
            # below, which already log length only.
            warnings.append(f"Redacted credential pattern ({len(m.group())} chars)")
            return _REDACTED_CREDENTIAL_TAG

        # ONE pass. `sub` walks the matches left-to-right exactly as `finditer`
        # did and calls the replacer in that same order, so `warnings` is
        # appended in an identical order with identical contents. The previous
        # shape rebuilt the entire string per match via
        # `result.replace(matched, tag, 1)` — O(n) per match, O(n²) overall on
        # credential-dense text — and replaced the FIRST occurrence of the
        # matched text rather than the span that actually matched. `sub` splices
        # each matched span in place, which is both linear and positionally
        # exact.
        result = _CREDENTIAL_PATTERNS.sub(_redact_one, result)

    # Passes 2 and 3 both scan the ORIGINAL `text` for runs of the base64
    # alphabet, and they select the SAME spans: `[A-Za-z0-9+/]{40,}` is greedy and
    # leftmost, so it yields exactly the maximal runs of length >= 40 — which is
    # also precisely what `_BARE_SECRET_RUN_RE`'s `(?<![A-Za-z0-9+/])` /
    # `(?![A-Za-z0-9+/])` boundaries select. The only difference is the trailing
    # `={0,2}` padding that `_B64_CHUNK_RE` additionally consumes, and `=` is not
    # in the run's character class, so `rstrip("=")` recovers the bare run
    # exactly. So one scan feeds both passes instead of two.
    #
    # The two loops stay SEPARATE and in their original order. Fusing them into a
    # single per-run loop would interleave the passes, which changes both the
    # order of `warnings` and — because each pass mutates `result` via
    # `str.replace(…, 1)` — which occurrence each replacement lands on, and
    # whether pass 3's `run not in result` guard sees pass 2's edits. Sharing the
    # scan while keeping the loops ordered is what makes this byte-identical.
    b64_chunks = [m.group() for m in _B64_CHUNK_RE.finditer(text)]

    # 2. Detect and redact base64-encoded credentials
    for chunk in b64_chunks:
        decoded = _decode_b64_chunk(chunk)
        if decoded:
            result = result.replace(chunk, _REDACTED_ENCODED_CREDENTIAL_TAG, 1)
            warnings.append(f"Redacted base64-encoded credential ({len(chunk)} chars)")

    # 3. Detect and redact BARE 40-char AWS secret keys with no label/prefix
    # These carry no distinctive marker for _CREDENTIAL_PATTERNS
    # to anchor on, so an entropy + structural heuristic is the only way to catch
    # a standalone secret value. Scan the ORIGINAL text (not the already-mutated
    # result) so match offsets are stable; skip any run whose text has already
    # been redacted away by an earlier pass.
    for chunk in b64_chunks:
        run = chunk.rstrip("=")
        # Slide a 40-char window across the run rather than gating the whole run
        # on len == 40: a real secret glued to an adjacent base64 char (no
        # delimiter) yields a 41+ char run that the exact-40 shape check would
        # miss, leaking the key verbatim. Redact the whole run if ANY window is a
        # secret.
        if not _contains_bare_secret(run):
            continue
        if run not in result:
            # Already redacted by pass 1/2 (e.g. it was a labelled value or an
            # encoded-credential chunk) — nothing left to replace.
            continue
        result = result.replace(run, _REDACTED_CREDENTIAL_TAG, 1)
        warnings.append(f"Redacted bare secret key ({len(run)} chars)")

    return result, warnings


# Suspicious bash patterns to flag during audit
SUSPICIOUS_BASH_PATTERNS: list[str] = [
    "curl * | bash",
    "curl * | sh",
    "wget * | bash",
    "| bash",
    "| sh",
    "| python",
    "| perl",
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "find * -delete",
    "find * -exec rm",
    "find * -exec shred",
    "xargs rm",
    "git clean -f",
    "shred ",
    "truncate ",
    "> /dev/sd",
    "mkfs.",
    "dd if=",
    "chmod 777",
    "chmod */usr/",
    "chmod */etc/",
    "chmod */sbin/",
    "chmod */boot/",
    "chmod */lib/",
    "chmod */lib64/",
    "chown */usr/",
    "chown */etc/",
    "chown */sbin/",
    "chown */boot/",
    "chown */lib/",
    "chown */lib64/",
    "eval $(",
    "base64 -d",
    "nc -e",
    "ncat -e",
    "/dev/tcp/",
    "xp_cmdshell",
    "GRANT ALL",
    "DROP DATABASE",
    "DROP TABLE",
    "TRUNCATE TABLE",
    "aws iam create-access-key",
    "aws sts assume-role",
    "export AWS_SECRET",
    "export AWS_ACCESS",
    "curl * -d @",
    "curl * --data @",
    "curl * -F file=@",
    "curl -d @",
    "curl --data @",
    "curl -F file=@",
    "wget --post-file",
    "nc * < ",
]

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


# Absolute filesystem paths, POSIX and Windows. Deliberately narrow: anchored to
# real filesystem roots rather than "any slash-separated token", and both branches
# refuse to start mid-token so a URL is never mistaken for a path -- without the
# lookbehinds, ``https://api.github.com/repos/x`` matches twice (``s:/`` as a drive
# letter, ``/repos`` as a root) and the URL is destroyed.
_LOCAL_PATH_RE = re.compile(
    r"(?:"
    r"(?<![\w:/])/(?:home|Users|root|tmp|var|opt|usr|etc|private|mnt|srv|workspace|workplace)"
    r"|(?<![A-Za-z])[A-Za-z]:\\"
    r")"
    r"[^\s'\"<>|]*"
)
_LOCAL_PATH_PLACEHOLDER = "[redacted-path]"


def redact_local_paths(text: str) -> tuple[str, list[str]]:
    """Strip absolute host filesystem paths from *text*.

    Complements :func:`redact_credentials`, which matches credential *patterns*
    and leaves a bare path such as
    ``[Errno 2] No such file or directory: '/home/alice/.kiro/crew/vaults/v1'``
    untouched. That string is the common shape of an OS or subprocess error, and
    on an error surface that reaches a browser it discloses the account name and
    on-disk layout of the host (CWE-209).

    Returns the redacted text and a list of human-readable notes, matching the
    signature of the sibling passes so callers can chain them uniformly.
    """
    notes: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        notes.append(f"Redacted local path ({len(match.group(0))} chars)")
        return _LOCAL_PATH_PLACEHOLDER

    return _LOCAL_PATH_RE.sub(_sub, text), notes


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


def _deny_pattern_matches(pattern: str, text: str, is_regex: bool) -> bool:
    """Match ``text`` (already lowercased) against a deny ``pattern``.

    Regex tier: matched via ``_deny_matcher`` (a memoized, ReDoS-safe
    *linear-time* matcher for the raw pattern — see the ReDoS-mitigation notes
    on ``_DenyMatcher``).  The matcher scans the FULL string, so a destructive
    needle at any offset (e.g. after a long benign prefix inside one un-split
    shell segment) is found — no length truncation.  A malformed stored pattern
    (``re.error``) is treated as a non-match so a single bad custom rule cannot
    wedge the whole gate — other rules still enforce.  Glob tier: ``fnmatch``
    (case-insensitive), unchanged.
    """
    if is_regex:
        return _deny_matcher(pattern).match(text)
    return fnmatch.fnmatch(text, pattern.lower())


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


def _deny_reason(
    matched: str,
    reason_notes: "dict[str, str] | None",
    *,
    note_override: str = "",
) -> str:
    """Refusal text for *matched*, with the operator note on a SECOND line.

    The first line is byte-for-byte what it has always been. That is load bearing,
    not stylistic: ``RecoveryCard.tsx`` extracts the pattern with
    ``/Blocked by security policy:\\s*(.+?)\\s*$/gm`` -- per-line and end-anchored --
    so anything appended to the SAME line is captured as part of the pattern, and
    ``_denied_by`` in the test suite partitions on the exact
    ``"Blocked by security policy: "`` separator.  A note therefore goes on its own
    line, where both readers ignore it.

    Built-in rules never carry a note (the map holds user patterns only), so for them
    this returns exactly the historical string -- unless the caller passes
    *note_override*, which the argv-structural floor uses to say why a pattern the
    input does not literally match was still the rule that fired.  Without it the
    reported pattern is the rule's catalog regex, which for that path provably
    cannot match the input, so the reason names a cause the reader can disprove.

    Module-level rather than a closure because EVERY tier that can refuse must emit
    the identical micro-format: a second producer would be free to drift from the
    three consumers that parse it.
    """
    head = f"{DENY_REASON_PREFIX}{matched}"
    note = (note_override or (reason_notes or {}).get(matched, "")).strip()
    return f"{head}\n{note}" if note else head


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

    def _reason(matched: str, note_override: str = "") -> str:
        """Refusal text for *matched* -- see :func:`_deny_reason`, the shared producer.

        *note_override* lets the argv-structural floor say why a pattern the input
        does not literally match was still the rule that fired (see
        ``_SELF_PROTECTION_FLOOR_NOTES``).
        """
        return _deny_reason(matched, reason_notes, note_override=note_override)

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
            return _reason(tag, f"{note} (rule pattern: {gated_pattern})".strip())
        push_allow_pending = True

    # ── Self-protection floor (argv-structural, not a glob) ──
    # Runs before the pattern passes and on the WHOLE string, for the same reason
    # the git-publish floor does: the evasions live in shell syntax that textual
    # splitting scatters or mis-reads.  Each predicate is checked only if its
    # rule is still in the effective set, so an operator-disabled rule stays
    # disabled.
    for rule_id, predicate in (
        ("credential-exfil-kirocrew-token", _is_credential_mint),
        ("self-protection-kill", _is_self_kill),
        ("self-protection-restart", _is_self_restart),
        ("self-protection-update", _is_self_update),
        ("self-protection-gateway-restart", _is_self_gateway_restart),
        ("self-protection-cloud", _is_self_cloud_destructive),
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
            return _reason(pattern, _SELF_PROTECTION_FLOOR_NOTES.get(rule_id, ""))

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


# Data-egress / reverse-shell command shapes — the exfiltration-specific subset
# of SUSPICIOUS_BASH_PATTERNS. These are enforced at the
# tool-invocation gate (denied), unlike the full SUSPICIOUS_BASH_PATTERNS list
# which stays advisory: that list also carries destructive-but-local shapes
# (rm -rf, dd if=, chmod on system dirs, DROP TABLE) that a user may legitimately
# run in their own workspace, so hard-denying all of them at the gate would break
# ordinary use. This subset is narrowly the "push local data OUT / open a shell
# to a remote" shapes, where a hijacked-agent block is worth the rare false
# positive.
#
# Entries containing `*` are fnmatch globs (`*<pat>*`); the rest are
# case-insensitive substrings, so they fire regardless of intervening flags /
# token layout — `curl -d @f`, `curl -s -d @f`, `curl --data-binary @f` all
# match. The `@` sigil on curl body/upload flags means "read from a local file"
# (the tell-tale of egress); a bare `-d 'x=1'` inline body has no `@` and is not
# matched. curl long options accept BOTH ` @` and `=@` separators, so both are
# listed. `--data-raw` is deliberately EXCLUDED: it is the one --data variant
# that does NOT interpret a leading `@` as a file reference, so `--data-raw @x`
# posts the literal string `@x` (never reads a file) — including it would only
# add false positives. Multipart uploads use a glob (`-F *=@`) so ANY field name
# matches, not just a field literally named `file` (`curl -F x=@secret` exfils
# just as well).
_BASH_EXFIL_PATTERNS: list[str] = [
    "-d @",  # curl POST body read from a local file (space + `=` separators)
    "-d@",
    "-d=@",
    "--data @",
    "--data=@",
    "--data-binary @",
    "--data-binary=@",
    "--data-ascii @",
    "--data-ascii=@",
    "--data-urlencode @",  # also reads a local file when the value starts with @
    "--data-urlencode=@",
    "-F *=@",  # curl multipart file upload, any field name (glob)
    "--form *=@",
    "--upload-file",  # curl upload, long form
    "wget --post-file",  # wget file upload
    "/dev/tcp/",  # bash builtin reverse shell (>/dev/tcp/host/port)
    "/dev/udp/",
]

# Exfil shapes where whitespace or flag CASE around an operator matters, so a
# plain lowercased substring/glob would either miss a no-space variant or
# false-positive. Matched via regex against the ORIGINAL (non-lowercased)
# command. Each entry is (compiled pattern, human label).
_BASH_EXFIL_RES: list[tuple[re.Pattern[str], str]] = [
    # netcat reading a local file via input redirect — `nc host port < file` AND
    # `nc host port <file` (no space after `<`, a valid shell redirect that the
    # old `nc * < ` glob missed). `nc`/`ncat` is anchored at a word boundary so
    # `sync`/`func` etc. do not match. Case-insensitive (command name).
    (re.compile(r"(?:^|\s)nc(?:at)?\s+\S.*<", re.IGNORECASE), "nc/ncat file redirect"),
    # netcat reverse shell `nc -e <prog>` / `ncat -e <prog>`. `nc`/`ncat` is
    # anchored at a word boundary so `rsync -e ssh` (contains `nc -e`) and
    # `vnc -e` do NOT match; a plain substring `"nc -e"` false-positived on them.
    (re.compile(r"(?:^|\s)nc(?:at)?\s+-e\b", re.IGNORECASE), "nc/ncat reverse shell"),
    # curl upload short form `-T <file>` / `-Tfile` (no space). CASE-SENSITIVE
    # `-T`: curl's upload flag is uppercase, so this does NOT match lowercase long
    # options such as `--trace-time`. `-T` must begin at a word boundary.
    (re.compile(r"\bcurl\b.*(?:^|\s)-T\s*\S"), "curl -T upload"),
]


# Which catalog rule each always-on exfil branch enforces, so a denial maps back
# to a rule id and an operator opt-out is honoured. Patterns/labels absent from
# these maps stay unconditional.
_BASH_EXFIL_RULE_BY_PATTERN: dict[str, str] = {
    "-d @": "data-exfil-curl-file-body",
    "-d@": "data-exfil-curl-file-body",
    "-d=@": "data-exfil-curl-file-body",
    "--data @": "data-exfil-curl-file-body",
    "--data=@": "data-exfil-curl-file-body",
    "--data-binary @": "data-exfil-curl-file-body",
    "--data-binary=@": "data-exfil-curl-file-body",
    "--data-ascii @": "data-exfil-curl-file-body",
    "--data-ascii=@": "data-exfil-curl-file-body",
    "--data-urlencode @": "data-exfil-curl-file-body",
    "--data-urlencode=@": "data-exfil-curl-file-body",
    "-F *=@": "data-exfil-curl-multipart-upload",
    "--form *=@": "data-exfil-curl-multipart-upload",
    "--upload-file": "data-exfil-curl-upload",
    "wget --post-file": "data-exfil-wget-post-file",
    "/dev/tcp/": "reverse-shell-devtcp",
    "/dev/udp/": "reverse-shell-devtcp",
}

# A single regex can span more than one catalog row, so this maps to a TUPLE. The
# gate attributes each MATCH to one of those rows and honours that row's own
# toggle — see _exfil_rule_id_for_match.
_BASH_EXFIL_RULE_BY_LABEL: dict[str, tuple[str, ...]] = {
    "nc/ncat file redirect": ("data-exfil-nc-file-redirect",),
    "nc/ncat reverse shell": ("reverse-shell-nc", "reverse-shell-ncat"),
    "curl -T upload": ("data-exfil-curl-upload",),
}

#: For a label whose regex spans several catalog rows, the token that identifies
#: WHICH row a given match belongs to. Ordered longest-first so ``ncat`` is tested
#: before ``nc`` — the reverse would classify every ``ncat`` hit as ``nc``.
_BASH_EXFIL_ROW_DISCRIMINATORS: dict[str, tuple[tuple[str, str], ...]] = {
    "nc/ncat reverse shell": (("ncat", "reverse-shell-ncat"), ("nc", "reverse-shell-nc")),
}


def _exfil_rule_id_for_match(label: str, matched: str, rule_ids: tuple[str, ...]) -> str:
    """The catalog row a single exfil match belongs to.

    One regex can cover more than one row, and the operator toggles rows, not
    regexes — so a match has to be attributed before its toggle can be honoured.
    Falls back to the label's first row when nothing discriminates, which keeps the
    single-row labels (the common case) on their existing behaviour and never
    returns an id outside ``rule_ids``.
    """
    low = matched.lower()
    for token, rid in _BASH_EXFIL_ROW_DISCRIMINATORS.get(label, ()):
        if token in low and rid in rule_ids:
            return rid
    return rule_ids[0]


def audit_bash_exfiltration(
    command: str, *, enabled_ids: "frozenset[str] | None" = None
) -> str | None:
    """Return a denial reason if *command* matches a data-egress / reverse-shell
    shape that must be blocked at the tool-invocation gate, else None.

    Scoped to _BASH_EXFIL_PATTERNS / _BASH_EXFIL_RES (exfil/reverse-shell only) so
    it can be wired into the deny path in ``hooks.on_tool_call`` without blocking
    benign local commands. The broader :func:`audit_bash_command` stays advisory.

    Every branch carries the id of the catalog rule it enforces, so *enabled_ids*
    lets the caller honour an operator opt-out: a branch whose rule the operator
    disabled is skipped. ``None`` (the default) means ALL enabled — fail-closed,
    which is what keeps the callers that hold no effective set (cron command
    vetting, computer-use input vetting) at full strength without a change.
    """
    lower = command.lower()

    def _on(rule_id: str) -> bool:
        return enabled_ids is None or rule_id in enabled_ids

    for pattern in _BASH_EXFIL_PATTERNS:
        rule_id = _BASH_EXFIL_RULE_BY_PATTERN.get(pattern, "")
        if rule_id and not _on(rule_id):
            continue
        pat = pattern.lower()
        if "*" in pat:
            if fnmatch.fnmatch(lower, f"*{pat}*"):
                return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
        elif pat in lower:
            return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
    for rx, label in _BASH_EXFIL_RES:
        rule_ids = _BASH_EXFIL_RULE_BY_LABEL.get(label, ())
        if not rule_ids:
            if rx.search(command):
                return f"Blocked: command matches data-exfiltration pattern ({label})"
            continue
        # A label can span more than one catalog row (one regex covers both the nc
        # and ncat rules). Denying while EITHER is enabled defeats the operator:
        # switching `reverse-shell-nc` off left `nc` blocked by its sibling. So
        # resolve each MATCH to the row it actually belongs to and honour that
        # row's own toggle. Every match is examined, not just the first, because a
        # command can carry both spellings and the leading one may be the disabled
        # row while the other is still enforced.
        for m in rx.finditer(command):
            matched_id = _exfil_rule_id_for_match(label, m.group(0), rule_ids)
            if _on(matched_id):
                return f"Blocked: command matches data-exfiltration pattern ({label})"
        continue
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


def contains_injection(text: str | None) -> bool:
    """Return True if *text* matches a known prompt-injection pattern.

    Accepts ``None`` (returns ``False``) so callers can screen optional
    fetched content — e.g. a Slack ``thread_parent_text`` that may be unset —
    without a separate None check.

    Public wrapper over the shared ``_INJECTION_PATTERNS`` set (defined in the
    dependency-free ``vector_memory_constants`` module) so untrusted content
    pulled from external surfaces — e.g. Slack thread-parent / thread-metadata
    fetched from arbitrary, possibly non-owner authors — can be screened
    before it is injected into the LLM prompt. The pattern set lives in the
    light constants module (not ``vector_memory``, whose numpy/faiss/stemmer
    deps are heavy), so it is imported at module top level with no lazy import
    and no fail-open path: a screen that cannot run must not silently pass
    untrusted content through.
    """
    if not text:
        return False
    return _contains_injection(text)


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


# ── Shell-aware command normalizer ──
# Strips shell quoting tricks, expands tilde/HOME, and resolves paths so that
# obfuscated commands (e.g. ca""t ~/.aws/credentials, $HOME/.ssh/id_rsa) are
# reduced to their canonical form before deny-list matching.

# Regex to strip empty-string concatenation: paired quotes ('' or "") that
# vanish (e.g. g""it -> git, ca''t -> cat).
_EMPTY_QUOTE_RE = re.compile(r'""|\'\'')

# Regex for $HOME or ${HOME} variable expansion.
_HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME", re.IGNORECASE)

# ANSI-C (``$'…'``) and locale (``$"…"``) quoting.  Both are QUOTING forms whose
# value the shell computes before the program sees it, so they are resolved as part
# of tokenization.  Matched on the RAW text rather than after ``shlex``, which is
# what makes it safe: ``shlex`` removes the quotes but leaves the ``$`` glued to the
# content, and at that point ``$'/'`` -> ``$/`` is indistinguishable from a variable
# reference like ``$HOME``, so stripping the ``$`` post-hoc would eat real variables.
# Requiring the quote character here means a bare ``$HOME`` never matches.
#
# The negated classes EXCLUDE the backslash, and that is a ReDoS fix, not a style
# choice.  With ``[^']`` a backslash could match either alternative -- ``\\.`` (two
# characters) or the class (one) -- the textbook ambiguous quoted-string pattern, so
# an unterminated ``$'`` followed by a run of backslashes forces the engine through
# ~1.618**n tilings of that run.  This regex runs inside the PreToolUse gate on the
# full, uncapped command, so that is a hang, not a slowdown (measured: 9 ms at 24
# backslashes, growing ~1.6x per character).  Excluding the backslash makes the
# alternation unambiguous -- a backslash is always consumed by ``\\.`` -- while
# accepting exactly the same language.  Found by the Opus 4.8 review lane.
_ANSI_C_QUOTE_RE = re.compile(r"\$'((?:\\.|[^'\\])*)'|\$\"((?:\\.|[^\"\\])*)\"", re.DOTALL)

# Single-character ANSI-C escapes that stand for a LITERAL character.  These are
# the ones bash resolves and a matcher must therefore see resolved: without them
# ``$'rm -rf \"/\"'`` keeps its backslashes and the rule does not match, while bash
# passes the plain quotes (BLOCKING from the GPT 5.6 lane).
_ANSI_C_LITERAL_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "?": "?"}
# Escapes that stand for a control character.  Mapped to a SPACE rather than the
# character itself, which is what ``_decode_printf_escapes`` has always done for
# this family: the value of resolving them here is that a token boundary appears
# where the shell puts one, and a literal control byte in a matched view would
# only travel into the audit record.  Deliberate, and the reason this decoder is
# not simply "what bash produces".
_ANSI_C_SPACE_ESCAPES = frozenset("abefnrtvE")


def _decode_ansi_c_body(body: str) -> str:
    """Resolve the escapes inside one ``$'…'`` body, in a SINGLE left-to-right pass.

    One pass is the whole point.  Sequential ``str.replace`` calls let one
    substitution's OUTPUT be re-read as another's input: ``$'\\\\n'`` is an escaped
    backslash followed by the letter ``n`` (two characters), but a chain that
    resolves ``\\\\`` first and then looks for ``\\n`` collapses it to whitespace and
    invents a separator bash never passed.  Consuming each escape atomically here
    makes that impossible -- the same failure mode as the Unicode-width guess this
    file already carries a note about.

    Numeric forms keep bash's exact widths (``\\xHH``, ``\\nnn`` octal, ``\\uHHHH``,
    ``\\UHHHHHHHH``) and the inert guard, so a NUL or lone surrogate stays encoded.
    An unrecognised escape keeps both characters, as bash does.
    """
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in _ANSI_C_LITERAL_ESCAPES:
            out.append(_ANSI_C_LITERAL_ESCAPES[nxt])
            i += 2
            continue
        if nxt in _ANSI_C_SPACE_ESCAPES:
            out.append(" ")
            i += 2
            continue
        if nxt == "c" and i + 2 < n and body[i + 2].isascii():
            # ``\cX`` is a CONTROL character, and ``\cI`` is a TAB -- so
            # ``bash -c $'rm\\cI-rf /'`` hands the inner shell a tab-separated
            # ``rm -rf /`` and it runs (BLOCKING from the GPT 5.6 lane; measured,
            # the inner shell does split on it).  The mapping is MEASURED rather
            # than derived: ``ord(upper(X)) & 0x1F``, with ``?`` special-cased to
            # 0x7F -- an XOR-0x40 guess gets ``\\c0`` wrong (bash gives 0x10, not
            # ``p``).  The result is always a control character, so it takes the
            # same normalization to a SPACE as the named family above, which is
            # what puts a token boundary where the shell puts one.
            #
            # Restricted to a single ASCII character, because ``str.upper()`` is
            # not length-preserving outside it: ``"ß".upper()`` is ``"SS"``, and
            # ``ord`` of that raised ``TypeError`` straight out of the permission
            # gate on ``echo $'\\cß'`` -- a crash where a security decision belongs
            # (also BLOCKING, same lane).  A non-ASCII target falls through to the
            # unrecognised branch and keeps both characters, as bash does for a
            # spelling it does not define.
            target = body[i + 2]
            code = 0x7F if target == "?" else (ord(target.upper()) & 0x1F)
            if code == 0:
                # ``\c@`` is a NUL, and bash TRUNCATES the word there -- see the
                # numeric branch below for the measurement.
                return "".join(out)
            out.append(" ")
            i += 3
            continue
        match = _ANSI_C_NUMERIC_ESCAPE_RE.match(body, i)
        if match:
            # A NUL TRUNCATES the word -- bash cannot place one in an argv, and what
            # it does instead is stop there.  Measured on every spelling that can
            # reach zero: ``$'AA\\0junk'``, ``$'AA\\400junk'``, ``$'AA\\x00junk'``,
            # ``$'AA\\u0000j'`` and ``$'AA\\c@junk'`` all yield ``AA``, and
            # ``$'\\0AA'`` yields the empty word.  Leaving the escape encoded instead
            # was a bypass: ``$'dd\\0junk' if=/dev/zero of=/dev/sda`` ran the
            # destructive command while the view held ``dd\\0junk if=`` and matched
            # nothing (BLOCKING from the GPT 5.6 lane).  The OTHER inert codes -- out
            # of range, lone surrogate -- keep the escape rather than truncating,
            # because bash does not produce them at all and guessing what it would do
            # is what the measurements above exist to avoid.
            numeric_code = _numeric_escape_code(match)
            if numeric_code == 0:
                return "".join(out)
            out.append(_numeric_escape_char(match))
            i = match.end()
            continue
        # Unrecognised: bash keeps the backslash and the character.
        out.append(body[i : i + 2])
        i += 2
    return "".join(out)


def _decode_shell_quoted_literals(cmd: str) -> str:
    """Resolve each ``$'…'`` span, and reduce each ``$"…"`` to plain double quotes.

    ``rm -rf $'/'`` runs exactly what ``rm -rf /`` runs, and ``$'\\x2d\\x76'`` is
    ``-v``, so a matcher that has not resolved these is reading a spelling the
    shell never hands over.  An ANSI-C value is re-quoted with ``shlex.quote`` so a
    value containing whitespace or a quote stays ONE token through ``shlex.split``.

    ``$"…"`` is LOCALE TRANSLATION, and it is NOT ANSI-C -- measured, because
    treating the two alike was a bypass (BLOCKING from the GPT 5.6 lane).  Bash
    gives ``$"\\r\\mAA"`` the word ``\\r\\mAA``, byte-identical to plain
    ``"\\r\\mAA"``: inside double quotes a backslash escapes only ``$``, `````,
    ``"``, ``\\`` and a newline, so ``\\r`` is a literal backslash-r and NOT a
    carriage return.  Decoding it as ANSI-C turned that ``\\r`` into whitespace and
    the command vanished from the view, while the inner shell of
    ``bash -c $"\\r\\m -rf /"`` resolves the backslashes in its OWN lexing pass and
    runs the destructive command (measured: it executes ``rmAA`` for
    ``bash -c $"\\r\\mAA"``).  So the ``$`` is dropped and the double-quoted text is
    left for ``shlex`` to resolve by double-quote rules -- which also keeps
    ``rm -rf $"/"`` reaching the rule, since bash's operand there is ``/``.
    """

    def _replace(match: re.Match[str]) -> str:
        ansi_c_body = match.group(1)
        if ansi_c_body is not None:
            return shlex.quote(_decode_ansi_c_body(ansi_c_body))
        return '"' + (match.group(2) or "") + '"'

    return _ANSI_C_QUOTE_RE.sub(_replace, cmd)


def _shell_tokens(cmd: str) -> list[str]:
    """Tokenize *cmd* the way a POSIX shell hands argv to a program.

    Quote removal, backslash de-escaping (``shlex`` POSIX mode), ANSI-C / locale
    quoting (``$'…'``, ``$"…"`` -- see :func:`_decode_shell_quoted_literals`),
    empty-string concatenation (``g""it`` -> ``git``) and whitespace-run collapsing
    -- and NOTHING else: no tilde, no ``$HOME``, no path resolution.  This is the
    shared core of two callers that need the same token identity but must stop at
    different points:

    * :func:`normalize_shell_command` continues on to expand ``~``/``$HOME``,
      because it feeds path matchers that decide FILE identity.
    * :func:`_deny_segment_views` stops here, because expansion is
      platform-dependent and would land the operator's real home path in the
      audit trail -- see that function.

    On parse failure (unbalanced quotes) falls back to whitespace splitting with
    quote/backslash stripping, so a hostile unterminated quote yields a degraded
    view rather than no view.
    """
    if not cmd or not cmd.strip():
        return []
    cmd = _decode_shell_quoted_literals(cmd)
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # Unbalanced quotes or other parse errors — fall back to basic split.
        tokens = [t.strip("\"'\\") for t in cmd.split()]
    # Strip empty-string concatenation artifacts: ca""t -> cat, g''it -> git
    return [_EMPTY_QUOTE_RE.sub("", token) for token in tokens]


def normalize_shell_command(cmd: str) -> list[str]:
    """Normalize a shell command string into a resolved token list.

    Handles:
    - Shell quoting via shlex.split(posix=True)
    - Empty-string concatenation (g""it -> git, ca''t -> cat)
    - Tilde expansion (~/... -> /home/user/...)
    - $HOME / ${HOME} expansion to actual home directory
    - Backslash stripping (handled by shlex POSIX mode)

    Returns a list of resolved tokens.  On parse failure (unmatched quotes)
    falls back to basic whitespace splitting with quote/backslash stripping.

    Tokenization — everything up to and including the empty-quote collapse — is
    :func:`_shell_tokens`; the EXPANSION below is what makes this the path
    normalizer rather than the plain argv view the deny tiers use.
    """
    # NOTE: $HOME expansion happens AFTER tokenization (in the per-token loop
    # below), NOT here.  The previous pre-shlex expansion inserted the raw home
    # path (e.g. ``C:\Users\name`` on Windows) into the command string before
    # shlex.split(posix=True), which then consumed the backslashes as escape
    # characters — mangling the path so is_sensitive_path() could not match it.
    # Moving expansion to per-token mirrors how tilde (``~``) is already
    # handled: shlex strips quotes and produces a literal ``$HOME/...`` token,
    # which the loop then expands safely without backslash reinterpretation.

    home = os.path.expanduser("~")
    resolved: list[str] = []
    for token in _shell_tokens(cmd):
        # Expand $HOME/${HOME} per-token (after shlex, so Windows backslashes
        # in the expanded path are never reinterpreted as escape characters).
        # Uses a callable replacement to avoid re.error on Windows where the
        # home path contains ``\U`` which re.sub parses as a template escape.
        token = _HOME_VAR_RE.sub(lambda _m: home, token)

        # Expand tilde (shlex doesn't do tilde expansion)
        if token.startswith("~"):
            token = os.path.expanduser(token)

        resolved.append(token)

    return resolved


# ── IP Canonicalization (IMDS bypass prevention) ──
# Attackers bypass IMDS checks by encoding 169.254.169.254 in alternate forms:
#   - Decimal:   2852039166 (single 32-bit integer)
#   - Hex:       0xa9fea9fe or 0xa9.0xfe.0xa9.0xfe
#   - Octal:     0251.0376.0251.0376
#   - IPv6-mapped: ::ffff:169.254.169.254 or ::ffff:a9fe:a9fe
#   - Mixed:     169.254.0xa9.0376
# canonicalize_ip converts ALL these to dotted-quad for uniform matching.


def canonicalize_ip(s: str) -> str:
    """Convert an IP address in any encoding to dotted-quad (a.b.c.d).

    Handles:
    - Standard dotted-quad (passthrough)
    - Single decimal integer (e.g. 2852039166)
    - Hex integer (e.g. 0xa9fea9fe)
    - Octal/hex per-octet (e.g. 0251.0376.0251.0376 or 0xa9.0xfe.0xa9.0xfe)
    - IPv6-mapped IPv4 (e.g. ::ffff:169.254.169.254 or ::ffff:a9fe:a9fe)

    Returns the dotted-quad string on success, or the original string unchanged
    if it cannot be parsed as an IP address.
    """
    s = s.strip()
    if not s:
        return s

    # Try IPv6-mapped IPv4: ::ffff:... forms
    if s.startswith("::ffff:") or s.startswith("::FFFF:"):
        try:
            addr = ipaddress.ip_address(s)
            if hasattr(addr, "ipv4_mapped") and addr.ipv4_mapped:
                return str(addr.ipv4_mapped)
            if isinstance(addr, ipaddress.IPv6Address):
                mapped = addr.ipv4_mapped
                if mapped:
                    return str(mapped)
        except (ValueError, AttributeError):
            pass

    # Try standard dotted-quad with possible hex/octal octets
    parts = s.split(".")
    if 1 <= len(parts) <= 4:
        octets: list[int] = []
        valid = True
        for part in parts:
            try:
                # Handle C-style octal (0NNN without 'o' prefix) which Python 3
                # int(x, 0) doesn't recognize. Must check before int(x, 0).
                if len(part) > 1 and part[0] == "0" and part[1:].isdigit():
                    # Could be octal (0251) or just "00" etc.
                    if all(c in "01234567" for c in part[1:]):
                        val = int(part, 8)
                    else:
                        # Has 8 or 9 -- not valid octal, treat as decimal
                        val = int(part)
                else:
                    # int() with base=0 handles: decimal, 0x hex
                    val = int(part, 0)
                octets.append(val)
            except (ValueError, OverflowError):
                valid = False
                break

        if valid:
            if len(octets) == 1:
                # Single integer: 2852039166 -> 4 octets
                val = octets[0]
                if 0 <= val <= 0xFFFFFFFF:
                    return str(ipaddress.IPv4Address(val))
            elif len(octets) == 4:
                # Four octets (each 0-255)
                if all(0 <= o <= 255 for o in octets):
                    return f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}"
            elif len(octets) in (2, 3):
                # inet_aton "short" forms the OS resolver / curl accept but which
                # neither ipaddress nor the 1-/4-octet branches above canonicalize:
                #   a.b     -> a.(b as 24-bit)     e.g. 169.16689662  -> 169.254.169.254
                #   a.b.c   -> a.b.(c as 16-bit)   e.g. 169.254.43518 -> 169.254.169.254
                # Resolve them exactly as the OS does via inet_aton (which also
                # rejects out-of-range forms like 169.254.11207422), so an IMDS
                # SSRF cannot slip through in a 2-/3-part encoding. The last octet
                # carries the remaining low-order bytes, so a decimal/hex value up
                # to 0xFFFFFF (3-part) / 0xFFFFFFFF (2-part) is legal — validate the
                # leading octets are single bytes, then defer to inet_aton.
                if all(0 <= o <= 255 for o in octets[:-1]):
                    try:
                        return socket.inet_ntoa(socket.inet_aton(s))
                    except OSError:
                        pass

    # Try parsing as a plain integer (no dots) -- decimal or hex
    try:
        val = int(s, 0)
        if 0 <= val <= 0xFFFFFFFF:
            return str(ipaddress.IPv4Address(val))
    except (ValueError, OverflowError):
        pass

    # Try full ipaddress parsing as fallback
    try:
        addr = ipaddress.ip_address(s)
        if isinstance(addr, ipaddress.IPv4Address):
            return str(addr)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
    except ValueError:
        pass

    return s


# ── IMDS Access Detection ──
# The AWS Instance Metadata Service at 169.254.169.254 (link-local) exposes
# IAM role credentials via /latest/meta-data/iam/security-credentials/.
# Any HTTP client (not just curl/wget) hitting this IP must be blocked.

# Regex to extract potential IP addresses from a command string.
# Captures dotted-quad, hex/octal per-octet, bare integers, IPv6-mapped forms.
# One component of a dotted literal, in EVERY base the C resolver accepts: hex
# (``0x..``), C-style octal (a leading ``0``) or decimal. A digit run covers
# octal and decimal alike, so leading zeros are admitted in EVERY position.
# Spelling the bases per-position (the previous form) meant a MIXED encoding
# such as ``169.254.0251.0376`` matched no branch whole, so the token reached
# ``canonicalize_ip`` TRUNCATED and folded to a harmless address while the OS
# resolver still routed the full token to IMDS.
#
# UNBOUNDED on purpose. A length cap here is not a safety measure, it is the
# very defect being fixed: any cap truncates a padded spelling of the same
# address into a DIFFERENT, harmless one, so the gate fails open on
# ``0x0a9fea9fe`` and ``169.254.0x00000000a9.0376`` (glibc ``inet_aton``
# accepts both and routes them to IMDS). These are plain character classes
# with no nested quantifier, so an unbounded run is linear -- bounding buys no
# ReDoS protection and costs the match. The canonicalizer stays the strict
# half (it returns the input unchanged for anything that is not a real
# address), so admitting more candidates can only ever ADD a denial.
_IP_COMPONENT = r"(?:0[xX][0-9a-fA-F]+|\d+)"
_IP_CANDIDATE_RE = re.compile(
    r"(?:"
    r"::ffff:[0-9a-fA-Fx.:]+|"  # IPv6-mapped
    r"[0-9a-fA-F]{1,4}:[0-9a-fA-F:]{2,}|"  # native IPv6 literal (colon run, e.g. fd00:ec2::254)
    # 2-, 3- and 4-part dotted forms, any base per component. The trailing
    # component of a 2-/3-part inet_aton "short" form packs the remaining
    # low-order bytes, so it must be captured WHOLE (not just the tail) for
    # canonicalize_ip to resolve it; the greedy repeat takes every component
    # present, so the full token always wins over a shorter prefix.
    rf"{_IP_COMPONENT}(?:\.{_IP_COMPONENT}){{1,3}}|"
    r"0[xX][0-9a-fA-F]+|"  # bare hex integer, unbounded (see _IP_COMPONENT)
    # Bare single-integer form. NOT capped: a zero-padded/octal spelling of the
    # same address is longer (``025177524776`` is IMDS), and a cap truncates it
    # into a different, harmless address.
    r"\d{7,}"
    r")"
)

_IMDS_IP = "169.254.169.254"
# Native IPv6 IMDS endpoint (dual-stack EC2). The IPv4 gate above misses this
# because canonicalize_ip returns native IPv6 unchanged; mirrors embeddings.py's
# SSRF gate which also blocks it (CWE-918 dual-stack parity).
_IMDS_IPV6 = "fd00:ec2::254"


def _check_imds_access(
    command: str, *, enabled_ids: "frozenset[str] | None" = None
) -> str | None:
    """Detect attempts to access the IMDS endpoint via any encoding.

    Returns denial reason if IMDS access detected, None otherwise.

    Enforces ``credential-exfil-imds-any``, so *enabled_ids* lets the caller
    honour an operator opt-out of that rule. The two curl/wget IMDS rows are
    deliberately NOT consulted: they are verb-anchored and match only the literal
    dotted quad, so gating on them would silently narrow this check from "any verb,
    any encoding" to "curl or wget, literal IP". ``None`` means all enabled.
    """
    if enabled_ids is not None and "credential-exfil-imds-any" not in enabled_ids:
        return None
    # Quick reject: no IP-like candidate in command
    candidates = _IP_CANDIDATE_RE.findall(command)
    if not candidates:
        return None

    try:
        imds_v6: ipaddress.IPv6Address | None = ipaddress.ip_address(_IMDS_IPV6)  # type: ignore[assignment]
    except ValueError:  # pragma: no cover - constant is a valid literal
        imds_v6 = None
    for candidate in candidates:
        canonical = canonicalize_ip(candidate)
        if canonical == _IMDS_IP:
            # Found IMDS IP -- block regardless of tool since even echo
            # piped into nc could exfil credentials from the metadata service
            return (
                f"Blocked: command accesses IMDS endpoint "
                f"(169.254.169.254 via encoding '{candidate}')"
            )
        # Native IPv6 IMDS endpoint (fd00:ec2::254) — reachable over IPv6 on
        # dual-stack hosts; the IPv4 canonicalization above never matches it.
        # ipaddress equality normalizes compressed/expanded forms.
        if imds_v6 is not None:
            try:
                if ipaddress.ip_address(candidate.strip("[]")) == imds_v6:
                    return (
                        f"Blocked: command accesses IMDS endpoint "
                        f"(fd00:ec2::254 via '{candidate}')"
                    )
            except ValueError:
                pass
    return None


# ── Environment Credential Exfiltration Detection ──
# Attackers can read AWS credentials from environment variables without
# touching the filesystem, bypassing is_sensitive_path/bash checks.
# Block: declare -p AWS_SECRET*, env | grep AWS_, printenv AWS_,
#         awk 'ENVIRON["AWS_*"]', export -p | grep AWS_

_ENV_CRED_PATTERNS: list[re.Pattern[str]] = [
    # declare -p AWS_SECRET_ACCESS_KEY / declare -p AWS_SESSION_TOKEN
    re.compile(
        r"declare\s+(?:-[a-zA-Z]+\s+)*-?p\s+AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # echo $AWS_SECRET* / echo ${AWS_SECRET*}
    re.compile(
        r"(?:echo|printf|cat)\s+.*\$\{?AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # awk ENVIRON["AWS_SECRET*"] / awk ENVIRON["AWS_SESSION*"]
    re.compile(
        r"awk\s+.*ENVIRON\s*\[\s*[\"']AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
    # python/ruby/node reading os.environ for AWS secrets
    re.compile(
        r"(?:python|ruby|node|perl)\S*\s+.*(?:os\.environ|ENV|process\.env)"
        r".*AWS_(?:SECRET|SESSION|SECURITY)",
        re.IGNORECASE,
    ),
]

# Two intents this tier and the deny catalog express identically: an environment
# dump piped through grep/awk/sed for AWS variables, and ``printenv`` naming a
# secret-bearing variable directly. They are held as CATALOG RULE IDS and resolved
# from ``BUILTIN_DENIED_RULES`` -- never from the user's effective set -- so this
# tier runs exactly the regex the catalog publishes and still refuses when the
# catalog rule is opted out. Naming the rule rather than keeping a second reference
# to its pattern is what makes "one regex per intent" structural: there is no
# parallel constant here that could be edited alone, and the two had already
# drifted in the dangerous direction once (the keystone covered three full variable
# names while the catalog covered every secret-bearing prefix, so the tier that
# cannot be switched off was the weaker of the two).
_ENV_CRED_SHARED_RULE_IDS: tuple[str, ...] = (
    "credential-exfil-env-grep-aws",
    "credential-exfil-printenv-aws",
)

# Resolved eagerly and without a default, so a renamed rule id fails loudly at
# import instead of silently shrinking the tuple and retiring the always-on block.
_ENV_CRED_SHARED_RULES: tuple[DeniedCommandRule, ...] = tuple(
    next(rule for rule in BUILTIN_DENIED_RULES if rule.id == rule_id)
    for rule_id in _ENV_CRED_SHARED_RULE_IDS
)

_ENV_CRED_DENIAL_REASON = "Blocked: command reads AWS credentials from environment variables"


def _check_env_credential_access(command: str) -> str | None:
    """Detect attempts to read AWS credentials from environment variables.

    Returns denial reason if env credential access detected, None otherwise.

    The shared rules run through the same ``_deny_matcher`` the catalog tier uses,
    not a raw ``re.search``. Sharing the regex TEXT alone is not enough: this tier
    applies no length cap, and an ordered-existence pattern
    (``dump .* | .* filter .* selector``) under Python's backtracking engine is
    superlinear in the number of candidate pipes and filter words -- seconds on a
    few thousand characters, against milliseconds on the linear fragment matcher --
    so a raw search here would hand a long crafted command a stall of the
    synchronous PreToolUse gate that the catalog tier is already immune to.
    """
    for rule in _ENV_CRED_SHARED_RULES:
        if _deny_matcher(rule.pattern).match(command):
            return _ENV_CRED_DENIAL_REASON
    for pattern in _ENV_CRED_PATTERNS:
        if pattern.search(command):
            return _ENV_CRED_DENIAL_REASON
    return None


# ── Resource Limits (preexec_fn) ──
# Applied to agent-influenced subprocess spawns to bound resource-exhaustion
# attacks (fork bombs, FD exhaustion, runaway memory/CPU) so a compromised or
# buggy tool/MCP server cannot starve the host out from under the gateway.
# Uses POSIX resource limits (setrlimit); see docs/architecture/resource-protection.md.

# Default ceilings. Only RLIMIT_NOFILE is default-on: it is per-PROCESS,
# generous enough that no legitimate tool trips it, yet finite so a descriptor
# leak (which climbs unbounded) is arrested. The other three default to 0
# (disabled) ON PURPOSE — each is unsafe as a blanket default (see the caveats
# below) — but all four stay operator-configurable per deployment.
#
# Why not a default-on fork-bomb / memory cap? RLIMIT is the wrong tool for
# those defaults: RLIMIT_NPROC is per-UID (not per-subtree) and RLIMIT_AS caps
# virtual (not resident) memory. cgroup v2 ``pids.max`` / ``memory.max`` are the
# correct per-cgroup fork-bomb and RSS ceilings and are tracked as future work
# (see docs/architecture/resource-protection.md); the ticket itself lists cgroup v2 as the
# alternative. This helper delivers the safe RLIMIT subset now and leaves the
# hazardous knobs opt-in.
_RLIMIT_DEFAULTS = {
    # RLIMIT_NOFILE: max open file descriptors (per-process). Caps FD leaks.
    "max_open_files": 1024,
    # RLIMIT_NPROC: max processes for the child's real UID. 0 = disabled
    # (default). CAVEAT: this is enforced per real-UID against the count of ALL
    # the user's existing processes AND threads — NOT the spawn's own subtree.
    # A busy login/desktop UID can already hold thousands of threads (a fork
    # bomb is bounded only relative to that shared total), so any fixed cap that
    # is tight enough to matter is below a real host's baseline and would make
    # EVERY spawn fail to fork (EAGAIN). Safe to enable ONLY when the gateway
    # runs as its own dedicated UID; operators opt in via config there.
    # NOTE: the fork-bomb defense that IS default-on is the cgroup v2 scope
    # (sandbox.cgroup_scope_argv → pids.max), which is per-cgroup not per-UID.
    # This same ``max_processes`` key sets that cgroup pids.max ceiling (default
    # 8192 there); the RLIMIT_NPROC path below stays opt-in for the reasons above.
    "max_processes": 0,
    # RLIMIT_CPU: CPU-seconds. 0 = disabled (default). CAVEAT: this counts
    # against the WHOLE lifetime of a long-lived process — the root agent runs
    # up to a 30-min wall-clock turn and a busy tool-heavy session can
    # legitimately burn hundreds of CPU-seconds, so a non-zero global cap
    # SIGXCPU-kills healthy sessions. Set per-deployment only if the spawn
    # population is exclusively short-lived tools.
    "max_cpu_seconds": 0,
    # RLIMIT_AS: virtual address space (bytes-worth, expressed in MB). 0 =
    # disabled (default). CAVEAT: RLIMIT_AS caps VIRTUAL memory, not resident
    # memory, and Node/V8 (kiro-cli, claude-agent-acp, every npm MCP server)
    # reserves huge virtual mappings far exceeding real use — measured ~2GB VSZ
    # for 4 idle worker threads, ~3.4GB for 8 — so even a "generous" 4GB cap
    # SIGKILLs normal MCP-heavy sessions with spurious ENOMEM. Do NOT enable
    # globally for Node-backed spawns. The default-on memory ceiling is instead
    # the cgroup v2 scope (sandbox.cgroup_scope_argv → memory.max, an RSS cap,
    # host-proportional by default — 65% of physical RAM, so ~10.6 GB on a
    # 16 GB box / ~21.3 GB on 32 GB), which this same ``max_memory_mb`` key
    # overrides; the RLIMIT_AS path here stays opt-in for non-Node fleets.
    "max_memory_mb": 0,
}


def _bias_child_oom_score() -> None:
    """Bias the kernel OOM killer toward the calling process (``oom_score_adj``
    = 1000, inherited by descendants) so a memory-ballooning tool subprocess is
    killed BEFORE the cgroup ``memory.max`` ceiling takes out the whole agent
    scope. Linux-only, unprivileged, best-effort — never raises. Kept
    async-signal-safe (single open/write/close, no allocation-heavy work) so it
    is callable from a ``preexec_fn``. Pattern from OpenClaw's linux-oom-score
    child shim.
    """
    if sys.platform != "linux":
        return
    try:
        fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY)
        try:
            os.write(fd, b"1000")
        finally:
            os.close(fd)
    except OSError:
        pass


def resource_limit_spec(config: dict | None = None) -> list[tuple[str, int]]:
    """Resolve the configured rlimits as ``(RLIMIT_* name, value)`` pairs.

    Split out of :func:`apply_resource_limits` so one policy reader serves both
    ways of applying the limits:

    * **post-fork**, as the ``preexec_fn`` :func:`apply_resource_limits` builds;
    * **post-exec**, by the process-group supervisor
      (``_process_group_supervisor.py``), which receives these pairs on its argv
      because it cannot import this module -- it runs under ``python -I -c`` from
      an immutable gateway-captured source string, and that is deliberate: a
      mutable package path would let a same-UID agent swap the code out.

    Names, not ``resource`` constants: the consumer resolves them with
    ``getattr`` and skips any its platform lacks. A value of ``0`` means "leave
    inherited" and is dropped here -- the OPPOSITE of what ``0`` means on the
    cgroup path, which reads two of these same keys and treats ``0`` as "use the
    module default" because systemd rejects a zero property. Both domains are
    stated on ``ResourceLimitsConfig``, which is where the coercion lives.
    """
    limits = dict(_RLIMIT_DEFAULTS)
    if config:
        # The one validated parse for this block. Two things this replaces a
        # local ``val >= 0`` test to get: an Infinity from json.loads used to
        # pass that test and then raise OverflowError inside ``int()`` -- with no
        # try/except on this path, so it propagated out of resource_limit_preexec
        # and failed the spawn; and a fraction in (0, 1) used to floor to 0,
        # which is this path's "leave inherited" sentinel, silently dropping a
        # limit the operator had asked for. from_raw refuses both and says so.
        # circular import: config.loader reaches back into this module (it
        # imports security.is_sensitive_path function-locally for the same
        # reason), so importing the loader at security's module scope would
        # close the cycle. Kept function-level, matching sandbox and
        # resource_status, which read the same block under the same constraint.
        from kiro_crew.config.loader import ResourceLimitsConfig

        parsed = ResourceLimitsConfig.from_raw(config.get("resource_limits"))
        for key in _RLIMIT_DEFAULTS:
            # None means "not usable" -- keep the documented default rather than
            # inventing a number. An explicit 0 survives, because disabling a
            # limit is a real request here.
            val = getattr(parsed, key, None)
            if val is not None:
                limits[key] = val

    # (rlimit name, requested soft/hard value in the rlimit's native unit).
    max_memory_bytes = limits["max_memory_mb"] * 1024 * 1024
    specs = [
        ("RLIMIT_NPROC", limits["max_processes"]),
        ("RLIMIT_NOFILE", limits["max_open_files"]),
        ("RLIMIT_CPU", limits["max_cpu_seconds"]),
        ("RLIMIT_AS", max_memory_bytes),
    ]
    return [(name, value) for name, value in specs if value > 0]


def apply_resource_limits(config: dict | None = None) -> "Callable[[], None]":
    """Return a preexec_fn that applies POSIX resource limits to a child process.

    Reads limits from the ``resource_limits`` config section:
      - ``max_processes``: RLIMIT_NPROC (process count for the child's UID).
      - ``max_open_files``: RLIMIT_NOFILE (open file descriptors).
      - ``max_cpu_seconds``: RLIMIT_CPU in seconds (``0`` disables — default).
      - ``max_memory_mb``: RLIMIT_AS (virtual address space) in MB (``0``
        disables — default; see the RLIMIT_AS caveat in ``_RLIMIT_DEFAULTS``).

    Each key accepts a positive integer to set that limit, or ``0`` to leave the
    limit unchanged (inherited). Missing keys fall back to ``_RLIMIT_DEFAULTS``.
    A requested limit is always clamped DOWN to the inherited hard limit — we
    never try to *raise* a ceiling (an unprivileged child cannot, and the
    attempt would raise), so this can only tighten, never loosen, the child's
    budget.

    The returned callable is intended for use as ``preexec_fn`` in
    ``subprocess.Popen`` / ``asyncio.create_subprocess_exec``. It runs in the
    child process after fork but before exec — setrlimit calls here only affect
    the child. It is a no-op on non-POSIX platforms (``resource`` unavailable)
    and degrades gracefully per-limit on platforms lacking a specific rlimit
    (e.g. macOS has no RLIMIT_NPROC / a flaky RLIMIT_AS).

    NOTE: ``preexec_fn`` runs post-fork in a subprocess that may be
    multi-threaded; it MUST stay async-signal-safe — only ``getrlimit`` /
    ``setrlimit`` here, no allocation-heavy or lock-taking work.

    Args:
        config: Full KiroCrew config dict (or any subset containing
            ``resource_limits``). Pass None for defaults.

    Returns:
        A no-arg callable suitable for ``preexec_fn``.
    """
    if _resource is None:
        # Non-POSIX (Windows): nothing to enforce.
        return lambda: None
    # Bind a non-None local so the nested preexec closure keeps the narrowed
    # type (closures don't inherit the guard's narrowing of the module global).
    res = _resource

    # Resolve the rlimit constants once in the parent (cheap, keeps the
    # post-fork callable minimal). Skip any this platform lacks.
    resolved = [
        (getattr(res, name), value)
        for name, value in resource_limit_spec(config)
        if hasattr(res, name)
    ]

    def _set_limits() -> None:
        """Apply resource limits in the child process (preexec_fn).

        Runs post-fork/pre-exec. Clamps each requested limit down to the
        inherited hard limit so we only ever tighten, and swallows per-limit
        failures so an unsupported rlimit never blocks the spawn.
        """
        for res_id, requested in resolved:
            try:
                _soft, hard = res.getrlimit(res_id)
                # Never exceed the inherited hard cap (RLIM_INFINITY == -1 means
                # "no ceiling", so any finite request is fine against it).
                if hard != res.RLIM_INFINITY:
                    requested = min(requested, hard)
                # Set BOTH soft and hard to the effective value: lowering the
                # hard cap (always permitted unprivileged) stops the child from
                # raising its own soft limit back up to escape the ceiling.
                res.setrlimit(res_id, (requested, requested))
            except (ValueError, OSError):
                # Platform doesn't support this rlimit, or the kernel rejected
                # the value — leave it inherited rather than fail the spawn.
                continue
        # Bias the OOM killer toward this child (see _bias_child_oom_score).
        _bias_child_oom_score()

    return _set_limits
