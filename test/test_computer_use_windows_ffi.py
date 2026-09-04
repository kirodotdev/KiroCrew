"""``computer_use.windows_ffi`` — the fail-closed secure read and the walk bounds.

Runs on EVERY platform: the native surface is reached only through module-level
seams these tests monkeypatch, so a Linux or macOS shard exercises the same
decision logic that runs on Windows. That matters because the branches with the
worst consequences are the ones a live probe cannot produce on demand — a
provider that does not implement ``IsPassword`` at all, or a read that fails.

Two properties are pinned here:

* **:func:`is_secure_element` fails CLOSED.** Three floors key off it (value
  redaction, keyboard-input refusal, whole-window screenshot suppression), so
  only an explicit, supported, live ``VARIANT_BOOL`` False may read as plain.
  Every other outcome — an error, the reserved not-supported sentinel, a
  surprising VARIANT type, an exception — must mean secure. A live probe on a
  real password box proves the happy path; these prove the rest.
* **:func:`walk_bounded` honours its budgets AND announces what it dropped.** A
  walk that silently truncated let the secure scan reflect only the nodes it
  reached, so a password field past the cut got the whole window captured.
"""

from __future__ import annotations

import ctypes
import inspect
from contextlib import contextmanager

import pytest

from kiro_crew.computer_use import windows_ffi as ffi


@contextmanager
def _null_scope():
    """A no-op stand-in for ``dpi_awareness_scope`` in tests that fake user32."""
    yield


class _FakeElement:
    """Stands in for a COM element pointer. Truthy, and never dereferenced."""

    def __init__(self, name: str = "elem") -> None:
        self.name = name

    def __bool__(self) -> bool:
        return True


class TestIsSecureElementFailsClosed:
    """The decision table, one test per row.

    Parametrized over the *reason* a read is inconclusive rather than over raw
    tuples, so a failure names which fail-closed guarantee broke.
    """

    def test_an_explicit_supported_false_is_the_only_plain_answer(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "prop_value_ex", lambda elem, pid: (ffi.S_OK, ffi.VT_BOOL, False))
        assert ffi.is_secure_element(_FakeElement()) is False

    def test_an_explicit_true_is_secure(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "prop_value_ex", lambda elem, pid: (ffi.S_OK, ffi.VT_BOOL, True))
        assert ffi.is_secure_element(_FakeElement()) is True

    def test_a_failed_read_is_secure(self, monkeypatch) -> None:
        """WPF's ``PasswordBox`` answers a value read with a managed failure.

        An error means "I do not know", and not-knowing must never render a
        password box's value.
        """
        monkeypatch.setattr(
            ffi, "prop_value_ex", lambda elem, pid: (0x80131509, ffi.VT_EMPTY, None)
        )
        assert ffi.is_secure_element(_FakeElement()) is True

    def test_the_reserved_not_supported_sentinel_is_secure(self, monkeypatch) -> None:
        """The branch that exists because a plain read CANNOT see this case.

        ``GetCurrentPropertyValue`` folds "the provider does not implement this"
        into the property's default of False, byte-identically to a real False.
        A password box in such a framework would read as safe, so the ``Ex`` form
        is used and its ``VT_UNKNOWN`` sentinel means secure.
        """
        monkeypatch.setattr(
            ffi, "prop_value_ex", lambda elem, pid: (ffi.S_OK, ffi.VT_UNKNOWN, 0x7FFD00000000)
        )
        assert ffi.is_secure_element(_FakeElement()) is True

    @pytest.mark.parametrize(
        ("vt", "value"),
        [
            (ffi.VT_BSTR, ""),
            (ffi.VT_BSTR, "Search sessions"),
            (ffi.VT_I4, 0),
            (ffi.VT_EMPTY, None),
        ],
    )
    def test_a_non_boolean_type_is_secure(self, monkeypatch, vt: int, value: object) -> None:
        """A string here is the signature of reading the WRONG property id.

        Property id 30097 returns a ``VT_BSTR`` and was once named as the secure
        flag. The empty-string case is the dangerous one: it is falsy, so a
        truthiness test would report a real password box as plain.
        """
        monkeypatch.setattr(ffi, "prop_value_ex", lambda elem, pid: (ffi.S_OK, vt, value))
        assert ffi.is_secure_element(_FakeElement()) is True

    def test_a_raising_read_is_secure_rather_than_propagating(self, monkeypatch) -> None:
        """The driver contract is never to raise; the floor is to fail closed."""

        def boom(elem, pid):
            raise OSError("COM call failed")

        monkeypatch.setattr(ffi, "prop_value_ex", boom)
        assert ffi.is_secure_element(_FakeElement()) is True

    def test_it_reads_the_correct_property_id(self, monkeypatch) -> None:
        """30019, not 30097.

        Asserted structurally because both ids return ``S_OK``: nothing about a
        wrong id looks like an error at runtime.
        """
        seen: list[int] = []

        def record(elem, pid):
            seen.append(pid)
            return ffi.S_OK, ffi.VT_BOOL, False

        monkeypatch.setattr(ffi, "prop_value_ex", record)
        ffi.is_secure_element(_FakeElement())
        assert seen == [30019]
        assert ffi.UIA_IsPasswordPropertyId == 30019

    def test_the_secure_flag_is_not_a_cached_walk_property(self) -> None:
        """A cached secure verdict goes stale while the cache holds cleartext.

        A field can flip plain to password on the application's own timer, so the
        flag is read live per element and must not appear in the walk's cache
        request — otherwise the walk reports False over a plaintext value.
        """
        assert ffi.UIA_IsPasswordPropertyId not in ffi.CACHED_WALK_PROPERTIES


class TestVariantDecoding:
    def test_variant_bool_true_is_minus_one_not_one(self) -> None:
        """``VARIANT_BOOL`` TRUE is -1, so a ``== 1`` test would read it as False."""
        var = ffi.VARIANT()
        var.vt = ffi.VT_BOOL
        var.u.boolVal = -1
        assert ffi.variant_value(var) is True

    def test_variant_bool_false_is_zero(self) -> None:
        var = ffi.VARIANT()
        var.vt = ffi.VT_BOOL
        var.u.boolVal = 0
        assert ffi.variant_value(var) is False

    def test_the_x64_layout_is_twenty_four_bytes_with_the_union_at_eight(self) -> None:
        """A shorter declaration lets the callee write past the end of the object.

        Pinned as a number rather than trusted: a wrong offset reports a
        plausible-looking type tag for every property read, which is not
        distinguishable from a real answer at the call site.
        """
        import ctypes

        assert ctypes.sizeof(ffi.VARIANT) == 24
        assert ffi.VARIANT.u.offset == 8

    @pytest.mark.parametrize("vt", [ffi.VT_EMPTY, ffi.VT_NULL])
    def test_an_empty_variant_decodes_to_none(self, vt: int) -> None:
        var = ffi.VARIANT()
        var.vt = vt
        assert ffi.variant_value(var) is None


class TestVtableTableIsTheOnlySourceOfSlotIndices:
    def test_element_from_handle_is_slot_six_and_from_point_is_seven(self) -> None:
        """The neighbour that returns S_OK and the whole DESKTOP.

        Handed an HWND, ``ElementFromPoint`` reads it as a screen coordinate and
        resolves to the desktop root — so a window-scoped walk would silently read
        every application's tree. Both entries are pinned so a transposition is a
        test failure rather than a disclosure.
        """
        assert ffi._VTABLE[("IUIAutomation", "ElementFromHandle")][0] == 6
        assert ffi._VTABLE[("IUIAutomation", "ElementFromPoint")][0] == 7

    def test_the_secure_accessor_slot_is_pinned(self) -> None:
        assert ffi._VTABLE[("IUIAutomationElement", "get_CurrentIsPassword")][0] == 35

    def test_every_row_declares_a_restype_and_argtypes(self) -> None:
        """A missing signature truncates a 64-bit pointer and segfaults."""
        for key, (slot, restype, argtypes) in ffi._VTABLE.items():
            assert isinstance(slot, int) and slot >= 0, key
            assert restype is not None, key
            assert isinstance(argtypes, list), key

    def test_vcall_refuses_an_unknown_method_rather_than_guessing(self) -> None:
        with pytest.raises(Exception):
            ffi.vcall(_FakeElement(), "IUIAutomation", "NoSuchMethod")

    def test_every_flat_function_row_declares_both_halves(self) -> None:
        for lib_key, symbol, restype, argtypes in ffi._FN_SPECS:
            assert lib_key in ffi._LIB_NAMES, symbol
            assert argtypes is not None, symbol
            assert isinstance(argtypes, list), symbol

    def test_bind_raises_when_argtypes_is_missing(self) -> None:
        """The tripwire: a row without argtypes must be refused, not defaulted."""

        class _Lib:
            def __getattr__(self, name: str):
                raise AssertionError("must not reach the symbol")

        with pytest.raises(Exception):
            ffi._bind(_Lib(), "AnySymbol", None, None)


class TestAvailabilityIsPlatformGuarded:
    def test_available_is_false_off_windows_without_raising(self, monkeypatch) -> None:
        """The dashboard renders a Settings row on a host with no driver at all."""
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        ffi.reset_libraries()
        try:
            assert ffi.available() is False
        finally:
            ffi.reset_libraries()

    def test_the_loader_raises_a_typed_error_off_windows(self, monkeypatch) -> None:
        """A typed refusal is what the backend converts into a tool refusal.

        A bare ``OSError`` from the loader would escape as an internal error
        instead.
        """
        from kiro_crew import platform_compat
        from kiro_crew.computer_use.types import ComputerUseUnsupported

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        ffi.reset_libraries()
        try:
            with pytest.raises(ComputerUseUnsupported):
                ffi.libraries()
        finally:
            ffi.reset_libraries()


