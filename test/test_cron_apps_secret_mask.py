"""The apps tree is masked from every cron child that runs model-supplied code.

``<config_dir>/apps/<name>/.app_secret`` is a bearer credential, not a marker:
``dashboard.token_auth.validate_app_secret`` compares it and issues that app's scoped
token. Both cron exec paths run model-supplied code -- a script body under the
LLM-writeable ``crons/`` dir, and a ``command`` string the cron tool accepts verbatim --
so neither child may reach any app's secret.

What these tests pin is the WIRING, the mask target's existence, and the data window each
installed app keeps inside the mask. The two sandbox builders' own handling of a
caller-supplied ``extra_hidden_dirs`` tree and of a window inside it is pinned by
``test_sandbox_private_window_in_caller_mask.py``; asserting it again here would test
the builders twice and the call site not at all.

Every assertion is over the arguments the call site hands ``wrap_argv`` and over the
directory it leaves on disk. Nothing here spawns a child: the recording stub raises
after capturing, which the runners convert into a failed job.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import sandbox
from kiro_crew.cron_script import run_command_sandboxed, run_script_sandboxed
from kiro_crew.sandbox import (
    SandboxCeilingUnsealable,
    SandboxUnavailableError,
    materialize_caller_masked_dir,
)

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits / bind-mount mask")


def _window_paths(apps_tree: object) -> tuple[str, ...]:
    """The enumeration's window PATHS, dropping the identity each record carries.

    Most cases here are about which directories earn a window, not about the inode the
    child compares, so they read the paths alone; the identity has its own cases.
    """
    return tuple(w.path for w in sandbox.app_data_window_targets(str(apps_tree)))


@pytest.fixture(autouse=True)
def _cron_caller_is_named(named_cron_caller):
    """Cron spawn bookkeeping wants a nameable caller; these tests assume it."""


@pytest.fixture(autouse=True)
def cron_home(monkeypatch, tmp_path):
    """Point ``cron_script.config_dir`` at the per-test patched home.

    Same redirect as ``test_cron_secret_env.py``: scripts live under
    ``<home>/.kirocrew/crons`` so the resolver and the mask target agree on one root,
    and a test that does not patch ``Path.home`` falls back to a per-test tmp dir rather
    than the operator's real home, which this fixture must never create or key.
    """
    real_home = Path.home()
    fallback = tmp_path / "kirocrew-home-fallback"
    fallback.mkdir(parents=True, exist_ok=True)

    def _dir() -> Path:
        home = Path.home()
        root = fallback if home == real_home else home / ".kirocrew"
        # The real ``config_dir`` resolves AND creates the home on every call, so a stub
        # that did not would invent an absent-parent case production cannot reach.
        root.mkdir(parents=True, exist_ok=True)
        return root

    monkeypatch.setattr("kiro_crew.cron_script.config_dir", _dir)
    return _dir


def _make_script(tmp_path: Path, body: str = "def run(ctx): pass\n") -> Path:
    crons_dir = tmp_path / ".kirocrew" / "crons"
    crons_dir.mkdir(parents=True, exist_ok=True)
    script = crons_dir / "job.py"
    script.write_text(body, encoding="utf-8")
    return script


class _Recorder:
    """Captures one ``wrap_argv`` call, then refuses so no child is spawned.

    ``SandboxUnavailableError`` is the refusal both runners already convert into a
    failed-job dict, so the capture costs no subprocess and no special-case handling.
    """

    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        self.kwargs = dict(kwargs)
        raise SandboxUnavailableError(
            "recorded by the test stub", "test-stub", "the call site's arguments"
        )

    @property
    def hidden(self) -> list[str]:
        return list(self.kwargs.get("extra_hidden_dirs") or ())

    @property
    def windows(self) -> list[str]:
        return list(self.kwargs.get("extra_private_dirs") or ())


def _record(monkeypatch, *, mask_applies: bool = True) -> _Recorder:
    """Stub ``wrap_argv`` with a recorder, and state whether the mask is CARRIED.

    The two are one setting, not two. A recorder stands in for a wrap that happens, and
    the call sites materialize their mask target only when ``credential_mask_applies``
    says the spawn will carry the mask -- so a recorder left beside a false predicate
    would describe a host that wraps the argv and drops the mask, which is not a host
    the sandbox has. The default states the ordinary host; ``mask_applies=False`` names
    the one where the child comes back unwrapped.
    """
    recorder = _Recorder()
    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", recorder)
    monkeypatch.setattr("kiro_crew.cron_script.credential_mask_applies", lambda mode: mask_applies)
    return recorder


class TestTheScriptCronChildCannotReachAnyAppSecret:
    def test_an_ungranted_run_masks_the_apps_tree(self, tmp_path, monkeypatch):
        """The ordinary case, and the one that hid nothing at all.

        An ungranted script body is the least trustworthy cron source -- the ``crons/``
        dir is LLM-writeable by design -- and it was the branch passing an EMPTY hidden
        list, so every installed app's secret was readable.
        """
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert recorder.calls == 1
        assert str(tmp_path / ".kirocrew" / "apps") in recorder.hidden
        assert result["status"] == "error"

    def test_a_granted_run_masks_the_apps_tree_beside_what_it_already_hid(
        self, tmp_path, monkeypatch
    ):
        """A grant's isolation is the STRICTER of the two, so it cannot be the branch
        that keeps the credentials. Its existing entries stay: the live ``crons/`` dir
        and the script's own parent close the mutable-sibling import route, which the
        app mask has nothing to do with.
        """
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        # A grant that fails its precheck never reaches wrap_argv, so drive the granted
        # branch through the pin the runner itself computes.
        from kiro_crew.cron_script import compute_secret_env_pin
        from kiro_crew.secrets import SecretVault

        SecretVault(tmp_path / ".kirocrew").set_sync("slack-sandbox", "xoxb-script")
        grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
        spec = f"{script}:run"
        with patch("pathlib.Path.home", return_value=tmp_path):
            pin = compute_secret_env_pin(spec, "", job_id="job1", grant=grant)
            run_script_sandboxed(spec, "job1", timeout=30, secret_env=grant, secret_env_pin=pin)

        assert recorder.calls == 1
        hidden = recorder.hidden
        assert str(tmp_path / ".kirocrew" / "apps") in hidden
        assert str(tmp_path / ".kirocrew" / "crons") in hidden
        assert str(Path(str(script)).resolve().parent) in hidden

    def test_the_containing_tree_is_masked_not_a_list_of_installed_apps(
        self, tmp_path, monkeypatch
    ):
        """An app installed WHILE this run executes must be covered too.

        A mask is applied to the paths named at spawn and is never recomputed for a live
        child, so a list of leaves read out of ``apps/`` cannot name an app installed a
        second later -- that app's secret would stay readable for the rest of the
        child's life. Naming the directory is what covers whatever appears under it, so
        this asserts the absence of per-app entries as well as the tree's presence.
        """
        apps = tmp_path / ".kirocrew" / "apps"
        for name in ("dev-fleet", "meetings"):
            (apps / name).mkdir(parents=True)
            (apps / name / ".app_secret").write_text("s3cret", encoding="utf-8")
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        hidden = recorder.hidden
        assert str(apps) in hidden
        below = [h for h in hidden if h.startswith(str(apps) + os.sep)]
        assert below == [], f"per-app entries cannot cover a later install: {below}"


class TestTheCommandCronChildCannotReachAnyAppSecret:
    def test_a_command_run_masks_the_apps_tree(self, tmp_path, monkeypatch):
        """The sibling exec path, and the reason it is in scope.

        ``run_command_sandboxed``'s own comment records that the command string is fully
        model-supplied, so the two cron exec paths share one trust level and a control
        on only one of them is bypassable by choosing the other.
        """
        recorder = _record(monkeypatch)
        # The brace-expansion shell probe wraps an argv of its own; pin the shell so the
        # recorded call is the command's, not the probe's.
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "/bin/sh")
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_command_sandboxed("echo hi", timeout=30, job_id="job1")

        assert recorder.calls == 1
        assert str(tmp_path / ".kirocrew" / "apps") in recorder.hidden
        assert result["status"] == "error"


class TestTheMaskTargetExistsBeforeTheMaskIsBuilt:
    @_POSIX_ONLY
    def test_an_absent_apps_tree_is_created_so_the_bind_has_a_name(self, tmp_path, monkeypatch):
        """The sharpest case: a home where no app has ever been installed.

        The Linux launcher's ``SENSITIVE_DIRS`` loop is guarded on ``isdir``, so an
        absent target gets no empty bind over it and the FIRST-EVER install then appears
        inside this running child's view -- with its secret in it.
        """
        apps = tmp_path / ".kirocrew" / "apps"
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        assert not apps.exists()
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert apps.is_dir()
        assert stat.S_IMODE(apps.stat().st_mode) == 0o700
        assert str(apps) in recorder.hidden

    @_POSIX_ONLY
    def test_an_existing_apps_tree_keeps_its_mode_and_its_contents(self, tmp_path, monkeypatch):
        """The installed apps live here and the gateway owns the directory.

        Tightening its mode, or touching what is in it, would change an unrelated
        component's behaviour for a reason having nothing to do with this spawn.
        """
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "dev-fleet").mkdir(parents=True)
        (apps / "dev-fleet" / ".app_secret").write_text("keep-me", encoding="utf-8")
        apps.chmod(0o755)
        _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert stat.S_IMODE(apps.stat().st_mode) == 0o755
        assert (apps / "dev-fleet" / ".app_secret").read_text(encoding="utf-8") == "keep-me"

    @_POSIX_ONLY
    def test_a_symlinked_apps_leaf_refuses_the_run_before_any_child_starts(
        self, tmp_path, monkeypatch
    ):
        """``isdir`` follows a link, so the mask would cover the link's TARGET while the
        replaceable name stayed writable. Refuse instead of masking the wrong inode --
        and refuse before ``wrap_argv``, so nothing is spawned unmasked.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        apps = tmp_path / ".kirocrew" / "apps"
        apps.parent.mkdir(parents=True, exist_ok=True)
        apps.symlink_to(elsewhere, target_is_directory=True)
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert result["status"] == "error"
        assert recorder.calls == 0, "a refused mask must not reach the spawn"


