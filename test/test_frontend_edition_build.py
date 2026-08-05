"""The runtime frontend rebuild must recompose the EDITION, not stage stock over it.

``POST /api/update``, ``kirocrew update``, and the gateway's auto-apply all shell
``npm run build`` and stage the result over the served ``static/dist``. Vite reads
the edition composition root from the environment
(``website/vite.config.ts``::``editionExtensionPlugin``), so what those rebuilds
pass in the environment decides WHICH edition gets built.

Dropping the edition vars is silent, which is why these are tests rather than a
comment: the rebuild would build the STOCK SPA and stage it over the edition
dashboard, and because the build SUCCEEDS nothing raises — the dashboard just
becomes upstream's.

The opt-in is READ, never synthesized. ``KIROCREW_ALLOW_EDITION=1`` gates
compiling an edition's proprietary sources into ``website/dist``, which is staged
into the packaged wheel; a published release cannot be unpublished, so that is a
one-way door and ``website/AGENTS.md`` says never to set the opt-in outside the
edition's own build. A helper that forced it would defeat the gate exactly when it
should fire, so an edition dir without the operator's opt-in declines and lets
vite raise its own explicit error.

A packaged install (wheel/bundle) ships the built ``dist`` but not the edition's
TypeScript sources, so a rebuild there can only produce a stock bundle — the
build is SKIPPED instead, leaving the shipped dashboard in place.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew import frontend

_DIR_ENV = "KIROCREW_EDITION_DIR"
_OPT_IN_ENV = "KIROCREW_ALLOW_EDITION"


@pytest.fixture(autouse=True)
def _clean_edition_env(monkeypatch):
    """Never inherit the developer's own edition vars into a test."""
    monkeypatch.delenv(_DIR_ENV, raising=False)
    monkeypatch.delenv(_OPT_IN_ENV, raising=False)


def _edition_dir(tmp_path: Path, *, with_entry: str | None = "extensions.tsx") -> Path:
    d = tmp_path / "edition"
    d.mkdir()
    if with_entry:
        (d / with_entry).write_text("export {}\n")
    return d


# ── _edition_build_env: the env handed to `npm run build` ──


def test_stock_build_inherits_the_environment_unchanged():
    """No edition dir → ``None``, i.e. inherit ``os.environ`` as before.

    Asserted as an exact ``None`` rather than "no edition keys": passing a COPY of
    the environment would also satisfy a key-absence check while silently changing
    the stock path from inherit-in-place to inherit-a-snapshot.
    """
    assert frontend._edition_build_env() is None


def test_edition_dir_is_forwarded(monkeypatch, tmp_path):
    d = _edition_dir(tmp_path)
    monkeypatch.setenv(_DIR_ENV, str(d))
    monkeypatch.setenv(_OPT_IN_ENV, "1")

    env = frontend._edition_build_env()

    assert env is not None
    assert env[_DIR_ENV] == str(d)
    assert env[_OPT_IN_ENV] == "1"


def test_the_opt_in_is_never_synthesized(monkeypatch, tmp_path):
    """A dir WITHOUT the operator's opt-in must not be turned into an edition build.

    `KIROCREW_ALLOW_EDITION=1` is the fail-closed gate on compiling an edition's
    proprietary sources into `website/dist`, which is staged into the packaged
    wheel — a one-way door, which is why `website/AGENTS.md` says never to set the
    opt-in outside the edition's own build. Forcing it here would defeat that gate
    precisely when it should fire, so the helper declines and lets vite raise its
    own explicit error instead.
    """
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path)))

    assert frontend._edition_build_env() is None


def test_an_explicitly_disabled_opt_in_is_honored(monkeypatch, tmp_path):
    """`KIROCREW_ALLOW_EDITION=0` is a refusal, not noise to override."""
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path)))
    monkeypatch.setenv(_OPT_IN_ENV, "0")

    assert frontend._edition_build_env() is None


def test_opt_in_alone_does_not_trigger_edition_composition(monkeypatch):
    """Without a dir there is no edition to compose; stay on the stock path."""
    monkeypatch.setenv(_OPT_IN_ENV, "1")

    assert frontend._edition_build_env() is None


def test_the_rest_of_the_environment_is_preserved(monkeypatch, tmp_path):
    """npm/node need PATH et al — the helper must ADD to the env, not replace it."""
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path)))
    monkeypatch.setenv(_OPT_IN_ENV, "1")
    monkeypatch.setenv("KIROCREW_TEST_SENTINEL", "keep-me")

    env = frontend._edition_build_env()

    assert env is not None
    assert env["KIROCREW_TEST_SENTINEL"] == "keep-me"
    assert "PATH" in env


# ── edition_sources_missing: the packaged-install skip ──


def test_sources_present_is_not_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path)))
    assert frontend.edition_sources_missing() is False