class TestWalkBoundedHonoursItsBudgets:
    """The walk is what makes Windows usable, and what must not lie about it.

    ``FindAllBuildCache(Subtree)`` ignores a node budget entirely — measured at
    2966 nodes / 13.7s on a real Chrome window against 630ms for the shipped
    1200-node budget here. These tests drive the traversal through a fake child
    reader so the bounds logic is checked without a desktop.
    """

    @staticmethod
    def _tree(monkeypatch, fanout: int, depth_limit: int) -> None:
        """Install a synthetic infinite-ish tree: every node has *fanout* children."""

        def children(parent, cache, limit, scratch):
            # (children, capped, failed) — a genuine leaf did not FAIL.
            level = getattr(parent, "level", 0)
            if level >= depth_limit:
                return [], False, False
            kids = []
            for i in range(min(fanout, limit)):
                child = _FakeElement(f"{parent.name}.{i}")
                child.level = level + 1  # type: ignore[attr-defined]
                kids.append(child)
            return kids, fanout >= limit, False

        monkeypatch.setattr(ffi, "_cached_children", children)
        monkeypatch.setattr(ffi, "release_all", lambda ptrs: None)
        monkeypatch.setattr(ffi, "release", lambda ptr: None)

    def test_the_node_budget_is_never_exceeded(self, monkeypatch) -> None:
        self._tree(monkeypatch, fanout=4, depth_limit=99)
        root = _FakeElement("root")
        result = ffi.walk_bounded(root, None, max_nodes=50, max_depth=64, max_children_per_node=512)
        assert len(result.elements) <= 50
        assert result.truncated is True

    def test_a_tree_inside_the_budget_is_not_reported_truncated(self, monkeypatch) -> None:
        """A false positive costs a suppressed screenshot on every small window."""
        self._tree(monkeypatch, fanout=2, depth_limit=2)
        root = _FakeElement("root")
        result = ffi.walk_bounded(
            root, None, max_nodes=500, max_depth=64, max_children_per_node=512
        )
        # 2 children + 4 grandchildren.
        assert len(result.elements) == 6
        assert result.truncated is False
        assert result.depth_truncated is False

    def test_a_depth_cut_sets_depth_truncated(self, monkeypatch) -> None:
        """There is more tree below, and the capture gate has to know."""
        self._tree(monkeypatch, fanout=2, depth_limit=99)
        root = _FakeElement("root")
        result = ffi.walk_bounded(
            root, None, max_nodes=10_000, max_depth=3, max_children_per_node=512
        )
        assert result.depth_truncated is True
        assert result.truncated is True

    def test_a_capped_child_read_sets_truncated(self, monkeypatch) -> None:
        """The tail must never be dropped silently.

        A capped read that stayed quiet let the secure scan reflect only the first
        N children, so a password field as child N+1 got the whole window
        captured.
        """
        self._tree(monkeypatch, fanout=64, depth_limit=1)
        root = _FakeElement("root")
        result = ffi.walk_bounded(
            root, None, max_nodes=10_000, max_depth=64, max_children_per_node=8
        )
        assert result.truncated is True

    def test_a_wedged_provider_hits_the_time_deadline(self, monkeypatch) -> None:
        """The node budget bounds the CALL COUNT, not the duration.

        Per-node cost was measured from ~2ms (UWP) to ~20ms (WPF), so a provider an
        order of magnitude slower than WPF would park a POOLED worker — shared with
        chat and terminal work — for minutes while no single call is slow enough to
        look wrong. ``MAX_WALK_SECS`` is the only thing that bounds the total.

        Driven by a fake clock rather than a real sleep: a timing test that waits is
        both slow and flaky, and the property under test is "the deadline is
        consulted and honoured", which a monotonic stub asserts exactly.
        """
        self._tree(monkeypatch, fanout=4, depth_limit=99)
        ticks = iter([0.0] + [ffi.MAX_WALK_SECS + 1.0] * 10_000)
        monkeypatch.setattr(ffi.time, "monotonic", lambda: next(ticks))
        root = _FakeElement("root")
        result = ffi.walk_bounded(
            root, None, max_nodes=10_000, max_depth=64, max_children_per_node=512
        )
        assert result.time_truncated is True
        # Also ``truncated``: the tree really is incomplete, so the screenshot gate
        # must refuse rather than treating "no secure field seen" as "none present".
        assert result.truncated is True

    def test_a_fast_walk_is_not_time_truncated(self, monkeypatch) -> None:
        """A false positive here would suppress every screenshot.

        A healthy window has ~16x headroom (a real Chrome window reached the shipped
        1200-node budget in 630ms), so the deadline must never fire on one.
        """
        self._tree(monkeypatch, fanout=2, depth_limit=2)
        root = _FakeElement("root")
        result = ffi.walk_bounded(
            root, None, max_nodes=500, max_depth=64, max_children_per_node=512
        )
        assert result.time_truncated is False
        assert result.truncated is False

    def test_depths_are_reported_per_element(self, monkeypatch) -> None:
        self._tree(monkeypatch, fanout=2, depth_limit=2)
        root = _FakeElement("root")
        result = ffi.walk_bounded(
            root, None, max_nodes=500, max_depth=64, max_children_per_node=512
        )
        assert len(result.depths) == len(result.elements)
        assert min(result.depths) == 1
        assert max(result.depths) == 2

    def test_a_provider_failure_marks_the_walk_truncated(self, monkeypatch) -> None:
        """A subtree the provider would not read is UNSCANNED, not empty.

        The distinction is the whole point of the third return value: a failed
        child read that reported "no children AND nothing cut off" would let a
        subtree that was never scanned for a password field pass the screenshot
        gate. A Chromium/WPF pane mid-relayout or an elevated child under UIPI
        produces exactly this.
        """

        def failing(parent, cache, limit, scratch):
            # Root yields one child; that child's read FAILS.
            if getattr(parent, "name", "") == "root":
                child = _FakeElement("root.0")
                return [child], False, False
            return [], False, True

        monkeypatch.setattr(ffi, "_cached_children", failing)
        monkeypatch.setattr(ffi, "release_all", lambda ptrs: None)
        monkeypatch.setattr(ffi, "release", lambda ptr: None)
        result = ffi.walk_bounded(
            _FakeElement("root"), None, max_nodes=500, max_depth=64, max_children_per_node=512
        )
        assert result.truncated is True

    def test_a_leaf_is_not_a_failure(self, monkeypatch) -> None:
        """A clean leaf (empty children, no failure) must NOT flag the walk.

        The mirror of the test above: over-flagging every leaf would suppress the
        screenshot of every window, since every walk ends in leaves.
        """
        self._tree(monkeypatch, fanout=2, depth_limit=1)
        result = ffi.walk_bounded(
            _FakeElement("root"), None, max_nodes=500, max_depth=64, max_children_per_node=512
        )
        assert result.truncated is False
        assert result.depth_truncated is False

    def test_the_traversal_is_iterative(self) -> None:
        """A pathological tree must not RecursionError inside a ctypes call.

        Asserted structurally: ``walk_bounded`` must not call itself.

        The whole MODULE is parsed and the function located by name in the AST,
        rather than reading ``inspect.getsource(ffi.walk_bounded)``. That helper
        slices the file by the code object's line number, which goes stale against
        ``linecache`` when the module is edited in a long-lived session — it returned
        a two-line fragment mid-suite and failed with a ``SyntaxError`` that named
        neither this test's subject nor its real cause.
        """
        import ast
        import pathlib

        source = pathlib.Path(ffi.__file__).read_text(encoding="utf-8")
        module = ast.parse(source)
        target = next(
            (
                node
                for node in module.body
                if isinstance(node, ast.FunctionDef) and node.name == "walk_bounded"
            ),
            None,
        )
        assert target is not None, "walk_bounded is no longer a module-level function"
        called = {
            node.func.id
            for node in ast.walk(target)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "walk_bounded" not in called


class TestCachedChildrenDistinguishesALeafFromAFailure:
    """``(children, capped, failed)`` — the third value must not be inferable.

    A failed read reporting "no children AND nothing cut off" is byte-identical to a
    genuine leaf, so ``walk_bounded`` would report ``truncated=False`` over a subtree
    it never scanned, and the screenshot gate would pass a window whose password
    field sat inside it.
    """

    @staticmethod
    def _plumbing(monkeypatch, *, hresults: dict) -> None:
        """Drive ``_cached_children`` through a fake vtable.

        *hresults* maps a method name to the HRESULT it returns; anything absent
        succeeds. A failing ``get_Length`` leaves its out-param UNTOUCHED, which is
        what a real failing call does — and why an unchecked return reads the
        ``c_int``'s zero-initialized value as a real count of zero.
        """

        def vcall(ptr, iface, method):
            def call(*args):
                code = hresults.get(method, ffi.S_OK)
                if code == ffi.S_OK and method == "BuildUpdatedCache":
                    args[-1]._obj.value = 0x1000  # a non-NULL updated element
                if code == ffi.S_OK and method == "GetCachedChildren":
                    args[-1]._obj.value = 0x2000  # a non-NULL child array
                return code

            return call

        monkeypatch.setattr(ffi, "vcall", vcall)
        monkeypatch.setattr(ffi, "release", lambda ptr: None)

    def test_a_failed_length_read_is_reported_as_failed(self, monkeypatch) -> None:
        """**The defect this test exists for.** ``E_FAIL`` from ``get_Length``.

        A fresh ``ctypes.c_int`` is 0, so dropping this HRESULT yields
        ``([], False, False)`` — the genuine-leaf tuple — and an unscanned subtree
        passes the screenshot gate silently.
        """
        self._plumbing(monkeypatch, hresults={"get_Length": -2147467259})
        children, _capped, failed = ffi._cached_children(_FakeElement(), None, 512, [])
        assert children == []
        assert failed is True, "a failed length read must not look like a leaf"

    @pytest.mark.parametrize(
        "method", ["BuildUpdatedCache", "GetCachedChildren"], ids=["cache", "children"]
    )
    def test_a_failed_child_read_is_reported_as_failed(self, monkeypatch, method: str) -> None:
        """The two failures a Chromium/WPF pane produces mid-relayout."""
        self._plumbing(monkeypatch, hresults={method: -2147467259})
        _children, _capped, failed = ffi._cached_children(_FakeElement(), None, 512, [])
        assert failed is True

    def test_a_failed_PER_CHILD_read_is_reported_as_failed(self, monkeypatch) -> None:
        """**The screenshot-suppression bypass this test exists for.**

        ``get_Length`` said N children; a failing ``GetElement`` skips one and the
        list comes back short. Answering ``failed=False`` there asserts "these are all
        the children" about a list missing one — and if the missing node was a
        password field, the secure scan never sees it, ``has_secure`` stays False, and
        the window is captured with the rendered credential in frame.
        """
        calls: list[int] = []

        def vcall(ptr, iface, method):
            def call(*args):
                if method == "BuildUpdatedCache":
                    args[-1]._obj.value = 0x1000
                    return ffi.S_OK
                if method == "GetCachedChildren":
                    args[-1]._obj.value = 0x2000
                    return ffi.S_OK
                if method == "get_Length":
                    args[-1]._obj.value = 3
                    return ffi.S_OK
                if method == "GetElement":
                    index = args[1]
                    calls.append(index)
                    if index == 1:  # the middle child fails
                        return -2147467259
                    args[-1]._obj.value = 0x3000 + index
                    return ffi.S_OK
                return ffi.S_OK

            return call

        monkeypatch.setattr(ffi, "vcall", vcall)
        monkeypatch.setattr(ffi, "release", lambda p: None)
        children, _capped, failed = ffi._cached_children(_FakeElement(), None, 512, [])
        assert calls == [0, 1, 2], "every child must still be attempted"
        assert len(children) == 2, "the failed child is not in the list"
        assert failed is True, "a short child list must not report success"

    def test_a_genuine_leaf_is_not_a_failure(self, monkeypatch) -> None:
        """The call SUCCEEDED and reported no child array: the normal terminal case.

        Over-flagging it would suppress the screenshot of every window, since every
        walk ends in leaves.
        """

        def vcall(ptr, iface, method):
            def call(*args):
                if method == "BuildUpdatedCache":
                    args[-1]._obj.value = 0x1000
                # GetCachedChildren succeeds and leaves the array NULL.
                return ffi.S_OK

            return call

        monkeypatch.setattr(ffi, "vcall", vcall)
        monkeypatch.setattr(ffi, "release", lambda ptr: None)
        assert ffi._cached_children(_FakeElement(), None, 512, []) == ([], False, False)


class TestSafearrayRankIsChecked:
    """``rgIndices`` needs one LONG per DIMENSION."""

    def test_a_multi_dimensional_array_is_refused(self, monkeypatch) -> None:
        """A single ``c_long`` handed to a 2-D array is read as 8 bytes from 4.

        The callee reads 4 bytes past the object into adjacent heap and uses it as
        the second index. The bounds calls cannot catch this: they are asked about
        dimension 1 only and both return ``S_OK`` on a 2-D array, so they bound the
        element COUNT while the RANK is what is wrong.
        """

        class _Oleaut:
            @staticmethod
            def SafeArrayGetDim(arr):
                return 2

            @staticmethod
            def SafeArrayGetElement(*args):  # pragma: no cover - must not be reached
                raise AssertionError("a 2-D array must be refused before any element read")

        monkeypatch.setattr(ffi, "_libraries", lambda: {"oleaut32": _Oleaut()})
        assert ffi._safearray_doubles(_FakeElement()) is None

    def test_a_one_dimensional_array_is_decoded(self, monkeypatch) -> None:
        values = [10.0, 20.0, 300.0, 40.0]

        class _Oleaut:
            @staticmethod
            def SafeArrayGetDim(arr):
                return 1

            @staticmethod
            def SafeArrayGetLBound(arr, dim, out):
                out._obj.value = 0
                return ffi.S_OK

            @staticmethod
            def SafeArrayGetUBound(arr, dim, out):
                out._obj.value = len(values) - 1
                return ffi.S_OK

            @staticmethod
            def SafeArrayGetElement(arr, index, out):
                out._obj.value = values[index._obj.value]
                return ffi.S_OK

        monkeypatch.setattr(ffi, "_libraries", lambda: {"oleaut32": _Oleaut()})
        assert ffi._safearray_doubles(_FakeElement()) == values


class TestClientLifetimeIsGenerationStamped:
    """A released ``c_void_p`` keeps its address, so truthiness cannot detect it."""

    def test_a_released_client_is_not_handed_back(self, monkeypatch) -> None:
        """**The reproduced use-after-free.**

        ``release`` does not alter the pointer, so a worker's cached client still
        reads truthy after ``release_all_clients`` and the next call would dispatch
        through a freed vtable — an access violation no ``try`` can catch, taking the
        gateway (cron, Slack, every session) down. The generation counter is the only
        signal that survives the release.
        """
        import ctypes

        monkeypatch.setattr(ffi, "release", lambda ptr: None)
        monkeypatch.setattr(ffi, "_ensure_apartment", lambda: None)
        stale = ctypes.c_void_p(0x1234)
        monkeypatch.setattr(ffi._thread_state, "automation", stale, raising=False)
        monkeypatch.setattr(
            ffi._thread_state, "automation_generation", ffi._clients_generation, raising=False
        )
        monkeypatch.setattr(ffi, "_clients", [stale])

        built: list[int] = []

        class _Ole32:
            @staticmethod
            def CoCreateInstance(*args):
                built.append(1)
                args[-1]._obj.value = 0x5678
                return ffi.S_OK

            @staticmethod
            def CLSIDFromString(text, out):
                return ffi.S_OK

        monkeypatch.setattr(ffi, "_libraries", lambda: {"ole32": _Ole32()})

        ffi.release_all_clients()
        # Still truthy — which is exactly why truthiness cannot be the test.
        assert bool(stale) is True
        client = ffi.automation()
        assert built == [1], "a released client was handed back instead of rebuilt"
        assert client is not stale

    def test_a_live_client_is_reused(self, monkeypatch) -> None:
        """The cache must still be a cache: rebuilding per call is a real cost."""
        import ctypes

        live = ctypes.c_void_p(0x1234)
        monkeypatch.setattr(ffi._thread_state, "automation", live, raising=False)
        monkeypatch.setattr(
            ffi._thread_state, "automation_generation", ffi._clients_generation, raising=False
        )

        def boom():  # pragma: no cover - must not be reached
            raise AssertionError("a live client must be reused, not rebuilt")

        monkeypatch.setattr(ffi, "_libraries", boom)
        assert ffi.automation() is live

    def test_release_without_an_apartment_leaks_rather_than_faulting(self, monkeypatch) -> None:
        """A cross-apartment Release is legal only while the MTA is alive.

        Once the last pooled worker exits, the apartment is torn down and
        ``UIAutomationCore`` can be unloaded, so dispatching ``Release`` through the
        object's vtable reads a slot in an unmapped page. Leaking is recoverable;
        faulting is not.
        """
        import ctypes

        released: list[object] = []
        monkeypatch.setattr(ffi, "release", lambda ptr: released.append(ptr))

        def no_apartment():
            raise OSError("no apartment")

        monkeypatch.setattr(ffi, "_ensure_apartment", no_apartment)
        monkeypatch.setattr(ffi, "_clients", [ctypes.c_void_p(0x1234)])
        before = ffi._clients_generation

        ffi.release_all_clients()

        assert released == [], "Release ran without an apartment to keep the MTA alive"
        # The generation still advances: the caller asked for these clients to stop
        # being used, and a stale stamp is what stops a worker handing one back.
        assert ffi._clients_generation == before + 1


@pytest.mark.skipif(
    not hasattr(ctypes, "WINFUNCTYPE"),
    reason=(
        "window_list builds a stdcall callback with ctypes.WINFUNCTYPE, which exists "
        "only in the Windows ctypes build. The rest of this module is deliberately "
        "cross-platform (its native surface is reached through patchable seams), but a "
        "callback TYPE is constructed at call time and cannot be faked without "
        "replacing the function under test."
    ),
)
class TestWindowListSurfacesACallbackFailure:
    """CPython cannot raise out of a ctypes callback."""

    @staticmethod
    def _reads(monkeypatch, *, title=lambda h: "A Window", exe=lambda pid: "app.exe") -> None:
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        monkeypatch.setattr(ffi, "window_text", title)
        monkeypatch.setattr(ffi, "window_root", lambda h: h)
        monkeypatch.setattr(ffi, "window_pid", lambda h: 42)
        monkeypatch.setattr(ffi, "window_class", lambda h: "Cls")
        monkeypatch.setattr(ffi, "window_bounds", lambda h: (0.0, 0.0, 10.0, 10.0))
        monkeypatch.setattr(ffi, "_exe_name", exe)

    @staticmethod
    def _enum(monkeypatch, handles, *, on_screen=True) -> None:
        class _User32:
            @staticmethod
            def IsWindowVisible(h):
                return True

            @staticmethod
            def IsIconic(h):
                return not on_screen

            @staticmethod
            def EnumWindows(callback, param):
                for handle in handles:
                    callback(handle, 0)
                return 1  # SUCCESS, which is the whole trap

        class _Dwmapi:
            @staticmethod
            def DwmGetWindowAttribute(h, attr, out, size):
                return ffi.S_OK  # not cloaked; ``out`` stays zero

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32(), "dwmapi": _Dwmapi()})

    def test_a_failing_callback_raises_rather_than_returning_a_short_list(
        self, monkeypatch
    ) -> None:
        """**The measured defect: 135 of 270 windows, and ``EnumWindows`` said OK.**

        A partial list reported as success is worse than an error, because
        ``apps_windows.list_apps`` then reports "no applications on screen" for a read
        it could not perform — and names ``computer_list_apps`` as the remedy, the
        call that just lied to the model.
        """

        def boom(pid):
            raise OSError("cannot query an elevated process")

        self._reads(monkeypatch, exe=boom)
        self._enum(monkeypatch, [11])
        with pytest.raises(ffi.ComputerUseUnsupported, match="enumerating windows failed"):
            ffi.window_list()

    def test_a_clean_enumeration_returns_its_windows(self, monkeypatch) -> None:
        self._reads(monkeypatch)
        self._enum(monkeypatch, [11, 12])
        assert [w.hwnd for w in ffi.window_list()] == [11, 12]

    def test_an_untitled_window_is_skipped_without_being_a_failure(self, monkeypatch) -> None:
        """Shell furniture is filtered, not reported as an enumeration error."""
        self._reads(monkeypatch, title=lambda h: "" if h == 11 else "Real")
        self._enum(monkeypatch, [11, 12])
        assert [w.hwnd for w in ffi.window_list()] == [12]


