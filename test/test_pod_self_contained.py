"""A pod is self-contained: its own venv leads PATH, and pod-scoped commands run
against the pod rather than the machine-wide install.

The bug these guard: ``cfg.gateway_path`` begins with ``~/.local/bin``, so a bare
``kirocrew`` inside a pod used to resolve the GLOBAL launcher shim — meaning a pod
exercised the global install instead of the checkout under test, and its boot path
depended on a symlink it does not own.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from kiro_crew.pod import provision as prov
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


def _pod_cfg(tmp_path: Path) -> PodConfig:
    """A PodConfig rooted entirely under tmp_path, with the real default PATH shape
    (``~/.local/bin`` first) so the ordering assertions are meaningful."""
    return PodConfig(
        pod_root=tmp_path / "pods",
        pods_dir=tmp_path / "podenv",
        artifacts_dir=tmp_path / "artifacts",
        base_port=7810,
        live_port=5476,
        unit_prefix="kirocrew-pod@",
        gateway_path=os.pathsep.join([str(tmp_path / ".local" / "bin"), "/usr/bin", "/bin"]),
        repo_hint=None,
        worktrees_root=None,
    )


def _provisioned_checkout(tmp_path: Path, name: str = "wt-feature") -> Path:
    """A checkout with a provisioned venv entrypoint, as `pod provision` leaves it."""
    checkout = tmp_path / name
    binary = prov.venv_bin(checkout)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return checkout


# --------------------------------------------------------------------------- #
# PATH ordering — the pod's own venv must win over the global shim dir
# --------------------------------------------------------------------------- #
def test_pod_env_puts_the_checkout_venv_ahead_of_the_global_shim_dir(tmp_path):
    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)

    env = rt.build_pod_env(cfg, tmp_path / "home", 7900, checkout)

    entries = env["PATH"].split(os.pathsep)
    venv_bin = str(prov.venv_bin_dir(checkout))
    assert entries[0] == venv_bin, "the pod's own venv must lead PATH"
    # The global shim dir is still reachable, just no longer first.
    shim_dir = str(tmp_path / ".local" / "bin")
    assert shim_dir in entries
    assert entries.index(venv_bin) < entries.index(shim_dir)


def test_pod_env_scrubs_the_live_gateways_bound_port(tmp_path, monkeypatch):
    """KIROCREW_BOUND_PORT must never cross the pod boundary.

    A gateway-descended caller (an agent bash turn) inherits the LIVE
    gateway's bound-port export. Inside a pod env it names the wrong plane —
    the pod's own KIROCREW_PORT is the target — and resolve_client_port reads
    it as a fallback, so leaving it in would let pod client commands aim at
    the live gateway if precedence ever changed. Scrubbed unconditionally.
    """
    monkeypatch.setenv("KIROCREW_BOUND_PORT", "5476")
    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)

    env = rt.build_pod_env(cfg, tmp_path / "home", 7900, checkout)

    assert "KIROCREW_BOUND_PORT" not in env
    assert env["KIROCREW_PORT"] == "7900"


def test_pod_env_still_isolates_home_and_port(tmp_path):
    """The PATH change must not disturb the existing isolation keys."""
    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    home = tmp_path / "podhome"

    env = rt.build_pod_env(cfg, home, 7900, checkout)

    assert env["KIROCREW_HOME"] == str(home)
    assert env["KIROCREW_PORT"] == "7900"
    assert env["KIROCREW_PROJECT_DIR"] == str(checkout)


def test_pod_env_scrubs_messaging_credentials(tmp_path, monkeypatch):
    """Pinned because pod_context() hands this same env to arbitrary commands —
    a regression here would let `pod exec` act as the live messaging identity."""
    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-live")
    monkeypatch.setenv("WECOM_SECRET", "live")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "live")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "keep-me")

    env = rt.build_pod_env(cfg, tmp_path / "home", 7900, checkout)

    assert "SLACK_BOT_TOKEN" not in env
    assert "WECOM_SECRET" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert env["AWS_SESSION_TOKEN"] == "keep-me", "AWS creds are kept on purpose"


# --------------------------------------------------------------------------- #
# pod_context — the single seam every pod-scoped command resolves through
# --------------------------------------------------------------------------- #
def test_pod_context_resolves_the_pods_own_binary_and_env(tmp_path, monkeypatch):
    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    monkeypatch.setattr(rt, "read_env_file", lambda c, n: {"CHECKOUT": str(checkout)})

    bin_path, env = rt.pod_context(cfg, "wt-feature")

    assert bin_path == prov.venv_bin(checkout)
    assert env["KIROCREW_HOME"] == str(cfg.home_dir("wt-feature"))
    assert env["PATH"].split(os.pathsep)[0] == str(prov.venv_bin_dir(checkout))


def test_pod_context_errors_when_no_checkout_is_pinned(tmp_path, monkeypatch):
    cfg = _pod_cfg(tmp_path)
    monkeypatch.setattr(rt, "read_env_file", lambda c, n: {})

    with pytest.raises(rt.PodError, match="no pinned checkout"):
        rt.pod_context(cfg, "wt-feature")


def test_pod_context_errors_when_the_venv_is_not_provisioned(tmp_path, monkeypatch):
    cfg = _pod_cfg(tmp_path)
    bare = tmp_path / "wt-bare"
    bare.mkdir()
    monkeypatch.setattr(rt, "read_env_file", lambda c, n: {"CHECKOUT": str(bare)})

    with pytest.raises(rt.PodError, match="provision"):
        rt.pod_context(cfg, "wt-feature")


def test_exec_in_pod_execs_the_pods_binary_with_the_pod_env(tmp_path, monkeypatch):
    """`pod exec` must exec the POD's binary (never the global one) and pass the
    isolated env through, so the command hits the pod's data and port."""
    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    monkeypatch.setattr(rt, "read_env_file", lambda c, n: {"CHECKOUT": str(checkout)})
    seen: dict[str, object] = {}

    def _fake_execve(path, argv, env):
        seen.update(path=path, argv=argv, env=env)
        raise SystemExit(0)  # stand in for "process replaced"

    monkeypatch.setattr(rt.os, "execve", _fake_execve)

    with pytest.raises(SystemExit):
        rt.exec_in_pod(cfg, "wt-feature", ["cron", "list"])

    expected = str(prov.venv_bin(checkout))
    assert seen["path"] == expected
    assert seen["argv"] == [expected, "cron", "list"]
    assert seen["env"]["KIROCREW_HOME"] == str(cfg.home_dir("wt-feature"))  # type: ignore[index]


