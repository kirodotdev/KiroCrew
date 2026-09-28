"""A cron job's own ``env`` map must not carry a governance policy path.

``job.env`` originates in an app manifest's ``crons[].env`` block, which
``apps.manifest.CronEntry.from_dict`` copies verbatim -- keys are stringified,
never screened -- and the gateway hands it to the scheduled session's spawn as
``extra_env``. Two names in it decide how the run is GOVERNED rather than what it
does: ``platform.governance`` reads ``KIROCREW_SECURITY_POLICY`` and
``platform.admission`` reads ``KIROCREW_ADMISSION_POLICY`` as a tier resolved
ahead of the operator's own ``security_policy.json``, first-present-wins, so a
job-supplied path replaces the operator's ceiling for the scheduled agent and
every MCP server it starts.

The seam that drops them is the untrusted-input one, deliberately: the gateway's
own ``os.environ`` copy of the pair is the OPERATOR's value, and a child that
inherits neither resolves the standalone ungoverned ceiling, which is open where
deny-by-default is open. So the agent spawn's env scrub must keep passing the
operator's value through, and the last test here is that control.

The seam is resolved by name off the gateway module rather than imported, so the
proof fails on its own assertion when no seam strips the pair -- an import error
would report a missing symbol instead of the property that matters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, cast

import kiro_crew.slack.gateway as gateway_mod
from kiro_crew.apps.manifest import CronEntry
from kiro_crew.cron import CronService
from kiro_crew.platform import governance
from kiro_crew.platform.governance import (
    TIER_ENV,
    TIER_HOME,
    load_security_policy,
    resolve,
)
from kiro_crew.sandbox import scrub_agent_subprocess_env

#: The two governance trust-root path variables under test.
_POLICY_PATH_VARS = ("KIROCREW_SECURITY_POLICY", "KIROCREW_ADMISSION_POLICY")

#: The reserved control variable the seam already dropped. Positive control: it
#: must keep being dropped, so the change adds names rather than replacing them.
_ALREADY_STRIPPED = "KIROCREW_APPROVAL_MODE"

#: Name of the gateway seam that filters a cron job's declared ``env``.
_SEAM = "cron_job_env_without_reserved"


def _policy_doc(**extra: object) -> dict:
    body: dict = {"version": 1, "boot": {"fail_closed": True}}
    body.update(extra)
    return body


def _cron_env_seam() -> Callable[[dict[str, str] | None], dict[str, str]]:
    """The gateway's cron ``env`` filter, or a failure naming what is missing."""
    seam = getattr(gateway_mod, _SEAM, None)
    assert seam is not None, (
        f"kiro_crew.slack.gateway exposes no {_SEAM}: nothing screens a cron job's "
        "declared env before it becomes the scheduled session's extra_env"
    )
    return cast(Callable[[dict[str, str] | None], dict[str, str]], seam)


def _store_job_env(tmp_path: Path, declared: dict[str, str]) -> dict[str, str]:
    """Persist *declared* as a real cron job's ``env`` and read it back off disk."""
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("app:digest", "summarise", every_secs=3600, env=dict(declared))
    reloaded = CronService(base_dir=tmp_path)
    stored = next(j for j in reloaded.list_jobs(include_disabled=True) if j.id == job.id)
    return stored.env


def test_manifest_cron_env_is_copied_without_screening() -> None:
    """PRECONDITION: the manifest parser applies no key policy to ``env``."""
    entry = CronEntry.from_dict(
        {
            "name": "digest",
            "every": 3600,
            "message": "summarise",
            "env": {
                "KIROCREW_SECURITY_POLICY": "/opt/app/loose.json",
                "KIROCREW_ADMISSION_POLICY": "/opt/app/loose_admission.json",
                "BENIGN": "1",
            },
        }
    )
    assert entry.env["KIROCREW_SECURITY_POLICY"] == "/opt/app/loose.json"
    assert entry.env["KIROCREW_ADMISSION_POLICY"] == "/opt/app/loose_admission.json"
    assert entry.env["BENIGN"] == "1"


