"""Argparse definitions for the ``Security and privacy``, ``Move it and back it up`` and
``Develop Kiro Crew itself`` commands.

One registrar per command of those ``cli_help.COMMAND_GROUPS`` sections: secrets,
security, file-delivery, policy, telemetry, cloud, tailnet, snapshot, restore, pod and
eval (``bench``, ``perf`` and ``desktop`` register from their own modules).
``kiro_crew.cli.main`` builds the top-level parser and calls every registrar in the command
line's registration order, so a new command of these sections gets a registrar here and a
call there.

Parser construction only: the handlers live where ``main`` dispatches them, and nothing
here reads a name a test patches through ``kiro_crew.cli``
(``test/test_cli_split_facade.py`` pins that).
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from kiro_crew import cli_help
from kiro_crew.cli_cloud import add_size_choices as _cloud_size_choices

if TYPE_CHECKING:
    _Commands = argparse._SubParsersAction[argparse.ArgumentParser]

# Helper for commands with examples
_fmt = argparse.RawDescriptionHelpFormatter


def register_snapshot_parser(sub: _Commands) -> None:
    snap_parser = cli_help.add_command(sub, "snapshot")
    snap_parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="Directory to write the snapshot into (default: the data home's snapshots dir)",
    )
    snap_parser.add_argument("--keep", type=int, default=7, help="Keep N most recent snapshots")
    snap_parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit 3 instead of 0 when the bundle was written but omits entries it was "
            "asked to carry (see MANIFEST.json 'skipped'). The bundle is kept."
        ),
    )
    snap_parser.add_argument(
        "--list", action="store_true", dest="list_snapshots", help="List existing snapshots"
    )
    snap_parser.add_argument(
        "--allow-unpinned-staging",
        action="store_true",
        dest="allow_unpinned",
        help=(
            "Stage by path name on a platform that cannot open a directory relative to "
            "a descriptor (Windows). Without this the snapshot is refused there rather "
            "than taken with a traversal an ancestor swap could redirect. The archive's "
            "MANIFEST.json records that it was staged unpinned."
        ),
    )
    snap_parser.add_argument(
        "--components",
        default=None,
        help="Comma-separated components to include (default: all); see restore --list-components",
    )
    snap_parser.add_argument(
        "--purpose",
        default="backup",
        choices=("backup", "share"),
        help=(
            "backup = restoring onto a host you control, keeps credential-bearing "
            "components (the default, and the only one that produces a bundle today); "
            "share = leaves your control, so it carries only components certified free "
            "of credential material — no component is certified yet, so this currently "
            "refuses rather than emitting a bundle that has not been checked"
        ),
    )

    snap_parser.add_argument(
        "--max-archive-gib",
        type=int,
        default=None,
        dest="max_archive_gib",
        help=(
            "Set the bound on a bundle's uncompressed content in GiB (default 4, at most "
            "1024) for this run only. A bundle written past the default restores with a "
            "--max-archive-gib at least as large on restore."
        ),
    )

    # Retired, and defined only so it fails with a pointer instead of a bare argparse
    # error. Dropping it entirely would still fail loudly ("unrecognized arguments"), but
    # an operator whose muscle memory or old cron line still says `--to s3://bucket/prefix`
    # is better served by being told where that capability went than by exit 2.
    snap_parser.add_argument("--to", default=None, help=argparse.SUPPRESS)


def register_restore_parser(sub: _Commands) -> None:
    rest_parser = cli_help.add_command(sub, "restore")
    rest_parser.add_argument("snapshot", nargs="?", help="Path to snapshot .tar.gz")
    rest_parser.add_argument(
        "--mode",
        choices=("replace", "merge"),
        help="replace = overwrite existing state, merge = keep it and add (default: auto-detect)",
    )
    rest_parser.add_argument(
        "--dry-run", action="store_true", help="Preview what would be restored, write nothing"
    )
    rest_parser.add_argument("--components", help="Comma-separated components to restore")
    rest_parser.add_argument(
        "--list-components", action="store_true", help="List restorable component names and exit"
    )
    rest_parser.add_argument(
        "--force", action="store_true", help="Restore even if gateway is running"
    )
    rest_parser.add_argument(
        "--allow-unpinned-staging",
        action="store_true",
        dest="allow_unpinned",
        help=(
            "Restore by path name on a platform that cannot open a directory relative "
            "to a descriptor (Windows). Without this the restore is refused there "
            "rather than run with a destination an ancestor swap could redirect."
        ),
    )
    rest_parser.add_argument(
        "--max-archive-gib",
        type=int,
        default=None,
        dest="max_archive_gib",
        help=(
            "Set the bound on a bundle's uncompressed content in GiB (default 4, at most "
            "1024) for this run only. A bundle written past the default restores with a "
            "--max-archive-gib at least as large on restore."
        ),
    )


def register_security_parser(sub: _Commands) -> None:
    sec_parser = cli_help.add_command(sub, "security")

    sec_sub = sec_parser.add_subparsers(dest="sec_action")
    sec_sub.add_parser("audit", help="Scan conversation history for suspicious tool usage")
    sec_sub.add_parser("deny-list", help="Show active deny patterns")
    sel_parser = sec_sub.add_parser("events", help="Show recent security event log entries")
    sel_parser.add_argument("-n", "--limit", type=int, default=20, help="Number of entries")
    # A count alone cannot express "what happened around 14:05" — on a busy log
    # 6000 entries reach only 15 minutes back, so answering a question about a
    # two-hour-old event means pulling ~90k entries and filtering by hand.
    # Reading the file directly is correctly refused by the
    # credential-path gate, so the time selector has to live here.
    _time_help = (
        "a relative age (30m, 2h, 7d) or an ISO 8601 instant " "(2026-08-21, 2026-08-21T04:00:00Z)"
    )
    sel_parser.add_argument("--since", default="", help=f"Only entries at or after {_time_help}")
    sel_parser.add_argument("--until", default="", help=f"Only entries before {_time_help}")
    sec_sub.add_parser("verify", help="Verify security event log HMAC integrity")


def register_eval_parser(sub: _Commands, _jail_opts: argparse.ArgumentParser) -> None:
    # eval (benchmark harness)
    eval_parser = cli_help.add_command(
        sub,
        "eval",
        epilog="""