class TestAHostCarryingNoMaskGainsNoNewRefusal:
    """The boundary the other direction: where the mask is DROPPED, nothing is created.

    ``credential_mask_applies`` is false exactly where ``wrap_argv`` hands back an
    unwrapped child, so every ``extra_hidden_dirs`` entry is dropped there. Creating or
    validating a mask target on such a host confines nothing, and its only possible
    effect is to fail a spawn the sandbox would otherwise allow -- so the materializer
    is not reached at all, and whatever the sandbox layer itself decides is what the
    operator hears.
    """

    @_POSIX_ONLY
    def test_the_apps_tree_is_not_created_when_the_mask_is_dropped(self, tmp_path, monkeypatch):
        apps = tmp_path / ".kirocrew" / "apps"
        recorder = _record(monkeypatch, mask_applies=False)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert recorder.calls == 1, "the spawn still reaches the sandbox layer"
        assert not apps.exists(), "an unwrapped child gains nothing from an empty dir"

    @_POSIX_ONLY
    def test_the_sandbox_layers_own_refusal_is_what_reaches_the_caller(self, tmp_path, monkeypatch):
        """The remedy an operator acts on lives in the sandbox layer's refusal.

        A create error standing in front of it would replace an actionable message with
        an incidental one on the very hosts that need the actionable one.
        """
        recorder = _record(monkeypatch, mask_applies=False)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert recorder.calls == 1
        assert result["status"] == "error"
        assert "recorded by the test stub" in result["error"]

    @_POSIX_ONLY
    def test_a_symlinked_apps_leaf_does_not_refuse_a_run_that_carries_no_mask(
        self, tmp_path, monkeypatch
    ):
        """The squat is only dangerous to a mask, and no mask is built here.

        Refusing on it would fail a spawn for a hazard this host cannot suffer, which is
        the new refusal the masking change deliberately does not add.
        """
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        apps = tmp_path / ".kirocrew" / "apps"
        apps.parent.mkdir(parents=True, exist_ok=True)
        apps.symlink_to(elsewhere, target_is_directory=True)
        recorder = _record(monkeypatch, mask_applies=False)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert recorder.calls == 1, "no mask to protect, so no refusal to add"
        assert apps.is_symlink(), "the squatted name is left exactly as found"


class TestEachInstalledAppKeepsItsDataInsideTheMask:
    """Masking the tree also covers ``apps/<app>/data``, which is not a credential.

    ``apps.manager.app_data_dir`` documents that directory as an app's persistence root,
    and the Linux mask binds a WRITABLE empty directory over the tree -- so a child
    writing there is told the write succeeded and the bytes go away with the namespace.
    Success with no data and no error is the worst shape a failure can take, so each
    installed app's data directory is passed as a private window: the mask and every
    ``.app_secret`` stay denied, and only those directories stay live on their real
    inodes.

    The builders' own handling of a window inside a caller mask is pinned by
    ``test_sandbox_private_window_in_caller_mask.py``; what these tests pin is that this
    call site composes the two arguments the builders already honour.
    """

    @staticmethod
    def _install(tmp_path: Path, *names: str) -> Path:
        apps = tmp_path / ".kirocrew" / "apps"
        for name in names:
            (apps / name).mkdir(parents=True, exist_ok=True)
            (apps / name / ".app_secret").write_text("secret", encoding="utf-8")
        return apps

    @_POSIX_ONLY
    def test_an_ungranted_script_run_keeps_every_installed_apps_data_live(
        self, tmp_path, monkeypatch
    ):
        apps = self._install(tmp_path, "alpha", "beta")
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert str(apps) in recorder.hidden
        assert recorder.windows == [str(apps / "alpha" / "data"), str(apps / "beta" / "data")]

    @_POSIX_ONLY
    def test_a_command_run_keeps_them_live_too(self, tmp_path, monkeypatch):
        """The command path is the same trust level, so it gets the same view."""
        apps = self._install(tmp_path, "alpha")
        recorder = _record(monkeypatch)
        # Pin the shell for the reason the sibling command test gives: the
        # brace-expansion probe wraps an argv of its own.
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "/bin/sh")
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_command_sandboxed("echo hi", timeout=30, job_id="job1")

        assert str(apps) in recorder.hidden
        assert recorder.windows == [str(apps / "alpha" / "data")]

    @_POSIX_ONLY
    def test_no_window_names_a_credential(self, tmp_path, monkeypatch):
        """The window is the DATA directory, so no spelling of it reaches a secret.

        A ``.app_secret`` sits beside ``data`` rather than inside it, so the ancestor
        mask still denies every one of them. This is the property that makes the window
        safe to open at all, which is why it is asserted rather than assumed.
        """
        self._install(tmp_path, "alpha", "beta")
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert recorder.windows, "the case is vacuous if no window was opened"
        for window in recorder.windows:
            assert os.path.basename(window) == "data"
            assert ".app_secret" not in window

    @_POSIX_ONLY
    def test_an_absent_data_dir_is_created_so_a_first_ever_write_survives(
        self, tmp_path, monkeypatch
    ):
        """The launcher's window loop is guarded on ``isdir`` like the mask loop.

        An app that has never written data has no ``data`` directory, so the window
        would be skipped in silence and the mask would cover the path -- losing exactly
        the first write, which is the one an app is most likely to make from a cron.
        """
        apps = self._install(tmp_path, "alpha")
        assert not (apps / "alpha" / "data").exists()
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert (apps / "alpha" / "data").is_dir()
        assert recorder.windows == [str(apps / "alpha" / "data")]

    @_POSIX_ONLY
    def test_a_dropped_mask_opens_no_window_and_creates_nothing(self, tmp_path, monkeypatch):
        """No mask, nothing to keep live inside it, and no directory to invent."""
        apps = self._install(tmp_path, "alpha")
        recorder = _record(monkeypatch, mask_applies=False)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert recorder.calls == 1
        assert recorder.windows == []
        assert not (apps / "alpha" / "data").exists()


class TestTheDataWindowEnumeration:
    """``app_data_window_targets`` degrades per app, and never fails the spawn.

    One app's on-disk layout must not stop every cron on the host, and the fail-closed
    answer for an unverifiable path is to withhold its view rather than to grant one.
    """

    @_POSIX_ONLY
    def test_an_absent_tree_yields_nothing(self, tmp_path):
        assert sandbox.app_data_window_targets(str(tmp_path / "nope")) == ()

    @_POSIX_ONLY
    def test_a_plain_file_where_the_tree_should_be_yields_nothing(self, tmp_path):
        target = tmp_path / "apps"
        target.write_text("not a tree", encoding="utf-8")
        assert sandbox.app_data_window_targets(str(target)) == ()

    @_POSIX_ONLY
    def test_a_symlinked_app_directory_is_skipped_rather_than_traversed(self, tmp_path):
        """Following it would re-expose the link's target inside the masked tree."""
        apps = tmp_path / "apps"
        (apps / "real").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (apps / "linked").symlink_to(elsewhere, target_is_directory=True)

        assert _window_paths(apps) == (str(apps / "real" / "data"),)
        assert not (elsewhere / "data").exists(), "nothing is created through the link"

    @_POSIX_ONLY
    def test_one_unusable_app_withholds_only_its_own_window(self, tmp_path):
        """A file squatting one ``data`` name leaves the other apps' windows intact."""
        apps = tmp_path / "apps"
        for name in ("alpha", "beta", "gamma"):
            (apps / name).mkdir(parents=True)
        (apps / "beta" / "data").write_text("squatter", encoding="utf-8")

        assert _window_paths(apps) == (
            str(apps / "alpha" / "data"),
            str(apps / "gamma" / "data"),
        )

    @_POSIX_ONLY
    def test_a_dangling_link_at_a_data_name_withholds_that_window(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / "alpha" / "data").symlink_to(tmp_path / "missing", target_is_directory=True)

        assert sandbox.app_data_window_targets(str(apps)) == ()

    @_POSIX_ONLY
    def test_an_existing_data_dir_keeps_its_contents(self, tmp_path):
        apps = tmp_path / "apps"
        data = apps / "alpha" / "data"
        data.mkdir(parents=True)
        (data / "state.json").write_text("{}", encoding="utf-8")

        assert _window_paths(apps) == (str(data),)
        assert (data / "state.json").read_text(encoding="utf-8") == "{}"


class TestALifecycleMoveAsideIsNotAnApp:
    """The apps tree holds the manager's own move-asides beside the installed apps.

    ``kiro_crew.apps.manager`` parks an app's data and secret under dot-prefixed names in
    the SAME directory while an install, update or uninstall is in flight, and leaves them
    there when the operation crashes. Because a window is CREATED when absent, an
    enumeration that counted one of those as an app would write a ``data`` child inside
    it, and the restore leg moves the move-aside onto the app's real persistence root.
    """

    #: Every dot-prefixed shape ``apps/manager.py`` parks beside an app, for ``alpha``.
    _MOVE_ASIDES = (
        ".alpha-data-tmp",
        ".alpha-secret-tmp",
        ".alpha-update-old-4321-beef",
        ".alpha-deps-doomed2",
    )

    @_POSIX_ONLY
    def test_a_data_move_aside_gets_no_window_and_gains_no_data_child(self, tmp_path):
        apps = tmp_path / "apps"
        parked = apps / ".alpha-data-tmp"
        parked.mkdir(parents=True)

        assert sandbox.app_data_window_targets(str(apps)) == ()
        assert not (parked / "data").exists()

    @_POSIX_ONLY
    @pytest.mark.parametrize("parked_name", _MOVE_ASIDES)
    def test_every_move_aside_shape_is_skipped(self, tmp_path, parked_name):
        apps = tmp_path / "apps"
        (apps / parked_name).mkdir(parents=True)

        assert sandbox.app_data_window_targets(str(apps)) == ()
        assert not (apps / parked_name / "data").exists()

    @_POSIX_ONLY
    def test_a_real_app_beside_a_move_aside_still_gets_its_window(self, tmp_path):
        """The skip is the name contract, not a reason to withhold a live app's window."""
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / ".beta-data-tmp").mkdir()

        assert _window_paths(apps) == (str(apps / "alpha" / "data"),)

    @_POSIX_ONLY
    def test_an_update_restores_preserved_data_without_a_nested_copy(self, tmp_path):
        """The harm, as the manager's own move-aside and restore legs perform it."""
        apps = tmp_path / "apps"
        data = apps / "alpha" / "data"
        data.mkdir(parents=True)
        (data / "keep.txt").write_text("user rows", encoding="utf-8")

        # update_app parks the data tree, replaces the app directory, then moves the
        # parked tree back onto the fresh copy. A cron spawn lands inside that window.
        parked = apps / ".alpha-data-tmp"
        data.rename(parked)
        sandbox.app_data_window_targets(str(apps))
        parked.rename(data)

        assert (data / "keep.txt").read_text(encoding="utf-8") == "user rows"
        assert not (data / "data").exists()
        assert sorted(p.name for p in data.iterdir()) == ["keep.txt"]