def test_env_tier_policy_path_replaces_a_home_tier_command_deny(
    monkeypatch, tmp_path: Path
) -> None:
    """Holding ``KIROCREW_SECURITY_POLICY`` is enough to swap the ceiling.

    Documented precedence, not a defect on its own -- it is what makes delivery
    of the variable an escalation rather than a cosmetic leak.
    """
    home = tmp_path / "security_policy.json"
    home.write_text(
        json.dumps(_policy_doc(commands={"mode": "deny", "deny": ["git push*"]})),
        encoding="utf-8",
    )
    monkeypatch.setattr(governance, "_policy_home_path", lambda: home)
    monkeypatch.delenv("KIROCREW_SECURITY_POLICY", raising=False)

    home_ceiling = load_security_policy()
    assert home_ceiling is not None
    assert home_ceiling.tier == TIER_HOME
    assert not resolve(home_ceiling, None, "commands", "git push origin main").permitted

    loose = tmp_path / "app_supplied" / "loose.json"
    loose.parent.mkdir(parents=True)
    loose.write_text(
        json.dumps(_policy_doc(commands={"mode": "deny", "deny": []})), encoding="utf-8"
    )
    monkeypatch.setenv("KIROCREW_SECURITY_POLICY", str(loose))

    env_ceiling = load_security_policy()
    assert env_ceiling is not None
    assert env_ceiling.tier == TIER_ENV
    assert resolve(env_ceiling, None, "commands", "git push origin main").permitted


def test_cron_job_env_must_not_deliver_governance_policy_paths(tmp_path: Path) -> None:
    """The policy-path vars must not survive the cron job's own ``env`` map."""
    attacker_policy = tmp_path / "app_supplied" / "loose.json"
    attacker_policy.parent.mkdir(parents=True)
    attacker_policy.write_text(json.dumps(_policy_doc()), encoding="utf-8")

    declared = {
        "KIROCREW_SECURITY_POLICY": str(attacker_policy),
        "KIROCREW_ADMISSION_POLICY": str(attacker_policy),
        _ALREADY_STRIPPED: "auto",
        "BENIGN": "1",
    }

    # PRECONDITION: the store keeps the map verbatim, across a reload.
    stored_env = _store_job_env(tmp_path, declared)
    assert stored_env == declared

    extra_env = _cron_env_seam()(stored_env)

    # POSITIVE CONTROLS: the seam drops the control var it already owned, and
    # passes an ordinary job variable through untouched.
    assert _ALREADY_STRIPPED not in extra_env
    assert extra_env["BENIGN"] == "1"

    # THE SECURITY PROPERTY: a job's env may not hand the spawn a governance
    # trust root.
    for name in _POLICY_PATH_VARS:
        assert name not in extra_env, (
            f"a cron job's env delivered {name} -- a governance trust-root path -- "
            "to the scheduled session's spawn"
        )


def test_cron_job_env_matches_reserved_names_case_insensitively(tmp_path: Path) -> None:
    """Policy-path names are reserved regardless of their declared case."""
    declared = {
        "kirocrew_security_policy": "/opt/app/loose.json",
        "kirocrew_admission_policy": "/opt/app/loose_admission.json",
        "MixedCaseJobValue": "preserved",
    }

    stored_env = _store_job_env(tmp_path, declared)
    assert stored_env == declared

    extra_env = _cron_env_seam()(stored_env)

    for name in ("kirocrew_security_policy", "kirocrew_admission_policy"):
        assert name not in extra_env, (
            f"a cron job's env delivered lowercase reserved name {name} to the "
            "scheduled session's spawn"
        )
    assert extra_env == {"MixedCaseJobValue": "preserved"}