class TestCacheRequestChecksItsHresults:
    """Both settings are load-bearing, and each fails like working code."""

    @pytest.mark.parametrize(
        "method", ["AddProperty", "put_TreeScope", "put_AutomationElementMode"]
    )
    def test_a_failed_setting_raises_rather_than_returning_a_half_built_request(
        self, monkeypatch, method: str
    ) -> None:
        """A dropped ``AddProperty`` makes the whole tree read role=Unknown with no
        frame while still answering ``ok=True``. A dropped
        ``put_AutomationElementMode`` leaves the mode ``None``, which fails every LIVE
        read — so ``is_secure_element`` fails closed on every text node and every
        window's screenshot is suppressed, with no reason surfaced anywhere.
        """
        released: list[object] = []
        monkeypatch.setattr(ffi, "automation", lambda: _FakeElement("auto"))
        monkeypatch.setattr(ffi, "release", lambda ptr: released.append(ptr))

        def vcall(ptr, iface, name):
            def call(*args):
                if name == "CreateCacheRequest":
                    args[-1]._obj.value = 0x9000
                    return ffi.S_OK
                return -2147467259 if name == method else ffi.S_OK

            return call

        monkeypatch.setattr(ffi, "vcall", vcall)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.create_cache_request()
        assert released, "the partly-built request leaked instead of being released"

    def test_a_fully_successful_build_returns_the_request(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "automation", lambda: _FakeElement("auto"))
        monkeypatch.setattr(ffi, "release", lambda ptr: None)

        def vcall(ptr, iface, name):
            def call(*args):
                if name == "CreateCacheRequest":
                    args[-1]._obj.value = 0x9000
                return ffi.S_OK

            return call

        monkeypatch.setattr(ffi, "vcall", vcall)
        assert ffi.create_cache_request().value == 0x9000


