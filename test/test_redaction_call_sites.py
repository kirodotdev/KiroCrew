"""Egress call sites redact through the composed pass, never credentials first.

``security.redact_with_findings`` composes the outbound-URL pass and the
credential pass on the original text. Code that still calls the two raw passes
must at least never feed the credential pass's output into the URL pass, which
hides a URL from the URL pass.

Two checks:

* helpers picked from several surfaces give the composed result;
* a scan of ``src/`` finds no site that feeds the credential pass's output into the
  URL pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

_CREDENTIAL_PASSES = frozenset({"redact_credentials", "redact_credentials_with_records"})
_URL_PASSES = frozenset({"redact_exfiltration_urls", "redact_exfiltration_urls_with_records"})

# A URL to an unknown host whose query carries a credential-shaped value.
_TEXT = (
    "see https://collector.example/c?d=AKIAIOSFODNN7EXAMPLE"
    "&s=wJalrXUtnFEMI%2FK7MDENG%2FbPxRfiCYEXAMPLEKEY&pad=" + "x" * 40 + " now"
)
_PAD = "a" * 40  # enough query to make the URL pass flag the address


def _helpers() -> list[tuple[str, object]]:
    from kiro_crew import snapshot_redact
    from kiro_crew.apps import routes
    from kiro_crew.apps.builtins.aws_control.backend import accounts
    from kiro_crew.apps.builtins.aws_control.backend.backup_parts import egress_text
    from kiro_crew.apps.builtins.dev_fleet import runtime as dev_fleet_runtime
    from kiro_crew.apps.builtins.mochi import redact as mochi_redact

    return [
        ("snapshot_redact._scrub", lambda s: snapshot_redact._scrub(s)[0]),
        ("apps.routes._redact_warning", routes._redact_warning),
        ("aws_control.accounts._safe_field", accounts._safe_field),
        ("aws_control.egress_text._redact_egress", egress_text._redact_egress),
        ("dev_fleet.runtime._redact", dev_fleet_runtime._redact),
        ("mochi.redact.redact_tree", mochi_redact.redact_tree),
    ]


@pytest.mark.parametrize("name", [n for n, _ in _helpers()])
def test_egress_helper_removes_the_url_whole(name: str) -> None:
    helper = dict(_helpers())[name]
    out = helper(_TEXT)
    assert "collector.example/c?" not in out
    assert "[REDACTED: suspicious URL to collector.example]" in out


@pytest.mark.parametrize("name", [n for n, _ in _helpers()])
def test_egress_helper_removes_a_credential_that_spans_a_url_end(name: str) -> None:
    helper = dict(_helpers())[name]
    out = helper(f"https://collector.example/?pad={_PAD}&token=)opaque-value-xyz")
    assert "opaque-value" not in out


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _first_target(stmt: ast.Assign) -> str | None:
    """Source text of the first assignment target: a name, ``self.x`` or ``d[k]``."""
    target = stmt.targets[0]
    if isinstance(target, ast.Tuple) and target.elts:
        target = target.elts[0]
    if isinstance(target, (ast.Name, ast.Attribute, ast.Subscript)):
        return ast.unparse(target)
    return None


def _reads(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, (ast.Name, ast.Attribute, ast.Subscript)) and ast.unparse(n) == name
        for n in ast.walk(node)
    )


def _rebinds(stmt: ast.stmt, name: str) -> bool:
    if not isinstance(stmt, ast.Assign):
        return False
    targets = [e for t in stmt.targets for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
    return any(ast.unparse(t) == name for t in targets)


def _credential_first_sites(tree: ast.AST) -> list[int]:
    """Lines where the credential pass's output is the URL pass's input.

    Two shapes: a URL-pass call whose first argument contains a credential-pass
    call, and an assignment from the credential pass whose target the URL pass
    (a name, attribute or subscript) reads within the next three statements of
    the same block.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) in _URL_PASSES and node.args:
            if any(_callee(c) in _CREDENTIAL_PASSES for c in _calls(node.args[0])):
                lines.append(node.lineno)
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
                    continue
                if _callee(stmt.value) not in _CREDENTIAL_PASSES:
                    continue
                name = _first_target(stmt)
                if name is None:
                    continue
                for nxt in block[i + 1 : i + 4]:
                    if any(
                        _callee(c) in _URL_PASSES and c.args and _reads(c.args[0], name)
                        for c in _calls(nxt)
                    ):
                        lines.append(stmt.lineno)
                        break
                    if _rebinds(nxt, name):
                        break
    return sorted(set(lines))


def test_no_source_site_runs_credentials_before_urls() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "redact_credentials" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        offenders.extend(f"{path.relative_to(SRC)}:{ln}" for ln in _credential_first_sites(tree))
    assert (
        offenders == []
    ), "run the URL pass before the credential pass (or call security.redact): " + ", ".join(
        offenders
    )


@pytest.mark.parametrize(
    "source",
    [
        "a, _ = redact_credentials(t)\nb, _ = redact_exfiltration_urls(a)\n",
        "x = redact_exfiltration_urls(redact_credentials(t)[0])[0]\n",
        "a, _ = security.redact_credentials(t)\nn = 1\na, _ = security.redact_exfiltration_urls(a)\n",
        "p[k], _ = redact_credentials(p[k])\np[k], _ = redact_exfiltration_urls(p[k])\n",
        "self.v, _ = redact_credentials(t)\nself.v, _ = redact_exfiltration_urls(self.v)\n",
    ],
)
def test_scan_flags_credential_first_shapes(source: str) -> None:
    assert _credential_first_sites(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "a, _ = redact_exfiltration_urls(t)\na, _ = redact_credentials(a)\n",
        "x = redact_credentials(redact_exfiltration_urls(t)[0])[0]\n",
        "_, c = redact_credentials(t)\n_, u = redact_exfiltration_urls(t)\n",
        "a, _ = redact_credentials(t)\na = other(t)\nb, _ = redact_exfiltration_urls(a)\n",
    ],
)
def test_scan_passes_url_first_and_independent_shapes(source: str) -> None:
    assert _credential_first_sites(ast.parse(source)) == []
