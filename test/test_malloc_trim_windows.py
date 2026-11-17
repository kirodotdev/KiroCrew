"""``_malloc_trim`` is a plain no-op where there is no glibc."""

from __future__ import annotations

import ctypes
from unittest import mock

import pytest

from kiro_crew.platform_compat_metrics import _malloc_trim

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def test_malloc_trim_returns_false_when_cdll_none_raises_type_error():
    # Windows: ``ctypes.CDLL(None)`` raises TypeError, not OSError.
    err = TypeError("argument of type 'NoneType' is not iterable")
    with mock.patch.object(ctypes, "CDLL", side_effect=err):
        assert _malloc_trim() is False