class TestDpiScopeRefusesAnUnknownAwareness:
    """A failed set must not silently run the block unaware."""

    def test_a_failed_set_raises(self, monkeypatch) -> None:
        """The worst outcome in the module: both halves agree on the wrong window.

        ``SetThreadDpiAwarenessContext`` returns NULL on failure, the same falsy value
        a successfully-restored default context can marshal as — so the return value
        alone cannot tell "the set failed" from "nothing to restore". Proceeding
        unaware is what made a physical point fed to ``WindowFromPoint`` resolve a
        DIFFERENT APPLICATION, which the confinement check cannot catch because it
        agrees with the click.
        """

        class _User32:
            @staticmethod
            def SetThreadDpiAwarenessContext(ctx):
                return None

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32()})
        monkeypatch.setattr(ffi._ct, "set_last_error", lambda code: None, raising=False)
        monkeypatch.setattr(
            ffi._ct, "get_last_error", lambda: 87, raising=False
        )  # ERROR_INVALID_PARAMETER

        with pytest.raises(ffi.ComputerUseUnsupported, match="SetThreadDpiAwarenessContext"):
            with ffi.dpi_awareness_scope():  # pragma: no cover - body must not run
                raise AssertionError("the block ran under an unknown DPI awareness")

    def test_a_falsy_previous_with_no_error_still_runs_the_block(self, monkeypatch) -> None:
        """NULL with no error means the set SUCCEEDED and there is nothing to restore."""

        class _User32:
            @staticmethod
            def SetThreadDpiAwarenessContext(ctx):
                return None

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32()})
        monkeypatch.setattr(ffi._ct, "set_last_error", lambda code: None, raising=False)
        monkeypatch.setattr(ffi._ct, "get_last_error", lambda: 0, raising=False)

        ran = []
        with ffi.dpi_awareness_scope():
            ran.append(1)
        assert ran == [1]

    def test_the_previous_context_is_always_restored(self, monkeypatch) -> None:
        """The pooled worker is shared with chat, terminal and browse work.

        A leaked PER_MONITOR_AWARE_V2 would make later ``GetWindowRect`` /
        ``GetCursorPos`` calls from unrelated code read physical rather than
        virtualized coordinates.
        """
        calls: list[object] = []

        class _User32:
            @staticmethod
            def SetThreadDpiAwarenessContext(ctx):
                calls.append(ctx)
                return 0x99  # a truthy previous context

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32()})
        monkeypatch.setattr(ffi._ct, "set_last_error", lambda code: None, raising=False)

        with pytest.raises(RuntimeError):
            with ffi.dpi_awareness_scope():
                raise RuntimeError("the caller's own failure")
        # Restored even when the body raised.
        assert calls[-1] == 0x99


class TestWindowIsOnScreen:
    """``IsWindowVisible`` means "not explicitly hidden", NOT "on screen".

    Three states pass it while being invisible to the operator, and each produced a
    window the model could read and photograph but the operator could not see —
    measured on one desktop, 4 of 12 enumerated windows. That is not cosmetic: the
    tool descriptions and the renderer both say "on-screen windows", a cloaked
    window's published origin OVERLAPS other applications' real pixels, and
    confinement's refusal then told the model to re-read bounds that never change,
    so the retry could not terminate. macOS gets this free from
    ``kCGWindowListOptionOnScreenOnly``.
    """

    @staticmethod
    def _libs(monkeypatch, *, iconic=False, cloaked=0, hr=None, bounds=(0.0, 0.0, 800.0, 600.0)):
        class _User32:
            @staticmethod
            def IsIconic(h):
                return iconic

        class _Dwmapi:
            @staticmethod
            def DwmGetWindowAttribute(h, attr, out, size):
                # ``out`` is a byref to a DWORD; write through it as the real call does.
                ctypes.cast(out, ctypes.POINTER(ctypes.c_ulong))[0] = cloaked
                return ffi.S_OK if hr is None else hr

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32(), "dwmapi": _Dwmapi()})
        monkeypatch.setattr(ffi, "window_bounds", lambda h: bounds)

    def test_an_ordinary_window_is_on_screen(self, monkeypatch) -> None:
        self._libs(monkeypatch)
        assert ffi.window_is_on_screen(0x10) is True

    def test_a_MINIMIZED_window_is_not(self, monkeypatch) -> None:
        """Its rect is parked at -32000,-32000, so no click can land inside it."""
        self._libs(monkeypatch, iconic=True)
        assert ffi.window_is_on_screen(0x10) is False

    def test_a_DWM_CLOAKED_window_is_not(self, monkeypatch) -> None:
        """A suspended packaged app, or a window on another virtual desktop. Measured
        live: HP Audio Control reported cloak value 2 while passing IsWindowVisible
        AND IsIconic-false, so neither ordinary check excludes it."""
        self._libs(monkeypatch, cloaked=2)
        assert ffi.window_is_on_screen(0x10) is False

    @pytest.mark.parametrize(
        "bounds",
        [(0.0, 0.0, 0.0, 600.0), (0.0, 0.0, 800.0, 0.0), None],
        ids=["no-width", "no-height", "unreadable"],
    )
    def test_a_zero_area_or_unreadable_rect_is_not(self, monkeypatch, bounds) -> None:
        self._libs(monkeypatch, bounds=bounds)
        assert ffi.window_is_on_screen(0x10) is False

    def test_an_unreadable_cloak_attribute_fails_OPEN(self, monkeypatch) -> None:
        """**Deliberately the permissive direction here.**

        ``DwmGetWindowAttribute`` is absent on a host without DWM composition, and
        treating "cannot ask" as "not on screen" would empty the entire application
        list — the model would be told the desktop has no windows. The ordinary
        ``IsIconic`` and rect reads still apply, and every downstream gesture is
        confined by ``apps_windows.hwnd_owns_point`` regardless.
        """
        self._libs(monkeypatch, hr=-2147024809)  # E_INVALIDARG
        assert ffi.window_is_on_screen(0x10) is True

    def test_a_RAISING_dwm_call_fails_OPEN_too(self, monkeypatch) -> None:
        class _User32:
            @staticmethod
            def IsIconic(h):
                return False

        class _Dwmapi:
            @staticmethod
            def DwmGetWindowAttribute(h, attr, out, size):
                raise OSError("dwmapi unavailable")

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32(), "dwmapi": _Dwmapi()})
        monkeypatch.setattr(ffi, "window_bounds", lambda h: (0.0, 0.0, 800.0, 600.0))
        assert ffi.window_is_on_screen(0x10) is True


