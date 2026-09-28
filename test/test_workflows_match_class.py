"""The workflow validator must see attribute reads hidden in class patterns.

``ast.MatchClass.kwd_attrs`` is a list of plain strings, so a ``case C(x=v)``
keyword never appears as an ``Attribute`` node, yet CPython resolves it with
``getattr(subject, "x")`` at run time.
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.validate import validate

_TEMPLATE = (
    'META = {{"name": "m"}}\n\n\n'
    "async def workflow(ctx):\n"
    "    match {{}}:\n"
    "        case dict({kw}=v):\n"
    "            return v\n"
    "    return None\n"
)


@pytest.mark.parametrize(
    "kw", ["__class__", "__globals__", "__subclasses__", "_session_key", "gi_frame", "format"]
)
def test_forbidden_attribute_in_class_pattern_is_rejected(kw: str) -> None:
    vr = validate(_TEMPLATE.format(kw=kw))
    assert vr.ok is False
    assert any("match pattern" in e for e in vr.errors), vr.errors


def test_public_attribute_in_class_pattern_still_passes() -> None:
    vr = validate(_TEMPLATE.format(kw="args"))
    assert vr.ok is True, vr.errors


def test_mro_is_rejected() -> None:
    src = 'META = {"name": "m"}\n\n\nasync def workflow(ctx):\n    return str(dict.mro())\n'
    vr = validate(src)
    assert vr.ok is False
    assert any("mro" in e for e in vr.errors), vr.errors
