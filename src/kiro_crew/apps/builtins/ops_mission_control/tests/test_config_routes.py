"""Tests for the config-write surface.

The load-bearing test here is ``test_secret_field_is_refused``. The app's
``data/config.json`` is served over ``/api/apps/<name>/config`` WITHOUT session
auth, so a settings form that posted a token to the config route would put a live
PagerDuty credential behind nothing but the gateway port. The route refuses it;
this pins that refusal.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import routes


def _request(body=None, match_info=None):
    """A MagicMock request whose ``.json()`` resolves to ``body``."""
    request = mock.MagicMock(spec=web.Request)
    request.match_info = match_info or {}

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    request.json = _json
    return request


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


class _HomeIsolationMixin:
    """Redirects the data home so config writes land in a temp dir."""

    def _enter_isolation(self) -> None:
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        self._clear_caches()
        registry.reset_registry()

    def _exit_isolation(self) -> None:
        import os

        from kiro_crew.apps.builtins.ops_mission_control.backend import registry

        registry.reset_registry()
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        self._clear_caches()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _clear_caches():
        from kiro_crew.config import loader

        for name in ("config_dir", "_config_dir"):
            fn = getattr(loader, name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()


class _HomeIsolatedAsync(_HomeIsolationMixin, unittest.IsolatedAsyncioTestCase):
    """Async base for tests that await a route handler directly.

    ``IsolatedAsyncioTestCase`` rather than a bare ``asyncio.run`` per test: the
    subprocess-spawn audit (``test/test_spawn_audit.py``) scans for
    ``asyncio.<spawn attr>`` calls across the package and ``asyncio.run`` trips it,
    so the convention here matches the other builtins' async tests.
    """

    def setUp(self):
        self._enter_isolation()

    def tearDown(self):
        self._exit_isolation()


class TestProviderConfigRoute(_HomeIsolatedAsync):
    async def test_secret_field_is_refused(self):
        """A token must never be writable into the unauthenticated config file."""
        response = await routes._handle_put_provider_config(
            _request({"api_token": "u+SuperSecret"}, {"provider_id": "pagerduty"})
        )
        self.assertEqual(response.status, 400)
        body = _payload(response)
        self.assertIn("secret field", body["error"])
        # And nothing was written.
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            provider_config,
        )

        self.assertEqual(provider_config("pagerduty"), {})

    async def test_unknown_field_is_refused(self):
        """The config file must not become a place to stash arbitrary data."""
        response = await routes._handle_put_provider_config(
            _request({"totally_made_up": "x"}, {"provider_id": "cloudwatch"})
        )
        self.assertEqual(response.status, 400)
        self.assertIn("no config field", _payload(response)["error"])

    async def test_unknown_provider_is_404(self):
        response = await routes._handle_put_provider_config(
            _request({"enabled": True}, {"provider_id": "nope"})
        )
        self.assertEqual(response.status, 404)

    async def test_declared_field_is_saved(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            provider_config,
        )

        response = await routes._handle_put_provider_config(
            _request({"enabled": True, "region": "eu-west-1"}, {"provider_id": "cloudwatch"})
        )
        self.assertEqual(response.status, 200)
        saved = provider_config("cloudwatch")
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["region"], "eu-west-1")

    async def test_merge_preserves_untouched_fields(self):
        """A form that submits one field must not wipe the rest."""
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            provider_config,
        )

        await routes._handle_put_provider_config(
            _request({"enabled": True, "region": "us-east-1"}, {"provider_id": "cloudwatch"})
        )
        await routes._handle_put_provider_config(
            _request({"region": "ap-south-1"}, {"provider_id": "cloudwatch"})
        )
        saved = provider_config("cloudwatch")
        self.assertEqual(saved["region"], "ap-south-1")
        self.assertTrue(saved["enabled"])  # survived the second write

    async def test_empty_body_is_refused(self):
        response = await routes._handle_put_provider_config(
            _request({}, {"provider_id": "cloudwatch"})
        )
        self.assertEqual(response.status, 400)

    async def test_non_json_body_is_400(self):
        response = await routes._handle_put_provider_config(
            _request(None, {"provider_id": "cloudwatch"})
        )
        self.assertEqual(response.status, 400)


class TestSettingsRoute(_HomeIsolatedAsync):
    async def test_valid_mode_is_applied(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        response = await routes._handle_put_settings(_request({"mode": "propose"}))
        self.assertEqual(response.status, 200)
        self.assertEqual(rotation.app_mode(), "propose")

    async def test_unknown_mode_is_refused_not_silently_defaulted(self):
        """A typo must not quietly change what the agent is allowed to do."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        response = await routes._handle_put_settings(_request({"mode": "yolo"}))
        self.assertEqual(response.status, 400)
        # Still the safe default.
        self.assertEqual(rotation.app_mode(), "observe")

    async def test_primary_flag_round_trips(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        await routes._handle_put_settings(_request({"primary_instance": False}))
        self.assertFalse(rotation.is_primary())

    async def test_non_positive_tuning_is_refused(self):
        response = await routes._handle_put_settings(_request({"max_claims_per_cycle": 0}))
        self.assertEqual(response.status, 400)

    async def test_non_integer_tuning_is_refused(self):
        response = await routes._handle_put_settings(_request({"stale_after_secs": "soon"}))
        self.assertEqual(response.status, 400)

    async def test_unrecognized_keys_are_refused(self):
        response = await routes._handle_put_settings(_request({"nonsense": 1}))
        self.assertEqual(response.status, 400)

    async def test_act_rules_are_settable_over_the_api(self):
        """The `act` tier had no authoring path at all — `set_rules` had zero callers.

        Settings advertised grants from "patterns you have explicitly allowlisted with a
        rule" and offered nothing to click, while the manual said to edit `data/config.json`
        (which the keystone migration ignores once the policy file exists). Every act-mode
        adopter silently got Propose. Found in review.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        rule = {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}
        response = await routes._handle_put_settings(_request({"autonomy_rules": [rule]}))
        self.assertEqual(response.status, 200)
        # Live in the actual gate, not merely stored.
        self.assertEqual([rotation.rule_to_dict(r) for r in rotation.load_rules()], [rule])

    async def test_a_blanket_act_rule_is_refused_with_400(self):
        """Stored-and-silently-dropped would show a grant that never matches."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        response = await routes._handle_put_settings(
            _request({"autonomy_rules": [{"source": "cloudwatch", "mode": "act"}]})
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(rotation.load_rules(), [])

    async def test_a_rejected_rule_leaves_the_previous_grants_intact(self):
        """All-or-nothing: a bad edit must not partially revoke working authority."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        good = {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}
        await routes._handle_put_settings(_request({"autonomy_rules": [good]}))
        response = await routes._handle_put_settings(
            _request({"autonomy_rules": [good, {"source": "datadog", "mode": "act"}]})
        )
        self.assertEqual(response.status, 400)
        self.assertEqual([rotation.rule_to_dict(r) for r in rotation.load_rules()], [good])

    async def test_the_team_memory_repo_is_settable_over_the_api(self):
        """The shared-ledger remote must be reachable from Settings.

        ``ledger_sync.set_settings`` shipped with NO caller outside the tests, so the
        app's headline team feature — one repo the whole team syncs its knowledge
        through — could only be configured by hand-editing ``data/config.json``. An
        operator looking for where to point it found nothing.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        response = await routes._handle_put_settings(
            _request(
                {
                    "ledger_sync_remote": "git@github.com:acme/ops-ledger.git",
                    "ledger_sync_branch": "main",
                    "ledger_sync_enabled": True,
                }
            )
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(ledger_sync.remote(), "git@github.com:acme/ops-ledger.git")
        self.assertEqual(ledger_sync.branch(), "main")
        self.assertTrue(ledger_sync.configured())

    async def test_an_option_like_branch_is_refused(self):
        """A branch name reaches a ``git`` argv, so refuse it looking like a flag."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        for bad in ("--upload-pack=evil", "main evil", "-x"):
            with self.subTest(branch=bad):
                response = await routes._handle_put_settings(_request({"ledger_sync_branch": bad}))
                self.assertEqual(response.status, 400)
                self.assertNotEqual(ledger_sync.branch(), bad)

    async def test_an_overlong_remote_is_refused(self):
        response = await routes._handle_put_settings(_request({"ledger_sync_remote": "x" * 600}))
        self.assertEqual(response.status, 400)

    async def test_sync_status_is_reported_for_the_board(self):
        """``ledger_sync.status()`` says it is "surfaced in Settings" — so surface it.

        It was written for the UI and then returned by no route, leaving the team repo
        invisible as well as unsettable. Asserted through the same helper ``/state``
        calls, so the status cannot go missing from the payload without failing here.
        """
        await routes._handle_put_settings(
            _request({"ledger_sync_remote": "git@github.com:acme/ops-ledger.git"})
        )
        status = routes._ledger_sync_status()
        self.assertEqual(status["remote"], "git@github.com:acme/ops-ledger.git")
        self.assertIn("detail", status)

    async def test_sync_status_never_breaks_the_board(self):
        """``/state`` paints everything, so an optional feature must not be able to 500 it."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        with mock.patch.object(ledger_sync, "status", side_effect=RuntimeError("git exploded")):
            status = routes._ledger_sync_status()
        self.assertFalse(status["enabled"])
        self.assertIn("detail", status)

    async def test_the_failure_fallback_has_the_same_shape_as_a_real_status(self):
        """One shape, so the UI can read every field instead of guarding each one.

        The fallback used to carry two keys out of six. A panel reading it therefore had to
        guard each field individually, and forgetting one renders ``undefined`` as the team's
        remote — which reads as a repo called "undefined" rather than as "we could not tell".
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        real = routes._ledger_sync_status()
        with mock.patch.object(ledger_sync, "status", side_effect=RuntimeError("git exploded")):
            fallback = routes._ledger_sync_status()
        self.assertEqual(set(real), set(fallback))

    async def test_a_conflicted_schedule_is_reported_rather_than_only_logged(self):
        """A refused push must be visible, not just audited.

        ``push`` REFUSES while ``rotation.yaml`` holds conflict markers, and said so only to
        the log and a SEL line — ``sync_safely`` swallows the refusal. Meanwhile ``status()``
        reported "Syncing …", so an operator watched a card claim sync worked through an
        indefinite publishing outage: no lesson reached the team, and the file that decides
        who picks up work stayed unparseable for everyone who pulled it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, ledger_sync

        await routes._handle_put_settings(
            _request(
                {
                    "ledger_sync_remote": "git@github.com:acme/ops-ledger.git",
                    "ledger_sync_enabled": True,
                }
            )
        )
        clean = routes._ledger_sync_status()
        self.assertFalse(clean["schedule_conflict"])

        schedule = ledger.ledger_path().parent / ledger_sync._SCHEDULE_FILENAME
        schedule.parent.mkdir(parents=True, exist_ok=True)
        schedule.write_text(
            "shifts:\n<<<<<<< HEAD\n  - who: alice\n=======\n  - who: bob\n>>>>>>> origin/main\n",
            encoding="utf-8",
        )

        conflicted = routes._ledger_sync_status()
        self.assertTrue(conflicted["schedule_conflict"])
        self.assertIn("refused", conflicted["detail"])
        self.assertNotIn("Syncing", conflicted["detail"])

    async def test_a_conflicted_ledger_is_reported_separately_from_the_schedule(self):
        """The two conflicts are not the same severity and must not be conflated.

        A ledger conflict is reconcilable — ids are content-addressed, ``read_entries`` skips
        the markers, and the next push rewrites the union — so sync keeps publishing. Reading
        it as fatal would send an operator hand-editing a file the app already handles;
        reading a SCHEDULE conflict as reconcilable would hide a total publishing stop.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        await routes._handle_put_settings(
            _request(
                {
                    "ledger_sync_remote": "git@github.com:acme/ops-ledger.git",
                    "ledger_sync_enabled": True,
                }
            )
        )
        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '<<<<<<< HEAD\n{"entry_id": "a"}\n=======\n{"entry_id": "b"}\n>>>>>>> origin/main\n',
            encoding="utf-8",
        )

        status = routes._ledger_sync_status()
        self.assertTrue(status["conflict"])
        self.assertFalse(status["schedule_conflict"])
        self.assertNotIn("refused", status["detail"])


class TestManifestCrons(unittest.TestCase):
    """The app is inert unless the manifest declares its crons."""

    def _manifest(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store
        from kiro_crew.apps.manifest import AppManifest

        path = Path(__file__).resolve().parents[1] / "app.json"
        assert path.is_file(), path
        del store  # only imported to assert the package layout is intact
        return AppManifest.from_json_file(path)

    def test_manifest_is_valid(self):
        self.assertEqual(self._manifest().validate(), [])

    def test_all_four_sops_have_a_cron(self):
        names = {c.name for c in self._manifest().crons}
        self.assertEqual(names, {"dispatch", "reconcile", "rotation-check", "ledger-hygiene"})

    def test_secrets_live_outside_the_app_dir_so_uninstall_cannot_reach_them(self):
        """Pins the credential-retention boundary, which is easy to get wrong twice.

        The keystone secret file MUST sit at the crew-home root: that is what puts it
        on ``security._CREW_SECRET_LEAVES`` so the agent's own tools cannot read or
        overwrite it. The consequence is that ``uninstall_app``, which removes
        ``apps/<name>/``, cannot delete it — a PagerDuty/Datadog token outlives an
        uninstall.

        That is the right trade (moving it under the app dir would hand the agent its
        own credentials, and silently wiping tokens would break uninstall/reinstall),
        but it must be DISCLOSED rather than discovered. Settings says so next to the
        Revoke button, which is the only control that changes it.

        If a future change moves the file under the app dir, this test fails — and the
        keystone protection would have been quietly lost.
        """
        import os
        import tempfile

        from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import secrets_path

        prev = os.environ.get("KIROCREW_HOME")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["KIROCREW_HOME"] = tmp
            try:
                # Both sides resolved: on Windows the temp dir arrives as an 8.3 short name
                # (`RUNNER~1`) while `secrets_path()` yields the long form (`runneradmin`), so
                # comparing a resolved path against an unresolved one raises in `relative_to`.
                home = Path(tmp).resolve()
                path = secrets_path().resolve()
                self.assertEqual(
                    path.parent,
                    home,
                    "secrets must sit at the crew-home root for the keystone floor",
                )
                self.assertNotIn(
                    "apps",
                    path.relative_to(home).parts,
                    "secrets must NOT live under apps/ — uninstall would delete them "
                    "and the sensitive-path floor would no longer cover them",
                )
            finally:
                if prev is None:
                    os.environ.pop("KIROCREW_HOME", None)
                else:
                    os.environ["KIROCREW_HOME"] = prev

    def test_settings_discloses_that_uninstall_keeps_credentials(self):
        """The retention boundary is invisible unless the UI says it.

        A user who uninstalls to "remove the app and its data" would otherwise leave a
        live third-party token on disk with nothing having told them.
        """
        # parents[6] is the repo root (…/tests/ops_mission_control/builtins/apps/
        # kiro_crew/src/<root>). Resolved by counting rather than guessed — an
        # off-by-one here makes this test skip, which reads as passing.
        panel = (
            Path(__file__).resolve().parents[6]
            / "website/src/apps/ops-mission-control/SettingsPanel.tsx"
        )
        if not panel.is_file():  # pragma: no cover - python-only checkout
            self.skipTest("website/ not present in this checkout")
        # Search the panel AND the i18n catalog. The disclosure is user-visible prose, so
        # i18n extraction correctly moved it out of the .tsx and into `en.json` — asserting
        # only against the source made a translated string look like a deleted one. What
        # this test cares about is that the sentence SHIPS somewhere the UI renders, not
        # which file holds it today.
        catalog = panel.parent.parent.parent / "i18n/locales/en.json"
        haystacks = [panel.read_text(encoding="utf-8")]
        if catalog.is_file():
            haystacks.append(catalog.read_text(encoding="utf-8"))
        self.assertTrue(
            any("uninstalling this app does not delete" in h for h in haystacks),
            "the retention disclosure must ship in the panel or the i18n catalog",
        )

    def test_only_tier_armed_crons_ship_paused(self):
        """A cron may ship paused ONLY if some tier will actually resume it.

        This replaces a broader "everything except rotation-check ships paused" rule.
        That rule's stated concern was "they must not fire before a provider is
        configured" — but shipping paused is the wrong mechanism for it, and the step-0
        cheap exit is the right one (which is why rotation-check was already exempted on
        exactly that basis). Enforced as "paused" it silently killed two more crons:
        ``ledger-hygiene`` and ``reconcile`` sit on tiers the rotation-check SOP is
        forbidden to touch, so nothing ever resumed them.

        Only ``on_shift`` crons may ship paused, because rotation-check arms that tier.
        The cheap-exit guarantee is asserted separately for every enabled cron.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        on_shift = {name.split("/", 1)[1] for name in rotation.TIER_CRONS[rotation.TIER_ON_SHIFT]}
        for cron in self._manifest().crons:
            if cron.enabled:
                continue
            with self.subTest(cron=cron.name):
                self.assertIn(
                    cron.name,
                    on_shift,
                    f"{cron.name!r} ships paused but no tier resumes it — it would never run",
                )

    def test_rotation_check_ships_enabled_or_nothing_ever_arms(self):
        """The cold-start deadlock this fixes, pinned.

        `dispatch` is armed by the `on_shift` tier, and the only thing that arms that
        tier is the rotation-check cron. Ship rotation-check paused too and NOTHING
        resumes it — no code path flips a manifest `enabled: false` — so a user
        enables the app, configures CloudWatch, and the app never fires. The store
        listing promises "the on-shift tier arms and disarms itself", which was
        impossible.

        It is safe to ship enabled because its SOP's step 0 exits with no output when
        no provider is configured, so a fresh install pays nothing for it.
        """
        rotation_check = [c for c in self._manifest().crons if c.name == "rotation-check"]
        self.assertEqual(len(rotation_check), 1)
        self.assertTrue(
            rotation_check[0].enabled,
            "rotation-check must ship enabled — it is the only thing that arms the "
            "on_shift tier, so pausing it strands the whole app",
        )

    def test_every_cron_no_tier_resumes_ships_enabled(self):
        """Generalizes the cold-start rule the rotation-check test above pins for one job.

        Nothing in the codebase flips a manifest ``enabled: false``. The rotation-check
        SOP resumes **only** ``tier_crons.on_shift`` — it is explicitly forbidden from
        touching the ``always`` and ``primary`` tiers ("out of scope and, in the first
        case, unrecoverable"). So any cron NOT on the ``on_shift`` tier that ships
        disabled stays disabled forever.

        That had silently happened twice more:

        - ``ledger-hygiene`` (``primary``) — proven dead on a real install: still
          ``enabled=False`` after days of uptime, ``last_run_at=None``. It is the ONLY
          caller of the git ledger sync, the vector-index import, and the closed-incident
          pruning, so all three could never run in production no matter how well tested.
        - ``reconcile`` (``always``) — a tier whose name means "always armed" shipped
          disarmed, so the board was never reconciled against provider truth and drifted
          into fiction exactly as its own SOP warns.

        Only ``dispatch`` may ship disabled, because ``rotation-check`` genuinely arms it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        on_shift = {name.split("/", 1)[1] for name in rotation.TIER_CRONS[rotation.TIER_ON_SHIFT]}
        for cron in self._manifest().crons:
            if cron.name in on_shift:
                continue
            with self.subTest(cron=cron.name):
                self.assertTrue(
                    cron.enabled,
                    f"{cron.name!r} is not on the on_shift tier, so nothing will ever "
                    "resume it — shipping it disabled means it never runs at all",
                )

    def test_every_enabled_cron_exits_cheaply_when_unconfigured(self):
        """Enabled means it runs on installs that are not set up.

        Every self-arming cron's prompt must tell the agent to check for a configured
        provider FIRST and stop, or a fresh install burns agent turns forever on an app
        the user has not configured. Applied to all of them, not just rotation-check,
        because enabling two more crons is exactly when this stops being checked.
        """
        for cron in self._manifest().crons:
            if not cron.enabled:
                continue
            with self.subTest(cron=cron.name):
                self.assertIn("configured=true", cron.message)
                self.assertIn("NO output", cron.message)

    def test_rotation_check_exits_cheaply_when_unconfigured(self):
        """Enabled means it runs every 5 minutes on installs that are not set up.

        Its prompt must tell the agent to check for a configured provider FIRST and
        stop, or a fresh install burns an agent turn every five minutes forever.
        """
        rotation_check = [c for c in self._manifest().crons if c.name == "rotation-check"][0]
        self.assertIn("configured=true", rotation_check.message)
        self.assertIn("NO output", rotation_check.message)

    def test_crons_are_silent_and_stateless(self):
        """Silence-by-default, and no unbounded session growth on a poller."""
        for cron in self._manifest().crons:
            with self.subTest(cron=cron.name):
                self.assertTrue(cron.silent)
                self.assertFalse(cron.persistent_session)

    def test_each_cron_has_exactly_one_schedule(self):
        for cron in self._manifest().crons:
            with self.subTest(cron=cron.name):
                self.assertTrue(bool(cron.every) != bool(cron.cron_expr))

    def test_dispatch_cadence_matches_the_spec(self):
        dispatch_cron = next(c for c in self._manifest().crons if c.name == "dispatch")
        self.assertEqual(dispatch_cron.every, 120)

    def test_manifest_declares_no_app_local_skills(self):
        """A builtin's app dir is NOT copied into the data home.

        ``register_builtin_apps`` writes only ``app.json`` + ``installed.json``, so a
        ``manifest.skills`` entry pointing at an app-local directory registers
        nothing at all — silently. This app's skill therefore lives in
        ``builtin_skills/`` (packaged, copied by ``_ensure_builtin_skills``), and
        the manifest must NOT claim otherwise.
        """
        self.assertEqual(self._manifest().skills, [])


class TestSkillDelivery(unittest.TestCase):
    """The skill and its SOPs must actually reach an installed user.

    The crons reference the SOPs by absolute path, so if the files are not copied
    into the data home every scheduled job fails at its first instruction — with
    no import error and no failing test to catch it. Hence this test.
    """

    def _skill_root(self) -> Path:
        import kiro_crew

        return Path(kiro_crew.__file__).resolve().parent / "builtin_skills" / "ops-mission-control"

    def test_setup_cfg_packages_every_runtime_dir_the_app_has(self):
        """A runtime directory absent from ``package_data`` ships as nothing.

        Verified once by building a real wheel and running the app from it, which is
        the only way to be sure — but a wheel build is far too slow for the per-commit
        gate, so this asserts the *rule* the wheel obeys: every directory under the app
        that holds runtime files must be matched by a ``setup.cfg`` glob.

        ``planning/`` and ``tests/`` are deliberately unlisted: planning docs are
        working notes with no runtime role, and ``tests/`` reaches installs via
        ``packages = find:`` (it has an ``__init__.py``, matching every sibling
        builtin) rather than via ``package_data``.
        """
        repo_root = Path(__file__).resolve().parents[6]
        cfg = (repo_root / "setup.cfg").read_text(encoding="utf-8")

        app_root = Path(__file__).resolve().parents[1]
        runtime_dirs = {
            p.relative_to(app_root).parts[0]
            for p in app_root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.parent != app_root
        }
        # Not runtime: working notes, and tests which ship via packages=find:.
        runtime_dirs -= {"planning", "tests"}
        self.assertIn("backend", runtime_dirs, "precondition: backend/ must be detected")

        for name in sorted(runtime_dirs):
            with self.subTest(directory=name):
                self.assertIn(
                    f"apps/builtins/*/{name}/**/*",
                    cfg,
                    f"{name}/ holds runtime files but no setup.cfg glob matches it — "
                    "it would be missing from the wheel and the app would break only "
                    "for pip-installed users",
                )

    def test_app_is_registered_in_builtin_names(self):
        """Without this entry the app does not exist to the loader at all.

        Every other test in this suite imports the app's modules directly, so all of
        them pass whether or not the app is registered — the one thing that makes it
        REACHABLE is a single line in a shared list that a merge can silently drop.
        The frontend half is covered by `website/src/test/opsMissionControl.test.ts`;
        this is the backend half.
        """
        from kiro_crew.apps.builtins import BUILTIN_NAMES

        self.assertIn("ops_mission_control", BUILTIN_NAMES)

    def test_frontend_route_is_registered(self):
        """The dashboard page must be reachable, or the nav entry leads nowhere.

        Asserted from Python as well as vitest because the two registrations live in
        different languages and a sync that copies one repo's backend without its
        frontend leaves a half-registered app — which looks fine in the API and blank
        in the browser.
        """
        repo_root = Path(__file__).resolve().parents[6]
        registry = repo_root / "website/src/apps/builtinRegistry.ts"
        if not registry.is_file():  # pragma: no cover - python-only checkout
            self.skipTest("website/ not present in this checkout")
        self.assertIn("'/ops-mission-control'", registry.read_text(encoding="utf-8"))

    @staticmethod
    def _registered_routes() -> dict:
        """Path -> {methods} as actually registered by ``register_routes``."""
        import re

        source = (
            Path(routes.__file__).read_text(encoding="utf-8") if hasattr(routes, "__file__") else ""
        )
        found: dict = {}
        for match in re.finditer(
            r'add\.add_(get|post|put|delete)\(\s*f?"\{_BASE\}([^"]*)"', source
        ):
            path = f"/api/apps/ops-mission-control{match.group(2)}"
            found.setdefault(path, set()).add(match.group(1).upper())
        return found

    def _sop_calls(self):
        """Every ops API call the SOPs instruct the agent to make.

        Matches the PATH, not a prefix. It previously required the literal
        ``GATEWAY/api/apps/...``, which silently narrowed the check to whichever lines
        happened to spell it that way: once the SOPs were rewritten to derive
        ``$BASE``/``$TOKEN`` from ``kirocrew token``, the scanner saw **4** endpoints out
        of the **10** actually referenced — so six routes could have been renamed with
        nothing failing at build time, which is the exact failure this test exists to
        prevent. A test whose input filter can quietly shrink is worse than no test,
        because the green tick still claims coverage.
        """
        import re

        # parents[4] is src/kiro_crew (routes.py -> backend -> app -> builtins ->
        # apps -> kiro_crew). Counted, not guessed: an off-by-one makes this SKIP,
        # and a skip reads as a pass in the summary line.
        sops = Path(routes.__file__).resolve().parents[4] / "builtin_skills/ops-mission-control"
        if not sops.is_dir():  # pragma: no cover - python-only checkout
            self.skipTest("builtin_skills not present in this checkout")
        calls = []
        for md in sorted(sops.rglob("*.md")):
            for line in md.read_text(encoding="utf-8").splitlines():
                # Trailing punctuation (``.`` / ``,`` / ``)``) is excluded by the charset;
                # a query string is cut at ``?`` since routes register the path only.
                for path in re.findall(r"/api/apps/ops-mission-control[a-zA-Z0-9/_-]*", line):
                    method_match = re.search(r"-X\s+([A-Z]+)", line)
                    if method_match:
                        method = method_match.group(1)
                    elif re.search(r"\bPOST\b", line):
                        # The SOPs also write prose like "POST /api/apps/.../dispatch".
                        method = "POST"
                    else:
                        method = "GET"
                    calls.append((md.name, method, path))
        return calls

    def test_the_sop_scanner_actually_sees_the_sop_calls(self):
        """Guards the guard: assert the scanner's own yield, not just its verdict.

        ``test_every_sop_endpoint_resolves_to_a_real_route`` passes trivially when the
        scanner finds nothing, and that is precisely how it silently degraded from 10
        endpoints to 4. Pinning a floor turns a narrowing filter into a failure instead
        of a still-green tick.
        """
        calls = self._sop_calls()
        distinct = {path for _, _, path in calls}
        self.assertGreaterEqual(
            len(distinct),
            10,
            f"scanner sees only {len(distinct)} distinct ops endpoint(s) in the SOPs: "
            f"{sorted(distinct)} — the filter has narrowed, so routes are unguarded",
        )

    def test_every_sop_endpoint_resolves_to_a_real_route(self):
        """The SOPs are the agent's API contract, and a wrong path fails silently.

        An agent told to curl a path that does not exist gets a 404 mid-investigation
        and has no way to tell "the app is broken" from "I was given the wrong URL". No
        import error and no failing test would catch it — the same shape as the `omc-*`
        cron names that made tier arming inert.
        """
        registered = self._registered_routes()
        self.assertTrue(registered, "precondition: routes must be discoverable")
        for filename, _method, path in self._sop_calls():
            with self.subTest(sop=filename, path=path):
                self.assertIn(
                    path,
                    registered,
                    f"{filename} tells the agent to call {path}, which is not registered",
                )

    def test_every_sop_call_uses_a_method_the_route_accepts(self):
        """A right path with the wrong verb is a 405 the agent cannot diagnose either.

        Note when writing this test: `-sS` sits before `-X POST` in these curls, so a
        regex that treats `-X` as optional right after `curl` silently defaults every
        call to GET and reports three false mismatches. Match `-X` anywhere on the line.
        """
        registered = self._registered_routes()
        for filename, method, path in self._sop_calls():
            with self.subTest(sop=filename, path=path, method=method):
                self.assertIn(
                    method,
                    registered.get(path, set()),
                    f"{filename} calls {method} {path}, which accepts "
                    f"{sorted(registered.get(path, set()))}",
                )

    def test_readme_exists_and_is_packaged(self):
        """A public app with no README leaves a stranger — and a companion author —
        with nowhere to start. Siblings ship one; the packaging glob already exists.

        Asserts that SOME glob covers the README rather than one literal pattern. The
        literal form broke on a merge that replaced ``apps/builtins/*/README.md`` with the
        broader ``apps/builtins/*/*.md`` — packaging was still correct, but the test failed
        because it pinned the spelling instead of the property it cares about.
        """
        readme = Path(__file__).resolve().parents[1] / "README.md"
        self.assertTrue(readme.is_file(), "the app must ship a README")
        repo_root = Path(__file__).resolve().parents[6]
        cfg = (repo_root / "setup.cfg").read_text(encoding="utf-8")
        covering = ("apps/builtins/*/README.md", "apps/builtins/*/*.md")
        self.assertTrue(
            any(glob in cfg for glob in covering),
            f"no package_data glob covers the app README; looked for {covering}",
        )

    def test_readme_companion_contract_names_the_real_symbols(self):
        """The README documents the companion entry point and the four Protocols.

        A doc that drifts from the code it teaches is worse than none, so pin the
        load-bearing names against their definitions rather than trusting prose.
        """
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        from kiro_crew.apps.builtins.ops_mission_control.backend import companion

        self.assertIn(companion.PROVIDER_GROUP, readme)
        self.assertIn("register_adapters", readme)
        for protocol in ("SignalSource", "RotationSource", "ActionSink", "EvidenceSource"):
            self.assertIn(protocol, readme)
        # The registrar methods the example calls must exist on the registry.
        from kiro_crew.apps.builtins.ops_mission_control.backend.registry import (
            OpsProviderRegistry,
        )

        for method in ("register_signal_source", "register_action_sink"):
            self.assertIn(method, readme)
            self.assertTrue(hasattr(OpsProviderRegistry, method))

    def test_app_manifest_is_packaged(self):
        """Without app.json the app does not exist to the loader at all."""
        repo_root = Path(__file__).resolve().parents[6]
        cfg = (repo_root / "setup.cfg").read_text(encoding="utf-8")
        self.assertIn("apps/builtins/*/app.json", cfg)

    def test_skill_is_in_the_packaged_builtin_skills_tree(self):
        self.assertTrue((self._skill_root() / "SKILL.md").is_file())

    def test_every_sop_ships_beside_the_skill(self):
        sops = self._skill_root() / "sops"
        found = {p.name for p in sops.glob("*.md")} if sops.is_dir() else set()
        self.assertEqual(
            found,
            {
                "dispatch.md",
                "investigate.md",
                "reconcile.md",
                "rotation-check.md",
                "ledger-hygiene.md",
                # Not cron-driven: a handover is read by a person at a moment they
                # choose, and a scheduled one nobody reads is the noise this app
                # exists to avoid. It still must SHIP, or the skill points the agent
                # at a file that is not there.
                "handover.md",
            },
        )

    def test_every_sop_tells_the_agent_how_to_authenticate(self):
        """Every SOP calls HTTP endpoints, so every SOP must say how to get a token.

        Regression test for an observed unattended failure. The SKILL and all six SOPs
        instructed the agent to call routes and NEVER mentioned auth, so a
        ``rotation-check`` run improvised: it hardcoded a port that belonged to a
        different gateway, collected ``{"error": "Token required"}`` 65 times, burned 41
        tool calls hunting for a token the cron runner deliberately destroys before the
        first tool call, and hit the 1800s cron timeout without ever reaching the API.
        That reads as "the app is broken" when the fix was six lines of documentation.

        A cron agent may read ONLY its own SOP, so a single note in SKILL.md is not
        enough — each SOP carries the recipe.
        """
        sops = sorted((self._skill_root() / "sops").glob("*.md"))
        self.assertEqual(len(sops), 6)
        for sop in sops:
            text = sop.read_text(encoding="utf-8")
            self.assertIn("kirocrew token", text, f"{sop.name} never says how to authenticate")
            self.assertIn("?token=", text, f"{sop.name} does not show how to pass the token")

    def test_the_auth_recipe_does_not_hardcode_a_port(self):
        """A hardcoded port is the exact failure that cost the timed-out run.

        The recipe must DERIVE the base URL from `kirocrew token`, because the port
        varies per instance (5476 default, but any operator may run another).
        """
        import re

        root = self._skill_root()
        for doc in [root / "SKILL.md", *sorted((root / "sops").glob("*.md"))]:
            text = doc.read_text(encoding="utf-8")
            for block in re.findall(r"```bash\n(.*?)```", text, re.S):
                if "kirocrew token" not in block:
                    continue
                # Inside the auth recipe itself, no literal host:port may appear.
                self.assertNotRegex(
                    block,
                    r"(?:127\.0\.0\.1|localhost):\d+",
                    f"{doc.name} hardcodes a host:port in the auth recipe",
                )

    def test_the_auth_recipe_is_runnable_shell(self):
        """The snippet is copy-pasted by an agent, so it must parse as shell.

        Asserted with `bash -n` on the extracted block rather than by eye: the recipe
        contains `${URL%%\\?*}`, whose backslash is easy to mangle when editing markdown,
        and a snippet that fails to parse sends the agent straight back to improvising.
        """
        import re
        import shutil
        import subprocess

        bash = shutil.which("bash")
        if not bash:  # pragma: no cover - CI images all have bash
            self.skipTest("bash not available")

        text = (self._skill_root() / "sops" / "rotation-check.md").read_text(encoding="utf-8")
        blocks = [b for b in re.findall(r"```bash\n(.*?)```", text, re.S) if "kirocrew token" in b]
        self.assertTrue(blocks, "the auth recipe block is missing")
        proc = subprocess.run(
            [bash, "-n"], input=blocks[0].encode(), capture_output=True, timeout=30
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"auth recipe is not valid shell: {proc.stderr.decode('utf-8', 'replace')}",
        )

    def test_cron_prompts_point_at_paths_that_will_exist(self):
        """Each cron's SOP reference must resolve in a real install."""
        from kiro_crew.apps.manifest import AppManifest

        manifest = AppManifest.from_json_file(Path(__file__).resolve().parents[1] / "app.json")
        prefix = "~/.kiro/crew/skills/ops-mission-control/sops/"
        for cron in manifest.crons:
            with self.subTest(cron=cron.name):
                self.assertIn(prefix, cron.message)
                # The referenced file must exist in the packaged tree.
                referenced = cron.message.split(prefix, 1)[1].split(".md", 1)[0] + ".md"
                self.assertTrue((self._skill_root() / "sops" / referenced).is_file())


if __name__ == "__main__":
    unittest.main()


class TestABooleanFieldIsNeverCoerced(_HomeIsolatedAsync):
    """`bool("false")` is True, and one of these fields decides a production write.

    Every boolean on these endpoints was parsed with `bool(body[...])`. On a string that is
    true for ANY non-empty text, so every spelling of "no" a client might send — `"false"`,
    `"False"`, `"no"`, `"0"` — became True. The decide endpoint is where that inverts an
    ANSWER rather than a setting: a request meaning "reject this proposal" reached
    `decide_proposal(approve=True)` and executed the authorized action against the provider.

    Refusing is the only defensible behavior. There is no safe guess about which way an
    operator meant an ambiguous answer to "may I write to production?", so the request 400s
    and the operator re-sends it unambiguously. Found in review.
    """

    #: Every string an operator or a sloppy client plausibly means as "no". Each one is
    #: truthy under `bool()`, which is the whole bug.
    FALSY_LOOKING = ("false", "False", "FALSE", "no", "0", "off")

    async def test_a_string_approve_is_refused_rather_than_read_as_yes(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        for value in self.FALSY_LOOKING:
            with self.subTest(approve=value):
                response = await routes._handle_decide_proposal(
                    _request({"id": "INV-1", "approve": value, "digest": "x"})
                )
                self.assertEqual(response.status, 400, f"{value!r} was accepted")
                body = _payload(response)
                self.assertEqual(body["code"], "invalid_field_type")
                # And nothing was decided either way.
                self.assertIsNone(store.get_incident("INV-1"))

    async def test_a_string_true_is_refused_too(self):
        """Not just the falsy-looking ones: `"true"` is equally not a boolean.

        Accepting `"true"` while refusing `"false"` would be the worst of both worlds — it
        would teach a client that strings work, and that client's next `"false"` would be the
        one that silently approved.
        """
        response = await routes._handle_decide_proposal(
            _request({"id": "INV-1", "approve": "true", "digest": "x"})
        )
        self.assertEqual(response.status, 400)

    async def test_a_missing_approve_is_refused_not_defaulted(self):
        """Absent is not "no". A decide request that forgot the field is malformed, and
        defaulting it either way invents an answer the operator did not give."""
        response = await routes._handle_decide_proposal(_request({"id": "INV-1", "digest": "x"}))
        self.assertEqual(response.status, 400)
        self.assertEqual(_payload(response)["code"], "missing_required_field")

    async def test_real_booleans_still_work(self):
        """The guard must not break the legitimate path — `false` has to reach the reject
        branch, not the 400."""
        response = await routes._handle_decide_proposal(
            _request({"id": "does-not-exist", "approve": False, "digest": "x"})
        )
        # Past validation: it fails on the unknown incident, not on the field type.
        self.assertNotEqual(_payload(response).get("code"), "invalid_field_type")

    async def test_every_settings_boolean_refuses_a_string(self):
        """The same coercion sat on five settings fields, two of them safety-relevant.

        `schedule_strict_gating` gates the off-shift refusal and `primary_instance` decides who
        may prune the SHARED ledger — a non-leader that believes it is the leader prunes the
        team's knowledge. Asserted per-field so a failure names the one that regressed.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        for field in (
            "schedule_strict_gating",
            "primary_instance",
            "slack_enabled",
            "notify_enabled",
            "ledger_sync_enabled",
        ):
            with self.subTest(field=field):
                response = await routes._handle_put_settings(_request({field: "false"}))
                self.assertEqual(response.status, 400, f"{field} accepted a string")
                self.assertEqual(_payload(response)["code"], "invalid_field_type")
                self.assertNotIn(field, read_config(), f"{field} was persisted by a 400")


class TestSettingsAppliesTheCeilingAtomically(_HomeIsolatedAsync):
    """`mode` and `autonomy_rules` are one decision; a partial write is never a safe state.

    `mode` was persisted first and the rules validated second, so a request carrying a valid
    `mode=act` and one malformed rule wrote the mode, returned 400, and left the instance in
    `act` — activating whatever grants were already stored, from a request the operator was told
    had FAILED. Found in review.
    """

    async def test_a_rejected_rule_does_not_leave_act_mode_enabled(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        response = await routes._handle_put_settings(
            _request(
                {
                    "mode": "act",
                    # Refused: an act-rule may not be a blanket grant.
                    "autonomy_rules": [{"source": "cloudwatch", "mode": "act"}],
                }
            )
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(
            rotation.app_mode(),
            "observe",
            "the mode was persisted by a request that returned 400",
        )

    async def test_an_invalid_mode_does_not_write_the_valid_rules(self):
        """The converse ordering must hold too."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        response = await routes._handle_put_settings(
            _request(
                {
                    "mode": "yolo",
                    "autonomy_rules": [
                        {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}
                    ],
                }
            )
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(rotation.load_rules(), [])

    async def test_no_rejected_field_anywhere_leaves_the_ceiling_raised(self):
        """The unit of atomicity is the WHOLE REQUEST, not the mode/rules pair.

        An earlier fix made `mode` and `autonomy_rules` atomic with respect to each other but
        still wrote them before validating everything else. So a PUT carrying a valid
        `mode=act` plus any other invalid field persisted `act` and THEN returned 400 — the
        operator was told the request failed while the instance had begun authorizing provider
        writes against whatever rules were already stored. Found in review, one round after the
        narrower pair fix: "validate both halves" was the wrong scope.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        for label, extra in (
            ("over-long remote", {"ledger_sync_remote": "x" * 600}),
            ("credential-bearing remote", {"ledger_sync_remote": "https://u:p@github.com/o/r.git"}),
            ("option-like branch", {"ledger_sync_branch": "--evil"}),
            ("non-positive tuning", {"max_claims_per_cycle": 0}),
            ("non-integer tuning", {"stale_after_secs": "soon"}),
        ):
            response = await routes._handle_put_settings(_request({"mode": "act", **extra}))
            self.assertEqual(response.status, 400, label)
            self.assertEqual(
                rotation.app_mode(),
                "observe",
                f"{label}: the ceiling was raised by a request that returned 400",
            )

    async def test_no_field_at_all_is_applied_when_any_field_is_rejected(self):
        """Generalized from the ceiling to EVERY field, because they all had the defect.

        The previous round moved only `mode`/`autonomy_rules` to the end of the handler, which
        answered "which field is dangerous to half-apply?" — the wrong question. Review then
        found `primary_instance`: `{"primary_instance": false, "ledger_sync_branch": "--bad"}`
        returned 400 having already flipped leadership, and leadership decides which instance
        passes the `not_primary` gate on `POST /ledger/hygiene` (a non-leader that thinks it is
        the leader prunes the SHARED ledger). So the handler is now two phases — validate
        everything, then write everything — and this test pins the property rather than the
        field list: a request that 400s changes NOTHING.

        Fails against a handler that writes any field before the last validation.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        # Every writable field, all valid, plus ONE poisoned field that must sink the lot.
        good = {
            "mode": "act",
            "autonomy_rules": [{"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}],
            "primary_instance": False,
            "slack_enabled": True,
            "slack_channel": "C123",
            "notify_enabled": True,
            "schedule_github_login": "octocat",
            "schedule_strict_gating": False,
            "max_claims_per_cycle": 7,
            "stale_after_secs": 99,
            "needs_human_stale_after_secs": 111,
            "ledger_sync_enabled": True,
            "ledger_sync_remote": "https://github.com/o/r.git",
            "ledger_sync_branch": "main",
        }
        for label, poison in (
            ("invalid mode", {"mode": "yolo"}),
            ("blanket act-rule", {"autonomy_rules": [{"source": "cloudwatch", "mode": "act"}]}),
            ("malformed login", {"schedule_github_login": "not a login!"}),
            ("over-long remote", {"ledger_sync_remote": "x" * 600}),
            ("credential remote", {"ledger_sync_remote": "https://u:p@github.com/o/r.git"}),
            ("option-like branch", {"ledger_sync_branch": "--evil"}),
            ("non-positive tuning", {"max_claims_per_cycle": 0}),
            ("non-integer tuning", {"stale_after_secs": "soon"}),
        ):
            response = await routes._handle_put_settings(_request({**good, **poison}))
            self.assertEqual(response.status, 400, label)

            # Nothing on the fenced floor moved...
            self.assertEqual(rotation.app_mode(), "observe", f"{label}: ceiling raised")
            self.assertEqual(rotation.load_rules(), [], f"{label}: rules written")
            # ...and nothing in plain config either. `primary_instance` is the one review
            # named: absent means the `is_primary()` default still applies.
            cfg = read_config()
            for key in (
                "primary_instance",
                "max_claims_per_cycle",
                "stale_after_secs",
                "needs_human_stale_after_secs",
            ):
                self.assertNotIn(key, cfg, f"{label}: {key} was persisted by a 400")

    async def test_a_fully_valid_request_applies_both(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        rule = {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}
        response = await routes._handle_put_settings(
            _request({"mode": "act", "autonomy_rules": [rule]})
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(rotation.app_mode(), "act")
        self.assertEqual([rotation.rule_to_dict(r) for r in rotation.load_rules()], [rule])