def test_a_ts_composition_root_also_counts(monkeypatch, tmp_path):
    """vite accepts ``extensions.ts`` too, so this helper must agree with it."""
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path, with_entry="extensions.ts")))
    assert frontend.edition_sources_missing() is False


def test_an_edition_dir_without_a_composition_root_is_missing(monkeypatch, tmp_path):
    """The packaged-install shape: the dir exists (or not) but the sources do not."""
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path, with_entry=None)))
    assert frontend.edition_sources_missing() is True


def test_a_nonexistent_edition_dir_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(_DIR_ENV, str(tmp_path / "not-there"))
    assert frontend.edition_sources_missing() is True


def test_no_edition_dir_is_never_missing():
    """A stock host has no edition sources to miss — the skip must not fire."""
    assert frontend.edition_sources_missing() is False


# ── The build helpers actually pass the env / take the skip ──


def _popen_recorder(seen: list) -> type:
    """Record the build spawn: the build runs through ``subprocess.Popen``."""

    class _P:
        returncode = 0

        def __init__(self, argv, **kwargs):
            seen.append((argv, kwargs.get("env")))

        def wait(self, timeout=None):
            return 0

    return _P


def _popen_forbidden(message: str) -> type:
    """A ``Popen`` that fails the test if the build is reached at all."""

    class _P:
        def __init__(self, argv, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError(message)

    return _P


def _website(tmp_path: Path) -> Path:
    """Minimal ``<proj>/website`` so the helpers get past their own guards."""
    w = tmp_path / "website"
    w.mkdir()
    (w / "package-lock.json").write_text("{}")
    return w


def test_sync_build_passes_the_edition_env_to_npm_run_build(monkeypatch, tmp_path):
    """The assertion that fails if the env is ever dropped from the build call."""
    _website(tmp_path)
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path)))
    monkeypatch.setenv(_OPT_IN_ENV, "1")
    monkeypatch.setattr(frontend.shutil, "which", lambda _n: "/usr/bin/npm")
    monkeypatch.setattr(frontend, "_stage_dist", lambda *_a, **_k: None)

    seen: list[tuple[list[str], dict | None]] = []

    class _Done:
        returncode = 0

    def _run(argv, **kwargs):
        seen.append((argv, kwargs.get("env")))
        return _Done()

    monkeypatch.setattr(frontend.subprocess, "run", _run)
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen_recorder(seen))
    monkeypatch.setattr(frontend, "_stage_dist_locked", lambda *_a, **_k: None)
    frontend.build_frontend_sync(tmp_path, log=lambda _m: None)

    build = [(argv, env) for argv, env in seen if list(argv[1:3]) == ["run", "build"]]
    assert build, f"npm run build was never invoked; saw {[a for a, _ in seen]}"
    _argv, env = build[0]
    assert env is not None, "npm run build inherited the env — the edition seam is lost"
    assert env[_DIR_ENV] == str(tmp_path / "edition")
    assert env[_OPT_IN_ENV] == "1"


def test_sync_build_skips_when_edition_sources_are_absent(monkeypatch, tmp_path):
    """A packaged edition install must keep its shipped dashboard, not rebuild it."""
    _website(tmp_path)
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path, with_entry=None)))
    monkeypatch.setattr(frontend.shutil, "which", lambda _n: "/usr/bin/npm")

    calls: list[list[str]] = []

    def _run(argv, **_kwargs):  # pragma: no cover — must never be reached
        calls.append(argv)
        raise AssertionError("npm must not run when the edition sources are absent")

    monkeypatch.setattr(frontend.subprocess, "run", _run)
    messages: list[str] = []
    monkeypatch.setattr(
        frontend.subprocess, "Popen", _popen_forbidden("npm must not run when the edition sources are absent")
    )
    frontend.build_frontend_sync(tmp_path, log=messages.append)

    assert calls == []
    # The skip is reported, so an operator can tell it from a silent no-op.
    assert any("Edition frontend sources" in m for m in messages), messages


def test_async_build_passes_the_edition_env_to_npm_run_build(monkeypatch, tmp_path):
    """`build_frontend_async` is the /api/update + auto-apply path — same contract.

    A separate test from the sync one because they are separate implementations:
    the sync helper's env could be correct while this one still staged stock over
    an edition dashboard on every gateway auto-update.
    """
    _website(tmp_path)
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path)))
    monkeypatch.setenv(_OPT_IN_ENV, "1")
    monkeypatch.setattr(frontend.shutil, "which", lambda _n: "/usr/bin/npm")
    monkeypatch.setattr(frontend, "_stage_dist", lambda *_a, **_k: None)

    seen: list[tuple[tuple, dict | None]] = []

    class _Proc:
        returncode = 0

        async def wait(self):
            return 0

    async def _exec(*argv, **kwargs):
        seen.append((argv, kwargs.get("env")))
        return _Proc()

    monkeypatch.setattr(frontend.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen_recorder(seen))
    monkeypatch.setattr(frontend, "_stage_dist_locked", lambda *_a, **_k: None)
    asyncio.run(frontend.build_frontend_async(str(tmp_path)))

    build = [(argv, env) for argv, env in seen if list(argv[1:3]) == ["run", "build"]]
    assert build, f"npm run build was never invoked; saw {[a for a, _ in seen]}"
    _argv, env = build[0]
    assert env is not None, "npm run build inherited the env — the edition seam is lost"
    assert env[_DIR_ENV] == str(tmp_path / "edition")
    assert env[_OPT_IN_ENV] == "1"


