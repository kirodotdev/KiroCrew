"""A cron child cannot read any app's ``.app_secret``, and still runs its own bundle.

``<config_dir>/apps/<app>/.app_secret`` is a bearer credential: whoever reads it can act
as that app against the gateway. Both cron exec paths mask the whole apps tree, so every
app's secret is covered, including an app installed while the child runs. The bundle a
cron runs from comes back as a READ-ONLY window (its code and sibling modules import),
with that app's ``data/`` kept writable and its secret still masked.

Three layers are pinned here, none of which needs a sandbox backend on the test host:

* ``cron_apps_mask`` -- which paths each kind of cron asks the sandbox to hide and
  re-expose, including both spellings of a symlinked home;
* the Linux launcher the namespace backend would exec -- the bind/hide lists it embeds;
* the Seatbelt profile -- the read/write rules it emits for the same request.

Must be runnable with ``--noconftest`` (no hypothesis dependency).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from kiro_crew import cron_script, sandbox_launcher, sandbox_seatbelt
from kiro_crew.cron_script import cron_apps_mask

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="no cron sandbox backend on Windows; the launcher and its paths are POSIX-only",
)

OWN = "own-app"
OTHER = "other-app"


def _make_app(apps: Path, name: str) -> Path:
    app = apps / name
    (app / "lib").mkdir(parents=True)
    (app / "data").mkdir()
    (app / ".app_secret").write_text(f"secret-of-{name}\n")
    (app / "job.py").write_text("def run(ctx):\n    pass\n")
    (app / "backend").mkdir()
    (app / "backend" / "server.mjs").write_text("export const ok = 1;\n")
    return app


@pytest.fixture()
def crew_home(tmp_path, monkeypatch) -> Path:
    """A crew home with two installed apps, reached through a plain (unlinked) $HOME."""
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    (crew / "crons").mkdir(parents=True)
    _make_app(crew / "apps", OWN)
    _make_app(crew / "apps", OTHER)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


@pytest.fixture()
def linked_crew_home(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """The same layout behind a symlinked $HOME (``/home -> /local/home``)."""
    real_home = tmp_path / "local" / "home"
    crew = real_home / ".kiro" / "crew"
    (crew / "crons").mkdir(parents=True)
    _make_app(crew / "apps", OWN)
    _make_app(crew / "apps", OTHER)
    link_home = tmp_path / "home"
    link_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link_home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew.resolve()))
    return crew.resolve(), link_home / ".kiro" / "crew"


def _under(parent: str, child: str) -> bool:
    parent = os.path.normpath(parent)
    child = os.path.normpath(child)
    return child == parent or child.startswith(parent + os.sep)


def _readable(path: str, mask) -> bool:
    """Whether *path* is readable under *mask*, by the rules both backends implement.

    Masked when some hidden entry covers it and no window between that entry and the
    path re-exposes it; a hidden entry nested INSIDE a window masks again.
    """
    best_hidden = max((h for h in mask.hidden if _under(h, path)), key=len, default=None)
    if best_hidden is None:
        return True
    best_window = max((w for w in mask.windows if _under(w, path)), key=len, default=None)
    return best_window is not None and len(best_window) > len(best_hidden)


def _writable(path: str, mask) -> bool:
    if not _readable(path, mask):
        return False
    best_window = max((w for w in mask.windows if _under(w, path)), key=len, default=None)
    if best_window is None:
        return True
    return best_window not in mask.readonly


class TestCronAppsMask:
    def test_own_bundle_script_reads_its_bundle_and_no_secret(self, crew_home):
        own = crew_home / "apps" / OWN
        other = crew_home / "apps" / OTHER

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert _readable(str(own / "job.py"), mask)
        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert _readable(str(own / "lib"), mask)
        assert not _writable(str(own / "job.py"), mask), "the bundle must be read-only"
        assert _writable(str(own / "data" / "state.json"), mask), "data/ stays writable"
        assert not _readable(str(own / ".app_secret"), mask)
        assert not _readable(str(other / ".app_secret"), mask)
        assert not _readable(str(other / "backend" / "server.mjs"), mask)
        assert not _readable(str(other / "data"), mask)

    def test_unrelated_cron_sees_no_app(self, crew_home):
        script = crew_home / "crons" / "job.py"
        script.write_text("def run(ctx):\n    pass\n")

        mask = cron_apps_mask(script_file=str(script))

        assert mask.windows == ()
        for name in (OWN, OTHER):
            assert not _readable(str(crew_home / "apps" / name / ".app_secret"), mask)
            assert not _readable(str(crew_home / "apps" / name / "job.py"), mask)
        # An app installed after the spawn lands under the masked tree too.
        assert not _readable(str(crew_home / "apps" / "installed-later" / ".app_secret"), mask)

    def test_host_stamped_owner_gets_its_bundle(self, crew_home):
        # A builtin app's script lives in the package, so only ``created_by`` says
        # which installed tree is its own.
        mask = cron_apps_mask(owner_app=OWN)

        assert _readable(str(crew_home / "apps" / OWN / "backend" / "server.mjs"), mask)
        assert _writable(str(crew_home / "apps" / OWN / "data" / "x"), mask)
        assert not _readable(str(crew_home / "apps" / OWN / ".app_secret"), mask)
        assert not _readable(str(crew_home / "apps" / OTHER / "job.py"), mask)

    @pytest.mark.parametrize("owner", ["", "..", "../other-app", ".own-app-secret-tmp", "x/y"])
    def test_an_unsafe_owner_name_opens_nothing(self, crew_home, owner):
        assert cron_apps_mask(owner_app=owner).windows == ()

    def test_command_naming_a_bundle_reads_the_code_but_not_its_data(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(command=f"node {own / 'backend' / 'server.mjs'} --flag")

        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert not _writable(str(own / "backend" / "server.mjs"), mask)
        assert not _readable(str(own / "data"), mask)
        assert not _readable(str(own / ".app_secret"), mask)
        assert not _readable(str(crew_home / "apps" / OTHER / "job.py"), mask)

    def test_command_naming_a_secret_gets_no_secret(self, crew_home):
        target = crew_home / "apps" / OTHER / ".app_secret"
        mask = cron_apps_mask(command=f"cat {target}")

        assert not _readable(str(target), mask)

    def test_absent_apps_tree_is_created_so_the_mask_has_a_target(self, tmp_path, monkeypatch):
        crew = tmp_path / "home" / ".kiro" / "crew"
        crew.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("KIROCREW_HOME", str(crew))

        mask = cron_apps_mask()

        assert (crew / "apps").is_dir()
        assert str(crew / "apps") in mask.hidden

    def test_both_spellings_of_a_symlinked_home_are_masked(self, linked_crew_home):
        real, link = linked_crew_home

        mask = cron_apps_mask(script_file=str(real / "apps" / OWN / "job.py"))

        for root in (real, link):
            assert str(root / "apps") in mask.hidden, f"{root} spelling of apps/ is not masked"
            assert not _readable(str(root / "apps" / OTHER / ".app_secret"), mask)
            assert not _readable(str(root / "apps" / OWN / ".app_secret"), mask)
            assert _readable(str(root / "apps" / OWN / "backend" / "server.mjs"), mask)
            assert _writable(str(root / "apps" / OWN / "data" / "x"), mask)


class TestSettledBundlesOnly:
    @pytest.mark.parametrize("staging", ["data-tmp", "secret-tmp"])
    def test_a_bundle_mid_update_stays_masked(self, crew_home, staging):
        # The installer moves data/ and the secret out beside the bundle while it
        # swaps the tree; a secret restored after the child masked an absent name
        # would be readable through a window opened now.
        (crew_home / "apps" / f".{OWN}-{staging}").mkdir()
        own = crew_home / "apps" / OWN

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.windows == ()
        assert not _readable(str(own / "job.py"), mask)

    def test_each_window_is_pinned_to_the_planned_directory(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))

        pins = {path: (dev, ino) for path, dev, ino in mask.window_ids}
        for window in mask.windows:
            info = os.lstat(window)
            assert pins[window] == (info.st_dev, info.st_ino)

    def test_an_absent_data_dir_is_not_created_or_windowed(self, crew_home):
        own = crew_home / "apps" / OWN
        (own / "data").rmdir()

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert not (own / "data").exists()
        assert str(own / "data") not in mask.windows
        assert _readable(str(own / "job.py"), mask)

    def test_a_hardlinked_secret_is_reported_for_the_alias_scan(self, crew_home, tmp_path):
        secret = crew_home / "apps" / OTHER / ".app_secret"
        os.link(secret, tmp_path / "leak")
        info = os.lstat(secret)

        mask = cron_apps_mask()

        assert (info.st_dev, info.st_ino) in mask.alias_ids

    def test_any_aliased_secret_withholds_every_window(self, crew_home):
        # The link may sit inside the owner's own data/, which a window would expose.
        own = crew_home / "apps" / OWN
        os.link(crew_home / "apps" / OTHER / ".app_secret", own / "data" / "stolen")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.windows == ()
        assert not _readable(str(own / "data" / "stolen"), mask)

    def test_each_windowed_secret_is_a_required_mask(self, crew_home):
        # Moved aside before the child masks it, it must refuse the spawn, not be skipped.
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert str(own / ".app_secret") in mask.required
        assert str(crew_home / "apps" / OTHER / ".app_secret") not in mask.required

    def test_a_bundle_without_a_secret_stays_masked(self, crew_home):
        # A secret written into it later would be readable through the window.
        own = crew_home / "apps" / OWN
        (own / ".app_secret").unlink()

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.windows == ()

    def test_a_single_linked_secret_is_not_reported(self, crew_home):
        assert cron_apps_mask().alias_ids == ()

    def test_a_variable_spelled_command_path_names_the_bundle(self, crew_home, monkeypatch):
        own = crew_home / "apps" / OWN
        monkeypatch.setenv("APPS_ROOT_FOR_TEST", str(crew_home / "apps"))

        mask = cron_apps_mask(command=f"node $APPS_ROOT_FOR_TEST/{OWN}/backend/server.mjs")

        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert not _readable(str(own / ".app_secret"), mask)


def _launcher_constant(script: str, name: str):
    match = re.search(rf"^{name} = (?:frozenset\()?(.*?)\)?$", script, re.M)
    assert match, f"{name} not found in the launcher"
    return json.loads(match.group(1))


class TestLinuxLauncherLists:
    """The bind/hide lists the namespace backend's launcher embeds for this request."""

    def _script(self, mask) -> str:
        return sandbox_launcher._build_launcher_script(
            "cc",
            extra_hidden_dirs=mask.hidden,
            extra_private_dirs=mask.windows,
            extra_readonly_private_dirs=mask.readonly,
        )

    def test_own_bundle_window_is_sealed_and_data_is_not(self, crew_home):
        own = str(crew_home / "apps" / OWN)
        mask = cron_apps_mask(script_file=os.path.join(own, "job.py"))

        script = self._script(mask)
        dirs = _launcher_constant(script, "SENSITIVE_DIRS")
        files = _launcher_constant(script, "SENSITIVE_FILES")
        windows = _launcher_constant(script, "PRIVATE_DIRS")
        readonly = _launcher_constant(script, "READONLY_WINDOWS")

        assert str(crew_home / "apps") in dirs
        assert own in windows and own in readonly
        assert os.path.join(own, "data") in windows
        assert os.path.join(own, "data") not in readonly
        assert os.path.join(own, ".app_secret") in files
        assert not any(_under(w, str(crew_home / "apps" / OTHER)) for w in windows)

    def test_both_spellings_reach_the_launcher(self, linked_crew_home):
        real, link = linked_crew_home
        mask = cron_apps_mask(script_file=str(real / "apps" / OWN / "job.py"))

        script = self._script(mask)
        dirs = _launcher_constant(script, "SENSITIVE_DIRS")
        files = _launcher_constant(script, "SENSITIVE_FILES")
        windows = _launcher_constant(script, "PRIVATE_DIRS")
        readonly = _launcher_constant(script, "READONLY_WINDOWS")

        for root in (real, link):
            assert str(root / "apps") in dirs
            assert str(root / "apps" / OWN) in windows
            assert str(root / "apps" / OWN) in readonly
            assert str(root / "apps" / OWN / ".app_secret") in files

    def test_a_readonly_entry_that_is_not_a_window_is_inert(self, crew_home):
        script = sandbox_launcher._build_launcher_script(
            "cc",
            extra_hidden_dirs=(str(crew_home / "apps"),),
            extra_readonly_private_dirs=(str(crew_home / "apps" / OWN),),
        )

        assert _launcher_constant(script, "READONLY_WINDOWS") == []
        assert _launcher_constant(script, "PRIVATE_DIRS") == []


