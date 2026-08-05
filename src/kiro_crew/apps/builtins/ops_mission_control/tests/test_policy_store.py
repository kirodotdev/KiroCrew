"""The autonomy ceiling must be on the keystone floor, not in agent-writable config.

`mode` + `autonomy_rules` are the app's security ceiling: `effective = min(app_mode,
rule_mode)` is only a ceiling if the agent cannot raise it. They lived in `data/config.json`,
which is served unauthenticated over `/config` and writable by any auto-approved agent shell —
so a prompt-injected agent could set `mode=act` plus a matching rule and unlock a provider
write. Found in review; fixed by moving them to `ops_mission_control_policy.json` on the
`security._CREW_SECRET_LEAVES` floor.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew import platform_compat


class _HomeIsolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTheCeilingIsOnTheKeystoneFloor(_HomeIsolated):
    def test_the_policy_file_is_fenced_and_config_is_not(self):
        """The whole point: the agent can neither read nor overwrite the ceiling."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            CONFIG_FILENAME,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.store import APP_NAME
        from kiro_crew.apps.manager import app_data_dir
        from kiro_crew.security import is_sensitive_path

        self.assertTrue(
            is_sensitive_path(str(policy_store.policy_path())),
            "the autonomy ceiling must be on the keystone floor",
        )
        cfg = app_data_dir(APP_NAME) / CONFIG_FILENAME
        self.assertFalse(
            is_sensitive_path(str(cfg)),
            "sanity: config.json is NOT fenced — which is exactly why the ceiling cannot "
            "live there",
        )

    def test_the_filename_matches_the_fence_entry(self):
        """A rename here without updating `_CREW_SECRET_LEAVES` silently un-protects it."""
        from kiro_crew import security
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        self.assertIn(policy_store.POLICY_FILENAME, security._CREW_SECRET_LEAVES)

    def test_setting_the_mode_writes_the_fenced_file_not_config(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        policy_store.set_mode("act")
        self.assertEqual(policy_store.read_mode("observe"), "act")
        self.assertTrue(policy_store.policy_path().exists())
        # The agent-writable file must NOT carry the ceiling.
        self.assertNotIn("mode", read_config())

    def test_rotation_reads_mode_and_rules_from_the_store(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation

        policy_store.set_mode("act")
        policy_store.set_rules(
            [{"mode": "act", "source": "cloudwatch", "resource_glob": "prod-*"}]
        )
        self.assertEqual(rotation.app_mode(), "act")
        rules = rotation.load_rules()
        self.assertEqual(len(rules), 1)


class TestConfigJsonCannotSetTheCeiling(_HomeIsolated):
    """The ceiling is read ONLY from the keystone file, never from agent-writable config.json.

    An earlier revision migrated `mode`/`autonomy_rules` out of `config.json` onto the fenced
    floor on first read, to spare a "pre-fence install" a shadowed copy. That migration WAS
    the hole: `config.json` is on no sensitive-path list, so an auto-approved agent shell could
    write `{"mode":"act", "autonomy_rules":[...]}` there, and the next `app_mode()` — reached
    from `authorize_action` on every action — promoted it to the keystone and made it
    authoritative, granting a real production write the operator never authorized. The app is
    new in this PR, so there is no install whose ceiling needs lifting. Found in review (Opus 5).
    """

    def _seed_config(self, **keys: object) -> Path:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            CONFIG_FILENAME,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.store import APP_NAME
        from kiro_crew.apps.manager import app_data_dir

        cfg = app_data_dir(APP_NAME) / CONFIG_FILENAME
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(keys), encoding="utf-8")
        return cfg

    def test_a_mode_written_to_config_json_does_not_raise_the_ceiling(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation

        # An auto-approved agent shell writes the ceiling it wants into config.json.
        self._seed_config(
            mode="act",
            autonomy_rules=[{"mode": "act", "source": "pagerduty", "resource_glob": "*"}],
        )

        # It must be ignored: the gate stays at the safe default.
        self.assertEqual(rotation.app_mode(), "observe")
        self.assertEqual(rotation.load_rules(), [])
        # And the read must NOT have created a keystone file from the config values.
        self.assertFalse(policy_store.policy_path().exists())

    def test_the_migration_helper_no_longer_exists(self):
        """Deleting the function is the fix; a test pins that it stays deleted, because
        re-adding it silently re-opens the ceiling-raise."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        self.assertFalse(hasattr(policy_store, "migrate_from_config_if_needed"))

    def test_the_keystone_is_authoritative_when_the_operator_set_it(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation

        policy_store.set_mode("propose")  # the dashboard PUT is the only writer
        self._seed_config(mode="act")  # a stale/hostile config value must NOT win
        self.assertEqual(rotation.app_mode(), "propose")


class TestOutboundDestinationsAreOperatorOnly(_HomeIsolated):
    """WHERE this app sends things is a security decision, not a preference.

    Found by auditing for the class of the autonomy-ceiling finding rather than waiting for it
    to be reported. Two destinations were in agent-writable `config.json`:

    - `ledger_sync_remote` — the git remote the shared knowledge ledger is PUSHED to. An agent
      that rewrites it exfiltrates the team's accumulated incident knowledge, and
      `POST /ledger/hygiene` (which the agent's own hygiene cron calls) performs the push.
    - `slack_channel` — where every incident title, diagnosis and resource name is mirrored.

    Verified before fixing: writing `config.json` moved both, and `config.json` is neither
    path-fenced (`is_sensitive_path`) nor shell-write-blocked
    (`is_sensitive_bash_command("echo x > …")`).
    """

    def test_an_agent_write_cannot_move_the_ledger_remote(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import write_config

        ledger_sync.set_settings(enabled=True, remote_url="https://github.com/org/real.git")
        # The agent writes config.json — the only file it can reach.
        write_config({"ledger_sync_remote": "https://attacker.example/exfil.git"})
        self.assertEqual(
            ledger_sync.remote(),
            "https://github.com/org/real.git",
            "the operator's remote must win; config.json must not redirect the push",
        )

    def test_an_agent_write_cannot_move_the_slack_channel(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import write_config

        slack_out.set_settings(enabled=True, channel_id="C_REAL")
        write_config({"slack_channel": "C_ATTACKER", "slack_enabled": True})
        self.assertEqual(slack_out.channel(), "C_REAL")

    def test_the_destinations_live_on_the_fenced_floor(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            ledger_sync,
            policy_store,
            slack_out,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        ledger_sync.set_settings(enabled=True, remote_url="https://github.com/org/real.git")
        slack_out.set_settings(enabled=True, channel_id="C_REAL")
        cfg = read_config()
        for key in ("ledger_sync_remote", "ledger_sync_enabled", "slack_channel", "slack_enabled"):
            self.assertNotIn(key, cfg, f"{key} must not be written to agent-writable config")
            self.assertIn(key, policy_store.OPERATOR_ONLY_KEYS)

    def test_the_branch_stays_in_plain_config_deliberately(self):
        """It selects a ref inside a remote the operator chose; it cannot move data off-box."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync, policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config

        ledger_sync.set_settings(branch_name="main")
        self.assertEqual(read_config().get("ledger_sync_branch"), "main")
        self.assertNotIn("ledger_sync_branch", policy_store.OPERATOR_ONLY_KEYS)

    def test_a_destination_written_to_config_json_does_not_redirect_the_exchange(self):
        """`ledger_sync_remote` in config.json must NOT become the push destination — that is
        the exfiltration the fencing exists to stop, and reading config for it (or migrating
        it onto the floor) would reopen it. With nothing on the keystone, the remote is unset,
        not the agent-supplied one."""
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync, policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            CONFIG_FILENAME,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.store import APP_NAME
        from kiro_crew.apps.manager import app_data_dir

        cfg = app_data_dir(APP_NAME) / CONFIG_FILENAME
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            json.dumps({"ledger_sync_remote": "https://attacker.example/exfil.git"}),
            encoding="utf-8",
        )
        self.assertEqual(ledger_sync.remote(), "")
        self.assertFalse(policy_store.policy_path().exists())

    def test_put_refuses_a_key_that_is_not_operator_only(self):
        """The allow-list is the contract — a typo must not silently create a fenced key."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        with self.assertRaises(KeyError):
            policy_store.put("region", "us-east-1")
        with self.assertRaises(KeyError):
            policy_store.get("region")


class TestPrimaryFlagIsOperatorOnly(_HomeIsolated):
    """`primary_instance` decides who may PRUNE the shared ledger, so the agent must not set it.

    The identity keys (`schedule-file.github_login`, `pagerduty.user_id`) fence one route to the
    ledger-prune gate: forge who you are, match the schedule's `leader:`, pass the 409. This flag
    is the OTHER route, and it needs no identity — when the committed schedule names no leader,
    `is_primary()` falls back to it, so an agent shell writing `{"primary_instance": true}` into
    `config.json` self-promotes to leader and `POST /ledger/hygiene` prunes and pushes a shared
    ledger this instance does not own. A concurrent prune deletes team knowledge no later run
    recovers. Found in review (GPT 5.6).
    """

    def test_an_agent_write_cannot_self_promote_to_primary(self):
        """The bypass itself: config.json says primary, the keystone says not, keystone wins."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import write_config

        # Operator opted this instance OUT of the primary tier.
        policy_store.put(policy_store.PRIMARY_KEY, False)
        # The agent writes the only file it can reach, claiming leadership.
        write_config({"primary_instance": True})
        self.assertFalse(
            rotation.is_primary(),
            "config.json must not grant the primary tier — it gates the SHARED ledger prune",
        )

    def test_the_flag_lands_on_the_fenced_floor_not_in_config_json(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config
        from kiro_crew.security import is_sensitive_path

        policy_store.put(policy_store.PRIMARY_KEY, False)
        self.assertTrue(is_sensitive_path(str(policy_store.policy_path())))
        self.assertNotIn("primary_instance", read_config())
        self.assertIn(policy_store.PRIMARY_KEY, policy_store.OPERATOR_ONLY_KEYS)

    def test_a_solo_install_still_defaults_to_primary(self):
        """Fencing must not break the single-instance case: nothing stored means primary."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation

        self.assertFalse(policy_store.policy_path().exists())
        self.assertTrue(rotation.is_primary(), "a solo install must still run ledger hygiene")


if __name__ == "__main__":
    unittest.main()


class TestActRulesAreAuthorable(_HomeIsolated):
    """The `act` mode must have a WRITE path. It had none.

    `policy_store.set_rules` existed with zero callers anywhere, so the app's headline
    autonomy tier was unreachable: Settings said grants came from "patterns you have
    explicitly allowlisted with a rule", offered nothing to click, and the manual pointed at
    `data/config.json` — which the keystone migration ignores once the policy file exists. So
    an operator who followed the docs got silent Propose behavior forever. Found in review.
    """

    def test_a_valid_rule_round_trips_through_save_and_load(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        submitted = [
            {
                "source": "pagerduty",
                "mode": "act",
                "resource_glob": "Checkout*",
                "actions": ["ack"],
            }
        ]
        ok, code, normalized = rotation.save_rules(submitted)
        self.assertTrue(ok, code)
        # Read back through the REAL gate loader, not the raw file: what the operator sees
        # must be what actually authorizes.
        loaded = [rotation.rule_to_dict(r) for r in rotation.load_rules()]
        self.assertEqual(loaded, normalized)
        self.assertEqual(loaded, submitted)

    def test_rules_land_on_the_keystone_floor_not_in_config_json(self):
        """A grant is half the authorization, so the agent must not be able to write it."""
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store, rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import read_config
        from kiro_crew.security import is_sensitive_path

        ok, _, _ = rotation.save_rules(
            [{"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}]
        )
        self.assertTrue(ok)

        path = policy_store.policy_path()
        self.assertTrue(is_sensitive_path(str(path)), "the ceiling is not agent-fenced")
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("autonomy_rules", stored)
        # And NOT in the agent-writable file.
        self.assertNotIn("autonomy_rules", read_config())

    def test_a_blanket_act_rule_is_refused_not_silently_dropped(self):
        """`load_rules` skips unparseable entries, so storing one would show the operator a
        saved grant that never matches — the exact failure the two-key design prevents."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        ok, code, _ = rotation.save_rules([{"source": "cloudwatch", "mode": "act"}])
        self.assertFalse(ok)
        self.assertEqual(code, "rule_0_invalid")
        # Nothing was persisted.
        self.assertEqual(rotation.load_rules(), [])

    def test_an_all_wildcard_glob_is_the_same_blanket_grant_and_is_refused(self):
        """`resource_glob: "*"` is "act on everything" spelled differently.

        The nonempty check above only asked whether a glob was PRESENT, so `"*"` passed it
        and `fnmatch` then matched every resource — including the empty string. That is the
        provider-wide act grant the module docstring calls inexpressible, reachable from
        Settings through `save_rules`. Asserted through `save_rules` (the write path an
        operator actually uses) AND `load_rules` (so a hand-edited keystone cannot smuggle
        one in either). Found in review (GPT 5.6).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        # Every spelling that narrows nothing: bare wildcards, `?`, and whitespace padding
        # (no resource id is whitespace, so `"*  *"` matches everything a bare `*` does).
        for glob in ("*", "**", "?", "*?*", "???", "*  *", "* ? *"):
            with self.subTest(glob=glob):
                ok, code, _ = rotation.save_rules(
                    [{"source": "cloudwatch", "mode": "act", "resource_glob": glob}]
                )
                self.assertFalse(ok, f"blanket glob {glob!r} was accepted")
                self.assertEqual(code, "rule_0_invalid")
                self.assertEqual(rotation.load_rules(), [])

    def test_a_glob_carrying_one_literal_still_grants(self):
        """The fix must refuse only what narrows NOTHING — a real glob keeps working."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        for glob in ("prod-*", "*-prod", "*prod*", "?-db", "a", "my app-*"):
            with self.subTest(glob=glob):
                ok, code, _ = rotation.save_rules(
                    [{"source": "cloudwatch", "mode": "act", "resource_glob": glob}]
                )
                self.assertTrue(ok, f"narrow glob {glob!r} was refused: {code}")
                self.assertEqual(len(rotation.load_rules()), 1)

    def test_a_non_act_rule_may_still_be_broad(self):
        """`observe`/`propose` write nothing, so a wide glob there is not a grant."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        ok, code, _ = rotation.save_rules(
            [{"source": "cloudwatch", "mode": "observe", "resource_glob": "*"}]
        )
        self.assertTrue(ok, code)
        self.assertEqual(len(rotation.load_rules()), 1)

    def test_the_offending_index_is_reported(self):
        """A ten-rule submission with one bad entry must say WHICH one."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        ok, code, _ = rotation.save_rules([
            {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"},
            {"source": "datadog", "mode": "act"},
        ])
        self.assertFalse(ok)
        self.assertEqual(code, "rule_1_invalid")

    def test_non_list_and_non_object_payloads_are_refused(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        # Deliberately the WRONG type: this arrives as untrusted JSON from a PUT body, so the
        # runtime guard is the thing under test and mypy's objection is the point.
        ok, code, _ = rotation.save_rules({"source": "cloudwatch"})  # type: ignore[arg-type]
        self.assertFalse(ok)
        self.assertEqual(code, "rules_not_a_list")

        ok2, code2, _ = rotation.save_rules(["cloudwatch"])
        self.assertFalse(ok2)
        self.assertEqual(code2, "rule_0_not_an_object")

    def test_saving_an_empty_list_clears_every_grant(self):
        """Revoking must be expressible, or an operator cannot take authority back."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation

        rotation.save_rules([{"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}])
        self.assertEqual(len(rotation.load_rules()), 1)
        ok, _, normalized = rotation.save_rules([])
        self.assertTrue(ok)
        self.assertEqual(normalized, [])
        self.assertEqual(rotation.load_rules(), [])

    def test_describe_exposes_the_rules_not_just_a_count(self):
        """A count cannot be rendered, edited or verified — the UI had nothing to show."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import rotation
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            ShiftStatus,
        )

        rule = {"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}
        rotation.save_rules([rule])
        view = rotation.describe(ShiftStatus(on_shift=True))
        self.assertEqual(view["rules"], 1)
        self.assertEqual(view["rules_detail"], [rule])


class TestConcurrentWritesCannotRestoreAStaleCeiling(unittest.TestCase):
    """Every writer here is a read-modify-write, and `atomic_write` replaces the whole file.

    Two concurrent settings PUTs therefore each wrote their own key onto a stale snapshot, and
    the later one silently restored the other's old value — measured at 100/200 rounds before
    the lock. On THIS file that is a security defect rather than a lost-update annoyance: an
    operator disabling `act` concurrently with any other settings change could have `act`
    restored, with both requests returning 200. Found in review.

    Third instance of one class in this app — `store._IndexLock` and `ledger._LedgerLock` came
    first, and the keystone was the file left without a lock, which is the one where it matters
    most.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_concurrent_destination_write_cannot_restore_act(self):
        """The scenario review named: disable `act` while another key is being written."""
        import json
        import threading

        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        losses = 0
        rounds = 40
        for _ in range(rounds):
            policy_store.set_mode("act")
            policy_store.put("slack_channel", "C-old")

            threads = [
                threading.Thread(target=lambda: policy_store.set_mode("observe")),
                threading.Thread(target=lambda: policy_store.put("slack_channel", "C-new")),
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

            data = json.loads(policy_store.policy_path().read_text(encoding="utf-8"))
            # BOTH writes must survive: neither key may be reverted by the other's snapshot.
            if data.get("mode") != "observe" or data.get("slack_channel") != "C-new":
                losses += 1

        self.assertEqual(
            losses,
            0,
            f"{losses}/{rounds} rounds lost a write — a concurrent settings PUT reverted the "
            "other's key, which on this file can restore a disabled autonomy ceiling",
        )

    @unittest.skipUnless(
        platform_compat.IS_POSIX,
        "The observable invariant depends on the file lock actually excluding, and on Windows "
        "`platform_compat.acquire_lock` is documented best-effort — `msvcrt.locking` failures "
        "are swallowed — so two threads in one process are not reliably serialized there. The "
        "single-acquisition property this test exists to protect is asserted "
        "platform-independently by `test_the_ceiling_is_written_in_one_acquisition` below.",
    )
    def test_the_two_halves_of_the_ceiling_commit_together(self):
        """`mode` and `autonomy_rules` are ONE decision, so a reader must never see a mix.

        The gate computes `effective = min(app_mode, rule_mode)`, so the pair is the
        authorization. Writing them through two separately-locked calls let a CONCURRENT
        settings PUT interleave between them: request A's `act` could land with request B's
        broader rules and authorize a provider write neither operator asked for. Each call was
        individually atomic, which is what made the gap read as safe. Found in review (GPT 5.6).

        Asserted as an INVARIANT over observed states rather than by trying to hit the
        interleaving: two writers alternate between two coherent (mode, rules) pairs, and every
        snapshot a reader takes must be one of those two pairs — never a cross. Measured against
        the split-write shape: 6751 torn reads, versus 0 here.
        """
        import json
        import threading

        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        narrow = [{"source": "cloudwatch", "mode": "act", "resource_glob": "prod-db-*"}]
        broad = [{"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}]
        # Two coherent ceilings. The dangerous cross is `act` + `broad` when nobody asked for it.
        pair_a = ("observe", narrow)
        pair_b = ("act", broad)

        stop = threading.Event()
        crosses: list[tuple] = []

        def _writer(mode, rules):
            while not stop.is_set():
                policy_store.set_ceiling(mode=mode, rules=rules)

        def _reader():
            while not stop.is_set():
                try:
                    data = json.loads(policy_store.policy_path().read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue  # mid-replace; atomic_write means this is transient
                observed = (data.get("mode"), data.get("autonomy_rules"))
                if observed not in (pair_a, pair_b):
                    crosses.append(observed)

        policy_store.set_ceiling(mode=pair_a[0], rules=pair_a[1])
        threads = [
            threading.Thread(target=_writer, args=pair_a),
            threading.Thread(target=_writer, args=pair_b),
            threading.Thread(target=_reader),
        ]
        for th in threads:
            th.start()
        # Bounded by ITERATIONS, not by a sleep: a fixed wall-clock window is the flake class
        # `testing-conventions.md` warns about. 300 write rounds is ample to expose a torn pair.
        for _ in range(300):
            policy_store.set_ceiling(mode=pair_b[0], rules=pair_b[1])
        stop.set()
        for th in threads:
            th.join(timeout=10)

        self.assertEqual(
            crosses[:3],
            [],
            "a reader observed a mode/rules pair no writer ever committed — the two halves of "
            f"the ceiling are not atomic ({len(crosses)} torn reads)",
        )

    def test_the_ceiling_is_written_in_one_acquisition(self):
        """The platform-independent form of the test above: COUNT the lock acquisitions.

        The observable-invariant test needs the lock to genuinely exclude, which Windows's
        best-effort `msvcrt.locking` does not guarantee. But the property that actually fixes
        the defect is narrower and checkable everywhere: writing both halves must take the lock
        ONCE. Two acquisitions is the window a concurrent PUT interleaves into, regardless of
        whether this platform lets a test observe the tear.

        Fails against the pre-fix shape (`set_mode` then `set_rules` = 2 acquisitions).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        acquisitions = 0
        real_enter = policy_store._PolicyLock.__enter__

        def _counting_enter(lock_self):
            nonlocal acquisitions
            acquisitions += 1
            return real_enter(lock_self)

        with mock.patch.object(policy_store._PolicyLock, "__enter__", _counting_enter):
            policy_store.set_ceiling(
                mode="act",
                rules=[{"source": "cloudwatch", "mode": "act", "resource_glob": "prod-*"}],
            )
        self.assertEqual(
            acquisitions,
            1,
            "writing mode+rules took the lock more than once, so a concurrent settings PUT can "
            "interleave between the two writes and pair one request's mode with another's rules",
        )
        # And both landed, so the single acquisition is not achieved by dropping a write.
        self.assertEqual(policy_store.read_mode("observe"), "act")
        self.assertEqual(len(policy_store.read_rules_raw()), 1)

    def test_every_writer_holds_the_lock(self):
        """Structural, because the race is timing-dependent and a new writer is easy to add.

        The behavioural test above can pass by luck on a fast machine; this cannot. Asserted
        over the SOURCE of each public writer so one added later without the lock fails here
        rather than intermittently in production.

        A writer satisfies this either by taking `_PolicyLock()` itself OR by delegating its
        whole body to a writer that does — `set_mode`/`set_rules` are now one-line wrappers
        over `set_ceiling`, which is what lets a caller commit BOTH halves of the ceiling under
        a single acquisition. Delegation is accepted only when the wrapper does no
        read-modify-write of its own, which is the property that actually matters.
        """
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        locked_writers = {"set_ceiling"}
        for writer in (
            policy_store.set_ceiling,
            policy_store.set_mode,
            policy_store.set_rules,
            policy_store.put,
        ):
            with self.subTest(writer=writer.__name__):
                source = inspect.getsource(writer)
                if "_PolicyLock()" in source:
                    continue
                delegates = [name for name in locked_writers if f"{name}(" in source]
                self.assertTrue(
                    delegates,
                    f"{writer.__name__} does a read-modify-write without the lock and without "
                    "delegating to a writer that holds it",
                )
                # A wrapper must not read or write the file itself — that would be a
                # second, unlocked access alongside the delegated one.
                for unlocked in ("_read()", "_write("):
                    self.assertNotIn(
                        unlocked,
                        source,
                        f"{writer.__name__} delegates but also touches the file directly",
                    )
