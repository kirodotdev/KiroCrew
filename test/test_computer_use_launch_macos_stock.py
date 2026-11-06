"""``computer_launch_app`` on macOS: a stock ``/Applications`` parent.

Beside ``test_computer_use_launch.py``'s :class:`TestMacOSBundleVerification`, which
covers every other parent. Runs on every platform: the parent's owner, group and mode
and every file's owner are injected.
"""

from __future__ import annotations

import os
import stat

import pytest

pytestmark = pytest.mark.xdist_group("computer_use_launch")


class TestMacOSStockApplicationsParent:
    """A stock ``/Applications`` (``root:admin 0775``) is judged by the bundle, not the parent.

    Every admin user can create there, so the parent probe would refuse every bundle under
    it, root-owned ones included. The parent and every owner are INJECTED: a ``tmp_path`` stands
    in for ``/Applications``, so this runs on every platform and never touches the real one.
    """

    @staticmethod
    def _stock(monkeypatch, tmp_path, *, parent=(0, 80, 0o775), mine=()):
        """A real ``Foo.app`` under an injected parent whose ``(uid, gid, mode)`` is *parent*.

        Everything below the parent is root-owned and mode ``0555`` except paths ending with
        one of *mine*, which this user (uid 501) owns. The parent itself is writable, as a
        stock ``/Applications`` is for an admin user, and nothing else is.
        """
        from kiro_crew.computer_use import launch_macos

        root = tmp_path / "Applications"
        macos = root / "Foo.app" / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        (macos / "Foo").write_bytes(b"MACHO")
        (macos.parent / "Info.plist").write_bytes(b"<plist/>")
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if str(path) == str(root):
                uid, gid, mode = parent
            else:
                uid, gid, mode = (501 if str(path).endswith(mine) else 0), 0, 0o555
            return os.stat_result(
                (
                    stat.S_IFMT(info.st_mode) | mode,
                    info.st_ino,
                    info.st_dev,
                    info.st_nlink,
                    uid,
                    gid,
                    info.st_size,
                    int(info.st_atime),
                    int(info.st_mtime),
                    int(info.st_ctime),
                )
            )

        monkeypatch.setattr(launch_macos, "_APPLICATIONS", str(root), raising=False)
        monkeypatch.setattr(launch_macos, "_directory_is_writable", lambda d: d == str(root))
        monkeypatch.setattr(launch_macos.os, "geteuid", lambda: 501, raising=False)
        monkeypatch.setattr(launch_macos.os, "access", lambda *_a, **_k: False)
        monkeypatch.setattr(launch_macos.os, "stat", fake_stat)
        app = launch_macos.InstalledApp(name="Foo", path=str(root / "Foo.app"), source=str(root))
        return launch_macos, app

    def test_a_ROOT_OWNED_bundle_under_a_stock_parent_resolves(self, monkeypatch, tmp_path):
        # A pkg- or App Store-installed bundle, root-owned throughout, resolves although its
        # parent is admin-writable (the parent is writable here too).
        launch_macos, app = self._stock(monkeypatch, tmp_path)
        assert launch_macos._writable_component(app) is None

    @pytest.mark.parametrize("mine", ["Foo.app", "Contents", "MacOS"])
    def test_a_bundle_directory_this_user_OWNS_is_refused_though_read_only(
        self, monkeypatch, tmp_path, mine
    ):
        # Skipping the parent probe must not admit a bundle this user created there: the
        # owner can ``chmod`` a ``0555`` directory back at will, so ownership decides.
        launch_macos, app = self._stock(monkeypatch, tmp_path, mine=(mine,))
        assert launch_macos._writable_component(app).endswith(os.sep + mine)

    @pytest.mark.parametrize("parent", [(0, 80, 0o777), (0, 20, 0o775), (501, 80, 0o775)])
    def test_any_OTHER_parent_keeps_the_parent_probe(self, monkeypatch, tmp_path, parent):
        # Only the exact stock owner, group and mode skips the probe; the writable parent
        # then refuses a bundle that is otherwise root-owned throughout.
        launch_macos, app = self._stock(monkeypatch, tmp_path, parent=parent)
        assert launch_macos._writable_component(app) == os.path.dirname(app.path)
