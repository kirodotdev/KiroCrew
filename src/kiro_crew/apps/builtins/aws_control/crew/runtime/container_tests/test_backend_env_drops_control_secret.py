"""The backend must not hand the front's control secret to the model subprocess.

``build_backend_env`` copies the task environment wholesale. That environment holds
``SMC_CONTROL_SECRET``, injected by the ECS task definition from Secrets Manager for
the FRONT to validate the ``X-SMC-Control-Secret`` header with. The backend never
reads it -- but the backend spawns the model subprocess, which inherits its
environment, and on Fargate that subprocess runs unsandboxed with an auto-approved
shell. So a prompt able to read its own environment could lift the secret and then
call the front's control endpoints as the control plane.
"""

from __future__ import annotations

from container.supervisor import backend as be

from .test_backup_restore import make_settings


def _settings(tmp_path):
    """The suite's own factory, with the control secret the front would validate."""
    s = make_settings(tmp_path)
    return s.__class__(**{**s.__dict__, "control_secret": "s3cr3t-control"})


def test_the_control_secret_is_not_in_the_backend_environment(tmp_path):
    base = {
        "SMC_CONTROL_SECRET": "s3cr3t-control",
        "KIRO_API_KEY": "aws-kiro-abcdefghijklmnopqrstuvwxyz0123456789",
        "PATH": "/usr/bin",
    }
    env = be.build_backend_env(_settings(tmp_path), base)

    assert "SMC_CONTROL_SECRET" not in env
    # And not under any other name either: a rename that kept the VALUE reachable
    # would defeat the point, which is that the value cannot be read from the
    # subprocess environment at all.
    assert "s3cr3t-control" not in env.values(), (
        f"the control secret's value survives under another key: "
        f"{sorted(k for k, v in env.items() if v == 's3cr3t-control')}"
    )


def test_the_model_credential_still_reaches_the_backend(tmp_path):
    """The strip must not take the credential the backend cannot start without."""
    key = "aws-kiro-abcdefghijklmnopqrstuvwxyz0123456789"
    env = be.build_backend_env(
        _settings(tmp_path), {"SMC_CONTROL_SECRET": "x", "KIRO_API_KEY": key}
    )
    assert env["KIRO_API_KEY"] == key
    be.require_api_key(env)  # must not raise


def test_the_front_still_receives_the_secret_through_settings(tmp_path):
    """Removing it from the backend env must not remove it from the front's config.

    The front validates the header against ``Settings.control_secret``, which is
    loaded from the task environment in the FRONT's own process -- a different
    process that is not launched through ``build_backend_env``.
    """
    settings = _settings(tmp_path)
    assert settings.control_secret == "s3cr3t-control"