@_POSIX_ONLY
class TestAParkedCopyOfAnAppsDataIsNotMarkedStale:
    """Creating an absent ``data`` beside a parked copy is what destroys the copy.

    An interrupted update or uninstall leaves ``.<name>-data-tmp`` holding the app's only
    data. ``apps/manager.py`` reads "the parked copy AND ``data`` both exist" as proof the
    parked one is stale and removes it, so a cron spawn that creates ``data`` forges that
    proof and the next lifecycle op deletes the survivor. Both directions are pinned: the
    create is withheld, and an app whose ``data`` is already there keeps its window --
    without the second case a producer that withheld on any parked sibling would pass.
    """

    def test_an_app_with_a_parked_copy_gets_no_window_and_no_created_data(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / ".alpha-data-tmp").mkdir()

        assert sandbox.app_data_window_targets(str(apps)) == ()
        # The manager's own proof, left false: ``data`` does not exist, so the parked copy
        # is not stale and the restore leg still has something to move back.
        assert not (apps / "alpha" / "data").exists()

    def test_an_app_whose_data_already_exists_keeps_its_window(self, tmp_path):
        """Nothing is forged by a directory that was already there."""
        apps = tmp_path / "apps"
        (apps / "alpha" / "data").mkdir(parents=True)
        (apps / ".alpha-data-tmp").mkdir()

        assert _window_paths(apps) == (str(apps / "alpha" / "data"),)

    def test_another_apps_parked_copy_does_not_withhold_this_window(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / ".beta-data-tmp").mkdir()

        assert _window_paths(apps) == (str(apps / "alpha" / "data"),)

    def test_a_parked_file_of_that_name_is_not_a_parked_copy(self, tmp_path):
        """The manager parks a DIRECTORY; a file of that name proves nothing."""
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / ".alpha-data-tmp").write_text("x", encoding="utf-8")

        assert _window_paths(apps) == (str(apps / "alpha" / "data"),)


@_POSIX_ONLY
class TestTheNameShapeEarnsAWindow:
    """A window is built for a name shaped the way the app contract issues, and no other.

    The tier-collision test compares path TEXT, so it matches one spelling of a hidden
    app. On a case-insensitive filesystem -- the macOS default -- ``apps/AWS-Control``
    resolves to the ``apps/aws-control`` the tier hides, and a window carrying that
    directory would re-bind an owner-authorization store read-write for a cron child
    running model-supplied code. ``KEBAB_RE`` forces lowercase kebab-case, so the
    spelling no admission path ever issued never reaches the comparison.

    Shape is the whole question here, and the contract's RESERVED and unportable names are
    admitted: nothing revalidates an installed app's name when it loads, so one installed
    before its name was reserved is still live, and withholding its window would discard
    its cron writes with the mask.

    POSIX-only for a reason beyond convention: one case creates a directory named
    ``nul``, which Windows reserves as a device name and refuses.
    """

    #: Lowercase kebab names a NEW install is refused, which an old one can still hold.
    _RESERVED_KEBAB = ("system", "library", "registry", "nul")

    def test_a_case_variant_of_a_tier_hidden_app_gets_no_window(self, tmp_path):
        apps = tmp_path / "apps"
        squatter = apps / "AWS-Control"
        squatter.mkdir(parents=True)

        assert sandbox.app_data_window_targets(str(apps)) == ()
        assert not (squatter / "data").exists()

    def test_an_uppercase_name_gets_no_window(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "Alpha").mkdir(parents=True)

        assert sandbox.app_data_window_targets(str(apps)) == ()
        assert not (apps / "Alpha" / "data").exists()

    @pytest.mark.parametrize("reserved", _RESERVED_KEBAB)
    def test_a_reserved_name_installed_earlier_keeps_its_window(self, tmp_path, reserved):
        """Refusing one discards a live app's cron writes; the shape is already sound."""
        apps = tmp_path / "apps"
        (apps / reserved).mkdir(parents=True)

        assert _window_paths(apps) == (str(apps / reserved / "data"),)

    def test_a_name_the_contract_admits_still_gets_its_window(self, tmp_path):
        """The refusals are a filter on names, not a reason to withhold a real app's data."""
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / "Alpha-Two").mkdir()

        assert _window_paths(apps) == (str(apps / "alpha" / "data"),)


@_POSIX_ONLY
class TestAWindowCarriesTheIdentityItWasApprovedAs:
    """A real directory renamed onto an approved name must not inherit its window.

    ``O_NOFOLLOW`` at every component refuses a LINK planted at the name and settles
    nothing else: a real directory RENAMED onto it opens and pins cleanly, and the vacated
    original then fails the mask loop's ``isdir`` guard, so the substitute would be staged
    read-write AND take that tree's mask with it. The producer therefore reads the inode in
    the same act that approves the directory -- a later lookup from anywhere else would read
    whatever sits there by then, which is the substitute -- and the child compares the
    descriptor it pinned against it.
    """

    @staticmethod
    def _script(apps: Path, windows: tuple, ids: tuple) -> str:
        return sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(str(apps),),
            extra_private_dirs=windows,
            extra_private_dir_ids=ids,
        )

    def test_the_producer_reads_the_identity_of_the_directory_it_approves(self, tmp_path):
        apps = tmp_path / "apps"
        window = apps / "alpha" / "data"
        window.mkdir(parents=True)
        real = os.lstat(window)

        records = sandbox.app_data_window_targets(str(apps))

        assert [(r.path, r.dev, r.ino) for r in records] == [
            (str(window), real.st_dev, real.st_ino)
        ]

    def test_the_identity_reaches_the_child_and_is_compared_before_staging(self, tmp_path):
        apps = tmp_path / "apps"
        window = apps / "alpha" / "data"
        window.mkdir(parents=True)
        records = sandbox.app_data_window_targets(str(apps))

        script = self._script(apps, tuple(r.path for r in records), tuple(records))
        ids = json.loads(re.search(r"PRIVATE_DIR_IDS = (\{.*?\})\n", script, re.S).group(1))
        real = os.lstat(window)

        assert ids[str(window)] == [real.st_dev, real.st_ino]
        # Order is the property: a comparison after the bind has already exposed it.
        assert script.index("PRIVATE_DIR_IDS.get(p)") < script.index("staging private window")
        assert script.index("os.fstat(_win_fd)") < script.index("staging private window")

    def test_a_caller_that_vouches_for_nothing_is_unchanged(self, tmp_path):
        """The three pre-existing window producers pass paths only, and keep their behaviour."""
        apps = tmp_path / "apps"
        window = apps / "alpha" / "data"
        window.mkdir(parents=True)

        script = self._script(apps, (str(window),), ())
        windows = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        ids = json.loads(re.search(r"PRIVATE_DIR_IDS = (\{.*?\})\n", script, re.S).group(1))

        assert str(window) in windows
        assert ids == {}

    def test_the_comparison_is_skipped_rather_than_failed_without_an_identity(self, tmp_path):
        """A window with no entry must not be refused: absence is not a mismatch."""
        apps = tmp_path / "apps"
        window = apps / "alpha" / "data"
        window.mkdir(parents=True)

        script = self._script(apps, (str(window),), ())

        assert "_want_id = PRIVATE_DIR_IDS.get(p)" in script
        assert "if _want_id is not None:" in script


@_POSIX_ONLY
class TestTheMaskRootCarriesTheIdentityItWasApprovedAs:
    """A directory renamed onto the mask root must not be masked in the tree's place.

    The mask root travels as a NAME and the child binds an empty directory over whatever
    answers to it. A real directory renamed onto that name is masked in the original's
    place while the original stays readable at the name it moved to, which the name alone
    cannot detect. The producer therefore reads the inode in the act that settles which
    directory the mask is for, and the child refuses rather than skipping: a skipped mask
    leaves the tree exposed, which is the opposite of what a window's skip does.
    """

    @staticmethod
    def _script(apps: Path, ids: tuple) -> str:
        return sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(str(apps),),
            extra_hidden_dir_ids=ids,
        )

    def test_the_producer_reads_the_identity_of_the_root_it_approves(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()
        real = os.lstat(apps)

        assert sandbox.masked_dir_identity(str(apps)) == (
            str(apps),
            real.st_dev,
            real.st_ino,
        )

    def test_a_path_that_is_not_a_directory_is_refused(self, tmp_path):
        plain = tmp_path / "apps"
        plain.write_text("x")

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox.masked_dir_identity(str(plain))

    def test_an_absent_root_is_refused_rather_than_reported_as_no_identity(self, tmp_path):
        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox.masked_dir_identity(str(tmp_path / "apps"))

    def test_the_identity_reaches_the_child_and_is_compared_before_the_bind(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()
        real = os.lstat(apps)

        script = self._script(apps, (sandbox.masked_dir_identity(str(apps)),))
        ids = json.loads(re.search(r"SENSITIVE_DIR_IDS = (\{.*?\})\n", script, re.S).group(1))

        assert ids[str(apps)] == [real.st_dev, real.st_ino]
        # Order is the property: a comparison after the bind has already masked the wrong
        # directory, and the real tree is readable at its new name meanwhile.
        assert script.index("SENSITIVE_DIR_IDS.get(d)") < script.index(
            "hiding credential directory"
        )
        assert script.index("os.fstat(_mask_fd)") < script.index("hiding credential directory")

    def test_a_mismatch_refuses_the_spawn_instead_of_masking_the_substitute(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()

        script = self._script(apps, (sandbox.masked_dir_identity(str(apps)),))

        assert "is not the directory this spawn approved" in script
        # A skip would leave the tree unmasked, so the refusal is what the child does.
        assert "sys.exit(" in script.split("_want_dir_id = SENSITIVE_DIR_IDS.get(d)")[1][:900]

    def test_a_caller_that_vouches_for_nothing_keeps_the_plain_name_behaviour(self, tmp_path):
        """Every other caller passes names only, and the child treats them as before."""
        apps = tmp_path / "apps"
        apps.mkdir()

        script = self._script(apps, ())
        ids = json.loads(re.search(r"SENSITIVE_DIR_IDS = (\{.*?\})\n", script, re.S).group(1))
        masked = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))

        assert str(apps) in masked
        assert ids == {}
        assert "if _want_dir_id is not None:" in script

    def test_the_script_cron_vouches_for_the_apps_root_it_masks(self, tmp_path, monkeypatch):
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        apps = tmp_path / ".kirocrew" / "apps"
        real = os.lstat(apps)
        assert recorder.kwargs["extra_hidden_dir_ids"] == ((str(apps), real.st_dev, real.st_ino),)

    def test_the_command_cron_vouches_for_the_apps_root_it_masks(self, tmp_path, monkeypatch):
        recorder = _record(monkeypatch)
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "/bin/sh")
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_command_sandboxed("echo hi", timeout=30, job_id="job1")

        apps = tmp_path / ".kirocrew" / "apps"
        real = os.lstat(apps)
        assert recorder.kwargs["extra_hidden_dir_ids"] == ((str(apps), real.st_dev, real.st_ino),)