class TestWindowRenderScale:
    """How much smaller a window draws itself than its DPI-aware rect.

    This ratio is what sizes a capture buffer. ``PrintWindow`` asks the window to
    draw in ITS OWN coordinate space, so a DPI-unaware window renders at its logical
    size into whatever buffer it is handed and an aware-sized one is left with a
    black margin — an image that no longer maps linearly onto the window rect the
    element frames use. Every failure answers 1.0, which is exactly right for an
    aware window and no worse than an unscaled capture for anything else.
    """

    @staticmethod
    def _libs(monkeypatch, *, window_dpi=96, monitor_dpi=96, monitor=0x500, hr=None):
        class _User32:
            @staticmethod
            def GetDpiForWindow(h):
                return window_dpi

            @staticmethod
            def MonitorFromWindow(h, flags):
                return monitor

        class _Shcore:
            @staticmethod
            def GetDpiForMonitor(mon, kind, dx, dy):
                dx._obj.value = monitor_dpi
                dy._obj.value = monitor_dpi
                return ffi.S_OK if hr is None else hr

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32(), "shcore": _Shcore()})

    @pytest.mark.parametrize(
        ("window_dpi", "monitor_dpi", "expected"),
        [
            (96, 96, 1.0),  # both unscaled
            (120, 120, 1.0),  # a per-monitor-aware app on a 125% display
            (96, 120, 0.8),  # THE case: an unaware window on a 125% display
            (96, 192, 0.5),  # unaware at 200%
            (192, 192, 1.0),  # aware at 200%
        ],
        ids=["unscaled", "aware-125", "unaware-125", "unaware-200", "aware-200"],
    )
    def test_the_ratio_is_the_windows_dpi_over_the_monitors(
        self, monkeypatch, window_dpi: int, monitor_dpi: int, expected: float
    ) -> None:
        """Measured on a 125% monitor: a WinForms window (96 over 120) drew 620x392
        into both a 620x400 buffer and a 775x500 one, while a Chromium window (120
        over 120) filled each buffer it was given."""
        self._libs(monkeypatch, window_dpi=window_dpi, monitor_dpi=monitor_dpi)
        assert ffi.window_render_scale(0x10) == pytest.approx(expected)

    def test_an_unreadable_window_dpi_is_1(self, monkeypatch) -> None:
        self._libs(monkeypatch, window_dpi=0)
        assert ffi.window_render_scale(0x10) == 1.0

    def test_no_monitor_is_1(self, monkeypatch) -> None:
        """A window with no monitor cannot be compared against one."""
        self._libs(monkeypatch, window_dpi=96, monitor=0)
        assert ffi.window_render_scale(0x10) == 1.0

    def test_a_failed_monitor_dpi_query_is_1(self, monkeypatch) -> None:
        self._libs(monkeypatch, window_dpi=96, monitor_dpi=120, hr=-2147024809)
        assert ffi.window_render_scale(0x10) == 1.0

    def test_a_zero_monitor_dpi_is_1_rather_than_a_ZeroDivisionError(self, monkeypatch) -> None:
        """S_OK with a zero out-param is the shape that would divide by zero on the
        observation path, where an exception costs the whole snapshot."""
        self._libs(monkeypatch, window_dpi=96, monitor_dpi=0)
        assert ffi.window_render_scale(0x10) == 1.0


class _FakeLib:
    """A flat DLL whose every symbol is a recorded, scriptable call.

    ``returns`` maps a symbol name to a value or a callable ``(*args) -> value``; an
    unlisted symbol answers ``S_OK``. Out-params are filled by the callable, which is
    how the native shape of each call is reproduced without a real DLL.
    """

    def __init__(self, **returns):
        self.returns = returns
        self.calls: list = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            spec = self.returns.get(name, ffi.S_OK)
            return spec(*args) if callable(spec) else spec

        return call


