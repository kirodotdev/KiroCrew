"""Installing the embedding model's abort flag on a loaded native context.

``install_preempt_flag`` hands llama.cpp a C callback (the C runtime's ``strlen``)
over a two-byte buffer. These tests stand in for the vendored binding with a
module of the same shape, so they run without the native library, but they call
the REAL ``strlen`` through the same ctypes function-pointer type the binding
declares: the flag's arm and clear are read by native code exactly as llama.cpp
reads them between graph nodes.
"""

from __future__ import annotations

import ctypes
import sys
import types

import pytest

import kiro_crew.platform_compat as platform_compat
from kiro_crew.embeddings_preempt import PreemptFlag, install_preempt_flag

# The binding's declaration: typedef bool (*ggml_abort_callback)(void * data);
_ABORT_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)


class _Ctx:
    def __init__(self) -> None:
        self.ctx = object()  # a live native context handle


class _Llm:
    def __init__(self) -> None:
        self._ctx = _Ctx()


class _NoAttrsLlm:
    __slots__ = ("_ctx",)

    def __init__(self) -> None:
        self._ctx = _Ctx()


@pytest.fixture
def binding(monkeypatch):
    """A stand-in for the vendored ``llama_cpp.llama_cpp`` that records the install."""
    calls: list[tuple[object, object, object]] = []
    native = types.SimpleNamespace(
        ggml_abort_callback=_ABORT_CALLBACK,
        llama_set_abort_callback=lambda ctx, cb, data: calls.append((ctx, cb, data)),
    )
    package = types.ModuleType("llama_cpp")
    package.llama_cpp = native  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", package)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", native)
    return native, calls


def test_the_installed_callback_reads_the_flag_through_native_strlen(binding):
    native, calls = binding
    llm = _Llm()
    flag = install_preempt_flag(llm)
    assert isinstance(flag, PreemptFlag)
    assert getattr(llm, "kiro_crew_preempt") is flag
    [(ctx, callback, data)] = calls
    assert ctx is llm._ctx.ctx
    assert callback is flag._callback
    # What llama.cpp does between graph nodes: call the C function on the buffer.
    assert callback(data) is False
    flag.arm()
    assert flag.fired() and callback(data) is True
    flag.clear()
    assert not flag.fired() and callback(data) is False


def test_no_flag_without_a_native_context():
    assert install_preempt_flag(types.SimpleNamespace(_ctx=types.SimpleNamespace(ctx=None))) is None


def test_no_flag_when_the_binding_does_not_import(monkeypatch):
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    assert install_preempt_flag(_Llm()) is None


def test_no_flag_when_the_binding_has_no_abort_setter(binding):
    native, _calls = binding
    native.llama_set_abort_callback = None
    assert install_preempt_flag(_Llm()) is None


def test_no_flag_when_strlen_cannot_be_resolved(binding, monkeypatch):
    monkeypatch.setattr(platform_compat, "c_strlen_address", lambda: 0)
    _native, calls = binding
    assert install_preempt_flag(_Llm()) is None
    assert calls == []


def test_a_failing_install_keeps_the_model_and_returns_none(binding):
    native, _calls = binding

    def refuse(ctx, cb, data):
        raise OSError("no such symbol")

    native.llama_set_abort_callback = refuse
    assert install_preempt_flag(_Llm()) is None


def test_a_model_that_takes_no_attributes_gets_no_flag(binding):
    _native, calls = binding
    assert install_preempt_flag(_NoAttrsLlm()) is None
    assert calls == [], "native code must never hold a buffer nothing keeps alive"
