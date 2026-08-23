"""Locking a store down must not be able to destroy the store.

Both keystone-floor stores in this app published their payload first and applied
the owner-only lockdown afterwards:

    atomic_write(path, payload, mode=0o600)     # already published
    try:
        platform_compat.restrict_to_owner(path)
    except OSError:
        path.unlink()                            # deletes the PUBLISHED file
        raise

The unlink is there for a real reason — a secret that cannot be protected must
not stay on disk — but by the time it runs the previous, healthy store has
already been replaced. Both writers are read-modify-write over the WHOLE
document, so the file that gets unlinked holds every provider token, or the
entire autonomy ceiling, not just the value being saved. One transient lockdown
failure (an `icacls` hiccup on Windows, where the DACL call is the one that can
actually fail) therefore destroys the lot.

``atomic_write(restrict_to_owner=True)`` applies the lockdown to the TEMP file
before any content reaches it and before the rename, so a lockdown failure
raises with the previous store still in place and nothing left to unlink. These
tests pin the surviving-store property, not the implementation: they assert what
is readable after a failed save.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


class TestSecretStoreSurvivesALockdownFailure(_HomeIsolated):
    """The provider-token store. Losing this logs every integration out."""

    def _backend(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.secrets import (
            KeystoneFileBackend,
        )

        return KeystoneFileBackend()

    def test_a_failed_lockdown_does_not_delete_the_existing_tokens(self):
        backend = self._backend()
        backend.put("pagerduty", "api_token", "pd-live-token")
        self.assertEqual(backend.get("pagerduty", "api_token"), "pd-live-token")

        # The lockdown is the one step that can fail on an otherwise healthy
        # filesystem: on Windows it is an icacls/DACL call, not a mode bit.
        # Patched on the platform_compat module so it intercepts the call
        # wherever it is made from -- the store itself, or atomic_write.
        with mock.patch(
            "kiro_crew.platform_compat.restrict_to_owner",
            side_effect=OSError("icacls: transient failure"),
        ):
            with self.assertRaises(OSError):
                backend.put("datadog", "api_key", "dd-key")

        self.assertEqual(
            self._backend().get("pagerduty", "api_token"),
            "pd-live-token",
            "a failed lockdown destroyed the existing provider tokens: the store "
            "was published before it was locked down, so the failure path "
            "unlinked a file that already held every credential",
        )

    def test_a_failed_lockdown_does_not_publish_the_new_secret(self):
        """Fail-loud is preserved: the unprotected value must not land either."""
        backend = self._backend()
        backend.put("pagerduty", "api_token", "pd-live-token")

        with mock.patch(
            "kiro_crew.platform_compat.restrict_to_owner",
            side_effect=OSError("icacls: transient failure"),
        ):
            with self.assertRaises(OSError):
                backend.put("datadog", "api_key", "dd-key")

        self.assertEqual(
            self._backend().get("datadog", "api_key"),
            "",
            "a secret the process could not protect was published anyway",
        )

    def test_a_temp_file_failure_also_leaves_the_store_intact(self):
        """Preservation control: the other failure mode must stay non-destructive.

        Passes on both trees by design — it is here so a future 'simplification'
        that reintroduces a post-publish cleanup has something to fail against.
        """
        backend = self._backend()
        backend.put("pagerduty", "api_token", "pd-live-token")

        with mock.patch("tempfile.mkstemp", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                backend.put("datadog", "api_key", "dd-key")

        self.assertEqual(self._backend().get("pagerduty", "api_token"), "pd-live-token")


class TestPolicyStoreSurvivesALockdownFailure(_HomeIsolated):
    """The autonomy ceiling. Losing this drops the app to its default mode."""

    def _policy(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import policy_store

        return policy_store

    def test_a_failed_lockdown_does_not_delete_the_existing_ceiling(self):
        policy_store = self._policy()
        policy_store.set_ceiling(mode="act", rules=[{"provider": "pagerduty"}])
        self.assertEqual(policy_store.read_mode("read"), "act")

        with mock.patch(
            "kiro_crew.platform_compat.restrict_to_owner",
            side_effect=OSError("icacls: transient failure"),
        ):
            with self.assertRaises(OSError):
                policy_store.set_ceiling(mode="read")

        self.assertEqual(
            policy_store.read_mode("read-DEFAULT-MEANS-FILE-GONE"),
            "act",
            "a failed lockdown deleted the autonomy ceiling file; the app would "
            "fall back to its default mode with no record that a ceiling was set",
        )

    def test_the_ceiling_file_is_still_owner_only_after_a_successful_write(self):
        """Preservation: the security property the unlink existed to protect."""
        policy_store = self._policy()
        policy_store.set_ceiling(mode="act")

        path = policy_store.policy_path()
        self.assertTrue(path.exists(), "the ceiling was not written at all")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")).get("mode"),
            "act",
            "the ceiling did not round-trip",
        )
        if os.name == "posix":
            self.assertEqual(
                path.stat().st_mode & 0o777,
                0o600,
                "the ceiling is readable beyond its owner",
            )