# --------------------------------------------------------------------------- #
# `pod exec` accepts only POD-SCOPED verbs (allowlist), never host management.
#
# Denying by name was incomplete three times over — `stop`, then `restart` for a
# second reason, then `service uninstall` — because the set of host-scoped verbs is
# open-ended. An allowlist makes an unlisted verb (including a newly added one)
# fail closed instead of silently operating on the user's live machine.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "verb",
    [
        "stop",
        "restart",
        "service",
        "setup",
        "update",
        "gateway",
        "cloud",
        "browse",
        "pod",
        "app",
        "logs",
        "snapshot",
        "run",
        "doctor",
        "tui",
        "chat",
    ],
)
def test_host_scoped_verbs_are_refused(verb):
    with pytest.raises(rt.PodError, match=f"refusing `{verb}`"):
        rt.require_pod_safe_verb([verb], "wt-feature")


def test_app_is_refused_because_it_rewrites_the_host_agent_registry():
    """`apps/bridges.py` symlinks app agents into `Path.home()/.kiro/agents` and
    edits `~/.kiro/settings/mcp.json` — the HOST registry. A pod install/uninstall
    would replace or delete symlinks the live gateway depends on."""
    from kiro_crew.apps import bridges

    # Resolved through the accessor, not the module constant: since #874 the
    # constant is an opt-in override that is ``None`` by default, so reading it
    # directly would assert the override rather than the path bridges actually
    # writes to. The claim under test is unchanged -- bridges targets the
    # machine-wide HOST registry, which is why a pod may not run `app`.
    assert bridges._kiro_agents_dir() == Path.home() / ".kiro" / "agents"
    assert "app" not in rt._POD_SAFE_VERBS


def test_service_uninstall_is_refused():
    """The specific escalation an earlier denylist missed: it would stop the live
    gateway AND delete its machine-wide service definition."""
    with pytest.raises(rt.PodError, match="refusing `service`"):
        rt.require_pod_safe_verb(["service", "uninstall"], "wt-feature")


@pytest.mark.parametrize("verb", ["status", "cron", "artifact", "learn", "memory", "agent"])
def test_pod_scoped_verbs_pass_through(verb):
    assert rt.require_pod_safe_verb([verb], "wt-feature") is None


def test_flags_after_the_verb_are_untouched():
    assert rt.require_pod_safe_verb(["status", "--json"], "wt-feature") is None


