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
    frontend.build_frontend_sync(tmp_path, log=lambda _m: None)

    build = [(argv, env) for argv, env in seen if argv[:3] == ["npm", "run", "build"]]
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
    asyncio.run(frontend.build_frontend_async(str(tmp_path)))

    build = [(argv, env) for argv, env in seen if argv[:3] == ("npm", "run", "build")]
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
    frontend.build_frontend_sync(tmp_path, log=lambda _m: None)

    build = [(argv, env) for argv, env in seen if argv[:3] == ["npm", "run", "build"]]
    assert build
    assert build[0][1] is None