class TestTheKeyboardRecords:
    """``send_key_chord`` / ``send_text`` build the batches ``SendInput`` takes."""

    @staticmethod
    def _spy(monkeypatch) -> list:
        batches: list = []

        def send(records):
            batches.append([(r.u.ki.wVk, r.u.ki.wScan, r.u.ki.dwFlags) for r in records])
            return len(records)

        monkeypatch.setattr(ffi, "_send", send)
        return batches

    def test_a_chord_presses_modifiers_first_and_releases_in_REVERSE(self, monkeypatch) -> None:
        """Reverse release is not cosmetic: releasing Ctrl before the key can register
        the key as an unmodified press in some targets."""
        batches = self._spy(monkeypatch)
        ffi.send_key_chord("c", ["ctrl", "shift"])
        down, up = batches
        assert [vk for vk, _s, _f in down] == [
            ffi.VK_MODIFIERS["ctrl"],
            ffi.VK_MODIFIERS["shift"],
            ffi.VK_CODES["c"],
        ]
        assert [vk for vk, _s, _f in up] == [
            ffi.VK_CODES["c"],
            ffi.VK_MODIFIERS["shift"],
            ffi.VK_MODIFIERS["ctrl"],
        ]
        assert all(flags & ffi.KEYEVENTF_KEYUP for _vk, _s, flags in up)

    def test_an_EXTENDED_key_carries_the_flag_in_BOTH_directions(self, monkeypatch) -> None:
        """Without it the injected scan code is the numpad twin, so an arrow key only
        moves the caret while NumLock happens to be off. A down carrying the flag and an
        up without it leaves the key logically held."""
        batches = self._spy(monkeypatch)
        ffi.send_key_chord("arrowleft", [])
        for batch in batches:
            assert all(flags & ffi.KEYEVENTF_EXTENDEDKEY for _vk, _s, flags in batch)

    def test_an_ordinary_key_does_NOT_carry_the_extended_flag(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        ffi.send_key_chord("a", [])
        for batch in batches:
            assert not any(flags & ffi.KEYEVENTF_EXTENDEDKEY for _vk, _s, flags in batch)

    def test_modifiers_are_RELEASED_even_when_the_press_is_rejected(self, monkeypatch) -> None:
        """A modifier left down is a stuck Ctrl on the operator's own keyboard, which
        changes every keystroke they type next."""
        batches: list = []

        def send(records):
            batches.append([(r.u.ki.wVk, r.u.ki.dwFlags) for r in records])
            return 0 if len(batches) == 1 else len(records)

        monkeypatch.setattr(ffi, "_send", send)
        ffi.send_key_chord("c", ["ctrl"])
        assert len(batches) == 2, "the release batch was not submitted"
        released = {vk for vk, fl in batches[1] if fl & ffi.KEYEVENTF_KEYUP}
        assert ffi.VK_MODIFIERS["ctrl"] in released

    @pytest.mark.parametrize(
        ("key", "mods"),
        [("definitely-not-a-key", []), ("a", ["hyper"]), ("a", ["fn"])],
        ids=["unknown-key", "unknown-modifier", "fn-has-no-vk"],
    )
    def test_an_unmappable_spec_RAISES_rather_than_dropping_it(
        self, monkeypatch, key: str, mods: list
    ) -> None:
        """A silently dropped modifier sends a DIFFERENT chord than the caller asked
        for. ``fn`` is the interesting one: it is a real modifier name with no virtual-key
        code at all, because Windows handles it in keyboard firmware."""
        sent = self._spy(monkeypatch)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.send_key_chord(key, mods)
        assert sent == []

    def test_text_becomes_UNICODE_records_two_per_code_unit(self, monkeypatch) -> None:
        """Layout-independent, unlike a per-character VK lookup which would type the
        wrong character on a non-US layout."""
        batches = self._spy(monkeypatch)
        assert ffi.send_text("hi") == 2
        (batch,) = batches
        assert len(batch) == 4  # two chars, down + up each
        assert all(flags & ffi.KEYEVENTF_UNICODE for _vk, _s, flags in batch)
        assert [scan for _vk, scan, _f in batch] == [ord("h"), ord("h"), ord("i"), ord("i")]

    def test_a_NON_BMP_character_is_two_code_units(self, monkeypatch) -> None:
        """An emoji is a surrogate PAIR, so it costs four records rather than two."""
        batches = self._spy(monkeypatch)
        assert ffi.send_text("\U0001f600") == 1
        (batch,) = batches
        assert len(batch) == 4, "a surrogate pair needs two code units"

    def test_empty_text_sends_nothing(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        assert ffi.send_text("") == 0
        assert batches == []

    def test_a_SHORT_accept_counts_whole_characters_only(self, monkeypatch) -> None:
        """A character half-written is not written. Dividing records by two reports an
        emoji-plus-``a`` as complete when only the emoji landed."""
        monkeypatch.setattr(ffi, "_send", lambda records: 4)
        assert ffi.send_text("\U0001f600a") == 1
        monkeypatch.setattr(ffi, "_send", lambda records: 2)
        assert ffi.send_text("abc") == 1


class TestReleaseBatchesAreVERIFIED:
    """A release is the one batch whose acceptance cannot be assumed.

    ``SendInput`` returns how many records it accepted and can stop early — a
    lower-level hook, or UIPI — and it returns NORMALLY. Submitting the release is
    therefore not enough: a discarded count leaves a modifier or a mouse button HELD
    on the operator's own machine, where a stuck Ctrl alters every keystroke they type
    next and a stuck button turns every motion of their hand into a drag.
    """

    @staticmethod
    def _spy(monkeypatch, accepts) -> list:
        """Install a ``_send`` whose accepted count follows *accepts* per call."""
        seen: list = []
        counts = list(accepts)

        def send(records):
            seen.append(len(records))
            want = counts.pop(0) if counts else len(records)
            return len(records) if want is None else want

        monkeypatch.setattr(ffi, "_send", send)
        return seen

    def test_a_fully_accepted_release_sends_once(self, monkeypatch) -> None:
        seen = self._spy(monkeypatch, [None])
        ffi._send_release([ffi._key_event(0x11, 0, ffi.KEYEVENTF_KEYUP)])
        assert seen == [1], "a clean release must not be retried"

    def test_an_unaccepted_TAIL_is_resubmitted(self, monkeypatch) -> None:
        """``SendInput`` accepts a PREFIX, so only ``records[accepted:]`` is resent.

        Resending the whole batch would re-release keys already up — harmless, but it
        muddies which record actually failed.
        """
        seen = self._spy(monkeypatch, [1, None])
        ups = [ffi._key_event(vk, 0, ffi.KEYEVENTF_KEYUP) for vk in (0x41, 0x10, 0x11)]
        ffi._send_release(ups)
        assert seen == [3, 2], "the retry must carry only the unaccepted tail"

    def test_an_ATOMIC_release_resubmits_the_WHOLE_batch(self, monkeypatch) -> None:
        """A drag's release is ``[move(end), up]`` and the two records are not independent.

        The move is what AIMS the up. A tail-only retry after a partial acceptance resends
        the bare ``up``, which carries no coordinate and therefore releases wherever the
        cursor happens to be — the drop lands outside the window ``hwnd_owns_point``
        authorized, which is exactly the defect the aimed release exists to prevent.
        Resending the move first is idempotent (a cursor position is state, not an edge).
        """
        seen = self._spy(monkeypatch, [1, None])
        # Opaque stand-ins rather than real records: ``_move_to`` normalizes against the
        # virtual screen through user32, which does not exist on a Linux shard, and
        # ``_send_release`` never inspects a record's contents.
        release = ["move(end)", "button_up"]
        ffi._send_release(release, atomic=True)
        assert seen == [2, 2], "the retry must carry the AIMING move, not just the button up"

    def test_a_NON_atomic_release_still_sends_only_the_tail(self, monkeypatch) -> None:
        # The default is unchanged: for independent key-ups, resending an already-released
        # key is harmless but muddies which record failed.
        seen = self._spy(monkeypatch, [1, None])
        ffi._send_release(["key_up_a", "key_up_shift"])
        assert seen == [2, 1]

    def test_the_DRAG_release_is_sent_atomically(self, monkeypatch) -> None:
        # The wiring, pinned. ``atomic`` defaults to False, so a drag that forgot to pass it
        # would silently keep the unaimed-retry defect with nothing going red.
        source = inspect.getsource(ffi.post_mouse_drag)
        assert "_send_release(release, atomic=True)" in source

    def test_a_release_that_never_lands_RAISES(self, monkeypatch) -> None:
        """Reporting success over a stuck modifier is what turns a failed keystroke
        into corrupted input, so this must not pass silently."""
        seen = self._spy(monkeypatch, [0, 0, 0])
        with pytest.raises(ffi.ComputerUseUnsupported, match="still be held"):
            ffi._send_release([ffi._key_event(0x11, 0, ffi.KEYEVENTF_KEYUP)])
        assert len(seen) == ffi._RELEASE_ATTEMPTS, "the retry budget must be bounded"

    def test_the_retry_budget_is_BOUNDED(self, monkeypatch) -> None:
        """An unbounded loop against a permanently-blocking hook would spin a pooled
        gateway worker forever."""
        assert 1 < ffi._RELEASE_ATTEMPTS <= 10

    def test_an_empty_release_is_a_no_op(self, monkeypatch) -> None:
        seen = self._spy(monkeypatch, [])
        ffi._send_release([])
        assert seen == []

    def test_a_CHORD_release_is_verified_not_merely_submitted(self, monkeypatch) -> None:
        """**The defect this class exists for.**

        ``send_key_chord`` released in a ``finally`` — which covers an exception but
        not a truncated release batch. A rejected modifier key-up left Ctrl held while
        the call reported success.
        """
        calls: list = []

        def send(records):
            calls.append([r.u.ki.wVk for r in records])
            # Reject the release batch outright, every time.
            return 0 if len(calls) > 1 else len(records)

        monkeypatch.setattr(ffi, "_send", send)
        with pytest.raises(ffi.ComputerUseUnsupported, match="still be held"):
            ffi.send_key_chord("a", ("ctrl",))
        assert len(calls) == 1 + ffi._RELEASE_ATTEMPTS


class TestTheMouseRecords:
    """Every function here moves the operator's REAL cursor."""

    @staticmethod
    def _spy(monkeypatch, *, accept=None, raises=None, on_call=1) -> list:
        """Record every batch. *accept* truncates the *on_call*-th batch's count.

        ``SendInput`` returning fewer than it was given is the case that leaves the
        operator's real mouse button held, and it returns NORMALLY — so a spy that
        always reports full acceptance cannot exercise it.

        *on_call* exists because a drag is no longer one batch. It submits
        ``[move, down]``, then one call per path point, then ``[move, up]`` — so the
        state where the button is PHYSICALLY HELD is a truncation on an intermediate
        MOVE batch (call 2+), not on the press. Truncating call 1 only means the press
        never landed, which strands nothing. A spy pinned to the first batch could no
        longer reach the hazard at all, which is how a truncation test can keep passing
        while testing nothing.
        """
        batches: list = []
        calls = {"n": 0}

        def send(records):
            batches.append([(r.type, r.u.mi.dwFlags, r.u.mi.dx, r.u.mi.dy) for r in records])
            calls["n"] += 1
            if calls["n"] == on_call:
                if raises is not None:
                    raise raises
                if accept is not None:
                    return accept
            return len(records)

        monkeypatch.setattr(ffi, "_send", send)
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 1920, 1200))
        return batches

    def test_a_click_moves_THEN_presses_in_one_batch(self, monkeypatch) -> None:
        """One batch so no other thread's input interleaves: a move and a press
        submitted separately can have the pointer moved between them."""
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(100.0, 200.0, button="left", count=1)
        (batch,) = batches
        assert batch[0][1] & ffi.MOUSEEVENTF_MOVE
        assert batch[1][1] == ffi.MOUSEEVENTF_LEFTDOWN
        assert batch[2][1] == ffi.MOUSEEVENTF_LEFTUP

    def test_a_double_click_repeats_the_button_pair(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(10.0, 10.0, button="left", count=2)
        (batch,) = batches
        downs = [f for _t, f, _x, _y in batch if f == ffi.MOUSEEVENTF_LEFTDOWN]
        assert len(downs) == 2

    @pytest.mark.parametrize(
        ("button", "down"),
        [
            ("left", ffi.MOUSEEVENTF_LEFTDOWN),
            ("right", ffi.MOUSEEVENTF_RIGHTDOWN),
            ("middle", ffi.MOUSEEVENTF_MIDDLEDOWN),
        ],
    )
    def test_each_button_sends_its_OWN_codes(self, monkeypatch, button: str, down: int) -> None:
        """Substituting a button performs a different gesture: a right-click that
        quietly becomes a left-click activates instead of opening a menu."""
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(10.0, 10.0, button=button, count=1)
        assert any(f == down for _t, f, _x, _y in batches[0])

    def test_an_unknown_button_RAISES_rather_than_defaulting(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.post_mouse_click(1.0, 1.0, button="pinky", count=1)
        assert batches == []

    @pytest.mark.parametrize("accepted", [1, 2], ids=["after-move", "after-button-down"])
    def test_a_TRUNCATED_click_batch_releases_the_button(self, monkeypatch, accepted: int) -> None:
        """**A stuck real mouse button is data loss, not a failed click.**

        ``SendInput`` returns how many records it accepted and can stop early — a
        lower-level hook, or UIPI blocking the injection mid-batch — and it returns
        NORMALLY, so nothing raises. A batch cut between the button-down and its
        button-up leaves the operator's own mouse button physically held, and every
        subsequent motion of their hand becomes a drag: on a file manager that moves
        their files, on a canvas it destroys their work.

        ``accepted=2`` is the dangerous case (move + down delivered, up dropped);
        ``accepted=1`` is checked too, because the release must not depend on
        correctly guessing which side of the pair the cut fell on.
        """
        batches = self._spy(monkeypatch, accept=accepted)
        with pytest.raises(ffi.ComputerUseUnsupported, match="partly delivered"):
            ffi.post_mouse_click(10.0, 20.0, button="left", count=1)
        assert len(batches) == 2, "no release batch was submitted"
        assert batches[1] == [
            (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)
        ], "the follow-up batch must be exactly the button RELEASE"

    def test_a_RAISING_click_batch_also_releases(self, monkeypatch) -> None:
        """A batch can be partly applied before the call fails, so the release runs on
        the exception path as well — and the original error still propagates."""
        boom = OSError("SendInput exploded")
        batches = self._spy(monkeypatch, raises=boom)
        with pytest.raises(OSError, match="exploded"):
            ffi.post_mouse_click(10.0, 20.0, button="right", count=1)
        assert batches[-1] == [(ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_RIGHTUP, 0, 0)]

    def test_a_FULLY_accepted_click_sends_no_extra_release(self, monkeypatch) -> None:
        """The guard must not double-click: the batch already contains its own up."""
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(10.0, 20.0, button="left", count=1)
        assert len(batches) == 1

    def test_a_TRUNCATED_drag_releases_and_REPORTS(self, monkeypatch) -> None:
        """A drag that pressed and moved but never released IS the stuck-button state,
        and reporting success would leave the caller believing the drop landed.

        The ``finally`` covers an exception; this covers the quieter truncation, which
        returns normally.

        **The truncation has to land on an INTERMEDIATE move**, which is why this uses
        ``on_call=2``: the press batch is ``[move, down]``, so a short accept there means
        the down never landed and no button is held. The dangerous state — button
        physically down, remaining motion dropped — is reachable only after the press
        succeeded. A version of this test that truncated the first batch was passing
        while exercising nothing.
        """
        batches = self._spy(monkeypatch, accept=0, on_call=2)
        with pytest.raises(ffi.ComputerUseUnsupported, match="partly delivered"):
            ffi.post_mouse_drag(
                (1.0, 2.0),
                (30.0, 40.0),
                button="left",
                points=[(1.0, 2.0), (15.0, 20.0), (30.0, 40.0)],
            )
        # The press DID land, so a button really was held when the truncation happened.
        pressed = [rec for batch in batches for rec in batch if rec[1] == ffi.MOUSEEVENTF_LEFTDOWN]
        assert pressed, "the press never landed, so this did not exercise a held button"
        assert batches[-1][-1] == (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)

    def test_a_truncated_PRESS_batch_is_also_reported(self, monkeypatch) -> None:
        # The other half: a short accept on the press means nothing was pressed, so
        # there is no held button — but the caller must still not be told the drag
        # landed, because the drop did not happen either.
        batches = self._spy(monkeypatch, accept=1, on_call=1)
        with pytest.raises(ffi.ComputerUseUnsupported, match="partly delivered"):
            ffi.post_mouse_drag((1.0, 2.0), (30.0, 40.0), button="left")
        assert batches[-1][-1] == (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)

    def test_a_FULLY_accepted_drag_still_releases_exactly_once(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        ffi.post_mouse_drag((1.0, 2.0), (30.0, 40.0), button="left")
        # Asserted on the RELEASE rather than on the batch count, because the number
        # of batches is now a function of the path length (one call per point) and
        # pinning it here would make every path-shape change edit this test.
        assert batches[-1][-1] == (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)
        ups = [rec for batch in batches for rec in batch if rec[1] == ffi.MOUSEEVENTF_LEFTUP]
        assert len(ups) == 1, "the button was released more than once"

    def test_the_RELEASE_is_AIMED_at_the_end_point(self, monkeypatch) -> None:
        """A bare ``up`` releases wherever the cursor IS, not where it was aimed.

        The button-up record carries no coordinate. That was harmless while the whole
        drag went out as one batch — nothing could interleave — but the path is now
        submitted as one ``SendInput`` per point, so there are N yield points where a
        foreign mouse move (the operator's own hand, another injector) can land between
        the last path point and the release.

        A release is where a DROP lands, and ``end`` is one of the two points
        ``hwnd_owns_point`` authorized, so the release is re-aimed immediately before
        the ``up`` — in the SAME batch, which is what makes the pair uninterruptible.
        """
        batches = self._spy(monkeypatch)
        ffi.post_mouse_drag((1.0, 2.0), (300.0, 400.0), button="left")
        release = batches[-1]
        assert len(release) == 2, release
        assert release[0][1] & ffi.MOUSEEVENTF_MOVE
        assert (release[0][2], release[0][3]) == ffi._normalized(300.0, 400.0)
        assert release[1] == (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)

    def test_the_release_batch_is_built_BEFORE_the_travel(self, monkeypatch) -> None:
        """``_move_to`` can raise, and a release that raises is a HELD button.

        ``_normalized`` raises on a zero-extent virtual screen. If the release batch
        were constructed inside the ``finally``, that raise would happen on the way to
        sending the release rather than before the press — leaving the operator's
        button down, which is the one outcome the whole discipline exists to prevent.

        Driven by making construction fail: with a zero extent NOTHING is submitted, so
        the press never happens either and there is no button to strand.
        """
        batches: list = []
        monkeypatch.setattr(
            ffi, "_send", lambda records: batches.append(list(records)) or len(records)
        )
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 0, 0))
        with pytest.raises(ffi.ComputerUseUnsupported, match="zero extent"):
            ffi.post_mouse_drag((1.0, 2.0), (30.0, 40.0), button="left")
        assert batches == [], "a batch was submitted despite an unbuildable release"

    def test_a_drag_MOVES_between_press_and_release(self, monkeypatch) -> None:
        """A press and release at two points with no motion between is not a drag to
        most applications: they start the drag on the first move after button-down.

        The aim and the press stay in ONE batch — nothing may interleave between
        aiming and pressing — and the motion follows.
        """
        batches = self._spy(monkeypatch)
        ffi.post_mouse_drag((10.0, 10.0), (90.0, 90.0), button="left")
        press = batches[0]
        assert press[0][1] & ffi.MOUSEEVENTF_MOVE
        assert press[1][1] == ffi.MOUSEEVENTF_LEFTDOWN
        flat = [rec for batch in batches for rec in batch]
        down_at = next(i for i, rec in enumerate(flat) if rec[1] == ffi.MOUSEEVENTF_LEFTDOWN)
        up_at = next(i for i, rec in enumerate(flat) if rec[1] == ffi.MOUSEEVENTF_LEFTUP)
        moved_between = [rec for rec in flat[down_at + 1 : up_at] if rec[1] & ffi.MOUSEEVENTF_MOVE]
        assert moved_between, "no motion between the press and the release"

    def test_each_path_POINT_is_its_own_SendInput_call(self, monkeypatch) -> None:
        """THE fidelity invariant, and it looks backwards without the measurement.

        Every other input path in this module batches deliberately (``_send``'s own
        docstring explains why: nothing may interleave inside a chord). A drag path is
        the exception: 65 move records in ONE batch drew a visibly corner-y polyline,
        while the same 65 points as 65 separate calls drew a smooth curve. A
        ``WH_MOUSE_LL`` hook counted 65 of 65 events reaching the queue in BOTH shapes,
        so nothing is dropped — Windows synthesizes ``WM_MOUSEMOVE`` from queue STATE
        when the target's pump asks, so one batch mutates the position many times
        before the target samples it once.

        Pinned because a reviewer reading ``_send``'s docstring would reasonably
        "fix" this back into a single batch, and the regression is invisible in code:
        it produces a drag that still works and no longer draws.
        """
        batches = self._spy(monkeypatch)
        points = [(float(i * 10), 0.0) for i in range(6)]
        ffi.post_mouse_drag(points[0], points[-1], button="left", points=points)
        # One batch for (aim + press), then one per remaining point, then the release.
        assert len(batches) == 1 + (len(points) - 1) + 1
        for batch in batches[1:-1]:
            assert len(batch) == 1, "an intermediate move shared a batch"
            assert batch[0][1] & ffi.MOUSEEVENTF_MOVE

    def test_a_drag_RELEASES_even_when_the_press_batch_fails(self, monkeypatch) -> None:
        """A button left down is a stuck mouse button, and every later motion becomes an
        unintended drag.

        A wholly rejected batch (0 accepted) is also REPORTED, not just released: the
        caller would otherwise be told the drag succeeded while nothing was delivered,
        and would believe a drop landed somewhere it never did.
        """
        batches: list = []

        def send(records):
            batches.append([r.u.mi.dwFlags for r in records])
            return 0 if len(batches) == 1 else len(records)

        monkeypatch.setattr(ffi, "_send", send)
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 1920, 1200))
        with pytest.raises(ffi.ComputerUseUnsupported, match="partly delivered"):
            ffi.post_mouse_drag((1.0, 1.0), (2.0, 2.0), button="left")
        assert ffi.MOUSEEVENTF_LEFTUP in batches[-1]

    def test_a_drag_PRESSES_and_RELEASES_at_the_callers_endpoints(self, monkeypatch) -> None:
        """The path's own ends must not move the press or the release.

        Those two points are what the confinement check authorized, and a release is
        where a drop lands — so a path whose endpoints drifted by a float epsilon (or
        a caller that passed a path with different ends) must not relocate either.
        """
        batches = self._spy(monkeypatch)
        # A path deliberately disagreeing with the endpoints at BOTH ends.
        ffi.post_mouse_drag(
            (100.0, 100.0),
            (400.0, 400.0),
            button="left",
            points=[(1.0, 1.0), (250.0, 250.0), (999.0, 999.0)],
        )
        flat = [rec for batch in batches for rec in batch]
        aim = flat[0]
        release_batch = batches[-1]
        assert aim[1] & ffi.MOUSEEVENTF_MOVE
        assert (aim[2], aim[3]) == ffi._normalized(100.0, 100.0)
        assert release_batch[-1][1] == ffi.MOUSEEVENTF_LEFTUP
        # The last MOVE is the caller's end point, not the path's 999,999 — and it is
        # the one paired with the release, so the drop lands at the authorized point.
        moves = [rec for rec in flat if rec[1] & ffi.MOUSEEVENTF_MOVE]
        assert (moves[-1][2], moves[-1][3]) == ffi._normalized(400.0, 400.0)
        assert (release_batch[0][2], release_batch[0][3]) == ffi._normalized(400.0, 400.0)

    def test_an_unknown_drag_button_RAISES(self, monkeypatch) -> None:
        self._spy(monkeypatch)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.post_mouse_drag((1.0, 1.0), (2.0, 2.0), button="pinky")

    def test_a_move_is_ABSOLUTE_over_the_VIRTUAL_desktop(self, monkeypatch) -> None:
        """``VIRTUALDESK`` is required for a multi-monitor desktop; without it the
        coordinates are interpreted against the primary monitor alone."""
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(0.0, 0.0, button="left", count=1)
        move_flags = batches[0][0][1]
        assert move_flags & ffi.MOUSEEVENTF_ABSOLUTE
        assert move_flags & ffi.MOUSEEVENTF_VIRTUALDESK