def test_cron_job_env_must_not_deliver_the_data_home(tmp_path: Path) -> None:
    """``KIROCREW_HOME`` picks the same ceiling one level up, so it is reserved.

    ``config.paths`` resolves the data home from it and
    ``governance._policy_home_path`` resolves ``security_policy.json`` under that
    home, so a job-supplied value points the ceiling read at a directory the job
    controls. It is absent from ``sandbox._AGENT_DENIED_ENV_KEYS``, unlike the
    ``KIROCREW_POLICY_*`` fetch family, so this seam is what stops it.
    """
    declared = {
        "KIROCREW_HOME": str(tmp_path / "app_controlled_home"),
        "kirocrew_home": str(tmp_path / "app_controlled_home_lower"),
        "BENIGN": "1",
    }

    stored_env = _store_job_env(tmp_path, declared)
    assert stored_env == declared

    extra_env = _cron_env_seam()(stored_env)

    for name in ("KIROCREW_HOME", "kirocrew_home"):
        assert name not in extra_env, (
            f"a cron job's env delivered {name} -- which relocates the home every "
            "keystone file resolves from -- to the scheduled session's spawn"
        )
    assert extra_env == {"BENIGN": "1"}


def test_cron_job_env_must_not_deliver_the_edition_profile(tmp_path: Path) -> None:
    """``KIROCREW_PROFILE`` picks which ceiling is composed, so it is reserved.

    ``platform.resolve_profile`` reads it, and a job-supplied ``standalone``
    drops a companion edition's overlay, so an operation the enterprise ceiling
    denies resolves as permitted.
    """
    declared = {
        "KIROCREW_PROFILE": "standalone",
        "kirocrew_profile": "standalone",
        "BENIGN": "1",
    }

    stored_env = _store_job_env(tmp_path, declared)
    assert stored_env == declared

    extra_env = _cron_env_seam()(stored_env)

    for name in ("KIROCREW_PROFILE", "kirocrew_profile"):
        assert name not in extra_env, (
            f"a cron job's env delivered {name} -- which picks which ceiling is "
            "composed -- to the scheduled session's spawn"
        )
    assert extra_env == {"BENIGN": "1"}


def test_the_policy_fetch_family_is_stopped_by_the_spawn_scrub(tmp_path: Path) -> None:
    """The fetch family needs no cron reservation: the spawn scrub owns it.

    ``sandbox._AGENT_DENIED_ENV_KEYS`` carries the ``KIROCREW_POLICY_*`` names by
    exact spelling, so a job-supplied value is dropped before the child reads it.
    This records where that boundary is, so the cron seam is not widened to
    duplicate it.
    """
    fetch_family = (
        "KIROCREW_POLICY_URL",
        "KIROCREW_POLICY_CACHE_ONLY",
        "KIROCREW_POLICY_MAX_CACHE_AGE_SECS",
    )
    declared = {name: "https://attacker.invalid/policy.json" for name in fetch_family}

    stored_env = _store_job_env(tmp_path, declared)
    extra_env = _cron_env_seam()(stored_env)

    # The cron seam passes them through: they are not its business.
    assert set(extra_env) == set(fetch_family)

    child = scrub_agent_subprocess_env({"PATH": "/usr/bin", **extra_env})

    for name in fetch_family:
        assert name not in child, f"{name} must be dropped by the agent spawn scrub"


def test_the_operators_own_policy_path_still_reaches_the_child(monkeypatch) -> None:
    """The fix must not cost the operator their own env-configured ceiling.

    ``governance`` resolves an absent tier as the standalone ungoverned ceiling,
    which is open where deny-by-default is open, so an operator who configures the
    ceiling only through the env var must keep having it inherited. Screening
    ``job.env`` leaves that inheritance intact; scrubbing the pair from every
    agent child would not.
    """
    operator_policy = "/etc/kirocrew/operator_policy.json"
    gateway_env = {"PATH": "/usr/bin"}
    for name in _POLICY_PATH_VARS:
        monkeypatch.setenv(name, operator_policy)
        gateway_env[name] = operator_policy

    child = scrub_agent_subprocess_env(gateway_env)

    for name in _POLICY_PATH_VARS:
        assert child.get(name) == operator_policy, (
            f"{name} set by the OPERATOR must still reach the agent child; only a "
            "job-supplied value is refused"
        )
