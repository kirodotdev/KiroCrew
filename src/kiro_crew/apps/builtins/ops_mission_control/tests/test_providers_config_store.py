"""Tests for the provider config store and the keystone secret store.

Both are read-modify-writes over a file that ``atomic_write`` replaces, so concurrent saves
must not lose data, and a write must never publish the config over a read that failed.
"""

import contextlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestConcurrentStoreWritesDoNotLoseData(unittest.TestCase):
    """The config store and the keystone secret store are both read-modify-writes over a file
    that `atomic_write` REPLACES, so two concurrent saves each write onto a stale snapshot and
    the later replace silently drops the other. Measured before the locks: the config store
    lost updates about half the time; the SECRET store lost ~118/120 — nearly every concurrent
    save of two different providers dropped one credential. Found in review, the same class as
    `store._IndexLock` / `ledger._LedgerLock` / `policy_store._PolicyLock`.
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

    def test_concurrent_config_writes_keep_both_keys(self):
        import threading

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            merge_provider_config,
            read_config,
            set_top_level,
        )

        losses = 0
        for _ in range(30):
            set_top_level("primary_instance", True)
            merge_provider_config("cloudwatch", {"region": "old"})
            threads = [
                threading.Thread(target=lambda: set_top_level("primary_instance", False)),
                threading.Thread(
                    target=lambda: merge_provider_config("cloudwatch", {"region": "new"})
                ),
            ]
            for th in threads:
                th.start()
            for th in threads:
                # flake-ok: pre-existing unbounded join of self-completing writers, moved verbatim
                th.join()
            cfg = read_config()
            if cfg.get("primary_instance") is not False or (
                cfg.get("providers", {}).get("cloudwatch", {}).get("region") != "new"
            ):
                losses += 1
        self.assertEqual(losses, 0, f"{losses}/30 config rounds lost a concurrent write")

    def test_concurrent_secret_saves_keep_both_credentials(self):
        import threading

        from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import KeystoneFileBackend

        backend = KeystoneFileBackend()
        losses = 0
        for _ in range(30):
            threads = [
                threading.Thread(target=lambda: backend.put("pagerduty", "api_token", "pd")),
                threading.Thread(target=lambda: backend.put("datadog", "api_key", "dd")),
            ]
            for th in threads:
                th.start()
            for th in threads:
                # flake-ok: pre-existing unbounded join of self-completing writers, moved verbatim
                th.join()
            if backend.get("pagerduty", "api_token") != "pd" or (
                backend.get("datadog", "api_key") != "dd"
            ):
                losses += 1
            backend.delete("pagerduty")
            backend.delete("datadog")
        self.assertEqual(
            losses,
            0,
            f"{losses}/30 secret rounds lost a credential — a lost update here is a lost secret",
        )

    def test_no_module_open_codes_a_config_read_modify_write(self):
        """`write_config` must only be reached through the locked setters.

        `_ConfigLock` guards `merge_provider_config` and `set_top_level`, but a caller that
        open-codes `cfg = read_config(); cfg[k] = v; write_config(cfg)` bypasses the lock and
        drops concurrent updates. Two did — `ledger_sync.set_settings` and
        `notify_out.set_settings` — and were repointed at `set_top_level`. This walks the whole
        backend so a third cannot reappear: outside `providers/__init__.py` (which DEFINES the
        store), no module may call `write_config` at all.
        """
        import ast
        import pathlib as _pl

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import __file__ as pf

        backend = _pl.Path(pf).parent.parent
        offenders = []
        for path in sorted(backend.rglob("*.py")):
            if path.name == "__init__.py" and path.parent.name == "providers":
                continue  # the store's own definition
            if "/tests/" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "write_config"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "write_config() is an unlocked whole-file replace; go through set_top_level / "
            f"merge_provider_config (which hold _ConfigLock). Open-coded at: {offenders}",
        )

    def test_both_stores_lock_their_read_modify_write(self):
        """Structural, because the races above are timing-dependent and pass by luck on a
        fast machine. A new writer added without the lock fails here instead of in production."""
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import secrets
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            merge_provider_config,
            set_top_level,
        )

        for fn in (merge_provider_config, set_top_level):
            with self.subTest(fn=fn.__name__):
                self.assertIn("_ConfigLock()", inspect.getsource(fn))
        for meth in (secrets.KeystoneFileBackend.put, secrets.KeystoneFileBackend.delete):
            with self.subTest(meth=meth.__name__):
                self.assertIn("self._lock()", inspect.getsource(meth))


class TestTheAppConfigIsNeverPublishedOverAFailedRead(unittest.TestCase):
    """The BASE read of a read-modify-write may not fail open.

    ``read_config`` collapses every failure to ``{}``, which is right for the
    accessors above it — each resolves to the caller's default, so an unreadable
    config reads as "nothing configured" and the Settings page still renders. It
    is wrong as the base of ``merge_provider_config``/``set_top_level``, which
    rewrite the WHOLE file: the merge is per-FIELD within one provider slot, not
    per-file, so ``{}`` means "drop every other provider's configuration and every
    top-level key".

    The loss is quiet and it disables detection. ``provider_enabled`` defaults to
    ``False``, so a wiped config does not error — it just stops polling every
    provider the operator switched on, while their tokens remain in the keystone
    store so the Settings page still shows each provider as credentialed. The
    operator is left with an app that reports no signals and looks configured.

    Isolates ``KIROCREW_HOME`` for the same reason the sibling classes here do:
    these tests WRITE provider config.
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        # addCleanup on the line after mkdtemp: unittest skips tearDown when setUp
        # raises, so cleaning up there leaks the directory on every setUp failure
        # (and trips the temp-residue guard). Same reasoning as `_HomeIsolated` in
        # test_policy_store.py, which carries this note too.
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self._prev_home = os.environ.get("KIROCREW_HOME")
        self.addCleanup(self._restore_home)
        os.environ["KIROCREW_HOME"] = str(self._tmp)

    def _restore_home(self):
        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home

    @staticmethod
    def _config_path() -> Path:
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers
        from kiro_crew.apps.manager import app_data_dir

        return app_data_dir(providers.APP_NAME) / providers.CONFIG_FILENAME

    def _unreadable_config(self):
        """Fail ONLY the app config's read, as a transient EACCES would.

        Scoped by path: a blanket ``read_text`` failure would also break home
        resolution and the app manifest, and the test would pass for the wrong
        reason.
        """
        target = self._config_path()
        real_read_text = Path.read_text

        def _guarded(path_self, *args, **kwargs):
            if Path(path_self) == target:
                raise PermissionError(13, "Permission denied")
            return real_read_text(path_self, *args, **kwargs)

        return mock.patch.object(Path, "read_text", _guarded)

    def test_a_read_that_failed_never_truncates_the_config(self):
        """The durable harm, asserted directly: the OTHER providers the operator
        configured must still be enabled afterwards."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        providers.merge_provider_config("pagerduty", {"enabled": True})
        providers.merge_provider_config("datadog", {"enabled": True, "site": "datadoghq.eu"})
        providers.set_top_level("stale_after_secs", 1800)
        before = self._config_path().read_bytes()

        with self._unreadable_config():
            with contextlib.suppress(OSError):
                providers.merge_provider_config("cloudwatch", {"enabled": True})
            with contextlib.suppress(OSError):
                providers.set_top_level("stale_after_secs", 60)

        self.assertEqual(
            self._config_path().read_bytes(),
            before,
            "a failed read was published back over the config",
        )
        self.assertTrue(
            providers.provider_enabled("pagerduty"),
            "a failed read silently switched off a provider the operator enabled",
        )
        self.assertEqual(
            providers.config_value("datadog", "site"),
            "datadoghq.eu",
            "a failed read dropped another provider's configuration",
        )
        self.assertEqual(providers.read_config().get("stale_after_secs"), 1800)

    def test_an_unreadable_config_refuses_the_merge(self):
        """A settings save that was not recorded must not answer as though it
        were — and must not report back a slot it never persisted."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        with self._unreadable_config():
            with self.assertRaises(OSError):
                providers.merge_provider_config("pagerduty", {"enabled": True})

    def test_an_unreadable_config_refuses_a_top_level_write(self):
        """``set_top_level`` rewrites the same whole file, so it needs the same
        refusal."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        with self._unreadable_config():
            with self.assertRaises(OSError):
                providers.set_top_level("stale_after_secs", 60)

    def test_a_missing_config_is_still_a_first_write(self):
        """Absent is the one failure where ``{}`` is the truth. The guard must
        not turn the operator's very first provider save into an error."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        self.assertFalse(self._config_path().exists())
        slot = providers.merge_provider_config("pagerduty", {"enabled": True})
        self.assertEqual(slot, {"enabled": True})
        self.assertTrue(providers.provider_enabled("pagerduty"))

    def test_a_corrupt_config_refuses_the_merge_instead_of_replacing_it(self):
        """Refusal, not repair-on-write.

        A half-written or hand-broken config still names every provider the operator
        enabled. Replacing it discards that and leaves them re-entering settings they
        already chose, so the merge refuses and surfaces the corruption instead. See
        `store._read_index_for_update` for why this diverges from the four merged
        siblings of the idiom. Found in review (GPT 5.6).
        """
        import json as _json

        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"providers": {"datadog": {"enabled": true}', encoding="utf-8")

        with self.assertRaises(_json.JSONDecodeError):
            providers.merge_provider_config("pagerduty", {"enabled": True})

    def test_a_corrupt_config_is_left_exactly_as_it_was(self):
        """The point of refusing: the operator's settings must survive on disk."""
        import contextlib
        import json as _json

        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        truncated = '{"providers": {"datadog": {"enabled": true, "site": "datadoghq.eu"}'
        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(truncated, encoding="utf-8")

        with contextlib.suppress(_json.JSONDecodeError):
            providers.set_top_level("stale_after_secs", 60)

        self.assertEqual(
            path.read_text(encoding="utf-8"),
            truncated,
            "the refused write still overwrote the recoverable config",
        )

    def test_the_display_read_logs_a_non_object_root(self):
        """The last silent branch, and the docstring promised it was not silent.

        A non-object root parses fine, so no `except` fires, and this branch answered `{}`
        without a word -- while the paragraph above it argued that THIS degradation must not be
        quiet, because every provider stops polling while Settings still shows them
        configured. Found in review (GPT 5.6); the index display read grew the same warning a
        head earlier and this is its twin.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        providers._config_path().write_text("[]", encoding="utf-8")
        with self.assertLogs("kiro_crew", level="WARNING") as caught:
            self.assertEqual(providers.read_config(), {})
        self.assertTrue(any("root is not an object" in m for m in caught.output))

    def test_a_config_that_is_valid_json_but_not_an_object_refuses_the_merge(self):
        """The normalization door, config side.

        `[]` parses without raising, so the strict reader's `except FileNotFoundError`
        never fires -- but coercing it to `{}` would let the merge rewrite the whole file
        from empty and drop every provider the operator enabled. Valid JSON is not the
        same as a usable document. Found in review (GPT 5.6).
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        providers._config_path().write_text("[]", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            providers.merge_provider_config("datadog", {"enabled": True})

        self.assertEqual(providers._config_path().read_text(encoding="utf-8"), "[]")

    def test_a_providers_key_of_the_wrong_shape_refuses_the_merge(self):
        """The per-slot half of the wrong-shape door, which was still open.

        The strict read refuses a bad ROOT, but `merge_provider_config` normalized a
        `providers` key that was not an object straight to `{}` -- and since it rewrites the
        whole file, that wiped every provider slot on the next merge. Same loss the index
        side closed with `strict=True`, one level down. Found in review (Design Review),
        which spotted that the description claimed per-row refusal while the config had it
        only at the root.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import models, providers

        for label, doc in (
            ("providers is a list", '{"providers": ["pagerduty"]}'),
            ("providers is a string", '{"providers": "pagerduty"}'),
            ("one slot is a string", '{"providers": {"datadog": "enabled"}}'),
        ):
            with self.subTest(shape=label):
                providers._config_path().write_text(doc, encoding="utf-8")
                with self.assertRaises(models.CorruptDocumentError):
                    providers.merge_provider_config("datadog", {"enabled": True})
                self.assertEqual(
                    providers._config_path().read_text(encoding="utf-8"),
                    doc,
                    "the refused merge still rewrote the file",
                )

    def test_a_missing_providers_key_is_still_a_normal_first_write(self):
        """The absent case stays absent-means-empty; only a WRONG shape refuses."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        providers._config_path().write_text('{"stale_after_secs": 1800}', encoding="utf-8")
        saved = providers.merge_provider_config("datadog", {"enabled": True})
        self.assertIs(saved["enabled"], True)
        self.assertEqual(providers.read_config()["stale_after_secs"], 1800)

    def test_the_display_read_still_tolerates_corruption(self):
        """The asymmetry, pinned. Only the MUTATION base got strict -- the Settings
        page must still render on a config it cannot parse."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import providers

        path = self._config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(providers.read_config(), {})
