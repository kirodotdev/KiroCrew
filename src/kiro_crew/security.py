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
        pattern=".*kirocrew.*token",
        category="credential-exfil",
        description=(
            "Blocks the `kirocrew ... token` CLI, which mints a signed dashboard access token "
            "an attacker could use to authenticate to the gateway."
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
        pattern=".*cat.*/\\.kirocrew/\\.env.*",
        category="sensitive-file-read",
        description=(
            "Blocks using cat to read ~/.kirocrew/.env, which holds KiroCrew's own secrets and "
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
        pattern=".*\\b(kill|pkill|killall)\\b.*\\bkiro[-.]?crew\\b.*",
        category="self-protection",
        description=(
            "Blocks kill/pkill/killall targeting a kirocrew process so the agent cannot "
            "terminate its own gateway or supervisor and disable the controls governing it."
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
    ".kirocrew/.env",
    # Browser-auth cookie jar + the Playwright storage-state it is converted into
    # both hold reusable authenticated-session cookies (the OSS browser-auth stub
    # reads a Netscape cookie jar the user exports; ``browser/setup.py`` writes the
    # storage state 0600). Agent file tools must not read them through the shared
    # gate, or a prompt-injected turn could exfiltrate live browser sessions. The
    # browser helpers open these paths directly (not via is_sensitive_path), so
    # legitimate cookie injection still works.
    ".kirocrew/browser-cookies.txt",
    ".kirocrew/playwright-storage-state.json",
    # Security Event Log trust root. The SEL is a
    # tamper-evident, HMAC-SHA256-chained audit trail (``sel.py``): each entry
    # signs over the previous entry's hash using the key in ``sel_hmac.key``.
    # An audited agent that could fs_read the key could forge/rewrite the entire
    # chain and erase evidence of its own actions, and one that could read/rewrite
    # ``security_events.jsonl`` could tamper with the log directly. Both are the
    # governed subject's own audit root, so — like ``.kirocrew/.env`` — they must
    # never be readable OR writable through the shared file gate
    # (``is_sensitive_path``). The gateway's own SEL writer/reader (``sel.py``,
    # ``session_health.py``) opens these files directly and does NOT route through
    # this gate, so legitimate audit logging still works. (Deeper hardening — an
    # out-of-process signer that never exposes the key to the agent's namespace —
    # is documented future work; this closes the read/rewrite path.)
    ".kirocrew/sel_hmac.key",
    ".kirocrew/security_events.jsonl",
    # App admission trust root (keystone invariant). ``apps/admission.py`` loads
    # ``config_dir()/app_admission.json`` as the SOLE fleet-controlled App Kit
    # admission ceiling (kill-switch / allowlist / require_signature) — the one
    # thing an app can never source itself. An absent file falls open (admit), so
    # a governed agent that could fs_write/tee/rm this file could delete or neuter
    # its own admission ceiling and re-admit a banned/unsigned app. Like the SEL
    # trust roots above, it is the governed subject's own ceiling and must never be
    # readable OR writable through the shared file gate (``is_sensitive_path``).
    # The gateway's own loader (``apps.admission.load_app_admission_policy``) opens
    # it directly via ``path.read_text`` and does NOT route through this gate, so
    # legitimate policy loading still works.
    ".kirocrew/app_admission.json",
    # Governance trust-root files (KEYSTONE of the two-level governance model).
    # Under "secure by default, not by mandate" the ONLY thing preventing a
    # prompt-injected agent from rewriting its own ceiling is that it cannot
    # WRITE these files — and is_sensitive_path() is the shared read+write gate
    # (hooks.on_tool_call, validate_file_path, artifacts) across every surface.
    # The agent (the governed subject) is blocked; the operator (trust root)
    # edits them out-of-band.  admission_policy.json is the existing plugin
    # trust root; security_policy.json + profiles/ are the new governance ones.
    ".kirocrew/security_policy.json",
    ".kirocrew/profiles",
    ".kirocrew/admission_policy.json",
    # Denied-command opt-out state (KEYSTONE). ``denied_commands.json`` holds the
    # user's opt-out from the built-in deny ceiling
    # (``{disable_all, disabled_ids, user_added}``). It is a security ceiling: if
    # an auto-approved / YOLO agent shell could WRITE it, it could set
    # ``disable_all=true`` and, after a restart, defeat the whole deny gate. It
    # therefore lives OUTSIDE the agent-readable ``config.json`` and on this
    # read+write floor, so the agent (the governed subject) can neither read nor
    # write its own ceiling via ANY shell form — inheriting the full
    # ``is_sensitive_path`` gate (variable-indirection, symlinks, KIROCREW_HOME,
    # interpreters, casefold, realpath) instead of a bespoke bash matcher. The
    # operator edits it out-of-band via the dashboard ``/api/security/…``
    # endpoints, which open the file directly and do NOT route through this gate,
    # so legitimate opt-out changes still work.
    ".kirocrew/denied_commands.json",
    # KiroCrew's own dashboard-auth secrets. ``token_signing.key``
    # (dashboard/token_secret.py) signs every access + refresh token;
    # ``refresh_chains.json`` (dashboard/refresh_tokens.py) stores refresh-token
    # chain state; ``.local_secret`` (server.py / cli_commands.py / mcp_core.py /
    # cron_script.py / mcp_shared.py) is the shared internal-auth secret used to
    # authenticate MCP/cron/hook callbacks back into the gateway. These are this
    # host's own crown-jewel credentials: like the SEL trust root (sel_hmac.key),
    # the app-admission root, and the governance security_policy.json above, an
    # agent that could fs_read them could forge dashboard auth tokens or
    # impersonate internal callers. All legitimate readers (token_secret.py,
    # refresh_tokens.py, cli_commands.py, mcp_core.py, cron_script.py,
    # mcp_shared.py, mcp_playwright_proxy.py, cli_server.py, mcp_cron.py) open
    # these files directly via ``Path.read_text()``/``open()`` and do NOT route
    # through this gate, so legitimate token minting/verification still works.
    ".kirocrew/token_signing.key",
    ".kirocrew/refresh_chains.json",
    ".kirocrew/.local_secret",
    # Runtime exec dir. ``run/`` holds paths that the gateway executes OUTSIDE the
    # agent sandbox: the sandbox launcher scripts (``sandbox.py`` execs
    # ``python ~/.kirocrew/run/kirocrew_sandbox_*.py``) and the remote-instance
    # run-marker ``gateway-<port>.bin`` (``instances/run_marker.py``), whose
    # contents the SSH token-mint reads and ``exec``s on the remote host. A
    # prompt-injected / sandboxed agent that could WRITE into this dir could point
    # a marker (or a launcher) at an attacker-controlled binary and, on the next
    # routine token refresh, get it executed unsandboxed — a reachable sandbox
    # escape (owner + ``-x`` checks don't help; agent writes run as the same user).
    # Classify the whole dir read+write, like the other trust roots above. The
    # gateway's own writers (``run_marker.write_marker``, the sandbox launcher
    # writer) open these paths directly and do NOT route through this gate, so
    # legitimate startup/spawn writes still work.
    ".kirocrew/run",
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
# above, so no bash-level write matcher is needed for it.)
_WRITE_PROTECTED_HOME_PATHS: list[str] = [
    ".kirocrew/config.json",
    ".kirocrew/config.local.json",
]

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
    return re.compile(
        # (1) verb/redirect-anchored, OR (2) verb-independent: the sensitive path
        # appears anywhere as a token.  The token anchor accepts start-of-string
        # plus the separators that precede a path argument: whitespace, quote,
        # ``=`` (VAR=path), AND ``:``/``,``/``;`` (option:path, PATH-style
        # colon lists, comma/semicolon-joined args) — without the latter a
        # ``FOO=bar:~/.aws/credentials`` or ``PATH=/x:~/.ssh/id_rsa`` token slips
        # past the backstop while no verb branch fires either.
        rf"(?:(?:{_READ_CMDS}.*|{_WRITE_CMDS}.*|{_SCRIPT_OPEN}.*|.*[<>|]\s*)"
        rf"{sensitive_path}"
        rf"|(?:^|.*[\s'\"=:,;]){sensitive_path})",
        re.IGNORECASE,
    )


_SENSITIVE_RE: re.Pattern[str] | None = None


def _get_sensitive_re() -> re.Pattern[str]:
    global _SENSITIVE_RE
    if _SENSITIVE_RE is None:
        _SENSITIVE_RE = _build_sensitive_regex()
    return _SENSITIVE_RE


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

    try:
        home = str(Path.home().resolve())
    except (OSError, ValueError):
        home = str(Path.home())
    # Compare against the sensitive dirs anchored at BOTH the logical home and
    # its realpath.  On macOS the per-user temp/home prefix can itself be
    # reached via OS symlinks (``/var`` → ``/private/var``); folding both roots
    # in means a resolved candidate under either spelling is still matched.
    sensitive_targets: set[str] = {os.path.join(home, d).casefold() for d in home_dirs}
    home_real = os.path.realpath(home)
    if home_real.casefold() != home.casefold():
        sensitive_targets |= {os.path.join(home_real, d).casefold() for d in home_dirs}
    # When KIROCREW_HOME points to a non-default path, the keystone secrets
    # (token_signing.key, refresh_chains.json, .local_secret, sel_hmac.key,
    # security_policy.json etc.) live there — NOT under ~/.kirocrew. Without
    # this expansion any ".kirocrew/X" entry in the home_dirs list would miss
    # the real file location, letting the agent read/write its own signing key
    # or governance ceiling via the custom KIROCREW_HOME. Expand each entry
    # prefixed with ".kirocrew" using the env-override root ADDITIONALLY (the
    # ~/~ root stays so both locations are always covered).
    kiro_home_env = os.environ.get("KIROCREW_HOME")
    if kiro_home_env:
        try:
            kiro_home = str(Path(kiro_home_env).expanduser().resolve())
        except (OSError, ValueError):
            kiro_home = os.path.abspath(os.path.expanduser(kiro_home_env))
        _kiro_prefix = ".kirocrew" + os.sep
        for d in home_dirs:
            if d.startswith(_kiro_prefix) or d == ".kirocrew":
                tail = d[len(".kirocrew") :]  # includes leading / or is ""
                full = os.path.join(kiro_home, tail.lstrip(os.sep)) if tail else kiro_home
                sensitive_targets.add(full.casefold())
                # Also add the resolved form in case the env value itself has
                # symlinks (matches the home/home_real duality above).
                try:
                    sensitive_targets.add(os.path.realpath(full).casefold())
                except (OSError, ValueError):
                    pass

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


# Archive/extraction destination flags (tar -C, unzip -d, rsync dest) pointing
# INTO the governance trust-root parent ``~/.kirocrew`` — an extraction there can
# drop/overwrite ``security_policy.json`` or a ``profiles/`` entry even though the
# bare ``~/.kirocrew`` dir is not itself a sensitive-path entry.  Match the
# destination-dir form specifically so normal ``~/.kirocrew`` access (sessions.db,
# config.json) is not over-blocked.
_EXTRACT_INTO_TRUST_ROOT_RE = re.compile(
    r"-(?:C|d)\s+(?:~|\$HOME|/home/[^/\s]+|/Users/[^/\s]+|"
    + re.escape(str(Path.home()))
    + r")/\.kirocrew(?:/[^\s]*)?(?:\s|$|['\"])",
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

    # Check if any token resolves to a known read verb (by basename, so
    # /usr/bin/cat is recognized as "cat").
    has_read_verb = False
    for token in tokens:
        if not token:
            continue
        basename = os.path.basename(token).lower()
        if basename in _NORMALIZER_READ_VERBS:
            has_read_verb = True
            break

    if not has_read_verb:
        return None

    # Route each path-like token through is_sensitive_path()
    for token in tokens:
        if not token:
            continue
        # Skip flags
        if token.startswith("-"):
            continue
        # Skip tokens that ARE the read verb itself
        basename = os.path.basename(token).lower()
        if basename in _NORMALIZER_READ_VERBS:
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
    domain: str, path_and_query: str, exempt_hosts: frozenset[str]
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
    if domain.lower() not in exempt_hosts:
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
    # JWT (JWS) is three `.`-separated base64url segments (header.payload.sig) and
    # an encrypted JWT (JWE, RFC 7516) is five (header.key.iv.ciphertext.tag), so
    # the segment quantifier accepts `(?:\.[A-Za-z0-9_-]*){2,4}` further segments
    # after the header to redact BOTH shapes as one token. Post-header segments use
    # `*` (not `+`) so an EMPTY segment still counts: a compact JWE with direct
    # (`alg:dir`) or key-agreement (`ECDH-ES`) key management has an empty Encrypted
    # Key (2nd) segment — shape `header..iv.ciphertext.tag` — which a `+` quantifier
    # would fail to match, leaking the ciphertext + tag. The `.` separators are
    # still required, so bare `eyJson`-style prose (no dots) is not over-redacted.
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
    preprocessed = _HOME_VAR_RE.sub(home, cmd)

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
# Uses POSIX resource limits (setrlimit); see docs/resource-protection.md.

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
# (see docs/resource-protection.md); the ticket itself lists cgroup v2 as the
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

    limits = dict(_RLIMIT_DEFAULTS)
    if config and isinstance(config.get("resource_limits"), dict):
        rl_config = config["resource_limits"]
        for key in _RLIMIT_DEFAULTS:
            val = rl_config.get(key)
            # Accept 0 (explicit disable) and positive ints; ignore junk.
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
                limits[key] = int(val)

    # (rlimit constant, requested soft/hard value in the rlimit's native unit).
    # A value of 0 means "leave inherited" and is skipped below.
    max_memory_bytes = limits["max_memory_mb"] * 1024 * 1024
    specs = [
        ("RLIMIT_NPROC", limits["max_processes"]),
        ("RLIMIT_NOFILE", limits["max_open_files"]),
        ("RLIMIT_CPU", limits["max_cpu_seconds"]),
        ("RLIMIT_AS", max_memory_bytes),
    ]
    # Resolve the rlimit constants once in the parent (cheap, keeps the
    # post-fork callable minimal). Skip any this platform lacks.
    resolved = [
        (getattr(res, name), value) for name, value in specs if value > 0 and hasattr(res, name)
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
