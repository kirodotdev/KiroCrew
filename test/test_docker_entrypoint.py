"""Regression tests for docker/entrypoint.sh seeding + credential hygiene.

The entrypoint owns three container-boundary behaviors:

* Sandbox posture is decided by PROBING the product's backend detector on a
  genuine first run — the inner namespace sandbox stays ON when the runtime
  permits it; the unsandboxed opt-out is seeded only when no backend works.
  "First run" means ``config.json`` is absent: an EMPTY mounted volume is
  still a first run, while any existing config is operator-owned and a
  legacy ``~/.kirocrew`` is owned by the product's migrator.
* Channel credentials arriving as container env are moved into the
  product's credential file (``.env``, 0600) and scrubbed from the
  environment BEFORE the gateway is exec'd, so the gateway's
  ``/proc/<pid>/environ`` snapshot never carries a credential.
* Explicit ``KIROCREW_HOME`` / legacy-home resolution mirrors the backend.

These tests execute the REAL script with stubbed ``kirocrew`` and
``python3`` binaries on PATH (the python3 stub makes the sandbox probe
outcome deterministic per test), in a throwaway ``$HOME``.

POSIX-only: the script is /bin/sh and ships inside the Linux image; there
is no Windows execution path for it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="entrypoint.sh is POSIX-only (runs inside the Linux image)"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"

SEED_AUTO_JSON = {"agent": {"sandbox": "auto"}}
SEED_CONSENT_JSON = {
    "agent": {"sandbox": "auto", "sandbox_allow_unsandboxed_exec": True}
}


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_entrypoint(
    home: Path,
    *args: str,
    probe_backend_available: bool = False,
    resolver_python: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real entrypoint with fake ``kirocrew``/``python3``/``tini``
    on PATH.

    The ``python3`` stub DISPATCHES on the ``-c`` code: the data-home
    resolver call (``ensure_data_home``) runs the REAL product code via the
    test process's interpreter (genuine integration coverage of override
    validation, tilde expansion, and legacy migration — hermetic under the
    throwaway ``$HOME``), while the sandbox probe (``detect_backend``)
    returns a scripted exit so posture tests are deterministic on any host.
    ``resolver_python`` overrides the resolver interpreter (e.g.
    ``/usr/bin/false`` to simulate a broken install). The ``kirocrew`` shim
    dumps its inherited environment to ``$HOME/captured-env`` for
    credential-scrub assertions; the ``tini`` stub mirrors exec-through.
    """
    shim_dir = home / ".shim-bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    _write_stub(
        shim_dir / "kirocrew",
        '#!/bin/sh\nenv > "$ENTRYPOINT_TEST_ENV_CAPTURE"\necho "shim:kirocrew $@"\nexit 0\n',
    )
    _write_stub(
        shim_dir / "python3",
        '#!/bin/sh\n'
        'case "${2:-}" in\n'
        '  *ensure_data_home*) exec "$ENTRYPOINT_TEST_REAL_PYTHON" "$@" ;;\n'
        f'  *detect_backend*) exit {0 if probe_backend_available else 1} ;;\n'
        '  *) exec "$ENTRYPOINT_TEST_REAL_PYTHON" "$@" ;;\n'
        'esac\n',
    )
    _write_stub(
        shim_dir / "tini",
        '#!/bin/sh\n[ "$1" = "--" ] && shift\nexec "$@"\n',
    )

    env = {
        # Minimal, hermetic environment: no inherited KIROCREW_HOME and no
        # inherited real HOME so the resolver sees exactly what we lay out.
        "HOME": str(home),
        "PATH": f"{shim_dir}:{os.environ.get('PATH', '')}",
        "ENTRYPOINT_TEST_ENV_CAPTURE": str(home / "captured-env"),
        "ENTRYPOINT_TEST_REAL_PYTHON": resolver_python or sys.executable,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        # "doctor" (any non-gateway arg) exercises the passthrough exec —
        # the seeding/sync logic is command-independent and runs first.
        ["/bin/sh", str(ENTRYPOINT), "doctor", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# ── Sandbox posture ──────────────────────────────────────────────────────


def test_first_run_with_backend_seeds_sandbox_auto(tmp_path: Path) -> None:
    """When the probe reports a working inner sandbox backend, the
    entrypoint must seed ``agent.sandbox="auto"`` to ENGAGE it: the
    product's default mode is "off", which short-circuits detect_backend()
    to "none" at exec time — a working backend changes nothing unless the
    mode is switched. This is the security-preferred posture (agent
    subprocesses namespace-isolated from gateway credentials)."""
    result = _run_entrypoint(tmp_path, probe_backend_available=True)
    assert result.returncode == 0, result.stderr

    config = tmp_path / ".kiro" / "crew" / "config.json"
    assert config.is_file(), "backend available must seed sandbox=auto"
    assert json.loads(config.read_text(encoding="utf-8")) == SEED_AUTO_JSON
    assert "sandbox=auto" in result.stdout
    assert "sandbox_allow_unsandboxed_exec" not in config.read_text(encoding="utf-8")


def test_first_run_without_backend_stays_fail_closed_without_consent(tmp_path: Path) -> None:
    """No working backend and NO explicit consent: seed sandbox="auto"
    WITHOUT the opt-out. Critically this must not leave config.json absent:
    the product default mode "off" bypasses wrap_argv's enforcement
    entirely (raw argv returned before the fail-closed guard) — only mode
    "auto" routes a missing backend into the audited refusal."""
    result = _run_entrypoint(tmp_path, probe_backend_available=False)
    assert result.returncode == 0, result.stderr

    config = tmp_path / ".kiro" / "crew" / "config.json"
    assert config.is_file(), (
        "config MUST be seeded — an absent config means mode 'off', which "
        "bypasses the fail-closed guard instead of engaging it"
    )
    assert json.loads(config.read_text(encoding="utf-8")) == SEED_AUTO_JSON
    assert "DISABLED (fail-closed)" in result.stdout
    assert "KIROCREW_ALLOW_UNSANDBOXED=1" in result.stdout, (
        "the message must document the explicit consent path"
    )


def test_first_run_without_backend_seeds_opt_out_with_explicit_consent(tmp_path: Path) -> None:
    """With -e KIROCREW_ALLOW_UNSANDBOXED=1 the operator has explicitly
    accepted container-as-boundary. The opt-out rides WITH sandbox="auto"
    so the allowance flows through the guard's audited opt-in path rather
    than mode-off's silent bypass."""
    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        extra_env={"KIROCREW_ALLOW_UNSANDBOXED": "1"},
    )
    assert result.returncode == 0, result.stderr

    config = tmp_path / ".kiro" / "crew" / "config.json"
    assert config.is_file(), "explicit consent must seed the opt-out"
    assert json.loads(config.read_text(encoding="utf-8")) == SEED_CONSENT_JSON
    assert "KIROCREW_ALLOW_UNSANDBOXED=1 given" in result.stdout


def test_empty_mounted_home_is_still_a_first_run(tmp_path: Path) -> None:
    """An EMPTY mounted volume pre-creates the home directory. That is
    still a first run: seeding must key on config.json absence, not on the
    directory's existence, or a fresh `-v` mount ships with agent exec
    unusable AND no opt-out."""
    (tmp_path / ".kiro" / "crew").mkdir(parents=True)
    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        extra_env={"KIROCREW_ALLOW_UNSANDBOXED": "1"},
    )
    assert result.returncode == 0, result.stderr

    config = tmp_path / ".kiro" / "crew" / "config.json"
    assert config.is_file(), "empty mounted home must still seed"
    assert json.loads(config.read_text(encoding="utf-8")) == SEED_CONSENT_JSON