Examples:
  kirocrew eval                         # smoke test (~30s)
  kirocrew eval memory_recall_basic     # specific scenario
  kirocrew eval --all                   # all scenarios (slow)
""",
        formatter_class=_fmt,
        parents=[_jail_opts],
    )
    eval_parser.add_argument(
        "scenarios",
        nargs="*",
        default=[],
        help="Scenario names to run (without extension). Default: smoke_test",
    )
    eval_parser.add_argument(
        "--all", action="store_true", dest="all_scenarios", help="Run all scenarios"
    )
    eval_parser.add_argument("--judge", action="store_true", help="Enable LLM judge scoring")


def register_tailnet_parser(sub: _Commands) -> None:
    tn_parser = cli_help.add_command(sub, "tailnet")
    tn_sub = tn_parser.add_subparsers(dest="tailnet_action")
    for _tn_name, _tn_help in (
        ("status", "Show whether the dashboard is published and trusted on your tailnet"),
        ("up", "Publish the dashboard on your tailnet and trust its origin"),
        ("down", "Stop publishing the dashboard on your tailnet"),
    ):
        _tn = tn_sub.add_parser(_tn_name, help=_tn_help)
        # The escape hatch for when discovery cannot decide. Publishing has to front
        # the port the gateway is ACTUALLY on: if the run marker is unreadable (or
        # several gateways are up, where it deliberately refuses) the fallback is the
        # configured port -- which, if the gateway moved because that port was taken,
        # now belongs to some OTHER local service that would get exposed. Naming the
        # port outranks every heuristic.
        _tn.add_argument(
            "--port",
            type=int,
            default=None,
            help="Dashboard port to publish (default: discover the running gateway)",
        )


def register_telemetry_parser(sub: _Commands) -> None:
    tel_parser = cli_help.add_command(sub, "telemetry")
    tel_sub = tel_parser.add_subparsers(dest="telemetry_action")
    tel_sub.add_parser(
        "status", help="Show exactly what the anonymous beacon sends (and whether it will)"
    )
    tel_sub.add_parser("disable", help="Turn the anonymous beacon off permanently")
    tel_sub.add_parser("enable", help="Turn the anonymous beacon back on")


def register_policy_parser(sub: _Commands) -> None:
    # policy — governance model inspection (read-only; MCP-safe)
    policy_parser = cli_help.add_command(sub, "policy")
    policy_sub = policy_parser.add_subparsers(dest="policy_action")
    policy_show = policy_sub.add_parser(
        "show", help="Show the effective enterprise security policy"
    )
    policy_show.add_argument(
        "--ids",
        action="store_true",
        # NOT named --verbose: the top-level parser already defines --verbose/-v
        # as an int `count` (log level). A same-named store_true on this
        # subparser would collide in the merged Namespace via argparse's
        # parent/subparser default-override gotcha (see the --no-jail comment
        # in cli.main) -- whichever parser's default applies last silently
        # overwrites the other's value.
        help="List each denied-command category's rule ids (default: counts only)",
    )
    policy_sub.add_parser("validate", help="Validate the policy + all profiles (load-check)")
    explain_parser = policy_sub.add_parser(
        "explain", help="Explain a tool/scope decision for a surface"
    )
    explain_parser.add_argument("scope", help="Governed scope, e.g. 'commands' or 'mcp'")
    explain_parser.add_argument("item", help="The item to evaluate, e.g. 'git push origin'")
    explain_parser.add_argument(
        "--session-key", default="cli_chat", help="Surface session key (default: cli_chat)"
    )
    explain_parser.add_argument("--agent", default="", help="Agent name (optional)")
    explain_parser.add_argument("--app", default="", help="App slug (optional)")
    profile_show = policy_sub.add_parser("profile", help="Show a profile by name")
    profile_show.add_argument("name", help="Profile file stem (without .json)")
    policy_sub.add_parser(
        "source", help="Show whether this host fetches its policy from a central source"
    )
    policy_fetch = policy_sub.add_parser(
        "fetch", help="Fetch the central policy now and apply it if it is usable"
    )
    policy_fetch.add_argument(
        "--force",
        action="store_true",
        # Skips the conditional-request validators. An operator running this by
        # hand wants to prove the round trip end to end; a 304 tells them nothing
        # about whether the document they just published reads correctly.
        help="Ignore the cached validators and re-download the whole document",
    )


def register_secrets_parser(sub: _Commands) -> None:
    # secrets — encrypted vault maintenance. Only the migration importer lives
    # here; the set/list/rm surface is owned by a separate change.
    secrets_parser = sub.add_parser("secrets", help="Encrypted secret vault")
    secrets_sub = secrets_parser.add_subparsers(dest="secrets_action")
    secrets_import = secrets_sub.add_parser(
        "import",
        help="Migrate plaintext .env credentials into the vault (dry-run unless --apply)",
    )
    secrets_import.add_argument(
        "--apply",
        action="store_true",
        help="Store the secrets and rewrite .env to secret:// refs (default: dry-run)",
    )


def register_pod_parser(sub: _Commands) -> None:
    # pod — isolated, throwaway, full-stack test instances per worktree (kubectl-style)
    pod_parser = cli_help.add_command(sub, "pod")
    pod_sub = pod_parser.add_subparsers(
        dest="pod_action",
        metavar="{up,down,ls,prune,status,token,url,scenarios,api,logs,exec,provision,install}",
    )
    pod_up = pod_sub.add_parser("up", help="Schedule an isolated pod for a worktree")
    pod_up.add_argument("name", help="Worktree name")
    pod_up.add_argument("--json", action="store_true", help="Emit {base_url, token, port} as JSON")
    pod_up.add_argument(
        "--provision",
        action="store_true",
        help="Provision (venv + SPA dist) if needed before bringing the pod up",
    )
    pod_up.add_argument("--ttl", default="2h", help="Token TTL (default: 2h)")
    pod_up.add_argument(
        "--no-token",
        dest="no_token",
        action="store_true",
        help=(
            "Boot the pod but do NOT mint a dashboard token: `token` in the "
            "--json handle is empty and no /api/token/local call is made. The "
            "gateway's agent pod surface uses this so it can mint in-process "
            "instead (see agent_pod_api); a sandboxed `pod up` child is in its "
            "own user namespace and the pod refuses to certify it as the local "
            "owner. A human running `pod up` should omit this and let the CLI "
            "mint, then `pod token` to re-mint."
        ),
    )
    pod_up.add_argument(
        "--seed",
        default="",
        help=(
            "Pre-populate the isolated home. A bare NAME is a shipped seed scenario "
            "and populates the whole home; unknown names are refused with the "
            "available list. A PATH (a separator or leading ~ or .) contributes only "
            "its sanitized config.json. Populated homes are never re-seeded."
        ),
    )
    pod_up.add_argument(
        "--approval",
        # Literal mirrors kiro_crew.pod.runtime.APPROVAL_MODES, which is the
        # enforcement point; this parser imports no pod module at startup.
        choices=["reads", "yolo", "interactive"],
        help=(
            "Approval mode the pod's gateway boots with, forwarded to "
            "`kirocrew gateway --approval`. Persisted per pod so it survives a "
            "service-manager restart. Omit to leave the gateway's own default in "
            "force, which resolves from config agent.approval_mode (default: "
            "auto). Applies at boot, so re-up a stopped pod to change it."
        ),
    )
    pod_up.add_argument(
        "--crons",
        action="store_true",
        help=(
            "Run the pod's cron scheduler. Pods boot with --no-crons by default. "
            "Without --seed the HOME starts with no cron definitions; a named "
            "scenario may provide them. Persisted per pod; applies at boot."
        ),
    )
    pod_up.add_argument(
        "--no-embeddings",
        dest="no_embeddings",
        action="store_true",
        help=(
            "Boot without the embedding model: the pod never downloads it and "
            "memory/knowledge search falls back to keyword matching (a documented "
            "mode). Use it to load-test ingestion without paying per-chunk embed "
            "compute. Affects the pod's env only, never your own home. Persisted "
            "per pod as EMBEDDINGS='0' in its env file (hand-edit that line to flip "
            "a kept pod); applies at boot. The switch is subsystem-wide, so the pod "
            "skips its speech-to-text (whisper) model download too."
        ),
    )
    pod_up.add_argument(
        "--wait-secs",
        dest="wait_secs",
        type=int,
        metavar="SECS",
        default=None,
        help=(
            "Health-wait budget in seconds before `pod up` gives up on the "
            "gateway answering /api/health (default: 90; a first boot does "
            "migration and staging work, so slow hosts may need more). Also "
            "settable via KIROCREW_POD_HEALTH_SECS; the flag wins. Values "
            "below 5 are raised to 5, values above 3600 are capped at 3600."
        ),
    )
    pod_down = pod_sub.add_parser("down", help="Evict a pod (zero residue)")
    pod_down.add_argument("name", help="Worktree name")
    pod_ls = pod_sub.add_parser("ls", help="List running pods")
    pod_ls.add_argument("--json", action="store_true", help="Emit rows as JSON")
    pod_prune = pod_sub.add_parser(
        "prune", help="Reclaim orphaned pod HOMEs in bulk (the N-at-once `pod down`)"
    )
    pod_prune.add_argument(
        "--older-than",
        dest="older_than",
        default="3d",
        help=(
            "Only reclaim orphans whose last activity is older than this "
            "(e.g. 3d, 12h, 30m, 45s). Default: 3d, so a freshly-crashed HOME "
            "an operator may still be debugging is kept. Use --all to reclaim "
            "regardless of age."
        ),
    )
    pod_prune.add_argument(
        "--all",
        dest="prune_all",
        action="store_true",
        help="Reclaim ALL orphans regardless of age (overrides --older-than)",
    )
    pod_prune.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Classify and print what would be reclaimed without deleting anything",
    )
    pod_prune.add_argument("--json", action="store_true", help="Emit per-name results as JSON")
    pod_status = pod_sub.add_parser("status", help="Up/down + health for one pod")
    pod_status.add_argument("name", help="Worktree name")
    pod_status.add_argument("--json", action="store_true", help="Emit status as JSON")
    pod_token = pod_sub.add_parser("token", help="(Re)mint a dashboard token for a running pod")
    pod_token.add_argument("name", help="Worktree name")
    pod_token.add_argument("--ttl", default="2h", help="Token TTL (default: 2h)")
    pod_url = pod_sub.add_parser("url", help="Print a pod's base URL")
    pod_url.add_argument("name", help="Worktree name")
    pod_scenarios = pod_sub.add_parser(
        "scenarios",
        help="List the seed scenarios `pod up --seed <scenario>` accepts",
    )
    pod_scenarios.add_argument("--json", action="store_true", help="Emit rows as JSON")
    pod_api = pod_sub.add_parser(
        "api",
        help="Call a running pod's HTTP API with its own token",
        description=(
            "Make one authenticated request against a running pod and print a "
            "fixed-key JSON document: {name, method, path, status, ok, body}. "
            "GET and HEAD are permitted by default; other methods require "
            "--allow-write."
        ),
        epilog=(
            "Examples:\n"
            "  kirocrew pod api my-wt GET sessions\n"
            "  kirocrew pod api my-wt GET /api/health\n"
            '  kirocrew pod api my-wt POST config --data \'{"key":"agent.model"}\' '
            "--allow-write\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pod_api.add_argument("name", help="Worktree name")
    pod_api.add_argument(
        "method",
        # No `choices=`: argparse would reject an unrecognised method with its own
        # usage prose on stderr and exit 2, escaping the fixed-key JSON envelope
        # this command promises on EVERY exit. `pod.runtime.pod_api` validates the
        # method against API_METHODS and raises PodError, which the envelope
        # reports as a status-0 document an agent can parse. `type=str.upper` stays
        # so a lowercase method still normalises.
        type=str.upper,
        metavar="METHOD",
        help="HTTP method: GET, HEAD, POST, PUT, PATCH or DELETE (case-insensitive)",
    )
    pod_api.add_argument(
        "path",
        help=(
            "API path; /api/ is prepended when absent. Existing query parameters "
            "are preserved, but caller-supplied token parameters are refused."
        ),
    )
    pod_api.add_argument(
        "--data",
        default="",
        help="Request body, sent verbatim as application/json",
    )
    pod_api.add_argument(
        "--allow-write",
        action="store_true",
        help="Permit POST, PUT, PATCH, or DELETE (off by default)",
    )
    pod_logs = pod_sub.add_parser("logs", help="Tail a pod's journal")
    pod_logs.add_argument("name", help="Worktree name")
    pod_logs.add_argument("-n", "--lines", type=int, default=50, help="Lines to tail (default: 50)")
    pod_exec = pod_sub.add_parser(
        "exec",
        help="Run a kirocrew command against a pod, using the pod's own binary and data",
    )
    pod_exec.add_argument("name", help="Worktree name")
    # REMAINDER so the pod's own flags (--json, -n, --ttl …) reach the child
    # instead of being claimed by this parser.
    pod_exec.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        metavar="-- ARGS",
        help="Command to run in the pod, e.g. `-- status` or `-- cron list`",
    )
    pod_prov = pod_sub.add_parser("provision", help="Build a worktree's venv + SPA dist")
    pod_prov.add_argument("name", help="Worktree name")
    pod_prov.add_argument(
        "--venv-only", action="store_true", help="Build only the venv (skip the slow dist)"
    )
    pod_sub.add_parser("install", help="Lay down the systemd --user template unit (once)")
    # Hidden verbs: `_run` is the unit's ExecStart body; `_cleanup` is the
    # single-name manual reclaim (teardown itself lives on the `down` path).
    # Registered without `help=` so they stay out of `pod --help` while remaining
    # dispatchable (the metavar above also omits them from the usage line).
    pod_run = pod_sub.add_parser("_run")
    pod_run.add_argument("name")
    pod_cleanup = pod_sub.add_parser("_cleanup")
    pod_cleanup.add_argument("name")


def register_file_delivery_parser(sub: _Commands) -> None:
    file_delivery_parser = cli_help.add_command(sub, "file-delivery")
    file_delivery_parser.add_argument(
        "action",
        choices=["approve"],
        help=(
            "approve: finish a flagged-file delivery consent armed from the "
            "dashboard's Security panel (proves you are at the host)"
        ),
    )


def register_cloud_parser(sub: _Commands) -> None:
    # cloud — provision + run Kiro Crew on the user's own AWS EC2 (bring-your-own
    # AWS; credentials resolved by the aws CLI, never stored by Kiro Crew).
    cloud_parser = cli_help.add_command(
        sub,
        "cloud",
        epilog="""
