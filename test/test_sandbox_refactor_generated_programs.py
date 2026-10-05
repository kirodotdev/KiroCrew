"""The two programs a spawn writes out, against a fully pinned host.

The Linux namespace launcher and the macOS Seatbelt profile are renderings of one
confinement plan (``kiro_crew.sandbox_plan``). Each is pinned here by ONE golden, taken
through the live host adapter (``kiro_crew.sandbox._live_plan_host``) with every host
input pinned so the golden does not depend on the machine that computes it: the run
root is ``tmp_path`` and is folded to ``<ROOT>``, and every table whose entries reach
either program is replaced by the small stand-in in ``_PINNED_TABLES``, so a leaf
added to a real table leaves these goldens alone.

* The launcher's golden is the PLAN DATA it carries -- its one substitution -- for a
  spawn that passes every kind of extra path and identity. The program text around it
  is the ``kiro_crew.sandbox_launcher_program`` module itself, which
  ``test_sandbox_launcher_program.py`` drives stage by stage.
* The profile's golden is its full text for a spawn with private windows.
* Every host reader and table the live host plans from is read off
  ``kiro_crew.sandbox`` when it runs, so a test that rebinds one there reaches the plan
  of either backend.

Both goldens are the output the builders produced before the plan existed, so they
also pin that the plan changed nothing a spawn sees. What each field means is pinned
in ``test_sandbox_plan.py``, as a table over ``plan_confinement``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from test_sandbox_launcher_program import rendered_payload

from kiro_crew import sandbox, sandbox_launcher, sandbox_launcher_program, sandbox_plan

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="both programs are POSIX-only: the launcher reads os.getuid and the paths are POSIX",
)

#: The launcher docstring line, spelled with the product name in two words.
_DOC_LINE = '"""Namespace sandbox launcher — spawned by Kiro Crew.'

_UID, _GID = 4242, 4343

#: Small stand-ins for every table whose entries reach either program, so a leaf, a
#: directory or an environment name added to the real tables leaves these digests alone.
#: Each keeps the shapes the real one has: nested leaves, both data-home spellings, the
#: policy cache and voice-runtime directories, a tier-only directory, an exposed file.
_HIDDEN_LEAVES = (
    ".env",
    "diag",
    "apps/aws-control/data",
    "workspace/md-notebook/pat",
    "workspace/md-notebook/vaults.json",
    "live_target.json",
    "crew-panels",
)
_READONLY_LEAVES = (
    "subagents",
    "security_policy.json",
    "profiles",
    "apps/.dev-grants.json",
    "mcp-launch-approvals",
    "mcp/resolved",
)
_CREW_SPELLINGS = (".kiro/crew", ".kirocrew")
_TIER_ONLY = {"strict": (".aws", ".config/gh", ".kube"), "cc": (".aws", ".kube")}
_SHARED_DIRS = (
    ".kiro/crew-auth-staging",
    ".gnupg",
    ".config/gcloud",
    *(f"{home}/{leaf}" for home in _CREW_SPELLINGS for leaf in (".vault", "policy_cache")),
    *(f"{home}/run/voice-runtime" for home in _CREW_SPELLINGS),
    *(f"{home}/{leaf}" for home in _CREW_SPELLINGS for leaf in _HIDDEN_LEAVES),
)
_PINNED_TABLES: dict[str, object] = {
    "_CREW_HIDDEN_LEAVES": _HIDDEN_LEAVES,
    "_CREW_READONLY_LEAVES": _READONLY_LEAVES,
    "_CREW_READONLY_TARGETS": [
        f"{home}/{leaf}" for home in _CREW_SPELLINGS for leaf in _READONLY_LEAVES
    ],
    "_CREW_UNREADABLE_MASK_LEAVES": frozenset({"live_target.json"}),
    "_STRICT_DIRS": [*_SHARED_DIRS, *_TIER_ONLY["strict"]],
    "_STANDARD_DIRS": list(_SHARED_DIRS),
    "_CC_DIRS": [*_SHARED_DIRS, *_TIER_ONLY["cc"]],
    "_CC_FILES": [".npmrc", ".netrc", ".kiro/crew/.env", ".kirocrew/.env"],
    "_CC_EXPOSE_FILES": [".aws/config"],
    "_POD_OS_HOME_MASKED_SUBLEAVES": (".aws/config", ".aws/credentials"),
    "_AGENT_DENIED_ENV_KEYS": ["SLACK_BOT_TOKEN", "JIRA_TOKEN_", "KIROCREW_POLICY_URL"],
    "_SENSITIVE_ENV_PREFIXES": ["AWS_SECRET", "SSH_AUTH_SOCK", "GIT_ASKPASS"],
    "_PYTHON_ENV_PREFIXES": ["PYTHONPATH", "PYTHONHOME"],
}


