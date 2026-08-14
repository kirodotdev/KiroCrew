"""``discovery.current_login`` — the one gh surface ``test_discovery.py`` skips.

It deliberately bypasses ``run_gh_json`` because ``--jq .login`` emits a BARE
STRING that the JSONL dict parser cannot represent, so it carries its own copy of
the error mapping. That copy is the thing under test here: "no gh" and "not
authenticated" must raise :class:`~sage_lib.discovery.GhSetupError` (the UI offers
setup instructions), while every other failure — non-zero exit, timeout, OSError,
empty output — must degrade to ``None`` rather than blocking the repo picker.

Patched the same way as ``test_discovery.py``: ``gh_bin`` plus
``subprocess.run``, so no real ``gh`` is required.
"""
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from sage_lib import discovery  # noqa: E402  (app root added to sys.path above)


def _stub_bin(name: str) -> str:
    """An absolute ``which``-style stub path that is valid on every platform.

    Absolute because the PATH-hijack guards refuse a relative binary path, and with a
    directory component because some call sites take ``Path(x).name`` -- a bare name
    would make that assertion vacuous. Rooted at ``tempfile.gettempdir()``, the
    portable root the cross-platform gate recommends, rather than a ``/usr/bin``
    literal that does not exist on Windows. Nothing is created or executed here.
    """
    return str(Path(tempfile.gettempdir()) / "stub-bin" / name)


def _proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestCurrentLogin(unittest.TestCase):
    def test_returns_the_bare_login_and_asks_gh_for_it_as_list_argv(self):
        captured: dict = {}

        def _fake_run(argv, *args, **kwargs):
            captured["argv"] = argv
            return _proc(stdout="octocat\n")

        with unittest.mock.patch.object(discovery, "gh_bin", return_value=_stub_bin("gh")), \
             unittest.mock.patch.object(discovery.subprocess, "run", side_effect=_fake_run):
            self.assertEqual(discovery.current_login(), "octocat")
        self.assertIsInstance(captured["argv"], list)
        self.assertEqual(captured["argv"], [_stub_bin("gh"), "api", "user", "--jq", ".login"])

    def test_empty_output_is_no_login_not_an_empty_string(self):
        with unittest.mock.patch.object(discovery, "gh_bin", return_value=_stub_bin("gh")), \
             unittest.mock.patch.object(discovery.subprocess, "run",
                                        return_value=_proc(stdout="   \n")):
            self.assertIsNone(discovery.current_login())

    def test_an_unauthenticated_gh_is_a_setup_error_the_ui_can_act_on(self):
        with unittest.mock.patch.object(discovery, "gh_bin", return_value=_stub_bin("gh")), \
             unittest.mock.patch.object(
                discovery.subprocess, "run",
                return_value=_proc(returncode=1, stderr="gh: To get started, run: gh auth login")):
            with self.assertRaises(discovery.GhSetupError) as ctx:
                discovery.current_login()
        self.assertIn("not authenticated", str(ctx.exception))

    def test_any_other_non_zero_exit_degrades_to_none(self):
        """A transient API failure must not be reported as a broken gh install."""
        with unittest.mock.patch.object(discovery, "gh_bin", return_value=_stub_bin("gh")), \
             unittest.mock.patch.object(discovery.subprocess, "run",
                                        return_value=_proc(returncode=1, stderr="502 bad gateway")):
            self.assertIsNone(discovery.current_login())

    def test_a_missing_gh_binary_is_a_setup_error(self):
        with unittest.mock.patch.object(discovery, "gh_bin", return_value=_stub_bin("gh")), \
             unittest.mock.patch.object(discovery.subprocess, "run",
                                        side_effect=FileNotFoundError("gh")):
            with self.assertRaises(discovery.GhSetupError) as ctx:
                discovery.current_login()
        self.assertIn("not installed", str(ctx.exception))

    def test_a_timeout_degrades_to_none(self):
        with unittest.mock.patch.object(discovery, "gh_bin", return_value=_stub_bin("gh")), \
             unittest.mock.patch.object(
                discovery.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=1)):
            self.assertIsNone(discovery.current_login())

    def test_an_os_error_degrades_to_none(self):
        with unittest.mock.patch.object(discovery, "gh_bin", return_value=_stub_bin("gh")), \
             unittest.mock.patch.object(discovery.subprocess, "run",
                                        side_effect=OSError("EAGAIN")):
            self.assertIsNone(discovery.current_login())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
