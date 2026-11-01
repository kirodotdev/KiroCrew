#!/usr/bin/env python3
"""Architecture V9: fail closed on reverse dependencies into runtime controllers.

This guard is deliberately narrow: it does not make a global "no cycles" claim.
It constrains the extracted leaf seams so new Modes, record processing and
ACP content helpers cannot silently import the coordinating runtime owners.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEAMS = (
    'routing_events', 'prompt_routing', 'provider_loop_terminal',
    'native_tool_dispatch', 'acp_content', 'mode_router', 'recovery_policy',
)
CONTROLLERS = frozenset({'turn_runner', 'acp_server'})


def forbidden_imports(source: str, module: str) -> list[str]:
    """Inspect actual import nodes, not comments or string literals."""
    tree = ast.parse(source, filename=module)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                name = item.name
                if (name.startswith('backend.') or name == 'backend'
                    or any(name == 'kiro_byo.' + x or name.startswith('kiro_byo.' + x + '.')
                           for x in CONTROLLERS)):
                    problems.append(f'{module}:{node.lineno}: {name}')
        elif isinstance(node, ast.ImportFrom):
            pkg = node.module or ''
            names = [alias.name for alias in node.names]
            if (pkg.startswith('backend') or pkg in CONTROLLERS
                or pkg.startswith('kiro_byo.') and pkg.split('.')[1] in CONTROLLERS
                or (node.level and not pkg and any(name in CONTROLLERS for name in names))
                or (pkg == 'kiro_byo' and any(name in CONTROLLERS for name in names))):
                problems.append(f'{module}:{node.lineno}: from {pkg or "."} import {names}')
    return problems


def check(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for module in SEAMS:
        path = root / 'src' / 'kiro_byo' / (module + '.py')
        if not path.is_file():
            errors.append(f'missing leaf module: {path}')
            continue
        errors.extend(forbidden_imports(path.read_text(encoding='utf-8'), module))
    return errors


def main() -> int:
    errors = check()
    if errors:
        print('\n'.join('FAIL: ' + error for error in errors), file=sys.stderr)
        return 1
    print(f'PASS: {len(SEAMS)} architecture leaf seams have no controller reverse imports')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
