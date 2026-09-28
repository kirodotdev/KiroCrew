"""Tests for macOS x86_64 CPU feature guard in _load_llama_class()."""

from __future__ import annotations

import sys

import pytest


@pytest.mark.skipif(
    sys.platform != "darwin" or __import__("platform").machine() != "x86_64",
    reason="live sysctl test only runs on macOS x86_64",
)
def test_macos_cpu_flags_returns_missing_on_ivy_bridge():
    """On Mac Pro 6,1 (Ivy Bridge-EP): sysctl must return a list of missing flags.

    Asserts avx2/fma/bmi2 are missing only when the CPU is confirmed pre-Haswell
    (Ivy Bridge-EP). On Haswell+ the result is [] — that is also a valid pass.
    """
    import platform

    from kiro_crew.embeddings import _macos_x86_64_missing_cpu_flags

    result = _macos_x86_64_missing_cpu_flags()
    assert result is not None, "sysctl machdep.cpu.features unreadable"
    assert isinstance(result, list), f"Expected list, got: {type(result)}"
    # Only assert specific missing flags on confirmed pre-Haswell hardware
    cpu_brand = platform.processor().lower()
    is_pre_haswell = any(x in cpu_brand for x in ("ivy", "e5-1", "e5-2", "e5 v2"))
    if is_pre_haswell:
        assert "avx2" in result, f"Expected avx2 missing on pre-Haswell, got: {result}"
        assert "fma" in result, f"Expected fma missing on pre-Haswell, got: {result}"
        assert "bmi2" in result, f"Expected bmi2 missing on pre-Haswell, got: {result}"


def test_load_llama_returns_none_when_macos_x86_64_missing_flags(monkeypatch, tmp_path, caplog):
    """pre-AVX2 simulation: CPU guard must return None with warning, not SIGILL."""
    import logging

    import kiro_crew.embeddings as emb

    # Clear the lru_cache so prior suite results do not leak into this test.
    emb._load_llama_class.cache_clear()
    try:
        # Simulate macOS x86_64
        monkeypatch.setattr(emb.sys, "platform", "darwin")
        monkeypatch.setattr(emb.platform, "machine", lambda: "x86_64")

        # Create fake libs so file-presence gate passes
        fake_libs_dir = tmp_path / "libs" / "macos_x86_64"
        fake_libs_dir.mkdir(parents=True)
        for name in emb._REQUIRED_VENDORED_LIBS.get("macos_x86_64", ()):
            (fake_libs_dir / name).write_bytes(b"")

        monkeypatch.setattr(emb, "_VENDOR_DIR", tmp_path / "libs" / "..")
        monkeypatch.setattr(emb, "_LIBS_DIR_NAME", "libs")

        # Missing AVX2/FMA/BMI2
        monkeypatch.setattr(emb, "_macos_x86_64_missing_cpu_flags", lambda: ["avx2", "bmi2", "fma"])

        monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.embeddings"):
            result = emb._load_llama_class()

        assert result is None, "CPU guard must return None on pre-AVX2 host"
        msgs = " ".join(r.message for r in caplog.records)
        assert any(
            kw in msgs for kw in ("SIGILL", "macOS x86_64", "missing", "avx2")
        ), f"Expected CPU guard warning, got: {msgs}"
    finally:
        # Always clear so this test does not pollute later tests via lru_cache
        emb._load_llama_class.cache_clear()


def test_load_llama_proceeds_past_guard_when_macos_x86_64_capable(monkeypatch, tmp_path):
    """On a capable host (all flags present), the guard must not block loading.

    The guard ``if _macos_flags is not None and _macos_flags:`` must pass through
    when missing flags is [] (empty), so the loader can proceed to the native
    import. We verify the guard itself does not return None; the import may still
    fail for other reasons (fake libs), but that happens AFTER the guard.
    """
    import kiro_crew.embeddings as emb

    emb._load_llama_class.cache_clear()
    try:
        monkeypatch.setattr(emb.sys, "platform", "darwin")
        monkeypatch.setattr(emb.platform, "machine", lambda: "x86_64")

        fake_libs_dir = tmp_path / "libs" / "macos_x86_64"
        fake_libs_dir.mkdir(parents=True)
        for name in emb._REQUIRED_VENDORED_LIBS.get("macos_x86_64", ()):
            (fake_libs_dir / name).write_bytes(b"")

        monkeypatch.setattr(emb, "_VENDOR_DIR", tmp_path / "libs" / "..")
        monkeypatch.setattr(emb, "_LIBS_DIR_NAME", "libs")

        # All flags present — guard must NOT block
        monkeypatch.setattr(emb, "_macos_x86_64_missing_cpu_flags", lambda: [])

        monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)

        # The import of llama_cpp will fail (fake libs), but the guard must not
        # have returned None before reaching the import.  We check this by
        # injecting a fake Llama class so the import succeeds.
        import types

        fake_llama = types.ModuleType("llama_cpp")
        fake_llama.Llama = object  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "llama_cpp", fake_llama)
        monkeypatch.setattr(emb, "_harden_llama_null_streams", lambda: None)

        result = emb._load_llama_class()
        assert result is not None, (
            "Guard must not block a capable host (missing flags = []); " f"got: {result}"
        )
    finally:
        emb._load_llama_class.cache_clear()
        sys.modules.pop("llama_cpp", None)


def test_macos_cpu_flags_empty_when_all_present(monkeypatch):
    """When sysctl reports all required flags, result is []."""
    import ctypes
    import ctypes.util

    import kiro_crew.embeddings as emb

    all_flags = b"avx avx2 fma bmi2 f16c sse3 ssse3 sse4.1 sse4.2 aes"

    class _FakeLibc:
        def sysctlbyname(self, name, buf, size, newp, newlen):
            if buf is None:
                # First call (two-call pattern): return required size
                size._obj.value = len(all_flags) + 1
                return 0
            n = min(len(all_flags), size._obj.value - 1)
            ctypes.memmove(buf, all_flags[:n], n)
            return 0

    monkeypatch.setattr(ctypes.util, "find_library", lambda name: "libc.dylib")
    monkeypatch.setattr(ctypes, "CDLL", lambda name: _FakeLibc())

    result = emb._macos_x86_64_missing_cpu_flags()
    assert result == [], f"Expected [] when all flags present, got: {result}"
