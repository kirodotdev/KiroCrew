"""Tests for Issue Radar's dispatch gate (RFC phase 0).

The gate decides whether an issue may be handed to an implementation attempt. It
ships ahead of anything that runs an agent, so these tests are the whole contract
for now:

* ``dispatch.resolve_checkout`` accepts an absolute, non-sensitive, existing git
  work tree and refuses everything else -- including a symlink whose TARGET is
  sensitive, which is why resolution happens before the sensitivity test.
* ``dispatch.readiness`` distinguishes "no path set" from "the path you set
  broke", because those need different sentences in the UI.
* the store round-trips the path per provider+host, and a permissions self-heal
  write does not drop it.
* the routes refuse rather than fall back: a rejected path stores nothing.

Handlers are driven directly with ``aiohttp.test_utils.make_mocked_request``, the
same shape the other Issue Radar route tests use.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.issue_radar.backend import dispatch, routes, store

BASE = "/api/apps/issue-radar"


def _git_dir(parent: Path, name: str = "checkout", *, marker: str = "dir") -> Path:
    """An absolute directory that looks like a git work tree.

    The markers are REAL, not just a ``.git`` entry: ``resolve_checkout``
    validates them positively, because an empty ``.git`` directory and a dangling
    ``gitdir:`` pointer both ``exists()`` while being unusable.

    ``marker="file"`` writes ``.git`` as a FILE holding a ``gitdir:`` pointer to an
    admin dir that EXISTS -- exactly the shape ``git worktree add`` produces. A
    linked worktree must be accepted, since that is where dispatch will work.
    """
    root = parent / name
    root.mkdir(parents=True)
    if marker == "dir":
        dot_git = root / ".git"
        dot_git.mkdir()
        (dot_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (dot_git / "objects").mkdir()
        (dot_git / "refs").mkdir()
    else:
        # What ``git worktree add`` leaves: an admin dir holding HEAD plus a
        # commondir pointing at the shared repo's object and ref stores.
        shared = parent / f"{name}-repo"
        (shared / "objects").mkdir(parents=True, exist_ok=True)
        (shared / "refs").mkdir(parents=True, exist_ok=True)
        admin = parent / f"{name}-admin"
        admin.mkdir(parents=True, exist_ok=True)
        (admin / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (admin / "commondir").write_text(f"{shared}\n", encoding="utf-8")
        (root / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return root


class TestResolveCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_blank_is_refused(self):
        self.assertIsNone(dispatch.resolve_checkout(""))
        self.assertIsNone(dispatch.resolve_checkout("   "))

    def test_relative_path_is_refused(self):
        # realpath() would resolve this against the gateway's own cwd and hand back
        # an absolute path, so the check has to happen on the expanded input.
        self.assertIsNone(dispatch.resolve_checkout("some/checkout"))
        self.assertIsNone(dispatch.resolve_checkout("."))

    def test_missing_directory_is_refused(self):
        self.assertIsNone(dispatch.resolve_checkout(str(self.tmp / "nope")))

    def test_file_is_refused(self):
        path = self.tmp / "a-file"
        path.write_text("x", encoding="utf-8")
        self.assertIsNone(dispatch.resolve_checkout(str(path)))

    def test_directory_without_git_is_refused(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIsNone(dispatch.resolve_checkout(str(plain)))

    def test_clone_is_accepted(self):
        root = _git_dir(self.tmp)
        self.assertEqual(dispatch.resolve_checkout(str(root)), root.resolve())

    def test_an_empty_git_directory_is_refused(self):
        """``.git`` exists but holds no repository. Existence is not the question:
        reporting ready for a directory no worktree can be added to is the same
        defect as rendering a check that never ran as a check that passed."""
        root = self.tmp / "hollow"
        (root / ".git").mkdir(parents=True)
        self.assertIsNone(dispatch.resolve_checkout(str(root)))

    def test_a_git_directory_missing_the_object_store_is_refused(self):
        root = self.tmp / "partial"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.assertIsNone(dispatch.resolve_checkout(str(root)))

    def test_a_git_directory_relocated_by_commondir_is_accepted(self):
        """``objects``/``refs`` can legitimately live elsewhere; ``commondir`` is
        how that is spelled, so requiring them in place would refuse a usable
        tree."""
        root = self.tmp / "shared"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        common = self.tmp / "the-repo"
        (common / "objects").mkdir(parents=True)
        (common / "refs").mkdir(parents=True)
        (root / ".git" / "commondir").write_text(f"{common}\n", encoding="utf-8")
        self.assertEqual(dispatch.resolve_checkout(str(root)), root.resolve())

    def test_a_commondir_pointing_at_nothing_is_refused(self):
        """A ``commondir`` naming a directory with no object store describes a
        repository that is not there, so accepting the marker on sight would
        report ready for a tree git cannot use."""
        root = self.tmp / "dangling-common"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (root / ".git" / "commondir").write_text("/nonexistent/repo\n", encoding="utf-8")
        self.assertIsNone(dispatch.resolve_checkout(str(root)))

    def test_a_pointer_to_an_ordinary_directory_is_refused(self):
        """The pointer can name ANY directory. Existence of the target is not the
        question -- it has to hold the admin markers git puts there."""
        root = self.tmp / "pointer-to-junk"
        root.mkdir()
        junk = self.tmp / "just-a-dir"
        junk.mkdir()
        (root / ".git").write_text(f"gitdir: {junk}\n", encoding="utf-8")
        self.assertIsNone(dispatch.resolve_checkout(str(root)))

    def test_a_dangling_worktree_pointer_is_refused(self):
        """The shape left behind when the parent repo (or the worktree's admin
        dir) is deleted: the pointer file still exists and still parses."""
        root = self.tmp / "orphan"
        root.mkdir()
        (root / ".git").write_text("gitdir: /nonexistent/.git/worktrees/x\n", encoding="utf-8")
        self.assertIsNone(dispatch.resolve_checkout(str(root)))

    def test_a_git_file_that_is_not_a_pointer_is_refused(self):
        root = self.tmp / "junk"
        root.mkdir()
        (root / ".git").write_text("not a pointer\n", encoding="utf-8")
        self.assertIsNone(dispatch.resolve_checkout(str(root)))

    def test_linked_worktree_is_accepted(self):
        root = _git_dir(self.tmp, "wt", marker="file")
        self.assertEqual(dispatch.resolve_checkout(str(root)), root.resolve())

    def test_sensitive_path_is_refused(self):
        root = _git_dir(self.tmp)
        with mock.patch.object(dispatch, "is_sensitive_path", return_value=True):
            self.assertIsNone(dispatch.resolve_checkout(str(root)))

    def test_symlink_is_resolved_before_the_sensitivity_test(self):
        """A symlink in a benign directory must not smuggle its target past the
        sensitivity check, so the value tested is the RESOLVED one."""
        real = _git_dir(self.tmp, "secret-checkout")
        link = self.tmp / "innocent"
        link.symlink_to(real, target_is_directory=True)
        seen: list[str] = []

        def _only_the_target(path: str) -> bool:
            seen.append(path)
            return path == str(real.resolve())

        with mock.patch.object(dispatch, "is_sensitive_path", side_effect=_only_the_target):
            self.assertIsNone(dispatch.resolve_checkout(str(link)))
        # The link's own path was never what got judged.
        self.assertEqual(seen, [str(real.resolve())])

    def test_symlink_to_a_benign_checkout_resolves(self):
        real = _git_dir(self.tmp, "real")
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        self.assertEqual(dispatch.resolve_checkout(str(link)), real.resolve())

    def test_an_unresolvable_path_is_refused_not_raised(self):
        """`realpath` raises ValueError on an embedded NUL. Every other unusable
        value returns None here, so this one must too -- otherwise a bad request
        reaches the route as a 500."""
        self.assertIsNone(dispatch.resolve_checkout("/tmp/a\x00b"))
        # And through the readiness wrapper, which is what the route calls.
        self.assertEqual(
            dispatch.readiness("/tmp/a\x00b"), (False, dispatch.REASON_CHECKOUT_UNUSABLE)
        )

    def test_security_module_is_present_in_this_build(self):
        """The fallback in dispatch.py fails CLOSED, which is only correct if the
        real predicate is normally in use -- assert that it is, so a silent import
        break shows up here rather than as every path being refused."""
        self.assertTrue(dispatch._HAS_SECURITY)


class TestReadiness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unset_reports_no_local_path(self):
        for value in ("", "   ", None):
            ready, reason = dispatch.readiness(value)
            self.assertFalse(ready)
            self.assertEqual(reason, dispatch.REASON_NO_LOCAL_PATH)

    def test_valid_checkout_is_ready(self):
        root = _git_dir(self.tmp)
        self.assertEqual(dispatch.readiness(str(root)), (True, dispatch.REASON_OK))

    def test_a_recorded_path_that_broke_is_its_own_reason(self):
        """A checkout deleted after being recorded must not keep reporting ready,
        and must not be confused with never having been set."""
        root = _git_dir(self.tmp)
        stored = str(root)
        self.assertTrue(dispatch.readiness(stored)[0])
        shutil.rmtree(root)
        ready, reason = dispatch.readiness(stored)
        self.assertFalse(ready)
        self.assertEqual(reason, dispatch.REASON_CHECKOUT_UNUSABLE)
        self.assertNotEqual(reason, dispatch.REASON_NO_LOCAL_PATH)


class TestLocalPathStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unset_and_unconnected_both_read_empty(self):
        self.assertEqual(store.read_repo_local_path("no", "pe", root=self.tmp), "")
        store.add_connected_repo("o", "r", root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "")

    def test_roundtrip(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.set_repo_local_path("o", "r", "/srv/checkout", root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "/srv/checkout")

    def test_empty_clears_the_key_entirely(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.set_repo_local_path("o", "r", "/srv/checkout", root=self.tmp)
        store.set_repo_local_path("o", "r", "", root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "")
        entry = store.list_connected_repos(self.tmp)[0]
        # Cleared, not stored as an empty string: a cleared repo has to be
        # indistinguishable from one that never had a path.
        self.assertNotIn("local_path", entry)

    def test_scoped_by_provider_and_host(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.add_connected_repo(
            "o", "r", provider="gitlab", host="gitlab.com", root=self.tmp
        )
        store.set_repo_local_path("o", "r", "/srv/gh", root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "/srv/gh")
        self.assertEqual(
            store.read_repo_local_path(
                "o", "r", provider="gitlab", host="gitlab.com", root=self.tmp
            ),
            "",
        )

    def test_writing_to_an_unconnected_repo_raises(self):
        """A concurrent disconnect lands between the caller's connected-check and
        this lock. Returning normally would let the route report a path as saved
        that no entry holds."""
        with self.assertRaises(KeyError):
            store.set_repo_local_path("ghost", "repo", "/srv/checkout", root=self.tmp)

    def test_survives_a_permissions_selfheal_write(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.set_repo_local_path("o", "r", "/srv/checkout", root=self.tmp)
        store.set_repo_permissions("o", "r", {"push": True}, root=self.tmp)
        self.assertEqual(store.read_repo_local_path("o", "r", root=self.tmp), "/srv/checkout")


def _get(query: dict | None = None) -> web.Request:
    full = f"{BASE}/dispatch-readiness"
    if query:
        full = f"{full}?{urlencode(query)}"
    return make_mocked_request("GET", full, app=web.Application())


def _post(body: object) -> web.Request:
    req = make_mocked_request("POST", f"{BASE}/repo/local-path", app=web.Application())
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    else:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.Response) -> dict:
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _connected(value: bool = True):
    return mock.patch.object(store, "is_repo_connected", return_value=value)


class TestDispatchRoutes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Point the app's data dir at a tmp tree so the routes exercise the REAL
        # store instead of a mock of it.
        patcher = mock.patch.object(store, "app_data_dir", return_value=self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        store.add_connected_repo("o", "r")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_get_requires_owner_and_repo(self):
        resp = await routes._handle_get_dispatch_readiness(_get())
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "repo_required")

    async def test_get_refuses_an_unconnected_repo(self):
        with _connected(False):
            resp = await routes._handle_get_dispatch_readiness(_get({"owner": "o", "repo": "r"}))
        self.assertEqual(resp.status, 404)
        self.assertEqual(_body(resp)["code"], "repo_not_connected")

    async def test_get_reports_no_local_path(self):
        with _connected():
            resp = await routes._handle_get_dispatch_readiness(_get({"owner": "o", "repo": "r"}))
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_NO_LOCAL_PATH)
        self.assertEqual(payload["local_path"], "")

    async def test_get_reports_ready_for_a_real_checkout(self):
        root = _git_dir(self.tmp, "co")
        store.set_repo_local_path("o", "r", str(root))
        with _connected():
            resp = await routes._handle_get_dispatch_readiness(_get({"owner": "o", "repo": "r"}))
        payload = _body(resp)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_OK)

    async def test_get_redacts_a_credential_pattern_in_the_echoed_path(self):
        """Nothing re-validates a stored string on read, and the config file the
        value comes back out of is not agent-unwritable, so the echo is treated as
        output rather than as a value this route vouched for."""
        store.set_repo_local_path("o", "r", "/srv/AKIAIOSFODNN7EXAMPLE/co")
        with _connected():
            resp = await routes._handle_get_dispatch_readiness(_get({"owner": "o", "repo": "r"}))
        payload = _body(resp)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", payload["local_path"])
        # Derived from the RAW value: the stored path is not a checkout, and
        # redacting the echo must not be what decides that.
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_CHECKOUT_UNUSABLE)
        # The store still holds what was written -- redaction is display-only.
        self.assertEqual(store.read_repo_local_path("o", "r"), "/srv/AKIAIOSFODNN7EXAMPLE/co")

    async def test_get_echoes_an_ordinary_checkout_path_unchanged(self):
        """Redaction rewrites credential patterns, not hex directory names, so a
        real path still round-trips into the settings field."""
        root = _git_dir(self.tmp, "a1cfe06b647009c417537edb8b93b2fe8f735fee")
        store.set_repo_local_path("o", "r", str(root))
        with _connected():
            resp = await routes._handle_get_dispatch_readiness(_get({"owner": "o", "repo": "r"}))
        self.assertEqual(_body(resp)["local_path"], str(root))

    async def test_post_rejects_a_malformed_body(self):
        for body in (None, ["not", "a", "dict"]):
            resp = await routes._handle_set_repo_local_path(_post(body))
            self.assertEqual(resp.status, 400)
            self.assertEqual(_body(resp)["code"], "invalid_body")

    async def test_post_requires_owner_and_repo(self):
        resp = await routes._handle_set_repo_local_path(_post({"local_path": "/srv/x"}))
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "repo_required")

    async def test_post_refuses_an_unconnected_repo(self):
        with _connected(False):
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": "/srv/x"})
            )
        self.assertEqual(resp.status, 404)
        self.assertEqual(_body(resp)["code"], "repo_not_connected")

    async def test_post_refuses_a_non_string_path(self):
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": 17})
            )
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_local_path")

    async def test_post_refuses_a_bad_path_and_stores_nothing(self):
        """The refusal is the feature: a path that does not validate must not be
        stored as if it were fine, and must not become a fallback."""
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": str(self.tmp / "not-a-repo")})
            )
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_local_path")
        self.assertEqual(store.read_repo_local_path("o", "r"), "")

    async def test_post_without_the_field_refuses_and_leaves_the_value_alone(self):
        """An omitted `local_path` is a bad request, not a request to clear. The
        assertion that matters is the second one: the stored path survives."""
        root = _git_dir(self.tmp, "co")
        store.set_repo_local_path("o", "r", str(root))
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r"})
            )
        self.assertEqual(resp.status, 400)
        self.assertEqual(_body(resp)["code"], "invalid_local_path")
        self.assertEqual(store.read_repo_local_path("o", "r"), str(root))

    async def test_post_404s_when_the_repo_vanishes_before_the_write(self):
        """The connected-check passes, then the entry is gone by the time the store
        takes its lock. Answering 200 would report a path as saved that no entry
        holds."""
        root = _git_dir(self.tmp, "co2")
        # `_connected` is patched True while the config holds no such entry, which is
        # exactly the state a concurrent disconnect leaves behind.
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "gone", "repo": "away", "local_path": str(root)})
            )
        self.assertEqual(resp.status, 404)
        self.assertEqual(_body(resp)["code"], "repo_not_connected")

    async def test_post_stores_the_resolved_path_and_reports_ready(self):
        real = _git_dir(self.tmp, "real")
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": str(link)})
            )
        self.assertEqual(resp.status, 200)
        payload = _body(resp)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_OK)
        # Resolved, not the symlink: readiness later re-checks the directory the
        # validator accepted.
        self.assertEqual(payload["local_path"], str(real.resolve()))
        self.assertEqual(store.read_repo_local_path("o", "r"), str(real.resolve()))

    async def test_post_with_an_empty_path_clears_it(self):
        root = _git_dir(self.tmp, "co")
        store.set_repo_local_path("o", "r", str(root))
        with _connected():
            resp = await routes._handle_set_repo_local_path(
                _post({"owner": "o", "repo": "r", "local_path": ""})
            )
        payload = _body(resp)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["reason"], dispatch.REASON_NO_LOCAL_PATH)
        self.assertEqual(store.read_repo_local_path("o", "r"), "")


class TestRouteRegistration(unittest.TestCase):
    def test_both_routes_are_registered_and_gated(self):
        app = web.Application()
        routes.register_routes(app)
        paths = {
            (r.method, r.resource.canonical)  # type: ignore[union-attr]
            for r in app.router.routes()
        }
        self.assertIn(("GET", f"{BASE}/dispatch-readiness"), paths)
        self.assertIn(("POST", f"{BASE}/repo/local-path"), paths)

    def test_dispatch_routes_are_not_reachable_with_the_internal_secret(self):
        """Only ``/investigation`` is admitted there, and deliberately as a full
        path rather than a prefix. A session that could write a checkout path
        could point a later dispatch at a directory the user never named."""
        from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

        self.assertNotIn(f"{BASE}/dispatch-readiness", _MIXED_INTERNAL_API_PATHS)
        self.assertNotIn(f"{BASE}/repo/local-path", _MIXED_INTERNAL_API_PATHS)
        self.assertNotIn(BASE, _MIXED_INTERNAL_API_PATHS)


class TestConfigIsWriteProtected(unittest.TestCase):
    """The route refusing the internal secret is not enough on its own: the value
    is read back out of a file on disk, and the readiness gate is only as
    trustworthy as the store behind it."""

    def test_agent_file_tools_cannot_write_the_repo_config(self):
        """The DIRECTORY is protected, not just ``config.json``: a leaf-only rule
        lets the store be replaced wholesale (rename the dir aside, move a
        prepared one in) without any write ever naming the protected leaf."""
        from kiro_crew.security import is_sensitive_write_path

        for prefix in ("~/.kiro/crew", "~/.kirocrew"):
            for target in (
                f"{prefix}/apps/issue-radar/data/config.json",
                f"{prefix}/apps/issue-radar/data",
            ):
                with self.subTest(target=target):
                    self.assertTrue(is_sensitive_write_path(target))

    def test_reads_are_still_allowed(self):
        """Write-protected, NOT read+write sensitive. The file holds no secret and
        the app reads it on every request; classifying it sensitive would be a
        heavier control than the defect needs."""
        from kiro_crew.security import is_sensitive_path

        for prefix in ("~/.kiro/crew", "~/.kirocrew"):
            with self.subTest(prefix=prefix):
                self.assertFalse(is_sensitive_path(f"{prefix}/apps/issue-radar/data/config.json"))

    def test_the_protected_path_is_the_one_the_store_actually_uses(self):
        """A drift guard: a protection pinned to a path the store stopped using
        would read as enforced while enforcing nothing.

        Compared as path PARTS, not as a "/"-joined suffix: on Windows the store
        returns backslashes, so a POSIX-separator string suffix never matches and
        the guard would fail there while passing on POSIX."""
        self.assertEqual(
            store.config_path().parts[-4:],
            ("apps", "issue-radar", "data", "config.json"),
        )

    def test_shell_writes_are_refused_too(self):
        """Closing only the file-edit tool gate leaves the shell form open, and
        the shell reaches the same file. Matched verb-independently, so a novel
        write verb or a quoted redirect cannot slip past an allowlist."""
        from kiro_crew.security import is_sensitive_bash_command

        leaf = "apps/issue-radar/data/config.json"
        for command in (
            f'echo x > "$HOME/.kiro/crew/{leaf}"',
            f"tee ~/.kiro/crew/{leaf}",
            f"cp /tmp/forged ~/.kirocrew/{leaf}",
            f"python3 -c \"open('~/.kiro/crew/{leaf}', 'w')\"",
            # Replacing the whole store, which names the DIRECTORY and never the
            # leaf -- the form a leaf-only rule would have allowed.
            "mv /tmp/forged ~/.kiro/crew/apps/issue-radar/data",
            "cp -r /tmp/forged ~/.kirocrew/apps/issue-radar/data",
            # The UNEXPANDED data-home variable: a command the shell expands at
            # runtime never contains the resolved root, so matching only that
            # form would miss these. Same treatment $HOME already gets.
            'echo x > "$KIROCREW_HOME/apps/issue-radar/data/config.json"',
            "tee ${KIROCREW_HOME}/apps/issue-radar/data/config.json",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(is_sensitive_bash_command(command))

    def test_a_custom_data_home_is_anchored_on_both_gates(self):
        """A non-default KIROCREW_HOME puts the file outside the home-anchored
        ``<home>/<crew-prefix>/`` shape, so the shell matcher would have refused
        the default path while allowing the real one -- with the tool gate, which
        re-anchors custom homes, refusing both. The two layers have to agree.

        Asserted for EVERY write-protected leaf, not just this app's: the gap was
        inherited, so a fix that only covered the newest entry would leave the
        data-home marker and the Ops Mission Control leaves exposed."""
        from kiro_crew import security as security_mod

        with tempfile.TemporaryDirectory() as custom:
            with mock.patch.dict(os.environ, {"KIROCREW_HOME": custom}):
                for leaf in (
                    "apps/issue-radar/data",
                    ".data-home-ready",
                    "apps/ops-mission-control/data/rotation.yaml",
                ):
                    target = os.path.join(custom, *leaf.split("/"))
                    with self.subTest(leaf=leaf):
                        self.assertTrue(security_mod.is_sensitive_write_path(target))

    @unittest.skipIf(sys.platform == "win32", "the shell matcher is authored for POSIX spellings")
    def test_a_custom_data_home_is_anchored_on_the_shell_gate(self):
        """The shell half of the same question. POSIX-only because every matcher in
        that module is authored against POSIX shell spellings (``~``, ``$HOME``,
        ``/home/<user>``, ``/`` separators) -- the same reason the credential rules
        are not asserted for backslash forms."""
        from kiro_crew import security as security_mod

        with tempfile.TemporaryDirectory() as custom:
            with mock.patch.dict(os.environ, {"KIROCREW_HOME": custom}):
                for leaf in (
                    "apps/issue-radar/data",
                    ".data-home-ready",
                    "apps/ops-mission-control/data/rotation.yaml",
                ):
                    with self.subTest(leaf=leaf):
                        self.assertIsNotNone(
                            security_mod.is_sensitive_bash_command(f'echo x > "{custom}/{leaf}"')
                        )

    @unittest.skipIf(sys.platform == "win32", "the shell matcher is authored for POSIX spellings")
    def test_the_matcher_is_not_served_stale_after_the_data_home_changes(self):
        """The pattern embeds the resolved custom root, so a single cached
        instance would keep matching the PREVIOUS root -- failing OPEN for the
        new one. Two different homes in sequence must each be refused."""
        from kiro_crew import security as security_mod

        for _ in range(2):
            with tempfile.TemporaryDirectory() as custom:
                with mock.patch.dict(os.environ, {"KIROCREW_HOME": custom}):
                    self.assertIsNotNone(
                        security_mod.is_sensitive_bash_command(
                            f'echo x > "{custom}/apps/issue-radar/data/config.json"'
                        )
                    )