class _Host:
    """The pinned host a case renders against."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "op"
        self.crew = root / "crew"
        self.home.mkdir()
        self.crew.mkdir()

    def voice_cache(
        self, crew: Path
    ) -> tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        run = crew / "run"
        root = run / "voice-runtime"
        return (str(crew), str(root), (str(root),), (str(run),), (str(run), str(crew)))


def _pinned_host(root: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    """Pin every host input either builder reads under ``root``."""
    pinned = _Host(root.resolve())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: pinned.home))
    monkeypatch.setenv("HOME", str(pinned.home))
    for name in ("KIROCREW_POD", "KIROCREW_OS_HOME", "KIRO_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sandbox, "config_dir", lambda: pinned.crew)
    monkeypatch.setattr(sandbox, "kiro_agents_dir", lambda: pinned.home / ".kiro" / "agents")
    monkeypatch.setattr(sandbox, "carveout_chain_has_planted_link", lambda _path: False)
    monkeypatch.setattr(sandbox, "_voice_runtime_paths_cache", pinned.voice_cache(pinned.crew))
    monkeypatch.setattr(os, "getuid", lambda: _UID, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: _GID, raising=False)
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
    for name, table in _PINNED_TABLES.items():
        monkeypatch.setattr(sandbox, name, table)
    assert type(sandbox._sandbox_policy()).__name__ == "DefaultSandboxPolicy"
    return pinned


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    return _pinned_host(tmp_path, monkeypatch)


def _symlinked_home(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    lexical = host.root / "crew-link"
    lexical.symlink_to(host.crew, target_is_directory=True)
    monkeypatch.setattr(sandbox, "config_dir", lambda: lexical)
    lex_run, can_run = lexical / "run", host.crew / "run"
    lex_vr, can_vr = lex_run / "voice-runtime", can_run / "voice-runtime"
    monkeypatch.setattr(
        sandbox,
        "_voice_runtime_paths_cache",
        (
            str(lexical),
            str(can_vr),
            (str(lex_vr), str(can_vr)),
            (str(lex_run), str(can_run)),
            (str(lex_run), str(lexical), str(can_run), str(host.crew)),
        ),
    )


def _pod_home(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_POD", "1")
    monkeypatch.setenv("KIROCREW_OS_HOME", str(host.root / "podhome"))


def _no_accept_new(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: False)


def _planted_notebook_chain(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    crew = str(host.crew)
    monkeypatch.setattr(
        sandbox, "carveout_chain_has_planted_link", lambda p: str(p).startswith(crew)
    )


def _carveout(host: _Host) -> dict[str, Any]:
    probe = host.crew / "run" / "mcp-tmp" / "probe-x"
    probe.mkdir(parents=True)
    return {"extra_writable_dirs": (str(probe),)}


def _private_windows(host: _Host) -> dict[str, Any]:
    apps = host.crew / "apps"
    window = apps / "alpha" / "data"
    window.mkdir(parents=True)
    return {
        "extra_hidden_dirs": (str(apps),),
        "extra_private_dirs": (str(window), str(apps), str(host.crew / "elsewhere")),
        "extra_private_dir_ids": ((str(window), 11, 12),),
        "extra_hidden_dir_ids": ((str(apps), 13, 14),),
    }


def _identities(host: _Host) -> dict[str, Any]:
    masked = host.crew / "apps"
    return {
        "extra_hidden_dirs": (str(masked),),
        "extra_alias_credential_ids": ((7, 99), (7, 99), (8, 1)),
        "fail_closed_file_masks": (
            (str(host.home / ".npmrc"), 3, 4),
            (str(host.home / ".npmrc"), 3, 4),
        ),
        "required_mask_targets": (
            str(masked / "alpha"),
            str(host.root / "outside"),
            str(masked / "alpha"),
        ),
        "mask_occupants": {
            str(masked): (21, 22, 0),
            str(host.home / ".aws"): (23, 24, 1, 1, 25, 26),
        },
    }


def _crew_home_alias(host: _Host) -> dict[str, Any]:
    """``$HOME/.kiro/crew`` is the data home reached through a link: every path under
    it is folded onto the resolved spelling, and the alias travels with its identity."""
    return {"crew_home_aliases": ((str(host.home / ".kiro" / "crew"), str(host.crew), 31, 32),)}


def _expose(host: _Host) -> dict[str, Any]:
    return {"extra_expose_files": (str(host.home / ".aws" / "sso" / "cache" / "token.json"),)}


def _everything(host: _Host) -> dict[str, Any]:
    """Every kind of extra path and identity a caller passes, in one spawn."""
    merged: dict[str, Any] = {}
    for part in (_identities, _private_windows, _carveout, _expose, _crew_home_alias):
        for key, value in part(host).items():
            merged[key] = merged[key] + value if key in merged else value
    merged["extra_visible_dirs"] = (str(host.crew / "policy_cache"),)
    merged["strip_python_env"] = True
    merged["forward_ssh_auth_sock"] = True
    return merged


def _fold(value: Any, host: _Host) -> Any:
    """*value* with the run root spelled ``<ROOT>``."""
    if isinstance(value, str):
        return value.replace(str(host.root), "<ROOT>")
    if isinstance(value, list):
        return [_fold(item, host) for item in value]
    if isinstance(value, dict):
        return {_fold(k, host): _fold(v, host) for k, v in value.items()}
    return value


#: The plan data the launcher carries for ``_everything`` at the strict tier in a pod.
_LAUNCHER_PLAN_GOLDEN: dict[str, Any] = {
    "real_uid": 4242,
    "real_gid": 4343,
    "sensitive_dirs": [
        "<ROOT>/op/.kiro/crew-auth-staging",
        "<ROOT>/op/.gnupg",
        "<ROOT>/op/.config/gcloud",
        "<ROOT>/crew/.vault",
        "<ROOT>/op/.kirocrew/.vault",
        "<ROOT>/op/.kirocrew/policy_cache",
        "<ROOT>/crew/run/voice-runtime",
        "<ROOT>/op/.kirocrew/run/voice-runtime",
        "<ROOT>/crew/.env",
        "<ROOT>/crew/diag",
        "<ROOT>/crew/apps/aws-control/data",
        "<ROOT>/crew/workspace/md-notebook/pat",
        "<ROOT>/crew/workspace/md-notebook/vaults.json",
        "<ROOT>/crew/live_target.json",
        "<ROOT>/crew/crew-panels",
        "<ROOT>/op/.kirocrew/.env",
        "<ROOT>/op/.kirocrew/diag",
        "<ROOT>/op/.kirocrew/apps/aws-control/data",
        "<ROOT>/op/.kirocrew/workspace/md-notebook/pat",
        "<ROOT>/op/.kirocrew/workspace/md-notebook/vaults.json",
        "<ROOT>/op/.kirocrew/live_target.json",
        "<ROOT>/op/.kirocrew/crew-panels",
        "<ROOT>/op/.aws",
        "<ROOT>/op/.config/gh",
        "<ROOT>/op/.kube",
        "<ROOT>/podhome/.kiro/crew-auth-staging",
        "<ROOT>/podhome/.gnupg",
        "<ROOT>/podhome/.config/gcloud",
        "<ROOT>/podhome/.kiro/crew/.vault",
        "<ROOT>/podhome/.kiro/crew/policy_cache",
        "<ROOT>/podhome/.kirocrew/.vault",
        "<ROOT>/podhome/.kirocrew/policy_cache",
        "<ROOT>/podhome/.kiro/crew/run/voice-runtime",
        "<ROOT>/podhome/.kirocrew/run/voice-runtime",
        "<ROOT>/podhome/.kiro/crew/.env",
        "<ROOT>/podhome/.kiro/crew/diag",
        "<ROOT>/podhome/.kiro/crew/apps/aws-control/data",
        "<ROOT>/podhome/.kiro/crew/workspace/md-notebook/pat",
        "<ROOT>/podhome/.kiro/crew/workspace/md-notebook/vaults.json",
        "<ROOT>/podhome/.kiro/crew/live_target.json",
        "<ROOT>/podhome/.kiro/crew/crew-panels",
        "<ROOT>/podhome/.kirocrew/.env",
        "<ROOT>/podhome/.kirocrew/diag",
        "<ROOT>/podhome/.kirocrew/apps/aws-control/data",
        "<ROOT>/podhome/.kirocrew/workspace/md-notebook/pat",
        "<ROOT>/podhome/.kirocrew/workspace/md-notebook/vaults.json",
        "<ROOT>/podhome/.kirocrew/live_target.json",
        "<ROOT>/podhome/.kirocrew/crew-panels",
        "<ROOT>/podhome/.config/gh",
        "<ROOT>/podhome/.kube",
        "<ROOT>/podhome/.aws/config",
        "<ROOT>/podhome/.aws/credentials",
        "<ROOT>/crew/apps",
    ],
    "sensitive_dir_ids": {"<ROOT>/crew/apps": [13, 14]},
    "private_dirs": ["<ROOT>/crew/apps/alpha/data"],
    "private_dir_ids": {"<ROOT>/crew/apps/alpha/data": [11, 12]},
    "readonly_dirs": [
        "<ROOT>/crew/policy_cache",
        "<ROOT>/crew/run",
        "<ROOT>/crew/subagents",
        "<ROOT>/crew/security_policy.json",
        "<ROOT>/crew/profiles",
        "<ROOT>/crew/apps/.dev-grants.json",
        "<ROOT>/crew/mcp-launch-approvals",
        "<ROOT>/crew/mcp/resolved",
        "<ROOT>/op/.kirocrew/subagents",
        "<ROOT>/op/.kirocrew/security_policy.json",
        "<ROOT>/op/.kirocrew/profiles",
        "<ROOT>/op/.kirocrew/apps/.dev-grants.json",
        "<ROOT>/op/.kirocrew/mcp-launch-approvals",
        "<ROOT>/op/.kirocrew/mcp/resolved",
        "<ROOT>/op/.kiro/agents",
    ],
    "writable_dirs": ["<ROOT>/crew/run/mcp-tmp/probe-x"],
    "sensitive_files": [
        "<ROOT>/op/.npmrc",
        "<ROOT>/op/.netrc",
        "<ROOT>/crew/.env",
        "<ROOT>/op/.kirocrew/.env",
        "<ROOT>/op/.kiro/crew-auth-staging",
        "<ROOT>/op/.gnupg",
        "<ROOT>/op/.config/gcloud",
        "<ROOT>/crew/.vault",
        "<ROOT>/op/.kirocrew/.vault",
        "<ROOT>/op/.kirocrew/policy_cache",
        "<ROOT>/crew/run/voice-runtime",
        "<ROOT>/op/.kirocrew/run/voice-runtime",
        "<ROOT>/crew/diag",
        "<ROOT>/crew/apps/aws-control/data",
        "<ROOT>/crew/workspace/md-notebook/pat",
        "<ROOT>/crew/workspace/md-notebook/vaults.json",
        "<ROOT>/crew/live_target.json",
        "<ROOT>/crew/crew-panels",
        "<ROOT>/op/.kirocrew/diag",
        "<ROOT>/op/.kirocrew/apps/aws-control/data",
        "<ROOT>/op/.kirocrew/workspace/md-notebook/pat",
        "<ROOT>/op/.kirocrew/workspace/md-notebook/vaults.json",
        "<ROOT>/op/.kirocrew/live_target.json",
        "<ROOT>/op/.kirocrew/crew-panels",
        "<ROOT>/op/.aws",
        "<ROOT>/op/.config/gh",
        "<ROOT>/op/.kube",
        "<ROOT>/podhome/.kiro/crew-auth-staging",
        "<ROOT>/podhome/.gnupg",
        "<ROOT>/podhome/.config/gcloud",
        "<ROOT>/podhome/.kiro/crew/.vault",
        "<ROOT>/podhome/.kiro/crew/policy_cache",
        "<ROOT>/podhome/.kirocrew/.vault",
        "<ROOT>/podhome/.kirocrew/policy_cache",
        "<ROOT>/podhome/.kiro/crew/run/voice-runtime",
        "<ROOT>/podhome/.kirocrew/run/voice-runtime",
        "<ROOT>/podhome/.kiro/crew/.env",
        "<ROOT>/podhome/.kiro/crew/diag",
        "<ROOT>/podhome/.kiro/crew/apps/aws-control/data",
        "<ROOT>/podhome/.kiro/crew/workspace/md-notebook/pat",
        "<ROOT>/podhome/.kiro/crew/workspace/md-notebook/vaults.json",
        "<ROOT>/podhome/.kiro/crew/live_target.json",
        "<ROOT>/podhome/.kiro/crew/crew-panels",
        "<ROOT>/podhome/.kirocrew/.env",
        "<ROOT>/podhome/.kirocrew/diag",
        "<ROOT>/podhome/.kirocrew/apps/aws-control/data",
        "<ROOT>/podhome/.kirocrew/workspace/md-notebook/pat",
        "<ROOT>/podhome/.kirocrew/workspace/md-notebook/vaults.json",
        "<ROOT>/podhome/.kirocrew/live_target.json",
        "<ROOT>/podhome/.kirocrew/crew-panels",
        "<ROOT>/podhome/.config/gh",
        "<ROOT>/podhome/.kube",
        "<ROOT>/podhome/.aws/config",
        "<ROOT>/podhome/.aws/credentials",
        "<ROOT>/crew/apps",
    ],
    "fail_closed_file_masks": [["<ROOT>/op/.npmrc", 3, 4]],
    "alias_credential_ids": [[7, 99], [8, 1]],
    "required_mask_targets": ["<ROOT>/outside"],
    "mask_occupants": {"<ROOT>/crew/apps": [21, 22, 0], "<ROOT>/op/.aws": [23, 24, 1, 1, 25, 26]},
    "crew_home_aliases": [["<ROOT>/op/.kiro/crew", "<ROOT>/crew", 31, 32]],
    "expose_files": [["<ROOT>/op/.aws/sso/cache/token.json", "token.json"]],
    "env_prefixes": [
        "AWS_SECRET",
        "GIT_ASKPASS",
        "SLACK_BOT_TOKEN",
        "JIRA_TOKEN_",
        "KIROCREW_POLICY_URL",
        "PYTHONPATH",
        "PYTHONHOME",
    ],
    "ssh_dir": "<ROOT>/op/.ssh",
    "ssh_known_hosts": "<ROOT>/op/.ssh/known_hosts",
    "hide_ssh": 1,
    "sandbox_level": "strict",
    "unreadable_masks": ["live_target.json"],
    "strict_host_key_opt": " -o StrictHostKeyChecking=accept-new",
    "stand_in_roots": ["/run/user/4242", "/dev/shm"],
}

#: The Seatbelt profile for ``_private_windows`` at the cc tier.
_PROFILE_GOLDEN = """\
(version 1)
(allow default)
(deny file-read* (subpath "<ROOT>/op/.kiro/crew-auth-staging"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew-auth-staging"))
(deny file-read* (subpath "<ROOT>/op/.gnupg"))
(deny file-link (subpath "<ROOT>/op/.gnupg"))
(deny file-read* (subpath "<ROOT>/op/.config/gcloud"))
(deny file-link (subpath "<ROOT>/op/.config/gcloud"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/.vault"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/.vault"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/policy_cache"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/policy_cache"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/policy_cache"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/.vault"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/.vault"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/policy_cache"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/policy_cache"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/policy_cache"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/run/voice-runtime"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/run/voice-runtime"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/run/voice-runtime"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/run/voice-runtime"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/run/voice-runtime"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/run/voice-runtime"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/.env"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/.env"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/.env"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/.env"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/diag"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/diag"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/diag"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/diag"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/apps/aws-control/data"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/apps/aws-control/data"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/apps/aws-control/data"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/apps/aws-control/data"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/workspace/md-notebook/pat"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/workspace/md-notebook/pat"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/workspace/md-notebook/pat"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/workspace/md-notebook/pat"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/workspace/md-notebook/vaults.json"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/workspace/md-notebook/vaults.json"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/workspace/md-notebook/vaults.json"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/workspace/md-notebook/vaults.json"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/live_target.json"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/live_target.json"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/live_target.json"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/live_target.json"))
(deny file-read* (subpath "<ROOT>/op/.kiro/crew/crew-panels"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/crew-panels"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/crew-panels"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/crew-panels"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/.env"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/.env"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/.env"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/.env"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/diag"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/diag"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/diag"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/diag"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/apps/aws-control/data"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/apps/aws-control/data"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/apps/aws-control/data"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/apps/aws-control/data"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/workspace/md-notebook/pat"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/workspace/md-notebook/pat"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/workspace/md-notebook/pat"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/workspace/md-notebook/pat"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/workspace/md-notebook/vaults.json"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/workspace/md-notebook/vaults.json"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/workspace/md-notebook/vaults.json"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/workspace/md-notebook/vaults.json"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/live_target.json"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/live_target.json"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/live_target.json"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/live_target.json"))
(deny file-read* (subpath "<ROOT>/op/.kirocrew/crew-panels"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/crew-panels"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/crew-panels"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/crew-panels"))
(deny file-read* (subpath "<ROOT>/op/.kube"))
(deny file-link (subpath "<ROOT>/op/.kube"))
(deny file-read* (subpath "<ROOT>/crew/policy_cache"))
(deny file-write* (subpath "<ROOT>/crew/policy_cache"))
(deny file-link (subpath "<ROOT>/crew/policy_cache"))
(deny file-read* (subpath "<ROOT>/crew/.env"))
(deny file-write* (subpath "<ROOT>/crew/.env"))
(deny file-write* (literal "<ROOT>/crew/.env"))
(deny file-link (subpath "<ROOT>/crew/.env"))
(deny file-read* (subpath "<ROOT>/crew/diag"))
(deny file-write* (subpath "<ROOT>/crew/diag"))
(deny file-write* (literal "<ROOT>/crew/diag"))
(deny file-link (subpath "<ROOT>/crew/diag"))
(deny file-read* (subpath "<ROOT>/crew/apps/aws-control/data"))
(deny file-write* (subpath "<ROOT>/crew/apps/aws-control/data"))
(deny file-write* (literal "<ROOT>/crew/apps/aws-control/data"))
(deny file-link (subpath "<ROOT>/crew/apps/aws-control/data"))
(deny file-read* (subpath "<ROOT>/crew/workspace/md-notebook/pat"))
(deny file-write* (subpath "<ROOT>/crew/workspace/md-notebook/pat"))
(deny file-write* (literal "<ROOT>/crew/workspace/md-notebook/pat"))
(deny file-link (subpath "<ROOT>/crew/workspace/md-notebook/pat"))
(deny file-read* (subpath "<ROOT>/crew/workspace/md-notebook/vaults.json"))
(deny file-write* (subpath "<ROOT>/crew/workspace/md-notebook/vaults.json"))
(deny file-write* (literal "<ROOT>/crew/workspace/md-notebook/vaults.json"))
(deny file-link (subpath "<ROOT>/crew/workspace/md-notebook/vaults.json"))
(deny file-read* (subpath "<ROOT>/crew/live_target.json"))
(deny file-write* (subpath "<ROOT>/crew/live_target.json"))
(deny file-write* (literal "<ROOT>/crew/live_target.json"))
(deny file-link (subpath "<ROOT>/crew/live_target.json"))
(deny file-read* (subpath "<ROOT>/crew/crew-panels"))
(deny file-write* (subpath "<ROOT>/crew/crew-panels"))
(deny file-write* (literal "<ROOT>/crew/crew-panels"))
(deny file-link (subpath "<ROOT>/crew/crew-panels"))
(deny file-read* (subpath "<ROOT>/crew/run/voice-runtime"))
(deny file-write* (subpath "<ROOT>/crew/run/voice-runtime"))
(deny file-link (subpath "<ROOT>/crew/run/voice-runtime"))
(deny file-write* (literal "<ROOT>/crew/run"))
(deny file-write* (subpath "<ROOT>/crew/run"))
(deny file-link (subpath "<ROOT>/crew/run"))
(deny file-write* (literal "<ROOT>/crew/run"))
(deny file-write* (literal "<ROOT>/crew"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/subagents"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/subagents"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/subagents"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/security_policy.json"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/security_policy.json"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/security_policy.json"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/profiles"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/profiles"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/profiles"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/apps/.dev-grants.json"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/apps/.dev-grants.json"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/apps/.dev-grants.json"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/mcp-launch-approvals"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/mcp-launch-approvals"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/mcp-launch-approvals"))
(deny file-write* (literal "<ROOT>/op/.kiro/crew/mcp/resolved"))
(deny file-write* (subpath "<ROOT>/op/.kiro/crew/mcp/resolved"))
(deny file-link (subpath "<ROOT>/op/.kiro/crew/mcp/resolved"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/subagents"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/subagents"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/subagents"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/security_policy.json"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/security_policy.json"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/security_policy.json"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/profiles"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/profiles"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/profiles"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/apps/.dev-grants.json"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/apps/.dev-grants.json"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/apps/.dev-grants.json"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/mcp-launch-approvals"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/mcp-launch-approvals"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/mcp-launch-approvals"))
(deny file-write* (literal "<ROOT>/op/.kirocrew/mcp/resolved"))
(deny file-write* (subpath "<ROOT>/op/.kirocrew/mcp/resolved"))
(deny file-link (subpath "<ROOT>/op/.kirocrew/mcp/resolved"))
(deny file-write* (literal "<ROOT>/crew/subagents"))
(deny file-write* (subpath "<ROOT>/crew/subagents"))
(deny file-link (subpath "<ROOT>/crew/subagents"))
(deny file-write* (literal "<ROOT>/crew/security_policy.json"))
(deny file-write* (subpath "<ROOT>/crew/security_policy.json"))
(deny file-link (subpath "<ROOT>/crew/security_policy.json"))
(deny file-write* (literal "<ROOT>/crew/profiles"))
(deny file-write* (subpath "<ROOT>/crew/profiles"))
(deny file-link (subpath "<ROOT>/crew/profiles"))
(deny file-write* (literal "<ROOT>/crew/apps/.dev-grants.json"))
(deny file-write* (subpath "<ROOT>/crew/apps/.dev-grants.json"))
(deny file-link (subpath "<ROOT>/crew/apps/.dev-grants.json"))
(deny file-write* (literal "<ROOT>/crew/mcp-launch-approvals"))
(deny file-write* (subpath "<ROOT>/crew/mcp-launch-approvals"))
(deny file-link (subpath "<ROOT>/crew/mcp-launch-approvals"))
(deny file-write* (literal "<ROOT>/crew/mcp/resolved"))
(deny file-write* (subpath "<ROOT>/crew/mcp/resolved"))
(deny file-link (subpath "<ROOT>/crew/mcp/resolved"))
(deny file-write* (literal "<ROOT>/op/.kiro/agents"))
(deny file-write* (subpath "<ROOT>/op/.kiro/agents"))
(deny file-link (subpath "<ROOT>/op/.kiro/agents"))
(deny file-read* (literal "<ROOT>/op/.npmrc"))
(deny file-link (literal "<ROOT>/op/.npmrc"))
(deny file-read* (literal "<ROOT>/op/.netrc"))
(deny file-link (literal "<ROOT>/op/.netrc"))
(deny file-read* (literal "<ROOT>/op/.kiro/crew/.env"))
(deny file-link (literal "<ROOT>/op/.kiro/crew/.env"))
(deny file-read* (literal "<ROOT>/op/.kirocrew/.env"))
(deny file-link (literal "<ROOT>/op/.kirocrew/.env"))
(deny file-read* (require-all (subpath "<ROOT>/crew/apps") (require-not (subpath "<ROOT>/crew/apps/alpha/data"))))
(deny file-write* (require-all (subpath "<ROOT>/crew/apps") (require-not (subpath "<ROOT>/crew/apps/alpha/data"))))
(deny file-link (require-all (subpath "<ROOT>/crew/apps") (require-not (subpath "<ROOT>/crew/apps/alpha/data"))))
(allow file-read-metadata (literal "<ROOT>/crew/apps/alpha"))
(allow file-read-metadata (literal "<ROOT>/crew/apps"))
"""


def test_the_launcher_carries_the_golden_plan(
    host: _Host, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _pod_home(host, monkeypatch)
    caplog.set_level(logging.WARNING, logger="kiro_crew.sandbox")
    script = sandbox._build_launcher_script("strict", **_everything(host))
    assert _fold(rendered_payload(script), host) == _LAUNCHER_PLAN_GOLDEN
    # The window that holds a masked tree is refused, as a log line naming no path.
    assert [r.getMessage() for r in caplog.records if r.name == "kiro_crew.sandbox"] == [
        sandbox_plan.WINDOW_REFUSAL
    ]


def test_the_launcher_is_the_program_module_plus_the_plan(host: _Host) -> None:
    """One substitution: the program module's own source, with the plan's data in it."""
    plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "strict", **_everything(host))
    source = Path(sandbox_launcher_program.__file__).read_text(encoding="utf-8")
    line = "_PLAN = %s\n" % json.dumps(sandbox_plan.namespace_payload(plan))
    assert sandbox_launcher.render_namespace_launcher(plan) == source.replace(
        sandbox_launcher.PLAN_PLACEHOLDER, line
    )


def test_the_seatbelt_profile_is_the_golden(host: _Host) -> None:
    kwargs = {k: v for k, v in _private_windows(host).items() if not k.endswith("_ids")}
    profile = sandbox._build_seatbelt_profile("cc", **kwargs)
    assert profile.replace(str(host.root), "<ROOT>") == _PROFILE_GOLDEN


def test_each_pinned_host_input_reaches_the_plan(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host input that stopped reaching the plan would leave the goldens above passing
    on a planner that ignores it, so each one must move the plan."""

    def _plan(**kwargs: Any) -> sandbox_plan.ConfinementPlan:
        return sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "strict", **kwargs)

    plain = _plan()
    assert plain.ssh_accept_new is True
    with pytest.MonkeyPatch.context() as scoped:
        _no_accept_new(host, scoped)
        assert _plan().ssh_accept_new is False
    with pytest.MonkeyPatch.context() as scoped:
        _pod_home(host, scoped)
        pod = str(host.root / "podhome")
        assert [d for d in _plan().sensitive_dirs if d.startswith(pod)]
        assert not [d for d in plain.sensitive_dirs if d.startswith(pod)]
    with pytest.MonkeyPatch.context() as scoped:
        _planted_notebook_chain(host, scoped)
        degraded = str(host.crew / "workspace" / "md-notebook")
        assert degraded in _plan().sensitive_dirs and degraded not in plain.sensitive_dirs
    with pytest.MonkeyPatch.context() as scoped:
        _symlinked_home(host, scoped)
        assert str(host.crew / "run" / "voice-runtime") in _plan().sensitive_dirs
    assert plain.uid == _UID and plain.gid == _GID
    assert plain.home == str(host.home)


# --------------------------------------------------------------------------- #
# The live host reads what it plans from kiro_crew.sandbox when it runs.
# --------------------------------------------------------------------------- #


class _Reached(BaseException):
    """Raised by a stub to prove the host adapter called the name it replaced.

    A ``BaseException`` because several of the readers are best-effort and swallow
    ``Exception``, which would let a patch that MISSED read as one that landed.
    """


def _raiser(label: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise _Reached(label)

    return _stub


#: The host readers ``_live_plan_host`` calls for each backend. A patch of one on
#: ``kiro_crew.sandbox`` must still reach that backend's plan.
_HOST_READERS: dict[str, tuple[str, ...]] = {
    sandbox_plan.BACKEND_NAMESPACE: (
        "_md_notebook_degraded_mask_dirs",
        "_relocated_crew_targets",
        "_relocated_policy_cache_dirs",
        "_resolved_kiro_agents_targets",
        "_sandbox_policy",
        "_ssh_supports_accept_new",
        "_voice_runtime_parent_paths",
        "_voice_runtime_sandbox_paths",
    ),
    sandbox_plan.BACKEND_SEATBELT: (
        "_md_notebook_degraded_mask_dirs",
        "_relocated_crew_targets",
        "_relocated_policy_cache_dirs",
        "_resolved_kiro_agents_targets",
        "_sandbox_policy",
        "_voice_runtime_ancestor_guards",
        "_voice_runtime_parent_paths",
        "_voice_runtime_sandbox_paths",
    ),
}


@pytest.mark.parametrize(
    ("backend", "name"),
    [(backend, name) for backend, names in _HOST_READERS.items() for name in names],
)
def test_a_reader_patched_on_the_sandbox_reaches_the_plan(
    backend: str, name: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, name, _raiser(name))
    with pytest.raises(_Reached, match=name):
        sandbox._spawn_plan(backend, "strict")


def _strings(value: object) -> list[str]:
    """Every string *value* holds: a plan's fields, a payload's lists and keys."""
    if isinstance(value, str):
        return [value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [
            s for field in dataclasses.fields(value) for s in _strings(getattr(value, field.name))
        ]
    if isinstance(value, Mapping):
        return [s for key, item in value.items() for s in _strings(key) + _strings(item)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [s for item in value for s in _strings(item)]
    return []


def _planned(backend: str, tier: str, kwargs: dict[str, Any]) -> list[str]:
    """What *backend* is handed for one spawn: the launcher's payload, or the plan itself."""
    plan = sandbox._spawn_plan(backend, tier, **kwargs)
    if backend == sandbox_plan.BACKEND_NAMESPACE:
        return _strings(sandbox_plan.namespace_payload(plan))
    return _strings(plan)


#: Tables both backends plan from, as (name, tier, keyword arguments, the probe value,
#: and the text the probe must put into the plan).
_TABLES: list[tuple[str, str, dict[str, Any], object, str]] = [
    ("_STANDARD_DIRS", "standard", {}, [".b10-probe-dir"], "/.b10-probe-dir"),
    ("_CC_FILES", "cc", {}, [".b10-probe-file"], "/.b10-probe-file"),
    ("_CREW_HIDDEN_LEAVES", "strict", {}, ("b10-probe-hidden",), "/crew/b10-probe-hidden"),
    ("_CREW_READONLY_LEAVES", "strict", {}, ("b10-probe-ro",), "/crew/b10-probe-ro"),
    ("_CREW_READONLY_TARGETS", "strict", {}, [".b10-probe-ceiling"], "/.b10-probe-ceiling"),
    ("_CC_EXPOSE_FILES", "cc", {}, [".gnupg/b10-probe-expose"], "/.gnupg/b10-probe-expose"),
]

#: Tables only the launcher's plan carries: the environment scrub lists and the leaves
#: whose Linux mask refuses the read.
_LAUNCHER_ONLY_TABLES: list[tuple[str, str, dict[str, Any], object, str]] = [
    ("_SENSITIVE_ENV_PREFIXES", "standard", {}, ("B10_PROBE_SENSITIVE_",), "B10_PROBE_SENSITIVE_"),
    ("_AGENT_DENIED_ENV_KEYS", "strict", {}, ("B10_PROBE_DENIED",), "B10_PROBE_DENIED"),
    (
        "_CREW_UNREADABLE_MASK_LEAVES",
        "strict",
        {},
        frozenset({"b10-probe-unreadable"}),
        "b10-probe-unreadable",
    ),
    (
        "_PYTHON_ENV_PREFIXES",
        "standard",
        {"strip_python_env": True},
        ("B10_PROBE_PY_",),
        "B10_PROBE_PY_",
    ),
]

_TABLE_CASES = [(sandbox_plan.BACKEND_NAMESPACE, *row) for row in _TABLES + _LAUNCHER_ONLY_TABLES]
_TABLE_CASES += [(sandbox_plan.BACKEND_SEATBELT, *row) for row in _TABLES]


@pytest.mark.parametrize(
    ("backend", "name", "tier", "kwargs", "value", "needle"),
    _TABLE_CASES,
    ids=[f"{case[0]}-{case[1]}" for case in _TABLE_CASES],
)
def test_a_table_patched_on_the_sandbox_reaches_the_plan(
    backend: str,
    name: str,
    tier: str,
    kwargs: dict[str, Any],
    value: object,
    needle: str,
    host: _Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not [s for s in _planned(backend, tier, kwargs) if needle in s]
    monkeypatch.setattr(sandbox, name, value)
    assert [s for s in _planned(backend, tier, kwargs) if needle in s]


@pytest.mark.parametrize("tier", ["strict", "standard", "cc"])
def test_namespace_argv_writes_exactly_what_the_builder_renders(
    tier: str, host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file the child runs is the builder's text, and the argv invokes it isolated."""
    rendered: list[str] = []
    real = sandbox._build_launcher_script

    def spy(*args: Any, **kwargs: Any) -> str:
        rendered.append(real(*args, **kwargs))
        return rendered[-1]

    monkeypatch.setattr(sandbox, "_build_launcher_script", spy)
    monkeypatch.setattr(sandbox, "_resolve_agent_executable", lambda executable: executable)
    argv = sandbox.namespace_argv(["/bin/true", "--flag"], tier)
    assert len(rendered) == 1
    assert argv[:3] == [sys.executable, "-I", "-S"]
    assert argv[4:] == ["/bin/true", "--flag"]
    launcher = Path(argv[3])
    assert launcher.parent == host.crew / "run"
    assert launcher.name.startswith(f"kirocrew_sandbox_{os.getpid()}_")
    assert launcher.read_bytes() == rendered[0].encode("utf-8")


def test_the_launcher_names_the_product_once_in_two_words(host: _Host) -> None:
    script = sandbox._build_launcher_script("strict")
    assert script.count(_DOC_LINE) == 1