@pytest.mark.parametrize("argv", [["-v", "status"], ["--log-level", "DEBUG", "stop"], []])
def test_a_leading_flag_or_empty_argv_is_refused(argv):
    """The verb must come first. Allowing flags ahead of it is what made the old
    denylist bypassable: a flag's VALUE is indistinguishable from a verb."""
    with pytest.raises(rt.PodError):
        rt.require_pod_safe_verb(list(argv), "wt-feature")


def test_the_refusal_names_a_pod_native_equivalent_where_one_exists():
    with pytest.raises(rt.PodError) as exc:
        rt.require_pod_safe_verb(["restart"], "wt-feature")
    assert "kirocrew pod down wt-feature" in str(exc.value)
    assert "pod up wt-feature" in str(exc.value)


def test_the_refusal_lists_the_allowed_verbs_otherwise():
    with pytest.raises(rt.PodError) as exc:
        rt.require_pod_safe_verb(["service"], "wt-feature")
    assert "status" in str(exc.value) and "cron" in str(exc.value)


def test_no_allowed_verb_is_host_scoped():
    """Drift guard: the allowlist must never grow to include a verb that manages
    the host rather than one instance."""
    host_scoped = {
        "setup",
        "update",
        "stop",
        "restart",
        "service",
        "gateway",
        "pod",
        "cloud",
        "browse",
        "manifest",
        "app",
        "logs",
        "snapshot",
        "run",
        "doctor",
        "tui",
        "chat",
    }
    assert not (rt._POD_SAFE_VERBS & host_scoped)


# --------------------------------------------------------------------------- #
# The audit trail must record the decision actually taken.
#
# Emitting "allowed" and then refusing the verb makes SEL attest the opposite of
# what happened — worse than no entry, because it is trusted.
# --------------------------------------------------------------------------- #
def test_a_refused_exec_is_audited_as_denied_not_allowed(tmp_path, monkeypatch):
    from kiro_crew.pod import cli as pod_cli

    cfg = _pod_cfg(tmp_path)
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pod_cli,
        "_audit",
        lambda op, outcome, resources="", error="": seen.append((op, outcome)),
    )
    monkeypatch.setattr(pod_cli, "_die", lambda msg: (_ for _ in ()).throw(SystemExit(2)))

    with pytest.raises(SystemExit):
        pod_cli._exec(cfg, argparse.Namespace(name="wt-feature", argv=["service", "uninstall"]))

    assert seen == [("pod.exec", "denied")], f"expected a single denied entry, got {seen}"