def test_async_build_skips_when_edition_sources_are_absent(monkeypatch, tmp_path):
    _website(tmp_path)
    monkeypatch.setenv(_DIR_ENV, str(_edition_dir(tmp_path, with_entry=None)))
    monkeypatch.setattr(frontend.shutil, "which", lambda _n: "/usr/bin/npm")

    async def _exec(*argv, **_kwargs):  # pragma: no cover — must never be reached
        raise AssertionError("npm must not run when the edition sources are absent")

    monkeypatch.setattr(frontend.asyncio, "create_subprocess_exec", _exec)
    progress: list[tuple[str, str]] = []
    monkeypatch.setattr(
        frontend.subprocess, "Popen", _popen_forbidden("npm must not run when the edition sources are absent")
    )
    asyncio.run(
        frontend.build_frontend_async(
            str(tmp_path), push_progress=lambda k, m: progress.append((k, m))
        )
    )

    assert any("Edition frontend sources" in m for _k, m in progress), progress


def test_stock_build_still_inherits_the_env(monkeypatch, tmp_path):
    """The stock path must be byte-identical to before: ``env=None``.

    Guards against "fixing" the seam by always materializing a dict, which would
    make every public build depend on this helper's copy of the environment.
    """
    _website(tmp_path)
    monkeypatch.setattr(frontend.shutil, "which", lambda _n: "/usr/bin/npm")
    monkeypatch.setattr(frontend, "_stage_dist", lambda *_a, **_k: None)

    seen: list[tuple[list[str], dict | None]] = []

    class _Done:
        returncode = 0

    def _run(argv, **kwargs):
        seen.append((argv, kwargs.get("env")))
        return _Done()

    monkeypatch.setattr(frontend.subprocess, "run", _run)
    monkeypatch.setattr(frontend.subprocess, "Popen", _popen_recorder(seen))
    monkeypatch.setattr(frontend, "_stage_dist_locked", lambda *_a, **_k: None)
    frontend.build_frontend_sync(tmp_path, log=lambda _m: None)

    build = [(argv, env) for argv, env in seen if list(argv[1:3]) == ["run", "build"]]
    assert build
    assert build[0][1] is None


def test_stage_built_dist_copies_website_dist_into_static_dist(tmp_path):
    """The staging-only seam Dev Fleet's Pull+Build calls.

    Pull+Build drives each build step as its own audited subprocess, so it cannot
    use build_frontend_sync's all-in-one path; before this seam existed it ran
    `npm run build` and never staged, leaving the gateway serving the previous
    bundle while reporting success.
    """
    built = tmp_path / "website" / "dist"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<html>fresh</html>")
    frontend.stage_built_dist(tmp_path, log=lambda _m: None)
    staged = tmp_path / "src" / "kiro_crew" / "static" / "dist" / "index.html"
    assert staged.read_text() == "<html>fresh</html>"


def test_stage_built_dist_replaces_a_stale_symlink(tmp_path):
    """A source-tree gateway leaves static/dist as a SYMLINK to website/dist.

    Staging must replace it with a real snapshot rather than fail or write
    through it, so a packaged layout gets a self-contained bundle.
    """
    built = tmp_path / "website" / "dist"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<html>fresh</html>")
    static_parent = tmp_path / "src" / "kiro_crew" / "static"
    static_parent.mkdir(parents=True)
    (static_parent / "dist").symlink_to(built)
    frontend.stage_built_dist(tmp_path, log=lambda _m: None)
    staged = static_parent / "dist"
    assert not staged.is_symlink(), "a stale symlink must be replaced by a copy"
    assert (staged / "index.html").read_text() == "<html>fresh</html>"


def test_stage_built_dist_fails_loudly_without_a_build(tmp_path):
    """No website/dist -> raise, and leave the served bundle alone.

    Two separate requirements. Deleting a working static/dist because the build
    step failed would take the dashboard down harder than the failed build
    already did -- so the previous bundle survives. But this runs as a sync step
    right after `npm run build` reported success, so a MISSING build output means
    something is genuinely wrong and must not be reported as a successful sync.
    """
    static_dist = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    static_dist.mkdir(parents=True)
    (static_dist / "index.html").write_text("<html>previous</html>")
    messages: list = []
    with pytest.raises(RuntimeError, match="staging failed"):
        frontend.stage_built_dist(tmp_path, log=messages.append)
    assert (static_dist / "index.html").read_text() == "<html>previous</html>"
    assert any("not found" in m for m in messages)