class TestThePatternHelpers:
    """``pattern`` / ``pattern_action`` and the state readers, through a fake vtable."""

    @staticmethod
    def _vtable(monkeypatch, *, supported=True, action_hr=None, **outs):
        released: list = []

        def vcall(ptr, iface, method):
            def call(*args):
                if method == "GetCurrentPattern":
                    if not supported:
                        return -2147467259
                    args[-1]._obj.value = 0x6000
                    return ffi.S_OK
                if method in outs:
                    args[-1]._obj.value = outs[method]
                    return ffi.S_OK
                return ffi.S_OK if action_hr is None else action_hr

            return call

        monkeypatch.setattr(ffi, "vcall", vcall)
        monkeypatch.setattr(ffi, "release", lambda p: released.append(p))
        return released

    def test_an_unsupported_pattern_is_None_not_an_error(self, monkeypatch) -> None:
        """``None`` is the normal answer that makes the action ladder a ladder."""
        self._vtable(monkeypatch, supported=False)
        assert ffi.pattern(object(), ffi.UIA_InvokePatternId) is None

    def test_a_RAISING_probe_is_also_None(self, monkeypatch) -> None:
        def vcall(ptr, iface, method):
            raise OSError("the element died")

        monkeypatch.setattr(ffi, "vcall", vcall)
        assert ffi.pattern(object(), ffi.UIA_InvokePatternId) is None

    def test_a_supported_pattern_is_RELEASED_after_the_action(self, monkeypatch) -> None:
        """A tree walk mints thousands of references; leaking one per action is a real
        RSS bug the session watchdog would eventually recycle the process over."""
        released = self._vtable(monkeypatch)
        assert ffi.invoke_element(object()) == ffi.S_OK
        assert released, "the pattern reference was not released"

    def test_an_action_HRESULT_is_returned_not_swallowed(self, monkeypatch) -> None:
        """``None`` means unsupported and an int means it RAN — the ladder reads the
        difference to decide whether to try the next rung."""
        self._vtable(monkeypatch, action_hr=-2147467259)
        assert ffi.invoke_element(object()) == -2147467259

    @pytest.mark.parametrize(
        "helper",
        ["invoke_element", "toggle_element", "select_element", "do_default_action"],
    )
    def test_every_ladder_rung_reports_unsupported_as_None(self, monkeypatch, helper: str) -> None:
        self._vtable(monkeypatch, supported=False)
        assert getattr(ffi, helper)(object()) is None

    def test_expand_collapse_state_reads_the_state(self, monkeypatch) -> None:
        self._vtable(monkeypatch, get_CurrentExpandCollapseState=ffi.ExpandCollapseState_Expanded)
        assert ffi.expand_collapse_state(object()) == ffi.ExpandCollapseState_Expanded

    def test_a_LEAF_node_refuses_to_expand(self, monkeypatch) -> None:
        """The control cannot expand at all, and the provider's own error for that is
        indistinguishable from a transient failure."""
        self._vtable(monkeypatch, get_CurrentExpandCollapseState=ffi.ExpandCollapseState_LeafNode)
        assert ffi.expand_element(object(), expand=True) == ffi.E_ACCESSDENIED

    def test_a_collapsed_node_expands(self, monkeypatch) -> None:
        self._vtable(monkeypatch, get_CurrentExpandCollapseState=ffi.ExpandCollapseState_Collapsed)
        assert ffi.expand_element(object(), expand=True) == ffi.S_OK

    def test_a_read_only_field_refuses_BEFORE_any_write(self, monkeypatch) -> None:
        """And before allocating the BSTR, so the early exit cannot leak one."""
        allocated: list = []

        class _Oleaut:
            @staticmethod
            def SysAllocString(text):
                allocated.append(text)
                return 0xB57A

            @staticmethod
            def SysFreeString(ptr):
                pass

        self._vtable(monkeypatch, get_CurrentIsReadOnly=1)
        monkeypatch.setattr(ffi, "_libraries", lambda: {"oleaut32": _Oleaut()})
        assert ffi.set_element_value(object(), "x") == ffi.E_ACCESSDENIED
        assert allocated == []

    def test_no_value_pattern_is_None(self, monkeypatch) -> None:
        self._vtable(monkeypatch, supported=False)
        assert ffi.set_element_value(object(), "x") is None

    def test_a_NON_SCROLLING_axis_is_refused(self, monkeypatch) -> None:
        """``ScrollPercent`` reports the no-scroll sentinel for it, and asking a
        non-scrolling axis to move returns a failure a model reads as "retry"."""
        released: list = []

        def vcall(ptr, iface, method):
            def call(*args):
                if method == "GetCurrentPattern":
                    args[-1]._obj.value = 0x6000
                    return ffi.S_OK
                if method.startswith("get_Current") and "ScrollPercent" in method:
                    args[-1]._obj.value = ffi.UIA_ScrollPatternNoScroll
                    return ffi.S_OK
                return ffi.S_OK

            return call

        monkeypatch.setattr(ffi, "vcall", vcall)
        monkeypatch.setattr(ffi, "release", lambda p: released.append(p))
        assert (
            ffi.scroll_element(
                object(),
                horizontal=ffi.ScrollAmount_NoAmount,
                vertical=ffi.ScrollAmount_LargeIncrement,
            )
            == ffi.E_ACCESSDENIED
        )

    def test_a_scrollable_axis_scrolls(self, monkeypatch) -> None:
        self._vtable(monkeypatch, get_CurrentVerticalScrollPercent=0)
        assert (
            ffi.scroll_element(
                object(),
                horizontal=ffi.ScrollAmount_NoAmount,
                vertical=ffi.ScrollAmount_LargeIncrement,
            )
            == ffi.S_OK
        )

    def test_no_scroll_pattern_is_None(self, monkeypatch) -> None:
        self._vtable(monkeypatch, supported=False)
        assert (
            ffi.scroll_element(
                object(), horizontal=ffi.ScrollAmount_NoAmount, vertical=ffi.ScrollAmount_NoAmount
            )
            is None
        )