def test_an_allowed_exec_is_audited_as_allowed(tmp_path, monkeypatch):
    from kiro_crew.pod import cli as pod_cli

    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    monkeypatch.setattr(rt, "read_env_file", lambda c, n: {"CHECKOUT": str(checkout)})
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pod_cli,
        "_audit",
        lambda op, outcome, resources="", error="": seen.append((op, outcome)),
    )
    monkeypatch.setattr(rt.os, "execve", lambda *a: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        pod_cli._exec(cfg, argparse.Namespace(name="wt-feature", argv=["status"]))

    assert seen == [("pod.exec", "allowed")]


def test_exec_in_pod_refuses_before_touching_the_pod(tmp_path, monkeypatch):
    """The refusal must precede resolution, so it holds even for a pod that has
    no pinned checkout — and must never reach execve."""
    cfg = _pod_cfg(tmp_path)
    monkeypatch.setattr(
        rt.os, "execve", lambda *a: pytest.fail("execve must not be reached")
    )

    with pytest.raises(rt.PodError, match="refusing `service`"):
        rt.exec_in_pod(cfg, "wt-feature", ["service", "uninstall"])


def test_logs_is_refused_and_points_at_the_pod_journal():
    """`cli_server._logs_cmd` runs `journalctl -u <host service unit>`, so inside a
    pod it would confidently show the LIVE gateway's journal."""
    with pytest.raises(rt.PodError) as exc:
        rt.require_pod_safe_verb(["logs"], "wt-feature")
    assert "kirocrew pod logs wt-feature" in str(exc.value)


def test_snapshot_is_refused_because_its_destination_is_configurable():
    """`snapshot_dir` is a config field and `--keep N` DELETES older archives, so a
    pod seeded from the live config could prune the user's real backups —
    `sanitized_seed_config` only forces tunnel/telegram/wecom off."""
    from kiro_crew.config.loader import KiroCrewConfig

    assert hasattr(KiroCrewConfig, "__dataclass_fields__")
    assert "snapshot_dir" in KiroCrewConfig.__dataclass_fields__
    with pytest.raises(rt.PodError, match="refusing `snapshot`"):
        rt.require_pod_safe_verb(["snapshot", "--keep", "1"], "wt-feature")


# --------------------------------------------------------------------------- #
# The pod must not inherit the LIVE workspace.
#
# workspace_root() falls through to a platform default under the real HOME when
# neither KIROCREW_WORKSPACE nor config_dir()/workspace_dir is present — which a
# fresh pod home has neither of. Every agent turn would then edit live files.
# --------------------------------------------------------------------------- #
def test_pod_env_scopes_the_workspace_into_the_pod_home(tmp_path):
    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    home = cfg.home_dir("wt-feature")

    env = rt.build_pod_env(cfg, home, 7900, checkout)

    assert env["KIROCREW_WORKSPACE"] == str(home / "workspace")
    # Inside the pod HOME, so zero-residue teardown removes it.
    assert Path(env["KIROCREW_WORKSPACE"]).is_relative_to(home)


def test_the_pod_workspace_is_not_the_live_workspace(tmp_path, monkeypatch):
    """The concrete regression: with no override, resolution reaches a default
    under the real HOME. The pod's value must not be that."""
    from kiro_crew.config import loader

    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    env = rt.build_pod_env(cfg, cfg.home_dir("wt-feature"), 7900, checkout)

    monkeypatch.delenv("KIROCREW_WORKSPACE", raising=False)
    live = loader.workspace_root()

    assert Path(env["KIROCREW_WORKSPACE"]).resolve() != live.resolve()


def test_the_override_actually_drives_workspace_root(tmp_path, monkeypatch):
    """Pin that KIROCREW_WORKSPACE is the mechanism workspace_root() honours, so
    the fix cannot silently stop working if resolution is reordered."""
    from kiro_crew.config import loader

    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    env = rt.build_pod_env(cfg, cfg.home_dir("wt-feature"), 7900, checkout)

    monkeypatch.setenv("KIROCREW_WORKSPACE", env["KIROCREW_WORKSPACE"])

    assert loader.workspace_root().resolve() == Path(env["KIROCREW_WORKSPACE"]).resolve()


def test_exec_in_pod_runs_inside_the_pod_workspace(tmp_path, monkeypatch):
    """Relative paths must resolve inside the pod, not the invoking shell's cwd."""
    cfg = _pod_cfg(tmp_path)
    checkout = _provisioned_checkout(tmp_path)
    monkeypatch.setattr(rt, "read_env_file", lambda c, n: {"CHECKOUT": str(checkout)})
    seen: dict[str, object] = {}
    monkeypatch.setattr(rt.os, "chdir", lambda p: seen.update(cwd=str(p)))
    monkeypatch.setattr(rt.os, "execve", lambda *a: (_ for _ in ()).throw(SystemExit(0)))

    with pytest.raises(SystemExit):
        rt.exec_in_pod(cfg, "wt-feature", ["status"])

    assert seen["cwd"] == str(cfg.home_dir("wt-feature") / "workspace")


def test_run_is_refused_because_it_writes_beside_the_spec():
    """`task_reporter.save_progress` writes TASK_PROGRESS.md into
    `Path(spec_path).parent`, so `run /host/TASK.md` writes outside the pod — an
    implicit write derived from an INPUT path, which the inclusion test excludes."""
    import inspect

    from kiro_crew import task_reporter

    src = inspect.getsource(task_reporter.save_progress)
    assert "spec_path" in src and "parent" in src, "premise: progress goes beside the spec"
    with pytest.raises(rt.PodError, match="refusing `run`"):
        rt.require_pod_safe_verb(["run", "/host/TASK.md"], "wt-feature")


def test_chat_is_refused_because_chat_tui_reaches_the_live_port():
    """`cli_chat._tui` resolves the CONFIG dashboard port with a literal 5476
    fallback and never reads KIROCREW_PORT, and `chat --tui` branches into it — so
    excluding only `tui` left the hole open."""
    import inspect

    from kiro_crew import cli_chat

    src = inspect.getsource(cli_chat)
    assert "5476" in src, "premise: _tui carries a hardcoded live-port fallback"
    assert "resolve_client_port" not in src, "premise: _tui bypasses the port resolver"
    for verb in ("chat", "tui"):
        with pytest.raises(rt.PodError, match=f"refusing `{verb}`"):
            rt.require_pod_safe_verb([verb], "wt-feature")
