"""Code-structure intelligence with an optional Tree-sitter backend.

Tree-sitter is deliberately an adapter, not a hard import at process startup.
KiroCrew in-gateway Apps share Crew's interpreter and the App Kit does not
install arbitrary Python wheel dependencies for hook backends.  When the
Tree-sitter packages are available we use them for Python/JavaScript/TypeScript;
without them, Python symbol/overview requests fall back to stdlib ``ast`` and
Tree-sitter-only queries fail explicitly instead of pretending parity.
"""
from __future__ import annotations

import ast
import importlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .sandbox import WorkspaceSandboxBackend


class CodeIntelligenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodeSymbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    parent: str = ""
    node_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "parent": self.parent or None,
            "node_type": self.node_type or None,
        }


_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}


class _TreeSitterAdapter:
    def __init__(self) -> None:
        try:
            tree_sitter = importlib.import_module("tree_sitter")
        except ImportError as exc:
            raise CodeIntelligenceError(
                "Tree-sitter runtime is not installed. Install Kiro BYO's code-intel extra "
                "into the Python environment that runs KiroCrew."
            ) from exc
        self.Language = getattr(tree_sitter, "Language")
        self.Parser = getattr(tree_sitter, "Parser")
        self.Query = getattr(tree_sitter, "Query")
        self.QueryCursor = getattr(tree_sitter, "QueryCursor")
        try:
            self._grammar_modules = {
                "python": importlib.import_module("tree_sitter_python"),
                "javascript": importlib.import_module("tree_sitter_javascript"),
                "typescript": importlib.import_module("tree_sitter_typescript"),
                "tsx": importlib.import_module("tree_sitter_typescript"),
            }
        except ImportError as exc:
            raise CodeIntelligenceError(
                "Tree-sitter core is installed but one or more Kiro BYO code-intel grammars are missing. "
                "Install the complete code-intel extra into the Python environment that runs KiroCrew."
            ) from exc
        self._languages: dict[str, Any] = {}
        # Capability honesty: loading the Python package names is not enough.
        # Validate every grammar capsule against the installed Tree-sitter ABI
        # before advertising the Tree-sitter-only code_query tool.
        for language in ("python", "javascript", "typescript", "tsx"):
            self._language(language)

    def _language(self, language: str) -> Any:
        if language in self._languages:
            return self._languages[language]
        try:
            module = self._grammar_modules.get(language)
            if language == "python" and module is not None:
                capsule = module.language()
            elif language == "javascript" and module is not None:
                capsule = module.language()
            elif language == "typescript" and module is not None:
                capsule = module.language_typescript()
            elif language == "tsx" and module is not None:
                capsule = module.language_tsx()
            else:
                raise CodeIntelligenceError(f"unsupported Tree-sitter language: {language}")
        except (ImportError, AttributeError) as exc:
            raise CodeIntelligenceError(
                f"Tree-sitter grammar is unavailable for {language}; install the code-intel extra."
            ) from exc
        try:
            lang = self.Language(capsule)
        except Exception as exc:
            raise CodeIntelligenceError(
                f"Tree-sitter grammar for {language} is incompatible with the installed runtime: {exc}"
            ) from exc
        self._languages[language] = lang
        return lang

    def parse(self, language: str, source: bytes) -> tuple[Any, Any]:
        lang = self._language(language)
        try:
            parser = self.Parser(lang)
        except TypeError:
            parser = self.Parser()
            parser.language = lang
        tree = parser.parse(source)
        return lang, tree

    def query(self, language: str, source: bytes, query_text: str) -> list[dict[str, Any]]:
        lang, tree = self.parse(language, source)
        try:
            query = self.Query(lang, query_text)
            cursor = self.QueryCursor(query)
            captures = cursor.captures(tree.root_node)
        except Exception as exc:
            raise CodeIntelligenceError(f"invalid Tree-sitter query: {exc}") from exc
        out: list[dict[str, Any]] = []
        if isinstance(captures, dict):
            items = ((name, node) for name, nodes in captures.items() for node in nodes)
        else:  # compatibility with older Python bindings returning (node, capture-name)
            items = ((name, node) for node, name in captures)
        for capture, node in items:
            out.append(
                {
                    "capture": str(capture),
                    "node_type": str(node.type),
                    "start_line": int(node.start_point[0]) + 1,
                    "end_line": int(node.end_point[0]) + 1,
                    "text": source[node.start_byte : node.end_byte].decode("utf-8", errors="replace"),
                }
            )
        return out


