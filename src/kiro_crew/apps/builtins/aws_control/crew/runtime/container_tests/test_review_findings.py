"""Two findings from review on this change, each pinned so a revert reddens.

**The task role reached the model process.** ``build_backend_env`` already drops the
front's control secret, on the premise that the backend spawns the model subprocess, which
inherits this environment and auto-approves every tool it calls (see
``test_backend_env_drops_control_secret``). The same premise covers the task role, and it
was not being dropped. On Fargate that credential is not a file or a key but an HTTP
endpoint named by ``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI``, which every AWS SDK resolves
unasked, so a turn could read the variable from its own environment and act as the task
role.

**The 400 body returned ``str(exc)``.** Harmless while one raise site holds an authored
message, and wrong the first time someone writes ``BadForwardField(f"...{inner}")``: the
response would carry whatever the inner exception says about the container, on a line whose
reviewer is not looking at the response path. ``detail`` names the caller-facing channel so
the decision sits where it is made.

The third finding on this change is about PACKAGING -- that no published wheel carried this
tree, because ``python -m build`` builds the wheel from the sdist and ``MANIFEST.in``
reached none of these files. It is pinned in ``test/test_crew_runtime_payload.py`` rather
than here: this suite's subject is the image, which runs on an installed copy where there
is no repository and no ``MANIFEST.in`` to read.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from container.supervisor import backend as be

from ._settings_helper import make_settings

RUNTIME_ROOT = pathlib.Path(be.__file__).resolve().parents[2]
FRONT_APP = RUNTIME_ROOT / "container" / "front" / "app.py"

RELATIVE_URI = "/v2/credentials/2b7f1e44-0000-4000-8000-0123456789ab"


# ---------------------------------------------------------------------------
# The task role must not reach the model process
# ---------------------------------------------------------------------------
def test_the_ecs_credential_endpoint_is_withheld_from_the_backend(tmp_path):
    """The variable an SDK resolves the task role through must not survive.

    Asserted on the RELATIVE_URI form because that is the one Fargate actually sets: a test
    covering only the static-key names would pass while the live credential path stayed
    open.
    """
    base = {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": RELATIVE_URI,
        "AWS_CONTAINER_AUTHORIZATION_TOKEN": "header-token-value",
        "KIRO_API_KEY": "aws-kiro-abcdefghijklmnopqrstuvwxyz0123456789",
        "PATH": "/usr/bin",
    }
    env = be.build_backend_env(make_settings(tmp_path), base)

    assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in env
    assert "AWS_CONTAINER_AUTHORIZATION_TOKEN" not in env
    # Not under another name either, the standard the control-secret test set: a rename
    # keeping the VALUE reachable would defeat the point, which is that the subprocess
    # cannot read it from its environment at all.
    assert RELATIVE_URI not in env.values(), (
        "the credential path survives under another key: "
        f"{sorted(k for k, v in env.items() if v == RELATIVE_URI)}"
    )
    assert env["KIRO_API_KEY"].startswith("aws-kiro-"), "the model credential is what stays"
    assert env["PATH"] == "/usr/bin", "unrelated variables must still be inherited"


@pytest.mark.parametrize("name", sorted(be.AWS_CRED_ENV))
def test_every_listed_aws_credential_variable_is_actually_dropped(name, tmp_path):
    """Each entry must be removed, not merely listed.

    A name present in the set that the builder never pops would read as covered while the
    variable sailed through -- the gap a single happy-path assertion cannot see.
    """
    base = {name: "sentinel-value", "KIRO_API_KEY": "aws-kiro-0123456789"}
    env = be.build_backend_env(make_settings(tmp_path), base)
    assert name not in env
    assert "sentinel-value" not in env.values()


def test_the_supervisor_keeps_its_own_credentials(tmp_path):
    """Stripping is scoped to the CHILD environment.

    The supervisor is what legitimately uses the task role, so ``build_backend_env``
    returning a copy rather than mutating its input is load-bearing here, not incidental.
    """
    base = {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": RELATIVE_URI, "KIRO_API_KEY": "k"}
    be.build_backend_env(make_settings(tmp_path), base)
    assert base["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"] == RELATIVE_URI


def test_the_two_credential_sets_are_populated_and_disjoint():
    """Non-vacuity, and a guard against the two lists growing into each other.

    An emptied set leaves its loop in ``build_backend_env`` running over nothing while
    every test above that names a variable explicitly would still fail -- but the
    parametrised one would silently collapse to zero cases.
    """
    assert be.AWS_CRED_ENV, "the AWS set is empty, so its strip loop does nothing"
    assert be.CHANNEL_CRED_ENV, "the channel set is empty"
    assert not (be.AWS_CRED_ENV & be.CHANNEL_CRED_ENV)


# ---------------------------------------------------------------------------
# Only text written for the caller reaches the caller
# ---------------------------------------------------------------------------
def test_no_exception_str_reaches_a_response_body():
    """``exc.detail``, never ``str(exc)``.

    A source assertion because the difference is invisible in behaviour today: the single
    raise site passes an authored message, so both spellings return the same bytes. What is
    pinned is the spelling the NEXT author inherits.
    """
    tree = ast.parse(FRONT_APP.read_text(encoding="utf-8"), str(FRONT_APP))
    offenders: list[int] = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        for call in ast.walk(handler):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "str"
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == handler.name
            ):
                offenders.append(call.lineno)

    assert not offenders, (
        f"an exception's str() is rendered inside a handler at front/app.py {offenders}; "
        "put caller-facing text on an explicit attribute instead"
    )


def test_the_detail_attribute_carries_the_authored_message():
    """The channel has to work, or the 400 would answer with an empty detail."""
    from container.front.app import BadForwardField

    message = '"stream" must be a boolean (true/false), not str.'
    exc = BadForwardField(message)
    assert exc.detail == message
    assert str(exc) == message, "str() must stay useful for logs and pytest output"
