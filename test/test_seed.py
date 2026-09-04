"""Tests for ``kiro_crew.seed`` — Phase 1.A walking skeleton.

Scope: one fixture (``empty``), one guardrail (``KIROCREW_HOME`` unset), plus a
regression guard for unknown fixture names. Non-empty target, main-home guardrail,
``--seed-replace``, and PRD acceptance tests 4-7 land in 1.B / 1.C.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import requires_symlinks
from kiro_crew import seed as seed_mod

# A test here spawns a real `python -m kiro_crew gateway --help` child interpreter;
# pin the module to a dedicated xdist worker so concurrent cold-starts under -n auto
# don't starve each other / blow the 30s timeout. Requires --dist loadgroup.
pytestmark = pytest.mark.xdist_group(name="subprocess_spawn")


def test_seed_empty_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``kirocrew seed --fixture empty`` writes fixture.yaml into $KIROCREW_HOME."""
    # copytree refuses an existing dst, so don't pre-create it.
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    seed_mod.seed("empty")

    out_file = target / "fixture.yaml"
    assert out_file.is_file(), f"expected {out_file} to exist after seed"
    src_file = Path(str(seed_mod._fixtures_root())) / "empty" / "fixture.yaml"
    assert out_file.read_bytes() == src_file.read_bytes()
    assert "schema-version:" in out_file.read_text(encoding="utf-8")


@pytest.mark.skipif(
    not seed_mod.pinned_fs.supports_pinned_tree_walk(),
    reason="descriptor-relative fixture copy is POSIX-only",
)
def test_pinned_fixture_copy_publishes_completion_manifest_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "home"
    destination.mkdir()
    copied: list[str] = []
    real_copy = seed_mod.pinned_fs.copy_file_pinned

    def _record(*args, **kwargs):
        copied.append(str(kwargs.get("dst_name")))
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(seed_mod.pinned_fs, "copy_file_pinned", _record)
    dst_fd = os.open(destination, seed_mod.pinned_fs.dir_flags())
    try:
        seed_mod.copy_fixture_into_dir_fd("minimal", dst_fd)
        assert seed_mod.FIXTURE_MANIFEST not in copied
        assert not (destination / seed_mod.FIXTURE_MANIFEST).exists()
        copied.append("<setup>")
        seed_mod.publish_fixture_manifest("minimal", dst_fd)
    finally:
        os.close(dst_fd)

    assert copied[-2:] == ["<setup>", seed_mod.FIXTURE_MANIFEST]
    assert (destination / seed_mod.FIXTURE_MANIFEST).is_file()


def test_seed_unset_home_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset $KIROCREW_HOME raises SeedError with exit code 2."""
    monkeypatch.delenv("KIROCREW_HOME", raising=False)

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert "KIROCREW_HOME" in str(excinfo.value)


def test_seed_unknown_fixture_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown fixture name raises SeedError with exit code 2.

    Regression guard for ``_resolve_fixture`` — if fixture dir lookup ever
    silently falls through to ``copytree``, the user would hit a raw
    ``FileNotFoundError`` instead of a friendly guardrail error.
    """
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("this-fixture-does-not-exist")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    msg = str(excinfo.value)
    assert "unknown fixture" in msg
    # Discoverability: the error must list the shipped fixtures so the
    # user doesn't have to read ``tests_fixtures/`` or the PRD. ``empty``
    # is the only fixture in 1.A; ``rich`` lands in 1.C.
    assert "Available fixtures:" in msg
    assert "empty" in msg
    # Target must not be written when fixture lookup fails.
    assert not target.exists()