def tree_sitter_runtime_available() -> bool:
    """Return True only when the core binding and all advertised grammars load."""
    try:
        _TreeSitterAdapter()
    except CodeIntelligenceError:
        return False
    return True


class CodeIntelligenceEngine:
    MAX_SOURCE_BYTES = 2 * 1024 * 1024

    def __init__(self, sandbox: WorkspaceSandboxBackend) -> None:
        self.sandbox = sandbox
        self._ts: _TreeSitterAdapter | None = None
        self._ts_error = ""
        try:
            self._ts = _TreeSitterAdapter()
        except CodeIntelligenceError as exc:
            self._ts_error = str(exc)

    @property
    def tree_sitter_available(self) -> bool:
        return self._ts is not None

    def status(self) -> dict[str, Any]:
        return {
            "tree_sitter_available": self.tree_sitter_available,
            "tree_sitter_error": self._ts_error or None,
            "languages": ["python", "javascript", "typescript", "tsx"] if self.tree_sitter_available else ["python"],
            "python_fallback": True,
        }

    @staticmethod
    def language_for(path: Path) -> str | None:
        return _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())

    def _load(self, raw_path: str) -> tuple[Path, str, bytes]:
        path = self.sandbox.resolve_read_path(raw_path)
        if not path.is_file():
            raise CodeIntelligenceError(f"code intelligence target is not a file: {raw_path}")
        language = self.language_for(path)
        if language is None:
            raise CodeIntelligenceError(f"unsupported code file extension: {path.suffix or '<none>'}")
        try:
            source = path.read_bytes()
        except OSError as exc:
            raise CodeIntelligenceError(f"cannot read source file: {exc}") from exc
        if len(source) > self.MAX_SOURCE_BYTES:
            raise CodeIntelligenceError(
                f"source file is too large for structural analysis ({len(source)} bytes > {self.MAX_SOURCE_BYTES})"
            )
        return path, language, source

    @staticmethod
    def _node_name(node: Any, source: bytes) -> str:
        try:
            name_node = node.child_by_field_name("name")
        except Exception:
            name_node = None
        if name_node is None:
            return ""
        return source[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")

    @classmethod
    def _ts_symbols(cls, language: str, source: bytes, root: Any) -> list[CodeSymbol]:
        mappings: dict[str, dict[str, str]] = {
            "python": {
                "class_definition": "class",
                "function_definition": "function",
            },
            "javascript": {
                "class_declaration": "class",
                "function_declaration": "function",
                "method_definition": "method",
            },
            "typescript": {
                "class_declaration": "class",
                "function_declaration": "function",
                "method_definition": "method",
                "interface_declaration": "interface",
                "type_alias_declaration": "type",
                "enum_declaration": "enum",
            },
            "tsx": {
                "class_declaration": "class",
                "function_declaration": "function",
                "method_definition": "method",
                "interface_declaration": "interface",
                "type_alias_declaration": "type",
                "enum_declaration": "enum",
            },
        }
        kinds = mappings.get(language, {})
        out: list[CodeSymbol] = []

        def visit(node: Any, parent_symbol: str = "", parent_kind: str = "") -> None:
            kind = kinds.get(str(node.type))
            if language == "python" and str(node.type) == "function_definition" and parent_kind == "class":
                kind = "method"
            next_parent = parent_symbol
            next_parent_kind = parent_kind
            if kind:
                name = cls._node_name(node, source)
                if name:
                    out.append(
                        CodeSymbol(
                            name=name,
                            kind=kind,
                            start_line=int(node.start_point[0]) + 1,
                            end_line=int(node.end_point[0]) + 1,
                            parent=parent_symbol,
                            node_type=str(node.type),
                        )
                    )
                    next_parent = f"{parent_symbol}.{name}" if parent_symbol else name
                    next_parent_kind = kind
            for child in getattr(node, "named_children", ()):
                visit(child, next_parent, next_parent_kind)

        visit(root)
        return out

    @staticmethod
    def _python_ast_symbols(source: bytes) -> list[CodeSymbol]:
        try:
            text = source.decode("utf-8")
            tree = ast.parse(text)
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise CodeIntelligenceError(f"Python AST fallback could not parse source: {exc}") from exc
        out: list[CodeSymbol] = []

        def visit_node(node: ast.AST, parent: str = "", parent_kind: str = "") -> None:
            symbol_kind = ""
            symbol_name = ""
            if isinstance(node, ast.ClassDef):
                symbol_kind, symbol_name = "class", node.name
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol_kind = "method" if parent_kind == "class" else "function"
                symbol_name = node.name

            next_parent = parent
            next_parent_kind = parent_kind
            if symbol_kind:
                end = int(getattr(node, "end_lineno", getattr(node, "lineno", 1)) or getattr(node, "lineno", 1))
                start_line = int(getattr(node, "lineno", 1))
                out.append(
                    CodeSymbol(
                        name=symbol_name,
                        kind=symbol_kind,
                        start_line=start_line,
                        end_line=end,
                        parent=parent,
                        node_type=type(node).__name__,
                    )
                )
                next_parent = f"{parent}.{symbol_name}" if parent else symbol_name
                next_parent_kind = symbol_kind

            for child in ast.iter_child_nodes(node):
                visit_node(child, next_parent, next_parent_kind)

        visit_node(tree)
        return out

    @staticmethod
    def _overview(symbols: list[CodeSymbol]) -> str:
        if not symbols:
            return "(no class/function/type symbols found)"
        lines: list[str] = []
        for symbol in symbols:
            depth = symbol.parent.count(".") + (1 if symbol.parent else 0)
            indent = "  " * depth
            lines.append(
                f"{indent}- {symbol.kind} {symbol.name} [L{symbol.start_line}-L{symbol.end_line}]"
            )
        return "\n".join(lines)

    def symbols(self, raw_path: str, *, name: str = "", max_results: int = 200) -> dict[str, Any]:
        path, language, source = self._load(raw_path)
        backend = "python-ast-fallback"
        if self._ts is not None:
            try:
                _lang, tree = self._ts.parse(language, source)
                symbols = self._ts_symbols(language, source, tree.root_node)
                backend = "tree-sitter"
            except CodeIntelligenceError:
                if language != "python":
                    raise
                symbols = self._python_ast_symbols(source)
        elif language == "python":
            symbols = self._python_ast_symbols(source)
        else:
            raise CodeIntelligenceError(
                f"Tree-sitter is required for {language}; {self._ts_error or 'runtime unavailable'}"
            )
        if name:
            try:
                matcher = re.compile(name)
            except re.error as exc:
                raise CodeIntelligenceError(f"invalid symbol name regex: {exc}") from exc
            symbols = [s for s in symbols if matcher.search(s.name) or matcher.search(f"{s.parent}.{s.name}".strip("."))]
        symbols = symbols[: max(1, min(int(max_results), 1000))]
        return {
            "path": self.sandbox.relative_display(path),
            "language": language,
            "backend": backend,
            "symbol_count": len(symbols),
            "overview": self._overview(symbols),
            "symbols": [symbol.as_dict() for symbol in symbols],
        }

    def query(self, raw_path: str, query_text: str, *, max_results: int = 200) -> dict[str, Any]:
        path, language, source = self._load(raw_path)
        if self._ts is None:
            raise CodeIntelligenceError(
                "code_query requires Tree-sitter; Python AST fallback only supports code_symbols. "
                + (self._ts_error or "")
            )
        matches = self._ts.query(language, source, query_text)
        matches = matches[: max(1, min(int(max_results), 1000))]
        return {
            "path": self.sandbox.relative_display(path),
            "language": language,
            "backend": "tree-sitter",
            "match_count": len(matches),
            "matches": matches,
        }

    def symbols_text(self, raw_path: str, *, name: str = "", max_results: int = 200) -> str:
        return json.dumps(self.symbols(raw_path, name=name, max_results=max_results), ensure_ascii=False, indent=2)

    def query_text(self, raw_path: str, query_text: str, *, max_results: int = 200) -> str:
        return json.dumps(self.query(raw_path, query_text, max_results=max_results), ensure_ascii=False, indent=2)