@_POSIX_ONLY
class TestALinkedAppSecretRefusesTheSpawn:
    """A symlinked ``.app_secret`` puts the credential bytes outside the masked tree.

    Nothing downstream reaches them: the mask denies a path the target is not under, no
    window is involved, and the launcher's alias scan considers only files with more than
    one HARD link while a symlink's target carries one. So the spawn is refused here.

    Deliberately narrower than refusing on any alias: a HARDLINKED secret is a state an
    ordinary host can reach, and refusing on it would let one app's on-disk layout stop
    every cron on the host.

    Break-arm: ``drop_the_linked_secret_refusal``.
    """

    def test_a_symlinked_secret_refuses(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        target = tmp_path / "elsewhere"
        target.write_text("bearer", encoding="utf-8")
        os.symlink(str(target), str(apps / "alpha" / ".app_secret"))

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            sandbox.refuse_if_an_app_secret_is_linked(str(apps))

        assert "is a link" in str(exc.value)

    def test_a_plain_secret_does_not_refuse(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / "alpha" / ".app_secret").write_text("bearer", encoding="utf-8")

        sandbox.refuse_if_an_app_secret_is_linked(str(apps))

    def test_a_hardlinked_secret_does_not_refuse(self, tmp_path):
        """The case that keeps its old handling: withhold windows, let the scan decide."""
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(tmp_path / "second-name"))

        sandbox.refuse_if_an_app_secret_is_linked(str(apps))

    def test_a_symlinked_app_directory_refuses(self, tmp_path):
        """The link one level up, whose leaf lstats as an ordinary file.

        The secret under a symlinked app directory is a REGULAR file at a path outside
        the masked tree, so the leaf test sees nothing wrong while the credential sits
        where no mask reaches.
        """
        apps = tmp_path / "apps"
        apps.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / ".app_secret").write_text("bearer", encoding="utf-8")
        os.symlink(str(outside), str(apps / "alpha"))

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            sandbox.refuse_if_an_app_secret_is_linked(str(apps))

        assert "is a link" in str(exc.value)

    def test_a_symlinked_app_directory_with_no_secret_yet_still_refuses(self, tmp_path):
        """The link ALONE is the harm: the writer fills it with real credential bytes.

        Waiting for a secret to be present would admit the spawn in exactly the window
        where the planter has not yet been handed one.
        """
        apps = tmp_path / "apps"
        apps.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(str(outside), str(apps / "alpha"))

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            sandbox.refuse_if_an_app_secret_is_linked(str(apps))

        assert "is a link" in str(exc.value)

    def test_an_unreadable_secret_refuses_rather_than_passing_over_it(self, tmp_path):
        """ "Cannot tell" must not be permissive on the path whose exposure IS the credential.

        Dropping the search bit on an app directory is an ordinary same-UID operation, so
        treating the resulting error as "no link here" would hand the whole check back for
        the cost of one chmod.
        """
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / "alpha" / ".app_secret").write_text("bearer", encoding="utf-8")
        os.chmod(apps / "alpha", 0o000)
        try:
            with pytest.raises(SandboxCeilingUnsealable) as exc:
                sandbox.refuse_if_an_app_secret_is_linked(str(apps))
        finally:
            os.chmod(apps / "alpha", stat.S_IRWXU)

        assert "could not be read" in str(exc.value)

    def test_an_unlistable_tree_refuses(self, tmp_path):
        """Same rule one level up: the entries it would refuse are what the error hides."""
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        os.chmod(apps, 0o000)
        try:
            with pytest.raises(SandboxCeilingUnsealable) as exc:
                sandbox.refuse_if_an_app_secret_is_linked(str(apps))
        finally:
            os.chmod(apps, stat.S_IRWXU)

        assert "could not be listed" in str(exc.value)

    def test_app_shaped_files_do_not_spend_the_bound(self, tmp_path):
        """A file carries no secret beneath it, so counting one costs availability only.

        A tree filled with app-shaped REGULAR FILES would otherwise reach the bound and
        refuse every cron on the host. That many app-shaped DIRECTORIES still refuses,
        which is the case the bound exists for.
        """
        apps = tmp_path / "apps"
        apps.mkdir()
        for i in range(sandbox.MAX_APP_DATA_WINDOWS + 1):
            (apps / ("app-%04d" % i)).write_text("x", encoding="utf-8")

        sandbox.refuse_if_an_app_secret_is_linked(str(apps))

    def test_a_plain_file_at_an_app_shaped_name_does_not_refuse(self, tmp_path):
        """Only a LINK refuses. A regular file carries no secret beneath it.

        Refusing on it would let one stray file in an agent-writable tree stop every
        cron on the host.
        """
        apps = tmp_path / "apps"
        apps.mkdir()
        (apps / "alpha").write_text("not a directory", encoding="utf-8")

        sandbox.refuse_if_an_app_secret_is_linked(str(apps))

    def test_a_missing_tree_does_not_refuse(self, tmp_path):
        sandbox.refuse_if_an_app_secret_is_linked(str(tmp_path / "absent"))

    def test_a_lifecycle_move_aside_is_not_an_app(self, tmp_path):
        """Only app-shaped names are consulted, so the manager's own dirs cannot refuse."""
        apps = tmp_path / "apps"
        (apps / ".alpha-data-tmp").mkdir(parents=True)
        target = tmp_path / "elsewhere"
        target.write_text("bearer", encoding="utf-8")
        os.symlink(str(target), str(apps / ".alpha-data-tmp" / ".app_secret"))

        sandbox.refuse_if_an_app_secret_is_linked(str(apps))

    def test_more_app_shaped_entries_than_the_bound_refuses(self, tmp_path):
        """Truncating at the bound would make the bound the bypass.

        The 257th app-shaped directory is the one an attacker puts the linked secret in, so
        stopping there leaves exactly that credential unexamined.
        """
        apps = tmp_path / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS + 1):
            (apps / ("app-%04d" % i)).mkdir(parents=True)

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            sandbox.refuse_if_an_app_secret_is_linked(str(apps))

        assert "cannot all be checked for" in str(exc.value)

    def test_a_tree_right_at_the_bound_does_not_refuse(self, tmp_path):
        apps = tmp_path / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS):
            (apps / ("app-%04d" % i)).mkdir(parents=True)

        sandbox.refuse_if_an_app_secret_is_linked(str(apps))

    def test_the_bound_is_not_spent_by_lifecycle_move_asides(self, tmp_path):
        """Shape gates the count here too, or the manager's own dirs refuse every cron."""
        apps = tmp_path / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS + 5):
            (apps / (".%04d-data-tmp" % i)).mkdir(parents=True)
        (apps / "alpha").mkdir()

        sandbox.refuse_if_an_app_secret_is_linked(str(apps))

    def test_a_full_tree_plus_one_unrelated_entry_does_not_refuse(self, tmp_path):
        """The bound counts app-shaped real directories, so only one may TEST it.

        With the test placed before the shape filters, a tree already holding the bound
        refuses on its next entry whatever that entry is -- a lifecycle move-aside, a stray
        file -- and the refusal names a condition that entry does not meet.
        """
        apps = tmp_path / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS):
            (apps / ("app-%04d" % i)).mkdir(parents=True)
        (apps / ".alpha-data-tmp").mkdir()
        (apps / "NotAnApp.txt").write_text("stray\n")

        sandbox.refuse_if_an_app_secret_is_linked(str(apps))

    def test_a_full_tree_plus_one_more_app_still_refuses(self, tmp_path):
        """The control: the condition the bound exists for must still be caught."""
        apps = tmp_path / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS):
            (apps / ("app-%04d" % i)).mkdir(parents=True)
        (apps / ".alpha-data-tmp").mkdir()
        (apps / "one-more-app").mkdir()

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            sandbox.refuse_if_an_app_secret_is_linked(str(apps))

        assert "cannot all be checked for" in str(exc.value)

    def test_the_script_cron_refuses_before_spawning(self, tmp_path, monkeypatch):
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "alpha").mkdir(parents=True)
        target = tmp_path / "planted"
        target.write_text("bearer", encoding="utf-8")
        os.symlink(str(target), str(apps / "alpha" / ".app_secret"))

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert result["status"] == "error"
        assert "is a link" in result["error"]
        assert recorder.kwargs == {}, "the spawn must not be wrapped at all"


@_POSIX_ONLY
class TestTheCronSpawnSuppliesTheAliasedCredentialInodes:
    """An alias to a secret at a path NO mask covers is read through that path.

    Withholding windows cannot answer it, and neither can the mask. The launcher's pre-exec
    hardlink scan is what answers it, and it collects from a masked DIRECTORY at depth 1 --
    one level above ``apps/<app>/.app_secret``.

    The child cannot read those secrets itself. It masks that tree in its own process,
    binding an empty directory over it, so any stat it takes there reports ENOENT: a
    collection placed in the child arms the walk on nothing and the control is dead, while a
    test that pointed the extracted block at a readable directory still passes. So the PARENT
    reads the inodes, before any mask exists, and they cross as ``(device, inode)`` pairs.

    Break-arm: ``drop_the_cron_alias_credential_ids``.
    """

    def test_the_script_cron_supplies_the_aliased_inode(self, tmp_path, monkeypatch):
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "alpha").mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(tmp_path / "second-name"))
        st = secret.stat()
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert recorder.kwargs["extra_alias_credential_ids"] == ((st.st_dev, st.st_ino),)

    def test_the_command_cron_supplies_the_aliased_inode(self, tmp_path, monkeypatch):
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "alpha").mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(tmp_path / "second-name"))
        st = secret.stat()
        recorder = _record(monkeypatch)
        monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: "/bin/sh")
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_command_sandboxed("echo hi", timeout=30, job_id="job1")

        assert recorder.kwargs["extra_alias_credential_ids"] == ((st.st_dev, st.st_ino),)

    def test_an_unaliased_tree_supplies_nothing(self, tmp_path, monkeypatch):
        """A healthy host arms no walk, so the common spawn pays exactly as before."""
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / "alpha" / ".app_secret").write_text("bearer", encoding="utf-8")
        recorder = _record(monkeypatch)
        script = _make_script(tmp_path)
        with patch("pathlib.Path.home", return_value=tmp_path):
            run_script_sandboxed(f"{script}:run", "job1", timeout=30)

        assert recorder.kwargs["extra_alias_credential_ids"] == ()

    def test_the_pairs_reach_the_launcher_as_a_literal(self, tmp_path):
        """A literal, not a scan: the child does no filesystem read of the apps tree."""
        apps = tmp_path / "apps"
        apps.mkdir()

        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(str(apps),), extra_alias_credential_ids=((7, 99),)
        )

        ids = json.loads(re.search(r"ALIAS_CREDENTIAL_IDS = (\[.*?\])\n", script, re.S).group(1))
        assert ids == [[7, 99]]
        assert "for _acid in ALIAS_CREDENTIAL_IDS:" in script
        assert "os.scandir(_asr)" not in script, "the child must not read the tree"

    def test_the_seatbelt_backend_is_not_handed_inodes_it_cannot_use(self):
        """Stated boundary, not an oversight: there is no launcher script to scan in.

        The seatbelt path writes a profile and execs ``sandbox-exec``; the pre-exec scan is
        a step of the Linux launcher, so the parameter has no consumer there and is not
        forwarded. Every dispatch that DOES carry the mask to the Linux launcher carries it.
        """
        source = inspect.getsource(sandbox.wrap_argv)
        dispatches = [
            block
            for block in re.findall(r"\w+_argv\((?:[^()]|\([^()]*\))*\)", source)
            if "extra_hidden_dirs=" in block
        ]
        assert dispatches, "no dispatch carries a caller's mask"
        for block in dispatches:
            if "namespace_argv(" in block:
                assert "extra_alias_credential_ids=" in block, block
            else:
                assert "extra_alias_credential_ids=" not in block, block


