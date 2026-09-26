"""The named policy for stripping Kiro Crew's Python startup variables.

The shell-facing scrubs (the dashboard terminal's PTY child, the MCP
gateway's third-party backend spawn) drop the Python startup prefixes and
keep every credential-bearing variable. These pins hold that policy under
one public name, ``sandbox.strip_python_env``, so the next caller routes
through it rather than copying a prefix loop.
"""

import kiro_crew.sandbox as sb


def test_strip_python_env_removes_the_python_prefixes():
    env = {
        "PYTHONPATH": "/host/site-packages",
        "PYTHONHOME": "/host/python",
        "PYTHONPYCACHEPREFIX": "/host/cache/pycache",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": "/usr/bin",
    }

    assert sb.strip_python_env(env) == {"PATH": "/usr/bin"}


def test_strip_python_env_keeps_every_credential_bearing_var():
    """The policy is deliberately narrower than ``scrub_agent_subprocess_env``:
    both call sites serve the operator's own unsandboxed processes, where taking
    ``SSH_AUTH_SOCK`` or the AWS keys would break git-over-SSH and the AWS CLI
    inside the panel. Benign Python settings outside the four prefixes
    (``PYTHONUNBUFFERED``) survive too -- the gateway's third-party backends are
    pinned on exactly that."""
    env = {
        "SSH_AUTH_SOCK": "/tmp/ssh-abc/agent.1",
        "AWS_SESSION_TOKEN": "FAKE-token",
        "GNUPGHOME": "/gnupg",
        "GIT_ASKPASS": "/git/askpass",
        "PYTHONUNBUFFERED": "1",
        "PATH": "/usr/bin",
    }

    assert sb.strip_python_env(env) == env


def test_strip_python_env_is_case_insensitive():
    """Windows treats environment names as case-insensitive, so a lowercase
    spelling of the same interpreter variable must not survive. On POSIX a
    lowercase ``pythonpath`` is inert to CPython, so the wider match strips
    nothing the interpreter would have honoured."""
    env = {"pythonpath": "/host/site-packages", "PythonHome": "/host/python"}

    assert sb.strip_python_env(env) == {}


def test_strip_python_env_does_not_mutate_its_input():
    env = {"PYTHONPATH": "/host/site-packages", "PATH": "/usr/bin"}

    out = sb.strip_python_env(env)

    assert out is not env
    assert env["PYTHONPATH"] == "/host/site-packages"