def test_existing_config_is_preserved_byte_identical(tmp_path: Path) -> None:
    """A mounted volume with operator state must come through untouched: the
    entrypoint may print advice but must never rewrite an existing config —
    an operator who deliberately re-enabled the inner sandbox (e.g. running
    with a permissive seccomp profile) must not have that choice reverted
    on restart."""
    home_dir = tmp_path / ".kiro" / "crew"
    home_dir.mkdir(parents=True)
    config = home_dir / "config.json"
    operator_state = (
        '{\n  "agent": {"sandbox_allow_unsandboxed_exec": false},\n'
        '  "dashboard": {"bot_name": "Custom"}\n}\n'
    )
    config.write_text(operator_state, encoding="utf-8")
    before = config.read_bytes()

    result = _run_entrypoint(tmp_path, probe_backend_available=False)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == before, "existing config must be byte-identical"
    # Key present ⇒ no advisory note either.
    assert "sandbox_allow_unsandboxed_exec is not set" not in result.stdout


def test_existing_config_without_key_gets_note_but_no_write(tmp_path: Path) -> None:
    """Existing config missing the sandbox key: advise, never write."""
    home_dir = tmp_path / ".kiro" / "crew"
    home_dir.mkdir(parents=True)
    config = home_dir / "config.json"
    config.write_text('{"dashboard": {"bot_name": "Custom"}}\n', encoding="utf-8")
    before = config.read_bytes()

    result = _run_entrypoint(tmp_path, probe_backend_available=False)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == before
    assert "sandbox_allow_unsandboxed_exec is not set" in result.stdout