class TestTheSeatbeltBackendVerifiesTheWindowItOpens:
    """A Seatbelt profile is path rules end to end, so it cannot enforce an inode at exec.

    It can still read the name's OWN identity immediately before the profile is written,
    which is what the mask roots get, so a window gets the same: granted when it is the
    directory the producer approved, and the spawn refused when it is not. Dropping every
    pinned window is not available -- both cron call sites pin every window they pass, so
    that would deny each app its own persistence directory on every macOS spawn. A window
    nobody pinned is unchanged.
    """

    @staticmethod
    def _captured(**kwargs: object) -> dict:
        seen: dict = {}

        def fake_profile(level: str, **kw: object) -> str:
            seen.update(kw)
            return "(version 1)(allow default)"

        with patch.object(sandbox, "_build_seatbelt_profile", fake_profile):
            sandbox.sandbox_exec_argv(["/bin/true"], "cc", **kwargs)  # type: ignore[arg-type]
        return seen

    def test_a_pinned_window_that_is_the_approved_directory_is_granted(self, tmp_path):
        window = tmp_path / "apps" / "alpha" / "data"
        window.mkdir(parents=True)
        real = os.lstat(window)

        seen = self._captured(
            extra_hidden_dirs=(str(tmp_path / "apps"),),
            extra_private_dirs=(str(window),),
            extra_private_dir_ids=((str(window), real.st_dev, real.st_ino),),
        )

        assert seen["extra_private_dirs"] == (str(window),)

    def test_a_pinned_window_that_was_replaced_refuses_the_spawn(self, tmp_path):
        window = tmp_path / "apps" / "alpha" / "data"
        window.mkdir(parents=True)
        real = os.lstat(window)

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="not the one this spawn"):
            self._captured(
                extra_hidden_dirs=(str(tmp_path / "apps"),),
                extra_private_dirs=(str(window),),
                extra_private_dir_ids=((str(window), real.st_dev, real.st_ino + 1),),
            )

    def test_a_pinned_window_that_is_now_a_file_refuses_the_spawn(self, tmp_path):
        (tmp_path / "apps" / "alpha").mkdir(parents=True)
        window = tmp_path / "apps" / "alpha" / "data"
        window.write_text("not a directory\n")
        real = os.lstat(window)

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="no longer a directory"):
            self._captured(
                extra_hidden_dirs=(str(tmp_path / "apps"),),
                extra_private_dirs=(str(window),),
                extra_private_dir_ids=((str(window), real.st_dev, real.st_ino),),
            )

    def test_a_pinned_window_whose_name_is_gone_refuses_the_spawn(self, tmp_path):
        window = tmp_path / "apps" / "alpha" / "data"

        with pytest.raises(sandbox.SandboxCeilingUnsealable, match="cannot confirm"):
            self._captured(
                extra_hidden_dirs=(str(tmp_path / "apps"),),
                extra_private_dirs=(str(window),),
                extra_private_dir_ids=((str(window), 66, 1234),),
            )

    def test_an_unpinned_window_is_still_granted(self):
        """The three pre-existing producers pin nothing, and keep their windows."""
        window = "/home/someone/.kirocrew/scratch/session/probe"

        seen = self._captured(
            extra_hidden_dirs=("/home/someone/.kirocrew/scratch",),
            extra_private_dirs=(window,),
        )

        assert seen["extra_private_dirs"] == (window,)

    def test_an_identity_for_a_window_this_spawn_does_not_open_is_not_checked(self, tmp_path):
        """Only the windows actually passed are verified; a stale id entry alone is inert."""
        window = tmp_path / "apps" / "alpha" / "data"

        seen = self._captured(
            extra_hidden_dirs=(str(tmp_path / "apps"),),
            extra_private_dirs=(),
            extra_private_dir_ids=((str(window), 66, 1234),),
        )

        assert seen["extra_private_dirs"] == ()


class TestTheMaskRootIdentityReachesBothBackends:
    """One approval, one rule for confirming it: no-follow, a real directory or nothing.

    A mask that cannot be confirmed REFUSES rather than skipping, because a skipped mask
    leaves the tree it was asked to hide fully readable -- the opposite of a window's skip.
    """

    @staticmethod
    def _captured(**kwargs: object) -> dict:
        seen: dict = {}

        def fake_profile(level: str, **kw: object) -> str:
            seen.update(kw)
            return "(version 1)(allow default)"

        with patch.object(sandbox, "_build_seatbelt_profile", fake_profile):
            sandbox.sandbox_exec_argv(["/bin/true"], "cc", **kwargs)  # type: ignore[arg-type]
        return seen

    def test_a_pinned_mask_root_that_still_matches_is_masked(self, tmp_path):
        """The approval is CONSUMED here, not discarded: a match proceeds as before."""
        apps = tmp_path / ".kirocrew" / "apps"
        apps.mkdir(parents=True)
        st = os.lstat(str(apps))

        seen = self._captured(
            extra_hidden_dirs=(str(apps),),
            extra_hidden_dir_ids=((str(apps), st.st_dev, st.st_ino),),
        )

        assert seen["extra_hidden_dirs"] == (str(apps),)

    def test_a_replaced_mask_root_refuses_the_spawn(self, tmp_path):
        """Withholding a MASK would open the tree, so the fail-closed answer is refusal.

        Break-arm: ``drop_seatbelt_mask_identity``.
        """
        apps = tmp_path / ".kirocrew" / "apps"
        apps.mkdir(parents=True)
        substitute = tmp_path / ".kirocrew" / "other"
        substitute.mkdir()
        approved = os.lstat(str(substitute))

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            self._captured(
                extra_hidden_dirs=(str(apps),),
                extra_hidden_dir_ids=((str(apps), approved.st_dev, approved.st_ino),),
            )

        assert "not the one this spawn approved" in str(exc.value)

    def test_an_unreadable_mask_root_refuses_the_spawn(self, tmp_path):
        apps = tmp_path / ".kirocrew" / "apps"

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            self._captured(
                extra_hidden_dirs=(str(apps),),
                extra_hidden_dir_ids=((str(apps), 66, 1234),),
            )

        assert "cannot confirm the masked directory" in str(exc.value)

    def test_a_mask_root_moved_aside_behind_a_symlink_refuses_the_spawn(self, tmp_path):
        """A rename PRESERVES the inode, so a followed lookup accepts the substitution.

        The tree is moved aside and a link left at the approved name. Following the link
        reaches the same ``(dev, ino)`` the approval recorded, so the comparison alone
        cannot see it, while the profile rule covers the link's name and the tree stays
        readable where it was moved to.

        Break-arm: ``follow_the_seatbelt_mask_identity``.
        """
        crew = tmp_path / ".kirocrew"
        crew.mkdir(parents=True)
        apps = crew / "apps"
        apps.mkdir()
        approved = os.lstat(str(apps))
        moved = crew / "apps.moved"
        os.rename(str(apps), str(moved))
        os.symlink(str(moved), str(apps))

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            self._captured(
                extra_hidden_dirs=(str(apps),),
                extra_hidden_dir_ids=((str(apps), approved.st_dev, approved.st_ino),),
            )

        assert "no longer a directory" in str(exc.value)

    def test_the_backends_agree_on_how_a_mask_root_is_confirmed(self):
        """One approval, so one rule: no-follow, and a real directory or nothing.

        The producer records the identity under those two rules and the Linux child
        re-reads it under them. A backend confirming the same approval by a followed
        lookup compares a different object than the one that was approved.

        Break-arm: ``follow_the_seatbelt_mask_identity``.
        """
        source = inspect.getsource(sandbox.sandbox_exec_argv)
        confirm = source[source.index("for path_, dev, ino in extra_hidden_dir_ids:") :]

        assert "os.lstat(path_)" in confirm
        assert "os.stat(path_)" not in confirm
        assert "stat.S_ISDIR(st.st_mode)" in confirm

    def test_a_mask_identity_alone_still_reaches_this_backend(self):
        """No dispatch may carry a caller's MASK without the identity it was approved as.

        Both backend families, because the defect is the same either way and only one of
        them is this class's own: a dispatch that forwards ``extra_hidden_dirs`` and drops
        ``extra_hidden_dir_ids`` hands its backend a pathname where an inode was settled.
        The Linux hop was unpinned until a mutation removed it and every case here stayed
        green.

        Break-arm: ``drop_a_mask_identity_forward`` (either dispatch).
        """
        source = inspect.getsource(sandbox.wrap_argv)
        dispatches = [
            block
            for block in re.findall(r"\w+_argv\((?:[^()]|\([^()]*\))*\)", source)
            if "extra_hidden_dirs=" in block
        ]

        assert len(dispatches) >= 3, dispatches
        for block in dispatches:
            assert "extra_hidden_dir_ids=" in block, block
        # And the branch tests that gate them: an identity supplied without a path list
        # would otherwise take the no-extras route and be dropped before any dispatch.
        seatbelt = [b for b in dispatches if b.startswith("sandbox_exec_argv(")]
        assert source.count("or extra_hidden_dir_ids") == len(seatbelt)


class TestTheParentCollectsTheAliasedCredentialInodes:
    """``aliased_app_secret_ids`` is the read that has to happen before any mask."""

    def test_a_hardlinked_secret_is_reported(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(tmp_path / "second-name"))
        st = secret.stat()

        assert sandbox.aliased_app_secret_ids(str(apps)) == ((st.st_dev, st.st_ino),)

    def test_an_unaliased_secret_is_not_reported(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / "alpha" / ".app_secret").write_text("bearer", encoding="utf-8")

        assert sandbox.aliased_app_secret_ids(str(apps)) == ()

    def test_an_app_without_a_secret_is_passed_over(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha" / "data").mkdir(parents=True)

        assert sandbox.aliased_app_secret_ids(str(apps)) == ()

    def test_a_missing_tree_reports_nothing(self, tmp_path):
        assert sandbox.aliased_app_secret_ids(str(tmp_path / "absent")) == ()

    def test_a_lifecycle_move_aside_does_not_spend_the_bound(self, tmp_path):
        """Shape before the count here too, or a real app is pushed past the bound."""
        apps = tmp_path / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS + 5):
            (apps / (".%04d-data-tmp" % i)).mkdir(parents=True)
        (apps / "alpha").mkdir()
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(tmp_path / "second-name"))
        st = secret.stat()

        assert sandbox.aliased_app_secret_ids(str(apps)) == ((st.st_dev, st.st_ino),)

    def test_a_symlinked_secret_is_not_this_passs_answer(self, tmp_path):
        """Its target carries one link, so the walk could never match it.

        That shape is refused before the spawn instead, where the producer sees it.
        """
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        target = tmp_path / "elsewhere"
        target.write_text("bearer", encoding="utf-8")
        os.symlink(str(target), str(apps / "alpha" / ".app_secret"))

        assert sandbox.aliased_app_secret_ids(str(apps)) == ()


