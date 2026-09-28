"""One forward path reaches a Fargate crew, proved from the source tree.

A Fargate crew is reached by the instances layer's ``fargate`` connection method.
These checks are the guard on that being the only one: they read the tree with
``ast`` and refuse a second implementation, including one spelled differently from
anything named here.

Static on purpose. "Does a second forward path exist" is a question about which
code exists, not about what a call returns, and a path with no caller is exactly
the shape this guards against -- exercising the live path would say nothing about
it. Each check is a census whose expected set is written out in full, so a new
entry fails loudly and names itself instead of being absorbed by a predicate that
happens to admit it.

The censuses are small because the tree funnels through a few named seams:
``cloud.ssm.build_port_forward_argv`` is the only argv builder, and of the two
places that spawn a forward child only ``cloud.ssm.open_port_forward`` carries
``assert_human_action``. That helper also gates unrelated cloud verbs elsewhere, so
the census below scopes itself to the forward paths rather than to every call site.
The instances layer spawns its own supervised child, so it is reached by the argv
census rather than the spawner census -- which is why both exist here.

Reads through ``source_corpus`` rather than walking the tree directly: its text
cache is shared with every other tree gate and is dropped at module teardown, it
matches on NFKC-normalised text so a homoglyph identifier cannot slip past the
pre-filter, and it records a file it could not decode instead of going quietly
blind.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from source_corpus import parsed_candidates, src_root

#: One xdist group, so the five scanning checks land on one worker and the shared
#: corpus is read once rather than once per worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_cloud_fargate_single_forward_path")

#: The Systems Manager document that forwards a local port. Spelled out so the
#: census is a fact about the tree's text rather than about an import.
PORT_FORWARD_DOC = "AWS-StartPortForwardingSession"

#: Names the cloud lane does not define. Asserted absent everywhere rather than
#: absent from one module, because a definition under any other module leaves two
#: forwards again.
RETIRED = {"connect_fargate", "FargateConnection"}


def _rel(path: Path) -> str:
    """A package-relative path, so an expected set reads as module names."""
    return path.relative_to(src_root()).as_posix()


def _callee(node: ast.Call) -> str:
    """The bare name being called, whether dotted or plain."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _owner_names(tree: ast.AST) -> dict[int, str]:
    """Map each node to the name of the function enclosing it."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(id(child), node.name)
    return owner


def _call_census(name: str) -> set[tuple[str, str]]:
    """``(module, enclosing function)`` for every call of *name* in the package."""
    found: set[tuple[str, str]] = set()
    for path, _text, tree in parsed_candidates(require_all=(name,), skip_syntax_errors=False):
        owner = _owner_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) == name:
                found.add((_rel(path), owner.get(id(node), "<module>")))
    return found


def _functions_of(rel_path: str) -> dict[str, ast.AST]:
    """Every top-level-or-nested function in one module, by name."""
    tree = ast.parse((src_root() / rel_path).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _names_called_in(node: ast.AST) -> set[str]:
    return {_callee(n) for n in ast.walk(node) if isinstance(n, ast.Call)}


def _string_constants_in(node: ast.AST) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def test_no_module_defines_a_fargate_forward_in_the_cloud_lane():
    """The cloud lane owns no forward for a Fargate crew, under any module.

    Checked as a definition census rather than as an import error, because a
    module that is never imported by a test would still carry the second
    implementation this guards against.
    """
    defined = set()
    for name in sorted(RETIRED):
        for path, _text, tree in parsed_candidates(require_all=(name,), skip_syntax_errors=False):
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == name:
                        defined.add(f"{_rel(path)}::{node.name}")
    assert defined == set(), f"a second Fargate forward is defined: {sorted(defined)}"

    from kiro_crew.cloud import connect

    for name in sorted(RETIRED):
        assert not hasattr(connect, name), f"cloud.connect still exposes {name}"


def test_the_gated_spawner_has_exactly_the_two_ec2_callers():
    """Two callers reach the spawner that carries the human-action gate.

    This pins the reach of that gate and nothing wider. The instances layer's
    supervised forwarder does NOT come through here -- it spawns its own child on
    the shared argv and carries no ``assert_human_action`` -- so the count below
    is two, and the supervised path is pinned by the argv census instead. Both
    callers here address an EC2 instance; that they cannot address an ECS task is
    asserted separately.
    """
    assert _call_census("open_port_forward") == {
        ("cloud/connect.py", "connect"),
        ("cloud/login.py", "_start_callback_login"),
    }


def test_the_argv_builder_has_exactly_the_two_known_callers():
    """One argv builder serves both lanes, so neither can drift on the document.

    This is the census that covers the instances layer's supervised forwarder: it
    composes no argv of its own but delegates here, so a third caller means a
    forward was built somewhere new.
    """
    assert _call_census("build_port_forward_argv") == {
        ("cloud/ssm.py", "open_port_forward"),
        ("instances/ssh_tunnel_manager.py", "_build_ssm_tunnel_argv"),
    }


def test_only_three_modules_spell_the_port_forward_document():
    """A module naming the document itself is composing a forward by hand.

    ``cloud/ssm.py`` defines it, ``cloud/iam.py`` authorises it in a policy
    document, and the instances forwarder names it in prose. Anything else is a
    fourth place that decides what a forward looks like.
    """
    spelled = {
        _rel(path)
        for path, _text, tree in parsed_candidates(
            require_all=(PORT_FORWARD_DOC,), skip_syntax_errors=False
        )
        if any(PORT_FORWARD_DOC in value for value in _string_constants_in(tree))
    }
    assert spelled == {
        "cloud/ssm.py",
        "cloud/iam.py",
        "instances/ssh_tunnel_manager.py",
    }


def test_no_cloud_lane_forward_reaches_an_ecs_target():
    """A cloud-lane forward addresses an EC2 instance, never an ECS task.

    This is the invariant behind the name census above, and it holds whatever a
    re-added function is called: a Fargate forward has to both split an ECS
    target and open a forward, so no single cloud-lane function may do both.
    """
    offenders = []
    for path, _text, tree in parsed_candidates(
        require_all=("open_port_forward",), skip_syntax_errors=False
    ):
        rel = _rel(path)
        if not rel.startswith("cloud/"):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = _names_called_in(node)
            if "open_port_forward" not in called:
                continue
            if "split_ecs_target" in called:
                offenders.append(f"{rel}::{node.name} splits an ECS target")
            if any(value.startswith("ecs:") for value in _string_constants_in(node)):
                offenders.append(f"{rel}::{node.name} names an ECS target")
    assert offenders == [], f"a cloud-lane forward reaches an ECS task: {offenders}"


def test_the_opener_carries_the_gate_and_no_instances_forward_does():
    """The shared opener calls the human-action gate; nothing under ``instances/`` does.

    Those two halves are exactly what the opener's docstring rests on, which is why
    they are checked rather than asserted in prose: it tells a reader that gate
    covers its own two callers and no Fargate forward.

    Scoped deliberately. The cloud lane gates other sensitive verbs through the same
    helper -- stack and instance lifecycle in ``ec2.py`` -- and this says nothing
    about those. Pinning every call site would redden on an unrelated EC2 change
    while proving nothing more about the two forwards.
    """
    opener = _functions_of("cloud/ssm.py").get("open_port_forward")
    assert opener is not None, "the shared opener is gone"
    assert "assert_human_action" in _names_called_in(
        opener
    ), "the shared opener lost its human-action gate"

    in_instances = {
        (rel, fn) for rel, fn in _call_census("assert_human_action") if rel.startswith("instances/")
    }
    assert in_instances == set(), (
        f"the instances layer now gates a forward on a human action: {sorted(in_instances)} -- "
        f"update the opener's docstring, which says that gate covers only the cloud tunnels"
    )


def test_the_instances_layer_keeps_the_one_fargate_forward():
    """The surviving path is present, so the census above is not vacuously true.

    Without this, deleting the ``fargate`` transport would leave every check
    above passing and no way at all to reach a Fargate crew.
    """
    functions = _functions_of("instances/ssh_tunnel_manager.py")

    resolve = functions.get("_resolve_transport")
    assert resolve is not None, "the transport resolver is gone"
    assert "fargate" in _string_constants_in(resolve), "the fargate transport arm is gone"
    assert "split_ecs_target" in _names_called_in(
        resolve
    ), "the fargate arm does not validate that its target is an ECS task"

    mint = functions.get("_mint_for")
    assert mint is not None, "the mint seam is gone"
    assert "fargate" in _string_constants_in(mint), "the fargate mint refusal is gone"