@pytest.mark.parametrize(
    "name",
    [
        "../../.ssh",
        "../.aws",
        "foo/bar",
        "foo\\bar",
        "..",
        "./empty",
        ".",
        "",
        "./",
        # SEC-1: NUL byte + control chars must be caught at the empty-or-root
        # gate before ``(root / name).resolve()`` raises ``ValueError`` and
        # escapes ``seed_cmd``'s ``except SeedError`` (bypassing both the
        # ``seed: error:`` ASCII prefix AND the SEL audit emit).
        "foo\x00bar",
        "\x00",
        "foo\nbar",
    ],
)
def test_seed_path_traversal_rejected(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names with path separators or '..' are rejected before copytree runs.

    Regression guard against review-bot security-controls findings: (1) names
    like ``../../.ssh`` would escape the fixtures tree; (2) names like
    ``"."`` or ``""`` resolve to the fixtures root itself, which would
    ``copytree`` the entire ``tests_fixtures/`` tree into ``$KIROCREW_HOME``.

    Also pins *which* gate rejects each input class — without the
    per-branch message assertion below, the post-resolve ``candidate ==
    resolved_root`` gate would silently catch ``"."`` / ``""`` / ``"./"``
    even if the upfront empty-name check were deleted, and CI would still
    pass. Pinning the error message forces a test failure if the guard
    ordering is ever reshuffled.
    """
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed(name)

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    # Target must not be written.
    assert not target.exists()

    # Pin which gate rejected each name. New "empty-or-root" cases must
    # hit the first gate; the old path-separator/``..`` cases must hit
    # the second. ``"./empty"`` is interesting: it has ``/`` so it hits
    # the separator gate, not the empty-or-root gate. NUL-byte / control-
    # char names (``"foo\x00bar"``, ``"foo\nbar"``) also hit gate 1 via
    # the ``ord(c) < 0x20`` check (SEC-1 regression guard).
    if name in ("", ".", "./") or any(ord(c) < 0x20 for c in name):
        assert "empty or refers to the root" in str(excinfo.value), (
            f"expected empty-or-root gate to reject {name!r}, got: {excinfo.value}"
        )
    else:
        assert "path separators or '..'" in str(excinfo.value), (
            f"expected path-separator gate to reject {name!r}, got: {excinfo.value}"
        )


@patch("kiro_crew.seed.sel")
def test_seed_cmd_exit_code_on_unset(
    mock_sel: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """seed_cmd returns 2 and prints to stderr when $KIROCREW_HOME is unset.

    ``sel`` is patched to keep the test hermetic: the real ``sel()``
    singleton resolves its log dir from ``Path.home()``, NOT
    ``$KIROCREW_HOME``, and would otherwise append real audit events to
    the dev's own ``~/.kirocrew/security_events.jsonl`` HMAC chain.
    """
    monkeypatch.delenv("KIROCREW_HOME", raising=False)

    args = type("Args", (), {"seed": "empty"})()
    rc = seed_mod.seed_cmd(args)

    assert rc == seed_mod.EXIT_GUARDRAIL
    err = capsys.readouterr().err
    assert "KIROCREW_HOME" in err
    # Plain ASCII prefix so non-UTF-8 terminals don't swallow the message.
    assert err.startswith("seed: error:")


def test_seed_cli_flag_registered(tmp_path: Path) -> None:
    """``kirocrew gateway --help`` mentions ``--seed FIXTURE``.

    Tracer-bullet acceptance from the ticket: prove the CLI
    wiring end-to-end. In Phase 1.A the seed primitive is invoked as
    ``kirocrew gateway --seed <fixture>`` (it seeds ``$KIROCREW_HOME``
    and THEN continues into the gateway event loop) — we can't let the
    subprocess actually run because ``run_gateway`` is a long-lived
    server. ``--help`` exits 0 after printing usage, which is enough to
    verify the flag is registered and the seed_cmd wiring imports clean.
    """
    repo_root = Path(__file__).resolve().parent.parent
    import os as _os

    env = {**_os.environ, "HOME": str(tmp_path)}
    # Preserve user site-packages: overriding HOME loses ~/.local/lib/pythonX.Y
    # where deps like croniter/cron_descriptor live when not system-installed.
    real_home = _os.environ.get("HOME", "")
    if real_home:
        import site
        user_site = site.getusersitepackages()
        if isinstance(user_site, str) and _os.path.isdir(user_site):
            existing_pp = env.get("PYTHONPATH", "")
            if existing_pp:
                env["PYTHONPATH"] = user_site + _os.pathsep + existing_pp
            else:
                env["PYTHONPATH"] = user_site
    # Guard against trailing separator when PYTHONPATH is unset — a trailing
    # ":" on POSIX adds CWD to sys.path, which would import unexpected
    # modules depending on where pytest runs.
    existing_pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(repo_root / "src") + (
        _os.pathsep + existing_pypath if existing_pypath else ""
    )
    env["KIROCREW_PROJECT_DIR"] = str(repo_root)

    result = subprocess.run(
        [sys.executable, "-m", "kiro_crew", "gateway", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"expected exit 0 from --help, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # The flag must be registered and documented.
    assert "--seed" in result.stdout
    assert "FIXTURE" in result.stdout


# ------------------------------------------------------------------
# SEL audit regression tests — pin the emission contract so a future
# refactor can't silently remove the ``sel().log_api_access(...)``
# call or swap the ``outcome`` enum values. Pattern matches
# ``test/test_enterprise.py::test_allowlist_add_emits_audit`` and
# ``test/test_token_auth.py::test_refresh_emits_denied_audit``.
# ------------------------------------------------------------------


@patch("kiro_crew.seed.sel")
def test_seed_cmd_emits_sel_audit_on_success(
    mock_sel: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path emits ``outcome="allowed"`` with fixture name + target_set flag."""
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    args = type("Args", (), {"seed": "empty"})()
    assert seed_mod.seed_cmd(args) == seed_mod.EXIT_OK

    kw = mock_sel().log_api_access.call_args.kwargs
    assert kw["caller"] == "cli"
    assert kw["operation"] == "seed"
    assert kw["outcome"] == "allowed"
    assert kw["source"] == "cli"
    assert "fixture='empty'" in kw["resources"]
    # ``target_set=True`` proves we log the presence-flag, not the raw
    # ``$KIROCREW_HOME`` value (per pre-review finding on leakage).
    assert "target_set=True" in kw["resources"]
    # Regression guard: the raw target path must NEVER appear in the
    # audit resources string. A future refactor adding
    # ``f"... target={target!r}"`` would silently leak the path.
    assert str(target) not in kw["resources"]


@patch("kiro_crew.seed.sel")
def test_seed_cmd_emits_sel_audit_on_rail_denied(
    mock_sel: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset $KIROCREW_HOME guardrail emits ``outcome="denied"`` with ``guardrail`` tag."""
    monkeypatch.delenv("KIROCREW_HOME", raising=False)

    args = type("Args", (), {"seed": "empty"})()
    assert seed_mod.seed_cmd(args) == seed_mod.EXIT_GUARDRAIL

    kw = mock_sel().log_api_access.call_args.kwargs
    assert kw["outcome"] == "denied"
    assert "fixture='empty'" in kw["resources"]
    # Guardrail-tag discipline: audit stream uses short code-controlled
    # identifiers, not the exception message (which embeds user-influenced
    # paths). ``guardrail=unset_home`` is the constant for this denial.
    assert f"guardrail={seed_mod.SeedError.GUARDRAIL_UNSET_HOME}" in kw["resources"]


@patch("kiro_crew.seed.sel")
def test_seed_cmd_emits_sel_audit_on_path_traversal_denied(
    mock_sel: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path-traversal fixture name emits ``outcome="denied"`` with the bad name."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    args = type("Args", (), {"seed": "../../.ssh"})()
    assert seed_mod.seed_cmd(args) == seed_mod.EXIT_GUARDRAIL

    kw = mock_sel().log_api_access.call_args.kwargs
    assert kw["outcome"] == "denied"
    # The full adversarial input must appear verbatim in the audit log so
    # an SOC reviewing SEL events can reconstruct the attack attempt.
    assert "fixture='../../.ssh'" in kw["resources"]
    # Path-traversal hits the bad-name guardrail.
    assert f"guardrail={seed_mod.SeedError.GUARDRAIL_BAD_NAME}" in kw["resources"]


@patch("kiro_crew.seed.sel", side_effect=OSError("read-only HOME"))
def test_seed_cmd_safe_audit_swallows_sel_init_failure(
    mock_sel: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``sel()`` itself raises (e.g. read-only $HOME in sandboxed CI),
    ``_safe_audit`` must swallow the exception so the CLI's exit-code
    contract is preserved. Regression guard against the pre-review
    security finding that ``SecurityEventLog.__init__`` does filesystem
    I/O which can fail."""
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    args = type("Args", (), {"seed": "empty"})()
    # Must NOT raise — the audit failure is swallowed by ``_safe_audit``.
    rc = seed_mod.seed_cmd(args)
    assert rc == seed_mod.EXIT_OK
    # And the actual seed still ran: target was populated.
    assert (target / "fixture.yaml").is_file()


@patch("kiro_crew.seed.sel")
def test_seed_cmd_emits_sel_audit_on_copytree_oserror(
    mock_sel: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``shutil.copytree`` raising ``OSError`` emits ``outcome="error"``
    and maps to ``EXIT_IO_ERROR``.

    Regression guard for two pre-review findings:
    1. correctness — before rev 3, an uncaught ``OSError`` from ``copytree``
       propagated as a raw Python traceback, bypassing both the
       ``seed: error:`` ASCII prefix contract and the SEL audit emit.
    2. tests — the ``outcome="denied" if code == EXIT_GUARDRAIL else "error"``
       ternary in ``seed_cmd`` had no coverage of the ``error`` branch.

    Triggers the failure by patching ``shutil.copytree`` to raise a disk-
    full-style ``OSError``. 1.A's ``FileExistsError`` trigger (pre-creating
    ``dst``) no longer works after 1.B because empty-dir targets are
    accepted — and populated targets hit the non-empty guardrail (``denied``,
    not ``error``).
    """
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    def _raise(*_a, **_kw):
        raise OSError("[Errno 28] No space left on device")

    monkeypatch.setattr(seed_mod.shutil, "copytree", _raise)

    args = type("Args", (), {"seed": "empty"})()
    rc = seed_mod.seed_cmd(args)

    assert rc == seed_mod.EXIT_IO_ERROR
    # ``seed: error:`` prefix contract: plain-ASCII, never a traceback.
    err = capsys.readouterr().err
    assert err.startswith("seed: error:"), (
        f"expected ASCII error prefix, got: {err!r}"
    )
    # SEL audit event fires with outcome="error".
    kw = mock_sel().log_api_access.call_args.kwargs
    assert kw["outcome"] == "error"
    assert "fixture='empty'" in kw["resources"]
    # Error type is named in the audit so operators can triage without
    # re-running the failure (OSError vs PermissionError vs FileExistsError etc.).
    assert "OSError" in kw["resources"]


@patch("kiro_crew.seed.sel", side_effect=OSError("read-only HOME"))
def test_seed_cmd_safe_audit_logs_warning_on_swallowed_failure(
    mock_sel: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: ``_safe_audit`` must log ``.warning`` (not ``.debug``) on failures.

    review-bot post #7 (security-controls, importance=1,
    rev 3) + post #10 (default, importance=0, rev 4): the original
    bare ``except: pass`` violated the security-review 7b7feebd guideline
    ("Callback failures must be logged"); the first fix (rev 4) used
    ``logger.debug(...)`` which review-bot rev 4 then flagged as equally
    silent — the ``seed`` subcommand short-circuits in ``cli.py``
    BEFORE ``logging.basicConfig()`` runs, so ``.debug`` is swallowed
    by Python's last-resort handler (WARNING+ only).  The rev 5 fix
    switched to ``.warning`` which survives the last-resort handler
    and actually reaches stderr, restoring audit observability.

    This test asserts a WARNING-level log record is emitted from
    ``kiro_crew.seed`` whenever ``sel()`` raises.  Regression guard
    against a future refactor that silently reverts to ``.debug`` /
    ``pass`` / drops the ``exc_info=True`` that carries the traceback.
    """
    import logging

    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    args = type("Args", (), {"seed": "empty"})()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.seed"):
        rc = seed_mod.seed_cmd(args)

    assert rc == seed_mod.EXIT_OK
    # Exactly one WARNING record from our audit handler.
    audit_records = [
        r
        for r in caplog.records
        if r.name == "kiro_crew.seed" and r.levelno == logging.WARNING
    ]
    assert len(audit_records) == 1, (
        f"expected exactly one WARNING log from _safe_audit on sel() failure; "
        f"got {len(audit_records)}: {[r.message for r in audit_records]!r}"
    )
    rec = audit_records[0]
    assert "SEL audit emit failed" in rec.message
    # ``exc_info=True`` preserves the traceback so callers see the OSError.
    assert rec.exc_info is not None
    assert rec.exc_info[0] is OSError


# ------------------------------------------------------------------
# Phase 1.B: safety guardrails + --seed-replace. PRD acceptance tests 4, 5, 6.
# ------------------------------------------------------------------


def test_seed_main_home_rail_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$KIROCREW_HOME=~/.kirocrew`` exits 2 with 'refusing to seed main
    gateway home' message, even when the path doesn't exist yet.

    PRD acceptance test 4. Monkeypatches ``$HOME`` so ``Path.home() /
    '.kirocrew'`` points into ``tmp_path`` — catching developers who set
    ``KIROCREW_HOME`` to their real main home would be the worst possible
    test failure mode.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    target = fake_home / ".kirocrew"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert "refusing to seed main gateway home" in str(excinfo.value)
    # Target dir must not be created as a side-effect.
    assert not target.exists()


def test_seed_new_home_rail_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$KIROCREW_HOME=~/.kiro/crew`` (the post-move default home) is refused
    too — the guardrail protects the CURRENT main gateway home, not just the
    pre-move legacy ``~/.kirocrew``.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    target = fake_home / ".kiro" / "crew"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert "refusing to seed main gateway home" in str(excinfo.value)
    assert not target.exists()


def test_seed_main_home_rail_refuses_even_with_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--seed-replace`` does NOT override the main-home guardrail.

    PRD guardrail precedence constraint. This is the single most important
    test of this CR — ``--seed-replace`` on ``$KIROCREW_HOME=~/.kirocrew``
    would silently ``rmtree`` the user's live gateway state.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    target = fake_home / ".kirocrew"
    target.mkdir()
    (target / "real_user_data.txt").write_text("don't delete me")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty", replace=True)

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert "refusing to seed main gateway home" in str(excinfo.value)
    # The main-home guardrail MUST fire before rmtree runs.
    assert (target / "real_user_data.txt").exists(), (
        "CRITICAL: --seed-replace wiped main gateway home despite guardrail"
    )
    assert (target / "real_user_data.txt").read_text(encoding="utf-8") == "don't delete me"


def test_seed_main_home_rail_catches_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlinked ``$KIROCREW_HOME`` resolving to ``~/.kirocrew`` is caught.

    PRD acceptance test 6. Developer might symlink ``~/dev-home -> ~/.kirocrew``
    and point ``$KIROCREW_HOME`` at the symlink; the resolved comparison
    must still hit the main-home guardrail.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    real_main = fake_home / ".kirocrew"
    real_main.mkdir()
    (real_main / "user_data.txt").write_text("preserved")
    symlinked_target = fake_home / "dev-home"
    symlinked_target.symlink_to(real_main)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("KIROCREW_HOME", str(symlinked_target))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty", replace=True)

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert "refusing to seed main gateway home" in str(excinfo.value)
    assert (real_main / "user_data.txt").read_text(encoding="utf-8") == "preserved"


def test_seed_non_empty_rail_refuses_without_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-empty target exits 2 with hint to pass --seed-replace.

    PRD acceptance test 5, part 1. Note the error wording should guide
    the user to the remedy — the mechanical solution is ``--seed-replace`` and
    it's cheap to mention.
    """
    target = tmp_path / "existing"
    target.mkdir()
    (target / "stale.txt").write_text("old content")
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    msg = str(excinfo.value)
    assert "not empty" in msg
    assert "--seed-replace" in msg
    # Pre-existing content must be untouched on the refusal path.
    assert (target / "stale.txt").read_text(encoding="utf-8") == "old content"


def test_seed_non_empty_rail_succeeds_with_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--seed-replace`` on a non-empty target wipes + re-seeds successfully.

    PRD acceptance test 5, part 2. Old content gone, fixture present.
    """
    target = tmp_path / "existing"
    target.mkdir()
    (target / "stale.txt").write_text("old content")
    (target / "subdir").mkdir()
    (target / "subdir" / "deep.txt").write_text("also stale")
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    seed_mod.seed("empty", replace=True)

    # Pre-existing content gone.
    assert not (target / "stale.txt").exists()
    assert not (target / "subdir").exists()
    # Fixture content present.
    assert (target / "fixture.yaml").is_file()
    src_file = Path(str(seed_mod._fixtures_root())) / "empty" / "fixture.yaml"
    assert (target / "fixture.yaml").read_bytes() == src_file.read_bytes()


@requires_symlinks
def test_seed_replace_refuses_symlinked_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--seed-replace`` on a symlinked $KIROCREW_HOME refuses, preventing
    rmtree from following the link and deleting the link target.

    Symlinks already resolve in the main-home guardrail path; this guards the
    case where $KIROCREW_HOME symlinks to a non-main-home directory.
    """
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "precious.txt").write_text("must survive")
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    monkeypatch.setenv("KIROCREW_HOME", str(link))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty", replace=True)

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert "symlinked" in str(excinfo.value)
    # Link target must be untouched.
    assert (real_dir / "precious.txt").read_text(encoding="utf-8") == "must survive"


@requires_symlinks
def test_seed_refuses_symlinked_nonempty_target_without_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlinked non-empty $KIROCREW_HOME fails fast with SYMLINK_REPLACE —
    does NOT route the user through the misleading NON_EMPTY → SYMLINK_REPLACE
    two-step failure.

    Regression guard (rev-3 review-bot finding): before the hoist,
    this path fired ``GUARDRAIL_NON_EMPTY`` with "Pass --seed-replace to wipe
    it and re-seed", which actively misled users — following that advice
    immediately hit ``GUARDRAIL_SYMLINK_REPLACE`` on the next attempt.

    The fix checks ``is_symlink()`` first inside the non-empty branch so
    the user gets the actionable "Point it at a real directory" message
    on the first attempt.
    """
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "existing.txt").write_text("already here")
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    monkeypatch.setenv("KIROCREW_HOME", str(link))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")  # NO replace=True

    # Fires the symlink guardrail (not NON_EMPTY) even without --seed-replace.
    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_SYMLINK_REPLACE
    # Message says "refusing to seed into a symlinked" (matches empty-symlink
    # and --replace-symlink cases) — no mention of --seed-replace which would
    # be the dead-end recommendation.
    msg = str(excinfo.value)
    assert "symlinked" in msg
    assert "--seed-replace" not in msg
    # Link target must be untouched.
    assert (real_dir / "existing.txt").read_text(encoding="utf-8") == "already here"


def test_seed_empty_existing_dir_succeeds_without_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty pre-existing $KIROCREW_HOME is accepted (no --seed-replace needed).

    Regression guard: 1.A used ``shutil.copytree`` which refuses any
    pre-existing ``dst``. 1.B's contract is looser — empty-dir targets
    are fine. This catches a future regression that tightens the check
    back to "must not exist".
    """
    target = tmp_path / "empty_preexisting"
    target.mkdir()  # exists, but empty
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    seed_mod.seed("empty")

    assert (target / "fixture.yaml").is_file()


@patch("kiro_crew.seed.sel")
def test_seed_cmd_replace_flag_threaded(
    mock_sel: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``args.seed_replace`` is forwarded to ``seed()`` and logged in audit.

    Pin the CLI wiring so a future refactor can't silently drop the flag.
    """
    target = tmp_path / "existing"
    target.mkdir()
    (target / "junk").write_text("stale")
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    args = type("Args", (), {"seed": "empty", "seed_replace": True})()
    assert seed_mod.seed_cmd(args) == seed_mod.EXIT_OK

    # Audit event records replace=True so an SOC reviewing SEL events can
    # see whether a dev tool invocation wiped data.
    kw = mock_sel().log_api_access.call_args.kwargs
    assert kw["outcome"] == "allowed"
    assert "replace=True" in kw["resources"]
    # Actual seed happened.
    assert (target / "fixture.yaml").is_file()
    assert not (target / "junk").exists()


@patch("kiro_crew.seed.sel")
def test_seed_cmd_missing_seed_replace_attr_defaults_false(
    mock_sel: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``seed_cmd`` tolerates an args Namespace without ``seed_replace``.

    Defensive: ``getattr(args, "seed_replace", False)`` means old callers that
    don't wire the flag still work. Regression guard in case someone
    refactors to ``args.seed_replace`` direct-access.
    """
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    # Intentionally omit ``seed_replace`` from the namespace.
    args = type("Args", (), {"seed": "empty"})()
    assert seed_mod.seed_cmd(args) == seed_mod.EXIT_OK
    assert (target / "fixture.yaml").is_file()
    kw = mock_sel().log_api_access.call_args.kwargs
    assert "replace=False" in kw["resources"]


# ------------------------------------------------------------------
# review-bot rev 11 findings — regression tests.
# ------------------------------------------------------------------


def test_seed_resolve_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolve()`` raising ``OSError`` in the main-home check denies, not allows.

    Regression guard for review-bot rev 11 post #31 (security-controls,
    importance=1) + #32: before the fix, ``except OSError: pass`` fell
    back to the unresolved path, silently bypassing the main-home guardrail.
    Combined with ``--seed-replace``, that could have wiped the dev's
    live gateway state on a broken-symlink-chain ``$KIROCREW_HOME``.

    The test patches ``Path.resolve`` to always raise inside the main-
    home check code path. Must surface as ``SeedError(guardrail=resolve_failed)``.
    """
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    original_resolve = Path.resolve

    def _selective_raise(self, *args, **kwargs):
        # Only fail on the for_main_home_check resolve — leave other
        # resolves alone (fixtures-root resolution, etc.) so the test
        # isolates the exact branch under regression.
        if str(self) == str(target):
            raise OSError("[Errno 40] Too many levels of symbolic links")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _selective_raise)

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty", replace=True)

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_RESOLVE_FAILED
    assert "cannot resolve $KIROCREW_HOME" in str(excinfo.value)
    # Critical: target must NOT have been wiped or created by the bypass.
    assert not target.exists()


def test_seed_empty_string_fixture_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--seed ""`` (empty fixture name) rejected with GUARDRAIL_BAD_NAME.

    Regression guard for review-bot rev 11 post #33: the CLI wiring used a
    truthiness check (``if args.seed``), which is falsy for ``""``. A
    user passing ``--seed ""`` would silently start the gateway without
    seeding. The fix is ``is not None`` in cli.py AND the existing
    empty-name guardrail in ``_resolve_fixture`` — this test pins both.
    """
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_BAD_NAME


@patch("kiro_crew.seed.sel")
def test_seed_cmd_empty_seed_routes_to_seed_cmd(
    mock_sel: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``seed_cmd`` with ``args.seed=""`` returns EXIT_GUARDRAIL via the empty-name
    guardrail, proving the CLI dispatch path reaches it instead of silently
    short-circuiting on truthiness.

    This tests the end-to-end path for review-bot rev 11 post #33.
    """
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    args = type("Args", (), {"seed": ""})()
    rc = seed_mod.seed_cmd(args)

    assert rc == seed_mod.EXIT_GUARDRAIL
    # Audit stream records the bad-name guardrail, not a swallowed no-op.
    kw = mock_sel().log_api_access.call_args.kwargs
    assert kw["outcome"] == "denied"
    assert f"guardrail={seed_mod.SeedError.GUARDRAIL_BAD_NAME}" in kw["resources"]


@pytest.mark.parametrize(
    "setup,expected_rail,replace",
    [
        # (setup_fn, expected guardrail, replace flag)
        ("main_home", "main_home", False),
        ("non_empty", "non_empty", False),
        ("symlinked_target", "symlink_replace", True),
    ],
)
def test_seed_audit_uses_rail_tag_not_raw_path(
    setup: str,
    expected_rail: str,
    replace: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guardrail-tagged ``SeedError`` audit path must log ``guardrail=<constant>``
    instead of the exception message (which embeds resolved paths).

    Regression guard for review-bot rev 11 post #34: the prior
    ``reason={exc}`` formatting leaked ``$HOME``-derived paths into the
    SEL stream on every denial, contradicting the presence-only design
    of the success/error paths.

    Asserts BOTH: (a) ``guardrail=<short-constant>`` is present, (b) the
    resolved target path is NOT present in the audit resources string.
    """
    # Patch ``sel`` LOCALLY per test so parametrize doesn't collide with
    # the @patch decorator's call-count tracking.
    with patch("kiro_crew.seed.sel") as mock_sel:
        if setup == "main_home":
            fake_home = tmp_path / "fake_home"
            fake_home.mkdir()
            target = fake_home / ".kirocrew"
            target.mkdir()
            monkeypatch.setenv("HOME", str(fake_home))
            monkeypatch.setenv("KIROCREW_HOME", str(target))
        elif setup == "non_empty":
            target = tmp_path / "stuffed"
            target.mkdir()
            (target / "stale.txt").write_text("old")
            monkeypatch.setenv("KIROCREW_HOME", str(target))
        else:  # symlinked_target
            real = tmp_path / "real"
            real.mkdir()
            (real / "precious.txt").write_text("keep")
            target = tmp_path / "link"
            target.symlink_to(real)
            monkeypatch.setenv("KIROCREW_HOME", str(target))

        args = type(
            "Args",
            (),
            {"seed": "empty", "seed_replace": replace},
        )()
        assert seed_mod.seed_cmd(args) == seed_mod.EXIT_GUARDRAIL

        kw = mock_sel().log_api_access.call_args.kwargs
        # Guardrail constant present.
        assert f"guardrail={expected_rail}" in kw["resources"], (
            f"expected guardrail={expected_rail} in audit, got: {kw['resources']!r}"
        )
        # Resolved target path must NOT leak into the audit stream —
        # that's the point of the guardrail-tag refactor.
        resolved = target.resolve(strict=False) if target.exists() or target.is_symlink() else target
        assert str(resolved) not in kw["resources"], (
            f"audit leaked resolved path {resolved!r}: {kw['resources']!r}"
        )


def test_seed_regular_file_target_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$KIROCREW_HOME`` pointing at a regular file raises GUARDRAIL_NOT_A_DIRECTORY.

    Regression guard (review nit #43): before this fix, a
    regular-file ``$KIROCREW_HOME`` reached ``dst.iterdir()`` and raised a
    raw ``NotADirectoryError`` -> EXIT_IO_ERROR with a cryptic message. Now
    it fails fast with the plain ``seed: error:`` prefix and the SEL audit
    records ``guardrail=not_a_directory`` instead of the raw OS error type.
    """
    target_file = tmp_path / "stray.log"
    target_file.write_text("some stale log the user left behind")
    monkeypatch.setenv("KIROCREW_HOME", str(target_file))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_NOT_A_DIRECTORY
    assert "not a directory" in str(excinfo.value)
    # Original file must NOT be touched (no rmtree, no copy).
    assert target_file.is_file()
    assert target_file.read_text(encoding="utf-8") == "some stale log the user left behind"


@requires_symlinks
def test_seed_empty_symlink_target_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty-but-existing symlinked ``$KIROCREW_HOME`` raises GUARDRAIL_SYMLINK_EMPTY.

    Regression guard (review nit #40): before this fix, an
    empty symlinked target fell through to ``shutil.copytree(src, dst)``
    which raised a bare ``FileExistsError`` -> EXIT_IO_ERROR. Now it fails
    with a symlink-specific guardrail so the user sees the same "point it at a
    real directory" message whether the link target is empty or populated.
    """
    real_dir = tmp_path / "real"
    real_dir.mkdir()  # empty
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    monkeypatch.setenv("KIROCREW_HOME", str(link))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_SYMLINK_EMPTY
    assert "symlinked" in str(excinfo.value)
    # Link target dir must remain empty — no fixture files leaked.
    assert list(real_dir.iterdir()) == []


def test_seed_double_resolve_target_called_once_per_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``seed()`` calls ``_resolve_target`` exactly twice: once for main-home
    check (resolved), once for copy/rmtree (raw). Regression guard (review
    nit #39): pin the call count so a future refactor can't silently
    collapse the two calls into one (would either break symlinked-target
    handling or the main-home guardrail, depending on which form was kept).
    """
    target = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(target))

    original = seed_mod._resolve_target
    calls: list[bool] = []

    def _spy(*, for_main_home_check: bool = False) -> Path:
        calls.append(for_main_home_check)
        return original(for_main_home_check=for_main_home_check)

    monkeypatch.setattr(seed_mod, "_resolve_target", _spy)
    seed_mod.seed("empty")

    assert calls == [True, False], (
        f"expected [for_main_home_check=True, False] got {calls!r}"
    )
    assert (target / "fixture.yaml").is_file()


@requires_symlinks
def test_seed_symlink_to_file_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$KIROCREW_HOME`` as a symlink pointing at a regular file raises
    GUARDRAIL_NOT_A_DIRECTORY.

    Regression guard (rev-1 review-bot finding): before this fix,
    the guard was ``not dst.is_symlink() and not dst.is_dir()`` — the
    ``not is_symlink()`` short-circuited for symlink-to-file, letting
    ``dst.iterdir()`` follow the link and raise a raw ``NotADirectoryError``.
    Fixed by dropping the ``not is_symlink()`` clause since ``is_dir()``
    already follows symlinks — a symlink to a dir passes the guard, a
    symlink to a file does not.

    The rev-1 regression ``test_seed_regular_file_target_rejected`` only
    covered plain regular files and missed this case.
    """
    real_file = tmp_path / "stale.log"
    real_file.write_text("a stale file")
    link = tmp_path / "link"
    link.symlink_to(real_file)
    monkeypatch.setenv("KIROCREW_HOME", str(link))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_NOT_A_DIRECTORY
    assert "not a directory" in str(excinfo.value)
    # Link target must NOT be touched.
    assert real_file.is_file()
    assert real_file.read_text(encoding="utf-8") == "a stale file"
    # Link itself still exists, still points at the file.
    assert link.is_symlink()


@requires_symlinks
def test_seed_dangling_symlink_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$KIROCREW_HOME`` as a symlink to a nonexistent target raises
    GUARDRAIL_DANGLING_SYMLINK.

    Regression guard (rev-2 review-bot finding): before this
    fix, a dangling symlink bypassed every ``exists()``-gated guard
    because ``Path.exists()`` follows the link and returns ``False`` when
    the target is missing. That let the link fall through to
    ``shutil.copytree(src, dst)``, which raised a raw ``FileExistsError``
    (the symlink path entry itself still exists on the filesystem).

    ``is_symlink()`` does NOT follow the link, so
    ``is_symlink() and not exists()`` is the only reliable way to detect
    a dangling link. The guard must run FIRST — before the
    GUARDRAIL_NOT_A_DIRECTORY and non-empty checks — because those both rely
    on ``exists() == True``.
    """
    missing_target = tmp_path / "never-existed"
    # Intentionally do NOT create missing_target.
    link = tmp_path / "dangling"
    link.symlink_to(missing_target)
    assert link.is_symlink()
    assert not link.exists(), "precondition: link must resolve to missing target"

    monkeypatch.setenv("KIROCREW_HOME", str(link))

    with pytest.raises(seed_mod.SeedError) as excinfo:
        seed_mod.seed("empty")

    assert excinfo.value.code == seed_mod.EXIT_GUARDRAIL
    assert excinfo.value.guardrail == seed_mod.SeedError.GUARDRAIL_DANGLING_SYMLINK
    assert "dangling symlink" in str(excinfo.value)
    # Link must still exist (not removed / not replaced by a directory).
    assert link.is_symlink()
    # Target must still not exist (no side-effect creation).
    assert not missing_target.exists()