class TestATierMaskedAppDataTreeGetsNoWindow:
    """An app whose data the tier masks in its own right keeps it masked.

    ``apps/aws-control/data`` and ``apps/meetings/data/edits`` are hidden in every mode
    -- one holds live AWS credentials, the other owner-authorization bits -- and they sit
    INSIDE the tree this call site masks. A window is re-bound read-write over that
    tree, and it is staged from the real inode before any mask is applied, so a window
    at or above one of those leaves would carry it back out: the ordinary
    ``.app_secret`` denial would be traded for a credential store.

    Two relationships have to be refused, and asking whether the window merely sits
    under SOME hidden parent catches neither: the window EQUALS the hidden leaf
    (``aws-control``), or the window CONTAINS it (``meetings``, whose ``data`` holds the
    hidden ``data/edits``).
    """

    @staticmethod
    def _install(tmp_path: Path, *names: str) -> Path:
        apps = tmp_path / ".kirocrew" / "apps"
        for name in names:
            (apps / name / "data").mkdir(parents=True, exist_ok=True)
        return apps

    @_POSIX_ONLY
    def test_a_window_equal_to_a_hidden_leaf_is_withheld(self, tmp_path):
        apps = self._install(tmp_path, "alpha", "aws-control")
        with patch("pathlib.Path.home", return_value=tmp_path):
            windows = _window_paths(apps)

        assert windows == (str(apps / "alpha" / "data"),)

    @_POSIX_ONLY
    def test_a_window_containing_a_hidden_leaf_is_granted_and_the_leaf_re_masked(self, tmp_path):
        """The shipped ``meetings`` app is this case, and withholding its window lost data.

        ``apps/meetings/data/edits`` is a tier leaf, so ``apps/meetings/data`` CONTAINS one.
        Refusing the window there left the app's documented persistence root covered by a
        writable empty bind: a cron child's write reported success and the bytes went with
        the namespace. The window is granted instead, and the nested leaf is re-hidden AFTER
        it is bound -- which is the ordering that keeps the leaf masked.
        """
        apps = self._install(tmp_path, "alpha", "meetings")
        with patch("pathlib.Path.home", return_value=tmp_path):
            windows = _window_paths(apps)

        assert windows == (
            str(apps / "alpha" / "data"),
            str(apps / "meetings" / "data"),
        )

    @_POSIX_ONLY
    def test_the_launcher_carries_a_containing_window_and_the_profile_does_not(self):
        """The two backends differ here on purpose, because only one can re-mask.

        The launcher re-hides the nested leaf after binding the window, so it may carry a
        containing window. A Seatbelt profile cannot order its rules that way, so the same
        window is still refused there -- which is what keeps the three pre-existing
        producers' macOS behaviour unchanged.
        """
        home = os.path.expanduser("~")
        apps = os.path.join(home, ".kirocrew", "apps")
        window = os.path.join(apps, "meetings", "data")

        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(window,)
        )
        carried = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        profile_windows = sandbox._private_window_spellings(
            (window,), [apps, os.path.join(window, "edits")]
        )

        assert window in carried
        assert profile_windows == []

    @_POSIX_ONLY
    def test_the_launcher_re_hides_a_nested_leaf_after_binding_the_window(self):
        """Order is the property: before the window, the re-mask lands on a shadowed path."""
        home = os.path.expanduser("~")
        apps = os.path.join(home, ".kirocrew", "apps")
        window = os.path.join(apps, "meetings", "data")

        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(window,)
        )

        assert script.index("opening private window") < script.index(
            "re-hiding nested masked directory"
        )

    @_POSIX_ONLY
    def test_a_window_equal_to_a_hidden_leaf_is_still_refused_everywhere(self):
        """The EQUALS case has no ordering that saves it: the window IS the masked tree."""
        home = os.path.expanduser("~")
        leaf = os.path.join(home, ".kirocrew", "apps", "aws-control", "data")

        assert sandbox._window_is_a_hidden_target(leaf, [leaf])
        for remasks in (False, True):
            assert (
                sandbox._private_window_spellings(
                    (leaf,),
                    [os.path.join(home, ".kirocrew", "apps"), leaf],
                    remasks_contained_targets=remasks,
                )
                == []
            )

    @_POSIX_ONLY
    def test_the_launcher_keeps_the_leaf_denied_and_opens_no_window_for_it(self):
        """The gate refuses the collision even when a caller names it directly.

        This is the guarantee that does not depend on the enumeration above: any caller
        handing the launcher such a window gets it dropped, and the leaf stays in the
        masked list, while an ordinary app's window on the same tree survives.
        """
        home = os.path.expanduser("~")
        apps = os.path.join(home, ".kirocrew", "apps")
        leaf = os.path.join(apps, "aws-control", "data")
        ordinary = os.path.join(apps, "alpha", "data")
        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(leaf, ordinary)
        )
        masked = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        windows = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))

        assert leaf not in windows
        assert leaf in masked
        assert ordinary in windows


class TestAnAppNameCannotForgeTheOperatorsLog:
    """An app DIRECTORY NAME is agent-created, so it never reaches the log raw.

    ``gateway.log`` uses a plain formatter, so a name holding a newline or an ANSI escape
    could forge log lines and rewrite the operator's terminal on every cron run. Two
    things keep it out. The name contract admits lowercase kebab-case only, so such a
    name earns no window and reaches no refusal branch -- that is what these cases
    assert, including that the contract wins over the later per-app refusal the same
    directory also triggers. And every log site that does interpolate an entry's name
    wraps it in ``safe_terminal_line``, asserted over the source below because no name
    the contract admits can exercise it.
    """

    @_POSIX_ONLY
    def test_a_newline_in_an_app_name_does_not_reach_the_log_record(self, tmp_path, caplog):
        apps = tmp_path / ".kirocrew" / "apps"
        forged = apps / "alpha\nSECURITY: forged"
        # A plain file at the ``data`` name is the per-app refusal branch, which logs.
        # The name contract refuses first, so that branch is never reached.
        forged.mkdir(parents=True)
        (forged / "data").write_text("x", encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.sandbox"):
            assert sandbox.app_data_window_targets(str(apps)) == ()

        for record in caplog.records:
            assert "\n" not in record.getMessage()
            assert "SECURITY: forged" not in record.getMessage()

    @_POSIX_ONLY
    def test_an_escape_in_an_app_name_does_not_reach_the_log_record(self, tmp_path, caplog):
        apps = tmp_path / ".kirocrew" / "apps"
        forged = apps / "alpha\x1b[2J"
        forged.mkdir(parents=True)
        (forged / "data").write_text("x", encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.sandbox"):
            assert sandbox.app_data_window_targets(str(apps)) == ()

        for record in caplog.records:
            assert "\x1b" not in record.getMessage()

    def test_every_log_site_that_names_an_entry_sanitizes_it(self):
        """The sanitization stays even though no admitted name can demonstrate it.

        Scoped to ``logger`` calls inside this one function: an app name reaching an
        argument list raw is the regression, and it cannot be caught by calling the
        function once the contract refuses every name that would prove it.

        BOTH spellings the function uses, so renaming the loop variable cannot retire the
        pin by making its search term stale: the directory entry during the bounded read
        (``entry.name``) and the admitted name during the window pass (``name``).
        """
        source = inspect.getsource(sandbox.app_data_window_targets)
        calls = re.findall(r"logger\.\w+\((?:[^()]|\([^()]*\))*\)", source, re.DOTALL)
        spellings = (r"entry\.name", r"name")
        naming = [
            call
            for call in calls
            if any(re.search(r"\b%s\b" % spelling, call) for spelling in spellings)
        ]

        assert naming, "no log site names an entry, so this pin has nothing to protect"
        for call in naming:
            for spelling in spellings:
                # Every occurrence of the name must be the argument of
                # ``safe_terminal_line``, so a second raw one beside a wrapped one fails.
                assert not re.search(
                    r"(?<!safe_terminal_line\()\b%s\b(?!\))" % spelling, call
                ), call


class TestTheLauncherPinsAWindowAgainstASwappedAncestor:
    """The window is resolved a component at a time, from its mask entry down.

    ``O_NOFOLLOW`` on one open of the whole path refuses a link only at the LAST
    component, so a same-uid process that swaps an ANCESTOR -- making ``apps/alpha`` a
    link to ``apps/aws-control`` after the parent enumerated the windows -- would still
    have its target traversed, and the staging bind runs before the mask loop, so the
    masked leaf would be re-bound read-write at the window's path.

    Asserted over the generated launcher text, which is where the walk lives: the
    descriptor-relative form must be present AND the whole-path form must be absent.
    The second half is the one that catches a regression, since a reviewer adding a
    convenience open beside the walk leaves the first half true.
    """

    @_POSIX_ONLY
    def test_every_component_below_the_mask_is_opened_relative_to_a_descriptor(self):
        script = sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(os.path.join(os.path.expanduser("~"), ".kirocrew", "apps"),),
            extra_private_dirs=(
                os.path.join(os.path.expanduser("~"), ".kirocrew", "apps", "alpha", "data"),
            ),
        )

        assert "os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY" in script
        assert "dir_fd=fd" in script
        # The regression shape: one open of the joined path, which leaves every
        # ancestor component following.
        assert "os.open(p, os.O_PATH" not in script

    @_POSIX_ONLY
    def test_the_bind_source_is_the_descriptor_not_the_name(self):
        """A second name resolution is a second chance to be redirected."""
        script = sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(os.path.join(os.path.expanduser("~"), ".kirocrew", "apps"),),
            extra_private_dirs=(
                os.path.join(os.path.expanduser("~"), ".kirocrew", "apps", "alpha", "data"),
            ),
        )

        assert "/proc/self/fd/%d" in script
        assert 'staging private window %s" % p' in script


