"""The last status line a foreground gateway prints says it is running.

`run()` parks on ``shutdown_event.wait()`` once boot is done, so whatever it
printed last stays on screen for the whole life of the process. A line that
reads as a step still in progress makes a healthy gateway look hung.
"""

import ast
import inspect
import textwrap

from kiro_crew.slack.gateway_runtime import service_boot


def _run_body() -> list[ast.stmt]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(service_boot.run)))
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    return fn.body


def _printed_literal(stmt: ast.stmt) -> str | None:
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    call = stmt.value
    if not (isinstance(call.func, ast.Name) and call.func.id == "print" and call.args):
        return None
    arg = call.args[0]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _is_shutdown_wait(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Await)
        and "shutdown_event.wait" in ast.unparse(stmt.value)
    )


def test_last_print_before_the_shutdown_wait_says_running():
    body = _run_body()
    wait_at = next(i for i, stmt in enumerate(body) if _is_shutdown_wait(stmt))
    printed = [
        (i, text) for i, stmt in enumerate(body[:wait_at]) if (text := _printed_literal(stmt))
    ]
    assert printed, "run() prints no status line before it parks"
    _, last = printed[-1]
    assert "running" in last
    assert "Ctrl+C" in last
    assert "starting" not in last


def test_running_line_comes_after_the_slack_connect():
    body = _run_body()
    connect_at = next(i for i, stmt in enumerate(body) if "_connect_slack" in ast.unparse(stmt))
    running_at = next(
        i for i, stmt in enumerate(body) if "running" in (_printed_literal(stmt) or "")
    )
    assert connect_at < running_at