Examples:
  kirocrew cloud launch                  # interactive: provision + configure + open dashboard
  kirocrew cloud launch --size power     # non-interactive size
  kirocrew cloud launch --new            # create a separate new instance
  kirocrew cloud launch --subnet subnet-0abc…  # pin the launch to an exact subnet
  kirocrew cloud launch --extra-packages gh,jq # also install these dnf packages
  kirocrew cloud launch --ami ami-0abc…  # launch from your own AL2023-based image
  kirocrew cloud list                    # list your cloud instances
  kirocrew cloud connect                 # reopen the dashboard over SSM
  kirocrew cloud stop | start            # pause / resume (save cost)
  kirocrew cloud destroy                 # remove EVERYTHING from AWS
  kirocrew cloud iam-policy              # print the least-privilege IAM policy
  kirocrew cloud doctor                  # check prerequisites + AWS reachability
""",
        formatter_class=_fmt,
    )

    # --profile/--region are universal; --tag only applies to verbs that address ONE
    # instance. `list`, `iam-policy`, `iam-boundary`, and `doctor` are account-wide,
    # so they take the pair WITHOUT --tag via _cloud_creds_opts.
    def _cloud_creds_opts(p: "argparse.ArgumentParser") -> None:
        p.add_argument(
            "--profile", default="", help="AWS profile name (default: saved / CLI default)"
        )
        p.add_argument("--region", default="", help="AWS region (default: saved / us-east-1)")

    def _cloud_common(p: "argparse.ArgumentParser") -> None:
        _cloud_creds_opts(p)
        p.add_argument("--tag", default="", help="Instance tag (default: last launched)")

    def _cloud_identity_opts(p: "argparse.ArgumentParser") -> None:
        # ONE definition for every command that signs kiro-cli in on the crew.
        # `cloud login`, `cloud launch` and `kirocrew setup` (which delegates to
        # launch) all accept the same identity flags, so a managed launch can
        # name an Identity Center identity instead of defaulting to Builder ID.
        p.add_argument(
            "--identity-provider",
            default="",
            help="IAM Identity Center start URL (your organization's access portal) for enterprise SSO",
        )
        p.add_argument(
            "--license",
            default="",
            choices=["", "free", "pro"],
            help="Kiro license tier (pro for Identity Center, free for Builder ID/social)",
        )
        p.add_argument(
            "--idp-region",
            default="",
            help="IAM Identity Center region (e.g. us-east-1), NOT the EC2 instance region",
        )

    cloud_sub = cloud_parser.add_subparsers(dest="cloud_action")
    _c_launch = cloud_sub.add_parser("launch", help="Provision + configure an instance")
    _cloud_creds_opts(_c_launch)
    _c_launch.add_argument(
        "--size",
        default="",
        choices=_cloud_size_choices(),
        help="Instance size tier (default: balanced / interactive picker)",
    )
    _c_launch.add_argument(
        "--subnet",
        default="",
        metavar="SUBNET_ID",
        help="Launch into this exact subnet (subnet-xxxx) instead of network "
        "auto-discovery — required to target a dedicated/private-subnet VPC "
        "when a default VPC exists. The subnet must have internet egress "
        "(NAT or IGW route).",
    )
    _c_launch.add_argument(
        "--ami",
        default="",
        metavar="AMI_ID",
        help="Launch from this AMI (ami-xxxx) instead of the latest Amazon Linux 2023 "
        "image. It must match the size tier's architecture and be AL2023-compatible.",
    )
    _c_launch.add_argument(
        "--extra-packages",
        default="",
        metavar="PKGS",
        help='Extra dnf packages to install on the box, comma-separated (e.g. "gh,jq"). '
        "A package dnf cannot find is skipped with a warning in the setup log.",
    )
    _c_launch.add_argument(
        "--ssm-kms-key",
        default="",
        metavar="KEY_ARN",
        help="Full ARN of the KMS key your org pins for Session Manager encryption. "
        "The box may then decrypt its own session data with that key only. Without it "
        "the default boundary is unchanged and the box has no KMS access.",
    )
    _c_launch.add_argument("-y", "--yes", action="store_true", help="Accept defaults, no prompts")
    _c_launch.add_argument(
        "--new",
        action="store_true",
        help="Create a separate new instance instead of resuming the saved one",
    )
    _c_launch.add_argument(
        "--keep-on-failure",
        action="store_true",
        help="On bootstrap failure, keep the instance (disable rollback) for inspection",
    )
    _cloud_identity_opts(_c_launch)
    _c_launch.add_argument(
        "--no-inherit-identity",
        action="store_true",
        help="Do not inherit this machine's Kiro sign-in (kiro-cli whoami) as the "
        "crew's identity; sign the crew in with Builder ID unless --identity-provider is given",
    )

    _c_list = cloud_sub.add_parser("list", help="List your Kiro Crew cloud instances")
    _cloud_creds_opts(_c_list)

    _c_status = cloud_sub.add_parser("status", help="Show one instance's state")
    _cloud_common(_c_status)

    def _tunnel_opts(p: "argparse.ArgumentParser") -> None:
        _cloud_common(p)
        p.add_argument(
            "--local-port",
            type=int,
            default=0,
            help="Local port to forward the dashboard to (default: 5599)",
        )
        p.add_argument(
            "--no-browser",
            action="store_true",
            help="Open the tunnel but don't launch a browser",
        )

    _c_connect = cloud_sub.add_parser("connect", help="Open the dashboard over an SSM tunnel")
    _tunnel_opts(_c_connect)
    # `tunnel` — a clear standalone command to open the dashboard SSM tunnel,
    # independent of launch/setup (alias of connect).
    _c_tunnel = cloud_sub.add_parser(
        "tunnel", help="Open the dashboard SSM tunnel (standalone; alias of connect)"
    )
    _tunnel_opts(_c_tunnel)
    _c_login = cloud_sub.add_parser(
        "login", help="Sign kiro-cli in on the instance (fixes 'not logged in' chat errors)"
    )
    _cloud_common(_c_login)
    _c_login.add_argument(
        "--no-browser", action="store_true", help="Print the device URL but don't open a browser"
    )
    _cloud_identity_opts(_c_login)
    _c_logout = cloud_sub.add_parser(
        "logout", help="Sign kiro-cli out on the instance (to switch Kiro account)"
    )
    _cloud_common(_c_logout)
    _c_stop = cloud_sub.add_parser("stop", help="Stop the instance (pause billing)")
    _cloud_common(_c_stop)
    _c_start = cloud_sub.add_parser("start", help="Start a stopped instance")
    _cloud_common(_c_start)

    _c_destroy = cloud_sub.add_parser(
        "destroy", help="Remove the instance and ALL its AWS resources"
    )
    _cloud_common(_c_destroy)
    _c_destroy.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    _c_destroy.add_argument(
        "--dry-run", action="store_true", help="Show the delete command, don't run it"
    )

    _c_iam = cloud_sub.add_parser(
        "iam-policy", help="Print the least-privilege IAM policy to apply"
    )
    _cloud_creds_opts(_c_iam)
    _c_boundary = cloud_sub.add_parser(
        "iam-boundary",
        help="Pre-create the immutable EC2 + Fargate permissions boundaries (admin, one-time)",
    )
    _cloud_creds_opts(_c_boundary)
    _c_boundary.add_argument(
        "--ssm-kms",
        action="store_true",
        help="Also pre-create the opt-in KMS-session instance boundary that "
        "`cloud launch --ssm-kms-key` uses",
    )
    _c_doctor = cloud_sub.add_parser("doctor", help="Check cloud prerequisites + AWS reachability")
    _cloud_creds_opts(_c_doctor)