def test_stage_built_dist_raises_when_staging_did_not_happen(tmp_path, monkeypatch):
    """A staging failure must FAIL the caller, not report success.

    Dev Fleet's Pull+Build runs this as a sync step whose exit status decides
    whether the sync reports success. A surviving older bundle is deliberately
    NOT treated as evidence of success: _stage_dist now preserves it on failure,
    so only its returned flag can distinguish "staged" from "kept the old one".
    """
    built = tmp_path / "website" / "dist"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<html>fresh</html>")
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    served.mkdir(parents=True)
    (served / "index.html").write_text("<html>previous</html>")
    monkeypatch.setattr(frontend, "_stage_dist", lambda *_a, **_k: False)
    with pytest.raises(RuntimeError, match="staging failed"):
        frontend.stage_built_dist(tmp_path, log=lambda _m: None)


def test_stage_dist_keeps_the_served_bundle_when_the_copy_fails(tmp_path, monkeypatch):
    """A failed rebuild must not take the working dashboard down with it.

    The original order removed the destination and THEN copied, so any copy error
    left static/dist missing entirely. Staging into a temp sibling and swapping
    means a failure costs the update, not the UI.
    """
    built = tmp_path / "website" / "dist"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<html>fresh</html>")
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    served.mkdir(parents=True)
    (served / "index.html").write_text("<html>previous</html>")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(frontend.shutil, "copytree", boom)
    assert frontend._stage_dist(built, tmp_path, log=lambda _m: None) is False
    # The previously served bundle is untouched.
    assert (served / "index.html").read_text() == "<html>previous</html>"
    # No staging leftovers.
    # The .dist.staging.lock file is the persistent flock target; what must
    # not survive is a staging DIRECTORY.
    assert not [q for q in served.parent.glob(".dist.staging.*") if q.is_dir()]


def test_stage_dist_replaces_the_served_bundle_on_success(tmp_path):
    """The happy path still swaps the new bundle in and reports True."""
    built = tmp_path / "website" / "dist"
    built.mkdir(parents=True)
    (built / "index.html").write_text("<html>fresh</html>")
    served = tmp_path / "src" / "kiro_crew" / "static" / "dist"
    served.mkdir(parents=True)
    (served / "index.html").write_text("<html>previous</html>")
    (served / "stale-asset.js").write_text("old")

    assert frontend._stage_dist(built, tmp_path, log=lambda _m: None) is True
    assert (served / "index.html").read_text() == "<html>fresh</html>"
    # A replace, not a merge -- stale files from the old bundle are gone.
    assert not (served / "stale-asset.js").exists()
    # The .dist.staging.lock file is the persistent flock target; what must
    # not survive is a staging DIRECTORY.
    assert not [q for q in served.parent.glob(".dist.staging.*") if q.is_dir()]


def test_edition_configured_tracks_the_env_var(monkeypatch):
    """The predicate callers use to decide whether staging is safe at all."""
    monkeypatch.delenv("KIROCREW_EDITION_DIR", raising=False)
    assert frontend.edition_configured() is False
    monkeypatch.setenv("KIROCREW_EDITION_DIR", "/opt/edition")
    assert frontend.edition_configured() is True


def test_edition_dir_survives_the_app_backend_env_allowlist(monkeypatch):
    """An app backend must be ABLE to tell an edition install from a stock one.

    The backend runs as a separate process started with
    ``apps.registry.minimal_env()``, which passes only a fixed safe-key set --
    so an edition guard inside a backend reads "stock" on every install unless
    the var is propagated explicitly. Dev Fleet's dist-staging guard depends on
    this: without it the guard can never fire and a stock SPA would be staged
    over an edition dashboard.
    """
    from kiro_crew.apps.registry import minimal_env

    monkeypatch.setenv("KIROCREW_EDITION_DIR", "/opt/edition")
    # The generic allowlist does NOT carry it -- that is the trap this guards.
    assert "KIROCREW_EDITION_DIR" not in minimal_env()

    # apps/backend.py must therefore add it to the explicit platform extras.
    src = Path(frontend.__file__).parent / "apps" / "backend.py"
    body = src.read_text()
    assert '_platform_extra["KIROCREW_EDITION_DIR"]' in body, \
        "app backends can no longer detect an edition install"
    # The opt-in must NOT be propagated: a backend may detect an edition but
    # never manufacture consent to compile edition sources into a package.
    assert '_platform_extra["KIROCREW_ALLOW_EDITION"]' not in body
