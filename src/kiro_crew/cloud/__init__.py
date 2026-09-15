"""KiroCrew cloud launcher — provision + run KiroCrew on the user's own EC2.

The design is documented in ``docs/system-specs/modules/cloud.md``. The short
version:

- **Bring-your-own-AWS, store nothing.** Every AWS call shells to the ``aws``
  CLI with ``--profile`` (never boto3) through the single :func:`cloud.aws.run_aws`
  chokepoint, so credential resolution stays in the CLI's own provider chain.
  KiroCrew persists only a profile name, region, and stack name.
- **CloudFormation** provisions the whole resource stack (IAM role + instance
  profile, security group, EC2 instance, EBS volume, tags) in one atomic,
  rollback-safe, one-command-teardown deploy.
- **SSM Session Manager** is the default connectivity — no inbound ports, no SSH
  key files. SSH is an opt-out fallback.
- Provisioning is a **human/installer action, never an LLM tool** — the cloud
  verbs are not registered as MCP tools, and the destructive paths are blocked
  for the agent by the ``deniedCommands`` regexes in ``config/defaults.json``
  (enforced by kiro-cli's ``execute_bash``/``shell`` tools): both the raw CLI
  verbs (``aws ec2 terminate-instances``, ``aws ec2 delete-*``,
  ``aws cloudformation delete-stack``) **and** the ``kirocrew cloud
  destroy|stop|start|launch|connect|tunnel|login|logout`` wrappers (so the agent
  can't reach teardown/launch through the wrapper, mint+print a dashboard token
  via ``connect``/``tunnel``/``login``, nor sign its own box out via ``logout``).
  Only the read-only ``list``/``status``
  verbs stay agent-accessible. (The built-in deny catalog in
  ``kiro_crew.security`` also refuses the hyphenated CLI verbs through its
  ``aws-destructive`` rows; it carries no underscored SDK-method spellings such
  as ``terminate_instance``, so a boto3 call is not refused by command text.)

Module map:

- ``aws`` — the ``run_aws`` chokepoint + ``AccessDenied`` → action mapping.
- ``sizes`` — the size-tier catalog (constants; no magic numbers in logic).
- ``iam`` — least-privilege policy generator + read-only reachability check.
- ``ec2`` — deploy / status / stop / start / destroy via ``aws cloudformation``.
- ``connect`` — SSM port-forward + dashboard token + open browser.
- ``login`` — ``kiro-cli`` sign-in / sign-out on the instance over SSM.
- ``wizard`` — the interactive launch flow.
- ``templates/kirocrew-ec2.yaml`` — the CloudFormation template.
"""

from __future__ import annotations

from kiro_crew.cloud.aws import AWSError, run_aws

__all__ = ["AWSError", "run_aws"]