class TestElementResolutionAndText:
    def test_element_from_hwnd_treats_NULL_as_a_hard_error(self, monkeypatch) -> None:
        """A NULL result must NOT fall back to the desktop root: a desktop-scoped
        element walks every application's tree while looking like it worked."""

        def vcall(ptr, iface, method):
            def call(*args):
                return ffi.S_OK  # succeeds but leaves the out-param NULL

            return call

        monkeypatch.setattr(ffi, "automation", lambda: object())
        monkeypatch.setattr(ffi, "vcall", vcall)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.element_from_hwnd(0x1234)

    def test_element_from_hwnd_reports_a_failed_hresult(self, monkeypatch) -> None:
        def vcall(ptr, iface, method):
            def call(*args):
                return -2147467259

            return call

        monkeypatch.setattr(ffi, "automation", lambda: object())
        monkeypatch.setattr(ffi, "vcall", vcall)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.element_from_hwnd(0x1234)

    def test_a_NULL_bstr_is_the_empty_string(self, monkeypatch) -> None:
        assert ffi.bstr_value(None) == ""

    def test_a_zero_length_bstr_is_the_empty_string(self, monkeypatch) -> None:
        """Length comes from ``SysStringLen``, not a NUL scan: a BSTR carries its length
        and may contain an embedded NUL, which a scan would truncate."""
        monkeypatch.setattr(
            ffi, "_libraries", lambda: {"oleaut32": _FakeLib(SysStringLen=lambda p: 0)}
        )
        assert ffi.bstr_value(0x1234) == ""

    def test_window_text_is_empty_for_an_untitled_window(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ffi,
            "_libraries",
            lambda: {"user32": _FakeLib(GetWindowTextLengthW=lambda h: 0)},
        )
        assert ffi.window_text(0x10) == ""

    def test_window_bounds_is_None_when_the_rect_read_fails(self, monkeypatch) -> None:
        monkeypatch.setattr(
            ffi, "_libraries", lambda: {"user32": _FakeLib(GetWindowRect=lambda h, r: 0)}
        )
        assert ffi.window_bounds(0x10) is None

    def test_window_is_live_requires_BOTH_existence_and_visibility(self, monkeypatch) -> None:
        for exists, visible, expected in ((1, 1, True), (1, 0, False), (0, 1, False)):
            monkeypatch.setattr(
                ffi,
                "_libraries",
                lambda e=exists, v=visible: {
                    "user32": _FakeLib(IsWindow=lambda h: e, IsWindowVisible=lambda h: v)
                },
            )
            assert ffi.window_is_live(0x10) is expected

    def test_a_minimized_window_is_detected(self, monkeypatch) -> None:
        """It stays IsWindow and IsWindowVisible — that flag means "not explicitly
        hidden" — so liveness cannot answer this."""
        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _FakeLib(IsIconic=lambda h: 1)})
        assert ffi.window_is_minimized(0x10) is True

    def test_an_exe_name_is_empty_when_the_process_cannot_be_opened(self, monkeypatch) -> None:
        """An elevated process under UIPI, which must not become an exception."""
        monkeypatch.setattr(
            ffi, "_libraries", lambda: {"kernel32": _FakeLib(OpenProcess=lambda *a: 0)}
        )
        assert ffi._exe_name(4) == ""


class TestReferenceDiscipline:
    def test_release_tolerates_a_NULL(self) -> None:
        ffi.release(None)  # must not raise

    def test_release_never_raises(self, monkeypatch) -> None:
        """A failure to release leaks one object; raising would abort a tree walk
        partway through and leak all of them."""

        def vcall(ptr, iface, method):
            raise OSError("Release faulted")

        monkeypatch.setattr(ffi, "vcall", vcall)
        ffi.release(0x1234)  # must not raise

    def test_add_ref_tolerates_a_NULL(self) -> None:
        ffi.add_ref(None)  # must not raise

    def test_owned_releases_even_when_the_body_raises(self, monkeypatch) -> None:
        released: list = []
        monkeypatch.setattr(ffi, "release", lambda p: released.append(p))
        with pytest.raises(RuntimeError):
            with ffi.owned(0x1234):
                raise RuntimeError("the caller's own failure")
        assert released == [0x1234]

    def test_release_all_tolerates_NULLs_among_real_pointers(self, monkeypatch) -> None:
        released: list = []
        monkeypatch.setattr(ffi, "release", lambda p: released.append(p))
        ffi.release_all([0x1, None, 0x2])
        assert released == [0x1, None, 0x2]

    def test_reset_thread_state_clears_the_slot_and_the_generation(self, monkeypatch) -> None:
        """A stale generation is what stops a worker handing back a freed client."""
        import ctypes

        released: list = []
        ptr = ctypes.c_void_p(0x1234)
        monkeypatch.setattr(ffi, "release", lambda p: released.append(p))
        monkeypatch.setattr(ffi._thread_state, "automation", ptr, raising=False)
        monkeypatch.setattr(ffi, "_clients", [ptr])
        ffi.reset_thread_state()
        assert released == [ptr]
        assert ffi._thread_state.automation is None
        assert ffi._thread_state.com_ready is False