@_POSIX_ONLY
class TestNoSecondPathToAWindowOutlivesTheMask:
    """A window's staging mount is retired once every window is bound.

    The stage holds the window's real inode across the bind that hides its parent tree.
    Left in place it is the same tree reachable under a directory nothing masks, and the
    re-mask that re-hides a masked leaf INSIDE the window lands on the window's own path:
    a non-recursive bind carries no submount, so the leaf stays readable through the stage.

    Order is the property, which is why it is asserted over the generated launcher rather
    than over a call count: retiring before the window is bound would leave the child with
    no window, and retiring before the re-mask would leave the leaf live. A stage whose
    mask root never materialized is retired by the same pass, so the sweep's own guarantee
    is that NO stage outlives the mask loop.
    """

    @staticmethod
    def _script(apps: Path) -> str:
        return sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(str(apps),),
            extra_private_dirs=(str(apps / "alpha" / "data"),),
        )

    def test_every_stage_is_retired_after_the_windows_and_their_nested_masks(self, tmp_path):
        script = self._script(tmp_path / "apps")
        # The CALL, not the helper definition, which necessarily precedes the whole loop.
        call = script.index("_retire_stage_or_die(_private_stage.pop(_staged)")

        assert script.index("opening private window") < call
        assert script.index("re-hiding nested masked directory") < call
        assert "for _staged in list(_private_stage):" in script

    def test_the_sweep_runs_outside_the_loop_that_may_skip_a_mask_root(self, tmp_path):
        """A stage whose mask root never materialized is still a second path to the tree."""
        script = self._script(tmp_path / "apps")
        loop = script.index("for d in SENSITIVE_DIRS:")
        sweep = script.index("for _staged in list(_private_stage):")
        readonly = script.index("for d in READONLY_DIRS:")

        # Between the end of the mask loop and the next loop: reached whatever that loop
        # decided about any one mask root.
        assert loop < sweep < readonly

    def test_retiring_detaches_and_refuses_rather_than_warning(self, tmp_path):
        script = self._script(tmp_path / "apps")
        body = script.split("def _retire_stage_or_die(")[1].split("\ndef ")[0]

        assert "_MNT_DETACH" in body
        # A warning here would leave the exposure in place with the spawn proceeding.
        assert "sys.exit(" in body
        assert "sandbox: WARNING" not in body


@_POSIX_ONLY
class TestTheMaskBindsOntoTheApprovedDirectoryNotItsName:
    """Comparing the inode and then mounting on the NAME leaves the race open.

    The rename the comparison exists to catch can land between the two, and the mask then
    covers whatever answers to the name while the real tree stays readable where it moved.
    So the descriptor that was compared is the mount target. A caller that vouched for no
    identity keeps the plain name, which is what leaves the other three producers alone.
    """

    @staticmethod
    def _script(apps: Path, ids: tuple) -> str:
        return sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=(str(apps),),
            extra_hidden_dir_ids=ids,
        )

    def test_the_bind_target_becomes_the_compared_descriptor(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()

        script = self._script(apps, (sandbox.masked_dir_identity(str(apps)),))
        region = script.split("_want_dir_id = SENSITIVE_DIR_IDS.get(d)")[1]
        region = region.split("hiding credential directory")[0]

        assert 'target = ("/proc/self/fd/%d" % _mask_fd).encode()' in region
        # The descriptor must still be open when the mount runs: closing it first is the
        # regression that turns the comparison back into a name check.
        assert "os.close(_mask_fd)" not in region.split("target = (")[1]

    def test_a_caller_that_vouches_for_nothing_still_binds_on_the_name(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()

        script = self._script(apps, ())

        assert "if _want_dir_id is not None:" in script
        assert "_mask_fd = -1" in script


class TestThePrivateWindowGate:
    """``_private_window_spellings`` admits a window only where one is safe.

    Both directions are pinned: a window that re-exposes a hidden target is refused, and
    an ordinary window inside the same masked tree is still admitted. Without the second
    assertion a gate that refused everything would pass the first.

    Paths are built from ``tmp_path`` rather than written as literals, because the rule is
    lexical over ``os.sep`` and a POSIX literal makes every case vacuous on Windows.
    """

    @staticmethod
    def _paths(tmp_path: Path) -> tuple[str, str, str, str]:
        apps = str(tmp_path / "apps")
        return (
            apps,
            os.path.join(apps, "aws-control", "data"),
            os.path.join(apps, "meetings", "data"),
            os.path.join(apps, "alpha", "data"),
        )

    def test_a_window_equal_to_a_hidden_target_is_refused(self, tmp_path):
        apps, leaf, _contains, _ordinary = self._paths(tmp_path)
        assert sandbox._private_window_spellings((leaf,), [apps, leaf]) == []

    def test_a_window_containing_a_hidden_target_is_refused(self, tmp_path):
        apps, _leaf, contains, _ordinary = self._paths(tmp_path)
        nested = os.path.join(contains, "edits")
        assert sandbox._private_window_spellings((contains,), [apps, nested]) == []

    def test_an_ordinary_window_inside_the_masked_tree_is_admitted(self, tmp_path):
        apps, leaf, _contains, ordinary = self._paths(tmp_path)
        assert sandbox._private_window_spellings((ordinary,), [apps, leaf]) == [ordinary]

    def test_a_window_outside_every_masked_tree_is_dropped(self, tmp_path):
        apps, _leaf, _contains, _ordinary = self._paths(tmp_path)
        outside = str(tmp_path / "elsewhere")
        assert sandbox._private_window_spellings((outside,), [apps]) == []

    def test_a_trailing_separator_does_not_defeat_the_refusal(self, tmp_path):
        """The spellings differ by a separator the comparison has to normalise."""
        apps, leaf, _contains, _ordinary = self._paths(tmp_path)
        assert sandbox._private_window_spellings((leaf,), [apps, leaf + os.sep]) == []


class TestTheCallerMaskMaterializer:
    @_POSIX_ONLY
    def test_it_creates_an_absent_target_owner_only_and_reports_it(self, tmp_path):
        target = tmp_path / "apps"
        assert materialize_caller_masked_dir(str(target)) is True
        assert target.is_dir()
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_an_existing_directory_is_left_alone_and_not_reported(self, tmp_path):
        target = tmp_path / "apps"
        target.mkdir()
        marker = target / "installed"
        marker.mkdir()
        assert materialize_caller_masked_dir(str(target)) is False
        assert marker.is_dir()

    def test_a_plain_file_at_the_path_refuses(self, tmp_path):
        target = tmp_path / "apps"
        target.write_text("not a directory", encoding="utf-8")
        with pytest.raises(SandboxCeilingUnsealable):
            materialize_caller_masked_dir(str(target))

    def test_an_absent_parent_refuses_rather_than_building_the_chain(self, tmp_path):
        """No ``parents=True``: an intermediate directory created here would be an
        agent-writable ancestor a rename could swap out from under the mount."""
        target = tmp_path / "never" / "created" / "apps"
        with pytest.raises(SandboxCeilingUnsealable):
            materialize_caller_masked_dir(str(target))
        assert not (tmp_path / "never").exists()

    @_POSIX_ONLY
    def test_a_dangling_link_squatting_the_name_refuses(self, tmp_path):
        target = tmp_path / "apps"
        target.symlink_to(tmp_path / "gone")
        with pytest.raises(SandboxCeilingUnsealable):
            materialize_caller_masked_dir(str(target))


class TestTheWindowIsCreatedThroughADescriptorNotAPath:
    """A link at the app entry must not redirect the create, and nothing may land past it.

    The caller tests the entry's shape during admission and creates the window later, and
    the interval between them is every other admitted app's stat work rather than a few
    instructions. Creating by path resolves the name again at create time, so a link planted
    in that interval sends the ``mkdir`` to a directory the planter chose -- and the window
    then travels on as a read-write carve-out onto it.

    The link cases are POSIX-only: creating a symlink needs a privilege on Windows, and that
    platform takes the path-based fallback anyway. The platform without the descriptor
    primitives has its own case below, because a simulated platform cannot show what a real
    host does on the excluded branch.
    """

    @_POSIX_ONLY
    def test_a_linked_app_entry_refuses_and_creates_nothing_past_the_link(self, tmp_path):
        apps = tmp_path / "apps"
        apps.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, apps / "alpha")

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            sandbox.materialize_app_data_window(str(apps), "alpha")

        assert "no-follow" in str(exc.value)
        assert not (elsewhere / "data").exists(), "the create landed past the link"

    def test_an_ordinary_entry_creates_the_window_and_reports_its_own_identity(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)

        created, dev, ino = sandbox.materialize_app_data_window(str(apps), "alpha")

        real = os.lstat(apps / "alpha" / "data")
        assert created is True
        assert (dev, ino) == (real.st_dev, real.st_ino)

    def test_an_existing_window_is_reported_as_not_created(self, tmp_path):
        apps = tmp_path / "apps"
        window = apps / "alpha" / "data"
        window.mkdir(parents=True)
        real = os.lstat(window)

        created, dev, ino = sandbox.materialize_app_data_window(str(apps), "alpha")

        assert created is False
        assert (dev, ino) == (real.st_dev, real.st_ino)

    def test_a_file_at_the_window_name_refuses(self, tmp_path):
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)
        (apps / "alpha" / "data").write_text("not a directory\n")

        with pytest.raises(SandboxCeilingUnsealable) as exc:
            sandbox.materialize_app_data_window(str(apps), "alpha")

        assert "not a directory" in str(exc.value)

    def test_the_producer_creates_through_the_descriptor_and_does_not_re_lookup(self):
        """Structural: the window path must not create or identify by path any more."""
        source = inspect.getsource(sandbox.app_data_window_targets)

        assert "materialize_app_data_window(apps_tree, name)" in source
        assert "materialize_caller_masked_dir(" not in source
        assert "os.lstat(window)" not in source

    def test_the_route_is_chosen_by_capability_not_by_platform_name(self):
        """Each primitive the descriptor route needs is tested for by name."""
        source = inspect.getsource(sandbox)
        decl = source[source.index("_WINDOW_DESCRIPTOR_CREATE = (") :][:400]

        assert 'hasattr(os, "O_DIRECTORY")' in decl
        assert "os.mkdir in os.supports_dir_fd" in decl
        assert "os.stat in os.supports_dir_fd" in decl
        assert "os.name" not in decl and "sys.platform" not in decl

    @pytest.mark.skipif(
        sandbox._WINDOW_DESCRIPTOR_CREATE,
        reason="this host HAS the descriptor primitives; the fallback branch is the subject",
    )
    def test_a_host_without_the_descriptor_primitives_still_opens_the_window(self, tmp_path):
        """The excluded platform's own case, with no simulation.

        A simulated capability cannot show that a real host lacking ``O_DIRECTORY`` or
        ``dir_fd`` reaches a working fallback rather than an ``AttributeError``.
        """
        apps = tmp_path / "apps"
        (apps / "alpha").mkdir(parents=True)

        created, dev, ino = sandbox.materialize_app_data_window(str(apps), "alpha")

        real = os.lstat(apps / "alpha" / "data")
        assert created is True
        assert (dev, ino) == (real.st_dev, real.st_ino)