def test_namespace_argv_hands_required_secrets_to_the_launcher(crew_home, monkeypatch):
    from kiro_crew import sandbox

    seen: dict = {}

    def _capture(*_args, **kwargs):
        seen.update(kwargs)
        return "#!/usr/bin/env python3\n"

    monkeypatch.setattr(sandbox_launcher, "_build_launcher_script", _capture)
    secret = str(crew_home / "apps" / OWN / ".app_secret")

    sandbox.namespace_argv(
        ["true"], "cc", extra_hidden_dirs=(secret,), extra_required_mask_targets=(secret,)
    )

    assert secret in seen["required_mask_targets"]


def test_wrap_argv_forwards_required_secrets_to_the_namespace_backend(monkeypatch):
    from kiro_crew import sandbox

    seen: dict = {}

    def _namespace(argv, level, **kwargs):
        seen.update(kwargs)
        return ["launcher", *argv]

    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_kw: "namespace")
    monkeypatch.setattr(sandbox, "namespace_argv", _namespace)
    monkeypatch.setattr(sandbox, "_launcher_script_of", lambda _wrapped: None)

    sandbox.wrap_argv(
        ["true"],
        mode="cc",
        extra_hidden_dirs=("/x/apps",),
        extra_required_mask_targets=("/x/apps/a/.app_secret",),
    )

    assert seen["extra_required_mask_targets"] == ("/x/apps/a/.app_secret",)


