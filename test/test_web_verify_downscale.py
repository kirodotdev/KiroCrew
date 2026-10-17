"""Tests for the web-verify downscale helper.

The helper exists because a shell one-liner had a portability hole per platform,
so the tests pin exactly those holes: a path an inline Python literal would choke
on, the Windows interpreter suffix, and the idempotence that keeps a repeat run
from re-encoding an already-safe frame.
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "web-verify"
    / "scripts"
    / "downscale_image.py"
)


def _load():
    """Import the script by path — it ships inside a skill dir, not a package."""
    spec = importlib.util.spec_from_file_location("downscale_image", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


class DownscaleTest(unittest.TestCase):
    def _png(self, tmp: Path, size: tuple[int, int], name: str = "shot.png") -> Path:
        p = tmp / name
        Image.new("RGB", size, "red").save(p)
        return p

    def test_oversized_image_is_capped_on_the_longest_edge(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                p = self._png(Path(td), (3000, 1800))
                ok, msg = mod.shrink(str(p))
                self.assertTrue(ok, msg)
                with Image.open(p) as img:
                    self.assertEqual(img.size, (2000, 1200))
                self.assertIn("3000x1800 -> 2000x1200", msg)

    def test_image_within_the_cap_is_left_untouched(self) -> None:
        """Idempotence: a second pass must not re-encode an already-safe frame."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = self._png(Path(td), (1500, 900))
            before = p.read_bytes()
            ok, msg = mod.shrink(str(p))
            self.assertTrue(ok, msg)
            self.assertEqual(p.read_bytes(), before)
            self.assertIn("already within", msg)

    def test_path_containing_an_apostrophe_is_handled(self) -> None:
        """The hole that killed the inline `p='...'` literal: argv, not source."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "O'Brien dir"
            d.mkdir()
            p = self._png(d, (2400, 2400), name="it's a shot.png")
            self.assertEqual(mod.main([str(p)]), 0)
            with Image.open(p) as img:
                self.assertEqual(img.size, (2000, 2000))

    def test_failed_save_leaves_the_original_intact(self) -> None:
        """The rewrite is a temp-file + os.replace, never a save onto the live
        path: Pillow holds the source open (which Windows refuses to overwrite,
        the failure that took out the Windows CI shard), and an interrupted save
        must not truncate a valid image."""
        import tempfile as _tf

        with _tf.TemporaryDirectory() as td:
            p = self._png(Path(td), (2400, 2400))
            before = p.read_bytes()
            with mock.patch("PIL.Image.Image.save", side_effect=OSError("disk full")):
                ok, msg = mod.shrink(str(p))
            self.assertFalse(ok, msg)
            self.assertEqual(p.read_bytes(), before)
            leftovers = [f for f in os.listdir(td) if f.endswith(".downscale-tmp")]
            self.assertEqual(leftovers, [], f"temp file leaked: {leftovers}")

    def test_no_temp_file_is_left_behind_on_success(self) -> None:
        import tempfile as _tf

        with _tf.TemporaryDirectory() as td:
            p = self._png(Path(td), (2400, 1200))
            self.assertEqual(mod.main([str(p)]), 0)
            self.assertEqual(sorted(os.listdir(td)), [p.name])

    def test_decompression_bomb_does_not_abort_the_remaining_paths(self) -> None:
        """A tall `fullPage` capture can exceed Pillow's pixel guard, and
        `DecompressionBombError` is NOT an OSError -- uncaught it escapes `main`
        and abandons every path after it, which is the opposite of this script's
        job. The ceiling is squeezed here rather than writing a 180M-pixel
        fixture, so BOTH paths trip it; what is asserted is that the run reported
        on both instead of dying on the first.
        """
        import contextlib
        import io
        import tempfile as _tf

        with _tf.TemporaryDirectory() as td:
            first = self._png(Path(td), (2400, 2400), name="bomb.png")
            second = self._png(Path(td), (2400, 1200), name="later.png")
            untouched = first.read_bytes()
            err = io.StringIO()
            with mock.patch.object(mod, "MAX_DECODE_PIXELS", 16), \
                    contextlib.redirect_stderr(err):
                rc = mod.main([str(first), str(second)])

            self.assertEqual(rc, 1)
            messages = err.getvalue()
            self.assertIn("bomb.png", messages)
            self.assertIn("later.png", messages, "the run stopped at the first path")
            self.assertIn("refusing to decode", messages)
            self.assertEqual(first.read_bytes(), untouched)
            # The ceiling is restored, so this process can still decode normally.
            self.assertGreater(Image.MAX_IMAGE_PIXELS or 0, 16)

    def test_pillow_is_imported_at_module_level(self) -> None:
        """The optional import is module-level (top-level-imports rule); a missing
        Pillow must leave the module importable so main() can re-exec."""
        self.assertIsNotNone(mod.Image)
        src = _SCRIPT.read_text(encoding="utf-8")
        body = src.split("def shrink(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("import", body.split('"""', 2)[-1])

    def test_missing_file_reports_failure_without_raising(self) -> None:
        self.assertEqual(mod.main(["/nonexistent/shot.png"]), 1)

    def test_no_arguments_is_an_error(self) -> None:
        self.assertEqual(mod.main([]), 1)

    def test_bundled_python_is_the_launcher_s_sibling(self) -> None:
        """bin/kirocrew -> bin/python, resolved THROUGH the launcher's symlink.

        `realpath` is patched rather than creating a real symlink: on Windows
        `Path.symlink_to` needs a privilege the CI runner does not hold, which
        failed shard 4 with the link never being created. What matters here is
        that the lookup runs on the RESOLVED path, and patching the resolver
        asserts exactly that on every platform.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            bindir = Path(td) / "venv" / "bin"
            bindir.mkdir(parents=True)
            (bindir / "kirocrew").write_text("#!/bin/sh\n")
            (bindir / "python").write_text("#!/bin/sh\n")
            shim = str(Path(td) / "shim" / "kirocrew")
            with mock.patch.object(mod.shutil, "which", return_value=shim), \
                    mock.patch.object(mod.os.path, "realpath", return_value=str(bindir / "kirocrew")), \
                    mock.patch.object(mod.os, "name", "posix"):
                self.assertEqual(mod.bundled_python(), str(bindir / "python"))

    def test_bundled_python_uses_the_exe_suffix_on_windows(self) -> None:
        """Windows: Scripts/kirocrew.exe -> Scripts/python.exe. Only the suffix
        differs, which is why the directory name needs no special casing.

        Compared as RESOLVED paths, not strings: on Windows
        `TemporaryDirectory` hands back the 8.3 short form
        (`C:\\Users\\RUNNER~1\\...`) while the code under test runs
        `os.path.realpath`, which returns the long form
        (`C:\\Users\\runneradmin\\...`). The two name the same file and a raw
        string compare fails — which is exactly how this failed the Windows CI
        shard while passing on POSIX.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            scripts = Path(td) / "venv" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "kirocrew.exe").write_text("")
            (scripts / "python.exe").write_text("")
            with mock.patch.object(mod.shutil, "which", return_value=str(scripts / "kirocrew.exe")), \
                    mock.patch.object(mod.os, "name", "nt"):
                got = mod.bundled_python()
            self.assertIsNotNone(got)
            self.assertEqual(
                os.path.realpath(str(got)), os.path.realpath(str(scripts / "python.exe"))
            )

    def test_bundled_python_is_none_without_a_launcher(self) -> None:
        with mock.patch.object(mod.shutil, "which", return_value=None):
            self.assertIsNone(mod.bundled_python())

    def test_reexec_refuses_to_loop(self) -> None:
        """The guard that stops a Pillow-less venv re-execing forever."""
        with mock.patch.dict(os.environ, {mod._REEXEC_ENV: "1"}, clear=False):
            self.assertEqual(mod._reexec_with_pillow(["x.png"]), 1)

    def test_cap_matches_the_prompt_block_ceiling(self) -> None:
        """One number, two enforcement points — drift here is silent."""
        from kiro_crew.acp.prompt_blocks import MAX_IMAGE_EDGE_PX

        self.assertEqual(mod.MAX_EDGE_PX, MAX_IMAGE_EDGE_PX)


if __name__ == "__main__":
    unittest.main()