class TestWhatMaskingThisTreeStillCosts:
    @_POSIX_ONLY
    def test_a_write_outside_any_data_dir_is_discarded_rather_than_refused(self):
        """The residual, narrowed to the part of the tree that is not persistence.

        The Linux mask is a plain bind of an empty directory -- ``SENSITIVE_DIRS``, not
        ``READONLY_DIRS`` -- so a write under the tree inside the child SUCCEEDS and
        vanishes when the child exits. Each app's ``data`` root is carved back out as a
        window, so what remains is a write to an app's BUNDLE: its manifest, its code,
        its ``.app_secret``. None of those is a place a cron is meant to write, and the
        credential among them is the whole reason the tree is masked.

        Making the rest fail loudly instead needs a read-only mask primitive threaded
        through all three backends, which is a change to the sandbox's own vocabulary
        rather than to this call site.
        """
        apps = os.path.join(os.path.expanduser("~"), ".kirocrew", "apps")
        data = os.path.join(apps, "alpha", "data")
        script = sandbox._build_launcher_script(
            "cc", extra_hidden_dirs=(apps,), extra_private_dirs=(data,)
        )
        masked = json.loads(re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S).group(1))
        readonly = json.loads(re.search(r"READONLY_DIRS = (\[.*?\])\n", script, re.S).group(1))
        windows = json.loads(re.search(r"PRIVATE_DIRS = (\[.*?\])\n", script, re.S).group(1))

        assert apps in masked
        assert apps not in readonly
        # The bundle keeps the discard-on-exit behaviour; the data root does not.
        assert data in windows


@_POSIX_ONLY
class TestTheEnumerationBoundsWhatItRetains:
    """Count AND per-name length, bounded where the window is retained.

    The apps tree is agent-writable, so both how many entries it holds and how long each
    name is are externally controlled, and one window record is retained per accepted
    name. Reading the whole tree into a list before any filter could refuse a name made
    the allocation the tree's to choose, once per scheduled spawn, on the gateway.

    Break-arm: ``unbound_enumeration``.
    """

    def test_reaching_the_bound_withholds_every_window(self, tmp_path):
        """A dropped name is a credential left unscanned, and the tree picks which drop."""
        apps = tmp_path / ".kirocrew" / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS + 5):
            (apps / ("app-%03d" % i) / "data").mkdir(parents=True)

        assert _window_paths(apps) == ()

    def test_the_overflow_is_counted_and_said_once(self, tmp_path, caplog):
        apps = tmp_path / ".kirocrew" / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS + 3):
            (apps / ("app-%03d" % i) / "data").mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            sandbox.app_data_window_targets(str(apps))

        said = [r.getMessage() for r in caplog.records if "data window" in r.getMessage()]
        assert len(said) == 1, said
        assert "3 went unscanned" in said[0]

    def test_a_tree_right_at_the_bound_still_earns_its_windows(self, tmp_path):
        """The cap is a ceiling, not a trigger: exactly at it nothing went unscanned."""
        apps = tmp_path / ".kirocrew" / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS):
            (apps / ("app-%03d" % i) / "data").mkdir(parents=True)

        assert len(_window_paths(apps)) == sandbox.MAX_APP_DATA_WINDOWS

    def test_a_name_longer_than_the_contract_allows_is_refused(self, tmp_path):
        from kiro_crew.apps.plugin_import import MAX_APP_NAME_CHARS

        apps = tmp_path / ".kirocrew" / "apps"
        (apps / ("a" * (MAX_APP_NAME_CHARS + 1)) / "data").mkdir(parents=True)
        (apps / "alpha" / "data").mkdir(parents=True)

        assert _window_paths(apps) == (str(apps / "alpha" / "data"),)

    def test_a_name_at_the_contracts_own_limit_still_earns_one(self, tmp_path):
        from kiro_crew.apps.plugin_import import MAX_APP_NAME_CHARS

        apps = tmp_path / ".kirocrew" / "apps"
        longest = "a" * MAX_APP_NAME_CHARS
        (apps / longest / "data").mkdir(parents=True)

        assert _window_paths(apps) == (str(apps / longest / "data"),)

    def test_the_bound_is_one_named_constant_not_a_literal(self):
        """Two literals bounding one population is the defect, whatever the numbers."""
        source = inspect.getsource(sandbox.app_data_window_targets)

        assert "MAX_APP_DATA_WINDOWS" in source
        assert "MAX_APP_NAME_CHARS" in source
        assert not re.search(r">=\s*\d+", source), source

    def test_a_refused_name_never_spends_the_count(self, tmp_path):
        """The shape answer gates the count, or the tree's junk pushes real apps out."""
        apps = tmp_path / ".kirocrew" / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS):
            (apps / (".%03d-data-tmp" % i)).mkdir(parents=True)
        (apps / "alpha" / "data").mkdir(parents=True)

        assert _window_paths(apps) == (str(apps / "alpha" / "data"),)


@_POSIX_ONLY
class TestACreatedWindowIsGivenBackIfItForgedTheStalenessProof:
    """The parked-copy test reads two names; the create happens after it.

    A lifecycle move landing in that gap parks a ``data`` this enumeration already saw
    present, and the create then puts an empty one back -- forging from the other
    direction the very proof the test refuses. ``apps/manager.py`` reads "the parked copy
    AND ``data`` both exist" as proof the parked one is stale and removes it, so the
    forgery costs the app's only surviving copy and has no recovery path.

    Break-arm: ``keep_the_forged_directory``.
    """

    def test_a_parked_copy_appearing_during_the_create_withholds_the_window(self, tmp_path):
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "alpha").mkdir(parents=True)
        parked = apps / ".alpha-data-tmp"
        real = sandbox.materialize_app_data_window

        def create_then_park(tree: str, name: str):
            made, dev, ino = real(tree, name)
            if made and name == "alpha":
                parked.mkdir()
            return made, dev, ino

        with patch.object(sandbox, "materialize_app_data_window", create_then_park):
            windows = _window_paths(apps)

        assert windows == ()

    def test_the_directory_it_created_is_removed_again(self, tmp_path):
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "alpha").mkdir(parents=True)
        parked = apps / ".alpha-data-tmp"
        real = sandbox.materialize_app_data_window

        def create_then_park(tree: str, name: str):
            made, dev, ino = real(tree, name)
            if made and name == "alpha":
                parked.mkdir()
            return made, dev, ino

        with patch.object(sandbox, "materialize_app_data_window", create_then_park):
            sandbox.app_data_window_targets(str(apps))

        assert not (apps / "alpha" / "data").exists(), "the forged proof was left standing"
        assert parked.is_dir(), "the surviving copy must not be touched"

    def test_a_racing_writers_bytes_are_never_removed(self, tmp_path):
        """``rmdir`` refuses a non-empty directory, and a refusal still withholds."""
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "alpha").mkdir(parents=True)
        parked = apps / ".alpha-data-tmp"
        real = sandbox.materialize_app_data_window

        def create_then_park_and_write(tree: str, name: str):
            made, dev, ino = real(tree, name)
            if made and name == "alpha":
                parked.mkdir()
                (Path(tree) / name / "data" / "kept.json").write_text("{}", encoding="utf-8")
            return made, dev, ino

        with patch.object(sandbox, "materialize_app_data_window", create_then_park_and_write):
            windows = _window_paths(apps)

        assert windows == ()
        assert (apps / "alpha" / "data" / "kept.json").is_file()

    def test_a_window_this_spawn_did_not_create_is_left_alone(self, tmp_path):
        """Only a directory THIS spawn made can be given back."""
        apps = tmp_path / ".kirocrew" / "apps"
        data = apps / "alpha" / "data"
        data.mkdir(parents=True)
        (data / "state.json").write_text("{}", encoding="utf-8")

        windows = _window_paths(apps)

        assert windows == (str(data),)
        assert (data / "state.json").is_file()


@_POSIX_ONLY
class TestAnAliasedAppSecretWithholdsEveryWindow:
    """A window re-exposes ``data`` read-write on its real inode inside the mask.

    A HARDLINK from ``.app_secret`` into any app's data tree therefore survives: the mask
    covers a path, the link is a second path to the same inode, and the child reads the
    bearer credential through it. Every alias check in the enumeration is symlink-shaped,
    so none of them sees this.

    ``st_nlink`` answers it in one ``lstat`` per app rather than walking each data tree,
    and a positive answer withholds EVERY window because a link count proves a second
    name exists without saying where -- it may sit in any app's data directory regardless
    of whose secret it names.

    Break-arm: ``drop_alias_check``.
    """

    def test_a_hardlinked_secret_withholds_the_windows(self, tmp_path):
        apps = tmp_path / ".kirocrew" / "apps"
        data = apps / "alpha" / "data"
        data.mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(data / "notes.txt"))

        assert _window_paths(apps) == ()

    def test_an_alias_in_one_app_withholds_anothers_window_too(self, tmp_path):
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "beta" / "data").mkdir(parents=True)
        (apps / "alpha" / "data").mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(apps / "beta" / "data" / "copy"))

        assert _window_paths(apps) == ()

    def test_an_unaliased_secret_keeps_every_window(self, tmp_path):
        apps = tmp_path / ".kirocrew" / "apps"
        data = apps / "alpha" / "data"
        data.mkdir(parents=True)
        (apps / "alpha" / ".app_secret").write_text("bearer", encoding="utf-8")

        assert _window_paths(apps) == (str(data),)

    def test_an_app_with_no_secret_keeps_its_window(self, tmp_path):
        apps = tmp_path / ".kirocrew" / "apps"
        data = apps / "alpha" / "data"
        data.mkdir(parents=True)

        assert _window_paths(apps) == (str(data),)

    def test_the_withholding_is_said_out_loud(self, tmp_path, caplog):
        apps = tmp_path / ".kirocrew" / "apps"
        data = apps / "alpha" / "data"
        data.mkdir(parents=True)
        secret = apps / "alpha" / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(data / "notes.txt"))

        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            sandbox.app_data_window_targets(str(apps))

        said = [r.getMessage() for r in caplog.records]
        assert any("reachable under 2 names" in m for m in said), said

    def test_a_symlinked_secret_withholds_every_window_too(self, tmp_path):
        """A link is a second name whose target can sit inside a window."""
        apps = tmp_path / ".kirocrew" / "apps"
        data = apps / "alpha" / "data"
        data.mkdir(parents=True)
        target = data / "kept-here"
        target.write_text("bearer", encoding="utf-8")
        os.symlink(str(target), str(apps / "alpha" / ".app_secret"))

        assert _window_paths(apps) == ()

    def test_the_link_withholding_is_said_out_loud(self, tmp_path, caplog):
        apps = tmp_path / ".kirocrew" / "apps"
        (apps / "alpha" / "data").mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_text("bearer", encoding="utf-8")
        os.symlink(str(elsewhere), str(apps / "alpha" / ".app_secret"))

        with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
            sandbox.app_data_window_targets(str(apps))

        said = [r.getMessage() for r in caplog.records]
        assert any("is a link" in m for m in said), said

    def test_an_alias_past_the_scan_cap_is_not_a_hole(self, tmp_path):
        """The cap drops names before the scan, so an unscanned secret must not be
        traded for another app's window."""
        apps = tmp_path / ".kirocrew" / "apps"
        for i in range(sandbox.MAX_APP_DATA_WINDOWS):
            (apps / ("app-%03d" % i) / "data").mkdir(parents=True)
        dropped = apps / "zz-dropped"
        (dropped / "data").mkdir(parents=True)
        secret = dropped / ".app_secret"
        secret.write_text("bearer", encoding="utf-8")
        os.link(str(secret), str(apps / "app-000" / "data" / "alias"))

        assert _window_paths(apps) == ()