class TestSeatbeltProfile:
    def test_bundle_is_read_only_and_secrets_stay_denied(self, crew_home):
        own = str(crew_home / "apps" / OWN)
        apps = str(crew_home / "apps")
        mask = cron_apps_mask(script_file=os.path.join(own, "job.py"))

        profile = sandbox_seatbelt._build_seatbelt_profile(
            "cc",
            extra_hidden_dirs=mask.hidden,
            extra_private_dirs=mask.windows,
            extra_readonly_private_dirs=mask.readonly,
        )
        lines = profile.splitlines()
        sub = f"(subpath {json.dumps(apps)})"
        read = [line for line in lines if line.startswith(f"(deny file-read* (require-all {sub}")]
        write = [line for line in lines if line.startswith(f"(deny file-write* (require-all {sub}")]

        assert read and f"(require-not (subpath {json.dumps(own)}))" in read[0]
        assert write, "writes under apps/ must stay denied"
        assert f"(require-not (subpath {json.dumps(own)}))" not in write[0]
        assert f"(require-not (subpath {json.dumps(os.path.join(own, 'data'))}))" in write[0]
        secret = json.dumps(os.path.join(own, ".app_secret"))
        assert f"(deny file-read* (literal {secret}))" in lines


class TestBothExecPathsPassTheMask:
    """What each cron call site hands ``wrap_argv``, recorded by a passthrough."""

    @pytest.fixture()
    def recorded(self, crew_home, monkeypatch):
        seen: list[dict] = []

        def _passthrough(argv, **kwargs):
            seen.append(kwargs)
            return list(argv), None

        monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", _passthrough)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:apps-secret-mask-test")
        src_dir = str(Path(__file__).resolve().parents[1] / "src")
        monkeypatch.setenv("PYTHONPATH", src_dir)
        return seen

    @staticmethod
    def _mask_of(kwargs):
        return cron_script.CronAppsMask(
            hidden=tuple(kwargs.get("extra_hidden_dirs", ())),
            windows=tuple(kwargs.get("extra_private_dirs", ())),
            readonly=tuple(kwargs.get("extra_readonly_private_dirs", ())),
        )

    def test_script_path(self, crew_home, recorded):
        own = crew_home / "apps" / OWN
        result = cron_script.run_script_sandboxed(f"{own / 'job.py'}:run", "job-x", timeout=60)

        assert result["status"] == "ok", result
        mask = self._mask_of(recorded[-1])
        assert recorded[-1]["extra_private_dir_ids"], "windows carry no identity pin"
        assert str(own / ".app_secret") in recorded[-1]["extra_required_mask_targets"]
        assert "extra_alias_credential_ids" in recorded[-1]
        assert _readable(str(own / "job.py"), mask)
        assert not _readable(str(own / ".app_secret"), mask)
        assert not _readable(str(crew_home / "apps" / OTHER / ".app_secret"), mask)

    def test_command_path(self, crew_home, recorded, monkeypatch):
        monkeypatch.setattr(cron_script, "_POSIX_STRICT_CACHE", {})
        if cron_script._resolve_command_shell() is None:
            pytest.skip("this host refuses command crons: no POSIX shell to run them")
        recorded.clear()

        result = cron_script.run_command_sandboxed(
            "true", timeout=60, job_id="job-y", owner_app=OWN
        )

        assert result["status"] == "ok", result
        mask = self._mask_of(recorded[-1])
        assert _readable(str(crew_home / "apps" / OWN / "backend" / "server.mjs"), mask)
        for name in (OWN, OTHER):
            assert not _readable(str(crew_home / "apps" / name / ".app_secret"), mask)
        assert not _readable(str(crew_home / "apps" / OTHER / "backend" / "server.mjs"), mask)
