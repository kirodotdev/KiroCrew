"""Built-in security controls — deny list, sensitive path protection, and audit scanning."""

from __future__ import annotations

import asyncio
import base64
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
import time
import uuid
from collections import Counter
from dataclasses import dataclass

try:
    import resource as _resource
except ImportError:
    _resource = None  # type: ignore[assignment]  # Windows/non-POSIX
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from kiro_crew.executors import maintenance_executor
from kiro_crew.sel import SecurityEvent, SecurityEventLog
from kiro_crew.vector_memory_constants import _contains_injection

# NB: kiro_crew.vector_memory is imported lazily inside scan_memory() rather than
# at module top level. vector_memory.py imports redact_credentials/
# redact_exfiltration_urls from this module at ITS top level, so a top-level
# import here would create a circular import — under which the ImportError guard
# would silently set the store to None and disable scan_memory(). The deferred
# import breaks the cycle and also keeps the numpy/faiss/snowballstemmer stack
# off the lightweight import path.

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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
        pattern=".*printenv.*AWS.*",
        category="credential-exfil",
        description=(
            "Blocks `printenv` dumping any AWS_* environment variable, which can leak AWS "
            "credentials held in the environment."
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
        pattern=".*env.*grep.*AWS.*",
        category="credential-exfil",
        description=(
            "Blocks `env | grep AWS`, which filters the environment for AWS_* variables and "
            "leaks any credentials stored there."
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
        # since a not-yet-migrated box still holds live secrets at the legacy
        # path.
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
        pattern=".*kiro.?crew restart.*",
        category="self-protection",
        description=(
            "Blocks 'kirocrew restart' so the agent cannot restart its own gateway process and "
            "disrupt the running session or evade in-flight controls."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-update",
        pattern=".*kiro.?crew update.*",
        category="self-protection",
        description=(
            "Blocks 'kirocrew update' so the agent cannot self-update (git pull + rebuild + "
            "execv restart) and swap out its own running code without operator oversight."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-cloud",
        pattern=".*kiro.?crew\\s+cloud\\s+(destroy|stop|start|launch|connect|tunnel|login).*",
        category="self-protection",
        description=(
            "Blocks 'kirocrew cloud' lifecycle subcommands "
            "(destroy/stop/start/launch/connect/tunnel/login) so the agent cannot tear down, "
            "provision, or re-authenticate its own cloud instance."
        ),
    ),
    DeniedCommandRule(
        id="self-protection-gateway-restart",
        pattern=".*kiro.?crew gateway restart.*",
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
_GIT_PUBLISH_RULE_PATTERNS: frozenset[str] = frozenset(
    r.pattern for r in BUILTIN_DENIED_RULES if r.category == _GIT_PUBLISH_RULE_CATEGORY
)

# The two self-protection rules whose enforcement lives in the argv-structural
# floor (``_is_credential_mint`` / ``_is_self_kill``) rather than in the regex
# tier.  Their ``pattern`` is retained as the catalog-visible, human-auditable
# statement of intent -- and it is a correct SUBSET of the floor -- but it is not
# fed to ``re`` because a raw-string match cannot resolve shell quoting or
# redirection, and a pattern loose enough to try would re-block ordinary paths.
_SELF_PROTECTION_FLOOR_RULE_IDS: frozenset[str] = frozenset(
    {"credential-exfil-kirocrew-token", "self-protection-kill"}
)
_SELF_PROTECTION_FLOOR_BY_ID: dict[str, str] = {
    r.id: r.pattern for r in BUILTIN_DENIED_RULES if r.id in _SELF_PROTECTION_FLOOR_RULE_IDS
}
_SELF_PROTECTION_FLOOR_PATTERNS: frozenset[str] = frozenset(_SELF_PROTECTION_FLOOR_BY_ID.values())

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
        return {_RULE_ID_BY_PATTERN[p] for p in pins if p in _RULE_ID_BY_PATTERN}
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
            rid = _RULE_ID_BY_PATTERN.get(p)
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
# Currently empty: the only former entry (``git stash push`` excepted from
# ``*git*push*``) is obsolete now that git-publish is detected by a
# verb-anchored regex that never matches ``git stash push`` in the first
# place. The two-pass exception machinery in ``is_denied`` is retained as a
# general mechanism for any future pattern that needs a scoped carve-out.
_DENY_EXCEPTIONS: dict[str, list[str]] = {}

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

    The known-safe linearized aws flag run is stripped before the structural
    check so the (harmless) built-in construct is never misflagged.

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
    scrubbed = pattern.replace(_DANGEROUS_AWS_FLAG_RUN, "").replace(_LINEARIZED_AWS_FLAG_RUN, "")
    if _redos_prone(scrubbed):
        return False
    return not _has_top_level_alternation(scrubbed)


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
            if not is_builtin or frags is None or _frags_can_underconsume(frags):
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
            # is USER-custom-regex-only (the 137 built-in security rules use the
            # full-input fragment matcher, no truncation), the cap far exceeds any
            # real command, and the only bypass is a single >cap-char shell
            # segment defeating the user's OWN custom rule. See security.md.
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
    r"(?:^|[;&|`\n]|\$\()\s*git\s+(?:-\S+\s+(?:[^-\s]\S*\s+)?)*push(?=\s|[)`;&|]|$)"
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
# ``VAR=value`` prefixes a command rather than being the command.
_ENV_ASSIGN_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*=")
# An EMPTY substitution expands to nothing, so ``p$()kill`` runs ``pkill`` -- the
# same glue-evasion as the empty-quote form (``ca""t`` -> ``cat``) that
# ``normalize_shell_command`` already undoes, but spelled with a substitution and
# placed MID-WORD, where a prefix-only strip never sees it.
_EMPTY_SUBST_RE = re.compile(r"\$\(\s*\)|`\s*`|\$\{\s*\}")
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
    """
    depth = 0
    for token in tokens[start:]:
        depth += token.count("(") - token.count(")")
        m = _SELF_NAME_RE.search(token)
        if m:
            return m.group(0)
        for verb in _KILL_BY_NAME_PROGRAMS:
            if verb in token:
                return verb
        if depth <= 0 and token is not tokens[start]:
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
_SHELL_COMMAND_FLAG_RE = re.compile(r"\A-[a-z]*c[a-z]*\Z")


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


def _data_consumer_exempt(index: int, token: str, programs: "list[str]", tokens: "list[str]") -> bool:
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
    if _pipes_into_evaluator(tokens):
        return False
    # ``$(printf <name>) <verb>`` puts the consumer INSIDE a substitution that occupies
    # program position, so its OUTPUT is what runs -- the words are not inert data.
    if tokens and tokens[0].lstrip("\"'").startswith("$(") or (
        tokens and tokens[0].lstrip("\"'").startswith("`")
    ):
        return False
    # A "data consumer" that can EXECUTE is not one for this command.  ``awk`` has
    # ``system()`` and pipe-to-command; GNU ``sed`` has the ``e`` flag.  The exemption is
    # withdrawn per-command when the script text carries such a construct, rather than
    # dropping ``awk`` from the list entirely -- that would also refuse ordinary
    # ``awk '{print $1}' <file>``, and the list is deliberately a denylist of consumers so
    # that a mistake here costs a false positive, never a bypass.
    if any(_SCRIPT_EXECUTES_RE.search(tok) for tok in tokens):
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
        if expect_program and token and not _ENV_ASSIGN_RE.match(token):
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


def _nested_shell_payloads(tokens: "list[str]") -> "list[str]":
    """Literal shell-script payloads carried as an argument inside *tokens*.

    Covers ``sh -c '<script>'`` / ``bash -c '<script>'`` (the payload is the
    first non-flag token after ``-c``) and ``eval '<script>'``.  Only LITERAL
    payloads are returned -- ``eval "$CMD"`` carries no visible script, and that
    case is covered by the regex tier running alongside this floor rather than by
    this function.
    """
    payloads: list[str] = []
    for i, token in enumerate(tokens):
        base = _program_basename(token)
        # A shell reached through a VARIABLE (``$SHELL -c '<payload>'``) runs the
        # payload exactly as a named shell does.  The recognizer already used for the
        # ``| $SHELL`` evaluator sink applies here too.
        if base in _NESTED_SHELL_PROGRAMS or _is_shell_variable_reference(token):
            for j in range(i + 1, len(tokens)):
                if _is_shell_command_flag(tokens[j]):
                    # ``bash -c -- '<script>'`` is legal: ``--`` ends option parsing
                    # and the script is the token AFTER it.  Skip any run of them.
                    k = j + 1
                    while k < len(tokens) and tokens[k] == "--":
                        k += 1
                    if k < len(tokens):
                        payloads.append(tokens[k])
                    break
                # A HERESTRING feeds the script on stdin instead of as an argument
                # (``bash <<< '<script>'``), so its text is a command just the same.
                # Both the spaced and glued spellings arrive here.
                if tokens[j] == "<<<":
                    if j + 1 < len(tokens):
                        payloads.append(tokens[j + 1])
                    break
                if tokens[j].startswith("<<<"):
                    payloads.append(tokens[j][3:])
                    break
        elif base in _ENV_SPLIT_PROGRAMS:
            # ``env -S '<script>'`` / ``env --split-string '<script>'`` splits the
            # payload into a command and runs it, so its text is a command line.
            for j in range(i + 1, len(tokens)):
                # ``is_denied`` lowercases its input, so compare case-insensitively:
                # the real flag is ``-S`` but it arrives here as ``-s``.
                flag = tokens[j].lower()
                if flag in {"-s", "--split-string"}:
                    if j + 1 < len(tokens):
                        payloads.append(tokens[j + 1])
                    break
                if flag.startswith("-s") and len(tokens[j]) > 2:
                    payloads.append(tokens[j][2:])
                    break
                if flag.startswith("--split-string="):
                    payloads.append(tokens[j].split("=", 1)[1])
                    break
        elif base in _NESTED_SHELL_VERBS or token in _NESTED_SHELL_VERBS:
            if i + 1 < len(tokens):
                payloads.append(tokens[i + 1])
    # ``bash<<<'<payload>'`` glues the program, the operator and the payload into ONE
    # token, so the program never appears as a token of its own for the walk above to
    # recognise.  Split on the operator and check the left half.
    for token in tokens:
        if "<<<" not in token:
            continue
        head, _, tail = token.partition("<<<")
        if tail and _program_basename(head) in _NESTED_SHELL_PROGRAMS:
            payloads.append(tail)
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


# ``printf`` numeric escapes: octal (``\\NNN``, ``\\0NNN``) and hex (``\\xHH``).
_NUMERIC_ESCAPE_RE = re.compile(r"\\(?:x([0-9a-f]{1,2})|0?([0-7]{1,3}))", re.IGNORECASE)


def _numeric_escape_char(match: "re.Match[str]") -> str:
    """One decoded character for an octal or hex ``printf`` escape."""
    hex_digits, octal_digits = match.group(1), match.group(2)
    try:
        code = int(hex_digits, 16) if hex_digits else int(octal_digits, 8)
    except (TypeError, ValueError):  # pragma: no cover - the pattern admits only digits
        return match.group(0)
    if code == 0 or code > 0x10FFFF:
        # A NUL cannot appear in an argv the shell builds, so leave it inert.
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
    """
    for esc, sub in _PRINTF_ESCAPES:
        text = text.replace(esc, sub)
    return _NUMERIC_ESCAPE_RE.sub(_numeric_escape_char, text)


def _self_token_frames(text_lower: str) -> "list[list[str]]":
    """The command's own argv plus the argv of every nested shell payload.

    ``bash -c "kirocrew token"`` tokenizes to ``['bash', '-c', 'kirocrew token']``
    -- the dangerous command is a single opaque token, so the direct scan cannot
    see it.  Re-tokenizing the payload and checking that argv too closes the
    class rather than one spelling of it.

    Descends to ANY depth.  A numeric depth cap is itself a bypass -- whatever the
    number, one more wrapper defeats it -- so the walk is bounded structurally: a
    payload lives inside one token of its parent and is therefore strictly shorter
    than the parent's source text, and a chain of strictly shorter strings is
    finite.
    """
    frames: list[list[str]] = []
    seen: set[str] = set()
    pending: list[tuple[str, int]] = [(text_lower, len(text_lower) + 1)]
    while pending:
        source, parent_len = pending.pop()
        tokens = _self_tokens(source)
        if not tokens:
            continue
        frames.append(tokens)
        # Every substitution body is itself a command line -- command substitution
        # (``$( )``, backticks) and PROCESS substitution (``<( )``, ``>( )``) alike, since
        # bash runs the inner command in all of them.  Walking them here means the
        # ordinary argv checks see ``cat <(kirocrew token)`` as the inner invocation.
        for payload in list(_nested_shell_payloads(tokens)) + _substitution_bodies(source):
            # Descend through EVERY literal payload, to any depth.  Termination is
            # structural, not a cap: a payload is carried inside one token of its
            # parent, so it is strictly shorter than the parent's source text.
            payload = _decode_printf_escapes(payload)
            if len(payload) >= parent_len or payload in seen:
                continue
            seen.add(payload)
            pending.append((payload, len(source)))
    return frames


def _substitution_depth_delta(token: str) -> int:
    """Net change in command-substitution nesting contributed by *token*.

    Used so a separator INSIDE ``$( … )`` is not mistaken for the end of the argv
    being scanned -- ``<name> $(true; echo <verb>)`` is one command, not two.
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
    """
    bodies: list[str] = []
    i = 0
    while i < len(text):
        # PROCESS substitutions run their body as a command just as a command
        # substitution does -- ``cat <(kirocrew token)`` executes the inner command and
        # feeds its output through a pipe.  Same paren-nesting walk.
        if text.startswith("<(", i) or text.startswith(">(", i):
            depth = 1
            j = i + 2
            while j < len(text) and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            bodies.append(text[i + 2 : j - 1 if depth == 0 else len(text)])
            i = j
            continue
        if text.startswith("$(", i):
            depth = 1
            j = i + 2
            while j < len(text) and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            bodies.append(text[i + 2 : j - 1 if depth == 0 else len(text)])
            i = j
        elif text[i] == "`":
            j = text.find("`", i + 1)
            bodies.append(text[i + 1 : j if j != -1 else len(text)])
            i = len(text) if j == -1 else j + 1
        else:
            i += 1
    return bodies


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
    program text is visible on the command line — a heredoc body or the left side of a pipe,
    both of which land as later tokens in this frame — matching the import is the same
    fail-closed decision as for ``-c``. When it is NOT visible (a file redirect, a bare
    ``python -`` fed by an unseen producer) there is nothing to match and the gate cannot see
    it; that residual is noted, not silently claimed as covered.
    """
    if not _PYTHON_PROGRAM_RE.match(_program_basename(tokens[i])):
        return False
    later_tokens = tokens[i + 1 :]
    # STDIN program: the whole FRAME is the search space, because the program text is not an
    # operand of this interpreter — it arrives on stdin, which the shell fills from a heredoc
    # body (later tokens) or a pipe producer (EARLIER tokens, e.g. `echo '…' | python -`). So a
    # per-position scan is wrong here; match the import anywhere in the frame. `_python_reads_stdin`
    # is precise so this does not fire for `python script.py`, `python -c …`, or `python -m …`.
    if _python_reads_stdin(later_tokens) and any(
        _inline_payload_reaches_cli(t.strip(_SHELL_WRAPPER_CHARS)) for t in tokens
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
    a heredoc redirect (``<<`` and the delimiter tag that follows it) is not an argument, and a
    pipe/redirect token ends this command's own arguments.
    """
    skip_next = False
    heredoc_tag_next = False
    for tok in later_tokens:
        norm = _normalize_operand(tok).strip("\"'")
        if heredoc_tag_next:
            heredoc_tag_next = False
            continue  # the heredoc delimiter word (`<< 'PY'` → the `PY`)
        if skip_next:
            skip_next = False
            continue  # value consumed by an operand-taking flag (`-X dev`)
        if not norm:
            continue
        if norm.startswith("<<"):
            heredoc_tag_next = norm == "<<"  # a bare `<<` splits its tag into the next token
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
    for frame in _self_token_frames(text_lower):
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
    subcommand (skipping flags and their arguments).

    Avoids false positives on ``git stash push`` by requiring ``push`` to
    be the FIRST non-flag token after ``git`` (the subcommand position).
    """
    try:
        tokens = normalize_shell_command(text_lower)
    except Exception:
        return False

    if not tokens:
        return False

    i = 0
    while i < len(tokens):
        token = tokens[i]
        # Check if this token resolves to "git"
        if os.path.basename(token) == "git" or token == "git":
            # Skip global flags and their arguments to find the subcommand
            j = i + 1
            while j < len(tokens):
                if tokens[j] in _GIT_ARG_FLAGS:
                    j += 2  # skip flag + its argument
                elif tokens[j].startswith("-"):
                    j += 1  # skip simple flag
                else:
                    break
            if j < len(tokens) and tokens[j] == "push":
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
_PROTECTED_BRANCHES = {"main", "mainline", "master"}

# Push flags that push EVERY local branch (protected ones included) regardless
# of any explicit refspec, so a per-branch target check cannot vouch for them.
# Presence of any of these denies the push outright (kept in lockstep with the
# ``--(mirror|all)`` regex in config/defaults.json).
_PUSH_ALL_BRANCHES_FLAGS = {"--mirror", "--all"}

# Symbolic refs that resolve at runtime — cannot statically verify safety.
# If the agent is on main and pushes HEAD, it pushes to main on the remote.
_AMBIGUOUS_REFS = {"head", "@", "fetch_head"}

# Refspecs containing shell expansion or git-revision syntax cannot be
# statically verified — deny them as ambiguous.
_AMBIGUOUS_REFSPEC_RE = re.compile(r"[$`]|@\{")

# TRUE shell command separators (NOT command-substitution boundaries). Used to
# scan the PRE-SPLIT text for substitution glued into a push target — see
# ``_is_push_to_protected_branch``.
_CMD_SEPARATOR_RE = re.compile(r"&&|\|\||[;|\n]")

# Shell expansions that fuse text INTO a word, so the literal command hides the
# real push target. Any of these inside a git-publish command is unverifiable
# -> deny (fail closed):
#   - command substitution   $(...)   and backticks  `...`
#   - parameter expansion     ${...}
#   - BRACE expansion         {a,b} / {1..5}  -- bash expands ``ma{i,i}n`` to
#     ``main`` and ``{main,x}`` to ``main x`` BEFORE git sees the token, so a
#     brace group containing a comma or ``..`` must be treated as ambiguous.
_AMBIGUOUS_EXPANSION_RE = re.compile(r"\$\(|\$\{|`|\{[^{}]*(?:,|\.\.)[^{}]*\}")


def _dequote_token(token: str) -> str:
    """Collapse shell quoting/escaping to the literal the shell passes to git.

    bash merges adjacent quoted/unquoted fragments into ONE word, so
    ``ma"in"``, ``m''ain`` and ``ma\\in`` all reach git as the literal
    ``main``. ``str.strip`` removes only the OUTERMOST quotes, leaving interior
    quote/backslash characters that make the token compare unequal to a
    protected name — an evasion of this gate. Remove ALL single/double quotes
    and backslash escapes so the comparison sees the shell-resolved word.
    """
    return token.replace("'", "").replace('"', "").replace("\\", "")


def _git_push_args(segment: str) -> list[str] | None:
    """Return the tokens AFTER the ``push`` subcommand if *segment* is a git push.

    Pure-Python (no regex backtracking — CodeQL ReDoS-safe) replacement for a
    ``\\bpush\\b`` scan. It anchors ``push`` as the git subcommand — the first
    non-flag token after ``git`` — so a segment that merely contains the word
    "push" (e.g. ``echo remember-to-push``) is NOT treated as a push and
    returns None. Skips leading flags, and a single non-flag value that a flag
    may take (e.g. ``-C <path>``) — but never swallows ``push`` itself.
    """
    tokens = segment.split()
    if "git" not in tokens:
        return None
    i = tokens.index("git") + 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 1  # skip the flag
        # A flag may take one separate non-flag value (e.g. ``-C <path>``);
        # never consume the ``push`` subcommand as a flag value.
        if i < len(tokens) and not tokens[i].startswith("-") and tokens[i] != "push":
            i += 1
    if i < len(tokens) and tokens[i] == "push":
        return tokens[i + 1 :]
    return None


def _is_protected_branch_name(name: str) -> bool:
    """Return True if *name* is a protected branch or an ambiguous ref."""
    return name in _PROTECTED_BRANCHES or name in _AMBIGUOUS_REFS


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


def _push_segment_targets_protected(arg_tokens: list[str]) -> bool:
    """Return True if a single push's argument tokens target protected/bare.

    *arg_tokens* are the tokens following the ``push`` subcommand within ONE
    shell segment (separators already removed).  A bare push (no explicit
    branch) is treated as protected because the current branch might be a
    protected one.  Force flags (``--force``/``-f``/``--force-with-lease``)
    do NOT by themselves make a feature-branch push protected — force-push to
    a feature branch is a normal PR/rebase workflow — but a force-push to a
    protected branch is still blocked, because the target check below fires
    regardless of any flags (force flags are stripped before the check).
    """
    tokens = [_dequote_token(t) for t in arg_tokens]
    # Deny-by-default: flags that push ALL local branches (protected ones
    # included) bypass any per-branch target check. Detect them BEFORE
    # stripping flags and deny outright, so the always-on gate never relies on
    # the secondary regex layer for this case.
    if any(tok in _PUSH_ALL_BRANCHES_FLAGS for tok in tokens):
        return True
    # Skip flags (tokens starting with -); non_flags[0] is the remote and
    # non_flags[1:] are the refspecs/branches.
    non_flags = [t for t in tokens if t and not t.startswith("-")]
    if len(non_flags) < 2:
        # Bare ``push`` or ``push <remote>`` with no explicit branch — the
        # current branch might be protected, so deny.
        return True
    for refspec in non_flags[1:]:
        # Refspecs with shell expansion ($, `) or git-revision syntax
        # (@{upstream}, @{u}) cannot be statically verified — deny.
        if _AMBIGUOUS_REFSPEC_RE.search(refspec):
            return True
        clean = refspec.lstrip("+")  # strip force-push '+' ref prefix
        # Wildcard refspec (refs/heads/*:refs/heads/*, *:*, feat*) expands to
        # MANY refs — like --mirror/--all it can include a protected branch and
        # cannot be statically verified. Deny.
        if "*" in clean:
            return True
        # Handle "local:remote" refspec format — the remote side is the target.
        target_branch = clean.split(":")[-1] if ":" in clean else clean
        # Normalize every ref spelling git resolves server-side (heads/main,
        # remotes/<remote>/main, refs/... ) to the bare name so the path form
        # cannot dodge the protected-name check.
        if _is_protected_branch_name(_normalize_ref(target_branch)):
            return True
    return False


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
    """
    saw_push = False
    for command in _CMD_SEPARATOR_RE.split(text_lower):
        # ``_is_git_publish`` (not ``_git_push_args``) gates the checks so that
        # glue-evasion forms — which do NOT tokenize to a clean ``git`` token —
        # are still recognized as pushes and cannot slip past the ambiguity /
        # fail-closed guards below.
        if not _is_git_publish(command):
            continue
        saw_push = True
        # Substitution / expansion glue anywhere in a push command makes it
        # unverifiable (the shell fuses it into the verb or the target word).
        if _AMBIGUOUS_EXPANSION_RE.search(command):
            return True
        args = _git_push_args(command)
        if args is None:
            # Detected as a push but not cleanly parseable (obfuscated) — deny.
            return True
        if _push_segment_targets_protected(args):
            return True
    if not saw_push:
        # A push was detected upstream (e.g. glue-evasion ``git_push``) but no
        # clean ``push`` segment survived splitting — deny to be safe.
        return True
    return False


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
                    "command": command[:200],
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
    ".local/share/kiro-cli",
    ".local/share/amazon-q",
    "Library/Application Support/kiro-cli",
    "Library/Application Support/amazon-q",
]

# ── KiroCrew's own data-home secrets & governance trust-root ──
# These leaves live directly under KiroCrew's data home and must never be
# readable OR writable through the shared file gate (``is_sensitive_path``):
#
#   .env                          channel/API credentials (Slack/Discord/…)
#   browser-cookies.txt           reusable browser-auth session cookies …
#   playwright-storage-state.json … and the Playwright storage-state they become
#   sel_hmac.key                  Security Event Log HMAC key — signs the
#   security_events.jsonl         tamper-evident audit chain (``sel.py``)
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
# not-yet-migrated pre-move legacy home (``~/.kirocrew``). Keeping one leaf list
# means a new secret is added once and covered in both locations. The migration
# force-deletes ``~/.kirocrew`` once the move completes — there is no rollback
# copy left behind to gate.
_CREW_HOME_PREFIXES: tuple[str, ...] = (".kiro/crew", ".kirocrew")
_CREW_SECRET_LEAVES: list[str] = [
    ".env",
    # The Notes builtin stores a GitHub Personal Access Token here so it can
    # push a vault. Owner-only mode (0600) does not isolate another process
    # running as the same UID, and the token is a live bearer credential for the
    # user's repositories, so it belongs behind the shared floor like every other
    # credential store. The app's own backend opens it directly rather than
    # through this gate, so it keeps working. It is a leaf here (not a flat
    # ``~/.kiro/crew`` entry) so it is generated for BOTH ``_CREW_HOME_PREFIXES``:
    # ``HOME`` follows ``config_dir()``, which can resolve to the legacy
    # ``.kirocrew`` data-home during a migration fallback, and the PAT must be
    # protected there too. A vault relocated with ``MD_NOTEBOOK_HOME`` falls
    # outside a home-relative entry; the default path is what ships and what an
    # agent would find.
    "workspace/md-notebook/pat",
    # The Notes builtin's vault registry. It is not a secret, but it stores each
    # vault's on-disk ``localPath``, which auto-sync trusts and runs ``git
    # add``/``commit``/``push`` against. A prompt-injected agent that could
    # rewrite this file would repoint a vault at an unrelated repository and have
    # the app commit and push work from it outside the hook controls, so the
    # agent must not be able to write it. The app's own backend opens it directly
    # rather than through this gate, so it keeps working.
    "workspace/md-notebook/vaults.json",
    "browser-cookies.txt",
    "playwright-storage-state.json",
    "sel_hmac.key",
    "security_events.jsonl",
    "app_admission.json",
    "security_policy.json",
    "profiles",
    "admission_policy.json",
    "denied_commands.json",
    # Which checkout the gateway executes (Dev Fleet "Make live"). The pointer is
    # resolved during startup and exec'd into, so a writable one is arbitrary
    # code execution in the gateway's own identity — the agent must not be able
    # to author it, and must not be able to read it back to discover a target to
    # aim at either. Only the human-driven dashboard cutover writes it, and the
    # gateway's own startup reader opens it directly rather than through this
    # gate, so both keep working.
    "live_target.json",
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
    "token_signing.key",
    "refresh_chains.json",
    ".local_secret",
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
]
_SENSITIVE_HOME_DIRS += [
    f"{prefix}/{leaf}" for prefix in _CREW_HOME_PREFIXES for leaf in _CREW_SECRET_LEAVES
]

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
    # .data-home-ready: the data-home completion marker (config.paths
    # MIGRATION_MARKER_NAME). It is AUTHORITATIVE — once present, boot trusts
    # ~/.kiro/crew and never re-migrates the legacy home. A prompt-injected
    # agent that could WRITE it into a pre-migration (empty/partial) new home
    # would make the next boot skip migration and ignore the legacy home's
    # governance policy + secrets. The migration code writes it directly and
    # does NOT route through this gate, so legitimate stamping still works.
    for leaf in ("config.json", "config.local.json", ".data-home-ready")
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
# ``is_sensitive_path`` stay unaffected), and the only legitimate readers
# (``kirocrew doctor``, the migration code) use Python ``os`` calls, not bash.
#
# The data-home marker is the sole entry: its mere PRESENCE is the migration
# trust signal, and — unlike config.json, whose inflated values the loader
# clamps at load time regardless of how they were written — nothing neutralizes
# a planted marker. A prompt-injected agent that shell-plants it into a
# pre-migration home makes the next boot skip migration and ignore the legacy
# home's governance policy + secrets; shell-deleting it forces a needless
# re-migration. The migration code stamps it directly in Python (not via bash),
# so legitimate stamping is unaffected. Kept as a literal to avoid a
# config->security import cycle; a drift guard in the tests pins it to
# ``MIGRATION_MARKER_NAME``.
#
# SCOPE NOTE (please do NOT flag incremental regex gaps as new HIGHs): this
# bash gate is DEFENSE-IN-DEPTH, not the primary control. The primary control
# is the file-edit tool gate (`is_sensitive_write_path`, above) plus the fact
# that the migration stamps the marker only from Python, never a tool/shell.
# Like the credential/sensitive-dir rules, the bash matcher is HOME-ANCHORED and
# shares their intrinsic limits — a `cd <home> && touch <leaf>` bare-relative
# write, or an unusual `${VAR}`/quoting form, can evade the regex exactly as it
# can for `~/.aws/credentials`. Chasing shell-parser completeness here is a
# losing game and holds the marker to a higher bar than credentials get; the
# realistic residual threat (skipping a one-time session-data copy) is low and
# already covered on the tool path. Widen this only via the SHARED matcher (so
# credentials benefit too), not with marker-only special cases.
#
# ``rotation.yaml`` is the second entry, and it meets the bar the scope note sets rather than
# being a special case: it is an INPUT TO AN AUTHORIZATION DECISION, not merely a
# security-relevant setting. An agent that rewrites it to name its own login makes
# ``rotation.authorize_action`` -> ``_definitely_off_shift`` accept a forged shift and execute a
# production write against a teammate's incident tooling — and unlike ``config.json``, whose
# inflated values the loader clamps, nothing downstream neutralizes a forged schedule entry.
# The tool-path gate (``is_sensitive_write_path``) is still the primary control; this closes the
# shell path for the same reason it is closed for the marker. Reads stay allowed on BOTH paths:
# every teammate's instance must read the file to answer "am I on call?", and it holds no
# secret. ``ledger_sync`` converges the file with a direct ``git checkout``, not through this
# gate, so team sync is unaffected.
# Entries are HOME-RELATIVE PATHS under the crew home, not bare filenames: the matcher
# builds ``<home>/<crew-prefix>/<entry>``. `rotation.yaml` therefore carries its
# ``apps/.../data/`` subpath — spelling it as a bare leaf silently matched nothing, which is
# the failure mode where a security addition reads as done and enforces nothing.
#
# The incident INDEX is the third, on the same reasoning one step over: it is what
# ``/incident/action`` reads to decide WHICH signal the autonomy gate is authorizing, so an
# agent that rewrites it can have the gate approve one signal while the sink mutates another.
# Reads stay allowed for the same reason as the schedule — it is the board every instance
# renders, and it holds no secret.
_WRITE_PROTECTED_BASH_LEAVES: tuple[str, ...] = (
    ".data-home-ready",
    "apps/ops-mission-control/data/rotation.yaml",
    "apps/ops-mission-control/data/incidents/index.json",
)

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

# Matches python/ruby/perl one-liners that open sensitive paths
_SCRIPT_OPEN = r"(?:python|ruby|perl)\S*\s.*open\s*\("


def _build_sensitive_regex() -> re.Pattern[str]:
    """Build a compiled regex matching bash reads OR writes of sensitive paths.

    Three matching strategies, OR'd:
      1. a READ verb / WRITE verb / script-open / shell-redirect followed by a
         sensitive path (the original verb-anchored form);
      2. a verb-INDEPENDENT catch-all: a sensitive path appearing ANYWHERE in
         the command as an argument token.  This is the real backstop — a write
         verb the allowlist forgot (or a novel one) is still blocked because the
         destination path is sensitive.  Reading a sensitive path is itself
         already blocked by is_sensitive_path on the file-read title, so flagging
         any command that *names* the trust-root/credential path is correct and
         fail-safe.
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
    escaped_dirs = [re.escape(d) for d in _SENSITIVE_HOME_DIRS]
    dirs_pattern = "|".join(escaped_dirs)
    sensitive_path = rf"{home_alts}/(?:{dirs_pattern})(?:/|\s|$|['\"])"
    # Write-protected leaves (e.g. the data-home marker): a full home-anchored
    # path to a specific leaf file, matched verb-INDEPENDENTLY (below) so no
    # write form can bypass it. See _WRITE_PROTECTED_BASH_LEAVES for why reads
    # are blocked too (harmless: no secret; legitimate readers use Python).
    wp_prefixes = "|".join(re.escape(p) for p in _CREW_HOME_PREFIXES)
    wp_leaves = "|".join(re.escape(leaf) for leaf in _WRITE_PROTECTED_BASH_LEAVES)
    write_protected_path = (
        # trailing ``/`` is included so ``mkdir -p ~/.kiro/crew/.data-home-ready/x``
        # (which also MATERIALISES the marker as a directory, satisfying
        # ``marker.exists()``) is caught, not just the exact-leaf forms.
        rf"{home_alts}/(?:{wp_prefixes})/(?:{wp_leaves})(?:/|\s|$|['\"])"
    )
    return re.compile(
        # (1) verb/redirect-anchored, OR (2) verb-independent: the sensitive path
        # appears anywhere as a token.  The token anchor accepts start-of-string
        # plus the separators that precede a path argument: whitespace, quote,
        # ``=`` (VAR=path), AND ``:``/``,``/``;`` (option:path, PATH-style
        # colon lists, comma/semicolon-joined args) — without the latter a
        # ``FOO=bar:~/.aws/credentials`` or ``PATH=/x:~/.ssh/id_rsa`` token slips
        # past the backstop while no verb branch fires either.
        # (3) write-protected leaf: matched verb-INDEPENDENTLY too (same token
        # anchor), so a quoted redirect (``> "$HOME/.../marker"``), ``cp``,
        # ``python -c "open(...,'w')"`` or any novel write verb is still caught.
        rf"(?:(?:{_READ_CMDS}.*|{_WRITE_CMDS}.*|{_SCRIPT_OPEN}.*|.*[<>|]\s*)"
        rf"{sensitive_path}"
        rf"|(?:^|.*[\s'\"=:,;]){sensitive_path}"
        rf"|(?:^|.*[\s'\"=:,;]){write_protected_path})",
        re.IGNORECASE,
    )


_SENSITIVE_RE: re.Pattern[str] | None = None


def _get_sensitive_re() -> re.Pattern[str]:
    global _SENSITIVE_RE
    if _SENSITIVE_RE is None:
        _SENSITIVE_RE = _build_sensitive_regex()
    return _SENSITIVE_RE


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
    # the lexical forms are the fail-safe fallback when resolution cannot
    # complete (over-matching a sensitive-looking path is the safe direction).
    candidates: set[str] = set()
    try:
        candidates.add(os.path.realpath(expanded))
    except (OSError, ValueError):
        pass
    try:
        # Guarded false-positive: this resolve() is INSIDE is_sensitive_path — the
        # sanitizer itself — building candidate forms to CHECK a path against the
        # sensitive denylist. It performs no read/write. CodeQL surfaces
        # py/path-injection here only because a new caller (artifact relocate)
        # reaches it with user input; the function's whole purpose is to vet that
        # input, so suppress the alert on the resolution step.
        candidates.add(str(Path(expanded).resolve()))  # lgtm[py/path-injection]
    except (OSError, ValueError, RuntimeError):
        pass
    candidates.add(os.path.normpath(expanded))
    candidates.add(expanded)
    return candidates


def _home_dir_targets_uncached(
    home_dirs: list[str],
    roots: tuple[str, str | None] | None = None,
) -> set[str]:
    """Anchor the ``$HOME``-relative *home_dirs* entries into absolute, casefolded
    on-disk targets.

    *roots* optionally supplies the ``(home, crew_home)`` anchors already
    resolved by the caller. The TTL cache in :func:`_home_dir_targets` MUST pass
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
    if roots is not None:
        home, crew_home = roots
    else:
        home, crew_home = _resolved_root_key()

    def _anchor(root: str, d: str) -> str:
        return os.path.join(root, *d.split("/")).casefold()

    sensitive_targets: set[str] = {_anchor(home, d) for d in home_dirs}
    home_real = os.path.realpath(home)
    if home_real.casefold() != home.casefold():
        sensitive_targets |= {_anchor(home_real, d) for d in home_dirs}
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
                    try:
                        sensitive_targets.add(os.path.realpath(full).casefold())
                    except (OSError, ValueError):
                        pass
                    break
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
# rebuild it replaces, so the win survives.
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


def _resolved_root_key() -> tuple[str, str | None]:
    """Return the (home, crew_home) roots the target set is anchored on.

    Mirrors how :func:`_home_dir_targets_uncached` derives its anchors, so the
    cache key changes exactly when the anchors would. Falls back to the
    unresolved form on OSError/ValueError the same way the builder does.
    """
    try:
        home = str(Path.home().resolve())
    except (OSError, ValueError):
        home = str(Path.home())
    crew_env = os.environ.get("KIROCREW_HOME")
    if not crew_env:
        return home, None
    try:
        crew = str(Path(crew_env).expanduser().resolve())
    except (OSError, ValueError):
        crew = os.path.abspath(os.path.expanduser(crew_env))
    return home, crew


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
    targets = _home_dir_targets_uncached(home_dirs, roots)
    # Bound the dict: the key space is tiny (two constant home_dirs lists ×
    # roots), but a test or embedder that churns KIROCREW_HOME must not grow it
    # without limit.
    if len(_home_targets_cache) > 32:
        _home_targets_cache.clear()
    _home_targets_cache[key] = (now + _HOME_TARGETS_TTL_SECS, targets)
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

    candidates = _candidate_forms(path_str, base_dir)
    sensitive_targets = _home_dir_targets(home_dirs)

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


def is_sensitive_path(path_str: str, base_dir: str | None = None) -> bool:
    """Return True if the path points to a read+write-sensitive location.

    Used across every file-access surface (hooks.on_tool_call, validate_file_path,
    artifacts, dashboard file I/O, knowledge indexing) to block BOTH reads and
    writes of credential files and the governance trust-root
    (:data:`_SENSITIVE_HOME_DIRS`). See :func:`_path_in_home_dirs` for the
    symlink/casefold matching contract.
    """
    return _path_in_home_dirs(path_str, _SENSITIVE_HOME_DIRS, base_dir)


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
    sensitive_targets = _home_dir_targets(_SENSITIVE_HOME_DIRS)
    for cand in _candidate_forms(dir_str, base_dir):
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
    """
    return _path_in_home_dirs(
        path_str, _SENSITIVE_HOME_DIRS + _WRITE_PROTECTED_HOME_PATHS, base_dir
    )


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


def exfil_query_min_len() -> int:
    """Public view of the long-query exfiltration threshold (chars)."""
    return _EXFIL_QUERY_MIN_LEN


# Archive/extraction destination flags (tar -C, unzip -d, rsync dest) pointing
# INTO the governance trust-root parent (the crew data home) — an extraction
# there can drop/overwrite ``security_policy.json`` or a ``profiles/`` entry even
# though the bare home dir is not itself a sensitive-path entry.  Match the
# destination-dir form specifically so normal home access (sessions.db,
# config.json) is not over-blocked.  Covers every crew home root: the current
# ``~/.kiro/crew`` and a not-yet-migrated pre-move legacy ``~/.kirocrew``.
_CREW_HOME_ALT = "|".join(re.escape("/" + p) for p in _CREW_HOME_PREFIXES)
_EXTRACT_INTO_TRUST_ROOT_RE = re.compile(
    r"-(?:C|d)\s+(?:~|\$HOME|/home/[^/\s]+|/Users/[^/\s]+|"
    + re.escape(str(Path.home()))
    + r")(?:"
    + _CREW_HOME_ALT
    + r")(?:/[^\s]*)?(?:\s|$|['\"])",
    re.IGNORECASE,
)

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
_RELATIVE_SENSITIVE_RE = re.compile(
    rf"(?:^|[\s'\"=:,;])(?:\.\.?/)+(?:{_SENSITIVE_SEGMENT_ALT})(?:/|\s|$|['\"])",
    re.IGNORECASE,
)


# ── Read verbs for normalizer second-pass ──
# Programs that can read file contents. Used to detect path-based credential
# access via the normalizer when the regex first-pass misses obfuscated forms.
_NORMALIZER_READ_VERBS: frozenset[str] = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "strings",
        "xxd",
        "base64",
        "cp",
        "scp",
        "open",
        "vi",
        "vim",
        "nano",
        "code",
        # Extended coverage for relative-traversal attacks (pentest finding):
        "awk",
        "od",
        "nl",
        "sed",
        "perl",
        "grep",
        "egrep",
        "fgrep",
        "sort",
        "uniq",
        "wc",
        "cut",
        "paste",
        "diff",
        "tee",
        "xargs",
        "file",
        "stat",
        "md5sum",
        "sha256sum",
        "python",
        "python3",
        "ruby",
        "node",
    }
)


# ── Hardlink/symlink creation verbs ──
# A hardlink (or symlink) to a credential file "flattens" it onto a benign,
# non-sensitive alias: `ln ~/.npmrc ./x` then reading `./x` exposes the token
# while dodging the path-based read matcher (GPT review, PR #1339 — standard
# sandbox mode does NOT bind-mask credential paths, so the command gate is the
# only line there). We do NOT block link/linkat at the syscall layer (that
# banned npm cacache's internal fs.link and every benign hardlink); instead we
# treat an AGENT-ISSUED link command like a read and resolve its operands
# through is_sensitive_path(), so linking a sensitive SOURCE is refused at the
# same fidelity as reading it. npm's own fs.link() never transits this gate.
_LINK_CREATE_VERBS: frozenset[str] = frozenset({"ln", "link"})


def is_sensitive_bash_command(command: str) -> str | None:
    """Check if a bash command reads sensitive paths, accesses IMDS, or leaks env creds.

    Uses a two-pass approach:
    1. **Regex first-pass (fast):** Pattern match against known read-verb + sensitive
       path combinations. Catches unobfuscated commands instantly.
    2. **Normalizer second-pass:** Tokenizes the command via
       ``normalize_shell_command`` (strips shell quoting, expands $HOME/~, resolves
       relative paths), then routes each path-like token through
       ``is_sensitive_path()`` to catch obfuscation (e.g. ``ca""t ~/.aws/credentials``,
       ``awk '{print}' $HOME/.ssh/id_rsa``, ``sed -n p ~/../../etc/shadow``).

    Returns denial reason string, or None if clean.
    """
    # ── Pass 1: regex fast-path ──
    if _get_sensitive_re().search(command):
        return "Blocked: command accesses sensitive credential path"
    if _EXTRACT_INTO_TRUST_ROOT_RE.search(command):
        return "Blocked: command extracts into the governance trust-root directory"
    # Block ANY command referencing a sensitive path via relative traversal,
    # regardless of verb.  The home-anchored/absolute forms are already caught
    # by the matcher above; this covers the relative-traversal forms that escape
    # it (was gated on ln/cp only, so dd/base64/xxd/head/tail slipped past).
    if _RELATIVE_SENSITIVE_RE.search(command):
        return "Blocked: command references a sensitive credential path via relative traversal"

    # ── Pass 2: normalizer-based sensitive path detection ──
    normalizer_result = _check_sensitive_via_normalizer(command)
    if normalizer_result:
        return normalizer_result

    # IMDS access via any IP encoding (decimal, hex, octal, IPv6-mapped)
    imds_result = _check_imds_access(command)
    if imds_result:
        return imds_result
    # Environment credential exfiltration (declare -p, env|grep, printenv, etc.)
    env_result = _check_env_credential_access(command)
    if env_result:
        return env_result
    return None


def _check_sensitive_via_normalizer(command: str) -> str | None:
    """Normalizer second-pass: tokenize command and route paths through is_sensitive_path.

    Catches obfuscation the regex first-pass cannot:
    - Quoted command names: ``ca""t ~/.aws/credentials``
    - Variable expansion: ``$HOME/.ssh/id_rsa``
    - Relative traversal: ``awk '{print}' ~/../../.aws/credentials``
    - Mixed evasion: ``"cat" ~/.aws/credentials``

    Only triggers when a recognized read verb is present in the resolved tokens
    (avoids false positives on write/create commands).

    Returns denial reason string, or None if clean.
    """
    try:
        tokens = normalize_shell_command(command)
    except Exception:
        return None

    if not tokens:
        return None

    # Check if any token resolves to a known read verb or hardlink/symlink
    # creation verb (by basename, so /usr/bin/cat is recognized as "cat").
    has_relevant_verb = False
    for token in tokens:
        if not token:
            continue
        basename = os.path.basename(token).lower()
        if basename in _NORMALIZER_READ_VERBS or basename in _LINK_CREATE_VERBS:
            has_relevant_verb = True
            break

    if not has_relevant_verb:
        return None

    # Route each path-like token through is_sensitive_path()
    for token in tokens:
        if not token:
            continue
        # Skip flags
        if token.startswith("-"):
            continue
        # Skip tokens that ARE the verb itself
        basename = os.path.basename(token).lower()
        if basename in _NORMALIZER_READ_VERBS or basename in _LINK_CREATE_VERBS:
            continue
        # Only check tokens that look like filesystem paths
        if not _is_path_like(token):
            continue
        # is_sensitive_path handles symlink resolution, traversal, ~ expansion,
        # $HOME expansion, and all sensitive directory checks
        if is_sensitive_path(token):
            return (
                "Blocked: command accesses sensitive credential path "
                f"(resolved via normalizer: {token[:80]})"
            )
    return None


# ── URL Exfiltration Detection ──
# Detects URLs whose path/query contain credential-like data. We flag the
# PAYLOAD, not the destination: any URL with secrets is suspicious regardless of
# host. The sole host-sensitive carve-out is a companion-supplied exact-host
# exemption (see _exfil_url_warning) that narrows ONLY the base64-blob and
# query-length heuristics for trusted tenants; the hard-credential floor and the
# heavy percent-encoding detector stay unconditional for every host.

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
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)

# Heavy URL-encoding detector — the same "20+ consecutive percent-encoded
# octets" branch carved out of _EXFIL_PATTERNS. This stays UNCONDITIONAL: the
# exact-host exemption below skips only the base64-blob and query-length
# heuristics (which false-positive on legitimate long base64 document
# pointers), NOT this percent-encoding detector, so an encoded exfil payload to
# a trusted-tenant host is still caught.
_EXFIL_PERCENT_RE = re.compile(
    r"%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}",
    re.IGNORECASE,
)

# S3 presigned URLs contain X-Amz-Signature (a 64-char hex string) that
# matches the base64-like blob pattern above.  These are intentional
# time-limited access tokens, not leaked credentials.  Skip the exfil
# check when ALL standard presigned-URL query params are present on an
# amazonaws.com domain.  Values are validated to prevent spoofing.
_S3_PRESIGNED_RE = re.compile(
    r"X-Amz-Algorithm=AWS4-HMAC-SHA256"
    r".*X-Amz-Credential=(?:AKIA|ASIA)[A-Z0-9]{16}(?:%2F|/)"
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
    r"^(?:AKIA|ASIA)[A-Z0-9]{16}(?:%2F|/)[0-9]{8}"
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
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
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

    Degrade semantics (INVERTED vs ``redact_via_context``'s baseline-redact
    fallback): ``PlatformCompositionError`` propagates fail-closed, but any other
    adapter failure degrades to ``frozenset()`` — the empty set means MORE
    redaction (every host runs the heuristics), the SAFE direction here.  A
    pre-method companion adapter (no ``exempt_exact_hosts``) degrades to the empty
    set via ``getattr`` rather than raising.  NO logging on the degrade path: this
    runs inside the stdio MCP servers whose stray writes corrupt the JSON-RPC
    stream.
    """
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    try:
        policy = current_context().credentials
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
    except PlatformCompositionError:
        raise
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


def _exfil_url_warning(
    domain: str,
    path_and_query: str,
    exempt_hosts: frozenset[str],
) -> str | None:
    """Classify one matched URL — the single per-URL exfil verdict.

    Shared by scan_exfiltration_urls (which collects the warnings) and
    redact_exfiltration_urls (which redacts every URL that returns non-None), so
    the two paths can never drift — redact_ early-returns on scan_'s warnings, so
    a divergence would silently produce warnings-without-redaction. Returns the
    warning string, or None if the URL is clean/exempt.
    """
    qmark = path_and_query.find("?")
    query = path_and_query[qmark + 1 :] if qmark != -1 else ""

    # Valid S3 presigned URLs carry AKIA in X-Amz-Credential legitimately, so
    # exempt them wholesale BEFORE the hard-credential path scan below would
    # otherwise flag them.
    if query and _is_safe_presigned(domain, query):
        return None

    # Hard credential markers ANYWHERE in the path or query.
    # The base64/length heuristics below are query-only, so a secret embedded in
    # the URL PATH (``https://evil/AKIA…`` — no ``?``) escaped them entirely, and
    # a raw-IP host never even matched _URL_RE. These markers (AKIA/ASIA,
    # key=value creds, SSH/PEM, Slack) are unambiguous, so flag regardless of
    # domain — a real AWS key in a URL is exfil even to an otherwise-safe (or
    # exempted) host. This hard-credential floor is UNCONDITIONAL.
    if _HARD_CREDENTIAL_RE.search(path_and_query):
        return f"Suspicious URL with credential in path/query: {domain}"

    if qmark == -1:
        return None

    # UNCONDITIONAL base64 decode-and-scan: a hard credential (AWS key, SSH/PEM,
    # Slack token) that is base64-ENCODED into the query would slip past the raw
    # _HARD_CREDENTIAL_RE floor above (which matches literal markers, not encoded
    # bytes) AND, on an exempt host, past the raw base64-blob heuristic below.
    # Decode any base64 chunk and re-scan the decoded bytes for credential
    # markers; a legitimate base64 *document* decodes to non-credential text and
    # _decode_b64_safe returns "" (so it still qualifies for the exemption).
    # This runs for EVERY host, closing the encoded-credential-to-trusted-tenant
    # gap without re-flagging benign document pointers.
    if query and _decode_b64_safe(query):
        return f"Suspicious URL with encoded credential in query: {domain}"

    # Exact-host heuristic exemption (companion-supplied trusted tenants),
    # matched case-insensitively and EXACTLY (not by suffix) so a shared
    # multi-tenant domain does not exempt every tenant. The exemption skips ONLY
    # the raw base64-blob and query-length heuristics below — the ones that
    # false-positive on legitimate long base64 document pointers. Everything
    # else stays unconditional: the hard-credential floor above already ran, the
    # decode-and-scan just above catches ENCODED credentials on every host, and
    # the heavy percent-encoding detector below runs even for exempted hosts, so
    # an encoded exfil payload to a trusted tenant is still caught.
    _dom = domain.lower()
    _exempt = _dom in exempt_hosts
    if not _exempt:
        # (Valid S3 presigned URLs were already exempted at the top, so no
        # _is_safe_presigned re-check is needed here.)
        if len(query) >= _EXFIL_QUERY_MIN_LEN:
            return (
                f"Suspicious URL with long query params ({len(query)} chars): "
                f"{domain}{path_and_query[:60]}..."
            )
        if _EXFIL_PATTERNS.search(query):
            return f"Suspicious URL with credential-like query data: {domain}"

    # Heavy percent-encoding is a hard heuristic, NOT part of the exempted
    # base64/length set — it runs for every host (for non-exempt hosts it was
    # already covered by _EXFIL_PATTERNS above, so this only adds coverage on
    # exempted hosts).
    if _EXFIL_PERCENT_RE.search(query):
        return f"Suspicious URL with credential-like query data: {domain}"
    return None


def scan_exfiltration_urls(text: str) -> list[str]:
    """Scan text for URLs that may be exfiltrating data via query params.

    Flags the PAYLOAD, not the destination: the hard-credential floor and the
    base64/length heuristics inspect the URL path+query for secret patterns
    regardless of host. The one host-sensitive exception is a companion-supplied
    exact-host exemption that narrows ONLY the base64/length heuristics for
    trusted tenants (see _exfil_url_warning); the hard-credential floor and the
    percent-encoding detector stay unconditional. Returns list of warning
    strings, empty if clean.
    """
    exempt_hosts = _exfil_exempt_hosts()
    warnings: list[str] = []
    for match in _URL_RE.finditer(text):
        warning = _exfil_url_warning(match.group(1), match.group(3) or "", exempt_hosts)
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
        if _exfil_url_warning(domain, match.group(3) or "", exempt_hosts):
            result = result.replace(match.group(0), f"[REDACTED: suspicious URL to {domain}]")
    return result, warnings


# ── Credential Output Redaction ──
# Catches raw credential patterns in LLM output / tool results,
# including base64-encoded variants.  Applied on all output paths
# alongside redact_exfiltration_urls().

_CREDENTIAL_PATTERNS = re.compile(
    r"(?:"
    # ── AWS ──
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"  # AWS access key ID
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
    # DB connection URIs with embedded credentials — redact the
    # ``scheme://user:pass@`` prefix (the password lives here).
    r"|(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|amqp(?:s)?)"
    # User portion is `*` (not `+`): empty-user connection strings (e.g. MongoDB
    # Atlas IAM `mongodb+srv://:secret@…`) still redact the password (ported
    # from the upstream project).
    r"://[^\s:/@]*:[^\s/@]+@"
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
    r"|eyJ[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}"  # JWS (3-seg) / JWE (5-seg incl. dir/ECDH-ES)
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


def _shannon_entropy(token: str) -> float:
    """Return the Shannon entropy of *token* in bits per character."""
    if not token:
        return 0.0
    counts = Counter(token)
    length = len(token)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


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
    printable = sum(1 for b in raw if 0x20 <= b <= 0x7E or b in (0x09, 0x0A, 0x0D))
    return printable / len(raw) >= _SECRET_PRINTABLE_DECODE_RATIO


def _longest_lowercase_run(token: str) -> int:
    """Return the length of the longest run of consecutive lowercase letters.

    Dictionary-word identifiers and file-path segments contain long lowercase
    word runs; a uniformly random base64 secret almost never does. This is the
    primary discriminator that keeps camelCase identifiers and mixed-case file
    paths out of the bare-secret heuristic.
    """
    best = current = 0
    for ch in token:
        if ch.islower():
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _vowel_ratio(token: str) -> float:
    """Return the fraction of alphabetic characters in *token* that are vowels."""
    letters = [ch for ch in token if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch in _VOWELS) / len(letters)


def _looks_like_secret_key(token: str) -> bool:
    """Return True if *token* has the shape of a bare AWS secret access key.

    Conservative, multi-gate classifier for a label-less 40-char base64 secret.
    Every gate must pass; the design bias is toward NOT
    redacting (a false negative merely reverts to today's behavior, a false
    positive corrupts benign output). Gates, cheapest-first:

    1. Length is EXACTLY 40 (AWS secret-key length).
    2. Contains all three of lower + upper + digit (rejects all-lower prose runs,
       all-upper CONSTANT_NAMES, base32, digit strings).
    3. Not an all-hex run (rejects git SHAs, sha256/md5 digests).
    4. Shannon entropy >= _SECRET_ENTROPY_MIN (rejects low-entropy repeats/prose
       and most code identifiers, which cluster below 4.3).
    5. Does not base64-decode to printable text (rejects encoded-text blobs).
    6. Structural randomness: longest lowercase run <= _SECRET_MAX_LOWER_RUN AND
       vowel ratio <= _SECRET_MAX_VOWEL_RATIO. These separate a random key from
       word-based identifiers and slash-delimited file paths that survive the
       entropy floor. Both gates apply to EVERY token (a '/' or '+' does not
       exempt a token, so 40-char mixed-case file paths stay intact).

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
    has_lower = any(ch.islower() for ch in token)
    has_upper = any(ch.isupper() for ch in token)
    has_digit = any(ch.isdigit() for ch in token)
    if not (has_lower and has_upper and has_digit):
        return False
    if _HEX_ONLY_RE.match(token):
        return False
    if _shannon_entropy(token) < _SECRET_ENTROPY_MIN:
        return False
    if _decodes_to_printable_text(token):
        return False
    return (
        _longest_lowercase_run(token) <= _SECRET_MAX_LOWER_RUN
        and _vowel_ratio(token) <= _SECRET_MAX_VOWEL_RATIO
    )


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
    that :func:`_looks_like_secret_key` already applies per-window (gate 5),
    lifted to run granularity so a misaligned window cannot defeat it. A genuine
    glued secret (``X`` + key, key + ``ABC``, key + ``X`` + key) does NOT decode
    cleanly as a whole run, so it still reaches the sliding window below.
    """
    if len(run) < _SECRET_KEY_LEN:
        return False
    if _decodes_to_printable_text(run):
        return False
    for start in range(len(run) - _SECRET_KEY_LEN + 1):
        if _looks_like_secret_key(run[start : start + _SECRET_KEY_LEN]):
            return True
    return False


def _decode_b64_safe(text: str) -> str:
    """Try to base64-decode chunks in text; return decoded content or ''."""
    for m in _B64_CHUNK_RE.finditer(text):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
            if _CREDENTIAL_PATTERNS.search(decoded):
                return decoded
        except Exception:
            continue
    return ""


# Standard replacement tag for a redacted credential. Shared between the batch
# redactor (`redact_credentials`) and the streaming fail-closed path
# (`StreamRedactor.feed`) so the on-the-wire marker is identical everywhere.
_REDACTED_CREDENTIAL_TAG = "[REDACTED: credential]"

# Public alias for modules that must emit the SAME tag rather than duplicate the
# literal — e.g. the pptx-maker preview, which excises a credential-bearing bitmap
# itself because this module's redactor recognises a narrower token set than that
# scan matches.
REDACTED_CREDENTIAL_TAG = _REDACTED_CREDENTIAL_TAG


def redact_credentials(text: str) -> tuple[str, list[str]]:
    """Redact raw credential patterns from text, including base64-encoded.

    Returns (cleaned_text, list_of_warnings).
    """
    warnings: list[str] = []
    result = text

    # 1. Redact plaintext credential patterns
    for m in _CREDENTIAL_PATTERNS.finditer(result):
        matched = m.group()
        tag = _REDACTED_CREDENTIAL_TAG
        result = result.replace(matched, tag, 1)
        # Emit ONLY non-sensitive metadata (length). Do NOT slice any part of
        # `matched` into the warning: `_CREDENTIAL_PATTERNS` matches the raw
        # secret value itself (e.g. `ghp_…`, `sk-ant-…`), so even a short prefix
        # is genuine plaintext key material — a fixed-length token prefix leaves
        # ~12-16 secret chars in a 20-char slice. The warnings list is a
        # redaction-subsystem output expected to be safe to log/surface, so it
        # must carry no secret bytes. Mirrors the base64 / bare-secret branches
        # below, which already log length only.
        warnings.append(f"Redacted credential pattern ({len(matched)} chars)")

    # 2. Detect and redact base64-encoded credentials
    for m in _B64_CHUNK_RE.finditer(text):
        chunk = m.group()
        decoded = _decode_b64_safe(chunk)
        if decoded:
            result = result.replace(chunk, "[REDACTED: encoded credential]", 1)
            warnings.append(f"Redacted base64-encoded credential ({len(chunk)} chars)")

    # 3. Detect and redact BARE 40-char AWS secret keys with no label/prefix
    # These carry no distinctive marker for _CREDENTIAL_PATTERNS
    # to anchor on, so an entropy + structural heuristic is the only way to catch
    # a standalone secret value. Scan the ORIGINAL text (not the already-mutated
    # result) so match offsets are stable; skip any run whose text has already
    # been redacted away by an earlier pass.
    for m in _BARE_SECRET_RUN_RE.finditer(text):
        run = m.group()
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

    Returns:
        Denial reason string (mentioning the matched pattern), or
        ``None`` if the input is allowed.
    """
    lower = tool_name.lower()
    glob_patterns = list(extra_patterns or [])
    if denied_regexes is None:
        regex_patterns = compute_effective_denied(BUILTIN_DENIED_RULES, (), False, (), ())
    else:
        regex_patterns = list(denied_regexes)
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
                    return f"Blocked by security policy: {interpreter_pattern}"
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
    push_allow_pending = False
    if _is_git_publish(lower):
        if _is_push_to_protected_branch(lower):
            _emit_deny_event(tool_name, _GIT_PUBLISH_DENY_LABEL, lower)
            return f"Blocked by security policy: {_GIT_PUBLISH_DENY_LABEL}"
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
    ):
        pattern = _SELF_PROTECTION_FLOOR_BY_ID.get(rule_id)
        if pattern is None or pattern not in floor_enabled:
            continue
        if predicate(lower):
            # Report the rule's own pattern, exactly as the regex tier does, so
            # the denial reason and the SEL event still map back to the rule id.
            _emit_deny_event(tool_name, pattern, lower)
            return f"Blocked by security policy: {pattern}"

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
            whole_string_exception_match = exceptions and any(
                fnmatch.fnmatch(lower, e.lower()) for e in exceptions
            )
            if not whole_string_exception_match:
                _emit_deny_event(tool_name, pattern, lower)
                return f"Blocked by security policy: {pattern}"

    # ── Pass 2: per-segment (re-)evaluation ──
    # Split into segments and check each.  This runs UNCONDITIONALLY: besides
    # the exception-carve-out re-check, splitting isolates an embedded real
    # publish/destructive command (e.g. after ``;`` / ``&&`` / inside
    # ``$(...)``) into its own segment so it matches the deny pattern in its own
    # right (chaining-bypass protection).  Segments that match a deny pattern
    # AND an exception are allowed with a SEL audit event.
    segments = _split_segments(lower)
    for segment in segments:
        seg_lower = segment.strip()
        if not seg_lower:
            continue
        for pattern, is_regex in all_patterns:
            if _deny_pattern_matches(pattern, seg_lower, is_regex):
                exceptions = _DENY_EXCEPTIONS.get(pattern, [])
                if exceptions and any(fnmatch.fnmatch(seg_lower, e.lower()) for e in exceptions):
                    if not _emit_deny_exception_event(tool_name, pattern):
                        _emit_deny_event(tool_name, pattern, seg_lower)
                        return f"Blocked by security policy: {pattern}"
                    # Exception granted for this pattern on this segment;
                    # continue to evaluate any remaining patterns against
                    # the same segment (a different pattern without an
                    # exception must still cause a deny).
                    continue
                _emit_deny_event(tool_name, pattern, seg_lower)
                return f"Blocked by security policy: {pattern}"
    # All windows cleared the deny passes — the input is allowed.  If it was a
    # feature-branch push, emit the deferred allow audit now (final outcome).
    if push_allow_pending:
        _schedule_push_allow_audit(lower)
    return None


def _split_segments(command_lower: str) -> list[str]:
    """Split a command into independently-evaluatable segments.

    Splits on shell separators and command-substitution boundaries.
    Returns the list of segments (which may include the empty string for
    adjacent separators; callers should skip empties).
    """
    return _CMD_SPLIT_RE.split(command_lower)


def _emit_deny_event(tool_name: str, deny_pattern: str, segment: str) -> None:
    """Emit a SEL audit event when a command is denied.

    Records the operation, matched pattern, and (for pass-2 denials) the
    specific segment that triggered the block.  This satisfies the
    security-controls guideline that every permission decision — both
    grants and denials — must produce an audit trail.

    Best-effort: SEL logging failures are logged at WARNING and do not
    affect the deny decision (denials are inherently fail-closed; the
    block stands regardless of audit success).
    """
    try:
        sel = SecurityEventLog()
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
                metadata={
                    "deny_pattern": deny_pattern,
                    "segment": segment[:200] if segment else "",
                    "mechanism": "BUILTIN_DENY_PATTERNS",
                },
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


def audit_bash_exfiltration(command: str) -> str | None:
    """Return a denial reason if *command* matches a data-egress / reverse-shell
    shape that must be blocked at the tool-invocation gate, else None.

    Scoped to _BASH_EXFIL_PATTERNS / _BASH_EXFIL_RES (exfil/reverse-shell only) so
    it can be wired into the deny path in ``hooks.on_tool_call`` without blocking
    benign local commands. The broader :func:`audit_bash_command` stays advisory.
    """
    lower = command.lower()
    for pattern in _BASH_EXFIL_PATTERNS:
        pat = pattern.lower()
        if "*" in pat:
            if fnmatch.fnmatch(lower, f"*{pat}*"):
                return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
        elif pat in lower:
            return f"Blocked: command matches data-exfiltration pattern '{pattern}'"
    for rx, label in _BASH_EXFIL_RES:
        if rx.search(command):
            return f"Blocked: command matches data-exfiltration pattern ({label})"
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
                    "sample": sample[:200] if sample else "",
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
    """
    if not cmd or not cmd.strip():
        return []

    # Pre-process: expand $HOME/${HOME} BEFORE shlex splitting so that
    # expansion happens even inside quoted strings that shlex won't expand.
    home = os.path.expanduser("~")
    # Replace via a FUNCTION, not a string template: on Windows the home path
    # is ``C:\Users\<name>``, and ``re.sub`` parses a str replacement as a
    # template eagerly -- ``\U`` is an invalid escape, so a string replacement
    # raises ``re.error`` for EVERY input on that platform, not just ones
    # containing ``$HOME``.  A callable is substituted literally.
    preprocessed = _HOME_VAR_RE.sub(lambda _m: home, cmd)

    # Tokenize using POSIX shlex — handles quoting, escaping, etc.
    try:
        tokens = shlex.split(preprocessed, posix=True)
    except ValueError:
        # Unbalanced quotes or other parse errors — fall back to basic split.
        tokens = preprocessed.split()
        tokens = [t.strip("\"'\\") for t in tokens]

    resolved: list[str] = []
    for token in tokens:
        # Strip empty-string concatenation artifacts: ca""t -> cat, g''it -> git
        token = _EMPTY_QUOTE_RE.sub("", token)

        # Expand tilde (shlex doesn't do tilde expansion)
        if token.startswith("~"):
            token = os.path.expanduser(token)

        resolved.append(token)

    return resolved


def resolve_command_paths(tokens: list[str]) -> list[str]:
    """Resolve path-like tokens to their canonical absolute form.

    Runs os.path.realpath() on tokens that look like filesystem paths
    (start with /, ~, ./, or ../) to resolve symlinks and directory traversal.
    Non-path tokens are returned unchanged.

    Args:
        tokens: List of shell tokens (typically from normalize_shell_command).

    Returns:
        New list with path-like tokens resolved to their realpath.
    """
    resolved: list[str] = []
    for token in tokens:
        if _is_path_like(token):
            resolved.append(os.path.realpath(token))
        else:
            resolved.append(token)
    return resolved


def _is_path_like(token: str) -> bool:
    """Heuristic: does this token look like a filesystem path?"""
    if not token:
        return False
    # Absolute path
    if token.startswith("/"):
        return True
    # Home-relative (already expanded, but handle edge cases)
    if token.startswith("~"):
        return True
    # Relative with explicit directory prefix
    if token.startswith("./") or token.startswith("../"):
        return True
    # Contains path separator and has directory component (not a flag)
    if "/" in token and not token.startswith("-"):
        # Exclude URLs (http://, https://, etc.)
        if "://" in token:
            return False
        return True
    return False


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
_IP_CANDIDATE_RE = re.compile(
    r"(?:"
    r"::ffff:[0-9a-fA-Fx.:]+|"  # IPv6-mapped
    r"[0-9a-fA-F]{1,4}:[0-9a-fA-F:]{2,}|"  # native IPv6 literal (colon run, e.g. fd00:ec2::254)
    r"0[xX][0-9a-fA-F]+(?:\.[0-9a-fA-Fx]+)*|"  # Hex (with possible dotted)
    # inet_aton "short" forms the OS resolver / curl accept (a.b.c and a.b),
    # where the trailing component packs the remaining low-order bytes. These
    # must be captured WHOLE (not just the tail) so canonicalize_ip can resolve
    # them and catch an IMDS SSRF hidden in a 2-/3-part encoding. Listed before
    # the bare-integer / dotted-quad alternatives so the full token wins.
    r"\d{1,3}\.\d{1,3}\.(?:0[xX][0-9a-fA-F]+|\d{4,10})|"  # 3-part: a.b.c
    r"\d{1,3}\.(?:0[xX][0-9a-fA-F]+|\d{5,10})|"  # 2-part: a.b
    r"\d{7,10}|"  # Large decimal (single integer IP)
    r"(?:0[0-7]+\.){3}0[0-7]+|"  # Octal dotted
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"  # Standard dotted-quad
    r")"
)

_IMDS_IP = "169.254.169.254"
# Native IPv6 IMDS endpoint (dual-stack EC2). The IPv4 gate above misses this
# because canonicalize_ip returns native IPv6 unchanged; mirrors embeddings.py's
# SSRF gate which also blocks it (CWE-918 dual-stack parity).
_IMDS_IPV6 = "fd00:ec2::254"

# HTTP tools that can fetch IMDS -- broader than just curl/wget
_HTTP_TOOLS_RE = re.compile(
    r"(?:curl|wget|http|https|fetch|lwp-request|lynx|links|"
    r"python|ruby|perl|node|nc|ncat|socat|telnet|"
    r"Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b",
    re.IGNORECASE,
)


def _check_imds_access(command: str) -> str | None:
    """Detect attempts to access the IMDS endpoint via any encoding.

    Returns denial reason if IMDS access detected, None otherwise.
    """
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
    # env / printenv / export -p piped through grep for AWS_ vars
    re.compile(
        r"(?:env|printenv|export\s+-p|set)\s*(?:\|.*)?(?:grep|awk|sed)\s+.*AWS_",
        re.IGNORECASE,
    ),
    # Direct printenv of sensitive vars
    re.compile(
        r"printenv\s+AWS_(?:SECRET_ACCESS_KEY|SESSION_TOKEN|SECURITY_TOKEN)",
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


def _check_env_credential_access(command: str) -> str | None:
    """Detect attempts to read AWS credentials from environment variables.

    Returns denial reason if env credential access detected, None otherwise.
    """
    for pattern in _ENV_CRED_PATTERNS:
        if pattern.search(command):
            return "Blocked: command reads AWS credentials from environment variables"
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
    inherited" and is dropped here.
    """
    limits = dict(_RLIMIT_DEFAULTS)
    if config and isinstance(config.get("resource_limits"), dict):
        rl_config = config["resource_limits"]
        for key in _RLIMIT_DEFAULTS:
            val = rl_config.get(key)
            # Accept 0 (explicit disable) and positive ints; ignore junk.
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
                limits[key] = int(val)

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
