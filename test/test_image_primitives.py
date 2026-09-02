"""Tests for image-support primitives.

Covers the two primitives that shipped from the image-generation work:
1. ``AcpRuntime(model=...)`` — pins a model at kiro-cli process start via
   ``--model`` (the only reliable way to run a cross-provider model; agent
   configs may pin their own model and post-session set_model cannot cross
   provider boundaries).
2. The ``image`` artifact kind — lets authored SVG illustrations be saved as
   artifacts with ``kind="image"`` and ``source_path`` pointing to the file.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp.runtime import AcpRuntime


class TestRuntimeModelParam:
    def test_model_defaults_to_none(self):
        rt = AcpRuntime()
        assert rt._model is None

    def test_model_stored(self):
        rt = AcpRuntime(model="gpt-5.6-sol")
        assert rt._model == "gpt-5.6-sol"

    def test_model_rejects_flag_injection(self):
        # A leading dash could be parsed as a CLI flag by kiro-cli — rejected.
        with pytest.raises(ValueError, match="Invalid model identifier"):
            AcpRuntime(model="--trust-all-tools")

    def test_model_rejects_shell_metacharacters(self):
        for bad in ("gpt 5.6", "gpt;rm", "a/b", "", "x" * 200):
            with pytest.raises(ValueError, match="Invalid model identifier"):
                AcpRuntime(model=bad)

    def _capture_spawn_argv(self, rt: AcpRuntime) -> list[str]:
        """Run spawn() just far enough to capture the argv passed to
        wrap_argv, then abort. Avoids launching a real kiro-cli process."""
        captured: list[str] = []

        def _capture(argv, **kwargs):
            captured.extend(argv)
            raise RuntimeError("abort-after-capture")

        with (
            patch(
                "kiro_crew.acp.runtime._resolve_kiro_bin_for_spawn",
                new_callable=AsyncMock,
                return_value="/bin/kiro-cli",
            ),
            patch("kiro_crew.acp.runtime.wrap_argv", side_effect=_capture),
        ):
            with pytest.raises(RuntimeError, match="abort-after-capture"):
                asyncio.get_event_loop().run_until_complete(rt.spawn())
        return captured

    def test_spawn_argv_includes_model_flag(self, tmp_path):
        rt = AcpRuntime(work_dir=tmp_path, model="gpt-5.6-sol")
        argv = self._capture_spawn_argv(rt)
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"

    def test_spawn_argv_omits_model_flag_when_unset(self, tmp_path):
        rt = AcpRuntime(work_dir=tmp_path)
        argv = self._capture_spawn_argv(rt)
        assert "--model" not in argv


class TestImageArtifactKind:
    def test_image_in_allowed_kinds(self):
        from kiro_crew.artifacts import ALLOWED_KINDS

        assert "image" in ALLOWED_KINDS

    def test_validation_kind_regex_accepts_image(self):
        from kiro_crew.validation import _ARTIFACT_KIND_RE

        assert _ARTIFACT_KIND_RE.match("image")

    def test_validation_kind_regex_rejects_unknown(self):
        from kiro_crew.validation import _ARTIFACT_KIND_RE

        assert not _ARTIFACT_KIND_RE.match("video")
