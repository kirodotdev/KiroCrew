"""``_git``'s sandbox-refusal branch: transient must reach the retry, others must not.

Unit-level, unlike ``test_ledger_sync_git.py``'s real-sandbox roundtrip: this mocks
``sandboxed_spawn_argv_async`` directly so it runs on every host, including one with
no unprivileged user namespaces. It pins the one behavior a real-sandbox test cannot
isolate on demand — that a TRANSIENT ``SandboxUnavailableError`` propagates out of
``_git`` so ``sync_safely``'s bounded retry can see it, while every other refusal
becomes rc=126 and never reaches that retry at all.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kiro_crew.sandbox import SandboxUnavailableError


class TestGitTransientSandboxRefusal(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev_home = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)
        self.addCleanup(self._restore_home)
        self._modules = dict(__import__("sys").modules)
        self.addCleanup(self._restore_modules)

    def _restore_home(self) -> None:
        if self._prev_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev_home

    def _restore_modules(self) -> None:
        import sys

        for name in list(sys.modules):
            if name not in self._modules:
                del sys.modules[name]
        sys.modules.update(self._modules)

    async def test_transient_refusal_propagates_instead_of_becoming_an_rc(self):
        """A cold-cache TRANSIENT refusal must reach the caller as an exception.

        ``pull``/``push`` turn an rc!=0 into ``(False, detail)`` and ``sync_safely`` never
        retries that shape — only its ``except Exception`` clause retries. Converting the
        transient refusal to rc=126 (as this PR's first pass did) silently drops the retry
        the docstring says must not be suppressed.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        refusal = SandboxUnavailableError(
            "sandbox backend probe still warming", kind="transient", detail="cold cache"
        )
        with mock.patch.object(ledger_sync, "sandboxed_spawn_argv_async", side_effect=refusal):
            with self.assertRaises(SandboxUnavailableError) as ctx:
                await ledger_sync._git("status")
        self.assertEqual(ctx.exception.kind, "transient")

    async def test_non_transient_refusal_becomes_rc_126_not_an_exception(self):
        """A durable refusal (no backend / governance-forbidden) must NOT propagate.

        These have no cold-cache condition to wait out, so turning them into an exception
        would just make ``sync_safely`` retry a refusal that cannot possibly succeed the
        second time -- rc=126 with a stable operator-facing detail is the correct outcome,
        unchanged by this PR.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        refusal = SandboxUnavailableError(
            "no sandbox backend available on this host", kind="no_backend", detail="x"
        )
        with mock.patch.object(ledger_sync, "sandboxed_spawn_argv_async", side_effect=refusal):
            rc, out, err = await ledger_sync._git("status")
        self.assertEqual(rc, 126)
        self.assertEqual(out, "")
        self.assertIn(ledger_sync._SANDBOX_REFUSAL_DETAIL, err)

    async def test_sync_safely_retries_once_on_the_propagated_transient_refusal(self):
        """End-to-end through ``sync_safely``: first attempt raises transient, retry succeeds.

        Proves the two units compose: ``_git`` re-raising is only useful if
        ``sync_safely``'s retry actually fires on what it re-raises.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        refusal = SandboxUnavailableError(
            "sandbox backend probe still warming (transient, retry)",
            kind="transient",
            detail="cold cache",
        )
        calls = {"n": 0}

        async def _flaky_pull():
            calls["n"] += 1
            if calls["n"] == 1:
                raise refusal
            return True, "pulled"

        with (
            mock.patch.object(ledger_sync, "configured", return_value=True),
            mock.patch.object(ledger_sync, "pull", side_effect=_flaky_pull),
            mock.patch("asyncio.sleep", new=mock.AsyncMock()),
        ):
            result = await ledger_sync.sync_safely(direction="pull")

        self.assertEqual(result, "pulled")
        self.assertEqual(calls["n"], 2, "the retry must have fired exactly once")
