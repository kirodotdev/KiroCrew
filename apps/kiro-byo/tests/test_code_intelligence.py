from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from kiro_byo.code_intelligence import CodeIntelligenceEngine, CodeIntelligenceError
from kiro_byo.native_tools import NativeToolRuntime
from kiro_byo.sandbox import create_sandbox


def _engine(root: Path) -> CodeIntelligenceEngine:
    return CodeIntelligenceEngine(create_sandbox(root))


def test_python_symbols_work_without_tree_sitter_via_honest_ast_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sample.py").write_text(
            "class Motor:\n"
            "    def start(self):\n"
            "        def nested():\n"
            "            return 1\n"
            "        return nested()\n\n"
            "def build():\n"
            "    return Motor()\n",
            encoding="utf-8",
        )
        engine = _engine(root)
        result = engine.symbols("sample.py")
        names = {(item["kind"], item["name"], item["parent"]) for item in result["symbols"]}
        assert ("class", "Motor", None) in names
        assert ("method", "start", "Motor") in names
        assert ("function", "nested", "Motor.start") in names
        assert ("function", "build", None) in names
        if not engine.tree_sitter_available:
            assert result["backend"] == "python-ast-fallback"


def test_code_symbols_name_filter_and_overview() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.py").write_text("def alpha():\n    pass\n\ndef beta():\n    pass\n", encoding="utf-8")
        result = _engine(root).symbols("a.py", name="alpha")
        assert result["symbol_count"] == 1
        assert result["symbols"][0]["name"] == "alpha"
        assert "function alpha" in result["overview"]
        assert "beta" not in result["overview"]


def test_invalid_python_reports_fallback_parse_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        engine = _engine(root)
        if engine.tree_sitter_available:
            # Tree-sitter is error-tolerant by design; parsing a partial file is a feature.
            result = engine.symbols("bad.py")
            assert result["backend"] == "tree-sitter"
        else:
            with pytest.raises(CodeIntelligenceError, match="fallback could not parse"):
                engine.symbols("bad.py")


def test_non_python_needs_tree_sitter_when_optional_runtime_is_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.js").write_text("function hello() {}\n", encoding="utf-8")
        engine = _engine(root)
        if engine.tree_sitter_available:
            result = engine.symbols("a.js")
            assert any(item["name"] == "hello" for item in result["symbols"])
        else:
            with pytest.raises(CodeIntelligenceError, match="Tree-sitter is required"):
                engine.symbols("a.js")


def test_code_query_is_never_faked_when_tree_sitter_is_absent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
        engine = _engine(root)
        if engine.tree_sitter_available:
            result = engine.query("a.py", "(function_definition name: (identifier) @name)")
            assert result["backend"] == "tree-sitter"
            assert any(match["text"] == "alpha" for match in result["matches"])
        else:
            with pytest.raises(CodeIntelligenceError, match="requires Tree-sitter"):
                engine.query("a.py", "(function_definition) @fn")


def test_runtime_advertises_fallback_symbols_but_hides_query_without_tree_sitter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.py").write_text("def alpha():\n    pass\n", encoding="utf-8")
        runtime = NativeToolRuntime(root)
        try:
            names = {spec.name for spec in runtime.specs()}
            assert "code_symbols" in names
            if runtime.policy_summary()["code_intelligence"]["tree_sitter_available"]:
                assert "code_query" in names
            else:
                assert "code_query" not in names
            invocation = runtime.validate("code_symbols", {"path": "a.py"})
            result = json.loads(runtime.execute(invocation).text)
            assert result["symbols"][0]["name"] == "alpha"
        finally:
            runtime.close()


def test_unsupported_extension_fails_clearly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes.txt").write_text("hello", encoding="utf-8")
        with pytest.raises(CodeIntelligenceError, match="unsupported code file extension"):
            _engine(root).symbols("notes.txt")
