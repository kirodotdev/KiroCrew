"""The built-in denied-command catalog, its linear matcher and the evaluator seam.

The catalog is the configurable tier: every row is DEFAULT-ON, user-disableable
from the security settings surface, and force-pinnable by a governance policy.
That makes it the opposite of the keystone in ``paths`` -- a row here can be
turned off, so nothing that must always hold may live here alone.

Three things in this module are load bearing beyond their own tier.

The floor id sets and pattern tables are DERIVED from catalog categories rather
than hand-maintained, so a new row in a floor-enforced category is covered
automatically and the pattern set and id set cannot drift apart. They are read
by the argv-structural floor, which owns no catalog knowledge of its own.

The matcher exists because the rule patterns were authored for a linear-time
(RE2-style) engine and are not safe to hand to a backtracking engine verbatim.
It is an EVALUATION-layer rewrite only: the catalog rows, and the golden fixture
the parity test pins to, stay byte-for-byte what they were, and a refusal still
reports the original pattern.

The refusal producer is the single place refusal text is formed. Its first line
is frozen because two consumers parse it, one of them with a per-line
end-anchored regex; an operator note therefore goes on a second line, which both
consumers ignore.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .diagnostics import RefusalDiagnostic, annotate_refusal, refusal_diagnostic
from .shell_normalizer import _SHELL_ACTIVE_CHARS

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


# ── Built-in Denied-Command Rules ──
# The canonical catalog of built-in denied commands.  Each rule is a Python
# REGEX (matched case-insensitively via ``re.search``) with a stable ``id``
# (the opt-out key + SEL audit key), a ``category`` for UI grouping, and a
# human ``description``.  Rules are DEFAULT-ON but user-disableable from
# Settings → Security; a governance ``commands``-scope policy can force-pin a
# rule as un-opt-out-able (see ``platform/governance.py``).  Enforcement is at
# Kiro Crew's own ``hooks.py`` PreToolUse gate — these are NOT injected into the
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

# ``AWS_CONFIG_FILE`` / ``AWS_SHARED_CREDENTIALS_FILE`` hold a PATH, not a
# secret, so neither is scrubbed from an agent child's environment -- the AWS CLI
# and every SDK read them directly.
#
# Two rules are deliberately ABSENT here, and the reasoning is worth keeping
# because it generalizes to any future "deny the variable name" proposal: a
# shell dereference (``$NAME``, ``${NAME}``, ``%NAME%``, ``!NAME!``,
# ``$env:NAME``) and an inline-interpreter environment lookup
# (``os.environ['NAME']``).
#
# Such a rule is only reachable at all when something EXPORTS one of these
# names into an agent child pinned at the real home, which would make the name
# an alias for a path the sensitive-path keystone fences; because the matchers
# here work on command TEXT with no variable expansion, that alias would be
# reachable while the literal path is refused.
#
# Closing that retrieval one spelling at a time -- the shell sigils, then the
# interpreter lookup, then ``cp "$(printenv NAME)" x`` -- is the shape of a
# losing race: command substitution, ``eval``, indirect expansion
# (``v=NAME; cat "${!v}"``), and a two-line helper script are all still
# available, and no text matcher can see through them. The alias is denied at
# its source instead: the remap in ``acp.client._apply_pod_home_remap`` exports
# neither name. With nothing manufacturing the alias, such rules would guard
# only an operator who set the variable in their own environment -- their own
# named file, not an alias this codebase created -- at the cost of implying a
# completeness the pattern class cannot deliver. Partial coverage of an
# unbounded bypass space is worse than none, because it reads as a fence.
#
# That judgement holds across this whole catalog, so this note reads as
# precedent rather than as an exception: the twenty-seven
# sensitive-file-read rows are absent for the identical reason, so no path --
# named literally or through a variable -- is fenced at the text layer. The
# floor is
# ``redact_credentials`` on the output, ``is_sensitive_path`` on every resolved
# path a file tool opens (which anchors ``KIROCREW_OS_HOME`` as an alternate home
# root, so a pod's relocated credential tree is covered there), and the OS sandbox
# on the subprocess. ``_AWS_SECRET_VAR_NAMES`` above still denies retrieval of the
# variables that hold a SECRET rather than a path; that set is closed and
# enumerable, which is why a name-based rule is defensible there and not here.


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
            "|"
            "(?:os\\.system|os\\.popen|os\\.exec\\w*|(?:asyncio\\.)?create_subprocess_\\w*"
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
            "``os.system(...)`` or ``execSync(...)``. Sink-qualified so prose, a commit message "
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
    # The ``restart`` / ``update`` / ``cloud <lifecycle>`` / ``gateway restart``
    # self-management commands have NO catalog row.  Their regex rows opened with
    # an unbounded any-run before the product name, so the name in a worktree
    # path plus the verb word anywhere later was a match (``ls
    # ~/kirocrew-wt/restart.log``); a row that fires on the product's name
    # appearing anywhere protects nothing the structural floor does not, and was
    # deleted rather than narrowed.  Enforcement is the argv floor alone
    # (``argv_floor._is_self_restart`` and siblings, ungated -- see
    # ``_SELF_PROTECTION_UNGATED_FLOOR_IDS``), which requires the product to be
    # the argv's own PROGRAM and the action its leading subcommand.
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
]

_RULES_BY_ID: dict[str, DeniedCommandRule] = {r.id: r for r in BUILTIN_DENIED_RULES}

# Reverse map (pattern → rule id) for SEL audit enrichment on a regex-tier match.
_RULE_ID_BY_PATTERN: dict[str, str] = {r.pattern: r.id for r in BUILTIN_DENIED_RULES}


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
#   * dev-mode confirm flag: regex+floor UNION. The regex catches the raw-text
#     literal; the floor additionally catches quote-splitting inside the token.
# All members stay in the regex tier (only git-publish is removed from ``re``);
# the floor is a union with it, never a replacement, and each predicate runs
# only while its row is in the effective set, so an operator-disabled rule stays
# disabled.
_SELF_PROTECTION_FLOOR_RULE_IDS: frozenset[str] = frozenset(
    {
        "credential-exfil-kirocrew-token",
        "self-protection-kill",
        "self-protection-dev-mode-out-of-root-confirm",
    }
)
_SELF_PROTECTION_FLOOR_BY_ID: dict[str, str] = {
    r.id: r.pattern for r in BUILTIN_DENIED_RULES if r.id in _SELF_PROTECTION_FLOOR_RULE_IDS
}
_SELF_PROTECTION_FLOOR_PATTERNS: frozenset[str] = frozenset(_SELF_PROTECTION_FLOOR_BY_ID.values())

# The four self-management SUBCOMMAND floors -- restart, update, gateway restart,
# cloud <destructive> -- have NO catalog row and NO opt-out.  Their regex rows
# (``.*kiro.?crew ... restart.*`` and siblings) opened with an unbounded any-run,
# so the product name in a worktree path plus the verb word anywhere later
# matched: ``ls ~/kirocrew-wt/restart.log`` was a denial.  A row that fires on
# the product's name appearing anywhere adds nothing to the structural predicate,
# which requires the product to be the argv's own PROGRAM (console script or
# ``python -m kiro_crew``) and the action its LEADING subcommand, so the rows were
# deleted and the floor now carries the whole of enforcement -- ungated, like the
# git-publish anti-obfuscation branches: with no row there is no toggle, and a
# floor that consulted an opt-out state no row can express would be a silent
# allow the moment the row went away (``is_denied`` used to ``continue`` past a
# predicate whose id resolved to no pattern).  These ids are what a refusal and
# its SEL event report; they are deliberately NOT catalog ids, and the two sets
# are pinned disjoint so a re-added row cannot silently gate a floor again.
_SELF_PROTECTION_UNGATED_FLOOR_IDS: frozenset[str] = frozenset(
    {
        "self-protection-restart",
        "self-protection-update",
        "self-protection-gateway-restart",
        "self-protection-cloud",
    }
)

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
#
# The four ``_SELF_PROTECTION_UNGATED_FLOOR_IDS`` entries are the same shape, but
# for them the first line reports the ID itself (there is no catalog pattern to
# report), the way the gated git-publish floor does.  The opening phrase is the
# anchor ``deny_guidance`` classifies a self-protection refusal by, so every entry
# keeps it verbatim.
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
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommand restarts this gateway. This floor has no "
        "catalog row and no opt-out."
    ),
    "self-protection-update": (
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommand self-updates this gateway. This floor has "
        "no catalog row and no opt-out."
    ),
    "self-protection-gateway-restart": (
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommands restart the gateway server. This floor has "
        "no catalog row and no opt-out."
    ),
    "self-protection-cloud": (
        "Matched structurally on the command's argv: the product CLI is the argv's own "
        "program and its leading subcommand is a destructive cloud lifecycle operation. "
        "This floor has no catalog row and no opt-out."
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


# ── Governance pins ("commands"-scope ceiling and profile pins) ──
# Legacy spellings of rules whose patterns were later widened.  A governance
# policy persists the pattern STRING it pinned, and the pin resolvers treat a
# pattern as pinning a built-in rule only when it maps back to a rule id — so a
# ceiling or profile written against an older catalog must keep resolving to
# the rule id after an upgrade (upgrade monotonicity), or a stale pin falls out
# of the id map and a user opt-out drops a rule the administrator pinned.
# LOOKUP-ONLY: consulted by :func:`_rule_id_for_pattern` (the pin resolvers),
# never merged into ``_RULE_ID_BY_PATTERN`` — the legacy spellings must not
# count as built-ins for ``_DenyMatcher``'s fast-path election or SEL
# enrichment, and they never enter ``BUILTIN_DENY_PATTERNS`` or the golden
# manifest.
#
# Currently EMPTY, on purpose.  Entries here would alias the pre-widening
# spellings of the ``restart`` / ``update`` / ``cloud`` / ``gateway restart``
# rows, and those rows do not exist: their enforcement is the
# ungated argv floor (``_SELF_PROTECTION_UNGATED_FLOOR_IDS``), which no opt-out
# can reach, so there is nothing left for such a pin to force back on.  A
# persisted pin in either spelling resolves to ``None`` and is reported by
# ``_resolved_pin_ids`` as pinning nothing -- which is the truth, and preferable
# to resolving it onto an id the catalog cannot display or toggle.
_LEGACY_RULE_ID_BY_PATTERN: dict[str, str] = {}


def _rule_id_for_pattern(pattern: str) -> "str | None":
    """Resolve a governance-pinned pattern string to a built-in rule id.

    Current catalog spellings first, then the legacy (pre-widening) spellings,
    so a persisted policy keeps its pin across a pattern change.
    """
    return _RULE_ID_BY_PATTERN.get(pattern) or _LEGACY_RULE_ID_BY_PATTERN.get(pattern)


def _resolved_pin_ids(pins: "Iterable[str]", component: str) -> set[str]:
    """Rule ids for *pins*, naming every pin that resolves to none.

    A pin that maps to no rule id used to leave in a set comprehension's filter,
    which is the same defect a refusal naming no rule has, one layer up: an
    administrator writes a ceiling, the pin silently pins nothing, and the policy
    still reads as valid everywhere it is displayed. So an unresolvable pin is
    reported -- by SHAPE, through the same diagnostic record a refusal carries,
    because a persisted pin is a PATTERN an operator authored and a log line
    quoting it would put a policy body into the log for every failed lookup.
    ``rule`` therefore names the failure, not the pin, and the shape is what tells
    an administrator which of their entries it was.

    Reporting only -- the resolved set is exactly what the comprehension produced,
    so no pin becomes more or less enforced by being named. Widening resolution
    itself (a trimmed form, an id-shaped pin resolved directly) is the governance
    cluster's own change and is deliberately not done here.
    """
    resolved: set[str] = set()
    for pin in pins:
        rule_id = _rule_id_for_pattern(pin)
        if rule_id is None:
            logger.warning(
                "governance pin resolves to no built-in rule and pins nothing; %s",
                refusal_diagnostic("governance-pin-unresolved", component, pin).as_line(),
            )
            continue
        resolved.add(rule_id)
    return resolved


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
        return _resolved_pin_ids(pins, "commands-ceiling-pin")
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

        ids |= _resolved_pin_ids(all_profile_pinned_commands(), "profile-pin")
    except Exception:
        pass
    return ids


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
    # same cause. (``top-level-imports``, documented exception.)
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


# Exceptions keyed by the deny pattern they apply to. If an input matches
# a deny pattern AND one of that pattern's exceptions, the deny is skipped.
# This avoids a blanket allowlist that could bypass unrelated deny rules.
# Exceptions are NOT applied when the input contains command separators
# (;, &&, ||, |, newlines) to prevent chaining bypasses.
#
# Scoped carve-out for INERT MENTIONS of a destructive literal.
#
# A read-only search verb cannot execute its own operands, so a destructive
# string handed to it as a pattern is text, not an action:
#
#     grep -rn "rm -rf /" tests/     <- searching FOR the rule, not running it
#
# Denying those identically to the real command prevents nothing (the same work
# completes by moving the payload into a file, which is not scanned) while
# blocking anyone working ON these rules, and surfaces to the agent as
# ``User denied tool execution`` -- indistinguishable from a human cancelling.
#
# Two conditions make this safe, and BOTH are load-bearing -- getting either one
# wrong re-allows a real wipe:
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
#      :func:`_exception_eligible`. Blocklisting only the openers already
#      known is defeated by the next one -- a
#      bash 5.3 funsub, ``grep x ${ rm -rf /;}``, which the splitter cuts only
#      at the ``;`` so the destructive command stays glued to the search verb.
#      Enumerating opener SPELLINGS is the losing side of that game (the same
#      point applies to any spelling-based recognizer), so the guard is
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
# it compatible with quote-NORMALIZED matching (quoting must never exculpate a
# command that does run).
#
# Deliberately NOT included, pending a maintainer decision:
# ``echo``/``printf`` (inert to execute, but ``>`` is not a segment
# separator, so an exoneration there also covers writing the string to a file)
# and ``git commit -m`` (the arguable non-search verb).
_INERT_SEARCH_VERBS = ("grep", "egrep", "fgrep")
#: Verb-anchored ONLY -- see condition 1 above. A leading ``*`` here would be a
#: bypass, not a convenience.
_INERT_SEARCH_GLOBS: list[str] = [f"{verb} *" for verb in _INERT_SEARCH_VERBS]


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
    through.

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
            # synchronous gate. Such a pattern keeps the bounded engine it already had;
            # only patterns that are free to run unbounded get the full-input path.
            single_fragment = (
                frags is not None and len(frags) == 1 and not _polynomial_backtracking_prone(linear)
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


def _deny_reason(
    matched: str,
    reason_notes: "dict[str, str] | None",
    *,
    note_override: str = "",
    diagnostic: "RefusalDiagnostic | None" = None,
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

    *diagnostic* appends a THIRD line naming the rule id, the deciding component and
    the matched span's offsets and character-class shape (see
    :mod:`kiro_crew.security.diagnostics`).  It is opt-in per call site rather than
    always-on, and the split is not cosmetic: a pattern-tier denial's first line
    already IS the accurate cause, so a diagnostic there would add a line to every
    ordinary refusal for no information, while a STRUCTURAL denial reports a pattern
    the input cannot match and is the case an agent cannot diagnose at all.  So the
    tiers that decide on argv shape pass one and the pattern tiers do not, which is
    also why a plain catalog refusal stays exactly one line.

    Module-level rather than a closure because EVERY tier that can refuse must emit
    the identical micro-format: a second producer would be free to drift from the
    three consumers that parse it.
    """
    head = f"{DENY_REASON_PREFIX}{matched}"
    note = (note_override or (reason_notes or {}).get(matched, "")).strip()
    reason = f"{head}\n{note}" if note else head
    if diagnostic is None:
        return reason
    return annotate_refusal(reason, diagnostic)


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