def test_explicit_kirocrew_home_env_is_honored(tmp_path: Path) -> None:
    """KIROCREW_HOME overrides the default seed location (parity with the
    backend's env handling)."""
    custom = tmp_path / "custom-home"
    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        extra_env={
            "KIROCREW_HOME": str(custom),
            "KIROCREW_ALLOW_UNSANDBOXED": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert (custom / "config.json").is_file()
    # Default location untouched.
    assert not (tmp_path / ".kiro" / "crew").exists()


def test_kirocrew_home_tilde_is_expanded_like_the_backend(tmp_path: Path) -> None:
    """A tilde override (docker -e KIROCREW_HOME=~/crew-data arrives
    unexpanded) must land under $HOME exactly as the backend resolves it —
    the resolver delegation makes this the REAL product expansion, not a
    shell reimplementation."""
    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        extra_env={
            "KIROCREW_HOME": "~/crew-data",
            "SLACK_BOT_TOKEN": "tilde-secret",
            "KIROCREW_ALLOW_UNSANDBOXED": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    expanded = tmp_path / "crew-data"
    assert (expanded / "config.json").is_file(), "seed must land under $HOME/crew-data"
    assert "tilde-secret" in (expanded / ".env").read_text(encoding="utf-8")
    assert not (tmp_path / "~").exists(), "no literal tilde directory may be created"
    # The override passes through VERBATIM — the gateway applies the
    # identical product expansion at boot, so both resolve the same dir.
    captured = (tmp_path / "captured-env").read_text(encoding="utf-8")
    assert "KIROCREW_HOME=~/crew-data" in captured


def test_system_dir_override_is_rejected_like_the_backend(tmp_path: Path) -> None:
    """KIROCREW_HOME pointing at a system directory is ignored by the
    backend's validation; the entrypoint must follow the resolver to the
    default home instead of aborting on an unwritable config path or
    writing credentials where the gateway will never look. Uses /usr:
    rejected on every platform (macOS resolves /etc to /private/etc BEFORE
    the blocklist check, slipping past it — tracked upstream; the image
    itself is Linux-only where /etc is rejected too)."""
    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        extra_env={
            "KIROCREW_HOME": "/usr",
            "KIROCREW_ALLOW_UNSANDBOXED": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    config = tmp_path / ".kiro" / "crew" / "config.json"
    assert config.is_file(), (
        "rejected override must fall through to the default home, same as "
        "the backend"
    )


def test_broken_resolver_falls_back_to_default_home(tmp_path: Path) -> None:
    """If the product resolver cannot run at all (broken install), the
    entrypoint must not die — it warns and uses the default home so the
    container still boots to a diagnosable state."""
    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        resolver_python="/usr/bin/false",
        extra_env={"KIROCREW_ALLOW_UNSANDBOXED": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "could not resolve the data home" in result.stderr
    assert (tmp_path / ".kiro" / "crew" / "config.json").is_file()


# ── Credential env -> .env sync + scrub ─────────────────────────────────


def test_channel_credentials_move_to_env_file_and_leave_environ(tmp_path: Path) -> None:
    """Credentials delivered as container env must land in the product's
    .env file (0600) and be ABSENT from the environment the gateway is
    exec'd with — otherwise they sit in /proc/<pid>/environ for the
    container's lifetime, readable by any same-UID process (the
    execve-time snapshot is immune to runtime scrubs)."""
    secret = "xoxb-testvalue-123"
    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        extra_env={"SLACK_BOT_TOKEN": secret, "WECOM_SECRET": "wc-s3cret"},
    )
    assert result.returncode == 0, result.stderr

    env_file = tmp_path / ".kiro" / "crew" / ".env"
    assert env_file.is_file()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    content = env_file.read_text(encoding="utf-8")
    assert f"SLACK_BOT_TOKEN={secret}" in content
    assert "WECOM_SECRET=wc-s3cret" in content

    captured = (tmp_path / "captured-env").read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN" not in captured, (
        "the exec'd gateway environment must not carry the credential"
    )
    assert "WECOM_SECRET" not in captured
    # The secret value itself must not be echoed to stdout/stderr (docker
    # logs are readable indefinitely).
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_credential_sync_replaces_key_and_preserves_other_lines(tmp_path: Path) -> None:
    """Re-delivering a credential updates its line in place (env wins —
    same precedence the product applies) without clobbering operator-added
    lines or comments in .env, and without duplicating the key."""
    home_dir = tmp_path / ".kiro" / "crew"
    home_dir.mkdir(parents=True)
    env_file = home_dir / ".env"
    env_file.write_text(
        "# operator comment\nSLACK_BOT_TOKEN=old-value\nCUSTOM_KEY=keep-me\n",
        encoding="utf-8",
    )
    (home_dir / "config.json").write_text("{}", encoding="utf-8")

    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        extra_env={"SLACK_BOT_TOKEN": "new-value"},
    )
    assert result.returncode == 0, result.stderr

    content = env_file.read_text(encoding="utf-8")
    assert "# operator comment" in content
    assert "CUSTOM_KEY=keep-me" in content
    assert "SLACK_BOT_TOKEN=new-value" in content
    assert "old-value" not in content
    assert content.count("SLACK_BOT_TOKEN=") == 1


def test_empty_credential_env_vars_touch_nothing(tmp_path: Path) -> None:
    """Compose maps every channel var with `${VAR:-}` — empty strings must
    not create .env entries or an empty .env file."""
    home_dir = tmp_path / ".kiro" / "crew"
    home_dir.mkdir(parents=True)
    (home_dir / "config.json").write_text("{}", encoding="utf-8")

    result = _run_entrypoint(
        tmp_path,
        probe_backend_available=False,
        extra_env={"SLACK_BOT_TOKEN": "", "DISCORD_BOT_TOKEN": ""},
    )
    assert result.returncode == 0, result.stderr
    assert not (home_dir / ".env").exists()


def test_unreadable_env_file_is_preserved_not_replaced(tmp_path: Path) -> None:
    """A grep read failure (exit >= 2, e.g. unreadable .env) must NOT be
    conflated with "no matching line" (exit 1): the sync would otherwise
    replace the whole file with only the current key, destroying every
    other stored credential. On failure the file stays untouched and the
    variable stays in the environment (the gateway reads env directly)."""
    home_dir = tmp_path / ".kiro" / "crew"
    home_dir.mkdir(parents=True)
    (home_dir / "config.json").write_text("{}", encoding="utf-8")
    env_file = home_dir / ".env"
    env_file.write_text(
        "OTHER_CRED=must-survive\nSLACK_BOT_TOKEN=old\n", encoding="utf-8"
    )
    before = env_file.read_bytes()
    env_file.chmod(0)  # unreadable -> grep exits 2

    try:
        result = _run_entrypoint(
            tmp_path,
            probe_backend_available=False,
            extra_env={"SLACK_BOT_TOKEN": "new-value"},
        )
    finally:
        env_file.chmod(0o600)

    assert result.returncode == 0, result.stderr
    assert env_file.read_bytes() == before, (
        ".env must be byte-identical after a failed read — never truncated "
        "to the single current key"
    )
    assert "WARNING: could not read" in result.stderr
    # The credential must still reach the gateway via its environment.
    captured = (tmp_path / "captured-env").read_text(encoding="utf-8")
    assert "SLACK_BOT_TOKEN=new-value" in captured
